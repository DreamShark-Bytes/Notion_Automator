"""
Tests for auto_first_value and auto_update_count.

These cover the generic field tracking automations that replaced
auto_due_date_update_count. Tests use a mocked client and injected
_db_configs / _db_schema_cache to avoid real API calls.
"""
import pytest
from unittest.mock import MagicMock
import automations
from helpers import make_task


DB_ID = "test-db-id-1234"
DEF_ID = "test-def-id"

# A minimal schema covering the field types we support.
TASK_SCHEMA = {
    "Due Date":            "date",
    "First Due Date":      "date",
    "Due Date Update Count": "number",
    "Status":              "status",
    "First Status":        "rich_text",
    "Status Update Count": "number",
    "Priority":            "select",
    "First Priority":      "rich_text",
}


def make_page(status="Not started", due_start=None, due_end=None,
              closed_date=None, first_due=None, due_count=None,
              first_status=None, status_count=None,
              first_priority=None, priority=None):
    """Build a task page dict with the full set of tracking fields."""
    def date_prop(start, end=None):
        return {"type": "date", "date": {"start": start, "end": end} if start else None}

    def number_prop(n):
        return {"type": "number", "number": n}

    def text_prop(val):
        if val is None:
            return {"type": "rich_text", "rich_text": []}
        return {"type": "rich_text", "rich_text": [{"plain_text": val, "type": "text", "text": {"content": val}}]}

    def select_prop(val):
        return {"type": "select", "select": {"name": val} if val else None}

    def status_prop(val):
        return {"type": "status", "status": {"name": val, "id": f"{val.lower()}-id"} if val else None}

    return {
        "id": "page-id",
        "parent": {"database_id": DB_ID},
        "properties": {
            "Name":                    {"type": "title", "title": [{"plain_text": "Test Task"}]},
            "Status":                  status_prop(status),
            "Priority":                select_prop(priority),
            "Due Date":                date_prop(due_start, due_end),
            "Closed Date":             date_prop(closed_date),
            "First Due Date":          date_prop(first_due),
            "Due Date Update Count":   number_prop(due_count),
            "First Status":            text_prop(first_status),
            "Status Update Count":     number_prop(status_count),
            "First Priority":          text_prop(first_priority),
        },
    }


@pytest.fixture(autouse=True)
def inject_config(monkeypatch):
    monkeypatch.setattr(automations, "_db_configs", {
        DB_ID.replace("-", ""): {
            "first_value_fields":  ["Due Date", "Status"],
            "update_count_fields": ["Due Date", "Status"],
        }
    })
    monkeypatch.setattr(automations, "_db_schema_cache", {
        DB_ID.replace("-", ""): TASK_SCHEMA,
    })
    monkeypatch.setattr(automations, "_deprecation_warned", set())


def client_mock():
    c = MagicMock()
    c.get_database.return_value = {"properties": {k: {"type": v} for k, v in TASK_SCHEMA.items()}}
    return c


# ------------------------------------------------------------------ #
#  auto_first_value
# ------------------------------------------------------------------ #

class TestAutoFirstValue:

    def test_stamps_first_due_date_on_first_observation(self):
        page = make_page(due_start="2026-06-17T03:00:00+00:00", due_end="2026-06-18T02:59:00+00:00")
        result = automations.auto_first_value(client_mock(), page, None)
        assert "First Due Date" in result
        assert result["First Due Date"]["date"]["start"] == "2026-06-17T03:00:00+00:00"

    def test_preserves_date_range_end(self):
        page = make_page(due_start="2026-06-17T03:00:00+00:00", due_end="2026-06-18T02:59:00+00:00")
        result = automations.auto_first_value(client_mock(), page, None)
        assert result["First Due Date"]["date"]["end"] == "2026-06-18T02:59:00+00:00"

    def test_does_not_overwrite_existing_first_due_date(self):
        page = make_page(
            due_start="2026-06-17T03:00:00+00:00",
            first_due="2026-06-01T03:00:00+00:00",
        )
        result = automations.auto_first_value(client_mock(), page, None)
        assert "First Due Date" not in result

    def test_stamps_first_status_as_text(self):
        page = make_page(status="In progress")
        result = automations.auto_first_value(client_mock(), page, None)
        assert "First Status" in result
        assert result["First Status"]["rich_text"][0]["text"]["content"] == "In progress"

    def test_does_not_stamp_when_source_empty(self):
        page = make_page()  # no due date
        result = automations.auto_first_value(client_mock(), page, None)
        assert "First Due Date" not in result

    def test_skips_field_when_target_column_absent(self, monkeypatch):
        schema_without_first_status = {k: v for k, v in TASK_SCHEMA.items() if k != "First Status"}
        monkeypatch.setattr(automations, "_db_schema_cache", {
            DB_ID.replace("-", ""): schema_without_first_status,
        })
        page = make_page(status="In progress")
        result = automations.auto_first_value(client_mock(), page, None)
        assert "First Status" not in result

    def test_no_op_when_no_fields_configured(self, monkeypatch):
        monkeypatch.setattr(automations, "_db_configs", {
            DB_ID.replace("-", ""): {}
        })
        page = make_page(due_start="2026-06-17T03:00:00+00:00")
        result = automations.auto_first_value(client_mock(), page, None)
        assert result == {}

    def test_backward_compat_due_date_tracking(self, monkeypatch):
        monkeypatch.setattr(automations, "_db_configs", {
            DB_ID.replace("-", ""): {"due_date_tracking": True}
        })
        page = make_page(due_start="2026-06-17T03:00:00+00:00")
        result = automations.auto_first_value(client_mock(), page, None)
        assert "First Due Date" in result


