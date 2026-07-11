#!/usr/bin/env python3
"""
baxter_triage — Baxter's poll+dispatch step.

Called by baxter_watch.ps1 every ~60s (only while Obsidian is open). Cheap and
free: it just checks each source for NEW items. Only when something new appears
does it wake Claude (costs credits) to do the rich triage per BAXTER_TRIAGE.md.

Wire points (after you authenticate): fetch_new_emails(), fetch_new_discord().
Run `python baxter_triage.py --selftest` to prove the write path with a demo item.
"""
import json, sys, subprocess, urllib.request, imaplib, email, re, os, time, importlib
from email.header import decode_header
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baxter_modelguard as _mg   # ONE source of truth: model (never Fable) + MCP scope
# ONE source of truth for the rules a worker prompt carries. Imported at module scope, and
# NOT in a try/except: a build prompt that quietly loses TRUST BUT VERIFY because an import
# failed is the precise drift baxter_rules exists to make impossible. baxter_rules imports
# only baxter_read_channel and baxter_reminders, neither of which imports this module, so
# there is no cycle here. Its guard imports THIS module lazily, inside check().
import baxter_rules as _rules


def _claude(cls="heavy", mcp=None):
    """The argv prefix for every claude spawn in this module. Never build one by hand-
    a hardcoded ['claude', '--model', 'opus'] silently re-inherits all five global MCP
    servers, which is the spawn storm that starves Atul's interactive TUI.

    TWO AXES (9th July). The first argument is a WORK CLASS- what this job needs to be
    clever enough for ('file', 'prose', 'classify', 'build', ...)- and it alone picks the
    MODEL. `mcp` is an optional MCP-SCOPE override: WHICH SERVERS load, nothing else.

    They used to be one string, and that was the trap: inbox filing asked for lane
    'media' whenever its batch carried a link, so the presence of a URL silently chose
    the model too. Filing is class 'file' (Haiku) whether or not it needs a browser-
    `mcp=_batch_lane(items)` moves the servers, never the tier.
    """
    return ["claude"] + _mg.args(cls, mcp=mcp)


# A batch only needs the Playwright MCP when it actually carries something to open.
# Plain text dumps- the overwhelming majority, and the ones that burst 5-wide- launch
# no MCP server at all. Contract step 30 (fetch + classify a link/reel/attachment) is
# the sole reason a triage worker ever gets a browser.
_URL_RE = re.compile(r"https?://", re.I)


def _batch_lane(items):
    """'media' if this batch has a link or an attachment to open, else 'heavy'."""
    try:
        for it in items:
            if it.get("attachments") or it.get("attachment_count"):
                return "media"
        return "media" if _URL_RE.search(json.dumps(items, ensure_ascii=False)) else "heavy"
    except Exception:
        return "media"   # unreadable batch- assume it needs the browser, never lose a link


VAULT = Path(r"C:\Users\you\Documents\Baxter")
STATE = VAULT / ".baxter_state.json"
SECRETS = VAULT / ".baxter_secrets.json"   # local only; holds discord token etc.
INBOX = VAULT / "00-Inbox"
CONTRACT = VAULT / "BAXTER_TRIAGE.md"
HEARTBEAT = VAULT / ".baxter_heartbeat.txt"   # self-healing watcher reads this
OFF_FLAG = VAULT / ".baxter_off"              # master OFF switch (Atul's /off): soft-pause the
                                              # proactive machinery- briefs, pings, filing, auto-work.
                                              # The fast lane stays live so /on + his questions land.
CATCHUP = VAULT / ".baxter_catchup"           # one-shot: /on drops this so the first pass after a
                                              # pause summarises what landed while off (backfill).
ASK = VAULT / ".baxter_ask.txt"               # dashboard "Ask Baxter" box writes here
SENDERS = VAULT / ".baxter_senders.json"      # learned sender -> project memory map
PROJECTS_JSON = VAULT / ".baxter_projects.json"   # generated project+thread list for the dashboard
OPEN_QUEUE = VAULT / ".baxter_open.txt"           # dashboard "open project" requests land here
PROJMOVE = VAULT / ".baxter_projmove.txt"         # dashboard project tier-change requests (key|tier)
MINE_QUEUE = VAULT / ".baxter_mine.txt"           # "pull open tasks from this project's chats" requests (project keys)
CLAUDE_PINGS = VAULT / ".baxter_claude_activity.txt"  # convo_autotag hook pings: every message in every Claude session
_PYCACHE_PREFIX = VAULT / ".baxter_pycache"   # a bytecode cache no stale pyc can hide in
OPENER = r"C:\Users\you\Documents\Python Scripts\utils\baxter_open.ps1"

# Every child we spawn runs fully SILENT - stdin/stdout/stderr detached, no console
# window. A triage that inherited a console (e.g. orphaned from a dead terminal) must
# never let its children spray output onto Atul's screen.
_SILENT = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
_NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CONVOS = r"C:\Users\you\Documents\Python Scripts\utils\convos.py"
BAXTER_SAY = r"C:\Users\you\Documents\Python Scripts\utils\baxter_say.py"   # Baxter's Discord voice (Atul's server only)
USAGE_PY = r"C:\Users\you\Documents\Python Scripts\utils\baxter_usage.py"   # the meters + governor (5th-July build)

# usage governor (contract step 29): probe writes .baxter_usage.json; check() gates work
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import baxter_usage as _gov
except Exception:
    _gov = None

# overdue auto-roll (hard rule, 5th July 12:04): a past 📅 date becomes today's date
try:
    import baxter_roll as _roll
except Exception:
    _roll = None

# verify + troubleshoot loop (9th July, his overnight order): the gate a build's success
# must pass, the classifier a failure is triaged by, and the ledger a success is passed on
# through. Absent, lanes fall back to the old blind behaviour rather than dying.
try:
    import baxter_verify as _bv
except Exception:
    _bv = None

# outbound text hygiene (9th July). baxter_say.py cleans everything that leaves through ITS
# door; these senders bypass it entirely- _say's argv, notify's balloon, the briefing mirror.
# Defensive import for the same reason baxter_say's is: a broken module must degrade to raw
# text, never take the whole triage pass down with it.
try:
    import baxter_text as _text
except Exception:
    _text = None

CORRUPT_LOG = VAULT / ".baxter_corruption.log"


def _clean(msg):
    """Repair mojibake + strip the banned ' / ' break, on the way out of this module.

    An UNREPAIRABLE mojibake means a caller upstream already decoded Baxter's UTF-8 through
    cp1252 with errors='ignore' and destroyed bytes nothing can restore. Send it anyway-
    silence is worse- but record it, with the caller, so the corrupting capture site can be
    found and pinned."""
    if not _text or not msg:
        return msg
    try:
        if _text.is_lossy_mojibake(msg):
            with open(CORRUPT_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] UNREPAIRABLE mojibake from "
                        f"{os.environ.get('BAXTER_CALLER', 'baxter_triage')}: {msg[:200]!r}\n"
                        f"    -> a caller decoded UTF-8 as cp1252 with errors='ignore'; "
                        f"bytes are gone. Fix the capture site (encoding='utf-8').\n")
    except Exception:
        pass
    try:
        return _text.clean_outbound(msg)
    except Exception:
        return msg


def _snapshot_config(mod):
    """A module's config globals EXACTLY as the code on disk defined them, captured at import.
    `_reload_module` compares against this by IDENTITY to tell a value a CALLER injected (the
    selftest's temp LEDGER) from one the module merely re-created on reload. Only injected
    values are restored; everything else is whatever the code on disk now says."""
    return ({k: v for k, v in vars(mod).items()
             if k.isupper() and not k.startswith("_") and not callable(v)}
            if mod is not None else {})


def _reload_module(mod, pristine, label):
    """Re-read `mod` from DISK, preserving the config globals a caller injected into it.

    A resume-worker imports its graders once, at spawn, and then holds them for the whole
    build. Measured 9th July: a worker started 11:23:53, baxter_verify gained POSIX/bash
    routing at 11:46:48, and at 11:48:57 the gate still judged with the PRE-FIX module- the
    sealed exam went to cmd.exe, exited 1, and a correct build was marked FAILED. Every hub
    fix landing mid-build is invisible to every worker already in flight. So the gate re-reads
    its graders immediately before it judges.

    importlib.reload re-executes the module at scope, which resets globals a caller injected-
    baxter_verify's LEDGER above all, which baxter_triage's own selftest rebinds to a temp
    file so the gate cannot write the live ledger. Reset that and the selftest writes to the
    real ledger. So we snapshot the injected values and put them back. Functions are NOT
    restored: the whole point is that run_verify and run_extras come from disk.

    A broken module on disk (a hub file caught mid-save) raises here. We keep the cached
    module and carry on: judging with slightly stale code beats killing the lane.
    """
    if mod is None:
        return None
    try:
        injected = {k: v for k, v in vars(mod).items()
                    if k in pristine and v is not pristine[k]}
        importlib.reload(mod)
        for k, v in injected.items():
            setattr(mod, k, v)
        # Re-baseline the values nobody injected, so the NEXT reload still reads them off
        # disk rather than mistaking last reload's object for an injection.
        for k in list(pristine):
            if k not in injected and hasattr(mod, k):
                pristine[k] = getattr(mod, k)
    except Exception as e:
        log(f"could not reload {label} ({e})- judging with the cached module")
    return mod


_BV_PRISTINE = _snapshot_config(_bv)


def _reload_bv():
    """The verifier, re-read from disk right before it grades. See _reload_module."""
    return _reload_module(_bv, _BV_PRISTINE, "baxter_verify")

# the VERTICAL tier (9th July, his 00:11 "more levels of hierarchy"): a planner writes the
# acceptance test before the executor starts, so the worker being graded no longer sets its
# own paper. Absent, lanes plan nothing and the builder declares its own check as before-
# this tier is grease, never a gate.
try:
    import baxter_orch as _orch
except Exception:
    _orch = None

_ORCH_PRISTINE = _snapshot_config(_orch)


def _reload_orch():
    """The extra-checks runner, re-read from disk right before it grades.

    Same staleness as _reload_bv, one tier up: `_orch.run_extras` runs the builder's own
    added checks, and a fix to it that lands mid-build is invisible to every lane already in
    flight. Reloaded strictly AFTER _reload_bv- baxter_orch imports baxter_verify at module
    scope, so re-executing it must find the fresh verifier already in sys.modules.
    """
    return _reload_module(_orch, _ORCH_PRISTINE, "baxter_orch")

def _usage_ok(cls="routine"):
    """Governor gate. vital never blocks; unreadable meters fail OPEN.
    HARD layer first: the .baxter_stop flag is an instant, network-free veto that
    holds even when the OAuth meters are stale (the case that let a runaway burn a
    whole window). Only then does the soft curve check() run."""
    if not _gov:
        return True
    try:
        if cls != "vital":
            # 4-tier governor (6th July): project maps to the big band (stops at 70%),
            # routine to the routine band (runs the 70-80 small-ask band, stops at 80%).
            # vital is never gated here. blocked() normalises 'project' -> 'big'.
            b, why = _gov.blocked(cls)
            if b:
                log(f"HARD STOP ({cls}): {why}")
                return False
        return bool(_gov.check(cls).get("allowed", True))
    except Exception:
        return True

def maybe_usage():
    """Refresh the meters (self rate-limited to ~4 min) + fire any due alerts."""
    if not _gov:
        return
    try:
        _gov.probe()
    except Exception as e:
        log(f"usage probe failed: {e}")
PENDING_Q = VAULT / ".baxter_pending_q.json"      # clarifying questions Baxter has asked, awaiting Atul's answer

def load_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text(encoding="utf-8-sig"))
        except Exception: pass
    return {"seen_email": [], "discord_after": {}, "gmail_last_uid": 0, "last_briefing": "", "last_backup": "", "last_weekly": "", "last_reprio": "", "last_demote": "", "seen_photos": [], "last_projscan": "", "last_followup": "", "followed_up": []}

def save_state(s):
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")

def load_secrets():
    if SECRETS.exists():
        try: return json.loads(SECRETS.read_text(encoding="utf-8-sig"))
        except Exception: pass
    return {}

# ---- SOURCES (wire after auth) ----------------------------------------------
def _decode_hdr(s):
    if not s:
        return ""
    out = ""
    for txt, enc in decode_header(s):
        if isinstance(txt, bytes):
            try: out += txt.decode(enc or "utf-8", "replace")
            except Exception: out += txt.decode("utf-8", "replace")
        else: out += txt
    return out

def _email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                try: return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception: pass
        return ""
    try: return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception: return ""

def _food_deal(item):
    """Uber Eats / Deliveroo RESTAURANT promos: instant Discord ping + vault log.
    Atul explicitly wants these (3rd July)- restaurants only, never grocery/shop offers."""
    frm = (item.get("from") or "").lower()
    if not re.search(r"uber\s*eats|@uber\.|deliveroo", frm):
        return False
    subj = item.get("subject") or ""
    text = (subj + " " + (item.get("body") or "")[:1500]).lower()
    if not re.search(r"% off|percent off|discount|promo|voucher|offer|save £|£\d+ off"
                     r"|free delivery|use code|2 for 1|bogof", text):
        return False
    # restaurant-only: skip promos whose SUBJECT is clearly grocery/shop-focused
    if re.search(r"grocer|supermarket|tesco|sainsbury|co-?op|asda|morrisons|aldi|lidl|boots"
                 r"|essentials|convenience|pharmacy|alcohol|off licence", subj.lower()):
        return False
    plat = "Deliveroo" if "deliveroo" in frm else "Uber Eats"
    # Live #deals board: Baxter maintains one edited-in-place board message + pings there
    # on a genuinely new deal, and prunes on the stated end date. The board is the single
    # source of truth; Food deals.md's Active list is generated from it. (Board build, 6 Jul.)
    deal_id = None
    try:
        import baxter_deals as _bd
        st = _bd._load(); tok = _bd._secrets()["discord_bot_token"]
        end = _bd.parse_end_date(subj)
        deal, is_new = _bd.add_deal(st, plat, subj.strip(), end_date=end, src=item.get("id"))
        deal_id = deal["id"]
        _bd.publish(st, tok, ping_new=(f"{plat}- {subj.strip()}" if is_new else None))
    except Exception as e:
        log(f"deals board update failed: {e}")
    item["note"] = (f"RESTAURANT FOOD DEAL ({plat}) - already on the live #deals board"
                    + (f" as `{deal_id}`" if deal_id else "") + " and Atul was pinged there; do NOT ping again. "
                    "Read the email body for the exact promo code and STATED end date, then enrich the board "
                    f"deal: python \"C:\\Users\\you\\Documents\\Python Scripts\\utils\\baxter_deals.py\" --update {deal_id or '<id>'} "
                    "[--code <code>] [--end YYYY-MM-DD] [--offer <short offer>]. That republishes the board and "
                    "regenerates Food deals.md- do NOT hand-edit Food deals.md's Active list (it's generated between "
                    "the DEALS markers). Prune on the STATED end date, not the send date (the 6th-July Burger King fix). "
                    "Do NOT create any '- [ ]' reminder tasks for deals. If the body reveals it is actually grocery/"
                    "shop-only, remove it (drop the deal from .baxter_deals.json and run --sync). No #attention, no inbox note.")
    return True

def _junk_senders():
    """Senders Atul unsubscribed from in the 4th-July deep-clean- new mail from them
    is auto-trashed at fetch time (tell Baxter to whitelist anyone wrongly caught)."""
    try:
        return set(json.loads((VAULT / ".baxter_junk_senders.json")
                              .read_text(encoding="utf-8-sig")).get("senders", []))
    except Exception:
        return set()

def fetch_new_emails(state):
    """Read NEW Gmail via IMAP + app passwords (no OAuth/Cloud project needed).
    Multi-account: .baxter_secrets.json holds
        gmail_accounts: [ {"address": "...", "app_password": "...", "always_attention": true?}, ... ]
    (legacy single gmail_address + gmail_app_password still works).
    Per-account last-UID tracking; never marks mail as read."""
    sec = load_secrets()
    accounts = list(sec.get("gmail_accounts") or [])
    if sec.get("gmail_address") and sec.get("gmail_app_password"):
        accounts.append({"address": sec["gmail_address"], "app_password": sec["gmail_app_password"]})
    if not accounts:
        return []
    uid_map = state.setdefault("gmail_last_uid_map", {})
    junk = _junk_senders()
    items = []
    for acct in accounts:
        addr = acct.get("address"); pw = acct.get("app_password")
        if not addr or not pw:
            continue
        last_uid = int(uid_map.get(addr, 0) or 0)
        # host defaults to Gmail; a non-Gmail account (e.g. UCL @ucl.ac.uk on
        # Office 365) just carries its own imap_host in .baxter_secrets.json.
        host = acct.get("imap_host", "imap.gmail.com")
        is_gmail = "gmail" in host
        try:
            M = imaplib.IMAP4_SSL(host)
            M.login(addr, pw)
            M.select("INBOX")
            if last_uid:
                typ, data = M.uid("search", None, f"UID {last_uid + 1}:*")
            else:
                typ, data = M.uid("search", None, "UNSEEN")   # first run: only unread, avoid a flood
            uids = data[0].split() if (data and data[0]) else []
            for uid in uids[-30:]:                              # cap per cycle
                uidn = int(uid)
                if uidn <= last_uid:
                    continue
                typ, md = M.uid("fetch", uid, "(RFC822)")
                if not md or not md[0]:
                    continue
                msg = email.message_from_bytes(md[0][1])
                fm = re.search(r"<([^>]+)>", msg.get("From") or "")
                fm = (fm.group(1) if fm else (msg.get("From") or "")).strip().lower()
                if fm and fm in junk:
                    try:
                        M.uid("store", uid, "+X-GM-LABELS", "\\Trash") if is_gmail \
                            else M.uid("store", uid, "+FLAGS", "\\Deleted")
                    except Exception: pass
                    uid_map[addr] = max(int(uid_map.get(addr, 0) or 0), uidn)
                    log(f"auto-trashed junk from {fm} ({addr})")
                    continue
                item = {
                    "id": f"em-{addr.split('@')[0]}-{uidn}", "source": "email",
                    "account": addr,
                    "from": _decode_hdr(msg.get("From")),
                    "subject": _decode_hdr(msg.get("Subject")),
                    "received": msg.get("Date", ""),
                    "body": _email_body(msg)[:4000],
                }
                if acct.get("always_attention"):
                    item["note"] = ("This arrived on Atul's rarely-used but HIGH-IMPORTANCE account "
                                    f"({addr}) - anything landing here is significant. Flag #attention.")
                else:
                    _food_deal(item)   # Uber Eats/Deliveroo restaurant promos: instant ping + log
                items.append(item)
                uid_map[addr] = max(int(uid_map.get(addr, 0) or 0), uidn)
            # ---- SENT mail: poll Atul's own outgoing so Baxter can close loops ----
            # (mark tasks done when he's emailed the thing, mark drafts sent, spot new
            #  awaiting-replies). First encounter just records the cursor - no flood.
            try:
                sent_map = state.setdefault("gmail_sent_uid_map", {})
                sent_folder = acct.get("sent_folder", '"[Gmail]/Sent Mail"' if is_gmail else "Sent")
                typ, _ = M.select(sent_folder, readonly=True)
                if typ == "OK":
                    if addr not in sent_map:
                        typ, sd = M.status('"[Gmail]/Sent Mail"', "(UIDNEXT)")
                        m2 = re.search(rb"UIDNEXT (\d+)", sd[0]) if sd and sd[0] else None
                        sent_map[addr] = (int(m2.group(1)) - 1) if m2 else 0
                    else:
                        last_sent = int(sent_map.get(addr, 0) or 0)
                        typ, sdata = M.uid("search", None, f"UID {last_sent + 1}:*")
                        suids = sdata[0].split() if (sdata and sdata[0]) else []
                        for uid in suids[-20:]:
                            uidn = int(uid)
                            if uidn <= last_sent:
                                continue
                            typ, md = M.uid("fetch", uid, "(BODY.PEEK[])")
                            if not md or not md[0]:
                                continue
                            msg = email.message_from_bytes(md[0][1])
                            items.append({
                                "id": f"sent-{addr.split('@')[0]}-{uidn}", "source": "email-sent",
                                "account": addr,
                                "from": addr, "to": _decode_hdr(msg.get("To")),
                                "subject": _decode_hdr(msg.get("Subject")),
                                "received": msg.get("Date", ""),
                                "body": _email_body(msg)[:2500],
                                "note": ("This is an email ATUL HIMSELF SENT - evidence of completed action. "
                                         "Close matching open tasks/drafts/awaiting items per the contract; "
                                         "never create a to-do from it (except a new awaiting-reply if he asked "
                                         "someone for something)."),
                            })
                            sent_map[addr] = max(int(sent_map.get(addr, 0) or 0), uidn)
            except Exception as e:
                log(f"sent-mail poll failed ({addr}): {e}")
            M.logout()
        except Exception as e:
            print(f"gmail fetch failed ({addr}): {e}")
            log(f"gmail fetch failed ({addr}): {e}")
    return items

def _fast_handled():
    """message ids the fast lane already replied to (so triage never double-replies)."""
    try:
        return set(json.loads((VAULT / ".baxter_fast_handled.json").read_text(encoding="utf-8-sig")).get("ids", []))
    except Exception:
        return set()

def fetch_new_discord(state):
    """Read NEW messages from your watched Discord channels via a real BOT token
    (official API, ToS-clean -- NOT a self-bot). Needs in .baxter_secrets.json:
        discord_bot_token, discord_channels[], discord_only_user_id (optional).
    Enable 'Message Content Intent' on the bot or content comes back empty."""
    sec = load_secrets()
    token = sec.get("discord_bot_token")
    channels = sec.get("discord_channels") or []
    only_user = sec.get("discord_only_user_id")
    if not token or not channels:
        return []
    after_map = state.setdefault("discord_after", {})
    items = []
    for ch in channels:
        ch = str(ch)
        ch_items = []
        url = f"https://discord.com/api/v10/channels/{ch}/messages?limit=50"
        if after_map.get(ch):
            url += f"&after={after_map[ch]}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://baxter.local, 1.0)",  # Discord/Cloudflare 403s without this
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                msgs = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"discord fetch failed (ch {ch}): {e}")
            continue
        for m in reversed(msgs):
            after_map[ch] = m["id"]
            if only_user and str((m.get("author") or {}).get("id")) != str(only_user):
                continue
            content = m.get("content", "")
            if not content:
                continue
            item = {
                "id": f"dc-{m['id']}", "source": "discord",
                "channel": ch, "message_id": m["id"],
                "from": (m.get("author") or {}).get("username", "you"),
                "subject": "discord dump", "received": m.get("timestamp", ""),
                "body": content,
            }
            _RESPONSIVE = re.compile(
                r"\?|^(what|whats|what's|when|where|who|why|how|is|are|am|do|does|did|can|could|should|status|update)"
                r"|^(done|snooze|cut|remove|delete|move|rename|roll|pause|resume|undo|revert|cancel)", re.I)
            if _RESPONSIVE.search(content) or str(m["id"]) in _fast_handled():
                item["note"] = ("The FAST LANE answers this kind of message (question/quick command) - "
                                "do NOT reply to it yourself under any circumstances. Only do durable "
                                "work (file/task/draft) if it contains new work beyond the reply; "
                                "otherwise skip silently.")
            elif "333333333333333301" in content or str(((m.get("referenced_message") or {}).get("author") or {}).get("id")) == "333333333333333301":
                item["note"] = ("Atul @mentioned Baxter / replied to Baxter here - the LIVE channel "
                                "session is answering him in real time. Do NOT reply. File durable "
                                "work only if the message contains any; otherwise skip silently.")
            ch_items.append(item)
        # chronology fix (Atul, 4th July): a dump is a TURN in a conversation, not a
        # standalone note. Attach the channel's last messages (BOTH sides, oldest-first)
        # so the worker reads each item in thread order - the cursor fetch above only
        # returns NEW messages, which strips Baxter's replies and everything before.
        if ch_items:
            try:
                creq = urllib.request.Request(
                    f"https://discord.com/api/v10/channels/{ch}/messages?limit=15",
                    headers={
                        "Authorization": f"Bot {token}",
                        "User-Agent": "DiscordBot (https://baxter.local, 1.0)",
                    })
                with urllib.request.urlopen(creq, timeout=20) as r:
                    recent = json.loads(r.read().decode("utf-8"))
                lines = []
                for m in reversed(recent):
                    a = m.get("author") or {}
                    who = "Baxter" if a.get("bot") else a.get("username", "?")
                    txt = (m.get("content") or "").replace("\n", " / ")[:400]
                    lines.append(f"[{(m.get('timestamp') or '')[11:16]}] {who}: {txt}")
                ctx = ("Last channel messages, oldest-first, both sides (Baxter = you). "
                       "Read each item as a turn in this conversation - fragments and "
                       "pronouns bind to the turns just before it:\n" + "\n".join(lines))
                for it in ch_items:
                    it["thread_context"] = ctx
            except Exception as e:
                print(f"discord context fetch failed (ch {ch}): {e}")
        items.extend(ch_items)
    return items

# ---- WhatsApp feed (bridge.mjs appends JSONL; read-only linked device) ----
WA_FEED = VAULT / ".baxter_whatsapp_feed.jsonl"

def fetch_new_whatsapp(state):
    """Consume new lines from the WhatsApp bridge feed. One item PER CHAT per run
    (messages coalesced oldest-first) so a burst reads as one conversation turn.
    Cursor = byte offset into the feed file."""
    if not WA_FEED.exists():
        return []
    offset = int(state.get("wa_offset", 0))
    try:
        size = WA_FEED.stat().st_size
        if size < offset:          # feed was rotated/truncated- start over
            offset = 0
        if size == offset:
            return []
        with WA_FEED.open("r", encoding="utf-8") as f:
            f.seek(offset)
            chunk = f.read()
            state["wa_offset"] = f.tell()
    except Exception as e:
        print(f"whatsapp feed read failed: {e}")
        return []
    by_chat = {}
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        by_chat.setdefault(rec.get("chat", "?"), []).append(rec)
    items = []
    stamp = datetime.now().strftime("%H%M%S")
    for n, (chat, recs) in enumerate(by_chat.items()):
        name = recs[-1].get("chatName") or chat
        lines = []
        for r in recs:
            t = (r.get("ts") or "")[11:16]
            lines.append(f"[{t}] {r.get('sender','?')}: {r.get('text','')}")
        items.append({
            "id": f"wa-{stamp}-{n}", "source": "whatsapp",
            "from": name,
            "subject": f"WhatsApp- {name}" + (" (group)" if recs[-1].get("isGroup") else ""),
            "received": recs[-1].get("ts", ""),
            "body": "\n".join(lines),
            "note": ("WHATSAPP INGESTION- read-only. Most WhatsApp traffic is social "
                     "chatter: file NOTHING for it, skip silently (no inbox note, no "
                     "daily-log line). Only act when a message carries a real plan, "
                     "task, date, money matter or something Atul must see- then triage "
                     "per the contract. Lines from 'Atul' are his OWN sent messages- "
                     "commitments he made are tasks ('I'll send it tomorrow' -> task). "
                     "NEVER draft or send a WhatsApp reply- no outward action exists "
                     "on this source."),
        })
    return items

# ---- dashboard quick-add queue (the ➕ box writes lines here; Baxter triages them) ----
QUEUE = VAULT / ".baxter_queue.txt"
# ---- live Claude-activity channel (fed by the convo_autotag hook on every message) ----
def fetch_claude_activity(state):
    """New Claude chats announce immediately; messages in ongoing chats buffer and
    flush once the session has been QUIET for 5 min (assess finished thoughts, not
    keystrokes). The hook already filters out Baxter's own vault-cwd runs."""
    pings = []
    if CLAUDE_PINGS.exists():
        try:
            for ln in CLAUDE_PINGS.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln:
                    try: pings.append(json.loads(ln))
                    except Exception: pass
            CLAUDE_PINGS.write_text("", encoding="utf-8")
        except Exception:
            pass
    buf = state.setdefault("claude_ping_buf", {})
    for p in pings:
        sid = p.get("sid")
        if not sid:
            continue
        b = buf.setdefault(sid, {"snippets": [], "section": "misc"})
        b["section"] = p.get("section") or b["section"]
        b["cwd"] = p.get("cwd", "")
        b["last_ts"] = p.get("ts") or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if p.get("prompt"):
            b["snippets"] = (b["snippets"] + [p["prompt"]])[-4:]
    items = []
    if "known_sids" not in state:
        # first ever run: seed with every existing transcript so old sessions
        # don't all get announced as "new Claude chats"
        try:
            state["known_sids"] = sorted({p.stem for p in (Path.home() / ".claude" / "projects").glob("*/*.jsonl")})
        except Exception:
            state["known_sids"] = []
    known = set(state.get("known_sids", []))
    now = datetime.now()
    for sid, b in list(buf.items()):
        if sid not in known:
            known.add(sid)
            items.append({"id": f"cc-new-{sid[:8]}", "source": "claude-chat",
                          "from": "Atul", "subject": f"NEW Claude chat ({b['section']})",
                          "received": b.get("last_ts", ""),
                          "body": (f"Atul just started a new Claude conversation "
                                   f"(project guess: {b['section']}, folder: {b.get('cwd','?')}). "
                                   f"Opening message: {b['snippets'][-1] if b['snippets'] else '(none)'}")})
            b["snippets"] = []          # announced; later messages flush on quiet
            continue
        try:
            quiet = (now - datetime.fromisoformat(b["last_ts"])).total_seconds() > 300
        except Exception:
            quiet = True
        if quiet:
            if b.get("snippets"):
                joined = "\n- ".join(b["snippets"])
                items.append({"id": f"cc-act-{sid[:8]}-{now.strftime('%H%M')}", "source": "claude-chat",
                              "from": "Atul", "subject": f"Claude chat activity ({b['section']})",
                              "received": b.get("last_ts", ""),
                              "body": (f"Recent messages Atul sent in an ongoing Claude conversation "
                                       f"(project: {b['section']}). Judge if anything here is a NEW workable "
                                       f"item for the vault; ignore pure working-chatter with that Claude:\n- {joined}")})
            del buf[sid]
    state["known_sids"] = sorted(known)[-500:]
    return items

