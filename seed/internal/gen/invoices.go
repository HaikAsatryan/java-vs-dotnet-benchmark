package gen

import (
	"math/rand/v2"
	"strconv"
	"strings"
	"time"
)

// skuCategories is the frozen category set the seeder draws from
// (spec/pricing-algorithm.md surcharge map) plus a trailing "ZZ" to exercise the
// unknown->0 pricing branch (spec/workload.yaml sku categories). Order is fixed;
// do not reorder (it would change the deterministic draw).
var skuCategories = []string{
	"EL", "GR", "HZ", "LX", "DG", "FU", "MD", "AP", "BK", "TL", "ZZ",
}

// statusWeights are the frozen invoice status weights (spec/workload.yaml /
// seeder contract): draft 0.10, open 0.30, paid 0.50, void 0.10. Stored as
// cumulative integer permille so the draw is integer-exact (no float compare).
var statusLadder = []struct {
	cumPermille int
	status      string
}{
	{100, "draft"}, // 0.10
	{400, "open"},  // +0.30
	{900, "paid"},  // +0.50
	{1000, "void"}, // +0.10
}

// qty range and unit price range (spec/workload.yaml create_invoice fields).
const (
	qtyMin           = 1
	qtyMax           = 50 // inclusive
	unitPriceMin     = 100
	unitPriceMax     = 5_000_000 // inclusive
	skuCodeMin       = 0
	skuCodeMax       = 9999 // four digits
	descFillerChunks = 3
)

// Invoice is one generated invoices row plus its items. total_minor is computed
// here as the sum of item line totals (spec/schema.sql invariant).
type Invoice struct {
	ID         int64
	CustomerID int64
	Status     string
	Currency   string
	TotalMinor int64
	CreatedAt  time.Time
	Items      []Item
}

// Item is one generated invoice_items row. line_total_minor = qty*unit_price.
type Item struct {
	SKU            string
	Description    string
	Qty            int32
	UnitPriceMinor int64
	LineTotalMinor int64
}

// GenInvoice returns invoice j (1-based) with its items as a pure function of
// (seed, j, customerCount, itemsMin, itemsMax). The customer_id is drawn from
// the shared Zipf sampler, which the caller supplies (built once over
// [1, customerCount]); zipf must be the SAME sampler for every invoice so the
// distribution is stable. The per-invoice child stream keyed by j makes every
// other field order-independent.
func GenInvoice(seed uint64, j int64, zipf *Zipf, itemsMin, itemsMax int) Invoice {
	r := childRand(seed, kindInvoice, uint64(j))

	custID := int64(zipf.Rank(r))

	nItems := itemsMin
	if itemsMax > itemsMin {
		nItems = itemsMin + r.IntN(itemsMax-itemsMin+1)
	}

	items := make([]Item, nItems)
	var total int64
	for k := 0; k < nItems; k++ {
		it := genItem(r)
		items[k] = it
		total += it.LineTotalMinor
	}

	return Invoice{
		ID:         j,
		CustomerID: custID,
		Status:     drawStatus(r),
		Currency:   Currency,
		TotalMinor: total,
		CreatedAt:  CreatedAt(j),
		Items:      items,
	}
}

// genItem draws one item from the per-invoice stream r. Draw order is frozen:
// sku-category, sku-code, qty, unit_price, description. Reordering would change
// every downstream value.
func genItem(r *rand.Rand) Item {
	cat := skuCategories[r.IntN(len(skuCategories))]
	code := skuCodeMin + r.IntN(skuCodeMax-skuCodeMin+1)
	sku := cat + "-" + pad4(code)

	qty := int32(qtyMin + r.IntN(qtyMax-qtyMin+1))
	unitPrice := int64(unitPriceMin + r.IntN(unitPriceMax-unitPriceMin+1))
	lineTotal := LineTotal(qty, unitPrice)

	return Item{
		SKU:            sku,
		Description:    fillerDescription(sku, qty),
		Qty:            qty,
		UnitPriceMinor: unitPrice,
		LineTotalMinor: lineTotal,
	}
}

// drawStatus picks an invoice status by the frozen integer-permille ladder.
func drawStatus(r *rand.Rand) string {
	roll := r.IntN(1000) // 0..999
	for _, s := range statusLadder {
		if roll < s.cumPermille {
			return s.status
		}
	}
	return statusLadder[len(statusLadder)-1].status // unreachable; safety
}

// pad4 formats a 0..9999 code as exactly four digits (sku NNNN part).
func pad4(code int) string {
	s := strconv.Itoa(code)
	if len(s) >= 4 {
		return s
	}
	return strings.Repeat("0", 4-len(s)) + s
}

// fillerDescription builds a deterministic, NOT NULL description. Content is
// keyed by sku+qty so it is stable per item; it is never asserted by
// equivalence (no index on description, schema only requires NOT NULL).
func fillerDescription(sku string, qty int32) string {
	var b strings.Builder
	b.WriteString("Item ")
	b.WriteString(sku)
	b.WriteString(" x")
	b.WriteString(strconv.FormatInt(int64(qty), 10))
	for i := 0; i < descFillerChunks; i++ {
		b.WriteString(" lorem")
	}
	return b.String()
}
