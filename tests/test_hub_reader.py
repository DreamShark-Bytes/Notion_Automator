"""Tests for the Configuration Hub reader: TOML parsing, read_config, init, polling and schema checks (no network)."""
import logging
import re

import pytest

import hub

CFG, STAT = "c" * 32, "d" * 32
TASKS, TASKS2, DEFS, DEFS2 = "1" * 32, "2" * 32, "3" * 32, "4" * 32


# ---- fakes -------------------------------------------------------------------

def _row(row_id, name="Row", status="Active", errors="", warnings="", edited="t1"):
    def text(kind, value):
        return {kind: [{"plain_text": value}]} if value else {kind: []}
    return {
        "id": row_id,
        "last_edited_time": edited,
        "properties": {
            "Name": text("title", name),
            "Status": {"status": {"name": status}},
            "Errors": text("rich_text", errors),
            "Warnings": text("rich_text", warnings),
        },
    }


def _code(toml_text):
    return {"type": "code", "code": {"language": "toml", "rich_text": [{"plain_text": toml_text}]}}


class FakeClient:
    def __init__(self, rows=None, bodies=None, schemas=None):
        self.rows = rows or {}          # data source id -> [row]
        self.bodies = bodies or {}      # row id -> [block]
        self.schemas = schemas or {}    # data source id -> {"properties": {...}}
        self.updates, self.created, self.appended = [], [], []
        self.fail_query = False
        self.fail_blocks = False
        self.write_time = None          # last_edited_time that a write reports back (Notion returns the page)

    def query_database(self, ds_id, filter_payload=None):
        if self.fail_query:
            raise RuntimeError("boom")
        return list(self.rows.get(ds_id, []))

    def get_blocks(self, row_id):
        if self.fail_blocks:
            raise RuntimeError("boom")
        return self.bodies.get(row_id, [])

    def update_page_properties(self, page_id, properties, icon=None):
        self.updates.append((page_id, properties))
        page = {"id": page_id}
        if self.write_time:
            page["last_edited_time"] = self.write_time
        return page

    def create_page(self, ds_id, properties, icon=None, template=None):
        page = {"id": f"new-{len(self.created)}"}
        self.created.append((ds_id, properties, icon))
        return page

    def append_blocks(self, block_id, children):
        self.appended.append((block_id, children))
        return {}

    def get_schema(self, any_id):
        if any_id not in self.schemas:
            raise RuntimeError("not found")
        return self.schemas[any_id]


@pytest.fixture
def ready(monkeypatch):
    """Hub marked available with config/status data sources; module state restored after each test."""
    monkeypatch.setattr(hub, "_available", True)
    monkeypatch.setattr(hub, "_config_ds_id", CFG)
    monkeypatch.setattr(hub, "_status_ds_id", STAT)
    monkeypatch.setattr(hub, "_config_row_ids", ["r1", "r2"])  # the Automator's own rows; others are ignored
    monkeypatch.setattr(hub, "_hub_status_row_id", "s1")
    monkeypatch.setattr(hub, "_clean_row_ids", set())
    monkeypatch.setattr(hub, "_own_write_times", {})


TASKS_BLOCK = f'''
[[property_automations]]
name = "tasks"
data_source_id = "{TASKS}"
closed_date_property = "Done On"
reopen_count_property = "Reopen Count"
update_count_properties = ["Due Date"]
first_value_properties = ["Due Date", "Status"]
'''
RECURRING = f'''
[recurring_tasks]
definitions_data_source_id = "{DEFS}"
tasks_data_source_id = "{TASKS}"
inherited_properties = ["Priority"]
non_completion_statuses = ["Cancelled"]
'''


# ---- parse_row_config ----------------------------------------------------------

def test_property_automation_maps_to_daemon_shape():
    dbs, rtd, problems = hub.parse_row_config(TASKS_BLOCK)
    assert dbs == [{
        "id": TASKS, "closed_date_field": "Done On", "reopen_count_field": "Reopen Count",
        "first_value_fields": ["Due Date", "Status"], "update_count_fields": ["Due Date"],
    }]
    assert rtd is None and problems == []


def test_dashes_in_ids_are_removed():
    dashed = "-".join([TASKS[:8], TASKS[8:12], TASKS[12:16], TASKS[16:20], TASKS[20:]])
    dbs, _, problems = hub.parse_row_config(f'[[property_automations]]\ndata_source_id = "{dashed}"\n')
    assert dbs[0]["id"] == TASKS and problems == []