# ------------------------------------------------------------------ #
#  auto_update_count
# ------------------------------------------------------------------ #

class TestAutoUpdateCount:

    def test_initializes_counter_to_zero_when_null(self):
        page = make_page(due_start="2026-06-17T03:00:00+00:00")  # due_count=None
        result = automations.auto_update_count(client_mock(), page, None)
        assert result.get("Due Date Update Count") == {"number": 0}

    def test_does_not_increment_on_init_pass(self):
        page = make_page(
            due_start="2026-06-17T03:00:00+00:00",
            due_count=0,
        )
        result = automations.auto_update_count(client_mock(), page, None)
        assert "Due Date Update Count" not in result

    def test_increments_when_date_portion_changes(self):
        prev = make_page(due_start="2026-06-16T03:00:00+00:00", due_count=2)
        page  = make_page(due_start="2026-06-17T03:00:00+00:00", due_count=2)
        result = automations.auto_update_count(client_mock(), page, prev)
        assert result.get("Due Date Update Count") == {"number": 3}

    def test_does_not_increment_on_time_only_change(self):
        prev = make_page(due_start="2026-06-17T09:00:00+00:00", due_count=1)
        page  = make_page(due_start="2026-06-17T14:00:00+00:00", due_count=1)
        result = automations.auto_update_count(client_mock(), page, prev)
        assert "Due Date Update Count" not in result

    def test_does_not_increment_when_unchanged(self):
        prev = make_page(due_start="2026-06-17T03:00:00+00:00", due_count=1)
        page  = make_page(due_start="2026-06-17T03:00:00+00:00", due_count=1)
        result = automations.auto_update_count(client_mock(), page, prev)
        assert "Due Date Update Count" not in result

    def test_does_not_increment_when_cleared(self):
        prev = make_page(due_start="2026-06-17T03:00:00+00:00", due_count=1)
        page  = make_page(due_count=1)  # due date cleared
        result = automations.auto_update_count(client_mock(), page, prev)
        assert "Due Date Update Count" not in result

    def test_increments_status_on_change(self):
        prev = make_page(status="Not started", status_count=0)
        page  = make_page(status="In progress", status_count=0)
        result = automations.auto_update_count(client_mock(), page, prev)
        assert result.get("Status Update Count") == {"number": 1}

    def test_does_not_increment_status_when_unchanged(self):
        prev = make_page(status="In progress", status_count=1)
        page  = make_page(status="In progress", status_count=1)
        result = automations.auto_update_count(client_mock(), page, prev)
        assert "Status Update Count" not in result

    def test_backward_compat_due_date_tracking(self, monkeypatch):
        monkeypatch.setattr(automations, "_db_configs", {
            DB_ID.replace("-", ""): {"due_date_tracking": True}
        })
        prev = make_page(due_start="2026-06-16T03:00:00+00:00", due_count=0)
        page  = make_page(due_start="2026-06-17T03:00:00+00:00", due_count=0)
        result = automations.auto_update_count(client_mock(), page, prev)
        assert result.get("Due Date Update Count") == {"number": 1}

    def test_deprecation_warning_fires_once(self, monkeypatch, caplog):
        import logging
        monkeypatch.setattr(automations, "_db_configs", {
            DB_ID.replace("-", ""): {"due_date_tracking": True}
        })
        page = make_page(due_start="2026-06-17T03:00:00+00:00", due_count=0)
        prev = make_page(due_start="2026-06-16T03:00:00+00:00", due_count=0)
        with caplog.at_level(logging.WARNING, logger="automations"):
            automations.auto_update_count(client_mock(), page, prev)
            automations.auto_update_count(client_mock(), page, prev)
        warnings = [r for r in caplog.records if "deprecated" in r.message]
        assert len(warnings) == 1
