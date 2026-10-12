"""spec/slo.yaml is only load-bearing where the code actually READS it.

Two failure shapes this file exists to catch, both live in the tree before P1:

  * the runner read `co_validation.stall_test["timeout_s"]` for the vegeta attack
    timeout -- a key that block has never defined -- so the value was always the
    hard-coded 5.0 fallback while looking spec-controlled;
  * `smoke.py` performed the stall check with a literal `sleep(1.0)` and a
    literal `"pause"` while the spec declared both one directory away, so an
    edit to spec/ changed nothing and the two could silently disagree.

Both are the same defect: a number that is declared in one place and obeyed in
another. The tests below assert the code reads what the spec says.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from ledgerbench import phases, registry, smoke, warmup
from ledgerbench.config import PostureWaiver, SloConfig, StallTest, load_slo, load_workload
from ledgerbench.live_runner import LiveProbeRunner
from ledgerbench.util import paths


@pytest.fixture(scope="module")
def raw_slo() -> dict:
    return yaml.safe_load(paths.slo_path().read_text(encoding="utf-8"))


def _patch_stall(monkeypatch, st: StallTest) -> None:
    """Make `smoke`'s spec read return `st`, leaving the rest of slo.yaml real."""
    base = load_slo()
    patched = base.model_copy(
        update={"co_validation": base.co_validation.model_copy(update={"stall_test": st})}
    )
    monkeypatch.setattr(smoke, "load_slo", lambda: patched)


# --------------------------------------------------------------------------- #
# stall test: typed, and actually obeyed
# --------------------------------------------------------------------------- #
def test_stall_test_is_typed_not_a_raw_dict():
    st = load_slo().co_validation.stall_test
    assert isinstance(st, StallTest)
    assert st.freeze_seconds == 1.0
    assert st.method == "docker_pause"


def test_the_phantom_timeout_key_can_no_longer_be_read():
    """A typed model turns the silent `.get(..., 5.0)` into an AttributeError."""
    st = load_slo().co_validation.stall_test
    assert not hasattr(st, "timeout_s")
    with pytest.raises(AttributeError):
        st.get  # a dict API on a spec block invites reading undeclared keys


def test_the_stall_check_performs_the_method_the_spec_declares():
    st = load_slo().co_validation.stall_test
    assert smoke._stall_verb(st) == "pause"
    line = smoke._stall_plan_line()
    assert " pause " in line
    assert str(st.freeze_seconds) in line


def test_an_unimplemented_stall_method_raises_rather_than_pausing_anyway():
    with pytest.raises(ValueError, match="not implemented"):
        smoke._stall_verb(StallTest(freeze_seconds=1.0, method="toxiproxy_latency"))


def test_the_executed_stall_step_refuses_a_method_it_cannot_perform(monkeypatch):
    """And it refuses BEFORE touching the SUT.

    Only one method is implemented, so a hard-coded `"pause"` on the executed
    path is observationally identical to the resolver in the happy case. This
    is the assertion that tells them apart: with a literal, an unimplemented
    pre-registered method would be silently performed as a docker pause and the
    run would report `co_stall ok`.
    """
    _patch_stall(monkeypatch, StallTest(freeze_seconds=1.0, method="toxiproxy_latency"))
    touched: list[str] = []
    monkeypatch.setattr(
        smoke.compose, "compose", lambda env, argv, **k: touched.append(argv[0])
    )
    with pytest.raises(ValueError, match="not implemented"):
        smoke.run_stall_check()
    assert touched == []


def test_the_executed_stall_step_obeys_the_spec_not_a_literal(monkeypatch):
    """The EXECUTED path, not the dry-run plan line.

    Both numbers lived as literals inside `run_smoke` while the spec declared
    them, so a spec edit moved the plan text and nothing else.
    """
    _patch_stall(monkeypatch, StallTest(freeze_seconds=2.5, method="docker_pause"))
    verbs: list[str] = []
    slept: list[float] = []
    monkeypatch.setattr(
        smoke.compose, "compose",
        lambda env, argv, **k: (
            verbs.append(argv[0]),
            type("R", (), {"ok": True, "returncode": 0, "stdout": "", "stderr": ""})(),
        )[1],
    )
    monkeypatch.setattr(smoke.time, "sleep", slept.append)
    monkeypatch.setattr(
        smoke.httpx, "get", lambda *a, **k: type("R", (), {"status_code": 200})()
    )

    step = smoke.run_stall_check()

    assert verbs == ["pause", "unpause"]
    assert slept == [2.5]
    assert step.ok is True
    assert "2.5s" in step.detail and "docker_pause" in step.detail


