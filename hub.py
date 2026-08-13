"""
hub.py
Automation Hub integration: finds or creates the Hub's 3 child databases on startup,
persists their IDs to config_hub.toml, and exposes helpers for all Hub-reading/writing paths.

Public API:
    init(client, hub_page_id, daemon_version="")  — call once at daemon startup
    is_available()                                  — True if Hub initialised successfully
    build_snapshot(client)                          — build initial Hub row snapshot + kill-switch state
    poll_for_changes(client, snapshot)              — diff Hub rows; return governance trigger + kill-switch state
    sync_metadata(client)                           — refresh Name/icon on rows from their Database IDs
"""

from __future__ import annotations
import logging
import sys
from datetime import datetime
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from notion_api import NotionClient

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11 detected: install tomli with 'pip install tomli'")

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Config file path
# ------------------------------------------------------------------ #

HUB_CONFIG_PATH = "config_hub.toml"

# ------------------------------------------------------------------ #
#  Database title constants
# ------------------------------------------------------------------ #

_AUTO_DB_TITLE   = "Automated Databases Config"
_RTD_DB_TITLE    = "Recurring Task Definitions (RTD) Config"
_STATUS_DB_TITLE = "Hub Status"

# Hub Status row — bot-created, single row, fully bot-managed.
_STATUS_ROW_NAME = "Hub Status"
_STATUS_ROW_ICON = {"type": "emoji", "emoji": "🌡️"}

# ------------------------------------------------------------------ #
#  Status option sets
# ------------------------------------------------------------------ #

# Automated Databases and RTD Config — full task-board-style options.
_STATUS_OPTIONS_FULL = [
    {"name": "Not started",  "color": "gray",   "group": "To-do"},
    {"name": "Active",       "color": "green",  "group": "In progress"},
    {"name": "On hold",      "color": "yellow", "group": "In progress"},
    {"name": "Discontinued", "color": "red",    "group": "Complete"},
]

# Hub Status — kill switch only.
_STATUS_OPTIONS_HUB = [
    {"name": "Inactive", "color": "red",   "group": "To-do"},
    {"name": "Active",   "color": "green", "group": "In progress"},
    
]

# ------------------------------------------------------------------ #
#  Database schemas
# ------------------------------------------------------------------ #

_AUTO_DB_SCHEMA: dict = {
    "Name":               {"title": {}, "description": "Bot-written: the database title, refreshed each governance pass."},
    "Status":             {
        "status": {"options": _STATUS_OPTIONS_FULL},
        "description": """\
Controls whether the bot monitors this database.
Not started → not yet configured.
Active → bot is monitoring and applying automations.
On hold → temporarily paused.
Discontinued → permanently stopped."""
    },
    "Health":             {
        "formula": {"expression": 'if(not empty(prop("Errors")), "🆘", "") + if(not empty(prop("Warnings")), "⚠️", "")'},
        "description": """\
🆘 = active errors.
⚠️ = active warnings.
Empty = all clear."""
    },
    "Database ID":        {"rich_text": {}, "description": "Notion database ID for the task database to monitor. Bot reloads config when this changes."},
    "Closed Date Property":  {"rich_text": {}, "description": "Property name to stamp with the close timestamp when a task moves to Done (e.g. 'Closed Date'). Empty = disabled."},
    "Reopen Count Property": {"rich_text": {}, "description": "Property name to increment each time a task is reopened (e.g. 'Reopen Count'). Requires Closed Date Property to be set. Empty = disabled."},
    "Update Count Properties":{"rich_text": {}, "description": "Comma-separated property names to count updates on (e.g. 'Due Date'). Bot increments '[Property] Update Count' each time the value changes."},
    "First Value Properties": {"rich_text": {}, "description": "Comma-separated property names to snapshot on first observation (e.g. 'Due Date, Status'). Bot writes 'First [Property]' once and never overwrites it."},
    "Errors":             {"rich_text": {}, "description": "Bot-written. Active errors for this database — cleared automatically when resolved."},
    "Warnings":           {"rich_text": {}, "description": "Bot-written. Active warnings for this database — cleared automatically when resolved."},
}
_AUTO_DB_DESC = (
    "Each row is a Notion task database the bot monitors. "
    "Enter the Database ID, configure automation properties, then set Status → Active. "
    "Bot refreshes Name automatically from the database. Empty config properties = feature disabled."
)

