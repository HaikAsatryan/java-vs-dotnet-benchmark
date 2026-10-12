using Ledgerline.Data.Entities;
using Ledgerline.Dtos;
using Ledgerline.Json;
using Microsoft.EntityFrameworkCore;

namespace Ledgerline.Data;

public sealed class EfInvoiceRepository(LedgerDbContext db) : IInvoiceRepository
{
   public const int PageSize = 20;

   public async Task<InvoiceDto?> GetAsync(long id, CancellationToken ct)
   {
      // Q1 + Q2 on ONE pooled-connection acquisition (spec/db-access.md pin: "EF: two
      // queries in one connection acquisition"); without the explicit open, each query
      // would rent and return the Npgsql connection separately.
      await db.Database.OpenConnectionAsync(ct);
      try
      {
         // Q1: header by PK.
         var header = await db.Invoices.AsNoTracking()
            .Where(i => i.Id == id)
            .Select(i => new { i.Id, i.CustomerId, i.Status, i.Currency, i.TotalMinor, i.CreatedAt })
            .FirstOrDefaultAsync(ct);

         if (header is null)
            return null;

         // Q2: items WHERE invoice_id ORDER BY id.
         var items = await db.InvoiceItems.AsNoTracking()
            .Where(it => it.InvoiceId == id)
            .OrderBy(it => it.Id)
            .Select(it => new InvoiceItemDto(it.Id, it.Sku, it.Description, it.Qty, it.UnitPriceMinor, it.LineTotalMinor))
            .ToListAsync(ct);

         return new InvoiceDto(
            header.Id, header.CustomerId, header.Status.TrimEnd(), header.Currency.TrimEnd(),
            header.TotalMinor, Rfc3339Micros.Format6(header.CreatedAt), items);
      }
      finally
      {
         await db.Database.CloseConnectionAsync();
      }
   }

   public async Task<bool> InvoiceExistsAsync(long id, CancellationToken ct)
   {
      // Same Q1 header projection as GetAsync so both paths share one statement shape
      // (and, under prepared modes, one prepared statement). spec/db-access.md: pdf-stub
      // probes existence with the Q1 header lookup ONLY, identically on every data layer.
      var header = await db.Invoices.AsNoTracking()
         .Where(i => i.Id == id)
         .Select(i => new { i.Id, i.CustomerId, i.Status, i.Currency, i.TotalMinor, i.CreatedAt })
         .FirstOrDefaultAsync(ct);
      return header is not null;
   }

   public async Task<IReadOnlyList<InvoiceSummaryDto>> ListByCustomerAsync(long customerId, int page, CancellationToken ct)
   {
      // Q3: ORDER BY id DESC LIMIT 20 OFFSET (page-1)*20. Must hit idx_invoices_customer_id_id_desc.
      // Wire-shape note: EF 10 parameterizes the row-limiting operator, so this
      // ships `LIMIT $2 OFFSET $3` (three parameters) where ado/dapper send the
      // canonical literal `LIMIT 20 OFFSET $2`. EF.Constant cannot be used in
      // Take(int) (verified: eager evaluation throws), so the parameterized form
      // IS idiomatic EF and is pinned by EfQueryShapeTests + disclosed in
      // spec/db-access.md and NOTES-equivalence.md. Same index path either way.
      var rows = await db.Invoices.AsNoTracking()
         .Where(i => i.CustomerId == customerId)
         .OrderByDescending(i => i.Id)
         .Skip((page - 1) * PageSize)
         .Take(PageSize)
         .Select(i => new { i.Id, i.CustomerId, i.Status, i.Currency, i.TotalMinor, i.CreatedAt })
         .ToListAsync(ct);

      return [.. rows.Select(i => new InvoiceSummaryDto(
         i.Id, i.CustomerId, i.Status.TrimEnd(), i.Currency.TrimEnd(), i.TotalMinor,
         Rfc3339Micros.Format6(i.CreatedAt)))];
   }

   public Task<bool> CustomerExistsAsync(long customerId, CancellationToken ct) =>
      db.Customers.AnyAsync(c => c.Id == customerId, ct);

   public async Task<CreateResult> CreateAsync(CreateInvoiceRequest req, CancellationToken ct)
   {
      // Q7 existence probe: EXISTS, outside the tx on both stacks (spec/db-access.md;
      // the Java side sends the canonical text as a native query, EF generates its
      // aliased form — pinned as a deviation there).
      if (!await db.Customers.AnyAsync(c => c.Id == req.CustomerId, ct))
         return new CreateResult(null);

      var totalMinor = 0L;
      var invoice = new Invoice
      {
         CustomerId = req.CustomerId,
         Status = req.Status,
         Currency = req.Currency,
      };

      foreach (var item in req.Items)
      {
         var lineTotal = (long)item.Qty * item.UnitPriceMinor;
         totalMinor += lineTotal;
         invoice.Items.Add(new InvoiceItem
         {
            Sku = item.Sku,
            Description = item.Description,
            Qty = item.Qty,
            UnitPriceMinor = item.UnitPriceMinor,
            LineTotalMinor = lineTotal,
         });
      }

      invoice.TotalMinor = totalMinor;
      db.Invoices.Add(invoice);

      // One explicit transaction (DB-default READ COMMITTED, the same level as the Java
      // side's bare BEGIN — spec/db-access.md): SaveChanges batches the header insert
      // (RETURNING id, created_at) + N item inserts; the balance then moves as an ATOMIC
      // increment (Q6 semantics). A tracked read-modify-write here would write an
      // absolute balance and lose concurrent updates on hot customers: Hibernate's
      // native Q6 and the ADO variant increment in place, and the seeder invariant
      // balance_minor == sum(total_minor) only survives the increment form.
      await using var tx = await db.Database.BeginTransactionAsync(ct);
      await db.SaveChangesAsync(ct);
      await db.Customers
         .Where(c => c.Id == req.CustomerId)
         .ExecuteUpdateAsync(s => s.SetProperty(c => c.BalanceMinor, c => c.BalanceMinor + totalMinor), ct);
      await tx.CommitAsync(ct);

      var items = invoice.Items
         .OrderBy(it => it.Id)
         .Select(it => new InvoiceItemDto(it.Id, it.Sku, it.Description, it.Qty, it.UnitPriceMinor, it.LineTotalMinor))
         .ToList();

      var dto = new InvoiceDto(
         invoice.Id, invoice.CustomerId, invoice.Status, invoice.Currency.TrimEnd(),
         invoice.TotalMinor, Rfc3339Micros.Format6(invoice.CreatedAt), items);

      return new CreateResult(dto);
   }
}
