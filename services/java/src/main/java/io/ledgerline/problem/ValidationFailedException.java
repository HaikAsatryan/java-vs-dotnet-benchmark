package io.ledgerline.problem;

import java.util.List;
import java.util.Map;

/**
 * A 400 raised by hand rather than by Jakarta validation: the path and query parameters, whose
 * frozen keys are lowercase single tokens ({@code id}, {@code page}) because .NET validates them
 * in endpoint code with no CLR member behind them (spec/openapi.yaml Problem.errors "KEY FORMAT").
 *
 * <p>Not {@code @Valid} on {@code @PathVariable}/{@code @RequestParam}: that raises
 * HandlerMethodValidationException, which is keyed on the Java parameter name and cannot carry
 * BOTH {@code id} and {@code page} in one map the way NOTES-equivalence.md pins for
 * GET /customers/{id}/invoices. Stack-trace capture is off for {@link NotFoundException}'s reason.
 */
public class ValidationFailedException extends RuntimeException {

    private final Map<String, List<String>> errors;

    public ValidationFailedException(Map<String, List<String>> errors) {
        super(null, null, false, false);
        this.errors = errors;
    }

    public Map<String, List<String>> errors() {
        return errors;
    }
}
