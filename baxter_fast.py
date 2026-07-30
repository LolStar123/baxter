"""baxter_fast — the FAST LANE. Polled every ~15s by the watcher (independently of the
main triage lock), it catches the owner's questions/commands and answers within ~a minute,
while the heavy triage handles filing on its own schedule.

Division of labour:
  fast lane  -> quick REPLY (answer a question, confirm a command it can safely do)
  main triage -> everything durable (filing, tasks, drafts, starters) - it sees a note
                 on messages the fast lane already replied to, so it never double-replies.
"""
import json, os, re, subprocess, sys, time, urllib.request, uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baxter_lanes as lanes   # 3-lane concurrency + session ledger (the owner, 8th July)
import baxter_rules as rules       # EVERY prompt rule, defined ONCE. Never retype one into a
                                   # prompt here- that duplication was the bug (the owner, 9th July).
                                   # It re-exports the reminder + channel-read rules too.
import baxter_send_dedup as dedup  # THE one cross-process locking scheme. The shared claim
                                   # ledger (.baxter_fast_handled.json) is written by this lane
                                   # AND by baxter_slash's listener; every write goes through
                                   # dedup.update_ids/claim_ids so a claim is never clobbered.
import baxter_usage as gov         # queue_read/enqueue/position_line. Imported, not shelled
                                   # out to: the placeholder must be WRITTEN before this
                                   # process says a word about it, and an import cannot half-
                                   # succeed the way a subprocess can. Stdlib-only, no network
                                   # at import (baxter_triage imports it the same way).
import baxter_autobuild as autobuild   # the voiceless denial sink- see _send_ack()
import baxter_siblings as siblings     # THE addressed-to check, shared with the
                                       # listener. A message the owner sent to Codex/Jem
                                       # is not this lane's to queue, ack or answer.
REPLY_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baxter_reply_worker.py")

VAULT = r"C:\Users\you\Documents\Baxter"
SECRETS = os.path.join(VAULT, ".baxter_secrets.json")
CURSOR = os.path.join(VAULT, ".baxter_fast_cursor.json")
HANDLED = os.path.join(VAULT, ".baxter_fast_handled.json")
REACTED = os.path.join(VAULT, ".baxter_fast_reacted.json")   # 👀 WORK-START receipts sent (the owner, 8th July: fires when work begins, not on sight)
# Receipts: message_id -> the queue entry written for it, BEFORE any 'queued' reply went out.
# The triage reconciler reads these back and pings if a 'queued' claim has none behind it.
ACKS = os.path.join(VAULT, ".baxter_queue_acks.json")
FLOCK = os.path.join(VAULT, ".baxter_fast.lock")
SAY = r"C:\Users\you\Documents\Python Scripts\utils\baxter_say.py"

# /usage is handled by its OWN orthogonal poller now (baxter_usage_cmd.py, 6th July v2)-
# a pure state-file read + reply, decoupled from this fast lane's 240s FLOCK so it can
# never wait on a slow claude quick-reply the way v1 did (the ~2-min hang the owner hit).
# /off and /on are the master pause switch (the owner, 6th July): /off drops a flag that makes
# the main triage soft-pause (no briefs, pings, filing or background work); /on clears it.
# The fast lane itself keeps running so /on and his questions always land. Pure code path.
OFF_CMD = re.compile(r"^/?off$", re.I)
ON_CMD = re.compile(r"^/?on$", re.I)
OFF_FLAG = os.path.join(VAULT, ".baxter_off")
# On /on we drop a one-shot marker so the FIRST triage pass after a pause summarises
# what landed while off (backfill), instead of silently draining or a brief-storm.
CATCHUP_FLAG = os.path.join(VAULT, ".baxter_catchup")
UA = "DiscordBot (https://baxter.local, 1.0)"

# looks-like-it-wants-a-response heuristic: questions, status asks, quick commands
RESPONSIVE = re.compile(
    r"\?|^(what|whats|what's|when|where|who|why|how|is|are|am|do|does|did|can|could|should|status|update)\b"
    r"|^(done|snooze|cut|remove|delete|move|rename|roll|pause|resume|undo|revert|cancel)\b",
    re.I)

