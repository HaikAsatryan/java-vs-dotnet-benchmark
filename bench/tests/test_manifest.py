"""RunManifest full-disclosure schema round-trip."""

from __future__ import annotations

import json

import pytest

from ledgerbench import cgroups
from ledgerbench import compose as compose_mod
from ledgerbench import hostread, phases
from ledgerbench import manifest as mf
from ledgerbench import registry
from ledgerbench.manifest import (
    AchievedVsOffered,
    FaultCounts,
    Knobs,
    ResourceDelta,
    RunManifest,
    ThrottleCounters,
    new_manifest,
)
from ledgerbench.registry import DEFAULT_SLOT_SET, get_slot_set
from ledgerbench.util import paths

SLOT_A, SLOT_B = get_slot_set(DEFAULT_SLOT_SET).slots


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "results_dir", lambda: tmp_path / "results")
    return tmp_path / "results"


def test_empty_manifest_roundtrips():
    m = new_manifest("run-x", config_hash="deadbeef")
    s = m.model_dump_json()
    again = RunManifest.model_validate_json(s)
    assert again.identity.run_id == "run-x"
    assert again.identity.config_hash == "deadbeef"
    assert again.schema_version == mf.MANIFEST_SCHEMA_VERSION


def test_full_disclosure_sections_present():
    m = new_manifest("run-y")
    # every full-disclosure section is a field on the model
    for field in (
        "identity", "versions", "knobs_by_slot", "knobs_by_cell", "host_state",
        "topology", "postgres_config", "achieved_vs_offered", "throttle_counters",
        "fault_counts", "resource_deltas", "load_generator_overhead",
        "cache_stats", "errors",
    ):
        assert hasattr(m, field)


def test_knobs_echo_every_declared_lever():
    """Every env var the registry declares as a knob has a `Knobs` field to land in.

    This is the mechanised form of round 1's `db_conn_lifetime_seconds` defect:
    the lever was declared in `spec/db-access.md` and set by compose, had no
    entry in the scrub list, and the manifest field was therefore ALWAYS null
    while the spec claimed the dial existed.
    """
    declared: dict[str, str] = dict(registry.SHARED_KNOB_ENV)
    for spec in registry.LANGUAGES.values():
        declared.update(spec.knob_env)

    fields = set(Knobs.model_fields)
    scrub = set(compose_mod.KNOB_ENV_VARS)
    for var, attr in sorted(declared.items()):
        assert attr in fields, f"{var} -> Knobs.{attr} does not exist"
        assert var in scrub, f"{var} is a declared knob but nothing scrubs it"


def test_knobs_int_levers_round_trip_as_ints():
    k = Knobs(
        db_pool_max=24, db_pool_min=24, db_conn_lifetime_seconds=3600,
        redis_pool_max=16, cache_ttl_seconds=300, log_queue_len=8192,
        max_ram_percent=25, gc_engine="-XX:+UseG1GC", prepare_threshold=5,
        gc_dynamic_adaptation_mode="1", max_auto_prepare=20,
        auto_prepare_min_usages=5, data_layer="efcore", processor_count=6,
    )
    d = json.loads(k.model_dump_json())
    # D4's two halves are recorded side by side: both stacks promote on the 5th
    # execution, and the manifest can evidence it rather than the spec asserting it.
    assert d["prepare_threshold"] == 5 and d["auto_prepare_min_usages"] == 5
    assert d["max_auto_prepare"] == 20
    assert d["processor_count"] == 6
    assert d["data_layer"] == "efcore"


def test_knobs_has_no_second_field_for_the_processor_count_flag():
    """`-XX:ActiveProcessorCount` reads back as -1 {DEFAULT} on JDK 25 while
    `availableProcessors()` correctly returns the cpuset size (measured, see
    docs/research/2026-08-21-measured.md FINDING 8). Two fields would routinely
    disagree and a reader could not tell which one carried the assertion."""
    assert "active_processor_count" not in Knobs.model_fields
    assert "processor_count" in Knobs.model_fields


