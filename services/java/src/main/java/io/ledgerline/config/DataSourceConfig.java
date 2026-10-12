package io.ledgerline.config;

import com.zaxxer.hikari.HikariDataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * The datasource, built from {@link LedgerProperties} rather than {@code spring.datasource.*} so
 * the shared pool env surface (spec/db-access.md) becomes driver settings in one place - the
 * mirror of {@code NpgsqlConnectionString.cs}.
 */
@Configuration(proxyBeanMethods = false)
class DataSourceConfig {

    // Declared as HikariDataSource rather than DataSource: /runtime-stats reads db_pool.total
    // and db_pool.idle straight off getHikariPoolMXBean(), which needs no JMX registration.
    @Bean
    HikariDataSource dataSource(LedgerProperties props) throws SQLException {
        LedgerProperties.Pg pg = props.pg();
        LedgerProperties.Db db = props.db();

        // Setters on a bare HikariDataSource, not new HikariDataSource(HikariConfig): that
        // constructor builds the pool and opens its first connection immediately, before
        // DB_POOL_WARM_ON_START has a say, and the test hosts have no database.
        HikariDataSource hikari = new HikariDataSource();
        // No query string on the URL: PgJDBC silently gives URL parameters priority over the
        // properties object, so every driver knob is set in exactly one place.
        hikari.setJdbcUrl("jdbc:postgresql://" + pg.host() + ":" + pg.port() + "/" + pg.database());
        hikari.setUsername(pg.user());
        hikari.setPassword(pg.password());
        hikari.setPoolName("ledgerline");

        hikari.setMaximumPoolSize(db.poolMax());
        hikari.setMinimumIdle(db.poolMin());
        hikari.setMaxLifetime(db.connLifetimeMillis());

        // PgJDBC's own default is 5 and spec/db-access.md's baseline leaves it there; written
        // explicitly because a value the manifest records but the SUT never saw is the failure
        // to avoid.
        hikari.addDataSourceProperty("prepareThreshold", String.valueOf(db.prepareThreshold()));

        if (db.poolWarmOnStart()) {
            warm(hikari, db.poolMax());
        }
        return hikari;
    }

    /**
     * Opens all {@code DB_POOL_MAX} connections before traffic starts, mirroring the warm loop in
     * {@code Program.cs}: HikariCP at min=max fills on demand, so the warmup gate
     * ({@code db_pool.total == max}) would otherwise be met on a different schedule than .NET's.
     * In the {@code @Bean} method, not an {@code ApplicationRunner}, so it precedes the connector.
     *
     * <p>CONCURRENTLY, matching {@code Program.cs}'s {@code Task.WhenAll}. Sequential also
     * satisfies the gate, but it serialises 24 handshakes into time-to-ready and the cold-start
     * curve is a published result (measured: 277/75/74 ms sequential against 18/11/11 ms).
     */
    private static void warm(HikariDataSource dataSource, int size) throws SQLException {
        List<Connection> opened = new ArrayList<>(size);
        try (ExecutorService openers = Executors.newVirtualThreadPerTaskExecutor()) {
            List<Future<Connection>> pending = new ArrayList<>(size);
            for (int i = 0; i < size; i++) {
                pending.add(openers.submit((Callable<Connection>) dataSource::getConnection));
            }
            for (Future<Connection> future : pending) {
                opened.add(future.get());
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("interrupted while warming the pool", e);
        } catch (ExecutionException e) {
            throw new IllegalStateException("pool warm failed", e.getCause());
        } finally {
            for (Connection connection : opened) {
                connection.close();
            }
        }
    }
}
