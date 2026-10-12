"""Capacity-gate lever logic + headroom rule (spec/slo.yaml)."""

from __future__ import annotations

import pytest

from ledgerbench import config
from ledgerbench.capacity_gate import (
    CORES_FROM_CPUSET,
    MIN_PG_KNEE_RUNGS,
    PgWindowSample,
    LeverApplication,
    LeverMachine,
    ResolvedWorkloadParams,
    RungResult,
    Verdict,
    apply_params_to_workload,
    effective_pg_capacity_rps,
    effective_workload_record,
    evaluate_gate,
    format_cpuset,
    headroom_after_remeasure,
    headroom_ok,
    params_for_applied_levers,
    parse_cpuset,
    pg_cpuset_overrides,
    pg_knee_from_rungs,
    pg_utilization_from_windows,
    projected_db_load_rps,
    realize_levers,
    regate_decision,
    unmeasured_knee_result,
    utilization_ceiling,
    utilization_ok,
)


def _params():
    # Built from the real workload mix so the projection uses actual per-endpoint
    # weights (get 0.30, list 0.25, quote 0.20, create 0.15, pdf 0.08, healthz 0.02).
    return ResolvedWorkloadParams.from_workload(config.load_workload())


def test_projected_db_load_drops_with_levers():
    p = _params()
    base = projected_db_load_rps(10_000, p)
    # write_share lever 0.15 -> 0.10
    p.write_share = 0.10
    lower_write = projected_db_load_rps(10_000, p)
    assert lower_write < base
    # cache hit 0.80 -> 0.90 lowers read DB-touch
    p.cache_hit_rate = 0.90
    lower_cache = projected_db_load_rps(10_000, p)
    assert lower_cache < lower_write


def test_projection_uses_actual_mix_not_write_complement():
    # DB-touch = create(0.15) + list(0.25) + pdf(0.08) + get(0.30)*miss(0.20) = 0.54.
    # The old (1 - write_share)*miss read model would give a different number;
    # quote(0.20) and healthz(0.02) must NOT contribute (they never touch PG).
    p = _params()
    assert abs(projected_db_load_rps(10_000, p) - 5400.0) < 1e-6


def test_projection_excludes_non_db_endpoints():
    # Shifting quote/healthz share has no effect on projected DB load.
    p = _params()
    base = projected_db_load_rps(10_000, p)
    p.pricing_quote_share = 0.0
    p.healthz_share = 0.0
    assert projected_db_load_rps(10_000, p) == base


def test_headroom_rule():
    assert headroom_ok(pg_capacity_rps=1300, projected_db_rps=1000, headroom_factor=1.3)
    assert not headroom_ok(pg_capacity_rps=1299, projected_db_rps=1000, headroom_factor=1.3)


def test_pg_cores_lever_scales_capacity_linearly():
    p = _params()
    assert effective_pg_capacity_rps(3000, p) == 3000  # 3 cores -> unchanged
    p.pg_cores = 4
    assert effective_pg_capacity_rps(3000, p) == 4000  # 4/3 linear scale


