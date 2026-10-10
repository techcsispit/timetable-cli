"""Reusable filtering of timetable slots.

Filtering is kept separate from the CLI so it can be reused and tested on
its own. Time-range matching uses half-open intervals, consistent with the
rest of the project: a class occupies ``[start, end)`` and a requested range
is ``[start, end)``. Two half-open intervals overlap when each starts before
the other ends, so a class that only touches a requested boundary (ends
exactly when the range starts, or starts exactly when the range ends) does
not match. Times are parsed with the existing ``parse_time`` helper.
"""

from timetable.display import WEEKDAYS, calc_duration, parse_time


def _minutes(value):
    """Converts a datetime.time into minutes since midnight."""
    return value.hour * 60 + value.minute


def _slot_minutes(slot, key):
    """Parses a slot's start/end HH:MM string into minutes since midnight.

    Returns None when the value is missing or not a valid time.
    """
    try:
        parsed = parse_time(slot.get(key, ""))
    except (ValueError, TypeError):
        return None
    return parsed.hour * 60 + parsed.minute


def _range_bounds(start, end):
    """Parses requested ``start``/``end`` HH:MM strings into minutes.

    Either argument may be None, meaning that side of the range is open.
    Returns a (low, high) tuple of minutes. Raises ValueError for a time
    that is not a valid HH:MM value.
    """
    low = 0 if start is None else _minutes(parse_time(start))
    high = 24 * 60 if end is None else _minutes(parse_time(end))
    return low, high


def overlaps_range(slot, start=None, end=None):
    """Returns True if ``slot`` intersects the half-open range [start, end).

    ``start`` and ``end`` are HH:MM strings (or None for an open bound). A
    slot that only touches a boundary does not overlap. Classes are assumed
    to start and end on the same day.
    """
    low, high = _range_bounds(start, end)

    slot_start = _slot_minutes(slot, "start")
    slot_end = _slot_minutes(slot, "end")
    if slot_start is None or slot_end is None:
        return False

    return slot_start < high and low < slot_end


def day_matches(day, requested):
    """Returns True if a weekday name matches the requested day.

    Comparison is case-insensitive; ``requested`` may be None to match any
    day.
    """
    if requested is None:
        return True
    if not isinstance(day, str) or not isinstance(requested, str):
        return False
    return day.strip().lower() == requested.strip().lower()


def filter_timetable(data, day=None, start=None, end=None, room=None):
    """Returns a new timetable containing only the matching slots.

    ``data`` maps weekday names to lists of class dictionaries, as returned
    by ``load_timetable``. ``day`` is a weekday name (case-insensitive);
    ``start`` and ``end`` are HH:MM strings defining the requested half-open
    time range; ``room`` is matched exactly against the slot's room. Any
    filter left as None is ignored, so filters can be combined freely.

    The input is not modified; a new dictionary is returned. Days with no
    matching slots are omitted, so an unmatched filter yields an empty dict.
    """
    if day is not None:
        day = day.strip().lower()
        if day not in WEEKDAYS:
            return {}

    results = {}
    for current_day, slots in data.items():
        if not day_matches(current_day, day):
            continue

        kept = []
        for slot in slots:
            if room is not None and slot.get("room") != room:
                continue
            if (start is not None or end is not None) and not overlaps_range(slot, start, end):
                continue
            kept.append(slot)

        if kept:
            results[current_day] = kept
    return results


def render_filter(results, day=None, start=None, end=None, room=None):
    """Prints the filtered timetable in the existing display style.

    The output mirrors ``render_day``: a heading, a column header, and one
    row per class showing time, duration, subject and room, listed in order
    of start time within each day. When nothing matches, a friendly message
    is printed instead of an error.
    """
    parts = []
    if day is not None:
        parts.append(day.capitalize())
    if start is not None or end is not None:
        parts.append(f"{start or '00:00'} - {end or '23:59'}")
    if room is not None:
        parts.append(room)
    heading = " | ".join(parts) if parts else "All classes"

    print(f"\nFiltered Timetable: {heading}")
    print("=" * 65)

    total = sum(len(slots) for slots in results.values())
    if total == 0:
        print("  No classes match the given filters.")
        return

    print(f"{'Time':<15} {'Duration':<12} {'Subject':<22} {'Room':<12}")
    print("-" * 65)
    for current_day in WEEKDAYS:
        slots = sorted(results.get(current_day, []), key=lambda s: s.get("start", ""))
        for slot in slots:
            dur = calc_duration(slot.get("start", "00:00"), slot.get("end", "00:00"))
            time_str = f"{slot.get('start')} - {slot.get('end')}"
            dur_str = f"{dur} min"
            print(f"{time_str:<15} {dur_str:<12} {slot.get('subject', ''):<22} {slot.get('room', ''):<12}")