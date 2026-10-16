"""Unit and integration tests for iCalendar (ICS) import and the `import-ics` command."""

import datetime
import json
import sys
import tempfile
import unittest
import zoneinfo
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from timetable.cli import main
from timetable.history import list_snapshots
from timetable.import_ics import (
    IcsImportPlan,
    expand_vevent,
    parse_ics_content,
    parse_ics_datetime,
    plan_ics_import,
    render_ics_plan,
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


class ImportIcsTestCase(unittest.TestCase):
    """Shared temporary directory and CLI runner for import-ics tests."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp_dir.name)
        self.addCleanup(self._temp_dir.cleanup)

    def write_ics(self, text, name="calendar.ics"):
        path = self.temp / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, data, name="timetable.json"):
        path = self.temp / name
        text = data if isinstance(data, str) else json.dumps(data, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    def run_import(self, *args):
        """Runs `timetable import-ics ...` and returns (code, out, err)."""
        # If --file is passed in args, move it before import-ics
        arg_list = list(args)
        prefix = []
        rest = []
        i = 0
        while i < len(arg_list):
            if arg_list[i] == "--file" and i + 1 < len(arg_list):
                prefix.extend(["--file", arg_list[i + 1]])
                i += 2
            else:
                rest.append(arg_list[i])
                i += 1
        return self.run_cli(*(prefix + ["import-ics"] + rest))

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


class TestIcsParsingAndExpansion(ImportIcsTestCase):
    """Unit tests covering parsing and recurrence expansion requirements."""

    def test_1_valid_non_recurring_event(self):
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "PRODID:-//Example//EN\n"
            "BEGIN:VEVENT\n"
            "UID:event-1@example.com\n"
            "SUMMARY:Single Lecture\n"
            "LOCATION:Room 101\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T103000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.events_found, 1)
        self.assertEqual(plan.occurrences_generated, 1)
        self.assertEqual(len(plan.new_classes), 1)
        line, day, slot = plan.new_classes[0]
        self.assertEqual(day, "monday")
        self.assertEqual(slot["subject"], "Single Lecture")
        self.assertEqual(slot["start"], "09:00")
        self.assertEqual(slot["end"], "10:30")
        self.assertEqual(slot["room"], "Room 101")

    def test_2_weekly_event_expanded_across_several_weeks(self):
        # Weekly event on Mondays for 4 weeks in June 2026 (June 1, 8, 15, 22)
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "PRODID:-//Example//EN\n"
            "BEGIN:VEVENT\n"
            "UID:weekly-math@example.com\n"
            "SUMMARY:Mathematics\n"
            "LOCATION:Hall A\n"
            "DTSTART:20260601T100000\n"
            "DTEND:20260601T110000\n"
            "RRULE:FREQ=WEEKLY\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 28)  # 4 Mondays

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.events_found, 1)
        self.assertEqual(plan.occurrences_generated, 4)
        # Because they all map to Monday 10:00-11:00 Mathematics, they deduplicate to 1 new class
        self.assertEqual(len(plan.new_classes), 1)
        self.assertEqual(len(plan.duplicates), 3)

    def test_3_byday_interval_and_bounded_recurrence(self):
        # Bi-weekly on Monday and Wednesday, COUNT=4
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:biweekly@example.com\n"
            "SUMMARY:Algorithms\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T100000\n"
            "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=4\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        # June 1 (Mo), June 3 (We), June 15 (Mo), June 17 (We)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.occurrences_generated, 4)
        # Yields two distinct timetable slots: Monday 09:00-10:00 and Wednesday 09:00-10:00
        days = {entry[1] for entry in plan.new_classes}
        self.assertEqual(days, {"monday", "wednesday"})
        self.assertEqual(len(plan.new_classes), 2)
        self.assertEqual(len(plan.duplicates), 2)

    def test_4_rdate_and_exdate(self):
        # Weekly event on Mondays with EXDATE for June 8 and RDATE for Thursday June 4
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:rdate-exdate@example.com\n"
            "SUMMARY:Physics Lab\n"
            "DTSTART:20260601T140000\n"
            "DTEND:20260601T160000\n"
            "RRULE:FREQ=WEEKLY;COUNT=3\n"
            "EXDATE:20260608T140000\n"
            "RDATE:20260604T140000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 20)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertTrue(plan.accepted)
        # Occurrences: June 1 (Mo), June 4 (Th via RDATE), June 15 (Mo). (June 8 is excluded)
        self.assertEqual(plan.occurrences_generated, 3)
        days = {entry[1] for entry in plan.new_classes}
        self.assertEqual(days, {"monday", "thursday"})

    def test_5_boundary_occurrences(self):
        # Event exactly on the boundary dates: June 1 and June 5
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:boundary-start@example.com\n"
            "SUMMARY:Start Boundary\n"
            "DTSTART:20260601T080000\n"
            "DTEND:20260601T090000\n"
            "END:VEVENT\n"
            "BEGIN:VEVENT\n"
            "UID:boundary-end@example.com\n"
            "SUMMARY:End Boundary\n"
            "DTSTART:20260605T170000\n"
            "DTEND:20260605T180000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 5)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.occurrences_generated, 2)
        subjects = [entry[2]["subject"] for entry in plan.new_classes]
        self.assertIn("Start Boundary", subjects)
        self.assertIn("End Boundary", subjects)

    def test_6_utc_and_timezone_aware_events(self):
        # Event specified in UTC: 04:30:00Z -> In Asia/Kolkata (+05:30) it becomes 10:00:00
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:utc-event@example.com\n"
            "SUMMARY:Global Seminar\n"
            "DTSTART:20260601T043000Z\n"
            "DTEND:20260601T060000Z\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 1)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date, timezone_name="Asia/Kolkata")
        self.assertTrue(plan.accepted)
        self.assertEqual(len(plan.new_classes), 1)
        slot = plan.new_classes[0][2]
        self.assertEqual(slot["start"], "10:00")
        self.assertEqual(slot["end"], "11:30")

    def test_7_daylight_saving_transition(self):
        # Event in Europe/London across DST boundary in October 2026 (clock falls back 1h on 2026-10-25)
        # 10:00 local time remains 10:00 local time
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:dst-event@example.com\n"
            "SUMMARY:DST Lecture\n"
            "DTSTART;TZID=Europe/London:20261019T100000\n"
            "DTEND;TZID=Europe/London:20261019T110000\n"
            "RRULE:FREQ=WEEKLY;COUNT=2\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 10, 19)
        to_date = datetime.date(2026, 10, 31)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date, timezone_name="Europe/London")
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.occurrences_generated, 2)
        # Both occurrences are on Monday 10:00-11:00
        self.assertEqual(len(plan.new_classes), 1)
        self.assertEqual(plan.new_classes[0][2]["start"], "10:00")
        self.assertEqual(plan.new_classes[0][2]["end"], "11:00")

    def test_weekly_new_york_event_converts_each_occurrence_after_dst(self):
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:new-york-dst@example.com\n"
            "SUMMARY:New York Lecture\n"
            "DTSTART;TZID=America/New_York:20261026T090000\n"
            "DTEND;TZID=America/New_York:20261026T100000\n"
            "RRULE:FREQ=WEEKLY;COUNT=2\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        events, issues = parse_ics_content(ics_text)
        self.assertEqual(issues, [])

        occurrences, issues = expand_vevent(
            events[0],
            datetime.date(2026, 10, 26),
            datetime.date(2026, 11, 2),
            zoneinfo.ZoneInfo("Asia/Kolkata"),
        )

        self.assertEqual(issues, [])
        self.assertEqual(
            [(occ.slot["start"], occ.slot["end"]) for occ in occurrences],
            [("18:30", "19:30"), ("19:30", "20:30")],
        )

    def test_weekly_london_event_converts_each_occurrence_after_dst(self):
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:london-dst@example.com\n"
            "SUMMARY:London Lecture\n"
            "DTSTART;TZID=Europe/London:20261019T100000\n"
            "DTEND;TZID=Europe/London:20261019T110000\n"
            "RRULE:FREQ=WEEKLY;COUNT=2\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        events, issues = parse_ics_content(ics_text)
        self.assertEqual(issues, [])

        occurrences, issues = expand_vevent(
            events[0],
            datetime.date(2026, 10, 19),
            datetime.date(2026, 10, 26),
            zoneinfo.ZoneInfo("Asia/Kolkata"),
        )

        self.assertEqual(issues, [])
        self.assertEqual(
            [(occ.slot["start"], occ.slot["end"]) for occ in occurrences],
            [("14:30", "15:30"), ("15:30", "16:30")],
        )

    def test_8_event_crossing_midnight(self):
        # Overnight event: 23:00 to 01:00 next day
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:night-event@example.com\n"
            "SUMMARY:Astronomy Observation\n"
            "DTSTART:20260601T230000\n"
            "DTEND:20260602T010000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 2)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertTrue(plan.accepted)
        self.assertEqual(len(plan.new_classes), 1)
        line, day, slot = plan.new_classes[0]
        self.assertEqual(day, "monday")
        self.assertEqual(slot["start"], "23:00")
        self.assertEqual(slot["end"], "01:00")

    def test_9_all_day_events_rejected(self):
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:allday@example.com\n"
            "SUMMARY:Holiday\n"
            "DTSTART;VALUE=DATE:20260601\n"
            "DTEND;VALUE=DATE:20260602\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertFalse(plan.accepted)
        self.assertTrue(any("all-day event" in issue.message for issue in plan.invalid_records))

    def test_10_missing_start_or_end_timestamps(self):
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:no-end@example.com\n"
            "SUMMARY:No End Time\n"
            "DTSTART:20260601T090000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertFalse(plan.accepted)
        self.assertTrue(any("missing both DTEND and DURATION" in issue.message for issue in plan.invalid_records))

    def test_11_zero_or_negative_duration(self):
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:zero-dur@example.com\n"
            "SUMMARY:Zero Duration\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T090000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertFalse(plan.accepted)
        self.assertTrue(any("zero or negative duration" in issue.message for issue in plan.invalid_records))

    def test_12_malformed_ics_input(self):
        ics_text = "NOT_A_VALID_ICS_FILE\nNO_COLONS_OR_COMPONENTS"
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertFalse(plan.accepted)
        self.assertTrue(len(plan.invalid_records) > 0)

    def test_13_unsupported_recurrence_rules(self):
        # Daily recurrence is not supported by weekly timetable schema
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:daily-event@example.com\n"
            "SUMMARY:Daily Standup\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T093000\n"
            "RRULE:FREQ=DAILY\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertFalse(plan.accepted)
        self.assertTrue(any("unsupported recurrence frequency 'DAILY'" in issue.message for issue in plan.invalid_records))

    def test_14_cancelled_events_skipped(self):
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:cancelled@example.com\n"
            "SUMMARY:Cancelled Class\n"
            "STATUS:CANCELLED\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T100000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertTrue(plan.accepted)
        self.assertEqual(plan.occurrences_generated, 0)
        self.assertEqual(len(plan.new_classes), 0)

    def test_15_conflict_with_existing_timetable_entry(self):
        timetable = empty_timetable()
        timetable["monday"].append({
            "subject": "Existing Chemistry",
            "start": "09:00",
            "end": "10:30",
            "room": "Lab 1",
        })
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:clash@example.com\n"
            "SUMMARY:Conflicting Physics\n"
            "DTSTART:20260601T100000\n"
            "DTEND:20260601T110000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, timetable, from_date, to_date)
        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.conflicts[0].day, "monday")

    def test_16_conflict_between_two_imported_events(self):
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:ev1@example.com\n"
            "SUMMARY:Event 1\n"
            "DTSTART:20260601T09:00:00\n"
            "DTEND:20260601T10:30:00\n"
            "END:VEVENT\n"
            "BEGIN:VEVENT\n"
            "UID:ev2@example.com\n"
            "SUMMARY:Event 2\n"
            "DTSTART:20260601T10:00:00\n"
            "DTEND:20260601T11:00:00\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        # Note: in ICS datetime format is YYYYMMDDTHHMMSS
        ics_text = ics_text.replace("09:00:00", "090000").replace("10:30:00", "103000").replace("10:00:00", "100000").replace("11:00:00", "110000")
        path = self.write_ics(ics_text)
        from_date = datetime.date(2026, 6, 1)
        to_date = datetime.date(2026, 6, 30)

        plan = plan_ics_import(path, empty_timetable(), from_date, to_date)
        self.assertFalse(plan.accepted)
        self.assertEqual(len(plan.conflicts), 1)


class TestCliImportIcs(ImportIcsTestCase):
    """End-to-end CLI integration tests for `import-ics`."""

    def test_17_atomic_rejection_when_any_event_is_invalid(self):
        tt_file = self.write_json(empty_timetable())
        # One valid event and one invalid all-day event
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:valid@example.com\n"
            "SUMMARY:Valid Class\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T100000\n"
            "END:VEVENT\n"
            "BEGIN:VEVENT\n"
            "UID:invalid@example.com\n"
            "SUMMARY:Invalid All Day\n"
            "DTSTART;VALUE=DATE:20260602\n"
            "DTEND;VALUE=DATE:20260603\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        ics_path = self.write_ics(ics_text)

        code, out, err = self.run_import(
            str(ics_path),
            "--file", str(tt_file),
            "--from", "2026-06-01",
            "--to", "2026-06-30",
        )
        self.assertEqual(code, 1)
        self.assertIn("Validation errors", err)

        # Timetable file remains empty
        saved = json.loads(tt_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["monday"], [])

    def test_18_dry_run_leaves_timetable_and_history_unchanged(self):
        tt_file = self.write_json(empty_timetable())
        history_dir = self.temp / ".timetable-history"

        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:dryrun@example.com\n"
            "SUMMARY:Preview Class\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T100000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        ics_path = self.write_ics(ics_text)

        code, out, err = self.run_import(
            str(ics_path),
            "--file", str(tt_file),
            "--from", "2026-06-01",
            "--to", "2026-06-30",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("Dry run complete. No files were modified.", out)

        # File is untouched
        saved = json.loads(tt_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["monday"], [])
        # History is empty
        self.assertFalse(history_dir.exists())

    def test_19_successful_import_preserves_pre_existing_entries(self):
        initial = empty_timetable()
        initial["tuesday"].append({
            "subject": "Pre-existing Tuesday Class",
            "start": "11:00",
            "end": "12:00",
            "room": "Room 5",
        })
        tt_file = self.write_json(initial)

        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:new@example.com\n"
            "SUMMARY:New Monday Class\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T100000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        ics_path = self.write_ics(ics_text)

        code, out, err = self.run_import(
            str(ics_path),
            "--file", str(tt_file),
            "--from", "2026-06-01",
            "--to", "2026-06-30",
        )
        self.assertEqual(code, 0)
        self.assertIn("Successfully imported 1 class(es)", out)

        saved = json.loads(tt_file.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["tuesday"]), 1)
        self.assertEqual(saved["tuesday"][0]["subject"], "Pre-existing Tuesday Class")
        self.assertEqual(len(saved["monday"]), 1)
        self.assertEqual(saved["monday"][0]["subject"], "New Monday Class")

        # Snapshot created
        history_dir = self.temp / ".timetable-history"
        snaps = list_snapshots(history_dir)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["operation"], "import-ics")

    def test_20_persistence_failure_not_corrupting_existing_data(self):
        initial = empty_timetable()
        initial["friday"].append({
            "subject": "Valuable Data",
            "start": "10:00",
            "end": "11:00",
            "room": "Room 1",
        })
        tt_file = self.write_json(initial)

        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:persist-fail@example.com\n"
            "SUMMARY:Should Fail Persistence\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T100000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        ics_path = self.write_ics(ics_text)

        with patch("timetable.cli._write_timetable_with_snapshot", side_effect=OSError("Disk write failed")):
            code, out, err = self.run_import(
                str(ics_path),
                "--file", str(tt_file),
                "--from", "2026-06-01",
                "--to", "2026-06-30",
            )
            self.assertEqual(code, 1)
            self.assertIn("Disk write failed", err)

        # File is unchanged
        saved = json.loads(tt_file.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["friday"]), 1)
        self.assertEqual(saved["friday"][0]["subject"], "Valuable Data")

    def test_21_repeated_import_behavior(self):
        tt_file = self.write_json(empty_timetable())
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:repeat@example.com\n"
            "SUMMARY:Repeated Lecture\n"
            "DTSTART:20260601T090000\n"
            "DTEND:20260601T100000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        ics_path = self.write_ics(ics_text)

        # First import
        code1, out1, _ = self.run_import(
            str(ics_path),
            "--file", str(tt_file),
            "--from", "2026-06-01",
            "--to", "2026-06-30",
        )
        self.assertEqual(code1, 0)
        self.assertIn("Successfully imported 1 class(es)", out1)

        # Second import: should deduplicate against existing timetable and import 0 classes
        code2, out2, _ = self.run_import(
            str(ics_path),
            "--file", str(tt_file),
            "--from", "2026-06-01",
            "--to", "2026-06-30",
        )
        self.assertEqual(code2, 0)
        self.assertIn("Duplicate entries skipped: 1", out2)
        self.assertIn("Successfully imported 0 class(es)", out2)

    def test_22_cli_argument_parsing_and_exit_codes(self):
        # Invalid date format
        ics_path = self.write_ics("BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")
        code, out, err = self.run_import(str(ics_path), "--from", "not-a-date")
        self.assertEqual(code, 1)
        self.assertIn("Invalid --from date", err)

        # Inverted range (--to earlier than --from)
        code, out, err = self.run_import(
            str(ics_path),
            "--from", "2026-07-01",
            "--to", "2026-06-01",
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot be earlier than", err)

        # Invalid timezone
        code, out, err = self.run_import(
            str(ics_path),
            "--timezone", "Invalid/Fake_Zone",
        )
        self.assertEqual(code, 1)
        self.assertIn("Invalid timezone", err)

    def test_23_empty_calendar_and_empty_expansion_window(self):
        # Empty calendar
        ics_path = self.write_ics("BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")
        tt_file = self.write_json(empty_timetable())
        code, out, _ = self.run_import(
            str(ics_path),
            "--file", str(tt_file),
            "--from", "2026-06-01",
            "--to", "2026-06-30",
        )
        self.assertEqual(code, 0)
        self.assertIn("Successfully imported 0 class(es)", out)

        # Non-empty calendar but window produces 0 occurrences
        ics_text = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "BEGIN:VEVENT\n"
            "UID:future@example.com\n"
            "SUMMARY:Future Class\n"
            "DTSTART:20261201T090000\n"
            "DTEND:20261201T100000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        future_ics = self.write_ics(ics_text, "future.ics")
        code2, out2, _ = self.run_import(
            str(future_ics),
            "--file", str(tt_file),
            "--from", "2026-06-01",
            "--to", "2026-06-30",
        )
        self.assertEqual(code2, 0)
        self.assertIn("Occurrences generated: 0", out2)
        self.assertIn("Successfully imported 0 class(es)", out2)


if __name__ == "__main__":
    unittest.main()

