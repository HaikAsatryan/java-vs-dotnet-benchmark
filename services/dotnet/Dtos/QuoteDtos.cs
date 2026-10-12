using System.ComponentModel.DataAnnotations;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Validation;

namespace Ledgerline.Dtos;

[ValidatableType]
public sealed class QuoteItem
{
   [JsonPropertyName("sku")]
   [Required(ErrorMessage = "is required")]
   // Braces doubled so string.Format emits the literal frozen text (see CreateInvoiceItem.Sku).
   [RegularExpression("^[A-Z]{2}-[0-9]{4}$", ErrorMessage = "must match ^[A-Z]{{2}}-[0-9]{{4}}$")]
   public string Sku { get; set; } = default!;

   [JsonPropertyName("qty")]
   [Range(1, 1000, ErrorMessage = "must be between 1 and 1000")]
   public int Qty { get; set; }

   [JsonPropertyName("unit_price_minor")]
   [Range(0L, 100000000L, ErrorMessage = "must be between 0 and 100000000")]
   public long UnitPriceMinor { get; set; }
}

[ValidatableType]
public sealed class QuoteRequest
{
   [JsonPropertyName("currency")]
   [Required(ErrorMessage = "is required")]
   [RegularExpression("^[A-Z]{3}$", ErrorMessage = "must match ^[A-Z]{{3}}$")]
   public string Currency { get; set; } = default!;

   [JsonPropertyName("items")]
   // No initializer: absent -> null -> [Required] "is required" (see CreateInvoiceRequest.Items).
   [Required(ErrorMessage = "is required")]
   [MinLength(50, ErrorMessage = "must contain exactly 50 items")]
   [MaxLength(50, ErrorMessage = "must contain exactly 50 items")]
   public List<QuoteItem> Items { get; set; } = default!;
}

// Response DTOs (output order-significant: lines in input order).

public sealed record QuoteLineDto(
   [property: JsonPropertyName("sku")] string Sku,
   [property: JsonPropertyName("category")] string Category,
   [property: JsonPropertyName("qty")] int Qty,
   [property: JsonPropertyName("unit_price_minor")] long UnitPriceMinor,
   [property: JsonPropertyName("category_surcharge_minor")] long CategorySurchargeMinor,
   [property: JsonPropertyName("qty_discount_minor")] long QtyDiscountMinor,
   [property: JsonPropertyName("line_total_minor")] long LineTotalMinor);

public sealed record QuoteResponseDto(
   [property: JsonPropertyName("currency")] string Currency,
   [property: JsonPropertyName("lines")] IReadOnlyList<QuoteLineDto> Lines,
   [property: JsonPropertyName("subtotal_minor")] long SubtotalMinor,
   [property: JsonPropertyName("order_discount_minor")] long OrderDiscountMinor,
   [property: JsonPropertyName("total_minor")] long TotalMinor);
