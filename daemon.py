"""
daemon.py
Polling loop that runs your automations on a schedule.

Usage:
    python daemon.py                   # uses config.toml
    python daemon.py --config my.toml  # use a different config file
"""

import sys
import time
import logging
import argparse
from datetime import datetime, timezone, time as dtime

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11 detected: install tomli with 'pip install tomli'")

from notion_api import NotionClient
import notion_api as _notion_api
from automations import AUTOMATIONS, GOVERNANCE, register_db as register_automation_db
from bot_notes import clear_bot_notes, flush_bot_notes
import recurring_tasks
from recurring_tasks import BOT_CREATED_PAGES_KEY

VERSION = "1.0.5"
NOTION_API_MIN_VERSION = "1.1.0"

parser = argparse.ArgumentParser(description="Notion automation daemon")
parser.add_argument(
    "--config",
    default="config.toml",
    help="Path to config file (default: config.toml)",
)
parser.add_argument(
    "--debug",
    action="store_true",
    help="Enable verbose API call logging (for manual runs only)",
)
parser.add_argument(
    "--governance-only",
    action="store_true",
    help="Run the automations init pass and GOVERNANCE functions once, then exit",
)
parser.add_argument(
    "--reconcile",
    action="store_true",
    help="Force-write Period Key, Occurrence #, and Period Target on all open recurring tasks, then exit. Use after changing RTD config during testing.",
)
args = parser.parse_args()

from logging.handlers import RotatingFileHandler

_log_level = logging.DEBUG if args.debug else logging.INFO
_log_fmt = logging.Formatter("%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s")

_stream_handler = logging.StreamHandler()
_stream_handler.setLevel(_log_level)
_stream_handler.setFormatter(_log_fmt)

_rotating_handler = RotatingFileHandler(
    "notion_daemon.log",
    maxBytes=5 * 1024 * 1024,  # 5 MB per file
    backupCount=3,             # keep notion_daemon.log + .1 .2 .3
)
_rotating_handler.setLevel(_log_level)
_rotating_handler.setFormatter(_log_fmt)

# 48-hour debug window — separate file, not rotating.
# notion_api logger stays at INFO (its DEBUG output is massive JSON response bodies).
_debug_handler = logging.FileHandler("notion_daemon_debug.log", mode="a")
_debug_handler.setLevel(logging.DEBUG)
_debug_handler.setFormatter(_log_fmt)

logging.basicConfig(
    level=logging.DEBUG,  # Root at DEBUG so _debug_handler receives all records.
    handlers=[_stream_handler, _rotating_handler, _debug_handler],
)
logging.getLogger("notion_api").setLevel(logging.INFO)
logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    """Current UTC time floored to the minute — matches Notion's last_edited_time resolution."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:00.000Z")


_EXCLUDED_PROP_TYPES = {"files"}


def _strip_files(page: dict) -> dict:
    """Remove file/attachment properties from a page before storing in the snapshot."""
    if "properties" not in page:
        return page
    cleaned = dict(page)
    cleaned["properties"] = {
        name: prop
        for name, prop in page["properties"].items()
        if prop.get("type") not in _EXCLUDED_PROP_TYPES
    }
    return cleaned


def run_governance(client: NotionClient) -> list[dict]:
    """
    Run all registered GOVERNANCE functions, then flush and reset Bot Notes.
    Called at daemon startup and on the daily governance cron (fires at day_start_hour).

    Returns a list of pages created during governance so the caller can run
    an automations init pass on them (to populate fields like Reopen Count
    and Due Date Update Count that are only written during init).
    """
    clear_bot_notes()
    created_pages: list[dict] = []
    for fn in GOVERNANCE:
        try:
            result = fn(client)
            if result:
                created_pages.extend(result)
        except Exception as e:
            logger.error(f"GOVERNANCE function '{fn.__name__}' failed: {e}")
    flush_bot_notes(client)
    clear_bot_notes()
    return created_pages


def _init_pass_on_pages(
    client: NotionClient, pages: list[dict], snapshots: dict[str, dict[str, dict]]
) -> None:
    """
    Run automations in init mode (prev_page=page) on a specific list of pages and
    insert the results into snapshots. Called after run_governance() so that pages
    created by governance get their init-pass fields (Reopen Count, Due Date Update
    Count, etc.) populated in the same daemon cycle rather than waiting for the next poll.
    """
    for page in pages:
        db_id = page.get("parent", {}).get("database_id")
        if not db_id or db_id not in snapshots:
            logger.warning(
                f"Cannot run init pass on governance-created page {page['id']}: "
                f"db_id {db_id!r} not in snapshots — skipping."
            )
            continue
        post_edit, extra_created = run_automations_on_page(client, page, page)
        final = post_edit if post_edit is not None else page
        snapshots[db_id][final["id"]] = _strip_files(final)
        logger.info(f"Init pass complete on governance-created page {final['id']}.")
        for new_page in extra_created:  # init mode should never create tasks, but be safe
            new_db_id = new_page.get("parent", {}).get("database_id")
            if new_db_id and new_db_id in snapshots:
                snapshots[new_db_id][new_page["id"]] = _strip_files(new_page)


def run_automations_init_pass(client: NotionClient, database_id: str) -> dict[str, dict]:
    """
    Fetch every page in a database, run all AUTOMATIONS in init mode, and
    return the resulting snapshot.

    Automations are called with prev_page=page (same object for both). This
    means change-detection logic sees no diff and stays silent, while
    per-record governance checks (initializing missing fields, backfilling
    dates, etc.) fire correctly.

    The returned snapshot is used as the change-detection baseline for the
    first live poll, so transitions that occur between the init pass and the
    first poll are detected correctly.
    """
    logger.info(f"Running automations init pass for database {database_id} ...")
    try:
        pages = client.query_database(database_id)
    except Exception as e:
        logger.error(f"Failed to run automations init pass for {database_id}: {e}")
        return {}

    snapshot: dict[str, dict] = {}
    for page in pages:
        post_edit_page, created = run_automations_on_page(client, page, page)
        final = post_edit_page if post_edit_page is not None else page
        snapshot[final["id"]] = _strip_files(final)
        for new_page in created:  # will be empty during init pass, but handled for safety
            snapshot[new_page["id"]] = _strip_files(new_page)

    logger.info(f"  → Automations init pass complete ({len(pages)} page(s)).")
    return snapshot


def run_automations_on_page(
    client: NotionClient, page: dict, prev_page: dict | None
) -> tuple[dict | None, list[dict]]:
    """Run all registered automations for a single page and apply updates.

    prev_page=None means the page is being seen for the first time mid-run.
    Automations still fire so data governance (initializing missing fields,
    stamping closed dates, etc.) takes effect immediately on new pages.

    Returns (post_edit_page, created_pages):
      - post_edit_page: the page as returned by the Notion API after bot writes,
        or None if no writes were made. Use this as the snapshot entry so the
        next poll's prev_page reflects the bot's changes rather than the pre-poll
        state. Only genuine user changes after the bot's writes will appear as diffs.
      - created_pages: pages created by automations (e.g. auto_recurring_tasks).
        Insert these into the snapshot so the next poll has a valid prev_page for them.
    """
    if prev_page is None:
        logger.info(f"First time seeing page {page['id']} — running automations for initial governance")

    updates = {}
    created_pages: list[dict] = []

    for fn in AUTOMATIONS:
        try:
            result = fn(client, page, prev_page)
            # Strip the bot-created-pages sentinel key before building field updates.
            new_pages = result.pop(BOT_CREATED_PAGES_KEY, [])
            created_pages.extend(new_pages)
            updates.update(result)
        except Exception as e:
            logger.error(f"Automation '{fn.__name__}' failed on page {page['id']}: {e}")

    post_edit_page: dict | None = None
    if updates:
        logger.info(f"Updating page {page['id']} with: {list(updates.keys())}")
        try:
            post_edit_page = client.update_page_properties(page["id"], updates)
        except Exception as e:
            logger.error(f"Failed to update page {page['id']}: {e}")

    return post_edit_page, created_pages


def poll_database(client: NotionClient, database_id: str, snapshot: dict[str, dict], since: str) -> dict[str, dict]:
    """
    Fetch pages changed since `since` (ISO timestamp), run automations, return updated snapshot.
    snapshot: {page_id: page_dict} from the previous poll
    """
    logger.info(f"Polling database {database_id} for changes since {since} ...")
    filter_payload = {
        "timestamp": "last_edited_time",
        "last_edited_time": {"on_or_after": since},
    }
    try:
        pages = client.query_database(database_id, filter_payload=filter_payload)
    except Exception as e:
        logger.error(f"Failed to query database {database_id}: {e}")
        return snapshot

    new_snapshot = dict(snapshot)  # preserve baseline for pages not returned this poll
    for page in pages:
        page_id = page["id"]
        prev_page = snapshot.get(page_id)
        stripped = _strip_files(page)

        # Notion's on_or_after filter is inclusive, so a page whose last_edited_time
        # equals `since` will be returned again even if it hasn't actually changed.
        # Guard: same timestamp AND same content → truly unchanged, skip.
        # If content differs despite same timestamp (two edits within the same minute),
        # fall through and process normally.
        if prev_page is not None and page.get("last_edited_time") == prev_page.get("last_edited_time"):
            if stripped == prev_page:
                logger.debug(f"Skipping unchanged page {page_id} (last_edited_time boundary overlap)")
                new_snapshot[page_id] = stripped
                continue
            logger.debug(f"Page {page_id}: same last_edited_time but content changed — processing")

        post_edit_page, created = run_automations_on_page(client, page, prev_page)
        # Use the post-edit page (returned by the API after bot writes) as the snapshot
        # entry so the next poll's prev_page reflects what the bot left, not the pre-poll
        # state. Only genuine user changes after the bot's writes will appear as diffs.
        final = post_edit_page if post_edit_page is not None else page
        new_snapshot[page_id] = _strip_files(final)
        for new_page in created:
            # Run init pass immediately on bot-created pages so fields like Reopen Count
            # and Due Date Update Count are populated in this cycle, not deferred to the next poll.
            post_init, _ = run_automations_on_page(client, new_page, new_page)
            init_final = post_init if post_init is not None else new_page
            new_snapshot[init_final["id"]] = _strip_files(init_final)
            logger.info(f"Snapshot: added bot-created page {init_final['id']} (init pass complete).")

    if pages:
        logger.info(f"  → {len(pages)} changed page(s) processed.")
    else:
        logger.info(f"  → No changes.")
    return new_snapshot


def _poll_rtd_for_changes(
    client: NotionClient, rt_defs_id: str, rtd_snapshot: dict[str, dict], since: str
) -> tuple[dict[str, dict], bool]:
    """
    Poll the RTD database for changes since `since`. Returns (updated_snapshot, should_govern)
    where should_govern is True only when an RTD's Status has toggled to Active — the only
    change that requires immediate governance. All other field edits (Grace Period, N Cadence,
    Anchor Time, etc.) update the snapshot but do not trigger governance; they take effect on
    the next scheduled governance run. New RTDs created already set to Active also trigger.
    """
    filter_payload = {
        "timestamp": "last_edited_time",
        "last_edited_time": {"on_or_after": since},
    }
    try:
        pages = client.query_database(rt_defs_id, filter_payload=filter_payload)
    except Exception as e:
        logger.error(f"Failed to poll RTD database: {e}")
        return rtd_snapshot, False

    if not pages:
        return rtd_snapshot, False

    def _rtd_status(page: dict) -> str:
        return (page.get("properties", {}).get("Status", {}).get("status", {}) or {}).get("name", "")

    new_snapshot = dict(rtd_snapshot)
    should_govern = False
    for page in pages:
        stripped = _strip_files(page)
        prev = rtd_snapshot.get(page["id"])
        if (prev is not None
                and page.get("last_edited_time") == prev.get("last_edited_time")
                and stripped == prev):
            new_snapshot[page["id"]] = stripped
            continue  # boundary overlap — truly unchanged

        new_snapshot[page["id"]] = stripped
        new_status = _rtd_status(stripped)
        old_status = _rtd_status(prev) if prev is not None else None

        if new_status == "Active" and old_status != "Active":
            label = "new" if prev is None else "existing"
            logger.info(f"RTD {page['id']} ({label} RTD): Status → Active — triggering governance.")
            should_govern = True
        else:
            logger.info(f"RTD {page['id']} changed (Status={new_status!r}, unchanged) — snapshot updated, no governance trigger.")

    return new_snapshot, should_govern


def main():
    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    token = cfg.get("token")
    if not token:
        raise RuntimeError("token is not set in config.toml")

    databases = cfg.get("databases", [])
    if not databases:
        raise RuntimeError("No [[databases]] entries found in config.toml")
    database_ids = [db["id"] for db in databases]
    for db in databases:
        register_automation_db(db["id"], db)

    poll_interval = cfg.get("poll_interval", 60)

    _dsh_aliases = {"governance_hour", "day_start", "daystart_hour", "start_hour"}
    for _alias in _dsh_aliases:
        if _alias in cfg:
            logger.warning(
                f"Config key '{_alias}' is not recognized — did you mean 'day_start_hour'? Value ignored."
            )
    _raw_dsh = cfg.get("day_start_hour")
    if _raw_dsh is None:
        logger.info("day_start_hour not set in config.toml — using default 3 (3am).")
        day_start_hour = 3
    else:
        try:
            _dsh_float = float(_raw_dsh)
        except (TypeError, ValueError):
            logger.warning(f"day_start_hour value '{_raw_dsh}' is not a valid number — defaulting to 3.")
            day_start_hour = 3
        else:
            day_start_hour = int(_dsh_float)
            if day_start_hour != _dsh_float:
                logger.warning(
                    f"day_start_hour {_raw_dsh} is not a whole number — truncated to {day_start_hour}."
                )
            if not 0 <= day_start_hour <= 23:
                logger.warning(f"day_start_hour {day_start_hour} is out of range (0–23) — defaulting to 3.")
                day_start_hour = 3

    _WEEK_DAY_MAP = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    week_start_str = str(cfg.get("week_start", "Sunday")).lower()
    week_start_day = _WEEK_DAY_MAP.get(week_start_str, 6)
    if week_start_str not in _WEEK_DAY_MAP:
        logger.warning(f"Unknown week_start '{cfg.get('week_start')}' — defaulting to Sunday.")

    rt_defs_id: str | None = None
    rt_cfg = cfg.get("recurring_tasks", {})
    if rt_cfg.get("enabled"):
        rt_defs_id = rt_cfg.get("definitions_db_id")
        rt_tasks_id = rt_cfg.get("tasks_db_id")
        if rt_defs_id and rt_tasks_id:
            recurring_tasks.init(rt_defs_id, rt_tasks_id, week_start_day, day_start_hour)
        else:
            logger.warning("recurring_tasks enabled but definitions_db_id or tasks_db_id is missing — skipping.")
            rt_defs_id = None

    client = NotionClient(token, debug=args.debug)

    api_version = getattr(_notion_api, "__version__", None)
    if api_version is None:
        logger.warning("Notion_API version unknown — run: pip install -r requirements.txt")
    else:
        logger.info(f"Notion Automator v{VERSION} | Notion API v{api_version}")
        min_tuple = tuple(int(x) for x in NOTION_API_MIN_VERSION.split("."))
        installed_tuple = tuple(int(x) for x in api_version.split("."))
        if installed_tuple < min_tuple:
            logger.warning(
                f"Notion_API v{api_version} is below minimum v{NOTION_API_MIN_VERSION}"
                f" — run: pip install -r requirements.txt"
            )


    # Record startup time before the governance pass so the first poll catches
    # any changes that occur during the (potentially slow) initial fetch.
    startup_time = _utcnow_iso()

    # Run AUTOMATIONS in init mode on every page to fill in missing fields.
    # The returned snapshot is used as the change-detection baseline so that
    # transitions occurring between the init pass and the first live poll are
    # detected correctly (e.g. a task reopened immediately after daemon start).
    snapshots: dict[str, dict[str, dict]] = {
        db_id: run_automations_init_pass(client, db_id)
        for db_id in database_ids
    }

    # GOVERNANCE functions (startup pass — also runs daily at day_start_hour via cron below).
    # --reconcile sets flags before this single governance run so no second pass is needed.
    if args.reconcile:
        logger.info("--reconcile flag set: retroactively writing Period Key, Occurrence #, and Period Target on all recurring tasks (task creation skipped).")
        recurring_tasks.set_reconcile_flags(period_key=True, period_target=True, occurrence_number=True)

    gov_created = run_governance(client)
    recurring_tasks.set_reconcile_flags()  # reset (no-op if flags were never set)

    if args.reconcile:
        return

    # Run init pass on any pages created by governance so their fields (Reopen Count,
    # Due Date Update Count, etc.) are populated immediately, not deferred to the next poll.
    if gov_created:
        _init_pass_on_pages(client, gov_created, snapshots)

    if args.governance_only:
        logger.info("--governance-only flag set: exiting after governance pass.")
        return

    # Build RTD snapshot after startup governance so bot-written Bot Notes are
    # captured as the baseline — preventing them from re-triggering governance next poll.
    rtd_snapshot: dict[str, dict] = {}
    last_polled_rtd: str | None = None
    if rt_defs_id:
        try:
            rtd_pages = client.query_database(rt_defs_id)
            rtd_snapshot = {p["id"]: _strip_files(p) for p in rtd_pages}
            last_polled_rtd = _utcnow_iso()
            logger.info(f"RTD snapshot built ({len(rtd_pages)} definition(s)).")
        except Exception as e:
            logger.error(f"Failed to build RTD snapshot: {e}")

    # Subsequent polls only fetch pages edited after startup.
    last_polled: dict[str, str] = {db_id: startup_time for db_id in database_ids}

    # Track date of last GOVERNANCE run so the daily cron fires at most once per day.
    # Initialized to yesterday so the startup governance run does not suppress
    # the next cron. The startup run has already executed above; the cron
    # will fire the next time the clock reaches day_start_hour.
    from datetime import timedelta
    last_governance_date = datetime.now().date() - timedelta(days=1)

    logger.info(f"Starting Notion automation daemon.")
    logger.info(f"  Databases  : {database_ids}")
    logger.info(f"  Interval   : {poll_interval}s")
    logger.info(f"  Automations: {[fn.__name__ for fn in AUTOMATIONS]}")
    logger.info(f"  Governance : {[fn.__name__ for fn in GOVERNANCE]}")

    while True:
        # Daily GOVERNANCE cron — fires once per calendar day at day_start_hour.
        now_local = datetime.now()
        governance_time_today = datetime.combine(now_local.date(), dtime(day_start_hour, 0))
        if now_local >= governance_time_today and last_governance_date != now_local.date():
            last_governance_date = now_local.date()
            logger.info(f"{day_start_hour:02d}:00 cron: running GOVERNANCE functions.")
            gov_created = run_governance(client)
            if gov_created:
                _init_pass_on_pages(client, gov_created, snapshots)
            # Refresh RTD snapshot post-governance so bot-written Bot Notes become
            # the new baseline and don't re-trigger governance next poll.
            if rt_defs_id and last_polled_rtd is not None:
                try:
                    rtd_pages = client.query_database(rt_defs_id)
                    rtd_snapshot = {p["id"]: _strip_files(p) for p in rtd_pages}
                    last_polled_rtd = _utcnow_iso()
                except Exception as e:
                    logger.error(f"Failed to refresh RTD snapshot after governance cron: {e}")

        # RTD change detection — triggers governance only when an RTD is activated.
        if rt_defs_id and last_polled_rtd is not None:
            rtd_poll_start = _utcnow_iso()
            rtd_snapshot, rtd_activated = _poll_rtd_for_changes(
                client, rt_defs_id, rtd_snapshot, last_polled_rtd
            )
            last_polled_rtd = rtd_poll_start
            if rtd_activated:
                gov_created = run_governance(client)
                if gov_created:
                    _init_pass_on_pages(client, gov_created, snapshots)
                # Refresh RTD snapshot so bot-written Bot Notes don't re-trigger governance.
                try:
                    rtd_pages = client.query_database(rt_defs_id)
                    rtd_snapshot = {p["id"]: _strip_files(p) for p in rtd_pages}
                except Exception as e:
                    logger.error(f"Failed to refresh RTD snapshot after governance: {e}")

        for db_id in database_ids:
            poll_start = _utcnow_iso()
            snapshots[db_id] = poll_database(client, db_id, snapshots[db_id], last_polled[db_id])
            last_polled[db_id] = poll_start
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
