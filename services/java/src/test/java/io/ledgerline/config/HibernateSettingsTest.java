package io.ledgerline.config;

import static org.assertj.core.api.Assertions.assertThat;

import io.ledgerline.data.entity.InvoiceItem;
import jakarta.persistence.EntityManagerFactory;
import org.hibernate.engine.spi.SessionFactoryImplementor;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.hibernate.autoconfigure.HibernateProperties;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;
import org.springframework.orm.jpa.support.OpenEntityManagerInViewInterceptor;

/**
 * Asserts the Hibernate posture from the BUILT EntityManagerFactory rather than from the lines
 * in application.yaml, and asserts the settings the fairness dials table claims are ABSENT are
 * genuinely absent. Every row here is a knob that would move a measured number or a statement
 * shape while leaving the functional tests green.
 *
 * <p>No database is needed or used: the pool warm is off and Hibernate is told the product and
 * version instead of probing for them, so the factory builds with no connection - the same
 * DB-less posture {@code services/dotnet/Ledgerline.Tests/TestEnv.cs} sets up for the .NET
 * suite.
 */
@SpringBootTest(
        webEnvironment = SpringBootTest.WebEnvironment.NONE,
        properties = {
            "ledger.db.pool-warm-on-start=false",
            "spring.jpa.properties.hibernate.boot.allow_jdbc_metadata_access=false",
            "spring.jpa.properties.jakarta.persistence.database-product-name=PostgreSQL",
            "spring.jpa.properties.jakarta.persistence.database-product-version=18.4",
        })
class HibernateSettingsTest {

    @Autowired
    private EntityManagerFactory entityManagerFactory;

    @Autowired
    private HibernateProperties hibernateProperties;

    @Autowired
    private ApplicationContext context;

    @Test
    void schemaManagementIsOff() {
        // spring.jpa.hibernate.ddl-auto has no static default: Boot resolves it at runtime from
        // the datasource ("create-drop when embedded, otherwise none"). The database here is
        // shared with the other SUT and holds the seeded dataset every measurement depends on,
        // which is what makes a wrong resolution catastrophic rather than inconvenient.
        // Boot then REMOVES hibernate.hbm2ddl.auto from the factory's properties
        // when the resolved value is none (measured), so the absence below is the assertion and
        // the declared value is checked separately.
        assertThat(hibernateProperties.getDdlAuto()).isEqualTo("none");
        assertThat(entityManagerFactory.getProperties())
                .doesNotContainKey("hibernate.hbm2ddl.auto")
                .doesNotContainKey("jakarta.persistence.schema-generation.database.action");
    }

    @Test
    void theItemInsertReadsItsGeneratedIdThroughAReturningClause() {
        // NON-DEFAULT and load-bearing for the frozen statement text. Left at its default (true,
        // because PgJDBC supports getGeneratedKeys) PgJDBC implements the call by appending a
        // newline and `RETURNING *` to the insert, so Q5 ships all seven item columns per row
        // where spec/db-access.md freezes "Q5 xN items (RETURNING id)" and EF ships one bigint.
        // The direction of the setting helps Java, so it carries a docs/FAIRNESS.md row.
        assertThat(entityManagerFactory.getProperties())
                .containsEntry("hibernate.jdbc.use_get_generated_keys", "false");
    }

    @Test
    void insertBatchingIsAtHibernatesDefaultAndInertForTheEntitiesThatWrite() {
        // The EFFECTIVE value, not the absence of a property: Hibernate 7's default batch size is
        // 15, so "hibernate.jdbc.batch_size is unset" and "batching is off" are different
        // statements and only this one is about behaviour. What makes the 15 inert is the
        // generator - InsertCoordinatorStandard drops the batch key whenever the identifier is
        // assigned by the insert - so the two are asserted together, and a fairness table that
        // read "batching not configured" off the yaml would be wrong.
        SessionFactoryImplementor factory =
                entityManagerFactory.unwrap(SessionFactoryImplementor.class);

        assertThat(factory.getSessionFactoryOptions().getJdbcBatchSize()).isEqualTo(15);
        assertThat(factory.getSessionFactoryOptions().isOrderInsertsEnabled()).isFalse();
        assertThat(factory.getMappingMetamodel().getEntityDescriptor(InvoiceItem.class)
                .getGenerator().generatedOnExecution()).isTrue();
    }

    @ParameterizedTest
    @ValueSource(strings = {
        // Boot 4.1.1 writes none of these (measured: zero occurrences in spring-boot-hibernate
        // -4.1.1.jar). The ordering keys are hibernate.order_inserts, NOT
        // hibernate.jdbc.order_inserts, so the plausible misspelling is silently ignored; both
        // spellings are asserted absent.
        "hibernate.order_inserts",
        "hibernate.order_updates",
        "hibernate.jdbc.order_inserts",
        // An L2 cache here would silently inflate the Redis hit rate spec/cache.md builds by
        // construction. SchemaMappingTest asserts the SessionFactory flag; this asserts nobody
        // wrote the property either.
        "hibernate.cache.use_second_level_cache",
        "hibernate.cache.use_query_cache",
        "hibernate.cache.region.factory_class",
    })
    void settingIsAbsent(String setting) {
        assertThat(entityManagerFactory.getProperties()).doesNotContainKey(setting);
    }

    @Test
    void openEntityManagerInViewIsOff() {
        // Boot's default is true and it logs a startup WARN when left implicit, which would put
        // a non-contract line on the frozen log stream (spec/logging.md). Asserted as the
        // absence of the interceptor, because that is the thing that would hold a pooled
        // connection for the whole request.
        assertThat(context.getBeanNamesForType(OpenEntityManagerInViewInterceptor.class)).isEmpty();
    }
}
