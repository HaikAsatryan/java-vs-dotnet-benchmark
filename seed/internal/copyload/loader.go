// Package copyload streams the deterministic generator into PostgreSQL via the
// pgx binary COPY protocol, in PK order, then restarts the IDENTITY sequences.
package copyload

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"ledgerline/seed/internal/gen"
)

// chunkRows bounds how many rows a single CopyFromSlice call buffers. The
// generator is re-entrant per id, so chunking only affects memory/throughput,
// never row content. 100k rows/flush.
const chunkRows = 100_000

// Config carries the load parameters.
type Config struct {
	Seed     uint64
	Scale    gen.Scale
	ItemsMin int
	ItemsMax int
}

// Truncate clears the three tables and restarts their IDENTITY sequences before
// a load (--truncate). CASCADE is unnecessary (we list all child tables) but
// RESTART IDENTITY resets the sequences so the post-load restart starts clean.
func Truncate(ctx context.Context, pool *pgxpool.Pool) error {
	_, err := pool.Exec(ctx,
		"TRUNCATE TABLE invoice_items, invoices, customers RESTART IDENTITY CASCADE")
	return err
}

// Load performs PASS 1 (balance accumulation) then PASS 2 (the three ordered
// COPYs), then restarts both IDENTITY sequences past the seeded max. It does NOT
// open a long transaction around the COPYs: each COPY is committed by pgx on its
// own; the post-load invariants (run by the caller) are the correctness gate.
func Load(ctx context.Context, pool *pgxpool.Pool, cfg Config) error {
	zipf := gen.NewZipf(int(cfg.Scale.Customers), 1.0)

	// PASS 1: per-customer balances from the deterministic invoice stream.
	balances := gen.AccumulateBalances(cfg.Seed, cfg.Scale, zipf, cfg.ItemsMin, cfg.ItemsMax)

	// PASS 2a: customers (balance_minor from the accumulated slice).
	if err := copyCustomers(ctx, pool, cfg, balances); err != nil {
		return fmt.Errorf("copy customers: %w", err)
	}
	// PASS 2b: invoices (regenerate the deterministic stream).
	if err := copyInvoices(ctx, pool, cfg, zipf); err != nil {
		return fmt.Errorf("copy invoices: %w", err)
	}
	// PASS 2c: invoice_items (regenerate the deterministic stream again).
	if err := copyItems(ctx, pool, cfg, zipf); err != nil {
		return fmt.Errorf("copy invoice_items: %w", err)
	}

	// Restart IDENTITY sequences past the seeded max (spec/schema.sql header):
	// without this, runtime POSTs collide with seeded ids.
	if err := restartIdentities(ctx, pool, cfg.Scale); err != nil {
		return fmt.Errorf("restart identities: %w", err)
	}
	return nil
}

func copyCustomers(ctx context.Context, pool *pgxpool.Pool, cfg Config, balances []int64) error {
	cols := []string{"id", "name", "email", "balance_minor", "currency", "created_at"}
	n := cfg.Scale.Customers
	return copyChunked(ctx, pool, "customers", cols, n, func(base, idx int64) ([]any, error) {
		id := base + idx + 1 // ids are 1-based, dense, ascending
		c := gen.GenCustomer(cfg.Seed, id)
		return []any{c.ID, c.Name, c.Email, balances[id-1], c.Currency, c.CreatedAt}, nil
	})
}

func copyInvoices(ctx context.Context, pool *pgxpool.Pool, cfg Config, zipf *gen.Zipf) error {
	cols := []string{"id", "customer_id", "status", "currency", "total_minor", "created_at"}
	n := cfg.Scale.Invoices
	return copyChunked(ctx, pool, "invoices", cols, n, func(base, idx int64) ([]any, error) {
		id := base + idx + 1
		inv := gen.GenInvoice(cfg.Seed, id, zipf, cfg.ItemsMin, cfg.ItemsMax)
		return []any{inv.ID, inv.CustomerID, inv.Status, inv.Currency, inv.TotalMinor, inv.CreatedAt}, nil
	})
}