def test_pg_cores_lever_can_rescue_a_db_imposed_case():
    # A case that FAILS headroom even after the write_share + cache_hit levers,
    # but PASSES once pg_cores 3->4 scales capacity, must NOT be declared
    # DB-imposed. This is the bug: previously pg_cores moved neither side.
    slo = config.load_slo()
    p = _params()
    projected_knee = 8000
    # need AFTER write+cache levers (the smallest requirement those two reach):
    after = ResolvedWorkloadParams.from_workload(config.load_workload())
    after.write_share = 0.10
    after.cache_hit_rate = 0.90
    need_after_soft = projected_db_load_rps(projected_knee, after) * slo.capacity_gate.headroom_factor
    # measured 3-core knee: below need_after_soft (soft levers can't pass) but its
    # 4/3 scale clears it (only the core bump rescues the case).
    measured = need_after_soft * 3.0 / 4.0 + 1.0
    assert measured < need_after_soft  # soft levers alone cannot pass
    res = evaluate_gate(
        pg_knee_rps=measured, projected_knee_rps=projected_knee,
        cfg=slo.capacity_gate, params=p,
    )
    assert res.verdict == Verdict.PASS
    assert "pg_cores" in {lv.name for lv in res.levers_fired}
    # the MEASURED knee is reported verbatim ...
    assert res.pg_knee_rps == pytest.approx(measured)
    assert res.pg_knee_measured_at_cores == 3
    # ... and the 4/3 core scaling lives in its OWN field, labeled a projection
    assert res.pg_capacity_projected_rps == pytest.approx(measured * 4 / 3)
    assert "LINEAR core-scaling assumption" in res.pg_capacity_projection_basis
    assert "NOT a measurement" in res.pg_capacity_projection_basis


def test_gate_passes_without_levers_when_capacity_high():
    slo = config.load_slo()
    res = evaluate_gate(
        pg_knee_rps=100_000, projected_knee_rps=8000,
        cfg=slo.capacity_gate, params=_params(),
    )
    assert res.verdict == Verdict.PASS
    assert res.levers_fired == []


def test_gate_fires_levers_in_order():
    slo = config.load_slo()
    # Choose a PG knee that fails at baseline but passes after some levers.
    p = _params()
    baseline_projected = projected_db_load_rps(8000, p) * slo.capacity_gate.headroom_factor
    # pg just under baseline so at least one lever must fire
    pg = baseline_projected - 1
    res = evaluate_gate(
        pg_knee_rps=pg, projected_knee_rps=8000, cfg=slo.capacity_gate, params=_params()
    )
    # Either it passed after firing >=1 lever, or it exhausted them honestly.
    if res.verdict == Verdict.PASS:
        assert len(res.levers_fired) >= 1
        # first lever fired is write_share (spec order)
        assert res.levers_fired[0].name == "write_share"
    else:
        assert res.verdict == Verdict.DB_IMPOSED_SHARED_KNEE


def test_gate_honest_fallback_when_levers_exhausted():
    slo = config.load_slo()
    res = evaluate_gate(
        pg_knee_rps=1.0, projected_knee_rps=100_000,
        cfg=slo.capacity_gate, params=_params(),
    )
    assert res.verdict == Verdict.DB_IMPOSED_SHARED_KNEE
    # all three levers attempted (write_share, cache_hit_rate, pg_cores)
    assert {lv.name for lv in res.levers_fired} == {"write_share", "cache_hit_rate", "pg_cores"}


def test_lever_machine_never_synchronous_commit_off():
    slo = config.load_slo()
    machine = LeverMachine(slo.capacity_gate, _params())
    fired = []
    while (lev := machine.try_next()) is not None:
        fired.append(lev.name)
    assert "synchronous_commit_off" not in fired
    assert fired == ["write_share", "cache_hit_rate", "pg_cores"]


def test_resolved_params_from_workload():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    assert p.write_share == wl.mix.create_invoice / 100.0  # 0.15
    assert p.cache_hit_rate == 0.80
    assert p.pg_cores == 3


# --------------------------------------------------------------------------- #
# a "knee" needs a LADDER, not one rung
# --------------------------------------------------------------------------- #
def _ladder(n: int, *, start: int = 500, step: float = 1.2) -> list[RungResult]:
    """n rungs that all track their offered rate cleanly."""
    out = []
    rate = float(start)
    for _ in range(n):
        out.append(RungResult(rate=int(rate), achieved=rate, p99_ms=3.0, error_rate=0.0))
        rate *= step
    return out


def test_single_rung_is_not_a_knee():
    # A create-only ladder that breaks on its first rung has one point and no
    # knee: that rung's achieved rate is not a measured PG ceiling.
    knee = pg_knee_from_rungs(_ladder(1))
    assert knee.measured is False
    assert knee.knee_rps is None
    assert knee.usable_rungs == 1
    assert "no usable PG knee measured" in knee.detail


