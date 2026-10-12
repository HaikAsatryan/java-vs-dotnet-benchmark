namespace Ledgerline.Infrastructure;

/// Accept inbound X-Request-Id else generate a UUIDv4; stash for handlers/logging; echo in the
/// response header. NEVER in a response body; excluded from equivalence (spec/openapi.yaml).
public sealed class RequestIdMiddleware(RequestDelegate next)
{
   public const string Json = "application/json";
   private const string JsonWithCharset = "application/json; charset=utf-8";

   public async Task InvokeAsync(HttpContext ctx)
   {
      // First value only, as the Java filter reads it; StringValues.ToString() would comma-join repeats.
      var headers = ctx.Request.Headers[RequestContext.RequestIdHeader];
      var inbound = headers.Count > 0 ? headers[0] : null;
      var requestId = string.IsNullOrEmpty(inbound) ? Guid.NewGuid().ToString() : inbound;

      ctx.Items[RequestContext.RequestIdItemKey] = requestId;
      ctx.Response.Headers[RequestContext.RequestIdHeader] = requestId;

      // OnStarting callbacks run LIFO, so registering here (outermost, before anything
      // else) makes this run LAST, after ExceptionHandlerMiddleware's own callback has
      // injected Cache-Control/Pragma/Expires no-cache headers. Nothing in Ledgerline
      // sets cache headers deliberately, and the Java side never sends them; stripping
      // keeps the exception-path 4xx/5xx header shape identical to every other response.
      ctx.Response.OnStarting(static state =>
      {
         var headers = ((HttpResponse)state).Headers;
         headers.Remove("Cache-Control");
         headers.Remove("Pragma");
         headers.Remove("Expires");
         // spec/openapi.yaml names the media type as "application/json", with no parameter, and
         // this app already wrote it both ways: ProblemDetails emits a bare
         // application/problem+json while TypedResults appends "; charset=utf-8". Normalized
         // here rather than per endpoint because TypedResults.Created takes no contentType.
         if (headers.ContentType == JsonWithCharset)
            headers.ContentType = Json;
         return Task.CompletedTask;
      }, ctx.Response);

      await next(ctx);
   }
}
