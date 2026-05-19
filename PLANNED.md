# Notion Automator — Planned Features

Living design document. Sections are deleted when a feature is implemented and its decisions have moved to `DESIGN.md`. See `STATUS.md` for one-line summaries and current priority.

---

## RTD Series State (Habit Lifecycle)

**Status:** In design
**One-liner:** Replace the Active checkbox on RTDs with a richer Status field that captures why a series is inactive — enabling semantic reporting and informational bot notes on stale open tasks.

### Problem being solved
The current Active checkbox is binary — it doesn't distinguish between "paused temporarily," "habit successfully formed," "bad habit eliminated," and "responsibility no longer relevant." This distinction matters for PowerBI reporting (how many habits were established vs. abandoned, average time to establishment) and for the user's own understanding of their series history.

### Decisions made

**Status field replaces the Active checkbox on the RTD.**
Notion's built-in status groups provide visual organization in the database view without requiring any additional bot logic.

| Status | Group | Bot creates tasks? |
|---|---|---|
| `Planned` | To-do | No |
| `Active` | In Progress | **Yes — only this status** |
| `On Hold` | In Progress | No |
| `Completed` | Complete | No |
| `Retired` | Complete | No |
| `Abandoned` | Complete | No |

**Bot logic: `Status == "Active"` is the only check.** Group membership is irrelevant to the bot — it checks the status value only. This means adding new statuses in future is safe by default (bot ignores them unless explicitly coded).

**Type + Status carries semantic meaning; no type-specific statuses needed.**
In reporting, the combination of Type and Status tells the full story:
- Good Habit + Completed → habit established
- Bad Habit + Completed → behavior eliminated
- Responsibility + Retired → no longer applicable
Terminal statuses are shared across all types; the Type column provides the interpretation in PowerBI.

**Bot Note on RTD when series is inactive but an open task exists.**
Message: *"Series is not active but there is an open task."*
- Appears when: `Status ≠ Active` AND at least one open task is linked to this RTD
- Clears when: no open tasks exist for the RTD (governance pass finds count = 0)
- Mechanism: existing Bot Notes accumulator on RTDs — no new architecture needed
- Instructional content (why Active is the only task-creating status) belongs in field descriptions and a Usage Guide, not the bot note

**No destructive action on open tasks when RTD goes inactive.**
Follows the least-destructive-intervention principle (§1.1). The open task stays open; the user closes it when ready. The bot note on the RTD is the only intervention.

**Note clearing is governance-timed, not event-driven.**
Notion does not push deletion or change notifications. The bot detects open task count at each governance pass (startup + 2am cron). Note may persist until the next governance run — acceptable given it's informational only.

### Open questions
- **Active checkbox relationship:** Does the new Status field fully replace the Active checkbox, or does it work alongside it? Replacing is cleaner (one field, not two) but requires updating every code path that currently checks `Active`. Recommend replacing, but this is a decision for implementation planning.
- **Reversal behavior:** When Status returns to `Active` from a terminal state (e.g. relapse on a formed habit), governance creates a new task as normal. Should the series history (prior terminal state) be preserved anywhere, or is it sufficient that Change Tracking logs the transition? Depends on Change Tracking being implemented.
- **`Planned` status and governance:** Should governance warn if an RTD has been in `Planned` state for longer than some threshold? Or is it purely informational with no bot action?

### Dependencies
- **RTD monitoring** (open decision in STATUS.md) — required for real-time detection of Status changes (e.g. task creation when an RTD transitions from Planned → Active mid-day). The bot note itself works without RTD monitoring since it runs in the governance pass.
- **Change Tracking** — required for lifecycle history: when a series was established, how long it was active, reversal count. Without Change Tracking, only the current state is known.

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

## Project Page

**Status:** Pre-design
**One-liner:** A single Notion page acts as the daemon's home base — auto-creates child databases on first run, provides a status dashboard, and surfaces configuration without editing `config.toml`.

### Decisions made so far
- Auto-detection over activation buttons — daemon detects what exists and creates what is missing; no explicit "activate" step.
- User creates the Project Page manually and adds its ID to `config.toml` (one-time step). Bot fills in the rest.
- Notion page IDs are permanent — users can move created pages anywhere after initial creation.
- Preference: automatic setup with a clear startup log of what was created (over a wizard-style prompt).

### Planned capabilities
- **Database bootstrapping:** auto-create the RTD database as a child of the Project Page on first run; write the new DB ID to `config.toml` or log it.
- **Status dashboard:** last poll time, health indicators, recent errors — written to the Project Page by the daemon.
- **Notion-based configuration:** fields on the Project Page (or child databases) that the daemon reads:
  - Recurring task field copy exclusions (beyond `FIELDS_NOT_INHERITED` defaults)
  - Task naming format (cadence suffix on/off)
  - Notification webhook URLs and alert conditions

### Required code additions
- `create_database(parent_page_id, title, properties)` — to be added to `notion_api.py`.

### Open questions
- Which config values move from `config.toml` to Notion? All of them, or only the ones non-technical users need to adjust?
- Status dashboard: how often is it written? Every poll (noisy) or only on governance runs?
- Error surfacing: written to the Project Page, or only logged to file?

### Dependencies
- Project Page must exist before Notifications can store webhook URLs there.
- `create_database()` API method needed in `notion_api.py` first.

---

## Notifications

**Status:** Pre-design
**One-liner:** Outbound webhook support (Discord, Telegram) so the daemon can alert on governance events without requiring the user to check logs.

### Decisions made so far
- Implemented as `notifiers.py` — a utility module, not an automation function.
- Automations call it as a side effect — outside the `automations.py` return-dict pattern.
- Webhook URLs stored in the Project Page (not hardcoded in `config.toml`).

### Open questions
- Which events trigger a notification? (e.g. task deleted and replaced, grace period cancel, RTD duplicate name, At-most-N cap reached)
- Opt-in per-event or opt-out?
- Rate limiting — governance could fire multiple alerts in one run.

### Dependencies
- Project Page feature (for webhook URL configuration).

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
