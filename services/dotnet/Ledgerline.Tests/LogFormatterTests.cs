using System.Text.Json;
using System.Text.RegularExpressions;
using Ledgerline.Logging;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Logging.Console;
using Xunit;

namespace Ledgerline.Tests;

/// Verifies the custom ConsoleFormatter emits spec/logging.md's exact flat JSON schema:
/// ts (6-frac UTC Z), lowercase level, msg, request_id, plus flat event fields.
public class LogFormatterTests
{
   private static readonly Regex TsMicros =
      new(@"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$", RegexOptions.Compiled);

   private static string Format<TState>(LogLevel level, string eventName, TState state)
   {
      var formatter = new LedgerConsoleFormatter();
      var entry = new LogEntry<TState>(level, "cat", default, state, null, (s, _) => eventName);
      using var sw = new StringWriter();
      formatter.Write(in entry, null, sw);
      return sw.ToString();
   }

   [Fact]
   public void AccessLine_FlatSchema_WithSixDigitTs_LowercaseLevel()
   {
      // Mirror the [LoggerMessage] state shape: a KVP list with the event fields + {OriginalFormat}.
      var state = new List<KeyValuePair<string, object?>>
      {
         new("method", "GET"),
         new("path", "/invoices/{id}"),
         new("status", 200),
         new("duration_ms", 1.234),
         new("request_id", "abc-123"),
         new("{OriginalFormat}", "http_access"),
      };

      var line = Format(LogLevel.Information, "http_access", state);
      Assert.EndsWith("\n", line);

      using var doc = JsonDocument.Parse(line);
      var root = doc.RootElement;

      Assert.Matches(TsMicros, root.GetProperty("ts").GetString()!);
      Assert.Equal("info", root.GetProperty("level").GetString());
      Assert.Equal("http_access", root.GetProperty("msg").GetString());
      Assert.Equal("abc-123", root.GetProperty("request_id").GetString());
      Assert.Equal("GET", root.GetProperty("method").GetString());
      Assert.Equal("/invoices/{id}", root.GetProperty("path").GetString());
      Assert.Equal(200, root.GetProperty("status").GetInt32());
      Assert.Equal(1.234, root.GetProperty("duration_ms").GetDouble());

      // {OriginalFormat} must NOT leak into the line.
      Assert.False(root.TryGetProperty("{OriginalFormat}", out _));
   }

   [Theory]
   [InlineData(LogLevel.Information, "info")]
   [InlineData(LogLevel.Warning, "warn")]
   [InlineData(LogLevel.Error, "error")]
   [InlineData(LogLevel.Critical, "error")]
   public void Level_MapsToFrozenLowercase(LogLevel level, string expected)
   {
      var state = new List<KeyValuePair<string, object?>> { new("{OriginalFormat}", "x") };
      var line = Format(level, "x", state);
      using var doc = JsonDocument.Parse(line);
      Assert.Equal(expected, doc.RootElement.GetProperty("level").GetString());
   }

   [Fact]
   public void LifecycleLine_HasEmptyRequestId()
   {
      var state = new List<KeyValuePair<string, object?>> { new("{OriginalFormat}", "shutdown_complete") };
      var line = Format(LogLevel.Information, "shutdown_complete", state);
      using var doc = JsonDocument.Parse(line);
      Assert.Equal(string.Empty, doc.RootElement.GetProperty("request_id").GetString());
      Assert.Equal("shutdown_complete", doc.RootElement.GetProperty("msg").GetString());
   }

   [Fact]
   public void DomainEvent_FieldsAreFlatAndTyped()
   {
      var state = new List<KeyValuePair<string, object?>>
      {
         new("customer_id", 42L),
         new("invoice_id", 1001L),
         new("item_count", 5),
         new("total_minor", 99500L),
         new("request_id", "r1"),
         new("{OriginalFormat}", "invoice_created"),
      };
      var line = Format(LogLevel.Information, "invoice_created", state);
      using var doc = JsonDocument.Parse(line);
      var root = doc.RootElement;
      Assert.Equal("invoice_created", root.GetProperty("msg").GetString());
      Assert.Equal(42L, root.GetProperty("customer_id").GetInt64());
      Assert.Equal(1001L, root.GetProperty("invoice_id").GetInt64());
      Assert.Equal(5, root.GetProperty("item_count").GetInt32());
      Assert.Equal(99500L, root.GetProperty("total_minor").GetInt64());
   }
}
