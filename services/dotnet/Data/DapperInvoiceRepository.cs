using System.Data;
using Dapper;
using Ledgerline.Dtos;
using Ledgerline.Json;
using Ledgerline.Sql;
using Npgsql;

namespace Ledgerline.Data;

/// Dapper variant (DATA_LAYER=dapper): the micro-ORM middle point between EF Core and
/// raw ADO.NET. Same shared NpgsqlDataSource (one pool), same wire statement set as the
/// other layers: DapperSql carries the spec bodies with named parameters, which Npgsql
/// rewrites to the identical positional text (asserted by DapperSqlParityTests).
///
/// Two deliberate, disclosed edges:
///  - Reads and the create header/existence are idiomatic Dapper. The N item inserts +
///    balance update use NpgsqlBatch exactly like AdoInvoiceRepository: Dapper has no
///    batch API (its ExecuteAsync-over-a-list runs one round-trip per element), and the
///    benchmark holds the wire ledger constant across layers so the contrast isolates
///    mapping cost, not round-trip count.
///  - Dapper materializes a row object for the pdf-stub existence probe (its natural
///    idiom); the ado variant checks row presence without decoding. Same single Q1 statement.
public sealed class DapperInvoiceRepository(NpgsqlDataSource source) : IInvoiceRepository
{
   private const int PageSize = 20;

   static DapperInvoiceRepository()
   {
      // snake_case columns -> PascalCase members, the standard Dapper+PostgreSQL setting.
      DefaultTypeMap.MatchNamesWithUnderscores = true;
   }

   public async Task<InvoiceDto?> GetAsync(long id, CancellationToken ct)
   {
      // Q1 + Q2 in one QueryMultipleAsync; Npgsql turns the two statements into one
      // batch (single round-trip, same wire shape as the ado variant's NpgsqlBatch).
      await using var conn = await source.OpenConnectionAsync(ct);
      using var grid = await conn.QueryMultipleAsync(
         new CommandDefinition(DapperSql.GetInvoiceWithItems, new { id }, cancellationToken: ct));

      var header = await grid.ReadFirstOrDefaultAsync<InvoiceRow>();
      if (header is null)
         return null;

      var items = (await grid.ReadAsync<ItemRow>())
         .Select(it => new InvoiceItemDto(it.Id, it.Sku, it.Description, it.Qty, it.UnitPriceMinor, it.LineTotalMinor))
         .ToList();

      return new InvoiceDto(
         header.Id, header.CustomerId, header.Status.TrimEnd(), header.Currency.TrimEnd(),
         header.TotalMinor, Rfc3339Micros.Format6(header.CreatedAt), items);
   }

   public async Task<bool> InvoiceExistsAsync(long id, CancellationToken ct)
   {
      // pdf-stub 404 probe: the Q1 header lookup only (spec/db-access.md).
      await using var conn = await source.OpenConnectionAsync(ct);
      var header = await conn.QueryFirstOrDefaultAsync<InvoiceRow>(
         new CommandDefinition(DapperSql.GetInvoice, new { id }, cancellationToken: ct));
      return header is not null;
   }

   public async Task<IReadOnlyList<InvoiceSummaryDto>> ListByCustomerAsync(long customerId, int page, CancellationToken ct)
   {
      await using var conn = await source.OpenConnectionAsync(ct);
      // int offset -> int4 parameter, same prepared-statement signature as EF/ADO.
      var offset = (page - 1) * PageSize;
      var rows = await conn.QueryAsync<InvoiceRow>(
         new CommandDefinition(DapperSql.ListInvoicesByCustomer, new { customerId, offset }, cancellationToken: ct));
      return [.. rows.Select(r => new InvoiceSummaryDto(
         r.Id, r.CustomerId, r.Status.TrimEnd(), r.Currency.TrimEnd(), r.TotalMinor,
         Rfc3339Micros.Format6(r.CreatedAt)))];
   }

   public async Task<bool> CustomerExistsAsync(long customerId, CancellationToken ct)
   {
      await using var conn = await source.OpenConnectionAsync(ct);
      return await conn.ExecuteScalarAsync<bool>(
         new CommandDefinition(DapperSql.CustomerExists, new { id = customerId }, cancellationToken: ct));
   }

   public async Task<CreateResult> CreateAsync(CreateInvoiceRequest req, CancellationToken ct)
   {
      await using var conn = await source.OpenConnectionAsync(ct);

      // Q7 existence.
      var exists = await conn.ExecuteScalarAsync<bool>(
         new CommandDefinition(DapperSql.CustomerExists, new { id = req.CustomerId }, cancellationToken: ct));
      if (!exists)
         return new CreateResult(null);

      var totalMinor = 0L;
      var lineTotals = new long[req.Items.Count];
      for (var i = 0; i < req.Items.Count; i++)
      {
         lineTotals[i] = (long)req.Items[i].Qty * req.Items[i].UnitPriceMinor;
         totalMinor += lineTotals[i];
      }

      await using var tx = await conn.BeginTransactionAsync(IsolationLevel.ReadCommitted, ct);

      // Q4 header RETURNING id, created_at.
      var (invoiceId, createdAt) = await conn.QuerySingleAsync<(long, DateTime)>(
         new CommandDefinition(
            DapperSql.InsertInvoice,
            new { customerId = req.CustomerId, status = req.Status, currency = req.Currency, totalMinor },
            transaction: tx, cancellationToken: ct));

      // Q5xN items (RETURNING id) + Q6 balance: one NpgsqlBatch in the same tx (see
      // class doc: Dapper has no batch API and the wire ledger is held constant).
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

   // Classic Dapper POCOs (settable properties; snake_case columns map via
   // MatchNamesWithUnderscores). InvoiceRow serves headers and list summaries:
   // both read the same six Q1/Q3 columns.
   private sealed class InvoiceRow
   {
      public long Id { get; set; }
      public long CustomerId { get; set; }
      public string Status { get; set; } = "";
      public string Currency { get; set; } = "";
      public long TotalMinor { get; set; }
      public DateTime CreatedAt { get; set; }
   }

   private sealed class ItemRow
   {
      public long Id { get; set; }
      public string Sku { get; set; } = "";
      public string Description { get; set; } = "";
      public int Qty { get; set; }
      public long UnitPriceMinor { get; set; }
      public long LineTotalMinor { get; set; }
   }
}
