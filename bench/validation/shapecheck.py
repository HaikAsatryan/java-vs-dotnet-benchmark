"""Shape (keys + JSON types) validation against golden/shapes.json descriptors.

Used for: (a) every equivalence-compared body must satisfy its schema shape
before the two services are deep-equal-compared; (b) RuntimeStats equivalence is
shape-ONLY per spec/runtime-stats.md (values runtime-specific, never compared).

Type tokens: int | str | array | object | number. `number` accepts int or float
(only uptime_seconds uses it). `int` rejects bool (Python bool is an int
subclass) so a JSON true/false never satisfies an integer field.
"""

from __future__ import annotations

from typing import Any

from . import golden


def _type_ok(value: Any, token: str) -> bool:
    if token == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if token == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if token == "str":
        return isinstance(value, str)
    if token == "array":
        return isinstance(value, list)
    if token == "object":
        return isinstance(value, dict)
    raise ValueError(f"unknown type token {token!r}")


def check_required(body: Any, required: dict[str, str], *, path: str) -> list[str]:
    """Return a list of shape violations (empty == OK)."""
    errs: list[str] = []
    if not isinstance(body, dict):
        return [f"{path}: expected object, got {type(body).__name__}"]
    for key, token in required.items():
        if key not in body:
            errs.append(f"{path}.{key}: missing required key")
            continue
        if not _type_ok(body[key], token):
            errs.append(
                f"{path}.{key}: expected {token}, got {type(body[key]).__name__}"
            )
    return errs


def check(body: Any, schema_name: str) -> list[str]:
    """Validate a body against a named descriptor in shapes.json (recursive).

    Returns a list of human-readable violations; empty list means the body
    satisfies the shape. Handles the nested array-element descriptors
    (Invoice.items -> InvoiceItem, InvoiceListPage.items -> InvoiceSummary) and
    the const checks (page_size==20, bytes_written==32768).
    """
    shapes = golden.shapes()
    if schema_name not in shapes:
        raise KeyError(f"no shape descriptor {schema_name!r}")
    desc = shapes[schema_name]
    errs = check_required(body, desc["required"], path=schema_name)
    if errs:
        return errs

    # Const-value checks.
    if "page_size_const" in desc and body.get("page_size") != desc["page_size_const"]:
        errs.append(
            f"{schema_name}.page_size: expected {desc['page_size_const']}, "
            f"got {body.get('page_size')}"
        )
    if "bytes_written_const" in desc and body.get("bytes_written") != desc[
        "bytes_written_const"
    ]:
        errs.append(
            f"{schema_name}.bytes_written: expected {desc['bytes_written_const']}, "
            f"got {body.get('bytes_written')}"
        )

    # Nested array elements.
    if "items_element" in desc:
        element = desc["items_element"]
        for i, item in enumerate(body.get("items", [])):
            sub = check(item, element)
            errs.extend(f"{schema_name}.items[{i}] -> {e}" for e in sub)

    # RuntimeStats extras: nested build object + endpoint_calls keys.
    if schema_name == "RuntimeStats":
        if "build_required" in desc:
            errs.extend(
                check_required(body.get("build", {}), desc["build_required"],
                               path=f"{schema_name}.build")
            )
        if "endpoint_calls_keys" in desc:
            calls = body.get("endpoint_calls", {})
            for k in desc["endpoint_calls_keys"]:
                if k not in calls:
                    errs.append(f"{schema_name}.endpoint_calls: missing key {k!r}")
    return errs
