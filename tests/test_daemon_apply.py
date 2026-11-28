"""Tests for the daemon's single apply function and the runtime state around it (no network)."""
import pytest

import automations
import daemon
import hub
import recurring_tasks

A, B, C = "a" * 32, "b" * 32, "c" * 32
D1, D2, T1, T2 = "d1" * 16, "d2" * 16, "e1" * 16, "e2" * 16


class FakeClient:
    """Notion client stand-in. data_sources maps a database ID to its data source IDs."""
    def __init__(self, uses_data_sources=False, data_sources=None):
        self.uses_data_sources = uses_data_sources
        self.databases = data_sources or {}
        self.queries = []
        self.fail_for = set()

    def resolve_data_source_id(self, any_id):
        sources = self.databases.get(any_id)
        if sources is None:
            return any_id
        return sources[0]

    def query_database(self, ds_id, filter_payload=None):
        self.queries.append(ds_id)
        if ds_id in self.fail_for:
            raise RuntimeError("boom")
        return []


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    automations.clear_data_sources()
    recurring_tasks.configure_pairs([], 0, 3)
    init_calls = []
    monkeypatch.setattr(daemon, "run_automations_init_pass",
                        lambda client, ds_id: init_calls.append(ds_id) or {"seed-" + ds_id: {"id": "seed-" + ds_id}})
    yield init_calls
    automations.clear_data_sources()
    recurring_tasks.configure_pairs([], 0, 3)


def _db(ds_id, **kw):
    return {"data_source_id": ds_id, "closed_date_field": None, "reopen_count_field": None,
            "first_value_fields": [], "update_count_fields": [], **kw}


def _pair(defs, tasks):
    return {"definitions_data_source_id": defs, "tasks_data_source_id": tasks, "closed_date_field": "Closed Date",
            "non_completion_statuses": [], "inherit_include_properties": []}


def _apply(state, databases, pairs, client=None):
    daemon.apply_config(client or FakeClient(), state, databases, pairs, 0, 3)


# ---- task data sources ---------------------------------------------------------------

def test_a_new_data_source_gets_an_init_pass_registration_and_a_poll_pointer(clean_state):
    state = daemon.RuntimeState()
    _apply(state, [_db(A, closed_date_field="Done")], [])
    assert clean_state == [A]
    assert state.data_source_ids == [A] and list(state.snapshots) == [A] and A in state.last_polled
    assert automations._data_source_configs[A]["closed_date_field"] == "Done"


def test_applying_again_keeps_existing_snapshots_and_adds_only_new_sources(clean_state):
    state = daemon.RuntimeState()
    _apply(state, [_db(A)], [])
    state.snapshots[A]["page"] = {"id": "page"}
    _apply(state, [_db(A), _db(B)], [])
    assert clean_state == [A, B]                       # A was not scanned again
    assert "page" in state.snapshots[A]                # and kept its snapshot
    assert state.data_source_ids == [A, B]


def test_a_data_source_that_is_no_longer_configured_stops_and_is_forgotten(clean_state):
    state = daemon.RuntimeState()
    _apply(state, [_db(A), _db(B)], [])
    _apply(state, [_db(B)], [])
    assert state.data_source_ids == [B] and list(state.snapshots) == [B] and list(state.last_polled) == [B]
    assert A not in automations._data_source_configs   # its flags are gone, so its pages get no automations


def test_an_empty_config_stops_everything(clean_state):
    state = daemon.RuntimeState()
    _apply(state, [_db(A)], [_pair(D1, T1)])
    _apply(state, [], [])
    assert state.data_source_ids == [] and state.snapshots == {} and state.rtd_definitions_ids == []
    assert recurring_tasks._rtd_pairs == [] and automations._data_source_configs == {}


def test_a_data_source_listed_twice_is_scanned_once(clean_state):
    _apply(daemon.RuntimeState(), [_db(A), _db(A)], [])
    assert clean_state == [A]


# ---- recurring-task pairs -------------------------------------------------------------

def test_a_removed_pair_stops_without_a_restart():
    state = daemon.RuntimeState()
    _apply(state, [], [_pair(D1, T1)])
    assert len(recurring_tasks._rtd_pairs) == 1 and state.rtd_definitions_ids == [D1]
    _apply(state, [], [])
    assert recurring_tasks._rtd_pairs == [] and state.rtd_definitions_ids == []


def test_every_pairs_definitions_data_source_is_polled():
    state = daemon.RuntimeState()
    client = FakeClient()
    _apply(state, [], [_pair(D1, T1), _pair(D2, T2)], client)
    daemon.refresh_rtd_snapshots(client, state)
    assert set(state.rtd_snapshots) == {D1, D2}
    client.queries.clear()
    daemon.poll_rtd_definitions(client, state)
    assert client.queries == [D1, D2]


def test_a_removed_pairs_rtd_snapshot_is_dropped():
    state = daemon.RuntimeState()
    client = FakeClient()
    _apply(state, [], [_pair(D1, T1), _pair(D2, T2)], client)
    daemon.refresh_rtd_snapshots(client, state)
    _apply(state, [], [_pair(D2, T2)], client)
    assert list(state.rtd_snapshots) == [D2] and list(state.last_polled_rtd) == [D2]


