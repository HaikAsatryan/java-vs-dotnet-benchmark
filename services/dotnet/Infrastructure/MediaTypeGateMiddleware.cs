using Ledgerline.Logging;
using Ledgerline.Problems;

namespace Ledgerline.Infrastructure;

/// The FROZEN content-type gate (spec/openapi.yaml x-error-strings.unsupported_media_type),
/// the same trivial grammar as the Java side's JsonMediaTypeGate so no parser-library
/// disagreement can split the two services: Content-Type up to the first ';', space/tab-trimmed, lowercased,
/// must be "application/json" or a "+json" suffix containing "/". Parameters are never
/// validated. Applies only to matched endpoints that accept a request body (the two POSTs
/// carry IAcceptsMetadata) and only when the request HAS a body; a body-less POST falls
/// through to binding and its frozen 400. Rejections are the frozen 415 with one
/// request_error, logged HERE because minimal-API binding otherwise 415s silently (no
/// exception, no log) for a present-but-non-JSON Content-Type.
public sealed class MediaTypeGateMiddleware(RequestDelegate next, ILogger<MediaTypeGateMiddleware> logger)
{
   /// Marker for endpoints the gate protects. The endpoints CANNOT keep the framework's
   /// inferred IAcceptsMetadata for this: routing's AcceptsMatcherPolicy would then 415
   /// during endpoint selection, before any middleware sees a matched endpoint, emitting
   /// the envelope with NO request_error and NO access line. The two body POSTs strip the
   /// inferred metadata (JsonBodyGate.Strip) and carry this marker instead, so the gate
   /// (and the access log) always see the real endpoint.
   public sealed class JsonBodyGate
   {
      public static readonly JsonBodyGate Instance = new();

      public static void Strip(EndpointBuilder b)
      {
         for (var i = b.Metadata.Count - 1; i >= 0; i--)
            if (b.Metadata[i] is Microsoft.AspNetCore.Http.Metadata.IAcceptsMetadata)
               b.Metadata.RemoveAt(i);
         b.Metadata.Add(Instance);
      }
   }

   public async Task InvokeAsync(HttpContext ctx)
   {
      // "Has a body" is the same rule as the Java gate's hasBody: a declared non-zero
      // length, or no declared length with chunked transfer-encoding. A body-less
      // POST (Content-Length: 0 or no length at all) falls through to binding.
      var cl = ctx.Request.ContentLength;
      var hasBody = cl > 0 || (cl is null && ctx.Request.Headers.ContainsKey("Transfer-Encoding"));
      var gated = ctx.GetEndpoint()?.Metadata.GetMetadata<JsonBodyGate>();
      if (gated is not null && hasBody
          && !IsJsonMediaType(ctx.Request.ContentType))
      {
         LedgerLog.RequestError(logger, StatusCodes.Status415UnsupportedMediaType, "validation", ctx.RequestId());
         await ProblemWriter.WriteUnsupportedMediaType(ctx);
         return;
      }
      await next(ctx);
   }

   public static bool IsJsonMediaType(string? contentType)
   {
      var mt = contentType ?? "";
      var semi = mt.IndexOf(';');
      if (semi >= 0)
         mt = mt[..semi];
      mt = mt.Trim(' ', '\t').ToLowerInvariant();
      return mt == "application/json" ||
             (mt.EndsWith("+json", StringComparison.Ordinal) && mt.Contains('/'));
   }
}
