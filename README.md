# timetable-cli

Your weekly class timetable in the terminal. A command-line tool in Python with cross-process concurrency safety. The timetable is saved in `timetable.json`.

## Installation & Running it

You need Python 3.8 or newer. Install dependencies using `requirements.txt`:

```
pip install -r requirements.txt
```

Run commands with:

```
python3 -m timetable show monday
python3 -m timetable week
python3 -m timetable now
python3 -m timetable export
python3 -m timetable filter --day monday --start 09:00 --end 12:00
python3 -m timetable free --duration 60
python3 -m timetable free --duration 120 --day monday --start 09:00 --end 17:00
python3 -m timetable add --day friday --subject "Maths" --start 09:00 --end 10:00 --room "Room 101"
python3 -m timetable import-csv semester.csv --dry-run
python3 -m timetable import-csv semester.csv
python3 -m unittest discover tests
```


On Windows, use `python` instead of `python3`.

## Commands

| Command | Does |
|---|---|
| `show <day>` | One day's classes |
| `week` | The whole week, Monday to Sunday |
| `now` | What's on right now, and what's next |
| `export [--output FILE]` | Writes a `timetable.ics` calendar file |
| `filter [--day --start --end --room]` | Classes matching a day, time range and/or room |
| `free --duration N [--day --start --end]` | Free time slots at least N minutes long |
| `add --day --subject --start --end --room` | Adds a class |
| `diff <first> <second>` | Compares two timetable files and reports what changed |
| `conflicts [file]` | Detects overlapping class schedules on the same day |
| `merge <first> <second> [--output OUT] [--force]` | Combines two timetables into one with conflict detection |
| `import-csv <file> [--dry-run]` | Imports classes from a CSV file with validation, duplicate detection and conflict checks |
| `optimize [--config FILE] [--dry-run] [--apply]` | Optimizes schedule to reduce daily gaps, span, and balance workload |
| `import-ics <file> [--from --to --timezone --dry-run]` | Imports classes from an iCalendar (.ics) file with recurrence expansion, duplicate detection and conflict checks |
| `history [list\|show\|restore]` | Inspects timetable snapshots and restores previous states |
| `restore <snapshot-id>` | Restores the active timetable directly from a snapshot |
| `undo` | Undoes the most recent timetable change |
| `redo` | Redoes the most recently undone timetable change |
| `undo-history` | Lists recorded undo entries for the timetable |
| `stats [file]` | Computes and displays aggregate statistics for a timetable |




## How it's supposed to work

- Classes are always listed in order of start time, however they were added.
- Day names ignore capital letters: `Monday`, `monday` and `MONDAY` are the same. A name that isn't a day, like `mondy`, is an error that lists the valid days, for both `show` and `add`.
- Times are 24-hour `HH:MM`. `add` refuses a time that doesn't exist, like `25:99`, and a class that ends before it starts.
- `add` refuses a class that overlaps another one on the same day, and says which one it clashes with.
- Each class shows how long it is, in minutes.

## What's on now

```
python3 -m timetable now
```

Looks at today's date, works out which class is running at this moment, and which one starts next, with a countdown in minutes. If nothing is running it says so, and if the day is finished it says that too.

## Exporting to a calendar

```
python3 -m timetable export
python3 -m timetable export --output timetable.ics
```

Writes an `.ics` file (default name `timetable.ics`) containing every class as a weekly recurring event, so you can import your timetable into Google Calendar, Apple Calendar, Outlook and the like. Each class becomes one `VEVENT` with the subject as the title and the room as the location, repeating every week.

Because `timetable.json` only stores a weekday and a time rather than a date, each weekday is mapped onto a fixed reference date in one week, and that date carries the weekly repeat rule. The same timetable always exports the same file.

## Filtering

```
python3 -m timetable filter --day monday --start 09:00 --end 12:00
python3 -m timetable filter --room "Lab 1"
python3 -m timetable filter --day monday --room "Lab 1" --start 09:00 --end 14:00
```

`filter` shows only the classes matching `--day`, `--start`/`--end` (a time range) and/or `--room`. Every option is optional and they combine: only the classes that satisfy all the given filters are shown. The time range is half-open, so a class matches when it overlaps the range; a class that only touches a boundary (for example one ending exactly at `--start`, or starting exactly at `--end`) is not included. `--day` ignores capital letters and `--room` matches exactly. If nothing matches it just says so.

## Finding free time

```bash
python3 -m timetable free --duration 60
python3 -m timetable free --duration 120 --day monday --start 09:00 --end 17:00
python3 -m timetable free --duration 30 --day MONDAY
```

`free` finds every stretch of the day when you have nothing scheduled that is at least `--duration` minutes long.

