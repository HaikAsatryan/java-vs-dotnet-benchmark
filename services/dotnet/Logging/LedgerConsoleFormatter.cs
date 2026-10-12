using System.Buffers;
using System.Text.Json;
using Ledgerline.Json;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Logging.Console;

namespace Ledgerline.Logging;

/// Custom ConsoleFormatter emitting spec/logging.md's exact flat JSON line:
///   { "ts": <RFC3339 6-frac UTC Z>, "level": "info|warn|error", "msg": <event>,
///     "request_id": <id or "">, ...event fields... }
/// Field NAMES/VALUES are frozen; field ORDER is irrelevant. The stock JsonConsoleFormatter
/// nests under "State"/"Message" and emits a 7-digit timestamp; this replaces it.
public sealed class LedgerConsoleFormatter() : ConsoleFormatter(FormatterName)
{
   public const string FormatterName = "ledger";

   public override void Write<TState>(
      in LogEntry<TState> entry,
      IExternalScopeProvider? scopeProvider,
      TextWriter textWriter)
   {
      var level = entry.LogLevel switch
      {
         LogLevel.Trace or LogLevel.Debug or LogLevel.Information => "info",
         LogLevel.Warning => "warn",
         LogLevel.Error or LogLevel.Critical => "error",
         _ => "info",
      };

      // "msg" is the event name. [LoggerMessage] methods set a constant Message (e.g. "http_access");
      // we read it back from the formatted message so the source-gen logger and manual logs agree.
      var msg = entry.Formatter is not null
         ? entry.Formatter(entry.State, entry.Exception)
         : entry.State?.ToString() ?? string.Empty;

      var buffer = new ArrayBufferWriter<byte>(256);
      using (var writer = new Utf8JsonWriter(buffer, JsonWriterOptionsCached))
      {
         writer.WriteStartObject();
         writer.WriteString("ts", Rfc3339Micros.Format6(DateTime.UtcNow));
         writer.WriteString("level", level);
         writer.WriteString("msg", msg);

         var requestId = string.Empty;
         var wroteRequestId = false;

         if (entry.State is IReadOnlyList<KeyValuePair<string, object?>> kvs)
         {
            foreach (var kv in kvs)
            {
               // {OriginalFormat} is the message template; skip it (msg already carries the event).
               if (kv.Key == "{OriginalFormat}")
                  continue;

               if (kv.Key == "request_id")
               {
                  requestId = kv.Value?.ToString() ?? string.Empty;
                  wroteRequestId = true;
                  writer.WriteString("request_id", requestId);
                  continue;
               }

               WriteField(writer, kv.Key, kv.Value);
            }
         }

         if (!wroteRequestId)
            writer.WriteString("request_id", string.Empty);

         writer.WriteEndObject();
      }

      // One line per record; LF terminator (json-file driver splits on newline).
      textWriter.Write(System.Text.Encoding.UTF8.GetString(buffer.WrittenSpan));
      textWriter.Write('\n');
   }

   private static readonly JsonWriterOptions JsonWriterOptionsCached = new() { Indented = false, SkipValidation = true };

   private static void WriteField(Utf8JsonWriter writer, string key, object? value)
   {
      switch (value)
      {
         case null:
            writer.WriteNull(key);
            break;
         case bool b:
            writer.WriteBoolean(key, b);
            break;
         case int i:
            writer.WriteNumber(key, i);
            break;
         case long l:
            writer.WriteNumber(key, l);
            break;
         case double d:
            writer.WriteNumber(key, d);
            break;
         case string s:
            writer.WriteString(key, s);
            break;
         default:
            writer.WriteString(key, value.ToString());
            break;
      }
   }
}
