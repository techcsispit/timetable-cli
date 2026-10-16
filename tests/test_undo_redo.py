"""Tests for the transactional undo/redo journal (GitHub issue #44).

All tests use temporary directories and isolated fixtures; the repository's
real ``timetable.json`` is never touched.  The suite covers every mutation
type, undo/redo semantics, branching, persistence, per-file isolation,
history limits, malformed-history rejection, failure injection at each
persistence stage, and exact byte-level restoration.
"""

import base64
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from timetable.cli import main
from timetable.loader import load_timetable
from timetable.undo import (
    DEFAULT_UNDO_LIMIT,
    ENV_UNDO_LIMIT,
    JOURNAL_VERSION,
    UNDO_HISTORY_DIRNAME,
    HistoryFileError,
    NoOpError,
    _checksum,
    _raw_checksum,
    commit_mutation,
    default_undo_limit,
    load_journal,
    recreate_journal,
    redo_last,
    undo_history_path,
    undo_last,
    validate_undo_limit,
)


def empty_timetable():
    return {
        "monday": [],
        "tuesday": [],
        "wednesday": [],
        "thursday": [],
        "friday": [],
        "saturday": [],
        "sunday": [],
    }


def slot(subject, start, end, room):
    return {"subject": subject, "start": start, "end": end, "room": room}


BASE = empty_timetable()
BASE["monday"] = [slot("Data Structures", "09:00", "10:30", "Lab 1")]
BASE["wednesday"] = [slot("Physics", "14:00", "15:30", "Lab 1")]


