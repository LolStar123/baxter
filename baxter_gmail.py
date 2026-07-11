"""baxter_gmail — full Gmail WRITE-side parity across ALL accounts over IMAP.

Why this exists (Atul, 6th July 2026): the hosted Claude Gmail bridge only wires
label/draft/sensitive-label tools to alice.personal. Reading was already universal
(IMAP covers all three inboxes), but swagg + company were second-class on the write
side. This makes every account equal- no bridge-only account, nothing sacrificed.

Route: IMAP, not a second OAuth bridge. All three accounts already hold app
passwords in .baxter_secrets.json, and Gmail's IMAP extensions expose everything
the bridge write-tools do:
  - labels   -> X-GM-LABELS (add/remove; applying a new label auto-creates it)
  - search   -> X-GM-RAW (full Gmail search syntax)
  - threads  -> X-GM-THRID (fetch a whole conversation by thread id)
  - drafts   -> APPEND a MIME message to [Gmail]/Drafts with the \\Draft flag
A "sensitive" label is just a Gmail label- IMAP applies any label identically, so
there is no capability the bridge has that this lacks.

Accounts (aliases): main/atul1 -> alice.personal, swagg -> alice.spam,
company -> alice.business, all -> every account.

Commands:
  labels         [--account X]                         list labels
  create-label   <name> [--account X]                  create a label (nest with /)
  search         <gmail-query> [--limit N] [--account X]
  thread         <thrid> [--account X]                  print a full conversation
  label          <label> (--query Q | --uid U) [--account X] [--sensitive]
  unlabel        <label> (--query Q | --uid U) [--account X]
  draft          --to A --subject S (--body B | --body-file F)
                 [--cc C] [--in-reply-to MSGID] [--account X]
  drafts         [--account X]                          list drafts

--account defaults to atul1 for write ops (safety) and to all for read ops.
Add --json for machine-readable output. Nothing is ever sent- drafts only.
Nothing is hard-deleted- unlabel just removes a label.
"""
import argparse
import email
import imaplib
import json
import re
import sys
import time
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

VAULT = Path(r"C:\Users\you\Documents\Baxter")
SECRETS = VAULT / ".baxter_secrets.json"

ALL_MAIL = '"[Gmail]/All Mail"'
DRAFTS = '"[Gmail]/Drafts"'

ALIASES = {
    "main": "alice.personal", "atul1": "alice.personal", "alice.personal": "alice.personal",
    "swagg": "alice.spam", "alice.spam": "alice.spam",
    "company": "alice.business", "alice.business": "alice.business",
}


def _secrets():
    return json.loads(SECRETS.read_text(encoding="utf-8-sig"))


def _accounts():
    """Return [(address, app_password), ...] for every account with a password."""
    return [(a["address"], a["app_password"])
            for a in _secrets().get("gmail_accounts", [])
            if a.get("address") and a.get("app_password")]


def _resolve(account):
    """Map an --account value to a list of (address, pw). 'all' -> every account."""
    accts = _accounts()
    if account in (None, "all"):
        return accts
    key = ALIASES.get(account.lower())
    if key:
        for addr, pw in accts:
            if addr.split("@")[0] == key:
                return [(addr, pw)]
    # allow a full address too
    for addr, pw in accts:
        if addr.lower() == account.lower():
            return [(addr, pw)]
    sys.exit(f"unknown account '{account}'- known: main/atul1, swagg, company, all")


def _connect(addr, pw):
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(addr, pw)
    return M


def _dec(raw):
    try:
        return " ".join(t.decode(enc or "utf-8", "replace") if isinstance(t, bytes) else t
                        for t, enc in decode_header(raw or ""))
    except Exception:
        return raw or ""


def _fmt_label(label):
    """System labels (\\Starred etc) go raw; user labels get quoted."""
    return label if label.startswith("\\") else '"%s"' % label.replace('"', '')


def _label_arg(labels):
    parts = [_fmt_label(x) for x in labels]
    return "(%s)" % " ".join(parts) if len(parts) > 1 else parts[0]


def _thrid_of(resp_line):
    m = re.search(rb"X-GM-THRID (\d+)", resp_line or b"")
    return m.group(1).decode() if m else ""


def _uid_of(resp_line):
    m = re.search(rb"UID (\d+)", resp_line or b"")
    return m.group(1).decode() if m else ""


