"""Command Line Interface for Timetable CLI."""

import argparse
import sys
from pathlib import Path

from timetable.loader import load_timetable, save_timetable, DEFAULT_FILEPATH
from timetable.display import render_day, render_week, WEEKDAYS
from timetable.filter import filter_timetable, render_filter
from timetable.now import current_now, render_now
from timetable.export import export_ics
from timetable.diff import DiffError, diff_timetables, load_for_diff, render_diff
from timetable.conflicts import find_conflicts, render_conflicts
from timetable.stats import compute_stats, render_stats
from timetable.merge import (
    MergeError,
    load_timetable_for_merge,
    merge_timetables,
    render_merge_conflicts,
)

def validate_day(day_str):
    """Validates that day_str is a recognized weekday, ignoring case.
    
    Returns the lowercased weekday name.
    Exits with error listing valid days if invalid.
    """
    day = day_str.lower()
    if day not in WEEKDAYS:
        valid_days = ", ".join(WEEKDAYS)
        print(f"Error: '{day_str}' is not a valid day. Valid days are: {valid_days}", file=sys.stderr)
        sys.exit(1)
    return day


def parse_time(time_str):
    """Parses and validates 24-hour HH:MM time string, returning minutes from midnight.
    
    Exits with error if format or time is invalid.
    """
    parts = time_str.split(":")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
        print(f"Error: Invalid time format '{time_str}'. Time must be 24-hour HH:MM.", file=sys.stderr)
        sys.exit(1)
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        print(f"Error: Non-numeric time value in '{time_str}'.", file=sys.stderr)
        sys.exit(1)

    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        print(f"Error: Time '{time_str}' does not exist. Hours must be 00-23 and minutes 00-59.", file=sys.stderr)
        sys.exit(1)

    return hours * 60 + minutes


def check_clash(slots, start_min, end_min):
    """Checks whether the new [start_min, end_min) interval overlaps with any existing slot.
    
    Returns the clashing slot dictionary, or None.
    """
    for slot in slots:
        try:
            sh, sm = map(int, slot["start"].split(":"))
            eh, em = map(int, slot["end"].split(":"))
            slot_start = sh * 60 + sm
            slot_end = eh * 60 + em
            if max(start_min, slot_start) < min(end_min, slot_end):
                return slot
        except Exception:
            continue
    return None


def cmd_show(args):
    day = validate_day(args.day)
    data = load_timetable(args.file)
    render_day(data, day)


def cmd_week(args):
    data = load_timetable(args.file)
    render_week(data)


def cmd_now(args):
    data = load_timetable(args.file)
    render_now(data, now=current_now())


def cmd_filter(args):
    data = load_timetable(args.file)
    results = filter_timetable(
        data,
        day=args.day,
        start=args.start,
        end=args.end,
        room=args.room,
    )
    render_filter(results, day=args.day, start=args.start, end=args.end, room=args.room)


def cmd_export(args):
    data = load_timetable(args.file)
    export_ics(data, args.output)
    print(f"Exported {args.output}")


def cmd_add(args):
    day = validate_day(args.day)
    start_min = parse_time(args.start)
    end_min = parse_time(args.end)

    if end_min <= start_min:
        print(f"Error: Class ends before or at its start time ({args.start} to {args.end}).", file=sys.stderr)
        sys.exit(1)

    data = load_timetable(args.file)
    if day not in data:
        data[day] = []

    clashing_slot = check_clash(data[day], start_min, end_min)
    if clashing_slot:
        clash_name = clashing_slot.get("subject", "Existing class")
        clash_time = f"{clashing_slot.get('start')} - {clashing_slot.get('end')}"
        print(
            f"Error: Class overlaps with '{clash_name}' ({clash_time}) on {day.capitalize()}.",
            file=sys.stderr
        )
        sys.exit(1)

    data[day].append({
        "subject": args.subject,
        "start": args.start,
        "end": args.end,
        "room": args.room
    })
    data[day].sort(key=lambda s: s.get("start", ""))

    save_timetable(data, args.file)
    print(f"Successfully added '{args.subject}' to {day.capitalize()}.")


def cmd_diff(args):
    """Compares two timetable files and reports what changed.

    Exits with status 1 when the timetables differ and 0 when they are
    identical, so the command can be used in scripts.
    """
    try:
        first = load_for_diff(args.first)
        second = load_for_diff(args.second)
    except DiffError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    result = diff_timetables(first, second)
    render_diff(result)
    sys.exit(1 if result.has_changes else 0)