def log(msg):
    try:
        with open(os.path.join(VAULT, ".baxter.log"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] fast-lane: {msg}\n")
    except Exception:
        pass

# ---- the shared claim ledger. THREE processes touch .baxter_fast_handled.json: this lane,
# baxter_slash's real-time listener (which claims here to mute the poll), and baxter_triage
# (reader, unlocked). Every write below goes through baxter_send_dedup's ONE locking scheme-
# locked, merge-not-replace, tmp+os.replace. Never truncate-and-rewrite this file again: an
# unlocked read-modify-write is what let this lane's stale snapshot delete the listener's claim and
# re-answer msg 444444444444444401 with a second Opus worker (9th July).
HANDLED_CAP = 200


def _handled_ids():
    """Every message id already claimed/answered, by either lane."""
    return dedup.read_ids(HANDLED)


def _claim_handled(new_ids):
    """Merge ids into the shared ledger under the lock. Returns the ids WE won- the ones no
    other writer had already claimed. An id we did not win belongs to the listener: do not
    work it. Order is preserved and the last HANDLED_CAP ids survive."""
    won, degraded = dedup.claim_ids(HANDLED, [str(i) for i in new_ids], cap=HANDLED_CAP)
    if degraded:
        log(f"claim ledger write DEGRADED (lock timeout or write failure) for {list(new_ids)}")
    return won


def _drop_handled(msgs):
    """The messages nobody has claimed yet- the real re-pick gate, read fresh off the ledger."""
    done = set(_handled_ids())
    return [m for m in msgs if str(m["id"]) not in done]


STOP_FLAG = os.path.join(VAULT, ".baxter_stop")
BREACH_FLAG = os.path.join(VAULT, ".baxter_breach")
OVERRIDE_FLAG = os.path.join(VAULT, ".baxter_override")
LIVE = os.path.join(VAULT, ".baxter_usage_live.json")
USAGE = r"C:\Users\you\Documents\Python Scripts\utils\baxter_usage.py"
BOT_ID = "333333333333333302"

# A BIG/sizeable ask (the owner, 6th July "wall behaviour" clarification)- a build/research/
# design job that must NOT be run live at the wall. When big work is gated (the 70-90
# band) these get a zero-cost templated 'logged, addressed after reset' ack + go high on
# the build queue, instead of burning a live claude reply. Deliberately CONSERVATIVE:
# a small ask wrongly acked reads as the "shell" the owner hates, so lean small when unsure
# (a false live answer is cheap; a false ack is not). Quick questions never match this.
BIG_ASK = re.compile(
    r"\b(build|rebuild|implement|develop|set ?up|wire ?up|integrate|migrate|refactor|automate|architect)\b"
    r"|\b(research|investigate|deep[- ]?dive|analyse|analyze)\b"
    r"|\bdesign (a|an|the|me)\b"
    r"|\b(make|create|write|draft|code) (me )?(a |an |the )?(new )?"
    r"(script|tool|app|dashboard|feature|system|bot|pipeline|integration|agent|plan|spec|prd|command)\b"
    # ADDED 9th July, repair pass. The INCIDENT'S OWN two messages- "run a rigorous
    # post-crash health check" and "root-cause the crash"- tripped none of the verbs above,
    # so the very asks this build exists to protect were invisible to it. Both are ordinary
    # English for "do a sizeable piece of work", and both are how he actually phrases it.
    # `root-cause` matches the VERB only. A verb takes an object, so it must be FOLLOWED by
    # one ("root-cause the crash", "root-causing it"); and the lookbehinds drop the noun
    # phrase ("the root cause of X", "a good root cause analysis"). Both halves are needed:
    # the lookahead alone admits "a root cause that explains it", the lookbehinds alone admit
    # "a good root cause analysis". Neither is an order to go and find one.
    r"|(?<!the )(?<!a )\broot[- ]?caus(e|ing)\b(?=\s+(the|it|this|that|why|our|these)\b)"
    r"|\b(run|do|perform|carry out) (a |an |the )?[\w -]{0,24}?"
    r"(health[- ]?check|post[- ]?mortem|diagnostic)\b",
    re.I)

# VITAL never gets PARKED (4-tier governor)- CoC troubleshooting + emergencies run through
# the 80-90 band rather than waiting on the reset. So even when a message trips BIG_ASK
# ("investigate why the coc bot keeps crashing"), an emergency marker exempts it from the
# templated wall ack: it falls through to the live lane / main session.
#
# IT DOES NOT EXEMPT IT FROM BEING WRITTEN DOWN (repair, 9th July). This regex used to sit
# inside _is_big_ask, which conflated two different questions- "is this sizeable work?" and
# "may it wait till reset?". Only the second is about urgency. The cost of the conflation was
# the exact incident in the task text: his 09:18 crash asks were vital AND sizeable, so the
# fast lane skipped the placeholder, told him they were queued, and nothing was. A vital big
# ask is queued like any other- at p1, and answered live rather than acked. See _is_vital.
VITAL_EXEMPT = re.compile(
    r"\b(coc|clash)\b|\b(crash\w*|broke\w*|breaking|not working|offline|"
    r"urgent|emergency|asap|on fire|troubleshoot\w*)\b|"
    r"\b(is|are|went|going|goes|be)\s+down\b",   # "the bot is down"- NOT "note down"/"sit down"
    re.I)

# A question ABOUT the build queue/list itself ("what's on the build list?", "what are
# you building?", "anything in the queue?") trips BIG_ASK's bare noun "build"/"task"- but
# it's a QUICK question, not a big ask. Exclude it so it falls through to a LIVE answer,
# never a false ack + junk queue entry (the owner's 6th-July false-ack warning- caught
# "What's on the build list baxter?" wrongly acked + queued at p3, 10:37). Failure mode is
# safe by design: a rare legit big ask worded as "build a task queue" gets answered live
# instead of acked- exactly the conservative "lean small when unsure" bias the owner wants.
QUEUE_Q = re.compile(
    r"\b(build|task|to-?do)\s*(list|queue|order|status|backlog)\b"   # "build list", "task queue"
    r"|\b(on|in)\s+(the|your|my)\s+(list|queue|backlog)\b"           # "on the list", "in the queue"
    r"|\bwhat('?s| is| are|s)?\b[^?]{0,40}\bbuilding\b",             # "what are you building"
    re.I)

# NON-TASK GUARD for the big-ask auto-queue (the owner, 6th July- the fast lane wrongly queued a
# Claude app-download/onboarding message as a p3 build task, removed). A message can trip
# BIG_ASK's verbs ("set up", "integrate"...) while being SYSTEM/ONBOARDING chatter, an
# app-download blurb, a paste, or quoted output- NOT a genuine build ask the owner is handing
# Baxter. Before --queue'ing a sizeable request, require it to read like a real instruction
# and reject non-actionable content. Conservative by design: a rejected message is simply
# dropped from the AUTO-QUEUE- it still gets the wall catch-all ack / live lane / triage, so
# nothing is lost; a false reject just means a live answer, never a silently-lost ask.
NON_TASK = re.compile(
    r"\bdownload (the |our |your )?(claude )?app\b"
    r"|\bapp[- ]?(store|download)\b|\bgoogle play\b|\bplay store\b|\bget it on\b"
    r"|\bavailable (now |today )?(on|for|in) (ios|android|the app store|your)\b"
    r"|\b(install|set ?up|download|get) (claude|the app|the claude app|it) (on|to|for|from)\b"
    r"|\bscan (the |this )?qr\b|\bverify your (email|account)\b"
    r"|\bsign( |-)?in (to|with)\b|\b(create|confirm) (an |your )?account\b"
    r"|\bget started with\b|\bwelcome to\b|\byour free trial\b|\bunsubscribe\b",
    re.I)

def _is_non_task(content):
    """True when a BIG_ASK-tripping message is NOT a genuine build/work ask but system/
    onboarding/app-download text, a paste, or quoted output- so it must NOT be auto-queued
    (the owner, 6th July). Conservative: rejecting only drops it from the queue; the wall
    catch-all / live lane / triage still see it, so nothing is lost."""
    c = (content or "").strip()
    if not c:
        return True
    if NON_TASK.search(c):
        return True
    # quoted output / forwarded paste: half-or-more of the non-blank lines are Discord
    # block-quotes ('> ...')- the owner's own instructions aren't written as quote lines.
    lines = [ln for ln in c.splitlines() if ln.strip()]
    if lines and sum(1 for ln in lines if ln.lstrip().startswith(">")) >= max(2, len(lines) * 0.5):
        return True
    # a paste dominated by a fenced code block / log, with little real instruction around it.
    if "```" in c:
        outside = re.sub(r"```.*?```", "", c, flags=re.S).strip()
        if len(outside) < 40:
            return True
    # a bare shared link with almost no instruction text (a media/URL drop, not a build ask).
    if re.search(r"https?://", c) and len(re.sub(r"https?://\S+", "", c).strip()) < 15:
        return True
    return False

def _breach_active():
    """the owner's gold-spend authorisation (the /breach command)- a future 'until' lifts
    even the 90% hard floor, so the fast lane answers normally above 90 during a breach."""
    try:
        with open(BREACH_FLAG, encoding="utf-8-sig") as f:
            until = json.load(f).get("until", "")
        return datetime.fromisoformat(until) > datetime.now()
    except Exception:
        return False

def _floor_active():
    """The fast lane is VITAL (answering the owner)- in the 4-tier governor it RUNS through
    the 80-90 vital-only band and stops ONLY at the 90%+ HARD FLOOR, where it sends a
    zero-cost templated 'please wait' ack (NO llm burn) instead of a claude quick-reply.
    A breach lifts even the floor. Instant, network-free read of the governor's flag."""
    if _breach_active():
        return False
    try:
        with open(STOP_FLAG, encoding="utf-8-sig") as f:
            return json.load(f).get("level") == "floor"
    except Exception:
        return False

def _reset_clock():
    """Local 'around 12:49pm' string from the baked session reset clock, or '' if
    unreadable. Shared by the floor ack and the big-ask ack- network-free."""
    try:
        with open(LIVE, encoding="utf-8-sig") as f:
            iso = json.load(f).get("session_resets_at", "")
        t = datetime.fromisoformat(iso).astimezone()
        return " around " + t.strftime("%I:%M%p").lower().lstrip("0")
    except Exception:
        return ""

def _floor_ack():
    """The locked zero-cost hard-floor ack (the owner's 6th-July spec)- one butler line, no
    LLM, no probe. Reads the baked reset clock from .baxter_usage_live.json if present."""
    when = _reset_clock()
    return (f"\U0001F6D1 At the wall, sir- usage is in the top 10% reserve, so I'm holding "
            f"everything (myself included) till the window resets{when}. Say 'breach' if it "
            f"genuinely can't wait.")

def _big_ack(position):
    """Zero-cost templated ack for a BIG ask while gated (the owner's 6th-July wall spec):
    logged + queued high, addressed after the window resets. No LLM burn- that's the
    whole point (token conservation on big asks). Reassures him small asks still land
    live, so the wall reads as selective, never a shell.

    `position` is NOT optional and never defaulted (his 9th-July 09:38 order): this ack is
    only ever sent by pre_enqueue(), after every entry of the sweep is written AND the slots
    have been read back off the settled queue- so the number in it is a fact about the queue
    rather than a promise about it. The old version said "I've put it high on the build
    queue" BEFORE any --queue call was made- the sentence that started all of this.

    It is never sent for a VITAL ask: its promise is "once usage resets", which is precisely
    what an emergency cannot wait for. Those are queued at p1 and answered live."""
    if not position:
        # Not defensive padding. With no position there is no entry, and this ack's entire
        # content is the assertion that there is one. Raising here means a future edit that
        # reaches the ack without a write CRASHES the sweep instead of lying to him, and the
        # message is left unhandled for the next sweep to retry.
        raise ValueError("refusing to ack a queue entry that does not exist")
    when = _reset_clock()
    return (f"\U0001F4CB Logged, sir- that's a sizeable one. It {rules.confirm_clause(position)}, "
            f"and I'll get to it once usage resets{when}. Quick questions still reach me "
            f"live in the meantime.")


def _is_vital(m):
    """True when a sizeable ask is ALSO an emergency- a crash, an outage, the CoC bot.

    It changes what he HEARS, never whether the ask is recorded: a vital is answered live
    and queued at p1, and must never receive the wall ack (which promises to get to it after
    the reset- the one thing an emergency cannot do)."""
    return bool(VITAL_EXEMPT.search(m.get("content") or ""))


def _is_big_ask(m, uid):
    """One definition of 'a sizeable ask from the owner that the fast lane must not answer'.

    It used to be an inline comprehension inside the `if _big_gated()` branch, which is why
    the queueing only ever happened at the wall. The band decides whether he gets a
    templated ack or a live reply- it has never had any business deciding whether his ask
    is written down.

    Nor has urgency (repair, 9th July). VITAL_EXEMPT used to be tested here too, so a big ask
    that mentioned a crash was neither acked NOR queued- it fell through every path this
    build added. That is precisely what the checker caught: the two messages named in the
    task, "run a rigorous post-crash health check" and "root-cause the crash", are vital, and
    so the fix written for them never covered them. Vitality is now asked separately, at the
    point where it means something: the ack. See _is_vital.

    Nor has WHO ANSWERS IT (second repair, 9th July- the same conflation again, on the last
    surface it survived on). `not _for_live_session(m)` used to sit here, so an @mention or a
    reply to Baxter- the live channel session's turf- was not a sizeable ask at all, and no
    placeholder was ever written for one. The live session was told by CLAUDE.md to queue big
    work, which rests the truth of "queued, sir" on a model choosing to make a tool call: the
    ack-before-act hole, exactly, on the one surface the 09:38 fix did not cover. Recognising
    an ask and choosing its responder are different questions. The fast lane still does not
    ANSWER a live-session message- main()'s `targets` filter decides that, and it still tests
    _for_live_session- and it still does not wall-ack a fresh one (see pre_enqueue)."""
    c = (m.get("content") or "")
    return bool(str((m.get("author") or {}).get("id")) == uid and c
                and BIG_ASK.search(c)
                and not QUEUE_Q.search(c) and not _is_non_task(c))


def _placeholder(m, gated, vital=False):
    """Write the queue entry FIRST. Returns the entry, or None if the write failed.

    THE FIX (the owner, 9th July 09:38). The ack must not precede the act. Every path that is
    about to tell him something is queued- the templated wall ack, and the live worker's
    reply via its prompt- goes through here first, so the entry exists before the sentence
    describing it does. Keyed on the message id, so triage's later, better-worded filing
    upgrades this row in place instead of forking a twin beside it.

    Returns None on ANY failure. No entry propagates as "you may not say 'queued'", which is
    the whole point: no entry, no claim. On 9th July he was told two builds were queued and
    neither was, because the ack went out on a path that never checked.

    It does NOT return a position. It used to, and the position was read the instant this
    entry landed- so the second big ask of the same sweep silently pushed the first one's
    slot along after its number had already been handed out. A slot is only true relative to
    a whole queue; pre_enqueue reads them all once every write is in."""
    mid = str(m["id"])
    task = (m.get("content") or "")[:280].strip()
    # The room he asked in, straight off the message. Every Discord message object carries
    # `channel_id`; the lane that eventually builds this reads it back off the entry and
    # answers THERE. Without it the finished build reached for baxter_say's `general` default
    # and answered in the wrong channel (the owner, 9th July 17:38).
    cid = str(m.get("channel_id") or "")
    try:
        return gov.enqueue(
            task,
            # SCOPE ONLY (10th July). An unscoped placeholder no longer takes the whole board:
            # it wears UNSCOPED_TAG, serialises against other unscoped entries, and runs beside
            # live builds. That is only safe while its first pass EDITS NOTHING- so the pass has
            # to be told so, in the field the builder prompt renders verbatim.
            "Big ask from the owner, UNSCOPED. This pass SCOPES ONLY- edit no source file. Read the "
            "ask, write its PRD, then declare the real touch-set with `baxter_usage.py --edit "
            "<id> --touch \"<paths>\"` (or --solo if it genuinely rewrites a hub file) and STOP. "
            "A later lane builds it against that declaration.",
            # A raw sentence of his, queued before anything has scoped it: no touch-set can
            # honestly be declared here. It is NOT solo- nobody has read it yet, and the two
            # are different locks now (see baxter_usage.SOLO_LOCK / UNSCOPED_TAG). A p5
            # placeholder that needed an empty fleet to earn a lane simply starved.
            #
            # A vital goes to p1 (the owner-says-first). It is the only priority consistent with
            # never parking it: a crash queued behind the ordinary backlog has been parked in
            # everything but name.
            priority=1 if vital else (3 if gated else 5), touch_set=[], solo=False,
            source_mid=mid, source_channel=cid)
    except Exception as ex:
        log(f"PLACEHOLDER ENQUEUE FAILED for {mid}: {ex}")
        return None


def _receipt(mid, entry, line):
    """Leave the reconciler proof that this entry existed before the reply did. The entry is
    real whether or not this lands- a failed receipt is lost bookkeeping, never a lost task,
    and the reconciler falls back to matching source_mid against the live queue."""
    try:
        acks = _load(ACKS, {}) or {}
        acks[mid] = {"qid": entry.get("id"), "pos": line,
                     "at": datetime.now().isoformat(timespec="seconds")}
        _save(ACKS, dict(sorted(acks.items())[-200:]))
    except Exception as ex:
        log(f"ack receipt write failed for {mid}: {ex}")

def _send_ack(mid, text):
    """The ONE outward call this file makes. Every templated ack- the big-ask, the wall
    catch-all, the hard floor, /off and /on- goes through here, so the exam can stub a single
    named function rather than the whole subprocess module, and a stub that misses it fails
    loudly instead of posting to Discord ([[selftests-stub-every-outward-path]]).

    Returns True only if something actually left. Exit 3 is a DENIAL: baxter_say refused the
    claim, printed why, and sent nothing. This call read neither `check` nor `returncode`, so a
    refused reply vanished into a captured pipe while the caller logged an auto-ack. The reason
    now lands in the denial sink and in this log, and the caller is handed a False it must
    never call a success."""
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable or "python", SAY, "--reply-to", str(mid), text],
                       cwd=VAULT, timeout=30, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True)
    if p.returncode == 3:
        row = autobuild.denial_alert("baxter_fast._send_ack", f"msg {mid}", p.stderr)
        log(f"DENIED: the ack to {mid} was refused- {row['reason']}")
        return False
    return True


