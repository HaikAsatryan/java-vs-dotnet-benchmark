package io.ledgerline.web;

import io.ledgerline.data.InvoiceHeader;
import io.ledgerline.data.InvoiceItemRow;
import io.ledgerline.data.entity.Invoice;
import io.ledgerline.data.entity.InvoiceItem;
import io.ledgerline.dto.InvoiceItemResponse;
import io.ledgerline.dto.InvoiceResponse;
import io.ledgerline.dto.InvoiceSummaryResponse;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/** Row-to-wire mapping, the work {@code EfInvoiceRepository} does inline in each method. */
final class InvoiceResponses {

    private InvoiceResponses() {
    }

    static InvoiceResponse fromRows(InvoiceHeader header, List<InvoiceItemRow> rows) {
        List<InvoiceItemResponse> items = new ArrayList<>(rows.size());
        for (InvoiceItemRow row : rows) {
            items.add(new InvoiceItemResponse(row.id(), row.sku(), row.description(), row.qty(),
                    row.unitPriceMinor(), row.lineTotalMinor()));
        }
        return new InvoiceResponse(header.id(), header.customerId(), header.status(),
                header.currency(), header.totalMinor(), header.createdAt(), items);
    }

    static InvoiceSummaryResponse summary(InvoiceHeader header) {
        return new InvoiceSummaryResponse(header.id(), header.customerId(), header.status(),
                header.currency(), header.totalMinor(), header.createdAt());
    }

    /**
     * The 201 body, built from the entities the transaction just wrote. Item ids are the REAL
     * DB-generated ones (pinned at integration, spec/db-access.md) and the ids are ascending by
     * construction; the sort mirrors {@code invoice.Items.OrderBy(it => it.Id)} rather than
     * relying on that.
     */
    static InvoiceResponse fromEntities(Invoice invoice, List<InvoiceItem> lines) {
        List<InvoiceItemResponse> items = new ArrayList<>(lines.size());
        for (InvoiceItem line : lines) {
            items.add(new InvoiceItemResponse(Objects.requireNonNull(line.getId()), line.getSku(),
                    line.getDescription(), line.getQty(), line.getUnitPriceMinor(),
                    line.getLineTotalMinor()));
        }
        items.sort((a, b) -> Long.compare(a.id(), b.id()));
        return new InvoiceResponse(Objects.requireNonNull(invoice.getId()),
                invoice.getCustomerId(), invoice.getStatus(), invoice.getCurrency(),
                invoice.getTotalMinor(), Objects.requireNonNull(invoice.getCreatedAt()), items);
    }
}
