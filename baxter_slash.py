"""baxter_slash - NATIVE Discord slash commands (/usage, /on, /off).

the owner, 6th July: typed "/off"/"/on" text commands can silently fail with no feedback-
he wants REAL Discord slash commands (autocomplete + guaranteed instant confirmation).
This is a tiny gateway bot (discord.py) that ONLY handles slash-command interactions-
it ignores messages entirely (the claude --channels session still owns the chat), so
the two coexist: chat via the plugin, commands via here.

Each command touches the SAME state files as the text commands in baxter_fast.py, so
slash and text behave identically:
  /usage -> read the baked line from .baxter_usage_live.json, reply
  /off   -> write .baxter_off (pause), reply
  /on    -> remove .baxter_off + drop .baxter_catchup (resume + backfill), reply

Runs on Python 3.12 (where discord.py 2.7 is installed). Persistent process- launched +
kept alive by the watcher, same pattern as the channel keeper / whatsapp bridge.
"""
import asyncio
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ---- HEADLESS SELFTEST BOOTSTRAP- must precede `import discord` --------------------
# A `--selftest*` flag is run as plain `python baxter_slash.py --selftest-...`, and the
# `python` on PATH is 3.11, which has NO discord.py. The bot itself runs on 3.12, where it
# is installed. So re-exec into the interpreter that actually serves the owner rather than
# skipping the import and testing a stand-in: a green selftest on the wrong runtime is the
# exact failure [[trust-but-verify-always]] exists for (9th July: the fast lane verified
# clean while the resident listener served the old code).
#
# Matched by PREFIX, not by literal flag: every selftest added here wants the identical
# bootstrap, and the one that forgets to re-exec is the one that quietly tests 3.11.
#
# THE INVARIANT: sys.argv belongs to whoever is RUNNING the process, never to a module it
# imports. An imported module that keys behaviour off argv hijacks its importer- so the flag
# is read only when this file is the entry point. The re-exec still fires for a direct
# `python baxter_slash.py --selftest*` run, and only for that. Held by
# baxter_bootstrap_selftest.py, which imports this module under a `--selftest` argv.
_SELFTEST = __name__ == "__main__" and any(a.startswith("--selftest") for a in sys.argv)

if _SELFTEST and not os.environ.get("BAXTER_SLASH_REEXEC"):
    import importlib.util
    if importlib.util.find_spec("discord") is None:
        _cands = [os.environ.get("BAXTER_PY312"),
                  r"C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe"]
        for _exe in _cands:
            if _exe and os.path.exists(_exe):
                _env = dict(os.environ, BAXTER_SLASH_REEXEC="1")
                sys.exit(subprocess.call([_exe, os.path.abspath(__file__)] + sys.argv[1:], env=_env))
        print("cannot run the selftest: no interpreter with discord.py installed.\n"
              "Set BAXTER_PY312 to one, or run it with the 3.12 that hosts the bot.")
        sys.exit(1)

import discord
from discord import app_commands

# reuse the fast lane's governor logic as ONE source of truth (no dup gate).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import baxter_fast as bf
import baxter_send_dedup as dedup  # THE one cross-process locking scheme. Every claim-ledger
                                   # write below goes through it- see _claim().
import baxter_lanes as lanes   # 3-lane concurrency + session ledger (the owner, 8th July)
import baxter_siblings as siblings  # THE addressed-to check, shared with the fast lane
import baxter_usage as gov     # the governor: queue_read/queue_write/lane helpers. IMPORTED,
                               # never reimplemented- run order lives in gov._qkey alone, and a
                               # second copy of it here would be the next build-queue bug.
import baxter_rules as rules       # EVERY prompt rule, defined ONCE. Never retype one into a
                                   # prompt here- that duplication was the bug (the owner, 9th July).
                                   # It re-exports the reminder + channel-read rules too.

REPLY_WORKER = str(Path(__file__).resolve().parent / "baxter_reply_worker.py")

VAULT = Path(r"C:\Users\you\Documents\Baxter")
SECRETS = VAULT / ".baxter_secrets.json"
LIVE = VAULT / ".baxter_usage_live.json"
OFF_FLAG = VAULT / ".baxter_off"
CATCHUP = VAULT / ".baxter_catchup"
BREACH_STEP = VAULT / ".baxter_breach_step"
LOG = VAULT / ".baxter_slash.log"
USAGE_PY = r"C:\Users\you\Documents\Python Scripts\utils\baxter_usage.py"
GUILD_ID = 111111111111111111

# The headless selftests connect to nothing, so they must not need a token to exist. Reading it
# there would make the check die on a missing secrets file rather than test the thing.
TOKEN = "" if _SELFTEST else json.loads(SECRETS.read_text(encoding="utf-8-sig"))["discord_bot_token"]

# ---- REAL-TIME EVENT-DRIVEN LISTENER (7 Jul) --------------------------------------
# the owner wants an INSTANT reply in EVERY channel- no polling. This gateway connection (the
# SAME one that serves the slash commands, still the ONLY 2nd connection on the Baxter
# token beside the live plugin session) now also fires on_message and dispatches in real
# time. De-dup is the top priority: the live plugin session owns @mentions in its 3 plugin
# channels; this listener owns everything else. Every reply still funnels through baxter_say
# -> baxter_send_dedup (atomic, keyed on channel+reply_to, 5h), so even a race can never
# post twice.
OWNER_ID = 333333333333333301                       # discord_only_user_id- the sole human served
ACCESS_JSON = Path(r"C:\Users\you\.claude\channels\discord\access.json")   # plugin-owned groups (READ-ONLY)
SAY = r"C:\Users\you\Documents\Python Scripts\utils\baxter_say.py"
# NO queue CLI path here on purpose. The listener queues by IMPORTING the governor
# (bf.pre_enqueue_one -> gov.enqueue), synchronously, so the write lands before the ack that
# describes it. The retired `_spawn_queue` fired a detached `--queue` Popen at the same
# instant as the ack beside it, and the ack could not name a position because a Popen returns
# none. Never re-add a shell-out here (9th July).
LISTENER_HANDLED = VAULT / ".baxter_listener_handled.json"   # own ledger (survives restart)
FAST_HANDLED = VAULT / ".baxter_fast_handled.json"           # shared- claim here to mute the poll
LISTENER_WORKER_LOG = VAULT / ".baxter_listener_worker.log"
REACTED = VAULT / ".baxter_fast_reacted.json"       # 👀 work-start receipts, shared w/ fast lane (dedup)
ARCHIVE_CH = {"222222222222222201"}                # activity-log: log-only, never chatter (the owner, 7th July)
PLUGIN_FALLBACK = {"222222222222222202", "222222222222222201", "222222222222222203"}
# coc-farm is BOTH a conversational channel (Baxter answers, like #general) AND the CoC
# daemon's command surface (coc_bot/coc_discord.py polls it and replies to exact keywords).
# Baxter now handles all conversation here, but must NOT also answer a bare CoC command-
# that would double the daemon's reply. So bare CoC commands are deferred to the daemon;
# everything else Baxter answers. The set MIRRORS coc_discord.dispatch()'s triggers.
COC_FARM_ID = "222222222222222203"
COC_CMDS = {"status", "s", "stat", "sup", "how's it going", "hows it going",
            "pause", "hold", "stop", "stop farming", "farmstop", "resume", "unpause", "go", "start",
            "farm", "dark", "black", "screen dark", "screen off", "screen black", "lights off",
            "back", "wake", "light", "lights on", "screen on", "screen back", "undark",
            "boot", "farm now", "reboot", "run", "help", "?", "commands", "cmds"}


def _is_coc_command(content):
    """True when a coc-farm message is a bare CoC daemon command (coc_discord owns the reply)-
    so Baxter defers rather than double-answering. Mirrors coc_discord.dispatch()'s matching."""
    t = (content or "").strip().lower()
    return t in COC_CMDS or t.startswith("wall")
START_TS = None                                    # set on first ready- ignore anything older (no history replay)

# CoC screen-black ("blacken") - mirrors coc_bot cmd_dark/cmd_back exactly.
COC_DIR = Path(r"C:\Users\you\Documents\Python Scripts\coc_bot")
FORCE_DARK = COC_DIR / "force_dark.py"
SET_BRIGHT = COC_DIR / "set_brightness.ps1"
CREATE_NO_WINDOW = 0x08000000


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _entry(name, interaction):
    """Log a slash interaction the INSTANT it lands- BEFORE the 3s ack (the owner, 8th July: a
    /blacken dropped with no reply AND no log line, so we couldn't even see it arrived). An
    entry line here turns any silent drop- ack miss, dup race, crash- into a visible
    diagnostic: if a command runs, this fires first, no exceptions."""
    try:
        uid = getattr(getattr(interaction, "user", None), "id", "?")
        _log(f"/{name} interaction ARRIVED (user {uid})")
    except Exception:
        _log(f"/{name} interaction ARRIVED")


async def _safe_defer(interaction, name):
    """defer() is the single 3s-ack point; if it raises (loop stall, dup already-acked) the
    handler used to bubble the error into discord.py's void with no log. Now the failure is
    logged with its reason. Returns True if the ack landed (proceed), False if it missed
    (bail- Discord already showed 'interaction failed', a followup would only error again)."""
    try:
        await interaction.response.defer()
        return True
    except Exception as e:
        _log(f"/{name} defer FAILED (missed 3s ack / already acked): {type(e).__name__}: {e}")
        return False


def _stamp(p):
    try:
        p.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except Exception as e:
        _log(f"stamp {p.name} failed: {e}")


# guilds + messages: the real-time listener needs message events. message_content is a
# privileged intent already enabled app-side (the live --channels plugin reads message text
# on this same app), so no dev-portal step- but we verify content is non-empty at runtime.
intents = discord.Intents.none()
intents.guilds = True
intents.messages = True
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
GUILD = discord.Object(id=GUILD_ID)


@tree.command(name="usage", description="Baxter's current Claude usage", guild=GUILD)
async def usage(interaction: discord.Interaction):
    _entry("usage", interaction)
    try:
        line = json.loads(LIVE.read_text(encoding="utf-8-sig")).get("line") or "Usage unavailable right now, sir."
    except Exception:
        line = "Usage unavailable right now, sir."
    try:
        await interaction.response.send_message(line)
        _log("/usage answered")
    except Exception as e:
        _log(f"/usage send FAILED: {type(e).__name__}: {e}")


@tree.command(name="off", description="Pause Baxter- go quiet, free the PC", guild=GUILD)
async def off(interaction: discord.Interaction):
    _entry("off", interaction)
    _stamp(OFF_FLAG)
    try:
        await interaction.response.send_message(
            "\U0001F634 Paused, sir- going quiet: no briefs, pings or background work. /on to bring me back.")
        _log("/off -> paused")
    except Exception as e:
        _log(f"/off send FAILED (paused anyway): {type(e).__name__}: {e}")


@tree.command(name="on", description="Resume Baxter", guild=GUILD)
async def on(interaction: discord.Interaction):
    _entry("on", interaction)
    try:
        OFF_FLAG.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"/on unlink failed: {e}")
    _stamp(CATCHUP)
    try:
        await interaction.response.send_message(
            "✅ Resumed, sir- back on and catching up on anything that landed while I was off.")
        _log("/on -> resumed")
    except Exception as e:
        _log(f"/on send FAILED (resumed anyway): {type(e).__name__}: {e}")


