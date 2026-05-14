"""
bot_notes.py
Accumulator for surfacing issues to the user via a 'Bot Notes' rich text
field on Notion pages.

GOVERNANCE functions call add_bot_note() and mark_page_examined() during
their run. run_governance() in daemon.py calls flush_bot_notes()
after all GOVERNANCE functions complete, then clear_bot_notes().

Structure: {page_id: {issue_code: message}}
- Setting the same code twice is idempotent (overwrites with same message).
- Pages with no active codes get their Bot Notes field cleared in Notion.
- mark_page_examined() registers every page a governance function looked at,
  so Bot Notes is cleared on pages where issues have been resolved.
"""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notion_api import NotionClient

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Issue code constants
# ------------------------------------------------------------------ #

RTD_DUPLICATE_NAME    = "RTD_DUPLICATE_NAME"
RTD_MULTIPLE_OPEN     = "RTD_MULTIPLE_OPEN_TASKS"
RTD_AT_MOST_N_REACHED = "RTD_AT_MOST_N_REACHED"

# ------------------------------------------------------------------ #
#  Accumulators
# ------------------------------------------------------------------ #

_bot_notes: dict[str, dict[str, str]] = {}
_examined_page_ids: set[str] = set()


def add_bot_note(page_id: str, code: str, message: str) -> None:
    """Set or overwrite the message for `code` on `page_id`."""
    _bot_notes.setdefault(page_id, {})[code] = message
    _examined_page_ids.add(page_id)


def mark_page_examined(page_id: str) -> None:
    """
    Register a page as examined during this governance run.
    Pages examined but with no active issues will have Bot Notes cleared.
    """
    _examined_page_ids.add(page_id)


def clear_bot_notes() -> None:
    """Reset all accumulators. Call before each governance run."""
    _bot_notes.clear()
    _examined_page_ids.clear()


# ------------------------------------------------------------------ #
#  Flush — called by daemon after all GOVERNANCE functions complete
# ------------------------------------------------------------------ #

BOT_NOTES_FIELD = "Bot Notes"


def flush_bot_notes(client: "NotionClient") -> None:
    """
    Write Bot Notes to every page that has active issues.
    Clear Bot Notes on every examined page that has no active issues.
    """
    pages_to_clear = _examined_page_ids - set(_bot_notes.keys())

    for page_id, issues in _bot_notes.items():
        bullets = "\n".join(f"• {msg}" for msg in issues.values())
        rich_text = [{"type": "text", "text": {"content": bullets}}]
        try:
            client.update_page_properties(page_id, {BOT_NOTES_FIELD: {"rich_text": rich_text}})
        except Exception as e:
            logger.error(f"Failed to write Bot Notes to {page_id}: {e}")

    for page_id in pages_to_clear:
        try:
            client.update_page_properties(page_id, {BOT_NOTES_FIELD: {"rich_text": []}})
        except Exception as e:
            logger.error(f"Failed to clear Bot Notes on {page_id}: {e}")
