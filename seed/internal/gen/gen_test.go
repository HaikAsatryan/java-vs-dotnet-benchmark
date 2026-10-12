package gen

import (
	"math/rand/v2"
	"regexp"
	"testing"
)

const (
	testSeed     = 42
	testItemsMin = 3
	testItemsMax = 8
)

func smokeScale() Scale {
	s, err := ParseScale("smoke")
	if err != nil {
		panic(err)
	}
	return s
}

// Determinism: two full generator runs at smoke scale produce identical row
// hashes (the seeder contract's core guarantee).
func TestStreamHashDeterministic(t *testing.T) {
	scale := smokeScale()
	h1 := StreamHash(testSeed, scale, testItemsMin, testItemsMax)
	h2 := StreamHash(testSeed, scale, testItemsMin, testItemsMax)
	if h1 != h2 {
		t.Fatalf("non-deterministic stream: %d != %d", h1, h2)
	}
}

// A different seed must change the hash (otherwise the seed is ignored).
func TestStreamHashSeedSensitive(t *testing.T) {
	scale := smokeScale()
	h1 := StreamHash(testSeed, scale, testItemsMin, testItemsMax)
	h2 := StreamHash(testSeed+1, scale, testItemsMin, testItemsMax)
	if h1 == h2 {
		t.Fatalf("seed had no effect: both runs hashed to %d", h1)
	}
}

// SeedBytes derivation is the frozen little-endian-repeated-4x rule.
func TestSeedBytes(t *testing.T) {
	got := SeedBytes(42)
	// 42 little-endian = 2a 00 00 00 00 00 00 00, repeated 4x.
	want := [32]byte{
		0x2a, 0, 0, 0, 0, 0, 0, 0,
		0x2a, 0, 0, 0, 0, 0, 0, 0,
		0x2a, 0, 0, 0, 0, 0, 0, 0,
		0x2a, 0, 0, 0, 0, 0, 0, 0,
	}
	if got != want {
		t.Fatalf("SeedBytes(42) = %x, want %x", got, want)
	}
}

// Balance accumulation: the per-customer balance slice equals the independently
// recomputed sum of invoice totals, and the grand total equals the sum of all
// invoice totals (no rows lost, no double counting).
func TestBalanceAccumulation(t *testing.T) {
	scale := smokeScale()
	zipf := NewZipf(int(scale.Customers), 1.0)
	balances := AccumulateBalances(testSeed, scale, zipf, testItemsMin, testItemsMax)

	if int64(len(balances)) != scale.Customers {
		t.Fatalf("balance slice length = %d, want %d", len(balances), scale.Customers)
	}

	// Independent recomputation.
	want := make([]int64, scale.Customers)
	var grand int64
	for j := int64(1); j <= scale.Invoices; j++ {
		inv := GenInvoice(testSeed, j, zipf, testItemsMin, testItemsMax)
		// invoice total must equal the sum of its item line totals.
		var sum int64
		for _, it := range inv.Items {
			sum += it.LineTotalMinor
		}
		if sum != inv.TotalMinor {
			t.Fatalf("invoice %d total=%d != sum(items)=%d", j, inv.TotalMinor, sum)
		}
		want[inv.CustomerID-1] += inv.TotalMinor
		grand += inv.TotalMinor
	}

	var gotGrand int64
	for c := range balances {
		if balances[c] != want[c] {
			t.Fatalf("balance[%d] = %d, want %d", c, balances[c], want[c])
		}
		gotGrand += balances[c]
	}
	if gotGrand != grand {
		t.Fatalf("grand balance %d != grand invoice total %d", gotGrand, grand)
	}
}

