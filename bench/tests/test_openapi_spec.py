"""spec/openapi.yaml is a FROZEN contract, and its prose is part of the freeze.

`config_hash` covers every byte of `spec/`, so a sentence in this file is a
declaration with the same standing as a numeric. Two of them were round-1
sentences carried into round 2: the API description named the wrong pair of
languages, and the errors-map KEY FORMAT said the second stack "mirrors it
exactly" when the JVM does not emit the frozen keys natively at all.
"""

from __future__ import annotations

import json

import pytest
import yaml

from ledgerbench.util import paths
from validation import golden


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load((paths.spec_dir() / "openapi.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw() -> str:
    return (paths.spec_dir() / "openapi.yaml").read_text(encoding="utf-8")


def test_the_contract_names_the_stacks_this_round_compares(spec, raw):
    assert spec["info"]["description"] == (
        "Small invoicing/billing API used as a Java vs .NET benchmark workload."
    )
    # and nowhere else in the frozen prose either: a timestamp rule addressed to
    # a runtime that does not exist is a rule nobody is held to.
    assert "Go " not in raw and "Go'" not in raw


def test_the_timestamp_rule_addresses_the_trap_each_runtime_actually_has(raw):
    """Both sides pad explicitly, and the JVM's trap is not .NET's.

    `Instant.toString()` emits VARIABLE precision and drops an all-zero
    fraction; `'O'` emits seven digits. One sentence covering both would be
    wrong for one of them.
    """
    assert "Instant.toString()" in raw
    assert "'SSSSSS'" in raw and "'ffffff'" in raw
    assert "7 digits ('O' format)" in raw


def test_the_errors_key_format_says_which_side_derives_the_keys(spec):
    """The frozen keys are .NET-native and Java-derived, and the file must say so.

    Jakarta Bean Validation builds its path from the Java component names, so
    the JVM sees `items[0].sku` and must transform it. Claiming both sides emit
    the frozen format natively hides a live translation step behind a wire that
    happens to match.
    """
    text = spec["components"]["schemas"]["Problem"]["properties"]["errors"]["description"]
    assert "items[0].sku" in text            # what Jakarta actually produces
    assert "Items[0].Sku" in text            # what the wire must carry
    assert "FieldPath" in text               # where the transform lives
    assert "FieldPathTest" in text and "ErrorContractTest" in text
    assert "mirrors it exactly" not in text


def test_the_frozen_error_envelopes_match_the_goldens_the_harness_compares_against(spec):
    """x-error-strings and `validation/golden/errors/*` are two copies of one fact.

    The equivalence suite compares live responses against the GOLDEN files, and
    this test compares those files against the spec block they were transcribed
    from, so a spec edit cannot move the frozen envelope while every other test
    keeps passing against the old copy.
    """
    for name, path in (("validation", "errors/validation_envelope.json"),
                       ("not_found", "errors/not_found.json"),
                       ("internal", "errors/internal.json")):
        frozen = spec["x-error-strings"][name]
        on_disk = json.loads(json.dumps(golden.load(path)))
        assert on_disk == {k: frozen[k] for k in ("type", "title", "status", "detail")}, name
