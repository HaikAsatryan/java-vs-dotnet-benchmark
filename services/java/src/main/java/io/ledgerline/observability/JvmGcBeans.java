package io.ledgerline.observability;

import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.jspecify.annotations.Nullable;

/**
 * The {@code /runtime-stats} GC sources, and the two rows of the frozen shape that have no honest
 * JVM value. Both are reported as a present null plus a note rather than a number, because a
 * number here would mean something different on each side and the runner reads null as
 * "not measured" (docs/research/2026-08-21-measured.md FINDING 6 / FINDING 7).
 */
final class JvmGcBeans {

    /** ZGC alone splits cycles from pauses into separate beans over the same pools. */
    private static final String PAUSE_BEAN_SUFFIX = "Pauses";

    static final String COLLECTIONS_NOTE =
            "gen0/gen1/gen2 are null on the JVM: G1's old-generation bean counts Full GC only "
            + "and reads 0 where ZGC's major-cycle bean reads 3 on identical work, and neither "
            + "matches the CLR's generations. The per-bean counters beside them are the real "
            + "figures, keyed by GarbageCollectorMXBean name.";

    static final String PAUSE_NOTE =
            "pause_total_seconds is null under ZGC: getCollectionTime() is whole milliseconds and "
            + "ZGC records every pause as 0 ms (measured: 255 pauses, 0 ms), so the field would "
            + "read as a constant 0.0 in the one cell where pause time is the variable.";

    private JvmGcBeans() {
    }

    static List<GarbageCollectorMXBean> beans() {
        return ManagementFactory.getGarbageCollectorMXBeans();
    }

    /** The frozen triple, null-filled, followed by one real counter per collector bean. */
    static Map<String, @Nullable Long> collections(List<GarbageCollectorMXBean> beans) {
        Map<String, @Nullable Long> counts = new LinkedHashMap<>();
        counts.put("gen0", null);
        counts.put("gen1", null);
        counts.put("gen2", null);
        for (GarbageCollectorMXBean bean : beans) {
            counts.put(bean.getName(), bean.getCollectionCount());
        }
        return counts;
    }

    /**
     * Under G1 every bean's time is stop-the-world time, {@code G1 Concurrent GC} included -
     * that bean reports the Remark and Cleanup sub-pauses, not concurrent wall time, so dropping
     * it would UNDERCOUNT the total. ZGC is the case with no answer and is told apart by the
     * {@code *Pauses} beans only it publishes. Both readings are in JvmGcBeansTest.
     */
    static @Nullable Double pauseTotalSeconds(List<GarbageCollectorMXBean> beans) {
        if (hasPauseBeans(beans)) {
            return null;
        }
        long totalMillis = 0;
        for (GarbageCollectorMXBean bean : beans) {
            totalMillis += bean.getCollectionTime();
        }
        return totalMillis / 1000.0;
    }

    static boolean hasPauseBeans(List<GarbageCollectorMXBean> beans) {
        return beans.stream().anyMatch(bean -> bean.getName().endsWith(PAUSE_BEAN_SUFFIX));
    }

    static boolean isZgc(List<GarbageCollectorMXBean> beans) {
        return beans.stream().anyMatch(bean -> bean.getName().startsWith("ZGC"));
    }
}
