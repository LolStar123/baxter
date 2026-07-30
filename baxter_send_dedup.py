"""baxter_send_dedup - one consolidated reply per inbound message; kill double-sends.

Baxter's live channel session was observed answering the SAME Discord message twice
within a turn (04:12-04:16, 6th July) - double token burn, reads erratic. This is the
code-level enforcement the owner asked for: a shared guard sitting on BOTH send paths that
Baxter uses to speak in his server, so a duplicate can't leave regardless of how the
model behaves.

Two paths, one shared state file (.baxter_send_dedup.json):
  1. baxter_say.py  - imports guard()/record() directly (contract step 25 tells the
     live session to reply via this).
  2. the Discord plugin `reply` tool - a PreToolUse hook runs `--hook`, which reads the
     tool call on stdin and DENIES a duplicate before it fires.

A send is a DUPLICATE when, inside a short window, either:
  - it answers a reply_to (inbound message id) already answered on this channel WITH
    NEAR-IDENTICAL TEXT (TARGET_WINDOW) - "you already said that to that message", or
  - it is byte-identical to any recent send on the channel (EXACT_WINDOW), or
  - its text is near-identical to a very recent send with the same target, or two
    plain (non-reply) sends match (CONTENT_WINDOW) - a literal double-send.

A DISTINCT answer to a message already answered is NOT a duplicate (10th July). Rule 1
used to key on (channel, reply_to) alone, so the live session's queue-time ack to the owner
consumed the one allowed reply and every build finishing inside 5h could not tell him it
had landed - `baxter_say --reply-to` exited 0 printing "deduped ... - not sent", and lane 2
(window_close_alert) had to force its landing reply through. The ack and the landing are
different sentences; only near-identical text is a double-send.

Distinct replies to DIFFERENT messages in one batch are NOT duplicates (different
reply_to) - the contract wants each inbound message answered with its own reply, so we
never block those. ASCII-safe, stdlib only.
"""
import json, os, re, time, difflib, random
from contextlib import contextmanager

STATE = r"C:\Users\you\Documents\Baxter\.baxter_send_dedup.json"
LOCK = STATE + ".lock"
LOCK_STALE = 30         # s: a lock file older than this belongs to a crashed holder- steal it
# TARGET_WINDOW must OUTLAST the whole retry lifetime, or a retry escapes it. A triage/
# resume batch that fails is retried every ~15 min up to ~12 times (~3h); the old 900s
# (15 min) window let a retry re-reply land 49s past the edge- that was the 6-Jul "addon"
# double (replies 949s apart). 5h > any retry chain, so once a message id is answered it
# cannot be answered again for the rest of the usage window, whatever re-fires.
TARGET_WINDOW = 18000   # s: don't answer the same inbound message id twice (5 h)
CONTENT_WINDOW = 180    # s: near-identical text counts as a double-send (3 min)
EXACT_WINDOW = 1800     # s: byte-identical text is NEVER re-sent within 30 min, whatever
                        # the source (backstop vs repeated pings- the 92% spam was identical
                        # lines every ~4 min, escaping the 3-min near-identical window)
SIM_THRESHOLD = 0.90    # difflib ratio at/above which two texts are "the same"


def _norm(text):
    return " ".join((text or "").split()).lower().strip()


def _near(a, b):
    """Are two NORMALISED texts the same message? Identical, or a difflib ratio at/above
    SIM_THRESHOLD. Rules 1 and 2 both call this so the two can never drift to two different
    thresholds- the one place the meaning of "the same thing, said again" is defined."""
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= SIM_THRESHOLD


def _acquire_at(lock, timeout=5.0):
    """Cross-process spin-lock via atomic exclusive-create. Closes the check-then-record
    RACE: two lanes replying to the same message within the sub-second gap would both pass
    the check and both send. Only the lock holder may check+record, so the loser sees the
    winner's record and is deduped. Steals a lock older than LOCK_STALE (a crashed holder)."""
    start = time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileNotFoundError:   # the directory is gone- spinning cannot conjure it
            return False
        except (FileExistsError, PermissionError):
            # BOTH mean "busy". PermissionError is the Windows one, and missing it was a real
            # bug (found 9th July by --selftest-ledger): `os.remove(lock)` leaves the file in
            # DELETE PENDING for a moment, and an `os.open` landing in that window raises
            # PermissionError, NOT FileExistsError. It used to fall through to the OSError arm
            # below and fail OPEN *instantly*- never waiting a millisecond of its timeout. A
            # 6-way hammer produced 12 such instant fail-opens and lost 12 ids, with a max
            # observed wait of 150ms against a 5s budget. A busy lock is waited for, not walked past.
            try:
                if time.time() - os.path.getmtime(lock) > LOCK_STALE:
                    os.remove(lock); continue
            except Exception:
                pass
            if time.time() - start > timeout:
                return False        # fail OPEN: never freeze a send forever on a stuck lock
            # Short + JITTERED. A flat 20ms sleep starves a waiter: the holder releases, loops
            # to its next id and re-acquires while every waiter is still asleep. Jitter breaks
            # the lockstep without the syscall storm of a tight spin.
            time.sleep(0.001 + random.random() * 0.002)
        except OSError:             # bad path / unusable name- don't spin forever on it
            return False


def _release_at(lock):
    try: os.remove(lock)
    except Exception: pass


@contextmanager
def file_lock(path, timeout=5.0):
    """THE one locking scheme (the owner, 9th July). Guards `path` with a sibling `path + '.lock'`
    using exactly the scheme _acquire() has always used- atomic O_CREAT|O_EXCL, a 30s steal of
    a stale holder, and fail-OPEN on timeout. Yields True if the lock is genuinely held, False
    if it timed out and the caller is proceeding unguarded. NEVER release a lock you didn't
    take: on a False yield the file belongs to the other holder. Do not write a second
    locking scheme anywhere in the fleet- reuse this."""
    lock = str(path) + ".lock"
    got = _acquire_at(lock, timeout)
    try:
        yield got
    finally:
        if got:
            _release_at(lock)


def _acquire(timeout=5.0):
    return _acquire_at(LOCK, timeout)


def _release():
    _release_at(LOCK)


# ---- shared APPEND-ONLY id ledgers (.baxter_fast_handled / _listener_handled / _fast_reacted)
# Three processes write these: the fast-lane poll, the real-time listener, and (reading only)
# the main triage. Until 9th July every writer did an UNLOCKED read-modify-write and a
# non-atomic `open(path,'w')`, so a claim written by one was clobbered by the other's stale
# snapshot- msg 4444444444444444401 was claimed by the listener at 09:39:55 and re-answered by
# the fast lane at 09:40:40, burning a second Opus worker. update_ids() fixes BOTH halves:
#   * the read-modify-write happens under file_lock(), so writers serialise; and
#   * it MERGES rather than replaces, so even a stale snapshot can never delete another
#     writer's id; and
#   * the write is tmp + os.replace, so triage's unlocked reader never sees a half file.
_DEGRADED = 0           # writes that could NOT be guaranteed (lock timeout / write failure)


def degraded_count():
    """How many ledger writes this process could not guarantee. A healthy fleet keeps this at
    0; the acceptance exam fails on any non-zero, because a fail-open write is exactly how a
    claim silently goes missing."""
    return _DEGRADED


def read_ids(path):
    """The ids in a ledger, in order. Missing/corrupt/foreign shapes read as []- never raises."""
    try:
        with open(str(path), encoding="utf-8-sig") as f:
            data = json.load(f)
        return [str(i) for i in data.get("ids", [])] if isinstance(data, dict) else []
    except Exception:
        return []


