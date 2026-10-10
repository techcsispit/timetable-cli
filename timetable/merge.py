"""Merge logic and utilities for combining two timetable JSON files."""

import json
from pathlib import Path
from timetable.display import WEEKDAYS, parse_time
from timetable.conflicts import do_overlap, find_conflicts


class MergeError(Exception):
    """Raised when an error occurs during timetable reading, validation, or merging."""
    pass


def validate_timetable_structure(data, source_name="timetable"):
    """Validates that ``data`` follows the timetable structure conventions.

    Structure rules:
    - Must be a JSON object (dict).
    - Keys should ideally be string day names.
    - Values associated with day keys must be lists of class dictionaries.
    - Each class dictionary must have subject, start, end, room fields (or valid types).
    """
    if not isinstance(data, dict):
        raise MergeError(
            f"Invalid timetable in '{source_name}': expected a JSON object mapping weekdays to lists of classes."
        )

    for day, slots in data.items():
        if not isinstance(day, str):
            raise MergeError(
                f"Invalid timetable in '{source_name}': weekday keys must be strings."
            )
        if not isinstance(slots, list):
            raise MergeError(
                f"Invalid timetable in '{source_name}': '{day}' must be a list of class slots."
            )
        for idx, slot in enumerate(slots, start=1):
            if not isinstance(slot, dict):
                raise MergeError(
                    f"Invalid timetable in '{source_name}': entry {idx} on '{day}' must be a class object dictionary."
                )


def load_timetable_for_merge(path):
    """Reads and validates a timetable JSON file.

    Raises MergeError if the file is missing, cannot be read, contains invalid JSON,
    or fails structural validation.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise MergeError(f"File not found: '{file_path}'")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise MergeError(f"Invalid JSON in '{file_path}': {exc}")
    except OSError as exc:
        raise MergeError(f"Could not read '{file_path}': {exc}")

    validate_timetable_structure(data, source_name=str(file_path))
    return data


def canonicalize_slot(slot):
    """Returns a canonical dictionary representation of a class slot."""
    return {
        "subject": str(slot.get("subject", "")),
        "start": str(slot.get("start", "")),
        "end": str(slot.get("end", "")),
        "room": str(slot.get("room", "")),
    }


def is_duplicate_slot(slot1, slot2):
    """Returns True if two class slots are identical across all relevant class details."""
    return (
        slot1.get("subject") == slot2.get("subject") and
        slot1.get("start") == slot2.get("start") and
        slot1.get("end") == slot2.get("end") and
        slot1.get("room") == slot2.get("room")
    )


def parse_start_minutes(time_str):
    """Parses 'HH:MM' string to total minutes for sorting."""
    try:
        t = parse_time(time_str)
        return t.hour * 60 + t.minute
    except Exception:
        return 0


def merge_timetables(first, second):
    """Combines two timetable dictionaries into a single timetable structure.

    - Preserves all canonical weekdays and any extra keys present.
    - Preserves unique classes.
    - Removes duplicate identical classes.
    - Orders output deterministically by canonical weekday (monday..sunday) then start time.
    - Detects overlapping classes on the same day.

    Returns:
        (merged_data, conflicts)
    """
    # Normalize input keys to lowercase for merging
    first_normalized = {}
    if isinstance(first, dict):
        for k, v in first.items():
            if isinstance(k, str) and isinstance(v, list):
                first_normalized[k.lower()] = v

    second_normalized = {}
    if isinstance(second, dict):
        for k, v in second.items():
            if isinstance(k, str) and isinstance(v, list):
                second_normalized[k.lower()] = v

    # Collect all day keys in canonical order, followed by any custom non-canonical keys
    all_keys = list(WEEKDAYS)
    for k in list(first_normalized.keys()) + list(second_normalized.keys()):
        if k not in all_keys:
            all_keys.append(k)

    merged_data = {}

    for day in all_keys:
        slots_1 = first_normalized.get(day, [])
        slots_2 = second_normalized.get(day, [])

        combined_slots = []

        # Add slots from first input
        for slot in slots_1:
            can_slot = canonicalize_slot(slot)
            if not any(is_duplicate_slot(can_slot, existing) for existing in combined_slots):
                combined_slots.append(can_slot)

        # Add slots from second input if not duplicate
        for slot in slots_2:
            can_slot = canonicalize_slot(slot)
            if not any(is_duplicate_slot(can_slot, existing) for existing in combined_slots):
                combined_slots.append(can_slot)

        # Sort slots deterministically by start time
        combined_slots.sort(key=lambda s: (parse_start_minutes(s["start"]), s["start"], s["subject"]))
        merged_data[day] = combined_slots

    # Detect conflicts in merged dataset
    conflicts = find_conflicts(merged_data)

    return merged_data, conflicts


def render_merge_conflicts(conflicts):
    """Prints merge conflict details to stdout or formatted string."""
    lines = []
    lines.append("⚠️  Merge Conflicts Detected:")
    lines.append("=" * 65)
    for day, a, b in conflicts:
        day_str = day.capitalize()
        lines.append(f"\nConflict on {day_str}:")
        lines.append(f"  • {a.get('start')} - {a.get('end')} : {a.get('subject')} [{a.get('room', 'No Room')}]")
        lines.append(f"  • {b.get('start')} - {b.get('end')} : {b.get('subject')} [{b.get('room', 'No Room')}]")
    lines.append("\n" + "=" * 65)
    return "\n".join(lines)