def fetch_new_queue(state):
    if not QUEUE.exists():
        return []
    try:
        lines = [l.strip() for l in QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []
    if not lines:
        return []
    try:
        QUEUE.write_text("", encoding="utf-8")   # consume
    except Exception:
        pass
    stamp = datetime.now().strftime("%H%M%S")
    return [{"id": f"q-{stamp}-{i}", "source": "quickadd", "from": "Atul",
             "subject": "quick add", "received": "", "body": t} for i, t in enumerate(lines)]

# ---- photo drop folder (drop a screenshot/photo; Baxter OCRs + triages it) ----
PHOTOS = VAULT / "60-Photos"
def fetch_new_photos(state):
    """Any new image dropped in 60-Photos gets read (OCR/vision) + triaged.
    Claude's Read tool views the image; we just point it at the path."""
    if not PHOTOS.exists():
        return []
    seen = set(state.get("seen_photos", []))
    new = []
    for p in PHOTOS.glob("*"):
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif") and p.name not in seen:
            new.append(p)
    if not new:
        return []
    for p in new:
        seen.add(p.name)
    state["seen_photos"] = sorted(seen)
    return new

def triage_photos(paths):
    if not _usage_ok("routine"):
        log("governor: photo triage held")
        return
    listing = "\n".join(f"- {p}" for p in paths)
    prompt = (
        f"You are Baxter. Read {CONTRACT} and follow it exactly. Atul dropped {len(paths)} "
        f"image(s) into 60-Photos. For EACH image, use your Read tool to view it, extract any "
        f"text/dates/amounts/action items (receipts, screenshots, whiteboards, posters), then "
        f"triage per the contract (inbox note + tasks + daily log). NEVER send anything.\n\nIMAGES:\n{listing}"
    )
    try:
        _touch_lock()
        # class 'classify'- still Opus (plan step 3 widens only once filing is proven clean).
        # No browser: these are local images, read off disk with the Read tool.
        subprocess.run(_claude("classify") + ["-p", prompt], cwd=str(VAULT), timeout=600, **_SILENT)
        log(f"triaged {len(paths)} photo(s)")
        notify("🎩 Baxter", f"Read {len(paths)} photo(s) - filed.")
    except Exception as e:
        log(f"photo triage failed: {e}")

# ---- "Talk to Baxter" command channel (the dashboard chatbox; do ANYTHING) ----
CHAT_QUEUE = VAULT / ".baxter_chat_queue.txt"
CHAT_LOG = VAULT / "Baxter-Chat.md"
def fetch_new_chat():
    if not CHAT_QUEUE.exists():
        return []
    try:
        msgs = [l.strip() for l in CHAT_QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []
    if not msgs:
        return []
    try:
        CHAT_QUEUE.write_text("", encoding="utf-8")   # consume
    except Exception:
        pass
    return msgs

def run_baxter_chat(messages):
    """General-purpose agent: whatever Atul typed in the dashboard chatbox, do it
    in the vault. Create/rephrase/cut/reschedule tasks, answer, draft, reorganise.
    Replies are appended to Baxter-Chat.md so the dashboard thread shows them."""
    for msg in messages:
        now = datetime.now().strftime("%H:%M")
        prompt = (
            f"You are Baxter, Atul's personal assistant, operating directly inside his Obsidian vault at {VAULT}. "
            f"Read {CONTRACT} for conventions (COMMAND vs CAPTURE, tags, hyphen style, never send outward). "
            f"Atul just typed this into his Baxter command box:\n\n\"{msg}\"\n\n"
            f"DO EXACTLY WHAT HE ASKED — it can be ANYTHING: create / rephrase / re-tag / cut (reversible) / "
            f"reprioritise / reschedule a task (fuzzy-match it across 00-Inbox and project notes), answer a "
            f"question about his vault or projects, draft a message into 40-Drafts, research into 50-Research, "
            f"reorganise or summarise. If his message is a CORRECTION or STANDING PREFERENCE ('you got X wrong', "
            f"'always do Y', 'never do Z', 'I don't want...') — fix the immediate thing AND record the rule "
            f"permanently: append/amend it in {CONTRACT} (Hard rules or the relevant step) so it applies to every "
            f"future triage, and note in your reply that it's now a standing rule. Also update Subscriptions.md "
            f"when he reports cancelling/keeping a subscription. **his Discord handle IS Atul** — never third-person him. "
            f"When finished, APPEND your reply to {CHAT_LOG} (create the file if missing) under a heading line "
            f"exactly like '## 🎩 Baxter - {now}'. BE BRIEF — 1-2 short lines by default, like a sharp PA "
            f"confirming it's done ('Renamed it.' / 'Killed that one - it's in History.' / 'Drafted - in 40-Drafts.'). "
            f"Only use grouped bullets if he explicitly asked to SEE a list. No #hashtags — plain project names "
            f"(Zeo, the internship project). Hyphen style. NEVER send anything outward."
        )
        try:
            env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
            subprocess.Popen(_claude("prose") + ["-p", prompt], cwd=str(VAULT), env=env,
                             creationflags=_NO_WIN, **_SILENT)
            log(f"chat dispatched (parallel): {msg[:60]}")
        except Exception as e:
            log(f"chat failed: {e}")
            try:
                with open(CHAT_LOG, "a", encoding="utf-8") as f:
                    f.write(f"\n## 🎩 Baxter - {now}\n(couldn't process that just now - {e})\n")
            except Exception:
                pass
    notify("🎩 Baxter", "Replied in your dashboard.")

# ---- "Ask Baxter" Q&A box (dashboard writes a question; Baxter answers into a note) ----
def fetch_new_questions():
    if not ASK.exists():
        return []
    try:
        qs = [l.strip() for l in ASK.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []
    if not qs:
        return []
    try:
        ASK.write_text("", encoding="utf-8")   # consume
    except Exception:
        pass
    return qs

def answer_questions(questions):
    """Wake Claude to answer natural-language questions ABOUT the vault.
    (Atul-direct = vital class- only the probe's fail-safe would ever hold it.)
    Read-only intent: search the vault and write a short answer note; never send."""
    qjoined = "\n".join(f"- {q}" for q in questions)
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        f"You are Baxter. Atul asked the following question(s) about his own vault at {VAULT}.\n"
        f"Search the vault (tasks, 00-Inbox, 20-Projects, 30-Daily, 40-Drafts) and ANSWER them.\n"
        f"Write the answer to {VAULT}/00-Inbox/{today} {datetime.now().strftime('%H%M')} - Ask Baxter.md "
        f"with frontmatter (source: ask, project: baxter, needs_attention: false, confidence: high|medium|low) "
        f"and a tight, scannable answer (bullets, link relevant notes with [[wikilinks]]). "
        f"Be concrete — cite the tasks/notes you found. Plain language — NO #hashtags. "
        f"Do NOT send anything.\n\nQUESTION(S):\n{qjoined}"
    )
    try:
        _touch_lock()
        subprocess.run(_claude("prose") + ["-p", prompt], cwd=str(VAULT), timeout=600, **_SILENT)
        log(f"answered {len(questions)} question(s)")
        notify("🎩 Baxter", "Answered your question — check the Inbox.")
    except Exception as e:
        log(f"ask-baxter failed: {e}")

# ---- learned sender -> project memory (so repeat senders classify consistently) ----
def load_senders():
    if SENDERS.exists():
        try: return json.loads(SENDERS.read_text(encoding="utf-8-sig"))
        except Exception: pass
    return {}

def senders_hint():
    m = load_senders()
    if not m:
        return ""
    pairs = ", ".join(f"{k} -> {v}" for k, v in list(m.items())[:40])
    return ("\n\nKNOWN SENDERS (classify these consistently; update "
            ".baxter_senders.json when you learn a new sender->project): " + pairs)

# ---- desktop notification (fire-and-forget) ----
# Quiet hours (22:00-07:00) suppress only ROUTINE balloons- no point popping a PC
# balloon overnight for a filing. A MANDATED ping (usage band-cross / urgent /
# follow-up nudge / resume) passes mandatory=True and fires any hour- the quiet-hours
# rule was scrapped for those (Atul, 6th July 22:06). Phone pushes go via _say/baxter_say,
# which has no night gate; this is the local desktop balloon only.
NOTIFY = Path(r"C:\Users\you\Documents\Python Scripts\utils\baxter_notify.ps1")
def notify(title, msg, mandatory=False):
    h = datetime.now().hour
    if not mandatory and (h >= 22 or h < 7):   # quiet hours: routine balloons file silently
        return
    try:
        subprocess.Popen(["powershell", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
                          "-File", str(NOTIFY), "-Title", title, "-Message", _clean(msg)],
                         creationflags=_NO_WIN, **_SILENT)
    except Exception:
        pass

# ---- logging ----
LOG = VAULT / ".baxter.log"
def log(msg):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

# ---- daily morning briefing (once per day) ----
def maybe_briefing(state):
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_briefing") == today:
        return
    if datetime.now().hour < 5:   # don't brief in the dead of night
        return
    if not _usage_ok("routine"):
        return   # retries next cycle- last_briefing stays unset
    prompt = (
        f"You are Baxter. Write today's morning briefing to {VAULT}/30-Daily/{today}.md "
        f"(create/prepend). Scan the vault: open tasks (esp. #attention), items in 00-Inbox, "
        f"drafts in 40-Drafts awaiting approval, anything overdue or due today. Sections:\n"
        f"1. One-line greeting.\n"
        f"2. **Top 3 today** — the most urgent things, each with its project.\n"
        f"3. **⏱ Day plan** — a realistic time-blocked suggestion for today's tasks "
        f"(morning/afternoon/evening blocks); don't over-pack it.\n"
        f"4. **🔮 Baxter's radar** — ONE proactive flag: a project going quiet, a deadline "
        f"sneaking up, a draft aging unsent, or a clash worth pre-empting. Be specific and brief.\n"
        f"5. **🍔 Deals on** — read Food deals.md and outline each ACTIVE restaurant deal on one "
        f"line (platform - offer - code - expires). Prune clearly expired lines from Food deals.md "
        f"while you're there. OMIT this section entirely if there are none.\n"
        f"6. Counts: inbox / drafts / overdue.\n"
        f"Short, scannable, no fluff. Plain language — NO #hashtags (say 'Zeo', not '#zeo'). Do NOT send anything."
    )
    try:
        _touch_lock()
        subprocess.run(_claude("prose") + ["-p", prompt], cwd=str(VAULT), timeout=300, **_SILENT)
        state["last_briefing"] = today
        log(f"briefing generated for {today}")
        mirror_briefing_to_discord(today)
    except Exception as e:
        log(f"briefing failed: {e}")

def mirror_briefing_to_discord(today):
    """OPT-IN: post today's briefing to Atul in the PD SERVER CHANNEL with an @mention (NOT a DM —
    Atul 2026-07-03: 'only communicate via the PD server, ping me'). OFF unless
    discord_dm_briefing:true in .baxter_secrets.json. The ONE sanctioned outward action."""
    sec = load_secrets()
    if not sec.get("discord_dm_briefing"):
        return
    token = sec.get("discord_bot_token")
    uid = sec.get("discord_only_user_id")
    channel = sec.get("discord_briefing_channel") or (sec.get("discord_channels") or [None])[0]
    if not token or not uid or not channel:
        return
    path = VAULT / "30-Daily" / f"{today}.md"
    if not path.exists():
        return
    try:
        # Clean the BRIEFING BODY ALONE, before it meets the header. repair_mojibake encodes
        # the whole string through cp1252 strict; the top hat below is undefined there, so a
        # clean of the composed message raises inside repair, no-ops, and the mangled body
        # ships. Clean first, interpolate second, and only then trim.
        text = _clean(path.read_text(encoding="utf-8"))[:1800]
        msg_req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel}/messages",
            data=json.dumps({"content": f"<@{uid}> 🎩 **Baxter briefing — {today}**\n{text}",
                             "allowed_mentions": {"users": [str(uid)]}}).encode("utf-8"),
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (https://baxter.local, 1.0)"}, method="POST")
        urllib.request.urlopen(msg_req, timeout=20)
        log("briefing posted to Discord channel (mention)")
    except Exception as e:
        log(f"discord briefing post failed: {e}")

# ---- weekly review (Sundays) ----
def maybe_weekly(state):
    now = datetime.now()
    week_id = now.strftime("%Y-W%U")
    if state.get("last_weekly") == week_id or now.weekday() != 6:
        return
    if not _usage_ok("routine"):
        return
    prompt = (
        f"You are Baxter. Write a WEEKLY REVIEW to {VAULT}/30-Daily/Weekly-{week_id}.md. "
        f"Scan the vault: tasks completed in the last 7 days (wins), still-open tasks per "
        f"project, anything stale (open >2 weeks), and what's due next week. Tight + motivating. "
        f"Sections: 🏆 Wins, 🔴 Still open (by project), ⏳ Going stale, 📅 Next week, "
        f"and an 💡 INSIGHT line: ONE pattern you noticed this week (which project got neglected, "
        f"what kept slipping, where momentum is) + one concrete suggestion. No fluff. "
        f"Plain language — NO #hashtags (say 'Zeo', not '#zeo'). Never send anything."
    )
    try:
        _touch_lock()
        subprocess.run(_claude("prose") + ["-p", prompt], cwd=str(VAULT), timeout=300, **_SILENT)
        state["last_weekly"] = week_id
        log(f"weekly review generated {week_id}")
    except Exception as e:
        log(f"weekly review failed: {e}")

# ---- daily auto-reprioritise (Baxter's agency; respects your 7-day manual overrides) ----
def maybe_reprioritize(state):
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_reprio") == today:
        return
    overrides = {}
    ovp = VAULT / ".baxter_overrides.json"
    if ovp.exists():
        try: overrides = json.loads(ovp.read_text(encoding="utf-8-sig"))
        except Exception: overrides = {}
    cutoff = datetime.now() - timedelta(days=7)
    def touched(line):
        low = line.lower()
        for key, d in overrides.items():
            if key and key.lower() in low:
                try:
                    if datetime.strptime(d, "%Y-%m-%d") >= cutoff:
                        return True
                except Exception:
                    pass
        return False
    soon = (datetime.now() + timedelta(days=2)).date()
    changed = 0
    bumped = []
    for md in (VAULT / "00-Inbox").glob("*.md"):
        try:
            lines = md.read_text(encoding="utf-8").split("\n")
        except Exception:
            continue
        dirty = False
        for i, line in enumerate(lines):
            if not line.lstrip().startswith("- [ ]") or "⏫" in line:
                continue
            m = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", line)
            if not m:
                continue
            try:
                due_d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except Exception:
                continue
            if due_d <= soon and not touched(line):
                lines[i] = line.rstrip() + " ⏫"
                dirty = True; changed += 1
                bumped.append(_clean_task_text(line))
        if dirty:
            md.write_text("\n".join(lines), encoding="utf-8")
    state["last_reprio"] = today
    if changed:
        log(f"auto-bumped {changed} due-soon task(s) to urgent")
        _say("🗂 **Board update, sir- moved to urgent (due within 2 days):**\n" + _bullets(sorted(set(bumped)), 5))

# ---- stale-task auto-demote (long-overdue + untouched -> drop urgency, tag #stale) ----
def maybe_rejig(state):
    """Hourly build-order re-triage (Atul, 9th July: "just do it every so often, unimportantly").

    NOT `maybe_reprioritize` above- that one bumps VAULT TASK lines by due date. This re-bands
    the BUILD QUEUE off each entry's impact/effort quadrant. Two different queues, two beats.

    Called every triage cycle; `rejig_pass()` unforced is a no-op until its own 3600s stamp
    expires, so the hook itself needs no cadence state. Non-vital by his explicit order: any
    failure here is swallowed, because a re-sort of the build order must never take down the
    triage cycle that files his email."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import baxter_rejig
        baxter_rejig.rejig_pass()
    except Exception as e:
        log(f"rejig pass skipped: {e}")


def maybe_demote(state):
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_demote") == today:
        return
    overrides = {}
    ovp = VAULT / ".baxter_overrides.json"
    if ovp.exists():
        try: overrides = json.loads(ovp.read_text(encoding="utf-8-sig"))
        except Exception: overrides = {}
    recent = datetime.now() - timedelta(days=7)
    def touched(line):
        low = line.lower()
        for key, d in overrides.items():
            if key and key.lower() in low:
                try:
                    if datetime.strptime(d, "%Y-%m-%d") >= recent:
                        return True
                except Exception:
                    pass
        return False
    way_overdue = (datetime.now() - timedelta(days=21)).date()
    changed = 0
    parked = []
    for md in (VAULT / "00-Inbox").glob("*.md"):
        try:
            lines = md.read_text(encoding="utf-8").split("\n")
        except Exception:
            continue
        dirty = False
        for i, line in enumerate(lines):
            if not line.lstrip().startswith("- [ ]") or "#stale" in line:
                continue
            m = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", line)
            if not m:
                continue
            try:
                due_d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except Exception:
                continue
            # The line's own date is no longer evidence of age- the overdue roll rewrites it
            # to today every pass, so nothing would EVER read as 3 weeks overdue and stale-
            # parking would quietly die. Ask the roll ledger when it was FIRST due.
            if _roll:
                try:
                    due_d = _roll.original_due(_roll.task_id(md, line), due_d)
                except Exception:
                    pass
            if due_d <= way_overdue and not touched(line):
                line2 = line.replace("⏫", "").replace("🔼", "").rstrip()
                if "🔽" not in line2:
                    line2 += " 🔽"
                lines[i] = line2 + " #stale"
                dirty = True; changed += 1
                parked.append(_clean_task_text(line))
        if dirty:
            md.write_text("\n".join(lines), encoding="utf-8")
    state["last_demote"] = today
    if changed:
        log(f"auto-demoted {changed} long-overdue task(s) to #stale")
        _say("🗄 **Filed away, sir- parked as stale (3+ weeks overdue, untouched):**\n"
             + _bullets(sorted(set(parked)), 5) + "\nSay the word in #general to revive any.",
             mention=False)

# ---- reply-awaited radar: chase-date passed -> escalate + AUTO-DRAFT the follow-up ----
def _followup_id(path, line):
    """Stable id for a stale-crossing: strip volatile bits (#attention, priority
    emojis, whitespace) so escalating doesn't re-arm, but a snoozed 📅 date does."""
    core = line.replace("#attention", "")
    core = re.sub(r"[⏫🔼🔺🔽]", "", core)
    core = re.sub(r"\s+", " ", core).strip()
    return f"{path.name}::{core}"

def maybe_followup(state, force=False):
    """Once daily: find open '#awaiting-reply' tasks whose 📅 chase date has passed,
    escalate them to #attention, and wake Claude ONCE to write ready-to-send
    follow-up drafts (Atul's voice, style doc) + put a send-task on today's plan."""
    today_s = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_followup") == today_s and not force:
        return
    if not _usage_ok("routine"):
        return   # retries next cycle
    state["last_followup"] = today_s
    today_d = datetime.now().date()
    done_ids = set(state.get("followed_up", []))
    stale = []   # (path, original line)
    for folder in ("00-Inbox", "20-Projects"):
        for md in (VAULT / folder).glob("*.md"):
            try:
                lines = md.read_text(encoding="utf-8").split("\n")
            except Exception:
                continue
            dirty = False
            for i, line in enumerate(lines):
                if not line.lstrip().startswith("- [ ]") or "#awaiting-reply" not in line:
                    continue
                m = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", line)
                if not m:
                    continue
                try:
                    chase_d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                except Exception:
                    continue
                fid = _followup_id(md, line)
                if chase_d > today_d or fid in done_ids:
                    continue
                stale.append((md, line.strip()))
                done_ids.add(fid)
                if "#attention" not in line:
                    lines[i] = line.rstrip() + " #attention"
                    dirty = True
            if dirty:
                md.write_text("\n".join(lines), encoding="utf-8")
    state["followed_up"] = sorted(done_ids)
    if not stale:
        return
    listing = "\n".join(f"- {p} :: {l}" for p, l in stale)
    prompt = (
        f"You are Baxter. Read {CONTRACT} and follow its conventions (hyphen style, UK English, "
        f"no #hashtags in prose, NEVER send anything outward). Today is {today_s}.\n\n"
        f"These 'awaiting reply' tasks have gone STALE — the chase date passed and the other "
        f"person still hasn't responded:\n\n{listing}\n\n"
        f"For EACH stale item:\n"
        f"1. Open the source note (and any linked notes / prior drafts in 40-Drafts, plus "
        f"People/<name>.md if it exists) to understand who Atul is chasing, about what, and "
        f"what was last sent.\n"
        f"2. Read {VAULT}/50-Research/Atul-writing-style.md and write a READY-TO-SEND follow-up "
        f"message in HIS voice to {VAULT}/40-Drafts/{today_s} - Follow-up to <who>.md with "
        f"frontmatter (project, status: awaiting-approval, to: <address/channel if known>). "
        f"Chasing someone senior: use the status-inquiry register from the style doc — "
        f"'is there any update on X?' + one practical reason for asking; never 'could you' "
        f"phrasing, never 'whenever convenient'. Reply on the existing thread where relevant "
        f"(note the subject as Re: <original subject>).\n"
        f"3. In the source note, directly below the awaiting task, add:\n"
        f"   - [ ] Send follow-up to <who> re <what> (draft ready: [[{today_s} - Follow-up to <who>]]) "
        f"#<project> #attention 📅 {today_s}\n"
        f"   so it lands on today's plan.\n"
        f"4. Log one line in 30-Daily/{today_s}.md (create if missing).\n"
        f"NEVER send anything — drafts await Atul's approval."
    )
    try:
        _touch_lock()
        subprocess.run(_claude("prose") + ["-p", prompt], cwd=str(VAULT), timeout=600, **_SILENT)
        who = ", ".join(re.sub(r"- \[ \]\s*(Awaiting reply from\s*)?", "", l)[:40] for _, l in stale[:3])
        log(f"followup radar: drafted {len(stale)} follow-up(s)")
        notify("🎩 Baxter", f"⏳ {len(stale)} repl{'y' if len(stale)==1 else 'ies'} gone quiet — follow-up draft{'' if len(stale)==1 else 's'} ready for approval. ({who})", mandatory=True)
        _say(f"📨 Gone quiet past the chase date: {who}. I've drafted the follow-up{'s' if len(stale)>1 else ''} "
             f"in your voice - waiting in Drafts for your approval, sir.")
    except Exception as e:
        log(f"followup radar failed: {e}")

# ---- REVERSE reconcile: close finished tasks the machinery never ticked off ----
# (Atul, 7th July- the one-directional-machinery fix. His Y2 results email landed and
#  was filed, yet the "grab Y2 results" task stayed open. The evidence is ALREADY in the
#  vault as recent notes/logs- match open tasks against it, auto-tick the sure ones, flag
#  the ambiguous. See 50-Research/Auto-reconcile to-do list - plan of attack.md +
#  memory auto-close-completed-tasks.)
RECONCILE_FLAG = VAULT / ".baxter_reconcile_flagged.json"  # legacy; kept in state now

def _open_reconcilable_tasks():
    """(path, stripped_line, clean_text) for open '- [ ]' tasks that a reconcile pass
    might close. Skips #awaiting-reply (the followup loop owns those) and already-#stale
    parked items (they're deliberately benched)."""
    out = []
    for folder in ("00-Inbox", "10-Tasks", "20-Projects"):
        d = VAULT / folder
        if not d.exists():
            continue
        for md in d.glob("*.md"):
            try:
                for line in md.read_text(encoding="utf-8").splitlines():
                    ls = line.lstrip()
                    if not ls.startswith("- [ ]"):
                        continue
                    if "#awaiting-reply" in line:
                        continue
                    ct = _clean_task_text(line)
                    if ct:
                        out.append((md, line.strip(), ct))
            except Exception:
                pass
    return out

def _recent_vault_evidence(days=4):
    """Completion evidence already sitting in the vault: recent inbox notes (with source),
    recent daily-log lines, recent drafts, and Atul's own recent WhatsApp lines. Returns a
    single readable text block (capped) for the reconcile judgement."""
    cutoff = datetime.now().timestamp() - days * 86400
    blocks = []
    # recent inbox notes- the primary signal (an incoming email/WA that completes a task
    # was filed here with its source)
    inbox_lines = []
    d = VAULT / "00-Inbox"
    if d.exists():
        for md in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            try:
                if md.stat().st_mtime < cutoff:
                    continue
                txt = md.read_text(encoding="utf-8")
                src = re.search(r"^source:\s*(.+)$", txt, re.M)
                src = src.group(1).strip() if src else "?"
                head = re.search(r"^#\s*(.+)$", txt, re.M)
                title = head.group(1).strip() if head else md.stem
                body = re.sub(r"^---.*?---", "", txt, flags=re.S)
                body = " ".join(body.split())[:280]
                inbox_lines.append(f"- [{src}] {title} :: {body}")
            except Exception:
                pass
    if inbox_lines:
        blocks.append("RECENT INBOX NOTES (filed items- an email/WA/chat that arrived):\n"
                      + "\n".join(inbox_lines[:30]))
    # recent daily-log lines
    dl_lines = []
    dd = VAULT / "30-Daily"
    if dd.exists():
        for md in sorted(dd.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:4]:
            try:
                if md.stat().st_mtime < cutoff:
                    continue
                for ln in md.read_text(encoding="utf-8").splitlines():
                    ln = ln.strip()
                    if ln.startswith("- ") and len(ln) > 4:
                        dl_lines.append(f"({md.stem}) {ln}")
            except Exception:
                pass
    if dl_lines:
        blocks.append("RECENT DAILY-LOG LINES:\n" + "\n".join(dl_lines[:40]))
    # recent drafts (weak signal- a draft written is partial, sending still pending)
    dr_lines = []
    drd = VAULT / "40-Drafts"
    if drd.exists():
        for md in sorted(drd.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:15]:
            try:
                if md.stat().st_mtime < cutoff:
                    continue
                txt = md.read_text(encoding="utf-8")
                st = re.search(r"^status:\s*(.+)$", txt, re.M)
                dr_lines.append(f"- {md.stem} (status: {st.group(1).strip() if st else '?'})")
            except Exception:
                pass
    if dr_lines:
        blocks.append("RECENT DRAFTS (written but NOT sent unless status: sent):\n" + "\n".join(dr_lines))
    # Atul's own recent WhatsApp lines (a commitment he then fulfilled- 'sent it', 'done')
    wa_lines = []
    if WA_FEED.exists():
        try:
            recs = WA_FEED.read_text(encoding="utf-8").splitlines()[-400:]
            for ln in recs:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                ts = r.get("ts", "")
                try:
                    if datetime.fromisoformat(ts.replace("Z", "")).timestamp() < cutoff:
                        continue
                except Exception:
                    pass
                if str(r.get("sender", "")).strip().lower().startswith("atul"):
                    wa_lines.append(f"[{ts[:16]}] Atul: {r.get('text','')[:160]}")
        except Exception:
            pass
    if wa_lines:
        blocks.append("ATUL'S OWN RECENT WHATSAPP MESSAGES (things he said/did):\n"
                      + "\n".join(wa_lines[-30:]))
    return "\n\n".join(blocks)

def maybe_reconcile(state, force=False):
    """REVERSE pass (once daily, routine-class): cross-check open tasks against completion
    evidence already filed in the vault. Auto-tick the sure ones, flag the ambiguous in ONE
    message, leave the rest. Fixes the machinery only capturing incoming items, never closing
    finished ones (Atul, 7th July)."""
    today_s = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_reconcile") == today_s and not force:
        return
    if not _usage_ok("routine"):
        return   # retries next cycle
    tasks = _open_reconcilable_tasks()
    if not tasks:
        state["last_reconcile"] = today_s
        return
    evidence = _recent_vault_evidence()
    if not evidence.strip():
        state["last_reconcile"] = today_s
        return
    state["last_reconcile"] = today_s
    already = set(state.get("reconciled_flagged", []))
    task_listing = "\n".join(f"- {p} :: {ln}" for p, ln, _ in tasks[:150])
    already_note = ("\nTasks you have ALREADY flagged as ambiguous on a previous pass "
                    "(do NOT flag these again- either they're genuinely still open or "
                    "waiting on Atul; only auto-tick one if NEW hard evidence now proves "
                    "it done):\n" + "\n".join(f"- {x}" for x in sorted(already)[:40])
                    if already else "")
    prompt = (
        f"You are Baxter. Read {CONTRACT} and follow its conventions (hyphen style 'word- word', "
        f"UK English, no #hashtags in prose, ordinal dates like '7th July', NEVER act outward). "
        f"Today is {today_s}.\n\n"
        f"This is the REVERSE RECONCILE pass. The watcher captures incoming items but never closes "
        f"a task once the world completes it (Atul's 7th-July complaint: his exam results email "
        f"landed and was filed, yet the 'grab exam results' task stayed open). Your job: decide "
        f"which OPEN tasks below are actually DONE, using the completion evidence already filed in "
        f"the vault.\n\n"
        f"OPEN TASKS (file path :: task line):\n{task_listing}\n\n"
        f"COMPLETION EVIDENCE (recently filed in the vault):\n{evidence}\n{already_note}\n\n"
        f"For EACH open task, choose ONE:\n"
        f"1. AUTO-TICK — only when the evidence UNAMBIGUOUSLY and DIRECTLY proves this exact task's "
        f"deliverable is done (e.g. task 'grab exam results' + an inbox note source:email titled "
        f"'Year 2 results'; task 'email Alex the invoice' + a source:email-sent to Alex "
        f"about the invoice; a commitment he confirmed done on WhatsApp). Edit the task line in its "
        f"file: change '- [ ]' to '- [x]' and append ' ✅ {today_s}'. Do NOT touch anything else on "
        f"the line. Add ONE line to 30-Daily/{today_s}.md (create if missing) noting it auto-closed "
        f"and the evidence.\n"
        f"2. FLAG — evidence SUGGESTS done but isn't certain, OR it's money/legal/family (NEVER "
        f"auto-tick those- always flag), OR only a due-date has passed. Collect these; do NOT edit "
        f"the line.\n"
        f"3. LEAVE — no evidence it's done. Do nothing.\n\n"
        f"STRICT: when in doubt, FLAG- never AUTO-TICK. A wrongly-closed task is worse than a "
        f"flagged one. A draft merely written (status not 'sent') does NOT complete a 'send' task. "
        f"A passed 📅 date alone never auto-ticks.\n\n"
        f"After processing, if you AUTO-TICKED any and/or have FLAGGED any, send ONE consolidated "
        f"message (butler register, SHORT BULLETS, no slurry, no #tags) via:\n"
        f'  python "{BAXTER_SAY}" --channel reminders "<message>"\n'
        f"Structure it: a '**Closed off (looked done):**' bullet list of what you auto-ticked "
        f"(so he can undo any), then a '**These look done- tick them off, sir?**' bullet list of "
        f"the flagged ones. If nothing was ticked and nothing flagged, send NOTHING.\n"
        f"Finally, print on the LAST line of your output exactly: FLAGGED_FINGERPRINTS: "
        f"followed by a JSON list of the flagged task lines' text (clean, no checkbox/tags), "
        f"e.g. FLAGGED_FINGERPRINTS: [\"grab Y2 results\", \"pay the deposit\"]. Empty list if none."
    )
    try:
        _touch_lock()
        # Prompt is large (open tasks + evidence blocks)- pipe it via STDIN, not argv:
        # Windows caps a command line at ~32KB (WinError 206), which the evidence blows past.
        r = subprocess.run(_claude("reconcile") + ["-p"], cwd=str(VAULT), timeout=600,
                           input=prompt, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "")
        m = re.search(r"FLAGGED_FINGERPRINTS:\s*(\[.*\])\s*$", out, re.S)
        if m:
            try:
                newly = json.loads(m.group(1))
                for f in newly:
                    already.add(re.sub(r"\s+", " ", str(f)).strip().lower())
                state["reconciled_flagged"] = sorted(already)
            except Exception:
                pass
        log(f"reconcile pass ran ({len(tasks)} open task(s) checked)")
    except Exception as e:
        log(f"reconcile pass failed: {e}")

# ---- projects: regenerate the dashboard's project+thread list ----
def _regen_projects():
    try:
        env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"   # force utf-8 stdout so titles don't mojibake
        r = subprocess.run(["python", CONVOS, "projects-json"], capture_output=True, timeout=120, env=env)
        if r.returncode == 0 and r.stdout.strip():
            PROJECTS_JSON.write_bytes(r.stdout)
            return True
    except Exception as e:
        log(f"projects regen failed: {e}")
    return False

def maybe_scan_projects(state):
    last = state.get("last_projscan", "")
    try:
        if PROJECTS_JSON.exists() and last and (datetime.now() - datetime.fromisoformat(last)).total_seconds() < 300:
            return
    except Exception:
        pass
    if _regen_projects():
        state["last_projscan"] = datetime.now().isoformat()
        log("projects list refreshed")

# ---- project tier moves (dashboard drag between current/shelved/graveyard) ----
def process_projmove():
    if not PROJMOVE.exists():
        return
    try:
        lines = [l.strip() for l in PROJMOVE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return
    if not lines:
        return
    try: PROJMOVE.write_text("", encoding="utf-8")
    except Exception: pass
    moved = False
    for ln in lines:
        if "|" not in ln:
            continue
        key, tier = [x.strip() for x in ln.split("|", 1)]
        if tier == "deprecated":
            tier = "deprecated"
        try:
            subprocess.run(["python", CONVOS, "set-tier", key, tier], capture_output=True, timeout=30)
            moved = True
            log(f"project {key} -> {tier}")
        except Exception as e:
            log(f"projmove failed ({key}): {e}")
    if moved:
        _regen_projects()

# ---- open-project queue (the dashboard's Open buttons write target keys here) ----
def process_open_queue():
    if not OPEN_QUEUE.exists():
        return
    try:
        targets = [l.strip() for l in OPEN_QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return
    if not targets:
        return
    try: OPEN_QUEUE.write_text("", encoding="utf-8")
    except Exception: pass
    for tgt in targets:
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                              "-File", OPENER, "-Target", tgt],
                             creationflags=_NO_WIN, **_SILENT)
            log(f"opened project target: {tgt}")
        except Exception as e:
            log(f"open failed ({tgt}): {e}")

# ---- mine a project's Claude conversations for open action items -> tasks ----
MINE_SEP = "\n\n===== NEXT CONVERSATION =====\n\n"
MINE_CHUNK = 150_000        # chars per window (~37K tokens); Opus's 1M window holds it with room to think
MINE_FANOUT = 3             # chunk extractions in flight at once- bounds wall clock without a spawn storm
MINE_THREADS = 40           # threads read per project (was 5, which silently dropped 656 of UCL's 661)


def _mine_chunks(text, size=MINE_CHUNK):
    """Split `text` into non-overlapping windows of at most `size` chars.

    HARD INVARIANT: ''.join(_mine_chunks(t)) == t for every t. Nothing is dropped and
    nothing is duplicated- that is the entire point of this function, which replaces a
    `combined[:200000]` that silently threw away 83% of the fortnite transcript.

    Cuts prefer a conversation separator, then the last newline in the window, then a
    hard cut. Cutting on a newline means a to-do line is never sliced down the middle.
    A to-do can still straddle two windows *contextually*- that is the accepted cost of
    non-overlapping windows, since overlap would break the invariant above."""
    if not text:
        return []
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    out, pos, n = [], 0, len(text)
    while pos < n:
        end = min(pos + size, n)
        if end < n:
            cut = -1
            # A separator cut is only worth taking near the END of the window. Search the
            # whole window and a project with many short threads (deadlock has 11, spaced
            # ~52K apart) backs every cut off to an early separator, burning a model call on
            # a two-thirds-empty window. Below that band the newline cut is the right one:
            # it keeps to-do lines whole, which is all the boundary really has to do.
            i = text.rfind(MINE_SEP, pos + (size * 9) // 10, end)
            if i > pos:
                cut = i + len(MINE_SEP)
            if cut <= pos:
                i = text.rfind("\n", pos, end)
                if i >= pos:
                    cut = i + 1
            if cut <= pos or cut > end:
                cut = end          # no usable break point- hard cut, never stall
        else:
            cut = end
        out.append(text[pos:cut])
        pos = cut
    return out


def _mine_key(t):
    """Normalised identity of an extracted task: strip a leading checkbox, collapse
    whitespace, casefold, drop trailing punctuation. Deliberately conservative- it must
    merge 'Email Alex.' with '- [ ] email  Alex', and merge nothing else. An
    over-eager key would silently fuse two real tasks: the same data loss this build
    exists to kill, moved one layer down."""
    t = re.sub(r"^\s*-\s*\[.\]\s*", "", str(t or ""))
    t = " ".join(t.split()).casefold()
    return t.rstrip(" .!;:,")


def _mine_merge(task_lists):
    """Flatten per-window task lists into one deduped list, first-seen order preserved.

    Deterministic: an insertion-ordered dict keyed by _mine_key- no set iteration order
    leaks into the output. Where a later duplicate carries a field the first-seen record
    left empty (a due date, a tag), that field is filled in rather than discarded."""
    merged = {}
    for lst in task_lists or []:
        for t in lst or []:
            if not isinstance(t, dict):
                t = {"text": t}
            k = _mine_key(t.get("text"))
            if not k:
                continue
            if k not in merged:
                merged[k] = dict(t)
            else:
                for f, v in t.items():
                    if v and not merged[k].get(f):
                        merged[k][f] = v
    return list(merged.values())


def _mine_extract(key, i, chunk, total, env):
    """Mine ONE window. Extract-only: writes a JSON array, creates no note, sends nothing.
    Returns the parsed task list (possibly empty). Never raises- a dead window costs its
    own tasks, not the whole project's mine."""
    inp = VAULT / f".baxter_mine_input_{key}_{i}.txt"
    outp = VAULT / f".baxter_mine_out_{key}_{i}.json"
    try:
        inp.write_text(chunk, encoding="utf-8")
    except Exception as e:
        log(f"mine: window {i + 1}/{total} ({key}) unwritable: {e}")
        return []
    try: outp.unlink()
    except Exception: pass
    prompt = (
        f"You are an EXTRACTOR, not a filer. This is window {i + 1} of {total} of Atul's "
        f"'{key}' Claude conversation transcript, in the file {inp} - read it.\n\n"
        f"Extract the action items he still needs to do: people to contact, questions to ask, "
        f"things to build / study / decide / send. Be conservative - genuine, still-open to-dos "
        f"only, no fluff. Skip anything this text shows as already done or clearly closed.\n\n"
        f"HARD RULES, all binding:\n"
        f"- Write ONLY a JSON array to {outp}. Each element: "
        f'{{"text": "<the to-do, one plain sentence>", "tag": "{key}", '
        f'"due": "<YYYY-MM-DD, or empty string>", "time_sensitive": <true|false>}}\n'
        f"- Write an empty array [] if this window holds no open to-dos. That is a normal result.\n"
        f"- Do NOT create, edit or touch any vault note. Do NOT write to 00-Inbox. "
        f"Do NOT send, post or message anything, to anyone, ever.\n"
        f"- This is ONE window of a larger transcript. Do not summarise the project as a whole, "
        f"and do not speculate about the other windows. Extract what is in THIS text.\n"
        f"- No #hashtags in the text field. Plain language, UK English."
    )
    try:
        subprocess.run(_claude("mine") + ["-p", prompt], cwd=str(VAULT), timeout=600, env=env, **_SILENT)
    except Exception as e:
        log(f"mine: window {i + 1}/{total} ({key}) failed: {e}")
        return []
    finally:
        try: inp.unlink()
        except Exception: pass
    tasks = []
    try:
        raw = json.loads(outp.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            tasks = raw
        else:
            log(f"mine: window {i + 1}/{total} ({key}) returned {type(raw).__name__}, not a list")
    except Exception as e:
        log(f"mine: window {i + 1}/{total} ({key}) no usable JSON: {e}")
    try: outp.unlink()
    except Exception: pass
    return tasks


def _mine_note_written(name, since):
    """The note the filing pass was told to write- '<today> - <name> open items.md' in
    00-Inbox- if it is there AND this pass is what wrote it. Returns the Path, else None.

    Compares real filenames against the literal suffix rather than globbing
    f'* - {name} open items.md': a project named 'Fortnite [map]' is a glob character
    class, so the glob silently finds nothing for a note that plainly exists. The 2s
    slack on `since` absorbs filesystem mtime granularity, not a stale note (which is
    minutes old, not milliseconds)."""
    suffix = f" - {name} open items.md"
    try:
        entries = list((VAULT / "00-Inbox").iterdir())
    except Exception:
        return None
    newest, newest_m = None, 0.0
    for p in entries:
        if len(p.name) <= len(suffix) or not p.name.endswith(suffix):
            continue
        try:
            m = os.stat(p).st_mtime
        except Exception:
            continue
        if m >= since - 2.0 and m > newest_m:
            newest, newest_m = p, m
    return newest


def process_mine_queue():
    import concurrent.futures as _futures   # local: keeps this build inside its own region
    if not MINE_QUEUE.exists():
        return
    if not _usage_ok("routine"):
        return   # queue file untouched- consumed next cycle
    try:
        keys = [l.strip() for l in MINE_QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return
    if not keys:
        return
    try: MINE_QUEUE.write_text("", encoding="utf-8")
    except Exception: pass
    proj = {}
    try: proj = json.loads(PROJECTS_JSON.read_text(encoding="utf-8-sig"))
    except Exception: proj = {}
    allp = []
    for tier in ("current", "shelved", "deprecated"):
        allp += proj.get(tier, [])
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
    for key in keys:
        p = next((x for x in allp if x.get("key") == key), None)
        if not p:
            continue
        sids = [t["sid"] for t in p.get("threads", []) if t.get("sid") and t["sid"] != "NEW"]
        # A discarded thread is never silent (it was `sids[:5]`, unlogged, and UCL has 661).
        if len(sids) > MINE_THREADS:
            log(f"mine: {key} has {len(sids)} threads, reading the {MINE_THREADS} most recent- "
                f"{len(sids) - MINE_THREADS} not scanned")
        texts = []
        for sid in sids[:MINE_THREADS]:
            try:
                r = subprocess.run(["python", CONVOS, "convo-text", sid], capture_output=True, timeout=90, env=env)
                if r.returncode == 0 and r.stdout:
                    texts.append(r.stdout.decode("utf-8", "ignore"))
            except Exception:
                pass
        combined = MINE_SEP.join(texts).strip()
        if not combined:
            continue
        name = p.get("name", key)

        # ---- pass 1: extract from EVERY window. The payload is consumed whole, not
        # truncated to its first 200K chars. Windows run a few at a time so a 9-window
        # project does not block the triage cycle for an hour.
        chunks = _mine_chunks(combined)
        log(f"mine: {key} payload {len(combined)} chars -> {len(chunks)} window(s)")
        results = [[] for _ in chunks]
        try:
            _touch_lock()
            with _futures.ThreadPoolExecutor(max_workers=MINE_FANOUT) as ex:
                futs = {ex.submit(_mine_extract, key, i, c, len(chunks), env): i
                        for i, c in enumerate(chunks)}
                for f in _futures.as_completed(futs):
                    i = futs[f]
                    try:
                        results[i] = f.result() or []
                    except Exception as e:
                        log(f"mine: window {i + 1}/{len(chunks)} ({key}) crashed: {e}")
                    _touch_lock()
        except Exception as e:
            log(f"mine failed ({key}): {e}")
            continue

        # A window that returned nothing may be empty or may be a dead claude call. If
        # NONE of them produced a task the mine is not trustworthy- say so, write no note.
        got = sum(1 for r in results if r)
        tasks = _mine_merge(results)
        if not tasks:
            log(f"mine: {key} extracted 0 tasks from {len(chunks)} window(s)- no note written")
            continue
        log(f"mine: {key} {sum(len(r) for r in results)} raw -> {len(tasks)} deduped "
            f"(from {got}/{len(chunks)} windows)")

        # ---- pass 2: ONE filing pass over the merged list. Same job the old single
        # prompt did- dedup against the live vault, skip closed items, write the note.
        merged = VAULT / f".baxter_mine_merged_{key}.json"
        try:
            merged.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log(f"mine failed ({key}): merged list unwritable: {e}")
            continue
        prompt = (
            f"You are Baxter. Read {CONTRACT} and follow it. Atul's OPEN to-dos have been "
            f"extracted from his '{name}' project conversations- the whole transcript, mined in "
            f"{len(chunks)} window(s) and deduped. The {len(tasks)} candidate items are in the JSON "
            f"file {merged} — read it.\n\n"
            f"Write them up as tasks per the contract in a note 00-Inbox/<today> - {name} open items.md, "
            f"each tagged #{key} (and #attention if time-sensitive), with due dates only if clearly "
            f"implied. DEDUP hard against existing OPEN tasks already in the vault — do NOT recreate "
            f"ones that exist, and SKIP anything already done or clearly closed. Merge any candidates "
            f"that say the same thing in different words. Be conservative — genuine, still-open to-dos "
            f"only, no fluff. Plain language, no #hashtags in prose. Write exactly ONE note. NEVER send anything."
        )
        # The ping follows the ARTEFACT, never the subprocess returning. On 9th July the
        # filing claude ran, exited clean, wrote nothing, and Atul was told his tasks had
        # been pulled. A no-op mine now says so and stays quiet. `t0` is stamped before
        # the spawn so a note this pass wrote outranks one an earlier pass left behind.
        t0 = time.time()
        try:
            _touch_lock()
            r = subprocess.run(_claude("mine") + ["-p", prompt], cwd=str(VAULT), timeout=600, env=env, **_SILENT)
            if r.returncode != 0:
                log(f"mine: {key} filing pass exited {r.returncode}")
            note = _mine_note_written(name, t0)
            if note is None:
                # The merged candidate list is NOT deleted: it is the evidence of what the
                # windows found, and the only way to see what a dead filing pass was handed.
                log(f"mine: {key} filing pass wrote no note")
                continue
            log(f"mined open tasks for project {key} ({len(tasks)} candidates, "
                f"{len(chunks)} windows) -> {note.name}")
            notify("🎩 Baxter", f"Pulled open tasks from your {name} chats.")
        except Exception as e:
            log(f"mine failed ({key}): {e}")
            continue
        try: merged.unlink()
        except Exception: pass

# ---- mobile reminders: daily due-today/overdue digest to #reminders (deterministic, no claude) ----
def _bullets(items, n=6):
    """Atul's hard rule (5th July, third strike): briefs are BULLET LISTS- one task
    per line, short, never ' · '-chained prose. Strips wikilinks to labels, trims
    each line, and counts the overflow instead of silently dropping it."""
    items = list(items)
    out = []
    for t in items[:n]:
        t = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", t)
        t = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m.group(1).split("/")[-1], t)
        t = " ".join(t.split())
        if len(t) > 140:
            t = t[:137].rstrip() + "..."
        out.append("- " + t)
    if len(items) > n:
        out.append(f"- (+{len(items) - n} more on the board)")
    return "\n".join(out)

def _clean_task_text(line):
    t = re.sub(r"[📅⏳🛫➕✅]\s*\d{4}-\d{2}-\d{2}", "", line)
    t = re.sub(r"[⏫🔼🔽⏬🔺]", "", t)
    t = re.sub(r"#[\w/-]+", "", t)
    t = re.sub(r"^\s*-\s*\[.\]\s*", "", t)
    return " ".join(t.split()).strip()

def _open_dated_tasks():
    """(due_date, clean_text) for every open task with a 📅 date, across the task folders.

    Rolls overdue dates to today FIRST (Atul, 5th July 12:04). Every brief builder reads
    tasks through here, so no brief can render a past date even if the pass never reached
    its own roll- `roll_once` memoises, so the repeat calls cost nothing."""
    if _roll:
        try:
            _roll.roll_once()
        except Exception as e:
            log(f"overdue roll failed: {e}")
    out = []
    for folder in ("00-Inbox", "10-Tasks", "20-Projects"):
        d = VAULT / folder
        if not d.exists():
            continue
        for md in d.glob("*.md"):
            try:
                for line in md.read_text(encoding="utf-8").splitlines():
                    if not line.lstrip().startswith("- [ ]"):
                        continue
                    # An awaiting-reply chase date is radar, not his docket (contract step 13:
                    # it "waits QUIETLY"). It belongs to _open_awaiting()'s "Waiting on others",
                    # which renders no date- so it is deliberately NOT rolled, and must not leak
                    # a past date in here. Listing it both places was a double-entry besides.
                    if "#awaiting-reply" in line:
                        continue
                    m = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", line)
                    if not m:
                        continue
                    try:
                        due = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                    except Exception:
                        continue
                    out.append((due, _clean_task_text(line)))
            except Exception:
                pass
    return out

def _say(msg, channel="reminders", mention=True):
    # Clean BEFORE the trim: the rsplit below must cut on the real newlines clean_outbound
    # creates, not on the ' / ' shorthand they replace, and a repaired multi-byte glyph must
    # not be sliced in half by a truncation that ran first.
    msg = _clean(msg)
    # Discord hard-caps at 2000 chars (the mention prefix eats some)- cut at a clean
    # bullet boundary instead of mid-word, and say that we cut.
    if len(msg) > 1850:
        msg = msg[:1850].rsplit("\n", 1)[0] + "\n- (trimmed- full board in the vault)"
    try:
        args = ["python", BAXTER_SAY, "--channel", channel]
        if not mention:
            args.append("--no-mention")
        args.append(msg)
        env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
        subprocess.run(args, capture_output=True, timeout=30, env=env)
        return True
    except Exception as e:
        log(f"say failed: {e}")
        return False

def _open_awaiting():
    """(chase_date, clean_text) for open #awaiting-reply tasks."""
    out = []
    for folder in ("00-Inbox", "20-Projects"):
        d = VAULT / folder
        if not d.exists():
            continue
        for md in d.glob("*.md"):
            try:
                for line in md.read_text(encoding="utf-8").splitlines():
                    if not line.lstrip().startswith("- [ ]") or "#awaiting-reply" not in line:
                        continue
                    m = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", line)
                    due = None
                    if m:
                        try: due = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                        except Exception: pass
                    out.append((due, _clean_task_text(line)))
            except Exception:
                pass
    return out

def maybe_remind(state):
    """The secretary's MORNING BRIEF (once a day from 08:00, @mention = phone push):
    today's docket, what's slipping, what's on the horizon, who we're waiting on."""
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_remind") == today or not (8 <= datetime.now().hour < 22):
        return
    tasks = _open_dated_tasks()
    td = datetime.now().date()
    # OVERDUE ROLLS FORWARD (Atul, 5th July 12:04): an overdue task IS due today- never a
    # separate "slipping" bucket, an "N other overdue items" aggregate, or a "was due 3d
    # ago" shame-stamp. baxter_roll has already rewritten the dates; `d <= td` is the belt
    # in case it couldn't. Every one is named, and reads as due today.
    due = sorted({t for d, t in tasks if d <= td})
    tomorrow = sorted({t for d, t in tasks if (d - td).days == 1})
    waiting = sorted({(f"{t} (chase due)" if (d and d <= td) else t) for d, t in _open_awaiting()})
    if not due and not tomorrow and not waiting:
        state["last_remind"] = today
        return
    wd = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"][td.weekday()]
    parts = [f"☀️ **Morning, sir - your {wd} brief.**"]
    if due:      parts.append("**On the docket today:**\n" + _bullets(due, 12))
    if waiting:  parts.append("**Waiting on others:**\n" + _bullets(waiting, 4))
    if tomorrow: parts.append("**On the horizon (tomorrow):**\n" + _bullets(tomorrow, 4))
    if _say("\n".join(parts)):
        state["last_remind"] = today
        log(f"morning brief sent ({len(due)} on the docket incl. rolled)")

def maybe_pulse(state):
    """Midday check-UP on Atul (from 13:00): how's the day going, what's still open,
    an easy opening to reshuffle or flag blockers."""
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_pulse") == today or not (13 <= datetime.now().hour < 16):
        return
    td = datetime.now().date()
    tasks = _open_dated_tasks()
    due = sorted({t for d, t in tasks if d <= td})
    if not due:
        state["last_pulse"] = today
        return
    parts = ["🕐 **Midday pulse, sir - how goes it?**"]
    parts.append("Still on today's docket:\n" + _bullets(due, 5))
    parts.append("Anything blocking you or worth reshuffling, a line here sorts it.")
    if _say("\n".join(parts)):
        state["last_pulse"] = today
        log("midday pulse sent")

def maybe_winddown(state):
    """Evening wind-down (from 21:00): tomorrow's shape + an offer to roll today's
    leftovers over, so the day closes deliberately instead of trailing off."""
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_winddown") == today or not (21 <= datetime.now().hour < 23):
        return
    td = datetime.now().date()
    tasks = _open_dated_tasks()
    left = sorted({t for d, t in tasks if d <= td})   # rolled, so `<=` is a belt, not a bucket
    tomorrow = sorted({t for d, t in tasks if (d - td).days == 1})
    if not left and not tomorrow:
        state["last_winddown"] = today
        return
    parts = ["🌙 **Winding down, sir.**"]
    if left:
        parts.append("Left on today's plate:\n" + _bullets(left, 4) +
                     "\nSay 'roll them over' and I'll move them to tomorrow.")
    if tomorrow:
        parts.append("**Tomorrow holds:**\n" + _bullets(tomorrow, 5))
    parts.append("Rest well - the morning brief will have it all in order.")
    if _say("\n".join(parts)):
        state["last_winddown"] = today
        log("evening wind-down sent")

def maybe_nudge(state):
    """End-of-day check (from 17:00): anything still open that was due TODAY gets one
    'needs you NOW' nudge, so nothing dies quietly at midnight."""
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_nudge") == today or not (17 <= datetime.now().hour < 22):
        return
    td = datetime.now().date()
    due = sorted({t for d, t in _open_dated_tasks() if d <= td})   # rolled; `<=` is the belt
    if not due:
        state["last_nudge"] = today
        return
    msg = ("🕔 **End-of-day check, sir.** Still open and due today:\n" + _bullets(due, 6) +
           "\nIf any won't happen, say the word in #general and I'll move them.")
    if _say(msg):
        state["last_nudge"] = today
        log(f"end-of-day nudge sent ({len(due)} still open)")

# ---- daily local git backup (version history; secrets are git-ignored) ----
SCRIPTS = Path(r"C:\Users\you\Documents\Python Scripts")

# Baxter's own source, admitted by explicit whitelist. `:(glob)` is load-bearing:
# in a plain git pathspec `*` crosses `/`, so `utils/*.ps1` also matches
# utils/baxter_whatsapp/node_modules/.bin/pino.ps1. `:(glob)` stops at `/`.
# Never `add -A` here- this repo pushes to a PUBLIC remote (LolStar123/poe-scripts)
# and carries ~640 untracked files. We commit locally and never contact the remote.
SCRIPTS_PATHSPEC = [":(glob)utils/baxter_*.py", ":(glob)utils/*.ps1", ":(glob)coc_bot/*.py"]


def _git(repo, *args, timeout=60):
    return subprocess.run(["git", "-C", str(repo), *args], timeout=timeout, capture_output=True)


def _backup_vault(state, today):
    """The vault: no remote, secrets git-ignored, so a blanket add -A is safe."""
    if state.get("last_backup") == today or not (VAULT / ".git").exists():
        return
    try:
        _git(VAULT, "add", "-A")
        _git(VAULT, "commit", "-m", f"baxter auto-backup {today}")
        state["last_backup"] = today
        log(f"backup committed {today}")
    except Exception as e:
        log(f"backup failed: {e}")


def _backup_scripts(state, today):
    """Baxter's own source. Separate repo, separate state key- never merged with the vault."""
    if state.get("last_backup_scripts") == today or not (SCRIPTS / ".git").exists():
        return
    try:
        # Stage each pathspec on its own. `git add` exits non-zero if ANY pathspec matches
        # nothing, so a single empty glob would abort the whole backup and log a failure
        # every day after. An empty glob is worth SAYING- it means the whitelist has gone
        # stale and something stopped being backed up- but it must not stop the rest.
        for spec in SCRIPTS_PATHSPEC:
            add = _git(SCRIPTS, "add", "--", spec)
            if add.returncode == 0:
                continue
            err = add.stderr.decode("utf-8", "replace").strip()
            if "did not match any files" in err:
                log(f"scripts backup: pathspec {spec} matches nothing- whitelist may be stale")
                continue
            log(f"scripts backup FAILED at add: {err}")
            return
        # `git commit` exits 1 on an empty index. That is the normal no-change case and
        # must not read as failure- but a real commit error must not read as success.
        if _git(SCRIPTS, "diff", "--cached", "--quiet").returncode == 0:
            state["last_backup_scripts"] = today
            log(f"scripts backup: no changes {today}")
            return
        commit = _git(SCRIPTS, "commit", "-m", f"baxter auto-backup {today}")
        if commit.returncode != 0:
            log(f"scripts backup FAILED at commit: {commit.stderr.decode('utf-8', 'replace').strip()}")
            return
        state["last_backup_scripts"] = today
        log(f"scripts backup committed {today}")
    except Exception as e:
        log(f"scripts backup failed: {e}")


def maybe_backup(state):
    """Two independent repos, two independent failures. Neither can block the other."""
    today = datetime.now().strftime("%Y-%m-%d")
    _backup_vault(state, today)
    _backup_scripts(state, today)

# ---- DISPATCH ----------------------------------------------------------------
BATCH_DIR = VAULT / ".baxter_batches"   # journal of dispatched-but-unfinished worker batches
INTERRUPTED = VAULT / ".baxter_interrupted.json"   # mid-flight state of halted builds (contract step 29)
RESUME_DIR = VAULT / ".baxter_resume"              # journal of spawned-but-unfinished resume workers
# A selftest must never stamp, re-queue or delete a LIVE lane's journal. Read once, here, so
# the `--stamp-governor-kill` subprocess lands in the same scratch dir its parent test set up.
if os.environ.get("BAXTER_RESUME_DIR"):
    RESUME_DIR = Path(os.environ["BAXTER_RESUME_DIR"])

def _triage_prompt(items):
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    pending = ""
    try:
        if PENDING_Q.exists():
            pq = json.loads(PENDING_Q.read_text(encoding="utf-8-sig"))
            if pq:
                pending = (f"\n\nPENDING CLARIFYING QUESTIONS you previously asked Atul (in {PENDING_Q}): "
                           f"{json.dumps(pq, ensure_ascii=False)}. If any ITEM below answers one, apply the "
                           f"answer (update the original note/task it references), then REMOVE that entry "
                           f"from the json file.")
    except Exception:
        pass
    return (
        f"You are Baxter. Read {CONTRACT} and follow it exactly. "
        f"Triage these {len(items)} NEW item(s) into the vault at {VAULT} "
        f"(inbox notes + tasks + drafts + daily log). NEVER send anything outward - the ONE exception "
        f"is speaking to Atul himself in his own server via: python \"{BAXTER_SAY}\" \"<message>\" "
        f"(use it ONLY per the contract's clarifying-question step).\n\n"
        # The two MACHINE-READABLE invariants, restated. The contract already says both, but it
        # says them among 31 numbered steps, and filing now runs on the grunt tier (Haiku 4.5).
        # Measured over the 9th-July eyeball diff: an unstated invariant held ~50% of passes,
        # against a 95% Opus baseline for the tag. A dropped #tag is SILENT- the note reads fine
        # and the task simply never appears in his dashboard's Dataview queries. Restating the
        # spec is what makes a cheap worker a cheaper worker, not a lesser one.
        f"NON-NEGOTIABLE (the dashboard breaks silently without these):\n"
        f"1. EVERY task line you write ends with its project tag- `- [ ] <task> #<project>` "
        f"(plus #attention only if it genuinely needs Atul now). A task line with no #<project> "
        f"tag is invisible to his dashboard. Never omit it.\n"
        f"2. Log ONE line per filed item into 30-Daily/<today>.md (create the file if missing).\n"
        f"3. Genuine compound hyphens stay closed: write `co-op`, `off-peak`, `turn-based`. "
        f"The `word- word` style is for dash punctuation ONLY, never inside a compound word."
        f"{senders_hint()}{pending}\n\n"
        f"{_rules.WRITER_TOUCH_RULE}\n"
        f"{_rules.voice()}\n\n"
        f"ITEMS:\n{payload}"
    )

def wake_claude(items):
    """Dispatch a triage batch to a PARALLEL detached worker (Atul: 'easy things get fast
    responses, hard things take time' - so nothing queues behind an unrelated long run).
    The batch is journaled to disk first; the worker deletes it on success and an orphan
    sweep respawns anything that dies. Falls back to synchronous if too many in flight."""
    try:
        BATCH_DIR.mkdir(exist_ok=True)
        in_flight = list(BATCH_DIR.glob("*.json"))
        bf = BATCH_DIR / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
        bf.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        if len(in_flight) >= 5:
            # too many parallel workers - run this one inline (backpressure)
            _worker_run(bf)
            return
        env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
        subprocess.Popen(["python", os.path.abspath(__file__), "--worker", str(bf)],
                         cwd=str(VAULT), env=env,
                         creationflags=_NO_WIN, **_SILENT)
        log(f"dispatched batch of {len(items)} to parallel worker ({len(in_flight)+1} in flight)")
    except Exception as e:
        log(f"dispatch failed ({e}) - running inline")
        try:
            # class 'file' = Haiku 4.5 (the grunt tier); _batch_lane only decides whether
            # a browser loads, never the model.
            subprocess.run(_claude("file", mcp=_batch_lane(items)) + ["-p", _triage_prompt(items)],
                           cwd=str(VAULT), timeout=600, **_SILENT)
        except Exception as e2:
            print(f"claude run failed: {e2}")

def _worker_run(batch_file):
    """Worker mode: process one journaled batch. The journal is deleted ONLY on a
    claude exit code of 0 - a failed run (usage limit, API error) leaves it in place
    so sweep_orphan_batches respawns it until it succeeds. (4th-July lesson: Atul's
    usage limit ran out and workers silently 'finished' in 3s, eating his messages.)"""
    try:
        items = json.loads(Path(batch_file).read_text(encoding="utf-8-sig"))
    except Exception as e:
        log(f"worker couldn't read batch: {e}")
        return
    if not _usage_ok("routine"):
        log(f"governor: routine triage held past the critical line- batch kept ({Path(batch_file).name})")
        # touch the journal so the orphan sweep's 15-min clock restarts- otherwise a
        # held batch respawns EVERY sweep (the 5th-July ping-spam morning's churn)
        try: os.utime(batch_file, None)
        except Exception: pass
        return
    try:
        # class 'file' = Haiku 4.5. Inbox filing is structured, templated, high-volume-
        # the grunt tier absorbs it so the flagship never touches filing again.
        r = subprocess.run(_claude("file", mcp=_batch_lane(items)) + ["-p", _triage_prompt(items)], cwd=str(VAULT),
                           timeout=900, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            err = " ".join((r.stderr or r.stdout or "").split())[:300]
            log(f"worker claude FAILED (exit {r.returncode}) - batch kept for retry: {err}")
            try: os.utime(batch_file, None)   # restart the sweep clock- retry in 15 min, not every sweep
            except Exception: pass
            return
        try: Path(batch_file).unlink()
        except Exception: pass
        log(f"worker finished batch of {len(items)}")
    except FileNotFoundError:
        print("claude CLI not found on PATH")
    except Exception as e:
        log(f"worker failed ({e}) - batch kept for retry")

def sweep_orphan_batches():
    """Respawn batches whose worker died or whose claude run failed (>15 min old).
    Retries up to 12 times before parking- and maybe_resume() clears the retry
    counters on every usage refresh, so a long outage can never drop messages."""
    try:
        if not BATCH_DIR.exists():
            return
        if not _usage_ok("routine"):
            return   # governor freeze- respawning now just churns held workers
        now = datetime.now().timestamp()
        for bf in BATCH_DIR.glob("*.json"):
            if now - bf.stat().st_mtime < 900:
                continue
            if bf.stem.count(".retry") >= 12:
                # PARK, don't drop (5th-July change): the usage refresh un-parks it
                if not bf.name.endswith(".parked.json"):
                    log(f"orphan batch failed {bf.stem.count('.retry')}x- parked until the usage refresh: {bf.name}")
                    bf.rename(bf.with_name(bf.stem + ".parked.json"))
                continue
            nb = bf.with_name(bf.stem + ".retry.json")
            bf.rename(nb)
            try: os.utime(nb, None)   # fresh 15-min window for the new worker
            except Exception: pass
            env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
            subprocess.Popen(["python", os.path.abspath(__file__), "--worker", str(nb)],
                             cwd=str(VAULT), env=env,
                             creationflags=_NO_WIN, **_SILENT)
            log(f"respawned orphan batch {nb.name}")
        # resume workers journal to RESUME_DIR the same way; they heartbeat their
        # journal's mtime every minute while alive, so >30 min stale = truly dead
        # (the BELT to reap_dead_lanes' braces. That reaper catches a corpse in ~25s and
        # classifies it; this 30-min sweep exists for the case where the reaper itself is
        # dead. Keep both. A `.parked.json` is a task the troubleshoot loop gave up on and
        # handed to Atul- respawning it would undo the park.)
        if RESUME_DIR.exists():
            for rf in RESUME_DIR.glob("*.json"):
                if rf.name.endswith((".failed.json", ".parked.json")) or now - rf.stat().st_mtime < 1800:
                    continue
                if rf.stem.count(".retry") >= 8:
                    log(f"resume worker failed {rf.stem.count('.retry')}x- shelved: {rf.name}")
                    rf.rename(rf.with_name(rf.stem + ".failed.json"))
                    continue
                nb = rf.with_name(rf.stem + ".retry.json")
                rf.rename(nb)
                _spawn_resume(nb)
                log(f"respawned resume worker {nb.name}")
    except Exception as e:
        log(f"orphan sweep failed: {e}")

# ---- BUILD START/STOP CONFIRMATIONS (Atul, 8th July 23:19 + 23:26) ----
# Builds used to speak because they ran inside a live channel session. Detached resume
# workers only log() to a file, so both ends went silent. Every build now confirms itself
# at BOTH ends- start, landing, failure- in #general and the activity-log, naming ITS OWN
# build so two lanes never blur. Vital-class: baxter_say carries no usage gate, so these
# fire above 90% as they must. The aggregate pause-lift ping below stays as gated as it
# was- a governor alert is not a landing.

def _build_label(entry):
    """The queued task's first clause, as a human would say it- never the evidence tail."""
    try:
        t = " ".join(str(entry.get("task", "") or "").split())
        t = re.split(r"\s*[(:]|\.\s", t, maxsplit=1)[0].strip(" -.:;")
        t = re.sub(r"^(?:Build|Builds|Wire|Stand up)\s+", "", t, flags=re.I)
        if len(t) > 110:
            t = t[:110].rsplit(" ", 1)[0] + "…"
        return t or "an unnamed build"
    except Exception:
        return "an unnamed build"

def _lead(label):
    """The label opening a sentence- 'bridge up/down pings' -> 'Bridge up/down pings'."""
    return label[:1].upper() + label[1:]

def _read_journal(rf):
    try:
        return json.loads(Path(rf).read_text(encoding="utf-8-sig"))
    except Exception:
        return None

def _journal_set(rf, **kv):
    """Stamp flags onto a live journal- one post per build per attempt, the pid the lane
    reaper judges liveness by, the failure a repair worker reads. Written via a temp file
    + atomic replace: lane_journals() globs this directory constantly, and a half-written
    journal reads as an unreadable lane."""
    try:
        rf = Path(rf)
        d = json.loads(rf.read_text(encoding="utf-8-sig"))
        d.update(kv)
        tmp = rf.with_suffix(".flagtmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(rf))
    except Exception as e:
        log(f"journal flag write failed on {Path(rf).name}: {e}")

def _lane_no(lane):
    """Lanes read as 1 and 2 on every surface (Atul, 9th July); internals stay 0-indexed.
    Mirrors _gov.lane_label, but survives a governor that failed to import- an announcement
    must never die for want of a lane number."""
    if _gov is not None:
        try:
            return _gov.lane_label(lane)
        except Exception:
            pass
    try:
        return int(lane) + 1
    except (TypeError, ValueError):
        return lane if lane not in (None, "") else "?"

def _clashing_lane(entry, lanes):
    """Which LIVE lane held this task off, if any. pick_for_lanes reports the reason but not
    the opponent, and a task can equally be held off by another pick in the same pass- that
    case has no lane, and is logged as such rather than blamed on a lane at random."""
    for rf, le in lanes:
        if le and _gov.clash(_gov.touch_of(entry), _gov.touch_of(le)):
            return _gov._lane_id(rf)
    return None

def _record_reject(entry, reason, lane=None, kind="clash"):
    """Mirror a guard rejection into the guard's own log. Never let it break the pump-
    a governor too old to have record_reject must still start builds."""
    try:
        _gov.record_reject(entry, reason, lane=lane, kind=kind)
    except Exception as e:
        log(f"reject log failed: {e}")

def _announce_build(line, logline):
    _say(line, channel="general")
    _say(f"· {logline}", channel="activity-log", mention=False)

def _announce_start(rf, entry):
    """Fired once per ATTEMPT, after the hold/yield aborts- so a start line means claude
    is genuinely running. Keyed on the journal's own name: the sweep renames on respawn
    (`.retry.json`), so a retry says so rather than posing as a fresh start."""
    rf = Path(rf)
    if entry.get("start_announced") == rf.name:
        return
    label, lane = _build_label(entry), _lane_no(entry.get("lane", "?"))
    again = ".retry" in rf.stem or ".repair" in rf.stem
    _announce_build(f"{'Resuming' if again else 'Starting'} {label}, sir- lane {lane}.",
                    f"Build {'resumed' if again else 'started'}- {label}")
    _journal_set(rf, start_announced=rf.name)
    entry["start_announced"] = rf.name

def _requeued(task):
    """A builder that halts itself at a gate check exits CLEANLY, having re-queued its
    own task text (`--halt` -> enqueue, same key). That is a stop, not a landing- and
    calling it a landing would be a lie."""
    if not _gov or not task:
        return False
    try:
        return any(e.get("task") == task for e in _gov.queue_read())
    except Exception:
        return False

def _announce_stop(rf, entry, ok, err="", verdict="", detail=""):
    """Fired at the journal's retire point- the one choke every build passes through.
    Re-reads the journal first: the builder writes its own landing line into `announce`
    (via `--announce`), and a derived fallback beats silence when it forgets.

    A landing NEVER claims more than the verify gate proved. `unverified` is said out
    loud rather than dressed up as done- Atul's own carve-out: say so plainly rather
    than implying it is proven."""
    rf = Path(rf)
    entry = _read_journal(rf) or entry
    if entry.get("announced") == rf.name:
        return
    label = _build_label(entry)
    if not ok:
        _announce_build(f"{_lead(label)} failed, sir- {' '.join(str(err).split())[:140]}. Kept for retry.",
                        f"Build failed- {label}")
    elif _requeued(entry.get("task", "")):
        _announce_build(f"Paused {label}, sir- lane {_lane_no(entry.get('lane', '?'))} freed, back on it "
                        f"when the curve and a lane reopen.", f"Build paused- {label}")
    else:
        claimed = " ".join(str(entry.get("announce") or "").split())
        if verdict == "unverified":
            # NOT "built and live, sir. Unverified-". That led with a completion claim the
            # gate never proved and then took it back in the next clause, which is how a
            # skimmed line reads as a landing. The truth goes first. The builder's own
            # sentence is a CLAIM, so it rides behind the verdict, never in front of it.
            line = f"{_lead(label)} is in, sir, but UNPROVEN- no check was declared for it."
            if claimed:
                line += f" The builder's own account: {claimed}"
        else:
            line = claimed or f"{_lead(label)}- built and live, sir."
            if verdict == "passed":
                line += " Verified."
        _announce_build(line, f"Build landed ({verdict or 'unverified'})- {label}")
    _journal_set(rf, announced=rf.name)

# ---- THE VERIFY + TROUBLESHOOT LOOP (Atul, 9th July 01:37- the overnight order) ----
# "automatically checking and confirming all your work where possible then auto trouble
# shoot failures and pass successes." Before this, a lane reported done because the worker
# SAID done, a failure was re-queued blind and unchanged, and a success passed nothing on.
# The three legs below hang off the lane's exit; baxter_verify holds the judgement.

def _stored_echo(rf, kind):
    """What the JOURNAL now holds for this check, read back off disk.

    `--verify-cmd` used to echo the builder's own pre-shell argv string, so a builder was
    shown quotes that never reached the journal- PowerShell 5.1 had already eaten them. A
    confirmation of a value you never stored is a lie, and it cost the top-hat build both
    its repair attempts on 9th July. Echo what is THERE, or say nothing.

    The echo is FLATTENED, separator and all, and only the echo. A multi-line `python -c`
    exam is stored with its real newlines (that is the whole point of the storage fix), but
    this string is printed at the builder and tailed into `.baxter.log`, and a confirmation
    that spans lines splits the record in two."""
    want = "cmd" if kind == "cmd" else "claim"
    try:
        entry = _read_journal(rf) or {}
        extras = [r.get("value") for r in (entry.get("verify_extra") or [])
                  if isinstance(r, dict) and r.get("kind") == want and r.get("value")]
        stored = extras[-1] if extras else str(
            entry.get("verify" if kind == "cmd" else "verify_assert") or "")
    except Exception as e:
        return f" | could not read the journal back to show you what was stored: {e}"
    return (" | stored as: " + " ".join(str(stored).split())) if stored else ""

def _record_check(rf, kind, value):
    """A builder declaring a check, through the planner's seal. Returns the line printed
    back to it. With no orch module the old behaviour stands: the builder's own word is
    the exam, which is precisely the hole the seal closes- so this fallback is a
    degradation, not a design.

    A `cmd` is VETTED before a byte is written, with `paths_must_exist=True`- here and
    nowhere else. A builder declares at its exit, so the script it names is on disk by
    then, unlike the planner, which seals before the build has written a thing. A command
    that can never run is not recorded AT ALL: recording it grades a correct build FAILED,
    which is exactly what it did on 9th July.

    NEWLINES SURVIVE. This used to flatten the value first, so a builder's multi-line
    `python -c` exam reached the vet as one line, was refused as uncompilable, and (before
    the vet existed) reached the gate as a guaranteed SyntaxError. Strip the ends; leave the
    middle exactly as written, and flatten only where it is DISPLAYED (`_stored_echo`)."""
    value = str(value).strip()
    if kind == "cmd" and value and _bv is not None:
        vetted, why = _bv.vet_verify_cmd(value, paths_must_exist=True)
        if vetted is None:
            # Into the VERIFIER's own log, never `.baxter.log`- that one is the build record
            # Atul reads, and a refusal is a gate decision, filed beside every other one.
            _bv._log(f"REFUSED a verify command on {Path(rf).name}- {why}: "
                     f"{' '.join(value.split())[:120]}")
            return (f"REFUSED- your verify command was NOT recorded: {why}\n"
                    f"Nothing was written to the journal. Declare one that can run, or this "
                    f"build lands UNVERIFIED.")
        value = vetted
    if _orch is not None:
        try:
            # orch vets again (it must: this path is skipped when `_bv` failed to import) and
            # can refuse in its own right. A refusal stored NOTHING, so there is nothing to
            # echo back- appending `_stored_echo` would show the builder the LAST check it
            # declared and read as a confirmation of the one just thrown out.
            msg = _orch.record_check(rf, kind, value)[1]
            return msg if msg.startswith("REFUSED-") else msg + _stored_echo(rf, kind)
        except Exception as e:
            log(f"seal-aware record_check failed ({e})- falling back to a raw journal write")
    _journal_set(rf, **({"verify": value} if kind == "cmd" else {"verify_assert": value}))
    return ("verify command recorded- it runs at your lane's exit and overrules your claim"
            if kind == "cmd" else
            "verify claim recorded- a separate checker will try to disprove it at your lane's exit"
            ) + _stored_echo(rf, kind)

# ---- THE INNOCENT BUILD (10th July 00:28:45) ---------------------------------------
# The gate read an exit code and nothing else, so it could not tell a broken build from a
# build whose exam asserted on a surface a LIVE sibling lane was in the middle of moving.
# Lane 9 changed `position_line()` from 'position N of M, pP' to 'position N of M pending
# (R running, B benched), pP'. POSITION_RE went red inside `baxter_queue_ack_selftest`, and
# the ack-audit build- whose own change was correct and load-bearing- was graded FAILED and
# sent to repair, burning one of its three attempts on a defect it never had.
#
# So a red exam asks one further question before it condemns: is any BREATHING lane holding
# the surface I just asserted on? If one is, the run says something about that lane and
# nothing yet about this build. It is classified 'collided', logged to the guard's rejects
# log, and re-graded once the owner retires. Only then can a red exam cost a repair attempt.
#
# 'collided' is a CLASSIFICATION, never a verdict: `_verify_gate` returns passed | failed |
# unverified and nothing else. See the contract note at the foot of the gate.
def _collide_knob(name, default):
    """A collision knob, overridable by env at import. Both names stay module-level and
    plainly spelled- the sealed exam patches them directly to run in seconds."""
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else default


COLLIDE_WAIT_S = _collide_knob("BAXTER_COLLIDE_WAIT_S", 1200)   # longest hold for an owner
COLLIDE_POLL_S = _collide_knob("BAXTER_COLLIDE_POLL_S", 10)     # how often it looks


def _exam_cmds(entry):
    """Every shell command this build is graded by: the sealed exam, then any extra the
    builder declared on top. `verify_extra` is a LIST of {kind, value} rows, never a string."""
    cmds = [str(entry.get("verify") or "")]
    for r in (entry.get("verify_extra") or []):
        if isinstance(r, dict) and r.get("kind") == "cmd" and r.get("value"):
            cmds.append(str(r["value"]))
        elif isinstance(r, str) and r.strip():
            cmds.append(r)
    return [c for c in cmds if c.strip()]


def _exam_files(entry):
    """The .py scripts an exam command names, absolute and existing. Never raises.

    Split on whitespace alone and `python "C:\\...\\Python Scripts\\utils\\x.py"` shatters at
    its space, so every `.py` is matched where it ENDS and the candidate is walked leftwards
    to the first start that is a real file on disk- the longest reading wins."""
    out = []
    try:
        for c in _exam_cmds(entry):
            for m in re.finditer(r"\.py\b", c):
                end = m.end()
                starts = [0] + [i + 1 for i, ch in enumerate(c[:end]) if ch in " \t\"'"]
                for s in starts:                       # ascending: leftmost = longest
                    cand = c[s:end].strip("\"'")
                    if cand and os.path.isfile(cand):
                        out.append(os.path.abspath(cand))
                        break
    except Exception:
        return []
    return sorted(set(out))


def _exam_surfaces(entry):
    """The files an exam's verdict actually depends on, as touch strings `_gov.clash()` reads.

    Each is WHOLE-FILE, so a sibling's region declaration of the same file clashes by the
    containment rule and a sibling's declaration of a different file does not.

    The walk is bounded to DEPTH 1- the exam's own top-level imports, resolved to
    `<Python Scripts>/utils/<name>.py` when such a file exists. Follow the transitive graph
    and every exam ends up owning every hub file, every red build finds an owner among ten
    live lanes, and the gate stops failing anything at all.

    An inline `python -c "..."` exam names no file whatsoever, and that is the shape nearly
    every sealed exam takes- so its source is read out of the command itself. Without that
    this guard would be dead code on the very case that motivated it."""
    import ast
    files = _exam_files(entry)
    srcs = []
    for f in files:
        try:
            srcs.append(Path(f).read_text(encoding="utf-8-sig", errors="replace"))
        except Exception:
            pass
    if _bv is not None:
        for c in _exam_cmds(entry):
            try:
                s = _bv.python_c_source(c)
            except Exception:
                s = None
            if s:
                srcs.append(s)
    mods = set()
    for s in srcs:
        try:
            tree = ast.parse(s)
        except Exception:
            continue                      # an exam we cannot parse blames nobody
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
                mods.add(n.module.split(".")[0])
    udir = Path(os.path.dirname(os.path.abspath(__file__)))
    surfaces = set(files)
    for m in mods:
        p = udir / f"{m}.py"
        if p.is_file():
            surfaces.add(str(p))
    # Widen through the hub import closure- and ONLY the hub closure. A hub among the
    # exam's depth-1 surfaces drags in what IT imports, so a depth-2 module like
    # baxter_send_dedup (baxter_slash:68 / baxter_fast:18 both import it) becomes a
    # surface too. Without this a sibling mid-write on that module kills the exam before
    # assertion one, _collision_owner finds no owner, and the innocent build is condemned.
    # Bounded to the five hub files reached at depth 1: walking the exam's FULL closure
    # would make every exam own every hub and the gate would fail nothing (see above).
    # Parse-tolerant per file- a mid-write module narrows the set, never aborts the grade;
    # a seen-set terminates cycles (rules -> slash -> rules).
    hubs = {"baxter_triage.py", "baxter_usage.py", "baxter_watch.ps1",
            "baxter_fast.py", "baxter_slash.py"}
    stack = [s for s in list(surfaces) if os.path.basename(s) in hubs]
    seen = set(stack)
    while stack:
        cur = stack.pop()
        try:
            tree = ast.parse(Path(cur).read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError):
            continue                       # a sibling mid-write: narrow, never fatal
        deps = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    deps.add(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
                deps.add(n.module.split(".")[0])
        for d in deps:
            q = udir / f"{d}.py"
            if q.is_file():
                qs = str(q)
                surfaces.add(qs)
                if qs not in seen:
                    seen.add(qs)
                    stack.append(qs)
    return sorted(surfaces)


def _collision_owner(rf, entry):
    """The live lane holding a surface this exam is graded on: (journal, task, why) or None.

    Three ways this could excuse every red build, all shut here:
    - AN EMPTY SURFACE SET. `clash()` reads an empty side as 'undeclared- runs solo' and
      returns a reason, so an exam naming nothing would collide with every lane alive.
    - OUR OWN JOURNAL. It sits in RESUME_DIR and this gate runs before it is unlinked, so a
      build not excluded by RESOLVED path collides with itself and waits out the whole window.
    - AN UNREADABLE SIBLING. Caught mid-write, it has no readable touch-set, and an undeclared
      touch-set clashes with everything. Skipped: it must not excuse a red build either."""
    if _gov is None:
        return None
    try:
        surfaces = _exam_surfaces(entry)
        if not surfaces:
            return None
        me = Path(rf).resolve()
        for journal, e in _gov.lane_journals(alive_only=True):
            try:
                if Path(journal).resolve() == me:
                    continue
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            theirs = list(e.get("touch_set") or []) + list(e.get("live_touch") or [])
            if not theirs:
                continue
            why = _gov.clash(surfaces, theirs)
            if why:
                return Path(journal), str(e.get("task") or "?"), why
    except Exception as ex:
        log(f"the collision check failed ({ex})- the exam's verdict stands unaltered")
    return None


def _await_lane_retire(journal):
    """Poll until the owning lane retires. True if it did, False on timeout.

    Our own heartbeat thread keeps our journal warm throughout, so the reaper cannot mistake
    a waiting lane for a corpse. The wait is bounded because an owner that hangs must not hang
    the gate with it: on timeout the red verdict stands exactly as it did before this existed."""
    journal = Path(journal)
    deadline = time.time() + float(COLLIDE_WAIT_S)
    while True:
        if not journal.exists():
            return True
        try:
            if not _gov.lane_alive(journal, _read_journal(journal)):
                return True
        except Exception:
            pass
        left = deadline - time.time()
        if left <= 0:
            return not journal.exists()
        time.sleep(min(float(COLLIDE_POLL_S), left))


# THE LOG LINE IS THE REPAIR WORKER'S FIRST SIGHT OF A FAILURE, and on d44c66bd it was its
# only one. `detail[:120]` spent that budget on the head of a 170-char command echo: the exit
# code sits AFTER the echo, so it never survived, and a long assertion lost its tail. Three
# workers flew blind and the task was parked. The command is already on disk in
# `entry["verify"]`; the evidence is nowhere else. So the echo is cut to a landmark, and the
# evidence is kept whole.
_GATE_LOG_CAP = 300       # the evidence budget for one line of .baxter.log
_GATE_CMD_KEEP = 60       # a command echo is a landmark, not evidence
_GATE_TAIL_KEEP = 40      # ...and `exited <rc>` TRAILS the echo, so a tail-cut evicts it


def _gate_log_evidence(detail, cap=_GATE_LOG_CAP, keep=_GATE_CMD_KEEP):
    """One bounded, single-line summary of a verdict's evidence, for `.baxter.log`.

    A backtick-quoted command echo is truncated to a landmark; everything else- the exception
    line, the exit code- is kept whole. Over budget the MIDDLE is elided, never the tail: the
    exit code is the one thing the worker came for and it trails the echo.

    Bounded on purpose. Dumping the raw 4000-char failure tail here would answer the blindness
    by drowning the log, and `last_tail` on the journal already carries the full traceback.
    """
    text = " ".join(str(detail or "").split())      # one line, always- never spill a traceback
    if not text:
        return "(the gate recorded no detail)"

    def _shrink(m):     # a repl FUNCTION: its return is literal, so backslashes stay put
        cmd = m.group(1)
        return m.group(0) if len(cmd) <= keep else f"`{cmd[:keep]}...`"

    text = re.sub(r"`([^`]+)`", _shrink, text)
    if len(text) <= cap:
        return text
    return f"{text[:cap - _GATE_TAIL_KEEP - 7]} [...] {text[-_GATE_TAIL_KEEP:]}"


def _verify_gate(rf, entry):
    """LEG 1. Prove the build before calling it done. The builder is the least reliable
    witness to its own success, so the check is a separate process (a shell command, or a
    fresh checker spawn) and its verdict overrides whatever the builder claimed. Re-reads
    the journal because the builder may have declared its own verify mid-build.

    THE SEALED EXAM runs first- written by the planner before the executor existed. Any
    EXTRA checks the builder added on top must also pass: a check the builder itself asked
    for and which then fails is a failing build, and believing the failure is the safe
    direction. Extras can only ever turn a pass into a fail, never the reverse.
    Returns (verdict, detail, entry)."""
    entry = _read_journal(rf) or entry
    # A LANE HOLDS A REGION WHILE IT EDITS IT, NOT FOR THE LIFE OF THE BUILD (10th July).
    # The worker has exited by the time this gate runs, so whatever it grew into mid-flight is
    # finished work: give the regions back before judging. Skip this and a lane clashes with its
    # own corpse- the runner process keeps the journal alive, so `live_touch` reads as a live
    # claim, and the hub-fence build's own exam was refused the very region the build had just
    # edited. Never fatal: a release that fails costs a sibling some parallelism, and turning a
    # good build red over it would be the worse trade.
    if _gov is not None:
        try:
            _held = list(entry.get("live_touch") or [])
            if _held:
                _gov.lane_touch_release(rf, _held)
                entry = _read_journal(rf) or entry
        except Exception as _e:
            log(f"lane_touch_release on {Path(rf).name} failed, regions stay held: {_e}")
    if _bv is None:
        return "unverified", "the verifier module would not import", entry
    # Judge with the code on DISK, not the copy bound when this worker spawned. A hub fix
    # that landed mid-build is otherwise invisible to it, and the gate hands back a FAILED
    # the build never earned. See _reload_bv.
    _reload_bv()

    def _grade(e):
        """One full grading pass: the sealed exam, then the builder's extras on top.

        Returns (verdict, detail, out_tail). `out_tail` is the exam's RAW stdout+stderr tail,
        newlines intact- the evidence a repair worker reads. `detail` is the one-line summary.
        """
        v, d, out = _bv.run_verify_ex(e)
        if v != "failed" and _orch is not None:
            try:
                # ...and so is the runner of the builder's extra checks. Reloaded INSIDE the try,
                # so a module caught mid-save on disk lands on the 'extra checks could not run'
                # path rather than killing the lane, and strictly AFTER _reload_bv: baxter_orch
                # imports baxter_verify at module scope.
                _reload_orch()
                ok, extra = _orch.run_extras(e)
                if not ok:
                    # The evidence must match the verdict. An extra check turned this red, so
                    # the sealed exam's own (passing) output is not what failed- carry the
                    # extra's output instead, or a repair worker reads the wrong tail.
                    v, d, out = "failed", extra, _bv.failure_tail(extra)
                elif extra:
                    d = f"{d}; {extra}"
            except Exception as ex:
                log(f"extra checks could not run ({ex})- the sealed verdict stands")
        return v, d, out

    verdict, detail, out_tail = _grade(entry)
    # A RED EXAM IS NOT YET A RED BUILD- see the collision note above `_collide_knob`. Guarded
    # by `collide_rerun` so one task waits at most once, ever: a repair worker re-enters this
    # gate on the same journal, and three twenty-minute waits would hold a lane for an hour.
    if verdict == "failed" and not entry.get("collide_rerun"):
        owner = _collision_owner(rf, entry)
        if owner:
            ojournal, otask, why = owner
            log(f"verify COLLIDED on '{str(entry.get('task', '?'))[:50]}': {why}. "
                f"'{otask[:50]}' is live and holds that surface- holding the verdict up to "
                f"{int(COLLIDE_WAIT_S)}s for it to retire.")
            try:
                _bv.record(entry, "collided", f"{why}; owner: {otask[:80]}",
                           lane=entry.get("lane"))
            except Exception as e:
                log(f"the collision could not be recorded in the ledger ({e})")
            if _gov is not None:
                try:
                    # The guard keeps its own log, and a collision hold belongs beside every
                    # other thing it has held back. Atul reads it back with `--rejects`.
                    _gov.record_reject(entry.get("task", ""),
                                       f"verify collided with '{otask[:60]}'- {why}",
                                       lane=entry.get("lane"), kind="collide")
                except Exception as e:
                    log(f"the collision could not reach the rejects log ({e})")
            if _await_lane_retire(ojournal):
                _journal_set(rf, collide_rerun=True)
                entry = _read_journal(rf) or entry
                verdict, detail, out_tail = _grade(entry)
                detail = f"re-graded once '{otask[:40]}' retired ({why}); {detail}"
            else:
                log(f"'{otask[:50]}' never retired within {int(COLLIDE_WAIT_S)}s- the red "
                    f"verdict stands and the build goes to repair, as it would have before.")
                detail = (f"{detail} | a live lane also holds this surface ({why}) and never "
                          f"retired, so the failure may not be this build's")
    # THE CONTRACT: passed | failed | unverified, and nothing else. `_resume_worker` special-
    # cases only 'failed', so a fourth verdict string would sail straight past it, be announced
    # as a landing and have its journal unlinked- a false green on a build that never passed.
    # 'collided' lives in the log, the outcome ledger and `.baxter_rejects.jsonl` alone.
    # PERSIST THE EVIDENCE HERE, not two functions downstream. If the lane dies between this
    # line and _handle_failure, the traceback is still on disk for whoever picks the journal up.
    # The command is already in entry["verify"]; what was missing was ever the output.
    if verdict == "failed" and out_tail and Path(rf).exists():
        _journal_set(rf, last_tail=out_tail)
        entry = _read_journal(rf) or entry
    log(f"verify gate on '{str(entry.get('task', '?'))[:50]}': {verdict}- "
        f"{_gate_log_evidence(detail)}")
    return verdict, detail, entry

def _park(rf, entry, diagnosis, why=""):
    """A task the loop cannot fix. It ANNOUNCES itself- overnight, a silently parked queue
    looks identical to a drained one in the morning- and re-enters the queue gated on Atul
    with its diagnosis, so `--ungate` is all it takes to run it again once he has looked."""
    rf, label = Path(rf), _build_label(entry)
    if _bv:
        _bv.record(entry, "parked", diagnosis, lane=entry.get("lane"))
    if _gov:
        try:
            # `vet=False` on the same terms as halt(): this re-states a touch-set the queue
            # already accepted. A legacy entry with a coarse declaration must not be refused
            # re-entry here- the except below would swallow it and the build would be gone.
            _gov.enqueue(entry.get("task", ""), entry.get("next_step", ""),
                         entry.get("note_path", ""),
                         state_summary=f"PARKED by the verify loop- {diagnosis}. {why}".strip(),
                         priority=getattr(_gov, "PRIO_RESUME", 2),
                         touch_set=entry.get("touch_set") or None, gated_on="atul",
                         verify=entry.get("verify"), verify_assert=entry.get("verify_assert"),
                         vet=False)
        except Exception as e:
            log(f"park re-queue failed: {e}")
    _announce_build(f"Parked {label}, sir- {diagnosis}. {why or 'It needs you.'} "
                    f"Say go and I'll run it again.", f"Build parked- {label}")
    _journal_set(rf, parked_at=datetime.now().isoformat(timespec="seconds"))
    try:
        rf.rename(rf.with_name(rf.stem + ".parked.json"))
    except Exception as e:
        log(f"park rename failed on {rf.name}: {e}")
    if _gov:
        try: _gov.yield_marker(rf).unlink(missing_ok=True)
        except Exception: pass
    log(f"PARKED '{str(entry.get('task', '?'))[:60]}'- {diagnosis}")

# ---- THE GOVERNOR'S KILL, MADE SELF-IDENTIFYING (9th July) --------------------
# Stop-Process -Force yields exit -1 (4294967295) and an empty log tail: byte-for-byte a
# real crash. On 9th July the 80% band crossing hard-stopped four healthy lanes and the
# classifier filed all four as 'transient- bare exit 4294967295', spawning four
# diagnose-and-repair workers to re-diagnose four builds that were never broken. Each
# crossing burnt a retry AND a repair attempt per live lane, and MAX_REPAIRS is 3- so a
# sound task parks as FAILED after three of them.
#
# The classifier cannot infer this: nothing in the exit code or the tail distinguishes a
# deliberate kill from a segfault. So the killer says so. Invoke-UsageEnforce stamps each
# victim's journal immediately BEFORE Stop-Process, and _handle_failure reads the stamp
# ahead of the exit code.
HOLD_WINDOW_S = 1800      # a stamp older than this is stale- see _governor_hold
_HOLD_SKEW_S = 120        # tolerated clock skew on a stamp from the future

def _ppid_map():
    """pid -> ppid for every live process, via Toolhelp32. psutil is not installed on this
    box and `wmic` is deprecated + slow; the governor's kill loop cannot wait on either.
    Returns {} on any failure- the caller then matches pids directly, which is the case
    that holds today (claude.exe's parent IS the python resume worker)."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)   # TH32CS_SNAPPROCESS
        if snap == -1:
            return {}
        try:
            e = PROCESSENTRY32(); e.dwSize = ctypes.sizeof(PROCESSENTRY32)
            out = {}
            ok = k32.Process32First(snap, ctypes.byref(e))
            while ok:
                out[int(e.th32ProcessID)] = int(e.th32ParentProcessID)
                ok = k32.Process32Next(snap, ctypes.byref(e))
            return out
        finally:
            k32.CloseHandle(snap)
    except Exception as ex:
        log(f"governor stamp: could not read the process tree ({ex})- matching pids directly")
        return {}

def _governor_yield_marker(rf):
    """Drop the lane's `.yield` sidecar, naming the governor as the source. The build's own
    `--check project --lane` gate reads this on its next phase boundary and halts CLEANLY
    via --halt: mid-flight state saved, code not left half-written. That is what the
    contract's '80-90%: big tasks pause into the queue' has always meant.

    Idempotent by design. The watcher re-stamps every beat (~5-15s) while the band is over,
    and a marker rewritten under a build that is mid-read is a torn file, not a stop signal.
    Existing marker -> leave it exactly as it is, and say so with the return value so the
    caller logs the yield ONCE per lane rather than every beat.
    Returns True only when THIS call created the marker."""
    if _gov is None:
        log("governor yield: no governor import- cannot drop a yield marker, "
            "the band will fall back to the hard kill")
        return False
    try:
        m = _gov.yield_marker(rf)
        if m.exists():
            return False
        _gov._write_atomic(m, {
            "reason": "governor: usage >=80%- big tasks pause into the queue",
            "by": "governor",
            "against": "usage band",
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        return True
    except Exception as ex:
        log(f"governor yield: marker write failed on {Path(rf).name} ({ex})- "
            f"this lane will be hard-killed instead of pausing")
        return False

def _stamp_governor_kill(pids, level="big"):
    """`--stamp-governor-kill`. The governor is about to pause (big) or Stop-Process
    (routine) these pids. Mark the journal of every lane they belong to, so the failure that
    follows identifies itself.

    At the 'big' band we ALSO drop each lane's yield marker, which turns the band edge from
    a kill into a request to stand down. At 'routine' (90%+, vitals-only) we do NOT: that
    wall has to bite immediately, and a grace window there would let usage run away at the
    exact moment headroom is scarcest.

    We walk each pid and up to 3 ancestors: today the watcher hands us claude.exe's
    ParentProcessId, which IS the python resume worker whose pid the journal carries. If
    claude ever gains a launcher shim that identity breaks silently- and a silent miss here
    is the exact bug this build fixes- so we climb rather than trust one hop.
    Returns the number of journals stamped."""
    want = set()
    tree = _ppid_map()
    for p in pids:
        try: p = int(p)
        except Exception: continue
        for _ in range(4):          # the pid itself + up to 3 ancestors
            if p <= 0 or p in want:
                break
            want.add(p)
            p = tree.get(p, 0)
    if not want:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    lvl = str(level or "big").strip().lower() or "big"
    n = 0
    try:
        journals = sorted(RESUME_DIR.glob("resume-*.json"))
    except Exception as ex:
        log(f"governor stamp: cannot read {RESUME_DIR} ({ex})")
        return 0
    for rf in journals:
        e = _read_journal(rf)
        if not e:
            continue
        try: jp = int(e.get("pid") or 0)
        except Exception: jp = 0
        if jp and jp in want:
            _journal_set(rf, halted_by="governor", halted_at=now, halted_level=lvl)
            n += 1
            if lvl != "big":
                log(f"governor stamp ({lvl}): lane {_lane_no(e.get('lane', '?'))} pid {jp} "
                    f"marked HELD before the kill ({rf.name})")
            elif _governor_yield_marker(rf):
                log(f"governor stamp (big): lane {_lane_no(e.get('lane', '?'))} pid {jp} "
                    f"marked HELD and asked to stand down ({rf.name})")
    if not n:
        log(f"governor stamp ({lvl}): no live journal matched pids {sorted(want)}- "
            f"the kill will classify as a crash")
    return n

def _governor_hold(entry):
    """Was this journal stamped by the governor, recently enough that the stamp describes
    THIS failure? A stamp that outlives its hold is a mask over the next real crash- the
    lane would be 'held' forever and never get a repair worker. So it expires."""
    if not entry or str(entry.get("halted_by") or "") != "governor":
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(str(entry.get("halted_at")))).total_seconds()
    except Exception:
        return False                       # unparseable stamp -> classify it normally
    return -_HOLD_SKEW_S <= age <= HOLD_WINDOW_S

def _hold_for_governor(rf, entry, rc, source):
    """The build was not broken- it was hard-stopped. Re-queue it at resume priority with
    its retry and repair counters INTACT, free the lane, and never spawn a repair worker.
    Silent by design: four lanes die on one band crossing, and the governor already pings
    the crossing itself ([[no-ping-storms]])."""
    label = _build_label(entry)
    lane = _lane_no(entry.get("lane", "?"))
    tr = int(entry.get("transient_retries", 0) or 0)
    ra = int(entry.get("repair_attempts", 0) or 0)
    log(f"lane {lane} HELD by governor ({source}, exit {rc}, {entry.get('halted_level', 'big')} band)- "
        f"resumes when the band clears; retries {tr}, repairs {ra} untouched")
    if _bv:
        try:
            _bv.record(entry, "held/governor",
                       f"hard-stopped by the governor at the {entry.get('halted_level', 'big')} band "
                       f"-> re-queued at p2, retries {tr}/repairs {ra} untouched",
                       lane=entry.get("lane"))
        except Exception as ex:
            log(f"governor hold: ledger write failed ({ex})")
    if not _gov:
        log("governor hold: no governor import- journal kept in place for the orphan sweep")
        return
    try:
        try:
            _gov.halt(entry.get("task", ""), entry.get("next_step", ""),
                      entry.get("note_path", ""), entry.get("state_summary", ""),
                      touch_set=entry.get("touch_set") or None,
                      transient_retries=tr, repair_attempts=ra)
        except TypeError:
            # A long-lived process can be serving a stale baxter_usage ([[long-lived-process-staleness]]).
            # Rather than lose the counters silently, keep the journal- the orphan sweep
            # respawns it once the band clears, and the counters ride along on the journal.
            log("governor hold: this baxter_usage cannot carry the retry counters- "
                "keeping the journal in place rather than resetting them")
            return
    except Exception as ex:
        log(f"governor hold: re-queue failed ({ex})- journal kept for the orphan sweep")
        return
    try: _gov.yield_marker(rf).unlink(missing_ok=True)
    except Exception: pass
    try: rf.unlink(missing_ok=True)          # counters now live on the queue entry
    except Exception as ex:
        log(f"governor hold: could not free lane {lane} ({ex})")

def _handle_failure(rf, entry, rc, log_text, source="worker"):
    """LEG 2. Classify BEFORE retrying, because a deterministic failure re-queued
    unchanged fails again, forever, and quietly- the overnight case where nobody is
    watching. transient -> one blind retry. deterministic -> a diagnose-and-repair
    worker on the same touch-set. gated/outward -> never auto-repaired, park it.
    Exit code first, log tail second, benign warning lines never (see baxter_verify)."""
    rf = Path(rf)
    # ASK THE GOVERNOR FIRST- before the exit code is ever consulted. Re-read the journal:
    # _resume_worker read `entry` at its own start, minutes before the stamp was written,
    # so the in-memory copy never carries it.
    entry = _read_journal(rf) or entry
    if _governor_hold(entry):
        _hold_for_governor(rf, entry, rc, source)
        return
    if _bv is None:
        _announce_stop(rf, entry, ok=False, err=f"exit {rc}")   # old blind path; the sweep retries
        return
    # DECIDE WITH THE CAPS ON DISK, not the ones bound when this worker spawned. _verify_gate
    # already re-reads its grader, but a worker that EXITS NON-ZERO never reaches the gate- it
    # arrives straight here, holding whatever baxter_verify said hours ago. On 9th July the cap
    # went 2 -> 3 while eight lane leads were mid-build: every one of them would have parked a
    # sound task an attempt early, off a module the disk had already corrected. Same reasoning
    # as _reload_bv's, one path over; it fails open on a module caught mid-save.
    _reload_bv()
    # classify() keeps reading tail() exactly as it did- a CHARACTER window, collapsed. Its
    # transient patterns are now shielded from traceback source echoes inside baxter_verify
    # itself (see _drop_frames), which is what makes a wider log_text safe here.
    tail = _bv.tail(log_text)
    kind, why = _bv.classify(rc, tail, entry)
    action, rationale = _bv.next_action(kind, entry)
    label = _build_label(entry)
    log(f"lane {_lane_no(entry.get('lane', '?'))} FAILED ({source}, exit {rc}): {kind}- {why} -> {action}")
    _bv.record(entry, f"failed/{kind}", f"{why} -> {action}", lane=entry.get("lane"))
    # THE TAIL, NOT THE COMMAND. failure_tail keeps the LAST lines with their newlines, so a
    # traceback's final line- the exception type and message- survives. The old
    # strip_benign(tail(...))[-1500:] took a character window of a string that LED with the
    # verify command echo, and on 10th July that echo ate the whole budget twice over.
    #
    # The verify gate has already written the exam's raw output to last_tail seconds ago and it
    # is longer than the one-line `detail` that reaches us as log_text. Never trade a traceback
    # for a summary of it. Only a gate-written tail is trusted this way: on any other path a
    # longer last_tail is stale evidence from an EARLIER attempt, and must be overwritten.
    _tail_now = _bv.failure_tail(log_text)
    if source == "verify gate":
        _gate_tail = str(entry.get("last_tail") or "")
        if len(_gate_tail) > len(_tail_now):
            _tail_now = _gate_tail
    _journal_set(rf, last_exit=rc, last_fail=f"{kind}: {why}", last_source=source,
                 last_tail=_tail_now,
                 failed_at=datetime.now().isoformat(timespec="seconds"))
    entry = _read_journal(rf) or entry
    if action == "park":
        _park(rf, entry, f"{kind}- {why}", rationale)
        return
    # A failure the loop is still HEALING is machinery, not news, and it does not reach him
    # (Atul, 9th July: "autonomous root-cause troubleshooting... before ever flagging a build
    # failure to Atul"). It lands in .baxter.log and the outcome ledger instead- both already
    # written above. The announce is not deleted, it is DEFERRED: _park() below still speaks
    # up the moment the cap is spent or the task is gated, and a landing still announces. So
    # there is no silent failure state, only a bounded silent one- MAX_REPAIRS attempts long.
    if action == "retry":
        log(f"{label}: {kind}- retrying once, unchanged. Self-healing, not flagged to Atul.")
        _journal_set(rf, transient_retries=int(entry.get("transient_retries", 0) or 0) + 1)
        suffix = ".retry.json"
    else:
        attempt = int(entry.get("repair_attempts", 0) or 0) + 1
        log(f"{label}: repair attempt {attempt} of {_bv.MAX_REPAIRS}- {why}. "
            f"Self-healing, not flagged to Atul.")
        _journal_set(rf, repair_attempts=attempt, repair_pending=True)
        suffix = ".repair.json"
    try:
        nb = rf.with_name(rf.stem + suffix)
        rf.rename(nb)
    except Exception as e:
        log(f"failure rename failed on {rf.name}: {e}- leaving it for the orphan sweep")
        return
    _spawn_resume(nb)

def _pump_now():
    """LEG 4. A lane just retired- start the next queued build immediately rather than
    waiting for the next poll beat. This is what makes the list actually chug."""
    try:
        env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
        subprocess.Popen(["python", os.path.abspath(__file__), "--pump"],
                         cwd=str(VAULT), env=env, creationflags=_NO_WIN, **_SILENT)
    except Exception as e:
        log(f"immediate pump spawn failed: {e}")

def _pump_once():
    """The `--pump` entry point. Takes triage's own single-flight lock so it can never
    race a live cycle into starting the same task twice; if a cycle already holds it,
    that cycle pumps at its end anyway and we quietly stand down."""
    if OFF_FLAG.exists():
        return
    if LOCK.exists():
        try:
            if (datetime.now().timestamp() - LOCK.stat().st_mtime) < 600:
                return
        except Exception:
            pass
    try:
        LOCK.write_text(datetime.now().isoformat())
    except Exception:
        pass
    try:
        state = load_state()
        maybe_resume(state, force=True)
        save_state(state)
    finally:
        try: LOCK.unlink()
        except Exception: pass

def reap_dead_lanes():
    """LEG 3. A dead lane must not report itself alive. `live_lanes()` used to count every
    journal on disk- no process check, no age check- so when the two-lane build died at
    01:37 on 9th July, `--lanes` went on printing "1/1 live" against a journal frozen at
    the moment of death. The 30-minute orphan sweep did clear it, but half an hour is
    forever during an unattended drain: every failure stalls a lane while every readout
    claims it is working, and a verify gate on a lane nobody knows is dead never runs.

    So: detect the corpse in ~25s (its pid is gone), hand the slot straight back to the
    pump, and route it into LEG 2's classifier rather than the blind 30-minute respawn.
    The sweep STAYS as the belt- it catches this reaper itself dying."""
    if not _gov or _bv is None:
        return
    try:
        corpses = _gov.dead_lanes()
    except Exception as e:
        log(f"lane reaper could not read the lanes: {e}")
        return
    # A build the governor HELD leaves an idle journal behind. That is a hold, not a corpse-
    # classifying it would "repair" a task that never ran. This used to be guarded by
    # `if not _usage_ok("project"): return`, which only holds while the band is SHUT: on the
    # first pass after it reopened, five held builds were reaped as silent deaths (13:01, 9th
    # July). The band was never the discriminator. `held` is, and dead_lanes() now applies it
    # regardless of band- so the guard is gone and the hold is merely reported.
    #
    # ABOVE the empty-corpses return, or a held-only pass says nothing at all. Throttled on
    # the SET of held names, not on time: this runs every 15s, and an unchanging hold that
    # logged each pass would bury `.baxter.log` under 240 identical lines an hour.
    try:
        held = sorted(rf.name for rf, _e in _gov.held_lanes())
    except Exception:
        held = []
    if held != getattr(reap_dead_lanes, "_held_seen", None):
        reap_dead_lanes._held_seen = held
        if held:
            log(f"lane reaper: {len(held)} build(s) HELD by the governor, not corpses- "
                f"{', '.join(held)}")
    if not corpses:
        return
    for rf, entry in corpses:
        if entry is None:
            log(f"lane reaper: {rf.name} is unreadable and dead- shelving it")
            try: rf.rename(rf.with_name(rf.stem + ".failed.json"))
            except Exception: pass
            continue
        log(f"lane reaper: lane {_lane_no(entry.get('lane', '?'))} died silently ({rf.name})- classifying")
        _handle_failure(rf, entry, entry.get("last_exit"), entry.get("last_tail", ""), source="ghost lane")


_STUCK_EVERY = 60        # a cpu sample per minute is plenty. See the throttle note below.
_STUCK_DEADLINE = 45.0   # the probe arms faulthandler at this and aborts with a stack...
_STUCK_GRACE = 15.0      # ...and we kill it this long after, if it outlived its own clock.
_STUCK_SD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baxter_stuck_doctor.py")
_SPIN_DUMP = VAULT / ".baxter_spin_dump.txt"


def _spin_dump_note():
    """The probe writes its own stacks there before aborting; a clean pass leaves nothing."""
    try:
        if _SPIN_DUMP.exists() and _SPIN_DUMP.stat().st_size > 0:
            return f"- its stack is in {_SPIN_DUMP.name}"
    except OSError:
        pass
    return ""


def stuck_doctor_tick(dry_run=False, force=False, deadline=_STUCK_DEADLINE, probe_cmd=None):
    """LEG 4 (Atul, 8th July 21:38). A lane past 15 min that has burned no cpu and rewritten
    nothing for a full 10-minute sample is wedged- and only then is baxter_doctor_ai summoned,
    once, detached.

    It runs INSIDE the beat rather than as an eleventh lonely process, immediately after the
    reaper: a corpse must be classified before a stall can be diagnosed, or the doctor is sent
    to examine a body the reaper already carried off.

    FAILS OPEN. Any exception is logged and swallowed. This is a watchdog on the machinery, and
    a watchdog that can stall the machinery it watches is worse than none. It logs TRANSITIONS
    only- ten lanes narrating "watching" every minute would bury `.baxter.log`.

    THE PROBE IS A CHILD, AND THE CHILD IS ON A CLOCK (10th July). It used to be
    `import baxter_stuck_doctor; _sd.pass_once()`- an inline call on the beat's only thread.
    On 10th July a bare `python baxter_triage.py` child pinned one core at 99% for 8h05m
    (28,995s cpu / 29,257s wall) and `baxter_watch.ps1` blocked in `Invoke-TriageWaitLoop`
    waiting for it: no filing, no briefs, and the queue pump never ran, for the whole night.

    An in-process watchdog could not have saved it. A python thread spinning inside a ctypes
    call holds the GIL and takes no signal, so no timer, thread or `except` in THIS process can
    interrupt it. A child process can simply be killed. So the probe is spawned, read back off
    `--json`, and killed by `subprocess.run(timeout=)` if it overruns `deadline + _STUCK_GRACE`.

    The kill reaches the DIRECT CHILD ONLY, deliberately. `baxter_stuck_doctor` spawns
    `baxter_doctor_ai` DETACHED so a 900s LLM fan-out never blocks the beat; killing the probe's
    process tree would abort the very diagnosis the probe was summoned to start.

    The probe also arms `faulthandler.dump_traceback_later(deadline, exit=True)` on itself, so
    it dumps every thread's stack to `.baxter_spin_dump.txt` and aborts BEFORE we lose patience.
    That file is the evidence the 10th-July spin never left: `baxter_watch.ps1` blames this
    probe's process-tree walk, but on process accounting alone, never on a stack. The walk
    measured 6.4ms over 466 processes when re-tested, and did not reproduce.

    FAILS OPEN, and now fails FAST. Any exception, timeout, non-zero exit or unparseable stdout
    is logged and yields `[]`. A watchdog that can stall the machinery it watches is worse than
    none- that is not a maxim here, it is the incident report.

    THE THROTTLE IS A GUARD, NOT THE CADENCE. `baxter_watch.ps1` spawns a FRESH triage process
    per cycle (interval 60s), so `_last` is always 0 here and the throttle never bites; it exists
    for `--stuck-tick` and for any caller that ever loops in-process. The real cadence is the
    beat's, which means this probe is only as timely as triage itself: when the watcher stops
    spawning triage, no lane is sampled. That is what `health_monitor.stuck_stamp_state` watches,
    and on 10th July it was not hypothetical- triage went 9 minutes without a cycle while the
    watcher heartbeated happily.
    """
    now = time.time()
    if not force and (now - getattr(stuck_doctor_tick, "_last", 0.0)) < _STUCK_EVERY:
        return []
    stuck_doctor_tick._last = now

    # `probe_cmd` is a test seam, not a second code path: the DEFAULT is a subprocess too.
    cmd = list(probe_cmd) if probe_cmd else [
        sys.executable, _STUCK_SD, "--dry-run" if dry_run else "--once",
        "--json", "--deadline", str(deadline)]
    hard = float(deadline) + _STUCK_GRACE
    try:
        p = subprocess.run(cmd, cwd=str(VAULT), timeout=hard, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           creationflags=_NO_WIN)
    except subprocess.TimeoutExpired:
        log(f"stuck doctor: the probe timed out after {hard:.0f}s and was killed"
            f"{_spin_dump_note()}")
        return []
    except Exception as e:
        log(f"stuck doctor tick failed: {e}")
        return []

    if p.returncode != 0:
        err = " ".join((p.stderr or "").split())[-300:]
        log(f"stuck doctor: the probe exited {p.returncode} {_spin_dump_note()} {err}".strip())
        return []
    try:
        recs = json.loads(p.stdout or "[]")
        if not isinstance(recs, list):
            raise ValueError(f"expected a list, got {type(recs).__name__}")
    except Exception as e:
        log(f"stuck doctor: the probe emitted unparseable json ({e})")
        return []

    for r in recs:
        if r.get("verdict") != r.get("prev") and r.get("verdict") != "watching":
            log(f"stuck doctor: lane {_lane_no(r.get('lane', '?'))} {r['verdict']}"
                f" ({r['journal']}, cpu={r['cpu']}, anchor_age={r['anchor_age']}"
                f"{', doctor summoned' if r.get('summoned') else ''})")
    return recs

# ---- AUTO-RESUME (5th-July build; his 20:55 order: never say 'get back to work' again) ----
def _spawn_resume(rf):
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
    # A lane, and every python IT spawns, reads its bytecode from a cache that cannot hold a
    # stale entry: writes are off, and reads come from a prefix dir rather than the
    # beside-source __pycache__ another process may have poisoned. A build graded against a
    # hub file's old code is the failure this exists to stop ([[stale-pyc-same-second-same-size]]).
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        _PYCACHE_PREFIX.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    env["PYTHONPYCACHEPREFIX"] = str(_PYCACHE_PREFIX)
    p = subprocess.Popen(["python", os.path.abspath(__file__), "--resume-worker", str(rf)],
                         cwd=str(VAULT), env=env, creationflags=_NO_WIN, **_SILENT)
    # The lane reaper judges liveness by this pid: a lane IS its process. Without it the
    # only signal is the heartbeat, which takes 150s to go cold and lies if the heartbeat
    # thread dies under a build that is still running.
    #
    # This is the ONLY writer of `pid`, so it is the only place a governor hold can end-
    # and the hold is cleared in the SAME write. Two calls would leave a window where a
    # crash between them strands a journal stamped `held` behind a live pid: permanently
    # invisible to the reaper, a lane that leaks and never comes back. Hence the stillborn
    # check below only chooses the VALUE written here; it never writes a journal of its own.
    #
    # A STILLBORN CHILD MUST NOT BE BLESSED AS A LANE (9th July). Popen hands back a pid for
    # a process that may already be dead- a bad interpreter, an import blowing up on the
    # first line. Writing `pid=<dead>, held=False` describes a living lane that never was:
    # it spends a fleet slot, and once LANE_PID_GRACE lapses the reaper finds a corpse and
    # routes a build that NEVER RAN into the failure classifier. `poll()` is free and catches
    # exactly the instant-death case; a child still running here is a real lane, and
    # LANE_PID_GRACE covers the rest of its infancy. Left with no pid the journal reads
    # body-less to `_never_had_a_body()`, so the sweep RESPAWNS it- the correct repair.
    alive = p.poll() is None
    if not alive:
        log(f"lane spawn stillborn on {rf.name} (exit {p.returncode})- journal left "
            f"body-less, the sweep will respawn it")
    _journal_set(rf, pid=(p.pid if alive else None), held=False, held_at=None,
                 spawned_at=datetime.now().isoformat(timespec="seconds"))

def _scope_prompt(rf, entry, lane):
    """THE SCOPE-ONLY LANE'S PROMPT. This pass has no declared touch-set, so its only job is to
    decide what a build of the task WOULD touch- and it edits NOTHING. The writer hook denies
    every Edit/Write for this lane, so there is no point attempting one; read instead, and hand
    the task on. Deliberately short: a scoping read is cheap and must stay that way."""
    task = str(entry.get("task", "")).strip()
    nxt = str(entry.get("next_step", "")).strip()
    note = str(entry.get("note_path", "")).strip()
    return (
        f"You are Baxter build lane {lane}, running a SCOPE-ONLY pass. You may READ anything and "
        f"EDIT NOTHING- every write tool is blocked for this lane by design, so do not attempt one.\n\n"
        f"THE TASK (not to build now- to SCOPE):\n{task}\n"
        + (f"\nWhat a build would do next: {nxt}\n" if nxt else "")
        + (f"Context note on disk: {note}\n" if note else "")
        + f"\nYour ONE job: read the code and work out exactly which files (and, for a hub file "
        f"- {', '.join(sorted(_HUB_FILES)) if '_HUB_FILES' in globals() else 'baxter_usage.py, baxter_triage.py, baxter_watch.ps1, baxter_fast.py, baxter_slash.py'} - which FUNCTION/region) a build of this task would edit. "
        f"Read the failing verify command, the named files, their imports- whatever it takes to be sure.\n\n"
        f"THEN do exactly one of these and STOP:\n"
        f"1. If it is a normal build, re-queue it WITH the touch-set you found:\n"
        f'   python "{USAGE_PY}" --halt "{task}" "{nxt or "build it"}"'
        + (f' --note "{note}"' if note else "")
        + f' --touch "file_a,file_b/region"   (use --solo instead only if it genuinely rewrites a whole hub)\n'
        f"2. If the task cannot be turned into a file-list because it needs a DESIGN first "
        f"(a new feature, a UI, an architecture), say so to Atul in one line via "
        f'python "{BAXTER_SAY}" "<message>" and re-queue it for the PM instead '
        f'(python "{USAGE_PY}" --halt "{task}" "{nxt or "needs a PRD"}" --gate pm).\n'
        f"3. If reading shows the task is already done or moot, say that to Atul and do NOT re-queue.\n\n"
        f"Do NOT build. Do NOT edit. One scoping read, one hand-off, then exit."
    )


def build_worker_prompt(rf="<your journal>", entry=None, lane=1, touch=None, repair=False):
    """THE BUILD LANE'S PROMPT. Module-level, and every parameter defaulted, for two reasons.

    It used to be a 70-line f-string buried inside `_resume_worker`, which meant no guard
    could see it and no test could render it. So it was the one worker prompt in Baxter that
    imported no rules and carried none- and TRUST BUT VERIFY, the rule Atul gave at 00:17 on
    9th July, reached every lane EXCEPT the lane that builds. It types no rule text of its
    own now: `_rules.VERIFY_STEP` is interpolated, exactly as baxter_fast and baxter_slash
    interpolate WORKER_RULES.

    The defaults are what let `baxter_rules.check()` render it on zero arguments every triage
    pass and fail loudly if the rule ever falls out again. They are also why nothing here may
    require an argument: the guard would hand it a dummy, `entry.get` would raise, and the
    guard would report a healthy tree as drifted.
    """
    entry = entry or {}
    touch = touch or []
    if repair:
        attempt = int(entry.get("repair_attempts", 1) or 1)
        # The cap is baxter_verify's to state, and ONLY its to state. A hardcoded fallback
        # lies twice over when _bv failed to import: it invents a number, and it implies
        # something is counting- while the module that would park the task is absent. Say
        # so instead, and the worker treats the attempt as its last.
        _cap = getattr(_bv, "MAX_REPAIRS", None) if _bv is not None else None
        cap_line = (
            f"This is repair attempt {attempt} of {_cap}- after that the task parks and "
            f"waits for him." if _cap else
            f"This is repair attempt {attempt}. The verify module is not loaded, so nothing "
            f"is counting your attempts or able to park you- treat this as your last."
        )
        _gate = entry.get("verify") or entry.get("verify_assert") or ""
        gate_line = (
            "- WHEN YOU HAVE FIXED IT, RE-RUN THE SEALED VERIFY GATE YOURSELF and read its exit "
            "code. It is the very command that judged you, it runs again in a separate process "
            "the moment you exit, and a non-zero exit turns your 'done' straight back into a "
            "FAILED. Claim done ONLY once you have watched it exit 0:\n"
            f"    {_gate}\n" if _gate else
            "- No verify gate is declared on this task, so nothing yet proves a repair. Run your "
            "fix end to end, then declare a gate with --verify-cmd before you exit. An unproven "
            "repair is just the failure, waiting.\n"
        )
        head = (
            f"You are Baxter. Read {CONTRACT} and follow it. A build FAILED and you are the "
            f"diagnose-and-repair worker sent after it, unprompted- do not wait for Atul. Do "
            f"NOT start the task afresh: find what actually broke, fix THAT, then finish the "
            f"build. {cap_line}\n\n"
            f"Atul has NOT been told this build failed, and will not be while you are on it. "
            f"The loop self-heals silently; only a spent cap reaches him. Fix it properly- do "
            f"not paper over it, and never soften a check to make it pass.\n\n"
            f"THE FAILURE:\n"
            f"  classified: {entry.get('last_fail', 'unknown')}\n"
            f"  exit code:  {entry.get('last_exit')}\n"
            f"  log tail (benign warnings already stripped):\n"
            f"{(entry.get('last_tail') or '(nothing- the lane died silently)')}\n\n"
            f"- REPRODUCE the failure before you fix it, and PROVE the fix by running it. A fix "
            f"you have not watched work is a guess.\n"
            f"- 'Sandbox disabled: sandbox is enabled but windows is not supported' is a benign "
            f"Windows warning from settings.json. It is never the cause. Ignore it.\n"
            f"{gate_line}\n"
        )
    else:
        head = (
            f"You are Baxter. Read {CONTRACT} and follow it. A build lane is free and the "
            f"usage curve open- this queued task now RUNS (resumes if partly done), unprompted- "
            f"do not wait for Atul.\n\n"
        )
    # the plan + the sealed acceptance, when a planner produced one (empty string if not)
    try:
        _plan = _orch.plan_block(entry) if _orch is not None else ""
    except Exception:
        _plan = ""
    # ANSWER WHERE HE ASKED (Atul, 9th July 17:38, on a build that answered in the wrong room).
    # This prompt used to call rules.reply_via() nowhere at all, so a finished build had no
    # reply command in front of it and reached for baxter_say's bare default- which is
    # channel_key='general'. He asked the bullet-resist question in #deadlock-research at 15:37
    # and read the answer in #general at 17:35.
    #
    # The line is rendered ONLY for an entry that actually came from a message of his. A build
    # queued by a lane, by autobuild or by the pump has no `source_mid`, nobody is waiting on a
    # reply, and inventing one would post at him unbidden. `source_channel` is the room; absent
    # it, reply_via() omits --channel and baxter_say falls back to #general as it always did-
    # the old behaviour, kept for the legacy entries that carry a message id and nothing else.
    _mid = str(entry.get("source_mid") or "").strip()
    _cid = str(entry.get("source_channel") or "").strip()
    reply_line = (
        "- ANSWER WHERE HE ASKED. This build came from a message of Atul's, in the channel "
        "named below. Reply to THAT message, in THAT channel- not in #general, and not as a "
        "bare post. Your landing announcement is separate and does not answer him.\n"
        "    " + _rules.reply_via(_mid, _cid or None) + "\n"
        if _mid else "")
    return (
        head
        + (_bv.recent_summary() + "\n" if _bv else "")
        + (_plan + "\n" if _plan else "")
        + f"THE TASK (from the big-task queue, .baxter_task_queue.json):\n"
        f"{json.dumps({k: v for k, v in entry.items() if k != 'plan'}, ensure_ascii=False, indent=1)}\n\n"
        f"- Read note_path (and any resume state it references) to see what is already done- "
        f"do NOT redo finished phases. Continue from next_step.\n"
        f"- YOU ARE BUILD LANE {lane}. Your journal: {rf}\n"
        f"  Baxter runs up to {getattr(_gov, 'LANE_COUNT', 10)} big builds at once (lanes 1-{getattr(_gov, 'LANE_COUNT', 10)}, "
        f"never lane 0), kept apart by the clash delegator. Your declared touch-set is "
        f"{touch or '(undeclared- you were scheduled SOLO)'}. A HUB file (baxter_triage.py, "
        f"baxter_usage.py, baxter_watch.ps1, baxter_fast.py, baxter_slash.py) must be declared "
        f"by REGION- 'utils/baxter_triage.py/_claude', not the bare file- or --queue refuses it.\n"
        f"- BUILDER SELF-CHECK. Before you start editing any file or directory OUTSIDE that "
        f"touch-set, declare it first:\n"
        f"    python \"{USAGE_PY}\" --lane-touch \"{rf}\" <path> [<path>...]\n"
        f"  Exit 0 = clear, the paths are registered on your lane. Exit 3 = it collides with "
        f"the other lane's work: do NOT edit those files- halt yourself (below) instead of "
        f"colliding. Never edit a file the other lane owns.\n"
        f"- Never start a second build/research/setup yourself. New big work you discover goes "
        f"into the queue, not into flight- and DECLARE ITS TOUCH-SET so it can share a lane:\n"
        f"  python \"{USAGE_PY}\" --queue \"<task>\" \"<first step>\" [--note <path>] [--priority 1-8] --touch \"utils/baxter_usage.py/ceiling,utils/coc_bot/,@probe\"\n"
        f"  (1 = Atul-says-first, 2 = interrupted resumes, 5 = default, 8 = background). The "
        f"touch-set is MANDATORY: --queue REFUSES an undeclared task (exit 2). Name the REAL "
        f"files this task will edit- the literal '@cluster'/'utils/x.py' placeholders are "
        f"refused too, since a fake declaration gets co-scheduled and then collides. The @tag "
        f"must be one REGISTERED in utils/baxter_clusters.py- an invented or retired tag "
        f"is refused, because it locks against nothing. If you "
        f"genuinely cannot name the files yet, pass --solo and say so- an undeclared task "
        f"clashes with everything and holds the second lane empty behind it.\n"
        f"- Governor + delegator discipline (contract step 29): at EVERY phase boundary- and "
        f"every few tool batches during a heavy burn- run\n"
        f"  python \"{USAGE_PY}\" --check project --lane \"{rf}\"\n"
        f"  Exit code 3 = over the usage curve OR the delegator has told your lane to yield "
        f"(the reason says which). Either way STOP cleanly, record where you got to via\n"
        f"  python \"{USAGE_PY}\" --halt \"<task>\" \"<next step>\" --note \"<note_path>\" --state \"<state summary>\" --touch \"<your touch-set>\"\n"
        f"  then exit- triage restarts you when a lane and the curve allow.\n"
        + _rules.VERIFY_STEP
        + _rules.HUB_EDIT_RULE
        + _rules.WRITER_TOUCH_LANE_RULE
        + f"- DECLARE HOW YOUR WORK IS PROVEN (the verify gate, 9th July). Before you exit, register "
        f"a command that PROVES the build works. It is run in a SEPARATE process after you exit, "
        f"and a non-zero exit turns your 'done' into a FAILED- so make it real:\n"
        f"    python \"{os.path.abspath(__file__)}\" --verify-cmd \"{rf}\" 'python \\\"{os.path.abspath(__file__)}\\\" --selftest'\n"
        f"  THE TWO SHELLS NEED OPPOSITE ESCAPING- quote for the one you actually register from.\n"
        f"    From POWERSHELL: outer SINGLE quotes, inner double quotes BACKSLASH-ESCAPED, exactly "
        f"as above. PowerShell 5.1 re-parses a double-quoted outer string and eats the inner "
        f"quotes, so a path with a space reaches this gate already split and is REFUSED "
        f"(doubling the quotes and the --% token do not save it; the single outer quote does).\n"
        f"    From the BASH tool: outer single quotes, inner double quotes PLAIN- "
        f"'python \"{os.path.abspath(__file__)}\" --selftest'. Bash keeps a backslash LITERAL "
        f"inside single quotes, so the PowerShell form stores `-File \\\"C:\\...` verbatim and the "
        f"exam dies on an illegal path having run zero assertions (9th July: a correct build was "
        f"graded FAILED that way). The gate now REFUSES that shape rather than seal it.\n"
        f"  Swap the illustrative selftest for the command that proves YOUR build.\n"
        f"  AN EXAM THAT CANNOT FAIL IS REFUSED, not recorded: `python -c \"pass\"`, a bare "
        f"`print`, an import with nothing asserted on it. Declare one that can go RED, then "
        f"sabotage your own code and watch it go red before you trust it.\n"
        + _rules.REDPROOF_FENCE_RULE
        + f"  If no command can express it, declare a claim a separate checker will test instead:\n"
        f"    python \"{os.path.abspath(__file__)}\" --verify-assert \"{rf}\" '<what must be observably true>'\n"
        f"  Test BEHAVIOUR, not source text- a selftest, a real run, a grep of the LIVE log. On 9th "
        f"July a file-level check passed green while the running listener still served the old code. "
        + ("Your acceptance test is already SEALED (see the plan above): what you declare here is an "
           "ADDITIONAL check that must ALSO pass, and it cannot replace the sealed one.\n"
           if entry.get("acceptance_sealed") else
           "Declare nothing and your build lands as UNVERIFIED, never as done.\n")
        + f"- FAN OUT WHEN THE WORK SPLITS. For independent steps- different files, no shared state-\n"
        f"  you may run sub-workers concurrently under you:\n"
        f"    python \"{os.path.join(os.path.dirname(os.path.abspath(__file__)), 'baxter_orch.py')}\" --fanout \"{rf}\"\n"
        f"  It runs your plan's `parallel` steps at once. A sub-worker's report of success is NOT "
        f"evidence: you run each step's check yourself and read the exit code. A step whose check you "
        f"never ran is unverified, never done- an unverified claim here is inherited by everything "
        f"below it. The same rule binds any Agent sub-agent you spawn by hand.\n"
        f"- On completion: mark the build's '- [ ]' task done in the vault and add one terse "
        f"line to the Workshop activity-log per the contract (no vault paths, no mention).\n"
        f"- ANNOUNCE YOUR LANDING (Atul, 8th July- he wants a confirmation at BOTH ends of every "
        f"build). Your START was already posted to #general for you. Before you exit, write your "
        f"one-line landing message into your journal- it is posted the moment your lane retires:\n"
        f"    python \"{os.path.abspath(__file__)}\" --announce \"{rf}\" \"<one clean line, his register, what is now live>\"\n"
        f"  e.g. 'Bridge up-down pings are live, sir- WhatsApp + all 3 IMAP accounts, one alert per "
        f"transition.' Skip it and a generic line goes out in its place. If you HALT instead of "
        f"finishing, don't write one- the pause announces itself.\n"
        + reply_line
        + f"- NEVER message anyone but Atul; no outward action ever.\n\n"
        f"{_rules.voice()}"
    )


def _resume_worker(rfile):
    """Continue ONE interrupted build (journaled in .baxter_resume). The journal is
    deleted only on a clean claude exit- failures stay for the orphan sweep, exactly
    like triage batches. Heartbeats the journal's mtime so the sweep can tell a
    3-hour build from a dead one."""
    rf = Path(rfile)
    try:
        entry = json.loads(rf.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log(f"resume worker couldn't read {rfile}: {e}")
        return
    # HARD STOP self-abort: a resume is the single biggest burn (3h timeout). If the
    # flag went up between spawn and now, abort BEFORE calling claude- the journal
    # stays, the sweep respawns it once the band clears. This is the belt to
    # maybe_resume's braces (which gates at spawn time).
    if _gov:
        try:
            b, why = _gov.blocked("big")
            if b:
                log(f"resume worker HELD ({why}) - build kept, retries after the band clears")
                # STAMP THE HOLD, AND SCRUB THE DEATH IT INHERITED. This journal is about to
                # sit idle with no process behind it, which is indistinguishable from a corpse
                # once the band reopens- and any last_exit/last_tail on it belongs to an EARLIER
                # worker's real death, not to this build. Left there, the reaper reads a
                # 4294967295 exit off a build that never ran (five false FAILED lanes at 13:01
                # on 9th July). `held` is what tells dead_lanes() there is no body; clearing the
                # stale death is what stops the classifier lying about how it ended.
                _journal_set(rf, pid=None, held=True, held_at=datetime.now().isoformat(),
                             last_exit=None, last_tail=None, last_fail=None)
                return
        except Exception:
            pass
        # DELEGATOR self-abort: a yield marker means this lane must stand down for a
        # clashing lane. Don't just return- that would hold the lane open with a marker
        # nothing can clear (the journal only dies on a clean exit), deadlocking the
        # slot. Re-queue the task at resume priority and free the lane; the
        # pre-assignment gate then keeps it off the winner's lane until that one ends.
        try:
            if _gov.yield_marker(rf).exists():
                m = json.loads(_gov.yield_marker(rf).read_text(encoding="utf-8-sig"))
                _gov.halt(entry.get("task", ""), entry.get("next_step", ""),
                          entry.get("note_path", ""), entry.get("state_summary", ""),
                          touch_set=entry.get("touch_set") or None)
                _gov.yield_marker(rf).unlink(missing_ok=True)
                rf.unlink(missing_ok=True)
                log(f"lane {_lane_no(entry.get('lane', '?'))} (idx {entry.get('lane', '?')}) YIELDED before start ({m.get('reason', 'clash')})- re-queued at p2, lane freed")
                return
        except Exception as e:
            log(f"yield handling failed on {rf.name}: {e}")
    # THE STAMP IS SINGLE-USE. Past every abort, this lane is genuinely running, so any
    # governor stamp it inherited describes a hold that is now OVER. Left on the journal it
    # would mask the NEXT real crash as a hold- the very failure this machinery fixes.
    if entry.get("halted_by"):
        _journal_set(rf, halted_by="", halted_at="", halted_level="")
        entry = _read_journal(rf) or entry
    # START confirmation- past every abort, so this line is true when he reads it
    _announce_start(rf, entry)
    import threading
    stop = threading.Event()
    def _beat():
        while not stop.wait(60):
            try: os.utime(rf)
            except Exception: pass
    threading.Thread(target=_beat, daemon=True).start()
    # THE PLANNER TIER. A separate spawn decomposes the task and, more importantly, writes
    # the acceptance test BEFORE the executor exists- then seals it. Until now the worker
    # that did the job also wrote its own exam (`--verify-cmd`), so a builder in a hurry
    # could declare `python -c "pass"` as its proof and the gate would wave it through
    # green. Runs AFTER the heartbeat starts, because a planner spawn takes minutes and a
    # journal going cold mid-plan reads as a corpse to the lane reaper. Fails OPEN: no
    # plan, no seal, the lane proceeds exactly as it did before this tier existed.
    if _orch is not None:
        try:
            entry = _orch.ensure_plan(rf, entry) or entry
        except Exception as e:
            log(f"planner tier failed on {rf.name} ({e})- the lane proceeds unplanned")
    # SCOPE-ONLY LANE (Atul, 10th July: "a read only touch set... forced to read only, no edit...
    # then this naturally allows for whatever needs to be done next... so it becomes autonomous").
    # An entry with no declared touch-set and not --solo runs read-only first: it reads the code
    # to find what a build WOULD touch. clash() already flows it onto this lane in parallel with
    # everything (it edits nothing, so it conflicts with nothing). BAXTER_SCOPE_ONLY makes that
    # MECHANICAL- the PreToolUse writer hook denies every Edit/Write while it is set. The pass
    # reads, then acts on its own: re-queues as a scoped build (--halt with a touch-set), or tells
    # Atul it needs a design. Set on THIS worker process; the claude call below inherits os.environ.
    scope_only = not ((entry.get("touch_set") or []) or entry.get("solo"))
    if scope_only:
        os.environ["BAXTER_SCOPE_ONLY"] = "1"
        prompt = _scope_prompt(rf=rf, entry=entry, lane=_lane_no(entry.get("lane", 0)))
    else:
        os.environ.pop("BAXTER_SCOPE_ONLY", None)
        prompt = build_worker_prompt(rf=rf, entry=entry,
                                     lane=_lane_no(entry.get("lane", 0)),   # builders speak Atul's
                                     touch=entry.get("touch_set") or [],    # numbering too: 1..10
                                     repair=bool(entry.get("repair_pending")))
    try:
        try:
            # 'build' lane: Opus (never Fable) + Playwright only- a build's research may hit a
            # login-walled URL, but it has no use for shadcn/context7/paper-search/vault-fs.
            r = subprocess.run(_claude("build") + ["-p", prompt], cwd=str(VAULT), timeout=10800,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        except Exception as e:
            log(f"resume worker failed ({e}) - classifying")
            _handle_failure(rf, entry, None, str(e))   # silence on failure is the same sin
            return
        if r.returncode != 0:
            out = (r.stdout or "") + "\n" + (r.stderr or "")
            log(f"resume worker claude FAILED (exit {r.returncode}) - classifying: "
                f"{' '.join(out.split())[:200]}")
            _handle_failure(rf, entry, r.returncode, out)
            return
        # CLEAN EXIT. That is the worker's CLAIM, not evidence. A build that halted itself
        # re-queued its own task and is a pause, not a landing- nothing to verify there.
        paused = _requeued(entry.get("task", ""))
        if not paused:
            verdict, detail, entry = _verify_gate(rf, entry)
            if verdict == "failed":
                # The gate overrules the worker: this build did NOT land, whatever it said.
                _handle_failure(rf, entry, 0, detail, source="verify gate")
                return
            if _bv:
                _bv.record(entry, "verified" if verdict == "passed" else "unverified",
                           detail, lane=entry.get("lane"))
        else:
            verdict, detail = "paused", ""
        _announce_stop(rf, entry, ok=True, verdict=verdict, detail=detail)
        try: rf.unlink()
        except Exception: pass
        # a clean exit (finished, or halted + re-queued itself) frees the lane- drop any
        # yield marker with it so a later lane reusing the name never inherits the stop
        if _gov:
            try: _gov.yield_marker(rf).unlink(missing_ok=True)
            except Exception: pass
        log(f"resume worker finished ({verdict}): {str(entry.get('task', '?'))[:60]}")
        if not paused:
            _pump_now()   # the lane is free- take the next task NOW, not on the next beat
    finally:
        stop.set()

def maybe_resume(state, force=False):
    """The usage-refresh probe + BIG-TASK PUMP. `force` skips the 5-minute retry throttle-
    a lane that just retired calls this immediately (`--pump`) so the queue keeps chugging
    rather than idling until the next poll beat. When the 5h window rolls over:
    (a) un-park and re-arm every queued batch (retry counters cleared- an outage
    longer than the retry budget must never eat messages), (b) start the TOP of
    the big-task queue. ONE project build at a time, ever (his 5th-July 12:00 +
    14:33 order- the parallel resume fan-out that drained a whole window in
    minutes is dead). The next queued task starts only when the slot frees;
    also retries every ~5 min whenever the curve reopens early."""
    snap = {}
    try:
        snap = json.loads((VAULT / ".baxter_usage.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return
    # tolerance compare: the endpoint's resets_at jitters ±a second around the minute
    # EDGE between polls (11:49:59.9 vs 11:50:00.1)- string/minute equality both false-
    # fire, so a window only counts as rolled when the time moves by >2 minutes
    marker = snap.get("session_resets_at") or ""
    prev = state.get("usage_reset_marker", "")
    rolled = False
    if marker and prev:
        try:
            rolled = abs((datetime.fromisoformat(marker) - datetime.fromisoformat(prev)).total_seconds()) > 120
        except Exception:
            rolled = marker[:16] != prev[:16]
    if marker and (rolled or not prev):
        state["usage_reset_marker"] = marker   # only advance on a REAL roll- jitter never creeps in
    if rolled:
        log("usage window refreshed- draining queues + resuming interrupted work")
        state["resurrect_pending"] = "resume"   # a usage-cap resume is a resurrection- the reply-audit end-stage fires next pass
        try:
            for bf in list(BATCH_DIR.glob("*.retry*.json")) + list(BATCH_DIR.glob("*.parked.json")):
                if datetime.now().timestamp() - bf.stat().st_mtime < 120:
                    continue   # a worker spawned seconds ago likely holds this file- renaming it mid-read killed batches on 5th July
                base = bf.name.split(".retry")[0].split(".parked")[0] + ".json"
                tgt = BATCH_DIR / base
                if not tgt.exists():
                    bf.rename(tgt)
        except Exception as e:
            log(f"retry-counter reset failed: {e}")
    # ---- the big-task pump: LANE_COUNT clash-checked lanes, top of the queue down ----
    # (8th July, Atul 22:09: concurrent builds to halve the drain, with a delegator keeping
    # the lanes off conflicting or near-adjacent work; four lanes since 9th July.) Every
    # rejection here- clash or human gate- is mirrored into the guard's own log, so "has the
    # guard ever rejected anything?" is one command (`--rejects`) and not a grep. A lane opens only when
    # the picked task's declared touch-set clashes with neither a live lane nor another
    # task picked in the same pass; the second lane additionally needs usage headroom.
    if not _gov:
        return
    q = []
    try:
        q = _gov.queue_read()
    except Exception as e:
        log(f"task queue unreadable: {e}")
    if not q:
        return
    try:
        lanes = _gov.lane_journals()      # live (non-failed) journals = builds in flight
    except Exception as e:
        log(f"lane accounting failed ({e})- holding the pump")
        return
    # ---- THE PUMP ARITHMETIC LINE (9th July) ------------------------------------------
    # `.baxter.log` contained ZERO statements of what a pump pass actually decided. It
    # logged individual clash skips and lane starts, and never once said "this pass had N
    # free slots and used M"- which is exactly why "the six lanes are serial" could be
    # asserted, and neither proved nor disproved, for a whole day. (It was false: ten lanes
    # were live, ten claude leads, three opening in a single pass. The real throttle is the
    # clash gate, where whole-file locks refuse correctly region-declared tasks.)
    #
    # ONE line per pass, at every exit past this point, so the question is permanently
    # answerable by grep. Rate-limited to 1/min on ordinary beats- but NEVER when `force`
    # (a lane just retired and re-pumped) or `rolled` (the window turned), because those
    # are precisely the passes worth reading.
    _gated = getattr(_gov, "is_human_gated", lambda e: bool(e.get("gated_on")))
    pump = {"lanes": len(lanes), "cap": _gov.LANE_COUNT, "free": 0,
            "runnable": sum(1 for e in q if not _gated(e)), "picked": 0, "skipped": 0}

    def _pump_line():
        if not (force or rolled):
            try:
                lp = state.get("last_pump_log", "")
                if lp and (datetime.now() - datetime.fromisoformat(lp)).total_seconds() < 60:
                    return
            except Exception:
                pass
        state["last_pump_log"] = datetime.now().isoformat()
        # THE DRAIN MARKER (10th July). `free=0` was two different worlds in one line: a
        # saturated fleet with ten builds running, and the 60% drain band, where capacity is
        # pinned to the live lane count so the board empties itself. `pump: lanes=3 cap=3
        # free=0` at 62% said nothing about which. Ask the governor for the band- never
        # re-derive it here, or the log and the pump can disagree about what stopped a lane.
        try:
            drain = 1 if _gov.soft_stopped()[0] else 0
        except Exception:
            drain = 0
        log("pump: lanes={lanes} cap={cap} free={free} drain={drain} runnable={runnable} "
            "picked={picked} skipped={skipped}".format(drain=drain, **pump))

    if len(lanes) >= _gov.LANE_COUNT:
        _pump_line()   # a FULL board is the single most important pass to state out loud
        return   # all lanes busy- cheap exit, no meter read needed
    if not _usage_ok("project"):
        state["queue_gate_closed"] = True   # remember the hold- reopening earns the ✅ line
        pump["cap"] = 0                     # the gate shut: the pump may open no lane at all
        _pump_line()
        # NO `held` STAMP BELONGS HERE, and adding one would write to nothing. This gate
        # returns BEFORE the journal is created (the write is ~55 lines below), so a
        # pump-time hold leaves no file on disk for the reaper to misread. The only site
        # that abandons a body-less journal is _resume_worker's HELD self-abort, which
        # stamps it there. Left as a note because it reads like an omission and is not.
        return
    # capacity AFTER the gate: _usage_ok forces a <=60s meter, so the 2nd lane's headroom
    # rule is decided on a fresh read. Off a stale (false-low) meter it could open a second
    # burn that the curve would never have allowed- the 6th-July false-low failure class.
    cap = _gov.lane_capacity()
    free = cap - len(lanes)
    pump["cap"], pump["free"] = cap, max(0, free)
    if free <= 0:
        _pump_line()
        return   # headroom too tight for a 2nd lane; the live one runs on
    if not rolled and not force:
        try:
            lt = state.get("last_resume_try", "")
            if lt and (datetime.now() - datetime.fromisoformat(lt)).total_seconds() < 300:
                return   # NOT a pump pass at all- nothing was decided, so nothing is logged
        except Exception:
            pass
    state["last_resume_try"] = datetime.now().isoformat()
    # HUMAN-GATED tasks never auto-pump. A task waiting on Atul's explicit 'go' or a
    # pending human answer (NOT the usage curve) must not be journaled into a lane-
    # the pump would just slam the gate every window and force a hold-and-re-park loop
    # (the email-hygiene cull, runs 4-6, 5th-6th July: outward unsub + destructive bin,
    # money/legal-flagged, never fires unprompted). These wait in the queue until Atul's
    # live 'go' (`--ungate`), which arrives as a channel command, not the pump.
    # The gate is the explicit `gated_on` FIELD, set at queue time- never inferred from
    # the task's prose. Reading the prose meant any task that so much as DESCRIBED being
    # gated disabled itself (9th July: the p1 pump fix, invisibly), while a genuinely
    # gated task phrased another way ran anyway. And every skip is LOGGED, like a clash-
    # a dropped task is never invisible again. (`_gated` is bound above, where the pump
    # line needs it to count `runnable` before any of the early exits.)
    runnable = []
    for e in q:
        if _gated(e):
            who = getattr(_gov, "gate_of", lambda x: "a human")(e)
            log(f"pump: '{str(e.get('task', '?'))[:50]}' held out of the queue- gated on "
                f"{who}, awaiting his go")
            _record_reject(e, f"gated on {who}, awaiting his go", kind="gated_on")
        else:
            runnable.append(e)
    if not runnable:
        _pump_line()
        return   # only human-gated tasks left- nothing for the pump; they await Atul's go
    # an unreadable journal (entry None) has no known touch-set, so it clashes with
    # everything and holds the second lane shut- exactly the safe default
    live_sets = [_gov.touch_of(e or {}) for _rf, e in lanes]
    picked, skips = _gov.pick_for_lanes(runnable, live_sets, free)
    pump["picked"], pump["skipped"] = len(picked), len(skips)
    _pump_line()   # BEFORE the per-skip lines, so the arithmetic heads the pass it explains
    for e, why in skips:
        log(f"delegator: '{str(e.get('task', '?'))[:50]}' held off a lane- {why}")
        _record_reject(e, why, lane=_clashing_lane(e, lanes), kind="clash")
    if not picked:
        return
    RESUME_DIR.mkdir(exist_ok=True)
    used = {_gov._lane_id(rf) for rf, _e in lanes}
    started = []
    for head in picked:
        lane = _gov.next_lane_id(used)
        used.add(lane)
        head["lane"] = lane
        head["started_at"] = datetime.now().isoformat(timespec="seconds")
        rf = RESUME_DIR / f"resume-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{lane}.json"
        try:
            # write via a non-matching temp name + atomic replace, so lane_journals()
            # (which globs resume-*.json) never catches a half-written journal
            tmp = RESUME_DIR / f".pending-{lane}.tmp"
            tmp.write_text(json.dumps(head, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(rf))
        except Exception as ex:
            log(f"resume journal write failed: {ex}")
            continue
        started.append((rf, head))
    if not started:
        return
    # POP THE STARTED TASKS UNDER THE QUEUE LOCK (9th July). `q` was read a while ago- the
    # gate check above forces a <=60s meter, i.e. a network round-trip- so anything queued
    # in that window is missing from it. Writing `q` minus the started entries would silently
    # DELETE those new tasks: the classic lost update, and the reason the queue got a real
    # lock. Re-read inside the transaction and remove by stable `id`, so exactly the entries
    # handed to a lane are taken and nothing else is touched.
    # `queue_txn` via getattr: this process may be running a stale import of baxter_usage
    # ([[long-lived-process-staleness]]), and an AttributeError here would skip the rewrite
    # entirely- leaving the started task in the queue to be started AGAIN next pass.
    rest = [e for e in q if not any(e is h for _rf, h in started)]
    started_ids = {h.get("id") for _rf, h in started if h.get("id")}
    started_texts = {h.get("task") for _rf, h in started if not h.get("id")}
    txn = getattr(_gov, "queue_txn", None)
    try:
        if txn is None:
            _gov.queue_write(rest)          # legacy module: unlocked, as it always was
        else:
            with txn():
                fresh = _gov.queue_read()
                rest = [e for e in fresh if e.get("id") not in started_ids
                        and e.get("task") not in started_texts]
                _gov.queue_write(rest)
    except Exception as ex:
        log(f"queue rewrite failed: {ex}")
    for rf, head in started:
        _spawn_resume(rf)   # if the spawn dies, the journal is on disk- the sweep respawns it
        log(f"build lane {_lane_no(head['lane'])} (idx {head['lane']}): started '{str(head.get('task', '?'))[:60]}' ({len(rest)} queued behind)")
        # A FLEET LOCK MUST NEVER BE SILENT (Atul, 9th July- "only lane 1 functional").
        # But ONLY a genuine --solo build locks the fleet now (10th July): an empty touch-set is
        # a READ-ONLY scoping pass that clashes with nothing and holds no lane shut, so warning on
        # it spammed Atul about locks that were not happening. Warn only when `solo` is truly set.
        if head.get("solo"):
            n = _lane_no(head["lane"])
            log(f"FLEET LOCK: lane {n} started a --solo build- it holds the other "
                f"{_gov.LANE_COUNT - 1} lanes shut until it finishes")
            _say(f"⚠️ Lane {n} is running **solo**, sir- '{str(head.get('task', '?'))[:60]}' "
                 f"is a whole-fleet build, so it holds the other {_gov.LANE_COUNT - 1} lanes shut "
                 f"until it finishes.")
    # ping only when a pause actually LIFTED (window roll / curve reopening)-
    # chained starts inside an open window stay silent (alert spam is a bug)
    gate_reopened = bool(state.pop("queue_gate_closed", False))
    if rolled or gate_reopened:
        n = len(lanes) + len(started)
        behind = f" ({len(rest)} queued)" if rest else ""
        _say(f"✅ Usage {'reset' if rolled else 'clear'}- resuming builds, {n} running{behind}.")

# ---- THE QUEUE-ACK RECONCILER (Atul, 9th July 09:38) --------------------------------
# The last line of his order: "for every fast-lane reply carrying the queued phrase, assert
# a queue entry references that message_id, and ping if not."
#
# baxter_fast now writes the placeholder before it says anything, so this should never fire.
# That is exactly why it exists. The prompt rule and the pre-announce guard both steer a
# language model; this checks the FILES afterwards and cannot be talked round. If it ever
# pings, the ack-before-act hole has reopened somewhere new.
#
# Evidence that a claim was honest, either of:
#   - a RECEIPT in .baxter_queue_acks.json (written by the fast lane before the reply left),
#   - a live queue entry whose source_mid is that message id.
# The receipt is primary and the queue entry secondary, because the pump POPS an entry out
# of the queue the moment it hands it to a lane- a build that started is not a lie.
QUEUE_ACKS = VAULT / ".baxter_queue_acks.json"
SEND_LOG = VAULT / ".baxter_send_dedup.json"
FAST_HANDLED = VAULT / ".baxter_fast_handled.json"
ACK_PINGED = VAULT / ".baxter_queue_ack_pinged.json"
# The moment receipts began. A reply sent BEFORE the fast lane started writing placeholders
# can have no receipt, so the check does not apply to it- judging those would flag the whole
# of this morning. Not a fudge: two of the sends it would flag (09:21) are the ones Atul
# caught himself, and a third (10:38, the top-hat role icon) was queued minutes later by
# triage under different prose. All three were true by luck, which is the fault, not the lie.
# He has already been told. The reconciler's job is the NEXT one.
ACK_EPOCH = VAULT / ".baxter_queue_ack_epoch"

# The fleet log. baxter_usage._log stamps it inside the same transaction that writes the
# queue file, so it is the only append-only record of WHEN a row was written.
ENQUEUE_LOG = VAULT / ".baxter.log"

QUEUE_CLAIM_TEXT = re.compile(r"\bqueu(?:ed|ing|eing)\b", re.I)

# `queued (p5, 33 deep): ...` for a fresh row, `queue updated (p1): ...` when an existing one
# is refined in place. Both are queue writes; nothing else in the log is. Groups: the stamp,
# the priority, the depth.
#
# The tail is deliberately unanchored. 22 live lines read `queued (p6, 21 deep, gated on
# atul)`, and a regex demanding `)` straight after the depth drops every one of them- deleting
# the very evidence an honest gated claim is cleared by. Only a FRESH row carries a depth:
# refining an entry in place does not change how deep the queue is, so 299 of the 702 live
# lines yield `depth=None`. That is the common case, not the corrupt one.
ENQUEUE_LINE = re.compile(
    r"^\[(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)\] usage: queue(?:d| updated) "
    r"\(p(\d+)(?:, (\d+) deep)?")

# The slot a worker quotes back, in either shape it can have been handed: DEFLECT_RULE's short
# `position 21 of 33, p5`, or the full `position 21 of 33 pending (0 running, 0 benched), p5`
# that position_line() prints. 33 characters separate the two figures in the long form, so a
# tight gap silently stops matching it and every long-form claim slides onto the bare window.
CLAIM_SLOT = re.compile(r"\bposition\s+\d+\s+of\s+(\d+)\b.{0,60}?\bp(\d+)\b", re.I | re.S)


def _enqueue_stamps(path=None):
    """Every queue WRITE, as `{"queued_at", "priority", "depth"}` rows the reconciler weighs.

    The queue FILE cannot answer this. The pump pops an entry out of it the moment a lane
    takes the task, and a landed build's journal is unlinked- so an honest claim loses the
    evidence that it was honest as soon as the work it named begins. Message
    444444444444444401 (9th July 19:32:22) named `position 21 of 33, p5`; the row was
    written at 19:32:17 and the audit called it a lie the instant lane 5 picked it up.

    `queued_at` keeps its name: `_queued_at_ts` and the live `queue_read()` rows that
    `queue_ack_audit` mixes in beside these both key on it. `priority` and `depth` borrow the
    queue entry's own field names, so one predicate weighs a log row and a queue row alike- a
    queue row simply carries no depth.

    Malformed lines are skipped rather than raised on: this runs inside the proactive pass.
    """
    try:
        text = Path(path or ENQUEUE_LOG).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    rows = []
    for ln in text.splitlines():
        m = ENQUEUE_LINE.match(ln)
        if m:
            rows.append({"queued_at": m.group(1),
                         "priority": int(m.group(2)),
                         "depth": int(m.group(3)) if m.group(3) else None})
    return rows


def _ack_epoch():
    """Unix ts from which 'queued' claims are judged. Stamped on first read, so a fresh
    install (or a deleted file) starts clean instead of pinging about ancient history."""
    try:
        return float(ACK_EPOCH.read_text(encoding="utf-8").strip())
    except Exception:
        now = time.time()
        try:
            ACK_EPOCH.write_text(str(now), encoding="utf-8")
        except Exception:
            pass
        return now


def _queued_at_ts(entry):
    """A queue entry's write time as a unix ts, or None if it is unparseable.

    OSError is caught, not just ValueError: on Windows, .timestamp() on a naive datetime
    near the epoch raises errno 22 rather than returning a negative number. One malformed
    queued_at must never take the whole audit- and with it the proactive pass- down.
    """
    try:
        return datetime.fromisoformat(str(entry.get("queued_at"))).timestamp()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _as_int(value):
    """`value` as an int, else None when it is absent or unparseable.

    A missing figure means the stamp does not KNOW it- never that it is zero, and never that
    it is the default priority of 5. Defaulting here would make an uninformative row vote.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slot_agrees(stamp_priority, stamp_depth, slot):
    """Could this queue write be the one the reply was quoting?

    `slot` is `(M, P)` read out of `position N of M, pP`, or None when the reply quoted none.
    A write agrees when it CONTRADICTS NOTHING IT KNOWS: an absent priority or depth abstains
    rather than votes. Abstention is what keeps the guard honest- a live queue row carries no
    depth, and 299 of the fleet log's 702 write stamps carry none either.
    """
    if slot is None:
        return True                        # nothing quoted, so nothing to contradict
    if stamp_priority is None and stamp_depth is None:
        return True                        # knows neither figure: uninformative, not damning
    depth, priority = slot
    if stamp_priority is not None and stamp_priority != priority:
        return False
    if stamp_depth is not None and stamp_depth != depth:
        return False
    return True


# How long before a reply a queue write still counts as that reply's write. A fast-lane
# worker queues and answers within seconds (11:54:40 -> 11:54:44, observed); five minutes is
# generous and still far short of the gap the fault leaves (09:21 ack, entry at 09:30, by
# another process entirely).
ACK_WINDOW = 300.0


def queue_ack_violations(sends, fast_handled, receipts, queue, epoch=0.0, window=ACK_WINDOW):
    """Message ids the fast lane told Atul were queued, with no entry behind them. PURE.

    Scoped to replies the FAST LANE handled: a reply from another lane may legitimately
    have queued fresh work of its own, unrelated to the message it answers, and pinging him
    about that would be the guard crying wolf.

    Scoped again to replies sent at or after `epoch`- the point placeholders began. Before
    it, an honest claim and a lucky one are indistinguishable from the files, and a check
    that cannot tell them apart must not accuse.

    THREE kinds of evidence make a claim honest, and the third is not optional:
      1. a RECEIPT- the fast lane wrote a placeholder before the reply left. Exact.
      2. a live queue entry whose source_mid is that message. Exact.
      3. a queue write in the `window` before the reply which DOES NOT CONTRADICT the slot
         the reply quoted. Inexact, and it has to be. A big ask gets a placeholder, but an
         ordinary message can still reach a worker that decides to defer real work, and
         DEFLECT_RULE tells it to run --queue itself. The CLI has no --source-mid flag, so
         that entry cannot name the message it came from, and a check demanding one would
         accuse every honest deferral. Caught live at 11:54 on 9th July: "queued, sir- the
         flow graphic, position 21 of 33, p5" was perfectly true, and an earlier draft of
         this function flagged it.

    WHY THE THIRD TEST IS NO LONGER A BARE WINDOW. It used to clear a claim on ANY write
    within five minutes. On a busy build night 668 unrelated stamps sit inside that window,
    so nearly every claim cleared and the guard had no bite left. A reply that quotes its
    slot- `position N of M, pP`- now has that slot weighed against the write: the write must
    agree on every field it knows, its priority being P and its depth M.

    Both abstentions are deliberate, because this guard ACCUSES. A stamp knowing neither
    figure (a live queue row, which never carries a depth) is uninformative and still clears.
    A reply quoting no slot at all still falls back to the bare window. Where the files cannot
    tell an honest claim from a lucky one, the check must not pretend that they can.

    The fault this catches is untouched: an ack with NO queue call anywhere near it leaves
    nothing in the window, which is exactly what happened at 09:21.
    """
    handled = {str(x) for x in (fast_handled or [])}
    receipted = {str(k) for k in (receipts or {})}
    sourced = {str(e.get("source_mid")) for e in (queue or []) if e.get("source_mid")}
    writes = []
    for e in (queue or []):
        w = _queued_at_ts(e)
        if w is not None:
            writes.append((w, _as_int(e.get("priority")), _as_int(e.get("depth"))))
    bad = set()
    for s in sends or []:
        mid = s.get("reply_to")
        if not mid:
            continue                       # a brief or a ping, not an answer to him
        try:
            ts = float(s.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if ts < float(epoch):
            continue                       # predates the receipt mechanism- not judgeable
        mid = str(mid)
        if mid not in handled:
            continue                       # not the fast lane's reply
        # The pre-announce guard owns the one question "does this claim a queueing?"- and
        # it handles negation, which the word-level QUEUE_CLAIM_TEXT never did, so "it was
        # never queued" now reads as the absence it is. Lazily imported, exactly as
        # queue_ack_audit imports baxter_autobuild: a module-level import is circular, since
        # the guard selftest imports triage.
        import baxter_preannounce_guard as _guard
        if not _guard.claims_queue(s.get("norm") or ""):
            continue                       # claimed nothing about a queue
        if mid in receipted or mid in sourced:
            continue                       # the entry exists and names this message
        # A write moments before the claim, which does not contradict the slot the claim
        # quoted. `+60` forgives clock skew between the queue's second-resolution ISO stamp
        # and the send log's float.
        m = CLAIM_SLOT.search(s.get("norm") or "")
        slot = (int(m.group(1)), int(m.group(2))) if m else None
        if any(ts - window <= w <= ts + 60 and _slot_agrees(prio, depth, slot)
               for w, prio, depth in writes):
            continue
        bad.add(mid)
    return sorted(bad)


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def queue_ack_audit(ping=True):
    """Run the reconciler over the live files. Returns the offending message ids.

    `ping` is opt-in so a hand-run can never buzz his phone, exactly as baxter_rules.check
    does- a guard that cries wolf while it is being tested is a guard he learns to ignore.
    """
    sends = _read_json(SEND_LOG, [])
    handled = (_read_json(FAST_HANDLED, {}) or {}).get("ids", [])
    receipts = _read_json(QUEUE_ACKS, {})
    try:
        queue = _gov.queue_read() if _gov else []
    except Exception:
        queue = []
    if _gov:
        # The rows the pump has already popped into a lane, recovered from the fleet log.
        # Gated on `_gov` with the queue read it extends: no governor, no queue evidence.
        queue = list(queue) + _enqueue_stamps()
    bad = queue_ack_violations(sends, handled, receipts, queue, epoch=_ack_epoch())
    if not bad or not ping:
        return bad          # a hand-run REPORTS and writes nothing: it must not consume the
                            # ping, nor pad the rejects log the proactive pass owns
    already = set(_read_json(ACK_PINGED, {}).get("ids", []))
    fresh = [m for m in bad if m not in already]
    if not fresh:
        return bad          # every violation already logged + pinged once; the log is a
                            # record of distinct holes, not a per-pass drumbeat
    for mid in fresh:
        log(f"QUEUE-ACK VIOLATION: told Atul {mid} was queued; no entry carries that message id")
        if _gov:
            try:
                _gov.record_reject(f"reply to message {mid}",
                                   "claimed 'queued' with no queue entry carrying that message id",
                                   kind="queue-ack")
            except Exception:
                pass
    # THE STANDING ORDER (Atul, 9th July). An ack that ran ahead of its write is MEAGER: it
    # is already logged, already in the rejects log, and the repair- closing the hole between
    # the pre-announce guard and the prompt rule it enforces- is code that
    # baxter_queue_ack_selftest proves. Baxter queues that build itself rather than telling
    # Atul about a hole he cannot fix. He hears only if no repair could be queued.
    _u = str(Path(__file__).resolve().parent)
    _repaired = False
    try:
        import baxter_autobuild as _ab
        _ab.notice(
            kind="queue-ack-violation",
            # STABLE across passes: the offending message ids ride in `signature`, never the
            # key. Keyed on the ids, every pass with a new violation would fork a fresh build.
            task=("Repair queue-ack violation: a worker told Atul something was queued with "
                  "no queue entry behind it"),
            next_step=("Read the offending message ids from .baxter_rejects.jsonl, then close "
                       "the gap between baxter_preannounce_guard and the rule it enforces"),
            severity="meager", signature=f"{len(fresh)} violation(s): {', '.join(fresh[:10])}",
            verify=f'python "{_u}\\baxter_queue_ack_selftest.py"',
        )
        _repaired = True
    except Exception as _abe:
        log(f"autobuild could not queue the queue-ack repair: {_abe!r}")
    if not _repaired:
        _say(f"⚠️ I told you {len(fresh)} thing{'s were' if len(fresh) > 1 else ' was'} queued, "
             f"sir, and no queue entry carries it. The ack ran ahead of the write- it's in the "
             f"rejects log, and I could not queue a repair for it either.")
    try:
        ACK_PINGED.write_text(json.dumps({"ids": sorted(already | set(fresh))[-200:]}),
                              encoding="utf-8")
    except Exception:
        pass
    return bad


RESURRECT_GAP = 900      # >15 min since the last proactive pass = the process was down/asleep -> a crash resurrection
RESURRECT_COOLDOWN = 900  # don't re-run the end-stage within 15 min (one sweep per resurrection, not a loop)
def maybe_resurrection_audit(state):
    """RESURRECTION REPLY-AUDIT END-STAGE (Atul, 8th July- queued build).
    Fires the fixed-order end-stage- confirm vitals -> answer every tickless (no genuine
    native reply) message -> reconcile emoji LAST- after ANY resurrection:
      - /off -> /on           (the .baxter_catchup breadcrumb, dropped by /on)
      - usage-cap window resume(state['resurrect_pending'], set by maybe_resume on a roll)
      - crash / sleep recovery (a >15-min gap since the last proactive pass)
    Delegates to baxter_resurrect_audit.py, which reuses the doctor + reaction engines-
    it never re-implements the sweep or the tick logic. The hard rule is enforced there:
    a message is ticked ONLY once a real reply exists, so we answer first, tick last.
    Deduped by a cooldown so a resurrection triggers exactly one sweep, not a loop."""
    now = datetime.now()
    reason = None
    if CATCHUP.exists():
        reason = "on"
    elif state.get("resurrect_pending"):
        reason = str(state.get("resurrect_pending"))
    else:
        last = state.get("last_proactive_pass", "")
        try:
            gap = (now - datetime.fromisoformat(last)).total_seconds() if last else 1e9
        except Exception:
            gap = 1e9
        if gap >= RESURRECT_GAP:
            reason = "crash"
    state["last_proactive_pass"] = now.isoformat(timespec="seconds")
    if not reason:
        return
    # cooldown: one end-stage per resurrection window, never a re-fire loop
    try:
        la = state.get("last_resurrect_audit", "")
        if la and (now - datetime.fromisoformat(la)).total_seconds() < RESURRECT_COOLDOWN:
            state.pop("resurrect_pending", None)
            return
    except Exception:
        pass
    state["last_resurrect_audit"] = now.isoformat(timespec="seconds")
    state.pop("resurrect_pending", None)
    log(f"resurrection reply-audit end-stage firing (trigger: {reason})")
    # DETACHED: the audit wakes its own answer worker (up to ~15 min) and reconciles
    # emoji at the end- blocking here would hold the triage lock past the stale-lock
    # reaper. Its three stages are internally ordered, so correctness holds off-thread.
    try:
        script = r"C:\Users\you\Documents\Python Scripts\utils\baxter_resurrect_audit.py"
        env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
        subprocess.Popen(["python", script, "--trigger", reason],
                         cwd=str(VAULT), env=env, creationflags=_NO_WIN, **_SILENT)
    except Exception as e:
        log(f"resurrection reply-audit failed: {e}")

EXAM_SWEEP_EVERY = 3600   # seconds. 4 exams a sweep, ~34 exams: the whole tree every ~8 hours.
def maybe_exam_sweep(state):
    """A LANDED BUILD'S PROOF KEEPS RUNNING (queued 10th July).

    Every big build seals an acceptance exam, that exam is run once as the lane exits, and the
    journal naming it is then unlinked. `utils/baxter_ack_ledger_exam.py`,
    `baxter_touchvet_exam.py` and `baxter_halt_verify_exam.py` were referenced by nothing at
    all. On 10th July `_enqueue_stamps()` was gutted and the sealed queue-ack selftest still
    went green, because the exam that would have caught it had not run since its lane retired.

    So a bounded slice of `utils/*_exam.py` is swept every hour. A red is confirmed on a second
    sweep (a sibling lane mid-edit reddens a bystander; the guild endpoint 429s) and then queues
    its own repair through baxter_autobuild, carrying the exam that caught it as the repair's
    acceptance. Meager by autobuild's grading: Atul never hears about it.

    DETACHED, like the resurrection audit. A sweep runs real exams in real subprocesses and can
    take minutes; the watcher blocks on this triage child, and the stale-lock reaper is watching
    the beat. It never blocks, and it never speaks.
    """
    if (VAULT / ".baxter_stop").exists():
        return               # vitals only. Exams burn CPU, and one of them refuses to run anyway.
    now = datetime.now()
    last = state.get("last_exam_sweep", "")
    try:
        if last and (now - datetime.fromisoformat(last)).total_seconds() < EXAM_SWEEP_EVERY:
            return
    except ValueError:
        pass
    state["last_exam_sweep"] = now.isoformat(timespec="seconds")
    try:
        env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"
        subprocess.Popen(["python", str(SCRIPTS / "utils" / "baxter_exam_sweep.py"), "--sweep"],
                         cwd=str(VAULT), env=env, creationflags=_NO_WIN, **_SILENT)
        log("exam sweep: spawned- a slice of utils/*_exam.py, reds queue their own repair")
    except Exception as e:
        log(f"exam sweep spawn failed: {e}")


def maybe_fallback_repair(state, ps1=None):
    """Reconcile baxter_watch.ps1's $slashWatchFallback with baxter_slash's live closure.

    That array is the watcher's fallback watch-set, and it only ever fires when the live import
    walk is broken- i.e. exactly when nobody is looking at it. So it is checked while everything
    still works, beside the other two drift guards.

    Until 10th July a drift QUEUED A BUILD. baxter_autobuild.notice() minted a repair task, a
    lane spawned, and a whole Claude rewrote a handful of names it could have derived- and every
    landed import did it again. The repair now happens here, in place, through the hub fence:
    machine work, not a build.

    Nothing queues a worker any more, so an unrepairable drift has nowhere left to go but Atul.
    A write_fallback that raises (WalkError, an unyielding HubConflict, OSError) or that leaves
    check_fallback still non-zero is a fault NOBODY is fixing, and it says so- once per distinct
    signature per hour. Silence here means the array is right, never that the fault was swallowed.

    `ps1` is the seam the exam drives; it defaults to the live file beside baxter_imports.
    """
    import io as _io, contextlib as _ctx
    import baxter_imports as _imp
    ps1 = Path(ps1) if ps1 else Path(_imp.__file__).resolve().parent / "baxter_watch.ps1"

    def _check():
        # IMPORTED, never shelled: the utils tree straddles py311/py312, so guessing an
        # interpreter is a latent bug, and only an in-process call can capture check_fallback's
        # stdout- which IS the drift signature. Both its DRIFT lines are sorted(), so that text
        # is stable and cannot churn the dedup key across passes.
        _buf = _io.StringIO()
        with _ctx.redirect_stdout(_buf):
            rc = _imp.check_fallback(ps1)
        return rc, _buf.getvalue().strip()

    def _forget():
        had = state.pop("fallback_drift_sig", None), state.pop("fallback_drift_pinged", None)
        if any(v is not None for v in had):
            save_state(state)

    rc, sig = _check()
    if not rc:
        _forget()          # clear on green, so a drift that returns is noticed afresh. Say nothing.
        return

    first = sig.splitlines()[0] if sig else "no detail"
    log(f"FALLBACK DRIFT- {first}")        # every pass: the log is the record he greps

    why = ""
    try:
        _imp.write_fallback(ps1)
        rc2, sig2 = _check()               # the rewrite is not believed, it is re-checked
        if rc2:
            after = sig2.splitlines()[0] if sig2 else "no detail"
            why = f"the array still reads as drifted after the rewrite: {after}"
    except Exception as _exc:
        why = f"the in-place repair raised {_exc!r}"

    if not why:
        log("FALLBACK DRIFT- repaired in place through the hub fence")
        _forget()
        return

    log(f"fallback repair FAILED- {why}")
    _last = state.get("fallback_drift_pinged") or 0
    if sig != state.get("fallback_drift_sig") or (time.time() - _last) >= 3600:
        _say(f"⚠️ Sir- baxter_watch.ps1's $slashWatchFallback has drifted from "
             f"baxter_slash's live import closure, and I could not repair it in place. "
             f"{first} ({why})")
        state["fallback_drift_sig"] = sig
        state["fallback_drift_pinged"] = time.time()
        save_state(state)


LOCK = VAULT / ".baxter.lock"
def _touch_lock():
    """Refresh the lock's mtime so the watcher's stale-lock reaper (>10 min) never
    reaps a legitimately long cycle (big triage + several mines can exceed it)."""
    try:
        if LOCK.exists():
            LOCK.write_text(datetime.now().isoformat())
    except Exception:
        pass
def run():
    # AD-HOC REMINDERS FIRE FIRST (8th July- his 'ping me in 10 mins' that never came).
    # Deliberately above BOTH the single-flight lock and the OFF switch: a reminder Atul
    # explicitly asked for is a promise he's counting on, not proactive machinery, and a
    # long triage cycle holding the lock must never swallow it. Deterministic, no claude.
    # The store's own lock makes this safe alongside the watcher's 15s --fire spawn.
    try:
        import baxter_reminders as _rem
        _rem.fire_due()
    except Exception as _e:
        log(f"reminder fire failed: {_e}")
    # single-flight: prevent the auto-started watcher and any manual run racing
    if LOCK.exists():
        try:
            if (datetime.now().timestamp() - LOCK.stat().st_mtime) < 600:
                return  # another triage in progress
        except Exception:
            pass
    try:
        LOCK.write_text(datetime.now().isoformat())
    except Exception:
        pass
    try:
        # master OFF switch (Atul's /off): soft-pause. Skip ALL proactive work- briefs,
        # pings, filing, queue pump, auto-resume. The fast lane runs in its own process and
        # ignores this flag, so /on and his live questions still land. /on removes the flag.
        if OFF_FLAG.exists():
            log("paused (OFF switch set)- skipping proactive pass; fast lane stays live for /on + questions")
            return
        state = load_state()
        maybe_usage()            # refresh the meters first- everything gates off them
        try:                     # stale-bytecode drift: a python run outside Baxter (a bare
            # `python x.py` in a terminal, the 3.12 slash bot before the watcher set its env)
            # lays down a TIMESTAMP-mode pyc, and a timestamp pyc can serve a hub file's old
            # code to a fresh process. Rewriting it to checked-hash costs eight bytes read per
            # pyc when the tree is already clean, so it runs every pass and says nothing.
            import baxter_verify as _bv
            _n = _bv.harden_bytecode()
            if _n:
                log(f"bytecode: rewrote {_n} stale-mode pyc(s) to checked-hash")
        except Exception as _e:
            log(f"bytecode harden failed: {_e}")
        try:                     # never-Fable detector (no Claude burn): flag drift on any live Baxter session
            import baxter_modelguard as _mg
            _mg.audit()
        except Exception as _e:
            log(f"modelguard audit failed: {_e}")
        try:                     # prompt-rule drift: every builder must carry the shared rules block
            import baxter_rules as _rules
            if _rules.check(ping=True, quiet=True):
                log("RULES DRIFT- a prompt builder lost the shared rules block; see .baxter_rules_alert.json")
        except Exception as _e:
            log(f"rules check failed: {_e}")
        try:                     # fallback drift: baxter_watch.ps1's $slashWatchFallback is a
            # derivative of baxter_slash's import closure, and it only ever fires when the live
            # walk breaks- i.e. exactly when nobody is watching it. So it is checked while
            # everything still works. Since 10th July it is also REPAIRED here, in place, rather
            # than queued as a build: see maybe_fallback_repair.
            maybe_fallback_repair(state)
        except Exception as _e:
            log(f"fallback check failed: {_e}")
        try:                     # ack-before-act: no 'queued' reply without an entry behind it
            queue_ack_audit(ping=True)
        except Exception as _e:
            log(f"queue-ack audit failed: {_e}")
        items = []
        items += fetch_new_emails(state)
        items += fetch_new_discord(state)
        items += fetch_new_whatsapp(state)
        items += fetch_new_queue(state)
        items += fetch_claude_activity(state)
        if items:
            print(f"{len(items)} new item(s) -> dispatching")
            log(f"{len(items)} new: " + ", ".join(f"{i['source']}:{i.get('subject','')[:30]}" for i in items))
            state["seen_email"] += [i["id"] for i in items if i["source"] == "email"]
            save_state(state)          # persist cursors BEFORE dispatch - workers run parallel
            wake_claude(items)
            notify("🎩 Baxter", f"Caught {len(items)} new item(s) - check your dashboard.")
        # Resurrection reply-audit END-STAGE: fires on /on, a usage-cap resume, or a crash/
        # sleep gap- confirm vitals, answer any tickless (unreplied) message, reconcile emoji
        # LAST. Runs BEFORE the CATCHUP backfill block (which consumes the /on breadcrumb).
        maybe_resurrection_audit(state)
        # Backfill report (Atul's v2 ON ask): the first pass after a pause tells him what
        # landed while off, ONCE- not a brief-storm, not a silent drain. Skipped briefs
        # aren't re-fired (they're time-gated), so the only catch-up owed is the new items.
        if CATCHUP.exists():
            try:
                if items:
                    _say(f"📥 Caught up, sir- filed {len(items)} item(s) that arrived while I was paused.")
                CATCHUP.unlink()
            except Exception:
                pass
        # WhatsApp bridge logged out -> ping Atul once a day until he re-links
        relink = VAULT / ".baxter_wa_needs_relink.txt"
        if relink.exists() and state.get("last_wa_relink_ping") != datetime.now().strftime("%Y-%m-%d"):
            _say("📵 Sir- WhatsApp has unlinked the bridge (they expire every few weeks). "
                 "Say 'relink whatsapp' here and I'll post a fresh QR to scan.")
            state["last_wa_relink_ping"] = datetime.now().strftime("%Y-%m-%d")
        # #deals board: prune expired deals once a day (they only lapse at date boundaries;
        # a new deal already republishes via _food_deal). Cheap- one PATCH when the day rolls.
        if state.get("last_deals_sync") != datetime.now().strftime("%Y-%m-%d"):
            try:
                import baxter_deals as _bd
                _st = _bd._load()
                if _st.get("channel_id"):
                    _bd.publish(_st, _bd._secrets()["discord_bot_token"])
                state["last_deals_sync"] = datetime.now().strftime("%Y-%m-%d")
            except Exception as e:
                log(f"deals daily prune failed: {e}")
        # LANE REAPER (9th July) runs BEFORE the sweep: a lane whose worker is gone is a
        # corpse, not a build. It hands the slot back and routes the corpse into the failure
        # classifier, so the pump below sees the truth about how many lanes are free.
        reap_dead_lanes()
        # STUCK-TASK DOCTOR (10th July) runs on the reaper's heels: the corpses are gone, so
        # every lane it still sees is one that claims to be working. It never blocks the beat.
        stuck_doctor_tick()
        sweep_orphan_batches()
        # DELEGATOR periodic re-check (8th July): live touch-sets GROW mid-build, so two
        # lanes cleared at assignment can drift into each other. Runs BEFORE the pump so a
        # yielded lane is seen standing down on this pass, not the next.
        if _gov:
            try:
                for f in _gov.delegator_recheck():
                    log(f"delegator: {f}")
            except Exception as e:
                log(f"delegator recheck failed: {e}")
        maybe_resume(state)      # usage-refresh probe: drain queues + restart interrupted builds
        process_open_queue()
        process_projmove()
        process_mine_queue()
        chat = fetch_new_chat()
        if chat:
            run_baxter_chat(chat)
        photos = fetch_new_photos(state)
        if photos:
            triage_photos(photos)
        questions = fetch_new_questions()
        if questions:
            answer_questions(questions)
        maybe_reconcile(state)   # REVERSE pass: close finished tasks before the brief reflects them
        # OVERDUE AUTO-ROLL (hard rule, 5th July 12:04)- every pass, after reconcile has
        # ticked what's done (never roll a task that's about to close) and before any brief
        # reads the board. "Overdue is not a category he reads; today's list is."
        if _roll:
            try:
                moved = _roll.roll_once()
                if moved:
                    log(f"rolled {len(moved)} overdue task(s) to today")
            except Exception as e:
                log(f"overdue roll failed: {e}")
        maybe_remind(state)
        maybe_pulse(state)
        maybe_nudge(state)
        maybe_winddown(state)
        maybe_scan_projects(state)
        maybe_briefing(state)
        maybe_weekly(state)
        maybe_reprioritize(state)
        maybe_rejig(state)
        maybe_demote(state)
        maybe_followup(state)
        maybe_backup(state)
        maybe_exam_sweep(state)  # standing guard: a landed build's sealed exam keeps running
        save_state(state)
    finally:
        try: HEARTBEAT.write_text(datetime.now().isoformat(), encoding="utf-8")
        except Exception: pass
        try: LOCK.unlink()
        except Exception: pass

def selftest_verify_collide():
    """Prove the gate can tell a BROKEN build from an innocent one whose exam asserted on a
    surface a live sibling was in the middle of moving- the 10th July 00:28:45 case- and that
    it never turns a real failure green while doing so.

    Seven legs, each the mirror of one way this could go wrong. The last four exist because
    the first three passed a gate deliberately mutated to be useless:
      0. the surface is held by OUR OWN journal -> a build never collides with itself
      1. no sibling at all                      -> a red exam is a red build, as it always was
      2. a sibling touching elsewhere           -> a live lane is not an excuse
      3. an exam that names no surface at all   -> it blames nobody (clash() reads an empty
                                                   side as 'undeclared', i.e. clashing with all)
      4. a colliding sibling retires, code IS broken -> re-graded, and still FAILED
      5. a colliding sibling retires, code is sound  -> passed, no repair attempt spent
      6. a colliding sibling never retires           -> red verdict stands, bounded, no hang

    The siblings retire on the COLLISION LOG, never on a stopwatch: a build's exam is a real
    subprocess, and a timer racing it retires the owner before the gate has looked at it.

    Every outward path is stubbed- the ledger, the rejects log and `.baxter.log` all land in
    lists. A selftest that can write into the build record Atul reads is one that will."""
    import tempfile, shutil, threading
    global RESUME_DIR, log, _orch, _reload_bv, _reload_orch, COLLIDE_WAIT_S, COLLIDE_POLL_S
    keep = (RESUME_DIR, _gov.RESUME_DIR, _bv.record, _gov.record_reject, log, _orch,
            _reload_bv, _reload_orch, COLLIDE_WAIT_S, COLLIDE_POLL_S)
    # never "collide" in the prefix: the temp path lands inside the exam's own detail line,
    # and half these legs assert on the ABSENCE of the word from the log.
    tmp = Path(tempfile.mkdtemp(prefix="baxter-clashtest-"))
    logged, rejected = [], []
    udir = os.path.dirname(os.path.abspath(__file__))
    RESUME_DIR = _gov.RESUME_DIR = tmp
    log = lambda msg: logged.append(str(msg))
    _bv.record = lambda *a, **k: None
    _gov.record_reject = lambda *a, **k: rejected.append((a, k))
    _orch = None                      # no extras tier: these legs are about the sealed exam
    _reload_bv = _reload_orch = lambda: None
    OWNED = ["utils/baxter_usage.py/position_line"]     # the region lane 9 actually moved

    def exam(rc):
        """A one-line exam whose only surface is baxter_usage, so a sibling holding a REGION
        of that file collides with it and a sibling holding coc_bot does not."""
        p = tmp / "exam_mod.py"
        p.write_text(f"import sys\nsys.path.insert(0, {udir!r})\nimport baxter_usage\n"
                     f"sys.exit({rc})\n", encoding="utf-8")
        return p

    # bare `python`, never sys.executable: an unquoted interpreter path with a space in it is
    # a path fault `vet_verify_cmd` refuses outright, and the exam would never run a line.
    cmd = f"python {exam(1)}"
    BLIND = 'python -c "import sys; sys.exit(1)"'      # names no file, imports nothing of ours
    n = [0]

    def mine(verify=None, touch=None):
        n[0] += 1
        rf = tmp / f"resume-19700101-000000-{n[0]:06d}-0.json"
        rf.write_text(json.dumps({"task": "ack audit", "pid": os.getpid(), "lane": 0,
                                  "verify": verify or cmd,
                                  "touch_set": touch or ["utils/baxter_triage.py/_verify_gate"]}),
                      encoding="utf-8")
        return rf, json.loads(rf.read_text())

    def sibling(touch):
        n[0] += 1
        rf = tmp / f"resume-19700101-000000-{n[0]:06d}-3.json"
        rf.write_text(json.dumps({"task": "sibling build", "pid": os.getpid(), "lane": 3,
                                  "touch_set": touch}), encoding="utf-8")
        return rf

    def collided(since=0):
        return any("COLLIDED" in x for x in logged[since:])

    def retire_once_held(s, fix=None):
        """Retire the owning lane the moment the gate says it is waiting on it- and not one
        instant before. Returns the thread so the caller can join it back.

        Watches the log from HERE, never from the top: an earlier leg left its own COLLIDED
        line behind, and a thread reading the whole list retires the owner before the gate has
        even run the exam- so the build passes first time and the re-grade is never exercised."""
        since = len(logged)
        def run():
            for _ in range(600):
                if collided(since):
                    if fix:
                        fix()
                    try:
                        s.unlink()
                    except OSError:
                        pass
                    return
                time.sleep(0.05)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def gate(rf, e):
        t0 = time.time()
        v, d, _ = _verify_gate(rf, e)
        return v, d, time.time() - t0

    LEGAL = ("passed", "failed", "unverified")
    try:
        # Every leg is bounded: a mutation that loses a guard must fail this suite in seconds,
        # never hang it for the full twenty-minute production wait.
        COLLIDE_WAIT_S, COLLIDE_POLL_S = 2, 0.25

        # 0. SELF-COLLISION. Our own journal sits in RESUME_DIR and this gate runs before it is
        #    unlinked. A build that does not exclude itself by RESOLVED path owns the very
        #    surface its exam imports, blames itself, and waits out the whole window.
        rf, e = mine(touch=OWNED)
        v, _d, held = gate(rf, e)
        assert v == "failed", f"a build must not collide with its own journal, got {v}"
        assert not collided(), f"the gate blamed the build for its own touch-set: {logged}"
        rf.unlink()

        # 1. NO SIBLING. Nothing changed for the ordinary red build.
        rf, e = mine()
        v, _d, _h = gate(rf, e)
        assert v == "failed", f"a red exam with no live sibling must fail the build, got {v}"
        assert not collided(), logged
        rf.unlink()

        # 2. AN UNRELATED SIBLING. A lane merely being alive must never excuse a red exam-
        #    this is the leg that keeps the gate from failing nothing at all.
        s = sibling(["utils/coc_bot/"])
        rf, e = mine()
        v, _d, _h = gate(rf, e)
        assert v == "failed", f"a lane touching coc_bot must not excuse a red exam, got {v}"
        assert not collided() and not rejected, "an unrelated lane was logged as a collision"
        rf.unlink(); s.unlink()

        # 3. AN EXAM THAT NAMES NO SURFACE. `clash()` reads an empty side as 'undeclared- runs
        #    solo' and returns a reason, so an exam importing nothing of ours would otherwise
        #    collide with every lane alive, and no red build would ever fail again.
        s = sibling(["utils/coc_bot/"])
        rf, e = mine(verify=BLIND)
        v, _d, _h = gate(rf, e)
        assert v == "failed", f"an exam naming no surface must blame nobody, got {v}"
        assert not collided() and not rejected, \
            f"an exam with no surfaces was held against a live lane: {logged}"
        rf.unlink(); s.unlink()

        # 4. THE OWNER LANDS ITS OWN BREAKAGE. The sibling retires having genuinely broken the
        #    shared surface. The re-grade goes red and the build is FAILED- correctly, on the
        #    gate's own evidence. A collision DEFERS a verdict; it never supplies one.
        COLLIDE_WAIT_S = 30
        s = sibling(OWNED)
        rf, e = mine()
        t = retire_once_held(s)
        v, d, _h = gate(rf, e)
        t.join(5)
        assert v == "failed", f"a re-graded collision on broken code must still FAIL, got {v}"
        assert "re-graded" in d, f"the gate never re-ran the exam after the owner retired: {d}"
        assert collided() and rejected, "the hold happened but was never logged"
        rf.unlink()
        mark, marked_rejects = len(logged), len(rejected)

        # 5. THE INCIDENT ITSELF. The sibling owns the region the exam imports, retires, and
        #    the re-run passes. The journal still shows zero repair attempts, because this
        #    build was never broken- which is the whole point of the machinery.
        s = sibling(OWNED)
        rf, e = mine()
        t = retire_once_held(s, fix=lambda: exam(0))
        v, d, _h = gate(rf, e)
        t.join(5)
        assert v == "passed", f"a collided build must be re-graded once its owner retires: {v}"
        assert "re-graded" in d, f"the pass did not come from a re-grade: {d}"
        assert int(json.loads(rf.read_text()).get("repair_attempts", 0) or 0) == 0, \
            "a collision spent one of the build's three repair attempts"
        assert collided(mark), "the second collision went unlogged"
        assert len(rejected) > marked_rejects, \
            "the guard must record every collision hold in its own rejects log"
        rf.unlink()

        # 6. AN OWNER THAT NEVER RETIRES. The wait is bounded; the red verdict stands. A gate
        #    that hung here would hold a lane for ever, and one that went green would land a
        #    broken build on Atul's estate.
        COLLIDE_WAIT_S = 2
        exam(1)
        s = sibling(OWNED)
        rf, e = mine()
        v, d, held = gate(rf, e)
        assert v == "failed", f"an owner that never retires must not turn a red exam green: {v}"
        assert held < 40, f"the gate waited {held:.0f}s on an owner that never retires"
        assert "never" in d and "re-graded" not in d, \
            f"a timed-out collision must say so and must not claim a re-grade: {d}"
        rf.unlink(); s.unlink()

        # 7. THE CONTRACT. `_resume_worker` branches on 'failed' alone, so a fourth verdict
        #    string would sail past it, be announced as a landing and have its journal
        #    unlinked. 'collided' is a classification: it lives in the log, the ledger and the
        #    rejects file, and never in the tuple this gate hands back.
        verdicts = [m.group(1) for m in
                    (re.match(r"^verify gate on '.*': (\w+)-", x) for x in logged) if m]
        assert len(verdicts) == 7, f"seven gate calls, seven logged verdicts: {verdicts}"
        assert all(v in LEGAL for v in verdicts), \
            f"the gate logged a verdict outside its contract: {verdicts}"
        print(f"verify-collide: 7 legs pass ({len(rejected)} collision(s) logged to the guard)")
    finally:
        (RESUME_DIR, _gov.RESUME_DIR, _bv.record, _gov.record_reject, log, _orch,
         _reload_bv, _reload_orch, COLLIDE_WAIT_S, COLLIDE_POLL_S) = keep
        shutil.rmtree(tmp, ignore_errors=True)


def selftest_lanes():
    """Prove the verify + troubleshoot loop BITES- no Discord post, no claude spawn.

    A loop that only ever passes catches nothing, which is the whole reason this build
    exists: on 9th July a lane reported done because the worker said done. So each leg is
    driven against the real failure that motivated it, with the announce + spawn + queue
    edges captured rather than fired."""
    import tempfile, shutil
    global RESUME_DIR, BATCH_DIR, _announce_build, _spawn_resume, _usage_ok, _requeued, _say, log
    global _handle_failure
    keep = (RESUME_DIR, BATCH_DIR, _gov.RESUME_DIR, _bv.LEDGER,
            _announce_build, _spawn_resume, _usage_ok, _requeued, _say, _gov.enqueue,
            log, _gov.record_reject)
    tmp = Path(tempfile.mkdtemp(prefix="baxter-lanetest-"))
    said, spawned, queued, logged = [], [], [], []
    log_size_before = LOG.stat().st_size if LOG.exists() else 0
    rej_size_before = _gov.REJECT_LOG.stat().st_size if _gov.REJECT_LOG.exists() else 0
    RESUME_DIR = _gov.RESUME_DIR = tmp / "resume"; RESUME_DIR.mkdir()
    BATCH_DIR = tmp / "batches"; BATCH_DIR.mkdir()
    _bv.LEDGER = tmp / "outcomes.json"
    _announce_build = lambda line, logline: said.append(line)
    _spawn_resume = lambda rf: spawned.append(Path(rf).name)
    _usage_ok = lambda cls="routine": True
    _requeued = lambda task: False
    _gov.enqueue = lambda *a, **k: queued.append(k)
    # EVERY outward path is stubbed, not just the ones a test means to touch. The first run
    # of this selftest posted a false "usage reset" line into #general, because maybe_resume
    # reaches _say directly and only _announce_build had been captured. A test that can speak
    # to Atul is a test that will.
    _say = lambda msg, channel="reminders", mention=True: said.append(f"[say] {msg}")
    # ...and a test that can WRITE is a test that will. Leg 10 drives the real maybe_resume,
    # which logs a start line- so `.baxter.log` collected four phantom "started 'next in line'
    # (0 queued behind)" entries at 03:17-03:20 on 9th July, and six more this morning, every
    # one of them a lane that never existed. The log is a record Atul reads back; a selftest
    # writing invented history into it is the same class of bug as posting to his channel.
    # The reject log is the second such file, and it is stubbed here before it can grow one.
    log = lambda msg: logged.append(str(msg))
    _gov.record_reject = lambda *a, **k: None

    def journal(name, **kv):
        rf = RESUME_DIR / name
        d = {"task": "Build the thing", "lane": 0, "next_step": "n", "touch_set": ["utils/x.py"]}
        d.update(kv)
        rf.write_text(json.dumps(d), encoding="utf-8")
        return rf

    def age(rf, secs):
        t = time.time() - secs
        os.utime(rf, (t, t))

    try:
        # 1. GHOST LANE. A dead pid means a corpse NOW- not in 30 minutes. The slot must
        #    come back, and --lanes must stop calling it live (the 01:37 lie).
        rf = journal("resume-20260709-000000-000001-0.json", pid=999999)
        age(rf, 300)
        assert _gov.lane_journals() == [], "a lane whose process is gone must not count as live"
        assert len(_gov.dead_lanes()) == 1, "the corpse must be visible to the reaper"
        reap_dead_lanes()
        assert spawned == ["resume-20260709-000000-000001-0.retry.json"], spawned
        assert any("retrying once" in l for l in logged), logged
        assert said == [], f"a self-healing retry must not reach Atul, got {said}"
        assert not rf.exists(), "the corpse journal must have been renamed, not left in place"

        # 2. A LIVE build with a dead heartbeat thread is NOT a corpse. Reaping it would
        #    start a duplicate on top of a running build- the costly direction.
        live = journal("resume-20260709-000000-000002-1.json", pid=os.getpid())
        age(live, 3600)
        assert _gov.lane_alive(live, json.loads(live.read_text())), "a live pid is a live lane"
        assert len(_gov.dead_lanes()) == 0, "a running worker must never be reaped"
        live.unlink()

        # 3. THE TRAP. Exit 4294967295 under the benign Windows sandbox warning. Reading the
        #    top of that log diagnoses a sandbox fault. The truth is "it died"- retry it once.
        said.clear(); spawned.clear(); logged.clear()
        rf = journal("resume-20260709-000000-000003-0.json")
        _handle_failure(rf, json.loads(rf.read_text()), 4294967295,
                        "Sandbox disabled: sandbox is enabled but windows is not supported\n")
        assert spawned == ["resume-20260709-000000-000003-0.retry.json"], spawned
        assert any("retrying once" in l for l in logged), logged
        assert said == [], f"a self-healing retry must not reach Atul, got {said}"

        # 4. A REAL traceback is deterministic: a repair worker, not a blind retry. And the
        #    repair is SILENT- he is told nothing while the loop is still healing it (his
        #    9th-July order: self-diagnose and self-fix "before ever flagging a build failure").
        said.clear(); spawned.clear(); logged.clear()
        rf = journal("resume-20260709-000000-000004-0.json")
        _handle_failure(rf, json.loads(rf.read_text()), 1,
                        "Traceback (most recent call last):\nModuleNotFoundError: no module named x\n")
        assert spawned == ["resume-20260709-000000-000004-0.repair.json"], spawned
        assert said == [], f"a self-healing repair must not reach Atul, got {said}"
        assert any("repair attempt 1 of 3" in l for l in logged), logged
        nb = RESUME_DIR / spawned[0]
        assert json.loads(nb.read_text())["repair_pending"] is True, "the repair worker must know it is one"

        # 5. THE VERIFY GATE overrules the worker. A clean exit with a failing check is a
        #    FAILED build, and it is deterministic (the check will fail again).
        rf = journal("resume-20260709-000000-000005-0.json",
                     verify='python -c "import sys; sys.exit(1)"')
        v, _d, _e = _verify_gate(rf, {})
        assert v == "failed", "a non-zero verify must flip a claimed success to failed"
        rf = journal("resume-20260709-000000-000006-0.json",
                     verify='python -c "import sys; sys.exit(0)"')
        assert _verify_gate(rf, {})[0] == "passed", "a zero-exit verify proves it"
        assert _verify_gate(journal("resume-20260709-000000-000007-0.json"), {})[0] == "unverified", \
            "no declared check = unverified, never done"

        # 6. THE CAP, raised 2 -> 3 on 9th July. TWO repairs spent is no longer the end: the
        #    third attempt must still be SPAWNED, and still silently. This leg is the exact
        #    one that used to expect a park- if it ever expects one again, the cap regressed.
        said.clear(); spawned.clear(); logged.clear()
        rf = journal("resume-20260709-000000-000008-0.json", repair_attempts=2)
        _handle_failure(rf, json.loads(rf.read_text()), 1, "Traceback (most recent call last):\n")
        assert spawned == ["resume-20260709-000000-000008-0.repair.json"], \
            f"with 2 of 3 repairs spent a THIRD must run, not park: {spawned}"
        assert said == [], f"the third repair is still self-healing, not news: {said}"
        assert any("repair attempt 3 of 3" in l for l in logged), logged
        assert json.loads((RESUME_DIR / spawned[0]).read_text())["repair_attempts"] == 3

        # 6b. THE CAP SPENT. The third repair failed too -> PARK, announce it OUT LOUD, and
        #     re-queue gated on Atul. This is the ONLY thing that breaks the silence, and it
        #     must never be quiet: overnight, a silently parked queue looks like a drained one.
        said.clear(); spawned.clear()
        rf = journal("resume-20260709-000000-000018-0.json", repair_attempts=3)
        _handle_failure(rf, json.loads(rf.read_text()), 1, "Traceback (most recent call last):\n")
        assert spawned == [], "a parked task must never be respawned"
        assert any(s.startswith("Parked ") and "Say go" in s for s in said), \
            f"a spent cap MUST reach Atul- silence here is the silent failure state: {said}"
        assert queued and queued[-1].get("gated_on") == "atul", queued
        parked = RESUME_DIR / "resume-20260709-000000-000018-0.parked.json"
        assert parked.exists(), "the parked journal must be renamed out of the lanes"
        seen = [rf for rf, _e in _gov.lane_journals() + _gov.dead_lanes()]
        assert parked not in seen, "a parked task is neither a live lane nor a corpse to reap"

        # 7. ...and the 30-minute orphan sweep (the belt) must not undo the park.
        age(parked, 3600)
        sweep_orphan_batches()
        assert spawned == [], "the sweep must leave a parked task alone"

        # 8. A HUMAN-GATED task is never auto-repaired, whatever its log says. It parks on
        #    attempt ZERO and speaks up at once- the silent self-heal must never swallow work
        #    that is waiting on Atul, no matter how repairable the failure looks.
        said.clear(); spawned.clear(); queued.clear(); logged.clear()
        rf = journal("resume-20260709-000000-000009-0.json", gated_on="atul")
        _handle_failure(rf, json.loads(rf.read_text()), 1, "Traceback (most recent call last):\n")
        assert spawned == [] and any(s.startswith("Parked ") for s in said), (spawned, said)
        assert not any("repair attempt" in l for l in logged), \
            f"gated work must never spend a repair attempt: {logged}"

        # 9. Every outcome is passed on, not lost with the journal.
        rows = json.loads(_bv.LEDGER.read_text())
        assert any(r["outcome"] == "parked" for r in rows), "a park must leave its diagnosis"
        assert _bv.recent_summary(3), "the next lane must be able to read what happened before it"

        # 10. A retired lane takes the next task NOW. maybe_resume throttles itself to one
        #     try per 5 minutes, which is exactly the poll beat a freed lane must not wait
        #     for- so `--pump` forces past it. Lanes, capacity and queue are faked; nothing
        #     real is started and no meter is read.
        spawned.clear(); said.clear()
        held = _gov.queue_read, _gov.queue_write, _gov.lane_journals, _gov.lane_capacity
        _gov.queue_read = lambda: [{"task": "next in line", "next_step": "go",
                                    "touch_set": ["utils/z.py"], "priority": 5}]
        _gov.queue_write = lambda q: None
        _gov.lane_journals = lambda *a, **k: []
        _gov.lane_capacity = lambda: 1
        try:
            # the marker must MATCH the live meter, or maybe_resume reads a window roll and
            # skips the throttle for that reason instead of the one under test
            marker = json.loads((VAULT / ".baxter_usage.json").read_text(encoding="utf-8-sig"))
            st = {"last_resume_try": datetime.now().isoformat(),
                  "usage_reset_marker": marker.get("session_resets_at", "")}
            maybe_resume(dict(st))
            assert spawned == [], "without force, a fresh try must wait out the 5-minute throttle"
            maybe_resume(dict(st), force=True)
            assert len(spawned) == 1, f"a retired lane must start the next task at once, got {spawned}"
            assert not any(s.startswith("[say]") for s in said), \
                f"a chained start inside an open window must stay silent, got {said}"
        finally:
            _gov.queue_read, _gov.queue_write, _gov.lane_journals, _gov.lane_capacity = held

        # ---- THE VERTICAL TIER (9th July, his 00:11 "more levels of hierarchy") ----
        # These legs test the WIRING, not the module: baxter_orch --selftest already proves
        # the seal and the fan-out in isolation. What must hold HERE is that the lane's own
        # CLI and its verify gate route through the seal rather than around it.
        assert _orch is not None, "the planner tier must import into the lane"

        # 11. The lane's `--verify-cmd` goes THROUGH the seal. A builder handed a sealed
        #     journal cannot swap in a soft exam of its own- it can only add to it.
        srf = journal("resume-20260709-000000-000011-0.json",
                      verify='python -c "import sys; sys.exit(0)"',
                      acceptance_sealed="cmd")
        _soft = 'python -c "assert 1 == 1"'
        msg = _record_check(srf, "cmd", _soft)
        assert "SEALED" in msg, msg
        j = json.loads(srf.read_text())
        assert j["verify"] == 'python -c "import sys; sys.exit(0)"', \
            "the lane's CLI must not let a builder overwrite the planner's exam"
        assert j["verify_extra"] == [{"kind": "cmd", "value": _soft}], j

        # 11b. AN EXAM THAT CANNOT FAIL IS REFUSED BEFORE THE SEAL IS EVEN CONSULTED. This is
        #      the incident: a builder in a hurry declares `python -c "pass"`, the gate runs
        #      it, reads exit 0, and stamps the build `passed`. The refusal writes NOTHING-
        #      so the sealed exam and the extra recorded above are both untouched, and the
        #      builder is told what to declare instead rather than being failed silently.
        before = json.loads(srf.read_text())
        msg = _record_check(srf, "cmd", 'python -c "pass"')
        assert msg.startswith("REFUSED-") and "CANNOT FAIL" in msg, msg
        assert "go RED" in msg, f"the refusal must say what to declare instead: {msg}"
        after = json.loads(srf.read_text())
        assert after["verify"] == before["verify"], "a refusal must not disturb the seal"
        assert after["verify_extra"] == before["verify_extra"], \
            "a refused vacuous exam reached the journal anyway"
        # ...and the same exam, declared on an UNSEALED journal, is refused just as flatly.
        drf = journal("resume-20260709-000000-000011b-0.json")
        assert _record_check(drf, "cmd", "python -c pass").startswith("REFUSED-"), \
            "the bare unquoted `python -c pass` is the same dead exam, and must be refused"
        assert not json.loads(drf.read_text()).get("verify"), \
            "a refused exam must never reach an unsealed journal either"

        # 12. ...and an UNSEALED journal keeps the old behaviour exactly. Nothing regresses
        #     for a build the planner never reached.
        urf = journal("resume-20260709-000000-000012-0.json")
        _record_check(urf, "cmd", "python -m pytest -q")
        assert json.loads(urf.read_text())["verify"] == "python -m pytest -q"

        # 12b. THE POWERSHELL QUOTE-STRIP, end to end through the REAL CLI. `--verify-cmd` is
        #      shelled out to from PowerShell 5.1, which strips the double quotes around a
        #      native command's argument- so the argv this process receives is the path ALREADY
        #      SPLIT on its space, and joining it back yields an exam python exits 2 on without
        #      running a line. Drive the entry point on exactly that argv (never call the
        #      worker past its gate) and prove three things: the CLI refuses out loud, the
        #      journal gains NOTHING, and the build therefore reads UNVERIFIED, not FAILED.
        real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baxter_role_colours.py")
        assert os.path.isfile(real), f"the fixture this case is written against is gone: {real}"
        brf = journal("resume-20260709-000000-000012b-0.json")
        shredded = ("python " + real + " --selftest").split(" ")   # what PowerShell delivers
        assert len(shredded) > 3, "the fixture path must contain a space, or this proves nothing"
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--verify-cmd", str(brf)]
                           + shredded, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        assert p.returncode != 0, f"a refused verify command must exit non-zero: {out}"
        assert "REFUSED" in out and "space" in out.lower(), out
        assert real in out, f"the refusal must name the file the builder meant: {out}"
        jb = json.loads(brf.read_text())
        assert not str(jb.get("verify") or ""), f"a refused command must not reach the journal: {jb}"
        assert _bv.run_verify(jb)[0] == "unverified", \
            "a build with no recorded exam is UNVERIFIED- never FAILED on a command that never ran"

        # 12c. ...and the CLI echoes back what the JOURNAL HOLDS, not its own argv. A quoted
        #      path survives, is stored, and is read back off disk to confirm it.
        grf = journal("resume-20260709-000000-000012c-0.json")
        good = 'python "' + real + '" --selftest'
        m = _record_check(grf, "cmd", good)
        stored = json.loads(grf.read_text())["verify"]
        assert stored == good and stored in m and "stored as" in m.lower(), (stored, m)

        # 12d. NEWLINES SURVIVE STORAGE. Both record_check paths used to flatten the value
        #      before writing it, so a builder's multi-line `python -c` exam reached python as
        #      one line and died with SyntaxError- a guaranteed FAILED for a correct build
        #      (measured 9th July, lane 6). Drive the REAL CLI, on a SEALED journal, exactly as
        #      a builder does: the check is accepted, the journal keeps its newlines, the gate
        #      then runs it and it passes, and the line echoed back is still ONE line.
        nl = chr(10); q = chr(34)
        multi = q + sys.executable + q + " -c " + q + nl.join(
            ["import sys", "x = 2 + 2", "assert x == 4"]) + q
        # The SEALED exam is multi-line too, because it usually is now- so the one-line
        # assertion below covers the message that quotes it back, not just the echo.
        nrf = journal("resume-20260709-000000-000012d-0.json",
                      verify=multi, acceptance_sealed="cmd")
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--verify-cmd",
                            str(nrf), multi], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        assert p.returncode == 0, f"a valid multi-line check must be accepted: {out}"
        assert nl not in p.stdout.strip(), f"the confirmation must stay one line: {p.stdout!r}"
        jn = json.loads(nrf.read_text())
        vals = [r["value"] for r in jn["verify_extra"] if r["kind"] == "cmd"]
        assert vals and nl in vals[-1], f"STORED FLATTENED: {vals!r}"
        assert _orch.run_extras(jn)[0] is True, "the gate must pass a valid multi-line extra"

        # 12e. ...and the vet still stands. A multi-line source that does not COMPILE is
        #      refused out loud, and nothing whatever reaches the journal- the newline fix
        #      must not be bought by deleting the check that keeps garbage out.
        brf2 = journal("resume-20260709-000000-000012e-0.json",
                       verify='python -c "import sys; sys.exit(0)"', acceptance_sealed="cmd")
        broken = q + sys.executable + q + " -c " + q + nl.join(["import sys", "def ("]) + q
        p2 = subprocess.run([sys.executable, os.path.abspath(__file__), "--verify-cmd",
                             str(brf2), broken], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        out2 = (p2.stdout or "") + (p2.stderr or "")
        assert p2.returncode != 0, f"a non-compiling check must be refused: {out2}"
        assert "REFUSED" in out2, out2
        assert not json.loads(brf2.read_text()).get("verify_extra"), \
            "a non-compiling check must not be stored at all"

        # 13. THE GATE HONOURS BOTH. The sealed exam passes, but the extra check the builder
        #     added on top FAILS- so the build fails. A check the builder itself asked for
        #     and which then fails is a failing build; believing the failure is the safe way.
        frf = journal("resume-20260709-000000-000013-0.json",
                      verify='python -c "import sys; sys.exit(0)"', acceptance_sealed="cmd",
                      verify_extra=[{"kind": "cmd", "value": 'python -c "import sys; sys.exit(1)"'}])
        v, d, _e = _verify_gate(frf, {})
        assert v == "failed" and "extra check" in d, (v, d)
        #     ...and extras can only ever turn a pass into a fail, never a fail into a pass.
        prf = journal("resume-20260709-000000-000014-0.json",
                      verify='python -c "import sys; sys.exit(1)"', acceptance_sealed="cmd",
                      verify_extra=[{"kind": "cmd", "value": 'python -c "import sys; sys.exit(0)"'}])
        assert _verify_gate(prf, {})[0] == "failed", \
            "a passing extra must never rescue a failing sealed exam"

        # 14. The planner tier FAILS OPEN. A planner that dies must cost a build nothing:
        #     the lane runs on, unplanned, exactly as it did before this tier existed.
        held_plan = _orch.ensure_plan
        _orch.ensure_plan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("planner died"))
        try:
            orf = journal("resume-20260709-000000-000015-0.json")
            e = json.loads(orf.read_text())
            try:
                e = _orch.ensure_plan(orf, e) or e
            except Exception:
                pass   # exactly what _resume_worker does- log and carry on
            assert "plan" not in e and not e.get("acceptance_sealed")
            assert _verify_gate(orf, {})[0] == "unverified", \
                "an unplanned lane still lands unverified, never falsely done"
        finally:
            _orch.ensure_plan = held_plan

        # 15. A repair worker is never re-planned, and the prompt tells a sealed builder the
        #     truth about what it may and may not do.
        rrf = journal("resume-20260709-000000-000016-0.repair.json", repair_pending=True)
        e = _orch.ensure_plan(rrf, json.loads(rrf.read_text()),
                              runner=lambda p: (0, '{"acceptance":{"cmd":"x"}}'))
        assert "plan" not in e, "a repair worker chases a failure, it does not decompose a task"
        blk = _orch.plan_block(json.loads(srf.read_text()))
        assert "SEALED" in blk and "not yours to soften" in blk, blk

        # 16. THE WHOLE LANE, END TO END- because the legs above test functions, and what
        #     serves Atul is `_resume_worker`. Drive the real one with the claude spawn
        #     stubbed: the planner must seal BEFORE the executor is called, the executor's
        #     prompt must carry the plan and the seal, and the gate must run the sealed exam
        #     on the way out. (On 9th July a file-level check passed green while the running
        #     process served the old code. Test the path, not the parts.)
        global _pump_now
        keep2 = (_pump_now, subprocess.run, _orch.make_plan, _gov.blocked)
        # _resume_worker asks the REAL governor whether big work may run. Left unstubbed, this
        # leg passes below the big-stop and fails above it- so the selftest would go red at 81%
        # usage for reasons having nothing to do with the code under test, and a lane's own
        # acceptance test runs this. A test whose verdict depends on the hour is not a test.
        _gov.blocked = lambda kind="big": (False, "")
        seen = {}
        canned = {"summary": "seal it", "steps": [{"id": "s1", "goal": "do the thing",
                                                   "touch": [], "parallel": False, "check": ""}],
                  "acceptance": {"cmd": 'python -c "import sys; sys.exit(0)"', "claim": ""},
                  "risks": [], "planned_at": "now"}

        def _fake_run(argv, *a, **k):
            # the executor spawn: capture its prompt, claim a clean exit like a builder would
            if isinstance(argv, list) and "-p" in argv:
                seen["prompt"] = argv[argv.index("-p") + 1]
                seen["argv"] = argv
            class R: returncode, stdout, stderr = 0, "all done!", ""
            return R()
        _pump_now = lambda: seen.setdefault("pumped", True)
        subprocess.run = _fake_run
        _orch.make_plan = lambda entry, runner=None, timeout=None: dict(canned)
        try:
            e2e = journal("resume-20260709-000000-000017-0.json")
            _resume_worker(str(e2e))
            assert "prompt" in seen, "the executor was never spawned"
            # the planner ran FIRST and sealed the exam before the executor saw the task
            assert "SEALED" in seen["prompt"], "the executor was not told its exam is sealed"
            assert "do the thing" in seen["prompt"], "the plan never reached the executor"
            assert "not yours to soften" in seen["prompt"], seen["prompt"][-400:]
            assert "--fanout" in seen["prompt"], "the executor was not told it may fan out"
            assert "fable" not in " ".join(seen["argv"]).lower(), "a lane must never spawn Fable"
            # the builder claimed a clean exit; the sealed gate still ran and proved it
            assert not e2e.exists(), "a verified lane retires its journal"
            assert seen.get("pumped"), "a retired lane must pump the next task at once"
            rows = json.loads(_bv.LEDGER.read_text())
            assert rows[-1]["outcome"] == "verified", rows[-1]
            assert any("Verified" in s for s in said), said
        finally:
            _pump_now, subprocess.run, _orch.make_plan, _gov.blocked = keep2

        # 17. NOTHING THIS TEST DID REACHED A FILE ATUL READS. Legs 10 and 16 drive the real
        #     maybe_resume/_resume_worker, which log a start line each- and those lines went
        #     into `.baxter.log` as history of builds that never ran. Prove the capture holds:
        #     the fake start is in `logged`, and none of this test's fiction is in the log.
        #
        #     KEY ON CONTENT, NOT SIZE (9th July). A byte-for-byte size compare asks "did the
        #     file grow", and up to ten OTHER lanes append to `.baxter.log` while this runs-
        #     so the same command passed and failed a minute apart. Once these legs moved onto
        #     the sealed-acceptance path that coin-flip became a FALSE FAILED on a correct
        #     build. What the leg means is "did THIS test write", and only the fiction it
        #     invents can answer that.
        assert any("started 'next in line'" in m for m in logged), \
            "leg 10's start line must be captured, not merely absent"

        def _appended(path, before):
            if not path.exists():
                return ""
            with open(path, "rb") as f:
                f.seek(before)
                return f.read().decode("utf-8", "replace")

        FICTION = ("next in line", "resume-20260709-000000-", "Build the thing")
        for path, before, what in ((LOG, log_size_before, "a phantom build into the log Atul reads back"),
                                   (_gov.REJECT_LOG, rej_size_before, "an invented rejection into the guard's log")):
            leaked = [ln for ln in _appended(path, before).splitlines()
                      if any(m in ln for m in FICTION)]
            assert not leaked, f"a selftest wrote {what}: {leaked[:3]}"

        # 18. THE GRADER IS THE CODE ON DISK, not the copy bound when this worker spawned.
        #     `_verify_gate` reloads baxter_verify AND baxter_orch before it judges. Measured
        #     9th July: the lane that ADDED the POSIX/bash branch to `run_command` was graded
        #     by the pre-fix module still in its memory- the sealed exam hit cmd.exe, died at
        #     parse time, ran zero assertions, and a correct build was marked FAILED.
        #     The proof mutates those two modules ON DISK mid-lane and watches the verdict
        #     change. It lives in its own process and its own temp dir, against COPIES: up to
        #     ten live lanes import the real hub files, and a sentinel written into `utils/`
        #     would grade every one of them FAILED. This leg is the thread that keeps that
        #     harness wired into every lane's acceptance run.
        gate_check = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "baxter_reload_gate_check.py")
        assert os.path.isfile(gate_check), f"the reload-gate harness is gone: {gate_check}"
        gc = subprocess.run([sys.executable, gate_check], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=300)
        assert gc.returncode == 0, ("the verify gate is grading with a STALE module- "
                                    + ((gc.stdout or "") + (gc.stderr or ""))[-600:])

        # 19. A HELD BUILD IS NOT A GHOST LANE. The reaper used to skip idle journals only
        #     while the big band was still SHUT, so the first pass after it reopened classified
        #     every governor-held build as a silent death: five false FAILED lanes at 13:01 on
        #     9th July, each inheriting a 4294967295 exit and a tail from an EARLIER real death,
        #     each spawning a repair worker with nothing to repair. `_usage_ok` is True
        #     throughout this leg- the band is OPEN, which is precisely the condition that used
        #     to misfire. Runs in its own resume dir so `fails` names exactly one journal.
        import types
        from unittest import mock
        hold_dir = Path(tempfile.mkdtemp(prefix="baxter-heldtest-"))
        real_spawn = keep[5]
        assert getattr(real_spawn, "__name__", "") == "_spawn_resume", \
            "keep[] was reordered- this leg is no longer testing the real _spawn_resume"
        keep4 = (RESUME_DIR, _gov.RESUME_DIR, _gov.blocked, _handle_failure,
                 subprocess.Popen, getattr(reap_dead_lanes, "_held_seen", None))
        # Captured BEFORE any leg overwrites subprocess.Popen. The alive leg below runs while
        # the stillborn leg's lambda is still installed, so `spec=subprocess.Popen` read at
        # that point would spec a lambda- which has no `poll`- and hand back the very
        # AttributeError this fake exists to prevent.
        real_popen = subprocess.Popen
        assert isinstance(real_popen, type), \
            f"subprocess.Popen was already stubbed before the spawn legs: {real_popen!r}"
        RESUME_DIR = _gov.RESUME_DIR = hold_dir
        reap_dead_lanes._held_seen = None
        logged.clear()
        fails = []
        _handle_failure = lambda rf, entry, rc, tail, source="worker": fails.append(Path(rf).name)
        try:
            _gov.blocked = lambda kind="big": (True, "usage 82%")
            hrf = journal("resume-20260709-000000-000019-0.json", pid=999999,
                          last_exit=4294967295, last_tail="stale tail from an earlier death")
            _resume_worker(str(hrf))
            e = json.loads(hrf.read_text(encoding="utf-8-sig"))
            assert e.get("held"), "a held build must stamp its journal, or it looks like a corpse"
            assert e.get("pid") is None, "the pid of a build that never ran must be cleared"
            assert e.get("last_exit") is None and not e.get("last_tail"), \
                f"the inherited death must be scrubbed, or the classifier lies about it: {e}"

            age(hrf, 9999)                       # cold enough that nothing calls it live
            _gov.blocked = lambda kind="big": (False, "")     # THE BAND REOPENS
            assert [rf.name for rf, _e in _gov.lane_journals()] == [], "a hold occupies no lane"
            assert [rf.name for rf, _e in _gov.dead_lanes()] == [], "a hold is not a corpse"
            assert [rf.name for rf, _e in _gov.held_lanes()] == [hrf.name], "held_lanes must see it"

            reap_dead_lanes()
            assert fails == [], f"the reaper classified a HELD build as a ghost lane: {fails}"
            assert any("HELD by the governor" in l for l in logged), \
                f"a held-only pass must still say so- the log sat below the empty-corpses return: {logged}"
            n = len(logged)
            reap_dead_lanes()
            assert len(logged) == n, "the held line must be throttled on change, not logged every 15s pass"

            # ...and a REAL corpse beside it is still reaped. The predicate must discriminate,
            # not merely suppress: a reaper that never fires is the opposite bug.
            crf = journal("resume-20260709-000000-000020-1.json", pid=999999,
                          last_exit=1, last_tail="boom")
            age(crf, 9999)
            assert [rf.name for rf, _e in _gov.dead_lanes()] == [crf.name], \
                "a real corpse is no longer reaped"
            reap_dead_lanes()
            assert fails == [crf.name], f"the reaper missed the real corpse: {fails}"

            # A STILLBORN CHILD IS NOT A LANE. Popen returns a pid for a process that may
            # already be dead (bad interpreter, an import that blows up on line one). Writing
            # that pid describes a living lane that never was: it eats a fleet slot, and once
            # LANE_PID_GRACE lapses the reaper finds a corpse and routes a build that NEVER
            # RAN into the failure classifier- the exact 12:22 bug, one layer down. Left
            # body-less the journal reads un-spawned and the sweep respawns it.
            #
            # The fake is SPEC'D against the real subprocess.Popen, not hand-rolled. A bare
            # `SimpleNamespace(pid=4242)` is not a Popen, and stubbing one crashed this whole
            # selftest the moment _spawn_resume grew its `poll()` call- a fake that silently
            # falls behind the code it stands in for. `spec=` closes that class of break: any
            # attribute a real child has is auto-supplied, so the next thing _spawn_resume
            # reaches for cannot raise AttributeError mid-suite.
            #
            # `poll.return_value` is then set EXPLICITLY on BOTH legs, and must never be left
            # unset. An unset spec'd mock returns a truthy MagicMock from `poll()`, so
            # `p.poll() is None` is False and EVERY spawn silently reads stillborn- green
            # where the stillborn leg is concerned, and wrong everywhere else. The alive leg's
            # `pid == 4242` assertion is what catches that; `baxter_spawnstub_exam.py` mutates
            # each of these three lines in turn and fails unless this suite goes red.
            srf = journal("resume-20260709-000000-000021-2.json", held=True)
            stillborn_child = mock.MagicMock(spec=real_popen)
            stillborn_child.pid, stillborn_child.returncode = 4242, 1
            stillborn_child.poll.return_value = 1
            subprocess.Popen = lambda *a, **k: stillborn_child
            real_spawn(srf)
            e1 = json.loads(srf.read_text(encoding="utf-8-sig"))
            assert e1.get("pid") is None, \
                f"a stillborn spawn was blessed with the pid of a corpse: {e1}"
            assert any("stillborn" in l for l in logged), \
                f"a stillborn spawn must say so- it is silent lane loss otherwise: {logged}"

            # The hold ends where the pid is written, in ONE stamp. A separate second call
            # would leave a crash window that makes the journal permanently un-reapable.
            live_child = mock.MagicMock(spec=real_popen)
            live_child.pid, live_child.returncode = 4242, None
            live_child.poll.return_value = None
            subprocess.Popen = lambda *a, **k: live_child
            real_spawn(hrf)
            e2 = json.loads(hrf.read_text(encoding="utf-8-sig"))
            assert e2.get("pid") == 4242 and not e2.get("held"), \
                f"a respawned build must lose its hold, or it is immune to the reaper for ever: {e2}"
        finally:
            (RESUME_DIR, _gov.RESUME_DIR, _gov.blocked, _handle_failure,
             subprocess.Popen, reap_dead_lanes._held_seen) = keep4
            shutil.rmtree(hold_dir, ignore_errors=True)

        print("lane loop selftest OK: a dead pid frees its lane at once, a live one is never "
              "reaped, the benign sandbox line is never a cause, a traceback earns a repair "
              "worker, a failing check overrules a claimed success, three repairs self-heal "
              "in silence and the fourth parks it out loud behind Atul's gate, gated work "
              "parks without spending an attempt, the sweep leaves the park alone, a builder "
              "handed a sealed exam can strengthen it but never soften it, a real lane run "
              "plans, seals, executes and verifies in that order- and none of it left a mark "
              "in the log or the reject log.")
    finally:
        (RESUME_DIR, BATCH_DIR, _gov.RESUME_DIR, _bv.LEDGER, _announce_build, _spawn_resume,
         _usage_ok, _requeued, _say, _gov.enqueue, log, _gov.record_reject) = keep
        shutil.rmtree(tmp, ignore_errors=True)

def selftest():
    # The write pipeline is proved into a TEMP inbox, never his real one. Every sealed
    # acceptance gate runs `--selftest`, so this dropped a "SELFTEST demo item" note into
    # 00-Inbox on every build- 15 of them by lunchtime on 9th July, each carrying a
    # `- [ ] delete this selftest note` onto the task list that is supposed to hold only
    # what ATUL must do. A test that leaves work on his desk is not a passing test.
    import shutil, tempfile
    global INBOX
    real_inbox, tmp = INBOX, Path(tempfile.mkdtemp(prefix="baxter-selftest-"))

    def _watch(root):
        """Record every write THIS PROCESS makes under `root`, by filename.

        Attribution, not a directory diff. A bare before/after snapshot of 00-Inbox
        cannot tell a leaked note from one the live watcher filed a second earlier-
        and every sealed acceptance gate in the fleet runs `--selftest` while that
        watcher is up, so a diff would go red on innocent traffic. An audit hook sees
        only our own opens, so a concurrent write by the triage worker is invisible
        to it, and a leak from any leg- under any filename- is not."""
        root_s = os.path.abspath(str(root)).lower() + os.sep
        hits, live = set(), [True]
        def hook(event, args):
            if not live[0]:
                return
            try:
                if event == "open":
                    path, mode, flags = args
                    if path is None or isinstance(path, int):
                        return
                    writing = (bool(set(str(mode)) & set("wxa+")) if mode
                               else bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)))
                    if not writing:
                        return
                elif event in ("os.remove", "os.unlink", "os.rename", "os.replace",
                               "os.mkdir", "os.rmdir"):
                    path = args[0]
                else:
                    return
                p = os.path.abspath(os.fsdecode(path)).lower()
                if p.startswith(root_s):
                    hits.add(os.path.basename(p))
            except Exception:
                pass    # an audit hook that raises kills the operation it observed
        sys.addaudithook(hook)
        return hits, live

    before = {p.name for p in real_inbox.iterdir()} if real_inbox.is_dir() else set()
    touched, live = _watch(real_inbox)
    try:
        INBOX = tmp / "00-Inbox"
        INBOX.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H%M")
        note = INBOX / f"{stamp} - SELFTEST demo item.md"
        note.write_text(
            "---\nsource: selftest\nproject: baxter\nneeds_attention: false\n---\n"
            "# Selftest demo item\n\n**TL;DR.** Write pipeline works - this note was "
            "created by baxter_triage.py.\n\n- [ ] delete this selftest note #baxter\n",
            encoding="utf-8")
        body = note.read_text(encoding="utf-8")
        assert note.exists() and "source: selftest" in body, "the note write pipeline is broken"
        assert not list(real_inbox.glob("* - SELFTEST demo item.md")), \
            "a selftest left a demo note in the inbox Atul reads"
        print(f"wrote {note.name} (temp inbox- his own is left alone)")
        INBOX = real_inbox
        # THE BUILD PROMPT'S RED-PROOF FENCE RULE (10th July). baxter_rules.check()
        # guards this too, but a lane editing THIS file runs THIS suite- and the prompt it
        # would break lives here. Without the rule a red-proof never declares its window,
        # its byte-mutation of a hub stays invisible, and a sibling lane's verify gate is
        # graded FAILED for a fault it never caused. Rendered, never grepped.
        assert _rules.REDPROOF_FENCE_RULE in build_worker_prompt(), \
            "build_worker_prompt dropped REDPROOF_FENCE_RULE- a red-proof will not " \
            "declare its window, and it will condemn a sibling lane's verify gate"
        assert _rules.REDPROOF_FENCE_RULE in build_worker_prompt(repair=True), \
            "the diagnose-and-repair worker's prompt dropped REDPROOF_FENCE_RULE"
        # ...and the LANE LOOP, which is part of the suite rather than an opt-in flag. Until
        # 9th July these legs sat behind `--selftest-lanes`, which nothing called- so `--selftest`,
        # the command every sealed acceptance gate runs, exercised not one line of the verify /
        # repair / park machinery. Reverting MAX_REPAIRS 3 -> 2 passed it green. A suite that
        # cannot see a cap regression is not guarding the cap. It runs INSIDE the guard: it
        # used to sit after the finally, where nothing watched what it wrote.
        selftest_lanes()
        # ...and the COLLISION guard on top of it: the gate must still fail a broken build
        # while a sibling lane is alive, or the 10th-July fix would have quietly disarmed it.
        selftest_verify_collide()
    finally:
        INBOX = real_inbox
        shutil.rmtree(tmp, ignore_errors=True)
        live[0] = False
        problems = []
        if touched:
            problems.append("wrote into the inbox Atul reads: " + ", ".join(sorted(touched)))
        if tmp.exists():
            problems.append(f"left its temp inbox behind at {tmp}")
        after = {p.name for p in real_inbox.iterdir()} if real_inbox.is_dir() else set()
        added, removed = sorted(after - before), sorted(before - after)
        if (added or removed) and not touched:
            print(f"note: 00-Inbox changed under us but not by us (added={added} "
                  f"removed={removed})- live triage traffic, not a leak")
        if problems:
            leak = "selftest leak guard- the suite " + "; ".join(problems)
            if sys.exc_info()[0] is not None:
                # Never mask the failure we were meant to sit beside: the lane-loop
                # traceback is the finding, the leak is a footnote on it.
                print("WARNING- " + leak)
            else:
                raise AssertionError(leak)

def capture(text):
    """Quick-capture a thought (from the hotkey) straight through triage."""
    item = [{"id": "cap-" + datetime.now().strftime("%H%M%S"), "source": "quickcapture",
             "from": "Atul", "subject": "quick capture", "received": "", "body": text}]
    log(f"quick-capture: {text[:60]}")
    wake_claude(item)
    notify("🎩 Baxter", "Captured & filing your note.")

if __name__ == "__main__":
    if "--worker" in sys.argv:
        i = sys.argv.index("--worker")
        _worker_run(sys.argv[i + 1])
    elif "--resume-worker" in sys.argv:
        i = sys.argv.index("--resume-worker")
        _resume_worker(sys.argv[i + 1])
    elif "--announce" in sys.argv:
        # a builder writing its OWN landing line into its journal, before it exits;
        # _announce_stop posts it the moment the lane retires
        i = sys.argv.index("--announce")
        _journal_set(sys.argv[i + 1], announce=" ".join(sys.argv[i + 2:]).strip())
        print("announce line recorded")
    elif "--verify-cmd" in sys.argv:
        # A builder declaring how its work is proven. THROUGH THE SEAL: if a planner already
        # wrote this build's acceptance test, the builder's command is recorded as an extra
        # that must also pass- it can strengthen its exam, never soften it. Unsealed, this
        # behaves exactly as it always did.
        i = sys.argv.index("--verify-cmd")
        _msg = _record_check(sys.argv[i + 1], "cmd", " ".join(sys.argv[i + 2:]))
        print(_msg)
        # A refusal exits NON-ZERO. The builder shells out to this; a silent zero is how it
        # learned to trust a command that was never recorded.
        if _msg.startswith("REFUSED-"):
            sys.exit(2)
    elif "--verify-assert" in sys.argv:
        i = sys.argv.index("--verify-assert")
        print(_record_check(sys.argv[i + 1], "claim", " ".join(sys.argv[i + 2:])))
    elif "--stamp-governor-kill" in sys.argv:
        # Called by baxter_watch.ps1's Invoke-UsageEnforce, immediately BEFORE Stop-Process.
        # Fails open and silent: a missed stamp costs a wasted repair worker, a raised
        # exception here could delay the 80% kill it precedes.
        try:
            i = sys.argv.index("--pids")
            _pids = [p for p in sys.argv[i + 1].replace(";", ",").split(",") if p.strip()]
        except Exception:
            _pids = []
        _lvl = "big"
        if "--level" in sys.argv:
            j = sys.argv.index("--level")
            if len(sys.argv) > j + 1:
                _lvl = sys.argv[j + 1]
        try:
            print(f"stamped {_stamp_governor_kill(_pids, _lvl)} journal(s)")
        except Exception as _e:
            log(f"governor stamp failed: {_e}")
    elif "--pump" in sys.argv:
        _pump_once()
    elif "--reap" in sys.argv:
        reap_dead_lanes()
    elif "--stuck-tick" in sys.argv:
        # force=True: the 60s throttle exists for the beat, not for a hand-run probe, and a
        # throttled no-op here would leave a stale stamp behind claiming the pass ran.
        for _r in stuck_doctor_tick(dry_run="--dry-run" in sys.argv, force=True):
            print(f"lane {_lane_no(_r.get('lane', '?'))}: {_r['verdict']}")
    elif "--selftest-lanes" in sys.argv:
        selftest_lanes()
    elif "--selftest-collide" in sys.argv:
        selftest_verify_collide()
    elif "--selftest" in sys.argv:
        selftest()
    elif "--followup" in sys.argv:
        st = load_state()
        maybe_followup(st, force=True)
        save_state(st)
    elif "--reconcile" in sys.argv:
        st = load_state()
        maybe_reconcile(st, force=True)
        save_state(st)
    elif "--queue-ack-audit" in sys.argv:
        # Never pings by hand- a run in a terminal must not buzz his phone (same bargain
        # baxter_rules.check strikes). The proactive pass is the only caller that pings.
        bad = queue_ack_audit(ping=False)
        print(f"queue-ack audit: {len(bad)} unbacked 'queued' claim(s)"
              + (": " + ", ".join(bad) if bad else " - every claim has an entry behind it"))
        sys.exit(1 if bad else 0)
    elif "--capture" in sys.argv:
        i = sys.argv.index("--capture")
        txt = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        if txt.strip():
            capture(txt)
    else:
        run()