def test_populated_manifest_roundtrips_and_saves(isolated_results):
    m = new_manifest("run-z", config_hash="c0ffee")
    m.knobs_by_slot[SLOT_A] = Knobs(processor_count=6, max_ram_percent=25, db_pool_max=24)
    m.knobs_by_slot[SLOT_B] = Knobs(processor_count=6, max_auto_prepare=20, db_pool_max=24)
    m.achieved_vs_offered.append(
        AchievedVsOffered(probe_id=f"b0.{SLOT_A}", offered_rate=8000,
                          achieved_rate=7990, ratio=0.99875)
    )
    m.throttle_counters.append(ThrottleCounters(label=SLOT_A, nr_throttled=0))
    m.fault_counts.append(FaultCounts(label=SLOT_A, major_faults=0, minor_faults=123))
    m.resource_deltas.append(ResourceDelta(label=SLOT_A, cpu_usage_usec_delta=42_000))
    m.load_generator_overhead.append(
        mf.LoadGeneratorOverhead(label="vegeta", role=cgroups.ROLE_LOAD_GENERATOR,
                                 cpu_usage_usec_delta=7_000, windows=1)
    )
    m.versions.image_digests["postgres"] = "postgres:18.4-bookworm"  # a tag in place of a digest
    m.save()

    loaded = RunManifest.load("run-z")
    assert loaded is not None
    assert loaded.knobs_by_slot[SLOT_A].processor_count == 6
    assert loaded.achieved_vs_offered[0].ratio == pytest.approx(0.99875)
    assert loaded.throttle_counters[0].nr_throttled == 0
    # the apparatus is accounted, and says it is not per-request cost
    assert loaded.load_generator_overhead[0].label == "vegeta"
    assert loaded.load_generator_overhead[0].excluded_from_per_request_cost is True
    assert loaded.versions.image_digests["postgres"].startswith("postgres:18.4")


def test_extra_fields_rejected_on_strict_sections():
    # Identity forbids extras; a typo'd key must fail validation (full-disclosure
    # schema integrity).
    with pytest.raises(Exception):
        mf.Identity.model_validate({"run_id": "r", "bogus_key": 1})


# --------------------------------------------------------------------------- #
# per-cell knobs, explicit "not measured", working-set disclosure
# --------------------------------------------------------------------------- #
def test_per_cell_knobs_roundtrip_and_never_cross_copy(isolated_results):
    """Per cell, per slot, and no runtime's knob on the other runtime's row.

    Round 1's manifest carried one run-level pair of rows, so it could only ever
    describe the last cell executed; and a knob written onto the other side
    fabricates a value that SUT never saw.
    """
    m = new_manifest("run-cells")
    m.knobs_by_cell["headline.mixed.gc-default"] = mf.CellKnobs(
        cell_id="headline.mixed.gc-default",
        by_slot={SLOT_A: Knobs(processor_count=6), SLOT_B: Knobs(processor_count=6)},
    )
    m.knobs_by_cell["headline.mixed.gc-generous"] = mf.CellKnobs(
        cell_id="headline.mixed.gc-generous",
        extra_env={"DOTNET_GCDynamicAdaptationMode": "0"},
        by_slot={
            SLOT_A: Knobs(max_ram_percent=75, gc_engine="-XX:+UseZGC"),
            SLOT_B: Knobs(gc_dynamic_adaptation_mode="0"),
        },
    )
    m.save()
    loaded = RunManifest.load("run-cells")
    assert loaded.schema_version == mf.MANIFEST_SCHEMA_VERSION
    # the defaults cell is NOT overwritten by the generous cell's knobs
    assert loaded.knobs_by_cell["headline.mixed.gc-default"].by_slot[SLOT_A].gc_engine is None
    generous = loaded.knobs_by_cell["headline.mixed.gc-generous"].by_slot
    assert generous[SLOT_A].gc_engine == "-XX:+UseZGC"
    # no cross-copy in either direction
    assert generous[SLOT_B].gc_engine is None and generous[SLOT_B].max_ram_percent is None
    assert generous[SLOT_A].gc_dynamic_adaptation_mode is None


def test_unmeasured_sections_default_to_not_measured():
    m = new_manifest("run-nm")
    assert m.cache_stats.source == mf.NOT_MEASURED
    assert m.cache_stats.keyspace_hits is None
    # a fault row with no data carries None, never a 0 that reads as measured
    row = mf.FaultCounts(label="c")
    assert row.major_faults is None and row.source == mf.NOT_MEASURED
    # a throttle row with no data is null too: round 1's `int = 0` defaults made
    # an unsampled container read as a measured "never throttled"
    tc = mf.ThrottleCounters(label="c")
    assert tc.nr_throttled is None and tc.throttled_usec is None
    rd = mf.ResourceDelta(label="c")
    assert rd.cpu_usage_usec_delta is None and rd.mem_peak_bytes is None
    assert m.errors.source == mf.NOT_MEASURED


