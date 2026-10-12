using Ledgerline.Dtos;
using Ledgerline.Infrastructure;
using Ledgerline.Logging;
using Ledgerline.Observability;
using Ledgerline.Pricing;
using Microsoft.AspNetCore.Http.HttpResults;

namespace Ledgerline.Endpoints;

public static class PricingEndpoints
{
   public static void MapPricingEndpoints(this WebApplication app)
   {
      var g = app.MapGroup("/pricing");

      // POST /pricing/quote: tiered pricing over int64 minor units. No DB, no Redis.
      g.MapPost("/quote", (
            QuoteRequest req,
            PricingEngine engine,
            RuntimeStatsCollector stats,
            ILogger<PricingEngine> logger,
            HttpContext ctx) =>
      {
         stats.RecordEndpointCall(EndpointKind.PricingQuote);
         var result = engine.Quote(req);
         LedgerLog.QuoteComputed(logger, req.Items.Count, result.TotalMinor, ctx.RequestId());
         return TypedResults.Ok(result);
      }).Finally(MediaTypeGateMiddleware.JsonBodyGate.Strip);
   }
}
