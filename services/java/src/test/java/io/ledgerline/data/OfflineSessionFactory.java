package io.ledgerline.data;

import io.ledgerline.data.entity.Customer;
import io.ledgerline.data.entity.Invoice;
import io.ledgerline.data.entity.InvoiceItem;
import java.lang.reflect.Array;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.hibernate.SessionFactory;
import org.hibernate.boot.MetadataSources;
import org.hibernate.boot.registry.StandardServiceRegistryBuilder;
import org.hibernate.engine.jdbc.connections.spi.ConnectionProvider;
import org.hibernate.engine.spi.SessionFactoryImplementor;
import org.hibernate.persister.entity.EntityPersister;

/**
 * A Hibernate {@link SessionFactory} over the real entity mappings and the real PostgreSQL
 * dialect, with no database behind it. The direct analog of
 * {@code services/dotnet/Ledgerline.Tests/EfQueryShapeTests}, which builds the EF model against
 * a connection string it never opens: the SQL Hibernate generates is a property of the mapping,
 * the dialect and the target version, so it can be asserted on a host with nothing running -
 * which is the posture of the whole default test run.
 *
 * <p>{@code allow_jdbc_metadata_access=false} is what makes that possible: with it set,
 * Hibernate takes the database product and version from the explicit settings instead of
 * probing a connection at boot. The version is pinned to the 18.4 of infra/compose.yaml, not
 * left at the dialect's 14.0 floor, so a version-conditional rendering could not silently
 * differ from what the measured runs get.
 *
 * <p>Limits, and they are why the P5 audit is a live PostgreSQL capture and not this: what is
 * recorded here is the SQL Hibernate hands to JDBC. It is blind to transaction control (the bare
 * {@code BEGIN}, and the {@code BEGIN READ ONLY} an annotated repository would add), to PgJDBC's
 * client-side rewrites (the {@code ?}-to-{@code $n} numbering), and to round-trip counts. In-JVM
 * capture is admissible for statement shape and inadmissible for a parity claim.
 */
final class OfflineSessionFactory implements AutoCloseable {

    private final List<String> recorded = new ArrayList<>();
    private final SessionFactory sessionFactory;

    OfflineSessionFactory() {
        Map<String, Object> settings = new HashMap<>(Map.of(
                "jakarta.persistence.database-product-name", "PostgreSQL",
                "jakarta.persistence.database-product-version", "18.4",
                "hibernate.boot.allow_jdbc_metadata_access", "false",
                "hibernate.hbm2ddl.auto", "none",
                // Mirrors application.yaml, which HibernateSettingsTest asserts off the real
                // factory. It decides whether Q5 carries a returning clause at all.
                "hibernate.jdbc.use_get_generated_keys", "false"));
        settings.put("hibernate.connection.provider_class", new RecordingConnections());

        var registry = new StandardServiceRegistryBuilder().applySettings(settings).build();
        this.sessionFactory = new MetadataSources(registry)
                .addAnnotatedClass(Customer.class)
                .addAnnotatedClass(Invoice.class)
                .addAnnotatedClass(InvoiceItem.class)
                .buildMetadata()
                .buildSessionFactory();
    }

    SessionFactory sessionFactory() {
        return sessionFactory;
    }

    EntityPersister persister(Class<?> entity) {
        return sessionFactory.unwrap(SessionFactoryImplementor.class)
                .getMappingMetamodel()
                .getEntityDescriptor(entity);
    }

    List<String> drainStatements() {
        List<String> statements = List.copyOf(recorded);
        recorded.clear();
        return statements;
    }

    @Override
    public void close() {
        sessionFactory.close();
    }

    /**
     * Hands Hibernate a {@link Connection} built entirely from dynamic proxies: every method
     * returning another JDBC interface answers with a further proxy, everything else answers
     * with the zero value for its return type, and {@code prepareStatement} records its SQL on
     * the way through. Queries therefore run to completion over an empty result set.
     */
    private final class RecordingConnections implements ConnectionProvider {

        @Override
        public Connection getConnection() {
            return (Connection) proxy(Connection.class);
        }

        @Override
        public void closeConnection(Connection connection) {
        }

        @Override
        public boolean supportsAggressiveRelease() {
            return false;
        }

        @Override
        public boolean isUnwrappableAs(Class<?> unwrapType) {
            return false;
        }

        @Override
        public <T> T unwrap(Class<T> unwrapType) {
            throw new UnsupportedOperationException();
        }

        private Object proxy(Class<?> type) {
            InvocationHandler handler = (target, method, args) -> switch (method.getName()) {
                case "prepareStatement", "prepareCall" -> {
                    recorded.add((String) args[0]);
                    yield proxy(method.getReturnType());
                }
                // JDBC's result-walking idiom loops until getUpdateCount() reports -1, so a
                // proxy that answered 0 here would spin forever.
                case "getUpdateCount" -> -1;
                case "toString" -> "offline-jdbc:" + type.getSimpleName();
                case "hashCode" -> System.identityHashCode(target);
                case "equals" -> target == args[0];
                default -> answer(method.getReturnType());
            };
            return Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[] {type}, handler);
        }

        private Object answer(Class<?> returnType) {
            if (returnType.isInterface()) {
                return proxy(returnType);
            }
            if (!returnType.isPrimitive() || returnType == void.class) {
                return null;
            }
            return Array.get(Array.newInstance(returnType, 1), 0);
        }
    }
}
