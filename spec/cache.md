# spec/cache.md — cache-aside contract (both services, frozen)

```yaml
policy: cache-aside
backend: Redis 8.x (AGPL noted in README; Valkey documented drop-in)
key: "invoice:{id}"            # base-10 invoice id, no padding
value: >
  The canonical GET /invoices/{id} JSON body: the exact UTF-8 bytes the endpoint
  returns. SERIALIZE-THEN-CACHE pin: both services serialize the response first
  and store those bytes, so a hit serves byte-equivalent output to a miss and
  the cache layer cannot introduce a hit/miss output difference.
ttl_seconds: 300               # env CACHE_TTL_SECONDS (default 300); capacity-gate lever raises it
```

## Read path (GET /invoices/{id})

1. `GET invoice:{id}`
2. hit: return the cached bytes (no DB, no re-serialization).
3. miss: Q1+Q2 from PG; absent -> 404 (NOT cached: no negative caching);
   present -> build canonical body, store it with a `<ttl>` expiry, return body.

The store is ONE command with the TTL attached on both sides, and the two
clients spell it differently. Captured from Redis `MONITOR` 2026-08-24 against
both live services:

| stack | wire text |
|---|---|
| Java / Lettuce | `"SET" "invoice:11" "<body>" "EX" "300"` |
| .NET / StackExchange.Redis | `"SETEX" "invoice:12" "300" "<body>"` |

Same operation, one round trip either way; `SETEX` is Redis's older
single-purpose form of `SET ... EX`. Recorded here because
spec/db-access.md's house rule -- every row is wire text, not a rendering --
applies to this file too.

## Write path (POST /invoices)

After commit: `DEL invoice:{new_id}`. Guards stale/negative entries; one extra
Redis RTT in the create path, identical both sides. (With no negative caching
and v1's seeded-only GET keyspace the DEL is a near-no-op, but it is the
realistic invalidation pattern and its cost belongs in the create path.)

## Server config (infra/compose, not client concerns)

```
maxmemory 1500mb
maxmemory-policy allkeys-lru
save ""            # persistence off
appendonly no
```

## Client pins

| env | default | Java (Lettuce) | .NET (StackExchange.Redis) |
|---|---|---|---|
| REDIS_ADDR | infra | `spring.data.redis.url` = `redis://<addr>` | `ConfigurationOptions.Parse(<addr>)` endpoint |
| REDIS_POOL_MAX | 16 | recorded only; sizes nothing (below) | recorded only; sizes nothing (below) |

`REDIS_POOL_MAX` binds to no pool on EITHER side, and this is a SYMMETRY, not
the asymmetry this file carried while the other slot was Go. Both clients are
shared MULTIPLEXERS over a single connection: Spring Boot's
`LettuceConnectionFactory` pools only when commons-pool2 is on the classpath and
`services/java/pom.xml` keeps it off deliberately (asserted by absence in
`BuildInvariantsTest`), and a `ConnectionMultiplexer` is StackExchange.Redis's
only production model. Both services echo the value in `/runtime-stats`
`config.redis_pool_max` and `redis_pool.max` and neither sizes anything with it:
it is a recorded knob, not a live one, and spec/db-access.md's pool table says
the same thing about the same knob.

`redis_pool.idle` is a literal 0 on both sides: a multiplexer has no idle pool
member to count. **`redis_pool.in_use` is NOT symmetric, and this file said it
was until 2026-08-24.** Java writes a literal 0
(`RuntimeStatsWriter.redisPool`). .NET writes StackExchange.Redis's
`GetCounters().TotalOutstanding` (`RuntimeStatsEndpoints.RedisCounters`), a live
count of commands in flight at the instant of the poll -- a gauge Lettuce
publishes no analog for. Measured 2026-08-24 under 40-way concurrent
`GET /invoices/{id}` with `/runtime-stats` polled at 10 Hz: Java peaked at
`in_use` 0, .NET at 39; both reported `total` 1 and `idle` 0 throughout.
Nothing in the harness compares `/runtime-stats` values across services -- the
resource numbers come from cgroup v2 and this endpoint is corroboration -- so no
published number moves. It is a `docs/FAIRNESS.md` row because a reader holding
two `/runtime-stats` bodies side by side would otherwise read the gap as a
pooling difference, and it is not one (spec/runtime-stats.md).

Neither client enables client-side caching or
server-assisted tracking: the .NET options object sets only `AbortOnConnectFail`
and the `UNLINK` command-map removal, and the Java side is Boot's stock factory
with serializers replaced and nothing else. Redis-side utilization is reported
from Redis's own cgroup.

## Hit rate

Target ~0.80 by construction (Zipf alpha=1.0 over the invoice keyspace + TTL
300 s). Measured, RECORDED AND REPORTED beside that target -- not asserted
against it. `manifest.cache_stats` carries the Redis INFO keyspace_hits/misses
delta summed over the measured windows plus the constructed target it is to be
read against, the analysis prints the realized rate (or an explicit
not-measured disclosure), and the capacity re-gate projects DB load from the
MEASURED rate rather than the constructed one. No gate compares the two: a
realized rate that misses 0.80 is a disclosure, not a failure, because the
construction is a workload property and the headline does not rest on it.
Capacity-gate lever 2 raises the target to 0.90
via CACHE_TTL_SECONDS (and, if needed, hot-set concentration), applied
identically to both languages and recorded.