def test_knee_needs_min_usable_rungs():
    for n in range(MIN_PG_KNEE_RUNGS):
        assert pg_knee_from_rungs(_ladder(n)).measured is False
    assert pg_knee_from_rungs(_ladder(MIN_PG_KNEE_RUNGS)).measured is True


def test_unusable_rungs_do_not_count_toward_the_minimum():
    rungs = _ladder(2)
    # two rungs that did NOT track offered (generator fell behind) + one errored
    rungs.append(RungResult(rate=900, achieved=500.0, p99_ms=90.0, error_rate=0.0))
    rungs.append(RungResult(rate=1000, achieved=1000.0, p99_ms=90.0, error_rate=0.5))
    knee = pg_knee_from_rungs(rungs)
    assert knee.usable_rungs == 2
    assert knee.measured is False


def test_knee_is_top_usable_rung_and_flags_lower_bound():
    rungs = _ladder(4)
    broken = RungResult(rate=1100, achieved=700.0, p99_ms=200.0, error_rate=0.0)
    knee = pg_knee_from_rungs([*rungs, broken])
    assert knee.measured and knee.knee_rps == pytest.approx(rungs[-1].achieved)
    assert knee.lower_bound is False
    # a ladder that never broke: the knee is only a FLOOR, flagged as such
    capped = pg_knee_from_rungs(_ladder(5))
    assert capped.lower_bound is True and "LOWER BOUND" in capped.detail


def test_unmeasured_knee_result_is_its_own_verdict():
    slo = config.load_slo()
    res = unmeasured_knee_result(pg_knee_from_rungs(_ladder(1)), cfg=slo.capacity_gate)
    assert res.verdict == Verdict.NO_PG_KNEE_MEASURED
    assert res.pg_knee_measured is False
    assert res.pg_knee_rps is None          # never a fallback number
    assert res.passed is False


# --------------------------------------------------------------------------- #
# fired levers must be REALIZABLE, and only realized levers may change the
# effective workload
# --------------------------------------------------------------------------- #
def test_write_share_lever_rewrites_the_mix_and_keeps_it_valid():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    p.write_share = 0.10
    eff = apply_params_to_workload(wl, p)
    assert wl.mix.create_invoice == 15          # spec untouched
    assert eff.mix.create_invoice == 10
    # the freed share lands on the one endpoint that never touches PG, so the
    # gate's DB-load projection stays exactly true after the rewrite
    assert eff.mix.pricing_quote == wl.mix.pricing_quote + 5
    assert sum(w for _, w in eff.mix.as_pairs()) == 100


def test_cache_hit_rate_lever_rewrites_the_target():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    p.cache_hit_rate = 0.90
    eff = apply_params_to_workload(wl, p)
    assert wl.cache_construction.target_hit_rate == 0.80
    assert eff.cache_construction.target_hit_rate == 0.90


def test_non_integral_write_share_is_refused():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    p.write_share = 0.125
    with pytest.raises(ValueError, match="integer mix percentage"):
        apply_params_to_workload(wl, p)


def test_pg_cores_lever_moves_cores_off_the_donor():
    env = pg_cpuset_overrides(4, taken_from="vegeta")
    assert env == {"PG_CPUS": "8-11", "VEGETA_CPUS": "12-13"}
    # no-op when PG already has the cores
    assert pg_cpuset_overrides(3, taken_from="vegeta") == {}
    # a donor that cannot spare a core fails loudly (never silently skipped)
    with pytest.raises(ValueError, match="cannot spare"):
        pg_cpuset_overrides(6, taken_from="vegeta", donor_cpus="11-13")
    with pytest.raises(ValueError, match="taken_from"):
        pg_cpuset_overrides(4, taken_from=None)


