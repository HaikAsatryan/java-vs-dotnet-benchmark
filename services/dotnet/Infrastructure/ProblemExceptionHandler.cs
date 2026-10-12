using System.Text.Json;
using Ledgerline.Logging;
using Ledgerline.Problems;
using Microsoft.AspNetCore.Diagnostics;

namespace Ledgerline.Infrastructure;

/// IExceptionHandler aligning exception emission with the frozen contract (spec/openapi.yaml
/// x-error-strings): a malformed/undeserializable/oversized request body becomes the frozen 400
/// validation problem with {"body": ["is required"]} (the undeserializable-body path,
/// spec/logging.md; identical on the Java side); any other
/// unhandled exception becomes the frozen 500 internal problem, with the request_error +
/// exception log events per spec/logging.md. Always returns true, so ExceptionHandlerMiddleware
/// suppresses its own UnhandledException diagnostics (default for DI-registered IExceptionHandler)
/// and the frozen log contract stays intact. Response-already-started exceptions are rethrown by
/// the middleware itself.
public sealed class ProblemExceptionHandler(ILogger<ProblemExceptionHandler> logger) : IExceptionHandler
{
   public async ValueTask<bool> TryHandleAsync(HttpContext ctx, Exception exception, CancellationToken cancellationToken)
   {
      // ExceptionHandlerMiddleware CLEARED the response before calling us, dropping the
      // X-Request-Id header RequestIdMiddleware set: restore it. (Its injected no-cache
      // headers are stripped by RequestIdMiddleware's LIFO OnStarting callback.)
      ctx.Response.Headers[RequestContext.RequestIdHeader] = ctx.RequestId();

      // Non-JSON Content-Type: MediaTypeGateMiddleware handles the common cases before
      // binding; this branch covers any 415 that binding still throws with
      // ThrowOnBadRequest pinned on. Same frozen envelope + request_error either way.
      if (exception is BadHttpRequestException { StatusCode: StatusCodes.Status415UnsupportedMediaType })
      {
         LedgerLog.RequestError(logger, StatusCodes.Status415UnsupportedMediaType, "validation", ctx.RequestId());
         await ProblemWriter.WriteUnsupportedMediaType(ctx);
      }
      // Oversized body: frozen 413, NO request_error (spec/logging.md). Kestrel
      // usually short-circuits this without throwing (the StatusCodePages path
      // writes the envelope); this branch covers any path that does throw.
      else if (exception is BadHttpRequestException { StatusCode: StatusCodes.Status413PayloadTooLarge })
      {
         await ProblemWriter.WritePayloadTooLarge(ctx);
      }
      else if (exception is BadHttpRequestException or JsonException)
      {
         LedgerLog.RequestError(logger, StatusCodes.Status400BadRequest, "validation", ctx.RequestId());
         await ProblemWriter.WriteValidation(ctx, new Dictionary<string, string[]> { ["body"] = ["is required"] });
      }
      else
      {
         // Frozen order (spec/logging.md: internal errors additionally log
         // msg="exception"): request_error first, then exception, so the two
         // services' 500-path log streams are line-for-line identical, not just
         // set-equal.
         LedgerLog.RequestError(logger, StatusCodes.Status500InternalServerError, "internal", ctx.RequestId());
         LedgerLog.Exception(logger, exception.Message, ctx.RequestId());
         await ProblemWriter.WriteInternal(ctx);
      }
      return true;
   }
}
