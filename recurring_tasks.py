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
    RTD_EXACTLY_N_EXCEEDED,
    RTD_INVALID_ANCHOR_TIME,
)

if TYPE_CHECKING:
    from notion_api import NotionClient

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Module state — set via init()
# ------------------------------------------------------------------ #

_definitions_db_id: str | None = None
_tasks_db_id: str | None = None
_task_db_properties: set[str] = set()  # populated lazily on first governance/automation run
_week_start_day: int = 0  # 0 = Monday … 6 = Sunday; configurable via week_start in config.toml
_day_start_hour: int = 0  # Hour (0–23) when the logical day begins; midnight to this hour is "yesterday"

# Retroactive reconcile flags — set via set_reconcile_flags() before calling run_recurring_governance().
# When True, governance writes the field to ALL tasks (open, closed, cancelled) not just open ones.
_reconcile_period_key: bool = False
_reconcile_period_target: bool = False
_reconcile_occurrence_number: bool = False

# Sentinel key used by auto_recurring_tasks to return newly created pages
# back through the automation protocol so daemon.py can add them to the snapshot.
# run_automations_on_page strips this key before calling update_page_properties.
BOT_CREATED_PAGES_KEY = "__bot_created_pages__"

# Fields the user may omit from their task database. Writes to absent fields are silently skipped.
OPTIONAL_TASK_FIELDS = {
    "Period Key (Recurring Task)",
    "Occurrence # this Period (Recurring Task)",
    "Period Target (Recurring Task)",
}


def set_reconcile_flags(
    period_key: bool = False,
    period_target: bool = False,
    occurrence_number: bool = False,
) -> None:
    """Set retroactive reconcile flags before calling run_recurring_governance().

    When a flag is True, governance writes that field to ALL tasks (open, closed,
    cancelled) instead of only drift-correcting open tasks.
    Call with no arguments to reset all flags to False.
    """
    global _reconcile_period_key, _reconcile_period_target, _reconcile_occurrence_number
    _reconcile_period_key = period_key
    _reconcile_period_target = period_target
    _reconcile_occurrence_number = occurrence_number


def init(definitions_db_id: str, tasks_db_id: str, week_start_day: int = 0, day_start_hour: int = 3) -> None:
    """Call once at daemon startup to enable recurring task automation.

    week_start_day: 0 = Monday (default/ISO), 6 = Sunday, etc.
    day_start_hour: whole hour (0–23) when the logical day begins. Times from midnight to
        day_start_hour are attributed to the previous calendar day for all period
        calculations. Default 3 (3am).
    """
    global _definitions_db_id, _tasks_db_id, _week_start_day, _day_start_hour
    _definitions_db_id = definitions_db_id
    _tasks_db_id = tasks_db_id
    _week_start_day = week_start_day
    _day_start_hour = day_start_hour


def _load_task_db_schema(client: "NotionClient") -> None:
    """Query the task database schema once and cache the property names.

    Called lazily on the first governance/automation run. Subsequent calls are no-ops.
    If the query fails, optional fields are written unconditionally (safe fallback).
    """
    global _task_db_properties
    if _task_db_properties or not _tasks_db_id:
        return
    try:
        db = client.get_database(_tasks_db_id)
        _task_db_properties = set(db.get("properties", {}).keys())
        logger.info(f"Task DB schema loaded: {len(_task_db_properties)} properties found.")
    except Exception as e:
        logger.warning(f"Could not load task DB schema — optional fields written unconditionally: {e}")


def _filter_optional(props: dict) -> dict:
    """Remove optional task fields not present in the task database schema.

    Required fields always pass through. If the schema hasn't been loaded yet,
    all fields pass through (safe fallback — avoids masking bugs on first run).
    """
    if not _task_db_properties:
        return props
    return {k: v for k, v in props.items() if k not in OPTIONAL_TASK_FIELDS or k in _task_db_properties}


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
    "Occurrence # this Period (Recurring Task)",
    "Period Key (Recurring Task)",
    "Period Target (Recurring Task)",
    "Ignore Grace Period (Recurring Task)",
    "Recurring Series",
}

# RTD Status field — replaces the Active checkbox. Only RTDs with this status create tasks.
RTD_STATUS_FIELD  = "Status"
RTD_ACTIVE_STATUS = "Active"

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