def test_cpuset_parse_format_roundtrip():
    assert parse_cpuset("8-10,14") == [8, 9, 10, 14]
    assert format_cpuset([8, 9, 10, 14]) == "8-10,14"
    assert format_cpuset([5]) == "5"


def test_cache_hit_rate_lever_is_not_applied_without_a_ttl():
    levers = [LeverApplication(name="cache_hit_rate", from_value=0.8, to_value=0.9)]
    rz = realize_levers(levers, cache_ttl_seconds=None)
    assert rz.applied == []
    assert "cache_hit_rate" in rz.not_applied
    assert rz.fully_applied is False
    # with an operator-supplied TTL it becomes a real compose override
    rz2 = realize_levers(levers, cache_ttl_seconds=900)
    assert rz2.applied == ["cache_hit_rate"]
    assert rz2.env_overrides["CACHE_TTL_SECONDS"] == "900"
    assert rz2.fully_applied is True


def test_only_applied_levers_reach_the_effective_params():
    wl = config.load_workload()
    spec = ResolvedWorkloadParams.from_workload(wl)
    fired = ResolvedWorkloadParams.from_workload(wl)
    fired.write_share, fired.cache_hit_rate, fired.pg_cores = 0.10, 0.90, 4
    # cache_hit_rate fired but could not be applied -> it must NOT show up
    out = params_for_applied_levers(spec, fired, ["write_share", "pg_cores"])
    assert out.write_share == 0.10
    assert out.pg_cores == 4
    assert out.cache_hit_rate == spec.cache_hit_rate


def test_effective_workload_record_shows_spec_vs_effective():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    p.write_share, p.pg_cores = 0.10, 4
    eff = apply_params_to_workload(wl, p)
    levers = [
        LeverApplication(name="write_share", from_value=0.15, to_value=0.10),
        LeverApplication(name="pg_cores", from_value=3, to_value=4, taken_from="vegeta"),
    ]
    rz = realize_levers(levers, cache_ttl_seconds=None)
    rec = effective_workload_record(wl, eff, p, realization=rz, levers_fired=levers)
    assert rec.spec_write_share == 0.15 and rec.effective_write_share == 0.10
    assert rec.spec_pg_cores == 3 and rec.effective_pg_cores == 4
    assert rec.spec_mix["create_invoice"] == 15
    assert rec.effective_mix["create_invoice"] == 10
    assert rec.write_share_absorbed_by == "pricing_quote"
    assert rec.differs_from_spec is True
    assert rec.env_overrides["PG_CPUS"] == "8-11"


def test_headroom_after_remeasure_does_not_rescale():
    slo = config.load_slo()
    p = ResolvedWorkloadParams.from_workload(config.load_workload())
    p.pg_cores = 4
    projected = projected_db_load_rps(8000, p)
    need = projected * slo.capacity_gate.headroom_factor
    ok, got = headroom_after_remeasure(
        remeasured_pg_knee_rps=need, projected_knee_rps=8000,
        cfg=slo.capacity_gate, params=p,
    )
    assert ok and got == pytest.approx(projected)
    # the 4/3 linear scaling must NOT be applied to a re-measured 4-core knee
    ok2, _ = headroom_after_remeasure(
        remeasured_pg_knee_rps=need * 3 / 4, projected_knee_rps=8000,
        cfg=slo.capacity_gate, params=p,
    )
    assert ok2 is False


