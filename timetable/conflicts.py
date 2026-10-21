"""Conflict detection for timetable schedules."""

from timetable.display import WEEKDAYS, parse_time


MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY


def _to_minutes(time_str):
    """Converts a 'HH:MM' string to total minutes since midnight."""
    t = parse_time(time_str)
    return t.hour * 60 + t.minute


def do_overlap(slot_a, slot_b):
    """
    Checks if two slots overlap using half-open intervals [start, end).
    Two intervals overlap when: max(start_a, start_b) < min(end_a, end_b).
    Boundary touches (e.g. 09:00-10:00 and 10:00-11:00) return False.
    """
    try:
        start_a = _to_minutes(slot_a.get("start", ""))
        end_a = _to_minutes(slot_a.get("end", ""))
        start_b = _to_minutes(slot_b.get("start", ""))
        end_b = _to_minutes(slot_b.get("end", ""))
    except (ValueError, TypeError, AttributeError):
        return False

    # An end earlier than its start belongs to the following day.  Put both
    # slots on the same linear timeline before applying the usual half-open
    # interval test.
    if end_a < start_a:
        end_a += MINUTES_PER_DAY
    if end_b < start_b:
        end_b += MINUTES_PER_DAY

    return max(start_a, start_b) < min(end_a, end_b)


def _weekly_interval(day_index, slot):
    """Return a slot as absolute minutes in a reference week."""
    start = _to_minutes(slot.get("start", ""))
    end = _to_minutes(slot.get("end", ""))
    absolute_start = day_index * MINUTES_PER_DAY + start
    absolute_end = day_index * MINUTES_PER_DAY + end
    if end < start:
        absolute_end += MINUTES_PER_DAY
    return absolute_start, absolute_end


def _weekly_overlap(interval_a, interval_b):
    """Return whether two weekly intervals overlap, including week wrap."""
    start_a, end_a = interval_a
    start_b, end_b = interval_b

    # Sunday overnight events extend into the next reference week.  Comparing
    # the second interval one week backward and forward makes the calculation
    # circular without changing ordinary weekday behaviour.
    for offset in (-MINUTES_PER_WEEK, 0, MINUTES_PER_WEEK):
        shifted_start = start_b + offset
        shifted_end = end_b + offset
        if max(start_a, shifted_start) < min(end_a, shifted_end):
            return True
    return False


def weekly_slots_overlap(day_a, slot_a, day_b, slot_b):
    """Return whether two weekday-qualified slots overlap in a weekly cycle.

    Unlike :func:`do_overlap`, this helper preserves each slot's starting
    weekday.  It therefore detects overlaps caused by an overnight slot
    continuing into the following day, including Sunday-to-Monday wraparound.
    """
    try:
        interval_a = _weekly_interval(WEEKDAYS.index(day_a.lower()), slot_a)
        interval_b = _weekly_interval(WEEKDAYS.index(day_b.lower()), slot_b)
    except (ValueError, TypeError, AttributeError):
        return False
    return _weekly_overlap(interval_a, interval_b)


def find_clashing_slot(data, day, candidate):
    """Return the first existing slot that overlaps ``candidate``.

    ``day`` identifies where the candidate starts. Unlike full-record
    equality used for deduplication, this comparison deliberately considers
    only the weekly intervals, including overnight and week-wrap overlap.
    """
    try:
        candidate_day = WEEKDAYS.index(day.lower())
        candidate_interval = _weekly_interval(candidate_day, candidate)
    except (ValueError, TypeError, AttributeError):
        return None

    if not isinstance(data, dict):
        return None

    normalized_data = {
        key.lower(): value
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, list)
    }
    for day_index, weekday in enumerate(WEEKDAYS):
        for slot in normalized_data.get(weekday, []):
            try:
                interval = _weekly_interval(day_index, slot)
            except (ValueError, TypeError, AttributeError):
                continue
            if _weekly_overlap(candidate_interval, interval):
                return slot
    return None


def find_conflicts(data):
    """
    Detects scheduling conflicts across days in the timetable.

    Returns a list of tuples: (day_name, slot_a, slot_b)
    - Evaluated in canonical WEEKDAYS order.
    - Sorted chronologically by the start time of the earlier class.
    - Avoids duplicate reporting of identical pairs.
    """
    conflicts = []

    # Map keys case-insensitively to days
    normalized_data = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, list):
                normalized_data[k.lower()] = v

    events = []
    for day_index, day in enumerate(WEEKDAYS):
        for slot in normalized_data.get(day, []):
            try:
                interval = _weekly_interval(day_index, slot)
            except (ValueError, TypeError, AttributeError):
                continue
            events.append((interval[0], day_index, day, slot, interval))

    events.sort(key=lambda event: (event[0], event[1]))

    for index, event_a in enumerate(events):
        _, day_index_a, day_a, slot_a, interval_a = event_a
        for event_b in events[index + 1:]:
            _, day_index_b, day_b, slot_b, interval_b = event_b
            if not _weekly_overlap(interval_a, interval_b):
                continue

            # For a cross-day collision, report the weekday on which the
            # overnight class starts.  Same-day conflicts retain the existing
            # label and ordering.
            if day_a != day_b and interval_a[1] > (day_index_a + 1) * MINUTES_PER_DAY:
                conflict_day = day_a
                first_slot, second_slot = slot_a, slot_b
            elif day_a != day_b and interval_b[1] > (day_index_b + 1) * MINUTES_PER_DAY:
                conflict_day = day_b
                first_slot, second_slot = slot_b, slot_a
            else:
                conflict_day = day_a
                first_slot, second_slot = slot_a, slot_b
            conflicts.append((conflict_day, first_slot, second_slot))

    return conflicts


def render_conflicts(conflicts):
    """Renders conflict results cleanly to stdout."""
    if not conflicts:
        print("No conflicts detected.")
        return

    print("⚠️  Scheduling Conflicts Detected:")
    print("=" * 65)
    for day, a, b in conflicts:
        day_str = day.capitalize()
        print(f"\nConflict on {day_str}:")
        print(f"  • {a.get('start')} - {a.get('end')} : {a.get('subject')} [{a.get('room', 'No Room')}]")
        print(f"  • {b.get('start')} - {b.get('end')} : {b.get('subject')} [{b.get('room', 'No Room')}]")
    print("\n" + "=" * 65)
