namespace Ledgerline.Data.Entities;

public sealed class Invoice
{
   public long Id { get; set; }                  // GENERATED ALWAYS AS IDENTITY
   public long CustomerId { get; set; }
   public string Status { get; set; } = default!;
   public string Currency { get; set; } = default!;   // char(3)
   public long TotalMinor { get; set; }
   public DateTime CreatedAt { get; set; }            // timestamptz DEFAULT now()

   public List<InvoiceItem> Items { get; set; } = [];
}
