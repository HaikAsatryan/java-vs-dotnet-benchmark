// Command seed is the canonical deterministic Ledgerline seeder.
//
// It applies the DDL (optional), loads the deterministic dataset via pgx binary
// COPY in PK order, restarts the IDENTITY sequences past the seeded max, applies
// the ledger-role grants (optional), and runs the post-load invariants. Every
// row is a pure function of (--seed, id); see internal/gen and seed/README.md.
//
// Spec sources: spec/db-access.md "Seeder contract", spec/workload.yaml seeding,
// spec/schema.sql, spec/postgres/initdb/01-role.sql. spec/ is the single source
// of truth.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"ledgerline/seed/internal/copyload"
	"ledgerline/seed/internal/gen"
	"ledgerline/seed/internal/invariants"
	"ledgerline/seed/internal/schema"
)

type flags struct {
	dsn        string
	scale      string
	seed       uint64
	itemsMin   int
	itemsMax   int
	mode       string
	dumpDir    string
	truncate   bool
	verifyOnly bool
	schemaPath string
	grants     bool
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "seed: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	var f flags
	flag.StringVar(&f.dsn, "dsn", "", "PostgreSQL DSN (seeder-only superuser creds)")
	flag.StringVar(&f.scale, "scale", "full", "dataset scale: full|smoke")
	flag.Uint64Var(&f.seed, "seed", 42, "RNG seed (frozen default 42)")
	flag.IntVar(&f.itemsMin, "items-min", 3, "min invoice line items (inclusive)")
	flag.IntVar(&f.itemsMax, "items-max", 8, "max invoice line items (inclusive)")
	flag.StringVar(&f.mode, "mode", "copy", "load mode: copy (live PG) | dump (CSV files)")
	flag.StringVar(&f.dumpDir, "dump-dir", "dump", "output directory for --mode dump")
	flag.BoolVar(&f.truncate, "truncate", false, "TRUNCATE ... RESTART IDENTITY before load")
	flag.BoolVar(&f.verifyOnly, "verify-only", false, "run invariants against an already-loaded DB and exit")
	flag.StringVar(&f.schemaPath, "schema", "", "path to schema.sql; applies the DDL first when given")
	flag.BoolVar(&f.grants, "grants", false, "apply the ledger-role GRANTs (spec/postgres/initdb/01-role.sql)")
	flag.Parse()

	scale, err := gen.ParseScale(f.scale)
	if err != nil {
		return err
	}
	if f.itemsMin < 1 || f.itemsMax < f.itemsMin {
		return fmt.Errorf("invalid items range: min=%d max=%d (need 1<=min<=max)", f.itemsMin, f.itemsMax)
	}

	cfg := copyload.Config{
		Seed:     f.seed,
		Scale:    scale,
		ItemsMin: f.itemsMin,
		ItemsMax: f.itemsMax,
	}

	// --mode dump needs no DB connection.
	if f.mode == "dump" && !f.verifyOnly {
		start := time.Now()
		if err := copyload.Dump(f.dumpDir, cfg); err != nil {
			return err
		}
		fmt.Printf("dump: wrote CSVs to %s (scale=%s, seed=%d) in %s\n",
			f.dumpDir, scale.Name, f.seed, time.Since(start).Round(time.Millisecond))
		return nil
	}

	if f.mode != "copy" {
		return fmt.Errorf("unknown --mode %q (want copy|dump)", f.mode)
	}
	if f.dsn == "" {
		return fmt.Errorf("--dsn is required for --mode copy / --verify-only")
	}

	ctx := context.Background()
	poolCfg, err := pgxpool.ParseConfig(f.dsn)
	if err != nil {
		return fmt.Errorf("parse dsn: %w", err)
	}
	// The PG config carries production-realistic workload timeouts
	// (statement_timeout=5s, lock_timeout=1s, idle_in_transaction_session_timeout=10s;
	// spec/postgres/postgresql.conf). Bulk COPY at full scale legitimately runs for
	// minutes, so the seeder session disables them; the spec timeouts are a promise
	// about the *workload* connections, not the loader.
	poolCfg.ConnConfig.RuntimeParams["statement_timeout"] = "0"
	poolCfg.ConnConfig.RuntimeParams["lock_timeout"] = "0"
	poolCfg.ConnConfig.RuntimeParams["idle_in_transaction_session_timeout"] = "0"
	pool, err := pgxpool.NewWithConfig(ctx, poolCfg)
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	defer pool.Close()

	if err := pool.Ping(ctx); err != nil {
		return fmt.Errorf("ping: %w", err)
	}

	// --verify-only: assert invariants on the existing data and exit.
	if f.verifyOnly {
		return reportInvariants(ctx, pool, scale)
	}

	// Optional DDL apply (single source of truth: spec/schema.sql).
	if f.schemaPath != "" {
		if err := schema.Apply(ctx, pool, f.schemaPath); err != nil {
			return err
		}
		fmt.Printf("schema: applied %s\n", f.schemaPath)
	}

	if f.truncate {
		if err := copyload.Truncate(ctx, pool); err != nil {
			return fmt.Errorf("truncate: %w", err)
		}
		fmt.Println("truncate: cleared customers/invoices/invoice_items")
	}

	// Optional ledger-role grants (documented in 01-role.sql; seeder executes).
	if f.grants {
		if err := schema.Grants(ctx, pool); err != nil {
			return err
		}
		fmt.Println("grants: applied ledger-role table/sequence grants")
	}

	start := time.Now()
	if err := copyload.Load(ctx, pool, cfg); err != nil {
		return err
	}
	fmt.Printf("load: COPY complete (scale=%s, seed=%d, customers=%d, invoices=%d) in %s\n",
		scale.Name, f.seed, scale.Customers, scale.Invoices, time.Since(start).Round(time.Millisecond))

	return reportInvariants(ctx, pool, scale)
}

// reportInvariants runs the post-load checks, prints each, and returns a
// non-nil error (→ non-zero exit) if any failed.
func reportInvariants(ctx context.Context, pool *pgxpool.Pool, scale gen.Scale) error {
	results, ok := invariants.CheckAll(ctx, pool, scale)
	for _, r := range results {
		status := "OK"
		if !r.OK {
			status = "FAIL"
		}
		if r.Note != "" {
			fmt.Printf("invariant %-28s %s  (%s)\n", r.Name, status, r.Note)
		} else {
			fmt.Printf("invariant %-28s %s\n", r.Name, status)
		}
	}
	if !ok {
		return fmt.Errorf("post-load invariants failed")
	}
	fmt.Println("invariants: all passed")
	return nil
}
