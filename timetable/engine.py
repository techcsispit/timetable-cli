"""Centralized validation engine and rules for timetable documents.

Integrates structural, schema, duplicate, temporal conflict, import, and merge
rules to produce structured, deterministic ValidationReports.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from timetable.conflicts import (
    MINUTES_PER_DAY,
    _to_minutes,
    _weekly_interval,
    _weekly_overlap,
    weekly_slots_overlap,
)
from timetable.diagnostics import (
    AffectedEntry,
    Diagnostic,
    Provenance,
    RuleId,
    Severity,
    ValidationReport,
)
from timetable.display import WEEKDAYS, parse_time
from timetable.schema import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    _ALLOWED_SLOT_FIELDS,
    _REQUIRED_SLOT_FIELDS,
    _TIME_RE,
    ValidationError,
)

CSV_REQUIRED_COLUMNS = ("day", "subject", "start", "end", "room")


# =============================================================================
# Helper: Canonicalize slot
# =============================================================================

def _canonical_slot_dict(slot: Any) -> Dict[str, str]:
    if not isinstance(slot, dict):
        return {}
    return {
        "subject": str(slot.get("subject", "")),
        "start": str(slot.get("start", "")),
        "end": str(slot.get("end", "")),
        "room": str(slot.get("room", "")),
    }


def _slot_identity_key(slot: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(slot.get("subject", "")),
        str(slot.get("start", "")),
        str(slot.get("end", "")),
        str(slot.get("room", "")),
    )


# =============================================================================
# 1. Structural and Schema Validation Rules
# =============================================================================

def validate_time_format(time_val: Any) -> Optional[str]:
    """Validates 24-hour HH:MM time string, returning error message or None."""
    if not isinstance(time_val, str):
        return f"Expected string time 'HH:MM', received {type(time_val).__name__}"
    if not _TIME_RE.match(time_val):
        return f"Expected valid 24-hour HH:MM time, received '{time_val}'"
    try:
        parse_time(time_val)
    except Exception as exc:
        return f"Invalid clock time '{time_val}': {exc}"
    return None


def validate_timetable_structure(
    data: Any,
    source: Optional[Provenance] = None,
    report: Optional[ValidationReport] = None,
) -> ValidationReport:
    """Validates structural and schema integrity of a timetable document.

    Checks:
    - Root object type (dict).
    - schema_version validity and supported versions.
    - Weekday mappings and recognized weekday names.
    - Class slot structure (required properties, allowed properties, time formats).
    - Interval non-zero duration (start != end).
    """
    if report is None:
        report = ValidationReport()

    if not isinstance(data, dict):
        report.add(
            Diagnostic(
                rule_id=RuleId.SCHEMA_INVALID.value,
                severity=Severity.ERROR.value,
                message=f"Expected top-level JSON object mapping weekdays, received {type(data).__name__}",
                source=source,
                context={"actual_type": type(data).__name__},
            )
        )
        return report

    # Check schema_version if present
    is_canonical = "schema_version" in data
    if is_canonical:
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            report.add(
                Diagnostic(
                    rule_id=RuleId.SCHEMA_INVALID.value,
                    severity=Severity.ERROR.value,
                    message=f"Expected integer schema_version, received {type(version).__name__}",
                    source=source,
                )
            )
        elif version not in SUPPORTED_SCHEMA_VERSIONS:
            report.add(
                Diagnostic(
                    rule_id=RuleId.VERSION_UNSUPPORTED.value,
                    severity=Severity.ERROR.value,
                    message=f"Unsupported schema version '{version}'. Supported versions are: {', '.join(map(str, SUPPORTED_SCHEMA_VERSIONS))}",
                    source=source,
                    context={"unsupported_version": version},
                )
            )

    # Determine timetable mapping
    target = data
    if is_canonical and "timetable" in data:
        if not isinstance(data["timetable"], dict):
            report.add(
                Diagnostic(
                    rule_id=RuleId.SCHEMA_INVALID.value,
                    severity=Severity.ERROR.value,
                    message=f"Expected 'timetable' object mapping weekdays, received {type(data['timetable']).__name__}",
                    source=source,
                )
            )
            return report
        target = data["timetable"]
    elif "metadata" in data and not is_canonical:
        target = {k: v for k, v in data.items() if k != "metadata"}

    # Validate day keys and class lists
    for day_key, slots in target.items():
        if day_key in ("schema_version", "metadata"):
            continue

        if not isinstance(day_key, str):
            report.add(
                Diagnostic(
                    rule_id=RuleId.WEEKDAY_INVALID.value,
                    severity=Severity.ERROR.value,
                    message=f"Expected string weekday name, received {type(day_key).__name__}",
                    source=source,
                )
            )
            continue

        norm_day = day_key.strip().lower()
        if norm_day not in WEEKDAYS:
            report.add(
                Diagnostic(
                    rule_id=RuleId.WEEKDAY_INVALID.value,
                    severity=Severity.ERROR.value,
                    message=f"'{day_key}' is not a recognized weekday. Valid days are: {', '.join(WEEKDAYS)}",
                    source=source,
                    context={"invalid_day": day_key},
                )
            )
            continue

        if not isinstance(slots, list):
            report.add(
                Diagnostic(
                    rule_id=RuleId.ENTRY_MALFORMED.value,
                    severity=Severity.ERROR.value,
                    message=f"Expected list of classes for '{day_key}', received {type(slots).__name__}",
                    source=source,
                    context={"day": norm_day},
                )
            )
            continue

        for idx, slot in enumerate(slots):
            if not isinstance(slot, dict):
                report.add(
                    Diagnostic(
                        rule_id=RuleId.ENTRY_MALFORMED.value,
                        severity=Severity.ERROR.value,
                        message=f"Class entry {idx + 1} on '{day_key}' must be an object, received {type(slot).__name__}",
                        source=source,
                        context={"day": norm_day, "index": idx},
                    )
                )
                continue

            affected = AffectedEntry(day=norm_day, slot=slot, provenance=source)

            # Validate against _REQUIRED_SLOT_FIELDS
            for req_f in _REQUIRED_SLOT_FIELDS:
                if req_f not in slot:
                    report.add(
                        Diagnostic(
                            rule_id=RuleId.ENTRY_MALFORMED.value,
                            severity=Severity.ERROR.value,
                            message=f"Class on '{day_key}' missing required property '{req_f}'",
                            affected_entries=[affected],
                            source=source,
                            context={"missing_field": req_f},
                        )
                    )
                elif not isinstance(slot[req_f], str):
                    report.add(
                        Diagnostic(
                            rule_id=RuleId.ENTRY_MALFORMED.value,
                            severity=Severity.ERROR.value,
                            message=f"Property '{req_f}' on '{day_key}' must be a string, received {type(slot[req_f]).__name__}",
                            affected_entries=[affected],
                            source=source,
                        )
                    )

            if is_canonical:
                for k in slot.keys():
                    if k not in _ALLOWED_SLOT_FIELDS:
                        report.add(
                            Diagnostic(
                                rule_id=RuleId.SCHEMA_INVALID.value,
                                severity=Severity.ERROR.value,
                                message=f"Unexpected property '{k}' in class slot on '{day_key}'",
                                affected_entries=[affected],
                                source=source,
                            )
                        )

            # Validate time formats and intervals
            s_val = slot.get("start")
            e_val = slot.get("end")

            s_err = validate_time_format(s_val) if s_val is not None else None
            e_err = validate_time_format(e_val) if e_val is not None else None

            if s_err:
                report.add(
                    Diagnostic(
                        rule_id=RuleId.TIME_INVALID.value,
                        severity=Severity.ERROR.value,
                        message=f"Invalid start time on '{day_key}': {s_err}",
                        affected_entries=[affected],
                        source=source,
                    )
                )
            if e_err:
                report.add(
                    Diagnostic(
                        rule_id=RuleId.TIME_INVALID.value,
                        severity=Severity.ERROR.value,
                        message=f"Invalid end time on '{day_key}': {e_err}",
                        affected_entries=[affected],
                        source=source,
                    )
                )

            if s_val is not None and e_val is not None and not s_err and not e_err:
                if s_val == e_val:
                    report.add(
                        Diagnostic(
                            rule_id=RuleId.INTERVAL_INVALID.value,
                            severity=Severity.ERROR.value,
                            message=f"End time '{e_val}' must not be equal to start time '{s_val}' (zero duration)",
                            affected_entries=[affected],
                            source=source,
                        )
                    )

    return report


# =============================================================================
# 2. Duplicate Detection Rules
# =============================================================================

def validate_duplicates(
    data: Dict[str, Any],
    source: Optional[Provenance] = None,
    report: Optional[ValidationReport] = None,
) -> ValidationReport:
    """Detects duplicate entries within a timetable dictionary."""
    if report is None:
        report = ValidationReport()

    if not isinstance(data, dict):
        return report

    target = data
    if "timetable" in data and isinstance(data["timetable"], dict):
        target = data["timetable"]

    for day_key, slots in target.items():
        if not isinstance(day_key, str) or not isinstance(slots, list):
            continue
        norm_day = day_key.strip().lower()
        if norm_day not in WEEKDAYS:
            continue

        seen_slots: Set[Tuple[str, str, str, str]] = set()
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            key = _slot_identity_key(slot)
            affected = AffectedEntry(day=norm_day, slot=slot, provenance=source)
            if key in seen_slots:
                report.add(
                    Diagnostic(
                        rule_id=RuleId.ENTRY_DUPLICATE.value,
                        severity=Severity.WARNING.value,
                        message=(
                            f"Duplicate class '{slot.get('subject', 'Untitled')}' "
                            f"({slot.get('start', '?')}-{slot.get('end', '?')}) on {norm_day.capitalize()}"
                        ),
                        affected_entries=[affected],
                        source=source,
                        context={"day": norm_day, "slot": dict(slot)},
                    )
                )
            else:
                seen_slots.add(key)

    return report


# =============================================================================
# 3. Temporal Conflict Detection Rules
# =============================================================================

def validate_temporal_conflicts(
    data: Dict[str, Any],
    source: Optional[Provenance] = None,
    report: Optional[ValidationReport] = None,
) -> ValidationReport:
    """Detects and explains all schedule conflicts in a timetable across all weekdays.

    Features:
    - Same-day half-open interval overlaps.
    - Overnight classes extending into following weekday.
    - Sunday-to-Monday circular week wrap.
    - Retains both affected entries with complete weekday and time interval information.
    - Deterministic ordering by chronological start time.
    """
    if report is None:
        report = ValidationReport()

    if not isinstance(data, dict):
        return report

    target = data
    if "timetable" in data and isinstance(data["timetable"], dict):
        target = data["timetable"]

    # Gather all valid slots with their weekly interval
    events = []
    for day_index, day in enumerate(WEEKDAYS):
        slots = target.get(day, [])
        if not isinstance(slots, list):
            continue
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            try:
                interval = _weekly_interval(day_index, slot)
            except Exception:
                continue
            events.append((interval[0], day_index, day, slot, interval))

    events.sort(key=lambda ev: (ev[0], ev[1]))

    for idx, ev_a in enumerate(events):
        _, day_idx_a, day_a, slot_a, interval_a = ev_a
        for ev_b in events[idx + 1:]:
            _, day_idx_b, day_b, slot_b, interval_b = ev_b

            if not _weekly_overlap(interval_a, interval_b):
                continue

            entry_a = AffectedEntry(day=day_a, slot=slot_a, provenance=source)
            entry_b = AffectedEntry(day=day_b, slot=slot_b, provenance=source)

            # Determine conflict characteristics: overnight collision vs same day
            is_overnight = False
            collision_day = day_a
            if day_a != day_b:
                is_overnight = True
                if interval_a[1] > (day_idx_a + 1) * MINUTES_PER_DAY:
                    collision_day = day_a
                elif interval_b[1] > (day_idx_b + 1) * MINUTES_PER_DAY:
                    collision_day = day_b

            rule = (
                RuleId.SCHEDULE_CONFLICT_OVERNIGHT.value
                if is_overnight
                else RuleId.SCHEDULE_CONFLICT.value
            )

            # Explanatory message
            if is_overnight:
                msg = (
                    f"Overnight schedule conflict between {day_a.capitalize()} "
                    f"'{slot_a.get('subject', 'Untitled')}' ({slot_a.get('start')}-{slot_a.get('end')}) "
                    f"and {day_b.capitalize()} '{slot_b.get('subject', 'Untitled')}' ({slot_b.get('start')}-{slot_b.get('end')})"
                )
            else:
                msg = (
                    f"Schedule conflict on {day_a.capitalize()} between "
                    f"'{slot_a.get('subject', 'Untitled')}' ({slot_a.get('start')}-{slot_a.get('end')}) "
                    f"and '{slot_b.get('subject', 'Untitled')}' ({slot_b.get('start')}-{slot_b.get('end')})"
                )

            report.add(
                Diagnostic(
                    rule_id=rule,
                    severity=Severity.ERROR.value,
                    message=msg,
                    affected_entries=[entry_a, entry_b],
                    source=source,
                    context={
                        "collision_day": collision_day,
                        "day_a": day_a,
                        "day_b": day_b,
                        "interval_a": interval_a,
                        "interval_b": interval_b,
                        "is_overnight": is_overnight,
                    },
                )
            )

    return report


# =============================================================================
# 4. CSV Import Record Validation Rules
# =============================================================================

def validate_csv_headers(headers: Sequence[Any], source_path: Optional[str] = None) -> Optional[Diagnostic]:
    """Validates CSV headers and returns a structured Diagnostic if invalid."""
    norm_headers = [str(val).strip().lower() for val in headers]
    source = Provenance(source_type="csv", source_path=source_path, row=1)

    missing = [col for col in CSV_REQUIRED_COLUMNS if col not in norm_headers]
    if missing:
        return Diagnostic(
            rule_id=RuleId.IMPORT_HEADER_INVALID.value,
            severity=Severity.ERROR.value,
            message=(
                f"missing required column(s): {', '.join(missing)}. "
                f"Required columns are: {', '.join(CSV_REQUIRED_COLUMNS)}"
            ),
            source=source,
            context={"missing_columns": missing},
        )

    duplicates = sorted({name for name in norm_headers if norm_headers.count(name) > 1})
    if duplicates:
        return Diagnostic(
            rule_id=RuleId.IMPORT_HEADER_INVALID.value,
            severity=Severity.ERROR.value,
            message=f"duplicate column name(s): {', '.join(duplicates)}",
            source=source,
            context={"duplicate_columns": duplicates},
        )

    return None


def validate_csv_row(
    values: Sequence[Any],
    row_num: int,
    source_path: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], List[Diagnostic]]:
    """Validates one CSV record row and returns (parsed_record, diagnostics_list)."""
    diagnostics: List[Diagnostic] = []
    source = Provenance(source_type="csv", source_path=source_path, row=row_num)

    fields = dict(zip(CSV_REQUIRED_COLUMNS, values))

    for col in CSV_REQUIRED_COLUMNS:
        val = fields.get(col, "")
        if not str(val).strip():
            diagnostics.append(
                Diagnostic(
                    rule_id=RuleId.IMPORT_RECORD_MALFORMED.value,
                    severity=Severity.ERROR.value,
                    message=f"missing value for required column '{col}'",
                    source=source,
                    context={"column": col},
                )
            )

    if diagnostics:
        return None, diagnostics

    raw_day = str(fields["day"]).strip()
    norm_day = raw_day.lower()
    if norm_day not in WEEKDAYS:
        diagnostics.append(
            Diagnostic(
                rule_id=RuleId.WEEKDAY_INVALID.value,
                severity=Severity.ERROR.value,
                message=f"'{raw_day}' is not a valid day. Valid days are: {', '.join(WEEKDAYS)}",
                source=source,
                context={"invalid_day": raw_day},
            )
        )

    start_val = str(fields["start"]).strip()
    end_val = str(fields["end"]).strip()

    start_err = validate_time_format(start_val)
    end_err = validate_time_format(end_val)

    if start_err:
        diagnostics.append(
            Diagnostic(
                rule_id=RuleId.TIME_INVALID.value,
                severity=Severity.ERROR.value,
                message=f"invalid start time '{start_val}'; expected 24-hour HH:MM",
                source=source,
                context={"field": "start", "value": start_val},
            )
        )
    if end_err:
        diagnostics.append(
            Diagnostic(
                rule_id=RuleId.TIME_INVALID.value,
                severity=Severity.ERROR.value,
                message=f"invalid end time '{end_val}'; expected 24-hour HH:MM",
                source=source,
                context={"field": "end", "value": end_val},
            )
        )

    if not start_err and not end_err:
        try:
            s_min = _to_minutes(start_val)
            e_min = _to_minutes(end_val)
            if e_min <= s_min:
                diagnostics.append(
                    Diagnostic(
                        rule_id=RuleId.INTERVAL_INVALID.value,
                        severity=Severity.ERROR.value,
                        message=f"class ends before or at its start time ({start_val} to {end_val})",
                        source=source,
                        context={"start": start_val, "end": end_val},
                    )
                )
        except Exception:
            pass

    if diagnostics:
        return None, diagnostics

    slot = {
        "subject": str(fields["subject"]).strip(),
        "start": start_val,
        "end": end_val,
        "room": str(fields["room"]).strip(),
    }
    return {"day": norm_day, "slot": slot}, diagnostics


# =============================================================================
# 5. Comprehensive Timetable Validation
# =============================================================================

def validate_timetable(
    data: Any,
    source: Optional[Provenance] = None,
    source_path: Optional[str] = None,
    check_conflicts: bool = True,
    check_dups: bool = True,
) -> ValidationReport:
    """Runs the complete suite of validation rules on a timetable document."""
    if source is None and source_path is not None:
        source = Provenance(source_type="file", source_path=source_path)
    report = ValidationReport()
    validate_timetable_structure(data, source=source, report=report)
    if report.is_valid and isinstance(data, dict):
        if check_dups:
            validate_duplicates(data, source=source, report=report)
        if check_conflicts:
            validate_temporal_conflicts(data, source=source, report=report)
    return report.deduplicated()
