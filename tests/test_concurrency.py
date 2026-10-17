"""Tests for cross-process locking, revision tokens, and optimistic concurrency control."""

from __future__ import annotations

import copy
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from timetable.cli import main
from timetable.history import list_snapshots
from timetable.locking import (
    DEFAULT_LOCK_TIMEOUT,
    ConcurrentModificationError,
    LockTimeoutError,
    TimetableFileLock,
    canonical_lock_path,
    validate_timeout,
)
from timetable.revision import (
    REVISION_ABSENT,
    compute_revision,
    load_timetable_snapshot,
)
from timetable.schema import create_empty_canonical_timetable
from timetable.undo import commit_mutation, load_journal, undo_last, redo_last


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _sample_timetable():
    tt = create_empty_canonical_timetable()
    tt["monday"].append(
        {
            "subject": "Math 101",
            "start": "09:00",
            "end": "10:00",
            "room": "Room A",
        }
    )
    return tt


class TestRevisions:
    def test_compute_revision_absent(self):
        assert compute_revision(None) == REVISION_ABSENT

    def test_compute_revision_consistent_and_sensitive(self, temp_dir):
        b1 = b'{"hello": "world"}'
        b2 = b'{"hello": "world"}'
        b3 = b'{"hello": "world2"}'
        rev1 = compute_revision(b1)
        rev2 = compute_revision(b2)
        rev3 = compute_revision(b3)

        assert rev1 == rev2
        assert rev1 != rev3
        assert len(rev1) == 64

    def test_load_timetable_snapshot_missing_file(self, temp_dir):
        path = temp_dir / "nonexistent.json"
        snapshot = load_timetable_snapshot(path)
        assert snapshot.exists is False
        assert snapshot.revision == REVISION_ABSENT
        assert snapshot.raw_bytes is None
        assert "monday" in snapshot.data

    def test_load_timetable_snapshot_existing_valid(self, temp_dir):
        path = temp_dir / "timetable.json"
        data = _sample_timetable()
        raw_bytes = json.dumps(data, indent=2).encode("utf-8")
        path.write_bytes(raw_bytes)

        snapshot = load_timetable_snapshot(path)
        assert snapshot.exists is True
        assert snapshot.revision == compute_revision(raw_bytes)
        assert snapshot.raw_bytes == raw_bytes
        assert len(snapshot.data["monday"]) == 1

    def test_load_timetable_snapshot_malformed_json_validate_false(self, temp_dir):
        path = temp_dir / "corrupted.json"
        raw_bytes = b'{"invalid": json'
        path.write_bytes(raw_bytes)

        # Without validate=False, JSON error raises
        with pytest.raises(ValueError, match="not valid JSON"):
            load_timetable_snapshot(path, validate=True)

        with pytest.raises(ValueError, match="not valid JSON"):
            load_timetable_snapshot(path, validate=False)

    def test_load_timetable_snapshot_non_schema_json_validate_false(self, temp_dir):
        path = temp_dir / "arbitrary.json"
        raw_bytes = b'{"random_key": "arbitrary_val"}'
        path.write_bytes(raw_bytes)

        # validate=False should succeed and return revision
        snapshot = load_timetable_snapshot(path, validate=False)
        assert snapshot.exists is True
        assert snapshot.revision == compute_revision(raw_bytes)
        assert snapshot.raw_data == {"random_key": "arbitrary_val"}


class TestLocking:
    def test_canonical_lock_path(self, temp_dir):
        p1 = temp_dir / "timetable.json"
        p2 = temp_dir / "." / "timetable.json"
        assert canonical_lock_path(p1) == canonical_lock_path(p2)
        assert canonical_lock_path(p1).name == "timetable.json.lock"

    def test_validate_timeout(self):
        assert validate_timeout(None) == DEFAULT_LOCK_TIMEOUT
        assert validate_timeout(5.0) == 5.0
        assert validate_timeout("2.5") == 2.5
        with pytest.raises(ValueError):
            validate_timeout(0)
        with pytest.raises(ValueError):
            validate_timeout(-1)
        with pytest.raises(ValueError):
            validate_timeout("invalid")
        with pytest.raises(ValueError):
            validate_timeout(True)

    def test_reentrant_lock_in_same_thread(self, temp_dir):
        target = temp_dir / "timetable.json"
        lock1 = TimetableFileLock(target)
        lock2 = TimetableFileLock(target)

        with lock1:
            with lock2:
                # Reentrancy allows nested acquisition on the same thread
                assert lock1.lock_path.exists()

    def test_lock_release_never_deletes_lock_file(self, temp_dir):
        target = temp_dir / "timetable.json"
        lock = TimetableFileLock(target)
        with lock:
            assert lock.lock_path.exists()
        # Lock file persists after release to prevent file-object race
        assert lock.lock_path.exists()


