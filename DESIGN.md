# Notion Automator — Design Document

> **Status:** Living document. Edit freely — implementation will follow this doc.
> Sections marked `[YOUR INPUT NEEDED]` are placeholders for you to fill in.

---

## 1. Purpose

Notion Automator is a background daemon that watches one or more Notion databases and applies rule-based automations whenever pages change. It runs on a schedule, compares each page against a stored snapshot of its previous state, and writes property updates back to Notion.

It is intentionally write-back only: the daemon reads from Notion and writes to Notion. It does not maintain a permanent local database (that is the job of `Notion_Analytics`).

---

## 1.1 Design Principles

These principles take precedence when individual decisions conflict. They are not re-debated per-feature — reference them in the Decision Log when a decision follows or deliberately departs from them.

**Minimum surprise.** The bot should never take an action the user would not expect given what they just did. Changing an RTD field should not silently delete a task. Closing a task should not silently alter the RTD.

**Least-destructive intervention.** Prefer informing over acting. When the bot must intervene, prefer the reversible action over the irreversible one: a bot note over an archive, an archive over a delete, a cancel over a hard delete. If a consequential action must be taken (e.g. auto-cancel on grace period expiry), it is always part of an explicitly designed and documented behavior the user opted into by configuring the RTD.

**Put consequential decisions in the user's hands.** The bot flags, warns, and suggests via Bot Notes and logs. It does not make permanent decisions on behalf of the user unless the feature explicitly requires it and the user has configured it. Example: when a task is deleted, the bot creates a replacement with a visible note rather than silently deactivating the series — the user decides whether to continue or stop.

