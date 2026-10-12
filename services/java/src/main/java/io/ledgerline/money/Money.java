package io.ledgerline.money;

/** int64 minor-unit helpers, 1:1 with {@code services/dotnet/Money/Money.cs}. */
public final class Money {

    private Money() {
    }

    /**
     * {@code bps_apply(amount, bps) = (amount * bps + 5000) / 10000}, frozen verbatim by
     * spec/pricing-algorithm.md - the +5000 term is round-half-up on non-negative values, and
     * Java's {@code /} on longs truncates toward zero exactly as C#'s does. No BigDecimal, no
     * double: the golden vectors are exact integers.
     */
    public static long bpsApply(long amount, long bps) {
        return (amount * bps + 5000) / 10000;
    }
}