# --------------------------------------------------------------------------- #
# attack timeout: declared where the attack is, and read from there
# --------------------------------------------------------------------------- #
def test_attack_timeout_is_declared_in_the_ladder_block(raw_slo):
    assert "attack_timeout_seconds" in raw_slo["ladder"]
    assert "timeout_s" not in raw_slo["co_validation"]["stall_test"]


@pytest.mark.parametrize("scale", ["full", "rehearsal"])
def test_every_measured_tier_takes_its_attack_timeout_from_the_spec(scale):
    slo = load_slo()
    assert (
        phases.ScaleProfile.resolve(scale, slo).timeout_s
        == slo.ladder.attack_timeout_seconds
    )


def test_moving_the_spec_value_moves_the_attack_timeout(raw_slo):
    """The anti-drift property: a spec edit must change what vegeta is passed."""
    edited = {**raw_slo, "ladder": {**raw_slo["ladder"], "attack_timeout_seconds": 7.5}}
    slo = SloConfig.model_validate(edited)
    assert phases.ScaleProfile.resolve("full", slo).timeout_s == 7.5


# --------------------------------------------------------------------------- #
# posture-resolved warmup gates (B3 / R1-A3 precondition)
# --------------------------------------------------------------------------- #
def test_no_posture_override_is_declared_yet():
    """Deliberately empty: a posture exemption is a MEASURED finding, not a guess.

    Round 1 declared warmup gates that it never implemented (R1-B1). The
    resolver exists so that a posture measured as unable to satisfy a gate has
    a pre-registered home.
    """
    assert load_slo().warmup.gates_by_posture == {}


def test_gates_for_falls_back_to_the_global_set():
    warm = load_slo().warmup
    assert warm.gates_for("headline.mixed.gc-default") == warm.gates
    assert warm.gates_for("a-cell-that-does-not-exist") == warm.gates


def test_a_posture_override_merges_over_the_global_set_and_never_replaces_it():
    """A full-set override would silently unenforce any gate added after it."""
    warm = load_slo().warmup.model_copy(
        update={
            "gates": {"p99_flat": True, "gc_steady": True},
            "gates_by_posture": {"headline.mixed.gc-generous": PostureWaiver(
                gates={"gc_steady": False},
                rationale="SYNTHETIC: exercising the merge, not a declared waiver",
            )},
        }
    )
    assert warm.gates_for("headline.mixed.gc-generous") == {
        "p99_flat": True, "gc_steady": False,
    }
    assert warm.gates_for("headline.mixed.gc-default") == {
        "p99_flat": True, "gc_steady": True,
    }


def test_the_override_does_not_mutate_the_global_set():
    warm = load_slo().warmup.model_copy(
        update={"gates_by_posture": {"c": PostureWaiver(
            gates={"pools_warm": False}, rationale="SYNTHETIC",
        )}}
    )
    before = dict(warm.gates)
    warm.gates_for("c")
    assert warm.gates == before


def test_a_spec_posture_waiver_must_state_what_it_rests_on(raw_slo):
    """The spec layer is made at least as strict as the code layer.

    `phases.Cell` refuses a `warmup_gates` override with no `gate_set_rationale`
    because a stated reason is the only thing separating a waiver from R1-A3's
    ~30 h against a gate that could not pass. The same declaration made HERE is
    inside `config_hash` and is the harder one to notice, and it carried no such
    requirement at all.
    """
    edited = {**raw_slo, "warmup": {**raw_slo["warmup"], "gates_by_posture": {
        "headline.mixed.gc-generous": {"gates": {"gc_steady": False},
                                       "rationale": "   "},
    }}}
    with pytest.raises(ValidationError, match="no rationale"):
        SloConfig.model_validate(edited)


def test_a_spec_posture_waiver_that_overrides_no_gate_is_refused(raw_slo):
    """An empty override reads as an exemption and waives nothing."""
    edited = {**raw_slo, "warmup": {**raw_slo["warmup"], "gates_by_posture": {
        "headline.mixed.gc-generous": {"gates": {}, "rationale": "SYNTHETIC"},
    }}}
    with pytest.raises(ValidationError, match="overrides no gate"):
        SloConfig.model_validate(edited)


