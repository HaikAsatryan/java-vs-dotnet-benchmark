using System.Data;
using Ledgerline.Dtos;
using Ledgerline.Json;
using Ledgerline.Sql;
using Npgsql;

namespace Ledgerline.Data;

/// Raw ADO.NET variant. Uses the EXACT SQL statement bodies from spec/db-access.sql
/// (SqlStatements), which carry PG positional placeholders ($1..). We bind via positional
/// NpgsqlParameters (parameter NAME left unset -> Npgsql binds by position to $1..). This
/// keeps query SHAPE byte-identical to the canonical SQL.
///
/// Create-path round-trip ledger (spec/db-access.md): Q7 existence, BEGIN, Q4 header
/// (RETURNING id), one NpgsqlBatch carrying Q5xN + Q6, COMMIT. The header RETURNING id is
/// needed before the item FKs can bind, so items+balance go in a batch after it, within the
/// SAME explicit transaction.
public sealed class AdoInvoiceRepository(NpgsqlDataSource source) : IInvoiceRepository
{
   private const int PageSize = 20;

   public async Task<InvoiceDto?> GetAsync(long id, CancellationToken ct)
   {
      // Q1 + Q2 in ONE NpgsqlBatch round-trip (spec/db-access.md: the ado and dapper
      // variants batch the pair; the two headline ORMs issue them sequentially). Both
      // statements are always executed server-side, including on the absent-id 404
      // path — disclosed in spec/db-access.md's absent-id note.
      await using var conn = await source.OpenConnectionAsync(ct);
      await using var batch = new NpgsqlBatch(conn);

      var header = new NpgsqlBatchCommand(SqlStatements.GetInvoice);
      header.Parameters.Add(new NpgsqlParameter { Value = id });
      batch.BatchCommands.Add(header);

      var itemsCmd = new NpgsqlBatchCommand(SqlStatements.GetInvoiceItems);
      itemsCmd.Parameters.Add(new NpgsqlParameter { Value = id });
      batch.BatchCommands.Add(itemsCmd);

      await using var r = await batch.ExecuteReaderAsync(ct);
      if (!await r.ReadAsync(ct))
         return null;

      var (invoiceId, customerId, status, currency, totalMinor, createdAt) =
         (r.GetInt64(0), r.GetInt64(1), r.GetString(2), r.GetString(3), r.GetInt64(4), r.GetDateTime(5));

      await r.NextResultAsync(ct);
      var items = new List<InvoiceItemDto>();
      while (await r.ReadAsync(ct))
         items.Add(new InvoiceItemDto(r.GetInt64(0), r.GetString(1), r.GetString(2), r.GetInt32(3), r.GetInt64(4), r.GetInt64(5)));

      return new InvoiceDto(
         invoiceId, customerId, status.TrimEnd(), currency.TrimEnd(),
         totalMinor, Rfc3339Micros.Format6(createdAt), items);
   }

   public async Task<bool> InvoiceExistsAsync(long id, CancellationToken ct)
   {
      // Q1 header lookup only (same statement text as GetAsync), row presence = existence.
      await using var conn = await source.OpenConnectionAsync(ct);
      await using var cmd = new NpgsqlCommand(SqlStatements.GetInvoice, conn);
      cmd.Parameters.Add(new NpgsqlParameter { Value = id });
      await using var r = await cmd.ExecuteReaderAsync(ct);
      return await r.ReadAsync(ct);
   }

   public async Task<IReadOnlyList<InvoiceSummaryDto>> ListByCustomerAsync(long customerId, int page, CancellationToken ct)
   {
      await using var conn = await source.OpenConnectionAsync(ct);
      // int (not long): the OFFSET parameter binds as int4, matching EF's int Skip, so the
      // prepared-statement signatures are identical across the three layers.
      var offset = (page - 1) * PageSize;

      var rows = new List<InvoiceSummaryDto>();
      await using var cmd = new NpgsqlCommand(SqlStatements.ListInvoicesByCustomer, conn);
      cmd.Parameters.Add(new NpgsqlParameter { Value = customerId });
      cmd.Parameters.Add(new NpgsqlParameter { Value = offset });
      await using var r = await cmd.ExecuteReaderAsync(ct);
      while (await r.ReadAsync(ct))
         rows.Add(new InvoiceSummaryDto(r.GetInt64(0), r.GetInt64(1), r.GetString(2).TrimEnd(), r.GetString(3).TrimEnd(), r.GetInt64(4), Rfc3339Micros.Format6(r.GetDateTime(5))));
      return rows;
   }

