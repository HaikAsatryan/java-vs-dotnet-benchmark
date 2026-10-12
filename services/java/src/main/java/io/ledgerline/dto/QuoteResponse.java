package io.ledgerline.dto;

import java.util.List;

/** {@code lines} is in input order and order-significant. */
public record QuoteResponse(
        String currency,
        List<QuoteLineResponse> lines,
        long subtotalMinor,
        long orderDiscountMinor,
        long totalMinor) {
}
