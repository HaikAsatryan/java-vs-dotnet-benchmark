package io.ledgerline.config;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.PositiveOrZero;
import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

/**
 * Effective configuration, mirroring {@code LedgerConfig.cs} field for field and default for
 * default; the env-var-to-property mapping lives in {@code application.yaml}. PORT is deliberately
 * absent - it binds to Boot's {@code server.port}, and a second field holding the same value would
 * be a second source of truth for what the app binds.
 */
@ConfigurationProperties("ledger")
@Validated
public record LedgerProperties(
        @Valid Pg pg,
        @Valid Db db,
        // Recorded and echoed only: Lettuce and SE.Redis both default to one shared multiplexed
        // connection, so neither side has a pool size to set, but the knob stays on the shared
        // env surface so both manifests record the same declared value.
        @Positive int redisPoolMax,
        @Positive int cacheTtlSeconds,
        @Positive int logQueueLen,
        @Positive long bodyMaxBytes,
        @NotBlank String pdfDir) {

    /**
     * Echoed as {@code /runtime-stats} build.data_layer. A constant, not an env var:
     * PROGRESS.md D5 gives the Java side one data layer, so a DATA_LAYER knob would declare a
     * variant axis that does not exist.
     */
    public static final String DATA_LAYER = "jpa";

    public Duration cacheTtl() {
        return Duration.ofSeconds(cacheTtlSeconds);
    }

    public record Pg(
            @NotBlank String host,
            @Positive int port,
            @NotBlank String database,
            @NotBlank String user,
            @NotBlank String password) {
    }

    public record Db(
            @Positive int poolMax,
            @Positive int poolMin,
            @Positive int connLifetimeSeconds,
            // The behavioural counterpart of Npgsql's AUTO_PREPARE_MIN_USAGES (spec/db-access.md):
            // both stacks promote a statement on its 5th execution. Defaulted to PgJDBC's own 5,
            // and bound rather than ignored so the value the manifest records is one the SUT saw.
            @PositiveOrZero int prepareThreshold,
            boolean poolWarmOnStart) {

        public Db {
            // min=max is POLICY (spec/db-access.md): the two pools idle-prune on different
            // schedules, so a min below max shrinks them differently mid-run. LedgerConfig.cs
            // refuses to start on the same mismatch with the same message.
            if (poolMin != poolMax) {
                throw new IllegalArgumentException(
                        "DB_POOL_MIN=" + poolMin + " != DB_POOL_MAX=" + poolMax
                                + " (min=max policy, spec/db-access.md)");
            }
        }

        /**
         * DB_CONN_LIFETIME is SECONDS (LedgerConfig.cs feeds it to Npgsql's ConnectionLifetime,
         * which is seconds); HikariCP's maxLifetime is MILLISECONDS. A 1:1 copy gives
         * 3.6-second connections and a reconnect storm that reads as a Java runtime problem.
         */
        public long connLifetimeMillis() {
            return connLifetimeSeconds * 1000L;
        }
    }
}