_RTD_DB_SCHEMA: dict = {
    "Name":                    {"title": {}, "description": "Bot-written: a label for this recurring task setup, refreshed each governance pass."},
    "Status":                  {
        "status": {"options": _STATUS_OPTIONS_FULL},
        "description": """\
Controls whether the bot runs recurring task governance for this setup.
Not started → not yet configured.
Active → governance enabled.
On hold → temporarily paused.
Discontinued → permanently stopped."""
    },
    "Health":                  {
        "formula": {"expression": 'if(not empty(prop("Errors")), "🆘", "") + if(not empty(prop("Warnings")), "⚠️", "")'},
        "description": """\
🆘 = active errors.
⚠️ = active warnings.
Empty = all clear."""
    },
    "Definitions DB":          {"rich_text": {}, "description": "Notion database ID for the Recurring Task Definitions (RTD) database. Bot also writes the database name here for readability."},
    "Tasks DB":                {"rich_text": {}, "description": "Notion database ID for the task database where recurring tasks are created. Bot also writes the database name here for readability."},
    "Inherited Properties from Prev Task": {"rich_text": {}, "description": "Comma-separated property names to copy from the previous task occurrence when a new recurring task is created (e.g. 'Priority, Effort, Area'). Leave blank to inherit nothing. The new task's title always comes from the RTD Name — it cannot be inherited from the previous task. Validated against the live schema at startup — typos or renamed properties are flagged as warnings."},
    "non_completion_statuses": {"rich_text": {}, "description": "Comma-separated status names treated as failed or skipped attempts rather than completions (e.g. 'Cancelled, Skipped'). If blank, falls back to the built-in list: Cancelled, Canceled, Skipped, Missed, Ignored, Abandoned."},
    "Errors":                  {"rich_text": {}, "description": "Bot-written. Active errors from recurring task governance — cleared automatically when resolved."},
    "Warnings":                {"rich_text": {}, "description": "Bot-written. Active warnings from recurring task governance — cleared automatically when resolved."},
}
_RTD_DB_DESC = (
    "Each row configures recurring task governance for one RTD database + task database pair. "
    "Enter Definitions DB and Tasks DB IDs, configure properties, then set Status → Active. "
    "Bot refreshes Name automatically from the Definitions DB."
)

_STATUS_DB_SCHEMA: dict = {
    "Name":               {"title": {}, "description": "Static label — 'Hub Status'. Set by the bot on first run."},
    "Status":             {
        "status": {"options": _STATUS_OPTIONS_HUB},
        "description": """\
Kill switch for all automation.
Active → bot runs normally.
Inactive → all automations and governance paused. Bot resumes and runs governance when set back to Active."""
    },
    "Health":             {
        "formula": {"expression": """\
if(
	(
		!empty(prop("Errors"))
		or if(
			empty(prop("Last Poll")),
			false,
			dateBetween(now(), prop("Last Poll"), "hours") > 8)
		) or if(
			empty(prop("Last Governance Run")),
			false,
			dateBetween(now(), prop("Last Governance Run"), "hours") > 28
	),
	"🆘",
	""
) + if(
	(
		!empty(prop("Warnings"))
		or if(
			empty(prop("Last Poll")),
			false,
			(
				dateBetween(now(), prop("Last Poll"), "hours") > 2
				) and (
				dateBetween(now(), prop("Last Poll"), "hours") <= 8
			)
		)
	) or if(
		empty(prop("Last Governance Run")),
		false,
		(
			dateBetween(now(), prop("Last Governance Run"), "hours") > 24
		) and (
			dateBetween(now(), prop("Last Governance Run"), "hours") <= 28
		)
	) or empty(prop("Last Poll"))
		or empty(prop("Last Governance Run")
	),
	"⚠️",
	""
)"""},
        "description": """\
🆘 = active errors, poll >8h stale, or governance >28h stale.
⚠️ = active warnings, poll 2–8h stale, governance 24–28h stale, or poll/governance dates are empty.
Empty = all clear."""
    },
    "Last Poll":           {"date": {}, "description": "Bot-written: timestamp of the most recent successful poll cycle."},
    "Last Governance Run": {"date": {}, "description": "Bot-written: timestamp of the most recent governance pass."},
    "Governance Trigger":  {"select": {"options": [
        {"name": "server-reset"},
        {"name": "scheduled"},
        {"name": "force-governance"},
    ]}, "description": "Bot-written: what triggered the last governance run — server-reset (startup), scheduled (daily cron), or force-governance (Force Governance checkbox)."},
    "Daemon Uptime":   {"rich_text": {}, "description": "Bot-written: elapsed time since the daemon process started (e.g. '3d 4h 12m')."},
    "Machine Uptime":  {"rich_text": {}, "description": "Bot-written: elapsed time since the host machine booted. Read from /proc/uptime on Linux. Blank if unavailable."},
    "Version":         {"rich_text": {}, "description": "Bot-written: the running daemon version."},
    "Errors":          {"rich_text": {}, "description": "Bot-written. Active daemon-level errors — cleared automatically when resolved."},
    "Warnings":        {"rich_text": {}, "description": "Bot-written. Active daemon-level warnings — cleared automatically when resolved."},
}
_STATUS_DB_DESC = (
    "Daemon health and last-run details. Written by the bot on each governance pass. "
    "Set Status → Inactive to pause all automation immediately. "
    "poll_interval, week_start, and day_start_hour are set in config.toml on the server "
    "and require a daemon restart to change."
)

