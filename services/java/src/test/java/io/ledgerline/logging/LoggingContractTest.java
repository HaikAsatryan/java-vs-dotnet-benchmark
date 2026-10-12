package io.ledgerline.logging;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.AsyncAppender;
import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.LoggingEvent;
import ch.qos.logback.core.Appender;
import ch.qos.logback.core.ConsoleAppender;
import ch.qos.logback.core.UnsynchronizedAppenderBase;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;
import java.util.stream.IntStream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.slf4j.LoggerFactory;
import org.springframework.boot.logging.logback.StructuredLogEncoder;
import org.springframework.boot.test.context.SpringBootTest;

/**
 * spec/logging.md's async sink contract, asserted off the RUNNING appender rather than off
 * logback-spring.xml, because three of AsyncAppender's defaults break the contract silently and
 * a typo in the XML is exactly how the file would stop taking effect.
 *
 * <p>The discarding one is the dangerous one: {@code discardingThreshold} resolves to
 * {@code queueSize / 5} and {@code isDiscardable()} is true for {@code level <= INFO}, and every
 * {@code http_access} line is INFO - so at the default the access log silently loses lines once
 * the queue is 80% full, i.e. precisely at the offered rates the ladder is climbing toward.
 *
 * <p>Boot, not plain logback, because {@code logback-spring.xml} is a name only Boot's
 * LogbackLoggingSystem looks for; a bare JUnit run would configure logback from its own defaults
 * and assert nothing about what the SUT ships.
 */
@SpringBootTest(
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
            "ledger.db.pool-warm-on-start=false",
            "spring.jpa.properties.hibernate.boot.allow_jdbc_metadata_access=false",
            "spring.jpa.properties.jakarta.persistence.database-product-name=PostgreSQL",
            "spring.jpa.properties.jakarta.persistence.database-product-version=18.4",
        })
class LoggingContractTest {

    private static final LoggerContext CONTEXT = (LoggerContext) LoggerFactory.getILoggerFactory();

    @Test
    void stdoutIsFedThroughOneBoundedAsyncAppenderAndNothingElse() {
        assertThat(rootAppenders()).singleElement().isInstanceOf(AsyncAppender.class);
    }

    @Test
    void theQueueIsBoundedAtLogQueueLenAndBlocksInsteadOfDropping() {
        AsyncAppender async = async();

        assertThat(async.getQueueSize()).isEqualTo(8192);
        assertThat(async.getDiscardingThreshold()).isZero();
        assertThat(async.isNeverBlock()).isFalse();
    }

    @Test
    void theShutdownDrainIsNotCappedAtLogbackSecond() {
        // spec/logging.md: on SIGTERM, drain the queue FULLY and then exit. maxFlushTime is the
        // budget AsyncAppenderBase.stop() gives that drain, and its default is 1000 ms.
        assertThat(async().getMaxFlushTime()).isGreaterThanOrEqualTo(60_000);
    }

    @Test
    void theSinkBlocksInsteadOfDroppingAndStopDrainsWhatIsQueued() throws Exception {
        // The BEHAVIOUR behind the four settings above, which are the part that fails silently:
        // spec/logging.md pins a lossless bounded queue and a shutdown that drains it FULLY. A
        // mirror of the shipped appender is driven here rather than the live one, because the
        // assertion is what stop() does and stopping the live sink would take stdout with it.
        AsyncAppender shipped = async();
        AsyncAppender mirror = new AsyncAppender();
        mirror.setContext(CONTEXT);
        mirror.setName("mirror");
        mirror.setQueueSize(shipped.getQueueSize());
        mirror.setDiscardingThreshold(shipped.getDiscardingThreshold());
        mirror.setNeverBlock(shipped.isNeverBlock());
        mirror.setMaxFlushTime(shipped.getMaxFlushTime());

        CountDownLatch held = new CountDownLatch(1);
        BlockingSink sink = new BlockingSink(held);
        sink.setContext(CONTEXT);
        sink.start();
        mirror.addAppender(sink);
        mirror.start();

        // Twice the queue: the producer CANNOT finish without the consumer, so a run that
        // completes proves it blocked, and a queue that discarded would lose the difference.
        int offered = shipped.getQueueSize() * 2;
        Thread producer = Thread.ofPlatform().start(() -> {
            for (int i = 0; i < offered; i++) {
                mirror.doAppend(event(String.valueOf(i)));
            }
        });
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
        while (mirror.getNumberOfElementsInQueue() < shipped.getQueueSize()) {
            assertThat(System.nanoTime()).as("queue never filled").isLessThan(deadline);
            Thread.onSpinWait();
        }
        held.countDown();
        producer.join(TimeUnit.SECONDS.toMillis(60));

        mirror.stop();

        assertThat(sink.received).hasSize(offered);
        assertThat(sink.received.stream().map(ILoggingEvent::getFormattedMessage))
                .containsExactlyElementsOf(
                        IntStream.range(0, offered).mapToObj(String::valueOf).toList());
    }

