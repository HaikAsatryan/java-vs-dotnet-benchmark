package io.ledgerline.cache;

import io.ledgerline.config.LedgerProperties;
import io.ledgerline.dto.InvoiceResponse;
import java.time.Duration;
import java.util.function.Supplier;
import org.jspecify.annotations.Nullable;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.json.JsonMapper;

/**
 * Cache-aside per spec/cache.md, 1:1 with {@code services/dotnet/Cache/InvoiceCache.cs}. The
 * stored value is the exact body the endpoint returns, so a hit and a miss are byte-equal and
 * the 0.80 hit rate cannot change what a reader sees.
 */
@Component
public class InvoiceCache {

    private final RedisTemplate<String, byte[]> redis;
    private final JsonMapper json;
    private final Duration ttl;

    InvoiceCache(RedisTemplate<String, byte[]> invoiceRedisTemplate, JsonMapper json,
            LedgerProperties properties) {
        this.redis = invoiceRedisTemplate;
        this.json = json;
        this.ttl = properties.cacheTtl();
    }

    /**
     * Hit returns the cached bytes verbatim - no deserialize, no re-serialize. A miss whose
     * loader finds nothing returns null and caches NOTHING: spec/cache.md forbids negative
     * caching, and a cached 404 would survive the invalidation DEL of a later create.
     */
    public byte @Nullable [] getOrLoadBytes(long id, Supplier<@Nullable InvoiceResponse> loader) {
        String key = key(id);
        byte[] cached = redis.opsForValue().get(key);
        if (cached != null) {
            return cached;
        }

        InvoiceResponse invoice = loader.get();
        if (invoice == null) {
            return null;
        }

        byte[] body = json.writeValueAsBytes(invoice);
        redis.opsForValue().set(key, body, ttl);
        return body;
    }

    /** The create path's DEL, after commit and outside the transaction (spec/db-access.md). */
    public void invalidate(long id) {
        redis.delete(key(id));
    }

    public boolean ping() {
        try {
            RedisConnectionFactory factory = redis.getRequiredConnectionFactory();
            try (var connection = factory.getConnection()) {
                connection.ping();
            }
            return true;
        } catch (RuntimeException e) {
            return false;
        }
    }

    private static String key(long id) {
        return "invoice:" + id;
    }
}
