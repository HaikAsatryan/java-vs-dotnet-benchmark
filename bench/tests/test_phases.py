"""Phase orchestration + RCB resume logic, docker-free.

These tests drive the cell stage functions (ladder/fit/confirm/soak) and the
ledger resume helpers with a FAKE ProbeRunner, so the orchestration logic is
exercised end-to-end with no daemon. The live docker paths (seed, host-setup,
equivalence) are covered by the crash-resume drill (PROGRESS.md, P6), not here.
"""

from __future__ import annotations

import pytest

import json
import re

from ledgerbench import capacity_gate as capgate
from ledgerbench import cgroups, compose, hostread
from ledgerbench import ledger as ledger_mod
from ledgerbench import phases
from ledgerbench.config import PostureWaiver, load_slo, load_workload
from ledgerbench.ledger import Ledger, ProbeRecord, State, probe_key, split_probe_key
from ledgerbench.manifest import HostReading, new_manifest
from ledgerbench.phases import ProbeOutcome, RunContext, ScaleProfile
from ledgerbench.registry import DEFAULT_SLOT_SET, get_slot, get_slot_set
from ledgerbench.util import paths
from ledgerbench.util.proc import ProcResult
from ledgerbench.warmup import IMPLEMENTED_GATES

# What a real probe's `gates_evaluated` looks like: the implemented gates plus
# the separate per-endpoint minimum, which is not one of spec/slo.yaml's.
_EVALUATED_GATES = sorted({*IMPLEMENTED_GATES, "per_endpoint_min_calls"})

# Registry-driven, never a literal: these are the default slot set's two slots.
SLOTS = get_slot_set(DEFAULT_SLOT_SET).slots
SLOT_A, SLOT_B = SLOTS


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    # git-backed: `run_all` ends in `checksums.write_run_checksums`, whose scope
    # is `git ls-files`' answer inside the run directory.
    from tests.conftest import git_backed_results

    return git_backed_results(tmp_path, monkeypatch)


class FakeRunner:
    """Deterministic, no-docker probe runner.

    `crossing` controls whether the synthetic per-second p99 series crosses the
    SLO: when False (smoke default) the series sits at ~5 ms (sub-knee) so the
    ladder never brackets a crossing; the no-crossing path. When True, p99 grows
    with the rate so a crossing is bracketed.
    """

    def __init__(self, *, crossing: bool = False, fail_on=None, under_warm_on=None):
        self.calls = 0
        self.crossing = crossing
        self.fail_on = fail_on or set()        # set of (lang, rate) to fail
        # set of (lang, rate) to flag under-warmed (excluded from the fit)
        self.under_warm_on = under_warm_on or set()
        self.probe_log: list[tuple[str, str, int, int]] = []

    def ensure_body_pool(self, cell):  # noqa: D401
        pass

    def run_probe(self, *, cell, stage, lang, rate, block, duration_s, warmup_cap_s, artifact_dir):
        self.calls += 1
        self.probe_log.append((stage, lang, rate, block))
        if (lang, rate) in self.fail_on:
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail="injected failure")
        if self.crossing:
            base = 4.0 + (rate / 100.0)        # grows with rate -> eventually >20
        else:
            base = 5.0
        # The per-second series sits well BELOW the PROBE p99 so a test that
        # consumed the series instead of p99_ms would mis-bracket: this guards the
        # "probe p99 drives the fit, not percentile-of-percentiles" contract.
        series = [1.0 + (i % 3) * 0.05 for i in range(10)]
        return ProbeOutcome(
            achieved=rate * 0.999, error_rate=0.0, p99_ms=base,
            artifact=f"cells/{cell.cell_id}/{stage}/{lang}.json",
            per_second_p99=series, ok=True,
            gates_evaluated=list(_EVALUATED_GATES),
            # The flag is expressed the way the runner expresses it: by naming
            # the gate that failed. `under_warmed` is derived from this list.
            gates_failed=(["p99_flat"] if (lang, rate) in self.under_warm_on else []),
        )


def _ctx(run_id, scale="smoke", runner=None, slot_set=DEFAULT_SLOT_SET) -> RunContext:
    wl = load_workload()
    slo = load_slo()
    profile = ScaleProfile.resolve(scale, slo)
    led = Ledger.load_or_create(run_id, slot_set=slot_set, config_hash="testhash",
                                rng_seed=wl.seeding.seed)
    return RunContext(
        led=led, profile=profile, workload=wl, slo=slo,
        manifest=new_manifest(run_id), runner=runner or FakeRunner(), run_id=run_id,
    )


_GC_DEFAULT = next(c for c in phases.CELLS if c.cell_id == "headline.mixed.gc-default")


# --------------------------------------------------------------------------- #
# smoke-params resolution
# --------------------------------------------------------------------------- #
def test_smoke_profile_is_functional_rehearsal():
    p = ScaleProfile.resolve("smoke", load_slo())
    assert p.functional_only is True
    assert p.warmup_cap_s == 15
    assert p.measure_s == 10
    assert p.ladder_start_rps == 100
    assert p.ladder_step_factor == 2.0
    assert p.ladder_max_rungs == 3
    assert p.ladder_reps_per_rung == 2
    assert p.confirm_blocks == 2
    assert p.soak_s == 20
    assert p.cells_filter == {"headline.mixed.gc-default"}


def test_full_profile_reads_slo_verbatim():
    slo = load_slo()
    p = ScaleProfile.resolve("full", slo)
    assert p.functional_only is False
    assert p.warmup_cap_s == slo.warmup.hard_cap_seconds          # 300
    assert p.ladder_start_rps == slo.ladder.start_rate_rps_default  # 500
    assert p.ladder_step_factor == slo.ladder.step_factor          # 1.2
    assert p.ladder_reps_per_rung == slo.ladder.reps_per_rung      # 3
    assert p.confirm_blocks == slo.ladder.confirm_reps             # 10
    assert p.cells_filter is None


def test_smoke_runs_only_gc_default_cell():
    p = ScaleProfile.resolve("smoke", load_slo())
    ids = [c.cell_id for c in phases.cells_for(p)]
    assert ids == ["headline.mixed.gc-default"]


def test_full_runs_the_single_headline_cell():
    """ONE headline cell since G6 decided D6 (heap committed peaked at 298 MB
    against the ~3 GiB default ceiling; the equalized cell was deleted under
    D6's own one-cell-defaults-done rule)."""
    p = ScaleProfile.resolve("full", load_slo())
    ids = [c.cell_id for c in phases.cells_for(p)]
    assert ids == ["headline.mixed.gc-default"]


def test_unknown_scale_rejected():
    with pytest.raises(ValueError, match="unknown scale"):
        ScaleProfile.resolve("medium", load_slo())


# --------------------------------------------------------------------------- #
# no-crossing ladder path
# --------------------------------------------------------------------------- #
def test_no_crossing_ladder_then_fit_skips_math(isolated_results):
    ctx = _ctx("nc-1", runner=FakeRunner(crossing=False))
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert lad.state == State.DONE
    assert all(v is None for v in lad.data["crossed_by_slot"].values())
    # each slot's bracket is its single top rung
    assert all(len(b) == 1 for b in lad.data["bracket_by_slot"].values())
    assert sorted(lad.data["ceiling_reached_without_crossing"]) == sorted(SLOTS)

    phases.run_fit(ctx, _GC_DEFAULT)
    fit = ctx.led.stage(_GC_DEFAULT.cell_id, "fit")
    assert fit.state == State.DONE
    assert sorted(fit.data["no_crossing_slots"]) == sorted(SLOTS)
    # confirm runs at each slot's top rung (its single-element bracket)
    assert {sid: [r] for sid, r in fit.data["confirm_rates_by_slot"].items()} \
        == lad.data["bracket_by_slot"]


def test_crossing_ladder_brackets_two_rungs(isolated_results):
    # crossing=True grows p99 with rate; with start=100 step=2 the third rung
    # (400 rps) p99 = 4 + 4 = 8ms still < 20 at smoke (3 rungs), so the profile
    # is widened below until a crossing occurs.
    ctx = _ctx("cr-1", runner=FakeRunner(crossing=True))
    # widen the ladder so a crossing actually occurs within range
    object.__setattr__(ctx.profile, "ladder_max_rungs", 12)
    object.__setattr__(ctx.profile, "ladder_step_factor", 1.6)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert all(v is not None for v in lad.data["crossed_by_slot"].values())
    assert all(len(b) == 2 for b in lad.data["bracket_by_slot"].values())

    phases.run_fit(ctx, _GC_DEFAULT)
    fit = ctx.led.stage(_GC_DEFAULT.cell_id, "fit")
    assert fit.data["no_crossing_slots"] == []
    assert all(fit.data["knee_by_slot"][sid] is not None for sid in SLOTS)
    # ladder reps (2/rung at smoke) < confirm_reps(10) -> CI flagged invalid
    assert all(fit.data["ci_valid_by_slot"][sid] is False for sid in SLOTS)


# --------------------------------------------------------------------------- #
# W24 / B1 / R1-A1 / D9: the ladder climbs PER SLOT
# --------------------------------------------------------------------------- #
class PerSlotRunner(FakeRunner):
    """p99 that crosses at a DIFFERENT rate for each slot.

    SLOT_A crosses at 400 rps; SLOT_B not until 3200. With the pooled rung mean
    the ladder stopped for BOTH the moment the mixed mean crossed, so every rung
    above SLOT_A's knee -- exactly the range SLOT_B's own knee lives in -- was
    never offered.
    """

    def __init__(self, *, knee_by_slot):
        super().__init__(crossing=True)
        self.knee_by_slot = knee_by_slot

    def run_probe(self, **kw):
        out = super().run_probe(**kw)
        knee = self.knee_by_slot[kw["lang"]]
        out.p99_ms = 5.0 if kw["rate"] < knee else 40.0
        return out


def _wide_ladder_ctx(run_id, runner, *, rungs=8, step=2.0):
    ctx = _ctx(run_id, runner=runner)
    object.__setattr__(ctx.profile, "ladder_max_rungs", rungs)
    object.__setattr__(ctx.profile, "ladder_step_factor", step)
    return ctx


