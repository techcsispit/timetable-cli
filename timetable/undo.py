"""Transactional undo/redo history for timetable mutations.

Persistent, snapshot-based undo/redo journal described in issue #44.  Free of
CLI coupling (like :mod:`timetable.history`); argument parsing and exit codes
live in :mod:`timetable.cli`.

Design
------
* One journal file per timetable file, stored beside it as
  ``.timetable-undo/undo-history.json``.  The journal records the *resolved*
  path of the timetable it belongs to and every entry repeats that path, so
  two different timetable files can never share history and a journal can
  never redirect writes to an unrelated timetable.  Existing timetable JSON
  files need no migration; read-only commands never touch this module.
* Entries are plain-JSON snapshots: the undo stack stores the timetable as it
  was *before* each successful mutation; the redo stack stores the state that
  was undone.  Nothing references other files, so restoring is exact.
* All writes are atomic (temp file in the destination directory, fsync,
  ``os.replace``).  The journal is written *before* the timetable, so a crash
  between the two writes leaves an extra undo step rather than a lost state;
  a failed timetable write rolls the journal back.  This is not a full
  multi-file transaction - see the README for exact guarantees.
* History files are untrusted input: schema, version, paths, entries,
  snapshots and checksums are validated before use and failures raise
  :class:`HistoryFileError` with actionable messages.  Corrupt history is
  never silently reset; recovery is explicit via :func:`recreate_journal`
  and never modifies the timetable itself.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from timetable.loader import DEFAULT_FILEPATH, load_timetable, save_timetable
from timetable.locking import ConcurrentModificationError, TimetableFileLock
from timetable.revision import REVISION_ABSENT, load_timetable_snapshot
from timetable.schema import create_canonical_timetable, is_versioned_format


#: Journal schema version written by this module.
JOURNAL_VERSION = 1

#: Documented maximum number of entries kept on each stack.
DEFAULT_UNDO_LIMIT = 50

#: Environment variable that overrides the per-stack history limit.
ENV_UNDO_LIMIT = "TIMETABLE_UNDO_LIMIT"

#: Directory (next to the timetable) that holds journal files.
UNDO_HISTORY_DIRNAME = ".timetable-undo"


def _journal_filename(timetable_file: Optional[Path] = None) -> str:
    """Return the journal file name for *timetable_file*.

    Each timetable file gets its own journal file name (derived from the
    timetable's own file name) inside the shared ``.timetable-undo``
    directory, so two timetable files living in the same directory never
    share history.
    """
    if timetable_file is None:
        timetable_file = DEFAULT_FILEPATH
    return "%s.undo-history.json" % (Path(timetable_file).name,)


def undo_history_path(timetable_file: Optional[Path] = None) -> Path:
    """Return the journal path for *timetable_file*.

    The journal lives in ``.timetable-undo/<timetable-name>.undo-history.json``
    next to the (resolved) timetable file, so each timetable file has its own
    history and custom ``--file`` paths resolve to their own journals.
    """
    if timetable_file is None:
        timetable_file = DEFAULT_FILEPATH
    base = Path(timetable_file).resolve().parent
    return base / UNDO_HISTORY_DIRNAME / _journal_filename(timetable_file)

#: Maximum bytes read from a journal file.
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024

#: Required top-level journal keys.
_REQUIRED_JOURNAL_KEYS = ("version", "limit", "timetable_path", "undo", "redo")

#: Required keys on every history entry.
_REQUIRED_ENTRY_KEYS = (
    "id",
    "operation",
    "at",
    "timetable_path",
    "snapshot",
    "checksum",
    "raw",
    "raw_checksum",
)


class HistoryFileError(Exception):
    """Raised when the undo/redo journal is missing data or is corrupt."""


class NoOpError(Exception):
    """Raised when a requested mutation would not change the timetable.

    Used by :func:`commit_mutation` so a no-op never creates a history entry
    and never clears the redo stack.
    """


def default_undo_limit() -> int:
    """Return the maximum number of undo (and redo) entries to keep.

    Read from :envvar:`TIMETABLE_UNDO_LIMIT` when it is a positive integer,
    otherwise the documented default :attr:`DEFAULT_UNDO_LIMIT`.
    """
    raw = os.environ.get(ENV_UNDO_LIMIT)
    if raw is None:
        return DEFAULT_UNDO_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_UNDO_LIMIT
    if value < 1:
        return DEFAULT_UNDO_LIMIT
    return value


def validate_undo_limit(limit: Any) -> int:
    """Validate and normalise an undo history limit.

    Raises :class:`HistoryFileError` when *limit* is not a positive integer.
    """
    if isinstance(limit, bool):
        raise HistoryFileError(
            "Undo history limit must be a positive integer, got %r." % (limit,)
        )
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise HistoryFileError(
            "Undo history limit must be a positive integer, got %r." % (limit,)
        ) from None
    if value < 1:
        raise HistoryFileError(
            "Undo history limit must be at least 1, got %d." % (value,)
        )
    return value


def _resolved_timetable_path(timetable_file: Optional[Path] = None) -> str:
    if timetable_file is None:
        timetable_file = DEFAULT_FILEPATH
    return str(Path(timetable_file).resolve())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return "undo-%s-%03d" % (stamp, os.getpid() % 1000)


def _canonical_bytes(data: Any) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _checksum(data: Any) -> str:
    return hashlib.sha256(_canonical_bytes(data)).hexdigest()


def _raw_checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_current_raw(filepath: Path) -> Optional[bytes]:
    """Return the raw bytes of *filepath*, or ``None`` if it does not exist."""
    path = Path(filepath)
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _raw_to_str(raw: Optional[bytes]) -> Optional[str]:
    """Encode raw file bytes as an ASCII-safe string for JSON storage."""
    if raw is None:
        return None
    return base64.b64encode(raw).decode("ascii")


def _str_to_raw(value: Any, context: str) -> bytes:
    """Decode a stored base64 string back to bytes, validating it."""
    if not isinstance(value, str):
        raise HistoryFileError(
            "%s: raw snapshot must be a base64 string, got %s."
            % (context, type(value).__name__)
        )
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise HistoryFileError(
            "%s: raw snapshot is not valid base64: %s" % (context, exc)
        ) from exc


def _empty_journal(timetable_path: str, limit: int) -> Dict[str, Any]:
    return {
        "version": JOURNAL_VERSION,
        "limit": limit,
        "timetable_path": timetable_path,
        "undo": [],
        "redo": [],
    }


def _validate_snapshot(snapshot: Any, context: str) -> Dict[str, Any]:
    """Validate that *snapshot* is a JSON object that can be stored and
    restored exactly.

    Timetables are weekday->list mappings, but a journal entry must restore
    the *exact* previous content of whatever JSON file was active (an output
    file may contain arbitrary JSON), so the check here is structural JSON
    soundness rather than timetable shape: an object whose values are
    recursively JSON-compatible.  Anything else raises
    :class:`HistoryFileError` instead of being written to or read from the
    journal.
    """
    if not isinstance(snapshot, dict):
        raise HistoryFileError(
            "%s: snapshot must be a JSON object, got %s."
            % (context, type(snapshot).__name__)
        )
    _validate_json_value(snapshot, context)
    return snapshot


def _validate_json_value(value: Any, context: str) -> None:
    """Recursively ensure *value* contains only JSON-compatible types."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, context)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise HistoryFileError(
                    "%s: snapshot has a non-string key." % (context,)
                )
            _validate_json_value(item, context)
        return
    raise HistoryFileError(
        "%s: snapshot contains a %s value that cannot be stored as JSON."
        % (context, type(value).__name__)
    )