def _write_ids(path, ids):
    """Atomic: a reader (baxter_triage) holds no lock, so it must never observe a partial file.
    The tmp name carries our pid so a fail-open writer can't clobber another's temp."""
    tmp = "%s.tmp.%d" % (str(path), os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ids": list(ids)}, f)
    # On Windows os.replace raises PermissionError when ANOTHER process merely has the
    # destination open for reading- and baxter_triage reads these ledgers unlocked, by design.
    # That is transient (microseconds), so retry it rather than reporting the write degraded
    # and losing the claim. Only a persistent failure propagates.
    for attempt in range(20):
        try:
            os.replace(tmp, str(path))
            return
        except PermissionError:
            if attempt == 19:
                try: os.remove(tmp)
                except Exception: pass
                raise
            time.sleep(0.005 + random.random() * 0.005)


def _merge_ids(path, add, cap):
    """(ids_after, newly_added, degraded). Caller MUST already hold file_lock(path)."""
    ids = read_ids(path)
    seen = set(ids)
    won = []
    for i in add:
        i = str(i)
        if i not in seen:
            seen.add(i); ids.append(i); won.append(i)
    if cap and len(ids) > cap:
        ids = ids[-cap:]
    try:
        _write_ids(path, ids)
    except Exception:
        # os.replace never half-applies, so nothing landed: report the ledger as it really is.
        return read_ids(path), [], True
    return ids, won, False


def update_ids(path, add=(), cap=200):
    """Merge `add` into the ledger at `path` under the lock and return (ids_after, degraded).
    Order-preserving, deduped, truncated to the LAST `cap` ids, written atomically.

    MERGE, NEVER REPLACE. This cannot remove an id- correct for the three append-only claim
    ledgers, and a silent no-op for anyone wanting the old replace/prune semantics. Prune by
    writing the file yourself under `with file_lock(path):`.

    `degraded` is True when the write could not be guaranteed (the lock timed out and we
    proceeded unguarded, or the write itself failed). It is fail-OPEN by design- a frozen
    listener is worse than a rare lost claim, and the send-dedup guard still blocks the
    double reply- but a caller that cares must check it. Also counted in degraded_count()."""
    global _DEGRADED
    with file_lock(path) as got:
        ids, _won, wfail = _merge_ids(path, add, cap)
    degraded = (not got) or wfail
    if degraded:
        _DEGRADED += 1
    return ids, degraded


def claim_ids(path, add=(), cap=200):
    """update_ids, but returns (newly_added, degraded)- the ids that were NOT already in the
    ledger, i.e. the ones THIS process won. The atomic test-and-set the fast lane needs: an id
    another writer claimed between our read and our write comes back as NOT won, so we never
    work a message the listener already took.

    On a WRITE FAILURE nothing landed, so nothing was won- the message stays unclaimed and the
    next 15s sweep retries it. On a mere LOCK TIMEOUT the merge did land (fail-open), so the
    win stands: skipping there would answer the owner with silence, which is the worse failure."""
    global _DEGRADED
    with file_lock(path) as got:
        _ids, won, wfail = _merge_ids(path, add, cap)
    degraded = (not got) or wfail
    if degraded:
        _DEGRADED += 1
    return ([] if wfail else won), degraded

def _load():
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(entries):
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp, STATE)
    except Exception:
        pass


def _prune(entries, now):
    keep_from = now - max(TARGET_WINDOW, CONTENT_WINDOW)
    return [e for e in entries if float(e.get("ts", 0)) >= keep_from]


def is_duplicate(channel, reply_to, content):
    """(bool, reason) - is this send a duplicate of a recent one? No state change."""
    now = time.time()
    ch = str(channel or "")
    rt = str(reply_to) if reply_to else None
    norm = _norm(content)
    if not norm:
        return False, ""
    for e in _load():
        if str(e.get("channel", "")) != ch:
            continue
        age = now - float(e.get("ts", 0))
        e_rt = e.get("reply_to")
        e_norm = e.get("norm", "")
        # 1. same inbound message already answered WITH THIS TEXT. The content test is what
        #    lets a build's landing reply follow the live session's queue-time ack to the same
        #    message: two different sentences are two answers, not one answer sent twice. A
        #    repeat (or a paraphrase) of what was already said is still refused for 5h.
        if rt and e_rt and str(e_rt) == rt and age <= TARGET_WINDOW and _near(norm, e_norm):
            return True, "already replied to that message with this text"
        # 1b. byte-identical text already sent recently (backstop vs repeated pings,
        #     ANY source/target- an exact repeat is never legitimate within EXACT_WINDOW)
        if age <= EXACT_WINDOW and norm == e_norm:
            return True, "identical message already sent"
        # 2. near-identical text (same target, or both plain sends)
        if age <= CONTENT_WINDOW and (
            (rt and e_rt and str(e_rt) == rt) or (not rt and not e_rt)
        ):
            if _near(norm, e_norm):
                return True, "near-identical to a message just sent"
    return False, ""


def record(channel, reply_to, content):
    """Log a send that actually went out, so the next call can dedup against it."""
    now = time.time()
    norm = _norm(content)
    if not norm:
        return
    entries = _prune(_load(), now)
    entries.append({
        "ts": now,
        "channel": str(channel or ""),
        "reply_to": str(reply_to) if reply_to else None,
        "norm": norm,
    })
    _save(entries)


def claim(channel, reply_to, content):
    """ATOMIC check-and-record under the lock. Returns (ok, reason). The ONLY safe way
    to reserve a send: the whole is_duplicate->record is done while holding the lock, so
    two racing lanes can never both pass. Call this BEFORE the POST; if the POST then
    fails, call release_claim() so a legit retry isn't permanently blocked. If the lock
    can't be taken (stuck), fail OPEN and allow- a rare double beats a frozen assistant."""
    if not _acquire():
        # can't get the lock (a stuck holder). For a REPLY, fail CLOSED- skip it; the
        # retry path (or the owner re-asking) recovers, and a double reply is the lethal case.
        # For a plain proactive send, fail open so pings/alerts never freeze.
        if reply_to:
            return False, "lock-timeout- reply skipped to guarantee no double"
        return True, "lock-timeout-fail-open (plain send)"
    try:
        dup, reason = is_duplicate(channel, reply_to, content)
        if dup:
            return False, reason
        record(channel, reply_to, content)
        return True, ""
    finally:
        _release()

def release_claim(channel, reply_to):
    """Undo the most recent claim for (channel, reply_to)- used when the POST failed, so
    the retry path can re-attempt instead of being deduped against a send that never left.

    KNOWN HAZARD, unfixed as of 10th July: this pops the LAST entry matching (channel,
    reply_to) and nothing else. Since rule 1 went content-aware, two DISTINCT replies to one
    message id can coexist in the state- an ack and a build's landing reply. If the landing
    reply's POST then fails and it is not the last matching entry, this deletes the wrong
    record: the ack's. The ack could then be re-sent, and the landing reply stays claimed
    though it never left. Narrow (it needs a failed POST plus an out-of-order entry), but
    real. The fix is to match on the normalised text too, not on (channel, reply_to) alone."""
    if not _acquire():
        return
    try:
        ch = str(channel or ""); rt = str(reply_to) if reply_to else None
        entries = _load()
        for idx in range(len(entries) - 1, -1, -1):
            e = entries[idx]
            if str(e.get("channel", "")) == ch and (str(e.get("reply_to")) if e.get("reply_to") else None) == rt:
                entries.pop(idx); break
        _save(entries)
    finally:
        _release()

def guard(channel, reply_to, content):
    """ATOMIC check-then-record for the hook path (the plugin reply tool). Returns
    (send_ok, reason). Now lock-guarded so it can't race the baxter_say path."""
    return claim(channel, reply_to, content)


def _record_live_pointer(payload):
    """Piggyback on the PreToolUse hook: if the firing session is the live `--channels`
    one, record its real transcript_path (from the hook payload) into the live-session
    pointer, so baxter_turntokens can read EXACT per-turn tokens without guessing which
    of the 500+ transcripts is Baxter's. Cached per session_id (one parent-walk per
    session). Never raises - must not affect the dedup deny decision."""
    try:
        import baxter_turntokens
        baxter_turntokens.record_live_session(payload)
    except Exception:
        pass


# ---- PreToolUse hook entrypoint for the Discord plugin `reply` tool ----
def _hook():
    import sys
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # can't parse -> don't interfere
    _record_live_pointer(payload)
    tool = payload.get("tool_name", "")
    if "discord" not in tool or "reply" not in tool:
        return 0
    ti = payload.get("tool_input", {}) or {}
    channel = ti.get("chat_id") or ti.get("channel") or ti.get("channel_id") or ""
    reply_to = ti.get("reply_to")
    content = ti.get("text") or ti.get("content") or ti.get("message") or ""
    ok, reason = guard(channel, reply_to, content)
    if not ok:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Duplicate send blocked ({reason}). You have already answered this "
                    "message- consolidate into ONE reply; do not re-send."
                ),
            }
        }
        print(json.dumps(out))
    return 0


def _bash_hook():
    """PreToolUse guard on Bash/PowerShell: DENY any raw Discord message SEND, forcing
    all replies through baxter_say (which holds the dedup lock). Closes the last hole-
    a worker curling/urlopen-ing discord.com directly would bypass every guard. Reads
    (GET) are untouched; only POST-shaped calls to the messages endpoint are blocked."""
    import sys, re
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    _record_live_pointer(payload)
    if payload.get("tool_name", "") not in ("Bash", "PowerShell"):
        return 0
    cmd = ((payload.get("tool_input", {}) or {}).get("command", "") or "").lower()
    if "discord.com/api" in cmd and "message" in cmd:
        post = any(p in cmd for p in (
            "-x post", "-xpost", "--request post", "--data", "-d ", "-d'", '-d"',
            "method post", "method=post", "method='post'", 'method="post"',
            "'post'", '"post"', "urlopen", "invoke-restmethod", "webrequest"))
        if post:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Raw Discord sends are blocked. Reply ONLY via "
                    "baxter_say.py --reply-to <id> (it holds the double-send lock). "
                    "A raw curl/urlopen bypasses the guard and can double-message the owner.")}}))
    return 0

# ---- the ledger-lock acceptance exam (--selftest-ledger) ----------------------------------
# The lock landed before this exam did, so the exam passes on the unmodified tree. That makes
# it decoration unless it can be shown to FAIL when the lock is absent. Two things stop that:
#   * a CONTROL ARM that hammers an unlocked writer with the identical harness and asserts it
#     DROPS ids. If the control ever stops dropping, the exam has lost its sensitivity to the
#     bug it guards and this test fails LOUDLY rather than passing on.
#   * real SUBPROCESSES, released together off a barrier file. Threads would pass with no lock
#     at all- the GIL plus a sub-millisecond read-modify-write almost never interleaves.
_UTILS = os.path.dirname(os.path.abspath(__file__))
VAULT = os.path.dirname(STATE)
_LIVE_LEDGERS = [os.path.join(VAULT, n) for n in (
    ".baxter_fast_handled.json", ".baxter_listener_handled.json",
    ".baxter_fast_reacted.json", ".baxter_send_dedup.json")]

