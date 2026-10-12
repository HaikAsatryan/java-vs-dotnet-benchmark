# spec/runtime-stats.md — GET /runtime-stats contract (both services, frozen)

Identical required JSON SHAPE on both services; values are runtime-specific.
Polled by the Python runner at 1 Hz for: warmup gates (GC-steady, pools-warm,
per-endpoint call counts), the per-run GC/pool table, and the effective-config
echo. Excluded from the workload mix and from access logging. The recorded
resource numbers still come from cgroup v2, never from this endpoint
(corroboration only).

Sources are `java.lang.management` / `com.sun.management` / `jdk.management`
MXBeans on the JVM and `System.Runtime` meters on .NET. **Never JFR** — a
recording's overhead lands inside the measured window, which this endpoint is
polled across.

## Shape

The block below is the REQUIRED key set: every key is present on both services,
`null` where a runtime has no honest analog. A service MAY emit keys beside
these; the additions are listed under "Per-runtime additions" and the runner
carries them through to the artifacts rather than dropping them. Nothing is
compared by value across services (see the last line of this file).

```json
{
  "ts": "2026-06-06T00:00:00.000000Z",
  "uptime_seconds": 12.3,
  "build": {
    "language": "java|dotnet",
    "runtime_version": "25.0.4+7 | 10.0.x",
    "service_version": "git sha or dev",
    "data_layer": "jpa | efcore | ado",
    "processor_count": 6
  },
  "config": {
    "db_pool_max": 24, "db_pool_min": 24, "db_conn_lifetime_seconds": 3600,
    "redis_pool_max": 16, "cache_ttl_seconds": 300, "log_queue_len": 8192,
    "max_auto_prepare": 20, "gogc": null, "gomemlimit": null,
    "gc_dynamic_adaptation_mode": "1"
  },
  "gc": {
    "collections": { "gen0": 0, "gen1": 0, "gen2": 0 },
    "pause_total_seconds": 0.0,
    "heap_in_use_bytes": 0,
    "heap_committed_bytes": 0,
    "heap_goal_bytes": 0,
    "allocated_total_bytes": 0
  },
  "db_pool": {
    "max": 24, "total": 0, "idle": 0, "in_use": 0,
    "acquire_count": 0, "empty_acquire_count": 0, "acquire_wait_total_seconds": 0.0
  },
  "redis_pool": { "max": 16, "total": 0, "idle": 0, "in_use": 0 },
  "goroutines_or_threadpool": 0,
  "endpoint_calls": {
    "GET /invoices/{id}": 0,
    "GET /customers/{id}/invoices": 0,
    "POST /pricing/quote": 0,
    "POST /invoices": 0,
    "POST /invoices/{id}/pdf-stub": 0,
    "GET /healthz": 0
  }
}
```

`ts` is RFC3339 UTC with **exactly six** fractional digits and a `Z`, the same
format as the wire (`spec/openapi.yaml`). Both `Instant.toString()` and
`OffsetDateTime.toString()` emit variable precision and are wrong here; the JVM
formatter is `uuuu-MM-dd'T'HH:mm:ss.SSSSSS'Z'` at `ZoneOffset.UTC`, which
truncates rather than rounds, matching .NET's `ffffff`.

`config` keys not applicable to a runtime are present with `null`. `gogc` and
`gomemlimit` are the two that are null on **both** round-2 runtimes: they are
round-1 Go knobs that neither the JVM nor the CLR has, kept only because the
required key set above is what both services emit today.

`endpoint_calls` keys are the frozen route templates above; counters are
process-lifetime totals (plain atomic increments) and drive the per-endpoint
warmup gate.

### Per-runtime additions

Keys a service emits **beside** the required set. They are additive by design —
the shape checker requires the set above and does not reject extras — so a
runtime can publish what only it can measure without either side faking a field.

| service | added keys |
|---|---|
| Java | `gc.collections.<GarbageCollectorMXBean name>` (one integer per bean); `gc.collections_note`; `gc.pause_total_seconds_note` (only when that field is null); `config.jdbc_prepare_threshold`, `config.gc_engine`, `config.max_ram_percentage`, `config.max_heap_bytes`, `config.virtual_threads_enabled`, `config.vt_scheduler_parallelism` |
| .NET | none |

