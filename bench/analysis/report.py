"""Markdown report assembly.

Pure functions that turn analysis outputs (paired verdicts, knee estimates,
GC/pool tables, the DB-CPU-per-request attribution) into markdown tables for
results/<run_id>/report/ and docs/ snippets. pandas assembles the frames.
No figures are produced; the caller gets markdown and the fits JSON.

The $/M illustrative cost uses an EXPLICIT formula with PINNED prices and a
sensitivity band so it is reproducible and clearly labeled illustrative, not a
vendor quote.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pandas as pd


def verdict_word(
    verdict: str, *, subject: str, reference: str, labels: Mapping[str, str]
) -> str:
    """Render a paired verdict using the CALLER'S labels for the two sides.

    The verdict token is deliberately side-neutral ("subject_better"), so the
    display name has to come from outside this package. `labels` maps a slot id
    to its report label; a verdict-to-name table hard-coded here would render
    the wrong stack's name the moment the compared pair changes.
    """
    if verdict == "tie":
        return "tie (within +-margin)"
    if verdict == "inconclusive":
        return "inconclusive (interval spans zero)"
    side = {"subject_better": subject, "reference_better": reference}.get(verdict)
    if side is None:
        return verdict
    return f"{labels.get(side, side)} better"


DB_CPU_ROW_KEYS = (
    "cell", "offered_rps", "slot", "windows", "requests", "db_requests",
    "app_cpu_ms_per_req", "db_cpu_ms_per_req", "total_cpu_ms_per_req",
    "db_share_pct", "source",
)


def db_cpu_attribution_table(
    rows: list[dict], *, labels: Mapping[str, str] | None = None
) -> str:
    """App vs PostgreSQL CPU per request, keyed (cell, offered rate, slot).

    NOT per endpoint: a cgroup `cpu.stat` delta is a whole-container number, so
    a per-rate figure under an endpoint key would read as finer-grained than the
    measurement supports.
    (cell, rate, slot) is the finest key the evidence carries: PostgreSQL
    serves exactly one slot's probe at a time, because a block's slots run
    sequentially. Per-endpoint attribution needs `pg_stat_statements` deltas
    and is a different measurement.

    Slot display names come from the caller, as in `verdict_word`: a table that
    spells its own stack names renders the wrong ones the moment the compared
    pair changes.
    """
    labels = labels or {}
    df = pd.DataFrame([
        {**r, "slot": labels.get(r.get("slot"), r.get("slot"))} for r in rows
    ], columns=list(DB_CPU_ROW_KEYS))
    return _df_to_md(df)


def gc_pool_table(rows: list[dict]) -> str:
    """Per-run GC + pool stats, one row per slot, columns taken from the rows.

    The columns are NOT fixed here. `gen0/gen1/gen2` is a CLR-native shape that
    does not survive G1/ZGC -- measured, `G1 Old Generation` counts Full GC only
    and reads 0 where `ZGC Major Cycles` reads 3 on identical work (see
    docs/research/2026-08-21-measured.md, FINDING 6). Forcing the JVM into that
    triple would freeze three different meanings under one field name, so the
    caller supplies whatever per-collector rows its runtime honestly has and
    this function only renders them. The frozen field contract lives in
    spec/runtime-stats.md, not here.
    """
    return _df_to_md(pd.DataFrame(rows))


@dataclass(frozen=True)
class CostInputs:
    """Pinned inputs for the illustrative $/M-requests figure (fully disclosed)."""

    vcpu_hour_usd: float          # pinned cloud vCPU-hour price
    cpu_ms_per_req_by_slot: Mapping[str, float]
    labels: Mapping[str, str] = field(default_factory=dict)
    sensitivity_pct: float = 20.0  # +-band on the price assumption


def cost_per_million(inputs: CostInputs) -> dict:
    """Illustrative $/M requests with the explicit formula + sensitivity band.

    formula:  $/M = (cpu_ms_per_req / 1000) * (vcpu_hour_usd / 3600) * 1e6
            = cpu_ms_per_req * vcpu_hour_usd / 3.6   (USD per million requests)
    """
    def usd_per_m(cpu_ms: float, price: float) -> float:
        return cpu_ms * price / 3.6

    band = inputs.sensitivity_pct / 100.0
    out: dict = {"by_slot": {}}
    for slot, cpu in inputs.cpu_ms_per_req_by_slot.items():
        out["by_slot"][slot] = {
            "usd_per_million": usd_per_m(cpu, inputs.vcpu_hour_usd),
            "low": usd_per_m(cpu, inputs.vcpu_hour_usd * (1 - band)),
            "high": usd_per_m(cpu, inputs.vcpu_hour_usd * (1 + band)),
        }
    out["formula"] = "usd_per_million = cpu_ms_per_req * vcpu_hour_usd / 3.6"
    out["vcpu_hour_usd"] = inputs.vcpu_hour_usd
    out["sensitivity_pct"] = inputs.sensitivity_pct
    return out


def cost_table(inputs: CostInputs) -> str:
    """The $/M table + its formula note.

    Four decimals, not two: at a per-request cost under a millisecond and a
    vCPU-hour price in cents, `$/M` lands in the thousandths, and `:.2f`
    rendered every slot as `$0.00` -- a published zero for a figure the run
    measured. The formula is READ BACK from `cost_per_million` rather than
    restated here; the two strings agreed by inspection, which is exactly the
    kind of agreement that stops holding after one edit.
    """
    out = cost_per_million(inputs)
    c = out["by_slot"]
    rows = [
        {
            "slot": inputs.labels.get(slot, slot),
            "$/M (mid)": f"${v['usd_per_million']:,.4f}",
            f"+-{inputs.sensitivity_pct:g}% band":
                f"[${v['low']:,.4f}, ${v['high']:,.4f}]",
        }
        for slot, v in c.items()
    ]
    table = _df_to_md(pd.DataFrame(rows))
    note = (
        f"\n*Illustrative only.* "
        f"`{out['formula']}`; "
        f"pinned vCPU-hour = ${inputs.vcpu_hour_usd:.4f}; "
        f"sensitivity band +-{inputs.sensitivity_pct:g}%.\n"
    )
    return table + note


def _df_to_md(df: pd.DataFrame) -> str:
    """DataFrame -> GitHub markdown table, without requiring the `tabulate` dep."""
    if df.empty:
        return "_(no data)_\n"
    cols = list(df.columns)
    header = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [header, sep]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(str(row[c]) for c in cols) + " |")
    return "\n".join(lines) + "\n"
