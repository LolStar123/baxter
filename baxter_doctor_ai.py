r"""baxter_doctor_ai - Codex and Jem diagnose Baxter; agreement is the only thing that repairs him.

THE SHAPE. A read-only `collect_bundle()` assembles evidence that survives a crash (stale
heartbeats, duplicate singletons, abandoned locks, the watcher's log tail). That bundle goes
to BOTH analysts in parallel through the existing `baxter_coop.py` fan-out. They diagnose
BLIND- neither sees the other's answer, nor its own prior one. A repair runs only when they
return the IDENTICAL action on the IDENTICAL target, that action is on a closed WHITELIST,
and the target is not PROTECTED. Everything else- disagreement, prose, an error envelope,
an off-whitelist action, a doctor pointing at its own life-support- is refused, written to
the audit log, and flagged to the owner. Refusal is the default; execution is the narrow exception.
When both doctors report no fault the pass is 'healthy': audited, and silent. Only a fault
ever reaches the owner.

WHY TWO ENGINES. Codex runs on the ChatGPT sub, Jem on Gemini. Correlated failure is the
thing being bought off: one hallucinated diagnosis cannot move the machine. The bundle is
still a SHARED input, so a misleading bundle correlates them anyway- which is why every
symptom below is measured, never inferred, and why the whitelist bounds the blast radius to
three reversible acts even when both are wrong.

THE TRAPS THIS CODE IS BUILT AROUND (each one found by reading the live fleet, 9th July):

  * HEARTBEAT STAMPS ARE NOT ALL NAIVE. The watcher writes `...+01:00`, the WhatsApp bridge
    writes `...Z`, triage writes a naive local stamp. `datetime.now() - fromisoformat(x)`
    raises TypeError on the first two, and an implementation that swallowed that as "never
    beat" would hand both doctors "the watcher is dead" four seconds after it beat. Ages are
    computed tz-aware, always. (The sealed exam writes only naive stamps, so it cannot catch
    this: green there, catastrophic here.)

  * FLEET LOCKS DO NOT RECORD A PID. `baxter_send_dedup.file_lock` creates an EMPTY file;
    `.baxter_usage_cmd.lock` holds a float timestamp (`1783621206.6464198`). Parsed as an
    owner PID that is a dead process id, and the lock a LIVE poller is holding gets deleted
    under it- two writers, one state file. So an owner is only believed when the content is a
    plausible PID, and a lock is only ever cleared when its owner is not provably ALIVE.

  * A LOCK CAN BE RE-TAKEN WHILE THE DOCTORS THINK. coop's timeout is 900s; a lock that was
    abandoned when the bundle was collected may be freshly held by the time the verdict comes
    back. `clear_stale_lock` therefore re-verifies staleness AT UNLINK TIME and refuses on any
    doubt. The bundle proposes; the executor is what decides.

  * A DOCTOR MUST NOT DIAGNOSE ITSELF. Codex's and Jem's heartbeats are structurally excluded
    from the bundle (any beat file whose name carries a PROTECTED token is dropped), and
    PROTECTED blocks any repair aimed at them or at `baxter_coop_guardian`, which is their
    only supervisor. Doctoring is aimed at Baxter, not at the coop.

  * A DOCTOR THAT RUNS EVERY TWO MINUTES WILL RESTART THE WATCHER FOREVER AND CALL IT HEALING.
    Every (action, target) pair is on a 900s cooldown, stamped BEFORE the executor runs, so a
    repair that crashes mid-flight still cannot flap.

CLI (outward paths armed LAST- `--bundle` is safe, `--once` is the one that touches the box):
    python baxter_doctor_ai.py --bundle     # print the evidence as json. No LLM, no repair.
    python baxter_doctor_ai.py --dry-run    # ask both doctors, decide, audit. Execute nothing.
    python baxter_doctor_ai.py --once       # a real diagnose-and-repair pass.

See `50-Research/Decouple Codex + Jem + collab-doctoring - plan of attack.md`.
"""
import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(r"C:\Users\you\Documents\Baxter")
UTILS = Path(__file__).resolve().parent
PY = sys.executable

AUDIT_PATH = VAULT / ".baxter_doctor_ai.jsonl"
FLAGS_PATH = VAULT / ".baxter_doctor_ai_flags.jsonl"
COOLDOWN_PATH = VAULT / ".baxter_doctor_ai_cooldown.json"
COOLDOWN_SEC = 900

NO_WIN = 0x08000000

# ---------------------------------------------------------------------------- the closed sets
#
# Four reversible acts. Editing code, touching secrets, moving the usage bands, anything
# outward- never executed, whatever the doctors agree on. the owner's standing line: the code
# touches his everyday life.
#
# `free_lane` earns its place on the same test as the other three: it is REVERSIBLE. It kills
# a wedged lane's process tree and deletes nothing- the resume journal survives, the lane
# reaper finds a corpse with no exit code, `baxter_verify.classify` calls that transient, and
# the build is retried on the same journal (measured 10th July: rc None -> transient -> retry).
# Nothing is lost; a stuck build is un-stuck.
WHITELIST = frozenset({"restart_component", "clear_stale_lock", "reap_duplicate", "free_lane"})

