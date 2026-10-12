using Microsoft.Extensions.Logging;

namespace Ledgerline.Logging;

/// Source-generated log methods. The Message string IS the frozen event name (msg=...); the
/// named parameters become the structured fields the custom formatter flattens. Parameter names
/// are the EXACT frozen field names from spec/logging.md (request_id, method, path, status, etc.).
internal static partial class LedgerLog
{
   // Access line: exactly one per HTTP request (health/readyz/runtime-stats excluded by middleware).
   [LoggerMessage(Level = LogLevel.Information, Message = "http_access")]
   public static partial void HttpAccess(
      ILogger logger, string method, string path, int status, double duration_ms, string request_id);

   // Domain events (successful writes).
   [LoggerMessage(Level = LogLevel.Information, Message = "invoice_created")]
   public static partial void InvoiceCreated(
      ILogger logger, long customer_id, long invoice_id, int item_count, long total_minor, string request_id);

   [LoggerMessage(Level = LogLevel.Information, Message = "cache_invalidated")]
   public static partial void CacheInvalidated(ILogger logger, long invoice_id, string request_id);

   [LoggerMessage(Level = LogLevel.Information, Message = "pdf_stub_written")]
   public static partial void PdfStubWritten(ILogger logger, long invoice_id, int bytes_written, string request_id);

   [LoggerMessage(Level = LogLevel.Information, Message = "quote_computed")]
   public static partial void QuoteComputed(ILogger logger, int item_count, long total_minor, string request_id);

   // Errors.
   [LoggerMessage(Level = LogLevel.Error, Message = "request_error")]
   public static partial void RequestError(ILogger logger, int status, string error_class, string request_id);

   [LoggerMessage(Level = LogLevel.Error, Message = "exception")]
   public static partial void Exception(ILogger logger, string detail, string request_id);

   // Lifecycle (request_id empty).
   [LoggerMessage(Level = LogLevel.Information, Message = "shutdown_complete")]
   public static partial void ShutdownComplete(ILogger logger);
}
