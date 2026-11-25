"""
hub.py
Configuration Hub integration. Configuration lives in a Notion data source ("config data source"): one row per
config, with the TOML in a code block in the row's page body. Daemon health lives in a second data source
("status data source"). Both are named by data source ID in config.toml (`config_hub_data_source_id`,
`status_data_source_id`); the user creates them (README: "Setting up the Configuration Hub").

Public API:
    init(client, config_ds_id, status_ds_id="", daemon_version="")  — call once at daemon startup
    is_available()                                                   — True if the Hub initialised successfully
    validate_hub_schema(client)                                      — log problems with either data source's columns
    read_config(client)                                              — parse Active rows into the daemon's config dict
    build_snapshot(client)                                           — initial row snapshot + kill-switch state
    poll_for_changes(client, snapshot)                               — diff rows; governance trigger + kill switch
"""

from __future__ import annotations
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

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
#  Constants
# ------------------------------------------------------------------ #

# Bot-owned file holding the page IDs of the Automator's own rows (other tools share the same data sources).
HUB_CONFIG_PATH = "config_hub_state.toml"
_CONFIG_PAGE_KEY = "automator_config_page_id"
_STATUS_PAGE_KEY = "automator_status_page_id"

# Status row — bot-created, fully bot-managed. Default title (the user may rename the page).
_STATUS_ROW_NAME = "Automator Notion Status"
_STATUS_ROW_ICON = {"type": "emoji", "emoji": "🌡️"}

# Row the bot adds to an empty config data source, as a worked example. Created without a Status so the data source's own default applies (its options vary).
_EXAMPLE_ROW_NAME = "Automator"
_EXAMPLE_ROW_INTRO = (
    "Notion Automator reads the TOML code block below when this row's Status is Active. "
    "Put the data source IDs of your task databases in it (Notion app: open the data source, three-dots menu "
    "(top right) > More > Developer > Copy data source ID). Remove the leading # to switch a line or block on. "
    "The bot never edits the code block; it reports problems in the Errors and Warnings columns."
)
_EXAMPLE_TOML = """\
# One [[property_automations]] block per task data source.
# To switch one off, put a # in front of every line of the block.
# [[property_automations]]
# name = "tasks"
# data_source_id = "paste-the-data-source-id-here"
# closed_date_property = "Closed Date"
# reopen_count_property = "Reopen Count"
# update_count_properties = ["Due Date"]
# first_value_properties = ["Due Date", "Status"]

# Recurring tasks: the definitions data source (RTDs) and the tasks data source they create tasks in.
# [recurring_tasks]
# definitions_data_source_id = "paste-the-data-source-id-here"
# tasks_data_source_id = "paste-the-data-source-id-here"
# inherited_properties = ["Priority", "Effort", "Area"]
# non_completion_statuses = ["Cancelled", "Skipped"]
"""

# Columns the bot needs on each data source (name -> Notion property type).
_REQUIRED_CONFIG_PROPS: dict[str, str] = {
    "Errors":   "rich_text",
    "Warnings": "rich_text",
}
_REQUIRED_STATUS_PROPS: dict[str, str] = {
    "Last Poll":           "date",
    "Last Governance Run": "date",
    "Errors":              "rich_text",
    "Warnings":            "rich_text",
}

# Optional date column on the config data source. The bot writes it after a governance pass that finished without
# errors, for each Active config row that read without errors; the column is never created by the bot.
_VERIFIED_PROP = "Last verified time"

_PROPERTY_AUTOMATION_KEYS = {
    "name", "data_source_id", "closed_date_property", "reopen_count_property",
    "update_count_properties", "first_value_properties",
}
_RECURRING_TASKS_KEYS = {
    "definitions_data_source_id", "tasks_data_source_id", "inherited_properties", "non_completion_statuses",
}

# ------------------------------------------------------------------ #
#  Module state — set by init()
# ------------------------------------------------------------------ #