# The doctors and the guardian that restarts them. A doctor that can restart its own
# life-support can put itself into a loop no one is left to break.
PROTECTED = frozenset({"codex", "jem", "coop_guardian"})

# ------------------------------------------------------------------------------- heartbeats
#
# The doctor is deliberately SLOWER to cry than health_monitor (150s) and the coop guardian
# (120s): those two only alert or restart something they own, this one hands a symptom to an
# LLM. A beat must be properly dead, not merely late.
BEAT_STALE_SEC = 300
BEAT_STALE_OVERRIDE = {
    "triage": 900,          # a stamp per triage PASS, and a heavy pass legitimately runs minutes
}
# The channel keeper is an expected-ZERO row in baxter_doctor.ps1- retired, and its last beat
# literally reads "stopped". Reporting a 21-hour-old beat for a component that is meant to be
# off is how you get two doctors agreeing to restart something nobody wants running.
IGNORED_BEATS = {".baxter_channel_heartbeat.txt"}
BEAT_ALIASES = {"watch": "watcher", "wa": "whatsapp_bridge", "": "triage"}

# A lock the fleet's own 30s steal rule (baxter_send_dedup._acquire_at) would have taken ten
# times over. Nothing in the fleet holds a lock for more than a few hundred milliseconds.
LOCK_STALE_SEC = 300

# ------------------------------------------------------------------------------- free_lane
#
# The bundle can be 900s stale by the time both doctors answer- that is coop's fan-out timeout.
# A lane that was flat then may be reasoning now, so every life witness is re-measured HERE,
# against the machine as it is at the instant of the kill. The bundle proposes; the executor
# decides. Exactly as `clear_stale_lock` re-verifies staleness at unlink time.
#
# CPU_EPS and the tree-cpu witness are `baxter_stuck_doctor`'s, reused rather than reinvented:
# a lane's worker blocks in `subprocess.run` for the whole build and burns 0.000s of its OWN
# cpu, so a self-cpu witness calls every healthy lane flat.
FREE_LANE_SAMPLE_SEC = 6         # a fresh cpu sample, taken now, not read off the bundle
CPU_EPS = 1.0                    # seconds of tree cpu that count as "it did something"

# Whose LONE diagnosis may execute. Read, never written- arming is the owner's word, and
# `baxter_doctor_ai_task.ps1` already refuses to install `-Mode once` without this file.
ARMED_NAME = ".baxter_doctor_ai_armed.json"

LOG_TAIL_BYTES = 8192
LOG_TAIL_LINES = 40
TRIAGE_EXIT_RE = re.compile(r"^.*\b(?:triage[^\n]*?exit|exit(?:\s+code)?)\s+([1-9]\d*)\b.*$", re.I)

# --------------------------------------------------------------------------- the components
#
# Every restartable component, keyed by its canonical name, matched EXACTLY as
# baxter_doctor.ps1 matches it (binary NAME + command line, never a bare cmdline substring-
# a substring census counts the very shell running it, [[process-census-match-name-not-cmdline]]).
#
# `restart` here means KILL, never relaunch. Each of these already has a supervisor that
# revives it within a known window, and that supervisor is the verified primitive. The doctor
# spawning its own replacement is how you manufacture the duplicate you exist to reap.
COMPONENTS = {
    "watcher": {
        "name": r"powershell",
        "cmd": r'-File\s+"?[^"]*baxter_watch\.ps1"?(?!.*-(?:ReviveWatchdogOnce|EmitWatchSet|SelfTest))',
        "supervisor": "baxter_guardian.ps1 (Startup loop, revives within 4 min)",
    },
    "slash_listener": {
        "name": r"python\.exe", "cmd": r"baxter_slash\.py",
        "supervisor": "baxter_watch.ps1 (relaunches each beat)",
    },
    "whatsapp_bridge": {
        "name": r"node\.exe", "cmd": r"bridge\.mjs",
        "supervisor": "baxter_watch.ps1 (relaunches each beat)",
    },
    "coc_discord": {
        "name": r"python", "cmd": r"coc_discord\.py",
        "supervisor": "watchdog.py --loop",
    },
    "coc_autopilot": {
        "name": r"python", "cmd": r"coc_autopilot\.py",
        "supervisor": "watchdog.py --loop",
    },
}

