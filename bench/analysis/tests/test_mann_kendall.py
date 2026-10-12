"""Mann-Kendall tests on known trends + the practical-significance gate band."""

from __future__ import annotations

import numpy as np

from analysis.mann_kendall import (
    DEFAULT_MIN_SLOPE_MS_PER_MIN,
    mann_kendall,
    min_slope_from_slo,
    stationarity_gate_fails,
    theil_sen_slope,
    theil_sen_slope_ms_per_min,
)


def test_strict_increase_is_max_positive_trend():
    mk = mann_kendall(list(range(1, 21)))
    # All pairs concordant: S = n(n-1)/2, tau = 1.
    assert mk.s == 20 * 19 // 2
    assert mk.tau == 1.0
    assert mk.has_positive_trend(0.05)
    assert not mk.no_positive_trend(0.05)


def test_strict_decrease_no_positive_trend():
    mk = mann_kendall(list(range(20, 0, -1)))
    assert mk.s == -(20 * 19 // 2)
    assert mk.tau == -1.0
    assert not mk.has_positive_trend(0.05)
    assert mk.no_positive_trend(0.05)
    assert mk.p_decreasing < 0.05


def test_flat_series_is_stationary():
    mk = mann_kendall([5.0] * 30)
    assert mk.s == 0
    assert not mk.has_positive_trend(0.05)
    assert mk.no_positive_trend(0.05)


def test_noisy_flat_has_no_significant_positive_trend():
    rng = np.random.default_rng(11)
    series = 10.0 + rng.normal(0, 1.0, size=120)  # stationary p99-like series
    mk = mann_kendall(series)
    assert mk.no_positive_trend(0.05)


def test_weak_upward_drift_detected():
    rng = np.random.default_rng(3)
    n = 120
    series = 10.0 + 0.05 * np.arange(n) + rng.normal(0, 0.5, size=n)
    mk = mann_kendall(series)
    assert mk.has_positive_trend(0.05)


def test_short_series_neutral():
    mk = mann_kendall([1.0, 2.0])
    assert mk.s == 0 and mk.no_positive_trend(0.05)


def test_tie_correction_runs():
    # Series with ties must not divide-by-zero and must give a sane result.
    mk = mann_kendall([1, 1, 2, 2, 3, 3, 4, 4])
    assert mk.var_s > 0
    assert mk.has_positive_trend(0.05)


def test_theil_sen_recovers_known_slope():
    # y = 2 + 0.5*t (per-second). Slope per second = 0.5; per minute = 30.
    t = np.arange(120, dtype=float)
    y = 2.0 + 0.5 * t
    assert abs(theil_sen_slope(y) - 0.5) < 1e-9
    assert abs(theil_sen_slope_ms_per_min(y) - 30.0) < 1e-9


def test_theil_sen_robust_to_tail_outliers():
    # A few huge p99 spikes must not swing the Theil-Sen slope (median-based).
    rng = np.random.default_rng(1)
    t = np.arange(120, dtype=float)
    y = 10.0 + 0.001 * t + rng.normal(0, 0.2, size=120)
    y[50] += 500.0  # tail spike
    y[90] += 800.0
    slope = theil_sen_slope_ms_per_min(y)
    assert abs(slope) < 1.0  # near-flat despite the spikes


def test_gate_passes_on_tiny_significant_drift():
    # Significant MK but negligible slope (< band): the gate must NOT fail.
    rng = np.random.default_rng(3)
    n = 120
    series = 10.0 + 0.005 * np.arange(n) + rng.normal(0, 0.05, size=n)
    mk = mann_kendall(series)
    assert mk.has_positive_trend(0.05)  # MK flags it
    slope = theil_sen_slope_ms_per_min(series)
    assert slope < DEFAULT_MIN_SLOPE_MS_PER_MIN  # but practically negligible
    res = stationarity_gate_fails(series)
    assert res.mk_significant is True
    assert res.fails is False  # band saves it from a false non-stationary verdict


def test_gate_fails_on_real_drift():
    # Significant MK AND a real slope above the band: the gate must fail.
    rng = np.random.default_rng(4)
    n = 120
    series = 10.0 + 0.1 * np.arange(n) + rng.normal(0, 0.3, size=n)  # 6 ms/min
    res = stationarity_gate_fails(series)
    assert res.mk_significant is True
    assert res.slope_ms_per_min > DEFAULT_MIN_SLOPE_MS_PER_MIN
    assert res.fails is True


def test_gate_passes_on_flat_series():
    rng = np.random.default_rng(5)
    series = 10.0 + rng.normal(0, 0.3, size=120)
    res = stationarity_gate_fails(series)
    assert res.fails is False


def test_min_slope_from_slo_reads_spec_then_default():
    assert min_slope_from_slo({"min_slope_ms_per_min": 2.5}) == 2.5
    assert min_slope_from_slo(None) == DEFAULT_MIN_SLOPE_MS_PER_MIN
    assert min_slope_from_slo({}) == DEFAULT_MIN_SLOPE_MS_PER_MIN


def test_gate_reads_pinned_threshold_from_slo():
    # The pinned spec threshold flows through to the gate.
    from ledgerbench import config

    slo = config.load_slo()
    band = min_slope_from_slo(slo.stationarity)
    assert band == 1.0  # spec/slo.yaml stationarity.min_slope_ms_per_min
