"""baxter_reminders — durable ad-hoc reminder store + poller (Atul, 8th July 22:24).

The trust bug this fixes: a confirmed reminder ("ping me in 10 mins to hop on CoC")
lived only as a chat acknowledgement- nothing scheduled the fire, so it evaporated the
moment the turn ended and no ping ever came. From now on a reminder is only real once
it is written here; the acknowledgement is a lie until then.

Two halves, both deterministic (NO claude burn):
  1. ADD  - the live/fast lane calls this the instant it confirms ANY ad-hoc reminder.
            Parses the relative offset / clock time into a concrete fire-time, writes it
            to .baxter_reminders.json, and prints the RESOLVED time so the reply can echo
            it back verifiably. Non-zero exit = it did NOT schedule- do not acknowledge.
  2. FIRE - baxter_triage's ~60s loop calls fire_due(), which sends any now-due reminder
            via baxter_say (pings his phone) and clears it.

CLI:
  python baxter_reminders.py --add --when "in 10 mins" --text "hop on CoC"
  python baxter_reminders.py --add --at "21:00" --text "take the cream" --channel general
  python baxter_reminders.py --add --in "1h30m" --text "check the oven"
  python baxter_reminders.py --fire          # send everything due (triage calls this)
  python baxter_reminders.py --list          # show pending (debug)
"""
import json, os, re, subprocess, sys, time, uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baxter_autobuild as autobuild   # the voiceless denial sink; see _send()

VAULT = r"C:\Users\you\Documents\Baxter"
STORE = os.path.join(VAULT, ".baxter_reminders.json")
LOCK = os.path.join(VAULT, ".baxter_reminders.lock")
SAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baxter_say.py")
LOG = os.path.join(VAULT, ".baxter.log")

# Instruction injected into the live/fast/listener reply prompts so EVERY Baxter that
# talks to Atul knows to register a real trigger before it acknowledges a reminder.
REMINDER_RULE = (
    "REMINDERS/TIMERS: if he asks you to remind/ping/nudge him at or after some time "
    "('remind me in 10 mins', 'ping me in an hour', 'nudge me at 9pm', 'in 2h remind me to X'), "
    "you MUST register a real scheduled trigger BEFORE you acknowledge it- a confirmation you "
    "haven't scheduled is a silent failure (his 8th-July bug). Run, as your action:\n"
    f"  python \"{os.path.join(os.path.dirname(os.path.abspath(__file__)), 'baxter_reminders.py')}\" "
    "--add --when \"<his exact time phrase, e.g. 'in 10 mins' or 'at 9pm'>\" --text \"<what to remind him>\""
    " [--channel <this channel id>]\n"
    "It prints the RESOLVED fire-time. Only then reply, confirming with that resolved time "
    "('Done, sir- I'll ping you at 9:42pm.'). If it errors / prints no time, tell him you "
    "couldn't set it- never claim a timer you didn't schedule.\n"
)

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] reminders: {msg}\n")
    except Exception:
        pass


def parse_duration(s):
    """Sum every <number><unit> pair in s -> seconds, or None if there are none.
    Handles '10m', '10 mins', '1h30m', '90 seconds', plus bare 'an hour'/'half an hour'."""
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", s):
        if unit in _UNITS:
            total += float(num) * _UNITS[unit]
            found = True
    if found:
        return total
    if "half an hour" in s or "half hour" in s:
        return 1800
    if re.search(r"\ban?\s+hour\b", s):
        return 3600
    if re.search(r"\ban?\s+min", s):
        return 60
    return None


def parse_clock(s, base, roll=True):
    """First HH[:MM][am/pm] in s -> a datetime on base's date. If roll and it's already
    past on that date, bump a day (so 'at 9am' said at 10am means tomorrow 9am)."""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ap = (m.group(3) or "").replace(".", "")
    if ap == "pm" and hh < 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    if hh > 23 or mm > 59:
        return None
    t = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if roll and t <= base:
        t += timedelta(days=1)
    return t


def parse_when(s, now=None):
    """Resolve a human time phrase to a concrete datetime, or None if unparseable.
    Offsets ('in 10 mins', '1h30m') resolve from now; clock times ('at 9pm', '21:00')
    to today (rolled to tomorrow if past); 'tomorrow ...' shifts the base a day; a bare
    ISO string is taken verbatim."""
    now = now or datetime.now()
    raw = s.strip()
    low = raw.lower()
    low = re.sub(r"^\s*(please\s+)?(remind me|ping me|nudge me)\s+(to\s+.+?\s+)?", "", low).strip()
    low = re.sub(r"^\s*(in|after)\s+", "", low).strip()
    # explicit ISO datetime ("2026-07-08T21:00")
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        pass
    tomorrow = False
    if "tomorrow" in low:
        tomorrow = True
        low = low.replace("tomorrow", "").strip()
    if "tonight" in low:
        low = low.replace("tonight", "").strip()
    # a duration offset, unless it's phrased as a clock time ('at ...')
    if not re.search(r"\bat\b", low) and not tomorrow:
        secs = parse_duration(low)
        if secs:
            return now + timedelta(seconds=secs)
    base = (now + timedelta(days=1)) if tomorrow else now
    t = parse_clock(re.sub(r"^\s*at\s+", "", low), base, roll=not tomorrow)
    if t:
        return t
    # last resort: a duration hiding behind other words
    secs = parse_duration(low)
    if secs:
        return now + timedelta(seconds=secs)
    return None


