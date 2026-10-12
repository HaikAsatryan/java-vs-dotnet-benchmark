package gen

import (
	"strconv"
	"time"
)

// Customer is one generated customers row (spec/schema.sql). balance_minor is
// NOT generated here: it is accumulated from the deterministic invoice stream
// by the loader (multi-pass) and written into the COPY. Keeping it out of this
// struct makes the "balance is derived, never invented" invariant structural.
type Customer struct {
	ID        int64
	Name      string
	Email     string
	Currency  string // always "USD" (char(3), spec/workload.yaml currency const)
	CreatedAt time.Time
}

// Currency is the single fixed currency for the whole dataset.
const Currency = "USD"

// GenCustomer returns customer i (1-based) as a pure function of (seed, i).
// Name/email are deterministic filler keyed by id; they are never asserted by
// equivalence, only required NOT NULL by the schema.
func GenCustomer(seed uint64, id int64) Customer {
	// childRand reserved for future per-customer randomized fields; currently
	// name/email are pure id-derived filler, which is already deterministic.
	idStr := strconv.FormatInt(id, 10)
	return Customer{
		ID:        id,
		Name:      "Customer " + idStr,
		Email:     "customer" + idStr + "@ledgerline.test",
		Currency:  Currency,
		CreatedAt: CreatedAt(id),
	}
}
