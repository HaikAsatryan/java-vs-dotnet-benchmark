"""Finite-support inverse-CDF Zipf(alpha) over ranks 1..N.

spec/workload.yaml is explicit: alpha is the rank exponent s (weight of rank k
proportional to 1/k**alpha). The reference generator MUST use an explicit
finite-support inverse-CDF Zipf over ranks 1..N to sidestep the
numpy.random.zipf(a=s+1) parameterization ambiguity. alpha=1.0 here.

Rank 1 maps to id 1 (the hottest), rank N to id N. The same construction is
reused by the seeder warm-touch and the cache-hit construction, so the hot-set
is identical at the *distribution* level across all three consumers.

Implementation:
  - Precompute the cumulative distribution `cdf[k] = H(k) / H(N)` where
    H(k) = sum_{j=1..k} 1/j**alpha (generalized harmonic number).
  - Sampling: draw u in [0,1), binary-search the first rank whose cdf >= u.
    This is the inverse-CDF method, O(log N) per draw, exact, allocation-free.
For large N (invoices: 5e6) the CDF is ~40 MB of float64; acceptable for the
runner host. Build once, reuse for every draw in a targets file.
"""

from __future__ import annotations

from dataclasses import dataclass

from .util.pcg64 import PCG64


@dataclass
class ZipfTable:
    """Precomputed finite-support Zipf CDF over ranks 1..n (== ids 1..n)."""

    n: int
    alpha: float
    cdf: list[float]  # length n; cdf[i] is P(rank <= i+1); cdf[-1] == 1.0

    def sample_id(self, rng: PCG64) -> int:
        """Draw an id in 1..n. rank 1 (id 1) is hottest."""
        u = rng.random()
        rank0 = _lower_bound(self.cdf, u)
        return rank0 + 1  # rank is 1-based; id == rank

    def probability(self, rank: int) -> float:
        """Exact P(draw == rank) for 1 <= rank <= n (for tests/assertions)."""
        if rank < 1 or rank > self.n:
            raise ValueError("rank out of range")
        if rank == 1:
            return self.cdf[0]
        return self.cdf[rank - 1] - self.cdf[rank - 2]


def build_zipf_table(n: int, alpha: float = 1.0) -> ZipfTable:
    """Build the finite-support Zipf CDF for ranks 1..n.

    Uses Kahan-style compensated summation so the harmonic accumulation does not
    drift for large n, keeping the table deterministic across runs/platforms.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    weights: list[float] = [0.0] * n
    total = 0.0
    comp = 0.0  # Kahan compensation
    for i in range(n):
        rank = i + 1
        w = 1.0 / (rank ** alpha) if alpha != 0.0 else 1.0
        weights[i] = w
        y = w - comp
        t = total + y
        comp = (t - total) - y
        total = t
    # Cumulative, normalized. Force the last entry to exactly 1.0 so a u very
    # close to 1.0 always resolves to rank n (no out-of-range from rounding).
    cdf: list[float] = [0.0] * n
    running = 0.0
    inv = 1.0 / total
    for i in range(n):
        running += weights[i]
        cdf[i] = running * inv
    cdf[-1] = 1.0
    return ZipfTable(n=n, alpha=alpha, cdf=cdf)


def _lower_bound(cdf: list[float], u: float) -> int:
    """First index i with cdf[i] > u (binary search). Returns 0..len-1."""
    lo, hi = 0, len(cdf) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if u < cdf[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo
