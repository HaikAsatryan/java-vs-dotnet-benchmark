package io.ledgerline.observability;

import io.ledgerline.config.LedgerProperties;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import org.jspecify.annotations.Nullable;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * The frozen {@code /runtime-stats} body of spec/runtime-stats.md - identical SHAPE to
 * {@code services/dotnet/Observability/RuntimeStatsWriter.cs}, runtime-specific values, and a
 * present-but-null field wherever the JVM has no honest analog.
 *
 * <p>A {@code LinkedHashMap} rather than a record per block: the frozen key set includes keys
 * that belong to another runtime, and map keys bypass the global SNAKE_CASE strategy, so what
 * this class writes is literally what goes on the wire.
 */
@Component
public class RuntimeStatsWriter {

    private final LedgerProperties properties;
    private final boolean virtualThreadsEnabled;
    private final String serviceVersion;

    RuntimeStatsWriter(LedgerProperties properties, Environment environment) {
        this.properties = properties;
        this.serviceVersion = environment.getProperty("SERVICE_VERSION", "dev");
        // The echoed KNOB, as the manifest records it. That it took effect is a different claim
        // and is asserted off the running connector by ServerPostureTest.
        this.virtualThreadsEnabled =
                environment.getProperty("spring.threads.virtual.enabled", boolean.class, false);
    }

    public Map<String, Object> write(RuntimeStatsSnapshot snapshot) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ts", Instant.now());
        body.put("uptime_seconds", snapshot.uptimeSeconds());
        body.put("build", build());
        body.put("config", config(snapshot));
        body.put("gc", gc(snapshot));
        body.put("db_pool", dbPool(snapshot));
        body.put("redis_pool", redisPool(snapshot));
        // Mounted virtual threads. Not the same QUANTITY as .NET's thread-pool size, which is
        // why it reads 1 at idle where .NET reads 5; disclosed in docs/FAIRNESS.md rather than
        // reshaped, because the frozen field means a different thing per runtime by construction.
        body.put("goroutines_or_threadpool", snapshot.mountedVirtualThreads());
        body.put("endpoint_calls", endpointCalls(snapshot));
        return body;
    }

    private Map<String, Object> build() {
        Map<String, Object> build = new LinkedHashMap<>();
        build.put("language", "java");
        build.put("runtime_version", Runtime.version().toString());
        build.put("service_version", serviceVersion);
        build.put("data_layer", LedgerProperties.DATA_LAYER);
        build.put("processor_count", Runtime.getRuntime().availableProcessors());
        return build;
    }

    private Map<String, @Nullable Object> config(RuntimeStatsSnapshot snapshot) {
        LedgerProperties.Db db = properties.db();
        Map<String, @Nullable Object> config = new LinkedHashMap<>();
        config.put("db_pool_max", db.poolMax());
        config.put("db_pool_min", db.poolMin());
        config.put("db_conn_lifetime_seconds", db.connLifetimeSeconds());
        config.put("redis_pool_max", properties.redisPoolMax());
        config.put("cache_ttl_seconds", properties.cacheTtlSeconds());
        config.put("log_queue_len", properties.logQueueLen());
        // The frozen knobs that belong to another runtime. The behavioural counterpart of
        // max_auto_prepare on this side is jdbc_prepare_threshold: both stacks promote a
        // statement on its 5th execution (spec/db-access.md).
        config.put("max_auto_prepare", null);
        config.put("gogc", null);
        config.put("gomemlimit", null);
        config.put("gc_dynamic_adaptation_mode", null);
        config.put("jdbc_prepare_threshold", db.prepareThreshold());
        config.put("gc_engine", snapshot.gcEngine());
        config.put("max_ram_percentage", snapshot.maxRamPercentage());
        config.put("max_heap_bytes", snapshot.maxHeapBytes());
        config.put("virtual_threads_enabled", virtualThreadsEnabled);
        config.put("vt_scheduler_parallelism", snapshot.schedulerParallelism());
        return config;
    }

    private static Map<String, @Nullable Object> gc(RuntimeStatsSnapshot snapshot) {
        Map<String, @Nullable Object> gc = new LinkedHashMap<>();
        gc.put("collections", snapshot.gcCollections());
        gc.put("collections_note", JvmGcBeans.COLLECTIONS_NOTE);
        gc.put("pause_total_seconds", snapshot.gcPauseTotalSeconds());
        if (snapshot.gcPauseTotalSeconds() == null) {
            gc.put("pause_total_seconds_note", JvmGcBeans.PAUSE_NOTE);
        }
        gc.put("heap_in_use_bytes", snapshot.gcHeapInUseBytes());
        gc.put("heap_committed_bytes", snapshot.gcHeapCommittedBytes());
        gc.put("heap_goal_bytes", snapshot.gcHeapGoalBytes());
        gc.put("allocated_total_bytes", snapshot.gcAllocatedTotalBytes());
        return gc;
    }

    private Map<String, @Nullable Object> dbPool(RuntimeStatsSnapshot snapshot) {
        Map<String, @Nullable Object> pool = new LinkedHashMap<>();
        pool.put("max", properties.db().poolMax());
        pool.put("total", snapshot.dbPoolTotal());
        pool.put("idle", snapshot.dbPoolIdle());
        pool.put("in_use", snapshot.dbPoolInUse());
        // HikariPoolMXBean exposes four gauges and no cumulative counters; the three below need a
        // MetricsTrackerFactory, i.e. per-acquire timing work Npgsql's side never pays. Null on
        // both stacks is the symmetric answer.
        pool.put("acquire_count", null);
        pool.put("empty_acquire_count", null);
        pool.put("acquire_wait_total_seconds", null);
        return pool;
    }

    private Map<String, Object> redisPool(RuntimeStatsSnapshot snapshot) {
        Map<String, Object> pool = new LinkedHashMap<>();
        pool.put("max", properties.redisPoolMax());
        // Lettuce's default is ONE shared multiplexed connection, the SE.Redis model
        // (spec/cache.md), so total is 0 or 1 and there is nothing idle or in use to report.
        pool.put("total", snapshot.redisTotal());
        pool.put("idle", 0);
        pool.put("in_use", 0);
        return pool;
    }

    private static Map<String, Object> endpointCalls(RuntimeStatsSnapshot snapshot) {
        Map<String, Object> calls = new LinkedHashMap<>();
        for (EndpointKind kind : EndpointKind.values()) {
            calls.put(kind.route(), snapshot.endpointCalls()[kind.ordinal()]);
        }
        return calls;
    }
}
