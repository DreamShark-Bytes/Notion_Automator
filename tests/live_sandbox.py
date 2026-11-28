"""Live check of the Configuration Hub against a real Notion sandbox page. It connects to Notion.

Run it yourself, only when you want it (pytest does not collect it, and nothing runs it automatically):

    venv/bin/python tests/live_sandbox.py

Settings (read by this script, never printed): `sandbox_page_id` in config.toml and `sandbox_token` in
config_secrets.toml. It refuses to run if either is missing or the sandbox token equals the normal token.

What it does: creates throwaway data sources under the sandbox page (a config data source and a task data
source), runs the Hub paths (create the example row, read, Report, Config Version, a broken apply, pause)
and archives only what it created, tracked by the IDs Notion returned. It never edits, moves or deletes the
callout at the top of the sandbox page (a warning is logged if the first block is not a callout).
"""
import logging
import os
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import hub  # noqa: E402

TRASH_VERSION = "2026-03-11"  # first Notion version where `archived` became `in_trash` (same as notion_api.TRASH_VERSION)


class SandboxRefused(Exception):
    """The sandbox settings are missing or unsafe. The message names the problem, never a value."""


def load_settings(config_path: str, secrets_path: str) -> tuple[dict, str, str]:
    """Return (config.toml dict, sandbox page ID, sandbox token) or raise SandboxRefused."""
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    secrets = hub.load_secrets(secrets_path)
    page_id = str(cfg.get("sandbox_page_id") or "").strip()
    sandbox_token = str(secrets.get("sandbox_token") or "").strip()
    if not page_id:
        raise SandboxRefused("`sandbox_page_id` is not set in config.toml.")
    if not sandbox_token:
        raise SandboxRefused("`sandbox_token` is not set in config_secrets.toml.")
    normal_token = hub.resolve_token(cfg, secrets)
    if normal_token and normal_token == sandbox_token:
        raise SandboxRefused("`sandbox_token` equals the normal token. Use a separate integration shared only with the sandbox page.")
    return cfg, hub._normalize_uuid(page_id), sandbox_token


def callout_warning(client, page_id: str) -> str | None:
    """A warning text if the first block of the sandbox page is not a callout, else None. Reads only."""
    blocks = client.get_blocks(page_id)
    if not blocks or blocks[0].get("type") != "callout":
        return "The first block of the sandbox page is not a callout. This script never creates one; add it by hand."
    return None


def trash(client, object_id: str, kind: str) -> None:
    """Move a page or a database to the trash (the API key is `in_trash` from 2026-03-11, `archived` before)."""
    if kind == "page":
        client.archive_page(object_id)
        return
    key = "in_trash" if client.notion_version >= TRASH_VERSION else "archived"
    client._patch(f"/databases/{object_id}", {key: True})


def cleanup(client, created: list[tuple[str, str]]) -> list[str]:
    """Trash exactly the objects this run created, newest first. Returns the failures; keeps going after one."""
    failures = []
    for kind, object_id in reversed(created):
        try:
            trash(client, object_id, kind)
        except Exception as e:
            failures.append(f"{kind} {object_id}: {e}")
    return failures


# ------------------------------------------------------------------ #
#  Throwaway data sources
# ------------------------------------------------------------------ #

def _status_property() -> dict:
    return {"status": {"options": [
        {"name": "Not started", "group": "To-do"},
        {"name": "Active", "group": "In progress"},
        {"name": "On hold", "group": "In progress"},
        {"name": "Done", "group": "Complete"},
    ]}}


def create_data_source(client, page_id: str, title: str, properties: dict, created: list) -> str:
    """Create a database under the sandbox page, record it for cleanup, return its data source ID."""
    db = client.create_database(page_id, title, properties)
    created.append(("database", db["id"]))
    sources = db.get("data_sources") or []
    return sources[0]["id"] if sources else db["id"]


def create_config_source(client, page_id: str, title: str, created: list) -> str:
    return create_data_source(client, page_id, title, {
        "Name": {"title": {}},
        "Status": _status_property(),
        "Errors": {"rich_text": {}},
        "Warnings": {"rich_text": {}},
        hub._REPORT_PROP: {"rich_text": {}},
        hub._VERSION_PROP: {"date": {}},
        hub._VERIFIED_PROP: {"date": {}},
    }, created)


def create_tasks_source(client, page_id: str, created: list) -> str:
    return create_data_source(client, page_id, "Sandbox Tasks", {
        "Name": {"title": {}},
        "Status": _status_property(),
        "Due Date": {"date": {}},
        "Closed Date": {"date": {}},
    }, created)


