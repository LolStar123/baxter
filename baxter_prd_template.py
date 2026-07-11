#!/usr/bin/env python3
"""The PRD template for the build queue, and the machine that refuses a bad one.

Atul, 9th July 09:47: "when something is judged worth building, a separate Claude
instance acts as product manager and fills out a rigorous PRD before the item is
filed". The design note is `50-Research/PRD template for the build queue - first pass.md`;
this module is that note made enforceable.

The queue's failure mode is UNDER-SPECIFICATION, and prose cannot catch it. Three
entries on 8th July carried invented declarations: a touch-set of `@lanes` for a task
about a role icon, a region `utils/baxter_triage.py/model_select` that does not exist,
and a thinner duplicate of a build already at priority 1. So every check here is a
MACHINE check on the two fields the machinery actually consumes:

  * the touch-set  -> run through `baxter_usage.touch_problems()`, the same gate
                      `--queue` uses. A placeholder, a bare hub file or an orphan path
                      is refused here for the same reason it is refused there.
  * the verify gate -> run through `baxter_verify.vet_verify_cmd()`, so a `python -c`
                      exam that cannot even compile never reaches a lane.

Everything else in the template is prose FOR the builder. It is counted, never read:
eleven sections, one non-goal, five edge cases, one visualisation. A section that cannot be
filled means the build is not ready, and that is the whole point of the form.

CLI:
  python baxter_prd_template.py --print          -> the blank form
  python baxter_prd_template.py --check <file>   -> validate a filled PRD (exit 2 = refused)
  python baxter_prd_template.py --selftest
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import baxter_usage as U          # noqa: E402  touch_problems / HUB_FILES / TOUCH_PLACEHOLDERS
import baxter_verify as V         # noqa: E402  vet_verify_cmd / run_command

VAULT = Path(os.environ.get("BAXTER_VAULT", r"C:\Users\you\Documents\Baxter"))
LOG = VAULT / ".baxter.log"

PRD_DIR = VAULT / "60-PRDs"

# The eleven sections, by number. The heading TEXT is advisory; the NUMBER is the contract,
# so a PM that renames "Solution" to "Solution + UX" is not punished for prose.
#
# 5. Visualisation was inserted on 9th July (Atul's own words for the ask: "user problem,
# solution, visualisation, affected codebase regions, edge cases"). It sits next to the
# Solution because it is the Solution seen from his chair, and inserting it there renumbered
# 5-10 into 6-11. Nothing outside this module and `baxter_pm_delegate` reads a section by
# number, and the one on-disk PRD was migrated in the same commit; if a third reader ever
# appears, it reads `SECTIONS`, never a literal.
SECTIONS = {
    1: "Origin",
    2: "Problem",
    3: "Non-goals",
    4: "Solution",
    5: "Visualisation",
    6: "Edge cases",
    7: "Touch-set",
    8: "Verification",
    9: "Rollback",
    10: "Priority + gate",
    11: "Open questions",
}

# The section numbers the machine actually consumes. Named, so a renumber is one edit here
# and not a hunt through `validate_prd` for a bare `sec.get(9)`.
S_PROBLEM, S_NON_GOALS, S_VISUALISATION = 2, 3, 5
S_EDGES, S_TOUCH, S_VERIFY, S_PRIORITY = 6, 7, 8, 10

MIN_EDGE_CASES = 5
MIN_NON_GOALS = 1
MIN_VISUALISATION = 1
PRIORITY_RANGE = (1, 8)

# How long the pre-build triviality probe may run a PRD's own verify command.
TRIVIALITY_TIMEOUT = 120


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] prd: {msg}\n")
    except Exception:
        pass


# ---- THE TEMPLATE -------------------------------------------------------------------
# Two rules govern the blank form, and the sealed acceptance test enforces both:
#   1. the literal token `verify:` appears in section 7, so the PM can see the field it
#      must fill and a reader can grep for it;
#   2. NOTHING is pre-filled. Every machine field carries an <angle-bracket> placeholder,
#      which `_filled()` treats as empty. A blank form must therefore never validate-
#      an empty PRD that passed the gate would be worse than no gate.

TEMPLATE = r"""# PRD: <one-line imperative title- this becomes the queue's task text>

