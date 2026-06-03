"""
Tests for _period_key.

The day_start_hour offset shifts the period boundary so times between
midnight and day_start_hour belong to the previous logical period.

All tests call init() to set module state before running.
"""
import pytest
from datetime import datetime, timezone
import recurring_tasks


def local_dt(year, month, day, hour=0, minute=0):
    """Return a local-timezone-aware datetime for the given date/time."""
    return datetime(year, month, day, hour, minute).astimezone()


class TestPeriodKeyDay:
    def setup_method(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_normal_daytime(self):
        dt = local_dt(2026, 6, 3, 10, 0)  # 10am June 3
        assert recurring_tasks._period_key("Day", dt) == "2026-06-03"

    def test_exactly_at_boundary(self):
        dt = local_dt(2026, 6, 3, 3, 0)  # 3am June 3 — exactly at day_start_hour
        assert recurring_tasks._period_key("Day", dt) == "2026-06-03"

    def test_after_midnight_before_boundary(self):
        # 2am June 3 is still logically June 2 when day_start_hour=3
        dt = local_dt(2026, 6, 3, 2, 0)
        assert recurring_tasks._period_key("Day", dt) == "2026-06-02"

    def test_midnight_is_previous_day(self):
        # Midnight June 3 is 0am — below day_start_hour=3 → still June 2
        dt = local_dt(2026, 6, 3, 0, 0)
        assert recurring_tasks._period_key("Day", dt) == "2026-06-02"

    def test_just_before_boundary(self):
        dt = local_dt(2026, 6, 3, 2, 59)  # 2:59am June 3
        assert recurring_tasks._period_key("Day", dt) == "2026-06-02"

    def test_just_after_boundary(self):
        dt = local_dt(2026, 6, 3, 3, 1)  # 3:01am June 3
        assert recurring_tasks._period_key("Day", dt) == "2026-06-03"

    def test_no_offset_when_day_start_hour_zero(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=0)
        dt = local_dt(2026, 6, 3, 0, 0)  # Midnight is start of June 3
        assert recurring_tasks._period_key("Day", dt) == "2026-06-03"

    def test_month_boundary_after_midnight(self):
        # 2am June 1 (day_start_hour=3) → still May 31
        dt = local_dt(2026, 6, 1, 2, 0)
        assert recurring_tasks._period_key("Day", dt) == "2026-05-31"

    def test_year_boundary_after_midnight(self):
        # 1am Jan 1 (day_start_hour=3) → still Dec 31 of previous year
        dt = local_dt(2026, 1, 1, 1, 0)
        assert recurring_tasks._period_key("Day", dt) == "2025-12-31"


class TestPeriodKeyWeek:
    def setup_method(self):
        # week_start_day=0 (Monday)
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_midweek(self):
        dt = local_dt(2026, 6, 3, 10, 0)  # Wednesday June 3
        assert recurring_tasks._period_key("Week", dt) == "W-2026-06-01"  # Week started Mon June 1

    def test_monday_after_boundary(self):
        # 3am Monday June 1 — just entered the new week
        dt = local_dt(2026, 6, 1, 3, 0)
        assert recurring_tasks._period_key("Week", dt) == "W-2026-06-01"

    def test_monday_before_boundary(self):
        # 2am Monday June 1 — still in previous week (Mon May 25)
        dt = local_dt(2026, 6, 1, 2, 0)
        assert recurring_tasks._period_key("Week", dt) == "W-2026-05-25"

    def test_sunday_normal_time(self):
        # Sunday June 7 is still part of the week starting Mon June 1
        dt = local_dt(2026, 6, 7, 12, 0)
        assert recurring_tasks._period_key("Week", dt) == "W-2026-06-01"

    def test_sunday_start(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=6, day_start_hour=3)
        dt = local_dt(2026, 6, 7, 10, 0)  # Sunday June 7
        assert recurring_tasks._period_key("Week", dt) == "W-2026-06-07"


class TestPeriodKeyMonth:
    def setup_method(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_midmonth(self):
        dt = local_dt(2026, 6, 15, 10, 0)
        assert recurring_tasks._period_key("Month", dt) == "2026-06"

    def test_first_day_after_boundary(self):
        dt = local_dt(2026, 6, 1, 3, 0)
        assert recurring_tasks._period_key("Month", dt) == "2026-06"

    def test_first_day_before_boundary(self):
        # 2am June 1 → still May
        dt = local_dt(2026, 6, 1, 2, 0)
        assert recurring_tasks._period_key("Month", dt) == "2026-05"


class TestPeriodKeyYear:
    def setup_method(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_midyear(self):
        dt = local_dt(2026, 6, 15, 10, 0)
        assert recurring_tasks._period_key("Year", dt) == "2026"

    def test_jan_first_after_boundary(self):
        dt = local_dt(2026, 1, 1, 3, 0)
        assert recurring_tasks._period_key("Year", dt) == "2026"

    def test_jan_first_before_boundary(self):
        dt = local_dt(2026, 1, 1, 2, 0)
        assert recurring_tasks._period_key("Year", dt) == "2025"
