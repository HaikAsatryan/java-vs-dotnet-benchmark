package io.ledgerline.pricing;

import static org.assertj.core.api.Assertions.assertThat;

import io.ledgerline.GoldenVectors;
import io.ledgerline.dto.QuoteItem;
import io.ledgerline.dto.QuoteRequest;
import io.ledgerline.dto.QuoteResponse;
import io.ledgerline.money.Money;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.PropertyNamingStrategies;
import tools.jackson.databind.json.JsonMapper;

/**
 * The three committed engine vectors, plus the two tables the vectors only sample. The vectors
 * are the cross-service contract; the tables are where an off-by-one in a tier bound would hide,
 * because every worked example in spec/pricing-algorithm.md sits in the interior of its band.
 */
class PricingEngineTest {

    // SNAKE_CASE here only to reach the golden file's key spelling; that the SHIPPED mapper is
    // configured the same way is WireContractTest's assertion, not this one's.
    private static final JsonMapper MAPPER = JsonMapper.builder()
            .propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE)
            .build();

    private final PricingEngine engine = new PricingEngine();

    @ParameterizedTest
    @ValueSource(strings = {"quote_a", "quote_b", "quote_c"})
    void goldenEngineVectorsReproduceExactly(String vector) {
        JsonNode expected = GoldenVectors.load("pricing_quote/" + vector + "_engine.json");
        List<QuoteItem> items = expected.get("lines").valueStream()
                .map(line -> new QuoteItem(line.get("sku").stringValue(),
                        line.get("qty").intValue(), line.get("unit_price_minor").longValue()))
                .toList();

        QuoteResponse actual = engine.quote(
                new QuoteRequest(expected.get("currency").stringValue(), items));

        // Compared as text: the golden file's 1500 parses to an IntNode and the record's long
        // serializes to a LongNode, and Jackson's equals() separates those two.
        assertThat(GoldenVectors.normalize(MAPPER.valueToTree(actual)).toString())
                .isEqualTo(GoldenVectors.normalize(expected).toString());
    }

    /**
     * The rounding line spec/pricing-algorithm.md calls the most divergence-prone in the spec.
     * 999 x 500 bps is 49.95 exactly, which every half-up rule rounds to 50 and every
     * round-half-even rule rounds to 50 as well - so the interesting case is the .5 that
     * round-half-even would send DOWN, which is what 1 x 5000 bps covers.
     */
    @ParameterizedTest
    @CsvSource({
        "999, 500, 50",
        "1, 5000, 1",
        "100000, 150, 1500",
        "0, 1200, 0",
        "10000000, 300, 300000",
    })
    void bpsApplyRoundsHalfUpOnIntegers(long amount, long bps, long expected) {
        assertThat(Money.bpsApply(amount, bps)).isEqualTo(expected);
    }

    @ParameterizedTest
    @CsvSource({"1, 0", "9, 0", "10, 200", "49, 200", "50, 500", "99, 500",
                "100, 800", "199, 800", "200, 1200", "1000, 1200"})
    void quantityTierBoundsAreInclusiveOnTheLowerEdge(int qty, long expectedBps) {
        // Read the tier back through a line whose subtotal is 10000, so the discount is the bps
        // value itself and the assertion names the table entry rather than a derived amount.
        QuoteResponse response = quoteOne("GR-0001", qty, 10_000 / qty);
        long subtotal = (long) qty * (10_000 / qty);
        assertThat(response.lines().getFirst().qtyDiscountMinor())
                .isEqualTo(Money.bpsApply(subtotal, expectedBps));
    }

    @ParameterizedTest
    @CsvSource({"9999999, 0", "10000000, 100", "49999999, 100", "50000000, 250",
                "199999999, 250", "200000000, 400"})
    void orderThresholdBoundsAreInclusiveOnTheLowerEdge(long subtotal, long expectedBps) {
        // qty 1 and an unknown category keeps line_total == unit_price, so the order tier is
        // driven by exactly the value named.
        QuoteResponse response = quoteOne("ZZ-0001", 1, subtotal);
        assertThat(response.subtotalMinor()).isEqualTo(subtotal);
        assertThat(response.orderDiscountMinor())
                .isEqualTo(Money.bpsApply(subtotal, expectedBps));
    }

    @Test
    void anUnknownCategoryIsZeroBpsAndNotAnError() {
        assertThat(quoteOne("ZZ-1234", 200, 5000).lines().getFirst())
                .satisfies(line -> {
                    assertThat(line.category()).isEqualTo("ZZ");
                    assertThat(line.categorySurchargeMinor()).isZero();
                    assertThat(line.lineTotalMinor()).isEqualTo(880_000);
                });
    }

    @Test
    void theCategoryIsTheEchoedTwoLetterPrefixNotTheWholeSku() {
        assertThat(quoteOne("HZ-0007", 3, 333).lines().getFirst().category()).isEqualTo("HZ");
    }

    private QuoteResponse quoteOne(String sku, int qty, long unitPriceMinor) {
        return engine.quote(new QuoteRequest("USD",
                List.of(new QuoteItem(sku, qty, unitPriceMinor))));
    }
}
