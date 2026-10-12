package io.ledgerline.cache;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoMoreInteractions;
import static org.mockito.Mockito.when;

import io.ledgerline.config.LedgerProperties;
import io.ledgerline.dto.InvoiceItemResponse;
import io.ledgerline.dto.InvoiceResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import tools.jackson.databind.PropertyNamingStrategies;
import tools.jackson.databind.json.JsonMapper;

/**
 * spec/cache.md's three contract clauses, each of which the 0.80 hit rate is constructed from:
 * the key format, the TTL on the SET, and SERIALIZE-THEN-CACHE - the stored value being the
 * exact response bytes, so a hit and a miss cannot differ.
 */
@ExtendWith(MockitoExtension.class)
class InvoiceCacheTest {

    private static final InvoiceResponse INVOICE = new InvoiceResponse(7, 3, "draft", "USD", 1200,
            Instant.parse("2026-06-06T00:00:00Z"),
            List.of(new InvoiceItemResponse(1, "EL-0042", "widget", 2, 600, 1200)));

    @Mock
    private RedisTemplate<String, byte[]> redis;

    @Mock
    private ValueOperations<String, byte[]> values;

    private JsonMapper json;
    private InvoiceCache cache;

    @BeforeEach
    void setUp() {
        json = JsonMapper.builder()
                .propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE)
                .build();
        cache = new InvoiceCache(redis, json, properties(300));
    }

    @Test
    void aHitReturnsTheStoredBytesWithoutTouchingTheLoader() {
        byte[] stored = "{\"id\":7}".getBytes(StandardCharsets.UTF_8);
        when(redis.opsForValue()).thenReturn(values);
        when(values.get("invoice:7")).thenReturn(stored);
        AtomicInteger loads = new AtomicInteger();

        byte[] body = cache.getOrLoadBytes(7, () -> {
            loads.incrementAndGet();
            return INVOICE;
        });

        assertThat(body).isSameAs(stored);
        assertThat(loads).hasValue(0);
        verify(values, never()).set(any(), any(), any(Duration.class));
    }

    @Test
    void aMissStoresTheSameBytesItReturns() {
        when(redis.opsForValue()).thenReturn(values);
        when(values.get("invoice:7")).thenReturn(null);

        byte[] body = cache.getOrLoadBytes(7, () -> INVOICE);

        // The bytes handed to Redis and the bytes handed to the client are one array: that is
        // what makes a later hit byte-equal to this miss, without a second serialization.
        verify(values).set("invoice:7", body, Duration.ofSeconds(300));
        assertThat(new String(body, StandardCharsets.UTF_8))
                .isEqualTo(new String(json.writeValueAsBytes(INVOICE), StandardCharsets.UTF_8));
    }

    @Test
    void anAbsentInvoiceIsNotCached() {
        // spec/cache.md forbids negative caching: a cached 404 would survive the DEL that a
        // later create issues for the same id.
        when(redis.opsForValue()).thenReturn(values);
        when(values.get("invoice:9")).thenReturn(null);

        assertThat(cache.getOrLoadBytes(9, () -> null)).isNull();

        verify(values).get("invoice:9");
        verifyNoMoreInteractions(values);
    }

    @Test
    void invalidationIsADeleteOfTheSameKeyShape() {
        cache.invalidate(41);
        verify(redis).delete(eq("invoice:41"));
    }

    @Test
    void theTtlComesFromCacheTtlSecondsRatherThanAConstant() {
        when(redis.opsForValue()).thenReturn(values);
        when(values.get("invoice:7")).thenReturn(null);

        new InvoiceCache(redis, json, properties(900)).getOrLoadBytes(7, () -> INVOICE);

        verify(values).set(eq("invoice:7"), any(), eq(Duration.ofSeconds(900)));
    }

    private static LedgerProperties properties(int cacheTtlSeconds) {
        return new LedgerProperties(
                new LedgerProperties.Pg("localhost", 5432, "ledgerline", "ledger", "ledger"),
                new LedgerProperties.Db(24, 24, 3600, 5, false),
                16, cacheTtlSeconds, 8192, 65536, "/data/pdf");
    }
}
