r"""baxter_coop_guardian - independent life-support for Codex + Jem.

THE ASYMMETRY THIS FIXES (root-caused 9th July). the owner saw Codex die with Baxter while
Jem lived on, and read it as "they're built differently". They are, but not where it
looked:

  * Codex was launched ONLY by `baxter_watch.ps1`'s outer loop. That loop blocks for
    minutes on the triage child ([[watcher-blocks-on-triage]]), and dies when the watcher
    dies. So Codex's uptime WAS Baxter's uptime- a supervised child of the very process
    that kept falling over. Nothing else on the box could restart it.
  * Jem was launched by NOTHING. The watcher says in as many words "Jem stays OFF- not
    launched here". The live Jem is an orphan hand-started on 8th July at 18:48, parented
    to a shell that has long since exited. It "survived Baxter's crash" because no code
    path can kill it- and by the same token, no code path can restart it either. Its
    survival was luck, not resilience.
  * Neither was monitored. `health_monitor.py` deliberately skipped both, on the (stale)
    belief that Coop v2 had decommissioned them as Discord bots.

So the fix is not to make Jem more like Codex, nor Codex more like Jem. It is to give
BOTH a supervisor that is not Baxter.

WHAT THIS IS. A scheduled task (`Baxter_CoopGuardian`, every 2 min), sibling of
`Baxter_HealthMonitor`, launched by Task Scheduler and NOT by the watcher, the guardian,
or any Claude session. It is the ONLY thing that starts or restarts either bot.

CRASH INDEPENDENCE, concretely:
  * Bots are spawned DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP with all three std
    handles on DEVNULL. No inherited handles, no console, no shared process group. A
    console control event (Ctrl-C/Ctrl-Break) aimed at any Baxter process cannot cascade
    into them, and closing the launcher cannot break their stdio.
    (DETACHED_PROCESS already means "no console at all", so CREATE_NO_WINDOW is redundant
    here- MSDN says it is ignored when combined with DETACHED_PROCESS. Don't add it back.)
  * This script exits in milliseconds. It holds no handle on the children it starts, so
    its own death, or Task Scheduler's, orphans nothing important.

LIVENESS IS THE HEARTBEAT, NOT THE PID. Each bot beats from inside its asyncio event
loop every ~30s. A wedged gateway loop keeps the PID alive while serving nothing- exactly
the "Baxter goes weird" failure mode the owner wants doctored- so a stale beat is treated as
death: kill the husk, relaunch. A bot mid-`codex exec` still beats (the call runs in a
worker thread), so a legitimate 15-minute run is never reaped.

Read-only sibling: health_monitor.py alerts the owner when a bot stays down across two of
these passes, i.e. when THIS script has already tried and failed. That inference is only
sound if the guardian is itself alive, so every pass stamps its own liveness (see main())
and health_monitor now checks THAT before blaming the bots.
"""
import json
import os
import subprocess
import sys
import time

from datetime import datetime
from pathlib import Path

# Import the stamper by path, not by luck: sys.path[0] is this directory only when the
# scheduler runs the script directly. A selftest that imports the guardian from elsewhere
# must get the same stamp() the live task uses, not an ImportError.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import baxter_hidden_run as hidden_run  # noqa: E402

VAULT = Path(r"C:\Users\you\Documents\Baxter")
UTILS = Path(r"C:\Users\you\Documents\Python Scripts\utils")
LOG = UTILS / "baxter_coop_guardian.log"

# discord.py lives in the 3.12 install, same interpreter the watcher used for these bots.
PY312 = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python/Python312/python.exe"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
NO_WIN = 0x08000000  # for our own short-lived helper calls only, never for the bots

# A beat is written every ~30s. 120s = four missed beats: long enough that a GC pause or a
# slow disk never triggers a restart, short enough that a wedged bot is back inside one
# scheduled pass.
STALE_SEC = 120

# This guardian is the SOLE supervisor of both bots. Nothing else may start, kill or restart
# them- `baxter_doctor_ai.PROTECTED` names codex, jem and coop_guardian for exactly that
# reason, so the two doctors can never repair their own life-support. Do not cross-wire them.
FLEET = {
    "codex": {"script": UTILS / "baxter_codex_bot.py", "beat": VAULT / ".baxter_codex_heartbeat.txt"},
    "jem": {"script": UTILS / "baxter_jemini_bot.py", "beat": VAULT / ".baxter_jem_heartbeat.txt"},
}


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _census():
    """[(pid, cmdline)] for every live python process.

    Matched on binary NAME plus command line, never a bare command-line substring: a
    substring census counts the very shell running it ([[process-census-match-name-not-cmdline]]).
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" -EA SilentlyContinue |"
             " ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=30, creationflags=NO_WIN).stdout or ""
    except Exception as e:
        _log(f"census failed: {e}")
        return []
    rows = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition("|")
        if pid.isdigit() and cmd:
            rows.append((int(pid), cmd))
    return rows


def _beat_age(path):
    """Seconds since the bot last beat, or None if it never has."""
    try:
        stamp = path.read_text(encoding="utf-8").split("\t")[0].strip()
        return (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()
    except Exception:
        return None


def _kill(pid, why):
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=15, creationflags=NO_WIN)
        _log(f"killed PID {pid} ({why})")
    except Exception as e:
        _log(f"kill PID {pid} failed: {e}")


def _launch(name, script):
    py = str(PY312) if PY312.exists() else "python"
    try:
        # Detached, own process group, stdio nulled- see the module docstring. The Popen
        # object is dropped on purpose: we do not want a parent-child relationship to
        # outlive this call.
        subprocess.Popen(
            [py, str(script)], cwd=str(script.parent),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True)
        _log(f"{name}: launched")
        return True
    except Exception as e:
        _log(f"{name}: launch failed: {e}")
        return False


def _tend(name, spec, procs):
    script_name = spec["script"].name
    mine = sorted(p for p, cmd in procs if script_name in cmd)
    age = _beat_age(spec["beat"])
    healthy = age is not None and age < STALE_SEC

    # Duplicates double-answer every mention, the same sin that bit the live session
    # ([[duplicate-channel-sessions]]). Keep the oldest- it holds the warm gateway.
    for extra in mine[1:]:
        _kill(extra, f"duplicate {name}")

    if healthy and mine:
        return "up"

    if mine and not healthy:
        # PID alive, beat stale: the event loop is wedged. A husk that serves nothing is
        # worse than a dead process, because nothing else notices. Reap and replace.
        _kill(mine[0], f"{name} wedged- last beat {int(age) if age else '?'}s ago")

    _launch(name, spec["script"])
    return "restarted"


def main():
    # A pass that finds both bots healthy writes NOTHING to the log, so a healthy guardian
    # and a guardian that never ran looked identical from the outside. This stamp is the
    # script's OWN testimony that it ran, independent of Task Scheduler's result code
    # (which the wscript shim forged for months- see baxter_hidden_run.py). It sits in a
    # `finally` so a throwing census still leaves proof of the attempt, and it writes
    # hung=False explicitly so a healthy pass CLEARS a stale hung flag left by the launcher.
    result = {}
    try:
        if not FLEET["codex"]["script"].exists():
            _log("bot scripts missing- nothing to tend")
            return
        procs = _census()
        result = {name: _tend(name, spec, procs) for name, spec in FLEET.items()}
        if any(v != "up" for v in result.values()):
            _log(f"pass: {json.dumps(result)}")
    finally:
        try:
            hidden_run.stamp("Baxter_CoopGuardian", result=result or None, hung=False)
        except Exception as e:
            _log(f"liveness stamp failed: {e}")


if __name__ == "__main__":
    main()
