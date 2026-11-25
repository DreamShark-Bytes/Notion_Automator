"""Tests for tools/describe_database.py pure functions (no network)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))

import describe_database as dd

_DB = {
    "title": [{"plain_text": "Task "}, {"plain_text": "Tracker"}],
    "properties": {
        "status": {"id": "%3AaB", "type": "status", "status": {"options": [{"name": "Done"}, {"name": "Not started"}]}},
        "Recurring Series": {
            "id": "xY9z",
            "type": "relation",
            "relation": {"database_id": "abc123", "type": "dual_property", "dual_property": {"synced_property_name": "Tasks"}},
        },
        "Due Date": {"id": "d1", "type": "date", "date": {}},
        "Total": {"id": "r1", "type": "rollup", "rollup": {"function": "sum", "rollup_property_name": "Hours", "relation_property_name": "Sessions"}},
        "Long": {"id": "f1", "type": "formula", "formula": {"expression": "x" * 200}},
    },
}


def test_rows_sorted_case_insensitive():
    names = [r["name"] for r in dd.describe_properties(_DB)]
    assert names == ["Due Date", "Long", "Recurring Series", "status", "Total"]


def test_ids_and_types_present():
    rows = {r["name"]: r for r in dd.describe_properties(_DB)}
    assert rows["status"]["id"] == "%3AaB"
    assert rows["Due Date"]["type"] == "date"
    assert rows["Due Date"]["details"] == ""


def test_status_options_listed():
    rows = {r["name"]: r for r in dd.describe_properties(_DB)}
    assert rows["status"]["details"] == "Done, Not started"


def test_relation_old_style_database_only():
    rows = {r["name"]: r for r in dd.describe_properties(_DB)}
    assert rows["Recurring Series"]["details"] == "-> database abc123 (reverse property: Tasks)"


def test_relation_shows_data_source_and_database():
    # Notion returns both IDs for relations on every API version (2025-09-03 upgrade guide, Step 2).
    db = {"properties": {"Series": {"id": "r", "type": "relation", "relation": {
        "database_id": "dbid", "data_source_id": "dsid", "type": "dual_property",
        "dual_property": {"synced_property_name": "Tasks", "synced_property_id": "x"}}}}}
    assert dd.describe_properties(db)[0]["details"] == "-> data source dsid (database dbid) (reverse property: Tasks)"


def test_relation_data_source_only_and_empty():
    only_ds = {"properties": {"S": {"id": "r", "type": "relation", "relation": {"data_source_id": "dsid", "type": "single_property", "single_property": {}}}}}
    assert dd.describe_properties(only_ds)[0]["details"] == "-> data source dsid"
    empty = {"properties": {"S": {"id": "r", "type": "relation", "relation": {}}}}
    assert dd.describe_properties(empty)[0]["details"] == ""


def test_header_line_uses_object_kind_and_own_id():
    ds = {"object": "data_source", "id": "dsid", "title": [{"plain_text": "Tasks"}]}
    assert dd.header_line(ds, 3) == "data_source: Tasks  id=dsid  (3 properties)"
    assert dd.header_line({"title": []}, 0) == "source: (untitled)  id=  (0 properties)"


def test_rollup_details():
    rows = {r["name"]: r for r in dd.describe_properties(_DB)}
    assert rows["Total"]["details"] == "sum of Hours via Sessions"


def test_formula_truncated():
    rows = {r["name"]: r for r in dd.describe_properties(_DB)}
    assert len(rows["Long"]["details"]) == 83 and rows["Long"]["details"].endswith("...")


def test_title_joined_and_untitled_fallback():
    assert dd.database_title(_DB) == "Task Tracker"
    assert dd.database_title({}) == "(untitled)"


def test_empty_database_formats_header_only():
    out = dd.format_table(dd.describe_properties({"properties": {}}))
    assert out.splitlines()[0].startswith("Property")
    assert len(out.splitlines()) == 2


def test_table_contains_every_id():
    out = dd.format_table(dd.describe_properties(_DB))
    for pid in ("%3AaB", "xY9z", "d1", "r1", "f1"):
        assert pid in out
