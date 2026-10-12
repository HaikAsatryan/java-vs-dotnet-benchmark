package io.ledgerline.infrastructure;

import io.ledgerline.config.LedgerProperties;
import io.ledgerline.logging.LedgerLog;
import jakarta.annotation.PostConstruct;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.event.ContextClosedEvent;
import org.springframework.context.event.EventListener;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;

/**
 * PDF_DIR at startup and the last line on stdout, the two things {@code Program.cs} does around
 * {@code app.Run()}. A directory that disappears mid-run is a 500 on both sides, never a silent
 * re-create.
 */
@Component
public class ServiceLifecycle {

    private static final Logger LOGGER = LoggerFactory.getLogger("io.ledgerline.Ledgerline");

    private final LedgerProperties properties;

    ServiceLifecycle(LedgerProperties properties) {
        this.properties = properties;
    }

    @PostConstruct
    void createPdfDir() {
        try {
            Files.createDirectories(Path.of(properties.pdfDir()));
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    /**
     * {@code ContextClosedEvent} fires after the connector has drained in-flight requests
     * ({@code server.shutdown: graceful}), which is where ApplicationStopped sits on the .NET
     * side. LOWEST_PRECEDENCE so nothing else in the application logs after it; the async
     * appender's own drain happens later still, when logback's shutdown hook stops the context.
     */
    @EventListener(ContextClosedEvent.class)
    @Order(Ordered.LOWEST_PRECEDENCE)
    public void shutdownComplete() {
        LedgerLog.shutdownComplete(LOGGER);
    }
}
