"""baxter_rules — the hard rules EVERY Baxter worker prompt carries. ONE copy of each.

WHY THIS MODULE EXISTS (Atul, 9th July 01:07: "this only fixes this instance. I need a
permanent fix"). The rules used to be HAND-TYPED into each prompt builder- the fast lane,
the fresh-session builder and the resumed-session builder. Three copies drift. On 9th July
the "full pass" ban was patched into two of the three; nine minutes later a RESUMED session
broke the rule live, because nobody had looked at the third. Patching the third copy would
have left the same hole for the fourth builder anyone added.

The duplication WAS the bug; the missing line was only its symptom. The precedent for the
repair already sat in these very prompts: REMINDER_RULE and WORKER_READ_RULE are imported
constants and have never drifted, because there is exactly one of each.

So: a prompt builder imports WORKER_RULES and interpolates it. It types no rule text of its
own, ever. A new builder gets the rules by construction. `check()` (run every triage pass)
renders every builder and fails loudly if one of them lost the block.

Adding a rule? Add it here, add it to WORKER_RULES, and every worker has it within a beat.
"""
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import baxter_read_channel as rc   # WORKER_READ_RULE- the anti-"can't access" rule
import baxter_reminders as rem     # REMINDER_RULE- a confirmed timer must be REGISTERED

VAULT = Path(r"C:\Users\you\Documents\Baxter")
SAY = str(Path(__file__).resolve().parent / "baxter_say.py")
ALERT = VAULT / ".baxter_rules_alert.json"
PINGED = VAULT / ".baxter_rules_pinged"

# The modules that build worker prompts. Every top-level function in them whose name ends
# in `_prompt` is treated as a builder and MUST carry the block- that suffix is the whole
# registration mechanism, so a fourth builder is covered the moment it is named.
#
# COROLLARY, and it has teeth: a HELPER in one of these modules must NOT be named `*_prompt`.
# The suffix scan cannot tell a helper from a builder, so it would demand the full REQUIRED
# rule set of it and fail the tree for a function that was never a prompt at all.
BUILDER_MODULES = ("baxter_fast", "baxter_slash")

# ---------------------------------------------------------------------------
# THE VOICE. One copy, in one file, exactly as the rules above have one copy each.
#
# Atul, 8th July: "generate and edit and develop your md files to be more like claude",
# refined at 22:58 to "research JARVIS- every AI adopts a Jarvis vocabulary and syntax
# approach (Baxter, Codex, Jem alike)". Before this, the voice was typed out in EIGHT
# places: two md files, both bot personas, three prompt builders and the resurrection
# answerer- and they had already drifted apart. That is the same bug this module was built
# to kill for the rules, so it gets the same cure rather than a ninth copy.
#
# baxter_voice.md carries the block between two literal HTML-comment markers. `voice()`
# reads it, `set_voice_md()` stamps it into the md surfaces, and `check()` goes red the
# instant any surface stops matching. The vocabulary bank and its citations live in
# 50-Research/'Jarvis voice - verified vocabulary and syntax spec.md'- research, not runtime.
# ---------------------------------------------------------------------------

VOICE_MD = VAULT / "baxter_voice.md"
VOICE_BEGIN = "<!-- baxter:voice:begin -->"
VOICE_END = "<!-- baxter:voice:end -->"

# The md files that carry a stamped copy of the block. set_voice_md() writes each one;
# check() reads each one back and demands it equal voice(), byte for byte after newline
# normalisation. AGENTS.md and GEMINI.md are Codex's and Jem's own instruction files- the
# thing Atul asked for when he said "your md files".
VOICE_MD_SURFACES = ("CLAUDE.md", "BAXTER_TRIAGE.md", "AGENTS.md", "GEMINI.md")

# The Python surfaces that must RENDER the voice. Each is (module, callable-name), and the
# callable must be renderable on dummy arguments for the same reason build_worker_prompt is:
# a prompt the guard cannot render is a prompt the guard cannot police.
#
# The fast lane's and the listener's builders are NOT listed here- they are found by the
# `*_prompt` suffix scan in _builders(), and check() demands the voice of them alongside the
# rest of REQUIRED. Listing them twice would only let the two lists disagree.
VOICE_PY_SURFACES = (
    ("baxter_codex_bot", "persona"),
    ("baxter_jemini_bot", "persona"),
    ("baxter_triage", "build_worker_prompt"),
    ("baxter_triage", "_triage_prompt"),
    ("baxter_resurrect_audit", "_answer_prompt"),
)

VOICE_NAME = "VOICE_BLOCK"     # what a failure calls the block, the way it names a rule


class VoiceError(RuntimeError):
    """The single voice source is missing, unreadable, or has lost its markers.

    A named exception, not a bare RuntimeError, so check() can catch exactly this and
    report it as a FAILURE- 'the single voice source is unusable'- instead of letting it
    escape and kill the triage pass that called the guard.
    """


_voice_cache = {}


def _read_md(path):
    """Read an md surface as text, with line endings normalised to LF.

    Reads BYTES. `Path.write_text` on Windows silently rewrites every LF as CRLF, so a
    read_text/write_text round trip 'restores' a file it has in fact rewritten end to end
    ([[write-text-flips-lf-to-crlf]]). Everything here reads and writes bytes; normalising
    on the way in is what lets a CRLF surface still compare equal to an LF source.
    """
    return Path(path).read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


def _fenced(text, label, begin=VOICE_BEGIN, end=VOICE_END):
    """The text between the two markers, stripped. Raises VoiceError if either is gone."""
    i = text.find(begin)
    j = text.find(end, i + 1) if i >= 0 else -1
    if i < 0 or j < 0:
        raise VoiceError(f"{label} has lost its voice markers ({begin} ... {end})")
    return text[i + len(begin):j].strip()


def voice():
    """The shared voice block, read from baxter_voice.md. THE one copy.

    Cached on the source's (mtime_ns, size), not on mtime alone: a same-second rewrite of
    the same length is exactly the shape of an edit that a coarse cache would serve stale
    ([[stale-pyc-same-second-same-size]]), and the acceptance exam mutates this file and
    re-reads it within a few milliseconds.
    """
    try:
        st = VOICE_MD.stat()
    except OSError as e:
        raise VoiceError(f"the single voice source {VOICE_MD} is gone: {e}") from e
    key = (st.st_mtime_ns, st.st_size)
    if _voice_cache.get("key") != key:
        # Populate the text FIRST. If _fenced raises, the key must not be left claiming
        # that a cached (and now absent) text belongs to this version of the file.
        _voice_cache["text"] = _fenced(_read_md(VOICE_MD), VOICE_MD.name)
        _voice_cache["key"] = key
    return _voice_cache["text"]


def _voice_list(head):
    """The `- key: value` lines under a `## <head>` section of baxter_voice.md."""
    text = _read_md(VOICE_MD)
    m = re.search(r"(?ims)^##\s+" + re.escape(head) + r"\s*$(.*?)(?=^##\s|\Z)", text)
    if not m:
        raise VoiceError(f"baxter_voice.md has no '## {head}' section")
    out = {}
    for line in m.group(1).splitlines():
        hit = re.match(r"\s*-\s+([a-z0-9_]+)\s*:\s*(.+?)\s*$", line)
        if hit:
            out[hit.group(1)] = hit.group(2)
    return out


def identity(agent):
    """The one-line identity of ONE agent- what it is, not how it speaks."""
    d = _voice_list("Identity")
    if agent not in d:
        raise VoiceError(f"baxter_voice.md carries no identity line for '{agent}'")
    return d[agent]


def blurb(agent):
    """The Discord profile 'about me' for ONE bot. Discord caps these near 190 characters."""
    d = _voice_list("Blurbs")
    if agent not in d:
        raise VoiceError(f"baxter_voice.md carries no blurb for '{agent}'")
    return d[agent]


def voice_surfaces():
    """Every place the voice is allowed to appear: {'md': [Path...], 'py': [dotted names]}.

    The registry IS the guard's worklist. A surface added to the estate and not added here
    is a surface nothing watches, which is how eight copies drifted in the first place.
    """
    py = [f"{m}.{n}" for m, n in VOICE_PY_SURFACES]
    try:
        py += [f"{m}.{n}" for m, n, _ in _builders()]
    except Exception:
        pass   # check() reports an unimportable builder module on its own, and by name
    return {"md": [VAULT / n for n in VOICE_MD_SURFACES], "py": sorted(py)}


def _eol(raw):
    """The dominant line ending of some bytes- b'\\r\\n', b'\\n', or None if it has neither."""
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if not crlf and not lf:
        return None
    return b"\r\n" if crlf > lf else b"\n"


