"""PCG64 determinism + distribution sanity (bench/NOTES.md ("PRNG: hand-rolled PCG64"))."""

from __future__ import annotations

from ledgerbench.util.pcg64 import PCG64


def test_same_seed_identical_stream():
    a = PCG64(12345)
    b = PCG64(12345)
    seq_a = [a.next_u64() for _ in range(1000)]
    seq_b = [b.next_u64() for _ in range(1000)]
    assert seq_a == seq_b


def test_different_seed_different_stream():
    a = [PCG64(1).next_u64() for _ in range(64)]
    b = [PCG64(2).next_u64() for _ in range(64)]
    assert a != b


def test_u64_in_range():
    rng = PCG64(7)
    for _ in range(10_000):
        v = rng.next_u64()
        assert 0 <= v < (1 << 64)


def test_random_in_unit_interval():
    rng = PCG64(99)
    for _ in range(10_000):
        u = rng.random()
        assert 0.0 <= u < 1.0


def test_randint_bounds_and_coverage():
    rng = PCG64(3)
    seen = set()
    for _ in range(20_000):
        v = rng.randint(1, 10)
        assert 1 <= v <= 10
        seen.add(v)
    # With 20k draws over 10 values every value should appear.
    assert seen == set(range(1, 11))


def test_randint_single_value():
    rng = PCG64(5)
    assert all(rng.randint(4, 4) == 4 for _ in range(100))


def test_randint_roughly_uniform():
    rng = PCG64(2024)
    counts = [0] * 6
    n = 60_000
    for _ in range(n):
        counts[rng.randint(0, 5)] += 1
    expected = n / 6
    # within 5% of expected for each bucket
    assert all(abs(c - expected) < expected * 0.05 for c in counts)
