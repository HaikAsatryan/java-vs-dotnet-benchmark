using System.Collections.Frozen;
using Ledgerline.Dtos;
using Ledgerline.Money;

namespace Ledgerline.Pricing;

/// Frozen tiered pricing over int64 minor units (spec/pricing-algorithm.md).
/// Pure integer arithmetic; branch-heavy; map lookups. No floats, no decimal.
public sealed class PricingEngine
{
   // Category -> surcharge bps. Unknown category -> 0 (default bucket, not an error).
   private static readonly FrozenDictionary<string, long> SurchargeBpsByCategory =
      new Dictionary<string, long>
      {
         ["EL"] = 150,
         ["GR"] = 0,
         ["HZ"] = 500,
         ["LX"] = 300,
         ["DG"] = 25,
         ["FU"] = 120,
         ["MD"] = 80,
         ["AP"] = 60,
         ["BK"] = 10,
         ["TL"] = 90,
      }.ToFrozenDictionary();

   private static long QtyDiscountBps(int qty) => qty switch
   {
      >= 200 => 1200,
      >= 100 => 800,
      >= 50 => 500,
      >= 10 => 200,
      _ => 0,
   };

   private static long OrderDiscountBps(long subtotalMinor) => subtotalMinor switch
   {
      >= 200_000_000 => 400,
      >= 50_000_000 => 250,
      >= 10_000_000 => 100,
      _ => 0,
   };

   public QuoteResponseDto Quote(QuoteRequest req)
   {
      var lines = new List<QuoteLineDto>(req.Items.Count);
      var subtotalMinor = 0L;

      foreach (var item in req.Items)
      {
         var lineSubtotal = (long)item.Qty * item.UnitPriceMinor;
         var category = item.Sku[..2];
         var surchargeBps = SurchargeBpsByCategory.TryGetValue(category, out var bps) ? bps : 0L;
         var categorySurchargeMinor = Money.Money.BpsApply(lineSubtotal, surchargeBps);
         var qtyDiscountMinor = Money.Money.BpsApply(lineSubtotal, QtyDiscountBps(item.Qty));
         var lineTotalMinor = lineSubtotal + categorySurchargeMinor - qtyDiscountMinor;

         lines.Add(new QuoteLineDto(
            item.Sku,
            category,
            item.Qty,
            item.UnitPriceMinor,
            categorySurchargeMinor,
            qtyDiscountMinor,
            lineTotalMinor));

         subtotalMinor += lineTotalMinor;
      }

      var orderDiscountMinor = Money.Money.BpsApply(subtotalMinor, OrderDiscountBps(subtotalMinor));
      var totalMinor = subtotalMinor - orderDiscountMinor;

      return new QuoteResponseDto(req.Currency, lines, subtotalMinor, orderDiscountMinor, totalMinor);
   }
}