def test_a_spec_posture_waiver_for_a_gate_the_spec_never_declared_is_refused(raw_slo):
    """A misspelt gate name is otherwise a completely silent no-op.

    `{"gc_stedy": false}` binds to nothing, so `gc_steady` stays ENFORCED while
    the spec reads as an exemption and the report renders the cell as having
    declared one, which is the R1-A3 failure mode.
    """
    edited = {**raw_slo, "warmup": {**raw_slo["warmup"], "gates_by_posture": {
        "headline.mixed.gc-generous": {"gates": {"gc_stedy": False},
                                       "rationale": "SYNTHETIC: misspelt gate"},
    }}}
    with pytest.raises(ValidationError, match="gc_stedy"):
        SloConfig.model_validate(edited)

    ok = {**raw_slo, "warmup": {**raw_slo["warmup"], "gates_by_posture": {
        "headline.mixed.gc-generous": {"gates": {"gc_steady": False},
                                       "rationale": "SYNTHETIC: spelt correctly"},
    }}}
    warm = SloConfig.model_validate(ok).warmup
    assert warm.gates_for("headline.mixed.gc-generous")["gc_steady"] is False
    assert warm.posture_rationale("headline.mixed.gc-generous") == (
        "SYNTHETIC: spelt correctly"
    )
    assert warm.posture_rationale("headline.mixed.gc-default") == ""


def test_a_misspelt_waiver_BLOCK_name_is_refused_not_dropped(raw_slo):
    """The last silent typo class in the waiver path.

    A misspelt gate name, a misspelt cell id, a missing rationale and a stray key
    inside the waiver were each already refused. `gates_by_postures:` was not:
    `WarmupCfg` ignored the extra key, so the waiver bound to nothing, the gate
    stayed ENFORCED, and `config_hash` still changed -- the run looks
    deliberately re-specified and is not.
    """
    edited = {**raw_slo, "warmup": {**raw_slo["warmup"], "gates_by_postures": {
        "headline.mixed.gc-generous": {"gates": {"gc_steady": False},
                                       "rationale": "SYNTHETIC: misspelt block"},
    }}}
    with pytest.raises(ValidationError, match="gates_by_postures"):
        SloConfig.model_validate(edited)

    # the real file still loads: the flip declares every key warmup carries
    assert load_slo().warmup.hard_cap_seconds > 0


# --------------------------------------------------------------------------- #
# a spec COMMENT is a declaration too, and spec/ is config-hashed
# --------------------------------------------------------------------------- #
def test_the_confirm_reps_comment_names_the_rate_confirm_actually_runs_at():
    """`confirm_reps` sizes the confirm stage; its comment says WHERE those blocks sit.

    It read "randomized complete blocks at bracket rates" after W24 moved confirm
    off the bracket rungs to each slot's fitted knee (`run_fit` persists
    `confirm_rate_basis="fitted_knee"`, and `_confirm_members` reads
    `confirm_rates_by_slot`). A false comment in the config-hashed spec is the
    same defect class as a false sentence in the report: nothing enforced it, so
    nothing caught it.
    """
    line = next(
        ln for ln in paths.slo_path().read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("confirm_reps:")
    )
    assert "bracket" not in line, line
    assert "fitted knee" in line, line
    # and the code really does put them there
    src = (paths.bench_dir() / "ledgerbench" / "phases.py").read_text(encoding="utf-8")
    assert 'confirm_rate_basis="fitted_knee" if not no_crossing else "mixed"' in src


# --------------------------------------------------------------------------- #
# W29: the ladder rung ceiling, declared instead of hard-coded
# --------------------------------------------------------------------------- #
def test_the_rung_ceiling_is_declared_and_the_full_tier_obeys_it(raw_slo):
    """It decides whether a non-crossing cell is a finding or a ran-out-of-rungs.

    It was a literal in `phases.ScaleProfile.full`, so the number that bounds
    every ladder was outside `config_hash` and a run could not evidence it.
    """
    assert "rung_ceiling" in raw_slo["ladder"]
    slo = load_slo()
    assert phases.ScaleProfile.resolve("full", slo).ladder_max_rungs == slo.ladder.rung_ceiling