# ------------------------------------------------------------------ #
#  Module state — set by init()
# ------------------------------------------------------------------ #

_auto_db_id:        str | None = None
_rtd_db_id:         str | None = None
_status_db_id:      str | None = None
_hub_page_id:       str | None = None
_hub_status_row_id: str | None = None
_available:         bool = False


def is_available() -> bool:
    """Return True if the Hub was successfully initialised. Check before any Hub write."""
    return _available


# ------------------------------------------------------------------ #
#  config_hub.toml read / write
# ------------------------------------------------------------------ #

def _load_hub_ids() -> dict:
    """Read config_hub.toml and return its [databases] section, or {} on any failure."""
    try:
        with open(HUB_CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        return data.get("databases", {})
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not read {HUB_CONFIG_PATH}: {e} — will re-detect Hub databases.")
        return {}


def _save_hub_ids(auto_db_id: str, rtd_db_id: str, status_db_id: str) -> None:
    """Write the 3 Hub child database IDs to config_hub.toml."""
    content = (
        "# Written by daemon at startup. Do not edit unless re-syncing after a database was\n"
        "# deleted or recreated in Notion. To re-sync: shut down the daemon, update the IDs\n"
        "# below to match the correct Notion database IDs, then restart.\n"
        "[databases]\n"
        f'automated_databases = "{auto_db_id}"\n'
        f'rtd_config          = "{rtd_db_id}"\n'
        f'hub_status          = "{status_db_id}"\n'
    )
    try:
        with open(HUB_CONFIG_PATH, "w") as f:
            f.write(content)
        logger.info(f"Hub database IDs saved to {HUB_CONFIG_PATH}.")
    except Exception as e:
        logger.error(f"Could not write {HUB_CONFIG_PATH}: {e}")


# ------------------------------------------------------------------ #
#  Hub page block scanning
# ------------------------------------------------------------------ #

def _find_existing_databases(client: "NotionClient", hub_page_id: str) -> dict[str, str]:
    """
    Scan the Hub page's children for child_database blocks.
    Returns {database_title: database_id}. In Notion, a child_database block's ID
    is the database's ID, so no separate lookup is needed.
    """
    try:
        blocks = client.get_blocks(hub_page_id)
    except Exception as e:
        logger.warning(f"Could not read Hub page blocks: {e}")
        return {}
    return {
        b.get("child_database", {}).get("title", ""): b["id"]
        for b in blocks
        if b.get("type") == "child_database"
    }


# ------------------------------------------------------------------ #
#  Config parsing helpers
# ------------------------------------------------------------------ #

def _parse_csv(text: str) -> list[str]:
    """Parse a comma-separated property list from a Hub text field.

    Splits on commas, strips whitespace per token, drops empty tokens.
    Handles double-quoted tokens for names that contain commas.
    Returns [] for blank/empty input.
    """
    text = (text or "").strip()
    if not text:
        return []
    tokens: list[str] = []
    current = ""
    in_quotes = False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            token = current.strip()
            if token:
                tokens.append(token)
            current = ""
        else:
            current += ch
    token = current.strip()
    if token:
        tokens.append(token)
    return tokens


def _is_valid_db_id(text: str) -> bool:
    """Return True if text (after stripping dashes) is a valid 32-char hex Notion ID."""
    stripped = (text or "").replace("-", "").strip()
    if len(stripped) != 32:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in stripped)


# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #

def _get_prop_text(row: dict, field: str) -> str:
    """Extract plain text from a rich_text or title property of a Hub row."""
    prop = row.get("properties", {}).get(field, {})
    parts = prop.get("rich_text") or prop.get("title") or []
    return "".join(t.get("plain_text", "") for t in parts).strip()


def _row_status(row: dict) -> str:
    """Return the Status option name for a Hub row, or '' if not set."""
    return (row.get("properties", {}).get("Status", {}).get("status", {}) or {}).get("name", "")


# ------------------------------------------------------------------ #
#  Hub Status row management
# ------------------------------------------------------------------ #