## 1. Origin
- Requested by: <Atul, verbatim quote + timestamp | Baxter's own judgement>
- Raw ask: "<his exact words, unparaphrased>"
- Date: <ordinal, e.g. 9th July>

## 2. Problem
What is broken or missing, in one paragraph. A problem asserted without evidence is
refused: cite the log line, the source line, the transcript, the reproduction.
- Evidence: <file:line, or the log excerpt you actually read>
- Who or what suffers, and how often: <...>

## 3. Non-goals
At least one entry. "None" is not an answer- scope creep inside a lane is invisible
until it collides with another lane.
- <what this build explicitly does NOT do>

## 4. Solution
The change, described as observable behaviour: before -> after. Not the implementation- the
behaviour. Why THIS solution and not the two you discarded. The happy path, step by step.
- <before -> after>
- <the happy path>
- <why this shape, and what you rejected>

## 5. Visualisation
What Atul SEES, and what the thing looks like in use. Draw it: the literal line that lands
in Discord, the shape of the log row, the columns of the table, the state file after a run.
"He sees nothing" is a valid answer- write it as such, and say what changes instead. A
solution nobody can picture has not been designed, only described.
- What he sees: <the literal output, quoted as it will appear>
- Where he sees it: <#general, the activity-log, .baxter.log, a vault note, or nowhere>
- What does NOT change on his screen: <...>

## 6. Edge cases
Each one written as `<condition>` then an arrow then `<required behaviour>`. Minimum five.
Consider at least: first run / cold state; concurrent access (another lane, the pump, the
watcher, a hand edit); partial failure (crashed mid-write, half-applied); the usage gate
firing mid-build; malformed or hostile input; idempotency- what happens when it runs twice?
- <condition> -> <required behaviour>
- <condition> -> <required behaviour>
- <condition> -> <required behaviour>
- <condition> -> <required behaviour>
- <condition> -> <required behaviour>

## 7. Touch-set
Every file or directory this build will edit, one `touch:` line each, and every one of
them READ before it is named. A region you have not opened may not be named. Hub files
(baxter_triage.py, baxter_usage.py, baxter_watch.ps1, baxter_fast.py, baxter_slash.py)
are declared BY REGION- `utils/baxter_triage.py/_claude`, never the bare file. If the
build genuinely rewrites a hub file end to end, write a `solo:` line instead, with a reason.
- touch: <path> - <why this file, and what changes in it>
- solo: <reason, only if no honest touch-set exists>

## 8. Verification
A build that cannot state how it will be proven does not get filed. Give ONE of these.
A multi-line command silently runs only its first line and passes, so it must be single-line.
- verify: <a single-line shell command that exits 0 only if the build worked>
- verify_assert: <or, a claim a separate checker must prove>
- What a FALSE pass would look like, and why this gate would not give one: <...>

## 9. Rollback
- How to undo it if it lands wrong: <...>
- What state it writes, and what backs that state up: <...>

## 10. Priority + gate
- priority: <1-8> - <one-line justification>
- gated_on: <atul, if it acts outward, destroys data, or needs a decision only he can
  make; otherwise none. Prose like "do not auto-run" does nothing- the field is the gate>
- next step: <the first concrete thing the builder does>

## 11. Open questions
Empty is a valid answer, and a strong signal. A long list means the ask is not ready.
- <anything you could not resolve from the vault and the source>
"""


def render_template():
    """The blank form. It never validates- see `TEMPLATE`."""
    return TEMPLATE


# ---- THE WORKED EXAMPLE -------------------------------------------------------------
# The PRD for THIS build, written to the form. It is handed to the PM instance as the
# one-shot example, and it is the fixture the sealed acceptance test mutates: it flips
# `utils/baxter_prd_template.py` to a hub path and to a placeholder, and `verify:` to
# `noverify:`, and demands a refusal each time. So the token must appear here literally.

EXAMPLE_PRD = r"""# PRD: PRD-gated build queue- a PM Claude writes the spec and a manager greenlights it