def test_moving_the_rung_ceiling_moves_the_full_ladder(raw_slo):
    """The anti-drift property, the same one the attack timeout carries."""
    edited = {**raw_slo, "ladder": {**raw_slo["ladder"], "rung_ceiling": 17}}
    slo = SloConfig.model_validate(edited)
    assert phases.ScaleProfile.resolve("full", slo).ladder_max_rungs == 17


def test_the_reduced_tiers_deviate_from_the_ceiling_deliberately():
    """They must NOT silently inherit it, and the deviation must be argued.

    `rehearsal` climbs at 2.0 rather than the spec's 1.2, so forty rungs would
    be absurd; `smoke` is functional. Both deviations are stated at their
    ScaleProfile, and this asserts the deviation exists rather than that some
    future edit quietly wired the spec value in and broke the tier.
    """
    slo = load_slo()
    for scale in ("smoke", "rehearsal"):
        assert phases.ScaleProfile.resolve(scale, slo).ladder_max_rungs < slo.ladder.rung_ceiling
    src = (paths.bench_dir() / "ledgerbench" / "phases.py").read_text(encoding="utf-8")
    assert "Deviates from spec's `ladder.rung_ceiling`" in src


# --------------------------------------------------------------------------- #
# W29: a declared gate must be able to DECIDE, not merely to run
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scale", ["full", "rehearsal", "smoke"])
def test_no_declared_warmup_gate_is_inert_at_the_rate_this_tier_warms_at(scale):
    """The governing rule's other half: not "is it implemented" but "can it flag".

    `p999_flat` was implemented, tested, declared, and evaluated on every probe
    -- and enforced on none of them, because nearest-rank q=0.999 over a
    one-second bucket needs `MIN_SAMPLES_PER_SECOND_P999` samples and no warmup
    rate this harness offers reaches it. That is R1-B1's shape with the
    implementation present, so the spec must not declare such a gate. The rate
    is taken from the runner's own `_warmup_rate`, not restated here.
    """
    slo = load_slo()
    profile = phases.ScaleProfile.resolve(scale, slo)
    runner = LiveProbeRunner(run_id="slo-cfg", profile=profile,
                             workload=load_workload(), slo=slo, bench_overlay=True)
    rate = runner._warmup_rate(runner._scaled_min_calls(), profile.warmup_cap_s)
    floors = {g.gate: g.min_samples_per_second for g in warmup.FLATNESS_GATES}

    for gate, enabled in slo.warmup.gates.items():
        if enabled and gate in floors:
            assert rate >= floors[gate], (
                f"{scale}: {gate} is declared but cannot resolve its quantile at "
                f"{rate} rps (floor {floors[gate]} samples/s)"
            )
    # ...and the measurement that keeps `p999_flat` out, rather than a memory of it
    assert "p999_flat" not in slo.warmup.gates
    assert rate < floors["p999_flat"]


def test_the_p999_statistic_the_spec_points_at_is_the_one_the_runner_writes():
    """The spec says p999 is recorded rather than gated; this pins WHERE.

    A retired gate whose statistic is asserted to live in a file nobody writes
    is the deletion done dishonestly.
    """
    text = paths.slo_path().read_text(encoding="utf-8")
    assert "<slot>.warmup_latency_per_second.csv" in text
    assert "segment,second,p99_ms,p999_ms,samples" in text
    src = (paths.bench_dir() / "ledgerbench" / "live_runner.py").read_text(encoding="utf-8")
    assert 'f"{lang}.warmup_latency_per_second.csv"' in src
    header = (paths.bench_dir() / "ledgerbench" / "vegeta.py").read_text(encoding="utf-8")
    assert 'head = "segment,second,p99_ms,p999_ms,samples"' in header