def set_voice_md(paths=None):
    """Stamp voice() into the fenced region of every md surface. Returns the names changed.

    Writes BYTES, and only when the bytes actually change: a no-op stamp must not touch the
    mtime of CLAUDE.md on every triage pass. Everything outside the two markers survives
    untouched- CLAUDE.md's reaction lifecycle and governor bands, BAXTER_TRIAGE.md's 31 steps
    and its hard lines- because only the slice between the markers is ever replaced.

    EACH SURFACE IS STAMPED IN ITS OWN LINE ENDINGS. voice() is LF-normalised, so a block
    built with bare LFs never equals the fenced region of a CRLF file: CLAUDE.md is CRLF on
    Windows, so every triage pass rewrote it, flipped its voice block to LF alone, and left
    the file mixed- and a `check()` that compares after normalising called it clean, so the
    churn was silent.

    The ending is read from the surface OUTSIDE the fenced region, never from the region
    itself. That region is the one part of the file the old stamp had already corrupted, so
    keying on it would hold every mixed surface mixed forever- idempotently, and invisibly.
    The body around it is the surface's own untouched style, and stamping in that heals the
    region back to it. It falls back to the fenced slice (for a file that is nothing but the
    block), then to LF. The write is atomic: a surface half-stamped by a crash is a surface
    with no markers, and every reader of it then raises.
    """
    b_begin, b_end = VOICE_BEGIN.encode("utf-8"), VOICE_END.encode("utf-8")
    body = (VOICE_BEGIN + "\n" + voice() + "\n" + VOICE_END).encode("utf-8")
    changed = []
    for p in (paths if paths is not None else voice_surfaces()["md"]):
        p = Path(p)
        raw = p.read_bytes()
        i = raw.find(b_begin)
        j = raw.find(b_end, i + 1) if i >= 0 else -1
        if i < 0 or j < 0:
            raise VoiceError(f"{p.name} has no voice markers to stamp into")
        k = j + len(b_end)
        eol = _eol(raw[:i] + raw[k:]) or _eol(raw[i:k]) or b"\n"
        block = body.replace(b"\n", eol)
        new = raw[:i] + block + raw[k:]
        if new != raw:
            tmp = p.with_name(p.name + ".voice.tmp")
            tmp.write_bytes(new)
            os.replace(tmp, p)
            changed.append(p.name)
    return changed

# ---------------------------------------------------------------------------
# The rules. Each is ONE string, defined ONCE, in this file and nowhere else.
# ---------------------------------------------------------------------------

# The literal appears exactly once in the codebase- here. NAMING_RULE interpolates it and
# the source sweep in check() greps for it, so neither retypes it.
BANNED_PHRASE = "full pass"

NAMING_RULE = (
    f"- NEVER write the words '{BANNED_PHRASE}' (or any synonym for the queue). The ONLY "
    f"name for queued/deferred work is 'the build queue'. Scan your reply before sending.\n")

NO_DIG_RULE = (
    "- Answer from the CONVERSATION ABOVE. Do NOT go digging. NO grep. Read AT MOST ONE "
    "vault file, and only if you genuinely cannot answer without it- otherwise touch "
    "nothing.\n")

# THE READING OVERRIDE (Atul, 10th July: "it should be allowed to READ all the docs and do
# READING based things automatically. It shouldn't need to do info fetches later"). NO_DIG_RULE
# and DEFLECT_RULE above were written to stop a QUICK reply ballooning into a 20-tool-call BUILD
# investigation- a real bug, when a yes/no answer triggered a vault expedition. But they overshot:
# they also stopped Baxter READING to answer a genuine question, and told it to defer the lookup
# to the queue ("later"), which is the exact thing Atul hates. This clause draws the line those
# rules missed: READING to answer him is never "later" work- it is the answer. Only work that
# WRITES (a build, a new file, a code change, a draft) is deferred. It rides ONLY on the reply
# prompt (baxter_slash._reply_prompt), never on a build worker, and sits LAST so recency makes it
# win the contradiction with NO_DIG_RULE/DEFLECT_RULE for a reply, while leaving both verbatim for
# the build workers and the exams that seal them.
READING_OVERRIDE = (
    "- READING IS ANSWERING, NOT 'LATER'. The 'no digging / read one file / deflect lookups' "
    "lines above exist to stop a quick reply becoming a 20-call BUILD investigation- they do "
    "NOT gate reading to answer a question. If answering Atul needs you to read vault files, "
    "docs, code or state, DO IT NOW and read as much as the question honestly needs. Never tell "
    "him you'll 'look into it' or 'fetch that later'- that is the one thing he does not want. "
    "ONLY work that WRITES- a build, a new file, a code change, a draft- is queued instead of "
    "done here; a question you can answer by reading is answered by reading, this turn.\n")

ONE_EDIT_RULE = (
    "- A quick command (done/snooze/move/cut/rename) -> apply it to the ONE matching task "
    "line, confirm in a few words. One edit, no hunting.\n")

QUEUE_CMD = str(Path(__file__).resolve().parent / "baxter_usage.py")

# The rule this module exists to make unbreakable (Atul, 9th July- three strikes in eight
# minutes). It is enforced in CODE by baxter_preannounce_guard, a PreToolUse hook that
# DENIES a reply making either claim without the matching tool-call already in the turn.
# The rule text and the guard must keep saying the same thing- if you soften one, the other
# starts blocking replies the prompt has just ordered.
PREANNOUNCE_RULE = (
    "- ACT FIRST, THEN CONFIRM. NEVER write 'going in now', 'starting now', 'on it' or any "
    "other present-tense promise before the work has actually happened- a reply is a "
    "statement of fact, and a fact stated early is a lie. Do the thing, then say what you "
    "did. A guard blocks these replies outright, so a pre-announce does not reach him.\n"
    "- A queue claim is only true AFTER a real queue call. Never say 'queued' until you "
    f"have run baxter_usage.py --queue; then confirm with its numeric position.\n")

# REWRITTEN 9th July. This rule used to order the exact reply the guard now blocks-
# "On it, sir- queued to the build queue" with no queue call behind it. It was the single
# biggest source of the lie: the fast lane was being INSTRUCTED to pre-announce. Deflecting
# is still right; deflecting without queueing was never right.
#
# REWRITTEN AGAIN, same day (his 09:38 order). Telling a language model to queue the work
# itself still rests the truth of the confirmation on the model choosing to make a tool
# call. baxter_fast now writes the placeholder in CODE before it wakes any worker, and
# hands the slot down in the prompt- so the ordinary path is to CONFIRM an entry that
# already exists, not to create one. The `--queue` branch survives for the builders that
# have no placeholder written for them.
DEFLECT_RULE = (
    "- ANYTHING that needs real lookup, research, drafting, or new work -> do NOT do it "
    "here. If this prompt carries an ALREADY QUEUED line, the entry exists and is yours to "
    "confirm- do not queue it again, that forks a duplicate. Otherwise queue it for real: "
    f"run `python \"{QUEUE_CMD}\" --queue \"<task>\" \"<first step>\" --touch \"<files it "
    "will edit>\"` and confirm with the position it printed. The heavy triage lane does "
    "deep work; you must not. Never claim a queueing you did not make.\n")

# The rule Atul dictated at 09:38 on 9th July, in the words he used: every queued
# confirmation states the exact position. It is not a formatting preference. A position can
# only be printed by a process that has written the entry and read the order back, so
# demanding the number is what makes the confirmation impossible to fake. That is its force.
POSITION_RULE = (
    "- NEVER write 'queued' without the exact slot beside it- 'position N of M, pP'. Take "
    "N, M and P from the queue itself: the ALREADY QUEUED line in this prompt, or the "
    "'queued at position ...' line your own --queue call printed. NEVER estimate any of "
    "them. No entry means no position, and no position means no claim: say 'Noted, sir' "
    "and describe what you will do instead. A state report ('it is queued at ...') is no "
    "exemption: baxter_preannounce_guard reads the live queue before every send and BLOCKS "
    "a slot the queue does not hold, so a guessed number never reaches him- it reaches the "
    "rejects log with your reply still unsent.\n")

# The standing order Atul gave on 9th July, in prompt form. baxter_autobuild is the same
# policy in code- the two must keep saying the same thing, and baxter_autobuild.should_ping()
# is the single place the severe/meager line is drawn.
AUTOBUILD_RULE = (
    "- THE MOMENT YOU NOTICE A PROBLEM, QUEUE A BUILD FOR IT YOURSELF, with a priority you "
    "assign. Do NOT flag a meager issue to Atul first, and do NOT ask permission- a fault he "
    "has to notice on your behalf is a fault you have not handled. Only a SEVERE fault- a "
    "vital down, something outward-facing, data lost- is worth his attention, and that one "
    "gets a build queued as well as a word to him. Every such build carries a verify command "
    "that proves the repair: a repair that cannot prove itself is not queued at all.\n")

