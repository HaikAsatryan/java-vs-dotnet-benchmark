namespace Ledgerline.Money;

/// int64 minor-unit helpers. No floats, no decimal, anywhere in money paths.
public static class Money
{
   /// bps_apply(amount, bps) = (amount * bps + 5000) / 10000  (int64 truncating division).
   /// The +5000 term gives round-half-up on non-negative values. Frozen by spec/pricing-algorithm.md.
   public static long BpsApply(long amount, long bps) => (amount * bps + 5000) / 10000;
}
