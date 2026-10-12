package io.ledgerline.web;

import io.ledgerline.cache.InvoiceCache;
import io.ledgerline.config.LedgerProperties;
import io.ledgerline.data.CustomerRepository;
import io.ledgerline.data.InvoiceHeader;
import io.ledgerline.data.InvoiceItemRepository;
import io.ledgerline.data.InvoiceRepository;
import io.ledgerline.data.InvoiceWriter;
import io.ledgerline.data.entity.Invoice;
import io.ledgerline.data.entity.InvoiceItem;
import io.ledgerline.dto.CreateInvoiceItem;
import io.ledgerline.dto.CreateInvoiceRequest;
import io.ledgerline.dto.InvoiceResponse;
import io.ledgerline.dto.PdfStubResponse;
import io.ledgerline.infrastructure.PdfStub;
import io.ledgerline.infrastructure.RequestContext;
import io.ledgerline.logging.LedgerLog;
import io.ledgerline.observability.EndpointKind;
import io.ledgerline.observability.RuntimeStatsCollector;
import io.ledgerline.problem.NotFoundException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.URI;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/invoices")
class InvoiceController {

    private static final Logger LOGGER = LoggerFactory.getLogger(InvoiceController.class);

    private final InvoiceRepository invoices;
    private final InvoiceItemRepository items;
    private final CustomerRepository customers;
    private final InvoiceWriter writer;
    private final InvoiceCache cache;
    private final RuntimeStatsCollector stats;
    private final String pdfDir;

    InvoiceController(InvoiceRepository invoices, InvoiceItemRepository items,
            CustomerRepository customers, InvoiceWriter writer, InvoiceCache cache,
            RuntimeStatsCollector stats, LedgerProperties properties) {
        this.invoices = invoices;
        this.items = items;
        this.customers = customers;
        this.writer = writer;
        this.cache = cache;
        this.stats = stats;
        this.pdfDir = properties.pdfDir();
    }

    /**
     * Cache-aside, then Q1+Q2. The response is the CACHED BYTES, returned without a parse, which
     * is what makes a hit byte-equal to a miss (spec/cache.md).
     */
    @GetMapping("/{id}")
    ResponseEntity<byte[]> get(@PathVariable String id) {
        stats.recordEndpointCall(EndpointKind.GET_INVOICE);
        long invoiceId = RequestParams.requireId(id);

        byte[] body = cache.getOrLoadBytes(invoiceId, () -> load(invoiceId));
        if (body == null) {
            throw new NotFoundException();
        }
        return ResponseEntity.ok().contentType(MediaType.APPLICATION_JSON).body(body);
    }

    @PostMapping
    ResponseEntity<InvoiceResponse> create(@Valid @RequestBody CreateInvoiceRequest request,
            HttpServletRequest http) {
        stats.recordEndpointCall(EndpointKind.CREATE_INVOICE);
        long customerId = request.customerId();
        // Q7, OUTSIDE the transaction: the frozen ledger is "existence/load (1) + BEGIN + ...",
        // and .NET probes before BeginTransactionAsync.
        if (!customers.customerExists(customerId)) {
            throw new NotFoundException();
        }

        Invoice invoice = new Invoice();
        invoice.setCustomerId(customerId);
        invoice.setStatus(request.status());
        invoice.setCurrency(request.currency());

        List<InvoiceItem> lines = new ArrayList<>(request.items().size());
        long totalMinor = 0;
        for (CreateInvoiceItem item : request.items()) {
            long lineTotal = (long) item.qty() * item.unitPriceMinor();
            totalMinor += lineTotal;

            InvoiceItem line = new InvoiceItem();
            line.setSku(item.sku());
            line.setDescription(item.description());
            line.setQty(item.qty());
            line.setUnitPriceMinor(item.unitPriceMinor());
            line.setLineTotalMinor(lineTotal);
            lines.add(line);
        }
        // Before the write, not after: IDENTITY makes persist() an immediate INSERT, so a
        // total assigned later would be a second, full-column UPDATE at flush.
        invoice.setTotalMinor(totalMinor);

        writer.create(invoice, lines);
        InvoiceResponse created = InvoiceResponses.fromEntities(invoice, lines);

        cache.invalidate(created.id());

        String requestId = RequestContext.requestId(http);
        LedgerLog.invoiceCreated(LOGGER, created.customerId(), created.id(), created.items().size(),
                created.totalMinor(), requestId);
        LedgerLog.cacheInvalidated(LOGGER, created.id(), requestId);

        return ResponseEntity.created(URI.create("/invoices/" + created.id())).body(created);
    }

    @PostMapping("/{id}/pdf-stub")
    PdfStubResponse pdfStub(@PathVariable String id, HttpServletRequest http) {
        stats.recordEndpointCall(EndpointKind.PDF_STUB);
        long invoiceId = RequestParams.requireId(id);
        // The Q1 header lookup ONLY - the items are never loaded here, on any data layer
        // (spec/db-access.md).
        if (invoices.findHeaderById(invoiceId).isEmpty()) {
            throw new NotFoundException();
        }

        int bytesWritten;
        try {
            bytesWritten = PdfStub.write(pdfDir, invoiceId);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }

        LedgerLog.pdfStubWritten(LOGGER, invoiceId, bytesWritten, RequestContext.requestId(http));
        return new PdfStubResponse(invoiceId, bytesWritten, PdfStub.pathFor(pdfDir, invoiceId));
    }

    /**
     * Q1 then Q2, two statements. Unlike EF, which opens the connection explicitly to keep both
     * on one acquisition, each repository call takes and returns its own pooled connection: a
     * HikariCP checkout is a local handoff with no reset statement, where an Npgsql checkout
     * sends one (spec/db-access.md). Disclosed rather than transacted away; what a read
     * transaction would cost instead is on {@link CustomerRepository#customerExists(long)}.
     */
    private @Nullable InvoiceResponse load(long invoiceId) {
        Optional<InvoiceHeader> header = invoices.findHeaderById(invoiceId);
        return header
                .map(found -> InvoiceResponses.fromRows(found, items.findByInvoiceId(invoiceId)))
                .orElse(null);
    }
}