def test_the_ladder_keeps_climbing_for_the_slot_that_has_not_crossed(isolated_results):
    """THE R1-A1 REGRESSION GUARD.

    Round 1's headline was invalidated because the ladder stopped when the POOLED
    rung mean crossed. The stack that degraded slower was then measured nowhere
    near its own knee, and no later analysis could recover the rungs the run
    never offered.
    """
    runner = PerSlotRunner(knee_by_slot={SLOT_A: 400, SLOT_B: 3200})
    ctx = _wide_ladder_ctx("split-1", runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")

    rates_by_slot = {sid: sorted({p.rate for p in lad.probes if p.slot == sid})
                     for sid in SLOTS}
    # SLOT_A stops at its own crossing rung; SLOT_B keeps climbing past it.
    assert max(rates_by_slot[SLOT_A]) == 400
    assert max(rates_by_slot[SLOT_B]) == 3200
    assert [r for r in rates_by_slot[SLOT_B] if r > 400] == [800, 1600, 3200]
    # ...and the rungs above SLOT_A's knee are SOLO blocks, which nothing stamps:
    # `len(slot_order) > 1` IS the statement and cannot disagree with the record.
    solo = [p for p in lad.probes if p.rate > 400]
    assert solo and all(p.slot_order == [SLOT_B] for p in solo)
    # both knees are named, per slot
    assert lad.data["crossed_by_slot"] == {SLOT_A: 400, SLOT_B: 3200}
    assert lad.data["ceiling_reached_without_crossing"] == []
    assert lad.data["bracket_by_slot"][SLOT_A] == [200, 400]
    assert lad.data["bracket_by_slot"][SLOT_B] == [1600, 3200]
    # The persisted ceiling is the top rung ACTUALLY OFFERED. The 8-rung plan
    # runs to 12,800 rps and this ladder stopped at 3,200 because both slots had
    # crossed, so `rates[-1]` would persist a rate no probe ever ran at.
    assert lad.data["rung_ceiling_rps"] == 3200
    assert max(p.rate for p in lad.probes) == lad.data["rung_ceiling_rps"]


def test_a_solo_rung_is_not_reported_as_emptied_by_exclusion(isolated_results):
    """The trap that would abort every split run.

    `seen_rates - by_rate.keys()` is the "emptied by exclusion" set the fit raises
    on and the retry re-runs. After the ladder splits, every solo rung carries
    zero probes of the slot that stopped -- so if the slot filter were applied
    AFTER `seen_rates.add`, those rungs would be reported as emptied for a slot
    that was never planned at them, the retry would fire, and the run would abort
    naming under-warming as the cause.
    """
    runner = PerSlotRunner(knee_by_slot={SLOT_A: 400, SLOT_B: 3200})
    ctx = _wide_ladder_ctx("split-2", runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")

    for sid in SLOTS:
        by_rate, empty = phases._rung_values(lad, sid)
        assert empty == set(), (sid, empty)
        phases._rungs_by_rate(lad, sid)          # must not raise
    assert "rewarmed_rungs" not in lad.data
    # the solo rungs are in SLOT_B's fit input and absent from SLOT_A's
    assert 3200 in phases._rungs_by_rate(lad, SLOT_B)
    assert 3200 not in phases._rungs_by_rate(lad, SLOT_A)


def test_ladder_block_ids_are_unique_within_the_stage(isolated_results):
    """Two rungs may not share a block id, and a resume is what makes it matter.

    Since W24 both `_execute_block_probes` and `reset_orphan_blocks` group probes
    by BLOCK ID ALONE -- they must, or a mixed-rate confirm block splits into
    singleton groups and a crash that halves it is never repaired. That makes
    `plan_blocks(block_offset=...)` load-bearing: without it every rung restarts
    at block 0, so rung 1's pending block 0 and rung 0's DONE block 0 become one
    group, `0 < done < members` fires, and the resume RESETS A RUNG THAT WAS
    ALREADY MEASURED -- discarding good windows and re-running hours of probes.
    """
    runner = FakeRunner(crossing=False)
    ctx = _wide_ladder_ctx("blockid-1", runner, rungs=3)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")

    rates = sorted({p.rate for p in lad.probes})
    assert len(rates) == 3
    blocks_by_rate = {r: {p.block for p in lad.probes if p.rate == r} for r in rates}
    seen: set[int] = set()
    for r in rates:
        assert not (blocks_by_rate[r] & seen), f"rung {r} reuses block id(s)"
        seen |= blocks_by_rate[r]
    # and the consequence, driven through the resume path itself: a completed
    # ladder has no orphan, so a resume must touch nothing.
    assert ctx.led.reset_orphan_blocks(_GC_DEFAULT.cell_id, "ladder") == []
    assert all(p.state == State.DONE for p in lad.probes)


def test_a_half_run_rung_never_resets_a_rung_that_finished(isolated_results):
    """The same defect from the other side: a PENDING rung must not drag a DONE
    one back to pending through a shared block id."""
    ctx = _wide_ladder_ctx("blockid-2", FakeRunner(crossing=False), rungs=3)
    cid = _GC_DEFAULT.cell_id
    rec = ctx.led.stage(cid, "ladder")
    n = ctx.profile.ladder_reps_per_rung
    for ri, rate in enumerate((100, 200)):
        for p in phases.plan_blocks(n_blocks=n, rate=rate, rng_seed=ctx.led.rng_seed,
                                    salt=phases._salt(cid, "ladder", ri),
                                    slots=ctx.slots, block_offset=ri * n):
            rec.probes.append(p)
    for p in rec.probes:
        if p.rate == 100:
            p.state = State.DONE                 # rung 100 finished; rung 200 never ran
    ctx.led.save()

    assert ctx.led.reset_orphan_blocks(cid, "ladder") == []
    assert all(p.state == State.DONE for p in rec.probes if p.rate == 100)


def test_the_crossing_check_ignores_a_rung_with_a_pending_probe(isolated_results):
    """RESUME BUG 1 -- the R1-A4 mechanism.

    `reset_orphan_blocks` only resets a block with 0 < done < members, so a rung
    whose blocks 0-1 are fully DONE and whose block 2 is fully PENDING survives a
    crash untouched and looks complete. Its mean is then taken over 4 probes
    where 6 were planned, and if THAT partial mean crosses, the ladder breaks,
    block 2 stays PENDING, and the stage is marked DONE anyway -- an under-repped
    crossing bracket, published.
    """
    runner = FakeRunner(crossing=False)
    ctx = _wide_ladder_ctx("resume-underrep", runner, rungs=4)
    object.__setattr__(ctx.profile, "ladder_reps_per_rung", 3)
    cid = _GC_DEFAULT.cell_id
    rec = ctx.led.stage(cid, "ladder")
    # A resumed rung 0: blocks 0-1 done and OVER the SLO, block 2 never run.
    for p in phases.plan_blocks(n_blocks=3, rate=100, rng_seed=ctx.led.rng_seed,
                                salt=phases._salt(cid, "ladder", 0), slots=ctx.slots):
        rec.probes.append(p)
    for p in rec.probes:
        if p.block < 2:
            p.state = State.DONE
            rec.data.setdefault("p99_ms", {})[probe_key(p.rate, p.block, p.slot)] = 40.0
            rec.data.setdefault("under_warmed", {})[probe_key(p.rate, p.block, p.slot)] = False
    ctx.led.mark_stage(cid, "ladder", State.RUNNING, rates=[100])
    ctx.allow_sub_knee_ladder = True

    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(cid, "ladder")
    assert lad.state == State.DONE
    # the third block RAN; no probe is left behind under a DONE stage
    assert all(p.state == State.DONE for p in lad.probes)
    assert len([p for p in lad.probes if p.rate == 100 and p.slot == SLOT_A]) == 3
    assert {("ladder", SLOT_A, 100, 2), ("ladder", SLOT_B, 100, 2)} <= set(runner.probe_log)


def test_a_rung_emptied_for_ONE_slot_is_retried_then_recorded_not_fatal(
    isolated_results,
):
    """Per-slot holes: retried once, then RECORDED rather than aborting the cell.

    The pooled rule never retried a rung emptied for one slot at all -- a warm
    probe from the other slot kept it "alive" and `analyze._knee_for_slot` then
    dropped it silently into `n_rungs_dropped`. Retrying is right; ABORTING is
    not: with 3 reps a rung, a per-slot hole is a p^3 event per rung per slot,
    which at round 1's 59.5% flagging rate is roughly one rung in five, so a
    raise would make a 15-hour cell hostage to the warmup gates.

    A rung with no warm probe from ANY slot that ran there is different -- it
    produced no measurement at all -- and still raises.
    """
    runner = FakeRunner(crossing=False, under_warm_on={(SLOT_A, 100)})
    ctx = _ctx("hole-1", runner=runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")

    assert lad.state == State.DONE
    # retried ONCE, keyed by (slot, rate) so the other slot's budget is intact
    assert lad.data["rewarmed_rungs"] == [f"{SLOT_A}@100"]
    # ...and then recorded, not raised
    assert lad.data["dropped_rungs_by_slot"] == {SLOT_A: [100]}
    assert 100 not in phases._rungs_by_rate(lad, SLOT_A)
    assert 100 in phases._rungs_by_rate(lad, SLOT_B)


def test_a_rung_no_slot_kept_a_warm_probe_at_still_raises(isolated_results):
    """The hole that is a hole for everyone: that rung measured nothing."""
    runner = FakeRunner(crossing=False,
                        under_warm_on={(SLOT_A, 100), (SLOT_B, 100)})
    ctx = _ctx("hole-2", runner=runner)
    with pytest.raises(phases.PhaseError, match="under-warmed/excluded after a retry"):
        phases.run_ladder(ctx, _GC_DEFAULT)


def test_the_ladder_stop_rule_is_the_published_fit(isolated_results):
    """ONE crossing predicate, on a sequence where the two old ones differ.

    The raw scan stops at the first rung whose OWN mean reaches the SLO; the
    isotonic fit stops where the PAVA-pooled fit does. They agree while the
    sequence is monotone, so only a deliberate dip shows the difference -- and
    nothing in the suite had one.
    """
    from analysis.knee import crosses

    # A dip: rung 2 spikes over the SLO, rung 3 falls back under it.
    rungs = [(1000, 5.0), (2000, 25.0), (3000, 8.0), (4000, 30.0)]
    raw_first = next(r for r, p in rungs if p >= 20.0)
    point, bracket = crosses(rungs, 20.0)
    # the raw scan would have stopped at 2000; the isotonic fit does not.
    assert raw_first == 2000
    assert bracket == [3000.0, 4000.0]
    assert 3000 < point <= 4000
    # and `phases._bracket_crossing` -- the ladder's stop rule -- is that fit.
    assert phases._bracket_crossing(rungs, 20.0) == ([3000, 4000], True)


def test_a_single_rung_ladder_is_a_clean_error(isolated_results, monkeypatch):
    """Rung 0 already over the SLO: a PhaseError naming `ladder_start_rps`.

    Before W24 the ladder broke after one rung and `estimate_knee_from_rungs`
    raised a bare `ValueError("need at least 2 rungs")`, which the CLI's
    PhaseError-only guard let out as a traceback. P8 pre-registers this exact
    case as "knee outside the measured ladder range -> stop and fix
    ladder_start_rps", and it is a live risk at rehearsal scale with a cold
    Spring Boot at 500 rps.
    """
    runner = PerSlotRunner(knee_by_slot={SLOT_A: 1, SLOT_B: 1})   # crosses at rung 0
    ctx = _wide_ladder_ctx("single-rung", runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert sorted({p.rate for p in lad.probes}) == [100]          # one rung only

    with pytest.raises(phases.PhaseError, match="ladder_start_rps"):
        phases.run_fit(ctx, _GC_DEFAULT)

    # ...and it reaches the OPERATOR as a refusal, not a traceback. The CLI's
    # guard catches PhaseError only, so the old bare ValueError from
    # `estimate_knee_from_rungs` escaped it.
    from typer.testing import CliRunner

    from ledgerbench.__main__ import app

    monkeypatch.setattr(phases, "make_context", lambda **kw: ctx)
    res = CliRunner().invoke(
        app, ["confirm", "--run-id", "single-rung", "--scale", "smoke"]
    )
    assert res.exit_code == 1
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "confirm refused" in res.output and "ladder_start_rps" in res.output


def test_a_slot_that_never_crosses_raises_at_measured_scale_and_says_which(
    isolated_results,
):
    """A slot that never crosses is named, and at measured scale it aborts.

    In round 1 Go had no measured knee in any cell and the artifact said
    nothing, so the comparison ran at a rate one stack's worst configuration
    had set. `ceiling_reached_without_crossing` records that outcome, and at
    measured scale the default is to abort rather than record it.
    """
    runner = PerSlotRunner(knee_by_slot={SLOT_A: 200, SLOT_B: 10 ** 9})
    ctx = _wide_ladder_ctx("subknee-1", runner, rungs=4)
    object.__setattr__(ctx.profile, "functional_only", False)
    ctx.led.mark_phase("capacity_gate", State.DONE, verdict="pass")

    with pytest.raises(phases.PhaseError) as exc:
        phases.run_ladder(ctx, _GC_DEFAULT)
    assert SLOT_B in str(exc.value)
    assert "800 rps" in str(exc.value)                 # the rung ceiling, named
    assert phases.ALLOW_SUB_KNEE_LADDER_ENV in str(exc.value)

    # ...and the opt-out records the cell instead of losing it.
    runner2 = PerSlotRunner(knee_by_slot={SLOT_A: 200, SLOT_B: 10 ** 9})
    ctx2 = _wide_ladder_ctx("subknee-2", runner2, rungs=4)
    object.__setattr__(ctx2.profile, "functional_only", False)
    ctx2.led.mark_phase("capacity_gate", State.DONE, verdict="pass")
    ctx2.allow_sub_knee_ladder = True
    phases.run_ladder(ctx2, _GC_DEFAULT)
    lad = ctx2.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert lad.data["ceiling_reached_without_crossing"] == [SLOT_B]
    assert lad.data["crossed_by_slot"] == {SLOT_A: 200, SLOT_B: None}
    assert lad.data["rung_ceiling_rps"] == 800
    assert lad.data["degraded"] is True
    assert lad.data["degraded_allowed_by"] == phases.ALLOW_SUB_KNEE_LADDER_ENV

    # the stamp SURVIVES a resume: the stage is DONE and its data is the ledger's
    reloaded = Ledger.load("subknee-2")
    assert reloaded.stage(_GC_DEFAULT.cell_id, "ladder").data["degraded"] is True


def test_the_env_flag_reaches_the_context(isolated_results, monkeypatch):
    """The opt-out is an ENV flag, exactly like ALLOW_DB_IMPOSED_KNEE."""
    monkeypatch.setenv(phases.ALLOW_SUB_KNEE_LADDER_ENV, "1")
    ctx = phases.make_context(run_id="subknee-env", scale="smoke",
                              runner=FakeRunner(crossing=False))
    assert ctx.allow_sub_knee_ladder is True
    monkeypatch.delenv(phases.ALLOW_SUB_KNEE_LADDER_ENV)
    ctx2 = phases.make_context(run_id="subknee-env2", scale="smoke",
                               runner=FakeRunner(crossing=False))
    assert ctx2.allow_sub_knee_ladder is False


def test_confirm_runs_at_the_fitted_knee_per_slot(isolated_results):
    """The headline metric is cost AT the crossing rate, so that is where it runs.

    Confirming at the bracket rungs would leave the roll-up's `rate <= knee`
    filter admitting only the LOWER one, so the metric would be measured one
    geometric step BELOW the crossing: within 16.7% at full's 1.2 step, within
    2x at rehearsal's 2.0, against a per-request cost round 1 measured at CoV
    19.3% across its ladder.
    """
    runner = PerSlotRunner(knee_by_slot={SLOT_A: 400, SLOT_B: 3200})
    ctx = _wide_ladder_ctx("knee-confirm", runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    phases.run_fit(ctx, _GC_DEFAULT)
    fit = ctx.led.stage(_GC_DEFAULT.cell_id, "fit")

    knees = fit.data["knee_by_slot"]
    rates = fit.data["confirm_rates_by_slot"]
    assert fit.data["confirm_rate_basis"] == "fitted_knee"
    for sid in SLOTS:
        assert rates[sid] == int(round(knees[sid]))
    # each slot's knee is its own, and they differ by an order of magnitude
    assert rates[SLOT_A] < rates[SLOT_B]
    # ...and the confirm blocks are planned at exactly those rates.
    phases.run_confirm(ctx, _GC_DEFAULT)
    conf = ctx.led.stage(_GC_DEFAULT.cell_id, "confirm")
    own = {(p.slot, p.rate) for p in conf.probes}
    assert (SLOT_A, rates[SLOT_A]) in own
    assert (SLOT_B, rates[SLOT_B]) in own


def test_the_common_rate_probe_sits_inside_its_own_block(isolated_results):
    """The common-rate contrast keeps its RCB adjacency, and costs one probe.

    `r_common` is the SLOWER slot's own knee, so that slot's own-knee probes ARE
    the common-rate probes and only the faster slot needs an extra one. It sits
    INSIDE the same block, in the seeded order -- a probe run an hour later under
    a different thermal state is not paired with anything.
    """
    runner = PerSlotRunner(knee_by_slot={SLOT_A: 400, SLOT_B: 3200})
    ctx = _wide_ladder_ctx("common-rate", runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    phases.run_fit(ctx, _GC_DEFAULT)
    phases.run_confirm(ctx, _GC_DEFAULT)

    conf = ctx.led.stage(_GC_DEFAULT.cell_id, "confirm")
    n_blocks = ctx.profile.confirm_blocks
    r_common = conf.data["common_rate_rps"]
    assert r_common == min(conf.data["confirm_rates_by_slot"].values())

    by_block = {}
    for p in conf.probes:
        by_block.setdefault(p.block, []).append(p)
    assert len(by_block) == n_blocks
    for members in by_block.values():
        # THREE members: slow@r_common, fast@its own knee, fast@r_common
        assert len(members) == 3
        assert sum(1 for m in members if m.rate == r_common) == 2
        # the order is a shuffle over MEMBERS, so the duplicated slot's two
        # probes have distinct positions and the record says which ran first
        assert sorted(m.order_index for m in members) == [0, 1, 2]
        for m in members:
            assert m.slot_order[m.order_index] == m.slot
    # exactly `confirm_blocks` extra probes, not a doubling
    extra = [p for p in conf.probes
             if p.slot != min(conf.data["confirm_rates_by_slot"],
                              key=conf.data["confirm_rates_by_slot"].get)
             and p.rate == r_common]
    assert len(extra) == n_blocks


# --------------------------------------------------------------------------- #
# the PROBE p99 (vegeta's one-number report tail for the whole window,
# NOT a percentile of the per-second p99 series) drives
# the SLO crossing + fit; the per-second series is persisted for stationarity.
# --------------------------------------------------------------------------- #
def test_probe_p99_drives_crossing_not_per_second_series(isolated_results):
    ctx = _ctx("probe-p99-1", runner=FakeRunner(crossing=True))
    object.__setattr__(ctx.profile, "ladder_max_rungs", 12)
    object.__setattr__(ctx.profile, "ladder_step_factor", 1.6)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")

    # Both series are persisted: the probe p99_ms AND the per-second series.
    assert lad.data["p99_ms"], "per-probe report p99 must be persisted"
    assert lad.data["p99_series"], "per-second p99 series must still be persisted"
    # The rung means come from the PROBE p99 (4 + rate/100), NOT the ~1ms series.
    rungs = phases._collect_rungs(lad)
    for rate, mean_p99 in rungs:
        assert abs(mean_p99 - (4.0 + rate / 100.0)) < 1e-6
    # The crossing rung is the first whose probe p99 >= 20 (rate >= 1600).
    crossed = [r for r, p in rungs if p >= ctx.slo_ms]
    assert crossed and crossed[0] >= 1600
    # stationarity still reads the per-second series (unchanged input).
    assert "stationarity" not in lad.data  # computed in run_fit, not ladder


def test_per_second_series_not_used_for_fit_input(isolated_results):
    """If the per-second series were the fit input the rungs would sit at ~1ms and
    never cross; the probe p99 must be what brackets the SLO."""
    ctx = _ctx("probe-p99-2", runner=FakeRunner(crossing=True))
    object.__setattr__(ctx.profile, "ladder_max_rungs", 12)
    object.__setattr__(ctx.profile, "ladder_step_factor", 1.6)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert all(v is not None for v in lad.data["crossed_by_slot"].values())
    by_rate = phases._rungs_by_rate(lad)
    # values are the probe p99 (>> the ~1ms per-second series)
    assert all(all(v > 3.0 for v in vals) for vals in by_rate.values())


# --------------------------------------------------------------------------- #
# under-warmed probes are excluded from the fit/bracket; a rung emptied
# purely by exclusion raises PhaseError (never a silent hole).
# --------------------------------------------------------------------------- #
def test_under_warmed_probes_excluded_from_fit(isolated_results):
    # Flag ONE lang's probes under-warmed at the start rung; the other lang's
    # warm probes survive so the rung is not emptied.
    runner = FakeRunner(crossing=False, under_warm_on={(SLOT_A, 100)})
    ctx = _ctx("uw-1", runner=runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    # the under_warmed map records per-probe flags
    assert lad.data["under_warmed"][probe_key(100, 0, SLOT_A)] is True
    assert lad.data["under_warmed"][probe_key(100, 0, SLOT_B)] is False
    # the excluded SLOT_A probe is NOT in the rung's fit input at rate 100
    by_rate = phases._rungs_by_rate(lad)
    n_warm_at_100 = sum(1 for k, uw in lad.data["under_warmed"].items()
                        if k.startswith("r100.") and not uw)
    assert len(by_rate[100]) == n_warm_at_100
    # the per-probe report p99 map still records the under-warmed probe (raw data
    # is kept; exclusion happens only at fit-input assembly).
    assert probe_key(100, 0, SLOT_A) in lad.data["p99_ms"]


def test_probe_record_names_every_gate_evaluated(isolated_results):
    """W23/B4: the LEDGER, not only the sidecar, says which gates were scored.

    Round 1 persisted `under_warmed` as a bare bool, so naming the gate behind a
    flagged probe meant opening that probe's `<slot>.warmup.json` -- 618 of them
    in a full run -- and the file `analyze` trusts could not answer at all.
    """
    runner = FakeRunner(crossing=False, under_warm_on={(SLOT_A, 100)})
    ctx = _ctx("gates-1", runner=runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    by_key = {probe_key(p.rate, p.block, p.slot): p for p in lad.probes}

    flagged = by_key[probe_key(100, 0, SLOT_A)]
    assert flagged.gates_evaluated == _EVALUATED_GATES
    assert flagged.gates_failed == ["p99_flat"]
    warm = by_key[probe_key(100, 0, SLOT_B)]
    assert warm.gates_evaluated == _EVALUATED_GATES
    assert warm.gates_failed == []          # scored and passed, not "unrecorded"
    # and the bool map agrees, because both are written from one ProbeOutcome
    assert lad.data["under_warmed"][probe_key(100, 0, SLOT_A)] is True
    assert lad.data["under_warmed"][probe_key(100, 0, SLOT_B)] is False


def test_under_warmed_cannot_disagree_with_gates_failed():
    """The bool is a property of the list, so no caller can set it wrong."""
    out = ProbeOutcome(achieved=1.0, error_rate=0.0, p99_ms=1.0, artifact="",
                       gates_evaluated=["p99_flat"], gates_failed=[])
    assert out.under_warmed is False
    out.gates_failed = ["p99_flat"]
    assert out.under_warmed is True
    with pytest.raises(AttributeError):
        out.under_warmed = False            # not settable: no second opinion


def test_a_crashed_warmup_attack_flags_the_probe_with_no_gate_to_blame():
    """The one under-warm cause that is NOT a gate, kept out of gates_failed.

    A failed warmup attack leaves the series the gates would have been scored on
    unwritten, so they can pass vacuously. Naming a gate as the cause would
    attribute the flag to something no gate decided.
    """
    out = ProbeOutcome(achieved=0.0, error_rate=0.0, p99_ms=0.0, artifact="",
                       gates_evaluated=["p99_flat"], gates_failed=[],
                       warmup_attack_failed=True)
    assert out.under_warmed is True
    assert out.gates_failed == []


def test_a_reset_probe_drops_the_previous_attempt_s_gate_verdict(isolated_results):
    """A rung retried after exclusion must not carry the flagged run's gates."""
    runner = FakeRunner(crossing=False, under_warm_on={(SLOT_A, 100)})
    ctx = _ctx("gates-reset", runner=runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    phases._reset_rung_probes(ctx.led, _GC_DEFAULT.cell_id, "ladder", 100)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    stale = [p for p in lad.probes if p.rate == 100 and p.gates_evaluated]
    assert stale == []


def test_fully_under_warmed_rung_raises(isolated_results):
    # Flag BOTH langs under-warmed at the start rung -> the rung empties on
    # exclusion -> PhaseError naming the rung + rate (no silent fit-around).
    runner = FakeRunner(crossing=False,
                        under_warm_on={(SLOT_A, 100), (SLOT_B, 100)})
    ctx = _ctx("uw-2", runner=runner)
    with pytest.raises(phases.PhaseError, match="100"):
        phases.run_ladder(ctx, _GC_DEFAULT)


# --------------------------------------------------------------------------- #
# the core-parallelism manifest knob (processor_count) +
# the Linux processor-count assertion.
# --------------------------------------------------------------------------- #
def test_record_cell_knobs_records_a_row_per_slot(isolated_results):
    # gc-default has empty extra_env, yet every slot still gets a row.
    ctx = _ctx("knobs-1", runner=FakeRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_DEFAULT)
    row = ctx.manifest.knobs_by_cell[_GC_DEFAULT.cell_id]
    assert set(row.by_slot) == set(SLOTS)
    # the fake runner exposes no /runtime-stats read -> processor_count stays None
    assert all(k.processor_count is None for k in row.by_slot.values())
    assert ctx.manifest.knobs_by_slot == {}


def test_record_cell_knobs_reads_processor_count(isolated_results):
    class PcRunner(FakeRunner):
        def read_processor_count(self, lang):
            return phases.SUT_CORES

    ctx = _ctx("knobs-2", runner=PcRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_DEFAULT)
    for slot in SLOTS:
        assert ctx.manifest.knobs_by_slot[slot].processor_count == phases.SUT_CORES


def test_processor_count_mismatch_asserts_on_linux(isolated_results, monkeypatch):
    class BadPcRunner(FakeRunner):
        def read_processor_count(self, lang):
            return phases.SUT_CORES + 1  # wrong: not the pinned core count

    monkeypatch.setattr(phases, "IS_LINUX", True)
    ctx = _ctx("knobs-3", runner=BadPcRunner(crossing=False))
    with pytest.raises(phases.PhaseError, match="processor_count"):
        phases._record_cell_knobs(ctx, _GC_DEFAULT)


def test_processor_count_mismatch_records_without_asserting_off_linux(isolated_results, monkeypatch):
    class BadPcRunner(FakeRunner):
        def read_processor_count(self, lang):
            return phases.SUT_CORES + 1

    monkeypatch.setattr(phases, "IS_LINUX", False)
    ctx = _ctx("knobs-4", runner=BadPcRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_DEFAULT)  # no raise on Windows smoke
    assert ctx.manifest.knobs_by_slot[SLOT_A].processor_count == phases.SUT_CORES + 1


# D5 removed the variant.ado / variant.dapper CELLS, so the variants MACHINERY
# is tested against synthetic entries (the W10 precedent: the machinery stays,
# tested synthetically, when its last real user goes). The one real variant,
# variant.npgsql-default-prepare, is asserted where the DECLARED matrix matters.
def _syn_variant(cell_id: str, data_layer: str | None = None) -> phases.Cell:
    return phases.Cell(
        cell_id=cell_id,
        extra_env={"DATA_LAYER": data_layer} if data_layer else {"MAX_AUTO_PREPARE": "0"},
        linux_only=True, deferred=True, note="synthetic test variant",
    )


def _with_synthetic_variants(monkeypatch, *cells: phases.Cell) -> None:
    monkeypatch.setattr(phases, "CELLS", [*phases.CELLS, *cells])


def test_data_layer_mismatch_asserts_on_linux(isolated_results, monkeypatch):
    from types import SimpleNamespace

    class WrongDlRunner(FakeRunner):
        def read_build(self, slot, extra_env=None):
            # The env override "never reached the SUT": it still reports efcore.
            return SimpleNamespace(processor_count=phases.SUT_CORES,
                                   data_layer="efcore")

    monkeypatch.setattr(phases, "IS_LINUX", True)
    ctx = _ctx("knobs-5", runner=WrongDlRunner(crossing=False))
    ado = _syn_variant("variant.syn-ado", data_layer="ado")
    with pytest.raises(phases.PhaseError, match="data_layer"):
        phases._record_cell_knobs(ctx, ado)


def test_data_layer_match_records_per_cell(isolated_results, monkeypatch):
    from types import SimpleNamespace

    class DlRunner(FakeRunner):
        def read_build(self, slot, extra_env=None):
            dl = (extra_env or {}).get("DATA_LAYER") or get_slot(slot).expected_data_layer
            return SimpleNamespace(processor_count=phases.SUT_CORES, data_layer=dl)

    monkeypatch.setattr(phases, "IS_LINUX", True)
    ctx = _ctx("knobs-6", runner=DlRunner(crossing=False))
    dapper = _syn_variant("variant.syn-dapper", data_layer="dapper")
    phases._record_cell_knobs(ctx, dapper)
    row = ctx.manifest.knobs_by_cell["variant.syn-dapper"]
    for slot in SLOTS:
        assert row.by_slot[slot].data_layer == "dapper"


def test_expected_data_layer_is_the_slots_own_declaration(isolated_results, monkeypatch):
    """A cell with no DATA_LAYER override must expect each slot's OWN layer.

    Round 1 hard-coded `{"go": "sqlc", "dotnet": ...}`, so P2's ado slot -- which
    reports `ado` because that is what it IS -- mismatched on every probe and
    raised before the shakedown could run a single block.
    """
    from types import SimpleNamespace

    class HonestRunner(FakeRunner):
        def read_build(self, slot, extra_env=None):
            return SimpleNamespace(processor_count=phases.SUT_CORES,
                                   data_layer=get_slot(slot).expected_data_layer)

    monkeypatch.setattr(phases, "IS_LINUX", True)
    ctx = _ctx("knobs-7", runner=HonestRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_DEFAULT)      # no raise
    row = ctx.manifest.knobs_by_cell[_GC_DEFAULT.cell_id]
    assert {row.by_slot[s].data_layer for s in SLOTS} == {
        get_slot(s).expected_data_layer for s in SLOTS
    }


# --------------------------------------------------------------------------- #
# variants phase: declare-only by default, executes + pins a selection
# --------------------------------------------------------------------------- #
def test_variants_phase_declares_without_executing_by_default(isolated_results, monkeypatch):
    monkeypatch.delenv(phases.RUN_VARIANTS_ENV, raising=False)
    calls = []
    monkeypatch.setattr(phases, "run_cell", lambda ctx, cell: calls.append(cell.cell_id))
    ctx = _ctx("var-1", runner=FakeRunner(crossing=False))
    phases.run_variants_phase(ctx)
    assert calls == []
    rec = ctx.led.phase("variants")
    assert rec.state == phases.State.DONE
    assert rec.data["executed"] == []
    assert "variant.npgsql-default-prepare" in rec.data["declared"]
    assert "variant.ado" not in rec.data["declared"]      # D5: cell deleted


def test_variants_phase_executes_requested_and_pins_selection(isolated_results, monkeypatch):
    _with_synthetic_variants(monkeypatch, _syn_variant("variant.syn-a", data_layer="ado"),
                             _syn_variant("variant.syn-b", data_layer="dapper"))
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.syn-a,variant.syn-b")
    monkeypatch.setattr(phases, "IS_LINUX", True)
    calls = []
    monkeypatch.setattr(phases, "run_cell", lambda ctx, cell: calls.append(cell.cell_id))
    ctx = _ctx("var-2", runner=FakeRunner(crossing=False))
    phases.run_variants_phase(ctx)
    assert calls == ["variant.syn-a", "variant.syn-b"]
    rec = ctx.led.phase("variants")
    assert rec.data["requested"] == ["variant.syn-a", "variant.syn-b"]
    assert rec.data["declared"]["variant.syn-a"]["deferred"] is False
    assert rec.data["declared"]["variant.npgsql-default-prepare"]["deferred"] is True

    # The pinned selection survives an env change on resume (phase already DONE
    # -> no re-execution; and the recorded list wins over the environment).
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.npgsql-default-prepare")
    phases.run_variants_phase(ctx)
    assert calls == ["variant.syn-a", "variant.syn-b"]
    assert ctx.led.phase("variants").data["requested"] == ["variant.syn-a", "variant.syn-b"]


def test_variants_phase_rejects_unknown_cell_without_pinning_it(isolated_results, monkeypatch):
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.nope")
    ctx = _ctx("var-3", runner=FakeRunner(crossing=False))
    with pytest.raises(phases.PhaseError, match="unknown variant"):
        phases.run_variants_phase(ctx)
    # The typo'd selection must NOT be pinned: after fixing the environment the
    # phase runs with the corrected list instead of re-raising forever.
    assert "requested" not in ctx.led.phase("variants").data
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "")
    phases.run_variants_phase(ctx)
    assert ctx.led.phase("variants").state == phases.State.DONE


def test_run_all_pins_variant_selection_before_any_phase(isolated_results, monkeypatch):
    """Power-loss drill: the selection is pinned at run START, not hours in.

    At full scale the variants phase begins after the headline cell; the
    documented crash recovery is a NAKED-shell `run-all --resume` (no env
    vars). If the pin only landed when the variants phase first executed, that
    resume would read an empty environment and silently mark every variant
    deferred: dropping the ~15 h variant cells with no error.
    """
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.npgsql-default-prepare")
    monkeypatch.setattr(phases, "IS_LINUX", True)
    pin_seen_at_seed = {}

    def crash_at_seed(ctx):
        # By the time ANY phase runs, the pin must already be on the ledger.
        pin_seen_at_seed["requested"] = ctx.led.phase("variants").data.get("requested")
        raise phases.PhaseError("simulated power loss")

    monkeypatch.setattr(phases, "run_doctor_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_host_setup_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_host_readback_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_seed_phase", crash_at_seed)
    ctx = _ctx("var-runall-1")
    with pytest.raises(phases.PhaseError, match="power loss"):
        phases.run_all(ctx)
    assert pin_seen_at_seed["requested"] == ["variant.npgsql-default-prepare"]

    # Naked resume: env cleared, fresh context loads the ledger from disk.
    monkeypatch.delenv(phases.RUN_VARIANTS_ENV, raising=False)
    executed = []
    monkeypatch.setattr(phases, "run_seed_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_capacity_gate_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_validation_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_cells_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "finalize_manifest", lambda ctx: None)
    monkeypatch.setattr(phases, "run_host_restore_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_cell", lambda ctx, cell: executed.append(cell.cell_id))
    ctx2 = _ctx("var-runall-1")
    phases.run_all(ctx2)
    assert executed == ["variant.npgsql-default-prepare"]
    assert ctx2.led.phase("variants").data["executed"] == ["variant.npgsql-default-prepare"]


def test_gate_degraded_optout_replays_from_ledger_stamp(isolated_results):
    """A resume shell WITHOUT the env var must not re-block a run whose gate
    already recorded the operator opt-out: the decision is run state."""
    from dataclasses import replace
    ctx = _ctx("gate-replay-1")
    ctx.profile = replace(ctx.profile, functional_only=False)
    ctx.led.mark_phase(
        "capacity_gate", phases.State.DONE, verdict="db-imposed-shared-knee",
        detail="insufficient headroom", degraded=True,
        degraded_allowed_by=phases.ALLOW_DB_IMPOSED_KNEE_ENV,
    )
    assert ctx.allow_db_imposed_knee is False
    phases._ensure_gate_applied(ctx)          # must not raise
    assert ctx.allow_db_imposed_knee is True


def test_gate_degraded_without_stamp_still_blocks_naked_resume(isolated_results):
    """No recorded opt-out + no env var: the degraded gate keeps blocking."""
    from dataclasses import replace
    ctx = _ctx("gate-replay-2")
    ctx.profile = replace(ctx.profile, functional_only=False)
    ctx.led.mark_phase(
        "capacity_gate", phases.State.DONE, verdict="db-imposed-shared-knee",
        detail="insufficient headroom", degraded=True,
    )
    with pytest.raises(phases.PhaseError, match="capacity gate verdict"):
        phases._ensure_gate_applied(ctx)


def test_gate_late_optin_on_resume_is_stamped(isolated_results):
    """Gate blocked the original start; the operator resumes WITH the env var:
    the decision must be recorded in ledger + manifest, not just acted on."""
    from dataclasses import replace
    ctx = _ctx("gate-replay-3")
    ctx.profile = replace(ctx.profile, functional_only=False)
    ctx.allow_db_imposed_knee = True
    ctx.led.mark_phase(
        "capacity_gate", phases.State.DONE, verdict="db-imposed-shared-knee",
        detail="insufficient headroom", degraded=True,
    )
    phases._ensure_gate_applied(ctx)          # proceeds via the opt-in
    data = ctx.led.phase("capacity_gate").data
    assert data["degraded_allowed_by"] == phases.ALLOW_DB_IMPOSED_KNEE_ENV
    assert ctx.manifest.capacity_gate.degraded is True
    assert ctx.manifest.capacity_gate.degraded_allowed_by == phases.ALLOW_DB_IMPOSED_KNEE_ENV


def test_variants_pinned_selection_beats_env_on_crash_resume(isolated_results, monkeypatch):
    # Simulated crash: the selection is pinned, one cell "ran", the phase is NOT
    # done. A resume under a DIFFERENT environment must replay the pinned list.
    _with_synthetic_variants(monkeypatch, _syn_variant("variant.syn-a", data_layer="ado"))
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.syn-a")
    monkeypatch.setattr(phases, "IS_LINUX", True)
    calls = []
    monkeypatch.setattr(phases, "run_cell", lambda ctx, cell: calls.append(cell.cell_id))
    ctx = _ctx("var-4", runner=FakeRunner(crossing=False))
    assert phases._requested_variants(ctx) == ["variant.syn-a"]   # pin, then "crash"

    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.npgsql-default-prepare")
    phases.run_variants_phase(ctx)
    assert calls == ["variant.syn-a"]
    assert ctx.led.phase("variants").data["requested"] == ["variant.syn-a"]


# --------------------------------------------------------------------------- #
# skip-if-done
# --------------------------------------------------------------------------- #
def test_manifest_records_real_achieved_rate(isolated_results):
    runner = FakeRunner(crossing=False)
    ctx = _ctx("mani-1", runner=runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    rows = ctx.manifest.achieved_vs_offered
    assert rows, "expected per-probe achieved-vs-offered rows"
    # FakeRunner achieves rate*0.999 -> achieved_rate must be that, NOT 0.0
    r = rows[0]
    assert r.achieved_rate == r.offered_rate * 0.999
    assert abs(r.ratio - 0.999) < 1e-6


def test_skip_if_done_does_not_rerun_probes(isolated_results):
    runner = FakeRunner(crossing=False)
    ctx = _ctx("skip-1", runner=runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    first = runner.calls
    assert first > 0
    # second call with the SAME ledger: stage is done -> no new probes
    phases.run_ladder(ctx, _GC_DEFAULT)
    assert runner.calls == first


def test_skip_if_done_survives_reload(isolated_results):
    runner = FakeRunner(crossing=False)
    ctx = _ctx("skip-2", runner=runner)
    phases.run_cell(ctx, _GC_DEFAULT)
    n = runner.calls
    # reload the ledger fresh and re-run the cell: everything done -> zero probes
    ctx2 = _ctx("skip-2", runner=runner)
    phases.run_cell(ctx2, _GC_DEFAULT)
    assert runner.calls == n


# --------------------------------------------------------------------------- #
# RCB block-reset on resume
# --------------------------------------------------------------------------- #
def test_block_reset_flips_orphan_done_member(isolated_results):
    led = Ledger.load_or_create("blk-1", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=42)
    cell_id, stage = "headline.mixed.gc-default", "confirm"
    s = led.stage(cell_id, stage)
    # block 0: both done; block 1: ONE done (orphan); block 2: both pending
    for block in range(3):
        for slot in SLOTS:
            s.probes.append(ProbeRecord(block=block, slot_order=list(SLOTS), slot=slot, rate=8120))
    led.mark_probe_done(cell_id, stage, s.probes[0], artifact="b0.go", achieved=1, error_rate=0)
    led.mark_probe_done(cell_id, stage, s.probes[1], artifact="b0.dn", achieved=1, error_rate=0)
    led.mark_probe_done(cell_id, stage, s.probes[2], artifact="b1.go", achieved=1, error_rate=0)  # orphan

    reset = led.reset_orphan_blocks(cell_id, stage)
    assert reset == [1]
    # block 0 stays done; block 1's done member is flipped back to pending
    states = {(p.block, p.slot): p.state for p in led.stage(cell_id, stage).probes}
    assert all(states[(0, s)] == State.DONE for s in SLOTS)
    assert all(states[(1, s)] == State.PENDING for s in SLOTS)
    assert led.stage(cell_id, stage).data["redone_after_resume"] is True
    assert led.stage(cell_id, stage).data["redone_blocks"] == [1]


def test_block_reset_idempotent_and_keyed_by_block_id(isolated_results):
    """Blocks are keyed by BLOCK ID, and block ids are unique within a stage.

    Re-pinned in W24. The old key was (block, rate), which was only ever correct
    while every block held one rate. `plan_blocks(block_offset=...)` is what
    keeps ids unique now that it no longer does.
    """
    led = Ledger.load_or_create("blk-2", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=42)
    cell_id, stage = "c", "confirm"
    s = led.stage(cell_id, stage)
    # two DISTINCT blocks at two rates; an orphan only in block 1.
    for block, rate in ((0, 8000), (1, 9000)):
        for slot in SLOTS:
            s.probes.append(
                ProbeRecord(block=block, slot_order=list(SLOTS), slot=slot, rate=rate)
            )
    led.mark_probe_done(cell_id, stage, s.probes[0], artifact="a", achieved=1, error_rate=0)
    led.mark_probe_done(cell_id, stage, s.probes[1], artifact="b", achieved=1, error_rate=0)
    led.mark_probe_done(cell_id, stage, s.probes[2], artifact="c", achieved=1, error_rate=0)
    reset = led.reset_orphan_blocks(cell_id, stage)
    assert reset == [1]
    states = {(p.block, p.slot): p.state for p in led.stage(cell_id, stage).probes}
    # the complete block 0 survives untouched
    assert all(states[(0, s)] == State.DONE for s in SLOTS)
    assert all(states[(1, s)] == State.PENDING for s in SLOTS)
    # a second call is a no-op (nothing orphaned now)
    assert led.reset_orphan_blocks(cell_id, stage) == []


def test_a_mixed_rate_block_split_by_a_crash_is_reset_whole(isolated_results):
    """THE NEW RCB HOLE W24 would have opened, closed.

    A confirm block now holds each slot at ITS OWN knee. Under the old
    (block, rate) grouping such a block split into two singleton groups, so
    `0 < done < members` was false for both and a crash that halved it was
    NEVER repaired on resume -- the two members then ran under different ambient
    conditions with nothing in the artifact saying so.
    """
    led = Ledger.load_or_create("blk-mixed", slot_set=DEFAULT_SLOT_SET,
                                config_hash="c", rng_seed=42)
    cell_id, stage = "c", "confirm"
    rec = led.stage(cell_id, stage)
    rates = {SLOT_A: 4000, SLOT_B: 11000}
    for i, slot in enumerate(SLOTS):
        rec.probes.append(ProbeRecord(block=0, slot_order=list(SLOTS), slot=slot,
                                      rate=rates[slot], order_index=i))
    led.mark_probe_done(cell_id, stage, rec.probes[0], artifact="a", achieved=1, error_rate=0)
    rec.data.setdefault("p99_ms", {})[probe_key(rates[SLOTS[0]], 0, SLOTS[0])] = 12.5

    assert led.reset_orphan_blocks(cell_id, stage) == [0]
    assert all(p.state == State.PENDING for p in led.stage(cell_id, stage).probes)
    # ...and the previous attempt's measurement went with it (resume bug 2):
    # a p99 left beside a PENDING probe is a number the ledger says never ran.
    assert led.stage(cell_id, stage).data["p99_ms"] == {}


def test_an_orphan_reset_drops_every_map_the_probe_wrote(isolated_results):
    """Resume bug 2, over the WHOLE map list rather than one map.

    `reset_to_pending` was thorough about the probe record and silent about the
    stage data, so a reset probe read PENDING while its p99, its gate verdict,
    its per-second series and its exclusion reason all stayed behind.
    """
    led = Ledger.load_or_create("blk-stale", slot_set=DEFAULT_SLOT_SET,
                                config_hash="c", rng_seed=42)
    cell_id, stage = "c", "confirm"
    rec = led.stage(cell_id, stage)
    for i, slot in enumerate(SLOTS):
        rec.probes.append(ProbeRecord(block=0, slot_order=list(SLOTS), slot=slot,
                                      rate=8000, order_index=i))
    led.mark_probe_done(cell_id, stage, rec.probes[0], artifact="a", achieved=1, error_rate=0)
    key = probe_key(8000, 0, SLOTS[0])
    survivor = probe_key(8000, 0, SLOTS[1])
    for name in ledger_mod.PROBE_DATA_MAPS:
        rec.data.setdefault(name, {})[key] = 1.0
        rec.data[name][survivor] = 2.0

    led.reset_orphan_blocks(cell_id, stage)
    data = led.stage(cell_id, stage).data
    for name in ledger_mod.PROBE_DATA_MAPS:
        assert key not in data[name], f"{name} kept the reset probe's value"
        # the untouched sibling's row is not collateral damage
        assert data[name][survivor] == 2.0


def test_block_reset_called_on_confirm_resume(isolated_results):
    """A confirm stage resumed with an orphan re-runs the whole block."""
    runner = FakeRunner(crossing=False)
    ctx = _ctx("blk-resume", runner=runner)
    # set up: ladder + fit done, confirm partially executed with an orphan.
    phases.run_ladder(ctx, _GC_DEFAULT)
    phases.run_fit(ctx, _GC_DEFAULT)
    # plan the confirm probes by calling run_confirm once, but inject a stop:
    # simulate a crash by marking only ONE probe of block 0 done, rest pending.
    cid = _GC_DEFAULT.cell_id
    conf = ctx.led.stage(cid, "confirm")
    members, _ = phases._confirm_members(ctx, _GC_DEFAULT)
    for p in phases.plan_blocks(n_blocks=ctx.profile.confirm_blocks, members=members,
                                rng_seed=ctx.led.rng_seed,
                                salt=phases._salt(cid, "confirm", 0)):
        conf.probes.append(p)
    ctx.led.mark_stage(cid, "confirm", State.RUNNING)
    # mark first probe of block 0 done (orphan)
    ctx.led.mark_probe_done(cid, "confirm", conf.probes[0], artifact="x", achieved=1, error_rate=0)
    runner.calls = 0

    # now resume confirm: orphan block 0 must be reset (redone), all probes run
    phases.run_confirm(ctx, _GC_DEFAULT)
    assert ctx.led.stage(cid, "confirm").state == State.DONE
    assert ctx.led.stage(cid, "confirm").data.get("redone_after_resume") is True
    # every probe ran (the orphan was flipped back and re-run)
    assert all(p.state == State.DONE for p in ctx.led.stage(cid, "confirm").probes)


# --------------------------------------------------------------------------- #
# RCB slot order is seeded + recorded
# --------------------------------------------------------------------------- #
def test_plan_blocks_records_seeded_slot_order(isolated_results):
    kw = dict(n_blocks=4, rate=1000, rng_seed=42, salt=7, slots=SLOTS)
    a = phases.plan_blocks(**kw)
    b = phases.plan_blocks(**kw)
    # deterministic given (seed, salt)
    assert [(p.block, p.slot) for p in a] == [(p.block, p.slot) for p in b]
    # each block holds exactly one probe per slot, in a permutation of the set
    by_block: dict[int, set[str]] = {}
    for p in a:
        by_block.setdefault(p.block, set()).add(p.slot)
        assert sorted(p.slot_order) == sorted(SLOTS)
    assert all(v == set(SLOTS) for v in by_block.values())
    # a different salt yields a different order stream (not identical)
    c = phases.plan_blocks(n_blocks=8, rate=1000, rng_seed=42, salt=99, slots=SLOTS)
    assert [p.slot_order for p in c] != [p.slot_order for p in a + a]


# --------------------------------------------------------------------------- #
# probe failure surfaces loudly
# --------------------------------------------------------------------------- #
def test_probe_failure_raises_phase_error(isolated_results):
    runner = FakeRunner(crossing=False, fail_on={(SLOT_A, 100)})
    ctx = _ctx("fail-1", runner=runner)
    with pytest.raises(phases.PhaseError, match="probe failed"):
        phases.run_ladder(ctx, _GC_DEFAULT)
    # the failing probe is recorded FAILED (not silently skipped)
    failed = [p for p in ctx.led.stage(_GC_DEFAULT.cell_id, "ladder").probes
              if p.state == State.FAILED]
    assert failed and failed[0].slot == SLOT_A


# --------------------------------------------------------------------------- #
# seed verify-only-on-resume gating (the count-divergence fix)
# --------------------------------------------------------------------------- #
def test_any_cell_probe_done_detects_writes(isolated_results):
    ctx = _ctx("seed-gate-1")
    assert phases._any_cell_probe_done(ctx.led) is False
    # add a done probe to a cell ladder
    from ledgerbench.ledger import ProbeRecord
    s = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    s.probes.append(ProbeRecord(block=0, slot_order=list(SLOTS), slot=SLOT_A, rate=100))
    ctx.led.mark_probe_done(_GC_DEFAULT.cell_id, "ladder", s.probes[0],
                            artifact="x", achieved=99, error_rate=0)
    assert phases._any_cell_probe_done(ctx.led) is True


def test_seed_resume_skips_verify_only_after_writes(isolated_results, monkeypatch):
    """A resumed seed must NOT run the count-invariant preflight once write-probes
    have run (the seeded counts diverge by design). It records the skip reason and
    never touches docker."""
    ctx = _ctx("seed-gate-2")
    ctx.led.mark_phase("seed", State.DONE, scale="smoke")  # seed already done
    from ledgerbench.ledger import ProbeRecord
    s = ctx.led.stage(_GC_DEFAULT.cell_id, "confirm")
    s.probes.append(ProbeRecord(block=0, slot_order=list(SLOTS), slot=SLOT_A, rate=100))
    ctx.led.mark_probe_done(_GC_DEFAULT.cell_id, "confirm", s.probes[0],
                            artifact="x", achieved=99, error_rate=0)

    # if docker were touched the test env has no seeder; assert it is NOT called.
    called = {"compose": False}

    def _boom(*a, **k):
        called["compose"] = True
        raise AssertionError("compose must not be called on the verify-only skip path")

    monkeypatch.setattr(phases.compose, "compose", _boom)
    phases.run_seed_phase(ctx)
    assert called["compose"] is False
    assert ctx.led.phase_done("seed")
    assert "verify_only_skipped" in ctx.led.phase("seed").data


# --------------------------------------------------------------------------- #
# capacity-gate divergence: verify-only preflight must NOT run after the gate
# wrote invoices (exact-count invariants no longer hold).
# --------------------------------------------------------------------------- #
def test_capacity_gate_counts_as_db_divergence(isolated_results):
    led = Ledger.load_or_create("cgd-1", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    assert phases._db_has_diverged(led) == (False, "")
    # RUNNING (mid-gate crash window) already counts as diverged.
    led.mark_phase("capacity_gate", State.RUNNING)
    diverged, why = phases._db_has_diverged(led)
    assert diverged and "capacity_gate" in why
    led.mark_phase("capacity_gate", State.DONE)
    assert phases._db_has_diverged(led)[0] is True


def test_seed_verify_only_skipped_after_capacity_gate(isolated_results, monkeypatch):
    ctx = _ctx("cgd-2")
    ctx.led.mark_phase("seed", State.DONE, scale="smoke")
    ctx.led.mark_phase("capacity_gate", State.RUNNING)  # crashed mid-gate

    def _boom(*a, **k):
        raise AssertionError("seeder must not run verify-only after gate writes")

    monkeypatch.setattr(phases.compose, "compose", _boom)
    phases.run_seed_phase(ctx)
    assert ctx.led.phase_done("seed")
    assert "capacity_gate" in ctx.led.phase("seed").data["verify_only_skipped"]


# --------------------------------------------------------------------------- #
# scale is pinned on the ledger like config_hash
# --------------------------------------------------------------------------- #
def test_resume_scale_mismatch_refused(isolated_results):
    Ledger.load_or_create("sc-1", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1, scale="smoke")
    with pytest.raises(RuntimeError, match="scale mismatch"):
        Ledger.load_or_create("sc-1", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1, scale="full")
    # same scale resumes; scale=None (legacy caller) skips the check
    assert Ledger.load_or_create("sc-1", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1, scale="smoke").scale == "smoke"
    assert Ledger.load_or_create("sc-1", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1).scale == "smoke"


def test_resume_scale_backfilled_on_legacy_ledger(isolated_results):
    led = Ledger.load_or_create("sc-2", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)  # pre-field
    assert led.scale is None
    led2 = Ledger.load_or_create("sc-2", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1, scale="full")
    assert led2.scale == "full"


# --------------------------------------------------------------------------- #
# measurement gates: error-rate / achieved-vs-offered exclusion (full scale)
# --------------------------------------------------------------------------- #
def test_error_rate_excludes_probe_from_fit(isolated_results):
    class ErrRunner(FakeRunner):
        def run_probe(self, **kw):
            out = super().run_probe(**kw)
            if kw["lang"] == SLOT_A and kw["rate"] == 100:
                out.error_rate = 0.5  # way over slo error_rate_max (0.001)
            return out

    ctx = _ctx("er-1", runner=ErrRunner(crossing=False))
    object.__setattr__(ctx.profile, "functional_only", False)  # gates are full-scale
    # measured scale => the stage guard demands a capacity-gate verdict
    ctx.led.mark_phase("capacity_gate", State.DONE, verdict="pass")
    # ...and a flat sub-knee ladder, which is what this fixture is: the CO/error
    # exclusion under test is orthogonal to the crossing enforcement.
    ctx.allow_sub_knee_ladder = True
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    excluded = lad.data.get("excluded", {})
    assert any(split_probe_key(k) == (100, b, SLOT_A) for k in excluded for b in (0, 1))
    by_rate, empty = phases._rung_values(lad)
    # SLOT_A probes at rung 100 are excluded; SLOT_B's reps keep the rung alive
    n_b_at_100 = sum(1 for k in lad.data["p99_ms"]
                     if (split_probe_key(k) or (None, None, None))[0::2] == (100, SLOT_B))
    assert len(by_rate[100]) == n_b_at_100
    assert not empty


def test_smoke_records_but_does_not_exclude_on_error_rate(isolated_results):
    class ErrRunner(FakeRunner):
        def run_probe(self, **kw):
            out = super().run_probe(**kw)
            out.error_rate = 0.5
            return out

    ctx = _ctx("er-2", runner=ErrRunner(crossing=False))  # smoke: functional only
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert lad.state == State.DONE
    assert not lad.data.get("excluded")


# --------------------------------------------------------------------------- #
# fully-excluded rung: one-shot retry, then loud failure
# --------------------------------------------------------------------------- #
def test_fully_under_warmed_rung_retried_once_then_succeeds(isolated_results):
    class TransientWarm(FakeRunner):
        def __init__(self):
            super().__init__(crossing=False)
            self.attempts: dict[tuple, int] = {}

        def run_probe(self, **kw):
            out = super().run_probe(**kw)
            key = (kw["lang"], kw["rate"], kw["block"])  # per-probe attempts
            self.attempts[key] = self.attempts.get(key, 0) + 1
            if kw["rate"] == 100 and self.attempts[key] == 1:
                out.gates_failed = ["gc_steady"]  # transient: clean on the retry
            return out

    ctx = _ctx("rw-1", runner=TransientWarm())
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert lad.state == State.DONE
    # keyed by (slot, rate): the one-shot retry one slot's hole consumed must
    # not be charged to the other's.
    assert lad.data.get("rewarmed_rungs") == sorted(f"{sid}@100" for sid in SLOTS)
    by_rate, empty = phases._rung_values(lad)
    assert 100 in by_rate and not empty
    # manifest rows replaced, not duplicated, across the rung re-run
    ids = [r.probe_id for r in ctx.manifest.achieved_vs_offered]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# capacity gate: a failed gate STOPS the run, and fired levers actually apply.
# These drive run_capacity_gate_phase on the Linux path with a fake
# runner: no docker, no PG.
# --------------------------------------------------------------------------- #
class GateRunner(FakeRunner):
    """FakeRunner + the capacity-gate surface (knee ladder, lever application)."""

    def __init__(self, knees):
        super().__init__(crossing=False)
        self._knees = list(knees)          # one PgKneeMeasurement per call
        self.workload = load_workload()
        self.env_overrides: dict[str, str] = {}
        self.knee_calls: list[int | None] = []
        self.topology_calls = 0
        self.apply_calls = 0

    def measure_pg_create_knee(self, *, out_dir, rungs, window_s, pg_cores=None):
        self.knee_calls.append(pg_cores)
        return self._knees.pop(0) if len(self._knees) > 1 else self._knees[0]

    def apply_effective(self, *, workload=None, env_overrides=None):
        self.apply_calls += 1
        if workload is not None:
            self.workload = workload
        if env_overrides:
            self.env_overrides.update(env_overrides)

    def apply_topology(self):
        self.topology_calls += 1
        return {"recreated": ["postgres", "vegeta"], "cpusets": dict(self.env_overrides)}


def _measured_knee(rps: float, *, cores: int = 3):
    return capgate.PgKneeMeasurement(
        knee_rps=rps, usable_rungs=4, total_rungs=5, measured_at_cores=cores,
        detail="knee = top usable rung",
    )


def _unmeasured_knee():
    # a create-only ladder that broke on rung one: one point, no knee
    return capgate.pg_knee_from_rungs(
        [capgate.RungResult(rate=500, achieved=500.0057, p99_ms=4.0, error_rate=0.0)]
    )


def _gate_ctx(run_id, runner, *, monkeypatch, allow=False):
    """Measured-scale gate context on the Linux path, with tiny targets gen."""
    monkeypatch.setattr(phases, "IS_LINUX", True)
    ctx = _ctx(run_id, runner=runner)
    ctx.allow_db_imposed_knee = allow
    # smoke numerics, but NOT functional-only: the gate must enforce.
    object.__setattr__(ctx.profile, "functional_only", False)
    # These are CAPACITY-GATE tests. `FakeRunner` holds p99 flat at ~5 ms, so the
    # 3-rung smoke ladder is sub-knee by construction and W24's crossing
    # enforcement would abort every one of them before the gate assertion runs.
    # Opting out here keeps the sub-knee refusal tested where it belongs
    # (`test_a_slot_that_never_crosses_...`) instead of shadowing the gate.
    ctx.allow_sub_knee_ladder = True
    object.__setattr__(ctx.profile, "body_pool_requests", 20)
    return ctx


def _needed_knee(ctx, *, write_share=None, cache_hit_rate=None):
    """PG knee that exactly meets headroom for the given lever state."""
    p = capgate.ResolvedWorkloadParams.from_workload(ctx.workload)
    if write_share is not None:
        p.write_share = write_share
    if cache_hit_rate is not None:
        p.cache_hit_rate = cache_hit_rate
    projected_knee = float(ctx.profile.ladder_start_rps) * 8.0
    return (capgate.projected_db_load_rps(projected_knee, p)
            * ctx.slo.capacity_gate.headroom_factor)


def test_unmeasured_pg_knee_aborts_the_run(isolated_results, monkeypatch):
    runner = GateRunner([_unmeasured_knee()])
    ctx = _gate_ctx("gate-nok", runner, monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="no-pg-knee-measured"):
        phases.run_capacity_gate_phase(ctx)
    data = ctx.led.phase("capacity_gate").data
    assert data["verdict"] == "no-pg-knee-measured"
    assert data["pg_knee_measured"] is False
    assert data["pg_knee_rps"] is None          # never a fallback number
    assert ctx.manifest.capacity_gate.degraded is True


def test_failed_gate_aborts_before_any_cell(isolated_results, monkeypatch):
    # PG far too small for the projected load; every lever exhausts.
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-fail", runner, monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)
    assert ctx.manifest.capacity_gate.verdict == "db-imposed-shared-knee"
    assert ctx.manifest.capacity_gate.degraded is True
    assert ctx.manifest.capacity_gate.degraded_allowed_by is None
    # no cell probe may have run
    assert phases._any_cell_probe_done(ctx.led) is False


def test_failed_gate_opt_out_stamps_degraded_everywhere(isolated_results, monkeypatch):
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-degraded", runner, monkeypatch=monkeypatch, allow=True)
    phases.run_capacity_gate_phase(ctx)            # no raise: explicit opt-out
    data = ctx.led.phase("capacity_gate").data
    assert data["degraded"] is True
    assert data["degraded_allowed_by"] == phases.ALLOW_DB_IMPOSED_KNEE_ENV
    assert ctx.manifest.capacity_gate.degraded is True
    assert ctx.manifest.capacity_gate.degraded_allowed_by == phases.ALLOW_DB_IMPOSED_KNEE_ENV


def test_opt_out_env_var_is_read_by_make_context(monkeypatch):
    monkeypatch.setenv(phases.ALLOW_DB_IMPOSED_KNEE_ENV, "1")
    assert phases._env_flag(phases.ALLOW_DB_IMPOSED_KNEE_ENV) is True
    monkeypatch.setenv(phases.ALLOW_DB_IMPOSED_KNEE_ENV, "0")
    assert phases._env_flag(phases.ALLOW_DB_IMPOSED_KNEE_ENV) is False
    monkeypatch.delenv(phases.ALLOW_DB_IMPOSED_KNEE_ENV, raising=False)
    assert phases._env_flag(phases.ALLOW_DB_IMPOSED_KNEE_ENV) is False


def test_fired_write_share_lever_reaches_workload_and_runner(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-lever", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10))]
    phases.run_capacity_gate_phase(ctx)

    assert ctx.led.phase("capacity_gate").data["verdict"] == "pass"
    assert ctx.led.phase("capacity_gate").data["levers_applied"] == ["write_share"]
    # the EFFECTIVE workload is what the cells will generate targets from
    assert ctx.workload.mix.create_invoice == 10
    assert ctx.spec_workload.mix.create_invoice == 15      # spec untouched
    assert runner.workload.mix.create_invoice == 10        # runner adopted it
    eff = ctx.manifest.effective_workload
    assert eff.spec_write_share == 0.15 and eff.effective_write_share == 0.10
    assert eff.differs_from_spec is True
    # and the ledger can replay it on resume
    assert ctx.led.phase("capacity_gate").data["effective"]["write_share"] == 0.10


def test_lever_that_cannot_be_applied_fails_the_gate(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-unapplied", runner, monkeypatch=monkeypatch)
    # needs BOTH soft levers: cache_hit_rate has no pre-registered TTL, so it
    # fires but cannot be realized -> the PASS would be a paper pass.
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10, cache_hit_rate=0.90))]
    monkeypatch.delenv(phases.CACHE_TTL_OVERRIDE_ENV, raising=False)
    with pytest.raises(phases.PhaseError, match="lever-not-applied"):
        phases.run_capacity_gate_phase(ctx)
    assert "cache_hit_rate" in ctx.manifest.capacity_gate.levers_not_applied
    # the unapplied lever must NOT show up as an effective change
    assert ctx.manifest.effective_workload.effective_cache_hit_rate == 0.80


def test_cache_ttl_override_makes_the_lever_real(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-ttl", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10, cache_hit_rate=0.90))]
    monkeypatch.setenv(phases.CACHE_TTL_OVERRIDE_ENV, "900")
    phases.run_capacity_gate_phase(ctx)
    assert ctx.manifest.capacity_gate.verdict == "pass"
    assert runner.env_overrides["CACHE_TTL_SECONDS"] == "900"
    assert ctx.workload.cache_construction.target_hit_rate == 0.90


def test_pg_cores_lever_moves_cores_remeasures_and_can_still_fail(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-cores", runner, monkeypatch=monkeypatch)
    monkeypatch.setenv(phases.CACHE_TTL_OVERRIDE_ENV, "900")
    need = _needed_knee(ctx, write_share=0.10, cache_hit_rate=0.90)
    # 3-core knee that only clears headroom under the 4/3 linear projection ...
    runner._knees = [_measured_knee(need * 3.0 / 4.0 + 1.0),
                     # ... but the RE-MEASURE at 4 cores says otherwise.
                     _measured_knee(need * 0.5, cores=4)]
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)
    assert runner.env_overrides["PG_CPUS"] == "8-11"
    assert runner.topology_calls == 1               # PG recreated on new cpuset
    assert runner.knee_calls == [None, 4]           # re-measured at the new cores
    assert ctx.manifest.capacity_gate.remeasured_pg_knee_rps == pytest.approx(need * 0.5)


def test_resume_reapplies_the_effective_workload(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-resume", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10))]
    phases.run_capacity_gate_phase(ctx)

    # fresh process: ledger reloaded, workload back at the frozen spec values
    runner2 = GateRunner([_measured_knee(1.0)])   # must NOT be called again
    ctx2 = _gate_ctx("gate-resume", runner2, monkeypatch=monkeypatch)
    assert ctx2.workload.mix.create_invoice == 15
    phases.run_capacity_gate_phase(ctx2)          # phase is done -> replay only
    assert runner2.knee_calls == []               # the ladder did not re-run
    assert ctx2.workload.mix.create_invoice == 10
    assert runner2.workload.mix.create_invoice == 10


def test_resume_replays_a_stamped_gate_optout(isolated_results, monkeypatch):
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-resume-fail", runner, monkeypatch=monkeypatch, allow=True)
    phases.run_capacity_gate_phase(ctx)           # recorded degraded + stamped

    # Resuming WITHOUT the env var must NOT re-block: the opt-out was stamped
    # into the ledger at the first start and is run state, not shell state
    # (the documented crash recovery is a naked shell).
    ctx2 = _gate_ctx("gate-resume-fail", GateRunner([]), monkeypatch=monkeypatch)
    phases.run_capacity_gate_phase(ctx2)          # no raise
    assert ctx2.allow_db_imposed_knee is True


def test_resume_re_enforces_an_unstamped_failed_gate(isolated_results, monkeypatch):
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-resume-fail2", runner, monkeypatch=monkeypatch, allow=False)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)       # recorded degraded, UNstamped

    # No opt-out was ever given: the degraded gate keeps blocking on resume.
    ctx2 = _gate_ctx("gate-resume-fail2", GateRunner([]), monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx2)


def test_smoke_records_the_verdict_without_aborting(isolated_results, monkeypatch):
    """A functional rehearsal exercises the path but publishes nothing."""
    monkeypatch.setattr(phases, "IS_LINUX", True)
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _ctx("gate-smoke", runner=runner)       # smoke: functional_only
    object.__setattr__(ctx.profile, "body_pool_requests", 20)
    phases.run_capacity_gate_phase(ctx)           # no raise at smoke scale
    assert ctx.led.phase("capacity_gate").data["verdict"] == "db-imposed-shared-knee"


# --------------------------------------------------------------------------- #
# knobs are recorded PER CELL and never cross-copied between languages
# --------------------------------------------------------------------------- #
# SYNTHETIC gc-posture cell: G6 deleted the real gc-generous cell (D6, one
# cell), and the per-language knob-routing machinery it exercised stays tested
# against this entry -- the same precedent as the D5 variant cells.
_GC_GENEROUS = phases.Cell(
    cell_id="synthetic.gc-posture",
    extra_env={
        "JVM_MAX_RAM_PERCENT": "75",
        "JVM_GC_FLAGS": "-XX:+UseZGC",
        "DOTNET_GCDynamicAdaptationMode": "0",
    },
    note="synthetic test cell (knob routing)",
)


def test_cell_knobs_are_recorded_per_cell(isolated_results):
    ctx = _ctx("knobs-cell-1", runner=FakeRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_DEFAULT)
    phases._record_cell_knobs(ctx, _GC_GENEROUS)
    by_cell = ctx.manifest.knobs_by_cell
    assert set(by_cell) == {_GC_DEFAULT.cell_id, _GC_GENEROUS.cell_id}
    # the defaults cell keeps NO gc overrides even though a later cell set them
    default_rows = by_cell[_GC_DEFAULT.cell_id].by_slot
    generous_rows = by_cell[_GC_GENEROUS.cell_id].by_slot
    assert all(k.gc_dynamic_adaptation_mode is None for k in default_rows.values())
    assert all(k.gc_engine is None for k in default_rows.values())
    assert generous_rows[SLOT_B].gc_dynamic_adaptation_mode == "0"
    # the verbatim cell env is echoed too
    assert by_cell[_GC_GENEROUS.cell_id].extra_env == _GC_GENEROUS.extra_env


def test_cell_knobs_are_not_cross_copied_between_runtimes(isolated_results):
    """A JVM knob on the .NET row fabricates a value that SUT never saw.

    Driven through the HEADLINE set explicitly, whose two slots are different
    languages: the shakedown pair is two .NET slots, where a cross-copy of a
    .NET-only knob is not an error at all and the property would go untested.
    """
    ctx = _ctx("knobs-cell-2", runner=FakeRunner(crossing=False),
               slot_set="headline")
    phases._record_cell_knobs(ctx, _GC_GENEROUS)
    row = ctx.manifest.knobs_by_cell[_GC_GENEROUS.cell_id].by_slot
    java, dotnet = row["java"], row["dotnet"]
    # JVM-only knobs never land on the .NET row ...
    assert dotnet.max_ram_percent is None and dotnet.gc_engine is None
    # ... and CLR-only knobs never land on the JVM row.
    assert java.gc_dynamic_adaptation_mode is None
    assert java.max_ram_percent == 75 and java.gc_engine == "-XX:+UseZGC"
    assert dotnet.gc_dynamic_adaptation_mode == "0"


def test_variant_dotnet_knobs_stay_on_the_dotnet_row(isolated_results):
    ctx = _ctx("knobs-cell-3", runner=FakeRunner(crossing=False),
               slot_set="headline")
    # A synthetic DATA_LAYER cell (D5 removed the real variant.ado CELL; the
    # per-language knob routing it exercised is unchanged).
    ado = _syn_variant("variant.syn-ado", data_layer="ado")
    phases._record_cell_knobs(ctx, ado)
    row = ctx.manifest.knobs_by_cell["variant.syn-ado"].by_slot
    assert row["dotnet"].data_layer == "ado"
    assert row["java"].data_layer is None


# --------------------------------------------------------------------------- #
# R1-A7: the effective working set is accumulated from the per-probe draws
# --------------------------------------------------------------------------- #
def _write_draw(run_id, *, slot, block, requests, invoices, customers,
                seconds=1.0, window=120, containment=0.5):
    """One probe's `<slot>.target_draw.json`, exactly as live_runner writes it."""
    adir = (paths.run_dir(run_id) / "cells" / "headline.mixed.gc-default"
            / "ladder" / "rate500" / f"block{block}")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{slot}.target_draw.json").write_text(json.dumps({
        "slot": slot, "cell_id": "headline.mixed.gc-default", "stage": "ladder",
        "rate": 500, "block": block, "draw_mode": "per_probe_stream",
        "seed_formula": "PCG64(seed, inc=stream_for(...))",
        "measured_window_seconds": window,
        "measured": {
            "phase": "measured", "inc": "0" * 32, "requests_drawn": requests,
            "distinct_invoice_ids": invoices, "distinct_customer_ids": customers,
            "distinct_customer_ids_list_urls": customers,
            "distinct_customer_ids_create_bodies": 0,
            "endpoint_counts": {}, "generation_seconds": seconds,
        },
        "warmup_stream_incs": ["1" * 32],
        "probe_draw_seconds": seconds,
        "prewarm_containment": {
            "measured_against": "warmup segments + discard",
            "invoice_read_requests": requests,
            "requests_hitting_a_prewarmed_id": (
                None if containment is None else int(requests * containment)
            ),
            "prewarmed_distinct_invoice_ids": invoices,
            "fraction": containment,
        },
    }), encoding="utf-8")


def test_the_working_set_is_the_spread_of_what_each_probe_drew(isolated_results):
    """R1-A7's disclosure, inverted.

    Round 1 generated ONE targets file per slot, replayed it, and recorded the
    seconds a full replay took: at 200 targets and 100 rps that was
    `replay_period_seconds["100"] == 2.0` against a 300 s cache TTL -- the whole
    defect in one number, and the test that used to live here asserted it. W20
    draws a stream per attack, so nothing replays, the field is deleted, and
    what the section carries is the per-probe spread.
    """
    ctx = _ctx("ws-1", runner=FakeRunner(crossing=False))
    _write_draw("ws-1", slot=SLOT_A, block=0, requests=61200, invoices=2000,
                customers=900, seconds=4.0)
    _write_draw("ws-1", slot=SLOT_B, block=0, requests=12240, invoices=600,
                customers=300, seconds=1.0)
    _write_draw("ws-1", slot=SLOT_A, block=1, requests=30600, invoices=1300,
                customers=500, seconds=2.0)
    phases._record_working_set(ctx)
    ws = ctx.manifest.working_set

    assert ws.draw_mode == "per_probe_stream"
    assert ws.requests_drawn_per_probe.n == 3
    assert (ws.requests_drawn_per_probe.min,
            ws.requests_drawn_per_probe.median,
            ws.requests_drawn_per_probe.max) == (12240, 30600, 61200)
    assert ws.distinct_invoice_ids_per_probe.median == 1300
    assert ws.distinct_customer_ids_per_probe.max == 900
    # The cost the design named as its own risk: a per-probe draw that becomes
    # the longest phase of the run has to be visible in the artifact.
    assert ws.draw_seconds_per_probe.max == 4.0
    # The TTL comparison is against the WINDOW now: there is no replay period,
    # and the field that used to carry one is gone from the model entirely.
    # The LONGEST window is what the section states -- soak runs a longer one
    # than the ladder, and the residency question ("can an entry outlive its own
    # window") is answered least favourably by the longest of them.
    _write_draw("ws-1", slot=SLOT_A, block=9, requests=150000, invoices=2400,
                customers=1000, seconds=9.0, window=600)
    phases._record_working_set(ctx)
    ws = ctx.manifest.working_set
    assert ws.measured_window_seconds == 600
    assert ws.requests_drawn_per_probe.n == 4
    assert "replay_period_seconds" not in type(ws).model_fields
    # seeded scale is recorded next to it so "seeded" can never read as "measured"
    assert ws.seeded_invoices == ctx.workload.seeding.scales["smoke"].invoices
    assert ws.distinct_invoice_ids_per_probe.max < ws.seeded_invoices


def test_the_working_set_accumulates_the_measured_prewarm_share(isolated_results):
    """The number that replaces a claim the manifest used to assert as ZERO.

    `working_set.note` said "no attack pre-warms the ids of the next", in every
    manifest. Separate per-attack streams stop one attack replaying another's
    SEQUENCE -- which is all of R1-A7 -- but two independent Zipf draws over one
    keyspace share the head by construction, and measured on this box (at the
    618 rps warmup rate `live_runner._warmup_rate` actually offers) that is
    52-67% of a window's invoice-id read requests. So the section publishes the
    per-probe spread of the measured fraction, and the note claims only what the
    streams achieve.

    A probe whose window asked for no invoice id records `fraction: null` and
    must be ABSENT from the spread rather than entering it as a measured 0.0 --
    the same null-is-not-zero rule every other section here follows, and it
    matters most in the field whose predecessor was an asserted zero.
    """
    ctx = _ctx("ws-pw", runner=FakeRunner(crossing=False))
    _write_draw("ws-pw", slot=SLOT_A, block=0, requests=61200, invoices=2000,
                customers=900, containment=0.4632)
    _write_draw("ws-pw", slot=SLOT_B, block=0, requests=61200, invoices=2000,
                customers=900, containment=0.6602)
    phases._record_working_set(ctx)
    ws = ctx.manifest.working_set
    assert ws.prewarm_containment_per_probe.n == 2
    assert ws.prewarm_containment_per_probe.min == 0.4632
    assert ws.prewarm_containment_per_probe.max == 0.6602

    # the claim the note makes is narrowed to the mechanism, not the outcome
    assert "no attack pre-warms the ids of the next" not in ws.note
    assert "no attack replays another's SEQUENCE" in ws.note
    assert "share the head by construction" in ws.note
    assert "prewarm_containment_per_probe" in ws.note

    # a probe that recorded no fraction is absent, not a zero
    _write_draw("ws-pw", slot=SLOT_A, block=1, requests=1000, invoices=0,
                customers=0, containment=None)
    phases._record_working_set(ctx)
    ws = ctx.manifest.working_set
    assert ws.requests_drawn_per_probe.n == 3
    assert ws.prewarm_containment_per_probe.n == 2
    assert ws.prewarm_containment_per_probe.min == 0.4632


def test_a_run_with_no_draw_artifact_says_so_instead_of_publishing_zeros(isolated_results):
    """An empty spread must never read as "measured, and it drew nothing"."""
    ctx = _ctx("ws-2", runner=FakeRunner(crossing=False))
    (paths.run_dir("ws-2") / "cells").mkdir(parents=True, exist_ok=True)
    phases._record_working_set(ctx)
    ws = ctx.manifest.working_set
    assert ws.draw_mode == "not_measured"
    assert ws.requests_drawn_per_probe.n == 0
    assert ws.requests_drawn_per_probe.n == 0
    assert ws.requests_drawn_per_probe.median is None
    assert "not_measured" in ws.note


def test_the_body_pool_disclosure_is_measured_off_the_pool(isolated_results):
    """The one distributional cost of drawing bodies from a fixed pool.

    A create body is picked uniformly from the pool, so the create path's
    customer_id distribution is the pool's empirical Zipf SAMPLE rather than a
    fresh draw. `body_pool_distinct_customer_ids` is that sample's size, and it
    is counted from the bodies on disk rather than assumed from the pool size:
    at alpha=1.0 the two differ by a lot, and the gap IS the disclosure.
    """
    ctx = _ctx("ws-3", runner=FakeRunner(crossing=False))
    bodies = paths.run_dir("ws-3") / "io" / "pool" / "bodies"
    bodies.mkdir(parents=True)
    for i, cid in enumerate([1, 1, 1, 2, 7]):
        (bodies / f"create_{i:08d}.json").write_text(
            json.dumps({"customer_id": cid, "items": []}), encoding="utf-8")
    phases._record_working_set(ctx)
    ws = ctx.manifest.working_set
    assert ws.body_pool_distinct_customer_ids == 3          # not 5
    assert ws.body_pool_create_bodies == round(
        ctx.profile.body_pool_requests * ctx.workload.mix.create_invoice / 100)
    assert "thinner in the tail" not in ws.note   # no draw: no claim about one


# --------------------------------------------------------------------------- #
# manifest aggregates are built from the artifacts already on disk, and say
# "not measured" when nothing is there.
# --------------------------------------------------------------------------- #
def _cgroup_summary(label, *, role, persistent=False, cpu_us=1000, throttled=2,
                    throttled_us=30, faults=True, peak=600, peak_start=100,
                    cpuset="0-5", mem_limit=12884901888, swap="0",
                    swap_seen=None, swap_peak=None):
    """One sampled cgroup's per-window summary, in the shape the sampler writes."""
    return {
        "label": label, "role": role, "persistent": persistent, "enabled": True,
        "cpuset_effective": cpuset, "mem_limit_bytes": mem_limit,
        "mem_swap_max": swap,
        "swap_current_max": swap_seen, "swap_peak_max": swap_peak,
        "cpu_usage_delta_usec": cpu_us,
        "nr_throttled_delta": throttled, "throttled_usec_delta": throttled_us,
        "oom_kill_delta": 0,
        "mem_anon_p99": 500, "mem_current_p99": 700,
        "mem_peak_start": peak_start, "mem_peak_end": peak,
        "major_faults_delta": 7 if faults else None,
        "minor_faults_delta": 9 if faults else None,
    }


def _write_probe_artifacts(run_id, *, lang=SLOT_A, faults=True, cache=True,
                           shared=False):
    import json as _json
    from ledgerbench import cgroups

    d = paths.run_dir(run_id) / "cells" / "c" / "ladder" / "rate100" / "block0"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"cpu_mem_{lang}.summary.json").write_text(_json.dumps(
        _cgroup_summary(lang, role=cgroups.ROLE_SUT, faults=faults)))
    if shared:
        for label, role, cpu in (
            (compose.SVC_POSTGRES, cgroups.ROLE_DATABASE, 400),
            (compose.SVC_VEGETA, cgroups.ROLE_LOAD_GENERATOR, 250),
            ("dockerd", cgroups.ROLE_CONTAINER_RUNTIME, 60),
        ):
            (d / f"cpu_mem_{lang}.{label}.summary.json").write_text(_json.dumps(
                _cgroup_summary(label, role=role, persistent=True, cpu_us=cpu,
                                faults=False)))
    (d / f"{lang}.summary.json").write_text(_json.dumps({
        "requests": 1000, "rate": 99.9, "success": 0.999,
        "status_codes": {"200": 900, "201": 98, "500": 2},
        "latencies": {"99th": 5_000_000},
    }))
    if cache:
        (d / f"cache_stats_{lang}.json").write_text(_json.dumps({
            "keyspace_hits": 80, "keyspace_misses": 20, "hit_rate": 0.8,
            "source": "redis INFO stats delta over the measured window",
        }))


def test_finalize_manifest_aggregates_probe_artifacts(isolated_results):
    ctx = _ctx("agg-1", runner=FakeRunner(crossing=False))
    _write_probe_artifacts("agg-1")
    phases.finalize_manifest(ctx)
    m = ctx.manifest
    rows = {r.label: r for r in m.resource_deltas}
    assert rows[SLOT_A].cpu_usage_usec_delta == 1000
    assert rows[SLOT_A].windows == 1
    assert rows[SLOT_A].mem_peak_bytes == 600 and rows[SLOT_A].mem_peak_start_bytes == 100
    assert {r.label: r for r in m.throttle_counters}[SLOT_A].nr_throttled == 2
    faults = {r.label: r for r in m.fault_counts}[SLOT_A]
    assert faults.major_faults == 7
    assert faults.source != "not_measured"
    # errors come from the vegeta status codes, not from a default zero
    assert m.errors.count == 2
    assert m.errors.total_requests == 1000
    assert m.errors.rate == pytest.approx(0.002)
    assert m.errors.status_codes["500"] == 2
    assert m.cache_stats.keyspace_hits == 80
    assert m.cache_stats.hit_rate == pytest.approx(0.8)
    # B6 row 10: the containers' own placement and ceilings, from the same
    # artifacts, instead of `topology: not_measured` beside them
    topo = m.topology.containers[SLOT_A]
    assert topo.cpuset_effective == "0-5" and topo.matches is True
    assert topo.mem_limit_bytes == 12884901888 and topo.mem_swap_max == "0"
    assert m.topology.source == "measured"


def test_the_swap_outcome_takes_the_max_over_windows_not_the_last_one(
    isolated_results,
):
    """One window that swapped poisons the cell whether or not the last one did.

    The settings beside it (cpuset, memory.max) are LAST-window-wins on purpose:
    a cpuset that changed mid-run changed because the capacity gate's pg_cores
    lever moved it, and the cells ran under the later value. The OUTCOME is the
    opposite -- a cell that swapped in probe 3 of 282 and not in probe 282 would
    publish `swap_current_max_bytes: 0` with nothing in the manifest red.
    """
    import json as _json

    from ledgerbench import cgroups

    run_id = "agg-swapmax"
    ctx = _ctx(run_id, runner=FakeRunner(crossing=False))
    _write_probe_artifacts(run_id)
    for block, (seen, peak) in enumerate(((4_294_967_296, 8_589_934_592), (0, 0))):
        d = paths.run_dir(run_id) / "cells" / "c" / "ladder" / "rate100" / f"block{block}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"cpu_mem_{SLOT_A}.summary.json").write_text(_json.dumps(
            _cgroup_summary(SLOT_A, role=cgroups.ROLE_SUT, swap_seen=seen,
                            swap_peak=peak)))
    phases.finalize_manifest(ctx)

    row = ctx.manifest.topology.containers[SLOT_A]
    assert row.windows == 2
    assert row.swap_current_max_bytes == 4_294_967_296
    assert row.swap_peak_max_bytes == 8_589_934_592


def test_finalize_manifest_says_not_measured_instead_of_zero(isolated_results):
    ctx = _ctx("agg-2", runner=FakeRunner(crossing=False))
    _write_probe_artifacts("agg-2", faults=False, cache=False)
    phases.finalize_manifest(ctx)
    # faults: null, NOT 0, and explicitly labeled
    assert ctx.manifest.fault_counts[0].major_faults is None
    assert ctx.manifest.fault_counts[0].source == "not_measured"
    assert ctx.manifest.fault_counts[0].label == SLOT_A
    assert ctx.manifest.cache_stats.keyspace_hits is None
    assert ctx.manifest.cache_stats.source == "not_measured"
    assert "NOT a measured zero" in ctx.manifest.cache_stats.note


def test_empty_aggregate_sections_are_marked_per_section(isolated_results):
    """With NO artifacts on disk the lists stay [] -- and say why.

    A row-level `source` cannot mark an empty list; `aggregate_provenance` does.
    """
    ctx = _ctx("agg-3", runner=FakeRunner(crossing=False))
    phases.finalize_manifest(ctx)
    m = ctx.manifest
    assert m.throttle_counters == [] and m.resource_deltas == [] and m.fault_counts == []
    prov = m.aggregate_provenance
    for section in (prov.throttle_counters, prov.resource_deltas,
                    prov.fault_counts, prov.achieved_vs_offered):
        assert section.startswith("not_measured")
    assert m.finalization_error is None


def test_populated_aggregate_sections_name_their_source(isolated_results):
    ctx = _ctx("agg-4", runner=FakeRunner(crossing=False))
    _write_probe_artifacts("agg-4")
    phases.finalize_manifest(ctx)
    prov = ctx.manifest.aggregate_provenance
    assert not prov.throttle_counters.startswith("not_measured")
    assert not prov.resource_deltas.startswith("not_measured")
    assert not prov.fault_counts.startswith("not_measured")
    # INVERTED at W16: the throttle numbers are measured-window deltas now, and
    # the provenance must not keep describing them as lifetime counters.
    assert "end-minus-start" in prov.throttle_counters
    assert "LIFETIME" not in prov.throttle_counters
    # memory.peak did NOT become a delta, and the resource provenance still says so
    assert "HIGH-WATER MARK" in prov.resource_deltas
    assert ctx.manifest.throttle_counters[0].source == prov.throttle_counters


def test_finalize_manifest_sums_window_deltas_not_lifetime_totals(isolated_results):
    """The manifest half of the delta contract: N windows sum to the true total.

    postgres persists for the whole run, so its cpu.stat throttle counters
    carry every earlier window. Round 1 read them at each window's END and
    summed THAT, which multiplies one lifetime total by the window count and
    publishes a pathology that never happened. With per-window deltas on disk,
    the sum over two windows is the true increment across them.
    """
    import json as _json
    from ledgerbench import cgroups

    ctx = _ctx("agg-delta", runner=FakeRunner(crossing=False))
    base = paths.run_dir("agg-delta") / "cells" / "c" / "ladder" / "rate100"
    # Two windows of the SAME persistent cgroup: increments 7 and 3, so the
    # true total across them is 10. Under the lifetime read the two windows
    # would have carried 107 and 110 and summed to 217.
    for block, throttled, throttled_us in ((0, 7, 900), (1, 3, 200)):
        d = base / f"block{block}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"cpu_mem_{SLOT_A}.{compose.SVC_POSTGRES}.summary.json").write_text(
            _json.dumps(_cgroup_summary(
                compose.SVC_POSTGRES, role=cgroups.ROLE_DATABASE, persistent=True,
                cpu_us=400, throttled=throttled, throttled_us=throttled_us,
                faults=False,
            ))
        )
    phases.finalize_manifest(ctx)

    pg = {r.label: r for r in ctx.manifest.throttle_counters}[compose.SVC_POSTGRES]
    assert pg.nr_throttled == 10
    assert pg.throttled_usec == 1100
    assert pg.windows == 2
    pg_res = {r.label: r for r in ctx.manifest.resource_deltas}[compose.SVC_POSTGRES]
    assert pg_res.cpu_usage_usec_delta == 800
    assert pg_res.persistent is True


def test_load_generator_overhead_is_not_in_resource_deltas(isolated_results):
    """vegeta and dockerd are measured, published, and kept out of system cost.

    `_finalize_manifest` rglobs every `cpu_mem_*.summary.json`, so adding the
    apparatus to the sampled set folds it into the list a reader sums as the
    system's cost unless the role routes it elsewhere.
    """
    from ledgerbench import cgroups

    ctx = _ctx("agg-lg", runner=FakeRunner(crossing=False))
    _write_probe_artifacts("agg-lg", shared=True)
    phases.finalize_manifest(ctx)
    m = ctx.manifest

    system = {r.label for r in m.resource_deltas}
    assert system == {SLOT_A, compose.SVC_POSTGRES}
    assert {r.label for r in m.throttle_counters} == system
    apparatus = {r.label: r for r in m.load_generator_overhead}
    assert set(apparatus) == {compose.SVC_VEGETA, "dockerd"}
    assert apparatus[compose.SVC_VEGETA].cpu_usage_usec_delta == 250
    assert apparatus[compose.SVC_VEGETA].excluded_from_per_request_cost is True
    assert apparatus["dockerd"].role == cgroups.ROLE_CONTAINER_RUNTIME
    assert not m.aggregate_provenance.load_generator_overhead.startswith("not_measured")


def test_finalize_manifest_keys_rows_by_label_not_by_container_or_file_stem(
    isolated_results,
):
    """Two cgroups in ONE block directory must stay two rows.

    Round 1 keyed on `str(doc.get("container", path.stem))`, which had three
    defects at once: `dict.get` does not apply its default to an EXPLICIT null,
    so a systemd unit (which has no container) would have published a row
    literally named "None"; `Path.stem` of `cpu_mem_java.summary.json` is the
    malformed `cpu_mem_java.summary`; and two slots that share a compose service
    would have merged into one row.
    """
    import json as _json
    from ledgerbench import cgroups

    ctx = _ctx("agg-label", runner=FakeRunner(crossing=False))
    d = paths.run_dir("agg-label") / "cells" / "c" / "ladder" / "rate100" / "block0"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"cpu_mem_{SLOT_A}.summary.json").write_text(
        _json.dumps(_cgroup_summary(SLOT_A, role=cgroups.ROLE_SUT, cpu_us=1000)))
    doc = _cgroup_summary("dockerd", role=cgroups.ROLE_CONTAINER_RUNTIME,
                          persistent=True, cpu_us=60, faults=False)
    doc["container"] = None                     # a unit is not a container
    (d / f"cpu_mem_{SLOT_A}.dockerd.summary.json").write_text(_json.dumps(doc))
    phases.finalize_manifest(ctx)

    labels = {r.label for r in ctx.manifest.resource_deltas}
    labels |= {r.label for r in ctx.manifest.load_generator_overhead}
    assert labels == {SLOT_A, "dockerd"}
    assert "None" not in labels
    assert not any("summary" in lbl for lbl in labels)


def test_finalize_manifest_reports_not_measured_when_a_window_lacks_delta_keys(
    isolated_results,
):
    """A window with no delta keys contributes NOTHING, and the section says so.

    The live hazard is within one run: `--resume` across the W16 change
    leaves pre-W16 summaries beside post-W16 ones in the same cells tree. An
    `or 0` on the delta path would turn every one of those into a measured zero
    and quietly dilute the sum. (`config_hash` cannot catch this -- it hashes
    `spec/`, not `bench/`.)
    """
    import json as _json

    ctx = _ctx("agg-legacy", runner=FakeRunner(crossing=False))
    d = paths.run_dir("agg-legacy") / "cells" / "c" / "ladder" / "rate100" / "block0"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"cpu_mem_{SLOT_A}.summary.json").write_text(_json.dumps({
        # the pre-W16 key set, verbatim
        "container": get_slot(SLOT_A).container_name(), "enabled": True,
        "cpu_usage_delta_usec": None, "nr_throttled_end": 2,
        "throttled_usec_end": 30, "mem_anon_p99": 500, "mem_peak": 600,
        "oom_events": 0,
    }))
    phases.finalize_manifest(ctx)

    row = ctx.manifest.throttle_counters[0]
    assert row.nr_throttled is None            # never 0
    assert row.windows == 0
    assert row.source == "not_measured"
    assert "pre-W16" in ctx.manifest.aggregate_provenance.throttle_counters


def test_fault_windows_counts_a_window_that_read_only_minor_faults(isolated_results):
    """Either counter is evidence the window was read.

    Counting only `pgmajfault` would publish a real `minor_faults` sum beside
    `windows: 0` and `source: not_measured`.
    """
    import json as _json
    from ledgerbench import cgroups

    ctx = _ctx("agg-faults", runner=FakeRunner(crossing=False))
    d = paths.run_dir("agg-faults") / "cells" / "c" / "ladder" / "rate100" / "block0"
    d.mkdir(parents=True, exist_ok=True)
    doc = _cgroup_summary(SLOT_A, role=cgroups.ROLE_SUT, faults=False)
    doc["minor_faults_delta"] = 9
    (d / f"cpu_mem_{SLOT_A}.summary.json").write_text(_json.dumps(doc))
    phases.finalize_manifest(ctx)

    row = ctx.manifest.fault_counts[0]
    assert row.minor_faults == 9
    assert row.major_faults is None
    assert row.windows == 1
    assert row.source != "not_measured"


def test_the_sampler_writes_exactly_what_the_manifest_and_analyze_read(
    isolated_results, tmp_path,
):
    """The writer/reader key contract, end to end, with no hand-written JSON.

    Every other test on this seam writes the summary dict by hand, so a rename
    on the WRITER side (cgroups.WindowResult.summary_dict) would leave both
    sides passing while the real pipeline silently produced nothing: the reader
    would find no key, fall back to null, and the manifest would publish a run
    with no resource rows at all. This test starts at the sampler.
    """
    import json as _json
    from types import SimpleNamespace

    from ledgerbench import analyze, cgroups
    from ledgerbench.live_runner import shared_summary_name

    def _fake(name, **fields):
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "cpu.stat").write_text(
            "usage_usec {usage}\nnr_throttled {thr}\nthrottled_usec {thru}\n".format(
                usage=fields.get("usage", 0), thr=fields.get("thr", 0),
                thru=fields.get("thru", 0)),
            encoding="utf-8")
        (d / "memory.stat").write_text(
            "anon 4096\nfile 0\npgfault 10\npgmajfault 1\n", encoding="utf-8")
        (d / "memory.current").write_text("8192\n", encoding="utf-8")
        (d / "memory.peak").write_text("9000\n", encoding="utf-8")
        (d / "memory.events").write_text("oom_kill 0\n", encoding="utf-8")
        return d

    # Both cgroups start with throttle HISTORY on them: the deltas below are 2
    # and 41, the lifetime totals are 6 and 140.
    sut_cg = _fake("sut", usage=1_000, thr=4, thru=99)
    pg_cg = _fake("pg", usage=5_000, thr=4, thru=99)
    sampler = phases_cgroup_sampler(cgroups, sut_cg, pg_cg)
    sampler.start()
    _fake("sut", usage=1_000 + 250_000, thr=6, thru=140)   # 250 ms of app CPU
    _fake("pg", usage=5_000 + 80_000, thr=6, thru=140)     # 80 ms of DB CPU
    windows = sampler.stop()

    ctx = _ctx("e2e", runner=FakeRunner(crossing=False))
    d = (paths.run_dir("e2e") / "cells" / "headline.mixed.gc-default"
         / "confirm" / "rate1000" / "block0")
    d.mkdir(parents=True, exist_ok=True)
    for label, win in windows.items():
        name = (f"cpu_mem_{SLOT_A}.summary.json" if label == SLOT_A
                else shared_summary_name(SLOT_A, label))
        (d / name).write_text(_json.dumps(win.summary_dict(), indent=2), encoding="utf-8")
    (d / f"{SLOT_A}.summary.json").write_text(_json.dumps({
        "requests": 1000, "rate": 999.0, "success": 1.0,
        "status_codes": {"200": 1000}, "latencies": {"99th": 5_000_000},
    }), encoding="utf-8")

    # --- the manifest reader ---------------------------------------------- #
    phases.finalize_manifest(ctx)
    assert ctx.manifest.finalization_error is None
    rows = {r.label: r for r in ctx.manifest.resource_deltas}
    assert set(rows) == {SLOT_A, compose.SVC_POSTGRES}
    assert rows[SLOT_A].cpu_usage_usec_delta == 250_000
    assert rows[compose.SVC_POSTGRES].cpu_usage_usec_delta == 80_000
    assert rows[compose.SVC_POSTGRES].persistent is True
    thr = {r.label: r for r in ctx.manifest.throttle_counters}[compose.SVC_POSTGRES]
    assert thr.nr_throttled == 2 and thr.throttled_usec == 41   # deltas, not 6/140

    # --- the analyze reader ------------------------------------------------ #
    rec = SimpleNamespace(
        cell_id="headline.mixed.gc-default",
        confirm=ledger_mod.StageRecord(),
    )
    win_rows = analyze._window_rows("e2e", rec, ("confirm",), (SLOT_A,))
    row = win_rows[SLOT_A]["warm"][0]
    assert row["cpu_ms"] == 250.0
    assert row["db_cpu_ms"] == 80.0                # the DB column, from the sampler
    assert row["cpu_ms_per_req"] == 0.25
    assert row["throttled"] == 2                   # delta, not the lifetime 6
    assert row["oom"] == 0
    assert row["mem_peak"] == 9000


def phases_cgroup_sampler(cgroups, sut_cg, pg_cg):
    """The real target set shape, over fixture paths (no docker, no root)."""
    return cgroups.MultiSampler([
        cgroups.SampleTarget.for_path(SLOT_A, sut_cg, role=cgroups.ROLE_SUT,
                                      persistent=False),
        cgroups.SampleTarget.for_path(compose.SVC_POSTGRES, pg_cg,
                                      role=cgroups.ROLE_DATABASE, persistent=True),
    ])


def test_finalize_manifest_never_raises_on_a_malformed_artifact(isolated_results):
    """A bad artifact must not cost a completed run (it records the failure)."""
    import json as _json

    ctx = _ctx("agg-5", runner=FakeRunner(crossing=False))
    d = paths.run_dir("agg-5") / "cells" / "c" / "ladder" / "rate100" / "block0"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cpu_mem_go.summary.json").write_text(_json.dumps({
        "label": "go", "enabled": True,
        "cpu_usage_delta_usec": "not-a-number",
    }))
    phases.finalize_manifest(ctx)                 # no raise
    assert ctx.manifest.finalization_error is not None
    assert "ValueError" in ctx.manifest.finalization_error


