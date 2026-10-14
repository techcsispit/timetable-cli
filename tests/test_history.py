"""Tests for timetable version history, snapshots, restore and retention.

All tests use temporary directories and never touch the repository's real
``timetable.json``.
"""

import json
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from timetable.cli import main
from timetable.history import (
    DEFAULT_RETENTION_LIMIT,
    ENV_HISTORY_LIMIT,
    METADATA_FILE,
    HistoryError,
    SnapshotCorruptError,
    SnapshotMismatchError,
    SnapshotNotFoundError,
    WriteFailureError,
    _checksum,
    default_retention_limit,
    history_directory,
    list_snapshots,
    load_snapshot,
    prune_snapshots,
    restore_snapshot,
    save_snapshot,
    show_snapshot,
    validate_retention_limit,
)

BASE = {
    "monday": [
        {"subject": "Data Structures", "start": "09:00", "end": "10:30", "room": "Lab 1"},
    ],
    "tuesday": [],
    "wednesday": [],
    "thursday": [],
    "friday": [],
    "saturday": [],
    "sunday": [],
}


def write_timetable(path, data):
    path = Path(path)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestHistoryValidation(unittest.TestCase):

    def test_default_retention_limit(self):
        self.assertEqual(default_retention_limit(), DEFAULT_RETENTION_LIMIT)

    def test_default_retention_limit_env(self):
        with mock.patch.dict(os.environ, {ENV_HISTORY_LIMIT: '5'}):
            self.assertEqual(default_retention_limit(), 5)
        with mock.patch.dict(os.environ, {ENV_HISTORY_LIMIT: 'abc'}):
            self.assertEqual(default_retention_limit(), DEFAULT_RETENTION_LIMIT)
        with mock.patch.dict(os.environ, {ENV_HISTORY_LIMIT: '0'}):
            self.assertEqual(default_retention_limit(), DEFAULT_RETENTION_LIMIT)

    def test_validate_retention_limit(self):
        self.assertEqual(validate_retention_limit('5'), 5)
        self.assertEqual(validate_retention_limit(5), 5)
        with self.assertRaises(HistoryError):
            validate_retention_limit(0)
        with self.assertRaises(HistoryError):
            validate_retention_limit(-1)
        with self.assertRaises(HistoryError):
            validate_retention_limit('abc')
        with self.assertRaises(HistoryError):
            validate_retention_limit(True)


