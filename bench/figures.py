"""Figures for a Ledgerline run, drawn from the run's own artifacts.

    make figures RUN=<run_id>
    # or: cd bench && uv run --with matplotlib python figures.py <run_id>

Writes docs/figures/*.png. matplotlib is deliberately not a repo dependency
(nothing in the measured path draws), so it is pulled in ad hoc with --with.

**No measured number on a chart is typed by hand.** Every series is recomputed from
`results/<run_id>/cells/**` under the exclusion rule `analyze` applies (a window
the runner recorded as under-warmed is dropped), and every figure quoted in a
title, a label or a footer is either computed here or read from
`report/fits.json`. `Run.__init__` cross-checks the two: if this file's
per-request CPU at the operating rate disagrees with the report's, it refuses to
draw rather than publish a chart that contradicts the table beside it.

Titles that name the larger stack or a ratio are computed too. Which stack is the
larger one, and by how much, is read off the data, so a re-run that moved the
result would redraw the sentence and not only the line. The titles of the
database and cost-per-hour figures are typed sentences.

Two inputs are constants, and say so where they are used:

  - the cloud list prices behind the two cost figures (`VCPU_HOUR_USD`,
    `GB_HOUR_USD`), which are a dated vendor quote and not a measurement;
  - the round-trip arithmetic in the database figure's footer, which is
    `spec/db-access.md`'s and holds for any run under this spec.

The rig diagram (`fig_rig`) draws no measurement: its cores and memory are typed
constants that mirror `infra/compose.bench.yaml`'s defaults, and its hardware and
version labels are typed too.
"""

from __future__ import annotations

import json
import random
import statistics as st
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import yaml  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledgerbench import registry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "figures"

# Per-slot series colour, keyed by SLOT and not by rank: the colour follows the
# stack whichever one comes out larger. The pair passes a colour-vision-deficiency
# separation check against SURFACE.
SLOT_COLOR: dict[str, str] = {"java": "#eb6834", "dotnet": "#4a3aa7"}
SLOT_LONG: dict[str, str] = {
    "java": "Java 25 / Spring Boot 4",
    "dotnet": ".NET 10 / Minimal API",
}
POSTGRES = "#2a78d6"
RAM = "#1baf7a"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

# AWS Fargate list price, us-east-1, Linux/x86 (aws.amazon.com/fargate/pricing:
# $0.000011244 per vCPU-second, $0.000001235 per GB-second), read 2026-10-12.
# A vendor quote, not a measurement: the cost figures are linear in both.
VCPU_HOUR_USD = 0.000011244 * 3600
GB_HOUR_USD = 0.000001235 * 3600
PRICE_AS_OF = "Oct 2026"

# DejaVu Sans ships inside matplotlib, so the layout below is the same on every OS.
plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK2,
    "xtick.color": INK2,
    "ytick.color": INK2,
    "axes.facecolor": SURFACE,
    "figure.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 1,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.titlelocation": "left",
    "axes.titleweight": "bold",
    "axes.titlesize": 12.5,
    "axes.titlecolor": INK,
    "legend.frameon": False,
    "text.parse_math": False,
})


class FiguresDisagreeWithReport(RuntimeError):
    """This file recomputed a headline number and got something else."""