- `--duration N` is required and must be a positive whole number of minutes; `0` or a negative value is an error.
- `--day` restricts the search to one weekday (capital letters ignored). Without it, all seven days are searched in canonical order, Monday to Sunday.
- `--start` and `--end` set the working window to search within, as 24-hour `HH:MM` times. They default to the full day (`00:00` to `24:00`). `--start` must be earlier than `--end`, and a malformed or non-existent time like `9:30` or `25:99` is an error.
- Consecutive and overlapping classes are merged into one busy block, so back-to-back classes never leave a phantom gap.
- Results are printed per day in canonical weekday order, and each day is ordered by start time. Every slot shows its day, start and end time, and its length in minutes.
- If no interval of the requested length exists, it prints a friendly message and exits with code `0` — that is a valid result, not an error.
- A missing timetable file is treated as an empty timetable, so `free` reports the whole working window rather than failing.

## Importing classes from CSV

```bash
python3 -m timetable import-csv semester.csv
python3 -m timetable import-csv semester.csv --dry-run
```

`import-csv` reads a CSV file and adds many classes to the timetable in one step. Use the existing global `--file` option if the timetable is not the default `timetable.json`, for example `python3 -m timetable --file timetable.json import-csv semester.csv`.

The CSV must have a header row with these five columns:

```csv
day,subject,start,end,room
```

Column names are matched ignoring capital letters and surrounding spaces, so `Day`, `DAY` and `day` are all accepted. A realistic file looks like this:

```csv
day,subject,start,end,room
Monday,Data Structures,09:00,10:30,Lab 1
Monday,Mathematics,11:00,12:00,Room 101
Tuesday,Operating Systems,14:00,15:00,Room 204
```

The whole file is checked before anything is written:

- Required columns must all be present, and duplicate headers are rejected.
- Every value must be filled in, weekday names must be real days, and times must be valid 24-hour `HH:MM` values.
- A class must end after it starts, like `add`.
- Invalid records are reported with their CSV row number and a specific message, and several errors are collected together when there is more than one.
- An empty file, or a file with only the header row, is treated as an import with nothing to add.

Duplicate handling:

- A class is a duplicate when its day, subject, start time, end time and room all match an existing class, either elsewhere in the CSV or already in the timetable.
- Duplicates inside the CSV are skipped after the first identical row, so repeating a row does not create repeated timetable entries.
- Classes already in the timetable are skipped too, and the skipped entries are listed in the summary.
- The same subject at a different time, or with a different room, is a distinct class and is imported normally.

Conflict handling:

- After duplicates are removed, any two overlapping classes on the same day conflict. Intervals are half-open, so `09:00-10:00` and `10:00-11:00` do not conflict, while `09:00-10:30` and `10:00-11:00` do.
- Conflicts are checked both between new CSV rows and between new rows and classes already in the timetable. Classes on different days never conflict.
- Each conflicting pair is reported once, showing both classes with their day, time, subject and room. Any conflict rejects the entire import, so nothing is added or removed.

Dry run and safety:

- `--dry-run` performs the same parsing, validation, duplicate and conflict checks as a real import, prints the same summary, and exits without writing anything.
- A rejected import, whether from validation errors or conflicts, leaves `timetable.json` exactly as it was.
- A successful import writes the file atomically: it writes a temporary file in the same directory and replaces the timetable only after the write succeeds. If writing fails, the temporary file is cleaned up and the original timetable is left untouched.
- Accepted classes are appended to their day and that day is re-sorted by start time, like the rest of the CLI.
- Exit code `0` means the import or dry run succeeded; any validation error, conflict, missing file, unreadable timetable JSON or write failure returns a non-zero exit code and never reports success.

## Importing classes from iCalendar (ICS)

```bash
python3 -m timetable import-ics semester.ics
python3 -m timetable import-ics semester.ics --dry-run
python3 -m timetable import-ics semester.ics --from 2026-06-01 --to 2026-10-31 --timezone Asia/Kolkata
```

`import-ics` parses an iCalendar (`.ics`) file (RFC 5545), expands weekly recurring events over a finite date window, maps occurrences to weekly timetable entries, deduplicates against existing classes, checks for scheduling conflicts, and atomically imports them into `timetable.json`.

Date range semantics:

- `--from <YYYY-MM-DD>`: Start date of the recurrence expansion window (inclusive, defaults to today).
- `--to <YYYY-MM-DD>`: End date of the recurrence expansion window (inclusive, defaults to 90 days after `--from`).
- Recurrence instances falling within `[--from, --to]` are expanded into weekday/time entries.
- If `--to` is earlier than `--from`, the import is rejected with an actionable error.

Timezone support:

- `--timezone <TZ>`: Target IANA timezone (e.g., `Asia/Kolkata`, `Europe/London`, `UTC`).
- Timestamps with `Z` (UTC) or `TZID` parameters are deterministically converted into the target timezone.
- Floating/local timestamps are interpreted in the specified target timezone.
- Daylight saving transitions (DST) are handled deterministically without producing shifted or invalid occurrences.

Recurrence support and limitations:

- Supports standard `VEVENT` components with `UID`, `SUMMARY`, `LOCATION`, `DTSTART`, and `DTEND` or `DURATION`.
- Supports weekly recurrence via `RRULE`: `FREQ=WEEKLY`, with `BYDAY`, `INTERVAL`, `COUNT`, and `UNTIL`.
- Supports explicit inclusion via `RDATE` and exclusion via `EXDATE`.
- Non-weekly frequencies (such as `FREQ=DAILY`, `FREQ=MONTHLY`, or `FREQ=YEARLY`) cannot be represented in a weekly timetable and are rejected with an explanatory error.
- All-day events (date-only `VALUE=DATE`) are rejected because the weekly timetable requires timed `HH:MM` intervals.
- Cancelled events (`STATUS:CANCELLED`) are cleanly skipped.
- Events with missing timestamps, zero or negative duration, or durations exceeding 24 hours are rejected.
- Events crossing midnight (e.g. 23:00 to 01:00) are mapped consistently to the start weekday, matching the timetable overnight schema.

Duplicate and conflict handling:

- Multiple occurrences of the same weekly slot generated by recurrence expansion are deduplicated within the import.
- Classes already present in the target timetable are recognized and skipped as duplicates.
- All non-duplicate classes are checked for scheduling conflicts against each other and against existing timetable entries using half-open intervals `[start, end)`. Any collision halts the import with an informative report.

Atomic persistence and history:

- Atomicity is strictly guaranteed: if any event fails validation or any conflict is detected, the entire import is rejected.
- Successful imports take a version history snapshot before writing and replace the timetable atomically.
- `--dry-run` performs full parsing, expansion, validation, deduplication, and conflict detection without writing files or recording history.

## Comparing two timetables

```bash
python3 -m timetable diff timetable.json timetable-new.json
```

`diff` takes exactly two timetable JSON files and reports what changed between them. It prints three sections — `Added:` (classes only in the second file), `Removed:` (classes only in the first file) and `Modified:` (classes present in both but with changed fields, listed as `Field: old -> new`). Classes are matched by their day and subject rather than their position in the JSON, so reordering entries is never reported as a change. The output is sorted by weekday and start time.

```text
Timetable changes

Added:
  Tuesday 14:00-15:00 — Operating Systems — Room 204

Removed:
  Wednesday 10:00-11:00 — Mathematics — Room 101

Modified:
  Monday 09:00-10:30 — Data Structures
    Room: Lab 1 -> Lab 2
```

When the timetables are identical it prints `No changes detected. The timetables are identical.` The command exits with status `0` when the files match and non-zero when they differ or an input is missing/invalid, so it can be used in scripts. Neither input file is modified.

## Merging timetables

### Two-way merge

```bash
python3 -m timetable merge timetable.json timetable-new.json --output merged.json
```

Combines two timetable JSON files into a single merged timetable file without modifying either input file.

- **Duplicate handling**: Identical classes (same subject, start time, end time, and room on the same day) are deduplicated so only one instance is kept in the output.
- **Conflict detection**: Classes occurring on the same day that overlap in time (evaluated using half-open intervals `[start, end)`) are detected as conflicts. If conflicts exist, the operation halts with exit code `1`, prints all conflicting pairs clearly to stderr, and avoids producing a merged file.
- **Deterministic ordering**: The output merged timetable is deterministically sorted by canonical weekday order (Monday to Sunday) and start time.
- **Output file safety**: The output path cannot match any input path. If the output file already exists, `merge` will refuse to overwrite it unless `--force` is provided.

### Three-way merge with change-aware conflict detection

```bash
# Positional syntax: base, ours, theirs
python3 -m timetable merge base.json ours.json theirs.json --output merged.json

# Alternative flag syntax:
python3 -m timetable merge ours.json theirs.json --base base.json --output merged.json
```

Three-way merge reconciles concurrent edits made to a common base timetable across two independent branches (`ours` and `theirs`):

- **Purpose & Workflow**:
  - `base.json`: The common original timetable snapshot.
  - `ours.json`: First independently modified version.
  - `theirs.json`: Second independently modified version.
  - Computes modifications and deletions on both sides relative to `base`, then reconciles non-conflicting changes automatically into the destination.
- **Reconciliation Rules**:
  1. *Unchanged*: If neither side changed an entry, the base entry is retained.
  2. *Single-side change*: If only one side modified or deleted an entry, that change is applied.
  3. *Identical change*: If both sides made the same modification or both deleted an entry, the change is applied once cleanly.
  4. *Compatible field-level edits*: If one side modified the room and the other modified the time slot of the same class, the edits are merged compatibly.
  5. *Independent additions*: Distinct new classes added on either side are both included. Identical additions on both sides are deduplicated.
- **Conflict Types**:
  - **Merge conflicts (structural)**: Incompatible edits to the same field (`modify_vs_modify`), deletion on one side with modification on the other (`delete_vs_modify`), competing additions of the same class (`add_vs_add`), or ambiguous identity matches (`ambiguous_match`).
  - **Schedule conflicts (semantic)**: Structurally reconcilable additions or modifications that introduce overlapping time slots on the same day in the merged schedule.
- **Diagnostics & Failure Behavior**:
  - When merge conflicts or schedule conflicts occur, structured diagnostics describing affected entries, competing values, or overlapping slots are printed to `stderr`, and the process halts with exit code `1`.
  - The destination file is **never written or partially overwritten** if any conflict occurs.
  - Neither `base`, `ours`, nor `theirs` input files are ever modified.

### Concurrency Safety: Multi-File Locking and Stale-Source Prevention

To prevent stale-source merges when multiple files are involved in two-way or three-way merges:
- **Snapshot Capture**: Revisions (SHA-256 fingerprints) of all source inputs (including `base`, `ours`, `theirs` / `first`, `second`) and the destination file (or expected absence) are captured upon initial load.
- **Deterministic Multi-File Locking**: At the commit boundary, `MultiFileLock` acquires cross-process file locks for all source inputs and destination file simultaneously in a deterministic, globally consistent canonical path order (`os.path.normcase`). This eliminates lock-order deadlocks regardless of the order arguments were passed.
- **Deduplication & Failure Rollback**: Duplicate or equivalent file paths are deduplicated so files are not locked multiple times. If any lock cannot be acquired within the timeout, all previously acquired locks are immediately released before propagating the error.
- **Revalidation Under Lock**: Under the multi-file lock, all source revisions and destination revision are re-validated before writing. If any source file was modified concurrently or destination state diverged, the merge aborts with exit code `1` (`ConcurrentModificationError`) without writing or corrupting the destination.

## Version history and restore

Every command that successfully rewrites `timetable.json` (`add`, `merge --output`, and `restore`) first saves a snapshot of the timetable as it was *before* the change, under a `.timetable-history` directory next to the timetable file (for example `./.timetable-history/`). Snapshots are skipped entirely when a command fails validation, so a rejected `add` never creates one.

```bash
python3 -m timetable history list
python3 -m timetable history list --limit 5
python3 -m timetable history show snap-20260101120000000000-000000
python3 -m timetable history restore snap-20260101120000000000-000000
python3 -m timetable restore snap-20260101120000000000-000000
```

- **Listing**: `history list` prints each snapshot's id, creation time and operation (`add`, `merge`, `edit`, `restore-previous`) in newest-first order, skipping any snapshot whose files are missing or whose checksum no longer matches. `--limit` caps how many are shown (positive integer).
- **Inspecting**: `history show <id>` prints the snapshot's timetable in the usual day view without touching the active file.
- **Restoring**: `history restore <id>` (or the shorter `restore <id>`) replaces the active timetable with the snapshot. Before overwriting, the current contents of the active timetable are saved as a recoverable snapshot with operation `restore-previous`, so nothing is ever lost by restoring. A missing or corrupt snapshot raises an error and leaves the active timetable untouched. The write is atomic, so a failure mid-write cannot leave a truncated file.
- **Snapshot layout**: each snapshot is a directory `<id>/` containing `timetable.json` and `.metadata.json`. The metadata records the id, creation time, source timetable path, operation, and a SHA-256 checksum of the timetable bytes; `list`/`show`/`restore` verify the checksum before trusting the data.
## Undo and Redo

The CLI provides a transactional, snapshot-based undo and redo system for timetable modifications.

```bash
python3 -m timetable undo
python3 -m timetable redo
python3 -m timetable undo-history
```

Use `--file` to operate on a custom timetable path (e.g. `python3 -m timetable --file custom.json undo`).

### Supported Mutations

All timetable-modifying commands participate in transactional undo and redo:
- `add`: Adding a new class slot.
- `import-csv`: Importing classes from a CSV file.
- `merge --output <file>`: Merging timetables into an output file.

Read-only commands (`show`, `week`, `now`, `filter`, `diff`, `conflicts`, `stats`, `export`, `undo-history`) never create history entries or touch the undo/redo journal.

