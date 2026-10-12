package io.ledgerline.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import org.slf4j.event.KeyValuePair;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * What a dependency fault looks like on the wire and on stdout - the shape every DB and Redis
 * error takes during a run, where spec/db-access.md counts them against the 0.1% error budget
 * that gates the headline.
 *
 * <p>Redis is pointed at a closed port rather than at "whatever is not running on this host":
 * the suite has to behave the same on a developer box with infrastructure up and on CI with
 * none, and a test that passes only because nothing is listening is a test that silently stops
 * asserting the day someone starts a container.
 */
@MockWebTest
@TestPropertySource(properties = "spring.data.redis.url=redis://127.0.0.1:1")
class InternalErrorContractTest {

    @Autowired
    private MockMvc mvc;

    @Autowired
    private JsonMapper json;

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

    @Test
    void anUnreachableDependencyIsTheFrozen500Envelope() {
        MockHttpServletResponse response = perform();

        assertThat(response.getStatus()).isEqualTo(500);
        assertThat(response.getContentType()).startsWith("application/problem+json");
        JsonNode body = parse(response);
        assertThat(body.get("type").stringValue())
                .isEqualTo("https://ledgerline.test/problems/internal");
        assertThat(body.get("title").stringValue()).isEqualTo("Internal server error");
        assertThat(body.get("detail").stringValue()).isEqualTo("An unexpected error occurred.");
        // Nothing from the exception reaches the client; the message goes to the log line only.
        assertThat(body.propertyNames()).containsExactlyInAnyOrder("type", "title", "status",
                "detail");
    }

    @Test
    void theTwoErrorLinesAreEmittedInTheFrozenOrder() {
        // spec/logging.md pins request_error BEFORE exception so the two stacks' 500 streams are
        // line-for-line identical rather than merely set-equal.
        perform();

        assertThat(sink.list.stream().map(ILoggingEvent::getFormattedMessage)
                .filter(msg -> msg.equals("request_error") || msg.equals("exception")).toList())
                .containsExactly("request_error", "exception");
        assertThat(fields("request_error")).containsEntry("status", 500)
                .containsEntry("error_class", "internal");
        assertThat(fields("exception")).containsKey("detail");
        assertThat(fields("http_access")).containsEntry("status", 500);
    }

    @Test
    void theRequestIdSurvivesTheExceptionPathIntoTheHeaderAndEveryLine() {
        MockHttpServletResponse response = perform("given-id");

        assertThat(response.getHeader("X-Request-Id")).isEqualTo("given-id");
        for (String event : List.of("request_error", "exception", "http_access")) {
            assertThat(fields(event)).containsEntry("request_id", "given-id");
        }
    }

    private java.util.Map<String, Object> fields(String msg) {
        return sink.list.stream()
                .filter(event -> event.getFormattedMessage().equals(msg))
                .findFirst()
                .map(event -> event.getKeyValuePairs().stream()
                        .collect(java.util.stream.Collectors.<KeyValuePair, String, Object>toMap(
                                pair -> pair.key, pair -> pair.value)))
                .orElseThrow(() -> new AssertionError("no " + msg + " line was emitted"));
    }

    private MockHttpServletResponse perform() {
        return perform(null);
    }

    private MockHttpServletResponse perform(String requestId) {
        try {
            var request = get("/invoices/1");
            if (requestId != null) {
                request = request.header("X-Request-Id", requestId);
            }
            return mvc.perform(request).andReturn().getResponse();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private JsonNode parse(MockHttpServletResponse response) {
        try {
            return json.readTree(response.getContentAsString(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