def test_capacity_gate_and_effective_workload_sections_roundtrip(isolated_results):
    m = new_manifest("run-gate")
    m.capacity_gate = mf.CapacityGateStatus(
        verdict="db-imposed-shared-knee", degraded=True,
        degraded_allowed_by="LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE",
        levers_fired=["write_share"], levers_not_applied={"cache_hit_rate": "no TTL"},
    )
    m.effective_workload = mf.EffectiveWorkload(
        spec_mix={"create_invoice": 15}, effective_mix={"create_invoice": 10},
        spec_pg_cores=3, effective_pg_cores=4,
    )
    m.save()
    loaded = RunManifest.load("run-gate")
    assert loaded.capacity_gate.degraded is True
    assert loaded.effective_workload.differs_from_spec is True


def test_a_spread_of_one_probe_is_not_a_spread_of_none():
    """`Spread.n` is what stops an empty section reading as a measured one.

    Round 1's manifest published `throttle_counters: []` and a reader could not
    tell "measured, nothing to report" from "never measured". The same trap sits
    on a per-probe spread: min/median/max default to null, and a null that means
    "no probe drew anything" is indistinguishable from one that means "this run
    recorded no draws" unless the sample count travels with them.
    """
    empty = mf.Spread.of([])
    assert (empty.n, empty.min, empty.median, empty.max) == (0, None, None, None)
    one = mf.Spread.of([7])
    assert (one.n, one.min, one.median, one.max) == (1, 7.0, 7.0, 7.0)
    odd = mf.Spread.of([5, 1, 9])
    assert (odd.n, odd.min, odd.median, odd.max) == (3, 1.0, 5.0, 9.0)
    # even n: the median is the mean of the two middle samples, so it is NOT
    # simply one of the inputs -- which is exactly the case a "pick the middle
    # element" implementation gets wrong and no odd-length test catches.
    even = mf.Spread.of([1, 2, 6, 10])
    assert (even.n, even.min, even.median, even.max) == (4, 1.0, 4.0, 10.0)


def test_unpopulated_sections_carry_an_explicit_not_measured_marker():
    """The ABSENCE claim, which must survive W18 populating these sections.

    A manifest whose read-back never ran (non-Linux, or a run that stopped
    before the host_readback phase) still has to say so: an empty object with no
    marker reads as "measured and empty".
    """
    m = new_manifest("run-unpop")
    for section in (m.host_state, m.topology, m.postgres_config):
        assert section.source == mf.NOT_MEASURED
        assert section.note.startswith("not populated")
    assert m.host_state.readings == []
    assert m.topology.containers == {}
    assert m.postgres_config.values == {}


def test_host_state_is_measured_after_a_read_back():
    """`host_state` is `measured` after a read-back (R1-B2).

    Round 1's `host_state` said `source: not_measured` on every published run,
    so the governor, boost state, C-states and THP mode the methodology claimed
    could not be evidenced from the artifact at all. The read-back fills it, and
    the Tier-1/SETTING invariant travels with the rows.
    """
    m = new_manifest("run-measured")
    readings = [
        mf.HostReading(attribute=hostread.ATTR_GOVERNOR, kind=hostread.KIND_SETTING,
                       tier=hostread.TIER_1, value=["performance"],
                       expected="performance", matches=True),
        mf.HostReading(attribute=hostread.ATTR_IRQ_COUNTS, kind=hostread.KIND_OUTCOME,
                       tier=hostread.TIER_2, value={"0": 12}),
    ]
    phases._accumulate_host_state(m, readings, [], ["irq: recorded"], [])
    assert m.host_state.source == "measured"
    assert "not populated" not in m.host_state.note
    assert m.host_state.readings and m.host_state.reads == 1
    assert m.host_state.first_read_utc == m.host_state.last_read_utc
    for r in m.host_state.readings:
        if r.tier == hostread.TIER_1:
            assert r.kind == hostread.KIND_SETTING


