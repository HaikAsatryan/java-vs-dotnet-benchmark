using System.Diagnostics.Metrics;
using Ledgerline.Config;

namespace Ledgerline.Observability;

/// Singleton owning a MeterListener over System.Runtime + Npgsql meters, plus endpoint_calls
/// counters. Produces the frozen /runtime-stats shape (spec/runtime-stats.md). Where .NET lacks
/// an analog the field is null. Equivalence checks SHAPE only.
public sealed class RuntimeStatsCollector : IDisposable
{
   private readonly LedgerConfig _cfg;
   private readonly DateTime _startUtc = DateTime.UtcNow;
   private MeterListener? _listener;

   // System.Runtime GC measurements (latest snapshot).
   private long _gcGen0, _gcGen1, _gcGen2;
   private double _gcPauseTimeSeconds;
   private long _gcHeapLastSize;
   private long _gcHeapCommitted;
   private long _gcAllocatedTotal;
   private long _threadPoolThreads;

   // Npgsql connection metrics (db.client.connection.*).
   private long _dbConnMax;
   private long _dbConnUsed;
   private long _dbConnIdle;

   // endpoint_calls: process-lifetime totals, frozen route-template keys.
   private readonly long[] _endpointCalls = new long[6];

   // Serializes Snapshot()'s zero-then-accumulate over the connection counters;
   // overlapping /runtime-stats requests would otherwise interleave and misreport.
   private readonly Lock _snapshotLock = new();

   public RuntimeStatsCollector(LedgerConfig cfg)
   {
      _cfg = cfg;
   }

   public void Start()
   {
      var listener = new MeterListener();
      listener.InstrumentPublished = (instrument, l) =>
      {
         if (instrument.Meter.Name is "System.Runtime" or "Npgsql")
            l.EnableMeasurementEvents(instrument);
      };
      listener.SetMeasurementEventCallback<long>(OnLong);
      listener.SetMeasurementEventCallback<double>(OnDouble);
      listener.SetMeasurementEventCallback<int>((inst, m, tags, state) => OnLong(inst, m, tags, state));
      listener.Start();
      _listener = listener;
   }

   private void OnLong(Instrument instrument, long measurement, ReadOnlySpan<KeyValuePair<string, object?>> tags, object? state)
   {
      switch (instrument.Name)
      {
         case "dotnet.gc.collections":
            {
               var gen = TagValue(tags, "gc.heap.generation");
               switch (gen)
               {
                  case "gen0": _gcGen0 = measurement; break;
                  case "gen1": _gcGen1 = measurement; break;
                  case "gen2": _gcGen2 = measurement; break;
               }
               break;
            }
         case "dotnet.gc.last_collection.heap.size":
            _gcHeapLastSize = measurement;
            break;
         case "dotnet.gc.last_collection.memory.committed_size":
            _gcHeapCommitted = measurement;
            break;
         case "dotnet.gc.heap.total_allocated":
            _gcAllocatedTotal = measurement;
            break;
         case "dotnet.thread_pool.thread.count":
            _threadPoolThreads = measurement;
            break;
         case "db.client.connection.max":
            _dbConnMax = measurement;
            break;
         case "db.client.connection.count":
            {
               // Npgsql >= 9 tags the state per stable OTel db semconv
               // ("db.client.connection.state"); older releases used bare "state".
               var st = TagValue(tags, "db.client.connection.state") ?? TagValue(tags, "state");
               switch (st)
               {
                  // Accumulate: counts are zeroed in Snapshot() before
                  // RecordObservableInstruments(), so multiple observations in
                  // one pass (e.g. per-pool) sum instead of last-write-wins.
                  case "used": _dbConnUsed += measurement; break;
                  case "idle": _dbConnIdle += measurement; break;
               }
               break;
            }
      }
   }

   private void OnDouble(Instrument instrument, double measurement, ReadOnlySpan<KeyValuePair<string, object?>> tags, object? state)
   {
      switch (instrument.Name)
      {
         case "dotnet.gc.pause.time":
            _gcPauseTimeSeconds = measurement;
            break;
      }
   }

   private static string? TagValue(ReadOnlySpan<KeyValuePair<string, object?>> tags, string key)
   {
      foreach (var t in tags)
         if (t.Key == key)
            return t.Value?.ToString();
      return null;
   }

   public void RecordEndpointCall(EndpointKind kind) =>
      Interlocked.Increment(ref _endpointCalls[(int)kind]);

   public RuntimeStatsSnapshot Snapshot(int redisTotal, int redisIdle, int redisInUse)
   {
      // System.Runtime/Npgsql gauges are OBSERVABLE instruments: they only emit measurements
      // when explicitly recorded. /runtime-stats is polled at 1 Hz by the runner, so recording
      // on demand here is the cheapest correct trigger (without it every value stays 0).
      // Connection counts are observed as absolute per-state values each pass: zero first,
      // accumulate in the callback (see OnLong), under the lock, so overlapping polls
      // cannot interleave zeroing with accumulation.
      using var _ = _snapshotLock.EnterScope();
      _dbConnUsed = 0;
      _dbConnIdle = 0;
      _listener?.RecordObservableInstruments();
      return new()
      {
         Cfg = _cfg,
         UptimeSeconds = (DateTime.UtcNow - _startUtc).TotalSeconds,
         GcGen0 = _gcGen0,
         GcGen1 = _gcGen1,
         GcGen2 = _gcGen2,
         GcPauseTotalSeconds = _gcPauseTimeSeconds,
         GcHeapInUseBytes = _gcHeapLastSize,
         GcHeapCommittedBytes = _gcHeapCommitted,
         GcAllocatedTotalBytes = _gcAllocatedTotal,
         ThreadPoolThreads = _threadPoolThreads,
         DbConnMax = _dbConnMax == 0 ? _cfg.DbPoolMax : _dbConnMax,
         DbConnTotal = _dbConnUsed + _dbConnIdle,
         DbConnIdle = _dbConnIdle,
         DbConnInUse = _dbConnUsed,
         RedisTotal = redisTotal,
         RedisIdle = redisIdle,
         RedisInUse = redisInUse,
         EndpointCalls = (long[])_endpointCalls.Clone(),
      };
   }

   private bool _disposed;

   public void Dispose()
   {
      if (_disposed)
         return;
      _disposed = true;
      _listener?.Dispose();
   }
}

public enum EndpointKind
{
   GetInvoice = 0,
   ListInvoices = 1,
   PricingQuote = 2,
   CreateInvoice = 3,
   PdfStub = 4,
   Healthz = 5,
}
