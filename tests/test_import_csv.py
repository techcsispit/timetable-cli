"""Unit tests for CSV timetable import and the `import-csv` command."""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from timetable.cli import main
from timetable.import_csv import (
    ImportError_,
    apply_import,
    load_timetable_for_import,
    plan_import,
    render_plan,
    save_timetable_atomic,
)


def empty_timetable():
    """Returns a timetable structure with empty lists for all weekdays."""
    return {
        "monday": [],
        "tuesday": [],
        "wednesday": [],
        "thursday": [],
        "friday": [],
        "saturday": [],
        "sunday": [],
    }


class ImportCsvTestCase(unittest.TestCase):
    """Shared temporary directory and CLI runner for import tests."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp_dir.name)
        self.addCleanup(self._temp_dir.cleanup)

    def write_csv(self, text, name="classes.csv"):
        path = self.temp / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, data, name="timetable.json"):
        path = self.temp / name
        text = data if isinstance(data, str) else json.dumps(data, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    def run_import(self, *args):
        """Runs `timetable import-csv ...` and returns (code, out, err)."""
        return self.run_cli("import-csv", *args)

    def run_cli(self, *argv):
        """Runs any `timetable ...` command and returns (code, out, err)."""
        out, err = StringIO(), StringIO()
        real_argv = sys.argv
        sys.argv = ["timetable"] + list(argv)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                main()
        except SystemExit as exc:
            if exc.code is None:
                code = 0
            elif isinstance(exc.code, int):
                code = exc.code
            else:
                code = 1
        else:
            code = 0
        finally:
            sys.argv = real_argv
        return code, out.getvalue(), err.getvalue()


class TestCsvValidation(ImportCsvTestCase):
    """CSV parsing, header validation, and record validation tests."""

    def test_valid_csv_with_multiple_classes(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Data Structures,09:00,10:30,Lab 1\n"
            "Monday,Mathematics,11:00,12:00,Room 101\n"
            "Tuesday,Operating Systems,14:00,15:00,Room 204\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.records_processed, 3)
        self.assertEqual(len(plan.new_classes), 3)
        self.assertEqual(plan.new_classes[0][1], "monday")
        self.assertEqual(plan.new_classes[0][2]["subject"], "Data Structures")
        self.assertEqual(plan.new_classes[2][1], "tuesday")

    def test_headers_are_case_insensitive_and_normalized(self):
        path = self.write_csv(
            "DAY,SUBJECT,Start,END,ROOM\n"
            "Monday,Physics,09:00,10:00,Room 1\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.new_classes[0][2], {
            "subject": "Physics",
            "start": "09:00",
            "end": "10:00",
            "room": "Room 1",
        })

    def test_missing_required_columns_rejected(self):
        path = self.write_csv(
            "day,subject,start\n"
            "Monday,Physics,09:00\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.invalid_records), 1)
        issue = plan.invalid_records[0]
        self.assertEqual(issue.row, 1)
        self.assertIn("missing required column", issue.message)
        self.assertIn("end", issue.message)
        self.assertIn("room", issue.message)

    def test_duplicate_headers_rejected(self):
        path = self.write_csv(
            "day,subject,start,end,room,day\n"
            "Monday,Physics,09:00,10:00,Room 1,extra\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertFalse(plan.accepted)
        self.assertEqual(plan.invalid_records[0].row, 1)
        self.assertIn("duplicate column", plan.invalid_records[0].message)

    def test_missing_and_blank_fields_reported(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,,09:00,10:00,Room 1\n"
            "Tuesday,Physics, ,11:00, \n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertFalse(plan.accepted)
        messages = [issue.message for issue in plan.invalid_records]
        self.assertIn(2, [issue.row for issue in plan.invalid_records])
        self.assertIn(3, [issue.row for issue in plan.invalid_records])
        self.assertTrue(any("missing value for required column 'subject'" in m for m in messages))
        self.assertTrue(any("missing value for required column 'start'" in m for m in messages))
        self.assertTrue(any("missing value for required column 'room'" in m for m in messages))

    def test_invalid_weekday_rejected(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monady,Physics,09:00,10:00,Room 1\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertFalse(plan.accepted)
        self.assertIn("not a valid day", plan.invalid_records[0].message)

    def test_invalid_time_formats_rejected(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Physics,9:00,10:00,Room 1\n"
            "Tuesday,Physics,08:00,25:00,Room 1\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertFalse(plan.accepted)
        rows = {issue.row for issue in plan.invalid_records}
        self.assertEqual(rows, {2, 3})
        self.assertTrue(all("expected 24-hour HH:MM" in issue.message for issue in plan.invalid_records))

    def test_end_before_or_equal_start_rejected(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Physics,10:00,10:00,Room 1\n"
            "Tuesday,Physics,11:00,10:00,Room 1\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.invalid_records), 2)
        self.assertTrue(all("ends before or at its start time" in issue.message for issue in plan.invalid_records))

    def test_multiple_validation_errors_with_row_numbers(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Physics,09:00,10:00,Room 1\n"
            "Monady,Bad Day,09:00,10:00,Room 1\n"
            "Tuesday,Bad Time,9:00,10:00,Room 1\n"
            "Wednesday,Bad Order,10:00,09:00,Room 1\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertFalse(plan.accepted)
        rows = [issue.row for issue in plan.invalid_records]
        self.assertEqual(rows, [3, 4, 5])
        self.assertEqual(len(plan.invalid_records), 3)

    def test_empty_file_is_handled(self):
        path = self.write_csv("")

        plan = plan_import(path, empty_timetable())
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.records_processed, 0)
        self.assertEqual(plan.new_classes, [])
        self.assertEqual(plan.invalid_records, [])

    def test_header_only_file_is_handled(self):
        path = self.write_csv("day,subject,start,end,room\n")

        plan = plan_import(path, empty_timetable())
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.records_processed, 0)
        self.assertEqual(plan.new_classes, [])

    def test_empty_record_rejected(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertFalse(plan.accepted)
        self.assertEqual(plan.invalid_records[0].row, 2)
        self.assertIn("record is empty", plan.invalid_records[0].message)

    def test_malformed_csv_is_handled(self):
        path = self.temp / "malformed.csv"
        path.write_bytes(
            b"day,subject,start,end,room\nMonday,Phy\x00sics,09:00,10:00,Room 1\n"
        )

        with self.assertRaises(ImportError_) as caught:
            plan_import(path, empty_timetable())
        self.assertIn("Malformed CSV file", str(caught.exception))

    def test_missing_csv_file_raises_error(self):
        missing = self.temp / "missing.csv"
        with self.assertRaises(ImportError_) as caught:
            plan_import(missing, empty_timetable())
        self.assertIn("CSV file not found", str(caught.exception))


class TestDuplicateHandling(ImportCsvTestCase):
    """Duplicate detection within CSV and against existing timetable."""

    def test_duplicate_within_csv_skipped_once(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Math,09:00,10:00,R1\n"
            "Monday,Math,09:00,10:00,R1\n"
            "Monday,Other,11:00,12:00,R2\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertTrue(plan.accepted)
        self.assertEqual(len(plan.new_classes), 2)
        self.assertEqual(len(plan.duplicates), 1)
        self.assertEqual(plan.duplicates[0].row, 3)
        self.assertEqual(plan.duplicates[0].day, "monday")

    def test_same_subject_at_different_times_not_duplicate(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Math,09:00,10:00,R1\n"
            "Monday,Math,11:00,12:00,R1\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertTrue(plan.accepted)
        self.assertEqual(len(plan.new_classes), 2)
        self.assertEqual(plan.duplicates, [])

    def test_different_room_or_time_not_duplicate(self):
        timetable = empty_timetable()
        timetable["monday"] = [
            {"subject": "Math", "start": "09:00", "end": "10:00", "room": "R1"}
        ]
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Math,09:00,10:00,R2\n"
            "Monday,Math,09:30,10:30,R1\n"
        )

        plan = plan_import(path, timetable)
        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.new_classes), 2)
        self.assertEqual(plan.duplicates, [])
        self.assertEqual(len(plan.conflicts), 3)


class TestConflictDetection(ImportCsvTestCase):
    """Scheduling conflict detection for imports."""

    def test_conflict_within_csv(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,A,09:00,10:30,R1\n"
            "Monday,B,10:00,11:00,R2\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.conflicts[0].day, "monday")
        self.assertEqual(plan.conflicts[0].first["subject"], "A")
        self.assertEqual(plan.conflicts[0].second["subject"], "B")

    def test_conflict_with_existing_timetable_class(self):
        timetable = empty_timetable()
        timetable["monday"] = [
            {"subject": "Existing", "start": "09:00", "end": "10:30", "room": "R1"}
        ]
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,New Class,10:00,11:00,R2\n"
        )

        plan = plan_import(path, timetable)
        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.conflicts[0].second["subject"], "Existing")

    def test_multiple_conflicts_reported(self):
        timetable = empty_timetable()
        timetable["monday"] = [
            {"subject": "Existing A", "start": "09:00", "end": "10:30", "room": "R1"},
            {"subject": "Existing B", "start": "14:00", "end": "15:30", "room": "R2"},
        ]
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,New A,10:00,11:00,R3\n"
            "Monday,New B,15:00,16:00,R4\n"
            "Monday,New C,17:00,18:00,R5\n"
        )

        plan = plan_import(path, timetable)
        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.conflicts), 2)
        days = {conflict.day for conflict in plan.conflicts}
        self.assertEqual(days, {"monday"})

    def test_different_days_do_not_conflict(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,A,09:00,11:00,R1\n"
            "Tuesday,A,09:00,11:00,R1\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(len(plan.new_classes), 2)

    def test_conflict_with_previous_day_overnight_existing_class(self):
        timetable = empty_timetable()
        timetable["friday"] = [
            {"subject": "Existing", "start": "23:00", "end": "01:00", "room": "R1"}
        ]
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Saturday,Imported,00:30,01:30,R2\n"
        )

        plan = plan_import(path, timetable)

        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.conflicts[0].first["subject"], "Imported")
        self.assertEqual(plan.conflicts[0].second["subject"], "Existing")

    def test_sunday_overnight_conflicts_with_monday(self):
        timetable = empty_timetable()
        timetable["sunday"] = [
            {"subject": "Sunday Overnight", "start": "23:30", "end": "00:30", "room": "R1"}
        ]
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Monday Early,00:00,01:00,R2\n"
        )

        plan = plan_import(path, timetable)

        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.conflicts), 1)

    def test_boundary_touching_intervals_do_not_conflict(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,A,09:00,10:00,R1\n"
            "Monday,B,10:00,11:00,R2\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(len(plan.new_classes), 2)

    def test_identical_duplicates_not_reported_as_conflicts(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,A,09:00,10:00,R1\n"
            "Monday,A,09:00,10:00,R1\n"
        )

        plan = plan_import(path, empty_timetable())
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(len(plan.duplicates), 1)
        self.assertEqual(len(plan.new_classes), 1)

    def test_render_plan_includes_conflict_details(self):
        path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,A,09:00,10:30,R1\n"
            "Monday,B,10:00,11:00,R2\n"
        )

        plan = plan_import(path, empty_timetable())
        text = render_plan(plan)
        self.assertIn("Scheduling conflicts:", text)
        self.assertIn("Conflict on Monday:", text)
        self.assertIn("09:00 - 10:30 : A [R1]", text)
        self.assertIn("10:00 - 11:00 : B [R2]", text)


class TestImportFileSafety(ImportCsvTestCase):
    """Dry-run behavior, successful writes, and safe failure handling."""

    def test_dry_run_does_not_modify_timetable(self):
        timetable = empty_timetable()
        timetable["monday"] = [
            {"subject": "Existing", "start": "08:00", "end": "09:00", "room": "R1"}
        ]
        timetable_path = self.write_json(timetable)
        before = timetable_path.read_text(encoding="utf-8")
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,New Class,10:00,11:00,R2\n"
        )

        plan = plan_import(csv_path, timetable)
        self.assertTrue(plan.accepted)
        self.assertEqual(timetable_path.read_text(encoding="utf-8"), before)

    def test_successful_import_writes_expected_entries(self):
        timetable = empty_timetable()
        timetable["monday"] = [
            {"subject": "Existing", "start": "08:00", "end": "09:00", "room": "R1"}
        ]
        timetable_path = self.write_json(timetable)
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Data Structures,09:00,10:30,Lab 1\n"
            "Tuesday,Operating Systems,14:00,15:00,Room 204\n"
        )

        plan = plan_import(csv_path, timetable)
        updated = apply_import(timetable, plan)
        save_timetable_atomic(updated, timetable_path)

        data = json.loads(timetable_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["monday"]), 2)
        self.assertEqual(data["monday"][0]["subject"], "Existing")
        self.assertEqual(data["monday"][1]["subject"], "Data Structures")
        self.assertEqual(len(data["tuesday"]), 1)

    def test_invalid_csv_leaves_timetable_unchanged(self):
        timetable = empty_timetable()
        timetable["monday"] = [
            {"subject": "Existing", "start": "08:00", "end": "09:00", "room": "R1"}
        ]
        timetable_path = self.write_json(timetable)
        before = timetable_path.read_text(encoding="utf-8")
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monady,Bad Day,09:00,10:00,R1\n"
        )

        plan = plan_import(csv_path, timetable)
        self.assertFalse(plan.accepted)
        self.assertEqual(timetable_path.read_text(encoding="utf-8"), before)

    def test_conflicting_csv_leaves_timetable_unchanged(self):
        timetable = empty_timetable()
        timetable["monday"] = [
            {"subject": "Existing", "start": "09:00", "end": "10:30", "room": "R1"}
        ]
        timetable_path = self.write_json(timetable)
        before = timetable_path.read_text(encoding="utf-8")
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,New Class,10:00,11:00,R2\n"
        )

        plan = plan_import(csv_path, timetable)
        self.assertFalse(plan.accepted)
        self.assertEqual(timetable_path.read_text(encoding="utf-8"), before)

    def test_duplicate_only_import_does_not_create_duplicates(self):
        timetable = empty_timetable()
        timetable["monday"] = [
            {"subject": "Math", "start": "09:00", "end": "10:00", "room": "R1"}
        ]
        timetable_path = self.write_json(timetable)
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Math,09:00,10:00,R1\n"
        )

        plan = plan_import(csv_path, timetable)
        self.assertTrue(plan.accepted)
        updated = apply_import(timetable, plan)
        save_timetable_atomic(updated, timetable_path)

        data = json.loads(timetable_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["monday"]), 1)
        self.assertEqual(data["monday"][0]["subject"], "Math")

    def test_failed_write_does_not_corrupt_original(self):
        timetable_path = self.write_json(empty_timetable())
        before = timetable_path.read_text(encoding="utf-8")
        data = {"monday": [{"subject": "Math"}], "broken": object()}

        with self.assertRaises(TypeError):
            save_timetable_atomic(data, timetable_path)

        self.assertEqual(timetable_path.read_text(encoding="utf-8"), before)
        leftovers = list(self.temp.glob(".timetable.json.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_temporary_files_cleaned_up_after_success(self):
        timetable_path = self.write_json(empty_timetable())
        data = {"monday": [{"subject": "Math", "start": "09:00", "end": "10:00", "room": "R1"}]}

        save_timetable_atomic(data, timetable_path)
        leftovers = list(self.temp.glob(".timetable.json.*.tmp"))
        self.assertEqual(leftovers, [])


class TestImportCsvCli(ImportCsvTestCase):
    """CLI behavior for the import-csv subcommand."""

    def test_successful_import_exit_code_and_output(self):
        timetable_path = self.write_json(empty_timetable())
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Data Structures,09:00,10:30,Lab 1\n"
        )

        code, out, err = self.run_cli("--file", str(timetable_path), "import-csv", str(csv_path))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Import summary", out)
        self.assertIn("CSV records processed: 1", out)
        self.assertIn("New classes to import: 1", out)
        self.assertIn("Successfully imported 1 class(es)", out)

        data = json.loads(timetable_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["monday"]), 1)

    def test_successful_dry_run_exit_code_and_output(self):
        timetable_path = self.write_json(empty_timetable())
        before = timetable_path.read_text(encoding="utf-8")
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Data Structures,09:00,10:30,Lab 1\n"
        )

        code, out, err = self.run_cli("--file", str(timetable_path), "import-csv", str(csv_path), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Dry run summary", out)
        self.assertIn("Dry run complete. No files were modified.", out)
        self.assertEqual(timetable_path.read_text(encoding="utf-8"), before)

    def test_invalid_csv_exit_code(self):
        timetable_path = self.write_json(empty_timetable())
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monady,Bad Day,09:00,10:00,R1\n"
        )

        code, out, err = self.run_cli("--file", str(timetable_path), "import-csv", str(csv_path))
        self.assertEqual(code, 1)
        self.assertIn("Validation errors:", err)
        self.assertIn("Could not import CSV due to validation errors.", err)
        self.assertNotIn("Successfully imported", out)

    def test_conflict_exit_code(self):
        timetable_path = self.write_json(empty_timetable())
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,A,09:00,10:30,R1\n"
            "Monday,B,10:00,11:00,R2\n"
        )

        code, out, err = self.run_cli("--file", str(timetable_path), "import-csv", str(csv_path))
        self.assertEqual(code, 1)
        self.assertIn("Scheduling conflicts:", err)
        self.assertIn("Could not import CSV due to scheduling conflicts.", err)
        self.assertNotIn("Successfully imported", out)

    def test_missing_csv_file_exit_code(self):
        timetable_path = self.write_json(empty_timetable())
        missing = self.temp / "missing.csv"

        code, out, err = self.run_cli("--file", str(timetable_path), "import-csv", str(missing))
        self.assertEqual(code, 1)
        self.assertIn("CSV file not found", err)

    def test_invalid_timetable_json_exit_code(self):
        timetable_path = self.write_json("Not valid JSON {", name="broken.json")
        csv_path = self.write_csv(
            "day,subject,start,end,room\n"
            "Monday,Data Structures,09:00,10:30,Lab 1\n"
        )

        code, out, err = self.run_cli("--file", str(timetable_path), "import-csv", str(csv_path))
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", err)

    def test_existing_commands_remain_functional(self):
        timetable_path = self.write_json(empty_timetable())
        code, out, err = self.run_cli("--file", str(timetable_path), "week")
        self.assertEqual(code, 0)
        self.assertIn("Weekly Timetable Overview", out)


class TestLoadTimetableForImport(ImportCsvTestCase):
    """Tests for timetable loading behavior used by the importer."""

    def test_missing_file_returns_empty_timetable(self):
        data = load_timetable_for_import(self.temp / "missing.json")
        self.assertEqual(data, empty_timetable())

    def test_invalid_json_raises_error(self):
        path = self.write_json("Not valid JSON {", name="broken.json")
        with self.assertRaises(ImportError_) as caught:
            load_timetable_for_import(path)
        self.assertIn("invalid JSON", str(caught.exception))

    def test_invalid_structure_raises_error(self):
        path = self.write_json({"monday": "not a list"}, name="bad.json")
        with self.assertRaises(ImportError_) as caught:
            load_timetable_for_import(path)
        self.assertIn("Expected list of classes", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
