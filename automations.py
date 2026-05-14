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
#  Automation: manage "Closed Date" and "Reopen Count"
# ------------------------------------------------------------------ #

def auto_closed_date(client: "NotionClient", page: dict, prev_page: dict | None) -> dict:
    """
    Manages 'Closed Date' and 'Reopen Count' across close and reopen transitions.

    On close (non-Complete → Complete):
      - If 'Closed Date' is already set (pre-filled by user, either in this edit or
        a prior one): leave it for all task types. It feeds period counting and
        _create_next_task period key derivation.
      - Otherwise: stamp 'Closed Date' with now().

    On reopen (Complete → non-Complete):
      - Clear 'Closed Date' so the next close always stamps correctly.
      - Increment 'Reopen Count'.

    Governance:
      - 'Reopen Count' missing → initialize to 0.
      - Status is Complete but 'Closed Date' is empty → backfill with last_edited_time.
    """
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

    # Governance: initialize Reopen Count if missing
    if reopen_count is None:
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
        updates[REOPEN_COUNT_FIELD] = {"number": reopen_count + 1}
        return updates

    # Governance: non-Complete task still has a Closed Date — missed reopen while daemon was down.
    # Only fires during the init pass (prev_page is page), NOT during live polling.
    # This preserves intentionally pre-filled Closed Dates so they survive until the user
    # actually closes the task. The explicit reopen transition above handles the live case.
    if current_group != DONE_GROUP and closed_date and prev_page is page:
        logger.info("Governance: non-Complete task has Closed Date — missed reopen; incrementing Reopen Count and clearing Closed Date")
        updates[REOPEN_COUNT_FIELD] = {"number": reopen_count + 1}
        updates[CLOSED_DATE_FIELD] = {"date": None}
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
      - Only the time component of Due Date changed (date must change to count)
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

    # Increment only when: page has due-date history, new value is a date, and the DATE changed.
    # Time-only changes (e.g. 9am → 2pm on the same day) do not count.
    # Guard against prev_page=None (first poll after startup) — no baseline means no change.
    current_date_only = current_due[:10] if current_due else None
    prev_date_only    = prev_due[:10] if prev_due else None
    if prev_page is not None and first_due and current_date_only and current_date_only != prev_date_only:
        logger.info(f"Due date changed {prev_due!r} → {current_due!r} — incrementing to {count + 1}")
        updates[COUNTER_FIELD] = {"number": count + 1}

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
    auto_due_date_update_count,
    auto_recurring_tasks,
    # auto_last_edited_note,   # ← uncomment to enable
]

# Functions run at startup and on the 2am cron. Each receives only `client`.
# After all GOVERNANCE functions complete, Bot Notes are flushed and cleared.
GOVERNANCE = [
    run_recurring_governance,
]
