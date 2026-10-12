using System.Text.Json.Serialization;

namespace Ledgerline.Dtos;

// Wire DTOs. JSON property names are snake_case EXACTLY as spec/openapi.yaml freezes them
// ("JSON property names: snake_case exactly as written here, both sides"). Money is int64
// (long) minor units end-to-end.

public sealed record InvoiceItemDto(
   [property: JsonPropertyName("id")] long Id,
   [property: JsonPropertyName("sku")] string Sku,
   [property: JsonPropertyName("description")] string Description,
   [property: JsonPropertyName("qty")] int Qty,
   [property: JsonPropertyName("unit_price_minor")] long UnitPriceMinor,
   [property: JsonPropertyName("line_total_minor")] long LineTotalMinor);

public sealed record InvoiceDto(
   [property: JsonPropertyName("id")] long Id,
   [property: JsonPropertyName("customer_id")] long CustomerId,
   [property: JsonPropertyName("status")] string Status,
   [property: JsonPropertyName("currency")] string Currency,
   [property: JsonPropertyName("total_minor")] long TotalMinor,
   [property: JsonPropertyName("created_at")] string CreatedAt,
   [property: JsonPropertyName("items")] IReadOnlyList<InvoiceItemDto> Items);

public sealed record InvoiceSummaryDto(
   [property: JsonPropertyName("id")] long Id,
   [property: JsonPropertyName("customer_id")] long CustomerId,
   [property: JsonPropertyName("status")] string Status,
   [property: JsonPropertyName("currency")] string Currency,
   [property: JsonPropertyName("total_minor")] long TotalMinor,
   [property: JsonPropertyName("created_at")] string CreatedAt);

public sealed record InvoiceListPageDto(
   [property: JsonPropertyName("customer_id")] long CustomerId,
   [property: JsonPropertyName("page")] int Page,
   [property: JsonPropertyName("page_size")] int PageSize,
   [property: JsonPropertyName("items")] IReadOnlyList<InvoiceSummaryDto> Items);

public sealed record PdfStubDto(
   [property: JsonPropertyName("invoice_id")] long InvoiceId,
   [property: JsonPropertyName("bytes_written")] int BytesWritten,
   [property: JsonPropertyName("path")] string Path);

public sealed record HealthDto(
   [property: JsonPropertyName("status")] string Status);