def test_a_resume_records_a_changed_value_and_never_a_repeat():
    """`identity.git_sha`'s provenance rule, applied to a SET of values.

    A reboot mid-run resets governor and boost; host_setup re-applies them and
    the read-back runs again. The FIRST read stays whole, an identical re-read
    adds nothing, and a value that actually moved is recorded with both ends.

    OUTCOMES ARE NOT DIFFED, and the fixture below carries one that moves on
    every read for exactly that reason. `scaling_cur_freq` differs between any
    two samples and `/proc/interrupts` counts only climb, so diffing them put
    two fabricated rows at the top of `changed_during_run` on 100% of resumes
    and the real governor flip arrived as row 3 of a list whose rows 1 and 2 are
    noise -- defeating the one thing this list exists to make visible.
    """
    m = new_manifest("run-resume")

    def reading(value):
        return mf.HostReading(
            attribute=hostread.ATTR_GOVERNOR, kind=hostread.KIND_SETTING,
            tier=hostread.TIER_1, value=value, expected="performance",
            matches=(value == ["performance"]),
        )

    def outcome(khz):
        return mf.HostReading(
            attribute=hostread.ATTR_CPU_FREQ, kind=hostread.KIND_OUTCOME,
            tier=hostread.TIER_2, value={"scaling_cur_freq_khz": {"cpu0": khz}},
        )

    phases._accumulate_host_state(m, [reading(["performance"]), outcome("3000000")],
                                  [], [], [])
    phases._accumulate_host_state(m, [reading(["performance"]), outcome("3812000")],
                                  [], [], [])
    phases._accumulate_host_state(m, [reading(["schedutil"]), outcome("2200000")],
                                  ["governor: ..."], [], [])

    assert [r.value for r in m.host_state.first_readings][0] == ["performance"]
    assert [r.value for r in m.host_state.readings][0] == ["schedutil"]
    assert m.host_state.reads == 3
    assert len(m.host_state.changed_during_run) == 1
    change = m.host_state.changed_during_run[0]
    assert change["attribute"] == hostread.ATTR_GOVERNOR
    assert change["from"] == ["performance"] and change["to"] == ["schedutil"]
    assert m.host_state.tier1_failures == ["governor: ..."]


def test_every_container_topology_field_is_written_by_the_code_that_fills_it():
    """No declared-but-never-written field. That is round 1's documented failure.

    `ContainerTopology.cpuset` / `.mem_limit` / `.ccd` and `Topology.ccd_mapping`
    were declared beside the fields W18 made real and constructed by nothing, so
    every published manifest carried `"cpuset": null, "ccd": null` under a
    section stamped `source: "measured"`. They are deleted; this keeps them
    deleted, and refuses the next one.
    """
    m = new_manifest("topo-fields")
    per_label = {"sut": {"role": "sut", "cpuset": "0-5", "mem_limit_bytes": 12,
                         "mem_swap_max": "0", "swap_current_max": 0,
                         "swap_peak_max": 0, "windows": 2}}
    phases._stamp_topology(m, per_label, env={"SUT_CPUS": "0-5"})
    row = m.topology.containers["sut"]
    unwritten = [f for f in type(row).model_fields
                 if getattr(row, f) == type(row).model_fields[f].default]
    assert unwritten == [], (
        f"{unwritten} are declared on ContainerTopology and left at their default "
        "by the only code that constructs it"
    )
    # Topology's own rows: `containers` and the read-back's L3 evidence, nothing
    # that no phase populates.
    assert "ccd_mapping" not in mf.Topology.model_fields


def test_a_tier_1_that_could_not_be_evaluated_is_not_an_empty_failure_list():
    """`tier1_failures: []` must mean "checked and matched", nothing else.

    A box with no boost knob at all yields a Tier-1 SETTING with `matches:
    None`. Recording that among the Tier-2 notes let a manifest carry
    `tier1_failures: []` and `source: measured` while the one attribute
    evidencing boost was never checked.
    """
    m = new_manifest("run-uneval")
    phases._accumulate_host_state(m, [], [], ["pgdata: recorded"],
                                  ["boost: not evaluable on this box (no knob)"])
    assert m.host_state.tier1_failures == []
    assert m.host_state.tier1_not_evaluable == [
        "boost: not evaluable on this box (no knob)"]
    assert m.host_state.tier2_notes == ["pgdata: recorded"]