def test_optional_keys_default_to_disabled():
    dbs, _, _ = hub.parse_row_config(f'[[property_automations]]\ndata_source_id = "{TASKS}"\n')
    assert dbs[0] == {"id": TASKS, "closed_date_field": None, "reopen_count_field": None,
                      "first_value_fields": [], "update_count_fields": []}


@pytest.mark.parametrize("line,fragment", [("", "is empty"), ('data_source_id = "nope"', "not a valid Notion ID")])
def test_missing_or_malformed_id_skips_the_block_with_an_error(line, fragment):
    dbs, _, problems = hub.parse_row_config(f'[[property_automations]]\nname = "x"\n{line}\n')
    assert dbs == []
    assert [sev for sev, _ in problems] == ["error"] and fragment in problems[0][1] and "'x'" in problems[0][1]


def test_reopen_count_without_closed_date_is_an_error_but_keeps_the_block():
    dbs, _, problems = hub.parse_row_config(
        f'[[property_automations]]\ndata_source_id = "{TASKS}"\nreopen_count_property = "R"\n')
    assert len(dbs) == 1 and problems[0][0] == "error" and "closed_date_property" in problems[0][1]


def test_lists_must_be_arrays():
    dbs, _, problems = hub.parse_row_config(
        f'[[property_automations]]\ndata_source_id = "{TASKS}"\nfirst_value_properties = "Due Date, Status"\n')
    assert dbs[0]["first_value_fields"] == [] and problems[0][0] == "error" and "array" in problems[0][1]


def test_property_names_with_commas_survive_in_arrays():
    dbs, _, problems = hub.parse_row_config(
        f'[[property_automations]]\ndata_source_id = "{TASKS}"\nfirst_value_properties = ["Field, A", "B"]\n')
    assert dbs[0]["first_value_fields"] == ["Field, A", "B"] and problems == []


def test_unknown_keys_warn():
    _, _, problems = hub.parse_row_config(
        f'colour = "red"\n[[property_automations]]\ndata_source_id = "{TASKS}"\ninherit_icon = false\n')
    assert [sev for sev, _ in problems] == ["warning", "warning"]
    assert "colour" in problems[0][1] and "inherit_icon" in problems[1][1]


def test_invalid_toml_reports_line_and_column_without_the_text():
    dbs, rtd, problems = hub.parse_row_config('[[property_automations]]\ndata_source_id = SECRETISH\n')
    assert dbs == [] and rtd is None
    assert problems[0][0] == "error" and "line 2" in problems[0][1] and "SECRETISH" not in problems[0][1]


def test_recurring_tasks_maps_to_daemon_shape():
    _, rtd, problems = hub.parse_row_config(RECURRING)
    assert rtd == {"definitions_db_id": DEFS, "tasks_db_id": TASKS,
                   "inherited_fields": ["Priority"], "non_completion_statuses": ["Cancelled"]} and problems == []


def test_recurring_tasks_with_a_missing_id_is_skipped_with_an_error():
    _, rtd, problems = hub.parse_row_config(f'[recurring_tasks]\ndefinitions_data_source_id = "{DEFS}"\n')
    assert rtd is None and problems[0][0] == "error" and "tasks_data_source_id" in problems[0][1]


def test_a_row_may_hold_both_sections():
    dbs, rtd, problems = hub.parse_row_config(TASKS_BLOCK + RECURRING)
    assert len(dbs) == 1 and rtd is not None and problems == []


def test_example_row_toml_is_inert_and_fits_one_rich_text():
    assert hub.parse_row_config(hub._EXAMPLE_TOML) == ([], None, [])
    assert len(hub._EXAMPLE_TOML) < 2000 and len(hub._EXAMPLE_ROW_INTRO) < 2000


def test_example_row_toml_is_valid_when_every_option_is_switched_on():
    live = "\n".join(re.sub(r"^# (?=\[|\w+ =)", "", line) for line in hub._EXAMPLE_TOML.splitlines())
    live = live.replace("paste-the-data-source-id-here", TASKS)
    dbs, rtd, problems = hub.parse_row_config(live)
    assert len(dbs) == 1 and rtd is not None and problems == []


# ---- read_config -----------------------------------------------------------------

