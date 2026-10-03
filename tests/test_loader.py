"""Unit tests for timetable loading and duration calculation."""

import unittest
from contextlib import redirect_stdout
from io import StringIO

from timetable.loader import load_timetable
from timetable.display import WEEKDAYS, calc_duration, render_day


class TestTimetableLoader(unittest.TestCase):

    def test_load_existing_timetable(self):
        """Loader should return a dictionary containing timetable data."""
        data = load_timetable()
        self.assertIsInstance(data, dict)
        self.assertIn("monday", data)

    def test_render_each_timetable_day(self):
        """Each weekday key should render its own heading and schedule."""
        data = load_timetable()

        for day in WEEKDAYS:
            with self.subTest(day=day):
                output = StringIO()
                with redirect_stdout(output):
                    render_day(data, day)

                heading = output.getvalue().splitlines()[1]
                self.assertEqual(heading, f"📅 Timetable for {day.capitalize()}:")

    def test_load_nonexistent_file(self):
        """Loading a non-existent file should return a default weekday structure."""
        data = load_timetable("does_not_exist.json")
        self.assertIsInstance(data, dict)
        self.assertEqual(data["monday"], [])

    def test_calc_duration_daytime(self):
        """Normal daytime slot duration should be calculated in minutes."""
        # 09:00 to 10:30 is 90 minutes
        self.assertEqual(calc_duration("09:00", "10:30"), 90)


if __name__ == "__main__":
    unittest.main()
