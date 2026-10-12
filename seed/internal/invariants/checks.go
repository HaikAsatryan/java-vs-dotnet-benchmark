// Package invariants runs the post-load assertions from spec/db-access.md. Any
// failure is a hard error (the caller maps it to a non-zero process exit).
package invariants

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"

	"ledgerline/seed/internal/gen"
)

// requiredIndex is the list-page index that GET /customers/{id}/invoices depends
// on (spec/schema.sql index 3, spec/db-access.md Q3). Its presence is asserted.
const requiredIndex = "idx_invoices_customer_id_id_desc"

// Result is a single named check outcome (for structured reporting).
type Result struct {
	Name string
	OK   bool
	Got  int64
	Want int64
	Note string
}

// CheckAll runs every invariant for the given scale and returns the results in
// run order. All checks run even if one fails so the report is complete; ok is
// false if any failed.
func CheckAll(ctx context.Context, pool *pgxpool.Pool, scale gen.Scale) ([]Result, bool) {
	var out []Result
	ok := true

	add := func(r Result) {
		out = append(out, r)
		if !r.OK {
			ok = false
		}
	}

	// Exact row counts per scale (invoice_items count is data-derived, not a
	// fixed constant, so it is asserted >0 and FK-consistent rather than equal
	// to a precomputed total; the line/total invariants below pin its content).
	add(countEq(ctx, pool, "customers", scale.Customers))
	add(countEq(ctx, pool, "invoices", scale.Invoices))
	add(countPositive(ctx, pool, "invoice_items"))

	// Per customer: balance_minor == COALESCE(sum(invoices.total_minor), 0).
	add(zeroViolations(ctx, pool, "balance_invariant",
		`SELECT count(*) FROM customers c
		   WHERE c.balance_minor <>
		         COALESCE((SELECT SUM(i.total_minor) FROM invoices i WHERE i.customer_id = c.id), 0)`))

	// Per invoice: total_minor == sum(items.line_total_minor).
	add(zeroViolations(ctx, pool, "invoice_total_invariant",
		`SELECT count(*) FROM invoices i
		   WHERE i.total_minor <>
		         COALESCE((SELECT SUM(it.line_total_minor) FROM invoice_items it WHERE it.invoice_id = i.id), 0)`))

	// Per item: line_total_minor == qty * unit_price_minor.
	add(zeroViolations(ctx, pool, "line_total_invariant",
		`SELECT count(*) FROM invoice_items WHERE line_total_minor <> qty * unit_price_minor`))

	// FK integrity: no orphan invoices or items (FKs enforce it; asserted to
	// also cover the dump/restore path).
	add(zeroViolations(ctx, pool, "orphan_invoices",
		`SELECT count(*) FROM invoices i WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.id = i.customer_id)`))
	add(zeroViolations(ctx, pool, "orphan_items",
		`SELECT count(*) FROM invoice_items it WHERE NOT EXISTS (SELECT 1 FROM invoices i WHERE i.id = it.invoice_id)`))

	// Index presence (pg_indexes).
	add(indexExists(ctx, pool, requiredIndex))

	return out, ok
}

func countEq(ctx context.Context, pool *pgxpool.Pool, table string, want int64) Result {
	var got int64
	err := pool.QueryRow(ctx, "SELECT count(*) FROM "+table).Scan(&got)
	r := Result{Name: "rowcount_" + table, Want: want, Got: got}
	switch {
	case err != nil:
		r.Note = err.Error()
	case got == want:
		r.OK = true
	default:
		r.Note = fmt.Sprintf("expected %d rows, found %d", want, got)
	}
	return r
}

func countPositive(ctx context.Context, pool *pgxpool.Pool, table string) Result {
	var got int64
	err := pool.QueryRow(ctx, "SELECT count(*) FROM "+table).Scan(&got)
	r := Result{Name: "rowcount_" + table, Got: got}
	switch {
	case err != nil:
		r.Note = err.Error()
	case got > 0:
		r.OK = true
	default:
		r.Note = "expected > 0 rows, found 0"
	}
	return r
}

func zeroViolations(ctx context.Context, pool *pgxpool.Pool, name, query string) Result {
	var violations int64
	err := pool.QueryRow(ctx, query).Scan(&violations)
	r := Result{Name: name, Want: 0, Got: violations}
	switch {
	case err != nil:
		r.Note = err.Error()
	case violations == 0:
		r.OK = true
	default:
		r.Note = fmt.Sprintf("%d rows violate %s", violations, name)
	}
	return r
}

func indexExists(ctx context.Context, pool *pgxpool.Pool, name string) Result {
	var exists bool
	err := pool.QueryRow(ctx,
		"SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = $1)", name).Scan(&exists)
	r := Result{Name: "index_" + name}
	switch {
	case err != nil:
		r.Note = err.Error()
	case exists:
		r.OK = true
	default:
		r.Note = "index missing"
	}
	return r
}
