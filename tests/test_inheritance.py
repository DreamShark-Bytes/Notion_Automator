"""Tests for Slice 2 recurring task inheritance and per-pair non_completion_statuses."""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import recurring_tasks


def _make_pair(
    inherited_fields=None,
    closed_date_field="Closed Date",
    reopen_count_field="Reopen Count",
    non_completion_statuses=None,
):
    """Build a minimal pair dict for testing _copy_task_fields and _is_non_completion."""
    norm = None
    if non_completion_statuses is not None:
        norm = frozenset(re.sub(r'[^a-z]', '', s.lower()) for s in non_completion_statuses)
    return {
        "definitions_db_id":       "b" * 32,
        "tasks_db_id":             "a" * 32,
        "closed_date_field":       closed_date_field,
        "reopen_count_field":      reopen_count_field,
        "non_completion_statuses": norm,
        "inherited_fields":        inherited_fields if inherited_fields is not None else [],
        "_task_schema":            None,
    }


def _make_task(**props):
    """Build a minimal task page dict with the given property types."""
    properties = {}
    for name, value in props.items():
        if isinstance(value, str):
            properties[name] = {"type": "rich_text", "rich_text": [{"plain_text": value}]}
        elif isinstance(value, (int, float)):
            properties[name] = {"type": "number", "number": value}
    return {"properties": properties}


def test_copy_task_fields_empty_inherited_inherits_nothing():
    task = _make_task(Priority="High", Effort="Low")
    pair = _make_pair(inherited_fields=[])
    result = recurring_tasks._copy_task_fields(task, pair)
    assert result == {}


def test_copy_task_fields_whitelist_copies_listed_only():
    task = _make_task(Priority="High", Effort="Low", Area="Work")
    pair = _make_pair(inherited_fields=["Priority"])
    result = recurring_tasks._copy_task_fields(task, pair)
    assert "Priority" in result
    assert "Effort" not in result
    assert "Area" not in result


def test_copy_task_fields_whitelist_multiple():
    task = _make_task(Priority="High", Effort="Low", Area="Work")
    pair = _make_pair(inherited_fields=["Priority", "Area"])
    result = recurring_tasks._copy_task_fields(task, pair)
    assert "Priority" in result
    assert "Area" in result
    assert "Effort" not in result


def test_copy_task_fields_excludes_fields_not_inherited():
    task = _make_task(Name="Old Task", Status="Done", Priority="High")
    pair = _make_pair(inherited_fields=["Name", "Status", "Priority"])
    result = recurring_tasks._copy_task_fields(task, pair)
    # Name and Status are in FIELDS_NOT_INHERITED — must always be excluded
    assert "Name" not in result
    assert "Status" not in result
    # Priority is not in exclusions — should be copied
    assert "Priority" in result


def test_copy_task_fields_excludes_dynamic_closed_date():
    task = _make_task(**{"My Closed Date": "2026-01-01", "Priority": "High"})
    pair = _make_pair(
        inherited_fields=["My Closed Date", "Priority"],
        closed_date_field="My Closed Date",
    )
    result = recurring_tasks._copy_task_fields(task, pair)
    assert "My Closed Date" not in result
    assert "Priority" in result


def test_copy_task_fields_excludes_readonly_types():
    task = {
        "properties": {
            "Priority": {"type": "rich_text", "rich_text": [{"plain_text": "High"}]},
            "Formula Field": {"type": "formula", "formula": {"string": "computed"}},
        }
    }
    pair = _make_pair(inherited_fields=["Priority", "Formula Field"])
    result = recurring_tasks._copy_task_fields(task, pair)
    assert "Priority" in result
    assert "Formula Field" not in result


# ------------------------------------------------------------------ #
#  _is_non_completion with per-pair statuses
# ------------------------------------------------------------------ #

def test_is_non_completion_uses_pair_statuses():
    pair = _make_pair(non_completion_statuses=["Purchased", "Skipped"])
    assert recurring_tasks._is_non_completion("Purchased", pair) is True
    assert recurring_tasks._is_non_completion("Skipped", pair) is True
    assert recurring_tasks._is_non_completion("Done", pair) is False


def test_is_non_completion_pair_overrides_builtin():
    # Pair has no "cancelled" in its list — but "cancelled" is in built-in defaults.
    # When pair has a non-None non_completion_statuses, it should override the built-in.
    pair = _make_pair(non_completion_statuses=["CustomSkip"])
    assert recurring_tasks._is_non_completion("Cancelled", pair) is False
    assert recurring_tasks._is_non_completion("CustomSkip", pair) is True


def test_is_non_completion_empty_pair_statuses_uses_builtin():
    # Empty list → norm_statuses = None (set in configure_pairs logic)
    # Here we test the _make_pair behavior: None → falls back to built-in
    pair = _make_pair(non_completion_statuses=None)
    assert pair["non_completion_statuses"] is None
    assert recurring_tasks._is_non_completion("Cancelled", pair) is True


def test_is_non_completion_no_pair_uses_builtin():
    assert recurring_tasks._is_non_completion("Cancelled", None) is True
    assert recurring_tasks._is_non_completion("Done", None) is False


def test_is_non_completion_case_insensitive():
    pair = _make_pair(non_completion_statuses=["Purchased"])
    assert recurring_tasks._is_non_completion("PURCHASED", pair) is True
    assert recurring_tasks._is_non_completion("purchased", pair) is True
