"""baxter_verify- the VERIFY + TROUBLESHOOT loop that wraps a build lane's exit.

the owner, 9th July 01:37 (the overnight order): "automatically checking and confirming all
your work where possible then auto trouble shoot failures and pass successes".

This turns the TRUST-BUT-VERIFY rule into lane MACHINERY. Before it, a lane reported
done because the worker said done, a failure was re-queued blind and unchanged, and a
success passed nothing on. The unattended overnight drain is the case that breaks: a
deterministic failure retries forever, and quietly.

Three things live here; `baxter_triage` calls them at the lane's retire point.

  1. run_verify(entry)   - the GATE. A queue entry may carry `verify` (a shell command)
     or `verify_assert` (a claim a checker must prove). Non-zero exit, or a checker
     verdict of NOT PROVEN, flips the outcome to FAILED whatever the worker claimed.
     Neither declared = "unverified"- a state of its own, never "done". The checker is
     a SEPARATE claude spawn from the builder (the builder is the least reliable witness
     to its own success) and is told to observe BEHAVIOUR, not source text: on 9th July
     the fast lane verified clean while the resident listener served stale code.

  2. classify(rc, tail, entry) - the TRIAGE of a failure, BEFORE any retry.
     transient -> retry once, unchanged.  deterministic -> a diagnose-and-repair worker.
     gated -> never auto-repaired; park it and tell him.

     THE TRAP, and why the order below is what it is. That night's failure led with
     `Sandbox disabled: sandbox is enabled but windows is not supported`- a benign
     warning from `sandbox.enabled: true` in settings.json, which Windows cannot honour.
     It sits at the TOP of the log and reads exactly like a cause. It is not one: the
     worker exited on 4294967295. So we classify on the EXIT CODE first, the log tail
     second, and strip known-benign warning lines before reading a single word of it.
     A classifier that reads the top of a log misdiagnoses every time.

  3. record()/recent() - the outcome LEDGER (.baxter_build_outcomes.json). "Pass
     successes" needs somewhere for a success to land: the next lane reads recent() in
     its prompt, and a parked task leaves its diagnosis here rather than dying in a log.

Caps are deliberate. One transient retry, three repair attempts, then PARK- and a parked
task ANNOUNCES itself, because overnight a silently parked queue looks identical to a
drained one in the morning. An unbounded self-repair loop is how a usage window is eaten.

The repair attempts themselves are SILENT (the owner, 9th July: "add autonomous root-cause
troubleshooting- self-diagnose + self-fix- before ever flagging a build failure to the owner").
A failure the loop is still healing is machinery, not news. Only two things reach him: a
park (the cap is spent, or the task is gated) and a landing. Silence is therefore bounded
by MAX_REPAIRS- raise it and you lengthen how long a broken build stays quiet.
"""
import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

VAULT = Path(r"C:\Users\you\Documents\Baxter")
LEDGER = VAULT / ".baxter_build_outcomes.json"
LOG = VAULT / ".baxter_verify.log"

UTILS = Path(__file__).resolve().parent
PYCACHE_PREFIX = VAULT / ".baxter_pycache"
HARDEN_TIMEOUT = 120      # compileall over utils/; it only runs when a stale pyc exists

LEDGER_CAP = 200          # outcomes kept; the ledger is a running record, not an archive
VERIFY_TIMEOUT = 900      # a shell verify command
CHECKER_TIMEOUT = 2400    # a claude checker spawn (it may have to drive a real flow)
MAX_REPAIRS = 3           # diagnose-and-repair attempts before the task parks
MAX_TRANSIENT = 1         # blind retries of an undiagnosed failure before we go looking
TAIL_CHARS = 4000         # how much of a worker's output the classifier reads

# The HARD CAP on waiting for a sibling lane's mutation window to close. A red-proof holds a
# hub broken for seconds; anything past this is a wedged marker, and a gate that waits for ever
# on one reports nothing but honest ignorance for the rest of the night.
MUTATION_WAIT_S = 90

# Values of `gated_on` that mean "not waiting on a human" (mirrors baxter_usage.GATE_NONE).
_GATE_NONE = ("", "none", "no", "false", "usage", "curve")


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


# ---- BENIGN NOISE -------------------------------------------------------------------
# Lines that are ALWAYS noise on this machine. Stripped before the tail is read, so they
# can never be mistaken for a cause. Add sparingly: a line removed here is a line the
# classifier becomes blind to.
BENIGN = (
    r"sandbox is enabled but windows is not supported",
    r"^\s*sandbox disabled",
    r"deprecationwarning",
    r"\buserwarning\b",
    r"experimentalwarning",
    r"^\s*npm\s+warn",
    r"^\s*warning:",
    r"punycode",
    r"^\s*$",
)
_BENIGN_RE = re.compile("|".join(BENIGN), re.I)

# Read in this order. Transient signs win over deterministic ones on purpose: a 429 or a
# lock often arrives WRAPPED in a traceback, and retrying that costs one spawn, whereas
# sending a repair worker after a rate limit costs a whole diagnosis for nothing.
TRANSIENT = (
    (r"\b429\b|rate.?limit", "rate limited"),
    (r"usage limit|quota exceeded", "usage limit"),
    (r"overloaded|api error 5\d\d|internal server error|bad gateway", "server overload"),
    (r"timed out|timeoutexpired|timeouterror", "timed out"),
    (r"econnreset|etimedout|connection (?:reset|refused|aborted)", "network fault"),
    (r"being used by another process|resource temporarily unavailable|lock", "lock contention"),
    (r"memoryerror|out of memory", "out of memory"),
)
DETERMINISTIC = (
    (r"traceback \(most recent call last\)", "python traceback"),
    (r"\bassertionerror\b", "assertion failed"),
    (r"\bsyntaxerror\b|\bindentationerror\b", "syntax error"),
    (r"\bmodulenotfounderror\b|\bimporterror\b", "missing import"),
    (r"\bfilenotfounderror\b|no such file or directory", "missing file"),
    (r"is not recognized as an internal|command not found", "missing command"),
    (r"\bjsondecodeerror\b", "malformed json"),
    (r"\b(?:nameerror|attributeerror|typeerror|valueerror|keyerror|indexerror)\b",
     "unhandled exception"),
)


def strip_benign(text):
    """The log tail with known-benign warning lines removed. What is left is signal."""
    if not text:
        return ""
    keep = [ln for ln in str(text).splitlines() if not _BENIGN_RE.search(ln)]
    return "\n".join(keep).strip()


def tail(text, n=TAIL_CHARS):
    t = " ".join(str(text or "").split("\r"))
    return t[-n:]


def failure_tail(text, max_lines=40, max_chars=4000):
    """The EVIDENCE a repair worker needs: the last lines of a failed run, newlines intact.

    `tail()` takes the last n CHARACTERS and is what `classify()` reads; it must not change.
    This takes the last n LINES, because a python traceback puts the exception type and line
    on its LAST line, and a character cap applied to a detail string that LEADS with the
    verify command echo evicts exactly that (10th July: the lane-ui build died on an
    import-time exception inside baxter_slash.py twice, and the type was never recorded).

    The char cap is applied AFTER the line cap and is tail-anchored, so one enormous line
    cannot push the exception off the end. A cap at or below zero returns "", never the
    whole text: see the clamp below.
    """
    clean = strip_benign(text)          # blank lines and warnings go first- they are not evidence
    if not clean:
        return ""
    # A NON-POSITIVE CAP MUST CUT EVERYTHING, NOT NOTHING. `lines[-0:]` is `lines[:]`, so a
    # caller computing a budget that lands at or below zero (max_chars=budget - len(prefix))
    # would be handed the WHOLE output where it asked for none of it, and a negative cap
    # returns a HEAD. That is this function inverted into the very defect it was written to
    # cure, so clamp before slicing rather than trusting the slice.
    max_lines, max_chars = int(max_lines), int(max_chars)
    if max_lines <= 0 or max_chars <= 0:
        return ""
    return "\n".join(clean.splitlines()[-max_lines:])[-max_chars:]


def failure_head(text, n=200):
    """The single most diagnostic LINE of a failed run: a traceback's last line is the
    exception type and its message. It leads `detail` so that `.baxter.log`'s 90-char slice
    and the ledger's 300-char slice both keep signal instead of a command echo."""
    clean = strip_benign(text)
    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    if not lines:
        return ""
    head = lines[-1]
    # 3.11+ underlines the offending expression with `^^^^` / `~~~^~~~` under the source echo.
    # That marker is the last line of some tracebacks and says nothing on its own.
    while lines and re.fullmatch(r"[\s^~]+", head):
        lines.pop()
        head = lines[-1] if lines else ""
    return head[:int(n)]


# A traceback FRAME line, and the source line python echoes underneath it. `classify` must not
# read those when it hunts for TRANSIENT words: the echoed source is the CODE THAT FAILED, not
# a description of the failure. Before 10th July `classify` only ever saw a 220-char collapsed
# command echo on the verify-gate path, so the frames were truncated away by accident. Now that
# the real traceback reaches it, a frame like `        with self._lock:` would match the
# 'lock contention' transient pattern and turn a deterministic exam failure into a blind retry-
# healing nothing, forever. The exception MESSAGE still scans, which is where a real 429 lands.
_FRAME_RE = re.compile(r'^\s*File "[^"]*", line \d+', re.I)


def _drop_frames(text):
    """`text` with traceback frame lines and their echoed source removed."""
    out, skip = [], False
    for ln in str(text or "").splitlines():
        if _FRAME_RE.match(ln):
            skip = True                 # the frame line itself is a path, never a diagnosis
            continue
        if skip:
            skip = False
            if ln[:1] in (" ", "\t"):
                continue                # the source line python echoes under the frame
        out.append(ln)
    return "\n".join(out)


def is_gated(entry):
    g = str((entry or {}).get("gated_on", "") or "").strip().lower()
    return g not in _GATE_NONE


def classify(rc, log_tail, entry=None):
    """Why a lane failed, as (kind, reason). kind is transient | deterministic | gated.

    EXIT CODE FIRST, log tail second, benign lines never. See the module docstring for
    the 9th-July failure that dictates that order.
    """
    entry = entry or {}
    if is_gated(entry):
        return "gated", f"waits on {entry.get('gated_on')}- never auto-repaired"

    # rc 0 can only reach here from the verify gate: the worker exited clean and the
    # check still refused to prove it. That is reproducible by definition.
    if rc == 0:
        return "deterministic", "the verify gate did not prove the change"

    clean = strip_benign(log_tail)
    # Transient words are believed only where they DESCRIBE the fault- log lines and the
    # exception message- never inside the source python echoes under a frame. See _drop_frames.
    for pat, why in TRANSIENT:
        if re.search(pat, _drop_frames(clean), re.I):
            return "transient", why
    for pat, why in DETERMINISTIC:
        if re.search(pat, clean, re.I):
            return "deterministic", why

    # Nothing diagnostic survived the strip. A bare abnormal exit (4294967295 was the
    # 9th-July one) tells us the worker died, not why- worth exactly one blind retry.
    if rc is None:
        return "transient", "the lane died without an exit code (ghost lane)"
    return "transient", f"bare exit {rc}, no diagnostic in the log"


def next_action(kind, entry):
    """What the lane does about a classified failure, as (action, why).
    action is retry | repair | park. The caps live here, in one place."""
    if kind == "gated":
        return "park", f"gated- {entry.get('gated_on', 'a human')} must say go"
    repairs = int(entry.get("repair_attempts", 0) or 0)
    if repairs >= MAX_REPAIRS:
        return "park", f"{repairs} repair attempts spent- it needs the owner"
    if kind == "transient":
        if int(entry.get("transient_retries", 0) or 0) < MAX_TRANSIENT:
            return "retry", "transient- one blind retry, unchanged"
        # A "transient" fault that survives its retry is not transient. Stop guessing
        # and go and look at it, rather than retrying it round the clock overnight.
        return "repair", "retried once and failed again- treating it as deterministic"
    return "repair", f"deterministic- repair attempt {repairs + 1} of {MAX_REPAIRS}"


# ---- THE VERIFY GATE ----------------------------------------------------------------
def _checker_brief(claim, task, touch):
    """The prompt for the SEPARATE checker spawn. It is not the builder, it did not do
    the work, and its default answer is NOT PROVEN."""
    return (
        "You are Baxter's build CHECKER. A separate worker has just claimed a build is "
        "finished. You did NOT do that work and you owe it no loyalty: your job is to "
        "TRY TO DISPROVE the claim, and to report honestly if you cannot prove it.\n\n"
        f"THE BUILD: {task}\n"
        f"FILES IT TOUCHED: {', '.join(touch) if touch else '(undeclared)'}\n"
        f"THE CLAIM TO TEST: {claim}\n\n"
        "HOW TO TEST IT:\n"
        "- Observe BEHAVIOUR, not source text. Run the thing. Read its real output. Grep "
        "the live log or state file it should have written. Reading the code and agreeing "
        "with it is NOT a verification- on 9th July a check like that passed while the "
        "running process still served the old code.\n"
        "- If a long-lived process serves the changed code, test the PROCESS, not the file.\n"
        "- Ignore this benign Windows line if you see it, it is never a cause: 'Sandbox "
        "disabled: sandbox is enabled but windows is not supported'.\n"
        "- Change nothing. You are a witness, not a builder. Do not edit, fix or queue.\n"
        "- A handful of commands, then answer. Do not investigate for its own sake.\n\n"
        "Print, as the LAST line of your reply, exactly one of:\n"
        "  VERDICT: PROVEN\n"
        "  VERDICT: NOT PROVEN - <one short clause on what failed or what you could not test>\n"
        "If you are unsure, it is NOT PROVEN. An unproven build reported as proven is the "
        "exact failure this checker exists to stop."
    )


def _run_checker(claim, entry, timeout=CHECKER_TIMEOUT):
    """Spawn a fresh claude as the checker- never the builder that made the claim."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import baxter_modelguard as _mg
        argv = ["claude"] + _mg.args("heavy")
    except Exception:
        argv = ["claude", "--model", "opus"]
    prompt = _checker_brief(claim, entry.get("task", "?"), entry.get("touch_set") or [])
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(argv + ["-p", prompt], cwd=str(VAULT), timeout=timeout,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env)
    except Exception as e:
        return "failed", f"the checker could not run ({e})"
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    hits = re.findall(r"VERDICT:\s*(PROVEN|NOT PROVEN)\s*-?\s*(.*)", out, re.I)
    if not hits:
        # No verdict is not a pass. Silence from a witness proves nothing.
        return "failed", f"the checker returned no verdict (exit {r.returncode})"
    kind, why = hits[-1]
    if kind.strip().upper() == "PROVEN":
        return "passed", "checker proved the claim against live behaviour"
    return "failed", f"checker says NOT PROVEN- {' '.join(why.split())[:180] or 'no reason given'}"


def _coreutils_dir():
    """Git for Windows' usr\\bin- rm, test, grep, cat: the POSIX tools a verify assumes.

    `shell=True` on Windows is cmd.exe, and cmd has no `rm`. Planners write POSIX verify
    commands anyway (they read as shell), so a build that WORKED fails its gate on
    "'rm' is not recognized"- a shell error indistinguishable, in the ledger, from a real
    defect. The directory is not on PATH by default: `Git\\cmd` is, `Git\\usr\\bin` is not.
    Resolved off git.exe rather than a hardcoded Program Files path.
    Returns None when there is no coreutils to add- then cmd.exe behaves exactly as before.
    """
    if os.name != "nt":
        return None
    git = shutil.which("git")
    roots = [Path(git).resolve().parent.parent] if git else []
    roots += [Path(r"C:\Program Files\Git"), Path(r"C:\Program Files (x86)\Git")]
    for root in roots:
        d = root / "usr" / "bin"
        if (d / "rm.exe").is_file():   # prove it is coreutils, not merely a directory
            return str(d)
    return None


def _bash():
    """Git for Windows' bash.exe, or None. Sits beside the coreutils we already resolve."""
    d = _coreutils_dir()
    if not d:
        return None
    b = Path(d) / "bash.exe"
    return str(b) if b.is_file() else None


