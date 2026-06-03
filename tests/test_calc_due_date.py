"""
Tests for _calc_due_date after the date-range redesign.

Design rules (agreed):
  1. Period=Week or Month (or Year), no AnchorDay → full period range adjusted for day_start_hour
  2. Period=Day, no AnchorTime → full day range adjusted for day_start_hour
  3. Period=Day + AnchorTime → single datetime (that day at anchor time, no end)
  4. Week/Month, AnchorDay set, no AnchorTime → single-day range on that anchor day (day_start_hour adjusted)
  5. Any period, AnchorDay + AnchorTime set → single datetime on anchor day at anchor time (no end)
  6. Habit / Bad Habit / Unlimited / At most N → no due date
  7. After midnight before day_start_hour → due date is for the PREVIOUS logical day's period

Range format:
  start: period_start @ day_start_hour  (ISO datetime with timezone)
  end:   next_period_start @ day_start_hour - 1 minute

Helper assertions check that returned dates are correct without caring about
exact timezone offset strings (which vary by machine locale).
"""
import pytest
from datetime import datetime, timedelta
import recurring_tasks


def local_dt(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute).astimezone()


def parse_notion_date(d):
    """Parse a Notion date dict {'start': ..., 'end': ...} into (start_dt, end_dt|None)."""
    if d is None:
        return None, None
    start = datetime.fromisoformat(d["date"]["start"]) if d["date"]["start"] else None
    end_str = d["date"].get("end")
    end = datetime.fromisoformat(end_str) if end_str else None
    return start, end


# ------------------------------------------------------------------ #
#  Shortcuts for calling _calc_due_date with a fixed 'now'
# ------------------------------------------------------------------ #

def calc(cadence_type, period, anchor_day, anchor_time, use_next, task_type, now,
         cadence_n=None, def_id="test-rtd"):
    return recurring_tasks._calc_due_date(
        cadence_type=cadence_type,
        period=period,
        anchor_day=anchor_day,
        anchor_time=anchor_time,
        use_next_period=use_next,
        task_type=task_type,
        def_id=def_id,
        cadence_n=cadence_n,
        now=now,
    )


