"""Mann-Kendall trend test, implemented directly (no extra dep).

Used by (a) the warmup controller flatness gate and (b) the fit stationarity gate
(spec/slo.yaml stationarity: per-second p99 series must show NO positive trend,
alpha 0.05). We implement S, the tie-corrected variance, the continuity-corrected
z, and the two-sided / one-sided p-value directly on numpy.

For the benchmark we care about the ONE-SIDED test "no positive trend":
positive trend present  <=>  z > 0 and p_one_sided_increasing < alpha.

PRACTICAL-SIGNIFICANCE BAND (stationarity gate only). On a 120-second per-second
p99 series the continuity-corrected MK test will flag a statistically significant
but PRACTICALLY NEGLIGIBLE positive drift (a fraction of a ms across the window),
which near the knee biases the knee estimate LOW. So the stationarity gate
(`stationarity_gate_fails`) fails ONLY when BOTH hold: MK is significant AND the
Theil-Sen slope over the window exceeds `min_slope_ms_per_min`. The threshold is
pre-registered: read from spec/slo.yaml `stationarity.min_slope_ms_per_min` when
present, else the pinned default `DEFAULT_MIN_SLOPE_MS_PER_MIN = 1.0` ms/min
(~0.017 ms/s drift; below this the open-loop p99 wobble is window-length noise,
not real degradation). Theil-Sen (median of pairwise slopes) is used instead of OLS
because it is robust to the heavy upper tail of p99 series; O(n^2) on 120 points
is trivial.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

# Pre-registered practical-significance threshold for the stationarity gate. A
# per-second p99 series may show a statistically significant but negligible drift;
# the gate fails only above this Theil-Sen slope. 1.0 ms/min == ~0.017 ms/s.
DEFAULT_MIN_SLOPE_MS_PER_MIN = 1.0


@dataclass(frozen=True)
class MannKendall:
    n: int
    s: int
    var_s: float
    z: float
    p_two_sided: float
    p_increasing: float        # one-sided: H1 = upward trend
    p_decreasing: float        # one-sided: H1 = downward trend
    tau: float

    def has_positive_trend(self, alpha: float = 0.05) -> bool:
        """True if a statistically significant UPWARD trend is present."""
        return self.z > 0 and self.p_increasing < alpha

    def no_positive_trend(self, alpha: float = 0.05) -> bool:
        """The stationarity gate predicate (spec/slo.yaml direction)."""
        return not self.has_positive_trend(alpha)


def _normal_sf(z: float) -> float:
    """Survival function P(Z > z) for the standard normal via erfc."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def mann_kendall(series: NDArray[np.float64] | list[float]) -> MannKendall:
    """Compute the Mann-Kendall statistics on a 1-D time-ordered series."""
    x = np.asarray(series, dtype=np.float64)
    n = x.shape[0]
    if n < 3:
        # Undefined trend on < 3 points; report a neutral result (no trend).
        return MannKendall(n=n, s=0, var_s=0.0, z=0.0, p_two_sided=1.0,
                           p_increasing=1.0, p_decreasing=1.0, tau=0.0)

    # S = sum_{i<j} sign(x_j - x_i). Vectorized over the upper triangle.
    diff = np.subtract.outer(x, x)            # diff[j, i] = x[j] - x[i]
    tri = np.triu_indices(n, k=1)
    s = int(np.sum(np.sign(diff[tri[1], tri[0]])))  # sign(x_j - x_i), i<j

    # Tie correction for the variance.
    _, counts = np.unique(x, return_counts=True)
    tie_term = float(np.sum(counts * (counts - 1) * (2 * counts + 5)))
    var_s = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0

    if var_s <= 0:
        z = 0.0
    elif s > 0:
        z = (s - 1) / math.sqrt(var_s)         # continuity correction
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s)
    else:
        z = 0.0

    p_increasing = _normal_sf(z)               # P(Z > z): upward trend
    p_decreasing = _normal_sf(-z)              # P(Z < z)
    p_two_sided = 2.0 * _normal_sf(abs(z))
    p_two_sided = min(1.0, p_two_sided)

    # Kendall's tau (denominator excludes ties; here the simple n(n-1)/2 form).
    denom = n * (n - 1) / 2.0
    tau = s / denom if denom > 0 else 0.0

    return MannKendall(
        n=n, s=s, var_s=var_s, z=z,
        p_two_sided=p_two_sided, p_increasing=p_increasing,
        p_decreasing=p_decreasing, tau=tau,
    )


