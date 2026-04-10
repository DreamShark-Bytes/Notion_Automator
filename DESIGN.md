# Notion Automator — Design Document

> **Status:** Living document. Edit freely — implementation will follow this doc.
> Sections marked `[YOUR INPUT NEEDED]` are placeholders for you to fill in.

---

## 1. Purpose

Notion Automator is a background daemon that watches one or more Notion databases and applies rule-based automations whenever pages change. It runs on a schedule, compares each page against a stored snapshot of its previous state, and writes property updates back to Notion.

It is intentionally write-back only: the daemon reads from Notion and writes to Notion. It does not maintain a permanent local database (that is the job of `Notion_PowerBI`).

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────┐
│                   daemon.py                     │
│                                                 │
│  startup                                        │
│  ├── run_governance_pass()  ← all pages         │
│  │     └── run_automations_on_page(page, page)  │  ← governance only, pages discarded
│  └── set last_polled = startup_time             │
│                                                 │
│  loop (every POLL_INTERVAL seconds)             │
│  └── poll_database()  ← pages edited since last │
│        └── run_automations_on_page(page, prev)  │  ← change detection + governance
│              └── NotionClient.update_page_...   │  ← writes back to Notion
└─────────────────────────────────────────────────┘
```

---

## 3. Components

### 3.1 `daemon.py` — Orchestrator

| Function | Purpose |
|---|---|
| `run_governance_pass()` | Fetches ALL pages from a database on startup. Runs automations with `prev_page=page` (governance only, no change detection fires). Pages are discarded after processing — not stored in the snapshot. |
| `poll_database()` | Fetches only pages edited since the last poll (using Notion's `last_edited_time` filter). Runs full automations (governance + change detection). Returns updated snapshot. |
| `run_automations_on_page()` | Calls every registered automation function, collects their returned update dicts, and patches Notion if anything changed. Skips pages where `prev_page is None` (first time seeing a page after startup). |
| `_strip_files()` | Removes `files`-type properties from a page dict before storing in the in-memory snapshot. Keeps memory footprint clean; attachments are not needed for automation logic. |
| `_utcnow_iso()` | Returns current UTC time floored to the minute, matching Notion's `last_edited_time` resolution. |

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `NOTION_TOKEN` | *(required)* | Notion integration secret |
| `DATABASE_IDS` | *(required)* | Comma-separated Notion database IDs to watch |
| `POLL_INTERVAL` | `60` | Seconds between polls |

**CLI flags:**

| Flag | Description |
|---|---|
| `--debug` | Enables verbose API request/response logging. For manual runs only. |

---

### 3.2 `notion_client.py` — API Wrapper

Thin wrapper around the Notion REST API (version `2022-06-28`).

| Method | Description |
|---|---|
| `get_database(id)` | Returns database schema (property definitions, status groups, etc.) |
| `query_database(id, filter)` | Returns all pages, handling pagination automatically |
| `get_page(id)` | Returns a single page |
| `update_page_properties(id, props)` | PATCHes one or more properties on a page |

Static property builders (`date_property`, `number_property`, `rich_text_property`, `checkbox_property`) are convenience constructors for building Notion property update payloads.

---

### 3.3 `automations.py` — Automation Functions

Each automation is a plain Python function with this signature:

```python
def my_automation(client: NotionClient, page: dict, prev_page: dict | None) -> dict:
    ...
    return {"Property Name": <notion_property_value>}  # or {} to do nothing