def pre_enqueue(msgs, uid, gated):
    """Write every sizeable ask to the build queue BEFORE anything says a word about it.
    Returns {message_id: 'position N of M, pP'} for the replies that follow.

    the owner, 9th July 09:38- the fix this file exists to carry. A sizeable ask is written here,
    in code, the moment it is seen: before the templated wall ack goes out, and before any
    live worker is woken to talk about it. Whatever is said afterwards describes an entry
    that already exists.

    This block used to sit inline in main(), inside `if _big_gated():`- so the only path that
    ever called --queue was the one at the usage wall. Below 80% nothing was written, while
    DEFLECT_RULE still told the woken worker to reply "queued to the build queue"; on 9th
    July, twice, it did, and nothing was. The band still decides what he HEARS (a zero-cost
    ack at the wall, a live reply below it); it no longer decides whether his ask is recorded.

    It is a FUNCTION, not a comment block inside 200 lines of Discord I/O, because the first
    pass at this fix was proven by calling _placeholder() directly- which jumps over the
    `_is_big_ask` gate that turned out to be the actual bug. The exam now drives this.

    Three passes, in this order, and the order is the point:
      1. WRITE every entry.
      2. READ every slot, once, off the settled queue.
      3. SPEAK- ack the ones the wall is holding.
    Reading a slot in pass 1 would print a number the next write immediately invalidates."""
    # `not siblings.message_for_sibling(...)`: a build ask the owner sent to CODEX is not a build ask
    # for Baxter. Before 11th July this list saw only `_is_big_ask`, so `<@codex> build me X`
    # was written to Baxter's queue and wall-acked as his own- junk work, from a message that
    # was never his. The listener claims a stood-back message too, so in the live fleet this
    # rarely sees one; it is tested here anyway, because a claim that races must not become a
    # queue entry.
    big_targets = [m for m in _drop_handled(list(reversed(msgs)))
                   if _is_big_ask(m, uid) and not siblings.message_for_sibling(m, BOT_ID)]

    # 1. THE ACT- every write lands before a single position is read or a word is said.
    written = []
    for m in big_targets[-3:]:
        vital = _is_vital(m)
        e = _placeholder(m, gated, vital)
        if e:
            written.append((m, e, vital))

    # 2. The slots, off the settled queue. A position is a fact about the whole run order,
    #    so it can only be taken once the run order has stopped moving.
    positions = {}
    for m, e, _v in written:
        mid = str(m["id"])
        line = gov.position_line(e)
        if not line:
            continue
        positions[mid] = line
        _receipt(mid, e, line)
        log(f"placeholder queued for {mid}: {line}")

    # 3. THE ACK.
    newly = []
    for m in big_targets[-3:]:
        mid = str(m["id"])
        pos = positions.get(mid, "")
        if _for_live_session(m) and not _stale_for_live(m):
            # An @mention / reply-to-Baxter belongs to the live channel session, and it is
            # ALREADY answering. Its entry is written above like any other- that is the whole
            # point of this pass- but the sentence about it is the live session's to say. A
            # wall ack from here would be a second voice on the same message. The exception is
            # a message the live session demonstrably missed (_stale_for_live), which this lane
            # rescues, and therefore speaks for.
            log(f"BIG-ASK live-session- queued at {pos or 'NOWHERE (write failed)'}, "
                f"ack left to the live session for {mid}")
            continue
        if _is_vital(m):
            # An emergency is never parked till reset, so it never gets the wall ack- that
            # ack's promise is "after the window resets", the one thing a crash cannot wait
            # for. It is recorded at p1 and left to the live lane, which answers him now.
            log(f"BIG-ASK vital- queued at {pos or 'NOWHERE (write failed)'}, left live for {mid}")
            continue
        if not gated:
            # Below the wall he gets a real reply from the live worker, which is handed this
            # exact position in its prompt. Nothing to ack here.
            continue
        if not pos:
            # The write failed. Say nothing about a queue: the wall ack's whole content is
            # "it is queued", so with no entry it would be the very lie this build removes.
            # Falling through leaves the message unhandled, so the next sweep retries it and
            # triage still files it.
            log(f"BIG-ASK not acked- placeholder failed, refusing to claim a queue for {mid}")
            continue
        try:
            if not _send_ack(mid, _big_ack(pos)):
                continue     # denied + recorded; leave it unhandled so the next sweep retries
            newly.append(mid)
            log(f"BIG-ASK auto-ack (no llm) {mid} at {pos} ({m['content'][:40]!r})")
        except Exception as e:
            log(f"big-ask ack failed for {mid}: {e}")
    if newly:
        _claim_handled(newly)
    return positions