def theil_sen_slope(series: NDArray[np.float64] | list[float], *, dt_seconds: float = 1.0) -> float:
    """Theil-Sen slope of `series` in VALUE-UNITS per SECOND.

    Median of the pairwise slopes (y_j - y_i) / ((j - i) * dt_seconds) over all
    i<j. Robust to the heavy upper tail of a p99 series (unlike OLS). `dt_seconds`
    is the spacing between consecutive samples (1.0 for a per-second series).
    Returns 0.0 for series shorter than 2 points. O(n^2); fine for ~120 points.
    """
    y = np.asarray(series, dtype=np.float64)
    n = y.shape[0]
    if n < 2:
        return 0.0
    i, j = np.triu_indices(n, k=1)
    dt = (j - i).astype(np.float64) * dt_seconds
    slopes = (y[j] - y[i]) / dt
    return float(np.median(slopes))


def theil_sen_slope_ms_per_min(
    series: NDArray[np.float64] | list[float], *, dt_seconds: float = 1.0
) -> float:
    """Theil-Sen slope expressed in ms per MINUTE (series is per-second p99 in ms)."""
    return theil_sen_slope(series, dt_seconds=dt_seconds) * 60.0


@dataclass(frozen=True)
class StationarityResult:
    fails: bool                 # True => series is NON-stationary (gate fails)
    mk_significant: bool        # MK one-sided upward trend significant at alpha
    slope_ms_per_min: float     # Theil-Sen slope over the window
    min_slope_ms_per_min: float # the pre-registered practical-significance band
    alpha: float


def stationarity_gate_fails(
    p99_series_ms: NDArray[np.float64] | list[float],
    *,
    alpha: float = 0.05,
    min_slope_ms_per_min: float = DEFAULT_MIN_SLOPE_MS_PER_MIN,
    dt_seconds: float = 1.0,
) -> StationarityResult:
    """Stationarity gate with a pre-registered practical-significance band.

    The gate FAILS (series deemed non-stationary, biasing the knee low) only when
    BOTH conditions hold:
      1. Mann-Kendall finds a statistically significant upward trend (alpha), AND
      2. the Theil-Sen slope exceeds `min_slope_ms_per_min`.
    A tiny-but-significant drift (condition 1 alone) is NOT a failure: that is the
    window-length artifact the band is designed to ignore. Pass
    `min_slope_ms_per_min` from spec/slo.yaml stationarity (see
    `min_slope_from_slo`), or rely on the pinned default.
    """
    mk = mann_kendall(p99_series_ms)
    mk_sig = mk.has_positive_trend(alpha)
    slope = theil_sen_slope_ms_per_min(p99_series_ms, dt_seconds=dt_seconds)
    fails = mk_sig and slope > min_slope_ms_per_min
    return StationarityResult(
        fails=fails, mk_significant=mk_sig, slope_ms_per_min=slope,
        min_slope_ms_per_min=min_slope_ms_per_min, alpha=alpha,
    )


def min_slope_from_slo(stationarity_cfg: dict | None) -> float:
    """Pre-registered slope band from spec/slo.yaml `stationarity`, else the default.

    Reads `stationarity.min_slope_ms_per_min`; if the section or key is absent the
    pinned `DEFAULT_MIN_SLOPE_MS_PER_MIN` is used (documented in the module
    docstring). Keeps the threshold pinned in the spec when it carries the knob.
    """
    if stationarity_cfg and "min_slope_ms_per_min" in stationarity_cfg:
        return float(stationarity_cfg["min_slope_ms_per_min"])
    return DEFAULT_MIN_SLOPE_MS_PER_MIN