# Constructs cmd.exe cannot parse AT ALL. cmd does not fail these one command at a time- it
# rejects the whole line at parse time ("t was unexpected at this time.") and runs NOTHING,
# so even the `echo` before the loop never executes. Planners write POSIX exams, because
# every step `check` they author is POSIX. Measured 9th July on this build's own sealed
# acceptance: `... && for t in A B C; do ...; done && ...` exited 1 having run no part of
# itself, which would have marked a working build FAILED. A verify that cannot run is not a
# verdict, it is noise ([[verify-gate-runs-cmd-exe]], and vet_verify_cmd's docstring).
#
# SCRIPT KEYWORDS ARE READ OUTSIDE THE QUOTES, AND ONLY THERE (11th July). A keyword inside a
# double-quoted span is an ARGUMENT to both shells- neither cmd.exe nor bash parses `for` in
# `python -c "... [p for p in sys.path] ..."` as a loop. Scanning the raw command therefore
# read a PYTHON LIST COMPREHENSION as POSIX script and sent a cmd.exe-only command to bash:
# measured 11th July on this build's own sealed acceptance, `cd /d "..." && python -c "...for p
# in sys.path..."`, where bash's `cd` rejected `/d` as a second argument ("cd: too many
# arguments"), exit 1, no leg after it ever run, and a correct build was graded FAILED. So the
# keywords are matched against the command with its double-quoted spans blanked out.
_POSIX_SCRIPT = re.compile(
    r"(?:^|[\s;&|(])(?:for|while|until)\s+\S+\s+in\s"   # for t in a b c
    r"|;\s*do(?:\s|$)"                                  # ; do
    r"|(?:^|[\s;&|])done(?:[\s;&|]|$)"
    r"|(?:^|[\s;&|])(?:fi|then|elif)(?:[\s;&|]|$)")

# Expansions, by contrast, are scanned over the WHOLE command, quotes and all- and that is not
# an oversight. bash expands `$(...)`, `$?` and `${...}` INSIDE double quotes; cmd.exe does not.
# So unlike a keyword, one of these in a quoted span is a real difference between the shells,
# and the command means what only bash can give it.
_POSIX_EXPANSION = re.compile(r"\$\(|\$\?|\$\{"        # $(...), $?, ${...}
                              r"|/dev/null")


def _outside_double_quotes(cmd):
    """`cmd` with every double-quoted span blanked to spaces, so a scan sees only the shell.

    Positions are preserved, because the keyword patterns key on the character BEFORE the
    keyword and a collapse would forge word boundaries that are not there. cmd.exe has no
    escape for `"`- a quote toggles, and an unbalanced one quotes to the end of the line- so
    that is exactly what this models.
    """
    s = str(cmd or "")
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == '"':
            j = s.find('"', i + 1)
            j = n if j < 0 else j + 1      # unbalanced: quoted to end of line
            out.append(" " * (j - i))
            i = j
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _split_legs(cmd):
    """The legs of a compound command: split on `&& || ; | &` and newlines OUTSIDE quotes.

    Quote-aware because the separator we must NOT split on is usually inside the very leg we
    are hunting: `python -c 'import sys; sys.exit(7)'` has a `;` that belongs to Python.
    """
    legs, buf, quote, i, n = [], [], None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
        elif c in "\"'":
            quote = c
            buf.append(c)
            i += 1
        elif c in "&|":
            j = i
            while j < n and cmd[j] == c:   # `&&`, `||`, and the single-char forms
                j += 1
            legs.append("".join(buf)); buf = []
            i = j
        elif c in ";\n":
            legs.append("".join(buf)); buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    legs.append("".join(buf))
    return [leg.strip() for leg in legs if leg.strip()]


# One leg of a compound, opening a `python -c` argument with a SINGLE quote. Deliberately
# does not require a closing quote or a quote-free source (unlike _PY_C_RE): a source that
# itself contains an apostrophe is shattered by cmd.exe just the same, and must still route.
_PY_C_SQ_LEG = re.compile(r"""^(?P<py>"[^"]+"|\S+)\s+-c\s+'""")


def _is_single_quoted_python_c_leg(leg):
    m = _PY_C_SQ_LEG.match(str(leg or ""))
    if not m:
        return False
    stem = os.path.basename(m.group("py").strip('"').lower())
    return stem in ("py", "py.exe") or stem.startswith("python")


def _has_single_quoted_python_c_leg(cmd):
    """True when `cmd` is a COMPOUND with at least one single-quoted `python -c` leg.

    cmd.exe does not treat `'` as a quote. It hands python the bare argument `'import` and
    python dies with SyntaxError before the exam runs- rc=1, and a correct build is marked
    FAILED. A whole-command `python -c` never gets here (run_command writes it to a temp file
    and skips the shell entirely); only a leg of a compound, which must go to a real shell.
    """
    legs = _split_legs(str(cmd or ""))
    if len(legs) < 2:                                   # not a compound: nothing to shatter
        return False
    return any(_is_single_quoted_python_c_leg(leg) for leg in legs)


def _has_single_quoted_region(cmd):
    """True when `cmd` carries a CLOSED single-quoted region OUTSIDE any double-quoted span.

    cmd.exe does not treat `'` as a quote at all: it hands the argument its quotes verbatim.
    So `cd 'C:/Users/you/Documents/Baxter'` becomes a `cd` to a path that literally begins
    with an apostrophe, and cmd rejects the whole line- "The filename, directory name, or
    volume label syntax is incorrect."- exit 1, having run nothing (measured 10th July on this
    build's own sealed acceptance, `cd 'C:/...' && python '.baxter_exams/...'`). Any leg quoted
    the POSIX way is unrunnable under cmd.exe, not only a `python -c` one.

    Quote-aware: a `'` inside a `"..."` span is a cmd.exe literal (`findstr "can't" f`), and a
    LONE apostrophe (`echo don't`) is likewise a cmd.exe literal, not POSIX quoting- neither
    routes. Only a properly closed `'...'` region does. A whole-command `python -c '...'` never
    reaches here: needs_posix_shell returns first on python_c_source, and _run_once runs that
    via a temp file with no shell in the path at all.
    """
    s = str(cmd or "")
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':                       # a double-quoted span: skip it whole
            j = s.find('"', i + 1)
            if j < 0:
                return False               # unbalanced- leave it to the other rules
            i = j + 1
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                return False               # lone apostrophe- a cmd.exe literal, not POSIX
            return True                    # a closed region cmd.exe cannot strip
        else:
            i += 1
    return False


def needs_posix_shell(cmd):
    """True when cmd.exe cannot faithfully run `cmd`, so a POSIX shell must.

    Routing is decided by what cmd.exe CANNOT DO, not by whether the command happens to
    contain a shell keyword. Three reasons, in order:

      1. A compound with a single-quoted `python -c` leg. cmd.exe cannot strip `'`, so the
         leg reaches python broken. Measured 9th July: the true exit code 7 came back as 1.
         Before this, such a compound routed to bash only by ACCIDENT- when its Python source
         happened to contain a token the script pattern matched, like `for t in (...)`.
      2. ANY closed single-quoted argument region. cmd.exe cannot strip `'` for a plain leg
         either: `cd 'C:/...' && python 'x.py'` dies at parse time under cmd.exe having run
         nothing (measured 10th July). The `python -c` case above is the narrow subset that
         was caught first; this is the general rule.
      3. Shell script cmd.exe rejects at parse time (`for`/`do`/`done`), read OUTSIDE the
         double quotes- inside them it is an argument, not script- or a bash expansion
         (`$(...)`, `$?`, `/dev/null`), which is read everywhere, quoted spans included.

    A whole-command `python -c "..."` is excluded outright- it already runs correctly, and
    rewriting a command that works is how this file earned its scars.

    ROUTING THE WRONG WAY IS NOT THE SAFE DIRECTION. Both mistakes mark a working build
    FAILED: cmd.exe shatters a POSIX exam, and bash rejects `cd /d` on a Windows one. Neither
    shell is a fallback for the other, so this decides on what the command actually says.
    """
    cmd = str(cmd or "")
    if not cmd.strip() or python_c_source(cmd) is not None:
        return False
    if _has_single_quoted_python_c_leg(cmd):
        return True
    if _has_single_quoted_region(cmd):
        return True
    if _POSIX_EXPANSION.search(cmd):
        return True
    return bool(_POSIX_SCRIPT.search(_outside_double_quotes(cmd)))


# ---- STALE BYTECODE ------------------------------------------------------------------
# CPython validates a timestamp-mode .pyc against (source mtime SECONDS, source size). Two
# edits to one file inside a single second, landing on the same byte-count, therefore leave
# the FIRST edit's bytecode live: a fresh process imports code that is not on disk. Measured
# on lane 2, 9th July- baxter_verify.py read `MAX_REPAIRS = 3` on disk while a fresh python
# imported 2. Aimed at the verify gate that is a false-FAILED generator for a correct build
# and, worse, a false-PASS generator for a broken one.
#
# Two things are true and both were measured here before this was written:
#   * PYTHONDONTWRITEBYTECODE=1 stops the WRITE, never the READ. A child still serves the
#     stale pyc that is already on disk. It is not, alone, a fix.
#   * `compileall --invalidation-mode checked-hash` WITHOUT `-f` decides staleness by the
#     same broken timestamp rule, finds the file "up to date", exits 0 and rewrites nothing.
#     `-f` is what makes it repair rather than agree.
#
# So: rewrite every pyc to checked-hash (flags 0b11- python then compares a hash of the
# source, and mtime stops mattering), and bin any that survives in timestamp mode. Only the
# interpreter that owns a cache tag can write that tag's bytecode, so each tag is recompiled
# under its own real python; 3.11 alone would leave the slash bot's 18 cpython-312 pycs stale.
_TAG_RE = re.compile(r"^cpython-(\d)(\d+)$")


def _pyc_flags(p):
    """The pyc's flags word, or None if it is too short to have one."""
    try:
        head = p.read_bytes()[:8]
    except OSError:
        return None
    return int.from_bytes(head[4:8], "little") if len(head) >= 8 else None


def _is_hardened(flags):
    """True only for checked-hash (hash-based AND check_source). Timestamp mode is 0."""
    return flags is not None and (flags & 0b11) == 0b11


def _interpreter_for_tag(tag):
    """The python that writes `tag`'s bytecode, or None if it is not on this machine."""
    if tag == sys.implementation.cache_tag:
        return sys.executable
    m = _TAG_RE.match(tag)
    if not m:
        return None
    major, minor = m.groups()
    for cand in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
                 / f"Python{major}{minor}" / "python.exe",
                 shutil.which(f"python{major}.{minor}"),
                 shutil.which(f"python{major}{minor}")):
        if cand and Path(cand).exists():
            return str(cand)
    return None


def _clean_compile_env():
    """compileall must write BESIDE the source, in this tag's __pycache__.

    Inherit PYTHONPYCACHEPREFIX and it writes the repaired bytecode into the prefix instead,
    leaving the stale pyc exactly where the next unprefixed python will read it.
    """
    env = dict(os.environ)
    env.pop("PYTHONPYCACHEPREFIX", None)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return env


