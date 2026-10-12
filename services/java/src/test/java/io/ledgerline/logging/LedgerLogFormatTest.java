package io.ledgerline.logging;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import java.util.Map;
import java.util.function.Consumer;
import java.util.regex.Pattern;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.Logger;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Every line spec/logging.md allows, formatted and read back as JSON. The .NET side asserts the
 * same set in {@code LogFormatterTests.cs}; the two must agree on field NAMES, VALUES and JSON
 * types, and the equivalence harness compares them by parsing, not by regex, so a stringified
 * number would pass a shape check and fail a byte comparison.
 *
 * <p>Its own {@link LoggerContext}, unattached to the SLF4J static factory: the application's
 * real appender writes to stdout, and a test that captured it would have to reach into the
 * running context and put it back.
 */
class LedgerLogFormatTest {

    private static final Pattern RFC3339_MICROS_Z =
            Pattern.compile("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{6}Z$");

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    private final LedgerStructuredLogFormatter formatter = new LedgerStructuredLogFormatter();
    private final ListAppender<ILoggingEvent> sink = new ListAppender<>();
    private LoggerContext context;
    private Logger logger;

    @BeforeEach
    void setUp() {
        context = new LoggerContext();
        sink.setContext(context);
        sink.start();
        ch.qos.logback.classic.Logger classic = context.getLogger("io.ledgerline.test");
        classic.setLevel(Level.INFO);
        classic.addAppender(sink);
        logger = classic;
    }

    @Test
    void theAccessLineCarriesTheFourFrozenFieldsWithTheirJsonTypes() {
        JsonNode line = emit(log -> LedgerLog.httpAccess(log, "GET", "/invoices/{id}", 200, 1.5,
                "11111111-2222-3333-4444-555555555555"));

        assertThat(fields(line)).containsExactlyInAnyOrderEntriesOf(Map.of(
                "level", "info", "msg", "http_access",
                "request_id", "11111111-2222-3333-4444-555555555555",
                "method", "GET", "path", "/invoices/{id}",
                "status", 200L, "duration_ms", 1.5));
        assertThat(line.get("status").isIntegralNumber()).isTrue();
        assertThat(line.get("status").intValue()).isEqualTo(200);
        // duration_ms is "number" in the contract, not int: a whole-millisecond request must
        // still not collapse the field to an integer type on one side only.
        assertThat(line.get("duration_ms").isNumber()).isTrue();
        assertThat(line.get("duration_ms").doubleValue()).isEqualTo(1.5);
    }

    @Test
    void everyTimestampIsRfc3339WithExactlySixFractionalDigits() {
        JsonNode line = emit(log -> LedgerLog.shutdownComplete(log));
        assertThat(line.get("ts").stringValue()).matches(RFC3339_MICROS_Z);
    }

    @Test
    void theDomainEventsCarryTheirFrozenFieldNames() {
        assertThat(fields(emit(log -> LedgerLog.invoiceCreated(log, 7, 9, 3, 1200, "r"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("level", "info",
                        "msg", "invoice_created", "request_id", "r",
                        "customer_id", 7L, "invoice_id", 9L, "item_count", 3L, "total_minor", 1200L));
        assertThat(fields(emit(log -> LedgerLog.cacheInvalidated(log, 9, "r"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("level", "info",
                        "msg", "cache_invalidated", "request_id", "r", "invoice_id", 9L));
        assertThat(fields(emit(log -> LedgerLog.pdfStubWritten(log, 9, 32768, "r"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("level", "info",
                        "msg", "pdf_stub_written", "request_id", "r",
                        "invoice_id", 9L, "bytes_written", 32768L));
        assertThat(fields(emit(log -> LedgerLog.quoteComputed(log, 50, 99500, "r"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("level", "info",
                        "msg", "quote_computed", "request_id", "r",
                        "item_count", 50L, "total_minor", 99500L));
    }

    @Test
    void theTwoErrorEventsAreLevelErrorAndCarryTheirOwnFields() {
        assertThat(fields(emit(log -> LedgerLog.requestError(log, 415, "validation", "r"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("level", "error",
                        "msg", "request_error", "request_id", "r",
                        "status", 415L, "error_class", "validation"));
        assertThat(fields(emit(log -> LedgerLog.exception(log, "boom", "r"))))
                .containsExactlyInAnyOrderEntriesOf(Map.of("level", "error",
                        "msg", "exception", "request_id", "r", "detail", "boom"));
    }

    @Test
    void lifecycleLinesCarryAnEmptyRequestIdRatherThanOmittingTheKey() {
        JsonNode shutdown = emit(log -> LedgerLog.shutdownComplete(log));
        assertThat(shutdown.get("msg").stringValue()).isEqualTo("shutdown_complete");
        assertThat(shutdown.get("request_id").stringValue()).isEmpty();

        JsonNode config = emit(log -> LedgerLog.config(log, Map.of("db_pool_max", 24)));
        assertThat(config.get("msg").stringValue()).isEqualTo("config");
        assertThat(config.get("request_id").stringValue()).isEmpty();
        assertThat(config.get("db_pool_max").intValue()).isEqualTo(24);
    }

    @Test
    void aFrameworkLineThatReachesStdoutIsStillAContractShapedObject() {
        // Everything below WARN on the framework loggers is filtered off in application.yaml,
        // but a WARN that does get through must not arrive as a differently shaped record.
        JsonNode line = emit(log -> log.warn("something the framework said"));

        assertThat(line.get("level").stringValue()).isEqualTo("warn");
        assertThat(line.get("msg").stringValue()).isEqualTo("something the framework said");
        assertThat(line.get("request_id").stringValue()).isEmpty();
    }

    @Test
    void everyLineEndsWithExactlyOneNewline() {
        // The json-file driver splits on newline; a missing or doubled terminator merges or
        // splits records downstream of anything this suite can see.
        LedgerLog.shutdownComplete(logger);
        String rendered = formatter.format(sink.list.getFirst());

        assertThat(rendered).endsWith("}\n");
        assertThat(rendered.chars().filter(c -> c == '\n').count()).isEqualTo(1);
    }

    private JsonNode emit(Consumer<Logger> event) {
        sink.list.clear();
        event.accept(logger);
        assertThat(sink.list).hasSize(1);
        return MAPPER.readTree(formatter.format(sink.list.getFirst()));
    }

    /** Everything but {@code ts}, whose value is a clock reading. */
    private static Map<String, Object> fields(JsonNode line) {
        return line.properties().stream()
                .filter(entry -> !entry.getKey().equals("ts"))
                .collect(java.util.stream.Collectors.toMap(Map.Entry::getKey,
                        entry -> scalar(entry.getValue())));
    }

    /** Integral fields all widen to Long: JSON has one integer type and so does the contract. */
    private static Object scalar(JsonNode value) {
        if (value.isIntegralNumber()) {
            return value.longValue();
        }
        return value.isNumber() ? value.doubleValue() : value.stringValue();
    }
}
