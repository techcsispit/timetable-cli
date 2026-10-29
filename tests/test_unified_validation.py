"""Comprehensive test suite for Issue #77: Unified Validation Engine with Structured Conflict Explanations."""

import json
from pathlib import Path
from timetable.diagnostics import (
    Diagnostic,
    Provenance,
    AffectedEntry,
    RuleId,
    Severity,
    ValidationReport,
    render_diagnostics,
)
from timetable.engine import (
    validate_timetable,
    validate_timetable_structure,
    validate_duplicates,
    validate_temporal_conflicts,
    validate_csv_headers,
    validate_csv_row,
)
from timetable.import_csv import plan_import
from timetable.import_ics import plan_ics_import
from timetable.merge import merge_three_way, MergeConflict


# =============================================================================
# 1. Diagnostic Data Model & Serialization Tests
# =============================================================================

def test_diagnostic_required_fields():
    """Verify diagnostic construction with all required fields."""
    diag = Diagnostic(
        rule_id=RuleId.SCHEMA_INVALID,
        severity=Severity.ERROR,
        message="Invalid root structure",
    )
    assert diag.rule_id == RuleId.SCHEMA_INVALID
    assert diag.severity == Severity.ERROR
    assert diag.message == "Invalid root structure"
    assert diag.is_error
    assert not diag.is_warning
    assert diag.affected_entries == []
    assert diag.source is None
    assert diag.context == {}


def test_severity_ordering():
    """Verify severity string values and equality."""
    assert Severity.ERROR == "error"
    assert Severity.WARNING == "warning"
    assert Severity.INFO == "info"


def test_rule_id_enums():
    """Verify stable rule IDs across domain areas."""
    assert RuleId.SCHEMA_INVALID == "SCHEMA_INVALID"
    assert RuleId.VERSION_UNSUPPORTED == "VERSION_UNSUPPORTED"
    assert RuleId.WEEKDAY_INVALID == "WEEKDAY_INVALID"
    assert RuleId.ENTRY_MALFORMED == "ENTRY_MALFORMED"
    assert RuleId.TIME_INVALID == "TIME_INVALID"
    assert RuleId.INTERVAL_INVALID == "INTERVAL_INVALID"
    assert RuleId.ENTRY_DUPLICATE == "ENTRY_DUPLICATE"
    assert RuleId.SCHEDULE_CONFLICT == "SCHEDULE_CONFLICT"
    assert RuleId.SCHEDULE_CONFLICT_OVERNIGHT == "SCHEDULE_CONFLICT_OVERNIGHT"
    assert RuleId.IMPORT_HEADER_INVALID == "IMPORT_HEADER_INVALID"
    assert RuleId.IMPORT_RECORD_MALFORMED == "IMPORT_RECORD_MALFORMED"
    assert RuleId.MERGE_STRUCTURAL_CONFLICT == "MERGE_STRUCTURAL_CONFLICT"
    assert RuleId.MERGE_SCHEDULE_CONFLICT == "MERGE_SCHEDULE_CONFLICT"


def test_diagnostic_serialization():
    """Verify to_dict and JSON serialization."""
    prov = Provenance(source_type="csv", source_path="test.csv", row=12)
    entry = AffectedEntry(day="monday", slot={"subject": "Math", "start": "09:00", "end": "10:00", "room": "101"}, provenance=prov)
    diag = Diagnostic(
        rule_id=RuleId.ENTRY_MALFORMED,
        severity=Severity.ERROR,
        message="Malformed entry",
        affected_entries=[entry],
        source=prov,
        context={"field": "start"},
    )
    d = diag.to_dict()
    assert d["rule_id"] == "ENTRY_MALFORMED"
    assert d["severity"] == "error"
    assert d["message"] == "Malformed entry"
    assert d["source"]["row"] == 12
    assert d["affected_entries"][0]["day"] == "monday"
    # Ensure serializable to JSON
    dumped = json.dumps(d)
    assert "ENTRY_MALFORMED" in dumped


def test_diagnostic_sorting_and_deduplication():
    """Verify deterministic sorting and deduplication in ValidationReport."""
    diag1 = Diagnostic(
        rule_id=RuleId.SCHEDULE_CONFLICT,
        severity=Severity.ERROR,
        message="Conflict B",
        affected_entries=[AffectedEntry(day="tuesday", slot={"subject": "B", "start": "11:00", "end": "12:00", "room": "1"})],
    )
    diag2 = Diagnostic(
        rule_id=RuleId.SCHEDULE_CONFLICT,
        severity=Severity.ERROR,
        message="Conflict A",
        affected_entries=[AffectedEntry(day="monday", slot={"subject": "A", "start": "09:00", "end": "10:00", "room": "1"})],
    )
    diag3 = Diagnostic(
        rule_id=RuleId.SCHEDULE_CONFLICT,
        severity=Severity.ERROR,
        message="Conflict A",
        affected_entries=[AffectedEntry(day="monday", slot={"subject": "A", "start": "09:00", "end": "10:00", "room": "1"})],
    )
    report = ValidationReport()
    report.add(diag1)
    report.add(diag2)
    report.add(diag3)
    deduped = report.deduplicated()
    assert len(deduped.diagnostics) == 2
    # Monday must sort before Tuesday
    sorted_diags = deduped.sorted_diagnostics()
    assert sorted_diags[0].message == "Conflict A"
    assert sorted_diags[1].message == "Conflict B"


