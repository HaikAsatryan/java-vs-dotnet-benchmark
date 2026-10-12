package io.ledgerline.dto;

public record PdfStubResponse(
        long invoiceId,
        int bytesWritten,
        String path) {
}