BRIEF = (
    "You are a diagnostician for 'Baxter', a fleet of long-lived Windows processes. The attached "
    "context file is a SYMPTOM BUNDLE: measured evidence only (stale heartbeats, duplicate "
    "singleton processes, abandoned lock files, the watcher's log tail, plus the two sections "
    "below). Diagnose the single most likely fault and name ONE repair.\n\n"
    "'silence' is the unanswered-work section. 'unanswered' lists message ids that Baxter reacted "
    "to and then never answered- no handled claim, and no reply in the send ledger- aged from the "
    "message id itself. A NON-EMPTY list means Baxter acknowledged the owner and went quiet: something "
    "in the reply path has stopped. An EMPTY list is the normal state and is not a fault.\n\n"
    "'lane_clog' is the build board. 'live' lanes are running, 'pending' entries are queued, "
    "'lane_count' is the maximum. 'clogged' is true only when every lane is occupied, work is "
    "waiting, and no lane's journal has changed for the whole clog window- a board that is full "
    "and not moving. 'wedged' lists lanes that a separate probe has already measured as having "
    "burned no cpu and written nothing for at least half an hour.\n\n"
    "Reply with a STRICT single json object and NOTHING else- no prose, no markdown, no preamble:\n"
    '{"action": "<one of: restart_component | clear_stale_lock | reap_duplicate | free_lane | '
    'none>", "target": "<component name, lock filename, journal name, or empty>", '
    '"reason": "<one short sentence>"}\n\n'
    "Rules: if the bundle shows no fault, use action \"none\". Never invent a symptom the bundle "
    "does not contain. 'target' for restart_component is one of: "
    + ", ".join(sorted(COMPONENTS)) + ". 'target' for clear_stale_lock is the lock's filename "
    "exactly as it appears in stale_locks. 'target' for reap_duplicate is the component name "
    "exactly as it appears in duplicates. 'target' for free_lane is the JOURNAL FILENAME exactly "
    "as it appears in lane_clog.wedged[].journal or lane_clog.journals- never a lane number. "
    "free_lane kills that lane's stuck worker so the board reopens and the build is retried from "
    "its journal; propose it ONLY for a lane the bundle itself calls wedged or clogged. "
    "A wrong repair is far worse than 'none'."
)


class Refused(Exception):
    """The executor declined. Never an error- a safety property doing its job."""


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _norm(s):
    """'whatsapp bridge' / 'WhatsApp Bridge' / 'whatsapp_bridge' all collapse to one key."""
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").strip().lower()).strip("_")


def _is_protected(name):
    """Structural, not a hardcoded list: any name carrying a protected token is protected."""
    n = _norm(name)
    return any(p in n for p in PROTECTED)


# =========================================================================== 1. PID liveness
def pid_alive(pid):
    """True | False | None (unknown). None NEVER licenses a repair- unknown means hands off.

    ERROR_ACCESS_DENIED (5) means the process EXISTS and is simply not ours to open: that is
    ALIVE, and reading it as dead would let the doctor clear a lock held by a SYSTEM process.
    ERROR_INVALID_PARAMETER (87) is the only clean "no such pid".
    """
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p          # a HANDLE is 64-bit; the default
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]  # c_int truncates it
        h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            err = ctypes.get_last_error() or k.GetLastError()
            if err == 5:
                return True
            if err == 87:
                return False
            return None
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k.CloseHandle(ctypes.c_void_p(h))
        if not ok:
            return None
        return code.value == 259                         # STILL_ACTIVE
    except Exception:
        return None


def _lock_owner(raw):
    """The PID recorded in a lock file, or None if it records no such thing.

    Most of the fleet's locks record NOTHING: `file_lock` creates an empty file, and
    `.baxter_usage_cmd.lock` holds a float epoch (`1783621206.6464198`). Both must return
    None- an epoch coerced to an int is a dead PID, and that is a licence to delete a lock a
    live poller is holding. Only an all-digit token inside Windows' practical PID range counts.
    """
    t = (raw or "").strip().split()[0] if (raw or "").strip() else ""
    if not t.isdigit():
        return None
    v = int(t)
    return v if 0 < v < 4194304 else None


# ============================================================================ 2. the evidence
def _beat_age(path):
    """Seconds since the component last beat, tz-correct, or None if it cannot be known.

    A torn read (the watcher rewriting the file as we read it) yields an unparseable stamp,
    never a symptom: we fall back to mtime, and failing that we report nothing at all.
    """
    try:
        raw = path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    stamp = raw.split("\t")[0].split("\n")[0].strip()
    try:
        d = datetime.fromisoformat(stamp)
    except ValueError:
        try:
            return max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            return None
    if d.tzinfo is None:
        d = d.astimezone()                               # naive stamps are local time
    return (datetime.now(timezone.utc) - d).total_seconds()


def _stale_beats(vault):
    out = []
    for p in sorted(vault.glob(".baxter_*heartbeat*.txt")):
        if p.name in IGNORED_BEATS or _is_protected(p.name):
            continue                                     # no self-diagnosis, no retired rows
        raw = p.name[len(".baxter_"):].replace("heartbeat.txt", "").strip("_")
        comp = BEAT_ALIASES.get(raw, raw)
        age = _beat_age(p)
        if age is None:
            continue                                     # unknown is not evidence
        limit = BEAT_STALE_OVERRIDE.get(comp, BEAT_STALE_SEC)
        if age > limit:
            out.append({"component": comp, "age_sec": int(age), "threshold_sec": limit,
                        "beat": p.name})
    return out


