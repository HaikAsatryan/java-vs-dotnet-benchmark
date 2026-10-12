using System.Text.Json;
using Ledgerline.Config;
using Ledgerline.Observability;
using Xunit;

namespace Ledgerline.Tests;

/// Asserts the /runtime-stats JSON SHAPE (keys/types) matches spec/runtime-stats.md. Values are
/// runtime-specific; the equivalence suite checks shape only. The spec-frozen keys with no
/// analog on either runtime (gogc, gomemlimit) are present and null.
public class RuntimeStatsShapeTests
{
   private static LedgerConfig Cfg() => LedgerConfig.FromEnvironment();

   private static JsonElement Render()
   {
      var collector = new RuntimeStatsCollector(Cfg());
      var snapshot = collector.Snapshot(redisTotal: 1, redisIdle: 0, redisInUse: 0);
      var bytes = RuntimeStatsWriter.Write(snapshot);
      return JsonDocument.Parse(bytes).RootElement.Clone();
   }

   [Fact]
   public void TopLevelKeys_Present()
   {
      var r = Render();
      foreach (var key in new[] { "ts", "uptime_seconds", "build", "config", "gc", "db_pool", "redis_pool", "goroutines_or_threadpool", "endpoint_calls" })
         Assert.True(r.TryGetProperty(key, out _), $"missing key {key}");
   }

   [Fact]
   public void Build_Block_Shape()
   {
      var b = Render().GetProperty("build");
      Assert.Equal("dotnet", b.GetProperty("language").GetString());
      Assert.True(b.TryGetProperty("runtime_version", out _));
      Assert.True(b.TryGetProperty("service_version", out _));
      Assert.Equal(Cfg().DataLayerName, b.GetProperty("data_layer").GetString());
      Assert.True(b.GetProperty("processor_count").GetInt32() > 0);
   }

   [Fact]
   public void Config_Block_FrozenRoundOneKnobsAreNull()
   {
      var c = Render().GetProperty("config");
      Assert.Equal(JsonValueKind.Null, c.GetProperty("gogc").ValueKind);
      Assert.Equal(JsonValueKind.Null, c.GetProperty("gomemlimit").ValueKind);
      Assert.Equal(JsonValueKind.String, c.GetProperty("gc_dynamic_adaptation_mode").ValueKind);
      foreach (var key in new[] { "db_pool_max", "db_pool_min", "db_conn_lifetime_seconds", "redis_pool_max", "cache_ttl_seconds", "log_queue_len", "max_auto_prepare" })
         Assert.Equal(JsonValueKind.Number, c.GetProperty(key).ValueKind);
   }

   [Fact]
   public void Gc_Block_Shape_HeapGoalIsNull()
   {
      var gc = Render().GetProperty("gc");
      var col = gc.GetProperty("collections");
      foreach (var g in new[] { "gen0", "gen1", "gen2" })
         Assert.Equal(JsonValueKind.Number, col.GetProperty(g).ValueKind);
      Assert.True(gc.TryGetProperty("pause_total_seconds", out _));
      Assert.True(gc.TryGetProperty("heap_in_use_bytes", out _));
      Assert.True(gc.TryGetProperty("heap_committed_bytes", out _));
      Assert.Equal(JsonValueKind.Null, gc.GetProperty("heap_goal_bytes").ValueKind);   // no DATAS analog
      Assert.True(gc.TryGetProperty("allocated_total_bytes", out _));
   }

   [Fact]
   public void DbPool_NpgsqlUnexposedCountersAreNull()
   {
      var p = Render().GetProperty("db_pool");
      foreach (var k in new[] { "max", "total", "idle", "in_use" })
         Assert.Equal(JsonValueKind.Number, p.GetProperty(k).ValueKind);
      foreach (var k in new[] { "acquire_count", "empty_acquire_count", "acquire_wait_total_seconds" })
         Assert.Equal(JsonValueKind.Null, p.GetProperty(k).ValueKind);
   }

   [Fact]
   public void EndpointCalls_FrozenRouteTemplateKeys()
   {
      var e = Render().GetProperty("endpoint_calls");
      foreach (var key in new[]
      {
         "GET /invoices/{id}", "GET /customers/{id}/invoices", "POST /pricing/quote",
         "POST /invoices", "POST /invoices/{id}/pdf-stub", "GET /healthz",
      })
         Assert.True(e.TryGetProperty(key, out _), $"missing endpoint key {key}");
   }

   [Fact]
   public void EndpointCalls_IncrementCounters()
   {
      var collector = new RuntimeStatsCollector(Cfg());
      collector.RecordEndpointCall(EndpointKind.GetInvoice);
      collector.RecordEndpointCall(EndpointKind.GetInvoice);
      collector.RecordEndpointCall(EndpointKind.PricingQuote);

      var bytes = RuntimeStatsWriter.Write(collector.Snapshot(0, 0, 0));
      var e = JsonDocument.Parse(bytes).RootElement.GetProperty("endpoint_calls");
      Assert.Equal(2, e.GetProperty("GET /invoices/{id}").GetInt32());
      Assert.Equal(1, e.GetProperty("POST /pricing/quote").GetInt32());
      Assert.Equal(0, e.GetProperty("POST /invoices").GetInt32());
   }
}