def _proc_running(needle):
    """Match python* processes only (the owner, 8th July- a bare CommandLine substring match
    can false-positive on an unrelated process that merely mentions the script name, e.g.
    an editor with the file open, which silently skipped a /blacken launch).

    encoding='utf-8' is load-bearing. Unpinned, text=True decodes through cp1252; one
    non-ANSI byte anywhere in a matched process's command line killed the reader thread,
    stdout came back None, and the bare except below reported 'nothing is running'- so
    /blacken relaunched an enforcer that was already up."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process|Where-Object{$_.Name -like 'python*' -and "
             f"$_.CommandLine -match '{needle}'}}|Measure-Object).Count"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=CREATE_NO_WINDOW).stdout.strip()
        return int(out) > 0
    except Exception:
        return False


def _blacken():
    """Launch the force_dark enforcer (idempotent)- exactly what coc cmd_dark does.
    Returns True only once the process is CONFIRMED alive, so a silent Popen/launch
    failure never gets reported to the owner as a success (8th July- caught reporting
    success while the enforcer never actually started)."""
    if _proc_running("force_dark.py"):
        return True
    subprocess.Popen([sys.executable, str(FORCE_DARK)], cwd=str(COC_DIR),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=CREATE_NO_WINDOW)
    time.sleep(0.8)
    return _proc_running("force_dark.py")


def _unblacken():
    """Stop the enforcer, restore brightness 80 + display on- exactly what coc cmd_back does.
    Returns True only once the enforcer is CONFIRMED gone, so a failed kill never gets
    reported as restored while it's still re-blackening every 1s."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process|Where-Object{$_.Name -like 'python*' -and "
                    "$_.CommandLine -match 'force_dark.py'}"
                    "|ForEach-Object{Stop-Process -Id $_.ProcessId -Force}"],
                   capture_output=True, timeout=12, creationflags=CREATE_NO_WINDOW)
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(SET_BRIGHT), "-Level", "80"],
                   capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW)
    try:  # SC_MONITORPOWER -1 = display ON
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x0112, 0xF170, -1, 0x0002, 1000, ctypes.byref(ctypes.c_ulong()))
    except Exception:
        pass
    time.sleep(0.5)
    return not _proc_running("force_dark.py")


def _breach_step():
    """Run baxter_usage --breach-step (lift the CURRENT usage limiter to the next tier), then
    read back the marker to compose the butler confirmation. The triage queue-pump re-reads
    the gate every cycle, so once this lifts it, the next queued build resumes on its own-
    no further nudge needed (blocked() now returns clear up to the new ceiling)."""
    try:
        subprocess.run([sys.executable, USAGE_PY, "--breach-step"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=60, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        _log(f"/breach --breach-step failed: {e}")
        return "⚠️ Couldn't lift the breach just now, sir- do try again in a moment."
    sc = wc = None
    try:
        m = json.loads(BREACH_STEP.read_text(encoding="utf-8-sig"))
        sc = m.get("session_ceiling")
        wc = m.get("weekly_ceiling")
    except Exception:
        pass
    if sc is not None and wc is not None:
        return (f"⚡ Breach lifted, sir- the next build runs until weekly {wc:.0f}% or "
                f"session {sc:.0f}%, whichever comes first, then I re-pause. /breach again to step further.")
    return ("⚡ Breach lifted, sir- the next build runs one tier further, then I re-pause. "
            "/breach again to step further.")


@tree.command(name="breach", description="Stepped usage breach- run one tier further, then re-pause", guild=GUILD)
async def breach(interaction: discord.Interaction):
    _entry("breach", interaction)
    if not await _safe_defer(interaction, "breach"):
        return
    line = await asyncio.get_running_loop().run_in_executor(None, _breach_step)
    await interaction.followup.send(line)
    _log("/breach -> step-breach set")


@tree.command(name="blacken", description="Force the screen black (CoC dark mode)", guild=GUILD)
async def blacken(interaction: discord.Interaction):
    _entry("blacken", interaction)
    if not await _safe_defer(interaction, "blacken"):
        return
    ok = await asyncio.get_running_loop().run_in_executor(None, _blacken)
    if ok:
        await interaction.followup.send(
            "🌑 Screen forced black, sir- brightness 0, display off, re-darkens every 1s. /unblacken to restore.")
        _log("/blacken -> force_dark launched")
    else:
        await interaction.followup.send(
            "⚠️ /blacken failed, sir- force_dark never came up. Screen's untouched, nothing to undo.")
        _log("/blacken -> FAILED (force_dark not confirmed alive)")


@tree.command(name="unblacken", description="Restore the screen from black (CoC back)", guild=GUILD)
async def unblacken(interaction: discord.Interaction):
    _entry("unblacken", interaction)
    if not await _safe_defer(interaction, "unblacken"):
        return
    ok = await asyncio.get_running_loop().run_in_executor(None, _unblacken)
    if not ok:   # one retry- the kill can race a mid-cycle relaunch, second pass clears it
        ok = await asyncio.get_running_loop().run_in_executor(None, _unblacken)
    if ok:
        await interaction.followup.send(
            "☀️ Screen restored, sir- brightness 80, display on, enforcer stopped.")
        _log("/unblacken -> restored")
    else:
        await interaction.followup.send(
            "⚠️ /unblacken failed twice, sir- force_dark is still alive and re-darkening. Needs a manual look.")
        _log("/unblacken -> FAILED (force_dark still alive after two kill attempts)")


# ---- /farm + /farmstop: force-action the CoC farm from his phone (the owner, 9th July 09:12) ---
# "so I can force action it when needed". coc_bot/coc_discord.py ALREADY owns every farm state
# change; this layer only calls it. No path arithmetic here, no second implementation of
# pause/resume/boot- [[coc-farm-dual-owner]]: a forked copy double-replies, then drifts.
_COC = None


def _coc():
    """Import coc_discord LAZILY, inside a handler- never at module import.

    coc_discord pulls `import nowin`, which monkeypatches subprocess.Popen process-wide to add
    CREATE_NO_WINDOW. Every Popen in this file already passes that flag explicitly, so the
    patch is a harmless no-op here- but it only becomes reachable once he actually runs a farm
    command, rather than mutating the bot's process on every start. It also pulls `notify`,
    which we don't want on the gateway's import path. Cached after the first call."""
    global _COC
    if _COC is None:
        if str(COC_DIR) not in sys.path:
            sys.path.insert(0, str(COC_DIR))
        import coc_discord
        _COC = coc_discord
    return _COC


@tree.command(name="farmstop", description="Stop the CoC farm (pause boots + farming)", guild=GUILD)
async def farmstop(interaction: discord.Interaction):
    """Always permitted, OFF or not- stopping a farm is never the unsafe direction."""
    _entry("farmstop", interaction)
    if not await _safe_defer(interaction, "farmstop"):
        return
    line = await asyncio.get_running_loop().run_in_executor(None, lambda: _coc().cmd_pause())
    await interaction.followup.send(line)
    _log("/farmstop -> auto_pause set")


@tree.command(name="farm", description="Force a CoC farm run now (clears the pause, boots)", guild=GUILD)
async def farm(interaction: discord.Interaction):
    """Clear the pause, then boot- via coc_discord.cmd_farm, the one owner of both.

    Gated on the grandmaster OFF flag ([[off-must-set-coc-auto-pause]]): /off exists to free the
    PC to game, and a farm boot is precisely the lag he switched it off to avoid. We REFUSE and
    touch nothing rather than silently clearing his pause.

    cmd_boot's verdict is surfaced VERBATIM- it already distinguishes a boot that survived, a
    deliberate stand-down (CoC open on his phone) and a crash. Wrapping it in a blanket
    "🚀 farming!" is the exact lie this build exists to remove."""
    _entry("farm", interaction)
    if not await _safe_defer(interaction, "farm"):
        return
    if OFF_FLAG.exists():
        await interaction.followup.send(
            "\U0001F634 I'm off, sir- farming stays down so the PC is free. /on first, then /farm.")
        _log("/farm REFUSED- grandmaster OFF is set")
        return
    lines = await asyncio.get_running_loop().run_in_executor(None, lambda: _coc().cmd_farm())
    await interaction.followup.send(lines)
    _log(f"/farm -> {' | '.join(lines.splitlines())}")


# ---- /queue: the build queue, live + reorderable (the owner, 8th July 20:49 + 22:32) ----------
# "Show the build queue, sleek + dynamically updating" (20:49) and "an easy way for me to
# organise the build order" (22:32). One surface: he sees what's building, what's waiting,
# and reprioritises any of it from his phone.
#
# Reordering means RENUMBERING `priority`, because priority IS the build order: the pump
# walks gov.queue_read(), which sorts on gov._qkey = (priority, queued_at). There is no
# private ordering here- a shadow order only this command understood would be a second
# scheduler competing with the delegator, and it would silently disagree with `--queue-list`.
# Ties inside a band stay FIFO on queued_at, untouched: `queued_at` is when a task was
# queued, not a slot to be traded, so we never rewrite it to fake a position.
#
# The write goes STRAIGHT onto the entry, not through gov.enqueue(): enqueue keeps
# min(old, new), so it can only ever promote. the owner must be able to demote a build too.
QUEUE_REFRESH_SECS = 30      # live re-render cadence while the view is open
QUEUE_REFRESH_TICKS = 20     # ~10 minutes of it, then the controls retire (no orphan loops)
QUEUE_MAX_OPTIONS = 25       # Discord's hard cap on select options

PRIO_LABELS = {1: "urgent- runs first", 2: "resume- interrupted work", 3: "high",
               4: "high", 5: "default", 6: "low", 7: "low", 8: "background",
               9: "background"}


_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(day):
    if 11 <= day <= 13:
        return f"{day}th"
    return f"{day}{_SUFFIX.get(day % 10, 'th')}"


def _when(iso):
    """'2026-07-08T20:49:00' -> '8th July'. He never reads a raw ISO stamp."""
    try:
        d = datetime.fromisoformat(str(iso))
    except Exception:
        return "?"
    return f"{_ordinal(d.day)} {d:%B}"


def _qk(entry):
    """Stable id for a queue entry. The task TEXT is already the queue's identity (enqueue
    updates by it), so hash that- a list index would go stale the moment he reorders."""
    return hashlib.sha1(str(entry.get("task", "")).encode("utf-8")).hexdigest()[:16]


def _short(text, n=68):
    """One clean label. Strips the '(the owner, 8th July 20:49)' attribution- provenance is noise
    in a list he's skimming on a phone."""
    t = " ".join(str(text or "").split())
    t = re.sub(r"\s*\((?:the owner|his)\b[^)]*\)", "", t)
    return t if len(t) <= n else t[:n - 1].rstrip(" ,.;:-") + "…"


def _prio_of(e):
    try:
        return int(e.get("priority", gov.PRIO_DEFAULT))
    except (TypeError, ValueError):
        return gov.PRIO_DEFAULT


def _live_lanes():
    """What's building right now, one row per live lane, labelled `lane 1` up to gov.LANE_COUNT
    (never lane 0- the owner, 9th July). The label comes from gov.lane_label, so it follows LANE_COUNT
    and no ceiling is written down here."""
    rows = []
    for rf, e in gov.lane_journals():
        if not e:
            continue
        rows.append((gov._lane_id(rf), _short(e.get("task"), 56)))
    return [f"`lane {gov.lane_label(i)}` {t}" for i, t in sorted(rows)]


def _set_priority(key, prio):
    """Renumber one entry's priority and write the queue back. Read-modify-write from disk
    every time, so a concurrent enqueue by triage is never clobbered by a stale snapshot.
    Held under gov.queue_txn() (9th July), so the read and the write are one critical
    section: reading fresh is no use if the pump writes between the read and the write.
    getattr, because this bot is long-lived and may hold a stale import ([[long-lived-process-staleness]]).
    Returns (ok, task_text, old_priority)."""
    import contextlib
    with getattr(gov, "queue_txn", contextlib.nullcontext)():
        q = gov.queue_read()
        for e in q:
            if _qk(e) == key:
                old = _prio_of(e)
                e["priority"] = int(prio)
                gov.queue_write(q)          # sorts on _qkey + atomic replace
                _log(f"/queue reprioritised p{old} -> p{int(prio)}: {str(e.get('task'))[:60]}")
                return True, e.get("task"), old
    _log(f"/queue reprioritise MISSED- no entry for key {key} (drained mid-edit?)")
    return False, None, None


_MOVE_LABELS = {"Top", "Up", "Down", "Bottom", "Slot…"}


def _move_note(msg, ok, where):
    """gov's message, trimmed to something that reads well in an embed footer. A refusal keeps
    its REASON- 'refused' alone tells him nothing about which of the three freezes fired."""
    t = " ".join(str(msg or "").split())
    if ok:
        m = re.search(r"((?:moved to|already sits at) slot .*)$", t)
        return m.group(1) if m else f"moved {where}"
    t = re.sub(r"^refused:\s*", "", t)
    return "refused- " + (t if len(t) <= 160 else t[:159].rstrip(" ,.;:-") + "…")


def _apply_move(key, where):
    """Move the selected entry within its priority band. Returns (ok, note).

    ALL ordering logic lives in gov.queue_move- the run order has exactly one implementation
    and a second copy here would be the next build-queue bug. This layer only resolves the
    panel's _qk() hash back to a queue id and carries gov's verdict to the footer.

    getattr, because this bot is long-lived and may be holding a stale import of the governor
    from before queue_move existed ([[long-lived-process-staleness]])- better a clean 'restart
    me' than an AttributeError swallowed into a silent no-op."""
    mv = getattr(gov, "queue_move", None)
    if not callable(mv):
        return False, "refused- reordering needs a newer governor than I'm holding; restart me, sir"
    hit = next((e for e in gov.queue_read() if _qk(e) == key), None)
    if hit is None:
        return False, "that build just left the queue"
    ok, msg = mv(str(hit.get("id") or hit.get("task")), where)
    _log(f"/queue move {where!r} {'ok' if ok else 'REFUSED'}: {str(msg)[:90]}")
    return ok, _move_note(msg, ok, where)


def _frozen_reason(entry):
    """Why the selected entry cannot move, or None. Mirrors gov.queue_move's three freezes so
    the buttons can grey out- but this is COSMETIC. The refusal is enforced in gov, which the
    panel cannot talk its way past; a stale view that greys nothing still cannot reorder."""
    if entry is None:
        return None
    lane = gov._live_lane_of(entry)
    if lane:
        return f"live in lane {lane}"
    if _prio_of(entry) == gov.PRIO_URGENT:
        return "pinned at p1"
    if gov.is_human_gated(entry):
        return f"gated on {gov.gate_of(entry)}"
    return None


def _queue_embed(selected=None, note=None):
    """Render the whole surface. Returns (embed, entries) so the view can rebuild its select
    from exactly the entries that were drawn."""
    q = gov.queue_read()
    em = discord.Embed(title="\U0001F528 Build queue", colour=0x5865F2)

    lanes_now = _live_lanes()
    blocks = ["**Building now**"]
    blocks += lanes_now or ["_All lanes idle._"]

    if not q:
        blocks.append("\n**Queued**\n_Nothing waiting, sir._")
    else:
        band = None
        for i, e in enumerate(q, 1):
            p = _prio_of(e)
            if p != band:
                band = p
                blocks.append(f"\n**p{p} · {PRIO_LABELS.get(p, 'low')}**")
            mark = "\U0001F512 " if gov.is_human_gated(e) else ""      # 🔒 waiting on the owner
            pick = "▸ " if selected and _qk(e) == selected else ""  # ▸ currently selected
            blocks.append(f"`{i:>2}.` {pick}{mark}{_short(e.get('task'))}  ·  _{_when(e.get('queued_at'))}_")

    desc = "\n".join(blocks)
    if len(desc) > 4000:                       # embed description hard cap is 4096
        desc = desc[:3990].rsplit("\n", 1)[0] + "\n_…trimmed._"
    em.description = desc

    # Selecting a build that cannot move says so, rather than just greying four buttons out
    # and leaving him to guess which of the three freezes he has hit.
    if note is None and selected:
        why = _frozen_reason(next((x for x in q if _qk(x) == selected), None))
        if why:
            note = f"frozen- {why}, so it cannot be moved"

    gated = sum(1 for e in q if gov.is_human_gated(e))
    tail = f"{len(q)} queued" + (f" · {gated} waiting on you" if gated else "")
    em.set_footer(text=f"{note + ' · ' if note else ''}{tail} · updated {datetime.now():%H:%M} · "
                       f"live for {QUEUE_REFRESH_SECS * QUEUE_REFRESH_TICKS // 60} min")
    return em, q


class _TaskSelect(discord.ui.Select):
    def __init__(self, entries, selected):
        opts = []
        for i, e in enumerate(entries[:QUEUE_MAX_OPTIONS], 1):
            k = _qk(e)
            opts.append(discord.SelectOption(
                label=f"{i}. {_short(e.get('task'), 80)}"[:100],
                value=k,
                description=f"p{_prio_of(e)} · queued {_when(e.get('queued_at'))}"[:100],
                default=(k == selected)))
        if not opts:
            opts = [discord.SelectOption(label="nothing queued", value="_none")]
        super().__init__(placeholder="Pick a build…", options=opts, row=0)

    async def callback(self, interaction):
        v = self.view
        v.selected = None if self.values[0] == "_none" else self.values[0]
        await v.rerender(interaction)


class _PrioSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="…then set its priority (lower runs first)",
            options=[discord.SelectOption(label=f"p{p}", value=str(p),
                                          description=PRIO_LABELS[p][:100])
                     for p in sorted(PRIO_LABELS)], row=1)

    async def callback(self, interaction):
        v = self.view
        if not v.selected:
            await interaction.response.send_message("Pick a build first, sir.", ephemeral=True)
            return
        ok, task, old = _set_priority(v.selected, int(self.values[0]))
        note = (f"moved to p{self.values[0]} from p{old}" if ok
                else "that build just left the queue")
        await v.rerender(interaction, note=note)


class _SlotModal(discord.ui.Modal, title="Move to an exact slot"):
    """The typed-slot half of the cozy panel. Discord has no drag-and-drop, so 'put it 3rd'
    is a number he types rather than a card he drags."""

    slot = discord.ui.TextInput(label="Slot within its priority band", placeholder="e.g. 1",
                                max_length=3, required=True)

    def __init__(self, view):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.slot.value).strip()
        if not raw.isdigit() or int(raw) < 1:
            await self._view.rerender(interaction, note="refused- a slot is a whole number from 1")
            return
        ok, note = _apply_move(self._view.selected, int(raw))
        await self._view.rerender(interaction, note=note)