def _census_dupes():
    """Duplicate singletons, straight from baxter_doctor.ps1's own matcher.

    Called with -DupesOnly and WITHOUT -Reap: that combination reaps nothing (`Invoke-Reap`
    is gated on -Reap) and skips the one write the script otherwise makes- clearing the
    watcher's `.baxter_dupe_alerted.txt` ping cooldown. So it is a pure read, and the matcher
    can never drift from the one the fleet actually polices itself with.
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(UTILS / "baxter_doctor.ps1"), "-DupesOnly"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, creationflags=NO_WIN)
        return parse_dupes(r.stdout or "")
    except Exception:
        return []                                        # a failed census is not a symptom


def parse_dupes(stdout):
    """DUPE/EXTRA rows -> [{component, count, pids}]. Codex and Jem are dropped: their rows
    exist in the doctor's fleet table, but a doctor must not be handed its own duplicate."""
    out = []
    for line in (stdout or "").splitlines():
        m = re.match(r"^\s*(DUPE|EXTRA)\s+(\S.*?)\s{2,}(\d+)\b.*?PIDs:\s*(.+?)\s*$", line)
        if not m:
            continue
        comp = _norm(m.group(2))
        if _is_protected(comp):
            continue
        pids = [int(x) for x in re.findall(r"\d+", m.group(4))]
        out.append({"component": comp, "count": int(m.group(3)), "pids": pids})
    return out


def _stale_locks(vault):
    out = []
    try:
        locks = sorted(vault.glob("*.lock"))
    except OSError:
        return out
    for p in locks:
        try:
            age = time.time() - p.stat().st_mtime
            raw = p.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        owner = _lock_owner(raw)
        alive = pid_alive(owner) if owner else None
        # Provably alive -> never. Otherwise the fleet's own 30s steal rule, given 10x slack.
        if age > LOCK_STALE_SEC and alive is not True:
            out.append({"lock": p.name, "age_sec": int(age), "owner_pid": owner,
                        "owner_alive": alive})
    return out


def _log_tail(vault):
    p = vault / ".baxter_watch.log"
    try:
        with p.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - LOG_TAIL_BYTES)
            fh.seek(start)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if start and lines:
        lines = lines[1:]                                # the seek landed mid-line
    return "\n".join(lines[-LOG_TAIL_LINES:])


# ------------------------------------------------------------------ lanes: the two new sections
#
# Both sides import lazily. `baxter_doctor_trigger` imports THIS module for its beat scan and
# its lock scan, so a top-level import either way is circular. Both sides tolerate the failure:
# a source that cannot be read contributes NOTHING, never a symptom.
def _stuck():
    if str(UTILS) not in sys.path:
        sys.path.insert(0, str(UTILS))
    import baxter_stuck_doctor
    return baxter_stuck_doctor


def _trigger():
    if str(UTILS) not in sys.path:
        sys.path.insert(0, str(UTILS))
    import baxter_doctor_trigger
    return baxter_doctor_trigger


def _lane_witness(vault, name):
    """(pid, journal content hash) for one lane journal, as they are RIGHT NOW."""
    p = Path(vault) / ".baxter_resume" / name
    e = _trigger()._read_json(p, {})
    pid = e.get("pid") if isinstance(e, dict) else None
    return (pid if isinstance(pid, int) else None), _stuck().journal_hash(p)


def _wedged_lanes(vault):
    """The stuck-doctor's own `wedged` verdict, carrying the witnesses free_lane re-measures.

    `pid` and `jhash` are stamped HERE, at bundle time, precisely so the executor can ask
    900 seconds later whether the lane moved. A bundle without them cannot be checked.
    """
    out = []
    for w in _trigger().wedged_lanes(vault):
        pid, jhash = _lane_witness(vault, w["journal"])
        out.append({"journal": w["journal"], "stalled_sec": w["stalled_sec"],
                    "pid": pid, "jhash": jhash})
    return out


def _queue_clog(vault):
    """All lanes occupied, pending work, and no journal advancing. See the trigger's own docstring.

    The trigger's persisted state carries the last-seen journal hashes and when they last
    changed, so `flat_sec` is measured against ITS clock, not a fresh one that always reads 0.
    The state is read, never saved: describing a clog must not consume the trigger's edge.
    """
    t = _trigger()
    c = t.lane_clog_scan(vault, state=t.load_state(vault))
    if c.get("clogged"):
        c["lanes"] = [dict(zip(("journal", "pid", "jhash"), (n,) + _lane_witness(vault, n)))
                      for n in c.get("journals", [])]
    return c


def collect_bundle(vault=VAULT, census=None):
    """The evidence, and nothing but. A pure read: no LLM, no repair, no side effect.

    Every source is individually guarded. A source that cannot be read contributes NOTHING-
    it never contributes a symptom. A doctor acting on a torn read is worse than a doctor
    that saw less than the whole truth.
    """
    vault = Path(vault)
    b = {"generated": _now_iso(), "stale_beats": [], "duplicates": [], "stale_locks": [],
         "log_tail": "", "triage_exits": [], "wedged_lanes": [], "queue_clog": {},
         "errors": []}
    for key, fn in (("stale_beats", lambda: _stale_beats(vault)),
                    ("duplicates", census or _census_dupes),
                    ("stale_locks", lambda: _stale_locks(vault)),
                    ("log_tail", lambda: _log_tail(vault)),
                    ("wedged_lanes", lambda: _wedged_lanes(vault)),
                    ("queue_clog", lambda: _queue_clog(vault))):
        try:
            b[key] = fn()
        except Exception as e:
            b["errors"].append("%s: %r" % (key, e))
    b["triage_exits"] = [ln for ln in b["log_tail"].splitlines() if TRIAGE_EXIT_RE.match(ln)]
    return b


