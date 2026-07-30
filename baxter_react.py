"""baxter_react — add or remove a reaction on a Discord message, for Baxter's message
lifecycle receipts: 👀 seen/logged  ->  ⚙️ actively working  ->  ✅ done (⚙️ removed).
Bot token only, the owner's own server. Not double-send-guarded (reactions are idempotent).

  python baxter_react.py --add    <channel> <message_id> <emoji>
  python baxter_react.py --remove <channel> <message_id> <emoji>
  python baxter_react.py --done   <channel> <message_id>      # remove ⚙️, add ✅ (atomic 'completed')
  python baxter_react.py --handsoff <channel> <message_id>    # remove ⚙️, add 🤚 (atomic 'seen, not mine, no action needed'- the owner, 8th July: distinct from the tick)
"""
import json, sys, time, urllib.error, urllib.request, urllib.parse

SECRETS = r"C:\Users\you\Documents\Baxter\.baxter_secrets.json"
UA = "DiscordBot (https://baxter.local, 1.0)"
WORKING = "⚙️"   # ⚙️
DONE = "✅"            # ✅
HANDSOFF = "🤚"        # seen + evaluated, correctly not mine to act on- never the tick

def _call(ch, mid, emoji, method, tok):
    url = (f"https://discord.com/api/v10/channels/{ch}/messages/{mid}"
           f"/reactions/{urllib.parse.quote(emoji)}/@me")
    headers = {"Authorization": f"Bot {tok}", "User-Agent": UA}
    if method == "PUT":
        headers["Content-Length"] = "0"
    req = urllib.request.Request(url, method=method, headers=headers)
    for attempt in range(4):
        try:
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:   # reaction route rate limit- honour Retry-After
                time.sleep(float(e.headers.get("Retry-After") or 2) + 0.5)
                continue
            print(f"react {method} {emoji} failed: {e}")
            return False
        except Exception as e:
            print(f"react {method} {emoji} failed: {e}")
            return False

def main():
    a = sys.argv[1:]
    if len(a) < 3 or a[0] not in ("--add", "--remove", "--done", "--handsoff"):
        print("usage: baxter_react.py --add|--remove <ch> <mid> <emoji>  |  --done <ch> <mid>  |  --handsoff <ch> <mid>")
        return 1
    tok = json.load(open(SECRETS, encoding="utf-8-sig"))["discord_bot_token"]
    op, ch, mid = a[0], a[1], a[2]
    if op == "--done":
        _call(ch, mid, WORKING, "DELETE", tok)   # remove the working gear
        ok = _call(ch, mid, DONE, "PUT", tok)     # stamp the tick
        print("done" if ok else "done (tick failed)")
        return 0 if ok else 1
    if op == "--handsoff":
        _call(ch, mid, WORKING, "DELETE", tok)     # remove the working gear, if present
        ok = _call(ch, mid, HANDSOFF, "PUT", tok)  # stamp hands-off, never the tick
        print("handsoff" if ok else "handsoff (react failed)")
        return 0 if ok else 1
    emoji = " ".join(a[3:]) or WORKING
    ok = _call(ch, mid, emoji, "PUT" if op == "--add" else "DELETE", tok)
    print(f"{op[2:]}: {'ok' if ok else 'failed'}")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