class QueueView(discord.ui.View):
    """View + reorder, auto-refreshing. Retires its controls on timeout so a stale message
    can never write to the queue hours later.

    The reorder controls are the closest thing Discord permits to dragging a card up a list:
    its message components have no draggable surface at all, so ⏫🔼🔽⏬ + a typed slot is the
    cozy equivalent, and the message rewrites itself in place after each move."""

    MOVES = (("Top", "⏫", "top"), ("Up", "\U0001F53C", "up"),
             ("Down", "\U0001F53D", "down"), ("Bottom", "⏬", "bottom"))

    def __init__(self, entries):
        super().__init__(timeout=QUEUE_REFRESH_SECS * QUEUE_REFRESH_TICKS + 30)
        self.selected = None
        self.message = None
        self._move_buttons = [self._mk_move(*m) for m in self.MOVES]
        self._build(entries)

    def _mk_move(self, label, emoji, where):
        """One move button. Built here rather than by four near-identical @button decorators-
        the only thing that differs between them is the word handed to gov.queue_move."""
        b = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=3)

        async def _cb(interaction, _where=where):
            await self._do_move(interaction, _where)

        b.callback = _cb
        return b

    async def _do_move(self, interaction, where):
        if not self.selected:
            await interaction.response.send_message("Pick a build first, sir.", ephemeral=True)
            return
        ok, note = _apply_move(self.selected, where)
        await self.rerender(interaction, note=note)

    def _build(self, entries):
        """Rebuild the selects from the CURRENT queue- positions and priorities shift under
        us on every reorder, and a select still offering the old order would lie.

        The move controls grey out when the selection cannot move (live in a lane, p1-pinned,
        gated), so he is not invited to press a button that will only refuse him."""
        self.clear_items()
        self.add_item(_TaskSelect(entries, self.selected))
        self.add_item(_PrioSelect())
        self.add_item(self.run_next)
        self.add_item(self.refresh)

        sel = next((e for e in entries if _qk(e) == self.selected), None) if self.selected else None
        frozen = bool(_frozen_reason(sel)) or sel is None
        for b in self._move_buttons:
            b.disabled = frozen
            self.add_item(b)
        self.slot.disabled = frozen
        self.add_item(self.slot)

    async def interaction_check(self, interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Not yours to touch.", ephemeral=True)
            return False
        return True

    async def rerender(self, interaction, note=None):
        em, entries = _queue_embed(self.selected, note)
        self._build(entries)
        try:
            await interaction.response.edit_message(embed=em, view=self)
        except Exception as e:
            _log(f"/queue rerender failed: {type(e).__name__}: {e}")

    # 📌, not ⏫: ⏫ now means "move to the top of this band", and two buttons wearing one
    # emoji beside each other is exactly the confusion a cozy panel exists to avoid. This one
    # PINS to p1- it crosses bands, which no move ever does.
    @discord.ui.button(label="Run next", emoji="\U0001F4CC", style=discord.ButtonStyle.primary, row=2)
    async def run_next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        """p1 = 'the owner says do this first'- the contract's own word for it."""
        if not self.selected:
            await interaction.response.send_message("Pick a build first, sir.", ephemeral=True)
            return
        ok, task, old = _set_priority(self.selected, gov.PRIO_URGENT)
        await self.rerender(interaction, note=(f"runs next (was p{old})" if ok
                                               else "that build just left the queue"))

    @discord.ui.button(label="Refresh", emoji="\U0001F504", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.rerender(interaction)

    @discord.ui.button(label="Slot…", emoji="\U0001F522", style=discord.ButtonStyle.secondary, row=3)
    async def slot(self, interaction: discord.Interaction, _button: discord.ui.Button):
        """The exact-placement escape hatch: 'put it 3rd', typed."""
        if not self.selected:
            await interaction.response.send_message("Pick a build first, sir.", ephemeral=True)
            return
        await interaction.response.send_modal(_SlotModal(self))

    async def autorefresh(self):
        """The 'dynamically updating' half: the same message keeps pace with the lanes as
        builds land, without him touching a thing."""
        for _ in range(QUEUE_REFRESH_TICKS):
            await asyncio.sleep(QUEUE_REFRESH_SECS)
            if self.is_finished() or self.message is None:
                return
            em, entries = _queue_embed(self.selected)
            self._build(entries)
            try:
                await self.message.edit(embed=em, view=self)
            except discord.NotFound:
                return                       # he deleted it- nothing to keep alive
            except Exception as e:
                _log(f"/queue autorefresh stopped: {type(e).__name__}: {e}")
                return

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            if self.message:
                em, _ = _queue_embed(self.selected, note="controls retired- /queue again")
                await self.message.edit(embed=em, view=self)
        except Exception:
            pass


def selftest_panel():
    """Prove the cozy /queue reorder panel works, headless- no bot, no gateway, no token.

    Drives _apply_move exactly as the buttons and the slot modal do, against a scratch
    gov.TASK_QUEUE, and asserts:
      1. a move reorders the run order
      2. an exact slot lands (the modal's path)
      3. a live-lane selection is REFUSED with a reason...
      4. ...and that reason reaches the embed footer, rather than being swallowed
      5. the controls exist, and grey out on a frozen selection

    Every outward path is stubbed ([[selftests-stub-every-outward-path]]): a lane selftest
    once reached _say and posted a false alert to the owner."""
    import tempfile
    real = (gov.TASK_QUEUE, gov._log, gov._live_lane_of, globals()["_log"])
    gov.TASK_QUEUE = Path(tempfile.mkdtemp(prefix="baxter_panel_")) / "queue.json"
    gov._log = lambda *a, **k: None
    gov._live_lane_of = lambda e: None          # no real lane journals: the vault's are live
    globals()["_log"] = lambda *a, **k: None    # never touch .baxter_slash.log
    try:
        assert callable(getattr(gov, "queue_move", None)), \
            "the slash layer has nothing to delegate to- gov.queue_move is missing"

        order = lambda: [e["task"] for e in gov.queue_read()]
        key = lambda i: _qk(gov.queue_read()[i])
        for t in ("alpha", "bravo", "charlie"):
            gov.enqueue(t, "step", priority=5, touch_set=[f"utils/x_{t}.py"])
        assert order() == ["alpha", "bravo", "charlie"], order()

        # 1. A MOVE REORDERS, and says where it landed.
        ok, note = _apply_move(key(2), "up")                       # charlie
        assert ok, note
        assert order() == ["alpha", "charlie", "bravo"], order()
        assert "slot 2 of 3" in note, note

        ok, note = _apply_move(key(2), "top")                      # bravo
        assert ok and order() == ["bravo", "alpha", "charlie"], (note, order())

        # 2. AN EXACT SLOT LANDS- what _SlotModal.on_submit hands us, an int not a word.
        ok, note = _apply_move(key(0), 3)                          # bravo -> 3rd
        assert ok and order() == ["alpha", "charlie", "bravo"], (note, order())

        # 3. A LIVE-LANE SELECTION IS REFUSED, with a reason, and nothing moves.
        gov._live_lane_of = lambda e: "3"
        snap = order()
        ok, note = _apply_move(key(1), "top")
        assert not ok, "a live-lane task must refuse a move from the panel"
        assert "lane 3" in note, f"the refusal must say which freeze fired: {note!r}"
        assert order() == snap, "a refused move must not reorder"

        # 4. AND THE REFUSAL REACHES THE FOOTER. A note that never renders is a silent no-op-
        #    he presses the button, the message redraws unchanged, and nothing tells him why.
        em, _entries = _queue_embed(selected=key(1), note=note)
        assert note in em.footer.text, f"the refusal never reached the footer: {em.footer.text!r}"

        # 5. SELECTING A FROZEN BUILD EXPLAINS ITSELF even with no note passed in.
        em, _entries = _queue_embed(selected=key(1))
        assert "frozen- live in lane 3" in em.footer.text, em.footer.text

        #    ...and the controls grey out rather than inviting a press that only refuses.
        v = QueueView(gov.queue_read())
        v.selected = key(1)
        v._build(gov.queue_read())
        moves = [c for c in v.children if getattr(c, "label", None) in _MOVE_LABELS]
        assert len(moves) == 5, f"the cozy controls are missing: {sorted(_MOVE_LABELS)} vs {moves}"
        assert all(c.disabled for c in moves), "a frozen entry must grey out the move controls"

        gov._live_lane_of = lambda e: None
        v._build(gov.queue_read())
        moves = [c for c in v.children if getattr(c, "label", None) in _MOVE_LABELS]
        assert not any(c.disabled for c in moves), "a movable entry must have live controls"

        # 6. A VANISHED ENTRY IS REPORTED, never silently ignored (it drained into a lane
        #    between the render and the press).
        ok, note = _apply_move("deadbeefdeadbeef", "up")
        assert not ok and "left the queue" in note, note

        # 7. A STALE GOVERNOR is named, not crashed on ([[long-lived-process-staleness]]).
        stale, gov.queue_move = gov.queue_move, None
        try:
            ok, note = _apply_move(key(0), "up")
            assert not ok and "restart me" in note, note
        finally:
            gov.queue_move = stale
    finally:
        gov.TASK_QUEUE, gov._log, gov._live_lane_of, globals()["_log"] = real
    print("baxter_slash panel selftest OK: /queue moves within a band, an exact slot lands, a "
          "live-lane entry refuses with a reason that reaches the footer, the controls grey out "
          "on a frozen selection, and a stale governor is named rather than crashed on.")
    return 0


class _FakePopen:
    """Records argv instead of launching LDPlayer. `rc` is what poll() reports, so one class
    drives all three of cmd_boot's verdicts: alive (None), self-abort (3), crash (1)."""
    rc = None
    stderr_text = ""
    calls = []

    def __init__(self, argv, **kw):
        _FakePopen.calls.append(list(argv))
        if _FakePopen.stderr_text and kw.get("stderr") is not None:
            try:
                kw["stderr"].write(_FakePopen.stderr_text)
            except Exception:
                pass

    def poll(self):
        return _FakePopen.rc


class _SubShim:
    """subprocess with ONLY Popen swapped. `coc_discord.subprocess` IS the real module object,
    so patching its Popen attribute would patch subprocess process-wide- including the
    interpreter probe we are trying to exercise for real."""

    def __init__(self, real):
        self._real = real
        self.Popen = _FakePopen

    def __getattr__(self, n):
        return getattr(self._real, n)


class _FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True

    def is_done(self):
        return self.deferred


class _FakeFollowup:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, content=None, **kw):
        self.sink.append(content or "")


class _FakeInteraction:
    """Just enough of discord.Interaction for _entry / _safe_defer / followup.send."""

    def __init__(self, sink):
        self.user = type("U", (), {"id": OWNER_ID})()
        self.response = _FakeResponse()
        self.followup = _FakeFollowup(sink)


def selftest_coc():
    """Prove /farm + /farmstop work, headless- no bot, no gateway, no token, NO REAL FARM.

    Drives the registered slash callbacks exactly as Discord does, and asserts:
      1. 'farmstop' is a real command end to end- COC_CMDS *and* coc_discord.dispatch
      2. /farmstop pauses, and coc_autopilot's own condition SEES the pause (the skipped beat)
      3. /farm refuses under the grandmaster OFF flag, touching nothing
      4. /farm clears the pause and boots- with the cv2-capable interpreter, not sys.executable
      5. cmd_boot's failure + stand-down verdicts reach the owner verbatim, never a blanket success

    Every outward path is stubbed and every write is redirected to a tempdir. coc_discord.HERE
    and OFF_FLAG in particular: writing the REAL coc_bot/auto_pause would pause his live farm
    from a test run, and an abort before cleanup would leave it paused
    ([[selftests-stub-every-outward-path]])."""
    import subprocess as _real_sub
    import tempfile

    coc = _coc()
    tmp = Path(tempfile.mkdtemp(prefix="baxter_coc_"))
    sys.path.insert(0, str(COC_DIR))
    import coc_autopilot as ap

    real = (coc.HERE, coc.subprocess, coc.reply, coc._proc_running,
            globals()["OFF_FLAG"], globals()["_log"], ap.BAXTER_OFF, ap.AUTOPILOT_LOG)
    coc.HERE = str(tmp)                              # never the real coc_bot/auto_pause
    coc.subprocess = _SubShim(_real_sub)             # record argv; never launch LDPlayer
    coc.reply = lambda *a, **k: None                 # never post to Discord
    coc._proc_running = lambda *a, **k: False        # never shell out to WMI
    globals()["OFF_FLAG"] = tmp / ".baxter_off"      # never the real .baxter_off
    globals()["_log"] = lambda *a, **k: None         # never touch .baxter_slash.log
    ap.BAXTER_OFF = str(tmp / ".baxter_off")
    ap.AUTOPILOT_LOG = str(tmp / "autopilot.log")

    auto_pause = tmp / "auto_pause"
    off_flag = tmp / ".baxter_off"
    drive = lambda name: _drive(name)
    try:
        # 1. 'farmstop' IS a command on BOTH sides. In COC_CMDS the listener defers to the
        #    daemon; if dispatch() then had no handler, a typed 'farmstop' would get no reply
        #    from ANYONE- a silent black hole. Assert the pair together, never apart.
        assert "farmstop" in COC_CMDS, "the listener won't defer 'farmstop' to the daemon"
        assert _is_coc_command("farmstop"), "_is_coc_command must mirror COC_CMDS"
        assert coc.dispatch("farmstop") is not None, \
            "'farmstop' is in COC_CMDS but coc_discord.dispatch ignores it- nobody would reply"
        assert auto_pause.exists(), "dispatch('farmstop') must pause"
        auto_pause.unlink()

        # 2. /farmstop PAUSES- and the autopilot's own condition sees it, so the beat it
        #    skips is a fact, not a hope.
        assert not ap.farm_paused(str(tmp))[0], "clean slate: nothing should be paused"
        out = drive("farmstop")
        assert auto_pause.exists(), "/farmstop did not write auto_pause"
        assert "Paused" in out, out
        paused, why = ap.farm_paused(str(tmp))
        assert paused and why == "auto_pause", (paused, why)

        # 3. THE OFF GATE. /farm must refuse and touch NOTHING- /off exists to free the PC to
        #    game, and a farm boot is the exact lag he switched it off to avoid.
        off_flag.write_text("off", encoding="utf-8")
        _FakePopen.calls.clear()
        out = drive("farm")
        assert "/on" in out and "off" in out.lower(), out
        assert not _FakePopen.calls, "/farm booted while OFF- it must touch nothing"
        assert auto_pause.exists(), "/farm cleared the pause while OFF- it must touch nothing"
        assert ap.farm_paused(str(tmp))[0], "still paused, still off"
        off_flag.unlink()

        # 4. /farm CLEARS THE PAUSE AND BOOTS. THE INTERPRETER TRAP: boot.py imports farm/loot
        #    (cv2 + numpy). This bot hosts on 3.12, which has discord but NOT cv2- so a boot
        #    spawned with sys.executable dies on import while the reply says "🚀 Booting".
        _FakePopen.rc, _FakePopen.stderr_text = None, ""
        _FakePopen.calls.clear()
        out = drive("farm")
        assert not auto_pause.exists(), "/farm must clear the pause"
        assert not ap.farm_paused(str(tmp))[0], "autopilot must see the resume"
        assert "Resumed" in out and "Booting" in out, out
        assert len(_FakePopen.calls) == 1, _FakePopen.calls
        argv = _FakePopen.calls[0]
        assert os.path.basename(argv[1]) == "boot.py" and "--farm" in argv, argv
        assert argv[0] == coc.coc_py(), f"boot spawned with {argv[0]!r}, not the resolved interpreter"
        assert _real_sub.run([argv[0], "-c", "import cv2, numpy"],
                             capture_output=True, timeout=60).returncode == 0, \
            f"{argv[0]!r} cannot import cv2+numpy- boot.py would die on import"
        if not coc._can_farm(sys.executable):        # today's reality: the 3.12 host has no cv2
            assert argv[0] != sys.executable, "cmd_boot must not spawn its own interpreter blindly"

        # 5. A DEAD BOOT IS REPORTED AS DEAD, with its actual error- not a blanket success.
        _FakePopen.rc, _FakePopen.stderr_text = 1, "ModuleNotFoundError: No module named 'cv2'\n"
        out = coc.cmd_boot()
        assert "FAILED" in out and "ModuleNotFoundError" in out, out

        #    ...and a DELIBERATE stand-down (CoC open on his phone) is not dressed up as a crash.
        _FakePopen.rc, _FakePopen.stderr_text = coc.BOOT_SELF_ABORT_RC, ""
        out = coc.cmd_boot()
        assert "Stood down" in out and "FAILED" not in out, out
    finally:
        (coc.HERE, coc.subprocess, coc.reply, coc._proc_running,
         globals()["OFF_FLAG"], globals()["_log"], ap.BAXTER_OFF, ap.AUTOPILOT_LOG) = real
    print("coc slash selftest OK: /farmstop pauses and coc_autopilot sees the skipped beat, "
          "/farm refuses under the grandmaster OFF flag, /farm resumes + boots with a "
          "cv2-capable interpreter, and a dead or stood-down boot is reported verbatim.")
    return 0


def _live_guild_commands():
    """The command names Discord ACTUALLY serves for this guild, read over the REST API.

    selftest_coc() runs the handlers in a fresh process on current source, which proves the
    code is right- not that the owner's phone can call it ([[long-lived-process-staleness]]). Only
    the registered tree proves the second thing, so this reads it rather than trusting a sync
    that may have failed, or a resident bot that may still serve older code.

    The explicit User-Agent is load-bearing: Discord 403s urllib's default `Python-urllib/3.x`,
    so without it the gate reds a working build with a permissions error that isn't one. The
    token comes straight from SECRETS because module-level TOKEN is deliberately "" under a
    selftest."""
    token = json.loads(SECRETS.read_text(encoding="utf-8-sig"))["discord_bot_token"]
    hdrs = {"Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/baxter, 1.0)"}

    def _get(url):
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    # a bot's application id IS its user id, so one hop gets us the commands endpoint.
    app_id = _get("https://discord.com/api/v10/users/@me")["id"]
    cmds = _get(f"https://discord.com/api/v10/applications/{app_id}/guilds/{GUILD_ID}/commands")
    return {c["name"] for c in cmds}


def selftest_coc_live():
    """selftest_coc() + the LIVE tree: the handlers are right AND Discord serves them.

    Guild-scoped by design- these commands are registered per-guild (`guild=GUILD`), and the
    global endpoint is empty. Reading the global tree here would report them missing forever.

    The real `coc_bot/auto_pause` and `.baxter_off` are snapshotted around the run and COMPARED,
    never merely asserted absent: if a future stubbing mistake in selftest_coc() writes the real
    pause file, this catches it instead of quietly pausing the owner's live farm
    ([[selftests-stub-every-outward-path]])."""
    real_pause = COC_DIR / "auto_pause"
    real_off = VAULT / ".baxter_off"
    before = (real_pause.exists(), real_off.exists())

    rc = selftest_coc()
    if rc:
        return rc

    after = (real_pause.exists(), real_off.exists())
    assert before == after, (
        f"the selftest MUTATED live state- auto_pause/.baxter_off went {before} -> {after}. "
        "A stub leaked onto the real paths; the owner's farm may now be paused.")

    names = _live_guild_commands()
    missing = {"farm", "farmstop"} - names
    assert not missing, (
        f"the live Discord tree for guild {GUILD_ID} is missing {sorted(missing)}- it serves "
        f"{sorted(names)}. The handlers pass, but his phone cannot call them.")

    print(f"coc slash LIVE selftest OK: the handlers pass headless, Discord serves /farm + "
          f"/farmstop on guild {GUILD_ID} ({len(names)} commands registered), and the run left "
          f"the real auto_pause + .baxter_off untouched.")
    return 0


def _drive(name):
    """Invoke a registered slash callback the way Discord does, and return what the owner is told."""
    sink = []
    cmd = tree.get_command(name, guild=GUILD)
    assert cmd is not None, f"/{name} is not registered on the command tree"
    asyncio.run(cmd.callback(_FakeInteraction(sink)))
    assert sink, f"/{name} answered with nothing"
    return "\n".join(sink)


@tree.command(name="queue", description="The build queue- see it, reorder it", guild=GUILD)
async def queue(interaction: discord.Interaction):
    _entry("queue", interaction)
    if not await _safe_defer(interaction, "queue"):
        return
    try:
        em, entries = await asyncio.get_running_loop().run_in_executor(None, _queue_embed)
        view = QueueView(entries)
        view.message = await interaction.followup.send(embed=em, view=view, wait=True)
        asyncio.create_task(view.autorefresh())
        _log(f"/queue answered ({len(entries)} queued)")
    except Exception as e:
        _log(f"/queue FAILED: {type(e).__name__}: {e}")
        await interaction.followup.send("⚠️ /queue hit an error, sir- logged it.")


@tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global catch-all for ANY slash-command exception (the owner, 8th July- unhandled command
    errors were vanishing into discord.py with no log). Every failure now leaves a line, and
    we try to tell the owner the command dropped rather than leave him staring at silence."""
    cmd = getattr(getattr(interaction, "command", None), "name", "?")
    _log(f"/{cmd} tree.on_error: {type(error).__name__}: {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ /{cmd} hit an error, sir- logged it. Do try again.")
        else:
            await interaction.response.send_message(
                f"⚠️ /{cmd} hit an error, sir- logged it. Do try again.")
    except Exception as e:
        _log(f"/{cmd} on_error notify FAILED: {type(e).__name__}: {e}")


# ---- listener plumbing -------------------------------------------------------------
# The claim ledgers are shared with the fast-lane poll (baxter_fast) and read, unlocked, by
# baxter_triage. There is exactly ONE way to write them: baxter_send_dedup.update_ids, which
# merges under a cross-process lock and lands the file with tmp + os.replace. `_save_ids` used
# to be a bare Path.write_text of a snapshot taken outside any lock- it is gone, and must not
# come back: it is what deleted a concurrent writer's claim. update_ids MERGES and can never
# remove an id, which is right for these three append-only ledgers; anything wanting to prune
# must do it itself under `with dedup.file_lock(path):`.
LISTENER_CAP = 500          # our own ledger- survives restarts, so it remembers further back
SHARED_CAP = 200            # .baxter_fast_handled.json- must match baxter_fast.HANDLED_CAP
REACTED_CAP = 300


def _load_ids(path):
    return dedup.read_ids(path)


def _add_ids(path, ids, cap):
    """Merge ids into an append-only ledger, atomically and under the lock."""
    _after, degraded = dedup.update_ids(path, ids, cap=cap)
    if degraded:
        _log(f"ledger write DEGRADED (lock timeout or write failure) for {Path(path).name}: {ids}")


def _already(mid):
    """This listener's own persistent de-dup- never process the same message twice, even
    across a reconnect/restart."""
    return str(mid) in _load_ids(LISTENER_HANDLED)


def _claim(mid):
    """Claim in our own ledger AND in the fast lane's shared ledger, so the ~15s poll skips
    anything the listener already took.

    Both writes now go through baxter_send_dedup's cross-process lock (9th July). Before that
    they were unlocked read-modify-writes, so a concurrent fast-lane write could clobber this
    claim with its own stale snapshot: on 9th July the listener claimed and answered msg
    444444444444444401 (spawn 09:39:55, reply 09:40:29), its claim was erased, and the fast
    lane re-picked and re-answered the same message at 09:40:40- a second Opus worker for
    nothing. No double reply ever reached the owner: baxter_say's atomic (channel + reply_to) guard
    caught it. That guard remains the SECOND line of defence; this lock is the first, and the
    wasted spawn is now prevented rather than merely absorbed."""
    mid = str(mid)
    _add_ids(LISTENER_HANDLED, [mid], LISTENER_CAP)
    try:
        _add_ids(FAST_HANDLED, [mid], SHARED_CAP)
    except Exception as e:
        _log(f"shared claim for {mid} failed: {e}")


def _plugin_channels():
    """The plugin-owned groups, read LIVE (read-only) from access.json so it stays correct
    as channels change. The live plugin session replies in these ONLY on @mention."""
    try:
        d = json.loads(ACCESS_JSON.read_text(encoding="utf-8-sig"))
        keys = set((d.get("groups") or {}).keys())
        return keys or PLUGIN_FALLBACK
    except Exception:
        return PLUGIN_FALLBACK


def _env():
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def _spawn_say(cid, mid, text):
    """Fire a zero-LLM native reply (governor ack) via baxter_say- detached so the event
    loop never blocks. baxter_say holds the double-send lock.

    The Popen is detached, so this returns before baxter_say does: a denied claim (exit 3)
    cannot be noticed inline. A reaper thread waits on the process off-thread and, on exit 3,
    records the reason in the voiceless denial sink- the ack never left, so nothing here may
    read as a delivered reply ([[outward-path-proven-from-the-send-ledger]])."""
    try:
        p = subprocess.Popen([sys.executable or "python", SAY, "--channel", str(cid),
                              "--reply-to", str(mid), text], cwd=str(VAULT), env=_env(),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        _log(f"listener say spawn failed for {mid}: {e}")
        return

    def _reap(p=p, cid=cid, mid=mid):
        try:
            _out, err = p.communicate(timeout=35)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
            return
        if p.returncode == 3:
            try:
                import baxter_autobuild as autobuild
                row = autobuild.denial_alert("baxter_slash._spawn_say",
                                             f"ch {cid} msg {mid}", err)
                _log(f"DENIED: governor ack to {mid} refused- {row['reason']}")
            except Exception:
                pass

    import threading
    threading.Thread(target=_reap, daemon=True).start()


def _live_big_ask(cid, mid, content, gated):
    """Write the queue entry for a sizeable ask BEFORE this listener says a word about it.
    Returns (position, acked, big).

    THE HOLE THIS CLOSES (the owner, 9th July). `_is_big_ask` stopped excluding @mentions and
    replies-to-Baxter, so the fast lane now recognises them- but the fast lane deliberately
    leaves them ALONE, because they are the live channel session's turf. Nothing wrote the
    entry for them. The live session was told by CLAUDE.md to queue big work, which rests the
    truth of "queued, sir" on a model choosing to make a tool call: the ack-before-act hole,
    on the one surface the 09:38 fix did not cover. The listener sees the @mention first, in
    code, and writes the entry here- so whatever the session says afterwards describes a row
    that already exists.

    It calls pre_enqueue_one, which runs the REAL `_is_big_ask` gate rather than a second copy
    of it. The first pass at the 09:38 fix proved itself by calling _placeholder directly,
    which jumps that gate- and the gate was the bug ([[exam-must-drive-the-gate]]).

    `_is_big_ask` is asked here as well, and only to tell the two empty answers apart:
    pre_enqueue_one returns (None, "") both for "not a sizeable ask" and for "sizeable, but
    the write failed", and those want opposite prompts. Not a big ask -> no block at all, and
    DEFLECT_RULE's own --queue branch stays in force. A failed write -> the blunt refusal
    block, because the one thing that must never follow a failed write is the word "queued".

    THE THREE THINGS IT REPLACES `_spawn_queue` FOR:
      - it writes SYNCHRONOUSLY, in-process, so the ack cannot race the write. _spawn_queue
        was a detached Popen fired at the same instant as the ack beside it.
      - it RETURNS the slot, read off the settled queue, so the ack can state it. The old ack
        was `bf._big_ack()` with no argument at all- and since 09:38 that argument is
        mandatory, so at the wall an @mention big-ask raised TypeError into on_message's
        outer except and the owner got SILENCE plus no queue entry.
      - it passes `source_mid`, so triage's later, better-worded filing upgrades this row
        instead of forking a twin beside it.

    A VITAL is never wall-acked: that ack promises to get to it once usage resets, which is
    precisely what an emergency cannot wait for. It is written at p1 and left to the reply
    worker, which answers him now. See bf._is_vital."""
    m = {"id": str(mid), "content": content or "", "author": {"id": str(OWNER_ID)}}
    uid = str(OWNER_ID)
    if not bf._is_big_ask(m, uid):
        return "", False, False
    try:
        _entry_row, position = bf.pre_enqueue_one(m, uid, gated)
    except Exception as e:
        _log(f"listener: placeholder write RAISED for {mid}: {e}")
        return "", False, True
    if not position:
        # No entry, so no claim. Falling through to the reply worker with the refusal block
        # gets him an answer that says nothing about a queue- never the silence a bare
        # `return` here would give him, and never the lie an ack would.
        _log(f"listener: BIG-ASK placeholder FAILED for {mid}- saying nothing about a queue")
        return "", False, True
    if gated and not bf._is_vital(m):
        _spawn_say(cid, mid, bf._big_ack(position))
        _log(f"listener: BIG-ASK wall ack (no llm) for {mid} at {position}")
        return position, True, True
    _log(f"listener: BIG-ASK placeholder for {mid} at {position} (ack left to the reply worker)")
    return position, False, True


def _reply_parent_block(ref):
    """When the owner used Discord's REPLY feature, the message he replied to IS the context
    (the owner, 8th July- the worker bound to recent chatter instead of the referenced message
    and answered the wrong topic). Given the resolved parent discord.Message, return a block
    to PIN as PRIMARY context above the recent-chatter convo, or '' if there's nothing to pin."""
    if ref is None:
        return ""
    try:
        who = "Baxter (you)" if ref.author.bot else (ref.author.name or "the owner")
        text = (ref.content or "").replace("\n", " / ").strip()[:600]
    except Exception:
        return ""
    if not text:
        return ""
    return ("REPLY TARGET- the owner used Discord's reply feature ON THIS message, so it is what "
            "he is responding to. ANCHOR your answer to it, NOT the recent chatter below:\n"
            f"  {who}: \"{text}\"\n\n")


def _reply_prompt(body, cid, mid, convo, reply_parent="", queued=""):
    """`queued` is the ALREADY QUEUED block for a sizeable ask whose placeholder the listener
    has just written in _live_big_ask- the worker's only job there is to state the slot it is
    handed. It is empty for an ordinary message, which leaves DEFLECT_RULE's own --queue branch
    in force. It is a DEFAULT kwarg because rules.check() renders every `*_prompt` here with
    dummy positional args, and a builder it cannot render is a rule it cannot police."""
    return (
        f"You are Baxter, the owner's butler-assistant. His Obsidian vault is {VAULT} "
        f"(00-Inbox + 20-Projects hold tasks as '- [ ] ...' lines with #project tags and due "
        f"dates like \U0001F4C5 2026-07-04; 40-Drafts holds drafts; Subscriptions.md money). "
        f"{reply_parent}"
        f"{rules.convo_block(convo)}"
        f"He just sent this in Discord channel {cid}:\n\n\"{body}\"\n\n"
        f"{queued}"
        f"{rules.bind_rule(reply_parent)}"
        f"MATCH EFFORT TO THE QUESTION. A quick ask ('is X on?', 'what's my usage?') gets ONE "
        f"short reply, now. A question that needs you to LOOK- 'why does X keep happening?', "
        f"'what's the state of Y?', 'go through the docs and tell me Z'- is answered by READING: "
        f"open whatever vault files and code you need, take the tool calls it genuinely needs, "
        f"and answer him NOW.\n"
        f"{rules.WORKER_RULES}"
        f"{rules.READING_OVERRIDE}"
        f"{rules.source_mid_rule(mid)}"
        f"{rules.reply_via(mid, cid)}"
        f"{rules.voice()}\n"
        f"{rules.SCOPE_RULE}"
    )


def _continuation_prompt(body, cid, mid, reply_parent="", queued=""):
    """Prompt for a RESUMED session (per-channel master, or a reply continuing a thread).
    The session already holds its own identity + the whole prior conversation, so we don't
    re-inject the Baxter preamble or convo- we just hand it the owner's new turn. If he replied to
    a SPECIFIC earlier message (not the latest turn), pin it so the resumed session anchors to
    that message, not merely its own last reply.

    It carries the SAME rules block as a fresh session. It used to carry only the naming rule-
    the tool-call budget, the deflection wording and the one-edit rule had quietly drifted out.
    Nobody decided a resumed session should follow fewer rules (the owner, 9th July). `queued` is
    the same placeholder block _reply_prompt carries, and for the same reason: a resumed
    session is exactly as able to invent a position as a fresh one."""
    return (
        f"the owner just sent a new message in Discord channel {cid}. Continue our ongoing "
        f"conversation- it binds to everything said in this thread so far.\n\n"
        f"{reply_parent}"
        f"\"{body}\"\n\n"
        f"{queued}"
        f"{rules.WORKER_RULES}"
        f"{rules.READING_OVERRIDE}"        # a resumed thread answers reading-questions too
        f"{rules.source_mid_rule(mid)}"
        f"{rules.reply_via(mid, cid)}"
        f"{rules.voice()}\n"
        f"{rules.SCOPE_RULE}"
    )


def _write_prompt_file(text):
    """Stash a worker prompt in a temp file under the lane dir (the worker reads then
    deletes it)- keeps long convo context off the command line.

    NOT a prompt builder, hence the name does not end in `_prompt`: baxter_rules.check()
    treats every `*_prompt` function here as a builder and renders it with dummy args.
    This one writes a file, so it must never match that suffix."""
    d = Path(lanes.LANES_DIR)
    d.mkdir(exist_ok=True)
    p = d / f"prompt-{datetime.now():%H%M%S}-{os.getpid()}-{abs(hash(text)) % 100000}.txt"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _spawn_reply_worker(body, cid, mid, convo, session, mode, reply_parent="", queued=""):
    """Detached, LANE-BOUNDED Baxter worker (baxter_reply_worker) that gives a real, judged
    reply in that channel. 'create' starts a fresh session (--session-id), 'resume' continues
    an existing one (--resume) so a per-channel master thread or a reply keeps full context.
    Carries NO 'big-task' marker so the watcher's usage-enforce spares it below the 90% floor-
    it's vital (answering the owner), like the fast lane. The 3-lane cap lives inside the worker."""
    try:
        prompt = _reply_prompt(body, cid, mid, convo, reply_parent, queued) if mode == "create" \
            else _continuation_prompt(body, cid, mid, reply_parent, queued)
        pf = _write_prompt_file(prompt)
        with open(LISTENER_WORKER_LOG, "a", encoding="utf-8") as out:
            out.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] reply worker for msg {mid} in "
                      f"{cid} (session {session[:8]} {mode}): {body[:80]!r}\n")
        args = [sys.executable or "python", REPLY_WORKER, "--channel", str(cid),
                "--mid", str(mid), "--session", session, "--mode", mode, "--prompt-file", pf]
        if str(cid) == lanes.GENERAL_ID:
            args.append("--general")
        subprocess.Popen(args, cwd=str(VAULT), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW)
        return True
    except Exception as e:
        _log(f"listener reply worker spawn failed for {mid}: {e}")
        return False


