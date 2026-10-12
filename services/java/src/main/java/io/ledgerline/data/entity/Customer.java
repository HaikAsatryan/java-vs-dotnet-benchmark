package io.ledgerline.data.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/**
 * spec/schema.sql: customers.id is app-assigned (the seeder writes explicit ids 1..N for the
 * dense keyspace the Zipf selector needs), so there is no @GeneratedValue here. The mapping is
 * the whole class: {@code @Id} on the field puts Hibernate in field access, and the two
 * statements that touch this table (Q3b/Q7 and Q6) are native, so nothing reads or writes an
 * instance.
 */
@Entity
@Table(name = "customers")
public class Customer {

    @Id
    @Column(name = "id", nullable = false)
    private long id;

    @Column(name = "name", nullable = false)
    private String name;

    @Column(name = "email", nullable = false)
    private String email;

    @Column(name = "balance_minor", nullable = false)
    private long balanceMinor;

    // char(3), ISO 4217. Money is bigint minor units end-to-end, never BigDecimal.
    @Column(name = "currency", nullable = false)
    private String currency;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;
}
