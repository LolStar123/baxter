"""baxter_siblings - the ONE addressed-to check. Is this message the owner talking to Codex/Jem?

WHY THIS IS A MODULE AND NOT A LINE (the owner, 11th July 00:51). Baxter answered two messages that
plainly began `<@codex>`, four minutes apart, and burned an Opus reply worker on each. The
standback rule DID exist- in `baxter_slash._route` alone. The fast lane (`baxter_fast`), which
polls the same channel every ~15s, had never heard of a sibling bot: three separate selections
there (`pre_enqueue`'s big-ask list, the wall catch-all, and `targets`) filtered on Baxter's own
bot id and nothing else. One rule in one of the two paths that can speak is not a rule.

So the rule lives HERE, once, and both callers import it. A fourth path added later imports it
too, or it is wrong in exactly the same way.

TWO WAYS A MESSAGE IS ADDRESSED TO A SIBLING:
  1. an @mention of the sibling's id (`<@id>` or the legacy `<@!id>` bang form);
  2. a LEADING VOCATIVE- `codex, restart yourself` / `jem: status?`- the name in the first
     position, followed by a comma or colon, or standing alone as the whole message.

A name in the middle of a sentence is ABOUT the sibling, not addressed to it: `ask codex why he
crashed` and `codex is down, fix him` are both the owner talking TO Baxter, and both stay `handle`.
That is why the vocative demands a comma/colon delimiter rather than a bare trailing space- a
space would swallow `codex is down` and silence Baxter on the very message asking him to help.

DEGRADES TO TODAY'S CONDUCT, NEVER TO SILENCE. An unreadable/absent `.baxter_secrets.json`
yields an EMPTY id set: mention-matching then finds nothing, the vocative still works, and
everything else routes as it does today (answer it). It can never mute Baxter on a message
addressed to Baxter.

Deciding whether Baxter was ALSO addressed is the caller's job- it holds the mention list
(the listener) or the raw content (the fast lane). This module answers one question only.
"""
import json
import os
import re

VAULT = r"C:\Users\you\Documents\Baxter"
SECRETS = os.path.join(VAULT, ".baxter_secrets.json")

# The keys .baxter_secrets.json uses for the two sibling bots' client ids.
SIBLING_KEYS = ("codex_bot_client_id", "jemini_bot_client_id")

# A leading vocative: the name FIRST, then a comma/colon (or nothing at all- a bare "codex").
# Deliberately narrow; see the module docstring for why a trailing space is not enough.
VOCATIVE = re.compile(r"^\s*(codex|jem|jemini)\s*(?:[,:]|$)", re.I)

_CACHE = {}   # path -> (mtime_ns, size, ids). A poll reads this every sweep; a stat is enough.


def sibling_ids(path=None):
    """The sibling bots' client ids, read from `.baxter_secrets.json`. Cached on the file's
    (mtime, size) so the ~15s poll does not re-parse it every sweep, and so a test pointing
    SECRETS at a fixture is picked up on the next call rather than frozen at import."""
    path = str(path or SECRETS)
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        _CACHE.pop(path, None)
        return set()
    hit = _CACHE.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        ids = {str(d[k]) for k in SIBLING_KEYS if d.get(k)}
    except Exception:
        return set()          # unreadable: answer everything, exactly as before this build
    _CACHE[path] = (stamp, ids)
    return ids


def addressed_to_sibling(content, ids=None):
    """True when `content` is addressed TO Codex or Jem: an @mention of a sibling id, or a
    leading vocative use of the name. Says nothing about whether Baxter was addressed too-
    the caller tests that (and a message naming BOTH is Baxter's to answer)."""
    content = content or ""
    if not content.strip():
        return False
    if ids is None:
        ids = sibling_ids()
    if any((f"<@{s}>" in content or f"<@!{s}>" in content) for s in ids):
        return True
    return bool(VOCATIVE.match(content))


def message_for_sibling(m, bot_id, ids=None):
    """The fast lane's form of the same question, over a raw Discord message dict: True when
    this message belongs to Codex/Jem and NOT to Baxter.

    Three things decide it, in this order:
      - Baxter is mentioned too -> False. `@Baxter @Codex compare notes` is addressed to him,
        and a message he was named in is never one he stands back from.
      - a Discord REPLY to a sibling bot's own message -> True. It carries no `<@id>` text, so
        the content test alone would miss it (the listener resolves the same thing off the
        gateway; here it comes free in `referenced_message`).
      - otherwise, the content test: @mention or leading vocative.
    """
    content = m.get("content") or ""
    if bot_id and str(bot_id) in content:
        return False
    if ids is None:
        ids = sibling_ids()
    ref = m.get("referenced_message") or {}
    if str((ref.get("author") or {}).get("id")) in ids:
        return True
    return addressed_to_sibling(content, ids)
