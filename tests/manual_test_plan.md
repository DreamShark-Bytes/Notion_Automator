# Manual Test Plan — Notion Automator

Tests are grouped by feature area. Each test is performed against a live Notion workspace
using the daemon in its normal polling mode unless noted as "GOVERNANCE" (requires waiting
for the 2am cron or restarting the daemon to trigger the governance pass).

**Status legend:** `[ ]` not run · `[P]` pass · `[F]` fail · `[S]` skip

## Tabe of Contents
- A. [Recurring Task Creation on Close](#a--recurring-task-creation-on-close)
- B. [Period Key and Period Detection](#b--period-key-and-period-detection)
- C. [Instance # Counting](#c--instance--counting)
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
**Expected:** New task created in same period; Instance # = (count of existing tasks this period) + 1; Period Key matches current period.  
**Status:** `[P]`

---

### A2 — Close triggers next task in NEXT period (Once per period)
**Setup:** RTD active, Cadence Type = "Once per period", open task exists.  
**Action:** Set task Status → Done.  
**Expected:** New task created; Due Date falls in the NEXT period (not the current one).  
**Status:** `[P]`

---

### A3 — Inactive RTD — no new task
**Setup:** RTD exists but Active checkbox is unchecked. Task linked to it.  
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
**Expected:** No new task created — the next-month task already exists. Log shows "open task already exists for period …". Instance # on the next-month task is unchanged.  
**Status:** `[ ]`

---

### A5 — Manually created task with missing bot fields gets initialized
**Setup:** Create a task manually in Notion and link it to an active RTD. Leave Period Key and Instance # blank.  
**Action:** Wait for the next daemon poll (or trigger a governance pass by restarting).  
**Expected:** Period Key, Instance #, Period Target, and Due Date are all stamped on the task. Instance # = (count of tasks in current period) + 1.  
**Status:** `[P]`

---

## B — Period Key and Period Detection

### B1 — Day period key uses local date, not UTC
**Setup:** Server is in a non-UTC timezone (e.g. UTC+2). It is 23:30 local (21:30 UTC).  
**Action:** Complete a recurring task with Period = "Day".  (where Due Date is generated. Type=Responsibility, Cadence Type!=Unlimited)
**Expected:** The new task's Period Key matches TODAY's local date (e.g. "2026-04-21"), not yesterday's UTC date.  
**Status:** `[P]`  
**Status Note:** Requires verifying server timezone is configured correctly.
**Testing Notes:** Had to wait until after 7pm Central for the datetime to flip over in UTC to the next day. Not only did it create the new task for tomorrow (Cadence Type=N per period, Cadence N = 4, Period=Day), it labelled it as Instance #=2, and the next task created had a Due Date of the NEXT day, not SAME day (despite being the first Responsibility/Task closed today)  
**Fix implemented (2026-05-12):** Root cause was NOT system timezone config — both Pi and laptop confirmed CDT. The actual bug: `_now_iso()` returns UTC, and `last_edited_time` from Notion is always UTC; both were stored directly into Closed Date. After 7pm CDT (= midnight UTC) this stamps the wrong calendar day. Fixed by adding `_now_local_date()` to `recurring_tasks.py` and replacing both the live close stamp and the `last_edited_time` backfill in `auto_closed_date`. Closed Dates are now always stored as local YYYY-MM-DD strings. Also fixes G5. Re-test needed.

---

### B2 — Week period key uses ISO week format
**Setup:** RTD with Period = "Week".  
**Action:** Complete a recurring task on any day.  
**Expected:** New task's Period Key = "YYYY-WNN" (ISO week, e.g. "2026-W17").  
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
**Setup:** Recurring task. User manually sets Closed Date to last month's date, then sets Status → Done. 
**Expected:** Closed Date is NOT overwritten (pre-filled respected). New task Due Date targets NEXT period relative to last month's date (i.e., this month's due date, not next month).  
**Status:** `[P]`
**Testing Notes:** Changing the Close date, letting the polling take place, and then closing a task DOES change the Closed Date AND edits the Reopen Count. Recurring tasks should NOT be touching the Closed Date, that should be exclusive to automations.py script, function: auto_closed_date() We should include that functionality as a requirement for recurring_tasks.py though. We had fixed the issue when a user had updated the Close Date and closed w/in the same polling period, but the issue occurs when changing Close Date, letting polling happen and then closing the task. (again this is regardless if the task is a Recurring Task or not.)  
**Fix implemented (2026-04-22):** `auto_closed_date` now respects any pre-set Closed Date for all task types regardless of when it was set — the same-poll-only restriction was removed. Re-test needed.
**Testing Notes 2:** This situation can only occur during polling as the field is cleared out from an Open task.  
**Fix implemented (2026-05-12):** The missed-reopen detection in `auto_closed_date` was firing on every live poll, wiping any pre-filled Closed Date on an open task before the user could close it. Fixed by adding `and prev_page is page` to that condition — the check now only fires during the daemon init pass (where the daemon passes the same object for both `page` and `prev_page`), not during live polling. Pre-filled Closed Dates on open tasks are now preserved across poll cycles. The field is still cleared correctly on: (1) explicit reopen transition (Complete → non-Complete), and (2) daemon restart/governance init pass (missed-reopen detection). Re-test needed.

---

### B5 — Period Key unchanged on unrelated property edit
**Setup:** Open recurring task with Period Key = current period.  
**Action:** Edit the task's Name or some other property.  
**Expected:** Period Key and Instance # are NOT changed by the daemon.  
**Status:** `[P]`
**Testing Notes:** Not entirely sure why this is a test. It's VERY specific, but all the other tests are more high-level. Regardless, I couldn't get it to edit Period Key or Instance # with the changes I tried. 

---

## C — Instance # Counting

### C1 — First task of the period gets Instance # = 1
**Setup:** RTD with Period = "Week", no tasks exist this week.  
**Action:** GOVERNANCE creates a new task (zero open tasks → governance creates one).  
**Expected:** New task Instance # = 1.  
**Status:** `[P]`

---

### C2 — Second completion in same period increments count
**Setup:** RTD with Cadence Type = "Minimum N per period", N = 3. One task for this period already closed (Instance # = 1). A second open task exists (Instance # = 2).  
**Action:** Complete the second task.  
**Expected:** New task created with Instance # = 3.  
**Status:** `[P]`
**Testing Notes:** This passes, but a different issue exists when closing out a responsibility that existed days prior (but not autoclosed)

---

### C3 — User edits Instance # — next task still uses COUNT, not MAX+1
**Setup:** RTD, two tasks exist this period. User manually changes one task's Instance # from 2 to 99.  
**Action:** Complete one of the tasks.  
**Expected:** New task Instance # = 3 (count of tasks in period + 1 = 2 + 1), NOT 100.  
**Status:** `[P]`

---

### C4 — New period resets Instance # to 1
**Setup:** RTD with Period = "Month". Last month had 3 tasks (Instance #s 1–3). New month begins.  
**Action:** Wait for GOVERNANCE pass after period rollover (or restart daemon in new month).  
**Expected:** New task created with Instance # = 1, Period Key = new month.  
**Status:** `[P]`
**Testing Notes:** GOVERNANCE pass doesn't autoclose tasks. It does autocreate a task if no open one exists, and does label it with the correct Instance # though.  
**Observation (2026-05-12):** While running this test, noticed that auto-cancelled Responsibility tasks were getting Closed Date = their Due Date, which is inaccurate (a task due weeks ago shouldn't appear closed on that old date). Fixed separately — see Z9 update.


---

### C5 — Bad Habit: Instance # resets at period boundary (not lifetime)
**Setup:** RTD with Type = "Bad Habit", Period = "Week". Previous week had 4 occurrences. New week begins.  
**Action:** Trigger GOVERNANCE.  
**Expected:** New task Instance # = 1 (not 5). Period Target reflects weekly cadence.  
**Status:** `[P]`

---

## D — Grace Period Auto-Cancel (GOVERNANCE / 2am cron)

### D1 — Overdue Responsibility task is auto-cancelled
**Setup:** RTD with Type = "Responsibility", Grace Period = 2. Open task with Due Date = 3 days ago. No Ignore Grace Period checkbox.  
**Action:** Wait for 2am GOVERNANCE cron (or restart daemon).  
**Expected:** Task Status set to "Cancelled". GOVERNANCE then creates a new task since zero open remain.  
**Status:** `[F]`
**Testing Notes:** Ran Governance on 5/01/2026, with no Anchor Day/Time and when the next task was created (Min N per period, N=100) and it set the Due Date to NEXT month (june 1 to 30).  
**Fix implemented (2026-04-22):** `use_next_period = False` when governance creates a task because none exists — new task now targets the current period (Z5). Grace period with None value now treated as 0 (Z8). Auto-cancelled tasks get a past-period Closed Date (Z9). Re-test needed.  
**Fix updated (2026-05-12, second pass):** Auto-cancel Closed Date now set to end-of-due-period via `_period_end()` (e.g. April 30 23:59 for an April Monthly task) — correctly attributes the cancellation to the past period regardless of when governance runs. See Z9.

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
**Testing Notes:** Manually created a task for a recurring task and it had to set the Instance # and Period Key.... despite the overdue nature of the task, it set the Period Key to "this week" and that caused the Responsibility to not auto-close. This is an outlier.  
**Fix implemented (2026-04-22):** The init block in `auto_recurring_tasks` now derives Period Key from the task's existing Due Date (if set) rather than always using `now`. A manually created task with a past Due Date will now get the correct past period key, allowing the grace period stale-key check to fire. Re-test needed.  
**Fix updated (2026-05-12):** Stale detection changed from `task_pk != current_period_key` to `task_pk < current_period_key` (lexicographic). Tasks with a FUTURE period key (pre-created for next period) are no longer considered stale and will not be auto-cancelled.

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
**Expected:** Both tasks are left untouched. No Bot Note is added. GOVERNANCE does NOT create a third task (current-period task already exists). Instance # on each task is corrected if wrong, but both tasks coexist without warning.  
**Status:** `[ ]`

---

### F2c — Future-period open task does not block current-period task creation
**Setup:** RTD with Period = "Month". One open task exists with Due Date in NEXT month (future period). No task exists for the current month.  
**Action:** GOVERNANCE pass.  
**Expected:** GOVERNANCE detects no open task for the current period and creates one. Two open tasks now exist: one for this month, one for next month. No warning emitted.  
**Status:** `[ ]`

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

### G2 — Completing a recurring task respects pre-filled Closed Date
**Setup:** Task linked to an RTD. User pre-fills Closed Date = last month. Sets Status → Done.  
**Expected:** Closed Date is NOT overwritten (stays as last month). Bot uses this date for period detection.  
**Status:** `[P]`

---

### G3 — Completing a non-recurring task overwrites pre-filled Closed Date
**Setup:** Non-recurring task. User pre-fills Closed Date = some past date. Sets Status → Done.  
**Expected:** Closed Date IS overwritten with now(). Non-recurring tasks always get stamped.  
**Status:** `[F]`
**Testing Notes:** Currently does not update the date. I think this is because we have measures in place to not update that date if changed between polling periods, but it's emptied out in governance (see test G1). This is technically a Pass though. We just can't create this scenario I think. I don't think there should be a difference in Close Date behavior between Recurring and Non-recurring tasks. again, this is technically a Pass. 


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
**Status:** `[F]`
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
**Status:** `[F]`  
**Fix implemented (2026-04-22):** `auto_due_date_update_count` now compares only the `[:10]` date portion of both current and previous Due Date strings. Time-only changes no longer increment the counter. Re-test needed.

---

### H7 — Changing Due Date time does not increment Due Date Update Count
**Setup:** Existing task with Due Date and First Due Date have different dates
**Action:** Daemon polls and sees Due Date changed the time but not the date. 
**Expected:** Count not incremented. 
**Status:** `[F]`  
**Fix implemented (2026-04-22):** Same fix as H6 — date-only comparison. Re-test needed.

---

## I — Edge Cases and Error Handling

### I1 — RTD definition page deleted mid-run
**Setup:** Task linked to an RTD. Delete the RTD page in Notion.  
**Action:** Complete the task.  
**Expected:** `client.get_page()` fails; error is logged; daemon continues without crashing. No new task created.  
**Status:** `[F]`
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
**Status:** `[F]`  
**Test Notes:** it does not complete it. (and by "complete" it should be "Cancelled")

---

### I4 — First poll sight of a page (prev_page = None) runs initialization
**Setup:** Create a new recurring task mid-run (after daemon has already built its snapshot). Task has no Period Key or Instance #.  
**Action:** Wait for the next poll to pick up the new page.  
**Expected:** `auto_recurring_tasks` sees `prev_page=None`, detects uninitialized fields, and stamps Period Key, Instance #, Period Target, Due Date.  
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

## J — Tests found by the developer that were not created by Claude

*(No entries yet — add test cases here as they are discovered during testing.)*

---

## Z — Changes/Defects that are needed based on testing (general closer analysis)

### Z1 — New Recurring Task Definition doesn't create a Task
This is a BIG MISTAKE. We need to monitor database: Recurring Task Definitions
When I create a new (active and valid) Recurring Task Definition, the daemon doesn't see that, and no task is created. ONLY when a governance pass is made. (overnight or program is started for the first time). This needs remedied by having the polling function.  
**Status:** `[OPEN]` — design discussion needed; polling the RTD database is a significant architectural change.

### Z2 — Due Dates for Habits
I don't think Habits should have auto-populating due dates.  
**Fix implemented (2026-04-22):** `_calc_due_date` now returns `None` for `task_type` of "Habit" or "Bad Habit". No due date is calculated or written for these types.

### Z3 — Empty Anchor Day
When no Anchor Day is filled in (and Type=Responsibility), do not set the [due date?]  
**Status:** `[OPEN]` — note appears truncated; unclear what the full requirement is.

### Z4 — Period Key not filling out/changing when daemon starts
In order to get Due Date populated for a new Recurring Task (on main task table, not definitions), erased Period Key, Instance #, and Period Target. The Period Target is NOT being filled in or changed though.  
**Status:** `[OPEN]` — investigate why Period Target sync is not firing in the init pass.

### Z5 — Due Date is not generating appropriately
Example task: Habit due once a month, created today 4/27/2026, with an Anchor Day of 29, but when it created a task for this period "2026-04", it set the due date to next month, 5/29/2026. (no grace period). This also happened in Test D1, when closed on 5/01.  
**Fix implemented (2026-04-22):** When governance creates a task because none exists (`closed_task is None`), `use_next_period` is now `False`. New tasks target the current period.

### Z6 — Instance # not appropriate
Same task as Z5 (Habit, once a month, with period key changed to previous period) is incrementing the Instance # with every closed task despite the Period Key being different than the other tasks.  
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