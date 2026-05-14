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
from datetime import datetime, timezone

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11 detected: install tomli with 'pip install tomli'")

from notion_api import NotionClient
from automations import AUTOMATIONS, GOVERNANCE
from bot_notes import clear_bot_notes, flush_bot_notes
import recurring_tasks

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
args = parser.parse_args()

from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            "notion_daemon.log",
            maxBytes=5 * 1024 * 1024,  # 5 MB per file
            backupCount=3,             # keep notion_daemon.log + .1 .2 .3
        ),
    ],
)
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


def run_governance(client: NotionClient) -> None:
    """
    Run all registered GOVERNANCE functions, then flush and reset Bot Notes.
    Called at daemon startup and on the 2am daily cron.
    """
    clear_bot_notes()
    for fn in GOVERNANCE:
        try:
            fn(client)
        except Exception as e:
            logger.error(f"GOVERNANCE function '{fn.__name__}' failed: {e}")
    flush_bot_notes(client)
    clear_bot_notes()


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
        run_automations_on_page(client, page, page)
        stripped = _strip_files(page)
        snapshot[stripped["id"]] = stripped

    logger.info(f"  → Automations init pass complete ({len(pages)} page(s)).")
    return snapshot


def run_automations_on_page(client: NotionClient, page: dict, prev_page: dict | None):
    """Run all registered automations for a single page and apply updates.

    prev_page=None means the page is being seen for the first time mid-run.
    Automations still fire so data governance (initializing missing fields,
    stamping closed dates, etc.) takes effect immediately on new pages.
    """
    if prev_page is None:
        logger.info(f"First time seeing page {page['id']} — running automations for initial governance")

    updates = {}

    for fn in AUTOMATIONS:
        try:
            result = fn(client, page, prev_page)
            updates.update(result)
        except Exception as e:
            logger.error(f"Automation '{fn.__name__}' failed on page {page['id']}: {e}")

    if updates:
        logger.info(f"Updating page {page['id']} with: {list(updates.keys())}")
        try:
            client.update_page_properties(page["id"], updates)
        except Exception as e:
            logger.error(f"Failed to update page {page['id']}: {e}")


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

        run_automations_on_page(client, page, prev_page)
        new_snapshot[page_id] = stripped

    if pages:
        logger.info(f"  → {len(pages)} changed page(s) processed.")
    else:
        logger.info(f"  → No changes.")
    return new_snapshot


def main():
    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    token = cfg.get("token")
    if not token:
        raise RuntimeError("token is not set in config.toml")

    database_ids = cfg.get("database_ids", [])
    if not database_ids:
        raise RuntimeError("database_ids is not set in config.toml")

    poll_interval = cfg.get("poll_interval", 60)

    rt_cfg = cfg.get("recurring_tasks", {})
    if rt_cfg.get("enabled"):
        rt_defs_id = rt_cfg.get("definitions_db_id")
        rt_tasks_id = rt_cfg.get("tasks_db_id")
        if rt_defs_id and rt_tasks_id:
            recurring_tasks.init(rt_defs_id, rt_tasks_id)
        else:
            logger.warning("recurring_tasks enabled but definitions_db_id or tasks_db_id is missing — skipping.")

    client = NotionClient(token, debug=args.debug)

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

    # GOVERNANCE functions (startup pass — also runs daily at 2am via cron below).
    run_governance(client)

    if args.governance_only:
        logger.info("--governance-only flag set: exiting after governance pass.")
        return

    # Subsequent polls only fetch pages edited after startup.
    last_polled: dict[str, str] = {db_id: startup_time for db_id in database_ids}

    # Track date of last GOVERNANCE run so the 2am cron fires at most once per day.
    # Initialized to yesterday so the startup governance run does not suppress
    # tonight's 2am cron. The startup run has already executed above; the cron
    # will fire the next time the clock reaches 2am.
    from datetime import timedelta
    last_governance_date = datetime.now().date() - timedelta(days=1)

    logger.info(f"Starting Notion automation daemon.")
    logger.info(f"  Databases  : {database_ids}")
    logger.info(f"  Interval   : {poll_interval}s")
    logger.info(f"  Automations: {[fn.__name__ for fn in AUTOMATIONS]}")
    logger.info(f"  Governance : {[fn.__name__ for fn in GOVERNANCE]}")

    while True:
        # 2am daily GOVERNANCE cron
        now_local = datetime.now()
        if now_local.hour == 2 and last_governance_date != now_local.date():
            last_governance_date = now_local.date()
            logger.info("2am cron: running GOVERNANCE functions.")
            run_governance(client)

        for db_id in database_ids:
            poll_start = _utcnow_iso()
            snapshots[db_id] = poll_database(client, db_id, snapshots[db_id], last_polled[db_id])
            last_polled[db_id] = poll_start
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
