"""Independent third implementation of the frozen pricing algorithm.

This is NOT used by either service. It exists so the golden fixtures (computed
BY HAND from spec/pricing-algorithm.md and committed under golden/) can be
cross-checked by an INDEPENDENT code path: the golden self-consistency test
recomputes each example with this module and asserts it equals the committed
golden integers. Two independent derivations agreeing is a strong guard that the
goldens are correct before any service is even built.

Pure int64 integer arithmetic, exactly as frozen in spec/pricing-algorithm.md.
No floats, no decimal, anywhere. Python ints are unbounded; the spec proves the
intermediate products stay well inside int64, so the math is identical to a
correct int64 implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Category -> surcharge bps (frozen, spec/pricing-algorithm.md). Unknown -> 0.
CATEGORY_SURCHARGE_BPS: dict[str, int] = {
    "EL": 150,
    "GR": 0,
    "HZ": 500,
    "LX": 300,
    "DG": 25,
    "FU": 120,
    "MD": 80,
    "AP": 60,
    "BK": 10,
    "TL": 90,
}

_SKU_RE = re.compile(r"^[A-Z]{2}-[0-9]{4}$")


def surcharge_bps(category: str) -> int:
    """Frozen category map; unknown category -> 0 bps (default bucket, not error)."""
    return CATEGORY_SURCHARGE_BPS.get(category, 0)


def qty_discount_bps(qty: int) -> int:
    """Frozen quantity tier table (lower bound inclusive)."""
    if qty >= 200:
        return 1200
    if qty >= 100:
        return 800
    if qty >= 50:
        return 500
    if qty >= 10:
        return 200
    return 0  # 1..9


def order_discount_bps(subtotal_minor: int) -> int:
    """Frozen order-level threshold ladder on subtotal_minor (lower bound incl.)."""
    if subtotal_minor >= 200_000_000:
        return 400
    if subtotal_minor >= 50_000_000:
        return 250
    if subtotal_minor >= 10_000_000:
        return 100
    return 0  # 0 .. 9,999,999


def bps_apply(amount: int, bps: int) -> int:
    """The single most divergence-prone line, frozen exactly:

        (amount * bps + 5000) / 10000   -- int64 TRUNCATING division

    `+5000` gives round-half-up on non-negative values. Implemented with Python
    floor division on non-negative operands (== truncation here since both
    operands are >= 0), matching the int64 truncating-division semantics.
    """
    return (amount * bps + 5000) // 10000


def sku_is_valid(sku: str) -> bool:
    return bool(_SKU_RE.match(sku))


@dataclass(frozen=True)
class QuoteLine:
    sku: str
    category: str
    qty: int
    unit_price_minor: int
    category_surcharge_minor: int
    qty_discount_minor: int
    line_total_minor: int


@dataclass(frozen=True)
class QuoteResult:
    currency: str
    lines: list[QuoteLine]
    subtotal_minor: int
    order_discount_minor: int
    total_minor: int


def compute_quote(currency: str, items: list[dict]) -> QuoteResult:
    """Apply the frozen per-item then order-level algorithm in input order.

    `items` is a list of {sku, qty, unit_price_minor}. This reference does NOT
    enforce the wire validation (exactly 50 items, sku pattern as a 400); it
    computes the pricing math for whatever well-formed items it is given so it
    can score both the unit-level worked examples (1-2 items) and the wire-level
    50-item padded goldens.
    """
    lines: list[QuoteLine] = []
    subtotal = 0
    for it in items:
        sku = it["sku"]
        qty = int(it["qty"])
        unit_price = int(it["unit_price_minor"])

        line_subtotal = qty * unit_price                      # step 1
        category = sku[0:2]                                   # step 2
        s_bps = surcharge_bps(category)
        category_surcharge = bps_apply(line_subtotal, s_bps)  # step 3
        d_bps = qty_discount_bps(qty)                          # step 4
        qty_discount = bps_apply(line_subtotal, d_bps)        # step 5
        line_total = line_subtotal + category_surcharge - qty_discount  # step 6

        lines.append(
            QuoteLine(
                sku=sku,
                category=category,
                qty=qty,
                unit_price_minor=unit_price,
                category_surcharge_minor=category_surcharge,
                qty_discount_minor=qty_discount,
                line_total_minor=line_total,
            )
        )
        subtotal += line_total

    o_bps = order_discount_bps(subtotal)
    order_discount = bps_apply(subtotal, o_bps)
    total = subtotal - order_discount

    return QuoteResult(
        currency=currency,
        lines=lines,
        subtotal_minor=subtotal,
        order_discount_minor=order_discount,
        total_minor=total,
    )


def line_to_dict(line: QuoteLine) -> dict:
    """Wire shape (QuoteLine schema, openapi.yaml)."""
    return {
        "sku": line.sku,
        "category": line.category,
        "qty": line.qty,
        "unit_price_minor": line.unit_price_minor,
        "category_surcharge_minor": line.category_surcharge_minor,
        "qty_discount_minor": line.qty_discount_minor,
        "line_total_minor": line.line_total_minor,
    }


def result_to_dict(result: QuoteResult) -> dict:
    """Full QuoteResponse wire body (openapi.yaml)."""
    return {
        "currency": result.currency,
        "lines": [line_to_dict(line) for line in result.lines],
        "subtotal_minor": result.subtotal_minor,
        "order_discount_minor": result.order_discount_minor,
        "total_minor": result.total_minor,
    }


# Frozen filler item for wire-level goldens (spec/pricing-algorithm.md note):
# GR = 0 bps, qty 1 -> tier 0, unit_price 0 -> contributes exactly 0 to every
# output amount. Used to pad worked examples to the wire minItems=50.
FILLER_ITEM = {"sku": "GR-0001", "qty": 1, "unit_price_minor": 0}


def pad_to_50(items: list[dict]) -> list[dict]:
    """Append the documented zero-contribution filler until len == 50."""
    if len(items) > 50:
        raise ValueError("worked example already exceeds 50 items")
    return list(items) + [dict(FILLER_ITEM) for _ in range(50 - len(items))]
