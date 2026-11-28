"""Tests for the stored config copy and for recreating a deleted config row (no network)."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

import hub
from test_hub_reader import CFG, TASKS, TASKS_BLOCK, FakeClient, NotFound, _client_with, _code, _row, ready  # noqa: F401

OLD = "e" * 32


class LiveClient(FakeClient):
    """A fake where a created page really becomes a readable row with its body, like Notion does."""

    def create_page(self, ds_id, properties, icon=None, template=None):
        page = super().create_page(ds_id, properties, icon, template)
        status = properties.get("Status", {}).get("status", {}).get("name", "")
        self.rows.setdefault(CFG, []).append(_row(page["id"], name="Automator", status=status))
        return page

    def append_blocks(self, block_id, children):
        super().append_blocks(block_id, children)
        self.bodies[block_id] = [
            {"type": "code", "code": {"rich_text": [{"plain_text": t["text"]["content"]} for t in c["code"]["rich_text"]]}}
            for c in children if c["type"] == "code"
        ]
        return {}


@pytest.fixture
def deleted_row(ready, monkeypatch):
    """The Automator's saved row is OLD and is gone from Notion (404)."""
    monkeypatch.setattr(hub, "_config_row_ids", [OLD])
    monkeypatch.setattr(hub, "_max_recreations", 2)
    monkeypatch.setattr(hub, "_baseline", hub._NO_BASELINE)
    return LiveClient(rows={CFG: []})


def _state():
    return hub._load_recreation()


# ---- the stored copy -----------------------------------------------------------------

def test_a_clean_apply_stores_the_row_text_verbatim():
    assert hub._write_stored_config("# my note\n[[property_automations]]\ndata_source_id = 'x'   \n") is None
    assert hub._read_stored_config() == "# my note\n[[property_automations]]\ndata_source_id = 'x'   \n"
    assert open(hub.STORED_CONFIG_PATH, encoding="utf-8").read().startswith("# Written by Notion Automator")


def test_read_config_stores_the_applied_text_and_a_bad_edit_never_overwrites_it(ready):
    text = "# keep this comment\n" + TASKS_BLOCK
    hub.read_config(_client_with((_row("r1"), text)))
    assert hub._read_stored_config() == text
    hub.read_config(_client_with((_row("r1", edited="t2"), "this is [not valid")))
    assert hub._read_stored_config() == text