_config_ds_id:      str | None = None
_status_ds_id:      str | None = None
_config_row_ids:    list[str] = []       # the Automator's own config rows; every other row is ignored
_hub_status_row_id: str | None = None    # the Automator's own status row
_available:         bool = False
_clean_row_ids:     set[str] = set()     # Active own config rows whose last read had no errors
_own_write_times:   dict[str, str] = {}  # row id -> last_edited_time returned by the bot's own write


def is_available() -> bool:
    """Return True if the Hub was successfully initialised. Check before any Hub write."""
    return _available


# ------------------------------------------------------------------ #
#  Config parsing helpers
# ------------------------------------------------------------------ #

def _normalize_uuid(value: str) -> str:
    """Normalize a Notion database ID to standard UUID format (8-4-4-4-12).
    Accepts IDs with no dashes or with dashes in wrong positions."""
    raw = value.replace("-", "")
    if len(raw) == 32 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return value


SECRETS_FILE_NAME = "config_secrets.toml"


def load_secrets(path: str) -> dict:
    """Read the secrets file. A missing file is not an error (returns {}); a malformed one raises
    tomllib.TOMLDecodeError, which names a line and column and lands in the log like a bad config.toml does.
    On POSIX a group- or world-readable file logs a warning (fix: chmod 600); Windows relies on the ACL
    set by install-service.ps1."""
    if not os.path.exists(path):
        return {}
    if os.name != "nt" and os.stat(path).st_mode & 0o077:
        logger.warning(f"{path} is readable by other users. Run: chmod 600 {path}")
    with open(path, "rb") as f:
        return tomllib.load(f)


def resolve_token(cfg: dict, secrets: dict | None = None) -> str | None:
    """Return the Notion integration token, or None if there is none.

    Order: `token` in the secrets file, then `token` in config.toml (deprecated: it keeps a secret in a file that
    is also read for ordinary settings, so it logs a warning). The source is logged at startup; the value never is.
    There is deliberately no environment variable: a stale one would silently override the file.
    """
    from_secrets = str((secrets or {}).get("token") or "").strip()
    if from_secrets:
        logger.info("Notion token source: secrets file")
        return from_secrets
    from_config = str(cfg.get("token") or "").strip()
    if from_config:
        logger.warning(
            f"Notion token source: `token` in config.toml. This is deprecated: move it to {SECRETS_FILE_NAME} "
            "(see config_secrets_example.toml) and remove it from config.toml."
        )
        return from_config
    return None


DEFAULT_NOTION_VERSION = "2026-03-11"
_NOTION_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_notion_version(raw) -> str:
    """Return the Notion API version to use from the config.toml `notion_version` value.

    Missing value -> DEFAULT_NOTION_VERSION. A value that is not a YYYY-MM-DD date is
    rejected with a warning (Notion would answer every request with a 400).
    """
    if raw is None:
        return DEFAULT_NOTION_VERSION
    text = str(raw).strip()
    if _NOTION_VERSION_RE.match(text):
        return text
    logger.warning(f"notion_version '{raw}' is not a YYYY-MM-DD date — using {DEFAULT_NOTION_VERSION}.")
    return DEFAULT_NOTION_VERSION


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


def _error_detail(e: Exception) -> str:
    """The exception text plus Notion's own error message when the exception carries an HTTP response."""
    response = getattr(e, "response", None)
    try:
        message = response.json().get("message", "") if response is not None else ""
    except Exception:
        message = ""
    return f"{e} ({message})" if message else str(e)


def _same_id(a: str, b: str) -> bool:
    return a.replace("-", "").lower() == b.replace("-", "").lower()


def _is_own_config_row(row: dict) -> bool:
    return any(_same_id(row["id"], i) for i in _config_row_ids)


def _find_own_row(rows: list[dict], cached_id: str | None, name: str) -> dict | None:
    """The Automator's row among a shared data source's rows: the saved page ID first, then the default Name."""
    if cached_id:
        for row in rows:
            if _same_id(row["id"], cached_id):
                return row
    for row in rows:
        if _get_prop_text(row, "Name") == name:
            return row
    return None