def _code_block(text: str) -> dict:
    return {"object": "block", "type": "code",
            "code": {"language": "toml", "rich_text": [{"type": "text", "text": {"content": text}}]}}


def _tasks_toml(tasks_ds: str) -> str:
    return f'[[property_automations]]\nname = "sandbox tasks"\ndata_source_id = "{tasks_ds}"\nclosed_date_property = "Closed Date"\n'


def _own_row(client, cfg_ds: str) -> dict:
    rows = [r for r in client.query_database(cfg_ds) if hub._get_prop_text(r, "Name") == hub._EXAMPLE_ROW_NAME]
    return rows[0]


def _set_text_block(client, block_id: str, text: str) -> None:
    client._patch(f"/blocks/{block_id}", {"code": {"language": "toml", "rich_text": [{"type": "text", "text": {"content": text}}]}})


def _version(minutes: int) -> dict:
    stamp = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    return {hub._VERSION_PROP: {"date": {"start": stamp}}}


# ------------------------------------------------------------------ #
#  Scenarios
# ------------------------------------------------------------------ #

class Results:
    def __init__(self):
        self.rows: list[tuple[bool, str]] = []

    def check(self, ok: bool, what: str) -> None:
        self.rows.append((bool(ok), what))
        print(("PASS  " if ok else "FAIL  ") + what)

    @property
    def failed(self) -> int:
        return sum(1 for ok, _ in self.rows if not ok)


class _ErrorCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def scenario_example_row(client, page_id: str, created: list, res: Results) -> None:
    """An empty config data source: init creates the inert example row with an explicit non-Active Status."""
    cfg_ds = create_config_source(client, page_id, "Sandbox Config (empty)", created)
    errors = _ErrorCollector()
    hub.logger.addHandler(errors)
    try:
        res.check(hub.init(client, cfg_ds, daemon_version="sandbox", settle_seconds=0), "init finds the empty config data source and creates a row")
        hub.validate_hub_schema(client)
    finally:
        hub.logger.removeHandler(errors)
    res.check(not errors.records, "schema check of the throwaway config data source logs no errors")
    row = _own_row(client, cfg_ds)
    created.append(("page", row["id"]))
    status = hub._row_status(row)
    res.check(status and status != "Active", f"the new example row has an explicit non-Active Status ({status or 'none'})")
    toml_text, err = hub._read_row_toml(client, row["id"])
    problems = hub.parse_row_config(toml_text)[2] if toml_text else [("error", err)]
    res.check(not any(severity == "error" for severity, _ in problems), "the example row's TOML parses with no errors (a comment-only block is a Warning)")


def scenario_apply_cycle(client, page_id: str, created: list, res: Results) -> None:
    """A prepared Active row: read, Report, Config Version, a broken edit keeps the last good config, pause."""
    cfg_ds = create_config_source(client, page_id, "Sandbox Config", created)
    tasks_ds = create_tasks_source(client, page_id, created)
    row = client.create_page(cfg_ds, {
        "Name": {"title": [{"text": {"content": hub._EXAMPLE_ROW_NAME}}]},
        "Status": {"status": {"name": "Active"}},
        **_version(0),
    })
    created.append(("page", row["id"]))
    client.append_blocks(row["id"], [_code_block(_tasks_toml(tasks_ds))])
    code_id = next(b["id"] for b in client.get_blocks(row["id"]) if b.get("type") == "code")

    res.check(hub.init(client, cfg_ds, daemon_version="sandbox", settle_seconds=0), "init finds the prepared row by its name")
    config = hub.read_config(client)
    res.check(config is not None and [hub._normalize_uuid(d["data_source_id"]) for d in config["databases"]] == [hub._normalize_uuid(tasks_ds)],
              "an Active row's TOML is applied: one task database")
    hub.flush(client)
    current = _own_row(client, cfg_ds)
    res.check(hub._get_prop_text(current, "Errors") == "", "Errors is empty after a clean read")
    report = hub._get_prop_text(current, hub._REPORT_PROP)
    res.check("App: Version sandbox, Started: " in report and "Config:" in report, "the Report column has the App line and a Config line")

    _set_text_block(client, code_id, "this is [not valid toml")
    client.update_page_properties(row["id"], _version(1))
    config = hub.read_config(client)
    hub.flush(client)
    current = _own_row(client, cfg_ds)
    res.check(config is not None and len(config["databases"]) == 1, "a broken edit keeps the last good config running")
    res.check("Apply failed: still running" in hub._get_prop_text(current, "Warnings") and hub._get_prop_text(current, "Errors") == "",
              "the broken edit shows as a Warning and Errors stays empty")

    _set_text_block(client, code_id, _tasks_toml(tasks_ds))
    client.update_page_properties(row["id"], _version(2))
    hub.read_config(client)
    hub.flush(client)
    res.check(hub._get_prop_text(_own_row(client, cfg_ds), "Warnings") == "", "fixing it and setting a newer Config Version clears the Warning")

    schema = client.get_schema(cfg_ds)["properties"]["Status"]["status"]
    paused = hub.pick_inactive_status(schema)
    client.update_page_properties(row["id"], {"Status": {"status": {"name": paused}}})
    config = hub.read_config(client)
    hub.flush(client)
    res.check(config is not None and config["databases"] == [], f"a row set to {paused} applies nothing")
    res.check("not Active" in hub._get_prop_text(_own_row(client, cfg_ds), hub._REPORT_PROP), "the Report says the row is not Active")


