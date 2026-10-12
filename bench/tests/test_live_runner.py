"""LiveProbeRunner logic that is testable without a docker daemon.

The attack/compose paths belong to the measured run; what is unit-tested here is
the state the runner carries INTO those calls: the realized capacity-gate levers
(compose env on every invocation, effective workload for targets generation) and
the window-boundary sampling helpers whose null-vs-zero distinction the manifest
depends on.
"""

from __future__ import annotations

import pytest

from ledgerbench import cgroups, compose, registry
from ledgerbench.config import PostureWaiver, load_slo, load_workload
from ledgerbench.live_runner import (
    LiveProbeRunner,
    _cache_delta,
    _sample_targets,
    oom_labels,
    probe_draw_cost,
    probe_targets_name,
    shared_summary_name,
    warmup_segment_draw,
)
from ledgerbench.phases import Cell, PhaseError, ScaleProfile
from ledgerbench import vegeta
from ledgerbench.vegeta import DrawnTargets


@pytest.fixture
def runner():
    slo = load_slo()
    return LiveProbeRunner(
        run_id="lr-1", profile=ScaleProfile.resolve("smoke", slo),
        workload=load_workload(), slo=slo, bench_overlay=True,
    )


def test_lever_env_reaches_every_compose_invocation(runner):
    assert runner._env().extra_env == {}
    runner.apply_effective(env_overrides={"PG_CPUS": "8-11", "VEGETA_CPUS": "12-13"})
    for profiles in ([], [compose.PROFILE_LOAD], [registry.get_slot("dotnet").profile]):
        env = runner._env(profiles=profiles)
        assert env.extra_env["PG_CPUS"] == "8-11"
        assert env.extra_env["VEGETA_CPUS"] == "12-13"


def test_apply_effective_swaps_the_workload_and_forces_regeneration(runner):
    from ledgerbench import capacity_gate as capgate

    params = capgate.ResolvedWorkloadParams.from_workload(runner.workload)
    params.write_share = 0.10
    effective = capgate.apply_params_to_workload(runner.workload, params)
    runner._pool = object()
    runner._pool_shipped = True
    runner.apply_effective(workload=effective)
    assert runner.workload.mix.create_invoice == 10
    # the pool is dropped so the stale (pre-lever) bodies are re-drawn: the mix
    # decides how many create vs quote bodies the pool holds, and a lever that
    # moves write_share moves both.
    assert runner._pool is None and runner._pool_shipped is False


def test_the_sampled_set_covers_db_cache_load_generator_and_both_daemons():
    """The probe samples six cgroups, and each declares what it IS.

    The DB row is what separates "Java costs more CPU" from "Hibernate's SQL
    costs PostgreSQL more" -- round 1 sampled the SUT alone and could not tell
    those apart. The load generator and the container runtime are sampled too,
    and their roles are what keeps them out of every per-request figure.
    """
    targets = _sample_targets(registry.get_slot("dotnet"))
    by_role = {}
    for t in targets:
        by_role.setdefault(t.role, []).append(t.label)

    assert by_role[cgroups.ROLE_SUT] == ["dotnet"]
    assert by_role[cgroups.ROLE_DATABASE] == [compose.SVC_POSTGRES]
    assert by_role[cgroups.ROLE_CACHE] == [compose.SVC_REDIS]
    assert by_role[cgroups.ROLE_LOAD_GENERATOR] == [compose.SVC_VEGETA]
    # BOTH daemon units: measured on this box, the containerd-shim (which
    # relays the SUT's json-file stdout) is charged to containerd.service, so
    # docker.service alone under-counts the runtime on the logging path.
    assert sorted(by_role[cgroups.ROLE_CONTAINER_RUNTIME]) == ["containerd", "dockerd"]

    # The SUT and vegeta are the non-persistent targets: run_probe's
    # `up --force-recreate` names BOTH, so their cgroups are new each probe.
    # This assertion used to claim the SUT alone, matching a declaration that
    # overstated what vegeta's memory.peak covered by a factor of the probe
    # count. Everything else lives for the whole run, which is precisely why
    # every cumulative counter is delta'd.
    assert sorted(t.label for t in targets if not t.persistent) == [
        "dotnet", compose.SVC_VEGETA,
    ]
    assert len({t.label for t in targets}) == len(targets)


def test_the_sut_container_name_is_never_spelled_by_hand():
    """`f"ledgerline-{svc}-1"` resolves NO cgroup under a non-default `-p`.

    compose.container_name exists and says so; a hand-built name makes the
    resource numbers -- the headline -- silently empty rather than wrong.
    """
    slot = registry.get_slot("dotnet")
    targets = {t.label: t for t in _sample_targets(slot)}
    assert slot.container_name() in targets[slot.slot_id].source
    assert compose.container_name(compose.SVC_POSTGRES) in targets[
        compose.SVC_POSTGRES].source


def _win(label, *, oom_delta, enabled=True):
    return cgroups.WindowResult(label=label, enabled=enabled,
                                oom_kill_delta=oom_delta)


def test_oom_abort_uses_the_window_delta_not_the_lifetime_counter():
    """A probe aborts on an OOM that happened IN ITS WINDOW, and only then.

    `memory.events oom_kill` is cumulative over a cgroup's lifetime, and
    postgres/redis/vegeta/the daemons outlive every probe. Testing the absolute
    value would latch the abort: probe 1 records the kill, and every later probe
    aborts on that history. Because a failed probe raises PhaseError, `--resume`
    re-runs it against the same latched counter -- an unrecoverable loop, on a
    run that is otherwise healthy.
    """
    # a cgroup that was ALREADY at oom_kill=5 before this window: delta 0
    assert oom_labels({"pg": _win("pg", oom_delta=0)}) == []
    # ...and one that took a kill during it
    assert oom_labels({"pg": _win("pg", oom_delta=1)}) == ["pg"]
    # a disabled target cannot fail a probe on a counter it never read
    assert oom_labels({"dockerd": _win("dockerd", oom_delta=None, enabled=False)}) == []
    # null is not a kill
    assert oom_labels({"pg": _win("pg", oom_delta=None)}) == []
    # every sampled cgroup is checked, not just the SUT, and the abort names them
    many = {
        "dotnet": _win("dotnet", oom_delta=0),
        "postgres": _win("postgres", oom_delta=2),
        "redis": _win("redis", oom_delta=1),
    }
    assert sorted(oom_labels(many)) == ["postgres", "redis"]


class _FakeSampler:
    """A MultiSampler stand-in: the placement verdict and whether it was stopped."""

    def __init__(self, bad, limit_bad=()):
        self.bad = bad
        self.limit_bad = list(limit_bad)
        self.stopped = False

    def cpuset_mismatches(self, expected):
        self.expected = expected
        return list(self.bad)

    def limit_mismatches(self, expected_bytes):
        self.expected_bytes = expected_bytes
        return list(self.limit_bad)

    def stop(self):
        self.stopped = True
        return {}

    def swap_allowance_misconfigured(self):
        return []

    def reread_placement(self):
        pass


