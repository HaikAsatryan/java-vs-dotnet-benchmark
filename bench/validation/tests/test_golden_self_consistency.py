"""Golden self-consistency: a THIRD independent derivation of the pricing math.

Three paths must agree for each worked example:
  (1) the SPEC hand integers, transcribed here as literals from
      spec/pricing-algorithm.md worked examples A/B/C,
  (2) the committed golden FILES under golden/pricing_quote/,
  (3) validation.pricing_reference recomputing from the algorithm.

If a golden file were wrong, (2) would disagree with (1) and (3); if the
reference were wrong, (3) would disagree with (1) and (2). Agreement of three
independent derivations is the gate, checked with no service running. Also
checks the frozen error envelopes against spec/openapi.yaml x-error-strings and
the wire filler-item zero-contribution invariant.
"""

from __future__ import annotations

import pytest

from validation import golden
from validation import pricing_reference as pr
from validation.normalize import normalize

# (1) SPEC hand integers, transcribed verbatim from spec/pricing-algorithm.md.
#     Each tuple: (sku, category, qty, unit_price, surcharge, qty_discount, line_total).
SPEC_EXAMPLES = {
    "quote_a": {
        "items": [("EL-0042", "EL", 10, 10000, 1500, 2000, 99500)],
        "subtotal": 99500, "order_discount": 0, "total": 99500,
    },
    "quote_b": {
        "items": [("ZZ-1234", "ZZ", 200, 5000, 0, 120000, 880000)],
        "subtotal": 880000, "order_discount": 0, "total": 880000,
    },
    "quote_c": {
        "items": [
            ("HZ-0007", "HZ", 3, 333, 50, 0, 1049),       # rounding: true 49.95 -> 50
            ("LX-9999", "LX", 100, 100000, 300000, 800000, 9500000),
        ],
        "subtotal": 9501049, "order_discount": 0, "total": 9501049,
    },
}


def _spec_engine_body(name: str) -> dict:
    """Build the expected engine-level QuoteResponse from the hand integers (1)."""
    ex = SPEC_EXAMPLES[name]
    lines = [
        {
            "sku": sku, "category": cat, "qty": qty, "unit_price_minor": price,
            "category_surcharge_minor": sur, "qty_discount_minor": disc,
            "line_total_minor": ltot,
        }
        for (sku, cat, qty, price, sur, disc, ltot) in ex["items"]
    ]
    return {
        "currency": "USD", "lines": lines,
        "subtotal_minor": ex["subtotal"],
        "order_discount_minor": ex["order_discount"],
        "total_minor": ex["total"],
    }


def _engine_items(name: str) -> list[dict]:
    return [
        {"sku": sku, "qty": qty, "unit_price_minor": price}
        for (sku, _cat, qty, price, *_rest) in SPEC_EXAMPLES[name]["items"]
    ]


@pytest.mark.parametrize("name", list(SPEC_EXAMPLES))
def test_engine_golden_matches_spec_and_reference(name):
    spec_body = normalize(_spec_engine_body(name))
    file_body = normalize(golden.load(f"pricing_quote/{name}_engine.json"))
    ref_body = normalize(pr.result_to_dict(pr.compute_quote("USD", _engine_items(name))))
    assert spec_body == file_body, f"{name}: golden file disagrees with spec hand integers"
    assert ref_body == file_body, f"{name}: reference recompute disagrees with golden file"


@pytest.mark.parametrize("name", list(SPEC_EXAMPLES))
def test_wire_golden_padded_to_50_and_real_lines_match(name):
    wire = golden.load(f"pricing_quote/{name}_wire.json")
    assert len(wire["lines"]) == 50, f"{name}: wire golden must have exactly 50 lines"
    # The leading real lines equal the engine golden lines (padding only appends).
    engine = golden.load(f"pricing_quote/{name}_engine.json")
    real = len(engine["lines"])
    assert wire["lines"][:real] == engine["lines"]
    # Wire totals equal engine totals (filler contributes exactly 0).
    for k in ("subtotal_minor", "order_discount_minor", "total_minor"):
        assert wire[k] == engine[k], f"{name}: filler changed {k}"
    # Every filler line is the documented zero-contribution item.
    for ln in wire["lines"][real:]:
        assert ln["sku"] == "GR-0001"
        assert ln["category"] == "GR"
        assert ln["category_surcharge_minor"] == 0
        assert ln["qty_discount_minor"] == 0
        assert ln["line_total_minor"] == 0


def test_filler_item_is_zero_contribution():
    res = pr.compute_quote("USD", [dict(pr.FILLER_ITEM)])
    assert res.total_minor == 0
    assert res.subtotal_minor == 0
    assert res.lines[0].line_total_minor == 0


def test_bps_apply_rounding_half_up_nonneg():
    # The single most divergence-prone line. 999*500 = 499500; +5000 = 504500;
    # /10000 truncating = 50 (true 49.95 rounds half-up).
    assert pr.bps_apply(999, 500) == 50
    # exact multiple: no rounding effect beyond the +5000 bias inside one unit.
    assert pr.bps_apply(100000, 150) == 1500
    assert pr.bps_apply(0, 1200) == 0


def test_qty_and_order_tier_boundaries():
    # Lower-bound-inclusive boundaries (frozen tables).
    assert pr.qty_discount_bps(9) == 0 and pr.qty_discount_bps(10) == 200
    assert pr.qty_discount_bps(49) == 200 and pr.qty_discount_bps(50) == 500
    assert pr.qty_discount_bps(99) == 500 and pr.qty_discount_bps(100) == 800
    assert pr.qty_discount_bps(199) == 800 and pr.qty_discount_bps(200) == 1200
    assert pr.order_discount_bps(9_999_999) == 0 and pr.order_discount_bps(10_000_000) == 100
    assert pr.order_discount_bps(49_999_999) == 100 and pr.order_discount_bps(50_000_000) == 250
    assert pr.order_discount_bps(199_999_999) == 250 and pr.order_discount_bps(200_000_000) == 400


def test_unknown_category_is_zero_not_error():
    assert pr.surcharge_bps("ZZ") == 0
    assert pr.surcharge_bps("XQ") == 0
    assert pr.surcharge_bps("EL") == 150


def test_error_envelopes_match_spec_strings():
    nf = golden.error_envelope("not_found")
    assert nf == {
        "type": "https://ledgerline.test/problems/not-found",
        "title": "Not found",
        "status": 404,
        "detail": "The requested resource does not exist.",
    }
    val = golden.error_envelope("validation")
    assert val["status"] == 400 and val["title"] == "Validation failed"
    internal = golden.error_envelope("internal")
    assert internal["status"] == 500 and internal["title"] == "Internal server error"


def test_validation_messages_frozen_set():
    msgs = golden.validation_messages()
    assert msgs["sku_pattern"] == "must match ^[A-Z]{2}-[0-9]{4}$"
    assert msgs["items_quote_count"] == "must contain exactly 50 items"
    assert msgs["currency_pattern"] == "must match ^[A-Z]{3}$"
    assert "_note" not in msgs
