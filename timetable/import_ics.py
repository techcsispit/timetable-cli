"""iCalendar (.ics) import planning, parsing, recurrence expansion, and validation.

This module parses iCalendar (.ics) streams and files conforming to RFC 5545,
expands recurring events (RRULE, RDATE, EXDATE) across a finite expansion window,
resolves recurrence exceptions and detached VEVENT overrides, and maps supported
events to the weekly timetable schema.
"""

from __future__ import annotations

import datetime
import os
import re
import tempfile
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import dateutil.rrule

from timetable.conflicts import do_overlap
from timetable.display import WEEKDAYS, parse_time
from timetable.diagnostics import (
    AffectedEntry,
    Diagnostic,
    Provenance,
    RuleId,
    Severity,
    ValidationReport,
)
from timetable.import_csv import (
    ConflictPair,
    DuplicateEntry,
    ImportError_,
    ValidationIssue,
    _canonical,
    _existing_slots,
    _time_minutes,
    load_timetable_for_import,
)


@dataclass(frozen=True)
class RawVEvent:
    """Raw parsed lines and properties of a single VEVENT component."""
    properties: List[Tuple[str, Dict[str, str], str]]
    source_line: int


@dataclass(frozen=True)
class EventOccurrence:
    """An expanded single occurrence of an iCalendar event."""
    uid: str
    summary: str
    room: str
    occurrence_date: datetime.date
    start_dt: datetime.datetime
    end_dt: datetime.datetime
    day: str
    slot: Dict[str, str]
    source_line: int


@dataclass
class IcsImportPlan:
    """The complete result of parsing, recurrence expansion, and validating an ICS import."""
    events_found: int = 0
    occurrences_generated: int = 0
    new_classes: List[Tuple[int, str, Dict[str, str]]] = field(default_factory=list)
    duplicates: List[DuplicateEntry] = field(default_factory=list)
    invalid_records: List[ValidationIssue] = field(default_factory=list)
    conflicts: List[ConflictPair] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        """True when the import has valid records and no conflicts."""
        return not self.invalid_records and not self.conflicts


def unfold_ics_lines(content: str) -> List[Tuple[int, str]]:
    """Unfolds RFC 5545 lines (lines starting with space or tab continue previous line).

    Returns a list of (1-based line number, unfolded text).
    """
    raw_lines = content.splitlines()
    unfolded: List[Tuple[int, str]] = []
    current_line_num = 0
    current_line = ""

    for idx, raw in enumerate(raw_lines, start=1):
        if not raw:
            continue
        if raw[0] in (" ", "\t"):
            if not unfolded and not current_line:
                # Continuation at the very start of file is malformed
                continue
            current_line += raw[1:]
        else:
            if current_line:
                unfolded.append((current_line_num, current_line))
            current_line_num = idx
            current_line = raw

    if current_line:
        unfolded.append((current_line_num, current_line))

    return unfolded


def unescape_ics_text(val: str) -> str:
    """Unescapes RFC 5545 text sequences: \\n, \\N, \\;, \\,, \\\\."""
    res = []
    i = 0
    n = len(val)
    while i < n:
        if val[i] == "\\" and i + 1 < n:
            next_ch = val[i + 1]
            if next_ch in ("n", "N"):
                res.append("\n")
                i += 2
            elif next_ch in (";", ",", "\\"):
                res.append(next_ch)
                i += 2
            else:
                res.append(next_ch)
                i += 2
        else:
            res.append(val[i])
            i += 1
    return "".join(res)


def parse_ics_property(line: str) -> Tuple[str, Dict[str, str], str]:
    """Parses a single content line into (name, params_dict, value)."""
    # Name and params are separated from value by the first unquoted colon
    colon_idx = -1
    in_quotes = False
    for idx, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ':' and not in_quotes:
            colon_idx = idx
            break

    if colon_idx == -1:
        raise ValueError(f"Missing colon in property line: '{line}'")

    prop_part = line[:colon_idx]
    value = line[colon_idx + 1:]

    # Parse name and parameters
    # Parameters are separated by semicolons; parameter values can be quoted
    tokens = []
    curr: List[str] = []
    in_quotes = False
    for ch in prop_part:
        if ch == '"':
            in_quotes = not in_quotes
            curr.append(ch)
        elif ch == ';' and not in_quotes:
            tokens.append("".join(curr))
            curr = []
        else:
            curr.append(ch)
    if curr:
        tokens.append("".join(curr))

    if not tokens or not tokens[0].strip():
        raise ValueError(f"Empty property name in: '{line}'")

    name = tokens[0].strip().upper()
    params: Dict[str, str] = {}
    for param_token in tokens[1:]:
        if not param_token.strip():
            continue
        if "=" in param_token:
            p_name, p_val = param_token.split("=", 1)
            p_name = p_name.strip().upper()
            p_val = p_val.strip()
            if p_val.startswith('"') and p_val.endswith('"') and len(p_val) >= 2:
                p_val = p_val[1:-1]
            params[p_name] = p_val
        else:
            params[param_token.strip().upper()] = ""

    return name, params, value


