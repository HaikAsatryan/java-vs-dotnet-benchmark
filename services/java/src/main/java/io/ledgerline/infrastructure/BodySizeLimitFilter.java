package io.ledgerline.infrastructure;

import io.ledgerline.config.LedgerProperties;
import io.ledgerline.problem.PayloadTooLargeException;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ReadListener;
import jakarta.servlet.ServletInputStream;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import org.springframework.core.annotation.Order;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * BODY_MAX_BYTES, enforced in the application because Tomcat's {@code maxPostSize} covers
 * form-encoded bodies only. On the .NET side the same limit is Kestrel's
 * {@code MaxRequestBodySize}, BELOW the application - a declared layer asymmetry, not a
 * behavioural one, and why spec/logging.md pins the 413 with no request_error on both sides.
 *
 * <p>The stream throws rather than the filter writing a response, so every error body is still
 * written in one place; the request attribute is how the handler knows why the read failed.
 */
@Order(BodySizeLimitFilter.ORDER)
public class BodySizeLimitFilter extends OncePerRequestFilter {

    public static final int ORDER = AccessLogFilter.ORDER + 10;

    private final long maxBytes;

    public BodySizeLimitFilter(LedgerProperties properties) {
        this.maxBytes = properties.bodyMaxBytes();
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
            FilterChain chain) throws ServletException, IOException {
        chain.doFilter(new LimitedBodyRequest(request, maxBytes), response);
    }

    private static final class LimitedBodyRequest extends HttpServletRequestWrapper {

        private final long limit;

        LimitedBodyRequest(HttpServletRequest request, long limit) {
            super(request);
            this.limit = limit;
        }

        /**
         * The declared length is checked here, not in the filter, so an over-declared
         * Content-Length and an over-delivered chunked body raise the same exception on the same
         * path. Kestrel decides the declared case earlier; the OUTCOME is what has to match.
         */
        @Override
        public ServletInputStream getInputStream() throws IOException {
            if (getContentLengthLong() > limit) {
                throw tripped(this, limit);
            }
            return new LimitedServletInputStream(this, super.getInputStream(), limit);
        }

        /**
         * Unreachable today (the JSON converter reads the stream), but the inherited
         * implementation returns an UNLIMITED reader straight off the wrapped request, so the
         * limit would be one converter choice away from absent.
         */
        @Override
        public BufferedReader getReader() throws IOException {
            String encoding = getCharacterEncoding();
            return new BufferedReader(new InputStreamReader(getInputStream(),
                    encoding == null ? StandardCharsets.UTF_8.name() : encoding));
        }
    }

    private static PayloadTooLargeException tripped(HttpServletRequest request, long limit) {
        request.setAttribute(PayloadTooLargeException.REQUEST_ATTRIBUTE, Boolean.TRUE);
        return new PayloadTooLargeException(limit);
    }

    private static final class LimitedServletInputStream extends ServletInputStream {

        private final HttpServletRequest request;
        private final ServletInputStream delegate;
        private final long limit;
        private long read;

        LimitedServletInputStream(HttpServletRequest request, ServletInputStream delegate,
                long limit) {
            this.request = request;
            this.delegate = delegate;
            this.limit = limit;
        }

        @Override
        public int read() throws IOException {
            int value = delegate.read();
            if (value >= 0) {
                advance(1);
            }
            return value;
        }

        @Override
        public int read(byte[] buffer, int offset, int length) throws IOException {
            int count = delegate.read(buffer, offset, length);
            if (count > 0) {
                advance(count);
            }
            return count;
        }

        private void advance(int count) throws PayloadTooLargeException {
            read += count;
            if (read > limit) {
                throw tripped(request, limit);
            }
        }

        @Override
        public boolean isFinished() {
            return delegate.isFinished();
        }

        @Override
        public boolean isReady() {
            return delegate.isReady();
        }

        @Override
        public void setReadListener(ReadListener listener) {
            delegate.setReadListener(listener);
        }
    }
}
