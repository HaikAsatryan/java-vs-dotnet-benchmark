package io.ledgerline.web;

import io.ledgerline.data.CustomerRepository;
import io.ledgerline.data.InvoiceHeader;
import io.ledgerline.data.InvoiceRepository;
import io.ledgerline.dto.InvoiceListPageResponse;
import io.ledgerline.dto.InvoiceSummaryResponse;
import io.ledgerline.observability.EndpointKind;
import io.ledgerline.observability.RuntimeStatsCollector;
import io.ledgerline.problem.NotFoundException;
import io.ledgerline.problem.ValidationFailedException;
import io.ledgerline.problem.ValidationStrings;
import jakarta.servlet.http.HttpServletRequest;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.jspecify.annotations.Nullable;
import org.springframework.data.domain.PageRequest;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

@RestController
class CustomerController {

    private final InvoiceRepository invoices;
    private final CustomerRepository customers;
    private final RuntimeStatsCollector stats;

    CustomerController(InvoiceRepository invoices, CustomerRepository customers,
            RuntimeStatsCollector stats) {
        this.invoices = invoices;
        this.customers = customers;
        this.stats = stats;
    }

    @GetMapping("/customers/{id}/invoices")
    InvoiceListPageResponse list(@PathVariable String id, HttpServletRequest http) {
        stats.recordEndpointCall(EndpointKind.LIST_INVOICES);

        // 400 before 404, and the id and page failures AGGREGATED into one errors map
        // (services/dotnet/NOTES-equivalence.md). Read straight off the servlet request rather
        // than through a bound List<String>: Spring splits a single "1,2" query value into two
        // elements, where ASP.NET's Query["page"][0] keeps it as the one unparseable string.
        Map<String, List<String>> errors = new LinkedHashMap<>();
        Long customerId = RequestParams.parseId(id);
        if (customerId == null) {
            errors.put("id", List.of(ValidationStrings.ID_POSITIVE));
        }
        int page = RequestParams.parsePage(firstValue(http, "page"), errors);
        if (!errors.isEmpty()) {
            throw new ValidationFailedException(errors);
        }

        // The ORDER BY lives in the query, so the Pageable must stay UNSORTED - a Sort here
        // emits a second one. PageRequest, not Page: a Page would issue a COUNT the ledger
        // does not carry.
        List<InvoiceHeader> rows = invoices.findPageByCustomerId(customerId,
                PageRequest.of(page - 1, InvoiceListPageResponse.PAGE_SIZE));

        // Q3b fires ONLY on an empty page, so the happy path stays one query on both stacks.
        if (rows.isEmpty() && !customers.customerExists(customerId)) {
            throw new NotFoundException();
        }

        List<InvoiceSummaryResponse> items = new ArrayList<>(rows.size());
        for (InvoiceHeader row : rows) {
            items.add(InvoiceResponses.summary(row));
        }
        return new InvoiceListPageResponse(customerId, page, InvoiceListPageResponse.PAGE_SIZE,
                items);
    }

    private static @Nullable String firstValue(HttpServletRequest request, String name) {
        String[] values = request.getParameterValues(name);
        return values == null || values.length == 0 ? null : values[0];
    }
}
