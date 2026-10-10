"""Unit tests for the reusable timetable filtering layer and the `filter` CLI."""

import unittest
from contextlib import redirect_stdout
from io import StringIO

from timetable.filter import day_matches, filter_timetable, overlaps_range, render_filter


SAMPLE = {
    "monday": [
        {"subject": "Maths", "start": "09:00", "end": "10:00", "room": "Room 101"},
        {"subject": "Physics", "start": "11:00", "end": "12:00", "room": "Room 204"},
        {"subject": "Chemistry", "start": "14:00", "end": "15:00", "room": "Lab 1"},
    ],
    "tuesday": [
        {"subject": "Biology", "start": "09:00", "end": "10:00", "room": "Lab 1"},
        {"subject": "History", "start": "13:00", "end": "14:30", "room": "Room 302"},
    ],
    "wednesday": [],
    "saturday": [],
}


def subjects(results, day):
    return [slot["subject"] for slot in results.get(day, [])]


class TestFilterByDay(unittest.TestCase):
    """Filtering by day alone."""

    def test_filter_by_day(self):
        """Only the requested day should be returned."""
        results = filter_timetable(SAMPLE, day="monday")
        self.assertEqual(list(results.keys()), ["monday"])
        self.assertEqual(subjects(results, "monday"), ["Maths", "Physics", "Chemistry"])

    def test_day_filter_is_case_insensitive(self):
        """`Monday` and `MONDAY` should behave like `monday`."""
        self.assertEqual(
            subjects(filter_timetable(SAMPLE, day="Monday"), "monday"),
            ["Maths", "Physics", "Chemistry"],
        )

    def test_invalid_day_returns_empty(self):
        """A name that is not a weekday should return no matches."""
        self.assertEqual(filter_timetable(SAMPLE, day="mondy"), {})

    def test_empty_day_returns_empty(self):
        """A weekday with no classes should return no matches."""
        self.assertEqual(filter_timetable(SAMPLE, day="wednesday"), {})


class TestFilterByRoom(unittest.TestCase):
    """Filtering by room alone."""

    def test_filter_by_room(self):
        """Every class in the room, on any day, should match exactly."""
        results = filter_timetable(SAMPLE, room="Lab 1")
        self.assertEqual(subjects(results, "monday"), ["Chemistry"])
        self.assertEqual(subjects(results, "tuesday"), ["Biology"])

    def test_room_matching_is_exact(self):
        """Rooms must match exactly, not by substring or case."""
        self.assertEqual(filter_timetable(SAMPLE, room="lab 1"), {})
        self.assertEqual(filter_timetable(SAMPLE, room="Lab"), {})


class TestFilterByTime(unittest.TestCase):
    """Filtering by start and/or end time."""

    def test_filter_by_start_time(self):
        """A range with only a start keeps classes that begin after it."""
        results = filter_timetable(SAMPLE, day="monday", start="10:00")
        self.assertEqual(subjects(results, "monday"), ["Physics", "Chemistry"])

    def test_filter_by_end_time(self):
        """A range with only an end keeps classes that begin before it."""
        results = filter_timetable(SAMPLE, day="monday", end="12:00")
        self.assertEqual(subjects(results, "monday"), ["Maths", "Physics"])

    def test_filter_by_start_and_end_time(self):
        """Start and end together keep only classes inside the range."""
        results = filter_timetable(SAMPLE, day="monday", start="09:00", end="12:00")
        self.assertEqual(subjects(results, "monday"), ["Maths", "Physics"])


class TestCombinedFilters(unittest.TestCase):
    """Combinations of day, room and time range."""

    def test_day_and_room(self):
        """Day and room filters combine with logical AND."""
        results = filter_timetable(SAMPLE, day="monday", room="Room 204")
        self.assertEqual(subjects(results, "monday"), ["Physics"])
        self.assertNotIn("tuesday", results)

    def test_day_time_and_room(self):
        """Day, time range and room filters all combine."""
        results = filter_timetable(SAMPLE, day="monday", start="09:00",
                                   end="15:00", room="Lab 1")
        self.assertEqual(subjects(results, "monday"), ["Chemistry"])

    def test_no_filters_returns_everything(self):
        """With no filters every non-empty day is returned unchanged."""
        results = filter_timetable(SAMPLE)
        self.assertEqual(set(results.keys()),
                         {d for d, s in SAMPLE.items() if s})
        for day, slots in SAMPLE.items():
            if slots:
                self.assertEqual(results[day], slots)


