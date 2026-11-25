"""
Tests for running on the newer Notion API versions (data sources, in_trash).

Sources: Notion's upgrade guides
  https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03
  https://developers.notion.com/guides/get-started/upgrade-guide-2026-03-11
and the installed Notion_API client (v1.4.0+). No network: the real NotionClient is used
with its HTTP methods replaced, and fake clients stand in for it elsewhere.

What these protect:
  - the project default version is a data-source version, and notion_version in config.toml is validated;
  - every place that needs a property schema asks get_schema() (works on every version) and never
    get_database() (no property schema on 2025-09-03+);
  - a page parent keeps its database_id on both API versions, so config matching still works;
  - the client behaviors this project relies on (routing, parents, in_trash) did not change in a pin bump.
"""
import logging
from unittest.mock import MagicMock

import pytest
import requests

import notion_api
from notion_api import NotionClient

import automations
import hub
import recurring_tasks

OLD = "2022-06-28"
MID = "2025-09-03"
NEW = "2026-03-11"


# ------------------------------------------------------------------ #
#  resolve_notion_version
# ------------------------------------------------------------------ #

def test_default_is_a_data_source_and_in_trash_version():
    assert hub.DEFAULT_NOTION_VERSION == NEW
    assert hub.DEFAULT_NOTION_VERSION >= notion_api.DATA_SOURCE_VERSION
    assert hub.DEFAULT_NOTION_VERSION >= notion_api.TRASH_VERSION


def test_missing_value_uses_default():
    assert hub.resolve_notion_version(None) == hub.DEFAULT_NOTION_VERSION


@pytest.mark.parametrize("value", [OLD, MID, NEW, "2030-01-31"])
def test_valid_dates_are_kept(value):
    assert hub.resolve_notion_version(value) == value


def test_whitespace_is_stripped():
    assert hub.resolve_notion_version("  2025-09-03 ") == MID


@pytest.mark.parametrize("value", ["latest", "2026-3-11", "20260311", "2026-03-11T00:00", "", 20260311, "v2"])
def test_invalid_values_fall_back_with_warning(value, caplog):
    with caplog.at_level(logging.WARNING, logger="hub"):
        assert hub.resolve_notion_version(value) == hub.DEFAULT_NOTION_VERSION
    assert any("notion_version" in r.getMessage() for r in caplog.records)


def test_valid_value_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="hub"):
        hub.resolve_notion_version(NEW)
    assert not caplog.records


# ------------------------------------------------------------------ #
#  Installed client behavior this project relies on
# ------------------------------------------------------------------ #

def _client(version: str, get=None, post=None, patch=None) -> NotionClient:
    c = NotionClient("test-token", notion_version=version)
    calls = {"get": [], "post": [], "patch": []}
    c._get = lambda path: (calls["get"].append(path), (get or (lambda p: {}))(path))[1]
    c._post = lambda path, payload: (calls["post"].append((path, payload)), (post or (lambda p, b: {}))(path, payload))[1]
    c._patch = lambda path, payload: (calls["patch"].append((path, payload)), (patch or (lambda p, b: {}))(path, payload))[1]
    c.calls = calls
    return c


def test_client_library_default_is_still_old_so_we_must_pass_the_version():
    assert notion_api.NOTION_VERSION == OLD
    assert NotionClient("t").notion_version == OLD


def test_client_sends_the_requested_version_header():
    assert NotionClient("t", notion_version=NEW).headers["Notion-Version"] == NEW


@pytest.mark.parametrize("version,expected", [(OLD, False), (MID, True), (NEW, True)])
def test_uses_data_sources_flag(version, expected):
    assert NotionClient("t", notion_version=version).uses_data_sources is expected


def test_get_schema_old_version_reads_the_database():
    c = _client(OLD, get=lambda p: {"properties": {"A": {"id": "1", "type": "date"}}})
    assert c.get_schema("abc")["properties"]["A"]["id"] == "1"
    assert c.calls["get"] == ["/databases/abc"]


def test_get_schema_new_version_reads_the_data_source():
    c = _client(NEW, get=lambda p: {"object": "data_source", "id": "abc", "properties": {"A": {"id": "1"}}})
    assert c.get_schema("abc")["properties"]["A"]["id"] == "1"
    assert set(c.calls["get"]) == {"/data_sources/abc"}


def test_database_id_is_resolved_to_its_single_data_source():
    def get(path):
        if path == "/data_sources/db1":
            err = requests.HTTPError(response=_response(404))
            raise err
        if path == "/databases/db1":
            return {"data_sources": [{"id": "ds1", "name": "Tasks"}]}
        if path == "/data_sources/ds1":
            return {"id": "ds1", "properties": {}}
        raise AssertionError(path)
    c = _client(NEW, get=get)
    assert c.resolve_data_source_id("db1") == "ds1"
    assert c.get_schema("db1")["id"] == "ds1"


