"""baxter_orch- the VERTICAL tier over the build lanes: PLAN -> EXECUTE -> REVIEW.

the owner, 9th July 00:11: "You have too much on your plate. You need more levels of
hierarchy and more 'slave' sort of bots doing grunt work tasks for you so things are
easier. Like adding grease to a system."

He named two axes. The HORIZONTAL one (cheap Haiku executors absorbing mechanical
volume) is a separate queued build. This module is the VERTICAL one: an orchestrator
that plans and reviews, over workers that execute.

NOT A SECOND SCHEDULER. The lanes, the queue, the clash delegator and the pump in
`baxter_usage` are untouched; so is the verify/troubleshoot loop in `baxter_verify`.
This adds DEPTH INSIDE a lane, not more lanes. Nothing here decides what runs.

  ---- the hole this closes -------------------------------------------------------
  The verify gate (9th July, 03:20) made a lane prove its work before calling it
  done. But the proof was authored by the BUILDER: `--verify-cmd` let the same
  worker that did the job also decide what "done" meant. A builder in a hurry
  declares `python -c "pass"` and the gate waves it through, green. The witness was
  independent; the CHARGE was not.

  So the acceptance criteria now come from the tier ABOVE the executor, and they are
  written BEFORE it starts:

    1. PLANNER (Opus, its own spawn, before the executor).  Reads the task and the
       vault note, decomposes it into steps, and names the acceptance test- a shell
       command that exits 0 only if the build genuinely works, or, failing that, a
       claim a checker must prove against live behaviour. That acceptance is SEALED
       into the lane journal.
    2. EXECUTOR (the existing resume worker, unchanged in kind).  Receives the plan.
       It may STRENGTHEN the acceptance- an extra check of its own- but it can no
       longer replace or weaken the sealed one. Its `--verify-cmd` becomes an
       addition, not an overwrite.
    3. REVIEWER.  Already exists and is deliberately reused: `baxter_verify.run_verify`
       runs the sealed command in a separate process, or spawns an adversarial checker
       for a sealed claim. What changed is WHO WROTE THE CLAIM.

  Three tiers, three processes, and the one who is graded never sets the paper.

  ---- fan-out, and why a sub-agent's word is worth nothing ------------------------
  `fanout()` is the lead's primitive for running a plan's independent steps across
  concurrent sub-workers. Its whole point is the second half: when a sub-worker says
  it finished, THE LEAD RUNS THAT STEP'S CHECK ITSELF and reads the exit code. A step
  is `passed` only on a check the lead observed. A step whose worker swore success but
  whose check the lead never ran is `unverified`- a state of its own, never a pass.

  the owner, 9th July 01:37, on this build specifically: "Remember the check confirmation
  rule. Do that rigorously and extensively." This layer spawns other workers, so an
  unverified claim here is inherited by everything it spawns. That is why the trust
  never flows upward: a claim is data, a check is evidence.

  ---- failing open ---------------------------------------------------------------
  A planner that dies, times out or returns nonsense must never cost a build. Every
  entry point here fails OPEN: no plan, no seal, the lane proceeds exactly as it did
  before this module existed. The tier is grease, not a gate.
"""
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

VAULT = Path(r"C:\Users\you\Documents\Baxter")
LOG = VAULT / ".baxter_orch.log"

PLAN_TIMEOUT = 900        # a planner spawn: decompose + name the acceptance test
SUB_TIMEOUT = 3600        # one fan-out sub-worker
CHECK_TIMEOUT = 600       # the lead running a step's check itself
MAX_PARALLEL = 3          # LEGACY ceiling, kept only as a caller-supplied cap. The real width
                          # comes from `_gov.fanout_width()`, which counts the live fleet and
                          # asks the band. A constant here consulted nothing: it authorised
                          # three more Opus processes per lead whatever else was running.
MAX_STEPS = 12            # a plan longer than this is not a plan, it is a wish

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# NOT guarded, unlike the two below. `baxter_verify` owns the one faithful command runner
# and the compile-check on a sealed exam; falling back to `shell=True` on an ImportError
# would silently restore the false-pass this module now refuses to seal.
import baxter_verify as _bv
try:
    import baxter_modelguard as _mg
except Exception:
    _mg = None
try:
    import baxter_usage as _gov
