"""Version history, snapshots and restore for the timetable CLI.

This module is deliberately free of CLI coupling.  It owns snapshot creation
and metadata persistence, snapshot listing and inspection, integrity
validation and checksum verification, safe restore with preservation of the
active timetable, and snapshot retention (pruning).  Argument parsing and
exit-code conventions live in :mod:`timetable.cli`.

Snapshots are stored under the history directory described in
:func:`history_directory`, which defaults to ``.timetable-history`` next to the
active timetable file.  The active timetable itself is never read or written
by this module, so ordinary reads work without any history data present.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from timetable.loader import load_timetable, save_timetable, DEFAULT_FILEPATH
from timetable.locking import ConcurrentModificationError, TimetableFileLock
from timetable.revision import load_timetable_snapshot
from timetable.display import WEEKDAYS, render_day

#: Default retention limit. The newest this many snapshots are kept.
DEFAULT_RETENTION_LIMIT = 10

#: Environment variable that overrides the maximum number of retained snapshots.
ENV_HISTORY_LIMIT = "TIMETABLE_HISTORY_LIMIT"

#: Metadata file name inside each snapshot directory.
METADATA_FILE = ".metadata.json"

#: Snapshot identifier prefix.
_SNAPSHOT_PREFIX = "snap"

#: Guards :func:`_snapshot_id` so ids are unique even when two snapshots are
#: created within the same clock reading (datetime has microsecond resolution
#: and consecutive saves on a fast machine can land on the same timestamp).
_ID_LOCK = threading.Lock()
_ID_SEQ = itertools.count()

#: The keys that must be present in snapshot metadata.
_REQUIRED_METADATA_KEYS = (
    "id",
    "created_at",
    "timetable",
    "operation",
    "checksum",
)


class HistoryError(Exception):
    """Base class for history-related errors."""


class SnapshotNotFoundError(HistoryError):
    """Raised when a snapshot id cannot be located."""


class SnapshotCorruptError(HistoryError):
    """Raised when a snapshot file or metadata is malformed or inconsistent."""


class WriteFailureError(HistoryError):
    """Raised when a file write fails and the timetable cannot be updated safely."""


class SnapshotMismatchError(HistoryError):
    """Raised when a snapshot belongs to a different timetable file."""


def history_directory(timetable_file: Optional[Path] = None) -> Path:
    """Return the directory where snapshots for *timetable_file* are stored.

    Defaults to ``.timetable-history`` next to the active timetable file, i.e.
    ``<directory-of-timetable.json>/.timetable-history``.  Passing an explicit
    path lets the CLI direct history into a dedicated location.
    """
    if timetable_file is None:
        timetable_file = DEFAULT_FILEPATH
    base = Path(timetable_file).resolve().parent
    return base / ".timetable-history"


def _same_timetable(recorded: Any, timetable_file: Path) -> bool:
    """Return True when the *recorded* snapshot path names *timetable_file*.

    Several timetables in one folder share the same history directory, so the
    timetable path stored in each snapshot's metadata is what ties a snapshot
    to the file it was taken from.
    """
    try:
        recorded_path = os.path.normcase(str(Path(recorded).resolve()))
        active_path = os.path.normcase(str(Path(timetable_file).resolve()))
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return recorded_path == active_path


def _require_same_timetable(
    metadata: Dict[str, Any], snapshot_id: str, timetable_file: Path
) -> None:
    """Raise when *snapshot_id* was created for a different timetable."""
    recorded = metadata.get("timetable")
    if not _same_timetable(recorded, timetable_file):
        raise SnapshotMismatchError(
            "Snapshot '%s' belongs to '%s', not '%s'."
            % (snapshot_id, recorded, timetable_file)
        )


def default_retention_limit() -> int:
    """Return the maximum number of snapshots to retain.

    The value is read from the :envvar:`TIMETABLE_HISTORY_LIMIT` environment
    variable when it is a positive integer, otherwise the documented default of
    :attr:`DEFAULT_RETENTION_LIMIT` is used.
    """
    raw = os.environ.get(ENV_HISTORY_LIMIT)
    if raw is None:
        return DEFAULT_RETENTION_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_LIMIT
    if value < 1:
        return DEFAULT_RETENTION_LIMIT
    return value


def validate_retention_limit(limit: Any) -> int:
    """Validate and normalise a retention limit.

    Raises :class:`HistoryError` when *limit* is not a positive integer.
    """
    if isinstance(limit, bool):
        raise HistoryError("Retention limit must be a positive integer, got %r." % (limit,))
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise HistoryError("Retention limit must be a positive integer, got %r." % (limit,)) from None
    if value < 1:
        raise HistoryError("Retention limit must be at least 1, got %d." % (value,))
    return value


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot_id() -> str:
    """Return a unique snapshot identifier.

    The identifier embeds the creation timestamp for readability, but the
    timestamp alone is not sufficient: ``datetime`` only resolves microseconds,
    so back-to-back :func:`save_snapshot` calls (or two threads) can read the
    same timestamp.  A process-wide monotonic counter is appended so every
    generated id is unique.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    with _ID_LOCK:
        seq = next(_ID_SEQ)
    return "%s-%s-%06d" % (_SNAPSHOT_PREFIX, stamp, seq)