def test_database_with_several_data_sources_needs_a_data_source_id():
    def get(path):
        if path == "/data_sources/db1":
            raise requests.HTTPError(response=_response(404))
        return {"data_sources": [{"id": "a", "name": "One"}, {"id": "b", "name": "Two"}]}
    c = _client(NEW, get=get)
    with pytest.raises(ValueError, match="2 data sources"):
        c.resolve_data_source_id("db1")


def test_query_database_new_version_posts_to_the_data_source():
    c = _client(
        NEW,
        get=lambda p: {"id": "ds1"},
        post=lambda p, b: {"results": [{"id": "page1"}], "has_more": False},
    )
    assert c.query_database("ds1", {"filter": 1}) == [{"id": "page1"}]
    path, payload = c.calls["post"][0]
    assert path == "/data_sources/ds1/query"
    assert payload["filter"] == {"filter": 1}


def test_query_database_old_version_posts_to_the_database():
    c = _client(OLD, post=lambda p, b: {"results": [], "has_more": False})
    c.query_database("db1")
    assert c.calls["post"][0][0] == "/databases/db1/query"


def test_create_page_parent_by_version():
    new = _client(NEW, get=lambda p: {"id": "ds1"}, post=lambda p, b: {})
    new.create_page("ds1", {"Name": {}})
    assert new.calls["post"][0][1]["parent"] == {"type": "data_source_id", "data_source_id": "ds1"}
    old = _client(OLD, post=lambda p, b: {})
    old.create_page("db1", {"Name": {}})
    assert old.calls["post"][0][1]["parent"] == {"database_id": "db1"}


@pytest.mark.parametrize("version,key", [(OLD, "archived"), (MID, "archived"), (NEW, "in_trash")])
def test_archive_page_uses_the_right_field(version, key):
    c = _client(version, patch=lambda p, b: {})
    c.archive_page("page1")
    assert c.calls["patch"][0] == ("/pages/page1", {key: True})


def _response(status: int) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    return r


# ------------------------------------------------------------------ #
#  Page parents: database_id is present on both shapes
# ------------------------------------------------------------------ #

_DB = "a" * 32
_OLD_PARENT = {"type": "database_id", "database_id": _DB}
_NEW_PARENT = {"type": "data_source_id", "data_source_id": "b" * 32, "database_id": _DB}


@pytest.mark.parametrize("parent", [_OLD_PARENT, _NEW_PARENT])
def test_automation_flags_found_from_either_parent_shape(parent):
    automations.register_db(_DB, {"closed_date_field": "Closed Date"})
    try:
        page = {"parent": parent}
        assert automations._flags(page) == {"closed_date_field": "Closed Date"}
        assert automations._db_id_of(page) == _DB
    finally:
        automations._db_configs.pop(_DB, None)


# ------------------------------------------------------------------ #
#  Schema reads use get_schema(), never get_database()
# ------------------------------------------------------------------ #

def _fake_client(schema_by_id: dict | None = None, error: Exception | None = None) -> MagicMock:
    c = MagicMock()
    c.get_database.side_effect = AssertionError("get_database has no property schema on 2025-09-03+; use get_schema")
    if error is not None:
        c.get_schema.side_effect = error
    else:
        c.get_schema.side_effect = lambda any_id: schema_by_id[any_id]
    return c


def test_automations_schema_uses_get_schema_and_caches():
    automations._db_schema_cache.clear()
    c = _fake_client({"db1": {"properties": {"Due Date": {"type": "date"}, "Status": {"type": "status"}}}})
    try:
        assert automations._get_db_schema(c, "db1") == {"Due Date": "date", "Status": "status"}
        automations._get_db_schema(c, "db1")
        assert c.get_schema.call_count == 1
        c.get_database.assert_not_called()
    finally:
        automations._db_schema_cache.clear()


def test_automations_schema_failure_gives_empty_schema(caplog):
    automations._db_schema_cache.clear()
    c = _fake_client(error=RuntimeError("boom"))
    try:
        with caplog.at_level(logging.WARNING):
            assert automations._get_db_schema(c, "db1") == {}
        assert any("Could not load schema" in r.getMessage() for r in caplog.records)
    finally:
        automations._db_schema_cache.clear()


def test_recurring_task_schema_uses_get_schema():
    c = _fake_client({"tasks": {"properties": {"Due Date": {}, "Priority": {}}}})
    pair = {"tasks_db_id": "tasks"}
    recurring_tasks._load_task_db_schema(c, pair)
    assert pair["_task_schema"] == {"Due Date", "Priority"}
    c.get_database.assert_not_called()
    recurring_tasks._load_task_db_schema(c, pair)  # already loaded: no second call
    assert c.get_schema.call_count == 1