// copyItems streams invoice_items in (invoice_id, item-order) order. Item ids
// are IDENTITY (never returned), so the seeder writes explicit dense ids 1..M in
// emission order and restarts the sequence past M afterward. To keep the COPY a
// single streaming pass with bounded memory, item rows are flattened on the fly:
// we walk invoices in id order and, for each, emit its items.
func copyItems(ctx context.Context, pool *pgxpool.Pool, cfg Config, zipf *gen.Zipf) error {
	cols := []string{"id", "invoice_id", "sku", "description", "qty", "unit_price_minor", "line_total_minor"}

	src := newItemSource(cfg, zipf)
	tag, err := pool.CopyFrom(ctx, pgx.Identifier{"invoice_items"}, cols,
		pgx.CopyFromFunc(src.next))
	if err != nil {
		return err
	}
	if tag != src.emitted {
		return fmt.Errorf("invoice_items COPY count mismatch: copied %d, generated %d", tag, src.emitted)
	}
	return nil
}

// itemSource is a CopyFromFunc adapter that lazily flattens invoices into item
// rows, holding at most one invoice's items in memory at a time.
type itemSource struct {
	cfg     Config
	zipf    *gen.Zipf
	invID   int64      // next invoice to expand (1-based)
	buf     []gen.Item // pending items of the current invoice
	bufInv  int64      // invoice_id of buffered items
	nextID  int64      // next explicit item id (1-based, dense)
	emitted int64      // total rows emitted (for the count assertion)
}

func newItemSource(cfg Config, zipf *gen.Zipf) *itemSource {
	return &itemSource{cfg: cfg, zipf: zipf, invID: 1, nextID: 1}
}

// next returns the next item row, or (nil, nil) at end of data (CopyFromFunc
// contract). Memory stays bounded: at most one invoice's item slice is live.
func (s *itemSource) next() ([]any, error) {
	for len(s.buf) == 0 {
		if s.invID > s.cfg.Scale.Invoices {
			return nil, nil // end of data
		}
		inv := gen.GenInvoice(s.cfg.Seed, s.invID, s.zipf, s.cfg.ItemsMin, s.cfg.ItemsMax)
		s.buf = inv.Items
		s.bufInv = inv.ID
		s.invID++
	}
	it := s.buf[0]
	s.buf = s.buf[1:]
	id := s.nextID
	s.nextID++
	s.emitted++
	return []any{id, s.bufInv, it.SKU, it.Description, it.Qty, it.UnitPriceMinor, it.LineTotalMinor}, nil
}

// copyChunked drives CopyFrom in fixed-size chunks. Each chunk regenerates rows
// by absolute id (base+idx+1), so chunk boundaries never affect content.
func copyChunked(
	ctx context.Context,
	pool *pgxpool.Pool,
	table string,
	cols []string,
	total int64,
	row func(base, idx int64) ([]any, error),
) error {
	for base := int64(0); base < total; base += chunkRows {
		size := int64(chunkRows)
		if base+size > total {
			size = total - base
		}
		captured := base
		src := pgx.CopyFromSlice(int(size), func(i int) ([]any, error) {
			return row(captured, int64(i))
		})
		tag, err := pool.CopyFrom(ctx, pgx.Identifier{table}, cols, src)
		if err != nil {
			return err
		}
		if tag != size {
			return fmt.Errorf("%s chunk COPY count mismatch: copied %d, expected %d", table, tag, size)
		}
	}
	return nil
}

// restartIdentities sets both IDENTITY sequences to (seeded_max + 1) so runtime
// POSTs do not collide with seeded ids (spec/schema.sql header). invoices max id
// == Scale.Invoices (dense). invoice_items max id == total item count, which we
// read from the table (cheaper and authoritative than recomputing).
func restartIdentities(ctx context.Context, pool *pgxpool.Pool, scale gen.Scale) error {
	invRestart := scale.Invoices + 1
	if _, err := pool.Exec(ctx,
		fmt.Sprintf("ALTER TABLE invoices ALTER COLUMN id RESTART WITH %d", invRestart)); err != nil {
		return err
	}

	var maxItem int64
	if err := pool.QueryRow(ctx,
		"SELECT COALESCE(MAX(id), 0) FROM invoice_items").Scan(&maxItem); err != nil {
		return err
	}
	if _, err := pool.Exec(ctx,
		fmt.Sprintf("ALTER TABLE invoice_items ALTER COLUMN id RESTART WITH %d", maxItem+1)); err != nil {
		return err
	}
	return nil
}
