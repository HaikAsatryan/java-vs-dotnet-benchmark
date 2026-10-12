"""Paired-difference CI + +-5% margin verdict tests on synthetic data."""

from __future__ import annotations

import numpy as np

from analysis.paired import paired_difference


def test_tie_when_within_margin():
    rng = np.random.default_rng(1)
    reference = 100.0 + rng.normal(0, 1.0, size=12)
    subject = reference + rng.normal(0, 0.5, size=12)  # ~0% difference
    r = paired_difference(subject, reference, metric="cpu_ms_per_req", margin_pct=5.0)
    assert r.is_tie
    assert -5.0 <= r.ci_low_pct and r.ci_high_pct <= 5.0


def test_verdict_method_is_always_t():
    # Pre-registered: the verdict path is the paired-t interval unconditionally,
    # never auto-switched to bootstrap on a normality screen.
    rng = np.random.default_rng(7)
    reference = 100.0 + rng.normal(0, 1.0, size=12)
    subject = reference + rng.normal(0, 0.5, size=12)
    r = paired_difference(subject, reference, metric="cpu_ms_per_req")
    assert r.method == "t"


def test_diagnostics_reported_but_off_verdict_path():
    # Shapiro p + bootstrap interval are surfaced as diagnostics, and the verdict
    # CI is the t-interval (distinct from the reported bootstrap interval object).
    rng = np.random.default_rng(8)
    reference = 100.0 + rng.normal(0, 1.0, size=12)
    subject = 90.0 + rng.normal(0, 1.0, size=12)
    r = paired_difference(subject, reference, metric="cpu_ms_per_req")
    assert r.shapiro_p is not None and 0.0 <= r.shapiro_p <= 1.0
    # bootstrap interval is populated and brackets the mean diff %
    assert r.boot_ci_low_pct <= r.mean_diff_pct <= r.boot_ci_high_pct
    # verdict CI is the t-interval; it is what the margin test uses
    assert r.ci_low_pct <= r.mean_diff_pct <= r.ci_high_pct


def test_subject_better_when_clearly_lower_cost():
    rng = np.random.default_rng(2)
    reference = 100.0 + rng.normal(0, 1.0, size=12)
    subject = 80.0 + rng.normal(0, 1.0, size=12)  # ~20% lower (better for cost)
    r = paired_difference(subject, reference, metric="cpu_ms_per_req",
                          lower_is_better=True)
    assert r.verdict == "subject_better"
    assert r.ci_high_pct < -5.0  # entirely below the lower margin


def test_reference_better_when_subject_costs_more():
    rng = np.random.default_rng(4)
    reference = 100.0 + rng.normal(0, 1.0, size=12)
    subject = 120.0 + rng.normal(0, 1.0, size=12)
    r = paired_difference(subject, reference, metric="cpu_ms_per_req",
                          lower_is_better=True)
    assert r.verdict == "reference_better"


def test_throughput_direction_flips():
    # For a higher-is-better metric (knee RPS), a higher subject is better.
    rng = np.random.default_rng(5)
    reference = 1000.0 + rng.normal(0, 5.0, size=12)
    subject = 1200.0 + rng.normal(0, 5.0, size=12)
    r = paired_difference(subject, reference, metric="knee_rps",
                          lower_is_better=False)
    assert r.verdict == "subject_better"


def test_the_denominator_is_the_reference_not_the_subject():
    """The declared headline is subject:reference; a flipped call must flip the %.

    `registry.SlotSet.reference` exists to settle this direction once. Round 1
    computed `.NET : Go` while its own plan declared the opposite, so a
    mechanical rename would have published the reciprocal -- a number that reads
    plausibly and is wrong.
    """
    subject = [120.0] * 8
    reference = [100.0] * 8
    r = paired_difference(subject, reference, metric="cpu_ms_per_req")
    assert abs(r.mean_diff_pct - 20.0) < 1e-9      # 20% ABOVE the reference
    assert r.mean_reference == 100.0 and r.mean_subject == 120.0

    back = paired_difference(reference, subject, metric="cpu_ms_per_req")
    assert abs(back.mean_diff_pct - (-100.0 / 6.0)) < 1e-9


def test_pairing_alignment_required():
    import pytest

    with pytest.raises(ValueError):
        paired_difference([1.0, 2.0], [1.0], metric="x")


def test_zero_variance_diffs_use_t_path_with_collapsed_ci():
    # Constant offset => zero-variance diffs => Shapiro undefined (None) and the
    # t-interval collapses to the point estimate; verdict still falls out cleanly.
    subject = [110.0] * 10
    reference = [100.0] * 10
    r = paired_difference(subject, reference, metric="cpu_ms_per_req")
    assert r.method == "t"
    assert r.shapiro_p is None  # undefined on zero-variance diffs
    assert abs(r.mean_diff_pct - 10.0) < 1e-9
    assert abs(r.ci_low_pct - 10.0) < 1e-9 and abs(r.ci_high_pct - 10.0) < 1e-9
    assert r.verdict == "reference_better"  # the subject is 10% dearer


def test_effect_size_reported():
    rng = np.random.default_rng(6)
    reference = 100.0 + rng.normal(0, 1.0, size=20)
    subject = 90.0 + rng.normal(0, 1.0, size=20)
    r = paired_difference(subject, reference, metric="cpu_ms_per_req")
    assert abs(r.effect_size) > 1.0  # large, well-separated effect


def test_a_ci_that_spans_zero_and_exits_the_band_is_inconclusive():
    """R1-A5's shape: the interval straddles 0 AND leaves the +-margin band.

    The old rule fell through to the point-estimate sign and declared a winner
    from an interval consistent with either side being better. Neither
    equivalence nor direction is established there, and the verdict token has
    to say so.
    """
    rng = np.random.default_rng(9)
    reference = [10.0 + rng.normal(0, 0.5) for _ in range(6)]
    # diffs are noisy around a small positive mean: CI like [-x, +y] with
    # x, y both > margin-relative bounds
    subject = [r + rng.normal(0.3, 3.0) for r in reference]
    r = paired_difference(subject, reference, metric="cpu_ms_per_req",
                          margin_pct=5.0)
    assert r.ci_low_pct < 0.0 < r.ci_high_pct
    assert not (r.ci_low_pct >= -5.0 and r.ci_high_pct <= 5.0)
    assert r.verdict == "inconclusive"


def test_a_ci_excluding_zero_still_gets_a_directional_verdict():
    reference = [10.0, 10.1, 9.9, 10.0, 10.2]
    subject = [12.0, 12.1, 11.9, 12.0, 12.2]      # ~+20%, tight
    r = paired_difference(subject, reference, metric="cpu_ms_per_req",
                          margin_pct=5.0)
    assert r.ci_low_pct > 0.0
    assert r.verdict == "reference_better"