def test_recurring_task_schema_failure_gives_empty_set(caplog):
    c = _fake_client(error=RuntimeError("boom"))
    pair = {"tasks_db_id": "tasks"}
    with caplog.at_level(logging.WARNING):
        recurring_tasks._load_task_db_schema(c, pair)
    assert pair["_task_schema"] == set()


def test_status_group_lookup_uses_get_schema():
    schema = {"properties": {"Status": {"status": {"groups": [
        {"name": "To-do", "option_ids": ["o1"]},
        {"name": "Complete", "option_ids": ["o2", "o3"]},
    ]}}}}
    c = _fake_client({_DB: schema})
    page = {"parent": _NEW_PARENT, "properties": {"Status": {"status": {"id": "o3", "name": "Done"}}}}
    recurring_tasks._status_group_cache.clear()
    try:
        assert recurring_tasks._get_status_group(c, page, "Status") == "Complete"
        assert recurring_tasks._get_status_group(c, {**page, "properties": {"Status": {"status": {"id": "o1"}}}}, "Status") == "To-do"
        assert c.get_schema.call_count == 1  # cached per database
        c.get_database.assert_not_called()
    finally:
        recurring_tasks._status_group_cache.clear()


def test_status_group_unknown_option_is_none():
    c = _fake_client({_DB: {"properties": {"Status": {"status": {"groups": []}}}}})
    page = {"parent": _OLD_PARENT, "properties": {"Status": {"status": {"id": "zzz"}}}}
    recurring_tasks._status_group_cache.clear()
    try:
        assert recurring_tasks._get_status_group(c, page, "Status") is None
    finally:
        recurring_tasks._status_group_cache.clear()


# ---- hub.validate_hub_schema ----

def _hub_schema(required: dict) -> dict:
    props = {name: {"type": t} for name, t in required.items()}
    props["Status"] = {"type": "status", "status": {"options": [{"name": "Active"}, {"name": "Inactive"}]}}
    return {"properties": props}


@pytest.fixture
def hub_ids(monkeypatch):
    monkeypatch.setattr(hub, "_available", True)
    monkeypatch.setattr(hub, "_config_ds_id", "config")
    monkeypatch.setattr(hub, "_status_ds_id", "status")


def _hub_client(schemas: dict) -> MagicMock:
    c = _fake_client(schemas)
    c.query_database.return_value = [{"id": "row1"}]
    return c


def _good_schemas() -> dict:
    return {
        "config": _hub_schema(hub._REQUIRED_CONFIG_PROPS),
        "status": _hub_schema(hub._REQUIRED_STATUS_PROPS),
    }


def test_hub_schema_check_passes_on_a_correct_schema(hub_ids, caplog):
    c = _hub_client(_good_schemas())
    with caplog.at_level(logging.WARNING, logger="hub"):
        hub.validate_hub_schema(c)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert c.get_schema.call_count == 2
    c.get_database.assert_not_called()


def test_hub_schema_check_reports_missing_property(hub_ids, caplog):
    schemas = _good_schemas()
    del schemas["config"]["properties"]["Errors"]
    with caplog.at_level(logging.ERROR, logger="hub"):
        hub.validate_hub_schema(_hub_client(schemas))
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "Config" in errors[0] and "Errors" in errors[0]


def test_hub_schema_check_reports_wrong_type(hub_ids, caplog):
    schemas = _good_schemas()
    schemas["status"]["properties"]["Last Poll"] = {"type": "rich_text"}
    with caplog.at_level(logging.ERROR, logger="hub"):
        hub.validate_hub_schema(_hub_client(schemas))
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "Last Poll" in errors[0] and "rich_text" in errors[0] and "date" in errors[0]


def test_hub_schema_check_reports_missing_active_option(hub_ids, caplog):
    schemas = _good_schemas()
    schemas["config"]["properties"]["Status"]["status"]["options"] = [{"name": "Inactive"}]
    with caplog.at_level(logging.ERROR, logger="hub"):
        hub.validate_hub_schema(_hub_client(schemas))
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "Config" in errors[0] and "Active" in errors[0]


def test_hub_schema_check_continues_after_a_read_failure(hub_ids, caplog):
    schemas = _good_schemas()

    def get_schema(any_id):
        if any_id == "config":
            raise RuntimeError("boom")
        return schemas[any_id]

    c = _hub_client(schemas)
    c.get_schema.side_effect = get_schema
    with caplog.at_level(logging.ERROR, logger="hub"):
        hub.validate_hub_schema(c)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "could not read the 'Config' data source" in errors[0]
    assert c.get_schema.call_count == 2


def test_hub_schema_check_skipped_when_hub_unavailable(monkeypatch):
    monkeypatch.setattr(hub, "_available", False)
    c = _hub_client({})
    hub.validate_hub_schema(c)
    c.get_schema.assert_not_called()
