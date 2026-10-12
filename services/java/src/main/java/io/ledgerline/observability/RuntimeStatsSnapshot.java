package io.ledgerline.observability;

import java.util.Map;
import org.jspecify.annotations.Nullable;

/** One {@code /runtime-stats} reading, taken under no lock: every source below is its own gauge. */
public record RuntimeStatsSnapshot(
        double uptimeSeconds,
        Map<String, @Nullable Long> gcCollections,
        @Nullable Double gcPauseTotalSeconds,
        long gcHeapInUseBytes,
        long gcHeapCommittedBytes,
        @Nullable Long gcHeapGoalBytes,
        long gcAllocatedTotalBytes,
        long maxHeapBytes,
        String gcEngine,
        String maxRamPercentage,
        int schedulerParallelism,
        int mountedVirtualThreads,
        int dbPoolTotal,
        int dbPoolIdle,
        int dbPoolInUse,
        int redisTotal,
        long[] endpointCalls) {
}