### Storage and Isolation

- Undo and redo history is stored separately from timetable files under `.timetable-undo/<timetable-filename>.undo-history.json` relative to the resolved timetable path.
- Each timetable file maintains its own isolated undo and redo journal. Custom timetable paths specified via `--file` resolve to their own independent journal, preventing accidental history mixing.
- History entries are validated and cross-referenced with the timetable's canonical path so a journal cannot redirect changes to an unrelated timetable file.
- Existing timetable JSON files require no migration or schema changes.

### Maximum History Size and Pruning

- Default limit: up to 50 undo entries are retained.
- Custom limit: override by setting the `TIMETABLE_UNDO_LIMIT` environment variable to a positive integer (e.g. `TIMETABLE_UNDO_LIMIT=20`).
- Pruning policy: when the history limit is exceeded, the oldest undo entries are pruned first, preserving the most recent undo/redo transition chain and semantics.

### Branching and Redo Invalidation

- Undoing an operation moves the state transition to the redo stack.
- Redoing an undone operation restores the target state and moves the entry back to the undo stack.
- Applying any new successful mutation after one or more undo operations automatically clears the redo stack.
- Failed mutations or no-op changes (mutations that produce identical content) leave the redo stack untouched.

### Crash-Recoverable Transactions and Error Recovery

Timetable modifications are executed with durable, crash-recoverable transactions managed by 	imetable.transaction.TransactionCoordinator. This eliminates inconsistencies between timetable data, undo/redo journals, and history snapshots in the event of abrupt process crashes, SIGKILL, or unexpected termination.

#### Protocol States & Two-Phase Lifecycle

