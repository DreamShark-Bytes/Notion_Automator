"""
Tests for _is_overdue_by.

With the Due Date range fix, grace period comparisons work against datetime
end-of-period timestamps (e.g., 2:59am) rather than calendar midnight.
The current .days comparison returns 0 for a 1-minute difference, which would
prevent auto-cancel from firing at the governance run immediately after a
period ends. The fix uses timedelta comparison for sub-day precision.
"""
import pytest
from datetime import datetime, timedelta
import recurring_tasks


def local_dt(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute).astimezone()


class TestIsOverdueBy:
    def setup_method(self):
        recurring_tasks.init("fake-def-id", "fake-tasks-id", week_start_day=0, day_start_hour=3)

    def test_past_due_grace_zero(self):
        due = local_dt(2026, 6, 2, 23, 59)  # end of June 2
        now = local_dt(2026, 6, 3, 3, 0)   # governance at 3am June 3
        assert recurring_tasks._is_overdue_by(due, grace_days=0, now=now) is True

    def test_one_minute_overdue_grace_zero(self):
        # Period ended at 2:59am; governance runs at 3:00am — exactly 1 minute over.
        # .days would return 0 and fail; timedelta comparison must catch this.
        due = local_dt(2026, 6, 3, 2, 59)
        now = local_dt(2026, 6, 3, 3, 0)
        assert recurring_tasks._is_overdue_by(due, grace_days=0, now=now) is True

    def test_not_yet_overdue_grace_zero(self):
        due = local_dt(2026, 6, 3, 3, 0)  # exactly now
        now = local_dt(2026, 6, 3, 3, 0)
        assert recurring_tasks._is_overdue_by(due, grace_days=0, now=now) is False

    def test_future_due(self):
        due = local_dt(2026, 6, 4, 2, 59)
        now = local_dt(2026, 6, 3, 3, 0)
        assert recurring_tasks._is_overdue_by(due, grace_days=0, now=now) is False

    def test_grace_one_day_not_yet(self):
        due = local_dt(2026, 6, 3, 2, 59)   # period end
        now = local_dt(2026, 6, 4, 2, 59)   # exactly 24h later
        assert recurring_tasks._is_overdue_by(due, grace_days=1, now=now) is False

    def test_grace_one_day_exceeded(self):
        due = local_dt(2026, 6, 3, 2, 59)
        now = local_dt(2026, 6, 4, 3, 0)   # 24h 1min later
        assert recurring_tasks._is_overdue_by(due, grace_days=1, now=now) is True

    def test_grace_zero_next_day_midnight(self):
        # Classic case: task due midnight June 2; governance runs 3am June 3.
        due = local_dt(2026, 6, 2, 0, 0)
        now = local_dt(2026, 6, 3, 3, 0)
        assert recurring_tasks._is_overdue_by(due, grace_days=0, now=now) is True

    def test_date_only_due_grace_zero(self):
        # Previously the only case — date midnight vs next-day governance.
        due = local_dt(2026, 6, 2)          # midnight June 2
        now = local_dt(2026, 6, 3, 3, 0)   # next morning
        assert recurring_tasks._is_overdue_by(due, grace_days=0, now=now) is True
