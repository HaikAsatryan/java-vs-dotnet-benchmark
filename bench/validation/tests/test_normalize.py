"""Unit tests for the canonical JSON normalizer rules."""

from __future__ import annotations

import pytest

from validation.normalize import (
    NormalizationError,
    TS_PLACEHOLDER,
    first_difference,
    is_money_key,
    normalize,
)

VALID_TS = "2026-06-06T00:00:00.123456Z"


def test_keys_sorted_recursively():
    a = {"b": 1, "a": {"y": 2, "x": 3}}
    n = normalize(a)
    assert list(n.keys()) == ["a", "b"]
    assert list(n["a"].keys()) == ["x", "y"]


def test_request_id_excluded_at_any_depth():
    a = {"request_id": "abc", "items": [{"request_id": "d", "id": 1}]}
    n = normalize(a)
    assert "request_id" not in n
    assert "request_id" not in n["items"][0]
    assert n["items"][0] == {"id": 1}


def test_arrays_order_preserved():
    a = {"items": [3, 1, 2]}
    assert normalize(a)["items"] == [3, 1, 2]  # NOT sorted


def test_array_reorder_is_a_difference():
    x = normalize({"items": [{"id": 1}, {"id": 2}]})
    y = normalize({"items": [{"id": 2}, {"id": 1}]})
    assert first_difference(x, y) is not None


def test_created_at_compare_mode_keeps_value():
    a = {"created_at": VALID_TS}
    assert normalize(a, created_at_mode="compare")["created_at"] == VALID_TS


def test_created_at_placeholder_mode_masks_value():
    a = {"created_at": VALID_TS}
    n = normalize(a, created_at_mode="placeholder")
    assert n["created_at"] == TS_PLACEHOLDER


def test_bad_timestamp_format_raises():
    # 7 fractional digits (the .NET 'O' trap) must fail.
    with pytest.raises(NormalizationError):
        normalize({"created_at": "2026-06-06T00:00:00.1234567Z"})
    # trimmed trailing zeros (variable precision) must fail.
    with pytest.raises(NormalizationError):
        normalize({"created_at": "2026-06-06T00:00:00.1Z"})
    # missing Z must fail.
    with pytest.raises(NormalizationError):
        normalize({"created_at": "2026-06-06T00:00:00.123456"})


def test_money_key_detection():
    assert is_money_key("total_minor")
    assert is_money_key("line_total_minor")
    assert is_money_key("unit_price_minor")
    assert is_money_key("balance_minor")
    assert not is_money_key("qty")
    assert not is_money_key("status")


def test_float_on_money_path_raises():
    with pytest.raises(NormalizationError):
        normalize({"total_minor": 1.5})
    with pytest.raises(NormalizationError):
        normalize({"items": [{"line_total_minor": 2.0}]})


def test_bool_on_money_path_raises():
    # bool is an int subclass in Python; it must NOT satisfy a money field.
    with pytest.raises(NormalizationError):
        normalize({"total_minor": True})


def test_integer_money_passes():
    n = normalize({"total_minor": 99500, "items": [{"unit_price_minor": 10000}]})
    assert n["total_minor"] == 99500
    assert n["items"][0]["unit_price_minor"] == 10000


def test_first_difference_paths():
    a = normalize({"a": {"b": [1, 2, 3]}})
    b = normalize({"a": {"b": [1, 9, 3]}})
    assert first_difference(a, b) == "$.a.b[1]: 2 != 9"


def test_first_difference_key_set():
    a = normalize({"x": 1})
    b = normalize({"x": 1, "y": 2})
    assert "key set differs" in first_difference(a, b)


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        normalize({}, created_at_mode="nope")