class Run:
    """One run's `fits.json` plus every measured window of its headline cell.

    Constructed from a run id passed in, never from a module-level constant, so
    `import figures` works on a tree with no run on disk.
    """

    def __init__(self, run_id: str, root: Path = ROOT) -> None:
        self.run_id = run_id
        self.dir = root / "results" / run_id
        self.fits = json.loads((self.dir / "report" / "fits.json").read_text())
        self.cell = self.fits["cells"][0]
        self.slo_ms = float(self.fits["slo_ms"])
        self.subject = self.fits["ratio_direction"]["subject"]
        self.reference = self.fits["ratio_direction"]["reference"]
        self.slots = [self.subject, self.reference]
        self.limit = {s: int(r) for s, r in
                      self.cell["resource"]["operating_rate_by_slot"].items()}
        slo = yaml.safe_load((root / "spec" / "slo.yaml").read_text(encoding="utf-8"))
        self.warmup_cap_s = float(slo["warmup"]["hard_cap_seconds"])
        self.windows = self._load_windows()
        self._cross_check()

    def _load_windows(self) -> list[dict]:
        cell_dir = self.dir / "cells" / self.cell["cell_id"]
        rows = []
        for wf in sorted(cell_dir.rglob("*.warmup.json")):
            slot = wf.name.split(".")[0]
            if slot not in self.slots:
                continue
            d = wf.parent
            warm = json.loads(wf.read_text())
            cg = json.loads((d / f"cpu_mem_{slot}.summary.json").read_text())
            pg = json.loads((d / f"cpu_mem_{slot}.postgres.summary.json").read_text())
            sm = json.loads((d / f"{slot}.summary.json").read_text())
            rs = json.loads((d / f"{slot}.runtime_stats.summary.json").read_text())
            rows.append({
                "slot": slot,
                "stage": warm["stage"],
                "rate": int(d.parent.name.removeprefix("rate")),
                "under_warmed": bool(warm["under_warmed"]),
                "warm_s": warm["elapsed_seconds"],
                "cpu_s": cg["cpu_usage_delta_usec"] / 1e6,
                "db_cpu_s": pg["cpu_usage_delta_usec"] / 1e6,
                "anon_mb": cg["mem_anon_p99"] / 1e6,
                "requests": sm["requests"],
                "p99_ms": sm["latencies"]["99th"] / 1e6,
                "heap_max_mib": (rs["levels"]["gc_heap_committed_bytes"]["max"] or 0) / 2**20,
            })
        return rows

    def _cross_check(self) -> None:
        for slot in self.slots:
            here = self.at_limit(slot)["cpu_ms"]
            report = self.cell["resource"]["by_slot"][slot]["cpu_ms_per_req"]
            if abs(here - report) > 0.005 * report:
                raise FiguresDisagreeWithReport(
                    f"{slot}: figures.py computes {here:.4f} CPU-ms/req at "
                    f"{self.limit[slot]} rps, report/fits.json says {report:.4f}")

    def short(self, slot: str) -> str:
        return registry.get_slot(slot).label

    def long(self, slot: str) -> str:
        return SLOT_LONG.get(slot, self.short(slot))

    def color(self, slot: str) -> str:
        return SLOT_COLOR.get(slot, MUTED)

    def rungs(self, slot: str, stages: set[str]) -> list[dict]:
        """Per offered rate: request-weighted CPU, plain means of the levels."""
        acc: dict[int, dict] = defaultdict(
            lambda: {"cpu": 0.0, "db": 0.0, "req": 0, "anon": [], "p99": []})
        for w in self.windows:
            if w["slot"] != slot or w["under_warmed"] or w["stage"] not in stages:
                continue
            a = acc[w["rate"]]
            a["cpu"] += w["cpu_s"]
            a["db"] += w["db_cpu_s"]
            a["req"] += w["requests"]
            a["anon"].append(w["anon_mb"])
            a["p99"].append(w["p99_ms"])
        return [{
            "rate": rate,
            "cpu_ms": 1000 * a["cpu"] / a["req"],
            "db_ms": 1000 * a["db"] / a["req"],
            "anon_mb": st.mean(a["anon"]),
            "p99_ms": st.mean(a["p99"]),
        } for rate, a in sorted(acc.items())]

    def curve(self, slot: str) -> list[dict]:
        return self.rungs(slot, {"ladder", "confirm", "soak"})

    def at_limit(self, slot: str) -> dict:
        """The roll-up's operating point: confirm + soak windows at the slot's own rate."""
        return next(r for r in self.rungs(slot, {"confirm", "soak"})
                    if r["rate"] == self.limit[slot])

    def confirm_p99(self, slot: str) -> float:
        return next(r["mean_p99_ms"] for r in self.cell["slo_at_rates"]
                    if r["stage"] == "confirm" and r["slot"] == slot
                    and r["rate"] == self.limit[slot])

    def ranked(self, key: str) -> tuple[str, str, float]:
        """(larger slot, smaller slot, larger / smaller) on `key` at the limit."""
        a, b = self.slots
        va, vb = self.at_limit(a)[key], self.at_limit(b)[key]
        return (a, b, va / vb) if va >= vb else (b, a, vb / va)

    def bill(self, slot: str, point: dict, rate: int, ram_mult: float = 1.0) -> tuple[float, float]:
        """($/h for the CPU actually busy, $/h for the memory actually held)."""
        cores = point["cpu_ms"] * rate / 1000
        return cores * VCPU_HOUR_USD, point["anon_mb"] / 1000 * GB_HOUR_USD * ram_mult


