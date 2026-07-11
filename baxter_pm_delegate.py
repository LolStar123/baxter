#!/usr/bin/env python3
"""The PM delegate: a separate Opus instance writes the PRD, and nothing else files a build.

Atul, 9th July 09:47. `baxter_prd_template` is the form and the gate; this is the thing
that hands the form to a product manager and refuses to file what comes back if it does
not validate.

  write_prd(ask, queue_it=True)
    -> spawn an Opus 4.8 instance with the raw ask verbatim, the blank template, the
       worked example, and the touch-set/hub-region rules
    -> validate what it returns against the real gates (touch_problems, vet_verify_cmd)
    -> hand the valid document to a SECOND Opus, the PM-manager, which answers
       `greenlight` / `changes` / `reject` and is logged to `.baxter_pm_reviews.jsonl`
    -> up to MAX_ATTEMPTS goes, feeding the refusals back to it- the validator's, or the
       manager's- ALONGSIDE the document the PM itself last wrote, so the retry amends that
       document instead of composing a new one and silently losing a section it had right
    -> a PM that returns that document UNCHANGED after the manager's note has stalled: it is
       stopped on receipt, logged `stalled`, and the manager never re-reads what it sent back
    -> not greenlit: write NOTHING, queue NOTHING, return None, and say why in .baxter.log
    -> greenlit: the document lands in `60-PRDs/`, and `--queue` is called with the
       touch-set, verify command, priority and gate parsed OUT OF THE DOCUMENT

TWO GATES, AND THEY CATCH DIFFERENT THINGS. The validator is a regex: it knows a section is
missing, it does not know the Evidence line is fiction. The manager is a reader: it opens the
`file:line` and finds nothing there. A machine-valid PRD for a build that should not exist
passes the first gate every time, and that is precisely what the second one is for.

The PM is a WRITER. It never edits code, never runs a build, never touches the queue file.
The model comes from `baxter_modelguard.args('heavy')`- never a hardcoded `--model opus`,
and never Fable. That is the module whose whole existence is to stop a spawn site guessing.

WHAT THIS DOES NOT DO (say it plainly, it is not a shipped gate yet): `baxter_usage.py
--queue` still accepts a bare task string from any caller. The PRD requirement binds only
work routed through here. Making `--queue` itself refuse a PRD-less entry is a hub region
outside this build's touch-set, and Atul's "is the shape right" call on the template is
still open.

CLI:
  python baxter_pm_delegate.py --ask "<raw ask>" [--queue-it] [--priority N]
  python baxter_pm_delegate.py --ask "<vital>" --prd-exempt --queue-it --touch "a,b"
  python baxter_pm_delegate.py --selftest
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import baxter_modelguard as MG    # noqa: E402  the model + MCP policy, in one place
import baxter_prd_template as T   # noqa: E402  the form and its gate

VAULT = Path(os.environ.get("BAXTER_VAULT", r"C:\Users\you\Documents\Baxter"))
UTILS = Path(os.path.dirname(os.path.abspath(__file__)))
LOG = VAULT / ".baxter.log"
USAGE_PY = UTILS / "baxter_usage.py"

PRD_DIR = VAULT / "60-PRDs"          # module-level so the selftest can point it at a tempdir
PM_TIMEOUT = 900                     # one Opus round-trip; a build waits on this
MANAGER_TIMEOUT = 900                # the second round-trip: the manager reads, it does not write
# The first go, plus TWO retries- each carrying the refusals AND the document the PM last
# wrote. Measured on 10th July: a PM round-trip is 3m 13s and a manager 4m 55s, so the
# worst case here is roughly 24 minutes of Opus rather than eight. The planner asked for 4;
# 3 is what it costs, and it is what the exam requires. A PM that STALLS- hands its draft
# back unchanged after the manager's note- is caught on receipt by `_same_document` below,
# so the expensive half of the budget is never spent on a document that is not moving.
MAX_ATTEMPTS = 3

# THE MANAGER'S OWN REJECT LOG. Atul, 9th July 08:46, of the clash delegator: "it is very
# important that security guard periodically rejects things, thats how we know it is
# functional." A gate whose refusals are invisible is a gate nobody can audit, so every
# verdict- greenlight, changes and reject alike- appends one json row here.
REVIEW_LOG = VAULT / ".baxter_pm_reviews.jsonl"

VERDICTS = ("greenlight", "changes", "reject")


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] pm-delegate: {msg}\n")
    except Exception:
        pass


# ---- THE SPAWN ----------------------------------------------------------------------

def _pm_argv():
    """The `claude` argv for the PM instance.

    Every flag comes from `baxter_modelguard.args('heavy')`: Opus, no MCP servers, and the
    never-downgrade class. Hardcoding `['claude', '--model', 'opus']` here is exactly the
    bug modelguard exists to prevent- it is the one place that knows Fable is banned, and
    the one place that will be edited when the tiering changes.

    `-p` is print mode: the instance answers once, on stdout, and exits. A PM that could
    hold a session could hold a tool call, and this one is a writer."""
    return ["claude", "-p"] + MG.args("heavy")


def _spawn_pm(prompt, timeout=PM_TIMEOUT):
    """Run the PM and return the PRD text it wrote. Raises if the spawn fails.

    Stubbed out by both the selftest and the sealed acceptance test- so a green test says
    nothing about whether a real Opus instance answers. That gap is closed by `--dry-argv`
    and by running `--ask` once for real, never by the exam."""
    argv = _pm_argv()
    _log(f"spawning PM: {' '.join(argv)}")
    r = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       cwd=str(VAULT))
    if r.returncode != 0:
        raise RuntimeError(f"the PM spawn exited {r.returncode}: "
                           f"{(r.stderr or '').strip()[:300]}")
    return r.stdout or ""


# ---- THE PROMPT ---------------------------------------------------------------------

_RULES = """
YOU ARE A PRODUCT MANAGER, NOT A BUILDER.
- You write ONE document and nothing else. You never edit code, never run a build,
  never write to the build queue. Your entire output is the filled PRD, in markdown,
  starting with the `# PRD:` line. No preamble, no commentary, no code fences around it.
- Every file or region you name in section 6 you must have READ. A region you have not
  opened and confirmed exists may not be named. Three queue entries on 8th July carried
  invented declarations and each burned a build lane; that is what this document prevents.
