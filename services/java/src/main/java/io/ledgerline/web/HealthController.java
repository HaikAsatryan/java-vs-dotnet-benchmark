package io.ledgerline.web;

import com.zaxxer.hikari.HikariDataSource;
import io.ledgerline.cache.InvoiceCache;
import io.ledgerline.dto.HealthResponse;
import io.ledgerline.observability.EndpointKind;
import io.ledgerline.observability.RuntimeStatsCollector;
import java.sql.Connection;
import java.sql.SQLException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
class HealthController {

    private final HikariDataSource dataSource;
    private final InvoiceCache cache;
    private final RuntimeStatsCollector stats;

    HealthController(HikariDataSource dataSource, InvoiceCache cache,
            RuntimeStatsCollector stats) {
        this.dataSource = dataSource;
        this.cache = cache;
        this.stats = stats;
    }

    /**
     * The HTTP floor: a static body, no DB and no Redis. Excluded from the access log and
     * counted in {@code endpoint_calls}, because the per-endpoint warmup gate reads it.
     */
    @GetMapping("/healthz")
    HealthResponse healthz() {
        stats.recordEndpointCall(EndpointKind.HEALTHZ);
        return HealthResponse.OK;
    }

    /**
     * DB first, Redis only if the DB answered: the same short-circuit as {@code /readyz} on .NET.
     */
    @GetMapping("/readyz")
    ResponseEntity<HealthResponse> readyz() {
        return databaseReachable() && cache.ping()
                ? ResponseEntity.ok(HealthResponse.OK)
                : ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                        .body(HealthResponse.UNAVAILABLE);
    }

    private boolean databaseReachable() {
        try (Connection connection = dataSource.getConnection()) {
            return connection.isValid(1);
        } catch (SQLException e) {
            return false;
        }
    }
}
