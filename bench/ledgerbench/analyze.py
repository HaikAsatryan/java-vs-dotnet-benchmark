"""Post-run analysis: knee fits + CIs, stationarity, paired verdicts -> report.

Reads a finished `results/<run_id>/ledger.json` and turns the persisted
per-probe ledger data into the publishable analysis artifacts the docs promise
(`make analyze RUN=<id>`):

  - results/<run_id>/report/fits.json: machine-readable PER-SLOT knee estimates,
    the CPU-ms/request-vs-rate curve, the per-slot p99-vs-SLO conformance readout
    at each slot's operating rate, stationarity summaries, the paired
    subject-vs-reference COST verdict, and the resource roll-up.
  - results/<run_id>/report/report.md: the same, assembled as markdown tables.

The fit inputs come from the SAME code path the live runner used to bracket the
SLO crossing: `phases._rung_values` reads the per-probe report p99 (the SLO
ground truth) and excludes under-warmed probes, and the crossing PREDICATE is
the one `analysis.knee.crosses` defines, which the runner's own ladder stop rule
now calls. We reuse both verbatim rather than re-deriving the rung shape, so the
analysis fits exactly what the run measured and agrees with where it stopped.

Five invariants this module enforces:

  1. Under-warmed probes never enter a roll-up silently. Every aggregate (the
     resource cost, the CPU-vs-rate curve, the per-slot p99 readout) applies
     the same exclusion the fit input applies, and reports the used/flagged
     counts next to the number. Where a slot has NO warm window at all, the
     numbers are still emitted, but labelled posture evidence, not a gated
     measurement, and the reason is DERIVED from the cell's own knobs and the
     per-window gate records, never asserted (see `_posture_notes`).
  2. A paired verdict carries its operating points. Each slot confirms at ITS
     OWN fitted knee, so a verdict states `rates_by_slot` rather than one rate,
     and `verdict_scope` says whether it is the own-knee headline, the same
     comparison at a declared rate no SLO crossing evidences, or the
     common-rate contrast. They are never merged.
  3. One aggregation rule for CPU-ms/request, stated in the report. Every
     published CPU-ms/request (the roll-up and every rung of the CPU-vs-rate
     curve) is REQUEST-WEIGHTED: total cgroup CPU delta over the aggregated
     windows divided by total requests serviced in them. So the request count
     printed beside it really is its denominator. The equal-weight-per-window
     alternative is computed too and the report quantifies the difference,
     because the two slots' window sets do not share a stage composition
     (a 600 s soak window services ~5x the requests of a confirm window).
  4. An SLO verdict states its rule. "Meets the promise" is the MEAN of the
     per-window CONFIRM/SOAK p99s against the threshold (a lenient rule for a
     tail promise), so the count of windows that individually met it, and the
     worst window, are published in the same row. It is a DIFFERENT quantity
     from the ladder's crossing predicate -- different stage, different windows,
     and the opposite inequality at the boundary -- and the report says so.
  5. Every ratio carries its direction. Which slot is the numerator is declared
     by `registry.SlotSet.reference` and travels with the number as a
     `RatioDirection`, rather than being encoded in a key name that can drift
     away from the arithmetic.

The analysis modules, which need the scientific stack (numpy/scipy/pandas), are
imported lazily inside the functions that need them.
Sections the run does not support (anything resting on cgroup evidence, which a
Windows/smoke run has none of) are reported as an explicit "not available in
this run" line, never fabricated -- and the line is derived from THIS run's
artifacts, so a capability the harness has cannot be described as absent.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import capacity_gate as capgate
from . import checksums, compose, phases
from .analysis_bridge import estimate_knee_from_rungs, stationarity_fails
from .config import load_slo, load_workload
from . import registry
from .ledger import REGATE_PHASE, Ledger, State, probe_key, split_probe_key
from .runtimestats import NOT_MEASURED as _RS_NOT_MEASURED
from .runtimestats import PHASE_MEASURED as _RS_PHASE_MEASURED
from .util import paths
from .warmup import (
    ENFORCING_STATUSES,
    IMPLEMENTED_GATES,
    STATUS_MEASURED,
    STATUS_NOT_APPLICABLE,
    STATUS_POSTURE_WAIVED,
    gate_set_for_cell,
)

# Stages whose per-second p99 series back the stationarity gate (the same two
# stages the runner records `p99_series` for: the ladder and the long soak).
_STATIONARITY_STAGES = ("ladder", "soak")

# Stages whose per-probe-window cgroup summaries back the resource roll-up.
_RESOURCE_STAGES = ("confirm", "soak")

# The co-sampled cgroup whose CPU delta is the DB column. It is the compose
# SERVICE name, which is also the sampling label the runner records, so this
# resolves the artifact name rather than spelling it.
_DB_LABEL = compose.SVC_POSTGRES

class SpecDriftError(RuntimeError):
    """spec/ no longer hashes to the run's pinned config_hash at analyze time."""


def _json_sanitize(obj):
    """NaN/inf -> None, recursively, for the strict-JSON artifact boundary."""
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    if isinstance(obj, dict):
        return {k: _json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_sanitize(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# small shared helpers over the persisted per-probe maps
# --------------------------------------------------------------------------- #
def _is_flagged(stage_data: dict, key: str, *, default: bool = False) -> bool:
    """True if this probe is under-warmed or gate-excluded (never measurement-grade).

    The ledger is the authority; `default` covers a probe present on disk but
    absent from the ledger map (an out-of-band window), where the caller passes
    the per-window `<lang>.warmup.json` verdict instead.
    """
    under_warmed = stage_data.get("under_warmed", {})
    excluded = stage_data.get("excluded", {})
    flagged = under_warmed.get(key, default)
    return bool(flagged) or key in excluded


def _rungs_by_rate_for_slot(stage_rec, slot: str, *, warm_only: bool = True) -> dict[int, list[float]]:
    """{rate: [per-probe report p99 ms]} for ONE slot: the fit input.

    The warm case DELEGATES to `phases._rung_values`, which is the code path the
    live runner's own ladder bracketed on. That is not tidiness: this module's
    docstring claims the analysis fits exactly the rungs the run measured, and a
    second copy of the exclusion rule is how that claim stops being true after
    one edit to either side.

    `warm_only=False` keeps under-warmed probes and is used ONLY for the explicit
    posture-evidence path (a slot with zero warm probes in the whole ladder),
    which the runner has no counterpart for -- it never fits over flagged probes.
    """
    if warm_only:
        by_rate, _empty = phases._rung_values(stage_rec, slot)
        return by_rate
    data = stage_rec.data
    by_rate = {}
    for key, p99 in data.get("p99_ms", {}).items():
        parsed = split_probe_key(key)
        if parsed is None or parsed[2] != slot:
            continue
        by_rate.setdefault(parsed[0], []).append(float(p99))
    return by_rate


def _p99_by_rate_slot(stage_rec) -> dict[tuple[int, str], dict[str, list[float]]]:
    """{(rate, slot): {"warm": [...], "all": [...]}} of per-probe pooled p99."""
    data = stage_rec.data
    out: dict[tuple[int, str], dict[str, list[float]]] = {}
    for key, p99 in data.get("p99_ms", {}).items():
        parsed = split_probe_key(key)
        if parsed is None:
            continue
        rate, _block, lang = parsed
        slot = out.setdefault((rate, lang), {"warm": [], "all": []})
        slot["all"].append(float(p99))
        if not _is_flagged(data, key):
            slot["warm"].append(float(p99))
    return out


def _exclusions_by_rate_slot(stage_rec) -> dict[tuple[int, str], dict[str, int]]:
    """{(rate, lang): {reason_kind: n}}: why each dropped window was dropped.

    A window can fail the warm-up gates AND the CO-safety gates at once, so each
    dropped probe is attributed to exactly ONE reason, gate exclusion first:
    "under-warmed" is the weaker statement and is never reported for a probe
    that also failed the error-rate gate. Counted this way the reasons sum to
    (total - used) for every row.
    """
    out: dict[tuple[int, str], dict[str, int]] = {}
    data = stage_rec.data
    excluded = data.get("excluded", {})
    for key, reason in excluded.items():
        parsed = split_probe_key(key)
        if parsed is None:
            continue
        rate, _block, slot = parsed
        kind = "error rate" if str(reason).startswith("error_rate") else "rate fidelity"
        counts = out.setdefault((rate, slot), {})
        counts[kind] = counts.get(kind, 0) + 1
    for key, flagged in data.get("under_warmed", {}).items():
        if not flagged or key in excluded:
            continue
        parsed = split_probe_key(key)
        if parsed is None:
            continue
        rate, _block, slot = parsed
        counts = out.setdefault((rate, slot), {})
        counts["under-warmed"] = counts.get("under-warmed", 0) + 1
    return out


def _exclusion_summary(counts: dict[str, int]) -> str:
    """E.g. "3 under-warmed, 6 error rate": the reasons a rung's windows were dropped."""
    if not counts:
        return "-"
    order = {"under-warmed": 0, "error rate": 1, "rate fidelity": 2}
    return ", ".join(
        f"{n} {kind}" for kind, n in sorted(counts.items(), key=lambda kv: order.get(kv[0], 9))
    )


def _cell_by_id(cell_id: str) -> "phases.Cell | None":
    """This cell's declaration, from the same `phases.CELLS` table the runner ran."""
    for cell in phases.CELLS:
        if cell.cell_id == cell_id:
            return cell
    return None


def _cell_extra_env(cell_id: str) -> dict[str, str]:
    """The per-cell runtime knob overrides this cell is declared with TODAY.

    `phases.CELLS` as it stands at ANALYSIS time, not as the runner read it: a
    cell whose knobs changed after the run reports its current ones here. That
    is the difference between this and `_waived_gate_names`, which reads the
    probe's own record. Good enough for the causal clause it feeds -- which
    names a knob and a registry entry, both re-derivable -- and NOT good enough
    to stand as the run's record of what was set: `manifest.knobs_by_cell[
    cell_id].extra_env`, written by `phases._record_cell_knobs` before the
    cell's first probe, is that.
    """
    cell = _cell_by_id(cell_id)
    return dict(cell.extra_env) if cell else {}


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cv_pct(values: list[float]) -> float | None:
    """Coefficient of variation (%): None when it is undefined (n<2 or mean 0)."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    mean = statistics.fmean(vals)
    if mean == 0:
        return None
    return 100.0 * statistics.stdev(vals) / mean


def _fmt(x: float | None, spec: str = ".3f", dash: str = "n/a") -> str:
    # NaN is this module's in-memory "no data" sentinel (SlotResource); it must
    # render as the dash, never as the literal "nan" (a suffixed format would
    # print "nanx").
    if x is None or x != x:
        return dash
    return format(x, spec)


# --------------------------------------------------------------------------- #
# dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RatioDirection:
    """WHICH WAY every published ratio points, carried beside the number.

    A direction encoded in a key name can drift from the arithmetic. This one is
    generated from the same `registry.SlotSet.reference` the arithmetic uses, so
    the artifact and the division cannot disagree, and it stays correct for a
    slot set whose two members are the same language (P2).
    """

    subject: str                 # the NUMERATOR slot id
    reference: str               # the DENOMINATOR slot id
    label: str                   # e.g. "Java : .NET" -- the report column header

    @classmethod
    def of(cls, slot_set: registry.SlotSet) -> "RatioDirection":
        return cls(
            subject=slot_set.subject,
            reference=slot_set.reference,
            label=slot_set.ratio_label(),
        )

    def divide(self, by_slot: Mapping[str, float | None]) -> float | None:
        """subject / reference, or None when either side is missing.

        A zero NUMERATOR is a real ratio (0.0) and is published as one; only a
        missing value or a zero denominator yields None. The round-1 form was
        `d / g if g else None`, which folded "the subject measured zero" into
        "there is nothing to divide" -- the same value standing for two states.
        """
        num, den = by_slot.get(self.subject), by_slot.get(self.reference)
        if num is None or not den:
            return None
        return num / den

    def to_dict(self) -> dict:
        return {"subject": self.subject, "reference": self.reference, "label": self.label}


@dataclass
class KneeFit:
    """One slot's knee estimate + the provenance needed to read it honestly.

    `scope` is the slot id this fit is FOR. There is no other scope: W24 deleted
    the pooled fit, which had been the thing that selected one shared confirm
    rate and so made the headline a comparison at whichever stack degraded first
    (R1-A1). Every knee in this report is one slot's own crossing over its own
    rungs.

    `crossed` is the ledger's verdict for this slot (`ladder.crossed_by_slot`)
    AND this refit's, and they cannot disagree: `phases._bracket_crossing` and
    `estimate_knee` both route through `analysis.knee`'s single crossing
    predicate. `crossed_agrees_with_ledger` records the check rather than
    assuming it.
    """

    crossed: bool
    scope: str
    knee_rps: float | None = None
    ci_low_rps: float | None = None
    ci_high_rps: float | None = None
    ci_valid: bool | None = None
    min_reps_in_bracket: int | None = None
    # Bootstrap resamples the printed interval was taken over, carried from the
    # estimator instead of restated, so the CI note cannot quote a resample
    # count the fit did not use.
    n_boot: int | None = None
    # How many of those resamples actually crossed in-range; the remainder are
    # right-censored at the top rung. When the upper percentile fell in that
    # censored mass, `ci_high_censored` is True and `ci_high_rps` is the top
    # rung, to be read ">= top", not a resolved quantile.
    n_crossed: int | None = None
    ci_high_censored: bool | None = None
    n_rungs: int = 0
    n_rungs_dropped: int = 0        # rungs with no measurement-grade probe in scope
    warm_only: bool = True          # False => fitted over under-warmed probes (posture)
    max_rung_rps: float | None = None
    max_rung_p99_ms: float | None = None
    warning: str | None = None
    note: str | None = None
    # The ladder's own recorded verdict for this slot, and whether this refit
    # reproduces it. Round 1 had four live definitions of "crossed the SLO" and
    # nothing that compared them.
    ledger_crossed: bool | None = None
    crossed_agrees_with_ledger: bool | None = None

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "crossed": self.crossed,
            "knee_rps": self.knee_rps,
            "ci_low_rps": self.ci_low_rps,
            "ci_high_rps": self.ci_high_rps,
            "ci_valid": self.ci_valid,
            "min_reps_in_bracket": self.min_reps_in_bracket,
            "n_boot": self.n_boot,
            "n_crossed": self.n_crossed,
            "ci_high_censored": self.ci_high_censored,
            "n_rungs": self.n_rungs,
            "n_rungs_dropped": self.n_rungs_dropped,
            "warm_only": self.warm_only,
            "max_rung_rps": self.max_rung_rps,
            "max_rung_p99_ms": self.max_rung_p99_ms,
            "warning": self.warning,
            "note": self.note,
            "ledger_crossed": self.ledger_crossed,
            "crossed_agrees_with_ledger": self.crossed_agrees_with_ledger,
        }


@dataclass
class SloAtRate:
    """One slot's own measured p99 at one offered rate, against the SLO.

    `meets_slo` applies ONE stated rule: the MEAN of the aggregated windows'
    p99 values against the threshold. That is lenient for a tail promise, so
    `n_windows_met_slo` (how many windows individually met it) and `max_p99_ms`
    (the worst one) travel with it and are printed in the same table row.
    """

    stage: str
    rate: int
    slot: str
    n_warm: int
    n_total: int
    n_used: int                      # windows behind the mean/worst/met counts
    n_windows_met_slo: int
    mean_p99_ms: float
    max_p99_ms: float
    meets_slo: bool                  # rule: mean of per-window p99 <= slo_ms
    posture_only: bool               # True => no warm window; flagged probes only
    dropped_by: dict[str, int] = field(default_factory=dict)  # reason -> n, sums to total-used

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "rate": self.rate,
            "slot": self.slot,
            "n_warm": self.n_warm,
            "n_total": self.n_total,
            "n_used": self.n_used,
            "n_windows_met_slo": self.n_windows_met_slo,
            "mean_p99_ms": self.mean_p99_ms,
            "max_p99_ms": self.max_p99_ms,
            "meets_slo": self.meets_slo,
            "meets_slo_rule": "mean of per-window p99 <= slo_ms",
            "posture_only": self.posture_only,
            "dropped_by": dict(self.dropped_by),
        }


@dataclass
class CpuRungValue:
    """One slot's CPU-ms/request at one ladder rung, with its window provenance.

    `db_cpu_ms_per_req` is PostgreSQL's cgroup CPU over the same windows under
    the same rule. `n_db` counts the windows that carried a DB sample, which is
    what separates "the database cost nothing" from "the database was not
    sampled" -- the value is None in the second case, never 0.0.

    `requests` and `db_requests` are the two figures' actual DENOMINATORS, and
    they are two fields because `_cpu_ms_per_req` takes numerator and
    denominator over the same subset: the DB figure is divided by the requests
    of the DB-SAMPLED windows only. Without both, a row whose `n_db` is short of
    `n_total` cannot be reproduced from what it publishes -- and the attribution
    table used to print the WINDOW count under a `requests` header, which is a
    denominator claim off by the window size.
    """

    cpu_ms_per_req: float | None
    n_warm: int
    n_total: int
    posture_only: bool
    db_cpu_ms_per_req: float | None = None
    n_db: int = 0
    requests: int = 0                # denominator of cpu_ms_per_req
    db_requests: int = 0             # denominator of db_cpu_ms_per_req

    def to_dict(self) -> dict:
        return {
            "cpu_ms_per_req": self.cpu_ms_per_req,
            "db_cpu_ms_per_req": self.db_cpu_ms_per_req,
            "n_warm": self.n_warm,
            "n_total": self.n_total,
            "n_db": self.n_db,
            "requests": self.requests,
            "db_requests": self.db_requests,
            "posture_only": self.posture_only,
        }


@dataclass
class CpuRateRung:
    """CPU-ms/request for every slot at ONE ladder rung."""

    rate: int
    by_slot: dict[str, CpuRungValue]
    ratio: float | None                  # subject / reference (see RatioDirection)

    def to_dict(self) -> dict:
        return {
            "rate": self.rate,
            "by_slot": {k: v.to_dict() for k, v in self.by_slot.items()},
            "ratio": self.ratio,
        }


@dataclass
class CpuVsRate:
    """CPU-ms/request as a function of offered rate, per language.

    The headline ratio is a single operating point; this is the curve behind it.
    If per-request cost were rate-independent (the assumption the "cost at a
    fixed latency promise" framing rests on), both columns would be flat and the
    CVs near zero. They are not, so the curve is published.
    """

    available: bool
    rungs: list[CpuRateRung] = field(default_factory=list)
    cv_pct_by_slot: dict[str, float | None] = field(default_factory=dict)
    ratio_min: float | None = None
    ratio_max: float | None = None
    ratio_direction: RatioDirection | None = None
    n_posture_only_values: int = 0
    note: str = ""

    def n_values(self, slot_id: str) -> int:
        """How many rungs this slot's CV was actually taken over.

        NOT `len(self.rungs)`: a rung with no window for one slot renders `n/a`
        in that column and `_cv_pct` drops the None, and since W24 a POSTURE-ONLY
        rung is excluded from the CV as well. Any sentence that quotes a rung
        count beside a CV has to quote THIS one, or it claims the dispersion was
        taken over rows the CV did not see.
        """
        return sum(
            1 for r in self.rungs
            if r.by_slot.get(slot_id) is not None
            and r.by_slot[slot_id].cpu_ms_per_req is not None
            and not r.by_slot[slot_id].posture_only
        )

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "rungs": [r.to_dict() for r in self.rungs],
            "cv_pct_by_slot": self.cv_pct_by_slot,
            "ratio_min": self.ratio_min,
            "ratio_max": self.ratio_max,
            "ratio_direction": (
                self.ratio_direction.to_dict() if self.ratio_direction else None
            ),
            "n_posture_only_values": self.n_posture_only_values,
            "note": self.note,
        }


@dataclass
class PairedVerdict:
    """A confirm-stage paired difference between the two slots, RCB-paired by block.

    `rates_by_slot`, not `rate`: each slot confirms at ITS OWN fitted knee, so a
    block has no single rate and a verdict has to state its operating points.
    `verdict_scope` says which question it answers -- `own_knee` (the headline
    metric's definition), `declared_operating_point` (the same comparison where
    at least one slot's declared rate is not an evidenced SLO crossing) or
    `common_rate` (the confound-free contrast) -- so they can never be read as
    one.
    """

    metric: str
    rates_by_slot: dict[str, int]
    verdict_scope: str
    n_blocks: int
    # The pre-registered floor (spec/slo.yaml ladder.confirm_reps). A verdict
    # below it used to render identically to a full one apart from the block
    # count; `under_min_blocks` makes the report label it instead.
    min_blocks: int
    # Blocks excluded BY CONSTRUCTION (one-member ladder-style blocks) vs blocks
    # that lost a probe to warm exclusion. The old pairing dropped both through
    # one key miss and could not tell them apart.
    n_blocks_solo: int
    n_blocks_incomplete: int
    mean_by_slot: dict[str, float]
    direction: RatioDirection
    mean_diff_pct: float             # 100 * (subject - reference) / reference
    ci_pct: float                    # confidence level of the interval (e.g. 95.0)
    ci_low_pct: float
    ci_high_pct: float
    margin_pct: float                # pre-registered equivalence margin (+-)
    verdict: str          # tie | inconclusive | subject_better | reference_better
    effect_size: float

    @property
    def under_min_blocks(self) -> bool:
        return self.n_blocks < self.min_blocks

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "rates_by_slot": self.rates_by_slot,
            "verdict_scope": self.verdict_scope,
            "n_blocks": self.n_blocks,
            "min_blocks": self.min_blocks,
            "under_min_blocks": self.under_min_blocks,
            "n_blocks_solo": self.n_blocks_solo,
            "n_blocks_incomplete": self.n_blocks_incomplete,
            "mean_by_slot": self.mean_by_slot,
            "direction": self.direction.to_dict(),
            "mean_diff_pct": self.mean_diff_pct,
            "ci_pct": self.ci_pct,
            "ci_low_pct": self.ci_low_pct,
            "ci_high_pct": self.ci_high_pct,
            "margin_pct": self.margin_pct,
            "verdict": self.verdict,
            "effect_size": self.effect_size,
        }


@dataclass
class SlotResource:
    """Per-slot steady-state SUT resource cost (cgroup v2 ground truth).

    `cpu_ms_per_req` is REQUEST-WEIGHTED (`cpu_ms_total / requests`), so
    `requests` is literally its denominator. `cpu_ms_per_req_unweighted` is the
    equal-weight-per-window alternative, kept because the two slots'
    aggregated windows need not share a stage composition (`stage_windows`) and
    a 600 s soak window services roughly 5x the requests of a confirm window;
    the report prints both ratios so the weighting choice is auditable rather
    than implicit.
    """

    n_windows: int                   # measurement-grade windows in the roll-up
    n_flagged_excluded: int          # windows dropped as under-warmed/gate-excluded
    posture_only: bool               # True => EVERY window was flagged; see note
    flagged_gate_counts: dict[str, int] = field(default_factory=dict)
    # Gates this posture was declared NOT held to, counted over EVERY window in
    # the roll-up (a waived gate cannot flag a window, so counting only the
    # flagged ones would report zero for exactly the postures that waived one).
    waived_gate_counts: dict[str, int] = field(default_factory=dict)
    # Gates that WERE applied to this posture and whose own record switched
    # enforcement off, counted over EVERY window in the roll-up for the same
    # reason the waivers are: a gate that cannot flag a window is absent from
    # `flagged_gate_counts` by construction, so counting flagged windows alone
    # reports zero exactly where the disclosure is needed.
    non_enforcing_gate_counts: dict[str, int] = field(default_factory=dict)
    # Flagged windows whose WARMUP ATTACK died -- the non-gate cause of
    # under-warmed. Counted separately because such a window is flagged with an
    # empty `flagged_gate_counts` entry, and without this the report would have
    # a flag it cannot account for and would reach for "no detail on disk".
    n_warmup_attack_failed: int = 0
    stage_windows: dict[str, int] = field(default_factory=dict)
    requests: int = 0
    non_2xx: int = 0
    create_share_pct: float | None = None
    cpu_ms_total: float = float("nan")
    cpu_ms_per_req: float = float("nan")            # request-weighted
    cpu_ms_per_req_unweighted: float = float("nan")  # equal weight per window
    # PostgreSQL's cgroup CPU over the SAME windows, under the SAME rule. None
    # (never 0.0) when no window carried a DB sample; `db_n_windows` says how
    # many did, because the DB numerator and denominator are taken over that
    # subset alone.
    db_cpu_ms_total: float | None = None
    db_cpu_ms_per_req: float | None = None
    db_n_windows: int = 0
    anon_p99_mb: float = float("nan")
    mem_peak_mb: float = float("nan")
    # FALSE, never True, when no window carried the counter: `all()` over an
    # empty set is True, so an unreadable counter would otherwise publish
    # "clean" as though it had been checked. That is reachable, not theoretical
    # -- a kernel without CONFIG_MEMCG_SWAP, or booted `swapaccount=0`, has no
    # `memory.swap.current` at all. The `n_windows_without_*` counts below are
    # what tells "checked and clean" apart from "never established", and the
    # report renders all three.
    throttle_clean: bool = False
    oom_clean: bool = False
    # TWO swap verdicts, never one. `swap_current_clean` is "`memory.swap.current`
    # was 0 at both edges of every window in which BOTH the SUT and the
    # co-sampled PostgreSQL cgroup read it"; `swap_peak_clean` is the same
    # sentence over `memory.swap.peak`, the kernel's lifetime high-water mark,
    # which is the only half that catches a cgroup that swapped and faulted back
    # inside one window. They are separate because a kernel can expose one file
    # and not the other, and one merged field made the report claim a
    # high-water-mark read that never happened. The scope is `_window_rows`'s
    # `swap_docs`, and the counts below say how many windows read neither.
    #
    # Beside `throttle_clean`/`oom_clean` because swap invalidates a different
    # number: a throttled window deflates CPU, an OOM window invalidates
    # everything, and a window that SWAPPED understates `anon_p99_mb` --
    # swapped-out anon pages leave `memory.stat anon` and the headline is memory
    # per request.
    swap_current_clean: bool = False
    swap_peak_clean: bool = False
    # Windows whose counter could not be read at all, one count per verdict so
    # each rendered caveat belongs to the verdict it sits beside.
    n_windows_without_counters: int = 0          # the throttle counter
    n_windows_without_oom_counter: int = 0
    n_windows_without_swap_current: int = 0
    n_windows_without_swap_peak: int = 0
    # Of the windows BEHIND each swap verdict, how many co-sampled PostgreSQL.
    # `_window_rows` narrows `swap_docs` to the SUT alone for a window with no
    # PostgreSQL summary on disk, and a run without a PG sampler is a supported
    # path there, so the report renders the verdict's scope from these counts
    # instead of naming both cgroups as fixed text.
    n_swap_current_windows_with_db: int = 0
    n_swap_peak_windows_with_db: int = 0
    db_pool_total: int | None = None
    db_pool_max: int | None = None

    @property
    def total_cpu_ms_per_req(self) -> float | None:
        """App + database CPU per request, or None when the DB was not sampled."""
        if self.db_cpu_ms_per_req is None or self.cpu_ms_per_req != self.cpu_ms_per_req:
            return None
        return self.cpu_ms_per_req + self.db_cpu_ms_per_req

    @property
    def db_share_pct(self) -> float | None:
        total = self.total_cpu_ms_per_req
        if total is None or total == 0:
            return None
        return 100.0 * (self.db_cpu_ms_per_req or 0.0) / total

    def to_dict(self) -> dict:
        return {
            "n_windows": self.n_windows,
            "n_flagged_excluded": self.n_flagged_excluded,
            "posture_only": self.posture_only,
            "flagged_gate_counts": self.flagged_gate_counts,
            "waived_gate_counts": self.waived_gate_counts,
            "non_enforcing_gate_counts": self.non_enforcing_gate_counts,
            "n_warmup_attack_failed": self.n_warmup_attack_failed,
            "stage_windows": self.stage_windows,
            "requests": self.requests,
            "non_2xx": self.non_2xx,
            "create_share_pct": self.create_share_pct,
            "cpu_ms_total": self.cpu_ms_total,
            "cpu_ms_per_req": self.cpu_ms_per_req,
            "cpu_ms_per_req_aggregation": "request-weighted: cpu_ms_total / requests",
            "cpu_ms_per_req_unweighted": self.cpu_ms_per_req_unweighted,
            "db_cpu_ms_total": self.db_cpu_ms_total,
            "db_cpu_ms_per_req": self.db_cpu_ms_per_req,
            "db_cpu_ms_per_req_aggregation": (
                "request-weighted: db_cpu_ms_total / the requests serviced by the "
                "windows that carried a postgres delta (the SAME rule as the app "
                "row, over that subset)"
            ),
            "db_n_windows": self.db_n_windows,
            "total_cpu_ms_per_req": self.total_cpu_ms_per_req,
            "db_share_pct": self.db_share_pct,
            "anon_p99_mb": self.anon_p99_mb,
            "mem_peak_mb": self.mem_peak_mb,
            "throttle_clean": self.throttle_clean,
            "oom_clean": self.oom_clean,
            "swap_current_clean": self.swap_current_clean,
            "swap_peak_clean": self.swap_peak_clean,
            "n_windows_without_counters": self.n_windows_without_counters,
            "n_windows_without_oom_counter": self.n_windows_without_oom_counter,
            "n_windows_without_swap_current": self.n_windows_without_swap_current,
            "n_windows_without_swap_peak": self.n_windows_without_swap_peak,
            "n_swap_current_windows_with_db": self.n_swap_current_windows_with_db,
            "n_swap_peak_windows_with_db": self.n_swap_peak_windows_with_db,
            "db_pool_total": self.db_pool_total,
            "db_pool_max": self.db_pool_max,
        }


@dataclass
class ResourceCost:
    """Per-slot resource cost at the operating rate(s), or unavailable.

    The cgroup deltas live per-probe-window under `cells/<cell>/<stage>/.../`,
    not in the ledger; this is the "stats that live elsewhere" the report needs.
    `available=False` (no cgroup summaries on disk; a Windows/smoke run, or a
    run whose cells tree was not retained) renders an honest not-captured line
    instead of a fabricated number.

    `scope` names WHICH operating point this roll-up is taken at. It is
    `SCOPE_OWN_KNEE` -- the headline's own definition, each slot at ITS OWN
    SLO-crossing rate -- only when every slot HAS a fitted knee and was selected
    at exactly that knee's rate; otherwise it is `SCOPE_DECLARED_POINT` and
    `scope_degraded_slots` names each slot the SLO claim is not true of and why.
    It is a published field rather than an assumption
    because a confirm block since W24 also holds the slower-degrading slot at
    the COMMON rate, and those windows are on disk in the same block directory:
    a roll-up that admitted both would be a request-weighted blend of two
    operating points for one slot against one for the other, which is not the
    metric. The common-rate contrast is published as the `common_rate` PAIRED
    verdict instead, where the pairing selects both slots' windows at that rate.

    `operating_rate_by_slot` is the rate each slot's windows were selected AT,
    and `operating_point_basis` says how it was chosen: `declared` when the
    stage records named it (`run_confirm`'s `confirm_rates_by_slot`, `run_soak`'s
    `soak_rates_by_slot`), `knee_bound` for a ledger that predates those keys,
    where the fallback is the old `rate <= that slot's knee` bound.
    """

    available: bool
    operating_rates: list[int] = field(default_factory=list)
    scope: str = ""
    scope_degraded_slots: dict[str, str] = field(default_factory=dict)
    operating_rate_by_slot: dict[str, int] = field(default_factory=dict)
    operating_point_basis: str = ""
    by_slot: dict[str, SlotResource] = field(default_factory=dict)
    ratio_direction: RatioDirection | None = None
    cpu_ratio: float | None = None              # subject : reference, request-weighted
    cpu_ratio_unweighted: float | None = None   # same ratio, equal weight per window
    # The DB column's whole purpose: `cpu_ratio` alone cannot separate "the
    # subject's runtime costs more CPU" from "its ORM's generated SQL costs
    # PostgreSQL more". These two split it.
    db_cpu_ratio: float | None = None
    total_cpu_ratio: float | None = None        # (app + db) per request
    anon_ratio: float | None = None
    peak_ratio: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "operating_rates": self.operating_rates,
            "scope": self.scope,
            "scope_degraded_slots": self.scope_degraded_slots,
            "operating_rate_by_slot": self.operating_rate_by_slot,
            "operating_point_basis": self.operating_point_basis,
            "by_slot": {k: v.to_dict() for k, v in self.by_slot.items()},
            "ratio_direction": (
                self.ratio_direction.to_dict() if self.ratio_direction else None
            ),
            "cpu_ratio": self.cpu_ratio,
            "cpu_ratio_unweighted": self.cpu_ratio_unweighted,
            "db_cpu_ratio": self.db_cpu_ratio,
            "total_cpu_ratio": self.total_cpu_ratio,
            "anon_ratio": self.anon_ratio,
            "peak_ratio": self.peak_ratio,
            "note": self.note,
        }


# The CPU-vs-rate curve's stages. NOT ladder-only: each slot's operating point
# is a confirm/soak rate, so a ladder-only curve omits the rung the headline is
# read at.
_CPU_CURVE_STAGES = ("ladder", "confirm", "soak")

# Every stage that runs warmed probes. The warmup census walks ALL THREE, which
# no other roll-up in this module does: `_STATIONARITY_STAGES` is ladder+soak
# and `_RESOURCE_STAGES` is confirm+soak.
# A per-cell under-warmed fraction taken over any of those three is a fraction of
# a subset, and the abort criterion below is written about the whole cell.
_WARMUP_CENSUS_STAGES = ("ladder", "confirm", "soak")

# PROGRESS.md P8, pre-registered: "under-warmed fraction, either language,
# > 25% -> stop and fix (round 1 was 59.5%)". It lived only in that sentence --
# nothing in the harness computed an under-warmed FRACTION at all, and round 1's
# 59.5% was worked out by hand, after every hour of the run was spent. This is
# the number that criterion reads. It is deliberately NOT in `spec/`: the spec
# carries declared GATES, and this is a stop rule for the operator between cells.
WARMUP_ABORT_FLAGGED_FRACTION = 0.25


@dataclass(frozen=True)
class WarmupCensus:
    """How many of ONE cell's probes for ONE slot were flagged under-warmed.

    The aggregate R1-A2 needed and round 1 did not have. Per-probe flagging was
    everywhere in the artifacts; the fraction was nowhere, so a gate rejecting
    two thirds of the data could not be seen to be doing it until the run was
    over.

    Counted from `ledger.ProbeRecord`, which is the same evidence the per-probe
    `under_warmed` bool is derived from (`gates_failed` plus
    `warmup_attack_failed`), so the census cannot disagree with the exclusions
    the fits actually applied. Only DONE probes are counted: a pending or reset
    probe has no verdict, and putting it in the denominator would dilute the
    fraction with windows that were never scored.
    """

    cell_id: str
    slot: str
    stages: tuple[str, ...]
    n_probes: int
    n_flagged: int
    # None, never 0.0, when no probe was scored: "0% under-warmed" and "nothing
    # to measure" are different findings and the abort rule must not read the
    # second as the first.
    flagged_fraction: float | None
    # {gate name: probes that gate flagged}. Sums to MORE than n_flagged when a
    # probe failed several gates, which is the point -- B2 needs to know which
    # gates are flagging the same windows.
    by_gate: dict[str, int]
    # One attributed cause per flagged probe, so these DO sum to n_flagged.
    by_reason: dict[str, int]
    by_stage: dict[str, dict[str, int]]
    abort_threshold: float = WARMUP_ABORT_FLAGGED_FRACTION

    @property
    def exceeds_abort_threshold(self) -> bool:
        """The P8 stop rule, as a value instead of a sentence in PROGRESS.md."""
        return (
            self.flagged_fraction is not None
            and self.flagged_fraction > self.abort_threshold
        )

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "slot": self.slot,
            "stages": list(self.stages),
            "n_probes": self.n_probes,
            "n_flagged": self.n_flagged,
            "flagged_fraction": self.flagged_fraction,
            "by_gate": self.by_gate,
            "by_reason": self.by_reason,
            "by_stage": self.by_stage,
            "abort_threshold": self.abort_threshold,
            "exceeds_abort_threshold": self.exceeds_abort_threshold,
        }


# The single attributed cause per flagged probe. A crashed warmup attack wins
# over the gates it left un-evaluated: the gates that "failed" after a crash
# failed on a series the crash prevented from being written, so naming one of
# them as the cause would blame the SUT for a harness failure.
REASON_ATTACK_FAILED = "warmup_attack_failed"
REASON_GATES_FAILED = "gates_failed"


def warmup_census_for_cell(cell_rec, slots: Sequence[str]) -> list[WarmupCensus]:
    """One `WarmupCensus` per slot, over every stage that runs warmed probes."""
    out: list[WarmupCensus] = []
    for slot in slots:
        n_probes = n_flagged = 0
        by_gate: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        by_stage: dict[str, dict[str, int]] = {}
        for stage in _WARMUP_CENSUS_STAGES:
            probes = [
                p for p in getattr(cell_rec, stage).probes
                if p.slot == slot and p.state == State.DONE
            ]
            stage_flagged = 0
            for probe in probes:
                flagged = bool(probe.gates_failed) or probe.warmup_attack_failed
                if not flagged:
                    continue
                stage_flagged += 1
                for gate in probe.gates_failed:
                    by_gate[gate] = by_gate.get(gate, 0) + 1
                reason = (
                    REASON_ATTACK_FAILED if probe.warmup_attack_failed
                    else REASON_GATES_FAILED
                )
                by_reason[reason] = by_reason.get(reason, 0) + 1
            n_probes += len(probes)
            n_flagged += stage_flagged
            by_stage[stage] = {"n_probes": len(probes), "n_flagged": stage_flagged}
        out.append(WarmupCensus(
            cell_id=cell_rec.cell_id, slot=slot, stages=_WARMUP_CENSUS_STAGES,
            n_probes=n_probes, n_flagged=n_flagged,
            flagged_fraction=(n_flagged / n_probes) if n_probes else None,
            by_gate=dict(sorted(by_gate.items())),
            by_reason=dict(sorted(by_reason.items())),
            by_stage=by_stage,
        ))
    return out


@dataclass
class CellAnalysis:
    cell_id: str
    knee_by_slot: dict[str, KneeFit] = field(default_factory=dict)
    stationarity: dict = field(default_factory=dict)
    # The headline: the own-knee CPU-ms/request verdict, i.e. cost at each
    # slot's own latency promise. `paired_verdicts` carries every metric and both
    # scopes; this one is what the CLI table prints.
    paired: PairedVerdict | None = None
    paired_verdicts: list[PairedVerdict] = field(default_factory=list)
    slo_at_rates: list[SloAtRate] = field(default_factory=list)
    cpu_vs_rate: CpuVsRate | None = None
    resource: ResourceCost | None = None
    warmup_census: list[WarmupCensus] = field(default_factory=list)
    slo_consistency: list["SloConsistency"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "knee_by_slot": {k: v.to_dict() for k, v in self.knee_by_slot.items()},
            "warmup_census": [c.to_dict() for c in self.warmup_census],
            "stationarity": self.stationarity,
            "paired_primary": self.paired.to_dict() if self.paired else None,
            "paired_verdicts": [p.to_dict() for p in self.paired_verdicts],
            "slo_consistency": [c.to_dict() for c in self.slo_consistency],
            "slo_at_rates": [s.to_dict() for s in self.slo_at_rates],
            "cpu_vs_rate": self.cpu_vs_rate.to_dict() if self.cpu_vs_rate else None,
            "resource": self.resource.to_dict() if self.resource else None,
        }


@dataclass
class AnalyzeResult:
    run_id: str
    slo_ms: float
    cells: list[CellAnalysis]
    fits_path: Path
    report_path: Path

    @property
    def analyzable(self) -> bool:
        return bool(self.cells)


# --------------------------------------------------------------------------- #
# per-cell analysis
# --------------------------------------------------------------------------- #
def _slot_label(slot: str) -> str:
    """The report label for a slot id. RAISES on an unregistered id.

    The round-1 form was `return "Go" if lang == "go" else ".NET"`, so every
    unknown id -- a typo, a new slot, a third stack -- rendered as ".NET" and
    the report read as if it had measured something it had not.
    """
    return registry.get_slot(slot).label


def _agrees(ledger_crossed: bool | None, refit_crossed: bool) -> bool | None:
    """None -- never False -- when the ladder recorded no verdict for this slot.

    "The ladder did not say" and "the ladder disagrees" are different findings,
    and rendering the first as the second puts a DISAGREES banner on every cell
    whose ladder predates the per-slot record.
    """
    return None if ledger_crossed is None else ledger_crossed == refit_crossed


def _knee_for_slot(ladder_rec, slot: str, slo_ms: float, confirm_reps: int) -> KneeFit:
    """Refit the knee from the SAME ladder rungs, restricted to one slot.

    Consults the LADDER'S OWN verdict for this slot (`crossed_by_slot`) and
    records whether the refit reproduces it. Until W24 the per-slot fit never
    looked at the ledger flag at all, and the pooled fit looked at it and then
    overwrote it -- three of the four live "crossed the SLO" definitions. There
    is now one predicate (`analysis.knee.crosses`), which the ladder's stop rule
    calls too, so a disagreement here is a bug and is reported as one instead of
    being invisible.

    Rungs with no measurement-grade probe for this slot are DROPPED (counted in
    `n_rungs_dropped`) rather than back-filled. A slot that never reaches the
    SLO inside the ladder range is reported as "no crossing", with the top
    rung's rate and mean p99 so a reader can see how far the ladder actually
    got; we do not extrapolate past the highest rate the run offered.

    A slot with ZERO warm probes across the whole ladder gets a POSTURE-ONLY
    refit over its flagged probes, marked `warm_only=False`, with the gates it
    failed named in the report's derived posture note.
    """
    ledger_crossed_map = ladder_rec.data.get("crossed_by_slot") or {}
    ledger_crossed = (
        None if slot not in ledger_crossed_map
        else ledger_crossed_map[slot] is not None
    )
    all_rungs = _rungs_by_rate_for_slot(ladder_rec, slot, warm_only=False)
    warm_rungs = _rungs_by_rate_for_slot(ladder_rec, slot, warm_only=True)
    warm_only = bool(warm_rungs)
    rungs = warm_rungs if warm_only else all_rungs
    dropped = len(all_rungs) - len(rungs)
    posture_note = (
        None if warm_only else
        f"POSTURE ONLY: every ladder probe for {slot} was flagged under-warmed, "
        f"so this fit runs over flagged probes and is not a gated measurement."
    )

    if len(rungs) < 2:
        return KneeFit(
            crossed=False, scope=slot, n_rungs=len(rungs), n_rungs_dropped=dropped,
            warm_only=warm_only, ledger_crossed=ledger_crossed,
            crossed_agrees_with_ledger=_agrees(ledger_crossed, False),
            note=posture_note or (
                f"fewer than 2 rungs carry a measurement-grade {slot} probe "
                f"({dropped} rung(s) dropped by under-warmed/gate exclusion); "
                f"cannot fit a per-slot knee."
            ),
        )

    top_rate = max(rungs)
    top_p99 = statistics.fmean(rungs[top_rate])
    knee = estimate_knee_from_rungs(rungs, slo_ms=slo_ms, min_reps=confirm_reps)
    if not knee.crossed:
        return KneeFit(
            crossed=False, scope=slot, n_rungs=len(rungs), n_rungs_dropped=dropped,
            warm_only=warm_only, max_rung_rps=float(top_rate), max_rung_p99_ms=top_p99,
            ledger_crossed=ledger_crossed,
            crossed_agrees_with_ledger=_agrees(ledger_crossed, False),
            note=(posture_note + " " if posture_note else "") +
                 f"no crossing within the measured ladder (max rung {top_rate} rps, "
                 f"p99 {top_p99:.2f} ms); not extrapolated.",
        )
    return KneeFit(
        crossed=True, scope=slot,
        knee_rps=knee.knee_rps,
        ci_low_rps=knee.ci_low_rps, ci_high_rps=knee.ci_high_rps,
        ci_valid=knee.ci_valid, min_reps_in_bracket=knee.min_reps_in_bracket,
        n_boot=knee.n_boot,
        n_crossed=knee.n_crossed, ci_high_censored=knee.ci_high_censored,
        n_rungs=len(rungs), n_rungs_dropped=dropped, warm_only=warm_only,
        max_rung_rps=float(top_rate), max_rung_p99_ms=top_p99,
        ledger_crossed=ledger_crossed,
        crossed_agrees_with_ledger=_agrees(ledger_crossed, True),
        note=posture_note,
        warning=knee.warning,
    )


def _slo_at_rates(cell_rec, slo_ms: float) -> list[SloAtRate]:
    """Each slot's OWN p99 at each confirm/soak rate, against the SLO.

    The runner holds both stacks to the same OFFERED RATE, never to the same
    latency promise, so "the operating rate" can be sub-knee for one slot and
    supra-knee for the other. This is the readout that makes that visible.

    THE VERDICT RULE, stated once here and again in the report: `meets_slo` is
    `mean(per-window p99) <= slo_ms`.

    It is NOT the ladder's crossing rule, and the difference is exact rather than
    stylistic. The ladder brackets on the ISOTONIC fit of the LADDER rungs
    (`analysis.knee.crosses`); this is the arithmetic mean of the CONFIRM/SOAK
    windows, and the inequality points the other way -- at exactly 20.000 ms a
    rung is `crossed` (`>=`) AND the slot `meets_slo` (`<=`). That is correct and
    has to stay expressible: a slot sitting exactly at its knee is meeting its
    promise.

    The rule is lenient for a tail promise; a language can be marked "meets"
    while individual windows miss by a wide margin, so the per-window met count
    and the worst window are carried alongside and printed next to the verdict,
    never instead of it. `_slo_consistency_for_cell` flags the one combination a
    reader must not skim past: `meets_slo=yes` at a rate strictly ABOVE that
    slot's own fitted knee.
    """
    rows: list[SloAtRate] = []
    for stage in _RESOURCE_STAGES:
        rec = getattr(cell_rec, stage)
        if rec.state != State.DONE:
            continue
        dropped = _exclusions_by_rate_slot(rec)
        for (rate, slot), vals in sorted(_p99_by_rate_slot(rec).items()):
            warm, allv = vals["warm"], vals["all"]
            used = warm or allv
            if not used:
                continue
            rows.append(SloAtRate(
                stage=stage, rate=rate, slot=slot,
                n_warm=len(warm), n_total=len(allv),
                n_used=len(used),
                n_windows_met_slo=sum(1 for p in used if p <= slo_ms),
                mean_p99_ms=statistics.fmean(used), max_p99_ms=max(used),
                meets_slo=statistics.fmean(used) <= slo_ms,
                posture_only=not warm,
                dropped_by=dropped.get((rate, slot), {}),
            ))
    return rows


@dataclass(frozen=True)
class SloConsistency:
    """A slot that meets the SLO at a rate ABOVE its own fitted knee.

    The two quantities are allowed to disagree -- `meets_slo` is a mean over
    confirm/soak windows, the knee is an isotonic fit of ladder rungs, and they
    are measured at different rep counts -- but a disagreement is information,
    not noise: either the knee is fitted low (ladder reps are 2-3) or the confirm
    windows were kinder than the ladder rungs at the same rate.

    Round 1 narrated this contradiction in prose inside a resource-section
    footnote. It is a FLAG, in fits.json, with both numbers attached.
    """

    cell_id: str
    slot: str
    stage: str
    rate: int
    knee_rps: float
    mean_p99_ms: float
    slo_ms: float

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id, "slot": self.slot, "stage": self.stage,
            "rate": self.rate, "knee_rps": self.knee_rps,
            "mean_p99_ms": self.mean_p99_ms, "slo_ms": self.slo_ms,
        }


def _slo_consistency_for_cell(
    cell_id: str, rows: Sequence[SloAtRate], knees: dict[str, KneeFit], slo_ms: float
) -> list[SloConsistency]:
    """Every `meets_slo=yes` sitting strictly above that slot's own fitted knee."""
    out: list[SloConsistency] = []
    for row in rows:
        k = knees.get(row.slot)
        if not (row.meets_slo and k and k.crossed and k.knee_rps is not None):
            continue
        if row.rate > k.knee_rps:
            out.append(SloConsistency(
                cell_id=cell_id, slot=row.slot, stage=row.stage, rate=row.rate,
                knee_rps=k.knee_rps, mean_p99_ms=row.mean_p99_ms, slo_ms=slo_ms,
            ))
    return out


def _stationarity_for_cell(cell_rec, slo_ms_cfg) -> dict:
    """Per-probe Mann-Kendall stationarity over each persisted per-second series.

    Walks the stages that record `p99_series` (ladder, soak). A probe with a
    series of >=3 points is gated; the summary records per-probe fails and an
    `any_fail` rollup so a non-stationary probe is visible without re-deriving it.
    """
    min_slope = 1.0
    if slo_ms_cfg and "min_slope_ms_per_min" in slo_ms_cfg:
        min_slope = float(slo_ms_cfg["min_slope_ms_per_min"])
    alpha = float(slo_ms_cfg.get("alpha", 0.05)) if slo_ms_cfg else 0.05

    per_probe: dict[str, bool] = {}
    n_series = 0
    for stage in _STATIONARITY_STAGES:
        series_by_key = getattr(cell_rec, stage).data.get("p99_series", {})
        for key, vals in series_by_key.items():
            if len(vals) < 3:
                continue
            n_series += 1
            per_probe[f"{stage}/{key}"] = stationarity_fails(
                vals, min_slope_ms_per_min=min_slope, alpha=alpha,
            )
    return {
        "n_series": n_series,
        "any_fail": any(per_probe.values()),
        "per_probe_fails": per_probe,
        "min_slope_ms_per_min": min_slope,
        "available": n_series > 0,
    }


# The metrics a paired verdict is taken on, in publication order. p99 is NOT
# among them, and that is the point: at their own knees both stacks sit at ~20 ms
# BY CONSTRUCTION, so a paired p99 verdict is a guaranteed tie -- an artefact of
# the design, not a result. p99 is published per slot as a CONFORMANCE readout
# (`_slo_at_rates`) instead. The metric names match `analysis.paired`'s own
# docstring: "(CPU-ms/req, anon-RSS/req, $/M)".
PAIRED_METRICS = ("cpu_ms_per_req", "anon_mb_per_req", "usd_per_million")

# `own_knee` is the headline the metric is DEFINED as ("cost at a fixed latency
# promise"): each slot at its own SLO-crossing rate. `common_rate` is the
# confound-free contrast at one shared rate, because per-request cost is not
# rate-independent (round 1 measured .NET at CoV 19.3% across its ladder), so an
# own-knee cost comparison confounds language with position on the cost-vs-rate
# curve. Both are legitimate, they answer different questions, and publishing
# only one would be a choice a reader cannot audit -- so every verdict carries
# which it is.
SCOPE_OWN_KNEE = "own_knee"
SCOPE_COMMON_RATE = "common_rate"
# The DEGRADED operating-point scope, and it exists because `own_knee` was
# emitted unconditionally: a slot that reached the rung ceiling without crossing
# has NO SLO-crossing rate, `phases.run_fit` records `knee_by_slot[sid] = None`
# and `_confirm_members` falls back to that slot's top rung -- so under the
# documented `LEDGERBENCH_ALLOW_SUB_KNEE_LADDER` opt-out the roll-up named a
# ladder CEILING as the slot's own crossing rate, in the same report that says
# that slot "has no operating point at the latency promise". The rates are still
# what the stages declared and the windows are still selected at them; what is
# not true is the SLO semantics, so the scope says `declared_operating_point`
# and names, per slot, why it is not the slot's own knee.
SCOPE_DECLARED_POINT = "declared_operating_point"
# Both answer the same question -- cost at the operating point, one rate per
# slot -- and differ only in whether that point is evidenced as an SLO crossing.
# The common-rate contrast is a different question and is never in this set.
_OPERATING_POINT_SCOPES = (SCOPE_OWN_KNEE, SCOPE_DECLARED_POINT)


def _own_knee_degradations(
    rates_by_slot: Mapping[str, Sequence[int]],
    knee_by_slot: Mapping[str, float | None],
) -> dict[str, str]:
    """{slot: why its selection is NOT that slot's own SLO-crossing rate}.

    Empty means every slot has a fitted knee AND was selected at exactly one
    rate AND that rate is the one the knee produced (`int(round(knee_rps))` --
    the rate `run_fit` writes and `_confirm_members` runs, so the comparison has
    to be against the ROUNDED value or a knee of 1454.55 confirmed at 1455 would
    read as a mismatch).

    Shared by the resource roll-up and the paired verdict so the two cannot
    label the same operating point differently.
    """
    out: dict[str, str] = {}
    for sid, rates in rates_by_slot.items():
        knee = knee_by_slot.get(sid)
        rs = sorted(set(rates))
        if knee is None:
            where = (f"windows at {rs[0]:,} rps, its ladder ceiling"
                     if len(rs) == 1 else
                     f"windows at {', '.join(f'{r:,}' for r in rs)} rps")
            out[sid] = (
                f"no SLO crossing in the ladder, so this slot has no fitted knee "
                f"({where}); that rate is a LOWER BOUND on its knee, never an "
                f"estimate of it"
            )
        elif len(rs) != 1:
            out[sid] = (
                f"windows at {len(rs)} rates "
                f"({', '.join(f'{r:,}' for r in rs)} rps), not one operating point"
            )
        elif rs[0] != int(round(knee)):
            out[sid] = (
                f"windows at {rs[0]:,} rps, which is not this slot's fitted knee "
                f"({knee:,.0f} rps)"
            )
    return out


@dataclass(frozen=True)
class PairedBlocks:
    """Per-block, per-slot metric columns for one verdict scope, index-aligned.

    `rates_by_slot` states the operating point each column was measured at --
    which is the whole reason the rate left the pairing KEY: a block now holds
    each slot at its own knee, so "the rate" of a block does not exist.
    """

    scope: str
    rates_by_slot: dict[str, int]
    blocks: list[int]
    by_slot: dict[str, dict[str, list[float]]]   # slot -> metric -> per-block
    n_blocks_solo: int = 0
    n_blocks_incomplete: int = 0


def _solo_blocks(stage_rec) -> set[int]:
    """Blocks that ran ONE probe, read off `slot_order` and not off a key miss.

    A rung above the first crosser's knee is a solo block, and nothing stamps
    `paired: false` on it: `len(slot_order) > 1` IS the statement, it is already
    persisted and validated, and a derived flag cannot disagree with the record.

    Counted separately from blocks a slot merely lost to warm exclusion. The old
    pairing dropped both through the same `all(sid in pair)` miss, so "this block
    was never paired by design" and "this block lost a probe to a gate" reported
    as one number.
    """
    return {p.block for p in stage_rec.probes if len(p.slot_order) <= 1}


def _metric_rows_by_block(rows_by_slot: dict, stage: str) -> dict:
    """{slot: {(block, rate): row}} for one stage, warm windows only."""
    out: dict[str, dict[tuple[int, int], dict]] = {}
    for slot, kinds in rows_by_slot.items():
        for r in kinds["warm"]:
            if r["stage"] == stage:
                out.setdefault(slot, {})[(r["block"], r["rate"])] = r
    return out


def _paired_by_block(
    stage_rec, slots: Sequence[str], rows_by_slot: dict, stage: str,
    rates_by_slot: dict[str, int], scope: str, vcpu_hour_usd: float,
) -> PairedBlocks:
    """Align every slot's per-block cost metrics for ONE verdict scope.

    Keyed on BLOCK ALONE. The rate is not part of the key and cannot be: each
    slot confirms at its own rate, so a (rate, block) key can never hold both
    slots. `rates_by_slot` selects WHICH probe of a block each slot contributes,
    which is what makes one block able to serve both the own-knee and the
    common-rate verdict.
    """
    solo = _solo_blocks(stage_rec)
    per_slot = _metric_rows_by_block(rows_by_slot, stage)
    candidate_blocks = sorted({
        b for slot in slots for (b, _rate) in per_slot.get(slot, {})
    })
    blocks: list[int] = []
    cols: dict[str, dict[str, list[float]]] = {
        sid: {m: [] for m in PAIRED_METRICS} for sid in slots
    }
    n_solo = n_incomplete = 0
    for block in candidate_blocks:
        if block in solo:
            n_solo += 1
            continue
        picked = {
            sid: per_slot.get(sid, {}).get((block, rates_by_slot[sid]))
            for sid in slots
        }
        if any(row is None for row in picked.values()):
            n_incomplete += 1
            continue
        blocks.append(block)
        for sid in slots:
            for metric, value in _block_metrics(picked[sid], vcpu_hour_usd).items():
                cols[sid][metric].append(value)
    return PairedBlocks(
        scope=scope, rates_by_slot=dict(rates_by_slot), blocks=blocks,
        by_slot=cols, n_blocks_solo=n_solo, n_blocks_incomplete=n_incomplete,
    )


def _block_metrics(row: dict, vcpu_hour_usd: float) -> dict[str, float]:
    """The per-block cost metrics a paired verdict is taken on.

    Same aggregation rule as everywhere else in this module for the CPU figure
    (cgroup CPU delta over serviced requests), applied to ONE window rather than
    a roll-up. `anon_mb_per_req` divides the window's anon-p99 level by its
    requests, which is the per-request memory the headline quotes.

    `$/M` is the CPU figure priced by `analysis.report.cost_per_million`'s
    formula, quoted here rather than restated: it is the reader-facing unit of
    the same measurement, not independent evidence, so its verdict is the CPU
    verdict in dollars and the report says so.
    """
    reqs = row["requests"]
    anon = row.get("anon_p99")
    cpu_ms_per_req = row["cpu_ms"] / reqs
    return {
        "cpu_ms_per_req": cpu_ms_per_req,
        # None, never 0: anon_p99 is None for a window with no readable
        # memory.stat tick, and `(anon or 0.0)` here would dilute one slot's
        # paired memory column with fabricated zeros.
        # The pairing loop drops the PAIR for this metric instead.
        "anon_mb_per_req": (anon / 1e6) / reqs if anon is not None else None,
        # analysis.report.cost_per_million: usd_per_million = cpu_ms * price / 3.6
        "usd_per_million": cpu_ms_per_req * vcpu_hour_usd / 3.6,
    }


def _paired_for_cell(
    cell_rec, rows_by_slot: dict, margin_pct: float,
    slot_set: registry.SlotSet, min_blocks: int,
    knee_by_slot: Mapping[str, float | None],
) -> list[PairedVerdict]:
    """Paired COST verdicts between the two slots, per metric and per scope.

    Empty when the confirm stage has no completed paired blocks (e.g. a run that
    stopped before confirm, or a cell where one slot has no warm window). Every
    metric here is lower-is-better, so the verdict reads "subject better" when
    the subject's value is the lower one; `SlotSet.reference` decides which side
    is which.

    The operating-point scope is DERIVED, by the same rule the resource roll-up
    uses: `own_knee` claims an SLO crossing per slot, so a slot whose ladder hit
    the ceiling without crossing (its confirm rate is then the top rung, not a
    knee) makes the verdict `declared_operating_point` instead.
    """
    confirm_rec = cell_rec.confirm
    if confirm_rec.state != State.DONE:
        return []

    from analysis.paired import paired_difference

    slots = slot_set.slots
    own = {
        sid: int(r)
        for sid, r in (confirm_rec.data.get("confirm_rates_by_slot") or {}).items()
        if sid in slots
    }
    if len(own) != len(slots):
        return []
    common = confirm_rec.data.get("common_rate_rps")
    own_degraded = _own_knee_degradations({s: [r] for s, r in own.items()}, knee_by_slot)
    scopes = [(SCOPE_DECLARED_POINT if own_degraded else SCOPE_OWN_KNEE, own)]
    if common is not None and len(set(own.values())) > 1:
        scopes.append((SCOPE_COMMON_RATE, {sid: int(common) for sid in slots}))

    ci_level = 0.95
    price, _origin = _vcpu_hour_usd()
    direction = RatioDirection.of(slot_set)
    out: list[PairedVerdict] = []
    for scope, rates in scopes:
        aligned = _paired_by_block(
            confirm_rec, slots, rows_by_slot, "confirm", rates, scope, price
        )
        n = len(aligned.blocks)
        if n < 2:
            continue
        for metric in PAIRED_METRICS:
            subject_all = aligned.by_slot[direction.subject][metric]
            reference_all = aligned.by_slot[direction.reference][metric]
            # A block where either side's value is None (anon_mb_per_req from a
            # window with no readable memory.stat) cannot be paired for THIS
            # metric: drop the PAIR, never fold to zero. `r.n_blocks` below
            # then reports the pairs actually scored, which is why it can read
            # lower than a sibling metric's over the same confirm stage.
            pairs = [
                (s, ref) for s, ref in zip(subject_all, reference_all)
                if s is not None and ref is not None
            ]
            if len(pairs) < 2:
                continue
            subject = [s for s, _ in pairs]
            reference = [ref for _, ref in pairs]
            if not any(reference):
                continue        # a zero reference mean has no percent margin
            r = paired_difference(
                subject, reference,
                metric=metric, margin_pct=margin_pct,
                subject_id=direction.subject, reference_id=direction.reference,
                lower_is_better=True, ci=ci_level,
            )
            out.append(PairedVerdict(
                metric=r.metric, rates_by_slot=aligned.rates_by_slot,
                verdict_scope=scope, n_blocks=r.n_blocks,
                min_blocks=min_blocks, n_blocks_solo=aligned.n_blocks_solo,
                n_blocks_incomplete=aligned.n_blocks_incomplete,
                mean_by_slot={
                    direction.subject: r.mean_subject,
                    direction.reference: r.mean_reference,
                },
                direction=direction,
                mean_diff_pct=r.mean_diff_pct,
                ci_pct=r.ci_pct,
                ci_low_pct=r.ci_low_pct, ci_high_pct=r.ci_high_pct,
                margin_pct=r.margin_pct, verdict=r.verdict,
                effect_size=r.effect_size,
            ))
    return out


def _failed_gate_names(warmup: dict | None) -> list[str]:
    """Names of the warmup gates a flagged window actually FAILED.

    A declared-but-unimplemented or waived gate is recorded with `passed: false`
    on purpose (`warmup.not_implemented_gate`, so nobody can read it as a pass)
    but with a non-enforcing status, and it never flags a probe. Counting it as
    a failure here would attribute the flagging to a gate that was never
    evaluated, so only the ENFORCING statuses count. Records written before the
    status field existed default to measured.

    `insufficient_data` is one of the enforcing statuses, and it has to be read
    here as well as written: a gate that flagged a probe because it had no
    evidence is exactly the one a reader needs named. Filtering on
    `status == measured` alone would have flagged the probe in the ledger and
    then reported that nothing failed.
    """
    if not warmup:
        return []
    return [
        g.get("name", "?")
        for g in warmup.get("gates", [])
        if not g.get("passed", True) and _record_enforced(g)
    ]


def _record_enforced(gate: dict) -> bool:
    """Did THIS gate record flag its probe? Read off the record, not re-derived.

    `warmup.GateResult.enforced` is a computed field and is written to every
    record, so the reader has the writer's own answer available and must use it:
    since B2/W21 the verdict is `enforce AND status in ENFORCING_STATUSES`, and
    a reader that looks only at `status` disagrees with the ledger for every
    gate the per-gate `enforce` flag switched off -- a record reading
    `insufficient_data`, `passed: false`, `enforced: false`, which the fallback
    path would report as a gate that flagged the window while `ledger.json`
    recorded no failed gate at all. The flag's only producer is a flatness gate
    below its quantile-resolution floor, and `spec/slo.yaml` currently declares
    no gate that can be below one, so on a round-2 run this reads every record
    the same way `status` alone would. It is read off the record regardless: the
    reader must not encode which gates the spec happens to declare.

    Falls back to the status filter for records written before `enforced`
    existed, which is the same compatibility rule the status default carries.
    """
    if "enforced" in gate:
        return bool(gate["enforced"])
    return gate.get("status", STATUS_MEASURED) in ENFORCING_STATUSES


def _non_enforcing_gate_names(warmup: dict | None) -> list[str]:
    """Gates this probe RECORDED but that were not allowed to flag it.

    Not the waivers (`_waived_gate_names`) and not the unimplemented ones: those
    two say a gate was never applied. This one says the gate WAS applied, on
    this probe, and its own record switched enforcement off -- the per-gate
    `enforce` flag B2/W21 added, whose only producer is a flatness gate below
    its quantile-resolution floor (`warmup.FlatnessGateSpec.min_samples_per_second`).

    It exists because a gate that is declared, evaluated and allowed to decide
    nothing is R1-B1's shape recurring one layer up: the fact is in every
    `<slot>.warmup.json` and, without this reader, in no run-level artifact, so
    a reader of the frozen spec would conclude every probe was held to a gate
    that enforced on none of them. `spec/slo.yaml` declares no such gate, so
    this returns an empty list; it is kept so a re-declared one is reported.
    """
    if not warmup:
        return []
    return [
        g.get("name", "?")
        for g in warmup.get("gates", [])
        if "enforced" in g
        and not g["enforced"]
        and g.get("status", STATUS_MEASURED) in ENFORCING_STATUSES
    ]


def _ledger_failed_gates(stage_rec) -> dict[str, list[str]]:
    """{probe_key: the gates that flagged it}, from the LEDGER's probe records.

    B4: `ledger.json` is what this module trusts for warm-versus-flagged, and
    it names the gates behind each verdict (W23). Only probes that recorded a
    `gates_evaluated` list are included, so a probe with an empty entry here
    really did pass every gate it was held to; a probe whose record names no
    evaluated gate at all is absent from this map and its caller reads the
    `<slot>.warmup.json` instead.
    """
    return {
        probe_key(p.rate, p.block, p.slot): list(p.gates_failed)
        for p in stage_rec.probes
        if p.gates_evaluated
    }


def _ledger_attack_failed(stage_rec) -> set[str]:
    """Probe keys whose WARMUP ATTACK died, from the LEDGER's probe records.

    The second cause of `under_warmed` and the one no gate name can express: it
    is why a window can be flagged with an empty failed-gate list. Read here so
    the report accounts for that flag from the ledger rather than describing the
    per-window detail as missing.
    """
    return {
        probe_key(p.rate, p.block, p.slot)
        for p in stage_rec.probes
        if p.warmup_attack_failed
    }


_WAIVER_STATUSES = (STATUS_POSTURE_WAIVED, STATUS_NOT_APPLICABLE)


def _waived_gate_names(warmup: dict | None) -> list[str]:
    """Gates a window's own record says this POSTURE was not held to.

    The counterpart of `_failed_gate_names`: that one reads the gates that
    flagged a window, this one the gates that could not, because the cell or the
    spec declared them not applicable to the posture. Read off the probe record
    rather than off `phases.CELLS`, so the report describes the run that
    happened even when the cell table has moved since.
    """
    if not warmup:
        return []
    return [
        g.get("name", "?")
        for g in warmup.get("gates", [])
        if g.get("status") in _WAIVER_STATUSES
    ]


def _swap_evidence(docs: Sequence[dict], key: str) -> int | None:
    """The largest value of ONE swap key across a window's summaries, or None.

    The two keys are tracked separately on purpose. `swap_current_max` is the
    LEVEL at the two window edges; `swap_peak_max` is the kernel's high-water
    mark over the cgroup's lifetime, and it is the only half that survives a
    container which swapped 4 GiB mid-window and faulted it all back. Folding
    them into one value made a window count as read when EITHER was present, so
    on a kernel with no `memory.swap.peak` (or a tree written before that read
    existed) every window read `swap_current` alone and the report still claimed
    the lifetime high-water mark. Each key therefore carries its own verdict and
    its own unread count, and the report renders only what was read.

    None -- never 0 -- unless EVERY summary in `docs` carried `key`: a partial
    read across the co-sampled cgroups is not evidence about the ones that did
    not read.
    """
    seen = [d.get(key) for d in docs]
    if not seen or any(v is None for v in seen):
        return None
    return max(int(v) for v in seen)


def _window_rows(
    run_id: str, cell_rec, stages: tuple[str, ...], slots: Sequence[str], rate_filter=None
) -> dict[str, dict[str, list[dict]]]:
    # `rate_filter` is a (slot, rate, stage) predicate, NOT a rate predicate. It
    # used to be language-blind and was called with the POOLED knee, so one
    # slot's operating bound selected the other slot's windows. The STAGE is in
    # the signature because the rate a stage put a slot at is a per-stage fact
    # the ledger declares per stage (`confirm_rates_by_slot`, `soak_rates_by_slot`),
    # and a confirm block since W24 holds one slot at TWO rates.
    """Read every per-probe window on disk for a cell -> {slot: {"warm"|"flagged": [rows]}}.

    A row carries what both the resource roll-up and the CPU-vs-rate curve need:
    per-request CPU, memory, throttle/OOM counters, serviced requests, status
    codes, the warmed pool, and the failed warmup gates for a flagged window.
    The ledger's under-warmed/excluded maps decide warm vs flagged, and its
    per-probe records name what is behind that verdict: the failed gates, or
    `warmup_attack_failed` for the flag no gate name can express. A window whose
    ledger record carries no evaluated-gate list falls back to its own
    `<slot>.warmup.json` for the gate names.
    """
    base = paths.run_dir(run_id) / "cells" / cell_rec.cell_id
    out: dict[str, dict[str, list[dict]]] = {
        slot: {"warm": [], "flagged": []} for slot in slots
    }
    for stage in stages:
        stage_rec = getattr(cell_rec, stage)
        stage_data = stage_rec.data
        ledger_gates = _ledger_failed_gates(stage_rec)
        ledger_crashed = _ledger_attack_failed(stage_rec)
        sdir = base / stage
        if not sdir.is_dir():
            continue
        for ratedir in sorted(sdir.glob("rate*")):
            try:
                rate = int(ratedir.name[len("rate"):])
            except ValueError:
                continue
            for blockdir in sorted(ratedir.glob("block*")):
                try:
                    block = int(blockdir.name[len("block"):])
                except ValueError:
                    continue
                for slot in slots:
                    if rate_filter is not None and not rate_filter(slot, rate, stage):
                        continue
                    cm = _read_json(blockdir / f"cpu_mem_{slot}.summary.json")
                    lat = _read_json(blockdir / f"{slot}.summary.json")
                    if not cm or not lat:
                        continue
                    reqs = int(lat.get("requests", 0) or 0)
                    cpu_us = cm.get("cpu_usage_delta_usec")
                    # The DB read is deliberately NOT part of this guard. A run
                    # with no PostgreSQL sampler (non-Linux, or a tree from
                    # before the DB column existed) must still yield every SUT
                    # row: gating on it would delete the whole resource section
                    # rather than one column of it.
                    if reqs <= 0 or cpu_us is None:
                        continue
                    # The PostgreSQL window is per (block, SLOT), not per block:
                    # a block's two probes run sequentially and share this
                    # directory, so a single per-block file would be overwritten
                    # by the second probe and its DB cost published as both
                    # slots'. Two identical DB columns read as a finding.
                    db = _read_json(
                        blockdir / f"cpu_mem_{slot}.{_DB_LABEL}.summary.json"
                    )
                    db_us = (db or {}).get("cpu_usage_delta_usec")
                    # The swap verdict covers the SUT AND the co-sampled
                    # PostgreSQL cgroup, because BOTH publish a per-window
                    # figure a swap episode would distort: the SUT's
                    # memory-per-request headline, and the DB CPU-ms/request
                    # column and `db_cpu_ratio` taken from this same file. redis
                    # and vegeta are deliberately not here -- no per-window
                    # number in the report is read off them -- and they are
                    # covered run-wide instead by
                    # `manifest.topology.containers[*].swap_*_max_bytes`, which
                    # `_container_ceiling_sentence` renders for every sampled
                    # cgroup.
                    swap_docs = [cm] if db is None else [cm, db]
                    wu = _read_json(blockdir / f"{slot}.warmup.json")
                    key = probe_key(rate, block, slot)
                    flagged = _is_flagged(
                        stage_data, key,
                        default=bool(wu.get("under_warmed")) if wu else False,
                    )
                    codes = {str(k): int(v) for k, v in (lat.get("status_codes") or {}).items()}
                    row = {
                        "stage": stage,
                        "rate": rate,
                        "block": block,
                        "cpu_ms": cpu_us / 1000.0,
                        "cpu_ms_per_req": (cpu_us / 1000.0) / reqs,
                        # PostgreSQL's cgroup CPU over the SAME window. None,
                        # never 0.0: an absent sampler is not a database that
                        # burned no CPU.
                        "db_cpu_ms": None if db_us is None else db_us / 1000.0,
                        "anon_p99": cm.get("mem_anon_p99"),
                        "mem_peak": cm.get("mem_peak_end"),
                        "requests": reqs,
                        "status_codes": codes,
                        # Window DELTAS. `nr_throttled` and `oom_kill` are
                        # lifetime counters, so the pre-W16 end-of-window reads
                        # made both verdicts latch for any cgroup that outlives
                        # a probe. None (not 0) when the counter was unreadable.
                        "throttled": cm.get("nr_throttled_delta"),
                        "oom": cm.get("oom_kill_delta"),
                        # The two halves of the swap evidence, SEPARATELY:
                        # `swap_current` is a LEVEL at the window edges,
                        # `swap_peak` the kernel's lifetime high-water mark, and
                        # a kernel or a tree can carry one without the other.
                        # Each is None (not 0) unless every cgroup in
                        # `swap_docs` carried it.
                        "swap_current": _swap_evidence(swap_docs, "swap_current_max"),
                        "swap_peak": _swap_evidence(swap_docs, "swap_peak_max"),
                        # Whether `swap_docs` actually held the PostgreSQL
                        # co-sample, so the rendered SCOPE of the swap verdict is
                        # read off what was on disk. A run with no PG sampler is
                        # a supported path here (see the guard above), and its
                        # verdict covers the SUT alone; a sentence that named
                        # both cgroups regardless would be the broader claim.
                        "swap_scope_db": db is not None,
                        "db_pool_total": (wu or {}).get("final_db_pool_total"),
                        "db_pool_max": (wu or {}).get("final_db_pool_max"),
                        "failed_gates": ledger_gates.get(
                            key, _failed_gate_names(wu)
                        ),
                        # The non-gate half of the same verdict, so a flagged
                        # window with no failed gate can still name its cause.
                        "warmup_attack_failed": key in ledger_crashed,
                        "waived_gates": _waived_gate_names(wu),
                        # Applied, recorded, and not allowed to flag: see
                        # `_non_enforcing_gate_names`.
                        "non_enforcing_gates": _non_enforcing_gate_names(wu),
                    }
                    out[slot]["flagged" if flagged else "warm"].append(row)
    return out


def _cpu_ms_per_req(rows: list[dict], *, key: str = "cpu_ms") -> float | None:
    """THE aggregation rule: total cgroup CPU-ms / total serviced requests.

    Request-weighted, so the request count published beside the figure really is
    its denominator. Used for every CPU-ms/request in this module -- the rung
    values, the roll-up, and the PostgreSQL column -- so the report has ONE rule
    to state rather than a per-column footnote.

    `key` selects which cgroup's CPU is being divided, and the numerator and the
    denominator are taken over the SAME subset of rows. That is what stops a DB
    figure being wrong by exactly the fraction of windows with no DB sample:
    mixing a full request denominator with a partial numerator understates the
    cost and looks entirely plausible. Returns None when no row carries the key.
    """
    usable = [r for r in rows if r.get(key) is not None]
    reqs = sum(r["requests"] for r in usable)
    return sum(r[key] for r in usable) / reqs if reqs else None


def _cpu_vs_rate_for_cell(run_id: str, cell_rec, slot_set: registry.SlotSet) -> CpuVsRate:
    """CPU-ms/request per offered rate per slot + the ratio and CVs.

    Measurement-grade windows only; where a rung has NO warm window for a slot,
    the flagged windows at that rung are used instead and the value is marked
    posture-only (rather than leaving a hole that hides the rung). The CVs are
    taken across the published rung values so the rate dependence is a number,
    not an impression.

    Values use the module's one aggregation rule (`_cpu_ms_per_req`): total
    cgroup CPU delta over the rung's windows divided by the requests they
    serviced.
    """
    slots = slot_set.slots
    direction = RatioDirection.of(slot_set)
    # LADDER + CONFIRM + SOAK. Ladder-only left the OPERATING POINTS -- the one
    # place on this curve the headline is actually read at -- off the curve
    # entirely, which is the one rate a reader most needs to see it at.
    rows = _window_rows(run_id, cell_rec, _CPU_CURVE_STAGES, slots)
    if not any(rows[sid][kind] for sid in slots for kind in ("warm", "flagged")):
        return CpuVsRate(
            available=False,
            ratio_direction=direction,
            note="no per-window cgroup summaries under "
                 f"cells/<cell>/{{{','.join(_CPU_CURVE_STAGES)}}} for either slot "
                 "(Windows/smoke run, or a run whose cells tree was not retained).",
        )

    by_rate: dict[int, dict[str, dict[str, list[dict]]]] = {}
    for sid in slots:
        for kind in ("warm", "flagged"):
            for r in rows[sid][kind]:
                bucket = by_rate.setdefault(r["rate"], {})
                bucket.setdefault(sid, {"warm": [], "flagged": []})[kind].append(r)

    rungs: list[CpuRateRung] = []
    posture_values = 0
    for rate in sorted(by_rate):
        vals: dict[str, CpuRungValue] = {}
        for sid in slots:
            bucket = by_rate[rate].get(sid, {"warm": [], "flagged": []})
            warm, flagged = bucket["warm"], bucket["flagged"]
            used = warm or flagged
            posture = not warm and bool(flagged)
            posture_values += 1 if posture else 0
            vals[sid] = CpuRungValue(
                cpu_ms_per_req=_cpu_ms_per_req(used) if used else None,
                db_cpu_ms_per_req=_cpu_ms_per_req(used, key="db_cpu_ms") if used else None,
                n_warm=len(warm), n_total=len(warm) + len(flagged),
                n_db=sum(1 for r in used if r["db_cpu_ms"] is not None),
                requests=sum(r["requests"] for r in used if r["cpu_ms"] is not None),
                db_requests=sum(
                    r["requests"] for r in used if r["db_cpu_ms"] is not None
                ),
                posture_only=posture,
            )
        rungs.append(CpuRateRung(
            rate=rate,
            by_slot=vals,
            ratio=direction.divide({k: v.cpu_ms_per_req for k, v in vals.items()}),
        ))

    ratios = [r.ratio for r in rungs if r.ratio is not None]
    return CpuVsRate(
        available=True,
        rungs=rungs,
        # POSTURE-ONLY rungs are excluded from the CV. A rung whose only windows
        # were flagged under-warmed is not evidence about the rate dependence of
        # a WARM stack, and letting it into the dispersion published as "is the
        # cost rate-independent?" makes a warmup artefact read as rate
        # sensitivity. The values themselves stay in the table, marked.
        cv_pct_by_slot={
            sid: _cv_pct([
                r.by_slot[sid].cpu_ms_per_req for r in rungs
                if not r.by_slot[sid].posture_only
            ])
            for sid in slots
        },
        ratio_min=min(ratios) if ratios else None,
        ratio_max=max(ratios) if ratios else None,
        ratio_direction=direction,
        n_posture_only_values=posture_values,
        note="per rung: total cgroup CPU delta / total serviced requests over "
             "that rung's probe windows "
             "(cells/<cell>/ladder/rate*/block*/cpu_mem_<slot>.summary.json).",
    )


def _roll_windows(rows: list[dict], flagged_rows: list[dict]) -> SlotResource:
    """Aggregate one slot's measurement-grade windows into a SlotResource.

    CPU-ms/request is request-weighted (`_cpu_ms_per_req`); the equal-weight
    alternative and the confirm/soak window composition are recorded alongside
    so the report can quantify what the weighting choice is worth instead of
    leaving an unlabelled mean of ratios in the headline.
    """
    n = len(rows)
    stage_windows: dict[str, int] = {}
    for r in rows:
        stage_windows[r["stage"]] = stage_windows.get(r["stage"], 0) + 1
    anon = [r["anon_p99"] for r in rows if r["anon_p99"] is not None]
    peak = [r["mem_peak"] for r in rows if r["mem_peak"] is not None]
    requests = sum(r["requests"] for r in rows)
    codes: dict[str, int] = {}
    for r in rows:
        for code, cnt in r["status_codes"].items():
            codes[code] = codes.get(code, 0) + cnt
    non_2xx = sum(c for code, c in codes.items() if not code.startswith("2"))
    created = codes.get("201", 0)
    gate_counts: dict[str, int] = {}
    for r in flagged_rows:
        for name in r["failed_gates"]:
            gate_counts[name] = gate_counts.get(name, 0) + 1
    waived_counts: dict[str, int] = {}
    for r in rows:
        for name in r.get("waived_gates", ()):
            waived_counts[name] = waived_counts.get(name, 0) + 1
    non_enforcing_counts: dict[str, int] = {}
    for r in rows:
        for name in r.get("non_enforcing_gates", ()):
            non_enforcing_counts[name] = non_enforcing_counts.get(name, 0) + 1
    pools = [r["db_pool_total"] for r in rows if r["db_pool_total"] is not None]
    pool_max = [r["db_pool_max"] for r in rows if r["db_pool_max"] is not None]
    db_rows = [r for r in rows if r["db_cpu_ms"] is not None]
    # A throttle/OOM/swap verdict is taken over the windows that HAVE the
    # counter, and `bool(...)` in front of each `all()` is what makes an empty
    # set FAIL rather than pass: `all([])` is True, so a run with no readable
    # counters would otherwise publish "clean" as though it had checked.
    throttle_rows = [r for r in rows if r["throttled"] is not None]
    oom_rows = [r for r in rows if r["oom"] is not None]
    swap_cur_rows = [r for r in rows if r["swap_current"] is not None]
    swap_peak_rows = [r for r in rows if r["swap_peak"] is not None]
    return SlotResource(
        n_windows=n,
        n_flagged_excluded=len(flagged_rows),
        posture_only=False,
        flagged_gate_counts=gate_counts,
        waived_gate_counts=waived_counts,
        non_enforcing_gate_counts=non_enforcing_counts,
        n_warmup_attack_failed=sum(
            1 for r in flagged_rows if r.get("warmup_attack_failed")
        ),
        stage_windows=stage_windows,
        requests=requests,
        non_2xx=non_2xx,
        create_share_pct=(100.0 * created / requests) if requests else None,
        cpu_ms_total=sum(r["cpu_ms"] for r in rows),
        cpu_ms_per_req=_cpu_ms_per_req(rows) or float("nan"),
        cpu_ms_per_req_unweighted=statistics.fmean(r["cpu_ms_per_req"] for r in rows),
        db_cpu_ms_total=sum(r["db_cpu_ms"] for r in db_rows) if db_rows else None,
        db_cpu_ms_per_req=_cpu_ms_per_req(rows, key="db_cpu_ms"),
        db_n_windows=len(db_rows),
        anon_p99_mb=(sum(anon) / len(anon) / 1e6) if anon else float("nan"),
        mem_peak_mb=(sum(peak) / len(peak) / 1e6) if peak else float("nan"),
        throttle_clean=bool(throttle_rows) and all(r["throttled"] == 0 for r in throttle_rows),
        oom_clean=bool(oom_rows) and all(r["oom"] == 0 for r in oom_rows),
        swap_current_clean=(bool(swap_cur_rows)
                            and all(r["swap_current"] == 0 for r in swap_cur_rows)),
        swap_peak_clean=(bool(swap_peak_rows)
                         and all(r["swap_peak"] == 0 for r in swap_peak_rows)),
        n_windows_without_counters=len(rows) - len(throttle_rows),
        n_windows_without_oom_counter=len(rows) - len(oom_rows),
        n_windows_without_swap_current=len(rows) - len(swap_cur_rows),
        n_windows_without_swap_peak=len(rows) - len(swap_peak_rows),
        n_swap_current_windows_with_db=sum(
            1 for r in swap_cur_rows if r["swap_scope_db"]
        ),
        n_swap_peak_windows_with_db=sum(
            1 for r in swap_peak_rows if r["swap_scope_db"]
        ),
        db_pool_total=max(pools) if pools else None,
        db_pool_max=max(pool_max) if pool_max else None,
    )


# Which stage record names the rate each slot ran at, per resource stage. The
# two keys are written by `phases.run_confirm` / `phases.run_soak` at the moment
# the probes are PLANNED, so they state the operating point the run intended
# rather than the set of rates that happen to have windows on disk -- and since
# W24 those two are not the same thing for the higher-knee slot.
_OPERATING_RATE_KEY = {
    "confirm": "confirm_rates_by_slot",
    "soak": "soak_rates_by_slot",
}


def _declared_operating_rates(cell_rec, slots: Sequence[str]) -> dict[str, dict[str, int]]:
    """{stage: {slot: the own-knee rate that stage declared}} for the resource stages.

    Missing stages and missing slots are simply absent: the caller falls back
    for exactly those and records that it did.
    """
    out: dict[str, dict[str, int]] = {}
    for stage in _RESOURCE_STAGES:
        rec = getattr(cell_rec, stage, None)
        rates = (getattr(rec, "data", None) or {}).get(_OPERATING_RATE_KEY[stage]) or {}
        picked = {sid: int(r) for sid, r in rates.items() if sid in slots}
        if picked:
            out[stage] = picked
    return out


def _resource_cost_for_cell(
    run_id: str, cell_rec, knee_by_slot: dict[str, float | None],
    slot_set: registry.SlotSet,
) -> ResourceCost:
    """Steady-state SUT cgroup CPU + anon memory per request, per slot.

    Walks the persisted per-probe-window cgroup summaries the live runner wrote
    under `cells/<cell>/{confirm,soak}/rate<R>/block<B>/cpu_mem_<slot>.summary.json`
    (CPU usec delta + anon-p99 + memory.peak + throttle/oom) alongside the latency
    summary (`<slot>.summary.json`, for the serviced-request count and status
    codes) and the warmup record (`<slot>.warmup.json`, for the warmed pool size
    and the gates a flagged window failed).

    UNDER-WARMED / GATE-EXCLUDED WINDOWS ARE EXCLUDED, exactly as the knee fit
    input excludes them, and both counts (used vs excluded) travel with the
    numbers. A slot whose windows are ALL flagged is still reported (dropping it
    would hide a whole cell's posture), but it is marked `posture_only=True`,
    and `_posture_notes` prints the gates it failed plus whatever the cell's OWN
    declared knobs explain about them.

    THE OPERATING POINT IS SELECTED, NOT BOUNDED, AND IT IS PER SLOT AND PER
    STAGE. Each slot's windows are the ones measured AT the rate that stage put
    it at -- the rate the headline metric is defined at, `run_fit`'s fitted knee
    for that slot -- read off the stage records the runner wrote
    (`confirm_rates_by_slot`, `soak_rates_by_slot`) rather than inferred from
    the rates present on disk.

    An INEQUALITY cannot do this job, and the version that tried is what W25
    found. Since W24 a confirm block also holds every slot whose knee is above
    the common rate at that common rate (`phases._confirm_members`), and the
    common rate is SUB-KNEE for exactly those slots by construction -- so
    `rate <= that slot's knee` admitted both operating points for one slot and
    one for the other, and `cpu_ratio` / `anon_ratio` / `total_cpu_ratio` and
    the $/M table became a request-weighted blend of the two. The bias is
    one-directional, because only the higher-knee slot ever gets the extra,
    cheaper-per-request-than-its-own-knee windows.

    The common-rate windows are not lost: they are the `common_rate` PAIRED
    verdict, which selects both slots at that rate and labels itself.

    Falls back to the old `rate <= knee + 1.0` bound for a stage that declares
    no rates (a ledger written before those keys existed, where a slot has one
    confirm rate anyway); `operating_point_basis` says which happened. With no
    fitted knee and no declared rate for a slot, all its confirm/soak rates are
    used -- and that slot then lands in `scope_degraded_slots`, because `scope`
    is derived from the surviving rows and their knees exactly as the basis is:
    the basis alone was NOT enough, since it says only how the rate was chosen
    and `scope` claims what the rate MEANS.
    Returns `available=False` when no cgroup summaries exist on disk
    (Windows/smoke run, or a non-retained cells tree).
    """
    slots = slot_set.slots
    direction = RatioDirection.of(slot_set)
    declared = _declared_operating_rates(cell_rec, slots)

    def _keep(slot: str, rate: int, stage: str) -> bool:
        own = declared.get(stage, {}).get(slot)
        if own is not None:
            return rate == own
        knee = knee_by_slot.get(slot)
        return knee is None or rate <= knee + 1.0

    rows = _window_rows(
        run_id, cell_rec, _RESOURCE_STAGES, slots, rate_filter=_keep,
    )
    have_any = any(rows[sid][kind] for sid in slots for kind in ("warm", "flagged"))
    if not have_any:
        return ResourceCost(
            available=False,
            ratio_direction=direction,
            note="no cgroup-v2 SUT resource summaries on disk for this cell "
                 "(Windows/smoke run, or a run whose per-window cells tree was "
                 "not retained); per-request CPU/memory cannot be reported.",
        )

    by_slot: dict[str, SlotResource] = {}
    rates: set[int] = set()
    rate_by_slot: dict[str, list[int]] = {}
    for sid in slots:
        warm, flagged = rows[sid]["warm"], rows[sid]["flagged"]
        used = warm or flagged
        if not used:
            return ResourceCost(
                available=False,
                ratio_direction=direction,
                note=f"no usable {_slot_label(sid)} resource windows for this cell; "
                     f"a {direction.label} ratio needs both slots.",
            )
        by_slot[sid] = _roll_windows(used, flagged)
        if not warm:
            by_slot[sid].posture_only = True
            by_slot[sid].n_flagged_excluded = 0  # nothing was excluded; all were used
        rates.update(r["rate"] for r in used)
        rate_by_slot[sid] = sorted({r["rate"] for r in used})

    # The basis is read off the rows that SURVIVED, not off the declaration: a
    # stage that declared a rate but has no window on disk must not let the
    # roll-up claim a selection it never made.
    all_declared = all(
        declared.get(r["stage"], {}).get(sid) is not None
        for sid in slots
        for kind in ("warm", "flagged")
        for r in rows[sid][kind]
    )

    # The scope is derived from the SURVIVING rows too, for the same reason the
    # basis is: `own_knee` asserts an SLO crossing per slot, and a slot with no
    # fitted knee (or selected anywhere but at it) has not evidenced one.
    degraded = _own_knee_degradations(rate_by_slot, knee_by_slot)

    return ResourceCost(
        available=True,
        operating_rates=sorted(rates),
        scope=SCOPE_DECLARED_POINT if degraded else SCOPE_OWN_KNEE,
        scope_degraded_slots=degraded,
        operating_rate_by_slot={
            sid: rs[0] for sid, rs in rate_by_slot.items() if len(rs) == 1
        },
        operating_point_basis="declared" if all_declared else "knee_bound",
        by_slot=by_slot,
        ratio_direction=direction,
        cpu_ratio=direction.divide({k: v.cpu_ms_per_req for k, v in by_slot.items()}),
        cpu_ratio_unweighted=direction.divide(
            {k: v.cpu_ms_per_req_unweighted for k, v in by_slot.items()}
        ),
        db_cpu_ratio=direction.divide(
            {k: v.db_cpu_ms_per_req for k, v in by_slot.items()}
        ),
        total_cpu_ratio=direction.divide(
            {k: v.total_cpu_ms_per_req for k, v in by_slot.items()}
        ),
        anon_ratio=direction.divide({k: v.anon_p99_mb for k, v in by_slot.items()}),
        peak_ratio=direction.divide({k: v.mem_peak_mb for k, v in by_slot.items()}),
    )


def _analyze_cell(
    cell_rec, slo, run_id: str, slot_set: registry.SlotSet
) -> CellAnalysis | None:
    """One cell: knees + stationarity + paired + SLO readout + resource curves."""
    ladder_rec = cell_rec.ladder
    if ladder_rec.state != State.DONE:
        return None
    slo_ms = float(slo.slo.threshold_ms)
    slots = slot_set.slots
    by_slot = {
        sid: _knee_for_slot(ladder_rec, sid, slo_ms, slo.ladder.confirm_reps)
        for sid in slots
    }
    knee_rps_by_slot = {sid: k.knee_rps if k.crossed else None
                        for sid, k in by_slot.items()}
    stat = _stationarity_for_cell(cell_rec, slo.stationarity)
    # The paired verdict reads the SAME window rows the resource roll-up reads,
    # unfiltered: the operating-point filter is a roll-up rule, and a paired
    # verdict selects its windows by the block it is pairing on instead.
    paired_rows = _window_rows(run_id, cell_rec, _RESOURCE_STAGES, slots)
    paired = _paired_for_cell(
        cell_rec, paired_rows, slo.equivalence.margin_pct, slot_set,
        slo.ladder.confirm_reps, knee_rps_by_slot,
    )
    resource = _resource_cost_for_cell(run_id, cell_rec, knee_rps_by_slot, slot_set)
    slo_rows = _slo_at_rates(cell_rec, slo_ms)
    # The headline is the OPERATING-POINT verdict, whichever label it earned:
    # selecting on `own_knee` alone would drop the cell's headline row entirely
    # on a degraded run rather than publish it under its honest scope.
    headline = next(
        (v for v in paired
         if v.verdict_scope in _OPERATING_POINT_SCOPES
         and v.metric == "cpu_ms_per_req"),
        None,
    )
    return CellAnalysis(
        cell_id=cell_rec.cell_id,
        knee_by_slot=by_slot, stationarity=stat,
        paired=headline,
        paired_verdicts=paired,
        slo_at_rates=slo_rows,
        slo_consistency=_slo_consistency_for_cell(
            cell_rec.cell_id, slo_rows, by_slot, slo_ms
        ),
        cpu_vs_rate=_cpu_vs_rate_for_cell(run_id, cell_rec, slot_set),
        resource=resource,
        warmup_census=warmup_census_for_cell(cell_rec, slots),
    )


# --------------------------------------------------------------------------- #
# report assembly
# --------------------------------------------------------------------------- #
def _md(rows: list[dict]) -> str:
    from analysis.report import _df_to_md
    import pandas as pd
    return _df_to_md(pd.DataFrame(rows))


def _knee_row(cell_id: str, k: KneeFit, slo_ms: float) -> dict:
    if not k.crossed:
        knee_s, ci_s, valid_s = "no crossing", "n/a", "n/a"
    elif k.knee_rps is None:
        knee_s, ci_s, valid_s = "unfit", "n/a", "n/a"
    else:
        knee_s = f"{k.knee_rps:.0f}"
        # A censored upper percentile reads ">= top rung": the resamples that
        # never crossed in-range sit above it, and printing the top rung as a
        # resolved quantile would understate the interval exactly where the
        # ladder stops (crossing at/near the ceiling).
        hi_s = (
            f">={k.ci_high_rps:.0f}" if k.ci_high_censored else f"{k.ci_high_rps:.0f}"
        )
        ci_s = f"[{k.ci_low_rps:.0f}, {hi_s}]"
        valid_s = "yes" if k.ci_valid else "NOT a CI (see note)"
    scope = _slot_label(k.scope)
    if k.crossed_agrees_with_ledger is False:
        # Cannot happen while both sides route through `analysis.knee.crosses`.
        # Printed, not asserted, so a disagreement between the runner and the
        # analyzer is visible in the report.
        scope += " **DISAGREES WITH THE LADDER**"
    if not k.warm_only:
        scope += " *posture*"
    # Keep only the first sentence of the knee module's warning and let the
    # paragraph under the table state what the interval is.
    warning = k.warning.split(". ")[0] if k.warning else ""
    return {
        "cell": cell_id,
        "scope": scope,
        f"knee rps @ {slo_ms:g}ms": knee_s,
        "bootstrap interval": ci_s,
        "inferential?": valid_s,
        "rungs used": f"{k.n_rungs}" + (f" (-{k.n_rungs_dropped})" if k.n_rungs_dropped else ""),
        "note": k.note or warning,
    }


def _knee_table(cells: list[CellAnalysis], slo_ms: float) -> str:
    rows = []
    for c in cells:
        for k in c.knee_by_slot.values():
            rows.append(_knee_row(c.cell_id, k, slo_ms))
    return _md(rows)


def _ladder_reps_per_rung(led: Ledger) -> int | None:
    """Reps per ladder rung for THIS run's pinned scale, or None.

    Re-resolved from `led.scale` rather than hard-coded: `full` runs the spec's
    3, and both `rehearsal` and `smoke` run 2, so a fixed "3" in the CI note is
    a claim about a ladder two of the three tiers did not run. Unlike
    `body_pool_requests`, this dial reads nothing from the ANALYZE-time
    environment, so re-resolving it returns the value the run used.
    """
    try:
        return phases.ScaleProfile.resolve(led.scale or "full", load_slo()).ladder_reps_per_rung
    except Exception:  # noqa: BLE001 - unknown scale, or spec unreadable in an isolated tree
        return None


def _knee_ci_note(cells: list[CellAnalysis], led: Ledger) -> str:
    """One honest sentence about what the printed interval actually is.

    `ci_valid` is False for every ladder-stage fit for a structural reason, not
    a bug: `analysis.knee.estimate_knee` marks the interval non-inferential when
    either rung framing the crossing carries fewer reps than
    `ladder.confirm_reps`. No tier's ladder runs that many reps per rung, and
    under-warmed exclusion thins the crossing bracket further, so the guard
    cannot clear at the ladder stage. Fixing it would mean re-running the
    crossing bracket at confirm-stage rep counts; a measurement change, not an
    analysis change, so we state what the interval is instead of relabelling it.

    Both quantified claims are derived: the resample count off the fits' own
    `n_boot`, the rep count off the run's pinned scale.
    """
    brackets = []
    boots: set[int] = set()
    for c in cells:
        for k in c.knee_by_slot.values():
            if k.knee_rps is not None and k.min_reps_in_bracket is not None:
                brackets.append(k.min_reps_in_bracket)
            if k.n_boot is not None:
                boots.add(k.n_boot)
    worst = min(brackets) if brackets else 0
    # No count rather than a guessed one: `n_boot` rides on every fit the
    # estimator produced, so an empty set means no fit was run at all.
    n_boot = (
        "/".join(str(b) for b in sorted(boots)) + " isotonic refits"
        if boots else "isotonic refits"
    )
    reps = _ladder_reps_per_rung(led)
    reps_clause = (
        f"the `{led.scale or 'full'}` ladder runs {reps} rep(s) per rung, and "
        if reps is not None else ""
    )
    try:
        confirm_reps: int | str = load_slo().ladder.confirm_reps
    except Exception:  # noqa: BLE001 - spec unreadable (isolated test tree)
        confirm_reps = "the spec minimum"
    # Resamples whose refit never crossed in-range are right-censored at the
    # top rung, not dropped; the sentence appears only when a fit actually had
    # censored mass, so the caveat's scope is derived from the data it caveats.
    n_censored = sum(
        (k.n_boot - k.n_crossed)
        for c in cells for k in c.knee_by_slot.values()
        if k.n_boot is not None and k.n_crossed is not None
    )
    censor_clause = (
        " Resamples whose refit never crossed inside the ladder are counted as "
        "beyond the top rung (right-censored), and an upper bound printed as "
        "`>=` landed in that censored mass rather than resolving."
        if n_censored else ""
    )
    return (
        f"The bracketed interval is the 2.5/97.5 percentile of {n_boot} "
        f"that resample the ladder's per-rung reps with replacement. It is NOT a "
        f"valid bootstrap CI here: the SLO-crossing bracket carries as few as "
        f"{worst} measurement-grade rep(s), against the pre-registered minimum of "
        f"`ladder.confirm_reps` = {confirm_reps} ({reps_clause}"
        f"under-warmed exclusion thins the bracket further). Read it as a spread "
        f"indicator over a handful of reps. An inferential CI needs the crossing "
        f"bracket re-run at confirm-stage rep counts, which this run did not "
        f"do.{censor_clause}\n"
    )


def _slo_table(cells: list[CellAnalysis], slo_ms: float) -> str:
    """Per-slot p99 vs the SLO, with the verdict RULE named in the header.

    The verdict column is `mean(per-window p99) <= slo`, so the header says so
    and the neighbouring column prints how many windows individually met the
    threshold: the honest tail summary the mean can hide.

    The window column counts USED / TOTAL, never warm / total. A window is used
    when it passed the warm-up gates AND the CO-safety gates, so a bare
    "warm/total" label would claim a warm-up verdict over windows that were
    really dropped for error rate or rate fidelity. The neighbouring
    "why dropped" column names the actual reasons, one per dropped window.
    """
    rows = []
    for c in cells:
        for s in c.slo_at_rates:
            rows.append({
                "cell": c.cell_id,
                "stage": s.stage,
                "offered rps": s.rate,
                "slot": _slot_label(s.slot),
                "mean p99 (ms)": f"{s.mean_p99_ms:.2f}",
                "worst window p99 (ms)": f"{s.max_p99_ms:.2f}",
                f"meets {slo_ms:g} ms? (mean rule)": "yes" if s.meets_slo else "NO",
                f"windows under {slo_ms:g} ms": f"{s.n_windows_met_slo}/{s.n_used}",
                "windows (used/total)": (
                    f"{0 if s.posture_only else s.n_used}/{s.n_total}"
                    + (" *posture*" if s.posture_only else "")
                ),
                "why dropped": _exclusion_summary(s.dropped_by),
            })
    return _md(rows)


def _slo_sentences(cells: list[CellAnalysis], slo_ms: float) -> list[str]:
    """Per-cell plain-language readout at the primary operating rate.

    Every "meets/MISSES" here is the mean-of-window-p99 rule, and it is
    qualified in place with the per-window met count and the worst window, so
    the sentence cannot be quoted as an unconditional tail guarantee.
    """
    out: list[str] = []
    for c in cells:
        rates = c.resource.operating_rates if (c.resource and c.resource.available) else []
        if not rates:
            continue
        rate = rates[0]
        bits = []
        for s in c.slo_at_rates:
            if s.stage != "confirm" or s.rate != rate:
                continue
            bits.append(
                f"{_slot_label(s.slot)} mean p99 {s.mean_p99_ms:.1f} ms "
                f"({'meets' if s.meets_slo else 'MISSES'} the {slo_ms:g} ms promise "
                f"on the mean rule; {s.n_windows_met_slo}/{s.n_used} windows "
                f"individually under it, worst {s.max_p99_ms:.1f} ms"
                + (", posture-only windows" if s.posture_only else "")
                + ")"
            )
        if bits:
            out.append(f"- **{c.cell_id}** at {rate} rps offered: " + "; ".join(bits) + ".")
    return out


def _cpu_vs_rate_table(c: CellAnalysis) -> str:
    cvr = c.cpu_vs_rate
    direction = cvr.ratio_direction
    rows = []
    for r in cvr.rungs:
        row: dict = {"offered rps": r.rate}
        for sid, v in r.by_slot.items():
            label = _slot_label(sid)
            row[f"{label} CPU-ms/req"] = (
                _fmt(v.cpu_ms_per_req, ".4f") + ("*" if v.posture_only else "")
            )
            row[f"{label} windows (used/total)"] = f"{v.n_warm}/{v.n_total}"
        if direction is not None:
            row[direction.label] = _fmt(r.ratio, ".2f") + ("x" if r.ratio else "")
        rows.append(row)
    return _md(rows)


def _paired_table(cells: list[CellAnalysis]) -> str:
    """Render the per-rate paired verdicts as markdown.

    The header states the CONFIDENCE LEVEL of the interval (95%); the
    pre-registered +-equivalence margin is its own column, because they are two
    different numbers and conflating them (the old `f"{margin_pct}% CI"` header)
    made a 95% interval read as a "5% CI".
    """
    from analysis.report import verdict_word

    rows = []
    for c in cells:
        for p in c.paired_verdicts:
            labels = {sid: _slot_label(sid) for sid in p.mean_by_slot}
            row: dict = {
                "cell": c.cell_id,
                "scope": p.verdict_scope,
                "metric": p.metric,
                "blocks (n)": (
                    f"{p.n_blocks} **BELOW the pre-registered {p.min_blocks}**"
                    if p.under_min_blocks else str(p.n_blocks)
                ),
            }
            for sid in p.rates_by_slot:
                row[f"{_slot_label(sid)} rps"] = p.rates_by_slot[sid]
            for sid, mean in p.mean_by_slot.items():
                row[labels[sid]] = f"{mean:.4g}"
            row["diff %"] = f"{p.mean_diff_pct:+.2f}%"
            row[f"{p.ci_pct:g}% CI (paired t)"] = (
                f"[{p.ci_low_pct:+.2f}, {p.ci_high_pct:+.2f}]"
            )
            row["equiv. margin"] = f"+-{p.margin_pct:g}%"
            row["verdict"] = verdict_word(
                p.verdict, subject=p.direction.subject,
                reference=p.direction.reference, labels=labels,
            )
            row["effect (d_z)"] = f"{p.effect_size:.2f}"
            rows.append(row)
    return _md(rows)


def _windows_cell(lr: SlotResource) -> str:
    """"used / excluded" counts, or the all-flagged posture wording, never a bare n."""
    if lr.posture_only:
        return f"{lr.n_windows} / 0 (ALL {lr.n_windows} flagged; used as posture)"
    return f"{lr.n_windows} / {lr.n_flagged_excluded}"


def _stage_mix_cell(lr: SlotResource) -> str:
    """The confirm/soak composition of the windows behind this slot's number.

    Published because the two slots' aggregated window sets are NOT guaranteed
    to share one, and a soak window is several times longer than a
    confirm window: without this row, a ratio between two differently composed
    samples reads as a like-for-like comparison.
    """
    if not lr.stage_windows:
        return "n/a"
    return ", ".join(f"{n} {stage}" for stage, n in sorted(lr.stage_windows.items()))


def _resource_table(cells: list[CellAnalysis]) -> str:
    """Steady-state resource cost per cell per slot (cgroup v2 ground truth)."""
    rows = []
    for c in cells:
        r = c.resource
        if not r or not r.available:
            continue
        ratio_col = r.ratio_direction.label if r.ratio_direction else "ratio"

        def _row(metric: str, cell_of, ratio, *, res=r, col=ratio_col, cid=c.cell_id):
            out = {"cell": cid, "metric": metric}
            for sid, lr in res.by_slot.items():
                out[_slot_label(sid)] = cell_of(lr)
            out[col] = ratio
            return out

        # `*` marks a slot whose windows were ALL flagged: reported as posture,
        # never silently mixed in with gated measurements.
        def _mark(lr) -> str:
            return "*" if lr.posture_only else ""

        rows.append(_row("app CPU-ms / request (SUT cgroup)",
                         lambda lr: f"{lr.cpu_ms_per_req:.3f}{_mark(lr)}",
                         _fmt(r.cpu_ratio, ".2f") + ("x" if r.cpu_ratio else "")))
        rows.append(_row("DB CPU-ms / request (postgres cgroup)",
                         lambda lr: _fmt(lr.db_cpu_ms_per_req, ".3f",
                                         dash="not measured") + _mark(lr),
                         _fmt(r.db_cpu_ratio, ".2f")
                         + ("x" if r.db_cpu_ratio else "")))
        rows.append(_row("app + DB CPU-ms / request",
                         lambda lr: _fmt(lr.total_cpu_ms_per_req, ".3f",
                                         dash="not measured") + _mark(lr),
                         _fmt(r.total_cpu_ratio, ".2f")
                         + ("x" if r.total_cpu_ratio else "")))
        rows.append(_row("DB share of that total (%)",
                         lambda lr: _fmt(lr.db_share_pct, ".1f", dash="-"), "-"))
        rows.append(_row("windows carrying a DB sample",
                         lambda lr: f"{lr.db_n_windows}/{lr.n_windows}", "-"))
        rows.append(_row("anon memory p99 (MB, in-window)",
                         lambda lr: f"{lr.anon_p99_mb:.0f}{_mark(lr)}",
                         _fmt(r.anon_ratio, ".2f") + ("x" if r.anon_ratio else "")))
        rows.append(_row("cgroup memory.peak (MB, container lifetime)",
                         lambda lr: f"{lr.mem_peak_mb:.0f}{_mark(lr)}",
                         _fmt(r.peak_ratio, ".2f") + ("x" if r.peak_ratio else "")))
        rows.append(_row("windows used / flagged-excluded", _windows_cell, "-"))
        rows.append(_row("stage mix of the windows used", _stage_mix_cell, "-"))
        rows.append(_row("requests serviced (the CPU denominator)",
                         lambda lr: f"{lr.requests}", "-"))
    return _md(rows)




def _posture_notes(cells: list[CellAnalysis]) -> list[str]:
    """Explicit per-slot callouts where EVERY window in a roll-up was flagged.

    The CAUSE is derived, never asserted, and since W22 it is derived from the
    SAME predicate the runner refuses cells with: a causal clause is emitted
    only when `registry.unsatisfiable_gates_for` says a knob this cell declares
    (`phases.CELLS` extra_env) makes that specific gate unsatisfiable AND the
    gate actually appears in the window's failed-gate counts. Round 1's form of
    this knowledge was a table local to this module, so the earliest it could
    act was here, in prose, after the runtime was spent (R1-A3, ~30 h). One
    predicate means a cause the report may claim is a cause the runner would
    have stopped the cell over.

    The waiver clause is read off the PROBES' recorded gate statuses, not off
    the cell table: a gate this posture was not held to must be visible in the
    report the same way it is visible in the per-probe record.
    """
    notes: list[str] = []
    for c in cells:
        r = c.resource
        if not r or not r.available:
            continue
        env = _cell_extra_env(c.cell_id)
        for sid, lr in r.by_slot.items():
            if not lr.posture_only:
                continue
            counts = lr.flagged_gate_counts
            gates = ", ".join(
                f"{name} {cnt}x" for name, cnt in sorted(counts.items())
            ) or (
                # NOT "no detail on disk": the ledger names this cause, and
                # blaming a missing artifact for a flag whose reason is recorded
                # is the claim this branch used to make.
                f"none -- no gate failed; {lr.n_warmup_attack_failed} of "
                f"{lr.n_windows} windows recorded a FAILED WARMUP ATTACK, which "
                "flags a probe on its own (see each probe's `exit_reason`)"
                if lr.n_warmup_attack_failed else
                "none recorded: these windows' records name neither a failed "
                "gate nor a failed warmup attack, so nothing in the warmup "
                "verdict explains the flag -- read the stage's `excluded` map"
            )

            unsat = registry.unsatisfiable_gates_for(sid, env)
            explained = sorted(g for g in unsat if g in counts)
            reasons = [
                f"`{g}` failed in {counts[g]} of {lr.n_windows} windows and "
                f"{unsat[g].why}"
                for g in explained
            ]
            unexplained = sorted(set(counts) - set(explained))

            if reasons:
                cause = " " + "; ".join(reasons) + "."
            elif counts:
                cause = (
                    " No knob this cell declares is registered as making any of "
                    "these gates unsatisfiable by construction "
                    "(`registry.unsatisfiable_gates_for`), so no cause is claimed "
                    "here; the gate counts above are the whole of the evidence."
                )
            else:
                cause = ""
            residual = (
                " The remaining flagged gate(s), "
                + ", ".join(f"`{g}` {counts[g]}x" for g in unexplained)
                + ", are NOT explained by that knob."
                if reasons and unexplained else ""
            )
            waived = (
                " This posture was not held to "
                + ", ".join(
                    f"`{g}` ({n} of {lr.n_windows} windows)"
                    for g, n in sorted(lr.waived_gate_counts.items())
                )
                + ", so those gates are absent from the counts above rather than "
                "passed."
                if lr.waived_gate_counts else ""
            )
            notes.append(
                f"- **{c.cell_id} / {_slot_label(sid)}: all {lr.n_windows} probe "
                f"windows for this slot were flagged under-warmed** (failed "
                f"gates: {gates})." + cause + residual + waived +
                " These numbers are therefore reported as POSTURE EVIDENCE, not as "
                "a gated measurement, and the row is marked `*`."
            )
    return notes


_DB_NOT_MEASURED_LINE = (
    "\nThe DB CPU rows are `not measured` in this run: no "
    f"`cpu_mem_<slot>.{_DB_LABEL}.summary.json` window is on disk (a non-Linux "
    "run, or a cells tree that was not retained). They are omitted rather than "
    "reported as zero, so the app-cost / query-plan-cost split rests on nothing "
    "here and no claim may lean on it.\n"
)


def _driver_sensitivity_clause(slot_set: registry.SlotSet) -> str:
    """The third DB-CPU caveat, read off THIS RUN's slots.

    Under the `shakedown` slot set (dotnet-efcore vs dotnet-ado) BOTH slots are
    Npgsql; under `headline` they are not. The drivers and their one pooling
    behaviour are declared per LANGUAGE (`registry.LanguageSpec.db_driver`), so
    the clause says which of the two cases this run is in.
    """
    by_driver: dict[str, list[str]] = {}
    for sid in slot_set.slots:
        lang = registry.get_slot(sid).language
        by_driver.setdefault(lang.db_driver or "an undeclared driver", []).append(sid)
    parts = [
        f"{_slot_label(sid)} uses {drv}"
        for drv, sids in sorted(by_driver.items()) for sid in sids
    ]
    notes = "; ".join(sorted({
        f"{drv} {registry.get_slot(sids[0]).language.db_driver_pooling_note}"
        for drv, sids in by_driver.items()
        if registry.get_slot(sids[0]).language.db_driver_pooling_note
    }))
    if len(by_driver) > 1:
        return (
            "And it is driver-sensitive, and this run's slots do NOT share a "
            "driver: " + ", ".join(parts) + ". " + notes
            + ". Part of the difference between the two DB columns is therefore "
            "the drivers' pooling behaviour rather than the languages'."
        )
    return (
        "It is also driver-sensitive, but that is NOT a confound between this "
        "run's slots: they share one driver (" + ", ".join(parts) + "). "
        + notes + ". That cost is in BOTH DB columns, so it inflates each "
        "absolute figure while cancelling out of the ratio between them."
    )


def _db_cpu_caveats(slot_set: registry.SlotSet) -> list[str]:
    """What the DB column is, and the three things it is NOT.

    This paragraph exists because the column is the most over-readable number
    in the report. It is a whole-container `cpu.stat` delta, and every caveat
    below is a way it can be read as finer-grained or cleaner than it is. It is
    printed ONCE, beside whichever DB content the report reaches first.
    """
    return [
        "\nThe DB row is PostgreSQL's own cgroup CPU over the SAME windows, under "
        "the SAME request-weighted rule, and it is what separates *this runtime "
        "costs more CPU* from *this ORM's generated SQL costs the database more*. "
        "Attribution is clean without de-interleaving because a block's two "
        "probes run SEQUENTIALLY: while one slot is under load the other's "
        "container is stopped, so the PostgreSQL delta belongs to exactly one "
        "slot.\n"
        "\nThree limits, all of them structural. It is a WHOLE-CONTAINER number, "
        "so it includes autovacuum, the checkpointer, the WAL writer and the "
        "background writer -- maintenance that is debt from earlier windows and "
        "is not proportional to this probe's requests. It is therefore NOT "
        "per-endpoint: only `pg_stat_statements` deltas can attribute cost to a "
        "statement, and this run does not do that. "
        + _driver_sensitivity_clause(slot_set) + "\n",
    ]


def _db_cpu_attribution_section(
    cells: list[CellAnalysis], slot_set: registry.SlotSet
) -> list[str]:
    """The per-(cell, rate, slot) app-vs-DB attribution table, with its caveats."""
    from analysis.report import db_cpu_attribution_table

    rows = _db_cpu_rows(cells)
    if not rows:
        return []
    labels = {sid: _slot_label(sid) for sid in slot_set.slots}
    return [
        *_db_cpu_caveats(slot_set),
        "\n**App vs database CPU, per ladder rung.** One row per (cell, offered "
        "rate, slot). A rate is present only where that rung has a DB sample; "
        "`windows` is how many of that rung's windows carried one. `requests` "
        "and `db_requests` are the two columns' own DENOMINATORS -- the DB "
        "figure divides only the requests of the DB-sampled windows, so the two "
        "differ whenever `windows` is short of the rung's window count, and each "
        "CPU-ms/request column times its own denominator is that column's total "
        "CPU-ms.\n",
        db_cpu_attribution_table(rows, labels=labels),
    ]


def _db_cpu_rows(cells: list[CellAnalysis]) -> list[dict]:
    """(cell, rate, slot) attribution rows from the CPU-vs-rate curve.

    The curve is the right source: the ladder rungs plus the confirm/soak
    operating points cover the whole rate range, and the DB share of per-request
    cost is exactly the kind of quantity that can move with rate.
    """
    out: list[dict] = []
    for c in cells:
        cvr = c.cpu_vs_rate
        if not cvr or not cvr.available:
            continue
        for rung in cvr.rungs:
            for sid, val in rung.by_slot.items():
                if not val.n_db:
                    continue
                app, db = val.cpu_ms_per_req, val.db_cpu_ms_per_req
                total = None if app is None or db is None else app + db
                out.append({
                    "cell": c.cell_id,
                    "offered_rps": rung.rate,
                    "slot": sid,
                    "windows": val.n_db,
                    "requests": val.requests,
                    "db_requests": val.db_requests,
                    "app_cpu_ms_per_req": None if app is None else round(app, 4),
                    "db_cpu_ms_per_req": None if db is None else round(db, 4),
                    "total_cpu_ms_per_req": None if total is None else round(total, 4),
                    "db_share_pct": (
                        None if not total else round(100.0 * db / total, 1)
                    ),
                    "source": f"{_DB_LABEL} cgroup cpu.stat delta over the "
                              "measured window",
                })
    return out


def _resource_scope_clause(cells: list[CellAnalysis]) -> str:
    """Which operating point this roll-up is taken at, per slot, and how chosen.

    Stated because a confirm block also carries the higher-knee slot at the
    COMMON rate (`phases._confirm_members`), so "the windows for this cell" is
    not the same set as "the windows the headline is defined over", and a reader
    who cannot see which was used cannot audit the number.

    The SLO half of the claim is conditional, and that is the point: it is made
    only for the cells whose every slot has a fitted knee it was actually
    selected at. Any other cell is listed under the degraded scope with the
    reason per slot, so the sentence can never name a ladder ceiling as a
    crossing rate the way the unconditional version did.
    """
    per_cell: list[str] = []
    degraded_cells: list[str] = []
    for c in cells:
        r = c.resource
        if not r or not r.available or not r.operating_rate_by_slot:
            continue
        pairs = ", ".join(
            f"{_slot_label(sid)} {rate:,} rps"
            for sid, rate in r.operating_rate_by_slot.items()
        )
        (degraded_cells if r.scope_degraded_slots else per_cell).append(
            f"{c.cell_id}: {pairs}"
        )
    if not per_cell and not degraded_cells:
        return ""
    bases = {c.resource.operating_point_basis for c in cells
             if c.resource and c.resource.available}
    how = ("read off the confirm/soak stage records" if bases == {"declared"}
           else "read off the confirm/soak stage records where they name it, and "
                "otherwise bounded by that slot's fitted knee (a ledger written "
                "before those keys existed)")
    parts: list[str] = []
    if per_cell:
        parts.append(
            "SCOPE: `own_knee` -- every window here was measured with its slot AT "
            f"ITS OWN SLO-crossing rate ({'; '.join(per_cell)}), {how}."
        )
    if degraded_cells:
        why = "; ".join(
            f"{c.cell_id}: " + "; ".join(
                f"{_slot_label(sid)} -- {reason}"
                for sid, reason in c.resource.scope_degraded_slots.items()
            )
            for c in cells
            if c.resource and c.resource.available and c.resource.scope_degraded_slots
        )
        parts.append(
            "SCOPE: `declared_operating_point` -- these windows were measured at "
            "the rate their stage DECLARED, which for at least one slot is NOT "
            f"that slot's SLO-crossing rate ({'; '.join(degraded_cells)}), {how}. "
            f"Where the claim fails, and why: {why}. Cost read at a rate that is "
            "not a measured crossing is not cost at the latency promise, so no "
            "figure in this roll-up is the headline metric for that slot."
        )
    parts.append(
        "The confirm stage also runs the higher-knee slot at the COMMON rate for "
        "the confound-free contrast; those windows are NOT in this roll-up (they "
        "would make one slot's figure a blend of two operating points against the "
        "other's one) and are published as the `common_rate` paired verdict "
        "instead."
    )
    return " ".join(parts)


def _stage_mix_sentences(cells: list[CellAnalysis]) -> list[str]:
    """Disclose (and quantify) any confirm/soak asymmetry behind a CPU ratio.

    The two slots' surviving window sets are built by the same exclusion but
    need not have the same stage composition: a flagged soak window drops one
    slot's longest window and keeps the other's. A soak window services roughly
    5x the requests of a confirm window, so under ANY weighting the two
    published numbers then average different stage mixes. Rather than leave that
    implicit, the asymmetry is named and the alternative weighting's ratio is
    printed beside the published one, so a reader can see exactly what the
    choice is worth.
    """
    out: list[str] = []
    for c in cells:
        r = c.resource
        if not r or not r.available or r.cpu_ratio is None:
            continue
        mixes = [lr.stage_windows for lr in r.by_slot.values()]
        if all(m == mixes[0] for m in mixes):
            continue
        composition = ", ".join(
            f"{_slot_label(sid)} {_stage_mix_cell(lr)}" for sid, lr in r.by_slot.items()
        )
        out.append(
            f"\n**Stage-mix asymmetry in {c.cell_id}.** The windows behind the two "
            f"CPU figures do not have the same stage composition: {composition}. "
            f"{_stage_mix_cause(r)} A soak window is the run's longest probe and "
            f"services several times a confirm window's requests, so the two figures "
            f"aggregate different stage mixes. "
            + _weighting_sentence(r, {v.margin_pct for v in c.paired_verdicts})
            + "\n"
        )
    return out


def _stage_mix_cause(r: ResourceCost) -> str:
    """WHY the two window sets differ, derived from the counts, never asserted.

    Derived from the flagged-excluded counts so the clause cannot contradict the
    table row below it; when the counts do not explain the difference, it says
    so.
    """
    excluded = {sid: lr.n_flagged_excluded for sid, lr in r.by_slot.items()}
    if any(excluded.values()):
        detail = ", ".join(
            f"{_slot_label(sid)} {n}" for sid, n in excluded.items()
        )
        return (
            f"Windows flagged under-warmed / gate-excluded and dropped from the "
            f"roll-up: {detail} -- which is what the two sets differ by."
        )
    return (
        "NO window of either slot was flagged under-warmed or gate-excluded (the "
        "flagged-excluded row below is zero for both), so exclusion is not the "
        "cause: the two slots simply have different numbers of measured windows "
        "in this roll-up. The cause is not derivable from this cell's counts and "
        "is not guessed at here."
    )


def _weighting_sentence(r: ResourceCost, margins: set[float]) -> str:
    """What the request-weighting choice is worth, WITH the conclusion derived.

    The conclusion is derived from the gap between the two ratios, never a
    constant. The comparison is against the margin the cell's OWN paired verdict
    was decided at, which is the only threshold in the report that means
    anything for a ratio.
    """
    if r.cpu_ratio is None or not r.cpu_ratio_unweighted:
        return "The alternative weighting could not be computed for this cell."
    gap_pct = 100.0 * abs(r.cpu_ratio - r.cpu_ratio_unweighted) / r.cpu_ratio_unweighted
    head = (
        f"Measured effect on the published ratio: the equal-weight-per-window "
        f"alternative gives {r.cpu_ratio_unweighted:.3f}x against the published "
        f"request-weighted {r.cpu_ratio:.3f}x, a {gap_pct:.1f}% difference"
    )
    if len(margins) != 1:
        return (
            head + ". This cell has no single equivalence margin to judge that "
            "against, so whether the weighting choice carries the headline is "
            "left to the reader with both numbers in hand."
        )
    margin = next(iter(margins))
    if gap_pct > margin:
        return (
            head + f" -- {gap_pct / margin:.2f}x the +-{margin:g}% equivalence "
            "margin this cell's verdict is decided against. The weighting choice "
            "DOES carry this cell's headline."
        )
    return (
        head + f", inside the +-{margin:g}% equivalence margin this cell's verdict "
        "is decided against, so the weighting choice does not carry this cell's "
        "headline. It is still the reader's to check, not ours to leave implicit."
    )


# --------------------------------------------------------------------------- #
# report body
# --------------------------------------------------------------------------- #
def _load_manifest(run_id: str):
    """The run's manifest, or None when it cannot be read (an isolated tree)."""
    from .manifest import RunManifest

    try:
        return RunManifest.load(run_id)
    except Exception:  # noqa: BLE001 - a report is still worth writing without it
        return None


def _counter_phrase(
    cells: list[CellAnalysis], clean_attr: str, missing_attr: str, zero_words: str,
) -> str:
    """One counter's verdict across the aggregated slots, in THREE states.

    Clean and not-clean are not the two possibilities. A slot no window of which
    could read the counter has no verdict at all -- `*_clean` is False for it
    precisely so the JSON cannot claim otherwise -- and printing that as
    "NON-ZERO" would just be the opposite lie. Unread slots are named as unread,
    and the per-window shortfall rides along even when the readable windows were
    clean: a verdict over 2 of 40 windows is not the claim a verdict over 40 is,
    and that count was computed, stored in the JSON and printed nowhere.
    """
    bad: list[str] = []
    unread: list[str] = []
    missing = 0
    for c in cells:
        for sid, lr in c.resource.by_slot.items():
            name = f"{c.cell_id}/{sid}"
            n_missing = getattr(lr, missing_attr)
            missing += n_missing
            if n_missing >= lr.n_windows:
                unread.append(name)
            elif not getattr(lr, clean_attr):
                bad.append(name)
    bits: list[str] = []
    if bad:
        bits.append("NON-ZERO in " + ", ".join(bad))
    if unread:
        bits.append("NOT ESTABLISHED for " + ", ".join(unread)
                    + " (no window carried the counter)")
    if not bits:
        bits.append(zero_words)
    if missing:
        bits.append(f"{missing} window(s) carried no read")
    return " -- ".join(bits)


@dataclass(frozen=True)
class _SwapScope:
    """WHICH cgroups the windows behind ONE per-window swap verdict covered.

    Derived, never spelled. `_window_rows` builds each window's `swap_docs` as
    the SUT summary plus PostgreSQL's only WHEN the co-sample is on disk, and a
    run with no PostgreSQL sampler is a supported path there (gating on it would
    delete the whole resource section rather than one column). So a sentence
    that names both cgroups regardless claims a scope the evidence may not have.
    """

    n_windows: int                  # windows that contributed to the verdict
    n_with_db: int                  # of those, how many co-sampled PostgreSQL
    slots: tuple[str, ...]          # slot ids contributing at least one window

    @property
    def labels(self) -> tuple[str, ...]:
        """The sampler labels this verdict actually covers (slot ids, + the DB)."""
        return self.slots + ((_DB_LABEL,) if self.n_with_db else ())

    def phrase(self) -> str:
        """The parenthetical scope label printed beside the verdict."""
        if not self.n_windows:
            return "no window in scope"
        if self.n_with_db == self.n_windows:
            return "SUT and PostgreSQL cgroups"
        if not self.n_with_db:
            return "SUT cgroups only: no window in scope carried a PostgreSQL co-sample"
        return (
            f"SUT cgroups in all {self.n_windows} window(s), PostgreSQL in "
            f"{self.n_with_db} of them"
        )


def _swap_scope(
    cells: list[CellAnalysis], missing_attr: str, db_attr: str
) -> _SwapScope:
    """Roll one swap verdict's window scope up across every aggregated slot."""
    n_windows = 0
    n_with_db = 0
    slots: list[str] = []
    for c in cells:
        for sid, lr in c.resource.by_slot.items():
            in_scope = lr.n_windows - getattr(lr, missing_attr)
            if in_scope <= 0:
                continue
            n_windows += in_scope
            n_with_db += getattr(lr, db_attr)
            slots.append(sid)
    return _SwapScope(n_windows, n_with_db, tuple(sorted(set(slots))))


def _container_ceiling_sentence(manifest, *, covered: Sequence[str]) -> str:
    """What the sampled cgroups' OWN memory ceilings and swap OUTCOMES read back.

    This paragraph used to assert infra/compose.bench.yaml's
    `memswap_limit: mem_limit` rule as prose -- "the containers have no swap
    allowance to use; this is the evidence, not the setting" -- while
    `ContainerTopology.mem_limit_bytes` and `.mem_swap_max`, the read-backs that
    could prove it, sat unread one field away.

    `swap_current_max_bytes` / `swap_peak_max_bytes` were the next field over
    and were written and read by nothing at all. They are the only swap evidence
    covering the sampled cgroups the per-window verdict does NOT reach, and
    WHICH those are is a property of this run, not a fixed list: `covered` is
    the label set that verdict actually spans, `_stamp_topology` skips any
    cgroup whose cpuset and memory.max both failed to read, and the difference
    is computed here rather than named. These are coarser -- a max over the
    whole run rather than per window -- and that is stated rather than glossed.

    The two swap keys carry SEPARATE unread sets, for the same reason the
    per-window verdict does: `memory.swap.peak` landed in kernel 6.5, so a
    cgroup can read one key and not the other, and one merged `unread` put such
    a row in neither the swapped nor the unread set and let the clean branch
    speak for a counter that never read.
    """
    rows = getattr(getattr(manifest, "topology", None), "containers", None) or {}
    if not rows:
        return ("No sampled cgroup recorded its own `memory.max` this run, so the "
                "memory ceilings and the swap allowance rest on what "
                "infra/compose.bench.yaml asked for rather than on a read-back.\n")

    def _gib(n: int | None) -> str:
        return "unread" if n is None else f"{n / 1024 ** 3:.2f} GiB"

    def _bytes(n: int | None) -> str:
        return "unread" if n is None else f"{n} B"

    seen = "; ".join(
        f"`{label}` memory.max={_gib(r.mem_limit_bytes)}, "
        f"memory.swap.max={r.mem_swap_max or 'unread'}, "
        f"max memory.swap.current={_bytes(r.swap_current_max_bytes)}, "
        f"max memory.swap.peak={_bytes(r.swap_peak_max_bytes)}"
        for label, r in sorted(rows.items())
    )
    allowed = sorted(
        label for label, r in rows.items() if r.mem_swap_max not in (None, "", "0")
    )
    verdict = (
        "A `memory.swap.max` of 0 is compose's `memswap_limit: mem_limit` as the "
        "kernel applied it -- no swap allowance to spend -- and it is compared "
        "against the overlay on the run's FIRST measured window, not here."
        if not allowed else
        "`memory.swap.max` is NOT 0 for " + ", ".join(f"`{x}`" for x in allowed)
        + ": those containers had a swap allowance to spend, so the swap verdict "
        "above is all that stands between the memory figures and swapped-out "
        "anon pages."
    )
    def _names(labels) -> str:
        return ", ".join(f"`{x}`" for x in labels)

    swapped = sorted(
        label for label, r in rows.items()
        if (r.swap_current_max_bytes or 0) > 0 or (r.swap_peak_max_bytes or 0) > 0
    )
    # PER KEY, never merged: a kernel older than 6.5 has no `memory.swap.peak`,
    # so a row can read the level and not the high-water mark. A single `unread`
    # requiring BOTH to be None put that row in neither set and let the clean
    # branch assert a lifetime-high-water read that never happened.
    cur_read = sorted(lb for lb, r in rows.items() if r.swap_current_max_bytes is not None)
    peak_read = sorted(lb for lb, r in rows.items() if r.swap_peak_max_bytes is not None)
    cur_unread = sorted(set(rows) - set(cur_read))
    peak_unread = sorted(set(rows) - set(peak_read))
    if swapped:
        outcome = (
            "SWAPPED BYTES RECORDED for " + _names(swapped)
            + ": every memory figure taken from those cgroups is understated, "
            "because a swapped-out anon page leaves `memory.stat anon`."
        )
    elif not cur_read and not peak_read:
        outcome = ("No sampled cgroup recorded a swap outcome, so nothing here "
                   "establishes whether any of them swapped.")
    else:
        bits = []
        if cur_read:
            bits.append("`memory.swap.current` read 0 at every window edge for "
                        + _names(cur_read))
        if peak_read:
            bits.append("`memory.swap.peak`, the kernel's own lifetime high-water "
                        "mark, read 0 for " + _names(peak_read))
        outcome = "; ".join(bits) + "."
        # WHICH cgroups that adds to the per-window verdict is derived from the
        # rows present, not named: `_stamp_topology` drops any label whose
        # cpuset and memory.max both failed to read, so a fixed "redis, vegeta
        # and the container-runtime units" claimed coverage of cgroups that may
        # have no row at all.
        extra = sorted((set(cur_read) | set(peak_read)) - set(covered))
        if extra:
            outcome += (" That covers " + _names(extra)
                        + ", which the per-window verdict above does not.")
    if cur_read or peak_read:
        for key, missing in (("`memory.swap.current`", cur_unread),
                             ("`memory.swap.peak`", peak_unread)):
            if missing:
                outcome += (f" {key} NOT ESTABLISHED for " + _names(missing)
                            + ", whose counter never read.")
    return ("Each sampled cgroup's own ceilings, read at window start, and its swap "
            "outcome maximised over ALL the run's windows, recorded in "
            f"`manifest.topology.containers`: {seen}. {verdict} {outcome}\n")


# Dials whose value decides whether a tier's numbers are measurement-grade, in
# the order a reader cares about them. Compared against `full` so the banner
# names the deviations THIS tier actually has rather than a remembered list.
_TIER_DIALS = (
    "warmup_cap_s", "warmup_discard_s", "measure_s", "ladder_start_rps",
    "ladder_step_factor", "ladder_max_rungs", "ladder_reps_per_rung",
    "confirm_blocks", "soak_s", "seed_scale", "body_pool_requests",
    "warmup_min_calls_factor",
)


def _tier_provenance(led: Ledger) -> dict:
    """What measurement tier produced this report, from the run's pinned scale.

    A `smoke` run's report carried knee fits, per-slot SLO verdicts, a CPU
    ratio, a paired verdict and a cost roll-up with nothing anywhere saying the
    tier is functional-only -- while CLAUDE.md says smoke "proves plumbing only
    ... is not measurement-grade and cannot validate JVM warmup". Given round
    1's failure mode (a number quoted out of a document that disclosed its own
    limits elsewhere), the tier has to be the first thing on the page and a
    field in `fits.json`, not an inference from the run id.

    `functional_only` is `phases.ScaleProfile`'s own flag, and the deviations
    are DIFFED against the `full` profile rather than listed, so a dial that
    changes in either profile changes the banner with it. Both resolutions can
    raise (an unknown scale, an unreadable spec, a bad
    LEDGERBENCH_BODY_POOL_REQUESTS in the analyze-time environment); the tier is
    then reported as unresolved,
    which is itself a reason not to quote the numbers.
    """
    scale = led.scale
    out: dict = {
        "scale": scale, "resolved": False, "functional_only": None,
        "measurement_grade": None, "deviations_from_full": {},
    }
    if not scale:
        return out
    try:
        slo = load_slo()
        prof = phases.ScaleProfile.resolve(scale, slo)
        full = phases.ScaleProfile.resolve("full", slo)
    except Exception:  # noqa: BLE001 - unknown scale, or spec unreadable
        return out
    out["resolved"] = True
    out["functional_only"] = prof.functional_only
    out["measurement_grade"] = not prof.functional_only
    out["deviations_from_full"] = {
        d: {"this_tier": getattr(prof, d), "full": getattr(full, d)}
        for d in _TIER_DIALS
        if getattr(prof, d) != getattr(full, d)
    }
    return out


def _tier_banner(prov: dict) -> str:
    """The tier banner, at the top of the report. Every clause comes off `prov`."""
    scale = prov["scale"]
    if not scale:
        return (
            "> **MEASUREMENT TIER NOT RECORDED.** This run's ledger carries no "
            "`scale`, so nothing here establishes whether it was a "
            "measurement-grade run or a functional rehearsal. Treat every number "
            "below as unattributed until the tier is known.\n"
        )
    if not prov["resolved"]:
        return (
            f"> **MEASUREMENT TIER `{scale}` COULD NOT BE RESOLVED.** This build's "
            "`phases.ScaleProfile` does not define that scale, or `spec/` was "
            "unreadable at analyze time, so the dials this run used are not known "
            "here and no number below can be attributed to a tier.\n"
        )
    devs = prov["deviations_from_full"]
    dev_s = ", ".join(
        f"`{d}` {v['this_tier']} vs full's {v['full']}" for d, v in sorted(devs.items())
    )
    if prov["functional_only"]:
        return (
            f"> **FUNCTIONAL REHEARSAL ONLY -- NOT MEASUREMENT-GRADE.** This run "
            f"used the `{scale}` scale profile, whose "
            f"`ScaleProfile.functional_only` is True. Under that flag the runner "
            f"applies neither the per-probe achieved-vs-offered nor the "
            f"error-rate exclusion (`phases._execute_block_probes`), does not "
            f"enforce the PostgreSQL capacity gate (`phases._enforce_gate`, "
            f"`phases._no_gate_record`), and waives the `docker_native_engine` "
            f"doctor check that cgroup resource ground truth rests on "
            f"(`phases.run_doctor_phase`). Its dials differ from `full` at "
            f"{dev_s}. Every knee fit, SLO verdict, CPU ratio, "
            f"paired verdict and roll-up below was produced by that profile and "
            f"proves the plumbing, not the numbers. **No figure in this report may "
            f"be quoted as a measurement.**\n"
        )
    if not devs:
        return (
            f"> **Measurement tier `{scale}`: measurement-grade.** "
            f"`ScaleProfile.functional_only` is False and every dial in "
            f"`analyze._TIER_DIALS` matches the `full` profile, so nothing in this "
            f"report is reduced relative to a full run.\n"
        )
    return (
        f"> **Measurement tier `{scale}`: measurement-grade, REDUCED.** "
        f"`ScaleProfile.functional_only` is False, so the per-probe "
        f"achieved-vs-offered and error-rate exclusions applied, the PostgreSQL "
        f"capacity gate was enforced, and the `docker_native_engine` doctor check "
        f"was not waived. The dials that differ from `full` "
        f"are {dev_s} -- a dial that differs is a dial this tier does not "
        f"validate, so read every interval, rung count and roll-up below as "
        f"resting on that reduced budget.\n"
    )


@dataclass(frozen=True)
class _RateDependence:
    """Whether THIS run's per-request CPU moved with the offered rate.

    The report used to assert the answer as fixed text -- "It does move." above
    the CPU-vs-rate table, and "Per-request CPU is NOT rate-independent" above
    the roll-up. Both were round 1's finding (its .NET column ran at CoV 19.3%)
    written into a round-2 generator, and CLAUDE.md forbids quoting a round-1
    number beside a round-2 one; a verdict is a number quoted in words. In a run
    whose columns are flat they also rendered directly above this run's own
    derived "Coefficient of variation ... 0.0%" line and contradicted it.

    `max_cv_pct` is the value ROUNDED the way `_build_report_md` prints it, and
    `moves` is `max_cv_pct > 0` on that rounded value, so the verdict cannot
    disagree with the figure printed beside it. `established` is False when no
    cell carries two rung-values for one slot: `_cv_pct` needs n >= 2 and
    returns None below it, and a run that measured one rung has not tested the
    question at all.
    """

    established: bool
    moves: bool
    max_cv_pct: float | None = None
    max_cv_slot: str = ""
    max_cv_cell: str = ""
    # Rungs BEHIND `max_cv_pct` -- `CpuVsRate.n_values(max_cv_slot)` in
    # `max_cv_cell`, not the height of any table. A max over cells would come
    # from a cell whose ladder was bracketed independently, and `len(rungs)`
    # counts rows the winning slot has no value at.
    cv_n_rungs: int = 0


def _rate_dependence(cells: list["CellAnalysis"]) -> _RateDependence:
    best: tuple[float, str, str, int] | None = None
    for c in cells:
        cvr = c.cpu_vs_rate
        if not cvr or not cvr.available:
            continue
        for sid, cv in cvr.cv_pct_by_slot.items():
            if cv is None:
                continue
            rounded = round(float(cv), 1)
            if best is None or rounded > best[0]:
                best = (rounded, sid, c.cell_id, cvr.n_values(sid))
    if best is None:
        return _RateDependence(established=False, moves=False)
    return _RateDependence(
        established=True, moves=best[0] > 0.0,
        max_cv_pct=best[0], max_cv_slot=best[1], max_cv_cell=best[2],
        cv_n_rungs=best[3],
    )


def _rate_dependence_clause(rd: _RateDependence) -> str:
    """The CPU-vs-rate section's verdict, as strong as the number behind it."""
    if not rd.established:
        return (
            "no cell of this run carries two or more ladder rungs with a "
            "CPU-ms/request value for the same slot, so this run does not "
            "establish it either way."
        )
    if rd.moves:
        return (
            f"the largest is {rd.max_cv_pct:.1f}% "
            f"({_slot_label(rd.max_cv_slot)}, {rd.max_cv_cell}, over the "
            f"{rd.cv_n_rungs} rungs that slot has a value at), so at least one "
            f"slot's per-request CPU is NOT constant across this run's rungs."
        )
    return (
        f"every per-slot coefficient of variation rounds to 0.0% at the "
        f"precision printed there -- the largest ({_slot_label(rd.max_cv_slot)}, "
        f"{rd.max_cv_cell}) over the {rd.cv_n_rungs} rungs that slot has a value "
        f"at -- so no slot's per-request CPU moved measurably over the rungs it "
        f"was measured at."
    )


def _operating_point_clause(rd: _RateDependence) -> str:
    """The roll-up's "these are values AT these rates" sentence.

    The last clause is unconditional because it is the conservative reading in
    every branch: a roll-up taken at one or two offered rates is a measurement
    at those rates whether or not the curve behind it turned out to be flat.
    """
    if not rd.established:
        lead = (
            "Whether per-request CPU is rate-independent is NOT ESTABLISHED by "
            "this run (no cell carries two ladder rungs with a value for one "
            "slot)"
        )
    elif rd.moves:
        lead = (
            f"Per-request CPU MOVED across this run's ladder rungs (largest "
            f"per-slot coefficient of variation {rd.max_cv_pct:.1f}%, "
            f"{_slot_label(rd.max_cv_slot)}, over {rd.cv_n_rungs} rungs)"
        )
    else:
        lead = (
            f"Per-request CPU did not move measurably over the ladder rungs each "
            f"slot was measured at (every per-slot coefficient of variation "
            f"rounds to 0.0%; the largest spans {rd.cv_n_rungs} rungs), which is "
            f"a result about the rungs this ladder tested and not a stack "
            f"constant"
        )
    return (
        f"{lead}; see the CPU-vs-rate section. These are therefore values AT "
        "these rates.\n"
    )


def _warmup_census_section(cells: list[CellAnalysis]) -> list[str]:
    """The under-warmed fraction per cell per slot, and the P8 stop rule read.

    Round 1 published this number once, computed by hand after the run, and it
    was 59.5%. Printing it per cell per slot is the whole of the fix: it is the
    quantity that says whether the flatness gates are calibrated, and the run
    has to be able to say it while there is still time to stop.
    """
    census = [c for cell in cells for c in cell.warmup_census]
    if not census:
        return []
    pct = WARMUP_ABORT_FLAGGED_FRACTION * 100.0
    parts = ["\n## Warmup census (under-warmed fraction per cell per slot)\n"]
    parts.append(
        "Counted over every DONE probe of the "
        + ", ".join(f"`{s}`" for s in _WARMUP_CENSUS_STAGES)
        + " stages, from the same per-probe evidence the fits excluded on "
        "(`gates_failed` plus `warmup_attack_failed` in `ledger.json`). "
        f"PROGRESS.md's P8 abort rule is `> {pct:g}%` for either slot; the "
        "`over P8 rule?` column is that rule applied, not a judgement. `by gate` "
        "counts each failing gate separately, so it can exceed the flagged count "
        "when one probe failed several; `by reason` attributes exactly one cause "
        "per flagged probe and therefore sums to it.\n"
    )
    rows = []
    for c in census:
        rows.append({
            "cell": c.cell_id,
            "slot": _slot_label(c.slot),
            "probes": c.n_probes,
            "flagged": c.n_flagged,
            "flagged %": (
                "n/a" if c.flagged_fraction is None
                else f"{c.flagged_fraction * 100:.1f}"
            ),
            f"over P8 rule (>{pct:g}%)?": "YES" if c.exceeds_abort_threshold else "no",
            "by gate": ", ".join(f"{g} {n}" for g, n in c.by_gate.items()) or "-",
            "by reason": ", ".join(f"{r} {n}" for r, n in c.by_reason.items()) or "-",
        })
    parts.append(_md(rows))
    over = [c for c in census if c.exceeds_abort_threshold]
    if over:
        parts.append(
            "\n" + "\n".join(
                f"- **{c.cell_id} / {_slot_label(c.slot)}: "
                f"{c.n_flagged} of {c.n_probes} probes "
                f"({c.flagged_fraction * 100:.1f}%) flagged under-warmed**, above "
                f"the pre-registered {pct:g}% stop rule."
                for c in over
            ) + "\n"
        )
    return parts


def _sub_knee_sentences(
    cells: list[CellAnalysis], led: Ledger, slots: Sequence[str]
) -> list[str]:
    """Name every slot that reached the rung ceiling without crossing.

    A slot with no measured knee is stated in the report rather than left
    implicit. The ladder stage records `ceiling_reached_without_crossing` and
    `rung_ceiling_rps`, and this reads them back rather than re-deriving the
    fact from the fits.
    """
    out: list[str] = []
    for c in cells:
        rec = led.cell(c.cell_id).ladder
        sub = [sid for sid in rec.data.get("ceiling_reached_without_crossing", [])
               if sid in slots]
        if not sub:
            continue
        ceiling = rec.data.get("rung_ceiling_rps")
        tops = "; ".join(
            f"{_slot_label(sid)} topped out at "
            f"{_fmt(c.knee_by_slot[sid].max_rung_rps, '.0f')} rps with p99 "
            f"{_fmt(c.knee_by_slot[sid].max_rung_p99_ms, '.2f')} ms"
            for sid in sub if sid in c.knee_by_slot
        )
        degraded = (
            " The run was allowed to continue under "
            f"`{rec.data.get('degraded_allowed_by')}`, so this cell is stamped "
            "DEGRADED in the ledger."
            if rec.data.get("degraded") else ""
        )
        out.append(
            f"\n- **{c.cell_id}: {', '.join(_slot_label(s) for s in sub)} reached the "
            f"{ceiling} rps rung ceiling WITHOUT crossing the SLO.** {tops}. That slot "
            f"has no measured knee, so it has no operating point at the latency "
            f"promise and no number below is cost at its crossing rate; the ceiling is "
            f"a LOWER BOUND on its knee, never an estimate of it.{degraded}"
        )
    return out


def _paired_block_sentences(cells: list[CellAnalysis]) -> list[str]:
    """What each verdict's block count survived, and whether it is enough.

    Two exclusion counts, never one: `solo` blocks were never pairable BY
    CONSTRUCTION (a one-member block), while `incomplete` blocks lost a probe to
    warm/gate exclusion. The old pairing dropped both through the same key miss
    and reported neither.
    """
    out: list[str] = []
    for c in cells:
        seen: set[tuple[str, int, int, int, int]] = set()
        for v in c.paired_verdicts:
            k = (v.verdict_scope, v.n_blocks, v.min_blocks,
                 v.n_blocks_solo, v.n_blocks_incomplete)
            if k in seen:
                continue
            seen.add(k)
            bits = [f"{v.n_blocks} paired block(s)"]
            if v.n_blocks_solo:
                bits.append(f"{v.n_blocks_solo} excluded as solo (one-member) blocks")
            if v.n_blocks_incomplete:
                bits.append(
                    f"{v.n_blocks_incomplete} dropped for a missing or flagged probe"
                )
            floor = (
                f" This is BELOW the pre-registered minimum of {v.min_blocks} "
                f"(spec/slo.yaml `ladder.confirm_reps`): read the verdict as "
                f"directional only."
                if v.under_min_blocks else ""
            )
            out.append(
                f"\n- **{c.cell_id}** ({v.verdict_scope}): " + ", ".join(bits) + "."
                + floor
            )
    return out


def _build_report_md(
    run_id: str, slo_ms: float, cells: list[CellAnalysis], led: Ledger,
    slot_set: registry.SlotSet,
) -> str:
    slots = slot_set.slots
    names = " and ".join(_slot_label(sid) for sid in slots)
    parts: list[str] = [
        f"# Ledgerline analysis report - run `{run_id}`\n",
        _tier_banner(_tier_provenance(led)),
    ]

    parts.append(
        "## Knee fits (isotonic p99-vs-rate crossing "
        f"{slo_ms:g} ms): ONE PER SLOT\n"
    )
    parts.append(
        f"Each of {names} laddered until ITS OWN p99 crossed {slo_ms:g} ms, and "
        "each is fitted over its own rungs alone. There is no combined fit and no "
        "shared operating rate, because the headline metric is cost at a fixed "
        "LATENCY PROMISE and that rate is only defined per stack. A ladder that "
        "stops when a mixed-stack mean crosses measures the slower-degrading "
        "stack nowhere near its own knee -- and the rungs above that point are "
        "never offered at all, so no later analysis can recover them.\n"
    )
    parts.append(
        "The crossing rule is ONE rule. `analysis.knee.crosses` -- the isotonic "
        "(PAVA) fit of the rung means -- is what the runner's ladder stops on and "
        "what this table reports, so where the ladder stopped and where the "
        "published fit crosses cannot be two different rates. A `scope` cell "
        "marked DISAGREES WITH THE LADDER would mean that invariant broke.\n"
    )
    parts.append(_knee_table(cells, slo_ms))
    ceilings = _sub_knee_sentences(cells, led, slots)
    if ceilings:
        parts.append("\n" + "\n".join(ceilings) + "\n")
    if any(k.knee_rps is not None
           for c in cells for k in c.knee_by_slot.values()):
        parts.append("\n" + _knee_ci_note(cells, led))

    parts.append(f"\n## The latency promise at the offered rate (p99 vs {slo_ms:g} ms SLO)\n")
    parts.append(
        "This is a CONFORMANCE readout, not a comparison. Each slot confirms at its "
        "own fitted knee, so both stacks sit at roughly the promise BY CONSTRUCTION "
        "and a head-to-head p99 verdict would be a guaranteed tie -- an artefact of "
        "the design. What a reader needs here is evidence the promise was met, per "
        "slot, at the rate that slot was measured at. (The head-to-head verdict is "
        "the paired COST verdict below.)\n"
    )
    parts.append(
        f"THE VERDICT RULE: the `meets {slo_ms:g} ms?` column compares the MEAN of the "
        f"aggregated confirm/soak windows' p99 values against {slo_ms:g} ms. It is a "
        f"DIFFERENT quantity from the ladder's crossing rule -- different stage, "
        f"different windows, an arithmetic mean rather than an isotonic fit, and the "
        f"opposite inequality at the boundary, so a slot sitting at exactly "
        f"{slo_ms:g} ms has both `crossed` there and `meets` it. It is also lenient "
        f"for a tail promise: a slot can be marked `yes` while individual windows miss "
        f"badly. So the worst window and the count of windows whose OWN p99 was under "
        f"{slo_ms:g} ms are printed in the same row: read those three columns "
        f"together, never the verdict alone.\n"
    )
    parts.append(_slo_table(cells, slo_ms))
    sentences = _slo_sentences(cells, slo_ms)
    if sentences:
        parts.append("\n" + "\n".join(sentences) + "\n")
    flags = [f for c in cells for f in c.slo_consistency]
    if flags:
        parts.append(
            "\nCONSISTENCY FLAGS -- a slot marked `yes` at a rate strictly ABOVE its "
            "own fitted knee. The two quantities are allowed to differ (an isotonic "
            "fit of 2-3-rep ladder rungs against a mean of confirm/soak windows), but "
            "a difference is information: either the knee is fitted low or the confirm "
            "windows were kinder than the ladder rungs at the same rate. Round 1 "
            "narrated this in prose; it is a flag, with both numbers.\n"
        )
        parts.append("\n".join(
            f"- **{f.cell_id}**: {_slot_label(f.slot)} meets {f.slo_ms:g} ms at "
            f"{f.rate} rps in `{f.stage}` (mean p99 {f.mean_p99_ms:.2f} ms), but its "
            f"own fitted knee is {f.knee_rps:.0f} rps."
            for f in flags
        ) + "\n")

    parts.append("\n## CPU-ms per request vs offered rate (is the cost rate-independent?)\n")
    rate_dep = _rate_dependence(cells)
    parts.append(
        "A per-request cost quoted at one operating point is only a stack property if "
        "it does not move with rate. Whether it moves is READ OFF the per-slot "
        "coefficients of variation printed under the table(s) below, never asserted "
        "here: " + _rate_dependence_clause(rate_dep) + " Each row is a rate this cell "
        "measured windows at -- the ladder rungs PLUS each slot's confirm/soak "
        "operating point, so the rate the headline is read at is on its own curve; "
        "'rung' in this section means a row of this table. A value is that row's "
        "TOTAL cgroup CPU delta divided by the TOTAL requests its "
        "windows serviced (the same request-weighted rule the roll-up below uses). "
        "`*` = no measurement-grade window at that rung for that slot, so the "
        "flagged windows were used (posture only).\n"
    )
    for c in cells:
        cvr = c.cpu_vs_rate
        if not cvr or not cvr.available:
            continue
        parts.append(f"\n**{c.cell_id}**\n")
        parts.append(_cpu_vs_rate_table(c))
        # Each slot's own rung count, never the table's height: a rung with no
        # window for one slot is `n/a` in that column and `_cv_pct` dropped it.
        cvs = ", ".join(
            f"{_slot_label(sid)} {_fmt(cv, '.1f')}% over {cvr.n_values(sid)} rungs"
            for sid, cv in cvr.cv_pct_by_slot.items()
        )
        ratio_name = cvr.ratio_direction.label if cvr.ratio_direction else "ratio"
        parts.append(
            f"\nCoefficient of variation, per slot: "
            f"{cvs}. {ratio_name} ratio spans {_fmt(cvr.ratio_min, '.2f')}x to "
            f"{_fmt(cvr.ratio_max, '.2f')}x across the ladder"
            + (f"; {cvr.n_posture_only_values} rung-values are posture-only.\n"
               if cvr.n_posture_only_values else ".\n")
        )
    if not any(c.cpu_vs_rate and c.cpu_vs_rate.available for c in cells):
        parts.append(
            "_Not available in this run: no per-window cgroup summaries under "
            "any of `cells/<cell>/{ladder,confirm,soak}`._\n"
        )
    # The app-vs-DB split belongs HERE, beside the per-rung curve it decomposes,
    # not in the operating-point section: the DB's share of per-request cost is
    # exactly the kind of quantity that can move with rate, and the ladder is
    # the only stage measured across the whole range.
    parts.extend(_db_cpu_attribution_section(cells, slot_set))

    parts.extend(_warmup_census_section(cells))

    parts.append("\n## Stationarity (per-second p99, Mann-Kendall + Theil-Sen band)\n")
    parts.append(_stationarity_table(cells))

    parts.append(
        f"\n## Paired {slot_set.ratio_label()} COST verdict "
        "(confirm stage, RCB-paired by block)\n"
    )
    paired = [c for c in cells if c.paired_verdicts]
    if paired:
        scopes = sorted({v.verdict_scope for c in paired for v in c.paired_verdicts})
        # "both stacks sit at roughly the SLO" is true only where both HAVE a
        # knee. A slot with no knee sits below the promise at its ceiling, so
        # the clause is conditional on scope.
        tie_clause = (
            f"At their own knees both stacks sit at roughly {slo_ms:g} ms by "
            "construction, so a paired p99 verdict is a guaranteed tie"
            if SCOPE_DECLARED_POINT not in scopes else
            f"A slot AT its own knee sits at roughly {slo_ms:g} ms by construction, "
            "so a paired p99 verdict between two such slots is a guaranteed tie -- "
            "and a slot with NO knee sits below the promise at its ladder ceiling, "
            "where a p99 verdict would report that ceiling's spare headroom rather "
            "than either stack's cost"
        )
        parts.append(
            f"The paired metric is COST, not p99. {tie_clause}; the p99 "
            "conformance readout above is where the latency "
            "promise is evidenced. Blocking still works exactly as before: "
            "`slot_order_for_block` randomises which member of a block runs first, so "
            "the two probes are adjacent in time in a randomised order, and that "
            "controls the nuisance factor REGARDLESS of the rate each slot runs at. "
            "What changed is the treatment definition -- no longer \"language at a "
            "fixed rate\" but \"language at its own SLO-crossing rate\", which is "
            "what the headline metric is defined as.\n"
        )
        # The scope COUNT is read off what rendered. Two scopes are the design,
        # but a run whose slots share a knee has no second rate to contrast at
        # and there is then only one -- a fixed "TWO SCOPES" above a one-scope
        # table is the overclaim this paragraph is warning the reader against.
        scope_prose = {
            SCOPE_OWN_KNEE:
                "`own_knee` is the headline the metric is defined as: each slot at "
                "its own crossing rate.",
            SCOPE_DECLARED_POINT:
                "`declared_operating_point` is that same comparison at the rates the "
                "confirm stage DECLARED, published under a different label because at "
                "least one slot has no fitted knee -- its ladder reached the rung "
                "ceiling without crossing the SLO, so its rate is that ceiling, a "
                "lower bound. This is NOT cost at the latency promise for that slot; "
                "the no-crossing bullet above names it and the resource scope line "
                "gives the reason per slot.",
            SCOPE_COMMON_RATE:
                "`common_rate` adds the faster slot at the SLOWER slot's knee INSIDE "
                "the same blocks, because per-request cost is not rate-independent, "
                "so an own-knee cost comparison confounds language with position on "
                "the cost-vs-rate curve.",
        }
        parts.append(
            f"SCOPES ARE NEVER MERGED, and this run carries {len(scopes)}: "
            + ", ".join(f"`{sc}`" for sc in scopes) + ". "
            + " ".join(scope_prose[sc] for sc in scopes if sc in scope_prose)
            + (
                " Both are published because they answer different questions and "
                "choosing one for the reader is a choice they cannot audit; the "
                "cost-vs-rate curve above is the third view of the same fact.\n"
                if len(scopes) > 1 else
                " Every slot in this run confirmed at the same rate, so there is no "
                "second operating point to contrast at and no `common_rate` verdict "
                "exists.\n"
            )
        )
        parts.append(
            "The interval is the paired-t interval on the per-block differences at its "
            "stated confidence level; the equivalence margin is the separate, "
            "pre-registered +-band the interval is compared against. The verdict word "
            "is that equivalence decision (interval not contained in +-margin -> "
            "direction of the mean difference); an interval that ALSO spans 0% is not "
            "a significant difference at its confidence level, so read the interval, "
            "not only the word. `$/M` is the CPU column priced, not independent "
            "evidence, so its verdict is the CPU verdict in dollars.\n"
        )
        parts.append(_paired_table(paired))
        parts.extend(_paired_block_sentences(paired))
    else:
        parts.append(
            "_Not available in this run: no completed confirm-stage paired blocks "
            f"(the paired difference needs >=2 RCB blocks with {names} both warm)._\n"
        )
    missing = [c.cell_id for c in cells if not c.paired_verdicts]
    if missing and paired:
        parts.append(
            "\nNo paired verdict for: " + ", ".join(missing) +
            "; a block contributes a pair only when EVERY slot is "
            "measurement-grade in it, and in the cell(s) listed one slot has no warm "
            "confirm window at all (see the posture note below).\n"
        )

    res_cells = [c for c in cells if c.resource and c.resource.available]
    # The heading names the cgroups this section actually read. PostgreSQL's
    # co-sample is optional per window (`_window_rows`), so a run without it
    # reports a SUT-only section rather than a heading promising a DB column
    # that every row below says is `not measured`.
    heading_scope = "SUT + PostgreSQL" if any(
        lr.db_n_windows for c in res_cells for lr in c.resource.by_slot.values()
    ) else "SUT"
    parts.append(
        "\n## Resource cost at the operating rate "
        f"(cgroup v2 ground truth: {heading_scope})\n"
    )
    if res_cells:
        rates = sorted({r for c in res_cells for r in c.resource.operating_rates})
        rates_s = ", ".join(f"{r:,}" for r in rates)
        parts.append(
            f"Steady-state SUT cgroup CPU + memory per serviced request at the offered "
            f"rate(s) {rates_s} rps, aggregated over "
            f"the confirm + soak probe windows. {_resource_scope_clause(res_cells)} "
            f"Under-warmed / gate-excluded windows are "
            f"EXCLUDED, the same exclusion the knee fit applies; the counts used vs "
            f"excluded are a row of the table. `{slot_set.ratio_label()}` > 1 means "
            f"{_slot_label(slot_set.subject)} spends more. "
            + _operating_point_clause(rate_dep)
        )
        parts.append(
            "THE AGGREGATION RULE: `CPU-ms / request` is REQUEST-WEIGHTED: the total "
            "cgroup CPU delta of the windows in the roll-up divided by the total "
            "requests they serviced, so the request count in the table really is its "
            "denominator. The memory rows are plain means over the same windows (a "
            "memory level is not a per-request quantity, so there is nothing to weight "
            "it by).\n"
        )
        # The caveats print once, beside whichever DB content comes first: the
        # ladder attribution table above, or this section when the ladder has
        # no rungs with a DB sample.
        if any(lr.db_n_windows for c in res_cells if c.resource
               for lr in c.resource.by_slot.values()):
            if not _db_cpu_rows(cells):
                parts.extend(_db_cpu_caveats(slot_set))
        else:
            parts.append(_DB_NOT_MEASURED_LINE)
        parts.extend(_stage_mix_sentences(res_cells))
        parts.append(_resource_table(res_cells))
        notes = _posture_notes(res_cells)
        if notes:
            parts.append("\n" + "\n".join(notes) + "\n")
        parts.append(
            "\n`anon memory p99` is the p99 of `memory.stat anon` sampled inside the "
            "measurement windows. `cgroup memory.peak` is the kernel's high-water mark "
            "of `memory.current` over the CONTAINER'S WHOLE LIFETIME: anonymous memory "
            "plus page cache plus kernel memory, including warmup, so it is not an "
            "in-window RSS figure and is not comparable to the anon p99 above it.\n"
        )
        pool_bits = []
        for c in res_cells:
            pool_bits.append(
                f"{c.cell_id}: " + ", ".join(
                    f"{_slot_label(sid)} {lr.db_pool_total}/{lr.db_pool_max}"
                    for sid, lr in c.resource.by_slot.items()
                )
            )
        parts.append(
            "\nPool warmth at the end of warmup (open connections / configured max): "
            + "; ".join(pool_bits) + ".\n"
        )
        n_windows = sum(
            lr.n_windows for c in res_cells for lr in c.resource.by_slot.values()
        )
        # The two swap verdicts' scopes are READ OFF the windows behind them.
        cur_scope = _swap_scope(res_cells, "n_windows_without_swap_current",
                                "n_swap_current_windows_with_db")
        peak_scope = _swap_scope(res_cells, "n_windows_without_swap_peak",
                                 "n_swap_peak_windows_with_db")
        db_in_scope = bool(cur_scope.n_with_db or peak_scope.n_with_db)
        db_clause = (
            " -- and a swapping database would move the DB CPU-ms/request column, "
            "which is why the swap verdicts span PostgreSQL's window as well as "
            "the SUT's."
            if db_in_scope else
            ". The DB CPU-ms/request column has no swap verdict here: no window in "
            "scope carried a PostgreSQL co-sample."
        )
        parts.append(
            f"\nResource-accounting validity, over the {n_windows} aggregated "
            f"confirm + soak windows in the table above (NOT over the ladder windows, "
            f"which are not aggregated here): CPU throttle counters "
            + _counter_phrase(res_cells, "throttle_clean", "n_windows_without_counters",
                              "zero")
            + "; cgroup OOM events "
            + _counter_phrase(res_cells, "oom_clean", "n_windows_without_oom_counter",
                              "zero")
            # Two swap phrases, not one: `memory.swap.peak` does not exist on
            # every kernel, and one merged phrase asserted a lifetime-high-water
            # read on trees that only ever carried the edge level. Each phrase's
            # SCOPE is derived too (`_SwapScope`), because the PostgreSQL
            # co-sample is optional per window.
            + f"; `memory.swap.current` ({cur_scope.phrase()}) "
            + _counter_phrase(res_cells, "swap_current_clean",
                              "n_windows_without_swap_current",
                              "zero at every window edge")
            + f"; `memory.swap.peak` ({peak_scope.phrase()}) "
            + _counter_phrase(res_cells, "swap_peak_clean",
                              "n_windows_without_swap_peak",
                              "zero in the kernel's lifetime high-water mark")
            + ". A throttled window would deflate CPU-ms/request; an OOM window would "
            "invalidate the memory figures; a window that swapped would UNDERSTATE "
            "them, because a swapped-out anon page leaves `memory.stat anon`"
            + db_clause + " "
            + _container_ceiling_sentence(
                _load_manifest(run_id),
                covered=sorted(set(cur_scope.labels) | set(peak_scope.labels)),
            )
        )
        served = "; ".join(
            f"{c.cell_id}: " + ", ".join(
                f"{_slot_label(sid)} {lr.requests} requests ({lr.non_2xx} non-2xx) "
                f"over {lr.cpu_ms_total:.0f} CPU-ms"
                for sid, lr in c.resource.by_slot.items()
            )
            for c in res_cells
        )
        parts.append(
            f"\nThe two quantities the CPU rows divide: total cgroup CPU over the "
            f"aggregated windows, and the requests those windows serviced: {served}. "
            f"Dividing the second into the first reproduces the `CPU-ms / request` row "
            f"exactly; that is the aggregation rule stated above.\n"
        )
    else:
        parts.append(
            "_Not available in this run: no SUT cgroup-v2 resource summaries on "
            "disk (Windows/smoke run, or a run whose per-window cells tree was not "
            "retained)._\n"
        )

    parts.extend(_gc_pool_section(run_id, slot_set))
    parts.extend(_cost_section(res_cells, slot_set, rate_dep))

    parts.append("\n## Not captured in this run (scope)\n")
    parts.extend(_scope_bullets(led, res_cells))
    return "\n".join(parts) + "\n"


def _stationarity_table(cells: list[CellAnalysis]) -> str:
    rows = []
    for c in cells:
        s = c.stationarity
        if not s.get("available"):
            verdict = "not available in this run"
        else:
            n_fail = sum(1 for v in s["per_probe_fails"].values() if v)
            verdict = (
                f"{n_fail}/{s['n_series']} series non-stationary"
                if s["any_fail"] else f"all {s['n_series']} series stationary"
            )
        rows.append({"cell": c.cell_id, "stationarity": verdict})
    return _md(rows)


def _container_placement_bullet(manifest) -> str:
    """The manifest's container-cpuset verdict, IN THE REPORT.

    `_stamp_topology` computed `matches` and nothing downstream ever read it, so
    a container that ran unpinned across every logical core while the run
    recorded a 6-core budget was a `false` in a JSON file no reader opens. The
    verdict has to be said where the numbers are said, or it is not a verdict.
    """
    topo = getattr(manifest, "topology", None)
    rows = getattr(topo, "containers", None) or {}
    if not rows:
        return (
            "- **Container placement.** Not recorded this run (no cgroup window "
            "summary carried a `cpuset.cpus.effective` read), so the CPU and memory "
            "figures above rest on the compose overlay's request rather than on the "
            "cpuset the kernel applied.\n"
        )
    wrong = [f"`{label}` on {r.cpuset_effective} (compose asked for {r.expected_cpuset})"
             for label, r in sorted(rows.items()) if r.matches is False]
    # "Unchecked" is two different rows and the distinction is the whole
    # sentence: a cgroup with NO declared expectation is the dockerd/containerd
    # case the wording is for, while one whose expectation is populated and
    # whose cpuset did not read is an unverified container being counted as
    # one of them.
    no_expectation = sorted(
        label for label, r in rows.items()
        if r.matches is None and not r.expected_cpuset
    )
    unreadable = sorted(
        label for label, r in rows.items()
        if r.matches is None and r.expected_cpuset
    )
    n_checked = len(rows) - len(no_expectation) - len(unreadable)
    tail = (
        (f" {', '.join(no_expectation)} declare no compose cpuset (pinned by the "
         "systemd drop-in instead)." if no_expectation else "")
        + (f" {', '.join(unreadable)} were asked for a cpuset that never read back, "
           "so their placement is NOT evidenced." if unreadable else "")
    )
    if wrong:
        return (
            "- **Container placement DIVERGED from the resource ledger.** "
            + "; ".join(wrong)
            + ". Every CPU-ms- and memory-per-request figure above was measured "
            "against a core budget the container did not have."
            + tail + "\n"
        )
    if not n_checked:
        # Never lead with "matches" on a count of zero: that bold sentence is
        # the verdict a reader takes away, and over zero checked rows it is a
        # claim about nothing.
        return (
            "- **Container placement NOT verified.** No sampled cgroup read back a "
            "`cpuset.cpus.effective` to compare against what "
            "infra/compose.bench.yaml asked for, so the CPU and memory figures "
            "above rest on the compose overlay's request rather than on the cpuset "
            "the kernel applied." + tail + "\n"
        )
    return (
        f"- **Container placement matches the resource ledger.** "
        f"{n_checked} sampled cgroup(s) read back the "
        "`cpuset.cpus.effective` infra/compose.bench.yaml asked for."
        + tail + "\n"
    )


def _runtime_stats_scan(run_id: str) -> dict:
    """What `*.runtime_stats.summary.json` this run actually left on disk.

    The scope bullet used to assert that both windows are flushed and that each
    summary carries a MEASURED cost bound. Neither is guaranteed:
    `runtimestats.PollWindow.summary_dict` writes `source: not_measured` with
    every field null whenever no poll succeeded, and a tree with no poller
    artifacts at all carries no summary to make any claim about. The counts are
    read here so the sentence describes the files rather than the intent.

    `n_with_edges`, `n_with_deltas` and `n_traces` are counted for the same
    reason and are three DIFFERENT counts, not one restated: a window with no
    successful poll has neither edge and writes no `.jsonl`
    (`PollWindow.write` writes the trace only `if self.snapshots`), and a window
    with exactly ONE successful poll has both edges but an EMPTY delta block
    (`PollWindow.deltas` returns `{}` when start is end). A single sentence
    claiming edges, deltas and a trace for every summary is false in two
    different ways, each reachable.

    `source == runtimestats.NOT_MEASURED` is the writer's own verdict, imported
    rather than re-derived from `n_polls`, so the two cannot drift apart.
    """
    run_dir = paths.run_dir(run_id)
    n_by_phase: dict[str, int] = {}
    n_measured = n_unmeasured = n_with_cost = 0
    n_with_edges = n_with_deltas = 0
    cost_ms = 0.0
    for path in sorted(run_dir.rglob("*.runtime_stats.summary.json")):
        doc = _read_json(path)
        if not doc:
            continue
        phase = str(doc.get("phase") or "unlabelled")
        n_by_phase[phase] = n_by_phase.get(phase, 0) + 1
        if doc.get("source") == _RS_NOT_MEASURED:
            n_unmeasured += 1
        else:
            n_measured += 1
        if doc.get("start") is not None and doc.get("end") is not None:
            n_with_edges += 1
        if doc.get("deltas"):
            n_with_deltas += 1
        total = (doc.get("poll_cost") or {}).get("round_trip_ms_total")
        if total is not None:
            cost_ms += float(total)
            n_with_cost += 1
    return {
        "n": n_measured + n_unmeasured,
        "by_phase": n_by_phase,
        "n_measured": n_measured,
        "n_unmeasured": n_unmeasured,
        "n_with_edges": n_with_edges,
        "n_with_deltas": n_with_deltas,
        "n_traces": sum(1 for _ in run_dir.rglob("*.runtime_stats.jsonl")),
        "n_with_cost": n_with_cost,
        "cost_ms": cost_ms,
    }


# The vCPU-hour price behind the $/M-request figure. It is a CHOSEN CONSTANT of
# this harness, not a vendor quote and not a measurement: the figure scales
# linearly with it, so the rendered note states the price and tells the reader
# to rescale. PROGRESS commits to publishing the dollar figure "with the vCPU
# price stated"; an undisclosed price is what would make it a claim instead of
# an illustration.
VCPU_HOUR_USD_ENV = "LEDGERBENCH_VCPU_HOUR_USD"
_VCPU_HOUR_USD_DEFAULT = 0.04


def _vcpu_hour_usd() -> tuple[float, str]:
    """(price, where it came from). A bad override is reported, never silent."""
    raw = os.environ.get(VCPU_HOUR_USD_ENV, "").strip()
    if not raw:
        return _VCPU_HOUR_USD_DEFAULT, "`analyze._VCPU_HOUR_USD_DEFAULT`"
    try:
        value = float(raw)
    except ValueError:
        print(
            f"WARNING: {VCPU_HOUR_USD_ENV}={raw!r} is not a number; falling back "
            f"to {_VCPU_HOUR_USD_DEFAULT}",
            file=sys.stderr,
        )
        return _VCPU_HOUR_USD_DEFAULT, (
            f"`analyze._VCPU_HOUR_USD_DEFAULT` ({VCPU_HOUR_USD_ENV}={raw!r} was "
            f"not a number and was ignored)"
        )
    return value, f"the `{VCPU_HOUR_USD_ENV}` environment variable"


def _cost_section(
    res_cells: list["CellAnalysis"], slot_set: registry.SlotSet, rd: _RateDependence,
) -> list[str]:
    """The illustrative $/M-request table, one per cell with a roll-up.

    The scope section used to carry this as a permanent absence -- "Not
    published as a dollar figure: it would only scale the CPU ratio above by a
    chosen cloud vCPU price, and that ratio is itself rate-dependent". Both
    halves were reasons to DISCLOSE, not to withhold: the price is chosen here
    and printed, and whether the ratio moves with rate is now this run's own
    measurement rather than round 1's finding. PROGRESS lists the dollar figure
    as in scope for exactly that reason.
    """
    from analysis.report import CostInputs, cost_table

    rows = [
        (c, {sid: lr.cpu_ms_per_req for sid, lr in c.resource.by_slot.items()
             if lr.cpu_ms_per_req == lr.cpu_ms_per_req})       # drop NaN
        for c in res_cells
    ]
    rows = [(c, m) for c, m in rows if m]
    if not rows:
        return []
    price, origin = _vcpu_hour_usd()
    labels = {sid: _slot_label(sid) for sid in slot_set.slots}
    parts = ["\n## Illustrative $/M-request cost\n"]
    parts.append(
        f"**Illustrative, and the price is an assumption.** ${price:.4f} per "
        f"vCPU-hour is a constant chosen by this harness (from {origin}), not a "
        f"vendor quote and not a measurement. The figure is linear in it, so a "
        f"reader with a different price rescales by the ratio of the two; the "
        f"SLOT-TO-SLOT ratio does not depend on the price at all. The only "
        f"measured input is the request-weighted `CPU-ms / request` from the "
        f"roll-up above, which means every caveat on that number -- the windows "
        f"it excludes, the cgroup it is taken from, the apparatus CPU it leaves "
        f"out -- is a caveat on this one. "
        + ("Whether per-request CPU is rate-independent is NOT ESTABLISHED by "
           "this run, so these dollars belong to the operating rate(s) the "
           "roll-up used and to no other.\n" if not rd.established else
           f"Per-request CPU moved across this run's ladder rungs (largest "
           f"per-slot coefficient of variation {rd.max_cv_pct:.1f}%), so these "
           f"dollars belong to the operating rate(s) the roll-up used and to no "
           f"other.\n" if rd.moves else
           f"Per-request CPU did not move measurably over the ladder rungs each "
           f"slot was measured at (the largest coefficient of variation spans "
           f"{rd.cv_n_rungs} rungs), but these dollars are still taken at the "
           f"operating rate(s) the roll-up used.\n")
    )
    for c, per_slot in rows:
        rates_s = ", ".join(f"{r}" for r in c.resource.operating_rates)
        parts.append(f"\n**{c.cell_id}** at {rates_s} rps offered\n")
        parts.append(cost_table(CostInputs(
            vcpu_hour_usd=price, cpu_ms_per_req_by_slot=per_slot, labels=labels,
        )))
    return parts


_GC_GENERATIONS = ("gen0", "gen1", "gen2")


def _gc_pool_rows(run_id: str, slot_set: registry.SlotSet) -> list[dict]:
    """Per-slot GC + pool activity, summed from the flushed window summaries.

    R1-B3: round 1 polled these counters and discarded them, so its artifacts
    carry no GC collection counts or pause totals at all and the only GC
    evidence a reader got was a pass/fail warmup gate. W17 writes them per
    window; this is the reader that puts them in the report, which is what
    "flushed" was for.

    SCOPE, and it is not the roll-up's: every `phase: measured` summary on disk
    that carries a non-empty delta block, across ladder, confirm and soak, at
    every rate. A summary records no warmup verdict, so under-warmed and
    gate-excluded windows CANNOT be dropped here and are not -- the rendered
    prose says so rather than letting the numbers read as gated. Warmup-phase
    summaries are excluded because a warmup window's GC is the transient this
    benchmark warms past.

    Per-generation counts are summed only over windows where that generation is
    non-null: `gc_collections_gen2` is nullable for the JVM (G1's old-gen bean
    counts Full GC only), and a null summed as zero would publish "no old-gen
    collections" for a collector that does not report them. `generations
    summed` names which ones the total covers, per the writer's own
    `gc_collections_generations_summed`.
    """
    per_slot: dict[str, dict] = {}
    for path in sorted(paths.run_dir(run_id).rglob("*.runtime_stats.summary.json")):
        doc = _read_json(path)
        if not doc or doc.get("phase") != _RS_PHASE_MEASURED:
            continue
        deltas = doc.get("deltas") or {}
        if not deltas:
            continue
        sid = str(doc.get("slot") or "")
        if sid not in slot_set.slots:
            continue
        acc = per_slot.setdefault(sid, {
            "windows": 0, "gens": {g: [0, 0] for g in _GC_GENERATIONS},
            "total": [0, 0], "pause_s": [0.0, 0], "alloc": [0, 0], "summed": set(),
            "heap_mb": None, "pool_total": None, "pool_max": None,
        })
        acc["windows"] += 1
        for g in _GC_GENERATIONS:
            v = deltas.get(f"gc_collections_{g}")
            if v is not None:
                acc["gens"][g][0] += int(v)
                acc["gens"][g][1] += 1
        # `total` and `pause_s` count the windows that REPORTED them, for the
        # reason the per-generation loop above does: both are null for the JVM
        # (all three generations null makes the summed total null, and
        # `pause_total_seconds` is null under ZGC because the bean is
        # millisecond-granular). Summed with `or 0` they rendered as a measured
        # `0` and `0.000` -- "this collector never collected and never paused" --
        # which is R1-B1's shape: a published figure with no evidence under it.
        for key, field_name in (("gc_collections_total", "total"),
                                ("gc_pause_total_seconds", "pause_s")):
            v = deltas.get(key)
            if v is not None:
                acc[field_name][0] += v
                acc[field_name][1] += 1
        # Same rule as `total`/`pause_s` directly above: a null is not a
        # measured zero.
        v = deltas.get("gc_allocated_total_bytes")
        if v is not None:
            acc["alloc"][0] += int(v)
            acc["alloc"][1] += 1
        acc["summed"].update(deltas.get("gc_collections_generations_summed") or [])
        levels = doc.get("levels") or {}
        for key, field_name, scale in (
            ("gc_heap_committed_bytes", "heap_mb", 1 << 20),
            ("db_pool_total", "pool_total", 1),
            ("db_pool_max", "pool_max", 1),
        ):
            v = (levels.get(key) or {}).get("max")
            if v is not None:
                cur = acc[field_name]
                acc[field_name] = v / scale if cur is None else max(cur, v / scale)

    rows = []
    for sid in slot_set.slots:
        acc = per_slot.get(sid)
        if not acc:
            continue
        row = {
            "slot": _slot_label(sid),
            "windows": acc["windows"],
            "generations summed": ", ".join(sorted(acc["summed"])) or "none named",
        }
        for g in _GC_GENERATIONS:
            total, n = acc["gens"][g]
            row[f"collections {g}"] = (
                f"{total:,} ({n}w)" if n else "not reported"
            )
        total, n_total = acc["total"]
        row["collections total"] = f"{int(total):,} ({n_total}w)" if n_total else "not reported"
        pause_s, n_pause = acc["pause_s"]
        row["GC pause total (s)"] = f"{pause_s:.3f} ({n_pause}w)" if n_pause else "not reported"
        alloc, n_alloc = acc["alloc"]
        row["allocated (MB)"] = (
            f"{alloc / (1 << 20):,.0f} ({n_alloc}w)" if n_alloc else "not reported"
        )
        row["heap committed max (MB)"] = (
            f"{acc['heap_mb']:,.0f}" if acc["heap_mb"] is not None else "not reported"
        )
        row["db pool max open / configured"] = (
            f"{acc['pool_total']:g}/{acc['pool_max']:g}"
            if acc["pool_total"] is not None and acc["pool_max"] is not None
            else "not reported"
        )
        rows.append(row)
    return rows


def _gc_pool_section(run_id: str, slot_set: registry.SlotSet) -> list[str]:
    from analysis.report import gc_pool_table

    rows = _gc_pool_rows(run_id, slot_set)
    parts = ["\n## GC and pool activity (`/runtime-stats`, flushed per window)\n"]
    if not rows:
        parts.append(
            "_Not available in this run: no `phase: measured` "
            "`*.runtime_stats.summary.json` on disk carries a non-empty "
            "end-minus-start delta block, so this run has no GC evidence at all "
            "(the round-1 hole R1-B3 named)._\n"
        )
        return parts
    n_windows = sum(int(r["windows"]) for r in rows)
    parts.append(
        f"Summed over the {n_windows} `phase: measured` window summaries on disk "
        f"that carry a non-empty delta block, across EVERY stage and rate -- "
        f"ladder, confirm and soak alike. **This is not the roll-up's window "
        f"set.** A `/runtime-stats` summary records no warmup verdict, so "
        f"under-warmed and gate-excluded windows cannot be dropped here and are "
        f"not; read these as run-total activity, never as a gated per-request "
        f"figure. Every counter is a process-lifetime total differenced across "
        f"that window's OWN edges, so summing them adds per-window increments "
        f"and never adds a lifetime total twice. `(Nw)` after a "
        f"per-generation count is how many of those windows reported that "
        f"generation at all: `generations summed` is the writer's own list of "
        f"which ones `collections total` covers, and a collector that does not "
        f"expose a generation reports `not reported` rather than 0. `collections "
        f"total` and `GC pause total` carry the same `(Nw)` and the same "
        f"`not reported`: the JVM publishes a present null for every generation "
        f"(so the summed total is null too) and for `pause_total_seconds` under "
        f"ZGC, and a null summed as zero would publish a collector that never "
        f"collected and never paused.\n"
    )
    parts.append(gc_pool_table(rows))
    return parts


def _runtime_stats_bullet(run_id: str) -> str:
    """The polling-cost scope bullet, derived from the scan above.

    Nothing in the shared head may describe what the poller DID: the
    no-summaries branch is exactly the case in which the run has no evidence
    that it ran at all, and a head asserting "the 1 Hz poller runs across warmup
    as well as the measured window" three clauses before "this run left NO
    summaries on disk" is the contradiction this function exists to avoid. The
    head therefore carries only the structural disclosure -- polling happens
    inside the measured window and its requests are in no denominator -- which
    is true of the CONFIGURATION and independent of the artifacts.
    """
    scan = _runtime_stats_scan(run_id)
    head = (
        "- **/runtime-stats polling is inside the measured window, and its cost "
        "has no denominator.** "
    )
    # The overstatement is structural (the poller runs inside the window and its
    # requests are in no denominator); only its SIZE depends on the artifacts,
    # so the clause is shared and only its last words differ.
    overstated = (
        "The polls' SUT-side CPU lands in the cgroup delta, while `/runtime-stats` "
        "is not one of the frozen `endpoint_calls` routes and is not in vegeta's "
        "request count either -- so per-request CPU is overstated by "
    )
    if not scan["n"]:
        return (
            head
            + "The 1 Hz poller is CONFIGURED to run across warmup as well as the "
            "measured window (`runtimestats.StatsPoller`), but this run left NO "
            "`*.runtime_stats.summary.json` on disk, so no window's polling was "
            "flushed, nothing here quantifies what it cost, and this run "
            "evidences no poll at all. "
            + overstated + "an amount this run does not measure.\n"
        )
    phases_s = ", ".join(
        f"{n} {ph}" for ph, n in sorted(scan["by_phase"].items())
    )
    if scan["n_with_cost"]:
        cost_s = (
            f"{scan['n_with_cost']} of them carry a MEASURED upper bound on that "
            f"cost (`poll_cost.round_trip_ms_total`, the client-side round trip), "
            f"totalling {scan['cost_ms']:,.0f} ms across the run. "
        )
    else:
        cost_s = (
            "None of them carries a round-trip bound "
            "(`poll_cost.round_trip_ms_total` is null throughout), so the cost is "
            "not bounded by anything this run measured. "
        )
    # Three counts, never one claim: edges, a non-empty delta block, and a trace
    # file are independently present or absent per window (see the scan's
    # docstring), so each is REPORTED rather than asserted over all of them.
    content_s = (
        f"{scan['n_with_edges']} of them carry both window edges and "
        f"{scan['n_with_deltas']} carry a non-empty end-minus-start delta block "
        f"(a window with a single successful poll has edges but no delta, since "
        f"a delta needs two); {scan['n_traces']} per-poll `.jsonl` traces sit "
        f"beside them (a window with no successful poll writes none). "
    )
    return (
        head
        + f"This run flushed {scan['n']} window summaries "
        f"(`<slot>[.warmup].runtime_stats.summary.json`): {phases_s}. "
        + content_s
        + (
            f"{scan['n_unmeasured']} of them record `source: not_measured` -- no "
            f"poll succeeded in that window, so every field is null and NOT a "
            f"measured zero. "
            if scan["n_unmeasured"] else
            "Every one of them records at least one successful poll. "
        )
        + overstated + "that much. " + cost_s
        + "The alternative, polling only at the boundaries, would lose the "
        "within-window heap trace that decides the heap-policy question.\n"
    )


def _seeded_corpus_phrase(ws) -> str:
    """The corpus clause -- `from N seeded invoices / M seeded customers` -- or
    a labelled absence.

    Read off `manifest.working_set`, which `phases._record_working_set` fills
    from `spec/workload.yaml seeding.scales[<the run's seed_scale>]` -- the same
    table `vegeta.ScaleRanges.from_workload` draws the ids from, so this is the
    corpus the targets were actually generated against.

    `spec/workload.yaml keyspaces` is deliberately NOT read here. It used to
    carry a `seeded_range` holding the FULL-scale range whatever the run seeded,
    so quoting it made a smoke-tier report claim 5,000,000 invoices against the
    5,000 the run had -- 1000x, in the sentence whose whole job is quantifying
    cache residency, and in the direction that understates it. That key is now
    deleted from the spec; the block that remains declares the DRAW, which
    `vegeta.assert_keyspace_contract` checks and this sentence does not use.
    """
    inv = getattr(ws, "seeded_invoices", None)
    cust = getattr(ws, "seeded_customers", None)
    if inv is None or cust is None:
        return (
            "from a seeded corpus this run's manifest does not record "
            "(`working_set.seeded_invoices` / `.seeded_customers` are null)"
        )
    return f"from {inv:,} seeded invoices / {cust:,} seeded customers"


def _body_pool_phrase(ws) -> str:
    """The create-body pool, and the ONE distributional cost of drawing from it.

    A create body is picked uniformly from a pool whose customer ids were
    themselves Zipf draws, so the create path's customer_id distribution is that
    pool's empirical sample rather than a fresh draw per request: identical on
    the head, thinner in the tail. That is a real asymmetry against the write
    path and it is stated here because a reader meets the report, not the
    manifest. The READ path is URL-only and unaffected -- which is what R1-A7
    was about.
    """
    n_create = getattr(ws, "body_pool_create_bodies", None)
    n_quote = getattr(ws, "body_pool_quote_bodies", None)
    if n_create is None or n_quote is None:
        return (
            "a size this run's manifest does not record "
            "(`working_set.body_pool_create_bodies` / `.body_pool_quote_bodies` "
            "are null)"
        )
    # The two counts have DIFFERENT provenance and the sentence has to say so.
    # `body_pool_create_bodies` is DERIVED -- `vegeta.body_pool_sizes` applies
    # the mix shares to the working-set dial -- while
    # `body_pool_distinct_customer_ids` is MEASURED, counted off the bodies on
    # disk. Rendering them in one clause as "N create bodies over M distinct
    # customer ids" reads as two facts about one measurement, and the gap
    # between them IS the disclosure this sentence exists to make.
    distinct = getattr(ws, "body_pool_distinct_customer_ids", None)
    tail = (
        f", counted off the pool on disk to hold {distinct:,} distinct customer "
        "ids" if distinct else
        ", over a distinct-customer count this run did not record"
    )
    return (
        f"{n_create:,} create bodies sized from the mix{tail}, and {n_quote:,} "
        f"quote bodies. Bodies "
        "carry no URL, so one pool serves both slots and both halves of an RCB "
        "block send byte-identical payloads. It costs one disclosure: a create "
        "body is drawn UNIFORMLY from that pool, so the create path's "
        "customer_id distribution is the pool's empirical Zipf sample rather "
        "than a fresh Zipf draw per request -- identical on the head, thinner "
        "in the tail. The read path is URL-only and unaffected"
    )


def _working_set_residency(ws, hit_rate: float | None) -> str:
    """The per-probe draw half of the residency bullet.

    Every number is measured from this run's own draws: the manifest's
    `working_set` is accumulated from the `<slot>.target_draw.json` each probe
    wrote (`phases._record_working_set`). The only spec value is the
    pre-registered `cache_construction.target_hit_rate`, quoted as the
    construction target it is.

    There is no whole-file count to quote any more, and no `replay_period`:
    round 1 replayed one targets file and this function turned its cycle time
    into prose. W20 draws a stream per attack, so the quantity is per probe and
    the spread across probes is what a reader needs -- a ladder spans a 20x
    range of offered rates, so one number would describe no probe.
    """
    spread = getattr(ws, "distinct_invoice_ids_per_probe", None)
    requests = getattr(ws, "requests_drawn_per_probe", None)
    if not spread or not spread.n or not requests or not requests.n:
        return (
            "The ids the measured windows named are NOT recorded in this run's "
            "manifest (`working_set.distinct_invoice_ids_per_probe` is empty), "
            "so the read path's residency is not quantified here."
        )
    seeded_inv = getattr(ws, "seeded_invoices", None)
    share = (
        f" ({100.0 * spread.median / seeded_inv:.3g}% of the seeded invoices at "
        "the median)"
        if seeded_inv else ""
    )
    cust = getattr(ws, "distinct_customer_ids_per_probe", None)
    cust_s = (
        f" and {_span(cust)} distinct customer ids"
        if cust is not None and cust.n else ""
    )
    out = (
        f"Each of the {spread.n} measured windows drew its OWN stream of "
        f"{_span(requests)} requests, naming {_span(spread)} distinct invoice "
        f"ids{share}{cust_s}. Those are PER-PROBE counts and this run publishes "
        f"no union across probes: two independent streams still overlap on the "
        f"Zipf head, so the counts may not be summed, and a union accumulated "
        f"in the finalizing process would come out short on exactly the runs "
        f"that resumed. "
    )
    return out + _residency_vs_ttl(ws, hit_rate)


def _span(spread) -> str:
    """`N` when a spread is one value, `min-max (median M)` otherwise."""
    if spread.min == spread.max:
        return f"{spread.min:,.0f}"
    return f"{spread.min:,.0f}-{spread.max:,.0f} (median {spread.median:,.0f})"


def _prewarm_phrase(ws, hit_rate: float | None) -> str:
    """What pre-warming the per-attack draw removes, and the part it CANNOT.

    This sentence used to assert that "nothing before a window populates the
    cache with the ids that window is about to request". That is false, and it
    was false for a structural reason the mechanism cannot fix: separate PCG64
    streams make the warmup's SEQUENCE independent of the measured window's --
    which is the whole of R1-A7, where all three attacks replayed one file's
    prefix and containment was 1.0 by construction -- but both streams are Zipf
    draws over the SAME keyspace, so the head appears in both by construction.
    `docs/p1-plan.md` 5.1 states exactly this about `hot_set_touched`, as the
    reason that gate would have been trivially true.

    So the claim is narrowed to what the draw achieves and the residual is
    PUBLISHED: `working_set.prewarm_containment_per_probe`, the request-weighted
    share of each window's invoice-id reads whose id an earlier attack of the
    same probe already drew, accumulated from the run's own draw artifacts. It
    is rendered next to the constructed target hit rate (a design input) and the
    realized Redis hit rate (the cache's own measurement), because a reader
    needs all three to interpret the read path.
    """
    spread = getattr(ws, "prewarm_containment_per_probe", None)
    achieved = (
        "The warmup, the head-of-window discard and the measured attack each "
        "draw a separate stream, so no attack replays the SEQUENCE another is "
        "about to consume -- which is what R1-A7 was, the warmup consuming the "
        "measured window's own prefix after a FLUSHALL"
    )
    limit = (
        " It does NOT mean a window opens cold: the streams are independent "
        "draws from the same Zipf keyspace, so the hot head appears in both by "
        "construction, and that residual is measured rather than asserted away."
    )
    if spread is None or not spread.n:
        return (
            f"{achieved}.{limit} This run's manifest records no measured share "
            "(`working_set.prewarm_containment_per_probe` is empty), so the "
            "residual is not quantified here."
        )
    implied = (
        f", against a constructed target hit rate of {hit_rate:.2f}"
        if hit_rate is not None else ""
    )
    windows = "window" if spread.n == 1 else "windows"
    return (
        f"{achieved}.{limit} Measured over {spread.n} {windows}, "
        f"{_pct_span(spread)} of a window's invoice-id READ REQUESTS name an id "
        f"an earlier attack of the same probe had already drawn{implied}. "
        "That is the read path's residency floor and it is a property of the "
        "workload, not of the harness."
    )


def _pct_span(spread) -> str:
    """A Spread of fractions as `N%` or `min-max% (median M%)`."""
    if spread.min == spread.max:
        return f"{100.0 * spread.min:.1f}%"
    return (
        f"{100.0 * spread.min:.1f}-{100.0 * spread.max:.1f}% "
        f"(median {100.0 * spread.median:.1f}%)"
    )


def _residency_vs_ttl(ws, hit_rate: float | None) -> str:
    """The cache TTL against the measured window -- the residency conclusion.

    Round 1's version compared the TTL against the seconds it took to replay the
    whole targets file, because the file WAS the request sequence and it cycled.
    Nothing replays now, so the comparison that remains is the honest one: can
    an entry populated inside a measured window still be there at the end of it,
    and how much of the window is populated before it opens. The second half is
    the part R1-A7 was about; it is a MEASURED fraction, not a construction
    argument (see `_prewarm_phrase`).
    """
    ttl = getattr(ws, "cache_ttl_seconds", None)
    window = getattr(ws, "measured_window_seconds", None)
    # Reads as a continuation of "WEAKER than the Zipf skew alone implies", so
    # it must not repeat the comparative -- the W20 text spliced " than the
    # constructed target hit rate of X implies" directly after "implies" and
    # rendered "implies than ... implies" in every soak-window report.
    implies = (
        f", and weaker than the constructed target hit rate of {hit_rate:.2f} "
        "implies" if hit_rate is not None else ""
    )
    independent = _prewarm_phrase(ws, hit_rate)
    if ttl is None:
        return (
            "The effective cache TTL was not established for this run "
            f"(`working_set.cache_ttl_source` = `{ws.cache_ttl_source}`), so "
            f"whether an entry outlives its window is not answered here. "
            f"{independent}"
        )
    if window is None:
        return (
            f"No measured-window length was recorded against the {ttl:,} s cache "
            "TTL (`working_set.measured_window_seconds` is null), so whether an "
            f"entry outlives its window is not answered here. {independent}"
        )
    if window < ttl:
        return (
            f"A measured window is {window:,} s against a {ttl:,} s cache TTL, so "
            f"an entry populated inside a window is still live at the end of it, "
            f"and within-window residency rests on the Zipf skew plus whatever "
            f"the window opened with. {independent}"
        )
    return (
        f"A measured window is {window:,} s against a {ttl:,} s cache TTL, so an "
        f"entry populated at the head of a window can expire before that window "
        f"ends: within-window residency is WEAKER than the Zipf skew alone "
        f"implies{implies}. {independent}"
    )


def _posture_waiver_bullet(res_cells: list[CellAnalysis]) -> str | None:
    """Warmup gates a posture was NOT held to, from the run's own probe records.

    Modeled on the unimplemented-gate bullet below and there for the same
    reason: a gate that stops being enforced has to be stated somewhere a reader
    will meet it, or its absence is invisible (R1-B1). The DECLARED waiver set
    -- `spec/slo.yaml warmup.gates_by_posture` merged with `phases.CELLS`
    `warmup_gates`, resolved by the SAME `warmup.gate_set_for_cell` the runner
    resolved with -- is cross-checked against what the probes RECORDED, and a
    disagreement is printed as INCONSISTENT rather than reconciled away.

    Returns None when nothing was waived and nothing recorded a waiver, which is
    this round's expected state: `spec/slo.yaml` declares no posture override
    and no cell declares `warmup_gates`.
    """
    try:
        warmup_cfg = load_slo().warmup
    except Exception:  # noqa: BLE001
        return None
    lines: list[str] = []
    for c in res_cells:
        cell = _cell_by_id(c.cell_id)
        declared = gate_set_for_cell(
            warmup_cfg, cell_id=c.cell_id,
            cell_gates=cell.warmup_gates if cell else None,
        ).waived
        recorded: dict[str, int] = {}
        for lr in c.resource.by_slot.values():
            for gate, n in lr.waived_gate_counts.items():
                recorded[gate] = recorded.get(gate, 0) + n
        if not declared and not recorded:
            continue
        # BOTH declaring layers, named separately: the cell's rationale and the
        # spec posture's are different artifacts with different review paths
        # (only the second is inside `config_hash`). Each layer requires its own
        # rationale, so "none declared" here means the waiver was RECORDED by
        # the probes without either layer declaring it -- which the INCONSISTENT
        # clause below then says outright.
        stated = []
        if cell and cell.gate_set_rationale:
            stated.append(f"cell: {cell.gate_set_rationale}")
        posture_why = warmup_cfg.posture_rationale(c.cell_id)
        if posture_why:
            stated.append(f"spec posture: {posture_why}")
        rationale = "; ".join(stated) or "none declared"
        lines.append(
            f"`{c.cell_id}`: declared "
            + (", ".join(f"`{g}` ({st})" for g, st in sorted(declared.items())) or "nothing")
            + ", recorded across this cell's probe windows: "
            + (", ".join(f"`{g}` {n}x" for g, n in sorted(recorded.items())) or "nothing")
            + f"; rationale: {rationale}"
        )
        if set(declared) != set(recorded):
            lines[-1] += (
                " -- INCONSISTENT: the declared waiver set and the set the probes "
                "recorded disagree; trust the per-probe records and fix the "
                "declaration."
            )
    if not lines:
        return None
    return (
        "- **Warmup gates a posture was NOT held to:** " + "; ".join(lines)
        + ". A waived gate is recorded per probe (`posture_waived` for a "
          "`spec/slo.yaml` waiver, `not_applicable` for a cell's own) with "
          "`passed: false` and `enforced: false`, so it can never read as a pass "
          "and never flags a probe -- but it also never checked anything, and the "
          "windows above are gated by fewer gates than the spec declares.\n"
    )


def _regate_bullet(d: dict) -> str:
    """The capacity re-gate's verdict, as a caveat the cells it covers inherit.

    Three verdicts, three different sentences, and every number in each is read
    off the record rather than restated: a PASS says what PostgreSQL actually
    ran at, a failure says which cells were already measured when it was
    reached, and NOT EVALUABLE says the question is open rather than answered.
    """
    verdict = str(d.get("verdict") or "")
    util = d.get("pg_utilization")
    ceiling = d.get("pg_utilization_ceiling")
    cells = ", ".join(str(c) for c in (d.get("cells_measured_before_regate") or []))
    rates = ", ".join(
        f"{_slot_label(sid)} {float(r):,.0f} rps"
        for sid, r in sorted((d.get("realized_rates_by_slot") or {}).items())
    )
    measured = (
        f"{util:.3f} of its {d.get('pg_cores')}-core budget against a "
        f"{ceiling:.3f} ceiling"
        if isinstance(util, (int, float)) and isinstance(ceiling, (int, float))
        else "an unmeasured utilization"
    )
    if verdict == capgate.Verdict.PASS_AT_REALIZED_RATES.value:
        return (
            "- **The capacity gate was re-run at the REALIZED rates, and passed.** "
            f"PostgreSQL ran at {measured} over the operating-point windows of "
            f"{cells or 'the first cell'}"
            + (f" ({rates})" if rates else "")
            + ". This is a MEASUREMENT of PostgreSQL under the actual cells, not "
            "a second projection: the pre-run gate rests on a PROJECTED knee, and "
            "round 1's did so while its cells then ran several times higher.\n"
        )
    if verdict == capgate.Verdict.DB_IMPOSED_AT_REALIZED_RATES.value:
        return (
            "- **The capacity gate FAILED when re-run at the realized rates.** "
            f"{d.get('detail') or 'headroom was not established at the measured rates'}. "
            f"The verdict is retroactive: {cells or 'the first cell'} had already "
            "been measured when it was reached"
            + (f" ({rates})" if rates else "")
            + ", so those numbers were taken against a database whose headroom AT "
            "THEIR OWN RATES is not established, and every cell NOT named there "
            "was either stopped or run under the operator opt-out. Read them with "
            "that caveat, not as clean cost-at-a-latency-promise figures.\n"
        )
    return (
        "- **The capacity gate could not be re-run at the realized rates.** "
        f"{d.get('detail') or 'no verdict was derivable'}. The pre-run gate's "
        "verdict stands on a PROJECTED mixed-workload knee; whether PostgreSQL "
        "had headroom at the rates these cells actually ran at is OPEN in this "
        "run, not answered.\n"
    )


def _scope_bullets(led: Ledger, res_cells: list[CellAnalysis]) -> list[str]:
    """The honest "what this run does NOT establish" list.

    Everything here is derived from the run's own artifacts (the ledger's phase
    records, the loaded spec, the aggregated status codes) rather than asserted,
    so the section cannot drift away from what actually happened.
    """
    bullets: list[str] = []

    # --- capacity gate: read the recorded verdict, do not assume it passed ---- #
    cap = led.phases.get("capacity_gate")
    verdict_json = _read_json(paths.run_dir(led.run_id) / "capacity-gate" / "verdict.json") or {}
    if cap is not None and cap.data:
        verdict = verdict_json.get("verdict") or cap.data.get("verdict")
        levers = verdict_json.get("levers_fired") or cap.data.get("levers_fired") or []
        lever_names = ", ".join(
            lv.get("name", "?") if isinstance(lv, dict) else str(lv) for lv in levers
        ) or "none"
        capacity = verdict_json.get("pg_knee_rps", cap.data.get("pg_knee_rps"))
        projected = verdict_json.get("projected_db_rps")
        headroom = verdict_json.get("headroom_factor")
        n_rungs = len(verdict_json.get("rungs") or [])
        realized = []
        for c in res_cells:
            for sid, lr in c.resource.by_slot.items():
                if lr.create_share_pct is not None:
                    realized.append(
                        f"{c.cell_id}/{_slot_label(sid)} {lr.create_share_pct:.2f}%"
                    )
        try:
            spec_share = load_workload().mix.create_invoice
        except Exception:  # noqa: BLE001 - spec unreadable (isolated test tree)
            spec_share = None
        if verdict and verdict != "pass":
            bullets.append(
                f"- **The capacity gate did NOT pass.** Recorded verdict `{verdict}`"
                + (
                    f": PG capacity {capacity:.0f} rps against a projected DB load of "
                    f"{projected:.0f} rps"
                    + (f" at headroom x{headroom:g}" if isinstance(headroom, (int, float)) else "")
                    if isinstance(capacity, (int, float)) and isinstance(projected, (int, float))
                    else ""
                )
                + f". Levers fired: {lever_names}. This is the exhausted-fallback branch: "
                f"every pre-registered lever was tried and the required DB headroom was "
                f"still not demonstrated, yet the cells phase proceeded and this report "
                f"was produced anyway; nothing gates on it. The gate's persisted verdict "
                f"records {n_rungs} create-only rung(s), so the capacity figure it rests "
                f"on is not a fitted knee. The lever values were held in the gate's own "
                f"verdict and were NOT written back into the workload the cells executed"
                + (
                    ": the realized create share over the aggregated windows is "
                    + ", ".join(realized)
                    + (f", i.e. the frozen spec mix ({spec_share}%), not the gate's "
                       f"lowered write-share lever." if spec_share is not None else ".")
                    if realized else "."
                )
                + " Read every rate in this report as measured against a DB whose "
                "headroom was never established.\n"
            )

    # --- capacity RE-gate: the same rule asked at the rates the cells ran at -- #
    # Read from the ledger's own phase record rather than the manifest, for the
    # same reason the bullet above reads `capacity_gate`: `analyze` runs against
    # a run directory, and the ledger is the file it is already trusting.
    rg = led.phases.get(REGATE_PHASE)
    if rg is not None and rg.data:
        bullets.append(_regate_bullet(rg.data))

    # --- validation checks this run's ledger recorded as deferred ---------- #
    # Reports what the ledger SAYS, and nothing about what will or will not be
    # measured elsewhere: a report bullet asserting that some check "never ran
    # and never will" is a standing negative declaration.
    val = led.phases.get("validation")
    if val is not None:
        deferred = [
            k for k in ("co", "logging_sink", "conn_sweep")
            if isinstance(val.data.get(k), dict) and "deferred" in val.data[k]
        ]
        if deferred:
            bullets.append(
                "- **Validation checks this run recorded as deferred.** The ledger "
                "carries " + ", ".join(f"`{k}`" for k in deferred)
                + " as deferred, so no number in this report is gated on them. The "
                "validation gate this run DID pass is cross-service response "
                "equivalence, and CO safety rests on the per-probe "
                "achieved-vs-offered evidence.\n"
            )

    # --- warmup gates a posture was not held to ------------------------------- #
    waiver_bullet = _posture_waiver_bullet(res_cells)
    if waiver_bullet:
        bullets.append(waiver_bullet)

    # --- warmup gates declared, applied, and enforcing nothing ---------------- #
    # The companion of the bullet below, and it exists because R1-B1 recurred one
    # layer up: a gate can now be IMPLEMENTED, evaluated on every probe, and
    # still decide nothing, because its own record carries `enforce: false`. The
    # per-probe records say so; nothing at run level did, while `spec/slo.yaml`
    # goes on declaring the gate `true` inside `config_hash`.
    non_enforcing: dict[str, int] = {}
    n_windows_total = 0
    for c in res_cells:
        for lr in c.resource.by_slot.values():
            n_windows_total += lr.n_windows
            for gate, n in lr.non_enforcing_gate_counts.items():
                non_enforcing[gate] = non_enforcing.get(gate, 0) + n
    if non_enforcing:
        bullets.append(
            "- **Warmup gates declared in `spec/slo.yaml`, implemented, and "
            "ENFORCING NOTHING in this run:** "
            + ", ".join(
                f"`{g}` ({n} of {n_windows_total} windows)"
                for g, n in sorted(non_enforcing.items())
            )
            + ". These gates were evaluated on those probes and their records "
            "carry `enforced: false`, so they could not flag a probe and are "
            "absent from the failed-gate counts rather than passed. The cause is "
            "in each `<slot>.warmup.json` record (`detail.flatness.reason`): a "
            "flatness gate whose quantile the per-second bucket cannot resolve "
            "at this run's warmup rate (`warmup.FlatnessGateSpec."
            "min_samples_per_second`). Read `spec/slo.yaml warmup.gates` with "
            "this bullet beside it: a probe recorded as warm was NOT held to "
            "them.\n"
        )

    # --- warmup gates declared in the spec but not implemented ---------------- #
    # Source of truth for "implemented" is the runner's own canonical registry
    # (`warmup.IMPLEMENTED_GATES`), imported rather than restated, so this bullet
    # cannot drift if a gate is implemented later. `per_endpoint_min_calls` is
    # NOT one of them: it is a separate spec key (`warmup.per_endpoint_min_calls`)
    # that the runner always evaluates, so it is named separately instead of
    # being smuggled into the declared-gate arithmetic.
    try:
        declared = set(load_slo().warmup.gates)
    except Exception:  # noqa: BLE001
        declared = set()
    unimplemented = sorted(declared - set(IMPLEMENTED_GATES))
    evaluated = sorted(declared & set(IMPLEMENTED_GATES))
    # Cross-check the claim against the run's own per-probe records: a gate the
    # registry calls unimplemented must never appear as a MEASURED failure.
    observed: set[str] = set()
    for c in res_cells:
        for lr in c.resource.by_slot.values():
            observed.update(lr.flagged_gate_counts)
    contradicted = sorted(observed & set(unimplemented))
    if unimplemented:
        n = len(unimplemented)
        bullets.append(
            "- **Warmup gates declared in `spec/slo.yaml` but NOT implemented:** "
            + ", ".join(f"`{g}`" for g in unimplemented)
            + f". The runner evaluates {', '.join(f'`{g}`' for g in evaluated)} "
            f"(`ledgerbench.warmup.IMPLEMENTED_GATES`) plus the separate "
            f"`per_endpoint_min_calls` spec key, so a probe recorded as warm was never "
            f"checked against the {n} gate(s) named above. Each one IS in every probe's "
            f"`<slot>.warmup.json`, carrying `status: not_implemented` and "
            f"`passed: false` so it can never read as a pass and never flags a probe; "
            f"this bullet states the same fact at run level. "
            + (
                "Cross-check against this run's own records: the gates its flagged "
                "windows report failing are "
                + (", ".join(f"`{g}`" for g in sorted(observed)) if observed else "none")
                + ", none of which is in the unimplemented list.\n"
                if not contradicted else
                "INCONSISTENT: this run's flagged windows report "
                + ", ".join(f"`{g}`" for g in contradicted)
                + " as failing, yet the registry says that gate is not implemented: "
                "trust the per-probe records over this bullet and fix the registry.\n"
            )
        )

    # --- effective working set / cache residency ------------------------------ #
    # The only spec value read here is the pre-registered construction TARGET;
    # every quantity describing what this run actually did comes off the
    # manifest's working_set (see `_seeded_corpus_phrase`).
    try:
        hit_rate = load_workload().cache_construction.target_hit_rate
    except Exception:  # noqa: BLE001
        hit_rate = None
    # GROUND TRUTH only: the manifest's working_set is accumulated from the
    # per-probe draw artifacts. There is no analyze-time fallback.
    # Under a per-probe draw there is nothing to re-resolve: the requests a
    # probe drew are a function of the rate it ran at, which only the run knows.
    manifest = _load_manifest(led.run_id)
    ws = getattr(manifest, "working_set", None)
    cache_stats = getattr(manifest, "cache_stats", None)
    measured_hit = None
    if isinstance(cache_stats, dict) and cache_stats.get("hit_rate") is not None:
        measured_hit = float(cache_stats["hit_rate"])
    elif cache_stats is not None and getattr(cache_stats, "hit_rate", None) is not None:
        measured_hit = float(cache_stats.hit_rate)
    hit_sentence = (
        f" The REALIZED Redis hit rate this run was {measured_hit:.3f} "
        "(manifest `cache_stats`, INFO keyspace deltas over measured windows)."
        if measured_hit is not None else
        " The actual hit rate was NOT measured this run (the manifest's "
        "`cache_stats` are null), so any claim that rests on cache-miss "
        "behaviour, notably the EF Core cache-miss round-trip asymmetry, "
        "is not supported by this run's evidence."
    )
    if ws is None:
        bullets.append(
            "- **Effective working set and cache residency.** This run's "
            "manifest carries no `working_set` section, so neither the ids the "
            "measured windows named nor their residency is quantified here."
            + hit_sentence + "\n"
        )
    else:
        bullets.append(
            "- **Effective working set and cache residency.** Targets are DRAWN "
            f"PER ATTACK (`working_set.draw_mode` = `{ws.draw_mode}`), Zipf-hot "
            + _seeded_corpus_phrase(ws) + ", from a shared body pool of "
            + _body_pool_phrase(ws) + ". "
            + _working_set_residency(ws, hit_rate) + hit_sentence + "\n"
        )

    # --- pre-existing scope notes -------------------------------------------- #
    # DB-CPU attribution: derived from THIS RUN's artifacts, never a standing
    # declaration. W16 measures it, so the bullet appears only when this
    # particular run has no PostgreSQL window on disk.
    db_windows = sum(
        lr.db_n_windows
        for c in res_cells if c.resource
        for lr in c.resource.by_slot.values()
    )
    if not db_windows:
        bullets.append(
            "- **DB-CPU-per-request attribution.** This run carries no "
            f"`cpu_mem_<slot>.{_DB_LABEL}.summary.json` window (a non-Linux run, "
            "or a cells tree that was not retained), so the PostgreSQL CPU delta "
            "that splits app cost from query-plan cost is unavailable HERE and is "
            "omitted rather than estimated. It is not out of scope: "
            "`live_runner._sample_targets` declares a PostgreSQL cgroup target "
            "for every probe; this run has no window carrying its summary.\n"
        )
    else:
        bullets.append(
            "- **DB CPU is per CONTAINER, not per endpoint or per statement.** The "
            "PostgreSQL column is a whole-cgroup `cpu.stat` delta, so it includes "
            "autovacuum, the checkpointer and the WAL writer -- maintenance that is "
            "debt from earlier windows and is not proportional to this probe's "
            "requests. Attributing cost to a statement needs `pg_stat_statements` "
            "deltas, which this run does not capture.\n"
        )
    bullets.append(_container_placement_bullet(manifest))
    # WHICH apparatus cgroups were sampled is read off the manifest section that
    # holds them, never listed here: `load_generator_overhead` stays empty for a
    # run with no cgroup accounting at all, and naming vegeta/dockerd/containerd
    # regardless would assert a measurement the run does not have.
    apparatus = [
        r.label for r in (getattr(manifest, "load_generator_overhead", None) or [])
    ]
    if apparatus:
        apparatus_bullet = (
            "- **Load-generator and container-runtime CPU is measured and "
            "EXCLUDED.** " + ", ".join(f"`{x}`" for x in sorted(apparatus))
            + (" is" if len(apparatus) == 1 else " are")
            + " sampled (manifest `load_generator_overhead`) so the single-host "
            "accounting is auditable, but their CPU is apparatus and never enters "
            "a per-request figure. "
        )
    else:
        apparatus_bullet = (
            "- **Load-generator and container-runtime CPU is EXCLUDED, and was NOT "
            "measured this run.** The manifest's `load_generator_overhead` section "
            "is empty, so the apparatus cost is excluded by construction (nothing "
            "downstream adds it to a per-request figure) but is not quantified "
            "here. "
        )
    bullets.append(
        apparatus_bullet
        + "The CO-safety of the load rests on the per-probe achieved==offered "
        "evidence (manifest `achieved_vs_offered`).\n"
    )
    bullets.append(_runtime_stats_bullet(led.run_id))
    bullets.append(
        "- **Hysteresis, p999 order-statistic CIs, and isolation runs.** Not captured "
        "in this build (see docs/METHODOLOGY.md).\n"
    )
    # Only an ABSENCE belongs here: when the roll-up exists the dollar figure is
    # a section of its own further up, and a scope bullet saying it is "not
    # published" would contradict the table above it.
    if not any(
        lr.cpu_ms_per_req == lr.cpu_ms_per_req      # not NaN
        for c in res_cells if c.resource
        for lr in c.resource.by_slot.values()
    ):
        bullets.append(
            "- **Illustrative $/M-request cost.** Not published in this run: it is "
            "the roll-up's `CPU-ms / request` scaled by a chosen vCPU-hour price, "
            "and no cell here has a request-weighted CPU-ms/request to scale.\n"
        )
    return bullets


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def analyze_run(run_id: str) -> AnalyzeResult:
    """Load the run's ledger, analyze every cell with ladder data, write artifacts.

    Returns the assembled result (with `analyzable=False` and an empty `cells`
    list when no cell has a completed ladder). Always writes fits.json + report.md
    so the caller can point a reader at them even on the no-data path.
    """
    led = Ledger.load(run_id)
    if led is None:
        raise FileNotFoundError(f"no ledger at {paths.run_dir(run_id) / 'ledger.json'}")

    # This function re-reads the LIVE spec tree (load_slo() here and in the
    # helpers above) and stamps the run's pinned hash into fits.json -- so a
    # spec edited after the run would silently re-fit at the new SLO/margins
    # under the old provenance label. The resume path has always been
    # hash-guarded (`Ledger.load_or_create`); analysis needs the same guard.
    from .config import config_hash as _live_config_hash

    live_hash = _live_config_hash()
    if live_hash != led.config_hash:
        if not os.environ.get("LEDGERBENCH_ALLOW_SPEC_DRIFT"):
            raise SpecDriftError(
                f"spec/ has changed since run {run_id} started: live config_hash "
                f"{live_hash[:12]} != the run's pinned {led.config_hash[:12]}. "
                f"Analysis would apply the CURRENT spec's SLO/margins to that "
                f"run's data while labelling it with the old hash. Restore "
                f"spec/ (git) or set LEDGERBENCH_ALLOW_SPEC_DRIFT=1 to analyze "
                f"anyway with both hashes recorded."
            )

    slo = load_slo()
    slo_ms = float(slo.slo.threshold_ms)
    # The slot pair comes from the RUN'S OWN pinned ledger field, never from the
    # analyze-time environment: a report must describe the comparison that ran.
    slot_set = registry.get_slot_set(led.slot_set)

    cells: list[CellAnalysis] = []
    for cell_rec in led.cells:
        analysis = _analyze_cell(cell_rec, slo, run_id, slot_set)
        if analysis is not None:
            cells.append(analysis)

    report_dir = paths.run_dir(run_id) / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    fits_path = report_dir / "fits.json"
    report_path = report_dir / "report.md"

    fits = {
        "run_id": run_id,
        "slo_ms": slo_ms,
        "config_hash": led.config_hash,
        # Present only when the operator analyzed across a spec edit under
        # LEDGERBENCH_ALLOW_SPEC_DRIFT: the hash of the spec the ANALYSIS read,
        # beside the hash of the spec the RUN measured under.
        **(
            {"spec_hash_at_analysis": live_hash}
            if live_hash != led.config_hash
            else {}
        ),
        "slot_set": slot_set.name,
        "slots": list(slot_set.slots),
        # The tier travels with the machine-readable fits for the same reason it
        # heads the report: a `smoke` fits.json is otherwise indistinguishable
        # from a measured one to anything that reads it.
        "tier": _tier_provenance(led),
        "ratio_direction": RatioDirection.of(slot_set).to_dict(),
        "cells": [c.to_dict() for c in cells],
    }
    # NaN is a valid float sentinel in memory and INVALID strict JSON on disk:
    # `json.dumps` happily emits a bare `NaN` token that jq/`json.loads` in
    # strict mode refuse. Null is the artifact spelling of not-measured.
    paths.write_text_atomic(fits_path, json.dumps(_json_sanitize(fits), indent=2))
    paths.write_text_atomic(
        report_path, _build_report_md(run_id, slo_ms, cells, led, slot_set)
    )
    # report/ does not exist when run_all writes its map, so the map is rewritten
    # here. Recompute-from-scratch means the second write supersedes the first
    # wholesale; nothing can be hashed twice.
    checksums.write_run_checksums(run_id)

    return AnalyzeResult(
        run_id=run_id, slo_ms=slo_ms, cells=cells,
        fits_path=fits_path, report_path=report_path,
    )