- Hub files- utils/baxter_triage.py, utils/baxter_usage.py, utils/baxter_watch.ps1,
  utils/baxter_fast.py, utils/baxter_slash.py- are declared BY REGION, as
  `utils/baxter_triage.py/<function_name>`. A bare hub path is REFUSED: declared whole,
  one task locks the entire queue behind it. If the build genuinely rewrites a hub file
  end to end, write a `solo:` line with the reason instead of a touch-set.
- Never copy a path out of the example. `utils/x.py`, `utils/some_dir` and `@cluster` are
  placeholders and are REFUSED. Name the real files. An `@tag` (`@lane-pump`, `@probe`,
  `@models`) is for adjacency a path cannot express. It must be one of the tags REGISTERED
  in `utils/baxter_clusters.py`- an invented or retired tag is refused at queue time,
  because a tag nothing knows locks against nothing.
- Section 7 is the point of the whole form. `verify:` must be a SINGLE-LINE shell command
  that exits 0 only if the build worked, and it must FAIL against the repo as it stands
  today- a proof that already passes is decoration. A `python -c "..."` source must
  compile. If you cannot state how the build would be proven, the build is not ready:
  say so in section 10 rather than inventing a command.
- Prose sections are for the builder. Sections 3, 5, 6, 7 and 9 are read by a machine and
  will be refused if they are thin: at least one real non-goal, at least five edge cases
  each written `<condition> -> <required behaviour>`, a touch-set or a `solo:` reason, a
  verify command or a verify_assert claim, and a `priority:` / `gated_on:` / `next step:` line.
- `gated_on: atul` if the build acts outward, destroys data, or needs a decision only he
  can make. Otherwise `gated_on: none`. Prose like "do not auto-run" does nothing.

Read the vault and the scripts tree before you write. Then output the document, only the
document, filled to the form below.
""".strip()


def _pm_prompt(ask, errors=None, previous=None):
    """The PM's prompt. On a retry it carries the document the PM itself wrote last time.

    Without `previous` a refused PM has nothing to amend: it is handed a list of complaints
    and a blank form, so it composes a NEW document and a section it had right first time is
    silently lost. That is the 10th-July fault- the `verify:` line vanished on attempt 2 and
    the validator refused what came back.

    The worked example is dropped from a retry ON PURPOSE. It is a full, clean PRD about a
    different build, and a retry already carries two documents- the blank form and the PM's
    own draft. A third would invite the model to amend the wrong one and return a document
    about the wrong build. By the retry the PM has already demonstrated it can produce the
    shape; what it needs now is its own text, not a stranger's.
    """
    parts = [
        "You are writing a PRD for Baxter's build queue.",
        "",
        _RULES,
        "",
        "=== THE RAW ASK, VERBATIM (do not paraphrase it away) ===",
        str(ask).strip(),
        "",
        "=== THE BLANK FORM YOU MUST FILL ===",
        T.render_template(),
    ]
    if not previous:
        parts += [
            "",
            "=== A WORKED EXAMPLE THAT VALIDATES CLEAN (a DIFFERENT build- do not fill this "
            "one in, it is here to show you the shape) ===",
            T.EXAMPLE_PRD,
        ]
    if errors:
        parts += [
            "",
            "=== YOUR PREVIOUS ATTEMPT WAS REFUSED ===",
            "Fix every one of these and return the WHOLE document again:",
            *(f"- {e}" for e in errors),
        ]
    if previous:
        parts += [
            "",
            "=== THE DOCUMENT YOU WROTE LAST TIME- AMEND IT, DO NOT REWRITE IT ===",
            "This is YOUR draft, for THIS ask. It is the only document below that you are",
            "editing. Change the parts the refusals above name, and nothing else: leave every",
            "other section, line and field exactly as you wrote it. A section you had right",
            "and then dropped is a worse answer than the one that was refused. Return the",
            "whole amended document, starting at the `# PRD:` line.",
            "",
            str(previous).strip(),
        ]
    return "\n".join(parts)


# ---- THE PM-MANAGER -----------------------------------------------------------------
# `validate_prd` counts sections, counts edge cases, and hands two declarations to the gates
# the queue itself uses. That is the whole of what a regex can know. It cannot tell whether
# the Problem is real, whether the Evidence `file:line` exists, whether the Solution is the
# right one, or whether the Visualisation is a picture or a paragraph of fog. A PRD can be
# fluent, well-formed, fully machine-valid, and describe a build that should not happen.
#
# So a second Opus reads it and answers one of three words. It is a READER: it never edits the
# document, never queues, never builds. `changes` is the safe default- an unparseable answer, a
# dead spawn, an invented verdict all land there, because the one outcome a broken judge must
# never produce is a greenlight.

_MANAGER_RULES = """
YOU ARE THE PM-MANAGER. A product manager has written the PRD below and a machine has already
validated its SHAPE: the sections are present, the touch-set passes the queue's own gate, the
verify command compiles. None of that tells anyone whether the thing should be built.

You audit what the machine cannot:
- Is the PROBLEM real, and is it stated as a problem rather than as a missing feature?
- Is the EVIDENCE citable? Open the `file:line` it names. A line that does not say what the
  PRD claims it says is a fabrication, and it is the single most common failure here.
- Is the SOLUTION the right one, or the first one? A PRD that never names a discarded
  alternative has not chosen; it has assumed.
- Is the VISUALISATION concrete- the literal output, the literal log row- or is it abstract
  prose? "The user sees improved feedback" is not a visualisation. "Nothing changes on his
  screen" IS one, if it is true.
- Are the EDGE CASES exhaustive for THIS build, or five generic ones copied off the form?
  Name the case it missed.
- Does the TOUCH-SET match the Solution? A file the solution obviously needs and the touch-set
  never names is a lane collision waiting to happen.
- Would the VERIFY command fail against the repo as it stands today, and pass only if the
  build truly worked? A proof that already passes is decoration.

You may read the vault and the scripts tree to check any of it. You write nothing.

ANSWER WITH ONE JSON OBJECT AND NOTHING ELSE:
  {"verdict": "greenlight", "reasons": []}
  {"verdict": "changes", "reasons": ["the evidence at baxter_usage.py:1631 is a docstring, not the guard"]}
  {"verdict": "reject", "reasons": ["this duplicates the queue --edit build already at priority 3"]}

- `greenlight`: build it as written. Nothing is filed unless you say this word.
- `changes`: the ask is sound, the document is not. Your reasons go straight back to the PM,
  which rewrites and returns. Be specific enough to act on- one reason, one defect.
