// Package schema applies the canonical DDL and the documented ledger-role grants.
package schema

import (
	"context"
	"fmt"
	"os"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Apply reads the DDL at path (spec/schema.sql) and executes it verbatim. The
// schema file is the single source of truth (spec/schema.sql header); the seeder
// never embeds its own copy of the DDL.
func Apply(ctx context.Context, pool *pgxpool.Pool, path string) error {
	ddl, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read schema %s: %w", path, err)
	}
	if _, err := pool.Exec(ctx, string(ddl)); err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	return nil
}

// grantStmts are the ledger-role table grants documented in
// spec/postgres/initdb/01-role.sql (the seeder is the documented executor of
// these; the init script only creates the role). No DELETE: the workload never
// deletes.
var grantStmts = []string{
	"GRANT SELECT, INSERT, UPDATE ON customers, invoices, invoice_items TO ledger",
	"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledger",
}

// Grants applies the documented ledger-role grants. Idempotent (GRANT is
// repeatable). Requires the ledger role to already exist (created by
// spec/postgres/initdb/01-role.sql at container init).
func Grants(ctx context.Context, pool *pgxpool.Pool) error {
	for _, stmt := range grantStmts {
		if _, err := pool.Exec(ctx, stmt); err != nil {
			return fmt.Errorf("grant (%s): %w", stmt, err)
		}
	}
	return nil
}
