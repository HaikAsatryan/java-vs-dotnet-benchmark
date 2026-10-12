package io.ledgerline.problem;

/**
 * The frozen 404 (spec/openapi.yaml x-error-strings.not_found). {@code writableStackTrace = false}
 * is the fourth constructor argument and the only line here with a cost: 404s are control flow on
 * a path the ladder walls hit hard, and .NET returns {@code Results.Problem(404, ...)} without
 * throwing at all.
 */
public class NotFoundException extends RuntimeException {

    public NotFoundException() {
        super(null, null, false, false);
    }
}
