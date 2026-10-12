package io.ledgerline.problem;

import com.fasterxml.jackson.annotation.JsonInclude;
import java.util.List;
import java.util.Map;
import org.jspecify.annotations.Nullable;

/**
 * The RFC 7807 body. Component order matches {@code ProblemWriter.cs}'s write order, so the wires
 * are byte-identical rather than merely equal after {@code normalize.py} sorts keys.
 *
 * <p>Deliberately not a {@link org.springframework.http.ProblemDetail}: MVC back-fills
 * {@code instance} from the request URI on both return paths guarded only by
 * {@code if (getInstance() == null)}, so nulling it in a handler does nothing, and the
 * equivalence suite deep-equals the whole body against a .NET one with no instance key.
 *
 * <p>{@code @JsonInclude(NON_NULL)} is on this record ONLY - globally it would delete the
 * present-but-null fields spec/runtime-stats.md requires.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record Problem(
        String type,
        String title,
        int status,
        String detail,
        @Nullable Map<String, List<String>> errors) {

    public static Problem validation(@Nullable Map<String, List<String>> errors) {
        return new Problem(ProblemStrings.VALIDATION_TYPE, ProblemStrings.VALIDATION_TITLE,
                400, ProblemStrings.VALIDATION_DETAIL, errors);
    }

    public static Problem notFound() {
        return new Problem(ProblemStrings.NOT_FOUND_TYPE, ProblemStrings.NOT_FOUND_TITLE,
                404, ProblemStrings.NOT_FOUND_DETAIL, null);
    }

    public static Problem payloadTooLarge() {
        return new Problem(ProblemStrings.TOO_LARGE_TYPE, ProblemStrings.TOO_LARGE_TITLE,
                413, ProblemStrings.TOO_LARGE_DETAIL, null);
    }

    public static Problem unsupportedMediaType() {
        return new Problem(ProblemStrings.UNSUPPORTED_TYPE, ProblemStrings.UNSUPPORTED_TITLE,
                415, ProblemStrings.UNSUPPORTED_DETAIL, null);
    }

    public static Problem internal() {
        return new Problem(ProblemStrings.INTERNAL_TYPE, ProblemStrings.INTERNAL_TITLE,
                500, ProblemStrings.INTERNAL_DETAIL, null);
    }

    /**
     * {@code ProblemDetailsConfig.cs}'s status switch including its default arm, which is what
     * lands every framework-produced error body on the frozen five strings.
     */
    public static Problem forStatus(int status) {
        return switch (status) {
            case 400 -> validation(null);
            case 404 -> notFound();
            case 413 -> payloadTooLarge();
            case 415 -> unsupportedMediaType();
            default -> new Problem(ProblemStrings.INTERNAL_TYPE, ProblemStrings.INTERNAL_TITLE,
                    status, ProblemStrings.INTERNAL_DETAIL, null);
        };
    }
}