# EVERY id, id-shape and channel these exams invent. Declared ONCE, so the ledger exam's
# footprint arm and the reply exam's cannot drift apart from the fixtures they are meant to
# catch (--selftest-arm6 asserts the reply exam's own constants are in here).
#   * 4444444444444444401 is the message the 9th-July race double-answered- every ledger arm
#     hammers it, and baxter_slash's listener exam does too.
#   * 9999999999 is the fast-lane gate's "genuinely new" sentinel: ten digits, where a Discord
#     snowflake is eighteen or nineteen, so it can never collide with a real message.
#   * 1500000000000000001/2 are the reply exam's two inbound messages (_RX_MID_A/_RX_MID_B).
_EXAM_FIXTURE_IDS = frozenset({
    "4444444444444444401", "9999999999",
    "1500000000000000001", "1500000000000000002"})
# The hammer's per-child id blocks ("L-0-3", "C-5-59", "X-0-0") and baxter_slash's "fast-7".
_EXAM_FIXTURE_RX = re.compile(r"^(?:[LCX]-\d+-\d+|fast-\d+)$")
_EXAM_FIXTURE_CHANNELS = frozenset({"999000111"})       # _RX_CH


def _is_fixture_id(i):
    i = str(i)
    return i in _EXAM_FIXTURE_IDS or bool(_EXAM_FIXTURE_RX.match(i))

# The control writer lives HERE, in a temp child script the exam writes at run time, and never
# in production. `nap` widens the same read->write window the old code always carried, so the
# drop is deterministic instead of a coin-flip on disk speed.
_HAMMER_CHILD = '''\
import sys, os, time, json
sys.path.insert(0, sys.argv[1])
import baxter_send_dedup as dedup

mode, path, barrier, nap = sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5])
ids = sys.argv[6].split(",")


def unlocked_merge(p, i, nap):
    """CONTROL ONLY- the exact pre-9th-July shape: an unlocked read-modify-write ending in a
    truncating json.dump(open(p, "w")). A stale snapshot here deletes another writer's id."""
    try:
        cur = json.load(open(p, encoding="utf-8-sig")).get("ids", [])
    except Exception:
        cur = []
    if i not in cur:
        cur.append(i)
    time.sleep(nap)
    json.dump({"ids": cur}, open(p, "w", encoding="utf-8"))


while not os.path.exists(barrier):      # every child spins here; the parent releases them together
    time.sleep(0.001)

won = []
for i in ids:
    if mode == "locked":
        dedup.update_ids(path, [i], cap=0)
    elif mode == "unlocked":
        unlocked_merge(path, i, nap)
    elif mode == "claim":
        w, _d = dedup.claim_ids(path, [i], cap=0)
        won.extend(w)
print(json.dumps({"degraded": dedup.degraded_count(), "won": won}))
'''

# Drives baxter_fast's REAL sweep gate, not claim_ids underneath it: a sweep that ignored the
# `won` filter would still spawn the duplicate worker with a perfect lock ([[exam-must-drive-the-gate]]).
_FASTLANE_CHILD = '''\
import sys, os, json
sys.path.insert(0, sys.argv[1])
import baxter_send_dedup as dedup
import baxter_fast as bf

bf.log = lambda *a, **k: None                    # never touch the live .baxter.log
led = os.path.join(sys.argv[2], "fast_handled.json")
bf.HANDLED = led
mid = "4444444444444444401"                      # the message the race actually double-answered

dedup.update_ids(led, [mid], cap=bf.HANDLED_CAP) # the listener claims it first, exactly as _claim() does
pre = bf._drop_handled([{"id": mid}])            # the sweep's pre-filter
won = bf._claim_handled([mid])                   # the atomic gate the sweep keys `won` off
fresh = bf._claim_handled(["9999999999"])        # a genuinely new id MUST still be won
print(json.dumps({"pre": [m["id"] for m in pre], "won": won, "fresh": fresh,
                  "ids": dedup.read_ids(led)}))
'''


# Drives the REAL door- baxter_say.main()- in its own process, with urlopen stubbed. A child
# is the only honest way to run it: main() reads sys.argv, and the module caches `_dedup` at
# import, so STATE must be redirected before baxter_say is ever imported. Nothing here may
# touch the owner's live dedup state or reach Discord.
_REPLY_CHILD = '''\
import sys, os, io, json

utils, state, out_path, channel, reply_to, text = sys.argv[1:7]
sys.path.insert(0, utils)

tmpdir = os.path.dirname(os.path.abspath(state))
import baxter_send_dedup as dedup
dedup.STATE = state
dedup.LOCK = state + ".lock"
# containment, asserted before ANYTHING runs: a mis-set STATE would let this exam claim and
# consume a real reply to the owner out of the live ledger.
assert os.path.dirname(os.path.abspath(dedup.STATE)) == tmpdir, "STATE escaped the temp dir"
assert os.path.basename(tmpdir).startswith("bxr_dedup_exam_"), "STATE is not in the exam dir"

# the footer would differ per call and change the guarded text under us
import baxter_turntokens
baxter_turntokens.footer_if_live = lambda: ""

import urllib.request
calls = []


class _Resp:
    def read(self): return b"{}"
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def close(self): pass


def _fake_urlopen(req, *a, **k):
    try:
        calls.append((req.get_method(), req.full_url))
    except Exception:
        calls.append(("?", str(req)))
    return _Resp()


urllib.request.urlopen = _fake_urlopen          # stubs the POST *and* the reaction PUT/DELETE

import baxter_say
assert baxter_say._dedup is dedup, "baxter_say bound a different baxter_send_dedup module"

sys.argv = ["baxter_say.py", "--channel", channel, "--reply-to", reply_to, text]
_out, _err = io.StringIO(), io.StringIO()
_so, _se = sys.stdout, sys.stderr
sys.stdout, sys.stderr = _out, _err
try:
    rc = baxter_say.main()
finally:
    sys.stdout, sys.stderr = _so, _se

# ONLY a POST to /messages is a send. The reply path also fires PUT/DELETE reaction calls
# through this same urlopen; counting those would make a deduped arm look like it sent.
posts = [c for c in calls if c[0] == "POST" and c[1].endswith("/messages")]
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({"rc": rc, "stdout": _out.getvalue(), "stderr": _err.getvalue(),
               "posts": len(posts), "other": len(calls) - len(posts)}, f)
print("child ok")
'''


def _ledger_snapshot(paths=None):
    """{path: ORDERED list of keys} for the live ledgers. Two shapes, told apart by content:
      * the three {"ids": [...]} claim ledgers -> the id strings, in order. Order, not a set:
        a cap eviction takes from the HEAD, a truncating clobber takes from anywhere, and only
        position tells the two apart.
      * .baxter_send_dedup.json, a LIST of sends -> one tuple per entry,
        (ts, channel, reply_to, sha1(norm)). The text hash is IN the key because two sends can
        share a float tick, a channel and a reply_to; keyed on (ts, reply_to) alone they would
        collide, and one of them would read as lost.
    A missing or corrupt file snapshots as []- never raises. An exam that dies here would grade
    a crash as a race."""
    import hashlib
    snap = {}
    for p in (paths if paths is not None else _LIVE_LEDGERS):
        # RETRY, rather than falling straight to []. A writer's os.replace can make a concurrent
        # open() fail transiently on Windows, and an empty snapshot would read as "every id
        # vanished"- a false FAILED of exactly the kind this arm exists to abolish. Only a file
        # that is genuinely absent, or still unreadable after five tries, snapshots as empty.
        data = None
        for attempt in range(5):
            try:
                with open(p, encoding="utf-8-sig") as f:
                    data = json.load(f)
                break
            except FileNotFoundError:
                break
            except Exception:
                if attempt == 4:
                    break
                time.sleep(0.005 + random.random() * 0.005)
        if data is None:
            snap[p] = []
            continue
        if isinstance(data, dict):
            snap[p] = [str(i) for i in data.get("ids", [])]
        elif isinstance(data, list):
            snap[p] = [(float(e.get("ts", 0)), str(e.get("channel", "")),
                        str(e.get("reply_to")) if e.get("reply_to") else None,
                        hashlib.sha1((e.get("norm") or "").encode("utf-8")).hexdigest()[:16])
                       for e in data if isinstance(e, dict)]
        else:
            snap[p] = []
    return snap


