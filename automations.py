"""
automations.py
Each automation is a standalone function that receives:
  - client: NotionClient
  - page: the full page dict from the API
  - prev_page: the previous snapshot of that page (or None on first run)

Return a dict of property updates to apply, or {} to skip.

Add your own automations by following the same pattern and registering
them in AUTOMATIONS at the bottom of this file.
"""

from __future__ import annotations
from typing import TYPE_CHECKING
import logging

from recurring_tasks import (
    _get_prop,
    _get_date,
    _get_number,
    _get_select,
    _get_status,
    _get_text,
    _get_status_group,
    _now_iso,
    _now_local_iso,
    _parse_closed_dt,
    auto_recurring_tasks,
    run_recurring_governance,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from notion_api import NotionClient


# ------------------------------------------------------------------ #
#  Per-database automation config registry
# ------------------------------------------------------------------ #

_db_configs: dict[str, dict] = {}  # database_id -> [[databases]] config entry

# Tracks databases for which a due_date_tracking deprecation warning has fired,
# so the warning appears once per database rather than on every page.
_deprecation_warned: set[str] = set()


def register_db(db_id: str, cfg: dict) -> None:
    """Register a database's automation config. Called once per database at daemon startup."""
    _db_configs[db_id.replace("-", "")] = cfg


def _flags(page: dict) -> dict:
    """Return the automation flags for the database this page belongs to."""
    db_id = page.get("parent", {}).get("database_id", "").replace("-", "")
    return _db_configs.get(db_id, {})


def _db_id_of(page: dict) -> str:
    return page.get("parent", {}).get("database_id", "")


# ------------------------------------------------------------------ #
#  Per-database Notion schema cache
# ------------------------------------------------------------------ #

_db_schema_cache: dict[str, dict[str, str]] = {}  # db_id -> {field_name -> field_type}


def _get_db_schema(client: "NotionClient", db_id: str) -> dict[str, str]:
    """Return {field_name: field_type} for the given database. Cached per daemon run."""
    key = db_id.replace("-", "")
    if key not in _db_schema_cache:
        try:
            db = client.get_database(db_id)
            _db_schema_cache[key] = {
                name: prop.get("type", "unknown")
                for name, prop in db.get("properties", {}).items()
            }
            logger.debug(f"Loaded schema for database {db_id}: {len(_db_schema_cache[key])} fields.")
        except Exception as e:
            logger.warning(f"Could not load schema for database {db_id}: {e}")
            _db_schema_cache[key] = {}
    return _db_schema_cache[key]


# ------------------------------------------------------------------ #
#  Field value helpers for generic tracking
# ------------------------------------------------------------------ #

def _read_canonical(page: dict, field_name: str, field_type: str) -> str | None:
    """Read a field value as a comparable string. Returns None if the field is empty."""
    if field_type == "date":
        val = _get_date(page, field_name)
        return val[:10] if val else None
    if field_type == "select":
        return _get_select(page, field_name)
    if field_type == "status":
        return _get_status(page, field_name)
    if field_type == "number":
        n = _get_number(page, field_name)
        return str(n) if n is not None else None
    if field_type in ("rich_text", "title", "text"):
        return _get_text(page, field_name)
    if field_type in ("url", "email", "phone_number"):
        prop = _get_prop(page, field_name)
        return (prop or {}).get(field_type) if prop else None
    return None


def _build_first_value_write(
    source_page: dict,
    field_name: str,
    source_type: str,
    target_type: str,
) -> dict | None:
    """
    Build the Notion API property payload for writing a first-value snapshot.
    The target field type determines the write format.
    Returns None if the source field is empty.
    """
    if source_type == "date" and target_type == "date":
        prop = _get_prop(source_page, field_name) or {}
        date_obj = prop.get("date") or {}
        filtered = {k: v for k, v in date_obj.items() if k in ("start", "end")}
        return {"date": filtered} if filtered.get("start") else None

    if target_type == "rich_text":
        val = _read_canonical(source_page, field_name, source_type)
        if not val:
            return None
        return {"rich_text": [{"type": "text", "text": {"content": val}}]}

    if target_type == "number":
        n = _get_number(source_page, field_name)
        return {"number": n} if n is not None else None

    return None


def _resolve_tracking_fields(flags: dict, config_key: str, db_id: str) -> list[str]:
    """
    Return the list of field names to track for a given config key.
    Handles the due_date_tracking -> first_value_fields/update_count_fields migration.
    """
    fields = flags.get(config_key)
    if fields:
        return fields

    if flags.get("due_date_tracking"):
        if db_id not in _deprecation_warned:
            _deprecation_warned.add(db_id)
            logger.warning(
                f"Database {db_id}: 'due_date_tracking = true' is deprecated. "
                f"Replace with 'first_value_fields = [\"Due Date\"]' and "
                f"'update_count_fields = [\"Due Date\"]' in config.toml."
            )
        return ["Due Date"]

    return []


# ------------------------------------------------------------------ #
#  Automation: manage "Closed Date" and "Reopen Count"
# ------------------------------------------------------------------ #

def auto_closed_date(client: "NotionClient", page: dict, prev_page: dict | None) -> dict:
    """
    Manages 'Closed Date' and 'Reopen Count' across close and reopen transitions.

    On close (non-Complete -> Complete):
      - If 'Closed Date' is already set (pre-filled by user, either in this edit or
        a prior one): leave it for all task types. It feeds period counting and
        _create_next_task period key derivation.
      - Otherwise: stamp 'Closed Date' with now().

    On reopen (Complete -> non-Complete):
      - Clear 'Closed Date' so the next close always stamps correctly.
      - Increment 'Reopen Count'.

    Governance:
      - 'Reopen Count' missing -> initialize to 0.
      - Status is Complete but 'Closed Date' is empty -> backfill with last_edited_time.
    """
    flags = _flags(page)
    if not flags.get("closed_date"):
        return {}

    STATUS_FIELD       = "Status"
    DONE_GROUP         = "Complete"
    CLOSED_DATE_FIELD  = "Closed Date"
    REOPEN_COUNT_FIELD = "Reopen Count"

    current_group = _get_status_group(client, page, STATUS_FIELD)
    prev_group    = _get_status_group(client, prev_page, STATUS_FIELD)
    closed_date   = _get_date(page, CLOSED_DATE_FIELD)
    reopen_count  = _get_number(page, REOPEN_COUNT_FIELD)
    logger.info(f"Compare groups: {current_group=} -> {prev_group=}, closed_date={closed_date!r}, reopen_count={reopen_count!r}")

    updates = {}

    # Governance: initialize Reopen Count if missing (only if reopen_count tracking is enabled)
    if flags.get("reopen_count") and reopen_count is None:
        updates[REOPEN_COUNT_FIELD] = {"number": 0}
        reopen_count = 0

    # Transition: status moved INTO Complete (close)
    if prev_page is not None and current_group == DONE_GROUP and prev_group != DONE_GROUP:
        if closed_date:
            logger.info("Status moved to Complete (Closed Date already set) — respecting user date")
        else:
            logger.info("Status moved to Complete — stamping Closed Date")
            updates[CLOSED_DATE_FIELD] = {"date": {"start": _now_local_iso()}}
        return updates

    # Transition: status moved OUT OF Complete (reopen)
    if prev_page is not None and prev_group == DONE_GROUP and current_group != DONE_GROUP:
        logger.info("Status moved out of Complete — clearing Closed Date, incrementing Reopen Count")
        updates[CLOSED_DATE_FIELD] = {"date": None}
        if flags.get("reopen_count"):
            updates[REOPEN_COUNT_FIELD] = {"number": reopen_count + 1}
        return updates

    # Governance: non-Complete task still has a Closed Date — missed reopen while daemon was down.
    # Only fires during the init pass (prev_page is page), NOT during live polling.
    # This preserves intentionally pre-filled Closed Dates so they survive until the user
    # actually closes the task. The explicit reopen transition above handles the live case.
    if current_group != DONE_GROUP and closed_date and prev_page is page:
        logger.info("Governance: non-Complete task has Closed Date — missed reopen; clearing Closed Date, incrementing Reopen Count")
        updates[CLOSED_DATE_FIELD] = {"date": None}
        if flags.get("reopen_count"):
            updates[REOPEN_COUNT_FIELD] = {"number": reopen_count + 1}
        return updates

    # Governance: already Complete but Closed Date never set — backfill.
    # last_edited_time from Notion is always UTC; convert to local calendar date
    # so the stored Closed Date reflects the local day, not the UTC day.
    if current_group == DONE_GROUP and not closed_date:
        raw_ts = page.get("last_edited_time")
        dt = _parse_closed_dt(raw_ts) if raw_ts else None
        backfill = dt.isoformat() if dt else _now_local_iso()
        logger.info(f"Governance: 'Closed Date' missing for completed task — backfilling: {backfill}")
        updates[CLOSED_DATE_FIELD] = {"date": {"start": backfill}}
        return updates

    return updates


# ------------------------------------------------------------------ #
#  Automation: stamp "First [Field]" on first observation
# ------------------------------------------------------------------ #

def auto_first_value(client: "NotionClient", page: dict, prev_page: dict | None) -> dict:
    """
    For each field listed in first_value_fields, stamps a 'First [Field Name]'
    column with the field's first observed non-empty value. Never overwrites.

    Naming convention: the bot looks for a column named 'First [Field Name]'
    in the database schema. Missing columns are skipped silently.

    Config (per [[databases]] entry):
        first_value_fields = ["Due Date", "Status", "Priority"]

    Deprecated alias: due_date_tracking = true is treated as
        first_value_fields = ["Due Date"]
    with a one-time warning per database.

    Supported source types: date, select, status, number,
    rich_text/text, url, email, phone_number.
    """
    flags = _flags(page)
    db_id = _db_id_of(page)
    fields = _resolve_tracking_fields(flags, "first_value_fields", db_id)
    if not fields:
        return {}

    schema = _get_db_schema(client, db_id)
    updates = {}

    for field_name in fields:
        target_name = f"First {field_name}"
        if target_name not in schema:
            continue
        source_type = schema.get(field_name)
        target_type = schema.get(target_name)
        if not source_type or not target_type:
            continue

        # Already stamped — never overwrite.
        if _read_canonical(page, target_name, target_type) is not None:
            continue

        write = _build_first_value_write(page, field_name, source_type, target_type)
        if write:
            logger.info(f"First value seen for '{field_name}' — stamping '{target_name}'.")
            updates[target_name] = write

    return updates


# ------------------------------------------------------------------ #
#  Automation: increment "[Field] Update Count" on value change
# ------------------------------------------------------------------ #

def auto_update_count(client: "NotionClient", page: dict, prev_page: dict | None) -> dict:
    """
    For each field listed in update_count_fields, increments a
    '[Field Name] Update Count' number column whenever the field value changes.

    Naming convention: the bot looks for '[Field Name] Update Count' in the
    schema. Missing columns are skipped silently.

    Config (per [[databases]] entry):
        update_count_fields = ["Due Date", "Status"]

    Governance: initializes the counter to 0 if the field is null.

    For date fields, only a change to the date portion counts. Time-only
    changes (e.g. 9 am to 2 pm on the same day) are ignored.

    Does not increment:
      - On the first time a value is set (no previous snapshot to compare)
      - When the field is cleared
      - When the value is unchanged

    Deprecated alias: due_date_tracking = true is treated as
        update_count_fields = ["Due Date"]
    with a one-time warning per database.
    """
    flags = _flags(page)
    db_id = _db_id_of(page)
    fields = _resolve_tracking_fields(flags, "update_count_fields", db_id)
    if not fields:
        return {}

    schema = _get_db_schema(client, db_id)
    updates = {}

    for field_name in fields:
        counter_name = f"{field_name} Update Count"
        if counter_name not in schema:
            continue

        count = _get_number(page, counter_name)

        # Governance: initialize counter if missing.
        if count is None:
            updates[counter_name] = {"number": 0}
            continue

        # No previous snapshot — skip incrementing on init pass.
        if prev_page is None:
            continue

        source_type = schema.get(field_name)
        if not source_type:
            continue

        current_val = _read_canonical(page, field_name, source_type)
        prev_val    = _read_canonical(prev_page, field_name, source_type)

        if current_val and prev_val and current_val != prev_val:
            logger.info(
                f"'{field_name}' changed {prev_val!r} -> {current_val!r} "
                f"— incrementing '{counter_name}' to {int(count) + 1}."
            )
            updates[counter_name] = {"number": int(count) + 1}

    return updates


# ------------------------------------------------------------------ #
#  Automation: auto-set "Last Edited" text field (example / disabled)
# ------------------------------------------------------------------ #

def auto_last_edited_note(_client, page: dict, prev_page: dict | None) -> dict:
    """
    Writes a human-readable timestamp to a 'Last Edited (Bot)' rich_text
    field whenever ANY property changes.  Useful for auditing.
    Disabled by default — add it to AUTOMATIONS below to enable.
    """
    TARGET_FIELD = "Last Edited (Bot)"

    if prev_page is None:
        return {}

    if page.get("properties") != prev_page.get("properties"):
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return {TARGET_FIELD: {"rich_text": [{"type": "text", "text": {"content": ts}}]}}

    return {}


# ------------------------------------------------------------------ #
#  Registry — add/remove automations here
# ------------------------------------------------------------------ #

AUTOMATIONS = [
    auto_closed_date,
    auto_first_value,
    auto_update_count,
    # auto_recurring_tasks must remain last — it creates Notion pages and should
    # run after all field-stamping automations have settled their updates.
    auto_recurring_tasks,
    # auto_last_edited_note,   # <- uncomment to enable
]

# Functions run at startup and on the 2am cron. Each receives only `client`.
# After all GOVERNANCE functions complete, Bot Notes are flushed and cleared.
GOVERNANCE = [
    run_recurring_governance,
]