# ========================================================================== 3. the diagnosis
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def _first_object(text):
    """The first balanced {...}, brace-counted, and only if it is a STANDALONE object.

    A regex cannot do the balancing: `reason` may itself hold a brace, so `.*?` stops at the
    first `}` and `.*` swallows the rest of the reply.

    An object nested in an ARRAY is refused outright. `[{"action":"restart_component",...},
    {"action":"reap_duplicate",...}]` is two proposals, and silently taking the first is
    exactly the guessing this module exists to refuse. The brief asks for a single object;
    anything else goes to the owner.
    """
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[" and depth == 0:
            return None                                  # an array reached before any object
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start:i + 1]
    return None


def parse_diagnosis(text):
    """{'action','verdict','target','reason','raw'} - action is None unless a REPAIR was read.

    Prose, an empty reply, a coop error envelope ('(baxter_ask_jem.py FAILED: ...)') and
    malformed json all yield action=None. A diagnosis that cannot be parsed is NEVER guessed
    at: the whole point of two doctors is lost the moment one of them is imagined.

    `verdict` separates the two ways action can be None. A doctor that read the bundle and
    found nothing wrong answers `{"action": "none"}`- the brief demands exactly that- and gets
    verdict='none'. A doctor whose reply could not be read at all gets verdict=None. Both are
    unexecutable, but only the second is a fault: collapsing them made a healthy fleet report
    'no parseable diagnosis' and ping the owner on every scheduled pass.
    """
    out = {"action": None, "verdict": None, "target": None, "reason": None, "raw": text}
    if not isinstance(text, str) or not text.strip():
        return out
    m = _FENCE_RE.search(text)
    blob = _first_object(m.group(1) if m else text)
    if not blob:
        return out
    try:
        obj = json.loads(blob)
    except Exception:
        return out
    if not isinstance(obj, dict):
        return out
    action = obj.get("action")
    if not isinstance(action, str) or not action.strip():
        return out                                       # no action field: malformed, not 'none'
    rsn = obj.get("reason")
    if action.strip().lower() == "none":
        out["verdict"] = "none"                          # 'none' is a verdict, not an action
        out["reason"] = rsn.strip() if isinstance(rsn, str) else None
        return out
    out["action"] = action.strip().lower()
    tgt = obj.get("target")
    out["target"] = tgt.strip() if isinstance(tgt, str) else None
    out["reason"] = rsn.strip() if isinstance(rsn, str) else None
    return out


def armed_authority(vault=VAULT):
    """Whose LONE diagnosis may execute: 'codex', or None. Read, never written.

    the owner asked for Codex to have full authority. That authority is a FILE- his word, recorded
    with his own quote- and never a default. Both `mode=once` and `authority=codex` must be
    present. Absent or malformed yields today's behaviour exactly: consensus required.
    """
    d = _read_armed(vault)
    if not isinstance(d, dict) or str(d.get("mode", "")).strip().lower() != "once":
        return None
    who = str(d.get("authority", "")).strip().lower()
    return who if who == "codex" else None


