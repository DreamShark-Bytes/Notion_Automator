"""Tests for Config Version, the settle delay, edited-not-applied warnings, last-good config, messages and Report."""
import logging

import pytest

import hub
from tests.test_hub_reader import (  # noqa: F401  (ready is a fixture used by name)
    CFG, TASKS, TASKS_BLOCK, RECURRING, FakeClient, _client_with, _code, _poll, _row, ready,
)

V1, V2 = "2026-11-25T10:00:00.000+00:00", "2026-11-25T11:00:00.000+00:00"


def _vrow(row_id="r1", version=V1, status="Active", edited="t1", **kw):
    """A config row with the optional Config Version column (version None = column present but empty)."""
    row = _row(row_id, status=status, edited=edited, **kw)
    row["properties"][hub._VERSION_PROP] = {"type": "date", "date": {"start": version} if version else None}
    return row


def _with_report(row):
    row["properties"][hub._REPORT_PROP] = {"type": "rich_text", "rich_text": []}
    return row


def _written(client, row_id="r1"):
    """Merged column values the bot wrote to a row (last write wins), as plain text."""
    out = {}
    for page_id, props in client.updates:
        if page_id == row_id:
            for column, value in props.items():
                parts = value["rich_text"]
                out[column] = parts[0]["text"]["content"] if parts else ""
    return out


def _load(row, body=TASKS_BLOCK):
    client = _client_with((row, body))
    cfg = hub.read_config(client)
    return client, cfg


# ---- Config Version: when the config is applied --------------------------------------

def test_a_changed_config_version_needs_apply_any_difference_counts(ready):
    _load(_vrow(version=V1))
    assert _poll([_vrow(version=V1)], [_vrow(version=V2)])[1] is True
    assert _poll([_vrow(version=V1)], [_vrow(version=V1)])[1] is False
    # Setting it back into the past is still a difference.
    _load(_vrow(version=V2))
    assert _poll([_vrow(version=V2)], [_vrow(version=V1)])[1] is True


def test_setting_an_empty_version_for_the_first_time_applies(ready):
    _load(_vrow(version=None))
    assert _poll([_vrow(version=None)], [_vrow(version=V1)])[1] is True


def test_an_edit_without_a_new_version_is_not_applied(ready):
    _load(_vrow(version=V1, edited="t1"))
    assert _poll([_vrow(version=V1, edited="t1")], [_vrow(version=V1, edited="t2")])[1] is False


def test_an_older_version_applies_with_a_warning(ready):
    client, _ = _load(_vrow(version=V2))
    client2 = _client_with((_vrow(version=V1), TASKS_BLOCK))
    assert hub.read_config(client2) is not None
    hub.flush(client2)
    assert "older than the one loaded" in _written(client2)["Warnings"]


def test_an_empty_version_warns_that_nothing_will_be_applied_until_it_is_set(ready):
    client, _ = _load(_vrow(version=None))
    hub.flush(client)
    assert "Config Version (Datetime) is empty" in _written(client)["Warnings"]


def test_a_version_column_that_is_not_a_date_is_an_error_and_edits_fall_back(ready):
    row = _row("r1")
    row["properties"][hub._VERSION_PROP] = {"type": "rich_text", "rich_text": []}
    client, cfg = _load(row)
    assert [d["data_source_id"] for d in cfg["databases"]] == [TASKS]  # the config still applies
    hub.flush(client)
    assert "not a Date property" in _written(client)["Errors"]
    assert _poll([row], [{**row, "last_edited_time": "t2"}])[1] is True  # edits are detected by last_edited_time


def test_the_bots_own_write_is_not_an_edit(ready):
    _load(_vrow(edited="t1"))
    hub._own_write_times["r1"] = "t2"
    assert _poll([_vrow(edited="t1")], [_vrow(edited="t2")])[1] is False
    assert hub._edit_seen == {}


# ---- Edited but not applied ----------------------------------------------------------

def _poll_and_flush(old, new, bodies):
    client = FakeClient(rows={CFG: list(new)}, bodies=bodies)
    result = hub.poll_for_changes(client, {r["id"]: r for r in old})
    return client, result


