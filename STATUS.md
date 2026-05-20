# Notion Automator — Status
_Last updated: May 19, 2026_

## Test Results
| ID      | Description                                                        | Status  |
| ---------| --------------------------------------------------------------------| ---------|
| A1      | Close triggers next task (Unlimited)                               | [P]     |
| A2      | Close triggers next task in next period (Once per period)          | [P]     |
| A3      | Inactive RTD (Status ≠ Active) — no new task                       | [ ]     |
| A4      | Task not linked to RTD — no action                                 | [P]     |
| A4b     | No duplicate if target period already has open task                | [P]     |
| A5      | Manually created task with missing bot fields gets initialized     | [P]     |
| B1      | Day period key uses local date                                     | [P]     |
| B2      | Week period key uses ISO week format                               | [P]     |
| B3      | Closed Date determines period boundary                             | [P]     |
| B4      | Pre-filled Closed Date respected on recurring task close           | [P]     |
| B5      | Period Key unchanged on unrelated property edit                    | [P]     |
| **B6**  | No Anchor Day: Due Date = end of period (not full span)            | **[P]** |
| C1      | First task of period gets Occurrence # = 1                         | [P]     |
| C2      | Second completion in same period increments count                  | [P]     |
| C3      | User edits Occurrence # — next task still uses COUNT               | [P]     |
| C4      | New period resets Occurrence # to 1                                | [P]     |
| C5      | Bad Habit: Occurrence # resets at period boundary                  | [P]     |
| D1      | Overdue Responsibility task is auto-cancelled                      | [P]     |
| D2      | Task within grace window is NOT cancelled                          | [P]     |
| D3      | Ignore Grace Period checkbox bypasses auto-cancel                  | [P]     |
| D4      | Stale period + 1 day into new period → auto-cancel                 | [P]     |
| D5      | Non-Responsibility type is NOT auto-cancelled                      | [P]     |
| D6      | No Due Date → no auto-cancel                                       | [P]     |
| E1–E4   | Bot Notes: duplicate name, multiple open tasks                     | [S]     |
| E5      | At-most-N cap exceeded → note on RTD                               | [P]     |
| **E6**  | Invalid Anchor Time format → Bot Note on RTD                       | **[P]** |
| E7      | Exactly N per period: N completions → next period task             | [P]     |
| **E8**  | Exactly N per period exceeded → Bot Note on RTD                    | **[P]** |
| F1      | Startup governance initializes missing fields                      | [P]     |
| F2      | RTD with zero open tasks → task created at startup                 | [P]     |
| **F2b** | Multiple open tasks in different periods preserved                 | **[P]** |
| **F2c** | Future-period open task does not block current-period creation     | **[P]** |
| F3–F4   | 2am cron timing                                                    | [S]     |
| G1–G4   | Closed Date stamp / reopen / pre-fill respected (all task types)   | [P]     |
| **G5**  | Governance backfills Closed Date for already-Complete tasks        | **[P]** |
| H1–H5   | Due Date Update Count and First Due Date                           | [P]     |
| **H6**  | Changing Due Date time only does NOT increment count               | **[P]** |
| **H7**  | Same as H6 with different date scenario                            | **[P]** |
| **I6**  | Optional task fields absent → bot skips writes without crashing    | **[P]** |
| I7      | Optional fields added mid-session require restart to write to them | [P]     |
| **I8**  | `closed_date` flag absent → no Closed Date stamped, no 400 errors  | **[P]** |
| **I9**  | `reopen_count` absent → Closed Date still works, count not written | **[P]** |
| **I10** | `due_date_tracking` absent → count/first-date never written        | **[P]** |
| **I11** | Recurring tasks + Closed Date column absent → CRITICAL logged      | **[P]** |
| I1      | RTD page deleted mid-run — daemon continues without crash          | [S]     |
| I2      | Notion API error during task creation                              | [S]     |
| **I3**  | Task linked to multiple RTDs — graceful handling                   | **[S]** |
| I4–I5   | First-sight init, unchanged-page skip                              | [S]     |
| **Z1**  | New/activated RTD creates task within one poll cycle (no restart)  | **[P]** |

---

## Deploy Prerequisites
- [Done] Rename Notion field: "Instance # (Recurring Task)" → "Occurrence # this Period (Recurring Task)"
- [ ] Rename RTD "Cadence Type" select option: "N per period" → "Exactly N per period"
- [ ] Rename RTD field: "Cadence N" → "N Cadence"
- [ ] Replace RTD "Active" checkbox with "Status" field (Status = "Active" to activate)
- [Done] Update `config.toml` to `[[databases]]` format
- [ ] Retest A3 against new Status field in production

---

## Priorities
_Ordered by importance._

3. **Bug: "Minimum N per period" period transition** — wrong due date on next task when minimum is met; governance should archive (not cancel) when minimum was met. See PLANNED.md.
4. **Pivot to Notion_PowerBI** — PC required for data connections; iPad exploration only for now.
5. **Automation Hub** — A single Notion page as the daemon home base: task database configs (checkboxes per flag), recurring tasks config, bot health dashboard. Requires `create_database()` in `notion_api.py`. Unblocks Notifications. (Formerly "Project Page" — see PLANNED.md.)
6. **Change Tracking** — Opt-in field change log (old/new value, page ID, timestamp). Storage format TBD. Feeds Notion_PowerBI.
7. **Timer / Mission Tracking** — Link closed tasks to mission areas for effort heatmap. Attribution method not yet decided — see PLANNED.md.
8. **Notifications** — Discord/Telegram webhooks via `notifiers.py`. Depends on Automation Hub for URL config.
9. **Clear Blocking/Blocked-By on Close** — confirm exact Notion field names before implementing — see PLANNED.md.
10. **First Value Field Tracking** — stamp `First [Field Name]` for any configured field — see PLANNED.md.
11. **Automated Testing** — unit tests for pure logic functions after feature set stabilizes — see PLANNED.md.

---

## Open Issues
_Things to file in the repository issue tracker._

- **timeout tuple** — `timeout=30` in notion_api.py could be `timeout=(10, 30)` (connect vs. read) for more precision; POST/PATCH timeout carries a silent-success risk if Notion processed the write but the response never arrived
- **Skipped tests** — E1–E4, F3–F4, I1–I5, I3 are marked [S]; decide which are worth automating or at least documenting as known gaps
- **`database_ids` removal** — no backward-compatible warning; old config silently errors; could log a helpful message pointing to config_example.toml

---

## Open Decisions
- **Automation Hub scope** — How much of `config.toml` moves into Notion? Does it replace the file entirely or live alongside it? Especially relevant for the `[[databases]]` automation flags.
