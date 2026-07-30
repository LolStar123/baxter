r"""baxter_stuck_doctor - a lane that has stopped THINKING, not merely a lane that is slow.

the owner, 8th July 21:38: "when any Baxter task/build runs past a 15-min threshold, auto-summon
the doctor to health-check that instance- if healthy leave it, if wedged diagnose+repair."

Elapsed time is the TRIGGER TO LOOK, never the finding. A build that has run twenty minutes
is a build, not a casualty. So this probe answers one question about each long-running lane:
has it done any WORK since we last looked? Two independent witnesses say yes-

  * CPU TIME OVER THE LANE'S PROCESS TREE, via GetProcessTimes. A lane that is reasoning,
    editing or shelling out burns cpu; a lane deadlocked on a mutex burns none. It must be the
    TREE: the journal's pid is the `--resume-worker` python process, and it sits blocked in
    `subprocess.run` for the whole build. Measured on nine live lanes, 10th July: self-cpu
    delta 0.000s over 30 seconds on every one of them, tree delta 0.3-1.1s. A self-cpu
    watchdog would have doctored nine healthy builds.
  * THE JOURNAL'S CONTENT HASH. A lane rewrites its own resume journal as it progresses.

...and three things are deliberately NOT witnesses:

  * mtime, on the journal or anything else. `os.utime` runs on the heartbeat's DAEMON THREAD,
    so a fully wedged worker keeps stamping a fresh mtime forever. So does `pid_alive`. A
    watchdog gated on either is a watchdog that never fires ([[long-lived-process-staleness]]).
  * A SHARED artefact- `.baxter.log`'s size, the vault's mtime. Ten lanes and the watcher all
    write it, so every lane reads healthy while any one of them lives.
  * cpu == 0. `cpu_seconds()` returns None when OpenProcess is refused (an elevated child, a
    WOW64 quirk). None is UNKNOWN, and unknown is treated as PROGRESS: the cost of a missed
    diagnosis is a lane sitting idle, the cost of a false one is an LLM fan-out and a repair
    aimed at a healthy build. We fail towards leaving him alone.

THE TWO CLOCKS. `THRESHOLD` (15 min) decides who is old enough to be watched at all.
`SAMPLE_MIN` (10 min) is how long a watched lane must stay flat, against a fixed ANCHOR, before
it is called wedged. A real claude lane blocked on a slow network call burns near-zero cpu, so
the sample window- not the elapsed time- is what separates it from a corpse-with-a-pulse. The
anchor moves forward on every sign of life; it stands still while the lane does.

EDGE-TRIGGERED, off PERSISTED per-lane state (never off the audit log). The doctor is summoned
on the watching->wedged transition and NOT AGAIN while the lane stays wedged; the recovery line
posts once on wedged->healthy. A steady-state wedged lane is silent. State keyed on the journal
name lives in `.baxter_stuck_doctor.json`; every transition is appended to
`.baxter_stuck_doctor.jsonl`.

The doctor is `baxter_doctor_ai.py --once`- two blind LLM diagnoses that must agree before
anything is repaired. It is spawned DETACHED, never called inline: its fan-out has a 900s
timeout and the triage beat is single-threaded, so an inline call would freeze the watcher, the
reaper and the pump for fifteen minutes to look at one stuck lane.

A DEAD lane is not our business. `verdict` returns 'gone' and stops- the lane reaper owns
corpses, and doctoring one would "repair" a build that is already being classified.

THE PROBE IS A CHILD, AND THE CHILD IS ON A CLOCK (10th July). This module is no longer
imported into the triage beat. `baxter_triage.stuck_doctor_tick` runs it as a SUBPROCESS behind
`subprocess.run(timeout=)`, reads its verdicts off `--json`, and kills it if it overruns. The
reason is arithmetic, not taste: a python thread spinning inside a ctypes call cannot be
interrupted from python- no signal, no timeout, no `KeyboardInterrupt` reaches it- so an
in-process watchdog on this probe is a watchdog that can never fire. A child can be killed.

And before it is killed it incriminates itself. `main()` arms
`faulthandler.dump_traceback_later(DEADLINE, exit=True)` before the pass and cancels it after,
so a wedged probe writes every thread's stack to `.baxter_spin_dump.txt` and aborts. Verified
10th July against a deliberate `while True: kernel32.GetTickCount()` loop: faulthandler's
watchdog is a native thread, it does not need the GIL, and it dumped the exact frame and line
at 3.0s. That file is the evidence the 10th-July spin never left behind.

WHAT THE 10th-JULY SPIN ACTUALLY PROVED, AND WHAT IT DID NOT. A bare `python baxter_triage.py`
child (pid 13668, spawned 00:37:44) burned 28,995s of cpu in 29,257s of wall- one core pinned
at 99% for eight hours- while `baxter_watch.ps1` blocked in `Invoke-TriageWaitLoop` waiting for
it. No triage cycle completed: no filing, no briefs, no queue pump. That much is measured.

`baxter_watch.ps1`'s comment goes on to name THIS FILE's process-tree walk as the culprit. That
attribution is UNPROVEN and this docstring will not repeat it as fact. It rests on process
accounting, never on a stack: nobody attached a debugger before the process was killed. Against
the live fleet on 10th July the accused walk measured 6.4ms per snapshot over 456 processes
(`_children_map` x50 in 0.318s), `tree_cpu_seconds` 5.6ms, and a full `pass_once` over 7 live
lanes 0.064s. `process_tree` already caps at 512 with a `seen` cycle-guard, and `Process32Next`
terminates at the end of the snapshot. The spin did not reproduce.

So the bounds below are defence, not repair. `_children_map` now caps its walk at `WALK_CAP`
and closes the snapshot handle in a `finally`; `cpu_seconds` closes its process handle in a
`finally` (a raise between OpenProcess and CloseHandle leaked one handle per pass, every pass);
`pass_once` takes a wall-clock `deadline` and returns the lanes it managed. None of these is
known to have been the bug. The next time it happens there will be a stack in
`.baxter_spin_dump.txt`, and whoever reads it can finally name the frame.

CLI:
    python baxter_stuck_doctor.py --dry-run   # probe, audit, stamp. No doctor, no Discord.
    python baxter_stuck_doctor.py --once      # a real pass; may summon and may post.
    python baxter_stuck_doctor.py --json      # verdicts as one JSON array on stdout
    python baxter_stuck_doctor.py --deadline N  # self-dump and abort after N seconds
    python baxter_stuck_doctor.py --selftest  # every branch, outward paths rigged to raise.
"""
import argparse
import ctypes
import faulthandler
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