def test_a_real_edit_without_a_new_version_warns_after_it_settles(ready):
    _load(_vrow(edited="t1"))
    changed = TASKS_BLOCK.replace("Done On", "Finished On")
    client, (_, needs_apply) = _poll_and_flush([_vrow(edited="t1")], [_vrow(edited="t2")], {"r1": [_code(changed)]})
    assert needs_apply is False
    assert "Edited but not applied" in _written(client)["Warnings"]
    assert "[config-edit]" in _written(client)["Warnings"]


def test_comment_spacing_and_key_order_edits_never_warn(ready):
    _load(_vrow(edited="t1"))
    reordered = "# a new comment\n" + TASKS_BLOCK.replace('name = "tasks"\n', '').replace(
        'data_source_id', 'name = "tasks"\n\n\ndata_source_id', 1)
    client, _ = _poll_and_flush([_vrow(edited="t1")], [_vrow(edited="t2")], {"r1": [_code(reordered)]})
    assert client.updates == []
    assert hub._loaded["r1"]["edited"] == "t2"  # compared and equal: the edit is not pending again


def test_the_edit_warning_says_why_it_would_not_apply(ready):
    _load(_vrow(edited="t1"))
    client, _ = _poll_and_flush([_vrow(edited="t1")], [_vrow(edited="t2")], {"r1": [_code("[[property_automations\n")]})
    assert "Invalid TOML" in _written(client)["Warnings"]


def test_the_edit_is_compared_only_once_per_edit(ready):
    _load(_vrow(edited="t1"))
    changed = {"r1": [_code(TASKS_BLOCK.replace("Done On", "X"))]}
    client = FakeClient(rows={CFG: [_vrow(edited="t2")]}, bodies=changed)
    snapshot = {"r1": _vrow(edited="t1")}
    hub.poll_for_changes(client, snapshot)
    hub.poll_for_changes(client, snapshot)
    assert client.bodies_read == ["r1"]


def test_applying_clears_the_edit_warning(ready):
    _load(_vrow(edited="t1"))
    client, _ = _poll_and_flush([_vrow(edited="t1")], [_vrow(edited="t2")], {"r1": [_code(TASKS_BLOCK.replace("Done On", "X"))]})
    assert ("config-edit", "r1") in hub._messages
    hub.read_config(_client_with((_vrow(version=V2, edited="t2"), TASKS_BLOCK.replace("Done On", "X"))))
    assert ("config-edit", "r1") not in hub._messages and ("config-edit", "r1") not in hub._report_lines


def test_the_daily_check_compares_every_row_in_config_version_mode(ready):
    _load(_vrow())
    client = FakeClient(rows={CFG: [_vrow()]}, bodies={"r1": [_code(TASKS_BLOCK.replace("Done On", "X"))]})
    hub.check_unapplied(client)
    assert ("config-edit", "r1") in hub._messages
    client = FakeClient(rows={CFG: [_vrow()]}, bodies={"r1": [_code(TASKS_BLOCK)]})
    hub.check_unapplied(client)
    assert ("config-edit", "r1") not in hub._messages


def test_the_daily_check_skips_rows_without_a_version_column(ready):
    _load(_row("r1"))
    client = FakeClient(rows={CFG: [_row("r1")]}, bodies={"r1": [_code("garbage = [")]})
    hub.check_unapplied(client)
    assert client.bodies_read == []


# ---- Settle delay --------------------------------------------------------------------