   public async Task<bool> CustomerExistsAsync(long customerId, CancellationToken ct)
   {
      await using var conn = await source.OpenConnectionAsync(ct);
      await using var cmd = new NpgsqlCommand(SqlStatements.CustomerExists, conn);
      cmd.Parameters.Add(new NpgsqlParameter { Value = customerId });
      var scalar = await cmd.ExecuteScalarAsync(ct);
      return scalar is true;
   }

   public async Task<CreateResult> CreateAsync(CreateInvoiceRequest req, CancellationToken ct)
   {
      await using var conn = await source.OpenConnectionAsync(ct);

      // Q7 existence.
      await using (var existsCmd = new NpgsqlCommand(SqlStatements.CustomerExists, conn))
      {
         existsCmd.Parameters.Add(new NpgsqlParameter { Value = req.CustomerId });
         if (await existsCmd.ExecuteScalarAsync(ct) is not true)
            return new CreateResult(null);
      }

      var totalMinor = 0L;
      var lineTotals = new long[req.Items.Count];
      for (var i = 0; i < req.Items.Count; i++)
      {
         lineTotals[i] = (long)req.Items[i].Qty * req.Items[i].UnitPriceMinor;
         totalMinor += lineTotals[i];
      }

      await using var tx = await conn.BeginTransactionAsync(IsolationLevel.ReadCommitted, ct);

      // Q4 header RETURNING id, created_at.
      long invoiceId;
      DateTime createdAt;
      await using (var insertCmd = new NpgsqlCommand(SqlStatements.InsertInvoice, conn, tx))
      {
         insertCmd.Parameters.Add(new NpgsqlParameter { Value = req.CustomerId });
         insertCmd.Parameters.Add(new NpgsqlParameter { Value = req.Status });
         insertCmd.Parameters.Add(new NpgsqlParameter { Value = req.Currency });
         insertCmd.Parameters.Add(new NpgsqlParameter { Value = totalMinor });
         await using var r = await insertCmd.ExecuteReaderAsync(ct);
         if (!await r.ReadAsync(ct))
            throw new InvalidOperationException("InsertInvoice returned no row.");
         invoiceId = r.GetInt64(0);
         createdAt = r.GetDateTime(1);
      }

      // Q5xN items (RETURNING id) + Q6 balance, one batch inside the same tx.
      var itemIds = new long[req.Items.Count];
      await using (var batch = new NpgsqlBatch(conn, tx))
      {
         for (var i = 0; i < req.Items.Count; i++)
         {
            var it = req.Items[i];
            var cmd = new NpgsqlBatchCommand(SqlStatements.InsertInvoiceItem);
            cmd.Parameters.Add(new NpgsqlParameter { Value = invoiceId });
            cmd.Parameters.Add(new NpgsqlParameter { Value = it.Sku });
            cmd.Parameters.Add(new NpgsqlParameter { Value = it.Description });
            cmd.Parameters.Add(new NpgsqlParameter { Value = it.Qty });
            cmd.Parameters.Add(new NpgsqlParameter { Value = it.UnitPriceMinor });
            cmd.Parameters.Add(new NpgsqlParameter { Value = lineTotals[i] });
            batch.BatchCommands.Add(cmd);
         }

         var balance = new NpgsqlBatchCommand(SqlStatements.UpdateCustomerBalance);
         balance.Parameters.Add(new NpgsqlParameter { Value = req.CustomerId });
         balance.Parameters.Add(new NpgsqlParameter { Value = totalMinor });
         batch.BatchCommands.Add(balance);

         // N item RETURNING id result sets, then the balance non-query.
         await using var r = await batch.ExecuteReaderAsync(ct);
         for (var i = 0; i < req.Items.Count; i++)
         {
            if (!await r.ReadAsync(ct))
               throw new InvalidOperationException("InsertInvoiceItem returned no row.");
            itemIds[i] = r.GetInt64(0);
            await r.NextResultAsync(ct);
         }
      }

      await tx.CommitAsync(ct);

      var items = new List<InvoiceItemDto>(req.Items.Count);
      for (var i = 0; i < req.Items.Count; i++)
      {
         var it = req.Items[i];
         items.Add(new InvoiceItemDto(itemIds[i], it.Sku, it.Description, it.Qty, it.UnitPriceMinor, lineTotals[i]));
      }

      var dto = new InvoiceDto(
         invoiceId, req.CustomerId, req.Status, req.Currency,
         totalMinor, Rfc3339Micros.Format6(createdAt), items);
      return new CreateResult(dto);
   }
}
