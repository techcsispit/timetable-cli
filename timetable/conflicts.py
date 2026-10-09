"""Conflict detection for timetable schedules."""

from timetable.display import WEEKDAYS, parse_time


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

    return max(start_a, start_b) < min(end_a, end_b)


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

    for day in WEEKDAYS:
        slots = normalized_data.get(day, [])
        if not slots or len(slots) < 2:
            continue

        # Sort slots by start time
        sorted_slots = sorted(
            slots,
            key=lambda s: _to_minutes(s.get("start", "00:00"))
            if s.get("start")
            else 0,
        )

        n = len(sorted_slots)
        for i in range(n):
            for j in range(i + 1, n):
                slot_a = sorted_slots[i]
                slot_b = sorted_slots[j]
                if do_overlap(slot_a, slot_b):
                    conflicts.append((day.lower(), slot_a, slot_b))

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