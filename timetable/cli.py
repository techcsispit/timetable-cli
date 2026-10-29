"""Command Line Interface for Timetable CLI."""

import argparse
import copy
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from timetable.loader import load_timetable, save_timetable, DEFAULT_FILEPATH
from timetable.schema import SchemaError
from timetable.display import render_day, render_week, WEEKDAYS
from timetable.filter import filter_timetable, render_filter
from timetable.free import find_free_slots, render_free_slots
from timetable.now import current_now, render_now
from timetable.export import export_ics
from timetable.diff import DiffError, diff_timetables, load_for_diff, render_diff
from timetable.conflicts import do_overlap, find_clashing_slot, find_conflicts, render_conflicts
from timetable.stats import compute_stats, render_stats
from timetable.engine import validate_timetable
from timetable.diagnostics import render_diagnostics
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
    merge_three_way,
    merge_timetables,
    render_merge_conflicts,
    render_three_way_conflicts,
    validate_timetable_structure,
)
from timetable.import_csv import (
    ImportError_,
    apply_import,
    load_timetable_for_import,
    plan_import,
    render_plan,
    save_timetable_atomic,
)
from timetable.optimizer_config import (
    ConfigurationError,
    InfeasibleScheduleError,
    ObjectiveWeights,
    OptimizationConfig,
    OptimizationError,
    TimeRange,
    load_optimization_config,
)
from timetable.optimizer_engine import (
    OptimizationResult,
    is_schedule_valid,
    optimize_timetable,
)

from timetable.import_ics import (
    plan_ics_import,
    render_ics_plan,
)

from timetable.undo import (
    HistoryFileError,
    NoOpError,
    commit_mutation,
    load_journal,
    redo_last,
    undo_last,
)
from timetable.locking import (
    ConcurrencyError,
    ConcurrentModificationError,
    LockTimeoutError,
    MultiFileLock,
    TimetableFileLock,
)

from timetable.transaction import TransactionCoordinator
from timetable.revision import REVISION_ABSENT, load_timetable_snapshot


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
    # int() also accepts signs, spaces and non-ASCII digits ("+6", " 8"), so
    # require plain 0-9 digits before converting.
    if not all(part.isascii() and part.isdigit() for part in parts):
        print(f"Error: Non-numeric time value in '{time_str}'.", file=sys.stderr)
        sys.exit(1)
    hours = int(parts[0])
    minutes = int(parts[1])

    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        print(f"Error: Time '{time_str}' does not exist. Hours must be 00-23 and minutes 00-59.", file=sys.stderr)
        sys.exit(1)

    return hours * 60 + minutes


def check_clash(slots, start_min, end_min):
    """Checks whether the new [start_min, end_min) interval overlaps with any existing slot.
    
    Returns the clashing slot dictionary, or None.
    """
    candidate = {
        "start": f"{start_min // 60:02d}:{start_min % 60:02d}",
        "end": f"{end_min // 60:02d}:{end_min % 60:02d}",
    }
    for slot in slots:
        if do_overlap(candidate, slot):
            return slot
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


def cmd_free(args):
    if args.duration <= 0:
        print("Error: --duration must be a positive integer number of minutes.", file=sys.stderr)
        sys.exit(1)

    day = validate_day(args.day) if args.day is not None else None
    start_min = parse_time(args.start) if args.start is not None else None
    end_min = parse_time(args.end) if args.end is not None else None

    if start_min is not None and end_min is not None and start_min >= end_min:
        print(
            f"Error: --start ({args.start}) must be earlier than --end ({args.end}).",
            file=sys.stderr,
        )
        sys.exit(1)

    data = load_timetable(args.file)
    slots = find_free_slots(
        data,
        duration_minutes=args.duration,
        days=day,
        start=args.start,
        end=args.end,
    )
    render_free_slots(
        slots,
        duration_minutes=args.duration,
        days=day,
        start=args.start,
        end=args.end,
    )


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
    """Write *data* to *filepath* atomically using :func:`timetable.loader.save_timetable`."""
    save_timetable(data, filepath)