def _validate_entry(
    entry: Any, index: int, stack: str, expected_path: str
) -> Dict[str, Any]:
    context = "entry %d of the %s stack" % (index + 1, stack)
    if not isinstance(entry, dict):
        raise HistoryFileError(
            "%s must be a JSON object, got %s."
            % (context, type(entry).__name__)
        )
    for key in _REQUIRED_ENTRY_KEYS:
        if key not in entry:
            raise HistoryFileError(
                "%s is missing required key %r." % (context, key)
            )
    if not isinstance(entry["id"], str) or not entry["id"]:
        raise HistoryFileError("%s has an invalid id." % (context,))
    if not isinstance(entry["operation"], str) or not entry["operation"]:
        raise HistoryFileError("%s has an invalid operation." % (context,))
    if not isinstance(entry["at"], str) or not entry["at"]:
        raise HistoryFileError("%s has an invalid timestamp." % (context,))
    entry_path = entry["timetable_path"]
    if not isinstance(entry_path, str) or not entry_path:
        raise HistoryFileError("%s has an invalid timetable path." % (context,))
    if Path(entry_path).resolve() != Path(expected_path).resolve():
        raise HistoryFileError(
            "%s belongs to %r but this history is for %r; refusing to use it."
            % (context, entry_path, expected_path)
        )
    _validate_snapshot(entry["snapshot"], context)
    if not isinstance(entry["checksum"], str) or not entry["checksum"]:
        raise HistoryFileError("%s has an invalid checksum." % (context,))
    computed = _checksum(entry["snapshot"])
    if computed != entry["checksum"]:
        raise HistoryFileError(
            "%s checksum mismatch (stored %s..., computed %s...); the journal "
            "is corrupt." % (context, entry["checksum"][:12], computed[:12])
        )
    # ``raw`` / ``raw_checksum`` hold the exact previous file bytes (base64)
    # so restoration can be byte-for-byte; both must be present and agree.
    raw_bytes = _str_to_raw(entry["raw"], context)
    raw_stored = entry["raw_checksum"]
    if not isinstance(raw_stored, str) or not raw_stored:
        raise HistoryFileError("%s has an invalid raw checksum." % (context,))
    raw_computed = _raw_checksum(raw_bytes)
    if raw_computed != raw_stored:
        raise HistoryFileError(
            "%s raw checksum mismatch (stored %s..., computed %s...); the "
            "journal is corrupt." % (context, raw_stored[:12], raw_computed[:12])
        )
    # The raw bytes must decode to exactly the parsed snapshot, otherwise the
    # entry is internally inconsistent.
    try:
        decoded = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HistoryFileError(
            "%s raw snapshot is not valid UTF-8 JSON: %s" % (context, exc)
        ) from exc
    if _canonical_bytes(decoded) != _canonical_bytes(entry["snapshot"]):
        raise HistoryFileError(
            "%s raw snapshot does not match its parsed snapshot; the journal "
            "is corrupt." % (context,)
        )
    return entry

