package io.ledgerline.dto;

import java.util.List;

/**
 * No total-count field: spec/openapi.yaml pins its absence because a count would force a second
 * COUNT query the statement ledger does not carry. {@code items} is order-significant.
 */
public record InvoiceListPageResponse(
        long customerId,
        int page,
        int pageSize,
        List<InvoiceSummaryResponse> items) {

    /** spec/openapi.yaml InvoiceListPage.page_size, and a const check in golden/shapes.json. */
    public static final int PAGE_SIZE = 20;
}
