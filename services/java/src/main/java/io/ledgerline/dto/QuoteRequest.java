package io.ledgerline.dto;

import io.ledgerline.problem.ValidationStrings;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.util.List;
import org.jspecify.annotations.Nullable;

public record QuoteRequest(
        @NotBlank(message = ValidationStrings.REQUIRED)
        @Pattern(regexp = "^[A-Z]{3}$", message = ValidationStrings.CURRENCY_PATTERN)
        @Nullable String currency,

        @NotNull(message = ValidationStrings.REQUIRED)
        @Size(min = 50, max = 50, message = ValidationStrings.ITEMS_QUOTE_COUNT)
        @Nullable List<@Valid QuoteItem> items) {
}
