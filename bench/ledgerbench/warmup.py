"""Warmup controller (spec/slo.yaml `warmup`).

Drives the SUT to steady state before every measured probe (every probe is
warmed), polling GET /runtime-stats at `poll_hz` and checking the
gate set. Hard cap `hard_cap_seconds`; if not steady by then the probe is
flagged under-warmed and that fact is recorded (never silently proceeded past).

Gates (spec/slo.yaml warmup.gates + per_endpoint_min_calls):
  - per-endpoint min call counts met (low-share endpoints tiered up),
  - p99 flat across trailing sub-windows (no positive trend), recording the FULL
    statistic vector (`flatness_verdict` -> `FlatnessResult`), of which only the
    Mann-Kendall verdict is enforced,
  - GC steady, SYMMETRIC across runtimes: `registry.JVM_GC` and
    `registry.CLR_GC` both require the collection-cadence series AND the
    heap_committed series to show no positive trend. Headline memory metric is
    steady-state anon RSS, so neither runtime may pass with a still-climbing
    heap. The two runtimes reach the cadence series differently and W27 declares
    which: .NET sums gen0+gen1+gen2, the JVM publishes those as a present null
    and sums the per-collector CYCLE beans instead
    (`registry.GcSemantics.cadence_beans`, `runtimestats.GcCollections.cadence`,
    spec/runtime-stats.md). The basis travels in the gate record,
  - pools warm (db_pool.total == max; redis ready).

Every gate spec/slo.yaml declares is one of the above: the governing rule
(nothing enters spec/ as a declared gate until its implementation exists) is
mechanised as `set(load_slo().warmup.gates) - IMPLEMENTED_GATES == set()` in the
test suite. `not_implemented_gate` and `unimplemented_gates()` are kept anyway,
so a gate declared ahead of its implementation is emitted per probe as
{"name": "...", "status": "not_implemented"} rather than being absent -- round 1
declared six gates, evaluated four, and recorded nothing about the other two
(R1-B1). W23 deleted those two from spec/slo.yaml (docs/p1-plan.md 5.1) rather
than leaving them declared; the machinery that would have surfaced them stays.

This module owns the *gate predicates* (pure, tested) + the polling loop that
applies them. The pure predicates take stats snapshots so they unit-test with no
network. Latency flatness uses the runner's own per-second p99 series (from the
warmup vegeta stream); a small monotone-trend test lives here to avoid a scipy
dependency in the runner (full Mann-Kendall lives in the analysis stage).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, computed_field

from . import registry
from .config import WarmupCfg
from .runtimestats import PollWindow, RuntimeStats, Snapshot
from .util import paths
from .util.clock import rfc3339_micros

# Map mix endpoint names -> runtime-stats route-template keys for the call gate.
_MIX_TO_ROUTE = {
    "get_invoice": "GET /invoices/{id}",
    "list_invoices": "GET /customers/{id}/invoices",
    "pricing_quote": "POST /pricing/quote",
    "create_invoice": "POST /invoices",
    "pdf_stub": "POST /invoices/{id}/pdf-stub",
    "healthz": "GET /healthz",
}

# spec/slo.yaml warmup.gates entries this module actually evaluates. Anything
# else the spec declares is emitted per probe with status "not_implemented".
IMPLEMENTED_GATES = frozenset({"p99_flat", "p999_flat", "gc_steady", "pools_warm"})

# Gate status values used in the per-probe warmup.json record.
STATUS_MEASURED = "measured"
STATUS_NOT_IMPLEMENTED = "not_implemented"
# Two waiver statuses, not one, because the two waivers have different
# provenance and a reader must be able to tell them apart: POSTURE_WAIVED comes
# from `spec/slo.yaml warmup.gates_by_posture` (pre-registered, inside the
# ledger's config_hash), NOT_APPLICABLE from `phases.CELLS[].warmup_gates`
# (declared in code, alongside the cell's own knobs, and carrying that cell's
# `gate_set_rationale`). Neither is enforced.
STATUS_POSTURE_WAIVED = "posture_waived"
STATUS_NOT_APPLICABLE = "not_applicable"
# A gate that WAS applied and found it had nothing to apply itself to. It is a
# fifth status rather than `passed=False, status=measured` because the two are
# different findings and only one of them is about the SUT -- but it ENFORCES,
# and that is the whole point of adding it: with an empty latency series
# `no_positive_trend` returns "flat" and with no poller snapshot `gc_steady`
# returns "steady", so before this a probe whose /runtime-stats never parsed
# collected three green gates on zero evidence (R1-B1, exactly).
STATUS_INSUFFICIENT_DATA = "insufficient_data"

# The statuses that DECIDE `under_warmed`. `insufficient_data` is in it: a gate
# with no evidence must flag the probe, never pass it and never abstain.
ENFORCING_STATUSES = frozenset({STATUS_MEASURED, STATUS_INSUFFICIENT_DATA})

# `WarmupResult.exit_reason` values. The attack-failure reason carries the
# vegeta return code, so it is matched by PREFIX. These are constants rather
# than literals at the one call site because `under_warmed` READS the reason:
# a crashed warmup is under-warmed even in the case where the gates it managed
# to evaluate all passed, and that read must not depend on a spelling.
EXIT_GATES_PASSED = "gates_passed"
EXIT_CAP_REACHED = "cap_reached"
EXIT_ATTACK_FAILED_PREFIX = "attack_failed_rc"

# One-sided significance level for every warmup flatness test. Distinct from
# `spec/slo.yaml stationarity.alpha`, which the STATIONARITY consumers read
# (threaded by analysis_bridge); this one is the WARMUP runner's, a named
# constant so the value recorded in `WarmupThresholds` is the value the gate
# used rather than a copy of it.
MANN_KENDALL_ALPHA = 0.05

# Trailing window (in 1 Hz polls / per-second latency samples) the flatness
# gates test. LIMITATION, deliberate and documented at the point of use: the
# Mann-Kendall test needs enough points to reach one-sided significance at
# alpha=0.05: with only 5 samples EVERY pair must be concordant (a strictly
# monotone rise) before the gate can fail, so a 5-sample window is very nearly
# unfailable. 30 samples (the last ~30 s of a warmup that is hard-capped at
# 300 s and normally ENDS EARLIER, once the gates settle) is short enough to
# still describe "the end of warmup" and long enough for the test to have power.
# Series shorter than this are used whole: which is why the smoke profile
# (15 s cap at 1 Hz) is unaffected by the widening.
#
# A wider window makes p99_flat/p999_flat/gc_steady HARDER to pass. See B2/W21:
# driven through this repo's own Mann-Kendall implementation, a DRIFT-FREE AR(1)
# series (which is what a per-second p99 series is) already false-flags at
# 0.16-0.29 for realistic autocorrelation, and a wider window makes that worse.
FLATNESS_TRAILING_SAMPLES = 30

# Fewest usable samples any trend gate may return a VERDICT on. Not a threshold
# in B2/W21's sense (nothing here is calibrated): Mann-Kendall is undefined below
# three points, and `mann_kendall` accordingly cannot report a trend there -- so
# the old `len(series) < 3 -> True` was not a lenient test, it was no test at
# all, reported as a pass. Below this the gate returns `insufficient_data` and
# flags the probe.
MIN_TREND_SAMPLES = 3

# Why a flatness gate returned `insufficient_data`. Two causes, and they are not
# interchangeable: one is about the SERIES (too short to test) and one is about
# the HARNESS (the quantile has no resolution at this sampling rate). Only the
# first says anything about the SUT, and only the first enforces.
REASON_TOO_FEW_SAMPLES = "too_few_samples"
REASON_QUANTILE_UNRESOLVED = "quantile_unresolved"

# Per-second sample count below which q=0.999 has no resolution, and the reason
# `p999_flat` is retired as an ENFORCED gate (docs/p1-plan.md 5.2).
# `vegeta._percentile_ns` is nearest-rank over a one-second bucket,
# `idx = min(n-1, int(q*(n-1)+0.5))`, so for q=0.999 the returned order statistic
# is, by bucket size n:
#     n <=  501  ->  idx == n-1, i.e. EXACTLY THE MAXIMUM of that second
#     502..1501  ->  idx == n-2, the second-largest
#     >= 1502    ->  idx <= n-3
# The harness warms at 618 rps on `full` and `rehearsal` and 149 rps on `smoke`
# (`live_runner._warmup_rate`), so at those rates `p999_flat` is a trend test on
# the per-second maximum or on the second-largest sample, not on a location
# parameter.
#
# The floor (docs/p1-plan.md 3.2.2) is 1000 samples/s, and it is a RESOLUTION
# floor and nothing more. Below it a flatness gate returns `insufficient_data`
# with `enforce=False`: recorded, deciding nothing. `spec/slo.yaml warmup.gates`
# does not declare `p999_flat`, so `evaluate` skips it; every probe still writes
# its full per-second series, with bucket sizes, to
# `<slot>.warmup_latency_per_second.csv`.
MIN_SAMPLES_PER_SECOND_P999 = 1000.0

# What the ENFORCED verdict is computed from, recorded beside every flatness
# record. It is still round 1's rule -- Mann-Kendall significance alone, no
# practical-significance band -- because step 1 of B2 changes no threshold:
# `s_rel`, the lag-1 autocorrelation and the half-vs-half ratio are RECORDED and
# read by nothing; spec/slo.yaml carries no threshold on them.
FLATNESS_ENFORCED_STATISTIC = "mann_kendall_alpha_only"


# --------------------------------------------------------------------------- #
# pure gate predicates
# --------------------------------------------------------------------------- #
def endpoint_calls_met(stats: RuntimeStats, min_calls: dict[str, int]) -> tuple[bool, dict[str, int]]:
    """All per-endpoint counters >= their configured minimum.

    Returns (ok, shortfall_by_route) where shortfall is min - actual for any
    route still short (positive only).
    """
    shortfall: dict[str, int] = {}
    for mix_name, minimum in min_calls.items():
        route = _MIX_TO_ROUTE.get(mix_name, mix_name)
        actual = stats.endpoint_total(route)
        if actual < minimum:
            shortfall[route] = minimum - actual
    return (not shortfall), shortfall


def pools_warm(stats: RuntimeStats) -> bool:
    """db_pool.total == max (min=max policy) and redis pool present.

    With min=max the pool must be fully opened during warmup (spec/db-access.md
    "db_pool.total == max"). Redis readiness is "total >= 1" or, where the client
    is a multiplexer rather than a pool and reports max == 0, unconditional.

    Known asymmetry (docs/research/2026-08-21-critique.md, E6): the `max == 0`
    escape means a Lettuce multiplexer reporting 0 passes the Redis half
    unconditionally while .NET must show total >= 1, and HikariCP does not open
    min=max connections synchronously at startup the way Npgsql's Minimum Pool
    Size does.

    A None gauge (the key was ABSENT from the body -- a contract violation, since
    spec/runtime-stats.md pins every key present) fails its half rather than
    comparing: before the runtimestats defaults went None-not-zero, an absent
    redis_pool block read as total=0/max=0 and passed this gate vacuously.
    """
    db_max, db_total = stats.db_pool.max, stats.db_pool.total
    db_ok = db_max is not None and db_total is not None and db_max > 0 and db_total >= db_max
    r_max, r_total = stats.redis_pool.max, stats.redis_pool.total
    redis_ok = (r_total is not None and r_total >= 1) or (r_max is not None and r_max == 0)
    return db_ok and redis_ok


@dataclass(frozen=True)
class TrendVerdict:
    """One trend gate's answer, WITH the evidence count behind it.

    `passed` alone cannot distinguish "Mann-Kendall found no upward trend across
    30 samples" from "there were no samples". Those are the two findings a
    warmup gate has to keep apart, so the count and the status travel with the
    verdict and land in `GateResult.detail`.
    """

    passed: bool
    status: str
    n: int

    @property
    def measured(self) -> bool:
        return self.status == STATUS_MEASURED


def usable(series: Sequence[float | int | None]) -> list[float]:
    """The points a trend test may actually score: the non-null ones.

    A null is a value the runtime did not report, and dropping it is the only
    honest handling -- coercing it to 0.0 would inject a step change into the
    very series a flatness test reads. Dropping shortens the series, which is
    why `TrendVerdict.n` is the LENGTH OF THIS LIST and not of the input.
    """
    return [float(x) for x in series if x is not None]


def no_positive_trend(
    series: Sequence[float | int | None], *, alpha: float = MANN_KENDALL_ALPHA
) -> TrendVerdict:
    """Mann-Kendall test: no statistically significant positive monotone trend.

    Uses the SAME tie-corrected, continuity-corrected MK implementation as the
    analysis-stage stationarity gate (analysis.mann_kendall; numpy is a core
    runner dep), so the warmup flatness gate and the fit stationarity gate share
    one statistic. Significance at `alpha` (one-sided, upward) is required to
    FAIL: a raw concordant-pair majority (S > 0) on a short noisy-but-flat
    window is expected ~half the time and must not flag a probe under-warmed.

    Below MIN_TREND_SAMPLES usable points the answer is `insufficient_data` and
    `passed=False`. It used to be `True`, which is how an empty series passed:
    `live_runner` leaves the p99/p999 series empty when the per-second encode
    fails, and a SUT whose /runtime-stats the poller cannot parse leaves every
    GC series empty -- both of which then reported a green flatness gate on
    nothing at all (docs/p1-plan.md 3.2 step 1).
    """
    points = usable(series)
    if len(points) < MIN_TREND_SAMPLES:
        return TrendVerdict(passed=False, status=STATUS_INSUFFICIENT_DATA, n=len(points))
    from analysis.mann_kendall import mann_kendall

    return TrendVerdict(
        passed=not mann_kendall(points).has_positive_trend(alpha),
        status=STATUS_MEASURED, n=len(points),
    )


# --------------------------------------------------------------------------- #
# the flatness statistic vector (docs/p1-plan.md 3.2.2 step 1)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FlatnessCfg:
    """Everything `flatness_verdict` needs that is not the series itself."""

    # Which quantile the series carries. Recorded rather than inferred from the
    # gate name, because the resolution floor below is a statement about THIS
    # quantile's order statistic and a reader has to be able to check it.
    quantile: float
    # Samples inside each one-second bucket of the series. See
    # `WarmupState._samples_per_second`: measured from the bucket sizes when the
    # encoder reported them, else the offered attack rate, and which of the two
    # is recorded in `samples_per_second_source`.
    samples_per_second: float
    samples_per_second_source: str = "unknown"
    # Below this the quantile has no resolution and the gate does not enforce.
    min_samples_per_second: float = 0.0
    alpha: float = MANN_KENDALL_ALPHA
    min_samples: int = MIN_TREND_SAMPLES
    # Spacing between consecutive samples. 1.0 for a per-second series; it is a
    # parameter because `s_rel` is expressed per MINUTE and the conversion is
    # only correct if the sample spacing is known rather than assumed.
    dt_seconds: float = 1.0


@dataclass(frozen=True)
class FlatnessResult:
    """The whole statistic vector behind one flatness verdict, not a bool.

    Every statistic is recorded per probe so the gate can be recalibrated from
    artifacts (R1-A2, docs/p1-plan.md 3.2); only `verdict` is read by anything.

    `s_rel_pct_per_min` is the statistic B2 calibrates on, and the reason it is
    that one rather than an absolute ms/min band: it is SCALE-FREE, so the same
    relative drift means the same thing on two stacks whose absolute p99 differ.
    An absolute band is language-asymmetric by construction -- it trips first on
    whichever stack has the higher absolute p99 -- which is the shape of round
    1's Go 67.3% / .NET 51.8% split. Nothing enforces it here, and spec/slo.yaml
    carries no threshold on it.
    """

    status: str
    # The enforced answer, and it is EXACTLY round 1's rule: True when
    # Mann-Kendall finds no significant upward trend at `alpha`. False whenever
    # the status is not `measured`, so a verdict never reads as a pass on
    # evidence that was not there.
    verdict: bool
    # Why the status is not `measured` (REASON_*); "" when it is.
    reason: str
    quantile: float
    # Usable (non-null) points scored, and the samples behind each of them.
    n: int
    samples_per_second: float
    samples_per_second_source: str
    alpha: float
    min_samples: int
    min_samples_per_second: float
    # None on every field below when there were too few points to compute it.
    median: float | None = None
    mk_z: float | None = None
    mk_p_increasing: float | None = None
    mk_tau: float | None = None
    # Theil-Sen, in SERIES UNITS PER SECOND (ms/s for a p99 series in ms).
    slope_per_second: float | None = None
    # 100 * slope_per_second * 60 / median. docs/p1-plan.md 3.2.2 writes the
    # ratio as `slope*60/median` and labels it %/min; the factor 100 is the
    # conversion that label requires, and it is applied here so the recorded
    # number is literally percent per minute. None when the median is <= 0,
    # where a relative slope is undefined rather than large.
    s_rel_pct_per_min: float | None = None
    # Lag-1 autocorrelation of the window. Recorded because it is the parameter
    # the false-positive rate depends on: 3.2.1 simulates MK's FPR at n=30 rising
    # 0.095 -> 0.361 as phi goes 0.3 -> 0.95 on a DRIFT-FREE series. Without it
    # a flagged window cannot be told from a merely autocorrelated one.
    lag1_autocorrelation: float | None = None
    # median(second half) / median(first half). A level shift is the other thing
    # 3.2.1 shows flagging a warm SUT (a single +10% mid-window shift flags 38%),
    # and it is invisible to a slope.
    half_vs_half_median_ratio: float | None = None

    @property
    def enforce(self) -> bool:
        """Whether this record is allowed to decide `under_warmed`.

        False for exactly one cause: the quantile had no resolution at this
        sampling rate. That is a fact about the HARNESS, not about the SUT, and
        it is true of every probe at the warmup rate -- so enforcing it would
        flag 100% of probes while saying nothing about warmth. Every other
        status, `insufficient_data` included, enforces.
        """
        return self.reason != REASON_QUANTILE_UNRESOLVED

    def to_detail(self) -> dict:
        """The vector as it lands in `GateResult.detail`."""
        return {
            "enforced_statistic": FLATNESS_ENFORCED_STATISTIC,
            "quantile": self.quantile,
            "status": self.status,
            "reason": self.reason,
            "n": self.n,
            "samples_per_second": self.samples_per_second,
            "samples_per_second_source": self.samples_per_second_source,
            "alpha": self.alpha,
            "min_samples": self.min_samples,
            "min_samples_per_second": self.min_samples_per_second,
            "median": self.median,
            "mk_z": self.mk_z,
            "mk_p_increasing": self.mk_p_increasing,
            "mk_tau": self.mk_tau,
            "slope_per_second": self.slope_per_second,
            "s_rel_pct_per_min": self.s_rel_pct_per_min,
            "lag1_autocorrelation": self.lag1_autocorrelation,
            "half_vs_half_median_ratio": self.half_vs_half_median_ratio,
            "observe_only": [
                "s_rel_pct_per_min",
                "lag1_autocorrelation",
                "half_vs_half_median_ratio",
            ],
        }


def lag1_autocorrelation(points: Sequence[float]) -> float | None:
    """Lag-1 autocorrelation of an already-cleaned series, or None.

    The textbook estimator, sample mean removed:
    sum_t (x_t - xbar)(x_{t-1} - xbar) / sum_t (x_t - xbar)^2. None on a series
    with zero variance, where the ratio is 0/0 -- reporting 0.0 there would say
    "uncorrelated" about a constant.
    """
    n = len(points)
    if n < 2:
        return None
    mean = sum(points) / n
    dev = [p - mean for p in points]
    denom = sum(d * d for d in dev)
    if denom <= 0.0:
        return None
    num = sum(dev[i] * dev[i - 1] for i in range(1, n))
    return num / denom


def half_vs_half_median_ratio(points: Sequence[float]) -> float | None:
    """median(second half) / median(first half), or None.

    Odd-length windows drop the middle point rather than assigning it to a half,
    so neither half is one sample longer than the other. None when the first
    half's median is <= 0, where the ratio is undefined rather than infinite.
    """
    import statistics as _st

    half = len(points) // 2
    if half < 1:
        return None
    first = _st.median(points[:half])
    second = _st.median(points[-half:])
    if first <= 0.0:
        return None
    return second / first


def flatness_verdict(
    series: Sequence[float | int | None], *, cfg: FlatnessCfg
) -> FlatnessResult:
    """One flatness gate's answer as the WHOLE statistic vector.

    The enforced verdict is unchanged from round 1 -- Mann-Kendall significance
    at `cfg.alpha`, no practical-significance band -- because step 1 of B2
    changes no threshold. What changes is that the numbers a recalibration needs
    are now recorded on every probe instead of being discarded.

    Two `insufficient_data` causes, checked in this order and the order is
    load-bearing:

    1. `samples_per_second < cfg.min_samples_per_second`: the quantile has no
       resolution at this sampling rate (see MIN_SAMPLES_PER_SECOND_P999). It is
       checked FIRST because it is decidable before any data arrives and because
       it must not enforce -- were the empty-series cause to win, `p999_flat`
       would flag every probe whose warmup produced no series at all, on a gate
       that is inert at the harness's own warmup rate.
    2. fewer than `cfg.min_samples` usable points: the series is too short to
       test. This one DOES enforce -- an empty series is the vacuous pass W23
       closed, and it is evidence about the probe.

    In case 1 the statistics are still computed and returned: the gate is
    retired as an enforcer, not as a measurement (docs/p1-plan.md 5.2).
    """
    points = usable(series)
    n = len(points)
    base = dict(
        quantile=cfg.quantile, n=n,
        samples_per_second=cfg.samples_per_second,
        samples_per_second_source=cfg.samples_per_second_source,
        alpha=cfg.alpha, min_samples=cfg.min_samples,
        min_samples_per_second=cfg.min_samples_per_second,
    )
    unresolved = cfg.samples_per_second < cfg.min_samples_per_second
    if n < cfg.min_samples:
        return FlatnessResult(
            status=STATUS_INSUFFICIENT_DATA,
            reason=REASON_QUANTILE_UNRESOLVED if unresolved else REASON_TOO_FEW_SAMPLES,
            verdict=False, **base,
        )

    from analysis.mann_kendall import mann_kendall, theil_sen_slope

    import statistics as _st

    mk = mann_kendall(points)
    slope = theil_sen_slope(points, dt_seconds=cfg.dt_seconds)
    median = _st.median(points)
    s_rel = (100.0 * slope * 60.0 / median) if median > 0.0 else None
    stats = dict(
        median=median, mk_z=mk.z, mk_p_increasing=mk.p_increasing, mk_tau=mk.tau,
        slope_per_second=slope, s_rel_pct_per_min=s_rel,
        lag1_autocorrelation=lag1_autocorrelation(points),
        half_vs_half_median_ratio=half_vs_half_median_ratio(points),
    )
    if unresolved:
        return FlatnessResult(
            status=STATUS_INSUFFICIENT_DATA, reason=REASON_QUANTILE_UNRESOLVED,
            verdict=False, **base, **stats,
        )
    return FlatnessResult(
        status=STATUS_MEASURED, reason="",
        verdict=not mk.has_positive_trend(cfg.alpha),
        **base, **stats,
    )


@dataclass(frozen=True)
class FlatnessGateSpec:
    """One flatness gate: its name, its quantile, and its resolution floor.

    A table rather than two hand-written branches in `evaluate`, so the p999
    floor is declared beside the gate it belongs to and cannot be applied to one
    quantile and forgotten on the other.
    """

    gate: str
    quantile: float
    min_samples_per_second: float


FLATNESS_GATES = (
    FlatnessGateSpec("p99_flat", 0.99, 0.0),
    FlatnessGateSpec("p999_flat", 0.999, MIN_SAMPLES_PER_SECOND_P999),
)


def gc_steady(
    series_by_name: Mapping[str, Sequence[float | int | None]],
    gc: registry.GcSemantics,
) -> tuple[TrendVerdict, dict[str, TrendVerdict]]:
    """GC-steady: every series this runtime's semantics REQUIRE shows no positive trend.

    One implementation, driven by declared data. Round 1 had two nearly identical
    functions and a dispatch of the form `if lang == "dotnet": <.NET> else: <Go>`,
    so any string that was not literally "dotnet" -- a typo, a new language, a
    slot id -- silently inherited Go's single-generation model AND still reported
    `passed`. `registry.gc_semantics()` RAISES on an unregistered slot instead.

    Both registered runtimes currently require both series, and that symmetry is
    deliberate rather than incidental: the headline memory metric is steady-state
    anon RSS, so neither stack may pass warmup with a still-climbing heap even if
    its collection cadence has settled.

    Returns the overall verdict plus the per-series ones. The overall status is
    `insufficient_data` when ANY required series has too few usable points: with
    zero poller snapshots every required series is empty, so a slot whose
    /runtime-stats never parsed is flagged. `n=0` on both series is a FLAG, not a
    pass.
    """
    per_series = {name: no_positive_trend(series_by_name[name]) for name in gc.requires}
    if not per_series:
        return TrendVerdict(passed=False, status=STATUS_INSUFFICIENT_DATA, n=0), {}
    short = min(per_series.values(), key=lambda v: v.n)
    if any(not v.measured for v in per_series.values()):
        return TrendVerdict(passed=False, status=STATUS_INSUFFICIENT_DATA, n=short.n), per_series
    return TrendVerdict(
        passed=all(v.passed for v in per_series.values()),
        status=STATUS_MEASURED, n=short.n,
    ), per_series


# --------------------------------------------------------------------------- #
# result record
# --------------------------------------------------------------------------- #
class GateResult(BaseModel):
    name: str
    passed: bool
    status: str = STATUS_MEASURED
    # Elapsed warmup seconds at the evaluation that produced this record --
    # i.e. the segment boundary the verdict was taken at, so a record can be
    # placed on the warmup timeline instead of only inside it. None means the
    # gate was never evaluated, and that holds for EVERY non-measured status:
    # the waiver records are built once, before the loop, and an unimplemented
    # gate has no evaluation at all. Stamping one would put a record nobody
    # scored on the timeline as though somebody had.
    at_seconds: float | None = None
    # Whether this gate is ALLOWED to decide `under_warmed`, independently of
    # whether it had evidence. The second axis exists because B2 ships a gate
    # that is computed on every probe and enforces nothing: `p999_flat` below
    # the q=0.999 resolution floor (MIN_SAMPLES_PER_SECOND_P999). Status alone
    # could not express that -- `insufficient_data` enforces by design, and
    # spelling the retirement as a waiver would put a harness fact in
    # `spec/slo.yaml warmup.gates_by_posture`, which is for POSTURES. Defaults
    # True, so a gate is observe-only only where the emitting site says so.
    enforce: bool = True
    detail: dict = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def enforced(self) -> bool:
        """Only measured gates decide under-warmed; not_implemented never does.

        `computed_field`, so `save()` writes it: a reader scanning a gate record
        for a `passed: false` needs to know whether it flagged the probe or was
        waived, and as a plain property that answer reached the artifact only as
        the `status` string plus knowledge of which values this maps to true.
        `spec/slo.yaml warmup.gates` and the report's waiver bullet both tell
        readers the record carries it, so the record carries it.

        `insufficient_data` enforces alongside `measured`. A gate that found no
        evidence has to FLAG the probe: abstaining would put it back in the
        company of the waivers, which is the same silence with a different
        spelling, and passing is what this whole status exists to stop.
        """
        return self.enforce and self.status in ENFORCING_STATUSES


def posture_waived_gate(name: str, cell_id: str) -> GateResult:
    """Explicit record for a gate this POSTURE is not held to.

    `spec/slo.yaml warmup.gates_by_posture` can waive a gate a cell cannot
    satisfy by construction (R1-A3). Waiving it is the right answer; silently
    waiving it is R1-B1. A waived gate therefore leaves the
    same kind of trace an unimplemented one does: `passed=False` so a reader
    scanning for `passed: true` never finds one, and `enforced=False` so it
    cannot flag the probe.
    """
    return GateResult(
        name=name,
        passed=False,
        status=STATUS_POSTURE_WAIVED,
        detail={
            "cell_id": cell_id,
            "note": (
                f"spec/slo.yaml enables warmup gate {name!r} globally and waives "
                f"it for posture {cell_id!r} via warmup.gates_by_posture. It was "
                "NOT evaluated for this probe and did not decide under-warmed."
            ),
        },
    )


def not_applicable_gate(
    name: str, cell_id: str, rationale: str, knob: str | None = None
) -> GateResult:
    """Explicit record for a gate THIS CELL declared itself not held to.

    The sibling of `posture_waived_gate`, and it exists separately because the
    two waivers come from different artifacts (see STATUS_NOT_APPLICABLE). This
    one carries the cell's `gate_set_rationale` verbatim, so the reason a probe
    was not held to a gate travels with the probe rather than living only in
    `phases.CELLS`, and `knob` names the declared setting the registry says
    makes the gate unsatisfiable -- None when no registry entry matches, in
    which case the rationale is the whole of the evidence and the record says so
    rather than implying a mechanism.

    passed=False and enforced=False, for the same reasons as the other two
    non-measured statuses: it can never read as a pass and can never flag.
    """
    detail = {
        "cell_id": cell_id,
        "rationale": rationale,
        "knob": knob,
        "note": (
            f"cell {cell_id!r} declares warmup gate {name!r} not applicable to its "
            "posture (phases.CELLS warmup_gates). It was NOT evaluated for this "
            "probe and did not decide under-warmed."
            + (
                f" The declared knob {knob} is registered as making it "
                "unsatisfiable by construction."
                if knob else
                " No registry entry names a knob that makes it unsatisfiable, so "
                "the rationale above is the whole of the evidence for the waiver."
            )
        ),
    }
    return GateResult(
        name=name, passed=False, status=STATUS_NOT_APPLICABLE, detail=detail,
    )


def not_implemented_gate(name: str) -> GateResult:
    """Explicit record for a spec-declared gate this runner does not evaluate.

    passed=False on purpose: a reader scanning for `passed: true` must not find
    one here. `enforced` is False so it cannot flag the probe under-warmed.
    """
    return GateResult(
        name=name,
        passed=False,
        status=STATUS_NOT_IMPLEMENTED,
        detail={
            "note": (
                f"spec/slo.yaml declares warmup gate {name!r}; the runner does "
                "not implement it. Recorded as not_implemented so its absence "
                "is visible; it neither passes nor flags the probe."
            )
        },
    )


@dataclass(frozen=True)
class GateSet:
    """The gate set ONE posture is held to, and what it dropped from the global set.

    `enforced` is what `WarmupState` evaluates. `waived` maps each globally
    declared gate the posture is NOT held to onto the status recording WHY it
    was dropped (STATUS_POSTURE_WAIVED / STATUS_NOT_APPLICABLE), so the dropped
    half is never merely absent.
    """

    enforced: dict[str, bool]
    waived: dict[str, str]

    @property
    def gate_set_id(self) -> str:
        """Content digest of (gates enforced, gates waived and by which layer).

        Recorded per probe so two probes can be shown to have been held to the
        SAME set without re-deriving it from `phases.CELLS` and `spec/slo.yaml`
        as they stand at READ time -- which is a different question from the one
        the run answered, and the reason `cell_id` alone is not enough. Two
        different cells resolving to the same set share an id; that is the point.
        """
        payload = json.dumps(
            {
                "enforced": sorted(n for n, on in self.enforced.items() if on),
                "waived": sorted(self.waived.items()),
            },
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def gate_set_for_cell(
    cfg: WarmupCfg, *, cell_id: str, cell_gates: Mapping[str, bool] | None = None
) -> GateSet:
    """Resolve one cell's warmup gate set: spec global <- spec posture <- cell.

    Every layer MERGES over the one before it rather than replacing it, so no
    layer can silently stop enforcing a gate added globally after it was written
    (the reason `WarmupCfg.gates_for` merges, extended to the cell layer).

    Shared by the runner, which evaluates the result, and by the analysis stage,
    which cross-checks the per-probe records against it -- one resolution, so a
    report that says a gate was waived and a probe that says it was measured
    cannot both be reading their own copy of the rule.
    """
    enforced = {**cfg.gates_for(cell_id), **(cell_gates or {})}
    waived: dict[str, str] = {}
    for name, declared in cfg.gates.items():
        if not declared or enforced.get(name, False):
            continue
        # The cell layer wins the LABEL when both layers drop the gate: it is
        # the one that carries a rationale.
        waived[name] = (
            STATUS_NOT_APPLICABLE if (cell_gates or {}).get(name) is False
            else STATUS_POSTURE_WAIVED
        )
    return GateSet(enforced=enforced, waived=waived)


class WarmupThresholds(BaseModel):
    """The numbers the gates were scored against, recorded WITH the verdict.

    `hard_cap_seconds` and `per_endpoint_min_calls` come from `spec/slo.yaml`,
    but the map here is the SCALED one this probe was actually held to (the
    smoke and rehearsal tiers divide it), which is not recoverable from the spec
    afterwards. The flatness window and alpha are this module's own constants.
    Recorded per probe because a gate verdict without its threshold cannot be
    re-derived, and B2 recalibrates exactly these numbers.
    """

    model_config = ConfigDict(extra="forbid")

    hard_cap_seconds: int
    flatness_trailing_samples: int
    mann_kendall_alpha: float
    per_endpoint_min_calls: dict[str, int]
    # Per-gate q-resolution floor in samples/s (MIN_SAMPLES_PER_SECOND_P999).
    # Recorded with the rest because it is the number that decided whether a
    # flatness gate enforced on this probe, and it is not in `spec/` -- it is a
    # property of `vegeta`'s nearest-rank estimator, argued at the constant.
    flatness_min_samples_per_second: dict[str, float] = Field(
        default_factory=lambda: {
            spec.gate: spec.min_samples_per_second for spec in FLATNESS_GATES
        }
    )


class WarmupResult(BaseModel):
    """Written to `warmup.json` in the cell artifacts."""

    # extra="forbid" so the W23 renames cannot be absorbed silently: a caller
    # still passing `rate=` or `under_warmed=` raises instead of having the
    # value dropped and the derived one published in its place.
    model_config = ConfigDict(extra="forbid")

    language: str
    # WHERE this warmup ran. Until W23 only the artifact PATH carried it, so a
    # warmup.json read out of its directory -- or collected across a run, which
    # is how the ledger consumes it -- could not say which probe it belonged to.
    cell_id: str = ""
    stage: str = ""
    block: int | None = None
    # `GateSet.gate_set_id` for the set this probe was held to.
    gate_set_id: str = ""
    # `warmup_rate`, not `rate`: the measured window's rate is a different
    # number living in the same tree (`ProbeRecord.rate`), and this one is
    # derived from per_endpoint_min_calls and the cap and is never offered to
    # the measured attack.
    warmup_rate: int
    thresholds: WarmupThresholds
    elapsed_seconds: float
    gates: list[GateResult] = Field(default_factory=list)
    # Warmup runs in segments and exits at the first boundary where every
    # enforced gate passes, so `elapsed_seconds` is the real cost of warming
    # THIS probe rather than a constant hard cap. Recorded so a reader can see
    # how long each stack actually needed -- the cold-start curve's companion
    # number, and the evidence for whether hard_cap_seconds ever binds (G6).
    segments: int = 0
    segment_seconds: int = 0
    exit_reason: str = ""                  # gates_passed | cap_reached | attack_failed_rc<N>
    final_db_pool_total: int | None = None
    final_db_pool_max: int | None = None
    # memory.stat pgmajfault delta over the probe's MEASURED window, filled by
    # the runner. DISCLOSURE ONLY: it is not a gate and never was. The spec once
    # declared `major_faults_asserted_equal`, which W23 deleted -- "equal" is a
    # cross-language statement and the two slots are separate probes paired only
    # by RCB block, so it was not per-probe evaluable as written (p1-plan 5.1).
    # This number is the honest remainder and does not stand in for that claim.
    # None when the counter was unreadable.
    major_faults: int | None = None
    finished_utc: str = Field(default_factory=rfc3339_micros)
    # W17: the two series warmup produces (R1-B3). `poll_window`
    # is the /runtime-stats sampling behind the gc_steady and pools_warm gates;
    # `quantile_series` is the per-second (p99, p999) the flatness gates scored.
    # Both are EXCLUDED from `save()` -- they are traces, and `warmup.json` is a
    # committed roll-up -- and the runner flushes them to their own artifacts.
    # B2's recalibration cannot begin without the second one.
    poll_window: PollWindow = Field(default_factory=lambda: PollWindow(phase="", slot=""))
    # (segment, second, p99_ms, p999_ms, samples). The bucket SIZE is in the row
    # because B2 step 2 builds its null corpus out of exactly this artifact, and
    # a q=0.999 sample whose bucket size is unknown cannot be told from the
    # per-second maximum (MIN_SAMPLES_PER_SECOND_P999).
    quantile_series: list[tuple[int, int, float, float, int]] = Field(default_factory=list)
    # W20: every warmup SEGMENT draws its own targets file from its own PCG64
    # stream. Round 1 pointed all three of a probe's attacks at one file, so the
    # warmup replayed the prefix the measured window was about to consume -- and
    # each segment replayed the segment before it. The increments are recorded
    # (hex, one per segment) so a reader can regenerate exactly what warmed this
    # probe, and the seconds spent drawing are recorded because a per-probe draw
    # is a real cost that has to be visible if it ever grows.
    target_draw_incs: list[str] = Field(default_factory=list)
    target_draw_seconds: float = 0.0
    # The union of the invoice ids every warmup segment's draw asked for, held
    # so the probe can score its measured window's REQUEST-WEIGHTED pre-warm
    # containment (`vegeta.prewarm_containment`). EXCLUDED from every dump: it
    # is a working set of tens of thousands of ids, and `warmup.json` is a
    # roll-up. The scored fraction is what gets published, in the measured
    # draw's own artifact.
    target_draw_invoice_ids: set[int] = Field(default_factory=set, exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gates_evaluated(self) -> list[str]:
        """Names of the gates that were actually SCORED for this probe.

        The enforced ones: a waived or unimplemented record is in `gates` so its
        absence is visible, but it was not evaluated and must not be listed as
        though it had been.
        """
        return [g.name for g in self.gates if g.enforced]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gates_failed(self) -> list[str]:
        """Of `gates_evaluated`, the ones that did not pass."""
        return [g.name for g in self.gates if g.enforced and not g.passed]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def under_warmed(self) -> bool:
        """DERIVED, never stored: the verdict cannot disagree with its evidence.

        Round 1 persisted the bool alone, so the ledger could not say which gate
        flagged a probe without opening its sidecar (R1-B1/B4). Now the list is
        the record and the bool is a function of it -- plus the one non-gate
        cause: a crashed warmup attack, which leaves the series it would have
        been judged on unwritten and so can pass gates vacuously.
        """
        return bool(self.gates_failed) or self.exit_reason.startswith(
            EXIT_ATTACK_FAILED_PREFIX
        )

    def save(self, path: Path) -> None:
        paths.write_text_atomic(
            path,
            self.model_dump_json(indent=2, exclude={"poll_window", "quantile_series"}),
        )


@dataclass
class WarmupState:
    """Mutable accumulator the polling loop updates each tick (testable)."""

    cfg: WarmupCfg
    slot: str
    rate: int
    p99_series: list[float] = field(default_factory=list)
    p999_series: list[float] = field(default_factory=list)
    # Requests in each one-second bucket the quantiles above were taken over,
    # index-aligned with them. MEASURED (`vegeta.per_second_buckets_from_ndjson`
    # counts the bucket it is about to take the percentile of), because the
    # q=0.999 resolution floor is a statement about this number and reading it
    # off the OFFERED rate would be an assumption dressed as a measurement.
    # Empty when the encoder produced no series at all, and
    # `_samples_per_second` then says so rather than silently substituting.
    samples_series: list[int] = field(default_factory=list)
    # `int | None`: a snapshot whose body omitted heap_committed_bytes (contract
    # violation) contributes a None, which `usable()` drops at scoring time --
    # so an unmeasured heap is `insufficient_data`, never a flat zero line.
    committed_series: list[int | None] = field(default_factory=list)
    # `int | None`, one entry per SNAPSHOT. A runtime that publishes neither a
    # generation triple nor any declared cycle bean contributes a None, and the
    # entry is KEPT rather than skipped, so this list stays index-aligned with
    # `snapshots` and a gap is visible as a gap instead of closing silently.
    gc_total_series: list[int | None] = field(default_factory=list)
    # What each entry above counted (`runtimestats.CADENCE_*`), index-aligned.
    # The gate record publishes the distinct set: "the cadence was flat" is a
    # different finding over CLR generations than over JVM collector cycles, and
    # a reader must not have to infer which from the slot name.
    gc_cadence_basis_series: list[str] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)

    def observe(
        self, snap: Snapshot, *, p99_ms: float | None = None,
        p999_ms: float | None = None, samples: int | None = None,
    ) -> None:
        self.snapshots.append(snap)
        self.committed_series.append(snap.stats.gc.heap_committed_bytes)
        total, basis, _ = snap.stats.gc.collections.cadence(
            registry.gc_semantics(self.slot).cadence_beans
        )
        self.gc_total_series.append(total)
        self.gc_cadence_basis_series.append(basis)
        if p99_ms is not None:
            self.p99_series.append(p99_ms)
        if p999_ms is not None:
            self.p999_series.append(p999_ms)
        if samples is not None:
            self.samples_series.append(samples)

    def _samples_per_second(self, trailing: int) -> tuple[float, str]:
        """(samples per one-second bucket, where that number came from).

        The MEDIAN of the measured bucket sizes over the same trailing window
        the gates score, because the first and last bucket of every vegeta
        segment are partial seconds and a mean would drag the figure below what
        the steady buckets held. Falls back to the OFFERED attack rate when no
        bucket count was recorded, and says which it returned: the resolution
        floor decides whether a gate enforces, so a reader has to be able to see
        whether that decision rested on a measurement or on a nominal rate.
        """
        window = self.samples_series[-trailing:]
        if window:
            import statistics as _st

            return float(_st.median(window)), "measured_bucket_median"
        return float(self.rate), "offered_rate"

    def _gc_cadence(self) -> list[int]:
        """Collections per poll interval, over the ADJACENT PAIRS that exist.

        A delta needs both edges. Where either is null the interval has no
        cadence and no value is produced -- differencing across the gap instead
        would attribute a whole unmeasured stretch to one interval, and filling
        the null with 0 would manufacture the flat cadence `gc_steady` is
        looking for. With every entry null this is empty, and an
        empty required series is `insufficient_data`, not a pass.
        """
        s = self.gc_total_series
        return [
            s[i] - s[i - 1]
            for i in range(1, len(s))
            if s[i] is not None and s[i - 1] is not None
        ]

    def evaluate(
        self, *, trailing: int = FLATNESS_TRAILING_SAMPLES,
        at_seconds: float | None = None,
    ) -> list[GateResult]:
        """Apply all gates against the current trailing window.

        `trailing` is the flatness window in samples. It defaults to
        FLATNESS_TRAILING_SAMPLES rather than the old 5: at 5 samples the
        Mann-Kendall test can only reach significance on a strictly monotone
        run, i.e. the p99_flat/p999_flat/gc_steady gates were nearly unfailable
        (see the constant's comment). Every gate spec/slo.yaml declares but this
        module does not implement is emitted with status "not_implemented".

        `at_seconds` is the elapsed warmup time of THIS evaluation, stamped onto
        the ENFORCED records only -- the unimplemented ones were not evaluated
        and get no instant. The runner passes it at each segment boundary, so
        the surviving record says when its verdict was taken; callers with no
        clock (the pure unit tests) leave it None.
        """
        latest = self.snapshots[-1].stats if self.snapshots else RuntimeStats()
        gates: list[GateResult] = []

        calls_ok, shortfall = endpoint_calls_met(latest, self.cfg.per_endpoint_min_calls)
        gates.append(GateResult(name="per_endpoint_min_calls", passed=calls_ok, detail={"shortfall": shortfall}))

        sps, sps_source = self._samples_per_second(trailing)
        series_for_quantile = {0.99: self.p99_series, 0.999: self.p999_series}
        for spec in FLATNESS_GATES:
            if not self.cfg.gates.get(spec.gate, False):
                continue
            window = series_for_quantile[spec.quantile][-trailing:]
            res = flatness_verdict(window, cfg=FlatnessCfg(
                quantile=spec.quantile,
                samples_per_second=sps, samples_per_second_source=sps_source,
                min_samples_per_second=spec.min_samples_per_second,
                alpha=MANN_KENDALL_ALPHA, min_samples=MIN_TREND_SAMPLES,
            ))
            gates.append(GateResult(
                name=spec.gate, passed=res.verdict, status=res.status,
                enforce=res.enforce,
                # `samples` is the length of the trailing slice and
                # `usable_samples` is what the test actually scored. They differ
                # only when the series carries nulls, and the gap is the reason
                # the verdict can read `insufficient_data` beside a non-zero
                # `samples`. The whole statistic vector rides under "flatness":
                # round 1 recorded the verdict alone and so could not recalibrate
                # the gate that rejected 59.5% of its windows.
                detail={"samples": len(window), "usable_samples": res.n,
                        "min_samples": MIN_TREND_SAMPLES,
                        "flatness": res.to_detail()},
            ))
        if self.cfg.gates.get("gc_steady", False):
            gc = registry.gc_semantics(self.slot)
            series = {
                registry.SERIES_HEAP_COMMITTED: self.committed_series[-trailing:],
                registry.SERIES_COLLECTION_CADENCE: self._gc_cadence()[-trailing:],
            }
            v, per_series = gc_steady(series, gc)
            gates.append(GateResult(
                name="gc_steady", passed=v.passed, status=v.status,
                # The probe record says WHICH series this runtime's gate tested
                # and how many samples each carried, so "gc_steady passed" can be
                # read without knowing the collector's model by heart. The
                # per-series status is here for one case in particular:
                # a slot whose /runtime-stats the poller cannot parse leaves
                # every required series at zero samples, and the record must name
                # that rather than showing a bare `passed: false`.
                detail={
                    "model": gc.model,
                    "requires": list(gc.requires),
                    "cadence_basis": sorted(set(self.gc_cadence_basis_series)),
                    "samples": {name: len(series[name]) for name in gc.requires},
                    "usable_samples": {n: pv.n for n, pv in per_series.items()},
                    "status_by_series": {n: pv.status for n, pv in per_series.items()},
                    "min_samples": MIN_TREND_SAMPLES,
                    "poller_snapshots": len(self.snapshots),
                },
            ))
        if self.cfg.gates.get("pools_warm", False):
            gates.append(GateResult(name="pools_warm", passed=pools_warm(latest)))
        gates.extend(self.unimplemented_gates())
        for g in gates:
            # ENFORCED only: `at_seconds` is when the verdict was taken, and the
            # unimplemented records appended just above have no verdict.
            if g.enforced:
                g.at_seconds = at_seconds
        return gates

    def unimplemented_gates(self) -> list[GateResult]:
        """Declared-but-unimplemented spec gates, in spec order."""
        return [
            not_implemented_gate(name)
            for name, enabled in self.cfg.gates.items()
            if enabled and name not in IMPLEMENTED_GATES
        ]

    def all_pass(self, *, trailing: int = FLATNESS_TRAILING_SAMPLES) -> bool:
        """True iff every ENFORCED gate passed (not_implemented gates abstain)."""
        return all(g.passed for g in self.evaluate(trailing=trailing) if g.enforced)
