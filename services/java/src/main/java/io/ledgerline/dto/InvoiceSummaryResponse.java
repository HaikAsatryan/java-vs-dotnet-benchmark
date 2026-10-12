package io.ledgerline.dto;

import java.time.Instant;

public record InvoiceSummaryResponse(
        long id,
        long customerId,
        String status,
        String currency,
        long totalMinor,
        Instant createdAt) {
}