def test_aggregate_provenance_marks_every_list_section():
    """An empty LIST has no row to carry a `source`; the section does."""
    m = new_manifest("run-prov")
    prov = m.aggregate_provenance
    assert prov.throttle_counters == mf.NOT_MEASURED
    assert prov.resource_deltas == mf.NOT_MEASURED
    assert prov.fault_counts == mf.NOT_MEASURED
    assert prov.achieved_vs_offered == mf.NOT_MEASURED
    assert prov.load_generator_overhead == mf.NOT_MEASURED
    # and the row type that had no marker at all now has one
    assert ThrottleCounters(label="c").source == mf.NOT_MEASURED


def test_throttle_counters_document_measured_window_semantics():
    """Throttle counters are per-measured-window deltas; `memory.peak` is not.

    Round 1 read cpu.stat's cumulative counters at window END, so a non-zero
    value meant "throttling happened somewhere in that container's life". Both
    edges are read now, so the number is the measured window's own, and the
    model docstrings must say so.
    """
    doc = " ".join(ThrottleCounters.__doc__.split())
    assert "per-MEASURED-WINDOW delta" in doc
    assert "throttled inside a measured window" in doc
    # memory.peak did NOT become a delta and must still say so.
    rd = " ".join(ResourceDelta.__doc__.split())
    assert "LIFETIME" in rd and "high-water mark" in rd


def test_a_previous_manifest_schema_version_is_refused_by_name(isolated_results):
    """A v3 manifest cannot be coerced into the v4 model, so say which is which.

    `RunManifest` is `extra="forbid"` and its only caller writes
    `load(run_id) or new_manifest(...)`, where an `or` cannot rescue a raise --
    so an unguarded bump crashes every resume with a field-level error naming a
    missing `label` instead of naming the real problem. The ledger has had this
    guard since W5; the manifest never got the matching test.
    """
    m = new_manifest("run-old")
    m.save()
    path = m.path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["schema_version"] = mf.MANIFEST_SCHEMA_VERSION - 1
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(mf.ManifestSchemaError) as exc:
        RunManifest.load("run-old")
    msg = str(exc.value)
    assert str(mf.MANIFEST_SCHEMA_VERSION - 1) in msg
    assert str(mf.MANIFEST_SCHEMA_VERSION) in msg
    # the file is NOT rewritten: a refused run must stay readable as what it is
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == (
        mf.MANIFEST_SCHEMA_VERSION - 1
    )


def test_the_current_schema_version_is_documented(isolated_results):
    """A bump must bring its paragraph. The W7 precedent, mechanised.

    The version constant on its own tells a reader that something changed, not
    what. Every earlier bump appended a dated paragraph naming the change and
    kept the older ones verbatim; without this test the next one is free to be
    a silent integer.
    """
    doc = mf.__doc__
    assert f"schema_version {mf.MANIFEST_SCHEMA_VERSION}" in doc
    # and the previous versions' paragraphs are kept, never rewritten
    for older in range(2, mf.MANIFEST_SCHEMA_VERSION):
        assert f"schema_version {older}" in doc


