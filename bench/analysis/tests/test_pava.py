"""PAVA isotonic regression tests on synthetic monotone+noise data."""

from __future__ import annotations

import numpy as np

from analysis.pava import isotonic_fit, isotonic_predict


def test_already_monotone_is_unchanged():
    y = [1.0, 2.0, 3.0, 4.0]
    np.testing.assert_allclose(isotonic_fit(y), y)


def test_pools_violators_to_mean():
    # [1, 3, 2, 4]: the 3,2 violation pools to mean 2.5.
    np.testing.assert_allclose(isotonic_fit([1.0, 3.0, 2.0, 4.0]), [1.0, 2.5, 2.5, 4.0])


def test_strictly_decreasing_becomes_constant_mean():
    y = [4.0, 3.0, 2.0, 1.0]
    out = isotonic_fit(y)
    np.testing.assert_allclose(out, np.full(4, 2.5))


def test_output_is_monotone_nondecreasing_on_noisy_monotone():
    rng = np.random.default_rng(7)
    x = np.linspace(0, 10, 200)
    truth = 2.0 * x
    noisy = truth + rng.normal(0, 3.0, size=x.size)
    fit = isotonic_fit(noisy)
    assert np.all(np.diff(fit) >= -1e-9), "fit must be non-decreasing"
    # The monotone fit should track the truth better than the raw noise (MSE).
    assert np.mean((fit - truth) ** 2) < np.mean((noisy - truth) ** 2)


def test_already_monotone_with_weights_unchanged():
    # 1 < 5 already non-decreasing; weights don't force pooling.
    np.testing.assert_allclose(isotonic_fit([1.0, 5.0], [1.0, 1.0]), [1.0, 5.0])


def test_weighted_fit_respects_weights():
    # A violation (5 then 1) pools to the WEIGHTED mean, not the plain mean.
    out = isotonic_fit([5.0, 1.0], [1.0, 3.0])
    expected_mean = (5.0 * 1 + 1.0 * 3) / 4  # = 2.0, pulled toward the heavy point
    np.testing.assert_allclose(out, np.full(2, expected_mean))


def test_predict_interpolates_and_clamps():
    xf = [0.0, 10.0, 20.0]
    gf = [0.0, 5.0, 5.0]
    assert isotonic_predict(xf, gf, -1.0) == 0.0       # clamp low
    assert isotonic_predict(xf, gf, 25.0) == 5.0       # clamp high
    assert isotonic_predict(xf, gf, 5.0) == 2.5        # interpolate


def test_empty_input():
    assert isotonic_fit([]).size == 0
