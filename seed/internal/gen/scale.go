package gen

import "fmt"

// Scale holds the exact (non-random) row counts for a named scale
// (spec/workload.yaml seeding.scales). Counts are EXACT totals, not random
// draws: full = 1,000,000 customers / 5,000,000 invoices; smoke = 1,000 / 5,000.
type Scale struct {
	Name      string
	Customers int64
	Invoices  int64
}

// ParseScale maps the --scale flag to a Scale.
func ParseScale(name string) (Scale, error) {
	switch name {
	case "full":
		return Scale{Name: "full", Customers: 1_000_000, Invoices: 5_000_000}, nil
	case "smoke":
		return Scale{Name: "smoke", Customers: 1_000, Invoices: 5_000}, nil
	default:
		return Scale{}, fmt.Errorf("unknown scale %q (want full|smoke)", name)
	}
}
