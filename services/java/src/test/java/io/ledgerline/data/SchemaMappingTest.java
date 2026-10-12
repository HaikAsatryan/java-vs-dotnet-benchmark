package io.ledgerline.data;

import static org.assertj.core.api.Assertions.assertThat;

import io.ledgerline.data.entity.Customer;
import io.ledgerline.data.entity.Invoice;
import io.ledgerline.data.entity.InvoiceItem;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.time.Instant;
import java.util.List;
import org.hibernate.persister.entity.EntityPersister;
import org.hibernate.type.BasicType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

/**
 * Asserts the Hibernate mapping matches spec/schema.sql exactly: table and column names, money
 * as bigint/long and qty as int, IDENTITY on the two generated keys, an assigned id on
 * customers, and the two properties of {@code invoices.created_at} that make Q4 a single
 * statement. The analog of {@code services/dotnet/Ledgerline.Tests/EfSchemaMappingTests.cs}.
 */
class SchemaMappingTest {

    private static OfflineSessionFactory offline;

    @BeforeAll
    static void boot() {
        offline = new OfflineSessionFactory();
    }

    @AfterAll
    static void shutdown() {
        offline.close();
    }

    @Test
    void tablesAreTheSpecTables() {
        assertThat(offline.persister(Customer.class).getTableName()).isEqualTo("customers");
        assertThat(offline.persister(Invoice.class).getTableName()).isEqualTo("invoices");
        assertThat(offline.persister(InvoiceItem.class).getTableName()).isEqualTo("invoice_items");
    }

    @ParameterizedTest
    @CsvSource({
        "io.ledgerline.data.entity.Customer,    name,           name,             VARCHAR,       java.lang.String",
        "io.ledgerline.data.entity.Customer,    email,          email,            VARCHAR,       java.lang.String",
        "io.ledgerline.data.entity.Customer,    balanceMinor,   balance_minor,    BIGINT,        long",
        "io.ledgerline.data.entity.Customer,    currency,       currency,         VARCHAR,       java.lang.String",
        "io.ledgerline.data.entity.Customer,    createdAt,      created_at,       TIMESTAMP_UTC, java.time.Instant",
        "io.ledgerline.data.entity.Invoice,     customerId,     customer_id,      BIGINT,        long",
        "io.ledgerline.data.entity.Invoice,     status,         status,           VARCHAR,       java.lang.String",
        "io.ledgerline.data.entity.Invoice,     currency,       currency,         VARCHAR,       java.lang.String",
        "io.ledgerline.data.entity.Invoice,     totalMinor,     total_minor,      BIGINT,        long",
        "io.ledgerline.data.entity.Invoice,     createdAt,      created_at,       TIMESTAMP_UTC, java.time.Instant",
        "io.ledgerline.data.entity.InvoiceItem, invoiceId,      invoice_id,       BIGINT,        long",
        "io.ledgerline.data.entity.InvoiceItem, sku,            sku,              VARCHAR,       java.lang.String",
        "io.ledgerline.data.entity.InvoiceItem, description,    description,      VARCHAR,       java.lang.String",
        "io.ledgerline.data.entity.InvoiceItem, qty,            qty,              INTEGER,       int",
        "io.ledgerline.data.entity.InvoiceItem, unitPriceMinor, unit_price_minor, BIGINT,        long",
        "io.ledgerline.data.entity.InvoiceItem, lineTotalMinor, line_total_minor, BIGINT,        long",
    })
    void propertiesMapToSpecColumnsAndTypes(
            Class<?> entity, String property, String column, String jdbcType, String fieldType)
            throws NoSuchFieldException {
        EntityPersister persister = offline.persister(entity);

        assertThat(persister.getPropertyColumnNames(property)).containsExactly(column);
        // The JDBC side: money is bigint and qty is int, matching spec/schema.sql. created_at
        // resolving to TIMESTAMP_UTC rather than TIMESTAMP is what makes the timestamptz
        // round-trip independent of the JVM's default zone.
        assertThat(((BasicType<?>) persister.getPropertyType(property)).getJdbcType().getFriendlyName())
                .isEqualTo(jdbcType);
        // The Java side: primitives, so there is no per-row boxing on a money column. Hibernate
        // reports the boxed class for either declaration, so the field itself is the assertion.
        assertThat(entity.getDeclaredField(property).getType().getName()).isEqualTo(fieldType);
    }

