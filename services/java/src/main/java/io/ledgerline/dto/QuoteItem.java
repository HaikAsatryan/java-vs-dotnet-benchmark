package io.ledgerline.dto;

import io.ledgerline.problem.ValidationStrings;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import org.jspecify.annotations.Nullable;

public record QuoteItem(
        @NotBlank(message = ValidationStrings.REQUIRED)
        @Pattern(regexp = "^[A-Z]{2}-[0-9]{4}$", message = ValidationStrings.SKU_PATTERN)
        @Nullable String sku,

        @NotNull(message = ValidationStrings.QTY_RANGE)
        @Min(value = 1, message = ValidationStrings.QTY_RANGE)
        @Max(value = 1000, message = ValidationStrings.QTY_RANGE)
        @Nullable Integer qty,

        @Min(value = 0, message = ValidationStrings.UNIT_PRICE_RANGE)
        @Max(value = 100_000_000, message = ValidationStrings.UNIT_PRICE_RANGE)
        @Nullable Long unitPriceMinor) {

    public QuoteItem {
        unitPriceMinor = unitPriceMinor == null ? 0L : unitPriceMinor;
    }
}