**Bot-managed fields are owned exclusively by the bot.** Users should not need to manually edit fields the bot writes (Period Key, Occurrence #, Closed Date, Bot Notes). If a field drifts, governance corrects it. If a user edits a bot field, the bot will overwrite it at the next poll — this is documented as a known limitation, not a supported workflow.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────┐
│                   daemon.py                     │
│                                                 │
│  startup                                        │
│  ├── run_automations_init_pass()  ← all pages         │
│  │     └── run_automations_on_page(page, page)  │  ← governance only, pages discarded
│  └── set last_polled = startup_time             │
│                                                 │
│  loop (every POLL_INTERVAL seconds)             │
│  └── poll_database()  ← pages edited since last │
│        └── run_automations_on_page(page, prev)  │  ← change detection + governance
│              └── NotionClient.update_page_...   │  ← writes back to Notion
└─────────────────────────────────────────────────┘
```

---

## 3. Components

### 3.1 `daemon.py` — Orchestrator

| Function                      | Purpose                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| -------------------------------| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `run_automations_init_pass()` | Fetches ALL pages from a database on startup. Runs every function in `AUTOMATIONS` with `prev_page=page` (per-page governance only — no change detection fires). Pages are discarded after processing — not stored in the snapshot.                                                                                                                                                                                                                                                                                                                                                                                                   |
| `run_governance()`            | Iterates `GOVERNANCE` and calls each function with `client`. Runs once at startup after all per-page governance passes are complete. This is the cross-page extension point — each function sees the full database state and acts on patterns that span multiple pages. After all GOVERNANCE functions complete, flushes the `Bot Notes` accumulator — writes `Bot Notes` to every affected page and clears the field on pages with no current issues.                                                                                                                                                                                |
| `poll_database()`             | Fetches only pages edited since the last poll (using Notion's `last_edited_time` filter). Runs full automations (governance + change detection). Returns updated snapshot.                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `run_automations_on_page()`   | Calls every function in `AUTOMATIONS`, collects returned update dicts, and patches Notion if anything changed. Returns `(post_edit_page, created_pages)`: `post_edit_page` is the page as returned by the Notion API after the bot write (becomes the next poll's `prev_page`, so change-detection sees the bot's own values rather than stale pre-write state); `created_pages` is a list of pages created by automations (e.g. a new recurring task), inserted into the snapshot so the next poll has a valid `prev_page` for them. Automations signal new pages via the `BOT_CREATED_PAGES_KEY` sentinel key in their return dict. |
| `_strip_files()`              | Removes `files`-type properties from a page dict before storing in the in-memory snapshot. Keeps memory footprint clean; attachments are not needed for automation logic.                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `_utcnow_iso()`               | Returns current UTC time floored to the minute, matching Notion's `last_edited_time` resolution.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |

**Startup sequence:**
```
for each database_id:
    run_automations_init_pass(client, database_id)   ← per-page, all AUTOMATIONS
run_governance(client)               ← cross-page, all GOVERNANCE
begin poll loop
```

**Scheduled governance (governance cron):**

The governance cron is not a separate process — it is a time-triggered run of the full governance suite inside the poll loop. When `_is_cron_time()` returns True, the daemon runs the same two-phase sequence before resuming polling:

```
if _is_cron_time(last_cron_run, hour=2):
    for db_id in database_ids:
        run_automations_init_pass(client, db_id)   ← same as startup
    run_governance(client)                         ← same as startup
    last_cron_run = now
```

`run_automations_init_pass` internally iterates `AUTOMATIONS`; `run_governance` internally iterates `GOVERNANCE`. The cron calls the functions — the registries are an internal detail. This means any function that needs to run at the period boundary simply needs to be in the correct registry and it is picked up by both startup and the governance cron automatically.

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `NOTION_TOKEN` | *(required)* | Notion integration secret |
| `DATABASE_IDS` | *(required)* | Comma-separated Notion database IDs to watch |
| `POLL_INTERVAL` | `60` | Seconds between polls |

**CLI flags:**

| Flag | Description |
|---|---|
| `--debug` | Enables verbose API request/response logging. For manual runs only. |
| `--governance` | Runs the automations init pass and all GOVERNANCE functions once, then exits. Useful for testing or forcing an out-of-schedule governance run without starting the poll loop. |

---

### 3.2 `notion_api.py` — API Wrapper (shared library)

Thin wrapper around the Notion REST API (version `2022-06-28`), provided by the `Notion_API` package.

| Method | Description |
|---|---|
| `get_database(id)` | Returns database schema (property definitions, status groups, etc.) |
| `query_database(id, filter)` | Returns all pages, handling pagination automatically |
| `get_page(id)` | Returns a single page. Returns `None` on 404 (page permanently deleted) rather than raising. Also returns archived pages — caller must check `page.get("archived")`. |
| `update_page_properties(id, props)` | PATCHes one or more properties on a page |
| `create_page(database_id, props)` | Creates a new page in a database |
| `archive_page(id)` | Moves a page to Notion trash (`PATCH /v1/pages/{id}` with `{"archived": true}`). Page stops appearing in `query_database` results but remains fetchable by ID. |

Static property builders (`date_property`, `number_property`, `rich_text_property`, `checkbox_property`) are convenience constructors for building Notion property update payloads.

---

### 3.3 `recurring_tasks.py` — Recurring Task Logic and Shared Helpers

Contains all recurring task automation logic as well as helper utilities shared with `automations.py`. `automations.py` imports from this module — not the reverse.

| Export                                          | Description                                                                                                                                           |
| -------------------------------------------------| -------------------------------------------------------------------------------------------------------------------------------------------------------|
| `init(definitions_db_id, tasks_db_id)`          | Called once at daemon startup to enable recurring task features                                                                                       |
| `run_recurring_governance(client)`              | Cross-page governance function registered in `GOVERNANCE`. Ensures each active definition has exactly one open task. Signature: `fn(client) -> None`. |
| `auto_recurring_tasks(client, page, prev_page)` | Per-page automation function registered in `AUTOMATIONS`                                                                                              |
| `FIELDS_NOT_INHERITED`                          | Set of field names excluded from new-task field copying                                                                                               |
| Shared helpers                                  | `_get_prop`, `_get_select`, `_get_status_group`, `_get_date`, `_get_number`, `_get_text`, `_get_title`, `_get_relation_ids`, `_now_iso`               |

---

### 3.4 `bot_notes.py` — Bot Notes Accumulator

Module-level accumulator for surfacing issues to the user via a `Bot Notes` field on Notion pages. GOVERNANCE functions call `add_bot_note()` during their run; `run_governance()` flushes all accumulated notes to Notion after every function has completed.

**Structure:**
```python
_bot_notes: dict[str, dict[str, str]] = {}  # {page_id: {issue_code: message}}
```

| Function | Description |
|---|---|
| `add_bot_note(page_id, code, message)` | Sets or overwrites the message for `code` on `page_id`. Idempotent — calling twice with the same code just refreshes the message. |
| `clear_bot_notes()` | Resets the accumulator. Called by the daemon before each governance run. |
| `get_bot_notes()` | Returns the current accumulator snapshot for the daemon to flush. |

**Issue code constants** (defined in `bot_notes.py`):

| Code                      | Raised by                  | Description                                 |
| ---------------------------| ----------------------------| ---------------------------------------------|
| `RTD_DUPLICATE_NAME`      | `run_recurring_governance` | Two or more active RTDs share the same name |
| `RTD_MULTIPLE_OPEN_TASKS` | `run_recurring_governance` | More than one open task exists for this RTD |

**Flush semantics:**
- Pages with one or more active codes → `Bot Notes` written as a bulleted list of current messages
- Pages with no active codes → `Bot Notes` cleared (field set to empty)
- Full rewrite every governance run — `Bot Notes` always reflects the current state, never accumulates history. Self-healing: resolved issues disappear at the next run.

**Write scope:** Only GOVERNANCE (uppercase) functions write to `Bot Notes` via the accumulator. Per-record governance (lowercase, inline in AUTOMATIONS) never writes to `Bot Notes` — it fixes issues silently. This may be revisited if a per-record check surfaces a user-visible issue that cannot be auto-corrected.

---

### 3.5 `automations.py` — Automation and Governance Registries

This file is the **user-facing extension point** for the daemon. It exposes two registries:

---

#### `AUTOMATIONS` — per-page functions

Runs on every page the daemon processes (governance pass + poll loop).

```python
def my_automation(client: NotionClient, page: dict, prev_page: dict | None) -> dict:
    ...
    return {"Property Name": <notion_property_value>}  # or {} to do nothing
```

| Parameter | Description |
|---|---|
| `client` | `NotionClient` instance. Use `_client` by convention if your function does not call the API. |
| `page` | Current page dict as returned by the Notion API |
| `prev_page` | Previous snapshot of the same page. During governance pass, equals `page` — so no field appears "changed" and change-detection logic stays silent while governance checks still fire. |

Return a dict of Notion property updates. Return `{}` to make no changes. All automation return values are merged and applied in a single API call.

**To enable**, add to `AUTOMATIONS`. **To disable**, remove it.

---

#### `GOVERNANCE` — cross-page functions

Runs **once at startup** after all per-page governance passes are complete. Use for checks that require seeing all pages before acting (e.g. "does every active definition have an open task?").

```python
def my_governance(client: NotionClient) -> None:
    # fetch whatever databases you need, act on cross-page patterns
    ...
```

| Parameter | Description |
|---|---|
| `client` | `NotionClient` instance — governance functions fetch their own data |

No return value. Governance functions write directly to Notion via `client` as needed.

**To enable**, add to `GOVERNANCE`. **To disable**, remove it.

---

**Why two registries?**
- `AUTOMATIONS` — page scope: acts on one page at a time, called with every changed page
- `GOVERNANCE` — global scope: sees the full database state, called once at startup

Per-page governance (initializing missing fields, stamping dates) belongs in `AUTOMATIONS` — the function receives the page and can check and fix it inline. Cross-page governance (ensuring referential integrity across many pages) belongs in `GOVERNANCE`.

---

## 4. In-Memory Snapshot

The snapshot is a `dict[database_id, dict[page_id, page_dict]]` held in RAM. It starts **empty** on every daemon start and grows organically as pages are returned by polls.

**Why not pre-load at startup?**
- The startup governance pass only needs to read and write each page once, then discard it. There is no reason to keep all pages in RAM after that.
- Change detection only needs the previous state of pages that have actually changed since the last poll. In steady state, most pages in a large database are untouched.

**Why in-memory only?**
- Avoids stale state between restarts.
- The daemon's job is to react to *changes*; it doesn't need historical data (that is `Notion_Analytics`'s job).

**Implications:**
- A daemon restart resets the snapshot. The startup governance pass re-applies missing field initialization.
- The first change to a page after a restart will have `prev_page=None` if the page was not returned by any prior poll. Change-detection automations will skip it; governance automations will still fire.
- Durable state that must survive restarts (e.g. "has this page ever had a due date?") is stored as a Notion property, not in the snapshot.

---

## 5. Governance System

Governance checks run on **every** page the daemon processes, including the startup pass. They are embedded in each automation function and fire independently of change detection.

The key pattern: during the startup governance pass, `prev_page=page` is passed to every automation. Change-detection logic compares `prev_page` to `page` — since they're the same object, nothing appears changed. Governance checks look only at the current page's field values (are they `None`? are they missing?) and are not affected by `prev_page`.

### Current governance rules

| Field | Rule |
|---|---|
| `Due Date Update Count` | If `None`, initialize to `0` |
| `First Due Date` | If empty and `Due Date` is set, stamp with current `Due Date` value. Does not increment the counter. |
| `Closed Date` | If status is in the `Complete` group and `Closed Date` is empty, backfill with `last_edited_time` |
| `Reopen Count` | If `None`, initialize to `0` |

---

## 6. Current Automations

### 6.1 `auto_closed_date`

**Triggers:**
- Status transitions **into** the `Complete` group (close)
- Status transitions **out of** the `Complete` group (reopen)

**On close (non-Complete → Complete):**
- Stamp `Closed Date` with `now()` only if `Closed Date` is currently empty. If the user has already set it, leave it unchanged. This applies to all task types — there is no distinction between recurring and non-recurring tasks. A pre-filled `Closed Date` is treated as intentional (e.g. retroactive dating, or data entered while the field was misnamed). For recurring tasks, `Closed Date` additionally feeds period counting and `_create_next_task` period key derivation.

**On reopen (Complete → non-Complete):**
- Clear `Closed Date` (set to null) — ensures the next close gets a fresh, accurate stamp.
- Increment `Reopen Count` by 1.

**Governance:**
- If status is `Complete` and `Closed Date` is empty: backfill with `last_edited_time`. Applies to both recurring and non-recurring tasks.
- If `Reopen Count` is null: initialize to `0`.

**Notion fields required:**

| Field | Type |
|---|---|
| `Status` | Status |
| `Closed Date` | Date |
| `Reopen Count` | Number |
| `Recurring Series` | Relation (read-only check — bot never writes this in `auto_closed_date`) |

---

### 6.2 `auto_due_date_update_count`

**Trigger:** `Due Date` changes from one date to a different date.

**Does NOT fire when:**
- Due Date is set for the first time (stamps `First Due Date` instead)
- Due Date is cleared (set to empty)
- Due Date is unchanged

**Governance:**
- If `Due Date Update Count` is `None`, initialize to `0`
- If `First Due Date` is empty but `Due Date` is set, stamp `First Due Date` with the current `Due Date` value

**Notion fields required:**

| Field | Type |
|---|---|
| `Due Date` | Date |
| `Due Date Update Count` | Number |
| `First Due Date` | Date |

---

### 6.3 `auto_last_edited_note` *(disabled)*

**Trigger:** Any property on the page changes.

**Action:** Writes a human-readable UTC timestamp to a `Last Edited (Bot)` rich text field.

Disabled by default. Enable by adding it to the `AUTOMATIONS` list.

---

## 7. Recurring Tasks

Recurring tasks enable habit and responsibility tracking by creating a new task whenever the current one is completed or cancelled. This preserves a full history of completions and supports trend reporting — unlike simply re-opening a task.

#### Design goals

- One open task per series at all times
- New task creation triggered by the current task entering the **Complete** status group (Done or Cancelled), including bot-initiated cancellations
- Due dates flexible enough to handle delayed action, missed deadlines, and shifting schedules
- Field values inherited from the closed task (not the definition) to preserve user customisations
- Governance on every startup to correct any out-of-sync state (e.g. tasks deleted by the user)

#### Recurring Task Definitions database

The definitions database is created manually by the user (see README §6). Automated creation via the Project Page is planned (see STATUS.md Planned Features).

| Field                  | Type                   | Notes                                                                                                                                                                                                                                                                                                           |
| ------------------------| ------------------------| -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Name                   | Title                  |                                                                                                                                                                                                                                                                                                                 |
| Type                   | Select                 | `Habit`, `Bad Habit`, `Responsibility`. See Task type behaviour below.                                                                                                                                                                                                                                          |
| Active                 | Checkbox               | Uncheck to pause without deleting                                                                                                                                                                                                                                                                               |
| Cadence Type           | Select                 | `Once per period`, `Exactly N per period`, `At most N per period`, `Minimum N per period`, `Unlimited`. **Bad Habit ignores this field — always treated as Unlimited.**                                                                                                                                        |
| Cadence N              | Number                 | Used by `Exactly N per period`, `At most N per period`, and `Minimum N per period`; blank for others. **Bad Habit: ignored.**                                                                                                                                                                                   |
| Period                 | Select                 | `Day`, `Week`, `Month`, `Year`. **Bad Habit: used for Instance # reset cadence only — no effect on due dates or cadence limits.**                                                                                                                                                                               |
| Anchor Day             | Number                 | Mon=1 … Sun=7 for weekly; 1–31 for monthly (overflows to last day of month). **Bad Habit: ignored.**                                                                                                                                                                                                            |
| Anchor Time            | Text                   | e.g. `13:00`; blank = no specific time. **Bad Habit: ignored.**                                                                                                                                                                                                                                                 |
| Grace Period (days)    | Number                 | Responsibilities only — auto-cancelled this many days past due; blank = never. Overridden by `Do Not Autoclose`. **Bad Habit: ignored.**                                                                                                                                                                        |
| Do Not Autoclose       | Checkbox               | Default: False. When True, suppresses grace-period auto-cancellation for this RTD regardless of Type or Grace Period value. Intended for Responsibilities the user never wants auto-cancelled.                                                                                                                  |
| Tasks Done This Period | Number                 | **Bot-managed display field.** Incremented by the bot each time a task closes in the current period. Reset to 0 by the governance cron at period boundary. User should not edit. **Bad Habit and Unlimited: not tracked.**                                                                                             |
| Current Period         | Date (start + end)     | **Bot-managed display field.** Updated by the governance cron to show the current period's date range (e.g. Apr 1 → Apr 30). **Bad Habit and Unlimited: not tracked.**                                                                                                                                                 |
| Notes                  | Rich Text              |                                                                                                                                                                                                                                                                                                                 |
| Last Completed         | Rollup                 | Max of `Closed Date` from related tasks (Notion-computed)                                                                                                                                                                                                                                                       |
| Number of Open Tasks   | Rollup                 | Count of related tasks where `Is Open` = true (Notion-computed via checkbox formula workaround — see §7.1 Is Open field)                                                                                                                                                                                        |
| Bot Notes              | Rich Text              | **Bot-managed.** Written by GOVERNANCE functions via the Bot Notes accumulator. Contains a bulleted list of current issues (e.g. duplicate name warning). Cleared automatically when all issues are resolved. User should not edit — content is overwritten each governance run.                                |
| Current Open Task      | Relation → MT Database | **Bot-managed.** Always points to the most recent bot-created open task for this series. Set when bot creates a task; cleared when that task enters the Complete group. Never updated for user-created or user-reopened tasks. Used by governance to detect deletion — see §7.1 Deletion and Archive Detection. |

#### Main task database additions

Fields specific to recurring task functionality are named with the suffix `(Recurring Task)` so users can distinguish them from general task fields.

| Field                                | Type               | Notes                                                                                                                                                                                                                                                                                                                 |
| --------------------------------------| --------------------| -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Recurring Series                     | Relation           | Points to the RTD database                                                                                                                                                                                                                                                                                            |
| Occurrence # this Period (Recurring Task) | Number        | Set by bot at creation; see counting rules below                                                                                                                                                                                                                                                                      |
| Period Key (Recurring Task)          | Text               | **Display-only label** set by bot (e.g. `2026-04`, `2026-W15`). Never read by bot logic for period comparisons. Period membership is always derived from Due Date (open tasks) or Closed Date (closed tasks) — see counting rules below.                                                                              |
| Period Target (Recurring Task)       | Text               | Human-readable goal set by bot (e.g. `Minimum 3 per Week`)                                                                                                                                                                                                                                                            |
| Ignore Grace Period (Recurring Task) | Checkbox           | Default: False. Bot sets to True when a user re-opens a Responsibility task from the Complete group. Once True, **never reset by the bot** — that task instance is permanently ignored by grace period auto-close. The user owns closure entirely. New tasks created from this task's closure start fresh with False. |
| Is Open                              | Formula → Checkbox | `prop("Status") == "Not started" or prop("Status") == "In progress" or prop("Status") == "On hold"`. Used as rollup target for "Number of Open Tasks" on the RTD. User-created Notion formula — bot never writes it.                                                                                                  |
| Manual Created Date                  | Date               | User-managed only — bot never writes this. For retroactively created tasks; provides accurate dates for reporting. No effect on bot logic.                                                                                                                                                                            |

> **Referencing previous tasks:** No bot logic or dedicated relation field is needed. In any RT page, create a filtered view of the Recurring Series relation sorted by: Created Date (tertiary) → Due Date (secondary) → Closed Date (primary, descending). The most recently closed task surfaces at the top.

#### Due date range logic

| Anchor Day | Anchor Time | Due Date on new task                       |
| ------------| -------------| --------------------------------------------|
| empty      | empty       | Full period span (e.g. April 1 → April 30) |
| set        | empty       | That day only, no time (e.g. April 15)     |
| set        | set         | That day + time (e.g. Monday 1:00 PM)      |

`Unlimited` cadence: no due date ever.
`At most N per period` cadence: no due date ever — tracks occurrences, not scheduled events.
`Bad Habit` type: no due date ever, regardless of any anchor settings.

#### Instance # counting rules

Instance # is assigned at task creation by counting existing tasks, not by reading the prior task's Instance # value. This makes the sequence resilient to user edits — if a user changes Instance # values directly, the count is unaffected.

**Assignment rule at `_create_next_task` time:**

| Cadence type         | Instance # assigned to new task                           |
| ----------------------| -----------------------------------------------------------|
| Once per period      | Count of all tasks for this RTD in the current period + 1 |
| At most N per period | Count of all tasks for this RTD in the current period + 1 |
| Minimum N per period | Count of all tasks for this RTD in the current period + 1 |
| Unlimited            | Count of all tasks for this RTD in the current period + 1 |

"Tasks for this RTD in the current period" is determined per-task by `_task_in_period`:

- **Open tasks** (no `Closed Date`): period derived from **Due Date**. If Due Date is absent, falls back to `now()` — an undated open task always counts as belonging to the current period.
- **Closed tasks**: period derived from **Closed Date** (ground truth). `auto_closed_date` governance backfills `last_edited_time` as Closed Date for any Complete task missing it, so this is always available. Handles retroactive Closed Date edits correctly.
- **Non-completion statuses** (Cancelled, Skipped, Missed, etc.): always excluded from counts — they represent missed/skipped attempts, not completions.
- The `Period Key (Recurring Task)` field is **never read** for period comparisons — it is a display label only.

`query_database` is used, so archived and permanently deleted tasks are excluded from the count — if tasks were deleted, the sequence restarts from the remaining count (acceptable; deleted tasks remove their slot).

**At the governance cron (period boundary carry-over):** Instance # on the carried-over open task is updated to: count of all tasks for this RTD in the new period + 1 (which will be 1 if no tasks exist yet for the new period).

**At most N per period alert**: when the count for the current period reaches N, the bot creates the next task normally but flags the RTD (mechanism TBD — depends on Status Page / notification design).

**Re-opened tasks:** Instance # is left unchanged. A re-open is the same task instance returning — its original sequence position stands. No renumbering occurs.

#### Field inheritance

When creating a new task, all properties from the closed task are copied **except**:

- Fields the bot sets explicitly: `Due Date`, `Status`, `Instance #`, `Period Key`, `Period Target`, `Recurring Series`
- Fields managed by governance automations: `Closed Date`, `Reopen Count`, `First Due Date`, `Due Date Update Count`
- Read-only Notion property types: `formula`, `rollup`, `created_time`, `last_edited_time`, `created_by`, `last_edited_by`, `unique_id`, `verification`, `button`

Governance automations initialize the excluded fields correctly on the new task.

#### Task type behaviour

**Habits** — can be missed. Without a grace period, an overdue task stays open indefinitely. No auto-cancellation ever.

**Bad Habits** — tracks incidents of behavior the user wants to reduce. No due dates, no grace period, no anchor settings, no cadence type or limit. `Period` is used for Instance # reset cadence only — it gives the user at-a-glance context (e.g. "picked my nose 4 times this week"). Instance # resets to 1 at each period boundary the same as other types. A closed task represents one logged incident; the bot immediately creates a new open task ready for the next. Note: users may want a custom status option (e.g. "Logged") in the Complete group rather than "Done" — this is a Notion configuration choice, not a bot concern.

**Responsibilities** — commitments that should be called out when missed. If `Do Not Autoclose` is False and `Grace Period (days)` is set, the bot cancels the task when `now() > due date end (or start) + Grace Period (days)` and `Ignore Grace Period (Recurring Task)` on the task is False. Cancellation triggers normal next-task creation. If `Do Not Autoclose` is True, the task stays open indefinitely like a Habit — the user handles closure manually.

Grace period evaluation is **cron-only** — it runs in a GOVERNANCE function at startup and the daily governance cron, never during the regular poll loop. This gives the user the full day to adjust details (including Due Date) before the bot intervenes. A Due Date change naturally shifts the window: `now() > new_due_date + grace_period` is re-evaluated at the next cron run. No special event handling for Due Date changes.

**Grace period cap when grace period exceeds the period length:**

If a task's Due Date falls in a past period and the stated Grace Period would keep it open indefinitely, a hard cap applies: cancel the task if `now >= current_period_start + 1 day`. This means:
- At the first governance cron of a new period: new task is created; old carry-over task is not yet cancelled (given 1 day of grace)
- At the second governance cron (Day 2 of new period): cap fires; old task cancelled regardless of stated Grace Period value

Result: at most 2 open tasks simultaneously for at most 1 day. `Period=Day, Grace=9999` cleans up in 1 day. Normal grace period still fires first — if grace expires within the same period, the cap never activates.

**Minimum N per period — stale task handling (cron-only):**

When a "Minimum N per period" task's Due Date falls in a past period, governance checks how many completions occurred in that past period:

- **Minimum not met:** Cancel the task (records the failure). Create a fresh task for the current period using existing task-creation logic.
- **Minimum met:** Archive the task (`archive_page()`) — not cancel. Cancellation implies failure; archiving removes it silently. Create a fresh task for the current period. The archived task is used as the field inheritance source for the replacement.

Archive rather than cancel is used because cancellation triggers `auto_recurring_tasks` and implies the user failed. Archiving removes the page from `query_database` results immediately, allowing the standard duplicate guard and task-creation flow to run cleanly. Creating fresh (rather than patching the stale task's Due Date) avoids incrementing Due Date Update Count.

**Minimum N per period — completion trigger behavior:**

When a "Minimum N per period" task is closed, the bot always creates the next task in the **current period**, regardless of how many completions have occurred. Reaching N does not close the period — more completions are always welcome. The period only advances at the cron (when the calendar period ends).

#### Bot re-trigger prevention

When the bot cancels a task or creates a new one, the Notion API updates `last_edited_time` on the affected pages. These pages will reappear in the next poll. Re-triggering is prevented by the in-memory snapshot: after processing, the snapshot stores the post-update state. On the next poll, `prev_group == current_group == Complete` — no transition detected, no trigger.

#### Governance invariant

On every daemon startup, `run_recurring_governance()` checks that each active definition has exactly one open task:

- **One open task** → no action (update `Current Open Task` on RTD if it doesn't match)
- **Multiple open tasks** → log warning; user must resolve manually
- **Zero open tasks** → consult `Current Open Task` on the RTD to determine cause:

```
Current Open Task = empty AND no related tasks exist
  → True first run → create task normally

Current Open Task = empty AND related tasks exist (all Complete)
  → Field was cleared (by bot after close, or by user) → create normally

Current Open Task = set → fetch page directly via GET /v1/pages/{id}:

  → 404 (permanently deleted from trash)
      → treat as deleted → create replacement task (see Deletion Reaction below)

  → archived: true (moved to trash — includes UI Delete and Del key)
      → treat as deleted → create replacement task (see Deletion Reaction below)

  → archived: false, status in Complete group
      → task completed while daemon was down → create normally

  → archived: false, status NOT Complete, not in full task query
      → Recurring Series relation was removed from task
      → log warning, create normally (not a deletion)

  → archived: false, status NOT Complete, IS in full task query
      → data inconsistency (should not reach here if open tasks == 0)
      → log warning, do nothing
```

#### Deletion reaction

When governance determines a task was deleted or archived, the bot creates a replacement task with:

- **Name**: `{original series name} (see note from bot in content)`
- **Status**: `On Hold` (draws user attention)
- **Page content** (prepended to top): `"Previous task in this Recurring Task Definition series was Deleted. If you wish to stop the creation of new tasks, please Deactivate the Definition itself."`
- All other fields follow normal task creation logic (Due Date, Instance #, Period Key, etc.)

To stop a series: set `Active = False` on the RTD. Governance will no longer create tasks for inactive definitions.

#### Scheduled governance (governance cron)

Runs daily at 2:00 AM server timezone. Low traffic, day is clearly over, users have had the evening to notice and react. Server timezone used because the program is self-hosted.

The governance cron is **not a separate process** — it is a time-triggered run of the full governance suite (both `AUTOMATIONS` per-page pass and `GOVERNANCE` cross-page pass) within the daemon's poll loop. See §3.1 for the implementation pattern.

Functions that need to run at the period boundary belong in the existing registries — no new infrastructure is needed:

| Responsibility | Where it lives | Called via |
|---|---|---|
| Grace period auto-close | function in `GOVERNANCE` | `run_governance` |
| Future-period task promotion | function in `GOVERNANCE` | `run_governance` |
| Tasks Done This Period reset | function in `GOVERNANCE` | `run_governance` |
| Current Period field update | function in `GOVERNANCE` | `run_governance` |

#### Period boundary behavior

The governance cron runs the full governance suite. At period boundary it performs two distinct checks:

**1. Carry-over: open task from the previous period**

If the current open task's **Due Date** falls in a previous period (i.e. the task was never closed and carried over), it is stale. For Responsibilities, the grace-period-cap auto-cancel handles this. For other types, governance updates the display fields:

- Update `Period Key (Recurring Task)` (display label) to the current period key
- Update `Occurrence # this Period (Recurring Task)` per the cadence rules: reset to 1 for `Once per period`, `At most N per period`, `Minimum N per period`; continue incrementing for `Unlimited`; never reset for `Bad Habit`

This keeps the open task's bot-managed fields accurate without closing or replacing it.

**2. User-created future task: promotion at period start**

If the cron finds an open task with a `Due Date` that fell in a future period that has now become the current period (i.e. a user created or edited a task with a forward due date):

- Assign it the next `Instance #` in sequence and the current `Period Key`
- If the cadence limit for the current period has **not** been reached (e.g. `Minimum N per period`, `Unlimited`):
  - Keep the existing bot-created `Current Open Task` as-is; the user's task coexists
- If the cadence limit **has** been reached (e.g. `Once per period`, `At most N per period` where N is met):
  - Archive the bot-created task (`archive_page()`) — preserves data, removes from queries, prevents automation re-trigger
  - Set `Current Open Task` on the RTD to the user-created task
  - The user-created task becomes the authoritative current task

**Period Key and Instance # are owned exclusively by the bot — never updated by the per-page automation in response to user field changes.**

- **Due Date changes** do not affect Period Key or Instance #. Moving Due Date into the past means the task is overdue — it still belongs to the current period's count. Moving Due Date into a future period creates the user-created future task scenario handled by check 2 above. In all cases the cron corrects any Period Key drift at the next run.
- **Closed Date changes** affect two things differently:
  - *`Tasks Done This Period` counting* — uses `Closed Date` date as ground truth. A retroactively set Closed Date (e.g. user marks a task closed and sets Closed Date to last week) automatically counts toward the correct past period. No special handling needed.
  - *Next task creation (Instance # and Period Key)* — `_create_next_task` must determine whether a new period has started by computing the period key from the closed task's `Closed Date` date, **not** from its `Period Key` field. If it used `Period Key` field, a retroactive Closed Date would leave Period Key pointing to the current period → `new_period = False` → Instance # increments instead of resetting. Closed Date date is the ground truth for which period a completion belongs to.

    **Same-poll-cycle note:** `auto_closed_date` and `auto_recurring_tasks` run in the same poll cycle. Automations collect their updates in-memory and write them to Notion together at the end of the cycle, so `Closed Date` stamped by `auto_closed_date` may not yet be in the page object when `_create_next_task` runs. `_create_next_task` upserts `closed_task` (the current page object) into its fetched task list before calling `_task_in_period`. If the user pre-filled `Closed Date` before marking Done, `_task_in_period` uses it directly. If `Closed Date` is absent (will be stamped by `auto_closed_date` after the poll), `_task_in_period` takes the open-task branch and uses the task's `Due Date` (or `now()` if absent). A task completed by the user in the current period will have a `Due Date` in the current period, so the period count is correct regardless. No `Period Key` fallback is used.

**3. Reset `Tasks Done This Period` on the RTD**

After the carry-over and promotion checks, the cron recomputes `Tasks Done This Period` for each RTD from scratch: count all related MT tasks where `Closed Date` falls within the current period's date range. This value overwrites whatever was stored, correcting any drift from daemon downtime or missed events. `Closed Date` is used as ground truth (not `Period Key` matching on the task). No extra queries needed — task data is already fetched by `run_recurring_governance`.

#### Known gaps

- Deleted/archived tasks: detected via `Current Open Task` field + direct `GET /v1/pages/{id}` call during governance. Both "Move to Trash" (`archived: true`) and permanent deletion (404) are treated identically — replacement task created with note and On Hold status. See Governance Invariant and Deletion Reaction above.
- Task naming with cadence suffix (e.g. `Weekly Therapy — Apr 2026`): deferred — depends on Project Page / configuration design (see STATUS.md Planned Features).

---




## 8. Adding a New Automation

1. Write a function in `automations.py` following the signature in §3.3
2. Add any governance checks inside the same function
3. Register it in `AUTOMATIONS` at the bottom of `automations.py`
4. Add the required Notion fields to your database if they don't exist

No changes to `daemon.py` or `notion_client.py` are needed for new automations.

---

## 9. Known Limitations

- **Notion's `last_edited_time` is floored to the minute.** Pages edited within the same minute as the poll window boundary may occasionally be missed or double-processed. In practice this is rare and self-correcting on the next poll.
- **No change history.** The daemon only knows "what it was last time I looked." If a field changes twice between polls, only the final value is seen. For a 60-second poll interval this is unlikely to matter.
- **Single-threaded.** Databases are polled sequentially, not in parallel. With multiple large databases the effective poll interval may be longer than `POLL_INTERVAL`.
- **No retry logic.** If a Notion API call fails, the error is logged and that page is skipped until the next poll.
- **File/attachment properties are excluded from the snapshot** but are still visible in Notion. Automations cannot act on file contents.
- **Archived pages are invisible to `query_database`** but remain fetchable by ID via `get_page()`. The bot explicitly checks `page.get("archived")` whenever fetching `Current Open Task` directly.
- **User edits to bot-managed fields made mid-poll are silently overwritten.** If a user edits a field (e.g. `Closed Date`, `Period Key`) between when the daemon fetched the page and when it writes its computed values, the bot's write wins. The daemon has no mechanism to detect or merge concurrent edits. This is an inherent limitation of polling-based automation without optimistic locking.

---

## 10. Decision Log

Resolved design decisions. Each entry states the rule and the reason so future contributors don't re-litigate them. All entries are final — open decisions live in STATUS.md.

---

### Task & Series Behavior

**[Q1] When a recurring task is deleted or archived, the bot creates a replacement — not deactivates the RTD.**
- Reason: the bot cannot distinguish intentional deletion from accidental deletion. Creating a replacement with a visible note puts the decision in the user's hands. To stop a series, the user sets `Active = False` on the RTD.

**[Q1] Deletion detection uses `Current Open Task` + a direct `GET /v1/pages/{id}` call.**
- Reason: `query_database` never returns archived pages. A direct fetch distinguishes "archived" (`archived: true`) from "permanently deleted" (404) from "Recurring Series link removed" (page found, not in relation query). Both archived and 404 are treated as deletion.
- Assumption: "Move to Trash" in the Notion UI and the Del key both result in `archived: true` via the API. Permanent deletion from trash results in a 404.

**[Q1] `Current Open Task` on the RTD always points to the last bot-created open task.**
- Reason: tracking "most recent" by Due Date or Created Date is unreliable — some tasks have bot-assigned Due Dates, some don't, and users can remove them. The bot sets this field at creation time, making it authoritative without any comparison logic.
- The bot does NOT update this field for user-created or user-reopened tasks (except the manually-linked task promotion case — see Q-C below).

**[Q1-B] When a user re-opens a Responsibility task from Complete, `Current Open Task` is left unchanged.**
- Reason: the re-opened task is a deliberate user override. `Ignore Grace Period` is set to True on it. The bot-created task remains the authoritative open task for the series.

**[Q1-C] A manually-created task linked to an RTD is initialized by the bot on the next poll.**
- No grace window is given before initialization — the bot sets Instance #, Period Key, Period Target on first poll.
- If Due Date is empty or within the current period → becomes `Current Open Task` immediately.
- If Due Date is in a future period → initialized but not set as `Current Open Task`; the governance cron promotes it when the period arrives.
- If the cadence limit is already met when the period arrives → archive the bot-created task, set `Current Open Task` to the user-created task.

**[Q1] Archiving uses `archive_page()` (Notion trash), not Cancellation.**
- Reason: Cancelling a task triggers `auto_recurring_tasks` (status → Complete group), which would create another new task. Archiving removes the page from all `query_database` results immediately, preventing re-triggering, while preserving the data for reporting.

**[Status groups] "On Hold" belongs to the In Progress group.**
- Reason: "On Hold" means the task has been acknowledged but paused — it's started, not unstarted. This ensures `Is Open` formula and In Progress group filters include it correctly.

**[Fields] `Is Open` formula is user-created in Notion, not bot-managed.**
- Value: `prop("Status") == "Not started" or prop("Status") == "In progress" or prop("Status") == "On hold"`
- Reason: rollup fields in Notion cannot filter by condition; a checkbox formula is the workaround to count only open tasks. The bot never writes this field.

**[Fields] `Manual Created Date` is user-managed only — bot never writes it.**
- Reason: used for retroactively created tasks where the user wants accurate reporting dates. No bot logic depends on it.

**[Fields] Recurring task Name always comes from the RTD title, not the previous task.**
- Reason: `base_props` (which includes the RTD-derived Name) wins the `_copy_task_fields` merge. This is intentional — the RTD title is the canonical series name. Users who want a one-off rename can edit the task instance; the change does not carry forward to the next task.

**[RTD Start Date] Automated RTD activation via a Start Date field is not implemented.**
- User activates RTDs manually by setting Status to Active. The manual step is lightweight.
- The motivating use cases (returning from vacation, seasonal responsibilities) are better served by reminder tasks the user already creates.
- Adding a Start Date field would introduce a second activation mechanism alongside Status, complicate governance logic, and require RTD monitoring for real-time response — complexity not justified by the benefit.

### Governance Behavior

**[Q2a] Cadence type formerly called "N per period" is renamed "At most N per period".**
- Reason: "N per period" implied exactly N. "At most N" correctly frames it as a soft cap — the bot continues creating tasks past N but alerts the user. Instance # increments within the period and resets at period boundary via the governance cron.
- No due dates for this cadence type — occurrence tracking only. Enforced in `_calc_due_date`: `At most N per period` returns `None` alongside `Unlimited`.
- Alert mechanism when N is reached: `RTD_AT_MOST_N_REACHED` Bot Note on the RTD (see [Q12]).

**[Q2a-ii] "Exactly N per period" is the canonical name for the hard-quota cadence type. The legacy Notion select option "N per period" must be renamed.**
- Behavior: governance counts completions in the current period (excluding cancelled/skipped). When count ≥ N, the next task is routed to the next period (`force_next=True`). If completions somehow exceed N, `RTD_EXACTLY_N_EXCEEDED` Bot Note is added to the RTD.
- Differs from "At most N per period": "At most N" is a soft cap (bot keeps creating, just warns). "Exactly N" is a hard quota (bot routes to next period once N is met).
- The legacy code normalization (`"N per period" → "Exactly N per period"`) has been removed. The Notion select option must be renamed before deploying.

**[Q2b] Three RTD types: Habit, Bad Habit, Responsibility — all in one RTD database.**
- Reason: user wants Habits and Bad Habits conceptually unified. Habits and Responsibilities share 12/15 fields — separating them gains little and doubles config burden.
- Bad Habit field clutter (6/15 fields used) is accepted; unused fields are documented as "Bad Habit: ignored."
- All task instances (all three types) live in the same MT database.
- Bot logic stays in `recurring_tasks.py` (one module); split only when file size demands it.

**[Q2b] Bad Habit type forces Unlimited cadence regardless of RTD configuration. Period IS used for Instance # reset cadence.**
- Reason: bad habit incidents are reactive (logged after the fact), not scheduled. No due dates, no anchor, no grace period, no cadence type or limit. Period determines when Instance # resets — giving the user at-a-glance context per period (e.g. "4 times this week") rather than a growing lifetime number that loses meaning.

**[Q2b-i / Grace Period] `Do Not Autoclose` checkbox added to RTD.**
- Reason: Responsibilities with empty Grace Period previously defaulted to 0 days (immediate auto-cancel on due date). `Do Not Autoclose = True` overrides all auto-cancel logic for that RTD, making the Responsibility behave like a Habit for closure purposes.
- Option A (empty Grace Period = never cancel) was rejected in favour of Option B (explicit checkbox) for UI clarity.

**[Appointments] Appointments require pre-scheduling of multiple future occurrences — fundamentally different from one-at-a-time recurring task creation. Flagged for separate design.**

**[Q3] `Ignore Grace Period (Recurring Task)` never resets once set to True.**
- Reason: the user re-opening a task signals they want to own its closure. Resetting on close would fight the user if they re-open it again. Bot backs off permanently on that task instance.
- New tasks created when the task eventually closes start with `False` (default) — fresh slate.
- Trigger: bot sets True on **all** Responsibility re-opens (Complete → non-Complete transition), regardless of whether Grace Period is currently set or Do Not Autoclose is True on the RTD.
- Reason for universal trigger: if the RTD later gains a Grace Period or Do Not Autoclose is changed, an already-reopened task would be unexpectedly auto-cancelled. The re-open is a fact about the task instance, not about the RTD's configuration at that moment. Keeping the flag clean at the cost of a True-when-unnecessary checkbox is far preferable to a surprising bot cancellation.

**[`auto_closed_date`] Renamed from `auto_last_closed`. Field renamed from `Last Closed` to `Closed Date`.**
- On close: recurring tasks respect a user-set `Closed Date`; non-recurring tasks always stamp with `now()`.
- On reopen: bot clears `Closed Date` and increments `Reopen Count`. Guarantees the next close always sees an empty `Closed Date` and stamps correctly — no stale dates persist across reopen cycles.
- Reason for reopen clear: Notion does not clear date fields on status change. Without explicit clearing, a stale `Closed Date` would cause `auto_closed_date` to skip stamping on the next close (recurring task path: "already set → leave it").
- **Missed reopen during daemon downtime:** if a task was closed while the daemon was running (Closed Date stamped), then the user reopened it while the daemon was down, the daemon will see a non-Complete task with `Closed Date` still set on the next startup. `auto_closed_date` treats this as a missed reopen: it increments `Reopen Count` first (using `Closed Date` as the signal), then clears `Closed Date`. No transition detection (`prev_page`) needed — the field state alone is sufficient.
- `Reopen Count` is NOT inherited when a new recurring task is created — new tasks start at 0.

**[Q11] Appointments: explicitly not being designed or implemented in this project.**
- Notion Automator is the wrong tool for appointment scheduling. Reliable appointment management requires push notifications, calendar sharing (iCal/CalDAV), and conflict detection — none of which Notion provides reliably, particularly on mobile (iOS/Android notification behaviour is inconsistent and platform-dependent).
- Sharing appointments via Notion pages is not equivalent to a calendar invite and has not been tested for multi-person scheduling.
- Platform migration (e.g. iOS → Android) is a concern for any calendar-adjacent tooling; native calendar apps handle this better, and paid migration tools exist if needed.
- If calendar integration is ever needed, the recommended path is a Notion → Google Calendar sync via an external automation tool (e.g. Make, Zapier) rather than logic in this daemon.
- Classified as "not ever" for practical purposes — not due to lack of interest, but because it sits below items that don't yet have names on the priority list, and the fundamental platform mismatch (Notion ≠ calendar app) makes this the wrong place to solve it regardless of priority. This entry exists to record the decision so it is not re-litigated.

**[Q12] "At most N per period" limit reached: surfaced via `Bot Notes` on the RTD.**
- Issue code: `RTD_AT_MOST_N_REACHED`. Written by `run_recurring_governance` when the count of tasks for the current period meets or exceeds N. Cleared when the period rolls over.
- The bot continues creating tasks past N — the note is informational, not a hard stop.

**[Q10] Task name suffix / period progress indicator: user-built Notion formula, no bot involvement.**
- Reason: consistent with the policy of not modifying user content fields. Bot-managed name suffixes would conflict with user edits, require updates as other fields change, and add complexity for no benefit over a native Notion formula.
- User formula example: `format(prop("Occurrence # this Period (Recurring Task)")) + " of " + prop("Period Target (Recurring Task)")` — displays "2 of Minimum 3 per Week" using fields already written by the bot. No new fields needed.
- Period Progress as a rollup: users can surface `Tasks Done This Period` from the RTD back to the task via the `Recurring Series` relation rollup. Fully Notion-native, zero bot work.

**[Q9] RTD Name uniqueness: warn via `Bot Notes` field, do not enforce or auto-rename.**
- Reason: auto-renaming the RTD Title is too invasive — it's user content. Warn-in-logs-only is too invisible. `Bot Notes` on the RTD is the middle ground: visible in Notion, user decides what to do.
- Duplicate detection runs in `run_recurring_governance` (GOVERNANCE). Both duplicate RTDs get the `RTD_DUPLICATE_NAME` note. Note clears automatically when resolved.
- Auto-rename with `(N)` suffix (OS-style) was considered and rejected as invasive.

**[Q7] Grace period > period length: hard cap at "1 day after the start of the new period."**
- Reason: without a cap, an arbitrarily large grace period (e.g. 9999 days) leaves a stale task open indefinitely alongside an ever-growing chain of new tasks. The cap limits overlap to at most 2 open tasks for at most 1 day.
- "Close" means Cancelled — same as normal grace period expiry, which triggers next-task creation.
- Enforced at runtime by the cron, not at task creation time. The stated Grace Period value is not modified.
- Implementation: if task's `Due Date` falls in a past period (derived via `_period_key`) AND `now >= current_period_start + 1 day` → cancel, regardless of grace period value. `Due Date` is used as ground truth; the stored `Period Key (Recurring Task)` field is never read for this comparison.

**[day_start_hour] Configurable logical day boundary — times before this hour count as "yesterday."**
- `day_start_hour` (global config, default 3) sets the hour when the logical day begins. Any datetime between midnight and `day_start_hour` is attributed to the previous calendar day for all period calculations: period key, occurrence #, quota counting, and governance cron trigger.
- Replaces `governance_hour`. Both purposes (when governance runs, when the day turns over) are intentionally unified under one variable — users who ask "when does my day reset?" have a single answer.
- Default 3am: statistical nadir of human activity; minimizes accidental day crossovers for night-owl users without affecting early risers.
- Implementation: `_period_dt(dt)` in `recurring_tasks.py` returns `dt - timedelta(hours=_day_start_hour)`. `_period_key` applies `_period_dt` before any strftime call. No callers of `_period_key` change — the offset is invisible to them.
- Period label convention: the period key uses the calendar date of the logical day start. A period key of "2026-05-29" covers midnight May 29 + day_start_hour through midnight May 30 + day_start_hour (exclusive). For default day_start_hour=3: "2026-05-29" covers 3am May 29 – 2:59am May 30.
- Extreme circadian case (e.g. active 6pm–11am): set `day_start_hour` to shortly after waking (e.g. 14 for 2pm). The period label will be the calendar date of waking, not sleeping. Users should reframe tasks as belonging to the day they woke up in.

**[Q6] Grace period auto-close is evaluated by the governance cron only — not during the regular poll loop.**
- Reason: the user may be mid-edit when the condition becomes true. Firing within 60 seconds would cancel a task the user is actively working on. The cron gives the full day to react.
- Implementation: moves from `auto_recurring_tasks` (AUTOMATIONS) to a dedicated GOVERNANCE function.
- Due Date changes require no special handling — the cron re-evaluates `now() > due_date + grace_period` at the next run. Moving the due date forward extends the window naturally; moving it to the past brings the condition forward. Same logic either way.

**[Period Target sync] `Period Target (Recurring Task)` is synced on every task poll, not only at task creation.**
- Reason: if the user changes Cadence Type or Cadence N on the RTD, existing open tasks would display a stale target until they were manually touched or the daemon restarted.
- Implementation: at the end of `auto_recurring_tasks`, for any initialized non-Complete task, the expected target is computed from the live RTD and compared to the task's current field value. If they differ, the field is updated in the same poll cycle.
- **Limitation:** the RTD database is not in the `database_ids` poll list — the daemon does not watch RTD pages for changes. Period Target sync fires the next time the *task itself* is edited, or at daemon restart (init pass). If the RTD changes but no task edits occur, the sync is delayed until one of those triggers. This is an acceptable trade-off — RTD config changes are infrequent and the drift is cosmetic only.

**[Period field change] Changing `Period`, `Cadence Type`, or `N Cadence` on an RTD mid-series takes effect on the next governance run.**
- Governance drift-corrects Period Key, Occurrence #, and Period Target on all tasks in the current and future periods on every pass. An RTD config change is fully reflected after the next startup, governance cron, or RTD activation trigger — no `--reconcile` needed for current/future tasks.
- Historical closed tasks (periods before the current period) are not corrected by normal governance. Use `--reconcile` if historical records also need correction.
- Due Dates on existing open tasks are not rewritten — only newly created tasks get Due Dates computed from the new settings. An open task may retain a Due Date from the old period granularity; it will close normally and the replacement task will have the correct Due Date.
- **Workaround for immediate application:** set RTD Status → inactive, wait one poll cycle, set back to Active. The Status → Active transition triggers an immediate governance run. A dedicated Force Governance checkbox on the Automation Hub is planned — see PLANNED.md.

**[Instance #] Assigned by COUNT of existing tasks, not MAX+1.**
- Reason: MAX+1 creates gaps or inflated numbers if a user edits Instance # values directly. COUNT of related tasks for this RTD in the current period is resilient to user edits — the value of Instance # on existing tasks is irrelevant, only the count matters.
- Bad Habit: period-based count (same as other types) — Period field determines reset cadence.
- Deleted/archived tasks are excluded from `query_database` results and therefore from the count. Their slot in the sequence is lost; this is acceptable.
- Re-opened tasks keep their original Instance # — a re-open is not a new instance.

**[Q5] `Tasks Done This Period` is recomputed from scratch at each governance pass — not maintained in memory.**
- Reason: real-time increments would go stale during any daemon downtime. Computing from `Closed Date` dates in Notion at startup and governance cron guarantees accuracy regardless of restarts or missed events.
- Count: related MT tasks where `Closed Date` falls within the current period's date range. `Closed Date` is used as ground truth — not `Period Key` field matching.
- No extra queries needed: task data is already fetched inside `run_recurring_governance`.
- The governance cron also updates `Period Key (Recurring Task)` and `Occurrence # this Period (Recurring Task)` on any open task that carried over from a previous period (see §7.1 Period boundary behavior).

**[Q4] `Previous Task (Recurring Task)` field and `Link Previous Tasks` RTD checkbox removed — no bot logic needed.**
- Reason: the original intent (easy reference to the prior instance, e.g. to copy a car mileage reading) is fully achievable via a Notion sorted view of the Recurring Series relation: sort by Closed Date (primary, descending) → Due Date (secondary) → Created Date (tertiary). The most recently closed task surfaces at the top. No bot work required; no schema overhead.

**[Fields] MT database recurring task fields use `(Recurring Task)` suffix in their names.**
- Reason: distinguishes bot-managed recurring task fields from general task fields at a glance in the Notion UI.

### Architecture

**[Architecture] The governance cron is a time-triggered run of the full governance suite — not a separate process or registry.**
- Reason: the governance suite (per-page AUTOMATIONS pass + cross-page GOVERNANCE pass) already does exactly what the cron needs. Adding a separate cron system would duplicate infrastructure for no benefit.

**[Architecture] Webhook-based event delivery is not used — polling is the delivery mechanism.**
- Notion webhooks require a publicly reachable HTTPS endpoint. On a home server behind NAT, this means port forwarding + SSL cert management, or a tunnel service (e.g. Cloudflare Tunnel) that requires a domain and a second daemon. Either option meaningfully raises the setup bar for semi-technical users.
- More importantly, the edge cases and workaround logic in the bot are not caused by polling — they stem from Notion's loose data model (missing fields, renameable columns, no push on deletion). Webhooks would not simplify any of that logic. The correct mitigations are Project Page (surface errors to the user) and field names as config (catch renames at a single point).
- On daemon downtime, a governance/catch-up poll on restart already recovers missed events — the same safety net webhooks would need anyway.
- Implementation: `_is_cron_time()` check inside the poll loop triggers the same two-phase sequence as startup. Any function needing period-boundary execution belongs in the existing registries.

**[Architecture] `GOVERNANCE` registry added alongside `AUTOMATIONS` in `automations.py`.**
- Reason: `run_recurring_governance()` was hardcoded in `daemon.py::main()`, making cross-page governance invisible to users and requiring them to edit `daemon.py` to add their own. The `GOVERNANCE` list follows the same pattern as `AUTOMATIONS` and gives users a consistent, documented extension point.
- Governance function signature: `fn(client: NotionClient) -> None`. Each function fetches its own data.
- Runs globally (once at startup, not per-database) — per-database governance is not supported; complexity without a demonstrated need.
- `daemon.py` calls `run_governance(client)` (formerly `run_governance_functions`) which iterates `GOVERNANCE`, replacing the hardcoded `run_recurring_governance(client)` call.

### Bug Fixes & Code Changes

**[Z16] Post-edit snapshot: `run_automations_on_page` stores the post-write page as `prev_page` for the next poll.**
- Reason: before this fix, the snapshot stored the pre-edit page state. On the next poll, the bot's own writes appeared as "user changes" and could trigger spurious re-runs of change-detection automations.
- Implementation: `update_page_properties` returns the updated page from the Notion API. This `post_edit_page` is stored as the snapshot entry, so the next poll's `prev_page` already reflects the bot's values. Bot-created pages (new recurring tasks) are returned via the `BOT_CREATED_PAGES_KEY` sentinel key in the automation's return dict, inserted into the snapshot immediately so the next poll has a valid `prev_page` for them.

**[Z17] `_period_dates` anchor-day bug: weekly anchor-day tasks were computing the wrong week on days past the anchor.**
- Root cause: an `elif not use_next and days_ahead < 0: days_ahead += 7` branch added 7 days when governance ran on a day after the anchor, pushing the target date into next week.
- Fix: removed the `elif` branch. `days_ahead` is now allowed to be negative for current-period lookups. The resulting `target_date` falls in the correct ISO week because negative offsets from Saturday still land in W20 (not W21).
- Impact: Occurrence # and period boundary detection now use the correct week for any governance run after the anchor day within the same week.

**[Z18] Period Key corruption: stale-check false-cancels from trailing whitespace in the stored `Period Key` field.**
- Root cause: Notion's rich-text editor sometimes appends a newline when a user closes a page. A stored value of `"2026-W19\n"` compared lexicographically as less than `"2026-W20"`, triggering the stale check and auto-cancelling an active task.
- Fix (part 1): the stale check now derives the task's period from `Due Date` (or `now()` if absent) instead of reading the stored `Period Key` field. A corrupted Period Key field cannot affect cancel decisions.
- Fix (part 2): all remaining reads of the stored `Period Key` field now call `.strip()` before comparison to guard against latent whitespace in any code path that still reads the field.

**[Z19] Period Key removed from all period-membership logic — derived from dates in memory.**
- Reason: the stored `Period Key (Recurring Task)` field was a repeated failure point. Users accidentally introduced trailing newlines (Z18); bot stamps could land on the wrong week (Z17); manual edits could corrupt it. Removing it from logic eliminates the entire class of field-corruption bugs.
- Rule: open tasks → period determined by `Due Date` (fallback: `now()`, meaning no-Due-Date open tasks always count as current period); closed tasks → period determined by `Closed Date` (backfilled from `last_edited_time` by `auto_closed_date` governance for any Complete task missing it).
- `Period Key (Recurring Task)` field is retained as a display-only label written by the bot for human readability in Notion. It is never read by bot logic for period comparisons anywhere in the codebase.
- Initialization gate changed from `period_key is None and instance_num is None` to just `instance_num is None` — removes the last read of the stored Period Key field from governance initialization.

**[Field rename] `Instance # (Recurring Task)` → `Occurrence # this Period (Recurring Task)`.**
- Reason: "Instance #" was ambiguous — it sounded like a global sequence number across all periods rather than a within-period count. "Occurrence # this Period" is self-documenting.
- Bot writes use the new field name. The old field name must be retired in the Notion database before deploying updated code (bot writes to a non-existent field fail silently).

**[Per-database automation config] Automations are enabled per-database via `[[databases]]` blocks in `config.toml` (replaces flat `database_ids` list).**
- Flags: `closed_date`, `reopen_count`, `due_date_tracking`. Absent = disabled (opt-in).
- Rationale: prevents batch-poisoning — if a flag's column doesn't exist, that flag is simply disabled rather than causing a 400 error that silently rejects the entire update batch (including writes to other columns like Closed Date).
- Implementation: `automations.py` holds `_db_configs: dict[str, dict]`. `register_db(db_id, cfg)` is called once per database at startup. Each automation reads `page["parent"]["database_id"]` to look up its flags — no signature changes to automation functions.
- Closed Date is required for recurring tasks to function. If absent from the task database schema, a CRITICAL error is logged at governance startup.

**[Optional task fields] `Period Key`, `Occurrence # this Period`, and `Period Target` are optional columns in the task database.**
- Users who do not want these columns in Notion can omit them. The bot will not crash and will not produce 400 API errors — writes to absent optional fields are silently skipped.
- Required fields (Status, Due Date, Recurring Series, etc.) are not filtered — a 400 on those still surfaces correctly.
- Implementation: on first governance or automation run, the bot queries the task database schema via `GET /v1/databases/{id}` and caches the property set for the session (`_task_db_properties`). `_filter_optional(props)` strips absent optional fields from every write payload. Schema is loaded once at startup; a restart is required to detect newly added columns.
- `OPTIONAL_TASK_FIELDS` constant in `recurring_tasks.py` defines the set. Adding a field to this set makes it optional without any other code changes.

**[RTD Series State] The `Active` checkbox on RTD pages is replaced by a `Status` field.**
- Bot logic checks `Status == "Active"` only. Group membership (To-do / In Progress / Done) is irrelevant to the bot — new statuses added in Notion are safe by default (ignored unless explicitly coded).
- Governance queries only Active RTDs via Notion API filter: `{"property": "Status", "status": {"equals": "Active"}}`.
- `auto_recurring_tasks` checks the fetched definition's Status value before creating a next task on close.
- Constants: `RTD_STATUS_FIELD = "Status"`, `RTD_ACTIVE_STATUS = "Active"` in `recurring_tasks.py`.
- No destructive action on open tasks when an RTD goes inactive — least-destructive-intervention principle. Open tasks remain; user closes them when ready.

**[RTD Monitoring / Z1] The RTD database is polled in the main loop; governance triggers only when an RTD is activated.**
- Problem: governance only ran at startup and the daily cron. A new RTD or an RTD toggled to Active mid-session got no task until the next governance pass.
- Fix: `_poll_rtd_for_changes()` runs each loop iteration (same interval as task DB polls). It triggers `run_governance()` only when an RTD's Status transitions to Active (including newly created RTDs already set to Active). Other field changes (Grace Period, N Cadence, Anchor Time, etc.) update the snapshot but do not trigger governance — they take effect at the next scheduled governance run.
- Rationale for trigger scope: field changes like Grace Period do not require an immediate governance run; triggering on every RTD edit caused spurious governance runs and race conditions where governance fired before Notion propagated the edit.
- After governance fires (whether from RTD activation or governance cron), the RTD snapshot is fully refreshed from the API. This ensures bot-written Bot Notes become the new baseline and do not re-trigger governance on the next poll.
- `rt_defs_id` is hoisted to function scope so the poll loop can access it regardless of whether recurring tasks were fully initialized.

**[Governance drift correction scope] Normal governance corrects Period Key, Occurrence #, and Period Target on all tasks in the current and future periods — not open tasks only.**
- Rationale: correcting only open tasks left closed tasks in the current period with stale fields after an RTD config change (e.g. Period changed from Daily to Weekly). The next governance pass now fixes all current+future tasks regardless of status.
- Occurrence # uses oldest-first sort (by Closed Date if set, else Due Date; no-date tasks last) starting at 1, incrementing only for non-cancelled tasks. Cancelled tasks share the slot number of the next non-cancelled task (same slot = failed attempt at that occurrence).
- Historical periods (period key < current period key) are not touched by normal governance. Use `--reconcile` to correct historical records.
- Tasks cancelled in the current governance pass are treated as cancelled for Occurrence # purposes (via the `cancelled_ids` set) even though `all_tasks` still shows them as open.
- `--reconcile` remains for force-writing all periods including history; normal governance is drift-only (writes only when value has changed).

**[week_start] The first day of the week is configurable via `week_start` in `config.toml` (global key).**
- Default: `"Sunday"` — matches Notion's default week view. Set to `"Monday"` for ISO/work-week convention. Accepts any full day name ("Sunday"–"Saturday").
- Global config key (not under `[recurring_tasks]`) — treated as a workspace-wide calendar preference, consistent with `day_start_hour`.
- Parsed in `daemon.py`, passed to `recurring_tasks.init(week_start_day)` as a 0–6 integer (0 = Monday, 6 = Sunday).
- `_week_start_day` module variable in `recurring_tasks.py`; affects `_week_start_date()` helper, `_period_key()`, `_period_dates()` (no-anchor span), `_period_start()`, and `_period_end()`.
- Period key format change: weekly period keys changed from ISO `"YYYY-Www"` to date-based `"W-YYYY-MM-DD"` (date of week-start day). Existing open recurring tasks with old-format period keys should have their `Period Key (Recurring Task)` field cleared before deploying, or the display value will show the old format until the next bot write.

---