// Item-level invariants over the whole smoke stream: line_total = qty*price,
// sku matches the frozen pattern, qty/price within the workload ranges, item
// count within [min,max].
func TestItemInvariants(t *testing.T) {
	scale := smokeScale()
	zipf := NewZipf(int(scale.Customers), 1.0)
	skuRe := regexp.MustCompile(`^[A-Z]{2}-[0-9]{4}$`)

	for j := int64(1); j <= scale.Invoices; j++ {
		inv := GenInvoice(testSeed, j, zipf, testItemsMin, testItemsMax)
		if n := len(inv.Items); n < testItemsMin || n > testItemsMax {
			t.Fatalf("invoice %d has %d items, want [%d,%d]", j, n, testItemsMin, testItemsMax)
		}
		if inv.CustomerID < 1 || inv.CustomerID > scale.Customers {
			t.Fatalf("invoice %d customer_id=%d out of range", j, inv.CustomerID)
		}
		for _, it := range inv.Items {
			if !skuRe.MatchString(it.SKU) {
				t.Fatalf("invoice %d sku %q fails pattern", j, it.SKU)
			}
			if it.Qty < qtyMin || it.Qty > qtyMax {
				t.Fatalf("qty %d out of [%d,%d]", it.Qty, qtyMin, qtyMax)
			}
			if it.UnitPriceMinor < unitPriceMin || it.UnitPriceMinor > unitPriceMax {
				t.Fatalf("unit_price %d out of [%d,%d]", it.UnitPriceMinor, unitPriceMin, unitPriceMax)
			}
			if it.LineTotalMinor != int64(it.Qty)*it.UnitPriceMinor {
				t.Fatalf("line_total %d != qty*price", it.LineTotalMinor)
			}
		}
	}
}

// Status draw respects the frozen set and is non-degenerate (all four statuses
// appear in a smoke run).
func TestStatusDistribution(t *testing.T) {
	scale := smokeScale()
	zipf := NewZipf(int(scale.Customers), 1.0)
	counts := map[string]int{}
	for j := int64(1); j <= scale.Invoices; j++ {
		inv := GenInvoice(testSeed, j, zipf, testItemsMin, testItemsMax)
		counts[inv.Status]++
	}
	for _, s := range []string{"draft", "open", "paid", "void"} {
		if counts[s] == 0 {
			t.Fatalf("status %q never produced in smoke run; counts=%v", s, counts)
		}
	}
	// paid (0.50) should dominate draft (0.10) over 5000 invoices.
	if counts["paid"] <= counts["draft"] {
		t.Fatalf("paid (%d) should exceed draft (%d)", counts["paid"], counts["draft"])
	}
}

// Zipf sanity: rank 1 is sampled strictly more than rank 2 (and far more than a
// mid rank), confirming the inverse-CDF skew that fills hot-customer list pages.
func TestZipfRankOneIsHottest(t *testing.T) {
	const n = 1000
	z := NewZipf(n, 1.0)
	r := rand.New(rand.NewChaCha8(SeedBytes(testSeed)))
	counts := make([]int, n+1)
	const draws = 200_000
	for i := 0; i < draws; i++ {
		counts[z.Rank(r)]++
	}
	if counts[1] <= counts[2] {
		t.Fatalf("rank1 (%d) should exceed rank2 (%d)", counts[1], counts[2])
	}
	if counts[1] <= counts[500] {
		t.Fatalf("rank1 (%d) should dominate rank500 (%d)", counts[1], counts[500])
	}
	// every rank is in range
	for i := 0; i < draws/100; i++ {
		k := z.Rank(r)
		if k < 1 || k > n {
			t.Fatalf("zipf rank %d out of [1,%d]", k, n)
		}
	}
}

// Zipf weight monotonicity: weight(1) > weight(2) > ... at alpha=1.0.
func TestZipfWeightMonotone(t *testing.T) {
	prev := weight(1, 1.0)
	for k := 2; k <= 100; k++ {
		w := weight(k, 1.0)
		if w >= prev {
			t.Fatalf("weight not decreasing at k=%d: %v >= %v", k, w, prev)
		}
		prev = w
	}
}