def test_a_failed_rtd_snapshot_leaves_that_pair_unpolled_until_it_is_built():
    state = daemon.RuntimeState()
    client = FakeClient()
    client.fail_for = {D1}
    _apply(state, [], [_pair(D1, T1), _pair(D2, T2)], client)
    daemon.refresh_rtd_snapshots(client, state)
    assert list(state.rtd_snapshots) == [D2]
    client.queries.clear()
    daemon.poll_rtd_definitions(client, state)
    assert client.queries == [D2]


def test_one_pairs_failed_poll_does_not_stop_the_others(monkeypatch):
    state = daemon.RuntimeState()
    client = FakeClient()
    _apply(state, [], [_pair(D1, T1), _pair(D2, T2)], client)
    daemon.refresh_rtd_snapshots(client, state)
    before = dict(state.last_polled_rtd)
    client.fail_for = {D1}
    assert daemon.poll_rtd_definitions(client, state) is False
    assert state.last_polled_rtd[D1] == before[D1]      # the pointer does not advance on failure


# ---- IDs from the config --------------------------------------------------------------

def test_a_database_id_in_the_config_is_resolved_to_its_data_source(caplog, clean_state):
    client = FakeClient(uses_data_sources=True, data_sources={A: [B]})
    state = daemon.RuntimeState()
    with caplog.at_level("WARNING", logger="daemon"):
        _apply(state, [_db(A)], [], client)
    assert state.data_source_ids == [B] and clean_state == [B]
    assert B in automations._data_source_configs and A not in automations._data_source_configs
    assert "is a database ID, not a data source ID" in caplog.text


def test_a_data_source_id_is_left_alone(caplog):
    client = FakeClient(uses_data_sources=True, data_sources={})
    state = daemon.RuntimeState()
    with caplog.at_level("WARNING", logger="daemon"):
        _apply(state, [_db(A.upper())], [], client)
    assert state.data_source_ids == [A] and "database ID" not in caplog.text


def test_ids_are_used_as_they_are_on_an_api_version_without_data_sources(clean_state):
    client = FakeClient(uses_data_sources=False, data_sources={A: [B]})
    state = daemon.RuntimeState()
    _apply(state, [_db(A)], [], client)
    assert state.data_source_ids == [A]


def test_a_failed_lookup_falls_back_to_the_configured_id(caplog):
    class Failing(FakeClient):
        def resolve_data_source_id(self, any_id):
            raise RuntimeError("network down")
    state = daemon.RuntimeState()
    with caplog.at_level("WARNING", logger="daemon"):
        _apply(state, [_db(A)], [], Failing(uses_data_sources=True))
    assert state.data_source_ids == [A] and "Could not look up data source" in caplog.text


# ---- pages created by governance ------------------------------------------------------

def test_governance_created_pages_find_their_snapshot_by_data_source(monkeypatch):
    seen = []
    monkeypatch.setattr(daemon, "run_automations_on_page", lambda client, page, prev: seen.append(page["id"]) or (None, []))
    dashed = "-".join([A[:8], A[8:12], A[12:16], A[16:20], A[20:]])
    snapshots = {A: {}, B: {}}
    page = {"id": "p1", "parent": {"data_source_id": dashed, "database_id": "some-database"}}
    daemon._init_pass_on_pages(None, [page], snapshots)
    assert seen == ["p1"] and "p1" in snapshots[A] and snapshots[B] == {}


# ---- governance reporting -------------------------------------------------------------

def test_a_governance_pass_records_failures_and_flushes_to_the_hub(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "GOVERNANCE", [lambda client: 1 / 0, lambda client: [{"id": "made"}]])
    monkeypatch.setattr(daemon, "flush_bot_notes", lambda client: None)
    monkeypatch.setattr(hub, "check_unapplied", lambda client: calls.append("check"))
    monkeypatch.setattr(hub, "record_governance", lambda failures, trigger: calls.append(("record", failures, trigger)))
    monkeypatch.setattr(hub, "flush", lambda client: calls.append("flush"))
    monkeypatch.setattr(hub, "mark_verified", lambda client, ok: calls.append(("verified", ok)))
    created = daemon.run_governance(None, "daily 03:00 cron")
    assert created == [{"id": "made"}]
    assert calls[0] == "check" and calls[2:] == ["flush", ("verified", False)]
    assert calls[1][0] == "record" and calls[1][2] == "daily 03:00 cron" and "division by zero" in calls[1][1][0]


def test_a_clean_governance_pass_stamps_verified(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "GOVERNANCE", [lambda client: None])
    monkeypatch.setattr(daemon, "flush_bot_notes", lambda client: None)
    monkeypatch.setattr(hub, "check_unapplied", lambda client: None)
    monkeypatch.setattr(hub, "record_governance", lambda failures, trigger: calls.append(failures))
    monkeypatch.setattr(hub, "flush", lambda client: None)
    monkeypatch.setattr(hub, "mark_verified", lambda client, ok: calls.append(ok))
    daemon.run_governance(None)
    assert calls == [[], True]  # no daily-pass check outside the daily cron


