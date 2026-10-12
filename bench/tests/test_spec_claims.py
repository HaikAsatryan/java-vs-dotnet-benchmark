"""Frozen `spec/` sentences that a live service or the harness can falsify.

`spec/` is hashed into `config_hash` and frozen at P7, so every sentence in it
publishes as pre-registered method. Round 1's overclaims (R1-B6) were sentences
true when written and not re-checked afterwards, in files no test read.
`test_db_access_spec.py` covers `spec/db-access.md`'s arithmetic and its
per-checkout table; this module covers the claims measured false on 2026-08-24,
so that restoring any of them fails rather than ships.
"""

from __future__ import annotations

import re

import pytest

from ledgerbench.util import paths


def _flat(name: str) -> str:
    """One spec file with runs of whitespace collapsed.

    The prose is hard-wrapped, so a sentence that must be asserted is split at
    an arbitrary column. Assert on the sentence, not on the wrap.
    """
    return " ".join((paths.spec_dir() / name).read_text(encoding="utf-8").split())


@pytest.fixture(scope="module")
def cache() -> str:
    return _flat("cache.md")


@pytest.fixture(scope="module")
def runtime_stats() -> str:
    return _flat("runtime-stats.md")


def test_the_two_spec_files_agree_that_redis_pool_in_use_is_not_symmetric(cache, runtime_stats):
    """`in_use` reports a DIFFERENT QUANTITY on each stack, and both files must say so.

    Measured 2026-08-24 against both live services under 40-way concurrent
    `GET /invoices/{id}`: Java peaked at `in_use` 0, .NET at 39. Java writes a
    literal zero (`RuntimeStatsWriter.redisPool`); .NET writes SE.Redis's
    `GetCounters().TotalOutstanding` (`RuntimeStatsEndpoints.RedisCounters`),
    commands in flight at the instant of the poll. `spec/cache.md` said the pair
    was "0 by construction on both sides for the same reason" and cited
    `spec/runtime-stats.md`, whose own row credited `GetCounters()` for `idle`,
    which is a literal 0 in the same tuple. Two frozen files describing the same
    two fields differently is the defect the `REDIS_POOL_MAX` test in
    `test_db_access_spec.py` covers for that knob.
    """
    dead = "are 0 by construction on both sides"
    assert dead not in cache, "spec/cache.md has restored a claim measured false on .NET"

    for name, text in (("cache.md", cache), ("runtime-stats.md", runtime_stats)):
        assert "GetCounters().TotalOutstanding" in text, (
            f"spec/{name} must name what .NET's redis_pool.in_use actually reports"
        )
    # The Java side is a literal, and both files must say literal rather than
    # "by construction" -- the phrase that made the .NET cell look symmetric.
    assert "literal 0" in cache and "literal `0`" in runtime_stats


def test_the_cache_write_carries_the_wire_text_of_BOTH_stacks(cache):
    """`spec/db-access.md`'s house rule -- wire text, not a rendering -- reaches here.

    Captured from Redis MONITOR 2026-08-24 against both live services: Java
    sends `"SET" "invoice:11" "<body>" "EX" "300"`, .NET sends
    `"SETEX" "invoice:12" "300" "<body>"`. Semantically one operation and one
    round trip either way, but the file declared only the `SET ... EX` spelling
    and .NET does not send it.
    """
    assert '`"SETEX" "invoice:12" "300" "<body>"`' in cache
    assert '`"SET" "invoice:11" "<body>" "EX" "300"`' in cache
    # The create-path invalidation IS the same command on both (MONITOR, same
    # capture: `DEL invoice:5173` from Java, `DEL invoice:5174` from .NET), so
    # the single-form declaration there is correct and must stay single.
    assert "After commit: `DEL invoice:{new_id}`" in cache


# One entry per token that named a round-1 consumer or mechanism. `spec/` is the
# frozen tree a P7 reviewer reads; a dead consumer declared in it is a claim
# about who reads the file, and it was wrong in three files at once.
#
# `pgx` is NOT here: `seed/` is Go by decision (PROGRESS.md) and genuinely uses
# pgx binary COPY, so it is allowed in db-access.md's seeder section and banned
# everywhere else -- see the test below.
_DEAD_TOKENS = {
    "sqlc": "the Go code generator; round 2 has no Go SUT and nothing consumes spec SQL with it",
    "MaxBytesReader": "Go's body limiter; .NET uses Kestrel MaxRequestBodySize, Java BodySizeLimitFilter",
    "go-redis": "round 1's Redis client; neither round-2 slot uses one",
    # Banned until a cold-start curve artifact exists: a warmup note promising
    # durations "overlaid on the cold-start curve" would be a hashed promise of
    # an overlay nothing produces.
    "cold-start curve": "PROGRESS.md keeps it in scope; spec/ may name it once it exists",
}


