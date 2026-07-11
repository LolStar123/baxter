"""Render the big-build queue/lane flow as a one-page landscape PDF for Atul.

The page is DERIVED, never transcribed: `LANE_COUNT`, the `PRIO_*` order, `WORKER_BUDGET`
and a real `HUB_FILES` entry are read out of `baxter_usage` at RENDER time, so the day the
lane count moves the diagram moves with it. Nothing here may hardcode a lane number.

This diagrams the BIG-BUILD queue (`baxter_usage.py` + the `baxter_triage.py` pump):
ask -> planner/PRD -> queue + touch-set -> gates -> clash-check -> lanes 1..N ->
verify gate -> landed, with the rejection paths drawn as first-class arrows rather
than footnotes. It is NOT `baxter_lanes.py`, which is the routine 3-lane reply governor.

Text is embedded as real TrueType glyphs (`pdf.fonttype = 42`), never stroked as vector
paths, so the sealed acceptance test can extract and assert on it.

    python baxter_flow_diagram.py --out 40-Drafts/build-queue-flow-diagram.pdf
    python baxter_flow_diagram.py --selftest
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")                       # no display, and no interactive backend to stall
matplotlib.rcParams["pdf.fonttype"] = 42    # TrueType: glyphs stay extractable text
matplotlib.rcParams["ps.fonttype"] = 42
matplotlib.rcParams["text.usetex"] = False  # usetex would outline every glyph into paths
matplotlib.rcParams["font.family"] = "DejaVu Sans"

import matplotlib.pyplot as plt                       # noqa: E402
from matplotlib.lines import Line2D                   # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import baxter_usage  # noqa: E402  (module object, so LANE_COUNT is read at call time)

# ---- palette: muted, deliberate. Never the matplotlib default rainbow. ----
INK = "#2b2f36"
PAPER = "#faf8f4"
FACE = "#f1eee8"
BLUE = "#5b6b7c"    # the happy path
AMBER = "#8a7346"   # gates
SLATE = "#6d6a78"   # clash / delegator
GREEN = "#5f7462"   # lanes
RUST = "#8c5a4a"    # rejections

PAGE_W, PAGE_H = 11.0, 8.5          # landscape letter, one page

# Variable column widths- the lane column needs only "Lane 10", the queue column needs prose.
# The wide gap before the lanes is where the delegator's arrows live; without it they tangle.
_MARGIN = 0.018
_COL_W = [0.090, 0.110, 0.145, 0.135, 0.150, 0.075, 0.130]
_GAPS = [0.016, 0.016, 0.016, 0.016, 0.045, 0.020]
_HEADERS = ["The ask", "Planner", "Build queue", "Gates", "Clash check", "Lanes", "Outcome"]

LINE_H = 0.030                      # one text line, in figure coords
BAND_TOP = 0.855
BAND_BOT = 0.200
CY = (BAND_TOP + BAND_BOT) / 2.0
REJ_RECT = (_MARGIN, 0.062, 1.0 - _MARGIN, 0.170)
YIELD_VIA = 0.181                   # the elbow lane for the .yield arrow, clear of the banner


def _col_x():
    """Left/right edge of each column, laid out cumulatively from the left margin."""
    xs, x = [], _MARGIN
    for i, w in enumerate(_COL_W):
        xs.append((x, x + w))
        x += w + (_GAPS[i] if i < len(_GAPS) else 0.0)
    return xs


def _box_h(lines):
    """A box tall enough for its title plus its lines, with even padding. No overflow."""
    return LINE_H * (len(lines) + 1) + 0.030


def _stack(x0, x1, heights, gap, centre=CY):
    """Boxes of the given heights, stacked top-down and vertically centred on `centre`."""
    total = sum(heights) + gap * (len(heights) - 1)
    top = centre + total / 2.0
    out = []
    for h in heights:
        out.append((x0, top - h, x1, top))
        top -= h + gap
    return out


def _lane_count():
    """Read LANE_COUNT from the live module every single time. Never cached, never a literal."""
    return max(1, int(getattr(baxter_usage, "LANE_COUNT", 1)))


def _lane_rects(x0, x1, n):
    """The lane column fills the band whatever N is, so the boxes never collide."""
    span = BAND_TOP - BAND_BOT
    gap = min(0.010, 0.30 * span / n)
    h = (span - gap * (n - 1)) / n
    return [(x0, BAND_TOP - i * (h + gap) - h, x1, BAND_TOP - i * (h + gap)) for i in range(n)]


def _layout():
    """Every node rect on the page, in figure coords, plus the text it carries.

    Returns a list of (rect, title, lines, colour). These rects are the single source of
    truth for both `render()` and `boxes()`, so a drawn box can never drift from its geometry.
    """
    g = baxter_usage
    cols = _col_x()
    n = _lane_count()
    hub = sorted(getattr(g, "HUB_FILES", ["utils/baxter_fast.py"]))[0]

    ask = ["Atul names a", "big task.", "It is never", "run ad-hoc."]
    plan = ["Writes the PRD", "and SEALS the", "acceptance test", "before any code", "is written."]
    queue = ["priority-ordered:",
             f"{getattr(g, 'PRIO_URGENT', 1)} urgent   {getattr(g, 'PRIO_RESUME', 2)} resume",
             f"{getattr(g, 'PRIO_DEFAULT', 5)} default",
             f"{getattr(g, 'PRIO_BACKGROUND', 8)} background",
             "",
             "--touch declares the", "touch-set, or --solo"]
    gates = [
        ("Usage gate", ["big work stops", "at 80%; vitals", "only at 90%"]),
        ("Human gate", ["gated_on: atul", "holds it until", "--ungate"]),
        ("Capacity", ["lane_capacity()", f"LANE_COUNT = {n}",
                      f"WORKER_BUDGET = {getattr(g, 'WORKER_BUDGET', 9)}"]),
    ]
    clash = [
        ("Clash check", ["a shared file, path", "containment, or a", "shared @tag"]),
        ("Undeclared", ["no touch-set means", "it clashes with all", "-> runs SOLO"]),
        ("Re-check", ["delegator_recheck()", "drops .yield on the", "loser; it halts"]),
    ]
    outcome = [
        ("Verify gate", ["the sealed exam", "runs after the", "worker exits"]),
        ("Landed", ["announced to Atul", "and the", "activity-log"]),
    ]

    nodes = []
    nodes.append((_stack(*cols[0], [_box_h(ask)], 0)[0], "The ask", ask, BLUE))
    nodes.append((_stack(*cols[1], [_box_h(plan)], 0)[0], "Planner", plan, BLUE))
    nodes.append((_stack(*cols[2], [_box_h(queue)], 0)[0], "Build queue", queue, BLUE))

    for rect, (t, ls) in zip(_stack(*cols[3], [_box_h(l) for _t, l in gates], 0.030), gates):
        nodes.append((rect, t, ls, AMBER))
    for rect, (t, ls) in zip(_stack(*cols[4], [_box_h(l) for _t, l in clash], 0.030), clash):
        nodes.append((rect, t, ls, SLATE))

    for i, rect in enumerate(_lane_rects(*cols[5], n), start=1):
        nodes.append((rect, f"Lane {i}", [], GREEN))

    for rect, (t, ls) in zip(_stack(*cols[6], [_box_h(l) for _t, l in outcome], 0.050), outcome):
        nodes.append((rect, t, ls, BLUE if t == "Verify gate" else GREEN))

    nodes.append((REJ_RECT, "Rejections", [
        "clash skips and human-gate holds both append to .baxter_rejects.jsonl "
        "- read them back with   --rejects 20",
        f"an undeclared touch-set runs SOLO; a bare hub path is refused at --queue "
        f"- declare a region, e.g.  {hub}/<function>",
    ], RUST))
    return nodes


def boxes():
    """The node rects, as (x0, y0, x1, y1) in figure coords. Pairwise disjoint, in-bounds."""
    return [rect for rect, _t, _l, _c in _layout()]


def _draw_node(ax, rect, title, lines, colour):
    x0, y0, x1, y1 = rect
    ax.add_patch(FancyBboxPatch(
        (x0, y0), x1 - x0, y1 - y0,
        boxstyle="round,pad=0,rounding_size=0.006",
        linewidth=1.1, edgecolor=colour, facecolor=FACE, mutation_aspect=PAGE_W / PAGE_H))
    cx = (x0 + x1) / 2.0
    if not lines:                                    # a lane pill: one centred label
        ax.text(cx, (y0 + y1) / 2.0, title, ha="center", va="center",
                fontsize=9.5, color=colour, fontweight="bold")
        return
    wide = (x1 - x0) > 0.5                           # the rejection banner spans the page
    tx, ha = (x0 + 0.012, "left") if wide else (cx, "center")
    step = 0.028 if wide else LINE_H
    ty = y1 - step
    ax.text(tx, ty, title, ha=ha, va="center", fontsize=10, color=colour, fontweight="bold")
    for ln in lines:
        ty -= step
        if ln:
            ax.text(tx, ty, ln, ha=ha, va="center", fontsize=9, color=INK)


def _arrow(ax, a, b, colour, rad=0.0, lw=1.2, ls="-"):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", color=colour, linewidth=lw,
                                 linestyle=ls, mutation_scale=11, shrinkA=1, shrinkB=1,
                                 connectionstyle=f"arc3,rad={rad}"))


def render(out_path):
    """Draw the page and write it to `out_path` as a single landscape PDF."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    nodes = _layout()
    cols = _col_x()
    n = _lane_count()
    lanes = _lane_rects(*cols[5], n)

    fig = plt.figure(figsize=(PAGE_W, PAGE_H))
    fig.patch.set_facecolor(PAPER)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    fig.text(0.5, 0.955, "How a big build reaches a lane", ha="center", va="center",
             fontsize=17, color=INK, fontweight="bold")
    fig.text(0.5, 0.917, "the build queue, its gates, the clash delegator and the "
                         "rejection paths - drawn live from baxter_usage",
             ha="center", va="center", fontsize=9.5, color=SLATE)
    for (x0, x1), head in zip(cols, _HEADERS):
        fig.text((x0 + x1) / 2.0, 0.876, head.upper(), ha="center", va="center",
                 fontsize=9, color=SLATE, fontweight="bold")

    for rect, title, lines, colour in nodes:
        _draw_node(ax, rect, title, lines, colour)

    # The spine, left to right. The last hop feeds a bus rather than one lane, so that
    # every lane is fed by its own short arrow instead of N curves crossing each other.
    for i in range(4):
        _arrow(ax, (cols[i][1], CY), (cols[i + 1][0], CY), BLUE)
    bus_x = cols[5][0] - 0.008
    _arrow(ax, (cols[4][1], CY), (bus_x, CY), GREEN)
    ax.add_line(Line2D([bus_x, bus_x],
                       [(lanes[-1][1] + lanes[-1][3]) / 2.0, (lanes[0][1] + lanes[0][3]) / 2.0],
                       color=GREEN, linewidth=1.0, zorder=1))
    for rect in lanes:
        cy = (rect[1] + rect[3]) / 2.0
        _arrow(ax, (bus_x, cy), (cols[5][0], cy), GREEN, lw=0.9)

    # A lane exits into the VERIFY GATE, and only a pass becomes a landing. Aiming the
    # lane arrow at the column's midline would point it into the gap between the two boxes.
    by_title = {t: r for r, t, _l, _c in nodes}
    verify, landed = by_title["Verify gate"], by_title["Landed"]
    vcx = (verify[0] + verify[2]) / 2.0
    _arrow(ax, (cols[5][1], CY), (cols[6][0], (verify[1] + verify[3]) / 2.0), GREEN, rad=0.07)
    _arrow(ax, (vcx, verify[1]), (vcx, landed[3]), GREEN)

    # Rejection paths, first-class: a human-gate hold and a clash skip both sink to the log.
    gates_cx = sum(cols[3]) / 2.0
    clash_cx = sum(cols[4]) / 2.0
    gates_bot = min(r[1] for r, _t, _l, c in nodes if c is AMBER)
    clash_bot = min(r[1] for r, _t, _l, c in nodes if c is SLATE)
    for cx, bot, label in ((gates_cx, gates_bot, "held"), (clash_cx - 0.035, clash_bot, "skipped")):
        _arrow(ax, (cx, bot - 0.008), (cx, REJ_RECT[3] + 0.004), RUST)
        ax.text(cx + 0.008, (bot + REJ_RECT[3]) / 2.0, label, ha="left", va="center",
                fontsize=9, color=RUST, style="italic")

    # The .yield marker lands on a LIVE lane. Routed as an elbow under the page rather than
    # a curve across the bus- a crossing line here reads as a mistake, not a path.
    yx = clash_cx + 0.045
    lx = sum(cols[5]) / 2.0
    ax.add_line(Line2D([yx, yx], [clash_bot - 0.008, YIELD_VIA], color=SLATE,
                       linewidth=1.0, linestyle="--"))
    ax.add_line(Line2D([yx, lx], [YIELD_VIA, YIELD_VIA], color=SLATE,
                       linewidth=1.0, linestyle="--"))
    _arrow(ax, (lx, YIELD_VIA), (lx, lanes[-1][1]), SLATE, lw=1.0, ls="--")
    ax.text(yx + 0.008, (clash_bot + YIELD_VIA) / 2.0, ".yield", ha="left", va="center",
            fontsize=9, color=SLATE, style="italic")

    fig.text(0.5, 0.038, "Lanes are numbered the way Atul reads them, counting from one: "
                         "lane_label() adds one to the internal index.",
             ha="center", va="center", fontsize=9, color=SLATE)

    fig.savefig(str(out), format="pdf", facecolor=fig.get_facecolor())
    plt.close(fig)
    return str(out)