def harden_bytecode(root=None):
    """Rewrite every stale-mode .pyc under `root` to checked-hash. Returns how many.

    Idempotent, and cheap when the tree is already clean: it reads eight bytes per pyc and
    returns 0 without spawning anything. A pyc it cannot repair- an orphan whose source is
    gone, a tag whose interpreter is not installed, a source that no longer compiles- is
    DELETED. Deleting bytecode is always safe; serving the wrong bytecode never is.
    """
    root = Path(root or UTILS)
    pycs = list(root.glob("**/__pycache__/*.pyc"))
    stale = [p for p in pycs if not _is_hardened(_pyc_flags(p))]
    if not stale:
        return 0

    by_tag = {}
    for p in stale:
        parts = p.name.split(".")
        if len(parts) < 3:
            continue
        src = p.parent.parent / (".".join(parts[:-2]) + ".py")
        if src.exists():
            by_tag.setdefault(parts[-2], set()).add(str(src))

    for tag, sources in by_tag.items():
        interp = _interpreter_for_tag(tag)
        if not interp:
            _log(f"harden: no interpreter for {tag}- its {len(sources)} pyc(s) will be binned")
            continue
        try:
            subprocess.run(
                [interp, "-m", "compileall", "-q", "-f",
                 "--invalidation-mode", "checked-hash", *sorted(sources)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=HARDEN_TIMEOUT, stdin=subprocess.DEVNULL, env=_clean_compile_env())
        except Exception as e:                       # a missing/hung interpreter, never fatal
            _log(f"harden: compileall under {tag} failed ({e})")

    # Whatever is STILL in timestamp mode could not be repaired. It goes.
    fixed, binned = 0, 0
    for p in stale:
        if _is_hardened(_pyc_flags(p)):
            fixed += 1
            continue
        try:
            p.unlink()
            binned += 1
        except OSError:
            pass                                    # locked by a reader; the next pass gets it
    if binned:
        _log(f"harden: {binned} pyc(s) could not be rewritten and were deleted")
    return fixed + binned


def _verify_env():
    """The environment a verify command runs in: UTF-8, plus POSIX tools where we have them.

    It also puts the child's bytecode cache somewhere it can hold nothing stale. Both vars
    are load-bearing and neither is sufficient alone: DONTWRITEBYTECODE stops a fresh stale
    pyc being laid down, PYCACHEPREFIX stops the child READING the beside-source
    __pycache__ that another process already poisoned. An exam graded against a hub file's
    old code is worth nothing, whichever way it lands.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        PYCACHE_PREFIX.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    env["PYTHONPYCACHEPREFIX"] = str(PYCACHE_PREFIX)
    d = _coreutils_dir()
    if d:
        path = env.get("PATH", "")
        # APPEND, never prepend: a verify naming `find` or `sort` must keep getting the
        # Windows one it was written against. This only fills gaps like `rm`.
        if d.lower() not in [p.strip().lower() for p in path.split(os.pathsep)]:
            env["PATH"] = (path + os.pathsep + d) if path else d
    return env


# ---- RUNNING A COMMAND FAITHFULLY ----------------------------------------------------
# `shell=True` on Windows is `cmd.exe /c`, and cmd.exe treats a NEWLINE as a command
# SEPARATOR. So a multi-line `python -c "..."` exam ran its FIRST LINE and exited 0- every
# assertion below line one silently never executed, and the gate reported `passed`. That is
# a false-pass generator: a build could land "verified" having proven nothing. Measured on
# the 4-lane build's own sealed exam (9th July): sabotaging line 1 was caught; sabotaging
# line 5 and line 7 were both missed, exit 0.
#
# The fix is to stop handing a multi-line command to a shell that cannot hold one. A
# `python -c` source goes to a temp file and runs argv-style with no shell in the path at
# all; any other multi-line command runs a line at a time, and the first non-zero exit is
# the verdict. Both carry every line. Neither can pass on line one alone.
_PY_C_RE = re.compile(
    r'^\s*(?P<py>"[^"]+"|\S+?)\s+-c\s+(?P<q>["\'])(?P<src>.*)(?P=q)\s*$', re.S)


def python_c_source(cmd):
    """The SRC of a whole-command `python -c "SRC"`, or None if it is anything else.

    Anything else includes a compound (`rm -f x && python -c "..."`): we only rewrite a
    command we can carry whole, and guessing at a shell's own syntax is how this broke.
    """
    m = _PY_C_RE.match(str(cmd or ""))
    if not m:
        return None
    # The quote cannot reappear inside the source- cmd.exe would have ended the argument
    # there. Without this, the greedy match reads `python -c "a"\npython -c "b"` as ONE
    # command with a two-line source, and a perfectly good pair of commands looks corrupt.
    if m.group("q") in m.group("src"):
        return None
    py = m.group("py").strip('"').lower()
    stem = os.path.basename(py)
    if stem in ("py", "py.exe") or stem.startswith("python"):
        return m.group("src")
    return None


def python_c_quote(cmd):
    """The quote character a whole-command `python -c` used, or None if it is not one.

    cmd.exe does not treat `'` as a quote, so `python -c 'import sys'` reaches python as the
    bare argument `'import` and dies with SyntaxError before the exam runs- a guaranteed
    false FAIL for a build that is fine. Such a command must never see a shell.
    """
    if python_c_source(cmd) is None:
        return None
    return _PY_C_RE.match(str(cmd)).group("q")


def _interpreter(cmd):
    """The python a `python -c` command asked for. A BARE `python`/`py` means ours.

    The test is the DIRECTORY, not the stem. Matching the basename made an absolute
    `C:\\...\\Python312\\python.exe` mean the same as a bare `python`, so on 10th July a
    baxter_slash exam sealed against 3.12 ran on the triage process's 3.11, died at
    `ModuleNotFoundError: discord` with zero assertions executed, and graded a correct
    build FAILED. An exam that names a path gets that path.
    """
    m = _PY_C_RE.match(str(cmd or ""))
    py = (m.group("py").strip('"') if m else "") or ""
    if not py or not os.path.dirname(py):
        return sys.executable
    # Fail OPEN on a named path that is not on this machine: refusing a good exam is the
    # same crime as sealing a broken one, and _interpreter has no way to say "unverified".
    # Stat it every call- a machine that installs 3.12 mid-session must not stay wrong.
    return py if os.path.isfile(py) else sys.executable


def _compiles(src):
    try:
        compile(src, "<acceptance>", "exec")
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {' '.join(str(e).split())[:120]}"


# ---- THE SCRIPT-PATH SLOT ------------------------------------------------------------
# `python <path> <args>` is the other exam shape, and it has its own way of never running a
# line. `baxter_triage --verify-cmd` is invoked from PowerShell 5.1, whose native-command
# argument passing STRIPS embedded double quotes: a correctly-quoted
# `python "C:\...\Python Scripts\utils\x.py" --selftest` arrives at record_check unquoted,
# python is handed the path truncated at the space, and the gate reports a FAILED the build
# never earned. Measured 9th July on the top-hat build- rc=2, `python: can't open file
# 'C:\Users\you\Documents\Python'`, twice, over code that was correct and already live.
#
# So the script slot is checked BEFORE anything is sealed. Everything here FAILS OPEN: an
# exam we cannot reason about is sealed unchanged, exactly as before. Refusing a good exam
# is the same crime as sealing a broken one.
_PY_SAFE_FLAGS = frozenset(("-u", "-b", "-B", "-E", "-I", "-O", "-OO", "-q", "-s", "-S"))
_SHELL_OPS = frozenset(("&&", "||", "|", ";", "&", ">", ">>", "<", "2>", "2>&1"))


def _strip_quotes(tok):
    tok = str(tok or "")
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok


def _is_quoted(tok):
    tok = str(tok or "")
    return len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'"


def _resolves(path):
    """Does this argument name a file that exists- as given, or under the vault (the cwd
    every verify command runs in)?"""
    path = str(path or "").strip()
    if not path:
        return False
    try:
        if os.path.isfile(path):
            return True
        return os.path.isfile(os.path.join(str(VAULT), path))
    except (OSError, ValueError):
        return False              # a path too long or holding a NUL byte is simply not a file


def _py_script_argv(cmd):
    """`(tokens, i)` where `tokens[i]` is the script path of a plain `python <path>` command.

    Returns `(None, None)` for anything we cannot reason about, and that list is deliberately
    generous: a newline, a shell operator, a command needing a POSIX shell, a `-c` or `-m`
    (source and module, not a path), an unrecognised interpreter flag, or an argv[0] that is
    not a python. All of those seal unchanged.
    """
    cmd = str(cmd or "")
    if not cmd.strip() or "\n" in cmd or needs_posix_shell(cmd):
        return None, None
    try:
        # posix=False, ALWAYS. In posix mode shlex reads `\U` in `C:\Users\...` as an escape
        # and hands back a path with the backslashes eaten, so every real path on this machine
        # would look nonexistent and the gate would refuse every script exam it ever saw.
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        return None, None         # an unbalanced quote: not ours to judge
    if len(tokens) < 2:
        return None, None
    if any(t in _SHELL_OPS for t in tokens):
        return None, None
    stem = os.path.basename(_strip_quotes(tokens[0]).lower())
    if not (stem in ("py", "py.exe") or stem.startswith("python")):
        return None, None
    for i, tok in enumerate(tokens[1:], start=1):
        if not tok.startswith("-"):
            return tokens, i      # the first non-flag argument is the script
        if tok not in _PY_SAFE_FLAGS:
            return None, None     # -c, -m, -X ... : no script slot, or one we misread
    return None, None             # flags only, no script


def _shredded_python_c(cmd):
    """True when `python -c` has LOST the quotes around its source.

    The same PowerShell strip, on the other exam shape. `python -c "import sys; sys.exit(3)"`
    arrives as `python -c import sys; sys.exit(3)`; python takes only the first bare token as
    the source, dies with SyntaxError, and a correct build is graded FAILED. Measured 9th
    July while proving the fix- the remedy this file used to recommend was itself shredded.

    A source whose quote SURVIVED is never judged here: `python -c "print(\"hi\")"` fails
    `python_c_source` for its own reasons and must still seal.
    """
    cmd = str(cmd or "")
    if "\n" in cmd or needs_posix_shell(cmd):
        return False
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        return False
    if len(tokens) < 4 or any(t in _SHELL_OPS for t in tokens):
        return False              # a lone bare token IS a runnable source: `python -c pass`
    stem = os.path.basename(_strip_quotes(tokens[0]).lower())
    if not (stem in ("py", "py.exe") or stem.startswith("python")):
        return False
    return tokens[1] == "-c" and tokens[2][:1] not in ("\"", "'")


def _quoting_remedy(cmd):
    """How to register `cmd` so its quotes reach argv intact- in BOTH shells, which need
    OPPOSITE escaping.

    Naming only PowerShell is what caused the 9th-July false FAILED. A builder read this very
    refusal, typed the backslash-escaped form through the BASH tool, and bash- which keeps a
    backslash literal inside single quotes- stored `-File \\"C:\\Users\\...` verbatim. cmd.exe
    then handed powershell a path glued to a backslash-quote, it died on `Illegal characters
    in path` with zero assertions run, and a correct build was graded FAILED.

    So both forms are handed back, side by side, each labelled with the shell it belongs to.
    """
    return ("Escape it for the shell you register from- the two forms are opposite. "
            "PowerShell 5.1 strips the plain quotes around a native command's argument, so "
            "typing them again will NOT help: BACKSLASH-ESCAPE them inside outer SINGLE "
            "quotes and they survive intact- "
            "--verify-cmd <journal> '" + cmd.replace('"', '\\"') + "'. "
            "The Bash tool keeps a backslash LITERAL inside single quotes, so there the inner "
            "quotes must stay PLAIN- "
            "--verify-cmd <journal> '" + cmd + "'.")


# ---- THE SHELL-AGNOSTIC PATH FAULT ---------------------------------------------------
# `_py_script_argv` returns (None, None) for any argv[0] that is not a python, so until now a
# `powershell -File ...`, a `node ...` or a bare exe was sealed with NO path vetting at all.
# On 9th July lane 9 stored `powershell -NoProfile -ExecutionPolicy Bypass -File \"<a path
# with a space>\"`- the backslashes literal, kept by bash inside its single quotes. record_check
# found no python, waved it through, and at grade time powershell exited 4294770688 on `Illegal
# characters in path` having run zero assertions.
#
# Two shapes are refused here, for EVERY argv[0]:
#   1. a token that looks like a PATH and carries a backslash against its quote;
#   2. an unquoted path torn at a space in the script slot of a non-python argv[0].
#
# Everything else FAILS OPEN, and the `_looks_like_path` guard is what keeps it honest: a
# `powershell -Command "if ($x -eq \"a\") { exit 1 }"` body throws off tokens that carry a
# backslash-quote and are not paths, and refusing a good exam is the same crime as sealing a
# broken one.
_SCRIPT_HOSTS = frozenset(("node", "node.exe", "deno", "deno.exe"))
_FILE_FLAGS = frozenset(("-file", "-f"))
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _backslash_wrapped(tok):
    """Does a backslash sit against this token's opening or closing quote?

    That is the signature of an escaping meant for one shell and swallowed by another: the
    quote was escaped for PowerShell, bash kept the backslash, and the backslash is now part
    of the argument. No shell on this machine puts one there on purpose.
    """
    tok = str(tok or "")
    return any(tok.startswith("\\" + q) or tok.endswith("\\" + q) for q in ("\"", "'"))


def _looks_like_path(tok):
    """Is this token plausibly a FILESYSTEM PATH, rather than a fragment of shell or source?

    Deliberately narrow. Only a drive-letter prefix (`C:\\` / `C:/`) or a directory separator
    counts, and only after any quote wrapper is peeled off. Everything else- an expression, a
    flag, a bare word- reads as not-a-path and seals, because this predicate is the only thing
    standing between the refusal below and a `powershell -Command` body.
    """
    t = str(tok or "")
    for q in ("\"", "'"):
        if t.startswith("\\" + q):
            t = t[2:]
        if t.endswith("\\" + q):
            t = t[:-2]
    t = _strip_quotes(t).strip("\"'")
    if not t:
        return False
    return bool(_DRIVE_RE.match(t)) or "/" in t or "\\" in t


def _is_python_source_exam(tokens):
    """`python -c <source>`: the argument is SOURCE, not a path, and its backslash-quotes are
    python's own escaping. `_shredded_python_c` already judges that shape; scanning its tokens
    for path faults would only ever produce false refusals."""
    if len(tokens) < 2:
        return False
    stem = os.path.basename(_strip_quotes(tokens[0]).lower())
    if not (stem in ("py", "py.exe") or stem.startswith("python")):
        return False
    return tokens[1] in ("-c", "-m")


def _script_slot(tokens):
    """The index of the script-path argument for a NON-python argv[0], or None.

    `powershell`/`pwsh` -> the token after `-File`/`-f`; a `-Command` body has no script slot
    and returns None. `node`/`deno` -> the first non-flag argument. Anything else is
    unrecognised and returns None, so a bare exe's first argument is never assumed to be a
    path.
    """
    if len(tokens) < 2:
        return None
    stem = os.path.basename(_strip_quotes(tokens[0]).lower())
    if stem in _PS_HOSTS:
        for i, tok in enumerate(tokens[1:], start=1):
            if tok.lower() in _FILE_FLAGS:
                return i + 1 if i + 1 < len(tokens) else None
        return None
    if stem in _SCRIPT_HOSTS:
        for i, tok in enumerate(tokens[1:], start=1):
            if not tok.startswith("-"):
                return i
        return None
    return None


def shell_agnostic_path_fault(cmd):
    """The refusal reason for a path fault this command can never survive, or None.

    Fails open on a newline, on `needs_posix_shell` (bash reads `\\"` as a legitimate escape,
    so the shape is not a fault there), on an unbalanced quote, and on a compound- exactly the
    set `_py_script_argv` already declines to judge.
    """
    cmd = str(cmd or "")
    if not cmd.strip() or "\n" in cmd or needs_posix_shell(cmd):
        return None
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        return None
    if len(tokens) < 2 or any(t in _SHELL_OPS for t in tokens):
        return None

    if not _is_python_source_exam(tokens):
        for tok in tokens[1:]:
            if _backslash_wrapped(tok) and _looks_like_path(tok):
                clean = cmd.replace('\\"', '"').replace("\\'", "'")
                return ("the path argument `" + tok + "` carries a LITERAL backslash against "
                        "its quote, so the program is handed `\\\"C:\\...` and dies on an "
                        "illegal path before a single assertion runs. The PowerShell escaping "
                        "was typed into a shell that keeps the backslash. " + _quoting_remedy(clean))

    i = _script_slot(tokens)
    if i is not None:
        whole = _unquoted_space_path(tokens, i)
        if whole:
            rebuilt = " ".join(tokens[:i] + ['"' + whole + '"']
                               + tokens[i + len(whole.split(" ")):])
            return ("the script path is UNQUOTED and broken at a space: `" + tokens[0]
                    + "` is handed `" + tokens[i] + "` and dies before a line runs. The file "
                    "you meant is `" + whole + "`. " + _quoting_remedy(rebuilt))
    return None


def _unquoted_space_path(tokens, i):
    """The real path a PowerShell-shredded script argument was TORN OUT OF, or None.

    `tokens[i]` is the fragment python would be handed. If it already resolves, nothing is
    wrong. Otherwise rejoin it with the following tokens one at a time, on a single space,
    and return the first join that is a real file: that reconstruction IS the signature of a
    quote-stripped path, and it names the file the builder meant.

    Only ever applied to the script slot. Applied to any token it would refuse a good exam
    that names an output file the run is about to create (`python x.py --out logs/new.json`).
    """
    tok = tokens[i]
    if _is_quoted(tok):
        return None               # its quotes survived; a space-join would be fiction
    if _resolves(tok):
        return None
    acc = tok
    for nxt in tokens[i + 1:]:
        acc = acc + " " + _strip_quotes(nxt)
        if _resolves(acc):
            return acc
    return None


# ---- THE EXAM THAT CANNOT FAIL -------------------------------------------------------
# The other half of TRUST BUT VERIFY, and the one the rule text cannot reach. A builder in a
# hurry declares `python -c "pass"` as its proof; the gate runs it, reads exit 0, and stamps
# the build `passed`. That is not a weak check, it is a FALSE one- it mints the very green
# the gate exists to withhold, and it is indistinguishable in the ledger from a real pass.
#
# The test is STATIC: it reads the exam's AST, it does not run it. So it can only refuse a
# PROVABLY dead exam. `python "x.py" --selftest` is accepted on faith that the selftest
# asserts something, and a `python -c` calling out to anything at all is accepted because
# that call may raise. That asymmetry is deliberate and load-bearing: a FALSE REFUSAL is the
# expensive failure mode here. It sends a builder round the repair loop over an exam that was
# honest all along, and it burns the owner's window doing it. Refusing a good exam is the same
# crime as sealing a broken one.
#
# Hence: every doubt resolves to "not vacuous". Only a source built entirely out of things
# that provably cannot fail is refused.
_SAFE_CALLS = frozenset(("print",))          # calls that cannot raise, given legal args
_EXIT_CALLS = frozenset(("exit", "quit", "sys.exit", "os._exit", "os.abort"))
# Absent on <3.10: then EVERY import reads as "may raise", and nothing importing is ever
# called vacuous. That is the fail-open direction, and it is the correct one.
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))


def _dotted(node):
    """`sys.exit` from a call target's AST, or '' for anything less straightforward."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _exit_can_fail(call):
    """Can this exit call leave a NON-ZERO code? `sys.exit()` and `sys.exit(0)` cannot.

    Without this, the refusal message teaches its own way round: a builder reads 'no exit
    with a failing code' and declares `python -c "import sys; sys.exit(0)"`, which is `pass`
    wearing a hat. A computed code (`sys.exit(rc)`) is unknowable statically- so it counts
    as able to fail, and seals.
    """
    if not call.args:
        return False                                  # exit() / sys.exit() -> code 0
    a = call.args[0]
    if isinstance(a, ast.Constant) and a.value in (0, None):
        return False                                  # sys.exit(0), sys.exit(None) -> code 0
    return True


def _imports_only_stdlib(node):
    """True when every module this statement imports is guaranteed present.

    An `import baxter_rules` CAN fail- ImportError, and a real one, since the build under
    test is what would break it. An `import sys` cannot. So the first is a failure primitive
    and the second is not, and `python -c "import baxter_rules"` seals while
    `python -c "import sys"` is refused. A relative import (module None) reads as able to
    fail, which is the safe direction.
    """
    if isinstance(node, ast.ImportFrom):
        root = (node.module or "").split(".")[0]
        return bool(root) and root in _STDLIB
    return all(a.name.split(".")[0] in _STDLIB for a in node.names)


_VACUOUS_WHY = (
    "this exam CANNOT FAIL, so it proves nothing and sealing it would mint a false green: "
    "the `python -c` source has no assert, no raise, no exception it could let through, and "
    "no exit with a failing code. Declare one that can go RED- run the module's --selftest, "
    "drive the changed code end to end and assert on its real output, or grep the LIVE log "
    "for what the build actually wrote. Then sabotage your own code and watch the exam fail "
    "before you seal it: an exam you have never seen go red is not a check, it is a wish."
)


def _bare_python_c_source(cmd):
    """The SRC of an UNQUOTED one-token `python -c SRC`, e.g. `python -c pass`.

    Complete and runnable as it stands (a single bare token is a legal source), so
    `_shredded_python_c` rightly leaves it alone- and `python_c_source` rightly does not
    match it, having no quote to match. It is also the shortest exam that cannot fail, which
    puts it squarely in this function's business. Judged here and nowhere else.
    """
    cmd = str(cmd or "")
    if "\n" in cmd or needs_posix_shell(cmd):
        return None
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        return None
    if len(tokens) != 3 or any(t in _SHELL_OPS for t in tokens):
        return None
    stem = os.path.basename(_strip_quotes(tokens[0]).lower())
    if not (stem in ("py", "py.exe") or stem.startswith("python")):
        return None
    if tokens[1] != "-c" or _is_quoted(tokens[2]):
        return None
    return tokens[2]


def vacuous_exam(cmd):
    """Is this exam INCAPABLE of exiting non-zero? Returns (vacuous, reason).

    Judges ONLY a whole-command `python -c` source, because that is the only exam shape whose
    behaviour is fully readable from its text. A script-path exam, a compound, a pytest run,
    a node command- anything this cannot parse is NEVER vacuous, and seals unchanged.

    A source is vacuous when every last thing in it provably cannot fail: no `assert`, no
    `raise`, no `try` (which catches, and re-raises, on purpose), no subscript (IndexError,
    KeyError- a real way to assert a shape), no exit carrying a failing code, no import of a
    module that might be absent, and no call to anything but a handful of calls that cannot
    raise. One node this does not recognise is enough for the whole exam to seal.
    """
    src = python_c_source(cmd)
    if src is None:
        src = _bare_python_c_source(cmd)
    if src is None:
        return False, ""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False, ""     # not ours to refuse: `_compiles` owns that, with a better reason
    if not tree.body:
        return True, _VACUOUS_WHY
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assert, ast.Raise, ast.Try, ast.Subscript)):
            return False, ""
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if not _imports_only_stdlib(node):
                return False, ""
            continue
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in _EXIT_CALLS:
                if _exit_can_fail(node):
                    return False, ""
                continue
            if name not in _SAFE_CALLS:
                return False, ""     # any other call may raise; it gets the benefit of doubt
    return True, _VACUOUS_WHY


