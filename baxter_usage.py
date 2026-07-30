"""baxter_usage- the METERS + GOVERNOR (built 5th July, plan: 50-Research/Usage
governor and auto-resume - plan of attack.md).

Reads Claude's real usage meters (the same OAuth endpoint the /usage panel uses)
and writes .baxter_usage.json in the vault. Everything downstream- triage gates,
build-time checkpoints, auto-resume- reads that one file; nothing else does its
own accounting. The token is re-read from ~/.claude/.credentials.json on EVERY
poll (Claude Code rotates it itself). A failed poll (401, endpoint down) keeps
the last-known values + records the error- never crashes, and the governor fails
OPEN once meters are >45 min stale. ccusage was tested and DEMOTED (5th July): its
block estimate read 3% when the real meter said 48%- estimates must never drive
stop decisions. It stays installed (`ccusage blocks`) for token telemetry only.

Thresholds carry the owner's 8th-July 15:34 FINAL recode ("what the fuck do you mean 55%?
builds pause at 80%. shrink to bare minimum at 90%. just vitals like the coc bot etc.
fully recode this")- the sliding by-time-left session curve is RETIRED, flat bands only.

Cadence SLOWED 6th July (his order- "poll significantly slower in downtimes, a bit
slower in uptimes; getting rate-limited is EXTREMELY dangerous"). Downtime (idle, no
worker in flight) polls every 20 min (was ~4 min); uptime (a build/resume worker
burning) every 90s (was ~60s). NO build-duration extrapolation- builds are non-linear,
so the only signal is worker-running-now vs not. The `/api/oauth/usage` endpoint is
INTERNAL + UNDOCUMENTED (no published limit, no limit header, Retry-After: 0) and
429s aggressively under sustained polling, so we stay well under the ~30-60s others
found "reasonable"; usage data only changes every few hours, so slow polling loses
nothing. Project-class GATE checks still force a fresh <=60s read on demand (PROBE_FLOOR),
independent of the slow base cadence, and the "held for vitals" figure in alerts is 100
minus the ACTUAL stop percentage- never the nominal 20.

Anti-spam 5th July (his 10:59 strike- ~13 identical 🛑 pings in an hour): the
endpoint jitters resets_at ~1 min poll-to-poll, and the exact-match window check
re-armed the ping bands every flap. Window identity is now jitter-tolerant
(WINDOW_TOL) and every alert key carries a hard resend cooldown (ALERT_COOLDOWN)-
each alert fires ONCE per state change, full stop.

THE BANDS (the owner's 8th-July 15:34 FINAL spec- flat, no curve, on the MAX 20x plan;
the 60% drain band added 9th July):
      <60   -> everything runs, the queue drains
      60-80 -> SOFT STOP: the build lanes DRAIN. No NEW lane is opened; each in-flight
               build finishes normally. Nothing is killed and no stop flag is written-
               it is a pump decision (lane_capacity), not an enforcement band.
      80-90 -> BIG tasks pause into the queue; routine + vital run
      90+   -> BARE MINIMUM: vitals only (CoC bot, emergencies, answering him).
               Vitals NEVER stop- the old 90% everything-stops HARD FLOOR is RETIRED.
  --override / --breach / --breach-step lift the 60% drain as well as the 80% big stop.
  weekly gate (project): 88% on the tightest weekly limit (incl. per-model scoped)
  routine (filing, briefs, drafts): held only past 90% (the vital-only line)

The BIG-TASK QUEUE (5th July, his 12:00 + 14:33 order- "never have multiple large
tasks happening at the same time"): every project-class task lives in
.baxter_task_queue.json, priority-ordered (1 the owner-says-first, 2 interrupted
resumes, 5 default, 8 background). --halt re-queues interrupted work at priority
2, so started work finishes first.

TEN LANES, NOT ONE (8th July 22:09 "run 2 builds at once, halve the drain"; 9th
July, 2 -> 4 -> 6 -> 10). The single slot is now LANE_COUNT=10- lanes 1 to 10, never lane 0-
guarded by a clash DELEGATOR: each task declares a touch_set, the pump refuses to
co-schedule overlapping/adjacent work, and a periodic re-check yields the losing
lane when live touch-sets drift into each other. Lanes only pay if the touch-sets
are FINE-GRAINED: a hub file declared whole serialises every task that names it, so
--queue refuses a bare HUB_FILES path and demands the region (`<file>/<function>`).
The uncontrolled parallel resume fan-out that annihilated a whole window stays dead-
this is bounded, declared and clash-checked. See the LANE section below.

CLI:
  python baxter_usage.py                 -> probe now + one-line status
  python baxter_usage.py --status        -> one-line status (no forced probe)
  python baxter_usage.py --check project -> exit 0 = go, exit 3 = over the curve
  python baxter_usage.py --check project --lane <journal>
                                         -> as above PLUS the delegator's yield check
  python baxter_usage.py --check routine
  python baxter_usage.py --halt "<task>" "<next step>" [--note <path>] [--state "<summary>"] [--touch "a,b,@c"]
                                         -> re-queue interrupted work (priority 2);
                                            triage restarts it when a lane + curve allow
  python baxter_usage.py --queue "<task>" "<first step>" [--note <path>] [--state "<summary>"] [--priority N] (--touch "utils/baxter_usage.py/ceiling,utils/coc_bot/,@probe" | --solo) [--gate owner]
                                         -> add a big task to the queue (default priority 5);
                                            --gate owner parks it until he says go.
                                            A touch-set is MANDATORY: declare --touch, or say
                                            --solo out loud. Omitting both is refused (exit 2)
  python baxter_usage.py --ungate "<task substring>"
                                         -> the owner said go: lift the human gate, the pump may run it
  python baxter_usage.py --queue-list    -> show the queue in run order (+ slot in band, touch-sets, gates)
  python baxter_usage.py --move "<id or substring>" <up|down|top|bottom|N> [--force]
                                         -> reorder one entry WITHIN its priority band (N is a
                                            1-based slot). Refused on a task live in a lane
                                            (never forceable), a p1 pin, or a gated entry.
  python baxter_usage.py --lanes         -> live build lanes, their touch-sets + yields
  python baxter_usage.py --delegator     -> re-check the live lanes for a clash now
  python baxter_usage.py --lane-touch <journal> <path>...
  python baxter_usage.py --writer-touch <path>...   (exit 3 = a live lane owns it)
                                         -> builder self-check: exit 3 = clash, halt yourself
  python baxter_usage.py --selftest-softstop
                                         -> prove the 60% drain band: no new lane opens, the
                                            in-flight ones are never cut, breach lifts it
  python baxter_usage.py --override [min] -> lift the 60% drain + the 80% big stop into the 80-90 band (default 120)
  python baxter_usage.py --breach [min]  -> lift EVERY band incl the 90% vital-only wall (default 60)
  python baxter_usage.py --breach-clear  -> re-seal (normal bands back in force)
  python baxter_usage.py --breach-step   -> STEPPED breach: lift the current limiter, run to the
                                            NEXT tier (session OR weekly, whichever first), then
                                            self-clear; /breach again to step further
  python baxter_usage.py --breach-step-clear -> drop the step-breach marker (normal bands back)

Import surface (baxter_triage uses these): probe(), check(cls), read_meters(),
queue_read(), queue_write(), enqueue().
Pings (band crossings, window-closing warnings, and the reset+resume 'waking up'
ack) fire from probe() via baxter_say- state change or silence, never scheduled.
The resume ack (6th July) fires when a fresh window opens after big tasks were
paused- it fires any hour (quiet hours scrapped 6th July: mandated pings, resume
included, are never night-held).
"""
import contextlib, json, os, subprocess, sys, tempfile, time, urllib.request, uuid
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(r"C:\Users\you\Documents\Baxter")
USAGE = VAULT / ".baxter_usage.json"
LIVE = VAULT / ".baxter_usage_live.json"           # baked /usage reply line- the orthogonal command reads ONLY this
STATUSLINE = VAULT / ".baxter_statusline.json"     # Claude Code's own session JSON, dumped by
                                                   # ~/.claude/statusline-command.sh after each
                                                   # assistant message. Carries rate_limits-
                                                   # server truth at ZERO HTTP cost.
STOP_FLAG = VAULT / ".baxter_stop"                 # HARD stop: its existence gates ALL work

# HARD-ENFORCE bands- the GOVERNOR (the owner's 8th-July 15:34 FINAL recode: "builds pause
# at 80%. shrink to bare minimum at 90%. just vitals like the coc bot etc"). Flat
# numbers, no sliding curve- four states by session %, of which only the last three
# are HARD (they write .baxter_stop and the watchdog kills on them):
#   <70   -> everything runs, the queue drains
#   70-80 -> SOFT STOP: the lanes DRAIN. The pump opens no NEW lane; every in-flight
#            build runs to completion untouched. Nothing is killed, no stop flag is
#            written, `blocked()` says nothing. Lives ONLY in lane_capacity().
#   80-90 -> BIG tasks pause into the queue; routine (small asks) + vital still run
#   90+   -> BARE MINIMUM: vitals only (CoC bot, emergencies, answering the owner).
#            Vitals NEVER stop- the old everything-stops HARD FLOOR is RETIRED.
# --override / --breach / --breach-step each lift the soft stop as well as the 80% one:
# when the owner says "go big" at 75% the pump must actually open a lane, not print a
# confirmation over a silent no-op.
SOFT_STOP_SESSION = 70.0     # >=70% -> lanes drain: no NEW lane opens, in-flight lanes finish.
                             # 60 -> 70 on the owner's 10th-July order ("auto slow feature to occur
                             # at 70"): at 64% the board collapsed to ONE lane with 16 runnable.
                             # RE-APPLIED 10th July after a softstop-exam repair lane reverted it
                             # to 60 to satisfy a STALE exam- the exam + CLAUDE.md are now 70 too,
                             # so nothing drifts it back.
                             # SOFT by construction- it is NOT in SESSION_TIERS (a /breach
                             # would step to it as a rung) and NOT in SESSION_PING_BANDS (it
                             # would machine-gun a band ping at a threshold that stops nothing).
                             # Session-only, deliberately: there is no weekly analogue.
BIG_STOP_SESSION = 80.0      # >=80% -> big tasks stop into the queue
ROUTINE_STOP_SESSION = 90.0  # >=90% -> routine ALSO stops; bare-minimum vital-only lane
FLOOR_SESSION = 200.0        # RETIRED (8th July)- vitals never stop; unreachable so legacy floor plumbing stays dead
# weekly is a genuine EMERGENCY ceiling, not a normal gate (the bands are the SESSION
# spec). Set high so ordinary weekly readings never freeze Baxter- same shape, higher.
BIG_STOP_WEEKLY = 88.0       # >=88% weekly -> big tasks stop until the weekly reset
ROUTINE_STOP_WEEKLY = 93.0   # >=93% weekly -> routine also stops; vital-only
FLOOR_WEEKLY = 200.0         # RETIRED (8th July)- vitals never stop on weekly either
# The tier ladders the STEPPED breach (/breach, 7th July) climbs- the exact hard bands
# above, one dimension each. A /breach lifts the CURRENT active band and runs until the
# NEXT rung UP on either ladder is reached, whichever first, then re-seals (see below).
SESSION_TIERS = (BIG_STOP_SESSION, ROUTINE_STOP_SESSION)   # 80 / 90
WEEKLY_TIERS = (BIG_STOP_WEEKLY, ROUTINE_STOP_WEEKLY)      # 88 / 93
BREACH_STEP = VAULT / ".baxter_breach_step"        # STEPPED breach marker (session + weekly ceiling)
QUIET95 = VAULT / ".baxter_quiet95"                # 95-ONLY window (the owner, 8th July MAX period):
QUIET95_WARN = 95.0                                # while armed, mute the 80/90 band pings and
                                                   # fire a SINGLE session ping- a heads-up at 95%
INTERRUPTED = VAULT / ".baxter_interrupted.json"   # LEGACY inbox- drained into the queue
TASK_QUEUE = VAULT / ".baxter_task_queue.json"     # the big-task queue (one runs at a time)
_LIVE_VAULT = VAULT                                # frozen at import- exams monkeypatch
_LIVE_QUEUE = TASK_QUEUE                            # VAULT/TASK_QUEUE; these stay pinned so
                                                   # _log() can tell which path a fixture moved
PRD_DIR = VAULT / "60-PRDs"                        # the PRD store. A big task is filed with its
                                                   # spec or it is not filed: see MissingPRD.
RESUME_DIR = VAULT / ".baxter_resume"   # resume-worker journals (mtime = heartbeat)
CRED = Path.home() / ".claude" / ".credentials.json"
SAY = r"C:\Users\you\Documents\Python Scripts\utils\baxter_say.py"

# POLL CADENCE (the owner, 6th July: "poll significantly slower in downtimes, a bit slower in
# uptimes; getting rate-limited is EXTREMELY dangerous"). NO build-duration extrapolation-
# builds are non-linear, so the ONLY signal is "is a worker running right now" (uptime) vs
# not (downtime). The KILL check still runs every 5-15s but reads the CACHED .baxter_stop
# (a local file, no network); only the OAuth endpoint hit is on these cadences, and the hard
# floor caps bursts. Measured 6 Jul: the endpoint 429s under sustained polling and exposes no
# limit header (only Retry-After: 0), so we stay deliberately conservative.
# FLAT PERMA-POLL (the owner, 8th July 23:45: "Make it just perma poll every 30 mins. Just every
# 30 mins dont worry anything else"). The uptime/downtime split is RETIRED- one cadence, always.
# BUILD_PROBE_EVERY is kept equal to PROBE_EVERY so every caller stays valid; never re-split them.
#
# THE 30-MINUTE FIGURE IS RETIRED (9th July), and the flat single cadence he asked for is kept.
# 30 min was three times BIG_FRESH, so the meter was stale BY CONSTRUCTION for 20 of every 30
# minutes and big work hard-stopped on a healthy 200-OK meter. The log proves it: the age quoted
# in every stale hard-stop runs 10m, 11m ... 29m and never once 30m+ or under 10m- a sawtooth
# between the gate's trust line and the poll it was waiting on. 559 such stops, 124 of them in an
# hour with no 429 anywhere. Cadence is therefore DERIVED from the trust window, never a magic
# number: a probe must land before the gate stops trusting the last one, with the burst floor's
# worth of slack for a retry. Volume goes 48 -> 160 requests/day, ~14x under the 6th-July flood
# (2,239 real 429s in one day); PROBE_FLOOR still bounds every burst.
PROBE_FLOOR = 60         # HARD burst floor: never hit the OAuth endpoint more than once per this,
                         # even on a FORCED probe. Stacked forced probes are what tripped the 429s.
BIG_FRESH = 600          # THE fail-closed line for big work (the owner, 9th July 00:35: "We are literally at
                         # 60% usage??? Usage governor is faulty. Fix"). A big burn holds when the last
                         # GOOD read is older than 10 min- a read that old can hide a jump over a wall
                         # (the 6th-July false-low: a cached 65% while reality was 75%). It does NOT hold
                         # on the mere PRESENCE of an `error` flag: a 429 blip sitting on top of a
                         # four-minute-old 60% conceals nothing, and gating on the flag froze the entire
                         # build queue for >=PROBE_EVERY per blip, indefinitely under repeated ones.
                         # Staleness is what makes a read dangerous, not the error beside it.
                         # Defined ABOVE the cadence because the cadence is derived from it. Never widen
                         # it to quiet a flap- that restores the 6th-July false-low burn.
PROBE_EVERY = BIG_FRESH - PROBE_FLOOR   # 540s. One clean read always lands inside the trust window,
BUILD_PROBE_EVERY = PROBE_EVERY         # with one PROBE_FLOOR retry's slack. Never re-split the two.

# IMPORT-TIME INVARIANT (9th July). A wrong cadence is silent: it freezes the build queue at 3am
# and looks like a healthy meter. It must fail LOUDLY here instead, the moment anyone edits a
# number above. The three that matter, and what each one costs if it slips:
#   BIG_FRESH <= 600     widen it and a stale false-low walks a big burn through the wall (6th July)
#   PROBE_FLOOR >= 60    lower it and the forced-probe bursts re-open the 429 flood (6th July)
#   PROBE_EVERY + PROBE_FLOOR <= BIG_FRESH   raise it and the gate distrusts a meter nothing refreshes
if BIG_FRESH > 600:
    raise AssertionError(f"BIG_FRESH={BIG_FRESH} > 600: a stale meter can hide a jump over a wall")
if PROBE_FLOOR < 60:
    raise AssertionError(f"PROBE_FLOOR={PROBE_FLOOR} < 60: the burst floor that ended the 429 flood")
for _n, _v in (("PROBE_EVERY", PROBE_EVERY), ("BUILD_PROBE_EVERY", BUILD_PROBE_EVERY)):
    if _v + PROBE_FLOOR > BIG_FRESH:
        raise AssertionError(
            f"{_n}={_v} + PROBE_FLOOR={PROBE_FLOOR} > BIG_FRESH={BIG_FRESH}: the meter would go stale "
            f"between polls and hard-stop big work on a healthy endpoint")
del _n, _v
BUILD_FRESH = 180        # resume journal heartbeated within this = worker in flight (= uptime)
RETRY_EVERY = 120        # seconds between attempts after a normal failed probe
RL_BACKOFF = 300         # after a 429 (rate-limited): hold 5 min before retrying- a hard cool-off
STALE_AFTER = 2700       # meters older than 45 min -> fail OPEN (never freeze the machine on a dead probe)
STALE_ERR_ALERT = 600    # a probe error that PERSISTS this long (last good read older than 10 min) ->
                         # ping the owner once (a real rate-limit/outage is freezing the %, not a transient blip)
STATUSLINE_SCALE_TOL = 15.0  # a push and an OAuth read taken seconds apart must agree within this many
                             # points or the dump is not what we think it is. The docs say
                             # used_percentage runs 0-100; if it were ever 0-1, an adopted 0.73 would
                             # read as 0.73% while the truth was 73%- a permanent false-low that walks
                             # every build through the wall. Never trusted on the strength of the docs
                             # alone: probe() corroborates once against the endpoint before adopting.
STATUSLINE_PAIR_MAX = 120    # ...and the two readings must be this close in time to be comparable,
                             # else the gap is real usage drift rather than a scale error.
STATUSLINE_DROP_TOL = 5.0    # session usage is MONOTONIC inside a 5h window- it only climbs until the
                             # window resets. A push claiming materially LESS than the stored reading of
                             # the SAME window is therefore impossible, and says the dump is not what we
                             # think it is (a scale switch, a truncated write, another account). Refuse
                             # it: probe() then falls through to HTTP, which re-corroborates and latches
                             # the zero-request path shut. Without this the corroboration could never
                             # RE-ARM, because adoption sits above the very request that would recheck
                             # it. The tolerance absorbs server rounding (<1 point); a scale error
                             # shows up as ~70.

# gate thresholds- aligned to the flat 8th-July bands
WEEKLY_PROJECT_GATE = BIG_STOP_WEEKLY   # weekly big-stop (88)
CRITICAL = ROUTINE_STOP_SESSION  # routine holds past this (90- the vital-only line)
SESSION_PING_BANDS = (80.0, 90.0)          # big-pause / vital-only crossings
WEEKLY_PING_BANDS = (80.0, 88.0)
CLOSE_WARN_MIN = 45      # session closing-warning: <=45 min to reset...
CLOSE_WARN_SPARE = 25.0  # ...with >=25% of the window unspent (his 5th-July 07:07 spec)
WINDOW_TOL = 900         # resets_at within 15 min = SAME window (the OAuth endpoint
                         # jitters the timestamp ~1 min poll-to-poll; exact-match
                         # comparison re-armed the bands every poll and machine-gunned
                         # the same 🛑 ping- his 5th-July 10:59 "100 of these" strike)
RESET_CATCHUP = 1800     # RESET-ANCHORED WAKE window (the owner, 8th July 21:34: "auto wake at
                         # 9:30... why 9:34"). Once the stored session_resets_at has passed
                         # but no fresh meter has landed for the new window, force a probe
                         # NOW (bypassing the 20-min downtime cadence) rather than noticing
                         # the reset up to PROBE_EVERY late. Only within this window past the
                         # reset- past it (machine was asleep, endpoint down) fall back to the
                         # normal cadence + 429 backoff instead of force-looping forever.
ALERT_COOLDOWN = 2700    # hard backstop: the same alert key never refires within 45 min,
                         # whatever the band state thinks- spam is a bug, silence isn't
OUTAGE_NOTICE = 2700     # a gap between CLEAN reads past this = Baxter was blind (crash, sleep,
                         # dead endpoint) and says so on the first probe back. 45 min, PINNED
                         # ABSOLUTE (9th July). It read `PROBE_EVERY + 900`, so tightening the
                         # cadence to 540 would have silently dragged this OUTWARD Discord alert
                         # from 2700s to 1440s and made it ~1.9x more sensitive- Baxter reporting
                         # outages that are not outages. What counts as an outage is a fact about
                         # the owner's machine, not about how often we poll it. the owner sketched "~20 min"
                         # when the cadence was 20; 45 min is the first gap that cannot be
                         # explained by any cadence we run plus jitter.

# The percentage-free half of a band ping. A 95-only quiet window may mute the NUMBER
# ("usage 82%"- a nag he asked to silence); it may never mute THIS- Baxter reporting that
# it has stopped or resumed doing work (the owner, 9th July 07:08: "I know we hit 80% usage.
# But you never pinged that over"). Keyed by session band: 1 = big-stop, 2 = vital-only.
SESSION_STATE_LINES = {
    1: "⏸️ Builds and big tasks paused- routine + vitals still run. Resets {t}.",
    2: "🛑 Bare minimum- vitals only (CoC + answering you); big tasks paused. Resets {t}.",
}


def _log(msg):
    # .baxter.log follows the path a fixture REPOINTED, so a scratch run never forges the live
    # vault log and a healthy scratch run is never read back as silent. Two sealed exams pin the
    # two shapes: baxter_loggag_exam repoints VAULT only (log must follow VAULT), baxter_logredir_exam
    # repoints TASK_QUEUE only (log must land beside that scratch queue, never the vault log). VAULT
    # and TASK_QUEUE are both monkeypatched by fixtures; _LIVE_VAULT/_LIVE_QUEUE stay pinned to the
    # real paths so we can tell which one moved. A moved VAULT wins (its own .baxter.log); else a
    # moved queue redirects beside itself; else the live vault log. Never dropped, never forged.
    # Do NOT collapse this to one key- either exam reddens. See [[log-gag-fakes-a-silent-probe]].
    if VAULT == _LIVE_VAULT and TASK_QUEUE != _LIVE_QUEUE:
        try:
            target = Path(str(TASK_QUEUE)).with_name(".baxter.log")
        except Exception:
            return
    else:
        target = VAULT / ".baxter.log"
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] usage: {msg}\n")
    except Exception:
        pass


def _in_quiet():
    """Quiet hours SCRAPPED (the owner, 6th July 22:06: "scrap the quiet hours rule- I want pings
    when they are mandated"). Always False now- hour-of-day suppresses NOTHING; every mandated
    ping and the resume ack fire whenever due, day or night. The band-crossing gate +
    ALERT_COOLDOWN already prevent spam, so a mandated ping is never noise. (Kept as a function
    so callers need no edits; flip the return if he ever reinstates quiet hours.)"""
    return False


def _say(msg):
    """One-line governor ping to #general (mention = phone push). No night gate-
    mandated pings fire any hour (quiet hours scrapped, the owner 6th July); _in_quiet()
    is always False now but kept as the single flip-point if he ever reinstates it.

    Returns True only if it actually left. Exit 3 is a DENIAL: baxter_say refused the claim,
    printed why, and sent nothing- record the reason in the voiceless denial sink and return
    False, never a success ([[outward-path-proven-from-the-send-ledger]])."""
    if _in_quiet():
        return False
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(["python", SAY, msg], capture_output=True, timeout=30, env=env)
    except Exception as e:
        _log(f"ping failed: {e}")
        return False
    if getattr(p, "returncode", 0) == 3:
        try:
            try:
                from utils import baxter_autobuild as autobuild
            except ImportError:
                try:
                    import baxter_autobuild as autobuild
                except ImportError:    # path-loaded: no utils anywhere on sys.path
                    autobuild = _sibling("baxter_autobuild")
            row = autobuild.denial_alert("baxter_usage._say", str(msg)[:80], p.stderr)
            _log(f"DENIED: governor ping refused- {row['reason']}")
        except Exception:
            pass
        return False
    return True


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _write_atomic(path, data):
    # Coerced, exactly as _read_json coerces (9th July). A str path used to die here on
    # `.parent`- and probe()'s FAILURE path swallows that write in `except Exception: pass`,
    # so `last_attempt` never advanced. Both PROBE_FLOOR and the 429 cool-off measure age
    # from `last_attempt`: a write that fails silently disarms them together and probe()
    # hammers a rate-limited endpoint on every call. The reader coerced and the writer did
    # not, so the meter read back fine while never being written.
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".usage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        try: os.remove(tmp)
        except Exception: pass
        raise


# The response headers worth keeping. A blanket dict() would park cookies and CDN ids in
# the meter file forever; these are the only ones that say anything about the limit.
_HTTP_KEEP = ("retry-after", "x-should-retry", "request-id", "anthropic-request-id")


def _http_snap(status, headers):
    """The observed HTTP reality of one probe- status, any anthropic-ratelimit-* header,
    retry-after. _fetch() used to read the body and drop the response object on the floor,
    so across 2,646 logged 429s (5th-9th July) Baxter never once recorded what the endpoint
    said its own limit was. Recording is all this does: `retry_after` is EVIDENCE, not policy.
    The endpoint has been observed answering `Retry-After: 0`, and honouring that would turn
    a rate-limit into a hammer- RL_BACKOFF stays the sole authority on when we retry."""
    keep = {}
    try:
        for k, v in dict(headers or {}).items():
            lk = str(k).lower()
            if lk.startswith("anthropic-ratelimit-") or lk in _HTTP_KEEP:
                keep[lk] = str(v)
    except Exception:
        pass
    retry_after = None
    if keep.get("retry-after") is not None:
        try:
            retry_after = int(float(keep["retry-after"]))
        except Exception:
            retry_after = None      # an HTTP-date form; we don't act on it either way
    return {"status": status, "retry_after": retry_after, "headers": keep,
            "at": datetime.now().isoformat(timespec="seconds")}


def _fetch():
    """Hit the OAuth usage endpoint Claude Code's own /usage panel reads."""
    tok = (_read_json(CRED) or {}).get("claudeAiOauth", {}).get("accessToken")
    if not tok:
        raise RuntimeError("no OAuth token in ~/.claude/.credentials.json")
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "baxter-usage-probe/1.0",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        http = _http_snap(getattr(r, "status", None) or r.getcode(), r.headers)
        d = json.loads(r.read().decode("utf-8"))
    session = d.get("five_hour") or {}
    weekly = d.get("seven_day") or {}
    # per-model scoped weekly limits can be tighter than the overall meter- gate on the max.
    # Fable is DISREGARDED (the owner's 5th-July order- he runs on Opus now, the Fable scoped
    # meter is phantom and must never gate a build).
    def _is_fable(l):
        return (((l.get("scope") or {}).get("model") or {}).get("display_name") or "").lower() == "fable"
    scoped = [float(l.get("percent") or 0) for l in (d.get("limits") or [])
              if l.get("group") == "weekly" and not _is_fable(l)]
    weekly_pct = float(weekly.get("utilization") or 0)
    return {
        "session_pct": float(session.get("utilization") or 0),
        "session_resets_at": session.get("resets_at") or "",
        "weekly_pct": weekly_pct,
        "weekly_resets_at": weekly.get("resets_at") or "",
        "weekly_gate_pct": max([weekly_pct] + scoped),
        "source": "oauth",
        "error": None,
        "_http": http,
    }


def _statusline_snap(old=None):
    """The zero-request read: Claude Code's own `rate_limits` push, as a snapshot shaped
    exactly like _fetch()'s. Returns None when there is nothing trustworthy to adopt.

    `rate_limits` "appears only for Claude.ai subscribers (Pro/Max) after the first API
    response in the session"- so a fresh session, a re-auth or a token blip yields a dump
    with NO rate_limits key. That must read as None (fall through to HTTP), NEVER as 0.0:
    a `.get(...) or 0` there is a false-low that walks a build straight through the wall.
    Both percentages and both reset stamps must be present and numeric, or there is no snap.

    `updated` is the PUSH'S OWN timestamp (the dump's mtime), never datetime.now(). Stamping
    a 9-minute-old push as 'now' would buy it another BIG_FRESH of trust, letting a ~19-minute
    -stale number gate a big burn while every freshness assertion passed. Naive-local, to match
    _age_of/_meter_age. resets_at is epoch seconds here; it is rendered tz-AWARE, matching the
    OAuth path's ISO- a naive one makes _same_window's compare raise, which returns False, which
    silently re-arms every band on every poll (the 5th-July ping storm, in a new costume).

    WEEKLY GATE FLOOR (`old`, the last stored snapshot). OAuth's weekly_gate_pct is
    max(weekly_pct, per-model scoped weekly limits); the push exposes only the overall
    seven_day figure, so adopting it wholesale can LOWER the weekly gate beneath a tighter
    scoped limit and walk a build through the weekly wall. Weekly usage is monotonic within
    a window, so the stored gate is carried forward as a floor while the window holds. A new
    weekly window drops the floor- it belongs to the window that set it."""
    try:
        mtime = STATUSLINE.stat().st_mtime
        d = json.loads(STATUSLINE.read_text(encoding="utf-8"))
    except Exception:
        return None
    rl = d.get("rate_limits")
    if not isinstance(rl, dict):
        return None

    def _num(window, key):
        v = (rl.get(window) or {}).get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v)

    session_pct, weekly_pct = _num("five_hour", "used_percentage"), _num("seven_day", "used_percentage")
    if session_pct is None or weekly_pct is None:
        return None
    if not (0.0 <= session_pct <= 100.0 and 0.0 <= weekly_pct <= 100.0):
        return None     # not a percentage; refuse rather than guess at the scale

    def _iso(window):
        ts = _num(window, "resets_at")
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(ts, timezone.utc).isoformat()
        except Exception:
            return None

    session_resets_at, weekly_resets_at = _iso("five_hour"), _iso("seven_day")
    if not session_resets_at or not weekly_resets_at:
        return None

    # MONOTONICITY, the guard's own re-arm. Inside one session window usage only climbs, so a
    # push reporting materially less than the stored reading of that same window is not a lower
    # number- it is a different number, from a dump we no longer understand. Refusing it drops
    # probe() onto the HTTP path, which re-corroborates the scale and shuts the zero-request
    # path. Skipped across a window boundary, where a fall to near-zero is exactly right.
    if old and _same_window(old.get("session_resets_at"), session_resets_at):
        try:
            if float(old.get("session_pct")) - session_pct > STATUSLINE_DROP_TOL:
                return None
        except (TypeError, ValueError):
            pass

    gate = weekly_pct
    if old and _same_window(old.get("weekly_resets_at"), weekly_resets_at):
        try:
            gate = max(gate, float(old.get("weekly_gate_pct") or 0))
        except Exception:
            pass
    return {
        "session_pct": session_pct,
        "session_resets_at": session_resets_at,
        "weekly_pct": weekly_pct,
        "weekly_resets_at": weekly_resets_at,
        "weekly_gate_pct": gate,
        "source": "statusline",
        "updated": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
    }


def _newer(a, b):
    """True when ISO stamp `a` is strictly later than `b`. An unreadable/absent `b` means
    there is nothing to be newer than, so `a` wins- never freeze on a corrupt stamp."""
    try:
        return datetime.fromisoformat(a) > datetime.fromisoformat(b)
    except Exception:
        return True


def _build_active():
    """True while any build/resume worker is in flight. Resume workers heartbeat
    their .baxter_resume journal's mtime every minute, so a journal touched in the
    last few minutes means a build is burning- the probe drops to the fast cadence."""
    try:
        now = datetime.now().timestamp()
        return any(now - rf.stat().st_mtime < BUILD_FRESH
                   for rf in RESUME_DIR.glob("*.json")
                   if not rf.name.endswith(".failed.json"))
    except Exception:
        return False


def _hours_left(resets_at):
    try:
        t = datetime.fromisoformat(resets_at)
        return max(0.0, (t - datetime.now(timezone.utc)).total_seconds() / 3600.0)
    except Exception:
        return None


def _fmt_left(hours):
    secs = int(hours * 3600)
    return f"{secs // 3600}:{(secs % 3600) // 60:02d}"


def ceiling(hours_left):
    """Big-task stop threshold- FLAT 80 (the owner, 8th July 15:34: "builds pause at 80%.
    fully recode this"). The sliding by-time-left curve (55/70/80) is RETIRED- it
    announced 'builds pause again at 55%' after a reset and he killed it on the spot.
    Signature kept (hours_left ignored) so every caller stays valid."""
    return BIG_STOP_SESSION


def _age_of(iso):
    """Seconds since a naive-local ISO stamp; 0 when it can't be read (never fake an outage)."""
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    except Exception:
        return 0.0


def _fmt_gap(secs):
    """'5h 4m' / '47m'- how long Baxter was blind, for the outage notice."""
    m = int(secs // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def _fmt_clock(iso):
    """UK-local '11:50am' style for alert lines (the owner's locked format, 5th July 7:17am)."""
    try:
        t = datetime.fromisoformat(iso).astimezone()
        return t.strftime("%I:%M%p").lower().lstrip("0")
    except Exception:
        return "?"


def _same_window(marker, resets_at):
    """True when two resets_at stamps are the same usage window. The endpoint
    jitters the timestamp poll-to-poll (11:49 vs 11:50), so exact equality is
    WRONG- it re-armed the bands every poll (the 5th-July spam strike). A real
    new window moves the reset by hours, so a 15-min tolerance separates the two."""
    try:
        a = datetime.fromisoformat(marker)
        b = datetime.fromisoformat(resets_at)
        return abs((a - b).total_seconds()) <= WINDOW_TOL
    except Exception:
        return False


def _reset_due(old):
    """True when a stored window's reset time has just passed but no fresh meter
    reflecting the NEW window has landed yet- the '9:34 not 9:30' the owner flagged
    (8th July 21:34). Anchored to the stored resets_at stamps (never a hardcoded 9:29
    clock); self-clears the instant a fresh fetch moves them into the future, since the
    new resets_at is then ahead of now and `passed` goes negative. Bounded by
    RESET_CATCHUP so a long sleep / dead endpoint doesn't wake every 60s forever.

    BOTH windows are anchors, not just the session one (9th July). The weekly meter
    reopens the 88% big-stop exactly as the session meter reopens the 80% one, and it
    turns over on its own 7-day clock- so a session-only anchor sleeps through the
    weekly reopen and the queue stays frozen for a whole PROBE_EVERY after the wall
    has actually lifted.

    probe()'s PROBE_FLOOR (60s burst floor) and the 429 backoff still apply on top of
    this, so it can't hammer the endpoint- it only lifts the slow downtime cadence."""
    now = datetime.now(timezone.utc)
    for key in ("session_resets_at", "weekly_resets_at"):
        try:
            reset = datetime.fromisoformat(old.get(key, ""))
        except Exception:
            continue
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        if 0 <= (now - reset).total_seconds() <= RESET_CATCHUP:
            return True
    return False


def _fire_alert(ping, key, msg):
    """Cooldown-guarded one-line alert. Sends msg unless this key fired within
    ALERT_COOLDOWN, then stamps ping['sent'][key]. Module-level so both _pings
    (band/close/resume) and probe()'s error path share one anti-spam backstop-
    spam is a bug, silence isn't (the owner's 5th-July strike)."""
    last = (ping.get("sent") or {}).get(key)
    try:
        if last and (datetime.now() - datetime.fromisoformat(last)).total_seconds() < ALERT_COOLDOWN:
            _log(f"cooldown held {key}: {msg}")
            return
    except Exception:
        pass
    _say(msg)
    ping.setdefault("sent", {})[key] = datetime.now().isoformat(timespec="seconds")


def window_close_alert(ping, snap, pct, tstr, fire):
    """Window-closing notices (his 5th-July spec)- the window is nearly over and
    capacity is going unspent. The line INFORMS; it gates on nothing. Under 80%
    the governor pauses nothing, so there is no word he can say that unlocks work
    already running- the old go-and-burn offer was a phantom, and
    the sliding curve that once justified it was retired on 8th July. Above it, the
    80-90 band is lifted by `--override`, not by a word in chat.
    Mutates `ping` in place: the close_warned_* window markers, plus whatever
    `fire` (the caller's cooldown-stamping _fire) writes into ping['sent']."""
    hl = _hours_left(snap["session_resets_at"])
    ceil = ceiling(hl)
    if hl is not None and hl * 60 <= CLOSE_WARN_MIN and (100 - pct) >= CLOSE_WARN_SPARE \
            and not _same_window(ping.get("close_warned_session"), snap["session_resets_at"]):
        ping["close_warned_session"] = snap["session_resets_at"]
        fire("close_session", f"💡 Window closes {tstr}, {100 - pct:.0f}% spare unspent. "
             f"Big tasks pause at {ceil:.0f}%.")
    whl = _hours_left(snap["weekly_resets_at"])
    if whl is not None and whl <= 24 and (100 - snap["weekly_pct"]) >= CLOSE_WARN_SPARE \
            and not _same_window(ping.get("close_warned_weekly"), snap["weekly_resets_at"]):
        ping["close_warned_weekly"] = snap["weekly_resets_at"]
        fire("close_weekly", f"💡 Weekly meter resets {_fmt_clock(snap['weekly_resets_at'])}, "
             f"{100 - snap['weekly_pct']:.0f}% spare unspent. "
             f"Builds pause at {WEEKLY_PROJECT_GATE:.0f}%.")


def _pings(snap, prev_ping, prev_updated=None):
    """Band-crossing + window-closing alerts. Fires only on upward state change.
    Format LOCKED by the owner (5th July 7:17am): ONE line- emoji + number + reset time
    + action. Times UK-local '11:50am' style. Never multi-line.
    ANTI-SPAM (his 5th-July 10:59 strike, ~13 identical pings in an hour): window
    identity uses _same_window (jitter-tolerant), and _fire refuses to resend the
    same alert key within ALERT_COOLDOWN whatever the band state says."""
    ping = dict(prev_ping or {})
    # The state Baxter was last KNOWN to be in (0 clear / 1 big-stop / 2 vital-only). Distinct
    # from ping['session_band'], which the window-reset block below zeroes silently- a state
    # line has to survive a window boundary to be able to report the recovery. Falls back to
    # session_band once, for meters written before this key existed.
    try:
        prev_state = int(ping.get("session_state", ping.get("session_band", 0)) or 0)
    except Exception:
        prev_state = 0
    pct = snap["session_pct"]; wk = snap["weekly_gate_pct"]
    hl = _hours_left(snap["session_resets_at"])
    ceil = ceiling(hl)
    tstr = _fmt_clock(snap["session_resets_at"])

    def _fire(key, msg):
        _fire_alert(ping, key, msg)

    # _pings only runs after a CLEAN read (probe success), so any error episode is OVER.
    # This is the RECOVERY EDGE: if the episode ever pinged him (meter_error_open, set in
    # probe's error path), close the loop with one line saying the meter is back and big
    # work is moving- an outage he was told about must have a visible end. Then re-arm both
    # keys so the NEXT episode pings immediately rather than waiting out ALERT_COOLDOWN
    # (mirrors the session_resume re-arm below). The edge latch is the dedupe; the cooldown
    # is only a backstop, so re-arming can't reopen the spam.
    said_recovery = bool(ping.pop("meter_error_open", False))
    if said_recovery:
        ping.get("sent", {}).pop("meter_error", None)
        _fire_alert(ping, "meter_recovered",
                    f"✅ Usage meter back- session {pct:.0f}%, resets {tstr}. Big tasks running again.")
        ping.get("sent", {}).pop("meter_recovered", None)
    else:
        ping.get("sent", {}).pop("meter_error", None)

    # OUTAGE NOTICE, the other recovery edge (9th July). The 03:56 bugcheck left a five-hour
    # hole and NOTHING announced it- the fleet came back at 09:14 and the 09:00 brief went out
    # as though the night had been ordinary. `prev_updated` is the last CLEAN read before this
    # one; a gap past OUTAGE_NOTICE means Baxter was blind, and the first probe back says so
    # before any brief goes out. Latched on the hole's own timestamp so it speaks exactly once.
    # Skipped when the meter-error episode above already announced its own end.
    if prev_updated and not said_recovery:
        gap = _age_of(prev_updated)
        if gap >= OUTAGE_NOTICE and ping.get("outage_announced") != prev_updated:
            ping["outage_announced"] = prev_updated
            _say(f"🕳️ Baxter was blind {_fmt_gap(gap)}- no usage probe since {_fmt_clock(prev_updated)}. "
                 f"Back now, session {pct:.0f}%; catching up.")

    # new window -> bands reset silently before any crossing check. If the PRIOR
    # window had paused big tasks (band>=1), the reset means held work resumes-
    # arm a one-line 'waking up' ack (the owner, 6th July: expected a reset+resume ping
    # on wake and got none- the governor only ever pinged on usage CLIMBING).
    if not _same_window(ping.get("session_marker"), snap["session_resets_at"]):
        if int(ping.get("session_band", 0)) >= 1:
            ping["resume_pending"] = True
        ping["session_band"] = 0
        ping.get("sent", {}).pop("session_resume", None)   # re-arm for the new window
    ping["session_marker"] = snap["session_resets_at"]
    # SELF-HEALING band pings (8th-July fix for the missed 90% floor). Fire on band
    # MEMBERSHIP keyed to the current window, NOT the strict upward edge. The old
    # `band > prev_band` fired ONCE- if that single poll was missed or clobbered by a
    # concurrent writer, the ping was lost for the whole window (exactly why the 90%
    # lockdown never reached the owner). Now: if pct sits in a band and that band hasn't been
    # announced THIS window, it fires- a lost poll simply re-fires on the next one. Only
    # the highest crossed band speaks; lower ones are stamped silently. Each stamp stores
    # the WINDOW marker, so a new window auto-re-arms every band (and probe()'s file lock
    # stops two racing writers double-firing).
    #
    # TWO MESSAGES, ONE LINE (the 9th-July bug). A live 95-only quiet window made the whole
    # band ping fall through to `pass`- and then stamped the band as announced anyway, so it
    # never re-fired. the owner crossed 80%, big work stopped, and nothing ever told him.
    #   * the PERCENTAGE ("usage 82%") is a nag- the quiet window may mute it.
    #   * the STATE ("big tasks paused" / "running again") is Baxter saying it has stopped or
    #     resumed WORK. That is never silenceable, by any mute.
    # Exactly ONE of them speaks per event: unmuted, the percentage line already carries the
    # state; muted, the state line speaks alone, stripped of the number he asked not to see.
    band = sum(1 for b in SESSION_PING_BANDS if pct >= b)   # 0=clear 1=big-stop 2=vital-only
    win = snap["session_resets_at"]
    crossed = [b for b in SESSION_PING_BANDS if pct >= b]
    sent = ping.setdefault("sent", {})
    muted = _quiet95_active() is not None
    # An override lifts exactly the 80->90 band; a breach lifts every band. A 'paused' line of
    # either kind would be a LIE while one is live, so it is suppressed, not just relabelled.
    lifted = (_breach_active() is not None) or (band == 1 and _override_active() is not None)
    if crossed:
        top = max(crossed)
        for b in crossed:                       # stamp lower crossed bands silently- no back-fire
            if b != top:
                sent.setdefault(f"sband_{int(b)}", win)
        topkey = f"sband_{int(top)}"
        unannounced = sent.get(topkey) != win   # this window hasn't heard it (or a poll was lost)
        if (unannounced or band > prev_state) and not lifted:
            if muted:
                _say(SESSION_STATE_LINES[band].format(t=tstr))
            elif band >= 2:
                _say(f"🛑 Usage {pct:.0f}%- bare minimum, VITALS ONLY (CoC + answering you); everything else paused. Resets {tstr}.")
            else:
                _say(f"⚠️ Usage {pct:.0f}%- builds/big tasks paused; routine + vitals still run. Resets {tstr}.")
            sent[topkey] = win
        # Deliberately NOT stamped while `lifted`: the moment the override or breach lapses the
        # band is real again and must speak. Stamping there is the original bug in another costume.
    elif prev_state >= 1:
        # RECOVERY, in-window or across a reset. "I have started working again" is the other
        # half of "I stopped", so it fires whatever the mute says- only the number drops out.
        # Cooldown-guarded rather than window-keyed, so a percentage oscillating around 80
        # can't machine-gun him with paused/running/paused.
        _fire_alert(ping, "session_running_again",
                    f"✅ Back under the band- big tasks running again. Resets {tstr}." if muted else
                    f"✅ Usage {pct:.0f}%- back under the band, big tasks running again. Resets {tstr}.")
    ping["session_band"] = band
    ping["session_state"] = band    # survives the window reset above- the state line's latch

    # 95-ONLY window warning (the owner, 8th July MAX period): while the toggle is armed the
    # 80/90 pings above are muted and THIS is the sole session usage ping- one heads-up
    # at 95%. Window-keyed like the bands (fires once per window, re-fires if a poll is
    # lost, re-arms on reset). Fired independently of SESSION_PING_BANDS so 95 works even
    # though it isn't a hard band.
    if _quiet95_active() is not None and pct >= QUIET95_WARN:
        sent95 = ping.setdefault("sent", {})
        if sent95.get("sband_95") != win:
            _say(f"⚠️ Usage {pct:.0f}%- nearing the ceiling, sir. Resets {tstr}.")
            sent95["sband_95"] = win

    if not _same_window(ping.get("weekly_marker"), snap["weekly_resets_at"]):
        ping["weekly_band"] = 0
    ping["weekly_marker"] = snap["weekly_resets_at"]
    wband = sum(1 for b in WEEKLY_PING_BANDS if wk >= b)
    if wband > int(ping.get("weekly_band", 0)):
        day = ""
        try:
            day = datetime.fromisoformat(snap["weekly_resets_at"]).astimezone().strftime("%A")
        except Exception:
            pass
        if wband >= 2:
            _fire("weekly_stop", f"🛑 Weekly usage {wk:.0f}%- builds paused, {100 - wk:.0f}% held for vitals. Resets {day}.")
        else:
            _fire("weekly_warn", f"⚠️ Weekly usage {wk:.0f}%- resets {day}. Builds pause at {WEEKLY_PROJECT_GATE:.0f}.")
    ping["weekly_band"] = max(wband, int(ping.get("weekly_band", 0)))

    # closing warnings (his 5th-July spec)- window nearly over with capacity unspent
    window_close_alert(ping, snap, pct, tstr, _fire)

    # waking-up ack: a mandated ping- fires any hour now quiet hours are scrapped
    # (the owner, 6th July: he wants mandated pings, resume included, whenever they're due).
    # The flag survives every poll until it's delivered. (_in_quiet() is the flip-point
    # if he ever reinstates a night hold- always False today.)
    if ping.get("resume_pending") and not _in_quiet():
        n = len(_read_json(TASK_QUEUE) or []) + len(_read_json(INTERRUPTED) or [])
        if n:
            _fire("session_resume", f"☀️ Usage reset, back up- {n} queued task{'' if n == 1 else 's'} resuming. Builds pause again at {ceil:.0f}%.")
        else:
            _fire("session_resume", "☀️ Usage reset, back up- draining the queue.")
        ping.pop("resume_pending", None)
    return ping


USAGE_LOCK = VAULT / ".baxter_usage.lock"


def _lock_acquire(retries=20, delay=0.1, path=None):
    """Cross-process lock around probe()'s read-modify-write so the many concurrent
    callers (watcher --enforce every 5s, fast lane, /usage, triage) can't clobber each
    other's _ping- the lost-update race that silently dropped the 90% floor ping (8th
    July). Best-effort: steals a >30s-stale lock (a crashed holder) and, if it still
    can't get it, proceeds anyway- a slow gate must NEVER hang.

    `path` generalises it to any lockfile (the queue uses QUEUE_LOCK, 9th July)."""
    path = str(path or USAGE_LOCK)
    for _ in range(retries):
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 30:
                    os.unlink(path); continue
            except Exception:
                pass
            time.sleep(delay)
        except Exception:
            return None
    return None


def _lock_release(fd, path=None):
    if fd is None:
        return
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.unlink(str(path or USAGE_LOCK))
    except Exception:
        pass


def probe(force=False):
    """Refresh .baxter_usage.json (rate-limited) and fire any due pings.
    Returns the current snapshot (last-known values on fetch failure)."""
    old = _read_json(USAGE) or {}
    now = datetime.now()
    try:
        upd = datetime.fromisoformat(old.get("updated", ""))
        age = (now - upd).total_seconds()
    except Exception:
        age = 1e9
    try:
        att = datetime.fromisoformat(old.get("last_attempt", ""))
        att_age = (now - att).total_seconds()
    except Exception:
        att_age = 1e9
    # ZERO-REQUEST SERVER TRUTH, consulted FIRST (9th July). Claude Code pushes the same two
    # windows /api/oauth/usage returns into the statusline dump after each assistant message.
    # This is checked ABOVE the burst floor, the cadence and the 429 backoff because every one
    # of those guards exists to protect the HTTP endpoint- and this path makes no request. It
    # is a push, not an estimate: the 5th-July law bars a locally-guessed percentage from
    # setting the band, and this is the server's own figure, so it is admissible. Adopted only
    # while FRESHER than BIG_FRESH- the same fail-closed line big work is gated on- because a
    # stale true number is exactly as dangerous as a false one (the 6th-July false-low).
    #
    # STRICTLY NEWER than what is stored, or we do nothing at all. The watcher calls --enforce
    # every 5s; without this, each call would re-adopt the same push, rewrite USAGE and re-run
    # _pings against a fresh `_ping` it just wrote. It also means a push OLDER than the last
    # OAuth read never claws the meter backwards.
    #
    # AND ONLY ONCE THE SCALE HAS BEEN CORROBORATED against the endpoint (`_statusline_scale`,
    # recorded below on any OAuth read that lands beside a fresh push). The docs say
    # used_percentage runs 0-100, but a doc is not an observation, and a 0-1 scale adopted on
    # the strength of one would read 73% as 0.73%- a false-low with no symptom. Until a pair
    # has been seen and agreed, this path stays shut and the meter behaves exactly as it did
    # before this build. It costs a single request, once.
    scale = old.get("_statusline_scale") or {}
    push = _statusline_snap(old)
    if push is not None and scale.get("ok") and _age_of(push["updated"]) <= BIG_FRESH \
            and _newer(push["updated"], old.get("updated", "")):
        lock = _lock_acquire()
        try:
            old = _read_json(USAGE) or old          # a concurrent writer may have committed
            push = _statusline_snap(old) or push    # re-floor the weekly gate onto the freshest read
            if (old.get("_statusline_scale") or {}).get("ok") and _newer(push["updated"], old.get("updated", "")):
                # last_attempt / error / _http describe the HTTP channel, which a zero-request
                # read has not touched. Carrying them forward keeps PROBE_FLOOR and the 429
                # cool-off honest: adopting a push must never license a burst at a sore endpoint.
                # _statusline_scale must survive too, or the next read is uncorroborated again
                # and the meter oscillates between adopting and polling.
                for k in ("last_attempt", "error", "_http", "_statusline_scale"):
                    if k in old:
                        push[k] = old[k]
                push["_ping"] = _pings(push, old.get("_ping"), old.get("updated"))
                _write_atomic(USAGE, push)
                write_live(push)
                _log(f"statusline push adopted: session {push['session_pct']:.0f}% "
                     f"weekly {push['weekly_pct']:.0f}% (gate {push['weekly_gate_pct']:.0f}%), no request made")
                return push
            return old
        finally:
            _lock_release(lock)
    # HARD BURST FLOOR (the owner, 6th July- rate-limiting is dangerous): never hit the OAuth
    # endpoint more than once per PROBE_FLOOR, even on a FORCED probe. Bursts of stacked
    # forced probes are what tripped the 429s. force bypasses the CADENCE, never this floor.
    if att_age < PROBE_FLOOR:
        return old
    # RESET-ANCHORED WAKE (the owner, 8th July 21:34- "auto wake at 9:30, why 9:34"). When a
    # stored window's reset time has just passed but no fresh meter has landed yet, bypass
    # the downtime cadence and fetch NOW, so the reset is noticed- resume ack fired + curve
    # reopened for the queue pump- within ~60s of the reset instant, not up to PROBE_EVERY
    # late. PROBE_FLOOR above bounds it to one hit per 60s.
    #
    # IT IS ITS OWN FLAG, NOT `force` (9th July). Folding it into `force` also handed it the
    # cool-off breaker below (`force and age > BIG_FRESH`), and RESET_CATCHUP is a STANDING
    # 30-minute window, not an instant: a reset arriving at a 429ing endpoint therefore broke
    # the 5-minute cool-off on EVERY probe for half an hour- ~30 requests into a rate limiter
    # that was already refusing us, which is how the 6th-July flood started. `force` keeps its
    # one meaning: a gate check recovering a wedged meter, where the breaker earns its keep.
    # The wake lifts the cadence and nothing else; a sore endpoint still gets its cool-off.
    wake = not force and _reset_due(old)
    # cadence: uptime (a worker is in flight) vs downtime (idle). No build-duration guess.
    every = BUILD_PROBE_EVERY if _build_active() else PROBE_EVERY
    if not force and not wake and age < every:
        return old
    if not force and not wake and att_age < min(RETRY_EVERY, every):
        return old
    # Rate-limit backoff. A 429 gets a HARD 5-min cool-off (RL_BACKOFF); any other error gets
    # RETRY_EVERY. Never hammer a rate-limited endpoint- that only deepens the 429.
    # BUT a forced probe on a STALE meter must be able to break the cool-off (9th July):
    # that call is a gate check trying to recover a wedged meter, and if it can't, big work
    # fails closed forever on an error the backoff won't let us clear- the deadlock the owner hit.
    # It breaks the cool-off ONLY once the last good read is past BIG_FRESH, i.e. only when
    # something is actually blocked. While the good read is still fresh there is nothing to
    # recover- the bands decide off it- so we honour the cool-off and stay off a sore endpoint.
    # PROBE_FLOOR above bounds every path: at worst one hit per 60s, never a burst.
    err = old.get("error", "")
    if err:
        backoff = RL_BACKOFF if "429" in err else RETRY_EVERY
        if att_age < backoff and not (force and age > BIG_FRESH):
            return old
    lock = _lock_acquire()
    try:
        # re-read INSIDE the lock so we merge onto the freshest _ping- a concurrent
        # writer may have committed between the first read (top of probe) and here.
        old = _read_json(USAGE) or old
        # Measured BEFORE the fetch lands, off the meter this read is about to replace:
        # how old the last clean read was, and whether big work was frozen on it. Both are
        # pure reads of `old`- no file, no network- so the lock is held no longer for them.
        prev_age = _meter_age(old)
        was_held = _big_meter_hold(old) is not None
        try:
            snap = _fetch()
        except Exception as e:
            # 401 / network / expired token / 429: skip this poll, keep last-known values.
            # Claude Code refreshes the token itself; the next poll re-reads the file.
            old["error"] = str(e)[:200]
            old["last_attempt"] = now.isoformat(timespec="seconds")
            # Record what the endpoint ACTUALLY said. urllib's HTTPError IS the response, so
            # a 429 carries its own status and headers- the one moment a real limit is
            # observable. A URLError/timeout has neither, and records status None rather
            # than inventing one. This is evidence only; the backoff above is unchanged.
            old["_http"] = _http_snap(getattr(e, "code", None), getattr(e, "headers", None))
            # SUSTAINED-error alert (the owner, 7th July: a live 429 held silently, no ping-
            # rate-limiting is dangerous, he wants to be told). A transient blip stays
            # silent; but once the last GOOD read is older than STALE_ERR_ALERT the % is
            # genuinely FROZEN- and that is also the moment big work starts failing closed
            # (BIG_FRESH), so the ping lands exactly when something is actually held.
            # EDGE-TRIGGERED, once per episode (9th July, folding in the notif-dedupe item):
            # `meter_error_open` latches here and is cleared by the recovery edge in _pings.
            # ALERT_COOLDOWN alone was NOT enough- it merely spaced the same alert 45 min
            # apart, so a long outage machine-gunned him all night. Once on the way in, once
            # on the way out, nothing in between.
            good_age = _meter_age(old)
            if good_age >= STALE_ERR_ALERT:
                ping = dict(old.get("_ping") or {})
                if not ping.get("meter_error_open"):
                    kind = "rate-limited" if "429" in str(e) else "errored"
                    lp = old.get("session_pct")
                    frozen = f"{lp:.0f}%" if isinstance(lp, (int, float)) else "last-known"
                    _fire_alert(ping, "meter_error",
                                f"🛑 Usage meter {kind}, stale {int(good_age // 60)}m- % frozen at {frozen}, "
                                f"big tasks holding till a clean read.")
                    ping["meter_error_open"] = True
                old["_ping"] = ping
            try:
                _write_atomic(USAGE, old)
            except Exception as we:
                # NEVER silent (9th July). This write is what advances `last_attempt`, and
                # PROBE_FLOOR and the 429 cool-off are both measured from it- so a swallowed
                # failure here disarms the rate guards and the next probe re-hits a sore
                # endpoint immediately. Still non-fatal, but it says so.
                _log(f"probe: could not persist the failed attempt ({we}); "
                     f"the burst floor and 429 backoff are running on a stale last_attempt")
            _log(f"probe failed (kept last-known values): {e}")
            return old
        snap["updated"] = now.isoformat(timespec="seconds")
        snap["last_attempt"] = snap["updated"]
        # CORROBORATE THE PUSH'S SCALE, free, off a read we were making anyway. Two readings of
        # the same window taken seconds apart must agree; a 0-1 scale would show up here as a
        # ~-60 point delta and latch `ok: False`, keeping the zero-request path shut. Recomputed
        # on every paired read, so a one-off disagreement heals itself rather than jamming.
        if old.get("_statusline_scale") is not None:
            snap["_statusline_scale"] = old["_statusline_scale"]     # carry forward by default
        pair = _statusline_snap()
        if pair is not None and _age_of(pair["updated"]) <= STATUSLINE_PAIR_MAX:
            delta = pair["session_pct"] - snap["session_pct"]
            agrees = abs(delta) <= STATUSLINE_SCALE_TOL
            snap["_statusline_scale"] = {"push": pair["session_pct"], "oauth": snap["session_pct"],
                                         "delta": round(delta, 1), "ok": agrees,
                                         "at": snap["updated"]}
            if not agrees:
                _log(f"statusline push DISTRUSTED: push says {pair['session_pct']}%, endpoint says "
                     f"{snap['session_pct']}% (delta {delta:+.1f}). Zero-request adoption stays shut.")
        # `old["updated"]` is the previous CLEAN read- the outage notice measures the hole
        # between it and now. Passed in rather than re-read, since USAGE still holds `old`.
        snap["_ping"] = _pings(snap, old.get("_ping"), old.get("updated"))
        _write_atomic(USAGE, snap)
        # A CLEAN PROBE IS NO LONGER SILENT (9th July). Failures logged, successes didn't- so
        # 3,122 log lines held zero evidence of a healthy meter, and a cadence wedging the gate
        # for 20 minutes in every 30 went unnoticed for three days. One line per probe (~160/day),
        # log only, never a ping.
        _prev = "no previous clean read" if prev_age > 1e8 else f"previous clean read {int(prev_age)}s old"
        _log(f"probe ok: session {snap['session_pct']:.0f}%, "
             f"weekly gate {float(snap.get('weekly_gate_pct') or 0):.0f}% ({_prev})")
        # ...and the wedge is now measurable. Fires ONLY on the hold -> clear EDGE, so "how long
        # was big work actually frozen" is greppable without ~160 lines/day of it ([[no-ping-storms]]).
        if was_held:
            _held_for = ("an unknown period (no prior clean read)" if prev_age > 1e8
                         else f"{int(prev_age // 60)}m {int(prev_age % 60)}s")
            _log(f"big-task meter hold CLEARED after {_held_for} with no clean read- big work runs again")
        write_live(snap)            # bake the /usage line for the orthogonal command
        return snap
    finally:
        _lock_release(lock)


def read_meters():
    return _read_json(USAGE) or {}


def _meter_age(snap=None):
    """Seconds since the last GOOD read (`updated`- only a clean fetch moves it).
    NOT `last_attempt`, which a failed probe bumps. 1e9 when there has never been one."""
    if snap is None:
        snap = _read_json(USAGE) or {}
    try:
        return (datetime.now() - datetime.fromisoformat(snap.get("updated", ""))).total_seconds()
    except Exception:
        return 1e9


def _big_meter_hold(snap=None):
    """The SINGLE fail-closed test for big work. Returns a reason string when a big task
    must HOLD for want of a trustworthy meter, else None.

    Gates on the AGE of the last good read, never on the `error` flag (the owner, 9th July).
    Under BIG_FRESH the last known percentages are recent enough to trust and the bands
    decide off them- an errored probe on top of a fresh read blocks nothing. Past it we
    cannot prove we're under a wall, so a big burn holds until a clean read lands.

    check() and blocked() both call this, so the soft gate and the instant belt can never
    disagree- there is one stop path, not two. Vital/fast never reach here."""
    if snap is None:
        snap = _read_json(USAGE) or {}
    age = _meter_age(snap)
    if age <= BIG_FRESH:
        return None
    why = snap.get("error") or ("no clean read yet" if age > 1e8 else f"{int(age // 60)}m since a clean read")
    return (f"meter unreadable ({why})- last good read {int(age // 60)}m old, "
            f"big tasks HOLD until a clean one lands (fail-closed, no false-low burn)")


def check(task_class="project", probe_if_stale=True, lane=None):
    """The governor. Returns {allowed, reason, session_pct, ceiling, hours_left,
    weekly_gate_pct}. vital is never blocked; stale/unreadable meters fail OPEN
    (logged)- a dead probe must never freeze the whole machine.

    `lane` (a build's .baxter_resume journal) adds the DELEGATOR's stop: if the
    delegator dropped a `.yield` marker on this lane- its touch-set drifted into the
    other lane's- the gate returns not-allowed, so the build halts + re-queues down
    the same path a usage stop uses. One stop mechanism, two reasons."""
    cls = (task_class or "project").lower()
    if cls == "vital":
        return {"allowed": True, "reason": "vital- exempt from the curve"}
    if lane:
        rf = Path(lane)
        if not rf.is_absolute():
            rf = RESUME_DIR / rf.name
        # Stamp the gate BEFORE reading the marker: reaching this line IS the lane checking
        # its gate, whether or not a yield is waiting. The governor keys its kill decision on
        # this timestamp, so it must land on every gate check, halting or not.
        _stamp_gate_check(rf)
        m = _read_json(yield_marker(rf))
        if m:
            return {"allowed": False, "yield": True,
                    "reason": f"YIELD to the other lane- {m.get('reason', 'clash')} "
                              f"(vs '{str(m.get('against', ''))[:60]}'). Halt + re-queue; it restarts when that lane frees."}
    snap = read_meters()
    try:
        age = (datetime.now() - datetime.fromisoformat(snap.get("updated", ""))).total_seconds()
    except Exception:
        age = 1e9
    # a project-class check IS a build moment: never gate a burn on a minutes-old
    # meter (the 07:25 catch- it read 75 while reality was 85). Routine tolerates 10 min.
    # Gate freshness is PROBE_FLOOR (<=60s), NOT the base cadence: the 6th-July slowdown
    # raised BUILD_PROBE_EVERY to 90, but the project gate must still force an on-demand
    # fresh read whenever the meter is >60s old (contract step 29's "<=60s" rule). The slow
    # base cadence governs IDLE/between-gate polling; the gate always reads fresh. PROBE_FLOOR
    # caps the forced read to once/60s so this can never itself hammer the endpoint into a 429.
    fresh_by = PROBE_FLOOR if cls == "project" else 600
    if age > fresh_by and probe_if_stale:
        snap = probe(force=True)
        try:
            age = (datetime.now() - datetime.fromisoformat(snap.get("updated", ""))).total_seconds()
        except Exception:
            age = 1e9
    if cls != "project" and age > STALE_AFTER:
        # Fail-OPEN keeps LIGHT work (briefs, answering the owner) alive on a dead probe.
        _log(f"meters stale ({int(age)}s)- failing open for {cls}")
        return {"allowed": True, "reason": "meters unavailable- failing open"}
    pct = float(snap.get("session_pct") or 0)
    wk = float(snap.get("weekly_gate_pct") or snap.get("weekly_pct") or 0)
    hl = _hours_left(snap.get("session_resets_at", ""))
    ceil = ceiling(hl)
    out = {"session_pct": pct, "ceiling": round(ceil, 1),
           "hours_left": round(hl, 2) if hl is not None else None,
           "weekly_gate_pct": wk}
    if cls == "routine":
        if pct >= ROUTINE_STOP_SESSION or wk >= ROUTINE_STOP_WEEKLY:
            out.update(allowed=False, reason=f"vital-only- session {pct:.0f}% / weekly {wk:.0f}% (routine holds past {ROUTINE_STOP_SESSION:.0f})")
        else:
            out.update(allowed=True, reason="under the vital-only line")
        return out
    # project-class. The ONE fail-closed guard: can we still trust the last good read?
    # (9th July- this replaces both the old `if snap.get("error")` test here and the
    # duplicate error-or-stale test in blocked(), which disagreed with each other and
    # with STALE_AFTER's fail-open belt.) A fresh read carries the day whatever the
    # probe's last attempt did; a stale one holds the queue whether it errored or not.
    hold = _big_meter_hold(snap)
    if hold:
        _log(f"project gate fail-closed: {hold}")
        out.update(allowed=False, reason=hold)
        return out
    if wk >= WEEKLY_PROJECT_GATE:
        out.update(allowed=False, reason=f"weekly meter {wk:.0f}% >= {WEEKLY_PROJECT_GATE:.0f}- no project work until the reset")
    elif pct >= ceil:
        out.update(allowed=False, reason=f"session {pct:.0f}% >= the {ceil:.0f}% big-stop")
    else:
        out.update(allowed=True, reason=f"session {pct:.0f}% under the {ceil:.0f}% big-stop")
    return out


# ---- HARD ENFORCEMENT (5th July: "pause at 80 must ACTUALLY pause") ---------
# The soft check() asks permission; a runaway worker that never asks, or asks on a
# stale meter, blew a whole window. The hard layer is a STOP FLAG that everything
# reads instantly (a file, no network) + a watchdog (in the watcher) that KILLS
# in-flight workers when the band is breached. Levels: none / big / routine.
def _band(snap):
    """Return 'routine' | 'big' | None for the current meters (no side effects).
    routine = >=90%, BARE MINIMUM- big + routine paused, vitals still run (never stop).
    big     = >=80%, big tasks paused into the queue; routine + vital run.
    None    = under 80%, everything runs.
    'floor' is RETIRED (8th July)- FLOOR_* sit unreachable so it can't be returned.

    THE DRAIN BAND IS DELIBERATELY ABSENT HERE. Do not 'complete' the ladder with a
    SOFT_STOP_SESSION rung: this function's return value is what enforce() writes into
    .baxter_stop, which blocked() reads and the watcher's watchdog KILLS in-flight
    workers on. A rung at 60 would turn the drain into a hard kill- the precise thing
    the band exists to avoid. The soft stop lives ONLY in lane_capacity(), where it
    decides whether a NEW lane opens and touches nothing already running."""
    pct = float(snap.get("session_pct") or 0)
    wk = float(snap.get("weekly_gate_pct") or snap.get("weekly_pct") or 0)
    if pct >= FLOOR_SESSION or wk >= FLOOR_WEEKLY:
        return "floor"
    if pct >= ROUTINE_STOP_SESSION or wk >= ROUTINE_STOP_WEEKLY:
        return "routine"
    if pct >= BIG_STOP_SESSION or wk >= BIG_STOP_WEEKLY:
        return "big"
    return None

def enforce(probe_if_stale=True):
    """Recompute the band from the meters and write/clear .baxter_stop accordingly.
    Cheap: reads the cached meter file; only forces an OAuth probe if it's stale.
    Returns the level ('any'/'big'/None). The watcher calls this every beat, then
    kills runaways if the level warrants it (kill lives in the watcher- native)."""
    snap = read_meters()
    try:
        age = (datetime.now() - datetime.fromisoformat(snap.get("updated", ""))).total_seconds()
    except Exception:
        age = 1e9
    if probe_if_stale:
        # Self-rate-limited: probe() refreshes only on its uptime/downtime cadence and is
        # hard-floored against bursts, so calling it every 15s beat is safe- it hits the
        # endpoint only when its own cadence allows. This replaces the old force-probe that
        # fired every beat and hammered the endpoint into 429s (the owner, 6th July).
        snap = probe()
    # A failed probe leaves stale (possibly false-low) values. Don't LIFT a hard stop
    # on that- hold the last enforced level until a clean read lands (6 Jul false-low
    # incident). Raising/setting a stop from stale data is safe; only CLEARING is not.
    if snap.get("error") and STOP_FLAG.exists():
        _log("probe errored- holding existing stop flag (no false-low clear)")
        return (_read_json(STOP_FLAG) or {}).get("level")
    # STEPPED breach self-clear (7th July): if a step-breach ceiling has been reached, drop the
    # marker HERE off the fresh meter so the very next gate re-blocks on the normal band- work
    # advances exactly one tier then re-pauses. Harmless while under the ceiling (no side effect).
    _breach_step_active(snap)
    # The 15-min queue reshuffle rides this beat. It sits ABOVE the `level is None` early
    # return on purpose: a clear band is ~99% of beats, so a hook placed below that line
    # would never fire in normal operation while a source grep for the call still passed.
    # Swallowed, always: enforce() IS the governor. A reprio exception here would mean no
    # band recomputed and no .baxter_stop written, and builds would run straight past 80%.
    try:
        queue_reprioritize()
    except Exception as exc:
        _log(f"reprio pass failed (governor beat unaffected): {exc.__class__.__name__}: {exc}")
    # The PRD pass rides the same beat, and is swallowed for the same reason: an ask waiting on
    # the PM must never be able to stop the governor from writing .baxter_stop.
    try:
        prd_sweep()
    except Exception as exc:
        _log(f"prd sweep failed (governor beat unaffected): {exc.__class__.__name__}: {exc}")
    level = _band(snap)
    if level is None:
        if STOP_FLAG.exists():
            try: STOP_FLAG.unlink()
            except Exception: pass
            _log("stop flag CLEARED- back under the band")
        return None
    prev = _read_json(STOP_FLAG) or {}
    payload = {"level": level, "session_pct": snap.get("session_pct"),
               "weekly_gate_pct": snap.get("weekly_gate_pct"),
               "at": datetime.now().isoformat(timespec="seconds")}
    try:
        _write_atomic(STOP_FLAG, payload)
    except Exception:
        pass
    if prev.get("level") != level:
        _log(f"stop flag SET to '{level}' (session {snap.get('session_pct')}%, weekly {snap.get('weekly_gate_pct')}%)")
    return level

def _override_active():
    """the owner's breach authorisation. A .baxter_override file with a future 'until'
    lifts the BIG (80%) stop up to the 90% vital-only wall- so authorised big work
    runs the 80-90 band. It NEVER lifts the 90% wall (the final 10% stays vital-only
    unless he clears the flag himself). Returns the remaining minutes, or None."""
    o = _read_json(VAULT / ".baxter_override")
    if not o:
        return None
    try:
        until = datetime.fromisoformat(o.get("until", ""))
        left = (until - datetime.now()).total_seconds() / 60
        return left if left > 0 else None
    except Exception:
        return None

def _breach_active():
    """the owner's full-breach authorisation (the /breach command). A .baxter_breach file
    with a future 'until' lifts EVERY hard band (big at 80 AND the 90% vital-only
    wall) so authorised work of any class can run at any %. Stronger than --override,
    which only lifts the 80->90 big band. Returns minutes left, or None. Breach is
    the ONLY thing that runs big/routine work above 90%."""
    o = _read_json(VAULT / ".baxter_breach")
    if not o:
        return None
    try:
        until = datetime.fromisoformat(o.get("until", ""))
        left = (until - datetime.now()).total_seconds() / 60
        return left if left > 0 else None
    except Exception:
        return None

def _quiet95_active():
    """the owner's 95-only window (8th July, the first MAX period: "only warn me at 95% for
    this period, dont worry about 80 and 90"). A .baxter_quiet95 file with a future
    'until' mutes the 80/90 SESSION band pings and swaps them for one 95% heads-up. A
    togglable window setting, NOT a permanent probe change- returns minutes left, or
    None. Governor ENFORCEMENT is untouched (the breach handles that separately); this
    only shapes what pings fire."""
    o = _read_json(QUIET95)
    if not o:
        return None
    try:
        until = datetime.fromisoformat(o.get("until", ""))
        left = (until - datetime.now()).total_seconds() / 60
        return left if left > 0 else None
    except Exception:
        return None

def _next_tier(current, tiers):
    """The lowest tier threshold STRICTLY above `current`- the next rung up the ladder.
    If usage already sits above the top rung there is nothing higher, so return 100.0
    (run to the very top). Used to compute a step-breach ceiling off the live meter."""
    above = [t for t in tiers if t > current]
    return min(above) if above else 100.0

def _breach_step_active(snap=None):
    """the owner's STEPPED breach (the /breach command, 7th July spec). A .baxter_breach_step
    marker holds a session-ceiling + weekly-ceiling = the NEXT tier rung above where usage
    sat when he set it. While the marker is live AND BOTH dimensions are still under their
    ceiling, big work runs one tier further than the normal band would allow; the instant
    EITHER dimension reaches its ceiling the marker SELF-CLEARS here and the ordinary band
    re-blocks. So one /breach advances usage by exactly ONE tier, then re-pauses- he must
    /breach again to step further. Returns the ceilings dict while live, else None (dropping
    the marker on the way out). DISTINCT from --override (70->80 big band only) and --breach
    (the gold spend- lifts EVERY band for a fixed window). Reads the cached meter (no network),
    so it's an instant gate like blocked()."""
    m = _read_json(BREACH_STEP)
    if not m:
        return None
    if snap is None:
        snap = _read_json(USAGE) or {}
    pct = float(snap.get("session_pct") or 0)
    wk = float(snap.get("weekly_gate_pct") or snap.get("weekly_pct") or 0)
    sc = float(m.get("session_ceiling") or 100.0)
    wc = float(m.get("weekly_ceiling") or 100.0)
    if pct < sc and wk < wc:
        return {"session_ceiling": sc, "weekly_ceiling": wc}
    # a ceiling reached- this step is done: re-seal the marker and re-block on the normal band
    try:
        BREACH_STEP.unlink()
    except Exception:
        pass
    _log(f"step-breach COMPLETE (session {pct}% vs {sc}, weekly {wk}% vs {wc})- marker cleared, normal band back")
    return None

def soft_stopped(snap=None):
    """The 60% DRAIN band: is the pump forbidden from opening a NEW lane right now?

    Returns (True, reason) or (False, ''). Reads the cached meter only- no network- so
    lane_capacity() can call it on every governor beat. Its one side effect is the
    step-breach self-clear that blocked() already performs, and only past its ceiling.

    This is NOT an enforcement gate and must never become one. It says nothing about
    work already in flight: a lane running at 65% runs to completion, is never killed,
    and never sees this. blocked() and _band() are untouched by it. The one consumer is
    lane_capacity(), which reports capacity == the live lane count, so the pump's
    `free = cap - live` comes out at zero and the lanes drain themselves empty.

    The same three authorisations that lift the 80% big stop lift this one. Without
    that, `--override` inside the 60-80 band is a silent no-op: the owner says "go big" at
    65%, the confirmation prints, and the pump still opens nothing."""
    if snap is None:
        snap = read_meters()
    pct = float(snap.get("session_pct") or 0)
    if pct < SOFT_STOP_SESSION:
        return (False, "")
    # Only now touch the authorisation files- under the band they cannot matter.
    if _breach_active() is not None or _override_active() is not None or _breach_step_active(snap) is not None:
        return (False, "")
    return (True, f"soft stop (>={SOFT_STOP_SESSION:.0f}%)- session {pct}%, lanes draining: "
                  f"no new lane opens, in-flight builds finish ('override' or 'breach' to authorise)")


# lane_capacity() runs on every governor beat, so the drain would otherwise print a line
# a second. Latch the last state and log only the FLIP. Without a line at all, `pump:
# free=0` at 62% is indistinguishable from a saturated fleet, and "why is nothing
# building?" is unanswerable from .baxter.log.
_SOFT_STOP_LAST = None


def blocked(kind="big"):
    """INSTANT gate (no network)- the belt to check()'s braces. A worker reads this at
    the top and self-aborts if set. The band map (kind -> which bands stop it):
      kind 'big'/'project'  -> stopped by big(80)+ / routine(90)+
      kind 'routine'        -> stopped by routine(90)+   (runs the 80-90 band)
      kind 'vital'/'fast'   -> NEVER stopped (the bare-minimum lane runs at any %)
    --override lifts the BIG(80) stop for big work into the 80-90 band. --breach lifts
    EVERY band. The 90%+ everything-stops floor is RETIRED (8th July)- vitals never
    pause. Returns (True, reason) or (False, '')."""
    # NO DRAIN RUNG HERE, AND THERE NEVER MAY BE ONE. The drain band lives in
    # soft_stopped(), read only by lane_capacity(). This function is the watchdog's kill
    # path: a worker reads it and self-aborts, and enforce() mints .baxter_stop off _band().
    # A 60 rung here would therefore CUT the in-flight builds that the owner's order exists to
    # let finish ("upon completion of the current task. lane closes"). The drain stops the
    # pump OPENING a lane; it says nothing to a lane already running.
    k = (kind or "big").lower()
    if k == "project":
        k = "big"
    breach = _breach_active()          # gold spend: lifts every band, incl the 90% floor
    step = _breach_step_active()       # stepped breach: lifts up to the NEXT tier, then self-clears
    # Fail-CLOSED belt for big tasks on an unreadable meter (6 Jul false-low incident:
    # a rate-limited read held a stale 65% while real usage was 75%, so a build slipped
    # the 70% wall). The STOP_FLAG alone can't catch this: a false-LOW read leaves no
    # flag set. So before trusting the flag, refuse a big burn whenever the last GOOD
    # read has aged past BIG_FRESH. Same _big_meter_hold() the soft gate uses- one test,
    # one stop path. Vital/fast lanes are cheap and stay alive (answering the owner is never
    # gated). Only an explicit breach lifts this hold- a meter we can't refresh cannot
    # prove we're under a wall, so nothing weaker overrides it.
    if k == "big" and breach is None:
        hold = _big_meter_hold()
        if hold:
            return (True, hold)
    f = _read_json(STOP_FLAG)
    if not f:
        return (False, "")
    level = f.get("level")
    pct = f.get("session_pct")
    # legacy 'floor' flags (written by pre-8th-July code): the floor is retired-
    # treat as vital-only so vitals are never frozen by a stale flag file.
    if level == "floor":
        level = "routine"
    # VITAL-ONLY / bare minimum (>=90%): big + routine paused, vitals still run.
    if level == "routine":
        if k in ("vital", "fast"):
            return (False, "")   # the vital lane never stops
        if breach is not None or step is not None:
            return (False, "")   # breach (or a live step-breach) lifts the 90% stop for big/routine
        return (True, f"hard stop VITAL-ONLY (>=90%)- session {pct}%, bare minimum- big + routine paused, vitals run ('breach' to authorise)")
    # BIG stop (>=80%): only big tasks pause; routine + vital run.
    if level == "big":
        if k == "big":
            if breach is not None or step is not None or _override_active() is not None:
                return (False, "")   # authorised: big work runs the 80-90 band
            return (True, f"hard stop BIG- session {pct}%, big tasks paused 80-90% ('breach' to authorise)")
        return (False, "")   # routine + vital run the 80-90 band
    return (False, "")


# ---- BIG-TASK QUEUE (5th July, his 12:00 + 14:33 order) --------------------
# One project-class task at a time, ever. Priorities: lower runs first.
PRIO_URGENT = 1      # the owner said "do this first"
PRIO_RESUME = 2      # interrupted mid-flight- finish what's started before new work
PRIO_DEFAULT = 5     # a normal queued build
PRIO_BACKGROUND = 8  # nice-to-have / passive prep
PRIO_GATED = 9       # waiting on a human- BELOW background, so it never holds head-of-line

# ---- AUTO-RESHUFFLING (9th July) -------------------------------------------
# `priority` is DERIVED and disposable; `base_priority` is the anchor, written only by a
# human decision or an explicit band assignment (enqueue -> N, halt -> 2, --edit -> N).
# Every 15 min the governor beat re-scores the pending queue off the anchor: starved entries
# age-promote, human-gated ones sink, the owner's p1 pins freeze. Auto-scoring never mints a p1
# and never touches `rank` or `queued_at`.
REPRIO_EVERY = 900   # seconds between passes; --force ignores it
REPRIO_AGE_CAP = 3   # an entry may age-promote at most 3 bands, however long it has waited

# `rank` is a PLACED slot within a priority band (9th July, his "cozy drag and drop" ask).
# Before it, the run order was (priority, queued_at), so a band of nine tasks could only be
# reordered by shoving one into another band- or by rewriting queued_at, which would fake a
# slot and destroy the one record of when a task actually arrived. RANK_DEFAULT is large, so
# an unranked arrival sorts BEHIND every explicitly-placed one and stays FIFO among its peers.
RANK_DEFAULT = 10 ** 6


def _prio_int(e):
    try:
        return int(e.get("priority", PRIO_DEFAULT))
    except (TypeError, ValueError):
        return PRIO_DEFAULT


def _rank_int(e):
    """A bad rank is COERCED to the default, never raised on: _qkey is the sort key for every
    read of the queue, so one malformed entry must not make the whole queue unreadable."""
    try:
        return int(e["rank"])
    except (KeyError, TypeError, ValueError):
        return RANK_DEFAULT


def _qkey(e):
    return (_prio_int(e), _rank_int(e), str(e.get("queued_at", "")))


# ---- THE QUEUE LOCK (9th July, fault 4 of the edit-surface investigation) ----
# queue_write() was atomic but UNLOCKED. Every mutator does a read-modify-write of the
# WHOLE list- enqueue, --ungate, the triage pump popping a task into a lane, /queue's
# reprioritise button- so two of them overlapping is a lost update: last writer wins and
# the other's change vanishes with no error anywhere. Atomicity protects a reader from a
# half-written file; it does nothing for a race between two writers.
#
# Same shape as USAGE_LOCK, which fixed exactly this bug for the ping state on 8th July:
# O_EXCL create, steal a >30s-stale lock (a crashed holder), and if it STILL can't be had,
# proceed anyway- a queue command that hangs is worse than one that races.
#
# Re-entrant by depth, because the mutators nest: enqueue() opens a transaction and then
# calls queue_read()/queue_write() inside it. Held across the whole read-modify-write via
# `with queue_txn():`, so the critical section is the transaction, not the two file ops.
# A lone queue_read()/queue_write() outside a transaction still locks itself, so a caller
# who forgets the ctx manager degrades to atomic-single-op rather than to nothing.
QUEUE_LOCK = VAULT / ".baxter_task_queue.lock"
_QTXN_DEPTH = 0


def _queue_lock():
    """The lockfile beside whatever TASK_QUEUE currently points at. Derived, not fixed,
    because the selftests swap TASK_QUEUE for a scratch file- a hard-coded lock would make
    them contend with the LIVE queue, and a stale scratch lock would then stall the pump."""
    try:
        return Path(str(TASK_QUEUE)).with_suffix(".lock")
    except Exception:
        return QUEUE_LOCK


@contextlib.contextmanager
def queue_txn():
    """Hold the queue lock across a read-modify-write. Re-entrant within one process."""
    global _QTXN_DEPTH
    path = _queue_lock()   # captured once: release must unlink the file acquire created
    fd = _lock_acquire(retries=60, delay=0.05, path=path) if _QTXN_DEPTH == 0 else None
    _QTXN_DEPTH += 1
    try:
        yield
    finally:
        _QTXN_DEPTH -= 1
        if _QTXN_DEPTH == 0:
            _lock_release(fd, path)


def _new_qid():
    return uuid.uuid4().hex[:8]


def queue_read():
    """The queue, run-order sorted. Drains the legacy .baxter_interrupted.json
    inbox first (old halt calls / prompts may still write it)- nothing is lost,
    and backfills a stable `id` onto any entry that predates them (9th July), so
    an entry can be addressed by something other than its exact prose.

    It also anchors `base_priority` on first sight- the immutable number the 15-min reprio
    beat re-derives `priority` from. Backfilled ONCE and never overwritten: re-anchoring off
    the derived number is a ratchet (an age-promoted entry would feed its own promotion back
    in and climb to the top). The backfill counts into `backfilled`, because that counter is
    the only thing that makes queue_read persist- an anchor written in memory and dropped on
    the next read is worse than none, since every beat then re-anchors off the derived value."""
    with queue_txn():
        q = _read_json(TASK_QUEUE) or []
        legacy = _read_json(INTERRUPTED) or []
        if legacy:
            for e in legacy:
                e.setdefault("priority", PRIO_RESUME)
                e.setdefault("queued_at", e.get("halted_at") or datetime.now().isoformat(timespec="seconds"))
                q.append(e)
            try: INTERRUPTED.unlink()
            except Exception: pass
            _log(f"drained {len(legacy)} legacy interrupted entr{'y' if len(legacy)==1 else 'ies'} into the queue")
        seen, backfilled = set(), 0
        for e in q:
            i = str(e.get("id") or "")
            if not i or i in seen:          # missing, or a duplicate from a hand-copied entry
                e["id"] = _new_qid()
                backfilled += 1
            seen.add(e["id"])
            if e.get("base_priority") is None:   # `is None`, not `not in`: a null must anchor too
                e["base_priority"] = _prio_int(e)
                backfilled += 1
        if legacy or backfilled:
            _write_atomic(TASK_QUEUE, sorted(q, key=_qkey))
        return sorted(q, key=_qkey)


def queue_write(q):
    with queue_txn():
        q = [e for e in q]
        # ---- QUEUE-LOSS GUARD (10th July) ----
        # A human-gated entry has exactly ONE legitimate exit from the queue: an explicit
        # queue_drop, which records it in the dropped store one line BEFORE it calls us. Any
        # other write that sheds a gated row is a lost update- a torn, pre-gated snapshot
        # written under the fail-open queue lock (_lock_acquire proceeds unlocked after ~3s).
        # So reconcile every write against what the file actually holds: re-read on-disk, and
        # for each human-gated id present there but ABSENT from `q` with NEITHER a dropped-store
        # record NOR a live lane journal, re-insert its original entry and log a queue_loss
        # reject. The queue physically cannot shed a gated row that was never dropped. It is a
        # cheap no-op on every ordinary write (enqueue/reprio/pop removes no gated id), and it is
        # idempotent- a healed list removes nothing, so it neither re-heals nor re-alerts.
        try:
            on_disk = _read_json(TASK_QUEUE)
            on_disk = on_disk if isinstance(on_disk, list) else []
            new_ids = {str(e.get("id", "")).strip().lower()
                       for e in q if isinstance(e, dict) and str(e.get("id", "")).strip()}
            gone = [e for e in on_disk
                    if isinstance(e, dict) and is_human_gated(e)
                    and str(e.get("id", "")).strip()
                    and str(e.get("id", "")).strip().lower() not in new_ids]
            if gone:
                dropped = _read_json(_dropped_store())
                dropped_ids = {str((r.get("entry") or {}).get("id", "")).strip().lower()
                               for r in (dropped if isinstance(dropped, list) else [])
                               if isinstance(r, dict)}
                restored = []
                for e in gone:
                    if str(e.get("id", "")).strip().lower() in dropped_ids or _live_lane_of(e):
                        continue                       # accounted: a real drop, or a live lane
                    q.append(dict(e))                  # heal- put the row back before we write
                    restored.append(e)
                    record_reject(e, f"gated on {gate_of(e) or 'owner'}, dropped by a write with "
                                     f"no drop record- restored", kind="queue_loss")
                if restored:
                    _log("queue-loss GUARD- restored {} gated entr{} a write would have dropped "
                         "with no drop record: {}".format(
                             len(restored), "y" if len(restored) == 1 else "ies",
                             "; ".join(f"[{e.get('id')}] {str(e.get('task', ''))[:40]}"
                                       for e in restored)))
        except Exception as exc:
            _log(f"queue-loss guard skipped ({type(exc).__name__}: {exc})")
        _write_atomic(TASK_QUEUE, sorted(q, key=_qkey))


def _reprio_stamp():
    """The cadence stamp beside whatever TASK_QUEUE currently points at.

    Derived, not fixed, for exactly the reason _queue_lock() and _dropped_store() are: the
    exam and the selftests swap TASK_QUEUE for a scratch file, and a hard-coded
    VAULT/.baxter_reprio_stamp would have every test read the owner's live stamp (so a real pass
    silently no-ops mid-test) and every forced run clobber it."""
    try:
        return Path(str(TASK_QUEUE)).with_name(".baxter_reprio_stamp")
    except Exception:
        return VAULT / ".baxter_reprio_stamp"


def reprio_score(entry, now):
    """The derived priority for one entry. PURE- no I/O, no clock read; `now` is passed in
    so a whole pass scores against one instant and the exam can pin it.

    - base 1 -> 1. the owner's pin is frozen: it neither ages nor sinks.
    - human-gated -> 9 (PRIO_GATED), below PRIO_BACKGROUND, so a task waiting on him can
      never sit at head-of-line and starve the runnable queue behind it.
    - otherwise max(2, base - min(3, age_days)). The floor of 2 is load-bearing: auto-scoring
      must NEVER mint a p1, which means "the owner said do this first" and nothing else.

    The gate is read through gate_of(), never off the raw field: one live entry carries
    `gated_on: null` rather than "", and a raw read scores it as gated."""
    base = _prio_int(entry) if entry.get("base_priority") is None else int(entry["base_priority"])
    if base == PRIO_URGENT:
        return PRIO_URGENT
    if gate_of(entry):
        return PRIO_GATED
    try:
        age_days = int((now - datetime.fromisoformat(str(entry.get("queued_at")))).total_seconds() // 86400)
    except Exception:
        age_days = 0
    return max(PRIO_RESUME, base - min(REPRIO_AGE_CAP, max(0, age_days)))


def queue_reprioritize(now=None, force=False):
    """Re-score every pending entry off its anchor. Returns (changed, total).

    Called from enforce(), i.e. every watcher beat, and gated to one real pass per
    REPRIO_EVERY seconds. The stamp is written BEFORE the work, so a crash mid-pass cannot
    make this re-fire on every 15s beat.

    The whole read-modify-write sits inside one queue_txn(): unlocked, it is a lost update
    against the pump, which pops entries out of this same file. `rank` (his drag-and-drop
    slot) and `queued_at` (the one record of arrival) are never touched, and queue_write is
    called only when a priority actually moved- otherwise the file churns four times an hour
    for nothing."""
    stamp = _reprio_stamp()
    if not force:
        prev = (_read_json(stamp) or {}).get("at")
        try:
            if prev and (datetime.now() - datetime.fromisoformat(prev)).total_seconds() < REPRIO_EVERY:
                return 0, 0
        except Exception:
            pass                                   # unparseable stamp: treat the pass as due
    try:
        _write_atomic(stamp, {"at": datetime.now().isoformat(timespec="seconds")})
    except Exception:
        pass
    now = now or datetime.now()
    with queue_txn():
        q = queue_read()
        changed = 0
        for e in q:
            want = reprio_score(e, now)
            if _prio_int(e) != want:
                e["priority"] = want
                changed += 1
        if changed:
            queue_write(q)
        total = len(q)
    # Logged on EVERY pass, not only when N>0 (a deviation from the design note). The note
    # feared churn, but the churn it feared is the queue FILE, guarded above. Nothing else
    # can prove that the LIVE watcher child- rather than the working tree- is running this
    # code, and a beat that is only observable when it happens to change something is a beat
    # you cannot check on a settled queue. Four lines an hour.
    _log(f"queue reprioritised: {changed} of {total} re-scored")
    return changed, total


# ---- WHERE IT ACTUALLY SITS (the owner, 9th July 09:38) ---------------------------------
# "Every queued confirmation must state the exact position." Not a formatting preference:
# a position can only be printed by a process that has WRITTEN the entry and then read the
# order back, so demanding the number is what makes the confirmation impossible to fake.
# The phrase is therefore built HERE, from the live queue, and nowhere else- a lane that
# formats its own position is a lane that can invent one.
def queue_position(entry):
    """(pos, total)- the 1-based slot of `entry` in the LIVE run order; pos 0 if it is gone.

    Addressed by `id`, never by prose: the caller holds the dict enqueue() just returned,
    and between the write and the question the pump may have re-sorted the queue around it.
    """
    qid = (entry or {}).get("id") if isinstance(entry, dict) else str(entry or "")
    q = queue_read()
    for i, e in enumerate(q, 1):
        if e.get("id") == qid:
            return i, len(q)
    return 0, len(q)


def _benched_store():
    """The benched-task store beside whatever TASK_QUEUE currently points at.

    Derived, not fixed, for the same reason _dropped_store() and _reprio_stamp() are: the
    exam and the selftests swap TASK_QUEUE for a scratch file, and a hard-coded
    VAULT/'.baxter_task_queue_benched.json' would count OWNER'S real benched pile into a
    fixture's totals- a number that looks right and means nothing.
    """
    try:
        p = Path(str(TASK_QUEUE))
        return p.with_name(f"{p.stem}_benched{p.suffix or '.json'}")
    except Exception:
        return VAULT / ".baxter_task_queue_benched.json"


def _queue_trend_log():
    """The arrivals-vs-departures ledger, beside TASK_QUEUE for the same reason."""
    try:
        return Path(str(TASK_QUEUE)).with_name(".baxter_queue_trend.jsonl")
    except Exception:
        return VAULT / ".baxter_queue_trend.jsonl"


def _bench_count():
    """How many tasks are benched. NEVER raises- see _lane_state()."""
    try:
        return len(_read_json(_benched_store()) or [])
    except Exception:
        return 0


def _lane_state():
    """(running_count, {ids}) read off the live lane journals. NEVER raises.

    Both halves come from ONE glob: position_line needs the count for its denominator and
    the id-set to tell a running entry from a departed one, and two calls would be two
    passes of file I/O on the enqueue confirmation path.

    That path is why this degrades to (0, set()) rather than propagating: the confirmation
    is printed AFTER the queue write has already succeeded. A journal caught mid-write must
    cost the owner a "(0 running)" he can ignore, never the position line for a task that is
    genuinely on the queue.
    """
    try:
        js = [e for _p, e in lane_journals() if isinstance(e, dict)]
    except Exception:
        return 0, set()
    return len(js), {str(e["id"]) for e in js if e.get("id")}


def queue_totals():
    """{'pending', 'running', 'benched'}- three ints, always, whatever is broken.

    `pending` is REMAINING WORK, not a lifetime total. the owner, 9th July: "i have never seen
    this build position total number EVER go down." He was right on both counts. The figure
    is only ever printed at the instant of an ARRIVAL, so every number he has ever been
    shown is a local peak, and every drain between two of his messages happened where he
    could not see it.
    """
    try:
        pending = len(queue_read())
    except Exception:
        pending = 0
    running, _live_ids = _lane_state()
    return {"pending": pending, "running": running, "benched": _bench_count()}


def position_line(entry):
    """'position 3 of 19 pending (5 running, 2 benched), p5'.

    The word `pending` is load-bearing. Without it the denominator reads as a lifetime count
    of everything ever asked for, which is the reading that made the number look stuck: it
    names the work still WAITING, and the two figures beside it account for the work that has
    left the queue without being finished.

    An entry holding no slot is NOT automatically running. It used to say so unconditionally,
    so a benched, dropped or long-finished entry announced itself as live work. The lane
    journals settle it: on one, it is running; on none, it has left the queue.
    """
    pos, total = queue_position(entry)
    prio = _prio_int(entry if isinstance(entry, dict) else {})
    running, live_ids = _lane_state()
    if pos:
        return (f"position {pos} of {total} pending "
                f"({running} running, {_bench_count()} benched), p{prio}")
    qid = str((entry or {}).get("id") or "") if isinstance(entry, dict) else str(entry or "")
    if qid and qid in live_ids:
        return f"running now (p{prio})"
    return f"no longer in the queue (p{prio})"


def _stamp_trend(entry, event="enqueue"):
    """One JSON line per arrival into .baxter_queue_trend.jsonl.

    THE ARTEFACT THAT ANSWERS HIM NEXT TIME. The queue file holds only the present, and the
    log stamped arrivals but never the departures between them, so "is it going down?" could
    only be answered from memory. Each line stamps the three totals at an arrival; the
    `pending` figures across consecutive lines ARE the drain.

    Never raises: bookkeeping hung off a queue write that has already succeeded.
    """
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event,
               "id": (entry or {}).get("id") if isinstance(entry, dict) else None}
        rec.update(queue_totals())
        with open(_queue_trend_log(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _announce_position(entry):
    """Print the slot the write just earned.

    This lives in enqueue() rather than in the `--queue` CLI branch on purpose: EVERY
    caller- the CLI, halt(), the fast lane's placeholder- then states a real position, and
    none of them has to compute one of its own. Never raises: a daemon whose stdout is
    closed must not die confirming a queue write that succeeded.
    """
    try:
        print(f"queued at {position_line(entry)}")
    except Exception:
        pass
    _stamp_trend(entry)


# ---- SEMANTIC DUPLICATES (9th July) -----------------------------------------------
# Two tickets- "Repair worker: raise retry cap 2->3" and "Raise repair-worker retry cap
# from 2 to 3 attempts PERMANENT"- were queued hours apart for work that had ALREADY
# shipped (MAX_REPAIRS=3 at baxter_verify.py:66). Their touch-sets named different files,
# so the delegator saw no clash and started BOTH: two lanes burnt on one stale ask, and one
# of them declared utils/baxter_repair.py, a module that has never existed. enqueue() keyed
# identity on the EXACT task string, so a reword was simply a different task.
#
# The test is a token-set overlap, and it needs BOTH halves. Overlap coefficient alone calls
# every short task a duplicate of a longer one containing it ("fix the queue" sits inside
# "fix the queue delegator" at overlap 1.0). Jaccard alone misses a reword that adds detail.
# Requiring both, measured on the real pair: jaccard 0.75, overlap 0.88- caught. Across all
# 946 pairs of the 44 entries live in the queue when this was written: zero collisions.
DUP_JACCARD = 0.70
DUP_OVERLAP = 0.80
DUP_MIN_TOKENS = 3

# Filler that carries no task identity. 'permanent'/'asap'/'now' are urgency decoration-
# the two 9th-July tickets differed by little else.
_DUP_STOPWORDS = frozenset("""
the a an of from to and for in on at is it that this with by as be so
permanent asap please now
""".split())


def _dup_stem(w):
    """A crude, symmetric suffix trim- 'permanently'->'permanent', 'attempts'->'attempt'.

    It only has to fold both sides of a comparison the same way, and it is never shown to
    anyone. 'ss' is spared so 'process' does not become 'proces'."""
    if len(w) > 5 and w.endswith("ly"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def _task_tokens(text):
    """A task's identity as a bag of words. Pure- no I/O, no globals.

    Splitting on every non-alphanumeric is what makes '2->3' and 'from 2 to 3' both yield
    {2, 3}: the arrow is punctuation, not a word. That single detail is what links the two
    9th-July wordings; a whitespace split leaves '2->3' as one opaque token and they share
    nothing but 'retry' and 'cap'."""
    flat = "".join(c if c.isalnum() else " " for c in str(text).lower())
    return {s for s in (_dup_stem(w) for w in flat.split()) if s and s not in _DUP_STOPWORDS}


def _near_duplicate(task, candidates):
    """The first candidate entry that says the same thing as `task`, else None.

    An ask with fewer than DUP_MIN_TOKENS distinctive tokens is not judged AT ALL. Two
    three-word tasks can share every word and still mean different things, and refusing a
    build on that evidence is a guess- silence is the honest answer. Refusing wrongly is
    worse than the duplicate this guards against: a duplicate costs a lane, a false refusal
    loses a build nobody notices is missing."""
    a = _task_tokens(task)
    if len(a) < DUP_MIN_TOKENS:
        return None
    for e in candidates or []:
        b = _task_tokens((e or {}).get("task", ""))
        if len(b) < DUP_MIN_TOKENS:
            continue
        shared = len(a & b)
        if not shared:
            continue
        if shared / len(a | b) >= DUP_JACCARD and shared / min(len(a), len(b)) >= DUP_OVERLAP:
            return e
    return None


def _dup_candidates(pending=None):
    """Everything a fresh ask could be a restatement OF: the pending queue, plus the builds
    already IN FLIGHT.

    In-flight is the half that matters. The 9th-July twin was queued while its original was
    already running in a lane- the pump had popped it off the queue- so a pending-only check
    would have waved it straight through. Never raises: lane_journals() yields entry=None for
    a journal caught mid-write, and a transient read must not take down the queue write it is
    supposed to be guarding."""
    out = list(pending if pending is not None else queue_read())
    try:
        for _rf, e in lane_journals(alive_only=True):
            if e:
                out.append(e)
    except Exception:
        pass
    return out


class DuplicateTask(Exception):
    """A near-duplicate of a pending or in-flight entry. Carries the colliding entry, because
    a refusal that will not say WHAT it collided with cannot be acted on."""

    def __init__(self, colliding):
        self.colliding = colliding or {}
        self.colliding_id = str(self.colliding.get("id", "") or "")
        self.colliding_task = str(self.colliding.get("task", "") or "")
        super().__init__(f"near-duplicate of {self.colliding_id} ({self.colliding_task[:60]})")


class MissingPRD(Exception):
    """A big task filed with no document behind it (the owner, 9th July: "a rigorous prd before it
    gets filed"). Carries the note it was given, because a refusal that will not say WHY the
    path it was handed is not a PRD cannot be acted on.

    The queue's failure mode is under-specification: an entry is a task string and a next-step
    string, written by whichever Baxter happened to be reading Discord. `TOUCH_PLACEHOLDERS`
    and `HUB_FILES` both exist because entries were filed badly, and both fire at --queue time,
    long after the entry was written. This one refuses the entry itself."""

    def __init__(self, note=""):
        self.note = str(note or "")
        super().__init__(f"{self.note} is not a document inside the PRD store" if self.note
                         else "no PRD: the task was filed as a bare string")


# ---- A REFUSED ASK GOES BACK TO THE PM, NOT INTO A BIN (the owner, 10th July) ----
# "I don't want a PRD ever parked. I want it to be sent back to the PM every single time with
#  feedback on how to improve it. This should cause a loop until the PRD is always sufficiently
#  built. Always success."
#
# The loop he describes already lives inside baxter_pm_delegate: a PM Opus writes the form, a
# machine validator checks its shape, a SECOND Opus reads it as manager and answers greenlight /
# changes / reject, and the PM amends its own document against those reasons. Three attempts,
# with a stall guard for a PM that hands back what it was asked to change.
#
# What never existed is the wire between `--queue`'s refusal and that loop. A bare ask was
# refused, logged, and destroyed- fourteen of his in one morning. So: a PRD-less ask is now
# WRITTEN DOWN FIRST (it can never be lost again) and handed straight to the PM. The entry sits
# gated on 'pm', which no pump will touch, for exactly as long as the PM takes. `prd_sweep()`
# rides the governor beat: it lifts the gate the moment a real PRD stands behind the entry, and
# re-spawns a PM for any ask still waiting on one.
#
# It is BOUNDED, and it has to be. An unbounded retry against a manager that keeps saying
# `reject` is an unbounded spend of the owner's usage on a build his own reviewer says should not
# exist. After PRD_MAX_ROUNDS the ask stays queued, stays visible, and he is told- which is the
# one outcome that is neither silent loss nor runaway cost.
PRD_GATE = "pm"          # gated_on value while the PM drafts. Not a GATE_NONE- the pump can't run it
PRD_MAX_ROUNDS = 3       # outer re-spawns. Each spawn is itself up to MAX_ATTEMPTS PM<->manager rounds
PM_DELEGATE = Path(os.path.dirname(os.path.abspath(__file__))) / "baxter_pm_delegate.py"


def _pm_running():
    """Is a PM delegate already drafting? One at a time: a PM round-trip is minutes of Opus."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
             "'baxter_pm_delegate\\.py' }).Count"],
            capture_output=True, text=True, timeout=30)
        return int((out.stdout or "0").strip() or 0) > 0
    except Exception:
        return True          # unreadable census -> assume busy. Never spawn a second PM blind.


def _spawn_pm_for(entry):
    """Hand one queued ask to the PM delegate, detached. Its --queue-it re-files the SAME entry
    (enqueue dedups on task text) carrying --note <prd>, and the next sweep lifts the gate."""
    argv = [sys.executable, str(PM_DELEGATE), "--ask", str(entry.get("task") or ""), "--queue-it",
            "--priority", str(int(entry.get("priority") or PRIO_DEFAULT))]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x08000000      # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, **kw)


def prd_sweep():
    """The governor beat's PRD pass. Lift the gate off every ask a PRD now stands behind, and
    push the oldest ask that still lacks one back to the PM. Never raises: it rides enforce()."""
    # BAXTER_NO_PM_SWEEP suppresses the SPAWN, never the RELEASE. Gating the whole function on it
    # (as this first shipped) meant an exam could write a PRD, watch the gate stay shut, and call
    # that correct- the release is the half that must always work.
    released, spawned = 0, 0
    with queue_txn():
        q = queue_read()
        waiting = []
        for e in q:
            if gate_of(e) != PRD_GATE:
                continue
            if prd_backed(e.get("note_path")):
                e["gated_on"] = ""                   # the document exists: it is a build now
                released += 1
                _log(f"PRD landed- gate lifted on [{e.get('id')}]: {str(e.get('task'))[:60]}")
            else:
                waiting.append(e)
        if released:
            queue_write(q)
    # PAUSED BY FILE, not only by env var. The watcher and its triage children are long-lived
    # processes started from a shell whose environment the owner cannot reach afterwards, so an env
    # var can only pause a sweep at the NEXT relaunch. A flag in the vault pauses it this beat.
    # (the owner, 10th July: "I will come back to you later about the PM sweep when my usage resets.")
    if not waiting or os.environ.get("BAXTER_NO_PM_SWEEP") or (VAULT / ".baxter_no_pm_sweep").exists():
        return released, spawned

    # THE GOVERNOR BINDS THE PM TOO (10th July). As first written this function read no band at
    # all, and a PM is not something the governor can clean up after: the watcher's kill matches
    # `claude.exe` whose command line carries 'big-task' or 'BAXTER_TRIAGE', and a PM's Opus
    # carries neither. So an unbanded sweep would have spawned PRD after PRD straight THROUGH the
    # 80% big stop and the 90% vital-only wall, with nothing able to stop it but the owner noticing.
    #
    # Spawning stops at the DRAIN (60%), not at the big stop: a PM round-trip is ~8 minutes of
    # two Opus instances, so starting one at 79% commits spend that lands well past 80. The drain
    # already means "open no new expensive thing"- a PM is exactly that. RELEASES are untouched:
    # lifting the gate off an ask whose PRD already exists costs nothing and must never pause.
    stopped, why = soft_stopped()
    if stopped:
        _log(f"PRD sweep held: {why}")
        return released, spawned
    is_blocked, reason = blocked("big")
    if is_blocked:
        _log(f"PRD sweep held: {reason}")
        return released, spawned
    if _pm_running():
        return released, spawned
    # Fewest rounds, then PRIORITY, then age. Priority leads age on purpose: a PM round-trip is
    # ~8 minutes of two Opus, and at that rate a window drafts only a handful- so a p2 ask the owner
    # is waiting on must not sit behind a dozen p9 background tickets that merely arrived earlier.
    # prd_rounds still leads everything, so a hard-to-spec ask cycles to the back rather than
    # monopolising the PM every beat.
    waiting.sort(key=lambda e: (int(e.get("prd_rounds") or 0), _prio_int(e),
                                str(e.get("queued_at") or "")))
    e = waiting[0]
    rounds = int(e.get("prd_rounds") or 0)
    if rounds >= PRD_MAX_ROUNDS:
        return released, spawned                     # bounded: it stays queued, visible, and his
    try:
        _spawn_pm_for(e)
        spawned += 1
        with queue_txn():
            q = queue_read()
            for x in q:
                if x.get("id") == e.get("id"):
                    x["prd_rounds"] = rounds + 1
            queue_write(q)
        _log(f"PM drafting a PRD for [{e.get('id')}] (round {rounds + 1}/{PRD_MAX_ROUNDS}): "
             f"{str(e.get('task'))[:60]}")
    except Exception as exc:
        _log(f"PM spawn failed for [{e.get('id')}]: {exc.__class__.__name__}: {exc}")
    return released, spawned


def prd_backed(note):
    """True when `note` names an EXISTING document inside `PRD_DIR`.

    Both sides are resolved before they are compared, so `60-PRDs/../60-PRDs/x.md` and a
    lower-cased drive letter both land where they belong- and a note that merely mentions
    the store in its text ("about 60-PRDs/") is not mistaken for one that lives in it.

    Existence is part of the claim, not a nicety: `--note` takes any string, and a PM that
    names a document it never wrote has documented nothing. PRD_DIR is read through the
    module global on every call, which is what lets an exam point the store at a tempdir."""
    if not note:
        return False
    try:
        doc = Path(str(note)).expanduser().resolve()
        root = Path(PRD_DIR).expanduser().resolve()
    except (OSError, ValueError):
        return False
    return root in doc.parents and doc.is_file()


def _prov(v):
    return str(v or "").strip().lower()


def _apply_exam(e, verify, verify_assert, acceptance_sealed, verify_by):
    """Write an acceptance exam onto a queue entry, provenance first.

    `verify_by` is CARRIED, never invented. Nothing in this module may stamp 'planner' on an
    exam: that word means "written by the planner tier, above the builder being graded", and
    the only place it is minted is baxter_orch.seal_acceptance. An enqueue that could type it
    would let any caller launder its own exam into the slot the planner tier exists to hold.

    And on the UPDATE branch a builder-authored exam never overwrites a planner-sealed one.
    A build that halts twice re-enters through here: without this line, attempt two's soft
    `--verify-cmd` quietly replaces the exam attempt one was actually set."""
    if _prov(e.get("verify_by")) == "planner" and _prov(verify_by) != "planner":
        return                                  # the planner's seal stands
    if verify: e["verify"] = str(verify)
    if verify_assert: e["verify_assert"] = str(verify_assert)
    if acceptance_sealed: e["acceptance_sealed"] = str(acceptance_sealed)
    if verify_by: e["verify_by"] = _prov(verify_by)


def _downgrade_provenance(e, verify_by):
    """The seal follows the COMMAND, not the entry. Whoever rewrites an exam owns it.

    `_apply_exam` guards the enqueue path; `queue_edit` walked round it. It wrote a new
    `verify` straight onto the entry and left `verify_by: "planner"` standing above it, so
    `orch._authored_above()` read a command the BUILDER had picked as an exam handed down from
    the planner tier, and the planner never re-sealed. Proven 9th July on a scratch queue.

    So a caller that cannot prove planner provenance has its rewrite stamped 'builder'.

    STAMPED, not cleared, and stamped with that exact word. `_authored_above` refuses the
    literal string 'builder' and trusts everything else- an absent `verify_by`, or an honest
    'edited', reads as authored-from-above just as loudly as a forged 'planner' does. The
    downgrade has to land on the one token the reader downstream actually rejects."""
    if _prov(verify_by) == "planner":
        return                                  # a proven planner keeps its seal
    e["verify_by"] = "builder"


def enqueue(task, next_step, note="", state_summary="", priority=PRIO_DEFAULT, touch_set=None,
            gated_on=None, verify=None, verify_assert=None, source_mid=None,
            source_channel=None, transient_retries=None, repair_attempts=None, dedup=False,
            acceptance_sealed=None, verify_by=None, vet=True, prd_required=False, solo=False):
    """Add a big task to the queue. RETURNS the entry, so its caller can state the position.

    `vet=True` puts the touch-set through `vet_touch()` HERE, at the write, rather than in
    `main()`- so `baxter_autobuild.notice()` and every direct import are held to the same
    declaration the CLI demands. It raises `BadTouchSet` before the transaction opens: a
    refused entry must not reach the queue file, and must not hold the lock while it fails.

    `vet=False` is for a caller re-stating a touch-set the queue ALREADY accepted- `halt()`
    and `baxter_triage._park()`. Validating those would let a legacy entry (3f89d826's bare
    hub file) be refused re-entry and vanish from the queue on its way through a halt, which
    is a lost build to fix a cosmetic declaration. Re-queued work fails safe by serialising.

    `source_mid`- the Discord message id the task came from (the owner, 9th July)- is a stronger
    identity than the task text, and is matched FIRST. The fast lane writes a placeholder
    carrying his raw sentence the moment he asks; triage later refines the prose. Keyed on
    the text alone that refinement forks a duplicate beside the placeholder. Keyed on the
    message id it upgrades the entry IN PLACE, which is what he asked for.

    `source_channel`- the channel that message arrived in (10th July). A task remembers WHERE
    it was asked, so the lane that finishes it answers THERE. Without it a finished build
    replied through baxter_say's `general` default whatever channel the owner used, and on 9th July
    the shred answer he asked for in #deadlock-research landed in #general. PRESERVED across a
    halt+re-queue exactly like the touch-set: passing nothing keeps the channel already known,
    so a halting build never forfeits the room it must answer in.

    Same task text already queued = UPDATE it
    (a build that halts repeatedly must not pile up duplicates); priority keeps
    the more urgent of the two. `touch_set` (files/dirs + @cluster tags the task
    will edit) is what lets the delegator co-schedule it on a second lane- an
    entry without one runs solo. On update an existing touch_set is PRESERVED
    unless a new one is given, so a halt+re-queue never silently forfeits its lane.
    `gated_on="owner"` parks it until he says go; pass "" to lift the gate. On update
    an existing gate is PRESERVED unless gated_on is given explicitly- a halt must
    never quietly un-gate the task it is re-queueing.

    `verify` (a shell command) or `verify_assert` (a claim a separate checker must
    prove) is how a lane knows the build actually worked. baxter_verify runs it at the
    lane's exit, BEFORE any success is announced, and its verdict overrides whatever the
    builder claimed. Neither declared = the build lands as 'unverified', never 'done'.
    Both are PRESERVED across a halt+re-queue, exactly like the touch-set- as are
    `acceptance_sealed` (which kind was sealed) and `verify_by` (WHO sealed it). The
    provenance travels with the exam or the exam is worthless: see `_apply_exam`.

    Priority here is PROMOTE-ONLY (`min`) and stays that way on purpose: halt() re-queues
    through this path, and a build that halts twice must never demote itself out of the
    resume band. Deliberate demotion is `queue_edit(..., priority=N)`, which sets it flat."""
    # BEFORE the lock, and before any write: a refusal must cost the queue nothing.
    #
    # THE PRD GATE (the owner, 9th July 09:47). `prd_required` is OFF by default, and that is
    # structural rather than timid- exactly as `dedup` is. halt(), baxter_triage._park,
    # baxter_fast's placeholder and baxter_autobuild.notice() each re-state work whose
    # document, if it ever had one, was written once already; a gate there would strand a
    # halted build outside its own queue. `--queue` is the boundary a FRESH ask crosses,
    # and it is the caller that opts in. A dunder fixture names no build and is exempt.
    if prd_required and not _is_fixture(task) and not prd_backed(note):
        raise MissingPRD(note)
    if vet and touch_set:
        for w in vet_touch(touch_set):
            _log(f"touch-set note ({str(task)[:40]}): {w}")
    with queue_txn():
        q = queue_read()
        now = datetime.now().isoformat(timespec="seconds")
        mid = str(source_mid).strip() if source_mid else ""
        cid = str(source_channel).strip() if source_channel else ""
        # Identity, strongest first. A message id is exact; the task text is prose, and
        # prose gets rewritten. Matching text first would let a refinement of the SAME ask
        # miss its own placeholder and fork a twin.
        existing = (next((e for e in q if mid and str(e.get("source_mid") or "") == mid), None)
                    or next((e for e in q if e.get("task") == task), None))
        if existing is not None:
            e = existing
            e["task"] = task or e.get("task", "")
            try:
                from utils import baxter_name
            except ImportError:
                try:
                    import baxter_name
                except ImportError:    # path-loaded: no utils anywhere on sys.path
                    baxter_name = _sibling("baxter_name")
            e["name"] = baxter_name.name_for(e["task"])
            e["next_step"] = next_step or e.get("next_step", "")
            if note: e["note_path"] = note
            if state_summary: e["state_summary"] = state_summary
            if touch_set:
                e["touch_set"] = list(touch_set)
                e["solo"] = False        # a real declaration supersedes the board-wide lock
            elif solo:
                e["solo"] = True
            if gated_on is not None: e["gated_on"] = str(gated_on).strip().lower()
            _apply_exam(e, verify, verify_assert, acceptance_sealed, verify_by)
            if mid: e["source_mid"] = mid
            if cid: e["source_channel"] = cid
            _carry_counters(e, transient_retries, repair_attempts)
            # The min is taken against the ANCHOR, never against the derived `priority`.
            # Against the derived number an age-promoted entry (base 8, sunk to 5 by four
            # days' wait) would re-anchor at 5 and ratchet itself to the top a band per
            # re-queue; a gated entry sunk to 9 would re-anchor at 9. Writing the base is
            # also what stops the next 15-min beat re-deriving halt()'s p2 back to p5.
            new_base = min(int(e.get("base_priority") if e.get("base_priority") is not None
                               else e.get("priority", PRIO_DEFAULT)), int(priority))
            e["base_priority"] = new_base
            e["priority"] = new_base            # the next beat re-derives it
            e["queued_at"] = e.get("queued_at") or now
            queue_write(q)
            _log(f"queue updated (p{e['priority']}{', gated on ' + gate_of(e) if is_human_gated(e) else ''}): {task[:60]}")
            _announce_position(e)
            return e
        # SEMANTIC DEDUP. Fresh entries only, and strictly BELOW the source_mid/exact-text
        # identity match above: a triage refinement of an existing ask must UPDATE its row,
        # and run any earlier it would be refused for resembling the very entry it refines.
        #
        # `dedup` is OFF by default, and that is structural rather than timid. enqueue() is
        # also called by halt(), baxter_triage._park, baxter_fast's placeholder write and
        # baxter_autobuild- every one of which legitimately re-states a task whose own lane
        # journal is still alive, and so would collide with itself. An exemption LIST is a
        # thing you forget to extend (_park and baxter_fast were both nearly missed); a
        # default-off cannot be forgotten. `--queue` is the boundary where a fresh human or
        # agent ask enters, and it is the one caller that opts IN.
        if dedup and not _is_fixture(task):
            hit = _near_duplicate(task, _dup_candidates(q))
            if hit is not None:
                raise DuplicateTask(hit)
        try:
            from utils import baxter_name
        except ImportError:
            try:
                import baxter_name
            except ImportError:        # path-loaded: no utils anywhere on sys.path
                baxter_name = _sibling("baxter_name")
        fresh = {"id": _new_qid(), "name": baxter_name.name_for(task),
                 "task": task, "note_path": note, "state_summary": state_summary,
                 "next_step": next_step, "priority": int(priority),
                 "base_priority": int(priority), "queued_at": now,
                 "touch_set": list(touch_set or []),
                 # Declared solo, or merely not scoped yet? Never inferred from the absence of a
                 # touch-set again: the two need opposite locks. See SOLO_LOCK / UNSCOPED_TAG.
                 "solo": bool(solo) and not touch_set,
                 "gated_on": str(gated_on or "").strip().lower()}
        if mid: fresh["source_mid"] = mid
        if cid: fresh["source_channel"] = cid
        _apply_exam(fresh, verify, verify_assert, acceptance_sealed, verify_by)
        _carry_counters(fresh, transient_retries, repair_attempts)
        q.append(fresh)
        queue_write(q)
        _log(f"queued (p{int(priority)}, {len(q)} deep{', gated on ' + str(gated_on).lower() if gated_on else ''}): {task[:60]}")
        _announce_position(fresh)
        return fresh

def _carry_counters(e, transient_retries, repair_attempts):
    """A build's retry + repair budget lives on its journal, and maybe_resume writes the
    queue entry out AS the next journal- so the only way a counter survives a halt is to
    ride the queue entry. Governor holds re-queue through here (baxter_triage._hold_for_governor):
    without this, every 80% crossing hands a held build a fresh repair budget, and a task
    that genuinely is broken never reaches MAX_REPAIRS and never parks.
    MAX, never overwrite- a build that halts twice must not walk its own counters back."""
    for k, v in (("transient_retries", transient_retries), ("repair_attempts", repair_attempts)):
        if v is None:
            continue
        try:
            e[k] = max(int(e.get(k, 0) or 0), int(v))
        except Exception:
            pass

def _lift_exam(task, qid=None):
    """The acceptance exam sitting on the LIVE lane journal for this task, as
    (verify, verify_assert, acceptance_sealed, verify_by). Four Nones when there is no
    unambiguous journal to lift from.

    WHY THE JOURNAL IS THE ONLY SOURCE. The pump POPS an entry out of the queue file when it
    hands it to a lane, so while a build runs, its journal holds the only copy of its sealed
    exam. enqueue() therefore cannot find the old row to carry it off- it forks a fresh entry
    with the exam gone (reproduced 9th July; observed live on 0b00e1b4). ensure_plan re-seals
    on resume, so nothing lands unverified, but the resumed build is graded against a NEW,
    possibly softer exam than the one it was set. Lifting it here is what makes a seal durable.

    Matched on `id` first and the EXACT task text second, never on a substring. Ambiguous
    (two lanes, one prose) or absent = carry NOTHING. Losing an exam costs a planner spawn on
    resume; carrying the WRONG one grades a build against a test it was never given, and a
    soft exam laundered off a neighbouring lane is exactly the failure this guards."""
    none4 = (None, None, None, None)
    want_id, text = str(qid or "").strip(), str(task or "").strip()
    try:
        js = [e for _rf, e in lane_journals(alive_only=False) if isinstance(e, dict)]
    except Exception as ex:
        _log(f"halt: cannot read the lane journals ({ex})- carrying no exam")
        return none4
    hits = [e for e in js if want_id and str(e.get("id") or "").strip() == want_id]
    if len(hits) != 1:                                  # no id, or an id that named nothing
        hits = [e for e in js if text and str(e.get("task") or "").strip() == text]
    if len(hits) != 1:
        if hits:
            _log(f"halt: {len(hits)} journals carry this exact task- carrying NO exam rather "
                 f"than the wrong lane's: {text[:50]}")
        return none4
    e = hits[0]
    if not (str(e.get("verify") or "").strip() or str(e.get("verify_assert") or "").strip()):
        return none4
    return (e.get("verify"), e.get("verify_assert"), e.get("acceptance_sealed"), e.get("verify_by"))


def halt(task, next_step, note="", state_summary="", touch_set=None,
         transient_retries=None, repair_attempts=None,
         verify=None, verify_assert=None, acceptance_sealed=None, verify_by=None, qid=None,
         gate=None):
    """Record interrupted work- it re-enters the queue at resume priority and
    triage restarts it (top of queue, next free lane) once slot + curve allow.

    THE EXAM RIDES ALONG. Left to itself the re-queued entry loses the sealed acceptance test
    the halted lane was carrying, so pass it, or let `_lift_exam` read it off the lane journal.
    Its provenance rides with it verbatim: a builder-authored exam arrives back stamped
    'builder', so `baxter_orch._authored_above` still refuses it and the planner seals over it
    on resume. Laundering a soft exam into a planner slot by surviving a halt is the one thing
    this must not enable.

    Every kwarg here is OPTIONAL and stays that way. baxter_triage wraps its halt call in
    `except TypeError` and, on a signature it cannot satisfy, KEEPS the journal instead of
    re-queueing- so a new required argument would silently stop governor holds re-queueing at all.

    `dedup=False` is stated out loud even though it is the default: a halting build re-queues
    a task whose OWN journal is still alive and heartbeating, so it is a guaranteed
    near-duplicate of itself. Turn the default on and this line is the one that silently
    destroys a build mid-flight."""
    if verify is None and verify_assert is None and acceptance_sealed is None and verify_by is None:
        verify, verify_assert, acceptance_sealed, verify_by = _lift_exam(task, qid)
    # `vet=False` states out loud what HUB_FILES has always documented: a build re-queueing
    # its own mid-flight touch-set is never refused. Vetting here would let an entry queued
    # before the guard existed- 3f89d826's bare `utils/baxter_watch.ps1`- raise on its way
    # through a halt, and halt()'s callers treat a raise as "keep the journal, drop the
    # re-queue". A cosmetic declaration would cost a whole build. Resumed work serialises.
    # THE HUMAN GATE RIDES ALONG TOO (10th July). enqueue() reaches its FRESH branch from
    # here- the pump POPPED the entry when it handed it to a lane- and a fresh entry stamps
    # `gated_on: ""` from enqueue's default. A build that was gated (an empty placeholder
    # gated on `pm` mid-PRD, or an entry gated on `owner`) would come back UNGATED, and the
    # pump would grab it: two lanes have scoped one empty ask this way. Recover the gate off
    # the same live journal on the same terms as the exam and the id- id first, EXACT task
    # text second, an ambiguous OR absent match carries NOTHING (a neighbour's gate is never
    # laundered on). Read it through gate_of() so a null/`none` journal reads as ungated,
    # not a crash. Only a real, non-empty gate is carried; otherwise gated_on stays None, so
    # enqueue's UPDATE branch leaves any gate already on a queued copy untouched. Passing ""
    # would WIPE that gate on a halt-twice update- the gate analogue of the exam's E4.
    # An explicit gate from the caller (the `--halt ... --gate pm` CLI, parsed by
    # `_gate_arg`) is the AUTHORITY and OVERRIDES the journal recovery below. The scope
    # lane's own journal carries the placeholder's ORIGINAL ungated state, never the `pm`
    # the scope pass just decided- only the flag can supply it, so trusting the journal
    # could never produce it. `gate is None` means "not passed"- fall through to the
    # journal recovery exactly as today. An explicit `""` (`--gate none`) LIFTS and stays
    # DISTINCT from None end to end, so an accidental journal miss never wipes a gate a
    # queued twin legitimately holds (the gate analogue of the exam's E4).
    if gate is not None:
        gated_on = gate
    else:
        gated_on = None
        try:
            gjs = [j for _rf, j in lane_journals(alive_only=False) if isinstance(j, dict)]
        except Exception:
            gjs = []
        _gid, _gtext = str(qid or "").strip(), str(task or "").strip()
        ghits = [j for j in gjs if _gid and str(j.get("id") or "").strip() == _gid]
        if len(ghits) != 1:
            ghits = [j for j in gjs if _gtext and str(j.get("task") or "").strip() == _gtext]
        if len(ghits) == 1:
            gated_on = gate_of(ghits[0]) or None
    e = enqueue(task, next_step, note, state_summary, priority=PRIO_RESUME, touch_set=touch_set,
                transient_retries=transient_retries, repair_attempts=repair_attempts, dedup=False,
                verify=verify, verify_assert=verify_assert, gated_on=gated_on,
                acceptance_sealed=acceptance_sealed, verify_by=verify_by, vet=False)
    kind = f"{_prov(verify_by) or 'unattributed'} exam carried" if (verify or verify_assert) else "no exam to carry"
    gk = f", gated on {gated_on}" if gated_on else ""
    _log(f"halt recorded ({kind}{gk}): {task[:60]}")

    # THE ID RIDES ALONG TOO (9th July). The pump POPS the entry when it hands it to a lane,
    # so enqueue() finds no row to update and forks a FRESH uuid. The halted task then
    # re-enters the queue wearing a stranger's name: every id-keyed reference to it- a sealed
    # exam auditing the queue, `--edit <id>`, a plan doc, a note- silently stops resolving,
    # and the entry reads as DROPPED. Two of the ten hub-region re-declarations were lost
    # exactly this way at 19:32, within three minutes of being fixed.
    #
    # `qid` is the halted entry's own id. Callers that have it pass it; `--halt` does not, so
    # it is recovered from the live journal on the same terms `_lift_exam` uses- id first,
    # then EXACT task text, and an ambiguous match carries nothing. It is only ever written
    # back onto a row that enqueue just created or updated, and never over an id another
    # entry already holds: minting a duplicate would have queue_read() re-mint one of them
    # on the next pass, which is the bug again with an extra step.
    want = str(qid or "").strip()
    if not want:
        try:
            js = [j for _rf, j in lane_journals(alive_only=False) if isinstance(j, dict)]
        except Exception:
            js = []
        text = str(task or "").strip()
        hits = [j for j in js if text and str(j.get("task") or "").strip() == text]
        want = str(hits[0].get("id") or "").strip() if len(hits) == 1 else ""
    got = str((e or {}).get("id") or "").strip()
    if want and got and want != got:
        with queue_txn():
            q = queue_read()
            if any(str(r.get("id") or "").strip() == want for r in q):
                _log(f"halt: id {want} is already held by another entry- {got} keeps its own")
            else:
                for r in q:
                    if str(r.get("id") or "").strip() == got:
                        r["id"] = want
                        queue_write(q)
                        e["id"] = want
                        _log(f"halt: restored the halted entry's own id {want} (enqueue minted {got})")
                        break
    return e


# ---- THE EDIT SURFACE (the owner, 9th July 09:21: "is editing things in build queue hard?") ----
# It was, structurally. The queue had three verbs- add, promote, ungate- and no way to
# reword an entry, push one DOWN the order, or remove one. Rewording meant re-running
# --queue with new text, and enqueue() keys on the task string, so a one-character change
# forked a duplicate beside the original instead of editing it. The only real route was to
# hand-edit the json, which races the pump. Editing PRDs is a mandatory task, so the tool
# needed a mouth for it.
#
# Entries are now addressed by a stable `id` (uuid at enqueue, backfilled on read) or by an
# UNAMBIGUOUS substring of their text- never by exact prose. An ambiguous needle is refused
# outright rather than resolved by guessing: silently editing the wrong build is the one
# failure mode worse than not editing at all.
FIXTURE_TASK_GATE = "test"


def _is_fixture(task):
    """A dunder-wrapped task name (`__editverify_throwaway__`) is a TEST FIXTURE, not a
    build: a verify command needs to round-trip a real entry through the real queue file.
    Fixtures skip the mandatory-touch refusal (they name no files because they edit none)
    and are force-gated, so the pump can never hand one a lane. No real build is named
    like this, and the exemption has to be typed to be taken."""
    t = str(task).strip()
    return len(t) > 4 and t.startswith("__") and t.endswith("__")


def _live_lane_of(entry):
    """The lane label of a build currently RUNNING this queue entry, or None.

    The pump POPS an entry off the queue when it hands it to a lane, so the two normally
    cannot both be true. They can when a lane halts and re-queues while its worker is
    still winding down- and an edit landing in that window is lost the moment the lane
    writes its own state back. Matched on id, else on the exact task text (a journal is a
    verbatim copy of the entry), never on a loose substring: a false positive here refuses
    a legitimate edit."""
    qid = str(entry.get("id", "")).strip().lower()
    task = str(entry.get("task", "")).strip()
    for rf, e in lane_journals():
        if not e:
            continue
        if (qid and qid == str(e.get("id", "")).strip().lower()) or \
           (task and task == str(e.get("task", "")).strip()):
            return lane_label(_lane_id(rf))
    return None


_HEXDIGITS = frozenset("0123456789abcdef")


def _is_id_shaped(needle):
    """Does this needle look like a queue id rather than prose?

    An id is 8 hex chars (`uuid4().hex[:8]`), and a prefix of one is a legitimate handle, so
    the shape starts at `pure hex, >= 4 chars`. That rule ALONE would swallow the ordinary
    English words that happen to be hex- cafe, deed, beef, face, feed- and turn `--edit cafe`
    into an id lookup that can never hit. Requiring a DIGIT, or the full 8-char length,
    separates a real handle from a word. An 8-letter all-hex word ('deadbeef') is read as an
    id; address that task by a longer phrase.
    """
    n = str(needle).strip().lower()
    if len(n) < 4 or not set(n) <= _HEXDIGITS:
        return False
    return len(n) == 8 or any(c.isdigit() for c in n)


def queue_match(q, needle, want_how=False):
    """(entry, error), or (entry, error, how) when want_how- `how` being the tier that
    resolved it, so a caller about to DESTROY something can see what it actually matched.

    Resolve a needle against the queue by id, then id-prefix, then exact text, then
    case-insensitive substring- taking the first tier that hits, and refusing a tier that
    hits more than once.

    AN ID-SHAPED NEEDLE NEVER REACHES THE TEXT TIERS (9th July, 13:12). `--drop 3af6a43a`,
    on an id a concurrent worker had already removed, found nothing in the id tiers, fell
    through to substring, and matched that id where it sat inside ANOTHER task's BODY- the
    surviving task cited the dead one's id as its own provenance. It deleted the survivor
    and printed 'dropped [179f667e]'. Citing a sibling's id in your task text is a normal,
    useful thing to do and must never make you a deletion target. So: if it looks like an id
    and matches no id, it is a typo or a stale id, and the answer is an error- never a
    different task. `want_how` is keyword-only in effect: queue_edit and queue_move unpack
    `e, err =` and the selftests index `[0]`, so the default arity is a contract.
    """
    n = str(needle).strip()
    if not n:
        return (None, "empty needle", None) if want_how else (None, "empty needle")
    low = n.lower()
    id_shaped = _is_id_shaped(low)
    tiers = [
        ("id", [e for e in q if str(e.get("id", "")).lower() == low]),
        ("id prefix", [e for e in q if len(low) >= 4 and str(e.get("id", "")).lower().startswith(low)]),
    ]
    if not id_shaped:
        tiers += [
            ("exact text", [e for e in q if str(e.get("task", "")) == n]),
            ("substring", [e for e in q if low in str(e.get("task", "")).lower()]),
        ]

    def _out(entry, err, how):
        return (entry, err, how) if want_how else (entry, err)

    for how, hits in tiers:
        if len(hits) == 1:
            return _out(hits[0], None, how)
        if len(hits) > 1:
            listing = "\n".join(f"    [{e.get('id', '?')}] p{e.get('priority', PRIO_DEFAULT)} "
                                f"{str(e.get('task', '?'))[:64]}" for e in hits)
            return _out(None, f"{needle!r} matches {len(hits)} entries by {how}- name one by its id:\n{listing}", None)
    if id_shaped:
        return _out(None, f"no queue entry matches {needle!r} (id-shaped needle: not matched against "
                          f"task text- it is mistyped, or the entry is already gone)", None)
    return _out(None, f"no queue entry matches {needle!r}", None)


# Which --edit flag writes which field. `retext` is deliberately not called `task`: the
# needle is matched BEFORE anything is written, so an entry can be renamed to a string
# that would no longer match it.
_EDIT_FIELDS = {"retext": "task", "priority": "priority", "touch_set": "touch_set",
                "verify": "verify", "verify_assert": "verify_assert",
                "note": "note_path", "next_step": "next_step",
                "state_summary": "state_summary", "gated_on": "gated_on",
                # `solo` is editable for the same reason `gated_on` is: it is a decision a
                # human or a scoping lane makes ABOUT a task, not a property of its prose.
                "solo": "solo"}


def queue_edit(needle, force=False, *, verify_by=None, vet=True, **fields):
    """Change one queued entry in place. Returns (ok, message, changes) where `changes`
    is [(field, before, after)]- the before/after diff the caller prints.

    A `touch_set` given here is vetted exactly as `enqueue()` vets one, and for the same
    reason: this is the OTHER function that writes the field, so a guard on only one of them
    is a guard on neither. It RAISES `BadTouchSet` rather than returning `(False, msg, [])`-
    the refusal has to be as loud for a library caller as `--edit` makes it for the owner, and a
    (False, ...) tuple is exactly the return value a caller forgets to read.

    Priority moves BOTH ways here (a flat set, not enqueue's promote-only `min`)- that is
    the whole point of the verb. Two edits are refused: one to a task that is live in a
    lane, and a priority change to a p1 entry, which is the owner's own "do this first" pin and
    must not be quietly undone by a passing worker. `force=True` lifts the p1 refusal.

    `verify_by` is the caller's CLAIMED provenance, not an editable field- hence keyword-only
    and absent from `_EDIT_FIELDS`. Rewriting `verify`/`verify_assert` re-authors the exam, so
    unless the caller proves 'planner' the entry is stamped 'builder' and the planner re-seals
    it on resume. There is deliberately NO `--verify-by` CLI flag: a command-line door here
    would hand the laundering back to every builder that can shell out."""
    given = {k: v for k, v in fields.items() if v is not None and k in _EDIT_FIELDS}
    if not given:
        return False, "nothing to change- pass at least one of " + \
                      "--retext/--priority/--touch/--verify/--verify-assert/--note/--next/--state/--gate", []
    # BEFORE the lock, and before any write: a refusal must cost the queue nothing.
    if vet and given.get("touch_set"):
        for w in vet_touch(given["touch_set"]):
            _log(f"touch-set note (edit {str(needle)[:40]}): {w}")
    with queue_txn():
        q = queue_read()
        e, err = queue_match(q, needle)
        if err:
            return False, "refused: " + err, []
        lane = _live_lane_of(e)
        if lane:
            return False, (f"refused: that task is LIVE in lane {lane}. Editing its queue entry "
                           f"changes nothing about the running build and is lost when it halts "
                           f"and re-queues. Let it land, or halt the lane first."), []
        if "priority" in given and int(e.get("priority", PRIO_DEFAULT)) == PRIO_URGENT and not force:
            return False, (f"refused: [{e.get('id')}] is pinned at priority 1- the owner's own "
                           f"'do this first'. Re-prioritising it needs --force."), []
        changes = []
        for k, v in given.items():
            key = _EDIT_FIELDS[k]
            before = e.get(key)
            if key == "priority":
                after = int(v)
            elif key == "touch_set":
                after = list(v)
            elif key == "solo":
                after = bool(v)
            else:
                after = str(v)
            if key == "gated_on":
                after = after.strip().lower()
            if before == after:
                continue
            e[key] = after
            changes.append((key, before, after))
        # A REAL DECLARATION ENDS SOLO. The whole point of scoping an unscopable task is that it
        # stops needing the board; leaving `solo: true` beside a fresh touch-set would keep the
        # board-wide lock and make the scoping pass pointless.
        if "touch_set" in given and list(given["touch_set"]) and e.get("solo"):
            e["solo"] = False
            changes.append(("solo", True, False))
        if not changes:
            return True, f"[{e.get('id')}] already matches- nothing changed", []
        if any(k in ("verify", "verify_assert") for k, _b, _a in changes):
            # The exam was re-authored. Provenance follows the command, and `acceptance_sealed`
            # must describe what the entry now actually HOLDS- a verify rewritten to a claim
            # leaves 'cmd' behind, and the runner then looks for a command that is gone.
            # Read the stamp back off the entry rather than assuming: with the downgrade
            # neutered, this appends nothing and the exam sees a seal that never moved.
            was_by = e.get("verify_by")
            _downgrade_provenance(e, verify_by)
            if e.get("verify_by") != was_by:
                changes.append(("verify_by", was_by, e.get("verify_by")))
            was_sealed = e.get("acceptance_sealed")
            if str(e.get("verify") or "").strip():
                e["acceptance_sealed"] = "cmd"
            elif str(e.get("verify_assert") or "").strip():
                e["acceptance_sealed"] = "claim"
            else:
                e.pop("acceptance_sealed", None)
            if e.get("acceptance_sealed") != was_sealed:
                changes.append(("acceptance_sealed", was_sealed, e.get("acceptance_sealed")))
        if any(k == "priority" for k, _b, _a in changes):
            # A `rank` is a slot within ONE band. Carried into a new band it would jump the
            # entry ahead of every unranked task there, on the strength of a placement the owner
            # made somewhere else. Re-prioritising re-enters it at the back, FIFO by queued_at.
            e.pop("rank", None)
            # Re-anchor. This edit is a DELIBERATE priority write- his, not the scorer's- and
            # the 15-min beat re-derives `priority` from the anchor. Without this line his
            # demotion is undone within the quarter-hour, and a --force'd demotion of a p1 pin
            # is worse still: base==1 re-freezes it straight back to 1.
            e["base_priority"] = int(e["priority"])
        queue_write(q)
        _log(f"queue edited [{e.get('id')}]: " +
             ", ".join(f"{k} {str(b)[:24]!r} -> {str(a)[:24]!r}" for k, b, a in changes))
        return True, f"[{e.get('id')}] {str(e.get('task', '?'))[:60]}", changes


def _dropped_store():
    """The append-only dropped-task store beside whatever TASK_QUEUE currently points at.

    Derived, not fixed, for the same reason _queue_lock() is: the selftests swap TASK_QUEUE
    for a scratch file, and a hard-coded VAULT path would have them append fixtures to- and
    recover fixtures from- the owner's real store.
    """
    try:
        p = Path(str(TASK_QUEUE))
        return p.with_name(f"{p.stem}_dropped{p.suffix or '.json'}")
    except Exception:
        return VAULT / ".baxter_task_queue_dropped.json"


def _record_drop(entry, how):
    """Append `entry` to the dropped store, and return the store's path.

    RAISES on failure, and queue_drop abandons the drop when it does. A drop whose undo
    silently failed to write is precisely the fault being fixed here, wearing a green tick.
    """
    store = _dropped_store()
    rec = _read_json(store)
    if not isinstance(rec, list):
        rec = []
    rec.append({"dropped_at": datetime.now().isoformat(timespec="seconds"),
                "dropped_by": "queue_drop", "how": how, "entry": dict(entry)})
    _write_atomic(store, rec)
    return store


def queue_drop(needle, force=False):
    """Remove one queued entry. Returns (ok, message). Refuses a live task and, without
    --force, a p1 pin- the same two guards as queue_edit, for the same reason.

    Two further guards, both bought by the 9th-July mis-drop (see queue_match):
      - A needle that resolved only by SUBSTRING is refused without --force, and the refusal
        names the id and the opening of the entry it would have deleted- so the caller sees
        what it actually matched before agreeing to destroy it. An id-shaped needle can no
        longer reach that tier at all, which is the root-cause half of the fix.
      - The entry is appended to the dropped store BEFORE it leaves the queue, inside this
        same transaction. If that append fails, nothing is dropped.
    """
    with queue_txn():
        q = queue_read()
        e, err, how = queue_match(q, needle, want_how=True)
        if err:
            return False, "refused: " + err
        lane = _live_lane_of(e)
        if lane:
            return False, (f"refused: that task is LIVE in lane {lane}- dropping the queue entry "
                           f"would not stop it. Halt the lane instead.")
        if int(e.get("priority", PRIO_DEFAULT)) == PRIO_URGENT and not force:
            return False, f"refused: [{e.get('id')}] is pinned at priority 1- dropping it needs --force."
        if how == "substring" and not force:
            return False, (f"refused: {needle!r} matched nothing but a SUBSTRING of "
                           f"[{e.get('id')}] p{_prio_int(e)} {str(e.get('task', '?'))[:60]}- "
                           f"that is the task that would be deleted. Name it by its id, or pass --force.")
        try:
            store = _record_drop(e, how)
        except Exception as exc:
            return False, (f"refused: [{e.get('id')}] was NOT dropped- the dropped store "
                           f"{Path(_dropped_store()).name} could not be written ({exc}). "
                           f"A drop that cannot be undone is not performed.")
        queue_write([x for x in q if x is not e])
        _log(f"queue dropped [{e.get('id')}] p{e.get('priority', PRIO_DEFAULT)} by {how}: "
             f"{str(e.get('task', ''))[:60]}")
        return True, (f"dropped [{e.get('id')}] p{_prio_int(e)} {str(e.get('task', '?'))[:60]} "
                      f"(recoverable: --undrop {e.get('id')}, from {Path(store).name})")


def queue_undrop(qid):
    """Restore a dropped entry from the store, verbatim and id-preserved. Returns (ok, msg).

    Addressed by id ONLY- the one handle a dropped entry certainly still has, and the one
    that cannot be confused with another task's prose. The store stays append-only: an
    undrop leaves its record in place, so the history of a task that was dropped, restored
    and dropped again reads in order. The newest record for an id is the one restored.
    """
    qid = str(qid).strip().lower()
    if not qid:
        return False, "usage: queue_undrop('<id>')"
    with queue_txn():
        store = _dropped_store()
        rec = _read_json(store)
        hits = [r for r in (rec if isinstance(rec, list) else [])
                if str((r.get("entry") or {}).get("id", "")).lower() == qid]
        if not hits:
            return False, f"no dropped entry with id {qid!r} in {Path(store).name}"
        q = queue_read()
        if any(str(e.get("id", "")).lower() == qid for e in q):
            return False, f"refused: [{qid}] is already in the live queue- nothing to restore."
        entry = dict(hits[-1]["entry"])
        q.append(entry)
        queue_write(q)
        _log(f"queue undropped [{qid}]: {str(entry.get('task', ''))[:60]}")
        return True, f"restored [{qid}] {str(entry.get('task', '?'))[:60]}- {position_line(entry)}"


# ---- REORDER WITHIN A BAND (9th July, his "cozy drag and drop" ask) ------------------
# Discord's message components have no draggable surface, so the closest thing to dragging a
# card up the list is a MOVE verb: up / down / top / bottom, or an exact 1-based slot. The
# order lives here and only here- baxter_slash's panel delegates to queue_move() rather than
# sorting anything itself, so there is exactly one implementation of "what runs next".
MOVE_WORDS = ("up", "down", "top", "bottom")


def _band_of(q, e):
    """The entries sharing e's priority, in run order, and e's index among them. Identity, not
    equality: two queue entries can compare equal as dicts, and `.index()` would find the wrong
    one and move a task the owner never picked."""
    band = [x for x in q if _prio_int(x) == _prio_int(e)]
    return band, next(i for i, x in enumerate(band) if x is e)


def _move_target(where, i, n):
    """0-based destination for the entry at index `i` of an `n`-long band, or None if `where`
    is not a move. A bare integer is a 1-based slot within that band."""
    w = str(where).strip().lower()
    if w == "up":
        return i - 1
    if w == "down":
        return i + 1
    if w == "top":
        return 0
    if w == "bottom":
        return n - 1
    try:
        return int(w) - 1
    except (TypeError, ValueError):
        return None


def queue_move(needle, where, force=False):
    """Reorder one entry WITHIN its priority band. Returns (ok, message).

    `where` is 'up' | 'down' | 'top' | 'bottom', or a 1-based slot number in that band.
    Moving rewrites `rank` and NEVER `queued_at`: a faked timestamp would buy a slot at the
    cost of the only record of when the task arrived, and it would pass any naive order check
    while quietly corrupting the FIFO fallback.

    Three freezes, all checked BEFORE a single rank is written- a refusal that has already
    reflowed the band reads as 'refused' while the run order has silently shifted:
      1. LIVE IN A LANE. Reordering a queue entry under a running build changes nothing about
         that build and is lost the moment it halts and re-queues. Never forceable.
      2. A p1 PIN- the owner's own "do this first". `force=True` lifts it, as it does for --edit.
      3. HUMAN-GATED. It cannot run until he says go, so its slot means nothing. force lifts it.
    """
    with queue_txn():
        q = queue_read()
        e, err = queue_match(q, needle)
        if err:
            return False, "refused: " + err
        # _live_lane_of resolved through the module globals, deliberately: bound to a local
        # name (or imported into one) a test's stub would miss it, the freeze would pass its
        # selftest, and it would never fire in production.
        lane = _live_lane_of(e)
        if lane:
            return False, (f"refused: that task is LIVE in lane {lane}- reordering its queue entry "
                           f"does nothing to the running build and is lost when it halts and "
                           f"re-queues. Let it land, or halt the lane first.")
        if _prio_int(e) == PRIO_URGENT and not force:
            return False, (f"refused: [{e.get('id')}] is pinned at priority 1- the owner's own "
                           f"'do this first'. Moving it within that band needs --force.")
        if is_human_gated(e) and not force:
            return False, (f"refused: [{e.get('id')}] is gated on {gate_of(e)}- it cannot run "
                           f"until he says go, so its slot decides nothing. --force to move it anyway.")
        band, i = _band_of(q, e)
        n = len(band)
        target = _move_target(where, i, n)
        if target is None:
            return False, (f"refused: {where!r} is not a move- say up, down, top, bottom, or a "
                           f"1-based slot from 1 to {n} within the p{_prio_int(e)} band.")
        target = max(0, min(n - 1, target))     # 'up' at the top is a no-op, not an error
        if target == i:
            return True, (f"[{e.get('id')}] already sits at slot {i + 1} of {n} in "
                          f"p{_prio_int(e)}- nothing moved")
        band.pop(i)
        band.insert(target, e)
        for k, x in enumerate(band, 1):         # dense 1..n, so the next move has no gaps to fall in
            x["rank"] = k
        queue_write(q)                          # same dicts, mutated in place; sorts on the new ranks
        _log(f"queue moved [{e.get('id')}] p{_prio_int(e)} slot {i + 1} -> {target + 1} of {n}: "
             f"{str(e.get('task', ''))[:60]}")
        return True, (f"[{e.get('id')}] {str(e.get('task', '?'))[:52]} moved to slot "
                      f"{target + 1} of {n} in p{_prio_int(e)}")


# ---- TWO-LANE BUILD SYSTEM + CLASH DELEGATOR (8th July, his 22:09/22:32 ask) ----
# Builds ran through ONE slot (5th July). He asked for TWO concurrent lanes to halve
# the queue-drain time, plus a delegator that keeps the lanes off conflicting or
# near-adjacent work. Design settled in
# 50-Research/Two-lane build system + build delegator - plan of attack.md.
#
# Each queue entry may declare a `touch_set`: the files/dirs it will edit, plus
# optional `@cluster` tags for logical adjacency that paths can't express (imports,
# a shared json contract). Two tasks CLASH when their touch-sets share a file, one
# path contains the other, or they share an `@cluster` tag. An UNDECLARED touch-set
# is UNKNOWN, so it clashes with everything and runs SOLO- declaring is what unlocks
# a parallel lane. The flat-dir "same parent = adjacent" rule is deliberately NOT
# used: every Baxter script lives in utils/, so it would nuke all parallelism. Real
# import-adjacency is caught by @cluster tags + the builder self-check instead.
#
# REGION-LEVEL DECLARATIONS (9th July). A whole-file lock on a HUB file- baxter_usage.py,
# baxter_triage.py- is too blunt: nearly every Baxter build edits one of them, in a
# different function, so a whole-file lock serialises builds that never actually meet.
# Declare the REGION instead, as `<file>/<function-or-section>`:
#     --touch "utils/baxter_usage.py/lane_capacity,utils/baxter_usage.py/ceiling"
# The containment rule above then does the right thing for free, with no change to
# clash(): two different regions of one file are PARALLEL; a region and a whole-file
# lock CLASH (the file lock is the coarser claim and wins); a region and its directory
# CLASH. Narrowing only pays when BOTH sides narrow- a region vs a whole-file lock is
# still a clash- so declare regions whenever you can name the functions you'll edit.
# Only declare a region you are SURE of: a wrong region is a silent concurrent edit to
# the same code, which is worse than the serialisation it avoids. When unsure, lock the
# whole file. The builder self-check (`--lane-touch`) is the backstop as a build grows.
#
# This is the coordination CORE. The queued orchestration item (N-lane fan-out) sits
# ON TOP of these primitives- LANE_COUNT, clash(), delegator_recheck()- rather than
# growing a second competing scheduler.
LANE_COUNT = 10       # concurrent big-build lanes: 1 -> 2 (his 8th-July order) -> 4 -> 6 -> 10 (9th July).
                      # They read as lanes 1-10 everywhere he looks; the code indexes from 0.
                      # Raising this ALONE is a no-op, and nearly shipped as one twice over.
                      # Lanes were never the binding constraint, touch-set GRANULARITY was:
                      # 7 of the 23 runnable tasks declared the whole of baxter_triage.py and
                      # 4 the whole of baxter_watch.ps1, so lanes contending over one hub file
                      # are still ONE effective lane. HUB_FILES + touch_problems() now refuse a
                      # bare hub declaration at queue time; that is what makes this number
                      # mean something. Never raise it without checking the queue declares
                      # regions- run `--queue-list` and look.
                      #
                      # The SECOND way it ships as a no-op: WORKER_BUDGET. A lane is not one
                      # process (see the FLEET section), so a budget that does not clear
                      # LANE_COUNT + MAX_SUBS_PER_LEAD leaves the top lanes unopenable the
                      # moment any lead fans out. Raise the two together, or not at all-
                      # baxter_fanout_selftest.py now fails the build if you forget.
HEADROOM_2ND = 0.0    # extra headroom the 2ND lane needs beyond the plain big-stop.
                      # WAS 15.0, which silently made the builder one-lane: against the
                      # flat 80% ceiling it opened lane 2 only below 65% session usage,
                      # and builds actually run at 68-79%. The band where two lanes were
                      # permitted barely overlapped the band where builds happen, so lane
                      # 2 never once opened (the owner, 9th July 00:08). The "two builds burn
                      # ~2x, don't sprint into the wall" fear was miscosted: the wall is a
                      # SOFT pause- a lane that hits it halts and re-queues at priority 2
                      # through the existing machinery, losing a spawn. Serialising every
                      # build to dodge that cost the whole feature. Lanes now DEFAULT TO
                      # SPLITTING and share one gate: if big work runs at all, both lanes
                      # run (his 9th-July 00:15 order- "over-serialising is the worse
                      # failure"). Keep the knob; raise it only with a measured reason.


def _norm_touch(p):
    """Case/separator-normalised touch entry. `@cluster` tags pass through as-is;
    paths lose their trailing slash so `utils/` and `utils` are one thing."""
    p = str(p).strip().replace("\\", "/").lower()
    if p.startswith("@"):
        return p
    if p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


# ---- ONE FILE, TWO SPELLINGS (found 9th July, by the orchestration build) ----
# Touch entries arrive in two dialects. Some are relative to the SCRIPTS dir
# ("utils/baxter_usage.py"), some to the VAULT ("00-Inbox/x.md", ".baxter_state.json"),
# and some are absolute forms of either. Case-and-separator normalisation is not enough:
# `utils/baxter_usage.py` and `c:/users/.../python scripts/utils/baxter_usage.py` are THE
# SAME FILE, and clash() judged them safely parallel. Two lanes could therefore be handed
# one file to edit concurrently while the delegator reported no conflict at all- the exact
# silent collision the whole touch-set mechanism exists to prevent.
#
# Caught live: the orchestration lane ran `--lane-touch 50-Research/<note>.md` while lane 2
# held `C:\Users\you\Documents\Baxter\50-Research`, and the self-check answered "clear".
#
# So an entry is compared on every spelling it could have. A relative path also stands for
# its absolute form under each known root; an absolute path under a known root also stands
# for its relative form. Where a relative path is ambiguous between the two roots it takes
# BOTH readings- which can only ever manufacture a clash, never hide one, and the safe
# default in this module has always been solo.
def _known_roots():
    """The two roots a relative touch entry can hang off- the vault, and Python Scripts.

    The scripts root comes off __file__, which is right for every live invocation and wrong for
    the one caller a guard most needs to serve: a sealed exam path-loads this module from a temp
    COPY (spec_from_file_location- see _sibling), so __file__ is that temp file and the root
    became its parent. Both candidates for `utils/baxter_usage.py/main` then resolved under
    nothing real, _path_state called a valid REGION an `orphan`, and touch_problems refused
    it- so main() returned 2 at the vetting gate and enqueue() was never reached at all. The
    park handler below it was correct and simply unreachable- the nastiest shape a bug has.

    sys.path cannot fix this the way it fixed the sibling imports (11th July): a root is a
    FILESYSTEM location, not an import. So __file__ is trusted only when it really is the utils/
    we ship from, and otherwise we fall back to the vault's own pinned literal- the two trees sit
    side by side. A live run resolves through the first branch and is bit-for-bit unchanged."""
    scripts = Path(os.path.dirname(os.path.abspath(__file__))).parent   # ...\Python Scripts
    if not (scripts / 'utils' / 'baxter_usage.py').is_file():
        scripts = Path(VAULT).parent / 'Python Scripts'   # path-loaded: __file__ is a stranger
    return (_norm_touch(str(VAULT)), _norm_touch(str(scripts)))


def _is_abs(p):
    return p.startswith("/") or (len(p) > 2 and p[1] == ":" and p[2] == "/")


def touch_keys(p):
    """Every spelling of one touch entry. Two entries name the same thing when their key
    sets intersect; one contains the other when any key is a path-prefix of any other."""
    p = _norm_touch(p)
    if p.startswith("@") or not p:
        return {p}
    keys = {p}
    for r in _known_roots():
        if p.startswith(r + "/"):
            keys.add(p[len(r) + 1:])
        elif not _is_abs(p):
            keys.add(f"{r}/{p}")
    return keys


# ---- THE TWO KINDS OF EMPTY (10th July) ----
# Until today an entry with no touch-set meant one of two opposite things, stored identically
# as `touch_set: []`, and the delegator could not tell them apart:
#
#   `--solo`   Someone READ the task and decided it rewrites too much to declare. It must
#              take the whole board. Over-locking is the correct answer.
#   unscoped   The fast lane wrote a placeholder the instant the owner typed a sentence. NOBODY has
#              read it. Its own next_step says "scope it, declare a real touch-set, then build".
#
# Both got the strictest lock there is, so the second kind could only run on a completely empty
# fleet- which never happens while work keeps arriving. A p5 placeholder therefore STARVED: it
# needed scoping to earn a lane, and needed a lane to get scoped. 1355480d sat there for days.
#
# The fix is not to weaken solo. It is to stop calling them the same thing. `solo: true` is now
# an explicit FIELD (like `gated_on` before it- prose and absence are not flags), and an entry
# that merely hasn't been scoped yet serialises against OTHER unscoped entries only. Its first
# act edits nothing: it reads the ask, declares a touch-set, and re-queues. That pass is safe
# beside any build. If it starts editing anyway, `live_touch` grows and delegator_recheck()
# re-clashes it mid-build- the loosening is bounded by the same mechanism that catches drift.
SOLO_LOCK = "\x00solo"        # NUL: no CLI, JSON hand-edit or prompt can spell it by accident
UNSCOPED_TAG = "@unscoped"    # legacy- retained so an old journal still parses; superseded below
# THE CHECKER FLOWS FREELY (the owner, 10th July: "every task that the only task is to 'check what
# this task actually entails' does no coding itself... they SHOULD have an explicitly
# inconsequential touchset so they have 0 barriers to entry"). A task that has not been scoped
# yet does exactly one thing first: it READS the code to work out what it will touch. A read
# conflicts with nothing- not another read, not a build editing the same file- so an unscoped
# entry now wears READONLY_TAG, which clash() treats as never-conflicting. It flows onto any
# lane immediately, in parallel with everything.
#
# The safety is not a promise, it is a lock: a lane running an unscoped entry is spawned with
# BAXTER_SCOPE_ONLY set, and the PreToolUse writer hook DENIES every Edit/Write while it is set
# (see _writer_touch_hook). So "read-only" is mechanically true- the pass physically cannot edit
# source. Its job is to declare the real touch-set (or route the ask to a PRD) and re-queue; the
# actual build runs later, scoped, with writes allowed.
READONLY_TAG = "@readonly"    # a scope-only pass: reads to find its touch-set, edits nothing


def autoscope(task, next_step=""):
    """Read a touch-set straight out of a task that NAMES its files- no lane, no model, no
    black-box checker (the owner, 10th July: "surely you can just tell, and auto scope every task...
    this isnt currently editing anything, just scrubbing through and figuring out which code
    bases this task touches"). The code that FILES a task like 'Sealed exam baxter_touchvet_
    exam.py is RED' already holds the filename; queueing it unscoped threw that away and made it
    serialise behind a scoping pass it never needed.

    Returns `['utils/<file>', ...]` for every EXISTING source file the text names that the
    QUEUE'S OWN GUARD will accept, or [] when it names none. It holds no fence of its own: each
    candidate goes through touch_problems(), the very function enqueue() vets with, and anything
    refused is dropped. Agreement is then structural rather than remembered- a hub file, a hub-
    IMPORTED file, a placeholder, an orphan path, and every rule the guard grows LATER are all
    obeyed the hour they land, with no edit here (11th July). It used to re-type two of the
    guard's four rules by hand; they drifted apart inside a day, and the rules-drift detector
    spent that day unable to queue its own repair- DRIFT_NEXT names baxter_rules.py, autoscope()
    scoped it bare, and every enqueue died on BadTouchSet.

    Three things it still refuses to guess:
      - It never invents a hub REGION. A hub file (baxter_usage/triage/watch/fast/slash) and
        anything in hub_closure() are refused bare by the guard, so they drop out here: the task
        stays unscoped and a real pass declares the region- the one case where reading the code
        is genuinely needed.
      - A task that names only a symbol or a concept ('the worker prompt builder dropped
        WORKER_RULES') yields [] and stays unscoped. A guessed set that lies is worse than none.
      - It never keeps HALF a set. One refused name drops the WHOLE scope, survivors included:
        a partial set is a guessed set that lies, and it lies with a lock on it (see below).
    """
    import re
    scripts = Path(os.path.dirname(os.path.abspath(__file__))).parent
    text = f"{task or ''}\n{next_step or ''}"
    out = []
    for m in re.finditer(r"\b([A-Za-z0-9_]+\.(?:py|ps1))\b", text):
        rel = f"utils/{m.group(1)}"
        if rel in out or not (scripts / "utils" / m.group(1)).exists():
            continue
        # ASK the guard, never re-state it. enqueue() vets what autoscope() emits with
        # touch_problems(), so anything that fence would refuse is dropped HERE, by that
        # same function- the two cannot drift apart. It is pure, cheap and fail-safe
        # (hub_closure() beneath it is cached and degrades to its seed rather than raise).
        try:
            refuse, _warn = touch_problems([rel])
        except Exception:                                                  # noqa: BLE001
            return []        # an unanswerable guard means we know nothing; unscoped is the safe state
        if refuse:
            # ONE REFUSAL DROPS THE WHOLE SET (11th July). Keeping the survivors declares what the
            # build will incidentally touch and hides the file it exists to EDIT- a real, writable
            # lane holding no claim on the refused module. The planner then names that module,
            # lane_touch_add() refuses it bare with the same guard, _widen_or_halt reads the refusal
            # as a collision and stands the lane down, and halt() re-queues the widened set unvetted
            # (vet=False)- so the bare hub path lands in the queue and the next queue_edit() of that
            # row dies on BadTouchSet. A lost build and a poisoned row, to save one read-only pass.
            return []
        out.append(rel)
    return out


def touch_of(entry):
    """A task's EFFECTIVE touch-set: what it declared at queue-time (`touch_set`)
    plus whatever it has grown into mid-build (`live_touch`, appended by the
    builder self-check).

    Nothing declared splits two ways- see the block above. `solo: true` returns the
    board-wide lock; an unscoped placeholder returns a tag that only other unscoped
    placeholders share."""
    both = list(entry.get("touch_set") or []) + list(entry.get("live_touch") or [])
    declared = {_norm_touch(x) for x in both if str(x).strip()}
    if declared:
        return declared
    # Not scoped yet. `--solo` takes the whole board; everything else is a read-only scoping
    # pass that clashes with nothing and flows straight onto a lane.
    return {SOLO_LOCK} if entry.get("solo") else {READONLY_TAG}


# ---- HUMAN GATE (9th July) ----
# A task waiting on the owner's explicit 'go' (an outward/destructive act, or a question
# only he can answer) must never be pumped into a lane. That state is now an EXPLICIT
# FIELD set at queue time- `gated_on: "owner"`- and nothing else.
# It used to be inferred by substring-matching gating words in the task's PROSE, which
# disabled any task whose DESCRIPTION merely mentioned being blocked: the p1 pump fix
# had to be reworded to avoid gating itself, and the browser-meter entry (genuinely
# gated, but phrased "GATED:" rather than "blocked") sailed straight through. Prose is
# not a flag. Never infer a gate from the text again.
GATE_NONE = ("", "none", "no", "false", "usage", "curve")   # values that mean "not gated"

def gate_of(entry):
    """Who a queue entry waits on, lowercased- '' when it is free to run."""
    g = str(entry.get("gated_on", entry.get("gate", "")) or "").strip().lower()
    return "" if g in GATE_NONE else g

def is_human_gated(entry):
    """True when the entry waits on a HUMAN, not on the usage curve."""
    return gate_of(entry) != ""


def clash(a, b):
    """Why touch-sets `a` and `b` conflict, or None if they're safely parallel.
    Undeclared (empty) always conflicts- the safe default is solo.

    Compared on every SPELLING of each path (see touch_keys): a relative and an absolute
    declaration of one file are one file. Region declarations (`<file>/<function>`) still
    fall out of the containment rule for free- two regions of a file are parallel, a region
    and a whole-file lock clash."""
    # A bare empty set still means solo. `touch_of()` never returns one, but clash() is called
    # directly on raw declarations by the builder self-check, and there the safe default holds.
    if not a or not b:
        return "undeclared touch-set- runs solo"
    # A read-only scoping pass edits NOTHING (the writer hook enforces it), so it can never
    # conflict- not with a build, not with another scoping pass. It flows onto any free lane.
    if READONLY_TAG in a or READONLY_TAG in b:
        return None
    if SOLO_LOCK in a or SOLO_LOCK in b:
        return "declared --solo- takes the whole board alone"
    shared = {x for x in a if str(x).startswith("@")} & {x for x in b if str(x).startswith("@")}
    if shared:
        tag = sorted(shared)[0]
        if tag == UNSCOPED_TAG:               # a legacy journal from before READONLY_TAG
            return None
        return f"shared cluster {tag}"
    ka = [(x, touch_keys(x)) for x in a if not str(x).startswith("@")]
    kb = [(y, touch_keys(y)) for y in b if not str(y).startswith("@")]
    for x, kx in ka:
        for y, ky in kb:
            if kx & ky:
                return f"both touch {x}"
            for px in kx:
                for py in ky:
                    if px.startswith(py + "/"):
                        return f"{x} sits inside {y}"
                    if py.startswith(px + "/"):
                        return f"{y} sits inside {x}"
    return None


def selftest_statusline():
    """Prove the statusline push is adopted as server truth without a request, and that the
    four ways it could silently lie are all shut.

    Drives probe() as the ENTRY POINT ([[exam-must-drive-the-gate]]- calling _statusline_snap
    past the gate proves nothing about what actually decides a burn), against a scratch USAGE,
    LIVE, STATUSLINE and lock. _fetch, _say, _log, _fire_alert and the breach/override/quiet
    probes are all stubbed: nothing here touches the network, the real meter, or Discord
    ([[selftests-stub-every-outward-path]]- a lane selftest once posted a false usage alert)."""
    global USAGE, LIVE, STATUSLINE, USAGE_LOCK
    import tempfile
    from datetime import timedelta
    real = (USAGE, LIVE, STATUSLINE, USAGE_LOCK,
            globals()["_fetch"], globals()["_say"], globals()["_log"], globals()["_fire_alert"],
            globals()["_quiet95_active"], globals()["_breach_active"], globals()["_override_active"])
    t = Path(tempfile.mkdtemp(prefix="baxter_statusline_"))
    USAGE, LIVE, STATUSLINE, USAGE_LOCK = t / "u.json", t / "live.json", t / "sl.json", t / "u.lock"

    said, http = [], []
    now_ts = time.time()
    sess_reset, week_reset = now_ts + 2 * 3600, now_ts + 5 * 86400
    week_iso = datetime.fromtimestamp(week_reset, timezone.utc).isoformat()

    def _oauth():
        http.append("request")
        return {"session_pct": 71.0, "session_resets_at": datetime.fromtimestamp(sess_reset, timezone.utc).isoformat(),
                "weekly_pct": 60.0, "weekly_resets_at": week_iso, "weekly_gate_pct": 60.0,
                "source": "oauth", "error": None, "_http": {"status": 200}}

    CORROBORATED = {"push": 73.0, "oauth": 71.0, "delta": 2.0, "ok": True, "at": "2026-07-09T00:00:00"}

    def _dump(session_pct, weekly_pct, age_secs, rate_limits=True, wk_reset=None):
        """Write a statusline dump aged `age_secs` seconds, exactly as the shell script does."""
        body = {"model": {"display_name": "Opus"}}
        if rate_limits:
            body["rate_limits"] = {
                "five_hour": {"used_percentage": session_pct, "resets_at": sess_reset},
                "seven_day": {"used_percentage": weekly_pct, "resets_at": wk_reset or week_reset},
            }
        STATUSLINE.write_text(json.dumps(body), encoding="utf-8")
        stamp = now_ts - age_secs
        os.utime(STATUSLINE, (stamp, stamp))
        return datetime.fromtimestamp(stamp).isoformat(timespec="seconds")

    globals()["_say"] = lambda m: said.append(m)
    globals()["_log"] = lambda *a, **k: None
    globals()["_fire_alert"] = lambda ping, key, msg: said.append(msg)
    globals()["_quiet95_active"] = lambda *a, **k: None
    globals()["_breach_active"] = lambda *a, **k: None
    globals()["_override_active"] = lambda *a, **k: None
    ok = 0
    try:
        # 1. AN UNCORROBORATED PUSH IS NEVER ADOPTED- the scale is confirmed against the endpoint
        #    once, off a read we were making anyway. Only THEN is a fresh push adopted with zero
        #    HTTP. _fetch raises for the adoption leg: if probe() reaches the endpoint, this dies.
        #    The stored meter is 20 min stale AND carries a live 429- precisely the state that
        #    used to force a probe and deepen the rate-limit.
        globals()["_fetch"] = _oauth
        pushed = _dump(73.0, 61.0, 30)
        snap = probe()                       # no _statusline_scale on record yet
        assert snap["source"] == "oauth" and http == ["request"], \
            f"an uncorroborated push must not set the band, got {snap['source']}"
        assert snap["_statusline_scale"] == {"push": 73.0, "oauth": 71.0, "delta": 2.0,
                                             "ok": True, "at": snap["updated"]}, snap["_statusline_scale"]

        globals()["_fetch"] = lambda: (_ for _ in ()).throw(AssertionError("probe made an HTTP request"))
        stale_attempt = (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds")
        _write_atomic(USAGE, {"session_pct": 44.0, "weekly_pct": 55.0, "weekly_gate_pct": 55.0,
                              "session_resets_at": datetime.fromtimestamp(sess_reset, timezone.utc).isoformat(),
                              "weekly_resets_at": week_iso, "source": "oauth",
                              "error": "HTTP Error 429: Too Many Requests",
                              "_http": {"status": 429}, "last_attempt": stale_attempt,
                              "_statusline_scale": dict(CORROBORATED),
                              "updated": (datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds")})
        pushed = _dump(73.0, 61.0, 30)
        http.clear()
        snap = probe()
        assert http == [], "a fresh corroborated push must make NO request"
        assert snap["source"] == "statusline", snap["source"]
        assert snap["session_pct"] == 73.0 and snap["weekly_pct"] == 61.0, snap
        assert _read_json(USAGE)["session_pct"] == 73.0, "the adopted push was never persisted"
        assert (snap.get("_statusline_scale") or {}).get("ok"), \
            "the corroboration was dropped on adoption- the next read would poll again"
        ok += 1

        # 2. `updated` IS THE PUSH'S OWN TIMESTAMP, NOT now(). The green-but-broken failure:
        #    stamped now(), every freshness assertion passes trivially and a 9-minute-old push
        #    buys another BIG_FRESH of trust. `last_attempt` tracks HTTP attempts and drives
        #    PROBE_FLOOR/RL_BACKOFF- a zero-request read must leave it exactly where it was.
        assert snap["updated"] == pushed, f"updated must be the push's mtime {pushed}, got {snap['updated']}"
        assert _age_of(snap["updated"]) >= 25, "a 30s-old push must report ~30s of age, not 0"
        assert snap["last_attempt"] == stale_attempt, f"last_attempt was disturbed: {snap['last_attempt']}"
        assert snap["error"] == "HTTP Error 429: Too Many Requests", "the HTTP channel's state was lost"
        ok += 1

        # Every case below starts from a CORROBORATED, 20-minute-stale meter, so the only thing
        # that can stop an adoption is the invariant under test- never a missing scale record.
        def _stored(**over):
            was = (datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds")
            base = {"session_pct": 44.0, "weekly_pct": 55.0, "weekly_gate_pct": 55.0,
                    "session_resets_at": datetime.fromtimestamp(sess_reset, timezone.utc).isoformat(),
                    "weekly_resets_at": week_iso, "source": "oauth", "error": None,
                    "_statusline_scale": dict(CORROBORATED), "last_attempt": was, "updated": was}
            base.update(over)
            _write_atomic(USAGE, base)

        # 3. NO rate_limits KEY -> None, NEVER 0.0. A fresh session, a re-auth or a token blip
        #    yields a dump without it. Read as 0.0 it is a false-low that walks a build through
        #    the wall; it must fall through to the endpoint instead.
        globals()["_fetch"] = _oauth
        http.clear()
        _stored()
        _dump(0, 0, 5, rate_limits=False)
        assert _statusline_snap() is None, "an absent rate_limits must yield None"
        snap = probe(force=True)
        assert snap["source"] == "oauth" and http == ["request"], f"must fall through to HTTP, got {snap['source']}"
        assert snap["session_pct"] == 71.0, snap
        ok += 1

        # 4. A STALE PUSH IS NOT THE BAND. Past BIG_FRESH it may only trigger a real poll-
        #    a stale true number is as dangerous as a false one (the 6th-July false-low).
        http.clear()
        _stored()
        _dump(73.0, 61.0, BIG_FRESH + 300)
        snap = probe(force=True)
        assert snap["source"] == "oauth" and http == ["request"], f"a stale push must not set the band: {snap}"
        ok += 1

        # 5. THE SAME PUSH IS NEVER RE-ADOPTED. The watcher calls --enforce every 5 seconds;
        #    without the strictly-newer guard each call rewrites USAGE and re-runs _pings.
        #    Unforced: the meter is left byte-identical. Forced: it falls through to HTTP
        #    rather than silently re-adopting a push it has already consumed.
        http.clear()
        _stored()
        _dump(73.0, 61.0, 30)
        assert probe()["source"] == "statusline", "the setup adoption did not happen"
        before = USAGE.read_bytes()
        probe(); probe()
        assert USAGE.read_bytes() == before, "a re-read of the same push rewrote the meter"
        assert http == [], "a re-read of the same push made a request"
        snap = probe(force=True)
        assert snap["source"] == "oauth" and http == ["request"], "a forced probe re-adopted a consumed push"
        ok += 1

        # 6. THE WEEKLY GATE NEVER FALLS. OAuth's weekly_gate_pct is max(overall, per-model
        #    scoped weekly limits); the push carries only the overall seven_day figure. Adopted
        #    wholesale it would drop the gate from 90 to 61 and walk a build through the weekly
        #    wall. Weekly usage is monotonic in a window, so the stored gate is a floor- and a
        #    genuinely NEW weekly window drops it, since the floor belonged to the old one.
        http.clear()
        globals()["_fetch"] = lambda: (_ for _ in ()).throw(AssertionError("probe made an HTTP request"))
        _stored(weekly_gate_pct=90.0)
        _dump(73.0, 61.0, 30)
        snap = probe()
        assert snap["source"] == "statusline", snap["source"]
        assert snap["weekly_pct"] == 61.0, snap["weekly_pct"]
        assert snap["weekly_gate_pct"] == 90.0, f"the scoped weekly floor was lowered to {snap['weekly_gate_pct']}"
        # ...and it is not sticky across a real window boundary.
        _stored(weekly_gate_pct=90.0)
        _dump(73.0, 61.0, 10, wk_reset=week_reset + 3 * 86400)
        snap = probe()
        assert snap["source"] == "statusline", snap["source"]
        assert snap["weekly_gate_pct"] == 61.0, f"the floor outlived its window: {snap['weekly_gate_pct']}"
        assert http == [], "the weekly-floor path made a request"
        ok += 1
    finally:
        (USAGE, LIVE, STATUSLINE, USAGE_LOCK,
         globals()["_fetch"], globals()["_say"], globals()["_log"], globals()["_fire_alert"],
         globals()["_quiet95_active"], globals()["_breach_active"], globals()["_override_active"]) = real
    print(f"statusline push selftest OK: {ok}/6")
    print("  an uncorroborated push never sets the band; once the scale is confirmed against the")
    print("  endpoint a fresh push sets it with zero requests, even through a live 429; `updated` is")
    print("  the push's own mtime; an absent rate_limits and a stale push both fall through to HTTP;")
    print("  the same push is never re-adopted; the scoped weekly gate is floored within its window.")


def selftest_pings():
    """Prove a quiet window mutes the PERCENTAGE and never the STATE, and that a hole between
    clean reads announces itself (9th July; the owner 07:08: "I know we hit 80% usage. But you never
    pinged that over"). Drives _pings() directly and asserts on what reached the outward path,
    never on the source text. _say, _log and both breach/override probes are stubbed, and
    QUIET95 is redirected to a scratch file- nothing here touches the network, the real markers,
    or Discord ([[selftests-stub-every-outward-path]])."""
    global QUIET95
    import tempfile
    from datetime import timedelta
    real = (QUIET95, globals()["_say"], globals()["_log"],
            globals()["_breach_active"], globals()["_override_active"])
    said = []
    globals()["_say"] = lambda m: said.append(m)
    globals()["_log"] = lambda *a, **k: None
    globals()["_breach_active"] = lambda *a, **k: None
    globals()["_override_active"] = lambda *a, **k: None
    QUIET95 = Path(tempfile.mkdtemp(prefix="baxter_pings_")) / "quiet95.json"

    wk = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()   # far out: no closing warn
    w1 = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    w2 = (datetime.now(timezone.utc) + timedelta(hours=7)).isoformat()  # a genuinely new window

    def snap(pct, win):
        return {"session_pct": pct, "weekly_gate_pct": 0.0, "weekly_pct": 0.0,
                "session_resets_at": win, "weekly_resets_at": wk}
    try:
        # Every muted-percentage assert below tests the '%'-suffixed token, never a bare digit
        # string: the outward line also carries the window's reset clock, so a bare '40' matches
        # inside a rendered '6:40pm'. The token is exact, not weaker- _pings renders the
        # percentage solely as f"{pct:.0f}%", so a leaked number always brings its '%' along.

        # 1. ARMED at 82%: the number is muted, "big tasks paused" still reaches _say. Once.
        _write_atomic(QUIET95, {"until": (datetime.now() + timedelta(hours=1)).isoformat()})
        assert _quiet95_active() is not None, "the scratch quiet window failed to arm"
        p = _pings(snap(82.0, w1), None)
        assert len(said) == 1, f"expected exactly one outward line, got {said}"
        assert "big tasks paused" in said[0], said[0]
        assert "82%" not in said[0], f"the percentage must stay muted: {said[0]}"

        # ...and it does not re-announce itself on the next poll of the same window.
        said.clear()
        p = _pings(snap(82.0, w1), p)
        assert said == [], f"one crossing, one line- got a repeat: {said}"

        # 2. Back under the band in the next window: the recovery speaks THROUGH the mute.
        said.clear()
        p = _pings(snap(40.0, w2), p)
        back = [m for m in said if "running again" in m]
        assert len(back) == 1, f"expected one 'running again' line, got {said}"
        assert "40%" not in back[0], f"the percentage must stay muted: {back[0]}"

        # 3. QUIET95 ABSENT: unchanged- one 80% band ping, carrying the state itself.
        QUIET95.unlink()
        assert _quiet95_active() is None
        said.clear()
        _pings(snap(82.0, w1), None)
        assert len(said) == 1, f"expected exactly one outward line, got {said}"
        assert "82%" in said[0] and "big tasks paused" in said[0], said[0]

        # 4. THE MUTE MUST NOT STAMP THE BAND (the exact bug). Muted at 82, then the window
        #    lapses out of quiet: crossing 80 again in a NEW window still speaks the number.
        _write_atomic(QUIET95, {"until": (datetime.now() + timedelta(hours=1)).isoformat()})
        said.clear()
        p = _pings(snap(82.0, w1), None)
        assert len(said) == 1 and "82%" not in said[0], said
        QUIET95.unlink()
        said.clear()
        _pings(snap(82.0, w2), p)
        assert any("82%" in m for m in said), f"the number never came back: {said}"

        # 5. OUTAGE NOTICE: a five-hour hole between clean reads announces itself, exactly once.
        said.clear()
        hole = (datetime.now() - timedelta(hours=5)).isoformat(timespec="seconds")
        p = _pings(snap(40.0, w1), None, hole)
        assert len(said) == 1 and "blind" in said[0] and "5h" in said[0], said
        said.clear()
        _pings(snap(40.0, w1), p, hole)
        assert said == [], f"one hole, one line- got a repeat: {said}"

        # ...and an ordinary 31-minute cadence gap says nothing at all.
        said.clear()
        _pings(snap(40.0, w1), None, (datetime.now() - timedelta(minutes=31)).isoformat())
        assert said == [], f"a normal poll gap must be silent: {said}"
    finally:
        (QUIET95, globals()["_say"], globals()["_log"],
         globals()["_breach_active"], globals()["_override_active"]) = real
    print("baxter_usage pings selftest OK: a quiet window mutes the percentage and never "
          "'big tasks paused'; the recovery speaks through it; a five-hour hole announces itself.")


def selftest_declare():
    """Prove `--queue` cannot create an undeclared entry by accident (9th-July hole).

    Drives main() exactly as a caller does, against a scratch queue file, and stubs the
    meter so nothing touches the network. Every outward path stays stubbed- a selftest that
    reaches _say once posted a false usage reset to the owner ([[selftests-stub-every-outward-path]])."""
    global TASK_QUEUE, REJECT_LOG
    import tempfile
    real_queue, real_log, real_rej = TASK_QUEUE, globals()["_log"], REJECT_LOG
    tmp = Path(tempfile.mkdtemp(prefix="baxter_declare_")) / "queue.json"
    TASK_QUEUE, globals()["_log"] = tmp, lambda *a, **k: None
    # Every accepted --queue below carries --prd-exempt, and every bypass is LOGGED. Without
    # this redirect a selftest run would write 'prd_exempt' rows into the guard log the owner reads.
    REJECT_LOG = tmp.parent / "exempt.jsonl"
    try:
        # 1. The bug, exactly as it was: no touch-set, no --solo. It must be REFUSED.
        assert main(["--queue", "some big build", "first step"]) == 2, \
            "an undeclared --queue must be refused, not silently queued solo"
        assert not tmp.exists() or not queue_read(), "a refused task must not reach the queue"

        # 2. Declaring unlocks it.
        assert main(["--queue", "declared build", "step", "--touch", "utils/a.py,@probe",
                     "--prd-exempt"]) != 2
        e = next(x for x in queue_read() if x["task"] == "declared build")
        assert e["touch_set"] == ["utils/a.py", "@probe"], e["touch_set"]

        # 2b. A PSEUDO-declaration is refused too. Copying '@cluster' straight out of the
        #     example line used to produce an entry that READ as declared, so the delegator
        #     co-scheduled it onto a lane and the build then collided on files it never named.
        #     That is strictly worse than undeclared, which at least fails safe (solo).
        for ph in ("utils/x.py,@cluster", "@cluster", "utils/some_dir/"):
            assert main(["--queue", f"placeholder {ph}", "step", "--touch", ph]) == 2, \
                f"the example-line placeholder {ph!r} must not pass as a real declaration"
        assert not any(x["task"].startswith("placeholder") for x in queue_read()), \
            "a refused placeholder must not reach the queue"

        # 3. --solo is the ONLY way to an undeclared entry, and it is deliberate.
        assert main(["--queue", "unscopable ask", "step", "--solo", "--prd-exempt"]) != 2
        e = next(x for x in queue_read() if x["task"] == "unscopable ask")
        assert e["touch_set"] == [], e["touch_set"]
        assert e["solo"] is True, "--solo must PERSIST on the entry, not be spent on the refusal check"
        assert clash(touch_of(e), {"anything"}), "a solo entry must still clash with everything"

        # 3b. THE TWO KINDS OF EMPTY (10th July). `--solo` and 'nobody has scoped this yet' both
        # carry touch_set: [] but need OPPOSITE locks. --solo takes the whole board. An unscoped
        # entry is a READ-ONLY scoping pass (the writer hook enforces it), so it clashes with
        # NOTHING and flows onto any lane in parallel- the owner, 10th July: "0 barriers to entry".
        placeholder = {"task": "raw sentence from the owner", "touch_set": []}          # no solo key
        declared = {"task": "a real build", "touch_set": ["utils/baxter_deals.py"]}
        assert touch_of(e) == {SOLO_LOCK}, touch_of(e)
        assert touch_of(placeholder) == {READONLY_TAG}, touch_of(placeholder)
        assert clash(touch_of(e), touch_of(declared)), "declared --solo must still take the board"
        assert clash(touch_of(placeholder), touch_of(declared)) is None, \
            "a scope-only pass must co-schedule with a declared build- that is the point"
        assert clash(touch_of(placeholder), touch_of({"touch_set": []})) is None, \
            "two scope-only passes edit nothing, so they never clash with each other either"
        assert clash(touch_of(placeholder), touch_of(e)) is None, \
            "a scope-only pass edits nothing, so even a --solo build does not block it"
        # ...and an entry that is legacy-empty but flagged solo reads as solo, not read-only.
        assert touch_of({"touch_set": [], "solo": True}) == {SOLO_LOCK}
        # the read-only tag is refused as a hand-declaration- only the scheduler may mint it.
        assert "@readonly" in (touch_problems(["@readonly"])[0] or [""])[0], \
            "a worker must not be able to declare itself read-only to dodge the writer hook"

        # 3c. Scoping an unscopable task RETIRES its board-wide lock. Leaving solo:true beside a
        # fresh declaration would keep the lock and make the whole scoping pass pointless.
        assert main(["--edit", "unscopable ask", "--touch", "utils/baxter_archive.py"]) == 0  # a non-closure leaf: baxter_deals.py is now hub-closure-fenced
        e = next(x for x in queue_read() if x["task"] == "unscopable ask")
        assert e["solo"] is False, "a real touch-set must clear solo"
        assert clash(touch_of(e), {"utils/baxter_say.py"}) is None, "it must now co-schedule"
        # ...and a priority bump alone must NOT silently un-solo a task nobody has scoped.
        assert main(["--queue", "still unscopable", "step", "--solo", "--prd-exempt"]) != 2
        assert main(["--edit", "still unscopable", "--priority", "4"]) == 0
        e2 = next(x for x in queue_read() if x["task"] == "still unscopable")
        assert e2["solo"] is True, "an unrelated --edit must leave solo alone"

        # 4. THE REGRESSION THIS NEARLY CAUSED: bumping an ALREADY-declared task passes no
        #    --touch. enqueue() preserves the old set, so it must not be refused.
        assert main(["--queue", "declared build", "step", "--priority", "1",
                     "--prd-exempt"]) != 2, \
            "re-queueing a declared task without --touch must inherit, not refuse"
        e = next(x for x in queue_read() if x["task"] == "declared build")
        assert e["touch_set"] == ["utils/a.py", "@probe"], "the inherited set survived the bump"
        assert e["priority"] == 1, e["priority"]

        # 5. THE HUB-FILE RULE (9th July). A whole-file lock on baxter_triage.py locked out
        #    7 of the 23 runnable tasks, so raising LANE_COUNT would have changed nothing.
        #    A bare hub path is refused; the same file named by REGION is exactly what we want.
        assert main(["--queue", "hub whole file", "step", "--touch", "utils/baxter_triage.py"]) == 2, \
            "a bare hub-file declaration must be refused- it serialises the whole queue"
        scripts = Path(os.path.dirname(os.path.abspath(__file__))).parent
        assert main(["--queue", "hub abs", "step", "--touch", f"{scripts}/utils/baxter_usage.py"]) == 2, \
            "and refused through its absolute spelling too, or the rule is one rename from useless"
        assert main(["--queue", "hub region", "step", "--prd-exempt",
                     "--touch", "utils/baxter_triage.py/_claude"]) != 2, "a region must queue"
        assert main(["--queue", "hub solo", "step", "--solo", "--prd-exempt"]) != 2, \
            "--solo stays the escape hatch for a task that genuinely rewrites a hub file"
        assert not any(x["task"].startswith("hub whole") or x["task"] == "hub abs"
                       for x in queue_read()), "a refused hub declaration must not reach the queue"

        # 5b. THE HUB CLOSURE (10th July). The region rule reaches past the five hub files to
        #     every module they IMPORT: baxter_send_dedup.py is the send ledger, imported at
        #     module scope by baxter_fast.py, and a whole-file lock on it serialises every
        #     hub-adjacent lane exactly as a bare hub path did. A REGION of it still queues; a
        #     non-closure LEAF (baxter_archive.py) stays whole-declarable- the fence is not
        #     universal, or it would starve the queue.
        assert main(["--queue", "closure whole", "step",
                     "--touch", "utils/baxter_send_dedup.py"]) == 2, \
            "a bare whole-file lock on a hub-imported module must be refused"
        assert main(["--queue", "closure region", "step", "--prd-exempt",
                     "--touch", "utils/baxter_send_dedup.py/_claim"]) != 2, \
            "a REGION of a closure module must still queue- lanes share it by region"
        assert main(["--queue", "closure leaf", "step", "--prd-exempt",
                     "--touch", "utils/baxter_archive.py"]) != 2, \
            "a non-closure leaf file must stay whole-declarable"
        assert not any(x["task"] == "closure whole" for x in queue_read()), \
            "a refused closure declaration must not reach the queue"

        # 5c. THE CLOSURE FENCE IS FAIL-SAFE. baxter_imports.closure() RAISES when any closure
        #     module is broken on disk- a sibling lane's declared red-proof window. touch_problems
        #     runs on every enqueue, so a raised walk must degrade to the static seed (which still
        #     names baxter_send_dedup.py) and NEVER crash the queue. Driven by monkeypatching the
        #     SAME baxter_imports object hub_closure() imports, with the cache cleared.
        global _HUB_CLOSURE_CACHE
        try:
            from utils import baxter_imports as _bi
        except ImportError:
            import baxter_imports as _bi
        real_closure, saved_cache = _bi.closure, _HUB_CLOSURE_CACHE

        def _boom(_root):
            raise _bi.WalkError("closure module broken on disk (simulated red-proof)")

        _HUB_CLOSURE_CACHE = None
        _bi.closure = _boom
        try:
            fb = hub_closure()
            assert "utils/baxter_send_dedup.py" in fb, \
                "the fail-safe seed dropped baxter_send_dedup.py"
            assert touch_problems(["utils/baxter_send_dedup.py"])[0], \
                "a raised closure walk must still refuse the whole-file lock via the seed"
        finally:
            _bi.closure = real_closure
            _HUB_CLOSURE_CACHE = saved_cache

        # 6. PATH VALIDATION WARNS, IT DOES NOT REFUSE. Four live entries legitimately name
        #    a file their task will CREATE. A hard exists-on-disk check would wedge the queue.
        assert main(["--queue", "creates its own file", "step", "--prd-exempt",
                     "--touch", "utils/baxter_not_yet_written.py"]) != 2, \
            "a not-yet-created file must WARN, never refuse- the task is about to create it"
        assert main(["--queue", "home dotfile", "step", "--touch", "~/.claude.json",
                     "--prd-exempt"]) != 2, \
            "~ must expanduser, not read as a missing directory"
        assert main(["--queue", "typo", "step", "--touch", "utils/no_such_dir/x.py"]) == 2, \
            "a path under a directory that does not exist is a typo, and it is refused"

        # 7. --source-mid KEYS THE ENTRY ON THE DISCORD MESSAGE, so a second --queue carrying
        #    the same id and better prose UPGRADES the placeholder rather than forking a twin
        #    beside it. Before the flag existed, enqueue()'s message-id dedup was reachable
        #    only by importing the module: every CLI caller queueing for a message that had
        #    already been given a placeholder doubled it, and the owner was told a position that
        #    described the wrong row.
        assert main(["--queue", "ask one", "step", "--solo", "--source-mid", "777",
                     "--prd-exempt"]) == 0
        assert main(["--queue", "ask one, reworded by triage", "step",
                     "--solo", "--source-mid", "777", "--prd-exempt"]) == 0
        keyed = [x for x in queue_read() if x.get("source_mid") == "777"]
        assert len(keyed) == 1, f"--source-mid forked a twin: {[x['task'] for x in keyed]}"
        assert "reworded" in keyed[0]["task"], keyed[0]["task"]

        #    ...and a DECLARED entry re-queued under its id with fresh prose inherits its
        #    touch-set, rather than reading as undeclared and being refused.
        assert main(["--queue", "keyed build", "step", "--touch", "utils/a.py",
                     "--source-mid", "778", "--prd-exempt"]) == 0
        assert main(["--queue", "keyed build, refined", "step", "--source-mid", "778",
                     "--prd-exempt"]) == 0, \
            "a reworded re-queue of a declared entry must inherit its touch-set, not refuse"
        e = next(x for x in queue_read() if x.get("source_mid") == "778")
        assert e["task"] == "keyed build, refined" and e["touch_set"] == ["utils/a.py"], e

        # 7b. THE PRD GATE (the owner, 9th July). A declared, non-duplicate, perfectly-formed big
        #     task is STILL refused when no document stands behind it- and the refusal, like
        #     every other, lands in the guard's own log. --prd-exempt is the one way past, and
        #     it is logged too: everything above this line used it, and said so.
        # The EXIT CODE is an acceptance (11th July): the ask is parked on the PM gate, so the
        # caller is told it is safe and moving. The bite is 7c below- it may never be runnable.
        assert main(["--queue", "an undocumented build", "step", "--solo"]) == 0, \
            "a PRD-less big task is routed to the PM, not reported back to him as a failure"
        assert main(["--queue", "a note that is not a PRD", "step", "--solo",
                     "--note", str(tmp)]) == 0, \
            "a --note outside 60-PRDs is not a PRD: it is parked for the PM, and reported as one"
        kinds = [x["kind"] for x in read_rejects(30)]
        assert "prd_missing" in kinds, f"the PRD refusal never reached the log: {kinds}"
        assert "prd_exempt" in kinds, f"the vitals bypass was never logged: {kinds}"

        # 7c. REFUSE THE BUILD, NEVER LOSE THE ASK (10th July). This case used to assert the
        #     OPPOSITE- "a PRD-less refusal must not reach the queue"- and that assertion is
        #     why fourteen of the owner's asks ceased to exist on 10th July, among them the Codex
        #     doctor pfp and the drag-and-drop queue UI he had asked for five times. Refusing
        #     to BUILD something is a guard doing its job. Refusing to REMEMBER it is data loss.
        parked = next((x for x in queue_read() if x["task"] == "an undocumented build"), None)
        assert parked is not None, \
            "the PRD-less ask must be RECORDED in the queue, not destroyed with its refusal"
        assert gate_of(parked) == PRD_GATE and is_human_gated(parked), \
            f"an ask awaiting its PRD must be gated so no pump can build it: {parked.get('gated_on')!r}"
        assert "PM is drafting" in parked["next_step"], \
            "the entry must say the PM has it, not that a human must go and rescue it"
        # ...and the pump must genuinely refuse it, not merely be expected to.
        assert is_human_gated(parked), "a gated entry is never runnable"

        # 7d. THE ASK GOES BACK TO THE PM, AND THE GATE LIFTS ITSELF (the owner, 10th July: "I don't
        #     want a PRD ever parked. I want it sent back to the PM every single time with
        #     feedback... a loop until the PRD is sufficiently built"). No PRD -> still gated.
        #     A real PRD in 60-PRDs/ -> prd_sweep() releases it on the very next governor beat,
        #     with no human in the loop. BAXTER_NO_PM_SWEEP keeps this exam from spawning Opus.
        assert prd_sweep() == (0, 0), "with no PRD and no PM allowed, nothing is released"
        assert gate_of(next(x for x in queue_read()
                            if x["task"] == "an undocumented build")) == PRD_GATE
        prd = Path(PRD_DIR) / "__selftest_prd.md"
        prd.parent.mkdir(parents=True, exist_ok=True)
        prd.write_text("# a PRD that exists", encoding="utf-8")
        try:
            assert main(["--edit", "an undocumented build", "--note", str(prd)]) == 0
            released, _ = prd_sweep()
            assert released == 1, f"a PRD-backed ask must have its gate lifted: released={released}"
            freed = next(x for x in queue_read() if x["task"] == "an undocumented build")
            assert gate_of(freed) == "" and not is_human_gated(freed), \
                "once its PRD exists the ask is an ordinary build, runnable by the pump"
        finally:
            prd.unlink(missing_ok=True)
        assert main(["--edit", "an undocumented build", "--gate", PRD_GATE]) == 0   # restore state
        # A second park of the SAME ask must not fork a twin beside the first.
        before = len([x for x in queue_read() if x["task"] == "an undocumented build"])
        assert main(["--queue", "an undocumented build", "step", "--solo"]) == 0
        after = [x for x in queue_read() if x["task"] == "an undocumented build"]
        assert len(after) == before == 1, f"re-refusing one ask parked it twice: {len(after)}"

        # 8. A REJECTION IS RECORDED, and reads back. This is the proof-of-life he asked for.
        REJECT_LOG = tmp.parent / "rejects.jsonl"
        try:
            record_reject({"task": "held task", "priority": 3}, "both touch utils/x.py",
                          lane=0, kind="clash")
            record_reject("gated task", "gated on owner", kind="gated_on")
            rows = read_rejects(10)
            assert len(rows) == 2 and rows[0]["kind"] == "gated_on", rows
            assert rows[1]["lane"] == 1, "a lane is logged as 1..N, never 0"
            assert rows[1]["priority"] == 3 and rows[1]["task"] == "held task", rows[1]
            assert all(set(("at", "task", "reason", "kind")) <= set(r) for r in rows), rows
            assert read_rejects(10, since="2099-01-01") == [], "--since must filter"
            assert main(["--rejects", "5"]) == 0
        finally:
            REJECT_LOG = real_rej
    finally:
        TASK_QUEUE, globals()["_log"], REJECT_LOG = real_queue, real_log, real_rej
    print("baxter_usage declare selftest OK: an undeclared queue entry is impossible to create "
          "by accident, a bare HUB file is refused in favour of a region, a PRD-less big task is "
          "accepted but parked with the PM where no lane can run it, its --prd-exempt bypass is "
          "logged, a not-yet-created file "
          "warns rather than wedging the queue, and every guard rejection lands in the log.")


# The two tickets, verbatim from 663dde15. Both were scheduled- into lane 8 and lane 2- for
# work that had already shipped. They are the fixture because they are the incident.
DUP_TICKET_A = "Repair worker: raise retry cap 2->3"
DUP_TICKET_B = "Raise repair-worker retry cap from 2 to 3 attempts PERMANENT"


def selftest_dup():
    """Prove `--queue` cannot schedule a reworded restatement of a pending or in-flight task,
    and- just as hard- that it still schedules everything else.

    Driven through main() rather than enqueue(), because the refusal the owner sees is an EXIT
    CODE and only main() produces one. Calling enqueue() directly would prove the helper works
    while the CLI still queued the duplicate ([[exam-must-drive-the-gate]]).

    Every outward path is stubbed, lane_journals() included: unstubbed it reads the REAL
    .baxter_resume/, so the verdict would depend on whatever happened to be building at the
    time ([[selftests-stub-every-outward-path]])."""
    global TASK_QUEUE, REJECT_LOG
    import io, tempfile
    real_queue, real_rej = TASK_QUEUE, REJECT_LOG
    real_log, real_journals = globals()["_log"], globals()["lane_journals"]
    tmp = Path(tempfile.mkdtemp(prefix="baxter_dup_"))
    TASK_QUEUE, REJECT_LOG = tmp / "queue.json", tmp / "rejects.jsonl"
    globals()["_log"] = lambda *a, **k: None
    inflight = []                      # [(journal, entry)]- what the stubbed fleet is running
    globals()["lane_journals"] = lambda alive_only=True: list(inflight)

    def run(*argv):
        """Drive the CLI exactly as a caller does -> (exit code, what it printed).

        Every --queue here carries --prd-exempt. These fixtures grade the DUPLICATE guard, and
        a bare ticket string is refused by the PRD gate long before it reaches one. The bypass
        rows land in the scratch rejects file this selftest already redirects."""
        argv = list(argv)
        if argv[:1] == ["--queue"] and "--prd-exempt" not in argv:
            argv.append("--prd-exempt")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def reset():
        _write_atomic(TASK_QUEUE, [])

    def depth():
        return len(queue_read())

    try:
        # 0. THE TOKENISER, pinned. '2->3' must split on the arrow into {2, 3}, or the two
        #    wordings share only 'retry' and 'cap' and the guard misses the incident entirely.
        assert _task_tokens(DUP_TICKET_A) == {"repair", "worker", "raise", "retry", "cap", "2", "3"}, \
            _task_tokens(DUP_TICKET_A)
        assert {"2", "3"} <= _task_tokens("from 2 to 3"), "'from 2 to 3' must yield the digits"
        assert "permanent" not in _task_tokens("PERMANENTLY"), "urgency filler is stemmed + dropped"

        reset()
        # 1. The first ticket queues normally.
        rc, out = run("--queue", DUP_TICKET_A, "raise the cap", "--touch", "utils/a.py")
        assert rc == 0, (rc, out)
        assert depth() == 1, depth()
        first_id = queue_read()[0]["id"]

        # 2. THE INCIDENT. The real reword is REFUSED, it names the ticket it collided with,
        #    and the queue does not grow. This is the whole build, in four lines.
        rc, out = run("--queue", DUP_TICKET_B, "raise the cap", "--touch", "utils/a.py")
        assert rc == 2, f"the 9th-July reword must be refused, got rc={rc}: {out}"
        assert first_id in out, f"the refusal must name the colliding id {first_id}: {out}"
        assert "near-duplicate" in out.lower(), out
        assert depth() == 1, f"a refused duplicate reached the queue: {depth()}"

        # 3. ...and the guard keeps its own log (the owner, 9th July 08:46).
        rows = read_rejects(5)
        assert rows and rows[0]["kind"] == "duplicate", rows
        assert first_id in rows[0]["reason"], rows[0]

        # 4. THE THRESHOLD IS NOT A BLANKET. Two genuinely distinct builds still queue- a guard
        #    that refused everything would sail through every assertion above it.
        for t in ("Wire the Claude statusline rate_limits push into the usage meter",
                  "Reap ghost lanes when the governor hard-stops a build mid-flight"):
            rc, out = run("--queue", t, "step", "--touch", "utils/a.py")
            assert rc == 0, f"a distinct task was refused: {t!r} -> {out}"
        assert depth() == 3, depth()

        # 5. An EXACT re-queue still takes the update path- a build that halts twice must never
        #    be refused for resembling itself.
        rc, out = run("--queue", DUP_TICKET_A, "carry on", "--priority", "1")
        assert rc == 0, (rc, out)
        assert depth() == 3, f"the update path forked an entry: {depth()}"
        assert next(e for e in queue_read() if e["id"] == first_id)["priority"] == 1

        # 6. THE REFINEMENT FORK. The fast lane writes a placeholder from the owner's raw sentence;
        #    triage rewords it later under the same message id. Keyed on that id it is an
        #    UPGRADE. Were the dedup check to run ABOVE the source_mid match, the refinement
        #    would be refused for resembling the very row it refines, and his ask would vanish.
        reset()
        rc, _ = run("--queue", DUP_TICKET_A, "step", "--touch", "utils/a.py", "--source-mid", "5150")
        assert rc == 0 and depth() == 1
        rc, out = run("--queue", DUP_TICKET_B, "step", "--source-mid", "5150")
        assert rc == 0, f"a source_mid refinement must upgrade in place, not be refused: {out}"
        assert depth() == 1, f"the refinement forked a twin: {depth()}"
        assert queue_read()[0]["task"] == DUP_TICKET_B, queue_read()[0]["task"]

        # 7. IN-FLIGHT COUNTS. The 9th-July twin was queued while its original was already
        #    RUNNING- the pump had popped it off the queue- so a pending-only check waves it
        #    straight through. Here the queue is empty and the collision is with a lane.
        reset()
        inflight[:] = [(Path("resume-fixture-0.json"), {"id": "13b1cfe7", "task": DUP_TICKET_A})]
        rc, out = run("--queue", DUP_TICKET_B, "step", "--touch", "utils/a.py")
        assert rc == 2, f"a duplicate of an IN-FLIGHT build must be refused: {out}"
        assert "13b1cfe7" in out, out
        assert depth() == 0, depth()

        # 8. A journal caught mid-write (entry=None) must not take the queue down with it.
        inflight[:] = [(Path("resume-fixture-0.json"), None)]
        rc, out = run("--queue", DUP_TICKET_B, "step", "--touch", "utils/a.py")
        assert rc == 0, f"an unreadable journal must not break enqueue: {out}"
        assert depth() == 1

        # 9. --dup-ok is the deliberate escape hatch: a genuine second pass over one subsystem
        #    has to stay queueable.
        reset()
        run("--queue", DUP_TICKET_A, "step", "--touch", "utils/a.py")
        rc, out = run("--queue", DUP_TICKET_B, "step", "--touch", "utils/a.py", "--dup-ok")
        assert rc == 0, f"--dup-ok must force a near-duplicate through: {out}"
        assert depth() == 2, depth()

        # 10. SELF-REJECTION IS IMPOSSIBLE. halt() re-queues a task whose own journal is still
        #     alive and heartbeating. If the in-flight check reached it, a halting build would
        #     delete itself at the very moment it tried to save its place.
        reset()
        inflight[:] = [(Path("resume-fixture-0.json"), {"id": "live1", "task": DUP_TICKET_A})]
        halt(DUP_TICKET_A, "resume from step 3", touch_set=["utils/a.py"])
        assert depth() == 1, "halt() was refused as a duplicate of its own live journal"
        enqueue(DUP_TICKET_B, "step")     # the _park / baxter_fast / autobuild path: dedup off
        assert depth() == 2, "an internal enqueue() must not dedup by default"

        # 11. TOO SHORT TO JUDGE, and the subset trap. Overlap alone scores 'fix the queue' a
        #     perfect 1.0 against any longer task containing it; demanding Jaccard as well is
        #     what keeps a distinct build out of the reject log.
        assert _near_duplicate("fix queue", [{"id": "x", "task": "fix queue"}]) is None, \
            "a 2-token ask is too short to judge honestly"
        assert _near_duplicate("fix the queue delegator bug in the pump right now",
                               [{"id": "x", "task": "fix the queue delegator bug now"}]) is None, \
            "overlap 1.0 with low jaccard is a SUBSET, not a duplicate"

        # 12. THE STALE-TICKET TELL, locked as a regression. The duplicate declared
        #     utils/baxter_repair.py, which has never existed. It WARNS loudly and still queues:
        #     live entries legitimately name a module they are about to write.
        reset()
        rc, out = run("--queue", "Build a brand new repair module from scratch", "step",
                      "--touch", "utils/baxter_repair.py")
        assert rc == 0, f"a not-yet-created module must warn, never refuse: {out}"
        assert "STALE" in out and "baxter_repair.py" in out, out
        assert depth() == 1
    finally:
        TASK_QUEUE, REJECT_LOG = real_queue, real_rej
        globals()["_log"], globals()["lane_journals"] = real_log, real_journals
    print("baxter_usage dup selftest OK: the 9th-July reword is refused by exit code and names "
          "the ticket it collided with, an in-flight build counts as a collision, a halt/park/"
          "fast-lane self-re-queue and a source_mid refinement are never refused, distinct tasks "
          "still queue, --dup-ok forces through, and a missing utils/*.py warns the ticket is stale.")


def selftest_edit():
    """Prove the queue can actually be EDITED (the owner, 9th July 09:21), and that the four
    faults behind "editing build PRDs is hard" are each closed:
      1. the verbs exist- --edit, --retext, --drop
      2. an entry is addressed by a stable id or an unambiguous substring, never exact prose
      3. priority moves DOWN as well as up
      4. the read-modify-write is locked, so a concurrent writer cannot vanish

    Drives main() as a caller does, against a scratch queue. Every outward path is stubbed-
    a selftest that reached _say once posted a false alert ([[selftests-stub-every-outward-path]])."""
    global TASK_QUEUE, REJECT_LOG
    import tempfile
    real_queue, real_log, real_rej = TASK_QUEUE, globals()["_log"], REJECT_LOG
    tmpdir = Path(tempfile.mkdtemp(prefix="baxter_edit_"))
    TASK_QUEUE, globals()["_log"] = tmpdir / "queue.json", lambda *a, **k: None
    REJECT_LOG = tmpdir / "rejects.jsonl"      # --prd-exempt logs every bypass; keep it scratch
    try:
        def _q(task, prio=5, touch="utils/baxter_usage.py/enqueue"):
            assert main(["--queue", task, "step", "--priority", str(prio), "--touch", touch,
                         "--prd-exempt"]) == 0, task
        def _one(needle):
            return queue_match(queue_read(), needle)[0]

        # 0. Every entry carries a stable id, and it survives an edit (fault 2's fix).
        _q("lane delegator rewrite", 3)
        first = _one("delegator")
        assert first and len(str(first["id"])) == 8, first

        # 1. DEMOTE. The headline fault: enqueue()'s min() could only ever promote, so a
        #    task could never be pushed down the order. --edit sets it flat, both ways.
        assert main(["--edit", "delegator", "--priority", "7"]) == 0
        assert _one("delegator")["priority"] == 7, "demote failed- priority is still promote-only"
        assert main(["--edit", "delegator", "--priority", "2"]) == 0
        assert _one("delegator")["priority"] == 2, "and it must still promote"
        assert _one("delegator")["id"] == first["id"], "an edit must not re-issue the id"

        # 2. RETEXT edits IN PLACE. --queue with new prose forked a duplicate beside the
        #    original, because enqueue() keys on the exact task string. Re-find it by ID,
        #    since the text it was found by no longer exists.
        assert main(["--edit", first["id"], "--retext", "lane delegator rewrite (v2)"]) == 0
        q = queue_read()
        assert len(q) == 1, f"retext forked a duplicate: {[e['task'] for e in q]}"
        assert q[0]["task"] == "lane delegator rewrite (v2)" and q[0]["id"] == first["id"]

        # 3. AMBIGUITY IS REFUSED, never resolved by guessing- editing the wrong build
        #    silently is the one failure worse than not editing at all.
        _q("lane delegator rewrite (v3)", 5)
        assert main(["--edit", "delegator", "--priority", "4"]) == 1, \
            "a needle matching two entries must be refused"
        assert [e["priority"] for e in queue_read()] == [2, 5], "and nothing may have moved"
        #    ...while the id, and an id PREFIX, stay unambiguous handles.
        assert main(["--edit", first["id"][:5], "--priority", "3"]) == 0
        assert _one(first["id"])["priority"] == 3
        assert main(["--edit", "(v3)", "--priority", "6"]) == 0, "an unambiguous substring works"
        assert main(["--edit", "no such task anywhere", "--priority", "6"]) == 1

        # 4. OWNER'S p1 PIN is not something a passing worker may quietly undo.
        assert main(["--edit", first["id"], "--priority", "1"]) == 0
        assert main(["--edit", first["id"], "--priority", "8"]) == 1, "a p1 pin needs --force"
        assert _one(first["id"])["priority"] == 1
        assert main(["--edit", first["id"], "--priority", "8", "--force"]) == 0
        assert _one(first["id"])["priority"] == 8, "--force lifts it"

        # 5. --queue STAYS PROMOTE-ONLY. halt() re-queues through enqueue(), so a build that
        #    halts twice must never demote itself out of the resume band.
        enqueue("lane delegator rewrite (v2)", "step", priority=2)
        assert _one(first["id"])["priority"] == 2, "a re-queue must promote"
        enqueue("lane delegator rewrite (v2)", "step", priority=7)
        assert _one(first["id"])["priority"] == 2, "a re-queue must NEVER demote- that is --edit's job"

        # 6. DROP. There was no way to remove an entry at all, at any priority.
        #    A substring needle now needs --force: it destroys whatever it happened to match,
        #    and on 9th July that was the wrong task (see selftest_drop). By id it needs none.
        assert main(["--drop", "(v3)"]) == 1, "a substring drop must refuse without --force"
        assert [e for e in queue_read() if "(v3)" in e["task"]], "and must have removed nothing"
        assert main(["--drop", "(v3)", "--force"]) == 0
        assert not [e for e in queue_read() if "(v3)" in e["task"]], "drop failed"
        assert main(["--drop", "(v3)", "--force"]) == 1, "dropping what is gone is an error, not a no-op"
        #    a p1 pin resists a drop exactly as it resists a demotion
        assert main(["--edit", first["id"], "--priority", "1", "--force"]) == 0
        assert main(["--drop", first["id"]]) == 1, "a p1 pin must not be droppable unforced"
        assert _one(first["id"]), "and the refused drop must have left it in place"
        assert main(["--drop", first["id"], "--force"]) == 0
        assert not queue_read(), "the queue must now be empty"

        # 7. A TEST FIXTURE queues without a touch-set and is force-gated, so the pump can
        #    never hand one a lane in the second it exists. This is what lets a verify
        #    command round-trip a real entry through the real queue file.
        assert main(["--queue", "__fixture_probe__", "x", "--priority", "2"]) == 0, \
            "a dunder fixture must be exempt from the mandatory touch-set"
        f = _one("__fixture_probe__")
        assert is_human_gated(f) and gate_of(f) == FIXTURE_TASK_GATE, f
        assert main(["--queue", "an ordinary build", "step"]) == 2, \
            "and the exemption must not leak- a real task still needs a declaration"
        assert main(["--drop", "__fixture_probe__"]) == 0 and not queue_read()

        # 8. THE LOCK EXISTS AND EXCLUDES (fault 4). Held, a second acquirer comes back empty.
        with queue_txn():
            assert _queue_lock().exists(), "queue_txn must actually create its lockfile"
            assert _lock_acquire(retries=1, delay=0.01, path=_queue_lock()) is None, \
                "the queue lock does not exclude a second holder"
        assert not _queue_lock().exists(), "and it must be released on the way out"

        # 9. THE PRD GATE does not care that the entry is otherwise perfect. It is ACCEPTED
        #    (exit 0 since 11th July- the ask is routed to the PM, and a routed ask is not a
        #    failure) but it is PARKED: gated on the PM, where no pump can ever reach it.
        assert main(["--queue", "an undocumented edit-selftest build", "step",
                     "--touch", "utils/a.py"]) == 0, \
            "a declared but PRD-less big task is routed to the PM, not reported as a failure"
        _u = _one("an undocumented edit-selftest build")
        assert gate_of(_u) == PRD_GATE and is_human_gated(_u), \
            f"a PRD-less build must be non-runnable, whatever the exit code says: {_u!r}"

        # 9. CONCURRENT WRITERS, for real: two PROCESSES enqueueing into one queue. Unlocked,
        #    this is a read-modify-write race and entries vanish with no error anywhere.
        #
        #    COUNTED RELATIVE TO THE BASELINE, never against a bare 30. The absolute number was
        #    only ever right because case 8's PRD-less refusal DESTROYED its entry; the moment
        #    a refused ask began parking itself (10th July) this read 31/30 and called a working
        #    lock a lost update. A fixture that encodes another bug's side effect breaks the day
        #    that bug is fixed, and points at the wrong file when it does.
        before = len(queue_read())
        child = ("import sys,pathlib;"
                 "sys.path.insert(0,sys.argv[1]);"
                 "import baxter_usage as g;"
                 "g.TASK_QUEUE=pathlib.Path(sys.argv[2]);"
                 "g._log=lambda *a,**k: None;"
                 "[g.enqueue(sys.argv[3]+'-'+str(i),'step',touch_set=['utils/a.py']) "
                 " for i in range(15)]")
        here = os.path.dirname(os.path.abspath(__file__))
        procs = [subprocess.Popen([sys.executable, "-c", child, here, str(TASK_QUEUE), tag])
                 for tag in ("alpha", "beta")]
        for p in procs:
            assert p.wait(timeout=120) == 0, "a concurrent writer crashed"
        q = queue_read()
        assert len(q) == before + 30, \
            f"lost update: {len(q) - before}/30 entries survived two concurrent writers"
        assert len({e["id"] for e in q}) == len(q), "ids must be unique across processes"
    finally:
        TASK_QUEUE, globals()["_log"], REJECT_LOG = real_queue, real_log, real_rej
    print("baxter_usage edit selftest OK: entries carry stable ids, --edit demotes and retexts "
          "in place, ambiguity is refused, --drop removes, a p1 pin needs --force, --queue stays "
          "promote-only, and two concurrent processes lose nothing.")


def selftest_drop():
    """Prove the 9th-July 13:12 mis-drop cannot happen again, and that no drop is permanent.

      1. THE INCIDENT. An id-shaped needle whose entry is GONE, but whose literal id sits
         inside ANOTHER task's body as provenance, resolves to NOTHING- never to that task.
      2. A real id, and an id prefix, still resolve in one hop.
      3. Prose is not regressed: exact text and substring still resolve, and the English
         words that happen to be hex (cafe, deed, beef) stay prose rather than becoming ids.
      4. Ambiguity is still refused rather than guessed.
      5. --drop on a substring match REFUSES without --force, and names what it matched.
      6. Every drop lands in an append-only store beside the queue and restores verbatim.
      7. A store that cannot be written ABORTS the drop instead of destroying the entry.

    Drives the real functions against a scratch queue. Every outward path is stubbed- a
    selftest that reached _say once posted a false alert ([[selftests-stub-every-outward-path]]).
    _live_lane_of is stubbed through the module globals, so the stub is reached the same way
    production reaches the real one.
    """
    global TASK_QUEUE
    import tempfile
    real_queue, real_log = TASK_QUEUE, globals()["_log"]
    real_say, real_lane_of = globals()["_say"], globals()["_live_lane_of"]
    tmpdir = Path(tempfile.mkdtemp(prefix="baxter_drop_"))
    TASK_QUEUE = tmpdir / "queue.json"
    globals()["_log"] = lambda *a, **k: None
    globals()["_say"] = lambda *a, **k: None
    globals()["_live_lane_of"] = lambda e: None
    try:
        GONE = "3af6a43a"        # already removed by a concurrent worker
        SURVIVOR = "179f667e"    # cites GONE in its own text, as provenance
        q = [
            {"id": SURVIVOR, "priority": 5, "queued_at": "2026-07-09T13:00:00",
             "task": f"root-cause fix for the governor force-kill; supersedes {GONE}"},
            {"id": "aa11bb22", "priority": 5, "queued_at": "2026-07-09T13:01:00",
             "task": "rebuild the fanout probe"},
            {"id": "beef1234", "priority": 6, "queued_at": "2026-07-09T13:02:00",
             "task": "cafe deed beef- prose that is pure hex, and must stay prose"},
        ]

        # 1. THE INCIDENT, replayed exactly. The old code returned q[0] here and deleted it.
        e, err = queue_match(q, GONE)
        assert e is None and err, f"an id-shaped stale needle resolved to a task: {e}"
        assert "id-shaped" in err, err
        assert SURVIVOR not in str(err), "the error must not point at the survivor as a match"

        # 2. THE SURVIVOR IS STILL REACHABLE, by its own id and by a prefix of it.
        e, err, how = queue_match(q, SURVIVOR, want_how=True)
        assert e is q[0] and not err and how == "id", (e, err, how)
        e, err, how = queue_match(q, SURVIVOR[:5], want_how=True)
        assert e is q[0] and not err and how == "id prefix", (e, err, how)
        assert queue_match(q, SURVIVOR)[0] is q[0], "the default arity is a caller contract"

        # 3. PROSE IS NOT REGRESSED- this is what would break if the id-shape rule were
        #    widened to bare `pure hex, >= 4`.
        e, err, how = queue_match(q, "rebuild the fanout probe", want_how=True)
        assert e is q[1] and how == "exact text", (e, err, how)
        e, err, how = queue_match(q, "fanout probe", want_how=True)
        assert e is q[1] and how == "substring", (e, err, how)
        for word in ("cafe", "deed", "beef", "face", "feed", "add", "ace"):
            assert not _is_id_shaped(word), f"{word!r} is prose, not an id"
        e, err, how = queue_match(q, "cafe deed", want_how=True)
        assert e is q[2] and how == "substring", (e, err, how)
        for handle in ("deadbeef", GONE, SURVIVOR, "aa11bb22", "beef1", "1234"):
            assert _is_id_shaped(handle), f"{handle!r} is an id handle"
        assert not _is_id_shaped("zz11bb22") and not _is_id_shaped("abc")

        # 4. AMBIGUITY REFUSES rather than guessing- unchanged behaviour.
        e, err = queue_match(q + [dict(q[1], id="cc22dd33")], "fanout probe")
        assert e is None and "matches 2 entries" in err, err

        # 5. A SUBSTRING DROP REFUSES, and says what it would have deleted.
        queue_write([dict(x) for x in q])
        assert len(queue_read()) == 3
        ok, msg = queue_drop("fanout probe")
        assert not ok and "force" in msg.lower(), (ok, msg)
        assert "aa11bb22" in msg and "rebuild the fanout probe" in msg, msg
        assert len(queue_read()) == 3, "a refused drop must not remove anything"
        #    ...and the stale id refuses too, naming no victim.
        ok, msg = queue_drop(GONE)
        assert not ok and SURVIVOR not in msg, (ok, msg)
        assert len(queue_read()) == 3, "the incident needle must delete nothing"
        #    Named by its id, it drops with no force at all.
        ok, msg = queue_drop("aa11bb22")
        assert ok, msg
        assert len(queue_read()) == 2, "an id drop removes exactly one"

        # 6. THE DROPPED ENTRY IS RECOVERABLE, verbatim, from a store beside THIS queue.
        store = _dropped_store()
        assert Path(store).parent == tmpdir, f"the store must follow TASK_QUEUE, not the vault: {store}"
        rec = _read_json(store) or []
        assert any(r["entry"]["id"] == "aa11bb22"
                   and r["entry"]["task"] == "rebuild the fanout probe"
                   and r.get("dropped_at") and r.get("how") == "id" for r in rec), rec
        ok, msg = queue_undrop("aa11bb22")
        assert ok, msg
        back = [e for e in queue_read() if e["id"] == "aa11bb22"]
        assert len(back) == 1 and back[0]["task"] == "rebuild the fanout probe", back
        assert back[0]["queued_at"] == "2026-07-09T13:01:00", "undrop must restore the entry verbatim"
        ok, msg = queue_undrop("aa11bb22")
        assert not ok and "already" in msg.lower(), ("restoring a live entry must refuse", ok, msg)
        ok, msg = queue_undrop("ffffffff")
        assert not ok, "undropping an id that was never dropped must refuse"

        # 7. AN UNWRITEABLE STORE ABORTS THE DROP. The net is not optional: a drop whose undo
        #    silently failed to write is the original fault with a green tick on it.
        real_record = globals()["_record_drop"]
        def _boom(*a, **k):
            raise OSError("dropped store is read-only")
        globals()["_record_drop"] = _boom
        try:
            ok, msg = queue_drop("aa11bb22")
            assert not ok and "not dropped" in msg.lower(), (ok, msg)
            assert len(queue_read()) == 3, "an aborted drop must leave the queue untouched"
        finally:
            globals()["_record_drop"] = real_record

        # 8. --force still destroys, on purpose, once the caller has seen what it matched.
        ok, msg = queue_drop("fanout probe", force=True)
        assert ok and "aa11bb22" in msg, msg
        assert len(queue_read()) == 2, "a forced drop removes exactly one"
        assert len([r for r in (_read_json(store) or []) if r["entry"]["id"] == "aa11bb22"]) == 2, \
            "the store is append-only- both drops of that id must be on the record"
    finally:
        TASK_QUEUE, globals()["_log"] = real_queue, real_log
        globals()["_say"], globals()["_live_lane_of"] = real_say, real_lane_of
    print("baxter_usage drop selftest OK: an id-shaped needle never matches task text, a "
          "substring drop refuses without --force and names its victim, every drop is appended "
          "to a store beside the queue and restores verbatim, and an unwriteable store aborts "
          "the drop rather than destroying the entry.")


def selftest_move():
    """Prove the queue can be REORDERED within a band (the owner, 9th July 09:29- the "cozy drag
    and drop" ask), and that a move can never do the one thing worse than not moving:

      1. the CLI reorders within a band- up, down, top, bottom, and an exact slot
      2. `--queue-list` then prints the NEW run order, with each entry's slot in its band
      3. `queued_at` is NEVER rewritten to fake a slot- `rank` carries the placement
      4. each of the three freezes REFUSES (live lane, p1 pin, human gate)...
      5. ...and a refused move leaves the run order byte-identical, rather than reflowing
         the band on its way to saying no

    Drives main() as a caller does, against a scratch queue. Every outward path is stubbed-
    a selftest that reached _say once posted a false alert ([[selftests-stub-every-outward-path]]).
    _live_lane_of is stubbed through the module globals, which is also the assertion that
    queue_move looks it up there rather than through a local alias a stub would miss."""
    global TASK_QUEUE, REJECT_LOG
    import io, tempfile
    real_queue, real_log, real_rej = TASK_QUEUE, globals()["_log"], REJECT_LOG
    real_lane = globals()["_live_lane_of"]
    tmpdir = Path(tempfile.mkdtemp(prefix="baxter_move_"))
    TASK_QUEUE, globals()["_log"] = tmpdir / "queue.json", lambda *a, **k: None
    REJECT_LOG = tmpdir / "rejects.jsonl"      # --prd-exempt logs every bypass; keep it scratch
    globals()["_live_lane_of"] = lambda e: None
    try:
        def _m(*args):
            """Drive main() exactly as a shell caller does, capturing what it prints so the
            OUTPUT can be asserted on too- a refusal that exits 1 but says nothing useful is
            still a bad refusal. Returns (exit_code, stdout)."""
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(list(args))
            return rc, buf.getvalue()

        def _q(task, prio=5, gate=None):
            a = ["--queue", task, "step", "--priority", str(prio), "--prd-exempt",
                 "--touch", f"utils/x_{task}.py"]
            assert _m(*(a + (["--gate", gate] if gate else [])))[0] == 0, task

        def _order():
            return [e["task"] for e in queue_read()]

        for t in ("alpha", "bravo", "charlie", "delta"):
            _q(t)
        assert _order() == ["alpha", "bravo", "charlie", "delta"], \
            f"unranked arrivals must stay FIFO: {_order()}"

        # 1. THE FOUR MOVES. Each is applied to the band, not the global list.
        rc, out = _m("--move", "charlie", "up")
        assert rc == 0 and _order() == ["alpha", "charlie", "bravo", "delta"], (out, _order())
        assert "before:" in out and "after:" in out, f"a move must show the run order it changed:\n{out}"
        assert _m("--move", "alpha", "bottom")[0] == 0
        assert _order() == ["charlie", "bravo", "delta", "alpha"], _order()
        assert _m("--move", "delta", "top")[0] == 0
        assert _order() == ["delta", "charlie", "bravo", "alpha"], _order()
        assert _m("--move", "bravo", "2")[0] == 0, "an exact 1-based slot must land"
        assert _order() == ["delta", "bravo", "charlie", "alpha"], _order()
        #    'up' at the top is a no-op, not an error- a button he can always press
        rc, out = _m("--move", "delta", "up")
        assert rc == 0 and "nothing moved" in out, out
        assert _order() == ["delta", "bravo", "charlie", "alpha"], _order()

        # 2. --queue-list PRINTS THE NEW RUN ORDER, with the slot that made it.
        rc, out = _m("--queue-list")
        assert rc == 0
        seen = [out.index(t) for t in _order()]
        assert seen == sorted(seen), f"--queue-list does not print the new run order:\n{out}"
        assert "slot 1/4" in out and "slot 4/4" in out, f"no slot-in-band shown:\n{out}"

        # 3. queued_at IS NEVER REWRITTEN. Faking a timestamp to buy a slot would pass a naive
        #    order check while destroying the record of when the task actually arrived.
        stamps = {e["task"]: e["queued_at"] for e in queue_read()}
        assert _m("--move", "alpha", "top")[0] == 0
        assert {e["task"]: e["queued_at"] for e in queue_read()} == stamps, \
            "a move rewrote queued_at to fake a slot"

        # 4a. FREEZE: LIVE IN A LANE. Never forceable- the build is already running.
        globals()["_live_lane_of"] = lambda e: "3"
        snap = _order()
        rc, out = _m("--move", "bravo", "top")
        assert rc == 1 and "lane 3" in out, f"a live-lane task must refuse a move, saying why: {out}"
        assert _order() == snap, "a refused move must not reorder"
        assert _m("--move", "bravo", "top", "--force")[0] == 1, "and --force must not lift it"
        assert _order() == snap, "a refused move must not reorder"
        globals()["_live_lane_of"] = lambda e: None

        # 4b. FREEZE: OWNER'S p1 PIN. --force lifts it, exactly as it does for --edit/--drop.
        assert _m("--edit", "delta", "--priority", "1")[0] == 0
        _q("echo", 1)                       # a second p1, so 'down' is even expressible
        assert _order()[:2] == ["delta", "echo"], _order()
        snap = _order()
        rc, out = _m("--move", "delta", "down")
        assert rc == 1 and "priority 1" in out, f"a p1 pin must refuse a move, saying why: {out}"
        assert _order() == snap, "a refused move must not reorder"
        assert _m("--move", "delta", "down", "--force")[0] == 0, "--force lifts the p1 refusal"
        assert _order()[:2] == ["echo", "delta"], _order()

        # 4c. FREEZE: HUMAN-GATED. Its slot decides nothing until he says go.
        _q("foxtrot", 5, gate="owner")
        snap = _order()
        rc, out = _m("--move", "foxtrot", "top")
        assert rc == 1 and "gated on owner" in out, f"a gated entry must refuse a move: {out}"
        assert _order() == snap, "a refused move must not reorder"
        assert _m("--move", "foxtrot", "top", "--force")[0] == 0, "--force lifts the gate refusal"

        # 5. A BAD MOVE IS REFUSED, never guessed at, and neither is a bad needle.
        snap = _order()
        rc, out = _m("--move", "charlie", "sideways")
        assert rc == 1 and "sideways" in out, out
        assert _m("--move", "no such build at all", "up")[0] == 1
        rc, out = _m("--move", "charlie")
        assert rc == 1 and "up|down|top|bottom" in out, f"a missing direction must print usage: {out}"
        assert _order() == snap, "a refused move must not reorder"

        # 6. A RANK BELONGS TO ONE BAND. Re-prioritised, an entry must re-enter the new band at
        #    the back- not jump every unranked task there on a placement made somewhere else.
        assert queue_read()[0]["task"] == "echo"
        assert _m("--edit", "charlie", "--priority", "1")[0] == 0
        assert "rank" not in queue_match(queue_read(), "charlie")[0], \
            "a re-prioritised entry carried its old band's rank into the new one"
        assert _order()[:3] == ["echo", "delta", "charlie"], \
            f"a re-prioritised entry must enter its new band at the back: {_order()}"
    finally:
        TASK_QUEUE, globals()["_log"], REJECT_LOG = real_queue, real_log, real_rej
        globals()["_live_lane_of"] = real_lane
    print("baxter_usage move selftest OK: --move reorders within a band (up/down/top/bottom/slot), "
          "--queue-list prints the new order with slots, queued_at is never rewritten, a live lane "
          "/ p1 pin / human gate each refuse without reordering, and a rank never crosses bands.")


def selftest_clash():
    """Prove clash() sees one file through both its spellings- the 9th-July hole."""
    scripts = _norm_touch(str(Path(os.path.dirname(os.path.abspath(__file__))).parent))
    vault = _norm_touch(str(VAULT))

    # THE BUG, exactly as it was found live: lane 2 held the vault's 50-Research directory
    # by its absolute path; the orchestration lane asked for a note inside it by a relative
    # one. The self-check said "clear". It must now say they collide.
    lane2 = {_norm_touch(f"{vault}/50-Research")}
    mine = {_norm_touch("50-Research/a note.md")}
    assert clash(lane2, mine), "a relative path inside an absolutely-declared dir must clash"
    assert clash(mine, lane2), "and the collision must be symmetric"

    # The same trap on the file every Baxter build actually edits.
    rel = {_norm_touch("utils/baxter_usage.py")}
    ab = {_norm_touch(f"{scripts}/utils/baxter_usage.py")}
    assert clash(rel, ab) == "both touch utils/baxter_usage.py", clash(rel, ab)
    assert clash(ab, rel), "symmetric"

    # ...and the dir/file containment still works across the two spellings.
    assert clash({_norm_touch(f"{scripts}/utils")}, rel), "a file inside an absolute dir clashes"

    # NOTHING THAT USED TO BE PARALLEL MAY SILENTLY SERIALISE. Different files stay apart,
    # two regions of one file stay apart, and unrelated clusters stay apart.
    assert clash({"utils/baxter_orch.py"}, {"utils/baxter_crashdump.py"}) is None
    assert clash({f"{scripts}/utils/baxter_orch.py"}, {"utils/baxter_crashdump.py"}) is None
    assert clash({"utils/baxter_usage.py/lane_capacity"}, {"utils/baxter_usage.py/ceiling"}) is None, \
        "two regions of one file must still run in parallel"
    assert clash({"utils/baxter_usage.py/ceiling"}, {"utils/baxter_usage.py"}), \
        "a region and a whole-file lock must still clash- the file lock is the coarser claim"
    assert clash({"@lanes"}, {"@system"}) is None
    assert clash({"@lanes"}, {"@lanes"}) == "shared cluster @lanes"
    assert clash(set(), {"utils/x.py"}), "an undeclared touch-set still runs solo"

    # The live pairing of the two lanes that found this: they must NOT be forced to
    # serialise by the fix. A correctness fix that stops all parallelism is not a fix.
    orch = {"@lanes", "utils/baxter_usage.py", "utils/baxter_triage.py", "utils/baxter_orch.py"}
    crash = {"@system", f"{vault}/.baxter_crashdumps", f"{scripts}/utils/baxter_crashdump.py"}
    assert clash({_norm_touch(x) for x in orch}, {_norm_touch(x) for x in crash}) is None, \
        "the orchestration and crash-dump lanes touch nothing in common and must stay parallel"

    print("baxter_usage clash selftest OK: one file is one file whether declared relatively "
          "or absolutely, regions of a file still run in parallel, and unrelated lanes are "
          "not serialised by the fix.")


def _lane_id(rf):
    """Lane index out of a `resume-<ts>-<lane>.json` name (survives `.retry` renames)."""
    try:
        return int(Path(rf).name.split(".")[0].rsplit("-", 1)[1])
    except Exception:
        return 0


def lane_label(lane):
    """Human lane number: lanes read as 1..LANE_COUNT, never lane 0 (the owner, 9th July).
    Internals stay zero-indexed- journal filenames, next_lane_id and the delegator are
    untouched- so EVERY surface he reads must render through this. A non-numeric lane
    ('?') passes through as-is rather than inventing a number."""
    try:
        return int(lane) + 1
    except (TypeError, ValueError):
        return lane if lane not in (None, "") else "?"


def _lane_started(rf, entry):
    return str(entry.get("started_at") or "") or Path(rf).name


# ---- LANE LIVENESS: a dead lane must not report itself alive (9th July 01:37) ----
# lane_journals() used to count every `resume-*.json` that was not `.failed.json`- no
# process check, no age check. The two-lane build died at 01:37 and `--lanes` went on
# printing "1/1 live" against a journal whose mtime had frozen at the moment of death.
# The 30-minute orphan sweep in baxter_triage does clear such a corpse, but half an hour
# is forever during the unattended drain the owner asked for: EVERY failure stalls a lane while
# every readout claims it is working. A verify gate on a lane nobody knows is dead never
# runs, so this is the leg that makes the other three matter overnight.
#
# A live worker heartbeats its journal's mtime every 60s. Two missed beats plus slack =
# a corpse. The recorded pid gives a faster answer still: a dead process is dead now, not
# in 150 seconds. Both tests fail SAFE- any doubt reads as alive, because reaping a live
# build costs far more than waiting one more beat for a dead one.
LANE_DEAD_AFTER = 150   # seconds without a heartbeat (60s beat) before a journal is a corpse
LANE_PID_GRACE = 25     # a just-spawned worker gets this long to exist before its pid is judged


def _pid_alive(pid):
    """Is that Windows pid still running? Unknown -> True; we never reap on a maybe.
    (os.kill is not an option here: on Windows it TERMINATES the process rather than
    signalling it, so a liveness probe would become a kill.)"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, int(pid))   # SYNCHRONIZE
        if not h:
            return k.GetLastError() != 87   # 87 = ERROR_INVALID_PARAMETER = no such pid
        alive = k.WaitForSingleObject(h, 0) != 0         # WAIT_OBJECT_0 (0) = it exited
        k.CloseHandle(h)
        return alive
    except Exception:
        return True


def lane_alive(rf, entry):
    """True while the worker behind this journal is still breathing.

    The recorded PID is the authority when we have one: a lane IS its process. Falling
    back to the heartbeat only when there is no pid matters in both directions- it reaps
    a dead worker in ~25s instead of 150, and it never reaps a LIVE three-hour build whose
    heartbeat thread happened to die (mtime alone would call that a corpse and start a
    duplicate on top of it). Pid reuse could keep a corpse looking alive; the 30-minute
    orphan sweep is still there as the belt for exactly that."""
    try:
        age = time.time() - os.path.getmtime(rf)
    except OSError:
        return True     # can't stat it- never reap on a bad read
    pid = (entry or {}).get("pid")
    if pid:
        if age <= LANE_PID_GRACE:
            return True             # just spawned- give the process time to exist
        return _pid_alive(pid)
    return age <= LANE_DEAD_AFTER   # legacy journal, no pid- two missed heartbeats = dead


def lane_journals(alive_only=True):
    """Every LIVE lane: [(journal_path, entry_or_None)]. A `.failed.json` (shelved) or
    `.parked.json` (troubleshot to a standstill, awaiting the owner) journal is a corpse, not
    a lane, and neither is one whose worker stopped heartbeating. An UNREADABLE journal
    (caught mid-write) still counts as a lane with entry=None: it occupies a slot and,
    having no readable touch-set, blocks a second lane from opening- the safe direction.
    The delegator skips such pairs rather than yielding a live build over a transient read."""
    out = []
    try:
        for rf in sorted(RESUME_DIR.glob("resume-*.json")):
            if rf.name.endswith((".failed.json", ".parked.json")):
                continue
            entry = _read_json(rf)
            if alive_only and not lane_alive(rf, entry):
                continue
            out.append((rf, entry))
    except Exception:
        pass
    return out


def _never_had_a_body(entry):
    """True when a journal describes a build that NEVER RAN, so there is no corpse to find.

    Two shapes: the governor HELD it before it spawned (`held`), or it has no pid at all-
    a legacy journal, or one written a heartbeat before its Popen. Either way it did not
    die, so classifying it as a silent death "repairs" a task that never started. On 9th
    July five held lanes were reaped that way, each inheriting a stale 4294967295 exit
    from an EARLIER real death and spawning a repair worker with nothing to repair.

    An UNREADABLE journal (entry=None) deliberately does NOT match: the reaper must still
    shelve those. Written as an explicit isinstance rather than `(entry or {}).get(...)`,
    which would swallow them and strand them silently for ever."""
    return isinstance(entry, dict) and (bool(entry.get("held")) or not entry.get("pid"))


def held_lanes():
    """Builds the governor is holding: [(journal_path, entry)]. Not lanes (no process) and
    not corpses (nothing died)- the 30-minute sweep respawns them once the band clears.
    The reaper logs them so a held build is visible rather than merely un-reaped."""
    out = []
    try:
        for rf in sorted(RESUME_DIR.glob("resume-*.json")):
            if rf.name.endswith((".failed.json", ".parked.json")):
                continue
            entry = _read_json(rf)
            if isinstance(entry, dict) and entry.get("held"):
                out.append((rf, entry))
    except Exception:
        pass
    return out


def dead_lanes():
    """Corpses holding a lane open: [(journal_path, entry_or_None)]. The reaper in
    baxter_triage hands their slot back to the pump and routes each into the failure
    classifier, instead of the blind 30-minute respawn."""
    live = {str(rf) for rf, _e in lane_journals()}
    out = []
    try:
        for rf in sorted(RESUME_DIR.glob("resume-*.json")):
            if rf.name.endswith((".failed.json", ".parked.json")) or str(rf) in live:
                continue
            entry = _read_json(rf)
            if _never_had_a_body(entry):
                continue
            out.append((rf, entry))
    except Exception:
        pass
    return out


def next_lane_id(used):
    for i in range(LANE_COUNT):
        if i not in used:
            return i
    return len(used)


def yield_marker(rf):
    """Sidecar file that tells a lane to stand down. Deliberately NOT `*.json`, so it
    can't be mistaken for a journal by the pump, the orphan sweep or _build_active()."""
    return Path(str(rf) + ".yield")


def gate_marker(rf):
    """Sidecar recording the LAST time this lane reached its `--check project --lane` gate.
    Deliberately NOT `*.json` (same reason as yield_marker- the pump, the orphan sweep and
    _build_active only ever glob `*.json`). The governor reads it to tell a lane that has
    genuinely IGNORED a yield- it hit its gate AFTER the marker dropped and stayed alive- from
    one that simply has not reached its next gate yet. The 120s grace force-killed 12 of 15
    healthy lanes on 10th July precisely because it had no way to draw that distinction."""
    return Path(str(rf) + ".gate")


def _stamp_gate_check(rf):
    """Record now on the lane's .gate sidecar every time it reaches the gate. Best-effort: a
    failure here must never break the gate check, which is the vital path it rides."""
    try:
        gate_marker(rf).write_text(
            datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except Exception:
        pass


# ---- THE FLEET: lanes have WIDTH, not just a count (9th July) ----
# A lane lead may fan its plan out across sub-workers (baxter_orch.fanout). Those are
# real concurrent claude processes, and until now NOTHING counted them: LANE_COUNT
# bounded the leads and nothing at all bounded the fleet. Four leads x three subs is
# sixteen Opus spawns against a governor that believed it had authorised four.
#
# So a lead DECLARES its width into its own lane journal (`subs`), and everything that
# reasons about concurrency counts leads + subs against WORKER_BUDGET.
WORKER_BUDGET = 13     # total concurrent build processes (leads + their sub-workers).
                       # >= LANE_COUNT + MAX_SUBS_PER_LEAD, always. `>= LANE_COUNT` is NOT
                       # enough, and that weaker rule is exactly how the 6-lane raise nearly
                       # shipped starved: lane_capacity() is live + (BUDGET - fleet), and one
                       # lead fanning out 3-wide spends 4 of the budget on its own. At 10 lanes
                       # and a budget of 11 that caps capacity at 8, so the top two lanes could
                       # never open once ANY lead split its work- while `--lanes` cheerfully
                       # printed LANE_COUNT=10. 13 = the 10 leads, plus room for ONE of them to
                       # run a full-width 3-sub fan-out while the other nine still hold a slot each.
                       # MEASURED AND LEFT AT 13 (9th July, 18:41): a full board read 10 leads,
                       # 0 subs, fleet 10, 3 free- the docstring's arithmetic exactly. Raising it
                       # is the cheapest way to make any concurrency number go up and buys nothing
                       # but a faster sprint into the 80% wall, where every in-flight lane halts.
MAX_SUBS_PER_LEAD = 3  # one lead may never eat the whole fleet, however idle it looks.


def _subs_live(entry):
    """Is a journal's declared `subs` count still honoured by a living process?

    A fan-out killed mid-flight leaves `subs: 3` behind, and a `.retry` respawn reuses
    the SAME journal- so a corpse's phantom subs would spend the budget forever and the
    pump would starve to zero lanes, which is worse than the bug this counting fixes.
    `register_width` therefore leaves a receipt (`subs_pid`, the fan-out process). No
    receipt at all -> trust the number (never invent one); a receipt whose process is
    dead -> the count is a ghost, worth nothing. Fail OPEN, in both directions."""
    pid = (entry or {}).get("subs_pid")
    return True if pid is None else _pid_alive(pid)


def lane_subs(entry):
    """A lane's HONOURED sub-worker count: what it declared, minus what died.

    The one definition of a lane's width. fleet_workers() spends the budget on this and
    lanes_report() prints it, so a readout can never disagree with the arithmetic that
    starves the pump. Reading `entry['subs']` raw anywhere else re-opens the ghost-sub
    bug: a fan-out killed mid-flight leaves its count in the journal, and only
    _subs_live()'s receipt check can tell that number from a live one."""
    e = entry or {}
    try:
        subs = max(0, int(e.get("subs") or 0))
    except (TypeError, ValueError):
        subs = 0
    return subs if (not subs or _subs_live(e)) else 0


def fleet_workers():
    """Concurrent build processes right now: one lead per live lane, plus each lead's
    declared sub-workers. An unreadable or `subs`-less journal contributes its lead and
    nothing more- a missing count is 0, never an assumed 3.

    IT COUNTS JOURNALS, AND THAT IS CORRECT (measured 9th July, 18:41). The tempting
    "count real processes instead" rewrite is a trap twice over. A lead cannot fan out
    uncounted: `baxter_orch.fanout()` calls `register_width()` BEFORE it spawns, and
    `fanout_width()` below now reserves the slots before it even answers. And each lead
    python owns exactly ONE claude.exe child, so a `1 + descendants` tree-walk would
    double-count every lane- a healthy 10-lane board reporting a fleet of 20 against a
    budget of 13, starving the pump to one lane. The census that "proved" four processes
    were unaccounted had counted two RESIDENT powershell-parented claude sessions as build
    workers. A Win32_Process query per lane per watcher beat would also stall the very pump
    this number gates. See `50-Research/2026-07-09 - Lane concurrency- what is actually serial.md`."""
    return sum(1 + lane_subs(e) for _rf, e in lane_journals())


def _free_slots():
    """Budget left for NEW processes, always reserving the caller's own slot. The `max`
    matters: a lead asking before its own journal is visible (or a test with an empty
    fleet) must still not count itself as free headroom."""
    return max(0, WORKER_BUDGET - max(fleet_workers(), 1))


def _own_journal():
    """This process's OWN lane journal, or None. A lead runs `baxter_orch.fanout()` inside
    the same python process that `_spawn_resume` recorded as the lane's `pid`, so identity
    needs no argument threaded through orch- which is another lane's file to edit.
    `register_width` already stamps `subs_pid = os.getpid()` on exactly this assumption."""
    me = os.getpid()
    for rf, e in lane_journals():
        if isinstance(e, dict) and e.get("pid") == me:
            return rf
    return None


def _reserve_subs(rf, n):
    """Spend `n` sub-worker slots into a lane journal, leaving the receipt `_subs_live`
    checks. Same keys and same shape as `baxter_orch.register_width`, so the lead's later
    `register_width(rf, n)` (n <= the reservation) NARROWS the claim rather than fighting
    it, and its `finally: register_width(rf, 0)` is what releases it.

    Best-effort: a failed reservation must never abort a build. It only means the slots
    stay visibly free, which is the pre-existing behaviour, not a new failure."""
    try:
        entry = _read_json(rf)
        if not isinstance(entry, dict):
            return False
        entry["subs"] = max(0, int(n))
        if n:
            entry["subs_pid"] = os.getpid()
        else:
            entry.pop("subs_pid", None)   # cleared, not zeroed: no ghost to misread
        # temp name must not match resume-*.json, or lane_journals() catches it mid-write
        tmp = Path(str(rf) + ".resv.tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(rf))
        return True
    except Exception:
        return False


def fanout_width(entry=None, rf=None):
    """How many sub-workers this lead may run RIGHT NOW, RESERVED before it answers.
    Replaces orch's hardcoded MAX_PARALLEL, which consulted nothing.

    It asks the BAND before it does slot arithmetic. Width multiplies concurrent Opus
    spawns, so a fleet that happens to look idle at 85% must not fan out into the wall:
    the big-stop that pauses one build pauses a lead's grunt-workers with it.

    THE GRANT IS ATOMIC (9th July). It used to READ `_free_slots()` and return the number,
    while `register_width()` wrote it some unbounded time later- so three leads asking in
    the same instant each saw the same 3 free slots and each was told it could run 3, which
    authorises 9 processes against 3. That count-then-spend window WAS the bug. The slots
    are now spent into the caller's own journal under USAGE_LOCK before the number is
    returned, so the second lead's `_free_slots()` already sees them gone.

    A caller with no journal (a test, `--lanes`, a stale import) reserves nothing and is
    told the plain number- exactly `register_width`'s contract for a journal-less caller.
    The band checks run OUTSIDE the lock: `blocked()` and `read_meters()` touch the very
    files USAGE_LOCK guards, and a gate must never deadlock."""
    b, _why = blocked("big")
    if b:
        return 0
    pct = float(read_meters().get("session_pct") or 0)
    if pct >= ceiling(None):
        return 0
    fd = _lock_acquire()
    try:
        width = min(MAX_SUBS_PER_LEAD, _free_slots())
        if width:
            target = rf or _own_journal()
            if target is not None:
                _reserve_subs(target, width)
    finally:
        _lock_release(fd)
    return width


def lane_capacity():
    """How many LANES may run right now (the pump does `free = lane_capacity() - live`).

    All lanes share ONE gate- the big-stop that already decides whether any build runs.
    Below it the builder defaults to splitting; at or above it the pump has stopped
    starting work anyway, so the `1` is only a floor for a lane already in flight.
    HEADROOM_2ND is a knob, now 0: see its definition for why a second, stricter
    threshold quietly reduced this to a one-lane builder.

    What is new: a lane is no longer worth one process. A lead that has fanned out
    SPENDS the fleet budget, so opening a fresh lane onto a saturated fleet is refused
    even though a lane number is free.

    Newer still (9th July): the 60% SOFT STOP. At or above it the lanes DRAIN- capacity
    is the live lane count EXACTLY, so `free` comes out at zero and no new lane opens,
    while every build already running finishes untouched. It returns before the trailing
    `max(1, ...)` on purpose: that floor exists to keep an in-flight lane's own capacity
    from reading zero, and applying it here would open one lane on an empty board at the floor
    and make the whole band a no-op."""
    global _SOFT_STOP_LAST
    snap = read_meters()
    pct = float(snap.get("session_pct") or 0)
    soft, why = soft_stopped(snap)
    if soft != _SOFT_STOP_LAST:                 # log the FLIP, never the beat
        first = _SOFT_STOP_LAST is None         # ...and never a spurious 'LIFTED' at startup
        _SOFT_STOP_LAST = soft
        if soft:
            _log(why)
        elif not first:
            _log(f"soft-stop LIFTED- new lanes may open again (session {pct}%)")
    if soft:
        return len(lane_journals())             # exactly the live count- 0 lanes means 0
    if pct >= ceiling(None) - HEADROOM_2ND:
        return 1
    live = len(lane_journals())
    free = max(0, WORKER_BUDGET - fleet_workers())
    return max(1, min(LANE_COUNT, live + free))


def lanes_report():
    """What `--lanes` must show: (fleet, WORKER_BUDGET, [(journal, entry, honoured_subs)]).

    Pure- no printing, no meters, no network- so a test can drive it and main() cannot
    smuggle a side effect into the readout. the owner reads lane COUNT everywhere and fleet
    WIDTH nowhere, which is how ten lanes fanning out three-wide could quietly spend
    forty Opus spawns against a budget of thirteen. The fleet number is fleet_workers()
    itself, never len(lanes) and never LANE_COUNT: on a FULL board with no lead fanned
    out, those coincidentally render the same `10/13`, so a line faked from len(lanes)
    would look right on the day it was written and lie the moment a lead split its work."""
    return fleet_workers(), WORKER_BUDGET, [(rf, e, lane_subs(e)) for rf, e in lane_journals()]


def lane_width_line(subs):
    """The one phrasing of a lane's width, so the readout can't drift from lane_subs()."""
    return f"lead + {subs} subs" if subs else "lead only"


def selftest_lanes():
    """Prove `--lanes` shows fleet WIDTH, and that a GHOST sub-worker is never counted.

    Drives main(['--lanes']) against a scratch RESUME_DIR with a stubbed meter, so nothing
    touches the network and no outward path is reachable ([[selftests-stub-every-outward-path]])."""
    global RESUME_DIR
    import io, tempfile, contextlib
    real_dir, real_meters, real_log = RESUME_DIR, globals()["read_meters"], globals()["_log"]
    d = Path(tempfile.mkdtemp(prefix="baxter_lanes_"))
    RESUME_DIR = d
    globals()["read_meters"] = lambda *a, **k: {"session_pct": 10.0}
    globals()["_log"] = lambda *a, **k: None
    try:
        me = os.getpid()
        # A plain lead; a lead whose fan-out is ALIVE; a lead whose fan-out is a CORPSE.
        (d / "resume-20260709-000000-0.json").write_text(json.dumps({"task": "alpha", "pid": me}))
        (d / "resume-20260709-000000-1.json").write_text(
            json.dumps({"task": "beta", "pid": me, "subs": 3, "subs_pid": me}))
        (d / "resume-20260709-000000-2.json").write_text(
            json.dumps({"task": "gamma", "pid": me, "subs": 2, "subs_pid": 999999}))

        assert lane_subs(_read_json(d / "resume-20260709-000000-1.json")) == 3
        assert lane_subs(_read_json(d / "resume-20260709-000000-2.json")) == 0, \
            "a sub-count behind a DEAD subs_pid is a ghost- it spends no budget and prints as none"
        assert lane_subs(None) == 0 and lane_subs({"subs": "three"}) == 0 and lane_subs({"subs": -4}) == 0

        fleet, budget, rows = lanes_report()
        # The 6 is this FIXTURE's fleet- its three journals spend 1 + (1+3) + 1 processes-
        # and has nothing to do with LANE_COUNT. Only the budget tracks the constant, so a
        # raise moves that side alone. Pinning the 6 to LANE_COUNT would silently stop this
        # test proving that fleet_workers() counts a live fan-out's WIDTH.
        assert (fleet, budget) == (fleet_workers(), WORKER_BUDGET) == (6, WORKER_BUDGET), (fleet, budget)
        assert [s for _rf, _e, s in rows] == [0, 3, 0], rows

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--lanes"])
        out = buf.getvalue()
        assert rc == 0, out
        # Lanes read 1..10 to the owner; the zero-indexed journal names stay internal.
        assert "[lane 1]" in out and "[lane 2]" in out and "[lane 3]" in out, out
        assert "[lane 0]" not in out, "a lane must never print as lane 0"
        assert f"6/{WORKER_BUDGET}" in out.replace(" ", ""), out
        # The width sits on the lane it belongs to, and the ghost never appears anywhere.
        beta = out.split("[lane 2]")[1].split("[lane")[0]
        assert "lead + 3 subs" in beta, out
        assert "lead + 2 subs" not in out, "the dead fan-out was printed as live width"
        assert out.split("[lane 3]")[1].split("[lane")[0].count("lead only") == 1, out
    finally:
        RESUME_DIR = real_dir
        globals()["read_meters"], globals()["_log"] = real_meters, real_log
    print("baxter_usage lanes selftest OK: --lanes prints each lane's honoured width and the "
          "fleet against WORKER_BUDGET; a ghost fan-out counts 0 and reads 'lead only'.")


def selftest_softstop():
    """Prove the DRAIN band: no NEW lane opens, no in-flight lane is cut, breach lifts it.

    Every meter, lane and authorisation is stubbed, so nothing touches the network, the
    real journals or any outward path ([[selftests-stub-every-outward-path]])."""
    global _SOFT_STOP_LAST
    g = globals()
    held = {k: g[k] for k in ("read_meters", "fleet_workers", "lane_journals", "_log",
                              "_breach_active", "_override_active", "_breach_step_active")}
    logged = []
    pct = [10.0]
    lanes = [[]]
    g["read_meters"] = lambda *a, **k: {"session_pct": pct[0]}
    g["fleet_workers"] = lambda *a, **k: 0
    g["lane_journals"] = lambda *a, **k: list(lanes[0])
    g["_log"] = lambda m: logged.append(m)
    for k in ("_breach_active", "_override_active", "_breach_step_active"):
        g[k] = lambda *a, **k: None
    _SOFT_STOP_LAST = None
    try:
        # under the band: every lane openable, and NOT ONE log line (a beat is not an event)
        assert lane_capacity() == LANE_COUNT, lane_capacity()
        assert logged == [], f"a quiet governor beat logged: {logged}"

        # in the band, empty board: ZERO. This is the max(1, ...) floor being bypassed-
        # a `1` here opens a lane at the drain floor and the whole band is a no-op.
        # DERIVED from the constant, never a literal: a hard-coded percentage would fall OUT
        # of the band the day the drain floor moves and turn this case green-but-meaningless.
        pct[0] = SOFT_STOP_SESSION + 5.0
        assert lane_capacity() == 0, f"soft stop opened a lane on an empty board: {lane_capacity()}"
        assert len(logged) == 1 and "lanes draining" in logged[0], logged
        assert lane_capacity() == 0 and len(logged) == 1, \
            f"the drain logged twice- it must latch on the flip, not the beat: {logged}"

        # in the band with work in flight: capacity == the live count exactly, so the pump's
        # free = cap - live is 0. Nothing is killed, nothing new starts.
        lanes[0] = [1, 2]
        assert lane_capacity() == 2, f"an in-flight lane was cut: {lane_capacity()}"
        lanes[0] = [1, 2, 3, 4, 5]
        assert lane_capacity() == 5, lane_capacity()

        # boundary: a tenth below the floor runs, the floor itself drains (inclusive at its
        # floor). Written OFF the constant, never off a literal: a hard-coded 59.9 would go
        # on passing while testing nothing the day the drain floor moves.
        lanes[0] = []
        pct[0] = SOFT_STOP_SESSION - 0.1
        assert lane_capacity() == LANE_COUNT, f"{SOFT_STOP_SESSION - 0.1}% must still open a lane"
        pct[0] = SOFT_STOP_SESSION
        assert lane_capacity() == 0, f"{SOFT_STOP_SESSION}% is inside the band, not below it"

        # A point INSIDE the drain band, derived- a hard-coded literal could fall outside the
        # band the day the drain floor moves, so the re-seal assertion below would pass for the
        # wrong reason (never stopped). Derive it off the constant so it tracks the band.
        in_band = SOFT_STOP_SESSION + 5.0
        assert SOFT_STOP_SESSION < in_band < BIG_STOP_SESSION, "the fixture must sit inside the drain band"

        # each of the three authorisations lifts it, one at a time
        pct[0] = in_band
        for key in ("_breach_active", "_override_active", "_breach_step_active"):
            g[key] = lambda *a, **k: 30.0
            assert soft_stopped()[0] is False, f"{key} did not lift the soft stop"
            assert lane_capacity() == LANE_COUNT, f"{key} lifted the stop but the pump opened nothing"
            g[key] = lambda *a, **k: None
        assert lane_capacity() == 0, "the stop did not re-seal once the authorisation lapsed"

        # the 80% and 90% HARD bands are untouched, and a mid-drain % still writes NO stop flag:
        # enforce() would kill in-flight workers on a band here- the drain must not be one.
        assert _band({"session_pct": in_band}) is None, "the drain band minted a stop flag- that is a kill"
        assert _band({"session_pct": 85.0}) == "big"
        assert _band({"session_pct": 92.0}) == "routine"
        assert SOFT_STOP_SESSION not in SESSION_TIERS, "the drain floor as a tier makes /breach step to it"
        assert SOFT_STOP_SESSION not in SESSION_PING_BANDS, "the drain floor as a ping band machine-guns a ping"

        # the drain announces itself in both directions, once each
        logged.clear()
        _SOFT_STOP_LAST = None
        assert lane_capacity() == 0 and len(logged) == 1
        pct[0] = 20.0
        assert lane_capacity() == LANE_COUNT
        assert len(logged) == 2 and "LIFTED" in logged[1], logged
    finally:
        g.update(held)
        _SOFT_STOP_LAST = None
    print("baxter_usage softstop selftest OK: at the drain floor+ the pump opens no new lane and cuts none "
          "in flight; breach/override/step-breach lift it; _band writes no stop flag at 65%.")


def _contained(path, lane_touch):
    """Does one touch entry sit inside a lane's declared touch-set? Same comparison the
    delegator uses (touch_keys/containment), never a second one: a region of a declared
    file is contained, a sibling file is not, and an `@tag` needs the same tag declared."""
    p = _norm_touch(path)
    if p.startswith("@"):
        return p in lane_touch
    kp = touch_keys(p)
    for lt in lane_touch:
        if str(lt).startswith("@"):
            continue
        kl = touch_keys(lt)
        if kp & kl:
            return True
        if any(a.startswith(b + "/") for a in kp for b in kl):
            return True
    return False


def plan_conflicts(steps, lane_touch=None):
    """Split a plan's steps into clash-free WAVES, and REFUSE the ones that escape the
    lane's declaration. Pure- no meters, no disk, no spawns.

    Returns `(waves, refused)`. Each wave is internally clash-free, so its steps may run
    concurrently; the waves themselves run in order. `refused` is [(step, why)].

    Two guarantees, and they are different:
      - CLASH. `parallel: true` is the planner's opinion, not a fact. Two steps naming
        one file are serialised however loudly the plan disagrees; two REGIONS of one
        file still run together, because that is what makes the tier worth having.
      - CONTAINMENT. Every OTHER lane's safety was computed against THIS lane's declared
        touch-set. A sub-worker editing outside it breaks a promise the lane already
        made, so the step is refused before it runs- never merely reported afterwards.
        An UNDECLARED lane (empty set) declares nothing to escape from and already runs
        solo, so containment is vacuous there and nothing is refused.
    """
    lane = {_norm_touch(x) for x in (lane_touch or []) if str(x).strip()}
    waves, wave_sets, refused = [], [], []
    for s in steps or []:
        touch = [str(x) for x in (s.get("touch") or []) if str(x).strip()]
        outside = [x for x in touch if not _contained(x, lane)] if lane else []
        if outside:
            refused.append((s, f"{outside[0]} is outside the lane's declared touch-set"))
            continue
        ts = {_norm_touch(x) for x in touch}
        for wave, sets in zip(waves, wave_sets):
            if all(clash(ts, other) is None for other in sets):
                wave.append(s)
                sets.append(ts)
                break
        else:
            waves.append([s])
            wave_sets.append([ts])
    return waves, refused


def pick_for_lanes(candidates, live_sets, free):
    """PRE-ASSIGNMENT GATE. Walk the queue in priority order and take the first `free`
    tasks whose touch-sets clash with neither a live lane nor another task picked in
    this same pass. A clash SKIPS that task (logged) and tries the next- priority is
    never silently reordered, the clashing task just waits for its lane to free.
    Returns (picked, skips) where skips is [(task, why)] for the log."""
    picked, picked_sets, skips = [], [], []
    for e in candidates:
        if len(picked) >= max(0, free):
            break
        ts = touch_of(e)
        why = next((w for w in (clash(ts, o) for o in live_sets + picked_sets) if w), None)
        if why:
            skips.append((e, why))
            continue
        picked.append(e)
        picked_sets.append(ts)
    return picked, skips


def _loser(a, b):
    """Of two clashing lanes, the one that stands down: the less important (higher
    priority NUMBER) yields; on a tie the LATER-started lane yields, so the build with
    more work behind it keeps its progress. Returns (loser, winner) as (rf, entry)."""
    (ra, ea), (rb, eb) = a, b
    pa = int(ea.get("priority", PRIO_DEFAULT))
    pb = int(eb.get("priority", PRIO_DEFAULT))
    if pa != pb:
        return (a, b) if pa > pb else (b, a)
    return (a, b) if _lane_started(ra, ea) > _lane_started(rb, eb) else (b, a)


def delegator_recheck():
    """PERIODIC RE-CHECK (triage runs this each proactive pass). Touch-sets GROW
    mid-build, so two lanes cleared at assignment can drift into each other. Compare
    every live pair on their effective touch-set; on a clash drop a `.yield` marker on
    the loser. Its next `--check project --lane <journal>` then returns exit 3, so it
    halts + re-queues through the EXISTING halt machinery- no new stop path, no kill.
    The winner barrels on. Also reaps markers whose journal is gone. Returns the
    reasons fired, for the log."""
    lanes = lane_journals()
    live = {str(rf) for rf, _ in lanes}
    try:
        for m in RESUME_DIR.glob("*.yield"):
            if str(m)[: -len(".yield")] not in live:
                m.unlink()
    except Exception:
        pass
    fired = []
    for i in range(len(lanes)):
        for j in range(i + 1, len(lanes)):
            a, b = lanes[i], lanes[j]
            if a[1] is None or b[1] is None:
                continue   # a journal caught mid-write: never yield a build on a bad read
            if yield_marker(a[0]).exists() or yield_marker(b[0]).exists():
                continue   # one of them is already standing down- don't yield both
            why = clash(touch_of(a[1]), touch_of(b[1]))
            if not why:
                continue
            (lrf, _le), (wrf, we) = _loser(a, b)
            try:
                _write_atomic(yield_marker(lrf), {
                    "reason": why,
                    "against": str(we.get("task", ""))[:80] or Path(wrf).name,
                    "at": datetime.now().isoformat(timespec="seconds"),
                })
            except Exception as e:
                _log(f"delegator: yield marker write failed: {e}")
                continue
            fired.append(f"{Path(lrf).name} yields ({why})")
            _log(f"delegator: {Path(lrf).name} YIELDS to {Path(wrf).name}- {why}")
    return fired


def lane_touch_add(journal, paths):
    """BUILDER SELF-CHECK. A lane calls this before editing a file cluster outside its
    declared touch-set. If the new cluster overlaps a sibling lane, the CALLER loses
    (it's the one straying) and gets exit 3 -> halt yourself rather than collide. If
    it's clear, the paths are appended to the journal's `live_touch` so the periodic
    re-check and any future lane assignment can see them. Returns (ok, message)."""
    rf = Path(journal)
    if not rf.is_absolute():
        rf = RESUME_DIR / rf.name
    entry = _read_json(rf)
    if entry is None:
        return (True, f"no journal at {rf.name}- nothing to guard, proceeding")
    new = {_norm_touch(p) for p in paths if str(p).strip()}
    if not new:
        return (True, "nothing to add")

    # OWNED PATHS ARE A NO-OP (10th July). A lane re-declaring a path its own effective
    # touch-set already covers is asking nothing, and must never be judged against a sibling.
    # Drop them by EXACT ownership- not containment: declaring a whole hub file while owning
    # one region of it is a WIDENING and must still be vetted + clash-checked below. Without
    # this, a live --solo sibling (whose board-wide lock clashes with everything) froze every
    # other lane re-declaring paths it already held- the 10th-July lane-1 bite.
    owned = touch_of(entry)
    new = {p for p in new if p not in owned}
    if not new:
        return (True, "nothing to add- all paths already owned")

    # VET BEFORE CLASH (10th July). touch_problems() guarded only the `--queue` CLI, so a lane
    # could declare mid-flight what the queue would have refused at birth: `utils/baxter_triage.py`
    # whole. The delegator then read that as a whole-file lock, and every sibling region of the
    # file clashed against a declaration nobody was allowed to make. Vetting runs FIRST so the
    # refusal names the real fault- a bare hub path- rather than whichever lane it happened to
    # collide with. `<file>/<function>` still passes; that is the whole point of a region.
    refuse, _warn = touch_problems(sorted(new))
    if refuse:
        return (False, "refused: " + "; ".join(refuse) + "\n" + TOUCH_REFUSAL_HELP)

    for orf, oe in lane_journals():
        if str(orf) == str(rf) or oe is None:
            continue
        # A SIBLING STANDING DOWN LOCKS NOTHING (10th July). A lane the delegator has told to
        # yield edits nothing before it halts + re-queues, so its declaration must not block a
        # live lane- least of all a --solo lane, whose board-wide lock clashes with everything.
        # delegator_recheck skips a yielding lane for exactly this reason.
        if yield_marker(orf).exists():
            continue
        # A CORPSE IS NOT A LANE. lane_alive() short-circuits its pid check for LANE_PID_GRACE
        # seconds after any journal write, so a worker that exited moments ago still reads as
        # alive- correct for the reaper, which must not race a spawn, and wrong here. A process
        # that no longer exists is editing nothing, and refusing a declaration against it halts a
        # live lane for a ghost. A journal with NO pid (held, legacy) still counts: fail safe.
        opid = oe.get("pid")
        if opid and not _pid_alive(opid):
            continue
        why = clash(new, touch_of(oe))
        if why:
            return (False, f"CLASH with {Path(orf).name} ({why})- halt + re-queue, don't collide")
    entry["live_touch"] = sorted(set(entry.get("live_touch") or []) | new)
    try:
        _write_atomic(rf, entry)
    except Exception as e:
        return (True, f"clear, but journal write failed: {e}")
    return (True, f"clear- {len(new)} path(s) registered on {rf.name}")


# ---- THE WRITER-SIDE GUARD (10th July) ----
# lane_touch_add answers "does path X collide with a SIBLING LANE", and it can only be asked
# by something holding a lane journal. Every other writer in the estate- a triage worker, the
# fast lane, a live channel session- meets no check at all. The delegator compares lane against
# lane and nothing else (pick_for_lanes, delegator_recheck), so on 9th July 10:13 a triage
# worker edited utils/baxter_usage.py and utils/baxter_triage.py while lane 1 held BOTH in its
# declared touch_set. Both sets of edits survived because they landed in different regions of
# the files. That was luck, and luck is not a guard.
#
# writer_touch() is the journal-less form: give it the paths you are about to write and it
# answers from the LIVE lanes. Exit 3 / ok=False means a lane owns it- do not edit, queue the
# work. The PreToolUse hook below is what makes the answer binding rather than advisory.
#
# THREE DECISIONS THAT LOOK LIKE DETAIL AND ARE NOT:
#
# 1. SELF-EXCLUSION IS THE WHOLE FLEET'S SAFETY. Lanes load .claude/settings.json too, so a
#    hook that cannot recognise the OWNING lane denies every lane every edit to its own
#    declared touch-set, and all ten lanes stop at once. A lane IS its process: the runner's
#    pid sits in the journal and the claude issuing the Edit is its descendant, so ancestry
#    identifies it. Measured: the journal's pid is TWO links above the claude process.
#
# 2. AN EDIT PAYLOAD CANNOT CARRY A REGION. tool_input.file_path is always a whole file,
#    never `utils/baxter_usage.py/main`, so _contained() asks the wrong question of the
#    owning lane: a lane that declared a REGION of a hub file fails containment against its
#    own edit. _owns_region_of() asks the reverse direction as well. For OTHER lanes the
#    comparison stays symmetric (clash), which is what denies a non-lane writer the whole
#    hub file while a lane holds one region of it- the exact 9th-July collision.
#
# 3. AMBIGUITY ALLOWS, IT DOES NOT DENY. An unreadable journal, an undeclared touch-set, an
#    ancestry walk that fails: each reads as "not owned". The reverse convention would stop
#    the fleet on a transient read, whereas failing open merely restores the status quo of
#    having no guard. `_pid_alive` already fails to True for the same reason. An undeclared
#    lane therefore remains unguarded against a non-lane writer- it runs solo among LANES,
#    which is a different promise. That hole is named, not hidden.
def _proc_parents():
    """pid -> parent pid, for every LIVE process. {} when the snapshot cannot be taken.

    Toolhelp32 rather than psutil: this runs on every Edit in every session, and the two
    interpreters in this estate do not carry the same third-party packages. ctypes is imported
    here rather than at module scope, exactly as _pid_alive does it- the import line of a hub
    file belongs to no lane's declared region."""
    try:
        import ctypes

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                        ("th32ProcessID", ctypes.c_ulong),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                        ("th32ParentProcessID", ctypes.c_ulong),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_char * 260)]

        k = ctypes.windll.kernel32
        snap = k.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
        if snap in (-1, 0xFFFFFFFF, None):
            return {}
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        out = {}
        ok = k.Process32First(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = int(e.th32ParentProcessID)
            ok = k.Process32Next(snap, ctypes.byref(e))
        k.CloseHandle(snap)
        return out
    except Exception:
        return {}


def _ancestor_pids(pid=None, limit=32):
    """This process and every LIVE process above it, nearest first. None when the walk
    cannot be made- which the caller must read as "cannot prove ownership", not as "no lane".

    Only pids present in the snapshot are climbed. Windows keeps reporting a parent pid long
    after that parent exited, so an unlisted pid ends the chain rather than extending it into
    a pid that may since have been reused by something unrelated."""
    parents = _proc_parents()
    if not parents:
        return None
    pid = os.getpid() if pid is None else int(pid)
    chain, seen = [], set()
    while pid in parents and pid not in seen and len(chain) < limit:
        seen.add(pid)
        chain.append(pid)
        pid = parents[pid]
    return chain


def _writer_lane(chain):
    """(journal, entry) of the lane this writer belongs to, or (None, None) for a non-lane
    writer. The lane's runner pid is an ancestor of the claude process issuing the write.

    The NEAREST ancestor wins, never whichever journal happens to sort first. A lane spawns
    sub-workers, and a sub-worker's own lane sits between it and the lane that spawned the
    chain- take the outer one and the writer is judged against a touch-set that is not its
    own, so it is denied a file it genuinely declared. `chain` is ordered nearest-first."""
    order = {int(p): i for i, p in enumerate(chain or ())}
    if not order:
        return (None, None)
    best = None
    for rf, e in lane_journals():
        try:
            if not (isinstance(e, dict) and e.get("pid")):
                continue
            depth = order.get(int(e["pid"]))
        except (TypeError, ValueError):
            continue
        if depth is not None and (best is None or depth < best[0]):
            best = (depth, rf, e)
    return (best[1], best[2]) if best else (None, None)


def _owns_region_of(path, lane_touch):
    """Does `lane_touch` claim `path`, or any REGION sitting inside it?

    _contained() only asks the first question. A lane declaring `utils/baxter_usage.py/main`
    is handed an Edit whose file_path is `utils/baxter_usage.py`- the file, never the region-
    and would fail containment against the very file it was scheduled to edit. Asking the
    reverse direction too is what makes a region declaration usable from the hook."""
    if _contained(path, lane_touch):
        return True
    kp = touch_keys(_norm_touch(path))
    for lt in lane_touch:
        if str(lt).startswith("@"):
            continue
        if any(b.startswith(a + "/") for a in kp for b in touch_keys(lt)):
            return True
    return False


def writer_touch(paths, pid=None):
    """WRITER SELF-CHECK, no journal required. Returns (ok, message, owner_journal).

    ok=False means a LIVE lane owns one of `paths`: do not edit it, tell the owner, queue the work.
    The owning lane- the one whose pid is an ancestor of this process- is excluded, and any
    path IT declared is skipped outright, because the delegator already proved that lane
    clash-free against every other live lane before it was ever started."""
    want = [_norm_touch(p) for p in paths if str(p).strip()]
    if not want:
        return (True, "clear- no path given", None)

    chain = _ancestor_pids(pid)
    if chain is None:
        _log("writer-touch: process ancestry unavailable- failing open")
        return (True, "clear- process ancestry unreadable, guard fails open", None)

    own_rf, own_e = _writer_lane(chain)
    own_touch = touch_of(own_e) if own_e else set()
    want = [p for p in want if not (own_touch and _owns_region_of(p, own_touch))]
    if not want:
        return (True, f"clear- declared on {Path(own_rf).name}", None)

    for rf, e in lane_journals():
        if not isinstance(e, dict):
            continue                                    # caught mid-write- not an owner
        if own_rf is not None and str(rf) == str(own_rf):
            continue                                    # this writer's own lane
        opid = e.get("pid")
        if opid and not _pid_alive(opid):
            continue                                    # a corpse edits nothing
        theirs = touch_of(e)
        if not theirs:
            continue                                    # undeclared: solo among lanes only
        for p in want:
            why = clash({p}, theirs)
            if why:
                return (False,
                        f"refused: a live lane owns {p}- {Path(rf).name} ({why})\n"
                        f"  that lane: {str(e.get('task', '?'))[:70]}\n"
                        f"  do NOT edit it. Tell the owner the lane owns it, and queue the work.",
                        str(rf))
    return (True, f"clear- no live lane owns {', '.join(want)}", None)


# Edit and Write carry `file_path`; NotebookEdit carries `notebook_path`; MultiEdit carries
# `file_path` plus an `edits` list. Registering the hook without covering every key reads as
# protection and is not, so the key list is the whole payload surface, not just the common one.
_WRITER_HOOK_KEYS = ("file_path", "notebook_path")


def _writer_touch_hook(raw=None):
    """The PreToolUse form. JSON payload on stdin, verdict on stdout, exit code always 0.

    SILENCE IS ALLOW. Printing permissionDecision "allow" would auto-approve the tool call and
    skip the owner's own permission prompt for every edit in every session, so the clear path prints
    NOTHING and lets the normal flow proceed. Only a deny is ever written.

    Every exception is swallowed. This fires on each Edit in each session; a crash or a raised
    timeout here stalls the whole fleet, and what it would be protecting is a guard that did
    not exist a day ago."""
    try:
        payload = json.loads((sys.stdin.read() if raw is None else raw) or "{}")
        ti = payload.get("tool_input") or {}
        paths = [ti[k] for k in _WRITER_HOOK_KEYS if ti.get(k)]
        if not paths:
            return 0
        # SCOPE-ONLY IS READ-ONLY, MECHANICALLY. A lane running an unscoped entry is spawned with
        # BAXTER_SCOPE_ONLY set (baxter_triage._resume_worker). Its whole job is to work out what
        # it will touch- so it may not touch anything yet. Every edit is denied here, which is
        # what lets clash() wave the pass through in parallel with real builds: the read-only
        # claim cannot be violated. The pass declares its touch-set and re-queues; the build runs
        # later, scoped, without this flag.
        if os.environ.get("BAXTER_SCOPE_ONLY"):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "This is a SCOPE-ONLY pass: it edits nothing. Read the code, work out what a "
                    "build of this task WOULD touch, then re-queue it as a real build and stop- do "
                    "NOT edit source now. Run: python baxter_usage.py --halt \"<task>\" \"<next "
                    "step>\" --touch \"file_a,file_b/region\" (or --solo if it rewrites a hub). If "
                    "the task needs a design rather than a file-list, say so to the owner instead."),
            }}))
            return 0
        ok, msg, _owner = writer_touch(paths)
        if ok:
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": msg,
        }}))
        return 0
    except Exception as e:
        try:
            _log(f"writer-touch hook failed open: {type(e).__name__}: {e}")
        except Exception:
            pass
        return 0


def lane_touch_release(journal, paths):
    """The other half of lane_touch_add: hand a region BACK once its edit has landed.

    `live_touch` was append-only, so a lane held every region it ever grew into for the whole
    build- long after the edit was finished- and a sibling that needed one was told to halt
    against work that had already stopped. Worse, a lane's acceptance gate runs in the RUNNER
    process AFTER the worker exits, so a build could clash with its own corpse: on 10th July the
    hub-fence build's sealed exam could not claim `utils/baxter_triage.py/build_worker_prompt`,
    because the build that had just finished editing it still nominally held the region.

    Only `live_touch` is given back. `touch_set` is the queue-time declaration- provenance, not
    a claim- and stays put. Released paths are appended to `released_touch`, so the audit still
    shows which regions the lane really edited. Releasing a path it never held is a no-op, not
    an error. Returns (ok, message)."""
    rf = Path(journal)
    if not rf.is_absolute():
        rf = RESUME_DIR / rf.name
    entry = _read_json(rf)
    if entry is None:
        return (True, f"no journal at {rf.name}- nothing to release")
    drop = {_norm_touch(p) for p in paths if str(p).strip()}
    if not drop:
        return (True, "nothing to release")
    held = list(entry.get("live_touch") or [])
    freed = [p for p in held if _norm_touch(p) in drop]
    if not freed:
        return (True, f"{rf.name} held none of those- unchanged")
    entry["live_touch"] = [p for p in held if _norm_touch(p) not in drop]
    entry["released_touch"] = sorted(set(entry.get("released_touch") or []) | set(freed))
    try:
        _write_atomic(rf, entry)
    except Exception as e:
        return (False, f"release failed- journal write: {e}")
    return (True, f"released {len(freed)} region(s) from {rf.name}: " + ", ".join(sorted(freed)))


def _band_face(pct, wkgate, ceil):
    """The tier emoji + short phrase for the current band (the owner's 6th-July v2 /usage
    spec- the command auto-attaches the phrase + emoji baked for that usage band).
    📊 clear · ⚠️ nearing the big-stop · 🛑 at the wall."""
    if pct >= ROUTINE_STOP_SESSION or wkgate >= ROUTINE_STOP_WEEKLY:
        return "\U0001F6D1", " Bare minimum- vitals only till reset."    # 🛑
    if pct >= ceil - 5 or wkgate >= WEEKLY_PROJECT_GATE - 5:
        return "⚠️", f" Builds pause at {ceil:.0f}."           # ⚠️
    return "\U0001F4CA", ""                                              # 📊


def report_line(snap):
    """The /usage on-demand line (the owner's COMMAND, 5th July)- the basic info in the
    LOCKED one-line alert format. Pure formatting; the caller refreshes the meter.
    Session % + reset clock + weekly %, with the band's emoji + action phrase."""
    pct = float(snap.get("session_pct") or 0)
    wk = float(snap.get("weekly_pct") or 0)
    wkgate = float(snap.get("weekly_gate_pct") or wk)
    hl = _hours_left(snap.get("session_resets_at", ""))
    ceil = ceiling(hl)
    tstr = _fmt_clock(snap.get("session_resets_at", ""))
    emoji, tail = _band_face(pct, wkgate, ceil)
    line = f"{emoji} Usage {pct:.0f}%"
    if tstr != "?":
        line += f"- resets {tstr}"
    line += f". Weekly {wk:.0f}%."
    return line + tail


def write_live(snap):
    """Bake the /usage reply line into .baxter_usage_live.json (the owner's 6th-July v2
    spec)- so the orthogonal /usage command is a pure file-read + Discord POST with
    ZERO meter compute on the hot path. Written on every successful probe; a failed
    probe leaves the last-known baked line untouched (never a blank/errored reply)."""
    try:
        pct = float(snap.get("session_pct") or 0)
        wk = float(snap.get("weekly_pct") or 0)
        hl = _hours_left(snap.get("session_resets_at", ""))
        ceil = ceiling(hl)
        emoji, _ = _band_face(pct, float(snap.get("weekly_gate_pct") or wk), ceil)
        _write_atomic(LIVE, {
            "line": report_line(snap),
            "emoji": emoji,
            "session_pct": pct,
            "weekly_pct": wk,
            "session_resets_at": snap.get("session_resets_at", ""),
            "updated": snap.get("updated") or datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as e:
        _log(f"write_live failed: {e}")


def _status_line(snap):
    hl = _hours_left(snap.get("session_resets_at", ""))
    return (f"session {snap.get('session_pct', '?')}% ({_fmt_left(hl) if hl is not None else '?'} left, "
            f"ceiling {ceiling(hl):.0f}%) | weekly {snap.get('weekly_pct', '?')}% "
            f"(gate meter {snap.get('weekly_gate_pct', '?')}%) | updated {snap.get('updated', 'never')}"
            + (f" | ERROR: {snap['error']}" if snap.get("error") else ""))


# The literal placeholder from the example line, copied verbatim into real declarations by
# whatever built the queue entry. A pseudo-declaration is WORSE than none: it reads as
# declared, so the delegator co-schedules the task onto a lane, and the build then collides
# on files it never named. Undeclared at least fails safe (solo). Reject the placeholder.
TOUCH_PLACEHOLDERS = {"@cluster", "utils/x.py", "utils/some_dir"}


# ---- HUB FILES MUST BE DECLARED BY REGION (9th July) ----
# Nearly every Baxter build edits one of these five, in a different function. A whole-file
# declaration is therefore a lock on the entire build queue: counted off the live queue on
# 9th July, 7 of 23 runnable tasks declared the whole of baxter_triage.py, 4 the whole of
# baxter_watch.ps1, and NOT ONE declared a region. Whichever starts first locks out the
# rest, so four lanes contending over one hub file is still one effective lane- raising
# LANE_COUNT alone would have shipped as a no-op.
#
# clash() already parallelises two regions of one file (see the containment rule above), so
# the fix is to stop the coarse declaration being typeable: `--queue --touch` REFUSES a bare
# hub path and asks for `<file>/<function>`. `--solo` remains the deliberate escape hatch for
# a task that genuinely rewrites a hub file end to end- it says so out loud and serialises.
# `--halt` is NOT validated: a build re-queueing its own mid-flight touch-set must never be
# refused, and a resume of already-started work fails safe by serialising anyway.
HUB_FILES = {
    "utils/baxter_triage.py",
    "utils/baxter_usage.py",
    "utils/baxter_watch.ps1",
    "utils/baxter_fast.py",
    "utils/baxter_slash.py",
}


import importlib
import importlib.util


# ---- ONE RESOLVER FOR THE SIBLINGS enqueue() NEEDS (11th July) ----
# Every sibling import on the enqueue path used a two-step chain- `from utils import X`, then a
# bare `import X`. Both resolve through sys.path, and neither consults the directory this module
# was actually loaded FROM. A caller that path-loads baxter_usage (spec_from_file_location- how
# every sealed exam loads it) therefore has no utils entry on sys.path, both steps raise, and
# enqueue() died at the baxter_clusters site before writing its entry; hub_closure() degraded
# silently to its static seed, so a path-loaded vet checked its touch-set against three names
# instead of the real hub closure.
def _sibling(name):
    """Load a utils/ sibling however THIS module was itself loaded. Never touches sys.path.

    The two-step chain first, exactly as the call sites still spell it; then the file load out of
    Path(__file__).parent- the directory we came from. A sys.path.insert would have been shorter
    and is deliberately rejected: a library that inserts into the caller's import path shadows
    same-named modules for the rest of that process, and an exam asserting on sys.path would see
    it move underneath. A file load resolves one module and changes nothing global.

    The literal `import` statements STAY at every call site: baxter_imports.closure() is a static
    ast walk, and those statements are the only reason baxter_name.py sits in baxter_usage.py's
    closure at all. Replace them with this call and the watcher stops reloading the siblings.
    """
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    for dotted in (f"utils.{name}", name):
        try:
            return importlib.import_module(dotted)
        except ImportError:
            pass
    src = _sibling_dir() / f"{name}.py"
    if not src.is_file():
        raise ModuleNotFoundError(
            f"No module named {name!r}- not on sys.path, and no {src}", name=name)
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # so a second enqueue() re-reads nothing from disk
    finder = _SiblingFinder()
    sys.meta_path.append(finder)     # a sibling's OWN bare imports must resolve too- see below
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)  # a half-executed module must never be served to anyone
        raise
    finally:
        with contextlib.suppress(ValueError):
            sys.meta_path.remove(finder)
    return mod


def _sibling_dir():
    """The directory this module was loaded from- utils/, however we got here."""
    return Path(__file__).resolve().parent


class _SiblingFinder:
    """Resolves a sibling's OWN bare sibling imports, for the window in which we execute it.

    Loading the file is not enough on its own: baxter_imports.py imports baxter_hub_edit at
    module scope, and under a path-load that bare import has no utils entry on sys.path to find
    it either, so the sibling dies in its own body. This finder is APPENDED to sys.meta_path-
    last, so it never shadows a module the normal machinery can already resolve- and it is
    removed again the moment the exec returns. It answers only for a top-level name that exists
    as a .py file beside us. sys.path is never touched, by design: an insert there would outlive
    the call and shadow same-named modules for the rest of the caller's process."""

    def find_spec(self, name, path=None, target=None):
        if path is not None or "." in name:
            return None              # a package submodule is its parent's business, not ours
        src = _sibling_dir() / f"{name}.py"
        if not src.is_file():
            return None
        return importlib.util.spec_from_file_location(name, src)


# ---- AND SO MUST THEIR IMPORT CLOSURE (10th July) ----
# The five hub files are declared by REGION because nearly every lane edits them. But a module
# a hub file IMPORTS is edited almost as widely and shares its mutable state fleet-wide:
# baxter_send_dedup.py is the send ledger every outward path writes, imported at module scope
# by baxter_fast.py. A bare whole-file lock on it is the same queue-wide serialiser a bare hub
# declaration was- whichever lane starts first locks out the rest. So the fence extends to the
# transitive import closure of the .py hub files. A REGION of a closure module
# (utils/baxter_send_dedup.py/_claim) still queues; only the bare whole-file form is refused,
# and `--solo` stays the deliberate escape for a task that rewrites one end to end.
#
# FAIL-SAFE, because touch_problems() runs on EVERY --queue/enqueue. baxter_imports.closure()
# RAISES WalkError the instant any closure module is broken on disk- which is exactly a sibling
# lane's DECLARED red-proof window. If that propagated, every --queue during that window would
# crash. So the walk is wrapped: any failure degrades to a static seed that always names
# baxter_send_dedup.py and the hub files, and NEVER raises. A good walk is cached; a fallback is
# NOT cached, so the next call retries once the disk is whole again.
_HUB_CLOSURE_SEED = frozenset({"utils/baxter_send_dedup.py"} | HUB_FILES)
_HUB_CLOSURE_CACHE = None


def hub_closure():
    """Normalised 'utils/<name>' touch keys for every utils/ module in the transitive import
    closure of the .py hub files. Cached after one clean walk; fail-safe to a static seed that
    always contains baxter_send_dedup.py and the hub files- it never raises, since it is on the
    hot path of every enqueue."""
    global _HUB_CLOSURE_CACHE
    if _HUB_CLOSURE_CACHE is not None:
        return _HUB_CLOSURE_CACHE
    try:
        try:
            from utils import baxter_imports as _bi
        except ImportError:            # run as a bare script: utils/ is already sys.path[0]
            try:
                import baxter_imports as _bi
            except ImportError:        # path-loaded: no utils anywhere on sys.path
                _bi = _sibling("baxter_imports")
        scripts = Path(os.path.dirname(os.path.abspath(__file__))).parent
        acc = set(_HUB_CLOSURE_SEED)
        for hub in HUB_FILES:
            if hub.endswith(".ps1"):
                continue               # baxter_imports walks Python; a .ps1 has no import graph
            for pth in _bi.closure(scripts / hub):
                acc.add(_norm_touch(os.path.relpath(pth, scripts)))
        _HUB_CLOSURE_CACHE = frozenset(acc)
        return _HUB_CLOSURE_CACHE
    except Exception:
        # NEVER raise on the enqueue hot path. Do not cache- retry once the disk is whole again.
        return frozenset(_HUB_CLOSURE_SEED)


def _resolve_candidates(p):
    """Every absolute path a touch entry could mean (see touch_keys for the two dialects)."""
    if p.startswith("~"):
        p = _norm_touch(os.path.expanduser(p))
    if _is_abs(p):
        return [Path(p)]
    return [Path(r) / p for r in _known_roots()]


def _path_state(p):
    """What a declared path IS on disk, under either root:
      exists    - a real file or directory
      region    - `<file>/<function>`: an ancestor is a real file, so it names part of one
      creatable - absent, but its parent directory is there (the task will create it)
      orphan    - absent AND its parent directory is absent- almost always a typo
    A hard exists-on-disk check would wedge the queue: live entries legitimately name files
    they will CREATE (baxter_rejig.py, baxter_doctor_ai.py), so only `orphan` is refused."""
    cands = _resolve_candidates(p)
    for c in cands:
        try:
            if c.exists():
                return "exists"
        except OSError:
            pass
    for c in cands:
        for anc in c.parents:
            try:
                if anc.is_file():
                    return "region"
            except OSError:
                pass
    for c in cands:
        try:
            if c.parent.is_dir():
                return "creatable"
        except OSError:
            pass
    return "orphan"


def touch_problems(touch):
    """Validate a declared touch-set at queue time. Returns (refusals, warnings).

    An unvalidated touch-set is worse than none, so four things are REFUSED outright: the
    example-line placeholder, a bare hub file, a path under a directory that does not exist,
    and an @tag no cluster registry knows (utils/baxter_clusters.py- an unknown tag is a lock
    on nothing). A path that simply doesn't exist yet only WARNS- the task will create it."""
    refuse, warn = [], []
    for p in touch or []:
        if p in TOUCH_PLACEHOLDERS:
            refuse.append(f"{p} is the placeholder from the example line, not a declaration")
            continue
        if p.startswith("@"):
            # An @tag is a LOCK, not a label: clash() treats a shared tag exactly as it
            # treats a shared file. An UNREGISTERED tag therefore locks against nothing-
            # @lanez never meets @lane-pump- so the delegator co-schedules work that should
            # have serialised, and the writer never learns. The registry is the only list.
            try:
                from utils import baxter_clusters as _bc
            except ImportError:            # run as a bare script: utils/ is already sys.path[0]
                try:
                    import baxter_clusters as _bc
                except ImportError:        # path-loaded: no utils anywhere on sys.path
                    _bc = _sibling("baxter_clusters")
            bad = _bc.tag_problem(p)
            if bad:
                refuse.append(bad)
            continue
        if touch_keys(p) & HUB_FILES:
            refuse.append(f"{p} is a HUB file- name the region you will edit "
                          f"({p}/<function>), or say --solo if you will rewrite the lot")
            continue
        if touch_keys(p) & hub_closure():
            refuse.append(f"{p} is imported by a hub file- name the region you will edit "
                          f"({p}/<function>), or say --solo if you will rewrite the lot")
            continue
        state = _path_state(p)
        if state == "orphan":
            refuse.append(f"{p} sits under a directory that does not exist- check the spelling")
        elif state == "creatable":
            # A missing .py is the stale-ticket tell (9th July). The duplicate repair ticket
            # declared utils/baxter_repair.py- a module that has never existed- so a builder
            # trusting the declaration would have CREATED a second repair worker nothing calls.
            # It stays a WARN, never a refusal: live entries legitimately name a module they
            # are about to write, and selftest_declare queues utils/a.py on purpose.
            if p.lower().endswith(".py"):
                warn.append(f"{p} does not exist- if you did not mean to CREATE a new module, "
                            f"the ticket is STALE and names code that was never written; "
                            f"check whether the work has already shipped elsewhere")
            else:
                warn.append(f"{p} does not exist yet- taking it as a file this task will create")
    return refuse, warn


TOUCH_REFUSAL_HELP = (
    "Name the files/dirs this task will actually edit- a hub file by the REGION\n"
    '  --touch "utils/baxter_triage.py/_claude,utils/coc_bot/,@lane-pump"\n'
    "and a REGISTERED @tag for adjacency- the list is utils/baxter_clusters.py, and\n"
    "an unknown tag is refused there, since it locks against nothing. A fake or coarse\n"
    "touch-set is worse than none: the delegator co-schedules it, then the build\n"
    "collides on files it never named- or locks a hub file and starves the queue.")


class BadTouchSet(ValueError):
    """A declared touch-set the delegator cannot be trusted with. Raised by the WRITERS.

    Until 10th July `touch_problems()` was only ever consulted by `main()`, so the refusal
    was a property of the COMMAND LINE rather than of the queue. Anything importing the
    module wrote straight past it, and two entries proved it on the live queue on 9th July:
    3f89d826 carried a bare `utils/baxter_watch.ps1` (a whole-file lock, off every lane),
    and 13c2035f carried ten paths spelt `Python Scripts/utils/...`, which resolve to an
    orphan- so `clash()` compared them against the real `utils/...` spelling, found nothing
    in common, and cleared two lanes to edit `baxter_rules.py/check` at the same time.

    The guard therefore lives on `enqueue()` and `queue_edit()`, the only two functions that
    ever write a touch-set. It raises rather than returns: a caller that ignores a return
    value would re-open the exact hole, and there is no safe way to half-admit a declaration
    the delegator will go on to schedule against."""

    def __init__(self, refusals, touch_set=None):
        self.refusals = list(refusals or [])
        self.touch_set = list(touch_set or [])
        super().__init__("; ".join(self.refusals) or "invalid touch-set")

    def report(self):
        """The refusal as the CLI has always printed it."""
        return "refused: " + "\n         ".join(self.refusals) + "\n" + TOUCH_REFUSAL_HELP


def vet_touch(touch):
    """Refuse a bad touch-set outright; return its warnings for the caller to render.

    An EMPTY touch-set is not a refusal here and must never become one: undeclared means
    solo, which is safe, and `halt()`, `_park()` and the fast lane's placeholder all queue
    with nothing declared by design. Only a declaration that LIES- a placeholder, a bare hub
    file, a path under a directory that does not exist- is refused."""
    refuse, warn = touch_problems(touch)
    if refuse:
        raise BadTouchSet(refuse, touch)
    return warn


# ---- THE GUARD'S OWN LOG (the owner, 9th July 08:46) ----
# "it is very important that security guard periodically rejects things. thats how we know
# it is functional." He asked for the rejection history and was told there was none. There
# were 48, all that day- they were simply buried among everything else in .baxter.log, with
# no way to answer the question in one command. Every clash skip and every human-gate hold
# now appends one json line here as well; `--rejects` reads it back. Additive: the .baxter.log
# line still goes out, because that is where the pump's story is read in sequence.
REJECT_LOG = VAULT / ".baxter_rejects.jsonl"
REJECT_LOG_MAX = 2 * 1024 * 1024


def record_reject(task, reason, lane=None, kind="clash", priority=None):
    """Append one rejection. `task` may be the queue entry itself. Never raises: a guard
    that can crash the pump by failing to log is worse than a guard with no log."""
    if isinstance(task, dict):
        priority = task.get("priority", priority)
        task = task.get("task", "?")
    try:
        prio = int(priority) if priority is not None else None
    except (TypeError, ValueError):
        prio = None
    row = {"at": datetime.now().isoformat(timespec="seconds"),
           "task": str(task)[:120], "reason": str(reason), "kind": str(kind),
           "lane": lane_label(lane) if lane is not None else None, "priority": prio}
    try:
        if REJECT_LOG.exists() and REJECT_LOG.stat().st_size > REJECT_LOG_MAX:
            os.replace(str(REJECT_LOG), str(REJECT_LOG) + ".1")
        with open(REJECT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        _log(f"reject log write failed: {e}")
    return row


def read_rejects(limit=20, since=""):
    """The rejection history, newest first. `since` is an ISO date/prefix, e.g. 2026-07-09."""
    rows = []
    try:
        for line in REJECT_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        return []
    if since:
        rows = [r for r in rows if str(r.get("at", "")) >= since]
    return rows[-max(1, int(limit)):][::-1]


def _touch_arg(argv):
    """`--touch "utils/a.py,utils/coc_bot/,@probe"` -> a normalised list, or None."""
    if "--touch" not in argv:
        return None
    i = argv.index("--touch")
    if len(argv) <= i + 1:
        return None
    parts = [p.strip() for p in argv[i + 1].replace(";", ",").split(",")]
    return [_norm_touch(p) for p in parts if p] or None


def _solo_arg(argv):
    """`--solo` -> the caller has looked at the task and cannot scope it, so it may queue
    with no touch-set and run alone. The ONLY way to create an undeclared entry.

    Why it exists (the owner, 9th July 10:06- "this is a problem with the builds not declaring
    touch set when they should have. I need YOU to fix this"): every queue entry used to
    default to undeclared, and `--queue` merely PRINTED "no --touch declared, so it runs
    SOLO" after the fact. Nothing read that line, so 26 of 27 queued builds carried no
    touch-set, each one clashing with everything by design- lane 2 sat empty with a full
    queue behind it. A default nobody has to choose is a default nobody notices. Undeclared
    is now a decision you have to type, and it lands in the log as one."""
    return "--solo" in argv


def _gate_arg(argv):
    """`--gate owner` -> "owner"; `--gate none` -> "" (lifts it); absent -> None (leave as-is).

    A gate is ONE token- the party a task waits on ('owner', 'pm', 'prd') or nothing. On 10th
    July baxter_pm_delegate handed --gate a whole justifying SENTENCE ('none - the build writes
    only code...'); the old code lower-cased it, found 'none - the build...' was not literally
    'none', and stored the paragraph as the gate. is_human_gated then read it as a real gate,
    and three greenlit builds- the doctor pfp among them- were stranded, waiting on a party
    that does not exist. So take only the FIRST word: 'none anything' lifts the gate, and a
    prose gate can never wedge a build again."""
    if "--gate" not in argv:
        return None
    i = argv.index("--gate")
    if len(argv) <= i + 1:
        return None
    raw = argv[i + 1].strip().lower()
    # first token only: split on whitespace, then trim trailing punctuation ('none,' -> 'none')
    first = (raw.split() or [""])[0].strip(",;:.-")
    return "" if (first in GATE_NONE or raw in GATE_NONE) else first


def main(argv):
    if "--enforce" in argv:
        lvl = enforce()
        print(f"band: {lvl or 'clear'}")
        return 0
    if "--reprioritize" in argv:
        # Manual trigger for the pass enforce() already runs every beat. --force ignores the
        # 900s stamp. main() ignores unknown flags and returns 0, so this branch's EXIT CODE
        # proves nothing about whether it exists- only the queue file it rewrites does.
        changed, total = queue_reprioritize(force="--force" in argv)
        print(f"reprioritised {changed} of {total}")
        return 0
    if "--blocked" in argv:
        kind = argv[argv.index("--blocked") + 1] if len(argv) > argv.index("--blocked") + 1 else "big"
        b, why = blocked(kind)
        print(f"{'BLOCKED' if b else 'clear'}: {why}" if b else "clear")
        return 3 if b else 0
    if "--override" in argv:
        i = argv.index("--override")
        try:
            mins = int(argv[i + 1]) if len(argv) > i + 1 and not argv[i + 1].startswith("--") else 120
        except Exception:
            mins = 120
        from datetime import timedelta
        until = datetime.now() + timedelta(minutes=mins)
        _write_atomic(VAULT / ".baxter_override", {"until": until.isoformat(timespec="seconds"),
                                                   "set_at": datetime.now().isoformat(timespec="seconds")})
        print(f"override ON for {mins} min (until {until:%H:%M})- BIG stop lifted to the 90% wall; 90% vital-only wall still holds")
        return 0
    if "--override-clear" in argv:
        try: (VAULT / ".baxter_override").unlink()
        except Exception: pass
        print("override cleared- 80% big stop back in force")
        return 0
    if "--breach-step" in argv:
        # The STEPPED breach (the owner's 7th-July /breach command). Lift the CURRENT active limiter
        # and run until the NEXT tier rung above- across BOTH session and weekly, whichever
        # first- then self-clear so he must /breach again to step further. DISTINCT from
        # --override (80->90 big band only) and --breach (lifts ALL bands for a fixed
        # window). Ceiling is computed off the LIVE meter (force a fresh read if stale).
        snap = read_meters()
        try:
            age = (datetime.now() - datetime.fromisoformat(snap.get("updated", ""))).total_seconds()
        except Exception:
            age = 1e9
        if age > 60:
            snap = probe(force=True)
        pct = float(snap.get("session_pct") or 0)
        wk = float(snap.get("weekly_gate_pct") or snap.get("weekly_pct") or 0)
        sc = _next_tier(pct, SESSION_TIERS)
        wc = _next_tier(wk, WEEKLY_TIERS)
        _write_atomic(BREACH_STEP, {
            "session_ceiling": sc, "weekly_ceiling": wc,
            "from_session": pct, "from_weekly": wk,
            "set_at": datetime.now().isoformat(timespec="seconds"),
        })
        print(f"step-breach ON: run until weekly {wc:.0f}% or session {sc:.0f}%, whichever first "
              f"(from session {pct:.0f}% / weekly {wk:.0f}%)- re-blocks + auto-clears at the ceiling; /breach again to step further")
        return 0
    if "--breach-step-clear" in argv:
        try: BREACH_STEP.unlink()
        except Exception: pass
        print("step-breach cleared- normal bands back in force")
        return 0
    if "--breach" in argv:
        # The full breach (/breach)- lifts EVERY hard band (80 big-stop AND the 90%
        # vital-only wall) for the window, so authorised big/routine work can run at
        # any %. Default 60 min- shorter than --override's 120.
        i = argv.index("--breach")
        try:
            mins = int(argv[i + 1]) if len(argv) > i + 1 and not argv[i + 1].startswith("--") else 60
        except Exception:
            mins = 60
        from datetime import timedelta
        until = datetime.now() + timedelta(minutes=mins)
        _write_atomic(VAULT / ".baxter_breach", {"until": until.isoformat(timespec="seconds"),
                                                 "set_at": datetime.now().isoformat(timespec="seconds")})
        print(f"BREACH ON for {mins} min (until {until:%H:%M})- ALL bands lifted incl the 90% vital-only wall")
        return 0
    if "--breach-clear" in argv:
        try: (VAULT / ".baxter_breach").unlink()
        except Exception: pass
        print("breach cleared- normal bands back in force (90% vital-only wall)")
        return 0
    if "--quiet95" in argv:
        # The 95-ONLY window (the owner, 8th July MAX period). Mutes the 80/90 SESSION band
        # pings and swaps them for one 95% heads-up. Default 8h- covers a MAX session;
        # auto-expires. Governor ENFORCEMENT is untouched (use --breach for that).
        i = argv.index("--quiet95")
        try:
            mins = int(argv[i + 1]) if len(argv) > i + 1 and not argv[i + 1].startswith("--") else 480
        except Exception:
            mins = 480
        from datetime import timedelta
        until = datetime.now() + timedelta(minutes=mins)
        _write_atomic(QUIET95, {"until": until.isoformat(timespec="seconds"),
                                "set_at": datetime.now().isoformat(timespec="seconds")})
        print(f"95-only window ON for {mins} min (until {until:%H:%M})- 80/90 pings muted, sole session ping is a 95% heads-up")
        return 0
    if "--quiet95-clear" in argv:
        try: QUIET95.unlink()
        except Exception: pass
        print("95-only window cleared- normal 80/90 band pings back in force")
        return 0
    # THE SELFTEST TABLE. A dict rather than a chain of ifs, because the guard below has to
    # know exactly which flags exist- and a guard that guesses is the bug it was written to fix.
    selftests = {
        "--selftest-pings": selftest_pings,
        "--selftest-statusline": selftest_statusline,
        "--selftest-clash": selftest_clash,
        "--selftest-declare": selftest_declare,
        "--selftest-lanes": selftest_lanes,
        "--selftest-softstop": selftest_softstop,
        "--selftest-edit": selftest_edit,
        "--selftest-move": selftest_move,
        "--selftest-dup": selftest_dup,
        "--selftest-drop": selftest_drop,
    }
    for a in argv:
        if a in selftests:
            selftests[a]()
            return 0
    # THE GUARD. Any --selftest token still here matched nothing above. Until 10th July it fell
    # through the whole branch chain into the status line and exited 0, so `--selftest-dup` (once)
    # and any sealed exam naming a selftest nobody ever wrote graded GREEN against a flag that did
    # nothing. It sits BELOW the table on purpose: above it, it would eat every real selftest flag.
    unknown = [a for a in argv if a.startswith("--selftest")]
    if unknown:
        print(f"unrecognised selftest flag: {unknown[0]}\n"
              f"       known: {', '.join(sorted(selftests))}")
        return 2
    if "--writer-touch" in argv:
        # THE WRITER-SIDE GUARD: `--writer-touch <path>...` -> exit 3 when a LIVE lane owns one
        # of them, exit 0 when none does. `--writer-touch --hook` is the PreToolUse form: a JSON
        # payload on stdin, a deny verdict on stdout. It sits ABOVE the status-line fallthrough
        # at the end of this chain deliberately- an unrecognised flag drops all the way through
        # and prints the status line at exit 0, which is precisely how an ABSENT guard grades
        # green in a sealed exam that only reads the exit code.
        if "--hook" in argv:
            return _writer_touch_hook()
        i = argv.index("--writer-touch")
        pos = [a for a in argv[i + 1:] if not a.startswith("--")]
        if not pos:
            print("usage: --writer-touch <path> [<path>...]   |   --writer-touch --hook")
            return 1
        ok, msg, _owner = writer_touch(pos)
        print(msg)
        return 0 if ok else 3
    if "--lane-touch" in argv:
        # BUILDER SELF-CHECK: `--lane-touch <journal> <path> [<path>...]`. Exit 3 = the new
        # cluster overlaps a sibling lane, halt yourself. Exit 0 = clear, paths registered.
        i = argv.index("--lane-touch")
        pos = [a for a in argv[i + 1:] if not a.startswith("--")]
        if len(pos) < 2:
            print("usage: --lane-touch <journal> <path> [<path>...]")
            return 1
        ok, msg = lane_touch_add(pos[0], pos[1:])
        print(msg)
        return 0 if ok else 3
    if "--delegator" in argv:
        fired = delegator_recheck()
        print("; ".join(fired) if fired else "lanes clear- no clash")
        return 0
    if "--lanes" in argv:
        fleet, budget, lanes = lanes_report()
        cap = lane_capacity()
        print(f"lanes {len(lanes)}/{cap} live (LANE_COUNT={LANE_COUNT})"
              f"\nfleet {fleet}/{budget} workers (leads + honoured sub-workers)")
        for rf, e, subs in lanes:
            if e is None:
                print(f"  [lane {lane_label(_lane_id(rf))}] (journal unreadable- counted as a busy lane)"
                      f"\n      width: {lane_width_line(subs)}")
                continue
            y = _read_json(yield_marker(rf))
            v = e.get("verify") or e.get("verify_assert")
            print(f"  [lane {lane_label(_lane_id(rf))}] p{e.get('priority', PRIO_DEFAULT)} {str(e.get('task', '?'))[:60]}"
                  f"\n      width: {lane_width_line(subs)}"
                  f"\n      touch: {sorted(touch_of(e)) or '(undeclared- solo)'}"
                  f"\n      verify: {str(v)[:70] if v else '(none declared- it will land UNVERIFIED)'}"
                  + (f"\n      YIELDING: {y.get('reason')}" if y else ""))
        # A corpse is never counted as a lane again (9th July). Show it anyway- the
        # reaper is about to route it into the classifier, and silence about a dead
        # build is what made `--lanes` lie all night.
        for rf, e in dead_lanes():
            task = str((e or {}).get("task", "?"))[:60]
            print(f"  [dead] {task}\n      no heartbeat for >{LANE_DEAD_AFTER}s- slot handed back, "
                  f"the reaper will classify it")
        return 0
    if "--check" in argv:
        i = argv.index("--check")
        cls = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("--") else "project"
        lane = argv[argv.index("--lane") + 1] if "--lane" in argv else None
        res = check(cls, lane=lane)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("allowed") else 3
    if "--halt" in argv:
        i = argv.index("--halt")
        pos = [a for a in argv[i + 1:] if not a.startswith("--")][:2]
        if len(pos) < 2:
            print('usage: --halt "<task>" "<next step>" [--note <path>] [--state "<summary>"] [--touch "a,b,@c"]')
            return 1
        note = argv[argv.index("--note") + 1] if "--note" in argv else ""
        stt = argv[argv.index("--state") + 1] if "--state" in argv else ""
        # DELIBERATELY NO --verify / --verify-by FLAGS. The halting build is the one being
        # graded; a flag here would let it name its own exam and its own provenance on the way
        # out, re-opening the exact hole the planner tier closed. The exam is LIFTED off the
        # lane journal instead- written there by the planner, above the builder's reach.
        e = halt(pos[0], pos[1], note, stt, touch_set=_touch_arg(argv), gate=_gate_arg(argv)) or {}
        exam = e.get("verify") or e.get("verify_assert")
        carried = (f"sealed exam carried ({_prov(e.get('verify_by')) or 'unattributed'})" if exam
                   else "no sealed exam found on the lane journal- the planner re-seals on resume")
        print(f"halt recorded- re-queued at resume priority, {carried}; "
              f"triage restarts it when a lane + the curve allow")
        return 0
    if "--queue" in argv:
        i = argv.index("--queue")
        pos = [a for a in argv[i + 1:] if not a.startswith("--")][:2]
        if len(pos) < 2:
            print('usage: --queue "<task>" "<first step>" --note <path to its PRD> '
                  '[--state "<summary>"] '
                  '[--priority N] [--touch "a,b,@c"] [--gate owner] [--verify "<shell cmd>"] '
                  '[--verify-assert "<claim a checker must prove>"] [--source-mid <discord id>] '
                  '[--dup-ok] [--prd-exempt]\n'
                  '       --note must point at a document inside 60-PRDs/. Route a fresh ask '
                  'through\n       baxter_pm_delegate.py --ask "<the ask>" --queue-it, which '
                  'writes one.\n'
                  '       --prd-exempt is the VITALS bypass, and every use of it is logged.\n'
                  '       --dup-ok forces a near-duplicate through. It is the escape hatch for a '
                  'genuine\n       second pass over one subsystem- not a way past a refusal you '
                  'have not read.')
            return 1
        # AN EMPTY ASK IS NOT AN ASK. It used to fall through to the PRD guard and be refused
        # there by accident; since 11th July that guard reports a park as an ACCEPTANCE, so a
        # stray `--queue "   " "step"` would be answered "accepted" and a blank entry would sit
        # on the PM gate for a document no PM can write. Refuse it before anything is recorded.
        if not pos[0].strip():
            print("refused: an empty ask. --queue needs a task string with something in it.")
            return 2
        note = argv[argv.index("--note") + 1] if "--note" in argv else ""
        stt = argv[argv.index("--state") + 1] if "--state" in argv else ""
        try:
            prio = int(argv[argv.index("--priority") + 1]) if "--priority" in argv else PRIO_DEFAULT
        except Exception:
            prio = PRIO_DEFAULT
        touch = _touch_arg(argv)
        gate = _gate_arg(argv)
        solo = _solo_arg(argv)
        # The Discord message this ask came from. enqueue() matches on it BEFORE the task
        # text, so a worker re-queueing its own placeholder with better prose upgrades that
        # row instead of forking a twin beside it. Without this flag the only way to reach
        # that dedup was to import the module, so every CLI caller queueing for a message
        # the owner had already been given a placeholder for silently doubled it.
        smid = argv[argv.index("--source-mid") + 1] if "--source-mid" in argv else ""
        # The channel that ask arrived in. build_worker_prompt() renders it into the lane's
        # REPLY VIA line, so the finished build answers where the owner asked rather than in
        # #general. 15:40 on 9th July: the live session queued his shred question from
        # #deadlock-research with neither flag, and the answer came back to the wrong room.
        scid = argv[argv.index("--source-channel") + 1] if "--source-channel" in argv else ""
        # A TEST FIXTURE (`__name__`) is not a build. A verify command that proves the queue
        # works has to put a real entry through the real file, and it has no files to declare
        # because it edits none. Exempt it from the touch refusal and FORCE-GATE it, so the
        # pump can never hand a fixture a lane in the second it exists.
        if _is_fixture(pos[0]):
            gate, solo = FIXTURE_TASK_GATE, True
            _log(f"fixture queued (gated on {FIXTURE_TASK_GATE}, unpumpable): {pos[0][:60]}")
        # A touch-set is MANDATORY (the owner, 9th July). Judge the entry as it will EXIST, not as
        # it was typed: re-queueing a declared task to bump its priority passes no --touch, and
        # enqueue() preserves the old set- that must keep working. Only an entry that would end
        # up genuinely undeclared is refused.
        # The POLICY now lives on enqueue(); this only renders it. Vetting here as well is
        # deliberate and not redundant: it keeps `--queue`'s exit code 2 and its guidance
        # text, and it refuses before the `--solo`/inherited-touch reasoning below runs.
        try:
            for w in vet_touch(touch):
                print(f"note: {w}")
        except BadTouchSet as bad:
            print(bad.report())
            return 2
        # Same identity ordering enqueue() uses: the message id is exact, the task text is
        # prose. Matching text alone would make a REWORDED re-queue of an already-declared
        # entry look undeclared, and refuse the very upgrade --source-mid exists to allow.
        _q = queue_read()
        inherited = (next((e.get("touch_set") for e in _q
                           if smid and str(e.get("source_mid") or "") == smid), None)
                     or next((e.get("touch_set") for e in _q if e.get("task") == pos[0]), None))
        if not touch and not inherited and not solo:
            print('refused: no touch-set. Declare what the task will edit-\n'
                  '  --touch "utils/baxter_usage.py/ceiling,utils/coc_bot/,@probe"\n'
                  'or, if it genuinely cannot be scoped yet, say so out loud with --solo.\n'
                  'An undeclared task clashes with everything and runs alone, so a queue full of\n'
                  'them leaves the second lane idle. This used to be the silent default.')
            return 2
        ver = argv[argv.index("--verify") + 1] if "--verify" in argv else ""
        vas = argv[argv.index("--verify-assert") + 1] if "--verify-assert" in argv else ""
        dup_ok = "--dup-ok" in argv
        # THE PRD GATE (the owner, 9th July: "a rigorous prd before it gets filed"). A big task
        # arrives as a document or it does not arrive. `--prd-exempt` is the vitals bypass-
        # a CoC-bot fix cannot wait on an Opus round-trip- and every use of it is appended to
        # the rejects log. A bypass nobody can count becomes the norm, and the guard's own log
        # is how the owner knows the gate is alive (9th July 08:46).
        prd_exempt = "--prd-exempt" in argv
        # `--queue` is the ONE caller that opts into semantic dedup, because it is the boundary
        # where a fresh human or agent ask enters. halt(), baxter_triage._park, the fast lane's
        # placeholder and baxter_autobuild each legitimately re-state a task whose own lane
        # journal is still alive, and keep enqueue()'s dedup=False default: reach one of those
        # with the guard on and a halting build refuses itself as a duplicate of its own journal.
        # `solo` RIDES THE WRITE (10th July). It was computed here, spent on the refusal check
        # below, and then thrown away- so a deliberate `--solo` landed in the queue file
        # indistinguishable from the fast lane's unscoped placeholder, and both got the board.
        kw = dict(note=note, state_summary=stt, priority=prio, touch_set=touch, gated_on=gate,
                  verify=ver, verify_assert=vas, source_mid=smid, source_channel=scid,
                  prd_required=not prd_exempt, solo=solo)
        # enqueue() announces the slot for EVERY caller- halt(), the fast lane's placeholder,
        # every direct import- and that print stays exactly where it is. `--queue` alone catches
        # it, because `--queue` alone then printed a SECOND line carrying the gate, the solo
        # declaration and the missing exam. Both lines were true, which is what made two of them
        # worth removing. The slot line comes back below with that detail folded into it.
        import io
        _cap = io.StringIO()
        try:
            with contextlib.redirect_stdout(_cap):
                if dup_ok:
                    entry = enqueue(pos[0], pos[1], **kw)
                else:
                    entry = enqueue(pos[0], pos[1], dedup=True, **kw)
        except DuplicateTask as dup:
            # The redirect is already unwound; the refusal below prints to the real stdout.
            sys.stdout.write(_cap.getvalue())
            # The refusal the owner sees is an EXIT CODE. Printing without returning 2 would let the
            # caller carry on believing the task was scheduled- the whole bug, one layer up.
            reason = (f"near-duplicate of {dup.colliding_id}: {dup.colliding_task[:80]}"
                      if dup.colliding_id else f"near-duplicate: {dup.colliding_task[:80]}")
            print(f"refused: near-duplicate of {dup.colliding_id or '<unknown id>'}\n"
                  f"         already queued or in flight: {dup.colliding_task[:100]}\n"
                  f"         new ask: {pos[0][:100]}\n"
                  "Two lanes on one stale ticket is what this guard exists to stop. Check whether\n"
                  "the work has already shipped; if this really is a second pass over the same\n"
                  "subsystem, say so out loud with --dup-ok.")
            # A guard with no log reads as a guard that never fires (the owner, 9th July 08:46).
            record_reject(pos[0], reason, kind="duplicate", priority=prio)
            return 2
        except MissingPRD as miss:
            sys.stdout.write(_cap.getvalue())
            reason = (f"--note {miss.note} is not a document inside {Path(PRD_DIR).name}/"
                      if miss.note else "filed as a bare task string, with no PRD behind it")
            # REFUSE THE BUILD. NEVER LOSE THE ASK. (the owner, 10th July: "my builds in the build q
            # are disappearing... I've asked like 5 times to do it but it keeps disappearing.")
            # This guard landed at ~09:45 and by 14:05 it had silently destroyed FOURTEEN of his
            # asks- including the Codex doctor-themed pfp and the drag-and-drop queue UI he had
            # asked for repeatedly. The refusal printed to a caller's stdout that nobody read,
            # wrote one line to the rejects log, and returned 2. The ask itself went nowhere.
            #
            # A guard is allowed to say "not like this". It is never allowed to be the reason a
            # thing the owner asked for ceases to exist. So the ask is PARKED: it lands in the queue
            # gated on 'prd', which is not a GATE_NONE value, so the pump can never hand it a
            # lane- but /queue shows it, --edit reaches it, and writing its PRD and lifting the
            # gate is all it takes to build it.
            #
            # AND A PARK IS AN ACCEPTANCE, SO IT EXITS 0 (the owner, 11th July: "im asking you to do a
            # task. your purpose is to send this to the PM to create the PRD lol"). This branch
            # used to print "refused: no PRD." and return 2 having ALREADY parked the ask and
            # spawned the PM. The exit code is the only thing a caller reliably reads, so every
            # session reported a failure for work that had in fact been accepted and routed. The
            # routing was never the bug; the report was. Only a park that genuinely LOST the ask
            # keeps `refused:` and 2- that is the one outcome where something is actually gone.
            parked = None
            already = False
            _park = io.StringIO()
            try:
                with contextlib.redirect_stdout(_park):
                    parked = enqueue(
                        pos[0],
                        "No PRD yet. The PM is drafting one: a PM Opus writes the form, the machine "
                        "validator checks its shape, and a manager Opus returns greenlight/changes/"
                        "reject- the PM amends against those reasons and tries again. prd_sweep() "
                        "lifts this gate the moment a real PRD stands behind the entry.",
                        note=note, state_summary=stt, priority=prio, touch_set=touch,
                        gated_on=PRD_GATE, verify=ver, verify_assert=vas, source_mid=smid,
                        source_channel=scid, solo=solo, prd_required=False, vet=False, dedup=True)
            except DuplicateTask:
                already = True             # already waiting on the PM from an earlier ask
            except Exception as exc:
                # A CRASH IS NOT A REFUSAL (the owner, 11th July). This handler logged one line and
                # fell through to `refused: no PRD.` + return 2- the house code for a POLICY
                # refusal- so a ModuleNotFoundError raised inside enqueue() came back to the
                # caller wearing the PRD gate's name. The dup_guard exam read its own crash as
                # the gate refusing its fixture and sat red for a day behind a green-looking
                # policy line. The park is where the ask is SAVED, so a fault HERE means the ask
                # is genuinely gone: name the class and exit 4, a code main() has never spoken
                # (0 accepted, 1 you typed it wrong, 2 the guard refused you, 3 the governor
                # blocked you, 4 the guard itself broke). Keyed on "not a queue-policy exception",
                # never on a list of classes- the incident was an import, the next one will not be.
                import traceback
                _log(f"the PRD park CRASHED ({exc.__class__.__name__}: {exc}): {pos[0][:60]}\n"
                     + traceback.format_exc())
                for _line in _park.getvalue().splitlines():
                    if not _line.strip().startswith("queued at"):
                        print(_line)
                # The row lands under its own kind, so `--rejects` can count a crashed park
                # against a refused one. record_reject is documented never to raise; if that ever
                # changes, the fault report is what must survive, not the log row.
                try:
                    record_reject(pos[0], f"the park crashed: {exc.__class__.__name__}: {exc}",
                                  kind="park_failed", priority=prio)
                except Exception as rex:
                    _log(f"record_reject failed while filing a crashed park: {rex}")
                # The exception message is DATA: printed, never interpolated into a format string,
                # never handed to _say. Stderr, because stdout is where a caller reads the slot.
                print("Sir, the park CRASHED. This is not the PRD gate.\n"
                      f"         {exc.__class__.__name__}: {exc}\n"
                      f"         raised inside enqueue() while parking: {pos[0][:100]}\n"
                      "The ask is NOT queued and NOT gated- it is in the rejects log as\n"
                      "kind=park_failed, with the traceback in .baxter.log. Fix the fault, then\n"
                      "queue it again.", file=sys.stderr)
                return 4
            # enqueue() announces the slot to every caller, and here that line is a lie: nothing
            # is queued to RUN. It is dropped; anything else it printed still reaches the caller.
            for _line in _park.getvalue().splitlines():
                if not _line.strip().startswith("queued at"):
                    print(_line)
            # ...and hand it straight to the PM rather than waiting for the next governor beat.
            # Best-effort: prd_sweep() will pick it up regardless if this spawn never lands.
            drafting = False
            if parked and not os.environ.get("BAXTER_NO_PM_SWEEP"):
                try:
                    if not _pm_running():
                        _spawn_pm_for(parked)
                        _log(f"PM drafting a PRD for [{parked.get('id')}] (parked at --queue)")
                    drafting = True
                except Exception as exc:
                    _log(f"PM spawn failed at the park: {exc.__class__.__name__}: {exc}")
            # A guard with no log reads as a guard that never fires (the owner, 9th July 08:46). The
            # row lands whichever way this went: an accepted park is still the gate biting.
            record_reject(pos[0], reason, kind="prd_missing", priority=prio)
            if parked or already:
                _pid = f"[{parked.get('id')}]" if parked else "the entry already waiting on it"
                print("accepted- no PRD yet.\n"
                      f"         {reason}\n"
                      f"Parked as {_pid}, gated on {PRD_GATE!r}, so no lane can start it.\n"
                      + ("The PM is drafting the document now; the gate lifts the moment it lands."
                         if drafting else
                         "The PM sweep has it; the gate lifts the moment a PRD stands behind it.")
                      + "\nA vital that genuinely cannot wait for a spec passes --prd-exempt, and the\n"
                        "bypass is written to the rejects log.")
                return 0
            print(f"refused: no PRD.\n"
                  f"         {reason}\n"
                  "A build lane's whole specification is the two strings you just typed. Write the\n"
                  "document first- a PM Claude fills the form, a manager greenlights it, and the\n"
                  "entry is filed carrying it:\n"
                  '  python "...\\utils\\baxter_pm_delegate.py" --ask "<the raw ask>" --queue-it\n'
                  "A vital that genuinely cannot wait for a spec passes --prd-exempt, and the\n"
                  "bypass is written to the rejects log."
                  "\nWARNING: the ask could not be parked- it exists only in the rejects log.")
            return 2
        # Anything else enqueue() printed is not ours to swallow- a future warning from inside
        # the write must still reach him. Only the announcement is replaced.
        for _line in _cap.getvalue().splitlines():
            if not _line.strip().startswith("queued at"):
                print(_line)
        # THE BYPASS IS LOGGED, ALWAYS. A guard that never visibly refuses is a guard nobody
        # trusts; one that cannot show who walked round it is worse. A fixture is not a bypass-
        # it names no build- so it is not logged as one.
        if prd_exempt and not _is_fixture(pos[0]):
            record_reject(entry, "queued with --prd-exempt: no PRD, no PM review",
                          kind="prd_exempt")
        # The real slot, from the entry the write returned, carrying the priority as `pN`- so
        # `queued at priority N` was only ever restating it.
        print(f"queued at {position_line(entry)}"
              + (f"- GATED on {gate}, the pump will not start it until it is ungated"
                 if gate else "- runs when a lane frees and the curve is open")
              + ("" if (touch or inherited) else "; declared SOLO, so it will not share a lane")
              # Since the planner tier (9th July) an undeclared task is no longer doomed to land
              # unverified: a planner writes and seals its acceptance test before the executor
              # starts. Declaring one here is still stronger- it comes from further above still,
              # and it saves the planner spawn.
              + ("; PRD-EXEMPT- filed with no spec, and the bypass is in the rejects log"
                 if prd_exempt and not _is_fixture(pos[0]) else "")
              + ("" if (ver or vas) else "; no --verify declared- the planner will write one "
                                         "before the build starts, though declaring it here is stronger"))
        return 0
    if "--edit" in argv:
        # `--edit <id-or-substring> [--retext "..."] [--priority N] [--touch "a,b"] ...`
        # The one verb the queue never had. Priority moves BOTH ways here.
        i = argv.index("--edit")
        pos = [a for a in argv[i + 1:] if not a.startswith("--")]
        if not pos:
            print('usage: --edit "<id or unambiguous substring>" [--retext "<new text>"] [--priority N]\n'
                  '              [--touch "a,b,@c"] [--verify "<cmd>"] [--verify-assert "<claim>"]\n'
                  '              [--note <path>] [--next "<next step>"] [--state "<summary>"]\n'
                  '              [--gate owner|none] [--force]')
            return 1
        def _opt(flag):
            return argv[argv.index(flag) + 1] if flag in argv and len(argv) > argv.index(flag) + 1 else None
        try:
            prio = int(_opt("--priority")) if "--priority" in argv else None
        except (TypeError, ValueError):
            print("refused: --priority needs a number 1-8")
            return 1
        touch = _touch_arg(argv)
        if touch:
            try:
                for w in vet_touch(touch):
                    print(f"note: {w}")
            except BadTouchSet as bad:
                print("refused: " + "\n         ".join(bad.refusals))
                return 2
        ok, msg, changes = queue_edit(
            pos[0], force=("--force" in argv),
            retext=_opt("--retext"), priority=prio, touch_set=touch,
            verify=_opt("--verify"), verify_assert=_opt("--verify-assert"),
            note=_opt("--note"), next_step=_opt("--next"), state_summary=_opt("--state"),
            gated_on=_gate_arg(argv),
            # None, not False, when the flag is absent: queue_edit drops None fields, so an
            # edit that only bumps priority must not silently un-solo a task nobody scoped.
            solo=(True if "--solo" in argv else None))
        print(msg)
        for k, before, after in changes:
            print(f"  {k}: {str(before)[:60]!r}\n    -> {str(after)[:60]!r}")
        return 0 if ok else 1
    if "--drop" in argv:
        i = argv.index("--drop")
        pos = [a for a in argv[i + 1:] if not a.startswith("--")]
        if not pos:
            print('usage: --drop "<id or unambiguous substring>" [--force]')
            return 1
        ok, msg = queue_drop(pos[0], force=("--force" in argv))
        print(msg)
        return 0 if ok else 1
    if "--undrop" in argv:
        # The undo half of --drop. BY ID ONLY, and no --force: an id is the one handle a dropped
        # entry certainly still has, and the one that cannot quietly resolve to another task.
        i = argv.index("--undrop")
        pos = [a for a in argv[i + 1:] if not a.startswith("--")]
        if not pos:
            print('usage: --undrop "<id>"\n'
                  "       The id --drop printed. Substrings are not accepted here: the entry is\n"
                  "       out of the queue, so there is no prose left to match it against.")
            return 1
        ok, msg = queue_undrop(pos[0])
        print(msg)
        return 0 if ok else 1
    if "--ungate" in argv:
        # the owner said go: lift the human gate so the pump can pick the task up.
        i = argv.index("--ungate")
        pat = (argv[i + 1] if len(argv) > i + 1 else "").strip().lower()
        if not pat:
            print('usage: --ungate "<substring of the task text>"')
            return 1
        with queue_txn():
            q = queue_read()
            hit = [e for e in q if pat in str(e.get("task", "")).lower() and is_human_gated(e)]
            if not hit:
                print(f"no gated queue entry matches {pat!r}")
                return 1
            for e in hit:
                e["gated_on"] = ""
                _log(f"gate lifted: {str(e.get('task',''))[:60]}")
                print(f"ungated: {str(e.get('task', '?'))[:70]}")
            queue_write(q)
        return 0
    if "--rejects" in argv:
        # The guard's proof of life: `--rejects [N]` / `--rejects --since 2026-07-09`.
        i = argv.index("--rejects")
        given = len(argv) > i + 1 and not argv[i + 1].startswith("--")
        since = argv[argv.index("--since") + 1] if "--since" in argv else ""
        try:
            n = int(argv[i + 1]) if given else (100000 if since else 20)
        except ValueError:
            n = 20
        rows = read_rejects(n, since)
        if not rows:
            print(f"no rejections logged{f' since {since}' if since else ''}"
                  f" ({REJECT_LOG.name})")
            return 0
        print(f"{len(rows)} rejection(s){f' since {since}' if since else ''}, newest first:")
        for r in rows:
            where = f" off lane {r['lane']}" if r.get("lane") else ""
            print(f"  {r.get('at', '?')}  [{r.get('kind', '?')}]{where}  "
                  f"p{r.get('priority', '?')} {str(r.get('task', '?'))[:56]}\n"
                  f"      {r.get('reason', '?')}")
        return 0
    if "--move" in argv:
        # `--move <id-or-substring> <up|down|top|bottom|N> [--force]`. The cozy panel's engine,
        # on the shell. Prints the before/after run order so a move is visible without Discord.
        i = argv.index("--move")
        pos = [a for a in argv[i + 1:] if not a.startswith("--")]
        if len(pos) < 2:
            print('usage: --move "<id or unambiguous substring>" <up|down|top|bottom|N> [--force]\n'
                  "       N is a 1-based slot WITHIN the entry's priority band.\n"
                  "       Refused on a task live in a lane (never forceable), on a p1 pin, and\n"
                  "       on a gated entry (--force lifts those last two).")
            return 1
        run_order = lambda: [f"  {n}. [{x.get('id', '?')}] p{_prio_int(x)} {str(x.get('task', ''))[:56]}"
                             for n, x in enumerate(queue_read(), 1)]
        before = run_order()
        ok, msg = queue_move(pos[0], pos[1], force=("--force" in argv))
        print(msg)
        if ok:
            print("\nbefore:\n" + "\n".join(before))
            print("\nafter:\n" + "\n".join(run_order()))
        return 0 if ok else 1
    if "--queue-list" in argv:
        q = queue_read()
        if not q:
            print("queue empty")
        # Each entry's slot WITHIN its band, so a --move is visible from the shell. The global
        # number tells him nothing about what a move can reach: moves never cross a band.
        size, slot = {}, {}
        for e in q:
            p = _prio_int(e)
            size[p] = size.get(p, 0) + 1
            slot[id(e)] = size[p]
        for n, e in enumerate(q, 1):
            t = sorted(touch_of(e))
            g = gate_of(e)
            p = _prio_int(e)
            # the id leads the line: it is the handle `--edit`/`--drop`/`--move` take
            print(f"{n}. [{e.get('id', '????????')}] [p{p} slot {slot[id(e)]}/{size[p]}]"
                  f"{' [GATED on ' + g + ']' if g else ''} "
                  f"{e.get('task', '?')} | next: {e.get('next_step', '?')}"
                  f" | touch: {t if t else 'SOLO (undeclared)'}")
        return 0
    if "--report" in argv:
        # the owner's /usage COMMAND (5th July): compose the basic-info line and SEND it-
        # a pure code path, no LLM turn, near-instant. Use the cached meter when fresh
        # (triage keeps it ≤4 min); only pay the network round-trip if it's >60s stale.
        snap = read_meters()
        try:
            age = (datetime.now() - datetime.fromisoformat(snap.get("updated", ""))).total_seconds()
        except Exception:
            age = 1e9
        if age > 60:
            snap = probe(force=True)
        line = report_line(snap)
        reply_to = argv[argv.index("--reply-to") + 1] if "--reply-to" in argv else None
        channel = argv[argv.index("--channel") + 1] if "--channel" in argv else None
        say = ["python", SAY]
        if channel:
            say += ["--channel", channel]
        if reply_to:
            say += ["--reply-to", str(reply_to)]
        say += [line]
        env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
        try:
            subprocess.run(say, env=env, timeout=30)
        except Exception as e:
            _log(f"--report say failed: {e}")
        print(line)
        return 0
    if "--live" in argv:
        # Seed/refresh the baked /usage line from the current meter (used to
        # initialise .baxter_usage_live.json; probe() keeps it fresh thereafter).
        snap = read_meters()
        write_live(snap)
        print(report_line(snap))
        return 0
    if "--status" in argv:
        print(_status_line(read_meters()))
        return 0
    print(_status_line(probe(force=True)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