def test_cells_phase_is_recorded_done_before_finalization(isolated_results, monkeypatch):
    """Finalization is bookkeeping; it must never decide whether cells are done."""
    ctx = _ctx("agg-6", runner=FakeRunner(crossing=False))
    seen: list[str] = []

    def _boom(_ctx):
        seen.append(ctx.led.phase("cells").state.value)
        raise RuntimeError("malformed artifact")

    monkeypatch.setattr(phases, "_finalize_manifest", _boom)
    phases.run_cells_phase(ctx)
    assert seen == ["done"]                       # already DONE when finalize ran
    assert ctx.led.phase_done("cells") is True
    assert "malformed artifact" in ctx.manifest.finalization_error


# --------------------------------------------------------------------------- #
# the effective cache TTL (the working set's other half) is recorded
# --------------------------------------------------------------------------- #
def test_effective_cache_ttl_falls_back_to_the_compose_default(isolated_results, monkeypatch):
    ctx = _ctx("ttl-1", runner=FakeRunner(crossing=False))
    monkeypatch.delenv("CACHE_TTL_SECONDS", raising=False)
    ttl, source = phases._effective_cache_ttl(ctx)
    # read out of infra/compose.yaml, never assumed
    compose_yaml = (paths.infra_dir() / "compose.yaml").read_text(encoding="utf-8")
    assert f"CACHE_TTL_SECONDS:-{ttl}" in compose_yaml
    assert source.startswith("infra/compose.yaml default")


