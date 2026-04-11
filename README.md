# Notion Automation Daemon

Replace Notion's paid automation features with a self-hosted polling daemon. Runs on any Linux system with systemd (Raspberry Pi, Ubuntu, Debian, etc.).

## Files

| File | Purpose |
|---|---|
| `daemon.py` | Polling loop / entry point |
| `automations.py` | Your automation rules (edit this!) |
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
| Send Slack/email | ✅ (add your own code) | ✅ |
| Webhook / instant | ❌ (polling only) | ✅ |
| Always-on device needed | ✅ | ❌ |
