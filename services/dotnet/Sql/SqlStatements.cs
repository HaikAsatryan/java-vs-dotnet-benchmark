namespace Ledgerline.Sql;

/// Canonical SQL statement bodies, byte-identical to the bodies in spec/db-access.sql
/// with the file's trailing ';' separator stripped (the Java side's SqlParityTest strips
/// the ';' identically for its two native queries). A unit test asserts equality against
/// the spec file (parsed by the `-- name:` annotations). LF newlines. spec/db-access.sql
/// is the single source of truth for query SHAPE; the two ORMs' generated deviations from
/// it are pinned in spec/db-access.md.
public static class SqlStatements
{
   // -- name: GetInvoice :one
   public const string GetInvoice =
      "SELECT id, customer_id, status, currency, total_minor, created_at\n" +
      "FROM invoices\n" +
      "WHERE id = $1";

   // -- name: GetInvoiceItems :many
   public const string GetInvoiceItems =
      "SELECT id, sku, description, qty, unit_price_minor, line_total_minor\n" +
      "FROM invoice_items\n" +
      "WHERE invoice_id = $1\n" +
      "ORDER BY id";

   // -- name: ListInvoicesByCustomer :many
   public const string ListInvoicesByCustomer =
      "SELECT id, customer_id, status, currency, total_minor, created_at\n" +
      "FROM invoices\n" +
      "WHERE customer_id = $1\n" +
      "ORDER BY id DESC\n" +
      "LIMIT 20 OFFSET $2";

   // -- name: CustomerExists :one
   public const string CustomerExists =
      "SELECT EXISTS(SELECT 1 FROM customers WHERE id = $1) AS exists";

   // -- name: InsertInvoice :one
   public const string InsertInvoice =
      "INSERT INTO invoices (customer_id, status, currency, total_minor)\n" +
      "VALUES ($1, $2, $3, $4)\n" +
      "RETURNING id, created_at";

   // -- name: InsertInvoiceItem :one
   public const string InsertInvoiceItem =
      "INSERT INTO invoice_items (invoice_id, sku, description, qty, unit_price_minor, line_total_minor)\n" +
      "VALUES ($1, $2, $3, $4, $5, $6)\n" +
      "RETURNING id";

   // -- name: UpdateCustomerBalance :exec
   public const string UpdateCustomerBalance =
      "UPDATE customers\n" +
      "SET balance_minor = balance_minor + $2\n" +
      "WHERE id = $1";
}
