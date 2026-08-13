# Notion Automator — Planned Features

Living design document. Sections are deleted when a feature is implemented and its decisions have moved to `DESIGN.md`. See `STATUS.md` for one-line summaries and current priority.

---

## Contents

- [Improvement: Snapshot Pruning / Deleted Page Detection](#improvement--snapshot-pruning---deleted-page-detection)
- [Improvement: Governance Schema Validation](#improvement--governance-schema-validation)
- [Improvement: Schema-Check Safety Net in automations.py](#improvement--schema-check-safety-net-in-automations-py)
- [Improvement: Minimum N Per Period — Carry-Over Instead of Archive](#improvement--minimum-n-per-period---carry-over-instead-of-archive)
- [Feature: Configurable Field Inheritance for Recurring Tasks](#feature--configurable-field-inheritance-for-recurring-tasks)
- [Automation Hub (formerly "Project Page")](#automation-hub--formerly--project-page)
- [Clear Blocking/Blocked-By on Close](#clear-blocking-blocked-by-on-close)
- [Automated Testing](#automated-testing)
- [Bulk Edit Tool (tools/)](#bulk-edit-tool--tools)
- [Habit Due Dates with Rolling Forward](#habit-due-dates-with-rolling-forward)

- [Undeveloped Ideas](#undeveloped-ideas)

---

## Improvement: Snapshot Pruning / Deleted Page Detection

**Status:** Pre-design
**One-liner:** Remove deleted Notion pages from the in-memory snapshot so stale entries do not accumulate across sessions or cause confusion after manual task deletion.

### Problem

`poll_database` initialises each new snapshot from a copy of the previous one (`new_snapshot = dict(snapshot)`). Pages deleted from Notion simply stop appearing in API results, so they are never removed. They stay in the snapshot indefinitely. Governance self-corrects because it calls `query_database` fresh each run, but the stale snapshot can confuse the automation path when manual task manipulation (delete + reopen old task + close) desynchs state. Workaround: deactivate and reactivate the RTD to force a fresh governance pass.

### Options

**Option A: Governance-level exclusion.** During each governance pass, cross-reference the open tasks for each RTD against the snapshot. Any snapshot entry not returned by the live query is treated as deleted and excluded from open-task counting. Low complexity; self-contained to the governance loop.

**Option B: Snapshot pruning on full poll.** Periodically run a full (non-incremental) `query_database` for each watched database and remove snapshot keys that no longer appear. More thorough but adds API calls. Natural fit for the daily governance cron.

**Option C: Per-page existence check.** When a snapshot entry has not been seen in N polls, verify it with a direct `GET /v1/pages/{id}` call and drop it if archived or deleted. Most precise but most API-heavy.

Option A is the right starting point. Option B is a clean addition to the existing daily cron pass.

### Dependencies
- None — self-contained daemon improvement

---

## Improvement: Governance Schema Validation

**Status:** Pre-design
**One-liner:** At each governance run, validate that RTD fields and bot-expected task table fields have the correct Notion property types and (for select/status fields) the expected option values. Warn and continue — daemon never stops over a schema mismatch.

### What gets validated

**RTD database fields:**
| Field        | Expected Type | Value check                                                                                                   |
| --------------| ---------------| ---------------------------------------------------------------------------------------------------------------|
| Type         | select        | Options must be a subset of {"Habit", "Responsibility", "Bad Habit"} — warn on any unrecognized value present |
| Cadence Type | select        | Options must be a subset of the known cadence type set — warn on any unrecognized value                       |
| Period       | select        | Options must be a subset of the known period set — warn on any unrecognized value                             |
| Status       | status        | Must include "Active" as an option                                                                            |
| N Cadence    | number        | —                                                                                                             |
| Anchor Day   | number        | —                                                                                                             |
| Grace Period | number        | —                                                                                                             |
| Anchor Time  | rich_text     | Format check (HH:MM) already warned in _calc_due_date — reference here for completeness                       |

**Task table fields (bot-managed):**
| Field | Expected Type |
|---|---|
| Ignore Grace Period | checkbox |
| Occurrence # (Recurring Task) | number |
| Period Key (Recurring Task) | rich_text or text |
| Period Target (Recurring Task) | rich_text or text |

### Behavior
- Runs at the top of each governance run (daily cron + startup) — not just once at startup.
- Warn and continue — daemon never halts over a schema issue.
- Until Hub exists: log WARNING only.
- When Hub exists: also write to Hub Section 2 "Errors & Warnings" for RTD warnings; Hub Section 1 "Errors & Warnings" for task table warnings.

### Dependencies
- Automation Hub for surfacing warnings visibly. Log-only until then.

> **Note:** This is distinct from the Schema-Check Safety Net below. This feature validates schema health proactively at governance time. The Safety Net prevents crashes defensively at write time during every poll. Both are needed.

---

## Improvement: Schema-Check Safety Net in automations.py

**Status:** Ready to implement
**One-liner:** `automations.py` should check field existence before writing, matching the `_filter_optional` pattern already in `recurring_tasks.py` — so a misconfigured or missing field logs a clear error and skips rather than sending a 400 to Notion.

### Two layers (both kept, neither replaced)
1. **Config flag** (`closed_date = true` etc.) — user consciously opts in. Unchanged.
2. **Schema-check safety net** — if the flag is on but the field doesn't exist in the Notion database, log a clear error and skip that write. The rest of the automation (and other automations) continue normally.

### Why this matters
Currently, if a config flag is `true` but the field is missing from Notion, the write attempt reaches Notion's API and returns a 400 error that can poison the entire update batch for that page. `recurring_tasks.py` already avoids this via `_filter_optional` — `automations.py` needs the same safety net.

### Implementation
- At automation startup (or governance), load the task DB schema into a set (already done in `recurring_tasks.py` via `_load_task_db_schema` — expose or share this)
- Before each property write in an automation, check if the field name is in the schema
- If missing: `logger.error(f"Field '{field}' not found in database schema — skipping write. Check your Notion setup.")` and skip that field only
- **Hub integration:** until Hub exists, log-only. When Hub is available, also write the error to the "Errors & Warnings" column for the affected database row in Section 1. Every Hub-writing code path must check Hub availability first.

### Dependencies
- `_task_db_properties` is currently private to `recurring_tasks.py` — either expose it or replicate the schema-loading pattern in `automations.py`.
- Hub integration requires Automation Hub to be implemented and `hub_page_id` to be configured.

---

## Improvement: Minimum N Per Period — Carry-Over Instead of Archive

**Status:** Ready to implement
**One-liner:** At period boundary, when Minimum N is already met, carry the remaining open task forward into the new period (resetting field tracking) instead of archiving it.

### Decision
The archive-on-minimum-met behavior was designed before governance could reset field tracking fields. Now that rolling forward is established as a pattern (see Habit Due Dates), archiving is unnecessary and removes a valid "do more than the minimum" opportunity.

**New behavior:**
- **Minimum met, period ends** → carry the open task forward: update Due Date to new period, clear First Due Date, reset Due Date Update Count to 0, correct Period Key and Occurrence #. No archiving. Governance sees the carried-over task covering the current period and skips creating a new one.
- **Minimum NOT met, period ends** → cancel + create replacement. Accountability signal intact. (Unchanged.)

### Implementation
In `run_recurring_governance`, replace the `archive_page()` call for the "minimum met" stale-task case with carry-over field writes (same pattern as Habit rolling forward).

### Dependencies
- Habit Due Dates with Rolling Forward (same field-reset pattern — implement together or ensure field-reset logic is shared)

---

## Feature: Configurable Field Inheritance for Recurring Tasks

**Status:** In design (whitelist approach chosen; lives in Hub Section 2)
**One-liner:** Let users control which fields are inherited when a recurring task is created using a whitelist — without touching code or config files.

### What was fixed (P3)
Root cause of inheritance failures was never `_copy_task_fields` logic — fields were being copied correctly. Failures came from API rejections on `create_page`:
- `files` type properties → Notion API rejects file attachments on create/update. Fixed: added `files` to `_READONLY_PROP_TYPES`.
- `people` type properties → read format includes full user objects (`name`, `avatar_url`, etc.); write format only accepts `{"id": "..."}`. Fixed: `_copy_task_fields` now strips people entries to ID only.
- Graceful fallback added: if `create_page` with inherited fields fails, log a WARNING (always visible) with Notion's error message, then retry with bot-managed fields only — task is always created.

### Code cleanup (pending, independent of Hub)
Two fields should be added to `FIELDS_NOT_INHERITED` in `recurring_tasks.py` regardless of the Hub feature:
- `"Name"` — already overwritten by `base_props` on every task creation; adding it makes the exclusion explicit and avoids a pointless copy.
- `"Primary Tasks (Relationship)"` and `"Secondary Tasks (Relationship)"` — back-relations to the Task Relationships database. Currently inherited, which would link the new task to the same relationship entries as the closed task. Relationships are task-instance-specific and must never carry forward.

### Configurable inheritance design (Automation Hub)

This belongs in **Hub Section 2 (Recurring Tasks)**, not `config.toml` and not Hub Section 1. Reasoning:
- Field inheritance is a recurring-task-creation concern, not a general per-database automation flag.
- Users should be able to reconfigure without restarting the daemon.
- `config.toml` is for workspace identity and connectivity (token, DB IDs). Behavioral schema decisions live in the Hub.

**Approach: whitelist (inclusive) only.** The blacklist failure mode (wrong data silently inherited) is worse than the whitelist failure mode (missing data, visibly empty). A field not listed is simply not inherited — safe default. No Inheritance Mode toggle needed.

Configured in Hub Section 2 as a text field:
```
Inherited Fields: Priority, Effort, Area, Pursuit
```
Parsed as CSV; names matched case-insensitively against the live task database schema at startup.

**Startup validation:** at daemon startup, each name in Inherited Fields is resolved against the task database schema. Any name that does not match a real property logs a WARNING:
```
WARNING: Inherited Fields contains 'Priorty' — no matching property found. Field skipped.
```
The warning is also written to Hub Section 2 Warnings field. This handles typos and renamed properties without storing IDs or writing back to the Hub.

Hardcoded invariants (`FIELDS_NOT_INHERITED`, `_READONLY_PROP_TYPES`) remain in code regardless of user config — bot-managed fields and API-unwritable types are never inherited.

**Default when Inherited Fields is empty:** no user fields are inherited. Bot-managed fields (Name from RTD, Recurring Series, Due Date, etc.) are always set regardless.

### Dependencies
- Automation Hub must exist before this can be configured at runtime.
- Code cleanup (Name + relation fields added to `FIELDS_NOT_INHERITED`) can ship independently before the Hub.

---

## Automation Hub (formerly "Project Page")

**Status:** Pre-design
**One-liner:** A single Notion page serves as the hub for all Notion Automator configuration, health status, and errors — one place to see and manage everything the bot does.

**Name note:** "Project Page" was used internally but is ambiguous. "Automation Hub" is preferred going forward. References in DESIGN.md and STATUS.md should be updated when this feature moves to implementation.

### Decisions made so far
- Auto-detection over activation buttons — daemon detects what exists and creates what is missing; no explicit "activate" step.
- User creates the Hub page manually and adds its ID to `config.toml` (one-time step). Bot creates the 3 child databases automatically on first run.
- Notion page IDs are permanent — users can move the Hub page anywhere after initial creation.
- Preference: automatic setup with a clear startup log of what was created.
- **Single hub for all automations** — not a page-per-database. All task database configs and recurring task config live here together, mirroring the structure of `config.toml`.
- **Optional — graceful degradation:** if `hub_page_id` is absent from `config.toml`, all Hub-controlled behavioral features default to off. No config.toml fallback for behavioral settings — absence means disabled. Every Hub-writing code path must check Hub availability before writing.
- **Field name parsing (all text-list fields):** Hub text fields that accept field name lists (First Value Fields, Update Count Fields, Inherited Fields) are parsed as CSV with: strip leading/trailing whitespace per token, case-insensitive match against the database schema, support double-quoted names for fields whose names contain commas.
- **Recurring Series type validation warning (Section 2):** if an RTD's cadence type is incompatible with its Task Type (e.g., "Maximum N per period" with Task Type ≠ "Bad Habit"), surface the warning in Hub Section 2 Warnings field.
- **Hub has 3 child databases:** Task Databases (Section 1), RTD Config (Section 2), Hub Status (Section 3). All three created by the bot on first run.
- **Hub Status is a single-row database** — structured columns, clean API access via `query_database`, consistent with the other two sections.
- **Closed Date and Reopen Count are text fields** (not checkboxes) containing the Notion property name to write to. Empty = feature disabled. Allows custom column names. Reopen Count requires Closed Date field to be set — warn if Reopen Count is configured but Closed Date is empty.
- **Inheritance settings live in Section 2 (RTD Config)**, not Section 1 — inheritance is a recurring-task-creation concern, not a general per-database flag.
- **Separate Errors and Warnings fields** on each database row — not combined. Enables clean formula-based health indicators (e.g. `not empty(prop("Errors"))` → 🔴).
- **Whitelist inheritance only** — no Inheritance Mode toggle. See Configurable Field Inheritance feature.
- **Template link for task DB and RTD setup** — bot links to the published Notion template rather than attempting to scaffold databases with formulas, rollups, and relations via API (not reliably possible). Bot-scaffolded setup applies only to the 3 Hub child databases.
- **non_completion_statuses moves to Hub** (global, not per-database). A closed status has the same meaning regardless of which task database it's in.
- **poll_interval, week_start, day_start_hour stay in config.toml** — they are operational parameters that affect fundamental loop timing and period calculations; changing mid-session is either meaningless or risky. Hub page displays a callout noting their location on the server.
- **Hub change detection in the poll cycle** — the daemon watches specific fields on Hub child database rows for changes between governance passes, the same way it watches RTD pages. Fields that trigger a config reload: `database_id`, `closed_date_field`, `reopen_count_field`, `first_value_fields`, `update_count_fields` (Task DB rows); `definitions_db_id`, `tasks_db_id`, inheritance fields (RTD Config row). `Name`, `Errors`, and `Warnings` are bot-written and never trigger a reload.
- **Name field on Task DB rows is bot-refreshed at governance** — bot looks up the database title via `get_database()` and writes it. User does not type it manually. Same for RTD Config row: bot writes both database names.
- **Hub write timing:** at startup (config/schema validation errors) AND at each governance pass (governance errors, health stats update). Mid-poll automation errors (e.g. a 400 from Notion on a specific task write) are log-only until next governance.
- **Config file split:** three files, all in `.gitignore`:
  - `config.toml` — user-managed; token, DB IDs, hub_page_id, poll_interval, day_start_hour, week_start. Unchanged from current.
  - `config_hub.toml` — bot-written; Hub child database IDs discovered or created at setup. User should not edit. Written once at Hub setup, read on every subsequent startup.
  - `config_hub_backup.toml` — bot-written; mirror of Hub behavioral config written at each governance pass. Human-readable TOML for reference and manual restore. No auto-restore logic.
- **Hub backup scope:** behavioral config only (automation flags, inheritance fields, global settings). Not health stats or error logs. Written at each governance pass (daily + startup).
- **Daemon version:** bot writes its `VERSION` constant to Hub Status on each governance pass. GitHub repo link added as a static block during Hub page setup.
- **Machine and daemon health stats:** last poll time, last governance run time + trigger (`server-reset` / `scheduled` / `force-governance`), daemon uptime (computed from recorded start time), machine uptime (read from `/proc/uptime`), CPU temperature (read from `/sys/class/thermal/thermal_zone0/temp` on Linux/Pi — skipped gracefully if unavailable). CPU% and memory usage deferred (requires `psutil` dependency).
- **Nexus summary** (user-built, not bot-managed) — a user-maintained Notion page with formula fields pulling health indicators from the Hub. Not a bot concern for now.

### Layout concept (mirrors config.toml structure)

**Section 1: Task Databases**
A child database (table view) where each row is a task database the bot monitors. Bot writes the Name field (database title) at each governance pass — user does not fill it in manually.

| Column              | Type      | Notes                                                                                            |
| ---------------------| -----------| --------------------------------------------------------------------------------------------------|
| Name                | Title     | Bot-written: database title looked up via `get_database()`. Refreshed at each governance pass.  |
| Database ID         | Text      | Notion database ID. Bot triggers config reload when this changes.                                |
| Closed Date Field   | Text      | Property name to stamp on close (e.g. "Closed Date"). Empty = disabled.                         |
| Reopen Count Field  | Text      | Property name to increment on reopen (e.g. "Reopen Count"). Empty = disabled. Requires Closed Date Field to be set — bot warns if configured without it. |
| Update Count Fields | Text      | CSV of fields to count updates for (e.g. "Due Date").                                           |
| First Value Fields  | Text      | CSV of fields to snapshot on first observation (e.g. "Due Date, Status").                       |
| Errors              | Rich Text | Bot-written. Cleared when resolved.                                                              |
| Warnings            | Rich Text | Bot-written. Cleared when resolved.                                                              |

**Section 2: RTD Config**
A child database with one row per recurring task setup (definitions DB + tasks DB pair). Bot writes database names at each governance pass.

| Column            | Type      | Notes                                                                                            |
| -------------------| -----------| --------------------------------------------------------------------------------------------------|
| Name              | Title     | Bot-written: e.g. "Recurring Tasks — Task Tracker".                                             |
| Definitions DB    | Text      | RTD database ID. Bot also writes the database name next to it for readability.                  |
| Tasks DB          | Text      | Task database ID. Bot also writes the database name next to it for readability.                 |
| Inherited Fields  | Text      | CSV of task field names to copy when creating a new recurring task. Empty = inherit nothing. Validated against live schema at startup — unmatched names logged as warnings. |
| non_completion_statuses | Text | CSV of status names treated as non-completions (e.g. "Cancelled, Skipped"). Global — applies to all task databases. |
| Errors            | Rich Text | Bot-written. Cleared when resolved.                                                              |
| Warnings          | Rich Text | Bot-written. Cleared when resolved.                                                              |

Warnings to surface here (currently only logged):
- **Anchor Day ignored (N>1):** RTD uses "Exactly N per period" or "Minimum N per period" with N>1 and Anchor Day set — Anchor Day suppressed.
- **"Maximum N per period" with wrong Task Type:** no due date will be set.
- **Unknown Task Type:** RTD Type is empty or unrecognized.
- **Inherited Fields name not found:** field name in Inherited Fields does not match any property in the task database schema.

**Section 3: Hub Status**
A child database with a single row representing the daemon. Written at each governance pass.

| Column              | Type      | Notes                                                                                            |
| ---------------------| -----------| --------------------------------------------------------------------------------------------------|
| Name                | Title     | Static: "Notion Automator".                                                                      |
| Version             | Text      | Bot-written: daemon `VERSION` constant. Updated each governance pass.                           |
| Last Poll           | Date      | Timestamp of the most recent successful poll cycle.                                              |
| Last Governance Run | Date      | Timestamp of the most recent governance pass.                                                    |
| Governance Trigger  | Select    | What triggered the last governance run: `server-reset`, `scheduled`, `force-governance`.        |
| Daemon Uptime       | Text      | Human-readable elapsed time since daemon start (e.g. "3d 4h 12m").                             |
| Machine Uptime      | Text      | Read from `/proc/uptime` on Linux/Pi. Skipped gracefully if unavailable.                        |
| CPU Temperature     | Text      | Read from `/sys/class/thermal/thermal_zone0/temp` on Linux/Pi. Skipped if unavailable.         |
| Errors              | Rich Text | Bot-written. Cleared when resolved.                                                              |
| Warnings            | Rich Text | Bot-written. Cleared when resolved.                                                              |

A static block on the Hub page (written once at setup) notes: "poll_interval, week_start, and day_start_hour are set in config.toml on the server and require a daemon restart to change." GitHub repo link also included as a static block.

Activity counters are a back-burner idea. Preferred approach: derive counters from the Notion_Analytics data pipeline rather than maintaining them independently in the Hub.

### On "selectable field" for First Value Field tracking
Notion does not have a native field-selector property type (the relation/rollup selector in the UI is not exposed as a reusable property). The closest approximation: a text field where the user types the field name manually. This is consistent with how `first_value_fields` works in `config.toml`. A dedicated child database (one row per tracked field, with a relation back to the Task Databases table) is possible but likely over-engineered for v1.

### Task DB and RTD database setup

Bot scaffolding via `create_database()` applies only to the 3 Hub child databases (Task Databases, RTD Config, Hub Status). These are simple, fully API-writable structures.

The task database and RTD database are **not scaffolded by the bot** — they contain formulas, rollups, and two-way relations that cannot be reliably created via the Notion API. Instead, the Hub page includes a static link to the published Notion template as the canonical setup reference. Keeping the template current is the maintainability obligation in exchange for not having to maintain API scaffolding code.

A callout block on the Hub page (written once at setup) links to the template and notes any fields or formulas the user needs to configure manually after duplicating it.

### Required code additions
- `create_database(parent_page_id, title, properties, icon=None)` — to be added to `notion_api.py`. POST to `/databases` with page parent. First Hub prerequisite.
- `config_hub.toml` read/write logic — daemon reads child database IDs from this file at startup; writes them after Hub setup or first-run database creation. Avoids block scanning on subsequent startups.
- `config_hub_backup.toml` write logic — written at each governance pass with a TOML mirror of the Hub's behavioral config (automation flags, inherited fields, global settings, database IDs). Human-readable for reference and manual restore. No auto-restore.
- Hub poll-cycle change detection — daemon snapshots relevant Hub database row fields each poll and triggers a config reload (and governance if appropriate) when they change. Mirrors the RTD change-detection pattern.
- Bot must read Hub config at startup (after `config.toml`) and apply to running config.
- Bot must write errors/warnings per-database-row and per-section rather than only to logs. Write timing: startup (validation errors) and governance pass (governance errors + health stats).
- Hub availability check: every Hub-writing path must check that Hub is configured and reachable. If not: log-only, no write attempted.

### Config layer ownership (settled)
- `config.toml` = workspace identity and connectivity (token, DB IDs, Hub page ID, poll interval). Requires restart to change. Not for behavioral decisions.
- Automation Hub = behavioral and schema-aware config that users adjust without touching files or restarting. Examples: automation flags per database, inheritance mode + field list, First Value Field config, webhook URLs.
- When Hub config and `config.toml` conflict: Hub takes precedence for flags; `config.toml` required only for IDs and the Hub page ID itself.

### README additions (pending)
- **Status Icon formula** — add to README Usage Guide as a "visual status indicator" callout. Formula uses Bot Notes field, Recurring Series relation, Blocking/Blocked By relations to show concatenated emoji at a glance (overdue, recurring, blocked, blocking, has bot note). Formula text to be inserted once confirmed. Note: formula references bot-managed fields so it's directly relevant to automator users.
- **Notion Tips callout** — Status Icon formula is the one tip tightly coupled to bot behavior; other tips (Task Creation Hub, button fields, parent/child buttons) belong in a separate personal Notion page, not the README.

### Deliverables
- `docs/recurring-task-usage-guide.md` — Notion-importable markdown guide for recurring task configuration. Self-contained; no links to code or version-specific internals. Avoid heavy table use (Notion markdown import renders tables inconsistently). Scope: how to configure your first RTD, common patterns, non-obvious behaviors. Distinct from the README Usage Guide (which is for technical readers of the tool docs). Include a section on `day_start_hour` — explain that tasks closed before `day_start_hour` count toward the previous day's period, and give guidance for extreme circadian rhythms.

### Force Governance checkbox

A checkbox field on the Hub page itself (not per-RTD). On each poll, the daemon checks this field. If checked: trigger `run_governance()`, uncheck it, log that a user-initiated governance run fired.

**Rationale:** RTD config changes (Period, Cadence Type, N Cadence) don't trigger governance immediately — they take effect at the next startup or daily cron. The deactivate/reactivate workaround works but requires two edits and a polling wait. A single checkbox is cleaner and more discoverable. Global scope (all RTDs) is acceptable — the use case is almost always "I just changed something, apply it now."

**Placement:** Section 3 (Bot Health) or as a standalone field near the top of the Hub page.

**Implementation notes:**
- Daemon reads Hub page properties each poll (already needed for other Hub features).
- If Force Governance is checked: call `run_governance(client)`, then write `False` back to the checkbox via `client.update_page_properties()`.
- Log at INFO: `"Force governance triggered by user via Automation Hub."`

### Open questions
- None currently open — all major design questions resolved in the August 2026 design session.

### Dependencies
- Automation Hub must exist before Notifications can store webhook URLs there.
- `create_database()` API method needed in `notion_api.py` first.
- Every automation must be updated to write its errors/warnings to the Hub row for its database rather than only to logs.

---

## Clear Blocking/Blocked-By on Close

**Status:** Ready to implement
**One-liner:** When a task is closed (moves to Done group), clear its "Blocking" and "Blocked By" relation fields so completed tasks don't appear as active blockers.

### Decisions made

- Clear both "Blocking" and "Blocked By" on close.
- If the relation is two-way synced in Notion (standard setup), clearing "Blocking" is sufficient — Notion automatically clears the corresponding "Blocked By" entries on the other tasks. Clearing both defensively is still fine and covers the one-way case.
- No historical preservation of the blocking relationship in this feature.
- Trigger: same Done-group transition check used by `auto_closed_date`. No new transition detection needed.

### Open questions
- What are the exact Notion property names for these two fields? Need to confirm before implementation.

### Dependencies
- None. Standalone automation function, no dependency on other planned features.

---

## Automated Testing

**Status:** Pre-design (deferred — add after feature set stabilizes)
**One-liner:** Unit tests for pure logic functions that have caused the most bugs — no mocking needed, no Notion API dependency.

### Decisions made
- Defer until design settles. Most bugs have been in edge cases that manual testing catches well; a test suite written mid-churn would need constant rewriting.
- Scope: pure logic functions only for Phase 1 (done). Phase 2 adds `_create_next_task` tests via a mock `NotionClient` — same scaffolding covers multiple high-value cases.

### Phase 1 — done (63 tests, pure functions)
- `_period_key` — period boundary edge cases
- `_calc_due_date` — end-of-period, anchor day, monthly/weekly math, Habit Due Dates
- `_is_overdue_by` — grace period precision

### Phase 2 — mock `NotionClient` scaffolding (beachhead: `_create_next_task`)
Build a minimal mock `NotionClient` that returns fabricated task lists from `query_database` and records `create_page` calls. All Phase 2 tests share this scaffolding.

Priority targets once scaffolding exists:
- **Icon inheritance** — emoji copied from RTD, file-type icon skipped, no icon when RTD has none
- **Anchor-time duplicate prevention** — when anchor_time has passed for Period=Day, `_create_next_task` must correct `target_period_key` from the actual due date and skip creation if an open task already exists for that period. Without this test, the June 2026 regression (infinite duplicate creation) is not caught automatically.
- **Minimum N carry-over** — governance carries stale task forward instead of archiving when minimum is met; no new task created
- **Habit rolling-forward** — open Habit task from previous period gets Due Date reset to current period; no new task created
- **Maximum N duplicate guard** — task creation skipped when current-period count already at cap

### What NOT to test (yet)
- Full governance end-to-end — requires live or deeply mocked Notion state with all RTD and task page shapes
- Integration tests — require a real Notion workspace; better handled by manual verification

### Dependencies
- Feature set should be stable before investing in tests. Revisit after PowerBI pivot.

---

## Bulk Edit Tool (tools/)

**Status:** Idea (not yet designed)
**One-liner:** CLI script in `tools/` to apply mass or individual property edits to Notion pages — e.g. recategorize a group of tasks, fix a field value across many pages.

### Context
Power BI is read-only — no write-back possible. When analysis reveals data that needs correcting in Notion (wrong category, bad status, etc.), the fix has to happen in Notion itself. A CLI tool here (alongside `fix_closed_date_timezone.py`) lets you select pages by filter and patch one or more properties in bulk, with the same backup-before-edit safety pattern already established.

### Decisions made
- Lives in `tools/` in this project (Notion_Automator owns all Notion write operations).
- Should follow the backup-then-patch pattern from `fix_closed_date_timezone.py` — write a CSV of old values before applying any changes.
- Counterpart `revert_from_backup.py` already exists and is generic enough to handle reverts.

### Open questions
- Selection method: filter by property value (e.g. all tasks where Area = "X")? Manual page ID list? Both?
- Interactive confirmation showing a preview of what will change before committing.
- Scope: single database only, or cross-database edits?

### Dependencies
- None. Standalone script using existing `notion_api.py`.

---

## Habit Due Dates with Rolling Forward

**Status:** Pre-design
**One-liner:** Give Habit tasks a Due Date, but instead of cancelling on expiry, roll the Due Date forward to the next period — closer to Responsibility behavior without the punitive cancel.

### Motivation
Currently Habits get no Due Date, so they never appear in Notion "today"/"this week" calendar views and there's no visible sense of which period a Habit belongs to. Rolling forward — rather than cancelling — preserves the "try again, never fail" spirit of a Habit while giving it calendar visibility throughout the period. This replaces what was previously tracked as "Due Date Visibility Throughout Period."

### Decided
- Governance rolls Due Date forward to the new period at the period boundary — does not cancel or archive the task
- Due Date spans the full period (range: period start → period end), matching Responsibility format
- If `Due Date Update Count` field exists: governance explicitly resets it to 0 AND clears `First Due Date` (null). On next poll, `auto_due_date_update_count` sees `First Due Date` empty → stamps it with the new period Due Date → returns early without incrementing. No -1 trick needed.
- Occurrence # resets to 1 on rollover (rolled-forward task is the first instance of the new period)

### Open questions
- If `Current Open Tasks` field exists: should governance update it when rolling forward (task stays open, same task, Due Date changes)?

### Dependencies
- `Current Open Tasks` field (for the open question above — not a blocker for the core rollover logic)

---

## Undeveloped Ideas

_Features with no active design work. Full history in git. Promote to a full section when actively designing._

- Extended Cadence (Every Y Periods) — support recurring cadences spanning multiple periods (bi-weekly, quarterly, etc.); simplified picker + Custom X-per-Y mode.
- Notifications — outbound webhook support (Discord, Telegram) for governance events; URLs stored in Automation Hub.
- Cumulative Overdue Time Tracking — bot-managed `Overdue Days` field incremented each governance pass the task is open and past-due.
- RTD Current Period Field — bot-written `Current Period` date range (start+end) on RTD; enables formula/rollup period filtering and Siri Shortcuts lookup.
- Timer / Mission Tracking — link closed tasks to mission areas; surface per-period effort heatmap. Scope unclear (Automator vs Analytics).
- Companion: Siri Shortcuts Integration — iOS shortcuts to close current period's open task for an RTD via voice; depends on RTD Current Period field.
- iOS Reminders Integration — use iOS Reminders (CalDAV) as reminder layer; creates reminder on due date set, clears on close.
- Range Cadence (Min N, Max M) — new Cadence Type enforcing both a minimum and a maximum per period.
- Task Templates — apply a Notion template to bot-created recurring tasks for user-configured buttons/content.
