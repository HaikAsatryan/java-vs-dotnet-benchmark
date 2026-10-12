package io.ledgerline.data;

/** The Q2 row: the columns spec/db-access.sql selects, in that order. */
public record InvoiceItemRow(
        long id,
        String sku,
        String description,
        int qty,
        long unitPriceMinor,
        long lineTotalMinor) {
}
