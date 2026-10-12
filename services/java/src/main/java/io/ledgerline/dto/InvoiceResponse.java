package io.ledgerline.dto;

import java.time.Instant;
import java.util.List;

/**
 * {@code created_at} is an {@link Instant}, not the preformatted String the .NET side carries:
 * {@link io.ledgerline.json.InstantValueSerializer} applies the frozen format to every Instant on
 * every body, so a response type added later cannot format a timestamp some other way. Instant
 * rather than OffsetDateTime because it is zone-free. {@code items} is order-significant.
 */
public record InvoiceResponse(
        long id,
        long customerId,
        String status,
        String currency,
        long totalMinor,
        Instant createdAt,
        List<InvoiceItemResponse> items) {
}
