package copyload

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"ledgerline/seed/internal/gen"
)

// timeFmt is the CSV timestamp format: RFC3339 in UTC. PostgreSQL parses this
// for timestamptz on a \copy fallback. Stable, no monotonic component.
const timeFmt = time.RFC3339

// Dump writes the three tables as RFC4180 CSVs into dir (the --mode dump debug
// path). Money is written as integer minor units; there
// are no nullable money columns, so no \N handling is needed. Rows are emitted
// in PK order, identical to the COPY path, sharing the same generators.
func Dump(dir string, cfg Config) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	zipf := gen.NewZipf(int(cfg.Scale.Customers), 1.0)
	balances := gen.AccumulateBalances(cfg.Seed, cfg.Scale, zipf, cfg.ItemsMin, cfg.ItemsMax)

	if err := dumpCustomers(dir, cfg, balances); err != nil {
		return err
	}
	if err := dumpInvoices(dir, cfg, zipf); err != nil {
		return err
	}
	if err := dumpItems(dir, cfg, zipf); err != nil {
		return err
	}
	return nil
}

func dumpCustomers(dir string, cfg Config, balances []int64) error {
	return withCSV(filepath.Join(dir, "customers.csv"),
		[]string{"id", "name", "email", "balance_minor", "currency", "created_at"},
		func(w *csv.Writer) error {
			for id := int64(1); id <= cfg.Scale.Customers; id++ {
				c := gen.GenCustomer(cfg.Seed, id)
				rec := []string{
					itoa(c.ID), c.Name, c.Email, itoa(balances[id-1]),
					c.Currency, c.CreatedAt.UTC().Format(timeFmt),
				}
				if err := w.Write(rec); err != nil {
					return err
				}
			}
			return nil
		})
}

func dumpInvoices(dir string, cfg Config, zipf *gen.Zipf) error {
	return withCSV(filepath.Join(dir, "invoices.csv"),
		[]string{"id", "customer_id", "status", "currency", "total_minor", "created_at"},
		func(w *csv.Writer) error {
			for id := int64(1); id <= cfg.Scale.Invoices; id++ {
				inv := gen.GenInvoice(cfg.Seed, id, zipf, cfg.ItemsMin, cfg.ItemsMax)
				rec := []string{
					itoa(inv.ID), itoa(inv.CustomerID), inv.Status, inv.Currency,
					itoa(inv.TotalMinor), inv.CreatedAt.UTC().Format(timeFmt),
				}
				if err := w.Write(rec); err != nil {
					return err
				}
			}
			return nil
		})
}

func dumpItems(dir string, cfg Config, zipf *gen.Zipf) error {
	return withCSV(filepath.Join(dir, "invoice_items.csv"),
		[]string{"id", "invoice_id", "sku", "description", "qty", "unit_price_minor", "line_total_minor"},
		func(w *csv.Writer) error {
			var itemID int64
			for id := int64(1); id <= cfg.Scale.Invoices; id++ {
				inv := gen.GenInvoice(cfg.Seed, id, zipf, cfg.ItemsMin, cfg.ItemsMax)
				for _, it := range inv.Items {
					itemID++
					rec := []string{
						itoa(itemID), itoa(inv.ID), it.SKU, it.Description,
						strconv.FormatInt(int64(it.Qty), 10),
						itoa(it.UnitPriceMinor), itoa(it.LineTotalMinor),
					}
					if err := w.Write(rec); err != nil {
						return err
					}
				}
			}
			return nil
		})
}

// withCSV opens path, writes the header, runs body, and flushes. The stdlib
// encoding/csv writer is RFC4180-compliant (CRLF line terminator, quoting of
// fields containing comma/quote/newline).
func withCSV(path string, header []string, body func(w *csv.Writer) error) error {
	f, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("create %s: %w", path, err)
	}
	defer f.Close()

	w := csv.NewWriter(f)
	if err := w.Write(header); err != nil {
		return err
	}
	if err := body(w); err != nil {
		return err
	}
	w.Flush()
	if err := w.Error(); err != nil {
		return err
	}
	return f.Close()
}

func itoa(v int64) string { return strconv.FormatInt(v, 10) }