def _ensure_hub_status_row(client: "NotionClient") -> None:
    """Create the 'Hub Status' row in the Hub Status database if it doesn't exist yet."""
    global _hub_status_row_id
    if not _available or _status_db_id is None:
        return
    try:
        rows = client.query_database(_status_db_id)
        if rows:
            _hub_status_row_id = rows[0]["id"]
            logger.info(f"Hub Status row found ({_hub_status_row_id}).")
            return
        page = client.create_page(
            _status_db_id,
            {
                "Name":   {"title": [{"text": {"content": _STATUS_ROW_NAME}}]},
                "Status": {"status": {"name": "Inactive"}},
            },
            icon=_STATUS_ROW_ICON,
        )
        _hub_status_row_id = page["id"]
        logger.info(f"Hub: created '{_STATUS_ROW_NAME}' row ({_hub_status_row_id}).")
    except Exception as e:
        logger.warning(f"Hub: could not ensure Hub Status row: {e}")


# ------------------------------------------------------------------ #
#  Metadata sync
# ------------------------------------------------------------------ #

def sync_metadata(client: "NotionClient") -> None:
    """
    Read Automated Databases and RTD Config rows that have Database IDs set,
    fetch the database name and icon from Notion, and write them back to the Hub rows.
    Runs for all rows regardless of Status — helps users confirm IDs before activating.
    Call after init() and after each governance pass.
    """
    if not _available:
        return
    _sync_auto_db_metadata(client)
    _sync_rtd_config_metadata(client)


def _sync_auto_db_metadata(client: "NotionClient") -> None:
    try:
        rows = client.query_database(_auto_db_id)
    except Exception as e:
        logger.warning(f"Hub: could not read Automated Databases rows: {e}")
        return
    for row in rows:
        db_id = _get_prop_text(row, "Database ID").replace("-", "")
        if not db_id:
            continue
        try:
            db = client.get_database(db_id)
            name = "".join(t.get("plain_text", "") for t in db.get("title", []))
            icon = db.get("icon")
            props = {"Name": {"title": [{"text": {"content": name}}]}} if name else {}
            client.update_page_properties(row["id"], props, icon=icon)
            logger.debug(f"Hub: synced Automated DB '{name}' ({db_id}).")
        except Exception as e:
            logger.warning(f"Hub: could not sync metadata for database '{db_id}': {e}")


def _sync_rtd_config_metadata(client: "NotionClient") -> None:
    try:
        rows = client.query_database(_rtd_db_id)
    except Exception as e:
        logger.warning(f"Hub: could not read RTD Config rows: {e}")
        return
    for row in rows:
        defs_db_id = _get_prop_text(row, "Definitions DB").replace("-", "")
        if not defs_db_id:
            continue
        try:
            db = client.get_database(defs_db_id)
            name = "".join(t.get("plain_text", "") for t in db.get("title", []))
            icon = db.get("icon")
            props = {"Name": {"title": [{"text": {"content": name}}]}} if name else {}
            client.update_page_properties(row["id"], props, icon=icon)
            logger.debug(f"Hub: synced RTD Config '{name}' ({defs_db_id}).")
        except Exception as e:
            logger.warning(f"Hub: could not sync RTD Config metadata for definitions DB '{defs_db_id}': {e}")


# ------------------------------------------------------------------ #
#  Hub polling — kill switch and governance triggers
# ------------------------------------------------------------------ #

def build_snapshot(client: "NotionClient") -> tuple[dict[str, dict], bool]:
    """
    Query all Hub database rows and build the initial change-detection snapshot.
    Returns (snapshot, hub_is_active).
    Call once after init() — existing Active rows do NOT trigger governance here.
    Raises on network/API errors.
    """
    if not _available:
        return {}, True
    snapshot: dict[str, dict] = {}
    hub_is_active = True
    for db_id, label in (
        (_auto_db_id,   "Automated Databases"),
        (_rtd_db_id,    "RTD Config"),
        (_status_db_id, "Hub Status"),
    ):
        rows = client.query_database(db_id)
        for row in rows:
            snapshot[row["id"]] = row
        if label == "Hub Status" and rows:
            if not any(_row_status(r) == "Active" for r in rows):
                hub_is_active = False
    logger.info(
        f"Hub snapshot built ({len(snapshot)} row(s)). "
        f"Kill switch: {'armed — automation paused' if not hub_is_active else 'clear'}."
    )
    return snapshot, hub_is_active


