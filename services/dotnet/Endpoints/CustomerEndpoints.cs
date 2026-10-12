using System.Globalization;
using Ledgerline.Data;
using Ledgerline.Dtos;
using Ledgerline.Observability;
using Ledgerline.Problems;

namespace Ledgerline.Endpoints;

public static class CustomerEndpoints
{
   private const int PageSize = 20;
   private const int PageMax = 100000;

   public static void MapCustomerEndpoints(this WebApplication app)
   {
      var g = app.MapGroup("/customers");

      // GET /customers/{id}/invoices?page=N: page of 20, ORDER BY id DESC, OFFSET (page-1)*20.
      // Page beyond data -> 200 empty. Customer existence probed ONLY when the page is empty.
      g.MapGet("/{id}/invoices", async (
            string id,
            IInvoiceRepository repo,
            RuntimeStatsCollector stats,
            HttpContext ctx,
            CancellationToken ct) =>
      {
         stats.RecordEndpointCall(EndpointKind.ListInvoices);

         // 400-before-404, and id + page errors AGGREGATED into one errors map,
         // mirroring the Java side's CustomerController. page comes from the FIRST
         // query value; string binding would comma-join repeated params.
         var errors = new Dictionary<string, string[]>();
         if (!InvoiceEndpoints.TryParseId(id, out var customerId))
            errors["id"] = ["must be a positive integer"];

         // page parsed by hand, mirroring the Java side's RequestParams.parsePage
         // exactly: an int? parameter would fail BINDING on non-integer/>int32 input
         // and emit a 400 with NO errors map (divergent wire bytes). Empty -> 1;
         // non-integer or <1 -> page_min; >100000 -> page_max. AllowLeadingSign (no
         // whitespace trimming): byte-for-byte the accepted grammar of Long.parseLong.
         var p = 1;
         var page = ctx.Request.Query["page"].Count > 0 ? ctx.Request.Query["page"][0] : null;
         if (!string.IsNullOrEmpty(page))
         {
            if (!long.TryParse(page, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var parsed) || parsed < 1)
               errors["page"] = ["must be >= 1"];
            else if (parsed > PageMax)
               errors["page"] = ["must be <= 100000"];
            else
               p = (int)parsed;
         }

         if (errors.Count > 0)
            return ValidationProblem(errors);

         var items = await repo.ListByCustomerAsync(customerId, p, ct);
         if (items.Count == 0)
         {
            // Q3b existence probe only on an empty page; 404 if the customer is absent.
            var exists = await repo.CustomerExistsAsync(customerId, ct);
            if (!exists)
               return InvoiceEndpoints.NotFound();
         }

         var pageDto = new InvoiceListPageDto(customerId, p, PageSize, items);
         return Results.Ok(pageDto);
      });
   }

   private static IResult ValidationProblem(Dictionary<string, string[]> errors) =>
      Results.Problem(
         statusCode: StatusCodes.Status400BadRequest,
         type: ProblemStrings.ValidationType,
         title: ProblemStrings.ValidationTitle,
         detail: ProblemStrings.ValidationDetail,
         extensions: new Dictionary<string, object?> { ["errors"] = errors });
}
