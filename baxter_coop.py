r"""baxter_coop - Baxter's coop-v2 fan-out (the MD gathering the analysts' work).

Baxter (MD) hands a SELF-CONTAINED brief (+ optional context) to BOTH analysts- Codex (Engineer)
and Jem (Analyst)- as BACKENDS, runs them in parallel, and prints their raw responses clearly
labelled. Baxter then REVIEWS them, bounces any sub-standard part back (re-run with a sharper
brief), and CURATES the finished answer to present to the owner. The bots never post to Discord;
Baxter owns the final output. See memory: coop-v2-baxter-orchestrated.

  python baxter_coop.py --brief "10 ideas to ..." [--context path\to\prd.md]
  python baxter_coop.py --brief "..." --json          # machine-readable {codex, jem}

This tool only GATHERS. The review + curation is Baxter's judgement, done in the live session.

TIMEOUTS (9th July). Jem was capped at min(--timeout, 120)s while codex got the full 900s, a
leftover from the raw-API days. He is CLI-backed now: measured cold start of one
`baxter_ask_jem.py` round trip is 12.5-15.7s over three trivial runs, and a quota refusal
costs a SECOND full-length attempt against FALLBACK_MODEL- observed firing on every call on
9th July, with the daily flash quota already exhausted. So jem now takes coop's full --timeout
like codex, and the parent's subprocess wrapper budgets `timeout * attempts + 60` rather than
`timeout + 60`, or the parent reaps the child mid-fallback and a real brief comes back as a
failure. The two analysts run in PARALLEL, so the wider jem cap adds no wall-clock while codex
remains the long pole.
"""
import argparse, concurrent.futures as cf, json, os, subprocess, sys
from pathlib import Path

UTILS = Path(r"C:\Users\you\Documents\Python Scripts\utils")
PY = sys.executable


def _call(script, brief, context, persona, timeout, attempts=1):
    """Run one analyst backend. `timeout` is the cap the CHILD is given per attempt.

    `attempts` is how many full-length attempts that child may make internally, and it is
    the parent's wrapper budget: reaping the child before it has finished its own retries
    turns a slow-but-successful answer into a fabricated failure. baxter_ask_jem.run_gemini
    retries a quota refusal with a SECOND full-timeout _run_once against FALLBACK_MODEL, so
    jem needs attempts=2; codex has no internal retry and stays at 1.
    """
    cmd = [PY, str(UTILS / script), "--brief", brief, "--persona", persona, "--timeout", str(timeout)]
    if context:
        cmd += ["--context", context]
    try:
        # utf-8 on BOTH ends: PYTHONIOENCODING forces the child's stdout to utf-8 (Windows
        # otherwise picks cp1252 and an em-dash/emoji answer dies mid-print as UnicodeEncodeError,
        # arriving here as empty stdout- 'no output'); encoding+errors decode it back on the parent.
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                           timeout=timeout * attempts + 60)
    except Exception as e:
        return _fail(script, e)
    out = (r.stdout or "").strip()
    if not out:
        # Both backends print the answer to stdout and the cause to stderr; an empty stdout IS
        # the failure. Returning that stderr text bare made a reaped or quota-refused analyst
        # read as CONTENT to the MD (and to --json consumers).
        return _fail(script, (r.stderr or "").strip() or "no output")
    return out


def _fail(script, why):
    """An error envelope, distinguishable from an answer, and loud on stderr."""
    msg = f"({script} FAILED: {why})"
    print(msg, file=sys.stderr, flush=True)
    return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", required=True)
    ap.add_argument("--context", help="path to a context file (PRD/notes) both analysts get")
    ap.add_argument("--codex-persona", default="engineer")
    ap.add_argument("--jem-persona", default="analyst")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", choices=["codex", "jem"], help="fan to just one analyst")
    a = ap.parse_args()

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        fc = ex.submit(_call, "baxter_ask_codex.py", a.brief, a.context, a.codex_persona,
                       a.timeout, 1) if a.only != "jem" else None
        # Jem gets the SAME cap as codex (see module docstring), and attempts=2 for his fallback.
        fj = ex.submit(_call, "baxter_ask_jem.py", a.brief, a.context, a.jem_persona,
                       a.timeout, 2) if a.only != "codex" else None
        codex = fc.result() if fc else ""
        jem = fj.result() if fj else ""

    if a.json:
        print(json.dumps({"codex": codex, "jem": jem}, ensure_ascii=False, indent=1))
    else:
        parts = []
        if codex:
            parts.append("=== CODEX (Engineer) ===\n" + codex)
        if jem:
            parts.append("=== JEM (Analyst) ===\n" + jem)
        print("\n\n".join(parts))


if __name__ == "__main__":
    main()
