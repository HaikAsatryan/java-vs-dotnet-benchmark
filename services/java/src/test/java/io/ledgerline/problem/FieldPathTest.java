package io.ledgerline.problem;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

/**
 * The transform in isolation, with no Spring context: the whole reason it lives in its own class
 * rather than inside the advice. Left column is what Jakarta Bean Validation produces, right column
 * is what spec/openapi.yaml freezes and what .NET emits (captured in
 * {@code services/dotnet/NOTES-equivalence.md}).
 */
class FieldPathTest {

    @ParameterizedTest
    @CsvSource({
        "customerId,                  CustomerId",
        "currency,                    Currency",
        "status,                      Status",
        "items,                       Items",
        "items[0].sku,                Items[0].Sku",
        "items[0].description,        Items[0].Description",
        "items[2].qty,                Items[2].Qty",
        "items[12].unitPriceMinor,    Items[12].UnitPriceMinor",
        // Not reachable from these DTOs; the rule is per dot-segment, not per path.
        "a.b.c,                       A.B.C",
    })
    void capitalisesTheFirstCharacterOfEveryDotSegment(String jakartaPath, String frozenKey) {
        assertThat(FieldPath.toClrPath(jakartaPath)).isEqualTo(frozenKey);
    }
}
