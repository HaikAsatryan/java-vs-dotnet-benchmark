package gen

// AccumulateBalances runs PASS 1 of the multi-pass streaming load: it iterates
// the deterministic invoice stream once and returns a per-customer balance
// slice indexed by (customerID-1). balances[c-1] = sum of total_minor over all
// invoices assigned to customer c (spec/db-access.md balance invariant).
//
// Memory is bounded to one int64 per customer (8 MB at 1M customers), regardless
// of invoice count, because items are summed into the invoice total inside
// GenInvoice and discarded immediately; only the per-customer running sum is
// retained. The invoice stream is a pure function of the seed, so PASS 2 (the
// COPY) regenerates the identical stream with no temp files.
func AccumulateBalances(seed uint64, scale Scale, zipf *Zipf, itemsMin, itemsMax int) []int64 {
	balances := make([]int64, scale.Customers)
	for j := int64(1); j <= scale.Invoices; j++ {
		inv := GenInvoice(seed, j, zipf, itemsMin, itemsMax)
		balances[inv.CustomerID-1] += inv.TotalMinor
	}
	return balances
}