def test_effective_cache_ttl_ignores_ambient_env_and_prefers_the_lever(isolated_results, monkeypatch):
    # Ambient CACHE_TTL_SECONDS is scrubbed by compose.KNOB_ENV_VARS and never
    # reaches the SUTs, so the manifest must NOT record it: without a lever the
    # source is the compose.yaml default, regardless of the ambient value.
    ctx = _ctx("ttl-2", runner=FakeRunner(crossing=False))
    monkeypatch.setenv("CACHE_TTL_SECONDS", "42")
    ttl, source = phases._effective_cache_ttl(ctx)
    assert ttl == 300 and "compose.yaml default" in source
    ctx.runner.env_overrides = {"CACHE_TTL_SECONDS": "900"}
    ttl, source = phases._effective_cache_ttl(ctx)
    assert ttl == 900 and phases.CACHE_TTL_OVERRIDE_ENV in source


def test_working_set_records_the_ttl_it_ran_with(isolated_results, monkeypatch):
    ctx = _ctx("ttl-3", runner=FakeRunner(crossing=False))
    monkeypatch.delenv("CACHE_TTL_SECONDS", raising=False)
    _write_draw("ttl-3", slot=SLOT_A, block=0, requests=100, invoices=90,
                customers=40, window=120)
    phases._record_working_set(ctx)
    ws = ctx.manifest.working_set
    # The TTL-versus-window comparison the residency bullet renders needs BOTH.
    assert ws.cache_ttl_seconds is not None
    assert ws.cache_ttl_source != "not_measured"
    assert ws.measured_window_seconds == 120


