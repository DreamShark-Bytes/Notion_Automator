---
name: Bug Report
about: Something isn't working as expected
title: '[Bug] '
labels: bug
---

## What happened
<!-- What did the daemon do that it shouldn't have, or fail to do that it should have? -->

## What I expected
<!-- What should have happened instead? -->

## Steps to reproduce
1. 
2. 
3. 

## Which feature is affected
<!-- Check all that apply -->
- [ ] Closed Date Stamping
- [ ] Reopen Count
- [ ] Due Date Tracking
- [ ] Recurring Tasks — task creation
- [ ] Recurring Tasks — governance / auto-cancel
- [ ] RTD monitoring (live detection of new/changed definitions)
- [ ] Other: 

## Versions
<!-- Run: grep VERSION daemon.py and grep __version__ venv/lib/*/site-packages/notion_api.py -->
- Notion Automator: 
- Notion API: 
- Python: 
- OS: 

## Relevant log output
<!-- Paste the lines from journalctl -u notion-daemon or your log file that relate to this issue. Just the relevant lines, not the whole log. -->
```
paste log lines here
```

## Config snippet (if relevant)
<!-- Paste only the affected [[databases]] block or [recurring_tasks] section. Replace your token with ntn_... -->
```toml

```

## Related
<!-- Any related issues or links to PLANNED.md sections? -->
