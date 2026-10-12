using System.Buffers;
using System.Text.Json;
using Ledgerline.Json;

namespace Ledgerline.Observability;

/// Writes the frozen /runtime-stats JSON shape (spec/runtime-stats.md). Identical SHAPE on
/// both services; values runtime-specific; null where .NET has no value (the spec-frozen gogc
/// and gomemlimit keys, and gc.heap_goal_bytes).
public static class RuntimeStatsWriter
{
   private static readonly string RuntimeVersion = Environment.Version.ToString();
   private static readonly string ServiceVersion =
      Environment.GetEnvironmentVariable("SERVICE_VERSION") is { Length: > 0 } v ? v : "dev";

   public static byte[] Write(RuntimeStatsSnapshot s)
   {
      var buffer = new ArrayBufferWriter<byte>(1024);
      using var w = new Utf8JsonWriter(buffer, new JsonWriterOptions { Indented = false, SkipValidation = true });
      var cfg = s.Cfg;

      w.WriteStartObject();
      w.WriteString("ts", Rfc3339Micros.Format6(DateTime.UtcNow));
      w.WriteNumber("uptime_seconds", s.UptimeSeconds);

      w.WriteStartObject("build");
      w.WriteString("language", "dotnet");
      w.WriteString("runtime_version", RuntimeVersion);
      w.WriteString("service_version", ServiceVersion);
      w.WriteString("data_layer", cfg.DataLayerName);
      w.WriteNumber("processor_count", Environment.ProcessorCount);
      w.WriteEndObject();

      w.WriteStartObject("config");
      w.WriteNumber("db_pool_max", cfg.DbPoolMax);
      w.WriteNumber("db_pool_min", cfg.DbPoolMin);
      w.WriteNumber("db_conn_lifetime_seconds", cfg.DbConnLifetimeSeconds);
      w.WriteNumber("redis_pool_max", cfg.RedisPoolMax);
      w.WriteNumber("cache_ttl_seconds", cfg.CacheTtlSeconds);
      w.WriteNumber("log_queue_len", cfg.LogQueueLen);
      w.WriteNumber("max_auto_prepare", cfg.MaxAutoPrepare);
      w.WriteNull("gogc");                                   // spec-frozen key; null on both runtimes
      w.WriteNull("gomemlimit");                             // spec-frozen key; null on both runtimes
      w.WriteString("gc_dynamic_adaptation_mode", cfg.GcDynamicAdaptationMode);
      w.WriteEndObject();

      w.WriteStartObject("gc");
      w.WriteStartObject("collections");
      w.WriteNumber("gen0", s.GcGen0);
      w.WriteNumber("gen1", s.GcGen1);
      w.WriteNumber("gen2", s.GcGen2);
      w.WriteEndObject();
      w.WriteNumber("pause_total_seconds", s.GcPauseTotalSeconds);
      w.WriteNumber("heap_in_use_bytes", s.GcHeapInUseBytes);
      w.WriteNumber("heap_committed_bytes", s.GcHeapCommittedBytes);
      w.WriteNull("heap_goal_bytes");                        // no direct DATAS analog
      w.WriteNumber("allocated_total_bytes", s.GcAllocatedTotalBytes);
      w.WriteEndObject();

      w.WriteStartObject("db_pool");
      w.WriteNumber("max", s.DbConnMax);
      w.WriteNumber("total", s.DbConnTotal);
      w.WriteNumber("idle", s.DbConnIdle);
      w.WriteNumber("in_use", s.DbConnInUse);
      w.WriteNull("acquire_count");                          // not exposed by Npgsql meters
      w.WriteNull("empty_acquire_count");
      w.WriteNull("acquire_wait_total_seconds");
      w.WriteEndObject();

      w.WriteStartObject("redis_pool");
      w.WriteNumber("max", cfg.RedisPoolMax);
      w.WriteNumber("total", s.RedisTotal);
      w.WriteNumber("idle", s.RedisIdle);
      w.WriteNumber("in_use", s.RedisInUse);
      w.WriteEndObject();

      w.WriteNumber("goroutines_or_threadpool", s.ThreadPoolThreads);

      w.WriteStartObject("endpoint_calls");
      w.WriteNumber("GET /invoices/{id}", s.EndpointCalls[(int)EndpointKind.GetInvoice]);
      w.WriteNumber("GET /customers/{id}/invoices", s.EndpointCalls[(int)EndpointKind.ListInvoices]);
      w.WriteNumber("POST /pricing/quote", s.EndpointCalls[(int)EndpointKind.PricingQuote]);
      w.WriteNumber("POST /invoices", s.EndpointCalls[(int)EndpointKind.CreateInvoice]);
      w.WriteNumber("POST /invoices/{id}/pdf-stub", s.EndpointCalls[(int)EndpointKind.PdfStub]);
      w.WriteNumber("GET /healthz", s.EndpointCalls[(int)EndpointKind.Healthz]);
      w.WriteEndObject();

      w.WriteEndObject();
      w.Flush();
      return buffer.WrittenSpan.ToArray();
   }
}