class TestCrossProcessLocking:
    def test_lock_timeout_across_processes(self, temp_dir):
        target = temp_dir / "timetable.json"
        target.write_text("{}", encoding="utf-8")

        script = (
            "import sys, time\n"
            "from timetable.locking import TimetableFileLock\n"
            f"lock = TimetableFileLock(r'{str(target)}', timeout=5.0)\n"
            "with lock:\n"
            "    sys.stdout.write('LOCKED\\n')\n"
            "    sys.stdout.flush()\n"
            "    line = sys.stdin.readline()\n"
        )

        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            line = proc.stdout.readline().strip()
            assert line == "LOCKED"

            # Try to acquire in this process with a short timeout
            lock = TimetableFileLock(target, timeout=0.5)
            start = time.time()
            with pytest.raises(LockTimeoutError) as exc_info:
                with lock:
                    pass
            elapsed = time.time() - start
            assert elapsed >= 0.4
            assert "Timed out after 0.5s waiting for lock" in str(exc_info.value)
        finally:
            try:
                proc.stdin.write("RELEASE\n")
                proc.stdin.flush()
                proc.wait(timeout=3.0)
            except Exception:
                proc.kill()
                proc.wait()


class TestOptimisticLockingCommit:
    def test_commit_mutation_verifies_expected_revision(self, temp_dir):
        target = temp_dir / "timetable.json"
        initial_data = _sample_timetable()
        target.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

        snap = load_timetable_snapshot(target)
        initial_rev = snap.revision

        # Simulate concurrent modification by another process
        modified_data = _sample_timetable()
        modified_data["monday"].append(
            {"subject": "Concurrent Class", "start": "10:00", "end": "11:00", "room": "B"}
        )
        target.write_text(json.dumps(modified_data, indent=2), encoding="utf-8")

        # Now attempting commit with stale expected_revision must fail
        proposed = _sample_timetable()
        proposed["tuesday"] = [
            {"subject": "My Class", "start": "11:00", "end": "12:00", "room": "C"}
        ]

        with pytest.raises(ConcurrentModificationError) as exc_info:
            commit_mutation(
                target,
                "add",
                proposed,
                expected_revision=initial_rev,
            )

        err = exc_info.value
        assert err.expected_revision == initial_rev
        assert err.current_revision != initial_rev
        assert "modified by another process concurrently" in str(err)

        # Ensure history and undo journal were NOT updated
        history_dir = temp_dir / ".timetable-history"
        assert len(list_snapshots(history_dir)) == 0

    def test_commit_mutation_concurrent_creation_rejection(self, temp_dir):
        target = temp_dir / "new_timetable.json"
        # Snapshot says absent
        snap = load_timetable_snapshot(target)
        assert snap.revision == REVISION_ABSENT

        # Another process creates it before our commit
        target.write_text("{}", encoding="utf-8")

        proposed = _sample_timetable()
        with pytest.raises(ConcurrentModificationError) as exc_info:
            commit_mutation(
                target,
                "add",
                proposed,
                expected_revision=REVISION_ABSENT,
            )
        assert "created by another process concurrently" in str(exc_info.value)

    def test_commit_mutation_success_updates_history_and_file(self, temp_dir):
        target = temp_dir / "timetable.json"
        initial_data = _sample_timetable()
        target.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

        snap = load_timetable_snapshot(target)
        proposed = _sample_timetable()
        proposed["monday"].append(
            {"subject": "Math 102", "start": "14:00", "end": "15:00", "room": "Room B"}
        )

        commit_mutation(
            target,
            "add",
            proposed,
            expected_revision=snap.revision,
        )

        # File updated
        updated_snap = load_timetable_snapshot(target)
        assert updated_snap.revision != snap.revision
        assert len(updated_snap.data["monday"]) == 2

        # Undo journal updated
        journal = load_journal(target)
        assert len(journal["undo"]) == 1

        # Undo works
        undo_last(target)
        reverted_snap = load_timetable_snapshot(target)
        assert len(reverted_snap.data["monday"]) == 1


