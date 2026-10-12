package io.ledgerline.infrastructure;

import io.ledgerline.logging.LedgerLog;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.Set;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.annotation.Order;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.servlet.HandlerMapping;

/**
 * Exactly one {@code http_access} line per matched request, with the ROUTE TEMPLATE as
 * {@code path} (spec/logging.md forbids the raw URL: the two services must have identical path
 * cardinality). {@code duration_ms} is a monotonic measurement around the whole downstream
 * pipeline, the same boundary {@code AccessLogMiddleware.cs} takes.
 */
@Order(AccessLogFilter.ORDER)
public class AccessLogFilter extends OncePerRequestFilter {

    public static final int ORDER = RequestIdFilter.ORDER + 10;

    private static final Set<String> EXCLUDED = Set.of("/healthz", "/readyz", "/runtime-stats");

    private static final Logger LOGGER = LoggerFactory.getLogger(AccessLogFilter.class);

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
            FilterChain chain) throws ServletException, IOException {
        long start = System.nanoTime();
        try {
            chain.doFilter(request, response);
        } finally {
            // Read AFTER dispatch: unlike ASP.NET, Spring matches the handler inside the servlet,
            // so the template does not exist on the way in. An unmatched request leaves it unset
            // and logs nothing, which is what the .NET side does for a null endpoint.
            String template = routeTemplate(request);
            if (template != null && !EXCLUDED.contains(template)) {
                double durationMs = (System.nanoTime() - start) / 1_000_000.0;
                LedgerLog.httpAccess(LOGGER, request.getMethod(), template, response.getStatus(),
                        durationMs, RequestContext.requestId(request));
            }
        }
    }

    private static @Nullable String routeTemplate(HttpServletRequest request) {
        Object pattern = request.getAttribute(HandlerMapping.BEST_MATCHING_PATTERN_ATTRIBUTE);
        return pattern == null ? null : pattern.toString();
    }
}