def isotonic(ys: list[float]) -> list[float]:
    """Pool-adjacent-violators, the same monotone fit the knee crossing uses."""
    blocks = [[y, 1] for y in ys]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0]:
            a, b = blocks[i], blocks[i + 1]
            blocks[i:i + 2] = [[(a[0] * a[1] + b[0] * b[1]) / (a[1] + b[1]), a[1] + b[1]]]
            i = max(i - 1, 0)
        else:
            i += 1
    return [v for v, n in blocks for _ in range(n)]


# --------------------------------------------------------------------------- #
# drawing helpers
# --------------------------------------------------------------------------- #
def header(ax, title: str, subtitle: str) -> None:
    ax.set_title(title, pad=24)
    ax.text(0, 1.03, subtitle, transform=ax.transAxes, color=INK2, fontsize=9, va="bottom")


def footer(ax, text: str, width: int = 118) -> None:
    """Source note under the x-axis label, wrapped so it never widens the canvas."""
    ax.annotate(textwrap.fill(text, width), xy=(0, 0),
                xycoords=("axes fraction", ax.xaxis.label), xytext=(0, -8),
                textcoords="offset points", color=MUTED, fontsize=7.5, ha="left",
                va="top", linespacing=1.5, annotation_clip=False)


def line(ax, run: Run, slot: str, xs, ys) -> None:
    ax.plot(xs, ys, color=run.color(slot), lw=2, marker="o", ms=7, mec=SURFACE, mew=2,
            solid_joinstyle="round", solid_capstyle="round", label=run.long(slot))


def big_dot(ax, run: Run, slot: str, x, y) -> None:
    ax.plot([x], [y], marker="o", ms=12, color=run.color(slot), mec=SURFACE, mew=2, zorder=5)


def note(ax, x, y, text: str, dx: int = 8, dy: int = 0, ha: str = "left", bold: bool = True,
         color: str = INK) -> None:
    ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points", color=color,
                fontsize=9, va="center", ha=ha, fontweight="bold" if bold else "normal")


def rate_axis(ax, run: Run, step: int = 1000) -> None:
    rates = [r["rate"] for s in run.slots for r in run.curve(s)]
    lo, hi = min(rates), max(rates)
    ticks = [lo] + list(range(step, int(hi) + 1, step))
    ax.set_xlabel("Traffic (requests per second)")
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{t:,}" for t in ticks])
    ax.set_xlim(lo * 0.6, hi * 1.3)


def stacked_h(ax, cats, segs, fmt, unit: str) -> None:
    """Thin horizontal stacked bars with a surface-coloured gap between segments."""
    y = list(range(len(cats)))[::-1]
    widest = max(sum(s) for s in segs)
    left = [0.0] * len(cats)
    for j in range(len(segs[0])):
        vals = [s[j] for s in segs]
        ax.barh(y, vals, left=left, height=0.42, edgecolor=SURFACE, linewidth=2)
        for yi, x0, v in zip(y, left, vals):
            if v > 0.14 * widest:
                ax.text(x0 + v / 2, yi, fmt(v), ha="center", va="center", color="white",
                        fontsize=9, fontweight="bold")
        left = [x0 + v for x0, v in zip(left, vals)]
    for yi, total in zip(y, [sum(s) for s in segs]):
        ax.text(total, yi, "  " + fmt(total) + unit, va="center", ha="left", color=INK,
                fontsize=10, fontweight="bold")
    ax.set_yticks(y)
    ax.set_yticklabels(cats, color=INK)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, widest * 1.45)


def paint(ax, run: Run, second: str, second_label: str, first: str = "app") -> None:
    """First segment wears its stack's colour; the second is shared, so it gets one."""
    n = len(run.slots)
    for patch, slot in zip(ax.patches[:n], run.slots):
        patch.set_facecolor(run.color(slot))
    for patch in ax.patches[n:]:
        patch.set_facecolor(second)
    handles = [plt.Rectangle((0, 0), 1, 1, color=run.color(s)) for s in run.slots]
    handles.append(plt.Rectangle((0, 0), 1, 1, color=second))
    ax.legend(handles, [f"{run.short(s)} {first}" for s in run.slots] + [second_label],
              loc="upper right", fontsize=8.5)


