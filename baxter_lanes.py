"""baxter_lanes - shared 3-lane concurrency governor + Claude-session ledger for
reply/channel continuation (the owner, 8th July).

TWO jobs, one small module both the real-time listener (baxter_slash) and the
#general fast-lane (baxter_fast) import so there is ONE source of truth:

  1. LANES - cap concurrent ROUTINE claude workers at 3 (the re-upped 3-lane model).
     Big/project builds keep their OWN single-slot governor (.baxter_resume, unchanged);
     this ONLY bounds quick/routine replies so many normal tasks fan out to 3 parallel
     lanes instead of serialising behind one. Below the cap a worker starts instantly;
     at the cap it waits briefly, then proceeds anyway (never DROP one of the owner's messages-
     a rare 4th lane beats silence). Lane files are stale-pruned so a crashed worker can
     never wedge a lane shut.

  2. SESSION LEDGER - so a channel / reply CONTINUES the same Claude conversation rather
     than cold-starting (his 1-lane complaint was really "fresh session loses context"):
       - non-#general channels -> ONE persistent MASTER session per channel; a new message
         resumes the channel's whole context (the owner, 8th July 11:58 "one master convo per
         channel, not new workers").
       - #general -> per-task (random one-offs), BUT a Discord REPLY to a Baxter answer
         resumes THAT answer's session (the owner, 8th July 12:39 "reply continues the thread").
     Backed by the CLI's --session-id / --resume (proven headless: create a session with a
     known uuid, resume it later by the same uuid with full context intact).

Files (all under the vault, git-ignored operational state):
  .baxter_lanes/               - lane lock dir (one lane-*.lock file per active worker)
  .baxter_reply_threads.json   - {"channels": {cid: sid}, "messages": {owner_mid: sid},
                                  "order": [owner_mid, ...]}  (messages LRU-capped)
"""
import json
import os
import time
import uuid
from datetime import datetime

VAULT = r"C:\Users\you\Documents\Baxter"
LANES_DIR = os.path.join(VAULT, ".baxter_lanes")
LEDGER = os.path.join(VAULT, ".baxter_reply_threads.json")
LEDGER_LOCK = os.path.join(VAULT, ".baxter_reply_threads.lock")
LOG = os.path.join(VAULT, ".baxter_lanes.log")

MAX_LANES = 3          # the re-upped 3-lane concurrency for routine work
LANE_TTL = 600         # seconds- a lane file older than this is a dead worker, prune it
MSG_CAP = 300          # keep the last N message->session mappings (#general reply chains)

GENERAL_ID = "222222222222222202"   # the ONE per-task/random channel (no master thread)


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


# ---- LANES ------------------------------------------------------------------------
def _prune_lanes():
    """Drop lane files whose worker died (mtime older than LANE_TTL)- self-healing so a
    crashed worker never wedges a lane permanently."""
    now = time.time()
    try:
        for n in os.listdir(LANES_DIR):
            if not n.startswith("lane-"):
                continue
            p = os.path.join(LANES_DIR, n)
            try:
                if now - os.path.getmtime(p) > LANE_TTL:
                    os.remove(p)
            except Exception:
                pass
    except FileNotFoundError:
        pass


def acquire_lane(wait_s=90):
    """Claim one of the 3 routine lanes. Returns a lane-file path (release it when done).
    Waits up to wait_s for a free lane; if still full, returns the path anyway (an
    over-cap lane, logged)- the owner's message is never dropped for want of a slot. The claim
    is best-effort atomic via O_CREAT|O_EXCL on a uuid-named file, so two workers can't
    grab the same slot."""
    os.makedirs(LANES_DIR, exist_ok=True)
    deadline = time.time() + max(0, wait_s)
    while True:
        _prune_lanes()
        try:
            active = [n for n in os.listdir(LANES_DIR) if n.startswith("lane-")]
        except Exception:
            active = []
        if len(active) < MAX_LANES or time.time() >= deadline:
            over = len(active) >= MAX_LANES
            path = os.path.join(LANES_DIR, f"lane-{uuid.uuid4().hex}.lock")
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                if over:
                    _log(f"over-cap lane granted (>{MAX_LANES} busy)- not dropping the owner's msg")
                return path
            except FileExistsError:
                continue   # uuid clash (astronomically rare)- retry
        time.sleep(2)


def touch_lane(path):
    """Keep a long-running worker's lane fresh so the pruner doesn't reclaim it mid-run."""
    try:
        os.utime(path, None)
    except Exception:
        pass


def release_lane(path):
    try:
        os.remove(path)
    except Exception:
        pass


# ---- SESSION LEDGER ---------------------------------------------------------------
class _Lock:
    """Tiny cross-process spinlock via atomic mkdir- guards the ledger read-modify-write
    so two workers writing at once can't clobber each other. Self-heals a stale lock."""
    def __enter__(self):
        for _ in range(50):
            try:
                os.mkdir(LEDGER_LOCK)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(LEDGER_LOCK) > 30:
                        os.rmdir(LEDGER_LOCK)   # stale- reclaim
                        continue
                except Exception:
                    pass
                time.sleep(0.1)
        return self   # gave up waiting- proceed unlocked rather than block forever

    def __exit__(self, *a):
        try:
            os.rmdir(LEDGER_LOCK)
        except Exception:
            pass


def _read():
    try:
        d = json.loads(open(LEDGER, encoding="utf-8-sig").read())
        d.setdefault("channels", {})
        d.setdefault("messages", {})
        d.setdefault("order", [])
        return d
    except Exception:
        return {"channels": {}, "messages": {}, "order": []}


def _write(d):
    tmp = LEDGER + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, LEDGER)   # atomic on Windows
    except Exception as e:
        _log(f"ledger write failed: {e}")


def session_for_channel(cid):
    return _read()["channels"].get(str(cid))


def set_channel_session(cid, sid):
    with _Lock():
        d = _read()
        d["channels"][str(cid)] = sid
        _write(d)


def session_for_message(mid):
    return _read()["messages"].get(str(mid))


def set_message_session(mid, sid):
    with _Lock():
        d = _read()
        mid = str(mid)
        if mid not in d["messages"]:
            d["order"].append(mid)
        d["messages"][mid] = sid
        # LRU-cap the #general reply chains so the ledger never grows unbounded
        while len(d["order"]) > MSG_CAP:
            old = d["order"].pop(0)
            d["messages"].pop(old, None)
        _write(d)


def plan_session(cid, is_reply=False, referenced_owner_mid=None):
    """Decide which Claude session a message should run in, and record the mapping.
    Returns (session_id, mode) where mode is 'create' (new --session-id) or 'resume'
    (--resume an existing one).

      non-#general channel -> the channel's persistent MASTER session (create once,
                              resume forever) - one master convo per channel.
      #general             -> per-task; a REPLY to a Baxter answer resumes that answer's
                              session (looked up by the ORIGINAL the owner message it replied
                              to), otherwise a fresh per-task session.
    """
    cid = str(cid)
    if cid != GENERAL_ID:
        sid = session_for_channel(cid)
        if sid:
            return sid, "resume"
        sid = str(uuid.uuid4())
        set_channel_session(cid, sid)
        return sid, "create"
    # #general
    if is_reply and referenced_owner_mid:
        sid = session_for_message(referenced_owner_mid)
        if sid:
            return sid, "resume"
    return str(uuid.uuid4()), "create"


def rebind_channel(cid, sid):
    """A resume failed (session file gone); the worker minted a fresh one- repoint the
    channel master at it so the next message resumes the NEW session, not the dead id."""
    set_channel_session(cid, sid)