# SIBLING BOTS (the owner, 10th July- Baxter was answering messages plainly addressed to Codex/Jem).
# When the owner addresses a sibling bot and NOT Baxter, Baxter stands back with the handsoff reaction
# and never replies- see _route's 'standback' branch and on_message.
#
# THE RULE ITSELF NOW LIVES IN baxter_siblings (11th July). It used to live here alone, and the
# fast-lane poll- the OTHER path that can speak in the same channel- had never heard of it: on
# 11th July it spawned Opus reply workers for two messages the listener had already stood back
# from. Both callers import the one check now. Ids still come from .baxter_secrets.json, and an
# unreadable secrets file still degrades to today's answer-everything conduct (empty set), never
# to silence on Baxter's own messages.
def _sibling_ids():
    return siblings.sibling_ids(SECRETS)


SIBLING_IDS = _sibling_ids()


def _route(cid, content, mentioned):
    """THE DE-DUP RULE as a pure, testable decision (no side effects). Returns exactly one
    dispatch category for a message already known to be from the owner (not a bot):
      'empty'   -> no text to answer (attachment-only / intent not delivering)
      'offon'   -> bare pause toggle: leave it to the fast lane's pure-code path
      'archive'    -> activity-log: one-way (Baxter->the owner), never a chatter reply
      'coc-daemon' -> a bare CoC command in coc-farm: the coc_discord daemon owns that reply
      'standback'  -> addressed to a sibling bot (Codex/Jem), not Baxter: react-only, never reply
      'handle'     -> EVERY other channel- this listener answers via the reply-worker lane
    This yields exactly ONE responder per message: never two, never zero."""
    if not (content or "").strip():
        return "empty"
    if re.match(r"^/?(off|on)$", content.strip(), re.I):
        return "offon"
    if str(cid) in ARCHIVE_CH:
        return "archive"                 # activity-log is one-way (Baxter->the owner): never chatter
    if str(cid) == COC_FARM_ID and _is_coc_command(content):
        return "coc-daemon"              # bare CoC command: the coc_discord daemon answers it, not us
    # FULL CHANNEL-PARITY (the owner, 8th July 20:21- coc-farm went 👀-but-silent, he had to chase
    # in #general twice). EVERY non-archive channel is now answered by THIS listener's headless
    # reply-worker lane, exactly like #general. The old 'skip-plugin-mention' branch deferred
    # any channel in access.json groups (in practice ONLY coc-farm- #general is caught by the
    # GENERAL_ID branch, activity-log is archive) to "the live plugin session / coc_discord
    # daemon", but that path stalls/crashes and left coc-farm silent- the identical symptom
    # that retired the plugin path from #general. The reply-worker path is headless, cheap
    # (sonnet), keeps a per-channel MASTER session's context, and EVERY post (baxter_say AND
    # the MCP reply tool, via the send_dedup PreToolUse hook) funnels through the atomic
    # (channel+reply_to) dedup- so even if the live plugin session also answers an @mention
    # here, it can never double-reply. Parity in every channel, no chase needed.
    if not mentioned and siblings.addressed_to_sibling(content, SIBLING_IDS):
        return "standback"               # addressed to Codex/Jem, not Baxter: react-only, no reply
    return "handle"


