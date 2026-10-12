package io.ledgerline.web;

import static org.assertj.core.api.Assertions.assertThat;

import java.lang.management.ManagementFactory;
import java.nio.file.Path;
import java.util.concurrent.Executor;
import java.util.concurrent.TimeUnit;
import org.apache.tomcat.util.threads.VirtualThreadExecutor;
import org.apache.coyote.http11.AbstractHttp11Protocol;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.thread.Threading;
import org.springframework.boot.tomcat.TomcatWebServer;
import org.springframework.boot.web.server.context.WebServerApplicationContext;
import org.springframework.core.env.Environment;

/**
 * D2 and the HTTP-surface dials, read back off the RUNNING connector. The property that
 * switches virtual threads on cannot be inferred from the JDK version and does not fail
 * anything when it is missing - the service simply runs on a platform-thread pool and the
 * headline compares the wrong two concurrency models.
 */
@SpringBootTest(
        webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {
            // A real connector, still no database: the same DB-less posture MockWebTest sets up.
            "spring.jpa.properties.hibernate.boot.allow_jdbc_metadata_access=false",
            "spring.jpa.properties.hibernate.dialect=org.hibernate.dialect.PostgreSQLDialect",
        })
class ServerPostureTest {

    @Autowired
    private WebServerApplicationContext context;

    @Autowired
    private Environment environment;

    @Test
    void requestsAreServedOnVirtualThreads() {
        // Boot 4.1.1: the property is the only trigger, and for this service its one live effect
        // is the connector's executor - so the executor type is the assertion, not the property.
        assertThat(Threading.VIRTUAL.isActive(environment)).isTrue();
        assertThat(connectorExecutor()).isInstanceOf(VirtualThreadExecutor.class);
    }

    @Test
    void thereIsNoPerConnectionRequestCap() {
        // Tomcat's default is 100: a keep-alive generator would then pay a fresh connection
        // every 100 requests, churn that grows with the offered rate and has no Kestrel analog.
        assertThat(protocol().getMaxKeepAliveRequests()).isEqualTo(-1);
    }

    @Test
    void theConnectorTimeoutsMatchTheKestrelLimitsOnTheOtherSide() {
        assertThat(protocol().getConnectionTimeout()).isEqualTo(5_000);
        assertThat(protocol().getKeepAliveTimeout()).isEqualTo(120_000);
    }

    @Test
    void noFlightRecordingIsRunning() {
        // JFR overhead lands inside the measured window, and /runtime-stats is sourced from
        // management beans precisely so that no recording is ever needed. isInitialized() is
        // read rather than getFlightRecorder(), which would start JFR to answer the question.
        assertThat(jdk.jfr.FlightRecorder.isInitialized()).isFalse();
        assertThat(ManagementFactory.getRuntimeMXBean().getInputArguments())
                .noneMatch(argument -> argument.contains("StartFlightRecording"));
    }

    @Test
    void theParallelismPinReachesAvailableProcessors() throws Exception {
        // The runner pins per-cell parallelism with -XX:ActiveProcessorCount (registry.py's
        // JAVA.parallelism_template) and then asserts /runtime-stats build.processor_count
        // against SUT_CORES. HotSpotDiagnosticMXBean reads the flag back as -1 {DEFAULT} when
        // it is not passed (docs/research/2026-08-21-critique.md B4), so this asserts the
        // thing that can actually fail: that a JVM started with the flag reports it from
        // availableProcessors(), which is what this process's own /runtime-stats echoes.
        assertThat(Runtime.getRuntime().availableProcessors())
                .isEqualTo(coresSeenBy("-XX:ActiveProcessorCount=" + hostCores()));
        assertThat(coresSeenBy("-XX:ActiveProcessorCount=1")).isEqualTo(1);
    }

    private static int hostCores() {
        return Runtime.getRuntime().availableProcessors();
    }

    /** {@code availableProcessors()} inside a fresh JVM started with one extra flag. */
    private static int coresSeenBy(String flag) throws Exception {
        Path java = Path.of(System.getProperty("java.home"), "bin", "java");
        Process process = new ProcessBuilder(java.toString(), flag,
                "-cp", System.getProperty("java.class.path"), Cores.class.getName())
                .redirectErrorStream(true)
                .start();
        String output = new String(process.getInputStream().readAllBytes()).trim();
        assertThat(process.waitFor(60, TimeUnit.SECONDS)).isTrue();
        assertThat(process.exitValue()).as("child JVM said: %s", output).isZero();
        return Integer.parseInt(output);
    }

    public static final class Cores {
        public static void main(String[] args) {
            System.out.print(Runtime.getRuntime().availableProcessors());
        }
    }

    private Executor connectorExecutor() {
        return protocol().getExecutor();
    }

    private AbstractHttp11Protocol<?> protocol() {
        TomcatWebServer server = (TomcatWebServer) context.getWebServer();
        return (AbstractHttp11Protocol<?>) server.getTomcat().getConnector().getProtocolHandler();
    }
}
