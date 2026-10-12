package io.ledgerline.observability;

import com.sun.management.HotSpotDiagnosticMXBean;
import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.HikariPoolMXBean;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.lang.management.RuntimeMXBean;
import java.util.List;
import java.util.concurrent.atomic.AtomicLongArray;
import jdk.management.VirtualThreadSchedulerMXBean;
import org.jspecify.annotations.Nullable;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.stereotype.Component;

/**
 * The {@code /runtime-stats} sources, all of them {@code java.lang.management} /
 * {@code com.sun.management} / {@code jdk.management} beans. NEVER JFR: a recording's overhead
 * lands inside the measured window, and the runner polls this endpoint at 1 Hz through it.
 *
 * <p>Every bean below is a live gauge read on demand, so unlike
 * {@code RuntimeStatsCollector.cs} - which must drive a MeterListener to make observable
 * instruments emit at all - there is nothing to start and nothing to serialize between polls.
 */
@Component
public class RuntimeStatsCollector {

    private final RuntimeMXBean runtime = ManagementFactory.getRuntimeMXBean();
    private final MemoryMXBean memory = ManagementFactory.getMemoryMXBean();
    // Fully qualified to keep the two ThreadMXBean types apart; the com.sun subtype is the one
    // with the allocation counter.
    private final com.sun.management.ThreadMXBean threads =
            (com.sun.management.ThreadMXBean) ManagementFactory.getThreadMXBean();
    private final HotSpotDiagnosticMXBean hotspot =
            ManagementFactory.getPlatformMXBean(HotSpotDiagnosticMXBean.class);
    private final VirtualThreadSchedulerMXBean scheduler =
            ManagementFactory.getPlatformMXBean(VirtualThreadSchedulerMXBean.class);
    private final List<GarbageCollectorMXBean> gcBeans = JvmGcBeans.beans();

    // spec/runtime-stats.md pins the mechanism: "counters are process-lifetime totals (plain
    // atomic increments)". Not a LongAdder, whose read is a sum over cells.
    private final AtomicLongArray endpointCalls =
            new AtomicLongArray(EndpointKind.values().length);

    private final HikariDataSource dataSource;
    private final @Nullable LettuceConnectionFactory redis;

    RuntimeStatsCollector(HikariDataSource dataSource, RedisConnectionFactory redis) {
        this.dataSource = dataSource;
        this.redis = redis instanceof LettuceConnectionFactory lettuce ? lettuce : null;
    }

    public void recordEndpointCall(EndpointKind kind) {
        endpointCalls.incrementAndGet(kind.ordinal());
    }

    public RuntimeStatsSnapshot snapshot() {
        MemoryUsage heap = memory.getHeapMemoryUsage();
        boolean zgc = JvmGcBeans.isZgc(gcBeans);
        HikariPoolMXBean pool = dataSource.getHikariPoolMXBean();

        long[] calls = new long[endpointCalls.length()];
        for (int i = 0; i < calls.length; i++) {
            calls[i] = endpointCalls.get(i);
        }

        return new RuntimeStatsSnapshot(
                runtime.getUptime() / 1000.0,
                JvmGcBeans.collections(gcBeans),
                JvmGcBeans.pauseTotalSeconds(gcBeans),
                heap.getUsed(),
                heap.getCommitted(),
                // gc.heap_goal_bytes wants the collector's TARGET. SoftMaxHeapSize is that under
                // ZGC; under G1 it reads back equal to MaxHeapSize, a ceiling, so it is null there.
                zgc ? Long.parseLong(vmOption("SoftMaxHeapSize")) : null,
                threads.getTotalThreadAllocatedBytes(),
                heap.getMax(),
                gcEngine(),
                vmOption("MaxRAMPercentage"),
                scheduler.getParallelism(),
                scheduler.getMountedVirtualThreadCount(),
                pool == null ? 0 : pool.getTotalConnections(),
                pool == null ? 0 : pool.getIdleConnections(),
                pool == null ? 0 : pool.getActiveConnections(),
                redis != null && redis.getNativeClient() != null ? 1 : 0,
                calls);
    }

    /** The bean names, not the {@code UseXGC} flags: ergonomics can pick a collector nobody set. */
    private String gcEngine() {
        return gcBeans.isEmpty() ? "unknown" : gcBeans.getFirst().getName().split(" ")[0];
    }

    private String vmOption(String name) {
        return hotspot.getVMOption(name).getValue();
    }
}
