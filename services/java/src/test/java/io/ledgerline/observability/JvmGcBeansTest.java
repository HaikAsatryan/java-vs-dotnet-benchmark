package io.ledgerline.observability;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.entry;

import java.lang.management.GarbageCollectorMXBean;
import java.util.List;
import javax.management.ObjectName;
import org.junit.jupiter.api.Test;

/**
 * The two {@code /runtime-stats} GC rows whose mapping is a judgement call, pinned against
 * COLLECTOR-SHAPED bean lists rather than whichever collector happens to be running the suite.
 * Both judgements were reached by measurement and both are easy to "tidy" in the wrong
 * direction, which is what these assertions exist to stop.
 */
class JvmGcBeansTest {

    private static final List<GarbageCollectorMXBean> G1 = List.of(
            new FakeBean("G1 Young Generation", 50, 43),
            new FakeBean("G1 Concurrent GC", 60, 3),
            new FakeBean("G1 Old Generation", 0, 0));

    private static final List<GarbageCollectorMXBean> ZGC = List.of(
            new FakeBean("ZGC Minor Cycles", 1, 5),
            new FakeBean("ZGC Minor Pauses", 3, 0),
            new FakeBean("ZGC Major Cycles", 33, 77),
            new FakeBean("ZGC Major Pauses", 255, 0));

    @Test
    void underG1TheConcurrentBeanIsPartOfThePauseTotal() {
        // The counts above are a real reading (Temurin 25.0.4, 30 forced concurrent cycles,
        // -XX:+ExplicitGCInvokesConcurrent). `G1 Concurrent GC` looks like wall time and is not:
        // its 60 events are the Remark and Cleanup STW sub-pauses of 30 cycles, and its 3 ms
        // matches the 3.42 + 0.10 ms those pauses report in -Xlog:gc, against 40.8 ms of
        // concurrent marking in the same run. Dropping the bean would UNDERCOUNT the pause total.
        assertThat(JvmGcBeans.pauseTotalSeconds(G1)).isEqualTo(0.046);
    }

    @Test
    void underZgcThereIsNoPauseTotalToReport() {
        // getCollectionTime() is whole milliseconds and ZGC reports every pause as 0: measured,
        // 255 major pauses -> 0 ms. Summing the *Cycles beans instead would report concurrent
        // wall time under a field named pause_total_seconds, so the honest answer is null.
        assertThat(JvmGcBeans.pauseTotalSeconds(ZGC)).isNull();
    }

    @Test
    void theFrozenGenerationTripleIsNullAndTheRealCountersSitBesideIt() {
        assertThat(JvmGcBeans.collections(G1))
                .containsEntry("gen0", null)
                .containsEntry("gen1", null)
                .containsEntry("gen2", null)
                .contains(entry("G1 Young Generation", 50L), entry("G1 Old Generation", 0L));
    }

    @Test
    void theCollectorIsIdentifiedByBeanNameNotByFlag() {
        assertThat(JvmGcBeans.isZgc(ZGC)).isTrue();
        assertThat(JvmGcBeans.isZgc(G1)).isFalse();
    }

    private record FakeBean(String name, long count, long millis) implements GarbageCollectorMXBean {

        @Override
        public String getName() {
            return name;
        }

        @Override
        public long getCollectionCount() {
            return count;
        }

        @Override
        public long getCollectionTime() {
            return millis;
        }

        @Override
        public boolean isValid() {
            return true;
        }

        @Override
        public String[] getMemoryPoolNames() {
            return new String[0];
        }

        @Override
        public ObjectName getObjectName() {
            throw new UnsupportedOperationException();
        }
    }
}
