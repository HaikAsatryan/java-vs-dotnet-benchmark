package io.ledgerline.logging;

import io.ledgerline.config.LedgerProperties;
import java.sql.Driver;
import java.sql.DriverManager;
import java.util.LinkedHashMap;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * The single {@code msg="config"} line the runner greps into the run manifest, field for field
 * with {@code services/dotnet/Infrastructure/EffectiveConfig.cs} plus the JVM knobs that have no
 * .NET counterpart. Every value is read from the bound configuration rather than from the
 * container env, so what is recorded is what the SUT actually used.
 */
@Component
public class EffectiveConfig {

    private static final Logger LOGGER = LoggerFactory.getLogger("io.ledgerline.Ledgerline");

    private final LedgerProperties properties;
    private final Environment environment;

    EffectiveConfig(LedgerProperties properties, Environment environment) {
        this.properties = properties;
        this.environment = environment;
    }

    @EventListener(ApplicationReadyEvent.class)
    public void logOnce() {
        LedgerLog.config(LOGGER, fields());
    }

    /**
     * PgJDBC is a runtime-scope dependency - the SUT compiles against no driver class - so the
     * version is read off the registered {@link Driver}, which is the one the pool actually
     * loaded, rather than off a compile-time constant.
     */
    private static String pgjdbcVersion() {
        return DriverManager.drivers()
                .filter(driver -> driver.getClass().getName().startsWith("org.postgresql."))
                .findFirst()
                .map(driver -> driver.getClass().getPackage().getImplementationVersion())
                .orElse("unknown");
    }

    private Map<String, Object> fields() {
        LedgerProperties.Db db = properties.db();
        Map<String, Object> fields = new LinkedHashMap<>();
        fields.put("data_layer", LedgerProperties.DATA_LAYER);
        fields.put("port", environment.getProperty("server.port", "8080"));
        fields.put("db_pool_max", db.poolMax());
        fields.put("db_pool_min", db.poolMin());
        fields.put("db_conn_lifetime_seconds", db.connLifetimeSeconds());
        fields.put("jdbc_prepare_threshold", db.prepareThreshold());
        fields.put("redis_pool_max", properties.redisPoolMax());
        fields.put("cache_ttl_seconds", properties.cacheTtlSeconds());
        // spec/logging.md pins the sink as bounded-and-lossless; these two are the values
        // logback-spring.xml gives the async appender, echoed so the manifest records them.
        fields.put("log_queue_len", properties.logQueueLen());
        fields.put("log_block_on_full", true);
        fields.put("body_max_bytes", properties.bodyMaxBytes());
        fields.put("pdf_dir", properties.pdfDir());
        fields.put("virtual_threads_enabled",
                environment.getProperty("spring.threads.virtual.enabled", boolean.class, false));
        fields.put("processor_count", Runtime.getRuntime().availableProcessors());
        fields.put("runtime_version", Runtime.version().toString());
        fields.put("pgjdbc_version", pgjdbcVersion());
        fields.put("service_version",
                environment.getProperty("SERVICE_VERSION", "dev"));
        return fields;
    }
}
