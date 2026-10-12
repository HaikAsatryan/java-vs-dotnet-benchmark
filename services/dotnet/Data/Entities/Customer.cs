namespace Ledgerline.Data.Entities;

public sealed class Customer
{
   public long Id { get; set; }                  // app-assigned (seeder writes explicit ids)
   public string Name { get; set; } = default!;
   public string Email { get; set; } = default!;
   public long BalanceMinor { get; set; }
   public string Currency { get; set; } = default!;   // char(3)
   public DateTime CreatedAt { get; set; }            // timestamptz
}