    @Test
    void callerDataIsOffSoNoLineWalksTheStack() {
        assertThat(async().isIncludeCallerData()).isFalse();
    }

    @Test
    void theOnlyDownstreamAppenderIsTheConsoleWithTheFrozenFormatter() {
        List<Appender<ILoggingEvent>> downstream = new ArrayList<>();
        async().iteratorForAppenders().forEachRemaining(downstream::add);

        assertThat(downstream).singleElement().isInstanceOf(ConsoleAppender.class);
        ConsoleAppender<ILoggingEvent> console = (ConsoleAppender<ILoggingEvent>) downstream.getFirst();
        assertThat(console.getEncoder()).isInstanceOf(StructuredLogEncoder.class);
        // The encoder resolves its <format> to a formatter instance at start(); an unknown class
        // name fails the context, so reaching a started encoder is the assertion.
        assertThat(console.isStarted()).isTrue();
    }

    /**
     * The counterpart of Program.cs's three AddFilter(..., Warning) calls. Every one of these
     * roots emits Information-level lines per request or per query when left at the root floor,
     * through the same bounded queue the measured traffic uses.
     */
    @ParameterizedTest
    @ValueSource(strings = {"org.springframework", "org.hibernate", "org.apache",
                            "com.zaxxer.hikari", "io.lettuce"})
    void frameworkCategoriesAreFlooredAtWarn(String category) {
        assertThat(CONTEXT.getLogger(category).getEffectiveLevel().isGreaterOrEqual(Level.WARN))
                .isTrue();
    }

    @Test
    void theOneFrameworkCategoryThatWritesAtWarnDuringTrafficIsSilenced() {
        // Measured against the running SUT: an unmatched path put
        // {"level":"warn","msg":"No mapping for GET /zzz-unmatched"} on stdout and a wrong method
        // put {"level":"warn","msg":"Request method 'DELETE' is not supported"}. Both write free
        // text into `msg`, which spec/logging.md freezes as the event NAME, and the first carries
        // a raw URL into a stream pinned to route-template cardinality. .NET emits neither.
        assertThat(CONTEXT.getLogger("org.springframework.web.servlet.PageNotFound")
                .getEffectiveLevel().isGreaterOrEqual(Level.ERROR)).isTrue();
    }

    @Test
    void theApplicationsOwnLoggersStayAtInfo() {
        assertThat(CONTEXT.getLogger("io.ledgerline").getEffectiveLevel()).isEqualTo(Level.INFO);
    }

    private static ILoggingEvent event(String message) {
        return new LoggingEvent(LoggingContractTest.class.getName(),
                CONTEXT.getLogger("io.ledgerline.test"), Level.INFO, message, null, null);
    }

    /** Refuses to consume until released, so the queue in front of it has to fill. */
    private static final class BlockingSink extends UnsynchronizedAppenderBase<ILoggingEvent> {

        private final ConcurrentLinkedQueue<ILoggingEvent> received = new ConcurrentLinkedQueue<>();
        private final CountDownLatch held;

        private BlockingSink(CountDownLatch held) {
            this.held = held;
        }

        @Override
        protected void append(ILoggingEvent event) {
            // parkNanos, not await(): AsyncAppenderBase.stop() interrupts its worker before the
            // drain, and a sink that threw on that interrupt would abandon the queue and make
            // this test pass or fail on the interrupt rather than on the drain.
            while (held.getCount() > 0) {
                LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(1));
            }
            received.add(event);
        }
    }

    private static AsyncAppender async() {
        return (AsyncAppender) rootAppenders().getFirst();
    }

    private static List<Appender<ILoggingEvent>> rootAppenders() {
        List<Appender<ILoggingEvent>> appenders = new ArrayList<>();
        Iterator<Appender<ILoggingEvent>> iterator =
                CONTEXT.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME).iteratorForAppenders();
        iterator.forEachRemaining(appenders::add);
        return appenders;
    }
}
