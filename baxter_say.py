"""baxter_say — Baxter's voice in the owner's Discord server (Potaters Doohickeys ONLY).

Used by triage runs to answer/clarify and by the reminder engine to ping his phone.
Posts only to the owner's own channels — never to anyone else, never outside the server.

  python baxter_say.py "message"                          -> #general, with @mention
  python baxter_say.py --reply-to <message_id> "message"  -> native Discord REPLY to that
                                                             message (pings via the reply
                                                             itself; no @ prefix added)
  python baxter_say.py --channel <key-or-id> "message"    -> key (general/activity-log/
                                                             drafts/plans) or a raw id
  python baxter_say.py --no-mention "message"             -> no @mention (logs, archives)
  python baxter_say.py --force "message"                  -> bypass the double-send guard
  python baxter_say.py -- "--literally dashed text"       -> everything after `--` is content
  python baxter_say.py --help                             -> this text, sends nothing

An unknown `--flag` is an ERROR, never message text: it exits 2 to stderr and posts
nothing (9th July- `--help` was posted verbatim and pinged his phone).

EXIT CODES
  0  sent, or suppressed by the double-send guard (a dedupe is a success: he got the reply)
  1  no message text given
  2  usage error- a bare value-flag, an unknown flag, or an unknown channel
  3  claim denied: the message quotes a queue slot the queue is not holding

WHY THE CLAIM CHECK LIVES HERE (10th July). baxter_preannounce_guard is registered as a
PreToolUse hook, and a hook fires only on a CLAUDE tool call. _send_ack, baxter_reminders,
baxter_autobuild and baxter_reply_worker all post by `subprocess.run([python, this_file])`,
so the hook never saw a single one of them. This file is the one door every outward path
uses, so the check sits here too- before the secrets load, the channel resolve, the --force
branch, the dedup claim and the POST. The hook keeps the half only it can judge: whether a
real work tool-call landed in the current turn. See _claim_guard.
"""
import json, os, sys, urllib.request
from datetime import datetime

SECRETS = r"C:\Users\you\Documents\Baxter\.baxter_secrets.json"
UA = "DiscordBot (https://baxter.local, 1.0)"
CORRUPT_LOG = r"C:\Users\you\Documents\Baxter\.baxter_corruption.log"

# shared double-send guard (one consolidated reply per inbound message)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import baxter_send_dedup as _dedup
except Exception:
    _dedup = None
# outbound text hygiene: repairs UTF-8/cp1252 mojibake + the banned " / " paragraph-break
# shorthand, on every message that leaves through this door (the owner, 8th July).
try:
    import baxter_text as _text
except Exception:
    _text = None


def _sanitise(msg):
    """Clean a message on its way out. An UNREPAIRABLE mojibake means something upstream
    is still decoding Baxter's UTF-8 through the Windows codepage and has destroyed bytes
    we cannot get back- send it anyway (silence is worse) but record it loudly so the
    corrupting caller can be found."""
    if not _text or not msg:
        return msg
    if _text.is_lossy_mojibake(msg):
        try:
            with open(CORRUPT_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] UNREPAIRABLE mojibake from "
                        f"{os.environ.get('BAXTER_CALLER', 'unknown caller')}: {msg[:200]!r}\n"
                        f"    -> a caller decoded UTF-8 as cp1252 with errors='ignore'; "
                        f"bytes are gone. Fix the capture site (encoding='utf-8').\n")
        except Exception:
            pass
    return _text.clean_outbound(msg)

VALUE_FLAGS = {"--channel", "--reply-to"}      # take exactly one argument
BOOL_FLAGS = {"--no-mention", "--force"}       # take none


def resolve_channel(key):
    """channel key -> live channel id. Offline, no HTTP, and it NEVER raises.

    The persisted map (discord_channels) is consulted BEFORE the raw-id fallback, so a
    name like 'fortnite-map-creation' becomes an id here and never reaches the API as a
    name. An unknown key returns None- the caller must refuse to send rather than POST
    the key itself, which is how a channel name once reached /channels/<name>/messages
    and 404'd.
    """
    key = str(key or "").strip()
    if not key:
        return None
    try:
        import discord_channels
        cid = discord_channels.resolve(key)
        if cid:
            return str(cid)
    except Exception:
        pass                       # a broken map must not take the voice down
    return key if key.isdigit() else None