# --------------------------------------------------------------------------- #
# identity provenance: the sha a run STARTED at survives a resume
# --------------------------------------------------------------------------- #
def test_resume_keeps_the_starting_git_sha_and_records_the_change(isolated_results, monkeypatch):
    from ledgerbench import manifest as mf

    m = mf.new_manifest("sha-1")
    monkeypatch.setattr(phases, "git_identity", lambda: ("aaaa", False))
    phases._record_git_identity(m)
    assert m.identity.git_sha == "aaaa"

    # resume from a DIFFERENT commit: the original sha must survive
    monkeypatch.setattr(phases, "git_identity", lambda: ("bbbb", True))
    phases._record_git_identity(m)
    assert m.identity.git_sha == "aaaa"
    assert m.identity.git_dirty is False
    assert [e["git_sha"] for e in m.identity.git_sha_changed_during_run] == ["bbbb"]
    phases._record_git_identity(m)                 # same state again: no duplicate
    assert len(m.identity.git_sha_changed_during_run) == 1


def test_exhausted_levers_do_not_change_the_workload(isolated_results, monkeypatch):
    """A gate that fails after every lever must leave the workload at spec.

    Applying levers that did not buy headroom would leave a run that is neither
    the spec workload nor a headroom-clean one.
    """
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-nolevers", runner, monkeypatch=monkeypatch, allow=True)
    monkeypatch.setenv(phases.CACHE_TTL_OVERRIDE_ENV, "900")
    phases.run_capacity_gate_phase(ctx)
    assert ctx.manifest.capacity_gate.verdict == "db-imposed-shared-knee"
    assert ctx.manifest.capacity_gate.levers_fired            # they were evaluated
    assert ctx.manifest.capacity_gate.levers_applied == []    # but never applied
    assert ctx.workload.mix.create_invoice == 15
    assert runner.env_overrides == {}
    assert ctx.manifest.effective_workload.differs_from_spec is False