def _text_of(path):
    import fitz
    with fitz.open(str(path)) as doc:
        return "\n".join(pg.get_text() for pg in doc).lower()


def selftest():
    """Prove the page is real: extractable text, disjoint in-bounds boxes, and a lane
    column that genuinely DERIVES from LANE_COUNT rather than hardcoding today's value."""
    live = _lane_count()
    tmp = Path(tempfile.gettempdir())

    p = tmp / "bx_flow_selftest.pdf"
    render(p)
    assert p.stat().st_size > 5000, "pdf suspiciously small"

    t = _text_of(p)
    assert t.strip(), "no extractable text- glyphs were stroked as paths"
    assert "lane 0" not in t, "a lane 0 reached the page"
    for k in ("queue", "clash", "touch", "solo", "gate", "reject", "priorit", "yield"):
        assert k in t, f"missing flow label: {k}"
    assert sum(f"lane {i}" in t for i in range(1, live + 1)) == live, "lane labels != LANE_COUNT"
    assert f"lane {live + 1}" not in t, "drew more lanes than LANE_COUNT"

    b = boxes()
    assert len(b) >= 8, f"only {len(b)} nodes- that is not the real flow"
    for r in b:
        assert 0.0 <= r[0] < r[2] <= 1.0 and 0.0 <= r[1] < r[3] <= 1.0, f"box off the page: {r}"
    for i, x in enumerate(b):
        for y in b[i + 1:]:
            assert not (x[0] < y[2] and y[0] < x[2] and x[1] < y[3] and y[1] < x[3]), \
                f"boxes overlap: {x} vs {y}"

    # DERIVATION. Patch the live constant and re-render: the page must follow it.
    saved = baxter_usage.LANE_COUNT
    try:
        baxter_usage.LANE_COUNT = 3
        q = tmp / "bx_flow_selftest3.pdf"
        render(q)
        t3 = _text_of(q)
        assert "lane 3" in t3 and "lane 4" not in t3, "LANE_COUNT is hardcoded, not read at render time"
        assert len(boxes()) == len(b) - live + 3, "boxes() does not track LANE_COUNT"
    finally:
        baxter_usage.LANE_COUNT = saved

    print(f"SELFTEST OK ({len(b)} nodes, {live} lanes, text extractable)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the one-page landscape PDF here")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the render, exit non-zero on any failure")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.out:
        ap.error("give --out <path> or --selftest")
    print(render(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
