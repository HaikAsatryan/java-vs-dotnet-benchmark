using Ledgerline.Data;
using Microsoft.EntityFrameworkCore;
using Xunit;

namespace Ledgerline.Tests;

/// Captures EF-generated SQL via ToQueryString() and asserts it matches the canonical Q3 shape
/// (spec/db-access.sql ListInvoicesByCustomer): WHERE customer_id, ORDER BY id DESC, LIMIT 20
/// OFFSET. The shape must hit idx_invoices_customer_id_id_desc with no sort node. No live DB needed.
public class EfQueryShapeTests
{
   private static LedgerDbContext NewContext()
   {
      // Same wiring as Program.cs: stock EF builds the model from OnModelCreating.
      var builder = new DbContextOptionsBuilder<LedgerDbContext>()
         .UseNpgsql("Host=localhost;Database=ledgerline;Username=ledger;Password=ledger");
      return new LedgerDbContext(builder.Options);
   }

   [Fact]
   public void ListByCustomer_Page1_MatchesQ3Shape()
   {
      using var db = NewContext();
      const int page = 1;

      var sql = db.Invoices.AsNoTracking()
         .Where(i => i.CustomerId == 123L)
         .OrderByDescending(i => i.Id)
         .Skip((page - 1) * 20)
         .Take(20)
         .Select(i => new { i.Id, i.CustomerId, i.Status, i.Currency, i.TotalMinor, i.CreatedAt })
         .ToQueryString();

      var normalized = Normalize(sql);

      Assert.Contains("from invoices", normalized);
      Assert.Contains("where i.customer_id =", normalized);
      Assert.Contains("order by i.id desc", normalized);
      // EF 10 parameterizes the row-limiting operator: the wire shape is
      // `LIMIT $n` (an extra parameter vs the canonical/ado/dapper literal LIMIT 20).
      // EF.Constant cannot force a literal in Take(int) (eager evaluation
      // throws), so the parameterized form is the pinned, DISCLOSED EF shape
      // (spec/db-access.md + NOTES-equivalence.md). If a future EF/Npgsql
      // changes this rendering, this assertion fails and the disclosure must be
      // re-checked; that is its job.
      Assert.Contains("limit @", normalized);
      Assert.DoesNotContain("limit 20", normalized);
      Assert.DoesNotContain("count(", normalized);   // no COUNT (no total-count field)
   }

   [Fact]
   public void ListByCustomer_Page3_EmitsOffset()
   {
      using var db = NewContext();
      const int page = 3;

      var sql = db.Invoices.AsNoTracking()
         .Where(i => i.CustomerId == 7L)
         .OrderByDescending(i => i.Id)
         .Skip((page - 1) * 20)
         .Take(20)
         .Select(i => new { i.Id, i.CustomerId, i.Status, i.Currency, i.TotalMinor, i.CreatedAt })
         .ToQueryString();

      var normalized = Normalize(sql);
      Assert.Contains("limit @", normalized);        // see Page1 test: pinned EF shape
      Assert.Contains("offset", normalized);
      Assert.Contains("order by i.id desc", normalized);
   }

   [Fact]
   public void GetInvoiceItems_OrdersByIdAscending()
   {
      using var db = NewContext();
      var sql = db.InvoiceItems.AsNoTracking()
         .Where(it => it.InvoiceId == 5L)
         .OrderBy(it => it.Id)
         .Select(it => new { it.Id, it.Sku })
         .ToQueryString();

      var normalized = Normalize(sql);
      Assert.Contains("where i.invoice_id =", normalized);
      Assert.Contains("order by i.id", normalized);
      Assert.DoesNotContain("order by i.id desc", normalized);
   }

   private static string Normalize(string sql) =>
      string.Join(' ', sql.Split([' ', '\r', '\n', '\t'], StringSplitOptions.RemoveEmptyEntries))
            .ToLowerInvariant();
}
