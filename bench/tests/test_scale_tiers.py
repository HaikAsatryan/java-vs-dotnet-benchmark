"""The three measurement tiers, and the two dials that are not the same dial.

`scale` names the TIER; `seed_scale` names the DATASET. They coincide for `full`
and `smoke` because those tier names are also dataset names, and they must not
for `rehearsal`: seeding a reduced tier at smoke's 1k/5k rows makes the whole
read path cache-resident, which answers the heap/knee questions by construction
rather than by measurement.

The dataset name crosses two boundaries this test suite is the only check on:
`spec/workload.yaml seeding.scales` (a KeyError in the target generator) and the
Go seeder's `ParseScale` (a non-zero exit ~30 min into a run).
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from ledgerbench import phases
from ledgerbench.__main__ import app
from ledgerbench.config import config_hash, load_slo, load_workload
from ledgerbench.ledger import Ledger, State
from ledgerbench.phases import SCALES, ScaleProfile
from ledgerbench.registry import DEFAULT_SLOT_SET
from ledgerbench.util import paths

runner = CliRunner()


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "results_dir", lambda: tmp_path / "results")
    return tmp_path / "results"


# --------------------------------------------------------------------------- #
# the rehearsal tier
# --------------------------------------------------------------------------- #
def test_rehearsal_is_measurement_grade():
    """Every full-scale EVIDENCE dial is kept; only wall-clock dials are cut.

    `functional_only=True` would gate out the CO/error exclusions and both
    gate-enforcement paths, which is the difference between a reduced measured
    tier and a slow smoke run.
    """
    slo = load_slo()
    p = ScaleProfile.resolve("rehearsal", slo)
    assert p.functional_only is False
    assert p.warmup_cap_s == slo.warmup.hard_cap_seconds
    assert p.warmup_discard_s == slo.ladder.warmup_discard_seconds
    assert p.measure_s == slo.ladder.window_seconds
    assert p.warmup_min_calls_factor == 1.0
    assert p.ladder_start_rps == slo.ladder.start_rate_rps_default


def test_rehearsal_ladder_can_bracket_a_10k_knee():
    """The anti-R1-A3 assertion: the rungs must be able to REACH a real knee.

    Six geometric rungs at the spec's 1.2 top out at 1,244 rps. With a knee
    anywhere near 10k every rehearsal probe is sub-knee, so "do both stacks
    cross?" is answered `no` by construction -- the same shape as the round-1
    posture that could not satisfy its own gate.
    """
    rates = phases._ladder_rates(ScaleProfile.resolve("rehearsal", load_slo()))
    assert len(rates) == 6
    assert rates[0] <= 500
    assert rates[-1] >= 16_000


def test_rehearsal_runs_one_cell():
    p = ScaleProfile.resolve("rehearsal", load_slo())
    assert [c.cell_id for c in phases.cells_for(p)] == ["headline.mixed.gc-default"]


def test_rehearsal_keeps_the_full_working_set():
    """`body_pool_requests` is the residency dial, not a budget item.

    Cutting it re-introduces exactly the cache residency `seed_scale="full"`
    exists to prevent, because it is what decides how many distinct customers
    the create path can name. Under W20 it is a FLOOR on the shared body pool
    rather than the size of any one draw, and the pool is built once per run
    rather than once per slot -- so rehearsal keeping full's value costs less
    wall clock than it did, not more.
    """
    slo = load_slo()
    assert (
        ScaleProfile.resolve("rehearsal", slo).body_pool_requests
        == ScaleProfile.resolve("full", slo).body_pool_requests
    )


def _gate_rungs(p) -> list[int]:
    """The rates the gate ladder actually climbs.

    `phases.gate_ladder_rates` is the runner's own definition, not a copy of it:
    the sequence used to be an inline `rate *= ...` inside a docker-only method,
    where no test could reach it.
    """
    return phases.gate_ladder_rates(p, p.capacity_gate_rungs)


def test_rehearsal_gate_ladder_reaches_higher_than_fulls():
    """The rung RAISE must buy reach, not just a bigger number.

    A ladder that runs out of rungs records a LOWER BOUND, never a measured
    knee. Rungs cost nothing unspent: the gate ladder stops at the first rung
    that fails to track.
    """
    slo = load_slo()
    assert (
        _gate_rungs(ScaleProfile.resolve("rehearsal", slo))[-1]
        > _gate_rungs(ScaleProfile.resolve("full", slo))[-1]
    )


def test_the_coarse_measurement_step_does_not_coarsen_the_gate_ladder():
    """The two ladders look for different quantities and must not share a dial.

    The measurement ladder must reach a mixed knee near 10k in six rungs; the
    gate ladder must accumulate MIN_PG_KNEE_RUNGS usable rungs BELOW PG's create
    ceiling, a write ceiling in the low thousands. Sharing the dial made a
    PG ceiling under ~2500 rps yield fewer than three usable rungs, so the gate
    reported NOT MEASURED and `run-all` aborted before a single probe -- on a
    box that passes `full`'s gate comfortably. A rehearsal that fails where the
    full run succeeds is the inversion of what the tier is for.
    """
    from ledgerbench.capacity_gate import MIN_PG_KNEE_RUNGS

    slo = load_slo()
    reh = ScaleProfile.resolve("rehearsal", slo)
    full = ScaleProfile.resolve("full", slo)
    assert reh.ladder_step_factor != reh.capacity_gate_step_factor
    for ceiling in (900, 1500, 2500):
        usable = sum(1 for r in _gate_rungs(reh) if r <= ceiling)
        assert usable >= MIN_PG_KNEE_RUNGS, (
            f"PG create ceiling {ceiling} rps leaves {usable} usable gate rung(s)"
        )
        assert usable == sum(1 for r in _gate_rungs(full) if r <= ceiling)


@pytest.mark.parametrize("ceiling", [2600, 3000, 3300, 3600])
def test_rehearsal_and_full_reach_the_same_gate_verdict(ceiling):
    """The tier must not change the ANSWER, only how long it takes to get it.

    Sharing the measurement step pinned rehearsal's measured PG knee to exactly
    2000 rps for every true ceiling in [2000, 4000) -- the band containing the
    gate's own threshold (projected 4000 x db_touch 0.54 x headroom 1.3 = 2808).
    In that band rehearsal fired all three pre-registered levers, could not
    realize `cache_hit_rate` (spec pins no TTL), and aborted -- or, given a TTL,
    passed only after re-coring PG 3->4 and rewriting the mix, rehearsing a
    different workload on a different topology than the run it de-risks.
    """
    from ledgerbench.capacity_gate import RungResult, pg_knee_from_rungs

    def knee(p):
        rungs = []
        for r in _gate_rungs(p):
            achieved = float(r) if r <= ceiling else ceiling * 0.5
            rungs.append(RungResult(rate=r, achieved=achieved, p99_ms=5.0, error_rate=0.0))
            if achieved < r * 0.95:
                break
        return pg_knee_from_rungs(rungs).knee_rps

    slo = load_slo()
    assert knee(ScaleProfile.resolve("rehearsal", slo)) == knee(ScaleProfile.resolve("full", slo))


@pytest.mark.parametrize("scale", sorted(SCALES))
def test_the_gate_step_defaults_to_the_measurement_step(scale):
    p = ScaleProfile.resolve(scale, load_slo())
    if scale == "rehearsal":
        return
    assert p.capacity_gate_step_factor == p.ladder_step_factor


# --------------------------------------------------------------------------- #
# scale vs seed_scale
# --------------------------------------------------------------------------- #
def test_rehearsal_seeds_the_full_dataset():
    assert ScaleProfile.resolve("rehearsal", load_slo()).seed_scale == "full"


@pytest.mark.parametrize("scale", ["full", "smoke"])
def test_seed_scale_defaults_to_the_tier_name(scale):
    assert ScaleProfile.resolve(scale, load_slo()).seed_scale == scale


@pytest.mark.parametrize("scale", sorted(SCALES))
def test_every_tier_seeds_a_dataset_the_spec_declares(scale):
    """A seed_scale absent from the spec KeyErrors inside the target generator."""
    declared = set(load_workload().seeding.scales)
    assert ScaleProfile.resolve(scale, load_slo()).seed_scale in declared


def test_the_go_seeder_accepts_exactly_the_datasets_the_spec_declares():
    """Cross-boundary: the seeder is a separate binary with its own switch.

    A dataset name the spec declares and `seed/internal/gen/scale.go` rejects
    fails the seed phase minutes into a run, after host tuning and a DB reset.
    """
    src = (paths.repo_root() / "seed" / "internal" / "gen" / "scale.go").read_text(
        encoding="utf-8"
    )
    body = src.split("func ParseScale", 1)[1]
    accepted = set(re.findall(r'case "([^"]+)":', body))
    assert accepted == set(load_workload().seeding.scales)


def test_the_gate_ladder_consumes_the_shared_rate_definition():
    """The runner must not rebuild the sequence inline.

    An inline `rate *= profile.<something>` inside a docker-only method is
    unreachable by every test in this suite, which is how the gate ladder came
    to share the measurement ladder's step without anything noticing.
    """
    src = (paths.bench_dir() / "ledgerbench" / "live_runner.py").read_text(encoding="utf-8")
    assert "gate_ladder_rates(self.profile, rungs)" in src
    assert "rate *= " not in src


def test_every_dataset_consumer_is_wired_to_seed_scale():
    """Asserting the FIELD is not asserting the WIRING.

    Every `--scale` argv and every `scale=` keyword in these two modules feeds
    either the Go seeder or the target generator, and both resolve the name
    against `spec/workload.yaml seeding.scales`. A `profile.scale` reaching one
    of them seeds `rehearsal` at a dataset that does not exist. There is no
    legitimate reader of the TIER name in either module -- the tier reaches the
    ledger from `make_context`'s own argument, not from the profile.
    """
    for rel in ("ledgerbench/phases.py", "ledgerbench/live_runner.py"):
        src = (paths.bench_dir() / rel).read_text(encoding="utf-8")
        assert "profile.scale" not in src, f"{rel} passes the TIER where a DATASET is required"


def test_rehearsal_records_the_full_seeded_counts(isolated_results, tmp_path):
    """`_record_working_set` resolves the DATASET, and a miss is loud.

    Round 1 read this through `.get(...)`, so an unresolvable name recorded
    `seeded_invoices: null` and the residency section became unreadable with
    nothing to say why.
    """
    from ledgerbench.manifest import new_manifest

    wl = load_workload()
    profile = ScaleProfile.resolve("rehearsal", load_slo())
    led = Ledger.load_or_create(
        "rehearsal-ws", slot_set=DEFAULT_SLOT_SET, config_hash="c",
        rng_seed=wl.seeding.seed, scale="rehearsal",
    )
    ctx = phases.RunContext(
        led=led, profile=profile, workload=wl, slo=load_slo(),
        manifest=new_manifest("rehearsal-ws"), runner=None, run_id="rehearsal-ws",
    )
    (paths.run_dir("rehearsal-ws") / "cells").mkdir(parents=True, exist_ok=True)

    phases._record_working_set(ctx)

    ws = ctx.manifest.working_set
    assert (ws.seeded_invoices, ws.seeded_customers) == (
        wl.seeding.scales["full"].invoices,
        wl.seeding.scales["full"].customers,
    )


def test_seed_scale_survives_the_body_pool_replace_on_resume():
    """`make_context` re-builds the profile with dataclasses.replace on resume.

    A `seed_scale` recomputed from `scale` there would silently re-point a
    resumed rehearsal at the smoke dataset.
    """
    from dataclasses import replace

    p = ScaleProfile.resolve("rehearsal", load_slo())
    assert replace(p, body_pool_requests=250_000).seed_scale == "full"


# --------------------------------------------------------------------------- #
# the builder table is the single source of truth
# --------------------------------------------------------------------------- #
def test_unknown_scale_names_every_supported_tier():
    """The message is built FROM the table, so it cannot drift from it."""
    with pytest.raises(ValueError) as exc:
        ScaleProfile.resolve("medium", load_slo())
    msg = str(exc.value)
    assert "unknown scale" in msg
    for name in SCALES:
        assert name in msg


@pytest.mark.parametrize("scale", sorted(SCALES))
def test_every_declared_tier_builds_and_names_itself(scale):
    assert ScaleProfile.resolve(scale, load_slo()).scale == scale


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_unknown_scale_is_a_clean_cli_error(isolated_results):
    """Exit 2 with a usage message, not an uncaught ValueError.

    `make_context` sits outside every `try/except PhaseError` in the CLI, so
    before the guard a mistyped tier surfaced as a traceback.
    """
    res = runner.invoke(app, ["run-all", "--scale", "medium"])
    assert res.exit_code == 2
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "medium" in res.output
    for name in SCALES:
        assert name in res.output


def test_scale_help_names_every_supported_tier():
    """A hand-written tier list in the help is one more place to forget a tier."""
    res = runner.invoke(app, ["run-all", "--help"])
    out = " ".join(res.output.split())
    for name in SCALES:
        assert name in out


def test_status_never_reports_the_dataset_as_the_tier(isolated_results):
    """`status` renders phase data keys verbatim.

    Writing the dataset under the key `scale` made a rehearsal run print
    `scale=full` on the seed phase -- which an operator reads as the run's tier,
    the one thing it is not.
    """
    led = Ledger.load_or_create(
        "rehearsal-status", slot_set=DEFAULT_SLOT_SET, config_hash="c",
        rng_seed=1, scale="rehearsal",
    )
    led.mark_phase("seed", State.DONE, seed_scale="full")

    res = runner.invoke(app, ["status", "rehearsal-status"])
    out = " ".join(res.output.split())
    assert "seed_scale=full" in out
    assert "scale=full" not in out.replace("seed_scale=full", "")


def _ctx_capturing_runner(monkeypatch) -> dict:
    """Run the CLI for real up to the first probe, capturing the built context.

    Stubs `phases.run_all` -- the first thing that needs a daemon -- and NOT
    `phases.make_context`. Stubbing `make_context` stops coverage at the string
    the CLI resolved and proves nothing about the profile that string builds,
    which is the whole point of the tier.
    """
    seen: dict = {}

    def _fake_run_all(ctx):
        seen["ctx"] = ctx
        raise phases.PhaseError("stop before any probe")

    monkeypatch.setattr(phases, "run_all", _fake_run_all)
    return seen


def test_resume_adopts_the_pinned_scale(isolated_results, monkeypatch):
    """The ledger pins the scale and REFUSES a mismatch.

    A fixed `--scale full` default made every scale-omitted resume of a
    rehearsal run fail with a RuntimeError naming a scale nobody typed.
    """
    led = Ledger.load_or_create(
        "rehearsal-x", slot_set=DEFAULT_SLOT_SET, config_hash=config_hash(),
        rng_seed=1, scale="rehearsal",
    )
    led.mark_phase("doctor", State.DONE)
    seen = _ctx_capturing_runner(monkeypatch)

    res = runner.invoke(app, ["run-all", "--run-id", "rehearsal-x"])

    ctx = seen.get("ctx")
    assert ctx is not None, res.output
    # End to end: the CLI's resolved string built the REHEARSAL profile, with
    # every dial the tier is defined by, not merely a matching string.
    assert ctx.profile.scale == "rehearsal"
    assert ctx.profile.seed_scale == "full"
    assert ctx.profile.functional_only is False
    assert ctx.profile.measure_s == load_slo().ladder.window_seconds
    assert ctx.led.scale == "rehearsal"


def test_explicit_scale_still_wins_over_the_pin(isolated_results, monkeypatch):
    """And when it disagrees with the pin, the LEDGER refuses -- audibly.

    `Ledger.load_or_create` raises RuntimeError naming both scales. That guard
    is what makes `--scale` safe to override at all, so it is asserted here
    rather than assumed.
    """
    Ledger.load_or_create(
        "rehearsal-y", slot_set=DEFAULT_SLOT_SET, config_hash=config_hash(),
        rng_seed=1, scale="rehearsal",
    )
    seen = _ctx_capturing_runner(monkeypatch)

    res = runner.invoke(app, ["run-all", "--run-id", "rehearsal-y", "--scale", "full"])

    assert "ctx" not in seen                      # refused before any context
    assert isinstance(res.exception, RuntimeError)
    assert "rehearsal" in str(res.exception) and "full" in str(res.exception)


def test_resume_picks_the_most_recent_run_not_the_alphabetically_last(isolated_results):
    """The run-id prefix must not become the resume ORDER.

    `latest_unfinished` sorted run ids lexicographically, which was chronological
    only while ids led with the timestamp. Leading with the scale makes that sort
    order by TIER: `full-...` < `rehearsal-...` < `smoke-...`. A full run that
    crashed twenty hours in would lose `--resume` to any older unfinished smoke
    run -- and `--resume` then adopts THAT ledger's pinned scale, so the measured
    run would have silently continued at smoke numerics.
    """
    old = Ledger.load_or_create(
        "smoke-20260822T090000-h", slot_set=DEFAULT_SLOT_SET, config_hash="c",
        rng_seed=1, scale="smoke",
    )
    old.created_utc = "2026-08-22T09:00:00.000000Z"
    old.save()
    new = Ledger.load_or_create(
        "full-20260822T150000-h", slot_set=DEFAULT_SLOT_SET, config_hash="c",
        rng_seed=1, scale="full",
    )
    new.created_utc = "2026-08-22T15:00:00.000000Z"
    new.save()

    latest = Ledger.latest_unfinished()
    assert latest is not None
    assert latest.run_id == "full-20260822T150000-h"
    assert latest.scale == "full"
    # `ledgerbench status` reads the same ordering, and it is the first thing an
    # operator runs after a crash -- naming the wrong run there is how the wrong
    # run gets resumed.
    assert Ledger.latest().run_id == "full-20260822T150000-h"


@pytest.mark.parametrize("scale", sorted(SCALES))
def test_a_new_run_id_leads_with_its_scale(scale):
    from ledgerbench.__main__ import _new_run_id

    assert _new_run_id(scale).startswith(f"{scale}-")


@pytest.mark.parametrize("scale", ["smoke", "rehearsal"])
def test_gitignore_excludes_every_non_publishable_tier(scale):
    """The prefix rules and the run-id format are one contract.

    Round 1 named runs timestamp-first, so `results/smoke-*/` matched nothing
    and every functional rehearsal's artifacts were staged for commit next to
    measured ones.
    """
    from ledgerbench.__main__ import _new_run_id

    rules = (paths.repo_root() / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert f"results/{scale}-*/" in rules
    assert _new_run_id(scale).startswith(f"{scale}-")


def test_the_published_tier_is_not_excluded():
    from ledgerbench.__main__ import _new_run_id

    rules = (paths.repo_root() / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "results/full-*/" not in rules
    assert _new_run_id("full").startswith("full-")


# --------------------------------------------------------------------------- #
# W20: the working-set dial changed MEANING, so its name and its pin changed too
# --------------------------------------------------------------------------- #
def test_the_predecessor_working_set_env_var_is_refused_not_ignored(monkeypatch):
    """`LEDGERBENCH_TARGET_COUNT` sized the ONE targets file a run replayed.

    Nothing replays now: the same integer sizes the shared body pool, which is a
    different quantity with a different effect on the workload. An operator
    whose shell still exports the old name would otherwise get the 1,000,000
    default while believing they had set the working set, which is the silent
    half of a rename. The refusal names the replacement.
    """
    monkeypatch.setenv("LEDGERBENCH_TARGET_COUNT", "250000")
    monkeypatch.delenv(phases.BODY_POOL_ENV, raising=False)
    with pytest.raises(ValueError) as exc:
        ScaleProfile.resolve("full", load_slo())
    assert "LEDGERBENCH_TARGET_COUNT is no longer read" in str(exc.value)
    assert phases.BODY_POOL_ENV in str(exc.value)
    # and the tier resolves normally once the stale name is gone
    monkeypatch.delenv("LEDGERBENCH_TARGET_COUNT")
    assert ScaleProfile.resolve("full", load_slo()).body_pool_requests == 1_000_000


def test_the_body_pool_size_is_pinned_in_the_ledger_under_its_own_schema(
    isolated_results, monkeypatch,
):
    """The pin has to move with the MEANING, or a resume swaps the working set.

    `Ledger.target_count` held the replayed file's request count. Repurposing
    that field as the body-pool size without a schema bump would let a v3 ledger
    resume into this build with 1,000,000 read as a pool floor -- exactly the
    mid-run working-set swap the pinning was added to prevent.
    """
    from ledgerbench import ledger as ledmod

    # >= 4, not == 4: W20 is the version the rename landed at, and later work
    # keeps bumping. What this pins is that the rename got A bump and that a
    # ledger written before it is refused BY VERSION rather than coerced.
    assert ledmod.LEDGER_SCHEMA_VERSION >= 4
    assert "target_count" not in ledmod.Ledger.model_fields
    monkeypatch.setenv(phases.BODY_POOL_ENV, "40000")
    ctx = phases.make_context(run_id="bp-1", scale="full")
    assert ctx.led.body_pool_requests == 40_000
    assert ctx.profile.body_pool_requests == 40_000
    # a resume under a different environment replays the PIN, not the shell
    monkeypatch.setenv(phases.BODY_POOL_ENV, "900000")
    resumed = phases.make_context(run_id="bp-1", scale="full")
    assert resumed.led.body_pool_requests == 40_000
    assert resumed.profile.body_pool_requests == 40_000