class TestTimeRangeBoundaries(unittest.TestCase):
    """Half-open interval overlap semantics."""

    def setUp(self):
        self.slot = {"subject": "Maths", "start": "09:00", "end": "10:00",
                     "room": "Room 101"}

    def test_class_inside_range_matches(self):
        """A class fully inside the range should match."""
        self.assertTrue(overlaps_range(self.slot, "08:00", "12:00"))

    def test_class_overlapping_range_matches(self):
        """A class that overlaps the range on either side should match."""
        self.assertTrue(overlaps_range(self.slot, "09:30", "11:00"))
        self.assertTrue(overlaps_range(self.slot, "08:00", "09:30"))

    def test_class_starting_exactly_at_range_end_is_outside(self):
        """A class starting exactly when the range ends does not overlap."""
        self.assertFalse(overlaps_range(self.slot, "08:00", "09:00"))

    def test_class_ending_exactly_at_range_start_is_outside(self):
        """A class ending exactly when the range starts does not overlap."""
        self.assertFalse(overlaps_range(self.slot, "10:00", "12:00"))

    def test_class_starting_exactly_at_range_start_matches(self):
        """A class starting exactly at the range start overlaps."""
        self.assertTrue(overlaps_range(self.slot, "09:00", "11:00"))

    def test_class_ending_exactly_at_range_end_matches(self):
        """A class ending exactly at the range end overlaps."""
        self.assertTrue(overlaps_range(self.slot, "08:00", "10:00"))

    def test_class_outside_range_does_not_match(self):
        """A class entirely outside the range should not match."""
        self.assertFalse(overlaps_range(self.slot, "11:00", "12:00"))
        self.assertFalse(overlaps_range(self.slot, "07:00", "08:00"))


class TestFilterNoAndMultipleMatches(unittest.TestCase):
    """Empty results and multiple matches."""

    def test_no_matching_classes_returns_empty(self):
        """No class in the requested range should return an empty result."""
        self.assertEqual(filter_timetable(SAMPLE, day="monday",
                                          start="16:00", end="17:00"), {})

    def test_multiple_matching_classes(self):
        """Several classes can match the same filter."""
        results = filter_timetable(SAMPLE, day="monday", start="08:00", end="16:00")
        self.assertEqual(subjects(results, "monday"),
                         ["Maths", "Physics", "Chemistry"])


class TestDayMatches(unittest.TestCase):
    """The day_matches helper."""

    def test_none_matches_any_day(self):
        """A requested day of None matches any day."""
        self.assertTrue(day_matches("monday", None))

    def test_case_insensitive(self):
        """Day comparison ignores capital letters."""
        self.assertTrue(day_matches("monday", "Monday"))
        self.assertFalse(day_matches("monday", "tuesday"))


class TestRenderFilter(unittest.TestCase):
    """The text printed by render_filter."""

    @staticmethod
    def render(results, **kwargs):
        output = StringIO()
        with redirect_stdout(output):
            render_filter(results, **kwargs)
        return output.getvalue()

    def test_shows_subject_time_and_room(self):
        """Filtered rows should show subject, time and room."""
        results = filter_timetable(SAMPLE, day="monday", start="09:00", end="12:00")
        text = self.render(results, day="monday", start="09:00", end="12:00")
        self.assertIn("Maths", text)
        self.assertIn("09:00 - 10:00", text)
        self.assertIn("Room 101", text)
        self.assertIn("60 min", text)

    def test_no_matches_message(self):
        """An empty result should print a friendly message, not an error."""
        results = filter_timetable(SAMPLE, day="monday", start="16:00", end="17:00")
        text = self.render(results, day="monday", start="16:00", end="17:00")
        self.assertIn("No classes match the given filters.", text)


    def test_classes_listed_in_start_time_order(self):
        """Rows should be in start-time order however they are stored."""
        unsorted = {
            "tuesday": [
                {"subject": "Databases", "start": "11:00", "end": "12:30", "room": "Lab 1"},
                {"subject": "Operating Systems", "start": "08:30", "end": "10:00", "room": "Room 302"},
                {"subject": "Software Engineering", "start": "13:30", "end": "15:00", "room": "Room 105"},
            ],
        }
        results = filter_timetable(unsorted, day="tuesday")
        text = self.render(results, day="tuesday")
        self.assertLess(text.index("Operating Systems"), text.index("Databases"))
        self.assertLess(text.index("Databases"), text.index("Software Engineering"))

    def test_start_time_order_applies_to_each_day(self):
        """With no day given, each weekday's rows are still ordered by start time."""
        unsorted = {
            "monday": [
                {"subject": "Late", "start": "14:00", "end": "15:00", "room": "A"},
                {"subject": "Early", "start": "09:00", "end": "10:00", "room": "A"},
            ],
            "tuesday": [
                {"subject": "TueLate", "start": "13:00", "end": "14:00", "room": "A"},
                {"subject": "TueEarly", "start": "08:00", "end": "09:00", "room": "A"},
            ],
        }
        text = self.render(filter_timetable(unsorted, room="A"), room="A")
        order = [text.index(name) for name in ("Early", "Late", "TueEarly", "TueLate")]
        self.assertEqual(order, sorted(order))


if __name__ == "__main__":
    unittest.main()