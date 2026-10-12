package io.ledgerline.web;

import io.ledgerline.dto.CreateInvoiceRequest;
import io.ledgerline.dto.HealthResponse;
import io.ledgerline.dto.InvoiceItemResponse;
import io.ledgerline.dto.InvoiceResponse;
import io.ledgerline.dto.QuoteRequest;
import io.ledgerline.problem.NotFoundException;
import io.ledgerline.problem.ValidationFailedException;
import io.ledgerline.problem.ValidationStrings;
import jakarta.validation.Valid;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Test-only routes that isolate binding and validation from the data layer, so that the error
 * contract is asserted from real emitted HTTP responses. It carries only what shapes an error
 * body: the two {@code @Valid @RequestBody} routes and the two hand-validated parameters. The
 * routes are under {@code /probe} rather than the frozen paths so that nothing here can be
 * mistaken for the service's routing table.
 *
 * <p>The parameter validation below mirrors the frozen behaviour spec/openapi.yaml
 * (Problem.errors "KEY FORMAT") and {@code services/dotnet/Endpoints/InvoiceEndpoints.cs} pin;
 * the service's own endpoints validate through {@code RequestParams}.
 */
@RestController
public class ContractProbeController {

    /** Echoes the BOUND items so a test can see what binding produced, not just that it passed. */
    @PostMapping("/probe/invoices")
    public InvoiceResponse createInvoice(@Valid @RequestBody CreateInvoiceRequest request) {
        List<InvoiceItemResponse> items = request.items().stream()
                .map(item -> new InvoiceItemResponse(0, item.sku(), item.description(),
                        item.qty(), item.unitPriceMinor(), 0))
                .toList();
        return new InvoiceResponse(1, request.customerId(), request.status(), request.currency(),
                0, Instant.EPOCH, items);
    }

    @PostMapping("/probe/quotes")
    public QuoteRequest quote(@Valid @RequestBody QuoteRequest request) {
        return request;
    }

    @GetMapping("/probe/invoices/{id}")
    public HealthResponse getInvoice(@PathVariable String id) {
        long invoiceId = parseId(id);
        if (invoiceId != 1) {
            throw new NotFoundException();
        }
        return HealthResponse.OK;
    }

    /**
     * One errors map carries BOTH keys when the id and the page are both invalid, and {@code page}
     * is read from the FIRST query value rather than a comma-joined list - both pinned in
     * {@code services/dotnet/NOTES-equivalence.md}.
     */
    @GetMapping("/probe/customers/{id}/invoices")
    public HealthResponse listInvoices(@PathVariable String id,
            @RequestParam(name = "page", required = false) List<String> page) {
        Map<String, List<String>> errors = new LinkedHashMap<>();
        if (!isPositiveLong(id)) {
            errors.put("id", List.of(ValidationStrings.ID_POSITIVE));
        }
        if (page != null && !page.isEmpty()) {
            errors.putAll(validatePage(page.getFirst()));
        }
        if (!errors.isEmpty()) {
            throw new ValidationFailedException(errors);
        }
        return HealthResponse.OK;
    }

    private static Map<String, List<String>> validatePage(String raw) {
        long value;
        try {
            value = Long.parseLong(raw);
        } catch (NumberFormatException e) {
            return Map.of("page", List.of(ValidationStrings.PAGE_MIN));
        }
        if (value < 1) {
            return Map.of("page", List.of(ValidationStrings.PAGE_MIN));
        }
        if (value > 100_000) {
            return Map.of("page", List.of(ValidationStrings.PAGE_MAX));
        }
        return Map.of();
    }

    private static long parseId(String raw) {
        if (!isPositiveLong(raw)) {
            throw new ValidationFailedException(
                    Map.of("id", List.of(ValidationStrings.ID_POSITIVE)));
        }
        return Long.parseLong(raw);
    }

    private static boolean isPositiveLong(String raw) {
        try {
            return Long.parseLong(raw) >= 1;
        } catch (NumberFormatException e) {
            return false;
        }
    }
}