def _claim_guard(msg):
    """(allowed, reason) for any queue slot `msg` quotes, judged against the live queue.

    The guard is imported LAZILY, never at module scope: baxter_say is imported by workers
    that the guard may one day want to read, and a module-scope import here would make that
    a cycle. It also keeps an ordinary reminder ping free of the cost entirely.

    `evaluate` is called, never re-implemented. Work and queue evidence are passed as
    SATISFIED because a bare subprocess cannot see the turn- only the hook can read the
    transcript. What survives in-process is the position rule, which needs nothing but the
    queue file. A second copy of that rule here would drift from the guard's on the next
    regex change, silently, and the two halves would disagree about the same sentence.

    FAILS OPEN, everywhere. A missing guard, an unreadable or absent queue (position_verdict
    returns None), or any exception raised in here allows the send. A guard that can wedge
    his voice costs more than the lie it stops- the same bias the hook already documents.
    """
    try:
        import baxter_preannounce_guard as guard
        if not (guard.QUEUE_CLAIM.search(msg) or guard.claims_queue_state(msg)):
            return True, ""        # claims nothing- never even open the queue file
        return guard.evaluate(msg, True, True, position_ok=guard.position_verdict(msg))
    except Exception:
        return True, ""


def main():
    args = sys.argv[1:]
    _pre = args[:args.index("--")] if "--" in args else args   # help only BEFORE the escape
    if "--help" in _pre or "-h" in _pre:
        print(__doc__.strip())
        return 0
    channel_key, mention, reply_to, force = "general", True, None, False
    msg_parts = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":                 # explicit end-of-flags: the rest is content, verbatim
            msg_parts.extend(args[i + 1:]); break
        if a in VALUE_FLAGS:
            if i + 1 >= len(args):
                # a bare value-flag used to fall through into msg_parts and get POSTED
                print(f"baxter_say: {a} needs a value; nothing sent", file=sys.stderr)
                return 2
            if a == "--channel": channel_key = args[i + 1]
            else:                reply_to = args[i + 1]
            i += 2; continue
        if a in BOOL_FLAGS:
            if a == "--no-mention": mention = False
            else:                   force = True
            i += 1; continue
        if a.startswith("--"):
            # NEVER treat an unrecognised flag as message text (9th July: `--help` was
            # posted to #general as '<@...> --help' and pinged his phone). Real content
            # that must begin with dashes goes after a bare `--`.
            print(f"baxter_say: unknown flag {a!r}; nothing sent. "
                  f"Use `--` before dashed message text, or --help.", file=sys.stderr)
            return 2
        msg_parts.append(a); i += 1
    msg = " ".join(msg_parts).strip()
    if not msg:
        print("no message"); return 1
    # Hygiene BEFORE the footer and before the dedup claim, so the guarded text is exactly
    # the text that gets posted.
    msg = _sanitise(msg)
    # Exact per-turn token footer (the owner's standing order). ONLY on live conversational
    # replies the owner reads - never on --no-mention Workshop logs, and only when this send
    # is fired by the live `--channels` session (footer_if_live self-checks and returns
    # '' otherwise, so workers/logs are untouched). OUTPUT tokens = the clean, exact,
    # measured figure. Appended before the dedup claim so what's guarded == what's sent.
    if mention or reply_to:
        try:
            import baxter_turntokens
            _foot = baxter_turntokens.footer_if_live()
            if _foot:
                msg = f"{msg}\n-# {_foot}"
        except Exception:
            pass
    # `msg` is now EXACTLY the text that would be posted, so it is exactly the text judged.
    # This is deliberately the first gate after that point: before the secrets, the channel,
    # the --force branch, the dedup claim and the POST. --force bypasses the double-send
    # guard; it has never licensed a lie, and it does not start now.
    _allowed, _why = _claim_guard(msg)
    if not _allowed:
        try:
            import baxter_preannounce_guard as _guard
            _guard.log_reject(msg, _why)
        except Exception:
            pass
        print(f"baxter_say: claim denied- {_why}; nothing sent", file=sys.stderr)
        return 3
    sec = json.load(open(SECRETS, encoding="utf-8-sig"))
    tok = sec["discord_bot_token"]; uid = str(sec.get("discord_only_user_id", ""))
    channels = {
        "general": str((sec.get("discord_channels") or ["222222222222222201"])[0]),
        "reminders": str(sec.get("discord_reminders_channel", "")),   # alias -> general in single-room mode
    }
    # "🗄️ the workshop" category — Baxter's own output log (activity-log / drafts / plans).
    # These are the archive channels; #general stays the live dialogue room.
    try:
        import os
        wpath = os.path.join(os.path.dirname(SECRETS), ".baxter_workshop.json")
        wk = json.load(open(wpath, encoding="utf-8-sig")).get("channels", {})
        for k, v in wk.items():
            channels[k] = str(v)   # keys: activity-log, drafts, plans
    except Exception:
        pass
    # built-in keys (general/reminders/activity-log) win; then the persisted channel map;
    # then a raw numeric id. An unknown key sends NOTHING- posting it would 404.
    ch = channels.get(channel_key) or resolve_channel(channel_key)
    if not ch:
        sys.stderr.write(f"unknown channel {channel_key!r} - nothing sent\n")
        return 2
    # double-send guard: ATOMIC claim BEFORE the POST (closes the check-then-record race).
    # If the POST then fails we release the claim so a real retry isn't blocked.
    claimed = False
    if _dedup and not force:
        ok, why = _dedup.claim(ch, reply_to, msg)
        if not ok:
            print(f"deduped ({why}) - not sent")
            return 0
        claimed = True
    if reply_to:
        # a native reply pings the author by itself - no @ prefix needed
        payload = {"content": msg[:1900],
                   "message_reference": {"message_id": str(reply_to), "channel_id": ch,
                                          "fail_if_not_exists": False},
                   "allowed_mentions": {"parse": [], "replied_user": True}}
    else:
        content = f"<@{uid}> {msg}" if (mention and uid) else msg
        payload = {"content": content[:1900],
                   "allowed_mentions": {"users": [uid] if (mention and uid) else []}}
    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{ch}/messages",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json", "User-Agent": UA},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception:
        # the send never left- release the claim so the retry path can re-attempt
        if claimed:
            try: _dedup.release_claim(ch, reply_to)
            except Exception: pass
        raise
    if reply_to:                                     # a reply = completion -> stamp target ✅, drop ⚙️
        # ✅ FIRST (the "answered" signal the owner reads) with a retry, THEN drop the ⚙️ cog.
        # Order + retry matter: the old code did DELETE ⚙️ then a single PUT ✅ that got
        # silently eaten by a transient 429/timeout, leaving his message stuck on 👀.
        import urllib.parse as _up, time as _t
        def _reactcall(_method, _emoji, _tries):
            for _n in range(_tries):
                try:
                    _h = {"Authorization": f"Bot {tok}", "User-Agent": UA}
                    if _method == "PUT":
                        _h["Content-Length"] = "0"
                    _rr = urllib.request.Request(
                        f"https://discord.com/api/v10/channels/{ch}/messages/{reply_to}"
                        f"/reactions/{_up.quote(_emoji)}/@me", method=_method, headers=_h)
                    urllib.request.urlopen(_rr, timeout=8)
                    return True
                except urllib.error.HTTPError as _e:
                    if _e.code == 429:               # rate-limited- honour retry-after then retry
                        try: _t.sleep(min(3.0, float(_e.headers.get("Retry-After", "1")) + 0.2))
                        except Exception: _t.sleep(1.0)
                        continue
                    return False                     # 4xx that isn't rate-limit- don't spin
                except Exception:
                    _t.sleep(0.5)
            return False
        _reactcall("PUT", "✅", 3)                    # the tick- retried so it can't quietly vanish
        _reactcall("DELETE", "⚙️", 1)                # clear the working-cog (best-effort)
    print("said")
    return 0

if __name__ == "__main__":
    sys.exit(main())
