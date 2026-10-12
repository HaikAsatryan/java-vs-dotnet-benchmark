package io.ledgerline.data;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import io.ledgerline.data.entity.Invoice;
import io.ledgerline.data.entity.InvoiceItem;
import java.lang.reflect.Method;
import java.util.List;
import java.util.function.Consumer;
import org.hibernate.Session;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.Query;

/**
 * Pins the SQL Hibernate generates for every statement the ORM builds - the three declared reads,
 * taken from the repository interfaces themselves so the assertion cannot drift from the shipped
 * query, and the two inserts. The analog of
 * {@code services/dotnet/Ledgerline.Tests/EfQueryShapeTests.cs}.
 *
 * <p>Four of these texts cannot match spec/db-access.sql (Q3 twice, Q4 and Q5 on column order)
 * and are pinned as deviations rather than rewritten as native SQL: hiding them behind
 * hand-written SQL would delete the ORM work this round exists to measure. A Hibernate upgrade
 * that changes any of them fails here, which is the point.
 */
class StatementShapeTest {

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
    void q1SelectsTheSpecColumnsInTheSpecOrder() {
        String sql = run(session -> session
                .createQuery(hql(InvoiceRepository.class, "findHeaderById", long.class), InvoiceHeader.class)
                .setParameter("id", 1L)
                .getResultList());

        // The constructor-expression projection recovers the frozen column ORDER, which an
        // entity load does not (an entity load emits id-then-alphabetical). What remains
        // different from spec/db-access.sql is the table alias, which is not reachable without
        // native SQL. Note there is no trailing LIMIT 1: Hibernate is closer to the frozen text
        // here than EF Core, which appends one for FirstOrDefault.
        assertThat(sql).isEqualTo(
                "select i1_0.id,i1_0.customer_id,i1_0.status,i1_0.currency,i1_0.total_minor,"
                        + "i1_0.created_at from invoices i1_0 where i1_0.id=?");
    }

    @Test
    void q2SelectsTheSpecColumnsOrderedByIdAscending() {
        String sql = run(session -> session
                .createQuery(hql(InvoiceItemRepository.class, "findByInvoiceId", long.class), InvoiceItemRow.class)
                .setParameter("invoiceId", 1L)
                .getResultList());

        assertThat(sql).isEqualTo(
                "select ii1_0.id,ii1_0.sku,ii1_0.description,ii1_0.qty,ii1_0.unit_price_minor,"
                        + "ii1_0.line_total_minor from invoice_items ii1_0 where ii1_0.invoice_id=? "
                        + "order by ii1_0.id");
    }

    @Test
    void q3PageTwoEmitsOffsetFetchRatherThanLimitOffset() {
        String sql = run(session -> session
                .createQuery(hql(InvoiceRepository.class, "findPageByCustomerId", long.class, Pageable.class),
                        InvoiceHeader.class)
                .setParameter("customerId", 1L)
                .setFirstResult(40)
                .setMaxResults(20)
                .getResultList());

        // PINNED DEVIATION. spec/db-access.sql freezes `LIMIT 20 OFFSET $2` and EF ships
        // `LIMIT $2 OFFSET $3`; this is a third distinct text. PostgreSQLDialect returns
        // OffsetFetchLimitHandler as a hard-coded constant, so no property or hint changes it.
        // Plan-identical - a backward index range scan on idx_invoices_customer_id_id_desc with
        // no sort node - which P5 confirms with EXPLAIN against the real database.
        assertThat(sql).isEqualTo(
                "select i1_0.id,i1_0.customer_id,i1_0.status,i1_0.currency,i1_0.total_minor,"
                        + "i1_0.created_at from invoices i1_0 where i1_0.customer_id=? "
                        + "order by i1_0.id desc offset ? rows fetch first ? rows only");
    }

    @Test
    void q3EmitsASecondTextWithNoOffsetClauseWhenFirstResultIsNeverSet() {
        String sql = run(session -> session
                .createQuery(hql(InvoiceRepository.class, "findPageByCustomerId", long.class, Pageable.class),
                        InvoiceHeader.class)
                .setParameter("customerId", 1L)
                .setMaxResults(20)
                .getResultList());

        // Page 1: Spring Data skips setFirstResult when the offset is zero, so no offset clause
        // is rendered. Pinned deviation: Q3 has two statement texts (spec/db-access.md).
        assertThat(sql).isEqualTo(
                "select i1_0.id,i1_0.customer_id,i1_0.status,i1_0.currency,i1_0.total_minor,"
                        + "i1_0.created_at from invoices i1_0 where i1_0.customer_id=? "
                        + "order by i1_0.id desc fetch first ? rows only");
        assertThat(sql).doesNotContain("offset");
    }

