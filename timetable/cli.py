"""Command Line Interface for Timetable CLI."""

import argparse
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

from timetable.loader import load_timetable, save_timetable, DEFAULT_FILEPATH
from timetable.display import render_day, render_week, WEEKDAYS
from timetable.filter import filter_timetable, render_filter
from timetable.now import current_now, render_now
from timetable.export import export_ics
from timetable.diff import DiffError, diff_timetables, load_for_diff, render_diff
from timetable.conflicts import find_conflicts, render_conflicts
from timetable.stats import compute_stats, render_stats
from timetable.history import (
    DEFAULT_RETENTION_LIMIT,
    ENV_HISTORY_LIMIT,
    HistoryError,
    SnapshotCorruptError,
    SnapshotMismatchError,
    SnapshotNotFoundError,
    WriteFailureError,
    default_retention_limit,
    history_directory,
    list_snapshots,
    load_snapshot,
    prune_snapshots,
    render_timetable,
    restore_snapshot,
    save_snapshot,
    show_snapshot,
    validate_retention_limit,
)
from timetable.merge import (
    MergeError,
    load_timetable_for_merge,
    merge_timetables,
    render_merge_conflicts,
)
from timetable.import_csv import (
    ImportError_,
    apply_import,
    load_timetable_for_import,
    plan_import,
    render_plan,
    save_timetable_atomic,
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


def _history_dir_for(args) -> Path:
    """Return the history directory for the active timetable file."""
    explicit = getattr(args, "history_dir", None)
    if explicit is not None:
        return Path(explicit)
    return history_directory(Path(args.file))


def _save_snapshot_before_file_write(
    data: Dict[str, Any],
    *,
    filepath: Path,
    operation: str,
) -> str:
    """Persist a snapshot of *data* before a successful file write.

    Returns the snapshot id so callers can assert that a snapshot was created.
    """
    snapshot_id = save_snapshot(
        data,
        filepath=filepath,
        operation=operation,
        history_dir=history_directory(filepath),
    )
    prune_snapshots(
        history_directory(filepath),
        default_retention_limit(),
        timetable=filepath,
    )
    return snapshot_id


def _atomic_save_timetable(data: Dict[str, Any], filepath: Path) -> None:
    """Write *data* to *filepath* atomically (write-to-temp then replace).

    :func:`timetable.loader.save_timetable` truncates the destination before
    writing, so a mid-write failure would leave a corrupted timetable.  This
    helper writes a temporary file next to the target and renames it over the
    target only after the JSON has been fully written and flushed.
    """
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_timetable_with_snapshot(
    data: Dict[str, Any],
    *,
    filepath: Path,
    operation: str,
    previous: Dict[str, Any],
) -> str:
    """Snapshot *previous*, then atomically write *data* to *filepath*.

    The snapshot records the timetable as it was *before* the mutation, so
    restoring it undoes the change.  It is taken only after the caller has
    validated the new data (i.e. when a write is genuinely about to happen),
    and the write is atomic so a failure never leaves a partially written
    timetable behind.  Returns the snapshot id.
    """
    snapshot_id = _save_snapshot_before_file_write(
        previous, filepath=filepath, operation=operation
    )
    _atomic_save_timetable(data, filepath)
    return snapshot_id


def cmd_history(args):
    """List snapshots, inspect one, or restore the timetable from one."""
    history_dir = _history_dir_for(args)

    if args.history_command == "show":
        if not args.snapshot_id:
            print("Error: 'history show' requires a snapshot id.", file=sys.stderr)
            return 1
        try:
            show_snapshot(
                args.snapshot_id,
                history_dir,
                timetable=Path(args.file),
            )
        except (
            SnapshotNotFoundError,
            SnapshotCorruptError,
            SnapshotMismatchError,
        ) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.history_command == "restore":
        if not args.snapshot_id:
            print("Error: 'history restore' requires a snapshot id.", file=sys.stderr)
            return 1
        return cmd_restore(args)

    # Default: list snapshots.
    if args.limit is not None:
        try:
            limit = validate_retention_limit(args.limit)
        except HistoryError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    else:
        limit = default_retention_limit()

    snapshots = list_snapshots(history_dir, limit=limit, timetable=Path(args.file))
    if not snapshots:
        print("No snapshots found.")
        return 0

    for snapshot in snapshots:
        created = snapshot.get("created_at") or "unknown"
        operation = snapshot.get("operation") or "unknown"
        print(f"{snapshot['id']}  {created}  {operation}")
    return 0


def cmd_restore(args):
    """Restore the active timetable from a snapshot."""
    history_dir = _history_dir_for(args)
    if not args.snapshot_id:
        print("Error: 'restore' requires a snapshot id.", file=sys.stderr)
        return 1

    try:
        result = restore_snapshot(
            args.snapshot_id,
            filepath=Path(args.file),
            history_dir=history_dir,
        )
    except (
        SnapshotNotFoundError,
        SnapshotCorruptError,
        SnapshotMismatchError,
        WriteFailureError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    prune_snapshots(
        history_dir,
        default_retention_limit(),
        timetable=Path(args.file),
    )
    print(f"Restored snapshot '{result['id']}' into '{args.file}'.")
    return 0


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
    previous_state = copy.deepcopy(data)
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

    # Snapshot the timetable as it was *before* this addition (so restoring
    # undoes it), then write atomically.  Validation above already succeeded,
    # so the write is genuinely attempted here.
    _write_timetable_with_snapshot(
        data,
        filepath=Path(args.file),
        operation="add",
        previous=previous_state,
    )
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

def cmd_import_csv(args):
    """Imports classes from a CSV file, with optional dry-run preview."""
    try:
        timetable_data = load_timetable_for_import(args.file)
        plan = plan_import(args.csv_file, timetable_data)
    except ImportError_ as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not plan.accepted:
        print(render_plan(plan, dry_run=args.dry_run), file=sys.stderr)
        if plan.invalid_records:
            print("Error: Could not import CSV due to validation errors.", file=sys.stderr)
        else:
            print("Error: Could not import CSV due to scheduling conflicts.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(render_plan(plan, dry_run=True))
        print("Dry run complete. No files were modified.")
        sys.exit(0)

    updated = apply_import(timetable_data, plan)
    try:
        save_timetable_atomic(updated, args.file)
    except OSError as exc:
        print(f"Error: Could not write timetable '{args.file}': {exc}", file=sys.stderr)
        sys.exit(1)

    print(render_plan(plan))
    print(f"Successfully imported {len(plan.new_classes)} class(es) into '{args.file}'.")
    sys.exit(0)


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
        previous_state = None
        output_file = Path(output_path)
        if output_file.exists():
            try:
                previous_state = load_timetable(output_file)
            except (OSError, ValueError):
                previous_state = None

        if previous_state is None:
            # Nothing meaningful to preserve (new or unreadable output file);
            # just write the merged data atomically.
            _atomic_save_timetable(merged_data, output_file)
        else:
            _write_timetable_with_snapshot(
                merged_data,
                filepath=output_file,
                operation="merge",
                previous=previous_state,
            )
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

    # import-csv
    p_import_csv = subparsers.add_parser(
        "import-csv",
        help="Import classes from a CSV file with validation, duplicate and conflict checks",
    )
    p_import_csv.add_argument("csv_file", help="Path to the CSV file to import")
    p_import_csv.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview the import without modifying the timetable",
    )
    p_import_csv.set_defaults(func=cmd_import_csv)

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

    # history
    p_history = subparsers.add_parser(
        "history",
        help="Inspect timetable snapshots and restore previous states",
    )
    p_history.add_argument(
        "history_command",
        nargs="?",
        choices=["list", "show", "restore"],
        default="list",
        help="Subcommand (default: list)",
    )
    p_history.add_argument(
        "snapshot_id",
        nargs="?",
        help="Snapshot identifier (required for show / restore).",
    )
    p_history.add_argument(
        "--limit",
        type=int,
        help="Maximum number of snapshots to list (positive integer). "
        "Defaults to %d or $%s if set." % (DEFAULT_RETENTION_LIMIT, ENV_HISTORY_LIMIT),
    )
    p_history.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="Directory in which snapshots are stored "
        "(defaults to .timetable-history next to the timetable).",
    )
    p_history.set_defaults(func=cmd_history)

    # restore
    p_restore = subparsers.add_parser(
        "restore",
        help="Restore the active timetable from a snapshot",
    )
    p_restore.add_argument("snapshot_id", help="Snapshot identifier to restore.")
    p_restore.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="Directory in which snapshots are stored "
        "(defaults to .timetable-history next to the timetable).",
    )
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    result = args.func(args)
    if isinstance(result, int) and result != 0:
        sys.exit(result)


if __name__ == "__main__":
    main()