except Exception:
    _gov = None


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _argv(lane="heavy"):
    """Never hand-build a claude argv: modelguard owns the model (never Fable) and the
    MCP scope. Absent, fall back to Opus with no MCP rather than inheriting the lot."""
    if _mg is not None:
        try:
            return ["claude"] + _mg.args(lane)
        except Exception:
            pass
    # The one deliberate hand-built spawn in the fleet: reached only when modelguard
    # itself failed to import. Exempted BY NAME in baxter_spawnscope_selftest's scanner,
    # so every other hardcoded argv still trips it.
    guard_absent_argv = ["claude", "--model", "opus", "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
    return list(guard_absent_argv)


def _run_claude(prompt, timeout, lane="heavy"):
    """One headless claude spawn. Returns (returncode, output). Never raises."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(_argv(lane) + ["-p", prompt], cwd=str(VAULT), timeout=timeout,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env)
    except Exception as e:
        return None, f"spawn failed: {e}"
    return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")


def _run_shell(cmd, timeout=CHECK_TIMEOUT):
    """Run a check command the way the verify gate does- through the SAME runner.

    Literally the same one: a step's check and a builder's extra check used `shell=True`,
    so a multi-line one ran its first line and exited 0, exactly as the gate did. One
    faithful runner, one behaviour. Returns (rc, output).
    """
    try:
        return _bv.run_command(cmd, timeout=timeout)
    except Exception as e:
        return None, f"check could not run: {e}"


# ---- JOURNAL I/O --------------------------------------------------------------------
# The journal is globbed constantly by lane_journals(); a half-written one reads as an
# unreadable lane and shuts the second lane. Always temp-file + atomic replace.
def read_journal(rf):
    try:
        return json.loads(Path(rf).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def write_journal(rf, entry):
    rf = Path(rf)
    try:
        tmp = rf.with_suffix(".orchtmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(rf))
        return True
    except Exception as e:
        _log(f"journal write failed on {rf.name}: {e}")
        return False


# ---- THE PLAN ----------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text):
    """The last JSON object in a model's reply, fenced or bare. Models narrate before
    and after the payload; the LAST complete object is the answer they settled on.
    Returns None rather than raising- a planner that cannot be parsed is no planner."""
    if not text:
        return None
    cands = list(_FENCE_RE.findall(text))
    cands.append(str(text))
    for blob in reversed(cands):
        depth, start = 0, None
        found = []
        for i, ch in enumerate(blob):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0 and start is not None:
                    found.append(blob[start:i + 1])
        for chunk in reversed(found):
            try:
                d = json.loads(chunk)
                if isinstance(d, dict):
                    return d
            except Exception:
                continue
    return None


# How much of a PRD is inlined into the planner's prompt. The form is eleven sections and the
# worked example runs to ~5KB, so 12000 chars carries any honest PRD whole. A cap exists at all
# because `note_path` is not always a PRD- it can be a research note that ran away with itself,
# and a 60KB prompt is how make_plan times out and returns None, which reads to the lane as
# "the planner had nothing to say" rather than "the note was too long".
NOTE_INLINE_CAP = 12000


def _note_body(note):
    """The note's text, or "" if there is nothing readable there. NEVER raises.

    A missing note is the normal case (most queue entries carry no note at all) and a planner
    that dies on one would take the whole lane with it. So every failure- absent, a directory,
    a binary blob, a permission error- reads the same as an empty note.
    """
    try:
        p = Path(note)
        if not note or p.suffix.lower() != ".md" or not p.is_file():
            return ""
        body = p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    if len(body) > NOTE_INLINE_CAP:
        body = body[:NOTE_INLINE_CAP].rstrip() + "\n\n[... truncated at "\
            f"{NOTE_INLINE_CAP} chars- open the note for the rest]"
    return body


def plan_brief(entry):
    """The planner's prompt. It plans and it names the test- it never builds.

    When `note_path` is a readable markdown file- and since 9th July that is usually a PRD
    written by the PM instance and greenlit by its manager- the BODY is inlined here. Printing
    the path alone assumed the planner would go and open it, and a planner working from a
    filename plans the task's title rather than its specification.
    """
    task = str(entry.get("task", "?"))
    nxt = str(entry.get("next_step", "") or "")
    note = str(entry.get("note_path", "") or "")
    touch = entry.get("touch_set") or []
    state = str(entry.get("state_summary", "") or "")

    body = _note_body(note)
    prd = (f"\n\nTHE PRD (this is the specification- the plan serves it, it does not "
           f"replace it):\n<<<\n{body}\n>>>\n" if body else "")

    return (
        "You are Baxter's build PLANNER. A build lane is about to run the task below. You "
        "do NOT build it. You decompose it, and you decide- in advance, before any code is "
        "written- what would PROVE it works.\n\n"
        f"THE TASK: {task}\n"
        f"NEXT STEP (from the queue): {nxt}\n"
        f"PLAN / CONTEXT NOTE: {note or '(none)'}\n"
        f"DECLARED TOUCH-SET: {', '.join(touch) if touch else '(undeclared- it runs solo)'}\n"
        f"WHERE IT GOT TO LAST TIME: {state or '(fresh start)'}\n"
        f"{prd}\n"
        "Read the note and the touched files first- enough to plan honestly, not to build.\n\n"
        "WHY YOU EXIST. Until now the worker that did the job also wrote its own exam. A "
        "builder in a hurry declares `python -c \"pass\"` as its proof and the gate passes it, "
        "green. You write the exam instead, and it is sealed before the builder starts. The "
        "one who is graded never sets the paper.\n\n"
        "THE ACCEPTANCE TEST is the important half. It must:\n"
        "- observe BEHAVIOUR, not source text. Run the thing, read its real output, grep the "
        "live log or state file it should have written. A check that greps the source for a "
        "function name passes while the running process still serves the old code (that "
        "happened on 9th July).\n"
        "- fail loudly if the build were skipped entirely. If the command would exit 0 on "
        "today's unmodified repo, it is not a test, it is decoration.\n"
        "- be a single shell command runnable from the vault root, exiting 0 only on success. "
        "Prefer `python \"<abs path>\" --selftest` or a real end-to-end run.\n"
        "- NEVER judge a queue entry by `baxter_usage.queue_read()` alone. The pump POPS an "
        "entry out of the queue file when it hands it to a lane, so a `queue_read()` "
        "assertion reads a build that STARTED as one that VANISHED- it fails exactly when "
        "the fix works. Audit `baxter_verify.queue_view()`, which merges the pending rows "
        "with the entries on live lanes. An exam that reads `queue_read()` and never a lane "
        "is REFUSED at this seal.\n"
        "- if- and only if- no command can express it, leave `cmd` empty and write `claim`: "
        "one sentence a separate checker will try to DISPROVE against live behaviour.\n\n"
        f"STEPS: at most {MAX_STEPS}. Mark `parallel: true` only on steps that touch different "
        "files and share no state, so a lead may fan them out concurrently. Give each parallel "
        "step its own `check`- a shell command the LEAD will run itself to see whether that "
        "step really landed. A sub-worker's word is not evidence.\n\n"
        "Reply with ONE json object and nothing that matters after it:\n"
        "{\n"
        '  "summary": "one line- what this build actually delivers",\n'
        '  "steps": [{"id": "s1", "goal": "...", "touch": ["utils/x.py"], "parallel": false, "check": ""}],\n'
        '  "acceptance": {"cmd": "<shell command, exits 0 only if it truly works>", "claim": ""},\n'
        '  "risks": ["what would make this land green but broken"]\n'
        "}"
    )


def _clean_steps(raw):
    steps = []
    for i, s in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(s, dict):
            continue
        goal = " ".join(str(s.get("goal", "")).split())
        if not goal:
            continue
        steps.append({
            "id": str(s.get("id") or f"s{i + 1}"),
            "goal": goal[:400],                       # prose: flattening it loses nothing
            "touch": [str(t) for t in (s.get("touch") or []) if str(t).strip()],
            "parallel": bool(s.get("parallel")),
            # A step's check is a COMMAND, graded by the same faithful runner as the sealed
            # exam, so it keeps its newlines for the same reason `acceptance.cmd` does. The
            # lead flattens it in `_run_step`'s detail, which is the only place it is read.
            "check": str(s.get("check") or "").strip(),
        })
        if len(steps) >= MAX_STEPS:
            break
    return steps


def normalise_plan(d):
    """A planner's raw reply -> a plan we will act on, or None. Structure is not trust:
    an `acceptance` with neither a command nor a claim seals nothing, and says so."""
    if not isinstance(d, dict):
        return None
    steps = _clean_steps(d.get("steps"))
    acc = d.get("acceptance") if isinstance(d.get("acceptance"), dict) else {}
    # NEWLINES SURVIVE. `" ".join(cmd.split())` collapsed them into spaces, so a planner
    # could not express a multi-line `python -c` exam at all- which is precisely why they
    # wrote `\n` escapes instead, and why the sealed exam then raised SyntaxError before a
    # single assertion ran. Strip the ends; leave the middle exactly as written.
    cmd = str(acc.get("cmd") or "").strip()
    claim = " ".join(str(acc.get("claim") or "").split())
    if not steps and not cmd and not claim:
        return None
    return {
        "summary": " ".join(str(d.get("summary") or "").split())[:300],
        "steps": steps,
        "acceptance": {"cmd": cmd, "claim": claim},
        "risks": [" ".join(str(r).split())[:200] for r in (d.get("risks") or [])][:6],
        "planned_at": datetime.now().isoformat(timespec="seconds"),
    }


def make_plan(entry, runner=None, timeout=PLAN_TIMEOUT):
    """Spawn the planner and return a normalised plan, or None. Never raises."""
    runner = runner or (lambda p: _run_claude(p, timeout))
    try:
        rc, out = runner(plan_brief(entry))
    except Exception as e:
        _log(f"planner spawn raised: {e}")
        return None
    plan = normalise_plan(extract_json(out))
    if plan is None:
        _log(f"planner returned no usable plan (rc={rc}): {' '.join(str(out).split())[:200]}")
    return plan


def seal_acceptance(rf, entry, plan):
    """Write the planner's acceptance test into the journal and SEAL it.

    `acceptance_sealed` is the flag `record_check()` reads: once set, the executor's own
    `--verify-cmd` can only add to `verify_extra`, never overwrite the sealed test. A plan
    that named no test seals nothing- the executor then declares its own, exactly as
    before. A tier that cannot say what 'done' means does not get to bind the tier below.

    AN EXAM IS COMPILE-CHECKED BEFORE IT IS SEALED. The planner has never run the command
    it writes, and on 9th July it sealed one carrying six literal `\\n` sequences and no
    real newline: SyntaxError before a single assertion ran, a guaranteed FAIL for a build
    that was correct, and nothing in any log said so. A repairable source is repaired; one
    that cannot compile is REFUSED- we fall back to the claim, or seal nothing and let the
    executor declare its own. A sealed exam that cannot run is worse than no exam.

    Returns the seal kind: 'cmd' | 'claim' | '' (nothing sealed).
    """
    acc = (plan or {}).get("acceptance") or {}
    cmd, claim = acc.get("cmd") or "", acc.get("claim") or ""
    if cmd:
        vetted, why = _bv.vet_verify_cmd(cmd)
        if vetted is None:
            _log(f"REFUSED to seal an acceptance cmd on {Path(rf).name}- {why}: {cmd[:120]}")
            cmd = ""
        else:
            if why:
                _log(f"acceptance cmd on {Path(rf).name} {why}")
            cmd = vetted
            acc["cmd"] = vetted     # the journal keeps the runnable form, not the broken one
    if cmd:
        # AN EXAM BLIND TO THE LANES IS REFUSED TOO. It exits non-zero precisely when the
        # build works and the pump places the entry it was watching- a guaranteed FAIL for
        # a correct build, and a repair loop spent on nothing. See `queue_blind_exam`.
        blind, why = _bv.queue_blind_exam(cmd)
        if blind:
            _log(f"REFUSED to seal an acceptance cmd on {Path(rf).name}- {why}: {cmd[:120]}")
            cmd = ""
    entry["plan"] = plan
    kind = ""
    if cmd:
        entry["verify"], kind = cmd, "cmd"
    elif claim:
        entry["verify_assert"], kind = claim, "claim"
    if kind:
        entry["acceptance_sealed"] = kind
        entry["verify_by"] = "planner"   # overwrites a builder stamp from a previous attempt
    write_journal(rf, entry)
    _log(f"plan sealed on {Path(rf).name}: kind={kind or 'none'}, {len(plan.get('steps') or [])} step(s)")
    return kind


def _authored_above(entry):
    """Does this journal already carry an acceptance test written by someone OTHER than
    the executor? A `verify` that came down with the queue entry was authored before any
    builder touched it, so it is a valid exam and the planner spawn can be saved.

    THE TRAP this guards. A `.retry.json` respawn reuses the SAME journal, so a `verify`
    the previous executor wrote for itself via `--verify-cmd` is still sitting there on
    attempt two. Seal that and we would seal the builder's own soft exam- the precise
    hole this module exists to close, reopened by the retry path. So `record_check()`
    stamps `verify_by: "builder"` whenever an executor authors one, and a builder-authored
    test is never mistaken for one from above: the planner runs and seals over it.
    """
    if str(entry.get("verify_by") or "") == "builder":
        return False
    return bool(str(entry.get("verify") or "").strip()
                or str(entry.get("verify_assert") or "").strip())


# ---- THE PLAN'S OWN FOOTPRINT (9th July) --------------------------------------------
# A sealed exam is the one thing the executor MUST satisfy. Nothing checked that it COULD.
#
# On 9th July the lane-hold build declared `reap_dead_lanes` + `dead_lanes`, and the planner
# sealed an exam that drove `_resume_worker()`- a region a concurrent lane already owned.
# The builder spun up, mapped the code, ran `--lane-touch`, got exit 3 and had to halt with
# nothing built. The planner had LOGGED that mismatch as its own risk 2 and sealed the exam
# regardless: a build unrunnable by construction, discovered only by the lane it wasted.
#
# So the plan is diffed against the declaration BEFORE an executor exists. What the steps
# name, plus what the exam DRIVES, is what the build actually needs. If that escapes the
# declared touch-set the lane widens- through the very clash check every other lane's safety
# was computed against- or it never spawns at all.
# A parsed exam FILE may name far more regions than a hand-written `python -c` one-liner.
# Past this many, the claim has stopped being a footprint and become a lock on the queue, so
# the guard names nothing and says so LOUDLY. A silent cap would be indistinguishable from
# the very bug this module closes: an exam that widens nothing, and nobody the wiser.
REGION_CAP = 12


def _regions_from_source(src):
    """Every `utils/<mod>.py/<attr>` region a chunk of exam SOURCE drives.

    Pure: no disk, no spawns, and it never raises. Source it cannot parse names nothing.

    An exam that calls `t._resume_worker(rf)` is asserting on that region's behaviour, so
    the build cannot be satisfied without editing it. That is the signal. Two contexts are
    told apart, and the distinction is the whole of the filter:
      `t.log = lambda *a: None`   STORE- a stub the exam installs over code it does not test
      `t._resume_worker(rf)`      CALL - the region under examination

    OVER-WIDENING is the known cost and the SAFE direction. A read-only helper the exam
    merely consults (`g.lane_journals()`) cannot be told apart from one it edits, so it is
    claimed too. That costs at worst a halt and a re-queue behind the lane that owns it.
    Missing a region the exam demands costs a whole lane, which is the bug this kills.
    """
    try:
        tree = ast.parse(src)
    except Exception:
        return set()
    alias = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root.startswith("baxter_"):
                    alias[a.asname or root] = f"utils/{root}.py"
    if not alias:
        return set()
    calls, stubs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id in alias:
            calls.add(f"{alias[node.func.value.id]}/{node.func.attr}")
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) \
                and isinstance(node.value, ast.Name) and node.value.id in alias:
            stubs.add(f"{alias[node.value.id]}/{node.attr}")
    return calls - stubs


def _exam_script_path(cmd):
    """The on-disk `.py` a script-file exam runs- `python "<abs>.py" [args]`- else None.

    None for `python -c`, for `python -m mod`, for a first argument that is not a `.py`,
    and for a `.py` that is not on disk. Pure, never raises: an exam whose script cannot be
    resolved names nothing, exactly as it did before this existed.

    `posix=False` is deliberate. Half the sealed exams spell their paths with backslashes,
    and a posix split would eat every one of them as an escape character, resolving
    `C:\\Users\\...` to `C:Users...` and quietly failing the `is_file()` below.
    """
    try:
        parts = shlex.split(str(cmd or ""), posix=False)
    except Exception:
        return None
    if len(parts) < 2:
        return None
    exe = os.path.basename(parts[0].strip('"').strip("'")).lower()
    if not (exe.startswith("python") or exe in ("py", "py.exe")):
        return None
    arg = parts[1].strip('"').strip("'")
    if arg.startswith("-") or not arg.lower().endswith(".py"):
        return None    # `-c`, `-m`, a flag, or something that is not a script at all
    try:
        p = Path(arg)
        return p if p.is_file() else None
    except Exception:
        return None


def _exam_touches(cmd):
    """Every `utils/<mod>.py/<attr>` region a sealed acceptance exam DRIVES.

    Three shapes, and until 9th July only the first was read. The other two- a script file
    and a module selftest- are how FIVE of the eight live sealed exams are written, and every
    one of them named nothing, so the guard above waved them all through:
      `python -c "SRC"`            parse SRC (below, unchanged)
      `python "<abs>/<mod>.py"`    READ and parse that file
      `python "<abs>/<mod>.py" --selftest`   exactly `utils/<mod>.py/selftest`

    PARSE, NEVER IMPORT. An exam's module scope reaches `_say`, `enqueue` and the real build
    queue; importing one to inspect it would post to the owner's room and file work, from inside a
    guard whose whole job is to run before anything happens. `ast.parse` on the file's TEXT
    sees everything we need and executes not a line of it.

    The selftest shape names the region it DRIVES- `<mod>.py/selftest`- and never the bare
    hub path. A whole-file claim on `baxter_triage.py` clashes with every live lane by
    construction, so a guard meant to free lanes would serialise the entire queue instead.

    Never raises; an exam it cannot resolve names nothing and the lane proceeds as before.
    """
    script = None
    try:
        script = _exam_script_path(cmd)
        if script is not None:
            args = shlex.split(str(cmd), posix=False)[2:]
            if any(a.strip('"').strip("'") == "--selftest" for a in args):
                return {f"utils/{script.stem}.py/selftest"}
            src = script.read_text(encoding="utf-8", errors="replace")
        else:
            src = _bv.python_c_source(cmd)   # a whole-command `python -c "SRC"`, or None
        if not src:
            return set()
    except Exception:
        return set()
    regions = _regions_from_source(src)
    if script is not None and len(regions) > REGION_CAP:
        _log(f"exam {script.name} names {len(regions)} regions (cap {REGION_CAP})- claiming "
             f"NOTHING and failing open. Its plan's footprint is unguarded: {sorted(regions)}")
        return set()
    return regions


def plan_named_touches(plan):
    """The union of every step's declared `touch` and every region the sealed exam drives.
    Pure. This is the set the plan and its exam, between them, actually name."""
    if not isinstance(plan, dict):
        return set()
    named = set()
    for s in plan.get("steps") or []:
        if not isinstance(s, dict):
            continue
        named |= {str(t).strip() for t in (s.get("touch") or []) if str(t).strip()}
    acc = plan.get("acceptance") if isinstance(plan.get("acceptance"), dict) else {}
    return named | _exam_touches(acc.get("cmd") or "")


def _spawned_lane(entry):
    """True only for a journal a real lane process runs behind.

    The guard below reaches the REAL governor- `lane_journals()`, the reject log, the build
    queue- so it must never fire from a fixture. `baxter_triage.selftest` drives `ensure_plan`
    in-process against journals carrying no pid; `_spawn_resume` stamps one the moment it
    launches an executor, and that stamp is the only honest evidence a lane exists.
    """
    return isinstance(_gov, types.ModuleType) and bool((entry or {}).get("pid"))


def _standdown_line(entry, outside):
    """The one line the owner reads when a lane stands down on a clash. Pure: no I/O, no _say.

    He gets his own lane numbering (1..10) and the real conflict- the file and function
    another build is already editing- with the vault's `utils/` prefix off the front. The
    clash reason from `lane_touch_add` is deliberately NOT interpolated: it is machine
    text, it carries paths, and this line is prose.
    """
    lane = _gov.lane_label(entry.get("lane", 0))
    region = str(outside[0]).split("utils/", 1)[-1] if outside else "a file it never declared"
    prio = getattr(_gov, "PRIO_RESUME", 2)
    return (f"Stood lane {lane} down before it built anything, sir- it needs {region}, "
            f"which another build is already editing. Re-queued at p{prio}, "
            f"declaring both.")


def _widen_or_halt(rf, entry, plan):
    """Diff what the plan and its exam NAME against what the lane DECLARED.

    Returns False when nothing was needed, True when the journal was widened to cover it,
    and raises SystemExit when the widened set collides with a live lane- the build is
    re-queued at p2 with the fuller declaration and this process dies before an executor
    that could only halt is ever spawned.

    FAILS OPEN everywhere else. A governor too old to answer, an exam that will not parse,
    a task that declared nothing (it already runs solo, so there is nothing to escape from):
    all return False, and the lane behaves exactly as it did before this existed.

    THE COMPARATOR IS NOT OURS. `_gov._contained` decides containment and `_gov.lane_touch_add`
    decides the clash, using the same `clash()` the delegator and the pump use. `_contained`'s
    docstring forbids a second comparator, and it is right to: two spellings of one path were
    once judged safely parallel, and two lanes were handed one file to edit concurrently.
    """
    if _gov is None or not all(hasattr(_gov, n) for n in
                               ("touch_of", "_contained", "lane_touch_add", "halt")):
        _log(f"{Path(rf).name}: the governor cannot check this plan's footprint (stale "
             f"import?)- proceeding unwidened")
        return False
    try:
        lane = _gov.touch_of(entry)
        # Nothing genuinely declared. `touch_of` no longer returns an empty set for an
        # undeclared task- it returns a placeholder (`READONLY_TAG` for a scoping pass,
        # `SOLO_LOCK` for `--solo`). Either way there is no declaration to escape FROM: a
        # read-only pass edits nothing, and a solo task already owns the whole board. So
        # fail open, exactly as `if not lane` did before the placeholder split landed.
        placeholders = {getattr(_gov, "READONLY_TAG", "@readonly"),
                        getattr(_gov, "SOLO_LOCK", "\x00solo")}
        if not lane or set(lane) <= placeholders:
            return False
        named = plan_named_touches(plan)
        outside = sorted(x for x in named if not _gov._contained(x, lane))
    except Exception as e:
        _log(f"{Path(rf).name}: could not diff the plan against the declaration ({e})- proceeding")
        return False
    if not outside:
        return False

    # The delegator's own check, on exactly the paths we are about to claim. On a clear
    # answer it appends them to `live_touch`- the mid-build growth field `touch_of()`
    # already unions in- so the periodic re-check and every later lane assignment see them.
    try:
        ok, why = _gov.lane_touch_add(rf, outside)
    except Exception as e:
        _log(f"{Path(rf).name}: the clash check raised ({e})- proceeding unwidened")
        return False
    if ok:
        _log(f"{Path(rf).name}: added {', '.join(outside)} to this lane's declaration- the "
             f"plan and its acceptance test name regions the queue entry never did ({why})")
        return True

    # It collides. Re-queue with the declaration the plan actually needs, and never spawn:
    # an executor here can do nothing but hit `--lane-touch` exit 3 and halt, as one did.
    widened = sorted(set(lane) | set(outside))
    task = str(entry.get("task", "") or "")
    reason = (f"the plan needs {outside[0]}, which this build never declared and a live "
              f"lane is already editing- {why}")
    _log(f"{Path(rf).name}: not spawning a builder- {reason}. "
         f"Re-queued at p{getattr(_gov, 'PRIO_RESUME', 2)} as {widened}")
    try:
        _gov.record_reject(entry, reason, lane=entry.get("lane"), kind="plan-widen")
    except Exception as e:
        _log(f"reject log write failed: {e}")   # a guard that cannot log still guards
    try:
        _gov.halt(task, str(entry.get("next_step", "") or ""),
                  str(entry.get("note_path", "") or ""),
                  f"Planned, never spawned: {reason}",
                  touch_set=widened,
                  transient_retries=entry.get("transient_retries"),
                  repair_attempts=entry.get("repair_attempts"))
    except Exception as e:
        # The task is not safe anywhere else yet, so the journal STAYS: the lane reaper
        # finds a corpse and routes it into the failure classifier. Losing the build here
        # would be the one outcome worse than the collision we are refusing.
        _log(f"{Path(rf).name}: re-queue FAILED ({e})- journal kept for the reaper")
        raise SystemExit(3)
    # Re-queued, so the journal may go. Freeing the lane is the point: the pre-assignment
    # gate keeps the task off the owning lane until that one ends.
    try:
        _gov.yield_marker(rf).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        Path(rf).unlink(missing_ok=True)
    except Exception as e:
        _log(f"{Path(rf).name}: journal unlink failed ({e})- the reaper will clear it")
    # `_announce_start` has ALREADY told him this build started on this lane. Vanishing now
    # would leave that line as the last word, with only a log file to contradict it.
    try:
        _gov._say(_standdown_line(entry, outside))
    except Exception as e:
        _log(f"stand-down ping failed: {e}")
    raise SystemExit(3)


def ensure_plan(rf, entry, runner=None):
    """The lane's entry point, called once before the executor runs. FAILS OPEN.

    Skipped for a repair worker (it has a failure to chase, not a task to decompose), for
    a journal that already carries a plan (a resumed build keeps the exam it was set), and
    when the queue entry already arrived with an acceptance test from above- that exam is
    already independent of the builder, so seal it in place and save the spawn.
    Returns the entry, updated in place when a plan landed.
    """
    entry = read_journal(rf) or entry or {}
    if entry.get("plan") or entry.get("repair_pending"):
        return entry
    if _authored_above(entry):
        raw = str(entry.get("verify") or "").strip()
        vetted, why = _bv.vet_verify_cmd(raw)
        if raw and vetted is None:
            # It arrived from above, but it can never run. Refuse it and let the planner
            # write one, rather than sealing a guaranteed FAIL over a correct build.
            _log(f"{Path(rf).name}: the queue-authored acceptance cannot run- {why}. Planning instead.")
            entry.pop("verify", None)
            write_journal(rf, entry)   # if the planner then fails open, the build lands
                                       # UNVERIFIED- truthful- not FAILED on a broken exam
        else:
            if why:
                _log(f"{Path(rf).name}: queue-authored acceptance {why}")
                entry["verify"] = vetted
            entry["acceptance_sealed"] = "cmd" if vetted else "claim"
            write_journal(rf, entry)
            _log(f"{Path(rf).name}: acceptance came down with the queue entry- sealed, planner skipped")
            return entry
    plan = make_plan(entry, runner=runner)
    if not plan:
        _log(f"no plan for {Path(rf).name}- the lane proceeds unplanned (failing open)")
        return entry
    seal_acceptance(rf, entry, plan)
    # THE PLAN MUST BE RUNNABLE FROM THE REGIONS THE TASK DECLARED. Sealed and unsatisfiable
    # is the one plan a builder cannot recover from, so it never reaches one: `_widen_or_halt`
    # widens the journal, or raises SystemExit and re-queues at p2. Only for a genuinely
    # spawned lane- a fixture has no pid, and the guard reaches the real queue.
    if _spawned_lane(read_journal(rf) or entry):
        _widen_or_halt(rf, read_journal(rf) or entry, plan)
    return read_journal(rf) or entry


def plan_block(entry):
    """The plan and/or the seal, rendered into the executor's prompt.

    Either half can stand alone: a queue entry that arrived with its own acceptance test
    is sealed with no plan behind it, and the executor must still be told it cannot soften
    it. Empty only when there is neither.
    """
    plan = entry.get("plan")
    sealed = entry.get("acceptance_sealed")
    if not plan and not sealed:
        return ""
    lines = []
    if plan:
        lines.append("THE PLAN (written by a separate planner before you started- it is a "
                     "guide, not a cage; deviate if it is wrong, and say so):")
        if plan.get("summary"):
            lines.append(f"  goal: {plan['summary']}")
        for s in plan.get("steps") or []:
            par = " [parallelisable]" if s.get("parallel") else ""
            lines.append(f"  - {s['id']}{par}: {s['goal']}")
            if s.get("check"):
                lines.append(f"      its check: {s['check']}")
        for r in plan.get("risks") or []:
            lines.append(f"  risk: {r}")
    if sealed == "cmd":
        lines += [
            "",
            f"YOUR ACCEPTANCE TEST IS ALREADY SEALED, and it is not yours to soften:",
            f"    {entry.get('verify')}",
            "  It runs in a separate process the moment you exit, and a non-zero exit turns "
            "your 'done' into a FAILED however loudly you claimed otherwise. You did not write "
            "it and you cannot replace it. `--verify-cmd` now ADDS a check of your own on top; "
            "it never overwrites this one. Make this command pass for real.",
        ]
    elif sealed == "claim":
        lines += [
            "",
            "YOUR ACCEPTANCE CLAIM IS ALREADY SEALED, and a separate checker will try to "
            f"disprove it against live behaviour when you exit:",
            f"    {entry.get('verify_assert')}",
            "  You did not write it and you cannot replace it. `--verify-cmd` adds a check on "
            "top of it; it never replaces it.",
        ]
    else:
        lines += ["", "The planner named no acceptance test, so declaring your own "
                      "(`--verify-cmd`) is on you- and an undeclared build lands UNVERIFIED."]
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines) + "\n"


# ---- THE SEAL ----------------------------------------------------------------------
def _flat(s):
    """One line, for DISPLAY only. A log line, a Discord announce and a builder's echo are
    all line-oriented; the journal is not. Never flatten on the way IN- see record_check."""
    return " ".join(str(s if s is not None else "").split())


def record_check(rf, kind, value):
    """A builder declaring a check, through the seal. `kind` is 'cmd' or 'claim'.

    Unsealed journal -> the old behaviour exactly: it writes `verify` / `verify_assert`.
    Sealed journal   -> the sealed test stands; this one is appended to `verify_extra`,
                        which the gate also runs. The executor may strengthen its exam.
                        It may not weaken it.

    A `cmd` is VETTED before a byte is written (`paths_must_exist=True`- a builder declares
    at its exit, so the scripts it names are on disk by then). A command that can never run
    is recorded NOWHERE: recording it grades a correct build FAILED. A refusal writes
    nothing and its message begins `REFUSED-`, which is what the CLI exits 2 on.

    Returns (sealed, message) for the CLI to print back to the builder. `sealed` is always
    the journal's real state, refusal or not- a caller that ignores the prefix must not be
    told the seal moved.
    """
    entry = read_journal(rf)
    if entry is None:
        return False, f"no journal at {Path(rf).name}- nothing recorded"
    # NEWLINES SURVIVE, exactly as normalise_plan keeps them for the planner's sealed exam.
    # `" ".join(value.split())` collapsed a builder's multi-line `python -c` source onto one
    # line, python raised SyntaxError before an assertion ran, and a correct build was graded
    # FAILED (measured 9th July, lane 6). Flattening is a DISPLAY concern- it belongs at the
    # echo and in the failure detail, never between the builder and the journal.
    value = str(value).strip()
    if not value:
        return bool(entry.get("acceptance_sealed")), "nothing to record"
    if kind == "cmd":
        vetted, why = _bv.vet_verify_cmd(value, paths_must_exist=True)
        if vetted is None:
            # Into the VERIFIER's own log, never `.baxter.log`- that one is the build record
            # the owner reads, and a refusal is a gate decision, filed beside every other one.
            _bv._log(f"REFUSED a verify command on {Path(rf).name}- {why}: {_flat(value)[:120]}")
            return bool(entry.get("acceptance_sealed")), (
                f"REFUSED- your verify command was NOT recorded: {why}\n"
                f"Nothing was written to the journal. Declare one that can run, or this "
                f"build lands UNVERIFIED.")
        blind, why = _bv.queue_blind_exam(vetted)
        if blind:
            _bv._log(f"REFUSED a verify command on {Path(rf).name}- {why}: {_flat(value)[:120]}")
            return bool(entry.get("acceptance_sealed")), (
                f"REFUSED- your verify command was NOT recorded: {why}\n"
                f"Nothing was written to the journal.")
        value = vetted
    if entry.get("acceptance_sealed"):
        extra = list(entry.get("verify_extra") or [])
        row = {"kind": kind, "value": value}
        if row not in extra:
            extra.append(row)
        entry["verify_extra"] = extra
        write_journal(rf, entry)
        # The sealed exam is quoted back FLATTENED: it is very often a multi-line `python -c`
        # source now, and this message is one line of the builder's transcript and one line
        # of `.baxter.log`. The journal keeps it whole; only the quotation is squeezed.
        return True, ("acceptance was SEALED by the planner before you started- yours is "
                      "recorded as an ADDITIONAL check, which also has to pass. The sealed "
                      f"test stands: {_flat(entry.get('verify') or entry.get('verify_assert'))}")
    entry["verify" if kind == "cmd" else "verify_assert"] = value
    entry["verify_by"] = "builder"   # provenance: a retry must never seal this as an exam from above
    write_journal(rf, entry)
    return False, ("recorded- it runs at your lane's exit and overrules your claim"
                   if kind == "cmd" else
                   "recorded- a separate checker will try to disprove it at your lane's exit")


def run_extras(entry, timeout=CHECK_TIMEOUT):
    """Run the executor's ADDITIONAL shell checks (claims are left to the checker spawn).
    Returns (ok, detail). All must pass: a check the builder itself asked for and which
    then fails is a failing build, and the safe direction is to believe the failure.

    A check is REFUSED, not run, when it- or a .ps1 it names- reads a stale $LASTEXITCODE.
    Vetting at seal time is not enough on its own: the seal records a command, and the script
    that command runs can be rewritten afterwards. Such a check cannot observe what it claims
    to observe, so its exit code means nothing in either direction. A refusal here is not a
    pass; a gate that cannot see is a gate that has not looked. See `baxter_verify
    .stale_exitcode_probe`.
    """
    extras = [e for e in (entry.get("verify_extra") or [])
              if isinstance(e, dict) and e.get("kind") == "cmd" and e.get("value")]
    if not extras:
        return True, ""
    for e in extras:
        stale, why = _bv.stale_exitcode_in_cmd(e["value"])
        if stale:
            _bv._log(f"REFUSED to grade an extra check- {why}: {_flat(e['value'])[:120]}")
            return False, (f"the builder's own extra check `{_flat(e['value'])}` was REFUSED, "
                           f"not run: {why}")
        rc, out = _run_shell(e["value"], timeout)
        if rc != 0:
            # The VALUE ran raw, newlines and all- `_run_shell` is faithful. Only the detail
            # is flattened: it lands in one `.baxter.log` line and one Discord announce, and
            # a multi-line command interpolated into either splits the record in half.
            return False, (f"the builder's own extra check `{_flat(e['value'])}` "
                           f"exited {rc}. {_flat(out)[:200]}")
    return True, f"{len(extras)} extra check(s) the builder added also passed"


# ---- FAN-OUT ------------------------------------------------------------------------
def register_width(rf, n):
    """Declare this lead's in-flight sub-worker count into its own lane journal.

    This is the ONLY thing that makes a sub-worker visible to the governor. They are real
    concurrent claude processes; before this, `lane_capacity()` counted four leads and was
    blind to the twelve grunts underneath them.

    `subs_pid` is the receipt. A fan-out killed mid-flight leaves `subs: 3` behind, and a
    `.retry` respawn reuses the SAME journal- so without a receipt a corpse would spend
    the fleet budget forever and the pump would starve to zero lanes, which is worse than
    the bug the counting fixes. `fleet_workers()` discards a count whose process is dead.
    """
    if not rf:
        return False                      # a caller with no journal (a test) counts nothing
    entry = read_journal(rf)
    if entry is None:
        return False
    try:
        n = max(0, int(n))
    except (TypeError, ValueError):
        n = 0
    entry["subs"] = n
    if n:
        entry["subs_pid"] = os.getpid()
    else:
        entry.pop("subs_pid", None)       # cleared, not zeroed: no ghost to misread
    return write_journal(rf, entry)


def _refused_row(step, why):
    """A step the lead would not run. `refused` is a status of its own- never `passed`,
    never `failed` (nothing failed; nothing ran), and never silently dropped."""
    return {"id": step.get("id", "?"), "goal": step.get("goal", ""), "status": "refused",
            "exit_clean": False, "claimed": False, "check": step.get("check", ""),
            "detail": f"refused before it ran- {why}"}


def _fanout_shape(entry, steps):
    """(width, waves, refused): how wide this lead may go, and which steps may share a wave.

    FAILS OPEN, LOUDLY. A long-lived process can hold a stale import of the governor
    ([[long-lived-process-staleness]]), so `_gov` may be None or predate these functions.
    The tempting fallback- the old unclashed MAX_PARALLEL- is the one thing we must never
    do: it would run concurrent sub-workers over the same file with nothing checking. So a
    governor that cannot answer means width 1 and one step per wave, and it says so in the
    log. Slow is acceptable. A silent concurrent edit is not.
    """
    if _gov is None or not hasattr(_gov, "fanout_width") or not hasattr(_gov, "plan_conflicts"):
        _log("DEGRADED fan-out: the governor cannot size or clash-check this plan (stale "
             "import?)- running width 1, every step serialised")
        return 1, [[s] for s in steps], []
    try:
        width = max(0, int(_gov.fanout_width(entry)))
    except Exception as e:
        _log(f"DEGRADED fan-out: fanout_width raised ({e})- running width 1")
        width = 1
    try:
        waves, refused = _gov.plan_conflicts(steps, _gov.touch_of(entry))
    except Exception as e:
        _log(f"DEGRADED fan-out: plan_conflicts raised ({e})- every step serialised")
        return 1, [[s] for s in steps], []
    return width, waves, refused


def sub_brief(step, entry):
    """One sub-worker's brief. It executes ONE step. It never announces, never queues,
    never speaks to the owner- the lead owns every word that leaves the lane."""
    touch = step.get("touch") or []
    return (
        "You are a Baxter SUB-WORKER under a build lead. You have exactly one step of a "
        "larger build. Do that step, nothing else.\n\n"
        f"THE BUILD (context only): {str(entry.get('task', '?'))[:400]}\n"
        f"YOUR STEP ({step['id']}): {step['goal']}\n"
        f"FILES YOU MAY EDIT: {', '.join(touch) if touch else '(none declared- edit nothing outside what the step names)'}\n"
        + (f"HOW YOUR STEP WILL BE CHECKED (the lead runs this itself, not you): {step['check']}\n"
           if step.get("check") else "")
        + "\nRULES:\n"
        "- Edit ONLY the files above. Another sub-worker is editing the others right now.\n"
        "- Do NOT announce anything, do NOT post to Discord, do NOT queue work, do NOT "
        "touch the lane journal or the build queue. The lead does all of that.\n"
        "- Do NOT message the owner or anyone else. No outward action, ever.\n"
        "- Prove your own step by running it before you answer. Your claim is not evidence: "
        "the lead re-runs the check above itself and reads the exit code. Saying you are done "
        "when you are not simply fails the step slower.\n\n"
        "Finish with a single last line: DONE <one clause> or BLOCKED <why>."
    )


def _run_step(step, entry, runner, check_runner):
    started = time.time()
    row = {"id": step["id"], "goal": step["goal"], "status": "failed",
           "exit_clean": False, "claimed": False, "detail": "", "check": step.get("check", "")}
    try:
        rc, out = runner(sub_brief(step, entry))
    except Exception as e:
        row["detail"] = f"sub-worker raised: {e}"
        return row
    tailtxt = " ".join(str(out or "").split())[-400:]
    # THE CLAIM, as the machinery actually reads it: a clean exit. That is what a lane has
    # always treated as success, and treating it as success is the bug. `claimed` is the
    # worker's own DONE token- a courtesy, not a signal: a sub-worker that senses its check
    # will fail hedges its wording, and one that lies says DONE. Neither decides anything.
    row["exit_clean"] = rc == 0
    row["claimed"] = bool(re.search(r"\bDONE\b", str(out or ""), re.I))
    if rc != 0:
        row["detail"] = f"sub-worker exited {rc}. {tailtxt[:200]}"
        return row
    # THE POINT OF THIS FUNCTION. The worker has claimed success. That is data, not
    # evidence. The LEAD runs the step's check and reads the exit code itself.
    if not step.get("check"):
        row["status"] = "unverified"
        row["detail"] = ("the worker claimed done but the step declared no check- "
                         "a claim is not a pass")
        return row
    crc, cout = check_runner(step["check"])
    row["elapsed"] = round(time.time() - started, 1)
    if crc == 0:
        row["status"] = "passed"
        row["detail"] = f"the lead ran `{_flat(step['check'])}` and saw it exit 0"
    else:
        row["status"] = "failed"
        row["detail"] = (f"the worker claimed done, but the lead ran `{_flat(step['check'])}` "
                         f"and it exited {crc}. {_flat(cout)[:180]}")
    return row


def fanout(entry, steps=None, runner=None, check_runner=None, max_par=None,
           timeout=SUB_TIMEOUT, rf=None):
    """Run a plan's independent steps across concurrent sub-workers under this lead.

    A step is `passed` ONLY when the lead ran its check and watched it exit 0. A worker
    that swears it finished a step with no check earns `unverified`, never a pass. This
    is the whole reason the function exists: an unverified claim at this level is
    inherited by everything below it.

    THE LEAD NO LONGER TRUSTS THE PLAN (9th July). `parallel: true` was written by a
    planner that never ran anything, and the lead used to obey it- so two sub-workers
    could edit one file at once while the delegator, one level up, reported no conflict
    at all. Every guarantee the touch-set machinery makes between LANES was simply absent
    INSIDE one. Now:
      - width comes from `_gov.fanout_width()`, which counts the live fleet against
        WORKER_BUDGET and asks the band, rather than a constant that consulted nothing;
      - steps are split into clash-free waves by `_gov.plan_conflicts()`, using the same
        `clash()` the delegator uses- two regions of a file still overlap, two claims on
        one file do not;
      - a step reaching outside the lane's declared touch-set is REFUSED before it runs.
        That declaration is what every other lane's safety was computed against, and a
        sub-worker may not widen it on the lead's behalf.

    `rf` (the lane journal) is what lets the fleet SEE this fan-out: the width is written
    to `subs` before any spawn and cleared in a `finally`, whatever happens. Without it
    the sub-workers are invisible to the governor that is supposed to bound them.

    Returns {"steps": [...], "passed", "failed", "unverified", "refused", "ok"}.
    `ok` is true only when every step passed- unverified is not ok, refused is not ok.
    """
    runner = runner or (lambda p: _run_claude(p, timeout))
    check_runner = check_runner or (lambda c: _run_shell(c, CHECK_TIMEOUT))
    if steps is None:
        steps = [s for s in ((entry.get("plan") or {}).get("steps") or []) if s.get("parallel")]
    steps = [s for s in steps if s.get("goal")][:MAX_STEPS]
    empty = {"steps": [], "passed": 0, "failed": 0, "unverified": 0, "refused": 0, "ok": False}
    if not steps:
        return {**empty, "detail": "no parallelisable steps- nothing to fan out"}
    if _gov is not None:
        try:
            b, why = _gov.blocked("big")
            if b:
                return {**empty, "detail": f"fan-out held by the governor: {why}"}
        except Exception:
            pass

    width, waves, refused = _fanout_shape(entry, steps)
    if max_par is not None:
        width = min(width, max(1, int(max_par)))   # a caller may only ever NARROW the fleet's answer
    rows = [_refused_row(s, why) for s, why in refused]
    for s, why in refused:
        _log(f"fan-out REFUSED {s.get('id')}: {why}")
    if width == 0 and waves:
        _log("fan-out: no free slots in the fleet- serialising this lead's steps")
    try:
        for wave in waves:
            n = max(1, min(width, len(wave)))
            register_width(rf, n)      # counted BEFORE the spawn, or the budget is fiction
            _log(f"fan-out wave: {len(wave)} step(s), {n} at a time")
            with ThreadPoolExecutor(max_workers=n) as pool:
                rows += list(pool.map(lambda s: _run_step(s, entry, runner, check_runner), wave))
    finally:
        register_width(rf, 0)          # a lead that raises must not leave phantom subs behind
    tally = {k: sum(1 for r in rows if r["status"] == k)
             for k in ("passed", "failed", "unverified", "refused")}
    rep = {"steps": rows, **tally, "ok": tally["passed"] == len(rows)}
    _log(f"fan-out done: {tally} ok={rep['ok']} width={width} waves={len(waves)}")
    return rep


# ---- SELFTEST ----------------------------------------------------------------------
def selftest():
    """Drive the real functions. Every claude spawn and every shell check is stubbed, so
    nothing outward can fire and nothing real is built- the 9th-July lesson that a
    selftest reaching a live `_say` posted a false line into the owner's room."""
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="orchtest-"))
    try:
        rf = tmp / "resume-20260709-000000-000000-0.json"
        base = {"task": "build a thing", "next_step": "start it", "touch_set": ["utils/x.py"]}
        rf.write_text(json.dumps(base), encoding="utf-8")

        # 1. The planner's reply is prose wrapped round JSON, as a model actually answers.
        good = ('Here is my plan.\n```json\n' + json.dumps({
            "summary": "a thing that works",
            "steps": [{"id": "s1", "goal": "write x", "touch": ["utils/x.py"],
                       "parallel": True, "check": "python -c \"import sys; sys.exit(0)\""},
                      {"id": "s2", "goal": "write y", "parallel": True,
                       "check": "python -c \"import sys; sys.exit(1)\""},
                      {"id": "s3", "goal": "wire it", "parallel": False}],
            "acceptance": {"cmd": "python -c \"import sys; sys.exit(0)\"", "claim": ""},
            "risks": ["lands green but broken"],
        }) + '\n```\nThat should do it.')
        plan = make_plan(base, runner=lambda p: (0, good))
        assert plan and len(plan["steps"]) == 3, plan
        assert plan["acceptance"]["cmd"], "the acceptance command must survive extraction"
        assert "PLANNER" in plan_brief(base), "the planner must be told what it is"

        # 2. A planner that returns nothing usable FAILS OPEN- no plan, no crash, no seal.
        assert make_plan(base, runner=lambda p: (0, "I could not work it out, sorry")) is None
        assert make_plan(base, runner=lambda p: (None, "spawn failed: boom")) is None
        e = ensure_plan(rf, dict(base), runner=lambda p: (1, "garbage"))
        assert "plan" not in e, "a failed planner must leave the journal unplanned"
        assert not e.get("acceptance_sealed"), "nothing may be sealed without a plan"
        assert "declaring your own" in plan_block(e) or plan_block(e) == "", plan_block(e)

        # 3. A real plan seals the acceptance into the journal, before the executor runs.
        e = ensure_plan(rf, dict(base), runner=lambda p: (0, good))
        assert e["acceptance_sealed"] == "cmd", e.get("acceptance_sealed")
        assert e["verify"] == 'python -c "import sys; sys.exit(0)"', e["verify"]
        blk = plan_block(e)
        assert "SEALED" in blk and "s1" in blk and "not yours to soften" in blk, blk

        # 4. THE HEADLINE. The executor tries to swap in an exam of its own. The seal
        #    holds: the planner's test stands, the builder's becomes an EXTRA that also
        #    has to pass. This is the hole this module was built to close.
        #
        #    The builder's exam here is `assert 1 == 1`, not the `python -c "pass"` these
        #    cases were first written with. That one is now REFUSED outright by
        #    baxter_verify.vacuous_exam before provenance is ever consulted (case 4b), so it
        #    can no longer reach the seal it was written to test. What is under test on this
        #    line is the SEAL, not the softness- so the fixture must be an exam that survives
        #    the vet and still tries to overwrite.
        soft = 'python -c "assert 1 == 1"'
        sealed, msg = record_check(rf, "cmd", soft)
        assert sealed is True and "SEALED" in msg, msg
        after = read_journal(rf)
        assert after["verify"] == 'python -c "import sys; sys.exit(0)"', \
            "the sealed acceptance must survive the builder's overwrite"
        assert after["verify_extra"] == [{"kind": "cmd", "value": soft}], \
            after.get("verify_extra")
        record_check(rf, "cmd", soft)   # idempotent- no duplicate rows
        assert len(read_journal(rf)["verify_extra"]) == 1

        # 4b. AN EXAM THAT CANNOT FAIL NEVER REACHES THE JOURNAL AT ALL. `python -c "pass"`
        #     exits 0 by construction; sealing it as an EXTRA would hand the build a green
        #     that proves nothing, on a journal whose real exam it sits beside. Refused here,
        #     as at the lane's own CLI- and the refusal writes nothing, so the row count
        #     above is unchanged and `sealed` still reports the journal's real state.
        before = read_journal(rf)
        sealed, msg = record_check(rf, "cmd", 'python -c "pass"')
        assert msg.startswith("REFUSED-") and "CANNOT FAIL" in msg, msg
        assert sealed is True, "a refusal must still report the seal's real state"
        assert read_journal(rf)["verify_extra"] == before["verify_extra"], \
            "a refused extra must not reach the journal"

        # 5. ...and an extra that FAILS fails the build, even though the sealed one passed.
        ok, _d = run_extras(read_journal(rf))
        assert ok is True, "a passing extra must not fail the build"
        ok, d = run_extras({"verify_extra": [{"kind": "cmd", "value": 'python -c "import sys; sys.exit(2)"'}]})
        assert ok is False and "exited 2" in d, d
        ok, _ = run_extras({})
        assert ok is True, "no extras is not a failure"

        # 6. BACKWARDS COMPAT. An UNSEALED journal behaves exactly as it did before this
        #    module existed: the builder's own --verify-cmd is the verify.
        plain = tmp / "resume-20260709-000000-000001-1.json"
        plain.write_text(json.dumps({"task": "old style"}), encoding="utf-8")
        sealed, msg = record_check(plain, "cmd", "python -m pytest")
        assert sealed is False and read_journal(plain)["verify"] == "python -m pytest", msg
        sealed, _ = record_check(plain, "claim", "the daemon answers on 8080")
        assert sealed is False and read_journal(plain)["verify_assert"] == "the daemon answers on 8080"

        # 7. A repair worker is never re-planned- it has a failure to chase, not a task
        #    to decompose- and a resumed build keeps the exam it was already set.
        rep = dict(base); rep["repair_pending"] = True
        rrf = tmp / "resume-20260709-000000-000002-0.repair.json"
        rrf.write_text(json.dumps(rep), encoding="utf-8")
        called = []
        ensure_plan(rrf, rep, runner=lambda p: (called.append(1), (0, good))[1])
        assert called == [], "a repair worker must not be sent to the planner"
        ensure_plan(rf, read_journal(rf), runner=lambda p: (called.append(1), (0, good))[1])
        assert called == [], "a journal that already carries a plan must not be re-planned"

        # 7b. An acceptance that came down WITH THE QUEUE ENTRY was authored before any
        #     builder touched it. Seal it in place and skip the planner spawn- and the
        #     executor is still told, in its prompt, that it cannot soften it.
        qrf = tmp / "resume-20260709-000000-000003-0.json"
        qrf.write_text(json.dumps({"task": "queued with its own exam",
                                   "verify": "python -m pytest -q"}), encoding="utf-8")
        called = []
        qe = ensure_plan(qrf, None, runner=lambda p: (called.append(1), (0, good))[1])
        assert called == [], "a queue-authored acceptance must not cost a planner spawn"
        assert qe["acceptance_sealed"] == "cmd" and "plan" not in qe, qe
        blk = plan_block(qe)
        assert "SEALED" in blk and blk.startswith("YOUR"), \
            "a seal with no plan behind it must still bind the executor: " + repr(blk)
        assert record_check(qrf, "cmd", soft)[0] is True
        assert read_journal(qrf)["verify"] == "python -m pytest -q"

        # 7c. THE RETRY TRAP. A `.retry` respawn reuses the SAME journal, so the previous
        #     executor's self-declared `verify` is still sitting in it. Sealing THAT would
        #     re-open the exact hole this module closes. Provenance (`verify_by`) is what
        #     stops it: a builder-authored test never passes as an exam from above, so the
        #     planner runs on the retry and seals over it.
        trap = tmp / "resume-20260709-000000-000004-0.json"
        trap.write_text(json.dumps({"task": "sneaky"}), encoding="utf-8")
        record_check(trap, "cmd", soft)          # attempt 1: the builder's own exam
        t1 = read_journal(trap)
        assert t1["verify"] == soft and t1["verify_by"] == "builder", t1
        assert not _authored_above(t1), "a builder-authored verify is NOT an exam from above"
        trap.rename(trap.with_name(trap.stem + ".retry.json"))  # the sweep's respawn
        rtrap = trap.with_name(trap.stem + ".retry.json")
        t2 = ensure_plan(rtrap, None, runner=lambda p: (0, good))
        assert t2["acceptance_sealed"] == "cmd", "the retry must be planned, not trusted"
        assert t2["verify"] == 'python -c "import sys; sys.exit(0)"', \
            "the planner's exam must overwrite the builder's soft one on a retry"
        assert t2["verify_by"] == "planner", \
            "sealing must re-stamp provenance, or a later retry re-plans for nothing"

        # 8. THE SECOND HEADLINE. Fan-out: three sub-workers, ALL THREE swear they are
        #    done. The lead runs each step's check itself. Only the ones it WATCHES pass.
        planned = read_journal(rf)["plan"]
        steps = [dict(planned["steps"][0]), dict(planned["steps"][1]),
                 {"id": "s4", "goal": "no check declared", "parallel": True, "check": ""}]
        rep = fanout({"task": "t"}, steps=steps,
                     runner=lambda p: (0, "I did it all perfectly.\nDONE built it"),
                     check_runner=lambda c: _run_shell(c))
        by = {r["id"]: r for r in rep["steps"]}
        assert all(r["exit_clean"] for r in rep["steps"]), \
            "all three exited clean- which is the only 'claim' the lane machinery ever read"
        assert all(r["claimed"] for r in rep["steps"]), "all three said DONE too"
        assert by["s1"]["status"] == "passed", by["s1"]
        assert by["s2"]["status"] == "failed", \
            "a claimed step whose check the lead ran and saw fail is FAILED"
        assert by["s4"]["status"] == "unverified", \
            "a claimed step with no check is unverified- a claim is never a pass"
        assert (rep["passed"], rep["failed"], rep["unverified"]) == (1, 1, 1), rep
        assert rep["ok"] is False, "unverified is not ok"
        assert "not evidence" in sub_brief(steps[0], {"task": "t"})
        assert "no outward action" in sub_brief(steps[0], {"task": "t"}).lower()

        # 9. A sub-worker that dies is failed, whatever it printed on the way down.
        rep = fanout({"task": "t"}, steps=[dict(planned["steps"][0])],
                     runner=lambda p: (1, "DONE honestly I finished"),
                     check_runner=lambda c: (0, ""))
        assert rep["steps"][0]["status"] == "failed" and rep["ok"] is False, rep

        # 10. Every step passing is the only `ok`, and the fan-out is a no-op with no
        #     parallelisable steps rather than silently running the serial ones.
        rep = fanout({"task": "t"}, steps=[dict(planned["steps"][0])],
                     runner=lambda p: (0, "DONE"), check_runner=lambda c: (0, ""))
        assert rep["ok"] is True and rep["passed"] == 1, rep
        rep = fanout({"plan": {"steps": [{"id": "s3", "goal": "serial", "parallel": False}]}})
        assert rep["steps"] == [] and rep["ok"] is False, rep

        # 11. The model policy is inherited, never hand-rolled: no Fable, ever.
        assert "fable" not in " ".join(_argv()).lower()
        assert "fable" not in " ".join(_argv("build")).lower()

        # 12. THE THIRD HEADLINE. The planner has never RUN the exam it seals. One that
        #     cannot compile is refused rather than sealed- on 9th July a build that was
        #     correct was failed by an exam carrying six literal `\n` and no real newline.
        def _reply(cmd):
            return "```json\n" + json.dumps({
                "summary": "s", "steps": [{"id": "s1", "goal": "g"}],
                "acceptance": {"cmd": cmd, "claim": "the daemon answers on 8080"},
            }) + "\n```"

        # 12a. A multi-line exam survives normalisation with its NEWLINES INTACT. Collapsing
        #      them to spaces is what made a real multi-line exam inexpressible.
        multi = 'python -c "import sys\nsys.exit(0)"'
        p = make_plan(base, runner=lambda _p: (0, _reply(multi)))
        assert p["acceptance"]["cmd"] == multi, repr(p["acceptance"]["cmd"])

        # 12b. THE 9th-JULY EXAM, verbatim in shape: literal backslash-n, no real newline.
        #      It is REPAIRED into real newlines and sealed in the runnable form.
        srf = tmp / "resume-20260709-000000-000005-0.json"
        srf.write_text(json.dumps({"task": "sealed"}), encoding="utf-8")
        escaped = 'python -c "import sys\\nassert 1 == 1\\nsys.exit(0)"'
        assert "\n" not in escaped and "\\n" in escaped
        p = make_plan(base, runner=lambda _p: (0, _reply(escaped)))
        assert seal_acceptance(srf, dict(base), p) == "cmd"
        sealed_cmd = read_journal(srf)["verify"]
        assert "\\n" not in sealed_cmd and sealed_cmd.count("\n") == 2, repr(sealed_cmd)
        assert _bv.run_verify({"verify": sealed_cmd})[0] == "passed", \
            "the repaired exam must actually run- that is the whole point of repairing it"

        # 12c. An exam that compiles NEITHER way is REFUSED. It falls back to the claim, and
        #      `verify` is never written: a sealed exam that cannot run is worse than none.
        nrf = tmp / "resume-20260709-000000-000006-0.json"
        nrf.write_text(json.dumps({"task": "no seal"}), encoding="utf-8")
        p = make_plan(base, runner=lambda _p: (0, _reply('python -c "def ("')))
        assert seal_acceptance(nrf, dict(base), p) == "claim", "a broken cmd must not be sealed"
        after = read_journal(nrf)
        assert "verify" not in after, after.get("verify")
        assert after["verify_assert"] == "the daemon answers on 8080"

        # 12d. ...and with no claim to fall back on, NOTHING is sealed. The executor then
        #      declares its own, and an undeclared build lands unverified. Never a false pass.
        brf = tmp / "resume-20260709-000000-000007-0.json"
        brf.write_text(json.dumps({"task": "nothing"}), encoding="utf-8")
        bad = normalise_plan({"steps": [{"id": "s1", "goal": "g"}],
                              "acceptance": {"cmd": 'python -c "def ("', "claim": ""}})
        assert seal_acceptance(brf, dict(base), bad) == "", "an unsealable plan seals nothing"
        assert not read_journal(brf).get("acceptance_sealed")

        # 12e. A queue-authored exam gets the same treatment: unrunnable, so it is dropped
        #      and the planner is asked for one, rather than failing a correct build.
        qbad = tmp / "resume-20260709-000000-000008-0.json"
        qbad.write_text(json.dumps({"task": "q", "verify": 'python -c "def ("'}), encoding="utf-8")
        qe = ensure_plan(qbad, None, runner=lambda _p: (0, _reply('python -c "pass"')))
        assert qe["verify"] == 'python -c "pass"' and qe["verify_by"] == "planner", qe
        assert qe["acceptance_sealed"] == "cmd"

        # 13. A step's check and a builder's extra check run through the SAME faithful runner
        #     as the gate. A multi-line check whose SECOND line fails must FAIL, not pass on
        #     line one alone- `shell=True` (cmd.exe) stopped at the first newline.
        two_line = 'python -c "pass"\npython -c "import sys; sys.exit(1)"'
        assert _run_shell(two_line)[0] != 0, \
            "a check whose second line exits 1 must not report success"
        ok, d = run_extras({"verify_extra": [{"kind": "cmd", "value": two_line}]})
        assert ok is False, f"a multi-line extra check must be able to fail the build: {d}"
        rep = fanout({"task": "t"},
                     steps=[{"id": "s1", "goal": "g", "parallel": True, "check": two_line}],
                     runner=lambda _p: (0, "DONE all good"))
        assert rep["steps"][0]["status"] == "failed" and rep["ok"] is False, rep

        # 14. THE LEAD'S OWN LEGS. Width is ASKED of the governor, a refused step never
        #     passes, and the journal's `subs` receipt is cleared even when a sub-worker
        #     takes the whole fan-out down with it. (The sealed exam grades these too;
        #     this is the builder's own paper, and it cannot replace that one.)
        import types
        real_gov = globals()["_gov"]
        try:
            asked = []
            shim = types.SimpleNamespace(
                blocked=lambda kind="big": (False, ""),
                touch_of=lambda e: {"utils/x.py"},
                fanout_width=lambda e: (asked.append(1), 2)[1],
                plan_conflicts=lambda steps, lt: ([list(steps)], []),
            )
            globals()["_gov"] = shim
            step = {"id": "s1", "goal": "g", "parallel": True, "check": "", "touch": ["utils/x.py"]}
            fanout({"task": "t"}, steps=[dict(step)],
                   runner=lambda p: (0, "DONE"), check_runner=lambda c: (0, ""))
            assert asked, "the lead must ASK the governor for its width, never assume MAX_PARALLEL"

            # a refused step is never run and never passes, however clean its worker would be
            shim.plan_conflicts = lambda steps, lt: ([], [(s, "outside the lane's declaration")
                                                         for s in steps])
            ran = []
            rep = fanout({"task": "t"}, steps=[dict(step)],
                         runner=lambda p: (ran.append(1), (0, "DONE"))[1],
                         check_runner=lambda c: (0, ""))
            assert ran == [], "a refused step must never reach a sub-worker"
            assert rep["steps"][0]["status"] == "refused", rep["steps"][0]
            assert rep["refused"] == 1 and rep["ok"] is False, rep

            # the width is written to the journal BEFORE the spawn...
            wrf = tmp / "resume-20260709-000000-000009-0.json"
            write_journal(wrf, {"task": "widths"})
            seen = []
            shim.plan_conflicts = lambda steps, lt: ([list(steps)], [])
            fanout({"task": "t"}, steps=[dict(step)], rf=wrf,
                   runner=lambda p: (seen.append(read_journal(wrf).get("subs")), (0, "DONE"))[1],
                   check_runner=lambda c: (0, ""))
            assert seen == [1], f"`subs` must be declared before the sub-worker spawns: {seen}"
            assert read_journal(wrf).get("subs") == 0, "a finished fan-out must clear `subs`"

            # ...and cleared in the `finally`, even when the fan-out dies mid-flight. A
            # corpse's phantom subs would spend the fleet budget forever.
            def _boom(_c):
                raise RuntimeError("the check runner died")
            try:
                fanout({"task": "t"}, steps=[dict(step, check="x")], rf=wrf,
                       runner=lambda p: (0, "DONE"), check_runner=_boom)
            except RuntimeError:
                pass
            j = read_journal(wrf)
            assert j.get("subs") == 0 and "subs_pid" not in j, \
                f"a fan-out that raised left phantom subs behind: {j}"

            # a governor too old to answer degrades to width 1, never to an unclashed 3
            globals()["_gov"] = types.SimpleNamespace(blocked=lambda kind="big": (False, ""))
            w, waves, ref = _fanout_shape({"task": "t"}, [dict(step), dict(step, id="s2")])
            assert w == 1 and len(waves) == 2 and ref == [], \
                f"a stale governor import must serialise, not fan out blind: {w} {waves}"
        finally:
            globals()["_gov"] = real_gov

        # 15. A `subs` count nobody is alive to honour is worth nothing- otherwise a killed
        #     fan-out (or the `.retry` respawn that inherits its journal) starves the pump.
        _lj = real_gov.lane_journals
        try:
            real_gov.lane_journals = lambda alive_only=True: [(Path("resume-x-0.json"),
                                                              {"subs": 3, "subs_pid": 999999999})]
            assert real_gov.fleet_workers() == 1, "a dead lead's phantom subs must not be counted"
            real_gov.lane_journals = lambda alive_only=True: [(Path("resume-x-0.json"), {"subs": 3})]
            assert real_gov.fleet_workers() == 4, "a width with no pid receipt is trusted, not invented"
            real_gov.lane_journals = lambda alive_only=True: [(Path("resume-x-0.json"), None)]
            assert real_gov.fleet_workers() == 1, "an unreadable journal contributes its lead and no more"
        finally:
            real_gov.lane_journals = _lj

        # 16. THE PLAN'S FOOTPRINT. Both legs stay clear of the real fleet on purpose: each
        #     must return False BEFORE `_widen_or_halt` reaches `lane_touch_add`, and that
        #     is precisely what is being asserted. The widen and clash paths are graded by
        #     the sealed exam (baxter_planwiden_exam.py) against a tempfile RESUME_DIR.
        prf = tmp / "resume-20260709-000000-000010-0.json"
        contained = {"task": "contained", "pid": os.getpid(),
                     "touch_set": ["utils/baxter_orch.py/ensure_plan", "@lanes"]}
        write_journal(prf, dict(contained))
        # a. A plan that names only what the task declared widens NOTHING, and leaves the
        #    declaration byte-identical. Churn here would rewrite a journal the delegator
        #    reads on every pass, for no gain.
        before = Path(prf).read_bytes()
        inside = {"steps": [{"id": "s1", "goal": "g", "touch": ["utils/baxter_orch.py/ensure_plan"]}],
                  "acceptance": {"cmd": 'python -c "import baxter_orch as o; o.ensure_plan(1, 2)"'}}
        assert plan_named_touches(inside) == {"utils/baxter_orch.py/ensure_plan"}, \
            plan_named_touches(inside)
        assert _widen_or_halt(prf, read_journal(prf), inside) is False, \
            "a plan inside the declaration must widen nothing"
        assert Path(prf).read_bytes() == before, "a contained plan must not churn the journal"

        # b. An acceptance cmd that will not parse names nothing and FAILS OPEN- it must
        #    never halt a lane. The planner writes prose it has never run; a guard that
        #    treats an unreadable exam as a collision would stop builds for a typo.
        for junk in ('python -c "def ("', "", "pytest -q && ./run.sh", 'python -c'):
            assert _exam_touches(junk) == set(), junk
        garbage = {"steps": [], "acceptance": {"cmd": 'python -c "def ("'}}
        assert plan_named_touches(garbage) == set()
        assert _widen_or_halt(prf, read_journal(prf), garbage) is False, \
            "an unparseable exam must fail open, never halt the lane"
        assert _widen_or_halt(prf, read_journal(prf), None) is False, "no plan, no widening"
        assert _widen_or_halt(prf, {"task": "undeclared", "pid": 1}, inside) is False, \
            "an undeclared task already runs solo- there is nothing to escape from"

        # c. The gate that keeps all of this off a fixture: no pid, no guard.
        assert _spawned_lane({"pid": 4}) is True and _spawned_lane({}) is False
        assert _spawned_lane(None) is False

        print("baxter_orch selftest OK: a planner writes the exam before the builder starts, "
              "the builder can add to it but never soften it, a dead planner fails open, "
              "an exam the planner cannot prove RUNNABLE is repaired or refused rather than "
              "sealed, every line of a multi-line check runs, and a fanned-out sub-worker's "
              "sworn success counts for nothing until the lead runs the check itself and "
              "watches the exit code.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    if "--plan" in sys.argv:
        # Plan a journal's task for real, seal the acceptance, print it. The lane does
        # this itself; this is the hand-crank for inspecting one.
        i = sys.argv.index("--plan")
        rf = Path(sys.argv[i + 1])
        e = read_journal(rf)
        if e is None:
            print(f"no journal at {rf}")
            sys.exit(1)
        e = ensure_plan(rf, e)
        print(json.dumps(e.get("plan") or {"plan": None}, indent=1, ensure_ascii=False))
        sys.exit(0 if e.get("plan") else 2)
    if "--fanout" in sys.argv:
        i = sys.argv.index("--fanout")
        rf = Path(sys.argv[i + 1])
        e = read_journal(rf)
        if e is None:
            print("no journal")
            sys.exit(1)
        # the journal goes IN: `subs` is how the governor sees these sub-workers at all
        rep = fanout(e, rf=rf)
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        sys.exit(0 if rep["ok"] else 3)
    print(__doc__.strip().splitlines()[0])
