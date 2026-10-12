package io.ledgerline.data;

import io.ledgerline.data.entity.Invoice;
import io.ledgerline.data.entity.InvoiceItem;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * The create path's transaction, which cannot live in any one repository: spec/db-access.md pins
 * Q4 + Q5xN + Q6 as ONE transaction and Spring Data demarcates per repository method across three
 * proxies, where {@code EfInvoiceRepository.CreateAsync} opens the same one by hand.
 *
 * <p>Q7 is NOT here - .NET probes existence before {@code BeginTransactionAsync} and the frozen
 * ledger reads {@code existence/load (1) + BEGIN + ...}, so the caller runs
 * {@link CustomerRepository#customerExists(long)} outside.
 */
@Service
public class InvoiceWriter {

    private final InvoiceRepository invoices;
    private final InvoiceItemRepository items;
    private final CustomerRepository customers;

    InvoiceWriter(InvoiceRepository invoices, InvoiceItemRepository items,
            CustomerRepository customers) {
        this.invoices = invoices;
        this.items = items;
        this.customers = customers;
    }

    /**
     * Isolation stays {@code DEFAULT}, which reaches the wire as a bare {@code BEGIN}. Naming
     * READ_COMMITTED would not match .NET's spelled-out BEGIN, it would add a separate
     * {@code SET SESSION CHARACTERISTICS} statement ahead of it (measured; NOTES-equivalence.md).
     * Q4's {@code RETURNING} must land before an item insert can bind the id, so nothing here
     * can be pipelined.
     */
    @Transactional
    public void create(Invoice invoice, List<InvoiceItem> lines) {
        invoices.save(invoice);
        for (InvoiceItem line : lines) {
            line.setInvoiceId(invoice.getId());
            items.save(line);
        }
        customers.addToBalance(invoice.getTotalMinor(), invoice.getCustomerId());
    }
}