class TestCalcDueDateSkipped:
    """Cases that always return None."""

    def setup_method(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_habit_returns_none(self):
        now = local_dt(2026, 6, 3, 10, 0)
        assert calc("Once per period", "Day", None, None, False, "Habit", now) is None

    def test_bad_habit_returns_none(self):
        now = local_dt(2026, 6, 3, 10, 0)
        assert calc("At most N per period", "Day", None, None, False, "Bad Habit", now) is None

    def test_unlimited_returns_none(self):
        now = local_dt(2026, 6, 3, 10, 0)
        assert calc("Unlimited", "Day", None, None, False, "Responsibility", now) is None

    def test_no_period_returns_none(self):
        now = local_dt(2026, 6, 3, 10, 0)
        assert calc("Once per period", None, None, None, False, "Responsibility", now) is None


class TestCalcDueDateDay:
    """Period=Day tests."""

    def setup_method(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_day_no_anchor_normal_time_is_range(self):
        now = local_dt(2026, 6, 3, 10, 0)  # 10am June 3 → logical day June 3
        result = calc("Once per period", "Day", None, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start is not None and end is not None, "Expected a date range"
        assert start.date() == datetime(2026, 6, 3).date()
        assert start.hour == 3 and start.minute == 0  # day_start_hour
        assert end.date() == datetime(2026, 6, 4).date()
        assert end.hour == 2 and end.minute == 59

    def test_day_no_anchor_after_midnight_is_previous_period(self):
        # 2am June 3 — logically still June 2 (day_start_hour=3)
        now = local_dt(2026, 6, 3, 2, 0)
        result = calc("Once per period", "Day", None, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start is not None and end is not None
        # Should be June 2's range, NOT June 3
        assert start.date() == datetime(2026, 6, 2).date(), \
            f"Expected start on June 2, got {start.date()}"
        assert start.hour == 3 and start.minute == 0
        assert end.date() == datetime(2026, 6, 3).date()
        assert end.hour == 2 and end.minute == 59

    def test_day_exactly_at_boundary(self):
        now = local_dt(2026, 6, 3, 3, 0)  # exactly 3am → June 3
        result = calc("Once per period", "Day", None, None, False, "Responsibility", now)
        start, _ = parse_notion_date(result)
        assert start.date() == datetime(2026, 6, 3).date()

    def test_day_anchor_time_no_end(self):
        # AnchorTime=09:00 → point-in-time, no end
        now = local_dt(2026, 6, 3, 10, 0)
        result = calc("Once per period", "Day", None, "09:00", False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert end is None, "AnchorTime should produce a single datetime, not a range"
        assert start.hour == 9 and start.minute == 0
        assert start.date() == datetime(2026, 6, 3).date()

    def test_day_anchor_time_after_midnight_correct_date(self):
        # 2am June 3 (logical June 2) with AnchorTime=09:00 → June 2 @ 9am
        now = local_dt(2026, 6, 3, 2, 0)
        result = calc("Once per period", "Day", None, "09:00", False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert end is None
        assert start.date() == datetime(2026, 6, 2).date(), \
            f"Expected June 2, got {start.date()}"
        assert start.hour == 9

    def test_day_use_next_period(self):
        now = local_dt(2026, 6, 3, 10, 0)  # June 3 → next = June 4
        result = calc("Once per period", "Day", None, None, True, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start.date() == datetime(2026, 6, 4).date()
        assert end.date() == datetime(2026, 6, 5).date()

    def test_day_no_offset_day_start_hour_zero(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=0)
        now = local_dt(2026, 6, 3, 2, 0)  # 2am, but day_start_hour=0 → June 3
        result = calc("Once per period", "Day", None, None, False, "Responsibility", now)
        start, _ = parse_notion_date(result)
        assert start.date() == datetime(2026, 6, 3).date()


class TestCalcDueDateWeek:
    """Period=Week tests. week_start_day=0 (Monday)."""

    def setup_method(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_week_no_anchor_is_range(self):
        now = local_dt(2026, 6, 3, 10, 0)  # Wednesday June 3 → week of June 1 (Mon)
        result = calc("Once per period", "Week", None, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start is not None and end is not None
        assert start.date() == datetime(2026, 6, 1).date()  # Monday
        assert start.hour == 3 and start.minute == 0
        assert end.date() == datetime(2026, 6, 8).date()  # Monday of next week
        assert end.hour == 2 and end.minute == 59

    def test_week_after_midnight_monday_is_previous_week(self):
        # 2am Monday June 1 → still in previous week (May 25)
        now = local_dt(2026, 6, 1, 2, 0)
        result = calc("Once per period", "Week", None, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start.date() == datetime(2026, 5, 25).date(), \
            f"Expected week starting May 25, got {start.date()}"

    def test_week_anchor_day_no_anchor_time_is_single_day_range(self):
        # AnchorDay=5 (Friday), no AnchorTime → range spanning that Friday (day_start_hour adjusted)
        now = local_dt(2026, 6, 3, 10, 0)  # Wednesday June 3
        result = calc("Once per period", "Week", 5, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start is not None and end is not None
        # Friday of current week is June 5
        assert start.date() == datetime(2026, 6, 5).date()
        assert start.hour == 3 and start.minute == 0
        assert end.date() == datetime(2026, 6, 6).date()  # next day (Saturday)
        assert end.hour == 2 and end.minute == 59

    def test_week_anchor_day_and_anchor_time_is_point_in_time(self):
        # AnchorDay=5 + AnchorTime=09:00 → single datetime on that Friday, no end
        now = local_dt(2026, 6, 3, 10, 0)
        result = calc("Once per period", "Week", 5, "09:00", False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert end is None, "AnchorTime set → single datetime, no range"
        assert start.date() == datetime(2026, 6, 5).date()
        assert start.hour == 9 and start.minute == 0

    def test_week_use_next_period(self):
        now = local_dt(2026, 6, 3, 10, 0)  # week of June 1 → next = June 8
        result = calc("Once per period", "Week", None, None, True, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start.date() == datetime(2026, 6, 8).date()
        assert end.date() == datetime(2026, 6, 15).date()

    def test_week_exactly_n_n_greater_than_1_suppresses_anchor(self):
        # N=2 with AnchorDay set → anchor suppressed, full period range
        now = local_dt(2026, 6, 3, 10, 0)
        result = calc("Exactly N per period", "Week", 5, None, False, "Responsibility", now, cadence_n=2)
        start, end = parse_notion_date(result)
        assert start is not None and end is not None
        assert start.date() == datetime(2026, 6, 1).date()  # full week, not Friday


class TestCalcDueDateMonth:
    """Period=Month tests."""

    def setup_method(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_month_no_anchor_is_range(self):
        now = local_dt(2026, 6, 15, 10, 0)
        result = calc("Once per period", "Month", None, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start is not None and end is not None
        assert start.date() == datetime(2026, 6, 1).date()
        assert start.hour == 3 and start.minute == 0
        assert end.date() == datetime(2026, 7, 1).date()
        assert end.hour == 2 and end.minute == 59

    def test_month_first_day_before_boundary_is_previous_month(self):
        now = local_dt(2026, 6, 1, 2, 0)  # 2am June 1 → still May
        result = calc("Once per period", "Month", None, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start.date() == datetime(2026, 5, 1).date(), \
            f"Expected May, got {start.date()}"
        assert end.date() == datetime(2026, 6, 1).date()

    def test_month_anchor_day_no_anchor_time_is_single_day_range(self):
        now = local_dt(2026, 6, 15, 10, 0)
        result = calc("Once per period", "Month", 20, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start.date() == datetime(2026, 6, 20).date()
        assert start.hour == 3 and start.minute == 0
        assert end.date() == datetime(2026, 6, 21).date()
        assert end.hour == 2 and end.minute == 59

    def test_month_anchor_day_and_anchor_time(self):
        now = local_dt(2026, 6, 15, 10, 0)
        result = calc("Once per period", "Month", 20, "14:00", False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert end is None
        assert start.date() == datetime(2026, 6, 20).date()
        assert start.hour == 14 and start.minute == 0

    def test_month_december_wraps_to_january(self):
        now = local_dt(2026, 12, 15, 10, 0)
        result = calc("Once per period", "Month", None, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start.date() == datetime(2026, 12, 1).date()
        assert end.date() == datetime(2027, 1, 1).date()

    def test_month_use_next_period(self):
        now = local_dt(2026, 6, 15, 10, 0)
        result = calc("Once per period", "Month", None, None, True, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start.date() == datetime(2026, 7, 1).date()
        assert end.date() == datetime(2026, 8, 1).date()


class TestCalcDueDateYear:
    """Period=Year tests."""

    def setup_method(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_year_no_anchor_is_range(self):
        now = local_dt(2026, 6, 15, 10, 0)
        result = calc("Once per period", "Year", None, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start is not None and end is not None
        assert start.date() == datetime(2026, 1, 1).date()
        assert start.hour == 3 and start.minute == 0
        assert end.date() == datetime(2027, 1, 1).date()
        assert end.hour == 2 and end.minute == 59

    def test_year_first_day_before_boundary_is_previous_year(self):
        now = local_dt(2026, 1, 1, 2, 0)  # 2am Jan 1 → still 2025
        result = calc("Once per period", "Year", None, None, False, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start.date() == datetime(2025, 1, 1).date(), \
            f"Expected 2025, got {start.date()}"

    def test_year_use_next_period(self):
        now = local_dt(2026, 6, 15, 10, 0)
        result = calc("Once per period", "Year", None, None, True, "Responsibility", now)
        start, end = parse_notion_date(result)
        assert start.date() == datetime(2027, 1, 1).date()
        assert end.date() == datetime(2028, 1, 1).date()