## Field -> source mapping (frozen)

| field | Java | .NET |
|---|---|---|
| gc.collections.gen0/1/2 | **null, all three.** No bean set maps onto the CLR's generations without meaning something different per collector; the real counters are published beside the triple under their own bean names (see the two rows below the table) | System.Runtime `dotnet.gc.collections` by generation |
| gc.pause_total_seconds | `GarbageCollectorMXBean.getCollectionTime()` summed over **every** bean under G1 — including `G1 Concurrent GC`, whose time is the Remark/Cleanup STW sub-pauses, not concurrent wall time. **null under ZGC**, see below | `dotnet.gc.pause.time` cumulative |
| gc.heap_in_use_bytes | `MemoryMXBean.getHeapMemoryUsage().getUsed()` | `dotnet.gc.last_collection.heap.size` (nearest analog, documented) |
| gc.heap_committed_bytes | `MemoryMXBean.getHeapMemoryUsage().getCommitted()` — the SAME call as the row above, one read per poll, correct under both collectors; no per-pool iteration | `dotnet.gc.last_collection.memory.committed_size` |
| gc.heap_goal_bytes | `HotSpotDiagnosticMXBean.getVMOption("SoftMaxHeapSize")` under ZGC, where it is the collector's actual target (`origin=ERGONOMIC`). **null under G1**, where it reads back equal to `MaxHeapSize` and is a ceiling, not a goal | null (no direct DATAS analog; documented) |
| gc.allocated_total_bytes | `com.sun.management.ThreadMXBean.getTotalThreadAllocatedBytes()` — counts virtual threads, verified on JDK 25 against a platform-thread control | `dotnet.gc.heap.total_allocated` |
| db_pool.max/total/idle/in_use | `HikariPoolMXBean` `getTotalConnections` / `getIdleConnections` / `getActiveConnections`; `max` is the configured `maximumPoolSize` | Npgsql metrics (`db.client.connection.max`, `db.client.connection.count{state}`) via MeterListener |
| db_pool.acquire_count, empty_acquire_count, acquire_wait_total_seconds | **null.** `HikariPoolMXBean` is four gauges and no cumulative counters | **null.** Not exposed by Npgsql's meters |
| redis_pool.max | the configured `REDIS_POOL_MAX` | the configured `REDIS_POOL_MAX` |
| redis_pool.total | Lettuce: 1 once the shared connection is established, else 0 | `ConnectionMultiplexer.IsConnected ? 1 : 0` -- the same 0-or-1 quantity, also not from `GetCounters()` |
| redis_pool.idle | literal `0`: Lettuce is a multiplexer, not a pool, so there is nothing idle to count | literal `0` in the same tuple, for the same reason -- NOT from `GetCounters()` |
| redis_pool.in_use | literal `0`: nothing is checked out of a multiplexer | SE.Redis `GetCounters().TotalOutstanding` -- commands IN FLIGHT at the poll, so NOT 0 under load (measured 39 at 40-way concurrency, 2026-08-24). The one `/runtime-stats` field the two runtimes do not report the same quantity for; see spec/cache.md and the FAIRNESS row |
| goroutines_or_threadpool | `VirtualThreadSchedulerMXBean.getMountedVirtualThreadCount()` | `dotnet.thread_pool.thread.count` |
| build.processor_count | `Runtime.availableProcessors()` | `Environment.ProcessorCount` |
| config.* | `HotSpotDiagnosticMXBean.getVMOption(...)` for the JVM values, the bound `LedgerProperties` for the rest | the bound `LedgerConfig` |

The field name `goroutines_or_threadpool` is round-1's and is kept because the
key is frozen; it names a **different quantity on each runtime** and always did.
The JVM number is virtual threads currently mounted on carriers — the closest
analog to .NET's thread-pool size, and not equal to it at idle.
`ThreadMXBean.getThreadCount()` is the wrong source: it counts platform threads
only (measured: 39 while 2,000 virtual threads had just run).

`HotSpotDiagnosticMXBean.getVMOption(...)` also carries an `origin`
(`DEFAULT` / `ERGONOMIC` / `VM_CREATION`) for every flag it returns. The echo
above publishes values, not origins; the origin is what makes
`docs/FAIRNESS.md`'s "is this a default?" column evidence rather than assertion,
and it is read back per run rather than asserted here.

