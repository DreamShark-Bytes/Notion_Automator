# Notion Automator

A self-hosted automation daemon that extends Notion with custom logic — running on your own hardware, on your own terms, no subscription required.

Notion's built-in automations are powerful but gated behind paid plans and limited to predefined actions. This daemon lets you write your own automation rules in Python and react to any change in your Notion databases within a configurable poll interval. Runs on any Linux system with systemd (Raspberry Pi, Ubuntu, Debian, etc.).

## Features

- **[Closed Date Stamping](#closed-date-stamping)** — automatically stamps a Closed Date when a task is marked done, and clears it on reopen.
- **[Reopen Count](#reopen-count)** — tracks how many times a task has been reopened.
- **[Due Date Tracking](#due-date-tracking)** — records the first due date ever set and counts how many times the due date has been changed.
- **[Recurring Tasks](#recurring-tasks)** — keeps one open task per series at all times; creates the next task automatically when one is completed or cancelled. Supports habits, responsibilities, and more.
- **[Custom Automations](#adding-your-own-automations)** — write your own rules in Python with full access to page properties and change history.

---

## Table of Contents

- [Setup](#setup)
  - [1. Get a Notion Integration Token](#1-get-a-notion-integration-token)
  - [2. Find Your Database ID](#2-find-your-database-id)
  - [3. Install](#3-install)
  - [4. Configure](#4-configure)
  - [5. Test It](#5-test-it)
  - [6. Run as a System Service](#6-run-as-a-system-service)
- [Closed Date Stamping](#closed-date-stamping)
- [Reopen Count](#reopen-count)
- [Due Date Tracking](#due-date-tracking)
- [Recurring Tasks](#recurring-tasks)
- [Adding Your Own Automations](#adding-your-own-automations)
- [Tuning Poll Interval](#tuning-poll-interval)
- [How it compares to Notion's built-in automations](#how-it-compares-to-notions-built-in-automations)
- [Usage Guide](#usage-guide)
- [Updating](#updating)
- [Future Plans](#future-plans)

---

## Setup

### 1. Get a Notion Integration Token

1. Go to https://www.notion.so/my-integrations
2. Click **+ New integration** → give it a name → Submit
3. Copy the **Internal Integration Secret**
4. Open each Notion database you want to automate → `...` menu → **Add connections** → select your integration

### 2. Find Your Database ID

Open the database in Notion. The URL looks like:
```
https://www.notion.so/myworkspace/abc123def456...?v=...
```
The database ID is the UUID between your workspace name and `?v=`. Copy it without dashes.

### 3. Install

**Linux (Raspberry Pi / Debian / Ubuntu) — install system dependencies if not already present:**
```bash
sudo apt update && sudo apt install -y python3 python3-venv git
```

```bash
git clone https://github.com/DreamShark-Bytes/Notion_Automator
cd Notion_Automator

python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**Windows — install Python if not already present:**

1. Download Python 3.11+ from [python.org](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install
2. (Optional) Install Git from [git-scm.com](https://git-scm.com/) to clone via terminal, or download the repo as a ZIP

```powershell
git clone https://github.com/DreamShark-Bytes/Notion_Automator
cd Notion_Automator

python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

`requirements.txt` includes the pinned [Notion_API](https://github.com/DreamShark-Bytes/Notion_API) dependency — no separate install step needed.

**Compatibility**

| Notion Automator | Notion API |
|---|---|
| v1.x | v1.x |

### 4. Configure

**Linux:**
```bash
cp config_example.toml config.toml
nano config.toml
```

**Windows:**
```powershell
copy config_example.toml config.toml
notepad config.toml
```

At minimum, set your integration token and add one `[[databases]]` block for each database you want to automate:

```toml
token = "ntn_your_token_here"

[[databases]]
id = "your-database-id"
```

Enable individual features per database by adding flags to the block. See each feature section below for the exact flags.

### 5. Test It

**Linux:**
```bash
venv/bin/python daemon.py
```

**Windows:**
```powershell
venv\Scripts\python daemon.py
```

You should see polling logs every 60 seconds. Make a change in Notion and watch it react.

### 6. Run as a System Service (auto-start on boot)

#### Linux — systemd

Edit the service file **before** copying it — replace both placeholder values:

| Placeholder | Replace with |
|---|---|
| `YOUR_USER` | your Linux username (e.g. `vince`) |
| `/path/to/Notion_Automator` | absolute path to this repo (e.g. `/home/vince/Documents/Notion_Automator`) |

```bash
nano notion-daemon.service

sudo cp notion-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable notion-daemon
sudo systemctl start notion-daemon

# Check status / logs
sudo systemctl status notion-daemon
journalctl -u notion-daemon -f
```

#### Windows — NSSM

[NSSM](https://nssm.cc/) (Non-Sucking Service Manager) wraps the daemon as a proper Windows service — auto-starts on boot, restarts on failure, runs while locked.

1. Download NSSM from [nssm.cc/download](https://nssm.cc/download) — extract and put `nssm.exe` somewhere permanent (e.g. `C:\Tools\nssm.exe`)

2. Open `install-service.ps1` in a text editor and set `$ProjectDir` and `$NssmPath` to match your machine.

3. Open **PowerShell as Administrator** and run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\install-service.ps1
```

4. Manage the service from **PowerShell**:

```powershell
Start-Service NotionAutomator
Stop-Service NotionAutomator
Get-Service NotionAutomator
```

To list all NSSM-managed services (useful if you forget the service name):

```powershell
Get-CimInstance Win32_Service | Where-Object PathName -match 'nssm.exe' | Format-Table Name, DisplayName, State
```

Or open **services.msc** and find **Notion Automator** in the list.

> **Note:** Avoid using `sc` in PowerShell — it is aliased to `Set-Content`, not the Service Control Manager. Use `Start-Service` / `Stop-Service` / `Get-Service` instead, or run `sc.exe` (with the `.exe` extension) if you prefer the `sc` syntax.

Logs are written to `notion_daemon.log` in the project directory.

---

## Closed Date Stamping

Automatically stamps a **Closed Date** field when a task moves to the Done status group, and clears it if the task is reopened. If you pre-fill Closed Date before closing (e.g. to backdate a completion), the bot leaves your value in place.

On daemon startup, any task already in Done without a Closed Date gets backfilled from its `last_edited_time`.

### Notion setup

Add a **Date** field named `Closed Date` to your task database.

### Config

```toml
[[databases]]
id          = "your-database-id"
closed_date = true
```

---

## Reopen Count

Increments a **Reopen Count** field each time a task moves out of the Done status group. Useful for tracking how often a task gets re-opened after being marked complete.

Requires Closed Date Stamping to be enabled, since reopen detection relies on the same status transition logic.

**Daemon-downtime recovery:** if a task was closed while the daemon was running (Closed Date stamped), then the user reopened it while the daemon was offline, the daemon will see a non-Complete task that still has a Closed Date set when it restarts. It treats this as a missed reopen: Reopen Count is incremented and Closed Date is cleared. This is self-healing — no manual correction needed.

### Notion setup

Add a **Number** field named `Reopen Count` to your task database.

### Config

```toml
[[databases]]
id           = "your-database-id"
closed_date  = true   # required
reopen_count = true
```

---

## Due Date Tracking

Tracks two things when a task's Due Date changes:

- **First Due Date** — stamped once with the original due date, never overwritten. Shows how far the task drifted from its original plan.
- **Due Date Update Count** — incremented each time the date portion of Due Date changes. Time-only changes (e.g. moving from 9am to 2pm on the same day) do not count.

### Notion setup

Add the following fields to your task database:

| Field | Type |
|---|---|
| `First Due Date` | Date |
| `Due Date Update Count` | Number |

### Config

```toml
[[databases]]
id                = "your-database-id"
due_date_tracking = true
```

---

## Recurring Tasks

Keeps one open task per series at all times. When a task is marked Done or Cancelled, the bot automatically creates the next one for the appropriate period.

### Notion setup — Definitions database

Create a new database in Notion with these fields:

| Field               | Type      | Notes                                                                                                  |
| ---------------------| -----------| --------------------------------------------------------------------------------------------------------|
| Name                | Title     |                                                                                                        |
| Type                | Select    | `Habit`, `Bad Habit`, `Responsibility`                                                                 |
| Status              | Status    | Bot only creates tasks when Status = `Active`                                                          |
| Cadence Type        | Select    | `Once per period`, `Exactly N per period`, `Maximum N per period`, `Minimum N per period`, `Unlimited` |
| N Cadence           | Number    | Used by cadence types that reference N; blank for others                                               |
| Period              | Select    | `Day`, `Week`, `Month`, `Year`                                                                         |
| Anchor Day          | Number    | Mon=1 … Sun=7 for weekly; 1–31 for monthly (overflows to last day of month)                            |
| Anchor Time         | Text      | e.g. `13:00`; blank = no specific time                                                                 |
| Grace Period (days) | Number    | Responsibilities only — auto-cancelled this many days past due; blank = never                          |
| Notes               | Rich Text |                                                                                                        |
| Last Completed      | Rollup    | Max of `Closed Date` from related tasks                                                                |

### Notion setup — Task database

Add these fields to your main tasks database:

| Field                                     | Type     | Required | Notes                                                                         |
| -------------------------------------------| ----------| ----------| -------------------------------------------------------------------------------|
| Recurring Series                          | Relation | Yes      | Points to the Definitions database                                            |
| Closed Date                               | Date     | Yes      | Required for period logic — see [Closed Date Stamping](#closed-date-stamping) |
| Occurrence # this Period (Recurring Task) | Number   | No       | Count of completions this period; filled in by the bot                        |
| Period Key (Recurring Task)               | Text     | No       | Display label for the current period — written by the bot, do not edit        |
| Period Target (Recurring Task)            | Text     | No       | e.g. `Minimum 3 per Week` — set by the bot at creation                        |

Connect your integration to both databases (`...` menu → **Add connections**).

### Config

```toml
# Global — controls when the logical day begins (affects all period calculations).
# Times between midnight and this hour are attributed to the previous calendar day.
# Also sets when the daily governance cron fires. Default: 3 (3am).
day_start_hour = 3

# Global — first day of the week. Affects weekly recurring task period boundaries.
# Default: "Sunday" (matches Notion). Use "Monday" for ISO/work-week convention.
week_start = "Sunday"

[[databases]]
id          = "your-tasks-database-id"
closed_date = true   # required for recurring task period logic

[recurring_tasks]
enabled           = true
definitions_db_id = "your-definitions-database-id"
tasks_db_id       = "your-tasks-database-id"
```

### How it works

- **One open task per series at all times.** When a task is marked Done or Cancelled, the bot creates the next one automatically.
- **Due dates** are calculated from Anchor Day and Anchor Time. Without an anchor, the due date is a range spanning the full period (e.g. April 1–30 for a monthly task).
- **Occurrence #** counts completions within the current period. Resets to 1 at the start of each new period for `Once per period` and `Exactly N per period`; continues incrementing for `Minimum N per period` and `Unlimited`.
- **Grace period** (Responsibilities only): if a task is still open more than N days past its due date, the bot cancels it and creates the next one.
- **Startup governance**: on every daemon start, the bot checks that each Active series has exactly one open task for the current period. Zero → creates one. Multiple → logs a warning for manual resolution.
- **Live monitoring**: the definitions database is polled alongside your task databases. Creating a new definition or toggling one to `Active` triggers governance within one poll cycle — no restart needed.
- **Deleted tasks** are handled by governance — if a recurring task is deleted, the startup check detects the missing open task and creates a replacement.

---

## Adding Your Own Automations

Open `automations.py`. Each automation is a plain Python function:

```python
def my_automation(client, page, prev_page) -> dict:
    # page     — the current state of the Notion page
    # prev_page — the state from the previous poll (or same as page on first sight)
    # Return a dict of Notion property updates, or {} to skip
    if some_condition:
        return {"My Field": {"number": 42}}
    return {}
```

Then add it to the `AUTOMATIONS` list at the bottom of the file.

See `DESIGN.md` for the full automation signature, governance system, and architecture details.

---

## Tuning Poll Interval

Set `poll_interval` in `config.toml` (applies to all databases).

- **60s** (default) — good balance, ~1440 API calls/day per database
- **30s** — more responsive, ~2880 calls/day
- Notion's free tier rate limit is 3 requests/second — you won't come close.

---

## How it compares to Notion's built-in automations

| Feature                   | This daemon                  | Notion built-in |
| ---------------------------| ------------------------------| -----------------|
| Trigger on change         | ✅ (within poll interval)     | ✅ (instant)     |
| Custom logic in Python    | ✅                            | ❌               |
| Recurring tasks           | ✅                            | ✅               |
| Outbound webhooks         | ✅ (add your own code)        | ✅ (paid plans)  |
| Always-on device required | ✅ (Raspberry Pi works great) | ❌               |
| Subscription required     | ❌                            | ✅               |

---

## Usage Guide

Tips for configurations that are valid but non-obvious. Useful once you're familiar with the features.

**`day_start_hour` — setting your logical day boundary:**
By default, the daemon treats 3am as the start of a new day. Any task completed between midnight and 3am is attributed to the *previous* calendar day's period. This prevents an accidental late-night close from counting toward the next day's quota. If you regularly work past 3am, increase this value (e.g. `day_start_hour = 4`). If you have an extreme schedule (e.g. sleep at 11am, wake at 6pm), set it to shortly after you wake up — periods will be labeled with the calendar date of your waking day. The same setting controls when the daily governance cron fires.

**`Minimum N per period` with Anchor Day:**
If all N repetitions of an activity happen at the same scheduled event (e.g. a weekly meeting, a class, a practice), `Minimum N per period` is probably not the right cadence. Use `Once per period` with Anchor Day set to that event's day instead. Completing the task means you showed up; how many times you did something *during* the event is detail that belongs in the task name or notes, not in N. `Minimum N per period` is better suited to activities spread across the period with no fixed day.

**Two recurring event days per period (e.g. Tuesday and Thursday):**
The system supports one Anchor Day per series definition. To track a recurring activity that happens on two specific days per week, create two separate definitions — one anchored to Tuesday, one to Thursday — each with `Once per period`. Using `Minimum 2 per period` with no anchor would give you a due date spanning the full period but would not enforce the specific days.

**Applying RTD config changes immediately:**
Changing an RTD's `Period`, `Cadence Type`, or `N Cadence` doesn't trigger governance right away — the change takes effect at the next daemon startup or daily governance cron. To apply it immediately without waiting: set the RTD's Status to inactive, wait one poll cycle (default 60s), then set it back to Active. The Status → Active transition triggers an immediate governance run that drift-corrects all affected tasks. A one-click Force Governance option is planned as part of the Automation Hub.

**Visual status indicator (formula field):**
Add a formula field to your task database to see task state at a glance. The formula below concatenates emoji based on task properties — useful as a first column in your board or list view.

Emoji key: ⏰ past due · 🧱 actively blocking an open task · 🛑 blocked by an open task · 🔁 recurring task · 🌿 has parent task · 🌳 has child tasks · 🤖 has bot note

```
if(
    and(
        prop("Status") != "Done",
        prop("Status") != "Cancelled"
        ),
        if(
            not empty(prop("Due Date")),
            if(
                or(
                    and( test(format(prop("Due Date")), "AM|PM"),prop("Due Date")<now()),
                    and( not test(format(prop("Due Date")), "AM|PM"),dateAdd(prop("Due Date"),1,"days") < now() )
            ),
            "⏰",
          ""
        ),
        ""
  ),
  ""
) + if(and(
            not empty(prop("Blocking")),
            prop("is Open")==true
            ),
            if(prop("Blocking").map(current.prop("is Open") == true).includes(true),
                "🧱",
                ""
            ),
    ""
) + if(and(
            not empty(prop("Blocked by")),
            prop("is Open") == true
            ),
            if(prop("Blocked by").map(current.prop("is Open") == true).includes(true),
                "🛑",
                ""
            ),
    ""
) + if(not empty(prop("Recurring Series")),
    "🔁",
    ""
) + if(not empty(prop("Parent Task")),
    "🌿",
    ""
) + if(not empty(prop("Child Task")),
    "🌳",
    ""
) + if(not empty(prop("Bot Notes")),
    "🤖",
    ""
)
```

Field names must match your database exactly. The `🧱` and `🛑` indicators depend on an `is Open` formula field (boolean) in your task database:

```
includes(["Not started", "Todo", "On hold", "In progress"], prop("Status"))
```

List only your open states here — the field name `is Open` should reflect that: it returns true only for tasks that are still in progress. Update the status option names to match your database. The overdue check handles both date-only and date+time fields: date+time compares directly to `now()`; date-only adds one day since a date with no time represents the whole day.

Note: Notion evaluates all branches of `and()`/`or()` — there is no short-circuit evaluation. Design formulas accordingly.

**Due Date sort helper (formula field):**
Add a formula field to your task database to sort tasks by Due Date in a way that keeps active tasks at the top. Helps sort tasks by Due Date: if a date range is active now, returns `now()` so the task sorts as a "today" task; if the range has ended, returns the end date; if Due Date is empty, returns a far-future date so the task sorts to the bottom.

```
if(
  empty(prop("Due Date")),
  parseDate("2999-01-01T00:01Z"),
  if(
    now() >= dateStart(prop("Due Date")) and now() <= dateEnd(prop("Due Date")),
    now(),
    if(
      now() > dateEnd(prop("Due Date")),
      dateEnd(prop("Due Date")),
      dateStart(prop("Due Date"))
    )
  )
)
```

---

## Updating
### Linux
1. Stop the service: `sudo systemctl stop notion-daemon`
2. Back up any files you've edited: `automations.py` and `config.toml` are the only ones you're expected to modify
3. Pull the latest changes: `git pull`
4. Install any new dependencies: `venv/bin/pip install -r requirements.txt`
   - This also updates [Notion_API](https://github.com/DreamShark-Bytes/Notion_API) if the pinned version changed. The release notes will call this out explicitly when it applies.
5. Apply any Notion-side changes listed in the release notes (field renames, new select options, new columns)
6. Update `config.toml` to match any new config format changes
7. Start the service: `sudo systemctl start notion-daemon`
8. Check logs to confirm governance ran cleanly: `journalctl -u notion-daemon -f`

To diff your local `automations.py` against the new version before overwriting: `git diff HEAD automations.py`

### Windows

1. Stop the service: `Stop-Service NotionAutomator`
2. Back up any files you've edited: `automations.py` and `config.toml` are the only ones you're expected to modify
3. Pull the latest changes: `git pull`
4. Install any new dependencies: `venv\Scripts\pip install -r requirements.txt`
   - This also updates [Notion_API](https://github.com/DreamShark-Bytes/Notion_API) if the pinned version changed. The release notes will call this out explicitly when it applies.
5. Apply any Notion-side changes listed in the release notes (field renames, new select options, new columns)
6. Update `config.toml` to match any new config format changes
7. Start the service: `Start-Service NotionAutomator`
8. Check logs to confirm governance ran cleanly: open `notion_daemon.log` in the project directory, or tail it in PowerShell: `Get-Content -Path .\notion_daemon.log -Tail 50 -Wait`

To diff your local `automations.py` against the new version before overwriting: `git diff HEAD automations.py`

---

## Credits

Developed in collaboration with [Claude Code](https://claude.ai/code) by Anthropic. All architectural decisions, requirements definition, design reviews, testing, and production deployment are owned by the human author. Claude assisted with implementation, debugging, and documentation under directed oversight — not vibe-coding, but a design-led workflow where nothing ships without human review and approval.

---

## Future Plans

See `PLANNED.md` for full details.

- **Automation Hub** — A single Notion page as the daemon's home base. Auto-creates required databases on first run, surfaces health and status information, and eventually replaces `config.toml` for behavioral settings that change frequently.
- **Notifications** — Outbound webhook support (Discord, Telegram) for alerts on governance events.
- **Change Tracking** — Opt-in field change log with old/new values and timestamps, feeding into reporting tools.
- **First Value Field Tracking** — Automatically stamp a `First [Field Name]` column with the first observed value of any configured field. Currently only recording `First Due Date`.
