---
name: usage-xray
description: Explain where the user's Claude Code usage or weekly limit went, using their own local session logs. Use when the user asks why they hit a limit, why Claude Code is using so much, where their tokens or usage went, how to make their plan last longer, or whether a long session is costing them.
---

# usage-xray

Run the report. It reads the local session logs and prints aggregates only; it sends nothing anywhere.

```
python "${CLAUDE_PLUGIN_ROOT}/ccxray.py" report
python "${CLAUDE_PLUGIN_ROOT}/ccxray.py" report --days 7
```

Use `python3` if `python` is not found. Run it once; the output is short.

Then tell the user, in their language and in plain words:

1. The one or two findings that cost them the most, with the report's own numbers. Do not add numbers the report did not print.
2. What to change, taken from the report's "WHAT TO DO" lines.

Things to keep straight when explaining:

- The shares are price-weighted estimates. How subscription limits are counted is not public; the report assumes they follow price.
- "Expired cache" means they sent a message into a session after its prompt cache had run out, so the whole conversation was sent again at the cache-write price. Measured on real sessions, that cost about 20 times the same message with a warm cache.
- Running /compact after the cache has expired costs almost as much as sending the message. Running it before leaving a big session is several times cheaper. Starting a new session is cheapest.
- In a session whose cache has expired, the user can type `xray handoff`. A hook writes a brief of the session without using any tokens, and the next new session in the same folder loads it.
- If the report shows no expired-cache events and little big-context usage, say so. This tool then has little to offer that user.