async def _convo(message):
    """A few turns of channel context so the worker answers as a conversation turn, not a
    cold read. In-process gateway fetch- no extra REST auth."""
    try:
        msgs = [m async for m in message.channel.history(limit=12)]
        msgs.reverse()
        lines = []
        for r in msgs:
            who = "Baxter" if r.author.bot else (r.author.name or "?")
            lines.append(f"[{r.created_at:%H:%M}] {who}: "
                         + (r.content or "").replace("\n", " / ")[:300])
        return "\n".join(lines)
    except Exception:
        return ""


async def _eyes(message, mid):
    """👀 WORK-START receipt (the owner, 8th July clarification). His semantics: 👀 means 'on it
    now', NOT 'seen it'- so it fires the instant Baxter actually PICKS UP a message and begins
    working it (just before a reply worker is spawned), never on mere sight/poll and never
    on a defer-till-reset ack. The listener sees every channel, so reacting here delivers the
    work-start receipt server-wide. Idempotent (add_reaction no-ops if already there) and
    deduped via the shared .baxter_fast_reacted.json ledger so the #general fast-lane poll
    never double-hits the API for the same message."""
    try:
        ids = _load_ids(REACTED)
        if mid in ids:
            return
        await message.add_reaction("\U0001F440")            # 👀 seen (on it now)
        # ⚙️ ACTIVELY-WORKING cog is NO LONGER stamped here (the 10th-July phantom-cog audit):
        # this fires at work-START, before the reply worker has actually run a Claude turn, so a
        # spawn that failed or a worker that died left a cog with no session behind it. The reply
        # worker now owns its own cog- it stamps ⚙️ when its Claude turn begins and drops it when
        # the turn ends- so a cog only ever marks genuine, in-flight work.
        _add_ids(REACTED, [mid], REACTED_CAP)
    except Exception as e:
        _log(f"eyes react failed for {mid}: {e}")


