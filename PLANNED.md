# Notion Automator — Planned Features

Living design document. Sections are deleted when a feature is implemented and its decisions have moved to `DESIGN.md`. See `STATUS.md` for one-line summaries and current priority.

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

### Dependencies
- `_task_db_properties` is currently private to `recurring_tasks.py` — either expose it or replicate the schema-loading pattern in `automations.py`.

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

### Dependencies
- None. Isolated to `recurring_tasks.py`.

---

## Bug: Field Inheritance for Recurring Tasks

**Status:** Ready to implement (pending P3 root cause investigation)
**One-liner:** Select, multi-select, and possibly other field types are not being copied to newly created recurring tasks — plus a design improvement to make the inheritance list configurable.

### Bug (P3)
`_copy_inherited_props()` uses `FIELDS_NOT_INHERITED` as a blacklist. Select/multi-select are not in that list and are not read-only, so they should be copied — but they aren't. Root cause needs investigation before the fix is written. Fields confirmed not copying: Select, Multi-select (priority, effort level, pursuit, area, parent/child). Fields not yet tested: relation, text, checkbox.

### Design improvement — configurable inheritance

Replace the module-level `FIELDS_NOT_INHERITED` set with two variables:

```python
fields_inheritance_list_is_inclusive: bool = True
# True  → inheritance_fields is a WHITELIST (only copy these fields)
# False → inheritance_fields is a BLACKLIST (copy everything except these)

inheritance_fields: list[str] = []
```

- Boolean flag prevents misconfiguration — it's either inclusive or exclusive, no ambiguity.
- Default behavior (empty list + inclusive=False) copies all non-bot, non-read-only fields, matching current intent.
- User can flip to inclusive=True and list exactly which fields to carry forward, ignoring everything else.
- These become config values in `config.toml` under `[recurring_tasks]`, not hardcoded constants.

### Open questions
- What is the actual root cause of select/multi-select not copying? (Investigate `_copy_inherited_props` before implementing the redesign.)
- Should relation fields be copied? They reference other pages by ID — if the related page is valid, copying makes sense. But orphaned relations could cause silent Notion errors.

### Dependencies
- P3 root cause investigation must happen first.

---

## Bug: "Minimum N per period" period transition behavior

**Status:** Ready to implement
**One-liner:** Two wrong behaviors when a Minimum N per period series crosses a period boundary — tasks advance to next period too early, and stale-period cleanup uses Cancel when it shouldn't.

### Decisions made

**During-period behavior (on task close):**
- When Occurrence # reaches N (minimum met), create the next task with the **current period's** due date, not the next period's. Minimum means more completions are still welcome; the period should stay open.
- When Occurrence # is below N, behavior is unchanged — create next task for the same period as before.

**Stale-period governance (due date has passed, period has rolled over):**
- **Minimum NOT met:** Cancel the open task (records the failure). Create a fresh task for the new period.
- **Minimum WAS met:** Archive (delete via API) the open task — do NOT cancel, as cancellation implies failure. Create a fresh task for the new period. Creating fresh rather than updating Due Date on the existing task prevents Due Date Update Count from firing. Standard duplicate guard applies.

**On recording missed completions as phantom cancelled tasks:** Not worth implementing. Occurrence # on the cancelled task already captures how many were completed — the gap to N is inferable without polluting the database.

### Open questions
- Does `notion_api.py` already support archiving a page (`PATCH /pages/{id}` with `{"archived": true}`)? Likely needs a new method or a flag on `update_page`. Verify before implementing.

### Dependencies
- None. Isolated to `recurring_tasks.py`.

---

## Change Tracking

**Status:** Pre-design
**One-liner:** Opt-in field change monitoring — every detected change logged with old value, new value, page ID, and timestamp for reporting via Notion_PowerBI.

### Decisions made so far
- Opt-in by default — no fields tracked unless explicitly configured. Prevents accidental capture of large or sensitive fields.
- Excluded field types: `files`, `rich_text` above a configurable length threshold — too large and too noisy.
- Local storage preferred over a Notion database — avoids API overhead on every change event.
- Change tracking reads from the in-memory snapshot (already held for change detection) — no extra API calls needed.

### Proposed config
```toml
[change_tracking]
enabled = true
fields = ["Status", "Due Date", "Assignee"]  # opt-in list
```

### Proposed record schema
| Field | Description |
|---|---|
| `timestamp` | UTC time the change was detected |
| `page_id` | Notion page ID |
| `database_id` | Notion database the page belongs to |
| `field_name` | Property name that changed |
| `old_value` | Previous value (serialized) |
| `new_value` | New value (serialized) |

### Open questions
- Storage format: SQLite vs. CSV/JSONL append log vs. dedicated Notion database. Local preferred — needs a decision.
- How does Notion_PowerBI consume the log? Pull from file, or does Notion_Automator push to a shared location?
- Maximum retention / rotation policy?

### Dependencies
- Storage decision must happen before implementation.
- Notion_PowerBI integration design should inform the storage format choice.

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

### Layout concept (mirrors config.toml structure)

**Section 1: Task Databases**
A child database (table view) where each row is a task database the bot monitors.

