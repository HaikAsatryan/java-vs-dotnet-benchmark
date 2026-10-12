using System.ComponentModel.DataAnnotations;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Validation;

namespace Ledgerline.Dtos;

// Validation messages are the FROZEN x-validation-messages from spec/openapi.yaml, used
// verbatim as ErrorMessage. The errors-map KEY carries the field path; the VALUE is the
// bare frozen predicate string (no field name interpolation).

[ValidatableType]
public sealed class CreateInvoiceItem
{
   [JsonPropertyName("sku")]
   [Required(ErrorMessage = "is required")]
   // Braces doubled: DataAnnotations runs string.Format on ErrorMessage, so {2}/{4} would be
   // treated as positional args. Doubling emits the literal frozen text "must match ^[A-Z]{2}-[0-9]{4}$".
   [RegularExpression("^[A-Z]{2}-[0-9]{4}$", ErrorMessage = "must match ^[A-Z]{{2}}-[0-9]{{4}}$")]
   public string Sku { get; set; } = default!;

   [JsonPropertyName("description")]
   [Required(ErrorMessage = "is required")]
   [StringLength(200, MinimumLength = 1, ErrorMessage = "must be between 1 and 200 characters")]
   public string Description { get; set; } = default!;

   [JsonPropertyName("qty")]
   [Range(1, 1000, ErrorMessage = "must be between 1 and 1000")]
   public int Qty { get; set; }

   [JsonPropertyName("unit_price_minor")]
   [Range(0L, 100000000L, ErrorMessage = "must be between 0 and 100000000")]
   public long UnitPriceMinor { get; set; }
}

[ValidatableType]
public sealed class CreateInvoiceRequest
{
   [JsonPropertyName("customer_id")]
   [Range(1L, long.MaxValue, ErrorMessage = "must be a positive integer")]
   public long CustomerId { get; set; }

   [JsonPropertyName("currency")]
   [Required(ErrorMessage = "is required")]
   [RegularExpression("^[A-Z]{3}$", ErrorMessage = "must match ^[A-Z]{{3}}$")]
   public string Currency { get; set; } = default!;

   [JsonPropertyName("status")]
   [Required(ErrorMessage = "is required")]
   [RegularExpression("^(draft|open)$", ErrorMessage = "must be one of: draft, open")]
   public string Status { get; set; } = default!;

   [JsonPropertyName("items")]
   // No initializer: an ABSENT items key must bind to null so [Required] emits
   // "is required" (with `= []` it bound to an empty list and emitted the count
   // message instead, where the frozen contract and the Java side say "is required").
   [Required(ErrorMessage = "is required")]
   [MinLength(3, ErrorMessage = "must contain between 3 and 8 items")]
   [MaxLength(8, ErrorMessage = "must contain between 3 and 8 items")]
   public List<CreateInvoiceItem> Items { get; set; } = default!;
}
