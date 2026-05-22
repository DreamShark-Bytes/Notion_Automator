# Notion Automator — Status
_Last updated: May 22, 2026_

## Project Info
**Version:** v1.0.1
**Open GitHub Issues:** #4 — "Minimum N per period" period transition bug

---

## Priorities
_Ordered by importance._

1. bug: daily due date off by 1
2. bug: field inheritance
3. **Bug: "Minimum N per period" period transition** — wrong due date on next task when minimum is met; governance should archive (not cancel) when minimum was met. See PLANNED.md.
4. 4. **Pivot (back) to Notion_PowerBI** — PC required for data connections; iPad exploration only for now.
5. **Automation Hub** — A single Notion page as the daemon home base: task database configs (checkboxes per flag), recurring tasks config, bot health dashboard. Requires `create_database()` in `notion_api.py`. Unblocks Notifications. (Formerly "Project Page" — see PLANNED.md.)
6. **Change Tracking** — Opt-in field change log (old/new value, page ID, timestamp). Storage format TBD. Feeds Notion_PowerBI.
7. **Timer / Mission Tracking** — Link closed tasks to mission areas for effort heatmap. Attribution method not yet decided — see PLANNED.md.
8. **Notifications** — Discord/Telegram webhooks via `notifiers.py`. Depends on Automation Hub for URL config.
9. **Clear Blocking/Blocked-By on Close** — confirm exact Notion field names before implementing — see PLANNED.md.
10. **First Value Field Tracking** — stamp `First [Field Name]` for any configured field — see PLANNED.md.
11. **Automated Testing** — unit tests for pure logic functions after feature set stabilizes — see PLANNED.md.

---

## Test Results
_Only tests from current session or currently pending. Full test history in `tests/manual_test_plan.md`._

| ID      | Description                                                        | Status  |
| ---------| --------------------------------------------------------------------| ---------|
| B2      | Week period key uses W-YYYY-MM-DD format (date of week-start day)  | [P]     |
| **P4**  | Minimum N=0 governance: creates task for current period, not next  | **[ ]** |
| **I8**  | `closed_date` flag absent → no Closed Date stamped, no 400 errors  | **[P]** |
| **I9**  | `reopen_count` absent → Closed Date still works, count not written | **[P]** |
| **I10** | `due_date_tracking` absent → count/first-date never written        | **[P]** |
| **I11** | Recurring tasks + Closed Date column absent → CRITICAL logged      | **[P]** |
| **Z1**  | New/activated RTD creates task within one poll cycle (no restart)  | **[P]** |

---

## Production Bugs

| ID  | Description                                                                          | Status              |
| -----| --------------------------------------------------------------------------------------| ---------------------|
| P3  | Select/Multi-select fields not copied to new recurring tasks                         | Needs investigation |
| P4  | Once-daily Responsibility created task with wrong due date (5/23 instead of 5/22)    | Fixed               |
| P5  | RTD Grace Period change (1→empty) not reflected in governance despite RTD monitoring | Needs fix           |

### P3 Notes — field inheritance
- `FIELDS_NOT_INHERITED` is a blacklist (skip these, copy everything else)
- `_copy_inherited_props()` also skips read-only prop types via `_READONLY_PROP_TYPES`
- Select/multi-select are NOT read-only — investigate whether they're being silently skipped due to a normalization or build error in `_copy_inherited_props`
- Fields to test: relation, text, checkbox (not yet tested)
- Proposed fix: replace `FIELDS_NOT_INHERITED` with `fields_inheritance_list_is_inclusive: bool` + `inheritance_fields: list` — see PLANNED.md

### P4 Notes — daily due date off by one
- Was correct 5/19–5/21, broke on 5/22 (created task due 5/23 instead of 5/22)
- Occurrence # = 1 on all tasks (correct for Once per period)
- Investigate `_calc_due_date` for daily period — possible off-by-one at a day boundary

### P5 Notes — RTD config changes not propagating
- Governance re-fetches RTDs fresh from Notion API each run — no explicit cache
- Likely race: Z1 detects change → triggers governance immediately → Notion API hasn't propagated the edit yet → governance runs with old value → snapshot refreshes with new value → next poll sees no change → no second governance run
- Fix options: (a) add a short delay before governance when triggered by Z1; (b) schedule a follow-up governance run one poll cycle later; (c) re-fetch RTD individually just before using its values in governance

---

## Open Issues
_Things to file in the repository issue tracker._

- **timeout tuple** — `timeout=30` in notion_api.py could be `timeout=(10, 30)` (connect vs. read) for more precision; POST/PATCH timeout carries a silent-success risk if Notion processed the write but the response never arrived
- **Skipped tests** — E1–E4, F3–F4, I1–I5, I3 are marked [S] in test plan; decide which are worth automating or at least documenting as known gaps
- **`database_ids` removal** — no backward-compatible warning; old config silently errors; could log a helpful message pointing to config_example.toml
- **P3** — Select/Multi-select fields not copied in recurring task inheritance (not yet filed)
- **P4** — Once-daily task due date off by one (not yet filed)
- **P5** — RTD config changes not reflected in governance (not yet filed)

---

## Deploy Prerequisites
- [Done] Rename Notion field: "Instance # (Recurring Task)" → "Occurrence # this Period (Recurring Task)"
- [Done] Rename RTD "Cadence Type" select option: "N per period" → "Exactly N per period"
- [Done] Rename RTD field: "Cadence N" → "N Cadence"
- [Done] Replace RTD "Active" checkbox with "Status" field (Status = "Active" to activate)
- [Done] Update `config.toml` to `[[databases]]` format
- [Done] Retest A3 against new Status field in production
- [ ] Add `week_start = "Monday"` to `config.toml` under `[recurring_tasks]` (or omit to default to Monday). Governance will auto-update Period Key format on next run — no manual field clearing needed.

---

## Open Decisions
- **Automation Hub scope** — How much of `config.toml` moves into Notion? Does it replace the file entirely or live alongside it? Especially relevant for the `[[databases]]` automation fla