def scenario_deleted_row(client, page_id: str, created: list, res: Results) -> None:
    """The Automator's row is trashed: the next read restores it, Active, from the stored copy of the applied config."""
    cfg_ds = create_config_source(client, page_id, "Sandbox Config (deleted row)", created)
    tasks_ds = create_tasks_source(client, page_id, created)
    row = client.create_page(cfg_ds, {
        "Name": {"title": [{"text": {"content": hub._EXAMPLE_ROW_NAME}}]},
        "Status": {"status": {"name": "Active"}},
        **_version(0),
    })
    created.append(("page", row["id"]))
    client.append_blocks(row["id"], [_code_block(_tasks_toml(tasks_ds))])
    hub.init(client, cfg_ds, daemon_version="sandbox", settle_seconds=0)
    hub.read_config(client)
    res.check(hub._read_stored_config() == _tasks_toml(tasks_ds), "a clean apply stores the row's TOML verbatim in the stored config file")

    client.archive_page(row["id"])
    config = None
    for _ in range(5):  # a query right after a create may lag behind
        config = hub.read_config(client)
        if config is not None:
            break
        time.sleep(2)
    new_id = hub._config_row_ids[0]
    created.append(("page", new_id))
    res.check(not hub._same_id(new_id, row["id"]), "a trashed row is recreated")
    res.check(config is not None and [hub._normalize_uuid(d["data_source_id"]) for d in config["databases"]] == [hub._normalize_uuid(tasks_ds)],
              "the restored row is Active and its config is applied")
    hub.flush(client)
    restored = client.get_page(new_id)
    res.check(hub._row_status(restored) == "Active", "the restored row has Status Active")
    res.check("restored from the backup" in hub._get_prop_text(restored, "Warnings"), "the restored row's Warnings says it was restored")
    res.check(hub._read_row_toml(client, new_id)[0] == _tasks_toml(tasks_ds), "the restored row's code block holds the stored TOML")


def run(client, page_id: str) -> int:
    """Run every scenario, always clean up, return the number of failed checks."""
    created: list[tuple[str, str]] = []
    res = Results()
    saved_paths = hub.HUB_CONFIG_PATH, hub.STORED_CONFIG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        # Never touch the real state file or the real stored config copy.
        hub.HUB_CONFIG_PATH = os.path.join(tmp, "config_hub_state.toml")
        hub.STORED_CONFIG_PATH = os.path.join(tmp, "config_automations.toml")
        try:
            warning = callout_warning(client, page_id)
            if warning:
                print(f"WARNING  {warning}")
            for scenario in (scenario_example_row, scenario_apply_cycle, scenario_deleted_row):
                try:
                    scenario(client, page_id, created, res)
                except Exception as e:
                    res.check(False, f"{scenario.__name__} stopped with an error: {type(e).__name__}: {e}")
        finally:
            hub.HUB_CONFIG_PATH, hub.STORED_CONFIG_PATH = saved_paths
            failures = cleanup(client, created)
    print(f"Cleanup: {len(created) - len(failures)} of {len(created)} created objects trashed.")
    for line in failures:
        print(f"CLEANUP FAILED  {line}")
    print(f"{len(res.rows) - res.failed} passed, {res.failed} failed.")
    return res.failed + len(failures)


def main() -> int:
    from notion_api import NotionClient
    try:
        cfg, page_id, token = load_settings(os.path.join(ROOT, "config.toml"), os.path.join(ROOT, hub.SECRETS_FILE_NAME))
    except SandboxRefused as e:
        print(f"Refusing to run: {e}")
        return 2
    client = NotionClient(token=token, notion_version=hub.resolve_notion_version(cfg.get("notion_version")))
    print(f"Sandbox run on API version {client.notion_version}.")
    return 1 if run(client, page_id) else 0


if __name__ == "__main__":
    sys.exit(main())
