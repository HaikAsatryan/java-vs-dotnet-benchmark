namespace Ledgerline.Problems;

/// Frozen x-error-strings from spec/openapi.yaml. Both services emit these EXACT values.
public static class ProblemStrings
{
   public const string ValidationType = "https://ledgerline.test/problems/validation";
   public const string ValidationTitle = "Validation failed";
   public const string ValidationDetail = "The request payload failed validation.";

   public const string NotFoundType = "https://ledgerline.test/problems/not-found";
   public const string NotFoundTitle = "Not found";
   public const string NotFoundDetail = "The requested resource does not exist.";

   public const string InternalType = "https://ledgerline.test/problems/internal";
   public const string InternalTitle = "Internal server error";
   public const string InternalDetail = "An unexpected error occurred.";

   public const string UnsupportedType = "https://ledgerline.test/problems/unsupported-media-type";
   public const string UnsupportedTitle = "Unsupported media type";
   public const string UnsupportedDetail = "The request content type must be application/json.";

   public const string TooLargeType = "https://ledgerline.test/problems/payload-too-large";
   public const string TooLargeTitle = "Payload too large";
   public const string TooLargeDetail = "The request body exceeds the configured limit.";
}
