---
description: Show where your Claude Code usage went, from your local logs
argument-hint: [days]
---

Run `python "${CLAUDE_PLUGIN_ROOT}/ccxray.py" report` (add `--days $ARGUMENTS` if a number of days was given; use `python3` if `python` is not found). Then follow the usage-xray skill: tell me the one or two findings that cost the most, using only the report's numbers, and what to change.