    @Test
    void q4InsertsAlphabeticalColumnsAndReturnsBothGeneratedValues() {
        // PINNED DEVIATION. spec/db-access.sql freezes
        // `INSERT INTO invoices (customer_id, status, currency, total_minor)`; Hibernate orders
        // insert columns ALPHABETICALLY, so both the column list and the parameter-to-column
        // mapping differ from the frozen text and from EF's. Not reachable without native SQL.
        // The `returning id,created_at` half IS the frozen semantics: @Generated(INSERT) plus
        // IDENTITY brings the DB DEFAULT now() back with no second SELECT.
        assertThat(insertSqlFor(newInvoice())).isEqualTo(
                "insert into invoices (currency,customer_id,status,total_minor) "
                        + "values (?,?,?,?) returning id,created_at");
    }

    @Test
    void q5InsertsAlphabeticalColumnsAndReturnsTheGeneratedIdOnly() {
        // Same alphabetical deviation as Q4. The returning clause is the assertion: at
        // Hibernate's default the id comes back through JDBC getGeneratedKeys, which PgJDBC
        // implements by appending a newline and `RETURNING *` CLIENT-SIDE - all seven item
        // columns per row, captured against PostgreSQL 18.4 with log_statement='all', where
        // spec/db-access.md freezes `RETURNING id`. use_get_generated_keys=false puts the clause
        // in the statement Hibernate builds, where it is visible here.
        assertThat(insertSqlFor(newItem())).isEqualTo(
                "insert into invoice_items (description,invoice_id,line_total_minor,qty,sku,"
                        + "unit_price_minor) values (?,?,?,?,?,?) returning id");
    }

    @Test
    void q3OrdersOnlyOnce() {
        // Measured trap: a Sort passed alongside an ORDER BY already in the query emits
        // `order by i1_0.id desc,i1_0.id desc`. Callers pass an unsorted Pageable; this is what
        // fails if one stops. Independent of the offset split above.
        String sql = run(session -> session
                .createQuery(hql(InvoiceRepository.class, "findPageByCustomerId", long.class, Pageable.class),
                        InvoiceHeader.class)
                .setParameter("customerId", 1L)
                .setMaxResults(20)
                .getResultList());

        assertThat(sql.split("order by", -1)).hasSize(2);
        assertThat(sql).doesNotContain("desc,");
    }

    /**
     * The INSERT Hibernate prepares for one entity. The statement is recorded at
     * {@code prepareStatement}, before execution: the offline connection returns an empty
     * generated-keys result, so the persist itself always fails afterwards, and that failure is
     * asserted rather than swallowed so a DIFFERENT failure cannot pass silently.
     */
    private static String insertSqlFor(Object entity) {
        offline.drainStatements();
        assertThatThrownBy(() -> offline.sessionFactory().inTransaction(session -> session.persist(entity)))
                .isInstanceOf(IndexOutOfBoundsException.class);
        List<String> recorded = offline.drainStatements();
        assertThat(recorded).hasSize(1);
        return recorded.getFirst();
    }

    private static Invoice newInvoice() {
        Invoice invoice = new Invoice();
        invoice.setCustomerId(1L);
        invoice.setStatus("draft");
        invoice.setCurrency("USD");
        invoice.setTotalMinor(300L);
        return invoice;
    }

    private static InvoiceItem newItem() {
        InvoiceItem item = new InvoiceItem();
        item.setInvoiceId(1L);
        item.setSku("EL-0001");
        item.setDescription("widget");
        item.setQty(3);
        item.setUnitPriceMinor(100L);
        item.setLineTotalMinor(300L);
        return item;
    }

    private static String hql(Class<?> repository, String method, Class<?>... parameterTypes) {
        Method declared;
        try {
            declared = repository.getMethod(method, parameterTypes);
        } catch (NoSuchMethodException e) {
            throw new AssertionError(e);
        }
        Query query = declared.getAnnotation(Query.class);
        assertThat(query.nativeQuery()).as("%s.%s is HQL, not native", repository.getSimpleName(), method).isFalse();
        return query.value();
    }

    private static String run(Consumer<Session> work) {
        offline.drainStatements();
        offline.sessionFactory().inSession(work);
        return offline.drainStatements().getFirst();
    }
}
