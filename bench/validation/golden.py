"""Golden-fixture loader. Resolves the committed golden/ tree under this package.

Golden files are computed BY HAND from the spec worked examples (pricing
examples A/B/C, padded to the wire minItems=50 with the documented
zero-contribution filler) and the frozen error strings from
spec/openapi.yaml x-error-strings / x-validation-messages. They are the
authority; tests recompute the pricing examples independently
(validation.pricing_reference) and assert agreement.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


def golden_dir() -> Path:
    return Path(__file__).resolve().parent / "golden"


@lru_cache(maxsize=None)
def load(rel_path: str) -> dict | list:
    """Load and parse a golden JSON file by path relative to golden/."""
    p = golden_dir() / rel_path
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def error_envelope(kind: str) -> dict:
    """Frozen RFC 7807 envelope for 'validation' | 'not_found' | 'internal'."""
    name = {
        "validation": "errors/validation_envelope.json",
        "not_found": "errors/not_found.json",
        "internal": "errors/internal.json",
    }[kind]
    env = load(name)
    assert isinstance(env, dict)
    return env


def validation_messages() -> dict[str, str]:
    """Frozen per-rule validation message values (x-validation-messages)."""
    d = load("errors/validation_messages.json")
    assert isinstance(d, dict)
    return {k: v for k, v in d.items() if not k.startswith("_")}


def shapes() -> dict:
    d = load("shapes.json")
    assert isinstance(d, dict)
    return d
