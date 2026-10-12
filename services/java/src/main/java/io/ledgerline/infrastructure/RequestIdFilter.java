package io.ledgerline.infrastructure;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.UUID;
import org.springframework.core.annotation.Order;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * Inbound {@code X-Request-Id} else a fresh UUIDv4, echoed in the response header and never in a
 * body (spec/openapi.yaml). Outermost of the three so the id exists before the access line and
 * before anything can reject the request.
 */
@Order(RequestIdFilter.ORDER)
public class RequestIdFilter extends OncePerRequestFilter {

    public static final int ORDER = 10;

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
            FilterChain chain) throws ServletException, IOException {
        // getHeader takes the FIRST value of a repeated header, matching the
        // explicit headers[0] in RequestIdMiddleware.cs; a comma-joined pair is not an id.
        String inbound = request.getHeader(RequestContext.REQUEST_ID_HEADER);
        String requestId = inbound == null || inbound.isEmpty()
                ? UUID.randomUUID().toString()
                : inbound;

        request.setAttribute(RequestContext.REQUEST_ID_ATTRIBUTE, requestId);
        response.setHeader(RequestContext.REQUEST_ID_HEADER, requestId);
        chain.doFilter(request, response);
    }
}