def parse_ics_content(content: str) -> Tuple[List[RawVEvent], List[ValidationIssue]]:
    """Extracts VEVENT components from unfolded ICS lines."""
    unfolded = unfold_ics_lines(content)
    events: List[RawVEvent] = []
    issues: List[ValidationIssue] = []

    had_vcalendar = False
    in_vcalendar = False
    in_vevent = False
    event_start_line = 0
    current_properties: List[Tuple[str, Dict[str, str], str]] = []

    for line_num, line in unfolded:
        try:
            name, params, value = parse_ics_property(line)
        except Exception as exc:
            issues.append(ValidationIssue(line_num, f"malformed iCalendar property: {exc}"))
            continue

        if name == "BEGIN":
            comp = value.strip().upper()
            if comp == "VCALENDAR":
                had_vcalendar = True
                in_vcalendar = True
            elif comp == "VEVENT":
                if in_vevent:
                    issues.append(ValidationIssue(line_num, "nested VEVENT is not supported"))
                in_vevent = True
                event_start_line = line_num
                current_properties = []
        elif name == "END":
            comp = value.strip().upper()
            if comp == "VEVENT":
                if in_vevent:
                    events.append(RawVEvent(current_properties, event_start_line))
                    in_vevent = False
                    current_properties = []
                else:
                    issues.append(ValidationIssue(line_num, "unmatched END:VEVENT"))
            elif comp == "VCALENDAR":
                in_vcalendar = False
        else:
            if in_vevent:
                current_properties.append((name, params, value))

    if not had_vcalendar and not events and not issues:
        issues.append(ValidationIssue(1, "missing VCALENDAR component"))

    return events, issues


def parse_ics_datetime(
    val: str,
    params: Dict[str, str],
    target_tz: Optional[datetime.tzinfo] = None,
    convert_to_target: bool = True,
) -> Tuple[Optional[datetime.datetime], bool, Optional[str]]:
    """Parses a date or datetime string from an ICS property.

    Returns (dt, is_all_day, error_message).
    - If VALUE=DATE or 8 digits (YYYYMMDD), treated as all-day.
    - If ends with 'Z', treated as UTC.
    - If TZID param is specified, parsed with ZoneInfo(tzid) if available.
    - If floating (no Z, no TZID), interpreted in target_tz if provided, else floating/naive.
    - Converted to target_tz when requested and the parsed value is timezone-aware.

    Recurrence expansion parses DTSTART with ``convert_to_target=False`` so
    weekly calendar arithmetic remains anchored to its source timezone. Each
    generated occurrence is converted only after the recurrence is expanded.
    """
    clean_val = val.strip()
    val_type = params.get("VALUE", "").upper()

    # All-day / DATE format: YYYYMMDD
    if val_type == "DATE" or (len(clean_val) == 8 and clean_val.isdigit()):
        try:
            d = datetime.date(int(clean_val[:4]), int(clean_val[4:6]), int(clean_val[6:8]))
            # Represent start of day as datetime for convenience
            return datetime.datetime(d.year, d.month, d.day), True, None
        except ValueError as exc:
            return None, True, f"invalid DATE value '{val}': {exc}"

    # DateTime formats:
    # 1) YYYYMMDDTHHMMSSZ (UTC)
    # 2) YYYYMMDDTHHMMSS (local / floating or TZID)
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z)?$", clean_val)
    if not m:
        return None, False, f"invalid DATETIME format '{val}'; expected YYYYMMDDTHHMMSS[Z]"

    year, month, day, hour, minute, second, is_utc = m.groups()
    try:
        y, mo, d, h, mi, s = map(int, (year, month, day, hour, minute, second))
    except ValueError as exc:
        return None, False, f"invalid DATETIME value '{val}': {exc}"

    tzid = params.get("TZID")
    if is_utc:
        tz = datetime.timezone.utc
    elif tzid:
        try:
            tz = zoneinfo.ZoneInfo(tzid)
        except Exception:
            # Fall back to target_tz if possible or report error
            if target_tz is not None:
                tz = target_tz
            else:
                return None, False, f"unknown or unsupported TZID '{tzid}' in '{val}'"
    else:
        # Floating
        tz = target_tz

    try:
        if tz is not None:
            # If target_tz was supplied for floating or parsed from Z/TZID
            dt = datetime.datetime(y, mo, d, h, mi, s, tzinfo=tz)
            if convert_to_target and target_tz is not None and tz != target_tz:
                dt = dt.astimezone(target_tz)
        else:
            dt = datetime.datetime(y, mo, d, h, mi, s)
        return dt, False, None
    except Exception as exc:
        return None, False, f"invalid date/time values in '{val}': {exc}"