def _client_with(*rows_and_bodies, extra=None):
    rows = [r for r, _ in rows_and_bodies]
    bodies = {r["id"]: [_code(b)] if isinstance(b, str) else b for r, b in rows_and_bodies}
    return FakeClient(rows={CFG: rows}, bodies=bodies)


def test_active_row_builds_the_daemon_config(ready):
    client = _client_with((_row("r1"), TASKS_BLOCK + RECURRING))
    cfg = hub.read_config(client)
    assert [d["id"] for d in cfg["databases"]] == [TASKS]
    assert cfg["rtd_pairs"] == [{
        "definitions_db_id": DEFS, "tasks_db_id": TASKS, "closed_date_field": "Done On",
        "non_completion_statuses": ["Cancelled"], "inherited_fields": ["Priority"],
    }]
    assert client.updates == []  # nothing to report, nothing written


def test_closed_date_defaults_when_no_property_automation_matches(ready):
    cfg = hub.read_config(_client_with((_row("r1"), RECURRING)))
    assert cfg["rtd_pairs"][0]["closed_date_field"] == "Closed Date"


def test_closed_date_comes_from_a_property_automation_in_any_row(ready):
    client = _client_with((_row("r1", status="Not started"), TASKS_BLOCK), (_row("r2"), RECURRING))
    cfg = hub.read_config(client)
    assert cfg["databases"] == [] and cfg["rtd_pairs"][0]["closed_date_field"] == "Done On"


def test_inactive_row_is_not_applied_and_its_problems_are_warnings(ready):
    client = _client_with((_row("r1", status="On hold"), '[[property_automations]]\nname = "x"\n'))
    assert hub.read_config(client) == {"databases": [], "rtd_pairs": []}
    (page_id, props), = client.updates
    assert page_id == "r1" and "Warnings" in props and "Errors" not in props


def test_active_row_with_two_code_blocks_is_an_error_and_applies_nothing(ready):
    client = _client_with((_row("r1"), [_code(TASKS_BLOCK), _code(RECURRING)]))
    assert hub.read_config(client) == {"databases": [], "rtd_pairs": []}
    props = client.updates[0][1]
    assert "2 code blocks" in props["Errors"]["rich_text"][0]["text"]["content"]


def test_active_row_without_a_code_block_is_an_error(ready):
    client = _client_with((_row("r1"), []))
    hub.read_config(client)
    assert "No code block" in client.updates[0][1]["Errors"]["rich_text"][0]["text"]["content"]


def test_messages_are_written_once_and_cleared_when_resolved(ready):
    bad = '[[property_automations]]\nname = "x"\n'
    client = _client_with((_row("r1"), bad))
    hub.read_config(client)
    written = client.updates[0][1]["Errors"]["rich_text"][0]["text"]["content"]

    # Same problem next time: the row already shows it, so nothing is written (a write changes last_edited_time).
    client2 = _client_with((_row("r1", errors=written), bad))
    hub.read_config(client2)
    assert client2.updates == []

    # Problem fixed: the stale text is cleared.
    client3 = _client_with((_row("r1", errors=written), TASKS_BLOCK))
    hub.read_config(client3)
    assert client3.updates == [("r1", {"Errors": {"rich_text": []}})]


def test_duplicate_active_tasks_data_source_disables_both_pairs(ready, caplog):
    other = RECURRING.replace(DEFS, DEFS2)
    client = _client_with((_row("r1", name="A"), RECURRING), (_row("r2", name="B"), other))
    with caplog.at_level(logging.ERROR, logger="hub"):
        cfg = hub.read_config(client)
    assert cfg["rtd_pairs"] == []
    assert "both reference the same tasks data source" in caplog.text
    assert any(page == "r2" and "Errors" in props for page, props in client.updates)


def test_two_pairs_with_different_data_sources_are_kept(ready):
    second = RECURRING.replace(DEFS, DEFS2).replace(TASKS, TASKS2)
    cfg = hub.read_config(_client_with((_row("r1"), RECURRING), (_row("r2"), second)))
    assert len(cfg["rtd_pairs"]) == 2


def test_inactive_row_sharing_a_data_source_with_an_active_pair_warns(ready):
    client = _client_with((_row("r1"), RECURRING), (_row("r2", name="old", status="Discontinued"), RECURRING))
    hub.read_config(client)
    page, props = client.updates[0]
    assert page == "r2" and "shares its tasks data source" in props["Warnings"]["rich_text"][0]["text"]["content"]