def _validate_journal(document: Any, expected_path: str) -> Dict[str, Any]:
    if not isinstance(document, dict):
        raise HistoryFileError(
            "Undo history root must be a JSON object, got %s."
            % (type(document).__name__)
        )
    for key in _REQUIRED_JOURNAL_KEYS:
        if key not in document:
            raise HistoryFileError(
                "Undo history is missing required key %r." % (key,)
            )
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise HistoryFileError(
            "Undo history has a non-integer version (%r)." % (version,)
        )
    if version != JOURNAL_VERSION:
        raise HistoryFileError(
            "Unsupported undo history version %s (this build supports "
            "version %d)." % (version, JOURNAL_VERSION)
        )
    try:
        validate_undo_limit(document["limit"])
    except HistoryFileError as exc:
        raise HistoryFileError(
            "Undo history has an invalid limit: %s" % (exc,)
        ) from exc
    journal_path = document["timetable_path"]
    if not isinstance(journal_path, str) or not journal_path:
        raise HistoryFileError("Undo history has an invalid timetable path.")
    if Path(journal_path).resolve() != Path(expected_path).resolve():
        raise HistoryFileError(
            "Undo history belongs to %r but the active timetable is %r; "
            "refusing to mix histories of different timetable files."
            % (journal_path, expected_path)
        )
    for stack_name in ("undo", "redo"):
        stack = document[stack_name]
        if not isinstance(stack, list):
            raise HistoryFileError(
                "Undo history %r stack must be a list, got %s."
                % (stack_name, type(stack).__name__)
            )
        for index, entry in enumerate(stack):
            _validate_entry(entry, index, stack_name, expected_path)
    recovery = document.get("recovery")
    if recovery is not None and not isinstance(recovery, dict):
        raise HistoryFileError("Undo history has an invalid recovery marker.")
    return document


