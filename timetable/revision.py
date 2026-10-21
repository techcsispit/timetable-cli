"""Revision fingerprinting and snapshot loading for concurrency-safe timetable updates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from timetable.display import WEEKDAYS
from timetable.loader import DEFAULT_FILEPATH
from timetable.schema import (
    SchemaError,
    ValidationError,
    UnsupportedSchemaVersionError,
    create_empty_canonical_timetable,
    normalize_to_timetable_dict,
    validate_schema,
)

#: Sentinel value representing a missing / uncreated timetable file.
REVISION_ABSENT = "absent"


@dataclass(frozen=True)
class TimetableSnapshot:
    """Represents a loaded timetable state alongside its cryptographic revision token.

    Attributes:
        filepath: Canonical path of the timetable file.
        exists: True if the file existed when read, False otherwise.
        raw_bytes: Exact bytes read from disk, or None if the file was missing.
        revision: SHA-256 hex digest of raw_bytes, or 'absent' if file was missing.
        data: In-memory normalized weekday dictionary (monday..sunday -> class list).
        raw_data: Un-normalized parsed JSON object as read from disk, or None if missing.
    """

    filepath: Path
    exists: bool
    raw_bytes: Optional[bytes]
    revision: str
    data: Dict[str, List[Dict[str, Any]]]
    raw_data: Optional[Dict[str, Any]]


def compute_revision(raw_bytes: Optional[bytes]) -> str:
    """Compute cryptographic SHA-256 revision token of raw bytes.

    Returns REVISION_ABSENT ('absent') when raw_bytes is None.
    """
    if raw_bytes is None:
        return REVISION_ABSENT
    return hashlib.sha256(raw_bytes).hexdigest()


def load_timetable_snapshot(
    filepath: Union[str, Path] = DEFAULT_FILEPATH,
    *,
    validate: bool = True,
) -> TimetableSnapshot:
    """Load timetable data and its cryptographic revision token from the same byte sequence.

    Guarantees:
    - The cryptographic revision is calculated strictly from the persisted bytes on disk.
    - If the file does not exist, revision is 'absent' and data is an empty timetable.
    - If the file contains malformed JSON or invalid schema (when validate=True),
      the exception is raised immediately so stale or corrupt data is never processed.
    - Distinguishes missing file, valid existing file, and malformed file.
    """
    path = Path(filepath)
    if not path.exists():
        empty_doc = {day: [] for day in WEEKDAYS}
        return TimetableSnapshot(
            filepath=path,
            exists=False,
            raw_bytes=None,
            revision=REVISION_ABSENT,
            data=empty_doc,
            raw_data=None,
        )

    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        empty_doc = {day: [] for day in WEEKDAYS}
        return TimetableSnapshot(
            filepath=path,
            exists=False,
            raw_bytes=None,
            revision=REVISION_ABSENT,
            data=empty_doc,
            raw_data=None,
        )

    revision = compute_revision(raw_bytes)

    # Parse and validate from exactly raw_bytes
    try:
        raw_data = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"Timetable file '{path}' is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Timetable file '{path}' is not valid JSON: {exc}") from exc

    if validate:
        validate_schema(raw_data)
        data = normalize_to_timetable_dict(raw_data)
    else:
        try:
            data = normalize_to_timetable_dict(raw_data)
        except Exception:
            data = raw_data if isinstance(raw_data, dict) else {day: [] for day in WEEKDAYS}

    return TimetableSnapshot(
        filepath=path,
        exists=True,
        raw_bytes=raw_bytes,
        revision=revision,
        data=data,
        raw_data=raw_data,
    )