def test_unreadable_rows_keep_the_current_config(ready):
    client = _client_with((_row("r1"), TASKS_BLOCK))
    client.fail_query = True
    assert hub.read_config(client) is None
    client.fail_query, client.fail_blocks = False, True
    assert hub.read_config(client) is None
    assert client.updates == []


def test_read_config_is_none_when_the_hub_is_unavailable(monkeypatch):
    monkeypatch.setattr(hub, "_available", False)
    assert hub.read_config(FakeClient()) is None


# ---- init ------------------------------------------------------------------------

@pytest.fixture
def fresh(monkeypatch, tmp_path):
    for name, value in (("_available", False), ("_config_ds_id", None), ("_status_ds_id", None),
                        ("_config_row_ids", []), ("_hub_status_row_id", None)):
        monkeypatch.setattr(hub, name, value)
    path = tmp_path / "config_hub_state.toml"
    monkeypatch.setattr(hub, "HUB_CONFIG_PATH", str(path))
    return path


def _schemas(*ids):
    return {hub._normalize_uuid(i): {"properties": {}} for i in ids}


def _saved(path):
    return hub.tomllib.loads(path.read_text(encoding="utf-8"))["databases"]


def test_init_creates_both_rows_and_saves_their_ids(fresh):
    cfg_id, stat_id = hub._normalize_uuid(CFG), hub._normalize_uuid(STAT)
    client = FakeClient(schemas=_schemas(CFG, STAT))
    assert hub.init(client, CFG, STAT, daemon_version="9.9.9") is True
    assert hub.is_available() and hub._config_ds_id == cfg_id and hub._status_ds_id == stat_id
    created = {ds: props for ds, props, _ in client.created}
    assert "Status" not in created[stat_id]  # only the "Active" option is ever referenced by name
    assert "Status" not in created[cfg_id]  # the data source's options vary; the default applies
    (page_id, children), = client.appended
    assert [c["type"] for c in children] == ["paragraph", "code"] and children[1]["code"]["language"] == "toml"
    assert hub._config_row_ids == ["new-0"] and hub._hub_status_row_id == "new-1"
    assert _saved(fresh) == {"automator_config_page_id": "new-0", "automator_status_page_id": "new-1"}


def test_other_tools_rows_are_never_adopted_and_the_automator_row_is_created(fresh):
    """A config data source that already holds another tool's row (Fuzzy Match) still gets its own page."""
    cfg_id, stat_id = hub._normalize_uuid(CFG), hub._normalize_uuid(STAT)
    client = FakeClient(rows={cfg_id: [_row("fuzzy", name="Fuzzy Match", status="Active")],
                              stat_id: [_row("fuzzy-status", name="Fuzzy Match Status", status="Active")]},
                        schemas=_schemas(CFG, STAT))
    assert hub.init(client, CFG, STAT) is True
    assert "fuzzy" not in hub._config_row_ids and hub._hub_status_row_id != "fuzzy-status"
    assert {ds for ds, _, _ in client.created} == {cfg_id, stat_id}


def test_the_saved_page_id_is_used_even_if_the_row_was_renamed(fresh):
    cfg_id, stat_id = hub._normalize_uuid(CFG), hub._normalize_uuid(STAT)
    fresh.write_text('[databases]\nautomator_config_page_id = "mine"\nautomator_status_page_id = "mine-status"\n')
    client = FakeClient(rows={cfg_id: [_row("fuzzy", name="Automator"), _row("mine", name="My renamed config")],
                              stat_id: [_row("mine-status", name="Renamed", status="Active")]},
                        schemas=_schemas(CFG, STAT))
    assert hub.init(client, CFG, STAT) is True
    assert hub._config_row_ids == ["mine"] and hub._hub_status_row_id == "mine-status"
    assert client.created == []
    assert _saved(fresh) == {"automator_config_page_id": "mine", "automator_status_page_id": "mine-status"}  # unchanged


def test_without_a_saved_id_the_default_name_finds_the_row_and_saves_it(fresh):
    cfg_id, stat_id = hub._normalize_uuid(CFG), hub._normalize_uuid(STAT)
    client = FakeClient(rows={cfg_id: [_row("fuzzy", name="Fuzzy Match"), _row("r1", name="Automator")],
                              stat_id: [_row("s1", name="Automator Notion Status")]},
                        schemas=_schemas(CFG, STAT))
    assert hub.init(client, CFG, STAT) is True
    assert client.created == [] and hub._config_row_ids == ["r1"] and hub._hub_status_row_id == "s1"
    assert _saved(fresh) == {"automator_config_page_id": "r1", "automator_status_page_id": "s1"}


