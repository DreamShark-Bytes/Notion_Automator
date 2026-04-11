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
from datetime import datetime, timezone
from typing import TYPE_CHECKING
import logging
logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from notion_api import NotionClient


# ------------------------------------------------------------------ #
#  Helper utilities
# ------------------------------------------------------------------ #

def _get_prop(page: dict, name: str) -> dict | None:
    return page.get("properties", {}).get(name)


def _get_select(page: dict, name: str) -> str | None:
    prop = _get_prop(page, name)
    if prop and prop.get("select"):
        return prop["select"]["name"]
    return None


def _get_status(page: dict, name: str) -> str | None:
    """Read a Notion 'status' type property (distinct from 'select')."""
    prop = _get_prop(page, name)
    if prop and prop.get("status"):
        return prop["status"]["name"]
    return None


# Cache: database_id -> {option_id: group_name}
_status_group_cache: dict[str, dict[str, str]] = {}


def _get_status_group(client, page: dict | None, status_field: str) -> str | None:
    """Return the group name (e.g. 'To-do', 'In Progress', 'Complete') for a status option."""
    if not page:
        return None
    prop = _get_prop(page, status_field)
    if not prop or not prop.get("status"):
        return None

    option_id = prop["status"]["id"]
    db_id = (page.get("parent") or {}).get("database_id")
    if not db_id:
        return None

    if db_id not in _status_group_cache:
        db = client.get_database(db_id)
        status_schema = db.get("properties", {}).get(status_field, {})
        mapping: dict[str, str] = {}
        for group in status_schema.get("status", {}).get("groups", []):
            for oid in group.get("option_ids", []):
                mapping[oid] = group["name"]
        _status_group_cache[db_id] = mapping

    return _status_group_cache[db_id].get(option_id)


def _get_date(page: dict, name: str) -> str | None:
    prop = _get_prop(page, name)
    if prop and prop.get("date"):
        return prop["date"]["start"]
    return None


def _get_number(page: dict, name: str) -> float | None:
    prop = _get_prop(page, name)
    if prop:
        return prop.get("number")
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ #
#  Automation: set "Last Closed" when Status → Done
# ------------------------------------------------------------------ #

def auto_last_closed(client: "NotionClient", page: dict, prev_page: dict | None) -> dict:
    """
    When the 'Status' field transitions INTO the 'Complete' group,
    stamp the 'Last Closed' date property with the current UTC time.

    Governance: if status is already Complete but 'Last Closed' is empty
    (e.g. tasks closed before the automation existed), backfill with
    last_edited_time. Transition check takes priority so real closures
    get an accurate timestamp rather than the backfill value.
    """
    STATUS_FIELD  = "Status"
    DONE_GROUP    = "Complete"
    TARGET_FIELD  = "Last Closed"

    current_group = _get_status_group(client, page, STATUS_FIELD)
    prev_group    = _get_status_group(client, prev_page, STATUS_FIELD)
    last_closed   = _get_date(page, TARGET_FIELD)
    logger.info(f"Compare groups: {current_group=} -> {prev_group=}, last_closed={last_closed!r}")

    # Transition: status just moved INTO Complete — stamp with current time
    if prev_page is not None and current_group == DONE_GROUP and prev_group != DONE_GROUP:
        logger.info("Status moved to Complete — stamping Last Closed")
        return {TARGET_FIELD: {"date": {"start": _now_iso()}}}

    # Governance: already Complete but Last Closed was never set — backfill
    if current_group == DONE_GROUP and not last_closed:
        backfill = page.get("last_edited_time", _now_iso())
        logger.info(f"Governance: '{TARGET_FIELD}' missing for completed task — backfilling: {backfill}")
        return {TARGET_FIELD: {"date": {"start": backfill}}}

    return {}


# ------------------------------------------------------------------ #
#  Automation: increment "Due Date Update Count" when Due Date changes
# ------------------------------------------------------------------ #

def auto_due_date_update_count(_client, page: dict, prev_page: dict | None) -> dict:
    """
    Increments 'Due Date Update Count' only when Due Date changes from one
    date to another.

    Governance (runs on every processed page):
      - 'Due Date Update Count' missing → initialized to 0.
      - 'First Due Date' missing but 'Due Date' set → stamped with current Due
        Date value; does NOT count as a change.

    Does NOT increment when:
      - Due Date is set for the first time (stamps 'First Due Date' instead)
      - Due Date is cleared
      - Due Date is unchanged
    """
    DATE_FIELD       = "Due Date"
    COUNTER_FIELD    = "Due Date Update Count"
    FIRST_DATE_FIELD = "First Due Date"

    current_due = _get_date(page, DATE_FIELD)
    prev_due    = _get_date(prev_page, DATE_FIELD) if prev_page else None
    first_due   = _get_date(page, FIRST_DATE_FIELD)
    count       = _get_number(page, COUNTER_FIELD)
    logger.info(f"Due date: {prev_due!r} → {current_due!r}, first_due={first_due!r}, count={count!r}")

    updates = {}

    # Governance: initialize counter if missing
    if count is None:
        updates[COUNTER_FIELD] = {"number": 0}
        count = 0

    # Governance: stamp First Due Date if Due Date is set but First Due Date isn't.
    # Treat this as the initial set — do not increment the counter.
    if not first_due and current_due:
        logger.info(f"First due date seen — stamping '{FIRST_DATE_FIELD}': {current_due}")
        updates[FIRST_DATE_FIELD] = {"date": {"start": current_due}}
        return updates

    # Increment only when: page has due-date history, new value is a date, and it changed.
    # Guard against prev_page=None (first poll after startup) — no baseline means no change.
    if prev_page is not None and first_due and current_due and current_due != prev_due:
        logger.info(f"Due date changed {prev_due!r} → {current_due!r} — incrementing to {count + 1}")
        updates[COUNTER_FIELD] = {"number": count + 1}

    return updates


# ------------------------------------------------------------------ #
#  Automation: auto-set "Last Edited" text field (example extra)
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

    # Compare raw property JSON to detect any change
    if page.get("properties") != prev_page.get("properties"):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return {TARGET_FIELD: {"rich_text": [{"type": "text", "text": {"content": ts}}]}}

    return {}


# ------------------------------------------------------------------ #
#  Registry — add/remove automations here
# ------------------------------------------------------------------ #

AUTOMATIONS = [
    auto_last_closed,
    auto_due_date_update_count,
    # auto_last_edited_note,   # ← uncomment to enable
]