def test_the_cgroup_row_shape_is_frozen_per_schema_version():
    """Changing a cgroup row's fields MUST move MANIFEST_SCHEMA_VERSION.

    `RunManifest` is `extra="forbid"`, so a field added or renamed here makes
    every existing manifest unloadable -- and `load()` refuses by version, which
    only helps if the version actually moved. Nothing can detect a forgotten
    bump from the constant alone, so the shape is frozen here instead: editing
    this list is the moment to ask whether the version moved with it.

    Frozen at schema_version 8 (P1/W20 correction). The four cgroup row shapes below did
    NOT change at 5 (host_state / topology / postgres_config), at 7
    (`WorkingSet`) nor at 8 (`WorkingSet` again -- `prewarm_containment_per_probe`
    added, `draw_seconds_per_probe` re-scoped to include the ship); at 6 they
    lost `basis` for the same reason `Versions` lost five fields -- no writer
    ever set it, so every published row carried the one default and the
    alternative value it existed to express was unreachable. Re-stating the
    shapes here is what keeps the guard from silently going stale across a bump
    it was not about, and neither `basis` nor the five `Versions` fields may
    come back without a writer that fills them per row.
    """
    assert mf.MANIFEST_SCHEMA_VERSION == 8, (
        "the frozen row shapes below belong to schema_version 8; bump them "
        "together or the guard is decorative"
    )
    assert set(mf.Versions.model_fields) == {
        "source", "note", "image_digests", "runtime_by_slot",
        "service_version_by_slot",
    }
    assert set(ThrottleCounters.model_fields) == {
        "label", "role", "nr_throttled", "throttled_usec", "windows", "source",
    }
    assert set(FaultCounts.model_fields) == {
        "label", "role", "major_faults", "minor_faults", "windows", "source",
    }
    assert set(ResourceDelta.model_fields) == {
        "label", "role", "persistent", "cpu_usage_usec_delta",
        "mem_anon_p99_bytes", "mem_current_p99_bytes", "mem_peak_start_bytes",
        "mem_peak_bytes", "windows", "source",
    }
    assert set(mf.LoadGeneratorOverhead.model_fields) == {
        "label", "role", "cpu_usage_usec_delta", "windows",
        "excluded_from_per_request_cost", "source",
    }
    # `container` is gone from all four: a systemd unit is not a container, and
    # it was the field every aggregation keyed by. `basis` is gone from all four
    # too, and the module-level constants with it: a field written by nothing
    # publishes one string forever, and on ResourceDelta that string was false
    # for the row's own `mem_peak_bytes`.
    for model in (ThrottleCounters, FaultCounts, ResourceDelta,
                  mf.LoadGeneratorOverhead):
        assert "container" not in model.model_fields
        assert "basis" not in model.model_fields
    assert not hasattr(mf, "BASIS_WINDOW_DELTA")
    assert not hasattr(mf, "BASIS_LIFETIME_AT_WINDOW_END")
    # v7: `WorkingSet` stops describing a FILE. Round 1 replayed one targets
    # file per slot, so the section counted that file's ids and disclosed the
    # seconds a full cycle took; W20 draws a stream per attack, so there is no
    # file, no cycle, and `replay_period_seconds` would be a fabricated
    # disclosure. Named here, not merely absent, so restoring any of the five
    # deleted fields has to argue with this list.
    # v8: `prewarm_containment_per_probe` added. It is the one field standing
    # where a CLAIM used to -- the section asserted that separate per-attack
    # streams meant nothing pre-warmed a measured window, which is false for the
    # Zipf head both streams draw by construction -- so deleting it would return
    # the section to asserting an unmeasured zero.
    assert set(mf.WorkingSet.model_fields) == {
        "draw_mode", "draw_seed_formula",
        "requests_drawn_per_probe", "distinct_invoice_ids_per_probe",
        "distinct_customer_ids_per_probe", "draw_seconds_per_probe",
        "prewarm_containment_per_probe",
        "body_pool_create_bodies", "body_pool_quote_bodies",
        "body_pool_distinct_customer_ids", "seeded_invoices", "seeded_customers",
        "cache_ttl_seconds", "cache_ttl_source", "measured_window_seconds", "note",
    }
    for gone in ("replay_period_seconds", "targets_file", "target_count",
                 "distinct_invoice_ids", "distinct_customer_ids",
                 "probes_recorded"):
        assert gone not in mf.WorkingSet.model_fields
    assert not hasattr(mf, "working_set_from_targets")


def test_capacity_gate_status_keeps_measured_and_projected_apart():
    """A linearly-scaled projection may not sit in a field read as measured."""
    st = mf.CapacityGateStatus(
        verdict="pass", pg_knee_rps=500.0057, pg_knee_measured=True,
        pg_knee_measured_at_cores=3, pg_capacity_projected_rps=666.674,
        pg_capacity_projection_basis="measured 500.0057 rps at 3 cores x 4/3",
    )
    assert st.pg_knee_rps != st.pg_capacity_projected_rps
    assert "MEASURED" in mf.CapacityGateStatus.__doc__


def test_degraded_docstring_matches_the_code_that_sets_it():
    """The flag is set on ANY non-pass verdict, aborted runs included."""
    doc = mf.CapacityGateStatus.__doc__
    assert "did NOT return `pass`" in doc
    assert "degraded_allowed_by" in doc


def test_identity_records_a_resume_from_different_code(isolated_results):
    m = new_manifest("run-sha")
    m.identity.git_sha = "aaa"
    m.identity.git_sha_changed_during_run.append(
        {"at": "2026-08-13T00:00:00.000000Z", "git_sha": "bbb", "git_dirty": False}
    )
    m.save()
    loaded = RunManifest.load("run-sha")
    assert loaded.identity.git_sha == "aaa"          # the sha the run STARTED at
    assert loaded.identity.git_sha_changed_during_run[0]["git_sha"] == "bbb"
