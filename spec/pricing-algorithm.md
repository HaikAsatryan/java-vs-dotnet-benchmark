# spec/pricing-algorithm.md — POST /pricing/quote, the complete frozen algorithm

Pure int64 integer arithmetic, branch-heavy, map lookups, per-item string parsing.
No floats, no decimal types, anywhere. Both services must produce identical
integers; the worked examples below are golden equivalence fixtures.

## Input

`QuoteRequest` (see openapi.yaml): `currency` (3 uppercase letters) + `items[]`
(exactly 50), each `{sku, qty, unit_price_minor}`. All money int64 minor units.

## SKU format and parse rule

- Pattern: `^[A-Z]{2}-[0-9]{4}$` (two uppercase letters, hyphen, four digits). Example `EL-0042`.
- Parse: `category = sku[0:2]`. The 4-digit code is validated (must be 4 digits) but not used by pricing.
- A sku failing the pattern: the whole request is a 400 validation error (RFC 7807) before any pricing runs.

## Category -> surcharge map (frozen; bps applied to the line subtotal; 1 bp = 0.01%)

Unknown category -> 0 bps (default bucket, NOT an error).

| category | meaning | surcharge_bps |
|---|---|---|
| EL | electronics | 150 |
| GR | groceries | 0 |
| HZ | hazmat | 500 |
| LX | luxury | 300 |
| DG | digital | 25 |
| FU | furniture | 120 |
| MD | medical | 80 |
| AP | apparel | 60 |
| BK | books | 10 |
| TL | tools | 90 |

(The workload sku generator draws from this set plus occasional `ZZ` to exercise the unknown->0 branch.)

## Quantity tier table (frozen; bulk DISCOUNT in bps off the line subtotal; lower bound inclusive)

| qty range | qty_discount_bps |
|---|---|
| 1 - 9 | 0 |
| 10 - 49 | 200 |
| 50 - 99 | 500 |
| 100 - 199 | 800 |
| 200+ | 1200 |

## Rounding rule (the single most divergence-prone line; frozen)

```
bps_apply(amount, bps) = (amount * bps + 5000) / 10000   -- int64 truncating division
```

The `+5000` term gives round-half-up on non-negative values. Both services
implement this EXACT integer expression. No `Math.Round`, no `math.Round`,
no decimal/float intermediate. Overflow-safe: max `amount * bps` here is
~`1e11 * 1200 = 1.2e14`, well inside int64.

## Per-item evaluation order (frozen)

For each item i, in input order:

1. `line_subtotal = qty * unit_price_minor`
2. `category = sku[0:2]`; `surcharge_bps = MAP[category]` else `0`
3. `category_surcharge_minor = bps_apply(line_subtotal, surcharge_bps)`
4. `qty_discount_bps = TIER(qty)`
5. `qty_discount_minor = bps_apply(line_subtotal, qty_discount_bps)`
6. `line_total_minor = line_subtotal + category_surcharge_minor - qty_discount_minor`
   (always >= 0: max discount 1200 bps = 12%)

## Order-level threshold discount (after all items; frozen)

1. `subtotal_minor = sum(line_total_minor)` over all lines
2. Threshold ladder on `subtotal_minor` (lower bound inclusive):

| subtotal_minor range | order_discount_bps |
|---|---|
| 0 - 9,999,999 | 0 |
| 10,000,000 - 49,999,999 | 100 |
| 50,000,000 - 199,999,999 | 250 |
| 200,000,000+ | 400 |

3. `order_discount_minor = bps_apply(subtotal_minor, order_discount_bps)`
4. `total_minor = subtotal_minor - order_discount_minor`

## Output

`QuoteResponse` (see openapi.yaml): `currency`, `lines[]` in input order (each
`{sku, category, qty, unit_price_minor, category_surcharge_minor,
qty_discount_minor, line_total_minor}`), `subtotal_minor`,
`order_discount_minor`, `total_minor`.

## Worked examples (golden fixtures; both services must emit these exact integers)

### Example A — electronics, mid qty tier

Item: `sku="EL-0042", qty=10, unit_price_minor=10000`
- line_subtotal = 10 * 10000 = 100000
- surcharge (EL, 150 bps) = (100000*150 + 5000) / 10000 = 15005000 / 10000 = **1500**
- qty tier (10 -> 200 bps) = (100000*200 + 5000) / 10000 = 20005000 / 10000 = **2000**
- line_total = 100000 + 1500 - 2000 = **99500**
- subtotal = 99500 -> order tier 0 -> order_discount = **0** -> total = **99500**

### Example B — unknown category, top qty tier

Item: `sku="ZZ-1234", qty=200, unit_price_minor=5000`
- line_subtotal = 200 * 5000 = 1000000
- surcharge (ZZ unknown -> 0 bps) = **0**
- qty tier (200 -> 1200 bps) = (1000000*1200 + 5000) / 10000 = 1200005000 / 10000 = **120000**
- line_total = 1000000 + 0 - 120000 = **880000**
- subtotal = 880000 -> order tier 0 -> total = **880000**

### Example C — two items, rounding + near order threshold

Item 1: `sku="HZ-0007", qty=3, unit_price_minor=333`
- line_subtotal = 999
- surcharge (HZ, 500 bps) = (999*500 + 5000) / 10000 = 504500 / 10000 = **50**  (true 49.95, half-up)
- qty tier (3 -> 0 bps) = **0**
- line_total = 999 + 50 - 0 = **1049**

Item 2: `sku="LX-9999", qty=100, unit_price_minor=100000`
- line_subtotal = 10000000
- surcharge (LX, 300 bps) = (10000000*300 + 5000) / 10000 = 3000005000 / 10000 = **300000**
- qty tier (100 -> 800 bps) = (10000000*800 + 5000) / 10000 = 8000005000 / 10000 = **800000**
- line_total = 10000000 + 300000 - 800000 = **9500000**

Order:
- subtotal = 1049 + 9500000 = **9501049** (< 10,000,000 -> tier 0)
- order_discount = **0**, total = **9501049**

Note: golden fixtures relax the minItems=50 wire rule for unit-level engine
tests; the wire-level golden tests pad requests to 50 items with the documented
filler item `{"sku":"GR-0001","qty":1,"unit_price_minor":0}` (GR = 0 bps, qty
tier 0, zero price -> contributes exactly 0 to every output amount).
