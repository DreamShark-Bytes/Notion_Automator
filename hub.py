"""
hub.py
Configuration Hub integration. Configuration lives in a Notion data source ("config data source"): one row per
config, with the TOML in a code block in the row's page body. The data source is named by its data source ID in
config.toml (`config_hub_data_source_id`); the user creates it (README: "Setting up the Configuration Hub").
A row acts only while its Status is Active; any other Status pauses it.

Public API:
    init(client, config_ds_id, daemon_version="")  — call once at daemon startup
    is_available()                                  — True if the Hub initialised successfully
    validate_hub_schema(client)                     — log problems with the config data source's columns
    read_config(client)                             — parse Active rows into the daemon's config dict
    build_snapshot(client)                          — initial snapshot of the Automator's own config rows
    poll_for_changes(client, snapshot)              — diff rows; says when the config must be re-read
"""

from __future__ import annotations
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
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

# Bot-owned file holding the page ID of the Automator's own row (other tools share the same data source).
HUB_CONFIG_PATH = "config_hub_state.toml"
_CONFIG_PAGE_KEY = "automator_config_page_id"
_OLD_STATUS_PAGE_KEY = "automator_status_page_id"  # written by earlier versions; dropped from the file on the next save

# Row the bot adds to an empty config data source, as a worked example. Its Status is set explicitly to an option that is not Active (see pick_inactive_status), so the data source's default can never start it.
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

# Columns the bot needs on the config data source (name -> Notion property type).
_REQUIRED_CONFIG_PROPS: dict[str, str] = {
    "Errors":   "rich_text",
    "Warnings": "rich_text",
}

# Optional date column on the config data source. The bot writes it after a governance pass that finished without
# errors, for each Active config row that read without errors; the column is never created by the bot.
_VERIFIED_PROP = "Last verified time"

# Optional columns on the config data source (never created by the bot; written or read only if they exist).
# Config Version is user-owned: set it to now (by hand or with an "Apply Config" button) when a changed config
# should be loaded. Report is bot-written, human-readable text that nothing parses.
_VERSION_PROP = "Config Version (Datetime)"
_REPORT_PROP = "Report"

# Seconds a row must stay unchanged before an edit is acted on (the fallback apply, or the edited-not-applied
# Warning), so the bot does not react while the user is still typing. config.toml: hub_settle_seconds.
DEFAULT_SETTLE_SECONDS = 300
_MESSAGE_LIMIT = 1900  # Notion allows 2000 characters per rich-text object

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

_config_ds_id:    str | None = None
_config_row_ids:  list[str] = []       # the Automator's own config rows; every other row is ignored
_available:       bool = False
_clean_row_ids:   set[str] = set()     # Active own config rows whose last read had no errors
_own_write_times: dict[str, str] = {}  # row id -> last_edited_time returned by the bot's own write
_settle_seconds:  float = DEFAULT_SETTLE_SECONDS
_now = time.monotonic                  # replaced in tests
_started_at: datetime = datetime.now().astimezone()
_app_version: str = ""

# What the bot has loaded, per Active own row: {"seen_version": Config Version at the last read, "applied_version":
# the version of the config that is running, "edited": last_edited_time at the last read, "parsed": the parsed TOML
# that is running (None if nothing valid), "mode": "ok" | "missing" | "invalid" (the Config Version column)}.
_loaded:          dict[str, dict] = {}
_last_good:       dict[str, tuple[list[dict], dict | None]] = {}  # row id -> (databases, recurring) of its last clean read
_edit_seen:       dict[str, tuple[str, float, datetime]] = {}  # row id -> (last_edited_time of a user edit, monotonic time the bot saw it, local time the wait ends)
_checked_edit:    dict[str, str] = {}                # row id -> user-edit time already compared with the loaded config

# Messages and Report lines in memory, written to the rows by flush().
_messages:        dict[tuple[str, str], dict[str, list[str]]] = {}  # (source, row id) -> {"errors", "warnings"}
_report_lines:    dict[tuple[str, str], str] = {}                   # (key, row id) -> one Report line
_REPORT_ORDER = ("app", "config", "config-edit", "governance")
_dirty:           bool = False


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


