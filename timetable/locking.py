"""Cross-process file locking utilities for timetable persistence."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Sequence, Union

import filelock

#: Default lock timeout in seconds.
DEFAULT_LOCK_TIMEOUT = 10.0

#: Environment variable to override the default lock timeout.
ENV_LOCK_TIMEOUT = "TIMETABLE_LOCK_TIMEOUT"


class ConcurrencyError(Exception):
    """Base class for concurrency and locking errors."""


class LockTimeoutError(ConcurrencyError):
    """Raised when a cross-process lock cannot be acquired within the timeout."""

    def __init__(self, target_path: Union[str, Path], timeout: float):
        self.target_path = str(target_path)
        self.timeout = timeout
        super().__init__(
            f"Timed out after {timeout:.1f}s waiting for lock on '{self.target_path}'. "
            "Another process may be updating the timetable."
        )


class ConcurrentModificationError(ConcurrencyError):
    """Raised when a timetable file has been modified concurrently (stale revision)."""

    def __init__(
        self,
        target_path: Union[str, Path],
        expected_revision: Optional[str] = None,
        current_revision: Optional[str] = None,
        message: Optional[str] = None,
    ):
        self.target_path = str(target_path)
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        if message is None:
            if expected_revision == "absent":
                msg = (
                    f"Timetable '{self.target_path}' was created by another process concurrently. "
                    "Please reload and retry your operation."
                )
            elif current_revision == "absent":
                msg = (
                    f"Timetable '{self.target_path}' was deleted by another process concurrently. "
                    "Please reload and retry your operation."
                )
            else:
                msg = (
                    f"Timetable '{self.target_path}' was modified by another process concurrently. "
                    "Please reload and retry your operation."
                )
        else:
            msg = message
        super().__init__(msg)


def canonical_lock_path(target_path: Union[str, Path]) -> Path:
    """Return the canonical lock file path for a given target timetable path.

    Normalizes the path across relative, absolute, and case differences so
    that all processes targeting the same filesystem object resolve to the
    same lock file.
    """
    resolved = Path(target_path).resolve()
    # On Windows, path casing should be normalized to avoid different lock files
    # for differently-cased paths pointing to the same file.
    norm_str = os.path.normcase(str(resolved))
    norm_path = Path(norm_str)
    # Store lock file beside the target with .lock suffix:
    # e.g. timetable.json.lock
    return norm_path.parent / f"{norm_path.name}.lock"


def get_default_timeout() -> float:
    """Read default lock timeout from environment variable or return default."""
    val = os.environ.get(ENV_LOCK_TIMEOUT)
    if val is not None:
        try:
            parsed = float(val)
            if parsed <= 0:
                return DEFAULT_LOCK_TIMEOUT
            return parsed
        except ValueError:
            return DEFAULT_LOCK_TIMEOUT
    return DEFAULT_LOCK_TIMEOUT


def validate_timeout(timeout: Optional[float]) -> float:
    """Validate and return timeout value, raising ValueError if invalid."""
    if timeout is None:
        return get_default_timeout()
    if isinstance(timeout, bool):
        raise ValueError(f"Lock timeout must be a positive number, got {timeout!r}")
    try:
        t = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Lock timeout must be a positive number, got {timeout!r}") from exc
    if t <= 0:
        raise ValueError(f"Lock timeout must be positive, got {timeout}")
    return t


import threading

_LOCK_REGISTRY: dict[Path, tuple[filelock.FileLock, threading.RLock]] = {}
_REGISTRY_LOCK = threading.Lock()


def _get_lock_instances(lock_path: Path) -> tuple[filelock.FileLock, threading.RLock]:
    with _REGISTRY_LOCK:
        if lock_path not in _LOCK_REGISTRY:
            fl = filelock.FileLock(str(lock_path))
            # On Windows, filelock's default _release unlinks the lock file.
            # To strictly prevent file-descriptor/inode reuse races and satisfy
            # "Do not delete a persistent lock file on every release", we override
            # _release to unlock and close the fd without unlinking.
            if hasattr(fl, "_release") and sys.platform == "win32":
                orig_release = fl._release

                def _no_unlink_release(self=fl) -> None:
                    fd = getattr(self._context, "lock_file_fd", None)
                    if fd is not None:
                        self._context.lock_file_fd = None
                        import msvcrt
                        try:
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        finally:
                            os.close(fd)

                fl._release = _no_unlink_release  # type: ignore[assignment]

            _LOCK_REGISTRY[lock_path] = (fl, threading.RLock())
        return _LOCK_REGISTRY[lock_path]


class TimetableFileLock:
    """Cross-process file lock protecting a timetable and its metadata.

    Uses `filelock.FileLock` which leverages OS-level primitives (msvcrt on Windows,
    fcntl on POSIX) guaranteeing cross-process mutual exclusion.
    The lock file is intentionally not deleted on release to prevent the race condition
    where a newly creating process locks a different filesystem object.
    Reentrant across function calls within the same thread.
    """

    def __init__(
        self,
        target_path: Union[str, Path],
        timeout: Optional[float] = None,
    ):
        self.target_path = Path(target_path)
        self.timeout = validate_timeout(timeout)
        self.lock_path = canonical_lock_path(self.target_path)
        # Ensure parent directory exists for lock file
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._flock, self._thread_lock = _get_lock_instances(self.lock_path)
        self._acquired_thread = False

    def acquire(self, timeout: Optional[float] = None) -> None:
        """Acquire the lock within the timeout or raise LockTimeoutError."""
        actual_timeout = validate_timeout(timeout) if timeout is not None else self.timeout
        # Acquire thread lock first
        if not self._thread_lock.acquire(timeout=actual_timeout):
            raise LockTimeoutError(self.target_path, actual_timeout)

        try:
            self._flock.acquire(timeout=actual_timeout)
            self._acquired_thread = True
        except filelock.Timeout as exc:
            self._thread_lock.release()
            raise LockTimeoutError(self.target_path, actual_timeout) from exc
        except Exception:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        """Release the lock."""
        if self._acquired_thread:
            try:
                self._flock.release()
            finally:
                self._acquired_thread = False
                self._thread_lock.release()

    def __enter__(self) -> TimetableFileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


class MultiFileLock:
    """Acquires and releases multiple TimetableFileLocks in deterministic global order.

    - Resolves and canonicalizes all paths.
    - Sorts unique canonical lock paths to prevent lock-order deadlocks.
    - Deduplicates identical/equivalent paths so files are not locked multiple times.
    - If any lock acquisition fails or times out, immediately releases all
      previously acquired locks before raising the exception.
    """

    def __init__(
        self,
        paths: Sequence[Union[str, Path]],
        timeout: Optional[float] = None,
    ):
        self.timeout = validate_timeout(timeout)
        # Deduplicate paths by canonical lock path while preserving deterministic order
        unique_targets: dict[Path, Path] = {}
        for p in paths:
            if p is None:
                continue
            res_p = Path(p).resolve()
            clp = canonical_lock_path(res_p)
            if clp not in unique_targets:
                unique_targets[clp] = res_p

        # Sort deterministically by canonical lock path string (os.path.normcase)
        sorted_pairs = sorted(
            unique_targets.items(),
            key=lambda item: os.path.normcase(str(item[0])),
        )
        self.locks = [
            TimetableFileLock(target, timeout=self.timeout)
            for _, target in sorted_pairs
        ]
        self.acquired_locks: list[TimetableFileLock] = []

    def acquire(self, timeout: Optional[float] = None) -> None:
        """Acquire all locks in sorted order, rolling back completely on failure."""
        actual_timeout = validate_timeout(timeout) if timeout is not None else self.timeout
        self.acquired_locks = []
        try:
            for lock in self.locks:
                lock.acquire(timeout=actual_timeout)
                self.acquired_locks.append(lock)
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        """Release all acquired locks in reverse order."""
        errors = []
        while self.acquired_locks:
            lock = self.acquired_locks.pop()
            try:
                lock.release()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def __enter__(self) -> MultiFileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