def save(fig, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name, dpi=200, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    print(f"wrote docs/figures/{name}")


# --------------------------------------------------------------------------- #
# the figures
# --------------------------------------------------------------------------- #
def fig_rig(run: Run) -> None:
    def box(x, y, w, h, title, sub, col):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.25",
                                    fc=SURFACE, ec=col, lw=2))
        ax.text(x + w / 2, y + h * 0.66, title, ha="center", va="center", color=INK,
                fontsize=10, fontweight="bold")
        ax.text(x + w / 2, y + h * 0.30, sub, ha="center", va="center", color=INK2, fontsize=8.2)

    def arrow(x0, y0, x1, y1):
        ax.annotate("", (x1, y1), (x0, y0),
                    arrowprops={"arrowstyle": "-|>", "color": MUTED, "lw": 1.6,
                                "shrinkA": 2, "shrinkB": 2})

    fig, ax = plt.subplots(figsize=(9, 4.0))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6)
    ax.axis("off")
    ax.set_title("One machine, pinned cores, one service under test at a time", pad=10)
    box(0.3, 2.2, 2.6, 1.6, "vegeta", "load generator\n3 cores, 4 GiB", MUTED)
    box(4.1, 3.7, 3.6, 1.7, "Java 25 · Spring Boot 4", "Hibernate 7, HikariCP, Tomcat\n6 cores, 12 GiB",
        run.color("java"))
    box(4.1, 0.7, 3.6, 1.7, ".NET 10 · Minimal API", "EF Core 10, Npgsql, Kestrel\n6 cores, 12 GiB",
        run.color("dotnet"))
    box(8.9, 3.5, 2.9, 1.5, "PostgreSQL 18.4", "3 cores, 8 GiB\nits CPU counted separately", POSTGRES)
    box(8.9, 1.1, 2.9, 1.5, "Redis 8", "1 core, 2 GiB\ncache, entries live 5 min", RAM)
    arrow(2.9, 3.3, 4.1, 4.4)
    arrow(2.9, 2.7, 4.1, 1.6)
    arrow(7.7, 4.6, 8.9, 4.3)
    arrow(7.7, 4.4, 8.9, 2.0)
    arrow(7.7, 1.5, 8.9, 4.1)
    arrow(7.7, 1.3, 8.9, 1.8)
    ax.text(5.9, 3.05, "only one of the two runs during a window", ha="center", color=INK2,
            fontsize=8.5, style="italic")
    ax.text(6, 0.1, "Ryzen 9 7950X, Fedora 43. CPU and memory are read from the Linux kernel's "
            "per-container counters, not from what the runtimes report.", ha="center",
            color=MUTED, fontsize=7.5)
    save(fig, "fig0-rig.png")


def fig_latency(run: Run) -> None:
    a, b = run.slots
    apart = 100 * abs(run.limit[a] - run.limit[b]) / min(run.limit.values())
    fig, ax = plt.subplots(figsize=(8, 4.6))
    header(ax, f"Both stacks reach the {run.slo_ms:g} ms line within {apart:.1f}% of each other",
           "How slow the slowest 1% of requests were (p99) as traffic rose; the limit is where "
           f"the fitted line crosses {run.slo_ms:g} ms")
    ax.axhline(run.slo_ms, color=MUTED, lw=1.2)
    top = run.slo_ms * 1.5
    for slot in run.slots:
        pr = run.rungs(slot, {"ladder"})
        xs, ys = [r["rate"] for r in pr], [r["p99_ms"] for r in pr]
        top = max(top, max(ys) * 1.12)
        ax.plot(xs, isotonic(ys), color=run.color(slot), lw=2, alpha=0.9, label=run.long(slot))
        ax.plot(xs, ys, ls="none", marker="o", ms=7, mec=SURFACE, mew=2, color=run.color(slot))
        # The two limits can sit a few rps apart, so the second one is a smaller
        # diamond drawn underneath rather than a second circle hiding the first.
        first = slot == run.subject
        ax.plot([run.limit[slot]], [run.slo_ms], marker="o" if first else "D",
                ms=12 if first else 10, color=run.color(slot), mec=SURFACE, mew=2,
                zorder=6 if first else 5)
    note(ax, run.limit[a], run.slo_ms, f"{run.short(a)} limit: {run.limit[a]:,} rps", dx=-14,
         dy=16, ha="right")
    note(ax, run.limit[b], run.slo_ms, f"{run.short(b)} limit: {run.limit[b]:,} rps", dx=12, dy=-16)
    rate_axis(ax, run)
    ax.text(ax.get_xlim()[0] * 1.1, run.slo_ms * 1.03, f"{run.slo_ms:g} ms promise", color=INK2,
            fontsize=8.5)
    ax.set_ylabel("p99 latency (ms)")
    ax.set_ylim(0, top)
    ax.legend(loc="upper left")
    confirm = {s: run.confirm_p99(s) for s in run.slots}
    tail = (" Both are above the line, so the true limits sit a little lower."
            if min(confirm.values()) > run.slo_ms else "")
    footer(ax, f"Run {run.run_id}. Each dot is the mean of one rate's measured windows. Re-run "
           "at exactly its own limit in the confirm stage, " +
           " and ".join(f"{run.short(s)} averaged {confirm[s]:.1f} ms" for s in run.slots) +
           "." + tail)
    save(fig, "fig1-p99-vs-rate.png")