@client.event
async def on_message(message):
    """Real-time dispatch. THE DE-DUP RULE (exactly one responder per message):
      - ignore bots (loop guard) and anyone who isn't the owner;
      - ignore anything from before startup (no history replay);
      - activity-log is one-way -> SKIP; a bare CoC command in coc-farm -> the CoC daemon owns it;
      - otherwise HANDLE- answer via the reply-worker lane, in EVERY channel (full parity).
    """
    try:
        if message.author.bot or message.author.id != OWNER_ID:
            return
        global START_TS
        if START_TS is not None and message.created_at < START_TS:
            return
        if OFF_FLAG.exists():                        # Baxter is /off- listener stays silent (slash /on + fast-lane text /on still wake it)
            return
        mid = str(message.id)
        cid = str(message.channel.id)
        content = message.content or ""
        if _already(mid):
            return
        mentioned = client.user in message.mentions   # direct @mention or a reply that pings us
        decision = _route(cid, content, mentioned)
        if decision == "handle" and not mentioned and message.reference and message.reference.message_id:
            # a Discord reply (not an @mention) to a sibling bot's OWN message is 'addressed to
            # the sibling' too, but carries no <@sibling> text for _route to see. Resolve the
            # referenced author once and stand back if it is Codex/Jem.
            try:
                ref = message.reference.resolved
                if not isinstance(ref, discord.Message):
                    ref = await message.channel.fetch_message(message.reference.message_id)
                if ref and str(ref.author.id) in SIBLING_IDS:
                    decision = "standback"
            except Exception as e:
                _log(f"listener: sibling reply-resolve failed for {mid}: {e}")
        if decision == "empty":
            # message_content is enabled app-side; an empty body here = attachment/embed-only
            # OR (if EVERY message is empty) the privileged intent isn't actually delivering.
            _log(f"listener: empty content for msg {mid} in {cid} "
                 f"(attachment-only, or message_content not delivering- check intent)")
            return
        if decision == "offon":
            return   # fast lane's pure-code pause toggle handles it- don't claim/interfere
        if decision == "archive":
            _log(f"listener: SKIP archive-channel msg {mid} in {cid} (log-only, never chatter)")
            return
        if decision == "coc-daemon":
            # a bare CoC command in coc-farm: the coc_discord daemon replies to it. Baxter
            # stays out so it doesn't double the daemon- no reply, no 👀 (the daemon owns it).
            _log(f"listener: DEFER coc-farm command msg {mid} in {cid} (coc_discord daemon owns it)")
            return
        if decision == "standback":
            # addressed to a sibling bot (Codex/Jem), not Baxter- stand back with the handsoff
            # reaction and stay silent. No eyes, no cog, no reply worker: it reads as 'seen, not
            # mine', and the reaper treats handsoff as 'no reply owed' so it is never surfaced as
            # stuck. add_reaction is idempotent, so a gateway redelivery just no-ops.
            #
            # AND WE CLAIM IT (11th July). This branch used to return BEFORE _claim, with a
            # comment calling the missing ledger write a feature. It was the bug: an unclaimed
            # message is an UNTAKEN message, and the fast-lane poll re-picked it 15s later and
            # answered it with a full Opus worker- the exact reply this branch exists to
            # suppress. Standing back is a decision about the message, so it is written down
            # like any other. The claim is idempotent (a locked set-merge), so a redelivery
            # costs nothing. The fast lane also carries the same check now, so a lost claim
            # degrades to a second stand-back, never to a reply.
            _claim(mid)
            try:
                await message.add_reaction("\U0001F91A")   # handsoff
            except Exception as e:
                _log(f"listener: standback react failed for {mid}: {e}")
            _log(f"listener: STANDBACK msg {mid} in {cid} (addressed to a sibling bot, not Baxter)")
            return
        # ---- HANDLE ----
        _claim(mid)
        # THE ACT, THEN THE ACK. A sizeable ask is written to the build queue here, in code,
        # before any branch below speaks: the wall ack, the floor ack, and the reply worker's
        # own "queued, sir" all describe an entry that exists by the time they run. This must
        # precede the floor check, not follow it- at 90% the floor ack holds everything till
        # reset, and an ask that was never written down is an ask the reset never brings back.
        # A placeholder is a local JSON write: no LLM, no network, nothing the floor is
        # protecting. `_big_gated()` is False at the floor, so nothing is acked from here.
        position, acked, big = _live_big_ask(cid, mid, content, bf._big_gated())
        if acked:
            return                                   # wall ack sent, no LLM burn; _live_big_ask logged it
        # governor (same 4-tier gate as the fast lane, reused from baxter_fast). NOTE: the
        # floor/big-gated branches are DEFERRALS (hold-till-reset acks), not work- so they get
        # NO 👀 receipt (👀 means 'on it now', and here we're explicitly NOT working it yet).
        if bf._floor_active():                       # 90%+ hard floor: zero-cost ack, no LLM
            _spawn_say(cid, mid, bf._floor_ack())
            _log(f"listener: FLOOR ack (no llm) to {mid}")
            return
        # ---- session continuation (the owner, 8th July). Resolve which Claude session this
        # turn runs in: a non-#general channel = the channel's persistent MASTER thread;
        # #general = per-task, unless it's a REPLY to a Baxter answer, in which case we
        # resume THAT answer's session. To resume a reply we resolve the ORIGINAL the owner
        # message: the owner replied to a Baxter msg, and that Baxter msg itself replied to
        # the owner's original- so referenced.reference.message_id is the key into the ledger.
        referenced_owner_mid = None
        reply_parent = ""              # PRIMARY context block: the message the owner actually replied to
        is_reply = bool(message.reference and message.reference.message_id)
        if is_reply:
            try:
                ref = message.reference.resolved
                if not isinstance(ref, discord.Message):
                    ref = await message.channel.fetch_message(message.reference.message_id)
                # pin the referenced message as primary context (the owner, 8th July- the worker
                # was answering off recent chatter instead of the message he replied to)
                reply_parent = _reply_parent_block(ref)
                if (ref and ref.author.id == client.user.id
                        and ref.reference and ref.reference.message_id):
                    referenced_owner_mid = str(ref.reference.message_id)
            except Exception as e:
                _log(f"listener: reply-chain resolve failed for {mid}: {e}")
        session, mode = lanes.plan_session(cid, is_reply=is_reply,
                                           referenced_owner_mid=referenced_owner_mid)
        if str(cid) == lanes.GENERAL_ID and mode == "create":
            # record so a future reply to THIS answer resumes this per-task session
            lanes.set_message_session(mid, session)
        convo = await _convo(message)
        await _eyes(message, mid)                    # work-start: about to actually work + reply
        # A big ask has had its placeholder written above, so the worker is told the slot
        # rather than asked to create one. A big ask whose write FAILED gets the blunt refusal
        # block ("you may not write 'queued'")- never a bare ack. An ordinary message gets no
        # block at all, leaving DEFLECT_RULE's --queue branch in force for genuinely new work
        # it decides to defer. Same three-way split the fast lane makes.
        qblock = rules.queued_block(position, content) if big else ""
        _spawn_reply_worker(content[:1500], cid, mid, convo, session, mode, reply_parent, qblock)
        _log(f"listener: reply worker spawned for {mid} in {cid} "
             f"(session {session[:8]} {mode}) ({content[:40]!r})")
    except Exception as e:
        _log(f"listener on_message failed: {e}")


