package gen

import "math/rand/v2"

// Zipf is a finite-support inverse-CDF Zipf sampler over ranks 1..N with
// exponent alpha (weight of rank k proportional to 1/k^alpha).
//
// Why this and not math/rand/v2's rand.Zipf or numpy.random.zipf
// (spec/workload.yaml "zipf"): the standard generators parameterize differently
// (numpy uses a = s + 1; passing alpha=1.0 there is illegal) and are
// infinite-support. The workload requires an EXPLICIT finite inverse-CDF over
// the dense customer keyspace [1, N] so that rank 1 = customer id 1 and the hot
// customers receive enough invoices to fill 20-row list pages
// (spec/db-access.md hot-row note, spec/workload.yaml ~6KB list-page
// assumption). A uniform assignment would break that assumption.
//
// Construction: precompute the cumulative weight array once (O(N) build,
// O(log N) per draw via binary search). For full scale N=1,000,000 the cum
// slice is 8 MB of float64; built once, reused across the whole invoice stream.
type Zipf struct {
	cum   []float64 // cum[k] = sum_{j=1..k+1} 1/j^alpha ; length N
	total float64   // cum[N-1]
	n     int
}

// NewZipf builds the sampler for ranks 1..n with the given alpha.
func NewZipf(n int, alpha float64) *Zipf {
	cum := make([]float64, n)
	var running float64
	for k := 1; k <= n; k++ {
		running += weight(k, alpha)
		cum[k-1] = running
	}
	return &Zipf{cum: cum, total: running, n: n}
}

// weight returns 1/k^alpha. alpha is 1.0 in the workload, so the hot path is a
// plain reciprocal (no pow), which is exact and platform-stable. The general
// branch uses math.Pow for completeness but is never hit at alpha=1.0.
func weight(k int, alpha float64) float64 {
	if alpha == 1.0 {
		return 1.0 / float64(k)
	}
	return powAlpha(k, alpha)
}

// Rank returns a rank in [1, n] sampled by the inverse CDF, using one float64
// draw from r. Determinism: r is a per-stream deterministic source, and the
// binary search over the precomputed cum array is integer/float-stable across
// platforms (no transcendental functions on the draw path at alpha=1.0).
func (z *Zipf) Rank(r *rand.Rand) int {
	u := r.Float64() * z.total
	// Lower-bound binary search for the first cum >= u.
	lo, hi := 0, z.n-1
	for lo < hi {
		mid := int(uint(lo+hi) >> 1)
		if z.cum[mid] < u {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	return lo + 1 // rank is 1-based; rank 1 maps to customer id 1
}