@pytest.mark.parametrize("token", sorted(_DEAD_TOKENS), ids=sorted(_DEAD_TOKENS))
def test_no_frozen_spec_file_declares_a_dead_consumer_or_mechanism(token):
    hits = [
        f"{path.name}:{n}"
        for path in sorted(paths.spec_dir().rglob("*"))
        if path.is_file()
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if token in line
    ]
    assert not hits, f"spec/ names {token!r} ({_DEAD_TOKENS[token]}): {hits}"


def test_pgx_appears_only_where_the_go_seeder_is_the_subject():
    """`seed/` stays Go, so `pgx` is correct in exactly one section and nowhere else.

    A blanket ban would corrupt a true statement; a blanket allowance is how
    `spec/schema.sql` offered "pgx RETURNING in the batch" as the round-2
    mechanism for BOTH stacks, in a round where neither stack is pgx.
    """
    offenders = {}
    for path in sorted(paths.spec_dir().rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "pgx" not in text:
            continue
        if path.name != "db-access.md":
            offenders[path.name] = "pgx belongs to the seeder, documented in db-access.md"
            continue
        seeder = text.split("## Seeder contract", 1)
        assert len(seeder) == 2, "db-access.md's seeder section header has moved"
        if "pgx" in seeder[0] or "\n## " in seeder[1].split("pgx", 1)[0]:
            offenders[path.name] = "pgx outside db-access.md's seeder section"
    assert not offenders, offenders


def test_every_deferred_variant_cell_says_it_is_not_part_of_a_default_run():
    """A "declared variant" a plain `run-all` never executes must say so.

    `spec/db-access.md` presented `variant.npgsql-default-prepare` as a declared
    variant that "quantifies" the prepare lever with no disclosure that it is
    `deferred=True` in `phases.CELLS`, so nothing runs it unless the operator
    names it in `LEDGERBENCH_RUN_VARIANTS`. The cell table is the fact; this
    asserts the frozen prose tracks it.
    """
    from ledgerbench import phases

    deferred = [c.cell_id for c in phases.CELLS if c.deferred]
    assert deferred, "no cell is deferred; this test's subject has gone"

    text = _flat("db-access.md")
    named = [c for c in deferred if c in text]
    assert named, f"spec/db-access.md names none of the deferred cells {deferred}"
    assert "NOT part of a default run" in text
    assert "deferred=True" in text and phases.RUN_VARIANTS_ENV in text


def test_the_logging_spec_claims_only_evidence_that_exists():
    """The sink's queue-full MODE is guarded on one side and echoed by neither.

    `spec/logging.md` declared, in a section titled "What the validation suite
    asserts", that block-on-full "is echoed by each service's config line and
    recorded in the manifest". Measured 2026-08-24 on both live services:
    .NET's `msg="config"` line carries no queue-full field at all, Java's
    carries `log_block_on_full: true` as a constant `EffectiveConfig` writes
    rather than reads back, `manifest.Knobs` has no such field, and no module in
    `bench/` parses a `config` log line -- `phases._record_cell_knobs` builds
    `Knobs` from the scrubbed environment plus `/runtime-stats` `build`. What
    the file may claim is the knob that IS wired, which this asserts end to end.
    """
    from ledgerbench import compose, registry
    from ledgerbench.manifest import Knobs

    text = _flat("logging.md")
    assert "is echoed by each service's config line and recorded in the manifest" not in text

    assert registry.SHARED_KNOB_ENV["LOG_QUEUE_LEN"] == "log_queue_len"
    assert "LOG_QUEUE_LEN" in compose.KNOB_ENV_VARS
    assert "log_queue_len" in Knobs.model_fields
    for token in ("registry.SHARED_KNOB_ENV", "compose.KNOB_ENV_VARS",
                  "manifest.Knobs.log_queue_len", "config.log_queue_len"):
        assert token in text, f"spec/logging.md no longer names {token}"

    # No module may start deriving a manifest field from the config line without
    # this file's evidence bullet changing with it.
    parsers = [
        p.name for p in sorted((paths.repo_root() / "bench" / "ledgerbench").glob("*.py"))
        if re.search(r'msg["\']?\s*[:=]=?\s*["\']config["\']', p.read_text(encoding="utf-8"))
    ]
    assert not parsers, f"{parsers} now parse a config log line; spec/logging.md must say so"