def test_a_stale_saved_id_falls_back_to_the_name_then_to_creating(fresh):
    cfg_id = hub._normalize_uuid(CFG)
    fresh.write_text('[databases]\nautomator_config_page_id = "deleted"\nautomator_status_page_id = ""\n')
    client = FakeClient(rows={cfg_id: [_row("fuzzy", name="Fuzzy Match")]}, schemas=_schemas(CFG))
    assert hub.init(client, CFG) is True
    assert hub._config_row_ids == ["new-0"] and _saved(fresh)["automator_config_page_id"] == "new-0"


def test_status_data_source_is_optional(fresh):
    client = FakeClient(schemas=_schemas(CFG))
    assert hub.init(client, CFG) is True
    assert hub._status_ds_id is None and hub._hub_status_row_id is None
    assert hub.poll_for_changes(client, {})[2] is True  # no kill switch without a status data source
    assert hub.build_snapshot(client)[1] is True


@pytest.mark.parametrize("cfg_id,stat_id", [("not-an-id", ""), (CFG, "short")])
def test_init_rejects_malformed_ids(fresh, cfg_id, stat_id):
    assert hub.init(FakeClient(schemas=_schemas(CFG, STAT)), cfg_id, stat_id) is False
    assert not hub.is_available() and not fresh.exists()


def test_init_fails_when_a_data_source_cannot_be_read(fresh, caplog):
    client = FakeClient(schemas=_schemas(CFG))  # the status data source is missing
    with caplog.at_level(logging.ERROR, logger="hub"):
        assert hub.init(client, CFG, STAT) is False
    assert not hub.is_available() and "status_data_source_id" in caplog.text


# ---- polling ---------------------------------------------------------------------

def _poll(old_rows, new_rows, status_old=(), status_new=()):
    snapshot = {r["id"]: r for r in [*old_rows, *status_old]}
    client = FakeClient(rows={CFG: list(new_rows), STAT: list(status_new)})
    return hub.poll_for_changes(client, snapshot)


def test_new_active_row_triggers_governance(ready):
    assert _poll([], [_row("r1")])[1] is True


def test_row_becoming_active_triggers_governance(ready):
    assert _poll([_row("r1", status="Not started")], [_row("r1")])[1] is True


def test_a_write_by_the_bot_is_not_mistaken_for_a_user_edit(ready):
    hub._own_write_times["r1"] = "t2"
    assert _poll([_row("r1", edited="t1")], [_row("r1", edited="t2")])[1] is False
    # A later user edit still counts.
    assert _poll([_row("r1", edited="t2")], [_row("r1", edited="t3")])[1] is True


def test_editing_an_active_row_triggers_governance(ready):
    assert _poll([_row("r1", edited="t1")], [_row("r1", edited="t2")])[1] is True


def test_row_leaving_active_triggers_a_reload(ready):
    """Switching a row off must reload the config so its databases and RTD pairs stop (no daemon restart)."""
    for status in ("On hold", "Not started", "Discontinued"):
        assert _poll([_row("r1")], [_row("r1", status=status)])[1] is True


def test_unchanged_active_row_does_not(ready):
    assert _poll([_row("r1")], [_row("r1")])[1] is False


def test_editing_an_inactive_row_does_not(ready):
    assert _poll([_row("r1", status="On hold", edited="t1")], [_row("r1", status="On hold", edited="t2")])[1] is False


def test_kill_switch_and_its_release(ready):
    inactive, active = _row("s1", status="Inactive"), _row("s1", status="Active")
    _, govern, is_active = _poll([], [], [active], [inactive])
    assert is_active is False and govern is False
    _, govern, is_active = _poll([], [], [inactive], [active])
    assert is_active is True and govern is True


def test_build_snapshot_reports_the_kill_switch(ready):
    client = FakeClient(rows={CFG: [_row("r1")], STAT: [_row("s1", status="Inactive")]})
    snapshot, is_active = hub.build_snapshot(client)
    assert set(snapshot) == {"r1", "s1"} and is_active is False


# ---- other tools' rows are left completely alone ---------------------------------------