# THE PRD IS THE SPECIFICATION (Atul, 9th July 09:47: a PM Claude fills a rigorous PRD, and a
# PM-manager greenlights it, before a build is filed). Two Opus instances argued the shape of
# the work out before this worker was woken; a worker that then builds something adjacent has
# thrown both away. The touch-set in the PRD's section 7 is the same declaration the clash
# delegator scheduled the lane on, so widening it silently is not scope creep- it is a
# concurrent edit to a file another lane believes it owns.
#
# It rides in WORKER_RULES rather than BUILD_REQUIRED because the build lane's prompt
# (baxter_triage.build_worker_prompt) interpolates VERIFY_STEP alone and is a hub region this
# build does not own. The clause is written to be a no-op for a worker whose task carries no
# PRD, which is every reply-lane worker today.
PRD_RULE = (
    "- IF YOUR TASK CARRIES A PRD (note_path points at a `60-PRDs/*.md`), that document IS "
    "the specification: a PM instance wrote it and a PM-manager greenlit it before you were "
    "woken. Build what it says, and nothing beside it. Its touch-set is the declaration your "
    "lane was scheduled on- you may not widen scope past it, and a file it never names is a "
    "file another lane may believe it owns. If the PRD is wrong, say so and halt; do not "
    "quietly build the better idea.\n")

# TRUST BUT VERIFY (Atul, 9th July 00:17, HIGH PRIO), as a rule a worker CARRIES rather than
# a line in a contract it may never open. The contract's hard-rules section has said this
# from the start; the workers that BUILD never saw it, because the build lane's prompt was
# hand-typed and imported nothing. Now it reaches every worker from here.
#
# The rule alone would still rest on a model choosing to obey it- the same weakness that put
# the queue placeholder into code. So baxter_verify.vacuous_exam is its other half: a builder
# declaring an exam that CANNOT FAIL is refused at seal time. Rule and machinery say the same
# thing, and neither is worth much on its own.
VERIFY_STEP = (
    "- TRUST BUT VERIFY. Before you write 'done', 'fixed', 'working' or 'shipped', RUN the "
    "code you changed, end to end, and read its REAL exit code and REAL output. Exercise the "
    "edge cases the task names- empty input, malformed input, an absent file, and above all "
    "the failure that prompted the work. Test the thing that actually serves him: where a "
    "long-lived process serves the changed code, drive that process, not the file. A 'done' "
    "with no run behind it is a lie, however certain you feel. If it genuinely cannot be run "
    "yet- gated on Atul, a timer that fires later- say THAT plainly, rather than wording it "
    "so it reads as proven.\n")


# ---------------------------------------------------------------------------
# WHO OWNS THE FILE YOU ARE ABOUT TO EDIT (10th July).
#
# The delegator only ever compared lane against lane. A triage worker, the fast lane and a
# live channel session met no check at all- so on 9th July 10:13 a triage worker edited
# utils/baxter_usage.py and utils/baxter_triage.py while lane 1 held BOTH in its declared
# touch_set. Neither write was lost, because they happened to land in different regions of
# the files. That is luck. `baxter_usage --writer-touch` is the check, and the PreToolUse
# hook registered on Edit|Write|MultiEdit|NotebookEdit is what makes it binding.
#
# TWO RULES, NOT ONE, and the split is deliberate. A LANE is exempt for its own declared
# touch-set: the delegator proved it clash-free before the lane ever started, and denying a
# lane its own files would stop all ten at once. Everyone else is refused outright. Handing
# a builder the non-lane text would tell it to check paths it already owns; handing a reply
# worker the lane text would offer it an exemption it does not have.
# ---------------------------------------------------------------------------
WRITER_TOUCH_RULE = (
    "- BEFORE ANY CODE EDIT, ASK WHO OWNS THE FILE. Up to ten build lanes edit this estate at "
    "once, each holding a declared touch-set, and you hold none. Run\n"
    f"      python \"{QUEUE_CMD}\" --writer-touch <path> [<path>...]\n"
    "  Exit 0 = nobody owns it, edit freely. Exit 3 = A LIVE LANE OWNS IT: do NOT edit that "
    "file, tell Atul the lane owns it, and queue the work instead. A PreToolUse hook denies "
    "the Edit outright either way, so this is not advice you may decline- it only tells you "
    "before the tool call is refused.\n")

WRITER_TOUCH_LANE_RULE = (
    "- THE WRITER GUARD, AND WHY IT DOES NOT BITE YOU. A PreToolUse hook DENIES any Edit or "
    "Write to a file another live lane declared. You are exempt inside your OWN touch-set: the "
    "hook recognises your lane by process ancestry and stands aside, because the delegator "
    "proved your declaration clash-free before your lane started. Stray outside it and you are "
    "merely another writer- declare the path first with `--lane-touch <your journal> <path>`, "
    "or ask\n"
    f"      python \"{QUEUE_CMD}\" --writer-touch <path>\n"
    "  Exit 3 means a sibling lane owns it: halt yourself, do not collide.\n")


def _hub_file_names():
    """The hub files by basename, read from `baxter_usage.HUB_FILES`- the one list.

    Imported inside the function, never at module scope, for the same reason `_build_prompts`
    is: this module is imported BY the hubs, and a module-scope import back into one of them
    would be a hard cycle at interpreter start. baxter_usage imports nothing of ours, so the
    call at import time below is safe today; keeping it in a function means a future cycle
    fails here, loudly and in one place, rather than everywhere at once.

    Rendered rather than retyped. A hand-typed list of five filenames in a rule string is a
    copy of HUB_FILES that nothing keeps honest, and the rule would go on naming four files
    the day a fifth is added- which is the exact hour the rule matters most.
    """
    import baxter_usage as _gov
    return sorted(h.rsplit("/", 1)[-1] for h in _gov.HUB_FILES)


# HUB FILES ARE FENCED (9th July 13:31, lane 4). A build wrote the `_reload_module` /
# `_reload_orch` header block into baxter_triage.py; a sibling lane rewrote the whole file from
# a buffer it had read before that, and the block vanished. The call site survived, so the file
# compiled, and only an AttributeError at runtime exposed it hours later.
#
# Region touch-sets promise two lanes may edit one hub file at once. They do not enforce it:
# nothing made a lane re-read before it wrote. baxter_hub_edit is the enforcement, and this is
# the rule that sends a builder to it. Note what it is NOT: a lock. Nothing mechanically stops a
# worker calling Write on a hub file, so the rule and the fence only protect a lane that uses
# them. It rides in BUILD_REQUIRED alone- a reply-lane worker edits task lines, never a hub.
HUB_EDIT_RULE = (
    "- HUB FILES ARE FENCED- NEVER WRITE ONE WHOLE. " + ", ".join(_hub_file_names()) + " are "
    "edited by several build lanes at once, each in a different function. Every edit you make "
    "to one of them MUST go through baxter_hub_edit: `baxter_hub_edit.edit(path, fn)`, or "
    "`read(path)` for a (text, token) pair and then `commit(token, new_text)`. Never write a "
    "hub file from a buffer you read earlier, and never rewrite one end to end to change one "
    "function- on 9th July that silently deleted another lane's header block, the file still "
    "compiled, and only a runtime AttributeError found it hours later. A HubConflict means the "
    "file MOVED under you and somebody else's work is now in it: re-read the file and re-apply "
    "your change to the new text. It never means write back what you already had.\n")


# A RED-PROOF MUST ANNOUNCE ITSELF (10th July). baxter_hub_edit.mutating() was built that
# morning and baxter_verify already obeyed it- a gate waits for an open window, and a red that
# overlapped one grades `unverified` rather than `failed`. It had ZERO callers. The fence was
# consulted by every reader and opened by nobody, because nothing told a builder to open it.
#
# Meanwhile every build lane is ordered, four lines above this rule in its own prompt, to
# "sabotage your own code and watch it go red". Doing that to a file a sibling's verify gate
# imports kills that gate in its module body, before one assertion runs, and the sibling is
# graded FAILED for a fault it never caused. Measured the same day: baxter_slash.py was wrong
# on disk for ~8s and the lane-ui build was condemned twice.
#
# The fence is ADVISORY and there is no way to enforce it from baxter_hub_edit's side- an
# unmarked mutation is invisible to every reader there. The prompt is the only enforcement
# available, which is exactly why it rides in BUILD_REQUIRED: a reply-lane worker never
# red-proofs anything, and check() now fails BY NAME the moment this falls out of the prompt.
#
# It says "any file another lane imports", not "any hub file", on purpose. baxter_rules.py is
# not a hub file and is imported by baxter_triage.py, which is one.
REDPROOF_FENCE_RULE = (
    "- A RED-PROOF MUST ANNOUNCE ITSELF. Proving your exam can go red means holding a file "
    "BROKEN on disk for a moment. Every other lane's verify gate runs in that moment: if it "
    "imports what you broke, it dies in the module body before one assertion runs and is "
    "graded FAILED for your mutation, not its own fault (10th July- baxter_slash.py was wrong "
    "for ~8s and condemned a sibling build twice). So declare the window:\n"
    "      import baxter_hub_edit\n"
    "      with baxter_hub_edit.mutating(path, why=\"red-proof of <what>\"):\n"
    "          <write the broken bytes, run the exam, restore them in a finally>\n"
    "  baxter_verify waits for an open window before it starts a gate, and re-runs once a red "
    "that overlapped one- so a DECLARED mutation costs a sibling a few seconds, and an "
    "undeclared one costs it a FAILED build. This binds any file another lane IMPORTS, not "
    "just the five hub files: baxter_rules.py is imported by baxter_triage.py. Mutate in "
    "BYTES and restore in BYTES, hashing before and asserting after- write_text turns every "
    "LF into CRLF and 'restores' a file it has rewritten end to end.\n")