class TestCliConcurrencyProtection:
    def run_cli(self, *argv):
        out, err = StringIO(), StringIO()
        old_argv = sys.argv
        sys.argv = ["timetable"] + list(argv)
        code = 0
        try:
            with patch("sys.stdout", out), patch("sys.stderr", err):
                main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        finally:
            sys.argv = old_argv
        return code, out.getvalue(), err.getvalue()

    def test_cli_add_concurrent_modification(self, temp_dir):
        target = temp_dir / "timetable.json"
        target.write_text(json.dumps(_sample_timetable(), indent=2), encoding="utf-8")

        # Monkeypatch load_timetable_snapshot during cmd_add to simulate race:
        # File gets changed right after initial read
        original_load = load_timetable_snapshot

        def raced_load(filepath, **kwargs):
            snap = original_load(filepath, **kwargs)
            # Mutate file immediately after read
            target.write_text('{"modified": true}', encoding="utf-8")
            return snap

        with patch("timetable.cli.load_timetable_snapshot", side_effect=raced_load):
            code, out, err = self.run_cli(
                "--file",
                str(target),
                "add",
                "--day",
                "tuesday",
                "--subject",
                "Physics",
                "--start",
                "10:00",
                "--end",
                "11:00",
                "--room",
                "Room 1",
            )

        assert code == 1
        assert "Error: Timetable" in err
        assert "modified by another process concurrently" in err
        assert "reload and retry" in err

    def test_cli_import_csv_concurrent_modification(self, temp_dir):
        target = temp_dir / "timetable.json"
        target.write_text(json.dumps(_sample_timetable(), indent=2), encoding="utf-8")

        csv_file = temp_dir / "classes.csv"
        csv_file.write_text(
            "day,subject,start,end,room\ntuesday,Chemistry,11:00,12:00,Lab 1\n",
            encoding="utf-8",
        )

        original_load = load_timetable_snapshot

        def raced_load(filepath, **kwargs):
            snap = original_load(filepath, **kwargs)
            target.write_text('{"modified": "by other"}', encoding="utf-8")
            return snap

        with patch("timetable.cli.load_timetable_snapshot", side_effect=raced_load):
            code, out, err = self.run_cli(
                "--file",
                str(target),
                "import-csv",
                str(csv_file),
            )

        assert code == 1
        assert "modified by another process concurrently" in err

    def test_cli_merge_concurrent_modification(self, temp_dir):
        f1 = temp_dir / "tt1.json"
        f2 = temp_dir / "tt2.json"
        out_f = temp_dir / "out.json"

        f1.write_text(json.dumps(_sample_timetable(), indent=2), encoding="utf-8")
        f2.write_text(json.dumps(_sample_timetable(), indent=2), encoding="utf-8")
        out_f.write_text(json.dumps(_sample_timetable(), indent=2), encoding="utf-8")

        original_load = load_timetable_snapshot

        def raced_load(filepath, **kwargs):
            snap = original_load(filepath, **kwargs)
            if Path(filepath).resolve() == out_f.resolve():
                out_f.write_text('{"concurrent": "edit"}', encoding="utf-8")
            return snap

        with patch("timetable.cli.load_timetable_snapshot", side_effect=raced_load):
            code, out, err = self.run_cli(
                "merge",
                str(f1),
                str(f2),
                "--output",
                str(out_f),
                "--force",
            )

        assert code == 1
        assert "modified by another process concurrently" in err

    def test_cli_lock_timeout_handling(self, temp_dir):
        target = temp_dir / "timetable.json"
        target.write_text(json.dumps(_sample_timetable(), indent=2), encoding="utf-8")

        # Mock TimetableFileLock.acquire to raise LockTimeoutError
        with patch.object(
            TimetableFileLock,
            "acquire",
            side_effect=LockTimeoutError(target, 10.0),
        ):
            code, out, err = self.run_cli(
                "--file",
                str(target),
                "add",
                "--day",
                "tuesday",
                "--subject",
                "Physics",
                "--start",
                "10:00",
                "--end",
                "11:00",
                "--room",
                "Room 1",
            )

        assert code == 1
        assert "Error: Timed out after 10.0s waiting for lock" in err

    def test_canonical_versioned_timetable_undo_redo_concurrency(self, temp_dir):
        """Ensure commit_mutation, undo_last, redo_last work on canonical versioned timetables."""
        from timetable.undo import commit_mutation, undo_last, redo_last
        target = temp_dir / "canonical_tt.json"
        initial_data = {
            "schema_version": 1,
            **_sample_timetable(),
            "metadata": {"academic_term": "Spring 2027"},
        }
        target.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

        snap = load_timetable_snapshot(target)
        proposed = _sample_timetable()
        proposed["monday"].append(
            {"subject": "Advanced ML", "start": "13:00", "end": "14:00", "room": "Lab 4"}
        )

        # Commit mutation with expected_revision
        commit_mutation(
            target,
            "add",
            proposed,
            expected_revision=snap.revision,
        )

        saved = json.loads(target.read_text(encoding="utf-8"))
        assert saved["schema_version"] == 1
        assert saved["monday"][-1]["subject"] == "Advanced ML"

        # Undo restores original exact bytes and schema
        undo_last(target)
        assert target.read_bytes() == snap.raw_bytes

        # Redo reapplies the change
        redo_last(target)
        redone = json.loads(target.read_text(encoding="utf-8"))
        assert redone["schema_version"] == 1
        assert redone["monday"][-1]["subject"] == "Advanced ML"


    def test_restore_snapshot_stale_write_rejection(self, temp_dir):
        """Ensure restore_snapshot rejects restoring onto a stale destination revision."""
        from timetable.history import save_snapshot, restore_snapshot, history_directory
        target = temp_dir / "restore_test.json"
        initial_data = _sample_timetable()
        target.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

        hd = history_directory(target)
        snap_id = save_snapshot(initial_data, filepath=target, operation="init", history_dir=hd)

        # Read snapshot
        initial_snap = load_timetable_snapshot(target)

        # External concurrent modification changes target
        target.write_text('{"externally": "modified"}', encoding="utf-8")

        # Attempt to restore passing stale expected_revision
        with pytest.raises(ConcurrentModificationError) as exc_info:
            restore_snapshot(
                snap_id,
                filepath=target,
                history_dir=hd,
                expected_revision=initial_snap.revision,
            )
        assert "modified by another process concurrently" in str(exc_info.value)
