package io.ledgerline.logging;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.spi.LoggingEventBuilder;

/**
 * The complete set of lines spec/logging.md allows on stdout, 1:1 with
 * {@code services/dotnet/Logging/LedgerLog.cs}. The message IS the frozen event name and the
 * key-value pairs are the frozen field names; {@link LedgerStructuredLogFormatter} flattens both
 * into one JSON object.
 *
 * <p>SLF4J's fluent {@code addKeyValue} rather than {@code "{} {}"} placeholders: the formatter
 * needs the fields as NAMED values, which is what {@code [LoggerMessage]}'s parameter names give
 * the .NET formatter. A placeholder message would arrive as one interpolated string.
 */
public final class LedgerLog {

    private LedgerLog() {
    }

    public static void httpAccess(Logger logger, String method, String path, int status,
            double durationMs, String requestId) {
        event(logger.atInfo(), "http_access", requestId)
                .addKeyValue("method", method)
                .addKeyValue("path", path)
                .addKeyValue("status", status)
                .addKeyValue("duration_ms", durationMs)
                .log();
    }

    public static void invoiceCreated(Logger logger, long customerId, long invoiceId,
            int itemCount, long totalMinor, String requestId) {
        event(logger.atInfo(), "invoice_created", requestId)
                .addKeyValue("customer_id", customerId)
                .addKeyValue("invoice_id", invoiceId)
                .addKeyValue("item_count", itemCount)
                .addKeyValue("total_minor", totalMinor)
                .log();
    }

    public static void cacheInvalidated(Logger logger, long invoiceId, String requestId) {
        event(logger.atInfo(), "cache_invalidated", requestId)
                .addKeyValue("invoice_id", invoiceId)
                .log();
    }

    public static void pdfStubWritten(Logger logger, long invoiceId, int bytesWritten,
            String requestId) {
        event(logger.atInfo(), "pdf_stub_written", requestId)
                .addKeyValue("invoice_id", invoiceId)
                .addKeyValue("bytes_written", bytesWritten)
                .log();
    }

    public static void quoteComputed(Logger logger, int itemCount, long totalMinor,
            String requestId) {
        event(logger.atInfo(), "quote_computed", requestId)
                .addKeyValue("item_count", itemCount)
                .addKeyValue("total_minor", totalMinor)
                .log();
    }

    public static void requestError(Logger logger, int status, String errorClass,
            String requestId) {
        event(logger.atError(), "request_error", requestId)
                .addKeyValue("status", status)
                .addKeyValue("error_class", errorClass)
                .log();
    }

    public static void exception(Logger logger, String detail, String requestId) {
        event(logger.atError(), "exception", requestId)
                .addKeyValue("detail", detail)
                .log();
    }

    /** The effective-config line the runner greps into the manifest. */
    public static void config(Logger logger, Map<String, Object> fields) {
        LoggingEventBuilder builder = event(logger.atInfo(), "config", "");
        fields.forEach(builder::addKeyValue);
        builder.log();
    }

    public static void shutdownComplete(Logger logger) {
        event(logger.atInfo(), "shutdown_complete", "").log();
    }

    private static LoggingEventBuilder event(LoggingEventBuilder builder, String msg,
            String requestId) {
        return builder.setMessage(msg).addKeyValue("request_id", requestId);
    }
}
