# ccxray

See where your Claude Code usage really goes, and stop paying for expired prompt caches.

Claude Code caches your conversation so each turn only pays a small fee to re-read it. That cache runs out (after an hour, or five minutes on some setups). If you then send one more message into a large session, the whole conversation is sent again at the cache-write price. On the author's machine that was 18% of three weeks of usage.

ccxray does three things about it:

1. **Report.** Reads the session logs Claude Code already keeps on your machine and shows what your usage was spent on.
2. **Warning.** Holds a message back once when it would re-send a large, expired conversation, and tells you what it will cost.
3. **Handoff.** Type `xray handoff` in the old session. A hook writes a brief of it without using any tokens, and your next new session in that folder loads it.

One Python file, standard library only. Nothing is sent anywhere.

## Install

As a plugin:

```
/plugin install ccxray --marketplace abhiyarecord-hue/ccxray
```

Or by hand:

```bash
git clone https://github.com/abhiyarecord-hue/ccxray.git
```

```bash
python ccxray/ccxray.py install-hook
```

The manual install adds three hooks to `~/.claude/settings.json`, keeps your other settings, and saves the original once as `settings.json.ccxray.bak`. Remove them with `python ccxray/ccxray.py uninstall-hook`.

## The report

```bash
python ccxray.py report
```

```bash
python ccxray.py report --days 7
```

```bash
python ccxray.py report --share
```

`--share` prints a few lines with no project names, safe to paste. `--json` gives the numbers for scripts. With the plugin installed, `/ccxray:xray` runs the report and explains it.

What it found on the author's machine (three weeks, 216 sessions, 5,462 requests, mostly Opus):

- 78% of usage came from requests made with more than 200k tokens of context.
- 18.0% went to re-sending conversations whose cache had expired (38 times).
- 3.1% went to switching model in the middle of a session (10 times), which also re-sends the conversation.
- All tool output together was about 2M tokens. Tools that shrink file reads work on that small part.

After more than 65 minutes idle, 35 of 52 next requests missed the cache. Inside the cache lifetime it was under 1%.

## What one message really costs

Measured on real sessions with `claude -p --resume`, Opus 5.5, list price as reported by the CLI.

| What was sent | Cost |
|---|---|
| "Reply ok", into a 212k-token session idle for 11 hours | $1.39 |
| "Reply ok", into a 224k-token session idle for 10 hours | $1.43 |
| "Reply ok", into a 231k-token session with a warm cache | $0.07 |
| `/compact` on a 278k-token session after the cache expired | $1.35 |
| `/compact` on a 231k-token session with a warm cache | $0.20 |
| First message of a brand-new session (empty folder) | $0.09 |

- A message into an expired large session cost about 20 times the same message with a warm cache.
- Compacting after the cache has expired does not rescue much, because it has to read the whole conversation at full price first. Compacting while the cache was warm cost about a seventh as much.
- A new session is cheap, but you lose the conversation. The handoff is for that.

## The handoff, tested

The same question was asked two ways about a real 224k-token session, four days idle: "what is the goal, what was done last, what is next, name the files".

| How | Cost | Answer |
|---|---|---|
| Resume the old session | $1.69 | The reference answer |
| New session with the ccxray handoff | $0.21 | Same goal, same last four pieces of work with the same file names, same main next step |

The handoff answer missed three smaller follow-ups the full session listed (a share link to remove, test accounts to delete, some release setup). So it is about an eighth of the price and keeps the main thread, but it is not the whole conversation.

How it works: the handoff is an extract, not a model-written summary. It holds Claude's last replies, your messages, the last task list, files changed and commands run, newest first (about 2,500 tokens in that test). The new session gets a short digest at start and the path of the full file, which it reads in one step. One test, one session; a first version put the newest work at the end and did noticeably worse.

## The hooks

| When | What happens |
|---|---|
| You send a message into a session idle past its cache lifetime, holding 60k tokens or more | The message is held back once, with the size and the options. Send it again and it goes through; no second warning for 10 minutes. Slash commands are never held back. |
| You type `xray handoff` | The brief is written, nothing is sent to the model. |
| You start a new session in the same folder within 6 hours | The brief is loaded, once. |
| A conversation passes 150k tokens, and each further 100k | One line after the reply: how large it is, when its cache runs out, and that `/compact` is cheap until then. |

Settings, as environment variables: `CCXRAY_GUARD=0` turns all hooks off; `CCXRAY_MIN_CONTEXT` (60000) and `CCXRAY_REMIND_CONTEXT` (150000) set the sizes; `CCXRAY_WEIGHTS=cr=0.1,out=5` overrides the price ratios.

## What is and is not proven

- The report is arithmetic over your own logs. The price ratios (1-hour cache write 2, cache read 0.05, output 5, relative to an input token) match the cost the CLI reports for Sonnet 5.5 and Opus 5.5; they are applied to every model, so with mixed models the shares are approximate.
- How subscription limits are counted is not public. The report assumes they follow price.
- An "expired cache" event is a request, after a gap longer than the cache lifetime, where under 20% of the prompt came from cache. Some may have other causes.
- The warning was tested on real sessions: resuming a 224k-token and a 278k-token session after hours idle, the message was stopped with zero tokens used and the session file untouched.
- The warning does not see forked sessions (`--fork-session`): the fork's log is empty when its first message is sent.
- How much the hooks save over a week has not been measured. Run `report --days 7 --json` before and after a week and compare `stale_cache_extra_pct`.
- Tested on Windows with Python 3.11 only. The session log format is internal to Claude Code and can change; if it does, the report will say it found no usage and the hooks will stay silent.
- A held-back message has to be sent again; Claude Code does not put it back in the input box.
- If your sessions are short, the report will show little to fix, and this tool has little to offer you.

## License

MIT