VAULT = Path(r"C:\Users\you\Documents\Baxter")
UTILS = Path(__file__).resolve().parent

THRESHOLD = 900        # 15 min elapsed before a lane is watched at all (his number)
SAMPLE_MIN = 600       # ...and 10 min flat against a fixed anchor before it is wedged
CPU_EPS = 1.0          # seconds of cpu that count as "it did something"

# The Toolhelp snapshot is finite and `Process32Next` ends it, so this ceiling should never
# bite. It exists because on 10th July a walk in this file was ACCUSED of pinning a core for
# eight hours and nobody could prove otherwise. A bound that never fires costs one comparison
# per process; an unbounded walk that fires once costs a day of triage.
WALK_CAP = 8192

# The probe's own patience with itself. `main()` arms faulthandler at this, so a wedged pass
# dumps its stacks and aborts rather than pinning a core in silence. The parent
# (baxter_triage.stuck_doctor_tick) allows a grace margin on top and then kills.
DEADLINE = 120.0

STATE_NAME = ".baxter_stuck_doctor.json"
AUDIT_NAME = ".baxter_stuck_doctor.jsonl"
CASE_NAME = ".baxter_stuck_doctor_case.json"
SPIN_DUMP_NAME = ".baxter_spin_dump.txt"

STATE = VAULT / STATE_NAME
AUDIT = VAULT / AUDIT_NAME
CASE = VAULT / CASE_NAME
SPIN_DUMP = VAULT / SPIN_DUMP_NAME

_NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_SILENT = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


# ------------------------------------------------------------------ the evidence layer

class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


def cpu_seconds(pid):
    """Kernel+user cpu seconds burned by `pid`, or None when we cannot know.

    None is not zero. OpenProcess is refused for an elevated child and for a process on the
    far side of a WOW64 boundary; reading that refusal as "zero cpu" would mark every such
    lane flat and doctor it the moment SAMPLE_MIN lapsed. The caller treats None as progress.

    THE HANDLE CLOSES IN A `finally`. It used to close on the straight-line path only, so any
    raise between OpenProcess and CloseHandle leaked a kernel handle- and this runs against
    every pid in every lane's tree, every pass, forever.
    """
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p       # a HANDLE is 64-bit; the default c_int
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]  # truncates it
        h = k.OpenProcess(0x1000, False, pid)         # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            creation, exit_, kern, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
            ok = k.GetProcessTimes(ctypes.c_void_p(h), ctypes.byref(creation), ctypes.byref(exit_),
                                   ctypes.byref(kern), ctypes.byref(user))
            if not ok:
                return None
            secs = lambda ft: ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7
            return secs(kern) + secs(user)
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        return None


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_char * 260)]


def _children_map(cap=WALK_CAP):
    """{ppid: [pid, ...]} from one Toolhelp snapshot. {} when the snapshot fails.

    BOUNDED AND CLOSED. `Process32Next` walks a finite snapshot, so `cap` should never bite-
    456 processes on the live box. It bites anyway, because the only reading of the 10th-July
    spin that implicates this file is a `Process32Next` that never says no, and eight hours of
    dark triage is too dear a price for trusting an API to terminate. On the ceiling we return
    the partial map: a lane whose tree we undercount reads as LOW cpu, and low is not flat-
    `verdict` needs a delta under CPU_EPS to call anything wedged, and an undercount that is
    stable across two passes was going to read flat regardless.

    The snapshot handle closes in a `finally`; before, a raise mid-walk leaked it.
    """
    if os.name != "nt":
        return {}
    try:
        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snap = k.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return {}
        h = ctypes.c_void_p(snap)
        try:
            e = _PROCESSENTRY32()
            e.dwSize = ctypes.sizeof(_PROCESSENTRY32)
            out = {}
            n = 0
            ok = k.Process32First(h, ctypes.byref(e))
            while ok and n < cap:
                out.setdefault(int(e.th32ParentProcessID), []).append(int(e.th32ProcessID))
                n += 1
                ok = k.Process32Next(h, ctypes.byref(e))
            return out
        finally:
            k.CloseHandle(h)
    except Exception:
        return {}