# ---------------------------------------------------------------------------
# Handing a worker its placeholder. baxter_fast has already written the entry; all the
# worker must do is state the slot truthfully.
#
# The exact clause matters, and is not decoration. baxter_preannounce_guard DENIES a reply
# containing a bare 'queued' unless a --queue call landed in the same turn- but it spares a
# STATE report ('is queued', 'already queued', "it's queued") via fixed-width lookbehinds,
# precisely because such a sentence describes an entry that already exists. The worker made
# no --queue call, so it must speak in that voice or the guard will (correctly) gag it.
# CONFIRM_CLAUSE therefore opens with 'is queued'. Change this and the guard's lookbehind
# list must change with it- baxter_queue_ack_selftest asserts the pair still agree.
# ---------------------------------------------------------------------------

def confirm_clause(position):
    """The literal clause a worker must reproduce, e.g. 'is queued at position 3 of 27, p5'."""
    return f"is queued at {position}"


def source_mid_rule(mid):
    """The clause that stops a worker forking a twin beside its own placeholder.

    enqueue() keys on the Discord message id BEFORE the task text, so the entry written for
    this message is found again only by a caller that names the id. A worker that queues
    with bare `--queue` and better prose looks, to the queue, like a brand new ask- and Atul
    gets two rows and a position describing the wrong one. `--source-mid` upgrades the row
    it already has. It is only reachable from the CLI since 9th July; before that, the dedup
    existed but nothing outside the fast lane could reach it.
    """
    return (f"- If you queue ANYTHING for this message, pass `--source-mid {mid}` to "
            f"baxter_usage.py --queue. An entry keyed on that id may already exist, and a "
            f"bare --queue forks a duplicate beside it instead of refining it.\n")


def queued_block(position="", task=""):
    """The ALREADY QUEUED block, or the flat refusal when nothing was written.

    Both halves of Atul's order live here: an ack may never precede the act, and a bare
    'queued' may never reach him. With no entry there is nothing to confirm, so the worker
    is told in as many words that the word is forbidden to it.
    """
    if not position:
        return ("NOTHING IS QUEUED for this message. You may not write 'queued' in your "
                "reply- there would be no entry behind it, and a guard will block the "
                "send. Say 'Noted, sir' and describe what you will do.\n\n")
    # "ALREADY QUEUED- " is a MARKER, not prose. DEFLECT_RULE and POSITION_RULE both refer to
    # "the ALREADY QUEUED line", so the bare phrase appears in every prompt whether or not an
    # entry exists; the trailing dash is what distinguishes this block from a mention of it.
    # Two assertions in baxter_livequeue_exam were unfalsifiable until they keyed on it.
    #
    # It no longer says WHICH lane wrote the entry. The fast lane writes it for a plain dump,
    # the listener for an @mention (baxter_slash._live_big_ask), and the worker cannot tell
    # them apart nor act differently if it could.
    return ("ALREADY QUEUED- this entry was written in code before you were woken, so "
            "it exists right now; you did not create it and you must not create it again:\n"
            f"  \"{str(task)[:120]}\"  ->  {position}\n"
            f"Confirm it using this clause VERBATIM, unchanged: \"{confirm_clause(position)}\". "
            "Reword it and you either state a position you invented or drop it altogether- "
            "both are the exact failure this rule exists to stop.\n\n")

BUDGET_RULE = (
    "- Total budget: a handful of actions, then reply. If you're on your 4th tool call, "
    "you've already overdone it- reply now.\n")

# WHAT THE OLD `VOICE_RULE` ALSO CARRIED, and must go on carrying. Its one string mixed
# register ("butler, tight, UK English") with SCOPE ("do NOT create notes/files; never
# message anyone but Atul"). Only the register half belongs in baxter_voice.md, which is an
# md file a model is invited to edit. Folding the scope half in with it would have put a
# hard line- never act outward- one careless prose edit away from disappearing. So the
# register moved to voice(), the scope stayed here, and REQUIRED still demands both.
SCOPE_RULE = (
    "- Do NOT create notes or files. Never message anyone but Atul, and never act outside "
    "his own server.\n")

# The block every builder interpolates, verbatim, in this order. REMINDER_RULE and
# WORKER_READ_RULE ride along so a builder cannot import the rules and still miss them.
WORKER_RULES = (
    "HARD RULES (this is why replies were timing out- a quick reply was turning into a "
    "20-tool-call investigation):\n"
    + NO_DIG_RULE
    + ONE_EDIT_RULE
    + WRITER_TOUCH_RULE
    + PREANNOUNCE_RULE
    + DEFLECT_RULE
    + POSITION_RULE
    + AUTOBUILD_RULE
    + PRD_RULE
    + VERIFY_STEP
    + NAMING_RULE
    + BUDGET_RULE
    + rem.REMINDER_RULE
    + rc.WORKER_READ_RULE
    + "\n")

# Every rule that must be present in a rendered prompt, named so a failure says WHICH one
# went missing rather than just "the block".
REQUIRED = (
    ("NO_DIG_RULE", NO_DIG_RULE),
    ("ONE_EDIT_RULE", ONE_EDIT_RULE),
    ("WRITER_TOUCH_RULE", WRITER_TOUCH_RULE),
    ("PREANNOUNCE_RULE", PREANNOUNCE_RULE),
    ("DEFLECT_RULE", DEFLECT_RULE),
    ("POSITION_RULE", POSITION_RULE),
    ("AUTOBUILD_RULE", AUTOBUILD_RULE),
    ("PRD_RULE", PRD_RULE),
    ("VERIFY_STEP", VERIFY_STEP),
    ("NAMING_RULE", NAMING_RULE),
    ("BUDGET_RULE", BUDGET_RULE),
    ("REMINDER_RULE", rem.REMINDER_RULE),
    ("WORKER_READ_RULE", rc.WORKER_READ_RULE),
    ("SCOPE_RULE", SCOPE_RULE),
)

# ---------------------------------------------------------------------------
# The BUILD lane's prompt is guarded too- but by NAME, and against a SMALLER rule set.
#
# Why baxter_triage is not simply appended to BUILDER_MODULES, which was the obvious move:
#
#   1. The suffix IS the registration there, and baxter_triage is a 4000-line hub holding
#      prompts for two unrelated workers. `_triage_prompt` files inbox notes; only
#      `build_worker_prompt` builds. A suffix-scan sweeps up both.
#   2. REQUIRED is the REPLY lane's rule set, and half of it is actively wrong for a builder.
#      NO_DIG_RULE forbids grep and permits one vault file; BUDGET_RULE calls a fourth tool
#      call excessive. Both are right for a worker answering a Discord message in seconds,
#      and both would cripple one editing three modules over an hour. Forcing them onto the
#      build prompt to satisfy a guard would be the guard corrupting the thing it guards.
#
# So the build prompt carries the rules that APPLY to it, BUILD_REQUIRED names them, and
# check() fails just as loudly when one goes missing. Adding a rule to both lanes means
# adding it to REQUIRED and to BUILD_REQUIRED- deliberately two decisions, not one.
BUILD_PROMPTS = (("baxter_triage", "build_worker_prompt"),)

BUILD_REQUIRED = (
    ("VERIFY_STEP", VERIFY_STEP),
    ("HUB_EDIT_RULE", HUB_EDIT_RULE),
    ("WRITER_TOUCH_LANE_RULE", WRITER_TOUCH_LANE_RULE),
    ("REDPROOF_FENCE_RULE", REDPROOF_FENCE_RULE),
)


def reply_via(mid, cid=None):
    """The mandatory reply command. Also duplicated three ways before this module."""
    ch = f"--channel {cid} " if cid else ""
    return (f"REPLY VIA: python \"{SAY}\" {ch}--reply-to {mid} \"<your reply>\"  (mandatory- "
            f"it is how Atul hears you, and it de-dups so you can never double-send). ")


# How a builder frames the conversation it hands the worker. Duplicated three ways too, and
# already divergent by the time this module was written- the fast lane's copy had drifted to
# a spaced hyphen and an extra example. One copy each.
REPLY_TARGET_BIND = (
    "His message is a REPLY to the REPLY TARGET above- read it as responding to THAT "
    "message. Fragments and pronouns ('that one', 'do it', 'this') bind to the reply "
    "target FIRST; the recent chatter is only secondary background.\n\n")