# ---- THE PROBE THAT READS A STALE EXIT CODE -------------------------------------------
# PowerShell does not set `$LASTEXITCODE` when a native command fails to LAUNCH. It raises
# CommandNotFound/NativeCommandFailed and leaves the variable holding whatever the PREVIOUS
# native command exited with. So this shape:
#
#     & $exe -c "import cv2"
#     if ($LASTEXITCODE -ne 0) { exit 1 }
#
# never observes the child at all when `$exe` cannot be executed- and the Store python under
# `C:\Program Files\WindowsApps` is ACL-denied to an unelevated exec, so it routinely cannot.
# On 9th July the preceding command exited 0, the clause read that 0 back, and a sealed exam
# reported GREEN having run nothing. Reverse the preceding exit code and the same clause
# reports "the revived loop has no cv2"- a false RED that brands a sound build FAILED for a
# reason with no connection to the build. One defect, two faces.
#
# So the shape is refused at seal time, wherever it appears: in a `verify` string, or inside a
# .ps1 the exam merely names (the offender lived in a FILE, not in the command).
#
# WHAT THIS IS NOT. It is not a PowerShell parser, and an OVER-BROAD detector here is worse
# than none: it would refuse every honest exam that reads `$LASTEXITCODE`, and no build would
# ever seal again. So a read is refused ONLY when the nearest preceding exit-code event is a
# native invocation whose launch can fail. A read after a cmdlet, after a pipeline into a
# cmdlet, or with nothing before it at all, seals unchanged- as does a `.ps1` invoked with `&`,
# which throws rather than failing to launch. Every doubt resolves to "not stale".
_LEC_RE = re.compile(r"\$LASTEXITCODE\b", re.I)

# `&` opens a native call when it follows a statement boundary, an assignment, or a quote
# (`powershell -Command "& $exe ..."`). It does NOT when it follows `>`, which is `2>&1`.
_CALLOP_RE = re.compile(
    r"""(?:^|[\n;|(={"'])\s*&\s*(?P<t>\$[A-Za-z_]\w*|'[^']*'|"[^"]*"|[^\s;|&<>)]+)""")
# A bare native call can sit anywhere a command can, INCLUDING downstream of a pipe:
# `Get-Content x | python filter.py` can still fail to launch.
_NATIVE_CMD_RE = re.compile(r"""(?:^|[\n;|{}])\s*(?P<t>[A-Za-z_][\w.+-]*)""")
# A cmdlet only CLEARS the hazard when it leads a real statement. `-File (Join-Path $S 'x')`
# is an ARGUMENT: it is evaluated BEFORE the native command it feeds, though it sits after it
# in the text. Counting it as a statement let `baxter_verify_watchdog_revive.ps1` hide its own
# stale read behind a Join-Path, and the detector called the file clean. Measured 9th July.
# `|` is excluded for the same reason a pipeline is one statement: `& python x | Out-String`
# leaves $LASTEXITCODE exactly as the failed launch left it.
_CMDLET_STMT_RE = re.compile(r"""(?:^|[\n;{}])\s*(?P<t>[A-Za-z]+-[A-Za-z]\w*)\b""")
_CMDLET_RE = re.compile(r"^[A-Za-z]+-[A-Za-z]\w*$")
_SCRIPT_REF_RE = re.compile(
    r'''"([^"]+\.(?:ps1|cmd|bat))"|'([^']+\.(?:ps1|cmd|bat))'|(\S+\.(?:ps1|cmd|bat))''', re.I)

_PS_KEYWORDS = frozenset((
    "if", "elseif", "else", "while", "for", "foreach", "do", "switch", "try", "catch",
    "finally", "return", "exit", "break", "continue", "function", "param", "filter",
    "throw", "begin", "process", "end", "in", "not", "and", "or", "data", "trap",
))
_NATIVE_EXTS = (".exe", ".com", ".bat", ".cmd")
_NATIVE_STEMS = frozenset((
    "python", "python3", "pythonw", "py", "powershell", "pwsh", "cmd", "node", "npm",
    "npx", "git", "curl", "schtasks", "cscript", "wscript", "robocopy", "tasklist",
    "taskkill", "reg", "icacls", "adb", "ffmpeg", "docker",
))

_STALE_WHY = (
    "this exam reads $LASTEXITCODE after `{tok}`, a NATIVE command whose LAUNCH can fail. "
    "PowerShell does not set $LASTEXITCODE when a launch fails- it raises and leaves the "
    "variable holding the PREVIOUS native command's code. So the clause silently grades some "
    "earlier command: GREEN when that one exited 0 (the child never ran), and a confidently "
    "wrong RED the moment it did not. Probe with Start-Process -PassThru instead, cache "
    "$p.Handle, then $p.WaitForExit() and read $p.ExitCode- a failed LAUNCH throws and is "
    "distinguishable from a real non-zero EXIT. utils/baxter_probe.ps1 does exactly this and "
    "exits 127 with LAUNCHFAIL on stdout when the child never ran."
)


def _strip_ps_noise(text):
    """PowerShell text with here-strings and comments blanked out.

    Load-bearing: the two gate scripts this build rewrote DESCRIBE the offending shape in
    their own headers, `$LASTEXITCODE` and all. A detector that read comments could never
    call either file clean, so the remedy could never be written down beside the code.
    """
    text = re.sub(r"@(['\"])[\s\S]*?\1@", " ", text)      # here-strings
    text = re.sub(r"<#[\s\S]*?#>", " ", text)             # block comments
    out = []
    for line in text.splitlines():
        quote, cut = None, len(line)
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1] not in "$`"):
                cut = i                                   # a `#` outside quotes ends the line
                break
        out.append(line[:cut])
    return "\n".join(out)


def _is_native_target(tok):
    """Can invoking `tok` fail to LAUNCH, leaving $LASTEXITCODE untouched?

    A `$var` target is unknowable, so it counts as native- that is the exact shape that
    started this. A `.ps1` does not: PowerShell runs it in-process and a missing one throws,
    so `& probe.ps1; exit $LASTEXITCODE` is honest and must keep sealing.
    """
    tok = _strip_quotes(str(tok or "").strip())
    if not tok:
        return False
    if tok.startswith("$"):
        return True
    low = os.path.basename(tok).lower()
    if low.endswith(".ps1"):
        return False
    if low.endswith(_NATIVE_EXTS):
        return True
    return low.split(".")[0] in _NATIVE_STEMS


def _exit_code_events(clean):
    """Every point in the text that decides what a later $LASTEXITCODE read sees.

    'native' -> a launch that can fail, and so can leave the variable stale.
    'safe'   -> a cmdlet. It does not set $LASTEXITCODE either, but it is not the hazard this
                refuses, and treating it as clearing the flag is what keeps the detector from
                refusing honest exams. Fail open, deliberately.
    An unrecognised bare word (a function like `Check`) is NO event at all: it must not clear
    a native launch two lines above it, which is precisely how `.baxter_verify_boot.ps1` hid
    its own stale read inside `Check ($LASTEXITCODE -eq 0 ...)`.
    """
    events = []
    for m in _CALLOP_RE.finditer(clean):
        tok = m.group("t")
        if _is_native_target(tok):
            events.append((m.start("t"), "native", _strip_quotes(tok)))
    for m in _NATIVE_CMD_RE.finditer(clean):
        tok = m.group("t")
        if _CMDLET_RE.match(tok) or tok.lower() in _PS_KEYWORDS:
            continue
        if _is_native_target(tok):
            events.append((m.start("t"), "native", tok))
    for m in _CMDLET_STMT_RE.finditer(clean):
        events.append((m.start("t"), "safe", m.group("t")))
    # A native and a safe event cannot share a position, so the sort is total on position.
    events.sort(key=lambda e: e[0])
    return events


def stale_exitcode_probe(text):
    """Does this PowerShell text read $LASTEXITCODE after a launch that can fail?

    Takes a verify string OR a whole .ps1 file. Returns (bad, reason).
    """
    text = str(text or "")
    if not _LEC_RE.search(text):
        return False, ""                     # the overwhelmingly common case, answered free
    clean = _strip_ps_noise(text)
    reads = [m.start() for m in _LEC_RE.finditer(clean)]
    if not reads:
        return False, ""                     # every mention was a comment: it is documentation
    events = _exit_code_events(clean)
    for r in reads:
        prior = [e for e in events if e[0] < r]
        if prior and prior[-1][1] == "native":
            return True, _STALE_WHY.format(tok=prior[-1][2])
    return False, ""


def _referenced_scripts(cmd):
    """Existing .ps1/.cmd/.bat files this command names.

    The exam that started this did not CARRY the offending shape- it named a script that did.
    A detector that only ever read the `verify` string would have sealed it without a murmur.
    """
    found = []
    for m in _SCRIPT_REF_RE.finditer(str(cmd or "")):
        p = m.group(1) or m.group(2) or m.group(3)
        if not p:
            continue
        p = _strip_quotes(p)
        cand = p if os.path.isabs(p) else os.path.join(str(VAULT), p)
        for c in (p, cand):
            if os.path.isfile(c) and c not in found:
                found.append(c)
    return found


_PS_HOSTS = frozenset(("powershell", "powershell.exe", "pwsh", "pwsh.exe"))


def _is_powershell_command(cmd):
    """Is this command's own text handed to PowerShell to EXECUTE?

    Only then can the text carry the hazard. A `python -c` exam is PYTHON: its source may
    quote `$LASTEXITCODE` and `cmd.exe` as string DATA- this build's own sealed acceptance
    test writes the offending .ps1 to a temp file in order to reproduce it- and refusing that
    would be the over-broad detector eating the very exam that proves it works. Measured: the
    first cut of this refused its own acceptance test.

    Judged on the FIRST token only. `powershell.exe` appearing anywhere in a python source is
    data, not a shell.
    """
    try:
        tokens = shlex.split(str(cmd or ""), posix=False)
    except ValueError:
        return False
    if not tokens:
        return False
    return os.path.basename(_strip_quotes(tokens[0])).lower() in _PS_HOSTS