# ---- store I/O (small lock so concurrent reply-workers don't clobber each other) ----
def _acquire():
    for _ in range(40):                      # ~2s max
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK) > 10:   # stale lock
                    os.remove(LOCK)
                    continue
            except Exception:
                pass
            time.sleep(0.05)
    return False


def _release():
    try:
        os.remove(LOCK)
    except Exception:
        pass


def _load():
    try:
        d = json.loads(open(STORE, encoding="utf-8-sig").read())
        d.setdefault("reminders", [])
        d.setdefault("fired", [])
        return d
    except Exception:
        return {"reminders": [], "fired": []}


def _save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STORE)


def add(text, when, channel="general", now=None):
    """Register a reminder. Returns (resolved_datetime, id) or (None, None) if the time
    phrase couldn't be parsed."""
    due = parse_when(when, now=now)
    if not due:
        return None, None
    rid = f"rem-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"
    entry = {
        "id": rid,
        "due": due.strftime("%Y-%m-%dT%H:%M:%S"),
        "channel": str(channel or "general"),
        "text": text.strip(),
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    got = _acquire()
    try:
        d = _load()
        d["reminders"].append(entry)
        _save(d)
    finally:
        if got:
            _release()
    _log(f"scheduled {rid} due {entry['due']} ch={entry['channel']}: {text[:60]!r}")
    return due, rid


def _send(channel, text):
    """Fire one reminder via baxter_say (default mention -> pings his phone). True if it went.

    Exit 3 means baxter_say REFUSED the claim and sent nothing. fire_due() has already deleted
    the reminder from the store by the time we get here, so a swallowed denial is a promise
    that evaporates- the exact failure this module exists to prevent. Record it, return False."""
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
    msg = f"⏰ Reminder, sir- {text}"
    p = subprocess.run([sys.executable or "python", SAY, "--channel", str(channel), msg],
                       cwd=VAULT, timeout=30, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True)
    if p.returncode == 3:
        row = autobuild.denial_alert("baxter_reminders._send", f"channel {channel}", p.stderr)
        _log(f"DENIED: the reminder to channel {channel} was refused- {row['reason']}")
        return False
    return True


def fire_due(now=None):
    """Send every reminder whose due-time has passed and clear it. Called by triage's
    ~60s loop. Returns the count fired. Deterministic, no claude."""
    now = now or datetime.now()
    if not os.path.exists(STORE):
        return 0
    got = _acquire()
    try:
        d = _load()
        due, keep = [], []
        for r in d.get("reminders", []):
            try:
                when = datetime.strptime(r["due"], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                keep.append(r); continue          # unparseable- leave it, don't lose it
            (due if when <= now else keep).append(r)
        if not due:
            return 0
        d["reminders"] = keep
        # archive fired ones (capped) for audit before releasing the lock
        for r in due:
            r["fired_at"] = now.strftime("%Y-%m-%dT%H:%M:%S")
        d["fired"] = (d.get("fired", []) + due)[-50:]
        _save(d)
    finally:
        if got:
            _release()
    fired = 0
    for r in due:
        try:
            if not _send(r.get("channel", "general"), r.get("text", "")):
                continue        # denied; _send already recorded it. Never count it as fired.
            fired += 1
            _log(f"fired {r.get('id')} (due {r.get('due')}): {r.get('text','')[:60]!r}")
        except Exception as e:
            _log(f"fire failed for {r.get('id')}: {e}")
    return fired


def _fmt(dt, now=None):
    """Human echo of a fire-time: 'today at 9:42pm' / 'tomorrow at 8:00am' / date if further."""
    now = now or datetime.now()
    clock = dt.strftime("%I:%M%p").lower().lstrip("0")
    days = (dt.date() - now.date()).days
    if days == 0:
        return f"at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    return f"on {dt.strftime('%d %b')} at {clock}"


def main():
    args = sys.argv[1:]
    if "--fire" in args:
        n = fire_due()
        print(f"fired {n}")
        return 0
    if "--list" in args:
        d = _load()
        for r in d.get("reminders", []):
            print(f"{r['due']}  [{r['channel']}]  {r['text']}  ({r['id']})")
        if not d.get("reminders"):
            print("(none pending)")
        return 0
    if "--add" in args:
        def val(flag):
            return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None
        text = val("--text")
        when = val("--when") or val("--in") or val("--at")
        channel = val("--channel") or "general"
        if not text or not when:
            print("ERROR: --add needs --text and one of --when/--in/--at", file=sys.stderr)
            return 2
        due, rid = add(text, when, channel=channel)
        if not due:
            print(f"ERROR: could not parse a time from {when!r}- reminder NOT scheduled", file=sys.stderr)
            return 3
        print(f"scheduled {rid} -> {_fmt(due)} ({due:%Y-%m-%d %H:%M})")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
