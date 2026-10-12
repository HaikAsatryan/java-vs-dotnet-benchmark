package io.ledgerline.config;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.HashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.boot.context.properties.bind.BindException;
import org.springframework.boot.context.properties.bind.Binder;
import org.springframework.boot.env.YamlPropertySourceLoader;
import org.springframework.core.env.MapPropertySource;
import org.springframework.core.io.ClassPathResource;
import org.springframework.mock.env.MockEnvironment;

/**
 * Binds the shipped {@code application.yaml} placeholders through the real environment, and
 * asserts the resolved values are the same defaults {@code services/dotnet/Config/LedgerConfig.cs}
 * uses. The env-var surface is the one thing the two services genuinely share, so a divergence
 * here is a cross-language config divergence inside a measured run.
 */
class LedgerPropertiesBindingTest {

    @Test
    void defaultsMatchTheDotnetDefaults() {
        LedgerProperties props = bind(Map.of());

        assertThat(props.pg().host()).isEqualTo("localhost");
        assertThat(props.pg().port()).isEqualTo(5432);
        assertThat(props.pg().database()).isEqualTo("ledgerline");
        assertThat(props.pg().user()).isEqualTo("ledger");
        assertThat(props.pg().password()).isEqualTo("ledger");
        assertThat(props.db().poolMax()).isEqualTo(24);
        assertThat(props.db().poolMin()).isEqualTo(24);
        assertThat(props.db().connLifetimeSeconds()).isEqualTo(3600);
        assertThat(props.db().poolWarmOnStart()).isTrue();
        assertThat(props.redisPoolMax()).isEqualTo(16);
        assertThat(props.cacheTtlSeconds()).isEqualTo(300);
        assertThat(props.logQueueLen()).isEqualTo(8192);
        assertThat(props.bodyMaxBytes()).isEqualTo(65536);
        assertThat(props.pdfDir()).isEqualTo("/data/pdf");
    }

    @Test
    void prepareThresholdDefaultsToTheDriverDefault() {
        // D4: PgJDBC's own default is 5 and the baseline leaves it there, which is what makes
        // the .NET side's explicitly-set Auto Prepare Min Usages=5 an equalization rather than
        // a tuning. A Java-side default that quietly de-tuned or over-tuned this would invert
        // the single most consequential PG dial (spec/db-access.md).
        assertThat(bind(Map.of()).db().prepareThreshold()).isEqualTo(5);
        assertThat(bind(Map.of("JDBC_PREPARE_THRESHOLD", "0")).db().prepareThreshold()).isZero();
    }

    @Test
    void poolMinFollowsPoolMaxWhenUnset() {
        assertThat(bind(Map.of("DB_POOL_MAX", "12")).db().poolMin()).isEqualTo(12);
    }

    @Test
    void mismatchedPoolBoundsRefuseToBind() {
        assertThatThrownBy(() -> bind(Map.of("DB_POOL_MAX", "24", "DB_POOL_MIN", "8")))
                .rootCause()
                .hasMessageContaining("min=max policy");
    }

    @Test
    void connectionLifetimeConvertsSecondsToMilliseconds() {
        // DB_CONN_LIFETIME is seconds (Npgsql's ConnectionLifetime takes seconds); Hikari's
        // maxLifetime is milliseconds. A 1:1 copy would give 3.6-second connections.
        assertThat(bind(Map.of()).db().connLifetimeMillis()).isEqualTo(3_600_000L);
    }

    @Test
    void malformedIntegerRefusesToBind() {
        // A SET-but-malformed knob must refuse startup rather than fall back to the default:
        // a silent fallback would run one service at the default while the other refuses.
        // LedgerConfig.cs throws for the same reason; on this side the binder does it.
        assertThatThrownBy(() -> bind(Map.of("DB_POOL_MAX", "twenty-four")))
                .isInstanceOf(BindException.class);
    }

    /**
     * Binds through the SHIPPED application.yaml, not a copy of it, over an environment holding
     * nothing but the supplied vars - so the defaults asserted above are the ones the container
     * gets, and an ambient var on the build host cannot change the answer.
     */
    private static LedgerProperties bind(Map<String, String> env) {
        var environment = new MockEnvironment();
        environment.getPropertySources().addFirst(new MapPropertySource("env", new HashMap<>(env)));
        try {
            for (var source : new YamlPropertySourceLoader()
                    .load("application", new ClassPathResource("application.yaml"))) {
                environment.getPropertySources().addLast(source);
            }
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        return Binder.get(environment).bindOrCreate("ledger", LedgerProperties.class);
    }
}