def poll_for_changes(
    client: "NotionClient",
    snapshot: dict[str, dict],
) -> tuple[dict[str, dict], bool, bool]:
    """
    Query all Hub database rows, diff against snapshot, and detect status changes.
    Returns (new_snapshot, should_govern, hub_is_active).
    - should_govern: True if any Automated DB or RTD Config row (new or existing)
      transitioned to Active status since the last snapshot.
    - hub_is_active: True if the Hub Status row is Active (or Hub is unavailable).
      False = kill switch engaged; caller should skip automations this cycle.
    Raises on network/API errors — caller must not update its snapshot on error.
    """
    if not _available:
        return snapshot, False, True

    new_snapshot = dict(snapshot)
    should_govern = False
    hub_is_active = True  # default: don't block when Hub Status has no rows

    for db_id, label in (
        (_auto_db_id, "Automated Databases"),
        (_rtd_db_id,  "RTD Config"),
    ):
        rows = client.query_database(db_id)
        for row in rows:
            prev = snapshot.get(row["id"])
            new_snapshot[row["id"]] = row
            new_status = _row_status(row)
            old_status = _row_status(prev) if prev is not None else None
            if new_status == "Active" and old_status != "Active":
                kind = "new" if prev is None else "existing"
                logger.info(
                    f"Hub: {label} row {row['id']} ({kind}) → Active — triggering governance."
                )
                should_govern = True

    status_rows = client.query_database(_status_db_id)
    for row in status_rows:
        prev = snapshot.get(row["id"])
        new_snapshot[row["id"]] = row
        new_status = _row_status(row)
        old_status = _row_status(prev) if prev is not None else None
        if new_status != "Active":
            hub_is_active = False
        if new_status == "Active" and old_status is not None and old_status != "Active":
            logger.info("Hub: kill switch lifted (Hub Status → Active) — triggering governance.")
            should_govern = True

    return new_snapshot, should_govern, hub_is_active


# ------------------------------------------------------------------ #
#  Hub schema validation
# ------------------------------------------------------------------ #

_REQUIRED_AUTO_DB_PROPS: dict[str, str] = {
    "Database ID":             "rich_text",
    "Closed Date Property":    "rich_text",
    "Reopen Count Property":   "rich_text",
    "Update Count Properties": "rich_text",
    "First Value Properties":  "rich_text",
    "Errors":                  "rich_text",
    "Warnings":                "rich_text",
}
_REQUIRED_RTD_DB_PROPS: dict[str, str] = {
    "Definitions DB":                     "rich_text",
    "Tasks DB":                           "rich_text",
    "Inherited Properties from Prev Task":"rich_text",
    "non_completion_statuses":            "rich_text",
    "Errors":                             "rich_text",
    "Warnings":                           "rich_text",
}
_REQUIRED_STATUS_DB_PROPS: dict[str, str] = {
    "Last Poll":           "date",
    "Last Governance Run": "date",
    "Errors":              "rich_text",
    "Warnings":            "rich_text",
}


def validate_hub_schema(client: "NotionClient") -> None:
    """
    Validate that the 3 Hub child databases have the expected schema.
    Logs ERROR for missing/wrong-type properties or missing "Active" Status option.
    Logs WARNING if Hub Status has multiple rows.
    Call once at daemon startup after init().
    """
    if not _available:
        return

    checks = [
        (_auto_db_id,   "Automated Databases", _REQUIRED_AUTO_DB_PROPS,  True),
        (_rtd_db_id,    "RTD Config",           _REQUIRED_RTD_DB_PROPS,   True),
        (_status_db_id, "Hub Status",           _REQUIRED_STATUS_DB_PROPS, True),
    ]
    for db_id, label, required_props, needs_active_option in checks:
        try:
            db = client.get_database(db_id)
        except Exception as e:
            logger.error(f"Hub schema check: could not read '{label}' database: {e}")
            continue

        props = db.get("properties", {})

        # Check Status property has "Active" option.
        if needs_active_option:
            status_prop = props.get("Status", {})
            options = status_prop.get("status", {}).get("options", [])
            names = {o.get("name", "") for o in options}
            if "Active" not in names:
                logger.error(
                    f"Hub schema: '{label}' Status property is missing the 'Active' option. "
                    "The bot cannot gate automations on this database."
                )

        # Check required properties exist with correct type.
        for prop_name, expected_type in required_props.items():
            actual = props.get(prop_name)
            if actual is None:
                logger.error(
                    f"Hub schema: '{label}' is missing required property '{prop_name}'. "
                    "Recreate the Hub databases to restore the expected schema."
                )
            elif actual.get("type") != expected_type:
                logger.error(
                    f"Hub schema: '{label}' property '{prop_name}' has type "
                    f"'{actual.get('type')}' (expected '{expected_type}'). "
                    "Recreate the Hub databases to restore the expected schema."
                )

    # Warn if Hub Status has multiple rows.
    try:
        rows = client.query_database(_status_db_id)
        if len(rows) > 1:
            logger.warning(
                f"Hub: Hub Status database has {len(rows)} rows — expected exactly 1. "
                f"Using the first row ({rows[0]['id']}). Remove extra rows."
            )
    except Exception as e:
        logger.warning(f"Hub schema check: could not count Hub Status rows: {e}")


