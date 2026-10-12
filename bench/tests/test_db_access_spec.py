"""spec/db-access.md quotes arithmetic done over spec/workload.yaml.

Two frozen files, one fact each time. The round-trip ledger's "mean 10.5 round
trips" is `5 + mean(item_count)`, and the Q3 statement-text shares are
`page_distribution` times the `list_invoices` mix share -- so a workload edit
that never touches db-access.md can silently turn a disclosure into a false one.
Round 1's overclaims (R1-B6) had this shape: a sentence true when it was
written and not re-checked afterwards.
"""

from __future__ import annotations

import pytest

from ledgerbench.config import load_workload
from ledgerbench.util import paths


@pytest.fixture(scope="module")
def text() -> str:
    return (paths.spec_dir() / "db-access.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def flat() -> str:
    """The same file with runs of whitespace collapsed.

    The prose is hard-wrapped, so a sentence that must match a computed number
    is split across lines at an arbitrary column. Assert on the sentence, not on
    the wrap.
    """
    return " ".join((paths.spec_dir() / "db-access.md")
                    .read_text(encoding="utf-8").split())


@pytest.fixture(scope="module")
def workload():
    return load_workload()


def _pct(x: float) -> str:
    """`17.5%` rather than `17.5%` from a float repr with a trailing zero."""
    return f"{x:.10g}%"


def test_the_q3_text_shares_are_the_workload_arithmetic(text, workload):
    """Java splits at page 1, EF at page 2, and the shares follow from the spec."""
    weights = {int(k): float(v) for k, v in workload.page_distribution.weights.items()}
    share = dict(workload.mix.as_pairs())["list_invoices"] / 100.0
    assert sum(weights.values()) == pytest.approx(1.0)

    java_majority = weights[1] * share * 100
    java_minority = (1 - weights[1]) * share * 100
    ef_minority = weights[2] * share * 100
    ef_majority = (1 - weights[2]) * share * 100

    for value in (java_majority, java_minority, ef_minority, ef_majority):
        assert _pct(value) in text, f"{_pct(value)} is no longer in spec/db-access.md"
    # the page weights themselves are quoted too, so the derivation is auditable
    assert "0.70 / 0.15 / 0.08 /" in text and "0.04 / 0.03" in text


def test_the_mean_java_create_round_trips_follow_from_the_realized_item_count(
    text, flat, workload,
):
    """Java pays 5 + N, .NET pays 6 -- and N is the distribution the GENERATOR
    actually produces, not the uniform draw the spec once claimed.

    P7 review finding C1: `gen_create_body` grows the drawn count toward
    maxItems while a fully-padded body cannot reach target-512, so at the
    frozen dials no create with fewer than 6 items is ever sent and the mean
    is ~6.50, not 5.5. The spec's arithmetic (11.5 round trips, 0.825/req) was
    recomputed from the realized distribution; this test derives that
    distribution from the real generator so the doc can never drift from the
    code again -- the previous version of this test asserted the uniform-draw
    figure and was green while the registered workload was not the one that
    runs."""
    import json as _json

    from ledgerbench.util.pcg64 import PCG64
    from ledgerbench import vegeta as _vegeta

    counts = [
        len(_json.loads(_vegeta.gen_create_body(PCG64(1234 + i), workload, 1))["items"])
        for i in range(400)
    ]
    realized_mean = sum(counts) / len(counts)
    assert min(counts) >= 6, "the growth floor moved; re-register spec/db-access.md"
    assert abs(realized_mean - 6.50) < 0.15, realized_mean

    assert "| **5 + N** |" in text
    assert "| **6** |" in text
    # The REGISTERED figures (from the mean over a large sample, 6.50) must
    # appear in the spec, and the sample-derived values must agree with them
    # within sampling tolerance -- the coupling this test exists for.
    assert "**11.5** round trips against EF Core's 6" in flat
    assert "{6: ~67%, 7: ~16%, 8: ~17%}" in flat
    assert abs((5 + realized_mean) - 11.5) < 0.15
    create_share = dict(workload.mix.as_pairs())["create_invoice"]
    assert f"`create_invoice` is {create_share:g}%" in flat
    # and the per-request figure follows: (N - 1) extra per create x the share
    assert "**0.825**" in flat
    assert abs((realized_mean - 1) * create_share / 100 - 0.825) < 0.02


def test_the_measured_round_trip_evidence_is_in_the_file_not_only_its_conclusion(text):
    """A ledger asserting counts nobody measured is exactly what this replaced.

    The previous text pinned the create path as "ASSERTED EQUAL at integration"
    across stacks that are not equal. The replacement must carry the measurement
    that decided it, not just the number it produced.
    """
    assert "ASSERTED EQUAL" not in text
    assert "25 ms in each direction" in text
    assert "Java   N=3" in text and "Java   N=8" in text
    assert ".NET   N=3" in text and ".NET   N=8" in text


def test_the_driver_pooling_sentence_has_one_definition(text):
    """`registry` and this spec file cannot say different things about a checkout.

    `analyze._driver_sensitivity_clause` renders
    `f"{lang.db_driver} {lang.db_driver_pooling_note}"` into the HEADLINE
    report's DB-CPU caveats. Until 2026-08-24 the Java string asserted PgJDBC +
    HikariCP "issues no reset on a pooled checkout -- an observed ABSENCE", a
    claim spec/db-access.md had already retracted in the same phase, and no test
    read either string: replacing it with a flat falsehood left the whole suite
    green. The spec table is the single definition and this is what reads it.
    """
    from ledgerbench import registry

    drivers = {spec.db_driver for spec in registry.LANGUAGES.values()}
    rows: dict[str, str] = {}
    for line in text.splitlines():
        cells = line.split("|")
        if len(cells) == 4 and cells[1].strip() in drivers:
            rows[cells[1].strip()] = cells[2].strip()
    assert len(rows) == len(registry.LANGUAGES), (
        "spec/db-access.md's per-checkout table must carry one row per registered "
        f"driver; found {sorted(rows)}"
    )
    for spec in registry.LANGUAGES.values():
        assert rows[spec.db_driver] == " ".join(spec.db_driver_pooling_note.split()), (
            f"{spec.lang}: registry.db_driver_pooling_note and spec/db-access.md's "
            "per-checkout table have drifted"
        )


def test_every_env_dial_the_pools_section_declares_is_a_pinned_knob(text):
    """A dial this file names must be scrubbed and recordable, not just described.

    `DB_POOL_WARM_ON_START` was declared here -- the `pools_warm` gate's meaning
    rests on it -- while being absent from the registry, from the compose scrub
    set and from `manifest.Knobs`. An ambient `export DB_POOL_WARM_ON_START=false`
    would then have reconfigured BOTH SUTs, made this section's sentence false
    for that run, and left no artifact able to say so. That is exactly the
    `db_conn_lifetime_seconds` defect, and it is not caught by the compose
    cross-check because compose does not interpolate this one.
    """
    import re

    from ledgerbench import registry

    section = text.split("## Pools (single env surface, both services)", 1)[1]
    section = section.split("\n## ", 1)[0]
    named = {
        cells[1].strip()
        for line in section.splitlines()
        if len(cells := line.split("|")) > 2 and re.fullmatch(r"[A-Z][A-Z0-9_]+", cells[1].strip())
    }
    named |= set(re.findall(r"`([A-Z][A-Z0-9_]+)`", section))
    assert named, "the Pools section names no env dial; the parser has drifted"
    assert named <= registry.all_knob_env_vars(), (
        f"spec/db-access.md declares {sorted(named - registry.all_knob_env_vars())} "
        "but the registry does not, so nothing scrubs them and the manifest "
        "cannot evidence them"
    )


def test_the_two_spec_files_agree_about_redis_pool_max(flat):
    """spec/cache.md and this file describe the SAME knob and contradicted it.

    `spec/cache.md` said `REDIS_POOL_MAX` "applies literally to go-redis" -- a
    client no round-2 slot uses -- while `spec/db-access.md`'s pool table said it
    binds to no pool on either side, and `spec/runtime-stats.md` gave a third
    version. Both are frozen into `config_hash`, and a reviewer handed two frozen
    files that disagree cannot tell which one the run honoured. No test read
    `spec/cache.md` at all before this one.
    """
    from ledgerbench.util import paths

    claim = "`REDIS_POOL_MAX` binds to no pool on EITHER side"
    cache = " ".join((paths.spec_dir() / "cache.md").read_text(encoding="utf-8").split())
    assert claim in flat and claim in cache
    assert "go-redis" not in cache, (
        "spec/cache.md still pins a round-1 Redis client; no round-2 slot uses one"
    )
