-- spec/db-access.sql — canonical SQL, single source of truth (spec/db-access.md).
-- A REFERENCE TEXT: neither headline stack sends it verbatim. Consumed by:
--   * services/dotnet/Sql/SqlStatements.cs (the `ado` variant) — byte-identity
--     asserted by Ledgerline.Tests/SqlParityTests.
--   * services/java .../data/CustomerRepository — the two NATIVE statements
--     (CustomerExists, UpdateCustomerBalance) — byte-identity asserted by
--     SqlParityTest once JDBC's positional placeholders are normalised.
-- Both ORMs generate the rest; those shapes are pinned against literals held
-- inside Ledgerline.Tests/EfQueryShapeTests and StatementShapeTest, and every
-- generated deviation is captured against a live database and listed in
-- spec/db-access.md's deviation table.
-- The `-- name: <Name>` lines are how both parity tests split this file into
-- statement bodies. The `:one` / `:many` / `:exec` suffixes are inert labels.

-- name: GetInvoice :one
SELECT id, customer_id, status, currency, total_minor, created_at
FROM invoices
WHERE id = $1;

-- name: GetInvoiceItems :many
SELECT id, sku, description, qty, unit_price_minor, line_total_minor
FROM invoice_items
WHERE invoice_id = $1
ORDER BY id;

-- name: ListInvoicesByCustomer :many
SELECT id, customer_id, status, currency, total_minor, created_at
FROM invoices
WHERE customer_id = $1
ORDER BY id DESC
LIMIT 20 OFFSET $2;

-- name: CustomerExists :one
SELECT EXISTS(SELECT 1 FROM customers WHERE id = $1) AS exists;

-- name: InsertInvoice :one
INSERT INTO invoices (customer_id, status, currency, total_minor)
VALUES ($1, $2, $3, $4)
RETURNING id, created_at;

-- name: InsertInvoiceItem :one
INSERT INTO invoice_items (invoice_id, sku, description, qty, unit_price_minor, line_total_minor)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING id;

-- name: UpdateCustomerBalance :exec
UPDATE customers
SET balance_minor = balance_minor + $2
WHERE id = $1;