def _curve_figure(run: Run, key: str, title: str, subtitle: str, ylabel: str, at_limit,
                  foot: str, name: str, legend_loc: str) -> None:
    hi, lo, ratio = run.ranked(key)
    fig, ax = plt.subplots(figsize=(8, 4.6))
    header(ax, title.format(hi=run.short(hi), lo=run.short(lo), ratio=ratio), subtitle)
    top = 0.0
    for slot in run.slots:
        pr = run.curve(slot)
        xs, ys = [r["rate"] for r in pr], [r[key] for r in pr]
        top = max(top, max(ys))
        line(ax, run, slot, xs, ys)
        ky = run.at_limit(slot)[key]
        big_dot(ax, run, slot, run.limit[slot], ky)
        note(ax, run.limit[slot], ky, at_limit(ky), dx=12, dy=(14 if slot == hi else -14))
    first = {s: run.curve(s)[0] for s in run.slots}
    if first[hi]["rate"] == first[lo]["rate"]:
        note(ax, first[hi]["rate"], first[hi][key],
             f"{first[hi][key] / first[lo][key]:.1f}x at {first[hi]['rate']:,} rps",
             dx=4, dy=16, bold=False, color=INK2)
    rate_axis(ax, run)
    ax.set_ylabel(ylabel)
    ax.set_ylim(0, top * 1.22)
    ax.legend(loc=legend_loc)
    footer(ax, foot)
    save(fig, name)


def fig_cpu(run: Run) -> None:
    p = run.cell["paired_primary"]
    d = p["mean_diff_pct"]
    lo_ci, hi_ci = sorted((abs(p["ci_low_pct"]), abs(p["ci_high_pct"])))
    _curve_figure(
        run, "cpu_ms",
        "{hi} spent {ratio:.1f}x the CPU of {lo} for the same request",
        "CPU time per request, counted by the Linux kernel, as traffic rose; big dots mark "
        "each stack's limit",
        "CPU milliseconds per request",
        lambda v: f"{v:.3f} ms at its limit",
        f"Run {run.run_id}. Windows that never fully warmed up are left out. Back-to-back runs "
        f"at the limits ({p['n_blocks']} pairs): {run.short(run.subject)} used {abs(d):.1f}% "
        f"{'less' if d < 0 else 'more'} CPU than {run.short(run.reference)} "
        f"({p['ci_pct']:g}% interval {lo_ci:.1f} to {hi_ci:.1f}).",
        "fig2-cpu-per-request.png", "lower left")


def fig_memory(run: Run) -> None:
    res = run.cell["resource"]["by_slot"]
    heap = {s: max(w["heap_max_mib"] for w in run.windows if w["slot"] == s) for s in run.slots}
    _curve_figure(
        run, "anon_mb",
        "{hi} held {ratio:.1f}x the memory of {lo}",
        "Memory each service was actually holding while serving traffic, counted by the "
        "Linux kernel",
        "Memory in use (MB)",
        lambda v: f"{v:,.0f} MB at its limit",
        f"Run {run.run_id}. Memory in use is the p99 of the container's anonymous memory "
        "inside each measured window. Largest heap in any window: " +
        ", ".join(f"{run.short(s)} {heap[s]:,.0f} MiB" for s in run.slots) +
        ". Whole-life container peaks, file cache included: " +
        ", ".join(f"{run.short(s)} {res[s]['mem_peak_mb']:,.0f} MB" for s in run.slots) + ".",
        "fig3-memory-vs-rate.png", "center")