- `reject`: this should NOT be built at all. A duplicate, a solution to a problem nobody has,
  a change that costs more than the fault it fixes. Say so plainly; there is no retry.

You are not a rubber stamp. A PRD you greenlight, a lane will build.
""".strip()


def _manager_prompt(prd_text):
    return "\n".join([
        "You are reviewing a PRD for Baxter's build queue.",
        "",
        _MANAGER_RULES,
        "",
        "=== THE PRD, AS THE PM WROTE IT ===",
        str(prd_text).strip(),
    ])


def _verdict_of(raw):
    """Anything the manager returned -> (verdict, reasons). Never raises.

    A dict comes straight from a stub; a string is what a real spawn hands back, and the model
    is asked for bare json but will sometimes wrap it in prose or a code fence, so the LAST
    balanced `{...}` carrying a `verdict` key wins. Everything unrecognised is `changes`: a
    judge that cannot be understood has not approved anything.
    """
    d = raw if isinstance(raw, dict) else None
    if d is None and isinstance(raw, str):
        for m in reversed(list(re.finditer(r"\{.*?\}", raw, re.S))):
            try:
                cand = json.loads(m.group(0))
            except Exception:
                continue
            if isinstance(cand, dict) and "verdict" in cand:
                d = cand
                break
    if not isinstance(d, dict):
        return "changes", ["the PM-manager's answer could not be parsed as a verdict"]
    v = str(d.get("verdict", "")).strip().lower()
    reasons = [str(r).strip() for r in (d.get("reasons") or []) if str(r).strip()]
    if v not in VERDICTS:
        return "changes", reasons or [f"the PM-manager returned an unknown verdict {v!r}"]
    return v, reasons


def _spawn_manager(prd_text, timeout=MANAGER_TIMEOUT):
    """Run the manager and return its verdict dict. NEVER raises.

    A raising judge would have to be caught by every caller, and the tempting catch is the
    wrong one: `except: proceed`. So the failure is folded in here, and it is `changes`. An
    unreachable model cannot greenlight a build.
    """
    argv = _pm_argv()
    _log(f"spawning PM-manager: {' '.join(argv)}")
    try:
        r = subprocess.run(argv, input=_manager_prompt(prd_text), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=str(VAULT))
        if r.returncode != 0:
            return {"verdict": "changes",
                    "reasons": [f"the PM-manager spawn exited {r.returncode}: "
                                f"{(r.stderr or '').strip()[:200]}"]}
        v, reasons = _verdict_of(r.stdout or "")
        return {"verdict": v, "reasons": reasons}
    except Exception as e:
        return {"verdict": "changes", "reasons": [f"the PM-manager spawn failed: {e}"]}


def _log_review(ask, verdict, reasons, attempt):
    """One json row per verdict, appended to REVIEW_LOG. Read at call time, so the selftest
    and the sealed exam can point it at a tempdir. A logging failure never fails a build."""
    row = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "ask": str(ask)[:300],
        "attempt": attempt,
        "verdict": verdict,
        "reasons": reasons,
    }
    try:
        p = Path(REVIEW_LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        _log(f"could not append to the review log: {e}")


# ---- THE QUEUE ----------------------------------------------------------------------

QUEUE_OK = 0    # the ONE accepted outcome. Everything else is a failure of some KIND.

# `baxter_usage.py --queue`'s exit vocabulary, as its main() speaks it (11th July):
# 0 accepted, 1 you typed it wrong, 2 the guard refused you, 3 the governor blocked you,
# 4 the guard itself broke. Flattening those five into the one word "refused" is the exact
# disguise the park fix removed from main(), and it survived up here: a ModuleNotFoundError
# came back to the caller wearing the PRD gate's name.
_QUEUE_OUTCOMES = {
    1: ("the queue call was MISTYPED- --queue would not parse it", False),
    2: ("the guard REFUSED the entry", False),
    3: ("the governor BLOCKED the entry", False),
    4: ("the queue call CRASHED. This is not the PRD gate and not a refusal", True),
}


def _queue_outcome(rc):
    """(headline, crashed) for an exit code. Keyed on "not an accepted outcome", NEVER on a
    list of codes: the incident was an import error and the next one will not be, so a code
    nobody has defined yet is reported as UNKNOWN and treated as a crash- never bucketed into
    a refusal it may have nothing to do with."""
    if rc is None:
        return ("the queue call CRASHED before --queue ran at all", True)
    if rc in _QUEUE_OUTCOMES:
        return _QUEUE_OUTCOMES[rc]
    if rc < 0:
        return (f"the queue call was KILLED by signal {-rc}", True)
    return (f"--queue returned an UNKNOWN outcome ({rc})- this code has no meaning here", True)


class QueueCallFailed(RuntimeError):
    """`--queue` did not accept the entry, and the exit code says WHY.

    `.returncode` is the code (None if the process could not be run at all), `.crashed` is
    True when the failure is a fault rather than a verdict, and `.report` is the whole thing
    in his register. The exception's message IS the report, so a caller that merely prints it
    still tells the truth about which thing broke."""

    def __init__(self, returncode, detail="", task="", note=""):
        self.returncode = returncode
        self.detail = str(detail or "").strip()
        headline, self.crashed = _queue_outcome(returncode)
        ran = "did not run" if returncode is None else f"exited {returncode}"
        # The detail is DATA- printed, never interpolated into a format string, never _say'd.
        lines = [f"Sir, {headline}.",
                 f"         --queue {ran}: {self.detail or '(it said nothing at all)'}"]
        if note:
            lines.append(f"         the PRD stands at {note}")
        lines.append(f"         the ask is NOT queued{': ' + str(task)[:100] if task else '.'}")
        if self.crashed:
            lines.append("         Fix the fault, then queue it again.")
        self.report = "\n".join(lines)
        super().__init__(self.report)


def _exit_for(e):
    """The delegate's own exit code for a failed queue call: 4 for a fault, and otherwise the
    code `--queue` itself gave, so the two ends of the wire keep one vocabulary."""
    if e.crashed:
        return 4
    return e.returncode if isinstance(e.returncode, int) and e.returncode > 0 else 2


def _enqueue(task, next_step, note="", priority=None, gate="", touch=None, solo=False,
             verify="", verify_assert="", prd_exempt=False):
    """Shell out to `baxter_usage.py --queue`. Kept as a module-level name with keyword
    args so the selftest and the sealed exam can stub it and prove that an invalid PRD
    never reaches the live queue file.

    `prd_exempt` is the one way past `--queue`'s PRD gate, and it exists for `queue_exempt()`
    alone. The governor appends every use of it to `.baxter_rejects.jsonl`, so a bypass can be
    counted rather than merely regretted."""
    argv = [sys.executable, str(USAGE_PY), "--queue", task, next_step]
    if prd_exempt:
        argv += ["--prd-exempt"]
    if note:
        argv += ["--note", str(note)]
    if priority:
        argv += ["--priority", str(priority)]
    if gate:
        argv += ["--gate", gate]
    if touch:
        argv += ["--touch", ",".join(touch)]
    elif solo:
        argv += ["--solo"]
    if verify:
        argv += ["--verify", verify]
    elif verify_assert:
        argv += ["--verify-assert", verify_assert]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    except OSError as e:
        # The interpreter path is wrong, or the file is gone: a fault, not a verdict.
        _log(f"--queue could not be run at all: {e.__class__.__name__}: {e}")
        raise QueueCallFailed(None, f"{e.__class__.__name__}: {e}", task=task, note=note) from e
    _log(f"--queue exited {r.returncode}: {(r.stdout or r.stderr or '').strip()[:200]}")
    if r.returncode != QUEUE_OK:
        raise QueueCallFailed(r.returncode, (r.stderr or r.stdout or "").strip()[:300],
                              task=task, note=note)
    return r.stdout


# ---- WRITING THE DOCUMENT -----------------------------------------------------------

_SLUG_BAD = re.compile(r'[<>:"/\\|?*]')


def _norm(text):
    """The document as its meaning, not its bytes: every line right-stripped, the whole thing
    stripped. A PM that pads its lines and appends a blank one has amended nothing."""
    return "\n".join(ln.rstrip() for ln in str(text).splitlines()).strip()


def _same_document(a, b):
    """Did the PM hand back the document it was asked to amend? Compared normalised, never raw:
    a raw-byte guard is evaded by one trailing space."""
    return _norm(a) == _norm(b)


_NONTASK_MIN_ALNUM = 3   # below this many letters/digits, no PM could read a build out of it


def _is_nontask(ask):
    """Is this ask objectively content-free- empty, whitespace, or punctuation with no
    buildable substance? True refuses it before any spawn; the two-Opus round-trip is the
    most expensive single spend in the flow and this one is spent to learn nothing.

    Machine-detectable only: a non-string, an empty/whitespace string, or one carrying fewer
    than `_NONTASK_MIN_ALNUM` alphanumeric characters. A fluent sentence that names no
    deliverable is NOT caught here- judging that is the PM-manager's `reject` to make."""
    if not isinstance(ask, str) or not ask.strip():
        return True
    return sum(c.isalnum() for c in ask) < _NONTASK_MIN_ALNUM


