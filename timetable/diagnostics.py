"""Structured diagnostic model, provenance, and validation rules for timetables.

Provides a unified validation engine that produces structured, deterministic
diagnostics across timetable validation, mutations, CSV and iCalendar imports,
and two-way and three-way merges.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from timetable.display import WEEKDAYS, parse_time


# =============================================================================
# 1. Severities and Rule Identifiers
# =============================================================================

class Severity(str, enum.Enum):
    """Documented severity levels for validation diagnostics."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    def __str__(self) -> str:
        return self.value


class RuleId(str, enum.Enum):
    """Stable, machine-readable rule identifiers for validation findings."""
    # Structural & schema rules
    SCHEMA_INVALID = "SCHEMA_INVALID"
    VERSION_UNSUPPORTED = "VERSION_UNSUPPORTED"
    WEEKDAY_INVALID = "WEEKDAY_INVALID"
    ENTRY_MALFORMED = "ENTRY_MALFORMED"
    TIME_INVALID = "TIME_INVALID"
    INTERVAL_INVALID = "INTERVAL_INVALID"

    # Duplicate rules
    ENTRY_DUPLICATE = "ENTRY_DUPLICATE"

    # Temporal & scheduling rules
    SCHEDULE_CONFLICT = "SCHEDULE_CONFLICT"
    SCHEDULE_CONFLICT_OVERNIGHT = "SCHEDULE_CONFLICT_OVERNIGHT"

    # Import-specific rules
    IMPORT_HEADER_INVALID = "IMPORT_HEADER_INVALID"
    IMPORT_RECORD_MALFORMED = "IMPORT_RECORD_MALFORMED"
    RECURRENCE_INVALID = "RECURRENCE_INVALID"
    RECURRENCE_UNSUPPORTED = "RECURRENCE_UNSUPPORTED"

    # Merge-specific rules
    MERGE_STRUCTURAL_CONFLICT = "MERGE_STRUCTURAL_CONFLICT"
    MERGE_SCHEDULE_CONFLICT = "MERGE_SCHEDULE_CONFLICT"

    def __str__(self) -> str:
        return self.value


# =============================================================================
# 2. Source Provenance Model
# =============================================================================

@dataclass(frozen=True)
class Provenance:
    """Identifies the origin or context of a timetable entry or diagnostic."""
    source_type: str  # e.g. "file", "csv", "ics", "mutation", "merge_base", "merge_ours", "merge_theirs"
    source_path: Optional[str] = None
    row: Optional[int] = None
    line: Optional[int] = None
    component_id: Optional[str] = None
    merge_side: Optional[str] = None
    entry_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert provenance to a dictionary, omitting None values."""
        res: Dict[str, Any] = {"source_type": self.source_type}
        if self.source_path is not None:
            res["source_path"] = self.source_path
        if self.row is not None:
            res["row"] = self.row
        if self.line is not None:
            res["line"] = self.line
        if self.component_id is not None:
            res["component_id"] = self.component_id
        if self.merge_side is not None:
            res["merge_side"] = self.merge_side
        if self.entry_id is not None:
            res["entry_id"] = self.entry_id
        return res

    def sort_key(self) -> Tuple[Any, ...]:
        return (
            self.source_type or "",
            self.source_path or "",
            self.row if self.row is not None else -1,
            self.line if self.line is not None else -1,
            self.component_id or "",
            self.merge_side or "",
            self.entry_id or "",
        )


# =============================================================================
# 3. Affected Entry Model
# =============================================================================

@dataclass(frozen=True)
class AffectedEntry:
    """Represents a timetable entry implicated by a diagnostic with optional provenance."""
    day: str
    slot: Dict[str, Any]
    provenance: Optional[Provenance] = None

    def to_dict(self) -> Dict[str, Any]:
        res: Dict[str, Any] = {
            "day": self.day,
            "slot": dict(self.slot),
        }
        if self.provenance is not None:
            res["provenance"] = self.provenance.to_dict()
        return res

    def sort_key(self) -> Tuple[Any, ...]:
        day_idx = WEEKDAYS.index(self.day.lower()) if self.day.lower() in WEEKDAYS else len(WEEKDAYS)
        start = str(self.slot.get("start", ""))
        end = str(self.slot.get("end", ""))
        subj = str(self.slot.get("subject", ""))
        room = str(self.slot.get("room", ""))
        prov_key = self.provenance.sort_key() if self.provenance else ()
        return (day_idx, self.day.lower(), start, end, subj, room, prov_key)


# =============================================================================
# 4. Structured Diagnostic Model
# =============================================================================

@dataclass(frozen=True)
class Diagnostic:
    """A structured, machine-readable validation finding."""
    rule_id: str
    severity: str
    message: str
    affected_entries: List[AffectedEntry] = field(default_factory=list)
    source: Optional[Provenance] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert diagnostic to structured JSON-serializable dictionary."""
        r_id = self.rule_id.value if isinstance(self.rule_id, enum.Enum) else str(self.rule_id)
        sev = self.severity.value if isinstance(self.severity, enum.Enum) else str(self.severity)
        res: Dict[str, Any] = {
            "rule_id": r_id,
            "severity": sev,
            "message": self.message,
            "affected_entries": [e.to_dict() for e in self.affected_entries],
        }
        if self.source is not None:
            res["source"] = self.source.to_dict()
        if self.context:
            res["context"] = dict(self.context)
        return res

    def sort_key(self) -> Tuple[Any, ...]:
        """Deterministic sort key for stable diagnostic ordering."""
        # Order by:
        # 1. source provenance (if any)
        # 2. rule_id
        # 3. severity (errors first)
        # 4. primary affected entry weekday / start time
        # 5. message
        source_k = self.source.sort_key() if self.source else ()
        sev_rank = 0 if self.severity == Severity.ERROR.value else (1 if self.severity == Severity.WARNING.value else 2)
        entry_k = self.affected_entries[0].sort_key() if self.affected_entries else ()
        return (source_k, self.rule_id, sev_rank, entry_k, self.message)

    @property
    def is_error(self) -> bool:
        return self.severity == Severity.ERROR.value

    @property
    def is_warning(self) -> bool:
        return self.severity == Severity.WARNING.value

    @property
    def is_info(self) -> bool:
        return self.severity == Severity.INFO.value