# =============================================================================
# 2. Structural & Format Validation Tests
# =============================================================================

def test_validate_non_dict_timetable():
    """Timetable root must be a dict."""
    report = validate_timetable(["invalid", "list"])
    assert not report.is_valid
    assert any(d.rule_id == RuleId.SCHEMA_INVALID for d in report.errors)


def test_validate_unsupported_schema_version():
    """Unsupported schema_version should emit VERSION_UNSUPPORTED."""
    data = {"schema_version": 999, "timetable": {}}
    report = validate_timetable(data)
    assert not report.is_valid
    assert any(d.rule_id == RuleId.VERSION_UNSUPPORTED for d in report.errors)


def test_validate_invalid_weekday_key():
    """Non-weekday keys should emit WEEKDAY_INVALID."""
    data = {"funday": [{"subject": "Fun", "start": "10:00", "end": "11:00", "room": "101"}]}
    report = validate_timetable(data)
    assert not report.is_valid
    assert any(d.rule_id == RuleId.WEEKDAY_INVALID for d in report.errors)


def test_validate_slots_not_a_list():
    """Slots for a day must be a list."""
    data = {"monday": "not a list"}
    report = validate_timetable(data)
    assert not report.is_valid
    assert any(d.rule_id == RuleId.ENTRY_MALFORMED for d in report.errors)


def test_validate_missing_slot_field():
    """Slot missing required fields emits ENTRY_MALFORMED."""
    data = {"monday": [{"subject": "Incomplete", "start": "09:00", "room": "101"}]}
    report = validate_timetable(data)
    assert not report.is_valid
    assert any(d.rule_id == RuleId.ENTRY_MALFORMED for d in report.errors)


def test_validate_invalid_time_format():
    """Invalid time format emits TIME_INVALID."""
    data = {"monday": [{"subject": "Bad Time", "start": "25:99", "end": "10:00", "room": "101"}]}
    report = validate_timetable(data)
    assert not report.is_valid
    assert any(d.rule_id == RuleId.TIME_INVALID for d in report.errors)


def test_validate_zero_duration_or_inverted_interval():
    """Start time equals or after end time on same day emits INTERVAL_INVALID."""
    data = {"monday": [{"subject": "Zero Dur", "start": "10:00", "end": "10:00", "room": "101"}]}
    report = validate_timetable(data)
    assert not report.is_valid
    assert any(d.rule_id == RuleId.INTERVAL_INVALID for d in report.errors)


def test_validate_duplicate_entries():
    """Identical entries on same day emit ENTRY_DUPLICATE warning."""
    data = {
        "monday": [
            {"subject": "Math", "start": "09:00", "end": "10:00", "room": "101"},
            {"subject": "Math", "start": "09:00", "end": "10:00", "room": "101"},
        ]
    }
    report = validate_timetable(data)
    assert any(d.rule_id == RuleId.ENTRY_DUPLICATE and d.is_warning for d in report.warnings)


# =============================================================================
# 3. Temporal Conflict Detection & Explanation Tests
# =============================================================================

def test_same_day_temporal_conflict_explanation():
    """Two overlapping classes on the same day must identify both classes and explain overlap."""
    data = {
        "monday": [
            {"subject": "Math", "start": "09:00", "end": "10:30", "room": "101"},
            {"subject": "Physics", "start": "10:00", "end": "11:00", "room": "102"},
        ]
    }
    report = validate_timetable(data)
    assert not report.is_valid
    conflict = next(d for d in report.errors if d.rule_id == RuleId.SCHEDULE_CONFLICT)
    assert len(conflict.affected_entries) == 2
    assert conflict.context["day_a"] == "monday"
    assert "overlaps" in conflict.message or "between" in conflict.message
    subjects = {e.slot["subject"] for e in conflict.affected_entries}
    assert "Math" in subjects and "Physics" in subjects