def _fetch_headers(M, uids, extra_thrid=True):
    """uids: list of str/bytes uids. Returns list of dicts with uid, thrid, from, subject, date, msgid."""
    out = []
    fields = "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)]"
    items = f"(X-GM-THRID {fields})" if extra_thrid else f"({fields})"
    uids = [u.decode() if isinstance(u, bytes) else str(u) for u in uids]
    for i in range(0, len(uids), 200):
        batch = ",".join(uids[i:i + 200])
        typ, md = M.uid("fetch", batch, items)
        if typ != "OK" or not md:
            continue
        for part in md:
            if not (isinstance(part, tuple) and len(part) == 2):
                continue
            meta, raw = part[0], part[1]
            hdr = email.message_from_bytes(raw)
            out.append({
                "uid": _uid_of(meta),
                "thrid": _thrid_of(meta) if extra_thrid else "",
                "from": _dec(hdr.get("From")),
                "subject": _dec(hdr.get("Subject")),
                "date": (hdr.get("Date") or "").strip(),
                "msgid": (hdr.get("Message-ID") or "").strip(),
            })
    return out


def _emit(rows, as_json, headline=None):
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    if headline:
        print(headline)
    if not rows:
        print("  (none)")
        return
    for r in rows:
        if isinstance(r, str):
            print(f"  {r}")
        else:
            frm = re.sub(r"\s+", " ", r.get("from", ""))[:38]
            subj = re.sub(r"\s+", " ", r.get("subject", ""))[:60]
            print(f"  uid {r.get('uid',''):>7}  thr {r.get('thrid',''):>19}  "
                  f"{frm:<38}  {subj}")


# ---- commands ---------------------------------------------------------------

def cmd_labels(args):
    for addr, pw in _resolve(args.account):
        M = _connect(addr, pw)
        typ, data = M.list()
        M.logout()
        names = []
        for line in data or []:
            s = line.decode() if isinstance(line, bytes) else line
            m = re.search(r'"[^"]*" "?([^"]+)"?$', s) or re.search(r'\)\s+".*"\s+(.+)$', s)
            name = (m.group(1).strip('"') if m else s.split()[-1]).strip()
            if name and name != "[Gmail]":
                names.append(name)
        _emit(sorted(set(names)), args.json, f"[{addr.split('@')[0]}] labels:")


def cmd_create_label(args):
    for addr, pw in _resolve(args.account or "atul1"):
        M = _connect(addr, pw)
        typ, resp = M.create(args.name)
        M.logout()
        msg = (resp[0].decode() if resp and isinstance(resp[0], bytes) else str(resp))
        ok = typ == "OK" or "ALREADYEXISTS" in msg.upper() or "already exists" in msg.lower()
        print(f"[{addr.split('@')[0]}] create-label '{args.name}': "
              f"{'ok' if ok else 'FAILED- ' + msg}")


def _gm_raw(query):
    """X-GM-RAW must arrive as ONE quoted IMAP string: imaplib joins args with spaces,
    so a bare multi-term query ("from:uber newer_than:400d") reaches the server as
    several search keys and dies with BAD Could not parse command."""
    return '"%s"' % query.replace("\\", "\\\\").replace('"', '\\"')


def _search_uids(M, query, limit=None):
    M.select(ALL_MAIL, readonly=True)
    typ, data = M.uid("search", None, "X-GM-RAW", _gm_raw(query))
    uids = (data[0].split() if data and data[0] else [])
    if limit:
        uids = uids[-int(limit):]     # most-recent last in Gmail's uid order
        uids = list(reversed(uids))   # show newest first
    return uids


def cmd_search(args):
    # --json emits ONE merged array across accounts, so a caller can json.loads the
    # whole of stdout; uids collide between mailboxes, so each row carries `account`.
    merged = []
    for addr, pw in _resolve(args.account):
        M = _connect(addr, pw)
        uids = _search_uids(M, args.query, args.limit)
        rows = _fetch_headers(M, uids)
        M.logout()
        acct = addr.split("@")[0]
        for r in rows:
            r["account"] = acct
        if args.json:
            merged.extend(rows)
        else:
            _emit(rows, False, f"[{acct}] search '{args.query}' ({len(rows)} shown):")
    if args.json:
        _emit(merged, True)


def cmd_thread(args):
    for addr, pw in _resolve(args.account):
        M = _connect(addr, pw)
        M.select(ALL_MAIL, readonly=True)
        typ, data = M.uid("search", None, "X-GM-THRID", str(args.thrid))
        uids = (data[0].split() if data and data[0] else [])
        rows = _fetch_headers(M, uids)
        M.logout()
        _emit(rows, args.json,
              f"[{addr.split('@')[0]}] thread {args.thrid} ({len(rows)} messages):")