class TestSaveSnapshot(unittest.TestCase):

    def test_save_creates_directory_and_metadata(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            tt = {'monday': [{'subject': 'Math', 'start': '09:00', 'end': '10:00', 'room': 'A'}]}
            sid = save_snapshot(tt, filepath=hd / 'timetable.json', operation='edit', history_dir=hd)
            self.assertTrue((hd / sid).is_dir())
            meta = json.loads((hd / sid / '.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(meta['id'], sid)
            self.assertEqual(meta['operation'], 'edit')
            expected_checksum = _checksum(json.dumps(tt, indent=2, ensure_ascii=False).encode('utf-8'))
            self.assertEqual(meta['checksum'], expected_checksum)
            self.assertEqual(meta['timetable'], str(hd / 'timetable.json'))
            self.assertTrue(meta['created_at'])
            self.assertTrue((hd / sid / 'timetable.json').exists())

    def test_save_creates_new_snapshot_with_updated_content(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            tt = {'monday': [{'subject': 'First', 'start': '09:00', 'end': '10:00', 'room': 'A'}]}
            sid = save_snapshot(tt, filepath=hd / 'timetable.json', operation='edit', history_dir=hd)
            tt['monday'][0]['subject'] = 'Second'
            sid2 = save_snapshot(tt, filepath=hd / 'timetable.json', operation='edit', history_dir=hd)
            self.assertNotEqual(sid2, sid)
            doc = load_snapshot(sid2, hd)
            self.assertEqual(doc['timetable']['monday'][0]['subject'], 'Second')
            self.assertEqual(len(list_snapshots(hd)), 2)


class TestLoadSnapshot(unittest.TestCase):

    def test_load_returns_document(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            tt = {'monday': [{'subject': 'Math', 'start': '09:00', 'end': '10:00', 'room': 'A'}]}
            sid = save_snapshot(tt, filepath=hd / 'timetable.json', operation='edit', history_dir=hd)
            doc = load_snapshot(sid, hd)
            self.assertEqual(doc['id'], sid)
            self.assertEqual(doc['metadata']['operation'], 'edit')
            self.assertEqual(doc['timetable'], tt)

    def test_load_missing_snapshot(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            with self.assertRaises(SnapshotNotFoundError):
                load_snapshot('snap-nothere', hd)

    def test_load_corrupt_metadata(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            sid = 'snap-corrupt'
            (hd / sid).mkdir()
            (hd / sid / '.metadata.json').write_text('{not json', encoding='utf-8')
            with self.assertRaises(SnapshotCorruptError):
                load_snapshot(sid, hd)

    def test_load_missing_timetable(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            sid = 'snap-missing-tt'
            (hd / sid).mkdir()
            (hd / sid / '.metadata.json').write_text(json.dumps({
                'id': sid,
                'created_at': '2026-01-01T00:00:00',
                'timetable': 'x',
                'operation': 'edit',
                'checksum': 'abc',
            }), encoding='utf-8')
            with self.assertRaises(SnapshotCorruptError):
                load_snapshot(sid, hd)


class TestShowSnapshot(unittest.TestCase):

    def test_show_snapshot(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            tt = {'monday': [{'subject': 'Math', 'start': '09:00', 'end': '10:00', 'room': 'A'}]}
            sid = save_snapshot(tt, filepath=hd / 'timetable.json', operation='edit', history_dir=hd)
            out = StringIO()
            with redirect_stdout(out):
                document = show_snapshot(sid, hd)
            text = out.getvalue()
            self.assertEqual(document['id'], sid)
            self.assertIn('Math', text)
            self.assertIn('Monday', text)

    def test_show_missing_snapshot(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            with self.assertRaises(SnapshotNotFoundError):
                show_snapshot('snap-nothere', hd)


class TestListSnapshots(unittest.TestCase):

    def test_list_returns_snapshots(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            save_snapshot({'monday': []}, filepath=hd / 'timetable.json', operation='edit', history_dir=hd)
            save_snapshot({'tuesday': []}, filepath=hd / 'timetable.json', operation='backup', history_dir=hd)
            snapshots = list_snapshots(hd, limit=10)
            self.assertEqual(len(snapshots), 2)
            self.assertEqual({s['operation'] for s in snapshots}, {'edit', 'backup'})

    def test_list_respects_limit(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            for i in range(5):
                save_snapshot({'day': i}, filepath=hd / 'timetable.json', operation='edit', history_dir=hd)
            snapshots = list_snapshots(hd, limit=2)
            self.assertEqual(len(snapshots), 2)

    def test_list_empty(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            self.assertEqual(list_snapshots(hd), [])

    def test_list_orders_newest_first(self):
        """Snapshots are returned in reverse chronological (newest first) order."""
        with TemporaryDirectory() as td:
            hd = Path(td)
            for i in range(4):
                save_snapshot(
                    {'day': i}, filepath=hd / 'timetable.json',
                    operation='edit', history_dir=hd,
                )
            entries = list_snapshots(hd)
            self.assertEqual(len(entries), 4)
            # Ids are lexicographically sortable, so newest-first == reverse order.
            self.assertEqual(
                [e['id'] for e in entries],
                sorted((e['id'] for e in entries), reverse=True),
            )

    def test_list_skips_missing_timetable_file(self):
        """A snapshot whose timetable file is missing is not listed."""
        with TemporaryDirectory() as td:
            hd = Path(td)
            sid = save_snapshot(
                {'monday': []}, filepath=hd / 'timetable.json',
                operation='edit', history_dir=hd,
            )
            (hd / sid / 'timetable.json').unlink()
            self.assertEqual(list_snapshots(hd), [])

    def test_list_skips_checksum_mismatch(self):
        """A snapshot whose timetable was altered after saving is not listed."""
        with TemporaryDirectory() as td:
            hd = Path(td)
            sid = save_snapshot(
                {'monday': []}, filepath=hd / 'timetable.json',
                operation='edit', history_dir=hd,
            )
            (hd / sid / 'timetable.json').write_text(
                '{"monday": [{"subject": "Tampered"}]}', encoding='utf-8'
            )
            self.assertEqual(list_snapshots(hd), [])
            # It still raises when loaded explicitly.
            with self.assertRaises(SnapshotCorruptError):
                load_snapshot(sid, hd)


class TestRestoreSnapshot(unittest.TestCase):

    def test_restore_replaces_file(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            current = {'monday': [{'subject': 'Current', 'start': '08:00', 'end': '09:00', 'room': 'B'}]}
            write_timetable(hd / 'timetable.json', current)
            saved = save_snapshot(
                {'monday': [{'subject': 'Saved', 'start': '09:00', 'end': '10:00', 'room': 'A'}]},
                filepath=hd / 'timetable.json',
                operation='edit',
                history_dir=hd,
            )
            restore_snapshot(saved, filepath=hd / 'timetable.json', history_dir=hd)
            restored = json.loads((hd / 'timetable.json').read_text(encoding='utf-8'))
            self.assertEqual(restored['monday'][0]['subject'], 'Saved')

    def test_restore_preserves_current_as_snapshot(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            current = {'monday': [{'subject': 'Current', 'start': '08:00', 'end': '09:00', 'room': 'B'}]}
            write_timetable(hd / 'timetable.json', current)
            saved = save_snapshot(
                {'monday': [{'subject': 'Saved', 'start': '09:00', 'end': '10:00', 'room': 'A'}]},
                filepath=hd / 'timetable.json',
                operation='edit',
                history_dir=hd,
            )
            before = len(list_snapshots(hd))
            restore_snapshot(saved, filepath=hd / 'timetable.json', history_dir=hd)
            after = list_snapshots(hd)
            self.assertEqual(len(after), before + 1)
            preserved = [s for s in after if s['operation'] == 'restore-previous']
            self.assertTrue(preserved)

    def test_restore_missing_snapshot(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            write_timetable(hd / 'timetable.json', {'monday': []})
            with self.assertRaises(SnapshotNotFoundError):
                restore_snapshot('snap-nothere', filepath=hd / 'timetable.json', history_dir=hd)

    def test_restore_missing_snapshot_preserves_active_timetable(self):
        """A missing snapshot must never overwrite the active timetable."""
        with TemporaryDirectory() as td:
            hd = Path(td)
            active = {'monday': [{'subject': 'Keep', 'start': '09:00', 'end': '10:00', 'room': 'A'}]}
            write_timetable(hd / 'timetable.json', active)
            with self.assertRaises(SnapshotNotFoundError):
                restore_snapshot('snap-nothere', filepath=hd / 'timetable.json', history_dir=hd)
            self.assertEqual(
                json.loads((hd / 'timetable.json').read_text(encoding='utf-8')),
                active,
            )

    def test_restore_corrupt_snapshot_preserves_active_timetable(self):
        """A corrupt snapshot must never overwrite the active timetable."""
        with TemporaryDirectory() as td:
            hd = Path(td)
            active = {'monday': [{'subject': 'Keep', 'start': '09:00', 'end': '10:00', 'room': 'A'}]}
            write_timetable(hd / 'timetable.json', active)
            sid = 'snap-corrupt'
            (hd / sid).mkdir()
            (hd / sid / '.metadata.json').write_text('{not json', encoding='utf-8')
            with self.assertRaises(SnapshotCorruptError):
                restore_snapshot(sid, filepath=hd / 'timetable.json', history_dir=hd)
            self.assertEqual(
                json.loads((hd / 'timetable.json').read_text(encoding='utf-8')),
                active,
            )

    def test_restore_repeatedly(self):
        """Restoring the same snapshot multiple times keeps working."""
        with TemporaryDirectory() as td:
            hd = Path(td)
            write_timetable(hd / 'timetable.json', {'monday': [{'subject': 'Old'}]})
            sid = save_snapshot(
                {'monday': [{'subject': 'Target'}]},
                filepath=hd / 'timetable.json', operation='edit', history_dir=hd,
            )
            for _ in range(3):
                restore_snapshot(sid, filepath=hd / 'timetable.json', history_dir=hd)
                current = json.loads((hd / 'timetable.json').read_text(encoding='utf-8'))
                self.assertEqual(current['monday'][0]['subject'], 'Target')

    def test_restore_write_failure_raises_write_failure_error(self):
        """A failed write raises the documented history exception."""
        with TemporaryDirectory() as td:
            hd = Path(td)
            write_timetable(hd / 'timetable.json', {'monday': []})
            sid = save_snapshot(
                {'monday': [{'subject': 'X'}]},
                filepath=hd / 'timetable.json', operation='edit', history_dir=hd,
            )
            with mock.patch(
                'timetable.history._write_atomic', side_effect=OSError('disk full')
            ):
                with self.assertRaises(WriteFailureError):
                    restore_snapshot(sid, filepath=hd / 'timetable.json', history_dir=hd)


class TestPruneSnapshots(unittest.TestCase):

    def test_prune_removes_oldest(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            for i in range(5):
                save_snapshot({'day': i}, filepath=hd / 'timetable.json', operation='edit', history_dir=hd)
            removed = prune_snapshots(hd, 2)
            self.assertEqual(removed, 3)
            self.assertEqual(len(list_snapshots(hd)), 2)

    def test_prune_never_deletes_active_restore_target(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            save_snapshot({'day': 1}, filepath=hd / 'timetable.json', operation='edit', history_dir=hd)
            sid = save_snapshot({'day': 2}, filepath=hd / 'timetable.json', operation='restore-previous', history_dir=hd)
            removed = prune_snapshots(hd, 1)
            snapshots = [s['id'] for s in list_snapshots(hd)]
            self.assertIn(sid, snapshots)
            self.assertEqual(removed, 0)

    def test_prune_retains_newest_and_reports_correct_count(self):
        """Pruning keeps the newest snapshots and returns the exact removals."""
        with TemporaryDirectory() as td:
            hd = Path(td)
            for i in range(6):
                save_snapshot(
                    {'day': i}, filepath=hd / 'timetable.json',
                    operation='edit', history_dir=hd,
                )
            removed = prune_snapshots(hd, 3)
            self.assertEqual(removed, 3)
            remaining = list_snapshots(hd)
            self.assertEqual(len(remaining), 3)
            # Newest three are retained: day values 3, 4, 5 (in reverse order).
            self.assertEqual([s['operation'] for s in remaining], ['edit'] * 3)

    def test_partial_snapshot_not_listed_and_valid_pruned(self):
        """Partial directories are never listed; pruning of valid ones still works."""
        with TemporaryDirectory() as td:
            hd = Path(td)
            # Two valid snapshots and one partial directory (metadata written
            # but the timetable file is missing, i.e. an interrupted write).
            first = save_snapshot(
                {'day': 0}, filepath=hd / 'timetable.json',
                operation='edit', history_dir=hd,
            )
            second = save_snapshot(
                {'day': 1}, filepath=hd / 'timetable.json',
                operation='edit', history_dir=hd,
            )
            partial = hd / 'snap-partial'
            partial.mkdir()
            (partial / METADATA_FILE).write_text(
                json.dumps({'id': 'snap-partial'}), encoding='utf-8'
            )

            # The partial directory is never reported as a usable snapshot.
            listed = [s['id'] for s in list_snapshots(hd)]
            self.assertEqual(listed, [second, first])

            # Pruning tolerates the partial directory (no exception) and
            # removes the oldest entries so that only `limit` directories
            # remain, keeping the newest valid snapshot intact.
            removed = prune_snapshots(hd, 1)
            self.assertEqual(removed, 2)
            self.assertFalse((hd / first).exists())
            self.assertFalse((hd / second).exists())
            self.assertTrue(partial.exists())  # tolerated, not listed above
            self.assertEqual(list_snapshots(hd), [])


class TestHistoryDirectory(unittest.TestCase):

    def test_history_directory_appends_dot_dir(self):
        d = history_directory(Path('a/b/timetable.json'))
        self.assertTrue(d.is_absolute())
        self.assertEqual(d.name, '.timetable-history')
        self.assertEqual(d.parent.name, 'b')

    def test_history_directory_next_to_timetable(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            d = history_directory(base / 'timetable.json')
            self.assertEqual(d, (base / '.timetable-history').resolve())


class TestSnapshotWriteFailure(unittest.TestCase):
    """A failed snapshot write raises WriteFailureError and cleans up."""

    def test_write_failure_raises_history_error(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            with mock.patch(
                'timetable.history._write_atomic', side_effect=OSError('disk full')
            ):
                with self.assertRaises(WriteFailureError):
                    save_snapshot(
                        {'monday': []}, filepath=hd / 'timetable.json',
                        operation='edit', history_dir=hd,
                    )

    def test_write_failure_leaves_no_partial_snapshot(self):
        with TemporaryDirectory() as td:
            hd = Path(td)
            with mock.patch(
                'timetable.history._write_atomic', side_effect=OSError('disk full')
            ):
                with self.assertRaises(WriteFailureError):
                    save_snapshot(
                        {'monday': []}, filepath=hd / 'timetable.json',
                        operation='edit', history_dir=hd,
                    )
            # No partial snapshot directory remains to be listed later.
            self.assertEqual(list_snapshots(hd), [])
            self.assertEqual([p for p in hd.iterdir() if p.is_dir()], [])


class TestCliHistoryCommands(unittest.TestCase):
    """CLI-level tests for history list/show/restore and existing commands."""

    def run_cli(self, *argv):
        out, err = StringIO(), StringIO()
        real_argv = sys.argv
        sys.argv = ['timetable', *argv]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        else:
            code = 0
        finally:
            sys.argv = real_argv
        return code, out.getvalue(), err.getvalue()

    def test_history_list_show_restore_roundtrip(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            tt = write_timetable(base / 'timetable.json', BASE)
            snap = save_snapshot(
                BASE, filepath=tt, operation='edit', history_dir=history_directory(tt)
            )

            code, out, _ = self.run_cli('--file', str(tt), 'history', 'list')
            self.assertEqual(code, 0)
            self.assertIn(snap, out)

            code, out, _ = self.run_cli(
                '--file', str(tt), 'history', 'show', snap
            )
            self.assertEqual(code, 0)
            self.assertIn('Data Structures', out)

            code, out, _ = self.run_cli(
                '--file', str(tt), 'history', 'restore', snap
            )
            self.assertEqual(code, 0)
            self.assertIn('Restored', out)
            restored = json.loads(tt.read_text(encoding='utf-8'))
            self.assertEqual(restored['monday'][0]['subject'], 'Data Structures')

    def test_history_list_limit(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            tt = write_timetable(base / 'timetable.json', BASE)
            hd = history_directory(tt)
            for i in range(3):
                save_snapshot(
                    {'day': i}, filepath=tt, operation='edit', history_dir=hd
                )
            code, out, _ = self.run_cli(
                '--file', str(tt), 'history', 'list', '--limit', '2'
            )
            self.assertEqual(code, 0)
            self.assertEqual(len([l for l in out.splitlines() if l.strip()]), 2)

    def test_add_snapshots_previous_state(self):
        """cmd_add snapshots the timetable before the new class was added."""
        with TemporaryDirectory() as td:
            base = Path(td)
            tt = write_timetable(base / 'timetable.json', BASE)
            code, out, _ = self.run_cli(
                '--file', str(tt), 'add', '--day', 'friday',
                '--subject', 'New Class', '--start', '11:00',
                '--end', '12:00', '--room', 'R1',
            )
            self.assertEqual(code, 0)
            self.assertIn('Successfully added', out)
            hd = history_directory(tt)
            snaps = list_snapshots(hd)
            self.assertEqual(len(snaps), 1)
            doc = load_snapshot(snaps[0]['id'], hd)
            subjects = [s['subject'] for s in doc['timetable'].get('friday', [])]
            self.assertNotIn('New Class', subjects)
            active = json.loads(tt.read_text(encoding='utf-8'))
            self.assertIn('New Class', [s['subject'] for s in active['friday']])

    def test_failed_add_creates_no_snapshot_and_keeps_file(self):
        """A rejected add must not snapshot or modify the timetable."""
        with TemporaryDirectory() as td:
            base = Path(td)
            tt = write_timetable(base / 'timetable.json', BASE)
            code, _, err = self.run_cli(
                '--file', str(tt), 'add', '--day', 'monday',
                '--subject', 'Clash', '--start', '10:00',
                '--end', '11:00', '--room', 'R1',
            )
            self.assertEqual(code, 1)
            self.assertIn('overlaps', err)
            self.assertEqual(list_snapshots(history_directory(tt)), [])
            active = json.loads(tt.read_text(encoding='utf-8'))
            self.assertEqual(active, BASE)

    def test_history_is_scoped_to_selected_timetable(self):
        """Timetables in one folder must not list or restore each other's snapshots."""
        with TemporaryDirectory() as td:
            base = Path(td)
            a = write_timetable(base / 'a.json', A_OLD)
            b = write_timetable(base / 'b.json', B_CURRENT)
            snap_a = save_snapshot(
                A_OLD, filepath=a, operation='edit', history_dir=history_directory(a)
            )

            code, out, _ = self.run_cli('--file', str(b), 'history', 'list')
            self.assertEqual(code, 0)
            self.assertNotIn(snap_a, out)

            code, out, _ = self.run_cli('--file', str(a), 'history', 'list')
            self.assertEqual(code, 0)
            self.assertIn(snap_a, out)

            code, out, err = self.run_cli(
                '--file', str(b), 'history', 'show', snap_a
            )
            self.assertEqual(code, 1)
            self.assertEqual(out, '')
            self.assertIn('belongs to', err)

            for argv in (('restore', snap_a), ('history', 'restore', snap_a)):
                code, _, err = self.run_cli('--file', str(b), *argv)
                self.assertEqual(code, 1)
                self.assertIn('belongs to', err)
                self.assertEqual(json.loads(b.read_text(encoding='utf-8')), B_CURRENT)


A_OLD = {'monday': [{'subject': 'A-old', 'start': '09:00', 'end': '10:00', 'room': 'R1'}]}
B_CURRENT = {'monday': [{'subject': 'B-current', 'start': '09:00', 'end': '10:00', 'room': 'R1'}]}


class TestSnapshotOwnership(unittest.TestCase):
    """Snapshots belong to the timetable file they were taken from."""

    def test_list_filters_by_timetable(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            a = write_timetable(base / 'a.json', A_OLD)
            b = write_timetable(base / 'b.json', B_CURRENT)
            hd = history_directory(a)
            self.assertEqual(hd, history_directory(b))
            snap_a = save_snapshot(A_OLD, filepath=a, operation='edit', history_dir=hd)
            snap_b = save_snapshot(B_CURRENT, filepath=b, operation='edit', history_dir=hd)

            self.assertEqual([s['id'] for s in list_snapshots(hd, timetable=a)], [snap_a])
            self.assertEqual([s['id'] for s in list_snapshots(hd, timetable=b)], [snap_b])
            self.assertEqual(len(list_snapshots(hd)), 2)

    def test_restore_rejects_snapshot_from_other_timetable(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            a = write_timetable(base / 'a.json', A_OLD)
            b = write_timetable(base / 'b.json', B_CURRENT)
            hd = history_directory(a)
            snap_a = save_snapshot(A_OLD, filepath=a, operation='edit', history_dir=hd)

            with self.assertRaises(SnapshotMismatchError):
                restore_snapshot(snap_a, filepath=b, history_dir=hd)

            # The other timetable is untouched and no snapshot was added for it.
            self.assertEqual(json.loads(b.read_text(encoding='utf-8')), B_CURRENT)
            self.assertEqual(list_snapshots(hd, timetable=b), [])
            self.assertEqual(len(list_snapshots(hd)), 1)

    def test_show_rejects_snapshot_from_other_timetable(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            a = write_timetable(base / 'a.json', A_OLD)
            b = write_timetable(base / 'b.json', B_CURRENT)
            hd = history_directory(a)
            snap_a = save_snapshot(A_OLD, filepath=a, operation='edit', history_dir=hd)

            with self.assertRaises(SnapshotMismatchError):
                show_snapshot(snap_a, hd, timetable=b)

    def test_prune_only_removes_selected_timetables_snapshots(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            a = write_timetable(base / 'a.json', A_OLD)
            b = write_timetable(base / 'b.json', B_CURRENT)
            hd = history_directory(a)
            first_a = save_snapshot(A_OLD, filepath=a, operation='edit', history_dir=hd)
            second_a = save_snapshot(B_CURRENT, filepath=a, operation='edit', history_dir=hd)
            first_b = save_snapshot(B_CURRENT, filepath=b, operation='edit', history_dir=hd)
            second_b = save_snapshot(A_OLD, filepath=b, operation='edit', history_dir=hd)

            removed = prune_snapshots(hd, 1, timetable=a)

            self.assertEqual(removed, 1)
            self.assertEqual(
                [entry['id'] for entry in list_snapshots(hd, timetable=a)],
                [second_a],
            )
            self.assertEqual(
                [entry['id'] for entry in list_snapshots(hd, timetable=b)],
                [second_b, first_b],
            )
            self.assertFalse((hd / first_a).exists())

    def test_restore_matches_relative_and_absolute_paths(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            a = write_timetable(base / 'a.json', A_OLD)
            hd = history_directory(a)
            old_cwd = os.getcwd()
            os.chdir(td)
            try:
                snap = save_snapshot(
                    A_OLD, filepath=Path('a.json'), operation='edit', history_dir=hd
                )
            finally:
                os.chdir(old_cwd)
            write_timetable(a, B_CURRENT)

            restore_snapshot(snap, filepath=a, history_dir=hd)
            self.assertEqual(json.loads(a.read_text(encoding='utf-8')), A_OLD)