def pre_enqueue_one(m, uid, gated):
    """The single-message form of pre_enqueue. Returns (entry, 'position N of M, pP').

    The LISTENER (baxter_slash) calls this IN-PROCESS, synchronously, before it speaks or
    spawns anything. That ordering is the whole build: baxter_slash used to fire a detached
    `--queue` subprocess and an ack at the same instant, so the ack raced the write and the
    position in it was never read off the queue at all. An import cannot half-succeed the way
    a Popen can, and a function that RETURNS the slot cannot be quoted before it has one.

    It runs the same `_is_big_ask` gate pre_enqueue runs- not a second copy of it. Calling
    _placeholder directly would jump that gate, which is the bug this build exists to fix,
    and is exactly how the first pass at the 09:38 fix proved itself green while broken
    ([[exam-must-drive-the-gate]]).

    Returns (None, "") when the message is not a sizeable ask, or when the write failed. No
    entry propagates as "you may not say 'queued'"- see rules.queued_block("").

    It speaks to nobody and marks nothing handled: the caller owns the reply. A single write
    needs no read-back barrier the way pre_enqueue's sweep does, since there is no second
    write to invalidate the slot."""
    if not _is_big_ask(m, uid):
        return None, ""
    vital = _is_vital(m)
    e = _placeholder(m, gated, vital)
    if not e:
        return None, ""
    mid = str(m["id"])
    line = gov.position_line(e)
    if not line:
        return None, ""
    _receipt(mid, e, line)
    log(f"placeholder queued in-process for {mid}: {line}{' (vital)' if vital else ''}")
    return e, line


