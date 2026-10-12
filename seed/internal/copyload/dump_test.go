package copyload

import (
	"encoding/csv"
	"os"
	"path/filepath"
	"strconv"
	"testing"

	"ledgerline/seed/internal/gen"
)

func smokeCfg() Config {
	s, _ := gen.ParseScale("smoke")
	return Config{Seed: 42, Scale: s, ItemsMin: 3, ItemsMax: 8}
}

// CSV shape: correct headers, exact customer/invoice counts, item count > 0 and
// consistent with the line invariant, and dense 1-based ids in PK order.
func TestDumpCSVShape(t *testing.T) {
	dir := t.TempDir()
	cfg := smokeCfg()
	if err := Dump(dir, cfg); err != nil {
		t.Fatalf("dump: %v", err)
	}

	// customers.csv
	cHeader, cRows := readCSV(t, filepath.Join(dir, "customers.csv"))
	wantC := []string{"id", "name", "email", "balance_minor", "currency", "created_at"}
	assertHeader(t, "customers", cHeader, wantC)
	if int64(len(cRows)) != cfg.Scale.Customers {
		t.Fatalf("customers rows = %d, want %d", len(cRows), cfg.Scale.Customers)
	}
	assertDenseIDs(t, "customers", cRows)
	for _, r := range cRows {
		if r[4] != "USD" {
			t.Fatalf("customer currency = %q, want USD", r[4])
		}
	}

	// invoices.csv
	iHeader, iRows := readCSV(t, filepath.Join(dir, "invoices.csv"))
	wantI := []string{"id", "customer_id", "status", "currency", "total_minor", "created_at"}
	assertHeader(t, "invoices", iHeader, wantI)
	if int64(len(iRows)) != cfg.Scale.Invoices {
		t.Fatalf("invoices rows = %d, want %d", len(iRows), cfg.Scale.Invoices)
	}
	assertDenseIDs(t, "invoices", iRows)

	// invoice_items.csv
	itHeader, itRows := readCSV(t, filepath.Join(dir, "invoice_items.csv"))
	wantIt := []string{"id", "invoice_id", "sku", "description", "qty", "unit_price_minor", "line_total_minor"}
	assertHeader(t, "invoice_items", itHeader, wantIt)
	if len(itRows) == 0 {
		t.Fatal("invoice_items has zero rows")
	}
	assertDenseIDs(t, "invoice_items", itRows)
	// line_total == qty * unit_price for every emitted row.
	for n, r := range itRows {
		qty, _ := strconv.ParseInt(r[4], 10, 64)
		price, _ := strconv.ParseInt(r[5], 10, 64)
		line, _ := strconv.ParseInt(r[6], 10, 64)
		if line != qty*price {
			t.Fatalf("item row %d: line_total %d != qty*price %d", n, line, qty*price)
		}
	}
}

// Dump is deterministic: two dumps to different dirs are byte-identical.
func TestDumpDeterministic(t *testing.T) {
	cfg := smokeCfg()
	d1, d2 := t.TempDir(), t.TempDir()
	if err := Dump(d1, cfg); err != nil {
		t.Fatal(err)
	}
	if err := Dump(d2, cfg); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"customers.csv", "invoices.csv", "invoice_items.csv"} {
		b1, err := os.ReadFile(filepath.Join(d1, name))
		if err != nil {
			t.Fatal(err)
		}
		b2, err := os.ReadFile(filepath.Join(d2, name))
		if err != nil {
			t.Fatal(err)
		}
		if string(b1) != string(b2) {
			t.Fatalf("%s differs between two dumps", name)
		}
	}
}

func readCSV(t *testing.T, path string) (header []string, rows [][]string) {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open %s: %v", path, err)
	}
	defer f.Close()
	rec, err := csv.NewReader(f).ReadAll()
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	if len(rec) == 0 {
		t.Fatalf("%s empty", path)
	}
	return rec[0], rec[1:]
}

func assertHeader(t *testing.T, name string, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s header = %v, want %v", name, got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("%s header[%d] = %q, want %q", name, i, got[i], want[i])
		}
	}
}

// assertDenseIDs checks column 0 is 1,2,3,... with no gaps (PK order).
func assertDenseIDs(t *testing.T, name string, rows [][]string) {
	t.Helper()
	for i, r := range rows {
		id, err := strconv.ParseInt(r[0], 10, 64)
		if err != nil {
			t.Fatalf("%s row %d id parse: %v", name, i, err)
		}
		if id != int64(i+1) {
			t.Fatalf("%s row %d id = %d, want dense %d", name, i, id, i+1)
		}
	}
}