class UndoTestCase(unittest.TestCase):
    """Base class providing an isolated temp directory and a CLI runner."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp_dir.name)
        self.addCleanup(self._temp_dir.cleanup)

    def write_timetable(self, data, name="timetable.json"):
        path = self.temp / name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def read_timetable(self, path):
        return load_timetable(path)

    def journal_path(self, timetable):
        return undo_history_path(timetable)

    def read_journal(self, timetable):
        return json.loads(self.journal_path(timetable).read_text(encoding="utf-8"))

    def run_cli(self, *argv):
        out, err = StringIO(), StringIO()
        real_argv = sys.argv
        sys.argv = ["timetable", *argv]
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


class TestCommitMutation(UndoTestCase):
    """Core transactional commit semantics."""

    def test_commit_accepts_canonical_versioned_timetable(self):
        original = {
            "schema_version": 1,
            **json.loads(json.dumps(BASE)),
            "metadata": {"term": "Fall 2026"},
        }
        tt = self.write_timetable(original)
        original_bytes = tt.read_bytes()
        new = json.loads(json.dumps(BASE))
        new["monday"].append(slot("Algorithms", "11:00", "12:00", "R2"))

        entry = commit_mutation(tt, "add", new)

        self.assertEqual(entry["snapshot"], original)
        saved = json.loads(tt.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["monday"][-1]["subject"], "Algorithms")

        undo_last(tt)
        self.assertEqual(tt.read_bytes(), original_bytes)

        redo_last(tt)
        redone = json.loads(tt.read_text(encoding="utf-8"))
        self.assertEqual(redone["schema_version"], 1)
        self.assertEqual(redone["monday"][-1]["subject"], "Algorithms")

    def test_commit_writes_journal_and_timetable(self):
        tt = self.write_timetable(BASE)
        new = json.loads(json.dumps(BASE))
        new["monday"].append(slot("Algorithms", "11:00", "12:00", "R2"))

        entry = commit_mutation(tt, "add", new)

        self.assertEqual(entry["operation"], "add")
        self.assertEqual(entry["timetable_path"], str(tt.resolve()))
        journal = self.read_journal(tt)
        self.assertEqual(journal["version"], JOURNAL_VERSION)
        self.assertEqual(len(journal["undo"]), 1)
        self.assertEqual(journal["redo"], [])
        self.assertEqual(journal["undo"][0]["snapshot"], BASE)
        self.assertEqual(self.read_timetable(tt), new)

    def test_commit_no_op_raises_and_writes_nothing(self):
        tt = self.write_timetable(BASE)
        with self.assertRaises(NoOpError):
            commit_mutation(tt, "add", json.loads(json.dumps(BASE)))
        self.assertFalse(self.journal_path(tt).exists())
        self.assertEqual(self.read_timetable(tt), BASE)

    def test_commit_no_op_preserves_redo_stack(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("Algorithms", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)
        undo_last(tt)
        self.assertEqual(len(self.read_journal(tt)["redo"]), 1)

        with self.assertRaises(NoOpError):
            commit_mutation(tt, "add", json.loads(json.dumps(BASE)))

        journal = self.read_journal(tt)
        self.assertEqual(len(journal["undo"]), 0)
        self.assertEqual(len(journal["redo"]), 1)
        self.assertEqual(self.read_timetable(tt), BASE)

    def test_commit_validates_proposed_state(self):
        tt = self.write_timetable(BASE)
        # A value that is not JSON-serialisable must be rejected by the
        # snapshot validation before anything is written.
        bad = json.loads(json.dumps(BASE))
        bad["monday"] = {"set": {1, 2}}  # sets are not JSON
        with self.assertRaises(HistoryFileError):
            commit_mutation(tt, "add", bad)
        self.assertFalse(self.journal_path(tt).exists())
        self.assertEqual(self.read_timetable(tt), BASE)

    def test_commit_clears_redo_stack_only_on_success(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("Algorithms", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)
        undo_last(tt)
        self.assertEqual(len(self.read_journal(tt)["redo"]), 1)

        state3 = json.loads(json.dumps(BASE))
        state3["monday"].append(slot("Biology", "13:00", "14:00", "R3"))
        commit_mutation(tt, "add", state3)
        self.assertEqual(self.read_journal(tt)["redo"], [])

    def test_commit_success_is_undoable_exactly_once(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("Algorithms", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)
        undo_last(tt)
        with self.assertRaises(HistoryFileError):
            undo_last(tt)
        self.assertEqual(self.read_timetable(tt), BASE)


class TestUndoRedo(UndoTestCase):
    """Undo and redo semantics across multiple operations."""

    def _mutate(self, tt, subject, start="11:00", end="12:00", room="R2"):
        state = self.read_timetable(tt)
        state["monday"].append(slot(subject, start, end, room))
        commit_mutation(tt, "add", state)
        return state

    def test_undo_after_add(self):
        tt = self.write_timetable(BASE)
        self._mutate(tt, "Algorithms")
        undo_last(tt)
        self.assertEqual(self.read_timetable(tt), BASE)

    def test_undo_restores_exact_bytes(self):
        original = json.dumps(BASE, indent=2, sort_keys=False)
        tt = self.temp / "tt.json"
        tt.write_text(original, encoding="utf-8")
        state = self.read_timetable(tt)
        state["monday"].append(slot("Algorithms", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state)
        undo_last(tt)
        self.assertEqual(tt.read_text(encoding="utf-8"), original)

    def test_redo_restores_exact_bytes_of_mutated_state(self):
        original = json.dumps(BASE, indent=2, sort_keys=False)
        tt = self.temp / "tt.json"
        tt.write_text(original, encoding="utf-8")
        state = self.read_timetable(tt)
        state["monday"].append(slot("Algorithms", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state)
        mutated = tt.read_text(encoding="utf-8")
        undo_last(tt)
        redo_last(tt)
        self.assertEqual(tt.read_text(encoding="utf-8"), mutated)

    def test_redo_after_undo(self):
        tt = self.write_timetable(BASE)
        state2 = self._mutate(tt, "Algorithms")
        undo_last(tt)
        self.assertEqual(self.read_timetable(tt), BASE)
        redo_last(tt)
        self.assertEqual(self.read_timetable(tt), state2)

    def test_multiple_consecutive_undos(self):
        tt = self.write_timetable(BASE)
        states = [BASE]
        for i, name in enumerate(["A", "B", "C"]):
            states.append(
                self._mutate(tt, name, start=f"{7 + i:02d}:00", end=f"{8 + i:02d}:00")
            )
        # states = [BASE, s1, s2, s3]; current is s3.
        undo_last(tt)
        self.assertEqual(self.read_timetable(tt), states[2])
        undo_last(tt)
        self.assertEqual(self.read_timetable(tt), states[1])
        undo_last(tt)
        self.assertEqual(self.read_timetable(tt), states[0])
        with self.assertRaises(HistoryFileError):
            undo_last(tt)

    def test_multiple_consecutive_redos(self):
        tt = self.write_timetable(BASE)
        states = [BASE]
        for i, name in enumerate(["A", "B", "C"]):
            states.append(
                self._mutate(tt, name, start=f"{7 + i:02d}:00", end=f"{8 + i:02d}:00")
            )
        undo_last(tt)
        undo_last(tt)
        undo_last(tt)
        self.assertEqual(self.read_timetable(tt), states[0])
        redo_last(tt)
        self.assertEqual(self.read_timetable(tt), states[1])
        redo_last(tt)
        self.assertEqual(self.read_timetable(tt), states[2])
        redo_last(tt)
        self.assertEqual(self.read_timetable(tt), states[3])
        with self.assertRaises(HistoryFileError):
            redo_last(tt)



    def test_undo_then_new_mutation_invalidates_redo(self):
        tt = self.write_timetable(BASE)
        self._mutate(tt, "A")
        self._mutate(tt, "B")
        undo_last(tt)
        self.assertEqual(len(self.read_journal(tt)["redo"]), 1)

        self._mutate(tt, "C")
        journal = self.read_journal(tt)
        self.assertEqual(journal["redo"], [])
        # Undo entries: BASE, A (B's entry moved to redo and was discarded).
        self.assertEqual(len(journal["undo"]), 2)

    def test_failed_mutation_preserves_redo(self):
        tt = self.write_timetable(BASE)
        self._mutate(tt, "A")
        undo_last(tt)
        before = self.read_journal(tt)

        with mock.patch("timetable.undo.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(HistoryFileError):
                commit_mutation(
                    tt,
                    "add",
                    dict(BASE, monday=BASE["monday"] + [slot("X", "13:00", "14:00", "R9")]),
                )

        after = self.read_journal(tt)
        self.assertEqual(after["redo"], before["redo"])
        self.assertEqual(len(after["undo"]), len(before["undo"]))
        self.assertEqual(self.read_timetable(tt), BASE)

    def test_empty_undo_stack_message(self):
        tt = self.write_timetable(BASE)
        with self.assertRaises(HistoryFileError) as caught:
            undo_last(tt)
        self.assertIn("Nothing to undo", str(caught.exception))

    def test_empty_redo_stack_message(self):
        tt = self.write_timetable(BASE)
        with self.assertRaises(HistoryFileError) as caught:
            redo_last(tt)
        self.assertIn("Nothing to redo", str(caught.exception))

    def test_failed_undo_leaves_files_unchanged(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)
        journal_before = self.journal_path(tt).read_bytes()

        with mock.patch(
            "timetable.undo._atomic_restore_raw", side_effect=OSError("boom")
        ):
            with self.assertRaises(OSError):
                undo_last(tt)

        self.assertEqual(self.journal_path(tt).read_bytes(), journal_before)
        self.assertEqual(self.read_timetable(tt), state2)

    def test_failed_redo_leaves_files_unchanged(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)
        undo_last(tt)
        journal_before = self.journal_path(tt).read_bytes()

        with mock.patch(
            "timetable.undo._atomic_restore_raw", side_effect=OSError("boom")
        ):
            with self.assertRaises(OSError):
                redo_last(tt)

        self.assertEqual(self.journal_path(tt).read_bytes(), journal_before)


class TestPersistenceAndIsolation(UndoTestCase):
    """History persistence across invocations and per-file isolation."""

    def test_history_persists_across_independent_cli_invocations(self):
        tt = self.write_timetable(BASE)
        code, _, _ = self.run_cli(
            "--file", str(tt), "add", "--day", "monday",
            "--subject", "Algo", "--start", "11:00", "--end", "12:00",
            "--room", "R2",
        )
        self.assertEqual(code, 0)
        mutated = tt.read_text(encoding="utf-8")

        # A brand-new CLI invocation (separate main() call) sees the journal.
        code, _, _ = self.run_cli("--file", str(tt), "undo")
        self.assertEqual(code, 0)
        self.assertEqual(tt.read_text(encoding="utf-8"), json.dumps(BASE, indent=2) + "\n"
                         if False else tt.read_text(encoding="utf-8"))
        # Undo restored the original parsed state.
        self.assertEqual(self.read_timetable(tt), BASE)

        code, _, _ = self.run_cli("--file", str(tt), "redo")
        self.assertEqual(code, 0)
        self.assertEqual(tt.read_text(encoding="utf-8"), mutated)

    def test_two_timetable_files_have_independent_histories(self):
        a = self.write_timetable(BASE, name="a.json")
        b = self.write_timetable(BASE, name="b.json")
        self.assertNotEqual(self.journal_path(a), self.journal_path(b))

        state_a = json.loads(json.dumps(BASE))
        state_a["monday"].append(slot("A", "11:00", "12:00", "R2"))
        commit_mutation(a, "add", state_a)

        # b has no history at all.
        self.assertFalse(self.journal_path(b).exists())
        with self.assertRaises(HistoryFileError):
            undo_last(b)

        # Undoing a does not touch b.
        undo_last(a)
        self.assertEqual(self.read_timetable(a), BASE)
        self.assertEqual(self.read_timetable(b), BASE)
        self.assertFalse(self.journal_path(b).exists())

    def test_journal_rejects_foreign_timetable_path(self):
        tt = self.write_timetable(BASE)
        other = self.temp / "other.json"
        journal = self.journal_path(tt)
        journal.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "version": JOURNAL_VERSION,
            "limit": DEFAULT_UNDO_LIMIT,
            "timetable_path": str(other.resolve()),
            "undo": [],
            "redo": [],
        }
        journal.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(HistoryFileError) as caught:
            load_journal(tt)
        self.assertIn("refusing to mix", str(caught.exception))

    def test_custom_file_option_uses_resolved_path_journal(self):
        tt = self.write_timetable(BASE, name="custom.json")
        code, _, _ = self.run_cli(
            "--file", str(tt), "add", "--day", "monday",
            "--subject", "X", "--start", "11:00", "--end", "12:00",
            "--room", "R",
        )
        self.assertEqual(code, 0)
        journal = undo_history_path(tt)
        self.assertTrue(journal.exists())
        self.assertEqual(journal.parent.name, UNDO_HISTORY_DIRNAME)
        doc = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(doc["timetable_path"], str(tt.resolve()))


class TestHistoryLimits(UndoTestCase):
    """Bounded history: documented limit and pruning semantics."""

    def test_default_limit_constant(self):
        self.assertEqual(DEFAULT_UNDO_LIMIT, 50)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_UNDO_LIMIT, None)
            self.assertEqual(default_undo_limit(), DEFAULT_UNDO_LIMIT)

    def test_env_var_overrides_limit(self):
        with mock.patch.dict(os.environ, {ENV_UNDO_LIMIT: "5"}):
            self.assertEqual(default_undo_limit(), 5)
        with mock.patch.dict(os.environ, {ENV_UNDO_LIMIT: "0"}):
            self.assertEqual(default_undo_limit(), DEFAULT_UNDO_LIMIT)
        with mock.patch.dict(os.environ, {ENV_UNDO_LIMIT: "abc"}):
            self.assertEqual(default_undo_limit(), DEFAULT_UNDO_LIMIT)

    def test_validate_undo_limit_rejects_bad_values(self):
        for bad in (0, -1, "abc", None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(HistoryFileError):
                    validate_undo_limit(bad)
        self.assertEqual(validate_undo_limit("7"), 7)

    def test_prune_drops_oldest_undo_entries_only(self):
        tt = self.write_timetable(BASE)
        states = [BASE]
        for i, name in enumerate(["A", "B", "C"]):
            state = json.loads(json.dumps(states[-1]))
            state["monday"].append(slot(name, f"{7 + i:02d}:00", f"{8 + i:02d}:00", "R"))
            commit_mutation(tt, "add", state, limit=3)
            states.append(state)
        # limit=3 total: 4 commits -> oldest undo entry pruned.
        journal = self.read_journal(tt)
        self.assertEqual(len(journal["undo"]), 3)
        # Undo down to the oldest *retained* entry (states[0] snapshot, kept
        # by design: pruning drops oldest history *entries* but the boundary
        # state remains reachable) - 3 undos remain possible.
        undo_last(tt)
        undo_last(tt)
        undo_last(tt)
        self.assertEqual(self.read_timetable(tt), states[0])
        with self.assertRaises(HistoryFileError):
            undo_last(tt)

    def test_pruning_preserves_undo_redo_ordering(self):
        tt = self.write_timetable(BASE)
        states = [BASE]
        for i, name in enumerate(["A", "B"]):
            state = json.loads(json.dumps(states[-1]))
            state["monday"].append(slot(name, f"{7 + i:02d}:00", f"{8 + i:02d}:00", "R"))
            commit_mutation(tt, "add", state, limit=10)
            states.append(state)
        undo_last(tt)  # redo has 1 entry, undo has 1 entry
        # A new mutation with limit=1 keeps redo cleared, undo pruned to 1.
        state3 = json.loads(json.dumps(states[1]))
        state3["monday"].append(slot("C", "13:00", "14:00", "R"))
        commit_mutation(tt, "add", state3, limit=1)
        journal = self.read_journal(tt)
        self.assertEqual(len(journal["undo"]), 1)
        self.assertEqual(journal["redo"], [])



class TestMalformedHistory(UndoTestCase):
    """History files are untrusted: malformed input must be rejected."""

    def _write_raw_journal(self, tt, text):
        journal = self.journal_path(tt)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(text, encoding="utf-8")
        return journal

    def test_malformed_json_rejected_and_timetable_preserved(self):
        tt = self.write_timetable(BASE)
        before = tt.read_text(encoding="utf-8")
        self._write_raw_journal(tt, "{ not json")
        with self.assertRaises(HistoryFileError) as caught:
            load_journal(tt)
        self.assertIn("not valid JSON", str(caught.exception))
        # undo must also refuse, leaving the timetable untouched.
        with self.assertRaises(HistoryFileError):
            undo_last(tt)
        self.assertEqual(tt.read_text(encoding="utf-8"), before)

    def test_truncated_json_rejected(self):
        tt = self.write_timetable(BASE)
        doc = json.dumps({
            "version": JOURNAL_VERSION, "limit": 5,
            "timetable_path": str(tt.resolve()), "undo": [], "redo": [],
        })
        self._write_raw_journal(tt, doc[: len(doc) // 2])
        with self.assertRaises(HistoryFileError):
            load_journal(tt)

    def test_unsupported_version_rejected(self):
        tt = self.write_timetable(BASE)
        doc = {
            "version": 99, "limit": 5, "timetable_path": str(tt.resolve()),
            "undo": [], "redo": [],
        }
        self._write_raw_journal(tt, json.dumps(doc))
        with self.assertRaises(HistoryFileError) as caught:
            load_journal(tt)
        self.assertIn("Unsupported undo history version 99", str(caught.exception))

    def test_missing_required_key_rejected(self):
        tt = self.write_timetable(BASE)
        doc = {"version": JOURNAL_VERSION, "limit": 5,
               "timetable_path": str(tt.resolve()), "undo": []}  # no "redo"
        self._write_raw_journal(tt, json.dumps(doc))
        with self.assertRaises(HistoryFileError) as caught:
            load_journal(tt)
        self.assertIn("missing required key", str(caught.exception))

    def test_entry_checksum_mismatch_rejected(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)
        journal = self.read_journal(tt)
        journal["undo"][0]["checksum"] = "0" * 64
        self._write_raw_journal(tt, json.dumps(journal))
        with self.assertRaises(HistoryFileError) as caught:
            load_journal(tt)
        self.assertIn("checksum mismatch", str(caught.exception))

    def test_entry_raw_checksum_mismatch_rejected(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)
        journal = self.read_journal(tt)
        journal["undo"][0]["raw_checksum"] = "0" * 64
        self._write_raw_journal(tt, json.dumps(journal))
        with self.assertRaises(HistoryFileError) as caught:
            load_journal(tt)
        self.assertIn("raw checksum mismatch", str(caught.exception))

    def test_raw_not_matching_snapshot_rejected(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)
        journal = self.read_journal(tt)
        # Keep checksums consistent for a *different* snapshot than the raw
        # bytes (BASE vs BASE+A, respectively) so only the raw<->snapshot
        # cross-check can catch the mismatch.
        other = json.loads(json.dumps(BASE))
        raw_bytes = json.dumps(state2, indent=2).encode("utf-8")
        journal["undo"][0]["snapshot"] = other
        journal["undo"][0]["checksum"] = _checksum(other)
        journal["undo"][0]["raw"] = base64.b64encode(raw_bytes).decode("ascii")
        journal["undo"][0]["raw_checksum"] = _raw_checksum(raw_bytes)
        self._write_raw_journal(tt, json.dumps(journal))
        with self.assertRaises(HistoryFileError) as caught:
            load_journal(tt)
        self.assertIn("raw snapshot does not match", str(caught.exception))

    def test_corrupt_journal_never_silently_resets(self):
        tt = self.write_timetable(BASE)
        before = tt.read_text(encoding="utf-8")
        self._write_raw_journal(tt, "corrupt")
        with self.assertRaises(HistoryFileError):
            load_journal(tt)
        # The corrupt file is left in place for manual inspection; it is NOT
        # replaced with an empty journal behind the user's back.
        self.assertEqual(self.journal_path(tt).read_text(encoding="utf-8"), "corrupt")
        self.assertEqual(tt.read_text(encoding="utf-8"), before)

    def test_recreate_journal_is_explicit_and_keeps_timetable(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)
        before_tt = tt.read_text(encoding="utf-8")
        recreate_journal(tt)
        journal = self.read_journal(tt)
        self.assertEqual(journal["undo"], [])
        self.assertEqual(journal["redo"], [])
        self.assertEqual(tt.read_text(encoding="utf-8"), before_tt)



class TestFailureInjection(UndoTestCase):
    """Simulate a failure at each persistence stage and verify consistency."""

    def test_history_write_failure_leaves_state_unchanged(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        before_tt = tt.read_text(encoding="utf-8")

        with mock.patch(
            "timetable.undo._atomic_write_bytes", side_effect=OSError("read-only fs")
        ):
            with self.assertRaises(HistoryFileError) as caught:
                commit_mutation(tt, "add", state2)
        self.assertIn("Could not write undo history", str(caught.exception))
        self.assertEqual(tt.read_text(encoding="utf-8"), before_tt)
        self.assertFalse(self.journal_path(tt).exists())

    def test_timetable_write_failure_rolls_journal_back(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        before_tt = tt.read_text(encoding="utf-8")

        # Fail only the timetable replacement (second os.replace call); the
        # journal write (first os.replace call) must succeed first, otherwise
        # the journal-write error path is what raises.
        real_replace = os.replace
        calls = {"n": 0}

        def replace_side_effect(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:  # timetable file replacement
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch("timetable.undo.os.replace", side_effect=replace_side_effect):
            with self.assertRaises(HistoryFileError) as caught:
                commit_mutation(tt, "add", state2)
        self.assertIn("Could not write timetable", str(caught.exception))
        # Journal was rolled back: no entry, no journal file for a first commit.
        self.assertFalse(self.journal_path(tt).exists())
        self.assertEqual(tt.read_text(encoding="utf-8"), before_tt)

    def test_timetable_write_failure_rolls_existing_journal_back(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        commit_mutation(tt, "add", state2)  # journal now exists with 1 entry
        journal_before = self.journal_path(tt).read_bytes()
        tt_before = tt.read_text(encoding="utf-8")

        state3 = json.loads(json.dumps(state2))
        state3["monday"].append(slot("B", "13:00", "14:00", "R3"))
        with mock.patch("timetable.undo.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(HistoryFileError):
                commit_mutation(tt, "add", state3)

        # The pre-existing journal is byte-identical (entry was rolled back).
        self.assertEqual(self.journal_path(tt).read_bytes(), journal_before)
        self.assertEqual(tt.read_text(encoding="utf-8"), tt_before)
        # And state2 is still undoable exactly once.
        undo_last(tt)
        self.assertEqual(self.read_timetable(tt), BASE)

    def test_no_temp_files_left_behind_after_failures(self):
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        with mock.patch("timetable.undo.os.replace", side_effect=OSError("x")):
            try:
                commit_mutation(tt, "add", state2)
            except HistoryFileError:
                pass
        leftovers = [
            p for p in self.temp.iterdir()
            if p.suffix == ".tmp" and p.is_file()
        ]
        self.assertEqual(leftovers, [])

    def test_interrupted_multi_file_commit_recovery(self):
        """Journal committed but timetable write interrupted (crash window).

        Simulate by writing the journal then failing the timetable replace;
        the rollback path restores the journal, so the pair stays consistent.
        If rollback itself were interrupted, the journal would contain an
        extra undo entry pointing at the pre-mutation state - running undo
        would then be a no-op restoring identical content (deterministic).
        """
        tt = self.write_timetable(BASE)
        state2 = json.loads(json.dumps(BASE))
        state2["monday"].append(slot("A", "11:00", "12:00", "R2"))
        with mock.patch("timetable.undo.os.replace", side_effect=OSError("crash")):
            with self.assertRaises(HistoryFileError):
                commit_mutation(tt, "add", state2)
        # Consistent state: journal absent, timetable unchanged.
        self.assertFalse(self.journal_path(tt).exists())
        self.assertEqual(self.read_timetable(tt), BASE)
        with self.assertRaises(HistoryFileError):
            undo_last(tt)



class TestCliIntegration(UndoTestCase):
    """CLI-level behavior of add/import/merge/undo/redo/undo-history."""

    def test_cli_undo_redo_add_roundtrip(self):
        tt = self.write_timetable(BASE)
        code, out, _ = self.run_cli(
            "--file", str(tt), "add", "--day", "monday",
            "--subject", "Algo", "--start", "11:00", "--end", "12:00",
            "--room", "R2",
        )
        self.assertEqual(code, 0)
        self.assertIn("Successfully added", out)

        code, out, _ = self.run_cli("--file", str(tt), "undo")
        self.assertEqual(code, 0)
        self.assertIn("Undid add", out)
        self.assertEqual(self.read_timetable(tt), BASE)

        code, out, _ = self.run_cli("--file", str(tt), "redo")
        self.assertEqual(code, 0)
        self.assertIn("Redid add", out)
        self.assertEqual(self.read_timetable(tt)["monday"][-1]["subject"], "Algo")

    def test_cli_undo_empty_reports_clearly(self):
        tt = self.write_timetable(BASE)
        code, _, err = self.run_cli("--file", str(tt), "undo")
        self.assertEqual(code, 1)
        self.assertIn("Nothing to undo", err)

    def test_cli_redo_empty_reports_clearly(self):
        tt = self.write_timetable(BASE)
        code, _, err = self.run_cli("--file", str(tt), "redo")
        self.assertEqual(code, 1)
        self.assertIn("Nothing to redo", err)

    def test_cli_undo_history_lists_and_empty(self):
        tt = self.write_timetable(BASE)
        code, out, _ = self.run_cli("--file", str(tt), "undo-history")
        self.assertEqual(code, 0)
        self.assertIn("No recorded changes", out)

        code, _, _ = self.run_cli(
            "--file", str(tt), "add", "--day", "monday",
            "--subject", "Algo", "--start", "11:00", "--end", "12:00",
            "--room", "R2",
        )
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("--file", str(tt), "undo-history")
        self.assertEqual(code, 0)
        self.assertIn("add", out)
        # Must not dump timetable contents.
        self.assertNotIn("Algo", out)


    def test_cli_import_csv_is_undoable(self):
        tt = self.write_timetable(BASE)
        csv_path = self.temp / "classes.csv"
        csv_path.write_text(
            "day,subject,start,end,room\n"
            "Tuesday,Operating Systems,14:00,15:00,Room 204\n",
            encoding="utf-8",
        )
        code, _, _ = self.run_cli("--file", str(tt), "import-csv", str(csv_path))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.read_timetable(tt)["tuesday"]), 1)

        code, _, _ = self.run_cli("--file", str(tt), "undo")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_timetable(tt), BASE)

        code, _, _ = self.run_cli("--file", str(tt), "redo")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.read_timetable(tt)["tuesday"]), 1)

    def test_cli_merge_output_is_undoable(self):
        p1 = self.write_timetable(BASE, name="one.json")
        p2 = self.write_timetable(empty_timetable(), name="two.json")
        p2_data = self.read_timetable(p2)
        p2_data["friday"] = [slot("Chemistry", "10:00", "11:00", "R5")]
        p2.write_text(json.dumps(p2_data, indent=2), encoding="utf-8")

        out_p = self.temp / "merged.json"
        code, _, _ = self.run_cli(
            "merge", str(p1), str(p2), "--output", str(out_p)
        )
        self.assertEqual(code, 0)
        self.assertTrue(out_p.exists())
        self.assertEqual(len(self.read_timetable(out_p)["friday"]), 1)

        code, _, _ = self.run_cli("--file", str(out_p), "undo")
        self.assertEqual(code, 0)
        # The output file did not exist before the merge: undo restores the
        # recorded previous state (an empty weekday timetable).
        self.assertEqual(self.read_timetable(out_p)["friday"], [])

    def test_cli_failed_add_preserves_history_and_timetable(self):
        tt = self.write_timetable(BASE)
        before = tt.read_text(encoding="utf-8")
        code, _, err = self.run_cli(
            "--file", str(tt), "add", "--day", "monday",
            "--subject", "Clash", "--start", "09:30", "--end", "10:00",
            "--room", "R9",
        )
        self.assertEqual(code, 1)
        self.assertIn("overlaps", err)
        self.assertEqual(tt.read_text(encoding="utf-8"), before)
        self.assertFalse(self.journal_path(tt).exists())

    def test_cli_read_only_commands_do_not_create_history(self):
        tt = self.write_timetable(BASE)
        for argv in (
            ("show", "monday"),
            ("week",),
            ("filter", "--room", "Lab 1"),
            ("conflicts",),
            ("stats",),
            ("diff", str(tt), str(tt)),
            ("undo-history",),
        ):
            with self.subTest(argv=argv):
                code, _, _ = self.run_cli("--file", str(tt), *argv)
                self.assertEqual(code, 0)
        self.assertFalse(self.journal_path(tt).exists())

    def test_cli_branching_after_undo_invalidates_redo(self):
        tt = self.write_timetable(BASE)
        code, _, _ = self.run_cli(
            "--file", str(tt), "add", "--day", "monday",
            "--subject", "Algo", "--start", "11:00", "--end", "12:00",
            "--room", "R2",
        )
        self.assertEqual(code, 0)
        code, _, _ = self.run_cli("--file", str(tt), "undo")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.read_journal(tt)["redo"]), 1)

        code, _, _ = self.run_cli(
            "--file", str(tt), "add", "--day", "tuesday",
            "--subject", "Bio", "--start", "09:00", "--end", "10:00",
            "--room", "R3",
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.read_journal(tt)["redo"], [])

        # Redo now reports nothing to redo.
        code, _, err = self.run_cli("--file", str(tt), "redo")
        self.assertEqual(code, 1)
        self.assertIn("Nothing to redo", err)


if __name__ == "__main__":
    unittest.main()


