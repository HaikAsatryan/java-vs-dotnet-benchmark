"""Zipf CDF correctness + determinism (bench/NOTES.md ("Zipf"), spec/workload.yaml)."""

from __future__ import annotations

import math

from ledgerbench.util.pcg64 import PCG64
from ledgerbench.zipf import build_zipf_table


def test_cdf_monotone_and_normalized():
    t = build_zipf_table(1000, alpha=1.0)
    assert t.cdf[-1] == 1.0
    assert all(t.cdf[i] <= t.cdf[i + 1] for i in range(len(t.cdf) - 1))
    assert t.cdf[0] > 0.0


def test_probability_matches_harmonic_weights():
    # For alpha=1.0, P(rank=k) = (1/k) / H(N), H(N) = sum_{j=1..N} 1/j.
    n = 50
    t = build_zipf_table(n, alpha=1.0)
    h = sum(1.0 / j for j in range(1, n + 1))
    for k in (1, 2, 7, 25, 50):
        expected = (1.0 / k) / h
        assert math.isclose(t.probability(k), expected, rel_tol=1e-9, abs_tol=1e-12)


def test_rank1_is_hottest():
    t = build_zipf_table(1000, alpha=1.0)
    p1 = t.probability(1)
    assert all(p1 >= t.probability(k) for k in range(2, 100))


def test_sampling_ids_within_range():
    t = build_zipf_table(500, alpha=1.0)
    rng = PCG64(1)
    for _ in range(20_000):
        i = t.sample_id(rng)
        assert 1 <= i <= 500


def test_sampling_deterministic():
    t = build_zipf_table(1000, alpha=1.0)
    a = [t.sample_id(PCG64(42)) for _ in range(1)]  # fresh rng each -> same first draw
    b = [t.sample_id(PCG64(42)) for _ in range(1)]
    assert a == b
    # whole stream with one rng
    ra, rb = PCG64(7), PCG64(7)
    sa = [t.sample_id(ra) for _ in range(5000)]
    sb = [t.sample_id(rb) for _ in range(5000)]
    assert sa == sb


def test_empirical_hot_rank_dominates():
    # Rank 1 should be drawn far more often than rank 100 over many samples.
    t = build_zipf_table(1000, alpha=1.0)
    rng = PCG64(2024)
    counts = {}
    n = 100_000
    for _ in range(n):
        i = t.sample_id(rng)
        counts[i] = counts.get(i, 0) + 1
    # Empirical freq of rank 1 within 10% of its analytic probability.
    p1 = t.probability(1)
    emp1 = counts.get(1, 0) / n
    assert abs(emp1 - p1) < p1 * 0.1
    assert counts.get(1, 0) > counts.get(100, 0)


def test_alpha_zero_is_uniform():
    t = build_zipf_table(10, alpha=0.0)
    # uniform: every probability == 0.1
    for k in range(1, 11):
        assert math.isclose(t.probability(k), 0.1, abs_tol=1e-12)