# ------------------------------------------------------------------ #
#  Hub config reading
# ------------------------------------------------------------------ #

def read_config(client: "NotionClient") -> dict | None:
    """
    Read Active rows from Automated Databases and RTD Config, apply data quality
    checks, and return a structured config dict for the daemon.

    Returns:
        {
          "databases": [{"id", "closed_date_field", "reopen_count_field",
                         "first_value_fields", "update_count_fields"}, ...],
          "rtd_pairs": [{"definitions_db_id", "tasks_db_id", "closed_date_field",
                         "non_completion_statuses", "inherited_fields"}, ...],
        }
    Returns None if Hub is not available.
    On API errors: logs ERROR and returns None.
    """
    if not _available:
        return None

    try:
        auto_rows = client.query_database(_auto_db_id)
        rtd_rows  = client.query_database(_rtd_db_id)
    except Exception as e:
        logger.error(f"Hub: could not read config rows: {e} — daemon will run with zero databases.")
        return None

    # ---- Automated Databases ----------------------------------------
    databases: list[dict] = []
    auto_db_by_tasks_id: dict[str, dict] = {}  # tasks_db_id (normalized) -> db config

    for row in auto_rows:
        row_name = _get_prop_text(row, "Name") or row["id"]
        status   = _row_status(row)
        active   = (status == "Active")

        raw_id = _get_prop_text(row, "Database ID").replace("-", "")

        if not raw_id:
            if active:
                logger.error(f"Hub: Automated Databases row '{row_name}' is Active but Database ID is empty — skipping.")
            else:
                logger.warning(f"Hub: Automated Databases row '{row_name}' has no Database ID — incomplete setup.")
            continue

        if not _is_valid_db_id(raw_id):
            if active:
                logger.error(f"Hub: Automated Databases row '{row_name}' has malformed Database ID '{raw_id}' — skipping.")
            else:
                logger.warning(f"Hub: Automated Databases row '{row_name}' has malformed Database ID '{raw_id}'.")
            continue

        closed_date_field  = _get_prop_text(row, "Closed Date Property") or None
        reopen_count_field = _get_prop_text(row, "Reopen Count Property") or None

        if reopen_count_field and not closed_date_field:
            if active:
                logger.error(
                    f"Hub: Automated Databases row '{row_name}' has Reopen Count Property set "
                    "but Closed Date Property is empty — Reopen Count will not function."
                )
            else:
                logger.warning(
                    f"Hub: Automated Databases row '{row_name}' has Reopen Count Property set "
                    "but Closed Date Property is empty."
                )

        db_cfg = {
            "id":                  raw_id,
            "closed_date_field":   closed_date_field,
            "reopen_count_field":  reopen_count_field,
            "first_value_fields":  _parse_csv(_get_prop_text(row, "First Value Properties")),
            "update_count_fields": _parse_csv(_get_prop_text(row, "Update Count Properties")),
        }
        auto_db_by_tasks_id[raw_id] = db_cfg

        if active:
            databases.append(db_cfg)

    # ---- RTD Config -------------------------------------------------
    rtd_pairs_raw: list[dict] = []  # Active pairs before duplicate check
    all_rtd_tasks_ids: list[str] = []  # all rows (any status) for duplicate warn

    for row in rtd_rows:
        row_name = _get_prop_text(row, "Name") or row["id"]
        status   = _row_status(row)
        active   = (status == "Active")

        defs_id  = _get_prop_text(row, "Definitions DB").replace("-", "")
        tasks_id = _get_prop_text(row, "Tasks DB").replace("-", "")

        missing = []
        if not defs_id:
            missing.append("Definitions DB")
        elif not _is_valid_db_id(defs_id):
            missing.append(f"Definitions DB (malformed: '{defs_id}')")
            defs_id = ""
        if not tasks_id:
            missing.append("Tasks DB")
        elif not _is_valid_db_id(tasks_id):
            missing.append(f"Tasks DB (malformed: '{tasks_id}')")
            tasks_id = ""

        if missing:
            label = "Error" if active else "Warning"
            lvl = logger.error if active else logger.warning
            lvl(f"Hub: RTD Config row '{row_name}' missing/invalid: {', '.join(missing)} — {'skipping' if active else 'incomplete setup'}.")
            if active:
                continue

        if active and defs_id and tasks_id:
            # Look up closed_date_field from the matching Automated Databases row.
            closed_date_field = (auto_db_by_tasks_id.get(tasks_id) or {}).get("closed_date_field") or "Closed Date"

            non_comp_raw = _parse_csv(_get_prop_text(row, "non_completion_statuses"))
            inherited_raw = _parse_csv(_get_prop_text(row, "Inherited Properties from Prev Task"))

            rtd_pairs_raw.append({
                "definitions_db_id":       defs_id,
                "tasks_db_id":             tasks_id,
                "closed_date_field":       closed_date_field,
                "non_completion_statuses": non_comp_raw,   # list[str]; [] = use built-in defaults
                "inherited_fields":        inherited_raw,  # list[str]; [] = inherit nothing
                "_row_name":               row_name,
            })
            all_rtd_tasks_ids.append(tasks_id)
        elif not active and tasks_id:
            all_rtd_tasks_ids.append(tasks_id)

    # ---- Duplicate pair detection -----------------------------------
    active_tasks_ids: dict[str, str] = {}   # tasks_db_id -> row_name of first Active pair
    active_defs_ids:  dict[str, str] = {}   # defs_db_id  -> row_name of first Active pair
    duplicate_tasks: set[str] = set()
    duplicate_defs:  set[str] = set()

    for pair in rtd_pairs_raw:
        t_id = pair["tasks_db_id"]
        d_id = pair["definitions_db_id"]
        if t_id in active_tasks_ids:
            logger.error(
                f"Hub: RTD Config rows '{active_tasks_ids[t_id]}' and '{pair['_row_name']}' "
                f"both reference the same Tasks DB ({t_id}). Both pairs disabled. "
                "Each tasks database must have exactly one Active RTD Config row."
            )
            duplicate_tasks.add(t_id)
        else:
            active_tasks_ids[t_id] = pair["_row_name"]

        if d_id in active_defs_ids:
            logger.error(
                f"Hub: RTD Config rows '{active_defs_ids[d_id]}' and '{pair['_row_name']}' "
                f"both reference the same Definitions DB ({d_id}). Both pairs disabled."
            )
            duplicate_defs.add(d_id)
        else:
            active_defs_ids[d_id] = pair["_row_name"]

    rtd_pairs = [
        {k: v for k, v in p.items() if k != "_row_name"}
        for p in rtd_pairs_raw
        if p["tasks_db_id"] not in duplicate_tasks and p["definitions_db_id"] not in duplicate_defs
    ]

    # Warn about inactive rows that share IDs with active pairs.
    for row in rtd_rows:
        if _row_status(row) == "Active":
            continue
        tasks_id = _get_prop_text(row, "Tasks DB").replace("-", "")
        defs_id  = _get_prop_text(row, "Definitions DB").replace("-", "")
        row_name = _get_prop_text(row, "Name") or row["id"]
        if tasks_id and tasks_id in active_tasks_ids:
            logger.warning(
                f"Hub: Inactive RTD Config row '{row_name}' shares Tasks DB ({tasks_id}) "
                f"with active pair '{active_tasks_ids[tasks_id]}'."
            )
        if defs_id and defs_id in active_defs_ids:
            logger.warning(
                f"Hub: Inactive RTD Config row '{row_name}' shares Definitions DB ({defs_id}) "
                f"with active pair '{active_defs_ids[defs_id]}'."
            )

    logger.info(
        f"Hub config read: {len(databases)} task database(s), {len(rtd_pairs)} RTD pair(s)."
    )
    return {"databases": databases, "rtd_pairs": rtd_pairs}


