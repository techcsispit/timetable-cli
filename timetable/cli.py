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


def cmd_show(args):
    data = load_timetable(args.file)
    render_day(data, args.day)


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
    data = load_timetable(args.file)
    day = args.day.lower()

    if day not in data:
        data[day] = []

    data[day].append({
        "subject": args.subject,
        "start": args.start,
        "end": args.end,
        "room": args.room
    })

    save_timetable(data, args.file)
    print(f"Successfully added '{args.subject}' to {args.day.capitalize()}.")


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


def main():
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

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()