## 1. Origin
- Requested by: Atul, 9th July 09:47
- Raw ask: "when we judge something worth building, i want a separate claude instance
  to be a product manager and fill out a rigorous prd before it gets filed. opus 4.8,
  it needs to be a clever bot. first design the prd format itself in detail- edge cases,
  functionality, how its meant to be used. then wire the delegation. a pm-manager agent
  checks it and greenlights to build."
- Date: 9th July

## 2. Problem
A queue entry is a task string and a next-step string, written by whichever Baxter
happened to be reading Discord at the time. That is the entire specification a lane gets
before it starts editing the fleet, and the queue's failure mode is under-specification.
- Evidence: `utils/baxter_usage.py:1631` defines `TOUCH_PLACEHOLDERS` precisely because
  entries were filed carrying `@cluster` copied out of the example line; `utils/baxter_usage.py:1648`
  defines `HUB_FILES` because 7 of 23 runnable tasks had locked the whole of baxter_triage.py.
  Both guards fire at `--queue` time, long after the entry was written badly.
- Who or what suffers, and how often: a build lane, once per malformed entry- three on
  8th July alone, each burning a spawn before the declaration was found to be fiction.

## 3. Non-goals
- Does NOT make `baxter_usage.py --queue` refuse an entry that has no PRD. Every existing
  caller still queues freely; the gate binds only work routed through `baxter_pm_delegate`.
- Does NOT give the queue an `--edit` / `--drop`. That is the separate priority-3 build.
- Does NOT let the PM write code, run a build, or touch the queue file directly.

## 4. Solution
- before -> after: a big ask went straight to `--queue` as two strings; now it goes to
  `write_prd()`, which spawns an Opus PM, gets a filled PRD back, validates it against
  the machine gates, sends it to a SECOND Opus- the PM-manager- for a greenlight, and only
  then files the queue entry with the PRD as its note.
- the happy path: Baxter calls `baxter_pm_delegate.py --ask "the raw ask" --queue-it` ->
  an Opus instance is spawned with the template, this example and the touch-set rules ->
  it returns a filled PRD -> `validate_prd()` passes -> the manager reads the document and
  answers `greenlight` -> the document lands in `60-PRDs/` -> `--queue` is called with the
  touch-set, verify command, priority and gate parsed out of the document.
- why this shape: the validator can count sections and vet a command, and that is ALL it
  can do. Whether the Problem is real, whether the Evidence line exists, whether the
  Solution is the right one- no regex reads those. A second model can. It answers one of
  three words: `greenlight`, `changes` (its reasons go back to the PM as a retry), or
  `reject` (the build should not happen at all). The rejected alternatives were a human
  gate, which is Atul doing the machine's job, and a second validator pass, which would
  only re-count what was already counted.

## 5. Visualisation
- What he sees: one line on stdout, the path of the filed PRD- `60-PRDs\2026-07-09 - PRD-gated
  build queue....md`. On a refusal, stderr instead: `REFUSED: the PM's PRD did not validate.
  Nothing filed, nothing queued`. A rejection reads `REFUSED by the PM-manager` and names its
  reasons, so he can see it was judgement and not a missing section.
- Where he sees it: the PRD itself, eleven headed sections he can open and read before a lane
  touches the fleet; one `pm-delegate:` line per spawn in `.baxter.log`; and one json row per
  verdict in `.baxter_pm_reviews.jsonl`, which is the manager's own reject log, the same shape
  as the clash guard's- a gate that never visibly refuses is a gate nobody trusts.
- What does NOT change on his screen: nothing new reaches Discord. No ping, no activity-log
  line, no brief. The queue's own "queued at position N" line is unchanged.

