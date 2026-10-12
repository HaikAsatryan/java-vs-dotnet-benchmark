package io.ledgerline.infrastructure;

import static org.assertj.core.api.Assertions.assertThatThrownBy;

import io.ledgerline.config.LedgerProperties;
import io.ledgerline.problem.PayloadTooLargeException;
import jakarta.servlet.http.HttpServletRequest;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

/**
 * BODY_MAX_BYTES holds on the READER as well as the stream. No handler reads a Reader today, so
 * nothing else in the suite would notice the wrapper inheriting an unlimited one straight off the
 * container - which is how a limit that is declared unconditionally stops being one.
 */
class BodySizeLimitFilterTest {

    private static final long LIMIT = 64;

    @Test
    void anOverDeclaredContentLengthTripsTheReader() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("POST", "/probe/invoices");
        request.setContent(new byte[(int) LIMIT + 1]);

        assertThatThrownBy(() -> wrap(request).getReader())
                .isInstanceOf(PayloadTooLargeException.class);
    }

    @Test
    void anUndeclaredBodyTripsTheReaderWhileItStreams() throws Exception {
        // Content-Length absent (chunked): nothing can be rejected up front, so the limit has to
        // hold while the characters are pulled through.
        MockHttpServletRequest request = new MockHttpServletRequest("POST", "/probe/invoices") {
            @Override
            public long getContentLengthLong() {
                return -1;
            }
        };
        request.setContent(new byte[(int) LIMIT + 1]);

        assertThatThrownBy(() -> wrap(request).getReader().readLine())
                .isInstanceOf(PayloadTooLargeException.class);
    }

    private static HttpServletRequest wrap(MockHttpServletRequest request) throws Exception {
        AtomicReference<HttpServletRequest> wrapped = new AtomicReference<>();
        new BodySizeLimitFilter(properties()).doFilter(request, new MockHttpServletResponse(),
                (req, res) -> wrapped.set((HttpServletRequest) req));
        return wrapped.get();
    }

    private static LedgerProperties properties() {
        return new LedgerProperties(
                new LedgerProperties.Pg("localhost", 5432, "ledgerline", "ledger", "ledger"),
                new LedgerProperties.Db(24, 24, 3600, 5, false),
                16, 300, 8192, LIMIT, "/data/pdf");
    }
}