| Column                | Type      | Notes                                                             |
| -----------------------| -----------| -------------------------------------------------------------------|
| Name                  | Title     | Human-readable label                                              |
| Database ID           | Text      | Notion database ID                                                |
| Closed Date           | Checkbox  | Enables closed date stamping                                      |
| Reopen Count          | Checkbox  | Enables reopen count tracking                                     |
| Due Date Update Count | Checkbox  | Enables due date change counter                                   |
| First Value Fields    | Text      | Comma-separated list of fields to track (e.g. "Due Date, Status") |
| Errors & Warnings     | Rich Text | Bot-written; cleared when resolved                                |

**Section 2: Recurring Tasks**
A block (or small sub-page) showing the `[recurring_tasks]` config: definitions DB ID, tasks DB ID, enabled toggle. Errors and warnings from governance written here.

**Section 3: Bot Health**
Last poll time, daemon uptime, most recent governance run. Written on each governance pass (not every poll — too noisy).

Activity counters are a back-burner idea here. Useful candidates: total recurring tasks created (lifetime), closed date stamps per period. Risk: every increment is an API write, which adds up quickly at poll frequency. Preferred approach: derive counters from the Change Tracking log (already planned) rather than maintaining them independently — log is source of truth, Hub surfaces the summary. Do not implement until Change Tracking exists.

### On "selectable field" for First Value Field tracking
Notion does not have a native field-selector property type (the relation/rollup selector in the UI is not exposed as a reusable property). The closest approximation: a text field where the user types the field name manually. This is consistent with how `first_value_fields` works in `config.toml`. A dedicated child database (one row per tracked field, with a relation back to the Task Databases table) is possible but likely over-engineered for v1.

### Required code additions
- `create_database(parent_page_id, title, properties)` — to be added to `notion_api.py`.
- Bot must read Hub config at startup (after `config.toml`) and merge with or override local flags.
- Bot must write errors/warnings per-database-row rather than only to logs.

### Open questions
- Which config values move from `config.toml` to Notion? Proposal: `config.toml` remains the source of truth for IDs and the Hub page ID; all automation flags migrate to Notion so non-technical users can toggle them without editing a file.
- Status dashboard write frequency: governance runs only (startup + 2am cron) to avoid excessive API writes.
- When Hub config and `config.toml` conflict, which wins? Proposal: Hub takes precedence for flags; `config.toml` required only for IDs and the Hub page ID itself.

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
- No historical preservation of the blocking relationship in this feature. Historical relation changes are covered by Change Tracking (relational fields are eligible for opt-in tracking there).
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
  due_date_update_count = true          # existing counter behavior
  first_value_fields    = ["Due Date", "Status"]   # new — list of fields to track
  ```
  Bot looks for `First Due Date`, `First Status`, etc. in the database schema. Missing columns are skipped silently.
- **Breaking change:** `due_date_tracking = true` is replaced. Existing users must update `config.toml`. Add to deploy prerequisites when this ships.
- **Type support:**

  | Type | Supported | Notes |
  |---|---|---|
  | `date` | Yes | native date field |
  | `number` | Yes | native number field |
  | `select` | Yes | store option name as text |
  | `status` | Yes | store option name as text (no group — group is inferable from option name) |
  | `text` | Yes | native text field |
  | `url` / `email` / `phone` | Yes | store as text |
  | `checkbox` | **No** | default `false` is indistinguishable from untouched |
  | `multi_select` | **No** | too complex for v1 |
  | `rich_text` / `files` / `relation` / `formula` / `rollup` / `people` | **No** | excluded |

- **Write-once:** once `First [Field]` is stamped, the bot never overwrites it. If the user manually clears it, the bot re-stamps on the next poll (same as First Due Date today).
- **Does not increment any counter.** Purely a snapshot of the first observed value.

### Open questions
- None — ready for implementation planning.

### Dependencies
- `_db_configs` registry already in place. Config flag rename is the only breaking change.

---

## Timer / Mission Tracking

**Status:** Pre-design (early — major open questions remain)
**One-liner:** Link closed tasks to mission areas (ADHD Medication, Art Chatbot, Improv, etc.) and surface a per-period effort heatmap across those missions.

### Context
The user maintains high-level mission/workbench areas in Notion. Each mission has its own page with notes and details. The idea is to aggregate closed tasks per period and attribute them to a mission — lightweight effort tracking, not time-logging.

### Open questions
- **Attribution method:** explicit relation field on the task (precise but requires user to fill it in), tag/keyword match (automatic but fuzzy), or mission page rollup?
- **Data model:** does the bot write anything back to the mission page, or is this pure reporting via Notion_PowerBI?
- **"Timer" definition:** duration field, task count, or something else? No logged time exists — effort is inferred.
- **Scope:** is this Notion_Automator (bot writes to tasks/missions) or Notion_PowerBI (read-only reporting from existing data)?

### Dependencies
- Needs attribution method decision before any design can happen.
- If bot-writes-back: depends on Project Page for configuration.
- If reporting-only: may belong entirely in Notion_PowerBI, not here.

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
