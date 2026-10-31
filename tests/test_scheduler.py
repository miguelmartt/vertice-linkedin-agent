import unittest
from datetime import datetime, timedelta

from vla.scheduler import ScheduleError, is_due, next_due, parse

# Un miércoles.
WED = datetime(2026, 11, 4, 10, 0)


class SchedulerTest(unittest.TestCase):
    def test_several_weekdays(self):
        s = parse("mon,wed,fri 08:30")
        self.assertEqual(s.weekdays, (0, 2, 4))
        self.assertTrue(is_due(s, WED, WED - timedelta(days=2)))  # el lunes ya corrió; toca el de hoy
        self.assertFalse(is_due(s, WED, WED.replace(hour=8, minute=31)))
        self.assertEqual(next_due(s, WED), datetime(2026, 11, 6, 8, 30))

    def test_spanish_names(self):
        self.assertEqual(parse("lun,mie,vie 09:00").weekdays, (0, 2, 4))

    def test_intervals_in_hours(self):
        s = parse("every 3h")
        self.assertEqual(s.minutes, 180)
        self.assertTrue(is_due(s, WED, None))
        self.assertFalse(is_due(s, WED, WED - timedelta(hours=2)))

    def test_first_run_of_a_fixed_time_job_does_not_fire(self):
        self.assertFalse(is_due(parse("09:00"), WED, None))

    def test_next_daily(self):
        self.assertEqual(next_due(parse("09:00"), WED), datetime(2026, 11, 5, 9, 0))

    def test_invalid(self):
        for spec in ("mon,xyz 08:00", "25:00", "every", "monday 8"):
            with self.assertRaises(ScheduleError):
                parse(spec)


if __name__ == "__main__":
    unittest.main()
