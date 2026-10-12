using Ledgerline.Cache;
using Ledgerline.Data;
using Ledgerline.Dtos;
using Ledgerline.Observability;
using Microsoft.AspNetCore.Http.HttpResults;
using Microsoft.EntityFrameworkCore;

namespace Ledgerline.Endpoints;

public static class HealthEndpoints
{
   private static readonly HealthDto Ok = new("ok");
   // Only "ok" is spec-frozen; "unavailable" is the aligned failure string, both sides.
   private static readonly HealthDto Unavailable = new("unavailable");

   public static void MapHealthEndpoints(this WebApplication app)
   {
      // Liveness: static body, no DB/Redis touch. The HTTP floor. Excluded from access log.
      // Counted in endpoint_calls on both services; the per-endpoint warmup gate
      // reads these counters for every mix endpoint including healthz.
      app.MapGet("/healthz", (RuntimeStatsCollector stats) =>
      {
         stats.RecordEndpointCall(EndpointKind.Healthz);
         return TypedResults.Ok(Ok);
      });

      // Readiness: DB pool reachable + Redis PING. 200/503. Not in the workload mix.
      app.MapGet("/readyz", async Task<Results<Ok<HealthDto>, JsonHttpResult<HealthDto>>> (
            LedgerDbContext db, InvoiceCache cache, CancellationToken ct) =>
      {
         // DB first, Redis only if the DB is reachable: same short-circuit on the Java side.
         var ready = await CanReachDbAsync(db, ct) && await cache.PingAsync();
         return ready
            ? TypedResults.Ok(Ok)
            : TypedResults.Json(Unavailable, statusCode: StatusCodes.Status503ServiceUnavailable);
      });
   }

   private static async Task<bool> CanReachDbAsync(LedgerDbContext db, CancellationToken ct)
   {
      try
      {
         return await db.Database.CanConnectAsync(ct);
      }
      catch
      {
         return false;
      }
   }
}