# ---- main(): start, then a config change in Notion, against a fake client ---------------

class _Stop(Exception):
    pass


def _run_main(monkeypatch, tmp_path, fake, config_text, after_each_sleep):
    """Run daemon.main() with a fake Notion client; `after_each_sleep[i]()` runs when the loop sleeps the i-th time."""
    (tmp_path / "config.toml").write_text(config_text, encoding="utf-8")
    (tmp_path / "config_secrets.toml").write_text('token = "test-token"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["daemon.py", "--config", str(tmp_path / "config.toml")])
    monkeypatch.setattr(daemon, "NotionClient", lambda *a, **k: fake)
    monkeypatch.setattr(daemon, "_configure_logging", lambda debug: None)
    monkeypatch.setattr(hub, "HUB_CONFIG_PATH", str(tmp_path / "config_hub_state.toml"))
    sleeps = iter(after_each_sleep)

    def fake_sleep(seconds):
        try:
            next(sleeps)()
        except StopIteration:
            raise _Stop()

    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        daemon.main()


def test_main_starts_from_the_stored_config_file_without_the_hub(monkeypatch, tmp_path, clean_state):
    fake = FakeClient()
    (tmp_path / "config_automations.toml").write_text(f'[[property_automations]]\ndata_source_id = "{A}"\nclosed_date_property = "Closed Date"\n', encoding="utf-8")
    monkeypatch.setattr(hub, "STORED_CONFIG_PATH", str(tmp_path / "config_automations.toml"))
    _run_main(monkeypatch, tmp_path, fake, "poll_interval = 60\n", [lambda: None])
    assert clean_state == [A]                                  # init pass ran once for the stored config's data source
    assert automations._data_source_configs[A]["closed_date_field"] == "Closed Date"
    assert fake.queries.count(A) >= 1                          # and the first poll queried it


def test_main_stops_with_the_converted_text_when_config_toml_has_the_old_format(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError) as err:
        _run_main(monkeypatch, tmp_path, FakeClient(), f'[[databases]]\nid = "{A}"\nclosed_date = true\n', [lambda: None])
    assert "[[property_automations]]" in str(err.value) and f'data_source_id = "{A}"' in str(err.value)


def test_main_stops_when_there_is_no_config_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(hub, "STORED_CONFIG_PATH", str(tmp_path / "config_automations.toml"))
    with pytest.raises(RuntimeError, match="No config"):
        _run_main(monkeypatch, tmp_path, FakeClient(), "poll_interval = 60\n", [lambda: None])


def test_main_applies_a_config_version_change_and_a_pause_without_a_restart(monkeypatch, tmp_path, clean_state):
    from tests.test_hub_reader import FakeClient as HubFake, TASKS, TASKS2, TASKS_BLOCK, CFG, _code, _row
    V1, V2, V3 = ("2026-11-25T10:00:00.000+00:00", "2026-11-25T11:00:00.000+00:00", "2026-11-25T12:00:00.000+00:00")

    def row(version, status="Active", edited="t1"):
        r = _row("r1", name="Automator", status=status, edited=edited)
        r["properties"][hub._VERSION_PROP] = {"type": "date", "date": {"start": version}}
        r["properties"][hub._REPORT_PROP] = {"type": "rich_text", "rich_text": []}
        return r

    cfg_key = hub._normalize_uuid(CFG)  # the Hub normalizes the configured ID before it queries
    fake = HubFake(rows={cfg_key: [row(V1)]}, bodies={"r1": [_code(TASKS_BLOCK)]},
                   schemas={cfg_key: {"properties": {hub._VERSION_PROP: {"type": "date"}, hub._REPORT_PROP: {"type": "rich_text"},
                                                     "Errors": {"type": "rich_text"}, "Warnings": {"type": "rich_text"},
                                                     "Status": {"status": {"options": [{"name": "Active"}]}}}}})

    def user_sets_a_new_version():
        fake.rows[cfg_key] = [row(V2, edited="t2")]
        fake.bodies["r1"] = [_code(TASKS_BLOCK.replace(TASKS, TASKS2))]

    def user_pauses_the_row():
        fake.rows[cfg_key] = [row(V2, status="On hold", edited="t3")]

    seen = []
    _run_main(monkeypatch, tmp_path, fake, f'config_hub_data_source_id = "{CFG}"\n',
              [user_sets_a_new_version, lambda: seen.append(set(automations._data_source_configs)), user_pauses_the_row,
               lambda: seen.append(set(automations._data_source_configs))])
    assert clean_state == [TASKS, TASKS2]                      # the new data source got its init pass
    assert seen == [{TASKS2}, set()]                           # old one stopped; pausing the row stopped the rest
    written = [props for page, props in fake.updates if page == "r1"]
    reports = [p["Report"]["rich_text"][0]["text"]["content"] for p in written if "Report" in p and p["Report"]["rich_text"]]
    assert any("Governance: OK" in r and "config applied" in r for r in reports)
    assert any("not Active, nothing running" in r for r in reports)
