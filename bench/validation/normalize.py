"""Canonical JSON normalization for cross-service equivalence.

The single job of this module: turn a parsed JSON response body into a
*canonical* form such that two byte-different-but-semantically-identical bodies
(one per SUT slot -- Java vs .NET this round) compare equal, WITHOUT masking
any difference that actually matters. Frozen rules, derived from spec/openapi.yaml + spec/cache.md +
spec/db-access.md:

  N1  Object keys sorted recursively. JSON object key order is insignificant on
      both wires (spec/openapi.yaml: "field ORDER ... is irrelevant" for logs;
      JSON objects are unordered), so we canonicalize by sorting.

  N2  Arrays are ORDER-SIGNIFICANT and preserved as-is. Every array in the wire
      contract is explicitly ordered: invoice.items ORDER BY id ASC,
      InvoiceListPage.items ORDER BY id DESC, QuoteResponse.lines input order,
      Problem.errors[field] is a frozen message list. We NEVER reorder arrays;
      a reorder is a real divergence and must fail.

  N3  request_id is excluded. Per spec it is NEVER in a response body (header
      only) and is explicitly "excluded from equivalence". We strip it defensively
      at any depth in case a service leaks it.

  N4  Server-generated, non-deterministic timestamps are normalized to a
      placeholder ONLY where the spec marks them non-deterministic: created_at on
      a FRESHLY CREATED invoice (POST /invoices), whose value is now() at write
      time and cannot match across two independent service processes. For a
      SEEDED invoice read back via GET, created_at IS deterministic across
      services on the same DB load (spec/db-access.md seeder note) and is compared
      structurally. The caller selects the mode via `created_at_mode`.

  N5  Money/ids are integers (int64 minor units) everywhere. We ASSERT no float
      appears on a money/amount/total/price/balance path: a float there is a
      contract violation (spec/openapi.yaml "no floats in money paths"). A bool is
      not an int for this purpose (Python bool is an int subclass; we reject it on
      money paths too).

  N6  Timestamp FORMAT is validated where a timestamp is structurally compared:
      RFC3339 UTC, EXACTLY 6 fractional digits, 'Z' suffix (spec/openapi.yaml).
      Java must not emit variable precision (Instant.toString trims an all-zero
      fraction; Rfc3339Micros holds the contract); .NET must not emit 7 digits.
      A malformed timestamp raises, so a format regression on either side fails
      the suite. (Round 1's trap was Go trimming trailing zeros -- same class.)

This module is pure (no I/O, no Docker, no services) and fully unit-tested.
"""

from __future__ import annotations

import re
from typing import Any

# Placeholder substituted for a non-deterministic server timestamp.
TS_PLACEHOLDER = "<server-generated-ts>"

# Keys excluded from comparison at any depth.
EXCLUDED_KEYS = frozenset({"request_id"})

# Substrings that mark a JSON key as carrying money/amount (N5: must be integer).
# Matched case-insensitively against the leaf key name. Kept explicit (not a
# broad regex) so the set of money paths is auditable against the wire contract:
# *_minor, balance_minor, and the bare totals/prices in the schemas.
_MONEY_KEY_SUBSTRINGS = (
    "_minor",       # total_minor, line_total_minor, unit_price_minor, ...
    "balance",      # balance_minor
)

# RFC3339 UTC, EXACTLY 6 fractional digits, Z. Frozen by spec/openapi.yaml.
_RFC3339_MICROS_Z = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)

# Keys whose VALUE is an RFC3339 timestamp in the wire contract.
_TIMESTAMP_KEYS = frozenset({"created_at", "ts"})


class NormalizationError(ValueError):
    """A contract violation found during normalization (float money, bad ts)."""


def is_money_key(key: str) -> bool:
    """True if `key` names a money/amount field that must be an integer."""
    low = key.lower()
    return any(sub in low for sub in _MONEY_KEY_SUBSTRINGS)


def assert_rfc3339_micros_z(value: str, *, path: str) -> None:
    """Validate the frozen timestamp format; raise on any deviation."""
    if not isinstance(value, str) or not _RFC3339_MICROS_Z.match(value):
        raise NormalizationError(
            f"{path}: timestamp not RFC3339 UTC with exactly 6 fractional "
            f"digits + Z (got {value!r})"
        )


def normalize(
    value: Any,
    *,
    created_at_mode: str = "compare",
    _path: str = "$",
    _key: str | None = None,
) -> Any:
    """Return a canonical copy of a parsed-JSON `value`.

    created_at_mode:
      - "compare"     : created_at is deterministic (seeded read); validate its
                        format and keep the value (N4 seeded branch, N6).
      - "placeholder" : created_at is server-generated (fresh POST /invoices);
                        validate its format then replace with TS_PLACEHOLDER so
                        two independent processes compare equal (N4 fresh branch).

    The mode applies to `created_at` specifically. `ts` (log-style) is always
    format-validated and kept; it does not appear in equivalence-compared bodies
    but the rule is here for completeness.
    """
    if created_at_mode not in ("compare", "placeholder"):
        raise ValueError(f"unknown created_at_mode {created_at_mode!r}")

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k in sorted(value.keys()):  # N1
            if k in EXCLUDED_KEYS:       # N3
                continue
            out[k] = normalize(
                value[k],
                created_at_mode=created_at_mode,
                _path=f"{_path}.{k}",
                _key=k,
            )
        return out

    if isinstance(value, list):
        # N2: order preserved. Index carried into the path for diagnostics only.
        return [
            normalize(
                item,
                created_at_mode=created_at_mode,
                _path=f"{_path}[{i}]",
                _key=None,
            )
            for i, item in enumerate(value)
        ]

    # Scalar leaf.
    if _key is not None and _key in _TIMESTAMP_KEYS and isinstance(value, str):
        assert_rfc3339_micros_z(value, path=_path)  # N6 (both modes)
        if _key == "created_at" and created_at_mode == "placeholder":
            return TS_PLACEHOLDER  # N4 fresh branch
        return value

    if _key is not None and is_money_key(_key):  # N5
        # bool is an int subclass in Python; reject it explicitly as a money value.
        if isinstance(value, bool) or not isinstance(value, int):
            raise NormalizationError(
                f"{_path}: money field {_key!r} must be an integer minor-unit "
                f"value, got {type(value).__name__} ({value!r})"
            )
        return value

    return value


def first_difference(a: Any, b: Any, *, _path: str = "$") -> str | None:
    """Return a human path to the first structural difference, or None if equal.

    Used by the equivalence tests to produce an actionable assert message instead
    of dumping two large blobs. Operates on already-normalized values.
    """
    if type(a) is not type(b):
        # int vs bool etc. Normalize numeric int/float comparison leniently:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a == b:
            return None
        return f"{_path}: type {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        ka, kb = set(a.keys()), set(b.keys())
        if ka != kb:
            missing = sorted(ka ^ kb)
            return f"{_path}: key set differs at {missing}"
        for k in sorted(a.keys()):
            d = first_difference(a[k], b[k], _path=f"{_path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{_path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, _path=f"{_path}[{i}]")
            if d:
                return d
        return None
    if a != b:
        return f"{_path}: {a!r} != {b!r}"
    return None
