using Ledgerline.Dtos;
using Ledgerline.Pricing;
using Xunit;

namespace Ledgerline.Tests;

public class PricingTests
{
   private readonly PricingEngine _engine = new();

   // --- bps_apply frozen rounding (spec/pricing-algorithm.md) ---

   [Theory]
   [InlineData(100000, 150, 1500)]    // Example A surcharge
   [InlineData(100000, 200, 2000)]    // Example A qty tier
   [InlineData(1000000, 1200, 120000)] // Example B qty tier
   [InlineData(999, 500, 50)]          // Example C HZ surcharge (true 49.95, half-up)
   [InlineData(10000000, 300, 300000)] // Example C LX surcharge
   [InlineData(10000000, 800, 800000)] // Example C LX qty tier
   [InlineData(0, 1200, 0)]
   [InlineData(1000000, 0, 0)]
   public void BpsApply_MatchesFrozenExpression(long amount, long bps, long expected) =>
      Assert.Equal(expected, Ledgerline.Money.Money.BpsApply(amount, bps));

   // --- Golden fixtures: examples A, B, C (relax minItems for unit-level engine tests) ---

   [Fact]
   public void ExampleA_Electronics_MidQtyTier()
   {
      var req = new QuoteRequest
      {
         Currency = "USD",
         Items = [new QuoteItem { Sku = "EL-0042", Qty = 10, UnitPriceMinor = 10000 }],
      };

      var r = _engine.Quote(req);
      var line = Assert.Single(r.Lines);
      Assert.Equal("EL", line.Category);
      Assert.Equal(1500, line.CategorySurchargeMinor);
      Assert.Equal(2000, line.QtyDiscountMinor);
      Assert.Equal(99500, line.LineTotalMinor);
      Assert.Equal(99500, r.SubtotalMinor);
      Assert.Equal(0, r.OrderDiscountMinor);
      Assert.Equal(99500, r.TotalMinor);
   }

   [Fact]
   public void ExampleB_UnknownCategory_TopQtyTier()
   {
      var req = new QuoteRequest
      {
         Currency = "USD",
         Items = [new QuoteItem { Sku = "ZZ-1234", Qty = 200, UnitPriceMinor = 5000 }],
      };

      var r = _engine.Quote(req);
      var line = Assert.Single(r.Lines);
      Assert.Equal("ZZ", line.Category);
      Assert.Equal(0, line.CategorySurchargeMinor);
      Assert.Equal(120000, line.QtyDiscountMinor);
      Assert.Equal(880000, line.LineTotalMinor);
      Assert.Equal(880000, r.SubtotalMinor);
      Assert.Equal(0, r.OrderDiscountMinor);
      Assert.Equal(880000, r.TotalMinor);
   }

   [Fact]
   public void ExampleC_TwoItems_RoundingNearOrderThreshold()
   {
      var req = new QuoteRequest
      {
         Currency = "USD",
         Items =
         [
            new QuoteItem { Sku = "HZ-0007", Qty = 3, UnitPriceMinor = 333 },
            new QuoteItem { Sku = "LX-9999", Qty = 100, UnitPriceMinor = 100000 },
         ],
      };

      var r = _engine.Quote(req);
      Assert.Equal(2, r.Lines.Count);

      Assert.Equal("HZ", r.Lines[0].Category);
      Assert.Equal(50, r.Lines[0].CategorySurchargeMinor);
      Assert.Equal(0, r.Lines[0].QtyDiscountMinor);
      Assert.Equal(1049, r.Lines[0].LineTotalMinor);

      Assert.Equal("LX", r.Lines[1].Category);
      Assert.Equal(300000, r.Lines[1].CategorySurchargeMinor);
      Assert.Equal(800000, r.Lines[1].QtyDiscountMinor);
      Assert.Equal(9500000, r.Lines[1].LineTotalMinor);

      Assert.Equal(9501049, r.SubtotalMinor);
      Assert.Equal(0, r.OrderDiscountMinor);
      Assert.Equal(9501049, r.TotalMinor);
   }

   // --- Order-discount ladder + filler item ($0 GR contributes nothing) ---

   [Fact]
   public void FillerItem_ContributesZero()
   {
      var filler = new QuoteItem { Sku = "GR-0001", Qty = 1, UnitPriceMinor = 0 };
      var req = new QuoteRequest { Currency = "USD", Items = [filler] };

      var r = _engine.Quote(req);
      Assert.Equal(0, r.Lines[0].CategorySurchargeMinor);
      Assert.Equal(0, r.Lines[0].QtyDiscountMinor);
      Assert.Equal(0, r.Lines[0].LineTotalMinor);
      Assert.Equal(0, r.SubtotalMinor);
      Assert.Equal(0, r.TotalMinor);
   }

   [Theory]
   [InlineData(9_999_999, 0)]
   [InlineData(10_000_000, 100)]
   [InlineData(49_999_999, 100)]
   [InlineData(50_000_000, 250)]
   [InlineData(199_999_999, 250)]
   [InlineData(200_000_000, 400)]
   public void OrderDiscount_Ladder(long targetSubtotal, long expectedBps)
   {
      // Construct a single item whose line_total equals targetSubtotal (GR=0 surcharge, qty 1 -> no discount).
      var req = new QuoteRequest
      {
         Currency = "USD",
         Items = [new QuoteItem { Sku = "GR-0001", Qty = 1, UnitPriceMinor = targetSubtotal }],
      };

      var r = _engine.Quote(req);
      Assert.Equal(targetSubtotal, r.SubtotalMinor);
      Assert.Equal(Ledgerline.Money.Money.BpsApply(targetSubtotal, expectedBps), r.OrderDiscountMinor);
   }
}
