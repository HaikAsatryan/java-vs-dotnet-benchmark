package io.ledgerline.observability;

/**
 * The six frozen {@code endpoint_calls} routes of spec/runtime-stats.md, in the order
 * {@link RuntimeStatsWriter} writes them. The per-endpoint warmup gate reads these counters, so
 * the set is the contract, not a convenience.
 */
public enum EndpointKind {

    GET_INVOICE("GET /invoices/{id}"),
    LIST_INVOICES("GET /customers/{id}/invoices"),
    PRICING_QUOTE("POST /pricing/quote"),
    CREATE_INVOICE("POST /invoices"),
    PDF_STUB("POST /invoices/{id}/pdf-stub"),
    HEALTHZ("GET /healthz");

    private final String route;

    EndpointKind(String route) {
        this.route = route;
    }

    public String route() {
        return route;
    }
}
