"""
Tests for _create_next_task period-advancement logic.

These test the completion trigger path — specifically that:
  1. Only actual completions (Done, not open, not cancelled) count toward
     advancing the series to the next period.
  2. An open task already covering the current period blocks advancement
     (the Physical Therapy bug: governance-cancelled old task should not
     trigger next-period creation when the current period is already covered).

Uses _day_start_hour=0 (midnight) so period boundaries are calendar dates —
simpler to reason about and date-independent.
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
import recurring_tasks
from helpers import make_task, make_definition, fake_status_group


DONE_STATUS = "Done"
CANCELLED_STATUS = "Cancelled"
OPEN_STATUS = "Not started"


@pytest.fixture(autouse=True)
def module_state(monkeypatch):
    monkeypatch.setattr(recurring_tasks, "_tasks_db_id", "test-tasks-db")
    monkeypatch.setattr(recurring_tasks, "_day_start_hour", 0)
    monkeypatch.setattr(recurring_tasks, "_get_status_group", fake_status_group)


def today_str():
    return datetime.now().astimezone().date().isoformat()

def yesterday_str():
    return (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()

def tomorrow_str():
    return (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()


# ------------------------------------------------------------------ #
#  Physical Therapy bug regression
# ------------------------------------------------------------------ #

def test_cancelled_trigger_with_open_current_period_task_does_not_advance():
    """Governance cancels an old overdue task; an open task already covers today.
    auto_recurring_tasks should NOT create a next-period task."""
    cancelled = make_task(
        task_id="old-task",
        status=CANCELLED_STATUS,
        due_start=f"{yesterday_str()}T00:00:00+00:00",
        due_end=f"{yesterday_str()}T23:59:00+00:00",
        closed_date=f"{yesterday_str()}T23:59:00+00:00",
    )
    open_today = make_task(
        task_id="current-task",
        status=OPEN_STATUS,
        due_start=f"{today_str()}T00:00:00+00:00",
        due_end=f"{today_str()}T23:59:00+00:00",
        closed_date=None,
    )

    client = MagicMock()
    client.query_database.return_value = [cancelled, open_today]

    recurring_tasks._create_next_task(
        client, cancelled, make_definition("Responsibility", period="Day",
                                           cadence_type="Once per period")
    )
    client.create_page.assert_not_called()


def test_cancelled_trigger_with_no_current_period_task_creates_for_today():
    """Governance cancels an old overdue task; nothing covers today.
    A replacement task should be created for the current period (not next)."""
    cancelled = make_task(
        task_id="old-task",
        status=CANCELLED_STATUS,
        due_start=f"{yesterday_str()}T00:00:00+00:00",
        due_end=f"{yesterday_str()}T23:59:00+00:00",
        closed_date=f"{yesterday_str()}T23:59:00+00:00",
    )

    client = MagicMock()
    client.query_database.return_value = [cancelled]
    client.create_page.return_value = {"id": "new-task-id", "properties": {}}

    recurring_tasks._create_next_task(
        client, cancelled, make_definition("Responsibility", period="Day",
                                           cadence_type="Once per period")
    )
    client.create_page.assert_called_once()
    # New task should target today, not tomorrow
    created_props = client.create_page.call_args[1].get(
        "properties", client.create_page.call_args[0][1] if client.create_page.call_args[0] else {}
    )
    due = (created_props or {}).get("Due Date", {}).get("date", {})
    assert today_str() in (due.get("start") or ""), \
        f"Expected today ({today_str()}) in Due Date start, got: {due}"


# ------------------------------------------------------------------ #
#  Normal completion advances to next period
# ------------------------------------------------------------------ #

def test_completion_advances_to_next_period():
    """User completes today's task. New task should be created for tomorrow."""
    done_task = make_task(
        task_id="done-task",
        status=DONE_STATUS,
        due_start=f"{today_str()}T00:00:00+00:00",
        due_end=f"{today_str()}T23:59:00+00:00",
        closed_date=f"{today_str()}T12:00:00+00:00",
    )

    client = MagicMock()
    client.query_database.return_value = [done_task]
    client.create_page.return_value = {"id": "new-task-id", "properties": {}}

    recurring_tasks._create_next_task(
        client, done_task, make_definition("Responsibility", period="Day",
                                           cadence_type="Once per period")
    )
    client.create_page.assert_called_once()
    created_props = client.create_page.call_args[1].get(
        "properties", client.create_page.call_args[0][1] if client.create_page.call_args[0] else {}
    )
    due = (created_props or {}).get("Due Date", {}).get("date", {})
    assert tomorrow_str() in (due.get("start") or ""), \
        f"Expected tomorrow ({tomorrow_str()}) in Due Date start, got: {due}"


def test_open_task_does_not_count_as_completion():
    """An open task in the current period should not push the series to next period."""
    open_today = make_task(
        task_id="open-task",
        status=OPEN_STATUS,
        due_start=f"{today_str()}T00:00:00+00:00",
        due_end=f"{today_str()}T23:59:00+00:00",
        closed_date=None,
    )
    # Trigger: user cancels a different old task (e.g. a duplicate)
    cancelled_old = make_task(
        task_id="old-cancelled",
        status=CANCELLED_STATUS,
        due_start=f"{yesterday_str()}T00:00:00+00:00",
        due_end=f"{yesterday_str()}T23:59:00+00:00",
        closed_date=f"{yesterday_str()}T23:59:00+00:00",
    )

    client = MagicMock()
    client.query_database.return_value = [cancelled_old, open_today]

    recurring_tasks._create_next_task(
        client, cancelled_old, make_definition("Habit", period="Day",
                                               cadence_type="Once per period")
    )
    # Should not advance: open task covers today, cancelled old task is not a completion
    client.create_page.assert_not_called()