def _exam_footprint_violations(before, after, now):
    """Did the exam itself touch a live ledger? A pure function of two snapshots, so
    --selftest-arm6 can drive every verdict off a synthetic table instead of the fleet.
    Returns [(kind, message)], kind in {"footprint", "lost"}; [] means the exam left no mark.

    WHY NOT A HASH. Arm 6 used to sha256 the four files before and after the run and fail if
    any byte moved. The fast lane, the listener and EVERY baxter_say send rewrite them
    continuously, so a ~2min exam raced real fleet activity: on 10th July --selftest-ledger
    exited 1 naming .baxter_send_dedup.json as MUTATED, then exited 0 twice on byte-identical
    code. Any lane whose verify gate named it could be graded FAILED by another lane's Discord
    post. These three rules catch the exam's own footprint and nothing else- none of them fires
    on ordinary churn, and none of them is a softening:

      (a) FOOTPRINT  an exam fixture id or channel APPEARED in a ledger. Appeared, not merely
          present: 4444444444444444401 already sits in .baxter_listener_handled.json and
          .baxter_fast_reacted.json, so an assert keyed on presence would fail forever.
      (b) LOST ID    a claim ledger may only lose ids from its HEAD, and only while it grows.
          That is exactly a cap eviction (append one, drop the oldest, length holds at the cap).
          A truncating unlocked write takes from anywhere, or shrinks the file. Both refused.
      (c) LOST SEND  a dedup entry still inside the prune window must survive. _prune() drops
          entries older than max(TARGET_WINDOW, CONTENT_WINDOW); anything newer that vanished
          was destroyed, not aged out. `now` is read AFTER the run, so our cutoff is never
          older than the cutoff any concurrent writer pruned with.
    """
    out = []
    keep_from = now - max(TARGET_WINDOW, CONTENT_WINDOW)
    for path in sorted(before):
        if path not in after:
            continue
        b, a = before[path], after[path]
        name = os.path.basename(path)
        a_set = set(a)
        added = [k for k in a if k not in set(b)]
        is_dedup = any(isinstance(k, tuple) for k in b) or any(isinstance(k, tuple) for k in a)

        # (a) FOOTPRINT
        for k in added:
            if isinstance(k, tuple):
                _ts, ch, rt, _h = k
                if ch in _EXAM_FIXTURE_CHANNELS or (rt and _is_fixture_id(rt)):
                    out.append(("footprint", "%s: an exam fixture send was recorded "
                                             "(channel %s, reply_to %s)" % (name, ch, rt)))
            elif _is_fixture_id(k):
                out.append(("footprint", "%s: exam fixture id %s was ADDED" % (name, k)))

        if is_dedup:
            # (c) LOST SEND
            lost = [k for k in b if k not in a_set and k[0] >= keep_from]
            if lost:
                out.append(("lost", "%s: %d in-window send(s) vanished (oldest ts %.0f, "
                                    "cutoff %.0f)" % (name, len(lost), min(k[0] for k in lost),
                                                      keep_from)))
        else:
            # (b) LOST ID
            survivors = [i for i in b if i in a_set]
            if survivors != b[len(b) - len(survivors):]:
                gone = [i for i in b if i not in a_set]
                out.append(("lost", "%s: %d id(s) lost from somewhere other than the head, "
                                    "which no cap eviction can do (e.g. %s)"
                                    % (name, len(gone), gone[:3])))
            elif len(survivors) < len(b) and len(a) < len(b):
                out.append(("lost", "%s: shrank from %d to %d ids- a head loss that no cap "
                                    "eviction explains, since nothing was appended"
                                    % (name, len(b), len(a))))
    return out