def _override_active():
    """the owner's --override (big-task breach): a future 'until' lifts the 70-80 big stop.
    Does NOT lift the 80-90 vital-only wall. Network-free read of the flag."""
    try:
        with open(OVERRIDE_FLAG, encoding="utf-8-sig") as f:
            until = json.load(f).get("until", "")
        return datetime.fromisoformat(until) > datetime.now()
    except Exception:
        return False

def _big_gated():
    """True when big/sizeable work is currently PAUSED by the governor (the 70-90 band),
    so a big ask is templated-acked + queued rather than answered with a live claude burn.
    Mirrors baxter_usage.blocked('big') cheaply from the stop flag- no network. A breach
    lifts every band; an --override lifts only the 70-80 big band, never the 80-90 wall.
    'floor' (>=90) is handled separately by _floor_active; None (<70) = everything runs."""
    if _breach_active():
        return False
    try:
        with open(STOP_FLAG, encoding="utf-8-sig") as f:
            level = json.load(f).get("level")
    except Exception:
        return False
    if level == "routine":          # 80-90 vital-only: big paused (override does NOT lift)
        return True
    if level == "big":              # 70-80: big paused unless the owner authorised an override
        return not _override_active()
    return False

def _routine_gated():
    """True when ROUTINE work (triage's filing of plain dumps) is paused- the 80%+ bands
    ('routine' vital-only + 'floor'). Below 80% triage still handles a plain instruction
    within a minute, so no catch-all is needed there. A breach lifts it."""
    if _breach_active():
        return False
    try:
        with open(STOP_FLAG, encoding="utf-8-sig") as f:
            return json.load(f).get("level") in ("routine", "floor")
    except Exception:
        return False

def _noted_ack():
    """Zero-cost catch-all ack (the owner, 6th July gap-fix): a plain instruction sent while
    triage is gated has NO other lane- without this it sits in silence till the reset.
    One butler line, no LLM, no probe. The durable filing still happens later (the msg is
    marked fast-handled, so triage files it but doesn't re-reply)."""
    when = _reset_clock()
    return (f"\U0001F4DD Noted and banked, sir- it's on file and I'll action it properly "
            f"once usage resets{when}. Nothing lost in the meantime.")

def _for_live_session(m):
    """@mention of the bot or a reply to the bot = the instant channel session's turf-
    the fast lane leaves those for the live session (which self-gates its own big work)."""
    if BOT_ID in (m.get("content") or ""):
        return True
    ref = m.get("referenced_message") or {}
    return str((ref.get("author") or {}).get("id")) == BOT_ID

LIVE_MISS_SECS = 120   # a live-session message unanswered this long = the live session
                       # missed it (floored/restarting/disconnected)- the fast lane rescues it
