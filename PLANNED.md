# Notion Automator — Planned Features

Living design document. Sections are deleted when a feature is implemented and its decisions have moved to `DESIGN.md`. See `STATUS.md` for one-line summaries and current priority.

---

## Contents

- [Improvement: Snapshot Pruning / Deleted Page Detection](#improvement--snapshot-pruning---deleted-page-detection)
- [Improvement: Governance Schema Validation](#improvement--governance-schema-validation)
- [Improvement: Schema-Check Safety Net in automations.py](#improvement--schema-check-safety-net-in-automations-py)
- [Improvement: Minimum N Per Period — Carry-Over Instead of Archive](#improvement--minimum-n-per-period---carry-over-instead-of-archive)
- [Feature: Configurable Field Inheritance for Recurring Tasks](#feature--configurable-field-inheritance-for-recurring-tasks)
- [Configuration Hub (formerly "Automation Hub" and "Project Page")](#configuration-hub-formerly-automation-hub-and-project-page)
- [Clear Blocking/Blocked-By on Close](#clear-blocking-blocked-by-on-close)
- [Automated Testing](#automated-testing)
- [Bulk Edit Tool (tools/)](#bulk-edit-tool--tools)
- [Habit Due Dates with Rolling Forward](#habit-due-dates-with-rolling-forward)

- [Feature: Task Continuation and Repeat Relationship Types](#feature--task-continuation-and-repeat-relationship-types)
- [Improvement: Migrate to Notion data sources (API 2025-09-03+)](#improvement--migrate-to-notion-data-sources--api-2025-09-03-)
- [Feature: Pursuit Hierarchy and Habit-to-Pursuit Link](#feature--pursuit-hierarchy-and-habit-to-pursuit-link)
- [Design Change Requests (2026-11-13)](#design-change-requests-2026-11-13)
- [Undeveloped Ideas](#undeveloped-ideas)

---

## Feature: Linked Page Automations

**Status:** Pre-design
**One-liner:** Allow for actions taken in one database to affect another database (through linked pages most likely)

### Problem
Managing multiple databases can be annoying. Being able to automate such connections will reduce user friction. 

### Examples
- **Parent task closes:** optionally close all linked child tasks. The rule must specify whether children mirror the parent's state or receive a named target state; default behavior should be no propagation.
- **Task closes:** find open linked Timebox sessions and stamp their close date/time. Whether the session status also changes must be separately configured.
- **Job application is submitted without a relation:** when an Application page receives an Applied Date, find the open recurring task in the Task database whose recurring definition is "Apply to Jobs" and whose Due Date is today or falls within the configured start/end period; then close that task. This is a deliberate cross-database match with no Relation property between the Application and Task pages.

The user should be able to perform the Application workflow entirely from the Application database. The daemon may update the matched Task page in the background, but the user does not need to open or edit the main Task table. The rule needs an explicit duplicate/ambiguity policy—for example, close exactly one unique match, or write a Bot Note and take no action when multiple tasks qualify.

Other candidates:
- Governance: when a Session is missing a related task for Primary or Secondary, remove the stale relation.
- Governance: when a Session remains un-ended past its end condition, close the Session.

### Relation-free matching example: Apply to Jobs

This example is not a relation lookup. It is a **source event → target query → target mutation** rule:

1. **Source event:** an Application page changes so `Applied Date` becomes set or changes.
2. **Target scope:** the configured Task database, restricted to open pages.
3. **Target identity:** the task's Recurring Series relation points to the specific `Apply to Jobs` RTD page. This is preferable to matching only by Pursuit because another task in the same Pursuit should not be closed accidentally. The implementation should use the stable RTD page ID where possible, with the visible name only as a human-readable label.
4. **Period predicate:** the task Due Date is today or is inside an explicitly configured start/end period. The rule must use Due Date/Closed Date semantics, not the stored Period Key display field.
5. **Action:** move the matched task to the configured Complete status and optionally record a Bot Note explaining the match.

The rule should support a configurable match policy:

- **Unique match required (recommended):** act only when one target qualifies; otherwise report an ambiguity warning.
- **First match:** allowed only with an explicit sort, such as nearest Due Date, and should be opt-in.
- **All matches:** useful for intentionally bulk behavior, but should require explicit opt-in and a result limit.

The source page does not need to be linked to the target task. This makes the behavior convenient from the Application database, but it also makes the matching predicate the safety boundary: names, dates, statuses, and series identifiers must be validated before any write occurs.

The RTD is designed to maintain **one open task at a time by default**, but that is an invariant the user may intentionally override by opening or creating additional tasks. An RTD relation therefore does not guarantee that only one task qualifies: several task pages may legitimately point to the same RTD, especially for `Exactly N per period` or manually created duplicates. Therefore the default policy should be **unique match required**. If zero or multiple open tasks match the RTD, status, and date window, the daemon should take no action and write a clear Bot Note or relationship-rule warning. The user can then close the intended task manually. Selecting the nearest task or closing all matches should be explicit opt-in policies, not implicit fallbacks.

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
- Configuration Hub for surfacing warnings visibly. Log-only until then.

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
- Hub integration requires the Configuration Hub to be implemented and its `config.toml` key set (`automation_hub_page_id` today, `config_hub_data_source_id` after the redesign).

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

### Configurable inheritance design (Configuration Hub)

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
- Configuration Hub must exist before this can be configured at runtime.
- Code cleanup (Name + relation fields added to `FIELDS_NOT_INHERITED`) can ship independently before the Hub.

---

## Configuration Hub (formerly "Automation Hub" and "Project Page")

**Status:** Pre-design
**One-liner:** A single Notion page serves as the hub for all Notion Automator configuration, health status, and errors — one place to see and manage everything the bot does.

**Name note:** "Project Page" was used internally but is ambiguous; it became "Automation Hub", and on 2026-11-13 the user renamed it **Configuration Hub** because configuration will live in a database of config pages. The Automator Status database stays separate. The code and the `hub_page_id` / `automation_hub_page_id` config keys keep their current names until a later rename with a deprecation alias (prod deploy prerequisites name the key). The branch name `feature/automation-hub` is unchanged.

### Redesign (user-confirmed 2026-11-13): config database with TOML in each row's page body

**Status:** Direction confirmed; not yet implemented. Where this conflicts with "Layout concept" (typed columns) and "Decisions made so far" below, this subsection wins. It reverses two earlier statements: DESIGN.md said stable config should use "typed Hub properties rather than an opaque JSON/TOML blob", and the inheritance design avoided "storing IDs or writing back to the Hub".

- **One config database** holds all configuration (Automator, and later Fuzzy Matching and Analytics). One row per config. Columns: Name (title), Status, Warnings, Errors. The body of each row's page holds one code block of TOML (user decision 2026-11-14: it matches the project's own config files and allows comments, so users can document or switch off parts of their config). A note above the code block explains the format and links to Wiki articles.
- **Starting points in `config.toml` (user decision 2026-11-14; reader implemented 2026-11-14).** `config_hub_data_source_id` is the data source ID of the config database: the place where config pages live. `status_data_source_id` points to a separate status data source (optional: without it there is no kill switch). These replaced `automation_hub_page_id`, which is no longer read. Both are data source IDs, not database IDs. The bot finds its own rows by the page IDs in `config_hub_state.toml` (`automator_config_page_id`, `automator_status_page_id`), then by default name, else creates them; it never reads or writes other tools' rows in the shared data sources (bug found 2026-11-14: the first version read every row, including the Fuzzy Match row). Built: `hub.parse_row_config`, `read_config`, `init`, `poll_for_changes`, `validate_hub_schema`. Not built yet: `inherit_icon`, property-ID cache in `config_hub_state.toml`, case-insensitive property-name checks against the live schema, Slice 3 health writes.
- **SUPERSEDED 2026-11-25 by "Config row, Report and Config Version" below: there is no separate status data source.** *(Original text, kept for the record:)* **Status stays a separate data source with runtime status on it (user decision 2026-11-14).** Runtime fields (last poll, last governance run, governance trigger, uptimes, version, kill switch, Force Governance) live on the status data source, not on config pages, because the bot rewrites them every poll and that would keep changing the config pages' last-edited time that change detection watches. Rows are per program (the Automator's default page title is now "Automator Notion Status"; `hub._STATUS_ROW_NAME`), and column visibility per program is done with database views, since Notion cannot hide columns per page. The existing Health formula and kill-switch Status property are kept.
- **Who creates the data sources (user decision 2026-11-14; now ONE data source, see 2026-11-25 below): the user does, from a published Notion template they duplicate.** The bot never creates them, so no parent page is needed (same pattern as the task and RTD databases). The README section "Setting up the Configuration Hub" lists the columns for anyone setting them up by hand or on an older version. Still to do: publish the template and put its link in the README.
- **No Kind column.** Each tool finds its own rows by page IDs saved in the bot-written `config_hub_state.toml`. If an ID goes stale, look the row up by Name, else create a new one. Row creation must be idempotent (cached page ID first, then Name) so a retry never makes duplicate rows.
- **SUPERSEDED 2026-11-25 (see "Config row, Report and Config Version" below).** *(Original:)* **The Automator Status database stays separate and unchanged** (single row, Section 3 above). No health rows for Fuzzy Matching or Analytics.
- **Adding an automation = adding a row.** No schema change. The TOML picks named triggers and actions from a typed vocabulary that code validates. It never carries free-form write instructions. Unknown keys produce a Warning.
- **The bot never edits the user's code block.** It reports problems in the row's Warnings and Errors columns.
- **No `"schema"` version key for now.** If a format change ever needs one, a missing key means version 1.
- **Property references use property IDs, never property types.** The user types a property name; the bot resolves it to a property ID at startup and at the daily governance pass, and caches `logical name | property ID | last-known property name` in bot-owned `config_hub_state.toml`. If the name no longer matches but the ID still exists, the bot uses the ID, updates the last-known name and writes a Warning naming the rename. Renaming a property keeps its ID (Notion docs, update data source properties: "Its ID stays the same"; confirmed in the raw page text 2026-11-14), and update requests accept either the ID or the name. If the user recreates a column (new ID), the last-known name is the human-readable link for re-mapping. This covers every property, including the Recurring Series relation (no find-by-type step, no two-relations check). It makes "N Cadence" to "Quota N" and "Recurring Series" to an emoji/acronym name a non-event for the code.
- **Cost:** the Hub is unreleased and the test Hub databases already need recreation, so there are no user rows to migrate. Rewrite `read_config` and `validate_hub_schema` in `hub.py` and up to 20 tests in `tests/test_hub_config.py`. Lost: Notion's per-field validation (a select cannot hold a typo, a code block can); mitigated by validation plus the Errors column.
- **Sequencing:** Slice 3 (Status writes) is unaffected. Slices 4-6 (error writes, change detection, Force Governance) depend on the config-row format, so settle it before them. Change detection should use each row's `last_edited_time`; editing a page's body updates its `last_edited_time` (user-confirmed 2026-11-14), so body edits can drive change detection.
- **Open questions:** (a) RESOLVED 2026-11-14: pages can be moved between data sources with `POST /v1/pages/{page_id}/move` and a `data_source_id` parent (path found in Notion's downloaded OpenAPI spec; the docs page says it needs Notion-Version 2026-03-11 and that `database_id` is not a valid parent for this call). Pages cannot move between "databases" as such, because a database is a container of data sources and views; (b) RESOLVED 2026-11-14: `toml` IS a valid code-block language. It is one of the 90 members of the `languageRequest` enum in Notion's OpenAPI spec (downloaded from developers.notion.com/openapi.json and searched locally), and the user confirmed it in the Notion app. An earlier entry here said it was not supported; that came from a stale docs page read through a summarizing fetch and was wrong. The bot creates the code block with language `toml`, and the reader should still accept any language tag; (b2) how the Automator ignores other tools' rows if they are not TOML (for example a Shortcut row written as JSON): undecided and not blocking, since no such rows exist yet; (c) daemon page-body reads cost one extra API call per row, so read bodies only for rows whose `last_edited_time` changed.

#### Config row, Report and Config Version (design agreed 2026-11-25; build steps 0-8 implemented 2026-11-25, unit-tested; live check of steps 5-7 pending, see STATUS.md)

Supersedes the separate status data source. Where this conflicts with the bullets above, this subsection wins. Settled in conversation 2026-11-25; items marked **to confirm** have a recommended default but no explicit user answer.

**One data source, no kill switch.** The status data source, `status_data_source_id`, the status row and `_REQUIRED_STATUS_PROPS` go away. The daemon warns if `status_data_source_id` is still set (same pattern as the old Hub keys). The bot-written `config_hub_state.toml` loses `automator_status_page_id`. Reason: with governance-only writes the bot no longer churns the config row's last-edited time, and `mark_verified` already writes to the config row with own-write suppression.

**Columns on the config data source.** Required: Name, Status, Errors, Warnings. Errors and Warnings stay separate, typed and shared by every tool's config rows (for sorting and filtering); they hold the full message text. Optional (written or read only if the column exists, like `Last verified time`):
- **Report** (text, user decision on the name 2026-11-25): bot-written, never parsed, not shared across tools in meaning (a tool-specific column such as "Analytics Report" is possible later). One line per operation, each with an emoji and a time: Config (the last read attempt, its time and result, and the loaded Config Version as a datetime), Governance, Version/started, and Poll (only with `verbose_status = true` in the row's TOML). A failure line names the operation and points to Errors; it never repeats the message text. Example: `🟢 Governance: OK · 2026-11-24 03:00 · daily cron`, `🔴 RTD: failed, see Errors`, `🟡 Config: edited, not applied`, `⚪ Config: not Active, nothing running`.
- **Config Version (Datetime)** (date, user-controlled; the property is named exactly this, in the style of `Period Key (Recurring Task)`): set to now by hand or by the Apply Config button; never written by the bot. Hover description (user wording, 2026-11-25): "Using Datetime for version to easily know when something is a newer version, and when that version was implemented. Just set to \"now\" when a change needs ingested. Manually set or use an \"Apply Config\" button to set the property value to \"now\"."
- **Apply Config** (button): its action is set up in the template by hand. Corrected 2026-11-25 from the Notion OpenAPI spec (`spec-verified`): a bare button property CAN be created through the API (`buttonPropertyConfigurationRequest` is an empty object in the create-property union), but it carries no action, so the action (set Config Version to Now; a button can set a date to Now, docs-relayed 2026-11-25) can only be added in the Notion app. The spec also allows creating a Status property with options and groups (`statusPropertyConfigurationRequest`). The button sets Config Version to Now. The bot never reads it. Property name and label can be the same text.
- **Last verified time** (existing): stays; decide later whether the Report's Governance line replaces it.
The existing Health formula (🆘 / ⚠️ from Errors and Warnings) is kept. The Status column keeps its options (Not started, Active, On hold, Discontinued); only Active acts and every other value does nothing, as for RTDs.

**When the bot reads and acts.**
| Event | Bot action |
|---|---|
| Row goes not Active to Active | Read the body and load it (the activation path already exists in `daemon.py`) |
| Row goes Active to not Active | Stop acting on that row's data sources and RTD pairs; no page-body read. `config.toml` and `config_hub_state.toml` stay as they are. Edits made while paused are caught by the governance run on reactivation, as today |
| Active row, Config Version differs from the saved version | Read the body and apply it |
| Config Version column missing | Fall back to the existing `last_edited_time` trigger |
| Daily governance and restart | Governance does not re-read config; restart reads it once because the bot has nothing loaded |

**Config Version rules.** The saved version is whatever the row's Config Version held when the body was last read (empty allowed). Change is detected by comparing the raw date strings (any difference counts, so setting it into the past still applies). Parse to UTC only for the "older than saved" Warning, which still applies the config. The bot does not fill an empty Config Version: the button's Now comes from Notion's clock and the bot's from the machine's, so skew would cause false "in the past" warnings, and the field stays user-owned.

**Severity (user rule 2026-11-25): an Error means the bot has no valid config loaded for that row, so nothing from the row is running** (first activation, or the first read after a restart with a broken body). **A Warning means a working config is still running** while a newer one has a problem. So: Warnings for edited but not applied, a failed apply while an older good config runs, Config Version older than the loaded one, and Config Version empty (the bot then waits for the first button press; it never auto-applies). Errors also for a Config Version column that is present but not a date. Column missing is not a message: the Report line notes the mode.

**Edited-but-not-applied check (a Warning).**
1. Every poll, at no extra API cost: compare the row's `last_edited_time` (already returned by the poll query) with the bot's last own write.
2. Only when it differs and has settled (see below): fetch the body once and compare PARSED TOML with the loaded config. Comparing the parsed result ignores comments, spacing and key order (verified with `tomllib` 2026-11-25), so those edits never warn; a changed array order or value does. Remember the checked timestamp so it is not re-fetched.
3. Daily governance: always compare the parsed body with the loaded config, as a safety net.
Settle delay (agreed 2026-11-25): do not warn while the user is still typing; warn only when the row has been unchanged for a settle time (default 5 minutes, a `config.toml` key as the handle). The same delay applies to the `last_edited_time` fallback used when the Config Version column is missing. The warning explains why the edit was not loaded: step 2 already parses the body, so it includes the TOML error with line and column, or the data-quality problem (bad ID, unknown key), checks that need no Notion call. Checks that need Notion (property names against the live schema) only run on an actual apply.

**Failed apply (agreed 2026-11-25; severity per the rule above).** Today, when the daemon re-reads a row with invalid TOML, `read_config` returns no databases for it and the daemon replaces its database list (task databases drop out), while RTD pairs are only replaced when the new list is non-empty (so they keep running): inconsistent (verified by reading `daemon.py` and `hub.py` 2026-11-25). Agreed: a failed apply keeps the last good config for both task databases and RTD pairs and writes a Warning (an Error only when nothing is loaded for the row, as after a restart). The Report says `🔴 Config: apply failed, still running version <saved>`. Pressing Apply Config again after each fix is the normal way to retry; each press costs one body read. Caveat (unverified): if the button's Now has only minute precision, two presses within the same minute give equal Config Version strings and the second is not detected; the edited-not-applied Warning still appears and says to press again.

**TOML writing.** `tomllib` only reads (no `dumps`), keeps key order and drops comments (verified 2026-11-25, Python 3.13.5). The only TOML the bot writes is `config_hub_state.toml`, as a hand-built text string with a static header comment; no TOML library is needed, and the user's config block is never written. If round-trip editing is ever needed: `tomlkit` (keeps comments and order, new dependency); `tomli_w` writes no comments.

**Not in this slice.** Force Governance checkbox (its status row is gone; revisit as an optional column), Slice 8 Hub page callouts (the template carries the instructions; dropped), the Hub-unavailable error page and the Last Use check (both in Undeveloped Ideas).

**Applying config is declarative (user requirement 2026-11-25).** After any config read, the running task databases and RTD pairs equal exactly what the Active rows say, including nothing. A pair stops when its row is not Active, its block is commented out or removed, or `enabled`-style switching in the row says so; no daemon restart is needed. Bug today (verified in `daemon.py` ~604): `configure_pairs` is only called when the new pair list is non-empty, so a removed pair keeps running until the daemon restarts, and `rt_defs_id` keeps being polled. The new apply function always calls it, with an empty list if need be (`configure_pairs` already resets its state), and clears `rt_defs_id`. An individual RTD definition page's own Status is separate and already handled by the RTD poll.

**Report times and zones (user decision 2026-11-25).** Notion's time zone is not available from the API, so Report times are shown in the zone of the Config Version value (its named `time_zone`, else the offset in the text), minute precision, display only; until one is read the machine's zone is used. Change detection compares the raw text, so daylight saving cannot look like a change. Implemented as `hub._zone_from_version` (assumption that the offset follows the user's Notion zone is not verified live).

**Hub and daemon boundary (agreed 2026-11-25).** `hub.py` owns everything that touches the Hub rows (read, write, validate, Report, version and edit checks, messages) and imports no project modules. `daemon.py` makes a few calls: init, poll (returns a ready-to-apply config or nothing), and report/flush. One apply function in `daemon.py` replaces the two copies of the apply logic (startup and loop), because it changes runtime state that lives there (`database_ids`, snapshots, `last_polled`, RTD pairs). Today the daemon calls the Hub in 11 places.

**Messages from several sources (proposed 2026-11-25).** Messages are kept in memory in `hub.py`, grouped by source, and written out as plain human-readable text that nothing parses (no JSON/TOML; the bot never reads them back).
- `hub.report(source, errors=[], warnings=[], row=None)` stores a source's current messages and makes no Notion call, so any code (daemon, governance, an automation) can call it. A source replaces only its own messages, so sources cannot erase each other's, and an empty report clears that source. Sources look like `config`, `governance`, `rtd:<name>`, `db:<name>`.
- `hub.flush(client)`, called by the daemon at the end of a poll, governance pass or apply, builds each row's Errors and Warnings text as `[source] message` lines and writes it only if it changed (one write per row; the existing `_write_row_messages` behavior). A message belongs to the row whose config defines its database or RTD pair, else to the Automator's own row.
- After a restart memory is empty; each source reports again as it runs, and the first flush replaces the column text wholesale.
- Limits (docs-verified 2026-11-25, raw `request-limits.md`): 2000 characters per rich-text object, 100 objects per array. `_message_text` currently cuts at 1900 characters without saying so; change to a visible marker such as `(N more, see the log)`.
- Documentation must state how to add an automation that reports to the Hub (one `hub.report` call and what its source name should be): README developer section, DESIGN.md extensibility section, `notion_wiki_drafts/automations.md`, and the setup docs.

**Deleted config row (user requirement 2026-11-25).** Today: mid-run, `read_config` logs an error and keeps the current config (nothing appears in Notion); at the next startup `_ensure_config_row` finds no row and creates a fresh inert one with the example text, so the user's config text is no longer in Notion. New: the bot writes a Warning on the row it creates, with a link to the old page (rich text may carry `text.link.url`, docs-verified 2026-11-25) and the path of the local backup copy. The Warning clears on its own once at least 24 hours have passed, or earlier on the next successful apply (to confirm: pressing Apply Config is the manual clear). The time the Warning was first written is kept in `config_hub_state.toml`, because memory is lost on restart. Whether Notion's trash lets the user restore the old page is unverified.

**Slice 7, kept in a small form (user question 2026-11-25).** The bot saves the raw text of the last applied config body (comments kept, no auto-restore) to a local bot-written file, as the recovery copy for a deleted or overwritten row. It was first suggested for removal on the assumption that the page body is the copy; that overlooked a deleted row.

**How the bot finds its row (Option A confirmed 2026-11-25; already the current design).** `config_hub_data_source_id` in `config.toml`; the bot finds its row by the page ID saved in `config_hub_state.toml`, then by default name, else creates it. `config_hub_state.toml` stays (only `automator_status_page_id` goes); the data source itself is not auto-created: a bare button property can be created through the API but its action cannot (spec-verified 2026-11-25), so a ready-made template is still needed. The spec does allow creating Status and button properties, which the live test script uses to build a throwaway copy in the sandbox.

**One stored config file (user decision 2026-11-25; replaces the earlier two-file idea).** A single file next to `config.toml`, `config_automations.toml`, in the same shape as a config row body and read by the same parser. When the Hub is on, the bot writes the last applied config text to it verbatim after each clean apply (comments and key order kept), starting with a header comment that says the bot writes it. The bot reads it only when the Hub is not available: the Hub is not configured (the file is then the user's own config, written by hand), or the Hub was available before (the bot's state file holds row IDs from an earlier successful start) and cannot be reached now. When the Hub is available it always wins. When the Hub is in use and a file with that exact name exists WITHOUT the bot's header comment (so a user wrote it): Warning on the config row and in the log, and the bot does not overwrite it, so no backup is kept until the user renames or removes it. `[[databases]]` and `[recurring_tasks]` in `config.toml` stop working entirely once this is built (user decision, so there is no third place to look). A `config.toml` that still has them makes the daemon stop with an Error that explains the conversion, instead of silently monitoring nothing, and a one-off helper in `tools/` prints the row-shape TOML from the old blocks (data-migration rule). `use_config_hub` was considered and not built (user decision 2026-11-25): leaving `config_hub_data_source_id` out turns the Hub off.

**Sandbox for live tests (user, 2026-11-25).** `sandbox_page_id` in `config.toml` and `sandbox_token` in `config_secrets.toml`. A live script (not part of the default test run, no automatic run) reads both itself, never prints them, creates throwaway data sources under the sandbox page (Status, date and bare button properties are allowed by the spec), runs the Hub read, apply and report paths end to end, and archives what it made. **The callout at the top of the sandbox page (it says what the page is dedicated to) is kept (user requirement 2026-11-25):** the script never edits, moves or deletes it, cleanup archives only the data sources and blocks the script itself created (tracked by the IDs it got back, never "everything under the page"), and at the start of a run it checks that the first block is still a callout and logs a warning if not (it does not create one). Test with a fake client: a sandbox page whose children are the callout plus one script-made data source loses only the data source. It refuses to run if the sandbox token equals the normal token, and the sandbox integration should be shared only with the sandbox page. Run only when the user asks (it connects to Notion).

**Names (user, 2026-11-25).** Decided and done: `config_hub.toml` is now `config_hub_state.toml` (bot-written state; it keeps the `config_hub` prefix so the files sort together, and the header comment says the user does not edit it); the local file was moved to the new name. Decided: the date column is named exactly `Config Version (Datetime)` ("Config Version" in this file is shorthand for it); `Config Updated` was suggested and rejected. Decided: the branch `feature/automation-hub` (it exists on origin) is renamed to `feature/config-hub`; the steps are in scratch.md because the new name must be pushed and the old remote branch deleted. Still listed: `config_backup.toml` and `config_hub_backup.toml` (legacy, unclear purpose; the second is superseded by `config_automations.toml`), `hub.py` (the module still says Hub), `config_hub_data_source_id` (does not say "config rows"), `data_source_id` in the TOML against the code's `*_db_id` names (a suffix, as in `tasks_db_id`), `rt_defs_id` and the RTD acronym, "Report" next to "Warnings" and "Errors", `tools/config.toml` and `tools/config_throw_away.toml`, `/tools` being gitignored ("for now").

**Documentation checklist for this redesign (user requirement 2026-11-25: every step in end-user docs).** Each item needs an entry in `docs/` (setup, upgrade or reference) and the README where it applies: rename `secrets.toml` to `config_secrets.toml` (upgrade, with the `icacls` re-run on Windows); `use_config_hub` and starting without the Hub; the one config data source (remove `status_data_source_id`, the status data source and the kill switch; set a row to a non-Active Status to pause it); the optional Report and Config Version columns and the Apply Config button with its action (steps in the Notion app); `inherited_properties` renamed to `inherit_include_properties` and the new exclude/icon keys; `config_automations.toml`; what a deleted-row recreation looks like and how to read the chain of links; how config problems show up in Warnings, Errors and the Report; the generic upgrade routine (done: `docs/upgrade_steps.md`).

**Example files and docs (user requirement 2026-11-25: audit, and keep them current).** Audit result 2026-11-25: `secrets_example.toml` is accurate. `config_example.toml` still documents `status_data_source_id` and the kill switch (obsolete). Its fallback `[recurring_tasks]` uses `enabled`, `definitions_db_id`, `tasks_db_id` only; the daemon fallback hard-codes the closed-date column and no inherited fields and reads only the global `non_completion_statuses`, while the Hub TOML (`_EXAMPLE_TOML`) already has `inherited_properties` and `non_completion_statuses` per pair. The fallback also still names things `id` and `*_db_id`, not `data_source_id`. README "Setting up the Configuration Hub" (about lines 129 and 162) still describes the status data source. To do in the same change as the code: update all of these to the final design, replace the old config.toml fallback format as described in "Stored config file and config.toml problems" below, and add tests that fail on drift: every key `parse_row_config` accepts appears in `_EXAMPLE_TOML`, and every column the code requires or optionally writes appears in the README column list. `tools/config.toml` and `tools/config_throw_away.toml` were not opened (they may hold credentials).

**Deleted row: recreate on every config read, with a limit (user requirement 2026-11-25; replaces "only at startup").** Each config read that finds the Automator's row missing creates a new row instead of waiting for a restart. Limit: at most 2 recreations in a row (default; a `config.toml` key as the handle). The counter lives in `config_hub_state.toml` (a restart loop counts too) and resets to zero when a read finds the row present. Recreation 1 writes the Warning with the old-page link. Recreation 2, the last allowed, writes an Error saying that automatic recreation stops after this one and debugging is needed (for example the integration cannot read rows, or something keeps deleting them). After that a missing row only logs an error until a read finds a row again.
**Confirm the row is really gone before recreating it (found in the final design review 2026-11-25).** "Not in the query results" does not mean "deleted": the row may have been moved to another data source, the integration may have lost access to it, or the query may be incomplete. Recreating in those cases leaves two Active rows with the same config, and duplicate detection then disables both RTD pairs (verified: `Both pairs disabled`, `hub.py` ~720). So when the saved row ID is missing from the query, fetch that page directly (`client.get_page`, exists in Notion_API): recreate only if Notion says it does not exist or `in_trash` is true. If the page still exists elsewhere, write a Warning with a link ("config row is no longer in the config data source; move it back") and do not recreate. The 2-in-a-row guard remains as the second safety net. Test with a fake client: query without the row plus a live page (no creation, Warning), plus a trashed page (creation), plus a 404 (creation).

**What the recreated row holds (user decision 2026-11-25).** Only the config TOML is backed up, not the whole page. The bot writes the verbatim text of the last successfully applied config body (comments, spacing and key order included; never rebuilt from the parsed config) to a local file at each successful apply, the single `config_automations.toml`, gitignored. The file also serves troubleshooting over SSH or remote desktop. The recreated page gets a short note above the code block saying it was reinstated from a backup, that only the config was copied, and to look at the prior page for anything else, with a link to that prior page (so the pointer survives if the Warning or Error is dismissed), then the backed-up text in a `toml` code block. The user copies the rest of the old page from Notion's own "copy page contents" menu. Status of the recreated row (user decision 2026-11-25): **Active** when it is restored from a backup, because a restored copy should restore the previous state and a silent stop would skip recurring-task governance; a blank example row, created when no backup exists (the config was never applied), stays inert as decided on 2026-11-14. The "Active" option is guaranteed to exist (schema validation requires it). The Warning says the row was restored and that On hold stops the automations while deleting the row does not. The repeat guard (2 recreations in a row) stays: it protects against a runaway loop of page creation whatever the Status is. Chain of recreations: the Warning or Error lists every earlier page of the chain, for example "2nd config row created in a row. Earlier rows: [original], [1st recreation]" with each as a link. The chain's page IDs are kept in `config_hub_state.toml` with the counter and cleared when a read finds the row present.

**Copy settings, final keys and defaults (user decisions 2026-11-25; supersede the key names and rules in the next paragraph).** Three keys in the `[recurring_tasks]` table, grouped by the shared `inherit_` prefix and ending in `_properties` like the existing `update_count_properties`: `inherit_include_properties` (a list of names, or the string `"all"`; replaces `inherited_properties`, safe to rename because the Hub is unreleased, and an old key in a test row shows as an unknown-key Warning), `inherit_exclude_properties` (a list of names) and `inherit_icon` (true or false, default true, which is today's behavior). Default when neither list is present: copy nothing, as today (a change to "all" would silently alter every existing RTD). A value that is neither a list nor `"all"`, or names not in the live schema, is a Warning and counts as nothing for those names (least-destructive: copying the wrong data silently is worse than copying less). The exclude list only applies with `"all"`; if `inherit_include_properties` has any value other than `"all"` (or is missing) and `inherit_exclude_properties` has a value, Warning: `inherit_exclude_properties has no effect unless inherit_include_properties = "all"`. Include-list names that cannot be copied (read-only property types, fixed bot-managed names) are a Warning, and the fixed set always wins.
**The keyword "all" versus a property named "all" (user decision 2026-11-25, replaces the Warning/Error rules first written for this).** The TOML type separates them, so there is no conflict and no schema check is needed. `inherit_include_properties` accepts either the plain string `"all"` (every copyable property) or an array of names; a list is required for names even when there is only one (`["Priority"]`). `inherit_exclude_properties` accepts only an array. A property that is literally named `all` is written as an array entry (`["all"]`) and is just a name. The parser already requires arrays of text for every list key and gives an Error for a bare string (verified in `hub._str_list`, 2026-11-25); only `inherit_include_properties` also accepts the exact string `"all"`, and any other bare string there is an Error that shows the array form. If the array entry `all` matches no property, the "not in the live schema" Warning adds a hint: `to copy every property use the string "all" without brackets`. Tests: `"all"` string accepted; `["all"]` is a name; `"Priority"` bare string rejected with the array hint; `inherit_exclude_properties = "all"` rejected.

**Recurring-task copy settings (proposed 2026-11-25; key names superseded above).** Today `inherited_properties` is an include list (empty = nothing), bot-managed fields are always excluded in code (`FIELDS_NOT_INHERITED`), and `inherit_icon` is planned (default true). Built (verified in `recurring_tasks._copy_task_fields`, 2026-11-25): the include list (`inherited_properties`, empty = nothing) and the fixed exclusion set (`FIELDS_NOT_INHERITED` plus the pair's closed-date and reopen-count columns, and read-only property types), which always beats the include list. Not built: a user-configurable exclude list, an "all" mode, and `inherit_icon` (the RTD page's icon is always copied, `recurring_tasks.py` ~1010). Proposed: `inherited_properties` accepts a list or the string `"all"`, a new `excluded_properties` list, and `inherit_icon` true or false. Rules (user decision 2026-11-25, no join logic): an explicit include list makes the exclude list pointless, so using both is a Warning ("excluded_properties has no effect when inherited_properties is a list"); no code is needed to merge them. The exclude list only does something with `inherited_properties = "all"`. A Warning also for any name in the include list that cannot be copied: a property of a read-only type (`_READONLY_PROP_TYPES`: formula, rollup, created_time, last_edited_time, created_by, last_edited_by, unique_id, verification, button, files) or one of the fixed bot-managed names (`FIELDS_NOT_INHERITED`). The fixed set always wins. All three settings go in the example file and the config row reference.

**Stored config file and config.toml problems (user decision 2026-11-25; the exact file key is to settle).** The old `[[databases]]` / `[recurring_tasks]` format in `config.toml` goes away, because keeping a second copy of the key set drifts. A fallback remains, in the SAME shape as a config row body (`[[property_automations]]`, `[recurring_tasks]`), read by the same `parse_row_config`, so one parser and one example (`docs/config_row_example.toml`) cover both. The bot's own verbatim backup file is exactly this shape, so it can serve as the stored config, and a user who does not want the Hub can write the file by hand. Migration (data-migration rule): the old format is still read for one release with a warning and a documented conversion. Without a Hub row there is nowhere in Notion to write a Report, Warnings or Errors, so in stored-config mode problems go to the log only; the Hub is how users get Report, Warnings and Errors. Problems with `config.toml` itself (`poll_interval`, `day_start_hour`, `week_start`, `notion_version` and so on) are reported on the Automator's config row when the Hub is available: details in Errors or Warnings under the source `config.toml`, and a Report line such as `🔴 config.toml: 2 problems, see Errors`. An unusable optional value falls back to its default with a Warning; a missing token or ID is fatal and logged, since nothing can be reported without them.

**What gets stored as the backup (user decision 2026-11-25, with my recommendation on warnings).** The text is stored only after a successful apply of a row that had no Errors; an apply with Errors never overwrites it. Warnings do not block storing it: a config that ran with a Warning is still what was running, and an older warning-free copy would make a restore silently drop the user's latest change.

**Naming (user decision 2026-11-25).** Underscores in directory and file names, because Python cannot import a hyphenated name and the repo already uses underscores (`config_hub_state.toml`, `bot_notes.py`, `__pycache__`). Exceptions are names fixed by tools (`install-service.ps1`, `notion-daemon.service`). Renamed: `notion_wiki_drafts/`, `docs/create_your_own_automations.md`.

**Config row example file and `/docs` (user requirement 2026-11-25).** A real example file for a config row's TOML is the single source: `docs/config_row_example.toml`. `hub.py` reads it when it creates a row (a minimal built-in string if the file is missing), tests check that every accepted key appears in it, and the docs link to it. Today `_EXAMPLE_TOML` exists only inside `hub.py`; the README does not hold it. `/docs` is committed and written for end users, so it needs the publication scan before any push and no internal notes. Planned pages: (1) initial setup, (2) upgrade steps (notes per version, plus the generic routine: stop the daemon, back up `config.toml`, `secrets.toml` and `config_hub_state.toml`, pull, read the changes, adjust, start), (3) create your own automations (first draft written 2026-11-25: `docs/create_your_own_automations.md`, with the Hub messages section marked Planned), (4) config row reference (every TOML key). `wiki-drafts/` (gitignored) was renamed to `notion_wiki_drafts/` on 2026-11-25 (user decision) so it is clear it holds Notion-workspace Wiki pages and not this project's docs; `.gitignore` and the references in this file were updated.

**Code touched.** `hub.py` (remove status data source code, add optional columns, per-row parsed config kept in memory, version and edit checks, Report lines), `daemon.py` (activation, deactivation, apply paths), `config_example.toml`, README "Setting up the Configuration Hub", `tests/test_hub_*.py`. Template: add Report, Config Version, Apply Config.

**Data source IDs everywhere (user decision 2026-11-25, checked against Notion's own sources; implemented 2026-11-25 as build step 0: `recurring_tasks.page_data_source_id`, `*_data_source_id` names, a database ID in the config resolved to its data source with a warning, test `test_page_in_second_data_source_of_same_database_gets_no_flags`).** **GitHub Issue:** #9. A database is a container of one or more data sources (`spec-verified`: the database object has a `data_sources` array of up to 100 references; `docs-verified`, raw `upgrade-guide-2025-09-03.md`: Retrieve Database now returns that list, the `/v1/data_sources` endpoints take a data source ID and not a database ID, and on 2022-06-28 retrieving a database with several data sources fails with `validation_error`). A page's parent on 2026-03-11 carries BOTH `data_source_id` and `database_id` (`spec-verified`, `dataSourceParentResponse`), so pages in two data sources of one database share the `database_id` and differ in `data_source_id`. That is consistent with what the Notion developer footer shows when views are switched: a view is a window on a data source, and only the data source ID changes. Today the code identifies a page's database by `parent.database_id` (`automations.py` lines 57 and 62, `daemon.py` lines 158 and 170), keys snapshots and `last_polled` by it, and finds per-database flags with it, so two data sources in one database would share flags and snapshots. Work: one shared helper that returns a page's data source ID (parent `data_source_id`, falling back to `database_id` only on API versions that lack it), used at those four places and for the snapshot, `last_polled` and flag keys; rename the internal `*_db_id`, `database_id` and `database_ids` names to `*_data_source_id`; replace `get_database` (title and icon) with the data source retrieve; the old `config.toml` `[[databases]]` block that still says `id` is removed anyway. Test: a fake page in a second data source of the same database must not pick up the first one's flags.

#### Draft TOML shapes (for review, 2026-11-14)

Derived from what `hub.py` `read_config` consumes today, so behavior stays the same. Key names use "property" (the Hub's Notion terminology) and "data_source_id" (Notion's API spelling, user decision 2026-11-14); the code keeps its internal `_field` and `_db_id` names and maps them. Every example below was parsed with `tomllib`. Where to find the IDs (user-reported 2026-11-14): in the Notion desktop app, open the data source, then three-dots (top right) > More > Developer; it shows Copy database ID and Copy data source ID, and a Developer bar toggle keeps the IDs visible. The row's explanatory text should say this.

**No tool-name header.** The code block is already inside the Automator's own config page, so the TOML does not repeat the tool name: the tables are top-level. Unknown top-level tables produce a Warning. A row is only a container: it may hold either section below, or both. How a row added by a user is attributed to a tool is deferred until a second tool actually uses the config database; an Automator row with no marker keeps working if a marker is added for other tools later.

**Section 1: property automations** (replaces "Automated Databases" rows). One `[[automator.property_automations]]` block per data source:
```toml
# One [[property_automations]] block per data source.
# To switch one off, put a # in front of every line of the block.
[[property_automations]]
name = "tasks"
data_source_id = "0123456789abcdef0123456789abcdef"
closed_date_property = "Closed Date"
reopen_count_property = "Reopen Count"
update_count_properties = ["Due Date"]
first_value_properties = ["Due Date", "Status"]

[[property_automations]]
name = "shopping"
data_source_id = "0123456789abcdef0123456789abcdef"
closed_date_property = "Purchase Date"
```

**Section 2: recurring tasks** (replaces "RTD Config" rows; one `[[recurring_tasks]]` block per pair, user decision 2026-11-27; a data source is in one block only, as definitions or tasks, never both):
```toml
[[recurring_tasks]]
definitions_data_source_id = "0123456789abcdef0123456789abcdef"
tasks_data_source_id = "0123456789abcdef0123456789abcdef"
inherited_properties = ["Priority", "Effort", "Area"]
inherit_icon = true
non_completion_statuses = ["Cancelled", "Skipped"]
```

**Switching things on and off (no `enabled` keys).** The row's Status column switches the whole row: Not started, Active, On hold, Discontinued (the options already defined in `hub.py` as `_STATUS_OPTIONS_FULL`; only Active rows are applied, as today). To switch off one block inside a row, comment it out with `#`. The Hub Status database keeps its own Inactive/Active options (`_STATUS_OPTIONS_HUB`).

**Rules (carried over from today unless marked new)**
- Missing key = feature disabled (`closed_date_property`, `reopen_count_property`, the two lists). `inherited_properties` empty = inherit nothing. `non_completion_statuses` empty = built-in defaults.
- **`inherit_icon` (new): default true**, which is today's behavior (the RTD page's icon is copied to each created task). `false` creates tasks without it.
- IDs may contain dashes; they are normalized and checked as today. A malformed or missing ID in an Active row is an Error and that block is skipped.
- `reopen_count_property` without `closed_date_property` is an Error in an Active row (a Warning otherwise), as today.
- A recurring pair gets its `closed_date_property` from the property automation with the same data source ID (in any row), defaulting to "Closed Date", as today.
- Duplicate active recurring blocks (same tasks or definitions data source): both disabled with an Error, as today.
- **Lists are TOML arrays (new).** This removes the comma-in-a-name problem, so the earlier "double-quoted names in CSV" rule is no longer needed.
- `update_count_properties` and `first_value_properties` are arrays only for the first build; the counter and first-value property names follow the existing convention (`[Property] Update Count`, `First [Property]`). A table form such as `update_count_properties = { "Due Date" = "Reschedule Count" }` is reserved for later: it is told apart from the array by type, so adding it changes no existing row. Note `recurring_tasks.py` resets `First Due Date` and `Due Date Update Count` by literal name (~1201-1202, ~1257-1258), which would have to read the mapping at that point.
- Property names are matched case-insensitively against the live schema and resolved to property IDs (see the property-ID rule above). A name with no match is a Warning (today this check exists only for inherited properties; extending it is new).
- **Errors go to the row's Errors column instead of only the log (new).** Invalid TOML: Error with the parser's line and column (for example `Invalid value (at line 3, column 15)`; verified with `tomllib`), row skipped. No code block, or more than one code block: Error. Messages for a property automation name its `name`. Warnings and Errors are cleared when resolved.

**Row body when the bot creates a row (new).** Created without a Status (the data source's options vary: the first test Hub had only Inactive and Active), so the default applies and it is inert. A short explanation above the code block, links to Wiki articles, and a `toml` code block containing the full shape with empty values and comments explaining each key (a commented-out example block can be included). The bot never edits the code block afterwards.

**Choices**
- Health formula column (🆘 / ⚠️ from Errors and Warnings): kept as it is today.
- Optional per-pair `properties` mapping for the RTD and task property names (item 4: Series relation, Quota N, and so on) is not part of the first build. It can be added later as a new optional key without changing existing rows.

### Decisions made so far
- Auto-detection over activation buttons — daemon detects what exists and creates what is missing; no explicit "activate" step.
- User creates the Hub page manually and adds its ID to `config.toml` (one-time step). Bot creates the 3 child databases automatically on first run.
- Notion page IDs are permanent — users can move the Hub page anywhere after initial creation.
- Preference: automatic setup with a clear startup log of what was created.
- **Single hub for all automations** — not a page-per-database. All task database configs and recurring task config live here together, mirroring the structure of `config.toml`.
- **Optional — graceful degradation (resolved 2026-11-14):** if no Hub key is set in `config.toml` (`automation_hub_page_id` today; `config_hub_data_source_id` after the redesign), Hub features are off and the daemon reads the `config.toml` `[[databases]]` / `[recurring_tasks]` fallback; it stops with an error if that has no databases either. This replaces the earlier wording "No config.toml fallback for behavioral settings", which contradicted the code (`daemon.py`), README, DESIGN.md ("developer escape hatch") and the fallback section below. The Aug 13 2026 session history also records "remove the `[[databases]]` fallback when promoting this feature": so the fallback stays until the Hub is promoted, then goes (or DESIGN.md's escape-hatch decision is kept instead; to be settled at promotion). Every Hub-writing code path must check Hub availability before writing.
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
  - `config.toml` — user-managed; token, notion_version, the Hub keys (`automation_hub_page_id` today; `config_hub_data_source_id` and `status_data_source_id` after the redesign), poll_interval, day_start_hour, week_start, and the fallback `[[databases]]` / `[recurring_tasks]`. `config_example.toml` documents every key.
  - `config_hub_state.toml` — bot-written state, never edited by the user: today the 3 Hub child database IDs discovered or created at setup; after the redesign the config page IDs and the property-ID table. `config.toml` is the starting point, `config_hub_state.toml` is what the program learns from it.
  - `config_hub_backup.toml` — bot-written; mirror of Hub behavioral config written at each governance pass. Human-readable TOML for reference and manual restore. No auto-restore logic.
- **Hub backup scope:** behavioral config only (automation flags, inheritance fields, global settings). Not health stats or error logs. Written at each governance pass (daily + startup).
- **Daemon version:** bot writes its `VERSION` constant to Hub Status on each governance pass. GitHub repo link added as a static block during Hub page setup.
- **Machine and daemon health stats:** last poll time, last governance run time + trigger (`server-reset` / `scheduled` / `force-governance`), daemon uptime (computed from recorded start time), machine uptime (read from `/proc/uptime`), CPU temperature (read from `/sys/class/thermal/thermal_zone0/temp` on Linux/Pi — skipped gracefully if unavailable). CPU% and memory usage deferred (requires `psutil` dependency).
- **Nexus summary** (user-built, not bot-managed) — a user-maintained Notion page with formula fields pulling health indicators from the Hub. Not a bot concern for now.

### Configuration fallback and developer extensibility

The Hub is the preferred configuration surface for stable features and non-technical users, but it does not replace `config.toml`. The configuration file remains required for workspace identity and operational settings such as the token, database IDs, Hub page ID, polling interval, week start, and day-start hour. It also remains an approved fallback for developer-owned, experimental, or ad hoc automation settings while an automation is being designed.

The extension path is intentionally incremental:

1. Add the implementation to the appropriate code registry (`AUTOMATIONS` or `GOVERNANCE`), with safe defaults and validation.
2. Keep experimental settings in a documented `config.toml` section while the behavior is changing.
3. When the behavior is stable and intended for general users, add typed Hub fields, schema validation, health/error reporting, and an explicit precedence rule.

Hub-backed values take precedence for features that have a Hub schema. Features without Hub integration continue to use their documented `config.toml` settings. We should not require developers to design a Hub database row or opaque JSON format before they understand the shape of their automation.

### Cross-datasource relationship automations

**Status:** Pre-design

Cross-datasource rules are a future capability separate from the current Task Databases and RTD Config rows. Candidate rules include:

- task moves into the Complete group → find linked open Timebox sessions and stamp a close date/time;
- parent task moves into the Complete group → optionally close linked child tasks.

The leading design is a dedicated **Relationship Automations** data source, with one row per rule. A first typed schema could include:

| Field | Purpose |
|---|---|
| Status | Enable/disable the rule safely |
| Source database/property | Page and transition that trigger the rule |
| Relationship path | Relation(s) used to find target pages |
| Target database/filter | Which related pages qualify; avoid implicit “all related pages” behavior |
| Action | A named library action such as stamp date/time, set status, or add Bot Note |
| Propagation policy | Explicit choice such as no child change, mirror state, or set a named target state |
| Errors/Warnings | Bot-managed validation and runtime health |

Do not start with arbitrary JSON/TOML interpreted as unrestricted write instructions. Use typed, validated triggers, selectors, and actions first; isolate any developer escape hatch from the stable Hub schema. The design still needs decisions on previous-state detection, cycles/re-trigger prevention, multi-hop relationships, filters/sorting, batching, permission failures, retries, and whether consequential child changes require confirmation or Bot Notes.

Notion's own trigger → action model is a useful reference, but this project should not attempt to clone its UI or internals. Notion currently supports editing pages in another database and variables derived from the trigger page, while also prohibiting one database automation from triggering another. Those constraints help define where daemon-level cross-datasource governance adds value. Reference: [Notion database automations](https://www.notion.com/help/database-automations).

### Hub data-source organization

Notion supports multiple data sources within one database and can expose views from different sources as tabs. This means the Hub can present several logical tables together, but it does not mean their schemas should be merged. Keep Task Databases, RTD Config, Hub Status, and future Relationship Automations as separate data sources. Use tabs, linked views, or nested pages for presentation, while making source ownership and deletion/recovery behavior explicit because hidden sources are easy to damage accidentally.

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

| Column              | Type      | Notes                                                                                    |
| ---------------------| -----------| ------------------------------------------------------------------------------------------|
| Name                | Title     | Static: "Notion Automator".                                                              |
| Version             | Text      | Bot-written: daemon `VERSION` constant. Updated each governance pass.                    |
| Last Poll           | Date      | Timestamp of the most recent successful poll cycle.                                      |
| Last Governance Run | Date      | Timestamp of the most recent governance pass.                                            |
| Governance Trigger  | Select    | What triggered the last governance run: `server-reset`, `scheduled`, `force-governance`. |
| Daemon Uptime       | Text      | Human-readable elapsed time since daemon start (e.g. "3d 4h 12m").                       |
| Machine Uptime      | Text      | Read from `/proc/uptime` on Linux/Pi. Skipped gracefully if unavailable.                 |
| CPU Temperature     | Text      | Read from `/sys/class/thermal/thermal_zone0/temp` on Linux/Pi. Skipped if unavailable.   |
| Errors              | Rich Text | Bot-written. Cleared when resolved.                                                      |
| Warnings            | Rich Text | Bot-written. Cleared when resolved.                                                      |

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
- `config_hub_state.toml` read/write logic — daemon reads child database IDs from this file at startup; writes them after Hub setup or first-run database creation. Avoids block scanning on subsequent startups.
- `config_hub_backup.toml` write logic — written at each governance pass with a TOML mirror of the Hub's behavioral config (automation flags, inherited fields, global settings, database IDs). Human-readable for reference and manual restore. No auto-restore.
- Hub poll-cycle change detection — daemon snapshots relevant Hub database row fields each poll and triggers a config reload (and governance if appropriate) when they change. Mirrors the RTD change-detection pattern.
- Bot must read Hub config at startup (after `config.toml`) and apply to running config.
- Bot must write errors/warnings per-database-row and per-section rather than only to logs. Write timing: startup (validation errors) and governance pass (governance errors + health stats).
- Hub availability check: every Hub-writing path must check that Hub is configured and reachable. If not: log-only, no write attempted.

### Config layer ownership (settled)
- `config.toml` = workspace identity and connectivity (token, DB IDs, Hub page ID, poll interval). Requires restart to change. Not for behavioral decisions.
- Configuration Hub = behavioral and schema-aware config that users adjust without touching files or restarting. Examples: automation flags per database, inheritance mode + field list, First Value Field config, webhook URLs.
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
- Log at INFO: `"Force governance triggered by user via Configuration Hub."`

### Open questions
- None currently open — all major design questions resolved in the August 2026 design session.

### Dependencies
- Configuration Hub must exist before Notifications can store webhook URLs there.
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

## Feature: Task Continuation and Repeat Relationship Types

**Status:** In design. Nothing below is agreed yet except where marked "user decided". Raised 2026-10-19 from the iOS_Shortcuts session (its Create Next Task shortcut needs the final Type names).

**Goal:** two new `Type` options in the existing Task Relationships data source, separating CONTINUE (log partial work, more remains) from DO AGAIN (e.g. therapy: know the last one). Primary = earlier task, Secondary = later task. Both currently get tagged `Previous/Next`, which the wiki (`notion_wiki_drafts/field_reference.md`) defines as a third thing: soft ordering ("complete before, without a strict blocking dependency").

**User decided:** new Types go in the existing data source, not a new database. Therapy stays out of RTDs (wants a specific page template, no auto-cancel, no due date surprises).

### Open decisions (ask at next session)
1. **Type wording.** Single words (`Continuation`, `Repeat`) vs slash pairs like the existing three, where the first name is the Primary role (`Prior Part/Next Part`, `Last Time/Next Time`). Recommendation: pairs, because direction confusion is the stated pain. Whatever is chosen, the iOS shortcut must send the exact string.
2. **Children of a continued task.** (1) leave them on the part they were created under and show them chain-wide via a read-only formula on the root; (2) copy open children's Parent/Child rows forward (child gets two parents); (3) move open children to the new part. Recommendation: start with (1).
3. **RTD "Repeat" toggle.** Recommendation: no toggle. DESIGN.md Decisions [Q4] already rejected a previous-instance field in favor of a sorted view of `Recurring Series`. The RTD `Last Completed` rollup gives last-done date in one hop; an optional RTD formula returning the last closed task as a link could give the page link (untested whether a task can read a page-link formula from the RTD).
4. **`Previous/Next`:** keep it as soft ordering and do not rename. Retag only the rows that are really Continue or Repeat.
5. **`Related Tasks` formula text** is needed from the user. It is not in any repo (`notion_wiki_drafts` only has `Relationship Tasks`, `Primary Summary`, `Secondary Summary`, `Status Icons`).

### Recommended structure (pending the decisions above)
- **Latest task in a chain:** a dual relation on Tasks, `Chain Root` (reverse `Chain Parts`), copied forward by the creator (button or shortcut): new.root = prev.root or prev. A formula on the root picks the latest part's Status; every part reads it from the root in one hop. Walking the relationship rows is not possible (formulas cannot reference themselves, so depth would be fixed).
- **Parent:** copy the previous task's Parent/Child row(s) to the new part. `Status Icons` 🌿 and parent lists run off the rows, so deriving the parent through the root would break them.
- **Formulas:** `Primary Summary` / `Secondary Summary` need `ifs` cases for the new Types (otherwise they fall back to 🔗). `Status Icons` and `Relationship Tasks` need no change. Avoid 🔁 for Repeat (Status Icons uses it for recurring series).
- **Where it lives:** Notion plus the button and iOS shortcut for v1. Automator does not watch the Relationship DB today (only reference is `FIELDS_NOT_INHERITED`). A later governance repair pass (backfill Chain Root / parent when a row is made by hand) fits the Linked Page Automations track.
- **One Automator change needed with Chain Root:** add `Chain Root` and `Chain Parts` to `FIELDS_NOT_INHERITED` (`recurring_tasks.py` ~204), otherwise the config.toml fallback path (copies everything not excluded) would carry the old root onto an RTD's next task. Not yet checked whether the Hub whitelist path is exposed.

### Migration (data-migration rule)
1. Backup script in `tools/`: dump full schema including formula `expression` (the Notion API returns it) plus all `Previous/Next` rows.
2. Export CSV with a proposed Type per row (hint: same Recurring Series or same Name suggests Repeat), both task names and dates, for the user to edit.
3. Apply script retags from the reviewed CSV and writes a revert CSV. Precedent: `tools/migrate_relationships.py`, `tools/revert_from_backup.py`.
4. Verify by counting rows per Type. Then test every button and view that sets or filters Type (button/view internals unchecked).
5. Docs: `field_reference.md` Type row, `usage_guide_task_tracker.md` line 13.

### Verified vs not
- Verified: Automator applies only the DB Default template (`template={"type": "default"}`, `recurring_tasks.py` 1018/1024); Notion docs say create-page also accepts `template_id` (applied asynchronously, `children` not allowed with a template). RTD `Do Not Autoclose` suppresses grace-period cancel (wiki doc, not tested for therapy). No code found that moves a user-edited due date (grep only, not a full review).
- Not verified: `Related Tasks` formula; whether buttons reference select options by name or id; property description length limit (docs state none); the one-hop formula limit (user-reported, untested); page-link formula read from RTD.

---

## Improvement: Migrate to Notion data sources (API 2025-09-03+)

**GitHub Issue:** #9 (database ID versus data source ID matching; see "Data source IDs everywhere" under Configuration Hub).

Added 2026-10-14 from the iOS_Shortcuts session. **Started 2026-11-14 (user priority: use the newest API version).** References: Notion upgrade guides [2025-09-03](https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03) and [2026-03-11](https://developers.notion.com/guides/get-started/upgrade-guide-2026-03-11).

**Done 2026-11-14:** pin bumped to Notion_API v1.4.0; daemon passes `notion_version` (config.toml key, default `2026-03-11`, validated by `hub.resolve_notion_version`); `automations._get_db_schema`, `recurring_tasks._load_task_db_schema`, `recurring_tasks._get_status_group` and `hub.validate_hub_schema` now call `get_schema()`; tests in `tests/test_api_version_compat.py`.

**Audit against the two upgrade guides (2026-11-14):**
- 2026-03-11 breaking changes: `after` to `position` in Append block children (not used; `append_blocks` is never called with `after`), `archived` to `in_trash` (the client picks the field by version; `hub.py` reads both), `transcription` to `meeting_notes` (not used). None affects this code.
- 2025-09-03: Search API (not used); create page with a database parent (the client sends a `data_source_id` parent on new versions); database read/query/update (the client routes through `get_schema` / `query_data_source` and resolves a database ID to its single data source); relation properties return both `database_id` and `data_source_id` on every version (`tools/describe_database.py` now shows both).
- Page parents keep `database_id` next to `data_source_id` on 2026-03-11 (spec-verified: `dataSourceParentResponse`), so the 7 `parent.database_id` reads still match the database IDs in config. They must change to `data_source_id` when config moves to `data_source_id` (Configuration Hub TOML).
- The guide warns that on an older API version, adding a second data source to a database breaks create-page, database queries and relation writes. That is the reason to stay on the new version.

**Still open:** a live run of the Hub's own database schema on the new version (the client's `create_database` payload is already live-verified on all three versions by Notion_API's `tests/live_test.py`, 2026-10-15); `get_database` is still used for the title and icon of a database (valid on new versions), but it will fail if config holds a data source ID, so the Hub reader rewrite must resolve IDs first; snapshot keys and `last_polled` are still keyed by database ID.

*Original notes (2026-10-14), kept for the open items:*

**Why:** Notion_API v1.4.0 (branch `feature/data-sources`, uncommitted/untagged at the time of writing) lets `NotionClient(notion_version="2026-03-11")` accept database IDs or data source IDs. The default stays `2022-06-28`, so nothing here breaks until we opt in. Notion has published no sunset for `2022-06-28` (its FAQ says no retirement process exists; a future minimum-version program would give ~6 months notice and start with older versions). The practical risk: once any watched database gains a second data source, legacy database-ID calls on it fail.

**Where this project touches the difference (check each when migrating):**
- `page["parent"]["database_id"]` reads in `automations.py` (~lines 57, 62) and `daemon.py` (~148, 160): on newer versions page parents are data source parents (`parent.type == "data_source_id"`); confirm whether `database_id` is still present before relying on it, and prefer `data_source_id` matching.
- `client.get_database(id)["properties"]` in `automations.py` and `hub.py` (schema reads): use `client.get_schema(id)`.
- `hub.py` Hub creation/lookup: `create_database` / `patch_database` payloads changed on newer versions (unit-tested in Notion_API, not yet verified live), and child_database block IDs are database IDs.
- `config.toml` / `config_hub_state.toml` store database IDs; both ID kinds will work via `resolve_data_source_id`, which costs one extra call per ID per run (cached).
- Snapshot keys / `last_polled` are keyed by database ID: decide whether to key by data source ID.

**Suggested path:** bump `requirements.txt` to the v1.4.0 tag (default version, no behavior change) and run the tests; then switch the client to `notion_version="2026-03-11"` on the test workspace and run the daemon against the test databases before prod (Pi 2B).

---

## Feature: Pursuit Hierarchy and Habit-to-Pursuit Link

**Status:** In design (2026-11-05). Notion schema changes are manual; the Automator part is optional and later.
**One-liner:** Let a Pursuit have one or more parent Pursuits, and let an RTD point at a Pursuit, so work can be tagged at whatever level of detail the day allows.

### Why
The user wants flexibility as an ADHD accommodation: on a low-energy day tag the abstract Pursuit (e.g. Drawing), on a high-energy day tag the specific project (e.g. a convention art project). A generic "Drawing" Pursuit sitting beside real projects mixes a habit-building goal with finite projects. A parent relation separates them. A Pursuit that stays in Notion while its notes move to Obsidian (Game Development) is fine: Pursuits are task containers, notes are a different layer.

### Decisions made so far
- Add a `Parent Pursuit` self-relation on the Pursuits database (Notion creates the reverse "Sub-pursuits"). A Pursuit can have several parents (e.g. Virtual Pet has parents Drawing and Game Dev).
- Tag a task with the most specific Pursuit that fits. More than one Pursuit on a task is allowed (already true in the data: the `Pursuit` relation holds multiple values) but is the exception, not the rule.
- No rollups or ancestor formulas in Notion. All ancestor/descendant logic lives in Notion_Analytics.
- Add a `Pursuit` relation on the RTD database (the synced RTD columns have no Pursuit or Area link today). Reuse the existing pattern of "Game Dev (hub)" as a Habit tied to a Pursuit.
- Areas are not part of this. They are being deprecated; do not build on them.

### Crediting rule for reports (Analytics side)
**Deliberately vague.** The user has no concrete reporting need yet (2026-11-05), so the rule is a direction, not a spec. What is known:
- The right answer depends on what is being reported on. Reporting per Project means Pursuits of Type = Project; a parent Project should be able to see all its child Projects' work (or let the report filter on that).
- Journeys (the abstract parents like Drawing, Game Dev) follow the earlier rule: if a task links a Project that already has a given Journey as a parent, do not separately credit that Journey from other tags on the task. If a linked Project does not have a Journey the task is also tagged with, the open choice is to pull in the Project's other Journeys or none.
- That last choice (pull in the other Journeys, or none) should be configurable, most likely a Config Hub setting, not hard-coded. Do not detail the setting until there is a real report that needs it.
- A task counts once per Pursuit it is credited to.
- Guard against cycles (A parent of B, B parent of A). Notion will not prevent them.

### Open questions
- Exact crediting rule (see above): pull in a Project's other Journeys, or none; where the setting lives. Not needed until a report requires it.
- Should the Automator copy the RTD's Pursuit onto each new recurring task, or is relying on field inheritance (`Inherited Fields` in Hub Section 2) enough? The RTD link is only needed if the first task of a series should be tagged automatically.
- Does the explicit-parent rule need a depth limit?

### Cost and undo
- Doing it: two new Notion properties, a rule in Analytics, optionally one Automator feature.
- Not doing it: parent and child work is never reported together; Area is the only grouping.
- Changing later: easy; removing the relations loses no task data.

### Dependencies
- Notion_Analytics: a task-to-pursuit link table plus parent edges, and a model for the crediting rule (see Notion_Analytics PLANNED/dbt work; stub not yet added there).
- Automator copy-from-RTD (optional) depends on the RTD `Pursuit` relation existing and on the Hub Inherited Fields feature.

---

## Design Change Requests (2026-11-13)

**Status:** Recorded, not implemented. Each item lists its cost to change or undo. The Configuration Hub direction is in its own subsection under "Configuration Hub".

### 1. Habit overdue handling (user rule)
Habits never autoclose. Grace Period (days) is extended to Habits as a roll-forward delay, not a cancel:
- While now is on or before Due Date end + Grace Period, the task is left untouched and shows as overdue.
- Once past that, Due Date is set to the current period, Due Date Update Count is reset to 0 and First Due Date is cleared (the existing roll-forward at `recurring_tasks.py` ~1255-1263).
- Blank or 0 Grace Period keeps today's behavior: roll at the period boundary. No new field.

Example, "read before bed", Day period, Grace Period 1: due Mon. Tue 3am: still inside the grace day, left untouched and overdue. Wed 3am: past due plus grace, Due Date becomes Wed and the update count is 0.

Multiple open Habit tasks are acceptable. They can arise two ways: 1. the user manually created extras, or 2. the user lowered Grace Period so an old overdue task is now past grace and rolls to today, where a task for today may already exist. Neither changes the design.

Cost to change: low (one branch in the Habit roll-forward; no schema change).

### 2. Rename "N Cadence" to "Quota N"
Accept both names during a transition (same pattern as the legacy Cadence Type names at `recurring_tasks.py` ~831 and ~1128). Touches 4 code sites, `tests/helpers.py`, the README and the Notion template. With the property-ID table (Configuration Hub) the rename stops mattering to the code. Cost to undo: low.

### 3. Emoji or acronym in "Recurring Series"
No code decision needed: the property-ID table means the name is cosmetic.

### 4. Hardcoded property names configurable
`recurring_tasks.py` hardcodes about 16 property names (Recurring Series, Quota N, Cadence Type, Period, Type, Anchor Day/Time, Grace Period (days), Do Not Autoclose, the "(Recurring Task)" fields, Due Date Update Count, First Due Date). Replace with one place in code holding `logical name | property ID | last-known property name`, filled by the bot from the names the user types in config (see the redesign subsection). The `[Property] Update Count` and `First [Property]` naming convention in `automations.py` is separate and unchanged for now. Cost: medium to build, low to extend, and doing it before Hub Slice 5 means change detection can watch logical names.

### 5. Cross-database automations (all become rows in the config database)
- 5.1 Application page status set to Applied (first time) closes today's "Apply to Job" RTD task. Already designed: "Feature: Linked Page Automations".
- 5.2 Closing a task closes its open Timebox session. Already designed there.
- 5.3 Creating a Review page in the Goals database closes the open weekly-review RTD task. New. Trigger type: page created.
- 5.4 Completing a Media Review closes the linked task. New. Idea: a "watch and review" button on the media page creates the "Watch [media]" task, an empty review page, and a "Review [media]" task, and links them with a relation. Notion's button property may be able to do the creation without the daemon (assumed, not checked). The daemon's part is closing the linked task when the review is completed (relation path) and a data-quality rule for orphaned review pages: write a Bot Note and flag the page, never delete (inform before archive before delete).

Trigger vocabulary to record now: property set (first time), status group entered, page created, relation path to a target. Cost to defer the build: low (rows are additive). Cost of getting the vocabulary wrong once rows exist: medium.

### 6. Data-quality fixes counted as updates
Example: fixing Due Dates bumps Due Date Update Count. Fix: pause the automation for that database (Status "On hold", or empty the Update Count Properties list in its config). On resume, reset that database's entry in `last_polled` (`daemon.py` ~550, one pointer per database) AND rebuild its snapshot; resetting the pointer alone would leave stale previous values, so the next real edit would count once (inference from the code; test it). Depends on Hub change detection (Slice 5) to be quick. Cost: low.

### 7. Icon inheritance for RTD tasks
Closed. The code only drops uploaded-file icons (`recurring_tasks.py` ~1010-1012), which the Notion API cannot write. The user tested a reusable page emoji (custom emoji) on newly created RTD tasks and it works.

### Bug / design decision: Grace Period "not working" for Evening Routine
Not a bug in the "one open task" rule. Evening Routine is a Habit (inference, to confirm), and Grace Period applied only to Responsibilities, while Habits were rolled forward immediately. Resolved by item 1.

### Wiki
The user made the Wiki non-public (no longer shared with the Template) for better versioning. Check README and template links for stale Wiki references.

---

## Documentation and Wiki Plan (2026-11-14)

**Status:** Direction set by the user: documentation goes in a GitHub Wiki, not the README. Not started.

- **Existing drafts to build from (do not start over):** `notion_wiki_drafts/` (gitignored) already holds `welcome.md`, `automations.md`, `field_reference.md`, `ios_shortcuts.md`, `usage_guide_areas.md`, `usage_guide_pursuits.md`, `usage_guide_recurring_tasks.md` and `usage_guide_task_tracker.md`. Inventory them by reading before writing any new page.
- **Pages still missing (proposed):** `config.toml` reference (every key, from `config_example.toml`); Configuration Hub setup and the config page TOML reference (with commented examples, error and warning meanings); the status page (columns, kill switch, Force Governance, Health icons); recurring task fields including Quota N and Habit Grace Period; troubleshooting; adding an automation (developers).
- **How the GitHub Wiki works (docs-verified 2026-11-14, GitHub docs pages `adding-or-editing-wiki-pages` and `about-wikis`, read through a summarizing fetch):** a wiki is a separate git repository, cloned from `https://github.com/DreamShark-Bytes/Notion_Automator.wiki.git`. The first page must be created once in the GitHub web UI before the wiki can be cloned. Pages are Markdown files; only the default branch goes live. On a public repository the wiki is public, and by default only people with write access can edit it. Soft limit of 5,000 files.
- **Who does what:** Claude writes and edits the Markdown locally in `notion_wiki_drafts/`. Publishing means cloning and pushing the wiki repository, which connects to GitHub: it is done only when the user asks, with the credential, PII and internal-notes scan from the global publication rules first (no STATUS.md, scratch.md, CLAUDE.md or session history content). A wiki's separate history makes it a natural sanitized export.
- **Order:** finish the Configuration Hub design and code first, then write the Hub pages from the working behavior; the drafts that already match shipped behavior can move earlier.

---

## Undeveloped Ideas

_Features with no active design work. Full history in git. Promote to a full section when actively designing._

- Extended Cadence (Every Y Periods) — support recurring cadences spanning multiple periods (bi-weekly, quarterly, etc.); simplified picker + Custom X-per-Y mode.
- Notifications — outbound webhook support (Discord, Telegram) for governance events; URLs stored in Configuration Hub.
- Cumulative Overdue Time Tracking — bot-managed `Overdue Days` field incremented each governance pass the task is open and past-due.
- RTD Current Period Field — bot-written `Current Period` date range (start+end) on RTD; enables formula/rollup period filtering and Siri Shortcuts lookup.
- Timer / Mission Tracking — link closed tasks to mission areas; surface per-period effort heatmap. Scope unclear (Automator vs Analytics).
- Companion: Siri Shortcuts Integration — iOS shortcuts to close current period's open task for an RTD via voice; depends on RTD Current Period field.
- iOS Reminders Integration — use iOS Reminders (CalDAV) as reminder layer; creates reminder on due date set, clears on close.
- Range Cadence (Min N, Max M) — new Cadence Type enforcing both a minimum and a maximum per period.
- Task Templates — apply a Notion template to bot-created recurring tasks for user-configured buttons/content.
- Hub-unavailable error page (backlog, user decision 2026-11-25) — when the Hub cannot be read but Notion is reachable, create and maintain one page titled like "Automator Error: Config Hub not found" with details in the body, and save its page ID in `config_hub_state.toml`. Notes from the 2026-11-25 discussion: it cannot cover network or token failures (no write is possible); the page needs a parent, either a page ID in `config.toml` (recommended: nothing watches it) or a row in a task database (visible daily, but the bot's own automations would see it, and no task database is known when the Hub is unreadable); archive it when the Hub reads again; whether a locked body blocks the update is unverified.
- Config staleness / "Last Use" check (backlog, user notes 2026-11-25) — a bot-written `Last Use` date on each config row plus a user-built Notion formula (using `let()`) that flags a row as old, with a cutoff per config chosen by page name or ID (for example Automator 15 minutes, Analytics 2 hours, Fuzzy Match no check). Critical input recorded: a 15-minute cutoff needs the bot to write `Last Use` at least every few minutes, which the Hub design avoids by default (governance-only writes, per-poll writes only with the verbose flag), so this needs a heartbeat setting; also unverified how often a formula using `now()` recalculates, so test it before relying on a short cutoff.
