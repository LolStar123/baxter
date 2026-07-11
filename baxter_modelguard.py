"""baxter_modelguard - the never-Fable layer + the tiering picker (Atul, 8th-9th July).

ONE source of truth for how any Baxter worker/session launches `claude`, plus a
runtime detector that catches Fable drift and flags Atul.

  TWO AXES, and they are independent (9th July):

    CLASS -> MODEL   `CLASS_MODELS`   how clever must this job be?
    CLASS -> MCP     `CLASS_MCP`      which servers may it load?  (overridable per call)

  A spawn site declares a WORK CLASS and gets both. `args(cls, mcp=...)` lets the
  MCP scope move on its own- inbox filing is class 'file' (Haiku) whether or not the
  batch carries a link; only the servers change. They used to be ONE string, and that
  conflation was the bug: a filing batch with a URL asked for lane 'media', which
  silently picked its model too.

  SHIPPED TIERING (Atul, 9th July 00:11- "more 'slave' sort of bots doing grunt work
  tasks for you... like adding grease to a system", and again 09:19):

    file   -> Haiku 4.5   inbox filing / dump triage- structured, templated, high-volume
    prose  -> Sonnet 5    briefs, weekly digest, chase drafts, answers, dashboard chat
    fast   -> Sonnet 5    the fast-lane conversational replies (unchanged)
    classify/reconcile/mine -> Opus   mechanical, but NOT yet widened (plan step 3)
    heavy/build/live/orch/resume -> Opus   NEVER downgraded. `NEVER_DOWNGRADE` + selftest.

  Retiering a whole job is now a one-word edit to CLASS_MODELS; no spawn site moves.
  An UNKNOWN class resolves to Opus, never to the grunt tier- a site that forgets to
  declare itself must fail expensive, not silently run the flagship's judgement on Haiku.

  Fable is REFUSED outright- pick()/args() NEVER return it, and if a caller ever passes
  Fable explicitly it is normalised to Opus + logged. Baxter is always Opus-and-stronger;
  Fable is a hard no (his words: "never ever EVER pick fable... you should always be opus
  and weaker").

  Proven by `baxter_tier_selftest.py`, which monkeypatches subprocess inside baxter_triage
  and CALLS the real spawn functions, asserting on the argv they actually build. Reading
  CLASS_MODELS and asserting on the dict would pass while every spawn site asked for the
  wrong class.

  Detector + SELF-HEAL (--audit, no Claude burn): scans Baxter's OWN live sessions
  (the vault project dir) + running claude.exe cmdlines for Fable. On a hit it
  AUTO-HEALS with zero prompt to Atul- it KILLS the drifted Fable instance (the
  triage/queue machinery then respawns that work pinned to Opus). Atul's standing
  order (8th July 22:02, emphatic repeat): "always take measures to ensure we are
  on opus AUTOMATICALLY upon detection", "no matter what"- and 22:00: "You sort it
  out or kill that instance." So the guard sorts it out itself; the old "swap it
  back with /model opus, sir" nag is RETIRED as the primary action. Atul is pinged
  ONLY as a fallback, once/hour, when the auto-heal genuinely can't reach the
  instance (no killable process mapped to it). Atul's own manual Fable sessions
  live under OTHER project dirs (e.g. the Fortnite maps) and are deliberately NOT
  swept- only Baxter's own vault sessions are policed/healed.

Why this exists: 27 Baxter big-task sessions silently burned claude-fable-5 on
8th July before the spawn sites were pinned. Pinning --model in 11 places is
fragile- one new spawn site without it reopens the hole and nothing notices.
This layer makes the policy central + self-checking. Spawn sites should call
args()/pick() rather than hardcode a model; the detector backstops the rest.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HEAVY = "opus"        # orchestrator, builds, the live session, resume- never downgraded
FAST = "sonnet"       # the declared fast/quick-reply lane
GRUNT = "haiku"       # mechanical, structured, high-volume: inbox filing
PROSE = "sonnet"      # briefs, weekly digest, chase drafts, answering him
BANNED = ("fable",)   # never, ever- Atul's hard line

# ---------------------------------------------------------------------------
# TWO AXES, NOT ONE (9th July). A spawn declares a WORK CLASS; the class picks the
# model, and (by default) the MCP profile. They used to be one string- 'heavy' meant
# both "Opus" and "no MCP servers"- so a triage batch carrying a link had to ask for
# lane 'media', which silently also chose its model. Conflated, the two axes cannot
# both be right: the batch that needs a browser is the same batch that should run on
# the cheap model.
#
#   class -> model   (CLASS_MODELS)   "how clever must this be"
#   class -> MCP     (CLASS_MCP)      "which servers may it load"   [overridable per call]
#
# Atul, 9th July 00:11: "more 'slave' sort of bots doing grunt work tasks for you...
# like adding grease to a system", and again 09:19 pasting the tiering proposal back.
# Filing is the grunt: structured template, high volume, no judgement- Haiku 4.5.
# Prose (briefs/digests/drafts) is Sonnet 5. Everything that THINKS stays Opus.
# ---------------------------------------------------------------------------
CLASS_MODELS = {
    # --- tiered down (this build) ---
    "file":      GRUNT,   # inbox filing / dump triage- wake_claude, _worker_run
    "prose":     PROSE,   # briefs, weekly, follow-up drafts, answers, dashboard chat
    "fast":      FAST,    # fast-lane conversational replies (baxter_reply_worker)
    # --- mechanical, but NOT downgraded yet: plan step 3 widens only once the
    #     filing pass is proven clean. The class name is the lever; flipping one
    #     value here retiers a whole job, with no spawn site touched. ---
    "classify":  HEAVY,   # triage_photos
    "reconcile": HEAVY,   # maybe_reconcile
    "mine":      HEAVY,   # process_mine_queue (also the 1M-context case- see below)
    # --- NEVER downgraded (Atul: "Baxter is never downgraded") ---
    "heavy":     HEAVY,
    "build":     HEAVY,
    "live":      HEAVY,
    "orch":      HEAVY,
    "resume":    HEAVY,
    # --- legacy lane name kept as a class so old callers keep their exact behaviour ---
    "media":     HEAVY,
}

# The flagship set. selftest() fails loudly if any of these ever stops being Opus-
# a downgrade here is the one regression that would look like a cost saving and read
# as a lobotomy. Grunt tiers exist so the flagship thinks, not so it shrinks.
NEVER_DOWNGRADE = ("heavy", "build", "live", "orch", "resume")

# Mining reads whole Claude transcripts. Haiku 4.5 and Sonnet 5 are BOTH 200K-context;
# only Opus carries 1M- so "drop mining to Sonnet" is not a context fix, it is the same
# ceiling for less capability. process_mine_queue caps its payload at 200_000 CHARS
# (~50K tokens), which fits any of the three; it stays Opus here on judgement, not size.

# ---------------------------------------------------------------------------
# MCP SCOPING- the other half of "how a Baxter worker launches claude" (9th July).
#
# Atul's box inherits five GLOBAL stdio MCP servers from ~/.claude.json (playwright,
# shadcn, context7, paper-search, baxter-vault) plus the Discord plugin. Every headless
# worker was inheriting all of them: measured +5 node and +5 conhost processes per
# spawn, and the dispatcher runs up to 5 triage workers in parallel. On a resident
# baseline of ~52 node that is the observed 76-node / 74-conhost burst- console-handle
# and CPU contention that starves Atul's interactive `claude --resume` TUI mid-render.
# That starvation is the ROOT TRIGGER of the terminal lag + ANSI corruption.
#
# The workers never needed them. Baxter reads Discord through baxter_read_channel.py
# (raw token GET- deliberately NOT the plugin, whose allowlist 403s), sends through
# baxter_say.py, reacts through baxter_react.py, reads mail over IMAP in python, and
# touches the vault with its native Read/Write/Edit tools. shadcn (UI registry),
# context7 (library docs) and paper-search (academic papers) are dead weight to every
# Baxter lane. The ONE genuine need is Playwright, for contract step 30's link/media
# classification and [[read-blocked-urls-via-browser]]- so it is handed out per-lane,
# never globally.
#
# `--strict-mcp-config` ignores EVERY other MCP config (global, project, plugin), so
# the profile below is exactly what the worker gets. Verified 9th July: strict + an
# empty profile spawns zero MCP node processes and the model reports no mcp__ tools.
# Atul's own interactive shells are untouched- this only decorates Baxter's spawns.
# ---------------------------------------------------------------------------
_NO_MCP = {"mcpServers": {}}
_PLAYWRIGHT_ONLY = {"mcpServers": {"playwright": {
    "type": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp@latest"], "env": {}}}}

# lane -> the MCP servers that lane may load. `None` = inherit the global config
# (the escape hatch; nothing uses it by default).
MCP_PROFILES = {
    "fast":  _NO_MCP,          # quick conversational replies- no browser, no docs
    "heavy": _NO_MCP,          # briefings, reconcile, follow-ups, verify, resurrect
    "media": _PLAYWRIGHT_ONLY,  # a triage batch that actually carries a link/attachment
    "build": _PLAYWRIGHT_ONLY,  # build lanes- research may hit a login-walled URL
    "full":  None,             # inherit everything (unused; kept as a deliberate opt-in)
}

# class -> its DEFAULT MCP profile. A caller may override per spawn (args(cls, mcp=...)):
# inbox filing is always class 'file' (Haiku), but the ONE batch in twenty that carries a
# link asks for the 'media' profile so it gets a browser. Model fixed, servers variable-
# that separation is the whole point of the two axes.
CLASS_MCP = {
    "file":      "heavy",   # plain-text dumps: no servers. _batch_lane() passes 'media' when needed.
    "prose":     "heavy",
    "fast":      "fast",
    "classify":  "heavy",   # 60-Photos images are read off disk with the Read tool, not a browser
    "reconcile": "heavy",
    "mine":      "heavy",
    "heavy":     "heavy",
    "build":     "build",   # a build's research may hit a login-walled URL
    "live":      "heavy",
    "orch":      "heavy",
    "resume":    "build",
    "media":     "media",   # legacy lane name -> the browser profile it always meant
}

VAULT = os.environ.get("BAXTER_VAULT", r"C:\Users\you\Documents\Baxter")
UTILS = os.path.dirname(os.path.abspath(__file__))
SAY = os.path.join(UTILS, "baxter_say.py")
ALERT = os.path.join(VAULT, ".baxter_fable_alert.json")
PINGED = os.path.join(VAULT, ".baxter_fable_pinged")
LOG = os.path.join(VAULT, ".baxter_modelguard.log")
# Baxter's OWN sessions run with cwd = the vault, so their transcripts live under
# this project dir. Atul's manual work (Fortnite Fable, etc.) lives under other
# project dirs and must NEVER be flagged/swept as a Baxter drift.
BAXTER_PROJ = os.path.join(os.path.expanduser("~"), ".claude", "projects",
                           "C--Users-you-Documents-Baxter")


def is_fable(name):
    return "fable" in (name or "").lower()


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def pick(cls="heavy", requested=None):
    """The model a spawn should use. NEVER returns Fable.

    cls       : the WORK CLASS ('file', 'prose', 'build', ...). Looked up in
                CLASS_MODELS. An UNKNOWN class resolves to Opus, never to the
                grunt tier: a spawn site that forgets to declare its class must
                fail expensive, not silently run the flagship's judgement on Haiku.
    requested : an explicit model a caller wants. If it's Fable it is REFUSED
                (normalised to Opus) + logged- the guard self-corrects rather
                than obey. A non-Fable explicit request is honoured as-is.
    """
    if requested and is_fable(requested):
        _log(f"REFUSED Fable request (class={cls}) -> forced {HEAVY}")
        return HEAVY
    if requested:
        return requested
    return CLASS_MODELS.get(str(cls).lower(), HEAVY)


def mcp_args(profile="heavy"):
    """CLI args scoping a spawn's MCP servers. Takes an MCP PROFILE name (a key of
    MCP_PROFILES), not a work class- args() maps class -> profile before calling here.

    Unknown profile -> the no-MCP profile: a new spawn site is quiet by default, and a
    lane that genuinely needs a server has to say so. (The mirror of pick()'s never-
    Fable stance: the safe answer is the default, not the thing you forgot to set.)
    """
    prof = MCP_PROFILES.get(str(profile).lower(), _NO_MCP)
    if prof is None:
        return []
    # --mcp-config takes a JSON *string* as well as a path, so no temp file to clean up.
    return ["--mcp-config", json.dumps(prof), "--strict-mcp-config"]


def args(cls="heavy", mcp=None):
    """Full CLI args for a `claude` spawn: the model (never Fable) + the MCP scope.

    cls : the work class- picks the MODEL, and the default MCP profile.
    mcp : optional MCP profile override- picks the SERVERS only, never the model.
          Inbox filing is class 'file' (Haiku) whether or not the batch carries a
          link; only the profile moves. Deriving the class from the MCP need is the
          bug this signature exists to prevent.

    Every Baxter spawn site should build its command as `["claude"] + args(cls)`, so
    both policies land in one place. Hardcoding `--model opus` at a spawn site is the
    bug this module exists to prevent- and it applies to the MCP flags identically.
    """
    profile = mcp if mcp is not None else CLASS_MCP.get(str(cls).lower(), "heavy")
    return ["--model", pick(cls)] + mcp_args(profile)


def _claude_procs():
    """[(pid, cmdline), ...] for every running claude.exe. One CIM call feeds both
    the Fable-cmdline scan and the session->PID map used for auto-heal."""
    procs = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30).stdout or ""
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):   # ConvertTo-Json emits a bare object for one proc
            data = [data]
        for d in data:
            procs.append((d.get("ProcessId"), d.get("CommandLine") or ""))
    except Exception:
        pass
    return procs


def _fable_proc_pids(procs):
    """PIDs of claude.exe explicitly pinned to --model fable in their cmdline. The
    pinned spawns can't be, but a hand-launched or drifted one could- belt-and-braces.
    Matches only the ACTUAL model flag, not any mention of 'fable' elsewhere in the
    cmdline (e.g. a prompt that talks ABOUT Fable, like this task's own)."""
    pids = []
    for pid, cmd in procs:
        for m in re.findall(r"--model[=\s]+(\S+)", cmd):
            if is_fable(m):
                pids.append(pid)
                break
    return pids


def _session_pid_map(sessions, procs):
    """{session-basename: [pids]} - claude.exe whose cmdline references a drifted
    session's id (via --session-id / --resume / -r). Baxter's lanes launch with
    those flags ([[three-lane-and-session-continuation]]), so a /model-drifted
    session is almost always mappable to its owning process and thus killable."""
    out = {}
    for s in sessions:
        stem = s[:-6] if s.endswith(".jsonl") else s   # strip .jsonl -> uuid
        if not stem:
            continue
        for pid, cmd in procs:
            if stem in cmd:
                out.setdefault(s, []).append(pid)
    return out


def _kill(pids):
    """Force-kill the given PIDs. Returns those actually killed. taskkill /F because
    a drifted worker mid-burn won't exit on a polite signal."""
    killed = []
    for pid in {p for p in pids if p}:
        try:
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                killed.append(pid)
        except Exception:
            pass
    return killed


def _live_baxter_fable(minutes=25):
    """Baxter-vault sessions whose LATEST assistant turn is Fable and whose
    transcript was appended in the last `minutes` (i.e. genuinely live)."""
    hits = []
    now = time.time()
    for f in glob.glob(os.path.join(BAXTER_PROJ, "*.jsonl")):
        try:
            if now - os.path.getmtime(f) > minutes * 60:
                continue
            lines = open(f, encoding="utf-8").readlines()
        except Exception:
            continue
        for line in reversed(lines[-60:]):
            if '"model"' not in line:
                continue
            try:
                m = json.loads(line).get("message", {}).get("model")
            except Exception:
                m = None
            if m:
                if is_fable(m):
                    hits.append(os.path.basename(f))
                break
    return hits


def _clear(*paths):
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def audit(minutes=25, quiet=False):
    """Scan Baxter's live sessions + running procs for Fable and AUTO-HEAL on a hit-
    kill the drifted instance so triage respawns it on Opus, zero prompt to Atul.
    Ping Atul ONLY as a fallback when the auto-heal can't reach the instance.
    Returns 0 (clean or fully self-healed) or 2 (Fable left unhealed). No Claude burn."""
    sessions = _live_baxter_fable(minutes)
    procs = _claude_procs()
    fable_pids = _fable_proc_pids(procs)
    if not sessions and not fable_pids:
        _clear(ALERT, PINGED)   # drift cleared- reset the fallback-ping rate limiter too
        return 0

    # --- AUTO-HEAL (his 8th-July 22:02 order): sort it out, never nag first. Kill
    # every drifted Fable instance we can map to a process; triage's queue pump then
    # restarts that work pinned to Opus. This IS the "you sort it out or kill that
    # instance" he authorised- not a ping asking him to do it. ---
    sess_map = _session_pid_map(sessions, procs)
    unmappable = [s for s in sessions if not sess_map.get(s)]   # no proc -> can't kill
    kill_pids = set(fable_pids)
    for pids in sess_map.values():
        kill_pids.update(pids)
    killed = _kill(kill_pids)
    _log(f"FABLE DETECTED sessions={sorted(set(sessions))} fable_procs={len(fable_pids)}; "
         f"AUTO-HEAL killed={sorted(killed)} unmappable={unmappable}")

    # Verify the heal on a PROCESS basis (jsonl mtime stays 'recent' after a kill, so
    # re-reading transcripts would false-fail). Heal succeeded when no Fable-pinned
    # process survives AND every drifted session was mapped+killed.
    time.sleep(1.5)
    still_fable = _fable_proc_pids(_claude_procs())
    unhealed = bool(still_fable) or bool(unmappable)
    if not unhealed:
        _clear(ALERT, PINGED)   # fully self-healed, silently- no ping (his hard rule)
        _log("AUTO-HEAL ok- drift cleared silently, no ping")
        return 0

    # --- FALLBACK ONLY: the auto-heal genuinely couldn't reach an instance. NOW tell
    # him, once/hour, so a drift can't burn Fable unseen. ---
    rec = {"when": datetime.now(timezone.utc).isoformat(),
           "unhealed_sessions": sorted(set(unmappable)),
           "surviving_fable_procs": sorted(still_fable),
           "auto_killed": sorted(killed)}
    try:
        with open(ALERT, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
    except Exception:
        pass
    if not quiet:
        recent = False
        try:
            recent = os.path.exists(PINGED) and (time.time() - os.path.getmtime(PINGED) < 3600)
        except Exception:
            pass
        if not recent:
            n = len(unmappable) + len(still_fable)
            msg = (f"⚠️ Model-guard: a Fable session drifted and I couldn't auto-kill it "
                   f"({n} left). Auto-heal reached the rest. This one won't die on its own- "
                   f"kill it or /model opus, sir.")
            _ping(msg)
    return 2


def _ping(msg):
    """The ONE outward call this guard makes- a drift alert to Atul. Returns True only if the
    line actually left. Exit 3 is a DENIAL: baxter_say refused the claim, printed why, and sent
    nothing. Record the reason in the voiceless denial sink and do NOT stamp PINGED- the
    hour-cooldown must never start on a ping that never went
    ([[outward-path-proven-from-the-send-ledger]])."""
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run([sys.executable or "python", SAY, msg],
                           cwd=VAULT, timeout=30, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True)
    except Exception as e:
        _log(f"drift ping failed to launch: {e}")
        return False
    if getattr(p, "returncode", 0) == 3:
        try:
            import baxter_autobuild as autobuild
            row = autobuild.denial_alert("baxter_modelguard._ping", str(msg)[:80], p.stderr)
            _log(f"DENIED: the drift ping was refused- {row['reason']}")
        except Exception:
            pass
        return False
    open(PINGED, "w").close()
    return True


def selftest():
    """Static policy check on the CLASS axis: every class resolves to a real model,
    none resolves to Fable, an explicit Fable request is refused on EVERY class, an
    unknown class lands on Opus, and the flagship set is still Opus."""
    assert pick() == HEAVY, pick()
    assert pick("fast") == FAST, pick("fast")
    assert pick(requested="claude-fable-5") == HEAVY
    assert pick("fast", requested="fable") == HEAVY
    assert pick(requested="opus") == "opus"
    assert not is_fable("opus") and is_fable("claude-fable-5")

    # every declared class resolves to a real, non-Fable model...
    for cls in CLASS_MODELS:
        m = pick(cls)
        assert m, f"class {cls!r} resolved to nothing"
        assert not is_fable(m), f"class {cls!r} resolved to Fable"
        # ...and refuses an explicit Fable request, not just the two once tested
        assert pick(cls, requested="claude-fable-5") == HEAVY, cls
        assert "fable" not in " ".join(args(cls)).lower(), cls

    # an unknown class must be EXPENSIVE, never grunt: a spawn site that forgets to
    # declare its class runs Opus and costs money, rather than silently running the
    # orchestrator's judgement on Haiku.
    assert pick("a_class_nobody_declared") == HEAVY
    assert pick("") == HEAVY

    # the tiering that actually shipped
    assert pick("file") == GRUNT, pick("file")
    assert pick("prose") == PROSE, pick("prose")

    # the flagship set- a downgrade here must fail loudly, not save a few pennies
    for cls in NEVER_DOWNGRADE:
        assert pick(cls) == HEAVY, f"NEVER_DOWNGRADE class {cls!r} is {pick(cls)}, not {HEAVY}"

    # the two axes are independent: the model must not move when the MCP scope does
    assert pick("file") == GRUNT
    a_plain, a_media = args("file"), args("file", mcp="media")
    assert a_plain[:2] == a_media[:2] == ["--model", GRUNT], (a_plain, a_media)
    assert "playwright" in " ".join(a_media) and "playwright" not in " ".join(a_plain)

    # legacy lane-string callers (orch/verify/resurrect/reply_worker/spawnscope) unchanged
    assert args("heavy")[:2] == ["--model", HEAVY]
    assert args("fast")[:2] == ["--model", FAST]
    assert args("media") == ["--model", HEAVY] + mcp_args("media")
    assert args("build") == ["--model", HEAVY] + mcp_args("build")
    assert mcp_args("full") == []

    print(f"modelguard selftest OK: {len(CLASS_MODELS)} classes, file={GRUNT}, "
          f"prose={PROSE}, fast={FAST}, flagship={HEAVY}, Fable refused.")


if __name__ == "__main__":
    if "--audit" in sys.argv:
        sys.exit(audit(quiet="--quiet" in sys.argv))
    elif "--selftest" in sys.argv:
        selftest()
    elif "--pick" in sys.argv:
        i = sys.argv.index("--pick")
        lane = sys.argv[i + 1] if len(sys.argv) > i + 1 else "heavy"
        print(pick(lane))
    else:
        print(__doc__)