# =============================================================================
# 5. Validation Result Container
# =============================================================================

@dataclass
class ValidationReport:
    """Container of diagnostics collected during a validation run."""
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True if there are no diagnostics with severity 'error'."""
        return not any(d.is_error for d in self.diagnostics)

    @property
    def errors(self) -> List[Diagnostic]:
        """All error-level diagnostics."""
        return [d for d in self.diagnostics if d.is_error]

    @property
    def warnings(self) -> List[Diagnostic]:
        """All warning-level diagnostics."""
        return [d for d in self.diagnostics if d.is_warning]

    def add(self, diagnostic: Diagnostic) -> None:
        self.diagnostics.append(diagnostic)

    def extend(self, diagnostics: Sequence[Diagnostic]) -> None:
        self.diagnostics.extend(diagnostics)

    def sorted_diagnostics(self) -> List[Diagnostic]:
        """Return all diagnostics in deterministic order."""
        return sorted(self.diagnostics, key=lambda d: d.sort_key())

    def deduplicated(self) -> ValidationReport:
        """Return a new ValidationReport with duplicates removed deterministically."""
        seen: Set[Tuple[Any, ...]] = set()
        deduped: List[Diagnostic] = []
        for d in self.sorted_diagnostics():
            # Identity key based on rule, severity, message, source, and affected entries
            key = (
                d.rule_id,
                d.severity,
                d.message,
                d.source.sort_key() if d.source else None,
                tuple(e.sort_key() for e in d.affected_entries),
                tuple(sorted((str(k), str(v)) for k, v in d.context.items())),
            )
            if key not in seen:
                seen.add(key)
                deduped.append(d)
        return ValidationReport(diagnostics=deduped)

    def to_dict(self) -> Dict[str, Any]:
        deduped = self.deduplicated()
        return {
            "valid": deduped.is_valid,
            "error_count": len(deduped.errors),
            "warning_count": len(deduped.warnings),
            "diagnostics": [d.to_dict() for d in deduped.sorted_diagnostics()],
        }


# =============================================================================
# 6. Reusable Formatting Helpers
# =============================================================================

def render_diagnostics(report: Union[ValidationReport, List[Diagnostic]]) -> str:
    """Renders human-readable summary of validation diagnostics."""
    if isinstance(report, list):
        r = ValidationReport()
        for d in report:
            r.add(d)
        deduped = r.deduplicated()
    else:
        deduped = report.deduplicated()
    if not deduped.diagnostics:
        return "No validation issues detected."

    lines = []
    if deduped.errors:
        lines.append(f"❌ Validation Errors ({len(deduped.errors)}):")
        for err in deduped.errors:
            prov_str = ""
            if err.source:
                parts = []
                if err.source.source_path:
                    parts.append(err.source.source_path)
                if err.source.row is not None:
                    parts.append(f"row {err.source.row}")
                if err.source.line is not None:
                    parts.append(f"line {err.source.line}")
                if parts:
                    prov_str = f" [{', '.join(parts)}]"
            lines.append(f"  • [{err.rule_id}]{prov_str} {err.message}")
            for entry in err.affected_entries:
                s = entry.slot
                lines.append(
                    f"      - {entry.day.capitalize()}: {s.get('start', '?')}-{s.get('end', '?')} "
                    f"{s.get('subject', 'Untitled')} [{s.get('room', 'No Room')}]"
                )

    if deduped.warnings:
        if lines:
            lines.append("")
        lines.append(f"⚠️ Validation Warnings ({len(deduped.warnings)}):")
        for warn in deduped.warnings:
            lines.append(f"  • [{warn.rule_id}] {warn.message}")

    return "\n".join(lines)