def _read_armed(vault):
    try:
        return json.loads((Path(vault) / ARMED_NAME).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _gate(action, target, reason, how="agreed"):
    """The closed sets, applied to whatever earned a green light. Authority never widens them:
    Codex has authority over what he may ALREADY do, never over what may be done at all."""
    if action not in WHITELIST:
        return "flag", action, target, "%s action %r is not on the whitelist" % (how, action)
    if _is_protected(target):
        return "flag", action, target, "%r is the doctors' own life-support" % target
    return "execute", action, target, reason


def cross_check(cx, jm, authority=None):
    """(decision, action, target, reason). 'execute' is the ONLY green light, and by default it
    needs all four: both parsed, both identical, action whitelisted, target not protected.

    'healthy' is the fleet's normal state and is SILENT: both doctors read the bundle and
    found no fault. It is not a flag. Flagging it would ping the owner every scheduled pass of a
    perfectly well machine, and the ping would say the doctors could not be parsed when in
    fact they answered clearly ([[no-ping-storms]]).

    `authority='codex'` (from `.baxter_doctor_ai_armed.json`) relaxes ONE thing: a lone
    parseable Codex diagnosis executes when Jem is SILENT- mute, unreadable, or reporting no
    fault. A Jem who parses and CONTRADICTS still flags: a silent Jem is Codex's authority, a
    contradicting Jem is the owner's information. Jem alone never executes, armed or not.
    """
    if cx.get("verdict") == "none" and jm.get("verdict") == "none":
        return "healthy", None, None, "both doctors report no fault"

    if cx.get("action") is None or jm.get("action") is None:
        # Codex, armed, speaking into a silence. The one relaxation the authority buys.
        if authority == "codex" and cx.get("action") is not None:
            silent = "reports no fault" if jm.get("verdict") == "none" else "could not be parsed"
            return _gate(cx["action"], cx.get("target"),
                         "codex has authority and jem %s: %s" % (silent, cx.get("reason") or ""),
                         how="codex's authorised")
        # A doctor whose reply could not be READ. Distinct from one that read it and saw nothing.
        mute = [w for w, d in (("codex", cx), ("jem", jm))
                if d.get("action") is None and d.get("verdict") != "none"]
        if mute:
            return ("flag", None, None, "no parseable diagnosis from %s"
                    % ("both doctors" if len(mute) == 2 else mute[0]))
        # One doctor sees a fault, the other sees none. That is not agreement, so nothing runs-
        # and it is worth the owner's eye, because exactly one of them is wrong.
        (seer, sn), (bn,) = ((jm, "jem"), ("codex",)) if cx.get("action") is None \
            else ((cx, "codex"), ("jem",))
        return ("flag", None, None,
                "the doctors disagree: %s reports no fault, %s says %s/%s"
                % (bn, sn, seer["action"], seer.get("target")))
    if cx["action"] != jm["action"] or _norm(cx.get("target")) != _norm(jm.get("target")):
        return ("flag", None, None,
                "the doctors disagree: codex says %s/%s, jem says %s/%s"
                % (cx["action"], cx.get("target"), jm["action"], jm.get("target")))
    return _gate(cx["action"], cx.get("target"),
                 cx.get("reason") or jm.get("reason") or "")


# ============================================================================= 4. the fan-out
def _default_fan(bundle, timeout=900):
    """Both analysts, in parallel, blind to each other. The bundle rides in a --context FILE,
    never in --brief: the log tail would blow the Windows command-line limit, and coop already
    hands a context path straight to both backends."""
    tmp = Path(tempfile.mkdtemp(prefix="baxter_doctor_ai_")) / "bundle.json"
    tmp.write_text(json.dumps(bundle, indent=1, ensure_ascii=False), encoding="utf-8")
    cmd = [PY, str(UTILS / "baxter_coop.py"), "--json", "--brief", BRIEF,
           "--context", str(tmp), "--timeout", str(timeout)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout * 2 + 120)
    except Exception as e:
        return {"codex": "(baxter_coop.py FAILED: %r)" % (e,), "jem": "(baxter_coop.py FAILED: %r)" % (e,)}
    try:
        obj = json.loads((r.stdout or "").strip())
        return {"codex": obj.get("codex", ""), "jem": obj.get("jem", "")}
    except Exception as e:
        why = ((r.stderr or "").strip() or repr(e))[:300]
        return {"codex": "(baxter_coop.py FAILED: %s)" % why, "jem": "(baxter_coop.py FAILED: %s)" % why}


# ============================================================================ 5. the cooldown
def _cd_load(path):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cd_stamp(path, key):
    d = _cd_load(path)
    d[key] = time.time()
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception:
        pass


def _cd_blocked(path, key, cooldown_sec):
    last = _cd_load(path).get(key)
    return isinstance(last, (int, float)) and (time.time() - last) < cooldown_sec


# ============================================================================ 6. the executor
def restart_plan(target):
    """What a restart WOULD do. Separated from doing it so the mapping can be tested without
    reaping the owner's fleet- the sealed exam injects its own executor and never drives this path,
    which is exactly why a typo'd component name would otherwise ship invisible."""
    name = _norm(target)
    if _is_protected(name):
        raise Refused("%r is protected- the coop guardian owns it" % target)
    spec = COMPONENTS.get(name)
    if not spec:
        raise Refused("unknown component %r (known: %s)" % (target, ", ".join(sorted(COMPONENTS))))
    return {"component": name, "supervisor": spec["supervisor"],
            "name_re": spec["name"], "cmd_re": spec["cmd"]}


def _wmi_census():
    """[(pid, name, cmdline)] for every process carrying a command line."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -EA SilentlyContinue | ForEach-Object "
         "{ \"$($_.ProcessId)|$($_.Name)|$($_.CommandLine)\" }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, creationflags=NO_WIN).stdout or ""
    rows = []
    for line in out.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[2]:
            rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def _taskkill(pid):
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                   timeout=20, creationflags=NO_WIN)


def match_pids(plan, rows):
    """Our own PID is never a casualty- the reaper must not be able to kill its caller
    (baxter_doctor.ps1 learned that on 9th July, when it decapitated the fleet)."""
    me = os.getpid()
    return [pid for pid, name, cmd in rows
            if pid != me and re.search(plan["name_re"], name, re.I)
            and re.search(plan["cmd_re"], cmd, re.I)]


def restart_component(target, census=None, killer=None):
    """KILL the component and let its own supervisor revive it. We never relaunch: spawning a
    replacement ourselves is how a doctor manufactures the duplicate it exists to reap."""
    plan = restart_plan(target)
    rows = (census or _wmi_census)()
    pids = match_pids(plan, rows)
    if not pids:
        raise Refused("%s is not running- a restart would be a launch, and %s owns that"
                      % (plan["component"], plan["supervisor"]))
    for pid in pids:
        (killer or _taskkill)(pid)
    return {"killed": pids, "revived_by": plan["supervisor"]}


def clear_stale_lock(target, vault=VAULT):
    """Unlink one abandoned lock, re-verified AT UNLINK TIME.

    The bundle may be fifteen minutes old by the time both doctors have answered- coop's
    timeout is 900s- and a lock abandoned then may be freshly held now. Every condition is
    therefore re-checked here, against the file as it is at this instant, and any doubt
    (unreadable, unknown owner that might be alive, fresh mtime) refuses.
    """
    name = os.path.basename(str(target or "").strip())
    if not name or name != str(target).strip() or not name.endswith(".lock"):
        raise Refused("%r is not a bare .lock filename in the vault" % (target,))
    p = Path(vault) / name
    if not p.exists():
        raise Refused("%s is already gone" % name)
    age = time.time() - p.stat().st_mtime
    if age <= LOCK_STALE_SEC:
        raise Refused("%s was touched %ds ago- a live holder, not an abandoned lock" % (name, age))
    owner = _lock_owner(p.read_bytes().decode("utf-8", "replace"))
    alive = pid_alive(owner) if owner else None
    if alive is True:
        raise Refused("%s is held by PID %s, which is alive" % (name, owner))
    p.unlink()
    return {"cleared": name, "age_sec": int(age), "owner_pid": owner}


def reap_duplicate(target, bundle, runner=None):
    """Trim a doubled singleton back to one, via the fleet's own reaper.

    `-Reap -DupesOnly` is the safe pair: -Reap is the switch that acts at all, -DupesOnly is
    the seatbelt that only ever touches a row holding MORE THAN ONE instance and always keeps
    one, so it cannot take a component to zero. (The plan of attack said '-DupesOnly, never
    -Reap'; read against baxter_doctor.ps1 that reaps nothing at all- `Invoke-Reap` is gated
    on -Reap. Named here because a silent deviation is a lie.)
    """
    comp = _norm(target)
    if _is_protected(comp):
        raise Refused("%r is protected" % target)
    listed = {d["component"] for d in (bundle or {}).get("duplicates", [])}
    if comp not in listed:
        raise Refused("the bundle shows no duplicate %r (it shows: %s)"
                      % (target, ", ".join(sorted(listed)) or "none"))
    run = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True,
                                                encoding="utf-8", errors="replace",
                                                timeout=120, creationflags=NO_WIN))
    r = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(UTILS / "baxter_doctor.ps1"), "-Reap", "-DupesOnly", "-SelfPid", str(os.getpid())])
    reaped = [ln.split("\t") for ln in (r.stdout or "").splitlines() if ln.startswith("REAP\t")]
    return {"reaped": reaped}


def _bundle_lane(bundle, name):
    """The bundle's record for `name`, or None. A wedged lane first; then a CLOGGED board's.

    This is the whole of "the bundle called it ill". A journal named by neither section is a
    lane the evidence never accused, and the doctors do not get to kill it.
    """
    for r in (bundle or {}).get("wedged_lanes") or []:
        if isinstance(r, dict) and r.get("journal") == name:
            return dict(r, why="wedged")
    c = (bundle or {}).get("queue_clog") or {}
    if isinstance(c, dict) and c.get("clogged"):
        for r in c.get("lanes") or []:
            if isinstance(r, dict) and r.get("journal") == name:
                return dict(r, why="clogged")
    return None


def free_lane(target, bundle, vault=VAULT, sample_sec=FREE_LANE_SAMPLE_SEC,
              killer=None, tree_cpu=None, alive=None, jhash=None, sleep=None, tree=None):
    """Kill a wedged lane's PROCESS TREE so the pump reopens the lane. Nothing is deleted.

    The licence for whitelisting this is reversibility: the resume journal survives untouched,
    the lane reaper finds a corpse with no exit code, `baxter_verify.classify` calls that
    transient, and the build is retried on the same journal. A stuck build is un-stuck.

    Four refusals, every one re-checked HERE and not at bundle time, because the bundle may be
    900 seconds old (coop's fan-out timeout) and a lane flat then may be reasoning now:

      1. the target is not a bare `resume-*.json` name inside `.baxter_resume`;
      2. the bundle calls that journal neither wedged nor clogged;
      3. the pid is not alive- there is nothing to free;
      4. a life witness MOVED: the journal's content hash changed since the bundle, or the
         process TREE burned >= CPU_EPS of cpu over a fresh `sample_sec` sample.

    A cpu delta that went BACKWARDS is progress too, not a stall: the figure is a sum over a
    tree, and a child exiting shrinks it. Unknown cpu is never read as zero.
    """
    s = _stuck()
    tree_cpu = tree_cpu or s.tree_cpu_seconds
    alive = alive or s.pid_alive
    jhash = jhash or s.journal_hash
    sleep = sleep or time.sleep
    tree = tree or s.process_tree

    # 1. the name, before anything touches the disk.
    raw = str(target or "").strip()
    name = os.path.basename(raw)
    if not name or name != raw or not name.startswith("resume-") or not name.endswith(".json"):
        raise Refused("%r is not a bare resume-*.json journal name" % (target,))
    if ".failed." in name or ".parked." in name:
        raise Refused("%s is a retired journal, not a live lane" % name)
    d = Path(vault) / ".baxter_resume"
    p = d / name
    if p.resolve().parent != d.resolve():
        raise Refused("%s escapes .baxter_resume" % name)
    if not p.exists():
        raise Refused("%s is gone- there is no lane to free" % name)

    # 2. the bundle must have accused this exact journal.
    rec = _bundle_lane(bundle, name)
    if rec is None:
        raise Refused("the bundle calls %s neither wedged nor clogged" % name)

    # 3. a corpse needs no doctor.
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise Refused("the bundle carries no pid for %s" % name)
    if alive(pid) is not True:
        raise Refused("PID %s is not alive- nothing to free" % pid)

    # 4. the witnesses, sampled across the instant of the kill.
    before = tree_cpu(pid)
    if rec.get("jhash") and jhash(p) != rec["jhash"]:
        raise Refused("%s advanced since the bundle- its journal changed" % name)
    sleep(sample_sec)
    after = tree_cpu(pid)
    if before is None or after is None:
        raise Refused("cpu is unreadable for PID %s- unknown is not flat" % pid)
    delta = after - before
    if delta >= CPU_EPS or delta < 0:
        raise Refused("PID %s burned %.3fs of tree cpu in %ss- it is working, not wedged"
                      % (pid, delta, sample_sec))
    if rec.get("jhash") and jhash(p) != rec["jhash"]:
        raise Refused("%s advanced during the sample- its journal changed" % name)

    victims = tree(pid)
    for q in reversed(victims):                    # children first: an orphan reparents and lives
        (killer or _taskkill)(q)
    return {"freed": name, "why": rec["why"], "killed": victims, "cpu_delta": round(delta, 3)}


def _default_executor(action, target, bundle):
    """The ONLY code in this module allowed to touch the machine. Returns True if it acted."""
    if action == "restart_component":
        restart_component(target)
    elif action == "clear_stale_lock":
        clear_stale_lock(target)
    elif action == "reap_duplicate":
        reap_duplicate(target, bundle)
    elif action == "free_lane":
        free_lane(target, bundle)
    else:
        raise Refused("action %r is not executable" % action)   # unreachable via cross_check
    return True


# ============================================================================= 7. the record
def _default_flagger(reason, payload):
    """Write the full record to the vault, then ONE line to the owner. Never outward."""
    try:
        with FLAGS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    line = "🩺 The doctors flagged something, sir- %s. Nothing was executed; it's in the log." % reason
    try:
        subprocess.run([PY, str(UTILS / "baxter_say.py"), line], capture_output=True,
                       timeout=60, creationflags=NO_WIN)
    except Exception:
        pass


def _print_flagger(reason, payload):
    print("FLAG: %s" % reason)


def _audit(path, rec):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# =========================================================================== 8. the whole pass
def run_once(bundle, fan=None, executor=None, flagger=None, audit=AUDIT_PATH,
             cooldown_path=COOLDOWN_PATH, cooldown_sec=COOLDOWN_SEC, dry_run=False,
             authority=None):
    """One diagnose-decide-repair pass. Every branch lands one json line in the audit log,
    carrying BOTH doctors' raw reasoning verbatim- refusals and flags included, because the
    refusals are the evidence that the gate works ([[the guard keeps its own log]])."""
    fan = fan or _default_fan
    executor = executor or _default_executor
    flagger = flagger or _default_flagger

    dg = fan(bundle) or {}
    codex_raw, jem_raw = dg.get("codex", ""), dg.get("jem", "")
    decision, action, target, reason = cross_check(parse_diagnosis(codex_raw),
                                                   parse_diagnosis(jem_raw),
                                                   authority=authority)
    executed = False

    if decision == "execute":
        key = "%s|%s" % (action, _norm(target))
        if _cd_blocked(cooldown_path, key, cooldown_sec):
            decision, reason = "cooldown", "%s on %s is within its %ds cooldown" % (
                action, target, cooldown_sec)
        elif dry_run:
            reason = "dry run- would have executed %s on %s" % (action, target)
        else:
            # Stamped BEFORE the executor: a repair that crashes mid-flight must not be
            # retried on the next scheduled pass. Flapping is the failure mode, not one miss.
            _cd_stamp(cooldown_path, key)
            try:
                executed = bool(executor(action, target, bundle))
            except Refused as e:
                decision, reason = "flag", "the executor refused: %s" % e
            except Exception as e:
                decision, reason = "flag", "the repair failed: %r" % (e,)

    rec = {"ts": _now_iso(), "decision": decision, "executed": executed, "action": action,
           "target": target, "reason": reason, "dry_run": bool(dry_run),
           "authority": authority, "codex": codex_raw, "jem": jem_raw}
    _audit(audit, rec)
    if decision == "flag":
        flagger(reason, rec)
    return rec


def main():
    ap = argparse.ArgumentParser(description="Codex + Jem diagnose Baxter.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--bundle", action="store_true", help="print the evidence as json; no LLM, no repair")
    g.add_argument("--dry-run", action="store_true", help="diagnose, decide, audit- execute nothing")
    g.add_argument("--once", action="store_true", help="a real diagnose-and-repair pass")
    ap.add_argument("--vault", default=str(VAULT))
    a = ap.parse_args()

    b = collect_bundle(Path(a.vault))
    if a.bundle:
        print(json.dumps(b, indent=1, ensure_ascii=False))
        return 0
    rec = run_once(b, dry_run=a.dry_run,
                   authority=armed_authority(Path(a.vault)),
                   flagger=_print_flagger if a.dry_run else None)
    print(json.dumps(rec, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