# --------------------------------------------------------------------------- #
# create-only targets are ABSOLUTE URLs
# --------------------------------------------------------------------------- #
def test_create_only_targets_emit_absolute_urls(tmp_path):
    """vegeta's attack argv has no base-URL flag: a schemeless `POST /invoices`
    fails per-request at ~10 us without touching the SUT, so a ladder of such
    targets reports a one-rung "PG ceiling" that is pure vegeta errors. Every
    emitted target line must carry the SUT's absolute compose-network URL."""
    from ledgerbench.capacity_gate import create_only_targets
    from ledgerbench.config import load_workload
    from ledgerbench.registry import DEFAULT_SLOT_SET, get_slot, get_slot_set

    drive = get_slot(get_slot_set(DEFAULT_SLOT_SET).reference)
    wl = load_workload()
    create_only_targets(wl, out_dir=tmp_path, count=50, seed=42, scale="smoke",
                        base_url=drive.net_url)
    lines = (tmp_path / "targets.txt").read_text(encoding="utf-8").splitlines()
    posts = [ln for ln in lines if ln.startswith("POST ")]
    assert posts, "no POST targets emitted"
    for ln in posts:
        assert ln == f"POST {drive.net_url}/invoices", ln


def test_create_only_targets_has_no_default_base_url():
    """Its round-1 default was `http://sut-go:8080` and no caller overrode it, so
    the gate's whole ladder pointed at a service that no longer exists on the
    compose network -- and vegeta fails a schemeless/unreachable target in ~10 us
    without touching the SUT, which is exactly how a rung of pure errors got
    published as a PG ceiling."""
    import inspect

    from ledgerbench.capacity_gate import create_only_targets

    param = inspect.signature(create_only_targets).parameters["base_url"]
    assert param.default is inspect.Parameter.empty


# --------------------------------------------------------------------------- #
# STAGE 2: the re-gate rule at REALIZED rates (W26 / R1-A6)
# --------------------------------------------------------------------------- #
def _win(frac, *, cores=3, seconds=10.0, cpuset="8-10"):
    return PgWindowSample(
        cpu_delta_usec=int(frac * cores * seconds * 1e6),
        window_seconds=seconds, cpuset_effective=cpuset,
    )


def test_the_utilization_ceiling_is_the_headroom_rule_restated():
    """`capacity >= load x factor` divided through by capacity.

    The re-gate may not invent a second, looser rule: it asks the SAME question
    stage 1 asks, against a measurement instead of a projection.
    """
    assert utilization_ceiling(1.3) == pytest.approx(1.0 / 1.3)
    # busy fraction u means load = u x capacity, so headroom_ok and
    # utilization_ok must agree for every u.
    for u in (0.10, 0.50, 0.7692307, 0.7692308, 0.80, 0.99):
        capacity = 1000.0
        assert utilization_ok(u, 1.3) is headroom_ok(capacity, u * capacity, 1.3)


def test_pg_utilization_binds_on_the_worst_slot_not_the_pooled_mean():
    """The measured case the pooled mean hides.

    With a 4x knee spread the faster slot can hold PG at 0.90 while the slower
    holds it at 0.30. Their pooled figure is 0.60, which clears the 0.769
    ceiling; PG was nonetheless past it for every window of the faster slot, and
    PG serves whichever stack is resident.
    """
    m = pg_utilization_from_windows({
        "slow": [_win(0.30), _win(0.30)],
        "fast": [_win(0.90), _win(0.90)],
    })
    assert m.by_slot == pytest.approx({"slow": 0.30, "fast": 0.90})
    assert m.utilization == pytest.approx(0.90)
    assert m.binding_slot == "fast"
    # the pooled figure the max is protecting against
    pooled = m.cpu_seconds / (m.cores * m.window_seconds)
    assert pooled == pytest.approx(0.60)
    assert utilization_ok(pooled, 1.3) and not utilization_ok(m.utilization, 1.3)


def test_pg_utilization_reads_its_core_budget_off_the_windows():
    m = pg_utilization_from_windows({"a": [_win(0.5, cores=6, cpuset="8-13")]})
    assert m.cores == 6
    assert m.cores_source == CORES_FROM_CPUSET
    assert m.utilization == pytest.approx(0.5)


