"""baxter_reaction_watch - the reaction-lifecycle self-audit (the owner, 8th July).

Guarantees every message of the owner's moves cleanly through 👀 (seen) -> ⚙️ (working)
-> ✅ (answered), and that a dropped or false reaction self-heals instead of him
having to spot it. A short-lived one-shot (like the fast lane): the watcher fires it
from the existing 15s slice loop, it self-throttles to ~once a minute, and each fire
loads fresh code- so there is no new long-lived process to babysit
([[long-lived-process-staleness]]).

VERIFY BEFORE REPAIRING (the HARD rule from the 14:11 incident). A ✅ is only ever
added when a genuinely DELIVERED same-channel native reply to that message exists- a
bot message whose message_reference points at it. Nothing is ticked off transcript
text or a cross-channel note ([[confirmations-must-be-reply-tool-calls]]). The three
real failures it heals:
  - DROPPED TICK: an answered message (real reply exists) stuck on 👀/⚙️ with no ✅
    (today's 13:45 rate-limited-ticker drop) -> complete it: eye on, cog off, tick on.
  - FALSE TICK: a ✅ sitting on a message with NO findable reply (the 14:08 guest-pass
    question that got wrongly re-ticked at 14:11) -> confirmed across two cycles (never
    a single racy poll), the misleading tick is REMOVED and the message surfaced. Never
    the reverse- an unanswered message is answered or flagged, never ticked.
  - PHANTOM COG: a ⚙️ 'actively working' mark with no ✅ and no delivered reply, older
    than any live worker's lifetime (PHANTOM_AFTER). The cog claims a Claude session is
    on it when none is- a worker that was stamped a cog then died before it ran, or a
    speculative pre-spawn stamp whose spawn failed. The lie is STRIPPED (the cog only,
    never the eye), and the seen-backstop + stuck escalation then surface the still-
    unanswered message honestly. A cog only ever belongs on a message a session is truly
    working right now (the 10th-July audit)- the reply worker now owns its own cog for
    exactly the span of its Claude turn.

Also: adds the 👀 seen-backstop to any un-reacted message of his, and escalates ONCE
(de-duped, quiet, one line) any message left with no reply past the stuck threshold-
the "nothing sits unanswered" guarantee. It does NOT speculatively add a ⚙️ (only the
lane actually working a message knows that) and it never composes an answer.

Read-mostly; the only writes are lifecycle reactions on the owner's own messages and, past
the stuck threshold, a single quiet ping to the owner. Never acts outward.

  python baxter_reaction_watch.py            # one throttled audit pass (the slice-loop entry)
  python baxter_reaction_watch.py --force    # ignore the ~60s self-throttle, audit now
  python baxter_reaction_watch.py --loop [s] # standalone poll loop (fallback; default 75s)
  python baxter_reaction_watch.py --report   # read-only: print the current lifecycle state
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(r"C:\Users\you\Documents\Baxter")
SECRETS = VAULT / ".baxter_secrets.json"
STATE = VAULT / ".baxter_reaction_audit.json"
LOCK = VAULT / ".baxter_reaction_watch.lock"
SAY = r"C:\Users\you\Documents\Python Scripts\utils\baxter_say.py"
UA = "DiscordBot (https://baxter.local, 1.0)"

EYE, GEAR, TICK, HANDSOFF = "👀", "⚙", "✅", "🤚"   # GEAR/others may carry a U+FE0F variation selector
GEAR_FULL, TICK_FULL = "⚙️", "✅"

# All channels Baxter converses in (the authoritative map- mirrors baxter_cogscan).
CHANNELS = {
    "general": "222222222222222201", "coc-farm": "222222222222222202",
    "deals": "222222222222222203", "assyst": "222222222222222204",
    "ai-abg": "222222222222222205", "finance-reels": "222222222222222206",
    "poe-bots": "222222222222222207", "deadlock-research": "222222222222222208",
    "coop-tasks": "222222222222222209",
}

MIN_INTERVAL = 60        # self-throttle: skip if the last real pass ran <60s ago (the
                         # slice loop fires us every 15s; ~60s cadence is what the owner asked)
FETCH_LIMIT = 50         # recent messages per channel to audit
EYE_GRACE = 30           # don't add the seen-backstop 👀 in a message's first 30s- give
                         # the live session / fast lane their react-on-read first
PHANTOM_AFTER = 1200     # 20 min: a ⚙️ 'actively working' mark with no ✅ and no delivered
                         # reply, older than this, has NO live session behind it- every reply
                         # worker self-terminates at ~15 min (baxter_reply_worker.TURN_TIMEOUT)
                         # and drops its own cog, so a cog surviving past 20 min is a corpse's
                         # speculative stamp, never real work. Strip it (the 10th-July audit).
STUCK_AFTER = 1800       # 30 min with no delivered reply -> surface it to the owner (once)
STUCK_MIN_LEN = 4        # skip trivial acks ("ok", "ty") from the stuck escalation
# Channels whose asks are answered by a SEPARATE daemon that doesn't use native replies
# (so "no message_reference" is NOT proof it went unanswered)- excluded from the stuck
# escalation to avoid false alarms ([[coc-farm-dual-owner]]). The tick/eye repairs still
# run there; only the "you weren't answered" ping is held back.
STUCK_SKIP_CHANNELS = {"coc-farm"}
DISCORD_EPOCH = 1420070400000


def _secrets():
    return json.loads(SECRETS.read_text(encoding="utf-8-sig"))


def _read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(d):
    try:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        os.replace(tmp, STATE)
    except Exception:
        pass


def _snowflake_age(mid):
    """Seconds since a Discord message id (snowflake) was created."""
    try:
        ms = (int(mid) >> 22) + DISCORD_EPOCH
        return max(0.0, datetime.now(timezone.utc).timestamp() - ms / 1000.0)
    except Exception:
        return 0.0


def _snowflake_clock(mid):
    """UK-local 'HH:MM' for the message's creation time (for the stuck-escalation line)."""
    try:
        ms = (int(mid) >> 22) + DISCORD_EPOCH
        return datetime.fromtimestamp(ms / 1000.0).strftime("%H:%M")
    except Exception:
        return "?"


