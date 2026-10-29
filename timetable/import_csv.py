"""CSV import planning, validation, duplicate detection, and conflict checks.

Parsing and validation are kept separate from the CLI so the complete import
can be planned before any file is written. The importer follows the existing
timetable rules: weekday names are case-insensitive, times are 24-hour
``HH:MM``, classes must end after they start, and overlap uses half-open
intervals ``[start, end)``.

CSV headers are normalized with ``strip().lower()``. Duplicate or missing
headers, blank values, invalid records, and scheduling conflicts reject the
whole import. Exact duplicates are removed before conflict detection.
"""

import csv
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List

from timetable.conflicts import weekly_slots_overlap
from timetable.diagnostics import (
    AffectedEntry,
    Diagnostic,
    Provenance,
    RuleId,
    Severity,
    ValidationReport,
)
from timetable.display import WEEKDAYS, parse_time
from timetable.engine import (
    validate_csv_headers,
    validate_csv_row,
)
from timetable.loader import load_timetable
from timetable.schema import SchemaError

REQUIRED_COLUMNS = ("day", "subject", "start", "end", "room")


class ImportError_(Exception):
    """Raised when a CSV file or timetable cannot be imported safely."""


@dataclass(frozen=True)
class ValidationIssue:
    """One invalid CSV record, identified by its one-based file row."""

    row: int
    message: str
    rule_id: Optional[str] = None
    source_path: Optional[str] = None

    def to_diagnostic(self) -> Diagnostic:
        """Converts this issue to a structured Diagnostic."""
        return Diagnostic(
            rule_id=self.rule_id or RuleId.IMPORT_RECORD_MALFORMED.value,
            severity=Severity.ERROR.value,
            message=self.message,
            source=Provenance(
                source_type="csv",
                source_path=self.source_path,
                row=self.row,
            ),
        )


@dataclass(frozen=True)
class DuplicateEntry:
    """A valid CSV record skipped because the class already exists."""

    row: int
    day: str
    slot: dict


@dataclass(frozen=True)
class ConflictPair:
    """Two overlapping classes in the weekly timetable."""

    day: str
    first: dict
    second: dict


@dataclass
class ImportPlan:
    """The complete result of parsing and validating a CSV import."""

    records_processed: int = 0
    new_classes: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    invalid_records: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def accepted(self):
        """True when the import has valid records and no conflicts."""
        return not self.invalid_records and not self.conflicts


