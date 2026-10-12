using Ledgerline.Config;
using Microsoft.Extensions.Logging;

namespace Ledgerline.Infrastructure;

/// One structured msg="config" line at startup with every effective knob + ProcessorCount.
/// The runner greps this single line into the run manifest. Uses the custom console formatter,
/// so the flat-JSON fields here become top-level keys.
public static class EffectiveConfig
{
   public static void LogOnce(ILogger logger, LedgerConfig cfg)
   {
      // Manual structured log so every knob is a flat field on the config line.
      // The state IS the field array: KeyValuePair<,>[] already implements
      // IReadOnlyList<KeyValuePair<string, object?>>, which is what the console
      // formatter reads.
      logger.Log(
         LogLevel.Information,
         default,
         Fields(cfg),
         null,
         static (_, _) => "config");
   }

   private static KeyValuePair<string, object?>[] Fields(LedgerConfig cfg) =>
      [
         new("data_layer", cfg.DataLayerName),
         new("port", cfg.Port),
         new("db_pool_max", cfg.DbPoolMax),
         new("db_pool_min", cfg.DbPoolMin),
         new("db_conn_lifetime_seconds", cfg.DbConnLifetimeSeconds),
         new("max_auto_prepare", cfg.MaxAutoPrepare),
         new("redis_pool_max", cfg.RedisPoolMax),
         new("cache_ttl_seconds", cfg.CacheTtlSeconds),
         new("log_queue_len", cfg.LogQueueLen),
         new("body_max_bytes", cfg.BodyMaxBytes),
         new("pdf_dir", cfg.PdfDir),
         new("gc_dynamic_adaptation_mode", cfg.GcDynamicAdaptationMode),
         new("processor_count", Environment.ProcessorCount),
         new("runtime_version", Environment.Version.ToString()),
         new("npgsql_version", typeof(Npgsql.NpgsqlConnection).Assembly.GetName().Version?.ToString()),
         new("service_version", Environment.GetEnvironmentVariable("SERVICE_VERSION") is { Length: > 0 } v ? v : "dev"),
      ];
}
