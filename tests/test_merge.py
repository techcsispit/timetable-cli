"""Unit tests for the `merge` command and merge logic."""

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from timetable.cli import main
from timetable.merge import (
    MergeError,
    load_timetable_for_merge,
    merge_timetables,
)


def empty_timetable():
    """Returns a timetable structure with empty lists for all canonical weekdays."""
    return {
        "monday": [],
        "tuesday": [],
        "wednesday": [],
        "thursday": [],
        "friday": [],
        "saturday": [],
        "sunday": [],
    }


def make_slot(subject, start, end, room):
    """Returns a class dictionary."""
    return {"subject": subject, "start": start, "end": end, "room": room}


class MergeTestCase(unittest.TestCase):
    """Base test case managing temporary files and CLI execution for merge tests."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp_dir.name)
        self.addCleanup(self._temp_dir.cleanup)

    def write_json(self, filename, data):
        path = self.temp / filename
        text = data if isinstance(data, str) else json.dumps(data, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    def run_merge(self, *args):
        """Runs the CLI with `timetable merge ...` and captures output and exit code."""
        out, err = StringIO(), StringIO()
        real_argv = sys.argv
        sys.argv = ["timetable", "merge"] + list(args)
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


class TestMergeLogic(MergeTestCase):
    """Tests for core merge algorithm logic."""

    def test_merge_two_non_overlapping_timetables(self):
        t1 = empty_timetable()
        t1["monday"] = [make_slot("Math", "09:00", "10:00", "R101")]
        t2 = empty_timetable()
        t2["tuesday"] = [make_slot("Physics", "11:00", "12:00", "R102")]

        merged, conflicts = merge_timetables(t1, t2)
        self.assertEqual(conflicts, [])
        self.assertEqual(len(merged["monday"]), 1)
        self.assertEqual(len(merged["tuesday"]), 1)
        self.assertEqual(merged["monday"][0]["subject"], "Math")
        self.assertEqual(merged["tuesday"][0]["subject"], "Physics")

    def test_preserve_unique_entries_and_remove_duplicates(self):
        t1 = empty_timetable()
        slot_math = make_slot("Math", "09:00", "10:00", "R101")
        slot_chem = make_slot("Chem", "10:30", "11:30", "R103")
        t1["monday"] = [slot_math, slot_chem]

        t2 = empty_timetable()
        # Duplicate Math, unique Physics
        slot_physics = make_slot("Physics", "12:00", "13:00", "R102")
        t2["monday"] = [copy.deepcopy(slot_math), slot_physics]

        merged, conflicts = merge_timetables(t1, t2)
        self.assertEqual(conflicts, [])
        self.assertEqual(len(merged["monday"]), 3)
        subjects = [s["subject"] for s in merged["monday"]]
        self.assertEqual(subjects, ["Math", "Chem", "Physics"])

    def test_detect_one_scheduling_conflict(self):
        t1 = empty_timetable()
        t1["monday"] = [make_slot("Math", "09:00", "10:30", "R101")]
        t2 = empty_timetable()
        t2["monday"] = [make_slot("Physics", "10:00", "11:00", "R102")]

        merged, conflicts = merge_timetables(t1, t2)
        self.assertEqual(len(conflicts), 1)
        day, a, b = conflicts[0]
        self.assertEqual(day, "monday")
        self.assertEqual(a["subject"], "Math")
        self.assertEqual(b["subject"], "Physics")

    def test_detect_multiple_conflicts(self):
        t1 = empty_timetable()
        t1["monday"] = [
            make_slot("Math", "09:00", "10:30", "R101"),
            make_slot("Bio", "14:00", "15:30", "R105"),
        ]
        t2 = empty_timetable()
        t2["monday"] = [
            make_slot("Physics", "10:00", "11:00", "R102"),
            make_slot("History", "15:00", "16:00", "R106"),
        ]

        merged, conflicts = merge_timetables(t1, t2)
        self.assertEqual(len(conflicts), 2)

    def test_conflicts_involving_classes_with_different_subjects(self):
        t1 = empty_timetable()
        t1["monday"] = [make_slot("Math", "09:00", "11:00", "R101")]
        t2 = empty_timetable()
        t2["monday"] = [make_slot("Physics", "09:30", "10:30", "R102")]

        merged, conflicts = merge_timetables(t1, t2)
        self.assertEqual(len(conflicts), 1)

    def test_classes_on_different_days_do_not_conflict(self):
        t1 = empty_timetable()
        t1["monday"] = [make_slot("Math", "09:00", "11:00", "R101")]
        t2 = empty_timetable()
        t2["tuesday"] = [make_slot("Physics", "09:00", "11:00", "R101")]

        merged, conflicts = merge_timetables(t1, t2)
        self.assertEqual(conflicts, [])

    def test_classes_touching_time_boundaries_do_not_overlap(self):
        t1 = empty_timetable()
        t1["monday"] = [make_slot("Math", "09:00", "10:00", "R101")]
        t2 = empty_timetable()
        t2["monday"] = [make_slot("Physics", "10:00", "11:00", "R102")]

        merged, conflicts = merge_timetables(t1, t2)
        self.assertEqual(conflicts, [])
        self.assertEqual(len(merged["monday"]), 2)

    def test_empty_first_timetable(self):
        t1 = {}
        t2 = empty_timetable()
        t2["monday"] = [make_slot("Physics", "10:00", "11:00", "R102")]

        merged, conflicts = merge_timetables(t1, t2)
        self.assertEqual(conflicts, [])
        self.assertEqual(merged["monday"], [make_slot("Physics", "10:00", "11:00", "R102")])

    def test_empty_second_timetable(self):
        t1 = empty_timetable()
        t1["monday"] = [make_slot("Math", "09:00", "10:00", "R101")]
        t2 = {}

        merged, conflicts = merge_timetables(t1, t2)
        self.assertEqual(conflicts, [])
        self.assertEqual(merged["monday"], [make_slot("Math", "09:00", "10:00", "R101")])

    def test_both_inputs_empty(self):
        merged, conflicts = merge_timetables({}, {})
        self.assertEqual(conflicts, [])
        for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
            self.assertEqual(merged[day], [])

    def test_identical_input_files(self):
        t1 = empty_timetable()
        t1["monday"] = [make_slot("Math", "09:00", "10:00", "R101")]

        merged, conflicts = merge_timetables(t1, copy.deepcopy(t1))
        self.assertEqual(conflicts, [])
        self.assertEqual(len(merged["monday"]), 1)

    def test_deterministic_weekday_and_time_ordering(self):
        t1 = empty_timetable()
        t1["tuesday"] = [make_slot("Physics", "14:00", "15:00", "R102")]
        t1["monday"] = [make_slot("Math", "11:00", "12:00", "R101")]
        t2 = empty_timetable()
        t2["monday"] = [make_slot("Algo", "09:00", "10:00", "R103")]

        merged, conflicts = merge_timetables(t1, t2)
        keys = list(merged.keys())
        self.assertEqual(keys[:2], ["monday", "tuesday"])
        self.assertEqual(merged["monday"][0]["start"], "09:00")
        self.assertEqual(merged["monday"][1]["start"], "11:00")


class TestMergeCLI(MergeTestCase):
    """Tests for CLI subcommand `merge`."""

    def test_successful_merge_command(self):
        p1 = self.write_json("t1.json", {"monday": [make_slot("Math", "09:00", "10:00", "R101")]})
        p2 = self.write_json("t2.json", {"tuesday": [make_slot("Physics", "10:00", "11:00", "R102")]})
        out_p = self.temp / "merged.json"

        code, out, err = self.run_merge(str(p1), str(p2), "--output", str(out_p))
        self.assertEqual(code, 0)
        self.assertIn("Successfully merged timetables into", out)

        merged = json.loads(out_p.read_text(encoding="utf-8"))
        self.assertEqual(len(merged["monday"]), 1)
        self.assertEqual(len(merged["tuesday"]), 1)

    def test_conflict_command_behavior_and_exit_code(self):
        p1 = self.write_json("t1.json", {"monday": [make_slot("Math", "09:00", "10:30", "R101")]})
        p2 = self.write_json("t2.json", {"monday": [make_slot("Physics", "10:00", "11:00", "R102")]})
        out_p = self.temp / "merged.json"

        code, out, err = self.run_merge(str(p1), str(p2), "--output", str(out_p))
        self.assertEqual(code, 1)
        self.assertIn("Merge Conflicts Detected", err)
        self.assertIn("Could not merge timetables due to scheduling conflicts", err)
        self.assertFalse(out_p.exists())

    def test_missing_input_file(self):
        p1 = self.write_json("t1.json", {"monday": []})
        p_missing = self.temp / "nonexistent.json"

        code, out, err = self.run_merge(str(p1), str(p_missing))
        self.assertEqual(code, 1)
        self.assertIn("File not found", err)

    def test_invalid_json_file(self):
        p1 = self.write_json("t1.json", "Not valid JSON {")
        p2 = self.write_json("t2.json", {"monday": []})

        code, out, err = self.run_merge(str(p1), str(p2))
        self.assertEqual(code, 1)
        self.assertIn("Invalid JSON", err)

    def test_invalid_timetable_structure(self):
        p1 = self.write_json("t1.json", {"monday": "not a list"})
        p2 = self.write_json("t2.json", {"monday": []})

        code, out, err = self.run_merge(str(p1), str(p2))
        self.assertEqual(code, 1)
        self.assertIn("must be a list of class slots", err)

    def test_input_files_remain_unchanged(self):
        t1_content = json.dumps({"monday": [make_slot("Math", "09:00", "10:00", "R101")]}, indent=2)
        t2_content = json.dumps({"tuesday": [make_slot("Physics", "10:00", "11:00", "R102")]}, indent=2)
        p1 = self.write_json("t1.json", t1_content)
        p2 = self.write_json("t2.json", t2_content)

        out_p = self.temp / "merged.json"
        self.run_merge(str(p1), str(p2), "--output", str(out_p))

        self.assertEqual(p1.read_text(encoding="utf-8"), t1_content)
        self.assertEqual(p2.read_text(encoding="utf-8"), t2_content)

    def test_prevent_output_overwriting_input_file(self):
        p1 = self.write_json("t1.json", {"monday": []})
        p2 = self.write_json("t2.json", {"tuesday": []})

        code, out, err = self.run_merge(str(p1), str(p2), "--output", str(p1))
        self.assertEqual(code, 1)
        self.assertIn("cannot be the same as input file", err)

    def test_output_file_overwrite_prevention_and_force(self):
        p1 = self.write_json("t1.json", {"monday": [make_slot("Math", "09:00", "10:00", "R101")]})
        p2 = self.write_json("t2.json", {"tuesday": [make_slot("Physics", "10:00", "11:00", "R102")]})
        out_p = self.write_json("merged.json", {"existing": "data"})

        # Without --force
        code, out, err = self.run_merge(str(p1), str(p2), "--output", str(out_p))
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        # With --force
        code, out, err = self.run_merge(str(p1), str(p2), "--output", str(out_p), "--force")
        self.assertEqual(code, 0)
        merged = json.loads(out_p.read_text(encoding="utf-8"))
        self.assertIn("monday", merged)

    def test_existing_commands_continue_working(self):
        p1 = self.write_json("t1.json", {"monday": [make_slot("Math", "09:00", "10:00", "R101")]})
        out, err = StringIO(), StringIO()
        real_argv = sys.argv
        sys.argv = ["timetable", "--file", str(p1), "show", "monday"]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                main()
        finally:
            sys.argv = real_argv
        self.assertIn("Math", out.getvalue())


if __name__ == "__main__":
    unittest.main()
