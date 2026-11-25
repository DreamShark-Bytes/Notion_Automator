# Create your own automations

> **Draft.** Sections marked *Planned* describe behavior that is designed but not built yet.

An automation is a small Python function. The daemon calls it when something changes in Notion, and it returns the property updates to write. This page covers where code goes, how to switch an automation on and off, and how to report problems so the user can see them in Notion.

## Where your code goes

| Kind | Signature | Register it in |
|---|---|---|
| Per-page automation (runs when a page changed) | `fn(client, page, prev_page) -> dict` | `AUTOMATIONS` in `automations.py` |
| Cross-page governance (runs at startup, after a config change, and once a day) | `fn(client)` | `GOVERNANCE` in `automations.py` |
| Recurring-task logic and shared helpers | any | `recurring_tasks.py` (`automations.py` imports from it, never the reverse) |

- `page` is the full page from the API and `prev_page` is the last snapshot of it (`None` on the first run).
- Return a dict of property updates, or `{}` to do nothing.
- `auto_recurring_tasks` must stay last in `AUTOMATIONS`: it creates pages, so it runs after the automations that stamp fields.
- Never call the Notion REST API directly. Use the `Notion_API` client that is passed in (`client.update_page_properties()`, `client.create_page()`).

## Be safe to run twice

The bot's own writes show up as changes on the next poll, so every automation must be idempotent: compare the current value with the value you would write, and return `{}` when they already match. Otherwise the automation re-triggers itself.

## Switching an automation on and off

There are no `enabled = true/false` keys. Use these instead:

- **Whole config row:** set the row's Status. Only **Active** rows act; every other status (Not started, On hold, Discontinued) does nothing, and the daemon stops that row's automations on its next poll without a restart.
- **One block in a row:** put a `#` in front of every line of the block. A removed or commented-out block stops on the next config reload.
- **Your own setting:** a missing key means the feature is off. Read it with a default and return `{}` early when it is absent, as `auto_closed_date` does when `closed_date_property` is not set.
- **Experiments:** while you are still shaping an automation, read its settings from `config.toml`. Move them to the config row, with validation, once the shape is stable.

Do not store the on/off state anywhere else (a hidden property, a file). The config row is the one place the user looks.

## Messages: who sees what

Pick the channel by who needs to act.

| Channel | Use it for | Where it shows |
|---|---|---|
| Log (`logger`) | What the code did, for debugging | The daemon log only |
| Bot Note (`add_bot_note(page_id, code, message)` in `bot_notes.py`) | Something about **one task** that the user should decide (a task was deleted, a period was skipped) | The task's Bot Notes property, flushed after each governance pass |
| Hub warning or error (*Planned*) | Something about **configuration** or a whole automation | The Warnings or Errors column of the config row that owns the automation |

Rules for all of them:

- **Consequential decisions go to the user.** Report them instead of acting silently. The bot never takes an action the user would not expect, and it prefers the least destructive step: inform, then archive, then cancel, then delete. Use `archive_page()` for clean-up, never a status change that triggers another automation.
- **Say what, where and what to do.** "Property 'Closed Dat' not found in tasks. Check the spelling of closed_date_property." is useful; "Error in config" is not.
- **Errors and warnings mean different things.** An *Error* means the bot cannot do what the config asks, so nothing from that block is running. A *Warning* means the automation still works but something needs a look (an unknown key, a renamed property, a newer edit that is not applied yet).
- **Plain text, not data.** Messages are written for people. Nothing in the daemon reads them back, so do not encode state in them.
- **Never write into the user's config.** The code block in a config row belongs to the user. The bot reports problems in the Warnings and Errors columns and the Report column only.

### Hub warnings and errors (*Planned*)

Messages are collected in memory and written to the config row in one update per pass.

```python
import hub

# Replaces this source's messages. An empty call clears them.
hub.report("db:tasks", errors=[], warnings=["Property 'Closed Dat' not found."])
```

- `source` names your automation (`db:<name>`, `rtd:<name>`, `governance`). A source replaces only its own messages, so two automations never erase each other's.
- Report on every run, including with empty lists when everything is fine, so an old message clears when the cause is fixed.
- Each message is prefixed with its source in the column (`[db:tasks] ...`). Notion limits one text value to 2000 characters, so keep messages short.

## Bot-managed fields

Fields such as Period Key, Occurrence #, Closed Date and Bot Notes belong to the bot. Users' edits to them are overwritten. When you add a bot-managed field, say so in the setup docs.

## Dates and periods

Work out which period a task belongs to from its dates: the Due Date for an open task and the Closed Date for a closed one. Never read the stored Period Key back for logic; it is a display label.

## Test it

Every automation should have tests that run without Notion. Build a fake page dict (see `tests/helpers.py` and `tests/test_field_tracking.py`), call the function, and assert on the returned updates, including the case where nothing should change.
