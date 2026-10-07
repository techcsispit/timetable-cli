import unittest
from timetable.conflicts import do_overlap, find_conflicts


class TestConflicts(unittest.TestCase):
    def test_touching_boundaries_do_not_overlap(self):
        a = {"start": "09:00", "end": "10:00", "subject": "Math", "room": "101"}
        b = {"start": "10:00", "end": "11:00", "subject": "Physics", "room": "102"}
        self.assertFalse(do_overlap(a, b))

    def test_partial_overlap_detected(self):
        a = {"start": "09:00", "end": "10:30", "subject": "Math", "room": "101"}
        b = {"start": "10:00", "end": "11:00", "subject": "Physics", "room": "102"}
        self.assertTrue(do_overlap(a, b))

    def test_contained_overlap_detected(self):
        a = {"start": "09:00", "end": "12:00", "subject": "Lab", "room": "L1"}
        b = {"start": "10:00", "end": "11:00", "subject": "Tut", "room": "L1"}
        self.assertTrue(do_overlap(a, b))

    def test_find_conflicts_deterministic_and_no_duplicates(self):
        data = {
            "monday": [
                {"start": "09:00", "end": "10:30", "subject": "Math", "room": "101"},
                {"start": "10:00", "end": "11:00", "subject": "Physics", "room": "102"},
            ],
            "tuesday": [
                {"start": "09:00", "end": "10:00", "subject": "Chem", "room": "103"},
            ]
        }
        conflicts = find_conflicts(data)
        self.assertEqual(len(conflicts), 1)
        day, a, b = conflicts[0]
        self.assertEqual(day, "monday")
        self.assertEqual(a["subject"], "Math")
        self.assertEqual(b["subject"], "Physics")

    def test_empty_timetable_returns_no_conflicts(self):
        self.assertEqual(find_conflicts({}), [])


if __name__ == "__main__":
    unittest.main()