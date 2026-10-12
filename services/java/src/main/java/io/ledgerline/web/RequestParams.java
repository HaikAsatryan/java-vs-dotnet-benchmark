package io.ledgerline.web;

import io.ledgerline.problem.ValidationFailedException;
import io.ledgerline.problem.ValidationStrings;
import java.util.List;
import java.util.Map;
import org.jspecify.annotations.Nullable;

/**
 * The path and query parameters spec/openapi.yaml validates by hand, with the frozen lowercase
 * keys ({@code id}, {@code page}) that mark a parameter with no CLR member behind it.
 *
 * <p>{@code Long.parseLong} is the accepted grammar of .NET's
 * {@code long.TryParse(raw, NumberStyles.AllowLeadingSign, InvariantCulture)}: an optional sign,
 * digits, and nothing else - no surrounding whitespace, no separators, no underscores.
 */
final class RequestParams {

    static final int PAGE_MAX = 100_000;

    private RequestParams() {
    }

    static @Nullable Long parseId(String raw) {
        try {
            long value = Long.parseLong(raw);
            return value >= 1 ? value : null;
        } catch (NumberFormatException e) {
            return null;
        }
    }

    /** The single-parameter case: 400 immediately, since there is no second key to aggregate. */
    static long requireId(String raw) {
        Long value = parseId(raw);
        if (value == null) {
            throw new ValidationFailedException(
                    Map.of("id", List.of(ValidationStrings.ID_POSITIVE)));
        }
        return value;
    }

    /**
     * Absent or empty -> 1. Anything else that is not a page number records its frozen message in
     * {@code errors} and returns 1, so that an invalid id and an invalid page arrive in ONE map.
     */
    static int parsePage(@Nullable String raw, Map<String, List<String>> errors) {
        if (raw == null || raw.isEmpty()) {
            return 1;
        }
        Long value = parseId(raw);
        if (value == null) {
            errors.put("page", List.of(ValidationStrings.PAGE_MIN));
            return 1;
        }
        if (value > PAGE_MAX) {
            errors.put("page", List.of(ValidationStrings.PAGE_MAX));
            return 1;
        }
        return value.intValue();
    }
}