def _stale_for_live(m):
    """True when a message is the live session's turf (@mention/reply-to-bot) but has gone
    UNANSWERED past LIVE_MISS_SECS- i.e. the live session was floored, restarting, or had
    dropped its gateway when it arrived, and it never replays old events. The fast lane then
    picks it up so the owner is never left hanging on an @mention (8th-July gap: 4 @mentions sent
    at the 90% floor got orphaned). The atomic send-dedup guarantees no double if the live
    session later answers too, so rescuing is always safe."""
    if not _for_live_session(m):
        return False
    try:
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat((m.get("timestamp") or "").replace("Z", "+00:00"))).total_seconds()
        return age > LIVE_MISS_SECS
    except Exception:
        return False

# Codex/coop machinery REMOVED IN FULL (the owner, 8th July- "remove every tendril, completely
# uncouple the two"). No coop constants, no coop worker, no handoff drain, no codex exec-
# the former coop channel is just a normal channel served by the standard listener.

def _api_get(path, tok):
    req = urllib.request.Request(f"https://discord.com/api/v10/{path}",
                                 headers={"Authorization": f"Bot {tok}", "User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())

def _reply_parent(m, ch, tok):
    """When the owner used Discord's REPLY feature, the message he replied to IS the context
    (the owner, 8th July- the worker was binding to recent chatter instead of the referenced
    message and answered the wrong topic, ticking the wrong thing). Returns a block to PIN
    as PRIMARY context above the recent-chatter convo, or '' if this isn't a reply.
    Discord embeds the parent as `referenced_message`; if that's absent but a
    `message_reference` id is present, fetch it directly so an OLD referenced message
    (outside the last-12 window) is still resolved."""
    ref = m.get("referenced_message") or {}
    if not ref:
        rid = (m.get("message_reference") or {}).get("message_id")
        if not rid:
            return ""
        try:
            ref = _api_get(f"channels/{ch}/messages/{rid}", tok)
        except Exception as e:
            log(f"reply-parent fetch failed for {m.get('id')}: {e}")
            return ""
    if not ref:
        return ""
    a = ref.get("author") or {}
    who = "Baxter (you)" if a.get("bot") else (a.get("username") or "the owner")
    text = (ref.get("content") or "").replace("\n", " / ").strip()[:600]
    if not text:
        return ""
    return ("REPLY TARGET- the owner used Discord's reply feature ON THIS message, so it is what "
            "he is responding to. ANCHOR your answer to it, NOT the recent chatter below:\n"
            f"  {who}: \"{text}\"\n\n")

def _react(ch, mid, emoji, tok):
    """PUT a reaction on a message (idempotent). Used for the 👀 on-read receipt so the owner
    sees, the instant he sends, that his message has been read and is being worked."""
    import urllib.parse
    url = (f"https://discord.com/api/v10/channels/{ch}/messages/{mid}"
           f"/reactions/{urllib.parse.quote(emoji)}/@me")
    req = urllib.request.Request(url, method="PUT",
                                 headers={"Authorization": f"Bot {tok}", "User-Agent": UA,
                                          "Content-Length": "0"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log(f"react failed for {mid}: {e}")
        return False

def _load(path, default):
    try:
        return json.loads(open(path, encoding="utf-8-sig").read())
    except Exception:
        return default

def _save(path, obj):
    try:
        json.dump(obj, open(path, "w", encoding="utf-8"))
    except Exception as e:
        log(f"save {os.path.basename(path)} failed: {e}")

UCMD = r"C:\Users\you\Documents\Python Scripts\utils\baxter_usage_cmd.py"

def _fire_usage_cmd():
    """Kick the ORTHOGONAL /usage poller (its own lock, own cursor, no claude, no
    probe) as a detached child BEFORE this fast lane's FLOCK/hard-stop guards- so
    /usage answers in ~1s even while a slow claude quick-reply holds the 240s FLOCK.
    That coupling was v1's ~2-min hang. baxter_fast.py is re-spawned fresh every ~15s,
    so this fires the poller continuously without needing a watcher restart."""
    try:
        subprocess.Popen([sys.executable or "python", UCMD], cwd=VAULT,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log(f"usage-cmd spawn failed: {e}")

def _fast_prompt(body, mid, convo="", pin="", queued="", cid=None):
    """The fast lane's worker prompt. A real function, not an f-string buried in main(),
    so baxter_rules.check() can render it every triage pass and fail loudly the moment it
    stops carrying the shared rules block (the owner, 9th July: "I need a permanent fix").

    `queued` is the ALREADY QUEUED block for a big ask whose placeholder this process has
    just written- the worker's only job there is to state the slot it is given. It is empty
    for an ordinary message, which leaves DEFLECT_RULE's own --queue branch in force.

    `cid` is the channel the message arrived in. It reaches rules.reply_via(), which then names
    the channel on the reply command. Omitted, reply_via drops --channel and baxter_say.main()
    falls back to channel_key='general'- which is how a question the owner asked in
    #deadlock-research was answered in #general. baxter_slash always passed it; this lane never
    did. Never let it default again."""
    return (
        f"You are Baxter, the owner's butler-assistant. His Obsidian vault is {VAULT} "
        f"(00-Inbox + 20-Projects hold tasks as '- [ ] ...' lines with #project tags and "
        f"due dates like \U0001F4C5 2026-07-04; 40-Drafts holds drafts; Subscriptions.md money). "
        f"{pin}"
        f"{rules.convo_block(convo)}"
        f"He just sent this in Discord:\n\n\"{body}\"\n\n"
        f"{queued}"
        f"{rules.bind_rule(pin)}"
        f"YOU ARE THE FAST LANE. Speed over thoroughness. Send ONE short reply, NOW.\n"
        f"{rules.WORKER_RULES}"
        f"{rules.source_mid_rule(mid)}"
        f"{rules.reply_via(mid, cid)}"
        f"{rules.voice()}\n"
        f"{rules.SCOPE_RULE}"
    )


def stand_back(msgs, uid, ch, tok):
    """Stamp the handsoff on every message the owner addressed to Codex or Jem, then claim it.

    The listener stamps these the instant they land; this is the same rule on the poll's side,
    so a message that arrived while the gateway was down still carries a mark. Zero reactions is
    what a dead Baxter looks like, and standing back must never read as that (the owner, 11th July).
    React FIRST, claim second: a failed PUT still claims, so a cosmetic loss can never re-open
    the reply path. Returns the ids claimed."""
    stood = [m for m in _drop_handled(list(reversed(msgs)))
             if str((m.get("author") or {}).get("id")) == str(uid)
             and siblings.message_for_sibling(m, BOT_ID)]
    for m in stood:
        _react(ch, str(m["id"]), "\U0001F91A", tok)
    claimed = _claim_handled([str(m["id"]) for m in stood])
    for mid in claimed:
        log(f"STANDBACK (fast) {mid}- addressed to a sibling, not Baxter")
    return claimed


def main():
    _fire_usage_cmd()   # /usage is orthogonal- fire it regardless of the fast lane's state
    # single flight for the fast lane itself
    if os.path.exists(FLOCK) and time.time() - os.path.getmtime(FLOCK) < 240:
        return
    # 4-tier governor: the fast lane is VITAL, so it RUNS the 80-90 band (answers the owner
    # normally). At the 90%+ HARD FLOOR it stays alive but answers with a zero-cost
    # templated ack (no claude) instead of going silent- computed per-message below.
    floor = _floor_active()
    open(FLOCK, "w").write(str(time.time()))
    try:
        sec = json.load(open(SECRETS, encoding="utf-8-sig"))
        tok = sec["discord_bot_token"]; uid = str(sec.get("discord_only_user_id", ""))
        ch = str((sec.get("discord_channels") or [])[0])
        guild = str(sec.get("discord_guild_id", "111111111111111111"))
        cur = {}
        try: cur = json.load(open(CURSOR, encoding="utf-8-sig"))
        except Exception: pass
        url = f"https://discord.com/api/v10/channels/{ch}/messages?limit=20"
        if cur.get(ch):
            url += f"&after={cur[ch]}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bot {tok}", "User-Agent": UA})
        msgs = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        if not msgs:
            return
        # advance cursor over EVERYTHING seen (bot posts too), respond only to the owner's responsive msgs
        newest = max(int(m["id"]) for m in msgs)
        first_run = ch not in cur
        cur[ch] = str(newest)
        json.dump(cur, open(CURSOR, "w", encoding="utf-8"))
        if first_run:
            return                      # initialise silently - no history replay
        # ---- 👀 WORK-START RECEIPT moved to the reply loop (the owner, 8th July clarification).
        # His semantics: 👀 means 'on it now', NOT 'seen it'. The old react HERE fired on mere
        # poll/sight (for live-session @mentions) before any work- exactly the 'former' behaviour
        # he rejected. Now: the live session reacts on its OWN work-start for @mentions, and this
        # fast lane reacts just before it spawns a quick-reply below (its work-start). Server-wide
        # coverage in every channel is handled at work-start by baxter_slash's real-time listener.
        # ---- SLASH COMMANDS: pure code path, no LLM. /off and /on flip the master pause
        # flag instantly (/usage lives in its own orthogonal poller now). Mark each handled
        # so the main triage/claude path never double-processes it.
        slash_done = set(_handled_ids())
        slash_newly = []
        for m in reversed(msgs):
            if str((m.get("author") or {}).get("id")) != uid:
                continue
            content = (m.get("content") or "").strip()
            if str(m["id"]) in slash_done:
                continue
            try:
                # The flag flips either way- the command WAS applied. Only its confirmation can
                # be denied, and _send_ack records that; we never log a send that never went.
                if OFF_CMD.match(content):
                    open(OFF_FLAG, "w", encoding="utf-8").write(datetime.now().isoformat())
                    if _send_ack(m["id"],
                                 "\U0001F634 Paused, sir- I'll go quiet: no briefs, pings or "
                                 "background work. Say /on to wake me; your questions still reach me."):
                        log(f"/off command -> paused for {m['id']}")
                elif ON_CMD.match(content):
                    try: os.remove(OFF_FLAG)
                    except FileNotFoundError: pass
                    open(CATCHUP_FLAG, "w", encoding="utf-8").write(datetime.now().isoformat())
                    if _send_ack(m["id"],
                                 "✅ Back on, sir- machinery's live again; the CoC bot resumes and "
                                 "I'll catch up on anything that landed while paused."):
                        log(f"/on command -> resumed for {m['id']}")
                else:
                    continue
            except Exception as e:
                log(f"slash command failed for {m['id']}: {e}")
            slash_done.add(str(m["id"])); slash_newly.append(str(m["id"]))
        if slash_newly:
            _claim_handled(slash_newly)
        # ---- STAND BACK from anything the owner addressed to Codex or Jem (11th July). Must precede
        # pre_enqueue and both ack passes below- a message queued or "noted, banked" is a
        # message answered.
        stand_back(msgs, uid, ch, tok)
        # ---- BIG-ASK PRE-ENQUEUE, EVERY BAND. The act before the ack; see pre_enqueue().
        # message_id -> 'position 3 of 27, p5', for the wall ack and the worker's prompt.
        positions = pre_enqueue(msgs, uid, _big_gated())
        # ---- CATCH-ALL AT THE WALL (the owner, 6th July gap-fix): at 80%+ triage is paused, so
        # a plain instruction- not a question/command (RESPONSIVE), not a build-verb big-ask
        # (BIG_ASK), not an @mention/reply (live session)- has NO lane and would sit in
        # silence till reset. Give it a zero-cost 'noted, banked' ack + mark handled (triage
        # still files it later, but won't re-reply). Fires ONLY when routine is gated- below
        # 80% triage handles these live within a minute, so this never fires there (never a
        # shell). Emergencies (VITAL_EXEMPT) are left to fall through- never wrongly 'banked'.
        if _routine_gated():
            catchall = [m for m in _drop_handled(list(reversed(msgs)))
                        if str((m.get("author") or {}).get("id")) == uid
                        and m.get("content")
                        and not _for_live_session(m)
                        and not siblings.message_for_sibling(m, BOT_ID)
                        and not RESPONSIVE.search(m["content"])
                        and not VITAL_EXEMPT.search(m["content"])]
            if catchall:
                ack = _noted_ack()
                newly = []
                for m in catchall[-3:]:
                    try:
                        if not _send_ack(m["id"], ack):
                            continue     # denied + recorded; never claim a message never acked
                        newly.append(str(m["id"]))
                        log(f"WALL catch-all ack (no llm) {m['id']} ({m['content'][:40]!r})")
                    except Exception as e:
                        log(f"catch-all ack failed for {m['id']}: {e}")
                if newly:
                    _claim_handled(newly)
        targets = [m for m in reversed(msgs)
                   if str((m.get("author") or {}).get("id")) == uid
                   and m.get("content")
                   and not siblings.message_for_sibling(m, BOT_ID)   # Codex/Jem's, not ours
                   and (( RESPONSIVE.search(m["content"]) and not _for_live_session(m) )
                        or _stale_for_live(m))]   # rescue @mentions the live session missed
        if not targets:
            return
        targets = _drop_handled(targets)
        # STARVATION FIX (8th July): stale @mention rescues go to the FRONT and are never
        # dropped by the per-sweep cap- they've already waited past LIVE_MISS_SECS, and the
        # cursor advances past them so a missed one never gets a second chance. Fresh
        # responsive replies fill the rest. Cap 5/sweep (was 3) so a floor-window backlog
        # clears in one pass instead of leaving the oldest orphaned.
        _stale = [m for m in targets if _stale_for_live(m)]
        _fresh = [m for m in targets if not _stale_for_live(m)]
        to_process = (_stale + _fresh)[:5]
        # CLAIM upfront - the main triage checks this file, so claiming before the (slow)
        # reply run closes the double-reply race. The claim is a locked TEST-AND-SET: an id the
        # listener took between _drop_handled above and this write comes back un-won, and we
        # drop it here rather than spawning a second worker on a message it is already
        # answering (msg 444444444444444401, 9th July).
        won = set(_claim_handled([str(m["id"]) for m in to_process]))
        skipped = [str(m["id"]) for m in to_process if str(m["id"]) not in won]
        if skipped:
            log(f"claimed elsewhere, not re-picking: {skipped}")
        to_process = [m for m in to_process if str(m["id"]) in won]
        if not to_process:
            return
        # chronology fix (the owner, 4th July): answer as a TURN in the conversation, not a
        # cold read - the cursor fetch above only holds NEW messages, no Baxter replies.
        convo = ""
        try:
            creq = urllib.request.Request(
                f"https://discord.com/api/v10/channels/{ch}/messages?limit=12",
                headers={"Authorization": f"Bot {tok}", "User-Agent": UA})
            recent = json.loads(urllib.request.urlopen(creq, timeout=15).read().decode())
            lines = []
            for r in reversed(recent):
                a = r.get("author") or {}
                who = "Baxter" if a.get("bot") else a.get("username", "?")
                lines.append(f"[{(r.get('timestamp') or '')[11:16]}] {who}: "
                             + (r.get("content") or "").replace("\n", " / ")[:300])
            convo = "\n".join(lines)
        except Exception:
            pass
        # HARD FLOOR (>=90%): answer with the locked zero-cost templated ack, no claude
        # burn- the owner is never met with silence, but the top 10% reserve is protected.
        if floor:
            ack = _floor_ack()
            for m in to_process:
                try:
                    if not _send_ack(m["id"], ack):
                        continue         # denied + recorded, never logged as an ack that landed
                    log(f"HARD FLOOR ack (no llm) to {m['id']}")
                except Exception as e:
                    log(f"floor ack failed for {m['id']}: {e}")
            return
        reacted = dedup.read_ids(REACTED)
        for m in to_process:            # stale rescues first, max 5 per sweep
            body = m["content"][:1500]
            # 👀 WORK-START receipt (the owner, 8th July): we're about to actually work this message-
            # react now, not on sight. Deduped via the shared ledger so the listener + this poll
            # never double-hit the API (add_reaction is idempotent regardless).
            if str(m["id"]) not in reacted and _react(ch, str(m["id"]), "\U0001F440", tok):
                reacted.append(str(m["id"]))
                dedup.update_ids(REACTED, [str(m["id"])], cap=300)   # shared w/ the listener
            # ⚙️ ACTIVELY-WORKING cog: NO LONGER stamped here (the 10th-July phantom-cog
            # audit). A cog stamped before the worker ran was a lie whenever the spawn failed or
            # the worker died before its Claude turn. The reply worker now OWNS its own cog- it
            # stamps ⚙️ when its Claude turn begins and drops it when the turn ends, so a cog is
            # present ONLY while a session is genuinely working. baxter_say still drops it + adds
            # ✅ on the delivered reply, and baxter_reaction_watch reaps any stray cog.
            pin = _reply_parent(m, ch, tok)   # PRIMARY: the message the owner actually replied to
            # A big ask has already had its placeholder written above, so the worker is told
            # the slot rather than asked to create one. A big ask whose write FAILED gets the
            # blunt refusal block instead ("you may not write 'queued'")- never a bare ack.
            # An ordinary message gets no block at all, leaving DEFLECT_RULE's --queue branch
            # in force for genuinely new work it decides to defer.
            qblock = (rules.queued_block(positions.get(str(m["id"]), ""), body)
                      if _is_big_ask(m, uid) else "")
            prompt = _fast_prompt(body, m["id"], convo, pin, qblock, cid=ch)
            # 3-LANE CONCURRENCY (the owner, 8th July): spawn a DETACHED, lane-bounded worker
            # per message instead of a blocking serial claude run- so up to 3 #general
            # quick-replies fan out in parallel rather than queueing behind one another.
            # #general is per-task, so each gets a FRESH session; record it so a later
            # reply to this answer can resume the same thread.
            try:
                sid = str(uuid.uuid4())
                lanes.set_message_session(str(m["id"]), sid)
                pf = os.path.join(lanes.LANES_DIR,
                                  f"prompt-{datetime.now():%H%M%S}-{os.getpid()}-{m['id']}.txt")
                os.makedirs(lanes.LANES_DIR, exist_ok=True)
                with open(pf, "w", encoding="utf-8") as f:
                    f.write(prompt)
                subprocess.Popen([sys.executable or "python", REPLY_WORKER,
                                  "--channel", str(ch), "--mid", str(m["id"]),
                                  "--session", sid, "--mode", "create",
                                  "--prompt-file", pf, "--general"], cwd=VAULT,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                log(f"quick-reply worker spawned for {m['id']} ({body[:40]!r})")
            except Exception as e:
                log(f"quick reply spawn failed: {e}")
    except Exception as e:
        log(f"sweep failed: {e}")
    finally:
        try: os.remove(FLOCK)
        except Exception: pass

if __name__ == "__main__":
    main()
