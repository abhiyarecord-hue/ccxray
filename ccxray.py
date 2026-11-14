#!/usr/bin/env python3
"""ccxray: see where your Claude Code usage really goes, and stop paying for expired caches.

  python ccxray.py report [--days N] [--json] [--share]   what your usage was spent on, from your own local logs
  python ccxray.py handoff [session-id | path.jsonl]      write a brief of a session, to continue it in a new one
  python ccxray.py install-hook | uninstall-hook          add / remove the three hooks below

Hooks (installed by install-hook, never called by hand):
  hook-prompt   UserPromptSubmit: holds back a message that would re-send a large conversation whose cache expired;
                typing `xray handoff` writes a brief instead, without using any tokens
  hook-stop     Stop: once a conversation gets large, shows when its cache expires and that /compact is cheap until then
  hook-start    SessionStart: loads the brief you just wrote into the new session

Stdlib only. Reads ~/.claude/projects/**/*.jsonl locally; nothing is sent anywhere.
"""
import collections, datetime, glob, json, os, re, sys, time

HOME = os.path.expanduser("~")
LOGS = os.environ.get("CCXRAY_LOGS") or os.path.join(HOME, ".claude", "projects")
STATE = os.environ.get("CCXRAY_STATE") or os.path.join(HOME, ".claude", "ccxray")
# Relative prices, 1.0 = one uncached input token. Ratios match the cost `claude -p` reports for Sonnet 5.5 and
# Opus 5.5 (1h cache write 2x, cache read 0.05x, output 5x). Override with CCXRAY_WEIGHTS=cr=0.1,out=5
W = {"inp": 1.0, "cw5": 1.25, "cw1": 2.0, "cr": 0.05, "out": 5.0}
if os.environ.get("CCXRAY_WEIGHTS"):
    try:
        W.update({k: float(v) for k, v in (kv.split("=") for kv in os.environ["CCXRAY_WEIGHTS"].split(","))})
    except Exception:
        pass
BIG_CTX = 200_000                                                    # "big context" line in the report
MIN_CTX = int(os.environ.get("CCXRAY_MIN_CONTEXT", "60000"))         # expired-cache guard ignores smaller sessions
REMIND_CTX = int(os.environ.get("CCXRAY_REMIND_CONTEXT", "150000"))  # first "this is getting large" note
REMIND_STEP = 100_000                                                # then again every this many tokens
ACK_SECS = 600             # after a warning, the same session may send for this long without another one
PENDING_SECS = 6 * 3600    # a handoff is offered to a new session in the same folder for this long
MAGIC = ("xray handoff", "ccxray handoff")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ------------------------------------------------------------------ log reading
def _ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _prompt(u):
    return u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)


def _ttl(u):
    """Cache lifetime in seconds this request wrote with, or None if it wrote nothing."""
    cc = u.get("cache_creation") or {}
    if cc.get("ephemeral_1h_input_tokens"):
        return 3600
    if cc.get("ephemeral_5m_input_tokens") or u.get("cache_creation_input_tokens"):
        return 300
    return None


def _units(u):
    cc = u.get("cache_creation") or {}
    cw1, cw5 = cc.get("ephemeral_1h_input_tokens", 0), cc.get("ephemeral_5m_input_tokens", 0)
    if not cw1 and not cw5:
        cw5 = u.get("cache_creation_input_tokens", 0)
    return {"inp": u.get("input_tokens", 0) * W["inp"], "cw": cw1 * W["cw1"] + cw5 * W["cw5"],
            "cr": u.get("cache_read_input_tokens", 0) * W["cr"], "out": u.get("output_tokens", 0) * W["out"]}


def _lines(path, tail_bytes=None):
    try:
        with open(path, "rb") as f:
            if tail_bytes:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - tail_bytes))
                if size > tail_bytes:
                    f.readline()          # drop the partial first line
            for raw in f:
                yield raw
    except OSError:
        return