def _checksum(data: bytes) -> str:
    """Return a lowercase hex SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def _snapshot_dir(history_dir: Path, snapshot_id: str) -> Path:
    return history_dir / snapshot_id


def _metadata_path(snapshot_dir: Path) -> Path:
    return snapshot_dir / METADATA_FILE


def _timetable_file(snapshot_dir: Path) -> Path:
    return snapshot_dir / "timetable.json"


def _delete_snapshot(snapshot_dir: Path, tt_path: Optional[Path] = None, *extra: Any) -> None:
    """Delete a snapshot directory and its files, tolerating partial state.

    *tt_path* names the snapshot's timetable file; when omitted the
    conventional timetable file inside *snapshot_dir* is used.  Any further
    positional arguments (older call sites passed a checksum) are accepted and
    ignored so cleanup never fails with ``TypeError``.
    """
    snapshot_dir = Path(snapshot_dir)
    try:
        target = Path(tt_path) if tt_path is not None else _timetable_file(snapshot_dir)
        if target.exists():
            target.unlink()
        meta = _metadata_path(snapshot_dir)
        if meta.exists():
            meta.unlink()
        snapshot_dir.rmdir()
    except OSError:
        pass


def _write_atomic(target: Path, data: bytes) -> Path:
    """Write *data* to *target* atomically and return the final path.

    A temporary file in the same directory is used, so the replacement is
    guaranteed to be an atomic rename on POSIX and Windows, and a failed write
    cannot leave a partially written snapshot.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix="." + target.suffix
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, target)
        return target
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def load_snapshot(snapshot_id: str, history_dir: Path) -> Dict[str, Any]:
    """Load a validated snapshot and return its document.

    Raises :class:`SnapshotNotFoundError` when the id is unknown, and
    :class:`SnapshotCorruptError` when the snapshot is truncated, invalid JSON or
    fails its integrity checks.
    """
    snapshot_dir = _snapshot_dir(history_dir, snapshot_id)
    meta_path = _metadata_path(snapshot_dir)
    tt_path = _timetable_file(snapshot_dir)

    if not meta_path.exists():
        raise SnapshotNotFoundError("Snapshot '%s' does not exist." % (snapshot_id,))

    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotCorruptError(
            "Snapshot '%s' metadata is invalid JSON: %s" % (snapshot_id, exc)
        ) from exc

    _validate_metadata(metadata, snapshot_id)

    if not tt_path.exists():
        raise SnapshotCorruptError(
            "Snapshot '%s' is missing its timetable file." % (snapshot_id,)
        )

    try:
        timetable = json.loads(tt_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotCorruptError(
            "Snapshot '%s' timetable file is invalid JSON: %s" % (snapshot_id, exc)
        ) from exc

    _validate_timetable_structure(timetable, snapshot_id)
    _validate_checksum(metadata, tt_path.read_bytes(), snapshot_id)

    return {"id": snapshot_id, "metadata": metadata, "timetable": timetable}


def _validate_metadata(metadata: Dict[str, Any], snapshot_id: str) -> None:
    if not isinstance(metadata, dict):
        raise SnapshotCorruptError(
            "Snapshot '%s' metadata is not a JSON object." % (snapshot_id,)
        )
    for key in _REQUIRED_METADATA_KEYS:
        if key not in metadata:
            raise SnapshotCorruptError(
                "Snapshot '%s' metadata is missing key '%s'." % (snapshot_id, key)
            )
    if metadata.get("id") != snapshot_id:
        raise SnapshotCorruptError(
            "Snapshot '%s' has mismatched id in metadata (%r)." % (snapshot_id, metadata.get("id"))
        )
    if not isinstance(metadata.get("created_at"), str) or not metadata.get("created_at"):
        raise SnapshotCorruptError(
            "Snapshot '%s' metadata has an invalid created_at." % (snapshot_id,)
        )
    if not isinstance(metadata.get("timetable"), (str, Path)):
        raise SnapshotCorruptError(
            "Snapshot '%s' metadata has an invalid timetable path." % (snapshot_id,)
        )
    if not isinstance(metadata.get("checksum"), str) or not metadata.get("checksum"):
        raise SnapshotCorruptError(
            "Snapshot '%s' metadata has an invalid checksum." % (snapshot_id,)
        )


def _validate_timetable_structure(data: Any, snapshot_id: str) -> None:
    if not isinstance(data, dict):
        raise SnapshotCorruptError(
            "Snapshot '%s' timetable root is not an object." % (snapshot_id,)
        )
    target = data
    if "schema_version" in data:
        if "timetable" in data:
            if not isinstance(data["timetable"], dict):
                raise SnapshotCorruptError(
                    "Snapshot '%s' versioned timetable is missing 'timetable' object." % (snapshot_id,)
                )
            target = data["timetable"]
        else:
            target = {k: v for k, v in data.items() if k not in ("schema_version", "metadata")}

    for day, slots in target.items():
        if not isinstance(day, str):
            raise SnapshotCorruptError(
                "Snapshot '%s' has a non-string weekday key." % (snapshot_id,)
            )
        if not isinstance(slots, list):
            raise SnapshotCorruptError(
                "Snapshot '%s' weekday '%s' is not a list." % (snapshot_id, day)
            )
        for idx, slot in enumerate(slots, start=1):
            if not isinstance(slot, dict):
                raise SnapshotCorruptError(
                    "Snapshot '%s' entry %d on '%s' is not an object."
                    % (snapshot_id, idx, day)
                )


def _validate_checksum(metadata: Dict[str, Any], tt_bytes: bytes, snapshot_id: str) -> None:
    stored = metadata.get("checksum")
    computed = _checksum(tt_bytes)
    if stored != computed:
        raise SnapshotCorruptError(
            "Snapshot '%s' checksum mismatch (stored %s..., computed %s...)."
            % (snapshot_id, stored[:12], computed[:12])
        )


def _is_valid_metadata(metadata: Dict[str, Any], snapshot_id: str) -> bool:
    """Return True when metadata is complete and consistent enough to list."""
    try:
        _validate_metadata(metadata, snapshot_id)
        return True
    except SnapshotCorruptError:
        return False


def _verify_snapshot(snapshot_dir: Path, metadata: Dict[str, Any]) -> bool:
    """Return True when the snapshot's timetable file matches its checksum.

    Used by :func:`list_snapshots` so partially written or corrupted snapshots
    are skipped instead of being reported as restorable.
    """
    tt_path = _timetable_file(snapshot_dir)
    if not tt_path.exists():
        return False
    try:
        data = tt_path.read_bytes()
    except OSError:
        return False
    return _checksum(data) == metadata.get("checksum")


def list_snapshots(
    history_dir: Path,
    *,
    limit: Optional[int] = None,
    timetable: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return snapshot metadata in reverse chronological order.

    Each entry is a dict with ``id``, ``created_at`` and ``operation``.  The
    list is deterministic and truncated to *limit* when a positive value is
    given.  When *timetable* is given, only snapshots taken from that
    timetable file are returned.
    """
    if limit is not None:
        limit = validate_retention_limit(limit)

    if not history_dir.exists():
        return []

    snapshots = []
    for path in sorted(history_dir.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        meta_path = _metadata_path(path)
        if not meta_path.exists():
            continue
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(metadata, dict) or not _is_valid_metadata(metadata, path.name):
            continue
        if not _verify_snapshot(path, metadata):
            continue
        if timetable is not None and not _same_timetable(metadata.get("timetable"), timetable):
            continue
        snapshots.append(
            {
                "id": metadata.get("id"),
                "created_at": metadata.get("created_at"),
                "operation": metadata.get("operation"),
            }
        )

    if limit is not None and len(snapshots) > limit:
        snapshots = snapshots[:limit]
    return snapshots


def show_snapshot(
    snapshot_id: str,
    history_dir: Path,
    *,
    timetable: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load and render a snapshot without changing the active timetable.

    Raises :class:`SnapshotNotFoundError` / :class:`SnapshotCorruptError` for
    missing, corrupt or invalid snapshots.  The active timetable is never
    modified.
    """
    document = load_snapshot(snapshot_id, history_dir)
    if timetable is not None:
        _require_same_timetable(document["metadata"], snapshot_id, timetable)
    render_timetable(document["timetable"])
    return document


def save_snapshot(
    data: Dict[str, Any],
    *,
    filepath: Optional[Path] = None,
    operation: Optional[str] = None,
    history_dir: Optional[Path] = None,
) -> str:
    """Persist a snapshot of *data* and return its id.

    The snapshot is written atomically and its checksum recorded in metadata.
    The active timetable file is untouched.
    """
    history_dir = history_dir or history_directory(filepath)
    timetable_file = Path(filepath) if filepath is not None else DEFAULT_FILEPATH

    snapshot_id = _snapshot_id()
    snapshot_dir = _snapshot_dir(history_dir, snapshot_id)
    tt_path = _timetable_file(snapshot_dir)

    metadata = {
        "id": snapshot_id,
        "created_at": _utc_now(),
        "operation": operation,
        "timetable": str(timetable_file.resolve()),
        "checksum": None,
    }

    payload = json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8")
    tt_payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")

    try:
        _write_atomic(_metadata_path(snapshot_dir), payload)
        _write_atomic(tt_path, tt_payload)
    except OSError as exc:
        _delete_snapshot(snapshot_dir, tt_path)
        raise WriteFailureError("Could not persist snapshot: %s" % (exc,)) from exc

    checksum = _checksum(tt_payload)
    metadata["checksum"] = checksum
    try:
        _write_atomic(
            _metadata_path(snapshot_dir),
            json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"),
        )
    except OSError as exc:
        # Never leave a metadata file that does not match the stored checksum.
        _delete_snapshot(snapshot_dir, tt_path, checksum)
        raise WriteFailureError("Could not persist snapshot metadata: %s" % (exc,)) from exc

    return snapshot_id


def render_timetable(data: Dict[str, Any]) -> None:
    """Print a timetable in the same readable format as the CLI."""
    for day in WEEKDAYS:
        slots = data.get(day, []) or []
        if not isinstance(slots, list):
            continue
        render_day(data, day)


def restore_snapshot(
    snapshot_id: str,
    *,
    filepath: Optional[Path] = None,
    history_dir: Optional[Path] = None,
    expected_revision: Optional[str] = None,
    lock_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Restore a snapshot into the active timetable file.

    The current active timetable is preserved as a snapshot before the restore,
    and restoration is safe against incomplete or interrupted writes.
    """
    history_dir = history_dir or history_directory(filepath)
    timetable_file = Path(filepath) if filepath is not None else DEFAULT_FILEPATH

    with TimetableFileLock(timetable_file, timeout=lock_timeout):
        # 0. Check revision under the lock if expected_revision was specified
        if expected_revision is not None:
            active_snap = load_timetable_snapshot(timetable_file, validate=False)
            if active_snap.revision != expected_revision:
                raise ConcurrentModificationError(
                    timetable_file,
                    expected_revision=expected_revision,
                    current_revision=active_snap.revision,
                )

        # 1. Locate and validate the requested snapshot.
        try:
            document = load_snapshot(snapshot_id, history_dir)
        except SnapshotNotFoundError:
            raise
        except SnapshotCorruptError as exc:
            raise SnapshotCorruptError("Cannot restore '%s': %s" % (snapshot_id, exc)) from exc

        # A snapshot may only replace the timetable it was taken from; timetables
        # in the same folder share one history directory.
        _require_same_timetable(document["metadata"], snapshot_id, timetable_file)

        restored_timetable = document["timetable"]

        # 2. Validate the restored timetable structure.
        _validate_timetable_structure(restored_timetable, snapshot_id)

        # 3. Preserve the current active timetable as a recoverable snapshot before
        #    overwriting it.  This works even when the active timetable does not
        #    yet exist: loading a missing file yields the default empty structure.
        existing = load_timetable(timetable_file)
        _preserve_current_timetable(
            existing, timetable_file, history_dir, operation="restore-previous"
        )

        # 4. Write the restored timetable safely (atomic replacement): a temporary
        #    file is written first and renamed over the target, so a failed write
        #    can never leave the active timetable truncated or corrupted.
        try:
            _write_atomic(
                timetable_file,
                json.dumps(restored_timetable, indent=2).encode("utf-8"),
            )
        except (OSError, TypeError, ValueError) as exc:
            raise WriteFailureError("Could not write restored timetable: %s" % (exc,)) from exc

        # 5./6. Success only after the file has been written.
        return {"id": snapshot_id, "restored": True}


def _preserve_current_timetable(
    data: Dict[str, Any],
    filepath: Path,
    history_dir: Path,
    *,
    operation: str,
) -> None:
    """Persist *data* as a snapshot before a restore, or delete an unused one."""
    try:
        save_snapshot(data, filepath=filepath, operation=operation, history_dir=history_dir)
    except WriteFailureError:
        # The restore itself failed; the active timetable was already written by
        # the caller, so leave the history clean.
        raise


def _active_restore_targets(history_dir: Path) -> set:
    """Return snapshot ids that are currently being restored from."""
    targets = set()
    if not history_dir.exists():
        return targets
    for path in history_dir.iterdir():
        if not path.is_dir():
            continue
        meta = path / METADATA_FILE
        if not meta.exists():
            continue
        try:
            metadata = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if metadata.get("operation") == "restore-previous":
            targets.add(path.name)
    return targets


def prune_snapshots(
    history_dir: Path,
    limit: int,
    *,
    timetable: Optional[Path] = None,
) -> int:
    """Retain the *limit* newest snapshots, deleting the older ones.

    Returns the number of snapshots removed.  Removals happen only after the
    snapshot was saved and verified successfully, and no snapshot currently
    needed for an in-progress restore is deleted.  When *timetable* is given,
    retention is applied only to snapshots owned by that timetable; snapshots
    for other files sharing the directory are left untouched.
    """
    limit = validate_retention_limit(limit)
    if not history_dir.exists():
        return 0

    snapshots = sorted(
        (p for p in history_dir.iterdir() if p.is_dir()), key=lambda p: p.name
    )

    if timetable is not None:
        owned_snapshots = []
        for path in snapshots:
            meta_path = _metadata_path(path)
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(metadata, dict) and _same_timetable(
                metadata.get("timetable"), timetable
            ):
                owned_snapshots.append(path)
        snapshots = owned_snapshots

    # Never delete a snapshot that is currently the active restore target.
    active_ids = _active_restore_targets(history_dir)
    removable = [p for p in snapshots if p.name not in active_ids]

    # Drop the oldest beyond the limit, deterministically.
    removed = 0
    while len(removable) > limit:
        oldest = removable.pop(0)
        _delete_snapshot(oldest, _timetable_file(oldest))
        (history_dir / oldest.name / METADATA_FILE).unlink(missing_ok=True)
        removed += 1
    return removed

