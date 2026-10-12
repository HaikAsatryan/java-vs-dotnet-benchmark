"""Thin bridge from the runner to bench/analysis.

The analysis package depends on numpy/scipy. The runner core stays import-light: these wrappers import the
analysis modules LAZILY inside each call so the no-fit / no-crossing paths (and
the docker-free orchestration unit tests) never pay the numpy import cost. The
wrappers consume the analysis modules' REAL APIs (analysis.knee.estimate_knee,
analysis.mann_kendall.stationarity_gate_fails) verbatim: no reimplementation.
"""

from __future__ import annotations


def estimate_knee_from_rungs(
    rungs_by_rate: dict[int, list[float]],
    *,
    slo_ms: float = 20.0,
    min_reps: int = 10,
):
    """Wrap analysis.knee.estimate_knee over a {rate: [per-rep p99 ms]} map.

    Returns the real `KneeEstimate` (knee_rps, ci_low/high_rps, ci_valid, warning,
    crossed). Respects estimate_knee's min_reps/ci_valid semantics: an
    under-repped crossing bracket comes back ci_valid=False with a warning, never
    a fabricated CI.
    """
    from analysis.knee import LadderData, Rung, estimate_knee

    ladder = LadderData(
        rungs=[Rung(rate_rps=float(rate), p99_ms_by_rep=list(reps))
               for rate, reps in sorted(rungs_by_rate.items())]
    )
    return estimate_knee(ladder, slo_ms=slo_ms, min_reps=min_reps)


def stationarity_fails(
    p99_series_ms: list[float], *, min_slope_ms_per_min: float = 1.0,
    alpha: float = 0.05,
) -> bool:
    """Wrap analysis.mann_kendall.stationarity_gate_fails (per-second p99 -> bool).

    True => the series is non-stationary by the pre-registered rule (MK
    significant at `alpha` AND Theil-Sen slope over the window exceeds the
    slo-pinned band). `alpha` is threaded from `spec/slo.yaml stationarity.alpha`
    by both callers, so the spec key is the value in force.
    """
    from analysis.mann_kendall import stationarity_gate_fails

    return stationarity_gate_fails(
        p99_series_ms, min_slope_ms_per_min=min_slope_ms_per_min, alpha=alpha,
    ).fails


def knee_crosses(rung_means, slo_ms: float):
    """THE crossing predicate, bridged: `analysis.knee.crosses` over rung means.

    Here for the same reason `estimate_knee_from_rungs` is: `ledgerbench` reaches
    `analysis` through this one module, so the runner and the analyzer cannot end
    up importing two different definitions of the same rule.
    """
    from analysis.knee import crosses

    return crosses(rung_means, slo_ms)
