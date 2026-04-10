# Notion Automation Daemon (Raspberry Pi)

Replace Notion's paid automation features with a self-hosted polling daemon.

## Files

| File | Purpose |
|---|---|
| `notion_client.py` | Low-level Notion API wrapper |
| `automations.py` | Your automation rules (edit this!) |
| `daemon.py` | Polling loop / entry point |
| `.env.example` | Config template |
| `notion-daemon.service` | systemd unit for auto-start on boot |

---

## 1. Get a Notion Integration Token

1. Go to https://www.notion.so/my-integrations
2. Click **+ New integration** → give it a name → Submit
3. Copy the **Internal Integration Secret** (starts with `secret_`)
4. Open each Notion database you want to automate → `...` menu → **Add connections** → select your integration

---

## 2. Find Your Database ID

Open the database in Notion. The URL looks like:
```
https://www.notion.so/myworkspace/abc123def456...?v=...
```
The database ID is the UUID between your workspace name and `?v=`.

---

## 3. Set Up on Raspberry Pi

```bash
# Clone / copy files to your Pi
mkdir ~/notion_automator && cd ~/notion_automator
# (copy all .py files here)

# Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install requests python-dotenv

# Configure
cp .env.example .env
nano .env   # fill in your NOTION_TOKEN and DATABASE_IDS
```

---

## 4. Test It

```bash
source venv/bin/activate
python daemon.py
```

You should see polling logs every 60 seconds. Make a change in Notion and watch it react.

---

## 5. Run as a System Service (auto-start on boot)

```bash
# Copy the service file
sudo cp notion-daemon.service /etc/systemd/system/

# Edit the paths if your username isn't 'pi'
sudo nano /etc/systemd/system/notion-daemon.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable notion-daemon
sudo systemctl start notion-daemon

# Check status / logs
sudo systemctl status notion-daemon
journalctl -u notion-daemon -f
```

---

## 6. Adding Your Own Automations

Open `automations.py`. Each automation is a plain function:

```python
def my_automation(client, page, prev_page) -> dict:
    # Return a dict of Notion property updates, or {} to skip
    if some_condition:
        return {"My Field": {"number": 42}}
    return {}
```

Then add it to the `AUTOMATIONS` list at the bottom of the file.

### Available helpers in automations.py

| Helper | Returns |
|---|---|
| `_get_select(page, "Field")` | string or None |
| `_get_date(page, "Field")` | ISO date string or None |
| `_get_number(page, "Field")` | float or None |
| `_now_iso()` | current UTC ISO timestamp |

### Property update formats

```python
# Date
{"Due Date": {"date": {"start": "2025-01-01"}}}

# Number
{"Count": {"number": 5}}

# Rich text
{"Notes": {"rich_text": [{"type": "text", "text": {"content": "hello"}}]}}

# Checkbox
{"Done": {"checkbox": True}}

# Select
{"Status": {"select": {"name": "In Progress"}}}
```

---

## Tuning Poll Interval

- **60s** (default) — good balance, ~1440 API calls/day per database
- **30s** — more responsive, 2880 calls/day
- Notion's free tier rate limit is 3 requests/second — you won't come close.

---

## Limitations vs. Notion's Native Automations

| Feature | This daemon | Notion paid |
|---|---|---|
| Trigger on change | ✅ (within poll interval) | ✅ (instant) |
| Update fields | ✅ | ✅ |
| Send Slack/email | ✅ (add your own code) | ✅ |
| Webhook / instant | ❌ (polling only) | ✅ |
| Always-on device needed | ✅ (your Pi) | ❌ |
