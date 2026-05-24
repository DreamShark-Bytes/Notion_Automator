# Notion Automator — Status
_Last updated: May 23, 2026_

## Project Info
**Version:** v1.0.1
**Open GitHub Issues:** #4 — "Minimum N per period" period transition bug

---

## Priorities
_Ordered by importance._

1. bug: daily due date off by 1
2. **Pivot (back) to Notion_PowerBI** — PC required for data connections; iPad exploration only for now.
4. **Automation Hub** — A single Notion page as the daemon home base: task database configs (checkboxes per flag), recurring tasks config, bot health dashboard. Requires `create_database()` in `notion_api.py`. Unblocks Notifications. (Formerly "Project Page" — see PLANNED.md.)
5. **Change Tracking** — Opt-in field change log (old/new value, page ID, timestamp). Storage format TBD. Feeds Notion_PowerBI.
6. **Timer / Mission Tracking** — Link closed tasks to mission areas for effort heatmap. Attribution method not yet decided — see PLANNED.md.
7. **Notifications** — Discord/Telegram webhooks via `notifiers.py`. Depends on Automation Hub for URL config.
8. **Clear Blocking/Blocked-By on Close** — confirm exact Notion field names before implementing — see PLANNED.md.
9. **First Value Field Tracking** — stamp `First [Field Name]` for any configured field — see PLANNED.md.
10. **Automated Testing** — unit tests for pure logic functions after feature set stabilizes — see PLANNED.md.

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
| P3  | Select/Multi-select fields not copied to new recurring tasks                         | Fixed               |
| P4  | Once-daily Responsibility created task with wrong due date (5/23 instead of 5/22)    | Fixed               |
| P5  | RTD Grace Period change (1→empty) not reflected in governance despite RTD monitoring | Fixed               |

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

### P5 Notes — RTD config changes not propagating (Fixed)
- Root cause: Z1 triggered governance on ANY RTD edit, including Grace Period changes — but governance fired before Notion propagated the edit, so it ran with the old value; snapshot then refreshed with new value; no re-trigger.
- Fix: `_poll_rtd_for_changes` now triggers governance only when Status transitions to Active. All other field edits update the snapshot only — they take effect at the next scheduled governance run (startup or 2am).
- Governance drift correction now covers all tasks in current+future periods (not open only), so field changes like Period, Cadence Type, N Cadence fully propagate on the next run.

---

## Open Issues
_Things to file in the repository issue tracker._

- **timeout tuple** — `timeout=30` in notion_api.py could be `timeout=(10, 30)` (connect vs. read) for more precision; POST/PATCH timeout carries a silent-success risk if Notion processed the write but the response never arrived
- **Skipped tests** — E1–E4, F3–F4, I1–I5, I3 are marked [S] in test plan; decide which are worth automating or at least documenting as known gaps
- **`database_ids` removal** — no backward-compatible warning; old config silently errors; could log a helpful message pointing to config_example.toml
- **P3** — Select/Multi-select fields not copied in recurring task inheritance (not yet filed)
- **P4** — Once-daily task due date off by one (not yet filed)
- **P5** — RTD config changes not reflected in governance (fixed this session — closes #6)

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