def _write_timetable_with_snapshot(
    data: Dict[str, Any],
    *,
    filepath: Path,
    operation: str,
    previous: Dict[str, Any],
    expected_revision: Optional[str] = None,
    lock_timeout: Optional[float] = None,
) -> str:
    """Snapshot *previous*, then atomically write *data* to *filepath*.

    The snapshot records the timetable as it was *before* the mutation, so
    restoring it undoes the change.  It is taken only after the caller has
    validated the new data (i.e. when a write is genuinely about to happen),
    and the write is atomic so a failure never leaves a partially written
    timetable behind.  Returns the snapshot id.
    """
    with TimetableFileLock(filepath, timeout=lock_timeout):

        TransactionCoordinator(filepath, lock_timeout=lock_timeout).recover_if_needed()


        if expected_revision is not None:
            snap = load_timetable_snapshot(filepath, validate=False)
            if snap.revision != expected_revision:
                raise ConcurrentModificationError(
                    filepath,
                    expected_revision=expected_revision,
                    current_revision=snap.revision,
                )
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
        active_snap = load_timetable_snapshot(args.file, validate=False)
        result = restore_snapshot(
            args.snapshot_id,
            filepath=Path(args.file),
            history_dir=history_dir,
            expected_revision=active_snap.revision,
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


def cmd_undo(args):
    """Undo the latest recorded mutation of the active timetable."""
    try:
        entry = undo_last(Path(args.file))
    except HistoryFileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Undid {entry['operation']} on '{args.file}' "
        f"(recorded {entry['at']})."
    )
    return 0


def cmd_redo(args):
    """Redo the latest undone mutation of the active timetable."""
    try:
        entry = redo_last(Path(args.file))
    except HistoryFileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Redid {entry['operation']} on '{args.file}' "
        f"(recorded {entry['at']})."
    )
    return 0


