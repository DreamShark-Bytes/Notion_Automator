# Initial setup

This page takes you from nothing to a running Notion Automator that reads its configuration from a Notion page (the Configuration Hub). The README has the same steps with more detail on each feature; this page is the shortest path in the right order. Upgrading an existing install is a different page: `docs/upgrade_steps.md`.

## 1. Create a Notion integration

1. Open https://www.notion.so/my-integrations and choose **+ New integration**. Give it a name and submit.
2. Copy the **Internal Integration Secret**. It is your token. Treat it like a password.

## 2. Install the program

Linux (Raspberry Pi, Debian, Ubuntu):

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/DreamShark-Bytes/Notion_Automator
cd Notion_Automator
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Windows (Python 3.11 or newer, with "Add Python to PATH" ticked at install):

```powershell
git clone https://github.com/DreamShark-Bytes/Notion_Automator
cd Notion_Automator
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

## 3. Put the token in its own file

Copy `config_secrets_example.toml` to `config_secrets.toml` (same folder) and replace the placeholder with your token. This file is never committed. On Linux restrict it with `chmod 600 config_secrets.toml`; on Windows `install-service.ps1` does it for you when you install the service.

## 4. Create the config data source in Notion

The data source is where your configuration lives: one row per config, with the settings written as TOML in a code block in the row's page body. You create it yourself; the program never creates it.

1. Duplicate the template page (link to be added with the first release). If you build it by hand, create a database with these columns:

| Column | Type | Needed |
|---|---|---|
| Name | Title | yes |
| Status | Status, with an option named `Active` | yes |
| Errors | Text | yes (the bot writes problems here) |
| Warnings | Text | yes |
| Config Version (Datetime) | Date | optional, recommended |
| Report | Text | optional, recommended |
| Last verified time | Date | optional |
| Apply Config | Button | optional |

2. Share the database with your integration: open it, `...` menu, **Connections**, add your integration. Do the same for every task database you want automated.
3. Get the data source ID: in the Notion app open the data source, three-dots menu (top right), **More**, **Developer**, **Copy data source ID**. Use the data source ID, not the database ID.

## 5. Point the program at it

Copy `config_example.toml` to `config.toml` and set one line:

```toml
config_hub_data_source_id = "paste-the-data-source-id-here"
```

All other settings in that file are optional and explained in comments.

## 6. First start

```bash
venv/bin/python daemon.py          # Linux
venv\Scripts\python daemon.py      # Windows
```

The log should show `Hub ready` and then that the Hub created a row named **Automator** in your config data source. That row is inert: its Status is not Active and its code block is a commented-out example.

## 7. Write your config

1. Open the **Automator** row. Replace the contents of its code block with your settings. `docs/config_row_example.toml` lists every key with a comment on each; the smallest useful config is:

```toml
[[property_automations]]
name = "tasks"
data_source_id = "paste-your-task-data-source-id-here"
closed_date_property = "Closed Date"
```

2. Set the row's **Status** to **Active**. Within one poll (60 seconds by default) the log shows `Hub config read` and a governance pass, and the **Report** column fills in.

## 8. Changing the config later

Edit the code block, then tell the program to load it by setting **Config Version (Datetime)** to now. Typing a time by hand works; a button is easier:

1. In the config data source add a **Button** property named `Apply Config`.
2. Edit its action: **Edit property**, choose `Config Version (Datetime)`, set it to **Now**.
3. After each change, press **Apply Config** on the row.

What the program tells you, in the row:

- **Report**: one line for the app version and when it started, then one each for the last config read and the last governance pass. A yellow line means look at Warnings; a red line means look at Errors.
- **Warnings**: something is wrong but a working config is still running (for example an edit you have not applied, or an apply that failed).
- **Errors**: the program has no valid config loaded for the row, so nothing from it is running.

To pause everything a row controls, set its Status to anything other than **Active**. Set it back to **Active** to resume. No restart is needed for any of this.

## 9. Run it all the time

Install it as a service so it starts on boot: README section "6. Run as a System Service".
