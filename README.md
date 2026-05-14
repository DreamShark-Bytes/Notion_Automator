# Notion Automation Daemon

Replace Notion's paid automation features with a self-hosted polling daemon. Runs on any Linux system with systemd (Raspberry Pi, Ubuntu, Debian, etc.).

## Files

| File | Purpose |
|---|---|
| `daemon.py` | Polling loop / entry point |
| `automations.py` | Your automation rules (edit this!) |
| `recurring_tasks.py` | Recurring task automation logic and shared helpers |
| `notion-daemon.service` | systemd unit for auto-start on boot |
| `config.toml.example` | Config template — copy to `config.toml` and fill in |

---

## 1. Get a Notion Integration Token

1. Go to https://www.notion.so/my-integrations
2. Click **+ New integration** → give it a name → Submit
3. Copy the **Internal Integration Secret**
4. Open each Notion database you want to automate → `...` menu → **Add connections** → select your integration

---

## 2. Find Your Database ID

Open the database in Notion. The URL looks like:
```
https://www.notion.so/myworkspace/abc123def456...?v=...
```
The database ID is the UUID between your workspace name and `?v=`.

---

## 3. Install

```bash
git clone https://github.com/DreamShark-Bytes/Notion_Automator
cd Notion_Automator

# Install Notion_API (shared dependency — must be done first)
pip install git+https://github.com/DreamShark-Bytes/Notion_API.git

# Create virtual environment and install remaining dependencies
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Configure
cp config.toml.example config.toml
nano config.toml   # fill in your token and database IDs
```

---

## 4. Test It

```bash
venv/bin/python daemon.py
```

You should see polling logs every 60 seconds. Make a change in Notion and watch it react.

---

## 5. Run as a System Service (auto-start on boot)

Edit the service file **before** copying it — replace both placeholder values:

| Placeholder | Replace with |
|---|---|
| `YOUR_USER` | your Linux username (e.g. `vince`) |
| `/path/to/Notion_Automator` | absolute path to this repo (e.g. `/home/vince/Documents/Notion_Automator`) |

```bash
# Edit placeholders
nano notion-daemon.service

# Copy to systemd and enable
sudo cp notion-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable notion-daemon
sudo systemctl start notion-daemon

# Check status / logs
sudo systemctl status notion-daemon
journalctl -u notion-daemon -f
```

---

## 6. Recurring Tasks

Automatically creates a new task whenever a recurring task is completed or cancelled, keeping one open task per series at all times.

### 6.1 Create the Definitions Database in Notion

Create a new database in Notion with these fields:

| Field | Type | Notes |
|---|---|---|
| Name | Title | |
| Type | Select | `Habit`, `Responsibility` |
| Active | Checkbox | Uncheck to pause a series without deleting it |
| Cadence Type | Select | `Once per period`, `N per period`, `Minimum N per period`, `Unlimited` |
| Cadence N | Number | Used by `N per period` and `Minimum N per period`; blank for others |
| Period | Select | `Day`, `Week`, `Month`, `Year` |
| Anchor Day | Number | Mon=1 … Sun=7 for weekly; 1–31 for monthly (overflows to last day of month) |
| Anchor Time | Text | e.g. `13:00`; blank = no specific time |
| Grace Period (days) | Number | Responsibilities only — auto-cancelled this many days past due; blank = never |
| Notes | Rich Text | |
| Last Completed | Rollup | Max of `Last Closed` from related tasks |

Then add these fields to your **main tasks database**:

| Field | Type | Notes |
|---|---|---|
| Recurring Series | Relation | Points to the Definitions database |
| Instance # | Number | Filled in by the bot at task creation |
| Period Key | Text | Internal — used by the bot to track period boundaries |
| Period Target | Text | e.g. `Minimum 3 per Week` — set by the bot at creation |

Connect your integration to both databases (`...` menu → **Add connections**).

### 6.2 Configure

In `config.toml`, add:

```toml
[recurring_tasks]
enabled = true
definitions_db_id = "your-definitions-database-id"
tasks_db_id = "your-main-tasks-database-id"
```

### 6.3 How it works

- **One open task per series at all times.** When a task is marked Done or Cancelled, the bot creates the next one automatically.
- **Due dates** are calculated from Anchor Day and Anchor Time. Without an anchor, the full period span is used (e.g. April 1 → April 30 for a monthly task).
- **Instance #** counts occurrences. Resets to 1 at the start of each new period for `Once per period` and `N per period`; continues incrementing for `Minimum N per period` and `Unlimited` until the period rolls over.
- **Grace period** (Responsibilities only): if a task is still open more than N days past its due date, the bot cancels it and creates the next one.
- **Startup governance**: on every daemon start, the bot checks that each active definition has exactly one open task. Zero → creates one. Multiple → logs a warning for manual resolution.
- **Deleted tasks** are handled by governance — if a recurring task is deleted, the startup check will detect the missing open task and create a replacement.

---

## 7. Adding Your Own Automations

Open `automations.py`. Each automation is a plain function (shared helpers are in `recurring_tasks.py`):

```python
def my_automation(client, page, prev_page) -> dict:
    # Return a dict of Notion property updates, or {} to skip
    if some_condition:
        return {"My Field": {"number": 42}}
    return {}
```

Then add it to the `AUTOMATIONS` list at the bottom of the file.

See `DESIGN.md` for the full automation signature, governance system, and architecture details.

---

## Tuning Poll Interval

Set `poll_interval` in `config.toml`.

- **60s** (default) — good balance, ~1440 API calls/day per database
- **30s** — more responsive, ~2880 calls/day
- Notion's free tier rate limit is 3 requests/second — you won't come close.

---

## Limitations vs. Notion's Native Automations

| Feature | This daemon | Notion paid |
|---|---|---|
| Trigger on change | ✅ (within poll interval) | ✅ (instant) |
| Update fields | ✅ | ✅ |
| Recurring tasks | ✅ | ✅ |
| Send Slack/email | ✅ (add your own code) | ✅ |
| Webhook / instant | ❌ (polling only) | ✅ |
| Always-on device needed | ✅ | ❌ |

---

## Future Plans

See `DESIGN.md` for full details on planned features.

- **Project Page** — A single Notion page per project serving as its home base. The daemon will auto-detect first-run and create any required databases (e.g. the Recurring Task Definitions database) as children of the Project Page. Users can move them anywhere afterwards — page IDs are permanent. The Project Page will also surface status/health information and Notion-based configuration for settings that change frequently.
