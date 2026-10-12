package io.ledgerline.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.data.redis.serializer.RedisSerializer;

@Configuration(proxyBeanMethods = false)
class RedisConfig {

    /**
     * Values are {@code byte[]} end to end: spec/cache.md's SERIALIZE-THEN-CACHE pin says the
     * cached value is the exact bytes the endpoint returns, and Boot's default template would
     * put a JDK-serialization round trip between them. Keys stay {@code String} so the wire key
     * is literally {@code invoice:{id}} rather than a serialized form.
     */
    @Bean
    RedisTemplate<String, byte[]> invoiceRedisTemplate(RedisConnectionFactory connectionFactory) {
        RedisTemplate<String, byte[]> template = new RedisTemplate<>();
        template.setConnectionFactory(connectionFactory);
        template.setKeySerializer(RedisSerializer.string());
        template.setValueSerializer(RedisSerializer.byteArray());
        template.setEnableDefaultSerializer(false);
        return template;
    }
}