def process_tree(pid, kids=None, cap=512):
    """`pid` and every descendant. Cycle- and blow-up-proof: Windows recycles pids, so a
    stale parent link in the snapshot can point a child back at an ancestor."""
    kids = _children_map() if kids is None else kids
    seen, stack, out = {pid}, [pid], [pid]
    while stack and len(out) < cap:
        for c in kids.get(stack.pop(), ()):
            if c not in seen:
                seen.add(c)
                out.append(c)
                stack.append(c)
    return out


def tree_cpu_seconds(pid, kids=None):
    """Cpu burned by the lane's WHOLE tree- the worker plus every descendant.

    THE DEFAULT, and it must be. The journal's pid is the `--resume-worker` python process,
    which spends the entire build blocked in `subprocess.run` waiting on its `claude` child:
    measured on 10th July across nine live lanes, every single one burned 0.000s of its OWN
    cpu in 30 seconds while its tree burned 0.3-1.1s. A self-cpu watchdog would therefore
    have found all ten lanes flat and summoned the doctor to nine healthy builds.

    The sum is NOT monotonic- a child exiting shrinks it (lane 6 fell 2.469s mid-sample). The
    caller reads a NEGATIVE delta as progress, because a tree that lost a process did work.
    """
    kids = _children_map() if kids is None else kids
    vals = [cpu_seconds(p) for p in process_tree(pid, kids)]
    known = [v for v in vals if v is not None]
    if not known:
        return None                                        # nothing readable: UNKNOWN, not zero
    return sum(known)


def pid_alive(pid):
    """True | False | None. ERROR_ACCESS_DENIED means it EXISTS and is not ours to open."""
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            err = k.GetLastError()
            if err == 5:
                return True                            # access denied: alive, just not ours
            if err == 87:
                return False                           # the only clean "no such pid"
            return None
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k.CloseHandle(ctypes.c_void_p(h))
        if not ok:
            return None
        return code.value == 259                       # STILL_ACTIVE
    except Exception:
        return None


def elapsed(entry, now):
    """Seconds since the lane started, or None when its journal never said."""
    raw = entry.get("started_at")
    if not raw:
        return None
    try:
        return now - datetime.fromisoformat(str(raw)).timestamp()
    except Exception:
        return None