    @Test
    void identifiersAreBigint() {
        List.of(Customer.class, Invoice.class, InvoiceItem.class).forEach(entity -> {
            EntityPersister persister = offline.persister(entity);
            assertThat(persister.getIdentifierColumnNames()).containsExactly("id");
            assertThat(((BasicType<?>) persister.getIdentifierType()).getJdbcType().getFriendlyName())
                    .isEqualTo("BIGINT");
        });
    }

    @Test
    void moneyIsLongAndNeverBigDecimal() {
        // P4 trap 4: int64 minor units end to end. A BigDecimal anywhere in a money path would
        // be an allocation and a slow arithmetic path the .NET side does not have.
        List.of(Customer.class, Invoice.class, InvoiceItem.class).forEach(entity -> {
            EntityPersister persister = offline.persister(entity);
            for (String property : persister.getPropertyNames()) {
                assertThat(persister.getPropertyType(property).getReturnedClass())
                        .isNotIn(BigDecimal.class, BigInteger.class, double.class, float.class);
            }
        });
    }

    @Test
    void generatedIdsAreIdentityAndCustomerIdIsAssigned() {
        // generatedOnExecution() is the flag InsertCoordinatorStandard reads to drop the batch
        // key, so this assertion is also the one that pins the disclosed N-round-trip item
        // insert (docs/research/2026-08-21-measured.md FINDING 1).
        assertThat(offline.persister(Invoice.class).getGenerator().generatedOnExecution()).isTrue();
        assertThat(offline.persister(InvoiceItem.class).getGenerator().generatedOnExecution()).isTrue();
        assertThat(offline.persister(Customer.class).getGenerator().generatedOnExecution()).isFalse();
    }

    @Test
    void invoiceCreatedAtIsDatabaseGeneratedAndNeverWritten() {
        // @Generated(INSERT) keeps created_at out of the INSERT column list so the DB DEFAULT
        // now() applies and comes back in the same RETURNING; @Column(updatable = false) keeps
        // it out of UPDATE, without which a dirty update would overwrite the DB timestamp with
        // the in-memory value. Both are invisible in review, so both are asserted.
        EntityPersister invoice = offline.persister(Invoice.class);
        int index = List.of(invoice.getPropertyNames()).indexOf("createdAt");

        assertThat(invoice.getPropertyInsertability()[index]).isFalse();
        assertThat(invoice.getPropertyUpdateability()[index]).isFalse();
        assertThat(invoice.getPropertyType("createdAt").getReturnedClass()).isEqualTo(Instant.class);
    }

    @Test
    void secondLevelCacheIsOff() {
        // Asserted on the SessionFactory rather than on a property, because the property nobody
        // sets is not evidence. An in-process entity cache would silently inflate the Redis hit
        // rate that spec/cache.md targets by construction. One cache-provider jar on the
        // classpath is all it takes to flip this.
        assertThat(offline.sessionFactory().getSessionFactoryOptions().isSecondLevelCacheEnabled()).isFalse();
    }

    @Test
    void thereAreNoAssociations() {
        // Scalar FKs, no @ManyToOne and no items collection. The customer FK is navigation-free
        // on the EF side too; the items collection is NOT (LedgerDbContext maps
        // Invoice.Items), and mirroring it here would cost N extra UPDATE statements rather than
        // save any - measured, and recorded on the Invoice entity. An association added later
        // would also open a path that loads invoice+items in one statement and deviates from the
        // two-statement cache-miss read spec/db-access.md pins.
        List.of(Customer.class, Invoice.class, InvoiceItem.class).forEach(entity -> {
            EntityPersister persister = offline.persister(entity);
            for (String property : persister.getPropertyNames()) {
                assertThat(persister.getPropertyType(property).isAssociationType())
                        .as("%s.%s", entity.getSimpleName(), property)
                        .isFalse();
            }
        });
    }
}
