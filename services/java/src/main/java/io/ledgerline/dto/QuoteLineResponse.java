package io.ledgerline.dto;

public record QuoteLineResponse(
        String sku,
        String category,
        int qty,
        long unitPriceMinor,
        long categorySurchargeMinor,
        long qtyDiscountMinor,
        long lineTotalMinor) {
}
