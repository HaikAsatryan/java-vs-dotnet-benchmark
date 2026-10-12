package io.ledgerline.web;

import io.ledgerline.dto.QuoteRequest;
import io.ledgerline.dto.QuoteResponse;
import io.ledgerline.infrastructure.RequestContext;
import io.ledgerline.logging.LedgerLog;
import io.ledgerline.observability.EndpointKind;
import io.ledgerline.observability.RuntimeStatsCollector;
import io.ledgerline.pricing.PricingEngine;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

@RestController
class PricingController {

    private static final Logger LOGGER = LoggerFactory.getLogger(PricingController.class);

    private final PricingEngine engine;
    private final RuntimeStatsCollector stats;

    PricingController(PricingEngine engine, RuntimeStatsCollector stats) {
        this.engine = engine;
        this.stats = stats;
    }

    /** No DB, no Redis: the CPU-only endpoint of the mix. */
    @PostMapping("/pricing/quote")
    QuoteResponse quote(@Valid @RequestBody QuoteRequest request, HttpServletRequest http) {
        stats.recordEndpointCall(EndpointKind.PRICING_QUOTE);
        QuoteResponse response = engine.quote(request);
        LedgerLog.quoteComputed(LOGGER, request.items().size(), response.totalMinor(),
                RequestContext.requestId(http));
        return response;
    }
}
