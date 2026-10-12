using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;

namespace Ledgerline.Problems;

/// Rewrites every emitted ProblemDetails to the frozen x-error-strings (type/title/detail per
/// status) and strips framework extensions (traceId etc.) so the envelope is byte-stable and
/// cross-language equivalent. The validation `errors` map (added by AddValidation) is preserved.
public static class ProblemDetailsConfig
{
   public static void Configure(Microsoft.AspNetCore.Http.ProblemDetailsOptions options)
   {
      options.CustomizeProblemDetails = ctx =>
      {
         var pd = ctx.ProblemDetails;
         var status = pd.Status ?? ctx.HttpContext.Response.StatusCode;
         pd.Status = status;

         (pd.Type, pd.Title, pd.Detail) = status switch
         {
            400 => (ProblemStrings.ValidationType, ProblemStrings.ValidationTitle, ProblemStrings.ValidationDetail),
            404 => (ProblemStrings.NotFoundType, ProblemStrings.NotFoundTitle, ProblemStrings.NotFoundDetail),
            // Kestrel enforces MaxRequestBodySize below the app (binding short-circuits
            // 413 without throwing), so the oversized-body envelope is written by the
            // StatusCodePages path through this mapping. spec/logging.md: no request_error.
            413 => (ProblemStrings.TooLargeType, ProblemStrings.TooLargeTitle, ProblemStrings.TooLargeDetail),
            415 => (ProblemStrings.UnsupportedType, ProblemStrings.UnsupportedTitle, ProblemStrings.UnsupportedDetail),
            _ => (ProblemStrings.InternalType, ProblemStrings.InternalTitle, ProblemStrings.InternalDetail),
         };

         pd.Instance = null;
         // Remove framework-added extensions (traceId, requestId) so the body is equivalence-clean.
         pd.Extensions.Remove("traceId");
         pd.Extensions.Remove("requestId");
      };
   }
}