def fig_app_vs_db(run: Run) -> None:
    pts = {s: run.at_limit(s) for s in run.slots}
    fig, ax = plt.subplots(figsize=(8, 2.9))
    header(ax, "The database did about the same work for both",
           "CPU per request at each stack's limit: the app itself plus PostgreSQL")
    stacked_h(ax, [run.long(s) for s in run.slots],
              [(pts[s]["cpu_ms"], pts[s]["db_ms"]) for s in run.slots],
              lambda v: f"{v:.3f}", " ms")
    paint(ax, run, POSTGRES, "PostgreSQL (either stack)")
    ax.set_xlabel("CPU milliseconds per request")
    footer(ax, f"Run {run.run_id}. PostgreSQL CPU per request: " +
           ", ".join(f"{pts[s]['db_ms']:.3f} ms serving {run.short(s)}" for s in run.slots) +
           ". On this schema Hibernate needs 11.5 database round trips per created invoice "
           "against EF Core's 6 (spec/db-access.md).")
    save(fig, "fig4-app-vs-db-cpu.png")


def fig_warmup(run: Run) -> None:
    rng = random.Random(7)
    floor = min(w["warm_s"] for w in run.windows)
    cap = run.warmup_cap_s
    fig, ax = plt.subplots(figsize=(8, 3.6))
    header(ax, "Time to settle under traffic, one dot per test run",
           "Seconds of warm-up traffic before latency was flat, garbage collection steady and "
           "the pools full")
    for x, text in ((floor, f" {floor:.0f} s: fastest possible"), (cap, f" {cap:.0f} s: time limit")):
        ax.axvline(x, color=MUTED, lw=1.2)
        ax.text(x, 1.66, text, color=INK2, fontsize=8.5)
    for yi, slot in zip((1, 0), run.slots):
        xs = [w["warm_s"] for w in run.windows if w["slot"] == slot]
        never = sum(w["under_warmed"] for w in run.windows if w["slot"] == slot)
        ax.scatter(xs, [yi + rng.uniform(-0.16, 0.16) for _ in xs], s=46, color=run.color(slot),
                   edgecolor=SURFACE, linewidth=1.5, alpha=0.9, zorder=3)
        ax.text(floor + 6, yi + 0.46, f"{run.short(slot)}: median {st.median(xs):.0f} s",
                color=INK, fontsize=9, fontweight="bold")
        ax.text(floor + 6, yi + 0.29,
                f"{sum(x <= floor + 1 for x in xs)} of {len(xs)} in the fastest possible time, "
                f"{sum(x >= cap for x in xs)} hit the time limit, {never} never settled",
                color=INK2, fontsize=8.5)
    ax.set_yticks([1, 0])
    ax.set_yticklabels([run.long(s) for s in run.slots], color=INK)
    ax.set_ylim(-0.5, 1.95)
    ax.set_xlim(floor - 13, cap + 20)
    ax.set_xlabel("Seconds until settled")
    ax.grid(axis="y", visible=False)
    footer(ax, f"Run {run.run_id}. Settled = enough calls to every endpoint, flat latency, "
           "steady garbage collection, full connection pools. Runs that never settled are left "
           "out of every other figure. This is not app start-up time, which the run does not "
           "record.")
    save(fig, "fig5-warmup.png")


def _prices() -> str:
    return (f"AWS Fargate us-east-1 Linux/x86 list prices, {PRICE_AS_OF}: "
            f"${VCPU_HOUR_USD:.4f} per vCPU-hour, ${GB_HOUR_USD:.4f} per GB-hour")