## 6. Edge cases
- the PM returns prose with no `touch:` line -> refused, nothing reaches the queue, `None` returned
- the PM names a bare hub file such as baxter_usage.py -> refused by `touch_problems()`, which demands the region
- the PM copies `utils/x.py` out of the example -> refused as the example-line placeholder
- the PM's `verify:` is a `python -c` that does not compile -> `vet_verify_cmd()` returns None and it is refused
- the PM's `verify:` already exits 0 against the current repo -> refused as decoration, not a proof (opt-in guard)
- the PM returns an invalid PRD twice -> one retry with the error list, then nothing is filed
- the PM omits section 5 -> the form has no Visualisation, so it is refused before the manager ever reads it
- the manager answers `changes` -> its reasons go back to the PM as the retry payload, and nothing is filed meanwhile
- the manager answers `reject` -> `write_prd` returns None at once, with no retry: a rejected ask does not improve by rewriting
- the manager answers with prose, or with a verdict nobody defined -> read as `changes`, never as a greenlight
- the manager spawn dies or times out -> read as `changes`, so an unreachable judge can never rubber-stamp a build
- the same ask is written twice -> `enqueue()` matches exact task text and UPDATES, so no duplicate lane
- a spawn times out or the model is unreachable -> `_spawn_pm` raises, `write_prd` returns None, the queue is untouched
- the usage gate fires while the PM is thinking -> the delegate is a caller, not a lane; nothing is half-written

## 7. Touch-set
- touch: `utils/baxter_prd_template.py` - the form, the validator, and this example.
- touch: `utils/baxter_pm_delegate.py` - spawns the PM, validates its answer, files the entry.
- touch: `60-PRDs/` - the PRD store, created by this build.

## 8. Verification
- verify: python -c "import sys;sys.path.insert(0,r'C:\Users\you\Documents\Python Scripts\utils');import baxter_prd_template as T,baxter_pm_delegate as D;assert T.validate_prd(T.render_template())[0];assert not T.validate_prd(T.EXAMPLE_PRD)[0];assert callable(D.write_prd) and callable(D._spawn_manager);print('ok')"
- What a FALSE pass would look like: `validate_prd` special-casing the four strings the
  acceptance test mutates, rather than running each touch entry through the real
  `touch_problems()`. The gate above would still pass. It is guarded against by the
  selftest, which feeds novel hub paths and novel placeholders the exam never names. The
  second false pass is a manager prompt that greenlights everything: the exam stubs the
  spawn, so only a real round-trip can catch a rubber stamp.

## 9. Rollback
- How to undo it: delete the two modules. Nothing else imports them, and `--queue` is
  unchanged, so the queue keeps working exactly as it did before.
- What state it writes: markdown files under `60-PRDs/`, one line per PM spawn in
  `.baxter.log`, and one json row per manager verdict in `.baxter_pm_reviews.jsonl`. All
  three are append-only; the vault's git auto-backup is the backstop.

## 10. Priority + gate
- priority: 1 - Atul asked for it directly and put it at position 1 of 32.
- gated_on: none
- next step: write the template module, then the delegate, then the PRD store.

## 11. Open questions
- Does every build get a PRD, or only those above some size? Vitals must skip it, which
  is what `--prd-exempt` is for, but the size threshold is his call.