def _st_run(child, args, timeout=180):
    import subprocess, sys
    p = subprocess.Popen([sys.executable, child] + [str(a) for a in args],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8")
    out, err = p.communicate(timeout=timeout)
    return p.returncode, (out or "").strip(), (err or "").strip()


def _st_hammer(child, mode, path, tmp, procs, per_proc, nap, label, contested=None):
    """Release `procs` subprocesses onto `path` at once and return (expected_ids, child_reports).

    Each child normally gets its OWN disjoint block of ids, so a dropped id names its writer.
    With `contested`, every child is handed the SAME id instead- the exclusivity arm, where
    exactly one process may come back having won it."""
    import subprocess, sys
    barrier = os.path.join(tmp, "go-" + label)
    blocks = ([[contested]] * procs if contested else
              [["%s-%d-%d" % (label, p, k) for k in range(per_proc)] for p in range(procs)])
    kids = [subprocess.Popen(
        [sys.executable, child, _UTILS, mode, path, barrier, str(nap), ",".join(b)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        for b in blocks]
    time.sleep(0.5)                     # let every child reach the barrier spin
    open(barrier, "w").close()          # ...and release them together
    reports = []
    for k in kids:
        out, err = k.communicate(timeout=180)
        reports.append((k.returncode, (out or "").strip(), (err or "").strip()))
    return ([contested] if contested else [i for b in blocks for i in b]), reports


def selftest_ledger():
    """Prove the shared-claim lock: no id is ever dropped, exactly one process wins a contested
    claim, and the fast lane skips what the listener took. Returns 0 on pass, 1 on failure."""
    import sys, tempfile, shutil, subprocess
    tmp = tempfile.mkdtemp(prefix="baxter_ledger_")
    before = _ledger_snapshot()             # arm 6: this exam must not touch the owner's live ledgers
    fails, notes = [], []

    def check(ok, name, detail=""):
        print(("  ok   " if ok else "  FAIL ") + name + (("- " + detail) if detail else ""))
        if not ok:
            fails.append(name)

    try:
        hammer = os.path.join(tmp, "hammer.py")
        with open(hammer, "w", encoding="utf-8") as f:
            f.write(_HAMMER_CHILD)
        fastlane = os.path.join(tmp, "fastlane.py")
        with open(fastlane, "w", encoding="utf-8") as f:
            f.write(_FASTLANE_CHILD)

        PROCS, PER = 6, 60

        # --- arm 1: the locked hammer. 6 processes, 360 interleaved read-modify-writes.
        led = os.path.join(tmp, "locked.json")
        expected, reports = _st_hammer(hammer, "locked", led, tmp, PROCS, PER, 0.0, "L")
        bad = [r for r in reports if r[0] != 0]
        check(not bad, "locked hammer: all %d children exited 0" % PROCS,
              "" if not bad else repr(bad[:1]))
        got = read_ids(led)
        missing = [i for i in expected if i not in set(got)]
        check(not missing, "locked hammer: every one of %d ids survived" % len(expected),
              "" if not missing else "%d DROPPED, e.g. %s" % (len(missing), missing[:3]))
        check(len(got) == len(set(got)), "locked hammer: no id written twice",
              "%d ids, %d unique" % (len(got), len(set(got))))
        degraded = [json.loads(r[1])["degraded"] for r in reports if r[0] == 0 and r[1]]
        # degraded_count() is fail-OPEN: a lock timeout still writes. An id-set assertion alone
        # would go green with every single write unguarded, so assert it explicitly.
        check(degraded and all(d == 0 for d in degraded),
              "locked hammer: degraded_count()==0 in every child", "saw %r" % (degraded,))

        # --- arm 2: the CONTROL. Identical harness, unlocked writer. It MUST drop ids.
        ctl = os.path.join(tmp, "control.json")
        cexp, creports = _st_hammer(hammer, "unlocked", ctl, tmp, PROCS, PER, 0.002, "C")
        cgot = set(read_ids(ctl))
        dropped = [i for i in cexp if i not in cgot]
        check(len(dropped) >= 1,
              "control arm (UNLOCKED): drops ids, so the exam has real sensitivity",
              "dropped %d/%d" % (len(dropped), len(cexp)) if dropped else
              "DROPPED NOTHING- this exam cannot detect the bug it guards")
        notes.append("control arm dropped %d of %d ids (%.0f%%)"
                     % (len(dropped), len(cexp), 100.0 * len(dropped) / len(cexp)))

        # --- arm 3: exclusivity. 8 processes claim the SAME id; exactly one may win it.
        exc = os.path.join(tmp, "exclusive.json")
        contested = "4444444444444444401"
        _e, ereports = _st_hammer(hammer, "claim", exc, tmp, 8, 1, 0.0, "X", contested=contested)
        ebad = [r for r in ereports if r[0] != 0]
        check(not ebad, "exclusivity: all 8 children exited 0", "" if not ebad else repr(ebad[:1]))
        winners = [w for r in ereports if r[0] == 0 and r[1] for w in json.loads(r[1])["won"]]
        check(len(winners) == 1, "exclusivity: exactly one of 8 processes won the contested id",
              "%d winners" % len(winners))
        check(read_ids(exc).count(contested) == 1, "exclusivity: the id is in the ledger once",
              "count=%d" % read_ids(exc).count(contested))

        # --- arm 4: the fast lane's real sweep gate skips a listener-claimed message.
        rc, out, err = _st_run(fastlane, [_UTILS, tmp])
        if rc != 0:
            check(False, "fast-lane gate: child ran", err[-200:] or "rc=%d" % rc)
        else:
            r = json.loads(out)
            check(r["pre"] == [], "fast-lane gate: _drop_handled filters the claimed message",
                  repr(r["pre"]))
            check(r["won"] == [], "fast-lane gate: _claim_handled returns [] -> no worker spawned",
                  repr(r["won"]))
            check(r["fresh"] == ["9999999999"],
                  "fast-lane gate: an unclaimed id IS still won (the gate is not a stub)",
                  repr(r["fresh"]))

        # --- arm 5: the listener half, in its own process (it lives on 3.12, we may be 3.11).
        slash = os.path.join(_UTILS, "baxter_slash.py")
        p = subprocess.run([sys.executable, slash, "--selftest-ledger"],
                           capture_output=True, text=True, encoding="utf-8", timeout=300)
        check(p.returncode == 0, "listener: baxter_slash --selftest-ledger passes",
              (p.stdout or "")[-300:] + (p.stderr or "")[-300:] if p.returncode else "")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- arm 6: a selftest that wrote a live claim file could itself cause a missed reply to
    # the owner, so this must stay a real assertion. It must ALSO ignore the fleet writing those
    # files underneath it, which it does continuously. _exam_footprint_violations() is the
    # difference; --selftest-arm6 proves it still reddens when the redirect is removed.
    viol = _exam_footprint_violations(before, _ledger_snapshot(), time.time())
    fp = [m for k, m in viol if k == "footprint"]
    lost = [m for k, m in viol if k == "lost"]
    check(not fp, "live ledgers: no exam fixture id or channel reached one", "; ".join(fp))
    check(not lost, "live ledgers: no id or in-window send was lost", "; ".join(lost))

    for n in notes:
        print("  note " + n)
    if fails:
        print("ledger selftest FAILED: %s" % ", ".join(fails))
        return 1
    print("ledger selftest OK: the shared claim ledger survives %d concurrent cross-process "
          "writes with zero drops and zero degraded writes; an unlocked writer under the same "
          "harness loses ids; one process wins a contested claim; and the fast lane spawns no "
          "worker for a message the listener took." % (6 * 60))
    return 0


# ---- the content-aware rule-1 acceptance exam (--selftest-reply) --------------------------
# Fixtures. The ack and the landing must sit BELOW SIM_THRESHOLD of each other or arm A3 would
# fail against a CORRECT fix; the near-variants must sit ABOVE it or A4/A7 could not bite. Both
# facts are asserted at run time (arm A0) rather than assumed- a threshold tweak must break the
# exam loudly, not quietly turn an arm into decoration.
_RX_CH = "999000111"
_RX_MID_A = "1500000000000000001"
_RX_MID_B = "1500000000000000002"
_RX_ACK = "Understood, sir. I will see to the window alert shortly."
_RX_ACK_NEAR = "Understood, sir. I shall see to the window alert shortly."
_RX_LANDING = "The window alert is live, sir. It fires on close."
_RX_LANDING_NEAR = "The window alert is live, sir. It fires at close."


def _rx_ratio(a, b):
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _rx_old_rule1(entries, channel, reply_to, now):
    """THE CONTROL. The pre-10th-July content-blind rule 1, re-implemented here and nowhere
    near production: (channel, reply_to, age <= TARGET_WINDOW) -> duplicate, text ignored.
    If this ever stops REFUSING the landing reply, the exam has lost its sensitivity to the
    very bug it guards and must fail loudly rather than pass on."""
    ch, rt = str(channel), str(reply_to)
    for e in entries:
        if str(e.get("channel", "")) != ch:
            continue
        e_rt = e.get("reply_to")
        if rt and e_rt and str(e_rt) == rt and (now - float(e.get("ts", 0))) <= TARGET_WINDOW:
            return True
    return False


def _rx_send(child, tmp, state, reply_to, text):
    """Run one real baxter_say.main() reply in a child and return its report."""
    out_path = os.path.join(tmp, "out.json")
    try:
        os.remove(out_path)
    except FileNotFoundError:
        pass
    rc, so, se = _st_run(child, [_UTILS, state, out_path, _RX_CH, reply_to, text], timeout=120)
    if rc != 0 or not os.path.exists(out_path):
        raise RuntimeError("reply child failed rc=%d\nout: %s\nerr: %s"
                           % (rc, so[-600:], se[-600:]))
    with open(out_path, encoding="utf-8") as f:
        return json.load(f)


def _rx_age(state, seconds):
    """Backdate every recorded entry, so an arm can test an age without waiting hours."""
    with open(state, encoding="utf-8") as f:
        entries = json.load(f)
    for e in entries:
        e["ts"] = float(e["ts"]) - seconds
    with open(state, "w", encoding="utf-8") as f:
        json.dump(entries, f)


def selftest_reply():
    """Prove rule 1 is content-aware: an ack and a build's LANDING reply to the same message
    both send, while a repeat or a paraphrase of either is still refused. Returns 0 / 1."""
    global STATE, LOCK
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="bxr_dedup_exam_")
    # Guard the ONE live file this exam could plausibly corrupt- a mis-redirected STATE would
    # consume a real reply to the owner. A HASH of it was the same race arm 6 carried: every
    # baxter_say send rewrites .baxter_send_dedup.json, so a hash arm here fails whenever a
    # live reply lands mid-run. Snapshot + footprint detector instead: this exam's own
    # fixtures (_RX_CH, _RX_MID_A/B) are named in _EXAM_FIXTURE_*, so containment breaking
    # still reddens it, while a real send to the owner does not.
    live = [STATE]
    before = _ledger_snapshot(live)
    fails = []

    def check(ok, name, detail=""):
        print(("  ok   " if ok else "  FAIL ") + name + (("- " + detail) if detail else ""))
        if not ok:
            fails.append(name)

    def sent(r, name):
        check(r["rc"] == 0 and r["posts"] == 1 and "said" in r["stdout"], name,
              "rc=%d posts=%d out=%r err=%r" % (r["rc"], r["posts"], r["stdout"].strip(),
                                                r["stderr"].strip()))

    def deduped(r, name):
        # rc==0 AND the word: rc 3 (claim denied) and rc 2 (unknown channel) also post nothing,
        # and would otherwise read as a clean dedupe.
        check(r["rc"] == 0 and r["posts"] == 0 and "deduped" in r["stdout"], name,
              "rc=%d posts=%d out=%r err=%r" % (r["rc"], r["posts"], r["stdout"].strip(),
                                                r["stderr"].strip()))

    try:
        child = os.path.join(tmp, "reply_child.py")
        with open(child, "w", encoding="utf-8") as f:
            f.write(_REPLY_CHILD)
        state = os.path.join(tmp, "dedup_state.json")

        # --- A0: the fixtures really do straddle SIM_THRESHOLD, in both directions.
        r_far = _rx_ratio(_RX_ACK, _RX_LANDING)
        r_ack = _rx_ratio(_RX_ACK, _RX_ACK_NEAR)
        r_land = _rx_ratio(_RX_LANDING, _RX_LANDING_NEAR)
        check(r_far < SIM_THRESHOLD, "A0 fixtures: ack vs landing is a DISTINCT text",
              "ratio %.4f must be < %.2f" % (r_far, SIM_THRESHOLD))
        check(r_ack >= SIM_THRESHOLD and r_land >= SIM_THRESHOLD,
              "A0 fixtures: the near-variants are above the threshold",
              "ack %.4f, landing %.4f must both be >= %.2f" % (r_ack, r_land, SIM_THRESHOLD))

        # --- A1: the live session's queue-time ack goes out.
        a1 = _rx_send(child, tmp, state, _RX_MID_A, _RX_ACK)
        sent(a1, "A1 ack: the first reply to a message is posted")
        check(a1["other"] >= 1, "A1 reaction calls are counted separately from the send",
              "posts=%d other=%d" % (a1["posts"], a1["other"]))

        # --- A2: the same ack again, immediately. Still a double-send.
        deduped(_rx_send(child, tmp, state, _RX_MID_A, _RX_ACK),
                "A2 repeat: a byte-identical ack is refused")

        # --- A3: THE FIX. The build's landing reply to the SAME message, inside TARGET_WINDOW.
        sent(_rx_send(child, tmp, state, _RX_MID_A, _RX_LANDING),
             "A3 THE FIX: a distinct landing reply to an already-answered message is posted")

        # --- A4: a paraphrase of the ack is still the ack.
        deduped(_rx_send(child, tmp, state, _RX_MID_A, _RX_ACK_NEAR),
                "A4 paraphrase: a near-identical ack is refused")

        # --- A5/A7: past EXACT_WINDOW, inside TARGET_WINDOW. Rule 1b no longer covers these,
        #     so ONLY the widened rule 1's content test can refuse them.
        _rx_age(state, EXACT_WINDOW + 3200)                     # 5000s: > 1800, << 18000
        deduped(_rx_send(child, tmp, state, _RX_MID_A, _RX_ACK),
                "A5 repeat past EXACT_WINDOW: an identical ack is still refused")
        deduped(_rx_send(child, tmp, state, _RX_MID_A, _RX_LANDING_NEAR),
                "A7 paraphrase past EXACT_WINDOW: a near-identical landing is still refused")

        # --- A6: THE FIX at the far edge of TARGET_WINDOW, on a second message id.
        sent(_rx_send(child, tmp, state, _RX_MID_B, _RX_ACK), "A6a ack on a second message")
        _rx_age(state, 17000)                                   # B's ack: 17000s < 18000s
        sent(_rx_send(child, tmp, state, _RX_MID_B, _RX_LANDING),
             "A6 THE FIX at the edge: a landing reply 17000s after the ack is posted")

        # --- A8: THE CONTROL. One fixture, two rules. The old one must refuse the landing;
        #     the live one must admit it. Without this the exam is decoration.
        fx = os.path.join(tmp, "control_state.json")
        now = time.time()
        entries = [{"ts": now - 60, "channel": _RX_CH, "reply_to": _RX_MID_A,
                    "norm": _norm(_RX_ACK)}]
        with open(fx, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        check(_rx_old_rule1(entries, _RX_CH, _RX_MID_A, now),
              "A8 control: the OLD content-blind rule 1 REFUSES the landing reply",
              "it admitted it- the exam can no longer detect the bug it guards")
        _sv = (STATE, LOCK)
        STATE, LOCK = fx, fx + ".lock"
        try:
            dup, why = is_duplicate(_RX_CH, _RX_MID_A, _RX_LANDING)
            dup_ack, _ = is_duplicate(_RX_CH, _RX_MID_A, _RX_ACK)
        finally:
            STATE, LOCK = _sv
        check(not dup, "A8 control: the LIVE rule ADMITS it, on that identical fixture", why)
        check(dup_ack, "A8 control: the LIVE rule still refuses the ack on that fixture")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    viol = _exam_footprint_violations(before, _ledger_snapshot(live), time.time())
    check(not [m for k, m in viol if k == "footprint"],
          "live dedup state: no exam fixture send reached it",
          "; ".join(m for k, m in viol if k == "footprint"))
    check(not [m for k, m in viol if k == "lost"],
          "live dedup state: no in-window send was lost",
          "; ".join(m for k, m in viol if k == "lost"))

    if fails:
        print("reply selftest FAILED: %s" % ", ".join(fails))
        return 1
    print("reply selftest OK: baxter_say posts a build's landing reply to a message the live "
          "session already acked, at 60s and at 17000s; and still refuses a repeat of either, "
          "identical or paraphrased, inside and beyond the 1800s exact-window.")
    return 0


def selftest_rules():
    """The blind spots of --selftest-reply, which only ever drives REPLIES through baxter_say:
    rule 2's plain-send (no reply_to) path, the CONTENT_WINDOW edge, and two distinct inbound
    messages each keeping their own answer. is_duplicate() is called directly here- the door is
    already covered by the reply exam. Returns 0 / 1."""
    global STATE, LOCK
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="bxr_dedup_rules_")
    fails = []

    def check(ok, name):
        print(("  ok   " if ok else "  FAIL ") + name)
        if not ok:
            fails.append(name)

    def age(sec):
        with open(STATE, encoding="utf-8") as f:
            entries = json.load(f)
        for e in entries:
            e["ts"] = float(e["ts"]) - sec
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(entries, f)

    _sv = (STATE, LOCK)
    STATE = os.path.join(tmp, "rules_state.json")
    LOCK = STATE + ".lock"
    assert os.path.dirname(STATE) == tmp, "STATE escaped the temp dir"
    try:
        # --- rule 2, the plain-send path. Untouched by the content-aware rule 1.
        record("111", None, "Morning brief, sir. Three items on the desk.")
        check(is_duplicate("111", None, "Morning brief, sir. Three items on the desk.")[0],
              "R1 plain send: an identical brief is refused")
        check(is_duplicate("111", None, "Morning brief, sir. Two items on the desk.")[0],
              "R2 plain send: a near-identical brief is refused")
        check(not is_duplicate("111", None, "Wind-down, sir. Nothing outstanding.")[0],
              "R3 plain send: a distinct brief is posted")
        check(not is_duplicate("111", "77", "Wind-down, sir. Nothing outstanding.")[0],
              "R4 a reply never collides with a plain send")

        # --- rule 2's window is still CONTENT_WINDOW, not TARGET_WINDOW.
        age(CONTENT_WINDOW + 60)
        check(not is_duplicate("111", None, "Morning brief, sir. Two items on the desk.")[0],
              "R5 plain send: the near-dupe rule expires at CONTENT_WINDOW")
        check(is_duplicate("111", None, "Morning brief, sir. Three items on the desk.")[0],
              "R6 rule 1b still refuses identical text for EXACT_WINDOW")

        # --- one answer per inbound message, but a DISTINCT answer is a second answer.
        record("222", "1", "Answer to the first, sir.")
        check(not is_duplicate("222", "2", "Answer to the second, sir.")[0],
              "R7 two inbound messages each keep their own reply")
        check(not is_duplicate("222", "1", "The build has landed, sir.")[0],
              "R8 a distinct reply to an answered message is posted")
        check(is_duplicate("222", "1", "Answer to the first, sir.")[0],
              "R9 a repeat of that reply is refused")
        age(EXACT_WINDOW + 1200)                 # past rule 1b; only rule 1's content test is left
        check(is_duplicate("222", "1", "Answer to the first, sir.")[0],
              "R10 an aged repeat is still refused, EXACT_WINDOW having lapsed")
        check(not is_duplicate("222", "1", "The build has landed, sir.")[0],
              "R11 an aged distinct reply still passes")
    finally:
        STATE, LOCK = _sv
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("rules selftest FAILED: %s" % ", ".join(fails))
        return 1
    print("rules selftest OK: rule 2's plain-send path and CONTENT_WINDOW edge are unchanged, "
          "and one message keeps one answer per distinct text.")
    return 0


# ---- the arm-6 acceptance exam (--selftest-arm6) ------------------------------------------
# Arm 6 asserts that --selftest-ledger leaves no mark on the owner's live ledgers. Such an arm is
# worthless in two opposite directions, and this exam pins both:
#   * TOO LOOSE- it passes even when the exam DOES write a live ledger. Proven false by an A/B
#     red-proof: a contained copy of the two modules is run twice, identical but for the single
#     line `bf.HANDLED = led` (the temp redirect) deleted from _FASTLANE_CHILD. Arm 6 must print
#     ok in run A and FAIL in run B, naming .baxter_fast_handled.json.
#   * TOO TIGHT- it fails on ordinary fleet churn, which is what the sha256 arm it replaced did.
#     Proven false by driving the REAL --selftest-ledger against the LIVE tree while the fast
#     lane, the listener and every baxter_say send are running.
# Import order MATTERS and is not cosmetic: baxter_fast imports baxter_rules, which does its own
# sys.path.insert(0, <real utils>) at line 31. Import baxter_fast first and the REAL
# baxter_send_dedup is bound underneath it- the copy never loads, and the red-proof would run
# against the owner's live STATE. _FASTLANE_CHILD imports dedup first for exactly this reason; the
# probe mirrors it, and asserts on __file__ so a future reordering cannot pass silently.
_ARM6_CHILD_PROBE = '''\
import sys, os, json
sys.path.insert(0, sys.argv[1])
import baxter_send_dedup as d
import baxter_fast as bf
print(json.dumps({"HANDLED": bf.HANDLED, "REACTED": bf.REACTED, "VAULT": bf.VAULT,
                  "STATE": d.STATE, "LEDGERS": d._LIVE_LEDGERS,
                  "dedup_file": d.__file__, "fast_file": bf.__file__,
                  "bound_dedup": bf.dedup.__file__}))
'''


# The fleet, in miniature. Appends BENIGN ids to the copy's vault ledgers and records real sends
# into its dedup state for `secs`, exactly as the fast lane, the listener and baxter_say do while
# a live exam runs. .baxter_fast_reacted.json is seeded AT its cap of 300, as it is on the owner's
# disk, so every append here also EVICTS its oldest id- the churn the old sha256 arm died on.
_ARM6_CHURN_CHILD = '''\
import sys, os, time
sys.path.insert(0, sys.argv[1])
import baxter_send_dedup as d

vault, secs = sys.argv[2], float(sys.argv[3])
fh = os.path.join(vault, ".baxter_fast_handled.json")
lh = os.path.join(vault, ".baxter_listener_handled.json")
fr = os.path.join(vault, ".baxter_fast_reacted.json")
end, n = time.time() + secs, 0
while time.time() < end:
    n += 1
    d.update_ids(fh, ["200000000000000%04d" % n], cap=200)
    d.update_ids(lh, ["300000000000000%04d" % n], cap=500)
    d.update_ids(fr, ["400000000000000%04d" % n], cap=300)   # at cap: appends AND evicts
    d.record("111", "77%d" % n, "A real reply to the owner, number %d." % n)
    with open(sys.argv[4], "w") as f:                        # the parent TERMINATEs us, so the
        f.write(str(n))                                      # count cannot wait for the exit
    time.sleep(0.15)
'''


def _arm6_tree(root, drop_redirect):
    """A self-contained COPY of baxter_send_dedup + baxter_fast under `root`, every absolute
    path rewritten into it. `drop_redirect` deletes `bf.HANDLED = led` from the copy's
    _FASTLANE_CHILD- the ONE line that keeps the fast-lane arm off the real claim ledger.
    Returns (utils_dir, vault_dir, dedup_copy, problems)."""
    utils = os.path.join(root, "utils")
    vault = os.path.join(root, "vault")
    os.makedirs(utils, exist_ok=True)
    os.makedirs(vault, exist_ok=True)
    problems = []

    with open(os.path.join(_UTILS, "baxter_send_dedup.py"), encoding="utf-8") as f:
        src = f.read()
    with open(os.path.join(_UTILS, "baxter_fast.py"), encoding="utf-8") as f:
        fsrc = f.read()

    def sub(text, old, new, what):
        n = text.count(old)
        if n != 1:
            problems.append("%s: expected 1 occurrence, found %d" % (what, n))
        return text.replace(old, new)

    src = sub(src, 'STATE = r"%s"' % STATE,
              'STATE = r"%s"' % os.path.join(vault, ".baxter_send_dedup.json"),
              "dedup STATE literal")
    # baxter_fast's VAULT is a hardcoded absolute, NOT __file__-derived: HANDLED and REACTED
    # both hang off it, so this one line is the whole of the copy's containment. If it ever
    # stops being a literal, sub() reports it and _arm6_probe below still refuses to proceed.
    fsrc = sub(fsrc, 'VAULT = r"%s"' % VAULT, 'VAULT = r"%s"' % vault, "baxter_fast VAULT literal")
    # Every needle is BUILT at run time, never written out as a literal: this function lives in
    # the very file it rewrites, so a spelled-out needle occurs twice and sub() replaces both.
    src = sub(src, "PROCS, PER = %d, %d" % (6, 60), "PROCS, PER = 4, 10", "hammer size")
    # arm 5 shells out to the real baxter_slash; the copy must not. Named slash_stub, never
    # baxter_slash.py- baxter_rules imports that lazily and a shadowing stub would break it.
    src = sub(src, 'slash = os.path.join(_UTILS, "%s.py")' % "baxter_slash",
              'slash = os.path.join(_UTILS, "slash_stub.py")', "arm 5 listener child")
    if drop_redirect:
        src = sub(src, "bf.HANDLED = led\n", "", "the _FASTLANE_CHILD temp redirect")

    for name, text in (("baxter_send_dedup.py", src), ("baxter_fast.py", fsrc)):
        if VAULT in text:
            problems.append("%s: the live vault path survived the rewrite" % name)
        with open(os.path.join(utils, name), "w", encoding="utf-8") as f:
            f.write(text)
    with open(os.path.join(utils, "slash_stub.py"), "w", encoding="utf-8") as f:
        f.write("raise SystemExit(0)\n")

    # Seed the copy's vault so rules (b) and (c) have something real to protect. fast_reacted is
    # seeded AT its cap of 300, as it is on the owner's disk: every 👀 reaction the fleet sends while
    # an exam runs therefore evicts its oldest id, and rule (b) must forgive exactly that.
    for n, data in ((".baxter_fast_handled.json", {"ids": ["seed-fh-1", "seed-fh-2"]}),
                    (".baxter_listener_handled.json", {"ids": ["seed-lh-1"]}),
                    (".baxter_fast_reacted.json", {"ids": ["seed-fr-%d" % k for k in range(300)]}),
                    (".baxter_send_dedup.json",
                     [{"ts": time.time(), "channel": "111", "reply_to": "222", "norm": "seeded"}])):
        with open(os.path.join(vault, n), "w", encoding="utf-8") as f:
            json.dump(data, f)
    return utils, vault, os.path.join(utils, "baxter_send_dedup.py"), problems


def _arm6_probe(utils, root):
    """Ask the COPY, in its own process, where its paths actually resolve. A source-text
    containment check is not enough: the red-proof only means anything if the copy's
    bf.HANDLED is a file arm 6 watches. Returns (info, problems)."""
    probe = os.path.join(root, "probe.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(_ARM6_CHILD_PROBE)
    env = dict(os.environ, PYTHONPATH=_UTILS)   # baxter_lanes/_rules/_usage are not copied
    import subprocess, sys
    p = subprocess.run([sys.executable, probe, utils], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    if p.returncode != 0:
        return {}, ["probe child rc=%d: %s" % (p.returncode, (p.stderr or "")[-300:])]
    info = json.loads(p.stdout.strip().splitlines()[-1])
    problems = []
    for k in ("HANDLED", "REACTED", "VAULT", "STATE", "dedup_file", "fast_file", "bound_dedup"):
        if not os.path.abspath(info[k]).startswith(os.path.abspath(root) + os.sep):
            problems.append("copy's %s escaped the temp tree: %s" % (k, info[k]))
    if info["HANDLED"] not in info["LEDGERS"]:
        problems.append("copy's bf.HANDLED is not one of the ledgers arm 6 watches- "
                        "the red-proof would prove nothing")
    return info, problems


def _arm6_sha(paths):
    """The sha256 of each path- the exact measure the OLD arm 6 used. Kept only so the churn arm
    can show what that arm would have said about a run this one passes."""
    import hashlib
    out = {}
    for p in paths:
        try:
            with open(p, "rb") as f:
                out[p] = hashlib.sha256(f.read()).hexdigest()
        except FileNotFoundError:
            out[p] = "<absent>"
    return out


# The two check names arm 6 emits. Named once: the red-proof, the churn arm and the live arm all
# grep for them, and a rename that reached only one of the three would quietly stop asserting.
_ARM6_FP = "live ledgers: no exam fixture id or channel reached one"
_ARM6_LOST = "live ledgers: no id or in-window send was lost"


def _selftest_arm6():
    """Prove arm 6 of --selftest-ledger is a real assertion and not a race. Returns 0 / 1."""
    import sys, tempfile, shutil, subprocess
    t0 = time.time()
    fails = []

    def check(ok, name, detail=""):
        print(("  ok   " if ok else "  FAIL ") + name + (("- " + detail) if detail else ""))
        if not ok:
            fails.append(name)

    # --- part 0: the two exams share ONE fixture table, so neither can drift out of the
    #     other's reach. The reply exam invents these; the ledger exam must know them.
    check(_RX_CH in _EXAM_FIXTURE_CHANNELS,
          "fixtures: the reply exam's channel is declared as a fixture", _RX_CH)
    check(_RX_MID_A in _EXAM_FIXTURE_IDS and _RX_MID_B in _EXAM_FIXTURE_IDS,
          "fixtures: the reply exam's message ids are declared as fixtures")
    check(_is_fixture_id("L-0-3") and _is_fixture_id("C-5-59") and _is_fixture_id("X-0-0")
          and _is_fixture_id("fast-7") and _is_fixture_id("9999999999"),
          "fixtures: every id shape the exams write is recognised")
    check(not _is_fixture_id("4444444444444444402") and not _is_fixture_id("fastlane")
          and not _is_fixture_id("999000111"),
          "fixtures: a real snowflake is NOT mistaken for one")

    # --- part 1: every verdict of the pure detector, off a synthetic table. No fleet, no disk.
    P = os.path.join("X:", "vault", ".baxter_fast_handled.json")
    D = os.path.join("X:", "vault", ".baxter_send_dedup.json")
    now = 1000000.0
    W = max(TARGET_WINDOW, CONTENT_WINDOW)
    fresh = (now - 10, "111", "222", "aaaa")
    older = (now - W - 60, "111", "333", "bbbb")
    fx_ch = (now - 5, _RX_CH, None, "cccc")
    fx_rt = (now - 5, "111", _RX_MID_A, "dddd")
    CON = "4444444444444444401"

    def i(*v):
        return {P: list(v)}

    def d(*v):
        return {D: list(v)}

    cases = [
        # (label, before, after, expected violation kinds)
        ("a benign append", i("a", "b"), i("a", "b", "c"), []),
        ("a cap eviction while the ledger grows", i("1", "2", "3"), i("2", "3", "4"), []),
        ("a cap eviction that evicts a fixture id", i(CON, "a"), i("a", "b"), []),
        ("the contested id already present, and still present", i(CON, "a"), i(CON, "a", "b"), []),
        ("a dedup entry aged past the prune window", d(older, fresh), d(fresh), []),
        ("an untouched dedup ledger", d(fresh), d(fresh), []),
        ("the contested id NEWLY added", i("a"), i("a", CON), ["footprint"]),
        ("a hammer block id added", i("a"), i("a", "L-0-3"), ["footprint"]),
        ("a listener-exam id added", i("a"), i("a", "fast-7"), ["footprint"]),
        ("the fast-lane sentinel added", i("a"), i("a", "9999999999"), ["footprint"]),
        ("a send recorded on the exam's channel", d(fresh), d(fresh, fx_ch), ["footprint"]),
        ("a send recorded to the exam's message id", d(fresh), d(fresh, fx_rt), ["footprint"]),
        ("a middle id deleted", i("a", "b", "c"), i("a", "c"), ["lost"]),
        ("the newest ids clobbered by a stale write", i("a", "b", "c"), i("a", "b"), ["lost"]),
        ("a head loss with nothing appended", i("a", "b", "c"), i("b", "c"), ["lost"]),
        ("an in-window send destroyed", d(older, fresh), d(older), ["lost"]),
        ("a clobber that also plants a fixture", i("a", "b", "c"), i("a", "c", "9999999999"),
         ["footprint", "lost"]),
    ]
    for label, before, after, want in cases:
        got = sorted({k for k, _m in _exam_footprint_violations(before, after, now)})
        check(got == sorted(want), "detector: %s -> %s" % (label, want or "clean"),
              "" if got == sorted(want) else "got %s" % got)

    # --- parts 2-4: the CONTAINED A/B red-proof.
    root = tempfile.mkdtemp(prefix="bxr_arm6_")
    live_before = _ledger_snapshot()         # containment, measured on the REAL ledgers
    runs = {}
    try:
        for mutated in (False, True):
            sub = os.path.join(root, "B" if mutated else "A")
            utils, _vault, dedup_copy, problems = _arm6_tree(sub, drop_redirect=mutated)
            check(not problems, "red-proof %s: the copy rewrote every absolute path"
                  % ("B" if mutated else "A"), "; ".join(problems))
            info, pp = _arm6_probe(utils, sub)
            check(not pp, "red-proof %s: the copy's paths resolve inside its own tree"
                  % ("B" if mutated else "A"), "; ".join(pp))
            if problems or pp:
                raise RuntimeError("containment failed- refusing to run the red-proof")
            p = subprocess.run([sys.executable, dedup_copy, "--selftest-ledger"],
                               capture_output=True, text=True, encoding="utf-8", timeout=420,
                               env=dict(os.environ, PYTHONPATH=_UTILS))
            runs["B" if mutated else "A"] = (p.returncode, (p.stdout or "") + (p.stderr or ""))

        FP = _ARM6_FP
        rc_a, out_a = runs["A"]
        rc_b, out_b = runs["B"]
        line_a, line_b = _arm6_line(out_a, FP), _arm6_line(out_b, FP)

        def only_if_bad(ok, detail):
            return "" if ok else (detail or "arm 6 said nothing about the fixture arm")

        ok = ("  ok   " + FP) in out_a
        check(ok, "red-proof A (redirect intact): arm 6 passes- the exam wrote no ledger",
              only_if_bad(ok, line_a))
        ok = ("  FAIL " + FP) in out_b
        check(ok, "red-proof B (redirect deleted): arm 6 FAILS- the exam wrote a ledger",
              only_if_bad(ok, line_b))
        ok = ".baxter_fast_handled.json" in line_b
        check(ok, "red-proof B: arm 6 names the ledger the exam wrote", only_if_bad(ok, line_b))
        ok = "9999999999" in line_b or CON in line_b
        check(ok, "red-proof B: arm 6 names the fixture id it found", only_if_bad(ok, line_b))
        check(rc_b != 0, "red-proof B: a footprint still FAILS the whole exam", "rc=%d" % rc_b)
        print("  note run A exited %d, run B exited %d- one deleted line apart" % (rc_a, rc_b))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # containment, on the real tree: the red-proof must not have written the owner's ledgers.
    real_fh = os.path.join(VAULT, ".baxter_fast_handled.json")
    check("9999999999" not in read_ids(real_fh),
          "containment: the sentinel is absent from the real .baxter_fast_handled.json")
    viol = _exam_footprint_violations(live_before, _ledger_snapshot(), time.time())
    check(not viol, "containment: the red-proof left no footprint on the real ledgers",
          "; ".join(m for _k, m in viol))

    # --- part 4b: the DETERMINISTIC race arm, and the reason this build exists. Part 5 below
    # only proves race-immunity if the fleet HAPPENS to write while it runs, which is luck: on
    # 10th July the same code exited 1, then 0, then 0. Here a churn child rewrites the copy's
    # four ledgers throughout the exam- appending ids, evicting at cap, recording real sends-
    # and arm 6 must stay green while every one of those files moves byte-wise. The sha256 arm
    # this replaced would have graded such a run FAILED, and did.
    root = tempfile.mkdtemp(prefix="bxr_arm6_churn_")
    try:
        utils, vault, dedup_copy, problems = _arm6_tree(root, drop_redirect=False)
        info, pp = _arm6_probe(utils, root)
        check(not (problems or pp), "churn arm: the copy is contained in its own tree",
              "; ".join(problems + pp))
        if problems or pp:
            raise RuntimeError("containment failed- refusing to run the churn arm")

        churn = os.path.join(root, "churn.py")
        with open(churn, "w", encoding="utf-8") as f:
            f.write(_ARM6_CHURN_CHILD)
        counter = os.path.join(root, "churn_count.txt")
        env = dict(os.environ, PYTHONPATH=_UTILS)
        led_paths = [os.path.join(vault, os.path.basename(q)) for q in _LIVE_LEDGERS]

        h_before = _arm6_sha(led_paths)
        writer = subprocess.Popen([sys.executable, churn, utils, vault, "120", counter],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=env)
        try:
            time.sleep(0.5)                 # let the churn get ahead of the exam's snapshot
            p = subprocess.run([sys.executable, dedup_copy, "--selftest-ledger"],
                               capture_output=True, text=True, encoding="utf-8",
                               timeout=420, env=env)
        finally:
            writer.terminate()
            try:
                writer.communicate(timeout=60)
            except Exception:
                writer.kill()
        h_after = _arm6_sha(led_paths)
        moved = [os.path.basename(q) for q in led_paths if h_before[q] != h_after[q]]
        try:
            with open(counter) as f:
                wrote = int(f.read().strip())
        except Exception:
            wrote = 0
        out_c = (p.stdout or "") + (p.stderr or "")

        check(len(moved) == 4 and wrote > 0,
              "churn arm: all four ledgers were rewritten under the running exam",
              "moved %s after %d churn rounds" % (moved, wrote))
        ok = ("  ok   " + _ARM6_FP) in out_c
        check(ok, "churn arm: the fixture arm stays green through the churn",
              "" if ok else (_arm6_line(out_c, _ARM6_FP) or "the fixture arm did not run"))
        ok = ("  ok   " + _ARM6_LOST) in out_c
        check(ok, "churn arm: the loss arm stays green through cap eviction and pruning",
              "" if ok else (_arm6_line(out_c, _ARM6_LOST) or "the loss arm did not run"))
        check(p.returncode == 0,
              "churn arm: the exam exits 0- the sha256 arm would have called this run MUTATED",
              "" if p.returncode == 0 else
              "rc=%d; %s" % (p.returncode, " | ".join(l.strip() for l in out_c.splitlines()
                                                      if l.startswith("  FAIL"))))
        print("  note churn arm: %d rounds of appends, cap evictions and sends landed on %d "
              "ledgers mid-run" % (wrote, len(moved)))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- part 5: drive the REAL exam, on the LIVE tree, while the fleet runs.
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "--selftest-ledger"],
                       capture_output=True, text=True, encoding="utf-8", timeout=420)
    out = (p.stdout or "") + (p.stderr or "")
    LOST = _ARM6_LOST
    ok = ("  ok   " + FP) in out
    check(ok, "live exam: the fixture arm passes against real fleet churn",
          "" if ok else (_arm6_line(out, FP) or "the fixture arm did not run"))
    ok = ("  ok   " + LOST) in out
    check(ok, "live exam: the loss arm passes against real fleet churn",
          "" if ok else (_arm6_line(out, LOST) or "the loss arm did not run"))
    check("byte-identical" not in out,
          "live exam: the sha256 arm is GONE, not merely bypassed")
    check(p.returncode == 0, "live exam: --selftest-ledger exits 0",
          "" if p.returncode == 0 else
          "rc=%d; failing lines: %s" % (p.returncode,
                                        " | ".join(l.strip() for l in out.splitlines()
                                                   if l.startswith("  FAIL"))))

    print("  note total wall time %.0fs" % (time.time() - t0))
    if fails:
        print("arm6 selftest FAILED: %s" % ", ".join(fails))
        return 1
    print("arm6 selftest OK: arm 6 catches the exam's own footprint- delete one redirect line "
          "and it reddens, naming the ledger and the id- and it stays green on the live tree "
          "while the fast lane, the listener and every send rewrite those files underneath it.")
    return 0


def _arm6_line(out, name):
    for line in out.splitlines():
        if name in line:
            return line.strip()
    return ""


def _reset():
    _save([])
    print("dedup state cleared")
    return 0


def _usage():
    print("baxter_send_dedup: import guard()/is_duplicate()/record(), or run --hook")
    return 0


def _main(argv):
    """Dispatch flags EXPLICITLY and refuse any flag we don't know.

    THE FALSE GREEN THIS CLOSES (measured 9th July): this __main__ used to fall through every
    `if` to a help line and exit 0. `--selftest-ledger` therefore PASSED before the selftest
    existed, and any verify gate naming it was green having executed nothing. A gate you can
    pass by naming a test that was never built is worse than no gate. Unknown -> exit 2, on
    stderr, the way baxter_slash._run_selftests already does it."""
    import sys
    reg = {"--bash-hook": _bash_hook, "--hook": _hook,
           "--reset": _reset, "--selftest-ledger": selftest_ledger,
           "--selftest-reply": selftest_reply, "--selftest-rules": selftest_rules,
           "--selftest-arm6": _selftest_arm6}
    flags = [a for a in argv if a.startswith("-")]
    unknown = [f for f in flags if f not in reg]
    if unknown:
        print("unknown flag(s): %s" % " ".join(unknown), file=sys.stderr)
        print("known: %s" % " ".join(sorted(reg)), file=sys.stderr)
        return 2
    if not flags:
        return _usage()
    for f in flags:                 # hooks run alone; a selftest may not be silently paired
        rc = reg[f]()
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
