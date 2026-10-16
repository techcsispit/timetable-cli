"""iCalendar (.ics) import planning, parsing, recurrence expansion, and validation.

This module parses iCalendar (.ics) streams and files conforming to RFC 5545,
expands recurring events (RRULE, RDATE, EXDATE) across a finite expansion window,
and maps supported events to the weekly timetable schema.
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
    curr = []
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
    weekly calendar arithmetic remains anchored to its source timezone.  Each
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


def expand_vevent(
    event: RawVEvent,
    from_date: datetime.date,
    to_date: datetime.date,
    target_tz: Optional[datetime.tzinfo] = None,
) -> Tuple[List[EventOccurrence], List[ValidationIssue]]:
    """Validates and expands a single VEVENT across [from_date, to_date].

    Returns (occurrences, issues).
    """
    occurrences: List[EventOccurrence] = []
    issues: List[ValidationIssue] = []
    line = event.source_line

    uid = ""
    summary = ""
    location = ""
    status = ""
    dtstart_raw: Optional[Tuple[str, Dict[str, str]]] = None
    dtend_raw: Optional[Tuple[str, Dict[str, str]]] = None
    duration_raw: Optional[str] = None
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
        elif name == "RRULE":
            rrules.append(val.strip())
        elif name == "RDATE":
            rdates_raw.append((val, params))
        elif name == "EXDATE":
            exdates_raw.append((val, params))

    # Explicit policy: Cancelled events are skipped
    if status == "CANCELLED":
        return [], []

    if not uid:
        # In timetable schema, UID is recommended; generate fallback or note
        uid = f"event-line-{line}"

    # DTSTART validation
    if dtstart_raw is None:
        issues.append(ValidationIssue(line, f"event '{summary or uid}' is missing DTSTART"))
        return [], issues

    start_dt, start_is_all_day, start_err = parse_ics_datetime(
        dtstart_raw[0], dtstart_raw[1], target_tz, convert_to_target=False
    )
    if start_err:
        issues.append(ValidationIssue(line, f"event '{summary or uid}': {start_err}"))
        return [], issues

    # All-day events policy: Timetable only models timed weekly classes with start & end times.
    # All-day events cannot be represented and are rejected with a clear message.
    if start_is_all_day:
        issues.append(
            ValidationIssue(
                line,
                f"event '{summary or uid}' is an all-day event, which is not supported by the weekly timetable schema",
            )
        )
        return [], issues

    # Calculate end_dt
    end_dt: Optional[datetime.datetime] = None
    if dtend_raw is not None:
        end_val, end_params = dtend_raw
        parsed_end, end_is_all_day, end_err = parse_ics_datetime(
            end_val, end_params, target_tz, convert_to_target=False
        )
        if end_err:
            issues.append(ValidationIssue(line, f"event '{summary or uid}': {end_err}"))
            return [], issues
        if end_is_all_day:
            issues.append(ValidationIssue(line, f"event '{summary or uid}' has all-day DTEND"))
            return [], issues
        end_dt = parsed_end
    elif duration_raw is not None:
        # Parse ISO 8601 duration e.g. PT1H, PT50M, P1D
        dur_match = re.match(
            r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
            duration_raw,
        )
        if not dur_match:
            issues.append(ValidationIssue(line, f"event '{summary or uid}' has unsupported duration '{duration_raw}'"))
            return [], issues
        d_days, d_hours, d_mins, d_secs = dur_match.groups()
        delta = datetime.timedelta(
            days=int(d_days or 0),
            hours=int(d_hours or 0),
            minutes=int(d_mins or 0),
            seconds=int(d_secs or 0),
        )
        end_dt = start_dt + delta
    else:
        issues.append(ValidationIssue(line, f"event '{summary or uid}' is missing both DTEND and DURATION"))
        return [], issues

    # Duration check: must be strictly positive and under 24 hours
    duration_secs = (end_dt - start_dt).total_seconds()
    if duration_secs <= 0:
        issues.append(
            ValidationIssue(
                line,
                f"event '{summary or uid}' has zero or negative duration ({start_dt} to {end_dt})",
            )
        )
        return [], issues

    if duration_secs >= 24 * 3600:
        issues.append(
            ValidationIssue(
                line,
                f"event '{summary or uid}' duration exceeds 24 hours ({duration_secs / 3600:.1f}h), which cannot be represented in a daily slot",
            )
        )
        return [], issues

    # Parse EXDATE and RDATE additions
    exdates: Set[datetime.datetime] = set()
    for ex_val, ex_params in exdates_raw:
        for part in ex_val.split(","):
            if not part.strip():
                continue
            ex_dt, _, ex_err = parse_ics_datetime(
                part.strip(), ex_params, target_tz, convert_to_target=False
            )
            if ex_err:
                issues.append(ValidationIssue(line, f"event '{summary or uid}' invalid EXDATE: {ex_err}"))
                return [], issues
            if ex_dt:
                exdates.add(ex_dt)

    rdates: List[datetime.datetime] = []
    for rd_val, rd_params in rdates_raw:
        for part in rd_val.split(","):
            if not part.strip():
                continue
            rd_dt, rd_all_day, rd_err = parse_ics_datetime(
                part.strip(), rd_params, target_tz, convert_to_target=False
            )
            if rd_err:
                issues.append(ValidationIssue(line, f"event '{summary or uid}' invalid RDATE: {rd_err}"))
                return [], issues
            if rd_all_day:
                issues.append(ValidationIssue(line, f"event '{summary or uid}' has all-day RDATE"))
                return [], issues
            if rd_dt:
                rdates.append(rd_dt)

    # Recurrence expansion window bounds (inclusive)
    # We construct window_start and window_end covering the full days
    window_tz = target_tz or start_dt.tzinfo
    window_start = datetime.datetime.combine(from_date, datetime.time.min)
    window_end = datetime.datetime.combine(to_date, datetime.time.max)
    if window_tz is not None:
        window_start = window_start.replace(tzinfo=window_tz)
        window_end = window_end.replace(tzinfo=window_tz)
        if start_dt.tzinfo is not None and window_tz != start_dt.tzinfo:
            window_start = window_start.astimezone(start_dt.tzinfo)
            window_end = window_end.astimezone(start_dt.tzinfo)

    occurrence_start_times: List[datetime.datetime] = []

    if not rrules:
        # Non-recurring event: include if start_dt falls within [window_start, window_end]
        if window_start <= start_dt <= window_end and start_dt not in exdates:
            occurrence_start_times.append(start_dt)
    else:
        # Process RRULEs
        for rrule_str in rrules:
            # Validate RRULE frequency
            # Extract FREQ=...
            freq_match = re.search(r"FREQ=([A-Z]+)", rrule_str, re.IGNORECASE)
            if not freq_match:
                issues.append(ValidationIssue(line, f"event '{summary or uid}' has malformed RRULE '{rrule_str}'"))
                return [], issues
            freq = freq_match.group(1).upper()
            if freq != "WEEKLY":
                issues.append(
                    ValidationIssue(
                        line,
                        f"event '{summary or uid}' has unsupported recurrence frequency '{freq}'; only FREQ=WEEKLY is supported",
                    )
                )
                return [], issues

            # Build rruleset using dateutil
            rule_text = rrule_str
            if not rule_text.upper().startswith("RRULE:"):
                rule_text = "RRULE:" + rule_text

            try:
                # If UNTIL is present and in UTC with Z, but dtstart is naive or has different tz,
                # handle timezone alignment
                rr = dateutil.rrule.rrulestr(rule_text, dtstart=start_dt, ignoretz=(start_dt.tzinfo is None))
            except Exception as exc:
                issues.append(
                    ValidationIssue(
                        line,
                        f"event '{summary or uid}' has invalid RRULE '{rrule_str}': {exc}",
                    )
                )
                return [], issues

            try:
                # dateutil .between() takes inc=True for inclusive bounds
                gen_starts = rr.between(window_start, window_end, inc=True)
                occurrence_start_times.extend(gen_starts)
            except Exception as exc:
                issues.append(
                    ValidationIssue(
                        line,
                        f"event '{summary or uid}' failed recurrence expansion: {exc}",
                    )
                )
                return [], issues

    # Add RDATEs that fall inside the window and not excluded
    for rd in rdates:
        if window_start <= rd <= window_end:
            occurrence_start_times.append(rd)

    # Filter out EXDATEs and deduplicate start times
    valid_starts = sorted(
        {st for st in occurrence_start_times if st not in exdates}
    )

    event_duration = end_dt - start_dt

    for occ_start in valid_starts:
        occ_end = occ_start + event_duration
        display_start = occ_start
        display_end = occ_end
        if target_tz is not None and occ_start.tzinfo is not None:
            display_start = occ_start.astimezone(target_tz)
            display_end = occ_end.astimezone(target_tz)

        occ_date = display_start.date()
        # Derive weekday name
        weekday_idx = occ_date.weekday()  # Monday is 0, Sunday is 6
        day_name = WEEKDAYS[weekday_idx]

        start_str = display_start.strftime("%H:%M")
        end_str = display_end.strftime("%H:%M")

        slot = {
            "subject": summary if summary else "Untitled Event",
            "start": start_str,
            "end": end_str,
            "room": location,
        }

        occurrences.append(
            EventOccurrence(
                uid=uid,
                summary=summary,
                room=location,
                occurrence_date=occ_date,
                start_dt=display_start,
                end_dt=display_end,
                day=day_name,
                slot=slot,
                source_line=line,
            )
        )

    return occurrences, issues


def plan_ics_import(
    ics_path: Path | str,
    timetable_data: Dict[str, Any],
    from_date: datetime.date,
    to_date: datetime.date,
    timezone_name: Optional[str] = None,
) -> IcsImportPlan:
    """Validates an ICS file against ``timetable_data`` and plans the import.

    Expands events occurring between ``from_date`` and ``to_date`` (inclusive).
    Checks for duplicate entries and scheduling conflicts.
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

    all_occurrences: List[EventOccurrence] = []
    for ev in events:
        ev_occs, ev_issues = expand_vevent(ev, from_date, to_date, target_tz)
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
    for day in WEEKDAYS:
        new_day = [slot for _, entry_day, slot in plan.new_classes if entry_day == day]
        existing_day = _existing_slots(timetable_data, day)

        for index, first in enumerate(new_day):
            for second in new_day[index + 1:]:
                if do_overlap(first, second):
                    plan.conflicts.append(ConflictPair(day, first, second))

            for existing in existing_day:
                if do_overlap(first, existing):
                    plan.conflicts.append(ConflictPair(day, first, existing))

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

