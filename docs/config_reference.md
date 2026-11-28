# Config reference

Every key you can set. For a walk-through see `initial_setup.md`; for a commented example of a config row see [`docs/config_row_example.toml` (docs folder)](config_row_example.toml). The other two example files are in the project folder, one level up: [`config_example.toml` (project folder)](../config_example.toml) for `config.toml` and [`config_secrets_example.toml` (project folder)](../config_secrets_example.toml) for the token file.

## Where each setting lives

| Place | What goes there |
|---|---|
| `config.toml` | How the daemon runs: where the Hub is, polling, time settings. Never secrets |
| `config_secrets.toml` | The Notion token (`token`) and, for the live test only, `sandbox_token` |
| A config row's code block, or `config_automations.toml` | Which data sources are automated and how: the keys in the two tables below |

## config.toml

| Key | Default | What it does |
|---|---|---|
| `config_hub_data_source_id` | not set | The data source holding the config rows. Leave it out to run without the Hub, from `config_automations.toml` |
| `poll_interval` | 60 | Seconds between polls |
| `day_start_hour` | 3 | Hour (0 to 23) when your logical day begins; also when the daily governance pass runs |
| `week_start` | `"Sunday"` | First day of the week, any full day name |
| `notion_version` | `"2026-03-11"` | Notion API version sent with every request |
| `hub_settle_seconds` | 300 | Seconds a config row must stay unchanged before the bot acts on an edit |
| `hub_max_recreations` | 2 | How many times in a row a deleted config row is recreated before the bot stops |
| `sandbox_page_id` | not set | Only for `tests/live_sandbox.py`: the page that holds throwaway test data |

A value the daemon cannot use falls back to its default and shows as a Warning on the config row (source `config.toml`). The Warning follows the file: save `config.toml` after fixing the value and the Warning clears within one poll, with no restart. The values themselves (poll interval, day start and so on) are still read only at startup, so a restart is what applies a changed value. The old `[[databases]]` and `[recurring_tasks]` tables and `non_completion_statuses` are no longer read.

## `[[property_automations]]`: one block per task data source

| Key | Needs | What it does |
|---|---|---|
| `data_source_id` | required | The task data source this block configures |
| `name` | | A label for you; it appears in messages |
| `closed_date_property` | | A Date property the bot stamps when a task is completed. Leave it out to switch Closed Date stamping off |
| `reopen_count_property` | `closed_date_property` | A Number property that counts how often a task is reopened |
| `first_value_properties` | | Properties whose first value is saved once, in `First <name>` |
| `update_count_properties` | | Properties whose changes are counted, in `<name> Update Count` |

## `[[recurring_tasks]]`: one block per pair of recurring task definitions and the tasks they create

| Key | Default | What it does |
|---|---|---|
| `definitions_data_source_id` | required | The data source holding the recurring task definitions |
| `tasks_data_source_id` | required | The data source the tasks are created in |
| `non_completion_statuses` | built-in list | Statuses that count as a failed or skipped attempt, not a completion |
| `inherit_include_properties` | `[]` (copy nothing) | Properties copied from the closed task to the next one: a list of names, or the string `"all"`. A list is required even for one name (`["Priority"]`). A property literally named `all` is written `["all"]` |
| `inherit_exclude_properties` | `[]` | Properties not copied. Only has an effect with `inherit_include_properties = "all"`; with a list it is a Warning |
| `inherit_icon` | `true` | Copy the definition's icon to each created task |

Properties that cannot be copied are never copied, whatever you list: formula, rollup, created and last-edited time and user, unique ID, verification, button and files properties, the bot-managed columns (Name, Status, Due Date, Closed Date, Reopen Count, the Period and Occurrence columns and the relationship columns) and the closed-date and reopen-count columns of the pair. A name that is not on the data source, or cannot be copied, is a Warning and is ignored.

Leave a key out to switch that feature off. To switch a whole block off, put `#` in front of every line of it.

A row may hold several `[[recurring_tasks]]` blocks. A data source is in exactly one block, in exactly one role: a definitions data source in one block only, a tasks data source in one block only, and never one data source as both. Blocks that share a data source are all disabled, each with an Error naming the ID. The old single `[recurring_tasks]` table (one pair, single brackets) is an Error; change it to `[[recurring_tasks]]`. Messages about a block start with `recurring_tasks` and its definitions data source ID.

## Columns on the config data source

| Column | Type | Needed | Written by |
|---|---|---|---|
| Name | Title | yes | you |
| Status | Status | yes, with an option named `Active` | you |
| Errors | Text | yes | the bot |
| Warnings | Text | yes | the bot |
| Config Version (Datetime) | Date | optional | you (an Apply Config button sets it to now) |
| Report | Text | optional | the bot |
| Last verified time | Date | optional | the bot |

## What the Report, Warnings and Errors say

Each column is a numbered list, one message per line: `1. [source] header: text. Fix: what to do.` The source in brackets is colored (red in Errors, orange in Warnings), the header (the words before the first colon, such as `property_automations #1` or `recurring_tasks <definitions data source ID>`) is bold and underlined, and key words (config keys, `[[blocks]]` and quoted names) are bold. A check that finds several names reports them in one message. Notion allows 100 text objects in one column value, so a long list ends with a line `(N more, see the log)`; the log always has every message.

| Source | Where it comes from |
|---|---|
| `[config]` | Reading and applying the code block: TOML errors, bad IDs, unknown keys, an apply that failed while an older config keeps running |
| `[config-edit]` | The code block was edited but Config Version was not changed |
| `[config.toml]` | A value in `config.toml` that could not be used |
| `[governance]` | A governance pass failed |
| `[inheritance]` | The copy settings name properties that are missing or cannot be copied |
| `[recreation]` | The config row was deleted and a new one was created; it links every earlier row |

The Report column has one line each: `App: Version X, Started: time`, then `config.toml`, `Config`, `Governance`. A line starts with 🟢 (fine), 🟡 (see Warnings or an edit is being waited on), 🔴 (see Errors) or ⚪ (the row is not Active, nothing runs).
