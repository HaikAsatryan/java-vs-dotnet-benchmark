package io.ledgerline.problem;

import java.io.IOException;

/**
 * Thrown while the request body is being read past BODY_MAX_BYTES. An IOException because that is
 * the only checked type {@code ServletInputStream.read} may raise.
 */
public class PayloadTooLargeException extends IOException {

    /** The handler recognises the 413 from this, not from a cause chain the converter rewraps. */
    public static final String REQUEST_ATTRIBUTE =
            PayloadTooLargeException.class.getName() + ".tripped";

    public PayloadTooLargeException(long limit) {
        super("request body exceeds BODY_MAX_BYTES=" + limit);
    }
}