def test_cells_refuse_to_start_after_a_failed_gate(isolated_results, monkeypatch):
    """Entering a cell directly (per-stage CLI / resume) re-asserts the gate."""
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-cell-guard", runner, monkeypatch=monkeypatch, allow=False)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)       # degraded, recorded, UNstamped

    ctx2 = _gate_ctx("gate-cell-guard", GateRunner([]), monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_cell(ctx2, _GC_DEFAULT)
    assert phases._any_cell_probe_done(ctx2.led) is False


def test_cell_entered_directly_gets_the_effective_workload(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-cell-eff", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10))]
    phases.run_capacity_gate_phase(ctx)

    runner2 = GateRunner([])
    ctx2 = _gate_ctx("gate-cell-eff", runner2, monkeypatch=monkeypatch)
    monkeypatch.setattr(phases, "IS_LINUX", False)   # skip the pinned-core assert
    phases.run_cell(ctx2, _GC_DEFAULT)
    assert ctx2.workload.mix.create_invoice == 10
    assert runner2.workload.mix.create_invoice == 10
    # every stage re-asserts the gate, but the effective workload is applied ONCE
    # per process: re-applying would regenerate targets on each stage boundary.
    assert runner2.apply_calls == 1


# --------------------------------------------------------------------------- #
# the per-stage CLI entry points (`ledgerbench ladder|confirm|soak`) never call
# run_cell, so each STAGE has to carry the same gate guard.
# --------------------------------------------------------------------------- #
def _failed_gate_run(run_id, monkeypatch):
    """Record a db-imposed-shared-knee verdict in `run_id` WITHOUT opting out.

    The stamped (opted-out) counterpart replays on resume by design; the guard
    the stage entry points must keep is against the UNstamped degraded record.
    """
    ctx = _gate_ctx(run_id, GateRunner([_measured_knee(1.0)]),
                    monkeypatch=monkeypatch, allow=False)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)
    assert ctx.led.phase("capacity_gate").data["verdict"] == "db-imposed-shared-knee"
    assert not ctx.led.phase("capacity_gate").data.get("degraded_allowed_by")


@pytest.mark.parametrize("stage_fn", ["run_ladder", "run_fit", "run_confirm", "run_soak"])
def test_every_stage_entry_point_refuses_after_a_failed_gate(
    isolated_results, monkeypatch, stage_fn
):
    _failed_gate_run(f"stage-guard-{stage_fn}", monkeypatch)
    ctx = _gate_ctx(f"stage-guard-{stage_fn}", GateRunner([]), monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        getattr(phases, stage_fn)(ctx, _GC_DEFAULT)
    assert phases._any_cell_probe_done(ctx.led) is False


def test_stage_entry_proceeds_when_the_optout_was_stamped(isolated_results, monkeypatch):
    """The stamped opt-out replays at stage entry: a naked resume shell must
    not re-block a degraded-allowed run (only the UNstamped record blocks)."""
    ctx = _gate_ctx("stage-guard-stamped", GateRunner([_measured_knee(1.0)]),
                    monkeypatch=monkeypatch, allow=True)
    phases.run_capacity_gate_phase(ctx)           # degraded, stamped, no raise
    ctx2 = _gate_ctx("stage-guard-stamped", GateRunner([]), monkeypatch=monkeypatch)
    phases.run_ladder(ctx2, _GC_DEFAULT)          # must not raise
    assert ctx2.allow_db_imposed_knee is True
    assert ctx2.led.stage(_GC_DEFAULT.cell_id, "ladder").state == State.DONE


def test_ladder_entered_directly_adopts_the_effective_workload(isolated_results, monkeypatch):
    """`ledgerbench ladder` must run the gate's EFFECTIVE mix, not the spec mix."""
    runner = GateRunner([])
    ctx = _gate_ctx("stage-eff", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10))]
    phases.run_capacity_gate_phase(ctx)

    runner2 = GateRunner([])
    ctx2 = _gate_ctx("stage-eff", runner2, monkeypatch=monkeypatch)
    monkeypatch.setattr(phases, "IS_LINUX", False)
    assert ctx2.workload.mix.create_invoice == 15      # frozen spec, pre-guard
    phases.run_ladder(ctx2, _GC_DEFAULT)
    assert ctx2.workload.mix.create_invoice == 10
    assert runner2.workload.mix.create_invoice == 10
    assert runner2.knee_calls == []                    # the gate did NOT re-run


@pytest.mark.parametrize("stage_fn", ["run_ladder", "run_confirm", "run_soak"])
def test_measured_stage_refuses_when_no_gate_ever_ran(isolated_results, monkeypatch, stage_fn):
    """An ABSENT verdict is not a passing one (the secondary hole in the guard)."""
    ctx = _gate_ctx(f"stage-nogate-{stage_fn}", GateRunner([]), monkeypatch=monkeypatch)
    assert ctx.led.phase("capacity_gate").data == {}
    with pytest.raises(phases.PhaseError, match="capacity gate has not run"):
        getattr(phases, stage_fn)(ctx, _GC_DEFAULT)
    assert phases._any_cell_probe_done(ctx.led) is False


def test_no_gate_plus_opt_out_runs_but_stamps_degraded(isolated_results, monkeypatch):
    ctx = _gate_ctx("stage-nogate-allow", GateRunner([]), monkeypatch=monkeypatch, allow=True)
    monkeypatch.setattr(phases, "IS_LINUX", False)
    phases.run_ladder(ctx, _GC_DEFAULT)
    assert ctx.led.stage(_GC_DEFAULT.cell_id, "ladder").state == State.DONE
    gate = ctx.manifest.capacity_gate
    assert gate.degraded is True
    assert gate.degraded_allowed_by == phases.ALLOW_DB_IMPOSED_KNEE_ENV
    assert "never established" in gate.detail


def test_smoke_stage_needs_no_gate_record(isolated_results):
    """A functional rehearsal publishes nothing, so it is not gate-blocked."""
    ctx = _ctx("stage-smoke", runner=FakeRunner(crossing=False))   # functional_only
    phases.run_ladder(ctx, _GC_DEFAULT)
    assert ctx.led.stage(_GC_DEFAULT.cell_id, "ladder").state == State.DONE
    assert ctx.manifest.capacity_gate.degraded is False


# --------------------------------------------------------------------------- #
# W18/B6: the host read-back phase
# --------------------------------------------------------------------------- #
def _reading(attribute, *, matches, tier=hostread.TIER_1, value="x"):
    return HostReading(
        attribute=attribute, kind=hostread.KIND_SETTING, tier=tier,
        paths=[f"/sys/{attribute}"], value=value, expected="performance",
        matches=matches,
    )


def test_host_readback_is_a_noop_off_linux(isolated_results, monkeypatch):
    """The Windows dev box has no sysfs to read and no tuning to verify."""
    monkeypatch.setattr(phases, "IS_LINUX", False)
    monkeypatch.setattr(phases.hostread, "read_host_state", _must_not_run)
    ctx = _ctx("hr-win")
    phases.run_host_readback_phase(ctx)
    rec = ctx.led.phase("host_readback")
    assert rec.state == State.DONE and rec.data["skipped"] == "non-linux"
    assert ctx.manifest.host_state.source == "not_measured"


def _must_not_run(*a, **k):
    raise AssertionError("the read-back touched the host off Linux")


def test_host_state_reaches_disk_before_a_tier_1_abort(isolated_results, monkeypatch):
    """The artifact must survive the raise that the artifact explains.

    An abort whose evidence never reached disk leaves the operator with an
    error string and nothing to read; `manifest.save()` therefore precedes both
    the ledger write and the raise.
    """
    monkeypatch.setattr(phases, "IS_LINUX", True)
    monkeypatch.setattr(
        phases.hostread, "read_host_state",
        lambda exp, **kw: [_reading(hostread.ATTR_GOVERNOR, matches=False)],
    )
    ctx = _ctx("hr-fail")
    with pytest.raises(phases.PhaseError, match="Tier-1"):
        phases.run_host_readback_phase(ctx)

    on_disk = json.loads((paths.run_dir("hr-fail") / "manifest.json").read_text())
    assert on_disk["host_state"]["source"] == "measured"
    assert "/sys/governor" in on_disk["host_state"]["readings"][0]["paths"]
    assert on_disk["host_state"]["tier1_failures"]
    assert json.loads((paths.run_dir("hr-fail") / "host_state.json").read_text())["reads"] == 1
    assert ctx.led.phase("host_readback").state == State.FAILED


def test_host_readback_passes_and_records_the_l3_groups(isolated_results, monkeypatch):
    monkeypatch.setattr(phases, "IS_LINUX", True)
    readings = [
        _reading(hostread.ATTR_GOVERNOR, matches=True),
        HostReading(
            attribute=hostread.ATTR_TOPOLOGY, kind=hostread.KIND_SETTING,
            tier=hostread.TIER_2, value={"l3_groups": {"0-7": ["cpu0"]},
                                         "smt_control": "on"},
        ),
    ]
    monkeypatch.setattr(phases.hostread, "read_host_state", lambda exp, **kw: readings)
    ctx = _ctx("hr-ok")
    phases.run_host_readback_phase(ctx)
    assert ctx.led.phase("host_readback").state == State.DONE
    assert ctx.manifest.topology.l3_groups == {"0-7": "cpu0"}
    assert ctx.manifest.topology.smt_control == "on"


