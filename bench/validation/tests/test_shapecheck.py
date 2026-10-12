"""Unit tests for shape (keys + JSON types) validation against shapes.json."""

from __future__ import annotations

from validation import shapecheck

GOOD_INVOICE = {
    "id": 1, "customer_id": 7, "status": "draft", "currency": "USD",
    "total_minor": 99500, "created_at": "2026-06-06T00:00:00.000000Z",
    "items": [
        {"id": 10, "sku": "EL-0042", "description": "x", "qty": 10,
         "unit_price_minor": 10000, "line_total_minor": 100000},
    ],
}

GOOD_RUNTIME_STATS = {
    "ts": "2026-06-06T00:00:00.000000Z", "uptime_seconds": 12.3,
    "build": {"language": "go", "runtime_version": "go1.26.4",
              "service_version": "dev", "data_layer": "sqlc", "processor_count": 6},
    "config": {}, "gc": {}, "db_pool": {}, "redis_pool": {},
    "goroutines_or_threadpool": 4,
    "endpoint_calls": {
        "GET /invoices/{id}": 0, "GET /customers/{id}/invoices": 0,
        "POST /pricing/quote": 0, "POST /invoices": 0,
        "POST /invoices/{id}/pdf-stub": 0, "GET /healthz": 0,
    },
}


def test_invoice_shape_ok():
    assert shapecheck.check(GOOD_INVOICE, "Invoice") == []


def test_invoice_missing_key():
    bad = dict(GOOD_INVOICE)
    del bad["currency"]
    errs = shapecheck.check(bad, "Invoice")
    assert any("currency" in e and "missing" in e for e in errs)


def test_invoice_wrong_type():
    bad = dict(GOOD_INVOICE, total_minor="100")  # string, not int
    errs = shapecheck.check(bad, "Invoice")
    assert any("total_minor" in e for e in errs)


def test_invoice_item_element_validated():
    bad = dict(GOOD_INVOICE, items=[{"id": 1}])  # missing item fields
    errs = shapecheck.check(bad, "Invoice")
    assert any("items[0]" in e for e in errs)


def test_int_token_rejects_bool():
    bad = dict(GOOD_INVOICE, id=True)
    errs = shapecheck.check(bad, "Invoice")
    assert any("id" in e for e in errs)


def test_list_page_const_page_size():
    page = {"customer_id": 1, "page": 1, "page_size": 19, "items": []}
    errs = shapecheck.check(page, "InvoiceListPage")
    assert any("page_size" in e for e in errs)


def test_pdf_stub_const_bytes_written():
    ok = {"invoice_id": 1, "bytes_written": 32768, "path": "/data/pdf/1.pdfstub"}
    assert shapecheck.check(ok, "PdfStub") == []
    bad = dict(ok, bytes_written=1)
    assert any("bytes_written" in e for e in shapecheck.check(bad, "PdfStub"))


def test_runtime_stats_shape_ok():
    assert shapecheck.check(GOOD_RUNTIME_STATS, "RuntimeStats") == []


def test_runtime_stats_missing_endpoint_key():
    bad = {**GOOD_RUNTIME_STATS, "endpoint_calls": {"GET /healthz": 0}}
    errs = shapecheck.check(bad, "RuntimeStats")
    assert any("endpoint_calls" in e for e in errs)


def test_runtime_stats_missing_build_field():
    bad = {**GOOD_RUNTIME_STATS, "build": {"language": "go"}}
    errs = shapecheck.check(bad, "RuntimeStats")
    assert any("build" in e for e in errs)
