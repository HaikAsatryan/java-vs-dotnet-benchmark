using Ledgerline.Config;
using Npgsql;

namespace Ledgerline.Infrastructure;

public static class NpgsqlConnectionString
{
   public static string Build(LedgerConfig cfg)
   {
      var b = new NpgsqlConnectionStringBuilder
      {
         Host = cfg.PgHost,
         Port = cfg.PgPort,
         Database = cfg.PgDb,
         Username = cfg.PgUser,
         Password = cfg.PgPassword,
         Pooling = true,
         MaxPoolSize = cfg.DbPoolMax,                 // DB_POOL_MAX (== HikariCP maximumPoolSize)
         MinPoolSize = cfg.DbPoolMin,                 // DB_POOL_MIN (= DB_POOL_MAX): no cold-start/idle-prune
         ConnectionLifetime = cfg.DbConnLifetimeSeconds, // explicit pin (DB_CONN_LIFETIME)
         // D4: 20 is the HEADLINE (behaviour parity with PgJDBC's default
         // prepareThreshold=5); 0 — the Npgsql default — is the declared
         // variant.npgsql-default-prepare cell. spec/db-access.md.
         MaxAutoPrepare = cfg.MaxAutoPrepare,
         // Multiplexing left OFF (default): experimental (spec/db-access.md).
      };

      // AutoPrepareMinUsages only meaningful when MaxAutoPrepare > 0.
      if (cfg.MaxAutoPrepare > 0)
         b.AutoPrepareMinUsages = cfg.AutoPrepareMinUsages;

      return b.ConnectionString;
   }
}