def load_journal(
    timetable_file: Optional[Path] = None,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Load and validate the journal for *timetable_file*.

    Returns an empty (in-memory) journal when no journal file exists yet, so
    read-only commands and first runs need no history data on disk.  Raises
    :class:`HistoryFileError` for malformed, truncated, unsupported-version or
    inconsistent journals; the caller must surface the error rather than
    silently resetting it.
    """
    if limit is None:
        limit = default_undo_limit()
    else:
        limit = validate_undo_limit(limit)

    path = _resolved_timetable_path(timetable_file)
    journal_path = undo_history_path(timetable_file)
    if not journal_path.exists():
        return _empty_journal(path, limit)

    try:
        raw = journal_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HistoryFileError(
            "Could not read undo history %s: %s" % (journal_path, exc)
        ) from exc
    if len(raw.encode("utf-8")) > _MAX_JOURNAL_BYTES:
        raise HistoryFileError(
            "Undo history %s is unreasonably large; refusing to load it."
            % (journal_path,)
        )
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HistoryFileError(
            "Undo history %s is not valid JSON (%s). The file may be "
            "truncated or corrupt; no timetable data was changed. To start a "
            "fresh history, delete the file (recover it first if you need "
            "its states)." % (journal_path, exc)
        ) from exc
    return _validate_journal(document, path)


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write *payload* to *target* atomically (temp file beside target)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def save_journal(
    document: Dict[str, Any], timetable_file: Optional[Path] = None
) -> Path:
    """Atomically persist *document* for *timetable_file* and return its path.

    The document is validated before writing so a programming error never
    produces an inconsistent journal on disk.
    """
    path = _resolved_timetable_path(timetable_file)
    _validate_journal(document, path)
    journal_path = undo_history_path(timetable_file)
    payload = json.dumps(
        document, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    try:
        _atomic_write_bytes(journal_path, payload)
    except OSError as exc:
        raise HistoryFileError(
            "Could not write undo history %s: %s" % (journal_path, exc)
        ) from exc
    return journal_path


def recreate_journal(
    timetable_file: Optional[Path] = None, *, limit: Optional[int] = None
) -> Path:
    """Explicitly start a fresh journal for *timetable_file*.

    This is the documented recovery action for a corrupt journal.  It only
    rewrites the journal file; the timetable itself is never touched.
    """
    if limit is None:
        limit = default_undo_limit()
    else:
        limit = validate_undo_limit(limit)
    document = _empty_journal(_resolved_timetable_path(timetable_file), limit)
    return save_journal(document, timetable_file)


def _prune(document: Dict[str, Any], limit: int) -> None:
    """Prune the oldest undo entries, preserving undo/redo ordering.

    Redo entries are protected (they describe states the user explicitly
    undid); undo entries beyond *limit* are dropped oldest-first.  The check
    also runs when the redo stack alone exceeds *limit* (possible after the
    limit is lowered via the environment), in which case only the undo side
    shrinks so redo semantics stay intact.
    """
    limit = validate_undo_limit(limit)
    document["limit"] = limit
    undo = document["undo"]
    redo = document["redo"]
    if len(undo) + len(redo) <= limit:
        return
    excess = len(undo) + len(redo) - limit
    del undo[:excess]


def _make_entry(
    operation: str,
    snapshot: Dict[str, Any],
    timetable_path: str,
    raw: Optional[bytes],
) -> Dict[str, Any]:
    raw_bytes = raw if raw is not None else json.dumps(
        snapshot, indent=2, ensure_ascii=False
    ).encode("utf-8")
    return {
        "id": _entry_id(),
        "operation": operation,
        "at": _utc_now(),
        "timetable_path": timetable_path,
        "snapshot": copy.deepcopy(snapshot),
        "checksum": _checksum(snapshot),
        "raw": _raw_to_str(raw_bytes),
        "raw_checksum": _raw_checksum(raw_bytes),
    }


def commit_mutation(
    filepath: Path,
    operation: str,
    new_data: Dict[str, Any],
    *,
    limit: Optional[int] = None,
    canonicalize: bool = False,
    expected_revision: Optional[str] = None,
    lock_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Persist *new_data* as a successful mutation of *filepath*.

    Single shared entry point for every mutating command:

    1. Acquires a cross-process lock on the target timetable file.
    2. Reads current state and raw bytes under the lock, computing the current revision.
    3. If *expected_revision* is provided, verifies that the current revision matches;
       otherwise raises :class:`ConcurrentModificationError`.
    4. Validates proposed state structurally and raises :class:`NoOpError` when it equals
       the current state.
    5. Writes the journal first, then atomically replaces the timetable.
    6. Releases the lock upon completion or error.
    """
    if limit is None:
        limit = default_undo_limit()
    else:
        limit = validate_undo_limit(limit)
    if not isinstance(operation, str) or not operation:
        raise HistoryFileError("Mutation operation must be a non-empty string.")

    with TimetableFileLock(filepath, timeout=lock_timeout):
        timetable_path = _resolved_timetable_path(filepath)

        # Check revision under the lock if expected_revision was specified
        if expected_revision is not None:
            snapshot = load_timetable_snapshot(filepath, validate=False)
            if snapshot.revision != expected_revision:
                raise ConcurrentModificationError(
                    filepath,
                    expected_revision=expected_revision,
                    current_revision=snapshot.revision,
                )

        previous_raw = _read_current_raw(filepath)
        if previous_raw is not None:
            try:
                previous_snapshot = json.loads(previous_raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise HistoryFileError(
                    "Current timetable %s is not valid UTF-8 JSON: %s"
                    % (filepath, exc)
                ) from exc
            _validate_snapshot(previous_snapshot, "current timetable document")
            previous = previous_snapshot
        else:
            previous = load_timetable(filepath)
            previous_snapshot = previous

        _validate_snapshot(previous, "current timetable")
        _validate_snapshot(new_data, "proposed timetable")

        if _canonical_bytes(previous) == _canonical_bytes(new_data):
            raise NoOpError(
                "The requested change would not modify the timetable; nothing was "
                "written and history was left unchanged."
            )

        journal_path = undo_history_path(filepath)
        existed_before = journal_path.exists()
        before: Optional[Dict[str, Any]] = None
        if existed_before:
            before = load_journal(filepath, limit=limit)

        document = before if before is not None else _empty_journal(timetable_path, limit)
        entry = _make_entry(operation, previous_snapshot, timetable_path, previous_raw)
        document["undo"].append(entry)
        document["redo"] = []
        _prune(document, limit)

        # 1. Journal first: a crash after this point leaves an extra undo step
        #    rather than a lost previous state.
        try:
            save_journal(document, filepath)
        except OSError as exc:
            # Roll back the journal so a failed history write leaves neither
            # the timetable nor history changed.  Any corruption left by the
            # partially written journal is best-effort repaired or deleted.
            _rollback_journal(journal_path, before, existed_before)
            raise HistoryFileError(
                "Could not write undo history %s: %s" % (journal_path, exc)
            ) from exc

        # 2. Atomically replace the timetable; repair the journal if this fails so
        #    a failed mutation changes neither the timetable nor history.
        try:
            to_write = create_canonical_timetable(new_data) if canonicalize else new_data
            _atomic_replace_timetable(filepath, to_write)
        except Exception as exc:
            _rollback_journal(journal_path, before, existed_before)
            raise HistoryFileError(
                "Could not write timetable %s: %s (the undo history was left "
                "unchanged)" % (filepath, exc)
            ) from exc

        return entry





def _atomic_replace_timetable(filepath: Path, data: Dict[str, Any]) -> None:
    """Atomically write *data* using the canonical schema persistence path."""
    save_timetable(data, filepath)


def _atomic_restore_raw(filepath: Path, payload: bytes) -> None:
    """Atomically write raw bytes back to *filepath* (byte-for-byte restore)."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name, suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _rollback_journal(
    journal_path: Path, before: Optional[Dict[str, Any]], existed_before: bool
) -> None:
    """Best-effort restore of the journal to its pre-commit state."""
    try:
        if existed_before and before is not None:
            # undo_history_path() = <parent>/.timetable-undo/undo-history.json
            timetable_file = Path(journal_path).resolve().parent.parent
            save_journal(before, timetable_file)
        else:
            journal_path.unlink(missing_ok=True)
    except (OSError, HistoryFileError):
        # The timetable write already failed; leave the journal as-is rather
        # than raising a second error that would mask the original failure.
        pass


def undo_last(
    filepath: Path,
    *,
    limit: Optional[int] = None,
    lock_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Restore the state immediately before the latest successful mutation.

    The entry is moved to the redo stack only after the timetable was restored
    successfully; the redo entry remembers the state that was undone so a later
    :func:`redo_last` can reapply it exactly.  On any failure neither the
    timetable nor the journal is modified.  Raises :class:`HistoryFileError`
    with an actionable message when there is nothing to undo or when the
    journal is invalid.
    """
    with TimetableFileLock(filepath, timeout=lock_timeout):
        timetable_path = _resolved_timetable_path(filepath)
        journal = load_journal(filepath, limit=limit)
        if not journal["undo"]:
            raise HistoryFileError(
                "Nothing to undo: no recorded changes for '%s'." % (timetable_path,)
            )

        entry = journal["undo"][-1]
        snapshot = entry["snapshot"]
        _validate_snapshot(snapshot, "undo entry '%s'" % (entry["id"],))

        # Decode the exact bytes to restore now, before touching anything, so a
        # corrupt entry can never partially modify the timetable.
        raw_bytes = _str_to_raw(entry["raw"], "undo entry '%s'" % (entry["id"],))

        # 1. Remember the state we are about to replace (the target for redo),
        #    captured as raw bytes so redo restores it exactly too.
        current_raw = _read_current_raw(filepath)
        try:
            current = json.loads(current_raw.decode("utf-8")) if current_raw else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise HistoryFileError(
                "Current timetable %s is not valid UTF-8 JSON: %s" % (filepath, exc)
            ) from exc
        _validate_snapshot(current, "current timetable")

        # 2. Restore the timetable first; if this fails, history is untouched.
        _atomic_restore_raw(filepath, raw_bytes)

        # 3. Only after a successful restoration, move the entry across.
        journal["undo"].pop()
        entry["redo_snapshot"] = copy.deepcopy(current)
        entry["redo_checksum"] = _checksum(current)
        entry["redo_raw"] = _raw_to_str(current_raw)
        entry["redo_raw_checksum"] = _raw_checksum(current_raw or b"")
        journal["redo"].append(entry)
        try:
            save_journal(journal, filepath)
        except Exception:
            # The timetable was restored but the journal update failed: put the
            # timetable back so neither file changed, then report the error.
            try:
                _atomic_restore_raw(filepath, current_raw or b"{}")
            except Exception:
                pass
            raise
        return entry


def redo_last(
    filepath: Path,
    *,
    limit: Optional[int] = None,
    lock_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Reapply the most recently undone state transition.

    The entry is moved back to the undo stack only after the timetable was
    restored successfully.  On any failure neither the timetable nor the
    journal is modified.
    """
    with TimetableFileLock(filepath, timeout=lock_timeout):
        timetable_path = _resolved_timetable_path(filepath)
        journal = load_journal(filepath, limit=limit)
        if not journal["redo"]:
            raise HistoryFileError(
                "Nothing to redo: no undone changes recorded for '%s'."
                % (timetable_path,)
            )

        entry = journal["redo"][-1]
        target = entry.get("redo_snapshot")
        if target is None:
            raise HistoryFileError(
                "Redo entry '%s' is missing its target state; the history is "
                "inconsistent." % (entry["id"],)
            )
        _validate_snapshot(target, "redo entry '%s'" % (entry["id"],))
        stored = entry.get("redo_checksum")
        if not isinstance(stored, str) or stored != _checksum(target):
            raise HistoryFileError(
                "Redo entry '%s' checksum mismatch; the journal is corrupt."
                % (entry["id"],)
            )
        # Decode the exact bytes to restore now, before touching anything, so a
        # corrupt entry can never partially modify the timetable.
        redo_raw = _str_to_raw(
            entry.get("redo_raw"), "redo entry '%s'" % (entry["id"],)
        )
        redo_raw_stored = entry.get("redo_raw_checksum")
        if not isinstance(redo_raw_stored, str) or (
            redo_raw_stored != _raw_checksum(redo_raw)
        ):
            raise HistoryFileError(
                "Redo entry '%s' raw checksum mismatch; the journal is corrupt."
                % (entry["id"],)
            )

        # 1. Restore the undone state first; if this fails, history is untouched.
        _atomic_restore_raw(filepath, redo_raw)

        # 2. Only after a successful restoration, move the entry back.
        journal["redo"].pop()
        journal["undo"].append(entry)
        try:
            save_journal(journal, filepath)
        except Exception:
            # Journal update failed after the timetable was written: restore the
            # timetable to what it was before this redo attempt, so neither file
            # changed, then report the error.
            try:
                previous_raw = _read_current_raw(filepath)
            except Exception:
                previous_raw = None
            if previous_raw is not None:
                try:
                    _atomic_restore_raw(filepath, previous_raw)
                except Exception:
                    pass
            raise
        return entry


