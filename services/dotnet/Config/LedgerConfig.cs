namespace Ledgerline.Config;

public enum DataLayer { EfCore, Ado, Dapper }

/// Effective configuration resolved from env at startup. Echoed once via the msg="config" line
/// and via /runtime-stats. Same env-var surface as the Java service (spec/db-access.md;
/// application.yaml's `ledger.*` block spells the same names).
public sealed class LedgerConfig
{
   public required int Port { get; init; }
   public required string PgHost { get; init; }
   public required int PgPort { get; init; }
   public required string PgDb { get; init; }
   public required string PgUser { get; init; }
   public required string PgPassword { get; init; }

   public required int DbPoolMax { get; init; }
   public required int DbPoolMin { get; init; }
   public required int DbConnLifetimeSeconds { get; init; }
   public required int MaxAutoPrepare { get; init; }
   public required int AutoPrepareMinUsages { get; init; }
   // Open all DbPoolMax connections at startup (spec/db-access.md: both services
   // warm concurrently). Off only for DB-less test hosts (WebApplicationFactory).
   public required bool DbPoolWarmOnStart { get; init; }

   public required string RedisAddr { get; init; }
   public required int RedisPoolMax { get; init; }
   public required int CacheTtlSeconds { get; init; }

   public required DataLayer DataLayer { get; init; }
   public required int LogQueueLen { get; init; }
   public required long BodyMaxBytes { get; init; }
   public required string PdfDir { get; init; }

   public required string GcDynamicAdaptationMode { get; init; }

   public TimeSpan CacheTtl => TimeSpan.FromSeconds(CacheTtlSeconds);

   /// The wire name echoed in /runtime-stats build.data_layer and the config line
   /// (asserted against the cell's DATA_LAYER by the bench runner).
   public string DataLayerName => DataLayer switch
   {
      DataLayer.Ado => "ado",
      DataLayer.Dapper => "dapper",
      _ => "efcore",
   };

   public static LedgerConfig FromEnvironment()
   {
      // min=max is POLICY (spec/db-access.md): HikariCP and Npgsql idle-prune on
      // different clocks (idleTimeout 600 s vs Connection Idle Lifetime 300 s),
      // so a min below max would shrink the two pools differently mid-run. Both
      // services refuse to start on a mismatch (LedgerProperties does the same).
      var poolMax = EnvInt("DB_POOL_MAX", 24);
      var poolMin = EnvInt("DB_POOL_MIN", poolMax);
      if (poolMin != poolMax)
         throw new InvalidOperationException(
            $"DB_POOL_MIN={poolMin} != DB_POOL_MAX={poolMax} (min=max policy, spec/db-access.md)");

      return new LedgerConfig
      {
         Port = EnvInt("PORT", 8080),
         PgHost = Env("PG_HOST", "localhost"),
         PgPort = EnvInt("PG_PORT", 5432),
         PgDb = Env("PG_DB", "ledgerline"),
         PgUser = Env("PG_USER", "ledger"),
         PgPassword = Env("PG_PASSWORD", "ledger"),

         DbPoolMax = poolMax,
         DbPoolMin = poolMin,
         DbConnLifetimeSeconds = EnvInt("DB_CONN_LIFETIME", 3600),
         MaxAutoPrepare = EnvInt("MAX_AUTO_PREPARE", 0),
         AutoPrepareMinUsages = EnvInt("AUTO_PREPARE_MIN_USAGES", 5),
         DbPoolWarmOnStart = EnvBool("DB_POOL_WARM_ON_START", true),

         RedisAddr = Env("REDIS_ADDR", "localhost:6379"),
         RedisPoolMax = EnvInt("REDIS_POOL_MAX", 16),
         CacheTtlSeconds = EnvInt("CACHE_TTL_SECONDS", 300),

         DataLayer = Env("DATA_LAYER", "efcore").Trim().ToLowerInvariant() switch
         {
            "ado" => DataLayer.Ado,
            "dapper" => DataLayer.Dapper,
            _ => DataLayer.EfCore,
         },
         LogQueueLen = EnvInt("LOG_QUEUE_LEN", 8192),
         BodyMaxBytes = EnvLong("BODY_MAX_BYTES", 65536),
         PdfDir = Env("PDF_DIR", "/data/pdf"),

         GcDynamicAdaptationMode = Env("DOTNET_GCDynamicAdaptationMode", "1"),
      };
   }

   private static string Env(string key, string fallback) =>
      Environment.GetEnvironmentVariable(key) is { Length: > 0 } v ? v : fallback;

   // A SET-but-malformed value refuses startup on BOTH services (Spring's
   // binder rejects it on the Java side). A silent TryParse fallback would let
   // a typo'd knob run one service at the default while the other refuses: a
   // cross-language config divergence inside a measured run.
   private static int EnvInt(string key, int fallback)
   {
      var raw = Environment.GetEnvironmentVariable(key);
      if (string.IsNullOrEmpty(raw)) return fallback;
      return int.TryParse(raw, out var v)
         ? v
         : throw new InvalidOperationException($"config: {key}={raw} is not an integer");
   }

   private static long EnvLong(string key, long fallback)
   {
      var raw = Environment.GetEnvironmentVariable(key);
      if (string.IsNullOrEmpty(raw)) return fallback;
      return long.TryParse(raw, out var v)
         ? v
         : throw new InvalidOperationException($"config: {key}={raw} is not an integer");
   }

   private static bool EnvBool(string key, bool fallback) =>
      Environment.GetEnvironmentVariable(key) is { Length: > 0 } v
         ? v is "1" or "true" or "True" or "TRUE" or "yes"
         : fallback;
}
