package io.ledgerline.problem;

/**
 * The frozen x-error-strings of spec/openapi.yaml, 1:1 with
 * {@code services/dotnet/Problems/ProblemStrings.cs}. Both services emit these EXACT values;
 * {@code bench/validation/golden/errors/} carries three of the five as committed vectors and
 * {@code bench/validation/tests/test_equivalence.py} deep-equals the whole body across stacks.
 */
public final class ProblemStrings {

    public static final String VALIDATION_TYPE = "https://ledgerline.test/problems/validation";
    public static final String VALIDATION_TITLE = "Validation failed";
    public static final String VALIDATION_DETAIL = "The request payload failed validation.";

    public static final String NOT_FOUND_TYPE = "https://ledgerline.test/problems/not-found";
    public static final String NOT_FOUND_TITLE = "Not found";
    public static final String NOT_FOUND_DETAIL = "The requested resource does not exist.";

    public static final String INTERNAL_TYPE = "https://ledgerline.test/problems/internal";
    public static final String INTERNAL_TITLE = "Internal server error";
    public static final String INTERNAL_DETAIL = "An unexpected error occurred.";

    public static final String UNSUPPORTED_TYPE =
            "https://ledgerline.test/problems/unsupported-media-type";
    public static final String UNSUPPORTED_TITLE = "Unsupported media type";
    public static final String UNSUPPORTED_DETAIL =
            "The request content type must be application/json.";

    public static final String TOO_LARGE_TYPE =
            "https://ledgerline.test/problems/payload-too-large";
    public static final String TOO_LARGE_TITLE = "Payload too large";
    public static final String TOO_LARGE_DETAIL =
            "The request body exceeds the configured limit.";

    private ProblemStrings() {
    }
}
