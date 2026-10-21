"""File loading and saving utilities for timetable schedules."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from timetable.display import WEEKDAYS
from timetable.locking import TimetableFileLock
from timetable.schema import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    SchemaError,
    ValidationError,
    UnsupportedSchemaVersionError,
    create_canonical_timetable,
    create_empty_canonical_timetable,
    detect_schema_version,
    is_versioned_format,
    migrate_to_canonical,
    normalize_to_timetable_dict,
    validate_schema,
)

DEFAULT_FILEPATH = Path(__file__).resolve().parent.parent / "timetable.json"


def load_timetable_raw(filepath=DEFAULT_FILEPATH) -> Dict[str, Any]:
    """Loads the raw timetable JSON document without in-memory normalization.

    Returns the parsed JSON data as stored on disk.
    If the file does not exist, returns an empty canonical document.
    Raises FileNotFoundError, json.JSONDecodeError, ValidationError, or
    UnsupportedSchemaVersionError if invalid or unreadable.
    """
    path = Path(filepath)
    if not path.exists():
        return create_empty_canonical_timetable()

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    validate_schema(data)
    return data


def load_timetable(filepath=DEFAULT_FILEPATH) -> Dict[str, List[Dict[str, Any]]]:
    """Loads timetable data from a JSON file and normalizes it to a weekday dictionary.

    Returns a dictionary mapping lowercase weekdays to lists of lecture slots.
    If the file does not exist, returns a dictionary with all weekdays mapping to empty lists.
    Validates legacy and versioned files, normalizing in memory without modifying the file.
    Raises json.JSONDecodeError, ValidationError, or UnsupportedSchemaVersionError if invalid.
    """
    path = Path(filepath)
    if not path.exists():
        return {day: [] for day in WEEKDAYS}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return normalize_to_timetable_dict(data)


def save_timetable(data: Any, filepath=DEFAULT_FILEPATH) -> None:
    """Saves timetable data atomically to a JSON file in canonical format.

    Ensures data is validated and wrapped in canonical schema_version: 1.
    Writes to a temporary file in the destination directory and renames atomically.
    Acquires TimetableFileLock to ensure cross-process safety.
    """
    path = Path(filepath)
    with TimetableFileLock(path):
        path.parent.mkdir(parents=True, exist_ok=True)

        # Check destination if it exists: never overwrite an unsupported future schema version
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing_raw = json.load(f)
                if isinstance(existing_raw, dict) and "schema_version" in existing_raw:
                    ex_ver = existing_raw["schema_version"]
                    if isinstance(ex_ver, int) and not isinstance(ex_ver, bool) and ex_ver not in SUPPORTED_SCHEMA_VERSIONS:
                        raise UnsupportedSchemaVersionError(ex_ver)
            except (json.JSONDecodeError, OSError):
                pass

        # Prepare canonical document
        if isinstance(data, dict) and "schema_version" in data:
            validate_schema(data)
            doc = data
        else:
            # data is weekday dictionary (legacy or normalized)
            # Migrate or wrap
            doc = migrate_to_canonical(data)

        validate_schema(doc)

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(doc, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp_path), str(path))
            temp_path = None
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