def _apply_label(add, args):
    if not args.query and not args.uid:
        sys.exit("label/unlabel needs --query <gmail search> or --uid <uid[,uid]>")
    op = "+X-GM-LABELS" if add else "-X-GM-LABELS"
    larg = _label_arg([args.label])
    for addr, pw in _resolve(args.account or "atul1"):
        M = _connect(addr, pw)
        M.select(ALL_MAIL)  # writable
        if args.uid:
            uids = [u.strip() for u in str(args.uid).split(",") if u.strip()]
        else:
            typ, data = M.uid("search", None, "X-GM-RAW", _gm_raw(args.query))
            uids = [u.decode() for u in (data[0].split() if data and data[0] else [])]
        done = 0
        for i in range(0, len(uids), 200):
            batch = ",".join(uids[i:i + 200])
            if not batch:
                continue
            typ, resp = M.uid("store", batch, op, larg)
            if typ == "OK":
                done += len(uids[i:i + 200])
            else:
                print(f"  store failed: {resp}")
            time.sleep(0.1)
        M.logout()
        verb = "labelled" if add else "unlabelled"
        note = " [sensitive]" if getattr(args, "sensitive", False) and add else ""
        print(f"[{addr.split('@')[0]}] {verb} {done} msg(s) '{args.label}'{note}")


def cmd_label(args):
    _apply_label(True, args)


def cmd_unlabel(args):
    _apply_label(False, args)


def cmd_draft(args):
    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    if body is None:
        sys.exit("draft needs --body or --body-file")
    for addr, pw in _resolve(args.account or "atul1"):
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = addr
        msg["To"] = args.to
        if args.cc:
            msg["Cc"] = args.cc
        msg["Subject"] = args.subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=addr.split("@")[1])
        if args.in_reply_to:
            irt = args.in_reply_to if args.in_reply_to.startswith("<") else f"<{args.in_reply_to}>"
            msg["In-Reply-To"] = irt
            msg["References"] = irt
        M = _connect(addr, pw)
        typ, resp = M.append(DRAFTS, "\\Draft",
                             imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        M.logout()
        msgtxt = (resp[0].decode() if resp and isinstance(resp[0], bytes) else str(resp))
        print(f"[{addr.split('@')[0]}] draft to {args.to} re '{args.subject}': "
              f"{'saved to Drafts' if typ == 'OK' else 'FAILED- ' + msgtxt}")


def cmd_drafts(args):
    for addr, pw in _resolve(args.account):
        M = _connect(addr, pw)
        M.select(DRAFTS, readonly=True)
        typ, data = M.uid("search", None, "ALL")
        uids = (data[0].split() if data and data[0] else [])
        rows = _fetch_headers(M, uids, extra_thrid=False)
        M.logout()
        _emit(rows, args.json, f"[{addr.split('@')[0]}] drafts ({len(rows)}):")


def main():
    p = argparse.ArgumentParser(prog="baxter_gmail",
                                description="Full Gmail write-side parity across all accounts over IMAP.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("labels"); s.add_argument("--account"); s.set_defaults(fn=cmd_labels)

    s = sub.add_parser("create-label"); s.add_argument("name"); s.add_argument("--account")
    s.set_defaults(fn=cmd_create_label)

    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--limit", type=int, default=25)
    s.add_argument("--account"); s.set_defaults(fn=cmd_search)

    s = sub.add_parser("thread"); s.add_argument("thrid"); s.add_argument("--account")
    s.set_defaults(fn=cmd_thread)

    s = sub.add_parser("label"); s.add_argument("label")
    s.add_argument("--query"); s.add_argument("--uid"); s.add_argument("--account")
    s.add_argument("--sensitive", action="store_true",
                   help="acknowledge the target may be sensitive (parity with the bridge's sensitive-label tools)")
    s.set_defaults(fn=cmd_label)

    s = sub.add_parser("unlabel"); s.add_argument("label")
    s.add_argument("--query"); s.add_argument("--uid"); s.add_argument("--account")
    s.set_defaults(fn=cmd_unlabel)

    s = sub.add_parser("draft")
    s.add_argument("--to", required=True); s.add_argument("--subject", required=True)
    s.add_argument("--body"); s.add_argument("--body-file")
    s.add_argument("--cc"); s.add_argument("--in-reply-to"); s.add_argument("--account")
    s.set_defaults(fn=cmd_draft)

    s = sub.add_parser("drafts"); s.add_argument("--account"); s.set_defaults(fn=cmd_drafts)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