def fig_cost(run: Run) -> None:
    fig, ax = plt.subplots(figsize=(8, 2.9))
    header(ax, "Same speed, different bill",
           "Dollars per hour for the CPU and memory each stack actually used at its limit")
    stacked_h(ax, [run.long(s) for s in run.slots],
              [run.bill(s, run.at_limit(s), run.limit[s]) for s in run.slots],
              lambda v: f"${v:.3f}", "/h")
    paint(ax, run, RAM, "Memory (either stack)", first="CPU")
    ax.set_xlabel("$ per hour")
    footer(ax, f"Run {run.run_id}. {_prices()}. The prices are AWS's and the chart is linear in "
           "them; the gap between the bars is the measured part.")
    save(fig, "fig6-cost-per-hour.png")


def fig_cost_sensitivity(run: Run) -> None:
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4.2))
    fig.subplots_adjust(wspace=0.34)

    header(a1, "Who is cheaper at your traffic",
           "$ per hour for the CPU and memory actually used")
    top = 0.0
    for slot in run.slots:
        pr = run.curve(slot)
        xs = [r["rate"] for r in pr]
        ys = [sum(run.bill(slot, r, r["rate"])) for r in pr]
        top = max(top, max(ys))
        line(a1, run, slot, xs, ys)
        note(a1, xs[-1], ys[-1], f"${ys[-1]:.3f}/h")
    rate_axis(a1, run, step=2000)       # half-width panel: 500 and 1,000 would collide
    a1.set_ylabel("$ per hour")
    a1.set_ylim(0, top * 1.25)
    a1.legend(loc="upper left")
    # Below the lowest rung only the memory line is left to pay for. Extrapolating
    # the lowest rung's own per-request CPU down gives where the two bills meet.
    hi_cpu, lo_cpu, _ = run.ranked("cpu_ms")
    first = {s: run.curve(s)[0] for s in run.slots}
    ram_gap = (first[lo_cpu]["anon_mb"] - first[hi_cpu]["anon_mb"]) / 1000 * GB_HOUR_USD
    cpu_gap = (first[hi_cpu]["cpu_ms"] - first[lo_cpu]["cpu_ms"]) / 1000 * VCPU_HOUR_USD
    if ram_gap > 0 and cpu_gap > 0:
        a1.text(0.46, 0.04,
                f"Extrapolated below the\nmeasured range, the lines\ncross near "
                f"{round(ram_gap / cpu_gap, -1):,.0f} rps. Under that,\n"
                f"{run.short(hi_cpu)}'s smaller footprint wins.",
                transform=a1.transAxes, color=INK2, fontsize=8.5, va="bottom")

    header(a2, "What if memory got much more expensive?",
           "$ per hour at the limit as the GB-hour price is multiplied")
    mults = list(range(1, 41))
    top = 0.0
    for slot in run.slots:
        ys = [sum(run.bill(slot, run.at_limit(slot), run.limit[slot], m)) for m in mults]
        top = max(top, max(ys))
        a2.plot(mults, ys, color=run.color(slot), lw=2, label=run.long(slot),
                solid_capstyle="round")
        note(a2, mults[-1], ys[-1], run.short(slot), dx=6)
    cpu = {s: run.bill(s, run.at_limit(s), run.limit[s])[0] for s in run.slots}
    ram = {s: run.bill(s, run.at_limit(s), run.limit[s])[1] for s in run.slots}
    a, b = run.slots
    if (ram[a] - ram[b]) * (cpu[b] - cpu[a]) > 0:
        cross = (cpu[b] - cpu[a]) / (ram[a] - ram[b])
        a2.axvline(cross, color=MUTED, lw=1.2)
        a2.text(cross + 0.8, top * 0.07, f"bills equal at {cross:.0f}x\ntoday's memory price",
                color=INK2, fontsize=8.5)
    a2.set_xlabel(f"Memory price multiplier (1 = {PRICE_AS_OF} list price)")
    a2.set_ylabel("$ per hour at the limit")
    a2.set_xlim(1, 47)
    a2.set_ylim(0, top * 1.15)
    a2.legend(loc="upper left")
    footer(a1, f"Run {run.run_id}. {_prices()}. CPU and memory use are measured; the prices are "
           "a vendor quote.", width=150)
    save(fig, "fig7-cost-sensitivity.png")


FIGURES = (fig_rig, fig_latency, fig_cpu, fig_memory, fig_app_vs_db, fig_warmup, fig_cost,
           fig_cost_sensitivity)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python figures.py <run_id>", file=sys.stderr)
        return 2
    run = Run(argv[0])
    for draw in FIGURES:
        draw(run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