CONVO_BIND = (
    "Read it as a turn in that conversation- fragments and pronouns ('that one', 'do it', "
    "'the second option') bind to the messages just before it, usually your own last "
    "reply.\n\n")


def bind_rule(pinned):
    """Pin the message he actually replied to, else bind to the recent turns."""
    return REPLY_TARGET_BIND if pinned else CONVO_BIND


def convo_block(convo):
    """The channel's recent messages, oldest-first, or nothing at all."""
    if not convo:
        return ""
    return (f"CONVERSATION SO FAR (last messages in this channel, oldest-first, 'Baxter' = "
            f"you):\n{convo}\n\n")


# ---------------------------------------------------------------------------
# The guard. A builder that loses the block fails HERE, on the next triage pass,
# rather than in front of Atul on the fifth strike.
# ---------------------------------------------------------------------------

@contextmanager
def _discord_stubbed():
    """baxter_slash imports discord at module scope, but only its prompt BUILDERS matter
    here and they touch none of it. Stub it when the running interpreter lacks it (triage
    is py311, the listener is py312) so the guard checks rules, not the environment.

    The stub is REMOVED on the way out. triage is a long-lived process: leaving a MagicMock
    named `discord` in its sys.modules would silently poison any later real import."""
    try:
        import discord  # noqa: F401
        yield
        return
    except ImportError:
        pass
    import importlib.machinery
    import importlib.util
    from unittest.mock import MagicMock
    stub = MagicMock()
    # A MagicMock does not conjure DUNDERS, so the stub arrives with no __spec__ and
    # `importlib.util.find_spec("discord")` raises ValueError rather than returning one.
    #
    # WHY THIS LINE STAYS (assessed 9th July). It was written for ONE caller: baxter_slash
    # asked find_spec("discord") at module scope whenever '--selftest' was in argv, so
    # `baxter_rules.py --selftest` blew up inside its own first assertion and reported the
    # live tree as drifted. That caller is GONE- the bootstrap is now gated on
    # `__name__ == "__main__"`, so an importer never reaches it, and nothing under check()
    # queries the stub's spec any more (verified by instrumenting find_spec across a real
    # check(): zero calls). The original justification is dead. The line is not.
    #
    # Putting a name in sys.modules CLAIMS a module lives there, and a module without
    # __spec__ is half a module: find_spec, importlib.reload, pkgutil and the
    # `import discord.x` submodule machinery all read __spec__ and raise ValueError
    # without it. Every builder module imported inside this block- and every one added
    # later- inherits that trap. Deleting the line trades one line for a failure that
    # surfaces in some future importer, far from here, exactly as it did in baxter_slash.
    #
    # So it stays, and the guard below makes it load-bearing rather than merely intended:
    # the guard is ITSELF the caller that queries the stub's spec, so deleting the line
    # raises here instead of passing silently. Held by baxter_stubspec_exam.py.
    # `raise`, never `assert`- `python -O` strips asserts, and a pin that vanishes under a
    # flag pins nothing.
    stub.__spec__ = importlib.machinery.ModuleSpec("discord", None)
    added = {"discord": stub, "discord.app_commands": stub.app_commands}
    for k, v in added.items():
        sys.modules.setdefault(k, v)

    def _unstub():
        for k, v in added.items():
            if sys.modules.get(k) is v:
                del sys.modules[k]

    # find_spec by ATTRIBUTE at call time, never `from importlib.util import find_spec`, so
    # the exam can substitute it. It consults sys.modules first- hence after the insert
    # above, never before it.
    try:
        spec = importlib.util.find_spec("discord")
    except BaseException as e:
        _unstub()
        raise RuntimeError(
            "the discord stub reached sys.modules without a usable __spec__ "
            f"({type(e).__name__}: {e}). Restore `stub.__spec__ = "
            "importlib.machinery.ModuleSpec('discord', None)` above- a spec-less module "
            "poisons find_spec for every later importer.") from e
    if spec is None:
        _unstub()
        raise RuntimeError(
            "find_spec('discord') returned None while the stub was installed- the stub is "
            "not reachable as a module. Restore `stub.__spec__ = "
            "importlib.machinery.ModuleSpec('discord', None)` above.")
    try:
        yield
    finally:
        _unstub()


def _builders():
    """Every prompt builder in every builder module: (module, name, function).

    The `*_prompt` suffix IS the registration- no list to keep in step, so a fourth builder
    is covered the moment somebody names it. A helper that is not a builder must not use it.
    """
    found = []
    with _discord_stubbed():
        for modname in BUILDER_MODULES:
            mod = __import__(modname)
            for name, fn in vars(mod).items():
                if (name.endswith("_prompt") and inspect.isfunction(fn)
                        and getattr(fn, "__module__", "") == modname):
                    found.append((modname, name, fn))
    return found


def _build_prompts():
    """The build lane's prompt builders: (module, name, function-or-None).

    Imported LAZILY and never at module scope. baxter_triage imports this module to get
    VERIFY_STEP, so a module-scope import back into it would be a hard circular import at
    interpreter start. Inside a function the cycle is harmless: by the time check() runs,
    baxter_triage is either already in sys.modules (the triage pass, which is the caller)
    or imports cleanly on its own (a hand-run). Its module scope is constants and guarded
    imports- no spawn, no write, no governor read- so importing it to render a string is
    an observation, not an action.

    A missing function is reported as a failure, not raised: the guard's job is to say WHICH
    prompt lost its rules, and a renamed builder is exactly that failure wearing a disguise.
    """
    found = []
    for modname, fname in BUILD_PROMPTS:
        mod = __import__(modname)
        fn = getattr(mod, fname, None)
        found.append((modname, fname, fn if inspect.isfunction(fn) else None))
    return found


