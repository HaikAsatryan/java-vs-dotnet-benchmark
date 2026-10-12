package io.ledgerline.data.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;
import org.hibernate.annotations.Generated;
import org.hibernate.generator.EventType;
import org.jspecify.annotations.Nullable;

/**
 * spec/schema.sql: invoices.id is GENERATED ALWAYS AS IDENTITY and created_at DEFAULT now().
 *
 * <p>Both FKs are scalar, and only the customer one is navigation-free on the .NET side too -
 * {@code LedgerDbContext} does map {@code Invoice.Items}, and CreateAsync inserts the graph in one
 * SaveChangesAsync. Mirroring that collection costs round trips rather than saving them: measured,
 * the closest JPA equivalent inserts each item without the FK and then issues N UPDATEs to fill it
 * in, 1 + 2N against the scalar model's 1 + N (SchemaMappingTest).
 */
@Entity
@Table(name = "invoices")
public class Invoice {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "id", nullable = false)
    private @Nullable Long id;

    @Column(name = "customer_id", nullable = false)
    private long customerId;

    @Column(name = "status", nullable = false)
    private String status;

    // char(3), ISO 4217.
    @Column(name = "currency", nullable = false)
    private String currency;

    @Column(name = "total_minor", nullable = false)
    private long totalMinor;

    // @Generated(INSERT) is what brings the DB's DEFAULT now() back in the INSERT's own
    // RETURNING clause instead of a second SELECT (Q4 semantics, spec/db-access.sql).
    // updatable=false is load-bearing and invisible in review: @Generated(INSERT) alone leaves
    // the column in later UPDATE statements, so any dirty update would overwrite the DB
    // timestamp with the in-memory value. SchemaMappingTest asserts it.
    @Generated(event = EventType.INSERT)
    @Column(name = "created_at", nullable = false, updatable = false)
    private @Nullable Instant createdAt;

    public @Nullable Long getId() {
        return id;
    }

    public long getCustomerId() {
        return customerId;
    }

    public void setCustomerId(long customerId) {
        this.customerId = customerId;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public String getCurrency() {
        return currency;
    }

    public void setCurrency(String currency) {
        this.currency = currency;
    }

    public long getTotalMinor() {
        return totalMinor;
    }

    public void setTotalMinor(long totalMinor) {
        this.totalMinor = totalMinor;
    }

    public @Nullable Instant getCreatedAt() {
        return createdAt;
    }
}
