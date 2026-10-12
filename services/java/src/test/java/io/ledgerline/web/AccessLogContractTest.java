package io.ledgerline.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.slf4j.LoggerFactory;
import org.slf4j.event.KeyValuePair;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;

/**
 * spec/logging.md's two counting rules, which are the ones a reader of the artifacts depends on:
 * EXACTLY one access line per matched request, and a {@code request_error} on exactly two paths.
 * Both are cheap to get wrong in a way no functional test notices - an extra line inflates the
 * log volume one side is measured on, and a missing one hides a status from the artifacts.
 *
 * <p>The appender is attached to the ROOT logger so the capture sees what stdout would, and it
 * sits beside the real async appender rather than replacing it.
 */
@MockWebTest
class AccessLogContractTest {

    @Autowired
    private MockMvc mvc;

    private final ListAppender<ILoggingEvent> sink = new ListAppender<>();
    private Logger root;

    @BeforeEach
    void attach() {
        root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        sink.setContext(root.getLoggerContext());
        sink.start();
        root.addAppender(sink);
    }

    @AfterEach
    void detach() {
        root.detachAppender(sink);
        sink.stop();
    }

    @ParameterizedTest
    @CsvSource({
        "GET,  /invoices/0,             /invoices/{id},                400",
        "POST, /invoices/0/pdf-stub,    /invoices/{id}/pdf-stub,       400",
        "GET,  /customers/0/invoices,   /customers/{id}/invoices,      400",
    })
    void oneAccessLineCarriesTheRouteTemplateAndTheEmittedStatus(
            String method, String path, String template, int status) {
        perform(method.equals("GET") ? get(path) : post(path));

        assertThat(events("http_access")).singleElement().satisfies(fields -> {
            assertThat(fields).containsEntry("method", method).containsEntry("path", template)
                    .containsEntry("status", status);
            assertThat(fields.get("request_id")).asString().isNotBlank();
            assertThat((Double) fields.get("duration_ms")).isNotNegative();
        });
    }

    @Test
    void theQuoteEndpointLogsItsAccessLineAndItsDomainEvent() {
        perform(post("/pricing/quote").contentType(MediaType.APPLICATION_JSON)
                .content(io.ledgerline.GoldenVectors
                        .load("pricing_quote/requests/quote_a_wire_request.json").toString()));

        assertThat(events("http_access")).singleElement()
                .satisfies(fields -> assertThat(fields).containsEntry("path", "/pricing/quote")
                        .containsEntry("status", 200));
        assertThat(events("quote_computed")).singleElement()
                .satisfies(fields -> assertThat(fields).containsEntry("item_count", 50)
                        .containsEntry("total_minor", 99500L));
    }

    @Test
    void theThreeInstrumentationEndpointsAreNotAccessLogged() {
        perform(get("/healthz"));
        perform(get("/readyz"));
        perform(get("/runtime-stats"));

        assertThat(events("http_access")).isEmpty();
    }

    @Test
    void anUnmatchedPathLogsNothingAtAll() {
        // ASP.NET's AccessLogMiddleware reads a null endpoint and skips; here the route template
        // attribute is simply never set. Same outcome, so neither side's log volume includes
        // requests the other's would not.
        perform(get("/not-a-route"));

        assertThat(events("http_access")).isEmpty();
    }

    @Test
    void theContentTypeGateEmitsBothItsAccessLineAndItsRequestError() {
        perform(post("/pricing/quote").contentType(MediaType.TEXT_PLAIN).content("{}"));

        assertThat(events("http_access")).singleElement()
                .satisfies(fields -> assertThat(fields).containsEntry("status", 415));
        assertThat(events("request_error")).singleElement()
                .satisfies(fields -> assertThat(fields).containsEntry("status", 415)
                        .containsEntry("error_class", "validation"));
    }

    @Test
    void aMalformedBodyIsTheOtherRequestErrorPath() {
        perform(post("/pricing/quote").contentType(MediaType.APPLICATION_JSON).content("{"));

        assertThat(events("request_error")).singleElement()
                .satisfies(fields -> assertThat(fields).containsEntry("status", 400)
                        .containsEntry("error_class", "validation"));
    }

    @Test
    void fieldLevelValidationAndNotFoundLogNoRequestError() {
        // spec/logging.md records those through the access line's status alone; a request_error
        // here would double-count the ladder's 400s against the .NET stream.
        perform(get("/invoices/0"));
        perform(post("/pricing/quote").contentType(MediaType.APPLICATION_JSON)
                .content("{\"currency\":\"usd\",\"items\":[]}"));

        assertThat(events("request_error")).isEmpty();
        assertThat(events("http_access")).hasSize(2);
    }

    @Test
    void anOversizedBodyIsAnAccessLineWithNoRequestError() {
        // The 413 is the one rejection spec/logging.md gives an access line and NO event, so
        // that the layer difference (Kestrel below the app, a filter inside it) stays invisible.
        perform(post("/pricing/quote").contentType(MediaType.APPLICATION_JSON)
                .content("x".repeat(70_000)));

        assertThat(events("http_access")).singleElement()
                .satisfies(fields -> assertThat(fields).containsEntry("status", 413));
        assertThat(events("request_error")).isEmpty();
    }

    private void perform(MockHttpServletRequestBuilder request) {
        try {
            mvc.perform(request);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private List<Map<String, Object>> events(String msg) {
        return sink.list.stream()
                .filter(event -> event.getFormattedMessage().equals(msg))
                .map(AccessLogContractTest::fields)
                .toList();
    }

    private static Map<String, Object> fields(ILoggingEvent event) {
        List<KeyValuePair> pairs = event.getKeyValuePairs();
        return pairs == null ? Map.of()
                : pairs.stream().collect(java.util.stream.Collectors
                        .toMap(pair -> pair.key, pair -> pair.value));
    }
}