"""


# ---- PARSING ------------------------------------------------------------------------

_HEADING = re.compile(r"^##\s*(\d{1,2})\.\s*(.*)$", re.M)
_TITLE = re.compile(r"^#\s*PRD:\s*(.+?)\s*$", re.M)


def _filled(value):
    """A machine field is EMPTY when it is blank or still carries an <angle-bracket>
    placeholder. The template ships every field pre-filled with its own instructions,
    so this is what stops the blank form from validating."""
    v = (value or "").strip()
    if not v:
        return False
    return not (("<" in v) and (">" in v))


def sections(text):
    """{section number: body text}. Unknown numbers are kept- a PM inventing an 11th
    section is a warning, not a refusal."""
    out, marks = {}, list(_HEADING.finditer(text or ""))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[int(m.group(1))] = text[m.end():end]
    return out


def title_of(text):
    m = _TITLE.search(text or "")
    return m.group(1).strip() if m else ""


def _bullets(body):
    out = []
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ")):
            out.append(s[2:].strip())
    return out


def _field(body, name):
    """The value of a `- <name>: <value>` line, bullet optional, backticks tolerated.

    Anchored at the field name so `noverify:` never matches `verify:`- the sealed exam
    mutates exactly that token to prove a PRD with no proof is refused."""
    pat = re.compile(r"^[ \t]*(?:[-*][ \t]*)?`?" + name + r":[ \t]*(.*?)[ \t]*$",
                     re.M | re.I)
    m = pat.search(body or "")
    return m.group(1).strip().strip("`").strip() if m else ""


def touch_entries(body):
    """(paths, solo_reason) from section 6. A `touch:` line names one path; a `solo:`
    line says out loud that no honest touch-set exists."""
    paths, solo = [], ""
    for b in _bullets(body):
        m = re.match(r"^touch:\s*(.+)$", b, re.I)
        if m:
            raw = m.group(1).strip()
            # `<path>` - why...  ->  path
            mm = re.match(r"^`([^`]+)`", raw)
            p = mm.group(1) if mm else re.split(r"\s+-\s+", raw, maxsplit=1)[0]
            p = p.strip().strip("`").strip()
            if _filled(p):
                paths.append(p)
            continue
        m = re.match(r"^solo:\s*(.+)$", b, re.I)
        if m and _filled(m.group(1)):
            solo = m.group(1).strip()
    return paths, solo


def edge_cases(body):
    """Bullets shaped `<condition> -> <behaviour>`. The template's own guidance bullets
    carry angle-bracket placeholders, so they are not counted and a blank form fails here."""
    out = []
    for b in _bullets(body):
        parts = re.split(r"->|→", b, maxsplit=1)
        if len(parts) != 2:
            continue
        if _filled(parts[0]) and _filled(parts[1]):
            out.append(b)
    return out


def queue_fields(text):
    """Everything `--queue` needs, taken OUT OF THE DOCUMENT. Never inferred."""
    sec = sections(text)
    s_prio = sec.get(S_PRIORITY, "")
    s_ver = sec.get(S_VERIFY, "")
    prio = _field(s_prio, "priority")
    m = re.match(r"^(\d+)", prio)
    gate = _field(s_prio, "gated_on").lower()
    verify = _field(s_ver, "verify")
    return {
        "task": title_of(text),
        "next_step": _field(s_prio, r"next\s+step"),
        "priority": int(m.group(1)) if m else None,
        "gate": "" if gate in ("", "none", "no", "false") else gate,
        "touch": touch_entries(sec.get(S_TOUCH, ""))[0],
        "solo": touch_entries(sec.get(S_TOUCH, ""))[1],
        "verify": verify if _filled(verify) else "",
        "verify_assert": _field(s_ver, "verify_assert") if not _filled(verify) else "",
    }


# ---- THE PRE-BUILD TRIVIALITY PROBE -------------------------------------------------
# A proof that passes BEFORE the build exists is decoration, not a test. So the PRD's own
# verify command can be run against the current repo and the PRD refused if it exits 0.
#
# It is OFF BY DEFAULT, deliberately, and for two reasons worth writing down:
#   1. The command is written by a language model and would run against the live vault.
#      Only a `python`/`py` invocation is even considered here, and anything with a shell
#      metacharacter is refused UNRUN- but the honest answer is that this is an opt-in risk,
#      not a safe default (the planner flagged it as risk 4 of this very build).
#   2. EXAMPLE_PRD documents a build that HAS landed, so its verify command passes today.
#      A default-on guard would refuse the module's own worked example.
# `write_prd()` therefore leaves it off; `--check --triviality` and the selftest turn it on.

_SHELL_META = re.compile(r"[&|;><`$\n]|\brm\b|\bdel\b|\brmdir\b|\bformat\b")


def _triviality_problem(cmd, timeout=TRIVIALITY_TIMEOUT):
    """None if the command is a real proof; a refusal string if it is decoration or
    if it is not safe to run unattended."""
    if not cmd:
        return None
    if _SHELL_META.search(cmd) and V.python_c_source(cmd) is None:
        return ("verify: carries shell metacharacters or a destructive verb and was NOT run- "
                "give a single `python -c \"...\"` or `python <script>` command")
    if not re.match(r"^\s*(python|py)\b", cmd):
        return ("verify: must be a python invocation for the pre-build triviality probe- "
                f"got {cmd.split()[0]!r}")
    try:
        rc, _ = V.run_command(cmd, timeout=timeout)
    except Exception as e:                                   # a probe that dies proves nothing
        _log(f"triviality probe could not run ({e}); not refusing on that basis")
        return None
    if rc == 0:
        return ("verify: already exits 0 against the current repo, before the build exists- "
                "a proof that passes now is decoration, not a test")
    return None


# ---- VALIDATION ---------------------------------------------------------------------

def validate_prd(text, triviality_check=False, triviality_timeout=TRIVIALITY_TIMEOUT):
    """(errors, warnings). A non-empty `errors` means the PRD may not be filed.

    Machine checks only. Nothing here reads the prose for quality- it counts sections,
    counts non-goals, counts edge cases, and hands the two load-bearing declarations to
    the very gates the build queue itself uses. The Origin quote, the Evidence line and
    the Problem are UNCHECKED and can be fabricated fluently; that is a known hole, named
    in the example PRD's own section 10, and the reason a human still reads the document.
    """
    errors, warnings = [], []
    text = text or ""

    if not title_of(text):
        errors.append("no `# PRD: <title>` heading- the title is the queue's task text")

    sec = sections(text)
    missing = [n for n in SECTIONS if n not in sec]
    if missing:
        errors.append("missing section(s): " +
                      ", ".join(f"{n}. {SECTIONS[n]}" for n in sorted(missing)))

    # 3. Non-goals- at least one, and "none" is not an answer.
    goals = [b for b in _bullets(sec.get(S_NON_GOALS, "")) if _filled(b)]
    goals = [g for g in goals if g.strip().lower().rstrip(".") not in ("none", "n/a")]
    if len(goals) < MIN_NON_GOALS:
        errors.append(f"section {S_NON_GOALS} needs at least {MIN_NON_GOALS} real non-goal- "
                      "'none' is not an answer, scope creep in a lane is invisible")

    # 5. Visualisation- an ERROR, not a warning. A build nobody can picture is a build whose
    # effect on Atul was never decided; the PM writes it down or the PRD is not fileable.
    # An empty section is the common failure, so a present-but-unfilled heading fails here
    # exactly as a missing one fails the section count above.
    vis = [b for b in _bullets(sec.get(S_VISUALISATION, "")) if _filled(b)]
    if S_VISUALISATION in sec and len(vis) < MIN_VISUALISATION:
        errors.append(f"section {S_VISUALISATION} Visualisation is empty- write what Atul "
                      "SEES, or write plainly that he sees nothing and what changes instead")

    # 6. Edge cases- at least five, each an arrow rule.
    edges = edge_cases(sec.get(S_EDGES, ""))
    if len(edges) < MIN_EDGE_CASES:
        errors.append(f"section {S_EDGES} has {len(edges)} edge case(s), needs {MIN_EDGE_CASES}- "
                      "each written as `<condition> -> <required behaviour>`")

    # 7. Touch-set- the first load-bearing declaration.
    paths, solo = touch_entries(sec.get(S_TOUCH, ""))
    if not paths and not solo:
        errors.append(f"section {S_TOUCH} declares no touch-set and no `solo:` reason- an "
                      "undeclared build clashes with every lane and runs alone")
    if paths and solo:
        warnings.append(f"section {S_TOUCH} declares both a touch-set and `solo:`- "
                        "the touch-set wins")
    if paths:
        refuse, warn = U.touch_problems(paths)
        errors.extend(refuse)
        warnings.extend(warn)

    # 8. Verification- the second, and the whole point of the gate.
    s7 = sec.get(S_VERIFY, "")
    verify, vassert = _field(s7, "verify"), _field(s7, "verify_assert")
    if not _filled(verify) and not _filled(vassert):
        errors.append(f"section {S_VERIFY} has no `verify:` command and no `verify_assert:` "
                      "claim- a build that cannot say how it will be proven is not fileable")
    elif _filled(verify):
        if "\n" in verify:
            errors.append("the `verify:` command spans lines- a multi-line command runs "
                          "only its first line and passes silently")
        vetted, why = V.vet_verify_cmd(verify)
        if vetted is None:
            errors.append(f"the `verify:` command cannot run: {why}")
        elif why:
            warnings.append(f"verify: {why}")
        if vetted is not None and triviality_check:
            trivial = _triviality_problem(vetted, timeout=triviality_timeout)
            if trivial:
                errors.append(trivial)

    # 10. Priority, gate, next step.
    s9 = sec.get(S_PRIORITY, "")
    prio = _field(s9, "priority")
    m = re.match(r"^(\d+)", prio)
    if not m:
        errors.append(f"section {S_PRIORITY} has no `priority: <1-8>` line")
    elif not (PRIORITY_RANGE[0] <= int(m.group(1)) <= PRIORITY_RANGE[1]):
        errors.append(f"priority {m.group(1)} is outside {PRIORITY_RANGE[0]}-{PRIORITY_RANGE[1]}")
    if not _filled(_field(s9, r"next\s+step")):
        errors.append(f"section {S_PRIORITY} has no `next step:` line- the builder starts from it")
    gate = _field(s9, "gated_on")
    if not _filled(gate):
        errors.append(f"section {S_PRIORITY} has no `gated_on:` line- write `none` if it "
                      "needs no human gate")

    # 2. Evidence- warned, never refused. Fabrication is not machine-detectable.
    if not re.search(r"\S+:\d+|\.log|\.jsonl|transcript", sec.get(S_PROBLEM, ""), re.I):
        warnings.append(f"section {S_PROBLEM} cites no `file:line` or log line- the evidence "
                        "is unverifiable")

    extra = [n for n in sec if n not in SECTIONS]
    if extra:
        warnings.append(f"unknown section(s): {sorted(extra)}")

    return errors, warnings


# ---- SELFTEST -----------------------------------------------------------------------

def selftest():
    """Stubs every outward path: nothing spawns, nothing posts, nothing writes the queue.

    The one thing that DOES run a subprocess is the triviality probe, and only against
    commands this test writes itself (`python -c "pass"`)."""
    # 1. The blank form must never validate.
    e, _ = validate_prd(render_template())
    assert e, "the blank template validated- the gate is a no-op"
    joined = " ".join(e).lower()
    assert "touch-set" in joined and "verif" in joined, e

    # 2. The worked example must validate clean.
    e, w = validate_prd(EXAMPLE_PRD)
    assert not e, ("EXAMPLE_PRD must validate with zero errors", e)

    # 3. NOVEL hub paths and NOVEL placeholders- not the four strings the sealed exam
    #    mutates. If validate_prd special-cased those, this is where it dies.
    for hub in ("utils/baxter_triage.py", "utils/baxter_watch.ps1", "utils/baxter_fast.py"):
        eh, _ = validate_prd(EXAMPLE_PRD.replace("utils/baxter_pm_delegate.py", hub))
        assert any("region" in x.lower() or "hub" in x.lower() for x in eh), (hub, eh)
    # ...and the region form of the same file passes, because two regions are parallel.
    er, _ = validate_prd(EXAMPLE_PRD.replace("utils/baxter_pm_delegate.py",
                                             "utils/baxter_triage.py/_claude"))
    assert not er, ("a hub REGION must be accepted", er)
    for ph in ("@cluster", "utils/some_dir"):
        ep, _ = validate_prd(EXAMPLE_PRD.replace("utils/baxter_pm_delegate.py", ph))
        assert any("placeholder" in x.lower() for x in ep), (ph, ep)
    eo, _ = validate_prd(EXAMPLE_PRD.replace("utils/baxter_pm_delegate.py",
                                             "utils/nowhere_at_all/deep/x.py"))
    assert any("does not exist" in x.lower() for x in eo), eo

    # 4. A verify command that cannot compile is refused (the 9th-July sealed-exam bug).
    bad = EXAMPLE_PRD.replace("import baxter_prd_template as T", "import ((")
    eb, _ = validate_prd(bad)
    assert any("cannot run" in x.lower() for x in eb), eb

    # 5. Counting, not reading: strip the non-goals and the edge cases.
    e3, _ = validate_prd(re.sub(r"(?s)## 3\. Non-goals.*?## 4\.", "## 3. Non-goals\n\n## 4.",
                                EXAMPLE_PRD))
    assert any("non-goal" in x.lower() for x in e3), e3
    trimmed = EXAMPLE_PRD.split("## 6. Edge cases")[0] + "## 6. Edge cases\n- a -> b\n\n## 7." + \
        EXAMPLE_PRD.split("## 7.", 1)[1]
    e5, _ = validate_prd(trimmed)
    assert any("edge case" in x.lower() for x in e5), e5

    # 5b. Visualisation is an ERROR, both ways: cut the section out entirely, and empty it.
    #     Atul named it in the ask ("user problem, solution, visualisation, ..."), so a PRD
    #     that cannot say what he SEES is not fileable.
    gone = re.sub(r"(?ms)^##\s*5\.\s*Visualisation.*?(?=^##\s*\d+\.)", "", EXAMPLE_PRD)
    assert gone != EXAMPLE_PRD, "the Visualisation section did not strip"
    ev, _ = validate_prd(gone)
    assert any("visualisation" in x.lower() for x in ev), ev
    hollow = re.sub(r"(?ms)^(##\s*5\.\s*Visualisation).*?(?=^##\s*\d+\.)", r"\1\n\n",
                    EXAMPLE_PRD)
    eh2, _ = validate_prd(hollow)
    assert any("visualisation" in x.lower() and "empty" in x.lower() for x in eh2), eh2

    # 6. The triviality probe. `python -c "pass"` exits 0 -> decoration, refused.
    trivial = _triviality_problem('python -c "pass"', timeout=30)
    assert trivial and "decoration" in trivial, trivial
    real = _triviality_problem('python -c "import sys;sys.exit(1)"', timeout=30)
    assert real is None, real
    unsafe = _triviality_problem('rm -rf / && python -c "pass"', timeout=30)
    assert unsafe and "NOT run" in unsafe, unsafe
    assert _triviality_problem("") is None

    # 7. ...and it is OFF unless asked for, or EXAMPLE_PRD would refuse itself.
    e7, _ = validate_prd(EXAMPLE_PRD)
    assert not e7, e7

    # 8. Queue fields come out of the document, never inferred.
    q = queue_fields(EXAMPLE_PRD)
    assert q["priority"] == 1 and q["gate"] == "" and q["next_step"], q
    assert "utils/baxter_prd_template.py" in q["touch"], q["touch"]
    assert q["verify"].startswith("python -c"), q["verify"]
    assert q["task"].startswith("PRD-gated build queue"), q["task"]

    print("baxter_prd_template selftest OK: a blank form is refused, the example validates, "
          "a PRD with no Visualisation (missing or empty) is refused, and novel hub paths / "
          "placeholders / uncompilable exams / trivial proofs are all refused by the real "
          "gates, not by string matching")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="the build queue's PRD form + its gate")
    ap.add_argument("--print", action="store_true", help="print the blank form")
    ap.add_argument("--check", metavar="FILE", help="validate a filled PRD")
    ap.add_argument("--triviality", action="store_true",
                    help="with --check: also RUN the PRD's verify command and refuse it "
                         "if it already passes (it runs against the live repo)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.print:
        sys.stdout.write(render_template())
        return 0
    if a.check:
        text = Path(a.check).read_text(encoding="utf-8")
        errors, warnings = validate_prd(text, triviality_check=a.triviality)
        for w in warnings:
            print(f"warn:  {w}")
        for e in errors:
            print(f"REFUSE: {e}")
        if errors:
            print(f"\n{len(errors)} refusal(s)- this PRD may not be filed")
            return 2
        print("PRD validates- fileable")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