Transactions progress through five explicit, monotonic states:
1. PREPARING: A unique transaction directory (.timetable-transactions/txn-<timestamp>-<uuid>) is created. Target replacement payloads (such as 	imetable.json and .timetable-undo/*.json) are staged to disk with sync.
2. PREPARED: Staged payloads, expected target revision SHA-256 digests, and payload checksums are durably recorded in 	ransaction.txn.json.
3. COMMIT_DECIDED: The commit decision boundary is crossed. The journal state is atomically updated and synced. **Once in this state, recovery will *always* roll forward.** If interrupted prior to COMMIT_DECIDED, crash recovery will abort and discard staged files without altering targets.
4. COMMITTED: All staged replacements have been applied to their final destinations.
5. CLEANED: Staging files and the transaction journal have been safely unlinked.

#### Automatic Crash Recovery

- **Automatic Trigger**: Any timetable load (load_timetable, load_timetable_raw, load_timetable_snapshot), save, or mutation command automatically inspects the .timetable-transactions directory under TimetableFileLock.
- **Durable Roll-Forward**: If an interrupted transaction is in COMMIT_DECIDED or COMMITTED, recovery validates staged checksums and applies target replacements idempotently, completing the mutation.
- **Safe Abort & Cleanup**: If an interrupted transaction was only in PREPARING or PREPARED, recovery verifies target revisions have not diverged, aborts the uncommitted transaction, and removes staged artifacts.
- **Conflict & Corruption Safety**: If an interrupted transaction encounters a target modified externally to an unexpected revision, recovery raises TransactionConflictError rather than blindly clobbering newer data. Malformed journals raise TransactionCorruptError for operator inspection.
- **Operating-System Level Testing**: Tested under forced termination (os._exit), fault-injected crashes across all stage boundaries, repeated crash recovery, corrupt journals, and concurrent execution.

## Timetable Optimization
 
```bash
python3 -m timetable optimize --dry-run
python3 -m timetable optimize --apply
python3 -m timetable optimize --config opt_config.json --dry-run
```

`optimize` uses a constraint-based search algorithm with configurable multi-objective scoring to improve timetable compactness, eliminate idle gaps, and balance workload across eligible weekdays.

### Objectives and Scoring

The optimizer scores candidates using a composite objective function (lower score is better):

1. **Minimize daily gaps**: Reduces idle time between consecutive classes on the same day.
2. **Minimize daily span**: Reduces the total elapsed duration from the first class's start to the last class's end.
3. **Balance workload**: Distributes class counts evenly across eligible weekdays by minimizing count variance.
4. **Minimize changes**: Penalizes moving classes to different days or shifting times unnecessarily, ensuring improvements justify any alterations.

Default weights: `gaps=1.0`, `span=0.5`, `balance=20.0`, `changes=15.0`. These can be overridden via CLI flags (`--weight-gaps`, `--weight-span`, `--weight-balance`, `--weight-changes`) or a configuration file.

### Hard Constraints vs Preferences

Mandatory constraints are strictly enforced before any candidate is scored:
- **No overlapping classes**: Two classes can never overlap in time on the same weekday (including overnight interval handling).
- **Fixed / immovable classes**: Classes specified as fixed (via `--fixed "Math,Physics"` or in config) remain untouched in day, start time, and end time.
- **Allowed weekdays**: Classes are only scheduled on eligible weekdays (via `--allowed-days` or config).
- **Availability windows & prohibited ranges**: Configurable per-weekday time limits and blocked intervals (e.g. lunch periods).
- **Class identity & duration**: Every class preserves its duration, subject, room, and metadata.

### Configuration Format

An optional JSON configuration file can define weights, fixed subjects, allowed days, and constraints:

```json
{
  "weights": {
    "gaps": 1.0,
    "span": 0.5,
    "balance": 20.0,
    "changes": 15.0
  },
  "fixed_classes": ["Mathematics"],
  "allowed_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
  "day_availability": {
    "Monday": ["09:00", "17:00"]
  },
  "prohibited_ranges": {
    "Monday": [["12:00", "13:00"]]
  },
  "max_iterations": 1000,
  "seed": 42
}
```

### Safety, Dry-Run, and Stale Result Protection

- **Dry-run**: By default or with `--dry-run`, `optimize` previews proposed improvements with per-entry change explanations without writing to disk or creating history entries.
- **Explicit application**: Changes are persisted only when `--apply` is specified.
- **Stale result rejection**: If the timetable file was modified concurrently while the optimizer was running, the proposal is safely aborted without overwriting.
- **Atomic persistence and history**: Applied optimizations are written atomically and recorded as an `optimize` snapshot in `.timetable-history/`.

## Timetable Schema & Migrations

`timetable-cli` uses a versioned JSON schema (current version: `1`) with strict schema validation and an automated migration framework.

### Schema Version 1

Canonical versioned timetable documents specify `schema_version: 1` and map weekdays to class entries:

```json
{
  "schema_version": 1,
  "monday": [
    {
      "subject": "Mathematics",
      "start": "09:00",
      "end": "10:30",
      "room": "Room 101"
    }
  ],
  "tuesday": [],
  "wednesday": [],
  "thursday": [],
  "friday": [],
  "saturday": [],
  "sunday": [],
  "metadata": {}
}
```

Both flat root weekdays and nested `{"schema_version": 1, "timetable": { ... }}` representations are supported.

### Backward Compatibility & Migration-on-Write Policy

- **Read operations** (`show`, `week`, `now`, `export`, `filter`, `diff`, `stats`, `conflicts`): Transparently normalize legacy unversioned files in-memory without rewriting them to disk.
- **Mutating operations** (`add`, `merge`, `restore`): Safely migrate valid legacy files to version 1 upon saving using atomic file writes.
- **Safety**: Malformed data or future unsupported schema versions (e.g. `schema_version: 99`) are rejected with clear, actionable validation error paths (`Invalid timetable at <path>: <message>`) and never overwritten or silently coerced.

## Concurrency Safety & Optimistic Locking

`timetable-cli` implements robust cross-process concurrency safety and optimistic locking to prevent concurrent CLI executions from silently overwriting one another's updates.

### Cross-Process File Locking

- Every mutation path (`add`, `import-csv`, `import-ics`, `merge`, `restore`, `undo`, `redo`) acquires a cross-process file lock on the target timetable before writing.
- **Lock mechanism**: Built on OS-level primitives (`msvcrt` on Windows, `fcntl` on POSIX) via a canonical lock path (e.g. `<target>.lock`), normalized by absolute path and filesystem casing.
- **Lock persistence**: Lock files are retained on release rather than unlinked, eliminating file-descriptor and inode reuse race conditions. Runtime lock files (`*.lock`) are coordination files and should not be edited or deleted during operation.
- **Deterministic Multi-File Locking**: Multi-resource operations (`merge`) use `MultiFileLock` to coordinate locks across all input files and destination files in a globally deterministic canonical path order (`os.path.normcase`), preventing deadlocks across concurrent runs.
- **Rollback on lock failure**: If any lock acquisition in a multi-file sequence times out or fails, all already-acquired locks are immediately released in reverse order before propagating the error.
- **Configurable timeout**: Acquisition timeout defaults to 10.0 seconds and can be configured with the `TIMETABLE_LOCK_TIMEOUT` environment variable. If another process holds the lock beyond this threshold, the command aborts cleanly with exit code `1` and an informative timeout message.

### Cryptographic Revision Tokens

- Every timetable read captures a cryptographic fingerprint (SHA-256 digest) of the exact persisted file bytes.
- The revision token distinguishes:
  - Valid existing file contents (`sha256(...)`)
  - Absent/uncreated files (`"absent"`)
  - Concurrent modifications, additions, or deletions
- Revisions are computed from raw bytes rather than modified timestamps or in-memory normalized structures.

### Atomic Compare-and-Write

- Mutating commands execute an atomic compare-and-write workflow protected under the file lock:
  1. Acquire target file lock.
  2. Read current persisted state and compute actual revision.
  3. Compare current revision against the `expected_revision` captured during initial command load.
  4. If revisions do not match, abort immediately with `ConcurrentModificationError` (exit code `1`), instructing the user to reload and retry.
  5. Validate invariants and serialize proposed changes to a temporary file in the destination directory.
  6. Atomically update undo journal / snapshots and replace the timetable file (`os.replace`).
- Stale writes are rejected before any history or undo/redo entries are recorded, ensuring failed operations leave data and journal state untouched.


## Code

- `timetable/schema.py`: canonical schema definitions, strict validation, and migration framework
- `timetable/loader.py`: reading and writing `timetable.json` safely and atomically
- `timetable/locking.py`: cross-process file locking abstraction and timeout management
- `timetable/revision.py`: cryptographic file revision tokens and snapshot loading
- `timetable/display.py`: printing a day or the week, plus time parsing
- `timetable/now.py`: working out what's on right now
- `timetable/export.py`: writing the `.ics` calendar file
- `timetable/filter.py`: reusable filtering by day, time range and room
- `timetable/free.py`: calculating and rendering free time slots of a given duration
- `timetable/diff.py`: comparing two timetable files
- `timetable/conflicts.py`: detecting overlapping class schedules on the same day
- `timetable/merge.py`: combining timetables with deduplication and conflict detection
- `timetable/import_csv.py`: parsing, validating, planning and safely writing CSV imports

- `timetable/optimizer_config.py`: optimization configuration and constraint representations
- `timetable/optimizer_scoring.py`: multi-objective metrics and scoring engine
- `timetable/optimizer_engine.py`: bounded hill-climbing search and change explanations
- `timetable/diagnostics.py`: structured validation diagnostics, rule catalog, and source provenance
- `timetable/engine.py`: unified validation engine and conflict explanations
- `timetable/import_ics.py`: parsing, recurrence expansion, validating, planning and importing iCalendar (.ics) files

- `timetable/history.py`: snapshot management and manual restore browser
- `timetable/undo.py`: transactional undo and redo journal
- `timetable/stats.py`: compute and render aggregate statistics for a timetable

- `timetable/cli.py`: the commands
- `tests/`: tests, run with `python3 -m unittest discover tests` or `pytest`


## Property-Based Temporal Testing

The repository includes a property-based test suite powered by [Hypothesis](https://hypothesis.readthedocs.io/) in `tests/test_temporal_properties.py`, supported by an independent temporal reference model (`tests/temporal_reference.py`) and reusable strategies (`tests/temporal_strategies.py`).

### Temporal Invariants & Reference Model

1. **Circular Weekly Timeline**:
   - Time is mapped to an independent circular timeline of 10,080 minutes per week (7 days x 1,440 minutes).
   - Weekdays are canonically indexed: `monday=0`, `tuesday=1`, ..., `sunday=6`.
   - Overnight classes (e.g., `23:00`-`01:00`) occupy their starting weekday as well as the following weekday's early morning hours.
   - Sunday overnight classes wrap around circularly into Monday morning.
2. **Half-Open Intervals `[start, end)`**:
   - Boundary-touching classes (e.g. `09:00`-`10:00` and `10:00`-`11:00`, or `23:00`-`00:00` on Monday and `00:00`-`01:00` on Tuesday) meet at an exact boundary point and do not overlap.
3. **Conflict Symmetry & Order Independence**:
   - Two classes collide if and only if their intervals overlap circularly: `overlap(A, B) == overlap(B, A)`.
   - Permuting or reordering classes within weekdays never changes the set of detected conflict pairs.
4. **Free Slots Invariants**:
   - Free intervals always have strictly positive duration `>= duration_minutes` and never overlap with scheduled classes.
   - Free slot detection is independent of input class ordering.
   - Per documented application semantics, free slot search evaluates each weekday's same-day classes; non-positive duration intervals and overnight spillover into subsequent days are excluded from single-day gap calculations.
5. **Serialization & Identity Invariants**:
   - Native JSON save and load preserves all weekday slots losslessly.
   - iCalendar (`.ics`) export and import round-trip accurately preserves classes, overnight transitions, and RFC 5545 escaped characters (commas, semicolons, backslashes, newlines) folded at 75 octets without UTF-8 splitting.
   - CSV import planning is verified against valid rows and rejects malformed records (invalid days, non-positive durations, invalid time formats). Note that full CSV export is intentionally unsupported by design in `timetable-cli`.
   - Event UIDs are functional, deterministic, and invariant to timetable list reordering across multiple distinct classes.

### Running Property-Based Tests

To run the property-based tests alone:
```bash
pytest tests/test_temporal_properties.py
```

To run the complete test suite:
```bash
pytest
```

### Reproducing Failing Hypothesis Examples

When Hypothesis finds a counterexample, it prints the minimal shrinking repro with an `@example(...)` decorator or failure seed. You can reproduce the exact failure by:
1. Adding the printed `@example(...)` decorator directly above the test function.
2. Or running pytest with the reproduction argument:
   ```bash
   pytest tests/test_temporal_properties.py --hypothesis-seed=<seed>
   ```

## Unified Validation Engine

`timetable-cli` incorporates a unified validation engine (`timetable/engine.py` and `timetable/diagnostics.py`) providing deterministic, structured diagnostics across the application: schema validation, timetable mutations (`add`), CSV and iCalendar imports, and three-way merges.

### Diagnostic Data Model

Every validation issue is surfaced as a structured `Diagnostic` object:
- `rule_id`: Stable machine-readable identifier (e.g., `RuleId.SCHEDULE_CONFLICT`, `RuleId.SCHEMA_INVALID`, `RuleId.WEEKDAY_INVALID`).
- `severity`: Standard severity level: `error`, `warning`, or `info`.
- `message`: Human-readable explanation of what failed and why.
- `affected_entries`: List of `AffectedEntry` objects identifying the specific day and slot(s) involved in the issue.
- `source`: Optional `Provenance` record tracing origin (file path, CSV row, ICS line/UID, merge side `base`/`ours`/`theirs`).
- `context`: Structured key-value metadata providing details (e.g. interval minutes, collision day, conflicting fields).

### Rule Catalog

| Rule Identifier | Severity | Description |
|---|---|---|
| `SCHEMA_INVALID` | error | Malformed document root, invalid slot types, or unrecognized keys |
| `VERSION_UNSUPPORTED` | error | Timetable schema version not supported |
| `WEEKDAY_INVALID` | error | Unrecognized weekday key or casing issue |
| `ENTRY_MALFORMED` | error | Slot missing required fields (`subject`, `start`, `end`, `room`) or wrong data types |
| `TIME_INVALID` | error | Invalid time string format (must be 24-hour `HH:MM`) |
| `INTERVAL_INVALID` | error | Class ends at or before start time within the same day |
| `ENTRY_DUPLICATE` | warning | Identical class slot repeated on the same day |
| `SCHEDULE_CONFLICT` | error | Overlapping classes within the same weekday |
| `SCHEDULE_CONFLICT_OVERNIGHT` | error | Overlap involving an overnight class crossing midnight into another day |
| `IMPORT_HEADER_INVALID` | error | CSV file missing mandatory column headers |
| `IMPORT_RECORD_MALFORMED` | error | CSV record with wrong number of fields or empty required values |
| `RECURRENCE_INVALID` | error | Malformed recurrence rule or invalid RRULE parameters |
| `RECURRENCE_UNSUPPORTED` | warning | Unsupported recurrence frequency or parameter |
| `MERGE_STRUCTURAL_CONFLICT` | error | Incompatible branch modifications during three-way merge |
| `MERGE_SCHEDULE_CONFLICT` | error | Overlapping schedule classes in merged timetable result |

### CLI Structured Diagnostics

The `conflicts` command supports structured diagnostic output via `--json`:
```bash
python -m timetable conflicts --json
```

Output:
```json
{
  "is_valid": false,
  "errors": [
    {
      "rule_id": "SCHEDULE_CONFLICT",
      "severity": "error",
      "message": "Schedule conflict on Monday between 'Math' (09:00-10:30) and 'Physics' (10:00-11:00)",
      "affected_entries": [
        {
          "day": "monday",
          "slot": {
            "subject": "Math",
            "start": "09:00",
            "end": "10:30",
            "room": "101"
          }
        },
        {
          "day": "monday",
          "slot": {
            "subject": "Physics",
            "start": "10:00",
            "end": "11:00",
            "room": "102"
          }
        }
      ],
      "context": {
        "collision_day": "monday",
        "day_a": "monday",
        "day_b": "monday",
        "interval_a": [540, 630],
        "interval_b": [600, 660],
        "is_overnight": false
      }
    }
  ],
  "warnings": []
}
```

## Contributing

Fork the repo, make your changes on a new branch, and open a pull request. Run the tests first.

If you find a bug, open an issue with the steps to reproduce it, what you expected, and what happened instead.

Part of Source Start by CSI SPIT. MIT licensed.
