"""Warmup gate predicates (spec/slo.yaml warmup)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ledgerbench import registry, warmup
from ledgerbench.config import PostureWaiver, load_slo
from ledgerbench.registry import DEFAULT_SLOT_SET, get_slot_set
from ledgerbench.runtimestats import DbPool, Gc, GcCollections, RedisPool, RuntimeStats, Snapshot
from ledgerbench.warmup import (
    EXIT_ATTACK_FAILED_PREFIX,
    EXIT_GATES_PASSED,
    FLATNESS_TRAILING_SAMPLES,
    IMPLEMENTED_GATES,
    MANN_KENDALL_ALPHA,
    STATUS_NOT_APPLICABLE,
    STATUS_NOT_IMPLEMENTED,
    STATUS_POSTURE_WAIVED,
    GateResult,
    WarmupResult,
    WarmupState,
    WarmupThresholds,
    endpoint_calls_met,
    gate_set_for_cell,
    gc_steady,
    no_positive_trend,
    not_applicable_gate,
    pools_warm,
    posture_waived_gate,
)

SLOT_A, SLOT_B = get_slot_set(DEFAULT_SLOT_SET).slots


def _stats(*, db_total=24, db_max=24, redis_total=4, calls=None, committed=0, gc_total=0) -> RuntimeStats:
    return RuntimeStats(
        db_pool=DbPool(max=db_max, total=db_total),
        redis_pool=RedisPool(max=16, total=redis_total),
        gc=Gc(heap_committed_bytes=committed, collections=GcCollections(gen2=gc_total)),
        endpoint_calls=calls or {},
    )


def test_no_positive_trend_flat():
    assert no_positive_trend([10, 10, 10, 10]).passed
    assert no_positive_trend([10, 9, 10, 9, 8]).passed  # declining/jitter


def test_no_positive_trend_rising_fails():
    assert not no_positive_trend([1, 2, 3, 4, 5, 6]).passed


def test_a_series_too_short_to_test_is_insufficient_data_and_never_a_pass():
    """The silent pass docs/p1-plan.md 3.2 step 1 names, closed.

    Mann-Kendall is undefined below three points, so `len(series) < 3` was not a
    lenient test -- it was no test, reported as a green gate. `live_runner`
    leaves the p99/p999 series EMPTY whenever the per-second encode fails, and a
    SUT whose /runtime-stats the poller cannot parse leaves every GC series
    empty, so the vacuous branch is the one a real failure lands in.
    """
    for series in ([], [1], [1, 2]):
        v = no_positive_trend(series)
        assert v.status == warmup.STATUS_INSUFFICIENT_DATA
        assert v.passed is False
        assert v.n == len(series)
    # Three points is the first length that can carry a verdict either way.
    assert no_positive_trend([1, 1, 1]).status == warmup.STATUS_MEASURED


def test_nulls_are_dropped_rather_than_read_as_zero():
    """A value the runtime did not report is not a value of 0.

    Coercing it would inject a step change into the series a FLATNESS test
    reads, which is the one place a fabricated zero does maximum damage: a
    rising series with one null coerced to 0.0 stops looking monotone.
    """
    v = no_positive_trend([None, 5.0, None, 5.0, 5.0, None])
    assert (v.status, v.passed, v.n) == (warmup.STATUS_MEASURED, True, 3)
    # Two real points behind six entries is still not enough to test.
    assert no_positive_trend([None, 5.0, None, None, 6.0]).status == (
        warmup.STATUS_INSUFFICIENT_DATA
    )
    # And a series that is ENTIRELY null scores nothing at all.
    assert no_positive_trend([None] * 30).n == 0


def test_pools_warm_requires_total_eq_max():
    assert pools_warm(_stats(db_total=24, db_max=24, redis_total=2))
    assert not pools_warm(_stats(db_total=20, db_max=24, redis_total=2))
    assert not pools_warm(_stats(db_total=24, db_max=24, redis_total=0))


def test_endpoint_calls_gate():
    mins = {"get_invoice": 100, "healthz": 10}
    s = _stats(calls={"GET /invoices/{id}": 150, "GET /healthz": 5})
    ok, shortfall = endpoint_calls_met(s, mins)
    assert not ok
    assert shortfall == {"GET /healthz": 5}
    s2 = _stats(calls={"GET /invoices/{id}": 150, "GET /healthz": 20})
    ok2, shortfall2 = endpoint_calls_met(s2, mins)
    assert ok2 and shortfall2 == {}


def test_warmup_state_all_pass():
    slo = load_slo()
    cfg = slo.warmup
    # shrink the per-endpoint minimums so the synthetic snapshot can satisfy them
    cfg = cfg.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, slot=SLOT_A, rate=500)
    calls = {k: 999999 for k in [
        "GET /invoices/{id}", "GET /customers/{id}/invoices", "POST /pricing/quote",
        "POST /invoices", "POST /invoices/{id}/pdf-stub", "GET /healthz",
    ]}
    # feed several flat snapshots
    for i in range(6):
        s = _stats(calls=calls, committed=1_000_000, gc_total=i)  # gc cadence flat (+1/tick)
        snap = Snapshot(captured_utc="t", monotonic_s=float(i), stats=s)
        st.observe(snap, p99_ms=5.0, p999_ms=8.0)
    by_name = {g.name: g for g in st.evaluate()}
    assert by_name["per_endpoint_min_calls"].passed
    assert by_name["pools_warm"].passed
    assert by_name["p99_flat"].passed
    assert by_name["gc_steady"].passed
    # The spec declares exactly these, and evaluate() computes exactly these:
    # nothing is scored that spec/slo.yaml does not ask for.
    assert set(by_name) == set(cfg.gates) | {"per_endpoint_min_calls"}
    assert st.all_pass()


def test_a_declared_p999_flat_is_computed_and_still_enforces_nothing():
    """The retired gate's machinery, exercised through a synthetic declaration.

    At the harness's warmup rate nearest rank makes q=0.999 the second's
    maximum, so the record must be present, must carry the full statistic
    vector, must NOT enforce, and must not stop a warm probe from passing.
    """
    st = _flat_state(cfg=_cfg_declaring("p999_flat"))
    by_name = {g.name: g for g in st.evaluate()}
    p999 = by_name["p999_flat"]
    assert p999.enforced is False
    assert p999.detail["flatness"]["reason"] == warmup.REASON_QUANTILE_UNRESOLVED
    assert st.all_pass()


def test_warmup_state_detects_rising_p99():
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, slot=SLOT_B, rate=500)
    for i in range(6):
        s = _stats(calls={"GET /healthz": 10}, committed=1_000_000 + i * 50_000)  # committed rising
        snap = Snapshot(captured_utc="t", monotonic_s=float(i), stats=s)
        st.observe(snap, p99_ms=5.0 + i, p999_ms=8.0 + i)  # rising latency
    gates = {g.name: g.passed for g in st.evaluate()}
    assert not gates["p99_flat"]
    assert not gates["gc_steady"]  # committed climbing -> not steady
    assert not st.all_pass()


def _series(committed, cadence) -> dict[str, list[float]]:
    return {
        registry.SERIES_HEAP_COMMITTED: committed,
        registry.SERIES_COLLECTION_CADENCE: cadence,
    }


@pytest.mark.parametrize("gc", [registry.JVM_GC, registry.CLR_GC], ids=["jvm", "clr"])
def test_gc_steady_symmetric_cadence_and_committed(gc):
    # Both runtimes: cadence flat AND committed flat => steady.
    flat_committed = [1_000_000, 1_000_000, 1_000_000, 1_000_000]
    flat_cadence = [1, 1, 1, 1]
    assert gc_steady(_series(flat_committed, flat_cadence), gc)[0].passed
    # Climbing committed with flat cadence => NOT steady, for BOTH runtimes.
    rising_committed = [1_000_000, 1_100_000, 1_200_000, 1_300_000]
    assert not gc_steady(_series(rising_committed, flat_cadence), gc)[0].passed
    # Flat committed with accelerating cadence => NOT steady, for BOTH runtimes.
    rising_cadence = [1, 2, 3, 4]
    assert not gc_steady(_series(flat_committed, rising_cadence), gc)[0].passed


@pytest.mark.parametrize("gc", [registry.JVM_GC, registry.CLR_GC], ids=["jvm", "clr"])
def test_gc_steady_on_an_unmeasured_series_is_insufficient_data_not_steady(gc):
    """An unmeasured series is `insufficient_data`, never steady (R1-B1).

    A slot whose `/runtime-stats` the poller cannot parse collects ZERO
    snapshots, so every series this gate requires is empty -- and an empty
    series used to return "no positive trend", i.e. `gc_steady` reported green
    on no samples whatsoever. Either required series being unmeasurable is
    enough: `heap_committed` is the one the memory headline rests on and
    `collection_cadence` is null for the whole triple on the JVM.
    """
    flat = [1_000_000] * 4
    for series in (_series([], [1, 1, 1, 1]),
                   _series(flat, []),
                   _series([], [])):
        v, per_series = gc_steady(series, gc)
        assert v.status == warmup.STATUS_INSUFFICIENT_DATA
        assert v.passed is False
        assert set(per_series) == set(gc.requires)
    # A cadence series that is entirely null -- today's JVM, where all three of
    # gen0/gen1/gen2 are a present null -- is the same finding.
    v, per_series = gc_steady(_series(flat, [None] * 4), gc)
    assert v.status == warmup.STATUS_INSUFFICIENT_DATA
    assert per_series[registry.SERIES_HEAP_COMMITTED].status == warmup.STATUS_MEASURED
    assert per_series[registry.SERIES_COLLECTION_CADENCE].n == 0


def test_gc_steady_semantics_lookup_raises_on_an_unregistered_slot():
    """THE defect the registry exists to close.

    Round 1 dispatched `if lang == "dotnet": <.NET> else: <Go's single-generation
    model>`, so any other string silently got the wrong collector's semantics and
    the gate still reported `passed`.
    """
    with pytest.raises(KeyError, match="unregistered slot 'rust'"):
        registry.gc_semantics("rust")


def test_the_gate_record_names_the_series_it_tested():
    st = _flat_state()
    detail = {g.name: g.detail for g in st.evaluate()}["gc_steady"]
    gc = registry.gc_semantics(SLOT_A)
    assert detail["requires"] == list(gc.requires)
    assert detail["model"] == gc.model
    assert set(detail["samples"]) == set(gc.requires)


def test_a_probe_fails_gc_steady_on_a_climbing_heap():
    # GC cadence flat but heap_committed still climbing must NOT pass.
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, slot=SLOT_A, rate=500)
    calls = {k: 999999 for k in [
        "GET /invoices/{id}", "GET /customers/{id}/invoices", "POST /pricing/quote",
        "POST /invoices", "POST /invoices/{id}/pdf-stub", "GET /healthz",
    ]}
    for i in range(6):
        # gc cadence flat (+1/tick) but committed heap climbing 50 KB/tick
        s = _stats(calls=calls, committed=1_000_000 + i * 50_000, gc_total=i)
        snap = Snapshot(captured_utc="t", monotonic_s=float(i), stats=s)
        st.observe(snap, p99_ms=5.0, p999_ms=8.0)
    gates = {g.name: g.passed for g in st.evaluate()}
    assert not gates["gc_steady"]  # climbing heap -> not steady
    assert not st.all_pass()


# --------------------------------------------------------------------------- #
# R1-B1: the governing rule, mechanised; then the machinery that catches
# a violation of it anyway, exercised against a synthetic gate.
# --------------------------------------------------------------------------- #
def test_every_gate_the_spec_declares_is_one_the_runner_implements():
    """THE governing rule as an assertion (CLAUDE.md; docs/p1-plan.md W23).

    Round 1 declared six warmup gates and evaluated four. This is the check that
    makes declaring a seventh ahead of its implementation a red test rather than
    a disclosure note. ALL declared keys count, including any set to `false`: a
    gate written down is a claim about the methodology whether or not this run
    enforces it.
    """
    assert set(load_slo().warmup.gates) - set(IMPLEMENTED_GATES) == set()


def _flat_state(slot=SLOT_A, cfg=None, rate=500):
    slo = load_slo()
    cfg = cfg or slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, slot=slot, rate=rate)
    for i in range(6):
        s = _stats(calls={"GET /healthz": 10}, committed=1_000_000, gc_total=i)
        st.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=s),
                   p99_ms=5.0, p999_ms=8.0)
    return st


# W29 deleted `p999_flat` from spec/slo.yaml warmup.gates: at every rate this
# harness warms at, nearest-rank q=0.999 over a one-second bucket is the
# second's largest-or-next-largest sample, so the gate could never enforce
# (docs/p1-plan.md 5.2). The quantile-GENERIC machinery is deliberately kept --
# `FLATNESS_GATES` still carries the row and its resolution floor -- so the
# coverage is kept too, by declaring the gate synthetically here exactly as
# `_state_declaring_an_unimplemented_gate` declares its subject. A spec that
# does not declare the gate and a test suite that never exercises the
# non-enforcing path would be two silences, not one honesty.
def _cfg_declaring(*gate_names, **update):
    slo = load_slo()
    return slo.warmup.model_copy(update={
        "per_endpoint_min_calls": {"healthz": 1},
        "gates": {**slo.warmup.gates, **{g: True for g in gate_names}},
        **update,
    })


# W23 deleted spec/slo.yaml's two declared-but-unimplemented gates
# (`hot_set_touched`, `major_faults_asserted_equal`; docs/p1-plan.md 5.1), so
# the spec no longer supplies a subject for these tests. The machinery is kept
# for any gate declared ahead of its implementation, and the coverage is kept by
# declaring one HERE -- a name the spec must never contain, which is the point of
# the governing-rule test above.
SYNTHETIC_UNIMPLEMENTED = "synthetic_gate_no_runner_implements"


def _state_declaring_an_unimplemented_gate():
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={
        "per_endpoint_min_calls": {"healthz": 1},
        "gates": {**slo.warmup.gates, SYNTHETIC_UNIMPLEMENTED: True},
    })
    st = _flat_state()
    st.cfg = cfg
    return st


def test_declared_but_unimplemented_gates_are_recorded():
    st = _state_declaring_an_unimplemented_gate()
    gates = {g.name: g for g in st.evaluate()}
    assert SYNTHETIC_UNIMPLEMENTED in gates, "must be visible in the probe record"
    assert gates[SYNTHETIC_UNIMPLEMENTED].status == STATUS_NOT_IMPLEMENTED
    assert gates[SYNTHETIC_UNIMPLEMENTED].passed is False   # never reads as a pass
    assert gates[SYNTHETIC_UNIMPLEMENTED].enforced is False  # never decides under-warmed


def test_unimplemented_gates_do_not_flag_a_warm_probe():
    st = _state_declaring_an_unimplemented_gate()
    # every IMPLEMENTED gate passes, so the probe is warm despite the
    # not_implemented record (otherwise every probe would be under-warmed).
    assert st.all_pass() is True
    assert {g.name for g in st.unimplemented_gates()} == {SYNTHETIC_UNIMPLEMENTED}


def test_implemented_gate_set_matches_what_evaluate_computes():
    """Drift guard on `IMPLEMENTED_GATES`, driven from the constant itself.

    Not from `spec/slo.yaml warmup.gates`: the spec declares a SUBSET (W29
    dropped `p999_flat`), so reading the spec here would stop guarding the two
    from drifting apart the moment the spec stops naming a gate.
    """
    st = _flat_state(cfg=_cfg_declaring(*IMPLEMENTED_GATES))
    computed = {g.name for g in st.evaluate() if g.status != STATUS_NOT_IMPLEMENTED}
    assert computed == IMPLEMENTED_GATES | {"per_endpoint_min_calls"}
    # ENFORCED is the smaller set, and the difference is exactly the gate B2
    # retires as an enforcer while keeping as a statistic.
    enforced = {g.name for g in st.evaluate() if g.enforced}
    assert computed - enforced == {"p999_flat"}


# --------------------------------------------------------------------------- #
# flatness window: the old trailing-5 window could only fail on a strictly
# monotone 5-point run.
# --------------------------------------------------------------------------- #
def test_flatness_window_is_wider_than_five_samples():
    assert FLATNESS_TRAILING_SAMPLES >= 30


def test_flatness_catches_a_slow_rise_that_trailing_five_would_miss():
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, slot=SLOT_A, rate=500)
    # 30 samples rising with a noisy wobble: the last 5 are NOT monotone, so the
    # old window could never fail; over the full window the trend is significant.
    for i in range(30):
        s = _stats(calls={"GET /healthz": 10}, committed=1_000_000, gc_total=i)
        wobble = 0.8 if i % 2 else 0.0
        st.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=s),
                   p99_ms=5.0 + i * 0.5 - wobble, p999_ms=8.0)
    assert no_positive_trend(st.p99_series[-5:]).passed is True   # trailing 5: "flat"
    gates = {g.name: g.passed for g in st.evaluate()}
    assert gates["p99_flat"] is False                          # 30-sample window sees it


# --------------------------------------------------------------------------- #
# posture-specific gate sets: spec global <- spec posture <- cell (W22/B3)
# --------------------------------------------------------------------------- #
def test_each_gate_set_layer_merges_over_the_one_before_it():
    """No layer REPLACES the set, so none can silently stop enforcing a gate
    added globally after it was written -- and the cell layer wins over the
    spec's posture layer, because it is the more specific declaration.
    """
    cfg = load_slo().warmup.model_copy(update={
        "gates": {"p99_flat": True, "gc_steady": True, "pools_warm": True},
        "gates_by_posture": {"c": PostureWaiver(
            gates={"gc_steady": False, "pools_warm": False},
            rationale="SYNTHETIC: exercising the merge order",
        )},
    })

    resolved = gate_set_for_cell(cfg, cell_id="c", cell_gates={"pools_warm": True})
    assert resolved.enforced == {
        "p99_flat": True,       # from the global set, untouched by either layer
        "gc_steady": False,     # from the spec's posture layer
        "pools_warm": True,     # the cell layer beats the posture layer
    }
    # And each dropped gate is LABELLED by the layer that dropped it: the two
    # waivers have different provenance and only one is inside the config_hash.
    assert resolved.waived == {"gc_steady": STATUS_POSTURE_WAIVED}

    both = gate_set_for_cell(cfg, cell_id="c", cell_gates={"gc_steady": False})
    assert both.waived == {"gc_steady": STATUS_NOT_APPLICABLE,
                           "pools_warm": STATUS_POSTURE_WAIVED}


def test_a_gate_the_spec_never_declared_is_not_reported_as_waived():
    """`waived` is the set DROPPED FROM the declared set, not everything absent.

    A gate spec/slo.yaml declares false globally was never enforced by anyone,
    and reporting it as a posture waiver would manufacture a posture-specific
    story for a run-wide decision.
    """
    cfg = load_slo().warmup.model_copy(update={
        "gates": {"p99_flat": True, "gc_steady": False}, "gates_by_posture": {},
    })
    assert gate_set_for_cell(cfg, cell_id="c").waived == {}


def test_the_gate_set_id_tracks_the_set_and_not_the_cell_that_resolved_it():
    """The join key W23 records per probe: same set, same id; different set, not.

    Keyed on CONTENT, because `cell_id` already answers "which cell" and cannot
    answer "was this probe held to the same rules as that one" -- which is the
    question, once `phases.CELLS` and `spec/slo.yaml` have moved on.
    """
    cfg = load_slo().warmup.model_copy(update={
        "gates": {"p99_flat": True, "gc_steady": True, "pools_warm": True},
        "gates_by_posture": {"c": PostureWaiver(
            gates={"pools_warm": False},
            rationale="SYNTHETIC: exercising the gate_set_id key",
        )},
    })
    posture = gate_set_for_cell(cfg, cell_id="c")
    other_cell_same_set = gate_set_for_cell(
        cfg, cell_id="d", cell_gates={"pools_warm": False}
    )
    strict = gate_set_for_cell(cfg, cell_id="d")

    assert posture.gate_set_id != strict.gate_set_id
    # the WHY differs (posture_waived vs not_applicable), so the id must differ
    # too: two probes held to the same names under different provenance were not
    # held to the same declared set.
    assert posture.gate_set_id != other_cell_same_set.gate_set_id
    assert gate_set_for_cell(cfg, cell_id="c").gate_set_id == posture.gate_set_id


# --------------------------------------------------------------------------- #
# W23/B4: the verdict is a function of the gate list, not a field beside it
# --------------------------------------------------------------------------- #
def _result(**over) -> WarmupResult:
    base = dict(
        language=SLOT_A, warmup_rate=500, elapsed_seconds=1.0,
        thresholds=WarmupThresholds(
            hard_cap_seconds=300, flatness_trailing_samples=FLATNESS_TRAILING_SAMPLES,
            mann_kendall_alpha=MANN_KENDALL_ALPHA, per_endpoint_min_calls={"healthz": 1},
        ),
        exit_reason=EXIT_GATES_PASSED,
    )
    return WarmupResult(**{**base, **over})


def test_under_warmed_is_derived_from_the_gates_that_were_actually_scored():
    measured_fail = GateResult(name="p99_flat", passed=False)
    waived = GateResult(name="pools_warm", passed=False,
                        status=STATUS_POSTURE_WAIVED)
    unimplemented = GateResult(name="synthetic", passed=False,
                               status=STATUS_NOT_IMPLEMENTED)

    warm = _result(gates=[GateResult(name="p99_flat", passed=True), waived,
                          unimplemented])
    assert warm.gates_evaluated == ["p99_flat"]   # abstainers are not "evaluated"
    assert warm.gates_failed == []
    assert warm.under_warmed is False

    flagged = _result(gates=[measured_fail, waived, unimplemented])
    assert flagged.gates_failed == ["p99_flat"]
    assert flagged.under_warmed is True


def test_a_crashed_warmup_is_under_warmed_with_no_gate_to_blame():
    """The one non-gate cause: an attack that never wrote the series to score."""
    crashed = _result(gates=[GateResult(name="p99_flat", passed=True)],
                      exit_reason=f"{EXIT_ATTACK_FAILED_PREFIX}7")
    assert crashed.gates_failed == []
    assert crashed.under_warmed is True


def test_the_renamed_warmup_fields_cannot_be_set_under_their_old_names():
    """`rate` -> `warmup_rate`, and `under_warmed` stopped being a field.

    Without extra="forbid" both would be dropped in silence and the record would
    publish a derived verdict beside an ignored asserted one.
    """
    with pytest.raises(ValidationError):
        _result(rate=500)
    with pytest.raises(ValidationError):
        _result(under_warmed=False)


def test_an_unimplemented_gate_carries_no_evaluation_instant():
    """`at_seconds` means "when the verdict was taken", so a record with no
    verdict must not have one.

    `evaluate()` appends the not_implemented records and used to stamp the
    boundary time onto them along with everything else, publishing an evaluation
    instant for a gate nobody evaluated -- the field comment's own claim
    ("None means the gate was never evaluated") falsified by its only writer.
    """
    st = _state_declaring_an_unimplemented_gate()
    gates = {g.name: g for g in st.evaluate(at_seconds=42.5)}

    assert gates[SYNTHETIC_UNIMPLEMENTED].at_seconds is None
    scored = [g for g in gates.values() if g.enforced]
    assert scored and all(g.at_seconds == 42.5 for g in scored)


def test_a_gate_with_no_evidence_flags_the_probe_rather_than_abstaining(tmp_path):
    """`insufficient_data` ENFORCES. That is the point of the status.

    The other three non-measured statuses exist to say "this gate was not
    applied", and they carry `enforced: false` so they cannot flag. This one is
    the opposite finding -- the gate WAS applied and found nothing to apply
    itself to -- and if it abstained it would be the same silence with a new
    spelling: the probe would be recorded warm on gates that scored nothing.
    """
    res = _result(gates=[
        GateResult(name="p99_flat", passed=True),
        GateResult(name="gc_steady", passed=False,
                   status=warmup.STATUS_INSUFFICIENT_DATA,
                   detail={"usable_samples": {"heap_committed": 0}}),
        posture_waived_gate("pools_warm", "headline.mixed.gc-generous"),
    ])
    assert res.under_warmed is True
    assert res.gates_failed == ["gc_steady"]
    assert "gc_steady" in res.gates_evaluated
    assert "pools_warm" not in res.gates_evaluated

    path = tmp_path / f"{SLOT_A}.warmup.json"
    res.save(path)
    on_disk = {g["name"]: g for g in json.loads(path.read_text())["gates"]}
    assert on_disk["gc_steady"]["enforced"] is True
    assert on_disk["gc_steady"]["status"] == warmup.STATUS_INSUFFICIENT_DATA
    assert on_disk["pools_warm"]["enforced"] is False


def test_the_gc_cadence_skips_the_intervals_it_cannot_measure(tmp_path):
    """A cadence delta needs BOTH edges, and a null edge is not a zero.

    A runtime that stops publishing a collection count mid-window (or never
    publishes one, which is today's JVM) leaves gaps. Differencing across a gap
    charges a whole unmeasured stretch to one interval -- a large positive
    spike in exactly the series a flatness test reads -- and filling the null
    with 0 manufactures the flat cadence the gate is looking for.
    """
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, slot=SLOT_A, rate=500)
    for i, total in enumerate([0, 1, None, None, 40, 41, 42]):
        stats = _stats(calls={"GET /healthz": 10}, committed=1_000_000)
        stats.gc.collections = GcCollections(gen0=total, gen1=None, gen2=None)
        st.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=stats),
                   p99_ms=5.0, p999_ms=8.0)
    # One entry per snapshot, nulls kept in place so the list stays aligned.
    assert st.gc_total_series == [0, 1, None, None, 40, 41, 42]
    # Only the three adjacent measured pairs yield a cadence; the 1 -> 40 jump
    # across the gap is NOT one of them.
    assert st._gc_cadence() == [1, 1, 1]

    # ...and with EVERY entry null there is no cadence at all, which is
    # insufficient_data rather than a perfectly flat one.
    st2 = WarmupState(cfg=cfg, slot=SLOT_A, rate=500)
    for i in range(6):
        stats = _stats(calls={"GET /healthz": 10}, committed=1_000_000)
        stats.gc.collections = GcCollections(gen0=None, gen1=None, gen2=None)
        st2.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=stats),
                    p99_ms=5.0, p999_ms=8.0)
    assert st2._gc_cadence() == []
    gate = {g.name: g for g in st2.evaluate()}["gc_steady"]
    assert gate.status == warmup.STATUS_INSUFFICIENT_DATA
    assert gate.passed is False
    assert gate.enforced is True
    assert gate.detail["poller_snapshots"] == 6


def test_the_saved_gate_record_carries_the_enforced_flag(tmp_path):
    """`spec/slo.yaml warmup.gates` and the report's waiver bullet both tell the
    reader a waived gate reaches `<slot>.warmup.json` with `enforced: false`.

    Read the file back rather than the model: `enforced` was a plain `@property`
    for as long as those two sentences have existed, so pydantic never
    serialised it and the artifact carried only `status`. A reader scanning a
    `passed: false` could not tell a flagged probe from a waived one without
    knowing which status strings map to enforced.
    """
    res = _result(gates=[
        GateResult(name="p99_flat", passed=True),
        posture_waived_gate("gc_steady", "headline.mixed.gc-generous"),
        not_applicable_gate("pools_warm", "headline.mixed", "SYNTHETIC"),
    ])
    path = tmp_path / f"{SLOT_A}.warmup.json"
    res.save(path)

    on_disk = {g["name"]: g for g in json.loads(path.read_text())["gates"]}
    assert on_disk["p99_flat"]["enforced"] is True
    assert on_disk["gc_steady"]["enforced"] is False
    assert on_disk["pools_warm"]["enforced"] is False
    # and it is not merely present: it agrees with the status it is derived from
    assert on_disk["gc_steady"]["status"] == STATUS_POSTURE_WAIVED
    assert on_disk["pools_warm"]["status"] == STATUS_NOT_APPLICABLE


# --------------------------------------------------------------------------- #
# B2/W21 step 1: the flatness verdict as a statistic vector, observe-only.
# docs/p1-plan.md 3.2.2. No threshold is chosen here.
# --------------------------------------------------------------------------- #
def _fcfg(**over) -> warmup.FlatnessCfg:
    base = dict(quantile=0.99, samples_per_second=618.0,
                samples_per_second_source="measured_bucket_median")
    return warmup.FlatnessCfg(**{**base, **over})


def test_the_flatness_verdict_records_the_whole_statistic_vector():
    """Round 1 kept pass/fail and could therefore not recalibrate."""
    res = warmup.flatness_verdict([5.0 + 0.2 * i for i in range(30)], cfg=_fcfg())
    assert res.status == warmup.STATUS_MEASURED
    assert res.verdict is False                     # a 30-point monotone rise
    for name in ("median", "mk_z", "mk_p_increasing", "mk_tau",
                 "slope_per_second", "s_rel_pct_per_min",
                 "lag1_autocorrelation", "half_vs_half_median_ratio"):
        assert getattr(res, name) is not None, name
    assert res.n == 30
    assert res.slope_per_second == pytest.approx(0.2)
    # and every one of them survives into the per-probe record
    assert set(res.to_detail()) >= {
        "s_rel_pct_per_min", "lag1_autocorrelation", "half_vs_half_median_ratio",
        "mk_z", "samples_per_second", "samples_per_second_source",
    }


def test_s_rel_is_scale_free_where_an_absolute_slope_is_not():
    """The concrete fix for the language asymmetry (Go 67.3% vs .NET 51.8%).

    Two stacks drifting by the SAME PROPORTION of their own p99 must produce the
    same statistic. An absolute ms/min band cannot do that -- it trips first on
    whichever stack has the higher absolute p99, which is the shape of round 1's
    split -- and that is the whole reason B2 calibrates `s_rel` instead.
    """
    low = [10.0 + 0.1 * i for i in range(30)]
    high = [100.0 + 1.0 * i for i in range(30)]      # 10x the level AND the slope
    a = warmup.flatness_verdict(low, cfg=_fcfg())
    b = warmup.flatness_verdict(high, cfg=_fcfg())
    assert b.slope_per_second == pytest.approx(10.0 * a.slope_per_second)
    assert b.median == pytest.approx(10.0 * a.median)
    assert b.s_rel_pct_per_min == pytest.approx(a.s_rel_pct_per_min)


def test_the_recorded_statistics_separate_autocorrelation_from_a_level_shift():
    """The two things docs/p1-plan.md 3.2.1 shows flagging a PERFECTLY WARM
    SUT, told apart."""
    shift = warmup.flatness_verdict([5.0] * 15 + [6.0] * 15, cfg=_fcfg())
    assert shift.half_vs_half_median_ratio == pytest.approx(1.2)
    assert shift.lag1_autocorrelation > 0.8          # a step is highly autocorrelated
    # an EVEN number of pairs, so each half holds the same count of each value
    alternating = warmup.flatness_verdict([5.0, 6.0] * 16, cfg=_fcfg())
    assert alternating.half_vs_half_median_ratio == pytest.approx(1.0)
    assert alternating.lag1_autocorrelation < 0.0    # and this one is not
    assert warmup.lag1_autocorrelation([5.0] * 10) is None   # zero variance


def test_the_new_statistics_are_observe_only_and_decide_nothing():
    """The enforced verdict is still round 1's rule, exactly.

    A three-point series rising at 3000 %/min PASSES, because Mann-Kendall
    cannot reach significance on three points. Step 1 records the number that
    would catch it and enforces none of it; spec/slo.yaml declares no band for
    it.
    """
    res = warmup.flatness_verdict([1.0, 2.0, 3.0], cfg=_fcfg())
    assert res.status == warmup.STATUS_MEASURED
    assert res.s_rel_pct_per_min == pytest.approx(3000.0)
    assert res.verdict is True
    assert res.to_detail()["enforced_statistic"] == warmup.FLATNESS_ENFORCED_STATISTIC


def test_p999_flat_does_not_enforce_below_the_quantile_resolution_floor():
    """docs/p1-plan.md 5.2, as behaviour rather than as prose.

    `vegeta._percentile_ns` is nearest rank, so at these bucket sizes q=0.999 IS
    the second's maximum. The gate is retired as an ENFORCER and kept as a
    statistic: the vector is still computed, and it decides nothing.
    """
    cfg = _fcfg(quantile=0.999, samples_per_second=618.0,
                min_samples_per_second=warmup.MIN_SAMPLES_PER_SECOND_P999)
    res = warmup.flatness_verdict([5.0 + i for i in range(30)], cfg=cfg)
    assert res.status == warmup.STATUS_INSUFFICIENT_DATA
    assert res.reason == warmup.REASON_QUANTILE_UNRESOLVED
    assert res.enforce is False
    # kept as a statistic: every field is still there
    assert res.s_rel_pct_per_min is not None and res.mk_z is not None
    assert GateResult(name="p999_flat", passed=res.verdict, status=res.status,
                      enforce=res.enforce).enforced is False


def test_p999_flat_enforces_once_the_bucket_can_resolve_the_quantile():
    cfg = _fcfg(quantile=0.999, samples_per_second=2000.0,
                min_samples_per_second=warmup.MIN_SAMPLES_PER_SECOND_P999)
    res = warmup.flatness_verdict([5.0 + i for i in range(30)], cfg=cfg)
    assert res.status == warmup.STATUS_MEASURED
    assert res.enforce is True
    assert res.verdict is False
    assert GateResult(name="p999_flat", passed=res.verdict, status=res.status,
                      enforce=res.enforce).enforced is True


def test_the_resolution_floor_is_checked_before_the_empty_series():
    """Order matters, and getting it wrong flags 100% of probes.

    An unresolvable quantile produces no series to test, so if the too-few-
    samples cause won it would return an ENFORCING `insufficient_data` on every
    probe -- a gate that is inert by construction flagging the whole run.
    """
    cfg = _fcfg(quantile=0.999, samples_per_second=618.0,
                min_samples_per_second=warmup.MIN_SAMPLES_PER_SECOND_P999)
    res = warmup.flatness_verdict([], cfg=cfg)
    assert res.reason == warmup.REASON_QUANTILE_UNRESOLVED
    assert res.enforce is False
    # and the short-series cause still enforces where the quantile IS resolvable
    short = warmup.flatness_verdict([], cfg=_fcfg())
    assert short.reason == warmup.REASON_TOO_FEW_SAMPLES
    assert short.enforce is True
    assert GateResult(name="p99_flat", passed=short.verdict, status=short.status,
                      enforce=short.enforce).enforced is True


def test_samples_per_second_is_measured_from_the_buckets_not_the_offered_rate():
    """The floor decides whether a gate enforces, so its input is measured."""
    cfg = _cfg_declaring("p999_flat")
    st = WarmupState(cfg=cfg, slot=SLOT_A, rate=618)
    for i in range(6):
        s = _stats(calls={"GET /healthz": 10}, committed=1_000_000, gc_total=i)
        st.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=s),
                   p99_ms=5.0, p999_ms=8.0, samples=2000)
    flat = {g.name: g.detail["flatness"] for g in st.evaluate()
            if g.name in ("p99_flat", "p999_flat")}
    assert flat["p999_flat"]["samples_per_second"] == 2000.0
    assert flat["p999_flat"]["samples_per_second_source"] == "measured_bucket_median"
    # measured 2000/s clears the floor, so the gate ENFORCES here even though
    # the OFFERED rate would have been the same number
    assert flat["p999_flat"]["status"] == warmup.STATUS_MEASURED

    st2 = WarmupState(cfg=cfg, slot=SLOT_A, rate=618)
    for i in range(6):
        s = _stats(calls={"GET /healthz": 10}, committed=1_000_000, gc_total=i)
        st2.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=s),
                    p99_ms=5.0, p999_ms=8.0)
    flat2 = {g.name: g.detail["flatness"] for g in st2.evaluate()
             if g.name == "p999_flat"}["p999_flat"]
    assert flat2["samples_per_second"] == 618.0
    assert flat2["samples_per_second_source"] == "offered_rate"


def test_the_saved_probe_record_carries_the_flatness_vector(tmp_path):
    """It has to reach the ARTIFACT: a statistic held only in memory recalibrates
    nothing, which is precisely round 1's position."""
    cfg = _cfg_declaring("p999_flat")
    st = WarmupState(cfg=cfg, slot=SLOT_A, rate=618)
    for i in range(6):
        s = _stats(calls={"GET /healthz": 10}, committed=1_000_000, gc_total=i)
        st.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=s),
                   p99_ms=5.0 + 0.1 * i, p999_ms=8.0, samples=618)
    path = tmp_path / f"{SLOT_A}.warmup.json"
    _result(gates=st.evaluate()).save(path)

    on_disk = {g["name"]: g for g in json.loads(path.read_text())["gates"]}
    vec = on_disk["p99_flat"]["detail"]["flatness"]
    assert vec["s_rel_pct_per_min"] is not None
    assert vec["lag1_autocorrelation"] is not None
    assert vec["mk_z"] is not None
    assert on_disk["p999_flat"]["enforce"] is False
    assert on_disk["p99_flat"]["enforce"] is True


