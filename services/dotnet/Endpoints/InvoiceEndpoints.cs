using System.Globalization;
using Ledgerline.Cache;
using Ledgerline.Config;
using Ledgerline.Data;
using Ledgerline.Dtos;
using Ledgerline.Infrastructure;
using Ledgerline.Logging;
using Ledgerline.Observability;
using Ledgerline.Problems;

namespace Ledgerline.Endpoints;

public static class InvoiceEndpoints
{
   public static void MapInvoiceEndpoints(this WebApplication app)
   {
      var g = app.MapGroup("/invoices");
      // One category logger for the domain events, resolved once at map time
      // (per-request CreateLogger would be extra work on the hot path).
      var logger = app.Services.GetRequiredService<ILoggerFactory>().CreateLogger("Ledgerline.Invoices");

      // GET /invoices/{id}: cache-aside -> PG fallback. id non-positive/non-integer -> 400; absent -> 404.
      g.MapGet("/{id}", async (
            string id,
            InvoiceCache cache,
            IInvoiceRepository repo,
            RuntimeStatsCollector stats,
            CancellationToken ct) =>
      {
         stats.RecordEndpointCall(EndpointKind.GetInvoice);
         if (!TryParseId(id, out var invoiceId))
            return IdValidationProblem();

         var bytes = await cache.GetOrLoadBytesAsync(invoiceId, repo, ct);
         return bytes is null
            ? NotFound()
            : Results.Bytes(bytes, RequestIdMiddleware.Json);
      });

      // POST /invoices: validate, one tx, Redis DEL, return full invoice. 404 if customer absent.
      // Pattern "" (not "/"): MapGroup("/invoices") + "/" yields RoutePattern.RawText
      // "/invoices/" and the access-log path would diverge from the Java side's
      // "/invoices" (spec/logging.md pins the matched route template).
      g.MapPost("", async Task<IResult> (
            CreateInvoiceRequest req,
            IInvoiceRepository repo,
            InvoiceCache cache,
            RuntimeStatsCollector stats,
            HttpContext ctx,
            CancellationToken ct) =>
      {
         stats.RecordEndpointCall(EndpointKind.CreateInvoice);
         var result = await repo.CreateAsync(req, ct);
         if (result.CustomerMissing)
            return NotFound();

         var created = result.Invoice!;
         await cache.InvalidateAsync(created.Id, ct);

         var requestId = ctx.RequestId();
         LedgerLog.InvoiceCreated(logger, created.CustomerId, created.Id, created.Items.Count, created.TotalMinor, requestId);
         LedgerLog.CacheInvalidated(logger, created.Id, requestId);

         return TypedResults.Created($"/invoices/{created.Id}", created);
      }).Finally(MediaTypeGateMiddleware.JsonBodyGate.Strip);

      // POST /invoices/{id}/pdf-stub: deterministic 32768-byte buffered write, no fsync. 404 if absent.
      g.MapPost("/{id}/pdf-stub", async (
            string id,
            IInvoiceRepository repo,
            LedgerConfig cfg,
            RuntimeStatsCollector stats,
            HttpContext ctx,
            CancellationToken ct) =>
      {
         stats.RecordEndpointCall(EndpointKind.PdfStub);
         if (!TryParseId(id, out var invoiceId))
            return IdValidationProblem();

         // Existence probe is the Q1 header lookup ONLY (spec/db-access.md: one
         // statement, items never loaded, identically on every data layer; the full
         // GetAsync would add a Q2 items query no other layer issues here).
         if (!await repo.InvoiceExistsAsync(invoiceId, ct))
            return NotFound();

         var bytesWritten = await PdfStub.WriteAsync(cfg.PdfDir, invoiceId, ct);

         LedgerLog.PdfStubWritten(logger, invoiceId, bytesWritten, ctx.RequestId());

         var dto = new PdfStubDto(invoiceId, bytesWritten, PdfStub.PathFor(cfg.PdfDir, invoiceId));
         return TypedResults.Ok(dto);
      });
   }

   internal static bool TryParseId(string raw, out long id)
   {
      // AllowLeadingSign (no whitespace trimming): byte-for-byte the accepted grammar of
      // the Java side's Long.parseLong (RequestParams.parseId). Same choice as the page parser.
      id = 0;
      if (!long.TryParse(raw, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var parsed) || parsed < 1)
         return false;
      id = parsed;
      return true;
   }

   internal static IResult IdValidationProblem()
   {
      var errors = new Dictionary<string, string[]> { ["id"] = ["must be a positive integer"] };
      return Results.Problem(
         statusCode: StatusCodes.Status400BadRequest,
         type: ProblemStrings.ValidationType,
         title: ProblemStrings.ValidationTitle,
         detail: ProblemStrings.ValidationDetail,
         extensions: new Dictionary<string, object?> { ["errors"] = errors });
   }

   internal static IResult NotFound() =>
      Results.Problem(
         statusCode: StatusCodes.Status404NotFound,
         type: ProblemStrings.NotFoundType,
         title: ProblemStrings.NotFoundTitle,
         detail: ProblemStrings.NotFoundDetail);
}