def _load_hub_ids() -> dict:
    """Read config_hub_state.toml's [databases] section ({key: page ID}), or {} on any failure. IDs are normalized."""
    try:
        with open(HUB_CONFIG_PATH, "rb") as f:
            ids = tomllib.load(f).get("databases", {})
        return {k: _normalize_uuid(v) for k, v in ids.items() if isinstance(v, str)}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Could not read {HUB_CONFIG_PATH}: {e} — will look the Automator's rows up by name.")
        return {}


def _save_hub_ids(config_page_id: str | None, status_page_id: str | None) -> None:
    text = (
        "# Written by the daemon. These are the page IDs of the Automator's own rows in the Configuration Hub\n"
        "# data sources. Other tools' rows in the same data sources are never read or written.\n"
        "# Edit only to point the daemon at a different row: shut down the daemon, change the ID, restart.\n\n"
        "[databases]\n"
        f'{_CONFIG_PAGE_KEY} = "{config_page_id or ""}"\n'
        f'{_STATUS_PAGE_KEY} = "{status_page_id or ""}"\n'
    )
    try:
        with open(HUB_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        logger.warning(f"Could not write {HUB_CONFIG_PATH}: {e}")


def _message_text(messages: list[str]) -> str:
    """Join messages for a Notion rich_text value (2000 characters per text object)."""
    return "\n".join(messages)[:1900]


# ------------------------------------------------------------------ #
#  Row body parsing (pure; no network)
# ------------------------------------------------------------------ #

Problem = tuple[str, str]  # (severity "error" | "warning", message)


def _str_list(value, label: str, problems: list[Problem]) -> list[str]:
    """Return value as a list of non-empty strings, or [] with an error if it is not an array of strings."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(("error", f"{label} must be an array of text values, for example [\"A\", \"B\"]."))
        return []
    return [v.strip() for v in value if v.strip()]


def _opt_str(block: dict, key: str, label: str, problems: list[Problem]) -> str | None:
    value = block.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        problems.append(("error", f"{label}: {key} must be text."))
        return None
    return value.strip() or None


def _data_source_id(block: dict, key: str, label: str, problems: list[Problem]) -> str:
    """Return the block's ID with dashes removed, or '' (with an error) if it is missing or malformed."""
    raw = str(block.get(key) or "").replace("-", "").strip()
    if not raw:
        problems.append(("error", f"{label}: {key} is empty."))
        return ""
    if not _is_valid_db_id(raw):
        problems.append(("error", f"{label}: {key} is not a valid Notion ID ('{raw}')."))
        return ""
    return raw


def parse_row_config(text: str) -> tuple[list[dict], dict | None, list[Problem]]:
    """Parse the TOML from one config row.

    Returns (databases, recurring, problems). `databases` has one dict per valid [[property_automations]] block, in
    the shape the daemon registers ({"id", "closed_date_field", "reopen_count_field", "first_value_fields",
    "update_count_fields"}). `recurring` is the [recurring_tasks] table as {"definitions_db_id", "tasks_db_id",
    "inherited_fields", "non_completion_statuses"}, or None. A block with a missing or malformed ID is skipped and
    reported as an error; the caller downgrades errors to warnings for rows that are not Active.
    """
    problems: list[Problem] = []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [], None, [("error", f"Invalid TOML: {e}")]

    for key in data:
        if key not in ("property_automations", "recurring_tasks"):
            problems.append(("warning", f"Unknown top-level key '{key}' ignored."))

    databases: list[dict] = []
    blocks = data.get("property_automations", [])
    if not isinstance(blocks, list):
        problems.append(("error", "property_automations must be written as [[property_automations]] blocks."))
        blocks = []
    for i, block in enumerate(blocks, start=1):
        label = f"property_automations '{block.get('name')}'" if block.get("name") else f"property_automations #{i}"
        for key in block:
            if key not in _PROPERTY_AUTOMATION_KEYS:
                problems.append(("warning", f"{label}: unknown key '{key}' ignored."))
        ds_id = _data_source_id(block, "data_source_id", label, problems)
        if not ds_id:
            continue
        closed = _opt_str(block, "closed_date_property", label, problems)
        reopen = _opt_str(block, "reopen_count_property", label, problems)
        if reopen and not closed:
            problems.append(("error", f"{label}: reopen_count_property is set but closed_date_property is empty, so Reopen Count will not function."))
        databases.append({
            "id":                  ds_id,
            "closed_date_field":   closed,
            "reopen_count_field":  reopen,
            "first_value_fields":  _str_list(block.get("first_value_properties"), f"{label}: first_value_properties", problems),
            "update_count_fields": _str_list(block.get("update_count_properties"), f"{label}: update_count_properties", problems),
        })

    recurring = None
    table = data.get("recurring_tasks")
    if table is not None:
        label = "recurring_tasks"
        if not isinstance(table, dict):
            problems.append(("error", "recurring_tasks must be a [recurring_tasks] table."))
        else:
            for key in table:
                if key not in _RECURRING_TASKS_KEYS:
                    problems.append(("warning", f"{label}: unknown key '{key}' ignored."))
            defs_id = _data_source_id(table, "definitions_data_source_id", label, problems)
            tasks_id = _data_source_id(table, "tasks_data_source_id", label, problems)
            if defs_id and tasks_id:
                recurring = {
                    "definitions_db_id":       defs_id,
                    "tasks_db_id":             tasks_id,
                    "inherited_fields":        _str_list(table.get("inherited_properties"), f"{label}: inherited_properties", problems),
                    "non_completion_statuses": _str_list(table.get("non_completion_statuses"), f"{label}: non_completion_statuses", problems),
                }
    return databases, recurring, problems


def _read_row_toml(client: "NotionClient", row_id: str) -> tuple[str | None, str | None]:
    """Return (toml_text, None) from the row's single code block, or (None, error message)."""
    blocks = client.get_blocks(row_id)
    codes = [b for b in blocks if b.get("type") == "code"]
    if not codes:
        return None, "No code block found in the row's page body (expected exactly one TOML code block)."
    if len(codes) > 1:
        return None, f"Found {len(codes)} code blocks in the row's page body (expected exactly one)."
    return "".join(t.get("plain_text", "") for t in codes[0]["code"].get("rich_text", [])), None


# ------------------------------------------------------------------ #
#  Status row and example row
# ------------------------------------------------------------------ #

def _ensure_hub_status_row(client: "NotionClient", cached_id: str | None) -> None:
    """Find the Automator's status row (saved ID, then default name) or create it. Sets _hub_status_row_id."""
    global _hub_status_row_id
    _hub_status_row_id = None
    if _status_ds_id is None:
        return
    try:
        row = _find_own_row(client.query_database(_status_ds_id), cached_id, _STATUS_ROW_NAME)
        if row:
            _hub_status_row_id = row["id"]
            logger.info(f"Hub: Automator status row found ({_hub_status_row_id}).")
            return
        page = client.create_page(
            _status_ds_id,
            {"Name": {"title": [{"text": {"content": _STATUS_ROW_NAME}}]}},
            icon=_STATUS_ROW_ICON,
        )
        _hub_status_row_id = page["id"]
        logger.info(f"Hub: created '{_STATUS_ROW_NAME}' row ({_hub_status_row_id}).")
    except Exception as e:
        logger.warning(f"Hub: could not find or create the Automator status row: {_error_detail(e)}")


def _ensure_config_row(client: "NotionClient", cached_id: str | None) -> None:
    """Find the Automator's config row (saved ID, then default name) or create an inert example row.

    The config data source is shared with other tools, so rows that are not the Automator's are left alone."""
    global _config_row_ids
    _config_row_ids = []
    try:
        row = _find_own_row(client.query_database(_config_ds_id), cached_id, _EXAMPLE_ROW_NAME)
        if row:
            _config_row_ids = [row["id"]]
            logger.info(f"Hub: Automator config row found ({row['id']}).")
            return
        page = client.create_page(
            _config_ds_id,
            {"Name": {"title": [{"text": {"content": _EXAMPLE_ROW_NAME}}]}},
        )
        client.append_blocks(page["id"], [
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": _EXAMPLE_ROW_INTRO}}]}},
            {"object": "block", "type": "code",
             "code": {"language": "toml", "rich_text": [{"type": "text", "text": {"content": _EXAMPLE_TOML}}]}},
        ])
        _config_row_ids = [page["id"]]
        logger.warning(
            f"Hub: no Automator config row was found, so '{_EXAMPLE_ROW_NAME}' was created ({page['id']}). "
            "Fill in its TOML code block, then set its Status to Active."
        )
    except Exception as e:
        logger.warning(f"Hub: could not find or create the Automator config row: {_error_detail(e)}")


def _write_row_messages(client: "NotionClient", row: dict, errors: list[str], warnings: list[str]) -> None:
    """Write the Errors and Warnings columns of a config row, only when the text changed.

    A write updates the row's last_edited_time, which change detection watches, so unchanged text is never rewritten.
    """
    props = {}
    for column, messages in (("Errors", errors), ("Warnings", warnings)):
        text = _message_text(messages)
        if text != _get_prop_text(row, column):
            props[column] = {"rich_text": [{"text": {"content": text}}] if text else []}
    if not props:
        return
    try:
        _remember_own_write(row["id"], client.update_page_properties(row["id"], props))
    except Exception as e:
        logger.warning(f"Hub: could not write Errors/Warnings to config row {row['id']}: {e}")


def _remember_own_write(row_id: str, page: dict | None) -> None:
    """Record the last_edited_time a bot write produced so change detection does not mistake it for a user edit."""
    edited = (page or {}).get("last_edited_time")
    if edited:
        _own_write_times[row_id] = edited


def mark_verified(client: "NotionClient", governance_ok: bool) -> None:
    """
    Stamp the optional "Last verified time" column on each Active own config row that read without errors.
    Call after a governance pass; nothing is written when governance reported a failure, so a stale date shows
    a row the bot has not confirmed lately. Rows without the column are skipped.
    """
    if not _available or not governance_ok or not _clean_row_ids:
        return
    try:
        rows = [r for r in client.query_database(_config_ds_id) if _is_own_config_row(r)]
    except Exception as e:
        logger.warning(f"Hub: could not read config rows to write '{_VERIFIED_PROP}': {e}")
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        prop = row.get("properties", {}).get(_VERIFIED_PROP)
        if row["id"] not in _clean_row_ids or _row_status(row) != "Active" or not prop or prop.get("type") != "date":
            continue
        try:
            _remember_own_write(row["id"], client.update_page_properties(row["id"], {_VERIFIED_PROP: {"date": {"start": now}}}))
        except Exception as e:
            logger.warning(f"Hub: could not write '{_VERIFIED_PROP}' to config row {row['id']}: {e}")


# ------------------------------------------------------------------ #
#  Hub polling — kill switch and governance triggers
# ------------------------------------------------------------------ #

def _own_status_row(client: "NotionClient") -> dict | None:
    if not (_status_ds_id and _hub_status_row_id):
        return None
    return next((r for r in client.query_database(_status_ds_id) if _same_id(r["id"], _hub_status_row_id)), None)


def build_snapshot(client: "NotionClient") -> tuple[dict[str, dict], bool]:
    """
    Build the initial change-detection snapshot of the Automator's own Hub rows.
    Returns (snapshot, hub_is_active).
    Call once after init() — existing Active rows do NOT trigger governance here.
    Raises on network/API errors.
    """
    if not _available:
        return {}, True
    snapshot = {r["id"]: r for r in client.query_database(_config_ds_id) if _is_own_config_row(r)}
    status = _own_status_row(client)
    hub_is_active = status is None or _row_status(status) == "Active"
    if status:
        snapshot[status["id"]] = status
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
    Query the Automator's own Hub rows, diff against snapshot, and detect changes. Other tools' rows in the
    same data sources are ignored.
    Returns (new_snapshot, should_govern, hub_is_active).
    - should_govern: True if a config row became Active, left Active (its automations must stop), or an Active
      config row was edited (a body edit changes the row's last_edited_time) since the last snapshot. The caller
      re-reads the config and applies it, then runs governance.
    - hub_is_active: True if the Automator's status row is Active (or there is no status data source / row).
      False = kill switch engaged; caller should skip automations this cycle.
    Raises on network/API errors — caller must not update its snapshot on error.
    """
    if not _available:
        return snapshot, False, True

    new_snapshot = dict(snapshot)
    should_govern = False
    hub_is_active = True  # default: don't block when there is no status row

    for row in client.query_database(_config_ds_id):
        if not _is_own_config_row(row):
            continue
        prev = snapshot.get(row["id"])
        new_snapshot[row["id"]] = row
        if _row_status(row) != "Active":
            if prev is not None and _row_status(prev) == "Active":
                logger.info(f"Hub: config row {row['id']} left Active — reloading config (its automations stop).")
                should_govern = True
            continue
        if prev is None or _row_status(prev) != "Active":
            logger.info(f"Hub: config row {row['id']} ({'new' if prev is None else 'existing'}) → Active — triggering governance.")
            should_govern = True
        elif (row.get("last_edited_time") != prev.get("last_edited_time")
              and row.get("last_edited_time") != _own_write_times.get(row["id"])):
            logger.info(f"Hub: Active config row {row['id']} was edited — triggering governance.")
            should_govern = True

    row = _own_status_row(client)
    if row:
        prev = snapshot.get(row["id"])
        new_snapshot[row["id"]] = row
        new_status = _row_status(row)
        old_status = _row_status(prev) if prev is not None else None
        if new_status != "Active":
            hub_is_active = False
        if new_status == "Active" and old_status is not None and old_status != "Active":
            logger.info("Hub: kill switch lifted (status row → Active) — triggering governance.")
            should_govern = True

    return new_snapshot, should_govern, hub_is_active


# ------------------------------------------------------------------ #
#  Hub schema validation
# ------------------------------------------------------------------ #

def validate_hub_schema(client: "NotionClient") -> None:
    """
    Validate that the config and status data sources have the expected columns.
    Logs ERROR for missing/wrong-type properties or a missing "Active" Status option.
    Call once at daemon startup after init().
    """
    if not _available:
        return

    checks = [(_config_ds_id, "Config", _REQUIRED_CONFIG_PROPS)]
    if _status_ds_id:
        checks.append((_status_ds_id, "Hub Status", _REQUIRED_STATUS_PROPS))
    for ds_id, label, required_props in checks:
        try:
            schema = client.get_schema(ds_id)
        except Exception as e:
            logger.error(f"Hub schema check: could not read the '{label}' data source: {e}")
            continue

        props = schema.get("properties", {})
        names = {o.get("name", "") for o in props.get("Status", {}).get("status", {}).get("options", [])}
        if "Active" not in names:
            logger.error(
                f"Hub schema: '{label}' Status property is missing the 'Active' option. "
                "The bot cannot gate automations on this data source."
            )
        if label == "Config":
            verified = props.get(_VERIFIED_PROP)
            if verified is None:
                logger.info(f"Hub schema: optional '{_VERIFIED_PROP}' (date) is not on the config data source; no verification time is written.")
            elif verified.get("type") != "date":
                logger.error(
                    f"Hub schema: '{_VERIFIED_PROP}' has type '{verified.get('type')}' (expected 'date'). "
                    "Notion does not let the bot write a 'Last edited time' property; use a Date property."
                )
        for prop_name, expected_type in required_props.items():
            actual = props.get(prop_name)
            if actual is None:
                logger.error(
                    f"Hub schema: '{label}' is missing required property '{prop_name}' "
                    "(README: \"Setting up the Configuration Hub\" lists the columns)."
                )
            elif actual.get("type") != expected_type:
                logger.error(
                    f"Hub schema: '{label}' property '{prop_name}' has type "
                    f"'{actual.get('type')}' (expected '{expected_type}')."
                )


# ------------------------------------------------------------------ #
#  Hub config reading
# ------------------------------------------------------------------ #

def read_config(client: "NotionClient") -> dict | None:
    """
    Read the Automator's own config row(s) (TOML in the page body), apply data quality checks, write the Errors
    and Warnings columns of those rows, and return a structured config dict for the daemon. Only Active rows are
    applied; other tools' rows in the same data source are never read or written.

    Returns:
        {
          "databases": [{"id", "closed_date_field", "reopen_count_field",
                         "first_value_fields", "update_count_fields"}, ...],
          "rtd_pairs": [{"definitions_db_id", "tasks_db_id", "closed_date_field",
                         "non_completion_statuses", "inherited_fields"}, ...],
        }
    Returns None if Hub is not available or the rows cannot be read (the daemon keeps its current config).
    """
    if not _available:
        return None

    try:
        rows = [r for r in client.query_database(_config_ds_id) if _is_own_config_row(r)]
    except Exception as e:
        logger.error(f"Hub: could not read config rows: {e} — keeping the current config.")
        return None
    if not rows:
        logger.error(f"Hub: the Automator's config row ({', '.join(_config_row_ids) or 'none'}) is not in the config data source (deleted or moved?) — keeping the current config.")
        return None

    databases: list[dict] = []
    closed_date_by_ds: dict[str, str | None] = {}  # property automation data source -> closed_date_field, any row
    rtd_raw: list[dict] = []                       # recurring tables from Active rows
    rtd_inactive: list[dict] = []
    messages: dict[str, dict[str, list[str]]] = {}  # row id -> {"errors": [...], "warnings": [...]}

    for row in rows:
        row_name = _get_prop_text(row, "Name") or row["id"]
        active = _row_status(row) == "Active"
        msgs = messages.setdefault(row["id"], {"errors": [], "warnings": []})

        try:
            text, body_error = _read_row_toml(client, row["id"])
        except Exception as e:
            logger.error(f"Hub: could not read the page body of config row '{row_name}': {e} — keeping the current config.")
            return None
        if body_error:
            dbs, recurring, problems = [], None, [("error", body_error)]
        else:
            dbs, recurring, problems = parse_row_config(text)

        for severity, message in problems:
            as_error = severity == "error" and active
            msgs["errors" if as_error else "warnings"].append(message)
            (logger.error if as_error else logger.warning)(f"Hub: config row '{row_name}': {message}")

        for db_cfg in dbs:
            closed_date_by_ds[db_cfg["id"]] = db_cfg["closed_date_field"]
            if active:
                databases.append(db_cfg)
        if recurring:
            recurring = {**recurring, "_row_id": row["id"], "_row_name": row_name}
            (rtd_raw if active else rtd_inactive).append(recurring)

    # ---- Duplicate pair detection -----------------------------------
    active_tasks_ids: dict[str, str] = {}   # tasks_db_id -> row_name of first Active pair
    active_defs_ids:  dict[str, str] = {}   # defs_db_id  -> row_name of first Active pair
    duplicate_tasks: set[str] = set()
    duplicate_defs:  set[str] = set()

    def _duplicate(row_id: str, text: str) -> None:
        logger.error(f"Hub: {text}")
        messages[row_id]["errors"].append(text)

    for pair in rtd_raw:
        t_id, d_id = pair["tasks_db_id"], pair["definitions_db_id"]
        if t_id in active_tasks_ids:
            _duplicate(pair["_row_id"],
                       f"config rows '{active_tasks_ids[t_id]}' and '{pair['_row_name']}' both reference the same "
                       f"tasks data source ({t_id}). Both pairs disabled. Each tasks data source must have exactly "
                       "one Active recurring_tasks table.")
            duplicate_tasks.add(t_id)
        else:
            active_tasks_ids[t_id] = pair["_row_name"]
        if d_id in active_defs_ids:
            _duplicate(pair["_row_id"],
                       f"config rows '{active_defs_ids[d_id]}' and '{pair['_row_name']}' both reference the same "
                       f"definitions data source ({d_id}). Both pairs disabled.")
            duplicate_defs.add(d_id)
        else:
            active_defs_ids[d_id] = pair["_row_name"]

    rtd_pairs = [
        {
            "definitions_db_id":       p["definitions_db_id"],
            "tasks_db_id":             p["tasks_db_id"],
            "closed_date_field":       closed_date_by_ds.get(p["tasks_db_id"]) or "Closed Date",
            "non_completion_statuses": p["non_completion_statuses"],   # [] = use built-in defaults
            "inherited_fields":        p["inherited_fields"],          # [] = inherit nothing
        }
        for p in rtd_raw
        if p["tasks_db_id"] not in duplicate_tasks and p["definitions_db_id"] not in duplicate_defs
    ]

    for p in rtd_inactive:
        for ids, key, label in ((active_tasks_ids, "tasks_db_id", "tasks"), (active_defs_ids, "definitions_db_id", "definitions")):
            if p[key] in ids:
                text = f"Inactive config row '{p['_row_name']}' shares its {label} data source ({p[key]}) with active pair '{ids[p[key]]}'."
                logger.warning(f"Hub: {text}")
                messages[p["_row_id"]]["warnings"].append(text)

    for row in rows:
        _write_row_messages(client, row, messages[row["id"]]["errors"], messages[row["id"]]["warnings"])
    _clean_row_ids.clear()
    _clean_row_ids.update(r["id"] for r in rows if _row_status(r) == "Active" and not messages[r["id"]]["errors"])

    logger.info(f"Hub config read: {len(databases)} task database(s), {len(rtd_pairs)} RTD pair(s).")
    return {"databases": databases, "rtd_pairs": rtd_pairs}


# ------------------------------------------------------------------ #
#  init
# ------------------------------------------------------------------ #

def init(client: "NotionClient", config_ds_id: str, status_ds_id: str = "", daemon_version: str = "") -> bool:
    """
    Check that the config data source (and the optional status data source) can be read, then create the status
    row and, if the config data source is empty, an inert example row.
    Returns True if the Hub is ready, False if setup failed (the daemon continues without the Hub).
    Call once at daemon startup after the NotionClient is created.
    """
    global _config_ds_id, _status_ds_id, _available

    _available = False
    config_id = _normalize_uuid(str(config_ds_id).strip())
    status_id = _normalize_uuid(str(status_ds_id).strip()) if status_ds_id else None
    if not _is_valid_db_id(config_id):
        logger.error("Hub: config_hub_data_source_id in config.toml is not a valid Notion ID — Hub features disabled.")
        return False
    if status_id and not _is_valid_db_id(status_id):
        logger.error("Hub: status_data_source_id in config.toml is not a valid Notion ID — Hub features disabled.")
        return False

    try:
        for key, ds_id in (("config_hub_data_source_id", config_id), ("status_data_source_id", status_id)):
            if ds_id:
                client.get_schema(ds_id)
    except Exception as e:
        logger.error(
            f"Hub: could not read the data source named by {key}: {_error_detail(e)} — Hub features disabled for this session. "
            "Check the ID, and that the integration has access to it (Notion: ... > Connections)."
        )
        return False

    _config_ds_id, _status_ds_id = config_id, status_id
    _available = True
    if not status_id:
        logger.info("status_data_source_id not set in config.toml — no kill switch or health status.")
    logger.info(f"Hub ready (Notion Automator v{daemon_version}): config_ds={_config_ds_id}, status_ds={_status_ds_id}")

    ids = _load_hub_ids()
    _ensure_config_row(client, ids.get(_CONFIG_PAGE_KEY))
    _ensure_hub_status_row(client, ids.get(_STATUS_PAGE_KEY))
    config_page = _config_row_ids[0] if _config_row_ids else None
    if (config_page or "") != ids.get(_CONFIG_PAGE_KEY, "") or (_hub_status_row_id or "") != ids.get(_STATUS_PAGE_KEY, ""):
        _save_hub_ids(_normalize_uuid(config_page) if config_page else None,
                      _normalize_uuid(_hub_status_row_id) if _hub_status_row_id else None)
    return True
