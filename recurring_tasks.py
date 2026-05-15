"""
recurring_tasks.py
Recurring task automation: creates a new task when a recurring task is completed.

Also exports shared helpers used by automations.py.

Call init() at daemon startup before using any functions here.
"""

from __future__ import annotations
import calendar
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from bot_notes import (
    add_bot_note,
    mark_page_examined,
    RTD_AT_MOST_N_REACHED,
)

if TYPE_CHECKING:
    from notion_api import NotionClient

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Module state — set via init()
# ------------------------------------------------------------------ #

_definitions_db_id: str | None = None
_tasks_db_id: str | None = None


def init(definitions_db_id: str, tasks_db_id: str) -> None:
    """Call once at daemon startup to enable recurring task automation."""
    global _definitions_db_id, _tasks_db_id
    _definitions_db_id = definitions_db_id
    _tasks_db_id = tasks_db_id


# ------------------------------------------------------------------ #
#  Fields never inherited from the closed task
# ------------------------------------------------------------------ #

FIELDS_NOT_INHERITED = {
    "Closed Date",
    "Reopen Count",
    "First Due Date",
    "Due Date Update Count",
    "Due Date",
    "Status",
    "Instance # (Recurring Task)",
    "Period Key (Recurring Task)",
    "Period Target (Recurring Task)",
    "Ignore Grace Period (Recurring Task)",
    "Recurring Series",
}

# How many days into a new period before stale Responsibility tasks are auto-cancelled.
# 1 means: cancel on the first governance run that occurs at least 1 day past the period
# boundary (e.g. ≥ Tuesday for a weekly task, ≥ the 2nd for a monthly task).
# Increase to give more buffer before cancellation fires.
PERIOD_CAP_DAYS = 1

# Statuses in the Complete group that do NOT count as completions.
# Tasks with these statuses are treated as "skipped/missed" by governance —
# they do not advance Instance # or trigger force_next.
# Compared after lowercasing and stripping all non-letter characters, so
# "Cancelled", "cancelled", "canceled", and "Canceled" all match "cancelled"/"canceled".
NON_COMPLETION_STATUSES: frozenset[str] = frozenset({
    "cancelled", "canceled",
    "skipped", "missed",
    "ignored", "abandoned",
    "destroyed", "disintegrated", "vaporized", "yeeted", "evaporated","banished"
})

# Property types that are read-only and cannot be set via the API
_READONLY_PROP_TYPES = {
    "formula", "rollup", "created_time", "last_edited_time",
    "created_by", "last_edited_by", "unique_id", "verification",
    "button",
}


# ------------------------------------------------------------------ #
#  Shared helpers (imported by automations.py)
# ------------------------------------------------------------------ #

def _get_prop(page: dict, name: str) -> dict | None:
    return page.get("properties", {}).get(name) if page else None


def _get_select(page: dict, name: str) -> str | None:
    prop = _get_prop(page, name)
    if prop and prop.get("select"):
        return prop["select"]["name"]
    return None


def _get_status(page: dict, name: str) -> str | None:
    prop = _get_prop(page, name)
    if prop and prop.get("status"):
        return prop["status"]["name"]
    return None


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


def _get_text(page: dict, name: str) -> str | None:
    """Read a rich_text property as a plain string."""
    prop = _get_prop(page, name)
    if not prop:
        return None
    parts = prop.get("rich_text", [])
    return "".join(p.get("plain_text", "") for p in parts) or None


def _get_title(page: dict) -> str:
    """Return the title of a page as a plain string."""
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(p.get("plain_text", "") for p in prop.get("title", []))
    return ""


def _get_relation_ids(page: dict, name: str) -> list[str]:
    prop = _get_prop(page, name)
    if not prop:
        return []
    return [r["id"] for r in prop.get("relation", [])]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_local_iso() -> str:
    """Current local datetime as a full ISO string with timezone offset.
    Use this for Closed Date stamps so the stored datetime reflects the local
    timezone rather than UTC."""
    return datetime.now().astimezone().isoformat()


# Cache: database_id -> {option_id: group_name}
_status_group_cache: dict[str, dict[str, str]] = {}


def _get_status_group(client: "NotionClient", page: dict | None, status_field: str) -> str | None:
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


# ------------------------------------------------------------------ #
#  Period key
# ------------------------------------------------------------------ #

