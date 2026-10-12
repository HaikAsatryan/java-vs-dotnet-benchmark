using Ledgerline.Config;

namespace Ledgerline.Observability;

public sealed class RuntimeStatsSnapshot
{
   public required LedgerConfig Cfg { get; init; }
   public required double UptimeSeconds { get; init; }

   public required long GcGen0 { get; init; }
   public required long GcGen1 { get; init; }
   public required long GcGen2 { get; init; }
   public required double GcPauseTotalSeconds { get; init; }
   public required long GcHeapInUseBytes { get; init; }
   public required long GcHeapCommittedBytes { get; init; }
   public required long GcAllocatedTotalBytes { get; init; }
   public required long ThreadPoolThreads { get; init; }

   public required long DbConnMax { get; init; }
   public required long DbConnTotal { get; init; }
   public required long DbConnIdle { get; init; }
   public required long DbConnInUse { get; init; }

   public required int RedisTotal { get; init; }
   public required int RedisIdle { get; init; }
   public required int RedisInUse { get; init; }

   public required long[] EndpointCalls { get; init; }
}