@client.event
async def on_ready():
    global START_TS
    if START_TS is None:
        START_TS = discord.utils.utcnow()
    try:
        await tree.sync(guild=GUILD)
        _log(f"ready as {client.user}; slash synced + real-time listener live (seed "
             f"{START_TS:%Y-%m-%d %H:%M:%S} UTC)")
        print("baxter_slash ready + synced + listener live")
    except Exception as e:
        _log(f"sync failed: {e}")
        print(f"sync failed: {e}")


def _acquire_singleton():
    """Named-mutex singleton guard (the owner, 8th July- a duplicate slash bot racing the SAME
    interaction means one wins the 3s ack and the other's defer throws 'already acknowledged'
    silently, dropping the command). A kernel mutex is stale-proof: Windows releases it the
    instant the holding process dies, unlike a PID lockfile that can outlive a crash. Both
    bots run as the owner in one session, so a session-local (un-prefixed) name is right- no
    Global\\ privilege needed. Returns the handle to keep alive, or None if another instance
    already holds it (caller must exit so it can't race the ack)."""
    ERROR_ALREADY_EXISTS = 183
    try:
        k = ctypes.windll.kernel32
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        h = k.CreateMutexW(None, False, "baxter_slash_singleton_mutex")
        if k.GetLastError() == ERROR_ALREADY_EXISTS:
            return None
        return h
    except Exception as e:
        _log(f"singleton guard errored, continuing UNGUARDED: {type(e).__name__}: {e}")
        return "unguarded"