def test_the_thresholds_record_the_floor_each_flatness_gate_was_held_to():
    th = WarmupThresholds(
        hard_cap_seconds=300, flatness_trailing_samples=FLATNESS_TRAILING_SAMPLES,
        mann_kendall_alpha=MANN_KENDALL_ALPHA, per_endpoint_min_calls={"healthz": 1},
    )
    assert th.flatness_min_samples_per_second == {
        "p99_flat": 0.0, "p999_flat": warmup.MIN_SAMPLES_PER_SECOND_P999,
    }


def test_a_java_probe_scores_gc_steady_on_collector_cycles_instead_of_flagging():
    """W27's decision, end to end: the JVM was flagging 100% of its probes.

    `registry.JVM_GC.requires` declares `gc_steady` over `heap_committed` AND
    `collection_cadence`, and the JVM publishes gen0/gen1/gen2 as a present
    null -- so the cadence series was empty at every poll, `gc_steady` returned
    `insufficient_data`, and every Java probe would have been recorded
    under-warmed with no way to pass. The counts here are the real shape of that
    data: bean names and per-second rates from an 18 s run at 1 Hz against
    Temurin 25.0.4 under allocation load (`G1 Young Generation` climbing ~61/s,
    `G1 Concurrent GC` ~8/s, `G1 Old Generation` pinned at 0 through 1,100 young
    collections). The cadence must come out flat at ~61 and the gate must pass.
    """
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, slot="java", rate=500)
    for i in range(6):
        stats = _stats(calls={"GET /healthz": 999999}, committed=536_870_912)
        stats.gc.collections = GcCollections.model_validate({
            "gen0": None, "gen1": None, "gen2": None,
            "G1 Young Generation": 59 + 61 * i,
            "G1 Concurrent GC": 8 * i,
            "G1 Old Generation": 0,
        })
        st.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=stats),
                   p99_ms=5.0, p999_ms=8.0)

    # `G1 Concurrent GC` is excluded, so the series is the young+old total and
    # the cadence is the young rate -- not 69, which is what a sum over every
    # bean present would report.
    assert st.gc_total_series == [59, 120, 181, 242, 303, 364]
    assert st._gc_cadence() == [61, 61, 61, 61, 61]

    gate = {g.name: g for g in st.evaluate()}["gc_steady"]
    assert gate.status == warmup.STATUS_MEASURED
    assert gate.passed is True
    # The probe record says which basis the number came from: "the cadence was
    # flat" is a different finding over CLR generations than over JVM cycles,
    # and a reader must not have to infer it from the slot name.
    assert gate.detail["cadence_basis"] == ["collector_cycles"]

    # The .NET side is untouched by the same code path and says so.
    clr = WarmupState(cfg=cfg, slot="dotnet", rate=500)
    for i in range(6):
        clr.observe(
            Snapshot(captured_utc="t", monotonic_s=float(i),
                     stats=_stats(calls={"GET /healthz": 999999},
                                  committed=536_870_912, gc_total=i)),
            p99_ms=5.0, p999_ms=8.0,
        )
    clr_gate = {g.name: g for g in clr.evaluate()}["gc_steady"]
    assert clr_gate.detail["cadence_basis"] == ["generations"]
    assert clr_gate.passed is True