def cmd_undo_history(args):
    """List recorded undo entries (operation type and timestamp only)."""
    try:
        journal = load_journal(Path(args.file))
    except HistoryFileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    entries = journal["undo"]
    if not entries:
        print("No recorded changes.")
        return 0
    for entry in reversed(entries):
        print(f"{entry['id']}  {entry['at']}  {entry['operation']}")
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

    snapshot = load_timetable_snapshot(args.file)
    data = snapshot.data
    previous_state = copy.deepcopy(data)
    if day not in data:
        data[day] = []

    candidate = {
        "subject": args.subject,
        "start": args.start,
        "end": args.end,
        "room": args.room,
    }
    clashing_slot = find_clashing_slot(data, day, candidate)
    if clashing_slot:
        clash_name = clashing_slot.get("subject", "Existing class")
        clash_time = f"{clashing_slot.get('start')} - {clashing_slot.get('end')}"
        print(
            f"Error: Class overlaps with '{clash_name}' ({clash_time}) on {day.capitalize()}.",
            file=sys.stderr
        )
        sys.exit(1)

    # Validate mutated candidate against unified validation engine
    candidate_timetable = copy.deepcopy(data)
    candidate_timetable[day] = copy.deepcopy(data[day])
    candidate_timetable[day].append(candidate)
    report = validate_timetable(candidate_timetable, source_path=args.file)
    if not report.is_valid:
        error_msgs = [d.message for d in report.errors]
        print(f"Error: {error_msgs[0]}", file=sys.stderr)
        sys.exit(1)

    data[day].append(candidate)
    data[day].sort(key=lambda s: s.get("start", ""))

    # Snapshot the timetable as it was *before* this addition (kept for the
    # existing `history` browser), then record the mutation transactionally:
    # the shared helper validates the proposed state, writes the undo journal
    # first, then atomically replaces the timetable.  Validation above already
    # succeeded, so the write is genuinely attempted here.
    _save_snapshot_before_file_write(
        previous_state,
        filepath=Path(args.file),
        operation="add",
    )
    try:
        commit_mutation(
            Path(args.file),
            "add",
            data,
            canonicalize=True,
            expected_revision=snapshot.revision,
        )
    except (HistoryFileError, NoOpError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
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

    if getattr(args, "json", False):
        report = validate_timetable(data, source_path=str(target))
        print(json.dumps(report.to_dict(), indent=2))
        sys.exit(1 if report.errors else 0)

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
        snapshot = load_timetable_snapshot(args.file, validate=False)
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
        commit_mutation(
            Path(args.file),
            "import-csv",
            updated,
            expected_revision=snapshot.revision,
        )
    except (HistoryFileError, NoOpError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(render_plan(plan))
    print(f"Successfully imported {len(plan.new_classes)} class(es) into '{args.file}'.")
    sys.exit(0)


def cmd_import_ics(args):
    """Imports classes from an iCalendar (.ics) file with recurrence expansion."""
    from_date = None
    to_date = None

    if args.from_date:
        try:
            from_date = datetime.date.fromisoformat(args.from_date)
        except ValueError:
            print(f"Error: Invalid --from date '{args.from_date}'; expected YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)

    if args.to_date:
        try:
            to_date = datetime.date.fromisoformat(args.to_date)
        except ValueError:
            print(f"Error: Invalid --to date '{args.to_date}'; expected YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)

    # Defaults: if not specified, default to the current semester/year or require explicit dates
    # When omitted, default to today and 90 days ahead
    if from_date is None:
        from_date = datetime.date.today()
    if to_date is None:
        to_date = from_date + datetime.timedelta(days=90)

    if to_date < from_date:
        print(
            f"Error: --to date ({to_date}) cannot be earlier than --from date ({from_date}).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        snapshot = load_timetable_snapshot(args.file)
        timetable_data = load_timetable_for_import(args.file)
        plan = plan_ics_import(
            args.ics_file,
            timetable_data,
            from_date=from_date,
            to_date=to_date,
            timezone_name=args.timezone,
        )
    except ImportError_ as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not plan.accepted:
        print(render_ics_plan(plan, dry_run=args.dry_run), file=sys.stderr)
        if plan.invalid_records:
            print("Error: Could not import ICS due to validation errors.", file=sys.stderr)
        else:
            print("Error: Could not import ICS due to scheduling conflicts.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(render_ics_plan(plan, dry_run=True))
        print("Dry run complete. No files were modified.")
        sys.exit(0)

    # Convert plan to timetable data structure using apply_import
    # IcsImportPlan new_classes is in the same format: (line, day, slot)
    updated = apply_import(timetable_data, plan)
    previous_state = copy.deepcopy(timetable_data)

    target_file = Path(args.file)
    try:
        _write_timetable_with_snapshot(
            updated,
            filepath=target_file,
            operation="import-ics",
            previous=previous_state,
            expected_revision=snapshot.revision,
        )
    except OSError as exc:
        print(f"Error: Could not write timetable '{args.file}': {exc}", file=sys.stderr)
        sys.exit(1)

    print(render_ics_plan(plan))
    print(f"Successfully imported {len(plan.new_classes)} class(es) into '{args.file}'.")
    sys.exit(0)


def cmd_merge(args):
    """Merges two or three timetable JSON files with conflict detection."""
    # Determine base, ours, theirs or first, second
    files = list(args.files) if hasattr(args, "files") and args.files else []
    if not files:
        if getattr(args, "first", None) and getattr(args, "second", None):
            files = [args.first, args.second]

    base_arg = getattr(args, "base", None)
    is_three_way = False

    if base_arg:
        is_three_way = True
        base_file = base_arg
        if len(files) != 2:
            print("Error: When --base is provided, exactly two modified timetable files ('ours' and 'theirs') must be specified.", file=sys.stderr)
            sys.exit(1)
        ours_file, theirs_file = files[0], files[1]
    elif len(files) == 3:
        is_three_way = True
        base_file, ours_file, theirs_file = files[0], files[1], files[2]
    elif len(files) == 2:
        is_three_way = False
        first_file, second_file = files[0], files[1]
    else:
        print("Error: 'merge' requires either two files (two-way merge) or three files (base, ours, theirs for three-way merge).", file=sys.stderr)
        sys.exit(1)

    try:
        if is_three_way:
            input_paths = {
                "base": Path(base_file).resolve(),
                "ours": Path(ours_file).resolve(),
                "theirs": Path(theirs_file).resolve(),
            }
        else:
            input_paths = {
                "first": Path(first_file).resolve(),
                "second": Path(second_file).resolve(),
            }
    except Exception as exc:
        print(f"Error resolving paths: {exc}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output).resolve() if args.output else None

    if output_path:
        for role, p in input_paths.items():
            if output_path == p:
                print(
                    f"Error: Output file '{args.output}' cannot be the same as input file '{p}'.",
                    file=sys.stderr,
                )
                sys.exit(1)
        if output_path.exists() and not args.force:
            print(
                f"Error: Output file '{args.output}' already exists. Use --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)
    # If destination specified, capture its initial revision before loading sources
    output_file = Path(output_path) if output_path else None
    expected_dest_snap = load_timetable_snapshot(output_file, validate=False) if output_file else None

    # Validate input files exist and are valid timetables, capturing source revisions
    source_snapshots = {}
    try:
        for role, p in input_paths.items():
            # load_timetable_for_merge raises MergeError for missing files, invalid JSON, or schema errors
            load_timetable_for_merge(p)
            source_snapshots[p] = load_timetable_snapshot(p, validate=True)
    except MergeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, SchemaError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Error reading input: {exc}", file=sys.stderr)
        sys.exit(1)

    if is_three_way:
        base_data = source_snapshots[input_paths["base"]].data
        ours_data = source_snapshots[input_paths["ours"]].data
        theirs_data = source_snapshots[input_paths["theirs"]].data
    else:
        first_data = source_snapshots[input_paths["first"]].data
        second_data = source_snapshots[input_paths["second"]].data

    if is_three_way:
        result = merge_three_way(base_data, ours_data, theirs_data)
        if result.has_conflicts:
            print(render_three_way_conflicts(result), file=sys.stderr)
            print("Error: Could not merge timetables due to conflicts.", file=sys.stderr)
            sys.exit(1)
        merged_data = result.merged_data
    else:
        merged_data, conflicts = merge_timetables(first_data, second_data)
        if conflicts:
            print(render_merge_conflicts(conflicts), file=sys.stderr)
            print("Error: Could not merge timetables due to scheduling conflicts.", file=sys.stderr)
            sys.exit(1)

    if output_path:
        # Collect all files to lock in deterministic order: all sources + destination
        all_lock_paths = list(input_paths.values()) + [output_file]

        try:
            with MultiFileLock(all_lock_paths):
                # 1. Revalidate all source revisions under the lock
                for p, initial_snap in source_snapshots.items():
                    current_snap = load_timetable_snapshot(p, validate=False)
                    if current_snap.revision != initial_snap.revision:
                        raise ConcurrentModificationError(
                            p,
                            expected_revision=initial_snap.revision,
                            current_revision=current_snap.revision,
                        )

                # 2. Revalidate destination revision under the lock
                dest_current_snap = load_timetable_snapshot(output_file, validate=False)
                if dest_current_snap.revision != expected_dest_snap.revision:
                    raise ConcurrentModificationError(
                        output_file,
                        expected_revision=expected_dest_snap.revision,
                        current_revision=dest_current_snap.revision,
                    )

                # 3. Snapshot destination for history if it exists
                if output_file.exists():
                    try:
                        previous_state = load_timetable(output_file)
                    except (OSError, ValueError, SchemaError):
                        previous_state = None
                    if previous_state is not None:
                        _save_snapshot_before_file_write(
                            previous_state,
                            filepath=output_file,
                            operation="merge",
                        )

                # 4. Commit mutation to destination
                # Note: TimetableFileLock is reentrant across thread calls so commit_mutation
                # will re-acquire destination lock safely.
                try:
                    commit_mutation(
                        output_file,
                        "merge",
                        merged_data,
                        expected_revision=expected_dest_snap.revision,
                    )
                except (HistoryFileError, NoOpError) as exc:
                    # If NoOpError, save_timetable directly to ensure output file is present
                    save_timetable(merged_data, output_file)
        except ConcurrencyError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"Successfully merged timetables into '{args.output}'.")
    else:
        print("Successfully merged timetables (dry run, no --output specified).")

    sys.exit(0)


def cmd_optimize(args):
    """Optimizes the weekly timetable according to configurable constraints and objectives."""
    timetable_file = Path(args.file)
    if not timetable_file.exists():
        print(f"Error: Timetable file not found: {timetable_file}", file=sys.stderr)
        sys.exit(1)

    # 1. Load configuration
    try:
        if args.config:
            config = load_optimization_config(args.config)
        else:
            config = OptimizationConfig()

        # Command-line overrides
        if args.max_iterations is not None:
            if args.max_iterations < 1:
                raise ConfigurationError(f"max_iterations must be a positive integer, got {args.max_iterations}")
            config.max_iterations = args.max_iterations

        if args.seed is not None:
            config.seed = args.seed

        if args.fixed:
            for item in args.fixed:
                for sub in item.split(","):
                    if sub.strip():
                        config.fixed_classes.add(sub.strip().lower())

        if args.allowed_days:
            days = []
            for item in args.allowed_days:
                for d in item.split(","):
                    d_norm = d.strip().lower()
                    if d_norm not in WEEKDAYS:
                        raise ConfigurationError(f"Invalid weekday '{d}' in --allowed-days")
                    if d_norm not in days:
                        days.append(d_norm)
            config.allowed_days = days

        # Weights override
        w = config.weights
        new_gaps = args.weight_gaps if args.weight_gaps is not None else w.gaps
        new_span = args.weight_span if args.weight_span is not None else w.span
        new_balance = args.weight_balance if args.weight_balance is not None else w.balance
        new_changes = args.weight_changes if args.weight_changes is not None else w.changes
        config.weights = ObjectiveWeights(
            gaps=new_gaps,
            span=new_span,
            balance=new_balance,
            changes=new_changes,
        )
        config.validate()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Read active file content for stale check
    try:
        raw_initial_bytes = timetable_file.read_bytes()
    except OSError as exc:
        print(f"Error reading {timetable_file}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        source_data = json.loads(raw_initial_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON in {timetable_file}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(source_data, dict):
        print(f"Error: Timetable in {timetable_file} must be a JSON object", file=sys.stderr)
        sys.exit(1)

    # 2. Run optimization
    result = optimize_timetable(source_data, config)

    # 3. Print report
    print("\n=== Timetable Optimization Report ===")
    print(f"Search Status: {result.search_status}")
    print(f"Iterations Evaluated: {result.iterations_evaluated}")
    print(f"Optimality Proven: {'Yes' if result.is_optimal_proven else 'No'}")
    print(f"\nOriginal {result.original_score.format_summary()}")
    print(f"Proposed {result.proposed_score.format_summary()}")

    if result.changes:
        print(f"\nProposed Changes ({len(result.changes)}):")
        for ch in result.changes:
            print(
                f"  • {ch.subject}: {ch.original_day} {ch.original_start}-{ch.original_end} -> "
                f"{ch.new_day} {ch.new_start}-{ch.new_end}"
            )
            print(f"    Reason: {ch.reason}")
    else:
        print("\nNo schedule changes proposed.")

    if result.search_status == "infeasible":
        print(f"\nError: {result.message}", file=sys.stderr)
        sys.exit(1)

    # 4. Handle persistence if --apply was requested
    if args.apply:
        if not result.has_improvement:
            print("\nNo timetable improvement to apply. File remains unchanged.")
            sys.exit(0)

        # Stale result protection: check if timetable on disk changed while optimization ran
        try:
            current_bytes = timetable_file.read_bytes()
        except OSError as exc:
            print(f"Error: Could not re-read {timetable_file}: {exc}", file=sys.stderr)
            sys.exit(1)

        if current_bytes != raw_initial_bytes:
            print(
                f"Error: Timetable file '{timetable_file}' was modified concurrently. "
                "Rejecting stale optimization result.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Validate proposed timetable against mandatory constraints
        valid, err_msg = is_schedule_valid(result.proposed_timetable, config)
        if not valid:
            print(f"Error: Proposed timetable failed final validation: {err_msg}", file=sys.stderr)
            sys.exit(1)

        # Re-validate structure
        try:
            validate_timetable_structure(result.proposed_timetable, str(timetable_file))
        except MergeError as exc:
            print(f"Error: Proposed timetable structure is invalid: {exc}", file=sys.stderr)
            sys.exit(1)

        # Snapshot and atomic write
        try:
            _write_timetable_with_snapshot(
                result.proposed_timetable,
                filepath=timetable_file,
                operation="optimize",
                previous=source_data,
            )
        except Exception as exc:
            print(f"Error saving optimized timetable: {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"\nSuccessfully applied optimization to '{timetable_file}'.")
    else:
        if args.dry_run:
            print("\nDry run complete. No files were modified.")
        else:
            print("\nPreview complete. To apply these changes, rerun with --apply.")

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

    # free
    p_free = subparsers.add_parser(
        "free",
        help="Find free intervals that fit a requested duration",
    )
    p_free.add_argument(
        "--duration",
        type=int,
        required=True,
        help="Minimum length of a free interval to report, in minutes (positive integer)",
    )
    p_free.add_argument("--day", help="Restrict the search to one weekday (e.g. monday, tuesday)")
    p_free.add_argument("--start", help="Working window start time (HH:MM)")
    p_free.add_argument("--end", help="Working window end time (HH:MM)")
    p_free.set_defaults(func=cmd_free)

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
    p_conflicts.add_argument(
        "--json",
        action="store_true",
        help="Output diagnostics in structured JSON format",
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

    # import-ics
    p_import_ics = subparsers.add_parser(
        "import-ics",
        help="Import classes from an iCalendar (.ics) file with recurrence expansion",
    )
    p_import_ics.add_argument("ics_file", help="Path to the .ics file to import")
    p_import_ics.add_argument(
        "--from",
        dest="from_date",
        help="Start date for recurrence expansion (YYYY-MM-DD, inclusive, default: today)",
    )
    p_import_ics.add_argument(
        "--to",
        dest="to_date",
        help="End date for recurrence expansion (YYYY-MM-DD, inclusive, default: 90 days from start)",
    )
    p_import_ics.add_argument(
        "--timezone",
        help="Target IANA timezone (e.g. Asia/Kolkata, UTC) for converting timestamps",
    )
    p_import_ics.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview the import without modifying the timetable",
    )
    p_import_ics.set_defaults(func=cmd_import_ics)

    # merge
    p_merge = subparsers.add_parser(
        "merge",
        help="Merge two or three timetable JSON files with conflict detection",
    )
    p_merge.add_argument(
        "files",
        nargs="+",
        help="Timetable files to merge. Specify two files (first second) or three files (base ours theirs).",
    )
    p_merge.add_argument(
        "--base",
        help="Path to common base timetable file (optional alternative for three-way merge: --base <base> <ours> <theirs>).",
    )
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

    # optimize
    p_optimize = subparsers.add_parser(
        "optimize",
        help="Optimize timetable schedule to minimize gaps, span, and balance workload",
    )
    p_optimize.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to JSON optimization configuration file",
    )
    p_optimize.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview proposed optimization changes without modifying files",
    )
    p_optimize.add_argument(
        "--apply",
        action="store_true",
        help="Apply and safely persist proposed optimization changes to the timetable",
    )
    p_optimize.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Maximum search iterations (default: 1000)",
    )
    p_optimize.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for deterministic optimization (default: 42)",
    )
    p_optimize.add_argument(
        "--fixed",
        action="append",
        help="Subject names to keep fixed (comma-separated or multiple flags)",
    )
    p_optimize.add_argument(
        "--allowed-days",
        action="append",
        help="Weekdays eligible for class scheduling (comma-separated or multiple flags)",
    )
    p_optimize.add_argument(
        "--weight-gaps",
        type=float,
        default=None,
        help="Weight for daily gaps minimization objective",
    )
    p_optimize.add_argument(
        "--weight-span",
        type=float,
        default=None,
        help="Weight for daily span minimization objective",
    )
    p_optimize.add_argument(
        "--weight-balance",
        type=float,
        default=None,
        help="Weight for workload balance objective",
    )
    p_optimize.add_argument(
        "--weight-changes",
        type=float,
        default=None,
        help="Weight for schedule change minimization penalty",
    )
    p_optimize.set_defaults(func=cmd_optimize)

    # undo
    p_undo = subparsers.add_parser(
        "undo",
        help="Undo the latest recorded change to the timetable",
    )
    p_undo.set_defaults(func=cmd_undo)

    # redo
    p_redo = subparsers.add_parser(
        "redo",
        help="Redo the latest undone change to the timetable",
    )
    p_redo.set_defaults(func=cmd_redo)

    # undo-history
    p_undo_history = subparsers.add_parser(
        "undo-history",
        help="List recorded undo entries for the timetable",
    )
    p_undo_history.set_defaults(func=cmd_undo_history)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        result = args.func(args)
    except SchemaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ConcurrentModificationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except LockTimeoutError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if isinstance(result, int) and result != 0:
        sys.exit(result)


if __name__ == "__main__":
    main()