def _render(fn):
    """Render with dummy args- rules must be unconditional, so any args will do."""
    sig = inspect.signature(fn)
    n = sum(1 for p in sig.parameters.values()
            if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
    return fn(*(["x"] * n))


def _sweep():
    """No .py under utils/ may contain the banned phrase except this file."""
    here = Path(__file__).resolve()
    hits = []
    for p in sorted(here.parent.glob("*.py")):
        if p.resolve() == here:
            continue
        try:
            if BANNED_PHRASE in p.read_text(encoding="utf-8", errors="ignore").lower():
                hits.append(p.name)
        except OSError:
            pass
    return hits


def _check_prompt(modname, name, fn, required, failures):
    """Render one prompt builder and append a named failure for each rule it dropped."""
    try:
        prompt = _render(fn)
    except Exception as e:
        failures.append(f"{modname}.{name} would not render: {e!r}")
        return
    if not isinstance(prompt, str):
        failures.append(f"{modname}.{name} returned {type(prompt).__name__}, not a prompt")
        return
    for rulename, text in required:
        if text not in prompt:
            failures.append(f"{modname}.{name} is missing {rulename}")


def _voice_failures():
    """Every way the single voice source can have stopped being single. Returns (failures, v).

    `v` is None when the source itself is unusable, and the caller then stops demanding the
    voice of anything: a missing baxter_voice.md must produce ONE clear failure naming the
    file, not eleven failures naming every surface that (truthfully) no longer matches a
    block that no longer exists.

    Nothing here raises. A guard that dies on the fault it exists to detect takes the whole
    triage pass down with it- the check() caller gets a named failure, always.
    """
    failures = []
    try:
        v = voice()
    except VoiceError as e:
        failures.append(f"the single voice source is unusable: {e}")
        return failures, None
    except Exception as e:                          # unreadable bytes, a permissions fault
        failures.append(f"the single voice source could not be read: {e!r}")
        return failures, None

    for p in voice_surfaces()["md"]:
        p = Path(p)
        if not p.is_file():
            failures.append(f"the voice surface {p.name} is gone- it carries no voice at all")
            continue
        try:
            region = _fenced(_read_md(p), p.name)
        except VoiceError as e:
            failures.append(str(e))
            continue
        except Exception as e:
            failures.append(f"{p.name} could not be read: {e!r}")
            continue
        if region != v:
            failures.append(f"{p.name}'s voice block has drifted from baxter_voice.md- "
                            f"run baxter_rules.set_voice_md()")

    # The Python surfaces. Rendered, never grepped: on 9th July a file-level check passed
    # green while the running listener still served the old code. What a worker is HANDED is
    # the only thing that steers it.
    with _discord_stubbed():
        for modname, fname in VOICE_PY_SURFACES:
            try:
                mod = __import__(modname)
            except Exception as e:
                failures.append(f"the voice surface {modname} would not import: {e!r}")
                continue
            fn = getattr(mod, fname, None)
            if not inspect.isfunction(fn):
                failures.append(f"{modname}.{fname} is gone- that voice surface is unguarded")
                continue
            try:
                text = _render(fn)
            except Exception as e:
                failures.append(f"{modname}.{fname} would not render: {e!r}")
                continue
            if not isinstance(text, str):
                failures.append(f"{modname}.{fname} returned {type(text).__name__}, not text")
            elif v not in text:
                failures.append(f"{modname}.{fname} is missing {VOICE_NAME}")

    return failures, v


def _eol_failures():
    """Name every tracked .py in EITHER repo that has silently gone CRLF.

    THE HALF GIT CANNOT SEE. Both repos run core.autocrlf=true and, since 11th July, carry a
    checked-in `.gitattributes` pinning `*.py text eol=lf`. Both normalise CRLF away BEFORE
    git diffs, so a text-mode write that flips a source file to CRLF on disk leaves `git
    status` and `git diff` calling the tree clean. Fourteen exams key on the BYTES of a
    source line and go red for it- baxter_stubspec_exam did, on 11th July, hunting a line
    that was plainly there. Only something that reads the bytes can see this, so the guard
    rides here, on the pass it happens, rather than surfacing as a mystery red three exams
    later.

    BOTH REPOS, ONE LINE PER REPO. It swept only `Python Scripts/utils` until 11th July, so a
    vault that had gone entirely CRLF came back as `[]` and this leg read that empty list as an
    all-clear- a guard reporting clean for a tree it never opened. `all_offenders()` carries
    the root with each path because both trees hold a `baxter_*_exam.py`, and a bare filename
    could not say which one had rotted.

    It REPORTS and never repairs. `baxter_eol.normalise()` is deliberately not called: a
    background byte-rewrite of a hub file a lane may be mid-edit on is a lost edit, and a
    lost edit is worse than the CRLF it cured. The named fix is `baxter_eol.py --fix`, run
    by the build the alert queues, or by hand.

    A guard that cannot enumerate a tree says so. It never returns an empty list on a broken
    `git`, and it never lets ONE readable root stand in for both- a false all-clear is the
    exact failure this leg exists to end.
    """
    try:
        import baxter_eol
        bad = baxter_eol.all_offenders()
    except Exception as e:
        return [f"the CRLF guard could not read the tree, so nothing here is proven "
                f"LF: {e!r}. Fix baxter_eol.all_offenders(), then re-run."]
    if not bad:
        return []
    out = []
    for root in dict.fromkeys(r for r, _ in bad):
        rels = [rel for r, rel in bad if r == root]
        names = ", ".join(Path(rel).name for rel in rels)
        verb = "has" if len(rels) == 1 else "have"
        out.append(f"{len(rels)} tracked .py in {baxter_eol.root_label(root)} {verb} gone "
                   f"CRLF- {names}. Git reports the tree clean; the bytes-keyed exams will "
                   f"not. Run baxter_eol.py --fix.")
    return out


def check(ping=False, quiet=False):
    """Return 0 when every builder carries every rule, every surface carries the one voice,
    and nobody retyped the ban.

    `ping` is opt-in (triage passes it) so that running this by hand can never buzz Atul's
    phone- a guard that cries wolf while it is being edited is a guard he learns to ignore.
    """
    failures = []

    voice_bad, v = _voice_failures()
    failures.extend(voice_bad)

    # The voice rides in REQUIRED as one more named rule, so a builder that drops it fails
    # with "missing VOICE_BLOCK" rather than a mute mismatch. It is appended DYNAMICALLY
    # because REQUIRED is built at import and voice() is read from disk: binding the block
    # into a module constant would serve a stale voice for the life of the triage process,
    # and every surface would then be judged against a copy- which is the bug, not the fix.
    required = REQUIRED + ((VOICE_NAME, v),) if v is not None else REQUIRED
    build_required = BUILD_REQUIRED + ((VOICE_NAME, v),) if v is not None else BUILD_REQUIRED

    try:
        builders = _builders()
    except Exception as e:
        failures.append(f"could not import the builder modules: {e!r}")
        builders = []

    if not builders:
        failures.append(f"no *_prompt builders found in {', '.join(BUILDER_MODULES)}")

    for modname, name, fn in builders:
        _check_prompt(modname, name, fn, required, failures)

    # The BUILD lane, against its own smaller rule set. A separate try: baxter_triage failing
    # to import must not be reported as "the fast lane lost its rules".
    try:
        build_prompts = _build_prompts()
    except Exception as e:
        failures.append(f"could not import the build-lane prompt module: {e!r}")
        build_prompts = []

    for modname, name, fn in build_prompts:
        if fn is None:
            failures.append(f"{modname}.{name} is gone- the BUILD lane's prompt is unguarded")
            continue
        _check_prompt(modname, name, fn, BUILD_REQUIRED, failures)

    for name in _sweep():
        failures.append(f"{name} retypes the banned phrase- import NAMING_RULE instead")

    failures.extend(_eol_failures())

    if not failures:
        _clear()
        if not quiet:
            surf = voice_surfaces()
            print(f"rules check OK: {len(builders)} reply builder(s) carry all "
                  f"{len(required)} rules, {len(build_prompts)} build prompt(s) carry all "
                  f"{len(build_required)}; the ban is typed once; the one voice reaches "
                  f"{len(surf['md'])} md + {len(surf['py'])} python surface(s).")
        return 0

    if not quiet:
        for f in failures:
            print(f"RULES DRIFT: {f}", file=sys.stderr)
    _alarm(failures, ping=ping)
    return 1


def _clear():
    for p in (ALERT, PINGED):
        try:
            p.unlink()
        except OSError:
            pass


# Prompt-rule drift, graded once against baxter_autobuild's severity table: it steers every
# reply Atul reads, but nothing is DOWN, nothing has gone outward, and the repair is pure
# code that --selftest can prove. That is MEAGER. He never hears about it- a build does.
DRIFT_SEVERITY = "meager"

# STABLE ACROSS PASSES, deliberately. enqueue() keys on the task text, and triage runs this
# check every pass- so a task string naming the builder that drifted, or counting the missing
# rules, would fork a fresh entry every fifteen seconds and drown the queue. The failing
# builders ride along as the entry's state_summary, where they inform the repair without
# churning the key.
DRIFT_TASK = ("Repair prompt-rule drift: a worker prompt builder no longer carries the "
              "shared WORKER_RULES block")
DRIFT_NEXT = ("Read .baxter_rules_alert.json for the failing builder(s), restore the missing "
              "rule(s) by interpolating baxter_rules.WORKER_RULES (never by retyping rule "
              "text), then prove it with baxter_rules.py --selftest")
DRIFT_VERIFY = f'python "{Path(__file__).resolve()}" --selftest'


def _self_repair(failures):
    """Queue the build that fixes this drift. True when a repair now exists to fix it.

    THE STANDING ORDER (Atul, 9th July): the moment Baxter notices a problem it queues a
    build for it, with a priority it assigns itself. It does not flag a meager issue to him
    and it does not ask permission. Returning False is the ONLY thing that re-arms the ping-
    if no repair could be queued, the fault is unhandled and he must hear about it after all.
    Silence is earned by having done something, never by having decided not to speak.

    `notice()` returning None means a repair for this drift is already in flight; that is
    handled, not failed. Imported lazily: baxter_autobuild imports baxter_usage, and a
    module-scope import here would risk a cycle back through the prompt builders.
    """
    try:
        import baxter_autobuild as ab
        ab.notice(kind="rules-drift", task=DRIFT_TASK, next_step=DRIFT_NEXT,
                  severity=DRIFT_SEVERITY, signature="; ".join(failures),
                  verify=DRIFT_VERIFY)
        return True
    except Exception as e:
        try:
            print(f"autobuild could not queue the rules-drift repair: {e!r}", file=sys.stderr)
        except Exception:
            pass
        return False


def _alarm(failures, ping=False):
    """Record the drift, queue its repair, and speak to Atul only if speaking is warranted.

    `ping` means "this is the live triage audit, you may act": a hand-run neither buzzes his
    phone NOR queues a build. That second half matters more than it looks- selftest() forces
    drift twice on purpose, and this function's own repair build is verified by running that
    selftest. Ungated, every dev run of the check would queue two real repair builds, and the
    repair's verification would queue two more.

    The ping survives for the cases that still deserve it: a severity graded severe, or a
    repair that could not be queued at all.
    """
    try:
        ALERT.write_text("\n".join(failures), encoding="utf-8")
    except OSError:
        pass
    if not ping:
        return

    repaired = _self_repair(failures)
    try:
        import baxter_autobuild as ab
        loud = ab.should_ping(DRIFT_SEVERITY)
    except Exception:
        loud = True     # the policy module is gone; fall back to telling him, never to silence
    if repaired and not loud:
        return          # queued, meager, handled- he has better things to read

    try:
        if PINGED.exists() and (time.time() - PINGED.stat().st_mtime) < 3600:
            return
    except OSError:
        pass
    msg = (f"⚠️ Prompt-rule drift: {len(failures)} builder rule(s) went missing- "
           f"{failures[0]}. Replies are steering on the wrong rules, sir.")
    if not repaired:
        msg += " I could not queue a repair for it either."
    _ping(msg)


def _ping(msg):
    """The ONE outward call in this module- a prompt-rule drift alert to Atul. Returns True only
    if it actually left. Exit 3 is a DENIAL: baxter_say refused the claim, printed why, and sent
    nothing. Record the reason in the voiceless denial sink and do NOT stamp PINGED- the
    hour-cooldown must never begin on a ping that never went
    ([[outward-path-proven-from-the-send-ledger]])."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run([sys.executable or "python", SAY, msg],
                           cwd=str(VAULT), timeout=30, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True)
    except Exception:
        return False
    if getattr(p, "returncode", 0) == 3:
        try:
            import baxter_autobuild as autobuild
            autobuild.denial_alert("baxter_rules._ping", str(msg)[:80], p.stderr)
        except Exception:
            pass
        return False
    PINGED.write_text("", encoding="utf-8")
    return True


def selftest():
    """Prove the guard BITES. A check that only ever passes catches no drift at all- which
    is how three copies of one rule survived five strikes. Never pings: the one case that
    exercises the live audit path (ping=True) stubs the outward call and redirects the queue,
    so it can neither post to Atul nor write the real build queue."""
    global ALERT, PINGED, subprocess

    assert check(quiet=True) == 0, "the live tree should be clean"

    # The guard must not leave a fake `discord` behind in a long-lived process like triage.
    from unittest.mock import MagicMock
    assert not isinstance(sys.modules.get("discord"), MagicMock), "stub leaked into sys.modules"

    # 1. A fourth builder arrives carrying no rules- exactly the hole Atul asked me to close.
    with _discord_stubbed():
        import baxter_slash
    def _rogue_prompt(body, cid, mid):
        return "a new builder that forgot the rules"
    _rogue_prompt.__module__ = "baxter_slash"
    baxter_slash._rogue_prompt = _rogue_prompt
    try:
        assert check(quiet=True) == 1, "a rules-less builder must FAIL the check"
    finally:
        del baxter_slash._rogue_prompt

    # 2. Somebody retypes the ban into another module instead of importing NAMING_RULE.
    tmp = Path(__file__).resolve().parent / "_baxter_rules_selftest_tmp.py"
    tmp.write_text(f"X = '{BANNED_PHRASE}'\n", encoding="utf-8")
    try:
        assert tmp.name in _sweep(), "a retyped ban must be swept up"
        assert check(quiet=True) == 1, "a retyped ban must FAIL the check"
    finally:
        tmp.unlink(missing_ok=True)

    # 3. The source_mid clause names the id it is given, and names the flag that reaches
    #    enqueue()'s dedup. A clause that mentions neither steers nothing.
    t = source_mid_rule("4242")
    assert "--source-mid" in t and "4242" in t, t
    assert source_mid_rule("1") != source_mid_rule("2"), "the clause ignores its message id"

    # 4. The standing order reaches workers only if its ABSENCE is an error. A builder that
    #    carries every other rule and drops AUTOBUILD_RULE must fail, and be named.
    def _halfrules_prompt(body, cid, mid):
        return WORKER_RULES.replace(AUTOBUILD_RULE, "") + SCOPE_RULE + voice()
    _halfrules_prompt.__module__ = "baxter_slash"
    baxter_slash._halfrules_prompt = _halfrules_prompt
    try:
        assert check(quiet=True) == 1, "a builder missing AUTOBUILD_RULE must FAIL the check"
        assert "missing AUTOBUILD_RULE" in ALERT.read_text(encoding="utf-8"), \
            "the check failed without naming the rule that went missing"
    finally:
        del baxter_slash._halfrules_prompt

    # 4b. THE BUILD LANE. Until 9th July its prompt was a 70-line f-string inside
    #     _worker_run's sibling `_resume_worker`- unguarded, unrenderable, and carrying no
    #     rules at all. So TRUST BUT VERIFY reached every worker EXCEPT the one that builds.
    #     Prove three things: the live build prompt carries VERIFY_STEP; the guard FAILS and
    #     names it when it does not; and the prompt still renders on zero arguments, which is
    #     the only reason the guard can see it at all.
    import baxter_triage as _bt
    assert VERIFY_STEP in _bt.build_worker_prompt(), \
        "the live BUILD worker prompt does not carry VERIFY_STEP"
    assert VERIFY_STEP in _bt.build_worker_prompt(repair=True), \
        "the diagnose-and-repair worker's prompt dropped VERIFY_STEP"
    _real_bp = _bt.build_worker_prompt

    def _rulesless(*a, **k):
        return _real_bp(*a, **k).replace(VERIFY_STEP, "")
    _bt.build_worker_prompt = _rulesless
    try:
        assert check(quiet=True) == 1, "a build prompt without VERIFY_STEP must FAIL the check"
        assert "build_worker_prompt is missing VERIFY_STEP" in ALERT.read_text(encoding="utf-8"), \
            "the check failed without naming the build prompt or the rule it lost"
    finally:
        _bt.build_worker_prompt = _real_bp

    # 4c. THE FENCE RULE, on the same terms. It names the hub files from HUB_FILES rather than
    #     retyping them, so the two must still agree: a sixth hub file added to the set and not
    #     to the rule would leave a builder free to clobber it whole.
    import baxter_usage as _gov
    for _h in _gov.HUB_FILES:
        assert _h.rsplit("/", 1)[-1] in HUB_EDIT_RULE, \
            f"HUB_EDIT_RULE does not name {_h}- the rule and HUB_FILES have drifted apart"
    assert "baxter_hub_edit" in HUB_EDIT_RULE, "the fence rule does not name the fence"
    assert HUB_EDIT_RULE in _bt.build_worker_prompt(), \
        "the live BUILD worker prompt does not carry HUB_EDIT_RULE"
    assert HUB_EDIT_RULE in _bt.build_worker_prompt(repair=True), \
        "the diagnose-and-repair worker's prompt dropped HUB_EDIT_RULE"
    assert HUB_EDIT_RULE not in WORKER_RULES, \
        "HUB_EDIT_RULE leaked into the reply lanes- they never edit a hub file"

    def _fenceless(*a, **k):
        return _real_bp(*a, **k).replace(HUB_EDIT_RULE, "")
    _bt.build_worker_prompt = _fenceless
    try:
        assert check(quiet=True) == 1, "a build prompt without HUB_EDIT_RULE must FAIL the check"
        assert "build_worker_prompt is missing HUB_EDIT_RULE" in ALERT.read_text(encoding="utf-8"), \
            "the check failed without naming the build prompt or the fence rule it lost"
    finally:
        _bt.build_worker_prompt = _real_bp

    # 4d. THE RED-PROOF FENCE RULE (10th July). mutating() had zero callers: baxter_verify
    #     consulted the fence and nothing ever opened a window, so a red-proof's byte-mutation
    #     of a hub stayed invisible and condemned a sibling lane's gate as FAILED. The rule is
    #     the only enforcement there can be- the fence is advisory from its own side- so it
    #     must reach BOTH build renders, name the CALL rather than merely the module, and stay
    #     out of the reply lanes, which never red-proof anything.
    import baxter_hub_edit as _hub
    assert callable(getattr(_hub, "mutating", None)), \
        "REDPROOF_FENCE_RULE names machinery that is gone: baxter_hub_edit.mutating"
    assert "baxter_hub_edit.mutating(" in REDPROOF_FENCE_RULE, \
        "the red-proof rule never names the CALL- a lane told only the module name opens nothing"
    assert REDPROOF_FENCE_RULE in _bt.build_worker_prompt(), \
        "the live BUILD worker prompt does not carry REDPROOF_FENCE_RULE"
    assert REDPROOF_FENCE_RULE in _bt.build_worker_prompt(repair=True), \
        "the diagnose-and-repair worker's prompt dropped REDPROOF_FENCE_RULE"
    assert REDPROOF_FENCE_RULE not in WORKER_RULES, \
        "REDPROOF_FENCE_RULE leaked into the reply lanes- they never red-proof anything"

    def _no_redproof(*a, **k):
        return _real_bp(*a, **k).replace(REDPROOF_FENCE_RULE, "")
    _bt.build_worker_prompt = _no_redproof
    try:
        assert check(quiet=True) == 1, \
            "a build prompt without REDPROOF_FENCE_RULE must FAIL the check"
        assert "build_worker_prompt is missing REDPROOF_FENCE_RULE" in ALERT.read_text(encoding="utf-8"), \
            "the check failed without naming the build prompt or the red-proof rule it lost"
    finally:
        _bt.build_worker_prompt = _real_bp

    # ...and a RENAMED build prompt is drift wearing a disguise: the guard must not fall
    # silent simply because the function it watches has gone.
    del _bt.build_worker_prompt
    try:
        assert check(quiet=True) == 1, "a vanished build prompt must FAIL the check"
        assert "unguarded" in ALERT.read_text(encoding="utf-8"), ALERT.read_text(encoding="utf-8")
    finally:
        _bt.build_worker_prompt = _real_bp
    assert check(quiet=True) == 0, "the tree must be clean again once the prompt is restored"

    # 4c. THE VOICE (Atul, 8th July: every AI adopts one Jarvis register). Same shape as the
    #     cases above: prove the guard BITES on each way the single source can stop being
    #     single. Every mutation is restored in BYTES, in a finally: a read_text/write_text
    #     round trip would rewrite every LF as CRLF and "restore" a file it had corrupted
    #     ([[mutate-in-bytes-not-text]]), so each case hashes before and asserts after.
    import baxter_resurrect_audit as _ra

    def _bytes_guarded(path, mutate, expect_in_alert, why):
        """Mutate a file, demand check() fails and NAMES it, restore, demand byte-identity."""
        raw = path.read_bytes()
        before = hashlib.sha256(raw).hexdigest()
        try:
            path.write_bytes(mutate(raw))
            assert check(quiet=True) == 1, why
            got = ALERT.read_text(encoding="utf-8")
            assert expect_in_alert in got, f"{why}- the failure did not name it: {got!r}"
        finally:
            path.write_bytes(raw)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before, \
            f"the selftest corrupted {path.name} restoring it"

    # An md surface whose fenced region goes stale must fail, and be named.
    _bytes_guarded(VAULT / "AGENTS.md",
                   lambda raw: raw.replace(b"- Address Atul as `sir`.", b"- Be casual."),
                   "AGENTS.md's voice block has drifted",
                   "a stale md voice surface must FAIL the check")

    # A DELETED single source must fail LOUDLY- one clear failure naming the file- rather
    # than raising out of the guard and taking the whole triage pass down with it.
    _bytes_guarded(VOICE_MD, lambda raw: b"nothing here\n",
                   "the single voice source is unusable",
                   "a voice source without markers must FAIL the check, not raise")

    # A Python surface that drops voice() must fail, and be named. Not grepped- RENDERED.
    _real_ap = _ra._answer_prompt
    try:
        _ra._answer_prompt = lambda items: "a prompt that forgot the voice"
        assert check(quiet=True) == 1, "a python surface without voice() must FAIL the check"
        assert "_answer_prompt is missing VOICE_BLOCK" in ALERT.read_text(encoding="utf-8"), \
            "the check failed without naming the surface that lost the voice"
        # ...and a RENAMED surface is that same drift wearing a disguise.
        del _ra._answer_prompt
        assert check(quiet=True) == 1, "a vanished voice surface must FAIL the check"
        assert "unguarded" in ALERT.read_text(encoding="utf-8"), ALERT.read_text(encoding="utf-8")
    finally:
        _ra._answer_prompt = _real_ap

    # set_voice_md() must RESETTLE a drifted surface, and leave everything outside the
    # markers alone- CLAUDE.md's governor bands and hard lines are not the voice's to touch.
    _c = VAULT / "CLAUDE.md"
    _raw = _c.read_bytes()
    try:
        _c.write_bytes(_raw.replace(b"- Address Atul as `sir`.", b"- Be casual."))
        assert check(quiet=True) == 1
        assert set_voice_md() == ["CLAUDE.md"], "set_voice_md restamped the wrong surfaces"
        assert check(quiet=True) == 0, "set_voice_md did not resettle the drifted surface"
        assert _c.read_bytes() == _raw, "set_voice_md did not restore CLAUDE.md byte for byte"
    finally:
        _c.write_bytes(_raw)
    assert b"## Hard lines (all Baxters)" in _c.read_bytes(), "the stamp ate CLAUDE.md's hard lines"

    # ...and it stamps each surface in THAT SURFACE'S line endings. A block built with bare
    # LFs never equals the fenced region of a CRLF file, so before 11th July the stamp
    # rewrote CLAUDE.md on every single triage pass and left it mixed. The idempotence case
    # is the one that bites: a clean tree must be a NO-OP, on both endings, in a scratch
    # copy so the real surfaces are never touched.
    assert set_voice_md() == [], "the stamp rewrites an undrifted surface"
    import tempfile as _tf   # `tempfile` itself is a local of this function, bound below
    _d = Path(_tf.mkdtemp())
    for _nl in (b"\r\n", b"\n"):
        _p = _d / f"surface{len(_nl)}.md"
        _p.write_bytes((VOICE_BEGIN + "\n" + voice() + "\n" + VOICE_END + "\n").encode("utf-8")
                       .replace(b"\n", _nl))
        _keep = _p.read_bytes()
        assert set_voice_md([_p]) == [], f"the stamp rewrites an undrifted {_nl!r} surface"
        assert _p.read_bytes() == _keep, "a no-op stamp still changed the bytes"
        _p.write_bytes(_keep.replace(b"- Address Atul as `sir`.", b"- Be casual."))
        assert set_voice_md([_p]) == [_p.name], f"the stamp skipped a drifted {_nl!r} surface"
        assert _p.read_bytes() == _keep, f"the stamp lost the {_nl!r} endings of a surface"
        assert _eol(_p.read_bytes()) == _nl, "the stamped block carries the wrong ending"

    assert check(quiet=True) == 0, "the tree must be clean again after the voice cases"

    # 5. THE TRIGGER, END TO END (Atul, 9th July). Drift must queue a REAL build through the
    #    governor, at the priority its severity earned, carrying proof- and must say NOTHING
    #    to Atul. The queue is redirected to a scratch file and the outward call is STUBBED:
    #    a selftest that reaches baxter_say posts a false alert to him.
    import tempfile
    import types
    import baxter_usage as _gov
    import baxter_autobuild as _ab

    d = Path(tempfile.mkdtemp())
    saved = (_gov.TASK_QUEUE, _gov.QUEUE_LOCK, ALERT, PINGED, subprocess)
    calls = []
    _gov.TASK_QUEUE, _gov.QUEUE_LOCK = d / "q.json", d / "q.lock"
    ALERT, PINGED = d / "a.json", d / "p"
    subprocess = types.SimpleNamespace(run=lambda *a, **k: calls.append(a), DEVNULL=0)

    def _silent_prompt(body, cid, mid):
        return "a builder that forgot the rules, on the live audit path"
    _silent_prompt.__module__ = "baxter_slash"
    baxter_slash._silent_prompt = _silent_prompt
    try:
        assert check(ping=True, quiet=True) == 1, "drift not detected on the live audit path"
        q = json.loads((d / "q.json").read_text(encoding="utf-8"))
        assert len(q) == 1, f"a noticed problem queued {len(q)} builds, expected exactly 1"
        e = q[0]
        assert int(e["priority"]) == _ab.priority_of(DRIFT_SEVERITY), e
        assert e.get("verify"), "the auto-build carries no proof"
        assert not calls, "pinged Atul for a meager issue"

        # A second pass with the fault still present must REFRESH the entry, never fork one.
        assert check(ping=True, quiet=True) == 1
        q2 = json.loads((d / "q.json").read_text(encoding="utf-8"))
        assert len(q2) == 1 and q2[0]["id"] == e["id"], "the drift forked a duplicate build"
        assert not calls, "the second pass pinged him"

        # ...and silence is earned by HAVING QUEUED something. If the repair cannot be
        # queued, the fault is unhandled and he hears about it after all.
        def _boom(**kw):
            raise RuntimeError("the queue is down")
        _real, _ab.notice = _ab.notice, _boom
        try:
            assert check(ping=True, quiet=True) == 1
            assert calls, "a drift we could NOT repair went unreported- that is real silence"
        finally:
            _ab.notice = _real
    finally:
        del baxter_slash._silent_prompt
        _gov.TASK_QUEUE, _gov.QUEUE_LOCK, ALERT, PINGED, subprocess = saved

    assert check(quiet=True) == 0, "the tree should be clean again"
    print("rules selftest OK: clean tree passes; a rules-less builder, a builder missing "
          "AUTOBUILD_RULE and a retyped ban all fail; a stale md voice block, a python surface "
          "that drops voice(), a renamed surface and a markerless baxter_voice.md each fail by "
          "NAME rather than raising; set_voice_md resettles a drifted surface byte for byte, "
          "stamps CRLF and LF surfaces in their own endings, and no-ops on a clean tree; "
          "the source-mid clause names its message id; a noticed drift queues its own repair "
          "silently, dedups across passes, and pings only when no repair could be queued.")


if __name__ == "__main__":
    # `--audit` is the triage pass (may ping Atul once an hour). A bare run is a dev check.
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    sys.exit(check(ping="--audit" in sys.argv, quiet="--quiet" in sys.argv))
