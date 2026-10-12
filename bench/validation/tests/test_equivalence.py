"""Cross-service equivalence suite.

Parametrized over (endpoint x case x slot). Driven by each slot's own
base-URL env var (`registry.Slot.url_env`); SKIPS CLEANLY when any is unset.
Each case:
  1. calls the endpoint on EACH live service,
  2. asserts the status + content-type,
  3. normalizes both bodies (validation.normalize) and asserts they are
     deep-equal to each other (the core identical-behavior gate) and, where a
     golden exists, equal to the committed golden,
  4. RuntimeStats is shape-ONLY (spec/runtime-stats.md): keys/types, not values.

Assumes a seed-42 DB at either scale (smoke: customers 1..1000 / invoices
1..5000; full: 1..1M / 1..5M per spec/workload.yaml); the seeded/absent ids
below are valid for both. Seeded reads compare created_at
structurally (deterministic across services on the same load); the freshly
created invoice from POST /invoices normalizes created_at to a placeholder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from validation import clients, golden, shapecheck
from validation.normalize import first_difference, normalize

PROBLEM_CT = "application/problem+json"
JSON_CT = "application/json"

# Seeded ids known to exist at smoke scale (low/hot ids; rank 1 == id 1).
SEEDED_INVOICE_ID = 1
SEEDED_CUSTOMER_ID = 1
# Well-formed but absent at EVERY scale: full seeds 1..5_000_000 invoices /
# 1..1_000_000 customers, and measured-run creates append from there, so the
# absent ids sit far beyond any reachable sequence value.
ABSENT_INVOICE_ID = 1_999_999_999
ABSENT_CUSTOMER_ID = 1_999_999_999
# Syntactically invalid id -> 400 before 404.
BAD_INVOICE_ID = 0

_REQ_DIR = Path(__file__).resolve().parent.parent / "golden" / "pricing_quote" / "requests"


def _quote_request(name: str) -> dict:
    with (_REQ_DIR / f"{name}_wire_request.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def _ct_startswith(resp: clients.Response, prefix: str) -> bool:
    return resp.content_type.split(";")[0].strip() == prefix


def _services_or_skip() -> list[clients.Service]:
    reason = clients.skip_reason()
    if reason:
        pytest.skip(reason)
    return clients.configured_services()


def _call_both(method: str, path: str, **kw) -> dict[str, clients.Response]:
    return {s.name: clients.request(s, method, path, **kw) for s in _services_or_skip()}


def _assert_cross_equal(bodies: dict[str, object], *, mode: str = "compare") -> dict:
    """Normalize each service body and assert all are deep-equal; return the first."""
    norm = {name: normalize(b, created_at_mode=mode) for name, b in bodies.items()}
    names = list(norm)
    base = norm[names[0]]
    for other in names[1:]:
        diff = first_difference(base, norm[other])
        assert diff is None, f"{names[0]} vs {other} differ at {diff}"
    return base


# --------------------------------------------------------------------------- #
# GET /invoices/{id} : hit, miss, 404
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("warm", [False, True], ids=["miss", "hit"])
def test_get_invoice_hit_and_miss(warm):
    path = f"/invoices/{SEEDED_INVOICE_ID}"
    if warm:
        # Prime the cache on each service so the second call is a hit; the
        # cache pin (serialize-then-cache) makes hit byte-equal to miss.
        _call_both("GET", path)
    resps = _call_both("GET", path)
    bodies = {}
    for name, r in resps.items():
        assert r.status == 200, f"{name}: {r.status} {r.text[:200]}"
        assert _ct_startswith(r, JSON_CT)
        assert shapecheck.check(r.json_body, "Invoice") == []
        bodies[name] = r.json_body
    _assert_cross_equal(bodies, mode="compare")


def test_get_invoice_404():
    resps = _call_both("GET", f"/invoices/{ABSENT_INVOICE_ID}")
    bodies = {}
    for name, r in resps.items():
        assert r.status == 404, f"{name}: {r.status}"
        assert _ct_startswith(r, PROBLEM_CT)
        bodies[name] = r.json_body
    body = _assert_cross_equal(bodies)
    assert normalize(body) == normalize(golden.error_envelope("not_found"))


def test_get_invoice_400_bad_id():
    resps = _call_both("GET", f"/invoices/{BAD_INVOICE_ID}")
    bodies = {}
    for name, r in resps.items():
        assert r.status == 400, f"{name}: {r.status}"
        assert _ct_startswith(r, PROBLEM_CT)
        bodies[name] = r.json_body
    body = _assert_cross_equal(bodies)
    # Envelope (type/title/status/detail) is frozen; compare only the frozen
    # envelope fields here. The errors-map keys are .NET source-gen PascalCase
    # (canonical), and Java derives them; the cross-equal check above covers them.
    for k, v in golden.error_envelope("validation").items():
        assert body[k] == v


# --------------------------------------------------------------------------- #
# GET /customers/{id}/invoices : page1, page3, empty, order
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("page", [1, 3], ids=["page1", "page3"])
def test_list_invoices_pages(page):
    resps = _call_both("GET", f"/customers/{SEEDED_CUSTOMER_ID}/invoices?page={page}")
    bodies = {}
    for name, r in resps.items():
        assert r.status == 200, f"{name}: {r.status}"
        assert shapecheck.check(r.json_body, "InvoiceListPage") == []
        assert r.json_body["page"] == page
        assert r.json_body["page_size"] == 20
        bodies[name] = r.json_body
    _assert_cross_equal(bodies)


def test_list_invoices_empty_page_beyond_data():
    # A high page beyond any customer's data -> 200 with empty items.
    resps = _call_both("GET", f"/customers/{SEEDED_CUSTOMER_ID}/invoices?page=100000")
    bodies = {}
    for name, r in resps.items():
        assert r.status == 200, f"{name}: {r.status}"
        assert r.json_body["items"] == []
        bodies[name] = r.json_body
    _assert_cross_equal(bodies)


def test_list_invoices_order_desc():
    # Order-significant: items MUST be ORDER BY id DESC, identical on both sides.
    resps = _call_both("GET", f"/customers/{SEEDED_CUSTOMER_ID}/invoices?page=1")
    per_service_ids = {}
    bodies = {}
    for name, r in resps.items():
        assert r.status == 200
        ids = [it["id"] for it in r.json_body["items"]]
        assert ids == sorted(ids, reverse=True), f"{name}: not id DESC: {ids}"
        per_service_ids[name] = ids
        bodies[name] = r.json_body
    _assert_cross_equal(bodies)


# --------------------------------------------------------------------------- #
# POST /invoices : valid create, 400
# --------------------------------------------------------------------------- #
def test_create_invoice_valid():
    req = {
        "customer_id": SEEDED_CUSTOMER_ID,
        "currency": "USD",
        "status": "draft",
        "items": [
            {"sku": "EL-0042", "description": "widget", "qty": 10, "unit_price_minor": 10000},
            {"sku": "GR-0001", "description": "grocery", "qty": 1, "unit_price_minor": 500},
            {"sku": "HZ-0007", "description": "hazmat", "qty": 3, "unit_price_minor": 333},
        ],
    }
    resps = _call_both("POST", "/invoices", json_body=req)
    bodies = {}
    for name, r in resps.items():
        assert r.status == 201, f"{name}: {r.status} {r.text[:200]}"
        assert r.headers.get("location", "").endswith(f"/invoices/{r.json_body['id']}")
        assert shapecheck.check(r.json_body, "Invoice") == []
        # Server computes line_total_minor = qty*unit_price_minor and total = sum.
        assert r.json_body["total_minor"] == sum(
            it["qty"] * it["unit_price_minor"] for it in req["items"]
        )
        bodies[name] = r.json_body
    # Fresh create: id and created_at differ across two independent processes;
    # normalize created_at to a placeholder and drop the server-assigned ids
    # before cross-comparison (the structural body must otherwise match).
    norm = {}
    for name, b in bodies.items():
        n = normalize(b, created_at_mode="placeholder")
        n.pop("id", None)
        for it in n.get("items", []):
            it.pop("id", None)
        norm[name] = n
    names = list(norm)
    for other in names[1:]:
        diff = first_difference(norm[names[0]], norm[other])
        assert diff is None, f"create body {names[0]} vs {other}: {diff}"


def test_create_invoice_400():
    bad = {"customer_id": 1, "currency": "usd", "status": "nope", "items": []}
    resps = _call_both("POST", "/invoices", json_body=bad)
    bodies = {}
    for name, r in resps.items():
        assert r.status == 400, f"{name}: {r.status}"
        assert _ct_startswith(r, PROBLEM_CT)
        bodies[name] = r.json_body
    body = _assert_cross_equal(bodies)
    for k, v in golden.error_envelope("validation").items():
        assert body[k] == v
    # The errors map (if both emit one) must be identical between services and
    # its values must all be drawn from the frozen message set.
    if all("errors" in b for b in bodies.values()):
        frozen_msgs = set(golden.validation_messages().values())
        for name, b in bodies.items():
            for field, msgs in b["errors"].items():
                for m in msgs:
                    assert m in frozen_msgs, f"{name}: unfrozen message {m!r} at {field}"


def test_create_invoice_404_absent_customer():
    req = {
        "customer_id": ABSENT_CUSTOMER_ID,
        "currency": "USD",
        "status": "open",
        "items": [
            {"sku": "EL-0042", "description": "a", "qty": 1, "unit_price_minor": 100},
            {"sku": "GR-0001", "description": "b", "qty": 1, "unit_price_minor": 100},
            {"sku": "HZ-0007", "description": "c", "qty": 1, "unit_price_minor": 100},
        ],
    }
    resps = _call_both("POST", "/invoices", json_body=req)
    bodies = {}
    for name, r in resps.items():
        assert r.status == 404, f"{name}: {r.status}"
        bodies[name] = r.json_body
    body = _assert_cross_equal(bodies)
    assert normalize(body) == normalize(golden.error_envelope("not_found"))


# --------------------------------------------------------------------------- #
# POST /invoices/{id}/pdf-stub
# --------------------------------------------------------------------------- #
def test_pdf_stub():
    resps = _call_both("POST", f"/invoices/{SEEDED_INVOICE_ID}/pdf-stub")
    bodies = {}
    for name, r in resps.items():
        assert r.status == 200, f"{name}: {r.status}"
        assert shapecheck.check(r.json_body, "PdfStub") == []
        assert r.json_body["bytes_written"] == 32768
        assert r.json_body["path"] == f"/data/pdf/{SEEDED_INVOICE_ID}.pdfstub"
        bodies[name] = r.json_body
    _assert_cross_equal(bodies)


def test_pdf_stub_404():
    resps = _call_both("POST", f"/invoices/{ABSENT_INVOICE_ID}/pdf-stub")
    for name, r in resps.items():
        assert r.status == 404, f"{name}: {r.status}"


# --------------------------------------------------------------------------- #
# POST /pricing/quote : golden numeric result, 400
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", ["quote_a", "quote_b", "quote_c"])
def test_quote_golden(case):
    req = _quote_request(case)
    expected = golden.load(f"pricing_quote/{case}_wire.json")
    resps = _call_both("POST", "/pricing/quote", json_body=req)
    bodies = {}
    for name, r in resps.items():
        assert r.status == 200, f"{name}: {r.status} {r.text[:200]}"
        assert normalize(r.json_body) == normalize(expected), f"{name} != golden {case}"
        bodies[name] = r.json_body
    _assert_cross_equal(bodies)


def test_quote_400_wrong_item_count():
    # minItems/maxItems = 50; send 1 item -> 400 items_quote_count.
    req = {"currency": "USD", "items": [{"sku": "EL-0042", "qty": 1, "unit_price_minor": 100}]}
    resps = _call_both("POST", "/pricing/quote", json_body=req)
    bodies = {}
    for name, r in resps.items():
        assert r.status == 400, f"{name}: {r.status}"
        bodies[name] = r.json_body
    body = _assert_cross_equal(bodies)
    for k, v in golden.error_envelope("validation").items():
        assert body[k] == v


# --------------------------------------------------------------------------- #
# GET /healthz : static body
# --------------------------------------------------------------------------- #
def test_healthz():
    resps = _call_both("GET", "/healthz")
    for name, r in resps.items():
        assert r.status == 200, f"{name}: {r.status}"
        assert normalize(r.json_body) == normalize(golden.load("healthz/ok.json"))


# --------------------------------------------------------------------------- #
# GET /runtime-stats : shape-only equivalence (values runtime-specific)
# --------------------------------------------------------------------------- #
def test_runtime_stats_shape_only():
    resps = _call_both("GET", "/runtime-stats")
    for name, r in resps.items():
        assert r.status == 200, f"{name}: {r.status}"
        errs = shapecheck.check(r.json_body, "RuntimeStats")
        assert errs == [], f"{name} runtime-stats shape: {errs}"
        # Shape only was NOT the whole contract: `shapes.json` types `ts` as a
        # bare string, so a service emitting variable-precision timestamps --
        # which is what `Instant.toString()` does -- passed here while failing
        # the same frozen format everywhere else. `normalize` is the one place
        # that format lives; what it enforces on this body is pinned offline in
        # test_runtime_stats_contract.py, since this test skips without both
        # SUTs running.
        normalize(r.json_body)
