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
from automations import AUTOMATIONS

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
args = parser.parse_args()

logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s", # former format: "%(asctime)s [%(levelname)s] %(message)s"
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("notion_daemon.log"),
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


def run_governance_pass(client: NotionClient, database_id: str) -> None:
    """
    Fetch every page in a database and run a governance pass to fill in any
    missing field values.

    Automations are called with prev_page=page (same object for both). This
    means change-detection logic sees no diff and stays silent, while
    governance checks (initializing missing fields) fire correctly.

    Pages are processed one at a time and not retained — the snapshot starts
    empty and grows organically as pages are returned by subsequent polls.
    """
    logger.info(f"Running governance pass for database {database_id} ...")
    try:
        pages = client.query_database(database_id)
    except Exception as e:
        logger.error(f"Failed to run governance pass for {database_id}: {e}")
        return

    for page in pages:
        run_automations_on_page(client, page, page)

    logger.info(f"  → Governance pass complete ({len(pages)} page(s)).")


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
        run_automations_on_page(client, page, prev_page)
        new_snapshot[page_id] = _strip_files(page)

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

    client = NotionClient(token, debug=args.debug)

    # Record startup time before the governance pass so the first poll catches
    # any changes that occur during the (potentially slow) initial fetch.
    startup_time = _utcnow_iso()

    # Governance pass: fill in missing field values on every page.
    # Pages are not retained — the snapshot starts empty and grows as pages
    # are returned by polls (change detection works from the first poll onward).
    for db_id in database_ids:
        run_governance_pass(client, db_id)

    snapshots: dict[str, dict[str, dict]] = {db_id: {} for db_id in database_ids}

    # Subsequent polls only fetch pages edited after startup.
    last_polled: dict[str, str] = {db_id: startup_time for db_id in database_ids}

    logger.info(f"Starting Notion automation daemon.")
    logger.info(f"  Databases  : {database_ids}")
    logger.info(f"  Interval   : {poll_interval}s")
    logger.info(f"  Automations: {[fn.__name__ for fn in AUTOMATIONS]}")

    while True:
        for db_id in database_ids:
            poll_start = _utcnow_iso()
            snapshots[db_id] = poll_database(client, db_id, snapshots[db_id], last_polled[db_id])
            last_polled[db_id] = poll_start
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