# ------------------------------------------------------------------ #
#  init
# ------------------------------------------------------------------ #

def init(client: "NotionClient", hub_page_id: str, daemon_version: str = "") -> bool:
    """
    Find or create the Hub's 3 child databases. Saves their IDs to config_hub.toml.
    Creates the 'Hub Status' row if it doesn't exist.
    Returns True if Hub is ready, False if setup failed (daemon continues without Hub).
    Call once at daemon startup after the NotionClient is created.
    daemon_version is appended to the database description at creation time so users
    can identify which databases the bot created and when.
    """
    global _auto_db_id, _rtd_db_id, _status_db_id, _hub_page_id, _available

    _hub_page_id = hub_page_id

    try:
        # Fast path: config_hub.toml has all 3 IDs.
        # Verify they're reachable. On intentional removal (404, trashed, wrong parent),
        # fall through to the scan path. On network errors, mark Hub unavailable and
        # return immediately — don't scan or create databases for a transient failure.
        ids = _load_hub_ids()
        _do_scan = False
        if all(ids.get(k) for k in ("automated_databases", "rtd_config", "hub_status")):
            try:
                db = client.get_database(ids["automated_databases"])
                if db.get("in_trash") or db.get("archived"):
                    raise ValueError("database is trashed or archived")
                parent = db.get("parent", {})
                if (parent.get("type") != "page_id" or
                        parent.get("page_id", "").replace("-", "") != hub_page_id.replace("-", "")):
                    raise ValueError("database is not a child of the Hub page")
                _auto_db_id   = ids["automated_databases"]
                _rtd_db_id    = ids["rtd_config"]
                _status_db_id = ids["hub_status"]
                _available = True
                logger.info(
                    f"Hub loaded from {HUB_CONFIG_PATH}: "
                    f"auto_db={_auto_db_id}, rtd_db={_rtd_db_id}, status_db={_status_db_id}"
                )
                _ensure_hub_status_row(client)
                return True
            except ValueError as e:
                logger.warning(f"Hub database IDs invalid ({e}) — scanning Hub page for existing databases.")
                _do_scan = True
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    logger.warning("Hub database not found (404) — scanning Hub page for existing databases.")
                    _do_scan = True
                else:
                    logger.warning(
                        f"Hub database verification returned HTTP "
                        f"{e.response.status_code if e.response is not None else '?'}: {e} "
                        "— Hub disabled for this session."
                    )
                    _available = False
                    return False
            except Exception as e:
                logger.warning(
                    f"Hub database verification failed (network error?): {e} "
                    "— Hub disabled for this session. Restart the daemon to retry."
                )
                _available = False
                return False
        else:
            _do_scan = True

        # Scan path: config_hub.toml missing, incomplete, or IDs unreachable.
        # Scans the Hub page for existing databases; creates any that are missing.
        logger.info("Scanning Hub page for existing databases ...")
        existing = _find_existing_databases(client, hub_page_id)

        creation_stamp = (
            f"\n\nCreated by Notion Automator{' v' + daemon_version if daemon_version else ''}"
            f" on {datetime.now().strftime('%Y-%m-%d %H:%M')}."
        )
        created_titles: list[str] = []

        def _get_or_create(title: str, schema: dict, description: str) -> str:
            if title in existing:
                block_id = existing[title]
                try:
                    check = client.get_database(block_id)
                    if check.get("in_trash") or check.get("archived"):
                        logger.info(
                            f"Hub: database '{title}' ({block_id}) is trashed or archived "
                            "— creating a fresh one."
                        )
                    else:
                        logger.info(f"Hub: found existing database '{title}' ({block_id}).")
                        return block_id
                except Exception as e:
                    logger.warning(
                        f"Hub: could not verify database '{title}' ({block_id}): {e} "
                        "— creating a fresh one."
                    )
            logger.info(f"Hub: creating database '{title}' ...")
            db = client.create_database(
                hub_page_id, title, schema,
                description=description + creation_stamp,
                is_inline=True,
            )
            db_id = db["id"]
            logger.info(f"Hub: created '{title}' ({db_id}).")
            created_titles.append(title)
            return db_id

        _auto_db_id   = _get_or_create(_AUTO_DB_TITLE,   _AUTO_DB_SCHEMA,   _AUTO_DB_DESC)
        _rtd_db_id    = _get_or_create(_RTD_DB_TITLE,    _RTD_DB_SCHEMA,    _RTD_DB_DESC)
        _status_db_id = _get_or_create(_STATUS_DB_TITLE, _STATUS_DB_SCHEMA, _STATUS_DB_DESC)

        _save_hub_ids(_auto_db_id, _rtd_db_id, _status_db_id)
        _available = True

        if created_titles:
            logger.warning(
                "=" * 60 + "\n"
                f"  HUB SETUP: {len(created_titles)} new database(s) created:\n"
                + "".join(f"    - {t}\n" for t in created_titles)
                + "  Add rows to Automated Databases and RTD Config, set Database IDs,\n"
                "  then set Status → Active when ready.\n"
                + "=" * 60
            )

        logger.info(
            f"Hub ready: auto_db={_auto_db_id}, rtd_db={_rtd_db_id}, status_db={_status_db_id}"
        )
        _ensure_hub_status_row(client)
        return True

    except Exception as e:
        logger.error(f"Hub initialisation failed: {e} — Hub features disabled for this session.")
        _available = False
        return False