def parse_iso_duration(duration_raw: str) -> Optional[datetime.timedelta]:
    """Parses an RFC 5545 / ISO 8601 duration string like PT1H, PT50M, P1D."""
    dur_match = re.match(
        r"^[+]?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
        duration_raw.strip(),
    )
    if not dur_match:
        return None
    d_days, d_hours, d_mins, d_secs = dur_match.groups()
    if not any((d_days, d_hours, d_mins, d_secs)):
        return None
    return datetime.timedelta(
        days=int(d_days or 0),
        hours=int(d_hours or 0),
        minutes=int(d_mins or 0),
        seconds=int(d_secs or 0),
    )


def normalize_recurrence_key(
    dt: datetime.datetime,
    ref_tz: Optional[datetime.tzinfo] = None,
) -> Tuple[str, Any]:
    """Returns a canonical hashable key for comparing recurrence instances across timezones.

    - For timezone-aware datetimes: converted to UTC timestamp.
    - If dt is naive but ref_tz is provided: assumed to be in ref_tz and converted to UTC.
    - If dt is naive and no ref_tz: ('naive', dt).
    """
    if dt.tzinfo is not None:
        return ("aware", dt.astimezone(datetime.timezone.utc))
    if ref_tz is not None:
        return ("aware", dt.replace(tzinfo=ref_tz).astimezone(datetime.timezone.utc))
    return ("naive", dt)


def _check_duration_bounds(
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    summary_or_uid: str,
    line: int,
) -> Optional[ValidationIssue]:
    """Validates that [start_dt, end_dt) has positive duration under 24 hours."""
    duration_secs = (end_dt - start_dt).total_seconds()
    if duration_secs <= 0:
        return ValidationIssue(
            line,
            f"event '{summary_or_uid}' has zero or negative duration ({start_dt} to {end_dt})",
        )
    if duration_secs >= 24 * 3600:
        return ValidationIssue(
            line,
            f"event '{summary_or_uid}' duration exceeds 24 hours ({duration_secs / 3600:.1f}h), "
            "which cannot be represented in a daily slot",
        )
    return None


def _build_occurrence(
    uid: str,
    summary: str,
    location: str,
    occ_start: datetime.datetime,
    occ_end: datetime.datetime,
    target_tz: Optional[datetime.tzinfo],
    source_line: int,
) -> EventOccurrence:
    """Constructs an EventOccurrence in target_tz with timetable day and slot."""
    display_start = occ_start
    display_end = occ_end
    if target_tz is not None and occ_start.tzinfo is not None:
        display_start = occ_start.astimezone(target_tz)
        display_end = occ_end.astimezone(target_tz)

    occ_date = display_start.date()
    weekday_idx = occ_date.weekday()
    day_name = WEEKDAYS[weekday_idx]

    start_str = display_start.strftime("%H:%M")
    end_str = display_end.strftime("%H:%M")

    slot = {
        "subject": summary if summary else "Untitled Event",
        "start": start_str,
        "end": end_str,
        "room": location,
    }

    return EventOccurrence(
        uid=uid,
        summary=summary,
        room=location,
        occurrence_date=occ_date,
        start_dt=display_start,
        end_dt=display_end,
        day=day_name,
        slot=slot,
        source_line=source_line,
    )


@dataclass
class ParsedVEvent:
    """Intermediary parsed representation of a single VEVENT component."""
    source_line: int
    uid: str
    summary: str
    location: str
    status: str
    dtstart_raw: Optional[Tuple[str, Dict[str, str]]]
    dtend_raw: Optional[Tuple[str, Dict[str, str]]]
    duration_raw: Optional[str]
    recurrence_id_raw: Optional[Tuple[str, Dict[str, str]]]
    rrules: List[str]
    rdates_raw: List[Tuple[str, Dict[str, str]]]
    exdates_raw: List[Tuple[str, Dict[str, str]]]


def parse_vevent_properties(event: RawVEvent) -> ParsedVEvent:
    """Extracts known iCalendar properties from a RawVEvent."""
    uid = ""
    summary = ""
    location = ""
    status = ""
    dtstart_raw: Optional[Tuple[str, Dict[str, str]]] = None
    dtend_raw: Optional[Tuple[str, Dict[str, str]]] = None
    duration_raw: Optional[str] = None
    recurrence_id_raw: Optional[Tuple[str, Dict[str, str]]] = None
    rrules: List[str] = []
    rdates_raw: List[Tuple[str, Dict[str, str]]] = []
    exdates_raw: List[Tuple[str, Dict[str, str]]] = []

    for name, params, val in event.properties:
        if name == "UID":
            uid = val.strip()
        elif name == "SUMMARY":
            summary = unescape_ics_text(val).strip()
        elif name == "LOCATION":
            location = unescape_ics_text(val).strip()
        elif name == "STATUS":
            status = val.strip().upper()
        elif name == "DTSTART":
            dtstart_raw = (val, params)
        elif name == "DTEND":
            dtend_raw = (val, params)
        elif name == "DURATION":
            duration_raw = val.strip()
        elif name == "RECURRENCE-ID":
            recurrence_id_raw = (val, params)
        elif name == "RRULE":
            rrules.append(val.strip())
        elif name == "RDATE":
            rdates_raw.append((val, params))
        elif name == "EXDATE":
            exdates_raw.append((val, params))

    return ParsedVEvent(
        source_line=event.source_line,
        uid=uid,
        summary=summary,
        location=location,
        status=status,
        dtstart_raw=dtstart_raw,
        dtend_raw=dtend_raw,
        duration_raw=duration_raw,
        recurrence_id_raw=recurrence_id_raw,
        rrules=rrules,
        rdates_raw=rdates_raw,
        exdates_raw=exdates_raw,
    )