def test_a_users_own_file_with_the_same_name_is_never_overwritten(ready):
    with open(hub.STORED_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("[[property_automations]]\ndata_source_id = 'mine'\n")
    hub.read_config(_client_with((_row("r1"), TASKS_BLOCK)))
    assert open(hub.STORED_CONFIG_PATH, encoding="utf-8").read().startswith("[[property_automations]]")
    assert "was not written by the bot" in hub._messages[("config", "r1")]["warnings"][0]


# ---- recreating the row ---------------------------------------------------------------

def test_a_deleted_row_is_restored_active_from_the_stored_copy_and_read_in_the_same_call(deleted_row):
    hub._write_stored_config("# comment\n" + TASKS_BLOCK)
    cfg = hub.read_config(deleted_row)
    (ds, props, _), = deleted_row.created
    assert props["Status"] == {"status": {"name": "Active"}}
    note, code = deleted_row.appended[0][1]
    assert "Reinstated from a backup" in note["paragraph"]["rich_text"][0]["text"]["content"]
    assert note["paragraph"]["rich_text"][1]["text"]["link"]["url"] == "https://www.notion.so/" + OLD
    assert "".join(t["text"]["content"] for t in code["code"]["rich_text"]) == "# comment\n" + TASKS_BLOCK
    assert [d["data_source_id"] for d in cfg["databases"]] == [TASKS]      # the restored config is applied at once
    assert hub._config_row_ids == ["new-0"]
    state = _state()
    assert state["count"] == 1 and state["chain"] == [hub._normalize_uuid(OLD)] and state["warning_level"] == "warning"
    assert hub._messages[("recreation", "new-0")]["warnings"] and "restored from the backup" in state["warning_text"]


def test_with_no_stored_copy_the_new_row_is_a_blank_inactive_example(deleted_row):
    deleted_row.schemas[CFG] = {"properties": {"Status": {"type": "status", "status": {
        "options": [{"id": "1", "name": "Active"}, {"id": "2", "name": "Not started"}],
        "groups": [{"name": "To-do", "option_ids": ["2"]}]}}}}
    hub.read_config(deleted_row)
    (_, props, _), = deleted_row.created
    assert props["Status"] == {"status": {"name": "Not started"}}
    assert "no backup existed" in _state()["warning_text"]


def test_a_long_stored_config_is_split_into_rich_text_pieces_and_read_back_whole(deleted_row):
    text = "# " + "x" * 4500 + "\n" + TASKS_BLOCK
    hub._write_stored_config(text)
    hub.read_config(deleted_row)
    code = deleted_row.appended[0][1][1]["code"]["rich_text"]
    assert len(code) >= 3 and all(len(t["text"]["content"]) <= 2000 for t in code)
    assert hub._read_row_toml(deleted_row, "new-0")[0] == text


def test_a_trashed_page_counts_as_gone(deleted_row):
    deleted_row.pages[OLD] = {"id": OLD, "in_trash": True}
    hub.read_config(deleted_row)
    assert len(deleted_row.created) == 1


def test_a_page_that_still_exists_elsewhere_is_not_replaced(deleted_row, caplog):
    deleted_row.pages[OLD] = {"id": OLD, "in_trash": False, "url": "https://www.notion.so/elsewhere"}
    with caplog.at_level(logging.ERROR, logger="hub"):
        assert hub.read_config(deleted_row) is None
    assert deleted_row.created == [] and "still exists (https://www.notion.so/elsewhere)" in caplog.text
    assert _state()["count"] == 0


def test_a_failed_lookup_is_not_taken_as_deleted(deleted_row, caplog):
    deleted_row.pages[OLD] = ConnectionError("network down")
    with caplog.at_level(logging.ERROR, logger="hub"):
        assert hub.read_config(deleted_row) is None
    assert deleted_row.created == [] and "could not say whether it still exists" in caplog.text


def test_at_most_two_recreations_in_a_row_and_the_last_one_is_an_error(deleted_row, caplog):
    hub._write_stored_config(TASKS_BLOCK)
    deleted_row.rows[CFG] = []
    hub.read_config(deleted_row)                       # recreation 1: Warning
    first = _state()
    assert first["warning_level"] == "warning"
    deleted_row.rows[CFG] = []                         # deleted again before another read saw it
    hub.read_config(deleted_row)                       # recreation 2: the last one, an Error
    second = _state()
    assert second["count"] == 2 and second["warning_level"] == "error" and "2nd config row created in a row" in second["warning_text"]
    assert len(second["chain"]) == 2 and hub._messages[("recreation", "new-1")]["errors"]
    deleted_row.rows[CFG] = []
    with caplog.at_level(logging.ERROR, logger="hub"):
        assert hub.read_config(deleted_row) is None    # a third time: only a log line
    assert len(deleted_row.created) == 2 and "automatic recreation stopped" in caplog.text


def _set_version(client, value):
    client.rows[CFG][0]["properties"][hub._VERSION_PROP] = {"type": "date", "date": {"start": value, "end": None, "time_zone": None}}


def test_the_message_stays_until_the_user_applies_again_and_then_the_counter_and_message_clear(deleted_row):
    hub._write_stored_config(TASKS_BLOCK)
    _set_version_later = lambda v: _set_version(deleted_row, v)
    hub.read_config(deleted_row)                       # creates and applies; records the baseline (no Config Version yet)
    assert _state()["count"] == 1 and ("recreation", "new-0") in hub._messages
    _set_version_later("2026-11-26T10:00:00.000+00:00")   # the column exists and the user presses Apply Config
    hub.read_config(deleted_row)                       # first read after: baseline was "no version"; now it differs
    assert ("recreation", "new-0") not in hub._messages
    assert _state() == {"count": 0, "chain": [], "warning_level": "", "warning_text": "", "warned_at": ""}


def test_a_read_that_changes_nothing_resets_the_counter_but_keeps_the_message(deleted_row):
    hub._write_stored_config(TASKS_BLOCK)
    hub.read_config(deleted_row)
    hub.read_config(deleted_row)                       # same Config Version (none): nothing was re-applied by the user
    state = _state()
    assert state["count"] == 0 and state["chain"] == [] and state["warning_text"]
    assert ("recreation", "new-0") in hub._messages


def test_the_message_clears_after_24_hours_even_without_a_clean_apply(deleted_row):
    hub._write_stored_config("this is [not valid")
    hub.read_config(deleted_row)                       # restored, but its text has an Error
    state = _state()
    state["warned_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    hub._save_hub_ids("new-0", state)
    hub.read_config(deleted_row)
    assert not _state()["warning_text"]


def test_the_saved_message_is_shown_again_after_a_restart(ready, tmp_path):
    state = {"count": 1, "chain": [OLD], "warning_level": "warning", "warning_text": "restored [original](https://www.notion.so/x)",
             "warned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    hub._save_hub_ids("r1", state)
    hub._config_row_ids[:] = ["r1"]
    hub._show_recreation_message()
    assert hub._messages[("recreation", "r1")]["warnings"] == ["restored [original](https://www.notion.so/x)"]


# ---- message links -------------------------------------------------------------------

def test_link_markup_becomes_a_notion_link_and_the_plain_text_is_what_notion_reports():
    text = "Earlier rows: [original](https://www.notion.so/abc), done"
    assert hub._plain(text) == "Earlier rows: original, done"
    assert hub._rich_text(text) == [
        {"text": {"content": "Earlier rows: "}},
        {"text": {"content": "original", "link": {"url": "https://www.notion.so/abc"}}},
        {"text": {"content": ", done"}},
    ]


def test_flush_writes_links_and_does_not_rewrite_unchanged_text(ready):
    hub.report("recreation", warnings=["see [old row](https://www.notion.so/abc)"], row="r1")
    client = FakeClient(rows={CFG: [_row("r1")]})
    hub.flush(client)
    (row_id, props), = client.updates
    assert props["Warnings"]["rich_text"][-1]["text"]["link"]["url"] == "https://www.notion.so/abc"
    shown = _row("r1", warnings="1. [recreation] see old row")
    client2 = FakeClient(rows={CFG: [shown]})
    hub._dirty = True
    hub.flush(client2)
    assert client2.updates == []


# ---- config keys -------------------------------------------------------------------

def test_resolve_max_recreations(caplog):
    assert hub.resolve_max_recreations(None) == 2 and hub.resolve_max_recreations(5) == 5
    for bad in (0, -1, 1.5, "2", True):
        assert hub.resolve_max_recreations(bad) == 2
    assert "hub_max_recreations" in caplog.text


def test_state_file_round_trips_a_message_with_quotes_and_unicode(tmp_path):
    state = {"count": 2, "chain": ["a", "b"], "warning_level": "error", "warning_text": 'it said "no" — [x](https://e.com)', "warned_at": "2026-11-25T10:00:00+00:00"}
    hub._save_hub_ids("page1", state)
    assert hub._load_recreation() == state
    assert json.loads(json.dumps(hub._load_hub_ids())) == {"automator_config_page_id": "page1"}


# ---- config.toml problems on the row, and the stored config fallback -----------------------

def test_config_toml_problems_show_in_warnings_and_the_report(ready):
    hub.report_config_toml([("warning", "week_start 'Funday' is unknown"), ("warning", "day_start_hour 3.5 truncated")])
    client = FakeClient(rows={CFG: [_row("r1")]})
    row = client.rows[CFG][0]
    row["properties"][hub._REPORT_PROP] = {"type": "rich_text", "rich_text": []}
    hub.flush(client)
    (_, props), = client.updates
    assert "1. [config.toml] week_start 'Funday' is unknown" in "".join(s["text"]["content"] for s in props["Warnings"]["rich_text"])
    assert props[hub._REPORT_PROP]["rich_text"][0]["text"]["content"] == "🟡 config.toml: 2 warning(s), see Warnings"
    hub.report_config_toml([])
    assert ("config.toml", "r1") not in hub._messages and ("config-toml", "r1") not in hub._report_lines


def _store(text):
    with open(hub.STORED_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def test_the_stored_config_is_the_users_own_file_when_no_hub_is_configured():
    assert hub.load_stored_config(False) is None                      # no file
    _store(TASKS_BLOCK + f'[[recurring_tasks]]\ndefinitions_data_source_id = "{"3" * 32}"\ntasks_data_source_id = "{TASKS}"\n')
    cfg = hub.load_stored_config(False)
    assert [d["data_source_id"] for d in cfg["databases"]] == [TASKS]
    assert cfg["rtd_pairs"][0]["closed_date_field"] == "Done On"      # the same lookup the Hub read uses


def test_with_a_hub_the_stored_config_is_used_only_if_an_earlier_start_saved_the_row(tmp_path):
    hub._write_stored_config(TASKS_BLOCK)
    assert hub.load_stored_config(True) is None                       # the Hub never worked here: the file is not trusted
    hub._save_hub_ids("saved-row")
    assert [d["data_source_id"] for d in hub.load_stored_config(True)["databases"]] == [TASKS]


def test_a_stored_config_with_errors_is_not_used(caplog):
    _store("this is [not valid")
    with caplog.at_level(logging.ERROR, logger="hub"):
        assert hub.load_stored_config(False) is None
    assert "Invalid TOML" in caplog.text and "so it is not used" in caplog.text
