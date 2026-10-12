package io.ledgerline.logging;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import io.ledgerline.json.Rfc3339Micros;
import java.util.List;
import java.util.function.BiConsumer;
import org.slf4j.event.KeyValuePair;
import org.springframework.boot.json.JsonWriter;
import org.springframework.boot.logging.structured.StructuredLogFormatter;

/**
 * spec/logging.md's flat JSON line - {@code ts}, {@code level}, {@code msg}, {@code request_id},
 * then the event's own fields - the counterpart of
 * {@code services/dotnet/Logging/LedgerConsoleFormatter.cs}. Registered by class name on the
 * encoder in {@code logback-spring.xml}; Boot's own ECS/GELF/Logstash formatters implement this
 * same interface, and every one of them nests or renames fields the contract freezes.
 *
 * <p>{@code request_id} is written from the event rather than left to the pair loop so that a
 * lifecycle line, and any framework WARN that reaches stdout, still carries the key with an empty
 * value - the .NET formatter's {@code wroteRequestId} fallback.
 */
public class LedgerStructuredLogFormatter implements StructuredLogFormatter<ILoggingEvent> {

    private static final String REQUEST_ID = "request_id";

    private static final JsonWriter<ILoggingEvent> WRITER =
            JsonWriter.<ILoggingEvent>of(members -> {
                members.add("ts", event -> Rfc3339Micros.format(event.getInstant()));
                members.add("level", event -> level(event.getLevel()));
                members.add("msg", event -> event.getFormattedMessage());
                members.add(REQUEST_ID, LedgerStructuredLogFormatter::requestId);
                members.add().usingPairs(LedgerStructuredLogFormatter::writeFields);
            }).withNewLineAtEnd();

    @Override
    public String format(ILoggingEvent event) {
        return WRITER.writeToString(event);
    }

    /** spec/logging.md allows exactly three level strings; logback has no CRITICAL. */
    private static String level(Level level) {
        if (level.isGreaterOrEqual(Level.ERROR)) {
            return "error";
        }
        return level.isGreaterOrEqual(Level.WARN) ? "warn" : "info";
    }

    private static String requestId(ILoggingEvent event) {
        for (KeyValuePair pair : pairs(event)) {
            if (REQUEST_ID.equals(pair.key)) {
                return String.valueOf(pair.value);
            }
        }
        return "";
    }

    private static void writeFields(ILoggingEvent event, BiConsumer<String, Object> out) {
        for (KeyValuePair pair : pairs(event)) {
            if (!REQUEST_ID.equals(pair.key)) {
                out.accept(pair.key, pair.value);
            }
        }
    }

    /** {@code getKeyValuePairs()} is null, not empty, on an event built without any. */
    private static List<KeyValuePair> pairs(ILoggingEvent event) {
        List<KeyValuePair> pairs = event.getKeyValuePairs();
        return pairs == null ? List.of() : pairs;
    }
}