# --------------------------------------------------------------------------- #
# W18: the read-back's expectation table and host-setup.sh's env must be ONE
# --------------------------------------------------------------------------- #
def test_host_setup_forwards_the_declared_knobs_explicitly_through_sudo(
    isolated_results, monkeypatch,
):
    """`sudo` resets the environment; the script must not run on stale defaults.

    `HOUSEKEEPING_CPUS` is a supported dial. Under sudo's `env_reset` the script
    saw it unset and wrote `CPUAffinity=15` while `HostExpectation.from_env`
    expected `CPUAffinity=14`, so the read-back reported a Tier-1 mismatch on
    every run and every resume with no operator remedy but unsetting the
    variable. The same split silently mis-targeted PGDATA_PATH, STOP_SERVICES
    and the IRQ housekeeping mask.
    """
    monkeypatch.setenv("HOUSEKEEPING_CPUS", "14")
    monkeypatch.setenv("PGDATA_PATH", "/srv/pg")
    monkeypatch.setattr(phases, "IS_LINUX", True)
    monkeypatch.setattr(phases.os, "geteuid", lambda: 1000, raising=False)
    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        return ProcResult(args=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(phases, "run", fake_run)
    phases.run_host_setup_phase(_ctx("hs-env"))

    argv = seen[0]
    assert argv[:3] == ["sudo", "-n", "env"]
    forwarded = dict(tok.split("=", 1) for tok in argv[3:argv.index("bash")])
    assert forwarded["HOUSEKEEPING_CPUS"] == "14"
    assert forwarded["PGDATA_PATH"] == "/srv/pg"
    # the unset ones cross as the SHARED default, never as an empty string
    assert forwarded["SUT_CPUS"] == hostread.DEFAULT_SUT_CPUS
    assert forwarded["SYSTEMD_DROPIN"] == hostread.DEFAULT_SYSTEMD_DROPIN
    assert set(forwarded) >= set(hostread.HOST_SETUP_ENV_DEFAULTS)


def test_host_restore_forwards_the_state_file_through_sudo_too(
    isolated_results, monkeypatch,
):
    """The restore side of the same `env_reset`, and it fails GREEN.

    infra/host-restore.sh replays the state file host-setup.sh wrote, and
    `LEDGER_HOST_STATE` is the only knob it reads. Unforwarded, sudo strips it,
    restore falls back to /var/tmp/ledgerline-host-state.json, finds nothing,
    prints "state-file noop" and exits 0 -- so the phase is recorded DONE while
    the box is left with governor=performance, C-states disabled, THP forced,
    tuned/irqbalance stopped and the systemd drop-in still in place. A green
    phase over an untouched box is worse than a failed one.
    """
    monkeypatch.setenv("LEDGER_HOST_STATE", "/srv/ledger-host-state.json")
    monkeypatch.setattr(phases, "IS_LINUX", True)
    monkeypatch.setattr(phases.os, "geteuid", lambda: 1000, raising=False)
    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        return ProcResult(args=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(phases, "run", fake_run)
    phases.run_host_restore_phase(_ctx("hr-env"))

    argv = seen[0]
    assert argv[:3] == ["sudo", "-n", "env"]
    forwarded = dict(tok.split("=", 1) for tok in argv[3:argv.index("bash")])
    assert forwarded["LEDGER_HOST_STATE"] == "/srv/ledger-host-state.json"
    assert argv[argv.index("bash") + 1].endswith("host-restore.sh")


def test_host_setup_shs_own_defaults_are_the_read_backs_defaults():
    """The module docstring's promise, mechanised.

    hostread.py says a divergence between the script's defaults and the
    read-back's is a test failure rather than a silent pass. Nothing enforced
    it, and `SYSTEMD_DROPIN` had already diverged: a literal in the script and a
    variable here, which made the env var an OFF SWITCH for a Tier-1 gate --
    point it anywhere and the script still wrote /etc/..., the read-back read
    nothing, and `tier1_failures` stayed empty with the drop-in unchecked.
    """
    script = (paths.infra_dir() / "host-setup.sh").read_text(encoding="utf-8")
    declared = dict(re.findall(r'^(\w+)="\$\{\1:-(.*)\}"\s*(?:#.*)?$', script, re.M))
    for name, default in hostread.HOST_SETUP_ENV_DEFAULTS.items():
        assert name in declared, (
            f"{name} is not env-overridable in host-setup.sh, so the read-back "
            "can compare against a value the script never reads"
        )
        assert declared[name] == default, (
            f"{name}: host-setup.sh defaults to {declared[name]!r}, hostread to "
            f"{default!r}"
        )


# `run_all`'s call sequence, which is the ONLY thing that decides execution
# order. `ledger.PHASE_ORDER` is a parallel list consumed by `next_pending()`
# and the status CLI.
RUN_ALL_PHASES = (
    "run_doctor_phase",
    "run_host_setup_phase",
    "run_host_readback_phase",
    "run_seed_phase",
    "run_capacity_gate_phase",
    "run_validation_phase",
    "run_cells_phase",
    "run_variants_phase",
    "finalize_manifest",
    "run_host_restore_phase",
)


def test_run_all_calls_every_phase_exactly_once_in_order(isolated_results, monkeypatch):
    """The OBSERVED call sequence of `run_all`, not a parallel constant.

    `ledger.PHASE_ORDER` is display-only: it drives `next_pending()` and the
    status CLI while `run_all` hardcodes its own sequence, so only this test
    catches a dropped or reordered phase call. Without the
    `run_host_readback_phase(ctx)` call B6 never runs on any real run and
    `host_state` stays `not_measured`, the round-1 defect; with it ahead of
    `run_host_setup_phase` every fresh-boot run Tier-1-aborts on an untuned
    box. Ordering is asserted for EVERY phase for the same reason: nothing
    else in the suite observes this sequence.

    Doctor first and the read-back after host_setup is the load-bearing pair:
    doctor runs BEFORE the tuning is applied, so a Tier-1 comparison there reads
    an untuned box on every fresh boot. The checksum map is last of all, after
    host_restore's own `mark_phase` has rewritten ledger.json.
    """
    calls: list[str] = []

    def recorder(name):
        return lambda ctx: calls.append(name)

    for name in RUN_ALL_PHASES:
        monkeypatch.setattr(phases, name, recorder(name))
    monkeypatch.setattr(
        phases.checksums, "write_run_checksums",
        lambda run_id: calls.append("write_run_checksums"),
    )
    monkeypatch.setattr(phases, "IS_LINUX", False)

    phases.run_all(_ctx("runall-order"))

    assert calls == [*RUN_ALL_PHASES, "write_run_checksums"]


def test_the_phase_order_constant_matches_what_run_all_executes():
    """The display list and the executed list are two objects; keep them one claim.

    `status` prints PHASE_ORDER and `next_pending()` walks it, so a phase that
    run_all executes but PHASE_ORDER omits is invisible to `ledgerbench status`
    and can never be reported as pending.
    """
    executed = [n.removeprefix("run_").removesuffix("_phase") for n in RUN_ALL_PHASES
                if n != "finalize_manifest"]
    assert [p for p in ledger_mod.PHASE_ORDER if p in executed] == executed


# --------------------------------------------------------------------------- #
# W18/B6 row 10: container ground truth into manifest.topology
# --------------------------------------------------------------------------- #
def _agg(role, *, cpuset, mem=None, swap=None, swap_seen=None, swap_peak=None,
         windows=3):
    return {"role": role, "cpuset": cpuset, "mem_limit_bytes": mem,
            "mem_swap_max": swap, "swap_current_max": swap_seen,
            "swap_peak_max": swap_peak, "windows": windows}


def test_container_cpuset_mismatch_is_recorded_not_fatal():
    """A 15 h cell must not die at finalization over a cpuset nobody can fix now.

    The mismatch is real and is worth publishing; the artifact carries it per
    window already. Raising here would destroy the cell to report it.
    """
    m = new_manifest("topo-1")
    per_label = {"sut": _agg(cgroups.ROLE_SUT, cpuset="0-4", mem=12884901888, swap="0")}
    phases._stamp_topology(m, per_label, env={"SUT_CPUS": "0-5"})
    row = m.topology.containers["sut"]
    assert row.cpuset_effective == "0-4"
    assert row.expected_cpuset == "0-5"
    assert row.matches is False
    assert m.topology.source == "measured"


def test_memory_limit_and_swap_reach_the_manifest():
    """The sampler -> summary -> manifest seam, end to end.

    `memory.swap.max` is kept RAW ("max", "0" or a byte count are three claims
    and an int cannot carry the first). It is the SETTING;
    `swap_current_max_bytes` beside it is the OUTCOME -- whether the container
    ever held swapped bytes at a window edge. Recording only the ceiling could
    not distinguish "never swapped" from "swapped 4 GiB", and swapped-out anon
    pages leave `memory.stat anon`, i.e. they silently deflate the published
    steady-state-memory-per-request headline.
    """
    m = new_manifest("topo-2")
    per_label = {
        "pg": _agg(cgroups.ROLE_DATABASE, cpuset="8-10", mem=8589934592, swap="0",
                   swap_seen=0, swap_peak=0),
    }
    phases._stamp_topology(m, per_label, env={})
    row = m.topology.containers["pg"]
    assert row.mem_limit_bytes == 8589934592
    assert row.mem_swap_max == "0"
    assert row.swap_current_max_bytes == 0
    # the level at the edges is 0 for a window that swapped and faulted back
    # inside itself; the kernel's lifetime high-water mark is what says so.
    assert row.swap_peak_max_bytes == 0
    assert row.matches is True                 # compose.bench.yaml's own default
    assert row.windows == 3


def test_a_label_with_no_cgroup_read_gets_no_topology_row():
    """dockerd and containerd are host units with no compose cpuset."""
    m = new_manifest("topo-3")
    phases._stamp_topology(
        m, {"dockerd": _agg(cgroups.ROLE_CONTAINER_RUNTIME, cpuset=None)}, env={})
    assert m.topology.containers == {}
    assert m.topology.source == "not_measured"


def test_the_capacity_gates_cpuset_lever_beats_the_ambient_env(isolated_results):
    """The pg_cores lever exports PG_CPUS onto the runner, not into os.environ."""
    runner = FakeRunner()
    runner.env_overrides = {"PG_CPUS": "8-11"}
    ctx = _ctx("topo-env", runner=runner)
    assert phases._effective_cpuset_env(ctx)["PG_CPUS"] == "8-11"
    m = new_manifest("topo-env")
    # and the comparison is by SET: the kernel writes `8-11`, an operator may
    # have exported `8,9,10,11`, and those are the same placement
    phases._stamp_topology(
        m, {"pg": _agg(cgroups.ROLE_DATABASE, cpuset="8,9,10,11")},
        env=phases._effective_cpuset_env(ctx))
    assert m.topology.containers["pg"].matches is True


def test_pg_settings_are_evidence_and_absent_never_fabricates_a_value():
    m = new_manifest("pg-echo")
    phases._stamp_postgres_config(m, None)
    assert m.postgres_config.source == "not_measured"
    assert m.postgres_config.values == {}
    phases._stamp_postgres_config(m, "shared_buffers\t262144\t8kB\tconfiguration file\t16384\t262144\n")
    assert m.postgres_config.source == "measured"
    assert m.postgres_config.values["shared_buffers"]["source"] == "configuration file"


# --------------------------------------------------------------------------- #
# posture-specific warmup gate sets (W22/B3, R1-A3)
# --------------------------------------------------------------------------- #
def _with_unsatisfiable(monkeypatch, slot_id, *, gate="gc_steady", var="SYNTHETIC_KNOB"):
    """Give one slot a SYNTHETIC unsatisfiable-gate entry and return it.

    The real registry table is empty and must stay empty until a posture is
    measured (test_registry.test_no_runtime_declares_an_unsatisfiable_gate_yet),
    so the refusal machinery is exercised against a planted entry.
    """
    import dataclasses

    from ledgerbench import registry as reg

    entry = reg.UnsatisfiableGate(
        gate=gate, env_var=var, value="off",
        why="SYNTHETIC TEST ENTRY: this knob disables collection outright",
    )
    slot = reg.SLOTS[slot_id]
    monkeypatch.setitem(reg.SLOTS, slot_id, dataclasses.replace(
        slot, language=dataclasses.replace(slot.language, unsatisfiable_gates=(entry,)),
    ))
    return entry


def test_a_gate_override_with_an_empty_rationale_fails_validation():
    """A waiver must carry a stated rationale (R1-A3).

    The difference between "this posture cannot satisfy this gate" and round 1's
    ~30 h of unsatisfiable-gate runtime is an auditable reason travelling with
    the declaration, so the dataclass refuses the declaration without one.
    """
    with pytest.raises(ValueError, match="gate_set_rationale"):
        phases.Cell(cell_id="c", warmup_gates={"gc_steady": False})

    ok = phases.Cell(
        cell_id="c", warmup_gates={"gc_steady": False},
        gate_set_rationale="measured at G6: this posture never reaches a flat heap",
    )
    assert ok.warmup_gates == {"gc_steady": False}


def test_no_declared_cell_overrides_the_warmup_gate_set():
    """Round 2 declares NO posture exemption, in code or in spec.

    A posture exemption is a MEASURED finding (G6). The mechanism lands first so
    the finding has a pre-registered home; declaring one now would be the
    over-declaration the governing rule forbids.
    """
    assert [c.cell_id for c in phases.CELLS if c.warmup_gates] == []
    assert load_slo().warmup.gates_by_posture == {}


def test_a_cell_enforcing_an_unsatisfiable_gate_is_refused_before_any_probe(
    isolated_results, monkeypatch
):
    """R1-A3: an unsatisfiable gate is refused before any probe runs.

    The refusal must happen with the ledger still innocent of probes: that is
    the whole difference between stopping the cell and reporting on it
    afterwards, and it is what P8's abort criterion reads.
    """
    ctx = _ctx("syn-unsat")
    _with_unsatisfiable(monkeypatch, SLOT_A)
    cell = phases.Cell(cell_id="headline.mixed.gc-default",
                       extra_env={"SYNTHETIC_KNOB": "off"})

    with pytest.raises(phases.PhaseError) as exc:
        phases.run_cell(ctx, cell)
    assert "unsatisfiable by construction" in str(exc.value)
    assert "SYNTHETIC TEST ENTRY" in str(exc.value)
    assert SLOT_A in str(exc.value)
    assert phases._any_cell_probe_done(ctx.led) is False


def test_the_refusal_checks_every_cell_before_the_first_one_runs(
    isolated_results, monkeypatch
):
    """Per-cell checking would discover cell 2's bad gate set after cell 1's hours."""
    ctx = _ctx("syn-unsat-all")
    _with_unsatisfiable(monkeypatch, SLOT_A)
    cells = [phases.Cell(cell_id="first"),
             phases.Cell(cell_id="second", extra_env={"SYNTHETIC_KNOB": "off"})]

    with pytest.raises(phases.PhaseError, match="second"):
        phases.assert_gate_sets_satisfiable(ctx, cells)
    assert phases._any_cell_probe_done(ctx.led) is False


def test_a_cell_that_waives_the_unsatisfiable_gate_is_allowed_through(
    isolated_results, monkeypatch
):
    """The refusal is about ENFORCING an unsatisfiable gate, not about the knob.

    A cell that declares the gate not applicable (with its rationale) is exactly
    round 1's own prescription for R1-A3 and must run.
    """
    ctx = _ctx("syn-unsat-waived")
    _with_unsatisfiable(monkeypatch, SLOT_A)
    cell = phases.Cell(
        cell_id="waived", extra_env={"SYNTHETIC_KNOB": "off"},
        warmup_gates={"gc_steady": False},
        gate_set_rationale="SYNTHETIC: the knob above disables collection",
    )
    assert phases.gate_set_conflicts(cell, ctx.slots, ctx.slo.warmup) == []
    phases.assert_gate_sets_satisfiable(ctx, [cell])


@pytest.mark.parametrize("door", ["run_ladder", "run_confirm", "run_soak"])
def test_every_stage_door_refuses_the_unsatisfiable_cell_before_any_probe(
    isolated_results, monkeypatch, door
):
    """The three per-stage CLI commands never pass through `run_cell`.

    `ledgerbench ladder|confirm|soak --cell X` enters these functions directly
    (`__main__` calls them), so a refusal that only `run_cell` performs is a
    refusal the documented CLI walks straight past -- R1-A3's ~30 h still
    reachable, on the very cell `run_cell` refuses. Every door already
    re-asserts the CAPACITY gate for exactly this reason.
    """
    ctx = _ctx(f"syn-unsat-{door}")
    _with_unsatisfiable(monkeypatch, SLOT_A)
    cell = phases.Cell(cell_id="headline.mixed.gc-default",
                       extra_env={"SYNTHETIC_KNOB": "off"})

    with pytest.raises(phases.PhaseError) as exc:
        getattr(phases, door)(ctx, cell)
    assert "unsatisfiable by construction" in str(exc.value)
    assert SLOT_A in str(exc.value)
    assert phases._any_cell_probe_done(ctx.led) is False


def test_a_posture_waiver_that_names_no_cell_is_refused_before_any_probe(
    isolated_results, monkeypatch
):
    """A misspelt cell id in `gates_by_posture` is otherwise a SILENT no-op.

    The waiver binds to nothing, so the gate it names stays ENFORCED while the
    spec reads as an exemption: the R1-A3 failure mode again, and in the layer
    inside `config_hash`, which is the harder one to notice. The
    second assertion is the no-op itself: the resolver really does still enforce
    the gate for the cell the operator meant.
    """
    from ledgerbench import warmup as warmup_mod

    ctx = _ctx("syn-posture-typo")
    monkeypatch.setattr(ctx.slo.warmup, "gates_by_posture", {
        "headline.mixed.gc-generus": PostureWaiver(
            gates={"gc_steady": False},
            rationale="SYNTHETIC: the cell id above is misspelt",
        ),
    })

    with pytest.raises(phases.PhaseError, match="bind to nothing"):
        phases.run_cell(ctx, _GC_DEFAULT)
    assert phases._any_cell_probe_done(ctx.led) is False
    assert warmup_mod.gate_set_for_cell(
        ctx.slo.warmup, cell_id="synthetic.some-other-cell",
    ).enforced["gc_steady"] is True


def test_a_posture_waiver_naming_a_real_cell_is_allowed_through(
    isolated_results, monkeypatch
):
    """The refusal is about a waiver that binds to NOTHING, not about waiving."""
    ctx = _ctx("syn-posture-ok")
    monkeypatch.setattr(ctx.slo.warmup, "gates_by_posture", {
        _GC_DEFAULT.cell_id: PostureWaiver(
            gates={"gc_steady": False}, rationale="SYNTHETIC: a real cell id",
        ),
    })
    assert phases.posture_waiver_conflicts(ctx.slo.warmup) == []
    phases.assert_gate_plan_sound(ctx, [_GC_DEFAULT])


def test_the_ledger_records_the_non_gate_cause_of_under_warmed(isolated_results):
    """`under_warmed: true` beside `gates_failed: []` must still name its cause.

    A crashed warmup ATTACK is the second input to the verdict and no gate
    expresses it, so until it was persisted the ledger could record the flag
    with nothing on disk to explain it -- and `analyze` then blamed a missing
    artifact for a cause that was present.
    """
    class CrashedWarmupRunner(FakeRunner):
        def run_probe(self, **kw):
            out = super().run_probe(**kw)
            out.warmup_attack_failed = True
            return out

    ctx = _ctx("syn-attack-failed")
    phases.run_ladder(ctx, _GC_DEFAULT)
    phases.run_fit(ctx, _GC_DEFAULT)
    ctx.runner = CrashedWarmupRunner()
    phases.run_confirm(ctx, _GC_DEFAULT)

    rec = ctx.led.stage(_GC_DEFAULT.cell_id, "confirm")
    done = [p for p in rec.probes if p.state == State.DONE]
    assert done
    for p in done:
        key = probe_key(p.rate, p.block, p.slot)
        assert rec.data["under_warmed"][key] is True
        assert p.gates_failed == []              # no gate decided this
        assert p.warmup_attack_failed is True    # the cause, on disk

    # and it survives the round trip, which is the whole point of persisting it
    reloaded = Ledger.load(ctx.run_id)
    assert all(
        p.warmup_attack_failed
        for p in reloaded.stage(_GC_DEFAULT.cell_id, "confirm").probes
        if p.state == State.DONE
    )


def test_a_re_run_probe_carries_none_of_the_previous_attempt_s_record(
    isolated_results,
):
    """`reset_to_pending` drops EVERY field a completed run wrote.

    Two callers reset probes and a field added to the record but to only one of
    them leaves a re-run probe carrying the previous attempt's verdict -- which
    is why the reset is one function, and why the assertion is over the model's
    own field set rather than a hand-kept list that would drift the same way.
    """
    probe = ProbeRecord(block=0, slot_order=list(SLOTS), slot=SLOT_A, rate=1000)
    fresh = probe.model_dump()

    probe.state = State.DONE
    probe.artifact = "cells/c/ladder/x.json"
    probe.achieved = 999.0
    probe.error_rate = 0.5
    probe.gates_evaluated = ["p99_flat"]
    probe.gates_failed = ["p99_flat"]
    probe.warmup_attack_failed = True
    probe.ts = "2026-08-23T00:00:00.000000Z"

    probe.reset_to_pending()
    assert probe.model_dump() == fresh


# --------------------------------------------------------------------------- #
# W26 / R1-A6: the capacity re-gate at REALIZED rates
# --------------------------------------------------------------------------- #
class RegateRunner(PerSlotRunner):
    """PerSlotRunner that also writes the PostgreSQL window + cache artifacts.

    `pg_busy` is the fraction of PG's core budget that slot's windows burn, so a
    test states the utilization it wants and the module computes it back out of
    the cpu.stat delta and the wall clock exactly as the run would.
    """

    PG_CORES = 3
    WINDOW_S = 10.0

    def __init__(self, *, knee_by_slot, pg_busy, cache=(80, 20), cpuset="8-10"):
        super().__init__(knee_by_slot=knee_by_slot)
        self.pg_busy = pg_busy
        self.cache = cache
        self.cpuset = cpuset

    def run_probe(self, **kw):
        adir, slot = kw["artifact_dir"], kw["lang"]
        adir.mkdir(parents=True, exist_ok=True)
        busy = self.pg_busy[slot]
        (adir / f"cpu_mem_{slot}.{compose.SVC_POSTGRES}.summary.json").write_text(
            json.dumps({
                "label": compose.SVC_POSTGRES, "role": "database", "enabled": True,
                "cpuset_effective": self.cpuset, "window_seconds": self.WINDOW_S,
                "cpu_usage_delta_usec": int(busy * self.PG_CORES * self.WINDOW_S * 1e6),
            }), encoding="utf-8")
        hits, misses = self.cache
        (adir / f"cache_stats_{slot}.json").write_text(
            json.dumps({"keyspace_hits": hits, "keyspace_misses": misses}),
            encoding="utf-8")
        return super().run_probe(**kw)


def _regate_ctx(run_id, runner, *, gate_verdict="pass", pg_knee=3715.05,
                lower_bound=True, rungs=8, step=2.0):
    """A full-scale ctx with a PASSED stage-1 gate already on the ledger."""
    ctx = _ctx(run_id, scale="full", runner=runner)
    object.__setattr__(ctx.profile, "ladder_max_rungs", rungs)
    object.__setattr__(ctx.profile, "ladder_step_factor", step)
    object.__setattr__(ctx.profile, "confirm_blocks", 2)
    ctx.led.mark_phase("capacity_gate", State.DONE, verdict=gate_verdict,
                       pg_knee_rps=pg_knee)
    ctx.manifest.capacity_gate.pg_knee_rps = pg_knee
    gdir = paths.run_dir(run_id) / "capacity-gate"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "verdict.json").write_text(json.dumps({
        "verdict": gate_verdict, "pg_knee_rps": pg_knee,
        "pg_knee_is_lower_bound": lower_bound,
    }), encoding="utf-8")
    return ctx


def _run_first_cell(ctx, cell=None):
    cell = cell or _GC_DEFAULT
    phases.run_ladder(ctx, cell)
    phases.run_fit(ctx, cell)
    phases.run_confirm(ctx, cell)
    return cell


def test_the_stage_one_projection_proxy_is_named_and_recorded(isolated_results):
    """R1-A6's invisible half: the 8x was an inline literal that reached no
    artifact, so no reader of round 1 could see that a 1.72x margin had been
    taken against 4,000 rps of proxy demand while the cells ran far above it."""
    ctx = _ctx("proxy-1", scale="full")
    assert phases.PROJECTED_KNEE_FACTOR == 8.0
    assert phases._projected_knee_rps(ctx) == 500 * 8.0
    basis = phases._projected_knee_basis(ctx)
    assert "PLANNING PROXY" in basis and "4000.0 rps" in basis
    assert "500 x 8" in basis


def test_regate_uses_realized_rates_and_the_measured_hit_rate(isolated_results):
    """The R1-A6 regression guard.

    Round 1 projected DB load from `ladder_start_rps * 8` and from the CONSTRUCTED
    0.80 hit rate, then ran cells 4-6x higher. Here the fitted knee is 5,714 rps
    against a 4,000 rps proxy, and the cache measured 0.50 rather than the
    constructed 0.80, so both inputs move and both moves are visible in the
    projected number.
    """
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.30, SLOT_B: 0.30}, cache=(50, 50))
    ctx = _regate_ctx("regate-1", runner)
    cell = _run_first_cell(ctx)
    phases.run_capacity_regate_phase(ctx, cell)

    rg = ctx.manifest.capacity_gate.regate
    assert rg.verdict == capgate.Verdict.PASS_AT_REALIZED_RATES.value
    # the realized knee, taken as the MAX across slots (PG serves both)
    knees = ctx.led.stage(cell.cell_id, "fit").data["knee_by_slot"]
    assert rg.realized_knee_rps == pytest.approx(max(knees.values()))
    assert rg.realized_knee_rps == pytest.approx(5714.285714, rel=1e-6)
    assert rg.realized_knee_rps > phases._projected_knee_rps(ctx)
    # the MEASURED hit rate, not spec/workload.yaml's constructed target
    assert rg.measured_cache_hit_rate == pytest.approx(0.50)
    assert load_workload().cache_construction.target_hit_rate == 0.80
    assert "MEASURED" in rg.cache_hit_rate_source
    # ...and the projection is the one those two inputs imply: DB-touch =
    # create .15 + list .25 + pdf .08 + get .30 x miss .50 = 0.63.
    assert rg.projected_db_rps_at_realized_rates == pytest.approx(
        rg.realized_knee_rps * 0.63)
    # which is NOT what either round-1 input would have produced
    proxy_at_spec_hit = phases._projected_knee_rps(ctx) * 0.54
    assert rg.projected_db_rps_at_realized_rates > proxy_at_spec_hit * 1.6


