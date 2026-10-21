"""CLI-layer tests for conflicts/week/export/filter/now (#26).

The helpers underneath are covered elsewhere; these tests pin the layer in
between: exit codes, --file selection, argument passthrough and output files.
"""
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import mock

from timetable.cli import main

BASE = {
    "monday": [
        {"subject": "Data Structures", "start": "09:00", "end": "10:30", "room": "Lab 1"},
        {"subject": "Mathematics", "start": "10:00", "end": "11:00", "room": "Room 101"},
    ],
    "tuesday": [],
    "wednesday": [
        {"subject": "Physics", "start": "14:00", "end": "15:30", "room": "Lab 1"},
    ],
    "thursday": [],
    "friday": [],
    "saturday": [],
    "sunday": [],
}

CLEAN = {
    day: ([{"subject": "Solo", "start": "09:00", "end": "10:00", "room": "R1"}] if day == "monday" else [])
    for day in BASE
}

OVERNIGHT = {
    day: ([{"subject": "Hackathon Lab Prep", "start": "23:00", "end": "01:00", "room": "R9"}]
          if day == "friday" else [])
    for day in BASE
}


class CliCommandsTest(unittest.TestCase):
    def write(self, tmp_path, data):
        path = Path(tmp_path) / "tt.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

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

    def test_conflicts_exits_1_on_overlap(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, BASE)
            code, out, _ = self.run_cli("conflicts", str(path))
            self.assertEqual(code, 1)
            self.assertIn("Data Structures", out)

    def test_conflicts_exits_1_on_missing_file(self):
        code, _, err = self.run_cli("conflicts", "/nonexistent/tt.json")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_conflicts_exits_0_when_clean(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, CLEAN)
            code, _, _ = self.run_cli("conflicts", str(path))
            self.assertEqual(code, 0)

    def test_week_reads_given_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, BASE)
            code, out, _ = self.run_cli("--file", str(path), "week")
            self.assertEqual(code, 0)
            self.assertIn("Physics", out)
            self.assertIn("Data Structures", out)

    def test_show_reports_positive_overnight_duration(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, OVERNIGHT)
            code, out, err = self.run_cli("--file", str(path), "show", "friday")
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("23:00 - 01:00", out)
            self.assertIn("120 min", out)
            self.assertNotIn("-1320 min", out)

    def test_week_reports_positive_overnight_duration(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, OVERNIGHT)
            code, out, err = self.run_cli("--file", str(path), "week")
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("23:00 - 01:00", out)
            self.assertIn("(120 min)", out)
            self.assertNotIn("-1320 min", out)

    def test_add_rejects_clash_with_overnight_class(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, OVERNIGHT)
            before = path.read_bytes()
            code, out, err = self.run_cli(
                "--file", str(path), "add",
                "--day", "friday",
                "--subject", "Clash",
                "--start", "22:00",
                "--end", "23:30",
                "--room", "R9",
            )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("overlaps with 'Hackathon Lab Prep'", err)
            self.assertEqual(path.read_bytes(), before)

    def test_add_rejects_next_day_clash_with_overnight_class(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, OVERNIGHT)
            before = path.read_bytes()
            code, _, err = self.run_cli(
                "--file", str(path), "add",
                "--day", "saturday",
                "--subject", "Early Clash",
                "--start", "00:30",
                "--end", "01:30",
                "--room", "R10",
            )
            self.assertEqual(code, 1)
            self.assertIn("overlaps with 'Hackathon Lab Prep'", err)
            self.assertEqual(path.read_bytes(), before)

    def test_now_reads_given_file(self):
        import tempfile

        import timetable.cli as cli_module

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, BASE)
            with mock.patch.object(
                cli_module, "current_now", return_value=datetime(2026, 10, 12, 9, 30)
            ):
                code, out, _ = self.run_cli("--file", str(path), "now")
            self.assertEqual(code, 0)
            self.assertTrue(out.strip())

    def test_export_writes_file_and_prints_where(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = self.write(tmp, BASE)
            dest = str(Path(tmp) / "out.ics")
            code, out, _ = self.run_cli("--file", str(src), "export", "--output", dest)
            self.assertEqual(code, 0)
            self.assertIn(dest, out)
            content = Path(dest).read_text(encoding="utf-8")
            self.assertIn("BEGIN:VCALENDAR", content)

    def test_filter_passes_room_through(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, BASE)
            _, out, _ = self.run_cli("--file", str(path), "filter", "--room", "Lab 1")
            self.assertIn("Data Structures", out)
            self.assertIn("Physics", out)
            self.assertNotIn("Mathematics", out)


if __name__ == "__main__":
    unittest.main()
