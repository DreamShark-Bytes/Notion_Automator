# Manual Test Plan — Notion Automator

Tests are grouped by feature area. Each test is performed against a live Notion workspace
using the daemon in its normal polling mode unless noted as "GOVERNANCE" (requires waiting
for the 2am cron or restarting the daemon to trigger the governance pass).

**Status legend:** `[ ]` not run · `[P]` pass · `[F]` fail · `[S]` skip

## Tabe of Contents
- A. [Recurring Task Creation on Close](#a--recurring-task-creation-on-close)
- B. [Period Key and Period Detection](#b--period-key-and-period-detection)
- C. [Occurrence # this Period Counting](#c--occurrence--this-period-counting)
- D. [Grace Period Auto-Cancel (GOVERNANCE / 2am cron)](#d--grace-period-auto-cancel-governance--2am-cron)
- E. [Bot Notes](#e--bot-notes)
- F. [GOVERNANCE Pass (Startup and 2am Cron)](#f--governance-pass-startup-and-2am-cron)
- G. [Closed Date and Reopen Count (auto_closed_date)](#g--closed-date-and-reopen-count-auto_closed_date)
- H. [Due Date Update Count and First Due Date (auto_due_date_update_count)](#h--due-date-update-count-and-first-due-date-auto_due_date_update_count)
- I. [Edge Cases and Error Handling](#i--edge-cases-and-error-handling)
- J. [Tests found by the developer that were not created by Claude](#j--tests-found-by-the-developer-that-were-not-created-by-claude)

---

## A — Recurring Task Creation on Close

### A1 — Close triggers next task (same period, Unlimited)
**Setup:** RTD active, Cadence Type = "Unlimited", open task exists linked to it.  
**Action:** Set task Status → Done.  
**Expected:** New task created in same period; Occurrence # = (count of existing tasks this period) + 1; Period Key matches current period.  
**Status:** `[P]`

---

### A2 — Close triggers next task in NEXT period (Once per period)
**Setup:** RTD active, Cadence Type = "Once per period", open task exists.  
**Action:** Set task Status → Done.  
**Expected:** New task created; Due Date falls in the NEXT period (not the current one).  
**Status:** `[P]`

---

### A3 — Inactive RTD — no new task
**Setup:** RTD exists with Status ≠ "Active" (e.g. "On Hold" or any non-Active value). Task linked to it.  
**Action:** Set task Status → Done.  
**Expected:** No new task created.  
**Status:** `[P]`

---

### A4 — Task not linked to any RTD — no action
**Setup:** Normal task with no "Recurring Series" relation.  
**Action:** Set task Status → Done.  
**Expected:** `auto_recurring_tasks` returns `{}`. No new task. No errors.  
**Status:** `[P]`

---

### A4b — Completing a task does not create a duplicate if target period already has an open task
**Setup:** RTD with Cadence Type = "Once per period", Period = "Month". One open task exists for NEXT month (user pre-created it). Close the current-month task.  
**Action:** Set current-month task Status → Done.  
**Expected:** No new task created — the next-month task already exists. Log shows "open task already exists for period …". Occurrence # on the next-month task is unchanged.  
**Status:** `[P]`

---

### A5 — Manually created task with missing bot fields gets initialized
**Setup:** Create a task manually in Notion and link it to an active RTD. Leave Period Key and Occurrence # blank.  
**Action:** Wait for the next daemon poll (or trigger a governance pass by restarting).  
**Expected:** Period Key, Occurrence #, Period Target, and Due Date are all stamped on the task. Occurrence # = (count of tasks in current period) + 1.  
**Status:** `[P]`

---

## B — Period Key and Period Detection

### B1 — Day period key uses local date, not UTC
**Setup:** Server is in a non-UTC timezone (e.g. UTC+2). It is 23:30 local (21:30 UTC).  
**Action:** Complete a recurring task with Period = "Day".  (where Due Date is generated. Type=Responsibility, Cadence Type!=Unlimited)
**Expected:** The new task's Period Key matches TODAY's local date (e.g. "2026-04-21"), not yesterday's UTC date.  
**Status:** `[P]`  
**Status Note:** Requires verifying server timezone is configured correctly.
**Testing Notes:** Had to wait until after 7pm Central for the datetime to flip over in UTC to the next day. Not only did it create the new task for tomorrow (Cadence Type=N per period, Cadence N = 4, Period=Day), it labelled it as Occurrence #=2, and the next task created had a Due Date of the NEXT day, not SAME day (despite being the first Responsibility/Task closed today)  
**Fix implemented (2026-05-12):** Root cause was NOT system timezone config — both Pi and laptop confirmed CDT. The actual bug: `_now_iso()` returns UTC, and `last_edited_time` from Notion is always UTC; both were stored directly into Closed Date. After 7pm CDT (= midnight UTC) this stamps the wrong calendar day. Fixed by adding `_now_local_date()` to `recurring_tasks.py` and replacing both the live close stamp and the `last_edited_time` backfill in `auto_closed_date`. Closed Dates are now always stored as local YYYY-MM-DD strings. Also fixes G5. Re-test needed.

---

### B2 — Week period key uses W-YYYY-MM-DD format (date of week-start day)
**Setup:** RTD with Period = "Week".  
**Action:** Complete a recurring task on any day.  
**Expected:** New task's Period Key = "W-YYYY-MM-DD" where the date is the week-start day (e.g. "W-2026-05-18" for a Monday-start week containing May 22).  
**Note:** Format changed from ISO `YYYY-Www` when `week_start` became configurable. Governance auto-corrects old-format keys on the next run.  
**Status:** `[P]`

---

### B3 — Closed Date (not Period Key field) determines period boundary
**Setup:** RTD with Period = "Month", Cadence Type = "Once per period". Open task's Period Key field was manually edited to say last month's key.  
**Action:** Complete the task. Task's Closed Date gets stamped as now (this month).  
**Expected:** New task Due Date targets NEXT month (because `_create_next_task` detected the closed task's Closed Date is in the current month, meaning current period was already covered).  
**Status:** `[P]`  
**Note:** Tests that Closed Date is used as ground truth, not the Period Key field.
**Testing Notes:** New task created but other issues came up. See sections Z5 and Z6 


---

### B4 — User pre-fills Closed Date on a recurring task before closing
**Setup:** Recurring task. User manually sets Closed Date to a date outside the current period (e.g. last month or a far-future year), then sets Status → Done.  
**Expected:**  
(1) Closed Date is NOT overwritten — the pre-filled date is preserved by `auto_closed_date`.  
(2) `_create_next_task` fetches all tasks for the RTD from the API, then upserts the just-closed task (with its pre-filled Closed Date) so `fetched_tasks` fully reflects its final state.  
(3) `_task_in_period` uses the Closed Date to attribute the closed task to its actual period. A date from last month or 2078 is NOT attributed to the current period.  
(4) The new task's target period is determined solely by counting completions in the CURRENT period from `fetched_tasks` — NOT from the pre-filled date directly.  
— If the pre-filled date is outside the current period: the closed task is not counted toward the current period. If no other completions exist this period, new task targets the CURRENT period.  
— If the pre-filled date is in the current period: the closed task counts toward the threshold normally (same behavior as no pre-fill).  
The pre-filled date has no direct influence on where the new task goes; it only affects which period the closed task is attributed to.  
**Status:** `[P]`
**Testing Notes:** Changing the Close date, letting the polling take place, and then closing a task DOES change the Closed Date AND edits the Reopen Count. Recurring tasks should NOT be touching the Closed Date, that should be exclusive to automations.py script, function: auto_closed_date() We should include that functionality as a requirement for recurring_tasks.py though. We had fixed the issue when a user had updated the Close Date and closed w/in the same polling period, but the issue occurs when changing Close Date, letting polling happen and then closing the task. (again this is regardless if the task is a Recurring Task or not.)  
**Fix implemented (2026-04-22):** `auto_closed_date` now respects any pre-set Closed Date for all task types regardless of when it was set — the same-poll-only restriction was removed. Re-test needed.
**Testing Notes 2:** This situation can only occur during polling as the field is cleared out from an Open task.  
**Fix implemented (2026-05-12):** The missed-reopen detection in `auto_closed_date` was firing on every live poll, wiping any pre-filled Closed Date on an open task before the user could close it. Fixed by adding `and prev_page is page` to that condition — the check now only fires during the daemon init pass (where the daemon passes the same object for both `page` and `prev_page`), not during live polling. Pre-filled Closed Dates on open tasks are now preserved across poll cycles. The field is still cleared correctly on: (1) explicit reopen transition (Complete → non-Complete), and (2) daemon restart/governance init pass (missed-reopen detection). Re-test needed.

---

### B5 — Period Key unchanged on unrelated property edit
**Setup:** Open recurring task with Period Key = current period.  
**Action:** Edit the task's Name or some other property.  
**Expected:** Period Key and Occurrence # are NOT changed by the daemon.  
**Status:** `[P]`
**Testing Notes:** Not entirely sure why this is a test. It's VERY specific, but all the other tests are more high-level. Regardless, I couldn't get it to edit Period Key or Occurrence # with the changes I tried. 

---

### B6 — No Anchor Day: Due Date defaults to end of period
**Setup:** RTD with Type = "Responsibility", Period = "Month", Anchor Day empty.  
**Action:** GOVERNANCE creates a task (zero open tasks).  
**Expected:** Task Due Date = last day of the current month (single date, no span). No date range written.  
**Variation:** Same setup with Anchor Time = "14:00" → Due Date = last day of month at 14:00.  
**Status:** `[P]` 
**Fix implemented (2026-05-18):** Previously wrote a full period span (e.g. May 1–May 31) which caused the task to appear on every calendar day. Now writes only the end-of-period date. Anchor Time (if set) is applied to the end-of-period date.

---

## C — Occurrence # this Period Counting

### C1 — First task of the period gets Occurrence # = 1
**Setup:** RTD with Period = "Week", no tasks exist this week.  
**Action:** GOVERNANCE creates a new task (zero open tasks → governance creates one).  
**Expected:** New task Occurrence # = 1.  
**Status:** `[P]`

---

### C2 — Second completion in same period increments count
**Setup:** RTD with Cadence Type = "Minimum N per period", N = 3. One task for this period already closed (Occurrence # = 1). A second open task exists (Occurrence # = 2).  
**Action:** Complete the second task.  
**Expected:** New task created with Occurrence # = 3.  
**Status:** `[P]`
**Testing Notes:** This passes, but a different issue exists when closing out a responsibility that existed days prior (but not autoclosed)

---

### C3 — User edits Occurrence # — next task still uses COUNT, not MAX+1
**Setup:** RTD, two tasks exist this period. User manually changes one task's Occurrence # from 2 to 99.  
**Action:** Complete one of the tasks.  
**Expected:** New task Occurrence # = 3 (count of tasks in period + 1 = 2 + 1), NOT 100.  
**Status:** `[P]`

---

### C4 — New period resets Occurrence # to 1
**Setup:** RTD with Period = "Month". Last month had 3 tasks (Occurrence #s 1–3). New month begins.  
**Action:** Wait for GOVERNANCE pass after period rollover (or restart daemon in new month).  
**Expected:** New task created with Occurrence # = 1, Period Key = new month.  
**Status:** `[P]`
**Testing Notes:** GOVERNANCE pass doesn't autoclose tasks. It does autocreate a task if no open one exists, and does label it with the correct Occurrence # though.  
**Observation (2026-05-12):** While running this test, noticed that auto-cancelled Responsibility tasks were getting Closed Date = their Due Date, which is inaccurate (a task due weeks ago shouldn't appear closed on that old date). Fixed separately — see Z9 update.


---

### C5 — Bad Habit: Occurrence # resets at period boundary (not lifetime)
**Setup:** RTD with Type = "Bad Habit", Period = "Week". Previous week had 4 occurrences. New week begins.  
**Action:** Trigger GOVERNANCE.  
**Expected:** New task Occurrence # = 1 (not 5). Period Target reflects weekly cadence.  
**Status:** `[P]`

---

## D — Grace Period Auto-Cancel (GOVERNANCE / 2am cron)

### D1 — Overdue Responsibility task is auto-cancelled
**Setup:** RTD with Type = "Responsibility", Grace Period = 2. Open task with Due Date = 3 days ago. No Ignore Grace Period checkbox.  
**Action:** Wait for 2am GOVERNANCE cron (or restart daemon).  
**Expected:** Task Status set to "Cancelled". GOVERNANCE then creates a new task since zero open remain.  
**Status:** `[P]`
**Testing Notes:** Ran Governance on 5/01/2026, with no Anchor Day/Time and when the next task was created (Min N per period, N=100) and it set the Due Date to NEXT month (june 1 to 30).  
**Fix implemented (2026-04-22):** `use_next_period = False` when governance creates a task because none exists — new task now targets the current period (Z5). Grace period with None value now treated as 0 (Z8). Auto-cancelled tasks get a past-period Closed Date (Z9). Re-test needed.  
**Fix updated (2026-05-12, second pass):** Auto-cancel Closed Date now set to end-of-due-period via `_period_end()` (e.g. April 30 23:59 for an April Monthly task) — correctly attributes the cancellation to the past period regardless of when governance runs. See Z9.
**Testing Notes:** tested and passed

---

### D2 — Task within grace window is NOT cancelled
**Setup:** Same as D1 but Due Date = 1 day ago (Grace Period = 2, so still within window).  
**Action:** Wait for 2am GOVERNANCE cron.  
**Expected:** Task remains open. No cancellation.  
**Status:** `[P]`

---

### D3 — "Ignore Grace Period" checkbox bypasses auto-cancel
**Setup:** Responsibility task that is overdue (past Grace Period). "Ignore Grace Period (Recurring Task)" checkbox is checked on the task.  
**Action:** Wait for 2am GOVERNANCE cron.  
**Expected:** Task is NOT cancelled. Bot Notes not added for this.  
**Status:** `[P]`

---

### D4 — Period cap: stale period key + 1 day into new period → auto-cancel
**Setup:** RTD with Period = "Week", Type = "Responsibility", Grace Period = 9999. Open task has Period Key from last week (stale). Current time is Tuesday (≥ 1 day past Monday's period start).  
**Action:** 2am GOVERNANCE cron.  
**Expected:** Task auto-cancelled despite enormous grace period. The period cap overrides grace.  
**Status:** `[P]`
**Testing Notes:** Manually created a task for a recurring task and it had to set the Occurrence # and Period Key.... despite the overdue nature of the task, it set the Period Key to "this week" and that caused the Responsibility to not auto-close. This is an outlier.  
**Fix implemented (2026-04-22):** The init block in `auto_recurring_tasks` now derives Period Key from the task's existing Due Date (if set) rather than always using `now`. A manually created task with a past Due Date will now get the correct past period key, allowing the grace period stale-key check to fire. Re-test needed.  
**Fix updated (2026-05-12):** Stale detection changed from `task_pk != current_period_key` to `task_pk < current_period_key` (lexicographic). Tasks with a FUTURE period key (pre-created for next period) are no longer considered stale and will not be auto-cancelled.
**Testing Notes:** tested and passed

---

### D5 — Non-Responsibility type is NOT auto-cancelled
**Setup:** RTD with Type = "Bad Habit" (or any non-Responsibility type), Grace Period = 0. Open task is 10 days overdue.  
**Action:** 2am GOVERNANCE cron.  
**Expected:** Task is NOT cancelled. Grace period logic only applies to Responsibility type.  
**Status:** `[P]`
**Testing Notes:** Tried both a period Period Key and expired Due Date and neither would close the Habit (good)

---

### D6 — No Due Date → no auto-cancel
**Setup:** Responsibility task, Grace Period set on RTD, but task has no Due Date.  
**Action:** 2am GOVERNANCE cron.  
**Expected:** Task is NOT cancelled. No due date = no expiry.  
**Status:** `[P]`

---

## E — Bot Notes
**Testing Notes:** No notes added. I've seen no notes added from any action. 

### E1 — Duplicate RTD name → note added to both
**Setup:** Two active RTDs both named "Take vitamins".  
**Action:** GOVERNANCE pass.  
**Expected:** Both RTD pages have a Bot Notes entry with code `RTD_DUPLICATE_NAME`, warning that another RTD with the same name exists.  
**Status:** `[S]`  
**Design change (2026-04-22):** Duplicate name detection removed per Z13 — the check should detect multiple open tasks with the same RTD link, not same name. E1 is now **obsolete**; the `RTD_DUPLICATE_NAME` code is no longer emitted.

---

### E2 — Duplicate name resolved → note cleared
**Setup:** Continuing from E1. Rename one RTD so names are now unique.  
**Action:** Next GOVERNANCE pass.  
**Expected:** Both RTD pages have Bot Notes cleared (field empty).  
**Status:** `[S]`  
**Design change (2026-04-22):** Obsolete — duplicate name detection removed (see E1).

---

### E3 — Multiple open tasks → note on RTD
**Setup:** RTD with 2 open tasks both linked to it.  
**Action:** GOVERNANCE pass.  
**Expected:** RTD page Bot Notes contains `RTD_MULTIPLE_OPEN` warning with the count of open tasks.  
**Status:** `[S]`  
**Design change (2026-05-12):** Multiple open tasks across different periods is now explicitly allowed (user may pre-create future-period tasks with Due Dates set in advance). The `RTD_MULTIPLE_OPEN` warning is no longer emitted. E3 is **obsolete**.

---

### E4 — Multiple open tasks resolved → note cleared
**Setup:** Continuing from E3. Close or delete the extra task so only 1 open remains.  
**Action:** Next GOVERNANCE pass.  
**Expected:** Bot Notes on RTD page is cleared.  
**Status:** `[S]`  
**Design change (2026-05-12):** Obsolete — see E3.

---

### E5 — At-most-N cap exceeded → note on RTD
**Setup:** RTD with Cadence Type = "At most N per period", Cadence N = 2. Three or more tasks exist this period (count > N).  
**Action:** GOVERNANCE pass.  
**Expected:** RTD page Bot Notes contains `RTD_AT_MOST_N_REACHED` warning that cap is exceeded. Count = N exactly should produce NO note — that is the happy path.  
**Status:** `[P]`
**Design change (2026-05-12):** Condition changed from `count >= N` to `count > N`. Reaching the limit is expected; only exceeding it warrants a warning.

---

### E6 — Invalid Anchor Time format → Bot Note on RTD
**Setup:** RTD with a malformed Anchor Time value (e.g. "2pm", "14", "bad").  
**Action:** GOVERNANCE creates or initializes a task.  
**Expected:** RTD page Bot Notes contains `RTD_INVALID_ANCHOR_TIME` message indicating the value could not be parsed and that Due Date was set to date only. Note clears when Anchor Time is corrected to a valid HH:MM value and the next GOVERNANCE pass runs.  
**Status:** `[P]`

---

### E7 — Exactly N per period: N completions in current period → next task targets NEXT period
**Setup:** RTD with Cadence Type = "Exactly N per period", N = 2. Two tasks for the current period have been completed (not cancelled).  
**Action:** GOVERNANCE pass.  
**Expected:** Governance detects 0 open tasks, counts 2 completions in current period (≥ N=2), sets `force_next=True`, and calls `_create_next_task` targeting the NEXT period. Cancelled/skipped tasks do NOT consume the quota — only completions count.  
**Status:** `[P]`  
**Fix implemented (2026-05-14):** `force_next` governance block expanded from Responsibility-only to all task types. `force_next_period=True` is passed to `_create_next_task` only when completion count ≥ N threshold.

---

### E8 — Exactly N per period exceeded → Bot Note on RTD
**Setup:** RTD with Cadence Type = "Exactly N per period", N = 2. Three or more completions exist this period (e.g. manually created tasks pushed past N).  
**Action:** GOVERNANCE pass.  
**Expected:** RTD page Bot Notes contains `RTD_EXACTLY_N_EXCEEDED` warning. Note clears when completions drop back to ≤ N (e.g. after manual cleanup).  
**Status:** `[P]`

---

## F — GOVERNANCE Pass (Startup and 2am Cron)

### F1 — Startup governance initializes missing fields across all pages
**Setup:** Several tasks exist with Reopen Count missing, First Due Date missing, etc.  
**Action:** Start the daemon.  
**Expected:** Governance pass runs on every page; missing fields are initialized (Reopen Count → 0, First Due Date → current Due Date if set, etc.).  
**Status:** `[P]`

---

### F2 — RTD with zero open tasks → new task created at startup
**Setup:** Active RTD with no open tasks.  
**Action:** Start the daemon.  
**Expected:** `run_recurring_governance` detects zero open tasks and calls `_create_next_task`. Task appears in Notion.  
**Status:** `[P]`

---

### F2b — Multiple open tasks in different periods are preserved (not warned about)
**Setup:** RTD with Period = "Month". Two open tasks exist: one with Period Key = current month and Due Date in current month; a second with Due Date in next month.  
**Action:** GOVERNANCE pass.  
**Expected:** Both tasks are left untouched. No Bot Note is added. GOVERNANCE does NOT create a third task (current-period task already exists). Occurrence # on each task is corrected if wrong, but both tasks coexist without warning.  
**Status:** `[P]`

---

### F2c — Future-period open task does not block current-period task creation
**Setup:** RTD with Period = "Month". One open task exists with Due Date in NEXT month (future period). No task exists for the current month.  
**Action:** GOVERNANCE pass.  
**Expected:** GOVERNANCE detects no open task for the current period and creates one. Two open tasks now exist: one for this month, one for next month. No warning emitted.  
**Status:** `[P]`
**Testing Notes:** Tested both past and future periods, test F3

---

### F3 — 2am cron fires once per day
**Setup:** Daemon running. Set system time to 02:00 local. Confirm `last_governance_date` was set to yesterday (e.g., start daemon near midnight).  
**Action:** Wait for the 2am poll.  
**Expected:** GOVERNANCE runs exactly once. Daemon log shows "2am cron: running GOVERNANCE functions."  
**Note:** Can verify by watching the log; the line should not appear twice within the same 2am minute.
**Status:** `[S]`  
**Testing Notes:** Not going to run this right now. Too many bugs existing in this code to test. We might need to implement a way to run the code base with ONLY this running. Plus with test F4 I'd have to wait an additional day to test it. I personally would want it to run twice that day after starting. 

---

### F4 — Daemon started after 2am — governance doesn't double-fire
**Setup:** Start daemon at 3am. GOVERNANCE runs at startup. `last_governance_date` is set to today.  
**Action:** Wait past 2am the next day.  
**Expected:** 2am cron fires the NEXT day only. No double-run today.  
**Status:** `[S]`
**Testing Notes:** I do NOT want this functionality. Remove it. Just have it run everyday at 2am local time.  
**Fix implemented (2026-04-22):** `last_governance_date` now initialized to `yesterday` (`now - timedelta(days=1)`). Startup's governance run no longer suppresses tonight's 2am cron — the cron fires every night regardless of when the daemon started. 

---

## G — Closed Date and Reopen Count (auto_closed_date)

### G1 — Completing a task stamps Closed Date
**Setup:** Non-recurring task, Status = "In Progress", Closed Date empty.  
**Action:** Set Status → Done.  
**Expected:** Closed Date is set to approximately now. Reopen Count unchanged (or initialized to 0 if missing).  
**Status:** `[P]`

---

### G2 — Completing any task respects pre-filled Closed Date
**Setup:** Any task (recurring or non-recurring). User pre-fills Closed Date = last month. Sets Status → Done.  
**Expected:** Closed Date is NOT overwritten (stays as last month). No distinction by task type — pre-filled Closed Date is always respected. For recurring tasks, the bot additionally uses this date for period detection.  
**Status:** `[P]`

---

### G3 — Completing a non-recurring task with pre-filled Closed Date does NOT overwrite it
**Setup:** Non-recurring task. User pre-fills Closed Date = some past date. Sets Status → Done.  
**Expected:** Closed Date is NOT overwritten. Behavior is identical to G2 — no type distinction exists.  
**Status:** `[P]`  
**Design note:** Original expectation (overwrite for non-recurring) was wrong. The 2026-04-22 fix made `auto_closed_date` respect pre-filled Closed Date for all task types. The only use case for a pre-filled Closed Date on a non-recurring task is a user who had the field named wrong and filled it in manually — overwriting that data would be destructive.


---

### G4 — Reopening a task clears Closed Date and increments Reopen Count
**Setup:** Task Status = "Done", Closed Date = set, Reopen Count = 1.  close
**Action:** Set Status → "In Progress".  
**Expected:** Closed Date cleared. Reopen Count = 2.  
**Status:** `[P]`

---

### G5 — Governance backfills Closed Date for already-Complete tasks
**Setup:** Task is already in Complete group but Closed Date is empty (e.g., was closed before the bot was set up).  
**Action:** Daemon startup governance pass (prev_page == page, no status transition fires, but governance check fires).  
**Expected:** Closed Date backfilled from `last_edited_time`. Reopen Count initialized to 0 if missing.  
**Status:** `[P]`
**Testing Notes:** When the daemon is running and copies the Last Edited time (yes), and it's setting the timezone of Last Closed to be UTC. I don't know why. I think that is the default for Last Edited since I don't think that is returning a timezone. I'm unsure.  
**Fix implemented (2026-05-12):** Same root cause as B1 — `last_edited_time` from Notion is always UTC. The backfill now parses it through `_parse_closed_dt()` (which converts UTC→local via `.astimezone()`) and stores only the local YYYY-MM-DD date string. Re-test needed.

---

## H — Due Date Update Count and First Due Date (auto_due_date_update_count)

### H1 — Due Date set for the first time stamps First Due Date, count stays 0
**Setup:** Task with no Due Date and no First Due Date.  
**Action:** Set Due Date.  
**Expected:** First Due Date = that date. Due Date Update Count = 0 (not incremented on first set).  
**Status:** `[P]`

---

### H2 — Changing Due Date from one date to another increments count
**Setup:** Task with First Due Date set (indicating a prior Due Date history), Due Date = April 10.  
**Action:** Change Due Date to April 17.  
**Expected:** Due Date Update Count incremented by 1.  
**Note:** Even if the date changes to the original First Due Date, still increment.
**Status:** `[P]`

---

### H3 — Clearing Due Date does NOT increment count
**Setup:** Task with First Due Date set, Due Date = April 10, Count = 2.  
**Action:** Clear Due Date.  
**Expected:** Due Date Update Count remains 2. First Due Date unchanged.  
**Status:** `[P]`

---

### H4 — Due Date unchanged between polls does NOT increment count
**Setup:** Task with Due Date = April 10, Count = 1. No change to Due Date.  
**Action:** Daemon polls and sees the same last_edited_time as the previous snapshot — page is skipped (boundary overlap logic). Alternatively, edit a non-Due-Date field.  
**Expected:** Count not incremented. (Edit a different field to force a poll hit; Due Date is same as prev_page, so no increment.)  
**Status:** `[P]`

---

### H5 — Count initialized to 0 on governance if missing
**Setup:** Existing task with Due Date set but Due Date Update Count property is empty/missing.  
**Action:** Daemon startup governance pass.  
**Expected:** Due Date Update Count initialized to 0. First Due Date stamped if Due Date is present.  
**Status:** `[P]`

---

### H6 — Changing Due Date time does not increment Due Date Update Count
**Setup:** Existing task with Due Date and First Due Date have the same date, but the time is different
**Action:** Daemon polls and sees Due Date is the same as First Due Date, though the time has changed. 
**Expected:** Count not incremented. 
**Status:** `[P]`  
**Fix implemented (2026-04-22):** `auto_due_date_update_count` now compares only the `[:10]` date portion of both current and previous Due Date strings. Time-only changes no longer increment the counter. Re-test needed.

---

### H7 — Changing Due Date time does not increment Due Date Update Count
**Setup:** Existing task with Due Date and First Due Date have different dates
**Action:** Daemon polls and sees Due Date changed the time but not the date. 
**Expected:** Count not incremented. 
**Status:** `[P]`  
**Fix implemented (2026-04-22):** Same fix as H6 — date-only comparison. Re-test needed.

---

## I — Edge Cases and Error Handling

### I1 — RTD definition page deleted mid-run
**Setup:** Task linked to an RTD. Delete the RTD page in Notion.  
**Action:** Complete the task.  
**Expected:** `client.get_page()` fails; error is logged; daemon continues without crashing. No new task created.  
**Status:** `[S]`
**Test Notes:** Unsure how to test this. I tried deleting the RTD midrun, and it just empties the field fr "Recurring Series" and the bot didn't appear to do anything. 

---

### I2 — Notion API error during task creation
**Setup:** Simulate or cause an API error on `create_page` (e.g., temporarily revoke token, or use an invalid database ID).  
**Action:** Complete a recurring task.  
**Expected:** Error logged with the definition ID. Daemon does not crash. Existing tasks and snapshots are unaffected.  
**Status:** `[S]`
**Test Notes:** Unsure how to simulate/cause this. 

---

### I3 — Task linked to multiple RTD series
**Setup:** Create a task with "Recurring Series" relation pointing to two different RTDs.  
**Action:** Complete the task.  
**Expected:** Only the FIRST series ID is used (current behavior). One new task created. No crash.  
**Note:** This is an unsupported configuration; test verifies graceful handling.
**Status:** `[S]`  
**Test Notes:** it does not complete it. (and by "complete" it should be "Cancelled")
**Test Notes:** DOn't feel like testing this

---

### I4 — First poll sight of a page (prev_page = None) runs initialization
**Setup:** Create a new recurring task mid-run (after daemon has already built its snapshot). Task has no Period Key or Occurrence #.  
**Action:** Wait for the next poll to pick up the new page.  
**Expected:** `auto_recurring_tasks` sees `prev_page=None`, detects uninitialized fields, and stamps Period Key, Occurrence #, Period Target, Due Date.  
**Status:** `[S]`
**Test Notes:** Don't feel up to testing this. 

---

### I5 — Unchanged page at poll boundary is skipped (no double-automation)
**Setup:** Task was just updated by the bot (its `last_edited_time` now equals the poll's `since` timestamp).  
**Action:** Next poll fetches the page due to the inclusive `on_or_after` filter.  
**Expected:** Daemon detects `last_edited_time == prev_page last_edited_time` and skips automations. No duplicate updates. Log shows "Skipping unchanged page."  
**Status:** `[S]`
**Test Notes:** Don't feel up to testing this. 


---

### I6 — Optional task fields absent → bot skips writes without crashing
**Setup:** Remove `Period Key (Recurring Task)`, `Occurrence # this Period (Recurring Task)`, and/or `Period Target (Recurring Task)` columns from the task database. Active RTD exists with no open tasks.  
**Action:** Start daemon (triggers governance + schema load). Close a recurring task (triggers `_create_next_task`).  
**Expected:** Daemon starts without error. Schema is logged ("Task DB schema loaded: N properties found"). New tasks are created/initialized successfully. Missing optional fields are silently skipped — no 400 Bad Request errors in the log. Present fields (Status, Due Date, Name, etc.) are still written correctly.  
**Status:** `[P]`
**Testing Notes:** I ran the program with the "--governance" flag and for an RTD with no open task: a New task IS created, but there are some key fields not filled out: Reopen count, Due Date Update Count. These columns WERE filled out though: Name, Recurring Series (relational field) and Due Date
**Testing Notes:** Claude did some changes and it's working now. 

---

### I7 — Optional fields added mid-session require restart before bot writes to them
**Setup:** Run daemon with optional fields absent (per I6). Add the missing columns to the task database in Notion.  
**Action:** Close a recurring task (no restart).  
**Expected:** Bot still skips writing to the newly added fields — schema is cached from startup, so `_filter_optional` strips them from write payloads. Reading is unaffected (Notion sends all fields on every page). After daemon restart, schema reloads and bot writes to the newly present fields normally.  
**Status:** `[P]`
**Testing notes:** I hate that this is the price to pay for making the fields optional, but I'd rather make them optional than not. 

---

### I8 — `closed_date` flag absent → no Closed Date stamped, no 400 errors
**Setup:** Config with `closed_date` absent (or `false`) for the task database. Task database has a Closed Date column.  
**Action:** Close a task (status → Done).  
**Expected:** No Closed Date is stamped, no 400 API errors in logs. All other automations still fire normally.  
**Status:** `[P]`

---

### I9 — `reopen_count` absent with `closed_date = true` → Closed Date still works, Reopen Count not written
**Setup:** Config with `closed_date = true` and `reopen_count` absent (or `false`).  
**Action:** Close a task, then reopen it.  
**Expected:** Closed Date is stamped on close. On reopen, Closed Date is cleared. Reopen Count is never written — no 400 errors, no initialization attempt.  
**Status:** `[P]`

---

### I10 — `due_date_tracking` absent → Due Date Update Count and First Due Date never written
**Setup:** Config with `due_date_tracking` absent (or `false`). Task database has Due Date Update Count and First Due Date columns.  
**Action:** Set or change a Due Date on a task.  
**Expected:** Neither Due Date Update Count nor First Due Date is written. No 400 API errors.  
**Status:** `[P]`

---

### I11 — Recurring tasks enabled, Closed Date column absent → CRITICAL error logged
**Setup:** `recurring_tasks.enabled = true` in config. Remove the Closed Date column from the task database in Notion.  
**Action:** Start daemon (or run `--governance-only`).  
**Expected:** CRITICAL error logged at governance startup: "Closed Date column not found". Daemon continues — does not abort. Recurring task behavior will be incorrect but other automations are unaffected.  
**Status:** `[P]`


---

## Z — Changes/Defects that are needed based on testing (general closer analysis)

### Z1 — New/activated RTD creates task within one poll cycle
**Setup:** Daemon running. No governance pass pending.
**Action (scenario A):** Create a new RTD with Status = "Active" and valid fields.
**Action (scenario B):** Set an existing RTD's Status from non-Active → "Active".
**Expected:** Within one poll cycle (~60s), governance fires automatically and creates a task for the current period. Log shows "RTD change detected" then "RTD changes detected — running governance."
**Status:** `[P]`

### Z2 — Due Dates for Habits
I don't think Habits should have auto-populating due dates.  
**Fix implemented (2026-04-22):** `_calc_due_date` now returns `None` for `task_type` of "Habit" or "Bad Habit". No due date is calculated or written for these types.

### Z3 — Empty Anchor Day: Due Date should not span full period
When no Anchor Day is set on a Responsibility RTD, the bot was writing a full date range (e.g. May 1–May 31), causing the task to appear on every calendar day in Notion's calendar view.  
**Fix implemented (2026-05-18):** Due Date now set to end of period only (e.g. May 31). Anchor Time, if set, is applied to that end date. See test B6.

### Z4 — Period Key not filling out/changing when daemon starts
In order to get Due Date populated for a new Recurring Task (on main task table, not definitions), erased Period Key, Occurrence #, and Period Target. The Period Target is NOT being filled in or changed though.  
**Status:** `[CLOSED]` — It fills out

### Z5 — Due Date is not generating appropriately
Example task: Habit due once a month, created today 4/27/2026, with an Anchor Day of 29, but when it created a task for this period "2026-04", it set the due date to next month, 5/29/2026. (no grace period). This also happened in Test D1, when closed on 5/01.  
**Fix implemented (2026-04-22):** When governance creates a task because none exists (`closed_task is None`), `use_next_period` is now `False`. New tasks target the current period.

### Z6 — Occurrence # not appropriate
Same task as Z5 (Habit, once a month, with period key changed to previous period) is incrementing the Occurrence # with every closed task despite the Period Key being different than the other tasks.  
**Status:** `[OPEN]` — investigate further; may be related to period key derivation from now vs. closed date.

### Z7 — Period Key incorrect
Closed a task on 4/30 @ 2:57pm Central, for a Daily task (N Per Period, N = 4, Cadence = Day) and the period key was set to 4/29. But the task was created a couple days prior.  
**Status:** `[OPEN]` — likely a system timezone issue; `datetime.now().astimezone()` uses system timezone. Confirm Pi/laptop timezone is set to local time, not UTC.

### Z8 — Autoclosing Responsibilities not working
When setting a Responsibility's due date to the past with no grace period set and Ignore Grace Period not set, tasks didn't autoclose when using the `--governance-only` flag.  
**Fix implemented (2026-04-22):** `grace = None` is now treated as `grace = 0` (cancel on due date). Previously, a missing Grace Period field caused the auto-cancel block to be skipped entirely.

### Z9 — Autoclose period needs to be past period
Autoclose needs to set the Closed Date to the previous period so it doesn't count towards "this period". This also impacts auto-closed tickets.  
**Fix implemented (2026-04-22):** Auto-cancel sets a past-period Closed Date in the same API call that sets Status = Cancelled.  
**Fix updated (2026-05-12):** Closed Date changed from the task's Due Date (inaccurate — could be weeks old) to 23:59 yesterday local time.
**Fix updated (2026-05-12, second pass):** "Yesterday at 23:59" was still wrong for week/month/year tasks — May 1 23:59 is still May, so an April Monthly task would count as May. Changed to end-of-due-period: `_period_end(period, due)` computes 23:59 on the last day of the period the Due Date falls in (e.g. April 30 23:59 for a Monthly task due in April, Sunday 23:59 for a Weekly task). This correctly attributes the cancellation to the past period regardless of when governance runs.
**Fix updated (2026-05-14):** `_period_end` could return a future date if governance cancels a task whose period hasn't ended yet (e.g. a May task auto-cancelled on May 14 — period end is May 31). Fixed by capping at `min(_period_end(period, due), yesterday 23:59)`. The Closed Date is now always in the past.

### Z10 — Don't copy task title when creating new recurring task
Need to automatically exclude copying the Title of the Recurring task that was closed. The default should be the title of the Recurring Task DEFINITION.  
**Fix implemented (2026-04-22):** `_create_next_task` now always overwrites `Name` with the definition title after calling `_copy_task_fields`.

### Z11 — Running --governance-only flag needs more information in logs
When looking at the log of governance function, would like to see the Task name and maybe "Created time" too.  
**Fix implemented (2026-04-22):** `_get_title(task)` is now called and included in the auto-cancel log line.

### Z12 — Rename "Ignore Grace Period" to "Ignore Due Date"
Since that name is more accurate.  
**Status:** `[OPEN]` — requires a field rename in the Notion database schema. Field name in code is `"Ignore Grace Period (Recurring Task)"`. If renamed in Notion, update `FIELDS_NOT_INHERITED` and the governance check in `recurring_tasks.py`.

### Z13 — Duplicate Recurring Task check incorrect field
It should not be checking the name but whether there are multiple open tasks with the same value in the Recurring Series field.  
**Fix implemented (2026-04-22):** Removed the duplicate RTD name detection block from `run_recurring_governance`. The `RTD_MULTIPLE_OPEN` check (multiple open tasks per series) already covers the real concern. `RTD_DUPLICATE_NAME` constant and `defaultdict` import also removed.

### Z14 — Do not increment Due Date Update Count when only changing time
If the First Due Date and Due Date match for Date, do not change the update count. If the dates of Due Date do NOT match First Due Date but the TIME on Due Date changed and the date stayed the same, still do not increment.  
**Fix implemented (2026-04-22):** Both current and previous Due Date are now compared using only the `[:10]` date portion. Time-only changes no longer increment the counter. Documented in DESIGN.md.

### Z15 — Period Target sync writes empty string every poll for unrecognized cadence types
When a task's RTD has a Cadence Type that `_build_period_target` doesn't recognize, it returns `""`. The Period Target sync condition `if expected_target != current_target` fires every poll (`"" != None`), writing an empty `rich_text` value to the task on every cycle.  
**Fix implemented (2026-05-15):** Added `if expected_target` guard before the sync condition. Unrecognized cadence types leave the field as-is rather than overwriting with empty string.

### Z16 — Bot-created/bot-edited tasks: snapshot doesn't reflect bot's writes → stale `prev_page` on next poll
Two related problems sharing the same root cause and fix:  
(A) **Bot-created tasks**: when `auto_recurring_tasks` creates a new task via `_create_next_task`, the daemon's snapshot doesn't know about it. If the user closes it before the next poll, the daemon sees it for the first time already in Complete state (`prev_page is None`). The `if prev_page is not None` guard blocks the completion trigger.  
(B) **Bot-edited tasks**: after `update_page_properties`, the snapshot still holds the pre-poll page state. Next poll, the bot's own writes appear as "user changes" in the diff, and automations may react unnecessarily.  
**Fix implemented (2026-05-15):**  
— `_create_next_task` returns the created page dict. `auto_recurring_tasks` passes it back via `BOT_CREATED_PAGES_KEY` sentinel in its return dict. `run_automations_on_page` strips the sentinel, collects created pages, and returns `(post_edit_page, created_pages)`.  
— `update_page_properties` (Notion API) returns the updated page. `run_automations_on_page` captures it as `post_edit_page`.  
— `poll_database` and `run_automations_init_pass` use `post_edit_page` as the snapshot entry (falling back to the original page if no writes were made), and insert `created_pages` into the snapshot separately.  
— Result: `prev_page` on the next poll reflects exactly what the bot left — only genuine user changes after the bot's writes appear as diffs.  
**Known caveat (for DESIGN.md):** If a user changes field X and the bot also changes field X in the same poll interval, and the user's change landed *before* the bot's write, that user change is silently overwritten — the bot wins, and the snapshot holds the bot's value. If the user's change landed *after* the bot's write, the Notion API returns the user's value in the `update_page_properties` response and the snapshot captures it correctly. This is acceptable behavior: the bot manages specific fields and its writes are authoritative. Users who want to override a bot-managed field should do so after the bot has had a chance to act.

### Z17 — Weekly anchor-day Due Date lands in next week when anchor day has already passed
When a Weekly RTD has an Anchor Day set (e.g. Monday=1) and governance/creation runs on a day *after* that anchor day in the same week (e.g. Saturday), `_period_dates` with `use_next=False` was incorrectly adding 7 to `days_ahead` (a negative number), pushing the target date to *next* week's anchor day instead of *this* week's.

**Example:** Today = Saturday May 16 (W20). Anchor Day = Monday (1). `days_ahead = 0 - 5 = -5`. Old code: `-5 < 0 → +7 → days_ahead=2 → May 18 (W21)`. Correct: stay at -5 → May 11 (W20).

**Impact:** New task's Due Date landed in W21 instead of W20, so `target_period_key` was W21. Governance counted 0 closed tasks in W21 and set Occurrence # = 1, ignoring the Done task from May 13 (W20). The duplicate guard ("open task already exists for period 2026-W21") also prevented creation of a task for the actual current period.

**Fix implemented (2026-05-16):** Removed `elif not use_next and days_ahead < 0: days_ahead += 7` from `_period_dates`. When targeting the current period and the anchor day has already passed, `days_ahead` stays negative, correctly targeting the past anchor day within the current week.

### Z18 — Stale check uses stored Period Key field; corrupted/newline values cause false auto-cancel
The auto-cancel stale check read the task's stored `Period Key (Recurring Task)` field and compared it to the current period key. If the stored value was corrupted (e.g., `"2026-W19\n"` with a trailing newline from Notion's text input), the comparison `"2026-W19\n" < "2026-W20"` evaluated to True, marking the task as stale even though its Due Date was in the current period (W20). The task was then auto-cancelled.

**Root cause confirmed from logs:** Task `360326b7-c770-8171` had `Period Key = "2026-W19\n"` (newline appended, likely from pressing Enter in Notion's text field) and `Due Date = 2026-05-16` (Saturday, W20). Stale check read the field value → stale=True → past_cap=True → auto-cancelled. The `open_by_period` correction (which would have fixed the Period Key) runs AFTER the stale check and doesn't apply to cancelled tasks.

**Fix implemented (2026-05-16):**  
1. Stale check now computes the expected period key from the task's **Due Date** (ground truth) instead of reading the stored Period Key field. Falls back to `now()` if Due Date or period is absent (no-Due-Date open tasks are never stale). Stored Period Key field is never consulted.  
2. All remaining reads of the stored Period Key for comparison purposes now strip whitespace (`.strip()`) to guard against whitespace corruption. (Note: Z19, implemented in the same session, subsequently removed Period Key reads from `_task_in_period` and `_create_next_task` entirely — `.strip()` in `open_by_period` drift-detection is the only remaining read.)

### Z19 — Period Key removed from all period-membership logic; period derived from dates in memory

All code that previously read the stored `Period Key (Recurring Task)` field to determine period membership has been replaced with date-based computation:

- **Open tasks**: period determined by `Due Date`. If `Due Date` is absent, falls back to `now()` — an open task with no Due Date always counts as belonging to the current period.
- **Closed tasks**: period determined by `Closed Date`. `auto_closed_date` governance backfills `last_edited_time` (local timezone) as `Closed Date` for any Complete task missing it, ensuring Closed Date is always set before `_task_in_period` is called by governance.

**Affected code paths (all changed to date-based computation):**
1. `_task_in_period` open-task branch — now uses Due Date / `now()`, never stored Period Key.
2. `has_current_period_task` check — now computes from Due Date / `now()`, no Period Key fallback.
3. Stale check (see Z18) — Due Date / `now()`, no Period Key.
4. `_create_next_task` duplicate guard no-Due-Date fallback — now uses `_period_key(period, now)`.
5. `auto_recurring_tasks` initialization gate — changed from `period_key is None and instance_num is None` to `instance_num is None` (removes the stored Period Key read from init).

**What did NOT change:** Period Key writes (the bot still writes the field as a human-readable display label); `open_by_period` drift-correction still reads the stored field with `.strip()` to detect and fix stale display values.

**Expected behavior after Z19:**
- A task whose `Period Key` field has been corrupted (wrong value, trailing newline, manually edited) will not affect any period comparison. The corruption will be corrected on the next governance pass (governance writes the correct value back).
- An open task with no `Due Date` counts as the current period in all logic paths.
- Occurrence # is immune to Period Key corruption — it counts tasks by date, not by the stored field.