def selftest_ledger():
    """The listener's half of the shared-claim lock: a claim written by _claim() must SURVIVE a
    fast-lane process hammering the same ledger at the same moment.

    That is the exact race that burned a second Opus worker on msg 444444444444444401 (9th
    July): the listener claimed it, the fast lane's unlocked read-modify-write wrote back a
    stale snapshot that no longer contained the claim, and the poll re-picked it.

    Runs against TEMP ledgers, with `_log` stubbed. Every outward path stays untouched- no
    client, no gateway, no token ([[selftests-stub-every-outward-path]])."""
    import tempfile, shutil, subprocess
    g = globals()
    real = (g["LISTENER_HANDLED"], g["FAST_HANDLED"], g["_log"])
    tmp = Path(tempfile.mkdtemp(prefix="baxter_slash_ledger_"))
    fails = []

    def check(ok, name, detail=""):
        print(("  ok   " if ok else "  FAIL ") + name + (("- " + detail) if detail else ""))
        if not ok:
            fails.append(name)

    try:
        g["_log"] = lambda *a, **k: None            # never touch .baxter_slash.log
        g["LISTENER_HANDLED"] = tmp / "listener.json"
        g["FAST_HANDLED"] = tmp / "fast.json"
        utils = str(Path(__file__).resolve().parent)

        hammer = tmp / "hammer.py"
        hammer.write_text(
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import baxter_send_dedup as dedup\n"
            "for k in range(int(sys.argv[3])):\n"
            "    dedup.update_ids(sys.argv[2], ['fast-%d' % k], cap=200)\n", encoding="utf-8")

        ROUNDS = 150
        mid = "444444444444444401"
        p = subprocess.Popen([sys.executable, str(hammer), utils, str(g["FAST_HANDLED"]),
                              str(ROUNDS)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.05)                            # land the claim mid-hammer, not before it
        _claim(mid)
        p.wait(timeout=120)
        check(p.returncode == 0, "the concurrent fast-lane writer exited 0")

        own = dedup.read_ids(g["LISTENER_HANDLED"])
        shared = dedup.read_ids(g["FAST_HANDLED"])
        check(mid in own, "the claim landed in the listener's own ledger")
        check(mid in shared, "the claim SURVIVED in the shared ledger under a concurrent writer")
        check(shared.count(mid) == 1, "the claim appears exactly once", "count=%d" % shared.count(mid))
        lost = [k for k in range(ROUNDS) if ("fast-%d" % k) not in set(shared)]
        check(not lost, "the fast lane's %d concurrent ids all survived too" % ROUNDS,
              "" if not lost else "%d lost" % len(lost))
        check(_already(mid), "_already() now reports the message as handled")
    finally:
        g["LISTENER_HANDLED"], g["FAST_HANDLED"], g["_log"] = real
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("listener ledger selftest FAILED: %s" % ", ".join(fails))
        return 1
    print("listener ledger selftest OK: _claim() survives a concurrent fast-lane writer, "
          "and neither writer loses an id.")
    return 0


def selftest_standback():
    """Baxter stands back with the handsoff reaction (never a reply) when the owner addresses a sibling
    bot- and still answers when he addresses, or co-mentions, Baxter.

    Guards the 10th-July regression (Baxter talking over Codex). Asserts the pure _route decision
    for all four cases, then drives on_message headless to prove a standback stamps the handsoff
    reaction and spawns NO reply worker, while a handle DOES spawn. Every outward path is stubbed-
    no client, no gateway, no token ([[selftests-stub-every-outward-path]])."""
    import tempfile, shutil, asyncio
    g = globals()
    fails = []

    def check(ok, name, detail=""):
        print(("  ok   " if ok else "  FAIL ") + name + (("- " + detail) if detail else ""))
        if not ok:
            fails.append(name)

    HANDSOFF = "\U0001F91A"
    sibs = sorted(SIBLING_IDS)
    check(len(sibs) == 2, "two sibling ids load from .baxter_secrets.json", "got %d" % len(sibs))
    if len(sibs) < 1:
        print("standback selftest FAILED: no sibling ids to test against")
        return 1
    cid = "222222222222222202"                     # #general: not archive, not coc-farm

    # ---- 1) the pure _route decision, all four cases ----
    for s in sibs:
        check(_route(cid, f"<@{s}> run the exam", False) == "standback",
              "a lone sibling @mention -> standback", s)
        check(_route(cid, f"<@!{s}> run the exam", False) == "standback",
              "the <@!id> bang form -> standback", s)
    check(_route(cid, f"<@{sibs[0]}> <@333333333333333301> both of you", True) == "handle",
          "a sibling+Baxter co-mention (mentioned=True) -> handle")
    check(_route(cid, "just ambient chatter, no mentions", False) == "handle",
          "a no-mention message -> handle")

    # ---- 2) on_message drives the outward behaviour, fully stubbed ----
    tmp = Path(tempfile.mkdtemp(prefix="baxter_slash_standback_"))
    real = {k: g[k] for k in ("LISTENER_HANDLED", "FAST_HANDLED", "OFF_FLAG", "_log",
                              "client", "_spawn_reply_worker", "_live_big_ask", "_eyes", "_convo")}
    real_floor, real_big, real_plan = bf._floor_active, bf._big_gated, lanes.plan_session
    spawned = []

    class _U:
        def __init__(self, uid, bot=False):
            self.id = uid
            self.bot = bot

    class _Ch:
        def __init__(self, cid, ref_msg=None):
            self.id = cid
            self._ref_msg = ref_msg

        async def fetch_message(self, mid):
            return self._ref_msg

    class _Msg:
        def __init__(self, content, mentions, mid, cid, reference=None, ref_msg=None):
            self.content = content
            self.mentions = mentions
            self.id = mid
            self.channel = _Ch(cid, ref_msg)
            self.reference = reference
            self.author = _U(333333333333333301)      # OWNER_ID, not a bot
            self.created_at = None
            self.reactions = []

        async def add_reaction(self, e):
            self.reactions.append(e)

    try:
        g["LISTENER_HANDLED"] = tmp / "listener.json"
        g["FAST_HANDLED"] = tmp / "fast.json"
        g["OFF_FLAG"] = tmp / ".baxter_off"            # absent -> .exists() False
        g["_log"] = lambda *a, **k: None
        g["client"] = type("C", (), {"user": object()})()
        g["_spawn_reply_worker"] = lambda *a, **k: spawned.append(a)
        g["_live_big_ask"] = lambda *a, **k: (0, False, False)

        async def _noop_eyes(*a, **k):
            return None

        async def _noop_convo(*a, **k):
            return ""

        g["_eyes"] = _noop_eyes
        g["_convo"] = _noop_convo
        bf._floor_active = lambda: False
        bf._big_gated = lambda: False
        lanes.plan_session = lambda *a, **k: ("sess1234", "resume")

        # a) a lone sibling mention -> handsoff stamped, NO reply worker
        m1 = _Msg(f"<@{sibs[0]}> run the exam", [], "9001", cid)
        asyncio.run(on_message(m1))
        check(HANDSOFF in m1.reactions, "standback stamps the handsoff reaction")
        check(not spawned, "standback spawns NO reply worker")

        # b) a plain message to Baxter -> reply worker spawned, no handsoff
        spawned.clear()
        m2 = _Msg("what's the weather, sir?", [], "9002", cid)
        asyncio.run(on_message(m2))
        check(len(spawned) == 1, "a handle DOES spawn a reply worker", "spawns=%d" % len(spawned))
        check(HANDSOFF not in m2.reactions, "a handled message gets no handsoff reaction")

        # c) a Discord reply to a sibling's OWN message (no mention) -> standback
        spawned.clear()
        sib_msg = _Msg("Codex output here", [], "8000", cid)
        sib_msg.author = _U(int(sibs[0]))
        reference = type("Ref", (), {"resolved": None, "message_id": 8000})()
        m3 = _Msg("thanks, do it again", [], "9003", cid, reference=reference, ref_msg=sib_msg)
        asyncio.run(on_message(m3))
        check(HANDSOFF in m3.reactions, "a reply to a sibling's own message -> standback (handsoff)")
        check(not spawned, "the sibling-reply standback spawns NO reply worker")
    finally:
        for k, v in real.items():
            g[k] = v
        bf._floor_active, bf._big_gated, lanes.plan_session = real_floor, real_big, real_plan
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("standback selftest FAILED: %s" % ", ".join(fails))
        return 1
    print("standback selftest OK: Baxter stands back with the handsoff reaction for sibling-"
          "addressed messages and only spawns a reply worker for his own.")
    return 0


def _run_selftests():
    """Dispatch --selftest* flags EXPLICITLY, and refuse any flag we don't know.

    THE FALSE-GREEN THIS CLOSES (measured 9th July): an unrecognised selftest flag- a typo like
    `--selftest-coc-live` misspelt, or a gate written against a check that was never built- used
    to fall past the two `if` branches into _acquire_singleton(), which returns None because the
    resident bot holds the mutex, and exited 0. `--selftest-coc-TYPO` returned rc=0 having proven
    nothing. A verify gate is worthless if the way to pass it is to name a test that doesn't
    exist, so an unknown flag is exit 2 and it happens BEFORE the singleton guard."""
    reg = {"--selftest-panel": selftest_panel,
           "--selftest-coc": selftest_coc,
           "--selftest-coc-live": selftest_coc_live,
           "--selftest-ledger": selftest_ledger,
           "--selftest-standback": selftest_standback}
    flags = [a for a in sys.argv[1:] if a.startswith("--selftest")]
    unknown = [f for f in flags if f not in reg]
    if unknown:
        print(f"unknown selftest flag(s): {' '.join(unknown)}", file=sys.stderr)
        print(f"known: {' '.join(sorted(reg))}", file=sys.stderr)
        return 2
    for f in flags:
        rc = reg[f]()
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    # BEFORE the singleton and before client.run: a selftest must never contend with the live
    # bot for the mutex, and must never open a gateway connection.
    if _SELFTEST:
        sys.exit(_run_selftests())
    _SINGLETON = _acquire_singleton()   # keep the handle alive for the whole process
    if _SINGLETON is None:
        _log("baxter_slash ABORT- another instance already holds the singleton mutex "
             "(dup would race the 3s ack); exiting so exactly one bot owns the slash tree")
        sys.exit(0)
    _log("baxter_slash starting")
    client.run(TOKEN, log_handler=None)
