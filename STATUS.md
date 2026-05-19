# Notion Automator — Status
_Last updated: May 18, 2026_

## Test Results
| ID      | Description                                                        | Status  |
| ---------| --------------------------------------------------------------------| ---------|
| A1      | Close triggers next task (Unlimited)                               | [P]     |
| A2      | Close triggers next task in next period (Once per period)          | [P]     |
| A3      | Inactive RTD — no new task                                         | [P]     |
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
| **I8**  | `closed_date` flag absent → no Closed Date stamped, no 400 errors  | **[ ]** |
| **I9**  | `reopen_count` absent → Closed Date still works, count not written  | **[ ]** |
| **I10** | `due_date_tracking` absent → count/first-date never written        | **[ ]** |
| **I11** | Recurring tasks + Closed Date column absent → CRITICAL logged      | **[ ]** |
| I1      | RTD page deleted mid-run — daemon continues without crash          | [S]     |
| I2      | Notion API error during task creation                              | [S]     |
| **I3**  | Task linked to multiple RTDs — graceful handling                   | **[S]** |
| I4–I5   | First-sight init, unchanged-page skip                              | [S]     |
| **Z1**  | New active RTD creates task within one poll cycle (no restart)     | **[ ]** |

---

## Deploy Prerequisites
- [Done] Rename Notion field: "Instance # (Recurring Task)" → "Occurrence # this Period (Recurring Task)"
- [ ] Rename RTD "Cadence Type" select option: "N per period" → "Exactly N per period"
  - Legacy normalization code removed. Deploying before the rename causes "N per period" to be unrecognized.
- [ ] Update `config.toml` to `[[databases]]` format — **breaking change this session**
  - Old `database_ids = [...]` key is no longer read. Daemon will error on startup until updated.
  - See `config_example.toml` for the new format and required automation flags.

---

## Priorities
_Ordered by importance. Deploy is gated on items 1–3._

1. **Run I8–I11** — verify config-gated automation flags (no 400 errors, correct behavior when flags absent)
2. **Implement + test Z1** — RTD monitoring: new/activated RTDs create a task within one poll cycle. Design and plan already complete; implementation is in `daemon.py` only.
3. **Deploy** — rename "N per period" Notion option, update `config.toml`, ship.
4. **Project Page** — Notion page as daemon home base: auto-creates child databases, Notion-based config (eventually replaces `config.toml` automation flags), status dashboard. Requires `create_database()` in `notion_api.py`. Unblocks Notifications.
5. **RTD Series State (Habit Lifecycle)** — Replace Active checkbox with a Status field (Planned / Active / On Hold / Completed / Retired / Abandoned). Requires RTD monitoring (Z1) for real-time activation response.
6. **Notifications** — Discord/Telegram webhooks via `notifiers.py`. Depends on Project Page for URL config.
7. **Change Tracking** — Opt-in field change log (old/new value, page ID, timestamp). Storage format TBD. Feeds Notion_PowerBI.
8. **Timer / Mission Tracking** — Link closed tasks to mission areas for effort heatmap. Attribution method not yet decided — see PLANNED.md.

---

## Open Decisions
- **Project Page scope** — How much of `config.toml` moves into Notion? Does it replace the file entirely or live alongside it? Especially relevant for the `[[databases]]` automation flags added this session.