def requests_in(path, tail_bytes=None):
    """API requests of one session log, in order: [ts, usage, model, n_tool_calls]. One entry per request id."""
    reqs, seen = [], {}
    for raw in _lines(path, tail_bytes):
        if b'"usage"' not in raw:
            continue
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        m = j.get("message")
        if j.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
            continue
        ntu = sum(1 for c in (m.get("content") or []) if isinstance(c, dict) and c.get("type") == "tool_use")
        mid = m.get("id") or j.get("requestId") or j.get("uuid")
        if mid in seen:
            r = reqs[seen[mid]]
            r[1] = m["usage"]; r[3] += ntu
        else:
            seen[mid] = len(reqs)
            reqs.append([_ts(j.get("timestamp")), m["usage"], m.get("model") or "?", ntu])
    return reqs


def session_state(path):
    """(last request time, context tokens, cache ttl seconds) of a session log, or None."""
    reqs = requests_in(path, tail_bytes=4_000_000)
    if not reqs:
        return None
    t, u, _model, _ = reqs[-1]
    ttl = next((x for x in (_ttl(r[1]) for r in reversed(reqs[-30:])) if x), None)
    if not t or not ttl:
        return None
    return t, _prompt(u), ttl


def _project(path):
    return os.path.relpath(path, LOGS).replace("\\", "/").split("/")[0]


