package io.ledgerline.problem;

/**
 * The twelve frozen x-validation-messages of spec/openapi.yaml, committed as vectors under
 * {@code bench/validation/golden/errors/}. Named constants rather than the inline literals
 * {@code services/dotnet/Dtos/} writes, so {@code GoldenErrorContractTest} can compare the golden
 * file to this class key by key.
 *
 * <p>The two regex messages are VERBATIM where {@code CreateInvoiceRequest.cs} doubles every
 * brace: DataAnnotations runs {@code string.Format} over ErrorMessage, Hibernate Validator does
 * not touch a quantifier naming no declared constraint attribute.
 */
public final class ValidationStrings {

    public static final String REQUIRED = "is required";
    public static final String SKU_PATTERN = "must match ^[A-Z]{2}-[0-9]{4}$";
    public static final String CURRENCY_PATTERN = "must match ^[A-Z]{3}$";
    public static final String STATUS_ENUM = "must be one of: draft, open";
    public static final String QTY_RANGE = "must be between 1 and 1000";
    public static final String UNIT_PRICE_RANGE = "must be between 0 and 100000000";
    public static final String ITEMS_CREATE_COUNT = "must contain between 3 and 8 items";
    public static final String ITEMS_QUOTE_COUNT = "must contain exactly 50 items";
    public static final String DESCRIPTION_LENGTH = "must be between 1 and 200 characters";
    public static final String PAGE_MIN = "must be >= 1";
    public static final String PAGE_MAX = "must be <= 100000";
    public static final String ID_POSITIVE = "must be a positive integer";

    private ValidationStrings() {
    }
}