def test_pg_utilization_refuses_a_disputed_core_budget():
    """Cores cannot legitimately change inside a cell, so a disagreement is a
    finding; picking either value publishes a denominator that is a guess."""
    m = pg_utilization_from_windows({
        "a": [_win(0.5, cores=3, cpuset="8-10"), _win(0.5, cores=4, cpuset="8-11")],
    })
    assert m.utilization is None and not m.measured
    assert "'8-10'" in m.detail and "'8-11'" in m.detail


def test_pg_utilization_falls_back_only_with_a_named_source():
    bare = PgWindowSample(cpu_delta_usec=15_000_000, window_seconds=10.0)
    m = pg_utilization_from_windows({"a": [bare]}, fallback_cores=3,
                                    fallback_cores_source="the gate's pg_cores")
    assert m.cores == 3 and m.cores_source == "the gate's pg_cores"
    assert m.utilization == pytest.approx(0.5)
    # and with nothing to fall back on it refuses rather than assuming 3
    assert pg_utilization_from_windows({"a": [bare]}).utilization is None


def test_pg_utilization_drops_a_window_it_cannot_read():
    """An unreadable CPU delta summed as zero would DILUTE the utilization, and a
    missing duration would inflate it. Both are dropped, never defaulted."""
    good = _win(0.9)
    m = pg_utilization_from_windows({"a": [
        good,
        PgWindowSample(cpu_delta_usec=None, window_seconds=10.0, cpuset_effective="8-10"),
        PgWindowSample(cpu_delta_usec=1, window_seconds=None, cpuset_effective="8-10"),
        PgWindowSample(cpu_delta_usec=1, window_seconds=0.0, cpuset_effective="8-10"),
    ]})
    assert m.windows == 1
    assert m.utilization == pytest.approx(0.9)


def test_pg_utilization_with_no_usable_window_is_not_a_zero():
    m = pg_utilization_from_windows({"a": []})
    assert m.utilization is None and m.measured is False
    assert "no usable PostgreSQL window" in m.detail


def test_regate_decision_needs_both_rules():
    cfg = config.load_slo().capacity_gate
    ok = pg_utilization_from_windows({"a": [_win(0.50)]})
    hot = pg_utilization_from_windows({"a": [_win(0.90)]})
    # utilization alone
    assert regate_decision(ok, cfg=cfg).verdict is Verdict.PASS_AT_REALIZED_RATES
    assert regate_decision(hot, cfg=cfg).verdict is Verdict.DB_IMPOSED_AT_REALIZED_RATES
    # a failing projection sinks a passing utilization, and vice versa
    fails = (1000.0, 900.0)          # 1000 < 900 * 1.3
    passes = (1300.0, 900.0)
    assert regate_decision(ok, cfg=cfg, projection=passes).verdict is (
        Verdict.PASS_AT_REALIZED_RATES)
    d = regate_decision(ok, cfg=cfg, projection=fails)
    assert d.verdict is Verdict.DB_IMPOSED_AT_REALIZED_RATES
    assert d.utilization_ok is True and d.projection_ok is False
    d2 = regate_decision(hot, cfg=cfg, projection=passes)
    assert d2.utilization_ok is False and d2.projection_ok is True
    # ...and an unmeasured utilization is never evaluated against either rule
    d3 = regate_decision(pg_utilization_from_windows({"a": []}), cfg=cfg,
                         projection=passes)
    assert d3.verdict is Verdict.REGATE_NOT_EVALUABLE
    assert d3.utilization_ok is None and d3.projection_ok is None


def test_the_regate_verdicts_are_enum_members():
    """`phases.py` already leaks one non-enum verdict into the ledger; a second
    string space would put these beyond every `Verdict.X.value` comparison that
    enforces the first gate."""
    for name in ("PASS_AT_REALIZED_RATES", "DB_IMPOSED_AT_REALIZED_RATES",
                 "REGATE_NOT_EVALUABLE"):
        assert isinstance(getattr(Verdict, name), Verdict)
