namespace Ledgerline.Data.Entities;

public sealed class InvoiceItem
{
   public long Id { get; set; }                  // GENERATED ALWAYS AS IDENTITY
   public long InvoiceId { get; set; }
   public string Sku { get; set; } = default!;
   public string Description { get; set; } = default!;
   public int Qty { get; set; }
   public long UnitPriceMinor { get; set; }
   public long LineTotalMinor { get; set; }
}