def test_a_wrong_container_cpuset_raises_on_the_first_window_and_only_once(
    runner, monkeypatch,
):
    """A wrong cpuset RAISES; it is not a failed probe.

    A failed probe is retried, and a container on the wrong cores is not
    something a retry fixes -- every CPU-ms- and memory-per-request figure in
    the cell would be measured against a core budget the container does not
    have. Checked once per process, on the first measured window, so a 282-probe
    cell costs one probe to find it instead of fifteen hours.

    The check must also STOP the sampler thread and the SUT container before it
    raises, or an abort leaves both running for the operator to find.
    """
    stopped: list[str] = []
    monkeypatch.setattr(
        "ledgerbench.live_runner.compose.compose",
        lambda env, argv, **kw: stopped.append(argv[-1]),
    )
    sampler = _FakeSampler(["sut (sut): cpuset.cpus.effective=0-15, "
                            "compose asked for 0-5"])
    with pytest.raises(PhaseError, match="0-15"):
        runner._assert_container_placement(sampler, runner._env(), "sut-dotnet")
    assert sampler.stopped and stopped == ["sut-dotnet"]

    # checked ONCE: the second window does not re-shell or re-raise
    runner._assert_container_placement(_FakeSampler(["still wrong"]), runner._env(), "x")


def test_run_probe_actually_calls_the_placement_check_after_the_sampler_starts():
    """The WIRING, because `run_probe` itself is a docker-only path.

    A check `run_probe` never calls can be deleted with no test failing, so the
    call site itself is asserted. `sampler.start()` is what reads
    `cpuset.cpus.effective`, so the call has to sit after it and before the
    attack.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(LiveProbeRunner.run_probe))
    tree = ast.parse(src)
    started = checked = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "start" and getattr(node.func.value, "id", "") == "sampler":
            started = node.lineno
        if node.func.attr == "_assert_container_placement":
            checked = node.lineno
    assert started is not None
    assert checked is not None, "run_probe never checks container placement"
    assert checked > started, "the cpusets are only read by sampler.start()"


def test_the_placement_check_compares_against_the_realized_lever_cpusets(
    runner, monkeypatch,
):
    """The pg_cores lever exports PG_CPUS onto the RUNNER, not into os.environ.

    Reading the ambient environment alone would compare PostgreSQL against a
    topology the run stopped using and abort a healthy run on the gate's own
    lever.
    """
    monkeypatch.delenv("PG_CPUS", raising=False)
    runner.apply_effective(env_overrides={"PG_CPUS": "8-11"})
    sampler = _FakeSampler([])
    runner._assert_container_placement(sampler, runner._env(), "sut-dotnet")
    assert sampler.expected[cgroups.ROLE_DATABASE] == "8-11"


def test_a_wrong_memory_ceiling_raises_on_the_first_window_too(runner, monkeypatch):
    """The OTHER half of the container budget, gated where the cpuset is gated.

    `memory.max` and `memory.swap.max` were recorded into the manifest and read
    by nothing: no gate compared them to what infra/compose.bench.yaml asked
    for, and the report asserted the memswap rule as prose one field away from
    the read-back that could prove it. A container that came up unlimited is not
    a cosmetic divergence -- both runtimes size their heap from the cgroup limit
    -- so it aborts on the first window like a wrong cpuset does.
    """
    stopped: list[str] = []
    monkeypatch.setattr(
        "ledgerbench.live_runner.compose.compose",
        lambda env, argv, **kw: stopped.append(argv[-1]),
    )
    sampler = _FakeSampler([], ["sut (sut): memory.max=max, compose asked for 12g"])
    with pytest.raises(PhaseError, match="memory.max=max"):
        runner._assert_container_placement(sampler, runner._env(), "sut-dotnet")
    assert sampler.stopped and stopped == ["sut-dotnet"]
    # and it is compared against the overlay's ceilings, in bytes
    assert sampler.expected_bytes[cgroups.ROLE_SUT] == 12 * 1024 ** 3


def test_shared_summary_name_is_slot_keyed_not_block_keyed():
    """The block directory is shared by BOTH slots of a block.

    `phases._artifact_dir` takes no slot component and a block's two probes run
    sequentially, so one `cpu_mem_postgres.summary.json` per block would be
    written by the first probe and overwritten by the second -- publishing one
    slot's DB cost as both slots'. Two identical DB columns read as a finding.
    """
    a = shared_summary_name("dotnet-efcore", compose.SVC_POSTGRES)
    b = shared_summary_name("dotnet-ado", compose.SVC_POSTGRES)
    assert a != b
    assert "dotnet-efcore" in a and "dotnet-ado" in b
    # and neither can shadow a SUT summary
    assert a not in {f"cpu_mem_{s}.summary.json" for s in registry.SLOTS}


def test_cache_delta_reports_measured_hit_rate_or_says_not_measured():
    out = _cache_delta(
        {"keyspace_hits": 100, "keyspace_misses": 100},
        {"keyspace_hits": 900, "keyspace_misses": 300},
    )
    assert out["keyspace_hits"] == 800 and out["keyspace_misses"] == 200
    assert out["hit_rate"] == pytest.approx(0.8)
    assert out["source"] != "not_measured"

    missing = _cache_delta({"keyspace_hits": None, "keyspace_misses": None}, {})
    assert missing["hit_rate"] is None
    assert missing["source"] == "not_measured"

    idle = _cache_delta(
        {"keyspace_hits": 1, "keyspace_misses": 1}, {"keyspace_hits": 1, "keyspace_misses": 1}
    )
    assert idle["hit_rate"] is None          # 0 ops != a 0% hit rate
    assert idle["keyspace_hits"] == 0


def test_measure_pg_create_knee_returns_a_measurement_not_a_float(runner, monkeypatch, tmp_path):
    """A ladder that cannot even start reports NOT MEASURED, never a fallback rate."""
    monkeypatch.setattr(
        compose, "compose",
        lambda *a, **k: type("R", (), {"ok": False, "stdout": "", "stderr": "no daemon",
                                       "returncode": 1})(),
    )
    knee = runner.measure_pg_create_knee(out_dir=tmp_path, rungs=3, window_s=1)
    assert knee.measured is False
    assert knee.knee_rps is None
    assert "no usable PG knee measured" in knee.detail


def test_every_knee_path_persists_pg_knee_json(runner, monkeypatch, tmp_path):
    """Even the "compose up failed" exit leaves the artifact behind.

    The early returns used to skip the write entirely, so the run had no record
    that the create-only ladder was attempted at all.
    """
    import json

    monkeypatch.setattr(
        compose, "compose",
        lambda *a, **k: type("R", (), {"ok": False, "stdout": "", "stderr": "no daemon",
                                       "returncode": 1})(),
    )
    out_dir = tmp_path / "capacity-gate"
    knee = runner.measure_pg_create_knee(out_dir=out_dir, rungs=3, window_s=1)
    doc = json.loads((out_dir / "pg_knee.json").read_text(encoding="utf-8"))
    assert doc["knee_rps"] is None
    assert doc["rungs"] == []
    assert doc["usable_rungs"] == 0
    assert doc["measured_at_cores"] == 3
    assert doc["detail"] == knee.detail
    assert "failed to come up" in doc["detail"]


# --------------------------------------------------------------------------- #
# Segmented warmup with early exit (spec/slo.yaml warmup.hard_cap_seconds)
#
# The cap is a CEILING, not a duration. These tests are what make that claim
# true: before segmentation every probe burned the full cap unconditionally, so
# raising it 180 -> 300 for the JVM (PROGRESS O4) would have cost ~9.4 h/cell.
# --------------------------------------------------------------------------- #
from ledgerbench import live_runner as lr  # noqa: E402
from ledgerbench import warmup as lr_warmup  # noqa: E402
from ledgerbench.runtimestats import PollWindow, RuntimeStats, Snapshot  # noqa: E402


def _snapshot(*, pool_total: int, pool_max: int = 24) -> Snapshot:
    stats = RuntimeStats.model_validate({
        "uptime_seconds": 10.0,
        "gc": {"collections": {"gen0": 7, "gen1": 2, "gen2": 1},
               "heap_committed_bytes": 512_000_000, "heap_in_use_bytes": 200_000_000},
        "db_pool": {"max": pool_max, "total": pool_total, "idle": pool_total, "in_use": 0},
        "redis_pool": {"max": 16, "total": 1, "idle": 1, "in_use": 0},
        "endpoint_calls": {k: 10**7 for k in [
            "GET /invoices/{id}", "GET /customers/{id}/invoices", "POST /pricing/quote",
            "POST /invoices", "POST /invoices/{id}/pdf-stub", "GET /healthz"]},
    })
    return Snapshot(captured_utc="2026-08-21T00:00:00.000000Z", monotonic_s=0.0, stats=stats)


class _FakePoller:
    """Serves identical snapshots, so every flatness series is dead flat."""

    def __init__(self, snapshot, *, phase="", slot=""):
        self._snap = snapshot
        self._n = 0
        self.phase = phase
        self.slot = slot

    def start(self):
        pass

    def stop(self):
        pass

    def poll_once(self, client=None):
        return self._snap

    def all(self):
        self._n += 40          # a segment's worth of 1 Hz samples
        return [self._snap] * self._n

    def latest(self):
        return self._snap

    def window(self):
        return PollWindow(phase=self.phase, slot=self.slot,
                          snapshots=[self._snap, self._snap], n_polls=2)


_CELL_ID = "headline.mixed.gc-default"
_CELL = Cell(cell_id=_CELL_ID)


def _wire(monkeypatch, runner, *, pool_total, attack_ok=True):
    snap = _snapshot(pool_total=pool_total)
    monkeypatch.setattr(
        lr, "StatsPoller",
        lambda *a, **k: _FakePoller(snap, phase=k.get("phase", ""), slot=k.get("slot", "")),
    )
    monkeypatch.setattr(
        lr.compose, "exec_in",
        lambda *a, **k: type("R", (), {"ok": attack_ok, "returncode": 0 if attack_ok else 7,
                                       "stdout": "", "stderr": ""})(),
    )
    # 30 flat one-second buckets per segment: p99 and p999 both constant. The
    # bucket size is above MIN_SAMPLES_PER_SECOND_P999 so `p999_flat` ENFORCES
    # here -- the gate-loop tests are about the loop, and a p999 record that
    # silently stopped enforcing would make them pass for the wrong reason.
    monkeypatch.setattr(
        lr.vegeta, "encode_per_second_buckets",
        lambda **k: [(i, (4.0, 9.0), 2000) for i in range(lr.WARMUP_SEGMENT_S)],
    )
    # The per-segment target draw is exercised by its own tests; here it is the
    # gate loop under test, and a real draw would generate a body pool per case.
    monkeypatch.setattr(runner, "draw_and_ship", _fake_draw)
    return runner._env(profiles=[compose.PROFILE_LOAD])


def _fake_draw(*, slot, cell, stage, rate, block, phase, count):
    from collections import Counter
    from pathlib import Path
    # Everything here varies with the PHASE, because every quantity the warmup
    # folds out of these draws is a per-segment one: an inc list that lost a
    # segment, or an id union that lost one, is only visible if the segments
    # differ. `seg` is the trailing digits of "warmup.segN".
    seg = int("".join(c for c in phase if c.isdigit()) or 0)
    # generation and ship are DIFFERENT values so a total that drops either term
    # is a different number, not the same one twice.
    return DrawnTargets(targets_path=Path(f"targets.{phase}.txt"), count=count,
                        inc=0x1234567890ABCDEF1234567890ABCDE0 + seg,
                        invoice_id_reads=Counter({100 + seg: 7, 200 + seg: 1}),
                        generation_seconds=0.25, ship_seconds=0.5)


def test_warmup_exits_at_the_first_segment_where_the_gates_pass(runner, monkeypatch):
    env = _wire(monkeypatch, runner, pool_total=24)
    res = runner._warmup(env, "dotnet", "http://localhost:8082", 300, _CELL, rate=500)
    assert res.exit_reason == "gates_passed"
    assert res.segments == lr.WARMUP_MIN_SEGMENTS      # not ceil(300/30) == 10
    assert res.under_warmed is False


def test_warmup_that_never_settles_runs_to_the_cap_and_is_flagged(runner, monkeypatch):
    # pool never fills => pools_warm can never pass
    env = _wire(monkeypatch, runner, pool_total=3)
    res = runner._warmup(env, "dotnet", "http://localhost:8082", 120, _CELL, rate=500)
    assert res.segments == 120 // lr.WARMUP_SEGMENT_S
    assert res.exit_reason == "cap_reached"
    assert res.under_warmed is True


def test_a_failed_warmup_attack_flags_the_probe_instead_of_passing_it(runner, monkeypatch):
    """Regression: `under and elapsed >= cap_s` recorded a crashed warmup as WARM.

    A failed vegeta exec leaves every series empty, so every gate fails, but the
    run stopped early -- so the old `elapsed >= cap_s` conjunct was False and the
    probe entered the fit as measurement-grade.
    """
    env = _wire(monkeypatch, runner, pool_total=24, attack_ok=False)
    res = runner._warmup(env, "dotnet", "http://localhost:8082", 300, _CELL, rate=500)
    assert res.under_warmed is True
    assert res.exit_reason == "attack_failed_rc7"
    assert res.segments == 1


def test_a_posture_override_relaxes_the_gate_set_for_that_cell_only(runner, monkeypatch):
    """R1-A3's fix, wired end to end -- not just resolvable in the config model.

    Same setup as the never-settles test above: the pool never fills, so
    `pools_warm` cannot pass. Under a posture that declares that gate
    unenforced, warmup exits at the first evaluated boundary; a cell with no
    override is still held to the full set and still burns the cap.
    """
    env = _wire(monkeypatch, runner, pool_total=3)
    monkeypatch.setattr(
        runner.slo.warmup, "gates_by_posture",
        {_CELL_ID: PostureWaiver(gates={"pools_warm": False},
                                 rationale="SYNTHETIC: the pool never fills here")},
    )
    relaxed = runner._warmup(env, "dotnet", "http://localhost:8082", 300, _CELL, rate=500)
    assert relaxed.exit_reason == "gates_passed"
    assert relaxed.under_warmed is False

    strict = runner._warmup(
        env, "dotnet", "http://localhost:8082", 120,
        Cell(cell_id="headline.mixed.gc-generous"), rate=500,
    )
    assert strict.exit_reason == "cap_reached"
    assert strict.under_warmed is True

    # The waiver must be VISIBLE. Resolving the posture makes the gate vanish
    # from evaluate(), and a gate that silently stops being checked is R1-B1
    # exactly: round 1 declared six warmup gates, evaluated four, and recorded
    # nothing about the other two.
    waived = [g for g in relaxed.gates if g.status == lr_warmup.STATUS_POSTURE_WAIVED]
    assert [g.name for g in waived] == ["pools_warm"]
    assert waived[0].passed is False        # never reads as a pass
    assert waived[0].enforced is False      # never flags the probe
    assert waived[0].detail["cell_id"] == _CELL_ID
    assert not [g for g in strict.gates if g.status == lr_warmup.STATUS_POSTURE_WAIVED]


def test_a_cell_declared_waiver_reaches_the_probe_and_carries_its_rationale(
    runner, monkeypatch
):
    """W22: the override declared on the CELL, not only in spec/slo.yaml.

    Same never-settling pool as above, so `pools_warm` cannot pass. The cell
    declares it not applicable and warmup exits at the first evaluated boundary.
    The record must say `not_applicable` (a code-declared waiver) rather than
    `posture_waived` (a spec-declared one): the two have different provenance,
    and only one of them is inside the ledger's config_hash.
    """
    env = _wire(monkeypatch, runner, pool_total=3)
    why = "SYNTHETIC: this posture never opens the pool"
    cell = Cell(cell_id=_CELL_ID, warmup_gates={"pools_warm": False},
                gate_set_rationale=why)

    res = runner._warmup(env, "dotnet", "http://localhost:8082", 300, cell, rate=500)
    assert res.exit_reason == "gates_passed"
    assert res.under_warmed is False

    waived = [g for g in res.gates if g.status == lr_warmup.STATUS_NOT_APPLICABLE]
    assert [g.name for g in waived] == ["pools_warm"]
    assert waived[0].passed is False        # never reads as a pass
    assert waived[0].enforced is False      # never flags the probe
    assert waived[0].detail["cell_id"] == _CELL_ID
    assert waived[0].detail["rationale"] == why
    # No registry entry names a knob for this gate, so the record must not imply
    # a mechanism it cannot point at.
    assert waived[0].detail["knob"] is None
    assert "whole of the evidence" in waived[0].detail["note"]
    assert not [g for g in res.gates if g.status == lr_warmup.STATUS_POSTURE_WAIVED]


def test_a_cell_waiver_names_the_registered_knob_behind_it(runner, monkeypatch):
    """When the registry DOES name the knob, the probe record carries it.

    That is the link W22 closes: the same declaration that would have refused
    the cell had the gate stayed enforced is what the waived record cites.
    """
    import dataclasses

    entry = registry.UnsatisfiableGate(
        gate="pools_warm", env_var="SYNTHETIC_KNOB", value="off",
        why="SYNTHETIC TEST ENTRY: the pool is never opened",
    )
    slot = registry.SLOTS["dotnet"]
    monkeypatch.setitem(registry.SLOTS, "dotnet", dataclasses.replace(
        slot, language=dataclasses.replace(slot.language, unsatisfiable_gates=(entry,)),
    ))
    env = _wire(monkeypatch, runner, pool_total=3)
    cell = Cell(cell_id=_CELL_ID, extra_env={"SYNTHETIC_KNOB": "off"},
                warmup_gates={"pools_warm": False},
                gate_set_rationale="SYNTHETIC: see the registered knob")

    res = runner._warmup(env, "dotnet", "http://localhost:8082", 300, cell, rate=500)
    waived = [g for g in res.gates if g.status == lr_warmup.STATUS_NOT_APPLICABLE]
    assert waived[0].detail["knob"] == "SYNTHETIC_KNOB=off"
    assert "unsatisfiable by construction" in waived[0].detail["note"]


def test_the_warmup_record_says_where_it_ran_and_what_it_was_scored_against(
    runner, monkeypatch
):
    """W23: the record carries cell/stage/block, not only the artifact PATH.

    A `warmup.json` read anywhere but out of its own directory must still say
    which probe it belongs to, and the gate verdict carries its threshold so it
    can be re-derived.
    """
    env = _wire(monkeypatch, runner, pool_total=24)
    res = runner._warmup(env, "dotnet", "http://localhost:8082", 300, _CELL, rate=500,
                         stage="ladder", block=3)
    assert (res.cell_id, res.stage, res.block) == (_CELL_ID, "ladder", 3)
    assert res.gate_set_id == lr_warmup.gate_set_for_cell(
        runner.slo.warmup, cell_id=_CELL_ID, cell_gates=None,
    ).gate_set_id
    assert res.thresholds.hard_cap_seconds == 300
    assert res.thresholds.flatness_trailing_samples == lr_warmup.FLATNESS_TRAILING_SAMPLES
    assert res.thresholds.mann_kendall_alpha == lr_warmup.MANN_KENDALL_ALPHA
    # the SCALED minimums this probe was held to, which the spec cannot supply
    # after the fact
    assert res.thresholds.per_endpoint_min_calls == runner._scaled_min_calls()


def test_a_gate_record_says_when_in_the_warmup_it_was_evaluated(runner, monkeypatch):
    """`at_seconds` was declared and never written. A waiver keeps it None."""
    env = _wire(monkeypatch, runner, pool_total=3)
    cell = Cell(cell_id=_CELL_ID, warmup_gates={"pools_warm": False},
                gate_set_rationale="SYNTHETIC: this posture never opens the pool")
    res = runner._warmup(env, "dotnet", "http://localhost:8082", 300, cell, rate=500)

    scored = [g for g in res.gates if g.enforced]
    assert scored and all(g.at_seconds is not None for g in scored)
    # the verdict was taken at a segment boundary, never before the load ran
    assert all(g.at_seconds > 0 for g in scored)
    waived = [g for g in res.gates if g.status == lr_warmup.STATUS_NOT_APPLICABLE]
    assert [g.at_seconds for g in waived] == [None]   # never evaluated, no instant


def test_the_warmup_result_lists_what_it_scored_and_what_failed(runner, monkeypatch):
    """The two lists `run_probe` hands to `ProbeOutcome`, and the ledger records.

    The cell waives one gate, so the record holds a `passed: false` entry that
    is NOT a failure: an abstaining record counted as one would attribute the
    flag to a gate nobody scored, which is the whole point of `enforced`.
    """
    env = _wire(monkeypatch, runner, pool_total=3)
    cell = Cell(cell_id=_CELL_ID, warmup_gates={"gc_steady": False},
                gate_set_rationale="SYNTHETIC: not scored on this posture")
    res = runner._warmup(env, "dotnet", "http://localhost:8082", 120, cell, rate=500)
    assert res.under_warmed is True
    assert res.gates_failed == ["pools_warm"]        # not gc_steady, which abstained
    assert "gc_steady" not in res.gates_evaluated
    assert set(res.gates_evaluated) == {g.name for g in res.gates if g.enforced}


def test_the_warmup_result_carries_the_measured_bucket_size(runner, monkeypatch):
    """W21: the flatness vector, and the bucket size the q=0.999 half of it needs.

    `samples_per_second` decides whether `p999_flat` enforces, so it is read off
    the bucket the quantile was actually taken over -- not off the offered rate.
    The same count lands in every `quantile_series` row, because B2's calibration
    corpus is built from that artifact and a q=0.999 sample whose bucket size is
    unknown cannot be told from the second's maximum.
    """
    env = _wire(monkeypatch, runner, pool_total=24)
    # W29 dropped `p999_flat` from spec/slo.yaml warmup.gates, so the cell
    # declares it: this test is about the BUCKET SIZE the q=0.999 half needs,
    # and the machinery that consumes it is quantile-generic and kept.
    cell = Cell(cell_id=_CELL_ID, warmup_gates={"p999_flat": True},
                gate_set_rationale="SYNTHETIC: exercising the q=0.999 resolution path")
    res = runner._warmup(env, "dotnet", "http://localhost:8082", 120, cell, rate=500)
    vec = {g.name: g.detail.get("flatness") for g in res.gates
           if g.name in ("p99_flat", "p999_flat")}
    assert vec["p99_flat"]["samples_per_second"] == 2000.0
    assert vec["p99_flat"]["samples_per_second_source"] == "measured_bucket_median"
    assert vec["p99_flat"]["s_rel_pct_per_min"] is not None
    # 2000 samples/s clears MIN_SAMPLES_PER_SECOND_P999, so p999 is enforceable
    assert vec["p999_flat"]["status"] == lr_warmup.STATUS_MEASURED
    assert res.quantile_series and all(row[4] == 2000 for row in res.quantile_series)


def test_warmup_rate_does_not_move_when_the_hard_cap_moves(runner):
    """The cap is a ceiling for the slow case; it must not shape warmup traffic.

    Tying the rate to the cap meant O4's 180 -> 300 raise silently dropped the
    warmup rate 618 -> 371 rps -- a different traffic shape for HotSpot to warm
    against, from a change that was supposed to be free.
    """
    min_calls = runner._scaled_min_calls()
    assert runner._warmup_rate(min_calls, 300) == runner._warmup_rate(min_calls, 180)


def test_the_measured_tiers_warm_at_the_rate_the_published_figures_were_taken_at():
    """The warmup rate is a PUBLISHED parameter, so it is pinned here.

    `working_set.prewarm_containment_per_probe`'s envelope, the manifest's
    comment on it and PROGRESS's W20 rows are all measurements taken AT this
    rate -- the pre-warm draw is 10 x `probe_draw_count(rate, 30)` targets, so
    the rate sets the size of the set a measured window is scored against. The
    first version of those figures was taken at 371 rps, the value this function
    was changed to stop producing, and every one of them was wrong.

    So: a change to `spec/slo.yaml warmup.per_endpoint_min_calls` or to
    `spec/workload.yaml mix` moves this number, and it must break here rather
    than quietly invalidating documented figures. Both measured tiers share it;
    smoke scales the minimums by 0.02 and caps at 15 s, so it does not.
    """
    slo = load_slo()
    for scale, expected in (("full", 618), ("rehearsal", 618), ("smoke", 149)):
        prof = ScaleProfile.resolve(scale, slo)
        r = LiveProbeRunner.__new__(LiveProbeRunner)
        r.profile, r.slo, r.workload = prof, slo, load_workload()
        assert r._warmup_rate(r._scaled_min_calls(), prof.warmup_cap_s) == expected, scale
    # 618 is the worst-case endpoint's requirement over WARMUP_RATE_TARGET_S with
    # 10% headroom -- healthz at a 2% mix share needs 100,000 requests -- and NOT
    # a function of the 300 s cap.
    assert lr.WARMUP_RATE_TARGET_S == 180


# --------------------------------------------------------------------------- #
# W20 / R1-A7: the body pool and the per-probe draw, at the runner boundary
# --------------------------------------------------------------------------- #
@pytest.fixture
def io_runner(tmp_path, monkeypatch):
    """A runner whose compose calls are stubbed and whose io tree is tmp_path.

    The draw itself is real: the label list is built HERE, and a label list is
    exactly the kind of thing a unit test of `vegeta.draw_targets` cannot check.
    """
    monkeypatch.setattr(lr.paths, "results_dir", lambda: tmp_path)
    ok = type("R", (), {"ok": True, "returncode": 0, "stdout": "yes", "stderr": ""})
    monkeypatch.setattr(lr.compose, "exec_in", lambda *a, **k: ok())
    monkeypatch.setattr(lr.compose, "compose", lambda *a, **k: ok())
    slo = load_slo()
    profile = ScaleProfile.resolve("smoke", slo)
    r = LiveProbeRunner(run_id="io-1", profile=profile, workload=load_workload(),
                        slo=slo, bench_overlay=False)
    r.ensure_body_pool(_CELL)
    return r


def test_both_slots_of_a_block_draw_the_identical_sequence(io_runner):
    """The RCB pairing contract, at the layer that builds the label list.

    `slot` must not reach `stream_for`. If it did, the two halves of a block
    would face different requests and the pairing -- which is what makes a
    blocked design a blocked design -- would be gone while every artifact still
    looked well-formed.
    """
    slots = [s.slot_id for s in lr._slots()]
    drawn = {
        sid: io_runner.draw_and_ship(
            slot=sid, cell=_CELL, stage="ladder", rate=500, block=0,
            phase="measured", count=900,
        )
        for sid in slots
    }
    a, b = (drawn[s] for s in slots)
    assert a.inc == b.inc
    urls = [registry.get_slot(s).net_url for s in slots]
    stripped = [
        d.targets_path.read_text(encoding="utf-8").replace(u, "")
        for d, u in zip(drawn.values(), urls, strict=True)
    ]
    assert stripped[0] == stripped[1]
    # and the files really did differ before stripping (a no-op replace would
    # make the assertion above vacuous)
    assert (drawn[slots[0]].targets_path.read_bytes()
            != drawn[slots[1]].targets_path.read_bytes())


def test_the_warmup_the_discard_and_the_measured_window_draw_different_streams(io_runner):
    """R1-A7 at the runner: three attacks, three streams, three files.

    Round 1 passed `targets_in_container="targets.txt"` for all three, and
    vegeta reads a targets file from the top on every invocation -- so the
    warmup, which runs right after a Redis FLUSHALL, filled the cache with the
    ids the measured window was about to ask for.
    """
    phases_seen = ["warmup.seg0", "warmup.seg1", "discard", "measured"]
    drawn = [
        io_runner.draw_and_ship(slot="dotnet", cell=_CELL, stage="ladder", rate=500,
                                block=0, phase=p, count=600)
        for p in phases_seen
    ]
    assert len({d.inc for d in drawn}) == len(phases_seen)
    assert len({d.targets_path.name for d in drawn}) == len(phases_seen)
    assert len({d.targets_path.read_bytes() for d in drawn}) == len(phases_seen)


def test_the_zipf_tables_are_built_once_per_run_not_once_per_draw(io_runner, monkeypatch):
    """`build_zipf_table` allocates an N-entry weights list AND an N-entry CDF.

    At full scale N is 5,000,000 invoices: ~300 MB of peak allocation and about
    a second of pure-Python work. Round 1 rebuilt both inside every
    `generate_targets` call and called it once or twice per run; a per-probe
    draw runs 600+ times, so injecting the tables is what keeps B8 off the
    critical path.
    """
    calls = []
    monkeypatch.setattr(lr.vegeta, "build_zipf_table",
                        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
                            AssertionError("rebuilt a Zipf table")))
    for phase in ("warmup.seg0", "discard", "measured"):
        io_runner.draw_and_ship(slot="dotnet", cell=_CELL, stage="ladder", rate=500,
                                block=0, phase=phase, count=400)
    io_runner.ensure_body_pool(_CELL)          # idempotent: no rebuild either
    assert calls == []


def test_the_body_pool_is_not_fsynced_per_body(tmp_path, monkeypatch):
    """~350,000 fsyncs per run for files that are regenerable by construction.

    `paths.write_bytes_atomic` mkstemps, writes, flushes, FSYNCS and renames.
    That is the right contract for the ledger and for a published artifact; the
    io tree is neither (`.gitignore` excludes `results/**/io/`, and the checksum
    map cannot cover it), so the atomic writer is not used there.
    """
    import os as _os

    from ledgerbench import vegeta as vg

    synced = []
    monkeypatch.setattr(_os, "fsync", lambda fd: synced.append(fd))
    wl = load_workload()
    z = vg.zipf_tables(wl, "smoke")
    pool = vg.generate_body_pool(wl, out_dir=tmp_path, seed=1, n_create=5,
                                 n_quote=5, scale="smoke", customer_zipf=z[0])
    vg.draw_targets(wl, out_path=tmp_path / "t.txt", count=200, rng=lr.PCG64(1),
                    base_url="", pool=pool, customer_zipf=z[0], invoice_zipf=z[1])
    assert synced == []
    assert len(list((tmp_path / "bodies").glob("*.json"))) == 10


def test_each_warmup_segment_draws_its_own_stream_at_the_warmup_rate():
    """Two facts the warmup loop cannot be unit-tested through, so they live here.

    The loop runs inside `_warmup`, which needs docker, and every test of the
    gate logic stubs the draw out -- so both of these were invisible to the
    suite until they were extracted. Planting each of them (a phase label that
    drops the segment index; a count sized from the probe's measured rate) and
    watching the whole file still pass is how that was found.
    """
    labels = [warmup_segment_draw(seg=i, warm_rate=618, seg_s=30)[0] for i in range(4)]
    assert labels == ["warmup.seg0", "warmup.seg1", "warmup.seg2", "warmup.seg3"]
    assert len(set(labels)) == 4

    # sized from the WARMUP rate; the probe's measured rate is a label, not a size
    _, count = warmup_segment_draw(seg=0, warm_rate=618, seg_s=30)
    assert count == vegeta.probe_draw_count(618, 30)
    assert count != vegeta.probe_draw_count(13312, 30)
    # the final segment is short when the cap is not a multiple of the segment
    assert warmup_segment_draw(seg=9, warm_rate=618, seg_s=10)[1] < count


def test_a_probes_draw_cost_counts_every_attack_and_the_ship_as_well():
    """The artifact that answers "did B8 become the longest phase of the run".

    It had NO test: the accumulation lived as three `+=` statements inside
    `run_probe`, which is reachable only through docker, so deleting any of
    them left the whole suite green -- and the published cost claim rests on
    this number. It also excluded the `docker cp`, which is the half that scales
    with the rung: 4.1 MB shipped at 500 rps against 109.3 MB at 13,312.

    Every term is scored separately here, so a total that drops one is caught by
    the assertion for that term rather than by a single sum that could be right
    for the wrong reason.
    """
    from pathlib import Path

    warm = lr_warmup.WarmupResult(
        language="dotnet", warmup_rate=618,
        thresholds=lr_warmup.WarmupThresholds(hard_cap_seconds=300,
                                              flatness_trailing_samples=30,
                                              mann_kendall_alpha=0.05,
                                              per_endpoint_min_calls={}),
        elapsed_seconds=60.0, target_draw_seconds=4.0,
    )

    def d(gen, ship):
        return DrawnTargets(targets_path=Path("t.txt"), count=1, inc=0,
                            generation_seconds=gen, ship_seconds=ship)

    measured, discard = d(3.0, 1.5), d(0.5, 0.25)
    assert probe_draw_cost(warm, measured, discard) == 4.0 + 3.0 + 1.5 + 0.5 + 0.25
    # each term is load-bearing
    assert probe_draw_cost(warm) == 4.0                       # warmup segments
    assert probe_draw_cost(warm, d(3.0, 0.0)) == 7.0          # generation
    assert probe_draw_cost(warm, d(0.0, 1.5)) == 5.5          # the ship


def test_the_warmup_total_sums_every_segments_generation_and_ship(runner, monkeypatch):
    """The one draw-cost accumulation that stays inside a loop, asserted through it.

    `_warmup` is reachable in this suite (the gate tests drive it with a stubbed
    draw), so its per-segment total is checked here rather than extracted. The
    stub pays 0.25 s of generation and 0.5 s of ship per segment, so a total
    that dropped either term would not be 0.75 x segments.
    """
    env = _wire(monkeypatch, runner, pool_total=3)      # never settles: runs to cap
    res = runner._warmup(env, "dotnet", "http://localhost:8082", 120, _CELL, rate=500)
    assert res.segments == 4
    assert res.target_draw_seconds == pytest.approx(4 * (0.25 + 0.5))


def test_the_warmup_carries_every_segments_ids_and_inc_out_of_the_loop(
    runner, monkeypatch,
):
    """The warmup's per-segment ids and incs survive the segment loop.

    `prewarm_containment` is scored against the union of the ids drawn before
    the measured window, and the warmup's segments are most of that union. A
    warmup that loses a segment's ids publishes a containment well below its
    true size, and no artifact flags it.

    The incs are asserted beside it because they are the other per-segment fold
    and they are what lets a reader regenerate exactly what warmed this probe.
    """
    env = _wire(monkeypatch, runner, pool_total=3)     # never settles: runs to cap
    res = runner._warmup(env, "dotnet", "http://localhost:8082", 120, _CELL, rate=500)

    assert res.segments == 4
    # every segment's ids, not just the last one's and not none of them
    assert res.target_draw_invoice_ids == {100, 101, 102, 103, 200, 201, 202, 203}
    assert len(res.target_draw_incs) == 4
    assert len(set(res.target_draw_incs)) == 4        # one per segment, in order
    assert res.target_draw_incs[0].endswith("e0")
    assert res.target_draw_incs[3].endswith("e3")


def test_the_prewarmed_set_is_the_warmup_plus_the_discard_and_neither_is_optional():
    """`PREWARM_SCOPE` as code: the warmup ids plus the discard ids.

    `run_probe` is docker-only, so `prewarmed_ids = set(warm.target_draw_invoice_ids)`
    could be replaced with `set()` and nothing failed -- which drops the warmup
    term from the containment the manifest and the report publish. Both terms
    are scored separately here so a union that loses either is caught by the
    assertion for that term.
    """
    from pathlib import Path

    warm = lr_warmup.WarmupResult(
        language="dotnet", warmup_rate=618,
        thresholds=lr_warmup.WarmupThresholds(hard_cap_seconds=300,
                                              flatness_trailing_samples=30,
                                              mann_kendall_alpha=0.05,
                                              per_endpoint_min_calls={}),
        elapsed_seconds=60.0,
        target_draw_invoice_ids={1, 2, 3},
    )

    def d(*ids):
        from collections import Counter
        return DrawnTargets(targets_path=Path("t.txt"), count=1, inc=0,
                            invoice_id_reads=Counter(dict.fromkeys(ids, 1)))

    assert lr.prewarmed_ids(warm, d(3, 4, 5)) == {1, 2, 3, 4, 5}
    assert lr.prewarmed_ids(warm) == {1, 2, 3}              # the warmup term
    assert lr.prewarmed_ids(warm, d(9)) - {1, 2, 3} == {9}  # the discard term
    # ...and the scope this set stands for is stated in the artifact, so the
    # published fraction cannot be read without it.
    assert "warmup segment" in lr.PREWARM_SCOPE and "discard" in lr.PREWARM_SCOPE


def test_the_warmup_fold_keeps_every_segment_and_never_collapses_them():
    """`warmup_draws` unit-tested away from the loop it was extracted from."""
    from pathlib import Path

    def d(seg, gen, ship, *ids):
        from collections import Counter
        return DrawnTargets(targets_path=Path("t.txt"), count=1, inc=seg,
                            invoice_id_reads=Counter(dict.fromkeys(ids, 1)),
                            generation_seconds=gen, ship_seconds=ship)

    folded = lr.warmup_draws([d(1, 0.25, 0.5, 7, 8), d(2, 0.25, 0.5, 8, 9)])
    assert folded.incs == [f"{1:032x}", f"{2:032x}"]
    assert folded.invoice_ids == {7, 8, 9}
    assert folded.seconds == pytest.approx(1.5)
    assert lr.warmup_draws([]) == lr.WarmupDraws(incs=[], invoice_ids=set(), seconds=0.0)


def test_a_shipped_targets_file_is_named_for_the_probe_that_drew_it():
    """/io outlives a probe, so a fixed filename is a stale-file hazard.

    Under `targets.<phase>.txt` every probe wrote over the last one, on a volume
    that persists across probes. Paired with an unchecked `docker cp` that meant
    a failed ship left the PREVIOUS probe's file for vegeta to attack -- and if
    that file had been drawn for a lower rung it WRAPS inside the measured
    window, which is R1-A7 reintroduced with no error anywhere in the run.

    The name is the stream identity minus the slot, which is exactly right: the
    slot is the directory, and both slots of an RCB block draw the SAME sequence
    by design, so their files are same-named in different directories.
    """
    base = dict(cell_id="headline.mixed.gc-default", stage="ladder", rate=500,
                block=0, phase="measured")
    name = probe_targets_name(**base)
    variants = [
        {**base, "cell_id": "headline.mixed.gc-alt"},
        {**base, "stage": "confirm"},
        {**base, "rate": 600},
        {**base, "block": 1},
        {**base, "phase": "discard"},
        {**base, "phase": "warmup.seg0"},
        {**base, "phase": "warmup.seg1"},
    ]
    assert len({probe_targets_name(**v) for v in variants} | {name}) == 8
    assert "/" not in name and " " not in name
    assert name.endswith(".txt")
    # every axis of the identity is IN the name, so two probes cannot collide
    for token in ("headline.mixed.gc-default", "ladder", "r500", "b0", "measured"):
        assert token in name


def test_a_failed_ship_raises_instead_of_letting_vegeta_attack_a_stale_file(io_runner, monkeypatch):
    """`check=False` with a discarded return value, against a persistent volume.

    `smoke.py` checks the same call; `live_runner` did not, and the failure is
    silent by construction: the file is already there from the last probe, so
    vegeta starts, reports a clean attack, and the run records a measured window
    against the wrong targets. Raising is right rather than returning a failed
    probe -- a probe that never received its targets has nothing to retry.
    """
    failed = type("R", (), {"ok": False, "returncode": 1, "stdout": "",
                            "stderr": "no such container"})

    def only_cp_fails(env, argv, **kw):
        if argv and argv[0] == "cp":
            return failed()
        return type("R", (), {"ok": True, "returncode": 0, "stdout": "yes",
                              "stderr": ""})()

    monkeypatch.setattr(lr.compose, "compose", only_cp_fails)
    # `PhaseError` specifically: `run-all` catches exactly that and prints
    # "run-all failed: ..." with exit 1, the way every other loud failure in the
    # probe path already surfaced. As a bare `RuntimeError` this one escaped as
    # a traceback -- from a run that has been going for twenty hours.
    with pytest.raises(lr.PhaseError) as exc:
        io_runner.draw_and_ship(slot="dotnet", cell=_CELL, stage="ladder", rate=500,
                                block=0, phase="measured", count=200)
    assert "docker cp" in str(exc.value) and "no such container" in str(exc.value)


def test_the_measured_draw_is_shipped_before_the_discard_attack_runs(io_runner):
    """ORDERING, and it is the reason the head-of-window discard exists.

    The discard is there so the measured window contains no transient. W20 put
    the measured draw and its `docker cp` BETWEEN the discard and the window --
    a rate-correlated perturbation inside exactly that gap: measured on this
    box, generation alone is 0.13 s for a 4.1 MB file at 500 rps against 3.59 s
    for 109.3 MB at 13,312, with the copy on top. Asserted against `run_probe`'s
    source because
    that method needs a docker daemon; the ordering is one statement's position
    and nothing else in the suite can see it.
    """
    import inspect

    src = inspect.getsource(LiveProbeRunner.run_probe)
    measured_draw = src.index('phase="measured", count=vegeta.probe_draw_count')
    discard_draw = src.index('phase="discard",')
    discard_attack = src.index('out_bin_in_container=f"/io/{lang}/discard.bin"')
    assert measured_draw < discard_draw < discard_attack
    # and the discard's own draw still precedes its own attack, which absorbs
    # whatever transient it makes
    assert src.index("d_params = AttackParams") > discard_draw


def test_the_pool_meta_carries_the_mix_so_a_fired_lever_rebuilds_the_bodies(
    io_runner, tmp_path,
):
    """`_pool_meta["mix"]` is the only thing that detects a lever-changed mix.

    The capacity gate can change the effective workload mid-run
    (`apply_effective`), and the create/quote SPLIT of the pool is a function of
    the mix. Regeneration is decided by comparing the meta on disk against the
    meta now -- an in-process flag cannot decide it, because a resume is a new
    process. Drop `mix` from that dict and a run whose lever fired keeps
    attacking bodies drawn under the pre-lever mix, with every artifact
    well-formed. Nothing asserted it.
    """
    import json as _json

    meta_path = tmp_path / "io-1" / "io" / "pool" / "pool.meta.json"
    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["mix"] == dict(io_runner.workload.mix.as_pairs())
    assert set(meta) >= {"mix", "body_pool_requests", "n_create", "n_quote",
                         "seed", "scale", "ref_prefix"}

    # a changed mix makes the meta on disk stale, which is what forces a rebuild
    changed = io_runner.workload.model_copy(deep=True)
    changed.mix.get_invoice, changed.mix.pdf_stub = (
        changed.mix.pdf_stub, changed.mix.get_invoice,
    )
    io_runner.apply_effective(workload=changed)
    assert io_runner._pool_meta()["mix"] != meta["mix"]
    io_runner.ensure_body_pool(_CELL)
    assert _json.loads(meta_path.read_text(encoding="utf-8")) == io_runner._pool_meta()


def test_the_draw_mode_the_report_prints_is_the_one_the_runner_recorded():
    """The report prints fixed text "Targets are DRAWN PER ATTACK" beside this.

    `working_set.draw_mode` is filled from `_draw_record`, which writes
    `live_runner.DRAW_MODE`, and the report renders that value in a sentence
    whose prose asserts the mode independently. Nothing tied the two together:
    change the constant and the report keeps claiming per-attack draws while
    printing a value that says something else. The seed formula beside it is the
    same shape of hazard -- it is the recipe a reader regenerates a probe's
    sequence from, so it has to name the labels the runner actually uses.
    """
    from pathlib import Path

    rec = lr._draw_record(
        DrawnTargets(targets_path=Path("t.txt"), count=10, inc=1),
        slot="dotnet", cell_id=_CELL_ID, stage="ladder", rate=500, block=0,
        measured_window_seconds=120, probe_draw_seconds=1.5, warmup_incs=["ab"],
        containment=vegeta.prewarm_containment(__import__("collections").Counter(), []),
    )
    assert rec["draw_mode"] == lr.DRAW_MODE == "per_probe_stream"
    assert rec["seed_formula"] == lr.DRAW_SEED_FORMULA
    for label in ("run_id", "cell_id", "stage", "rate", "block", "phase"):
        assert label in lr.DRAW_SEED_FORMULA
    # a window that asked for no invoice id records null, never a measured zero
    assert rec["prewarm_containment"]["fraction"] is None
    assert rec["prewarm_containment"]["measured_against"] == lr.PREWARM_SCOPE


def test_swap_repair_issues_docker_update_and_the_reread_decides(runner, monkeypatch):
    """The docker-29 create-path flake (memory.swap.max=max despite HostConfig
    MemorySwap == Memory, observed on the first tuned rehearsal's resume
    recreate of postgres and redis) gets ONE `docker update --memory X
    --memory-swap X` repair per flaked CONTAINER target; the placement re-read
    is what decides, so a repair that did not take still aborts. A target whose
    role has no declared ceiling is never touched."""
    from ledgerbench import live_runner as lr_mod

    calls: list[list[str]] = []

    class _R:
        ok = True
        stdout = stderr = ""

    monkeypatch.setattr(lr_mod, "run", lambda argv, **kw: calls.append(argv) or _R())

    class _RepairSampler(_FakeSampler):
        def __init__(self, flaked):
            super().__init__(bad=[])
            self.flaked = flaked
            self.reread = 0

        def swap_allowance_misconfigured(self):
            return self.flaked

        def reread_placement(self):
            self.reread += 1

    pg = cgroups.SampleTarget(
        label="postgres", path=None, role=cgroups.ROLE_DATABASE,
        persistent=True, container_name="ledgerline-postgres-1",
    )
    no_ceiling = cgroups.SampleTarget(
        label="dockerd", path=None, role=cgroups.ROLE_CONTAINER_RUNTIME,
        persistent=True, container_name="should-never-be-updated",
    )
    sampler = _RepairSampler([pg, no_ceiling])
    limits = {cgroups.ROLE_DATABASE: 8 * 1024 ** 3}
    assert runner._repair_swap_allowances(sampler, limits) is True
    assert calls == [[
        "docker", "update", f"--memory={8 * 1024 ** 3}",
        f"--memory-swap={8 * 1024 ** 3}", "ledgerline-postgres-1",
    ]]
    # nothing flaked -> no update issued, and the caller then skips the re-read
    sampler2 = _RepairSampler([])
    calls.clear()
    assert runner._repair_swap_allowances(sampler2, limits) is False
    assert calls == []
