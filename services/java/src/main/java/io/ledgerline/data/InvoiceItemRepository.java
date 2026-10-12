package io.ledgerline.data;

import io.ledgerline.data.entity.InvoiceItem;
import java.util.List;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.Repository;

/**
 * Q2 of spec/db-access.sql, plus the item insert (Q5). Emitted SQL pinned by StatementShapeTest.
 */
public interface InvoiceItemRepository extends Repository<InvoiceItem, Long> {

    /** Q2. Same treatment as Q1: projected, aliased, untransacted. */
    @Query("""
            select new io.ledgerline.data.InvoiceItemRow(
                it.id, it.sku, it.description, it.qty, it.unitPriceMinor, it.lineTotalMinor)
            from InvoiceItem it
            where it.invoiceId = :invoiceId
            order by it.id""")
    List<InvoiceItemRow> findByInvoiceId(long invoiceId);

    /**
     * Q5, one statement per item; no {@code saveAll}, which would read as if it batched when
     * IDENTITY makes batching unreachable ({@link InvoiceItem}). PINNED DEVIATION: alphabetical
     * column order. The frozen {@code returning id} depends on
     * {@code hibernate.jdbc.use_get_generated_keys=false} in application.yaml.
     */
    InvoiceItem save(InvoiceItem item);
}
