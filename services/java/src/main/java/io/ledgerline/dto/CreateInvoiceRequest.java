package io.ledgerline.dto;

import io.ledgerline.problem.ValidationStrings;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.util.List;
import org.jspecify.annotations.Nullable;

/**
 * The frozen messages are written verbatim into {@code message = ...}; see
 * {@link ValidationStrings} for why the regex quantifiers need no brace doubling here and do need
 * it in {@code services/dotnet/Dtos/CreateInvoiceRequest.cs}.
 *
 * <p>{@code customerId}'s {@code @NotNull} carries the RANGE message: .NET's non-nullable
 * {@code long CustomerId} binds 0 when the key is absent and emits
 * {@code "must be a positive integer"} (captured). Same reason as {@link CreateInvoiceItem}.
 */
public record CreateInvoiceRequest(
        @NotNull(message = ValidationStrings.ID_POSITIVE)
        @Min(value = 1, message = ValidationStrings.ID_POSITIVE)
        @Nullable Long customerId,

        @NotBlank(message = ValidationStrings.REQUIRED)
        @Pattern(regexp = "^[A-Z]{3}$", message = ValidationStrings.CURRENCY_PATTERN)
        @Nullable String currency,

        @NotBlank(message = ValidationStrings.REQUIRED)
        @Pattern(regexp = "^(draft|open)$", message = ValidationStrings.STATUS_ENUM)
        @Nullable String status,

        @NotNull(message = ValidationStrings.REQUIRED)
        @Size(min = 3, max = 8, message = ValidationStrings.ITEMS_CREATE_COUNT)
        @Nullable List<@Valid CreateInvoiceItem> items) {
}
