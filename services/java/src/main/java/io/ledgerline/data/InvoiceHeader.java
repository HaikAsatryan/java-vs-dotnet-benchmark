package io.ledgerline.data;

import java.time.Instant;

/**
 * The Q1/Q3 row: exactly the columns spec/db-access.sql selects, in that order. Returned instead
 * of the entity, the analog of EF's {@code .AsNoTracking().Select(...)} - the persistence context
 * stays empty, so nothing dirty-checks, nothing flushes, and the reads need no transaction.
 */
public record InvoiceHeader(
        long id,
        long customerId,
        String status,
        String currency,
        long totalMinor,
        Instant createdAt) {
}