def stale_exitcode_in_cmd(cmd):
    """The command's PowerShell text (if any) AND every script it names. Returns (bad, reason).

    A `.ps1` is probed whoever names it: the offender that prompted this build lived in a FILE
    the exam merely referenced, not in the sealed `verify` string.
    """
    if _is_powershell_command(cmd):
        bad, why = stale_exitcode_probe(cmd)
        if bad:
            return True, why
    for script in _referenced_scripts(cmd):
        try:
            with open(script, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            continue                          # unreadable: not ours to refuse
        bad, why = stale_exitcode_probe(body)
        if bad:
            return True, f"the script `{script}` this exam runs is unsound: {why}"
    return False, ""


# ---- THE QUEUE AS IT REALLY IS -------------------------------------------------------
# The pump POPS an entry out of `.baxter_task_queue.json` when it hands it to a lane. So
# the queue FILE is only half the queue: the other half is running. An exam that asserts
# "entry X is in `queue_read()`" therefore reads a build that STARTED as a build that
# VANISHED, and grades a correct build FAILED the moment its own fix lets the pump place
# the work. That is what killed the hub-region exam (94de27f2) on 9th July: three of its
# ten entries went to lanes BECAUSE the re-declaration worked.
#
# `.baxter_hubregion_plan/verify.py` solved this privately, in a local `_in_flight()`.
# Solved privately, it is not solved: the NEXT exam is written blind again. So the view
# lives here, where both the seal (`baxter_orch.seal_acceptance`) and the gate
# (`run_verify`) can reach it, and where any exam can `import baxter_verify`.
_u = None


def _usage():
    """baxter_usage, imported late. Nothing else here needs it, and a module-scope import
    would drag the governor into every `python -c` exam that merely wants a command run."""
    global _u
    if _u is None:
        if str(UTILS) not in sys.path:
            sys.path.insert(0, str(UTILS))
        import baxter_usage as _mod
        _u = _mod
    return _u


def in_flight_entries(journals=None):
    """{id: entry} for every entry the pump has handed to a LIVE lane.

    `journals` is injectable for tests; by default it is `baxter_usage.lane_journals()`,
    which already excludes shelved (`.failed`), parked (`.parked`) and non-heartbeating
    journals. An unreadable journal (entry=None) holds a lane but names no entry- it
    contributes nothing here rather than becoming a phantom id."""
    rows = journals if journals is not None else _usage().lane_journals()
    out = {}
    for _rf, e in rows:
        if isinstance(e, dict) and e.get("id"):
            out[str(e["id"])] = e
    return out


def queue_view(entries=None, journals=None):
    """The whole queue: PENDING rows plus the entries running on lanes, deduped by id.

    Every row carries `_where` ('queue' | 'lane'), and a lane row carries `_journal`. The
    PENDING row wins a collision: the queue file is the live record, a lane journal is the
    snapshot the pump took when it spawned. Rows are copies- an exam may not scribble on
    the queue through this.

    This is what an acceptance exam should audit. `queue_read()` alone cannot tell "the
    build dropped it" from "the build worked and the pump took it"."""
    # short glanceable name for every row (10th July). The three-step chain is the one the
    # enqueue sites carry (baxter_usage.py:2251, :2301): the first two steps go through
    # sys.path and both miss for a caller that loaded THIS file by path- which is how every
    # sealed exam loads it- so the third file-loads the sibling out of utils/. The literal
    # import statements stay: baxter_imports.closure() is a static ast walk, and they are the
    # only reason baxter_name sits in the closure at all.
    try:
        from utils import baxter_name
    except ImportError:
        try:
            import baxter_name
        except ImportError:                  # path-loaded: no utils anywhere on sys.path
            baxter_name = _usage()._sibling("baxter_name")
    pend = list(entries) if entries is not None else _usage().queue_read()
    view, seen = [], set()
    for e in pend:
        if not isinstance(e, dict):
            continue
        qid = str(e.get("id") or "")
        if qid:
            if qid in seen:
                continue
            seen.add(qid)
        row = dict(e)
        row["_where"] = "queue"
        row.setdefault("name", baxter_name.name_for(e.get("task", "")))
        view.append(row)
    for rf, e in (journals if journals is not None else _usage().lane_journals()):
        if not isinstance(e, dict):
            continue
        qid = str(e.get("id") or "")
        if not qid or qid in seen:
            continue
        seen.add(qid)
        row = dict(e)
        row["_where"] = "lane"
        row["_journal"] = str(rf)
        row.setdefault("name", baxter_name.name_for(e.get("task", "")))
        view.append(row)
    return view


_QUEUE_READ_RE = re.compile(r"\bqueue_read\s*\(")
_QUEUE_SIGHTED_RE = re.compile(r"\b(queue_view|in_flight_entries|lane_journals)\s*\(")
# An exam that OWNS its queue reads a FIXTURE, and no pump will ever pop a row out from
# under it. `baxter_queue_ack_selftest.py` repoints `TASK_QUEUE` at a temp file and drives
# fourteen `queue_read()` calls against it. Condemning that would refuse a sound selftest
# at its seal and downgrade its real failures to `unverified`- a gate blinded by its own
# blindness check. Two sealed exams in the live estate were flagged this way before it.
_QUEUE_FIXTURE_RE = re.compile(
    r"\b(TASK_QUEUE|QUEUE|QUEUE_LOCK)\s*=(?!=)"       # repoints the queue file
    r"|\bqueue_write\s*\("                             # writes the queue it then reads
    r"|\bqueue_read\s*=(?!=)")                         # stubs the reader outright
_PY_REF_RE = re.compile(r'"([^"\n]+?\.py)"' r"|'([^'\n]+?\.py)'" r"|(\S+\.py)", re.I)
_EXAM_SRC_CAP = 200_000     # a source larger than this is not an exam


def _named_python_sources(cmd):
    """[(label, source)] for every .py file this command names AND which exists.

    The offending exam rarely carries its own text: it says `python "<exam>.py"`. A detector
    that only read the `verify` string would miss every file exam ever sealed. A file that
    does not exist yet is silently skipped- the planner seals before the build has written
    it, which is exactly why the builder's `record_check` re-checks with the file on disk."""
    out = []
    for m in _PY_REF_RE.finditer(str(cmd or "")):
        p = m.group(1) or m.group(2) or m.group(3)
        if not p:
            continue
        p = _strip_quotes(p)
        for cand in (p, p if os.path.isabs(p) else os.path.join(str(VAULT), p)):
            if not os.path.isfile(cand):
                continue
            try:
                if os.path.getsize(cand) > _EXAM_SRC_CAP:
                    break
                out.append((os.path.basename(cand),
                            Path(cand).read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
            break
    return out


def queue_blind_exam(cmd):
    """(blind, why). Does this exam judge the build against the QUEUE FILE ALONE?

    Blind means all three: its source calls `queue_read(`, it never once consults the lanes
    (`queue_view(`, `in_flight_entries(`, `lane_journals(`), and it never installs a queue
    of its own to read. Such an exam cannot tell a dropped entry from a running one, so a
    build that succeeds in placing work on a lane fails its own acceptance test. It is
    refused at both seals, and an already-sealed one grades `unverified` rather than
    condemning a correct build.

    Both shapes are read: the inline `python -c` source, and the text of any .py file the
    command names and which is on disk."""
    sources = []
    src = python_c_source(cmd)
    if src:
        sources.append(("the `python -c` source", src))
    sources.extend(_named_python_sources(cmd))
    for label, text in sources:
        if (_QUEUE_READ_RE.search(text) and not _QUEUE_SIGHTED_RE.search(text)
                and not _QUEUE_FIXTURE_RE.search(text)):
            return True, (
                f"{label} asserts on `queue_read()` and never looks at a lane. The pump POPS "
                f"a running entry out of the queue file, so this exam reads a build that "
                f"STARTED as one that VANISHED. Audit `baxter_verify.queue_view()` instead- "
                f"it merges the pending rows with the entries on live lanes.")
    return False, ""


def vet_verify_cmd(cmd, paths_must_exist=False):
    """Can this command actually RUN? Returns (cmd, reason); cmd is None if it never can.

    The planner seals an exam it has never proved runnable. On 9th July it wrote one with
    six literal backslash-n sequences and zero real newlines, so Python raised SyntaxError
    before a single assertion ran- a guaranteed FAIL for a build that was correct. A sealed
    exam that cannot run is worse than no exam: it reads as a verdict and it is noise.

    A `python -c` source is compile-checked. A double-escaped source is REPAIRED to real
    newlines when the repair compiles; one that compiles neither way is rejected, and the
    caller must refuse to seal it.

    A `python <path>` exam has its script slot checked instead. A path shredded by
    PowerShell's quote-stripping is ALWAYS refused- it can never run, whoever wrote it and
    whenever. A script that merely does not exist is refused only when `paths_must_exist`:
    the planner seals before the build has written its files, so `python "<the selftest this
    build is about to create>" --selftest` must still seal, while a BUILDER declaring at its
    exit has no such excuse- its files are on disk by then.

    A path fault NO shell survives is refused on both branches and for EVERY argv[0], python or
    not: a backslash left standing against a path's quote, or an unquoted path torn at a space
    in the `-File`/script slot. Until 10th July only a python argv[0] was vetted at all, so
    `powershell -File \\"<a path with a space>\\"` sealed clean and graded a correct build
    FAILED. See `shell_agnostic_path_fault`.

    An exam that CANNOT FAIL is refused on that same `paths_must_exist` branch, and only
    there. The flag already means precisely "a BUILDER is declaring at its exit", which is
    the moment a false proof is minted. The planner's seal path must stay untouched: it vets
    with `paths_must_exist=False` before its files exist, and `seal_acceptance` in baxter_orch
    calls it that way. So a PLANNER can still seal a vacuous acceptance test- this closes the
    BUILDER's hole, not both. See `vacuous_exam`.

    An exam that reads a STALE $LASTEXITCODE is refused on BOTH branches, unlike `vacuous_exam`
    above, and BEFORE every other check so the refusal names the real defect rather than some
    quoting symptom. The cv2 probe that prompted this was sealed by a PLANNER, at
    `paths_must_exist=False`: gating it on the builder's exit alone would leave the exact hole
    open. Nor does it depend on the exam's files existing- it is a property of the text.
    See `stale_exitcode_probe`.
    """
    cmd = str(cmd or "").strip()
    if not cmd:
        return "", ""
    stale, why = stale_exitcode_in_cmd(cmd)
    if stale:
        return None, why
    src = python_c_source(cmd)
    if src is not None:
        ok, err = _compiles(src)
        note = ""
        if not ok:
            if "\\n" in src:
                fixed = src.replace("\\n", "\n")
                if _compiles(fixed)[0]:
                    cmd = cmd.replace(src, fixed)
                    note, ok = "repaired literal \\n escapes into real newlines", True
            if not ok:
                return None, f"the `python -c` source does not compile ({err})"
        # Judged on the REPAIRED command- that is the one that would actually run.
        if paths_must_exist:
            dead, why = vacuous_exam(cmd)
            if dead:
                return None, why
        return cmd, note
    # The branch above always returns, so only NON-quoted commands reach here- among them
    # `python -c pass`, which has no quote for `python_c_source` to match, is complete and
    # runnable as it stands, and is the shortest exam in existence that cannot fail.
    if paths_must_exist:
        dead, why = vacuous_exam(cmd)
        if dead:
            return None, why
    if _shredded_python_c(cmd):
        return None, (
            "the `python -c` source is UNQUOTED: python reads only the first bare token as "
            "the source and dies with SyntaxError before a line runs. "
            + _quoting_remedy(cmd))
    why = shell_agnostic_path_fault(cmd)
    if why:
        return None, why
    tokens, i = _py_script_argv(cmd)
    if i is None:
        return cmd, ""            # nothing we can prove without running it
    whole = _unquoted_space_path(tokens, i)
    if whole:
        rebuilt = " ".join([tokens[0], '"' + whole + '"'] + tokens[i + len(whole.split(" ")):])
        return None, (
            f"the script path is UNQUOTED and broken at a space: python is handed "
            f"`{tokens[i]}` and dies before a line runs. The file you meant is `{whole}`. "
            + _quoting_remedy(rebuilt))
    script = _strip_quotes(tokens[i])
    if paths_must_exist and not _resolves(script):
        return None, (f"no such file: `{script}`. The exam names a script that is not on "
                      f"disk, so it would exit 2 without running a line.")
    return cmd, ""


def _run_once(cmd, timeout=VERIFY_TIMEOUT, env=None):
    """Run one verify/check command so that ALL of it runs. Returns (rc, output)."""
    env = env or _verify_env()
    kw = dict(cwd=str(VAULT), timeout=timeout, stdin=subprocess.DEVNULL,
              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    src = python_c_source(cmd)
    # A temp file and argv-style exec puts NO shell in the path at all. Required when the
    # source spans lines (cmd.exe would run line one and call it a pass) and equally when it
    # was single-quoted (cmd.exe would hand python a broken argument and call it a defect).
    if src is not None and ("\n" in src or python_c_quote(cmd) == "'"):
        fd, path = tempfile.mkstemp(suffix=".py", prefix="baxter_verify-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(src)
            r = subprocess.run([_interpreter(cmd), path], **kw)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")
    if needs_posix_shell(cmd):
        sh = _bash()
        if not sh:
            # NEVER fall through to cmd.exe here. needs_posix_shell means cmd.exe would
            # shatter or reject this command, and a shattered exam returns rc=1 that reads
            # as a code defect. Say plainly that the environment, not the build, is broken.
            return 127, ("no bash available to run this exam: it needs a POSIX shell "
                         "(git bash was not found beside the coreutils we resolve), and "
                         "cmd.exe cannot run it faithfully. This is an environment fault, "
                         "not a failing build.")
        # Hand it to a shell that can hold it, WHOLE- a `for` loop cannot survive being run
        # a line at a time any more than it survives cmd.exe.
        #
        # `set -e` only for a multi-line exam with no `$?` in it: there, the contract is the
        # same as the cmd path below ("these commands, in order, all must pass"), and without
        # -e bash would report only the LAST line's status- a fresh false-pass generator. An
        # exam that reads `$?` is inspecting exit codes on purpose, so -e would abort it at
        # the first non-zero it meant to catch. A single-line exam already says what it means
        # with its own `&&` chain.
        script = cmd if ("$?" in cmd or "\n" not in cmd) else "set -e\n" + cmd
        # Git bash rewrites `/query` into a Windows path before a native exe ever sees it, so
        # `schtasks /query` silently becomes an argument error. An exam that shells out to a
        # Windows tool must get the switches it wrote.
        env = dict(env)
        env.setdefault("MSYS2_ARG_CONV_EXCL", "*")
        kw["env"] = env
        r = subprocess.run([sh, "-c", script], **kw)
        return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")
    if "\n" in cmd:
        # A plain multi-line shell command means "these commands, in order, all must pass".
        # cmd.exe would run line one and stop; run each line and let the first failure speak.
        out = ""
        for line in [ln for ln in cmd.splitlines() if ln.strip()]:
            r = subprocess.run(line, shell=True, **kw)
            out += (r.stdout or "") + "\n" + (r.stderr or "")
            if r.returncode != 0:
                return r.returncode, out
        return 0, out
    r = subprocess.run(cmd, shell=True, **kw)
    return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")


# ---- THE MUTATION FENCE --------------------------------------------------------------
# A lane proving its own exam can go red holds a file BROKEN on disk for a few seconds. If that
# file is a hub, every sibling gate that imports it dies in the module body- before one
# assertion runs- and is graded FAILED for a fault it never caused. Measured 10th July:
# `baxter_slash.py` was wrong on disk for ~8s and the lane-ui build was condemned twice.
#
# `baxter_hub_edit.mutating()` now marks such a window. Here we consult it, and the ONLY three
# things this may ever do are: wait for an open window before starting, re-run ONCE when a red
# coincided with one, and downgrade a surviving red to `unverified` while a window is STILL
# open. It must never turn a red into a green.
#
# THE FAILURE MODE THAT MATTERS is the fence becoming a laundry for real defects. Two things
# stop it. A passing exam never waits and never re-runs, so nothing about the fence can mint a
# green. And a red with no window anywhere- none before, none after, no epoch movement- returns
# UNTOUCHED, so an ordinary failing build fails exactly as it did yesterday.
_hub = False        # False = not yet tried; None = unavailable; else the module


def _hub_edit():
    """baxter_hub_edit, imported late and tolerantly.

    If it will not import- or is an older copy with no fence- every gate behaves EXACTLY as it
    did before this existed. The fence may never be the reason a gate misbehaves.
    """
    global _hub
    if _hub is False:
        try:
            if str(UTILS) not in sys.path:
                sys.path.insert(0, str(UTILS))
            import baxter_hub_edit as _m
            for name in ("wait_clear", "mutations_live", "mutation_epoch"):
                getattr(_m, name)
            _hub = _m
        except Exception as e:
            _log(f"the mutation fence is unavailable ({e})- gates grade as they did before")
            _hub = None
    return _hub


def _blank_info():
    return {"waited_s": 0.0, "epoch_moved": False, "active_before": [],
            "active_after": [], "rerun": False, "still_mutating": []}


def run_command_ex(cmd, timeout=VERIFY_TIMEOUT, env=None):
    """(rc, output, info). `run_command` is the two-tuple wrapper; no caller changed shape.

    `info` carries {waited_s, epoch_moved, active_before, active_after, rerun, still_mutating}.
    `still_mutating` is [{path, pid, why}] and is populated ONLY when a red survived the re-run
    with a window still open- that is the sole evidence on which `run_verify` may say
    `unverified` rather than `failed`.

    The re-run inherits the REMAINING timeout budget, never a fresh one: a genuinely failing
    exam under VERIFY_TIMEOUT=900 must not be able to hold a lane for half an hour twice over.
    """
    info = _blank_info()
    h = _hub_edit()
    if h is None:
        rc, out = _run_once(cmd, timeout=timeout, env=env)
        return rc, out, info

    t0 = time.time()
    _, still = h.wait_clear(min(float(MUTATION_WAIT_S), float(timeout)))
    info["waited_s"] = round(time.time() - t0, 3)
    info["active_before"] = list(still)
    epoch0 = h.mutation_epoch()

    rc, out = _run_once(cmd, timeout=max(1.0, timeout - info["waited_s"]), env=env)
    if rc == 0:
        # A passing exam has nothing to forgive. Never wait on it, never re-run it- that is the
        # one guarantee that keeps the fence from ever manufacturing a green.
        return rc, out, info

    info["epoch_moved"] = h.mutation_epoch() != epoch0
    info["active_after"] = sorted({m["path"] for m in h.mutations_live()})
    if not (info["epoch_moved"] or info["active_before"] or info["active_after"]):
        return rc, out, info          # a red with no mutation anywhere IS the build's defect

    _log(f"the exam exited {rc} while a hub mutation was in flight "
         f"(epoch_moved={info['epoch_moved']}, before={info['active_before']}, "
         f"after={info['active_after']})- waiting, then re-running once: {str(cmd)[:80]}")

    remaining = timeout - (time.time() - t0)
    if remaining <= 1.0:              # the budget is spent; do not start a run we cannot finish
        info["still_mutating"] = [dict(m) for m in h.mutations_live()]
        return rc, out, info
    h.wait_clear(min(float(MUTATION_WAIT_S), remaining))
    info["rerun"] = True              # exactly once, ever
    rc, out = _run_once(cmd, timeout=max(1.0, timeout - (time.time() - t0)), env=env)
    if rc != 0:
        info["still_mutating"] = [dict(m) for m in h.mutations_live()]
    return rc, out, info


def run_command(cmd, timeout=VERIFY_TIMEOUT, env=None):
    """Run one verify/check command so that ALL of it runs. Returns (rc, output)."""
    rc, out, _info = run_command_ex(cmd, timeout=timeout, env=env)
    return rc, out


def _record_hold(entry, info, why):
    """The guard keeps its own log. A hold that only ever reached `.baxter_verify.log` would be
    invisible to `--rejects`, which is the one place the owner reads the guard's proof of life."""
    try:
        _usage().record_reject(entry or {"task": "?"}, why,
                               lane=(entry or {}).get("lane"), kind="hub-mutation")
    except Exception as e:
        _log(f"could not record the mutation hold in the rejects log: {e}")


def _holders(rows):
    """`baxter_slash.py (pid 1234)`, for the humans reading an `unverified`."""
    return ", ".join(f"{os.path.basename(r.get('path', '?'))} (pid {r.get('pid', '?')})"
                     for r in rows) or "a hub"


def run_verify_ex(entry, timeout=VERIFY_TIMEOUT):
    """THE GATE. Returns (verdict, detail, out_tail); verdict is passed | failed | unverified.

    `out_tail` is the RAW PROGRAM OUTPUT ONLY- never the command, never the exit code. It is
    the last lines of stdout+stderr with their newlines intact (see `failure_tail`), and it is
    what `_verify_gate` persists to the journal so a repair worker inherits the exception TYPE
    and LINE rather than an echo of the command it can already read in `entry["verify"]`.

    `run_verify` is the two-tuple wrapper. Callers that only grade keep their shape.

    A shell `verify` command is preferred (cheap, deterministic, no LLM in the loop);
    `verify_assert` falls back to a checker spawn. Neither = unverified, which is a
    truthful state and NOT a pass- the owner's own carve-out: say so plainly rather than
    implying it is proven.

    The command is vetted before it is run and carried WHOLE when it runs: see
    `vet_verify_cmd` and `run_command` for the two silent failures that cost this gate its
    meaning on 9th July.

    Bytecode is hardened FIRST, so the gate can never grade a build against code it has not
    just hash-validated. A failure there is `unverified`, never `failed`: the build is not
    what broke.
    """
    try:
        n = harden_bytecode()
        if n:
            _log(f"harden: rewrote {n} stale-mode pyc(s) before the gate ran")
    except Exception as e:
        return "unverified", f"bytecode could not be hardened ({e})- the gate would be blind", ""

    raw = str(entry.get("verify") or "").strip()
    claim = str(entry.get("verify_assert") or "").strip()
    if raw:
        cmd, why = vet_verify_cmd(raw)
        if cmd is None:
            return "failed", f"the sealed verify command cannot run: {why}", ""
        if why:
            _log(f"verify {why}: {raw[:80]}")
        try:
            rc, raw_out, info = run_command_ex(cmd, timeout=timeout)
        except Exception as e:
            return "failed", f"verify command could not run ({e})", ""
        out_tail = failure_tail(raw_out)
        if rc == 0:
            if info.get("rerun"):
                # The first red coincided with a sibling lane holding a hub deliberately
                # broken. The re-run, against the restored file, passed. Say so out loud- a
                # silent re-run is how a genuinely flaky exam would hide.
                _log(f"the first red was a mutation artefact- the re-run of `{cmd[:80]}` "
                     f"passed once the window closed")
                _record_hold(entry, info, "an exam went red inside a hub mutation window; "
                                          "the re-run passed and the build stands")
                return ("passed", f"`{cmd}` exited 0 on a re-run- its first red landed while a "
                                  f"sibling lane held a hub broken on disk", out_tail)
            return "passed", f"`{cmd}` exited 0", out_tail
        # A BLIND exam's non-zero exit means nothing. It asked the queue file whether an
        # entry survived; the pump takes a running entry out of that file, so the exam
        # condemns the very build that placed the work. Both seals now refuse this shape,
        # but one sealed before they did must not send a correct build into the repair
        # loop. `unverified` is the truthful state: a gate that cannot see has not looked.
        blind, why = queue_blind_exam(cmd)
        if blind:
            _log(f"the sealed exam is BLIND to in-flight lanes- {why}: {cmd[:100]}")
            return ("unverified", f"the sealed exam exited {rc}, but it cannot see the lanes: "
                                  f"{why} Nothing is proven either way.", out_tail)
        # A red that SURVIVED the re-run, with a window STILL open on a hub. The exam very
        # probably died importing a file another lane is holding deliberately broken- it never
        # reached an assertion, so it condemns nothing. `unverified` is the truthful state.
        held = info.get("still_mutating") or []
        if held:
            _log(f"the sealed exam exited {rc} with {_holders(held)} still held broken- "
                 f"grading unverified, not failed: {cmd[:80]}")
            _record_hold(entry, info, f"exam still red with {_holders(held)} held broken")
            return ("unverified", f"the sealed exam exited {rc} twice, but {_holders(held)} is "
                                  f"being held deliberately broken on disk by another lane. "
                                  f"Nothing is proven either way.", out_tail)
        # OUTPUT FIRST, ECHO LAST. `detail` is sliced to 300 chars by `record()` and to 90 by
        # the log line under it; the command is already on disk in `entry["verify"]`, so leading
        # with it spent both budgets saying nothing. The exception line leads instead.
        head = failure_head(out_tail)
        return ("failed", (f"{head}. `{cmd}` exited {rc}" if head
                           else f"`{cmd}` exited {rc} and said nothing"), out_tail)
    if claim:
        v, d = _run_checker(claim, entry)
        return v, d, ""
    return "unverified", "no verify declared- the build is unproven, not proven", ""


def run_verify(entry, timeout=VERIFY_TIMEOUT):
    """The two-tuple wrapper on `run_verify_ex`. Returns (verdict, detail).

    Kept because `baxter_orch.run_extras`' selftest and `baxter_triage`'s own selftest consume
    this shape. `_verify_gate` calls `run_verify_ex` directly- it is the one caller that needs
    the tail.
    """
    v, d, _out_tail = run_verify_ex(entry, timeout=timeout)
    return v, d


# ---- THE OUTCOME LEDGER -------------------------------------------------------------
def _read_ledger():
    try:
        d = json.loads(LEDGER.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def record(entry, outcome, detail="", lane=None):
    """Append one build outcome. This is where a success is PASSED ON and where a parked
    task leaves its diagnosis- the thing a lane never did before."""
    row = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "task": str(entry.get("task", "?"))[:160],
        "lane": lane if lane is not None else entry.get("lane"),
        "outcome": outcome,
        "detail": " ".join(str(detail).split())[:300],
        "repair_attempts": int(entry.get("repair_attempts", 0) or 0),
    }
    rows = _read_ledger()
    rows.append(row)
    rows = rows[-LEDGER_CAP:]
    tmp = LEDGER.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(LEDGER))
    except Exception as e:
        _log(f"ledger write failed: {e}")
    _log(f"{outcome.upper()}: {row['task'][:70]}- {row['detail'][:90]}")
    return row


def recent(n=5):
    return _read_ledger()[-n:]


def recent_summary(n=4):
    """What the last few lanes actually produced, for the next builder's prompt- so a
    success is passed forward instead of evaporating when the journal is unlinked."""
    rows = recent(n)
    if not rows:
        return ""
    out = []
    for r in rows:
        when = str(r.get("at", ""))[11:16]
        out.append(f"- [{when}] {r.get('outcome', '?')}: {r.get('task', '?')[:90]}"
                   + (f" ({r.get('detail')[:70]})" if r.get("detail") else ""))
    return "RECENT BUILD OUTCOMES (what the lanes before you did):\n" + "\n".join(out) + "\n"


# ---- SELFTEST -----------------------------------------------------------------------
def selftest():
    """Prove the classifier BITES on the exact failure that prompted this build."""
    # 1. THE TRAP. The benign sandbox warning at the top of the log, exit 4294967295.
    #    Reading the top of that log diagnoses a sandbox fault; the truth is "it just died".
    trap = ("Sandbox disabled: sandbox is enabled but windows is not supported\n"
            "\n")
    kind, why = classify(4294967295, trap, {})
    assert kind == "transient", f"the benign sandbox line must not be a cause, got {kind}: {why}"
    assert "bare exit" in why, why
    assert strip_benign(trap) == "", "the sandbox warning must be stripped entirely"

    # 2. A real traceback is deterministic even with the benign line sitting above it.
    det = (trap + "Traceback (most recent call last):\n"
                  '  File "x.py", line 1\nModuleNotFoundError: no module named foo\n')
    kind, why = classify(1, det, {})
    assert kind == "deterministic", f"a traceback is deterministic, got {kind}"

    # 3. A rate limit wrapped in a traceback is TRANSIENT, not a repair job.
    kind, _ = classify(1, det + "\nAPI error 429 rate limit exceeded\n", {})
    assert kind == "transient", "a 429 inside a traceback must still read as transient"

    # 4. A clean exit that the verify gate refused is deterministic (exit code FIRST).
    kind, why = classify(0, "everything looks great to me", {})
    assert kind == "deterministic" and "verify gate" in why, why

    # 5. A ghost lane- dead, no exit code at all.
    kind, why = classify(None, "", {})
    assert kind == "transient" and "ghost" in why, why

    # 6. A human-gated task is NEVER auto-repaired, whatever the log says.
    kind, _ = classify(1, det, {"gated_on": "owner"})
    assert kind == "gated", "a gated task must never be classified for auto-repair"
    kind, _ = classify(1, det, {"gated_on": ""})
    assert kind == "deterministic", "an empty gate is not a gate"

    # 7. The caps. Transient retries once, then goes looking; repairs stop at three.
    assert next_action("transient", {})[0] == "retry"
    assert next_action("transient", {"transient_retries": 1})[0] == "repair"
    assert next_action("deterministic", {})[0] == "repair"
    assert next_action("gated", {"gated_on": "owner"})[0] == "park"

    # 7b. THE CAP ITSELF, driven attempt by attempt (raised 2 -> 3 on 9th July). Every
    #     attempt below the cap self-heals; the cap exactly is where the silence ends.
    assert MAX_REPAIRS == 3, f"the cap is three attempts, got {MAX_REPAIRS}"
    for spent in range(MAX_REPAIRS):
        act, why = next_action("deterministic", {"repair_attempts": spent})
        assert act == "repair", f"{spent} repairs spent must still repair, got {act}"
        assert f"attempt {spent + 1} of {MAX_REPAIRS}" in why, why
    assert next_action("deterministic", {"repair_attempts": MAX_REPAIRS})[0] == "park", \
        "the third repair spent must PARK, not repair a fourth time"
    # A spent cap outranks the transient path: 'transient' must not buy a fourth attempt.
    assert next_action("transient", {"repair_attempts": MAX_REPAIRS})[0] == "park", \
        "a spent cap parks whatever the classifier called the failure"
    # ...and a human gate parks INSTANTLY, on attempt zero. Gated work is never self-repaired.
    assert next_action("gated", {"gated_on": "owner", "repair_attempts": 0})[0] == "park", \
        "a gated task must park before it ever spends a repair attempt"

    # 8. No verify declared is UNVERIFIED- never a pass, never a failure.
    v, why = run_verify({})
    assert v == "unverified", v
    # 9. A shell verify is obeyed on its exit code, both ways.
    v, _ = run_verify({"verify": "python -c \"import sys; sys.exit(0)\""})
    assert v == "passed", "a zero-exit verify passes"
    v, _ = run_verify({"verify": "python -c \"import sys; sys.exit(3)\""})
    assert v == "failed", "a non-zero verify fails, whatever the builder claimed"

    # 10. POSIX tools resolve. Planners write `rm -f x && ...`; cmd.exe has no `rm`, so
    # a WORKING build failed its gate on "'rm' is not recognized" (9th July, the 0xEF
    # crashdump lane). Guard both halves: the tool runs, and its failure still bites.
    if _coreutils_dir():
        v, why = run_verify({"verify": 'rm -f ".baxter_selftest_absent" && python -c "pass"'})
        assert v == "passed", f"a POSIX verify must resolve `rm`, not die in cmd.exe: {why}"
        v, _ = run_verify({"verify": 'rm -f ".baxter_selftest_absent" && python -c "import sys;sys.exit(4)"'})
        assert v == "failed", "coreutils on PATH must not soften a failing verify"

    # 11. THE HEADLINE. A MULTI-LINE exam must run every line. Under `shell=True` cmd.exe
    #     treated the newline as a command separator: line 1 ran, the rest never did, and
    #     the gate said `passed`. Sabotage each line in turn- every one must bite.
    lines = ['import sys', "assert 1 == 1, 'line 2'", "assert 2 == 2, 'line 3'",
             'sys.exit(0)']
    def _exam(src):
        return 'python -c "' + src + '"'
    v, why = run_verify({"verify": _exam("\n".join(lines))})
    assert v == "passed", f"an honest multi-line exam must pass: {why}"
    for i in range(1, len(lines) - 1):
        bad = list(lines)
        bad[i] = f'sys.exit({i + 1})'          # sabotage line i+1 only
        v, why = run_verify({"verify": _exam("\n".join(bad))})
        assert v == "failed", (f"sabotaging line {i + 1} of a {len(lines)}-line exam went "
                               f"UNCAUGHT- the gate said {v}. This is the false pass.")
    # The task's own acceptance, stated plainly: the SECOND line exits non-zero.
    v, _ = run_verify({"verify": 'python -c "pass"\npython -c "import sys; sys.exit(1)"'})
    assert v == "failed", "a multi-line command whose SECOND line exits 1 must FAIL"
    v, _ = run_verify({"verify": 'python -c "pass"\npython -c "pass"'})
    assert v == "passed", "...and one whose every line exits 0 must still pass"

    # 12. A python -c source that cannot compile is caught BEFORE it runs, and the
    #     planner's double-escaped `\n` is repaired rather than failing a correct build.
    assert python_c_source('python -c "pass"') == "pass"
    assert python_c_source('rm -f x && python -c "pass"') is None, "a compound is not rewritten"
    assert python_c_source('python "C:\\x.py" --selftest') is None
    broken = 'python -c "import sys\\nsys.exit(0)"'          # literal backslash-n, no newline
    cmd, why = vet_verify_cmd(broken)
    assert cmd and "repaired" in why and "\n" in cmd, (cmd, why)
    assert run_verify({"verify": broken})[0] == "passed", \
        "a double-escaped exam is repaired, not failed- the build under it was correct"
    cmd, why = vet_verify_cmd('python -c "def ("')
    assert cmd is None and "does not compile" in why, why
    v, why = run_verify({"verify": 'python -c "def ("'})
    assert v == "failed" and "cannot run" in why, why

    # 12b. THE UNQUOTED SPACE. PowerShell 5.1 strips the quotes off a native command's
    #      argument, so a correctly-typed script path arrives torn at its space and python
    #      exits 2 without running a line. Refused at seal time, whatever the flag says.
    _real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baxter_role_colours.py")
    assert os.path.isfile(_real), f"the fixture this case is written against is gone: {_real}"
    for _flag in (False, True):
        cmd, why = vet_verify_cmd(f"python {_real} --selftest", paths_must_exist=_flag)
        assert cmd is None, "the unquoted-space exam was SEALED, exactly as on 9th July"
        assert "space" in why.lower() and "unquot" in why.lower(), why
        assert _real in why, f"the refusal must name the file he meant: {why}"
    # ...and the same command, quoted, is untouched. Byte for byte- a rewritten exam is a
    # different exam.
    _good = f'python "{_real}" --selftest'
    assert vet_verify_cmd(_good) == (_good, ""), vet_verify_cmd(_good)
    assert vet_verify_cmd(_good, paths_must_exist=True) == (_good, "")

    # 12b-ii. THE SAME STRIP, on the `python -c` shape. The remedy this file used to
    #      recommend was itself shredded by PowerShell: `python -c "import sys; sys.exit(3)"`
    #      arrives with no quotes, python reads `import` as the whole source and dies. Refuse
    #      it- and refuse it whatever the flag says, because it can never run.
    for _flag in (False, True):
        cmd, why = vet_verify_cmd("python -c import sys; sys.exit(3)", paths_must_exist=_flag)
        assert cmd is None and "unquot" in why.lower(), why
    # A bare ONE-token source is complete and runnable- never refuse it.
    assert vet_verify_cmd("python -c pass") == ("python -c pass", "")
    # ...nor a source whose own quotes survived, whatever python_c_source makes of it.
    _nested = 'python -c "print(\\"hi\\")"'
    assert vet_verify_cmd(_nested)[0] == _nested, vet_verify_cmd(_nested)

    # 12b-iii. THE REMEDY IS THE ONE THAT WORKS. Measured 9th July from a real PowerShell
    #      5.1: plain quotes, doubled quotes and `--%` all arrive shredded; backslash-escaped
    #      quotes reach argv whole. A refusal that recommends the broken escaping is a trap-
    #      the builder re-declares, is refused again, and lands UNVERIFIED.
    _, why = vet_verify_cmd(f"python {_real} --selftest")
    assert '\\"' in why, f"the refusal must hand back the escaping that survives: {why}"
    assert '`python -c "..."`' not in why, "never recommend an escaping PowerShell eats"

    # 12c. A script that simply does not exist. The PLANNER seals before the build has
    #      written its files, so it must still pass; a BUILDER declaring at its exit is
    #      refused, its files being on disk by then.
    _ghost = 'python "' + os.path.join(os.path.dirname(_real), "no_such_file_9jul.py") + '" --selftest'
    assert vet_verify_cmd(_ghost) == (_ghost, ""), "a script the build has yet to create must seal"
    cmd, why = vet_verify_cmd(_ghost, paths_must_exist=True)
    assert cmd is None and "no such file" in why.lower(), why

    # 12d. FAIL OPEN on everything we cannot reason about. Each of these seals unchanged:
    #      refusing a good exam is the same crime as sealing a broken one. `--out <new file>`
    #      is the one that matters- only the SCRIPT slot is ever checked, never an argument
    #      naming a file the run is about to create.
    for c in (f'python "{_real}" --out logs/not_yet_written.json',
              "python -m pytest -q",
              'python -c "import sys" && python -c "pass"',
              f'python "{_real}" --selftest > out.txt',
              'node scripts/thing.js'):
        got, _ = vet_verify_cmd(c, paths_must_exist=True)
        assert got == c, f"a command we cannot reason about must seal unchanged: {c} -> {got}"

    # 12d-ii. THE SHELL-AGNOSTIC PATH FAULT. Until 10th July `_py_script_argv` was the only
    #      path check, and it declines every argv[0] that is not a python- so a
    #      `powershell -File ...` was sealed with no vetting at all. The exact value lane 9
    #      stored on 9th July: the PowerShell escaping, typed through the BASH tool, which
    #      keeps the backslash literal inside single quotes. It exited 4294770688 on `Illegal
    #      characters in path` with zero assertions run, and graded a correct build FAILED.
    _ps1 = os.path.join(os.path.dirname(_real), "baxter_watch_triagewait_e2e.ps1")
    assert os.path.isfile(_ps1), f"the fixture this case is written against is gone: {_ps1}"
    _jul9 = 'powershell -NoProfile -ExecutionPolicy Bypass -File \\"' + _ps1 + '\\"'
    for _flag in (False, True):
        cmd, why = vet_verify_cmd(_jul9, paths_must_exist=_flag)
        assert cmd is None, "the 9th-July powershell value was SEALED all over again"
        assert "backslash" in why.lower(), why
        assert "bash" in why.lower() and "powershell" in why.lower(), (
            f"the remedy must name BOTH shells- teaching one is what caused this: {why}")
    # ...and the same fault in the OTHER shape: an unquoted path torn at its space, in the
    # `-File` slot of a non-python argv[0]. Refused whatever the flag says: it can never run.
    for _flag in (False, True):
        cmd, why = vet_verify_cmd(f"powershell -File {_ps1}", paths_must_exist=_flag)
        assert cmd is None, "an unquoted powershell script path was sealed"
        assert "space" in why.lower() and "unquot" in why.lower(), why
        assert _ps1 in why, f"the refusal must name the file he meant: {why}"

    # 12d-iii. THE FALSE-POSITIVE GUARD, which is the whole cost of the check above. Each of
    #      these carries a backslash-quote, or an unresolvable script slot, and each is SOUND.
    #      A `-Command` body is PowerShell source, not a path; `node "<a file the build has yet
    #      to write>"` names a real path that simply does not exist yet; the `grep` compound is
    #      not ours to judge. Refusing a good exam is the same crime as sealing a broken one.
    _ghostjs = os.path.join(os.path.dirname(_real), "no_such_file_9jul.js")
    for c in (f'powershell -NoProfile -ExecutionPolicy Bypass -File "{_ps1}"',
              'powershell -Command "if ($x -eq \\"a\\") { exit 1 }"',
              f'node "{_ghostjs}"',
              'python -c "print(\\"hi\\")"',
              'grep -q "\\"x\\"" f.txt && echo ok'):
        for _flag in (False, True):
            got, _w = vet_verify_cmd(c, paths_must_exist=_flag)
            assert got == c, f"a sound exam was REFUSED ({_flag}): {c} -> {_w}"

    # 12e. THE EXAM THAT CANNOT FAIL. The incident: a builder declares `python -c "pass"` as
    #      its proof, the gate runs it, reads exit 0, and stamps the build `passed`. Refused
    #      at the BUILDER's flag, and only there- the planner seals before its files exist and
    #      its path must stay exactly as it was.
    _dead = 'python -c "pass"'
    cmd, why = vet_verify_cmd(_dead, paths_must_exist=True)
    assert cmd is None, "a builder can still seal an exam that cannot fail"
    assert "CANNOT FAIL" in why and "go RED" in why, f"the refusal must say what to do: {why}"
    assert vet_verify_cmd(_dead) == (_dead, ""), \
        "the PLANNER's seal path must be untouched- it vets with paths_must_exist=False"

    # Every shape of dead exam, refused. `sys.exit(0)` is the back-door the refusal message
    # would otherwise teach: 'no exit with a failing code' -> declare one that exits 0.
    for _c in ('python -c "pass"',
               'python -c "print(1)"',
               "python -c pass",                       # bare, unquoted: still a whole source
               'python -c "import sys"',               # stdlib import raises nothing
               'python -c "x = 1"',
               'python -c "import sys; sys.exit(0)"',  # `pass` in a hat
               'python -c "import sys; print(\'OK\')"'):
        assert vacuous_exam(_c)[0], f"this exam cannot fail and must be refused: {_c}"
        assert vet_verify_cmd(_c, paths_must_exist=True)[0] is None, _c
        assert vet_verify_cmd(_c)[0] == _c, f"the planner seal path must still take it: {_c}"

    # 12e-ii. A FALSE REFUSAL IS THE EXPENSIVE FAILURE. It sends a builder round the repair
    #      loop over an honest exam and burns his window doing it. So every exam that CAN go
    #      red seals unchanged under BOTH flags- including the ones with no `assert` token at
    #      all, which is the whole reason this is an AST walk and not a grep.
    for _c in ('python -c "assert 1 == 1"',
               'python -c "import sys; sys.exit(3)"',      # a failing exit code
               'python -c "raise SystemExit(2)"',
               'python -c "import baxter_rules"',          # ImportError is a real failure
               'python -c "d = {}; d[1]"',                 # KeyError: asserting a shape
               'python -c "open(\'.baxter.log\').read()"',  # any call may raise
               'python -c "import sys; sys.exit(len(sys.argv) - 1)"',   # computed code
               f'python "{_real}" --selftest'):            # a script path is never judged
        assert not vacuous_exam(_c)[0], f"an exam that CAN fail was called vacuous: {_c}"
        for _flag in (False, True):
            assert vet_verify_cmd(_c, paths_must_exist=_flag)[0] == _c, \
                f"an honest exam must seal unchanged, byte for byte: {_c}"

    # 12f. THE STALE $LASTEXITCODE. PowerShell leaves the variable UNTOUCHED when a native
    #      command fails to launch, so a probe of the ACL-denied Store python read back the
    #      exit code of the command before it: GREEN having run nothing, and a confidently
    #      wrong RED the moment that earlier command exited non-zero. Refused on BOTH branches,
    #      unlike `vacuous_exam`- the exam that carried it was sealed by the PLANNER, at
    #      paths_must_exist=False.
    _stale = ('powershell -Command "& $exe -c \'import cv2\'; '
              'if ($LASTEXITCODE -ne 0) { exit 1 }"')
    for _flag in (False, True):
        cmd, why = vet_verify_cmd(_stale, paths_must_exist=_flag)
        assert cmd is None, f"the stale-$LASTEXITCODE exam still seals at paths_must_exist={_flag}"
        assert "Start-Process" in why, f"the refusal must name the remedy: {why}"
    assert stale_exitcode_probe(_stale)[0] is True

    # It bites on every spelling of the hazard the live gates actually used, not just the one
    # from the task text: a `$var` target, a quoted literal path, a bare `python`, and a read
    # buried inside a function call rather than an `if`.
    for _c in ("& $exe -c 'import cv2'\nif ($LASTEXITCODE -ne 0) { exit 1 }",
               "& 'C:\\Program Files\\WindowsApps\\python.exe' -V\nexit $LASTEXITCODE",
               "python x.py\nif ($LASTEXITCODE -ne 0) { exit 1 }",
               "$out = & python $f 2>&1\nCheck ($LASTEXITCODE -eq 0) 'ran'",
               # a cmdlet in an ARGUMENT is evaluated before the native command it feeds, so
               # it must not clear the hazard just by sitting to the right of it in the text.
               "& powershell.exe -File (Join-Path $S 'x.ps1')\n$rc = $LASTEXITCODE",
               # a pipeline is ONE statement: Out-String does not reset $LASTEXITCODE either.
               "& python x.py | Out-String\nexit $LASTEXITCODE"):
        assert stale_exitcode_probe(_c)[0] is True, f"the detector missed the hazard: {_c!r}"

    # 12f-ii. AN OVER-BROAD DETECTOR IS WORSE THAN NONE: it would refuse every honest exam that
    #      reads $LASTEXITCODE, and nothing would ever seal again. Every doubt resolves to
    #      "not stale".
    for _c in ("$p = Start-Process -PassThru cmd.exe; $p.WaitForExit(); exit $p.ExitCode",
               "Get-Process; if ($LASTEXITCODE -ne 0) { exit 1 }",   # a cmdlet, not a launch
               "Write-Host hi\nexit $LASTEXITCODE",                  # ...at a statement start
               "exit $LASTEXITCODE",                                 # nothing before it at all
               # a .ps1 is run in-process: a missing one THROWS, it cannot fail to launch.
               "& 'C:\\u\\baxter_probe.ps1' -Exe foo\nexit $LASTEXITCODE",
               # the gates DESCRIBE the hazard in their headers. Comments are documentation.
               "# & $exe -c pass\n# if ($LASTEXITCODE -ne 0) { exit 1 }",
               'python -c "assert 1"',
               "python -c \"import sys; sys.exit(3)\""):
        assert stale_exitcode_probe(_c)[0] is False, f"false positive on an honest exam: {_c!r}"
        for _flag in (False, True):
            assert vet_verify_cmd(_c, paths_must_exist=_flag)[0] is not None, \
                f"an honest exam was refused: {_c!r}"
    # A clean exam still seals byte for byte, both branches. This is the false-refusal guard.
    assert vet_verify_cmd('python -c "assert 1"') == ('python -c "assert 1"', "")

    # 12f-ii-b. A `python -c` SOURCE IS PYTHON, not PowerShell. An exam that quotes the hazard
    #      as string DATA- in order to REPRODUCE it, which is exactly what this build's own
    #      sealed acceptance test does- must seal untouched. The first cut of this detector
    #      refused its own acceptance test, which is the over-broad failure in its purest form.
    _reproducer = ('python -c "import subprocess\n'
                   "h = open('t.ps1','w')\n"
                   "h.write('& cmd.exe /c exit 0\\n& $exe -c pass\\n"
                   "if ($LASTEXITCODE -ne 0) { exit 1 }\\n')\n"
                   'assert subprocess.run([\'powershell.exe\', \'t.ps1\']).returncode == 0"')
    assert _is_powershell_command(_reproducer) is False
    for _flag in (False, True):
        assert vet_verify_cmd(_reproducer, paths_must_exist=_flag)[0] == _reproducer, \
            "the detector refused a PYTHON exam that merely quotes the hazard as data"
    # ...while the same text handed to PowerShell to EXECUTE is refused.
    assert _is_powershell_command('powershell -Command "& $e -c x; exit $LASTEXITCODE"') is True
    assert _is_powershell_command('python -c "print(1)"') is False

    # 12f-iii. THE OFFENDER LIVED IN A FILE, not in the sealed `verify` string. An exam that
    #      merely NAMES a .ps1 carrying the shape is refused too, or the whole check is theatre.
    _fd, _bad_ps1 = tempfile.mkstemp(suffix=".ps1", prefix="baxter_selftest_stale-")
    os.close(_fd)
    try:
        with open(_bad_ps1, "w", encoding="utf-8") as _fh:
            _fh.write("& cmd.exe /c exit 0\n& $exe -c 'import cv2'\n"
                      "if ($LASTEXITCODE -ne 0) { exit 1 }\n")
        _exam = f'powershell -NoProfile -File "{_bad_ps1}"'
        for _flag in (False, True):
            cmd, why = vet_verify_cmd(_exam, paths_must_exist=_flag)
            assert cmd is None, "an exam naming an unsound .ps1 sealed"
            assert _bad_ps1 in why and "Start-Process" in why, why
        # ...and the same exam naming a SOUND script seals unchanged.
        with open(_bad_ps1, "w", encoding="utf-8") as _fh:
            _fh.write("$p = Start-Process -PassThru cmd.exe\n$p.WaitForExit()\nexit $p.ExitCode\n")
        assert vet_verify_cmd(_exam)[0] == _exam, "a sound script must not be refused"
    finally:
        os.unlink(_bad_ps1)

    # 12f-iv. The two LIVE gate scripts are clean. This is the regression that matters: both
    #      carried the shape on 9th July, and one of them described it in its own header while
    #      committing it fourteen lines later.
    for _g in (os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "baxter_verify_watchdog_revive.ps1"),
               os.path.join(str(VAULT), ".baxter_verify_boot.ps1")):
        if os.path.isfile(_g):
            with open(_g, encoding="utf-8", errors="replace") as _fh:
                _bad, _why = stale_exitcode_probe(_fh.read())
            assert _bad is False, f"a live verify gate still reads a stale exit code: {_g}: {_why}"

    # 12e-iii. Not a `python -c` at all -> never judged. Refusing what we cannot read is the
    #      same crime as sealing what we can.
    for _c in ("python -m pytest -q", "node scripts/thing.js",
               'python -c "import sys" && python -c "pass"',   # a compound: not whole-command
               'python -c "def ("'):                           # `_compiles` owns this refusal
        assert not vacuous_exam(_c)[0], f"only a whole-command `python -c` is judged: {_c}"

    # 12e-iv. THE GATE JUDGES WHAT WAS SEALED; IT DOES NOT RE-LITIGATE IT. A vacuous exam that
    #      is already in a journal (sealed by a planner, or by a builder before this build
    #      existed) still runs, and its exit code still decides. The refusal happens at seal
    #      time or not at all- a gate that second-guessed its own sealed exam could fail a
    #      build no one could ever make pass.
    assert run_verify({"verify": _dead})[0] == "passed", \
        "run_verify must obey an already-sealed exam's exit code, not re-judge it"
    assert run_verify({"verify": 'python -c "import sys; sys.exit(5)"'})[0] == "failed"

    # 12e-v. And the repair path is judged on the REPAIRED command. A double-escaped `\n`
    #      source is fixed first, then read for vacuity- so the repair cannot smuggle a dead
    #      exam past the vet, nor can it get an honest one refused.
    cmd, why = vet_verify_cmd('python -c "import sys\\nsys.exit(0)"', paths_must_exist=True)
    assert cmd is None and "CANNOT FAIL" in why, (cmd, why)
    cmd, why = vet_verify_cmd('python -c "import sys\\nsys.exit(3)"', paths_must_exist=True)
    assert cmd and "repaired" in why and "\n" in cmd, (cmd, why)

    # A POSIX exam reaches a POSIX shell, and nothing else is rerouted. cmd.exe rejects a
    # `for ... do ... done` line at PARSE time and runs no part of it, so before this the
    # planner's own sealed acceptance exams exited 1 without executing a single assertion.
    for c in (r'python "C:\x\selftest.py" --selftest',
              r'python "C:\x\a.py" --check && python "C:\x\b.py" --check',
              'python -c "import json; print(1)"',
              # A PYTHON LIST COMPREHENSION IS NOT A POSIX `for` LOOP (11th July). The keyword
              # sits inside a double-quoted span, where both shells read it as an argument. Read
              # raw, this routed a `cd /d` command- which ONLY cmd.exe can run- to bash, and
              # bash's `cd` took `/d` as a second argument: "cd: too many arguments", exit 1,
              # nothing after the first leg run, a correct build graded FAILED.
              r'cd /d "C:\x\utils" && python e.py && python -c "import sys;'
              r'sys.path[:]=[p for p in sys.path if p];print(1)"',
              # ...and the same keyword in a bare cmd.exe argument, with no shell meaning.
              'findstr /c:"do not care" file.txt'):
        assert not needs_posix_shell(c), f"must keep the cmd.exe path: {c}"
    for c in ('a && for t in X Y; do echo $t; done',
              # An EXPANSION inside double quotes is not the same case: bash substitutes it
              # there and cmd.exe does not, so the command means what only bash can give it.
              'python x.py --at "$(hostname)"',
              'python x.py; rc=$?; [ "$rc" = 3 ] && echo ok',
              'schtasks /query /tn A /xml 2>/dev/null | grep -q z',
              'if [ -e f ]; then echo y; fi',
              # A single-quoted PLAIN leg, not just `python -c`: cmd.exe cannot strip `'`, so
              # `cd 'C:/...'` dies "The filename, directory name... is incorrect." having run
              # nothing (10th July, a build's own sealed `cd '...' && python '...'` graded FAILED).
              "cd 'C:/Users/you/Documents/Baxter' && python '.baxter_exams/x.py'",
              "python 'C:/x/a.py' --swap-codex --dry-run"):
        assert needs_posix_shell(c), f"cmd.exe cannot parse this; it must go to bash: {c}"
    # ...but a `'` that is a cmd.exe LITERAL- inside a double-quoted span, or a lone apostrophe-
    # is not POSIX quoting and must NOT reroute a working cmd.exe command.
    for c in ('findstr "can\'t" file.txt', 'echo it is fine'):
        assert not needs_posix_shell(c), f"a cmd.exe literal apostrophe must not reroute: {c}"

    if _bash():
        rc, out = run_command('for i in 1 2; do echo $i; done')
        assert rc == 0 and out.split()[:2] == ["1", "2"], (rc, out)
        # `set -e` on a multi-line POSIX exam: the same "all lines must pass" contract the
        # cmd path has. Without it bash reports only the last line, a fresh false pass.
        assert run_command('false\nfor i in 1 2; do echo $i; done')[0] != 0, \
            "a failing first line must fail a multi-line POSIX exam"
        # ...but an exam that reads $? is catching an exit code deliberately; -e would abort it.
        rc, out = run_command('python -c "import sys; sys.exit(3)"\nrc=$?\n[ "$rc" = "3" ] && echo CAUGHT')
        assert rc == 0 and "CAUGHT" in out, (rc, out)
    assert run_command('python -c "import sys; sys.exit(1)"\necho second')[0] != 0, \
        "a failing first line must still fail a multi-line cmd exam"

    # A single-quoted `python -c` must bypass cmd.exe, which does not treat `'` as a quote.
    # It must still be able to FAIL: bypassing a shell is not the same as softening a verify.
    assert run_command("python -c 'import sys; sys.exit(0)'")[0] == 0, \
        "a single-quoted python -c exam must run, not die on cmd.exe quoting"
    assert run_command("python -c 'import sys; sys.exit(4)'")[0] == 4, \
        "...and must still report a real failure"

    # ---- STALE BYTECODE. The gate must never grade a hub file's OLD code.
    # Behaviour, not source text: write a module, let a python cache it in timestamp mode,
    # then edit it inside the same second to the same byte-size- the exact shape that served
    # `MAX_REPAIRS = 2` off a disk reading 3 on 9th July. Unhardened, a fresh python reads the
    # stale copy. That is asserted first, so this leg can never pass by the bug being absent.
    _d = Path(tempfile.mkdtemp(prefix="baxter_pyc-"))
    try:
        _m = _d / "stalemod.py"
        _m.write_bytes(b'K = "AAA"\n')
        _imp = f'import sys;sys.path.insert(0,r"{_d}");import stalemod;print(stalemod.K)'

        # Every probe below must run as an UNPROTECTED python. This selftest is itself run
        # from inside a lane, whose env carries both hardening vars- inherit them and the
        # child writes no beside-source pyc and reads no beside-source pyc, so the trap is
        # never laid and the legs prove nothing (they raised StopIteration on the glob).
        def _serves():
            r = subprocess.run([sys.executable, "-c", _imp], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=_clean_compile_env())
            return (r.stdout or "").strip()

        assert _serves() == "AAA", "the probe module did not import"
        _st = _m.stat()
        _m.write_bytes(b'K = "BBB"\n')
        os.utime(_m, (_st.st_atime, _st.st_mtime))     # same mtime SECOND, same size
        assert _m.stat().st_size == _st.st_size
        _pyc = next((_d / "__pycache__").glob("*.pyc"))
        assert _pyc_flags(_pyc) == 0, "the probe pyc must start in timestamp mode"
        assert not _is_hardened(_pyc_flags(_pyc))
        assert _serves() == "AAA", \
            "THE BUG IS GONE FROM CPYTHON: a timestamp pyc no longer serves stale code, so " \
            "everything below proves nothing. Re-derive this leg before trusting it."

        assert harden_bytecode(_d) == 1, "harden must repair exactly the one stale pyc"
        assert _is_hardened(_pyc_flags(_pyc)), "the pyc must come back checked-hash (flags 0b11)"
        assert _serves() == "BBB", "a hardened pyc must be revalidated by hash, not by mtime"
        assert harden_bytecode(_d) == 0, "harden must be idempotent on a clean tree"

        # Hash-based mode SURVIVES the recompile it triggers: CPython rewrites the pyc in the
        # mode it found. So a module hardened once stays hardened however often it is edited,
        # and only a module that has never been compiled can lay down a fresh timestamp pyc.
        # That is what makes the per-pass harden cheap rather than a treadmill.
        _m.write_bytes(b'K = "CCC"\n')
        assert _serves() == "CCC" and _is_hardened(_pyc_flags(_pyc)), \
            "a recompile must not silently drop the pyc back to timestamp mode"

        # EVERY caller of harden_bytecode already has PYTHONPYCACHEPREFIX set- a lane inherits
        # it from _spawn_resume, and run_verify runs inside one. If compileall inherits it too,
        # it writes the repair INTO the prefix and leaves the stale pyc exactly where the next
        # unprefixed python will read it: harden silently becomes a no-op on the only case that
        # matters. Measured. So drive it the way a lane drives it, not the way a shell does.
        _pm = _d / "prefixmod.py"
        _pm.write_bytes(b"P = 1\n")
        subprocess.run([sys.executable, "-c",
                        f'import sys;sys.path.insert(0,r"{_d}");import prefixmod'],
                       capture_output=True, env=_clean_compile_env())
        _ppyc = next((_d / "__pycache__").glob("prefixmod.*.pyc"))
        assert _pyc_flags(_ppyc) == 0
        _was = os.environ.get("PYTHONPYCACHEPREFIX")
        os.environ["PYTHONPYCACHEPREFIX"] = str(_d / "prefix")
        try:
            assert "PYTHONPYCACHEPREFIX" not in _clean_compile_env(), \
                "compileall must not inherit the caller's pycache prefix"
            harden_bytecode(_d)
        finally:
            if _was is None:
                os.environ.pop("PYTHONPYCACHEPREFIX", None)
            else:
                os.environ["PYTHONPYCACHEPREFIX"] = _was
        assert _is_hardened(_pyc_flags(_ppyc)), \
            "harden must repair the pyc BESIDE THE SOURCE even when its caller has a prefix set"

        # A STALE pyc that cannot be repaired is BINNED, never left to be read: its source is
        # gone, or its source no longer compiles. Deleting bytecode is always safe; serving the
        # wrong bytecode never is. (A HARDENED orphan is left alone on purpose- it is inert:
        # CPython will not import a __pycache__ pyc whose source has vanished.)
        _o = _d / "orphanmod.py"
        _o.write_bytes(b"Z = 1\n")
        subprocess.run([sys.executable, "-c",
                        f'import sys;sys.path.insert(0,r"{_d}");import orphanmod'],
                       capture_output=True, env=_clean_compile_env())
        _opyc = next((_d / "__pycache__").glob("orphanmod.*.pyc"))
        assert _pyc_flags(_opyc) == 0, "a first import must lay the pyc down in timestamp mode"
        _o.unlink()
        assert harden_bytecode(_d) == 1 and not _opyc.exists(), \
            "a stale orphan pyc must be deleted, not left behind for the next import to read"
    finally:
        shutil.rmtree(_d, ignore_errors=True)

    # The live tree carries no timestamp-mode pyc, under ANY interpreter tag- 3.11 and the
    # slash bot's 3.12 both write here, and compileall only ever rewrites its own tag. Other
    # lanes import between the harden and the read, so give the self-heal room to win.
    for _ in range(3):
        harden_bytecode()
        _stale = [p.name for p in (UTILS / "__pycache__").glob("*.pyc")
                  if not _is_hardened(_pyc_flags(p))]
        if not _stale:
            break
    assert not _stale, f"timestamp-mode pyc(s) survive in utils/__pycache__: {_stale[:6]}"

    # Both vars, on every gate child. One stops the write, the other stops the read; neither
    # is sufficient alone, and PYTHONDONTWRITEBYTECODE alone was measured NOT to fix the read.
    _env = _verify_env()
    assert _env.get("PYTHONDONTWRITEBYTECODE") == "1", "the gate's children may not write bytecode"
    assert _env.get("PYTHONPYCACHEPREFIX"), "the gate's children must read from a prefix cache"
    assert Path(_env["PYTHONPYCACHEPREFIX"]).is_dir(), "the pycache prefix must exist"

    # ---- THE MUTATION FENCE. The in-process half; `baxter_hubmut_exam.py` drives the real
    # multi-process one. Everything here is about what the fence must NOT do: it must not wait
    # on a clean fleet, it must not re-run a passing exam, it must not soften an honest red,
    # and it must not hang a gate on a marker that never clears.
    _h = _hub_edit()
    assert _h is not None, "baxter_hub_edit will not import- the fence is dead on arrival"

    # A sibling lane may be red-proofing RIGHT NOW, and the two legs below assert on what a
    # CLEAN fleet does. Give its window a generous chance to close before claiming anything.
    # This is the sibling case in miniature: an assertion made mid-window proves nothing.
    _clean, _held = _h.wait_clear(20)
    if _clean:
        # 13a. A PASSING exam: no wait worth measuring, no re-run, nothing held.
        _t0 = time.time()
        _rc, _out, _i = run_command_ex('python -c "assert 1 == 1"')
        assert _rc == 0 and _i["rerun"] is False, (_rc, _i)
        assert _i["waited_s"] < 1.0, f"a clean fleet must not make a passing exam wait: {_i}"
        assert _i["still_mutating"] == [] and _i["epoch_moved"] is False, _i
        assert time.time() - _t0 < 30, "run_command_ex took absurdly long on a trivial exam"

        # 13b. AN HONEST RED, with no mutation anywhere, is returned UNTOUCHED. This is the
        #      anti-laundry assertion: the fence may never soften an ordinary failing build.
        _rc, _out, _i = run_command_ex('python -c "import sys; sys.exit(3)"')
        assert _rc == 3, f"the fence changed a real exit code: {_rc}"
        assert _i["rerun"] is False, "an honest red was re-run- the fence is laundering defects"
        assert _i["still_mutating"] == [], _i
        assert run_verify({"verify": 'python -c "import sys; sys.exit(3)"'})[0] == "failed", \
            "a red with no window anywhere must still be FAILED, not unverified"
    else:
        print(f"baxter_verify selftest: clean-fleet legs skipped, {_held} is held broken")

    # 13c. THE HARD CAP. A marker that never clears must not hang the gate: the wait returns
    #      after MUTATION_WAIT_S and the exam runs anyway. Written by hand with a LIVE pid (our
    #      own) and a long ttl, so neither fail-safe can sweep it- only the cap can end the wait.
    global MUTATION_WAIT_S
    _fx = str((VAULT / ".baxter_verify_fence_selftest.tmp").resolve())
    _mk = _h.LOCK_DIR / (_h._slug(_fx) + "-vfyst" + _h.MUTATION_SUFFIX)
    _cap_was = MUTATION_WAIT_S
    _h.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        MUTATION_WAIT_S = 0.5
        _mk.write_bytes(json.dumps({"id": "vfy-selftest", "path": _fx, "pid": os.getpid(),
                                    "started": time.time(), "ttl": 300.0,
                                    "why": "baxter_verify selftest"}).encode("utf-8"))
        assert any(m["path"] == _fx for m in _h.mutations_live()), \
            "the fixture marker is not visible- the legs below would prove nothing"

        _t0 = time.time()
        _rc, _out, _i = run_command_ex('python -c "assert 1 == 1"')
        _spent = time.time() - _t0
        assert _rc == 0, "a passing exam must still pass while a window is open"
        assert _i["waited_s"] >= 0.4, f"the gate started mid-window without waiting: {_i}"
        assert _i["rerun"] is False, "a PASSING exam was re-run- the fence must never do that"
        assert _spent < 30, f"the wait was not capped- the gate hung for {_spent:.1f}s"
        assert _fx in _i["active_before"], _i

        # ...and a red under a window that never closes is UNVERIFIED, never FAILED- after
        # exactly one re-run, both waits capped.
        _t0 = time.time()
        _rc, _out, _i = run_command_ex('python -c "import sys; sys.exit(4)"')
        _spent = time.time() - _t0
        assert _rc == 4 and _i["rerun"] is True, (_rc, _i)
        assert _fx in [m["path"] for m in _i["still_mutating"]], _i
        assert _spent < 30, f"the two capped waits took {_spent:.1f}s"
        _v, _why = run_verify({"verify": 'python -c "import sys; sys.exit(4)"'})
        assert _v == "unverified", f"a red under a held window was graded {_v}: {_why}"
        assert _fx.split(os.sep)[-1] in _why and "pid" in _why, \
            f"the unverified must name the holder and its pid: {_why}"

        # 13d. THE FENCE IS NEVER THE REASON A GATE MISBEHAVES. With baxter_hub_edit absent,
        #      behaviour is EXACTLY today's: no wait, no re-run, blank info- even with that
        #      same marker sitting on disk.
        global _hub
        _hub_was = _hub
        try:
            _hub = None
            _rc, _out, _i = run_command_ex('python -c "import sys; sys.exit(4)"')
            assert _rc == 4 and _i == _blank_info(), \
                f"an unimportable fence changed the gate's behaviour: {_i}"
            assert run_verify({"verify": 'python -c "import sys; sys.exit(4)"'})[0] == "failed", \
                "with no fence, a red is FAILED- exactly as it was before this build"
        finally:
            _hub = _hub_was
    finally:
        MUTATION_WAIT_S = _cap_was
        try:
            _mk.unlink()
        except OSError:
            pass
    assert not _mk.exists(), "the fixture marker leaked- every later gate would wait on it"

    print("baxter_verify selftest OK: the benign sandbox line is never a cause, "
          "a 429 in a traceback is transient, a clean exit with a failed check is "
          "deterministic, a ghost is transient once, gated never repairs, an "
          "undeclared verify reads as unverified, a POSIX verify finds its tools AND "
          "a POSIX shell to run them in, EVERY line of a multi-line exam runs and can "
          "be caught, an exam that cannot compile is repaired or refused rather "
          "than silently mis-judged, a builder's exam that CANNOT FAIL is refused at "
          "seal time while every exam that can go red seals byte for byte, and an exam that "
          "reads a STALE $LASTEXITCODE after a launch that can fail is refused on BOTH seal "
          "paths- in its own text and inside any .ps1 it merely names. Bytecode cannot go "
          "stale: a timestamp pyc that serves a hub file's OLD code is rewritten to "
          "checked-hash or binned, the live tree carries none under either interpreter tag, "
          "and no child of the gate may write one. A sibling lane holding a hub deliberately "
          "broken no longer condemns this build: the gate waits for the window, re-runs ONCE, "
          "and grades a surviving red `unverified` rather than `failed`- while a passing exam "
          "never waits, an honest red is never re-run, the wait is hard-capped, and a fence "
          "that will not import leaves every verdict exactly as it was.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    if "--recent" in sys.argv:
        print(recent_summary(8) or "no build outcomes recorded yet")
        sys.exit(0)
    if "--classify" in sys.argv:
        i = sys.argv.index("--classify")
        rc = int(sys.argv[i + 1])
        k, w = classify(rc, sys.stdin.read() if not sys.stdin.isatty() else "", {})
        print(f"{k}: {w}")
        sys.exit(0)
    print(__doc__.strip().splitlines()[0])