```

| Parameter | Description |
|---|---|
| `client` | `NotionClient` instance — use if you need to call the Notion API (e.g. to look up schema). Use `_client` by Python convention if your function does not call the API. |
| `page` | Current page dict as returned by the Notion API |
| `prev_page` | Previous snapshot of the same page. During governance pass, this is the same object as `page` (so no field appears "changed"). |

Return a dict of Notion property updates. Return `{}` to make no changes. All automation return values are merged and applied in a single API call.

**To enable an automation**, add it to the `AUTOMATIONS` list. **To disable one**, remove it. The `_client` naming convention has no effect on dispatch — it is purely a Python style signal that the parameter is intentionally unused.

---

## 4. In-Memory Snapshot

The snapshot is a `dict[database_id, dict[page_id, page_dict]]` held in RAM. It starts **empty** on every daemon start and grows organically as pages are returned by polls.

**Why not pre-load at startup?**
- The startup governance pass only needs to read and write each page once, then discard it. There is no reason to keep all pages in RAM after that.
- Change detection only needs the previous state of pages that have actually changed since the last poll. In steady state, most pages in a large database are untouched.

**Why in-memory only?**
- Avoids stale state between restarts.
- The daemon's job is to react to *changes*; it doesn't need historical data (that is `Notion_PowerBI`'s job).

**Implications:**
- A daemon restart resets the snapshot. The startup governance pass re-applies missing field initialization.
- The first change to a page after a restart will have `prev_page=None` if the page was not returned by any prior poll. Change-detection automations will skip it; governance automations will still fire.
- Durable state that must survive restarts (e.g. "has this page ever had a due date?") is stored as a Notion property, not in the snapshot.

---

## 5. Governance System

Governance checks run on **every** page the daemon processes, including the startup pass. They are embedded in each automation function and fire independently of change detection.

The key pattern: during the startup governance pass, `prev_page=page` is passed to every automation. Change-detection logic compares `prev_page` to `page` — since they're the same object, nothing appears changed. Governance checks look only at the current page's field values (are they `None`? are they missing?) and are not affected by `prev_page`.

### Current governance rules

| Field | Rule |
|---|---|
| `Due Date Update Count` | If `None`, initialize to `0` |
| `First Due Date` | If empty and `Due Date` is set, stamp with current `Due Date` value. Does not increment the counter. |
| `Last Closed` | If status is in the `Complete` group and `Last Closed` is empty, backfill with `last_edited_time` |

---

## 6. Current Automations

### 6.1 `auto_last_closed`

**Trigger:** Status field transitions into the `Complete` status group.

**Action:** Stamps the `Last Closed` date property with the current UTC time.

**Governance:** If status is already `Complete` but `Last Closed` is empty, backfills with the page's `last_edited_time`.

**Notion fields required:**

| Field | Type |
|---|---|
| `Status` | Status |
| `Last Closed` | Date |

---

### 6.2 `auto_due_date_update_count`

**Trigger:** `Due Date` changes from one date to a different date.

**Does NOT fire when:**
- Due Date is set for the first time (stamps `First Due Date` instead)
- Due Date is cleared (set to empty)
- Due Date is unchanged

**Governance:**
- If `Due Date Update Count` is `None`, initialize to `0`
- If `First Due Date` is empty but `Due Date` is set, stamp `First Due Date` with the current `Due Date` value

**Notion fields required:**

| Field | Type |
|---|---|
| `Due Date` | Date |
| `Due Date Update Count` | Number |
| `First Due Date` | Date |

---

### 6.3 `auto_last_edited_note` *(disabled)*

**Trigger:** Any property on the page changes.

**Action:** Writes a human-readable UTC timestamp to a `Last Edited (Bot)` rich text field.

Disabled by default. Enable by adding it to the `AUTOMATIONS` list.

---

## 7. Planned Features

### 7.1 Recurring Tasks

`[YOUR INPUT NEEDED]`

> Describe the intended behaviour here. Suggested questions to answer:
> - What field(s) indicate a task is recurring? (e.g. a checkbox "Recurring", a select field with a recurrence interval, etc.)
> - When a task is closed, what fields should be copied to the new task?
> - What fields should be reset or recalculated on the new task? (e.g. Due Date offset from close date, Status reset to a default)
> - Should the original task be left as-is after the new one is created, or should it be archived/marked in some way?
> - Should there be any limit on how many recurrences can be created?

---

## 8. Adding a New Automation

1. Write a function in `automations.py` following the signature in §3.3
2. Add any governance checks inside the same function
3. Register it in `AUTOMATIONS` at the bottom of `automations.py`
4. Add the required Notion fields to your database if they don't exist

No changes to `daemon.py` or `notion_client.py` are needed for new automations.

---

## 9. Known Limitations

- **Notion's `last_edited_time` is floored to the minute.** Pages edited within the same minute as the poll window boundary may occasionally be missed or double-processed. In practice this is rare and self-correcting on the next poll.
- **No change history.** The daemon only knows "what it was last time I looked." If a field changes twice between polls, only the final value is seen. For a 60-second poll interval this is unlikely to matter.
- **Single-threaded.** Databases are polled sequentially, not in parallel. With multiple large databases the effective poll interval may be longer than `POLL_INTERVAL`.
- **No retry logic.** If a Notion API call fails, the error is logged and that page is skipped until the next poll.
- **File/attachment properties are excluded from the snapshot** but are still visible in Notion. Automations cannot act on file contents.