def _read_csv(path):
    """Reads a CSV file and returns its rows.

    The header is included as row 1. Raises ImportError_ for a missing file,
    unreadable file, decoding problem, or malformed CSV data.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise ImportError_(f"CSV file not found: '{file_path}'")
    try:
        with open(file_path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            rows = []
            for row in reader:
                if any("\x00" in value for value in row):
                    raise ImportError_(f"Malformed CSV file '{file_path}': unexpected null byte")
                rows.append(row)
            return rows
    except UnicodeDecodeError as exc:
        raise ImportError_(f"Could not read CSV file '{file_path}': {exc}") from exc
    except csv.Error as exc:
        raise ImportError_(f"Malformed CSV file '{file_path}': {exc}") from exc
    except OSError as exc:
        raise ImportError_(f"Could not read CSV file '{file_path}': {exc}") from exc


def _normalize_headers(header_row):
    """Normalizes headers and returns an error message, or None when valid."""
    headers = [str(value).strip().lower() for value in header_row]

    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        return (
            "missing required column(s): "
            + ", ".join(missing)
            + ". Required columns are: "
            + ", ".join(REQUIRED_COLUMNS)
        )

    duplicates = sorted({name for name in headers if headers.count(name) > 1})
    if duplicates:
        return "duplicate column name(s): " + ", ".join(duplicates)

    return None


def _parse_time(value, label):
    """Parses an HH:MM value and returns ``(minutes, error_message)``."""
    try:
        return parse_time(value), None
    except SystemExit:
        raise
    except Exception:
        return None, f"invalid {label} time '{value}'; expected 24-hour HH:MM"


def _validate_record(values, row):
    """Validates one CSV record.

    Returns ``(record, issues)``. ``record`` is None when the record is invalid.
    Independent errors are collected rather than stopping at the first one.
    """
    issues = []
    fields = dict(zip(REQUIRED_COLUMNS, values))

    for column in REQUIRED_COLUMNS:
        value = fields.get(column, "")
        if not str(value).strip():
            issues.append(ValidationIssue(row, f"missing value for required column '{column}'"))

    if issues:
        return None, issues

    day = fields["day"].strip().lower()
    if day not in WEEKDAYS:
        issues.append(
            ValidationIssue(
                row,
                f"'{fields['day'].strip()}' is not a valid day. Valid days are: "
                + ", ".join(WEEKDAYS),
            )
        )

    start_value = fields["start"].strip()
    end_value = fields["end"].strip()
    start_min, start_error = _parse_time(start_value, "start")
    end_min, end_error = _parse_time(end_value, "end")
    if start_error:
        issues.append(ValidationIssue(row, start_error))
    if end_error:
        issues.append(ValidationIssue(row, end_error))
    if start_min is not None and end_min is not None and end_min <= start_min:
        issues.append(
            ValidationIssue(
                row,
                f"class ends before or at its start time ({start_value} to {end_value})",
            )
        )

    if issues:
        return None, issues

    return {
        "day": day,
        "slot": {
            "subject": fields["subject"].strip(),
            "start": start_value,
            "end": end_value,
            "room": fields["room"].strip(),
        },
    }, []


def _canonical(slot):
    """Returns the equality key used for duplicate detection."""
    return (
        str(slot.get("subject", "")),
        str(slot.get("start", "")),
        str(slot.get("end", "")),
        str(slot.get("room", "")),
    )


def _existing_slots(data, day):
    """Returns valid class dictionaries already scheduled on ``day``."""
    if not isinstance(data, dict):
        return []
    slots = data.get(day, [])
    if not isinstance(slots, list):
        return []
    return [slot for slot in slots if isinstance(slot, dict)]


def _validate_timetable(data, source):
    """Validates the structure needed for an import.

    Returns a list of error messages. Only the fields relevant to import are
    checked, so an import cannot be blocked by unrelated timetable data.
    """
    errors = []
    if not isinstance(data, dict):
        return [
            f"invalid timetable in '{source}': expected a JSON object mapping weekdays to lists of classes"
        ]

    target = data
    if "schema_version" in data:
        if "timetable" in data:
            if not isinstance(data["timetable"], dict):
                return [
                    f"invalid timetable in '{source}': expected 'timetable' object in versioned timetable"
                ]
            target = data["timetable"]
        else:
            target = {k: v for k, v in data.items() if k not in ("schema_version", "metadata")}

    for day, slots in target.items():
        if not isinstance(day, str):
            errors.append(f"invalid timetable in '{source}': weekday keys must be strings")
        elif not isinstance(slots, list):
            errors.append(f"invalid timetable in '{source}': '{day}' must be a list of classes")
        else:
            for position, slot in enumerate(slots, start=1):
                if not isinstance(slot, dict):
                    errors.append(
                        f"invalid timetable in '{source}': entry {position} on "
                        f"'{day}' must be a class object"
                    )
    return errors


def load_timetable_for_import(path):
    """Reads and validates a timetable file for CSV import.

    A missing file is treated as an empty timetable, matching
    ``loader.load_timetable``. Invalid JSON and unsupported structures raise
    ImportError_ instead of being silently replaced.
    """
    file_path = Path(path)
    if not file_path.exists():
        return {day: [] for day in WEEKDAYS}

    try:
        return load_timetable(file_path)
    except json.JSONDecodeError as exc:
        raise ImportError_(f"invalid JSON in '{file_path}': {exc}") from exc
    except SchemaError as exc:
        raise ImportError_(str(exc)) from exc
    except OSError as exc:
        raise ImportError_(f"could not read '{file_path}': {exc}") from exc


def plan_import(csv_path, timetable_data):
    """Validates a CSV file against ``timetable_data`` and builds an import plan.

    The timetable is never modified. All CSV records are parsed and validated
    first. Exact duplicates are removed before overlap detection, so a
    duplicate is not also reported as a scheduling conflict.
    """
    rows = _read_csv(csv_path)
    plan = ImportPlan()
    if not rows:
        return plan

    src_str = str(csv_path)
    header_diag = validate_csv_headers(rows[0], source_path=src_str)
    if header_diag:
        plan.invalid_records.append(ValidationIssue(1, header_diag.message, rule_id=header_diag.rule_id, source_path=src_str))
        plan.diagnostics.append(header_diag)
        return plan

    records = rows[1:]
    plan.records_processed = len(records)
    if not records:
        return plan

    valid_records = []
    for offset, values in enumerate(records):
        row = offset + 2
        if not values or not any(str(value).strip() for value in values):
            diag = Diagnostic(
                rule_id=RuleId.IMPORT_RECORD_MALFORMED.value,
                severity=Severity.ERROR.value,
                message="record is empty",
                source=Provenance(source_type="csv", source_path=src_str, row=row),
            )
            plan.invalid_records.append(ValidationIssue(row, "record is empty", rule_id=RuleId.IMPORT_RECORD_MALFORMED.value, source_path=src_str))
            plan.diagnostics.append(diag)
            continue

        record, diags = validate_csv_row(values, row, source_path=src_str)
        if diags:
            for d in diags:
                plan.invalid_records.append(ValidationIssue(row, d.message, rule_id=d.rule_id, source_path=src_str))
                plan.diagnostics.append(d)
            continue
        valid_records.append((row, record["day"], record["slot"]))

    if plan.invalid_records:
        return plan

    seen = set()
    for row, day, slot in valid_records:
        key = (day, _canonical(slot))
        if key in seen:
            plan.duplicates.append(DuplicateEntry(row, day, slot))
            continue
        seen.add(key)
        plan.new_classes.append((row, day, slot))

    existing_keys = set()
    for day in WEEKDAYS:
        for slot in _existing_slots(timetable_data, day):
            existing_keys.add((day, _canonical(slot)))

    unique_new = []
    for row, day, slot in plan.new_classes:
        if (day, _canonical(slot)) in existing_keys:
            plan.duplicates.append(DuplicateEntry(row, day, slot))
        else:
            unique_new.append((row, day, slot))
    plan.new_classes = unique_new

    for index, (first_row, day, first) in enumerate(plan.new_classes):
        entry_a = AffectedEntry(day=day, slot=first, provenance=Provenance(source_type="csv", source_path=src_str, row=first_row))
        for second_row, second_day, second in plan.new_classes[index + 1:]:
            if weekly_slots_overlap(day, first, second_day, second):
                plan.conflicts.append(ConflictPair(day, first, second))
                entry_b = AffectedEntry(day=second_day, slot=second, provenance=Provenance(source_type="csv", source_path=src_str, row=second_row))
                plan.diagnostics.append(Diagnostic(
                    rule_id=RuleId.SCHEDULE_CONFLICT_OVERNIGHT.value if day != second_day else RuleId.SCHEDULE_CONFLICT.value,
                    severity=Severity.ERROR.value,
                    message=f"Schedule conflict on {day.capitalize()} between '{first.get('subject')}' and '{second.get('subject')}'",
                    affected_entries=[entry_a, entry_b],
                    source=Provenance(source_type="csv", source_path=src_str, row=first_row),
                ))

        for existing_day in WEEKDAYS:
            for existing in _existing_slots(timetable_data, existing_day):
                if weekly_slots_overlap(day, first, existing_day, existing):
                    plan.conflicts.append(ConflictPair(day, first, existing))
                    entry_exist = AffectedEntry(day=existing_day, slot=existing, provenance=Provenance(source_type="timetable"))
                    plan.diagnostics.append(Diagnostic(
                        rule_id=RuleId.SCHEDULE_CONFLICT_OVERNIGHT.value if day != existing_day else RuleId.SCHEDULE_CONFLICT.value,
                        severity=Severity.ERROR.value,
                        message=f"Schedule conflict on {day.capitalize()} between import class '{first.get('subject')}' and existing class '{existing.get('subject')}'",
                        affected_entries=[entry_a, entry_exist],
                        source=Provenance(source_type="csv", source_path=src_str, row=first_row),
                    ))

    return plan



def _time_minutes(value):
    """Converts a valid HH:MM string to minutes; invalid values sort last."""
    try:
        parsed = parse_time(value)
    except Exception:
        return 24 * 60
    return parsed.hour * 60 + parsed.minute


def apply_import(data, plan):
    """Returns a new timetable containing the plan's accepted classes.

    Existing classes are preserved and affected days are sorted by start time
    using the same ordering convention as ``merge_timetables``.
    """
    updated = dict(data)
    for day in WEEKDAYS:
        entries = [slot for _, entry_day, slot in plan.new_classes if entry_day == day]
        if not entries:
            continue

        current = list(updated.get(day, [])) if isinstance(updated.get(day, []), list) else []
        current.extend(entries)
        current.sort(
            key=lambda slot: (
                _time_minutes(slot.get("start", "")),
                str(slot.get("start", "")),
                str(slot.get("subject", "")),
            )
        )
        updated[day] = current
    return updated


def save_timetable_atomic(data, filepath):
    """Writes a timetable through a temporary file and atomic replacement.

    The temporary file lives beside the target so replacement stays on the
    same filesystem. Failures remove the temporary file and leave the original
    timetable untouched.
    """
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def render_plan(plan, dry_run=False):
    """Returns a deterministic human-readable summary of an import plan."""
    lines = []
    title = "Dry run summary" if dry_run else "Import summary"
    lines.append(title)
    lines.append(f"CSV records processed: {plan.records_processed}")
    lines.append(f"New classes to import: {len(plan.new_classes)}")
    lines.append(f"Duplicate entries skipped: {len(plan.duplicates)}")
    lines.append(f"Invalid records rejected: {len(plan.invalid_records)}")
    lines.append(f"Scheduling conflicts detected: {len(plan.conflicts)}")

    if plan.invalid_records:
        lines.append("")
        lines.append("Validation errors:")
        for issue in plan.invalid_records:
            lines.append(f"  Row {issue.row}: {issue.message}")

    if plan.duplicates:
        lines.append("")
        lines.append("Skipped duplicates:")
        for duplicate in plan.duplicates:
            slot = duplicate.slot
            lines.append(
                f"  Row {duplicate.row}: {duplicate.day.capitalize()} "
                f"{slot['start']}-{slot['end']} - {slot['subject']} - {slot['room']}"
            )

    if plan.conflicts:
        lines.append("")
        lines.append("Scheduling conflicts:")
        for conflict in plan.conflicts:
            first = conflict.first
            second = conflict.second
            lines.append(f"Conflict on {conflict.day.capitalize()}:")
            lines.append(
                f"  - {first.get('start')} - {first.get('end')} : "
                f"{first.get('subject')} [{first.get('room', 'No Room')}]"
            )
            lines.append(
                f"  - {second.get('start')} - {second.get('end')} : "
                f"{second.get('subject')} [{second.get('room', 'No Room')}]"
            )

    return "\n".join(lines)