### The two rows that cannot be mapped, and are reported as null with a reason

**`gc.collections.gen0/gen1/gen2` — null on the JVM.** `G1 Old Generation`
increments on Full GC **only**; G1's mixed collections, which are how it
actually reclaims old regions, increment `G1 Young Generation`. Generational
ZGC's `ZGC Major Cycles` increments on every old-generation cycle. Measured on
identical work, G1 reads **0** where ZGC reads **3**, and neither counts what
the CLR's `gen2` counts. A frozen `gen2 -> old-generation bean` mapping would
mean three different things under one field name, so the JVM publishes the
triple as a present null and its real counters beside it, one key per
`GarbageCollectorMXBean` name, with `gc.collections_note` carrying this reason
on the wire.

**`gc.pause_total_seconds` — null on the JVM under ZGC.**
`getCollectionTime()` is whole milliseconds and ZGC's pauses are sub-millisecond
by design: measured, 255 pauses totalling **0 ms**, and still 0 ms after a
forced full GC, against 7 ms and 5 ms for G1 on the same workload. Publishing
`0.0` would read as "ZGC is pauseless" in exactly the cell where pause time is
the variable, so the field is null there and `gc.pause_total_seconds_note`
carries the reason. Under G1 it is a real STW total.

### Collector bean names (measured, Temurin 25.0.4)

| collector | beans |
|---|---|
| G1 | `G1 Young Generation`, `G1 Concurrent GC`, `G1 Old Generation` |
| generational ZGC | `ZGC Minor Cycles`, `ZGC Minor Pauses`, `ZGC Major Cycles`, `ZGC Major Pauses` |

**Two beans in that list count pause EVENTS, not collections, and summing
everything present is wrong by a factor of three to four.** Under ZGC, `*Cycles`
and `*Pauses` are separate beans over the same pools: measured over one 18 s
run, 1,029 minor cycles against 3,094 minor pauses, 80 major cycles against 394
major pauses. Under G1, `G1 Concurrent GC` counts the Remark and Cleanup STW
sub-pauses of a concurrent mark cycle: measured 86 events against exactly 43
`Pause Remark` + 43 `Pause Cleanup` in `-Xlog:gc`, over 43 concurrent cycles.
Its *time* is genuine stop-the-world time and does belong in
`gc.pause_total_seconds`; its *count* is not a collection count. So the two bean
sets are not one list and cannot be derived from each other.

## Warmup GC-steady: the declared cadence source

`spec/slo.yaml warmup.gates.gc_steady` requires two series to show no positive
trend: `heap_committed`, and the **collection cadence** (collections per poll
interval). The runtimes reach the second differently and the difference is
declared, not inferred:

| runtime | cadence source |
|---|---|
| .NET | `gen0 + gen1 + gen2` |
| Java | the sum of the **cycle-counting beans only**: `G1 Young Generation` + `G1 Old Generation`, or `ZGC Minor Cycles` + `ZGC Major Cycles` |

The Java list is an **inclusion** list (`registry.GcSemantics.cadence_beans`),
so a bean this file does not name is dropped rather than silently double-counted,
and a JVM matching none of them produces an empty cadence series — which is
`insufficient_data` and flags the probe, never a pass. Each probe's warmup
record publishes which basis its numbers came from (`gc_steady.detail.cadence_basis`).

That sum is the honest analog of .NET's generation total: it is a count of
collection cycles under both collectors, it is what `G1 Old Generation` fails to
be, and it excludes exactly the beans shown above to count something else.
Measured end to end at 1 Hz for 18 s under allocation load, it yields 17 cadence
samples on both collectors (G1 ~61 collections/s, ZGC ~60/s) and `gc_steady`
scores `measured`.

PIN: where a runtime lacks a true analog the field is still present (null, with
the reason on the wire beside it) so the required shape is identical and the
runner has one code path. A `null` here means NOT MEASURED and is never read as
zero — the delta and roll-up paths propagate it as null.

Equivalence suite checks the required SHAPE plus the `ts` format, never values.