def resolve_settle_seconds(raw) -> float:
    """Return the settle time in seconds from the config.toml `hub_settle_seconds` value.

    Missing value -> DEFAULT_SETTLE_SECONDS. A value that is not a number of 0 or more is rejected with a warning."""
    if raw is None:
        return DEFAULT_SETTLE_SECONDS
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw)
    logger.warning(f"hub_settle_seconds '{raw}' is not a number of seconds (0 or more) — using {DEFAULT_SETTLE_SECONDS}.")
    return DEFAULT_SETTLE_SECONDS


def _is_valid_id(text: str) -> bool:
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


def _save_hub_ids(config_page_id: str | None) -> None:
    text = (
        "# Written by the daemon. This is the page ID of the Automator's own row in the Configuration Hub\n"
        "# data source. Other tools' rows in the same data source are never read or written.\n"
        "# Edit only to point the daemon at a different row: shut down the daemon, change the ID, restart.\n\n"
        "[databases]\n"
        f'{_CONFIG_PAGE_KEY} = "{config_page_id or ""}"\n'
    )
    try:
        with open(HUB_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        logger.warning(f"Could not write {HUB_CONFIG_PATH}: {e}")


def _message_text(messages: list[str]) -> str:
    """Join messages for a Notion rich_text value (2000 characters per text object). What does not fit is
    replaced by a visible "(N more, see the log)" line; the log always has every message."""
    text = "\n".join(messages)
    if len(text) <= _MESSAGE_LIMIT:
        return text
    kept: list[str] = []
    used = 0
    for message in messages:
        extra = len(message) + (1 if kept else 0)
        if used + extra > _MESSAGE_LIMIT - 40:
            break
        kept.append(message)
        used += extra
    if not kept:
        kept = [messages[0][:_MESSAGE_LIMIT - 40] + "…"]
    more = len(messages) - len(kept)
    return "\n".join(kept) + (f"\n({more} more, see the log)" if more else "")


# Times shown in the Report and in messages: minute precision, with a zone abbreviation. The zone is taken from the
# Config Version value (Notion writes it with the offset of whoever set it, and with a named zone if one was
# chosen), so it follows the user's Notion time zone each time Config Version is set; until a value with a zone
# has been seen it is the machine's local zone. This is display only. Change detection compares the raw
# Config Version text from Notion, so a daylight-saving change or a different zone can never look like a changed config.
_version_zone = None  # a tzinfo learned from the Config Version column, or None


def _zone_from_version(date_value: dict | None):
    """The time zone of a Notion date value: its named `time_zone` if it has one, else the offset in `start`.
    None for an empty value or a date without a time."""
    if not date_value:
        return None
    name = date_value.get("time_zone")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass  # unknown name, or no time zone database (Windows without tzdata): use the offset below
    start = date_value.get("start") or ""
    if len(start) <= 10:
        return None
    try:
        return datetime.fromisoformat(start).tzinfo  # None when the text has no offset
    except ValueError:
        return None


def _zone_label(dt: datetime) -> str:
    name = dt.tzname() or ""
    if 0 < len(name) <= 5:
        return name
    offset = dt.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def _format_time(dt: datetime, with_zone: bool = True) -> str:
    """e.g. 2026-11-25 14:02 CST (naive times are taken as local time)."""
    if dt.tzinfo is None:
        dt = dt.astimezone()  # a time without a zone is the machine's local time
    local = dt.astimezone(_version_zone) if _version_zone else dt.astimezone()
    return local.strftime("%Y-%m-%d %H:%M") + (f" {_zone_label(local)}" if with_zone else "")


def _local_stamp() -> str:
    return _format_time(datetime.now().astimezone())


def _format_version(raw: str | None) -> str:
    """A Config Version for the Report: minute precision in the display time zone. A date without a time stays a
    date. Text that is not a date is shown as it is."""
    if not raw:
        return "none"
    parsed = _parse_version(raw)
    if parsed is None:
        return raw
    if len(raw) <= 10:
        return raw
    return _format_time(parsed)


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
    """Return the block's ID lowercase with dashes removed, or '' (with an error) if it is missing or malformed."""
    raw = str(block.get(key) or "").replace("-", "").strip().lower()
    if not raw:
        problems.append(("error", f"{label}: {key} is empty."))
        return ""
    if not _is_valid_id(raw):
        problems.append(("error", f"{label}: {key} is not a valid Notion ID ('{raw}')."))
        return ""
    return raw


def parse_row_config(text: str) -> tuple[list[dict], dict | None, list[Problem]]:
    """Parse the TOML from one config row.

    Returns (databases, recurring, problems). `databases` has one dict per valid [[property_automations]] block, in
    the shape the daemon registers ({"data_source_id", "closed_date_field", "reopen_count_field", "first_value_fields",
    "update_count_fields"}). `recurring` is the [recurring_tasks] table as {"definitions_data_source_id", "tasks_data_source_id",
    "inherited_fields", "non_completion_statuses"}, or None. A block with a missing or malformed ID is skipped and
    reported as an error; the caller downgrades errors to warnings for rows that are not Active.
    """
    problems: list[Problem] = []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [], None, [("error", f"Invalid TOML: {e}")]

    if text.strip() == "":
        return [], None, [("error", "The code block is empty. Paste your config into it (see docs/config_row_example.toml).")]
    if not any(key in data for key in ("property_automations", "recurring_tasks")):
        problems.append(("warning", "The code block has no [[property_automations]] and no [recurring_tasks], so nothing is configured and nothing runs."))
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
            "data_source_id":      ds_id,
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
                    "definitions_data_source_id":       defs_id,
                    "tasks_data_source_id":             tasks_id,
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
#  Example row
# ------------------------------------------------------------------ #

_TODO_GROUP_NAMES = ("to-do", "to do", "todo", "not started")


def pick_inactive_status(status_schema: dict) -> str | None:
    """Name of a Status option that is not "Active", from a status property's schema, or None if there is none.

    Prefers an option in the To-do group (normally "Not started"), then any other non-Active option, in the order
    Notion lists them. Never guesses a name: it only returns one that the schema lists."""
    options = [o for o in (status_schema or {}).get("options") or [] if o.get("name")]
    candidates = [o for o in options if o["name"].strip().lower() != "active"]
    todo_ids = {oid for g in (status_schema or {}).get("groups") or []
                if str(g.get("name", "")).strip().lower() in _TODO_GROUP_NAMES for oid in g.get("option_ids") or []}
    for o in candidates:
        if o.get("id") in todo_ids:
            return o["name"]
    return candidates[0]["name"] if candidates else None


def _example_row_properties(client: "NotionClient") -> dict:
    """Properties for a new example row: the Name, plus an explicit non-Active Status when the schema offers one.
    If there is none, the row is created without a Status and a warning says the data source's default applies."""
    props = {"Name": {"title": [{"text": {"content": _EXAMPLE_ROW_NAME}}]}}
    try:
        status = client.get_schema(_config_ds_id).get("properties", {}).get("Status", {}).get("status", {})
    except Exception as e:
        logger.warning(f"Hub: could not read the Status options ({_error_detail(e)}); the new row gets the data source's default Status.")
        return props
    name = pick_inactive_status(status)
    if name:
        props["Status"] = {"status": {"name": name}}
    else:
        logger.warning(
            "Hub: the Status property has no option other than Active, so the new row gets the data source's "
            f"default (\"{status.get('default_option')}\"). Add another option such as Not started."
        )
    return props


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
        page = client.create_page(_config_ds_id, _example_row_properties(client))
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


def _primary_row_id() -> str | None:
    return _config_row_ids[0] if _config_row_ids else None


def report(source: str, errors=(), warnings=(), row: str | None = None) -> None:
    """Store a source's current Errors and Warnings for a config row (default: the Automator's own row).

    Makes no Notion call; flush() writes. A source replaces only its own messages, and an empty report clears
    them, so sources cannot erase each other's. Sources look like "config", "governance", "rtd:<name>".
    Messages are plain text for people; nothing parses them back."""
    global _dirty
    row_id = row or _primary_row_id()
    if row_id is None:
        return
    key = (source, row_id)
    value = {"errors": list(errors), "warnings": list(warnings)}
    if value["errors"] or value["warnings"]:
        if _messages.get(key) == value:
            return
        _messages[key] = value
    elif key in _messages:
        del _messages[key]
    else:
        return
    _dirty = True


def _report_app_line() -> None:
    """The Report's "App" line: version and start time on one line, no emoji (it reports no status)."""
    report_line("app", f"App: Version {_app_version}, Started: {_format_time(_started_at)}")


def report_line(key: str, text: str | None, row: str | None = None) -> None:
    """Store one line of the optional Report column ("app", "config", "config-edit" or "governance");
    text None removes it. flush() writes."""
    global _dirty
    row_id = row or _primary_row_id()
    if row_id is None:
        return
    if text is None:
        if _report_lines.pop((key, row_id), None) is not None:
            _dirty = True
    elif _report_lines.get((key, row_id)) != text:
        _report_lines[(key, row_id)] = text
        _dirty = True


def flush(client: "NotionClient") -> None:
    """Write the stored messages and Report lines to the Automator's own rows, only where the text changed.

    A write updates the row's last_edited_time, which change detection watches, so unchanged text is never
    rewritten and the time each write returns is remembered as the bot's own. Does nothing unless a report
    changed since the last flush. Without the optional Report column the Report lines are simply not written."""
    global _dirty
    if not _available or not _dirty:
        return
    try:
        rows = [r for r in client.query_database(_config_ds_id) if _is_own_config_row(r)]
    except Exception as e:
        logger.warning(f"Hub: could not read config rows to write messages: {e}")
        return
    write_failed = False
    for row in rows:
        mine = [(src, v) for (src, rid), v in sorted(_messages.items()) if _same_id(rid, row["id"])]
        wanted = {
            "Errors":   _message_text([f"[{src}] {m}" for src, v in mine for m in v["errors"]]),
            "Warnings": _message_text([f"[{src}] {m}" for src, v in mine for m in v["warnings"]]),
        }
        report_prop = row.get("properties", {}).get(_REPORT_PROP)
        if report_prop and report_prop.get("type") == "rich_text":
            lines = [text for key in _REPORT_ORDER for (k, rid), text in _report_lines.items()
                     if k == key and _same_id(rid, row["id"])]
            wanted[_REPORT_PROP] = "\n".join(lines)
        props = {column: {"rich_text": [{"text": {"content": text}}] if text else []}
                 for column, text in wanted.items() if text != _get_prop_text(row, column)}
        if not props:
            continue
        try:
            _remember_own_write(row["id"], client.update_page_properties(row["id"], props))
        except Exception as e:
            logger.warning(f"Hub: could not write to config row {row['id']}: {e}")
            write_failed = True
    _dirty = write_failed  # a failed write is tried again on the next flush


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
#  Hub polling — detecting config changes
# ------------------------------------------------------------------ #

def _version_info(row: dict | None) -> tuple[str, str | None]:
    """The row's Config Version column as (mode, raw start string): mode is "missing" (no such column: edits
    are detected by last_edited_time instead), "invalid" (not a Date property) or "ok"."""
    prop = ((row or {}).get("properties") or {}).get(_VERSION_PROP)
    if prop is None:
        return "missing", None
    if prop.get("type") != "date":
        return "invalid", None
    return "ok", (prop.get("date") or {}).get("start")


def _parse_version(raw: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _toml_or_none(text: str | None) -> dict | None:
    try:
        return tomllib.loads(text) if text is not None else None
    except tomllib.TOMLDecodeError:
        return None


def build_snapshot(client: "NotionClient") -> dict[str, dict]:
    """
    Build the initial change-detection snapshot of the Automator's own config rows.
    Call once after init() — existing Active rows do NOT trigger governance here.
    Raises on network/API errors.
    """
    if not _available:
        return {}
    snapshot = {r["id"]: r for r in client.query_database(_config_ds_id) if _is_own_config_row(r)}
    logger.info(f"Hub snapshot built ({len(snapshot)} row(s)).")
    return snapshot


def _check_edit(client: "NotionClient", row_id: str) -> None:
    """Compare a row's body with the config that is running and report "edited, not applied" if they differ.

    The comparison is on the PARSED TOML, so comment, spacing and key-order edits never warn. The warning says
    why an apply would fail, from checks that need no Notion call (TOML syntax, bad IDs, unknown keys)."""
    loaded = _loaded.get(row_id)
    if loaded is None:
        return
    text, body_error = _read_row_toml(client, row_id)
    detail = body_error or ""
    if body_error is None:
        data = _toml_or_none(text)
        if data is not None and data == loaded["parsed"]:
            report("config-edit", row=row_id)
            report_line("config-edit", None, row=row_id)
            return
        detail = "; ".join(m for sev, m in parse_row_config(text)[2] if sev == "error")
    message = "Edited but not applied: set Config Version to apply it." + (f" The edit has a problem: {detail}" if detail else "")
    report("config-edit", warnings=[message], row=row_id)
    report_line("config-edit", f"🟡 Config: edited, not applied · checked {_local_stamp()}", row=row_id)
    logger.warning(f"Hub: config row {row_id}: {message}")


def check_unapplied(client: "NotionClient") -> None:
    """Safety net for the daily governance pass: compare every Active row's body with the running config, in
    Config Version mode. (Polling does the same after an edit has settled.)"""
    if not _available:
        return
    for row_id, loaded in list(_loaded.items()):
        if loaded["mode"] != "ok":
            continue
        try:
            _check_edit(client, row_id)
        except Exception as e:
            logger.warning(f"Hub: could not check config row {row_id} for unapplied edits: {e}")


def poll_for_changes(
    client: "NotionClient",
    snapshot: dict[str, dict],
) -> tuple[dict[str, dict], bool]:
    """
    Query the Automator's own config rows, diff against snapshot, and detect changes. Other tools' rows in the
    same data source are ignored.
    Returns (new_snapshot, needs_apply). needs_apply is True when the caller must call read_config() and apply
    the result:
    - a row became Active, or left Active (its automations must stop);
    - the row's Config Version differs from the one loaded (any difference, even into the past);
    - without a Config Version column: the row was edited by the user and has stayed unchanged for the settle time.
    With the column, an edit made without setting Config Version is not applied; once it has settled the body is
    compared with the running config and a Warning appears if they differ.
    The bot's own writes are never mistaken for edits. Raises on network/API errors — the caller must not
    update its snapshot on error.
    """
    if not _available:
        return snapshot, False

    new_snapshot = dict(snapshot)
    needs_apply = False
    now = _now()

    for row in client.query_database(_config_ds_id):
        if not _is_own_config_row(row):
            continue
        row_id = row["id"]
        prev = snapshot.get(row_id)
        new_snapshot[row_id] = row
        loaded = _loaded.get(row_id)
        was_active = prev is not None and _row_status(prev) == "Active"

        if _row_status(row) != "Active":
            if was_active or loaded is not None:
                logger.info(f"Hub: config row {row_id} left Active — reloading config (its automations stop).")
                needs_apply = True
            continue
        if not was_active:
            logger.info(f"Hub: config row {row_id} ({'new' if prev is None else 'existing'}) → Active — applying its config.")
            needs_apply = True
            continue

        mode, version = _version_info(row)
        if mode == "ok":
            saved_version = loaded["seen_version"] if loaded else _version_info(prev)[1]
            if version != saved_version:
                logger.info(f"Hub: config row {row_id}: Config Version changed ({saved_version!r} → {version!r}) — applying.")
                needs_apply = True
                continue

        # An edit by the user: the row changed, and not by the bot's own write or before the config was last read.
        edited = row.get("last_edited_time")
        baseline = loaded["edited"] if loaded else prev.get("last_edited_time")
        if edited and edited not in (baseline, _own_write_times.get(row_id)) and _edit_seen.get(row_id, (None,))[0] != edited:
            ends = datetime.now().astimezone() + timedelta(seconds=_settle_seconds)
            _edit_seen[row_id] = (edited, now, ends)
        seen = _edit_seen.get(row_id)
        if seen is None:
            continue
        if now - seen[1] < _settle_seconds:
            # Still waiting for the user to stop editing: say so in the Report (not in Warnings).
            action = "will check" if mode == "ok" else "will apply"
            report_line("config-edit", f"🟡 Config: edit detected, {action} at {_format_time(seen[2], with_zone=False)}", row=row_id)
            continue
        if mode == "ok":
            if _checked_edit.get(row_id) != seen[0]:
                _checked_edit[row_id] = seen[0]
                try:
                    _check_edit(client, row_id)
                except Exception as e:
                    logger.warning(f"Hub: could not check config row {row_id} for an unapplied edit: {e}")
                else:
                    if loaded is not None and "config-edit" not in {src for src, rid in _messages if rid == row_id}:
                        loaded["edited"] = seen[0]  # compared and equal: a harmless edit, so it is not pending again
                        _edit_seen.pop(row_id, None)
        else:
            logger.info(f"Hub: Active config row {row_id} was edited and has settled — applying.")
            needs_apply = True

    flush(client)
    return new_snapshot, needs_apply


# ------------------------------------------------------------------ #
#  Hub schema validation
# ------------------------------------------------------------------ #

def validate_hub_schema(client: "NotionClient") -> None:
    """
    Validate that the config data source has the expected columns.
    Logs ERROR for missing/wrong-type properties or a missing "Active" Status option.
    Call once at daemon startup after init().
    """
    if not _available:
        return

    label = "Config"
    try:
        schema = client.get_schema(_config_ds_id)
    except Exception as e:
        logger.error(f"Hub schema check: could not read the '{label}' data source: {e}")
        return

    props = schema.get("properties", {})
    names = {o.get("name", "") for o in props.get("Status", {}).get("status", {}).get("options", [])}
    if "Active" not in names:
        logger.error(
            f"Hub schema: '{label}' Status property is missing the 'Active' option. "
            "The bot cannot gate automations on this data source."
        )
    verified = props.get(_VERIFIED_PROP)
    if verified is None:
        logger.info(f"Hub schema: optional '{_VERIFIED_PROP}' (date) is not on the config data source; no verification time is written.")
    elif verified.get("type") != "date":
        logger.error(
            f"Hub schema: '{_VERIFIED_PROP}' has type '{verified.get('type')}' (expected 'date'). "
            "Notion does not let the bot write a 'Last edited time' property; use a Date property."
        )
    for prop_name, expected_type in ((_VERSION_PROP, "date"), (_REPORT_PROP, "rich_text")):
        optional = props.get(prop_name)
        if optional is None:
            logger.info(
                f"Hub schema: optional '{prop_name}' ({expected_type}) is not on the config data source; "
                + ("config edits are detected by the row's last edited time instead." if prop_name == _VERSION_PROP
                   else "no Report is written.")
            )
        elif optional.get("type") != expected_type:
            logger.error(f"Hub schema: '{prop_name}' has type '{optional.get('type')}' (expected '{expected_type}').")
    for prop_name, expected_type in _REQUIRED_CONFIG_PROPS.items():
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
    Read the Automator's own config row(s) (TOML in the page body), apply data quality checks, store their
    Errors, Warnings and Report lines (flush() writes them), and return a structured config dict for the daemon.
    Only Active rows are applied; other tools' rows in the same data source are never read or written.

    A read that finds errors in an Active row never replaces a config that is already running for that row: the
    last clean config stays (a Warning says so). An Error is raised only for a row with nothing loaded.

    Returns:
        {
          "databases": [{"data_source_id", "closed_date_field", "reopen_count_field",
                         "first_value_fields", "update_count_fields"}, ...],
          "rtd_pairs": [{"definitions_data_source_id", "tasks_data_source_id", "closed_date_field",
                         "non_completion_statuses", "inherited_fields"}, ...],
        }
    Returns None if Hub is not available or the rows cannot be read (the daemon keeps its current config).
    """
    global _version_zone
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
    closed_date_by_ds: dict[str, str | None] = {}  # property automation data source -> closed_date_field, Active rows
    rtd_raw: list[dict] = []                       # recurring tables from Active rows
    messages: dict[str, dict[str, list[str]]] = {}  # row id -> {"errors": [...], "warnings": [...]}
    config_lines: dict[str, str] = {}              # row id -> Report line for the config read
    for row in rows:
        row_id = row["id"]
        row_name = _get_prop_text(row, "Name") or row_id
        active = _row_status(row) == "Active"
        msgs = messages.setdefault(row_id, {"errors": [], "warnings": []})
        mode, version = _version_info(row)
        previous = _loaded.get(row_id)
        stamp = _local_stamp()
        zone = _zone_from_version((row.get("properties", {}).get(_VERSION_PROP) or {}).get("date")) if mode == "ok" else None
        if zone is not None:
            _version_zone = zone

        if not active:
            # Paused: nothing from this row runs and its body is not read. Edits made meanwhile are read when it
            # becomes Active again.
            _loaded.pop(row_id, None)
            _edit_seen.pop(row_id, None)
            _checked_edit.pop(row_id, None)
            config_lines[row_id] = "⚪ Config: not Active, nothing running"
            continue
        try:
            text, body_error = _read_row_toml(client, row_id)
        except Exception as e:
            logger.error(f"Hub: could not read the page body of config row '{row_name}': {e} — keeping the current config.")
            return None
        data = None
        if body_error:
            dbs, recurring, problems = [], None, [("error", body_error)]
        else:
            dbs, recurring, problems = parse_row_config(text)
            data = _toml_or_none(text)

        failed = any(sev == "error" for sev, _ in problems)
        good = _last_good.get(row_id)
        keep_last = failed and good is not None
        applied_before = previous["applied_version"] if previous else None
        if keep_last:
            dbs, recurring = good
            retry = "set Config Version again" if mode == "ok" else "edit the row again"
            msgs["warnings"].append(
                f"Apply failed, still running the config from Config Version {applied_before or 'none'}. "
                f"Fix the problems below, then {retry}."
            )
        elif not failed:
            _last_good[row_id] = (dbs, recurring)

        for severity, message in problems:
            as_error = severity == "error" and not keep_last
            msgs["errors" if as_error else "warnings"].append(message)
            (logger.error if as_error else logger.warning)(f"Hub: config row '{row_name}': {message}")

        if mode == "invalid":
            text_ = f"'{_VERSION_PROP}' is not a Date property, so edits are detected by the row's last edited time instead."
            msgs["errors"].append(text_)
            logger.error(f"Hub: config row '{row_name}': {text_}")
        elif mode == "ok":
            if version is None:
                msgs["warnings"].append(
                    f"{_VERSION_PROP} is empty. Set it to now to apply a changed config; until then only "
                    "activating the row or restarting reads it."
                )
            elif previous and previous["seen_version"]:
                new_dt, old_dt = _parse_version(version), _parse_version(previous["seen_version"])
                if new_dt and old_dt and new_dt < old_dt:
                    msgs["warnings"].append(
                        f"{_VERSION_PROP} ({version}) is older than the one loaded ({previous['seen_version']}). "
                        "The config was applied anyway."
                    )

        mode_note = "" if mode == "ok" else " · no Config Version column, applies after an edit settles"
        if keep_last:
            config_lines[row_id] = f"🔴 Config: apply failed, still running version {_format_version(applied_before)} · {stamp}"
        elif failed:
            what = "applied with errors" if dbs or recurring else "not loaded"
            config_lines[row_id] = f"🔴 Config: {what}, see Errors · {stamp}"
        else:
            icon, note = ("🟡", ", see Warnings") if msgs["warnings"] else ("🟢", "")
            config_lines[row_id] = f"{icon} Config: applied{note} · {stamp} · version {_format_version(version)}{mode_note}"
        _loaded[row_id] = {
            "seen_version":    version,
            "applied_version": applied_before if keep_last else version,
            "edited":          row.get("last_edited_time"),
            "parsed":          previous["parsed"] if keep_last and previous else data,
            "mode":            mode,
        }
        _edit_seen.pop(row_id, None)
        _checked_edit.pop(row_id, None)

        for db_cfg in dbs:
            closed_date_by_ds[db_cfg["data_source_id"]] = db_cfg["closed_date_field"]
            databases.append(db_cfg)
        if recurring:
            rtd_raw.append({**recurring, "_row_id": row_id, "_row_name": row_name})

    # ---- Duplicate pair detection -----------------------------------
    active_tasks_ids: dict[str, str] = {}   # tasks_data_source_id -> row_name of first Active pair
    active_defs_ids:  dict[str, str] = {}   # definitions_data_source_id -> row_name of first Active pair
    duplicate_tasks: set[str] = set()
    duplicate_defs:  set[str] = set()

    def _duplicate(row_id: str, text: str) -> None:
        logger.error(f"Hub: {text}")
        messages[row_id]["errors"].append(text)

    for pair in rtd_raw:
        t_id, d_id = pair["tasks_data_source_id"], pair["definitions_data_source_id"]
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
            "definitions_data_source_id": p["definitions_data_source_id"],
            "tasks_data_source_id":       p["tasks_data_source_id"],
            "closed_date_field":          closed_date_by_ds.get(p["tasks_data_source_id"]) or "Closed Date",
            "non_completion_statuses":    p["non_completion_statuses"],   # [] = use built-in defaults
            "inherited_fields":           p["inherited_fields"],          # [] = inherit nothing
        }
        for p in rtd_raw
        if p["tasks_data_source_id"] not in duplicate_tasks and p["definitions_data_source_id"] not in duplicate_defs
    ]

    for row in rows:
        row_id = row["id"]
        report("config", messages[row_id]["errors"], messages[row_id]["warnings"], row=row_id)
        report("config-edit", row=row_id)
        report_line("config", config_lines.get(row_id), row=row_id)
        report_line("config-edit", None, row=row_id)
    _clean_row_ids.clear()
    _clean_row_ids.update(r["id"] for r in rows if _row_status(r) == "Active" and not messages[r["id"]]["errors"])

    _report_app_line()  # re-written here so the start time is shown in the zone learned above
    logger.info(f"Hub config read: {len(databases)} task database(s), {len(rtd_pairs)} RTD pair(s).")
    return {"databases": databases, "rtd_pairs": rtd_pairs}


def record_governance(failures: list[str], trigger: str) -> None:
    """Store the result of a governance pass: its failures as Errors (source "governance") and a Report line.
    flush() writes them. An empty `failures` list clears the previous errors."""
    report("governance", errors=failures)
    if failures:
        report_line("governance", f"🔴 Governance: failed, see Errors · {_local_stamp()} · {trigger}")
    else:
        report_line("governance", f"🟢 Governance: OK · {_local_stamp()} · {trigger}")


# ------------------------------------------------------------------ #
#  init
# ------------------------------------------------------------------ #

def init(client: "NotionClient", config_ds_id: str, daemon_version: str = "", settle_seconds: float = DEFAULT_SETTLE_SECONDS) -> bool:
    """
    Check that the config data source can be read, then find the Automator's own row or, if there is none,
    create an inert example row.
    Returns True if the Hub is ready, False if setup failed (the daemon continues without the Hub).
    Call once at daemon startup after the NotionClient is created.
    """
    global _config_ds_id, _available, _settle_seconds, _dirty, _version_zone, _started_at, _app_version

    _available = False
    _settle_seconds = settle_seconds
    _version_zone = None
    _started_at = datetime.now().astimezone()
    _app_version = daemon_version
    for state in (_loaded, _last_good, _edit_seen, _checked_edit, _messages, _report_lines, _own_write_times):
        state.clear()
    _dirty = False
    config_id = _normalize_uuid(str(config_ds_id).strip())
    if not _is_valid_id(config_id):
        logger.error("Hub: config_hub_data_source_id in config.toml is not a valid Notion ID — Hub features disabled.")
        return False

    try:
        client.get_schema(config_id)
    except Exception as e:
        logger.error(
            f"Hub: could not read the data source named by config_hub_data_source_id: {_error_detail(e)} — "
            "Hub features disabled for this session. "
            "Check the ID, and that the integration has access to it (Notion: ... > Connections)."
        )
        return False

    _config_ds_id = config_id
    _available = True
    logger.info(f"Hub ready (Notion Automator v{daemon_version}): config_ds={_config_ds_id}")

    ids = _load_hub_ids()
    _ensure_config_row(client, ids.get(_CONFIG_PAGE_KEY))
    config_page = _config_row_ids[0] if _config_row_ids else None
    if (config_page or "") != ids.get(_CONFIG_PAGE_KEY, "") or _OLD_STATUS_PAGE_KEY in ids:
        _save_hub_ids(_normalize_uuid(config_page) if config_page else None)
    _report_app_line()
    return True
