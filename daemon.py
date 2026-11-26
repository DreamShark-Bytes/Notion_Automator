"""
daemon.py
Polling loop that runs your automations on a schedule.

Usage:
    python daemon.py                   # uses config.toml
    python daemon.py --config my.toml  # use a different config file
"""

import os
import sys
import time
import logging
import argparse
from dataclasses import dataclass, field
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
from automations import AUTOMATIONS, GOVERNANCE, register_data_source, clear_data_sources
from bot_notes import clear_bot_notes, flush_bot_notes
import recurring_tasks
from recurring_tasks import BOT_CREATED_PAGES_KEY, page_data_source_id
import hub

VERSION = "1.2.1"
NOTION_API_MIN_VERSION = "1.4.0"

parser = argparse.ArgumentParser(description="Notion automation daemon")
parser.add_argument(
    "--config",
    default="config.toml",
    help="Path to config file (default: config.toml)",
)
parser.add_argument(
    "--secrets",
    default=None,
    help="Path to secrets file (default: config_secrets.toml next to the config file)",
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
    help="Force-write Period Key, Occurrence #, and Period Target on all recurring tasks (including closed and historical), then exit. Use after changing RTD config or to correct historical records.",
)
logger = logging.getLogger(__name__)


def _configure_logging(debug: bool) -> None:
    """Console, rotating file and 48-hour debug file handlers. Called once, by main()."""
    from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

    log_level = logging.DEBUG if debug else logging.INFO
    log_fmt = logging.Formatter("%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level)
    stream_handler.setFormatter(log_fmt)

    rotating_handler = RotatingFileHandler(
        "notion_daemon.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB per file
        backupCount=3,             # keep notion_daemon.log + .1 .2 .3
    )
    rotating_handler.setLevel(log_level)
    rotating_handler.setFormatter(log_fmt)

    # 48-hour debug window: rotates at midnight, keeps 1 backup (current + yesterday).
    # notion_api logger stays at INFO (its DEBUG output is massive JSON response bodies).
    debug_handler = TimedRotatingFileHandler(
        "notion_daemon_debug.log",
        when="midnight",
        backupCount=1,
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(log_fmt)

    logging.basicConfig(
        level=logging.DEBUG,  # Root at DEBUG so debug_handler receives all records.
        handlers=[stream_handler, rotating_handler, debug_handler],
    )
    logging.getLogger("notion_api").setLevel(logging.INFO)


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


def run_governance(client: NotionClient, trigger: str = "daemon") -> list[dict]:
    """
    Run all registered GOVERNANCE functions, then flush and reset Bot Notes.
    Called at daemon startup, on the daily governance cron (fires at day_start_hour) and when the config changes.
    `trigger` is the reason, shown in the Hub row's Report.

    Returns a list of pages created during governance so the caller can run
    an automations init pass on them (to populate fields like Reopen Count
    and Due Date Update Count that are only written during init).
    The pass also records its result on the Hub config row (Errors, Report), (daily pass only) warns about config
    edits that were never applied, and, when no GOVERNANCE function failed, stamps "Last verified time" on the Active rows.
    """
    clear_bot_notes()
    created_pages: list[dict] = []
    failures: list[str] = []
    for fn in GOVERNANCE:
        try:
            result = fn(client)
            if result:
                created_pages.extend(result)
        except Exception as e:
            failures.append(f"{fn.__name__} failed: {e}")
            logger.error(f"GOVERNANCE function '{fn.__name__}' failed: {e}")
    flush_bot_notes(client)
    clear_bot_notes()
    if trigger.startswith("daily"):
        hub.check_unapplied(client)  # safety net; a startup or apply pass has just read the rows
    hub.record_governance(failures, trigger)
    hub.flush(client)
    hub.mark_verified(client, not failures)
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
        data_source_id = page_data_source_id(page)
        if not data_source_id or data_source_id not in snapshots:
            logger.warning(
                f"Cannot run init pass on governance-created page {page['id']}: "
                f"data source {data_source_id!r} not in snapshots — skipping."
            )
            continue
        post_edit, extra_created = run_automations_on_page(client, page, page)
        final = post_edit if post_edit is not None else page
        snapshots[data_source_id][final["id"]] = _strip_files(final)
        logger.info(f"Init pass complete on governance-created page {final['id']}.")
        for new_page in extra_created:  # init mode should never create tasks, but be safe
            new_data_source_id = page_data_source_id(new_page)
            if new_data_source_id and new_data_source_id in snapshots:
                snapshots[new_data_source_id][new_page["id"]] = _strip_files(new_page)


def run_automations_init_pass(client: NotionClient, data_source_id: str) -> dict[str, dict]:
    """
    Fetch every page in a data source, run all AUTOMATIONS in init mode, and
    return the resulting snapshot.

    Automations are called with prev_page=page (same object for both). This
    means change-detection logic sees no diff and stays silent, while
    per-record governance checks (initializing missing fields, backfilling
    dates, etc.) fire correctly.

    The returned snapshot is used as the change-detection baseline for the
    first live poll, so transitions that occur between the init pass and the
    first poll are detected correctly.
    """
    logger.info(f"Running automations init pass for data source {data_source_id} ...")
    try:
        pages = client.query_database(data_source_id)
    except Exception as e:
        logger.error(f"Failed to run automations init pass for {data_source_id}: {e}")
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


def poll_database(client: NotionClient, data_source_id: str, snapshot: dict[str, dict], since: str) -> dict[str, dict]:
    """
    Fetch pages changed since `since` (ISO timestamp), run automations, return updated snapshot.
    snapshot: {page_id: page_dict} from the previous poll
    """
    logger.info(f"Polling data source {data_source_id} for changes since {since} ...")
    filter_payload = {
        "timestamp": "last_edited_time",
        "last_edited_time": {"on_or_after": since},
    }
    try:
        pages = client.query_database(data_source_id, filter_payload=filter_payload)
    except Exception as e:
        logger.error(f"Failed to query data source {data_source_id}: {e}")
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
    client: NotionClient, rtd_definitions_ds_id: str, rtd_snapshot: dict[str, dict], since: str
) -> tuple[dict[str, dict], bool]:
    """
    Poll the RTD definitions data source for changes since `since`. Returns (updated_snapshot, should_govern)
    where should_govern is True only when an RTD's Status has toggled to Active — the only
    change that requires immediate governance. All other field edits (Grace Period, N Cadence,
    Anchor Time, etc.) update the snapshot but do not trigger governance; they take effect on
    the next scheduled governance run. New RTDs created already set to Active also trigger.
    Raises on network/API errors — callers must catch and must NOT advance their time pointer.
    """
    filter_payload = {
        "timestamp": "last_edited_time",
        "last_edited_time": {"on_or_after": since},
    }
    pages = client.query_database(rtd_definitions_ds_id, filter_payload=filter_payload)

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


@dataclass
class RuntimeState:
    """What the daemon is running right now. apply_config() is the only place that changes which data sources
    and recurring-task pairs are monitored."""
    data_source_ids: list[str] = field(default_factory=list)
    snapshots: dict[str, dict[str, dict]] = field(default_factory=dict)        # data source -> {page id: page}
    last_polled: dict[str, str] = field(default_factory=dict)                  # data source -> poll pointer
    rtd_definitions_ids: list[str] = field(default_factory=list)               # one per active recurring pair
    rtd_snapshots: dict[str, dict[str, dict]] = field(default_factory=dict)    # definitions data source -> snapshot
    last_polled_rtd: dict[str, str] = field(default_factory=dict)


def _resolve_data_source_id(client: NotionClient, configured_id: str) -> str:
    """The data source ID for an ID from the config, lowercase without dashes.

    A page reports the data source it lives in, and the daemon matches pages to config by that ID. If the config
    holds a database ID instead (it works for a database with one data source), the client resolves it to its
    data source; a warning asks for the data source ID. On an API version without data sources, or when the lookup
    fails, the configured ID is used as it is."""
    configured = configured_id.replace("-", "").lower()
    if not getattr(client, "uses_data_sources", False):
        return configured
    try:
        resolved = client.resolve_data_source_id(configured).replace("-", "").lower()
    except Exception as e:
        logger.warning(f"Could not look up data source {configured}: {e} — using the configured ID as it is.")
        return configured
    if resolved != configured:
        logger.warning(
            f"{configured} is a database ID, not a data source ID; using its data source {resolved}. "
            "Put the data source ID in the config (Notion app: ... > More > Developer > Copy data source ID)."
        )
    return resolved


def apply_config(
    client: NotionClient,
    state: RuntimeState,
    databases: list[dict],
    rtd_pairs: list[dict],
    week_start_day: int,
    day_start_hour: int,
) -> None:
    """Make what runs equal exactly this config, including nothing.

    `databases` and `rtd_pairs` have the shapes hub.read_config returns. Data sources no longer listed stop being
    polled and forgotten; new ones get an init pass; RTD pairs are always reconfigured, with an empty list if
    need be, so a removed or paused pair stops without a restart. RTD snapshots are rebuilt by
    refresh_rtd_snapshots() after the governance pass that follows.
    """
    databases = [{**db, "data_source_id": _resolve_data_source_id(client, db["data_source_id"])} for db in databases]
    rtd_pairs = [
        {**p,
         "definitions_data_source_id": _resolve_data_source_id(client, p["definitions_data_source_id"]),
         "tasks_data_source_id":       _resolve_data_source_id(client, p["tasks_data_source_id"])}
        for p in rtd_pairs
    ]

    clear_data_sources()
    for db in databases:
        register_data_source(db["data_source_id"], db)
    wanted = list(dict.fromkeys(db["data_source_id"] for db in databases))
    for data_source_id in [ds for ds in state.snapshots if ds not in wanted]:
        del state.snapshots[data_source_id]
        state.last_polled.pop(data_source_id, None)
        logger.info(f"Stopped monitoring data source {data_source_id}.")
    for data_source_id in wanted:
        if data_source_id not in state.snapshots:
            # Recorded before the (possibly slow) init pass so the first poll catches changes made during it.
            started = _utcnow_iso()
            state.snapshots[data_source_id] = run_automations_init_pass(client, data_source_id)
            state.last_polled[data_source_id] = started
    state.data_source_ids = wanted

    recurring_tasks.configure_pairs(rtd_pairs, week_start_day, day_start_hour)
    state.rtd_definitions_ids = list(dict.fromkeys(p["definitions_data_source_id"] for p in rtd_pairs))
    for definitions_id in [ds for ds in state.rtd_snapshots if ds not in state.rtd_definitions_ids]:
        del state.rtd_snapshots[definitions_id]
        state.last_polled_rtd.pop(definitions_id, None)


def refresh_rtd_snapshots(client: NotionClient, state: RuntimeState) -> None:
    """Rebuild the RTD definitions snapshot of every active pair. Run after a governance pass so the Bot Notes
    the bot just wrote become the baseline and do not re-trigger governance on the next poll."""
    for definitions_id in state.rtd_definitions_ids:
        try:
            pages = client.query_database(definitions_id)
        except Exception as e:
            logger.error(f"Failed to build the RTD snapshot for {definitions_id}: {e}")
            continue
        state.rtd_snapshots[definitions_id] = {p["id"]: _strip_files(p) for p in pages}
        state.last_polled_rtd[definitions_id] = _utcnow_iso()
        logger.info(f"RTD snapshot built for {definitions_id} ({len(pages)} definition(s)).")


def poll_rtd_definitions(client: NotionClient, state: RuntimeState) -> bool:
    """Poll every active pair's definitions data source. True if an RTD was activated (governance is due)."""
    activated = False
    for definitions_id in state.rtd_definitions_ids:
        since = state.last_polled_rtd.get(definitions_id)
        if since is None:
            continue
        poll_start = _utcnow_iso()
        try:
            state.rtd_snapshots[definitions_id], pair_activated = _poll_rtd_for_changes(
                client, definitions_id, state.rtd_snapshots.get(definitions_id, {}), since
            )
            state.last_polled_rtd[definitions_id] = poll_start  # only advance on success
        except Exception as e:
            logger.error(f"Failed to poll RTD definitions {definitions_id}: {e}")
            continue
        activated = activated or pair_activated
    return activated


def main():
    args = parser.parse_args()
    _configure_logging(args.debug)

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    secrets_path = args.secrets or os.path.join(os.path.dirname(os.path.abspath(args.config)), hub.SECRETS_FILE_NAME)
    token = hub.resolve_token(cfg, hub.load_secrets(secrets_path))
    if not token:
        old_path = os.path.join(os.path.dirname(secrets_path), "secrets.toml")
        renamed = (f" The file was renamed: rename {old_path} to {secrets_path}."
                   if os.path.exists(old_path) and not os.path.exists(secrets_path) else "")
        raise RuntimeError(f'No Notion token: put token = "..." in {secrets_path} (see config_secrets_example.toml).{renamed}')

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

    config_ds_id = cfg.get("config_hub_data_source_id")
    if cfg.get("automation_hub_page_id"):
        logger.warning(
            "automation_hub_page_id in config.toml is no longer used. Set config_hub_data_source_id; "
            "README: \"Setting up the Configuration Hub\"."
        )
    if cfg.get("status_data_source_id"):
        logger.warning(
            "status_data_source_id in config.toml is no longer used: there is no separate status data source or "
            "kill switch any more. Set a config row's Status to anything but Active to pause it. "
            "Remove the key from config.toml."
        )
    settle_seconds = hub.resolve_settle_seconds(cfg.get("hub_settle_seconds"))

    api_version = getattr(_notion_api, "__version__", None)
    if api_version is None:
        logger.warning("Notion_API version unknown — run: pip install -r requirements.txt")
    else:
        logger.info(f"Notion Automator v{VERSION} | Notion API v{api_version}")
        min_tuple = tuple(int(x) for x in NOTION_API_MIN_VERSION.split("."))
        installed_tuple = tuple(int(x) for x in api_version.split("."))
        if installed_tuple < min_tuple:
            sys.exit(
                f"Notion_API v{api_version} is below the required v{NOTION_API_MIN_VERSION}"
                f" — run: pip install -r requirements.txt"
            )

    notion_version = hub.resolve_notion_version(cfg.get("notion_version"))
    logger.info(f"Notion API version: {notion_version}")
    client = NotionClient(token, debug=args.debug, notion_version=notion_version)

    if config_ds_id:
        hub.init(client, config_ds_id, daemon_version=VERSION, settle_seconds=settle_seconds)
    else:
        logger.info("config_hub_data_source_id not set in config.toml — Hub features disabled.")

    # ---- Database and RTD config loading --------------------------------
    # Hub takes precedence when available. config.toml [[databases]] / [recurring_tasks]
    # are the fallback when Hub is not configured or unavailable at startup.

    if hub.is_available():
        hub.validate_hub_schema(client)
        hub_cfg = hub.read_config(client)
    else:
        hub_cfg = None

    if hub_cfg is not None:
        databases = hub_cfg["databases"]
        rtd_pairs = hub_cfg["rtd_pairs"]
        if not databases:
            logger.warning("Hub: no Active property_automations in the config rows — monitoring zero task databases.")
        if not rtd_pairs:
            logger.warning("Hub: no Active recurring_tasks table in the config rows — recurring task governance disabled.")
        logger.info(
            f"Config source: Configuration Hub "
            f"({len(databases)} database(s), {len(rtd_pairs)} RTD pair(s))"
        )
    else:
        # config.toml fallback
        _toml_dbs = cfg.get("databases", [])
        if not _toml_dbs:
            if config_ds_id:
                raise RuntimeError(
                    "The Configuration Hub is configured (config_hub_data_source_id) but its config could not be read; "
                    "see the 'Hub:' errors and warnings above for the reason. config.toml has no [[databases]] fallback."
                )
            raise RuntimeError(
                "No [[databases]] entries found in config.toml and config_hub_data_source_id is not set."
            )
        databases = []
        for db in _toml_dbs:
            entry = {k: v for k, v in db.items() if k != "id"}
            entry["data_source_id"] = db["id"]
            if db.get("closed_date"):
                entry["closed_date_field"] = "Closed Date"
            if db.get("reopen_count"):
                entry["reopen_count_field"] = "Reopen Count"
            databases.append(entry)

        rtd_pairs = []
        rt_cfg = cfg.get("recurring_tasks", {})
        if rt_cfg.get("enabled"):
            _rt_defs_id = rt_cfg.get("definitions_db_id")
            _rt_tasks_id = rt_cfg.get("tasks_db_id")
            _non_comp = cfg.get("non_completion_statuses") or []
            if _rt_defs_id and _rt_tasks_id:
                rtd_pairs = [{
                    "definitions_data_source_id": _rt_defs_id,
                    "tasks_data_source_id":       _rt_tasks_id,
                    "closed_date_field":          "Closed Date",
                    "non_completion_statuses":    _non_comp,
                    "inherited_fields":           [],
                }]
            else:
                logger.warning("recurring_tasks enabled but definitions_db_id or tasks_db_id is missing — skipping.")
        src = "Hub not available" if config_ds_id else "Hub not configured"
        logger.info(
            f"Config source: config.toml ({len(databases)} database(s)) [{src}]"
        )

    # ---- Hub snapshot -------------------------------------------------
    hub_snapshot: dict[str, dict] = {}
    if hub.is_available():
        try:
            hub_snapshot = hub.build_snapshot(client)
        except Exception as e:
            logger.warning(f"Could not build Hub snapshot: {e}")

    # Run AUTOMATIONS in init mode on every page to fill in missing fields (apply_config does this for each
    # data source). The returned snapshot is the change-detection baseline, so transitions occurring between the
    # init pass and the first live poll are detected correctly (e.g. a task reopened right after daemon start).
    state = RuntimeState()
    apply_config(client, state, databases, rtd_pairs, week_start_day, day_start_hour)

    # GOVERNANCE functions (startup pass — also runs daily at day_start_hour via cron below).
    # --reconcile sets flags before this single governance run so no second pass is needed.
    if args.reconcile:
        logger.info("--reconcile flag set: retroactively writing Period Key, Occurrence #, and Period Target on all recurring tasks (task creation skipped).")
        recurring_tasks.set_reconcile_flags(period_key=True, period_target=True, occurrence_number=True)

    logger.info("Running governance: daemon startup.")
    gov_created = run_governance(client, "daemon startup")
    recurring_tasks.set_reconcile_flags()  # reset (no-op if flags were never set)

    if args.reconcile:
        return

    # Run init pass on any pages created by governance so their fields (Reopen Count,
    # Due Date Update Count, etc.) are populated immediately, not deferred to the next poll.
    if gov_created:
        _init_pass_on_pages(client, gov_created, state.snapshots)

    if args.governance_only:
        logger.info("--governance-only flag set: exiting after governance pass.")
        return

    # Build RTD snapshots after startup governance so bot-written Bot Notes are
    # captured as the baseline — preventing them from re-triggering governance next poll.
    refresh_rtd_snapshots(client, state)

    # Track date of last GOVERNANCE run so the daily cron fires at most once per day.
    # Initialized to today because the startup governance pass above already ran.
    # The next cron fires tomorrow at day_start_hour.
    last_governance_date = datetime.now().date()

    logger.info(f"Starting Notion automation daemon.")
    logger.info(f"  Data sources: {state.data_source_ids}")
    logger.info(f"  Interval    : {poll_interval}s")
    logger.info(f"  Automations : {[fn.__name__ for fn in AUTOMATIONS]}")
    logger.info(f"  Governance  : {[fn.__name__ for fn in GOVERNANCE]}")

    while True:
        # Hub: has the config changed? Runs every cycle before anything else.
        if hub.is_available():
            try:
                new_hub_snapshot, needs_apply = hub.poll_for_changes(client, hub_snapshot)
                if not needs_apply:
                    hub_snapshot = new_hub_snapshot
                else:
                    new_cfg = hub.read_config(client)
                    if new_cfg is None:
                        logger.warning("Hub: the config could not be read — keeping the running config; retrying next poll.")
                    else:
                        hub_snapshot = new_hub_snapshot
                        apply_config(client, state, new_cfg["databases"], new_cfg["rtd_pairs"], week_start_day, day_start_hour)
                        logger.info(
                            f"Hub config reload: reconfigured with {len(state.data_source_ids)} database(s), "
                            f"{len(new_cfg['rtd_pairs'])} RTD pair(s)."
                        )
                        logger.info("Running governance: Hub config applied.")
                        gov_created = run_governance(client, "config applied")
                        if gov_created:
                            _init_pass_on_pages(client, gov_created, state.snapshots)
                        refresh_rtd_snapshots(client, state)
            except Exception as e:
                logger.warning(f"Hub poll failed: {e}")

        # Daily GOVERNANCE cron — fires once per calendar day at day_start_hour.
        now_local = datetime.now()
        governance_time_today = datetime.combine(now_local.date(), dtime(day_start_hour, 0))
        if now_local >= governance_time_today and last_governance_date != now_local.date():
            last_governance_date = now_local.date()
            logger.info(f"Running governance: daily {day_start_hour:02d}:00 cron.")
            gov_created = run_governance(client, f"daily {day_start_hour:02d}:00 cron")
            if gov_created:
                _init_pass_on_pages(client, gov_created, state.snapshots)
            # Refresh RTD snapshots post-governance so bot-written Bot Notes become
            # the new baseline and don't re-trigger governance next poll.
            refresh_rtd_snapshots(client, state)

        # RTD change detection — triggers governance only when an RTD is activated.
        if poll_rtd_definitions(client, state):
            logger.info("Running governance: RTD definition activated.")
            gov_created = run_governance(client, "RTD activated")
            if gov_created:
                _init_pass_on_pages(client, gov_created, state.snapshots)
            # Refresh RTD snapshots so bot-written Bot Notes don't re-trigger governance.
            refresh_rtd_snapshots(client, state)

        for data_source_id in state.data_source_ids:
            poll_start = _utcnow_iso()
            state.snapshots[data_source_id] = poll_database(
                client, data_source_id, state.snapshots[data_source_id], state.last_polled[data_source_id]
            )
            state.last_polled[data_source_id] = poll_start
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
