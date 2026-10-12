package io.ledgerline;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.DataInputStream;
import java.io.IOException;
import java.io.InputStream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

/**
 * Invariants of the built artifact rather than of any one class: what the compiler emitted and
 * what is NOT on the classpath. Each changes a measured number while leaving every functional
 * test green.
 *
 * <p>Nothing here reads the environment. The JVM-flag carriers (JAVA_TOOL_OPTIONS, JAVA_OPTS,
 * _JAVA_OPTIONS) are a property of the SUT CONTAINER, not of the build artifact, and asserting
 * them from whatever shell invokes Maven encodes the developer's profile in a unit test; the
 * manifest read-back in bench/ is where that belongs.
 */
class BuildInvariantsTest {

    @Test
    void classFilesAreJava25() {
        // spring-boot-starter-parent:4.1.1 sets <java.version>17</java.version> and feeds
        // maven.compiler.release from it, so without the POM override the SUT would compile to
        // class file 61 and run on JDK 25 with nothing warning and the manifest's Java-25 claim
        // false. 69 is Java 25 (D1). Asserted on the emitted bytecode, not on the POM property.
        assertThat(majorVersion(LedgerlineApplication.class)).isEqualTo(69);
    }

    @ParameterizedTest
    @ValueSource(strings = {
        // Boot's Lettuce pool gate is `enabled != null ? enabled : COMMONS_POOL2_AVAILABLE`, so
        // the jar alone switches Redis from one shared multiplexed connection - the SE.Redis
        // model - to a pool, with no property set anywhere.
        "org.apache.commons.pool2.ObjectPool",
        // find-and-add-modules defaults true and both accelerators ship a ServiceLoader entry,
        // so either dependency alone would enable bytecode-generated (de)serialization. Their
        // absence is a disclosed posture (docs/FAIRNESS.md JSON row), so it is asserted from
        // the classpath rather than described in prose.
        "tools.jackson.module.blackbird.BlackbirdModule",
        "tools.jackson.module.afterburner.AfterburnerModule",
        // A Hibernate cache-provider jar makes RegionFactoryInitiator resolve a provider, at
        // which point hibernate.cache.use_second_level_cache defaults to TRUE and an in-process
        // entity cache silently inflates the Redis hit rate spec/cache.md builds by hand.
        "org.hibernate.cache.jcache.internal.JCacheRegionFactory",
    })
    void classpathDoesNotCarry(String className) {
        assertThat(isOnClasspath(className))
                .as("%s must not be on the resolved classpath", className)
                .isFalse();
    }

    private static boolean isOnClasspath(String className) {
        try {
            Class.forName(className, false, BuildInvariantsTest.class.getClassLoader());
            return true;
        } catch (ClassNotFoundException e) {
            return false;
        }
    }

    private static int majorVersion(Class<?> type) {
        String resource = type.getName().replace('.', '/') + ".class";
        try (InputStream in = type.getClassLoader().getResourceAsStream(resource);
                DataInputStream data = new DataInputStream(in)) {
            // The major version follows the magic (u4) and the minor version (u2).
            data.readInt();
            data.readShort();
            return data.readUnsignedShort();
        } catch (IOException e) {
            throw new AssertionError(e);
        }
    }
}
