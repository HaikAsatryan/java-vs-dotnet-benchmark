using Ledgerline.Dtos;

namespace Ledgerline.Data;

/// Result of a create: either the created invoice, or a sentinel that the customer was absent
/// (so the endpoint can emit 404 without the repo coupling to HTTP).
public readonly record struct CreateResult(InvoiceDto? Invoice)
{
   public bool CustomerMissing => Invoice is null;
}

public interface IInvoiceRepository
{
   Task<InvoiceDto?> GetAsync(long id, CancellationToken ct);

   /// pdf-stub 404 probe: the Q1 header lookup ONLY (no items query); one statement,
   /// identically on every data layer and on the Java side (spec/db-access.md).
   Task<bool> InvoiceExistsAsync(long id, CancellationToken ct);

   /// Returns the page of summaries. CustomerExists is probed by the endpoint only when the
   /// page is empty (Q3b), so the happy path stays one query.
   Task<IReadOnlyList<InvoiceSummaryDto>> ListByCustomerAsync(long customerId, int page, CancellationToken ct);

   Task<bool> CustomerExistsAsync(long customerId, CancellationToken ct);

   /// Create path. Probes customer existence (Q7); if absent returns CustomerMissing.
   /// Otherwise: header insert (RETURNING id+created_at), N item inserts and the balance update
   /// inside one READ COMMITTED transaction (round-trip ledger per data layer: spec/db-access.md).
   /// Returns the full invoice for the 201 body.
   Task<CreateResult> CreateAsync(CreateInvoiceRequest req, CancellationToken ct);
}