def process_event_group(
    uid: str,
    masters: List[ParsedVEvent],
    overrides: List[ParsedVEvent],
    from_date: datetime.date,
    to_date: datetime.date,
    target_tz: Optional[datetime.tzinfo],
) -> Tuple[List[EventOccurrence], List[ValidationIssue]]:
    """Processes a logical recurring event group (master + detached overrides) for a UID."""
    occurrences: List[EventOccurrence] = []
    issues: List[ValidationIssue] = []

    # 1. Validation of master definitions
    if len(masters) > 1:
        first_line = masters[1].source_line
        issues.append(
            ValidationIssue(first_line, f"duplicate master VEVENT found for UID '{uid}'")
        )
        return [], issues

    if not masters:
        # Orphaned overrides
        first_line = overrides[0].source_line
        issues.append(
            ValidationIssue(
                first_line,
                f"detached override for UID '{uid}' has no matching master event",
            )
        )
        return [], issues

    master = masters[0]
    line = master.source_line
    summary_label = master.summary or uid

    # If master is cancelled, entire series is cancelled
    if master.status == "CANCELLED":
        return [], []

    # Master must have DTSTART
    if master.dtstart_raw is None:
        issues.append(ValidationIssue(line, f"event '{summary_label}' is missing DTSTART"))
        return [], issues

    start_dt, start_is_all_day, start_err = parse_ics_datetime(
        master.dtstart_raw[0], master.dtstart_raw[1], target_tz, convert_to_target=False
    )
    if start_err:
        issues.append(ValidationIssue(line, f"event '{summary_label}': {start_err}"))
        return [], issues

    if start_is_all_day:
        issues.append(
            ValidationIssue(
                line,
                f"event '{summary_label}' is an all-day event, which is not supported by the weekly timetable schema",
            )
        )
        return [], issues

    # Master end_dt / duration
    master_end_dt: Optional[datetime.datetime] = None
    if master.dtend_raw is not None:
        end_val, end_params = master.dtend_raw
        parsed_end, end_is_all_day, end_err = parse_ics_datetime(
            end_val, end_params, target_tz, convert_to_target=False
        )
        if end_err:
            issues.append(ValidationIssue(line, f"event '{summary_label}': {end_err}"))
            return [], issues
        if end_is_all_day:
            issues.append(ValidationIssue(line, f"event '{summary_label}' has all-day DTEND"))
            return [], issues
        master_end_dt = parsed_end
    elif master.duration_raw is not None:
        dur_delta = parse_iso_duration(master.duration_raw)
        if dur_delta is None:
            issues.append(
                ValidationIssue(
                    line,
                    f"event '{summary_label}' has unsupported duration '{master.duration_raw}'",
                )
            )
            return [], issues
        master_end_dt = start_dt + dur_delta
    else:
        issues.append(
            ValidationIssue(line, f"event '{summary_label}' is missing both DTEND and DURATION")
        )
        return [], issues

    dur_issue = _check_duration_bounds(start_dt, master_end_dt, summary_label, line)
    if dur_issue:
        issues.append(dur_issue)
        return [], issues

    master_duration = master_end_dt - start_dt

    # Parse master EXDATEs
    master_exdates: Set[Tuple[str, Any]] = set()
    for ex_val, ex_params in master.exdates_raw:
        for part in ex_val.split(","):
            part_str = part.strip()
            if not part_str:
                continue
            ex_dt, ex_all_day, ex_err = parse_ics_datetime(
                part_str, ex_params, target_tz, convert_to_target=False
            )
            if ex_err:
                issues.append(ValidationIssue(line, f"event '{summary_label}' invalid EXDATE: {ex_err}"))
                return [], issues
            if ex_all_day:
                issues.append(ValidationIssue(line, f"event '{summary_label}' has unsupported all-day EXDATE"))
                return [], issues
            if ex_dt:
                master_exdates.add(normalize_recurrence_key(ex_dt, start_dt.tzinfo))

    # Parse master RDATEs
    master_rdates: List[datetime.datetime] = []
    for rd_val, rd_params in master.rdates_raw:
        val_type = rd_params.get("VALUE", "").upper()
        if val_type == "PERIOD":
            issues.append(
                ValidationIssue(
                    line,
                    f"event '{summary_label}' has unsupported RDATE VALUE=PERIOD",
                )
            )
            return [], issues
        for part in rd_val.split(","):
            part_str = part.strip()
            if not part_str:
                continue
            if "/" in part_str:
                issues.append(
                    ValidationIssue(
                        line,
                        f"event '{summary_label}' has unsupported RDATE period format '{part_str}'",
                    )
                )
                return [], issues
            rd_dt, rd_all_day, rd_err = parse_ics_datetime(
                part_str, rd_params, target_tz, convert_to_target=False
            )
            if rd_err:
                issues.append(ValidationIssue(line, f"event '{summary_label}' invalid RDATE: {rd_err}"))
                return [], issues
            if rd_all_day:
                issues.append(ValidationIssue(line, f"event '{summary_label}' has all-day RDATE"))
                return [], issues
            if rd_dt:
                master_rdates.append(rd_dt)

    # 2. Process and validate detached overrides
    overrides_by_rec_id: Dict[Tuple[str, Any], ParsedVEvent] = {}
    override_rec_id_dts: Dict[Tuple[str, Any], datetime.datetime] = {}

    for ov in overrides:
        ov_line = ov.source_line
        ov_label = ov.summary or master.summary or uid

        rec_val, rec_params = ov.recurrence_id_raw  # type: ignore[misc]

        # Check for unsupported RANGE
        range_val = rec_params.get("RANGE", "").upper()
        if range_val:
            issues.append(
                ValidationIssue(
                    ov_line,
                    f"override for event '{ov_label}' uses unsupported RECURRENCE-ID RANGE='{range_val}'",
                )
            )
            continue

        rec_dt, rec_all_day, rec_err = parse_ics_datetime(
            rec_val, rec_params, target_tz, convert_to_target=False
        )
        if rec_err:
            issues.append(
                ValidationIssue(ov_line, f"override for event '{ov_label}': invalid RECURRENCE-ID: {rec_err}")
            )
            continue
        if rec_all_day:
            issues.append(
                ValidationIssue(
                    ov_line,
                    f"override for event '{ov_label}' has all-day RECURRENCE-ID, which does not match timed master",
                )
            )
            continue

        rec_key = normalize_recurrence_key(rec_dt, start_dt.tzinfo)

        if rec_key in overrides_by_rec_id:
            issues.append(
                ValidationIssue(
                    ov_line,
                    f"conflicting duplicate override for event '{ov_label}' with RECURRENCE-ID '{rec_val}'",
                )
            )
            continue

        overrides_by_rec_id[rec_key] = ov
        override_rec_id_dts[rec_key] = rec_dt

    if issues:
        return [], issues

    # 3. Recurrence expansion bounds calculation
    window_tz = target_tz or start_dt.tzinfo
    window_start = datetime.datetime.combine(from_date, datetime.time.min)
    window_end = datetime.datetime.combine(to_date, datetime.time.max)
    if window_tz is not None:
        window_start = window_start.replace(tzinfo=window_tz)
        window_end = window_end.replace(tzinfo=window_tz)
        if start_dt.tzinfo is not None and window_tz != start_dt.tzinfo:
            window_start = window_start.astimezone(start_dt.tzinfo)
            window_end = window_end.astimezone(start_dt.tzinfo)

    # If any override RECURRENCE-ID lies outside the window, expand bounding dates
    exp_start = window_start
    exp_end = window_end
    for rec_dt in override_rec_id_dts.values():
        rec_aligned = rec_dt
        if start_dt.tzinfo is not None and rec_dt.tzinfo is not None and rec_dt.tzinfo != start_dt.tzinfo:
            rec_aligned = rec_dt.astimezone(start_dt.tzinfo)
        elif start_dt.tzinfo is not None and rec_dt.tzinfo is None:
            rec_aligned = rec_dt.replace(tzinfo=start_dt.tzinfo)
        exp_start = min(exp_start, rec_aligned)
        exp_end = max(exp_end, rec_aligned)

    occurrence_start_times: List[datetime.datetime] = []

    if not master.rrules:
        # Non-recurring master
        if exp_start <= start_dt <= exp_end:
            occurrence_start_times.append(start_dt)
    else:
        for rrule_str in master.rrules:
            freq_match = re.search(r"FREQ=([A-Z]+)", rrule_str, re.IGNORECASE)
            if not freq_match:
                issues.append(
                    ValidationIssue(line, f"event '{summary_label}' has malformed RRULE '{rrule_str}'")
                )
                return [], issues
            freq = freq_match.group(1).upper()
            if freq != "WEEKLY":
                issues.append(
                    ValidationIssue(
                        line,
                        f"event '{summary_label}' has unsupported recurrence frequency '{freq}'; "
                        "only FREQ=WEEKLY is supported",
                    )
                )
                return [], issues

            rule_text = rrule_str
            if not rule_text.upper().startswith("RRULE:"):
                rule_text = "RRULE:" + rule_text

            try:
                rr = dateutil.rrule.rrulestr(
                    rule_text, dtstart=start_dt, ignoretz=(start_dt.tzinfo is None)
                )
            except Exception as exc:
                issues.append(
                    ValidationIssue(
                        line,
                        f"event '{summary_label}' has invalid RRULE '{rrule_str}': {exc}",
                    )
                )
                return [], issues

            try:
                gen_starts = rr.between(exp_start, exp_end, inc=True)
                occurrence_start_times.extend(gen_starts)
            except Exception as exc:
                issues.append(
                    ValidationIssue(
                        line,
                        f"event '{summary_label}' failed recurrence expansion: {exc}",
                    )
                )
                return [], issues

    # Add RDATEs that fall inside [exp_start, exp_end]
    for rd in master_rdates:
        if exp_start <= rd <= exp_end:
            occurrence_start_times.append(rd)

    # Canonical mapping of all expanded master instances
    # Map key -> occ_start_dt
    master_instances: Dict[Tuple[str, Any], datetime.datetime] = {}
    for st in occurrence_start_times:
        k = normalize_recurrence_key(st, start_dt.tzinfo)
        if k not in master_instances:
            master_instances[k] = st

    # 4. Validate that all overrides match a valid recurrence instance
    for rec_key, ov in overrides_by_rec_id.items():
        if rec_key not in master_instances:
            rec_val = ov.recurrence_id_raw[0]  # type: ignore[index]
            issues.append(
                ValidationIssue(
                    ov.source_line,
                    f"override for event '{ov.summary or summary_label}' references non-existent recurrence instance '{rec_val}'",
                )
            )

    if issues:
        return [], issues

    # 5. Build final occurrences
    # Filter master instances: exclude master_exdates and overridden/cancelled instances
    for inst_key, inst_start in sorted(master_instances.items(), key=lambda item: item[1]):
        if inst_key in master_exdates:
            continue
        if inst_key in overrides_by_rec_id:
            # Replaced by override; will be processed below
            continue

        # Check if master occurrence falls within configured window [window_start, window_end]
        if window_start <= inst_start <= window_end:
            occ_end = inst_start + master_duration
            occurrences.append(
                _build_occurrence(
                    uid=uid,
                    summary=master.summary,
                    location=master.location,
                    occ_start=inst_start,
                    occ_end=occ_end,
                    target_tz=target_tz,
                    source_line=master.source_line,
                )
            )

    # 6. Process detached overrides
    for rec_key, ov in overrides_by_rec_id.items():
        if ov.status == "CANCELLED":
            # Instance is cancelled; omitted from final occurrences
            continue

        ov_line = ov.source_line
        ov_label = ov.summary or master.summary or uid

        # Inherit fields: summary, location, start, duration
        ov_summary = ov.summary if ov.summary else master.summary
        ov_location = ov.location if ov.location else master.location

        # Effective start time
        if ov.dtstart_raw is not None:
            ov_start_dt, ov_start_all_day, ov_start_err = parse_ics_datetime(
                ov.dtstart_raw[0], ov.dtstart_raw[1], target_tz, convert_to_target=False
            )
            if ov_start_err:
                issues.append(ValidationIssue(ov_line, f"override for event '{ov_label}': {ov_start_err}"))
                continue
            if ov_start_all_day:
                issues.append(
                    ValidationIssue(
                        ov_line,
                        f"override for event '{ov_label}' has unsupported all-day DTSTART",
                    )
                )
                continue
        else:
            # Inherit original occurrence start time
            ov_start_dt = master_instances[rec_key]

        # Effective end time / duration
        ov_end_dt: Optional[datetime.datetime] = None
        if ov.dtend_raw is not None:
            ov_end_val, ov_end_params = ov.dtend_raw
            parsed_end, end_all_day, end_err = parse_ics_datetime(
                ov_end_val, ov_end_params, target_tz, convert_to_target=False
            )
            if end_err:
                issues.append(ValidationIssue(ov_line, f"override for event '{ov_label}': {end_err}"))
                continue
            if end_all_day:
                issues.append(
                    ValidationIssue(
                        ov_line,
                        f"override for event '{ov_label}' has all-day DTEND",
                    )
                )
                continue
            ov_end_dt = parsed_end
        elif ov.duration_raw is not None:
            ov_dur_delta = parse_iso_duration(ov.duration_raw)
            if ov_dur_delta is None:
                issues.append(
                    ValidationIssue(
                        ov_line,
                        f"override for event '{ov_label}' has unsupported duration '{ov.duration_raw}'",
                    )
                )
                continue
            ov_end_dt = ov_start_dt + ov_dur_delta
        else:
            # Inherit master duration
            ov_end_dt = ov_start_dt + master_duration

        dur_err = _check_duration_bounds(ov_start_dt, ov_end_dt, ov_label, ov_line)
        if dur_err:
            issues.append(dur_err)
            continue

        # Check if the override's effective start time falls within the configured window
        # Align ov_start_dt to window_tz for window check
        check_start = ov_start_dt
        if window_tz is not None and ov_start_dt.tzinfo is not None and ov_start_dt.tzinfo != window_tz:
            check_start = ov_start_dt.astimezone(window_tz)
        elif window_tz is not None and ov_start_dt.tzinfo is None:
            check_start = ov_start_dt.replace(tzinfo=window_tz)

        # Build window bounds in window_tz
        w_start_local = datetime.datetime.combine(from_date, datetime.time.min)
        w_end_local = datetime.datetime.combine(to_date, datetime.time.max)
        if window_tz is not None:
            w_start_local = w_start_local.replace(tzinfo=window_tz)
            w_end_local = w_end_local.replace(tzinfo=window_tz)

        if w_start_local <= check_start <= w_end_local:
            occurrences.append(
                _build_occurrence(
                    uid=uid,
                    summary=ov_summary,
                    location=ov_location,
                    occ_start=ov_start_dt,
                    occ_end=ov_end_dt,
                    target_tz=target_tz,
                    source_line=ov_line,
                )
            )

    return occurrences, issues


