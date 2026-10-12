package io.ledgerline.dto;

import io.ledgerline.problem.ValidationStrings;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import org.jspecify.annotations.Nullable;

/**
 * Numerics are BOXED and carry the RANGE message on {@code @NotNull}, because an absent field
 * binds the CLR default 0 on the .NET side and fails its {@code [Range]} (captured). A primitive
 * component would instead make absence a Jackson binding failure and emit the body-level 400.
 * {@code unitPriceMinor} has no {@code @NotNull} at all for the same reason read backwards - 0
 * passes its range - and coalescing here is what makes both stacks STORE the same row.
 *
 * <p>An EXPLICIT null is the case this shape cannot reach, because Jackson cannot tell it from
 * absence for a creator property. Disclosed in docs/FAIRNESS.md, asserted in ErrorContractTest.
 *
 * <p>{@code @NotBlank} rather than {@code @NotNull} on the strings: {@code [Required]} defaults to
 * {@code AllowEmptyStrings=false} and rejects {@code ""} and {@code "   "} alike.
 */
public record CreateInvoiceItem(
        @NotBlank(message = ValidationStrings.REQUIRED)
        @Pattern(regexp = "^[A-Z]{2}-[0-9]{4}$", message = ValidationStrings.SKU_PATTERN)
        @Nullable String sku,

        @NotBlank(message = ValidationStrings.REQUIRED)
        @Size(min = 1, max = 200, message = ValidationStrings.DESCRIPTION_LENGTH)
        @Nullable String description,

        @NotNull(message = ValidationStrings.QTY_RANGE)
        @Min(value = 1, message = ValidationStrings.QTY_RANGE)
        @Max(value = 1000, message = ValidationStrings.QTY_RANGE)
        @Nullable Integer qty,

        @Min(value = 0, message = ValidationStrings.UNIT_PRICE_RANGE)
        @Max(value = 100_000_000, message = ValidationStrings.UNIT_PRICE_RANGE)
        @Nullable Long unitPriceMinor) {

    public CreateInvoiceItem {
        unitPriceMinor = unitPriceMinor == null ? 0L : unitPriceMinor;
    }
}