def test_regate_measures_only_the_operating_point_windows(isolated_results):
    """The confirm stage also runs the higher-knee slot at the COMMON rate for
    the paired verdict. Those windows sit below the operating point by
    construction, so averaging them in would drag the measured utilization down
    -- measuring the gate somewhere other than where the headline is taken, which
    is R1-A1's shape."""
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.30, SLOT_B: 0.30})
    ctx = _regate_ctx("regate-2", runner)
    cell = _run_first_cell(ctx)
    phases.run_capacity_regate_phase(ctx, cell)

    rates = ctx.led.stage(cell.cell_id, "confirm").data["confirm_rates_by_slot"]
    common = ctx.led.stage(cell.cell_id, "confirm").data["common_rate_rps"]
    assert rates[SLOT_A] != rates[SLOT_B] and common == min(rates.values())
    # every confirm window on disk...
    written = sorted(
        p.parent.parent.name
        for p in (paths.run_dir("regate-2") / "cells" / cell.cell_id / "confirm")
        .rglob(f"cpu_mem_*.{compose.SVC_POSTGRES}.summary.json")
    )
    assert len(written) == 3 * ctx.profile.confirm_blocks     # incl. the common-rate probe
    # ...against the ones the re-gate counted: one per slot per block, own rate only.
    rg = ctx.manifest.capacity_gate.regate
    assert rg.windows_by_slot == {SLOT_A: 2, SLOT_B: 2}
    assert rg.windows == 4
    assert rg.realized_rates_by_slot == {SLOT_A: float(rates[SLOT_A]),
                                         SLOT_B: float(rates[SLOT_B])}


def test_regate_binds_on_the_slot_a_pooled_figure_would_have_excused(isolated_results):
    """0.30 and 0.90 pool to 0.60, which clears the 0.769 ceiling. PG was past it
    for every window of the faster slot, and it serves whichever stack is up."""
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.30, SLOT_B: 0.90})
    ctx = _regate_ctx("regate-3", runner)
    cell = _run_first_cell(ctx)
    with pytest.raises(phases.PhaseError, match="db-imposed-at-realized-rates"):
        phases.run_capacity_regate_phase(ctx, cell)

    rg = ctx.manifest.capacity_gate.regate
    assert rg.pg_utilization == pytest.approx(0.90)
    assert rg.pg_utilization_binding_slot == SLOT_B
    assert rg.pg_utilization_by_slot == pytest.approx({SLOT_A: 0.30, SLOT_B: 0.90})
    pooled = rg.pg_cpu_seconds / (rg.pg_cores * rg.window_seconds)
    assert pooled == pytest.approx(0.60) and pooled < rg.pg_utilization_ceiling


def test_regate_failure_stamps_cell_one(isolated_results):
    """Cell 1 cannot be un-run -- it is the thing being measured -- so the
    verdict is retroactive and has to name what it lands on."""
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.90, SLOT_B: 0.90})
    ctx = _regate_ctx("regate-4", runner)
    cell = _run_first_cell(ctx)
    with pytest.raises(phases.PhaseError):
        phases.run_capacity_regate_phase(ctx, cell)

    rg = ctx.manifest.capacity_gate.regate
    assert rg.cells_measured_before_regate == [cell.cell_id]
    assert rg.degraded is True and rg.degraded_allowed_by is None
    # the run-level stamp travels too, so an artifact reading only the gate's
    # own `degraded` cannot miss the re-gate
    assert ctx.manifest.capacity_gate.degraded is True
    # ...while stage 1's own verdict is untouched: it passed on its own terms
    assert ctx.manifest.capacity_gate.verdict is None
    assert ctx.led.phase("capacity_gate").data["verdict"] == "pass"
    # and the evidence reached disk BEFORE the raise
    doc = json.loads((paths.run_dir("regate-4") / "manifest.json").read_text())
    assert doc["capacity_gate"]["regate"]["verdict"] == (
        capgate.Verdict.DB_IMPOSED_AT_REALIZED_RATES.value)


def _with_second_headline_cell(monkeypatch) -> phases.Cell:
    """The regate's stop-between-cells contract needs a run matrix with a cell
    AFTER the first. G6 shrank the real matrix to one headline cell, so the
    second is synthetic here -- the machinery is unchanged and fires between
    ANY cell 1 and cell 2 (a selected variant recreates the multi-cell case in
    production)."""
    syn = phases.Cell(cell_id="synthetic.second-cell", extra_env={},
                      note="synthetic second headline cell (regate contract)")
    monkeypatch.setattr(phases, "CELLS", [*phases.CELLS, syn])
    monkeypatch.setattr(
        phases, "HEADLINE_CELL_IDS", phases.HEADLINE_CELL_IDS | {syn.cell_id})
    return syn


def test_the_regate_stamp_names_every_cell_already_measured_not_just_the_first(
    isolated_results, monkeypatch,
):
    """The retroactive caveat has to cover what it is retroactive OVER.

    `cells_measured_before_regate` was hard-coded to `[first_cell.cell_id]`
    while `CapacityRegateStatus`'s docstring said it "names every cell whose
    windows were already on disk when the verdict was reached" and the report
    bullet said "every later cell was either stopped or run under the operator
    opt-out". W26 deliberately bumps no `LEDGER_SCHEMA_VERSION`, so a ledger
    whose later cells ran BEFORE the re-gate existed still resumes and gets the
    re-gate evaluated over artifacts already on disk -- and then both halves of
    that sentence were false for those cells: they had been measured, and they
    were neither stopped nor opted out of.

    Reproduced here by measuring two cells before the re-gate runs, which is
    also what per-cell `ledgerbench ladder/confirm/soak --cell <cell2>` leaves
    behind, since those CLI paths never invoke the re-gate at all.
    """
    _with_second_headline_cell(monkeypatch)
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.90, SLOT_B: 0.90})
    ctx = _regate_ctx("regate-legacy", runner)
    cells = phases.cells_for(ctx.profile)
    assert len(cells) > 1, "this test needs a second already-measured cell"
    for cell in cells[:2]:
        _run_first_cell(ctx, cell)
    # a cell RECORDED but not yet measured: it must not be named, or the field
    # becomes "cells that exist" rather than "cells whose windows are on disk"
    ctx.led.stage("headline.mixed.not-run", "ladder")

    with pytest.raises(phases.PhaseError):
        phases.run_capacity_regate_phase(ctx, cells[0])

    rg = ctx.manifest.capacity_gate.regate
    assert rg.cells_measured_before_regate == [c.cell_id for c in cells[:2]]
    assert "headline.mixed.not-run" not in rg.cells_measured_before_regate


def test_regate_failure_stops_before_cell_two(isolated_results, monkeypatch):
    _with_second_headline_cell(monkeypatch)
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.90, SLOT_B: 0.90})
    ctx = _regate_ctx("regate-5", runner)
    cells = phases.cells_for(ctx.profile)
    assert len(cells) > 1, "this test needs a second cell to be stopped"
    with pytest.raises(phases.PhaseError, match="db-imposed-at-realized-rates"):
        phases.run_cells_phase(ctx)
    second = cells[1].cell_id
    assert ctx.led.stage(cells[0].cell_id, "soak").state == State.DONE
    # cell 2 never started: no stage record, no probe, no artifact directory
    assert ctx.led.stage(second, "ladder").state == State.PENDING
    assert ctx.led.stage(second, "ladder").probes == []
    assert not (paths.run_dir("regate-5") / "cells" / second).exists()
    assert ctx.led.phase("cells").state != State.DONE
    assert ctx.led.phase(phases.REGATE_PHASE).data["verdict"] == (
        capgate.Verdict.DB_IMPOSED_AT_REALIZED_RATES.value)


def test_regate_failure_is_opted_out_of_by_the_same_flag(isolated_results, monkeypatch):
    monkeypatch.setenv(phases.ALLOW_DB_IMPOSED_KNEE_ENV, "1")
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.90, SLOT_B: 0.90})
    ctx = _regate_ctx("regate-6", runner)
    ctx.allow_db_imposed_knee = True
    cell = _run_first_cell(ctx)
    phases.run_capacity_regate_phase(ctx, cell)      # no raise
    rg = ctx.manifest.capacity_gate.regate
    assert rg.degraded_allowed_by == phases.ALLOW_DB_IMPOSED_KNEE_ENV


def test_regate_never_fires_a_lever(isolated_results, monkeypatch):
    """A lever fired AFTER cell 1 changes the workload for cells 2..N while cell
    1 already ran the old one, so the cells stop being comparable to each other.
    A failure here is a stop, not a lever."""
    def _boom(self):
        raise AssertionError("the re-gate must never touch LeverMachine")

    monkeypatch.setattr(capgate.LeverMachine, "try_next", _boom)
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.90, SLOT_B: 0.90})
    ctx = _regate_ctx("regate-7", runner)
    cell = _run_first_cell(ctx)
    with pytest.raises(phases.PhaseError, match="db-imposed-at-realized-rates"):
        phases.run_capacity_regate_phase(ctx, cell)
    # and the effective workload the cells run is untouched by the re-gate
    assert ctx.workload.mix.create_invoice == load_workload().mix.create_invoice


def test_regate_is_idempotent_on_resume(isolated_results):
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.30, SLOT_B: 0.30})
    ctx = _regate_ctx("regate-8", runner)
    cell = _run_first_cell(ctx)
    phases.run_capacity_regate_phase(ctx, cell)
    first = dict(ctx.led.phase(phases.REGATE_PHASE).data)
    ts = ctx.led.phase(phases.REGATE_PHASE).ts

    # a resume re-enters the phase; it must not re-evaluate or re-stamp
    ctx.manifest.capacity_gate.regate = None
    phases.run_capacity_regate_phase(ctx, cell)
    assert ctx.led.phase(phases.REGATE_PHASE).data == first
    assert ctx.led.phase(phases.REGATE_PHASE).ts == ts
    assert ctx.manifest.capacity_gate.regate is None


def test_regate_not_evaluable_when_cell_one_did_not_cross(isolated_results):
    """A cell that never crossed ran its confirm windows at a TOP RUNG, not at an
    operating point. PG's utilization there is a fact about a rate the headline
    is not taken at, and the remaining cells run at their own fitted knees --
    which this cell says nothing about."""
    runner = RegateRunner(knee_by_slot={SLOT_A: 10 ** 9, SLOT_B: 10 ** 9},
                          pg_busy={SLOT_A: 0.05, SLOT_B: 0.05})
    ctx = _regate_ctx("regate-9", runner, rungs=3)
    ctx.allow_sub_knee_ladder = True
    cell = _run_first_cell(ctx)
    assert ctx.led.stage(cell.cell_id, "fit").data["no_crossing_slots"] == list(SLOTS)
    with pytest.raises(phases.PhaseError, match="regate-not-evaluable"):
        phases.run_capacity_regate_phase(ctx, cell)
    rg = ctx.manifest.capacity_gate.regate
    assert rg.verdict == capgate.Verdict.REGATE_NOT_EVALUABLE.value
    # NOT a measured zero: the windows exist, but they answer another question
    assert rg.pg_utilization is None
    assert rg.realized_knee_rps is None
    assert "fitted no knee for any slot" in rg.detail


def test_regate_is_skipped_on_a_functional_only_run(isolated_results):
    """smoke rehearses the path and publishes nothing, exactly as stage 1 does."""
    runner = RegateRunner(knee_by_slot={SLOT_A: 10 ** 9, SLOT_B: 10 ** 9},
                          pg_busy={SLOT_A: 0.05, SLOT_B: 0.05})
    ctx = _ctx("regate-10", scale="smoke", runner=runner)
    assert ctx.profile.functional_only
    ctx.led.mark_phase("capacity_gate", State.DONE, verdict="pass")
    cell = _run_first_cell(ctx)
    phases.run_capacity_regate_phase(ctx, cell)      # records, does not raise
    assert ctx.manifest.capacity_gate.regate.verdict == (
        capgate.Verdict.REGATE_NOT_EVALUABLE.value)


def test_regate_runs_no_ladder_so_the_seed_verification_survives(isolated_results):
    """`_db_has_diverged` returns True as soon as the capacity gate leaves
    PENDING, because the create-only ladder commits real invoices. A re-gate
    that RE-RAN that ladder would permanently disable the exact-count seed
    verification for every later resume. The utilization rule needs no ladder,
    and this pins that it never grows one."""
    class NoLadder(RegateRunner):
        def measure_pg_create_knee(self, **kw):
            raise AssertionError("the re-gate must not re-run the create-only ladder")

    runner = NoLadder(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                      pg_busy={SLOT_A: 0.30, SLOT_B: 0.30})
    ctx = _regate_ctx("regate-11", runner)
    cell = _run_first_cell(ctx)
    before = runner.calls
    phases.run_capacity_regate_phase(ctx, cell)
    assert runner.calls == before          # no probe of any kind either
    assert ctx.manifest.capacity_gate.regate.verdict == (
        capgate.Verdict.PASS_AT_REALIZED_RATES.value)


def test_the_projection_rule_is_skipped_while_the_gate_ladder_never_broke(isolated_results):
    """`capacity_gate_rungs=12` from 500 rps at step 1.2 caps the create-only
    ladder at 3,715 rps. Against the DB load a realized knee implies, a
    projection re-gate off a number that only means "at least 3,715" cannot
    pass -- and a gate that always fails is one the operator opts out of as
    routine. So it is only part of the decision when the ladder actually broke.
    """
    busy = {SLOT_A: 0.30, SLOT_B: 0.30}
    knees = {SLOT_A: 1500, SLOT_B: 6000}
    lower = _regate_ctx("regate-12", RegateRunner(knee_by_slot=knees, pg_busy=busy),
                        lower_bound=True)
    phases.run_capacity_regate_phase(lower, _run_first_cell(lower))
    rg = lower.manifest.capacity_gate.regate
    assert rg.verdict == capgate.Verdict.PASS_AT_REALIZED_RATES.value
    assert rg.projection_evaluated is False and rg.projection_ok is None
    assert "lower bound" in rg.projection_skipped_because
    # the projected load IS still recorded -- as context, next to the reason it
    # did not decide anything -- and it is a load the lower bound would have
    # FAILED: 3,715.05 rps of "at least" against 3,085.71 x 1.3 = 4,011.43 rps.
    assert rg.projected_db_rps_at_realized_rates == pytest.approx(3085.714285, rel=1e-6)
    assert rg.pg_capacity_rps == pytest.approx(3715.05)
    assert rg.pg_capacity_rps < rg.projected_db_rps_at_realized_rates * rg.headroom_factor

    # ...and with a ladder that DID break, the same numbers now decide the verdict
    broke = _regate_ctx("regate-13", RegateRunner(knee_by_slot=knees, pg_busy=busy),
                        lower_bound=False)
    with pytest.raises(phases.PhaseError, match="db-imposed-at-realized-rates"):
        phases.run_capacity_regate_phase(broke, _run_first_cell(broke))
    rg2 = broke.manifest.capacity_gate.regate
    assert rg2.projection_evaluated is True and rg2.projection_ok is False
    assert rg2.utilization_ok is True          # the utilization rule was happy
    assert rg2.projected_db_rps_at_realized_rates == pytest.approx(
        rg.projected_db_rps_at_realized_rates)


def test_regate_takes_its_core_budget_from_the_gate_lever_not_the_spec_default(
    isolated_results,
):
    """`ResolvedWorkloadParams.from_workload` always initialises pg_cores to the
    bench default, so reading it back would report 3 on exactly the run where
    the pg_cores lever moved PG to 4 -- an under-counted denominator that
    inflates every utilization computed from it."""
    runner = RegateRunner(knee_by_slot={SLOT_A: 1500, SLOT_B: 6000},
                          pg_busy={SLOT_A: 0.30, SLOT_B: 0.30}, cpuset=None)
    ctx = _regate_ctx("regate-14", runner)
    ctx.led.phase("capacity_gate").data["effective"] = {
        "write_share": 0.15, "cache_hit_rate": 0.80, "pg_cores": 4,
        "env_overrides": {},
    }
    cell = _run_first_cell(ctx)
    phases.run_capacity_regate_phase(ctx, cell)
    rg = ctx.manifest.capacity_gate.regate
    assert rg.pg_cores == 4
    assert "effective pg_cores" in rg.pg_cores_source
    # the windows burned 0.30 x 3 cores x 10 s each; against a 4-core budget
    # that is 0.225, and reading the spec default back would have said 0.300.
    assert rg.pg_utilization == pytest.approx(0.225)
