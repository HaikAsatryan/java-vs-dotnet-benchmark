package gen

import (
	"encoding/binary"
	"hash/fnv"
	"strconv"
)

// StreamHash returns a 64-bit FNV-1a hash over the ENTIRE deterministic dataset
// for a scale (customers, then invoices, then their items, all in PK order). Two
// invocations with the same (seed, scale, items range) MUST return the same
// value; this is the determinism contract that the unit tests assert. Balances
// are included so accumulation is covered too.
func StreamHash(seed uint64, scale Scale, itemsMin, itemsMax int) uint64 {
	h := fnv.New64a()
	var u8 [8]byte
	putU := func(v uint64) {
		binary.LittleEndian.PutUint64(u8[:], v)
		h.Write(u8[:])
	}
	putI := func(v int64) { putU(uint64(v)) }
	putS := func(s string) { h.Write([]byte(s)); h.Write([]byte{0}) }

	zipf := NewZipf(int(scale.Customers), 1.0)
	balances := AccumulateBalances(seed, scale, zipf, itemsMin, itemsMax)

	// customers
	for id := int64(1); id <= scale.Customers; id++ {
		c := GenCustomer(seed, id)
		putI(c.ID)
		putS(c.Name)
		putS(c.Email)
		putI(balances[id-1])
		putS(c.Currency)
		putS(strconv.FormatInt(c.CreatedAt.Unix(), 10))
	}
	// invoices + items
	for id := int64(1); id <= scale.Invoices; id++ {
		inv := GenInvoice(seed, id, zipf, itemsMin, itemsMax)
		putI(inv.ID)
		putI(inv.CustomerID)
		putS(inv.Status)
		putS(inv.Currency)
		putI(inv.TotalMinor)
		putS(strconv.FormatInt(inv.CreatedAt.Unix(), 10))
		for _, it := range inv.Items {
			putS(it.SKU)
			putS(it.Description)
			putI(int64(it.Qty))
			putI(it.UnitPriceMinor)
			putI(it.LineTotalMinor)
		}
	}
	return h.Sum64()
}