def _load(name):
    try:
        with open(os.path.join(STATE, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(name, obj):
    try:
        os.makedirs(STATE, exist_ok=True)
        tmp = os.path.join(STATE, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, os.path.join(STATE, name))
    except OSError:
        pass


def _dur(sec):
    sec = int(sec)
    return f"{sec // 3600}h {sec % 3600 // 60:02}m" if sec >= 3600 else f"{sec // 60}m"


# ------------------------------------------------------------------ report
def analyse(days=None):
    cutoff = time.time() - days * 86400 if days else None
    A = {"units": collections.Counter(), "reqs": 0, "sessions": 0, "big_units": 0.0, "big_reqs": 0,
         "stale": [], "switch": [], "tiny_reqs": 0, "tiny_units": 0.0, "sess": [], "first": None, "last": None,
         "models": collections.Counter()}
    for path in glob.glob(os.path.join(LOGS, "**", "*.jsonl"), recursive=True):
        if cutoff and os.path.getmtime(path) < cutoff:
            continue
        reqs = requests_in(path)
        if cutoff:
            reqs = [r for r in reqs if r[0] is None or r[0] >= cutoff]
        if not reqs:
            continue
        A["sessions"] += 1
        s_units, peak, ttl, prev = 0.0, 0, None, None
        for t, u, model, ntu in reqs:
            un = _units(u); tot = sum(un.values()); ctx = _prompt(u)
            A["units"].update(un); A["reqs"] += 1; A["models"][model] += tot
            s_units += tot; peak = max(peak, ctx)
            if t:
                A["first"] = t if A["first"] is None else min(A["first"], t)
                A["last"] = t if A["last"] is None else max(A["last"], t)
            if ctx >= BIG_CTX:
                A["big_units"] += tot; A["big_reqs"] += 1
            if u.get("output_tokens", 0) < 200 and ntu:
                A["tiny_reqs"] += 1; A["tiny_units"] += un["cr"] + un["inp"]
            if prev is not None and ctx > 20000 and u.get("cache_read_input_tokens", 0) < 0.2 * ctx:
                pt, pmodel = prev
                gap = (t - pt) if (t and pt) else None
                extra = un["cw"] - u.get("cache_creation_input_tokens", 0) * W["cr"]   # cost beyond a cache hit
                ev = {"tokens": u.get("cache_creation_input_tokens", 0), "extra": extra, "gap": gap}
                if gap is not None and gap > (ttl or 300):
                    A["stale"].append(ev)
                elif model != pmodel:
                    A["switch"].append(ev)
            ttl = _ttl(u) or ttl
            prev = (t, model)
        A["sess"].append((s_units, peak, len(reqs), _project(path)))
    return A


def cmd_report(args):
    days = float(args[args.index("--days") + 1]) if "--days" in args else None
    A = analyse(days)
    tot = sum(A["units"].values())
    if not tot:
        print(f"No Claude Code session logs with usage found under {LOGS}")
        return
    pc = lambda x: 100.0 * x / tot
    stale_x = sum(e["extra"] for e in A["stale"]); switch_x = sum(e["extra"] for e in A["switch"])
    out = {
        "period_days": round((A["last"] - A["first"]) / 86400, 1) if A["first"] else None,
        "sessions": A["sessions"], "requests": A["reqs"],
        "share_cache_read_pct": round(pc(A["units"]["cr"]), 1), "share_cache_write_pct": round(pc(A["units"]["cw"]), 1),
        "share_output_pct": round(pc(A["units"]["out"]), 1),
        "big_context_cost_pct": round(pc(A["big_units"]), 1), "big_context_request_pct": round(100.0 * A["big_reqs"] / A["reqs"], 1),
        "stale_cache_rewrites": len(A["stale"]), "stale_cache_tokens": sum(e["tokens"] for e in A["stale"]),
        "stale_cache_extra_pct": round(pc(stale_x), 1),
        "model_switch_rewrites": len(A["switch"]), "model_switch_tokens": sum(e["tokens"] for e in A["switch"]),
        "model_switch_extra_pct": round(pc(switch_x), 1),
        "tiny_step_request_pct": round(100.0 * A["tiny_reqs"] / A["reqs"], 1), "tiny_step_cost_pct": round(pc(A["tiny_units"]), 1),
    }
    if "--json" in args:
        print(json.dumps(out, indent=1)); return
    d0 = time.strftime("%Y-%m-%d", time.localtime(A["first"])) if A["first"] else "?"
    d1 = time.strftime("%Y-%m-%d", time.localtime(A["last"])) if A["last"] else "?"
    P = print
    if "--share" in args:                 # no project names, safe to paste anywhere
        P(f"My Claude Code usage, {d0} to {d1} ({A['sessions']} sessions, {A['reqs']:,} requests), via ccxray:")
        P(f"- {out['big_context_cost_pct']:.0f}% went to requests with over {BIG_CTX // 1000}k tokens of context")
        P(f"- {out['stale_cache_extra_pct']:.1f}% went to re-sending conversations whose cache had expired ({len(A['stale'])} times)")
        if A["switch"]:
            P(f"- {out['model_switch_extra_pct']:.1f}% went to switching model in the middle of a session ({len(A['switch'])} times)")
        P(f"- split: {out['share_cache_read_pct']:.0f}% re-reading the conversation, {out['share_cache_write_pct']:.0f}% new content, {out['share_output_pct']:.0f}% output")
        return
    P(f"ccxray  {d0} to {d1}  |  {A['sessions']} sessions, {A['reqs']:,} API requests")
    P("")
    P("WHERE YOUR USAGE WENT (price-weighted)")
    P(f"  re-reading the conversation (cache reads)   {out['share_cache_read_pct']:5.1f}%")
    P(f"  new content written to cache                {out['share_cache_write_pct']:5.1f}%")
    P(f"  model output                                {out['share_output_pct']:5.1f}%")
    P("")
    P("FINDINGS")
    P(f"  1. Big conversations: {out['big_context_cost_pct']:.0f}% of usage came from requests made with more than "
      f"{BIG_CTX // 1000}k tokens of context ({out['big_context_request_pct']:.0f}% of requests).")
    if A["stale"]:
        big = max(A["stale"], key=lambda e: e["tokens"])
        P(f"  2. Expired cache: {len(A['stale'])} times you came back to a session after its cache had expired and the whole")
        P(f"     conversation was sent again ({out['stale_cache_tokens']:,} tokens). That cost {out['stale_cache_extra_pct']:.1f}% of your usage")
        P(f"     over what a cache hit would have. Largest: {big['tokens']:,} tokens after {_dur(big['gap'])} idle.")
    else:
        P("  2. Expired cache: no expired-cache rewrites found.")
    if A["switch"]:
        P(f"  3. Model switches: {len(A['switch'])} times a mid-session model change re-sent the conversation "
          f"({out['model_switch_tokens']:,} tokens, {out['model_switch_extra_pct']:.1f}% of usage).")
    P(f"  {'4' if A['switch'] else '3'}. Small steps: {out['tiny_step_request_pct']:.0f}% of requests were a tool call with almost no text; re-reading the")
    P(f"     conversation for them cost {out['tiny_step_cost_pct']:.1f}% of usage.")
    P("")
    P("MOST EXPENSIVE SESSIONS")
    for s_units, peak, n, proj in sorted(A["sess"], reverse=True)[:5]:
        P(f"  {pc(s_units):5.1f}%  {n:5} requests  peak context {peak // 1000:4}k  {proj[:48]}")
    top = sorted((s[0] for s in A["sess"]), reverse=True)
    k = max(1, len(top) // 10)
    P(f"  The top {k} session(s) used {pc(sum(top[:k])):.0f}% of everything.")
    P("")
    P("WHAT TO DO")
    P("  - Before leaving a big session, run /compact while its cache is still warm.")
    if A["stale"]:
        P("  - Coming back to an old, large session: type `xray handoff` there and continue in a new session.")
    P("  - Start a new session when the task changes; cost per turn grows with the size of the conversation.")
    if A["switch"]:
        P("  - Pick the model before a long session, not in the middle of it.")
    P("  `ccxray install-hook` does the first two for you.")
    if len(A["models"]) > 1:
        P("")
        P("  Note: shares use the same price weights for every model; with mixed models they are approximate.")


# ------------------------------------------------------------------ handoff
_NOISE = ("<local-command", "<command-", "<system-reminder", "<task-notification", "[SYSTEM", "Caveat:", "<bash-",
          "[Request interrupted")


def _clip(s, n):
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _blocks_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def build_handoff(path):
    """Extract (not summarise) what a session was about: requests, last replies, files, plan. Returns markdown."""
    prompts, finals, edits, reads, cmds = [], [], collections.Counter(), collections.Counter(), []
    plan, summary, cwd, branch, last_text = None, None, None, None, ""
    for raw in _lines(path):
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        m = j.get("message")
        if not isinstance(m, dict):
            continue
        cwd = j.get("cwd") or cwd; branch = j.get("gitBranch") or branch
        if j.get("type") == "user":
            text = _blocks_text(m.get("content")).strip()
            if j.get("isCompactSummary"):
                summary = text; continue
            if not text or j.get("isMeta") or text.startswith(_NOISE):
                continue
            if last_text:
                finals.append(last_text); last_text = ""
            prompts.append(text)
        elif j.get("type") == "assistant" and not j.get("isSidechain"):
            for c in m.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    last_text = c["text"]
                elif c.get("type") == "tool_use":
                    name, inp = c.get("name", ""), c.get("input") or {}
                    if name in ("Edit", "Write", "NotebookEdit") and inp.get("file_path"):
                        edits[inp["file_path"]] += 1
                    elif name == "Read" and inp.get("file_path"):
                        reads[inp["file_path"]] += 1
                    elif name in ("Bash", "PowerShell") and inp.get("command"):
                        cmds.append(" ".join(str(inp["command"]).split()))
                    elif name == "TodoWrite" and inp.get("todos"):
                        plan = [(t.get("status", "?"), t.get("content", "")) for t in inp["todos"] if isinstance(t, dict)]
                    elif name.endswith("update_plan") and inp.get("items"):
                        plan = [(t.get("status", "?"), t.get("content", "")) for t in inp["items"] if isinstance(t, dict)]
    if last_text:
        finals.append(last_text)
    if not prompts and not finals:
        return None
    st = session_state(path)
    head = ["# Handoff from a previous Claude Code session", ""]
    head.append(f"Folder: `{cwd or '?'}`" + (f"  |  branch: `{branch}`" if branch and branch != "HEAD" else "")
                + (f"  |  the old conversation was ~{st[1] // 1000}k tokens" if st else ""))
    head.append("This is an extract made by ccxray from the old session's log, not a summary written by a model. "
                "It holds the last replies, the user's requests and the files touched, newest information first. "
                "Check the files themselves before relying on a detail, and ask the user if something needed is missing.")
    o = list(head)
    if finals:                                        # the end of the conversation is what a new session needs most
        o += ["", "## Where the work stood: Claude's last replies (oldest first)"]
        tail = finals[-5:]
        for k, t in enumerate(tail):
            o += ["", f"### Reply {len(finals) - len(tail) + k + 1} of {len(finals)}", "", _clip(t, 4000 if k == len(tail) - 1 else 1500)]
    if plan:
        o += ["", "## Last task list", ""] + [f"- [{s}] {_clip(c, 160)}" for s, c in plan[:15]]
    rest = prompts[1:]
    if rest:
        o += ["", f"## The user's messages after the first, in order ({len(rest)} in total)", ""]
        if len(rest) > 22:
            shown = [(i, p) for i, p in enumerate(rest[:4], 2)] + [(None, None)] + [(len(rest) - 16 + i, p) for i, p in enumerate(rest[-18:])]
        else:
            shown = list(enumerate(rest, 2))
        for i, p in shown:
            o.append("- …" if p is None else f"{i}. {_clip(' '.join(p.split()), 450)}")
    if edits:
        o += ["", "## Files changed in that session", ""] + [f"- `{p}` ({n} edits)" for p, n in edits.most_common(25)]
    if reads:
        o += ["", "## Files read most", ""] + [f"- `{p}`" for p, _ in reads.most_common(10)]
    if cmds:
        o += ["", "## Last commands run", ""] + [f"- `{_clip(c, 180)}`" for c in cmds[-8:]]
    if prompts:
        o += ["", "## What the user asked for first", "", _clip(prompts[0], 1500)]
    if summary:
        o += ["", "## Older background: summary carried over from an earlier compaction", "", _clip(summary, 2500)]
    # Short version for the SessionStart hook: large hook context is moved to a file by Claude Code, so only this is inline.
    d = list(head)
    if finals:
        d += ["", "## Claude's last reply in the old session", "", _clip(finals[-1], 1800)]
    if prompts:
        d += ["", "## The user's last messages", ""] + [f"- {_clip(' '.join(p.split()), 300)}" for p in prompts[-3:]]
    return "\n".join(o) + "\n", "\n".join(d) + "\n"


def _key(cwd):
    return os.path.normcase(os.path.abspath(cwd or ".")).replace("\\", "/")


def write_handoff(transcript, cwd):
    built = build_handoff(transcript)
    if not built:
        return None
    md, digest = built
    d = os.path.join(STATE, "handoffs")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, os.path.splitext(os.path.basename(transcript))[0] + ".md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    pend = {k: v for k, v in _load("pending.json").items() if time.time() - v.get("ts", 0) < PENDING_SECS}
    pend[_key(cwd)] = {"path": out, "ts": time.time(), "from": os.path.basename(transcript), "digest": digest}
    _save("pending.json", pend)
    return out, len(md) // 4


def _find_transcript(arg, cwd):
    if arg and os.path.isfile(arg):
        return arg
    if arg:
        hits = glob.glob(os.path.join(LOGS, "*", arg + "*.jsonl"))
        return max(hits, key=os.path.getmtime) if hits else None
    proj = os.path.join(LOGS, re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd)))
    hits = glob.glob(os.path.join(proj, "*.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def cmd_handoff(args):
    arg = next((a for a in args if not a.startswith("--")), None)
    t = _find_transcript(arg, os.getcwd())
    if not t:
        print("No session log found. Pass a session id or a path to a .jsonl log."); return
    if "--print" in args:
        print((build_handoff(t) or ["nothing to extract"])[0]); return
    r = write_handoff(t, os.getcwd())
    if not r:
        print("Nothing to extract from that session."); return
    print(f"Handoff written: {r[0]} (~{r[1]:,} tokens)\n"
          f"Start a new session in this folder within {PENDING_SECS // 3600} hours and it is loaded automatically "
          f"(with the hooks installed), or tell Claude: read {r[0]} and continue.")


# ------------------------------------------------------------------ hooks
def prompt_decision(transcript, session_id, prompt, cwd=None, now=None):
    """UserPromptSubmit. Returns a hook output dict, or None to let the prompt through."""
    now = now or time.time()
    if os.environ.get("CCXRAY_GUARD", "1") == "0" or not transcript or not os.path.isfile(transcript):
        return None
    p = (prompt or "").strip()
    if p.lower().rstrip(".!") in MAGIC:
        r = write_handoff(transcript, cwd or os.getcwd())
        if not r:
            return {"decision": "block", "reason": "ccxray: nothing to hand off in this session yet."}
        return {"decision": "block", "reason":
                f"ccxray: handoff saved (~{r[1]:,} tokens, no tokens used). Open a new session in this folder and it is "
                f"loaded automatically. Or tell Claude there: read {r[0]} and continue."}
    if p.startswith("/"):
        return None                                   # /compact, /clear, /model ... must always go through
    st = session_state(transcript)
    if not st:
        return None
    t, ctx, ttl = st
    idle = now - t
    if ctx < MIN_CTX or idle <= ttl:
        return None
    acks = {k: v for k, v in _load("guard.json").items() if now - v < 86400}
    key = str(session_id or transcript)
    if now - acks.get(key, 0) < ACK_SECS:
        return None                                   # already warned; the user chose to send anyway
    acks[key] = now
    _save("guard.json", acks)
    # Wording follows what was measured on real sessions (see README).
    return {"decision": "block", "reason":
            f"ccxray: this session has been idle for {_dur(idle)}, so its prompt cache has expired. "
            f"Your message would send the whole conversation (~{ctx // 1000}k tokens) at the cache-write price again, "
            f"roughly 20 times what a turn costs here while the cache is warm. "
            f"Cheapest: type `xray handoff` (free), then continue in a new session. "
            f"/compact now costs almost as much as sending. "
            f"To send anyway, submit your message again (no warning for the next {ACK_SECS // 60} minutes)."}


def stop_decision(transcript, session_id, now=None):
    """Stop. Once per size step, tell the user the conversation is large and when its cache runs out."""
    now = now or time.time()
    if os.environ.get("CCXRAY_GUARD", "1") == "0" or not transcript or not os.path.isfile(transcript):
        return None
    st = session_state(transcript)
    if not st:
        return None
    t, ctx, ttl = st
    if ctx < REMIND_CTX:
        return None
    step = (ctx - REMIND_CTX) // REMIND_STEP
    seen = {k: v for k, v in _load("remind.json").items() if now - v.get("ts", 0) < 7 * 86400}
    key = str(session_id or transcript)
    if seen.get(key, {}).get("step", -1) >= step:
        return None
    seen[key] = {"step": step, "ts": now}
    _save("remind.json", seen)
    until = time.strftime("%H:%M", time.localtime(t + ttl))
    return {"systemMessage":
            f"ccxray: this conversation is now ~{ctx // 1000}k tokens, and every turn re-reads all of it. "
            f"Its cache lasts until about {until}. If you stop here, run /compact before then: "
            f"it is several times cheaper while the cache is warm, and coming back later costs about 20 times a normal turn."}


def start_decision(cwd, source, now=None):
    """SessionStart. Load a handoff written for this folder, once."""
    now = now or time.time()
    if os.environ.get("CCXRAY_GUARD", "1") == "0" or source not in (None, "startup", "clear"):
        return None
    pend = _load("pending.json")
    rec = pend.get(_key(cwd))
    if not rec or now - rec.get("ts", 0) > PENDING_SECS or not os.path.isfile(rec.get("path", "")):
        return None
    with open(rec["path"], encoding="utf-8") as f:
        full = f.read()
    ctx = (rec.get("digest") or full[:3000]) + (
        f"\nThe full handoff (~{len(full) // 4:,} tokens: earlier replies, all user messages, files changed, commands) is in "
        f"`{rec['path']}`. Read that file before you start work on the user's first request.\n")
    del pend[_key(cwd)]
    _save("pending.json", pend)
    return {"systemMessage": f"ccxray: handoff from your previous session is loaded (full file ~{len(full) // 4:,} tokens).",
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}


def _hook(fn):
    try:
        data = json.loads(sys.stdin.read() or "{}")
        out = fn(data)
        if os.environ.get("CCXRAY_DEBUG"):
            with open(os.environ["CCXRAY_DEBUG"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"event": data.get("hook_event_name"), "session": data.get("session_id"),
                                    "transcript_exists": os.path.isfile(data.get("transcript_path") or ""),
                                    "acted": sorted(out) if out else None}) + "\n")
        if out:
            print(json.dumps(out))
    except Exception:
        pass                                          # a hook must never break a session


# ------------------------------------------------------------------ install
EVENTS = {"UserPromptSubmit": "hook-prompt", "Stop": "hook-stop", "SessionStart": "hook-start"}


def _settings(home=None):
    return os.path.join(home or HOME, ".claude", "settings.json")


def _hook_command(sub):
    py = "python" if sys.platform == "win32" else (os.path.basename(sys.executable) or "python3")
    return f'{py} "{os.path.abspath(__file__).replace(chr(92), "/")}" {sub}'


def _strip(s):
    hooks = s.get("hooks", {})
    for event in list(hooks):
        for g in hooks[event]:
            g["hooks"] = [h for h in g.get("hooks", []) if "ccxray" not in h.get("command", "")]
        hooks[event] = [g for g in hooks[event] if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    if "hooks" in s and not s["hooks"]:
        del s["hooks"]


def cmd_install_hook(args, install=True):
    home = args[args.index("--home") + 1] if "--home" in args else None
    sp = _settings(home)
    s = {}
    if os.path.exists(sp):
        try:
            with open(sp, encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            print(f"{sp} is not valid JSON; fix it first. Nothing changed."); return
        bak = sp + ".ccxray.bak"
        if not os.path.exists(bak):
            import shutil
            shutil.copy2(sp, bak)
    elif not install:
        print("no settings found"); return
    _strip(s)
    if install:
        for event, sub in EVENTS.items():
            s.setdefault("hooks", {}).setdefault(event, []).append(
                {"hooks": [{"type": "command", "command": _hook_command(sub), "timeout": 15}]})
    os.makedirs(os.path.dirname(sp), exist_ok=True)
    tmp = sp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, sp)
    print(("hooks installed in " if install else "ccxray hooks removed from ") + sp)


def main(argv):
    cmd = argv[0] if argv else "help"
    if cmd == "report":
        cmd_report(argv[1:])
    elif cmd == "handoff":
        cmd_handoff(argv[1:])
    elif cmd in ("hook-prompt", "guard"):
        _hook(lambda d: prompt_decision(d.get("transcript_path"), d.get("session_id"), d.get("prompt"), d.get("cwd")))
    elif cmd == "hook-stop":
        _hook(lambda d: stop_decision(d.get("transcript_path"), d.get("session_id")))
    elif cmd == "hook-start":
        _hook(lambda d: start_decision(d.get("cwd") or os.getcwd(), d.get("source")))
    elif cmd == "install-hook":
        cmd_install_hook(argv[1:], True)
    elif cmd == "uninstall-hook":
        cmd_install_hook(argv[1:], False)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