def expand_vevent(
    event: RawVEvent,
    from_date: datetime.date,
    to_date: datetime.date,
    target_tz: Optional[datetime.tzinfo] = None,
) -> Tuple[List[EventOccurrence], List[ValidationIssue]]:
    """Backward-compatible wrapper to expand a single stand-alone VEVENT."""
    parsed = parse_vevent_properties(event)
    uid = parsed.uid or f"event-line-{parsed.source_line}"
    if parsed.recurrence_id_raw is not None:
        return process_event_group(
            uid=uid,
            masters=[],
            overrides=[parsed],
            from_date=from_date,
            to_date=to_date,
            target_tz=target_tz,
        )
    return process_event_group(
        uid=uid,
        masters=[parsed],
        overrides=[],
        from_date=from_date,
        to_date=to_date,
        target_tz=target_tz,
    )


def plan_ics_import(
    ics_path: Path | str,
    timetable_data: Dict[str, Any],
    from_date: datetime.date,
    to_date: datetime.date,
    timezone_name: Optional[str] = None,
) -> IcsImportPlan:
    """Validates an ICS file against ``timetable_data`` and plans the import.

    Expands events occurring between ``from_date`` and ``to_date`` (inclusive),
    resolving master events, EXDATE exclusions, RDATE additions, and detached
    RECURRENCE-ID overrides and cancellations. Checks for duplicate entries and
    scheduling conflicts.
    """
    file_path = Path(ics_path)
    if not file_path.exists():
        raise ImportError_(f"ICS file not found: '{file_path}'")

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ImportError_(f"Could not read ICS file '{file_path}': {exc}") from exc
    except OSError as exc:
        raise ImportError_(f"Could not read ICS file '{file_path}': {exc}") from exc

    target_tz = None
    if timezone_name:
        try:
            target_tz = zoneinfo.ZoneInfo(timezone_name)
        except Exception as exc:
            raise ImportError_(f"Invalid timezone '{timezone_name}': {exc}") from exc

    events, issues = parse_ics_content(content)
    plan = IcsImportPlan()
    plan.events_found = len(events)

    if issues:
        plan.invalid_records.extend(issues)
        return plan

    # Group components by UID: masters vs overrides
    parsed_events: List[ParsedVEvent] = [parse_vevent_properties(ev) for ev in events]
    grouped_masters: Dict[str, List[ParsedVEvent]] = {}
    grouped_overrides: Dict[str, List[ParsedVEvent]] = {}
    uid_order: List[str] = []

    for pe in parsed_events:
        uid = pe.uid
        if not uid:
            uid = f"event-line-{pe.source_line}"
        if uid not in grouped_masters and uid not in grouped_overrides:
            uid_order.append(uid)

        if pe.recurrence_id_raw is not None:
            grouped_overrides.setdefault(uid, []).append(pe)
        else:
            grouped_masters.setdefault(uid, []).append(pe)

    all_occurrences: List[EventOccurrence] = []
    for uid in uid_order:
        masters = grouped_masters.get(uid, [])
        overrides = grouped_overrides.get(uid, [])
        ev_occs, ev_issues = process_event_group(
            uid=uid,
            masters=masters,
            overrides=overrides,
            from_date=from_date,
            to_date=to_date,
            target_tz=target_tz,
        )
        if ev_issues:
            plan.invalid_records.extend(ev_issues)
        else:
            all_occurrences.extend(ev_occs)

    if plan.invalid_records:
        return plan

    plan.occurrences_generated = len(all_occurrences)

    # In a weekly timetable, individual date occurrences that map to the same
    # (day, canonical_slot) represent recurring weekly instances of the same slot.
    # We deduplicate within the proposed import.
    seen_in_import: Set[Tuple[str, Tuple[str, str, str, str]]] = set()
    unique_import_occurrences: List[EventOccurrence] = []

    for occ in all_occurrences:
        key = (occ.day, _canonical(occ.slot))
        if key in seen_in_import:
            plan.duplicates.append(DuplicateEntry(occ.source_line, occ.day, occ.slot))
            continue
        seen_in_import.add(key)
        unique_import_occurrences.append(occ)

    # Check for duplicates against existing timetable entries
    existing_keys = set()
    for day in WEEKDAYS:
        for slot in _existing_slots(timetable_data, day):
            existing_keys.add((day, _canonical(slot)))

    for occ in unique_import_occurrences:
        key = (occ.day, _canonical(occ.slot))
        if key in existing_keys:
            plan.duplicates.append(DuplicateEntry(occ.source_line, occ.day, occ.slot))
        else:
            plan.new_classes.append((occ.source_line, occ.day, occ.slot))

    # Conflict detection:
    # 1. Check between proposed new classes
    # 2. Check between new classes and existing timetable slots
    src_str = str(file_path)
    for issue in plan.invalid_records:
        plan.diagnostics.append(
            Diagnostic(
                rule_id=issue.rule_id or RuleId.IMPORT_RECORD_MALFORMED.value,
                severity=Severity.ERROR.value,
                message=issue.message,
                source=Provenance(source_type="ics", source_path=src_str, line=issue.row),
            )
        )

    for day in WEEKDAYS:
        new_day_tuples = [(line_no, slot) for line_no, entry_day, slot in plan.new_classes if entry_day == day]
        existing_day = _existing_slots(timetable_data, day)

        for index, (first_line, first) in enumerate(new_day_tuples):
            entry_a = AffectedEntry(day=day, slot=first, provenance=Provenance(source_type="ics", source_path=src_str, line=first_line))
            for second_line, second in new_day_tuples[index + 1:]:
                if do_overlap(first, second):
                    plan.conflicts.append(ConflictPair(day, first, second))
                    entry_b = AffectedEntry(day=day, slot=second, provenance=Provenance(source_type="ics", source_path=src_str, line=second_line))
                    plan.diagnostics.append(
                        Diagnostic(
                            rule_id=RuleId.SCHEDULE_CONFLICT.value,
                            severity=Severity.ERROR.value,
                            message=f"Schedule conflict on {day.capitalize()} between '{first.get('subject')}' and '{second.get('subject')}'",
                            affected_entries=[entry_a, entry_b],
                            source=Provenance(source_type="ics", source_path=src_str, line=first_line),
                        )
                    )

            for existing in existing_day:
                if do_overlap(first, existing):
                    plan.conflicts.append(ConflictPair(day, first, existing))
                    entry_exist = AffectedEntry(day=day, slot=existing, provenance=Provenance(source_type="timetable"))
                    plan.diagnostics.append(
                        Diagnostic(
                            rule_id=RuleId.SCHEDULE_CONFLICT.value,
                            severity=Severity.ERROR.value,
                            message=f"Schedule conflict on {day.capitalize()} between imported event '{first.get('subject')}' and existing class '{existing.get('subject')}'",
                            affected_entries=[entry_a, entry_exist],
                            source=Provenance(source_type="ics", source_path=src_str, line=first_line),
                        )
                    )

    return plan


def render_ics_plan(plan: IcsImportPlan, dry_run: bool = False) -> str:
    """Returns a deterministic human-readable summary of an ICS import plan."""
    lines = []
    title = "Dry run summary (ICS)" if dry_run else "Import summary (ICS)"
    lines.append(title)
    lines.append(f"Calendar events found: {plan.events_found}")
    lines.append(f"Occurrences generated: {plan.occurrences_generated}")
    lines.append(f"New weekly classes to import: {len(plan.new_classes)}")
    lines.append(f"Duplicate entries skipped: {len(plan.duplicates)}")
    lines.append(f"Invalid events rejected: {len(plan.invalid_records)}")
    lines.append(f"Scheduling conflicts detected: {len(plan.conflicts)}")

    if plan.invalid_records:
        lines.append("")
        lines.append("Validation errors:")
        for issue in plan.invalid_records:
            lines.append(f"  Line {issue.row}: {issue.message}")

    if plan.duplicates:
        lines.append("")
        lines.append("Skipped duplicates:")
        for duplicate in plan.duplicates:
            slot = duplicate.slot
            lines.append(
                f"  Line {duplicate.row}: {duplicate.day.capitalize()} "
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
