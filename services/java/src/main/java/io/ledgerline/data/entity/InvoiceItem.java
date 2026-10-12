package io.ledgerline.data.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import org.jspecify.annotations.Nullable;

/**
 * spec/schema.sql: invoice_items.id is GENERATED ALWAYS AS IDENTITY. Scalar {@code long
 * invoiceId} for the reasons recorded on {@link Invoice}.
 *
 * <p>IDENTITY has a measured cost here that no setting recovers: Hibernate's
 * InsertCoordinatorStandard drops the batch key whenever the identifier is assigned by the
 * insert, so N items cost N prepare/execute round trips where EF Core's batch costs one
 * (6 assigned-id rows -> 1 statement, 6 IDENTITY rows -> 6; docs/research/2026-08-21-measured.md
 * FINDING 1, unchanged on the Hibernate 8.0 branch). Setting hibernate.jdbc.batch_size would
 * declare a knob that provably does nothing for this entity. Disclosed in docs/FAIRNESS.md.
 */
@Entity
@Table(name = "invoice_items")
public class InvoiceItem {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "id", nullable = false)
    private @Nullable Long id;

    @Column(name = "invoice_id", nullable = false)
    private long invoiceId;

    @Column(name = "sku", nullable = false)
    private String sku;

    @Column(name = "description", nullable = false)
    private String description;

    @Column(name = "qty", nullable = false)
    private int qty;

    @Column(name = "unit_price_minor", nullable = false)
    private long unitPriceMinor;

    @Column(name = "line_total_minor", nullable = false)
    private long lineTotalMinor;

    public @Nullable Long getId() {
        return id;
    }

    public long getInvoiceId() {
        return invoiceId;
    }

    public void setInvoiceId(long invoiceId) {
        this.invoiceId = invoiceId;
    }

    public String getSku() {
        return sku;
    }

    public void setSku(String sku) {
        this.sku = sku;
    }

    public String getDescription() {
        return description;
    }

    public void setDescription(String description) {
        this.description = description;
    }

    public int getQty() {
        return qty;
    }

    public void setQty(int qty) {
        this.qty = qty;
    }

    public long getUnitPriceMinor() {
        return unitPriceMinor;
    }

    public void setUnitPriceMinor(long unitPriceMinor) {
        this.unitPriceMinor = unitPriceMinor;
    }

    public long getLineTotalMinor() {
        return lineTotalMinor;
    }

    public void setLineTotalMinor(long lineTotalMinor) {
        this.lineTotalMinor = lineTotalMinor;
    }
}
