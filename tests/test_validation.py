"""Unit tests for input validation, collision detection, and chronological ordering."""

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from timetable.cli import check_clash, cmd_add, cmd_show, parse_time, validate_day
from timetable.display import WEEKDAYS, render_day
from timetable.loader import load_timetable, save_timetable


class TestValidationAndOrdering(unittest.TestCase):

    def test_validate_day_case_insensitive(self):
        """Valid days with various casings should normalize to lowercase."""
        self.assertEqual(validate_day("Monday"), "monday")
        self.assertEqual(validate_day("MONDAY"), "monday")
        self.assertEqual(validate_day("tUeSdAy"), "tuesday")
        self.assertEqual(validate_day("FRIDAY"), "friday")

    def test_validate_day_invalid_exits(self):
        """Invalid day name should print valid weekdays to stderr and exit."""
        err_out = StringIO()
        with redirect_stderr(err_out):
            with self.assertRaises(SystemExit) as cm:
                validate_day("mondy")
        self.assertEqual(cm.exception.code, 1)
        output = err_out.getvalue()
        self.assertIn("not a valid day", output)
        for day in WEEKDAYS:
            self.assertIn(day, output)

    def test_parse_time_valid(self):
        """Valid 24-hour HH:MM times should convert correctly to minutes from midnight."""
        self.assertEqual(parse_time("00:00"), 0)
        self.assertEqual(parse_time("09:30"), 570)
        self.assertEqual(parse_time("23:59"), 1439)

    def test_parse_time_invalid_format(self):
        """Malformed or non-numeric time strings should exit with an error."""
        for invalid in ["9:30", "09:3", "ab:cd", "25-00", "0900"]:
            with self.subTest(invalid=invalid):
                err_out = StringIO()
                with redirect_stderr(err_out):
                    with self.assertRaises(SystemExit):
                        parse_time(invalid)
                self.assertIn("Error", err_out.getvalue())

    def test_parse_time_nonexistent_hours_and_minutes(self):
        """Non-existent times (e.g. 25:99, 24:00, 12:60) should exit with an error."""
        for invalid in ["24:00", "25:99", "12:60", "99:00"]:
            with self.subTest(invalid=invalid):
                err_out = StringIO()
                with redirect_stderr(err_out):
                    with self.assertRaises(SystemExit):
                        parse_time(invalid)
                self.assertIn("does not exist", err_out.getvalue())

    def test_check_clash_overlap_detected(self):
        """Overlapping intervals should be detected as clashes."""
        existing_slots = [
            {"subject": "Mathematics", "start": "09:00", "end": "10:30", "room": "101"}
        ]
        # Overlaps within the slot
        clash = check_clash(existing_slots, parse_time("09:30"), parse_time("10:00"))
        self.assertIsNotNone(clash)
        self.assertEqual(clash["subject"], "Mathematics")

        # Overlaps the start boundary
        clash = check_clash(existing_slots, parse_time("08:30"), parse_time("09:15"))
        self.assertIsNotNone(clash)

        # Overlaps the end boundary
        clash = check_clash(existing_slots, parse_time("10:15"), parse_time("11:00"))
        self.assertIsNotNone(clash)

    def test_check_clash_consecutive_allowed(self):
        """Classes that end exactly when another begins should not clash."""
        existing_slots = [
            {"subject": "Mathematics", "start": "09:00", "end": "10:30", "room": "101"}
        ]
        # Starts right when previous ends
        clash = check_clash(existing_slots, parse_time("10:30"), parse_time("11:30"))
        self.assertIsNone(clash)

        # Ends right when next begins
        clash = check_clash(existing_slots, parse_time("08:00"), parse_time("09:00"))
        self.assertIsNone(clash)

    def test_check_clash_detects_overnight_slot(self):
        """A stored class that runs past midnight should clash with an overlapping add."""
        existing_slots = [
            {"subject": "Hackathon Lab Prep", "start": "23:00", "end": "01:00", "room": "IC"}
        ]
        # Overlaps the late class
        clash = check_clash(existing_slots, parse_time("22:00"), parse_time("23:30"))
        self.assertIsNotNone(clash)
        self.assertEqual(clash["subject"], "Hackathon Lab Prep")

        # Ends exactly when the late class starts: no clash
        clash = check_clash(existing_slots, parse_time("21:00"), parse_time("23:00"))
        self.assertIsNone(clash)

    def test_cmd_add_refuses_clash_with_shipped_overnight_class(self):
        """On the shipped timetable.json, a Friday 22:00-23:30 class clashes with 23:00-01:00."""
        with tempfile.TemporaryDirectory() as tmp:
            temp_path = Path(tmp) / "timetable.json"
            save_timetable(load_timetable(), temp_path)

            args = SimpleNamespace(
                file=temp_path, day="friday", subject="Clash",
                start="22:00", end="23:30", room="R9"
            )
            err_out = StringIO()
            with redirect_stderr(err_out):
                with self.assertRaises(SystemExit):
                    cmd_add(args)
            self.assertIn("Hackathon Lab Prep", err_out.getvalue())

    def test_cmd_add_rejects_end_before_start(self):
        """Adding a class where end <= start must exit with error."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            temp_path = tf.name
        save_timetable({"monday": []}, temp_path)

        args = SimpleNamespace(
            file=temp_path, day="monday", subject="Physics",
            start="14:00", end="10:00", room="101"
        )
        err_out = StringIO()
        with redirect_stderr(err_out):
            with self.assertRaises(SystemExit):
                cmd_add(args)
        self.assertIn("ends before or at its start time", err_out.getvalue())

    def test_cmd_add_and_sort_chronologically(self):
        """Adding classes in non-chronological order should keep them sorted by start time."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            temp_path = tf.name
        save_timetable({"monday": []}, temp_path)

        dummy_out = StringIO()
        with redirect_stdout(dummy_out):
            # Add afternoon class first
            args1 = SimpleNamespace(
                file=temp_path, day="monday", subject="Afternoon Class",
                start="14:00", end="15:30", room="101"
            )
            cmd_add(args1)

            # Add morning class second
            args2 = SimpleNamespace(
                file=temp_path, day="monday", subject="Morning Class",
                start="08:30", end="10:00", room="102"
            )
            cmd_add(args2)

        data = load_timetable(temp_path)
        self.assertEqual(len(data["monday"]), 2)
        self.assertEqual(data["monday"][0]["subject"], "Morning Class")
        self.assertEqual(data["monday"][1]["subject"], "Afternoon Class")

    def test_render_day_case_insensitive(self):
        """render_day should render the same schedule regardless of day parameter casing."""
        data = {
            "monday": [
                {"subject": "Mathematics", "start": "09:00", "end": "10:30", "room": "101"}
            ]
        }
        out_lower = StringIO()
        with redirect_stdout(out_lower):
            render_day(data, "monday")

        out_title = StringIO()
        with redirect_stdout(out_title):
            render_day(data, "Monday")

        self.assertEqual(out_lower.getvalue(), out_title.getvalue())
        self.assertIn("Mathematics", out_title.getvalue())


if __name__ == "__main__":
    unittest.main()
