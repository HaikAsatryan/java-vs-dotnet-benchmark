package io.ledgerline.infrastructure;

import jakarta.servlet.http.HttpServletRequest;

/** Where the request id lives for the duration of a request, mirroring {@code ctx.Items}. */
public final class RequestContext {

    public static final String REQUEST_ID_HEADER = "X-Request-Id";

    static final String REQUEST_ID_ATTRIBUTE = "ledger.request_id";

    private RequestContext() {
    }

    public static String requestId(HttpServletRequest request) {
        Object value = request.getAttribute(REQUEST_ID_ATTRIBUTE);
        return value instanceof String id ? id : "";
    }
}