def test_without_a_version_column_an_edit_applies_only_after_the_settle_time(ready, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(hub, "_now", lambda: clock[0])
    monkeypatch.setattr(hub, "_settle_seconds", 300)
    _load(_row("r1", edited="t1"))
    old, new = [_row("r1", edited="t1")], [_row("r1", edited="t2")]
    assert _poll(old, new)[1] is False          # seen now: the user may still be typing
    clock[0] += 299
    assert _poll(old, new)[1] is False
    clock[0] += 2
    assert _poll(old, new)[1] is True            # unchanged for the settle time


def test_another_edit_restarts_the_settle_time(ready, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(hub, "_now", lambda: clock[0])
    monkeypatch.setattr(hub, "_settle_seconds", 300)
    _load(_row("r1", edited="t1"))
    old = [_row("r1", edited="t1")]
    _poll(old, [_row("r1", edited="t2")])
    clock[0] = 290
    _poll(old, [_row("r1", edited="t3")])        # edited again
    clock[0] = 400
    assert _poll(old, [_row("r1", edited="t3")])[1] is False
    clock[0] = 600
    assert _poll(old, [_row("r1", edited="t3")])[1] is True


@pytest.mark.parametrize("raw, expected", [(None, 300), (0, 0), (45, 45), (2.5, 2.5)])
def test_settle_seconds_setting(raw, expected):
    assert hub.resolve_settle_seconds(raw) == expected


@pytest.mark.parametrize("raw", [-1, "5 minutes", True])
def test_bad_settle_seconds_falls_back_with_a_warning(raw, caplog):
    with caplog.at_level(logging.WARNING, logger="hub"):
        assert hub.resolve_settle_seconds(raw) == hub.DEFAULT_SETTLE_SECONDS
    assert "hub_settle_seconds" in caplog.text


# ---- Leaving and entering Active -----------------------------------------------------

def test_leaving_active_needs_apply_and_stops_everything_from_that_row(ready):
    _load(_vrow())
    assert _poll([_vrow()], [_vrow(status="On hold")])[1] is True
    cfg = hub.read_config(_client_with((_vrow(status="On hold"), TASKS_BLOCK + RECURRING)))
    assert cfg == {"databases": [], "rtd_pairs": []}
    assert "r1" not in hub._loaded


def test_becoming_active_needs_apply(ready):
    assert _poll([_vrow(status="On hold")], [_vrow()])[1] is True


# ---- A failed apply keeps the last good config ---------------------------------------

def test_a_failed_apply_keeps_the_running_config_and_warns(ready):
    _load(_vrow(version=V1))
    client = _client_with((_vrow(version=V2), "[[property_automations\n"))
    cfg = hub.read_config(client)
    assert [d["data_source_id"] for d in cfg["databases"]] == [TASKS]   # the config from V1 still runs
    hub.flush(client)
    written = _written(client)
    assert "Invalid TOML" in written["Warnings"] and "still running the config from Config Version" in written["Warnings"]
    assert written.get("Errors", "") == ""                                 # nothing is stopped, so no Error
    assert hub._report_lines[("config", "r1")].startswith("🔴 Config: apply failed, still running version " + hub._format_version(V1))
    assert hub._messages[("config", "r1")]["errors"] == []
    # The failed version is remembered, so the next poll does not retry until Config Version changes again.
    assert _poll([_vrow(version=V2)], [_vrow(version=V2)])[1] is False


def test_a_failed_first_apply_is_an_error_and_applies_what_it_can(ready):
    both = TASKS_BLOCK + '\n[[property_automations]]\nname = "bad"\n'
    client, cfg = _load(_vrow(), both)
    assert [d["data_source_id"] for d in cfg["databases"]] == [TASKS]
    hub.flush(client)
    assert "data_source_id is empty" in _written(client)["Errors"]
    assert hub._report_lines[("config", "r1")].startswith("🔴 Config: applied with errors, see Errors")


def test_a_failed_apply_after_a_good_one_does_not_replace_the_last_good_config(ready):
    _load(_vrow(version=V1))
    hub.read_config(_client_with((_vrow(version=V2), "garbage [")))
    cfg = hub.read_config(_client_with((_vrow(version="2026-11-25T12:00:00.000+00:00"), "garbage [")))
    assert [d["data_source_id"] for d in cfg["databases"]] == [TASKS]


def test_a_later_good_apply_clears_the_failure(ready):
    _load(_vrow(version=V1))
    hub.read_config(_client_with((_vrow(version=V2), "garbage [")))
    client = _client_with((_vrow(version="2026-11-25T12:00:00.000+00:00"), TASKS_BLOCK.replace("Done On", "X")))
    cfg = hub.read_config(client)
    assert cfg["databases"][0]["closed_date_field"] == "X"
    assert hub._messages.get(("config", "r1")) is None
    assert hub._report_lines[("config", "r1")].startswith("🟢 Config: applied")


# ---- Messages and Report -------------------------------------------------------------

def test_sources_replace_only_their_own_messages(ready):
    hub.report("governance", errors=["a failed"], row="r1")
    hub.report("config", warnings=["b"], row="r1")
    hub.report("governance", row="r1")  # empty report clears that source only
    assert hub._messages == {("config", "r1"): {"errors": [], "warnings": ["b"]}}


def test_flush_prefixes_each_message_with_its_source_and_writes_once(ready):
    hub.report("governance", errors=["x failed"], row="r1")
    hub.report("config", warnings=["careful"], row="r1")
    client = FakeClient(rows={CFG: [_row("r1")]})
    hub.flush(client)
    assert _written(client) == {"Errors": "[governance] x failed", "Warnings": "[config] careful"}
    hub.flush(client)  # nothing changed since the last flush
    assert len(client.updates) == 1


def test_flush_does_not_rewrite_text_the_row_already_shows(ready):
    hub.report("governance", errors=["x failed"], row="r1")
    client = FakeClient(rows={CFG: [_row("r1", errors="[governance] x failed")]})
    hub.flush(client)
    assert client.updates == []


def test_a_failed_flush_is_retried(ready):
    hub.report("governance", errors=["x"], row="r1")
    client = FakeClient(rows={CFG: [_row("r1")]})
    client.update_page_properties = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    hub.flush(client)
    assert hub._dirty is True


def test_long_messages_end_with_a_visible_marker():
    messages = [f"message number {i} " + "x" * 200 for i in range(20)]
    text = hub._message_text(messages)
    assert len(text) <= 1900 and text.endswith("more, see the log)")
    assert text.startswith("message number 0 ")
    assert hub._message_text(["short"]) == "short"
    assert hub._message_text(["y" * 5000]).endswith("…")


def test_report_lines_are_written_in_order_when_the_column_exists(ready):
    hub.report_line("governance", "🟢 Governance: OK", row="r1")
    hub.report_line("app", "App: Version 9, Started: now", row="r1")
    hub.report_line("config", "🟢 Config: applied", row="r1")
    client = FakeClient(rows={CFG: [_with_report(_row("r1"))]})
    hub.flush(client)
    assert _written(client)["Report"].splitlines() == ["App: Version 9, Started: now", "🟢 Config: applied", "🟢 Governance: OK"]


def test_no_report_is_written_without_the_column(ready):
    hub.report_line("governance", "🟢 Governance: OK", row="r1")
    client = FakeClient(rows={CFG: [_row("r1")]})
    hub.flush(client)
    assert client.updates == []


def test_governance_failures_become_errors_under_their_own_source(ready):
    hub.record_governance(["run_recurring_governance failed: boom"], "daemon startup")
    assert hub._messages[("governance", "r1")]["errors"] == ["run_recurring_governance failed: boom"]
    assert hub._report_lines[("governance", "r1")].startswith("🔴 Governance: failed, see Errors")
    hub.record_governance([], "daily 03:00 cron")
    assert ("governance", "r1") not in hub._messages
    assert hub._report_lines[("governance", "r1")].startswith("🟢 Governance: OK")


def test_init_records_the_version_line_for_the_row(fresh_hub_init):
    client, path = fresh_hub_init
    assert hub.init(client, CFG, daemon_version="9.9.9") is True
    line = hub._report_lines[("app", hub._config_row_ids[0])]
    assert line.startswith("App: Version 9.9.9, Started: ")


def test_schema_check_reports_optional_columns_of_the_wrong_type(ready, caplog):
    schemas = {CFG: {"properties": {
        hub._VERSION_PROP: {"type": "rich_text"},
        hub._REPORT_PROP: {"type": "date"},
    }}}
    with caplog.at_level(logging.ERROR, logger="hub"):
        hub.validate_hub_schema(FakeClient(schemas=schemas))
    assert hub._VERSION_PROP in caplog.text and hub._REPORT_PROP in caplog.text


@pytest.fixture
def fresh_hub_init(monkeypatch, tmp_path):
    for name, value in (("_available", False), ("_config_ds_id", None), ("_config_row_ids", [])):
        monkeypatch.setattr(hub, name, value)
    monkeypatch.setattr(hub, "HUB_CONFIG_PATH", str(tmp_path / "config_hub_state.toml"))
    monkeypatch.setattr(hub, "_dirty", False)
    for name in ("_loaded", "_last_good", "_edit_seen", "_checked_edit", "_messages", "_report_lines", "_own_write_times"):
        monkeypatch.setattr(hub, name, {})
    return FakeClient(schemas={hub._normalize_uuid(CFG): {"properties": {}}}), tmp_path


# ---- Empty and comment-only code blocks ------------------------------------------------

def test_an_empty_code_block_is_an_error_when_nothing_is_loaded(ready):
    client, cfg = _load(_vrow(), "   \n")
    assert cfg == {"databases": [], "rtd_pairs": []}
    hub.flush(client)
    assert "The code block is empty" in _written(client)["Errors"]
    assert hub._report_lines[("config", "r1")].startswith("🔴 Config: not loaded, see Errors")


def test_an_empty_code_block_never_replaces_a_running_config(ready):
    _load(_vrow(version=V1))
    client = _client_with((_vrow(version=V2), ""))
    cfg = hub.read_config(client)
    assert [d["data_source_id"] for d in cfg["databases"]] == [TASKS]
    hub.flush(client)
    assert "The code block is empty" in _written(client)["Warnings"]
    assert _written(client).get("Errors", "") == ""


def test_a_comment_only_block_is_a_warning_that_nothing_runs(ready):
    client, cfg = _load(_vrow(), "# [[property_automations]]\n# name = 'x'\n")
    assert cfg == {"databases": [], "rtd_pairs": []}
    hub.flush(client)
    assert "nothing is configured" in _written(client)["Warnings"]
    assert hub._report_lines[("config", "r1")].startswith("🟡 Config: applied, see Warnings")


# ---- Yellow Report line for Warnings, empty and older versions --------------------------

def test_an_empty_config_version_warns_in_the_row_and_shows_a_yellow_line(ready):
    """Row with the column present but empty (what Notion returns: a date property whose value is null)."""
    client = _client_with((_with_report(_vrow(version=None)), TASKS_BLOCK))
    hub.read_config(client)
    hub.flush(client)
    written = _written(client)
    assert "Config Version (Datetime) is empty" in written["Warnings"]
    assert "🟡 Config: applied, see Warnings" in written["Report"]


def test_an_older_config_version_shows_a_yellow_line(ready):
    _load(_vrow(version=V2))
    client = _client_with((_with_report(_vrow(version=V1)), TASKS_BLOCK))
    hub.read_config(client)
    hub.flush(client)
    assert "🟡 Config: applied, see Warnings" in _written(client)["Report"]


# ---- Waiting for an edit to settle is a Report line, not a Warning ---------------------

def test_an_edit_that_is_still_settling_shows_a_report_line_and_no_warning(ready, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(hub, "_now", lambda: clock[0])
    monkeypatch.setattr(hub, "_settle_seconds", 300)
    _load(_with_report(_vrow(edited="t1")))
    client = FakeClient(rows={CFG: [_with_report(_vrow(edited="t2"))]}, bodies={"r1": [_code(TASKS_BLOCK)]})
    hub.poll_for_changes(client, {"r1": _with_report(_vrow(edited="t1"))})
    written = _written(client)
    assert "🟡 Config: edit detected, will check at " in written["Report"]
    assert written.get("Warnings", "") == "" and client.bodies_read == []


def test_without_a_version_column_the_waiting_line_says_will_apply(ready, monkeypatch):
    monkeypatch.setattr(hub, "_now", lambda: 0.0)
    monkeypatch.setattr(hub, "_settle_seconds", 300)
    _load(_with_report(_row("r1", edited="t1")))
    client = FakeClient(rows={CFG: [_with_report(_row("r1", edited="t2"))]})
    hub.poll_for_changes(client, {"r1": _with_report(_row("r1", edited="t1"))})
    assert "will apply at " in _written(client)["Report"]


def test_reading_the_config_clears_the_waiting_line(ready, monkeypatch):
    monkeypatch.setattr(hub, "_now", lambda: 0.0)
    monkeypatch.setattr(hub, "_settle_seconds", 300)
    _load(_vrow(edited="t1"))
    hub.poll_for_changes(FakeClient(rows={CFG: [_vrow(edited="t2")]}), {"r1": _vrow(edited="t1")})
    assert ("config-edit", "r1") in hub._report_lines
    hub.read_config(_client_with((_vrow(version=V2, edited="t2"), TASKS_BLOCK)))
    assert ("config-edit", "r1") not in hub._report_lines


# ---- Time display ---------------------------------------------------------------------

def test_versions_are_shown_to_the_minute_with_a_zone_and_no_seconds(ready):
    import zoneinfo
    shown = hub._format_version("2026-11-25T16:30:45.123+00:00")
    assert shown.startswith("2026-11-25 ") and ":45" not in shown and ".123" not in shown
    hub._version_zone = zoneinfo.ZoneInfo("America/Chicago")
    assert hub._format_version("2026-11-25T16:30:45.123+00:00") == "2026-11-25 10:30 CST"
    assert hub._format_version("2026-07-25T16:30:00+00:00") == "2026-07-25 11:30 CDT"  # daylight saving
    assert hub._format_version("2026-11-25") == "2026-11-25"
    assert hub._format_version(None) == "none" and hub._format_version("not a date") == "not a date"


def test_the_zone_comes_from_the_config_version_value(ready):
    import datetime as dt
    assert hub._zone_from_version({"start": "2026-11-25T10:00:00.000-06:00", "time_zone": None}).utcoffset(None) == dt.timedelta(hours=-6)
    assert str(hub._zone_from_version({"start": "2026-11-25T10:00:00.000-06:00", "time_zone": "America/Chicago"})) == "America/Chicago"
    assert hub._zone_from_version({"start": "2026-11-25", "time_zone": None}) is None
    assert hub._zone_from_version(None) is None
    assert hub._zone_from_version({"start": "2026-11-25T10:00:00", "time_zone": None}) is None  # no offset in the text


def test_reading_the_config_adopts_the_version_zone_and_shows_it(ready):
    client = _client_with((_with_report(_vrow(version="2026-11-25T10:00:00.000-06:00")), TASKS_BLOCK))
    hub.read_config(client)
    hub.flush(client)
    report = _written(client)["Report"]
    assert "version 2026-11-25 10:00 UTC-6" in report and "Started: " in report and "UTC-6" in report.split("Started: ")[1].splitlines()[0]
    # The user moves to another zone and presses the button: the next read shows that zone.
    client2 = _client_with((_with_report(_vrow(version="2026-11-26T09:00:00.000+01:00")), TASKS_BLOCK))
    hub.read_config(client2)
    hub.flush(client2)
    assert "version 2026-11-26 09:00 UTC+1" in _written(client2)["Report"]


def test_a_daylight_saving_change_is_a_different_value_and_the_same_text_is_not(ready):
    """Only the raw text is compared: the same text never counts as a change, and a different offset does."""
    _load(_vrow(version="2026-03-08T01:30:00.000-06:00"))
    assert _poll([_vrow(version="2026-03-08T01:30:00.000-06:00")], [_vrow(version="2026-03-08T01:30:00.000-06:00")])[1] is False
    assert _poll([_vrow(version="2026-03-08T01:30:00.000-06:00")], [_vrow(version="2026-03-08T03:30:00.000-05:00")])[1] is True
