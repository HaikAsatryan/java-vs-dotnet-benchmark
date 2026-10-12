package io.ledgerline.data;

import io.ledgerline.data.entity.Invoice;
import java.util.List;
import java.util.Optional;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.Repository;

/**
 * Q1 and Q3 of spec/db-access.sql, plus the header insert (Q4). Emitted SQL and every deviation
 * from the frozen text are pinned and explained in StatementShapeTest.
 *
 * <p>The narrow {@link Repository} marker, not {@code CrudRepository}: an inherited name is one
 * autocomplete away, and a declared query that collides with one silently acquires
 * {@code SimpleJpaRepository}'s {@code @Transactional(readOnly = true)}. Nothing here is
 * transactional at all; what that would cost is measured once, on
 * {@link CustomerRepository#customerExists(long)}, and RepositorySeamTest closes the trap for
 * every name.
 */
public interface InvoiceRepository extends Repository<Invoice, Long> {

    /** Q1. The constructor expression recovers the frozen column order; the alias cannot. */
    @Query("""
            select new io.ledgerline.data.InvoiceHeader(
                i.id, i.customerId, i.status, i.currency, i.totalMinor, i.createdAt)
            from Invoice i
            where i.id = :id""")
    Optional<InvoiceHeader> findHeaderById(long id);

    /**
     * Q3. A {@code List}, not a {@code Page}: a Page issues a second COUNT query the ledger does
     * not carry. Callers pass an UNSORTED Pageable - the ORDER BY is in the query, and a Sort on
     * top emits it twice. PINNED DEVIATION: two statement texts split by page number
     * (spec/db-access.md).
     */
    @Query("""
            select new io.ledgerline.data.InvoiceHeader(
                i.id, i.customerId, i.status, i.currency, i.totalMinor, i.createdAt)
            from Invoice i
            where i.customerId = :customerId
            order by i.id desc""")
    List<InvoiceHeader> findPageByCustomerId(long customerId, Pageable pageable);

    /**
     * Q4. IDENTITY makes the insert immediate at persist, so total_minor must be set BEFORE it -
     * assigning it after produces a full-column UPDATE at flush that no other stack sends.
     * PINNED DEVIATION: alphabetical insert column order.
     */
    Invoice save(Invoice invoice);
}
