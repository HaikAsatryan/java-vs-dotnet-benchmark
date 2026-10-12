namespace Ledgerline.Sql;

/// The spec statements with NAMED parameters for the Dapper variant. Dapper binds
/// parameters by name, and Npgsql client-side-rewrites each @name to a positional
/// $n (in order of first appearance) before it hits the wire, so the TEXT sent to
/// PostgreSQL is byte-identical to SqlStatements (asserted by DapperSqlParityTests,
/// which performs the same substitution and compares). Paying that per-command
/// rewriting is part of what a production Dapper app costs; it is not tuned away.
public static class DapperSql
{
   // -- name: GetInvoice :one, then GetInvoiceItems :many: one Dapper
   // QueryMultipleAsync; Npgsql rewrites the two statements into a single
   // round-trip batch, the same wire shape as the ado variant's NpgsqlBatch.
   public const string GetInvoiceWithItems =
      "SELECT id, customer_id, status, currency, total_minor, created_at\n" +
      "FROM invoices\n" +
      "WHERE id = @id;\n" +
      "SELECT id, sku, description, qty, unit_price_minor, line_total_minor\n" +
      "FROM invoice_items\n" +
      "WHERE invoice_id = @id\n" +
      "ORDER BY id";

   // -- name: GetInvoice :one (pdf-stub existence probe, header only)
   public const string GetInvoice =
      "SELECT id, customer_id, status, currency, total_minor, created_at\n" +
      "FROM invoices\n" +
      "WHERE id = @id";

   // -- name: ListInvoicesByCustomer :many
   public const string ListInvoicesByCustomer =
      "SELECT id, customer_id, status, currency, total_minor, created_at\n" +
      "FROM invoices\n" +
      "WHERE customer_id = @customerId\n" +
      "ORDER BY id DESC\n" +
      "LIMIT 20 OFFSET @offset";

   // -- name: CustomerExists :one
   public const string CustomerExists =
      "SELECT EXISTS(SELECT 1 FROM customers WHERE id = @id) AS exists";

   // -- name: InsertInvoice :one
   public const string InsertInvoice =
      "INSERT INTO invoices (customer_id, status, currency, total_minor)\n" +
      "VALUES (@customerId, @status, @currency, @totalMinor)\n" +
      "RETURNING id, created_at";
}