# --------------------------------------------------------------------------- #
# W29: the gc_steady comment is a declaration of WHICH BEANS, and is hashed
# --------------------------------------------------------------------------- #
def test_the_gc_steady_comment_names_the_bean_sets_the_registry_declares():
    """`spec/` freezes the bean names; the registry is what warmup.py scores.

    A comment naming a different set than `GcSemantics.cadence_beans` would be a
    frozen, config-hashed claim about a series nothing computes.
    """
    text = paths.slo_path().read_text(encoding="utf-8")
    for bean in registry.JVM_GC.cadence_beans:
        assert f'"{bean}"' in text, bean
    # the two measured exclusions, named as exclusions rather than omitted
    assert "G1 Concurrent GC" in text
    assert '"*Pauses"' in text
    # the CLR half, which has no bean list because its triple IS the source
    assert registry.CLR_GC.cadence_beans == ()
    assert "gen0 + gen1 + gen2" in text
    # and the runner records which basis a probe used
    assert "gc_steady.detail.cadence_basis" in text
    src = (paths.bench_dir() / "ledgerbench" / "warmup.py").read_text(encoding="utf-8")
    assert '"cadence_basis": sorted(set(self.gc_cadence_basis_series))' in src


# --------------------------------------------------------------------------- #
# W29: soak declares only what code reads
# --------------------------------------------------------------------------- #
def test_the_soak_block_declares_only_the_key_the_code_reads(raw_slo):
    """`pass_criteria` held four sub-keys no code evaluated, and they read as gates."""
    assert set(raw_slo["soak"]) == {"duration_seconds"}
    slo = load_slo()
    assert phases.ScaleProfile.resolve("full", slo).soak_s == raw_slo["soak"]["duration_seconds"]


# --------------------------------------------------------------------------- #
# W30: extra="forbid" across the SLO tree -- the governing rule at load time
# --------------------------------------------------------------------------- #
def test_a_gate_added_to_the_spec_with_no_field_behind_it_is_refused_at_load(tmp_path):
    """A spec key with no model field is refused at load (R1-B1).

    Under `extra="ignore"` this file loaded silently: `warmup.bogus_gate` bound to
    nothing, was enforced by nothing, changed `config_hash` (so the run looked
    deliberately re-specified), and was echoed into the manifest's spec section as
    pre-registered method. The load is the only place that can refuse it, because
    every later consumer reads fields rather than the YAML.

    Written against a real file through `load_slo`, not a dict, so it exercises
    the path `doctor`, `status` and `run-all` all take.
    """
    text = paths.slo_path().read_text(encoding="utf-8")
    edited = text.replace("warmup:\n", "warmup:\n  bogus_gate: true\n", 1)
    assert edited != text
    spec = tmp_path / "slo.yaml"
    spec.write_text(edited, encoding="utf-8")

    with pytest.raises(ValidationError, match="bogus_gate"):
        load_slo(spec)


# One entry per `extra="forbid"` site in the SLO tree. A model flipped back to
# `ignore` -- or a new block added under `extra="ignore"` -- fails here.
_STRAY_KEY_SITES = [
    ("(top level)", lambda raw: {**raw, "bogus_block": {"x": 1}}),
    ("slo", lambda raw: {**raw, "slo": {**raw["slo"], "bogus": 1}}),
    ("equivalence",
     lambda raw: {**raw, "equivalence": {**raw["equivalence"], "bogus": 1}}),
    ("ladder", lambda raw: {**raw, "ladder": {**raw["ladder"], "bogus": 1}}),
    ("warmup", lambda raw: {**raw, "warmup": {**raw["warmup"], "bogus": 1}}),
    ("co_validation",
     lambda raw: {**raw, "co_validation": {**raw["co_validation"], "bogus": 1}}),
    ("co_validation.stall_test",
     lambda raw: {**raw, "co_validation": {
         **raw["co_validation"],
         "stall_test": {**raw["co_validation"]["stall_test"], "bogus": 1}}}),
    ("capacity_gate",
     lambda raw: {**raw, "capacity_gate": {**raw["capacity_gate"], "bogus": 1}}),
    ("capacity_gate.levers_in_order[]",
     lambda raw: {**raw, "capacity_gate": {
         **raw["capacity_gate"],
         "levers_in_order": [{**raw["capacity_gate"]["levers_in_order"][0],
                              "bogus": 1}]
         + raw["capacity_gate"]["levers_in_order"][1:]}}),
    # `warmup.gates_by_posture` is EMPTY in the frozen spec, so this is the one
    # forbid site the real file cannot reach -- which is exactly why it was the
    # only one no parameter covered: reverting `PostureWaiver` to `extra=ignore`
    # left the module green. It is also the site where an ignored key is worst.
    # A waiver's whole job is to say "this posture cannot satisfy this gate, and
    # here is why"; a typo inside it (`gates` misspelt, `rationale` misspelt)
    # under `ignore` yields a waiver that binds nothing, leaves the gate
    # ENFORCED, and still changes `config_hash` -- R1-A3's ~30 wasted hours with
    # a pre-registered exemption on the ledger saying it could not happen.
    ("warmup.gates_by_posture[].<waiver>",
     lambda raw: {**raw, "warmup": {
         **raw["warmup"],
         "gates_by_posture": {"headline.mixed.gc-generous": {
             "gates": {"gc_steady": False},
             "rationale": "placeholder for the stray-key check",
             "bogus": 1}}}}),
]