def cmd_conflicts(args):
    """Detects overlapping classes in the timetable file."""
    filepath = args.file if args.file is not None else DEFAULT_FILEPATH
    target = Path(filepath)

    if not target.exists():
        print(f"Error: Timetable file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    try:
        data = load_timetable(target)
    except Exception as exc:
        print(f"Error reading {filepath}: {exc}", file=sys.stderr)
        sys.exit(1)

    conflicts = find_conflicts(data)
    render_conflicts(conflicts)
    sys.exit(1 if conflicts else 0)


def cmd_stats(args):
    """Prints aggregate statistics for the whole timetable."""
    filepath = args.file if args.file is not None else DEFAULT_FILEPATH
    target = Path(filepath)

    if not target.exists():
        print(f"Error: Timetable file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    try:
        data = load_timetable(target)
    except Exception as exc:
        print(f"Error: cannot read {filepath}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        stats = compute_stats(data)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    render_stats(stats)

def cmd_merge(args):
    """Merges two timetable JSON files with conflict detection."""
    try:
        first_path = Path(args.first).resolve()
        second_path = Path(args.second).resolve()
    except Exception as exc:
        print(f"Error resolving paths: {exc}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output).resolve() if args.output else None

    if output_path:
        if output_path == first_path or output_path == second_path:
            print(
                f"Error: Output file '{args.output}' cannot be the same as input file '{args.first if output_path == first_path else args.second}'.",
                file=sys.stderr,
            )
            sys.exit(1)
        if output_path.exists() and not args.force:
            print(
                f"Error: Output file '{args.output}' already exists. Use --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        first_data = load_timetable_for_merge(args.first)
        second_data = load_timetable_for_merge(args.second)
    except MergeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    merged_data, conflicts = merge_timetables(first_data, second_data)

    if conflicts:
        print(render_merge_conflicts(conflicts), file=sys.stderr)
        print("Error: Could not merge timetables due to scheduling conflicts.", file=sys.stderr)
        sys.exit(1)

    if output_path:
        save_timetable(merged_data, output_path)
        print(f"Successfully merged timetables into '{args.output}'.")
    else:
        print("Successfully merged timetables (dry run, no --output specified).")

    sys.exit(0)


def main():
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Timetable CLI - Manage and view your weekly schedule")
    parser.add_argument("--file", default=DEFAULT_FILEPATH, help="Path to timetable.json")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # show
    p_show = subparsers.add_parser("show", help="Show classes for a specific day")
    p_show.add_argument("day", help="Day of the week (e.g. monday, tuesday)")
    p_show.set_defaults(func=cmd_show)

    # week
    p_week = subparsers.add_parser("week", help="Show full weekly schedule")
    p_week.set_defaults(func=cmd_week)

    # now
    p_now = subparsers.add_parser("now", help="Show what is on right now")
    p_now.set_defaults(func=cmd_now)

    # filter
    p_filter = subparsers.add_parser("filter", help="Filter classes by day, time range and room")
    p_filter.add_argument("--day", help="Day of the week (e.g. monday, tuesday)")
    p_filter.add_argument("--start", help="Range start time (HH:MM)")
    p_filter.add_argument("--end", help="Range end time (HH:MM)")
    p_filter.add_argument("--room", help="Room/location to match exactly")
    p_filter.set_defaults(func=cmd_filter)

    # export
    p_export = subparsers.add_parser("export", help="Export the timetable as an .ics calendar file")
    p_export.add_argument("--output", default="timetable.ics", help="Path of the .ics file to write (default: timetable.ics)")
    p_export.set_defaults(func=cmd_export)

    # add
    p_add = subparsers.add_parser("add", help="Add a new class slot")
    p_add.add_argument("--day", required=True, help="Day of the week")
    p_add.add_argument("--subject", required=True, help="Subject name")
    p_add.add_argument("--start", required=True, help="Start time (HH:MM)")
    p_add.add_argument("--end", required=True, help="End time (HH:MM)")
    p_add.add_argument("--room", required=True, help="Room/location")
    p_add.set_defaults(func=cmd_add)

    # diff
    p_diff = subparsers.add_parser(
        "diff",
        help="Compare two timetable files and report what changed",
    )
    p_diff.add_argument("first", help="Path to the first timetable JSON file")
    p_diff.add_argument("second", help="Path to the second timetable JSON file")
    p_diff.set_defaults(func=cmd_diff)

    # conflicts
    p_conflicts = subparsers.add_parser(
        "conflicts",
        help="Detect overlapping class schedules on the same day",
    )
    p_conflicts.add_argument(
        "file",
        nargs="?",
        default=None,
        help="Path to timetable JSON file (defaults to timetable.json)",
    )
    p_conflicts.set_defaults(func=cmd_conflicts)

    # stats
    p_stats = subparsers.add_parser(
        "stats",
        help="Compute and print aggregate statistics for the whole timetable",
    )
    p_stats.add_argument(
        "file",
        nargs="?",
        default=None,
        help="Path to timetable JSON file (defaults to timetable.json)",
    )
    p_stats.set_defaults(func=cmd_stats)

    # merge
    p_merge = subparsers.add_parser(
        "merge",
        help="Merge two timetable JSON files with conflict detection",
    )
    p_merge.add_argument("first", help="Path to first timetable JSON file")
    p_merge.add_argument("second", help="Path to second timetable JSON file")
    p_merge.add_argument("--output", help="Path to output merged timetable JSON file")
    p_merge.add_argument("--force", action="store_true", help="Overwrite output file if it already exists")
    p_merge.set_defaults(func=cmd_merge)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()