"""/runtime-stats per-window flush: both edges, error accounting, the trace.

Round 1 polled during WARMUP ONLY and discarded every snapshot, so no measured
window in its artifacts has GC evidence of any kind (R1-B3). These tests pin the
three properties that make the flush worth having: the counters are turned into
window deltas, an unpolled window is distinguishable from a failed one and from
a real zero, and a kill mid-window leaves valid lines.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ledgerbench.runtimestats import (
    PHASE_MEASURED,
    PollWindow,
    RuntimeStats,
    Snapshot,
    StatsPoller,
)
from ledgerbench.util.clock import rfc3339_micros

SLOT = "dotnet"


def _stats(*, gen0=0, gen1=0, gen2=0, pause=0.0, allocated=0, committed=0,
           in_use=0, uptime=0.0, calls=None, acquires=0):
    return RuntimeStats.model_validate({
        "uptime_seconds": uptime,
        "gc": {
            "collections": {"gen0": gen0, "gen1": gen1, "gen2": gen2},
            "pause_total_seconds": pause,
            "heap_in_use_bytes": in_use,
            "heap_committed_bytes": committed,
            "heap_goal_bytes": committed,
            "allocated_total_bytes": allocated,
        },
        "db_pool": {"max": 24, "total": 24, "idle": 20, "in_use": 4,
                    "acquire_count": acquires},
        "redis_pool": {"max": 16, "total": 16, "idle": 16, "in_use": 0},
        "goroutines_or_threadpool": 12,
        "endpoint_calls": calls or {},
    })


def _snap(stats, *, t, request_ms=1.0):
    return Snapshot(captured_utc=rfc3339_micros(), monotonic_s=t, stats=stats,
                    request_ms=request_ms)


def _window(snaps, **kw):
    return PollWindow(phase=PHASE_MEASURED, slot=SLOT, snapshots=snaps,
                      n_polls=len(snaps), **kw)


# --------------------------------------------------------------------------- #
# the delta contract
# --------------------------------------------------------------------------- #
def test_counters_are_window_deltas_not_process_lifetime_totals():
    """Every counter here is a PROCESS-lifetime total.

    The SUT container is force-recreated per probe, so an end-only flush
    publishes "how much this process has ever done", which for the first probe
    happens to look right and for a long soak window is off by the whole warmup.
    Only end-minus-start is the window.
    """
    w = _window([
        _snap(_stats(gen0=100, gen2=4, pause=1.5, allocated=1_000, uptime=30.0,
                     calls={"GET /invoices/{id}": 5_000}, acquires=900), t=10.0),
        _snap(_stats(gen0=340, gen2=6, pause=2.1, allocated=9_000, uptime=150.0,
                     calls={"GET /invoices/{id}": 65_000}, acquires=4_900), t=130.0),
    ])
    d = w.deltas()
    assert d["gc_collections_gen0"] == 240
    assert d["gc_collections_gen2"] == 2
    assert d["gc_collections_total"] == 242
    assert d["gc_pause_total_seconds"] == pytest.approx(0.6)
    assert d["gc_allocated_total_bytes"] == 8_000
    assert d["db_pool_acquire_count"] == 4_000
    assert d["endpoint_calls"]["GET /invoices/{id}"] == 60_000
    # uptime_seconds is the SUT's own view of the window length
    assert d["uptime_seconds"] == pytest.approx(120.0)
    assert w.window_seconds == pytest.approx(120.0)
    # both edges survive, so every delta is recomputable from the artifact
    doc = w.summary_dict()
    assert doc["start"]["stats"]["gc"]["collections"]["gen0"] == 100
    assert doc["end"]["stats"]["gc"]["collections"]["gen0"] == 340


def test_levels_are_not_delta_d_and_carry_the_in_window_max():
    """Heap size and pool occupancy are levels; a difference of two is not a
    window figure. The in-window max is what the heap trace that decides D6
    needs, and the per-poll series behind it is in the JSONL."""
    w = _window([
        _snap(_stats(committed=100, in_use=40), t=0.0),
        _snap(_stats(committed=900, in_use=80), t=1.0),
        _snap(_stats(committed=300, in_use=50), t=2.0),
    ])
    lv = w.levels()
    assert lv["gc_heap_committed_bytes"] == {"start": 100, "end": 300, "max": 900}
    assert "gc_heap_committed_bytes" not in w.deltas()


def test_a_missing_counter_yields_null_never_zero():
    """The JVM reports `gc.collections.gen0/gen1/gen2` and, under ZGC,
    `pause_total_seconds` as NULL (they mean three different things across
    G1/ZGC/the CLR, and ZGC's pause total is identically 0.0 because the bean is
    ms-granular). The arithmetic here is null-safe, so the honest report does not
    have to fight it."""
    a, b = _stats(gen0=1), _stats(gen0=9)
    b.gc.collections.gen2 = None
    w = _window([_snap(a, t=0.0), _snap(b, t=1.0)])
    d = w.deltas()
    assert d["gc_collections_gen0"] == 8
    assert d["gc_collections_gen2"] is None       # null, not 0
    # ...and the total says which generations it actually covers, rather than
    # silently standing for all three.
    assert d["gc_collections_total"] == 8
    assert d["gc_collections_generations_summed"] == ["gen0", "gen1"]


# A REDUCED body carrying the JVM-specific rows this module is about: the two
# blocks with a present null, and the per-bean counters the JVM publishes beside
# the frozen triple. G1 reports a real pause total; ZGC reports null there too.
# Deliberately NOT the whole wire body -- `ts` here is not the frozen six-digit
# format and `config`/`endpoint_calls` are trimmed, so `normalize()` and
# `shapecheck` both reject it, and it must not be read as evidence of what the
# service emits. The frozen shape is pinned against the emitters themselves in
# validation/tests/test_runtime_stats_contract.py, both sides.
_JAVA_G1 = {
    "ts": "2026-08-23T12:00:00Z", "uptime_seconds": 42.0,
    "build": {"language": "java", "runtime_version": "25.0.4+7",
              "service_version": "dev", "data_layer": "jpa", "processor_count": 6},
    "config": {"db_pool_max": 24, "max_auto_prepare": None, "gogc": None,
               "gomemlimit": None, "gc_dynamic_adaptation_mode": None,
               "jdbc_prepare_threshold": 5, "gc_engine": "G1"},
    "gc": {"collections": {"gen0": None, "gen1": None, "gen2": None,
                           "G1 Young Generation": 118, "G1 Concurrent GC": 9,
                           "G1 Old Generation": 0},
           "collections_note": "gen0/gen1/gen2 are null on the JVM: ...",
           "pause_total_seconds": 0.412,
           "heap_in_use_bytes": 1, "heap_committed_bytes": 2,
           "heap_goal_bytes": 3, "allocated_total_bytes": 4},
    "db_pool": {"max": 24, "total": 24, "idle": 24, "in_use": 0,
                "acquire_count": None, "empty_acquire_count": None,
                "acquire_wait_total_seconds": None},
    "redis_pool": {"max": 1, "total": 1, "idle": 0, "in_use": 0},
    "goroutines_or_threadpool": 1,
    "endpoint_calls": {"GET /invoices/{id}": 10},
}


def _java_zgc() -> dict:
    body = json.loads(json.dumps(_JAVA_G1))
    body["config"]["gc_engine"] = "ZGC"
    body["gc"]["pause_total_seconds"] = None
    body["gc"]["pause_total_seconds_note"] = "ms-granular bean, ZGC pauses are not"
    body["gc"]["collections"] = {
        "gen0": None, "gen1": None, "gen2": None,
        "ZGC Minor Cycles": 40, "ZGC Minor Pauses": 120,
        "ZGC Major Cycles": 3, "ZGC Major Pauses": 9,
    }
    return body


@pytest.mark.parametrize("body", [_JAVA_G1, _java_zgc()], ids=["g1", "zgc"])
def test_the_java_runtime_stats_body_parses_at_all(body):
    """The run-stopper, in one assertion.

    `gen0/gen1/gen2` and `pause_total_seconds` were non-nullable, so EVERY 1 Hz
    poll of the Java SUT raised `ValidationError` -- three of them under G1, four
    under ZGC. `StatsPoller.poll_once` catches `ValueError` and counts a poll
    error, so the Java slot would have collected ZERO snapshots for the whole
    run: `pools_warm` and `per_endpoint_min_calls` could never pass, and
    `gc_steady` scored green on no samples at all.
    """
    st = RuntimeStats.model_validate(body)
    assert st.build.language == "java"
    assert st.gc.collections.total() is None
    assert st.gc.collections.generations_present() == []
    assert st.db_pool.total == 24
    # The per-bean counters are the JVM's REAL figures and the writer puts them
    # beside the null triple. They must survive into the flushed trace verbatim,
    # or the only honest GC evidence the Java slot produces is dropped on the way
    # to disk.
    beans = {k: v for k, v in st.gc.collections.model_extra.items()}
    assert beans and all(isinstance(v, int) for v in beans.values())
    line = json.loads(_window([_snap(st, t=0.0)]).jsonl_text().splitlines()[0])
    assert line["stats"]["gc"]["collections"] == body["gc"]["collections"]


def test_a_runtime_reporting_no_generation_at_all_totals_to_null_not_zero():
    """`total()` feeds the GC-steady cadence series, so a fabricated 0 here is a
    perfectly flat cadence -- a gate passing on a runtime that published nothing.

    Same convention as the window-delta path (`sum_present`): sum what was
    reported, name it, and answer None when nothing was.
    """
    from ledgerbench.runtimestats import GcCollections

    assert GcCollections(gen0=None, gen1=None, gen2=None).total() is None
    partial = GcCollections(gen0=7, gen1=None, gen2=None)
    assert partial.total() == 7
    assert partial.generations_present() == ["gen0"]


def test_an_absent_key_parses_as_not_measured_never_as_zero():
    """spec/runtime-stats.md pins every key PRESENT (null where unmapped), so an
    absent key is a contract violation -- and the old defaults turned it into
    measured zeros: `{"gc": {}}` parsed to a flat all-zero cadence and heap
    series, and `gc_steady` passed vacuously on a body that reported nothing.
    Absent must land in the same place a present null does: None, which
    `sum_present`/`usable()` drop and the trend gates score `insufficient_data`.
    """
    from ledgerbench.runtimestats import RuntimeStats
    from ledgerbench.warmup import pools_warm

    st = RuntimeStats.model_validate({"gc": {}})
    assert st.gc.collections.total() is None
    assert st.gc.collections.cadence(()) == (None, "unreported", [])
    assert st.gc.heap_committed_bytes is None
    assert st.gc.heap_in_use_bytes is None
    assert st.gc.allocated_total_bytes is None
    assert st.gc.pause_total_seconds is None
    # an empty body must not pass pools_warm on default zeros either
    empty = RuntimeStats.model_validate({})
    assert pools_warm(empty) is False
    assert empty.uptime_seconds is None


# --------------------------------------------------------------------------- #
# not-measured vs failed vs zero
# --------------------------------------------------------------------------- #
def test_a_zero_poll_window_says_not_measured_with_nulls():
    doc = PollWindow(phase=PHASE_MEASURED, slot=SLOT).summary_dict()
    assert doc["source"] == "not_measured"
    assert doc["n_polls"] == 0
    assert doc["start"] is None and doc["end"] is None
    assert doc["deltas"] == {}                    # no zeros anywhere
    assert doc["window_seconds"] is None
    assert "NOT a measured zero" in doc["note"]


def test_an_all_failed_window_is_distinguishable_from_an_unpolled_one():
    """Round 1's loop swallowed every poll error with a bare `pass`.

    A window where /runtime-stats 500'd on every tick, a window where the
    poller never ran, and a window whose counters were genuinely zero were all
    the same artifact: nothing.
    """
    failed = PollWindow(phase=PHASE_MEASURED, slot=SLOT, n_polls=0,
                        n_poll_errors=120, last_error="HTTPStatusError: 503")
    doc = failed.summary_dict()
    assert doc["n_poll_errors"] == 120
    assert doc["last_error"].startswith("HTTPStatusError")
    assert doc["source"] == "not_measured"
    unpolled = PollWindow(phase=PHASE_MEASURED, slot=SLOT).summary_dict()
    assert unpolled["n_poll_errors"] == 0
    assert doc != unpolled


def test_poll_errors_are_counted_and_the_reason_kept(monkeypatch):
    """The counting lives in `poll_once`, reachable with no thread and no SUT."""
    poller = StatsPoller("http://nowhere.invalid", phase=PHASE_MEASURED, slot=SLOT)

    def boom(base_url, client=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("ledgerbench.runtimestats.fetch_once", boom)
    assert poller.poll_once() is None
    assert poller.poll_once() is None
    w = poller.window()
    assert w.n_polls == 0
    assert w.n_poll_errors == 2
    assert "connection refused" in w.last_error


def test_a_successful_poll_records_its_round_trip(monkeypatch):
    """The poll cost gets a MEASURED upper bound, not an assumed constant."""
    monkeypatch.setattr("ledgerbench.runtimestats.fetch_once",
                        lambda base_url, client=None: _stats(gen0=1))
    poller = StatsPoller("http://sut", phase=PHASE_MEASURED, slot=SLOT)
    poller.poll_once()
    w = poller.window()
    assert w.n_polls == 1 and w.n_poll_errors == 0
    cost = w.poll_cost()
    assert cost["n_polls"] == 1
    assert cost["round_trip_ms_total"] is not None
    # /runtime-stats is not in vegeta's denominator: the note has to say so.
    assert "no denominator" in cost["note"]


# --------------------------------------------------------------------------- #
# the trace
# --------------------------------------------------------------------------- #
def test_jsonl_is_one_reparseable_object_per_poll_with_extra_fields_kept():
    """N snapshots -> N lines, and a field the frozen model does not declare
    survives the round trip (the models are `extra="allow"` precisely so a
    runtime can report more than the spec froze)."""
    st = _stats(gen0=3)
    st.gc.__pydantic_extra__["jvm_specific_thing"] = 7
    # NOT t=0: `time.monotonic()` is an arbitrary epoch, so a window that
    # starts at 0 makes an absolute reading and an offset indistinguishable --
    # which would let `window_offset_s` be the raw clock and still look right.
    w = _window([_snap(st, t=8_421.5), _snap(_stats(gen0=9), t=8_422.5)])
    lines = w.jsonl_text().splitlines()
    assert len(lines) == 2
    docs = [json.loads(x) for x in lines]
    assert [d["window_offset_s"] for d in docs] == [0.0, 1.0]
    assert [d["monotonic_s"] for d in docs] == [8_421.5, 8_422.5]
    assert all(d["phase"] == PHASE_MEASURED and d["slot"] == SLOT for d in docs)
    assert docs[0]["stats"]["gc"]["jvm_specific_thing"] == 7
    assert docs[0]["request_ms"] is not None


def test_a_kill_after_k_polls_leaves_k_valid_lines():
    """Line-per-poll, not one array: a truncated JSON document is unreadable,
    a truncated JSONL file is k good records and one bad tail."""
    w = _window([_snap(_stats(gen0=i), t=float(i)) for i in range(5)])
    text = w.jsonl_text()
    truncated = text[: int(len(text) * 0.62)]
    good = [ln for ln in truncated.splitlines() if ln.endswith("}")]
    assert 0 < len(good) < 5
    for line in good:
        json.loads(line)


def test_write_emits_the_summary_even_with_no_snapshots(tmp_path):
    """The summary is ALWAYS written; the trace only when there is one.

    A missing summary file and a summary saying `not_measured` are different
    claims, and only the second one survives someone grepping the tree.
    """
    paths_written = PollWindow(phase=PHASE_MEASURED, slot=SLOT).write(tmp_path, SLOT)
    assert [p.name for p in paths_written] == [f"{SLOT}.runtime_stats.summary.json"]
    assert not (tmp_path / f"{SLOT}.runtime_stats.jsonl").exists()

    w = _window([_snap(_stats(gen0=1), t=0.0), _snap(_stats(gen0=2), t=1.0)])
    written = w.write(tmp_path, SLOT)
    assert {p.name for p in written} == {
        f"{SLOT}.runtime_stats.summary.json", f"{SLOT}.runtime_stats.jsonl",
    }
    doc = json.loads((tmp_path / f"{SLOT}.runtime_stats.summary.json").read_text())
    assert doc["deltas"]["gc_collections_gen0"] == 1


# --------------------------------------------------------------------------- #
# the GC-steady cadence source (W27)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body, expected_total, expected_names",
    [
        (_JAVA_G1, 118, ["G1 Young Generation", "G1 Old Generation"]),
        (_java_zgc(), 43, ["ZGC Minor Cycles", "ZGC Major Cycles"]),
    ],
    ids=["g1", "zgc"],
)
def test_the_jvm_cadence_sums_the_cycle_beans_and_nothing_else(
    body, expected_total, expected_names,
):
    """W27's declaration, against the bean sets a JVM actually publishes.

    With the generation triple null the cadence series was empty at every poll,
    so `gc_steady` returned `insufficient_data` and would have flagged EVERY
    Java probe under-warmed. The counts beside the triple are real; the mapping
    onto gen0/1/2 was the dishonest part. Summing all of them is not the fix:
    the G1 body here would read 127 instead of 118 (`G1 Concurrent GC` counts
    STW sub-pauses of a mark cycle) and the ZGC body 172 instead of 43 (the
    `*Pauses` beans cover the same pools as the `*Cycles` beans).
    """
    from ledgerbench import registry
    from ledgerbench.runtimestats import CADENCE_COLLECTOR_CYCLES

    collections = RuntimeStats.model_validate(body).gc.collections
    total, basis, names = collections.cadence(
        registry.gc_semantics("java").cadence_beans
    )
    assert (total, basis, names) == (
        expected_total, CADENCE_COLLECTOR_CYCLES, expected_names,
    )
    # ...and the triple it stands in for is still null, not a fabricated sum.
    assert collections.total() is None


def test_a_runtime_publishing_generations_is_scored_on_them_not_on_bean_names():
    """The .NET path must not change, and no snapshot may use two bases at once."""
    from ledgerbench.runtimestats import (
        CADENCE_GENERATIONS,
        CADENCE_UNREPORTED,
        GcCollections,
    )

    clr = GcCollections(gen0=5, gen1=2, gen2=1)
    assert clr.cadence(("G1 Young Generation",)) == (
        8, CADENCE_GENERATIONS, ["gen0", "gen1", "gen2"],
    )
    # Precedence is DECLARED, not an accident of which branch is written first.
    # A runtime that grew a per-collector key beside a triple it still publishes
    # would otherwise change basis mid-contract, silently and without a rename.
    both = GcCollections.model_validate(
        {"gen0": 5, "gen1": 2, "gen2": 1, "ZGC Minor Cycles": 900}
    )
    assert both.cadence(("ZGC Minor Cycles",)) == (
        8, CADENCE_GENERATIONS, ["gen0", "gen1", "gen2"],
    )
    # A bean this file does not name is DROPPED, never guessed at: an inclusion
    # list undercounts a new collector's cadence, an exclusion list would
    # silently double-count it, and only one of those two failures is safe.
    unknown = GcCollections.model_validate(
        {"gen0": None, "gen1": None, "gen2": None, "Shenandoah Cycles": 12}
    )
    assert unknown.cadence(("ZGC Minor Cycles",)) == (None, CADENCE_UNREPORTED, [])