def _prd_path(title, when=None):
    when = when or datetime.now()
    slug = _SLUG_BAD.sub("", title).strip().rstrip(".")[:90] or "untitled"
    return Path(PRD_DIR) / f"{when:%Y-%m-%d} - {slug}.md"


def write_prd(ask, queue_it=False, priority=None, timeout=PM_TIMEOUT,
              triviality_check=False, manager_timeout=MANAGER_TIMEOUT):
    """Spawn the PM, validate its PRD, put it to the manager, file it. Returns a path or None.

    Two gates, in this order, and NOTHING is written to `60-PRDs/` or to the queue until both
    are passed. The validator refuses a malformed document; the PM-manager refuses a
    well-formed document that describes the wrong build. Only `greenlight` files anything.

    None means REFUSED, and the reason is in `.baxter.log`, with the verdict in
    `.baxter_pm_reviews.jsonl`. A caller that treats None as "queued anyway" has defeated the
    whole layer.

    A PM that returns the document unchanged after the MANAGER asked for changes has stalled:
    it is stopped on receipt, before the manager pays 4m 55s to reach a verdict it has already
    given. The same document unchanged after a VALIDATOR refusal is not a stall- a regex costs
    nothing to run twice- and it still spends every attempt.

    `triviality_check` runs the PRD's own verify command against the current repo and
    refuses a PRD whose proof already passes. It is OFF by default: the command is written
    by a model and would run unattended against the live vault. Turn it on when a human is
    watching the output."""
    # THE NON-TASK GUARD, first statement and before any spawn: a content-free ask never
    # earns a PM. One `non-task` row, one log line, and None- no PM spawn, no manager spawn.
    if _is_nontask(ask):
        _log_review(ask, "non-task", ["the ask carries no buildable content"], 0)
        _log(f"NON-TASK {ask!r}: the ask carries no buildable content- refused before any "
             f"spawn, nothing filed")
        return None

    errors, text, warnings, approved = None, "", [], False
    last_refusal = None                  # "validator" | "manager"- WHO sent the last one back
    for attempt in range(1, MAX_ATTEMPTS + 1):
        previous = text                  # captured BEFORE the spawn overwrites it; "" on attempt 1
        try:
            # `text` still holds the document the PM wrote last time round- malformed if the
            # validator refused it, machine-valid if only the manager did. Either way the PM
            # amends that, rather than composing a new one. On attempt 1 it is `""`, so the
            # prompt carries no previous document and the worked example stays in.
            text = _spawn_pm(_pm_prompt(ask, errors, previous=previous or None), timeout=timeout)
        except Exception as e:
            _log(f"REFUSED {ask!r}: the PM spawn failed on attempt {attempt}: {e}")
            return None

        # THE STALL GUARD, and it sits here on purpose: after the spawn, before either gate.
        # Only the manager's path. A document the validator refused and that comes back
        # unchanged costs one more regex, and the budget is its to spend.
        if last_refusal == "manager" and previous and _same_document(text, previous):
            reasons = ["the PM returned the document unchanged after the manager asked for "
                       "changes", *(errors or [])]
            _log_review(ask, "stalled", reasons, attempt)
            _log(f"STALLED {ask!r} on attempt {attempt}/{MAX_ATTEMPTS}: the PM handed back the "
                 f"document it was asked to amend, unchanged. The manager is not asked to read "
                 f"it twice; nothing filed, nothing queued")
            return None

        errors, warnings = T.validate_prd(text, triviality_check=triviality_check)
        if errors:
            last_refusal = "validator"
            _log(f"attempt {attempt}/{MAX_ATTEMPTS} refused by the validator for {ask!r}: "
                 f"{'; '.join(errors)}")
            continue

        # The shape is right. Now: should it be built at all?
        verdict, reasons = _verdict_of(_spawn_manager(text, timeout=manager_timeout))
        _log_review(ask, verdict, reasons, attempt)

        if verdict == "greenlight":
            approved = True
            break
        if verdict == "reject":
            _log(f"REJECTED by the PM-manager: {ask!r}- {'; '.join(reasons) or 'no reason given'}"
                 f". Nothing filed, nothing queued, and there is no retry")
            return None
        # `changes`: the manager's reasons ARE the next attempt's refusal list. The PM sees
        # them in the same block it would see a validator's, so one retry path serves both.
        last_refusal = "manager"
        errors = reasons or ["the PM-manager asked for changes but gave no reasons"]
        _log(f"attempt {attempt}/{MAX_ATTEMPTS} sent back by the PM-manager for {ask!r}: "
             f"{'; '.join(errors)}")

    if not approved:
        _log(f"REFUSED {ask!r} after {MAX_ATTEMPTS} attempts- nothing filed, nothing queued")
        return None

    for w in warnings:
        _log(f"warn on {ask!r}: {w}")

    fields = T.queue_fields(text)
    path = _prd_path(fields["task"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _log(f"PRD written: {path}")

    if queue_it:
        # The document is already on disk (line above), so a failed queue call leaves a PRD
        # with nothing behind it. Say so in the log, name the KIND of failure, and re-raise:
        # a caller that gets a path back has been told the ask is queued, and it would not be.
        try:
            _enqueue(task=fields["task"],
                     next_step=fields["next_step"],
                     note=str(path),
                     priority=priority or fields["priority"],
                     gate=fields["gate"],
                     touch=fields["touch"],
                     solo=bool(fields["solo"] and not fields["touch"]),
                     verify=fields["verify"],
                     verify_assert=fields["verify_assert"])
        except QueueCallFailed as e:
            _log(f"NOT QUEUED ({'crash' if e.crashed else 'refusal'}, exit {e.returncode}): "
                 f"{fields['task']!r}- the PRD stands at {path}\n{e.report}")
            raise
        _log(f"queued from PRD: {fields['task']!r}")
    return str(path)


def queue_exempt(task, next_step, touch=None, solo=False, priority=None, gate="",
                 verify="", verify_assert=""):
    """The vitals escape hatch the design note demands: file a build with NO PRD.

    Every use is logged to `.baxter.log`, loudly, so the bypass is never silent- and, now the
    gate lives in `--queue` itself, to `.baxter_rejects.jsonl` beside the clash skips and the
    human-gate holds. Latency is the reason it exists- an Opus round-trip before a CoC-bot fix
    is filed would be absurd."""
    _log(f"PRD-EXEMPT: {task!r} filed with no PRD (the bypass was used, on purpose)")
    try:
        return _enqueue(task=task, next_step=next_step, priority=priority, gate=gate,
                        touch=touch, solo=solo, verify=verify, verify_assert=verify_assert,
                        prd_exempt=True)
    except QueueCallFailed as e:
        # The line above says FILED. It is the vitals path, so it fires when something is
        # already broken: correct the log rather than leave a vital recorded as queued.
        _log(f"PRD-EXEMPT NOT QUEUED ({'crash' if e.crashed else 'refusal'}, "
             f"exit {e.returncode}): {task!r}\n{e.report}")
        raise


# ---- SELFTEST -----------------------------------------------------------------------

def selftest():
    """Nothing spawns, nothing queues, nothing posts. A selftest that launches Opus or
    writes `.baxter_task_queue.json` is a FAILED selftest even if it prints OK.

    `_spawn_manager` is stubbed alongside `_spawn_pm` for that reason. It was the newest
    outward path, and an unstubbed one would have fired a real 900-second Opus round-trip
    from a selftest- see [[selftests-stub-every-outward-path]], where a lane's selftest once
    posted a real alert to Discord.
    """
    global PRD_DIR, REVIEW_LOG, _spawn_pm, _spawn_manager, _enqueue
    import tempfile

    real_spawn, real_enqueue, real_dir, real_log = _spawn_pm, _enqueue, PRD_DIR, globals()["_log"]
    real_mgr, real_review = _spawn_manager, REVIEW_LOG
    real_subprocess = subprocess          # section 8 stubs it to drive --queue's exit codes
    PRD_DIR = Path(tempfile.mkdtemp(prefix="baxter_prd_"))
    REVIEW_LOG = PRD_DIR / "reviews.jsonl"
    globals()["_log"] = lambda *a, **k: None
    ok = lambda *a, **k: {"verdict": "greenlight", "reasons": []}    # noqa: E731

    def verdicts():
        if not Path(REVIEW_LOG).exists():
            return []
        return [json.loads(l)["verdict"]
                for l in Path(REVIEW_LOG).read_text(encoding="utf-8").splitlines() if l.strip()]

    def prds():
        return sorted(p.name for p in PRD_DIR.glob("*.md"))

    try:
        # 1. The argv is modelguard's, not a hardcoded string. Opus, never Fable.
        argv = _pm_argv()
        joined = " ".join(argv).lower()
        assert argv[0] == "claude", argv
        assert "opus" in joined, joined
        assert "fable" not in joined, joined
        assert "--strict-mcp-config" in argv, "the PM must not inherit MCP servers"

        # 2. A greenlit PRD writes a real file and queues carrying its touch-set + verify.
        calls = []
        globals()["_spawn_pm"] = lambda *a, **k: T.EXAMPLE_PRD
        globals()["_spawn_manager"] = ok
        globals()["_enqueue"] = lambda **k: calls.append(k)
        p = write_prd("selftest ask", queue_it=True)
        assert p and Path(p).exists(), p
        assert Path(p).parent == PRD_DIR, "the PRD must land in PRD_DIR, not the live vault"
        assert len(calls) == 1, calls
        c = calls[0]
        assert "utils/baxter_prd_template.py" in c["touch"], c
        assert c["priority"] == 1 and c["gate"] == "", c
        assert c["verify"].startswith("python -c"), c
        assert c["note"] == p, "the PRD path must go in as --note, so it can be revised in place"
        assert c["next_step"], c
        assert verdicts() == ["greenlight"], verdicts()

        # 3. An INVALID PRD reaches neither the queue nor the disk. Every attempt is spent,
        #    then nothing is filed. The manager is never even consulted: a malformed document
        #    has nothing to judge.
        calls.clear()
        tries, judged = [], []
        globals()["_spawn_pm"] = lambda *a, **k: (tries.append(1), T.render_template())[1]
        globals()["_spawn_manager"] = lambda *a, **k: (judged.append(1), ok())[1]
        before = prds()
        assert write_prd("bad ask", queue_it=True) is None, "a blank PRD must not be filed"
        assert not calls, "an invalid PRD reached the queue"
        assert prds() == before, "an invalid PRD wrote a file"
        assert len(tries) == MAX_ATTEMPTS, \
            f"expected {MAX_ATTEMPTS} attempts, got {len(tries)}"
        assert not judged, "the manager was asked to judge a PRD the validator had refused"

        # 4. The retry carries the refusals back to the PM- AND the document the PM itself
        #    wrote, so it amends that rather than composing a new one. Asserted on the prompt
        #    string the stub is actually handed, never on the source of `_pm_prompt`: the seam
        #    that matters is what reaches the model.
        seen = []
        first_draft = T.render_template().replace(
            "## 2. Problem", "- Selftest sentinel: 9e1a4c\n\n## 2. Problem", 1)

        def _second_time_lucky(prompt, *a, **k):
            seen.append(prompt)
            return first_draft if len(seen) == 1 else T.EXAMPLE_PRD
        globals()["_spawn_pm"] = _second_time_lucky
        globals()["_spawn_manager"] = ok
        calls.clear()
        assert write_prd("retry ask", queue_it=True), "a valid retry must be filed"
        assert "PREVIOUS ATTEMPT WAS REFUSED" in seen[1], "the retry did not carry the errors"
        assert "verif" in seen[1].lower(), seen[1][-400:]
        assert "AMEND IT, DO NOT REWRITE IT" in seen[1], "the retry never ordered an amendment"
        assert "Selftest sentinel: 9e1a4c" in seen[1], \
            "the retry prompt did not carry the PM's own previous document"
        assert first_draft.strip() in seen[1], "the previous document was not carried verbatim"
        assert "Selftest sentinel: 9e1a4c" not in seen[0], \
            "attempt 1 carried a previous document there was none of"
        assert "AMEND IT, DO NOT REWRITE IT" not in seen[0], \
            "attempt 1 was told to amend a document it had not written"
        assert "A WORKED EXAMPLE" in seen[0], "attempt 1 lost the worked example"
        assert "A WORKED EXAMPLE" not in seen[1], \
            "the retry still carries the example: three full PRDs, and the PM may amend the wrong one"
        assert len(calls) == 1

        # 5. A dead PM spawn refuses cleanly- it never queues on a guess.
        calls.clear()

        def _boom(*a, **k):
            raise RuntimeError("claude is not on PATH")
        globals()["_spawn_pm"] = _boom
        assert write_prd("doomed ask", queue_it=True) is None
        assert not calls, "a failed spawn must not queue anything"

        # 6. queue_it=False writes the document and queues nothing.
        calls.clear()
        globals()["_spawn_pm"] = lambda *a, **k: T.EXAMPLE_PRD
        assert write_prd("no-queue ask", queue_it=False)
        assert not calls, "queue_it=False must not queue"

        # ---- THE MANAGER GATE. A machine-valid PRD is not a fileable one. ----
        # `changes` files nothing and queues nothing. This PM hands the SAME document back, so
        # it has stalled: caught on receipt at attempt 2, and the manager never reads it twice.
        calls.clear()
        Path(REVIEW_LOG).unlink()
        before, prompts = prds(), []

        def _watch_pm(prompt, *a, **k):
            prompts.append(prompt)
            return T.EXAMPLE_PRD
        globals()["_spawn_pm"] = _watch_pm
        globals()["_spawn_manager"] = lambda *a, **k: {
            "verdict": "changes", "reasons": ["the evidence line was never checked"]}
        assert write_prd("changes ask", queue_it=True) is None, "a PRD sent back was filed anyway"
        assert prds() == before, "a PRD the manager refused reached 60-PRDs"
        assert not calls, "a PRD the manager refused reached the build queue"
        assert len(prompts) == 2, f"a stalled PM was spawned {len(prompts)} times, expected 2"
        assert "the evidence line was never checked" in prompts[1], \
            "the manager's reasons never reached the PM's retry"
        assert "AMEND IT, DO NOT REWRITE IT" in prompts[1], \
            "the manager's path never showed the PM its own machine-valid document"
        assert verdicts() == ["changes", "stalled"], verdicts()

        # ...and a PM that AMENDS each round is not a stalled one: every attempt is spent.
        calls.clear()
        Path(REVIEW_LOG).unlink()
        prompts.clear()
        globals()["_spawn_pm"] = lambda *a, **k: (
            prompts.append(1),
            T.EXAMPLE_PRD.replace("\n## 6. Edge cases",
                                  f"\n- Amended: round {len(prompts)}\n\n## 6. Edge cases", 1))[1]
        assert write_prd("amending ask", queue_it=True) is None
        assert len(prompts) == MAX_ATTEMPTS, \
            f"an amending PM was stopped after {len(prompts)} attempt(s)"
        assert verdicts() == ["changes"] * MAX_ATTEMPTS, verdicts()

        # `reject` files nothing, queues nothing, and does NOT retry: a rejected ask does not
        # improve by being rewritten. One PM spawn, one verdict, done.
        calls.clear()
        Path(REVIEW_LOG).unlink()
        prompts.clear()
        globals()["_spawn_manager"] = lambda *a, **k: {
            "verdict": "reject", "reasons": ["this duplicates the queue --edit build"]}
        assert write_prd("rejected ask", queue_it=True) is None, "a rejected PRD was filed"
        assert prds() == before, "a rejected PRD reached 60-PRDs"
        assert not calls, "a rejected PRD reached the build queue"
        assert len(prompts) == 1, f"a reject must not be retried, got {len(prompts)} PM spawn(s)"
        assert verdicts() == ["reject"], verdicts()

        # A judge that cannot be understood has approved NOTHING. Prose, an invented verdict,
        # a dead spawn and a thrown exception all read as `changes`- never as a greenlight.
        for raw in ("looks good to me, ship it",
                    '{"verdict": "approve"}',
                    '{"verdict": "GREENLIGHT!!"}',
                    "",
                    {"reasons": ["no verdict key at all"]}):
            v, _ = _verdict_of(raw)
            assert v == "changes", (raw, v)
        assert _verdict_of('{"verdict": "greenlight", "reasons": []}')[0] == "greenlight"
        assert _verdict_of({"verdict": "reject", "reasons": ["dup"]}) == ("reject", ["dup"])
        # ...including json buried in prose, which is what a real spawn returns.
        v, why = _verdict_of('Here is my review.\n```json\n{"verdict": "changes", '
                             '"reasons": ["section 5 is fog"]}\n```\nHope that helps.')
        assert (v, why) == ("changes", ["section 5 is fog"]), (v, why)

        calls.clear()
        Path(REVIEW_LOG).unlink()
        globals()["_spawn_pm"] = lambda *a, **k: T.EXAMPLE_PRD
        globals()["_spawn_manager"] = lambda *a, **k: "the model died mid-sentence"
        assert write_prd("garbled ask", queue_it=True) is None, \
            "an unparseable verdict was read as a greenlight"
        assert not calls and prds() == before
        assert verdicts() == ["changes", "stalled"], verdicts()

        # A whitespace-padded resubmission is the same document, so the guard normalises rather
        # than comparing bytes. And `stalled` is a row this module writes ABOUT the PM- never a
        # word the manager may say and be honoured.
        calls.clear()
        Path(REVIEW_LOG).unlink()
        prompts.clear()
        globals()["_spawn_pm"] = lambda *a, **k: (
            prompts.append(1),
            T.EXAMPLE_PRD if len(prompts) == 1 else
            "\n".join(ln + "   " for ln in T.EXAMPLE_PRD.splitlines()) + "\n\n")[1]
        globals()["_spawn_manager"] = lambda *a, **k: {
            "verdict": "changes", "reasons": ["section 5 is fog"]}
        assert write_prd("padded ask", queue_it=True) is None
        assert len(prompts) == 2, f"a padded resubmission escaped the guard ({len(prompts)})"
        assert verdicts() == ["changes", "stalled"], verdicts()
        assert "stalled" not in VERDICTS, "a manager that says `stalled` must not be honoured"
        assert _verdict_of('{"verdict": "stalled"}')[0] == "changes"

        # A content-free ask is refused BEFORE any spawn: empty, whitespace, None and
        # punctuation all cost zero PM round-trips, while a real short ask still spawns.
        calls.clear()
        Path(REVIEW_LOG).unlink()
        spawned = []
        globals()["_spawn_pm"] = lambda *a, **k: (spawned.append(1), T.EXAMPLE_PRD)[1]
        globals()["_spawn_manager"] = ok
        for dead in ("", "   ", None, "----", "..."):
            assert write_prd(dead, queue_it=True) is None, f"a non-task was filed: {dead!r}"
        assert not spawned, "a non-task reached a PM spawn"
        assert not calls, "a non-task reached the build queue"
        assert prds() == before, "a non-task wrote a PRD file"
        assert verdicts() == ["non-task"] * 5, verdicts()
        # ...and a short-but-real ask is NOT a non-task: it spawns and files as normal.
        assert write_prd("go build the coc pause thing", queue_it=True), \
            "a real short ask was refused as a non-task"
        assert spawned, "a real ask never reached the PM"

        # 7. The exempt path is a real bypass, it is loud, and it consults nobody.
        calls.clear()
        globals()["_spawn_manager"] = lambda *a, **k: 1 / 0
        queue_exempt("a vital fix", "step one", touch=["utils/coc_bot/"], priority=1)
        assert calls and calls[0]["touch"] == ["utils/coc_bot/"], calls

        # 8. THE EXIT CODE IS READ, NOT FLATTENED (11th July). The REAL `_enqueue` runs here-
        #    only `subprocess` under it is stubbed- so this grades the function, not a stand-in.
        globals()["_enqueue"] = real_enqueue

        def _exiting(code, err="", out=""):
            class _S:
                @staticmethod
                def run(*a, **k):
                    return type("R", (), {"returncode": code, "stdout": out, "stderr": err})()
            globals()["subprocess"] = _S

        def _failure(code, err="", out=""):
            _exiting(code, err, out)
            try:
                _enqueue("t", "s", touch=["utils/a.py"], note="60-PRDs/x.md")
            except QueueCallFailed as e:
                return e
            raise AssertionError(f"exit {code} did not raise")

        # 0 is untouched: stdout comes back, nothing raises.
        _exiting(0, out="queued at position 2 of 7\n")
        assert _enqueue("t", "s", touch=["utils/a.py"]) == "queued at position 2 of 7\n"

        # 4- the park crashed. It says CRASH, it carries the class, and it never says refused.
        e4 = _failure(4, "Sir, the park CRASHED. ModuleNotFoundError: No module named 'baxter_name'")
        assert e4.crashed and e4.returncode == 4, (e4.crashed, e4.returncode)
        assert "refused" not in e4.report.lower(), e4.report
        assert "CRASH" in e4.report and "ModuleNotFoundError" in e4.report, e4.report
        assert "60-PRDs/x.md" in e4.report, "the crash report lost the PRD still on disk"
        assert _exit_for(e4) == 4, _exit_for(e4)

        # 2- a genuine policy refusal STILL says refused. A build that made everything a crash
        #    would pass a lazy "no refused anywhere" check and be just as wrong.
        e2 = _failure(2, "near-duplicate of 13b1cfe7")
        assert not e2.crashed and e2.returncode == 2, (e2.crashed, e2.returncode)
        assert "REFUSED" in e2.report and "13b1cfe7" in e2.report, e2.report
        assert "CRASH" not in e2.report.upper(), e2.report
        assert _exit_for(e2) == 2

        # 3- the governor, not the guard.
        e3 = _failure(3, "big tasks are paused: usage at 84%")
        assert not e3.crashed and "governor" in e3.report.lower(), e3.report
        assert "refused" not in e3.report.lower(), e3.report
        assert _exit_for(e3) == 3

        # 1- mistyped.
        e1 = _failure(1, "--touch is required")
        assert not e1.crashed and "MISTYPED" in e1.report, e1.report

        # 5- a code nobody has defined yet. UNKNOWN, named, and never bucketed into refused.
        #    This is the assertion that stops the fix being keyed to a list of codes.
        e5 = _failure(5)
        assert e5.crashed and "UNKNOWN" in e5.report and "5" in e5.report, e5.report
        assert "refused" not in e5.report.lower(), e5.report
        assert "(it said nothing at all)" in e5.report, "a silent failure reported nothing"
        assert _exit_for(e5) == 4

        # A signal, and a subprocess that cannot be run at all: faults, not verdicts.
        eneg = _failure(-9)
        assert eneg.crashed and "signal 9" in eneg.report, eneg.report

        class _Dead:
            @staticmethod
            def run(*a, **k):
                raise OSError(2, "The system cannot find the file specified")
        globals()["subprocess"] = _Dead
        try:
            _enqueue("t", "s", touch=["utils/a.py"])
            raise AssertionError("a dead subprocess did not raise QueueCallFailed")
        except QueueCallFailed as e:
            assert e.crashed and e.returncode is None, (e.crashed, e.returncode)
            assert "FileNotFoundError" in e.report, e.report      # the CLASS, not "OSError"
            assert "refused" not in e.report.lower(), e.report

        # ...and the callers. write_prd leaves the PRD on disk and raises rather than handing
        # back a path for an ask that was never queued; the exempt vitals path raises too, and
        # main() reports the crash and returns 4 instead of tracebacking or printing "queued".
        globals()["_spawn_pm"] = lambda *a, **k: T.EXAMPLE_PRD
        globals()["_spawn_manager"] = ok
        _exiting(4, "Sir, the park CRASHED. ImportError: baxter_name")
        try:
            write_prd("crashing ask", queue_it=True)
            raise AssertionError("write_prd swallowed a crashed queue call")
        except QueueCallFailed as e:
            assert e.crashed, e.report
        assert prds(), "write_prd deleted the PRD it had already written"
        try:
            queue_exempt("a vital fix", "step one", touch=["utils/coc_bot/"])
            raise AssertionError("queue_exempt swallowed a crashed queue call")
        except QueueCallFailed as e:
            assert e.crashed, e.report

        import contextlib as _ctx
        import io as _io
        for code, want in ((4, 4), (2, 2)):
            _exiting(code, "Sir, the park CRASHED. ImportError: baxter_name"
                     if code == 4 else "near-duplicate of 13b1cfe7")
            out, err = _io.StringIO(), _io.StringIO()
            with _ctx.redirect_stdout(out), _ctx.redirect_stderr(err):
                rc = main(["--ask", "a vital fix", "--prd-exempt", "--queue-it",
                           "--touch", "utils/coc_bot/"])
            assert rc == want, (code, rc)
            assert "queued WITHOUT a PRD" not in out.getvalue(), out.getvalue()
            assert ("CRASH" in err.getvalue()) == (code == 4), err.getvalue()
    finally:
        globals()["_spawn_pm"], globals()["_enqueue"] = real_spawn, real_enqueue
        globals()["_spawn_manager"] = real_mgr
        globals()["subprocess"] = real_subprocess
        PRD_DIR, REVIEW_LOG, globals()["_log"] = real_dir, real_review, real_log

    print("baxter_pm_delegate selftest OK: the PM argv is modelguard's (opus, no MCP, no "
          "Fable); a greenlit PRD files and queues with its touch-set; an invalid one is "
          "never even shown to the manager; a retry carries the PM's own previous document "
          "verbatim and orders an amendment, while attempt 1 carries the worked example and "
          f"no draft; an amending PM spends all {MAX_ATTEMPTS} attempts on `changes` while a "
          "PM that hands the same document back- padded with whitespace or not- is STALLED at "
          "attempt 2 and the manager never reads it twice; `reject` does not retry at all; and "
          "prose, an invented verdict, `stalled` from the manager's own mouth or a dead judge "
          "all read as `changes`, so nothing but a greenlight ever reaches a lane. And "
          "`--queue`'s exit code is READ, not flattened: 4 and an undefined 5 are reported as a "
          "CRASH and an UNKNOWN outcome and never say refused, 2 still says REFUSED, 3 names the "
          "governor, and a crashed call raises out of write_prd and the vitals bypass alike "
          "rather than printing `queued`")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="delegate the PRD to a PM instance, then file it")
    ap.add_argument("--ask", help="the raw ask, in Atul's words")
    ap.add_argument("--queue-it", action="store_true", help="file the validated PRD into the queue")
    ap.add_argument("--priority", type=int, help="override the priority the PRD asks for")
    ap.add_argument("--triviality", action="store_true",
                    help="also RUN the PRD's verify command and refuse it if it already passes")
    ap.add_argument("--prd-exempt", action="store_true",
                    help="vitals bypass: queue with NO PRD. Every use is logged to .baxter.log")
    ap.add_argument("--next-step", default="", help="with --prd-exempt: the first step")
    ap.add_argument("--touch", default="", help="with --prd-exempt: comma-separated touch-set")
    ap.add_argument("--solo", action="store_true", help="with --prd-exempt: no honest touch-set")
    ap.add_argument("--verify", default="", help="with --prd-exempt: the proof command")
    ap.add_argument("--dry-argv", action="store_true", help="print the PM argv and exit")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.dry_argv:
        print(" ".join(_pm_argv()))
        return 0
    if not a.ask:
        ap.print_help()
        return 1

    if a.prd_exempt:
        if not a.queue_it:
            print("--prd-exempt only means anything with --queue-it", file=sys.stderr)
            return 1
        touch = [t.strip() for t in a.touch.split(",") if t.strip()]
        if not touch and not a.solo:
            print("--prd-exempt still needs --touch or --solo", file=sys.stderr)
            return 2
        try:
            queue_exempt(a.ask, a.next_step or "start", touch=touch, solo=a.solo,
                         priority=a.priority, verify=a.verify)
        except QueueCallFailed as e:
            # A vital that was never queued may never be reported as queued.
            print(e.report, file=sys.stderr)
            return _exit_for(e)
        print(f"queued WITHOUT a PRD (exempt): {a.ask!r}- logged to .baxter.log")
        return 0

    try:
        path = write_prd(a.ask, queue_it=a.queue_it, priority=a.priority,
                         triviality_check=a.triviality)
    except QueueCallFailed as e:
        print(e.report, file=sys.stderr)
        return _exit_for(e)
    if not path:
        print("REFUSED: the PM's PRD did not validate. Nothing filed, nothing queued- "
              "see .baxter.log", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