def journal_hash(path):
    """Content hash of the lane's own journal. Content, never mtime- the heartbeat thread
    stamps mtime from outside the wedged worker's stalled main loop."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _resume_journals(vault):
    d = Path(vault) / ".baxter_resume"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("resume-*.json")):
        n = p.name
        if ".failed." in n or ".parked." in n:
            continue                                   # already classified; not a live lane
        out.append(p)
    return out


# ------------------------------------------------------------------ the anchor state machine

def verdict(prev, el, cpu_now, jhash, now):
    """(verdict, anchor) for one lane. Pure- the selftest drives every branch.

    `prev` is the persisted per-lane record ({} on first sight). The order matters:

      1. dead            -> 'gone'.     The reaper owns corpses. We never repair one.
      2. young           -> 'watching'. Under 15 min is a build, not a symptom.
      3. no anchor       -> 'watching'. First sight of an old lane: plant the anchor, look again.
      4. progress        -> 'healthy'.  cpu moved, or the journal changed, or cpu is UNKNOWN.
      5. flat >= sample  -> 'wedged'.
      6. flat < sample   -> 'watching'. Not yet damning.

    A cpu delta that went BACKWARDS is progress, not a stall: the figure is a sum over the
    lane's process tree, and a child that finished and exited takes its ticks with it.
    """
    anchor = {"anchor_t": prev.get("anchor_t"), "anchor_cpu": prev.get("anchor_cpu"),
              "anchor_jhash": prev.get("anchor_jhash")}
    fresh = {"anchor_t": now, "anchor_cpu": cpu_now, "anchor_jhash": jhash}

    if el is None or el < THRESHOLD:
        return "watching", anchor
    if anchor["anchor_t"] is None:
        return "watching", fresh

    if cpu_now is None or anchor["anchor_cpu"] is None:
        moved_cpu = True                                   # UNKNOWN is never evidence of a stall
    else:
        delta = cpu_now - anchor["anchor_cpu"]
        moved_cpu = delta >= CPU_EPS or delta < 0.0        # backwards = a child exited = work
    moved_journal = jhash != anchor["anchor_jhash"]
    if moved_cpu or moved_journal:
        return "healthy", fresh

    if (now - anchor["anchor_t"]) >= SAMPLE_MIN:
        return "wedged", anchor                        # the anchor STAYS: it dates the stall
    return "watching", anchor


# ------------------------------------------------------------------ state + audit

def _load_state(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"last_pass": 0.0, "lanes": {}}
    if not isinstance(raw, dict):
        return {"last_pass": 0.0, "lanes": {}}
    raw.setdefault("last_pass", 0.0)
    if not isinstance(raw.get("lanes"), dict):
        raw["lanes"] = {}
    return raw


def _save_state(path, st):
    """Atomic. The edge-trigger lives here: a torn write re-fires the doctor every 60s."""
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, p)


def _audit(path, rec):
    try:
        with Path(path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ the outward paths

def _pythonw():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w) if w.exists() else str(exe)


def _default_doctor(rec, vault=VAULT):
    """Write the case file, spawn `baxter_doctor_ai.py --once` DETACHED, return immediately.

    NEVER `doctor_ai.run_once(...)` inline. Its fan-out waits up to 900s on two LLMs, and
    triage's run loop is single-threaded: one wedged lane would freeze the watcher, the lane
    reaper and the queue pump for a quarter of an hour.
    """
    try:
        case = {k: rec.get(k) for k in ("journal", "lane", "pid", "elapsed", "anchor_age", "cpu")}
        case["summoned_at"] = datetime.now().isoformat(timespec="seconds")
        (Path(vault) / CASE_NAME).write_text(json.dumps(case, indent=1), encoding="utf-8")
    except Exception:
        pass
    try:
        subprocess.Popen([_pythonw(), str(UTILS / "baxter_doctor_ai.py"), "--once"],
                         cwd=str(vault), close_fds=True,
                         creationflags=_NO_WIN | _DETACHED, **_SILENT)
        return True
    except Exception:
        return False


def _default_poster(msg):
    """Through health_monitor.post- the existing, dedup-aware channel plumbing. No new
    outward path is opened for this probe."""
    try:
        import health_monitor as _hm
        return _hm.post(msg, channel="general", mention=False)
    except Exception:
        return False


def _lane_label(lane):
    try:
        return int(lane) + 1                           # lanes read as 1..10, never lane 0
    except (TypeError, ValueError):
        return lane if lane not in (None, "") else "?"


def _arm_spin_dump(deadline, vault=VAULT):
    """Arm faulthandler to dump every thread's stack and abort if the pass overruns `deadline`.

    This is the ONLY mechanism that gets a stack out of a wedged probe. A spin inside a ctypes
    call holds the GIL and ignores signals, so no python-level timer, thread or `except` can
    reach it. faulthandler's watchdog is a native thread that writes to a raw fd and calls
    `_exit`- it does not need the GIL and it does not need the interpreter to be well.
    """
    if not deadline or deadline <= 0:
        return None
    try:
        fh = open(Path(vault) / SPIN_DUMP_NAME, "w", encoding="utf-8")
        faulthandler.dump_traceback_later(float(deadline), repeat=False, exit=True, file=fh)
        return fh
    except Exception:
        return None


def _disarm_spin_dump(fh):
    """Cancel the timer and bin the empty file. A clean pass leaves no dump behind, so the
    presence of `.baxter_spin_dump.txt` is itself the finding."""
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    if fh is None:
        return
    try:
        name = fh.name
        fh.close()
        p = Path(name)
        if p.exists() and p.stat().st_size == 0:
            p.unlink()
    except Exception:
        pass


# ------------------------------------------------------------------ the pass

def pass_once(now=None, vault=VAULT, cpu=tree_cpu_seconds, alive=pid_alive, doctor=None,
              poster=None, state_path=None, audit=None, dry_run=False, deadline=None):
    """Probe every live lane once. Returns one record per journal.

    Summons `doctor(rec)` ONLY on the watching->wedged edge, and posts ONLY on that edge and
    on wedged->healthy. Steady-state wedged is silent: the doctor is already looking.

    `deadline` (wall seconds, measured on the MONOTONIC clock- `now` is a synthetic timestamp
    the selftest drives the state machine with, and must never be mistaken for one) bounds the
    loop: on overrun we return the lanes we managed and persist their anchors. Partial evidence
    beats a probe that never returns. It is a second line only- a spin INSIDE a lane's ctypes
    walk never reaches the check, which is what `_arm_spin_dump` is for.
    """
    now = time.time() if now is None else now
    started = time.monotonic()
    vault = Path(vault)
    state_path = Path(state_path) if state_path else vault / STATE_NAME
    audit = Path(audit) if audit else vault / AUDIT_NAME
    doctor = _default_doctor if doctor is None else doctor
    poster = _default_poster if poster is None else poster

    st = _load_state(state_path)
    lanes = st["lanes"]
    out = []
    seen = set()
    truncated = False

    for path in _resume_journals(vault):
        if deadline and (time.monotonic() - started) >= deadline:
            truncated = True
            break                                      # out of time: keep what we have
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue                                   # unreadable journal: the reaper's problem
        if not isinstance(entry, dict) or entry.get("held") or not entry.get("pid"):
            continue                                   # a hold, or a build that never had a body
        name = path.name
        seen.add(name)
        pid = entry.get("pid")
        prev = lanes.get(name, {}) if isinstance(lanes.get(name), dict) else {}
        was = prev.get("verdict")

        if alive(pid) is False:                        # None is UNKNOWN, and unknown is not dead
            v, anchor = "gone", {}
            cpu_now = None
            el = elapsed(entry, now)
        else:
            el = elapsed(entry, now)
            cpu_now = cpu(pid)
            v, anchor = verdict(prev, el, cpu_now, journal_hash(path), now)

        anchor_age = (now - anchor["anchor_t"]) if anchor.get("anchor_t") else None
        rec = {"journal": name, "lane": entry.get("lane"), "pid": pid,
               "elapsed": None if el is None else round(el, 1),
               "cpu": cpu_now, "anchor_age": None if anchor_age is None else round(anchor_age, 1),
               "verdict": v, "prev": was, "summoned": False}

        if v == "wedged" and was != "wedged":
            rec["summoned"] = bool(doctor(rec)) if not dry_run else False
            if not dry_run:
                mins = int((anchor_age or 0) // 60)
                poster(f"Sir, lane {_lane_label(entry.get('lane'))} has made no progress "
                       f"in {mins}m- the doctor is looking at it.")
        elif v == "healthy" and was == "wedged" and not dry_run:
            poster(f"Lane {_lane_label(entry.get('lane'))} is moving again, sir.")

        if v != was and v != "watching":
            _audit(audit, dict(rec, at=now, dry_run=dry_run))

        if not dry_run:
            lanes[name] = {"verdict": v, **anchor}
        out.append(rec)

    if not dry_run and not truncated:
        # ONLY on a complete walk. A deadline break leaves live lanes unvisited, and pruning on
        # `not in seen` would then throw away their anchors- resetting the very sample window
        # that dates a stall, every pass, so a truly wedged lane could never age into 'wedged'.
        for gone in [k for k in lanes if k not in seen]:
            lanes.pop(gone)                            # the lane retired; forget its anchor
    st["last_pass"] = now
    st["lanes"] = lanes
    try:
        _save_state(state_path, st)
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ selftest

def _selftest_tree_cpu():
    """The real GetProcessTimes/Toolhelp path, against a real child. No injection.

    This is the assertion that would have caught the 10th-July bug before it shipped: a parent
    that merely WAITS on a working child burns no cpu of its own, exactly as every resume-worker
    does. Nothing below is stubbed- if the ctypes calls are wrong, this goes red.
    """
    if os.name != "nt":
        return
    me = os.getpid()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time\nt=time.time()\nx=0\nwhile time.time()-t<3.0: x+=1"],
        creationflags=_NO_WIN, **_SILENT)
    try:
        self0, tree0 = cpu_seconds(me), tree_cpu_seconds(me)
        assert self0 is not None and tree0 is not None, "the live cpu probe read nothing"
        time.sleep(2.5)                                    # we sleep; the child spins
        self1, tree1 = cpu_seconds(me), tree_cpu_seconds(me)
        assert child.pid in process_tree(me), "the process tree missed a real child"
        assert (self1 - self0) < CPU_EPS, \
            f"a sleeping parent burned {self1 - self0:.2f}s of cpu- the fixture is not idle"
        assert (tree1 - tree0) >= CPU_EPS, (
            f"tree cpu moved only {tree1 - tree0:.2f}s while a child spun for 2.5s- "
            "a lane blocked on its claude child would read as WEDGED")
    finally:
        try:
            child.kill()
            child.wait(timeout=10)
        except Exception:
            pass
    # A tree nobody can read is UNKNOWN. Returning 0.0 here would mark every lane flat.
    assert cpu_seconds(0x7FFFFFF0) is None, "a nonexistent pid must read UNKNOWN"
    assert tree_cpu_seconds(0x7FFFFFF0) is None, "an unreadable tree must be UNKNOWN, never 0.0"
    print("tree_cpu_seconds: a waiting parent reads flat, its tree does not")


class _BadFiletime:
    """Instantiates fine; `ctypes.byref` refuses it. Stands in for a GetProcessTimes call that
    raises AFTER OpenProcess handed us a live handle- the exact shape of the leak."""


def _selftest_handle_hygiene():
    """cpu_seconds must close its process handle on the RAISING path, not just the happy one.

    Mutation-proof by construction: delete the `finally` in cpu_seconds and this goes red with
    ~300 leaked handles. A leak here is not cosmetic- cpu_seconds runs once per pid per lane
    per tree, every 60s, in a process that used to be the long-lived triage beat.
    """
    if os.name != "nt":
        return
    k = ctypes.windll.kernel32

    def handles():
        c = ctypes.c_ulong()
        if not k.GetProcessHandleCount(ctypes.c_void_p(k.GetCurrentProcess()), ctypes.byref(c)):
            return None
        return c.value

    me = os.getpid()
    for _ in range(20):
        cpu_seconds(me)                                    # warm any lazy ctypes machinery
    if handles() is None:
        return
    h0 = handles()
    for _ in range(300):
        assert cpu_seconds(me) is not None, "the live cpu probe stopped reading its own process"
    grew = handles() - h0
    assert grew <= 8, f"cpu_seconds leaked {grew} handles across 300 clean calls"

    saved = globals()["_FILETIME"]
    globals()["_FILETIME"] = _BadFiletime
    try:
        assert cpu_seconds(me) is None, "a raising GetProcessTimes must read UNKNOWN, not 0.0"
        h1 = handles()
        for _ in range(300):
            cpu_seconds(me)
        grew = handles() - h1
        assert grew <= 8, f"cpu_seconds leaked {grew} handles across 300 RAISING calls"
    finally:
        globals()["_FILETIME"] = saved

    # ...and the snapshot handle, on the same principle.
    h2 = handles()
    for _ in range(200):
        _children_map()
    grew = handles() - h2
    assert grew <= 8, f"_children_map leaked {grew} snapshot handles across 200 calls"
    print("handle hygiene: cpu_seconds and _children_map close on the raising path")


def _selftest_walk_cap():
    """The Process32Next walk is bounded. This is the ceiling the 10th-July spin was ALLEGED to
    have needed; unproven, but a bound that never fires is cheap and a walk that never ends is
    eight hours of dark triage."""
    if os.name != "nt":
        return
    total = sum(len(v) for v in _children_map().values())
    assert total > 20, f"the live snapshot read only {total} processes- the fixture is broken"
    for cap in (1, 3, 17):
        got = sum(len(v) for v in _children_map(cap=cap).values())
        assert got == cap, f"cap={cap} walked {got} entries, not {cap}"
    # The default must NOT bite. Never assert snapshot equality here: two Toolhelp snapshots of a
    # live box never agree- this one spawns and reaps processes between them, and ten build lanes
    # make that constant. Asserting `== total` failed five runs in six. Assert the ceiling was
    # simply not reached, which is the only claim the code makes.
    got = sum(len(v) for v in _children_map(cap=WALK_CAP).values())
    assert 20 < got < WALK_CAP, f"the default cap truncated a live snapshot at {got}"
    print(f"_children_map: walk bounded (live snapshot ~ {total} processes)")


def _selftest_spin_dump():
    """faulthandler must get a stack out of a ctypes spin, and must NOT fire once cancelled.

    The first half is the whole reason this module can now be diagnosed. A python thread inside
    a ctypes call holds the GIL, so nothing at the python level can interrupt it- proven 10th
    July against `while True: kernel32.GetTickCount()`, which no signal or timer touched and
    faulthandler aborted at the deadline with the exact frame and line.
    """
    if os.name != "nt":
        return
    import tempfile
    d = Path(tempfile.mkdtemp())

    # armed, and it really fires: a child spins inside ctypes and is aborted with a stack.
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,ctypes,time\n"
         f"sys.path.insert(0, r'{UTILS}')\n"
         "import baxter_stuck_doctor as sd\n"
         f"fh = sd._arm_spin_dump(2.0, vault=r'{d}')\n"
         "k = ctypes.windll.kernel32\n"
         "t = time.time()\n"
         "while time.time() - t < 60: k.GetTickCount()\n"],
        creationflags=_NO_WIN, **_SILENT)
    try:
        rc = child.wait(timeout=45)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    assert rc != 0, "a wedged probe must abort, not exit clean"
    dump = (d / SPIN_DUMP_NAME).read_text(encoding="utf-8", errors="replace")
    assert "Timeout" in dump, f"the deadline passed without a dump:\n{dump[:400]}"
    assert "in <module>" in dump, f"the dump carried no python frame:\n{dump[:400]}"

    # cancelled, and it really stops: arm short, disarm, outlive it.
    fh = _arm_spin_dump(0.5, vault=d)
    assert fh is not None and (d / SPIN_DUMP_NAME).exists()
    _disarm_spin_dump(fh)
    assert not (d / SPIN_DUMP_NAME).exists(), "a clean pass must leave no dump behind"
    time.sleep(1.2)                                        # if the timer still lived, we are dead
    assert _arm_spin_dump(0, vault=d) is None, "deadline 0 must disarm the dump entirely"
    assert _arm_spin_dump(None, vault=d) is None
    print("spin dump: fires on a ctypes spin, cancels clean, leaves nothing behind")


def _selftest_json_and_deadline(tmp_root):
    """`--json` prints parseable records and nothing else; a real child proves the arm/cancel
    pair does not disturb a healthy pass. Driven as a SUBPROCESS- that is how triage calls it."""
    import tempfile
    d = Path(tempfile.mkdtemp(dir=str(tmp_root)))
    (d / ".baxter_resume").mkdir()
    (d / ".baxter_resume" / "resume-20260710-000000-000009-2.json").write_text(json.dumps(
        {"id": "j", "lane": 2, "pid": os.getpid(),
         "started_at": datetime.now().isoformat(timespec="seconds")}))

    p = subprocess.run([sys.executable, str(UTILS / "baxter_stuck_doctor.py"),
                        "--dry-run", "--json", "--vault", str(d), "--deadline", "30"],
                       capture_output=True, text=True, timeout=90, creationflags=_NO_WIN)
    assert p.returncode == 0, f"--json --dry-run exited {p.returncode}: {p.stderr[-400:]}"
    recs = json.loads(p.stdout)                            # nothing but json on stdout
    assert len(recs) == 1 and recs[0]["lane"] == 2, recs
    assert recs[0]["verdict"] == "watching", recs          # a young lane is never judged
    assert not (d / SPIN_DUMP_NAME).exists(), "a clean --json pass left a spin dump behind"

    # the wall-clock bound returns PARTIAL records rather than never returning.
    for n in range(3):
        (d / ".baxter_resume" / f"resume-20260710-000000-00001{n}-{n}.json").write_text(json.dumps(
            {"id": f"s{n}", "lane": n, "pid": os.getpid(),
             "started_at": datetime.now().isoformat(timespec="seconds")}))
    slow = lambda pid: (time.sleep(0.4), 1.0)[1]
    t = time.monotonic()
    recs = pass_once(vault=d, cpu=slow, alive=lambda p: True, dry_run=True,
                     doctor=lambda r: True, poster=lambda m: True, deadline=0.5)
    el = time.monotonic() - t
    assert 0 < len(recs) < 4, f"the deadline returned {len(recs)} of 4 lanes- it did not bound"
    assert el < 3.0, f"pass_once ran {el:.1f}s against a 0.5s deadline"

    # ...and a truncated walk must NOT forget the anchors of lanes it never reached.
    st = {"last_pass": 0.0, "lanes": {f"resume-20260710-000000-00001{n}-{n}.json":
                                      {"verdict": "watching", "anchor_t": 1.0} for n in range(3)}}
    _save_state(d / STATE_NAME, st)
    pass_once(vault=d, cpu=slow, alive=lambda p: True, doctor=lambda r: True,
              poster=lambda m: True, deadline=0.5)
    kept = _load_state(d / STATE_NAME)["lanes"]
    assert len(kept) >= 3, f"a truncated pass pruned live lanes' anchors: {kept}"
    print("--json / deadline: parseable records, bounded walk, anchors survive truncation")


def _selftest():
    import tempfile
    _selftest_tree_cpu()
    _selftest_handle_hygiene()
    _selftest_walk_cap()
    _selftest_spin_dump()
    tmp = Path(tempfile.mkdtemp())
    _selftest_json_and_deadline(tmp)
    (tmp / ".baxter_resume").mkdir()

    # A selftest that reaches an outward path has already spawned an LLM fan-out and posted a
    # fabricated alert into the owner's server. Rig all three to explode ([[selftests-stub-every-outward-path]]).
    import health_monitor as _hm
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("selftest reached an outward path"))
    saved = (subprocess.Popen, subprocess.run, _hm.post)
    subprocess.Popen, subprocess.run, _hm.post = boom, boom, boom
    try:
        T = 1_900_000_000.0

        def mk(n, ago, pid, **extra):
            p = tmp / ".baxter_resume" / f"resume-{n}.json"
            p.write_text(json.dumps(dict(
                {"id": n, "lane": 0, "pid": pid,
                 "started_at": datetime.fromtimestamp(T - ago).isoformat(timespec="seconds")},
                **extra)))
            return p

        # verdict(), branch by branch.
        assert verdict({}, 300, 5.0, "h", T)[0] == "watching", "a young lane is never judged"
        assert verdict({}, 2400, 5.0, "h", T)[0] == "watching", "first sight only plants the anchor"
        a = verdict({}, 2400, 5.0, "h", T)[1]
        assert a["anchor_t"] == T and a["anchor_cpu"] == 5.0
        anc = {"anchor_t": T, "anchor_cpu": 5.0, "anchor_jhash": "h"}
        assert verdict(anc, 3000, 5.0, "h", T + 60)[0] == "watching", "flat, but inside the sample"
        assert verdict(anc, 3000, 5.0, "h", T + SAMPLE_MIN)[0] == "wedged"
        assert verdict(anc, 3000, 6.5, "h", T + SAMPLE_MIN)[0] == "healthy", "cpu moved"
        assert verdict(anc, 3000, 5.0, "OTHER", T + SAMPLE_MIN)[0] == "healthy", "journal changed"
        assert verdict(anc, 3000, None, "h", T + SAMPLE_MIN)[0] == "healthy", \
            "cpu UNKNOWN must never read as flat"
        assert verdict(anc, 3000, 5.5, "h", T + SAMPLE_MIN)[0] == "wedged", \
            "a sub-epsilon twitch is not progress"
        assert verdict(anc, 3000, 2.0, "h", T + SAMPLE_MIN)[0] == "healthy", \
            "a tree that shrank lost a child- that is work, not a stall"
        w = verdict(anc, 3000, 5.0, "h", T + SAMPLE_MIN)[1]
        assert w["anchor_t"] == T, "the wedged anchor must not move- it dates the stall"

        # pass_once(): skips, edges, persistence.
        mk("20260710-000000-000001-1", 2400, 11)
        mk("20260710-000000-000002-2", 2400, 12, held=True)          # a hold, not a lane
        mk("20260710-000000-000003-3", 2400, None)                   # never had a body
        (tmp / ".baxter_resume" / "resume-x-4.failed.json").write_text("{}")
        cpu = {11: 5.0}
        calls, posts = [], []
        kw = dict(vault=tmp, cpu=lambda p: cpu[p], alive=lambda p: True,
                  doctor=lambda r: calls.append(r["journal"]) or True,
                  poster=lambda m: posts.append(m))

        live = "resume-20260710-000000-000001-1.json"
        r = pass_once(now=T, **kw)
        assert [x["journal"] for x in r] == [live], "held / bodiless / failed journals must be skipped"
        assert r[0]["verdict"] == "watching" and calls == [] and posts == []

        r = pass_once(now=T + SAMPLE_MIN + 5, **kw)
        assert r[0]["verdict"] == "wedged", r
        assert len(calls) == 1 and len(posts) == 1 and r[0]["summoned"] is True
        assert "no progress in 10m" in posts[0] and "lane 1" in posts[0], posts

        r = pass_once(now=T + SAMPLE_MIN + 65, **kw)
        assert r[0]["verdict"] == "wedged" and len(calls) == 1 and len(posts) == 1, \
            "steady-state wedged must neither re-summon nor re-post"

        cpu[11] += 99.0
        r = pass_once(now=T + SAMPLE_MIN + 125, **kw)
        assert r[0]["verdict"] == "healthy" and len(calls) == 1 and len(posts) == 2
        assert posts[1] == "Lane 1 is moving again, sir."

        # dry-run touches neither the doctor, Discord, nor the persisted edge.
        st_before = _load_state(tmp / STATE_NAME)["lanes"][live]
        r = pass_once(now=T + SAMPLE_MIN + 130, dry_run=True, **kw)
        assert len(calls) == 1 and len(posts) == 2, "dry-run reached an outward path"
        st_after = _load_state(tmp / STATE_NAME)
        assert st_after["lanes"][live] == st_before, "dry-run must not consume the edge"
        assert st_after["last_pass"] == T + SAMPLE_MIN + 130, "dry-run must still stamp"

        # a corpse is the reaper's, never the doctor's.
        r = pass_once(now=T + 9999, **dict(kw, alive=lambda p: False))
        assert r[0]["verdict"] == "gone" and len(calls) == 1, "a dead lane must never be doctored"

        # the retired lane's anchor is forgotten.
        for p in (tmp / ".baxter_resume").glob("resume-*"):
            p.unlink()
        pass_once(now=T + 10000, **kw)
        assert _load_state(tmp / STATE_NAME)["lanes"] == {}, "a retired lane left its anchor behind"

        aud = [json.loads(l) for l in (tmp / AUDIT_NAME).read_text().splitlines() if l.strip()]
        assert any(a["verdict"] == "wedged" and a["summoned"] for a in aud), aud
        assert any(a["verdict"] == "healthy" for a in aud), aud
        assert not any(a["verdict"] == "watching" for a in aud), "watching is not an event"
    finally:
        subprocess.Popen, subprocess.run, _hm.post = saved
    print("PASS baxter_stuck_doctor selftest")


def main():
    ap = argparse.ArgumentParser(description="Probe long-running lanes for real progress.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="a real pass; may summon and may post")
    g.add_argument("--dry-run", action="store_true", help="probe, audit, stamp. Nothing outward.")
    g.add_argument("--selftest", action="store_true")
    ap.add_argument("--vault", default=str(VAULT))
    ap.add_argument("--json", action="store_true",
                    help="print the pass records as one JSON array on stdout")
    ap.add_argument("--deadline", type=float, default=DEADLINE,
                    help="dump every thread's stack to %s and abort after N seconds (0 = off)"
                         % SPIN_DUMP_NAME)
    a = ap.parse_args()

    if a.selftest:
        _selftest()
        return

    # Armed BEFORE the pass and cancelled after it. The window covers every ctypes walk, which
    # is the only place a spin has ever been alleged.
    # The soft bound trips first, so an honestly slow pass returns partial records and exits 0;
    # faulthandler's hard bound is left for the case where the loop check is never reached.
    fh = _arm_spin_dump(a.deadline, vault=a.vault)
    try:
        recs = pass_once(vault=Path(a.vault), dry_run=a.dry_run,
                         deadline=(a.deadline * 0.75) if a.deadline else None)
    finally:
        _disarm_spin_dump(fh)

    if a.json:
        print(json.dumps(recs))                        # the only thing on stdout in this mode
        return
    for r in recs:
        print(f"lane {_lane_label(r['lane'])} {r['journal']}: {r['verdict']}"
              f" (elapsed={r['elapsed']} cpu={r['cpu']} anchor_age={r['anchor_age']})")
    if not recs:
        print("no live lanes")


if __name__ == "__main__":
    main()
