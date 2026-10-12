package gen

import (
	"math"
	"time"
)

// All money is int64 minor units end-to-end (spec/schema.sql). No floats,
// no decimal types touch any amount. The Zipf cumulative weights are the only
// float64 in the generator and they never feed a money value.

// LineTotal computes line_total_minor = qty * unit_price_minor.
// Inputs are bounded by the workload (qty <= 50, unit_price <= 5_000_000) so the
// product (<= 2.5e8) and the per-invoice sum (<= 8 items * 2.5e8 = 2e9) and the
// per-customer balance are all comfortably inside int64.
func LineTotal(qty int32, unitPriceMinor int64) int64 {
	return int64(qty) * unitPriceMinor
}

// epoch is the fixed base timestamp for deterministic created_at values:
// 2025-01-01T00:00:00Z + id seconds, computed per table (spec/db-access.md
// allows any fixed epoch progression; equivalence never compares seeded
// timestamps across DB loads). UTC, no monotonic component.
var epoch = time.Date(2025, 1, 1, 0, 0, 0, 0, time.UTC)

// CreatedAt returns epoch + id seconds. id is the per-table row id (1-based).
func CreatedAt(id int64) time.Time {
	return epoch.Add(time.Duration(id) * time.Second)
}

// powAlpha returns 1/k^alpha for the general (alpha != 1.0) Zipf branch. Not on
// any hot path at the frozen alpha=1.0; kept for completeness.
func powAlpha(k int, alpha float64) float64 {
	return 1.0 / math.Pow(float64(k), alpha)
}
