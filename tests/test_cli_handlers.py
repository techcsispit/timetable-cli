"""Tests that run the conflicts, week, export, filter and now commands through the CLI.

The other test files check the helpers underneath (find_conflicts,
filter_timetable, build_ics, render_now, ...). These tests call
``timetable.cli.main()`` the way ``python -m timetable <command>`` does, with
a temporary timetable file, so they cover the command handlers themselves:
argument passing, exit status, what is printed and what is written.
"""

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import mock

from timetable.cli import main

CLASSES = {
    "monday": [
        {"subject": "Math", "start": "09:00", "end": "10:30", "room": "Room 101"},
        {"subject": "Physics", "start": "10:30", "end": "12:00", "room": "Lab 1"},
        {"subject": "Chemistry", "start": "13:00", "end": "14:00", "room": "Lab 1"},
    ],
    "tuesday": [
        {"subject": "Biology", "start": "09:00", "end": "10:00", "room": "Room 101"},
    ],
}


class CliTestCase(unittest.TestCase):
    """Gives each test a temporary directory and a way to run the CLI."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write_timetable(self, data, name="timetable.json"):
        path = self.dir / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def run_cli(self, *args):
        """Runs ``main()`` with the given arguments; returns (exit code, stdout, stderr)."""
        out, err = StringIO(), StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["timetable"] + list(args)):
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    main()
                except SystemExit as exc:
                    code = 0 if exc.code is None else exc.code
        return code, out.getvalue(), err.getvalue()


class TestConflictsCommand(CliTestCase):

    def test_no_overlap_exits_zero(self):
        path = self.write_timetable(CLASSES)  # Math ends 10:30 exactly when Physics starts
        code, out, _ = self.run_cli("conflicts", path)
        self.assertEqual(code, 0)
        self.assertIn("No conflicts detected.", out)

    def test_overlap_is_reported_and_exits_one(self):
        data = {
            "monday": [
                {"subject": "Math", "start": "09:00", "end": "10:30", "room": "R1"},
                {"subject": "Physics", "start": "10:00", "end": "11:00", "room": "R2"},
            ]
        }
        path = self.write_timetable(data)
        code, out, _ = self.run_cli("conflicts", path)
        self.assertEqual(code, 1)
        self.assertIn("Conflict on Monday", out)
        self.assertIn("Math", out)
        self.assertIn("Physics", out)
        self.assertNotIn("No conflicts detected.", out)

    def test_overlap_on_other_days_only_reports_that_day(self):
        data = {
            "monday": [
                {"subject": "Math", "start": "09:00", "end": "10:00", "room": "R1"},
            ],
            "wednesday": [
                {"subject": "Art", "start": "09:00", "end": "11:00", "room": "R2"},
                {"subject": "Music", "start": "10:00", "end": "12:00", "room": "R3"},
            ],
        }
        path = self.write_timetable(data)
        code, out, _ = self.run_cli("conflicts", path)
        self.assertEqual(code, 1)
        self.assertIn("Conflict on Wednesday", out)
        self.assertNotIn("Conflict on Monday", out)

    def test_missing_file_is_an_error(self):
        missing = str(self.dir / "nope.json")
        code, out, err = self.run_cli("conflicts", missing)
        self.assertEqual(code, 1)
        self.assertIn("Timetable file not found", err)
        self.assertIn(missing, err)
        self.assertEqual(out, "")

    def test_invalid_json_is_an_error(self):
        path = self.dir / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        code, out, err = self.run_cli("conflicts", str(path))
        self.assertEqual(code, 1)
        self.assertIn("Error reading", err)
        self.assertNotIn("No conflicts detected.", out)


class TestWeekCommand(CliTestCase):

    def test_lists_every_day_in_order_with_its_classes(self):
        path = self.write_timetable(CLASSES)
        code, out, _ = self.run_cli("--file", path, "week")
        self.assertEqual(code, 0)
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        positions = [out.index("● " + day) for day in days]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("● Monday (3 classes):", out)
        self.assertIn("● Tuesday (1 classes):", out)
        self.assertIn("● Wednesday (0 classes):", out)
        self.assertIn("09:00 - 10:30", out)
        self.assertIn("(90 min) - Math [Room 101]", out)

    def test_classes_are_listed_by_start_time(self):
        data = {
            "tuesday": [
                {"subject": "Afternoon", "start": "14:00", "end": "15:00", "room": "R1"},
                {"subject": "Morning", "start": "08:30", "end": "09:30", "room": "R2"},
            ]
        }
        path = self.write_timetable(data)
        _, out, _ = self.run_cli("--file", path, "week")
        self.assertLess(out.index("Morning"), out.index("Afternoon"))

    def test_reads_the_file_given_with_file_option(self):
        path = self.write_timetable({"sunday": [
            {"subject": "Unique Sunday Seminar", "start": "10:00", "end": "11:00", "room": "R9"},
        ]})
        _, out, _ = self.run_cli("--file", path, "week")
        self.assertIn("Unique Sunday Seminar", out)
        self.assertIn("● Sunday (1 classes):", out)

    def test_missing_file_shows_an_empty_week(self):
        code, out, _ = self.run_cli("--file", str(self.dir / "nope.json"), "week")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("No classes."), 7)


class TestExportCommand(CliTestCase):

    def test_output_option_writes_calendar_and_reports_the_path(self):
        path = self.write_timetable(CLASSES)
        target = str(self.dir / "mine.ics")
        code, out, _ = self.run_cli("--file", path, "export", "--output", target)
        self.assertEqual(code, 0)
        self.assertIn("Exported " + target, out)
        text = Path(target).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("BEGIN:VCALENDAR"))
        self.assertEqual(text.count("BEGIN:VEVENT"), 4)
        self.assertIn("SUMMARY:Math", text)
        self.assertIn("LOCATION:Lab 1", text)
        self.assertIn("RRULE:FREQ=WEEKLY", text)

    def test_default_output_is_timetable_ics_in_the_working_directory(self):
        path = self.write_timetable(CLASSES)
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.dir)
        code, out, _ = self.run_cli("--file", path, "export")
        self.assertEqual(code, 0)
        self.assertIn("Exported timetable.ics", out)
        self.assertTrue((self.dir / "timetable.ics").is_file())

    def test_export_does_not_modify_the_timetable(self):
        path = self.write_timetable(CLASSES)
        before = Path(path).read_bytes()
        self.run_cli("--file", path, "export", "--output", str(self.dir / "x.ics"))
        self.assertEqual(Path(path).read_bytes(), before)

    def test_missing_file_exports_an_empty_calendar(self):
        target = self.dir / "empty.ics"
        code, _, _ = self.run_cli("--file", str(self.dir / "nope.json"), "export", "--output", str(target))
        self.assertEqual(code, 0)
        text = target.read_text(encoding="utf-8")
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertEqual(text.count("BEGIN:VEVENT"), 0)


class TestFilterCommand(CliTestCase):

    def setUp(self):
        super().setUp()
        self.path = self.write_timetable(CLASSES)

    def filter(self, *options):
        code, out, _ = self.run_cli("--file", self.path, "filter", *options)
        self.assertEqual(code, 0)
        return out

    def test_day_and_time_range_exclude_boundary_touching_classes(self):
        out = self.filter("--day", "monday", "--start", "09:00", "--end", "10:30")
        self.assertIn("Math", out)
        self.assertNotIn("Physics", out)    # starts exactly when the range ends
        self.assertNotIn("Chemistry", out)  # outside the range
        self.assertNotIn("Biology", out)    # other day

    def test_day_ignores_capital_letters(self):
        out = self.filter("--day", "TUESDAY")
        self.assertIn("Biology", out)
        self.assertNotIn("Math", out)

    def test_room_matches_exactly(self):
        out = self.filter("--room", "Lab 1")
        self.assertIn("Physics", out)
        self.assertIn("Chemistry", out)
        self.assertNotIn("Math", out)
        self.assertIn("No classes match", self.filter("--room", "lab 1"))

    def test_start_only_leaves_the_end_open(self):
        out = self.filter("--start", "13:00")
        self.assertIn("Chemistry", out)
        self.assertNotIn("Math", out)
        self.assertNotIn("Physics", out)

    def test_end_only_leaves_the_start_open(self):
        out = self.filter("--end", "10:30")
        self.assertIn("Math", out)
        self.assertIn("Biology", out)
        self.assertNotIn("Physics", out)

    def test_options_combine(self):
        out = self.filter("--day", "monday", "--room", "Lab 1", "--start", "11:00", "--end", "13:30")
        self.assertIn("Physics", out)
        self.assertIn("Chemistry", out)
        self.assertNotIn("Math", out)
        out = self.filter("--day", "tuesday", "--room", "Lab 1")
        self.assertIn("No classes match", out)

    def test_no_options_lists_every_class(self):
        out = self.filter()
        for subject in ("Math", "Physics", "Chemistry", "Biology"):
            self.assertIn(subject, out)

    def test_nothing_matching_says_so(self):
        out = self.filter("--day", "friday")
        self.assertIn("No classes match the given filters.", out)


class TestNowCommand(CliTestCase):

    def now_output(self, when, path=None):
        path = path or self.write_timetable(CLASSES)
        with mock.patch("timetable.cli.current_now", return_value=when):
            code, out, _ = self.run_cli("--file", path, "now")
        self.assertEqual(code, 0)
        return out

    def test_during_a_class_shows_it_and_the_next_one(self):
        out = self.now_output(datetime(2024, 1, 1, 9, 30))  # a Monday
        self.assertIn("Today: Monday", out)
        self.assertIn("Current class", out)
        self.assertIn("Math", out)
        self.assertIn("60 minutes remaining", out)
        self.assertIn("Next class", out)
        self.assertIn("Physics", out)
        self.assertIn("Starts in 60 minutes", out)

    def test_between_classes_says_nothing_is_running(self):
        out = self.now_output(datetime(2024, 1, 1, 12, 30))
        self.assertIn("No class is currently in progress.", out)
        self.assertIn("Chemistry", out)
        self.assertIn("Starts in 30 minutes", out)

    def test_after_the_last_class_says_the_day_is_finished(self):
        out = self.now_output(datetime(2024, 1, 1, 15, 0))
        self.assertIn("No class is currently in progress.", out)
        self.assertIn("No more classes scheduled today.", out)

    def test_day_without_classes(self):
        out = self.now_output(datetime(2024, 1, 3, 10, 0))  # a Wednesday
        self.assertIn("Today: Wednesday", out)
        self.assertIn("No classes scheduled today.", out)

    def test_uses_the_weekday_of_the_clock(self):
        out = self.now_output(datetime(2024, 1, 2, 9, 30))  # a Tuesday
        self.assertIn("Today: Tuesday", out)
        self.assertIn("Biology", out)
        self.assertNotIn("Math", out)


if __name__ == "__main__":
    unittest.main()
