"""Isotonic regression via the Pool-Adjacent-Violators Algorithm (PAVA).

In-repo, on numpy, NO sklearn: the relationship
p99(rate) is monotone non-decreasing physically but not a known parametric form,
so we fit with the constraint that is EXACTLY true (monotonicity) and nothing
more. Isotonic regression is non-parametric and robust at the high-variance knee.

`isotonic_fit(y, w)` returns the monotone-non-decreasing least-squares fit g
minimizing sum w_i (y_i - g_i)^2 subject to g_1 <= g_2 <= ... <= g_n. Inputs are
assumed already sorted by the independent variable (rate). Weights default to 1.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def isotonic_fit(
    y: NDArray[np.float64] | list[float],
    w: NDArray[np.float64] | list[float] | None = None,
) -> NDArray[np.float64]:
    """PAVA monotone-non-decreasing fit. O(n). Pure (no mutation of inputs)."""
    y_arr = np.asarray(y, dtype=np.float64)
    n = y_arr.shape[0]
    if n == 0:
        return y_arr.copy()
    if w is None:
        w_arr = np.ones(n, dtype=np.float64)
    else:
        w_arr = np.asarray(w, dtype=np.float64)
        if w_arr.shape[0] != n:
            raise ValueError("weights length must match y")
        if np.any(w_arr <= 0):
            raise ValueError("weights must be positive")

    # Block representation: each block holds (weighted) mean, total weight, count.
    # Merge adjacent blocks while the monotonicity constraint is violated.
    means: list[float] = []
    weights: list[float] = []
    counts: list[int] = []
    for i in range(n):
        means.append(float(y_arr[i]))
        weights.append(float(w_arr[i]))
        counts.append(1)
        # Pool with the previous block while it is greater (violates non-decrease).
        while len(means) > 1 and means[-2] > means[-1]:
            w2 = weights[-1] + weights[-2]
            m2 = (means[-1] * weights[-1] + means[-2] * weights[-2]) / w2
            c2 = counts[-1] + counts[-2]
            means[-2:] = [m2]
            weights[-2:] = [w2]
            counts[-2:] = [c2]

    # Expand block means back to per-point fitted values.
    out = np.empty(n, dtype=np.float64)
    idx = 0
    for m, c in zip(means, counts):
        out[idx : idx + c] = m
        idx += c
    return out


def isotonic_predict(
    x_fit: NDArray[np.float64] | list[float],
    g_fit: NDArray[np.float64] | list[float],
    x_query: float,
) -> float:
    """Linear interpolation of the (already monotone) fit at `x_query`.

    Clamps to the endpoints outside the fitted range. Used by knee.py to read the
    SLO crossing off the isotonic fit at arbitrary rates.
    """
    xf = np.asarray(x_fit, dtype=np.float64)
    gf = np.asarray(g_fit, dtype=np.float64)
    if xf.shape[0] == 0:
        raise ValueError("empty fit")
    if x_query <= xf[0]:
        return float(gf[0])
    if x_query >= xf[-1]:
        return float(gf[-1])
    return float(np.interp(x_query, xf, gf))
