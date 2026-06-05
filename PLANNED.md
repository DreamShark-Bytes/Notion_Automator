# Notion Automator — Planned Features

Living design document. Sections are deleted when a feature is implemented and its decisions have moved to `DESIGN.md`. See `STATUS.md` for one-line summaries and current priority.

---

## Contents

- [Extended Cadence (Every Y Periods)](#feature-extended-cadence-every-y-periods)f
- [Governance Schema Validation](#improvement-governance-schema-validation)
- [Schema-Check Safety Net in automations.py](#improvement-schema-check-safety-net-in-automationspy)
- [RTD Optional Fields — Default Handling](#bug-rtd-optional-fields--default-handling-for-emptyunknown-values)
- [Minimum N — Carry-Over Instead of Archive](#improvement-minimum-n-per-period--carry-over-instead-of-archive)
- [Rename "At most N" → "Maximum N"](#migration-rename-at-most-n-per-period--maximum-n-per-period)
- [Configurable Field Inheritance](#feature-configurable-field-inheritance-for-recurring-tasks)
- [Icon Inheritance from RTD](#feature-icon-inheritance-from-rtd)
- [Automation Hub](#automation-hub-formerly-project-page)
- [Notifications](#notifications)
- [Clear Blocking/Blocked-By on Close](#clear-blockingblocked-by-on-close)
- [First Value Field Tracking](#first-value-field-tracking)
- [Field Update Count (Abstract)](#improvement-field-update-count-abstract)
- [RTD Display Fields (Current Period)](#improvement-rtd-display-fields-current-period)
- [Timer / Mission Tracking](#timer--mission-tracking)
- [Automated Testing](#automated-testing)
- [Bulk Edit Tool](#bulk-edit-tool-tools)
- [Current Open Tasks Field](#current-open-tasks-field)
- [One-click Close Button (RTD Home Page)](#one-click-close-button-rtd-home-page)
- [Habit Due Dates with Rolling Forward](#habit-due-dates-with-rolling-forward)
- [Range Cadence (At Least N, At Most M)](#range-cadence-at-least-n-at-most-m)
- [Task Templates](#task-templates)
- [Undeveloped Ideas](#undeveloped-ideas)

---



## Feature: Extended Cadence (Every Y Periods)

**Status:** Pre-design
**One-liner:** Support recurring cadences spanning multiple periods (bi-weekly, quarterly, etc.) with a simplified picker for common cadences and a Custom mode for arbitrary X-per-Y configurations.

### Use cases
- Bi-weekly therapy prep task (1 per 2 weeks)
- Quarterly oil change (1 per quarter)
- "3 wrestling tournaments per year" already works today — Exactly N=3, Period=Year

### Fields added / changed

| #   | Field                                 | Type      | Change                      | Notes                                                                                                                                              |
| -----| ---------------------------------------| -----------| -----------------------------| ----------------------------------------------------------------------------------------------------------------------------------------------------|
| 1   | Condition                             | Select    | New (replaces Cadence Type) | `Every` / `At least` / `At most` / `Unlimited`                                                                                                     |
| 2   | Cadence                               | Select    | New                         | `Every Day` / `Every Week` / `Every 2 Weeks` / `Every Month` / `Every Quarter` / `Every Year` / `Custom`                                           |
| 3   | X (Custom Cadence)                    | Number    | Rename from `N Cadence`     | Task count per Y periods. Only read when Cadence=Custom.                                                                                           |
| 4   | Y (Custom Cadence)                    | Number    | New                         | Period multiplier. Only read when Cadence=Custom.                                                                                                  |
| 5   | Period (Custom Cadence)               | Select    | Rename from `Period`        | Day / Week / Month / Quarter / Year. Only read when Cadence=Custom.                                                                                |
| 6   | Cadence (Display)                     | Rich Text | New                         | Bot-written each governance run. Human-readable full cadence string (e.g. "At least 1 every 2 weeks"). Like Period Target on tasks — display only. |
| 7   | Current Period Start (Custom Cadence) | Date      | New                         | Bot advances on each new period; user can edit to shift window boundaries. Only meaningful when Y > 1.                                             |

Net-new fields: **3** (Y, Cadence (Display), Current Period Start). Fields 3 and 5 are renames of existing fields.

### Field interaction model

- **Condition=Unlimited**: no cap, no minimum — create a task whenever the prior completes. Bot ignores X, Y, Cadence. Use case: log bad habit occurrences without a period cap. If used with a non-Bad-Habit RTD, log a warning and treat as Every.
- **Cadence=simple option** (not Custom): X implicitly = 1, Y implicitly = 1, Period derived from the selected option. Custom fields (X, Y, Period) are ignored.
- **Cadence=Custom**: reads X, Y, Period (Custom Cadence) fields.
- **Condition applies to both simple and Custom cadences.** "At least 1 every 2 weeks" = Condition: At least + Cadence: Custom, X=1, Y=2, Period=Week.
- **Quarter** added as a first-class Period option alongside Day, Week, Month, Year.

### Default values and validation

| Field | Invalid / Missing | Action |
|---|---|---|
| Condition | Empty or unknown value | Default to `Every`; log WARNING; surface to Hub |
| Cadence | Empty or unknown value | Log ERROR, skip RTD this governance run; surface to Hub |
| X (Custom Cadence) | Empty when Cadence=Custom | Default to 1; log WARNING |
| X (Custom Cadence) | ≤ 0 | Default to 1; log WARNING |
| Y (Custom Cadence) | Empty when Cadence=Custom | Default to 1; log WARNING |
| Y (Custom Cadence) | ≤ 0 | Default to 1; log WARNING |
| Period (Custom Cadence) | Empty when Cadence=Custom | Log ERROR, skip RTD this governance run; surface to Hub |
| Current Period Start | Future date | Log ERROR, skip RTD this governance run; surface to Hub |
| Current Period Start | Empty on first activation | Bot writes today as the initial anchor |

Default-value behavior follows the same pattern as existing RTD field defaults (Grace Period, Anchor Day, etc.) — warn and continue where possible; skip and surface where the field is required for correct behavior.

### Period key computation for Y > 1

- Bot steps forward from Current Period Start in increments of `Y × period_length` to find the window containing today.
- When `today >= Current Period Start + Y × period_length`, bot advances Current Period Start by one interval and creates the next task.
- Period key format: extend `_period_key()` to embed Y (e.g. `W2-2026-05-19` for a 2-week window starting May 19).

### Decisions made so far
- **Unlimited in Condition, not Cadence.** Cadence answers "how often?" — all values are period-based. Condition answers "what's the enforcement rule?" — Unlimited fits there.
- **Current Period Start: future date = halt + Hub warning.** Backward extrapolation would be silent and surprising. The current period must contain today by definition.
- **Single Current Period Start field** (not two). Bot advances it forward each new period; user can edit mid-period to shift future boundaries. Field description on hover explains the dual role.
- **Cadence (Display)** bot-written on each governance run.
- **Quarter** added as first-class Period option.

### Open questions
- **Anchor Day for Year period**: does `_calc_due_date` currently handle it? Verify before designing behavior for Cadence=Custom + Period=Year + Anchor Day set.
- **Cadence (Display) write frequency**: every governance run (simple, slightly more API writes) or only on detected change (requires diffing)?
- **Condition=Unlimited + Cadence interaction**: if Cadence is still set when Condition=Unlimited, does the bot use it to determine how often to create a new tracking task, or fully ignore it? Needs decision.

### Migration
Breaking changes: Cadence Type → split into Condition + Cadence (select option values change). `N Cadence` → `X (Custom Cadence)`. `Period` → `Period (Custom Cadence)`.

Requires a migration script in `tools/` before shipping:
1. Read each RTD row: Cadence Type, N Cadence, Period values.
2. Map to new Condition + Cadence + X + Y + Period. RTDs with N=1 → simple Cadence option. RTDs with N>1 → Cadence=Custom, X=N, Y=1.
3. Write CSV backup of old values before any writes.
4. Apply new values.

### Dependencies
- Governance Schema Validation (PLANNED) — add new fields to the validation table once implemented.
- RTD Optional Fields Default Handling (PLANNED) — new fields need the same treatment.
- Automation Hub — Current Period Start future-date warning surfaces in Hub Section 2.

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

## Bug: RTD Optional Fields — Default Handling for Empty/Unknown Values

**Status:** Ready to implement (partial — Type default is the remaining gap)
**One-liner:** When an RTD has empty or unknown values for optional fields, governance should use sensible defaults rather than erroring or producing wrong results.

### Current state (what's already handled)
- **Anchor Day empty** → end-of-period anchor (already implemented)
- **Anchor Time empty** → no specific time (already implemented)
- **Grace Period empty / None** → treat as 0 / cancel on due date (fixed in Z8)

### Remaining gap
- **Type empty or unknown string** → treat as `"Habit"`. Currently, an unrecognized Type may cause unexpected governance behavior or a silent wrong-path execution. The fix: when reading Type from the RTD, if the value is empty, None, or not one of the known types (`"Habit"`, `"Responsibility"`, `"Bad Habit"`), default to `"Habit"` and log a warning.

### Note on scope
These fields are on the **RTD database only** — not on Task pages. No Task fields are added or changed by this fix. Users who want to surface these values on tasks can use a Notion rollup or linked-page view.

### Implementation
In `recurring_tasks.py`, when reading Type from an RTD page, add a guard:
```python
task_type = get_select(page, "Type") or "Habit"
if task_type not in {"Habit", "Responsibility", "Bad Habit"}:
    logger.warning(f"RTD '{title}' has unknown Type '{task_type}' — defaulting to 'Habit'.")
    task_type = "Habit"
```

**Hub integration:** the "unknown Type → defaulting to Habit" warning should surface in Hub Section 2 (Recurring Tasks) alongside the existing Anchor Day N>1 and "At most N wrong type" entries. Section 2 needs an "Errors & Warnings" sub-field for RTD-level warnings.

### Dependencies
- None. Isolated to `recurring_tasks.py`.
- Hub integration for surfacing warnings requires Automation Hub.

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

## Migration: Rename "At most N per period" → "Maximum N per period"

**Status:** Ready to implement
**One-liner:** Rename the cadence type select option to match the "Minimum N per period" naming convention.

### Changes required
1. Rename the Notion select option on the RTD from `"At most N per period"` → `"Maximum N per period"` (manual step in Notion)
2. Update the backward-compat shim in `recurring_tasks.py` to also normalize `"At most N per period"` → `"Maximum N per period"` (the existing `"N per period"` shim is the pattern to follow)
3. Update all references in DESIGN.md and README.md

### Notes
- Backward-compat shim means the rename is non-breaking for existing configs
- Deploy order: update code first, then rename in Notion (old option still works until renamed)

---

## Feature: Configurable Field Inheritance for Recurring Tasks

**Status:** Pre-design (P3 bug fixed; configurable inheritance is an Automation Hub feature)
**One-liner:** Let users control which fields are inherited when a recurring task is created — inclusive (whitelist) or exclusive (blacklist) — without touching code or config files.

### What was fixed (P3)
Root cause of inheritance failures was never `_copy_task_fields` logic — fields were being copied correctly. Failures came from API rejections on `create_page`:
- `files` type properties → Notion API rejects file attachments on create/update. Fixed: added `files` to `_READONLY_PROP_TYPES`.
- `people` type properties → read format includes full user objects (`name`, `avatar_url`, etc.); write format only accepts `{"id": "..."}`. Fixed: `_copy_task_fields` now strips people entries to ID only.
- Graceful fallback added: if `create_page` with inherited fields fails, log a WARNING (always visible) with Notion's error message, then retry with bot-managed fields only — task is always created.

### Configurable inheritance design (Automation Hub)

This belongs in the **Automation Hub**, not `config.toml`. Reasoning:
- Field inheritance is a schema-aware behavioral decision ("I added a column — should it be inherited?").
- Users should be able to reconfigure without restarting the daemon.
- `config.toml` is for workspace identity and connectivity (token, DB IDs). Behavioral schema decisions live in Automation Hub.

Two variables, configured per task database in the Automation Hub:

```
Inheritance Mode: Inclusive | Exclusive  (toggle or select)
Inheritance Fields: [field names]         (text list or multi-select)
```

- **Inclusive (whitelist):** only copy fields explicitly listed. Safest — new columns are ignored until opted in.
- **Exclusive (blacklist):** copy everything except listed fields. Current default behavior.

Hardcoded invariants (`FIELDS_NOT_INHERITED`, `_READONLY_PROP_TYPES`) remain in code regardless of user config — bot-managed fields and API-unwritable types are never inherited.

### Open questions
- Should "no config set" default to inclusive (safe) or exclusive (current behavior)?
- Automation Hub implementation is a prerequisite.

### Dependencies
- Automation Hub must exist before this can be configured at runtime.

---

## Feature: Icon Inheritance from RTD

**Status:** Ready to implement
**One-liner:** When the bot creates a recurring task, copy the RTD's icon (emoji or external image) to the new task so task instances carry consistent series branding automatically.

### Decisions made
- Source is always the RTD icon — same logic as Name. Users who want a one-off icon change can edit the task instance; it does not carry forward.
- `file` type icons (Notion-hosted uploads) are skipped — the hosted URL may not be reliably writable via the API. Only `emoji` and `external` types are copied.
- If the RTD has no icon set, the task is created without one (no change to current behavior).

### Implementation
1. Read `definition.get("icon")` from the RTD page object after fetching it.
2. If icon is not `None` and type is not `"file"`: pass it to `create_page`.
3. Add optional `icon` parameter to `create_page` in `notion_api.py`; include `"icon": icon` in the POST body when provided.
4. Notion_API patch version bump required.

### Dependencies
- Minor update to `create_page` in `Notion_API` (add `icon` param to POST body).

---

## Automation Hub (formerly "Project Page")

**Status:** Pre-design
**One-liner:** A single Notion page serves as the hub for all Notion Automator configuration, health status, and errors — one place to see and manage everything the bot does.

**Name note:** "Project Page" was used internally but is ambiguous. "Automation Hub" is preferred going forward. References in DESIGN.md and STATUS.md should be updated when this feature moves to implementation.

### Decisions made so far
- Auto-detection over activation buttons — daemon detects what exists and creates what is missing; no explicit "activate" step.
- User creates the Hub page manually and adds its ID to `config.toml` (one-time step). Bot fills in the rest.
- Notion page IDs are permanent — users can move the Hub page anywhere after initial creation.
- Preference: automatic setup with a clear startup log of what was created.
- **Single hub for all automations** — not a page-per-database. All task database configs and recurring task config live here together, mirroring the structure of `config.toml`.
- **Optional — graceful degradation:** if `hub_page_id` is absent from `config.toml`, all Hub features degrade gracefully: errors and warnings are log-only, no Hub writes occur. Every Hub-writing code path must check Hub availability before writing. This allows the daemon to run without a Hub configured.
- **Field name parsing (all text-list fields):** Hub text fields that accept field name lists (First Value Fields, Update Count Fields, Inheritance Fields) are parsed as CSV with: strip leading/trailing whitespace per token, case-insensitive match against the database schema, support double-quoted names for fields whose names contain commas.
- **Recurring Series type validation warning (Section 2):** if an RTD's cadence type is incompatible with its Task Type (e.g., "At most N per period" with Task Type ≠ "Bad Habit"), surface the warning in Hub Section 2 "Errors & Warnings". This extends the existing Anchor Day N>1 and "At most N wrong type" warning entries already planned for Section 2.

### Layout concept (mirrors config.toml structure)

**Section 1: Task Databases**
A child database (table view) where each row is a task database the bot monitors.

| Column              | Type      | Notes                                                                                            |
| ---------------------| -----------| --------------------------------------------------------------------------------------------------|
| Name                | Title     | Human-readable label                                                                             |
| Database ID         | Text      | Notion database ID                                                                               |
| Closed Date         | Checkbox  | Enables closed date stamping                                                                     |
| Reopen Count        | Checkbox  | Enables reopen count tracking                                                                    |
| Update Count Fields | Text      | Comma-separated fields to count updates for (e.g. "Due Date"). Replaces `due_date_update_count`. |
| First Value Fields  | Text      | Comma-separated fields to snapshot on first observation (e.g. "Due Date, Closed Date, Status")   |
| Inheritance Mode    | Select    | Inclusive (whitelist) or Exclusive (blacklist). Default: Exclusive (current behavior).           |
| Inheritance Fields  | Text      | Comma-separated fields for the selected inheritance mode.                                        |
| Errors & Warnings   | Rich Text | Bot-written; cleared when resolved                                                               |

**Section 2: Recurring Tasks**
A block (or small sub-page) showing the `[recurring_tasks]` config: definitions DB ID, tasks DB ID, enabled toggle. Errors and warnings from governance written here in an "Errors & Warnings" rich-text sub-field. Bot clears it when the issue resolves; user clears it to acknowledge.

Warnings to surface here (currently only logged):
- **Anchor Day ignored (N>1):** RTD uses "Exactly N per period" or "Minimum N per period" with N>1 and an Anchor Day set — Anchor Day is suppressed. User should clear Anchor Day or set N=1.
- **"At most N per period" with wrong Task Type:** RTD uses this cadence with a type other than Bad Habit — no due date will be set. User should change Task Type to Bad Habit or change cadence.
- **Unknown Task Type:** RTD Type field is empty or unrecognized — defaulting to Habit. User should set a valid Type: Habit, Responsibility, or Bad Habit.

**Section 3: Bot Health**
Last poll time, daemon uptime, most recent governance run. Written on each governance pass (not every poll — too noisy).

Activity counters are a back-burner idea here. Useful candidates: total recurring tasks created (lifetime), closed date stamps per period. Risk: every increment is an API write, which adds up quickly at poll frequency. Preferred approach: derive counters from the Notion_Analytics data pipeline rather than maintaining them independently in the Hub. Do not implement until Notion_Analytics integration is further along.

### On "selectable field" for First Value Field tracking
Notion does not have a native field-selector property type (the relation/rollup selector in the UI is not exposed as a reusable property). The closest approximation: a text field where the user types the field name manually. This is consistent with how `first_value_fields` works in `config.toml`. A dedicated child database (one row per tracked field, with a relation back to the Task Databases table) is possible but likely over-engineered for v1.

### RTD database setup (via create_database)

`create_database()` can also be used to scaffold a new RTD database, not just Hub sub-databases. Useful for first-time setup.

**Bot creates automatically:**
All non-formula/non-rollup fields: Type (select, pre-populated options), Cadence Type (select), Period (select), Status (status with "Active"), N Cadence (number), Anchor Day (number), Grace Period (number), Anchor Time (rich_text), plus the relation field pointing to the task database. Each field is created with a description (Notion API supports property descriptions) so the database is self-documenting out of the box.

**RTD field descriptions (to include in create_database call):**
| Field        | Description                                                                                                                                                                     |
| --------------| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Type         | Governs task behavior. Habit = repeats whether done or not; Responsibility = repeats only when completed; Bad Habit = tracks things to limit (use with "At most N per period"). |
| Cadence Type | How tasks recur per period. "Exactly N" = strict count; "Minimum N" = at least N, more welcome; "At most N" = cap, Bad Habit only.                                              |
| Period       | The recurring cycle length (Daily, Weekly, Monthly, etc.).                                                                                                                      |
| N Cadence    | Tasks to create per period. Leave blank for 1.                                                                                                                                  |
| Anchor Day   | Day to anchor the due date (1=Mon … 7=Sun for weekly; day of month for monthly). Leave blank for end-of-period.                                                                 |
| Anchor Time  | Due time in HH:MM (24-hour). Leave blank for no specific time.                                                                                                                  |
| Grace Period | Days after due date before the bot cancels an incomplete task. Leave blank to cancel on the due date.                                                                           |
| Status       | Set to Active to enable governance for this definition.                                                                                                                         |

**User completes manually (bot provides instructions):**
- "Is Open" formula on the task table — syntax depends on the user's Status field option names, so cannot be auto-generated reliably
- Open Task Count rollup on the RTD — requires the two-way relation and "Is Open" formula to exist first. Bot outputs the formula syntax and rollup config as a guide.
- Bot adds the new RTD database ID to `config.toml` automatically after creation.

**The two-way relation** between RTD and task table is required for the rollup. Notion API can create the relation field on the RTD side; the reverse field on the task table side is added by Notion automatically if the relation is set as two-way.

### Required code additions
- `create_database(parent_page_id, title, properties)` — to be added to `notion_api.py`.
- Bot must read Hub config at startup (after `config.toml`) and merge with or override local flags.
- Bot must write errors/warnings per-database-row rather than only to logs.
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
- Status dashboard write frequency: governance runs only (startup + daily governance cron) to avoid excessive API writes.

### Dependencies
- Automation Hub must exist before Notifications can store webhook URLs there.
- `create_database()` API method needed in `notion_api.py` first.
- Every automation must be updated to write its errors/warnings to the Hub row for its database rather than only to logs.

---

## Notifications

**Status:** Pre-design
**One-liner:** Outbound webhook support (Discord, Telegram) so the daemon can alert on governance events without requiring the user to check logs.

### Decisions made so far
- Implemented as `notifiers.py` — a utility module, not an automation function.
- Automations call it as a side effect — outside the `automations.py` return-dict pattern.
- Webhook URLs stored in the Automation Hub (not hardcoded in `config.toml`).

### Open questions
- Which events trigger a notification? (e.g. task deleted and replaced, grace period cancel, RTD duplicate name, At-most-N cap reached)
- Opt-in per-event or opt-out?
- Rate limiting — governance could fire multiple alerts in one run.

### Dependencies
- Project Page feature (for webhook URL configuration).

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

## First Value Field Tracking

**Status:** Pre-design (decisions largely made — ready for implementation planning)
**One-liner:** For any configured field, automatically stamp a `First [Field Name]` column with the field's first observed value — never updated after the initial write.

### Decisions made

- **Naming convention:** `First [Field Name]` — bot looks for a matching column by convention. User creates `First Due Date` and the bot auto-associates it with `Due Date`. No explicit field mapping in config.
- **Config:** replaces `due_date_tracking` with two independent flags:
  ```toml
  [[databases]]
  update_count_fields = ["Due Date"]                        # replaces due_date_update_count
  first_value_fields  = ["Due Date", "Closed Date", "Status"]  # list of fields to snapshot
  ```
  Bot looks for `First Due Date`, `First Closed Date`, `First Status`, etc. in the database schema. Missing columns are skipped silently. "Closed Date" is a useful example — "First Closed Date" captures the first time a task was ever completed.
- **`update_count_fields` replaces `due_date_update_count`:** old boolean key kept with a deprecation warning (treated as `["Due Date"]`). Bot looks for `[Field] Update Count` columns (e.g. `Due Date Update Count`, `Status Update Count`) and increments on each detected change. Moves from a per-database checkbox in Hub Section 1 to a text-list field ("Update Count Fields").
- **Breaking change:** `due_date_tracking = true` is replaced. Existing users must update `config.toml`. Add to deploy prerequisites when this ships.
- **Type support:**

| Type                                                                 | Supported | Notes                                                                      |
| ----------------------------------------------------------------------| -----------| ----------------------------------------------------------------------------|
| `date`                                                               | Yes       | native date field                                                          |
| `number`                                                             | Yes       | native number field                                                        |
| `select`                                                             | Yes       | store option name as text                                                  |
| `status`                                                             | Yes       | store option name as text (no group — group is inferable from option name) |
| `text`                                                               | Yes       | native text field                                                          |
| `url` / `email` / `phone`                                            | Yes       | store as text                                                              |
| `checkbox`                                                           | **No**    | default `false` is indistinguishable from untouched                        |
| `multi_select`                                                       | **No**    | too complex for v1                                                         |
| `rich_text` / `files` / `relation` / `formula` / `rollup` / `people` | **No**    | excluded                                                                   |

- **Write-once:** once `First [Field]` is stamped, the bot never overwrites it. If the user manually clears it, the bot re-stamps on the next poll (same as First Due Date today).
- **Does not increment any counter.** Purely a snapshot of the first observed value.

### Open questions
- None — ready for implementation planning.

### Dependencies
- `_db_configs` registry already in place. Config flag rename is the only breaking change.

---

## Improvement: Field Update Count (Abstract)

**Status:** Pre-design
**One-liner:** Generalize `auto_due_date_update_count` to track update counts for any configured field(s) — a companion to First Value Field Tracking, kept as a separate feature and separate config key.

### Design
- New config key per `[[databases]]` entry: `update_count_fields = ["Due Date", "Status"]`
- For each configured field, bot maintains a `[Field Name] Update Count` number field in the task database
- Increment logic: same as current `auto_due_date_update_count` — only when the date portion changes (for date fields) or value changes (for other types); not on first set; not on clear
- `due_date_update_count = true` becomes a deprecated alias for `update_count_fields = ["Due Date"]`; warn and continue

### Why separate from First Value Field Tracking
These answer different questions. First Value = "what was this when first observed?" (snapshot, one-time write). Update Count = "how many times has this changed?" (running counter). Users may want one without the other — merging forces unnecessary coupling.

### Dependencies
- First Value Field Tracking (shares config pattern; implement together or after)
- `_db_configs` registry already in place

---

## Improvement: RTD Display Fields (Current Period)

**Status:** Pre-design
**One-liner:** Bot-written `Current Period` Date field on the RTD showing the current period's start and end dates — enables Notion formula fields and rollups that filter tasks by current period.

### Design
- Field: `Current Period` — Date (start + end) on the RTD database
- Governance writes this field on every governance pass (cron + startup + RTD activation)
- Value: period start datetime → period end datetime for the current period
- User may edit to shift boundaries (same pattern as Extended Cadence's `Current Period Start`)

### Why needed
Notion formulas on task pages cannot compute period boundaries without a reference point. A formula using `.map(current.prop("Current Period"))` on the RTD relation can check whether a task's Due Date or Closed Date falls within the period — enabling "tasks this period" filters and rollups without bot involvement in per-task field writes.

### Note on Tasks Done This Period
The `Tasks Done This Period` Number field (previously planned as a bot-maintained counter on the RTD) is dropped in favour of this approach. A Notion rollup counting tasks where the period formula is satisfied is more accurate and requires no bot maintenance.

### Dependencies
- None — independent of `Current Open Tasks`

---

## Timer / Mission Tracking

**Status:** Pre-design (early — major open questions remain)
**One-liner:** Link closed tasks to mission areas (ADHD Medication, Art Chatbot, Improv, etc.) and surface a per-period effort heatmap across those missions.

### Context
The user maintains high-level mission/workbench areas in Notion. Each mission has its own page with notes and details. The idea is to aggregate closed tasks per period and attribute them to a mission — lightweight effort tracking, not time-logging.

### Open questions
- **Attribution method:** explicit relation field on the task (precise but requires user to fill it in), tag/keyword match (automatic but fuzzy), or mission page rollup?
- **Data model:** does the bot write anything back to the mission page, or is this pure reporting via Notion_Analytics?
- **"Timer" definition:** duration field, task count, or something else? No logged time exists — effort is inferred.
- **Scope:** is this Notion_Automator (bot writes to tasks/missions) or Notion_Analytics (read-only reporting from existing data)?

### Dependencies
- Needs attribution method decision before any design can happen.
- If bot-writes-back: depends on Project Page for configuration.
- If reporting-only: may belong entirely in Notion_Analytics, not here.

---

## Automated Testing

**Status:** Pre-design (deferred — add after feature set stabilizes)
**One-liner:** Unit tests for pure logic functions that have caused the most bugs — no mocking needed, no Notion API dependency.

### Decisions made
- Defer until design settles. Most bugs have been in edge cases that manual testing catches well; a test suite written mid-churn would need constant rewriting.
- Scope: pure logic functions only. Automation functions require mocked pages and a mocked client — high scaffolding cost for a personal project.

### Priority targets (highest bug history)
- `_period_dates` — weekly anchor-day off-by-week bug (Z17) would have been caught
- `_period_key` — period boundary edge cases
- `_calc_due_date` — end-of-period, anchor day, monthly/weekly math

### What NOT to test (yet)
- Automation functions (`auto_closed_date`, `auto_recurring_tasks`, etc.) — require mocked Notion API and fabricated page dicts; high scaffolding cost
- Governance functions — require live or deeply mocked Notion state
- Integration tests — require a real Notion workspace; better handled by the existing manual test plan

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

## Current Open Tasks Field

**Status:** Pre-design
**One-liner:** Bot-written Relation field on the RTD that tracks all currently open tasks for the series — exposes in-memory tracking as a visible Notion property.

### Motivation
The bot already tracks open tasks per RTD in memory (`open_tasks_by_def`). Exposing this as a Notion Relation field lets the user see exactly which tasks the bot considers active, making bot state visible and debuggable. Also a prerequisite for one-click close and deletion detection.

### Design
- Field type: Relation (multi) → task database
- Bot writes this field on every governance pass: adds newly created tasks, removes tasks that entered the Complete group or were cancelled
- All currently open tasks for the series are included (across periods — the user may have pre-created future-period tasks)
- User should not edit — content is overwritten by governance

### Required implementation behaviors
All of the following must be implemented for this feature to be complete:

1. **Write on task creation** — add newly created task to the relation
2. **Clear on completion/cancellation** — remove task when it enters Complete group or is cancelled
3. **Full refresh on RTD activation** — rebuild the relation from scratch when RTD transitions to Active
4. **Q1-C promotion logic** — when a user-created future-period task becomes the current period's task, archive the bot-created task and update `Current Open Tasks` to point to the user's task (only if cadence limit is met)
5. **Deletion detection** — when governance runs, fetch each task in `Current Open Tasks` directly via `GET /v1/pages/{id}`; treat 404 or `archived: true` as deletion; create replacement task with On Hold status and note
6. **Recovery guard** — if a previously archived task is recovered from trash, it will appear as an open task alongside the replacement; do not auto-re-archive it; let governance drift-correct both tasks' fields and let them coexist

### Governance interaction
Governance writes this field when:
- A new task is created (add to relation)
- A task enters the Complete group (remove from relation)
- A task is cancelled (remove from relation)
- RTD transitions to Active (full refresh)
- Period changes, `week_start`, or `day_start_hour` changes trigger governance via RTD activation

### Blocks
- One-click Close Button (requires this field as a Notion Button target)
- Deletion detection (requires this field to check if the bot-tracked task still exists)

### Dependencies
- `notion_api.py` must support writing Relation properties
- Governance must be updated to maintain this field on all relevant transitions

---

## One-click Close Button (RTD Home Page)

**Status:** Pre-design
**One-liner:** A Notion Button field on the RTD (or a home page linking to RTDs) that closes the current period's open task with one tap — triggers normal next-task creation automatically.

### Motivation
Habits especially benefit from minimal friction. Currently closing a task requires opening it, changing the status, and waiting for the bot to create the next one. A button on the RTD row reduces this to one tap.

### Design
- Notion Button field on the RTD: "Complete Current Task"
- Button action: "Edit pages in relation" → targets `Current Open Tasks` field → sets Status to Done (or equivalent Complete-group status)
- Bot detects the status change on next poll → stamps Closed Date → creates next task normally
- If multiple open tasks: button targets only the task in the current period (not future-period pre-created tasks). Design TBD — may require filtering in the button action or accepting that all open tasks are closed.
- Applies to all task types (Habit, Responsibility, Bad Habit) — not Habit-only

### Dependencies
- `Current Open Tasks` field must be implemented first
- Notion Buttons must support "filter by period" or user accepts all-open behavior

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

## Range Cadence (At Least N, At Most M)

**Status:** Pre-design
**One-liner:** A new Cadence Type that enforces both a minimum and a maximum per period — e.g. "at least 2 gym sessions, at most 5."

### Motivation
Current cadences are one-sided: Minimum N (open-ended upper bound) or Exactly N (hard cap). A range cadence gives users a target window rather than a single number.

### Open questions
- Field representation: two separate number fields (`N Cadence` and `M Cadence`), or a single text field ("2-5"), or two new RTD columns?
- Governance behavior when count is between N and M: no action (in range), create more if below N, stop creating if at M.
- What happens at M? Same as "Exactly N" — route next task to next period. Or flag the RTD and let the user decide?
- How does this interact with Extended Cadence (every Y periods)?

### Dependencies
- Resolve DESIGN.md discrepancy #9 (At most N behavior) first — the current implementation hard-routes to next period rather than soft-capping. Range cadence needs that resolved to know which model to build on.
- Extended Cadence design should be reviewed for overlap before committing to new fields.

---

## Task Templates

**Status:** Pre-design
**One-liner:** Apply a Notion template to bot-created recurring tasks so they include user-configured buttons and content (e.g. "Create child task", "Create blocker task").

### Background
Notion's API supports templates via `GET /v1/data_sources/{db_id}/templates` (list) and `POST /v1/pages` with `template: {type: "template_id", template_id: "..."}` (create). Template content is applied asynchronously — the page is returned immediately but blank; Notion fills in content in the background.

### Design
- Config: optional `template_id` (or `template_name`) per `[[databases]]` entry in `config.toml`
- At startup: bot resolves template name → ID (via list endpoint) if name configured
- On task creation: pass `template` param to `create_page()`; omit `children` (not allowed when using templates)
- Template must be in the same workspace and accessible to the integration token

### Open questions
- Should template be per-RTD or per-database? Per-database is simpler; per-RTD allows different templates for different series.
- `notion_api.py` needs a `list_templates(database_id)` method — design alongside the feature.
- Async gap: page exists but is blank for a moment. Does the bot's next poll see incomplete content? Probably not an issue since the bot only reads/writes properties, not page content.

### Dependencies
- `notion_api.py`: add `list_templates()` and update `create_page()` to accept an optional `template_id` param

---

## Undeveloped Ideas

Ideas raised but not designed. No open questions analyzed — revisit when the relevant feature area is active.

- **Lookahead (Future Task Pre-creation)** — Auto-create tasks for N future periods so they appear on the calendar in advance. Deferred: the manual workaround (create a task with a future Due Date → bot initializes it) already serves the need and is more precise. Revisit if manual creation becomes burdensome at scale.

- **Specific Days (Recurring Tasks, multi-day per week)** — One RTD creates tasks on specific weekdays (e.g., Tuesday AND Thursday club meetings). Currently handled by two RTDs (one per day), which is clean and keeps tracking streams separate. Implementing this would require governance to create N tasks per week, one per selected day — a significant change to the governance loop. Two-RTD workaround is the current recommendation.

- **New Anchor Types (Recurring Tasks)** — Apple Calendar-style rules for RTDs: "First Monday of the month", "Last weekday of the month", "Second Tuesday". Anchor Day on the RTD currently handles day-of-month (1–31) for monthly tasks. Supporting ordinal weekday logic ("nth weekday within a period") would require a new anchor type field on the RTD and additional calendar math. Dependency: Extended Cadence feature should land first since it touches the same period/anchor system.