def _period_key(period: str, dt: datetime) -> str:
    """Canonical string identifying the period a datetime falls in."""
    if period == "Day":
        return dt.strftime("%Y-%m-%d")
    if period == "Week":
        return dt.strftime("%G-W%V")  # ISO week year + week number
    if period == "Month":
        return dt.strftime("%Y-%m")
    if period == "Year":
        return dt.strftime("%Y")
    return dt.strftime("%Y-%m-%d")


# ------------------------------------------------------------------ #
#  Due date calculation
# ------------------------------------------------------------------ #

def _period_dates(period: str, anchor_day: int | None, use_next: bool, now: datetime) -> tuple[datetime, datetime | None]:
    """
    Return (target_date, end_date) for either the current or next period.
    end_date is None for single-day targets; set for full-span targets.
    """
    if period == "Day":
        target = now + timedelta(days=1) if use_next else now
        return target.replace(hour=0, minute=0, second=0, microsecond=0), None

    if period == "Week":
        if anchor_day:
            target_weekday = anchor_day - 1  # Python: 0=Mon, 6=Sun
            days_ahead = target_weekday - now.weekday()
            if use_next and days_ahead <= 0:
                days_ahead += 7
            elif not use_next and days_ahead < 0:
                days_ahead += 7
            target = now + timedelta(days=days_ahead)
            return target.replace(hour=0, minute=0, second=0, microsecond=0), None
        else:
            days_to_monday = (7 - now.weekday()) % 7 or 7 if use_next else -now.weekday()
            monday = (now + timedelta(days=days_to_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
            sunday = monday + timedelta(days=6)
            return monday, sunday

    if period == "Month":
        if use_next:
            month = now.month + 1 if now.month < 12 else 1
            year = now.year if now.month < 12 else now.year + 1
        else:
            month, year = now.month, now.year
        max_day = calendar.monthrange(year, month)[1]
        if anchor_day:
            day = min(anchor_day, max_day)
            return datetime(year, month, day).astimezone(), None
        else:
            start = datetime(year, month, 1).astimezone()
            end = datetime(year, month, max_day).astimezone()
            return start, end

    if period == "Year":
        year = now.year + 1 if use_next else now.year
        if anchor_day:
            # Anchor day = day of year (1-365)
            base = datetime(year, 1, 1).astimezone()
            target = base + timedelta(days=min(anchor_day, 365) - 1)
            return target, None
        else:
            start = datetime(year, 1, 1).astimezone()
            end = datetime(year, 12, 31).astimezone()
            return start, end

    # Fallback
    return now + timedelta(days=1), None


def _calc_due_date(
    cadence_type: str | None,
    period: str | None,
    anchor_day: int | None,
    anchor_time: str | None,
    use_next_period: bool,
    task_type: str | None = None,
) -> dict | None:
    """Return a Notion date property dict for the new task's Due Date, or None."""
    if cadence_type in ("Unlimited", "At most N per period") or not period:
        return None
    if task_type in ("Habit", "Bad Habit"):
        return None

    now = datetime.now().astimezone()
    target, end = _period_dates(period, anchor_day, use_next_period, now)

    if anchor_day and anchor_time:
        # Specific day + time
        try:
            hour, minute = (int(p) for p in anchor_time.strip().split(":"))
            dt = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return {"date": {"start": dt.isoformat(), "end": None}}
        except Exception:
            logger.warning(f"Could not parse anchor_time '{anchor_time}' — falling back to date only.")
            return {"date": {"start": target.strftime("%Y-%m-%d"), "end": None}}

    if anchor_day and not anchor_time:
        return {"date": {"start": target.strftime("%Y-%m-%d"), "end": None}}

    # No anchor — full period span
    if end:
        return {"date": {"start": target.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")}}
    return {"date": {"start": target.strftime("%Y-%m-%d"), "end": None}}


# ------------------------------------------------------------------ #
#  Period target text
# ------------------------------------------------------------------ #

def _build_period_target(cadence_type: str | None, cadence_n: float | None, period: str | None) -> str:
    n = int(cadence_n) if cadence_n is not None else "N"
    p = period or "period"
    if cadence_type == "Once per period":
        return f"1 per {p}"
    if cadence_type == "At most N per period":
        return f"At most {n} per {p}"
    if cadence_type == "Exactly N per period":
        return f"Exactly {n} per {p}"
    if cadence_type == "Minimum N per period":
        return f"Minimum {n} per {p}"
    if cadence_type == "Unlimited":
        return "Unlimited"
    return ""


# ------------------------------------------------------------------ #
#  Field copying
# ------------------------------------------------------------------ #

def _copy_task_fields(closed_task: dict) -> dict:
    """Copy properties from a closed task, excluding bot-managed and read-only fields."""
    props = {}
    for name, prop in closed_task.get("properties", {}).items():
        if name in FIELDS_NOT_INHERITED:
            continue
        prop_type = prop.get("type")
        if not prop_type or prop_type in _READONLY_PROP_TYPES:
            continue
        props[name] = {prop_type: prop.get(prop_type)}
    return props


# ------------------------------------------------------------------ #
#  Instance # counting helpers
# ------------------------------------------------------------------ #

def _parse_closed_dt(closed_date_str: str) -> datetime | None:
    """Parse a Closed Date string into a local-timezone-aware datetime, or return None."""
    try:
        if "T" in closed_date_str:
            return datetime.fromisoformat(closed_date_str.replace("Z", "+00:00")).astimezone()
        return datetime.strptime(closed_date_str, "%Y-%m-%d").astimezone()
    except Exception:
        return None


def _normalize_status(s: str | None) -> str:
    """Normalize a status string for comparison: lowercase, letters only.

    Strips whitespace, dashes, punctuation, etc. so that "Cancelled",
    "cancelled", "CANCELLED", and "cancel led" all normalize to "cancelled".
    """
    if not s:
        return ""
    return re.sub(r'[^a-z]', '', s.lower())


def _is_open(task: dict) -> bool:
    """Return True if a task has no Closed Date and is not a non-completion status.

    Used for period-tracking logic where a "still open" task is one the user
    has not yet completed or explicitly cancelled.  Note: tasks that were just
    completed in the current poll cycle may not yet have a Closed Date stamped
    (auto_closed_date writes it in the same update), so callers that need to
    distinguish truly-open from just-completed should use _get_status_group
    instead (requires a client call to look up the status group schema).
    """
    if _normalize_status(_get_status(task, "Status")) in NON_COMPLETION_STATUSES:
        return False
    return _get_date(task, "Closed Date") is None


def _task_in_period(
    task: dict,
    period: str | None,
    current_period_key: str | None,
) -> bool:
    """Return True if a task belongs to the current period.

    Closed tasks: determined by Closed Date (ground truth). Period Key field
    is ignored once a task is closed — it remains as a human-readable label
    in Notion but has no effect on bot logic.
    Open tasks:   determined by Period Key field (no Closed Date available).

    Auto-cancelled tasks are correctly attributed to their due period because
    governance stamps Closed Date = min(_period_end(period, due), yesterday 23:59),
    placing the cancellation at the last moment of the due period — or yesterday
    if that period hasn't ended yet — rather than the day governance ran.
    """
    if not current_period_key:
        return False
    # Non-completion statuses (cancelled, skipped, missed, etc.) do not count —
    # they represent missed/skipped attempts, not completions.
    if _normalize_status(_get_status(task, "Status")) in NON_COMPLETION_STATUSES:
        return False
    closed_date_str = _get_date(task, "Closed Date")
    if closed_date_str:
        dt = _parse_closed_dt(closed_date_str)
        if dt and period:
            return _period_key(period, dt) == current_period_key
        return False
    return _get_text(task, "Period Key (Recurring Task)") == current_period_key


def _count_tasks_in_period(
    client: "NotionClient",
    definition_id: str,
    period: str | None,
    current_period_key: str | None,
) -> int:
    """Count tasks for this RTD in the current period via API call.

    Used at task creation time when a pre-fetched task list is unavailable.
    """
    if not _tasks_db_id or not current_period_key:
        return 0
    try:
        tasks = client.query_database(
            _tasks_db_id,
            filter_payload={"property": "Recurring Series", "relation": {"contains": definition_id}},
        )
    except Exception as e:
        logger.warning(f"Could not count tasks for {definition_id}: {e}")
        return 0
    return sum(1 for t in tasks if _task_in_period(t, period, current_period_key))


def _count_tasks_in_period_from_list(
    tasks: list[dict],
    definition_id: str,
    period: str | None,
    current_period_key: str | None,
) -> int:
    """Count tasks for this RTD in the current period from an already-fetched list.

    Used inside run_recurring_governance where all_tasks is already available.
    """
    if not current_period_key:
        return 0
    return sum(
        1 for t in tasks
        if definition_id in _get_relation_ids(t, "Recurring Series")
        and _task_in_period(t, period, current_period_key)
    )


# ------------------------------------------------------------------ #
#  Grace period check
# ------------------------------------------------------------------ #

def _get_due_end_or_start(page: dict) -> datetime | None:
    """Return the due date end (if set) or start as a local-timezone-aware datetime, or None."""
    prop = _get_prop(page, "Due Date")
    if not prop:
        return None
    date = prop.get("date")
    if not date:
        return None
    date_str = date.get("end") or date.get("start")
    if not date_str:
        return None
    try:
        if "T" in date_str:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone()
        return datetime.strptime(date_str, "%Y-%m-%d").astimezone()
    except Exception:
        return None


def _is_overdue_by(due: datetime, grace_days: float) -> bool:
    now = datetime.now().astimezone()
    return (now - due).days > grace_days


def _period_start(period: str | None, now: datetime) -> datetime:
    """Return the UTC start of the current period (calendar boundary, ignoring anchor day)."""
    if period == "Week":
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "Month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "Year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    # Day (or unknown): today's midnight UTC
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _period_end(period: str | None, dt: datetime) -> datetime:
    """Return 23:59:00 on the last day of the period that `dt` falls in.

    Used to set Closed Date on auto-cancelled tasks so the cancellation is
    attributed to the period the task was *due* in, not the day governance ran.
    """
    if period == "Week":
        # ISO week ends on Sunday; dt.weekday(): 0=Mon … 6=Sun
        sunday = dt + timedelta(days=6 - dt.weekday())
        return sunday.replace(hour=23, minute=59, second=0, microsecond=0)
    if period == "Month":
        last_day = calendar.monthrange(dt.year, dt.month)[1]
        return dt.replace(day=last_day, hour=23, minute=59, second=0, microsecond=0)
    if period == "Year":
        return dt.replace(month=12, day=31, hour=23, minute=59, second=0, microsecond=0)
    # Day (or unknown): end of that day
    return dt.replace(hour=23, minute=59, second=0, microsecond=0)


# ------------------------------------------------------------------ #
#  Task creation
# ------------------------------------------------------------------ #

def _create_next_task(
    client: "NotionClient",
    closed_task: dict | None,
    definition: dict,
    tasks_db_id: str | None = None,
    force_next_period: bool = False,
) -> None:
    """Create the next recurring task based on the closed task and its definition."""
    cadence_type = _get_select(definition, "Cadence Type")
    if cadence_type == "N per period":  # legacy name, normalized to current name
        cadence_type = "Exactly N per period"
    task_type    = _get_select(definition, "Type")
    period = _get_select(definition, "Period")
    anchor_day_raw = _get_number(definition, "Anchor Day")
    anchor_day = int(anchor_day_raw) if anchor_day_raw is not None else None
    anchor_time = _get_text(definition, "Anchor Time")
    cadence_n = _get_number(definition, "Cadence N")
    definition_id = definition["id"]

    now = datetime.now().astimezone()
    current_period_key = _period_key(period, now) if period else None

    # Determine whether we're in a new period relative to the closed task.
    # Closed Date is the ground truth, but it may not be stamped yet when this runs
    # (auto_closed_date collects the write in the same poll cycle, before it's committed).
    # Fallback: use the task's Period Key field. If both are unavailable, assume new period.
    if closed_task is None:
        # Governance is creating a task because none exists.
        # Normally targets the current period, but caller can force next period
        # (e.g. when the current period's allotment is already consumed by cancelled tasks).
        use_next_period = force_next_period
    else:
        closed_period_key = None
        closed_date_str = _get_date(closed_task, "Closed Date")
        if closed_date_str and period:
            dt = _parse_closed_dt(closed_date_str)
            if dt:
                closed_period_key = _period_key(period, dt)
        if closed_period_key is None and period:
            closed_period_key = _get_text(closed_task, "Period Key (Recurring Task)")
        new_period = (current_period_key != closed_period_key) if (current_period_key and closed_period_key) else True

        if cadence_type in ("Once per period", "At most N per period") or new_period:
            use_next_period = True
        else:
            use_next_period = False

    # Compute the Period Key of the period we're targeting for the new task.
    if period:
        target_date, _ = _period_dates(period, anchor_day, use_next_period, now)
        target_period_key = _period_key(period, target_date)
    else:
        target_period_key = current_period_key

    # Fetch all tasks for this definition: used for the duplicate guard and Instance # count.
    # A single query here avoids two round-trips.
    fetched_tasks: list[dict] = []
    if _tasks_db_id:
        try:
            fetched_tasks = client.query_database(
                _tasks_db_id,
                filter_payload={"property": "Recurring Series", "relation": {"contains": definition_id}},
            )
        except Exception as e:
            logger.warning(f"Could not fetch tasks for {definition_id} before creation: {e}")

    # Guard: skip creation if an open task already exists for the target period.
    # Uses _get_status_group (not _is_open) so that a just-completed task whose
    # Closed Date hasn't been written yet is correctly seen as Complete, not open.
    if target_period_key:
        for t in fetched_tasks:
            if _get_status_group(client, t, "Status") == "Complete":
                continue  # already closed or cancelled — not a duplicate
            t_due = _get_date(t, "Due Date")
            if t_due and period:
                pk_dt = _parse_closed_dt(t_due) or now
                t_pk = _period_key(period, pk_dt)
            else:
                t_pk = _get_text(t, "Period Key (Recurring Task)")
            if t_pk == target_period_key:
                logger.info(
                    f"Skipping task creation for definition {definition_id}: "
                    f"open task already exists for period {target_period_key}."
                )
                return

    # Instance # = completions in target period (non-cancelled) + 1.
    new_instance = sum(1 for t in fetched_tasks if _task_in_period(t, period, target_period_key)) + 1

    # Build properties — always use the definition title, never copy from the closed task.
    def_name = _get_title(definition) or "Recurring Task"
    if closed_task is not None:
        props = _copy_task_fields(closed_task)
    else:
        props = {}
    props["Name"] = {"title": [{"type": "text", "text": {"content": def_name}}]}

    props["Recurring Series"] = {"relation": [{"id": definition_id}]}
    props["Instance # (Recurring Task)"] = {"number": new_instance}

    if target_period_key:
        props["Period Key (Recurring Task)"] = {"rich_text": [{"type": "text", "text": {"content": target_period_key}}]}

    period_target = _build_period_target(cadence_type, cadence_n, period)
    if period_target:
        props["Period Target (Recurring Task)"] = {"rich_text": [{"type": "text", "text": {"content": period_target}}]}

    due_date = _calc_due_date(cadence_type, period, anchor_day, anchor_time, use_next_period, task_type)
    if due_date:
        props["Due Date"] = due_date

    # Resolve target database
    db_id = tasks_db_id
    if not db_id and closed_task:
        db_id = closed_task.get("parent", {}).get("database_id")
    if not db_id:
        db_id = _tasks_db_id
    if not db_id:
        logger.error("Cannot create recurring task: no tasks database ID available.")
        return

    try:
        client.create_page(db_id, props)
        logger.info(f"Created recurring task for definition {definition_id}, instance #{new_instance}.")
    except Exception as e:
        logger.error(f"Failed to create recurring task for definition {definition_id}: {e}")


# ------------------------------------------------------------------ #
#  Startup governance
# ------------------------------------------------------------------ #

def run_recurring_governance(client: "NotionClient") -> None:
    """
    Governance pass for recurring task definitions.

    For each active definition:
      - Ensures at least one open task covers the current period; creates one if not.
        Multiple open tasks across different periods is explicitly allowed — the user
        may pre-create future-period tasks with their Due Dates set in advance.
      - Corrects Period Key and Instance # drift on every open task.
      - Auto-cancels overdue Responsibility tasks (past grace period or period cap).
      - Alerts on At-most-N cap breaches.
    """
    if not _definitions_db_id or not _tasks_db_id:
        return

    logger.info("Running recurring task governance ...")

    try:
        definitions = client.query_database(
            _definitions_db_id,
            filter_payload={"property": "Active", "checkbox": {"equals": True}},
        )
    except Exception as e:
        logger.error(f"Failed to fetch recurring definitions: {e}")
        return

    try:
        all_tasks = client.query_database(_tasks_db_id)
    except Exception as e:
        logger.error(f"Failed to fetch tasks for recurring governance: {e}")
        return

    # Register all RTD pages as examined so Bot Notes are cleared on resolved issues
    for defn in definitions:
        mark_page_examined(defn["id"])

    # --- Build map: definition_id -> [open task pages] ---
    open_tasks_by_def: dict[str, list] = {}
    for task in all_tasks:
        series = _get_relation_ids(task, "Recurring Series")
        if not series:
            continue
        if _get_status_group(client, task, "Status") == "Complete":
            continue
        open_tasks_by_def.setdefault(series[0], []).append(task)

    # --- Per-definition checks ---
    now = datetime.now().astimezone()
    for definition in definitions:
        def_id = definition["id"]
        def_name = _get_title(definition)
        cadence_type = _get_select(definition, "Cadence Type")
        if cadence_type == "N per period":  # legacy name, normalized to current name
            cadence_type = "Exactly N per period"
        cadence_n = _get_number(definition, "Cadence N")
        period = _get_select(definition, "Period")
        current_period_key = _period_key(period, now) if period else None
        open_tasks = open_tasks_by_def.get(def_id, [])

        # --- Grace period: auto-cancel overdue Responsibility tasks ---
        # Runs before the 0/multiple-open check so cancelled tasks don't count.
        task_type = _get_select(definition, "Type")
        cancelled_ids: set[str] = set()
        if task_type == "Responsibility":
            do_not_autoclose = bool((_get_prop(definition, "Do Not Autoclose") or {}).get("checkbox"))
            if not do_not_autoclose:
                grace = _get_number(definition, "Grace Period (days)")
                if grace is None:
                    grace = 0  # No grace period set → cancel on due date
                cur_period_start = _period_start(period, now)
                past_cap = now >= cur_period_start + timedelta(days=PERIOD_CAP_DAYS)
                for task in open_tasks:
                    ignore_prop = _get_prop(task, "Ignore Grace Period (Recurring Task)")
                    if ignore_prop and ignore_prop.get("checkbox"):
                        continue
                    due = _get_due_end_or_start(task)
                    if not due:
                        continue
                    task_pk = _get_text(task, "Period Key (Recurring Task)")
                    # A task is stale only if its Period Key is BEFORE the current period
                    # (lexicographic comparison works for all period key formats).
                    # Tasks with a FUTURE period key are pre-created and must not be cancelled.
                    stale = task_pk is not None and current_period_key is not None and task_pk < current_period_key
                    if _is_overdue_by(due, grace) or (stale and past_cap):
                        reason = "grace period expired" if _is_overdue_by(due, grace) else "period cap (stale task in new period)"
                        task_name = _get_title(task)
                        logger.info(f"Auto-cancelling task '{task_name}' ({task['id']}) for '{def_name}': {reason}.")
                        # Set Closed Date to the last moment of the period the Due Date
                        # falls in (e.g. April 30 23:59 for a Monthly task due in April),
                        # but no later than yesterday at 23:59 — if the period hasn't ended
                        # yet (today is still inside it), a future Closed Date would be wrong.
                        yesterday_end = (now - timedelta(days=1)).replace(
                            hour=23, minute=59, second=0, microsecond=0
                        )
                        close_date = min(_period_end(period, due), yesterday_end).isoformat()
                        cancel_updates = {"Status": {"status": {"name": "Cancelled"}}}
                        cancel_updates["Closed Date"] = {"date": {"start": close_date}}
                        try:
                            client.update_page_properties(task["id"], cancel_updates)
                            cancelled_ids.add(task["id"])
                        except Exception as e:
                            logger.error(f"Failed to cancel overdue task {task['id']}: {e}")

        remaining_open = [t for t in open_tasks if t["id"] not in cancelled_ids]

        # --- Ensure a task exists for the current period ---
        # Multiple open tasks across different periods is explicitly allowed (e.g. the user
        # pre-creates tasks for future periods with their Due Dates filled in advance).
        # We only create a new task when NO open task covers the current period.
        if current_period_key:
            has_current_period_task = any(
                (_period_key(period, _parse_closed_dt(_get_date(t, "Due Date")) or now) == current_period_key
                 if (_get_date(t, "Due Date") and period)
                 else _get_text(t, "Period Key (Recurring Task)") == current_period_key)
                for t in remaining_open
            )
        else:
            # No period defined — any open task counts.
            has_current_period_task = bool(remaining_open)

        if not has_current_period_task:
            # Check whether the current period's allotment has already been consumed.
            # Uses all_tasks (fetched once at the top of this governance pass) so that
            # cancellations made earlier in the same pass are visible without relying
            # on Notion API propagation.
            force_next = False
            if period and cadence_type != "Unlimited" and current_period_key:
                if cadence_type == "Once per period":
                    n_threshold = 1
                elif cadence_n is not None and cadence_type in ("At most N per period", "Exactly N per period", "Minimum N per period"):
                    n_threshold = int(cadence_n)
                else:
                    n_threshold = None
                if n_threshold is not None:
                    # Only completions count — skipped/cancelled tasks are retried,
                    # not treated as consuming the period quota.
                    count_all = sum(
                        1 for t in all_tasks
                        if def_id in _get_relation_ids(t, "Recurring Series")
                        and _task_in_period(t, period, current_period_key)
                    )
                    if count_all >= n_threshold:
                        force_next = True
                        logger.info(
                            f"Recurring governance: '{def_name}' current period already has "
                            f"{count_all} completion(s) — targeting next period."
                        )
            logger.info(f"Recurring governance: no open task for '{def_name}' in current period — creating one.")
            _create_next_task(client, None, definition, force_next_period=force_next)

        # --- Correct Period Key and Instance # on every open task ---
        # Group open tasks by their expected Period Key (derived from Due Date when
        # available, falling back to the current period for no-date tasks).
        open_by_period: dict[str, list] = {}
        for task in remaining_open:
            due = _get_date(task, "Due Date")
            if due and period:
                pk_dt = _parse_closed_dt(due) or now
                task_pk = _period_key(period, pk_dt)
            else:
                task_pk = current_period_key or ""
            open_by_period.setdefault(task_pk, []).append(task)

        for group_pk, group_tasks in open_by_period.items():
            # Count = completed (non-skipped/cancelled) closed tasks in this period
            # plus the open tasks being assigned to this period group.
            closed_count = sum(
                1 for t in all_tasks
                if (def_id in _get_relation_ids(t, "Recurring Series")
                    and _normalize_status(_get_status(t, "Status")) not in NON_COMPLETION_STATUSES
                    and _get_date(t, "Closed Date")
                    and period is not None
                    and _period_key(period, _parse_closed_dt(_get_date(t, "Closed Date")) or now) == group_pk)
            )
            count = closed_count + len(group_tasks)

            # No-Due-Date tasks get the highest Instance #s (order among them is arbitrary).
            # Dated tasks sorted newest-first get the next highest numbers so the
            # oldest Due Date always corresponds to the lowest open-task Instance #.
            # Result: closed (lowest) → dated open (mid, oldest date first) → no-date open (highest).
            no_date = [t for t in group_tasks if not _get_date(t, "Due Date")]
            dated = sorted(
                [t for t in group_tasks if _get_date(t, "Due Date")],
                key=lambda t: _get_date(t, "Due Date"),
                reverse=True,  # newest first → highest Instance # among dated tasks
            )
            ordered = no_date + dated  # no-date first (get count, count-1, …)

            for i, task in enumerate(ordered):
                expected_instance = count - i
                current_instance = _get_number(task, "Instance # (Recurring Task)")
                current_pk = _get_text(task, "Period Key (Recurring Task)")

                drift_updates: dict = {}

                if group_pk and current_pk != group_pk:
                    logger.info(
                        f"Governance: correcting Period Key on {task['id']} for '{def_name}': "
                        f"{current_pk!r} → {group_pk!r}"
                    )
                    drift_updates["Period Key (Recurring Task)"] = {
                        "rich_text": [{"type": "text", "text": {"content": group_pk}}]
                    }

                if expected_instance > 0 and current_instance != expected_instance:
                    logger.info(
                        f"Governance: correcting Instance # on {task['id']} for '{def_name}': "
                        f"{current_instance} → {expected_instance}"
                    )
                    drift_updates["Instance # (Recurring Task)"] = {"number": expected_instance}

                if drift_updates:
                    try:
                        client.update_page_properties(task["id"], drift_updates)
                    except Exception as e:
                        logger.error(f"Failed to correct open task {task['id']}: {e}")

        # --- At most N alert (breach only — count == N is the happy path) ---
        if cadence_type == "At most N per period" and cadence_n is not None:
            count = _count_tasks_in_period_from_list(all_tasks, def_id, period, current_period_key)
            if count > int(cadence_n):
                add_bot_note(
                    def_id,
                    RTD_AT_MOST_N_REACHED,
                    f"'At most N per period' cap of {int(cadence_n)} exceeded "
                    f"({count} task(s) exist this period). Manual cleanup needed.",
                )
                logger.info(f"At-most-N cap exceeded for '{def_name}': {count}/{int(cadence_n)}")

    logger.info("Recurring task governance complete.")


# ------------------------------------------------------------------ #
#  Automation function
# ------------------------------------------------------------------ #

def auto_recurring_tasks(client: "NotionClient", page: dict, prev_page: dict | None) -> dict:
    """
    Triggered when a recurring task enters the Complete status group (Done or Cancelled).
    Creates the next task in the series.

    Also initializes Period Key, Instance #, Period Target, and Due Date on tasks
    that are linked to a series but were never stamped by the bot.
    """
    if not _definitions_db_id:
        return {}

    series_ids = _get_relation_ids(page, "Recurring Series")
    if not series_ids:
        return {}

    try:
        definition = client.get_page(series_ids[0])
    except Exception as e:
        logger.error(f"Could not fetch recurring definition {series_ids[0]}: {e}")
        return {}

    if not definition.get("properties", {}).get("Active", {}).get("checkbox"):
        return {}

    current_group = _get_status_group(client, page, "Status")
    prev_group = _get_status_group(client, prev_page, "Status")

    # Transition into Complete — create the next task.
    # Guard: skip non-completion statuses (cancelled, skipped, missed, etc.).
    # Governance auto-cancels overdue tasks and immediately creates the replacement;
    # the live poll would otherwise also fire and create a duplicate.
    if prev_page is not None and current_group == "Complete" and prev_group != "Complete":
        if _normalize_status(_get_status(page, "Status")) in NON_COMPLETION_STATUSES:
            logger.info(f"Recurring task {page['id']} was skipped/cancelled — skipping new task creation.")
            return {}
        logger.info(f"Recurring task {page['id']} completed — creating next task.")
        _create_next_task(client, page, definition)
        return {}

    # Initialization: task is linked to a series but has never been initialized by the bot
    # (Period Key and Instance # are both unset — happens when a task is manually created
    # and linked to a series, or when governance creates a task but fields weren't stamped)
    if current_group != "Complete":
        period_key = _get_text(page, "Period Key (Recurring Task)")
        instance_num = _get_number(page, "Instance # (Recurring Task)")
        if period_key is None and instance_num is None:
            period = _get_select(definition, "Period")
            cadence_type = _get_select(definition, "Cadence Type")
            if cadence_type == "N per period":  # legacy name, normalized to current name
                cadence_type = "Exactly N per period"
            cadence_n = _get_number(definition, "Cadence N")
            anchor_day_raw = _get_number(definition, "Anchor Day")
            anchor_day = int(anchor_day_raw) if anchor_day_raw is not None else None
            anchor_time = _get_text(definition, "Anchor Time")

            now = datetime.now().astimezone()

            # Derive Period Key from the task's Due Date when available, so a manually
            # created task with a past Due Date lands in the correct period rather than
            # the current one. Fall back to now when Due Date is absent.
            existing_due = _get_date(page, "Due Date")
            if existing_due and period:
                pk_dt = _parse_closed_dt(existing_due) or now
            else:
                pk_dt = now
            current_pk = _period_key(period, pk_dt) if period else None

            task_type = _get_select(definition, "Type")
            count = _count_tasks_in_period(client, definition["id"], period, current_pk)
            updates: dict = {"Instance # (Recurring Task)": {"number": count + 1}}
            if current_pk:
                updates["Period Key (Recurring Task)"] = {"rich_text": [{"type": "text", "text": {"content": current_pk}}]}

            period_target = _build_period_target(cadence_type, cadence_n, period)
            if period_target:
                updates["Period Target (Recurring Task)"] = {"rich_text": [{"type": "text", "text": {"content": period_target}}]}

            if not existing_due:
                due_date = _calc_due_date(cadence_type, period, anchor_day, anchor_time, use_next_period=False, task_type=task_type)
                if due_date:
                    updates["Due Date"] = due_date

            logger.info(f"Initializing uninitialized recurring task {page['id']}: {list(updates.keys())}")
            return updates

        # Task is already initialized — sync Period Target in case RTD Cadence Type changed.
        period = _get_select(definition, "Period")
        cadence_type = _get_select(definition, "Cadence Type")
        if cadence_type == "N per period":  # legacy name, normalized to current name
            cadence_type = "Exactly N per period"
        cadence_n = _get_number(definition, "Cadence N")
        expected_target = _build_period_target(cadence_type, cadence_n, period)
        current_target = _get_text(page, "Period Target (Recurring Task)")
        if expected_target != current_target:
            logger.info(f"Syncing Period Target on {page['id']}: {current_target!r} → {expected_target!r}")
            return {"Period Target (Recurring Task)": {"rich_text": [{"type": "text", "text": {"content": expected_target}}]}}

    return {}
