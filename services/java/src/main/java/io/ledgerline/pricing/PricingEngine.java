package io.ledgerline.pricing;

import io.ledgerline.dto.QuoteItem;
import io.ledgerline.dto.QuoteLineResponse;
import io.ledgerline.dto.QuoteRequest;
import io.ledgerline.dto.QuoteResponse;
import io.ledgerline.money.Money;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Component;

/** The frozen tiered pricing of spec/pricing-algorithm.md. No DB, no Redis, no floats. */
@Component
public class PricingEngine {

    // Unknown category -> 0 bps (the default bucket, NOT an error).
    private static final Map<String, Long> SURCHARGE_BPS_BY_CATEGORY = Map.ofEntries(
            Map.entry("EL", 150L),
            Map.entry("GR", 0L),
            Map.entry("HZ", 500L),
            Map.entry("LX", 300L),
            Map.entry("DG", 25L),
            Map.entry("FU", 120L),
            Map.entry("MD", 80L),
            Map.entry("AP", 60L),
            Map.entry("BK", 10L),
            Map.entry("TL", 90L));

    public QuoteResponse quote(QuoteRequest request) {
        List<QuoteItem> items = request.items();
        List<QuoteLineResponse> lines = new ArrayList<>(items.size());
        long subtotalMinor = 0;

        for (QuoteItem item : items) {
            int qty = item.qty();
            long lineSubtotal = (long) qty * item.unitPriceMinor();
            String category = item.sku().substring(0, 2);
            long categorySurchargeMinor =
                    Money.bpsApply(lineSubtotal,
                            SURCHARGE_BPS_BY_CATEGORY.getOrDefault(category, 0L));
            long qtyDiscountMinor = Money.bpsApply(lineSubtotal, qtyDiscountBps(qty));
            long lineTotalMinor = lineSubtotal + categorySurchargeMinor - qtyDiscountMinor;

            lines.add(new QuoteLineResponse(item.sku(), category, qty, item.unitPriceMinor(),
                    categorySurchargeMinor, qtyDiscountMinor, lineTotalMinor));
            subtotalMinor += lineTotalMinor;
        }

        long orderDiscountMinor = Money.bpsApply(subtotalMinor, orderDiscountBps(subtotalMinor));
        return new QuoteResponse(request.currency(), lines, subtotalMinor, orderDiscountMinor,
                subtotalMinor - orderDiscountMinor);
    }

    private static long qtyDiscountBps(int qty) {
        if (qty >= 200) {
            return 1200;
        }
        if (qty >= 100) {
            return 800;
        }
        if (qty >= 50) {
            return 500;
        }
        return qty >= 10 ? 200 : 0;
    }

    private static long orderDiscountBps(long subtotalMinor) {
        if (subtotalMinor >= 200_000_000L) {
            return 400;
        }
        if (subtotalMinor >= 50_000_000L) {
            return 250;
        }
        return subtotalMinor >= 10_000_000L ? 100 : 0;
    }
}
