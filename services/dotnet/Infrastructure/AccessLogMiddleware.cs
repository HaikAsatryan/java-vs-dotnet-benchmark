using System.Diagnostics;
using Ledgerline.Logging;
using Microsoft.AspNetCore.Routing;

namespace Ledgerline.Infrastructure;

/// Exactly one http_access line per request, with the MATCHED ROUTE TEMPLATE as `path`
/// (e.g. "/invoices/{id}"), never the raw URL. /healthz, /readyz, /runtime-stats excluded.
/// duration_ms measured handler-entry -> response-write-complete via a monotonic clock.
public sealed class AccessLogMiddleware(RequestDelegate next, ILogger<AccessLogMiddleware> logger)
{
   private static readonly HashSet<string> Excluded = new(StringComparer.Ordinal)
   {
      "/healthz", "/readyz", "/runtime-stats",
   };

   public async Task InvokeAsync(HttpContext ctx)
   {
      // Snapshot the template BEFORE the pipeline runs: routing has already matched
      // (implicit UseRouting precedes user middleware), and ExceptionHandlerMiddleware
      // CLEARS the endpoint while handling a thrown request: reading it afterwards
      // would silently drop the access line for every exception-handled request.
      var template = RouteTemplate(ctx);
      var start = Stopwatch.GetTimestamp();
      try
      {
         await next(ctx);
      }
      finally
      {
         // Matched routes only: the path field is the low-cardinality route template
         // (spec/logging.md forbids raw URLs), and the Java side logs nothing for
         // unmatched requests either (spring.web.resources.add-mappings=false keeps
         // them unmatched there).
         if (template is not null && !Excluded.Contains(template))
         {
            var elapsed = Stopwatch.GetElapsedTime(start);
            LedgerLog.HttpAccess(
               logger,
               ctx.Request.Method,
               template,
               ctx.Response.StatusCode,
               elapsed.TotalMilliseconds,
               ctx.RequestId());
         }
      }
   }

   private static string? RouteTemplate(HttpContext ctx)
   {
      var endpoint = ctx.GetEndpoint();
      if (endpoint is not RouteEndpoint re)
         return null;
      // MapGroup("/invoices") + MapPost("") yields RawText "/invoices/"; the frozen
      // template set (spec/logging.md) has no trailing slashes, so normalize (non-root only).
      var template = "/" + re.RoutePattern.RawText?.TrimStart('/');
      return template.Length > 1 ? template.TrimEnd('/') : template;
   }
}