def test_overnight_temporal_conflict_explanation():
    """Class running overnight (e.g. 23:00 to 02:00) colliding with next morning class."""
    data = {
        "monday": [
            {"subject": "Night Shift", "start": "23:00", "end": "02:00", "room": "Lab"},
        ],
        "tuesday": [
            {"subject": "Early Morning", "start": "01:00", "end": "03:00", "room": "Lab"},
        ]
    }
    report = validate_timetable(data)
    assert not report.is_valid
    conflict = next(d for d in report.errors if d.rule_id == RuleId.SCHEDULE_CONFLICT_OVERNIGHT)
    assert len(conflict.affected_entries) == 2
    assert conflict.context["is_overnight"] is True
    assert "Overnight" in conflict.message or "overnight" in conflict.message


def test_sunday_to_monday_wrap_around_conflict():
    """Sunday night class crossing into Monday morning colliding with Monday class."""
    data = {
        "sunday": [
            {"subject": "Sunday Night", "start": "23:00", "end": "01:30", "room": "Lab"},
        ],
        "monday": [
            {"subject": "Monday Morning", "start": "01:00", "end": "02:00", "room": "Lab"},
        ]
    }
    report = validate_timetable(data)
    assert not report.is_valid
    conflict = next(d for d in report.errors if d.rule_id == RuleId.SCHEDULE_CONFLICT_OVERNIGHT)
    assert len(conflict.affected_entries) == 2
    assert "Overnight" in conflict.message or "overnight" in conflict.message


def test_multiple_overlapping_classes():
    """A single class colliding with multiple other classes emits diagnostics for all."""
    data = {
        "wednesday": [
            {"subject": "Long Class", "start": "09:00", "end": "13:00", "room": "Auditorium"},
            {"subject": "Short Class 1", "start": "09:30", "end": "10:30", "room": "101"},
            {"subject": "Short Class 2", "start": "11:00", "end": "12:00", "room": "102"},
        ]
    }
    report = validate_timetable(data)
    assert not report.is_valid
    conflicts = [d for d in report.errors if d.rule_id == RuleId.SCHEDULE_CONFLICT]
    assert len(conflicts) == 2


# =============================================================================
# 4. Provenance & Workflow Integration Tests
# =============================================================================

def test_csv_validation_engine_diagnostics(tmp_path):
    """CSV planning emits diagnostics with file and row provenance."""
    csv_file = tmp_path / "schedule.csv"
    csv_file.write_text(
        "day,subject,start,end,room\n"
        "monday,Math,09:00,10:00,101\n"
        "funday,BadDay,10:00,11:00,102\n"
        "tuesday,ZeroDur,12:00,12:00,103\n",
        encoding="utf-8"
    )
    plan = plan_import(str(csv_file), {"monday": [], "tuesday": []})
    assert not plan.accepted
    assert len(plan.diagnostics) >= 2
    # Check provenance
    bad_day_diag = next(d for d in plan.diagnostics if d.rule_id == RuleId.WEEKDAY_INVALID)
    assert bad_day_diag.source.row == 3
    assert bad_day_diag.source.source_type == "csv"

    zero_dur_diag = next(d for d in plan.diagnostics if d.rule_id == RuleId.INTERVAL_INVALID)
    assert zero_dur_diag.source.row == 4


def test_csv_header_validation():
    """CSV header validation detects missing columns."""
    diag = validate_csv_headers(["day", "subject", "start"])
    assert diag is not None
    assert diag.rule_id == RuleId.IMPORT_HEADER_INVALID
    assert "missing required column(s)" in diag.message


def test_three_way_merge_diagnostics():
    """Three-way merge produces structured diagnostics with merge provenance."""
    base = {
        "monday": [{"subject": "Math", "start": "09:00", "end": "10:00", "room": "101"}]
    }
    ours = {
        "monday": [{"subject": "Math", "start": "09:00", "end": "10:30", "room": "101"}]
    }
    theirs = {
        "monday": [{"subject": "Math", "start": "09:00", "end": "11:00", "room": "101"}]
    }
    result = merge_three_way(base, ours, theirs)
    assert not result.is_clean
    assert len(result.diagnostics) > 0
    diag = result.diagnostics[0]
    assert diag.rule_id == RuleId.MERGE_STRUCTURAL_CONFLICT
    assert len(diag.affected_entries) >= 2
    sides = {e.provenance.merge_side for e in diag.affected_entries}
    assert "ours" in sides and "theirs" in sides


def test_render_diagnostics_output():
    """render_diagnostics outputs clean human-readable text."""
    diag = Diagnostic(
        rule_id=RuleId.SCHEDULE_CONFLICT,
        severity=Severity.ERROR,
        message="Math overlaps with Physics",
        affected_entries=[
            AffectedEntry(day="monday", slot={"subject": "Math", "start": "09:00", "end": "10:30", "room": "101"}),
            AffectedEntry(day="monday", slot={"subject": "Physics", "start": "10:00", "end": "11:00", "room": "102"}),
        ]
    )
    rendered = render_diagnostics([diag])
    assert "Math" in rendered
    assert "Physics" in rendered
    assert "[SCHEDULE_CONFLICT]" in rendered