def _get(url, tok):
    req = urllib.request.Request(url, headers={"Authorization": f"Bot {tok}", "User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())


def _react(ch, mid, emoji, method, tok):
    """PUT/DELETE a bot reaction, honouring the reaction-route rate limit. Returns
    True on a landed 2xx (the NEXT audit cycle re-verifies, so a lost write self-heals)."""
    url = (f"https://discord.com/api/v10/channels/{ch}/messages/{mid}"
           f"/reactions/{urllib.parse.quote(emoji)}/@me")
    headers = {"Authorization": f"Bot {tok}", "User-Agent": UA}
    if method == "PUT":
        headers["Content-Length"] = "0"
    req = urllib.request.Request(url, method=method, headers=headers)
    for attempt in range(4):
        try:
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(float(e.headers.get("Retry-After") or 2) + 0.5)
                continue
            return False
        except Exception:
            return False
    return False


def _say(msg):
    """One quiet line to the owner (phone push). Used only for a genuinely stuck message.

    Returns True only if it actually left. Exit 3 is a DENIAL: baxter_say refused the claim,
    printed why, and sent nothing- record the reason in the voiceless denial sink and hand back
    a False the caller must never read as a delivered ping
    ([[outward-path-proven-from-the-send-ledger]])."""
    import subprocess
    try:
        p = subprocess.run(["python", SAY, msg], timeout=30, capture_output=True)
    except Exception:
        return False
    if getattr(p, "returncode", 0) == 3:
        try:
            import baxter_autobuild as autobuild
            autobuild.denial_alert("baxter_reaction_watch._say", str(msg)[:80], p.stderr)
        except Exception:
            pass
        return False
    return True


def _reaction_flags(m):
    names = [(r.get("emoji") or {}).get("name") or "" for r in (m.get("reactions") or [])]
    return (any(EYE in n for n in names), any(GEAR in n for n in names),
            any(TICK in n for n in names), any(HANDSOFF in n for n in names))


def audit_once(tok, owner_id, bot_id, state, repair=True, silence_stuck=False,
               stuck_after=STUCK_AFTER):
    """Scan every channel once. Returns a summary dict. Mutates `state` (the escalation
    dedup map), and when repair=True applies the safe self-heals. silence_stuck marks the
    current stuck backlog as already-surfaced WITHOUT pinging- used on the first live run
    so the watcher never replays hours-old history as a fresh alarm.

    stuck_after overrides the 'no reply for this long -> stuck' threshold. The 30-min
    default is right for the steady-state watcher; the resurrection reply-audit passes 0
    so EVERY Baxter-addressed message with no delivered reply is surfaced (an outage may
    have swallowed a reply that landed only seconds ago). Read-only callers pair this with
    repair=False to get the unanswered set without touching reactions or pinging."""
    repairs, stuck, false_ticks, phantoms, clean = [], [], [], [], 0
    escalated = state.setdefault("escalated", {})

    for name, ch in CHANNELS.items():
        try:
            msgs = _get(f"https://discord.com/api/v10/channels/{ch}/messages?limit={FETCH_LIMIT}", tok)
        except Exception:
            continue
        # ids Baxter has DELIVERED a native reply to (a real message_reference), our
        # only trusted "answered" signal- transcript text never reaches here.
        replied_ids = {
            str((m.get("message_reference") or {}).get("message_id"))
            for m in msgs
            if str((m.get("author") or {}).get("id")) == str(bot_id)
            and (m.get("message_reference") or {}).get("message_id")
        }
        for m in msgs:
            if str((m.get("author") or {}).get("id")) != str(owner_id):
                continue
            mid = str(m["id"])
            eye, cog, tick, handsoff = _reaction_flags(m)
            replied = mid in replied_ids
            age = _snowflake_age(mid)
            content = (m.get("content") or "").replace("\n", " ").strip()
            snip = content[:50]

            # is this message CLEARLY addressed to Baxter? (an @mention of the bot, or a
            # reply to one of the bot's own messages). Only these carry a Baxter reply
            # expectation- ambient dumps and asks aimed at Codex/Jem don't.
            ref = m.get("message_reference") or {}
            reply_to_bot = str(((m.get("referenced_message") or {}).get("author") or {}).get("id")) == str(bot_id)
            addressed = (f"<@{bot_id}>" in content or f"<@!{bot_id}>" in content
                         or (bool(ref.get("message_id")) and reply_to_bot))

            if replied:
                if not tick:
                    # DROPPED TICK: verified real reply, but the tick never landed
                    # (today's 13:45 rate-limited-ticker drop). Safe to complete- the
                    # delivered reply is the proof the tick is earned.
                    if repair:
                        if not eye:
                            _react(ch, mid, EYE, "PUT", tok)
                        _react(ch, mid, GEAR_FULL, "DELETE", tok)   # clear the working cog
                        _react(ch, mid, TICK_FULL, "PUT", tok)      # stamp the earned tick
                    repairs.append((name, mid, "completed (dropped tick)", snip))
                else:
                    clean += 1
                continue

            # --- not replied ---
            if handsoff:
                continue   # 🤚 = seen + correctly not mine to action; no reply owed

            # PHANTOM COG (the 10th-July audit): a ⚙️ 'actively working' mark with no ✅ and
            # no delivered reply, aged past any live worker's lifetime. A worker owns its cog
            # for exactly its Claude turn and drops it when done, so a cog surviving PHANTOM_AFTER
            # has no session behind it- the 'on it' signal is a lie. Strip the cog only (never
            # the 👀), leaving the message honestly un-worked; the seen-backstop and stuck
            # escalation below then surface it. Age is the trusted signal: baxter_say completes
            # the lifecycle (✅ on, ⚙️ off) the instant a reply lands, so a bare ⚙️-without-✅
            # this old inherently means no reply ever completed and no worker is still running.
            if cog and not tick and age >= PHANTOM_AFTER:
                if repair and _react(ch, mid, GEAR_FULL, "DELETE", tok):
                    repairs.append((name, mid, "stripped phantom cog", snip))
                phantoms.append((name, mid, snip))

            # FALSE TICK (the 14:11 bug): a ✅ sitting on a message with no findable
            # delivered reply. VERIFY-BEFORE-REPAIR means we NEVER re-tick an unanswered
            # message- but stripping a tick the owner has already seen is its own destructive
            # guess (the tick may mark a plain-send ack or a deliberate stand-back), so we
            # FLAG, never remove ([[confirmations-must-be-reply-tool-calls]]). If it's a
            # Baxter-addressed ask it rides the same stuck escalation below.
            if tick and addressed:
                false_ticks.append((name, mid, snip))

            # seen-backstop: an un-reacted message of his, past the react-on-read grace,
            # gets a 👀 so nothing sits with zero lifecycle marks.
            if repair and not eye and not cog and not tick and age >= EYE_GRACE:
                if _react(ch, mid, EYE, "PUT", tok):
                    repairs.append((name, mid, "added seen backstop", snip))

            # "nothing sits unanswered": a Baxter-addressed message with no delivered
            # reply past the threshold is surfaced to the owner ONCE (de-duped by id). Daemon
            # channels (coc-farm answers without native replies) are excluded.
            if (age >= stuck_after and len(content) >= STUCK_MIN_LEN and addressed
                    and name not in STUCK_SKIP_CHANNELS and mid not in escalated):
                stuck.append((name, ch, mid, snip))

    # escalate the stuck set as ONE quiet line (never per-message spam).
    if repair and stuck:
        for name, ch, mid, snip in stuck:
            escalated[mid] = datetime.now().isoformat()
        if silence_stuck:
            pass   # first live run- backlog is now marked seen, no retroactive ping
        elif len(stuck) == 1:
            name, ch, mid, snip = stuck[0]
            _say(f"⚠️ Sir- your message in #{name} from {_snowflake_clock(mid)} "
                 f"(“{snip}”) hasn't had a reply land yet- flagging it so it's not lost.")
        else:
            lines = "\n".join(f"- #{n} {_snowflake_clock(mid)}: “{s}”" for n, c, mid, s in stuck[:5])
            _say(f"⚠️ Sir- {len(stuck)} messages of yours are still without a reply:\n{lines}")

    # keep the counters from growing unbounded (only recent ids matter).
    if len(escalated) > 200:
        for k in list(escalated)[:-100]:
            escalated.pop(k, None)
    state["repairs"] = (state.get("repairs", []) + [
        {"ts": datetime.now().isoformat(), "ch": n, "mid": mid, "kind": k, "snip": s}
        for n, mid, k, s in repairs])[-50:]
    return {"repairs": repairs, "stuck": stuck, "false_ticks": false_ticks,
            "phantoms": phantoms, "clean": clean}


def _run(force=False, repair=True):
    sec = _secrets()
    tok = sec["discord_bot_token"]
    owner_id = str(sec.get("discord_only_user_id", ""))
    state = _read_state()
    now = datetime.now().timestamp()
    if not force:
        try:
            if (now - datetime.fromisoformat(state.get("last_run", "1970-01-01")).timestamp()) < MIN_INTERVAL:
                return None   # throttled- another slice already audited this minute
        except Exception:
            pass
    # single-flight: a stale >120s lock (crashed holder) is stolen, else we bow out.
    if LOCK.exists() and not force:
        try:
            if (now - LOCK.stat().st_mtime) < 120:
                return None
        except Exception:
            pass
    try:
        LOCK.write_text(datetime.now().isoformat())
    except Exception:
        pass
    try:
        bot_id = state.get("bot_id")
        if not bot_id:
            bot_id = str(_get("https://discord.com/api/v10/users/@me", tok)["id"])
            state["bot_id"] = bot_id
        first_live = repair and not state.get("initialized")
        summary = audit_once(tok, owner_id, bot_id, state, repair=repair, silence_stuck=first_live)
        if first_live:
            state["initialized"] = True
        state["last_run"] = datetime.now().isoformat()
        _write_state(state)
        return summary
    finally:
        try: LOCK.unlink()
        except Exception: pass


def _selftest():
    """Prove the PHANTOM-COG reaper end to end, hermetically (no network, no Discord).

    Stubs the module's I/O (_get, _react, _say, _snowflake_age) and drives audit_once over
    four fixtures, asserting the exact reaction calls:
      p1  aged ⚙️, no ✅, no reply   -> STRIPPED (the phantom; DELETE ⚙️), listed in phantoms.
      f1  fresh ⚙️ (age < gate)      -> UNTOUCHED (a worker may still be live on it).
      d1  ⚙️ + ✅ (already answered)  -> UNTOUCHED (has a tick; the lifecycle completed).
      r1  aged ⚙️ WITH a real reply   -> COMPLETED (✅ stamped), NOT a phantom.
    Sabotage the reaper (remove the block, or drop the age gate) and this goes red: the
    phantom is not stripped, or the fresh/answered cog is wrongly torn off.
    """
    import sys as _sys
    mod = _sys.modules[__name__]
    OWNER, BOT, GEN = "1000", "2000", CHANNELS["general"]
    calls = []                                   # (ch, mid, emoji, method)
    ages = {"p1": 1500, "f1": 60, "d1": 1500, "r1": 1500, "b1": 1500}

    def _rx(*names):
        return [{"emoji": {"name": n}} for n in names]

    fixtures = [
        {"id": "p1", "author": {"id": OWNER}, "content": "phantom- nobody is on this",
         "reactions": _rx(GEAR_FULL)},
        {"id": "f1", "author": {"id": OWNER}, "content": "fresh- a worker just started",
         "reactions": _rx(GEAR_FULL)},
        {"id": "d1", "author": {"id": OWNER}, "content": "already answered and ticked",
         "reactions": _rx(GEAR_FULL, TICK_FULL)},
        {"id": "r1", "author": {"id": OWNER}, "content": "answered, tick just dropped",
         "reactions": _rx(GEAR_FULL)},
        {"id": "b1", "author": {"id": BOT}, "content": "the delivered reply",
         "reactions": [], "message_reference": {"message_id": "r1"}},
    ]

    def fake_get(url, tok):
        return list(fixtures) if GEN in url else []

    def fake_react(ch, mid, emoji, method, tok):
        calls.append((ch, mid, emoji, method))
        return True

    orig = (mod._get, mod._react, mod._say, mod._snowflake_age)
    mod._get = fake_get
    mod._react = fake_react
    mod._say = lambda *a, **k: True
    mod._snowflake_age = lambda mid: float(ages.get(str(mid), 0))
    try:
        summary = audit_once(tok="x", owner_id=OWNER, bot_id=BOT, state={}, repair=True)
    finally:
        mod._get, mod._react, mod._say, mod._snowflake_age = orig

    fails = []
    phantom_ids = {mid for _n, mid, _s in summary["phantoms"]}
    if phantom_ids != {"p1"}:
        fails.append(f"phantom set should be {{'p1'}}, got {phantom_ids}")
    if (GEN, "p1", GEAR_FULL, "DELETE") not in calls:
        fails.append(f"the phantom cog on p1 was not stripped: {calls}")
    if any(mid == "f1" for _c, mid, _e, _m in calls):
        fails.append(f"a FRESH cog (f1) was torn off- the age gate is not honoured: {calls}")
    if any(mid == "d1" for _c, mid, _e, _m in calls):
        fails.append(f"an already-ticked message (d1) was touched: {calls}")
    if (GEN, "r1", TICK_FULL, "PUT") not in calls:
        fails.append(f"an answered message (r1) was not completed with a tick: {calls}")
    if "r1" in phantom_ids:
        fails.append("an ANSWERED message (r1) was wrongly flagged a phantom cog")

    if fails:
        for f in fails:
            print(f"PHANTOM-COG SELFTEST FAIL: {f}")
        return 1
    print("PHANTOM-COG SELFTEST OK")
    return 0


def main(argv):
    if "--report" in argv:
        summary = _run(force=True, repair=False)
        if summary is None:
            print("audit skipped"); return 0
        print(f"clean(ticked)={summary['clean']} | repairs_needed={len(summary['repairs'])} "
              f"| false_ticks={len(summary['false_ticks'])} | phantom_cogs={len(summary['phantoms'])} "
              f"| stuck={len(summary['stuck'])}")
        for n, mid, k, s in summary["repairs"]:
            print(f"  REPAIR [{n}] {mid} {k} | {s!r}")
        for n, mid, s in summary["false_ticks"]:
            print(f"  FALSE-TICK [{n}] {mid} | {s!r}")
        for n, mid, s in summary["phantoms"]:
            print(f"  PHANTOM-COG [{n}] {mid} | {s!r}")
        for n, ch, mid, s in summary["stuck"]:
            print(f"  STUCK [{n}] {mid} | {s!r}")
        return 0
    if "--selftest" in argv:
        return _selftest()
    if "--loop" in argv:
        i = argv.index("--loop")
        interval = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 75
        print(f"reaction-watch loop every {interval}s (Ctrl+C to stop)")
        while True:
            try:
                s = _run(force=True)
                if s and (s["repairs"] or s["false_ticks"] or s["stuck"]):
                    print(f"[{datetime.now():%H:%M:%S}] repairs={len(s['repairs'])} "
                          f"false_ticks={len(s['false_ticks'])} stuck={len(s['stuck'])}")
            except Exception as e:
                print(f"audit error: {e}")
            time.sleep(interval)
    force = "--force" in argv
    s = _run(force=force)
    if s and (s["repairs"] or s["false_ticks"] or s["phantoms"] or s["stuck"]):
        print(f"repairs={len(s['repairs'])} false_ticks={len(s['false_ticks'])} "
              f"phantom_cogs={len(s['phantoms'])} stuck={len(s['stuck'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
