"""Knee bootstrap tests on synthetic ladder data."""

from __future__ import annotations

import numpy as np

from analysis.knee import LadderData, Rung, estimate_knee, fit_knee_point


def _synthetic_ladder(seed: int = 0, noise: float = 0.5) -> LadderData:
    """A ladder whose true p99 crosses 20 ms between 1000 and 1100 rps.

    p99 grows slowly then steeply near the knee; reps carry small noise.
    """
    rng = np.random.default_rng(seed)
    rate_to_truth = {
        600: 8.0, 700: 9.5, 800: 11.5, 900: 14.5,
        1000: 18.5, 1100: 23.0, 1200: 30.0, 1300: 40.0,
    }
    ladder = LadderData()
    for rate, truth in rate_to_truth.items():
        reps = [float(truth + rng.normal(0, noise)) for _ in range(10)]
        ladder.rungs.append(Rung(rate_rps=float(rate), p99_ms_by_rep=reps))
    return ladder


def test_point_knee_in_expected_bracket():
    ladder = _synthetic_ladder(seed=1, noise=0.3)
    knee = fit_knee_point(ladder, slo_ms=20.0)
    assert knee is not None
    assert 1000.0 < knee < 1100.0  # crossing is between these rungs


def test_bootstrap_ci_brackets_point():
    ladder = _synthetic_ladder(seed=2, noise=0.4)
    est = estimate_knee(ladder, slo_ms=20.0, n_boot=500, seed=99)
    assert est.crossed
    assert est.ci_low_rps <= est.knee_rps <= est.ci_high_rps
    assert est.ci_low_rps < est.ci_high_rps  # nonzero-width interval
    assert 950.0 < est.knee_rps < 1150.0
    # 10-rep synthetic ladder: bracket meets the default min_reps -> CI valid.
    assert est.ci_valid is True
    assert est.min_reps_in_bracket >= 10
    assert est.warning is None


def test_three_rep_ladder_flags_ci_invalid():
    # The exploratory ladder runs reps_per_rung=3; the bootstrap CI must be
    # flagged point-estimate-only, not silently trusted.
    rng = np.random.default_rng(5)
    rate_to_truth = {
        600: 8.0, 800: 11.5, 1000: 18.5, 1100: 23.0, 1200: 30.0,
    }
    ladder = LadderData()
    for rate, truth in rate_to_truth.items():
        reps = [float(truth + rng.normal(0, 0.4)) for _ in range(3)]
        ladder.rungs.append(Rung(rate_rps=float(rate), p99_ms_by_rep=reps))
    est = estimate_knee(ladder, slo_ms=20.0, n_boot=300, seed=1)
    assert est.crossed
    assert est.ci_valid is False
    assert est.min_reps_in_bracket == 3
    assert est.warning is not None and "min_reps" in est.warning
    # bounds are still reported (not faked/widened): a real interval, just not inferential
    assert est.ci_low_rps <= est.knee_rps <= est.ci_high_rps


def test_min_reps_guard_is_tunable():
    # Lowering min_reps to the ladder rep count restores ci_valid: the guard is a
    # parameter, and the runner passes spec/slo.yaml ladder.confirm_reps.
    rng = np.random.default_rng(6)
    rate_to_truth = {600: 8.0, 800: 11.5, 1000: 18.5, 1100: 23.0, 1200: 30.0}
    ladder = LadderData()
    for rate, truth in rate_to_truth.items():
        reps = [float(truth + rng.normal(0, 0.4)) for _ in range(3)]
        ladder.rungs.append(Rung(rate_rps=float(rate), p99_ms_by_rep=reps))
    est = estimate_knee(ladder, slo_ms=20.0, n_boot=200, seed=1, min_reps=3)
    assert est.ci_valid is True
    assert est.warning is None


def test_ci_is_deterministic_given_seed():
    ladder = _synthetic_ladder(seed=3, noise=0.4)
    a = estimate_knee(ladder, n_boot=300, seed=7)
    b = estimate_knee(ladder, n_boot=300, seed=7)
    assert (a.ci_low_rps, a.knee_rps, a.ci_high_rps) == (b.ci_low_rps, b.knee_rps, b.ci_high_rps)


def test_sub_knee_ladder_reports_not_crossed():
    # All rungs below the SLO: no crossing in range.
    ladder = LadderData(rungs=[
        Rung(500.0, [5.0] * 10), Rung(600.0, [6.0] * 10), Rung(700.0, [7.0] * 10),
    ])
    est = estimate_knee(ladder, slo_ms=20.0, n_boot=200)
    assert est.crossed is False
    assert est.knee_rps >= 700.0  # reported as a lower bound (top rate)


def test_needs_two_rungs():
    import pytest

    with pytest.raises(ValueError):
        estimate_knee(LadderData(rungs=[Rung(500.0, [5.0])]))


def test_non_crossing_resamples_censor_the_upper_bound_instead_of_vanishing():
    """A marginal top rung (mean ~SLO) makes many resamples never cross in-range.

    Dropping those resamples took the 97.5th percentile over the crossing
    SUBSET, interpolating an upper bound BELOW the top rung while a real mass
    of resamples said "beyond the ladder" -- truncation exactly in the ladder's
    normal stopping geometry. They are right-censored now: when the upper
    percentile lands in the censored mass, ci_high_rps is the top rung and
    ci_high_censored says so, and n_crossed makes the drop visible.
    """
    rng = np.random.default_rng(11)
    ladder = LadderData(rungs=[
        Rung(140.0, [float(10.0 + rng.normal(0, 1.5)) for _ in range(10)]),
        Rung(160.0, [float(13.0 + rng.normal(0, 1.5)) for _ in range(10)]),
        Rung(180.0, [float(16.0 + rng.normal(0, 1.5)) for _ in range(10)]),
        Rung(200.0, [float(20.1 + rng.normal(0, 1.5)) for _ in range(10)]),
    ])
    est = estimate_knee(ladder, slo_ms=20.0, n_boot=2000, seed=5)
    assert 0 < est.n_crossed < est.n_boot          # a real censored mass exists
    assert est.n_boot - est.n_crossed > 0.025 * est.n_boot   # enough to reach q=.975
    assert est.ci_high_censored is True
    assert est.ci_high_rps == 200.0                # the top rung, read as ">="
    # the point estimate and lower bound still resolve inside the ladder
    assert est.crossed is True
    assert est.ci_low_rps <= est.knee_rps <= est.ci_high_rps


def test_fully_crossing_bootstrap_is_uncensored():
    ladder = LadderData(rungs=[
        Rung(500.0, [10.0] * 10), Rung(600.0, [30.0] * 10),
    ])
    est = estimate_knee(ladder, slo_ms=20.0, n_boot=200)
    assert est.n_crossed == est.n_boot == 200
    assert est.ci_high_censored is False