@pytest.mark.parametrize("site,inject", _STRAY_KEY_SITES,
                         ids=[s for s, _ in _STRAY_KEY_SITES])
def test_no_block_of_the_slo_tree_accepts_a_key_it_does_not_declare(raw_slo, site, inject):
    with pytest.raises(ValidationError, match="bogus"):
        SloConfig.model_validate(inject(raw_slo))


def test_the_capacity_lever_from_alias_survives_the_flip(raw_slo):
    """`from` is a Python keyword, so the field is `from_` with an alias.

    `extra="forbid"` plus an aliased field is the one combination that can reject
    the project's own spec: if the alias ever stopped being the validation name,
    every lever's `from:` would land as an extra key and `load_slo()` would raise
    on the frozen file. This asserts the value arrives, not merely that it loads.
    """
    lever = load_slo().capacity_gate.levers_in_order[0]
    assert lever.name == "write_share"
    assert lever.from_ == raw_slo["capacity_gate"]["levers_in_order"][0]["from"]
    with pytest.raises(ValidationError, match="frm"):
        SloConfig.model_validate({**raw_slo, "capacity_gate": {
            **raw_slo["capacity_gate"],
            "levers_in_order": [{"name": "write_share", "frm": 0.15, "to": 0.10}],
        }})


# The reverse direction. `forbid` catches a spec key with no field; nothing in
# pydantic catches a field with no spec key, and that field is just as much a
# declaration -- an OPTIONAL one loads forever against a spec that stopped
# carrying it. `ladder.start_rate_strategy` was exactly that at P1: a field for a
# key removed from the spec, read by no code, echoed into the manifest as None.
_BLOCK_FIELDS = [
    ("slo", lambda c: c.slo),
    ("equivalence", lambda c: c.equivalence),
    ("ladder", lambda c: c.ladder),
    ("co_validation", lambda c: c.co_validation),
    ("capacity_gate", lambda c: c.capacity_gate),
]


@pytest.mark.parametrize("block,get", _BLOCK_FIELDS, ids=[b for b, _ in _BLOCK_FIELDS])
def test_every_field_in_the_slo_tree_has_a_key_in_the_frozen_spec(raw_slo, block, get):
    assert set(type(get(load_slo())).model_fields) == set(raw_slo[block])


def test_the_warmup_blocks_only_spec_absent_field_is_the_posture_waiver_map(raw_slo):
    """`gates_by_posture` is deliberately unused and deliberately declared.

    It is the one field in the tree with no key in the frozen spec: the resolver
    exists so that a posture measured as unable to satisfy a gate lands in the
    pre-registered spec instead of a disclosure note. Pinned here so it stays
    the ONLY one, and so its emptiness stays a stated decision rather than a
    field nobody noticed.
    """
    from ledgerbench.config import WarmupCfg

    assert set(WarmupCfg.model_fields) - set(raw_slo["warmup"]) == {"gates_by_posture"}
    assert set(raw_slo["warmup"]) - set(WarmupCfg.model_fields) == set()
    assert load_slo().warmup.gates_by_posture == {}


def test_the_frozen_spec_itself_still_loads_under_forbid():
    """The failure mode a forbid flip actually has: rejecting your own spec.

    Every key in `spec/slo.yaml` reaches a field, and the two blocks kept as raw
    dicts still arrive. If this fails, `doctor`, `status` and `run-all` are down.
    """
    slo = load_slo()
    assert slo.slo.threshold_ms == 20
    assert slo.ladder.knee_fit == "isotonic"
    assert slo.co_validation.stall_test.method == "docker_pause"
    assert slo.capacity_gate.never == "synchronous_commit_off"
    assert slo.stationarity and slo.soak