def test_read_config_ignores_and_never_writes_other_tools_rows(ready):
    fuzzy = _row("fuzzy", name="Fuzzy Match", status="Active")
    client = _client_with((_row("r1"), TASKS_BLOCK), (fuzzy, "not toml at all"))
    cfg = hub.read_config(client)
    assert [d["id"] for d in cfg["databases"]] == [TASKS]
    assert client.updates == []


def test_read_config_reports_a_missing_own_row(ready, caplog):
    client = _client_with((_row("fuzzy", name="Fuzzy Match"), "x"))
    with caplog.at_level(logging.ERROR, logger="hub"):
        assert hub.read_config(client) is None
    assert "not in the config data source" in caplog.text and client.updates == []


def test_editing_another_tools_row_does_not_trigger_governance(ready):
    assert _poll([_row("fuzzy", edited="t1")], [_row("fuzzy", edited="t2")])[1] is False


def test_another_tools_status_row_cannot_pause_the_automator(ready):
    other = _row("fuzzy-status", status="Inactive")
    assert _poll([], [], [other], [other])[2] is True
    snapshot, is_active = hub.build_snapshot(FakeClient(rows={CFG: [_row("fuzzy")], STAT: [other]}))
    assert snapshot == {} and is_active is True


def test_a_failed_row_creation_logs_notions_own_message(fresh, caplog):
    class Boom(Exception):
        response = type("R", (), {"json": lambda self: {"message": "Status option does not exist"}})()

    client = FakeClient(schemas=_schemas(CFG))
    client.create_page = lambda *a, **k: (_ for _ in ()).throw(Boom("400 Client Error"))
    with caplog.at_level(logging.WARNING, logger="hub"):
        assert hub.init(client, CFG) is True  # the Hub is up; the row problem is reported, not hidden
    assert "400 Client Error (Status option does not exist)" in caplog.text
    assert hub._config_row_ids == []


# ---- Last verified time ----------------------------------------------------------

def _verifiable(row_id="r1", status="Active"):
    row = _row(row_id, status=status)
    row["properties"][hub._VERIFIED_PROP] = {"type": "date", "date": None}
    return row


def _verified_client(row, toml_text=TASKS_BLOCK):
    return _client_with((row, toml_text))


def test_verified_time_is_written_to_a_clean_active_row_after_governance(ready):
    client = _verified_client(_verifiable())
    hub.read_config(client)
    client.updates.clear()
    client.write_time = "t9"
    hub.mark_verified(client, True)
    (page_id, props), = client.updates
    assert page_id == "r1" and props[hub._VERIFIED_PROP]["date"]["start"]
    assert hub._own_write_times["r1"] == "t9"   # the write is not read back as a user edit


def test_verified_time_is_not_written_when_governance_failed(ready):
    client = _verified_client(_verifiable())
    hub.read_config(client)
    client.updates.clear()
    hub.mark_verified(client, False)
    assert client.updates == []


def test_verified_time_is_not_written_to_a_row_with_errors(ready):
    client = _verified_client(_verifiable(), "[[property_automations]]\nname = \"x\"\n")
    hub.read_config(client)
    client.updates.clear()
    hub.mark_verified(client, True)
    assert client.updates == []


def test_verified_time_is_not_written_to_an_inactive_row(ready):
    client = _verified_client(_verifiable(status="On hold"))
    hub.read_config(client)
    client.updates.clear()
    hub.mark_verified(client, True)
    assert client.updates == []


def test_verified_time_is_skipped_when_the_column_is_missing_or_not_a_date(ready):
    plain = _verified_client(_row("r1"))
    hub.read_config(plain)
    plain.updates.clear()
    hub.mark_verified(plain, True)
    assert plain.updates == []

    wrong = _verifiable()
    wrong["properties"][hub._VERIFIED_PROP] = {"type": "last_edited_time", "last_edited_time": "t1"}
    client = _verified_client(wrong)
    hub.read_config(client)
    client.updates.clear()
    hub.mark_verified(client, True)
    assert client.updates == []


def test_schema_check_flags_a_verified_column_that_is_not_a_date(ready, caplog):
    schemas = {CFG: {"properties": {}}, STAT: {"properties": {}}}
    schemas[CFG]["properties"][hub._VERIFIED_PROP] = {"type": "last_edited_time"}
    with caplog.at_level(logging.ERROR, logger="hub"):
        hub.validate_hub_schema(FakeClient(schemas=schemas))
    assert hub._VERIFIED_PROP in caplog.text
