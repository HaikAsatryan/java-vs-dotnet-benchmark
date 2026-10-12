package io.ledgerline.dto;

public record InvoiceItemResponse(
        long id,
        String sku,
        String description,
        int qty,
        long unitPriceMinor,
        long lineTotalMinor) {
}
