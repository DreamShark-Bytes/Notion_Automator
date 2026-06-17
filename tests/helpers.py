import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import recurring_tasks

# Real status groups from the Task Tracker database.
# To-do:        Not started
# In Progress:  In progress, On hold
# Complete:     Done, Cancelled, Handed off
COMPLETE_STATUSES = {"done", "cancelled", "handed off"}


def fake_status_group(client, page, field):
    """Test stub for _get_status_group using real Task Tracker status groups."""
    name = (recurring_tasks._get_status(page, field) or "").lower()
    return "Complete" if name in COMPLETE_STATUSES else "To-do"


def make_task(
    task_id,
    status,
    due_start=None,
    due_end=None,
    closed_date=None,
    def_id="test-def-id",
    db_id="test-tasks-db",
    name=None,
):
    """Build a minimal Notion task page dict for unit tests."""
    date_val = (
        {"start": due_start, "end": due_end}
        if due_start is not None
        else None
    )
    closed_val = {"start": closed_date, "end": None} if closed_date else None
    return {
        "id": task_id,
        "parent": {"database_id": db_id},
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": name or task_id}],
            },
            "Status": {
                "type": "status",
                "status": {"id": f"{status.lower()}-id", "name": status},
            },
            "Due Date": {"type": "date", "date": date_val},
            "Closed Date": {"type": "date", "date": closed_val},
            "Recurring Series": {
                "type": "relation",
                "relation": [{"id": def_id}],
            },
            "Ignore Grace Period (Recurring Task)": {
                "type": "checkbox",
                "checkbox": False,
            },
            "Reopen Count": {"type": "number", "number": None},
            "Occurrence # this Period (Recurring Task)": {
                "type": "number",
                "number": None,
            },
        },
    }


def make_definition(type_value, period=None, cadence_type=None, cadence_n=None):
    """Build a minimal Notion RTD page dict for unit tests."""
    def select_prop(val):
        return {"type": "select", "select": {"name": val} if val else None}

    return {
        "id": "test-def-id",
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "Test RTD"}],
            },
            "Type": select_prop(type_value),
            "Cadence Type": select_prop(cadence_type),
            "Period": select_prop(period),
            "Anchor Day": {"type": "number", "number": None},
            "Anchor Time": {"type": "rich_text", "rich_text": []},
            "N Cadence": {
                "type": "number",
                "number": cadence_n,
            },
        },
    }