# Property types that are read-only and cannot be set via the API.
# "files" is included because Notion does not support setting file attachments
# via the API on create or update — including them causes a 400 error.
_READONLY_PROP_TYPES = {
    "formula", "rollup", "created_time", "last_edited_time",
    "created_by", "last_edited_by", "unique_id", "verification",
    "button", "files",
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

def _period_dt(dt: datetime) -> datetime:
    """Shift dt back by day_start_hour so times before the logical day boundary
    are attributed to the previous calendar day.

    Example: day_start_hour=3, dt=01:30am → returns 10:30pm the previous night.
    Callers pass the result to _period_key; they do not call _period_dt themselves.
    """
    return dt - timedelta(hours=_day_start_hour)


def _week_start_date(dt: datetime) -> datetime:
    """Return midnight on the week-start day (per _week_start_day) that contains dt."""
    days_since_start = (dt.weekday() - _week_start_day) % 7
    return (dt - timedelta(days=days_since_start)).replace(hour=0, minute=0, second=0, microsecond=0)


def _period_key(period: str, dt: datetime) -> str:
    """Canonical string identifying the period a datetime falls in.

    Applies the day_start_hour offset before any date math so times between
    midnight and day_start_hour are treated as belonging to the previous period.
    """
    dt = _period_dt(dt)
    if period == "Day":
        return dt.strftime("%Y-%m-%d")
    if period == "Week":
        return _week_start_date(dt).strftime("W-%Y-%m-%d")  # date of week-start day (configurable)
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
            target = now + timedelta(days=days_ahead)
            return target.replace(hour=0, minute=0, second=0, microsecond=0), None
        else:
            start = _week_start_date(now)
            if use_next:
                start = start + timedelta(days=7)
            end = start + timedelta(days=6)
            return start, end

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
    def_id: str | None = None,
    cadence_n: float | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Return a Notion date property dict for the new task's Due Date, or None.

    Due Dates are date ranges that span the task's full logical period, adjusted
    for day_start_hour, so tasks are visible in Notion throughout their active period:
      start: period_start @ day_start_hour
      end:   next_period_start @ day_start_hour − 1 minute

    AnchorTime overrides to a single point-in-time (no end). AnchorDay narrows
    the range to that specific day within a Week/Month period. For Period=Day,
    AnchorTime alone sets the due time with no end.
    """
    if cadence_type == "Unlimited" or not period:
        return None
    if task_type == "Bad Habit":
        return None

    # "Maximum N per period" is a restriction cadence — no due date by design.
    # (Bad Habit is the intended type; other types are a misconfiguration.)
    if cadence_type == "Maximum N per period":
        if task_type != "Bad Habit":
            logger.warning(
                f"RTD {def_id}: 'Maximum per period' cadence used with "
                f"Task Type='{task_type}' — this cadence is intended for Bad Habit only. "
                "No due date will be set."
            )
        return None

    # Anchor Day/Time has no meaningful target when N>1 — all N tasks would share
    # the same date. Suppress it (with a warning) for all multi-N cadences.
    # "Once per period" is always N=1 and is exempt.
    n = int(cadence_n) if cadence_n is not None else None
    if n is not None and n > 1 and cadence_type in (
        "Exactly N per period", "Minimum N per period"
    ):
        if anchor_day is not None:
            logger.warning(
                f"RTD {def_id}: Anchor Day is set but N Cadence={n} (>1) — "
                "Anchor Day ignored for multi-task cadences."
            )
        anchor_day = None
        anchor_time = None

    if now is None:
        now = datetime.now().astimezone()

    # Shift 'now' back by day_start_hour so times between midnight and day_start_hour
    # are attributed to the previous logical period (the same offset used by _period_key).
    adjusted_now = _period_dt(now)

    # Get the period-start date (midnight-anchored) for the target period.
    target, _ = _period_dates(period, anchor_day, use_next_period, adjusted_now)

    # If anchor day has already passed in the current period, fall back to a full
    # period range — the task remains visible for the rest of the period and won't
    # appear immediately overdue. (The past-period case is handled upstream in
    # _create_next_task by advancing use_next_period=True before we reach here.)
    if anchor_day and not use_next_period and target.date() < adjusted_now.date():
        anchor_day = None
        target, _ = _period_dates(period, None, False, adjusted_now)

    # start_dt: first moment of the period (or anchor day) at day_start_hour.
    # target is always midnight-anchored from _period_dates, so this is exact.
    start_dt = target + timedelta(hours=_day_start_hour)

    def _parse_anchor_time(s: str) -> tuple[int, int]:
        h, m = (int(p) for p in s.strip().split(":"))
        return h, m

    if anchor_day:
        if anchor_time:
            # Rule 5: specific anchor day + specific time → single point-in-time
            try:
                h, m = _parse_anchor_time(anchor_time)
                due_dt = target.replace(hour=h, minute=m, second=0, microsecond=0)
                if not use_next_period and due_dt < now and now.date() == adjusted_now.date():
                    target, _ = _period_dates(period, anchor_day, True, adjusted_now)
                    due_dt = target.replace(hour=h, minute=m, second=0, microsecond=0)
                return {"date": {"start": due_dt.isoformat(), "end": None}}
            except Exception:
                logger.warning(f"Could not parse anchor_time '{anchor_time}' — falling back to single-day range.")
                if def_id:
                    add_bot_note(def_id, RTD_INVALID_ANCHOR_TIME,
                                 f"Anchor Time '{anchor_time}' could not be parsed (expected HH:MM). Due Date set to day range.")
        # Rule 4: anchor day, no anchor time → single-day range spanning that anchor day
        end_dt = start_dt + timedelta(days=1) - timedelta(minutes=1)
        return {"date": {"start": start_dt.isoformat(), "end": end_dt.isoformat()}}

    if anchor_time:
        if period == "Day":
            # Rule 3: Period=Day + anchor time → point-in-time on the logical day (no end)
            try:
                h, m = _parse_anchor_time(anchor_time)
                due_dt = target.replace(hour=h, minute=m, second=0, microsecond=0)
                if not use_next_period and due_dt < now and now.date() == adjusted_now.date():
                    due_dt += timedelta(days=1)
                return {"date": {"start": due_dt.isoformat(), "end": None}}
            except Exception:
                logger.warning(f"Could not parse anchor_time '{anchor_time}' — falling back to full-day range.")
                if def_id:
                    add_bot_note(def_id, RTD_INVALID_ANCHOR_TIME,
                                 f"Anchor Time '{anchor_time}' could not be parsed (expected HH:MM). Due Date set to day range.")
        else:
            # Anchor Time without Anchor Day for non-Day periods: meaningless without a target day.
            # Fall through to full period range.
            logger.warning(f"RTD {def_id}: Anchor Time is set but Anchor Day is not for a non-Day period — Anchor Time ignored. Due Date set to full period range.")

    # Rule 1 / Rule 2: full period range — start at day_start_hour, end at next period start − 1 min.
    next_target, _ = _period_dates(period, None, True, target)
    next_start_dt = next_target + timedelta(hours=_day_start_hour)
    end_dt = next_start_dt - timedelta(minutes=1)
    return {"date": {"start": start_dt.isoformat(), "end": end_dt.isoformat()}}


# ------------------------------------------------------------------ #
#  Period target text
# ------------------------------------------------------------------ #

def _build_period_target(cadence_type: str | None, cadence_n: float | None, period: str | None) -> str:
    n = int(cadence_n) if cadence_n is not None else "N"
    if isinstance(n, int) and n < 1:
        n = 1
    p = period or "period"
    if cadence_type == "Once per period":
        return f"1 per {p}"
    if cadence_type == "Maximum N per period":
        return f"Maximum {n} per {p}"
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
        if prop_type == "people":
            # Read format includes full user objects; write format only accepts {"id": "..."}.
            props[name] = {"people": [{"id": u["id"]} for u in (prop.get("people") or []) if u.get("id")]}
        else:
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

    Closed tasks: determined by Closed Date (ground truth). auto_closed_date governance
    backfills last_edited_time as Closed Date for any Complete task missing it, so this
    field is always populated before this function is called. The Period Key field is
    never read — it is a display-only label.
    Open tasks:   determined by Due Date. Falls back to now() if Due Date is absent —
    an open task with no Due Date always counts as belonging to the current period.

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
    # Open task (no Closed Date): use Due Date end (or start); fall back to now.
    # Closed tasks always have Closed Date set by auto_closed_date governance before this runs.
    now = datetime.now().astimezone()
    if period:
        due_dt = _get_due_end_or_start(task) or now
        return _period_key(period, due_dt) == current_period_key
    return False


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


def _is_overdue_by(due: datetime, grace_days: float, now: datetime | None = None) -> bool:
    if now is None:
        now = datetime.now().astimezone()
    return (now - due) > timedelta(days=grace_days)


def _period_start(period: str | None, now: datetime) -> datetime:
    """Return the UTC start of the current period (calendar boundary, ignoring anchor day)."""
    if period == "Week":
        return _week_start_date(now)
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
        end = _week_start_date(dt) + timedelta(days=6)
        return end.replace(hour=23, minute=59, second=0, microsecond=0)
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
    if cadence_type == "N per period":  # legacy name
        cadence_type = "Exactly N per period"
    if cadence_type == "At most N per period":  # legacy name
        cadence_type = "Maximum N per period"
    task_type    = _get_select(definition, "Type")
    if task_type not in {"Habit", "Responsibility", "Bad Habit"}:
        logger.warning(f"RTD '{def_name}': Type is '{task_type}' — unrecognized or empty, defaulting to 'Habit'.")
        task_type = "Habit"
    period = _get_select(definition, "Period")
    anchor_day_raw = _get_number(definition, "Anchor Day")
    anchor_day = int(anchor_day_raw) if anchor_day_raw is not None else None
    anchor_time = _get_text(definition, "Anchor Time")
    cadence_n = _get_number(definition, "N Cadence")
    definition_id = definition["id"]
    def_name = _get_title(definition) or "Recurring Task"

    now = datetime.now().astimezone()
    current_period_key = _period_key(period, now) if period else None

    # Fetch all tasks for this definition first — used for use_next_period determination,
    # the duplicate guard, and Instance # count.
    fetched_tasks: list[dict] = []
    if _tasks_db_id:
        try:
            fetched_tasks = client.query_database(
                _tasks_db_id,
                filter_payload={"property": "Recurring Series", "relation": {"contains": definition_id}},
            )
        except Exception as e:
            logger.warning(f"Could not fetch tasks for {definition_id} before creation: {e}")

    # Upsert closed_task into fetched_tasks so it reflects its current (just-closed) state.
    # The API query above may have captured it before the status change propagated.
    # If no Closed Date yet (auto_closed_date hasn't written it), inject now so
    # _task_in_period attributes this task to the correct period (closure time, not Due Date).
    if closed_task is not None:
        ct = closed_task
        if not _get_date(ct, "Closed Date"):
            ct = {**closed_task, "properties": {
                **closed_task.get("properties", {}),
                "Closed Date": {"type": "date", "date": {"start": now.isoformat(), "end": None}},
            }}
        fetched_tasks = [t for t in fetched_tasks if t["id"] != ct["id"]]
        fetched_tasks.append(ct)

    # Determine whether the new task targets the current period or the next one.
    if closed_task is None:
        # Governance path: caller already computed force_next_period from all_tasks.
        use_next_period = force_next_period
    else:
        # Completion trigger path: count completions in the current period from fetched_tasks
        # (which now includes the just-closed task in its final state). The pre-filled
        # Closed Date on closed_task correctly places it in its actual period via
        # _task_in_period — so a 1991 or 2078 date won't incorrectly affect this period's count.
        if cadence_type == "Once per period":
            n_threshold = 1
        elif cadence_n is not None and cadence_type in (
            "Exactly N per period", "Maximum N per period"
        ):
            n_threshold = int(cadence_n)
            if n_threshold < 1:
                logger.warning(
                    f"RTD '{def_name}': N Cadence={cadence_n} for '{cadence_type}' is < 1 — treating as 1."
                )
                n_threshold = 1
        else:
            n_threshold = None  # Unlimited / Minimum N — always stay in current period
        if n_threshold is not None:
            current_completions = sum(
                1 for t in fetched_tasks
                if _task_in_period(t, period, current_period_key)
            )
            use_next_period = current_completions >= n_threshold
        else:
            use_next_period = False

    # Compute the Period Key of the period we're targeting for the new task.
    if period:
        target_date, _ = _period_dates(period, anchor_day, use_next_period, now)
        # If anchor day is set and its date has already passed, handle based on
        # whether the anchor fell in a past period or the current one.
        if anchor_day and not use_next_period and target_date.date() < now.date():
            if _period_key(period, target_date) < current_period_key:
                # Anchor day was in a past period (e.g. Mon 5/18 in prev week with
                # Tue-start). Advance to next occurrence so the task lands in the
                # current period.
                use_next_period = True
                target_date, _ = _period_dates(period, anchor_day, True, now)
            # else: anchor day was in the current period but has already passed
            # (e.g. Wed 5/20 when today is Sat 5/23). Keep current period;
            # _calc_due_date will fall back to a full period range in this case.
        # _period_dates returns midnight-anchored dates. Passing midnight to _period_key
        # shifts it back by day_start_hour into the previous period. Use now directly
        # for the current period; add day_start_hour+1min to target_date for next period
        # so _period_key lands firmly inside the correct logical day.
        if use_next_period:
            target_period_key = _period_key(period, target_date + timedelta(hours=_day_start_hour, minutes=1))
        else:
            target_period_key = _period_key(period, now)
    else:
        target_period_key = current_period_key

    # Compute Due Date before the duplicate guard so target_period_key can be corrected
    # if _calc_due_date advances the date independently (e.g. anchor-time past-check for
    # Period=Day advances the due time to the next day while target_period_key still
    # reflects the current period — leaving them inconsistent without this correction).
    due_date = _calc_due_date(cadence_type, period, anchor_day, anchor_time, use_next_period, task_type, def_id=definition_id, cadence_n=cadence_n)
    if due_date and period:
        due_start_str = due_date.get("date", {}).get("start")
        if due_start_str:
            try:
                actual_pk = _period_key(period, datetime.fromisoformat(due_start_str))
                if actual_pk != target_period_key:
                    target_period_key = actual_pk
            except Exception:
                pass

    # Guard: skip creation if an open task already exists for the target period.
    # Uses _get_status_group (not _is_open) so that a just-completed task whose
    # Closed Date hasn't been written yet is correctly seen as Complete, not open.
    # Also excludes NON_COMPLETION_STATUSES (cancelled, skipped, etc.) — those are
    # retry candidates and must not block creation of a new task for the same period.
    if target_period_key:
        for t in fetched_tasks:
            if _get_status_group(client, t, "Status") == "Complete":
                continue  # completed — not a duplicate
            if _normalize_status(_get_status(t, "Status")) in NON_COMPLETION_STATUSES:
                continue  # cancelled/skipped — not a duplicate
            due_dt = _get_due_end_or_start(t)
            if due_dt and period:
                t_pk = _period_key(period, due_dt)
            else:
                t_pk = _period_key(period, now) if period else ""
            if t_pk == target_period_key:
                logger.info(
                    f"Skipping task creation for definition {definition_id}: "
                    f"open task already exists for period {target_period_key}."
                )
                return

    # Instance # = completions in target period (non-cancelled) + 1.
    new_instance = sum(1 for t in fetched_tasks if _task_in_period(t, period, target_period_key)) + 1

    # Build bot-managed fields (always safe to write).
    base_props: dict = {}
    base_props["Name"] = {"title": [{"type": "text", "text": {"content": def_name}}]}
    base_props["Recurring Series"] = {"relation": [{"id": definition_id}]}
    base_props["Occurrence # this Period (Recurring Task)"] = {"number": new_instance}

    if target_period_key:
        base_props["Period Key (Recurring Task)"] = {"rich_text": [{"type": "text", "text": {"content": target_period_key}}]}

    period_target = _build_period_target(cadence_type, cadence_n, period)
    if period_target:
        base_props["Period Target (Recurring Task)"] = {"rich_text": [{"type": "text", "text": {"content": period_target}}]}

    if due_date:
        base_props["Due Date"] = due_date

    # Merge inherited fields under base_props (base_props wins on conflict).
    if closed_task is not None:
        props = {**_copy_task_fields(closed_task), **base_props}
    else:
        props = base_props

    # Resolve target database
    db_id = tasks_db_id
    if not db_id and closed_task:
        db_id = closed_task.get("parent", {}).get("database_id")
    if not db_id:
        db_id = _tasks_db_id
    if not db_id:
        logger.error("Cannot create recurring task: no tasks database ID available.")
        return

    rtd_icon = definition.get("icon")
    if rtd_icon and rtd_icon.get("type") == "file":
        rtd_icon = None  # file URLs are not reliably writable via API

    try:
        new_page = client.create_page(db_id, _filter_optional(props), icon=rtd_icon)
        logger.info(f"Created recurring task for definition {definition_id}, instance #{new_instance}.")
        return new_page
    except Exception as e:
        notion_msg = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                notion_msg = e.response.json().get("message", "")
            except Exception:
                pass
        if closed_task is not None:
            logger.warning(
                f"Recurring task creation failed with inherited fields for '{def_name}' "
                f"({notion_msg or e}). Retrying without inherited fields."
            )
            try:
                new_page = client.create_page(db_id, _filter_optional(base_props), icon=rtd_icon)
                logger.info(f"Created recurring task for definition {definition_id}, instance #{new_instance} (no inherited fields).")
                return new_page
            except Exception as e2:
                logger.error(f"Failed to create recurring task for definition {definition_id}: {e2}")
                return None
        logger.error(f"Failed to create recurring task for definition {definition_id}: {e}")
        return None


# ------------------------------------------------------------------ #
#  Startup governance
# ------------------------------------------------------------------ #

def run_recurring_governance(client: "NotionClient") -> list[dict]:
    """
    Governance pass for recurring task definitions.

    For each active definition:
      - Ensures at least one open task covers the current period; creates one if not.
        Multiple open tasks across different periods is explicitly allowed — the user
        may pre-create future-period tasks with their Due Dates set in advance.
      - Corrects Period Key and Instance # drift on every open task.
      - Auto-cancels overdue Responsibility tasks (past grace period or period cap).
      - Alerts on At-most-N cap breaches.

    Returns a list of pages created during this governance pass so that daemon.py
    can run an automations init pass on them immediately (populating fields like
    Reopen Count and Due Date Update Count that are only written during init).
    """
    if not _definitions_db_id or not _tasks_db_id:
        return []

    _load_task_db_schema(client)
    if _task_db_properties and "Closed Date" not in _task_db_properties:
        logger.error(
            "CRITICAL: 'Closed Date' column not found in the task database. "
            "Recurring task period logic (occurrence counting, auto-cancel, period detection) "
            "requires this field — results will be incorrect until the column is added."
        )
    logger.info("Running recurring task governance ...")
    created_pages: list[dict] = []

    try:
        definitions = client.query_database(
            _definitions_db_id,
            filter_payload={"property": RTD_STATUS_FIELD, "status": {"equals": RTD_ACTIVE_STATUS}},
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
    # Excludes completed tasks (status group "Complete") and already-cancelled/skipped tasks
    # (NON_COMPLETION_STATUSES). Cancelled tasks are not "open" — they're dealt with and
    # should not be re-processed by the auto-cancel loop on subsequent governance runs.
    open_tasks_by_def: dict[str, list] = {}
    for task in all_tasks:
        series = _get_relation_ids(task, "Recurring Series")
        if not series:
            continue
        if _get_status_group(client, task, "Status") == "Complete":
            continue
        if _normalize_status(_get_status(task, "Status")) in NON_COMPLETION_STATUSES:
            continue
        open_tasks_by_def.setdefault(series[0], []).append(task)

    # --- Per-definition checks ---
    now = datetime.now().astimezone()
    for definition in definitions:
        def_id = definition["id"]
        def_name = _get_title(definition)
        cadence_type = _get_select(definition, "Cadence Type")
        if cadence_type == "N per period":  # legacy name
            cadence_type = "Exactly N per period"
        if cadence_type == "At most N per period":  # legacy name
            cadence_type = "Maximum N per period"
        cadence_n = _get_number(definition, "N Cadence")
        period = _get_select(definition, "Period")
        current_period_key = _period_key(period, now) if period else None
        open_tasks = open_tasks_by_def.get(def_id, [])
        anchor_time = _get_text(definition, "Anchor Time")
        anchor_day_raw = _get_number(definition, "Anchor Day")
        anchor_day = int(anchor_day_raw) if anchor_day_raw is not None else None

        # --- Anchor Time validation ---
        # Runs for every RTD so the note appears/clears regardless of whether a task is created.
        if anchor_time:
            try:
                _h, _m = (int(p) for p in anchor_time.strip().split(":"))
            except Exception:
                add_bot_note(def_id, RTD_INVALID_ANCHOR_TIME,
                             f"Anchor Time '{anchor_time}' could not be parsed (expected HH:MM). Due Date set to date only.")

        # --- Grace period: auto-cancel overdue Responsibility tasks ---
        # Runs before the 0/multiple-open check so cancelled tasks don't count.
        task_type = _get_select(definition, "Type")
        if task_type not in {"Habit", "Responsibility", "Bad Habit"}:
            logger.warning(f"RTD '{def_name}': Type is '{task_type}' — unrecognized or empty, defaulting to 'Habit'.")
            task_type = "Habit"
        cancelled_ids: set[str] = set()
        cancelled_tasks: list[dict] = []
        carried_over_ids: set[str] = set()
        reconcile_active = _reconcile_period_key or _reconcile_period_target or _reconcile_occurrence_number
        if task_type == "Responsibility" and not reconcile_active:
            do_not_autoclose = bool((_get_prop(definition, "Do Not Autoclose") or {}).get("checkbox"))
            if not do_not_autoclose:
                grace = _get_number(definition, "Grace Period (days)")
                if grace is None:
                    grace = 0  # No grace period set → cancel on due date
                elif grace < 0:
                    logger.warning(f"RTD '{def_name}': Grace Period is negative ({grace}) — defaulting to 0.")
                    grace = 0
                for task in open_tasks:
                    ignore_prop = _get_prop(task, "Ignore Grace Period (Recurring Task)")
                    if ignore_prop and ignore_prop.get("checkbox"):
                        continue
                    due = _get_due_end_or_start(task)
                    if not due:
                        continue
                    # Derive task's period from Due Date end (or start) — consistent with how
                    # all other governance period attribution works. The stored Period Key field
                    # is never read here — it may be corrupted.
                    if period:
                        task_pk = _period_key(period, _get_due_end_or_start(task) or now)
                    else:
                        task_pk = None
                    if _is_overdue_by(due, grace):
                        reason = "grace period expired"
                        task_name = _get_title(task)
                        # For "Minimum N per period": if the minimum was met in the task's
                        # period, archive instead of cancel — cancellation implies failure,
                        # but the user succeeded. The archived task disappears from queries;
                        # the existing create-task logic then creates a fresh task for the
                        # new period without triggering Due Date Update Count.
                        if cadence_type == "Minimum N per period" and task_pk and cadence_n is not None:
                            n_min = int(cadence_n)
                            completions_in_stale = sum(
                                1 for t in all_tasks
                                if def_id in _get_relation_ids(t, "Recurring Series")
                                and _task_in_period(t, period, task_pk)
                                and _get_status_group(client, t, "Status") == "Complete"
                            )
                            if completions_in_stale >= n_min:
                                carry_updates: dict = {
                                    "Occurrence # this Period (Recurring Task)": {"number": 1},
                                    "First Due Date": {"date": None},
                                    "Due Date Update Count": {"number": 0},
                                }
                                new_due = _calc_due_date(cadence_type, period, anchor_day, anchor_time, use_next_period=False, task_type=task_type, def_id=def_id, cadence_n=cadence_n)
                                if new_due:
                                    carry_updates["Due Date"] = new_due
                                if current_period_key:
                                    carry_updates["Period Key (Recurring Task)"] = {"rich_text": [{"type": "text", "text": {"content": current_period_key}}]}
                                try:
                                    client.update_page_properties(task["id"], _filter_optional(carry_updates))
                                    carried_over_ids.add(task["id"])
                                    logger.info(
                                        f"Carrying forward task '{task_name}' ({task['id']}) for '{def_name}': "
                                        f"minimum met ({completions_in_stale}/{n_min})."
                                    )
                                except Exception as e:
                                    logger.error(f"Failed to carry forward task {task['id']}: {e}")
                                continue
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
                            updated = client.update_page_properties(task["id"], cancel_updates)
                            cancelled_ids.add(task["id"])
                            cancelled_tasks.append(updated)  # post-cancel state: Status=Cancelled, Closed Date set
                        except Exception as e:
                            logger.error(f"Failed to cancel overdue task {task['id']}: {e}")

        # --- Habit: roll open tasks from a previous period forward to the current period ---
        # Also bootstraps existing Habit tasks that pre-date the Due Date feature (no Due Date set).
        if task_type == "Habit" and period and current_period_key and not reconcile_active:
            for task in open_tasks:
                due = _get_due_end_or_start(task)
                task_name = _get_title(task)
                if due is None:
                    # Existing task with no Due Date — set current period Due Date without resetting counters.
                    new_due = _calc_due_date(cadence_type, period, anchor_day, anchor_time, use_next_period=False, task_type=task_type, def_id=def_id, cadence_n=cadence_n)
                    if new_due:
                        try:
                            client.update_page_properties(task["id"], _filter_optional({"Due Date": new_due}))
                            logger.info(f"Setting initial Due Date on Habit task '{task_name}' ({task['id']}) for '{def_name}'.")
                        except Exception as e:
                            logger.error(f"Failed to set Due Date on Habit task {task['id']}: {e}")
                    continue
                if _period_key(period, due) >= current_period_key:
                    continue  # current or future period — nothing to do
                roll_updates: dict = {
                    "Occurrence # this Period (Recurring Task)": {"number": 1},
                    "First Due Date": {"date": None},
                    "Due Date Update Count": {"number": 0},
                    "Period Key (Recurring Task)": {"rich_text": [{"type": "text", "text": {"content": current_period_key}}]},
                }
                new_due = _calc_due_date(cadence_type, period, anchor_day, anchor_time, use_next_period=False, task_type=task_type, def_id=def_id, cadence_n=cadence_n)
                if new_due:
                    roll_updates["Due Date"] = new_due
                try:
                    client.update_page_properties(task["id"], _filter_optional(roll_updates))
                    carried_over_ids.add(task["id"])
                    logger.info(f"Rolling forward Habit task '{task_name}' ({task['id']}) for '{def_name}' to current period.")
                except Exception as e:
                    logger.error(f"Failed to roll forward Habit task {task['id']}: {e}")

        remaining_open = [t for t in open_tasks if t["id"] not in cancelled_ids]

        # --- Ensure a task exists for the current period ---
        # Multiple open tasks across different periods is explicitly allowed (e.g. the user
        # pre-creates tasks for future periods with their Due Dates filled in advance).
        # We only create a new task when NO open task covers the current period.
        if current_period_key:
            has_current_period_task = any(
                t["id"] in carried_over_ids
                or _period_key(period, _get_due_end_or_start(t) or now) == current_period_key
                for t in remaining_open
            ) if period else bool(remaining_open)
        else:
            # No period defined — any open task counts.
            has_current_period_task = bool(remaining_open)

        if not has_current_period_task and not (
            _reconcile_period_key or _reconcile_period_target or _reconcile_occurrence_number
        ):
            # Skip task creation in reconcile mode — reconcile corrects existing tasks only.
            # Check whether the current period's allotment has already been consumed.
            # Uses all_tasks (fetched once at the top of this governance pass) for
            # consistent quota counting across all definitions in a single pass.
            # Tasks cancelled this pass are excluded via cancelled_ids (they still
            # appear open in all_tasks since the snapshot predates the pass).
            force_next = False
            if period and cadence_type != "Unlimited" and current_period_key:
                if cadence_type == "Once per period":
                    n_threshold = 1
                elif cadence_n is not None and cadence_type in ("Maximum N per period", "Exactly N per period"):
                    n_threshold = int(cadence_n)
                else:
                    n_threshold = None
                if n_threshold is not None and n_threshold < 1:
                    logger.warning(
                        f"RTD '{def_name}': N Cadence={n_threshold} for '{cadence_type}' is < 1 — treating as 1."
                    )
                    n_threshold = 1
                if n_threshold is not None:
                    # Only completions count — skipped/cancelled tasks are retried,
                    # not treated as consuming the period quota.
                    # Exclude cancelled_ids: tasks cancelled in this pass still appear
                    # open in all_tasks (snapshot taken before the pass), so they must
                    # be excluded manually to avoid a false force_next=True.
                    count_all = sum(
                        1 for t in all_tasks
                        if def_id in _get_relation_ids(t, "Recurring Series")
                        and t["id"] not in cancelled_ids
                        and _task_in_period(t, period, current_period_key)
                    )
                    if count_all >= n_threshold:
                        force_next = True
                        logger.info(
                            f"Recurring governance: '{def_name}' current period already has "
                            f"{count_all} completion(s) — targeting next period."
                        )
            logger.info(f"Recurring governance: no open task for '{def_name}' in current period — creating one.")
            # If this period's task was auto-cancelled, use it as the inheritance source.
            # Pick the cancelled task with the most recent Due Date as the reference.
            ref_task = max(cancelled_tasks, key=lambda t: _get_due_end_or_start(t) or now) if cancelled_tasks else None
            new_page = _create_next_task(client, ref_task, definition, force_next_period=force_next)
            if new_page:
                created_pages.append(new_page)

        # --- Correct Period Key, Occurrence #, and Period Target ---
        expected_target = _build_period_target(cadence_type, cadence_n, period)

        if _reconcile_period_key or _reconcile_period_target or _reconcile_occurrence_number:
            # Retroactive reconcile: process ALL tasks for this definition (open, closed,
            # cancelled). Groups by period, then assigns Occurrence # using a counter that
            # only decrements for non-cancelled tasks — so cancelled tasks share the slot
            # number of the next non-cancelled task (same occurrence, failed attempt).
            all_for_def = [t for t in all_tasks if def_id in _get_relation_ids(t, "Recurring Series")]

            def _task_pk(task: dict) -> str:
                closed = _get_date(task, "Closed Date")
                if closed and period:
                    return _period_key(period, _parse_closed_dt(closed) or now)
                due_dt = _get_due_end_or_start(task)
                if due_dt and period:
                    return _period_key(period, due_dt)
                return current_period_key or ""

            # Group ALL tasks by period.
            tasks_by_period: dict[str, list] = {}
            for task in all_for_def:
                tasks_by_period.setdefault(_task_pk(task), []).append(task)

            for group_pk, group_tasks in tasks_by_period.items():
                # Sort oldest-first by Closed Date (if set), otherwise Due Date end (or start).
                # Using Due Date end ensures open tasks sort after any cancelled tasks that share
                # the same period slot, keeping Occurrence # stable across governance passes.
                # Tasks with neither date go at the end in arbitrary order.
                # Occurrence # starts at 1 and increments only for non-cancelled tasks,
                # so a cancelled task shares the same slot number as the task that follows it.
                def _occ_sort_key(t: dict) -> tuple:
                    closed = _get_date(t, "Closed Date")
                    if closed:
                        return (0, closed)
                    due_dt = _get_due_end_or_start(t)
                    return (0, due_dt.isoformat()) if due_dt else (1, "")

                is_cancelled_fn = lambda t: _normalize_status(_get_status(t, "Status")) in NON_COMPLETION_STATUSES
                ordered = sorted(group_tasks, key=_occ_sort_key)
                current_occurrence = 1

                for task in ordered:
                    task_cancelled = is_cancelled_fn(task)
                    rec_updates: dict = {}
                    if _reconcile_period_key and group_pk:
                        rec_updates["Period Key (Recurring Task)"] = {
                            "rich_text": [{"type": "text", "text": {"content": group_pk}}]
                        }
                    if _reconcile_occurrence_number:
                        rec_updates["Occurrence # this Period (Recurring Task)"] = {"number": current_occurrence}
                    if _reconcile_period_target and expected_target:
                        rec_updates["Period Target (Recurring Task)"] = {
                            "rich_text": [{"type": "text", "text": {"content": expected_target}}]
                        }
                    rec_updates = _filter_optional(rec_updates)
                    if rec_updates:
                        label = "cancelled " if task_cancelled else ""
                        logger.info(
                            f"Reconcile {label}'{def_name}' task {task['id']}: "
                            f"Period Key={group_pk!r}, Occurrence#={current_occurrence}, "
                            f"Period Target={expected_target!r}"
                        )
                        try:
                            client.update_page_properties(task["id"], rec_updates)
                        except Exception as e:
                            logger.error(f"Reconcile: failed to update task {task['id']}: {e}")
                    if not task_cancelled:
                        current_occurrence += 1

        else:
            # Normal governance: drift-correct all tasks in the current and future periods
            # (open, closed, and cancelled). Historical periods are left untouched.
            # Uses the same oldest-first / increment-for-non-cancelled Occurrence # logic
            # as --reconcile, but only writes when a value has actually drifted.
            # This means RTD config changes (Period, Cadence Type, N) take effect on the
            # next governance run without needing --reconcile.
            # Exclude carried_over_ids: their Due Date in all_tasks is stale (pre-carry-over),
            # so _gov_task_pk would compute the old period key and overwrite the corrected fields.
            all_for_def = [
                t for t in all_tasks
                if def_id in _get_relation_ids(t, "Recurring Series")
                and t["id"] not in carried_over_ids
            ]

            def _gov_task_pk(task: dict) -> str:
                closed = _get_date(task, "Closed Date")
                if closed and period:
                    return _period_key(period, _parse_closed_dt(closed) or now)
                due_dt = _get_due_end_or_start(task)
                if due_dt and period:
                    return _period_key(period, due_dt)
                return current_period_key or ""

            tasks_by_period: dict[str, list] = {}
            for task in all_for_def:
                pk = _gov_task_pk(task)
                # Only include current and future periods; skip historical closed tasks.
                if current_period_key and pk and pk < current_period_key:
                    continue
                tasks_by_period.setdefault(pk, []).append(task)

            def _gov_sort_key(t: dict) -> tuple:
                closed = _get_date(t, "Closed Date")
                if closed:
                    return (0, closed)
                due_dt = _get_due_end_or_start(t)
                return (0, due_dt.isoformat()) if due_dt else (1, "")

            is_cancelled_fn = lambda t: (
                t["id"] in cancelled_ids
                or _normalize_status(_get_status(t, "Status")) in NON_COMPLETION_STATUSES
            )

            for group_pk, group_tasks in tasks_by_period.items():
                ordered = sorted(group_tasks, key=_gov_sort_key)
                current_occurrence = 1

                for task in ordered:
                    task_cancelled = is_cancelled_fn(task)
                    current_instance = _get_number(task, "Occurrence # this Period (Recurring Task)")
                    current_pk = (_get_text(task, "Period Key (Recurring Task)") or "").strip() or None
                    current_target = (_get_text(task, "Period Target (Recurring Task)") or "").strip() or None

                    drift_updates: dict = {}
                    if group_pk and current_pk != group_pk:
                        logger.info(
                            f"Governance: correcting Period Key on {task['id']} for '{def_name}': "
                            f"{current_pk!r} → {group_pk!r}"
                        )
                        drift_updates["Period Key (Recurring Task)"] = {
                            "rich_text": [{"type": "text", "text": {"content": group_pk}}]
                        }
                    if current_instance != current_occurrence:
                        logger.info(
                            f"Governance: correcting Occurrence # on {task['id']} for '{def_name}': "
                            f"{current_instance} → {current_occurrence}"
                        )
                        drift_updates["Occurrence # this Period (Recurring Task)"] = {"number": current_occurrence}
                    if expected_target and current_target != expected_target:
                        logger.info(
                            f"Governance: correcting Period Target on {task['id']} for '{def_name}': "
                            f"{current_target!r} → {expected_target!r}"
                        )
                        drift_updates["Period Target (Recurring Task)"] = {
                            "rich_text": [{"type": "text", "text": {"content": expected_target}}]
                        }
                    if drift_updates:
                        try:
                            client.update_page_properties(task["id"], _filter_optional(drift_updates))
                        except Exception as e:
                            logger.error(f"Failed to correct task {task['id']}: {e}")

                    if not task_cancelled:
                        current_occurrence += 1

        # --- Maximum alert (breach only — count == N is the happy path) ---
        if cadence_type == "Maximum N per period" and cadence_n is not None:
            count = _count_tasks_in_period_from_list(all_tasks, def_id, period, current_period_key)
            if count > int(cadence_n):
                add_bot_note(
                    def_id,
                    RTD_AT_MOST_N_REACHED,
                    f"'Maximum per period' cap of {int(cadence_n)} exceeded "
                    f"({count} task(s) exist this period). Manual cleanup needed.",
                )
                logger.info(f"At-most-N cap exceeded for '{def_name}': {count}/{int(cadence_n)}")

        # --- Exactly N alert (completions exceeded N — unexpected; force_next should prevent this) ---
        if cadence_type == "Exactly N per period" and cadence_n is not None and current_period_key:
            completion_count = sum(
                1 for t in all_tasks
                if def_id in _get_relation_ids(t, "Recurring Series")
                and _task_in_period(t, period, current_period_key)
            )
            if completion_count > int(cadence_n):
                add_bot_note(
                    def_id,
                    RTD_EXACTLY_N_EXCEEDED,
                    f"'Exactly N per period' target of {int(cadence_n)} exceeded "
                    f"({completion_count} completion(s) this period). Manual cleanup may be needed.",
                )
                logger.info(f"Exactly-N exceeded for '{def_name}': {completion_count}/{int(cadence_n)}")

        # --- Write Current Period to the RTD ---
        # Governance writes a Date (start + end) representing the current period boundary
        # to the `Current Period` field on the RTD, if the field exists. Enables Notion
        # formulas and Siri Shortcuts to filter tasks by period without computing boundaries.
        if period and current_period_key and "Current Period" in definition.get("properties", {}):
            adjusted_now = _period_dt(now)
            cp_start, _ = _period_dates(period, None, False, adjusted_now)
            cp_start_dt = cp_start + timedelta(hours=_day_start_hour)
            cp_next, _ = _period_dates(period, None, True, cp_start)
            cp_end_dt = cp_next + timedelta(hours=_day_start_hour) - timedelta(minutes=1)
            try:
                client.update_page_properties(
                    def_id,
                    {"Current Period": {"date": {"start": cp_start_dt.isoformat(), "end": cp_end_dt.isoformat()}}},
                )
                logger.debug(f"Updated Current Period on RTD '{def_name}'.")
            except Exception as e:
                logger.error(f"Failed to update Current Period on RTD '{def_name}': {e}")

    logger.info("Recurring task governance complete.")
    return created_pages


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

    _load_task_db_schema(client)

    series_ids = _get_relation_ids(page, "Recurring Series")
    if not series_ids:
        return {}

    try:
        definition = client.get_page(series_ids[0])
    except Exception as e:
        logger.error(f"Could not fetch recurring definition {series_ids[0]}: {e}")
        return {}

    rtd_status = definition.get("properties", {}).get(RTD_STATUS_FIELD, {}).get("status", {}).get("name", "")
    if rtd_status != RTD_ACTIVE_STATUS:
        return {}

    current_group = _get_status_group(client, page, "Status")
    prev_group = _get_status_group(client, prev_page, "Status")

    # Transition into Complete — create the next task.
    # Applies to both normal completions and manual cancellations. The duplicate
    # guard in _create_next_task prevents double-creation when governance already
    # created a replacement in the same pass (e.g. auto-cancel of an overdue task).
    if prev_page is not None and current_group == "Complete" and prev_group != "Complete":
        action = "cancelled" if _normalize_status(_get_status(page, "Status")) in NON_COMPLETION_STATUSES else "completed"
        logger.info(f"Recurring task {page['id']} {action} — creating next task.")
        new_page = _create_next_task(client, page, definition)
        created = [new_page] if new_page is not None else []
        return {BOT_CREATED_PAGES_KEY: created}

    # Initialization: task is linked to a series but Occurrence # has never been stamped.
    # Happens when a task is manually created and linked to a series, or when governance
    # creates a task but fields weren't stamped yet.
    if current_group != "Complete":
        instance_num = _get_number(page, "Occurrence # this Period (Recurring Task)")
        if instance_num is None:
            period = _get_select(definition, "Period")
            cadence_type = _get_select(definition, "Cadence Type")
            if cadence_type == "N per period":  # legacy name
                cadence_type = "Exactly N per period"
            if cadence_type == "At most N per period":  # legacy name
                cadence_type = "Maximum N per period"
            cadence_n = _get_number(definition, "N Cadence")
            anchor_day_raw = _get_number(definition, "Anchor Day")
            anchor_day = int(anchor_day_raw) if anchor_day_raw is not None else None
            anchor_time = _get_text(definition, "Anchor Time")

            now = datetime.now().astimezone()

            # Derive Period Key from the task's Due Date when available, so a manually
            # created task with a past Due Date lands in the correct period rather than
            # the current one. Fall back to now when Due Date is absent.
            existing_due_dt = _get_due_end_or_start(page)
            pk_dt = existing_due_dt or now
            current_pk = _period_key(period, pk_dt) if period else None

            task_type = _get_select(definition, "Type")
            if task_type not in {"Habit", "Responsibility", "Bad Habit"}:
                logger.warning(f"RTD '{_get_title(definition)}': Type is '{task_type}' — unrecognized or empty, defaulting to 'Habit'.")
                task_type = "Habit"
            count = _count_tasks_in_period(client, definition["id"], period, current_pk)
            updates: dict = {"Occurrence # this Period (Recurring Task)": {"number": count + 1}}
            if current_pk:
                updates["Period Key (Recurring Task)"] = {"rich_text": [{"type": "text", "text": {"content": current_pk}}]}

            period_target = _build_period_target(cadence_type, cadence_n, period)
            if period_target:
                updates["Period Target (Recurring Task)"] = {"rich_text": [{"type": "text", "text": {"content": period_target}}]}

            if not existing_due_dt:
                due_date = _calc_due_date(cadence_type, period, anchor_day, anchor_time, use_next_period=False, task_type=task_type, def_id=definition["id"], cadence_n=cadence_n)
                if due_date:
                    updates["Due Date"] = due_date

            updates = _filter_optional(updates)
            logger.info(f"Initializing uninitialized recurring task {page['id']}: {list(updates.keys())}")
            return updates

        # Task is already initialized — sync Period Target in case RTD Cadence Type changed.
        period = _get_select(definition, "Period")
        cadence_type = _get_select(definition, "Cadence Type")
        if cadence_type == "N per period":  # legacy name
            cadence_type = "Exactly N per period"
        if cadence_type == "At most N per period":  # legacy name
            cadence_type = "Maximum N per period"
        cadence_n = _get_number(definition, "N Cadence")
        expected_target = _build_period_target(cadence_type, cadence_n, period)
        current_target = _get_text(page, "Period Target (Recurring Task)")
        if expected_target and expected_target != current_target:
            logger.info(f"Syncing Period Target on {page['id']}: {current_target!r} → {expected_target!r}")
            return _filter_optional({"Period Target (Recurring Task)": {"rich_text": [{"type": "text", "text": {"content": expected_target}}]}})

    return {}
