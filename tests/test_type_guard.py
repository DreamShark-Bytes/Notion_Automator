"""
Tests for the Type field guard in _create_next_task.

The guard fires before any API call, so no NotionClient mock setup is needed —
a MagicMock client is sufficient to verify no calls escape.

Covered cases:
  - Empty Type (field not set)
  - Explicit "None" option
  - Unrecognized / misspelled values
  - Valid types pass through the guard (query_database is attempted)
"""
import pytest
from unittest.mock import MagicMock
import recurring_tasks


def make_definition(type_value):
    """Minimal Notion RTD dict with just the fields the Type guard reads."""
    select_prop = {"name": type_value} if type_value else None
    return {
        "id": "test-def-id",
        "properties": {
            "Name":        {"type": "title",      "title": [{"plain_text": "Test RTD"}]},
            "Type":        {"type": "select",     "select": select_prop},
            "Cadence Type":{"type": "select",     "select": None},
            "Period":      {"type": "select",     "select": None},
            "Anchor Day":  {"type": "number",     "number": None},
            "Anchor Time": {"type": "rich_text",  "rich_text": []},
            "N Cadence":   {"type": "number",     "number": None},
        },
    }


@pytest.mark.parametrize("type_value", [
    None,         # empty field — not configured yet
    "None",       # explicit "None" select option
    "habit",      # wrong case
    "HABIT",      # wrong case
    "garbage",    # unrecognized value
])
def test_skips_and_makes_no_api_calls(type_value):
    client = MagicMock()
    recurring_tasks._create_next_task(client, None, make_definition(type_value))
    client.query_database.assert_not_called()
    client.create_page.assert_not_called()
    client.update_page_properties.assert_not_called()


@pytest.mark.parametrize("type_value", ["Habit", "Responsibility", "Bad Habit"])
def test_valid_type_passes_guard(type_value, monkeypatch):
    """Valid types proceed past the guard and attempt API calls."""
    monkeypatch.setattr(recurring_tasks, "_tasks_db_id", "test-db-id")
    client = MagicMock()
    client.query_database.return_value = []
    recurring_tasks._create_next_task(client, None, make_definition(type_value))
    client.query_database.assert_called()
