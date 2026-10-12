"""Post-run analyze wiring: synthetic ledger -> fits.json + report.md.

Builds a small hand-computable ledger (1 cell, a 2-rung ladder whose isotonic
SLO crossing is exactly 1500 rps, plus a 3-block confirm stage) and asserts the
analyze entry point fits the expected knee bracket, emits the report table, and
exits cleanly on the no-data path. The knee math is verified independently in
bench/analysis/tests/test_knee.py; here we test the ledger -> artifact wiring.

The second half of this module pins the HONESTY behaviour the report depends on,
each with a hand-computable fixture:

  - under-warmed windows are excluded from the resource roll-up (and the used vs
    excluded counts are published), exactly as the fit input excludes them;
  - a slot whose windows are ALL flagged is published as posture evidence,
    labelled as such, never as a gated measurement;
  - the knee is fitted ONCE PER SLOT over that slot's own rungs (there is no
    pooled fit any more), and a slot that never crosses inside the ladder is
    reported with the ladder's top rung instead of an extrapolation;
  - the paired verdict carries the offered rate it was computed at;
  - the CPU-ms/request-vs-rate curve (with per-slot CV) is emitted;
  - no pooled knee survives anywhere -- not in report.md, not in fits.json, and
    not as a function or class name in `analyze.py`;
  - CPU-ms/request is request-weighted (total CPU / total requests), the stage
    composition of the aggregated windows is published, and the SLO verdict
    states its mean-of-window-p99 rule alongside a per-window met count;
  - the all-flagged posture note derives its cause from the cell's OWN knobs and
    claims none when the knobs do not imply one.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

# analyze_run fits a knee via the scientific stack (`uv sync` installs it); skip
# the whole module (not just collect) if it is somehow absent, the way the
# analysis suite guards its own tests.
_MISSING = [m for m in ("numpy", "scipy", "pandas") if importlib.util.find_spec(m) is None]
pytestmark = pytest.mark.skipif(
    bool(_MISSING), reason=f"analysis stack missing: {', '.join(_MISSING)}"
)

from ledgerbench import analyze, cgroups, compose, registry, warmup
# analyze_run refuses a ledger whose pinned hash is not the LIVE spec's
# (SpecDriftError), so synthetic ledgers pin the real hash of the tree they
# run in; the drift path itself is tested explicitly.
from ledgerbench.config import config_hash as _live_config_hash
from ledgerbench.ledger import (
    REGATE_PHASE,
    CellRecord,
    Ledger,
    ProbeRecord,
    State,
    StageRecord,
    probe_key,
)
from ledgerbench.registry import DEFAULT_SLOT_SET, get_slot, get_slot_set

# Slot identity comes from the registry, never from a literal. SUBJECT is the
# ratio NUMERATOR and REFERENCE the DENOMINATOR (`registry.SlotSet.reference`),
# so every fixture below states which side of a published ratio it is feeding.
# Round 1's code computed the reciprocal of its declared ratio.
_SET = get_slot_set(DEFAULT_SLOT_SET)
SLOTS = _SET.slots
SUBJECT = _SET.subject
REFERENCE = _SET.reference
SUBJECT_LABEL = get_slot(SUBJECT).label
REFERENCE_LABEL = get_slot(REFERENCE).label

# A CORRECT report says "pooled" in exactly one place: the DB-CPU caveats render
# `registry.LanguageSpec.db_driver_pooling_note`, and Npgsql's names the reset it
# issues "on essentially every pooled CHECKOUT". That is a CONNECTION pool; the
# deleted knee fit pooled PROBES. A bare `"pooled" not in md.lower()` passes
# today only because the synthetic ledgers below yield no DB-CPU section, so it
# would start failing for a correct reason the moment a fixture gains DB samples
# -- and it is the pooled-KNEE prose these tests are actually guarding against.
_POOLING_NOTES = tuple(
    spec.db_driver_pooling_note
    for spec in registry.LANGUAGES.values()
    if spec.db_driver_pooling_note
)


def _pooled_knee_text(md: str) -> str:
    """Everything in the report mentioning "pooled" that is NOT a driver note."""
    for note in _POOLING_NOTES:
        md = md.replace(note, "")
    return "\n".join(ln for ln in md.splitlines() if "pooled" in ln.lower())


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    # git-backed: `analyze_run` ends in `checksums.write_run_checksums`, whose
    # scope is `git ls-files`' answer inside the run directory.
    from tests.conftest import git_backed_results

    return git_backed_results(tmp_path, monkeypatch)


def _done_probe(rate: int, block: int, slot: str) -> ProbeRecord:
    return ProbeRecord(
        block=block, slot_order=list(SLOTS), slot=slot, rate=rate,
        state=State.DONE, artifact=f"{probe_key(rate, block, slot)}.json",
        achieved=float(rate), error_rate=0.0,
    )


def _ladder_data(*, crossed_by_slot, bracket_by_slot, ceiling=None, **rest) -> dict:
    """The v5 ladder-stage `data` shape, in ONE place.

    W24 replaced the flat `crossed` / `bracket` with per-slot maps, because the
    ladder now climbs per slot; a fixture that still wrote the flat keys would be
    testing `analyze` against a ledger the runner can no longer produce.
    """
    rates = rest.get("rates") or []
    return {
        "crossed_by_slot": crossed_by_slot,
        "bracket_by_slot": bracket_by_slot,
        "rung_ceiling_rps": ceiling if ceiling is not None else (rates[-1] if rates else None),
        "ceiling_reached_without_crossing": [
            sid for sid, v in crossed_by_slot.items() if v is None
        ],
        **rest,
    }


def _confirm_data(rates_by_slot: dict, **rest) -> dict:
    """The v5 confirm-stage `data` shape: each slot's OWN operating rate."""
    return {
        "confirm_rates_by_slot": rates_by_slot,
        "common_rate_rps": min(rates_by_slot.values()),
        **rest,
    }


def _ladder_stage(flagged_slot: str | None = None) -> StageRecord:
    """A 2-rung ladder whose isotonic p99 crosses 20 ms at exactly 1500 rps.

    rung 1000 rps -> p99 10 ms; rung 2000 rps -> 30 ms. Both slots carry
    the same value per rung so the rung mean is exact and the linear crossing is
    hand-computable: 1000 + (20-10)/(30-10) * (2000-1000) = 1500.

    `flagged_slot` marks EVERY probe of one slot under-warmed.
    """
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    series: dict[str, list[float]] = {}
    for rate, val in ((1000, 10.0), (2000, 30.0)):
        for block in (0, 1):
            for slot in SLOTS:
                key = probe_key(rate, block, slot)
                rec.probes.append(_done_probe(rate, block, slot))
                p99[key] = val
                under_warmed[key] = slot == flagged_slot
                # flat, stationary per-second series (no positive trend)
                series[key] = [5.0, 5.0, 5.0, 5.0, 5.0]
    rec.data = _ladder_data(
        crossed_by_slot={sid: 2000 for sid in SLOTS},
        bracket_by_slot={sid: [1000, 2000] for sid in SLOTS},
        p99_ms=p99, under_warmed=under_warmed, p99_series=series,
        rates=[1000, 2000],
    )
    return rec


def _confirm_stage() -> StageRecord:
    """A 3-block confirm at one rate; the REFERENCE slot's p99 is ~10% below the
    SUBJECT's, so the paired verdict is decisive (reference better on a
    lower-is-better latency metric)."""
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    rate = 1500
    for block in range(3):
        for slot, val in ((REFERENCE, 9.0), (SUBJECT, 10.0)):
            key = probe_key(rate, block, slot)
            rec.probes.append(_done_probe(rate, block, slot))
            p99[key] = val
            under_warmed[key] = False
    rec.data = _confirm_data({sid: rate for sid in SLOTS},
                             p99_ms=p99, under_warmed=under_warmed)
    return rec


def _synthetic_ledger(run_id: str, *, cost_windows: bool = False) -> Ledger:
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    cell = CellRecord(
        cell_id="headline.mixed.gc-default",
        ladder=_ladder_stage(),
        confirm=_confirm_stage(),
    )
    led.cells.append(cell)
    led.save()
    if cost_windows:
        # The paired verdict is on COST since W24, so it needs the per-window
        # cgroup summaries; the p99 map alone no longer feeds it. The SUBJECT
        # spends 3x the REFERENCE's CPU per request, decisively.
        _write_cost_confirm_windows(run_id, 1500, blocks=3)
    return led


def _write_cost_confirm_windows(
    run_id: str, rate: int, *, blocks: int = 3, cell="headline.mixed.gc-default",
    sub_us=300_000, ref_us=100_000, rates_by_slot=None,
) -> None:
    """Confirm windows with a decisive per-request CPU gap and non-zero variance."""
    base = paths_run_dir(run_id) / "cells" / cell / "confirm"
    rates = rates_by_slot or {SUBJECT: rate, REFERENCE: rate}
    for block in range(blocks):
        for slot, us in ((SUBJECT, sub_us), (REFERENCE, ref_us)):
            bd = base / f"rate{rates[slot]}" / f"block{block}"
            _write_probe_window(bd, slot, reqs=1000, cpu_us=us + 1000 * block,
                                anon=150_000_000 if slot == SUBJECT else 30_000_000,
                                peak=180_000_000)


def test_analyze_fits_expected_knee_bracket(isolated_results):
    _synthetic_ledger("syn-1")
    result = analyze.analyze_run("syn-1")

    assert result.analyzable
    assert len(result.cells) == 1
    # BOTH slots carry the same rung values in this fixture, so both fits land
    # on the same crossing -- which is the point: the knee is now per slot even
    # where the two agree.
    for slot, knee in result.cells[0].knee_by_slot.items():
        assert knee.crossed is True, slot
        # isotonic crossing of 20 ms between (1000,10) and (2000,30) is exactly 1500
        assert abs(knee.knee_rps - 1500.0) < 1.0
        # CI bounds straddle the point estimate
        assert knee.ci_low_rps <= knee.knee_rps <= knee.ci_high_rps

    # fits.json is machine-readable with the same numbers
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert fits["run_id"] == "syn-1"
    assert fits["slo_ms"] == 20.0
    cell = fits["cells"][0]
    for slot in SLOTS:
        assert abs(cell["knee_by_slot"][slot]["knee_rps"] - 1500.0) < 1.0
        assert cell["knee_by_slot"][slot]["n_rungs"] == 2


def test_analyze_report_md_contains_tables(isolated_results):
    _synthetic_ledger("syn-2", cost_windows=True)
    result = analyze.analyze_run("syn-2")
    md = result.report_path.read_text(encoding="utf-8")
    assert "# Ledgerline analysis report" in md
    assert "Knee fits" in md
    assert "headline.mixed.gc-default" in md
    # the knee table renders the crossing rate
    assert "1500" in md
    # paired section renders the decisive reference-better COST verdict
    assert f"{REFERENCE_LABEL} better" in md
    assert "COST verdict" in md


def test_paired_metric_is_cost_not_p99(isolated_results):
    """The headline verdict is CPU-ms/request, and p99 is not a verdict at all.

    At their own knees both stacks sit at ~20 ms BY CONSTRUCTION, so a paired
    p99 verdict is a guaranteed tie -- an artefact of the design rather than a
    result. p99 becomes a per-slot conformance readout; cost becomes the verdict.
    """
    _synthetic_ledger("syn-3", cost_windows=True)
    result = analyze.analyze_run("syn-3")
    cell = result.cells[0]
    paired = cell.paired
    assert paired is not None
    assert paired.metric == "cpu_ms_per_req"
    assert paired.verdict_scope == analyze.SCOPE_OWN_KNEE
    assert paired.n_blocks == 3
    # the SUBJECT burns 3x the CPU per request: lower-is-better -> reference
    # better, decisive. `mean_diff_pct` is 100*(subject - reference)/reference.
    assert paired.verdict == "reference_better"
    assert paired.mean_diff_pct > 0
    assert paired.direction.subject == SUBJECT
    assert paired.direction.reference == REFERENCE
    # ...and memory-per-request and $/M are published ALONGSIDE it, never
    # instead of it, so the reader gets the whole cost vector.
    metrics = {v.metric for v in cell.paired_verdicts
               if v.verdict_scope == analyze.SCOPE_OWN_KNEE}
    assert metrics == set(analyze.PAIRED_METRICS)
    assert "p99_ms" not in metrics


def test_analyze_stationarity_summary_flat_series_passes(isolated_results):
    _synthetic_ledger("syn-4")
    result = analyze.analyze_run("syn-4")
    stat = result.cells[0].stationarity
    assert stat["available"] is True
    assert stat["any_fail"] is False  # flat series -> no positive trend


def test_no_crossing_ladder_reports_no_knee(isolated_results):
    led = Ledger(run_id="syn-nc", config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    for rate in (1000, 2000):
        for block in (0, 1):
            for slot in SLOTS:
                key = probe_key(rate, block, slot)
                rec.probes.append(_done_probe(rate, block, slot))
                p99[key] = 5.0  # sub-knee everywhere
                under_warmed[key] = False
    rec.data = _ladder_data(
        crossed_by_slot={sid: None for sid in SLOTS},
        bracket_by_slot={sid: [2000] for sid in SLOTS},
        p99_ms=p99, under_warmed=under_warmed, rates=[1000, 2000],
    )
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default", ladder=rec))
    led.save()

    result = analyze.analyze_run("syn-nc")
    assert result.analyzable  # the cell IS analyzed (ladder ran), just no knee
    for slot, knee in result.cells[0].knee_by_slot.items():
        assert knee.crossed is False, slot
        assert knee.knee_rps is None
    md = result.report_path.read_text(encoding="utf-8")
    assert "no crossing" in md


def test_no_analyzable_cells_when_no_ladder_done(isolated_results):
    led = Ledger(run_id="syn-empty", config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    # a cell whose ladder never completed -> not analyzable
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default"))
    led.save()
    result = analyze.analyze_run("syn-empty")
    assert result.analyzable is False
    assert result.cells == []
    # artifacts are still written (empty cells list) so the path is reportable
    assert result.fits_path.exists() and result.report_path.exists()


def test_missing_ledger_raises(isolated_results):
    with pytest.raises(FileNotFoundError):
        analyze.analyze_run("does-not-exist")


# --------------------------------------------------------------------------- #
# resource cost (cgroup CPU/mem per request, read from the cells/ tree)
# --------------------------------------------------------------------------- #
def _write_probe_window(
    blockdir, slot, *, reqs, cpu_us, anon, peak, under_warmed=False,
    failed_gates=(), waived_gates=(), starved_gates=(),
    non_enforcing_gates=(), throttled=0, oom=0,
    swap=0, swap_peak=0, db_cpu_us=None, db_swap=0, db_swap_peak=0,
):
    """Write the per-window cgroup + latency + warmup summaries one probe emits.

    Keyed by SLOT id, not language: two slots of one language would otherwise
    write the same three filenames into the same block directory and overwrite
    each other while the run still looked valid.

    `db_cpu_us` writes the co-sampled PostgreSQL window. It is slot-keyed for
    the same reason: the block directory is shared by BOTH slots of the block,
    so one `cpu_mem_postgres.summary.json` per block would be overwritten by the
    second probe and its DB cost published as both slots'. `db_swap` /
    `db_swap_peak` are that window's swap reads: the DB CPU-ms/request column is
    published off this file, so its swap counts toward the roll-up's verdict.
    """
    blockdir.mkdir(parents=True, exist_ok=True)
    (blockdir / f"cpu_mem_{slot}.summary.json").write_text(json.dumps({
        "label": slot, "role": cgroups.ROLE_SUT, "persistent": False,
        "enabled": True,
        "cpu_usage_delta_usec": cpu_us, "nr_throttled_delta": throttled,
        "throttled_usec_delta": 0, "mem_anon_p99": anon,
        "mem_peak_start": 0, "mem_peak_end": peak,
        "mem_file_end": 0, "oom_kill_delta": oom,
        "swap_current_start": 0, "swap_current_end": swap,
        "swap_current_max": swap,
        "swap_peak_start": 0, "swap_peak_end": swap_peak,
        "swap_peak_max": swap_peak, "note": "",
    }), encoding="utf-8")
    if db_cpu_us is not None:
        (blockdir / f"cpu_mem_{slot}.{compose.SVC_POSTGRES}.summary.json").write_text(
            json.dumps({
                "label": compose.SVC_POSTGRES, "role": cgroups.ROLE_DATABASE,
                "persistent": True, "enabled": True,
                "cpu_usage_delta_usec": db_cpu_us,
                "nr_throttled_delta": 0, "throttled_usec_delta": 0,
                "oom_kill_delta": 0, "mem_current_p99": 4_000_000_000,
                "swap_current_max": db_swap, "swap_peak_max": db_swap_peak,
                "cpuset_effective": "8-10", "note": "",
            }), encoding="utf-8")
    (blockdir / f"{slot}.summary.json").write_text(json.dumps({
        "latencies": {"99th": 9_000_000}, "requests": reqs, "success": 1,
        "status_codes": {"200": reqs}, "errors": [],
    }), encoding="utf-8")
    gates = ["per_endpoint_min_calls", "p99_flat", "p999_flat", "gc_steady", "pools_warm"]
    skip = {*waived_gates, *non_enforcing_gates}
    records = [
        {"name": g, "passed": g not in failed_gates,
         "status": analyze.STATUS_MEASURED, "enforced": True}
        for g in gates if g not in skip
    ]
    # A waived gate is recorded, never absent: `passed: false` so it can never
    # read as a pass, and `status` naming which layer waived it. `enforced` is
    # written on EVERY record here because `WarmupResult.save()` writes it on
    # every record and analyze now reads it: since B2/W21 `enforced` is
    # `enforce AND status in ENFORCING_STATUSES`, so a fixture carrying only
    # `status` can no longer express what a real record says.
    records += [
        {"name": g, "passed": False, "status": analyze.STATUS_NOT_APPLICABLE,
         "enforced": False,
         "detail": {"cell_id": "synthetic", "rationale": "SYNTHETIC", "knob": None}}
        for g in waived_gates
    ]
    # `insufficient_data` is the one non-`measured` status that ENFORCES: the
    # gate was applied and found nothing to apply itself to, so it flags.
    records += [
        {"name": g, "passed": False, "status": warmup.STATUS_INSUFFICIENT_DATA,
         "enforced": True, "detail": {"usable_samples": 0}}
        for g in starved_gates
    ]
    # Applied, recorded, and switched off by the PER-GATE `enforce` flag: the
    # `p999_flat` shape, where the status enforces but the gate does not.
    records += [
        {"name": g, "passed": False, "status": warmup.STATUS_INSUFFICIENT_DATA,
         "enforce": False, "enforced": False,
         "detail": {"flatness": {"reason": warmup.REASON_QUANTILE_UNRESOLVED}}}
        for g in non_enforcing_gates
    ]
    (blockdir / f"{slot}.warmup.json").write_text(json.dumps({
        "slot": slot, "under_warmed": under_warmed,
        "final_db_pool_total": 24, "final_db_pool_max": 24,
        "gates": records,
    }), encoding="utf-8")


def test_resource_cost_from_cells_tree(isolated_results):
    """A sub-knee confirm window where the SUBJECT slot burns 3x the CPU and 5x
    the memory of the REFERENCE, read from the cgroup summaries.

    The ratio is the direction assertion: `SlotSet.reference` is the
    denominator, so 0.3 / 0.1 must publish as 3.00x. Round 1 divided the other
    way while its plan declared this one, so this pins the direction.
    """
    run_id = "syn-res"
    _synthetic_ledger(run_id)  # knee crosses at 1500 rps; confirm declares 1500
    cell = "headline.mixed.gc-default"
    # The windows sit at the rate the confirm stage DECLARED for both slots --
    # the roll-up selects its operating point, it does not bound it, so a window
    # at any other rate is not this cell's headline. reference: 100 us/req over
    # 1000 reqs -> 0.1 CPU-ms/req; subject: 300 us/req -> 0.3; anon 30/150 MB.
    base = isolated_results / run_id / "cells" / cell / "confirm" / "rate1500"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000,
                            anon=30_000_000, peak=60_000_000)
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res is not None and res.available is True
    assert res.operating_rates == [1500]
    assert abs(res.by_slot[REFERENCE].cpu_ms_per_req - 0.1) < 1e-9
    assert abs(res.by_slot[SUBJECT].cpu_ms_per_req - 0.3) < 1e-9
    assert abs(res.cpu_ratio - 3.0) < 1e-9              # subject / reference
    assert abs(res.anon_ratio - 5.0) < 1e-9
    assert res.ratio_direction.subject == SUBJECT
    assert res.ratio_direction.reference == REFERENCE
    assert res.by_slot[REFERENCE].db_pool_total == 24
    assert res.by_slot[REFERENCE].db_pool_max == 24
    assert res.by_slot[REFERENCE].throttle_clean
    assert res.by_slot[REFERENCE].oom_clean

    md = result.report_path.read_text(encoding="utf-8")
    assert "Resource cost at the operating rate" in md
    assert "3.00x" in md                                 # the subject:reference CPU ratio
    assert _SET.ratio_label() in md                      # ...under a header that says so
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert fits["cells"][0]["resource"]["available"] is True
    assert fits["cells"][0]["resource"]["ratio_direction"] == {
        "subject": SUBJECT, "reference": REFERENCE, "label": _SET.ratio_label(),
    }


def test_resource_cost_absent_when_no_cells_tree(isolated_results):
    """No cgroup summaries on disk (the default synthetic ledger) -> the report
    honestly says not-captured rather than fabricating a number."""
    _synthetic_ledger("syn-nores")
    result = analyze.analyze_run("syn-nores")
    res = result.cells[0].resource
    assert res is not None and res.available is False
    md = result.report_path.read_text(encoding="utf-8")
    assert "Not available in this run: no SUT cgroup-v2 resource summaries" in md


# --------------------------------------------------------------------------- #
# under-warmed exclusion: the roll-up must apply the SAME filter as the fit
# --------------------------------------------------------------------------- #
def _confirm_stage_at(rate: int, n_blocks: int, *, flagged: set[str] = frozenset()) -> StageRecord:
    """A confirm stage at ONE rate; `flagged` holds probe keys to mark under-warmed."""
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    for block in range(n_blocks):
        for slot, val in ((REFERENCE, 9.0), (SUBJECT, 10.0)):
            key = probe_key(rate, block, slot)
            rec.probes.append(_done_probe(rate, block, slot))
            p99[key] = val
            under_warmed[key] = key in flagged
    rec.data = _confirm_data({sid: rate for sid in SLOTS},
                             p99_ms=p99, under_warmed=under_warmed)
    return rec


def _confirm_at_rates(rates_by_slot: dict[str, int], n_blocks: int) -> StageRecord:
    """A confirm stage at each slot's OWN declared rate, plus the common-rate extra.

    The W24 block shape `phases._confirm_members` builds: every slot at its own
    rate, and every slot whose rate is above `min` again at that min, inside the
    same block.
    """
    rec = StageRecord(state=State.DONE)
    common = min(rates_by_slot.values())
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    members = [(sid, r) for sid, r in rates_by_slot.items()]
    members += [(sid, common) for sid, r in rates_by_slot.items() if r != common]
    for block in range(n_blocks):
        for slot, rate in members:
            key = probe_key(rate, block, slot)
            rec.probes.append(_done_probe(rate, block, slot))
            p99[key] = (9.0 if slot == REFERENCE else 10.0) + 0.1 * block
            under_warmed[key] = False
    rec.data = _confirm_data(dict(rates_by_slot), p99_ms=p99, under_warmed=under_warmed)
    return rec


def _ledger_with_confirm(
    run_id: str, confirm: StageRecord, ladder: StageRecord | None = None,
) -> Ledger:
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    led.cells.append(CellRecord(
        cell_id="headline.mixed.gc-default",
        ladder=ladder if ladder is not None else _ladder_stage(),
        confirm=confirm,
    ))
    led.save()
    return led


def test_resource_rollup_excludes_under_warmed_windows(isolated_results):
    """A flagged window must not enter the CPU/memory means, and both counts
    (used vs excluded) must be published so the exclusion is visible.

    Three REFERENCE windows at a sub-knee rate: two warm at 0.1 CPU-ms/req and
    one FLAGGED at 1.0. Including it would report 0.4; the honest number is 0.1.
    """
    run_id = "syn-uw"
    rate = 1000
    _ledger_with_confirm(
        run_id,
        _confirm_stage_at(rate, 3, flagged={probe_key(rate, 2, REFERENCE)}),
    )
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(3):
        bd = base / f"block{block}"
        flagged = block == 2
        _write_probe_window(
            bd, REFERENCE, reqs=1000, cpu_us=1_000_000 if flagged else 100_000,
            anon=30_000_000, peak=60_000_000, under_warmed=flagged,
            failed_gates=("gc_steady",) if flagged else (),
        )
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    res = analyze.analyze_run(run_id).cells[0].resource
    ref, sub = res.by_slot[REFERENCE], res.by_slot[SUBJECT]
    assert res.available is True
    assert ref.n_windows == 2 and ref.n_flagged_excluded == 1
    assert ref.posture_only is False
    assert abs(ref.cpu_ms_per_req - 0.1) < 1e-9         # NOT 0.4
    assert abs(res.cpu_ratio - 3.0) < 1e-9
    assert sub.n_windows == 3 and sub.n_flagged_excluded == 0

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "windows used / flagged-excluded" in md
    assert "2 / 1" in md


def test_the_flagging_gate_is_read_from_the_ledger_not_the_sidecar(isolated_results):
    """B4: `ledger.json` now names the gate, and it is the authority when it does.

    The ledger and the sidecar are made to DISAGREE here, which cannot happen in
    a run (both are written from one `ProbeOutcome`), because it is the only way
    to show which one the report reads. The fallback is the case the neighbouring
    posture test covers: its probe records carry no gate list, so its counts come
    off the sidecars.
    """
    run_id = "syn-ledger-gates"
    rate = 1000
    flagged = {probe_key(rate, b, REFERENCE) for b in range(2)}
    confirm = _confirm_stage_at(rate, 2, flagged=flagged)
    for probe in confirm.probes:
        probe.gates_evaluated = ["p99_flat", "gc_steady"]
        probe.gates_failed = (
            ["p99_flat"]
            if probe_key(probe.rate, probe.block, probe.slot) in flagged else []
        )
    _ledger_with_confirm(run_id, confirm)
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, under_warmed=True,
                            failed_gates=("gc_steady",))
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    res = analyze.analyze_run(run_id).cells[0].resource
    assert res.by_slot[REFERENCE].flagged_gate_counts == {"p99_flat": 2}


def test_all_flagged_slot_is_published_as_posture_not_measurement(isolated_results):
    """Every window of one slot flagged: the numbers are still emitted, but
    marked posture evidence with the failed gate named."""
    run_id = "syn-posture"
    rate = 1000
    flagged = {probe_key(rate, b, REFERENCE) for b in range(2)}
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2, flagged=flagged))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, under_warmed=True, failed_gates=("gc_steady",))
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res.available is True
    assert res.by_slot[REFERENCE].posture_only is True
    assert res.by_slot[REFERENCE].n_windows == 2
    assert res.by_slot[REFERENCE].flagged_gate_counts == {"gc_steady": 2}
    assert res.by_slot[SUBJECT].posture_only is False

    md = result.report_path.read_text(encoding="utf-8")
    assert "all 2 probe windows for this slot were flagged under-warmed" in md
    assert "POSTURE EVIDENCE, not as a gated measurement" in md
    assert "gc_steady 2x" in md
    # no knob in this cell is registered as making the gate unsatisfiable, so no
    # cause may be manufactured for it
    assert "No knob this cell declares is registered as making any of these gates" in md


def test_posture_note_derives_its_cause_from_the_registry_predicate(
    isolated_results, monkeypatch
):
    """A causal clause is printed only for a cell that actually declares the
    knob, and only for the gate that knob explains.

    The derivation now reads `registry.unsatisfiable_gates_for` -- the SAME
    predicate `phases.assert_gate_sets_satisfiable` refuses cells with -- rather
    than a table local to this module. That is W22's point: round 1's only home
    for this knowledge was the report generator, so the earliest it could act
    was a prose clause after the runtime had been spent (~30 h, R1-A3).

    The entry is SYNTHETIC (the real table is empty, see test_registry) so the
    derivation machinery stays covered either way. The cell is a synthetic one
    that declares `DOTNET_GCDynamicAdaptationMode=0`, with a second failed gate
    the knob does NOT explain.
    """
    import dataclasses

    entry = registry.UnsatisfiableGate(
        gate="gc_steady", env_var="DOTNET_GCDynamicAdaptationMode", value="0",
        why=("SYNTHETIC TEST ENTRY: this cell pins the GC heap-count adaptation "
             "off, so the gate cannot be satisfied by construction"),
    )
    slot = registry.SLOTS[REFERENCE]
    monkeypatch.setitem(registry.SLOTS, REFERENCE, dataclasses.replace(
        slot, language=dataclasses.replace(slot.language, unsatisfiable_gates=(entry,)),
    ))
    # G6 deleted the real gc-generous cell (D6, one headline cell); the
    # derivation needs a cell that DECLARES the knob, so it is synthetic now.
    from ledgerbench import phases as phases_mod
    syn_cell = phases_mod.Cell(
        cell_id="synthetic.gc-posture",
        extra_env={"DOTNET_GCDynamicAdaptationMode": "0"},
        note="synthetic (posture-cause derivation)",
    )
    monkeypatch.setattr(phases_mod, "CELLS", [*phases_mod.CELLS, syn_cell])
    run_id = "syn-posture-knob"
    rate = 1000
    flagged = {probe_key(rate, b, REFERENCE) for b in range(2)}
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    led.cells.append(CellRecord(
        cell_id="synthetic.gc-posture", ladder=_ladder_stage(),
        confirm=_confirm_stage_at(rate, 2, flagged=flagged),
    ))
    led.save()
    base = isolated_results / run_id / "cells" / "synthetic.gc-posture" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(
            bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000, peak=60_000_000,
            under_warmed=True,
            failed_gates=("gc_steady", "p99_flat") if block == 0 else ("gc_steady",),
        )
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "SYNTHETIC TEST ENTRY" in md
    assert "`gc_steady` failed in 2 of 2 windows" in md
    # the unrelated gate is named as unexplained, not swept into the GC story
    assert "The remaining flagged gate(s), `p99_flat` 1x, are NOT explained" in md


def test_the_report_names_the_gates_a_posture_was_not_held_to(isolated_results):
    """A waived gate must be visible in the REPORT, not only in the sidecars.

    R1-B1 is a run that declared six warmup gates, evaluated four, and left the
    other two's absence discoverable nowhere. A gate a posture is not held to is
    the same absence with a reason, so it gets stated: the windows behind these
    numbers are gated by one gate fewer than the spec declares.

    The declared set here is EMPTY (round 2 declares no posture override), so
    the cross-check must report the disagreement rather than quietly print the
    recorded half.
    """
    run_id = "syn-waived"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, waived_gates=("gc_steady",))
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    ref = result.cells[0].resource.by_slot[REFERENCE]
    assert ref.waived_gate_counts == {"gc_steady": 2}
    assert ref.flagged_gate_counts == {}       # a waived gate never flags

    md = result.report_path.read_text(encoding="utf-8")
    assert "Warmup gates a posture was NOT held to" in md
    assert "declared nothing, recorded across this cell's probe windows" in md
    assert "`gc_steady` 2x" in md
    assert "INCONSISTENT" in md                # nothing declares this waiver


def test_a_gate_that_flagged_for_want_of_evidence_is_named_as_the_cause(
    isolated_results,
):
    """The reader half of `insufficient_data`, and it is not symmetric with the
    writer half.

    `_failed_gate_names` filtered on `status == measured`, which is right for
    the three statuses that mean "not applied" and WRONG for this one. Left as
    it was, a probe flagged because its `/runtime-stats` never parsed would be
    flagged in the ledger and then reported as having no failed gate -- the
    report blaming a missing artifact for a cause that is on disk, which is the
    same defect `test_a_flag_with_no_failed_gate_names_the_crashed_warmup_attack`
    exists for, one status over.
    """
    run_id = "syn-starved"
    rate = 1000
    flagged = {probe_key(rate, b, REFERENCE) for b in range(2)}
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2, flagged=flagged))
    base = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
            / "confirm" / f"rate{rate}")
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, under_warmed=True,
                            starved_gates=("gc_steady",))
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    ref = result.cells[0].resource.by_slot[REFERENCE]
    assert ref.flagged_gate_counts == {"gc_steady": 2}
    assert ref.waived_gate_counts == {}
    md = result.report_path.read_text(encoding="utf-8")
    assert "no per-window gate detail on disk" not in md


def test_no_waiver_bullet_when_nothing_was_waived(isolated_results):
    """The expected round-2 state: no posture override anywhere, no bullet."""
    run_id = "syn-no-waiver"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000)
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "Warmup gates a posture was NOT held to" not in md


# --------------------------------------------------------------------------- #
# per-slot knee fits: one per slot, over that slot's own rungs
# --------------------------------------------------------------------------- #
def _split_ladder_stage() -> StageRecord:
    """3 rungs where the SUBJECT crosses inside the ladder and the REFERENCE never does.

    rung -> (reference p99, subject p99): 1000 -> (5, 10); 2000 -> (8, 30);
    3000 -> (10, 50). The subject crosses 20 ms at exactly 1500; the reference
    tops out at 10 ms (no crossing).
    """
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    for rate, (ref_v, sub_v) in ((1000, (5.0, 10.0)), (2000, (8.0, 30.0)), (3000, (10.0, 50.0))):
        for block in (0, 1):
            for slot, val in ((REFERENCE, ref_v), (SUBJECT, sub_v)):
                key = probe_key(rate, block, slot)
                rec.probes.append(_done_probe(rate, block, slot))
                p99[key] = val
                under_warmed[key] = False
    rec.data = _ladder_data(
        crossed_by_slot={SUBJECT: 2000, REFERENCE: None},
        bracket_by_slot={SUBJECT: [1000, 2000], REFERENCE: [3000]},
        p99_ms=p99, under_warmed=under_warmed, rates=[1000, 2000, 3000],
    )
    return rec


def test_the_slot_that_never_crossed_is_named_and_never_extrapolated(isolated_results):
    """Each slot is fitted over its own rungs, and a sub-knee slot SAYS SO.

    There is no pooled fit (W24). A slot that never crosses the SLO inside its
    ladder is named in the artifact; in round 1 Go never crossed in any measured
    ladder and nothing in the artifact said it.
    """
    run_id = "syn-split"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default", ladder=_split_ladder_stage()))
    led.save()

    result = analyze.analyze_run(run_id)
    cell = result.cells[0]
    sub = cell.knee_by_slot[SUBJECT]
    ref = cell.knee_by_slot[REFERENCE]
    # the subject crosses 20 ms between (1000,10) and (2000,30) -> exactly 1500
    assert sub.crossed is True and abs(sub.knee_rps - 1500.0) < 1.0
    # the reference never reaches the SLO: reported as such, with the top rung
    assert ref.crossed is False and ref.knee_rps is None
    assert ref.max_rung_rps == 3000.0 and abs(ref.max_rung_p99_ms - 10.0) < 1e-9
    # ...and both refits reproduce the LADDER's own per-slot verdict.
    assert sub.crossed_agrees_with_ledger is True
    assert ref.crossed_agrees_with_ledger is True

    md = result.report_path.read_text(encoding="utf-8")
    assert "ONE PER SLOT" in md
    assert "no crossing within the measured ladder (max rung 3000 rps" in md
    assert f"{REFERENCE_LABEL} reached the 3000 rps rung ceiling WITHOUT crossing" in md
    assert "LOWER BOUND on its knee, never an estimate of it" in md
    # the deleted pooled row and its prose must not come back
    assert _pooled_knee_text(md) == ""
    # the banner is EXPLAINED in the prose but never STAMPED on a row here
    assert "DISAGREES WITH THE LADDER |" not in md

    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    by_slot = fits["cells"][0]["knee_by_slot"]
    assert abs(by_slot[SUBJECT]["knee_rps"] - 1500.0) < 1.0
    assert by_slot[REFERENCE]["knee_rps"] is None
    assert "knee_pooled" not in fits["cells"][0]


def test_the_per_slot_fit_input_is_the_runners_own_exclusion(isolated_results):
    """The analyzer fits what the run measured, through ONE definition of it.

    `analyze.py`'s module docstring claims the analysis fits exactly the rung
    shape the run bracketed. It is only true while there is one exclusion rule:
    `_rungs_by_rate_for_slot` delegates its warm case to `phases._rung_values`,
    the code path the live ladder itself stopped on. A second copy is how that
    claim quietly stops holding.

    The behavioural half below cannot see that copy: an INLINED but identical
    reimplementation of the rule agrees with the delegation by construction and
    walks straight through every assertion in this module. So the structural
    property is asserted over the SOURCE first -- the warm branch calls
    `phases._rung_values`, and it does not itself touch the `under_warmed` /
    `excluded` maps that rule is made of -- and the behaviour is checked after.
    """
    import ast
    import pathlib as _pl

    from ledgerbench import analyze as _an
    from ledgerbench import phases

    fn = next(
        n for n in ast.walk(ast.parse(
            _pl.Path(_an.__file__).read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "_rungs_by_rate_for_slot"
    )
    delegations = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_rung_values"
    ]
    assert len(delegations) == 1, "the warm case must DELEGATE, exactly once"
    restated = {
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    } & {"under_warmed", "excluded"}
    assert not restated, f"the exclusion rule is restated here: {sorted(restated)}"

    run_id = "syn-excl"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    lad = _ladder_stage()
    # flag ONE probe of the SUBJECT at the lower rung, and gate-exclude another
    flagged = probe_key(1000, 0, SUBJECT)
    excluded = probe_key(1000, 1, SUBJECT)
    lad.data["under_warmed"][flagged] = True
    lad.data["excluded"] = {excluded: "achieved/offered 0.5000 outside +/-2%"}
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default", ladder=lad))
    led.save()

    # the runner's view: rung 1000 keeps no SUBJECT probe at all
    assert 1000 not in phases._rung_values(lad, SUBJECT)[0]
    assert 1000 in phases._rung_values(lad, REFERENCE)[0]

    result = analyze.analyze_run(run_id)
    sub = result.cells[0].knee_by_slot[SUBJECT]
    ref = result.cells[0].knee_by_slot[REFERENCE]
    # the SUBJECT is down to ONE rung, so no fit; the REFERENCE still has two
    assert sub.crossed is False and sub.n_rungs == 1 and sub.n_rungs_dropped == 1
    assert "cannot fit a per-slot knee" in sub.note
    assert ref.crossed is True and ref.n_rungs == 2


def test_no_pooled_knee_survives_anywhere(isolated_results):
    """No pooled knee in report.md, in fits.json, or in `analyze.py`'s names.

    A report must not explain what a pooled fit pooled when there is no pooled
    fit. This asserts over the SOURCE as well as the output, so restoring the
    pooled fit fails here.
    """
    import ast
    import pathlib as _pl

    from ledgerbench import analyze as _an

    _synthetic_ledger("syn-nopool")
    result = analyze.analyze_run("syn-nopool")
    md = result.report_path.read_text(encoding="utf-8")
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))

    assert _pooled_knee_text(md) == ""
    assert "knee_pooled" not in json.dumps(fits)
    assert all(k.scope in SLOTS for c in result.cells for k in c.knee_by_slot.values())

    # The within-probe sense of "pooled" -- vegeta's report p99 for a whole
    # window, which `test_probe_p99_drives_crossing_not_per_second_series`
    # defends -- was renamed to `probe_p99` FIRST, precisely so this grep cannot
    # hit a passing test that looks like it protects the pooled fit.
    src = _pl.Path(_an.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.ClassDef))
    }
    assert not [n for n in names if "pooled" in n.lower()], names


def test_knee_interval_is_labelled_non_inferential_with_the_reason(isolated_results):
    """`ci_valid` is False for every ladder-stage fit (reps_per_rung < confirm_reps);
    the report must say what the printed interval IS instead of calling it a CI."""
    _synthetic_ledger("syn-ci")
    md = analyze.analyze_run("syn-ci").report_path.read_text(encoding="utf-8")
    assert "NOT a valid bootstrap CI here" in md
    assert "ladder.confirm_reps" in md
    assert "| 5% CI |" not in md  # the old mislabelled header must be gone


# --------------------------------------------------------------------------- #
# per-language p99 at the offered rate, and the paired verdict's rate label
# --------------------------------------------------------------------------- #
def _split_knee_ladder() -> StageRecord:
    """A ladder whose two slots cross at DIFFERENT rates: 1500 and 2000.

    `_ladder_stage` crosses at 1500 for BOTH slots, so pairing it with
    `_per_slot_rate_confirm` (which confirms the SUBJECT at 2000) describes a
    run that cannot exist: `phases.run_fit` writes
    `confirm_rates_by_slot[sid] = int(round(that slot's knee))` and
    `_confirm_members` runs exactly those rates. The per-slot-rate tests need
    the ladder the confirm rates came from, or the operating point they assert
    the headline is taken at is not the slot's crossing rate at all.

    REFERENCE 1000->10, 2000->30 ms: crosses at 1000 + (20-10)/(30-10)*1000 =
    1500. SUBJECT 1000->10, 2000->20 ms: crosses AT the rung, so exactly 2000.
    """
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    series: dict[str, list[float]] = {}
    for rate, (ref_v, sub_v) in ((1000, (10.0, 10.0)), (2000, (30.0, 20.0)),
                                 (3000, (50.0, 40.0))):
        for block in (0, 1):
            for slot, val in ((REFERENCE, ref_v), (SUBJECT, sub_v)):
                key = probe_key(rate, block, slot)
                rec.probes.append(_done_probe(rate, block, slot))
                p99[key] = val
                under_warmed[key] = False
                series[key] = [5.0, 5.0, 5.0, 5.0, 5.0]
    rec.data = _ladder_data(
        crossed_by_slot={REFERENCE: 2000, SUBJECT: 2000},
        bracket_by_slot={REFERENCE: [1000, 2000], SUBJECT: [1000, 2000]},
        p99_ms=p99, under_warmed=under_warmed, p99_series=series,
        rates=[1000, 2000, 3000],
    )
    return rec


def _per_slot_rate_confirm() -> StageRecord:
    """The W24 confirm shape: each slot at ITS OWN knee, plus the common rate.

    REFERENCE's knee is 1500, SUBJECT's is 2000, so `r_common` is 1500 and the
    block carries a THIRD probe -- SUBJECT at 1500 -- for the common-rate
    verdict. The REFERENCE meets the 20 ms SLO at both rates, the SUBJECT at
    neither, and the SUBJECT is worse at the higher rate.
    """
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    for rate, (ref_v, sub_v) in ((1500, (10.0, 30.0)), (2000, (12.0, 50.0))):
        for block in range(3):
            for slot, val in ((REFERENCE, ref_v), (SUBJECT, sub_v)):
                key = probe_key(rate, block, slot)
                rec.probes.append(_done_probe(rate, block, slot))
                p99[key] = val + 0.1 * block      # non-zero variance for the t-CI
                under_warmed[key] = False
    rec.data = _confirm_data({REFERENCE: 1500, SUBJECT: 2000},
                             p99_ms=p99, under_warmed=under_warmed)
    return rec


def test_paired_verdict_survives_per_language_confirm_rates(isolated_results):
    """THE R1-A5 STRUCTURAL GUARD: a verdict at java@1500 vs dotnet@2000.

    Pairing must not require both slots inside one (rate, block) cell: with the
    two slots confirming at different rates such a pairing matches nothing,
    every rate is skipped, and the report renders "Not available in this run"
    with no error and no warning.
    """
    run_id = "syn-rates"
    _ledger_with_confirm(run_id, _per_slot_rate_confirm(), _split_knee_ladder())
    _write_cost_confirm_windows(run_id, 0, blocks=3,
                                rates_by_slot={REFERENCE: 1500, SUBJECT: 2000})
    result = analyze.analyze_run(run_id)
    cell = result.cells[0]

    own = [v for v in cell.paired_verdicts
           if v.verdict_scope == analyze.SCOPE_OWN_KNEE
           and v.metric == "cpu_ms_per_req"]
    assert len(own) == 1
    assert own[0].n_blocks == 3
    assert own[0].rates_by_slot == {REFERENCE: 1500, SUBJECT: 2000}
    assert own[0].ci_pct == 95.0 and own[0].margin_pct == 5.0

    md = result.report_path.read_text(encoding="utf-8")
    assert "95% CI (paired t)" in md                  # confidence level, not the margin
    assert "equiv. margin" in md
    # the silent-zero this test exists to catch
    assert "Not available in this run: no completed confirm-stage paired blocks" not in md

    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    rates = [v["rates_by_slot"] for v in fits["cells"][0]["paired_verdicts"]
             if v["verdict_scope"] == analyze.SCOPE_OWN_KNEE]
    assert all(r == {REFERENCE: 1500, SUBJECT: 2000} for r in rates)


def test_common_rate_verdict_is_labelled_and_distinct(isolated_results):
    """Both scopes are published and can never be conflated.

    `own_knee` is the headline the metric is defined as; `common_rate` is the
    confound-free contrast, because per-request cost is not rate-independent so
    an own-knee comparison confounds language with position on the cost-vs-rate
    curve. They are different numbers and `verdict_scope` is what keeps them
    apart in fits.json.
    """
    run_id = "syn-scopes"
    _ledger_with_confirm(run_id, _per_slot_rate_confirm(), _split_knee_ladder())
    # the SUBJECT's window at the COMMON rate (1500) is cheaper per request than
    # at its own knee (2000), so the two scopes must not agree by accident.
    _write_cost_confirm_windows(run_id, 0, blocks=3,
                                rates_by_slot={REFERENCE: 1500, SUBJECT: 2000})
    _write_cost_confirm_windows(run_id, 0, blocks=3, sub_us=150_000,
                                rates_by_slot={REFERENCE: 1500, SUBJECT: 1500})
    result = analyze.analyze_run(run_id)
    cell = result.cells[0]

    scopes = {v.verdict_scope for v in cell.paired_verdicts}
    assert scopes == {analyze.SCOPE_OWN_KNEE, analyze.SCOPE_COMMON_RATE}
    common = next(v for v in cell.paired_verdicts
                  if v.verdict_scope == analyze.SCOPE_COMMON_RATE
                  and v.metric == "cpu_ms_per_req")
    own = next(v for v in cell.paired_verdicts
               if v.verdict_scope == analyze.SCOPE_OWN_KNEE
               and v.metric == "cpu_ms_per_req")
    # the common-rate verdict states ONE rate for both slots
    assert set(common.rates_by_slot.values()) == {1500}
    assert len(set(own.rates_by_slot.values())) == 2
    # and it is a genuinely different measurement, not a relabelled copy
    assert common.mean_by_slot[SUBJECT] != own.mean_by_slot[SUBJECT]
    # the headline stays the own-knee CPU verdict
    assert cell.paired is own

    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert {v["verdict_scope"] for v in fits["cells"][0]["paired_verdicts"]} == scopes


def test_paired_verdict_is_labelled_below_the_pre_registered_block_floor(isolated_results):
    """A 2-block verdict used to render identically to a 10-block one.

    `_paired_for_cell` skipped a rate at `len(cols) < 2` with no floor against
    `spec/slo.yaml ladder.confirm_reps`, so the only difference between a verdict
    carrying the pre-registered evidence and one carrying a fifth of it was the
    `blocks (n)` column, which reads as a detail rather than a caveat.
    """
    run_id = "syn-floor"
    _ledger_with_confirm(run_id, _confirm_stage_at(1500, 2))
    _write_cost_confirm_windows(run_id, 1500, blocks=2)
    result = analyze.analyze_run(run_id)
    v = result.cells[0].paired
    assert v is not None
    assert v.n_blocks == 2 and v.min_blocks == 10        # spec/slo.yaml confirm_reps
    assert v.under_min_blocks is True

    md = result.report_path.read_text(encoding="utf-8")
    assert "BELOW the pre-registered 10" in md
    assert "read the verdict as directional only" in md


def test_solo_blocks_are_excluded_by_construction_and_counted_apart(isolated_results):
    """A one-member block is not pairable BY CONSTRUCTION, and that is a
    different fact from a block that lost a probe to warm exclusion.

    The old pairing dropped both through the same `all(sid in pair)` key miss, so
    the report could not tell "never pairable" from "lost a measurement".
    """
    run_id = "syn-solo"
    rec = _confirm_stage_at(1500, 3)
    # block 2 becomes a SOLO block: one member, `slot_order` of length 1.
    rec.probes = [pr for pr in rec.probes if not (pr.block == 2 and pr.slot == SUBJECT)]
    for pr in rec.probes:
        if pr.block == 2:
            pr.slot_order = [pr.slot]
    _ledger_with_confirm(run_id, rec)
    _write_cost_confirm_windows(run_id, 1500, blocks=3)
    result = analyze.analyze_run(run_id)

    v = result.cells[0].paired
    assert v is not None
    assert v.n_blocks == 2                   # blocks 0 and 1
    assert v.n_blocks_solo == 1              # block 2, by slot_order
    assert v.n_blocks_incomplete == 0        # and NOT counted as a lost probe
    md = result.report_path.read_text(encoding="utf-8")
    assert "1 excluded as solo (one-member) blocks" in md


def test_cpu_per_request_is_request_weighted_and_states_its_denominator(isolated_results):
    """CPU-ms/request must be total CPU / total requests, not an unweighted mean
    of per-window ratios: otherwise the request count published beside it is
    not its denominator and the label is a lie.

    Two REFERENCE windows: 1000 reqs at 0.1 CPU-ms/req and 9000 reqs at 0.2.
    The unweighted mean of the ratios is 0.15; the honest per-request cost over
    the aggregated windows is 1900 CPU-ms / 10000 requests = 0.19.
    """
    run_id = "syn-weighted"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    _write_probe_window(base / "block0", REFERENCE, reqs=1000, cpu_us=100_000,
                        anon=30_000_000, peak=60_000_000)
    _write_probe_window(base / "block1", REFERENCE, reqs=9000, cpu_us=1_800_000,
                        anon=30_000_000, peak=60_000_000)
    for block in range(2):
        _write_probe_window(base / f"block{block}", SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    ref = result.cells[0].resource.by_slot[REFERENCE]
    assert ref.requests == 10_000
    assert abs(ref.cpu_ms_total - 1900.0) < 1e-9
    assert abs(ref.cpu_ms_per_req - 0.19) < 1e-9           # weighted, NOT 0.15
    assert abs(ref.cpu_ms_per_req_unweighted - 0.15) < 1e-9

    md = result.report_path.read_text(encoding="utf-8")
    assert "THE AGGREGATION RULE" in md
    assert "REQUEST-WEIGHTED" in md
    assert "requests serviced (the CPU denominator)" in md
    # the old, wrong label must be gone
    assert "the denominator of CPU-ms/request): " not in md
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    res = fits["cells"][0]["resource"]
    assert res["by_slot"][REFERENCE]["cpu_ms_per_req_aggregation"].startswith(
        "request-weighted"
    )
    assert res["cpu_ratio_unweighted"] is not None


def test_db_cpu_per_request_is_request_weighted(isolated_results):
    """The DB column obeys THE aggregation rule, not a second one of its own.

    Two REFERENCE windows: 1000 reqs at 0.05 DB-CPU-ms/req and 9000 reqs at
    0.15. The unweighted mean of the ratios is 0.10; the honest per-request DB
    cost over the aggregated windows is 1400 CPU-ms / 10000 requests = 0.14.
    A duplicated helper here would quietly re-introduce the equal-weight mean
    the report's "THE AGGREGATION RULE" paragraph says does not exist.
    """
    run_id = "syn-dbcpu"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    _write_probe_window(base / "block0", REFERENCE, reqs=1000, cpu_us=100_000,
                        anon=30_000_000, peak=60_000_000, db_cpu_us=50_000)
    _write_probe_window(base / "block1", REFERENCE, reqs=9000, cpu_us=1_800_000,
                        anon=30_000_000, peak=60_000_000, db_cpu_us=1_350_000)
    for block in range(2):
        _write_probe_window(base / f"block{block}", SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000, db_cpu_us=100_000)

    result = analyze.analyze_run(run_id)
    ref = result.cells[0].resource.by_slot[REFERENCE]
    assert ref.db_n_windows == 2
    assert abs(ref.db_cpu_ms_total - 1400.0) < 1e-9
    assert abs(ref.db_cpu_ms_per_req - 0.14) < 1e-9        # weighted, NOT 0.10
    assert abs(ref.total_cpu_ms_per_req - (0.19 + 0.14)) < 1e-9

    # the ratio points the declared way: subject / reference
    res = result.cells[0].resource
    sub = res.by_slot[SUBJECT]
    assert abs(res.db_cpu_ratio - sub.db_cpu_ms_per_req / ref.db_cpu_ms_per_req) < 1e-9

    md = result.report_path.read_text(encoding="utf-8")
    assert "DB CPU-ms / request (postgres cgroup)" in md
    assert "app + DB CPU-ms / request" in md
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    agg = fits["cells"][0]["resource"]["by_slot"][REFERENCE]["db_cpu_ms_per_req_aggregation"]
    assert agg.startswith("request-weighted")


def test_db_cpu_per_request_uses_only_the_windows_that_carry_a_db_sample(
    isolated_results,
):
    """Numerator and denominator over the SAME subset.

    A full request denominator against a partial DB numerator understates the
    DB cost by exactly the fraction of missing windows, and looks entirely
    plausible while doing it.
    """
    run_id = "syn-dbpartial"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    # block0 has a DB sample, block1 does not.
    _write_probe_window(base / "block0", REFERENCE, reqs=1000, cpu_us=100_000,
                        anon=30_000_000, peak=60_000_000, db_cpu_us=200_000)
    _write_probe_window(base / "block1", REFERENCE, reqs=1000, cpu_us=100_000,
                        anon=30_000_000, peak=60_000_000)
    for block in range(2):
        _write_probe_window(base / f"block{block}", SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000, db_cpu_us=100_000)

    ref = analyze.analyze_run(run_id).cells[0].resource.by_slot[REFERENCE]
    assert ref.requests == 2000           # the APP denominator is both windows
    assert ref.db_n_windows == 1
    # 200 CPU-ms over the 1000 requests of the window that HAS the sample.
    assert abs(ref.db_cpu_ms_per_req - 0.2) < 1e-9
    # ...not 200 / 2000 = 0.1, which is what a mismatched denominator gives.
    assert abs(ref.db_cpu_ms_per_req - 0.1) > 1e-9


def test_db_cpu_absent_is_reported_not_fabricated(isolated_results):
    """No PostgreSQL window on disk -> null, and every SUT row survives.

    Two failure modes in one test. Adding the DB read to `_window_rows`' drop
    guard would delete the entire resource section for any run without a PG
    sampler; defaulting the value to 0.0 would publish a database that cost
    nothing.
    """
    run_id = "syn-nodb"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            _write_probe_window(base / f"block{block}", slot, reqs=1000, cpu_us=cpu,
                                anon=30_000_000, peak=60_000_000)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res.available is True                       # the SUT rows survive
    for sid in (REFERENCE, SUBJECT):
        lr = res.by_slot[sid]
        assert lr.n_windows == 2                       # every window still counted
        assert lr.cpu_ms_per_req == lr.cpu_ms_per_req  # not nan
        assert lr.db_cpu_ms_per_req is None            # null, NEVER 0.0
        assert lr.db_cpu_ms_total is None
        assert lr.db_n_windows == 0
        assert lr.total_cpu_ms_per_req is None
    assert res.db_cpu_ratio is None

    md = result.report_path.read_text(encoding="utf-8")
    assert "not measured" in md
    assert "0.000" not in md.split("DB CPU-ms / request")[1].split("\n")[0]
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert fits["cells"][0]["resource"]["by_slot"][REFERENCE]["db_cpu_ms_per_req"] is None


def test_db_cpu_is_attributed_to_the_slot_that_ran_the_probe(isolated_results):
    """Two slots share ONE block directory; their DB costs must not merge.

    `_artifact_dir` takes no slot component, so a per-block
    `cpu_mem_postgres.summary.json` would be written by the first probe and
    overwritten by the second -- and a blockdir-level read would then stamp one
    slot's DB cost onto both. Two identical DB columns read as a finding, which
    is why this is the test that pins the slot-keyed name.
    """
    run_id = "syn-dbattr"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000,
                            anon=30_000_000, peak=60_000_000, db_cpu_us=50_000)
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000, db_cpu_us=400_000)
        # both slots' DB windows coexist in the SAME directory
        names = {p.name for p in bd.iterdir()}
        assert f"cpu_mem_{REFERENCE}.{compose.SVC_POSTGRES}.summary.json" in names
        assert f"cpu_mem_{SUBJECT}.{compose.SVC_POSTGRES}.summary.json" in names

    res = analyze.analyze_run(run_id).cells[0].resource
    assert abs(res.by_slot[REFERENCE].db_cpu_ms_per_req - 0.05) < 1e-9
    assert abs(res.by_slot[SUBJECT].db_cpu_ms_per_req - 0.40) < 1e-9
    assert res.by_slot[REFERENCE].db_cpu_ms_per_req != res.by_slot[SUBJECT].db_cpu_ms_per_req


def test_db_cpu_attribution_table_is_keyed_by_cell_rate_and_slot(isolated_results):
    """The re-key, wired. Not per-endpoint: a cgroup delta is a container number."""
    from analysis.report import DB_CPU_ROW_KEYS

    run_id = "syn-dbtable"
    _synthetic_ledger(run_id)
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "ladder"
    for rate in (500, 1000):
        for slot, cpu, db in ((REFERENCE, 100_000, 50_000), (SUBJECT, 300_000, 400_000)):
            _write_probe_window(base / f"rate{rate}" / "block0", slot, reqs=1000,
                                cpu_us=cpu, anon=30_000_000, peak=60_000_000,
                                db_cpu_us=db)

    result = analyze.analyze_run(run_id)
    rows = analyze._db_cpu_rows(result.cells)
    assert {r["offered_rps"] for r in rows} == {500, 1000}
    assert {r["slot"] for r in rows} == {REFERENCE, SUBJECT}
    assert all(set(r) == set(DB_CPU_ROW_KEYS) for r in rows)
    # NOT keyed by endpoint: no row carries a path, and nothing claims one.
    assert not any("endpoint" in k for r in rows for k in r)

    md = result.report_path.read_text(encoding="utf-8")
    assert "App vs database CPU, per ladder rung" in md
    assert "offered_rps" in md
    for word in ("autovacuum", "checkpointer", "WAL writer"):
        assert word in md            # the whole-container caveat is stated
    assert "not per-endpoint" in md.lower() or "NOT per endpoint" in md


def test_db_attribution_publishes_each_columns_own_denominator(isolated_results):
    """The column headed `requests` was publishing a WINDOW count.

    Each rung in a real run has several windows of many requests, so the row
    printed a number smaller than its own denominator by the window size and
    neither CPU-ms/request figure could be reproduced from the row that carried
    it. There are TWO denominators, not one: `_cpu_ms_per_req` takes numerator
    and denominator over the SAME subset, so the DB figure divides only the
    requests of the DB-sampled windows.
    """
    run_id = "syn-db-denom"
    _synthetic_ledger(run_id)
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "ladder"
    # rate 1000: both REFERENCE windows carry app CPU, only block0 carries a DB
    # sample, so the two denominators must differ by exactly one window.
    for block, db in ((0, 50_000), (1, None)):
        _write_probe_window(base / "rate1000" / f"block{block}", REFERENCE, reqs=1000,
                            cpu_us=100_000, anon=30_000_000, peak=60_000_000,
                            db_cpu_us=db)

    result = analyze.analyze_run(run_id)
    rows = [r for r in analyze._db_cpu_rows(result.cells)
            if r["offered_rps"] == 1000 and r["slot"] == REFERENCE]
    assert len(rows) == 1
    row = rows[0]
    assert row["windows"] == 1                    # windows carrying a DB sample
    assert row["requests"] == 2000                # app denominator: both windows
    assert row["db_requests"] == 1000             # DB denominator: the sampled one
    # each figure times its OWN denominator is that cgroup's CPU-ms over the rung
    assert abs(row["app_cpu_ms_per_req"] * row["requests"] - 200.0) < 1e-6
    assert abs(row["db_cpu_ms_per_req"] * row["db_requests"] - 50.0) < 1e-6


def test_throttle_and_oom_verdicts_are_window_deltas_not_lifetime_latches(
    isolated_results,
):
    """`throttle_clean` / `oom_clean` read the DELTA keys.

    Against a lifetime counter both verdicts latch: once any throttle has ever
    happened in a cgroup that outlives a probe, every later window reads dirty.
    And a window whose counters could not be read at all must not be counted as
    clean -- `all()` over an empty set is True.
    """
    run_id = "syn-latch"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        _write_probe_window(base / f"block{block}", REFERENCE, reqs=1000, cpu_us=100_000,
                            anon=30_000_000, peak=60_000_000)
        _write_probe_window(base / f"block{block}", SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000,
                            throttled=1 if block == 0 else 0)

    res = analyze.analyze_run(run_id).cells[0].resource
    assert res.by_slot[REFERENCE].throttle_clean is True
    assert res.by_slot[SUBJECT].throttle_clean is False
    assert res.by_slot[REFERENCE].n_windows_without_counters == 0

    # A window with NO readable counter is not evidence of cleanliness.
    run_id2 = "syn-nocounters"
    _ledger_with_confirm(run_id2, _confirm_stage_at(rate, 2))
    base2 = isolated_results / run_id2 / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            bd = base2 / f"block{block}"
            _write_probe_window(bd, slot, reqs=1000, cpu_us=cpu, anon=30_000_000,
                                peak=60_000_000)
            doc = json.loads((bd / f"cpu_mem_{slot}.summary.json").read_text())
            doc["nr_throttled_delta"] = None
            doc["oom_kill_delta"] = None
            (bd / f"cpu_mem_{slot}.summary.json").write_text(json.dumps(doc))

    result2 = analyze.analyze_run(run_id2)
    res2 = result2.cells[0].resource
    assert res2.by_slot[REFERENCE].n_windows_without_counters == 2
    assert res2.by_slot[REFERENCE].n_windows_without_oom_counter == 2
    # THE verdicts, not just the counts. `None == 0` is False, so the pre-fix
    # form failed safe by accident; taking `all()` over the readable rows alone
    # inverted that to fail-open with nothing printed to say so.
    assert res2.by_slot[REFERENCE].throttle_clean is False
    assert res2.by_slot[REFERENCE].oom_clean is False
    assert res2.to_dict()["by_slot"][SUBJECT]["throttle_clean"] is False
    assert res2.to_dict()["by_slot"][SUBJECT]["oom_clean"] is False
    # and the shortfall is RENDERED: it was computed, stored in the JSON and
    # printed nowhere, so the reader saw "counters zero" with no caveat.
    md2 = result2.report_path.read_text(encoding="utf-8")
    assert "CPU throttle counters NOT ESTABLISHED" in md2
    assert "cgroup OOM events NOT ESTABLISHED" in md2


def test_stage_mix_asymmetry_is_published_and_quantified(isolated_results):
    """One slot keeps a soak window the other does not: the two published CPU
    numbers then aggregate different stage mixes, so the composition must be a
    row of the table and the alternative weighting's ratio must be printed."""
    run_id = "syn-stagemix"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    cellbase = isolated_results / run_id / "cells" / "headline.mixed.gc-default"
    for block in range(2):
        bd = cellbase / "confirm" / f"rate{rate}" / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000,
                            anon=30_000_000, peak=60_000_000)
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)
    # a reference-only soak window (5x a confirm window's requests), no twin
    _write_probe_window(cellbase / "soak" / f"rate{rate}" / "block0", REFERENCE,
                        reqs=5000, cpu_us=600_000, anon=30_000_000, peak=60_000_000)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res.by_slot[REFERENCE].stage_windows == {"confirm": 2, "soak": 1}
    assert res.by_slot[SUBJECT].stage_windows == {"confirm": 2}
    # weighted and unweighted disagree here, and BOTH are published
    assert res.cpu_ratio != res.cpu_ratio_unweighted

    md = result.report_path.read_text(encoding="utf-8")
    assert "Stage-mix asymmetry in headline.mixed.gc-default" in md
    assert "stage mix of the windows used" in md
    assert "2 confirm, 1 soak" in md
    assert "equal-weight-per-window alternative gives" in md


def test_slo_verdict_states_its_rule_and_publishes_the_per_window_count(isolated_results):
    """The `meets SLO?` column is a MEAN of per-window p99s. A slot can pass it
    while a window misses badly, so the rule must be stated and the count of
    windows that individually met the SLO published beside the verdict."""
    run_id = "syn-slorule"
    rate = 1000
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    # reference: 5, 5, 45 -> mean 18.33 (under 20), but only 2 of 3 windows are
    for block, ref_v in enumerate((5.0, 5.0, 45.0)):
        for slot, val in ((REFERENCE, ref_v), (SUBJECT, 30.0 + block)):
            key = probe_key(rate, block, slot)
            rec.probes.append(_done_probe(rate, block, slot))
            p99[key] = val
            under_warmed[key] = False
    rec.data = _confirm_data({sid: rate for sid in SLOTS},
                             p99_ms=p99, under_warmed=under_warmed)
    _ledger_with_confirm(run_id, rec)

    result = analyze.analyze_run(run_id)
    rows = {(s.rate, s.slot): s for s in result.cells[0].slo_at_rates}
    ref = rows[(rate, REFERENCE)]
    assert ref.meets_slo is True                 # mean rule
    assert ref.n_windows_met_slo == 2 and ref.n_used == 3
    assert abs(ref.max_p99_ms - 45.0) < 1e-9

    md = result.report_path.read_text(encoding="utf-8")
    assert "THE VERDICT RULE" in md
    assert "meets 20 ms? (mean rule)" in md
    assert "windows under 20 ms" in md
    assert "| 2/3 |" in md
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    slo_rows = fits["cells"][0]["slo_at_rates"]
    assert all(r["meets_slo_rule"] == "mean of per-window p99 <= slo_ms" for r in slo_rows)


def test_slo_readout_names_which_slot_met_the_promise(isolated_results):
    run_id = "syn-slo"
    _ledger_with_confirm(run_id, _per_slot_rate_confirm(), _split_knee_ladder())
    result = analyze.analyze_run(run_id)
    rows = {(s.rate, s.slot): s for s in result.cells[0].slo_at_rates}
    assert rows[(1500, REFERENCE)].meets_slo is True
    assert rows[(1500, SUBJECT)].meets_slo is False
    assert rows[(2000, SUBJECT)].meets_slo is False

    md = result.report_path.read_text(encoding="utf-8")
    # p99 is a CONFORMANCE readout now, not a comparison: at their own knees both
    # stacks sit at ~20 ms by construction and a paired p99 verdict would tie.
    assert "This is a CONFORMANCE readout, not a comparison" in md
    assert "meets 20 ms?" in md
    # ...and the rule it applies is NOT the ladder's crossing rule. Two live
    # sentences claimed it was "the same rung-mean rule the ladder bracketed on";
    # the ladder brackets isotonically, so both had to go.
    assert "same rung-mean rule" not in md
    assert "DIFFERENT quantity from the ladder's crossing rule" in md


# --------------------------------------------------------------------------- #
# CPU-ms/request vs offered rate (the rate-dependence artifact)
# --------------------------------------------------------------------------- #
def test_cpu_per_request_vs_rate_curve_and_cv(isolated_results):
    """The SUBJECT's per-request CPU falls with rate while the REFERENCE's is
    flat: the curve, the per-rung ratio and both CVs must be published, not just
    one operating point."""
    run_id = "syn-curve"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default", ladder=_ladder_stage()))
    led.save()
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "ladder"
    # reference 0.1 CPU-ms/req at both rungs; subject 0.5 then 0.3, so the
    # subject:reference ratio is 5.00x then 3.00x.
    for rate, sub_us in ((1000, 500_000), (2000, 300_000)):
        for block in (0, 1):
            bd = base / f"rate{rate}" / f"block{block}"
            _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000,
                                anon=30_000_000, peak=60_000_000)
            _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=sub_us,
                                anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    cvr = result.cells[0].cpu_vs_rate
    assert cvr.available is True
    assert [r.rate for r in cvr.rungs] == [1000, 2000]
    assert abs(cvr.rungs[0].ratio - 5.0) < 1e-9
    assert abs(cvr.rungs[1].ratio - 3.0) < 1e-9
    assert cvr.cv_pct_by_slot[REFERENCE] == 0.0      # flat reference column
    assert cvr.cv_pct_by_slot[SUBJECT] > 20.0        # the subject moves with rate
    assert abs(cvr.ratio_min - 3.0) < 1e-9 and abs(cvr.ratio_max - 5.0) < 1e-9

    md = result.report_path.read_text(encoding="utf-8")
    assert "CPU-ms per request vs offered rate" in md
    assert "Coefficient of variation, per slot: " in md
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert len(fits["cells"][0]["cpu_vs_rate"]["rungs"]) == 2


def test_cpu_vs_rate_is_available_when_only_one_slot_has_ladder_windows(isolated_results):
    """Availability must be decided over BOTH slots: a cell with one slot's
    ladder windows and none of the other's is not "no cgroup summaries on disk"."""
    run_id = "syn-curve-one"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default", ladder=_ladder_stage()))
    led.save()
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "ladder"
    for rate in (1000, 2000):
        for block in (0, 1):
            _write_probe_window(base / f"rate{rate}" / f"block{block}", SUBJECT,
                                reqs=1000, cpu_us=300_000, anon=150_000_000,
                                peak=180_000_000)

    cvr = analyze.analyze_run(run_id).cells[0].cpu_vs_rate
    assert cvr.available is True
    assert [r.rate for r in cvr.rungs] == [1000, 2000]
    assert all(r.by_slot[REFERENCE].cpu_ms_per_req is None for r in cvr.rungs)
    assert all(r.by_slot[SUBJECT].cpu_ms_per_req is not None for r in cvr.rungs)
    assert all(r.ratio is None for r in cvr.rungs)   # a ratio needs both sides


# --------------------------------------------------------------------------- #
# scope section: what this run does NOT establish
# --------------------------------------------------------------------------- #
def test_scope_section_reports_capacity_gate_failure_and_missing_gates(isolated_results):
    run_id = "syn-scope"
    led = _synthetic_ledger(run_id)
    led.mark_phase(
        "capacity_gate", State.DONE,
        verdict="db-imposed-shared-knee", pg_knee_rps=666.67,
        levers_fired=["write_share", "cache_hit_rate", "pg_cores"],
    )
    led.mark_phase(
        "validation", State.DONE,
        co={"deferred": "bench-tier"}, logging_sink={"deferred": "bench-tier"},
        conn_sweep={"deferred": "bench-tier"},
    )

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "The capacity gate did NOT pass" in md
    assert "db-imposed-shared-knee" in md
    # A ledger carrying {"deferred": ...} entries still surfaces them -- as a
    # statement about THIS RUN's record, never as a standing claim that the
    # deferred checks will never be measured (a standing negative claim is
    # over-declaration too).
    assert "Validation checks this run recorded as deferred" in md
    assert "never" not in md.lower().split("deferred, so no number")[1][:400]
    # W23: spec/slo.yaml declares no gate the runner does not implement, so the
    # bullet has nothing to report and must not appear. That absence IS the
    # governing rule as the reader sees it (the assertion form lives in
    # tests/test_warmup.py); the bullet's own behaviour is covered below.
    assert "but NOT implemented" not in md
    # the working-set / cache-residency caveat
    assert "Effective working set" in md
    assert "`cache_stats` are null" in md


def test_a_declared_but_unimplemented_gate_is_named_in_the_scope_section(
    isolated_results, monkeypatch
):
    """The R1-B1 machinery, exercised against a gate the spec no longer carries.

    W23 deleted `hot_set_touched` and `major_faults_asserted_equal`, so the only
    way to cover the bullet is to declare a synthetic gate here. The set of
    implemented gates is taken from the runner's canonical registry, not
    restated in analyze.py.
    """
    from ledgerbench.config import load_slo
    from ledgerbench.warmup import IMPLEMENTED_GATES

    synthetic = "synthetic_gate_no_runner_implements"
    real = load_slo()
    patched = real.model_copy(update={
        "warmup": real.warmup.model_copy(
            update={"gates": {**real.warmup.gates, synthetic: True}}
        )
    })
    monkeypatch.setattr(analyze, "load_slo", lambda *a, **k: patched)

    run_id = "syn-scope-gate"
    _synthetic_ledger(run_id)
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")

    assert f"`{synthetic}`" in md
    assert "`ledgerbench.warmup.IMPLEMENTED_GATES`" in md
    # per_endpoint_min_calls is a separate spec key, not one of the declared
    # gates: it must not be smuggled into the implemented-gate arithmetic
    assert "per_endpoint_min_calls" not in IMPLEMENTED_GATES
    assert "plus the separate `per_endpoint_min_calls` spec key" in md
    assert "none of which is in the unimplemented list" in md
    # The bullet must not claim the gate is missing from the per-probe records:
    # `warmup.unimplemented_gates()` writes one for every declared-but-
    # unimplemented gate, and a bullet broader than the code is this repo's
    # worst defect class.
    assert "IS in every probe's" in md


# --------------------------------------------------------------------------- #
# scope section: three claims that must be derived from the run, not fixed text
# --------------------------------------------------------------------------- #
def _scoped_ledger(run_id: str, scale: str, reps: int = 2) -> Ledger:
    """A synthetic ledger that PINS its scale, the way every real run does."""
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET, scale=scale)
    led.cells.append(CellRecord(
        cell_id="headline.mixed.gc-default",
        ladder=_ladder_stage(), confirm=_confirm_stage(),
    ))
    led.save()
    return led


def _write_working_set(run_id: str, **fields):
    """Persist a manifest carrying only the working_set section under test."""
    from ledgerbench import manifest as mf

    m = mf.new_manifest(run_id, config_hash=_live_config_hash())
    m.working_set = mf.WorkingSet(**fields)
    m.save()
    return m


def _spread(*values):
    from ledgerbench import manifest as mf

    return mf.Spread.of(list(values))


_SMOKE_WS = dict(
    draw_mode="per_probe_stream",
    draw_seed_formula="PCG64(seed, inc=stream_for(seed, run_id, cell, stage, rate, block, phase))",
    requests_drawn_per_probe=_spread(6120, 12240, 24480),
    distinct_invoice_ids_per_probe=_spread(500, 822, 1200),
    distinct_customer_ids_per_probe=_spread(200, 360, 500),
    draw_seconds_per_probe=_spread(0.4, 0.8, 1.6),
    prewarm_containment_per_probe=_spread(0.4632, 0.5657, 0.6602),
    body_pool_create_bodies=900, body_pool_quote_bodies=1200,
    body_pool_distinct_customer_ids=311,
    seeded_invoices=5000, seeded_customers=1000,
    cache_ttl_seconds=300, cache_ttl_source="compose default",
    measured_window_seconds=10,
)


def test_working_set_bullet_quotes_the_corpus_this_run_seeded(isolated_results):
    """The seeded corpus comes off the MANIFEST, never off the FULL-scale table.

    `spec/workload.yaml` used to carry `keyspaces.*.seeded_range`, a full-scale
    range whatever tier ran, and a smoke-tier report rendered from it claimed
    5,000,000 invoices against the 5,000 the run actually seeded -- 1000x, in
    the sentence whose entire job is quantifying cache residency, and in the
    direction that makes the run look far less cache-resident than it was. That
    key is deleted; `seeding.scales.full` is the surviving full-scale pair and
    the same trap, so the report is held against IT here.
    """
    run_id = "syn-ws-smoke"
    _scoped_ledger(run_id, "smoke")
    _write_working_set(run_id, **_SMOKE_WS)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "from 5,000 seeded invoices / 1,000 seeded customers" in md
    # the FULL-scale corpus must appear nowhere in a smoke-tier report
    from ledgerbench.config import load_workload

    wl = load_workload()
    assert wl.seeding.scales["full"].invoices == 5_000_000        # the trap value
    assert not hasattr(wl.keyspaces.invoices, "seeded_range")
    assert "5,000,000" not in md and "5000000" not in md
    # The per-probe SPREAD, not a whole-file count: a ladder spans a 20x range
    # of offered rates, so one number would describe no probe in the run.
    assert "drew its OWN stream of 6,120-24,480 (median 12,240) requests" in md
    assert "500-1,200 (median 822) distinct invoice ids (16.4% of the seeded" in md
    assert "this run publishes no union across probes" in md
    # the sentence must not claim the windows are DISJOINT: two independent
    # streams still share the Zipf head, and "no id is shared" would be false.
    assert "no id set is shared" not in md
    assert "the counts may not be summed" in md


def test_working_set_bullet_states_the_body_pool_disclosure(isolated_results):
    """The one distributional cost of drawing bodies from a fixed pool.

    A rendered report sentence is what a reader actually meets, so the create
    path's uniform-over-a-Zipf-sample customer draw has to be stated there and
    stated from THIS run's numbers -- not left in a manifest field or a docs
    file nobody opens beside the report.

    The two counts in that clause have different provenance and the sentence
    must not read as though they had the same: the body count is DERIVED (the
    mix shares applied to the working-set dial) and the distinct-customer count
    is MEASURED off the pool on disk. The gap between 900 and 311 is the whole
    disclosure, and it is only readable if the reader knows one was counted.
    """
    run_id = "syn-ws-pool"
    _scoped_ledger(run_id, "smoke")
    _write_working_set(run_id, **_SMOKE_WS)
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert ("900 create bodies sized from the mix, counted off the pool on disk "
            "to hold 311 distinct customer ids") in md
    assert "1,200 quote bodies" in md
    assert "thinner in the tail" in md
    assert "The read path is URL-only and unaffected" in md


def test_working_set_residency_verdict_follows_the_window_and_the_ttl(isolated_results):
    """The residency CONCLUSION is derived, not fixed.

    Round 1's version compared the TTL against the seconds a full replay of the
    targets file took, and asserted "more cache-resident than the constructed
    target hit rate implies" whichever way that came out. Nothing replays now,
    so the comparison that remains is whether an entry populated inside a
    measured window is still live at the end of it -- and a soak window longer
    than the TTL must not be reported as though it were shorter.
    """
    resident = "syn-ws-resident"
    _scoped_ledger(resident, "smoke")
    _write_working_set(resident, **_SMOKE_WS)
    md_resident = analyze.analyze_run(resident).report_path.read_text(encoding="utf-8")
    assert "A measured window is 10 s against a 300 s cache TTL" in md_resident
    assert "still live at the end of it" in md_resident
    assert "rests on the Zipf skew plus whatever the window opened with" in md_resident

    not_resident = "syn-ws-cold"
    _scoped_ledger(not_resident, "full")
    _write_working_set(not_resident, **{
        **_SMOKE_WS, "measured_window_seconds": 600,
        "seeded_invoices": 5_000_000, "seeded_customers": 1_000_000,
    })
    md_cold = analyze.analyze_run(not_resident).report_path.read_text(encoding="utf-8")
    assert "can expire before that window ends" in md_cold
    assert ("WEAKER than the Zipf skew alone implies, and weaker than the "
            "constructed target hit rate of 0.80 implies.") in md_cold
    # the W20 text spliced the hit-rate clause straight after "implies" and
    # rendered "implies than ... implies" in every report with a soak window
    assert "implies than" not in md_cold
    assert "MORE cache-resident" not in md_cold


def test_the_report_publishes_measured_prewarming_instead_of_denying_it(
    isolated_results,
):
    """THE claim the W20 correction narrows, and the number that replaces it.

    Until the W20 correction this bullet rendered, in every tier, "nothing
    before a window populates the cache with the ids that window is about to
    request". It is false:
    separate PCG64 streams make the warmup's SEQUENCE independent of the
    measured window's -- which is all of R1-A7, where all three attacks replayed
    one file and containment was 1.0 by construction -- but both streams are
    Zipf draws over the SAME keyspace and share the head by construction.
    Measured on this box at the harness's own 618 rps warmup rate, 52-67% of a
    window's invoice-id read requests name an id an earlier attack of the same
    probe already drew. The numbers in this test are the SYNTHETIC fixture's,
    not that measurement: the bullet derives its spread from the run's own
    `working_set`, which is the property under test.

    So the report must (a) claim only what the mechanism achieves, (b) print the
    residual as a MEASURED request-weighted fraction from the run's own draws,
    and (c) print it beside the constructed target hit rate, since the reader
    needs both to read the cache numbers at all.
    """
    run_id = "syn-ws-prewarm"
    _scoped_ledger(run_id, "smoke")
    _write_working_set(run_id, **_SMOKE_WS)
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")

    # (a) the absolute is GONE, in the exact words that propagated
    assert "nothing before a window populates the cache" not in md
    assert "no attack replays the SEQUENCE another is about to consume" in md
    assert "It does NOT mean a window opens cold" in md
    # (b) the measured spread, request-weighted, from working_set
    assert "Measured over 3 windows, 46.3-66.0% (median 56.6%) of a window's " \
           "invoice-id READ REQUESTS name an id an earlier attack of the same " \
           "probe had already drawn" in md
    # (c) beside the pre-registered construction target
    assert "against a constructed target hit rate of 0.80" in md
    assert "residency floor and it is a property of the workload" in md

    # a run with ONE scored window says so, and a spread of one value renders as
    # that value rather than as a min-max range of it against itself
    single = "syn-ws-prewarm-1"
    _scoped_ledger(single, "smoke")
    _write_working_set(single, **{
        **_SMOKE_WS, "prewarm_containment_per_probe": _spread(0.5719),
    })
    md1 = analyze.analyze_run(single).report_path.read_text(encoding="utf-8")
    assert "Measured over 1 window, 57.2% of a window's invoice-id READ" in md1


def test_the_prewarm_share_is_an_absence_when_no_probe_scored_it(isolated_results):
    """A run with no containment record must not render 0% -- or the old claim.

    A missing measurement and a measured zero are different findings, and this
    is the field whose predecessor was an ASSERTED zero. The narrowed claim
    about what separate streams achieve still stands (it is a property of the
    mechanism, not of the run); the residual is stated as unquantified.
    """
    run_id = "syn-ws-noprewarm"
    _scoped_ledger(run_id, "smoke")
    _write_working_set(run_id, **{
        **_SMOKE_WS, "prewarm_containment_per_probe": _spread(),
    })
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "no attack replays the SEQUENCE another is about to consume" in md
    assert "`working_set.prewarm_containment_per_probe` is empty" in md
    assert "0.0%" not in md
    assert "nothing before a window populates the cache" not in md


def test_working_set_bullet_names_the_absence_when_nothing_recorded_it(isolated_results):
    """No manifest: the corpus and the ids are absences, not spec text."""
    run_id = "syn-ws-none"
    _scoped_ledger(run_id, "smoke")
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "Effective working set" in md
    assert "carries no `working_set` section" in md
    assert "seeded invoices /" not in md


def test_working_set_bullet_does_not_claim_a_residency_it_has_no_window_for(
    isolated_results,
):
    """A run that recorded no draw has no window to compare the TTL against."""
    run_id = "syn-ws-nowindow"
    _scoped_ledger(run_id, "smoke")
    _write_working_set(run_id, **{
        **_SMOKE_WS, "measured_window_seconds": None,
        "requests_drawn_per_probe": _spread(),
        "distinct_invoice_ids_per_probe": _spread(),
    })
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "are NOT recorded in this run's manifest" in md
    assert "A measured window is" not in md
    assert "cache-resident" not in md


def test_runtime_stats_bullet_counts_the_summaries_this_run_wrote(isolated_results):
    """"Both windows are flushed" was fixed text over an artifact that may not exist.

    `runtimestats.PollWindow.summary_dict` writes `source: not_measured` with
    every field null when no poll succeeded, so the bullet must count what is on
    disk instead of asserting flushed summaries with a measured cost bound.
    """
    run_id = "syn-rs"
    _scoped_ledger(run_id, "smoke")
    base = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
            / "confirm" / "rate1000" / "block0")
    base.mkdir(parents=True, exist_ok=True)
    _write_runtime_stats_summary(base / f"{REFERENCE}.runtime_stats.summary.json",
                                 phase="measured", measured=True)
    _write_runtime_stats_summary(base / f"{REFERENCE}.warmup.runtime_stats.summary.json",
                                 phase="warmup", measured=True)
    _write_runtime_stats_summary(base / f"{SUBJECT}.runtime_stats.summary.json",
                                 phase="measured", measured=False)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "This run flushed 3 window summaries" in md
    assert "2 measured, 1 warmup" in md
    assert "1 of them record `source: not_measured`" in md
    assert "2 of them carry a MEASURED upper bound" in md
    assert "totalling 169 ms across the run" in md          # 84.5 x 2, exactly


def test_runtime_stats_bullet_claims_nothing_when_no_summary_exists(isolated_results):
    """A tree with no poller artifacts must not describe flushed summaries."""
    run_id = "syn-rs-none"
    _scoped_ledger(run_id, "smoke")
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "left NO `*.runtime_stats.summary.json` on disk" in md
    assert "both windows are flushed" not in md
    assert "MEASURED upper bound" not in md


def test_knee_ci_note_quotes_this_ladders_reps_and_the_fits_own_n_boot(
    isolated_results,
):
    """The note hard-coded `reps_per_rung: 3` and `2000 isotonic refits`.

    `ScaleProfile` runs 2 reps per rung at BOTH `rehearsal` and `smoke`, so the
    fixed 3 was a claim about a ladder two of the three tiers never run. The
    resample count now rides on `KneeFit.n_boot`, carried from the estimator.
    """
    run_id = "syn-ci-note"
    _scoped_ledger(run_id, "smoke")
    result = analyze.analyze_run(run_id)
    md = result.report_path.read_text(encoding="utf-8")
    assert "the `smoke` ladder runs 2 rep(s) per rung" in md
    assert "reps_per_rung: 3" not in md and "reps_per_rung=3" not in md
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    n_boot = fits["cells"][0]["knee_by_slot"][SUBJECT]["n_boot"]
    assert n_boot == 2000                                   # analysis.knee default
    assert f"percentile of {n_boot} isotonic refits" in md


def _write_runtime_stats_summary(
    path, *, phase: str, measured: bool, slot: str = "x",
    edges: bool = False, deltas: dict | None = None, levels: dict | None = None,
    trace: bool = False,
) -> None:
    """One `<stem>.runtime_stats.summary.json` in the shape PollWindow writes.

    `edges`, `deltas` and `trace` default OFF and are independent, because they
    are independent on disk: a window with no successful poll has neither edge
    and `PollWindow.write` writes it no `.jsonl` at all, while a window with
    exactly one poll has both edges and an EMPTY delta block.
    """
    from ledgerbench.runtimestats import NOT_MEASURED

    if trace:
        path.with_name(
            path.name.replace(".summary.json", ".jsonl")
        ).write_text("{}\n", encoding="utf-8")
    path.write_text(json.dumps({
        "phase": phase, "slot": slot,
        "source": "polled at 1 Hz" if measured else NOT_MEASURED,
        "n_polls": 120 if measured else 0, "n_poll_errors": 0, "last_error": "",
        "window_seconds": 120.0 if measured else None,
        "start": {"stats": {}} if edges else None,
        "end": {"stats": {}} if edges else None,
        "deltas": deltas or {}, "levels": levels or {},
        "poll_cost": {
            "n_polls": 120 if measured else 0,
            "round_trip_ms_total": 84.5 if measured else None,
            "round_trip_ms_max": 2.1 if measured else None, "note": "",
        },
        "note": "" if measured else (
            "no /runtime-stats poll succeeded in this window; every field is "
            "null and NOT a measured zero"
        ),
    }), encoding="utf-8")


def test_scope_section_omits_capacity_bullet_when_the_gate_passed(isolated_results):
    """A passing gate must not be described as a failure (the bullet is derived
    from the recorded verdict, not asserted)."""
    run_id = "syn-scope-pass"
    led = _synthetic_ledger(run_id)
    led.mark_phase("capacity_gate", State.DONE, verdict="pass", pg_knee_rps=5000.0)
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "The capacity gate did NOT pass" not in md


def test_a_window_that_swapped_is_not_clean_and_the_report_says_so(isolated_results):
    """`swap_clean`, beside `throttle_clean`/`oom_clean` and under the same rule.

    Each invalidates a DIFFERENT number: a throttled window deflates CPU, an
    OOM window invalidates everything, and a window that swapped UNDERSTATES the
    memory headline -- a swapped-out anon page leaves cgroup v2
    `memory.stat anon`, so `mem_anon_p99` drops and nothing else says why. And a
    window whose swap counter could not be read is NOT evidence of cleanliness:
    `all()` over an empty set is True.
    """
    run_id = "syn-swap"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000)
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000, anon=150_000_000,
                            peak=180_000_000, swap=4_294_967_296 if block == 0 else 0)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res.by_slot[REFERENCE].swap_current_clean is True
    assert res.by_slot[REFERENCE].n_windows_without_swap_current == 0
    assert res.by_slot[SUBJECT].swap_current_clean is False
    md = result.report_path.read_text(encoding="utf-8")
    # No window here carried a PostgreSQL co-sample, so the rendered scope says
    # so rather than naming a cgroup the verdict never read (`_SwapScope`).
    assert ("`memory.swap.current` (SUT cgroups only: no window in scope carried "
            "a PostgreSQL co-sample) NON-ZERO in "
            f"headline.mixed.gc-default/{SUBJECT}") in md
    # and the container-placement verdict is WIRED INTO the rendered report, not
    # merely computable from it
    assert "Container placement" in md

    # No swap read at all: unread, NOT clean, and the report says which.
    run_id2 = "syn-swap-unread"
    _ledger_with_confirm(run_id2, _confirm_stage_at(rate, 2))
    base2 = isolated_results / run_id2 / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            _write_probe_window(base2 / f"block{block}", slot, reqs=1000, cpu_us=cpu,
                                anon=30_000_000, peak=60_000_000, swap=None,
                                swap_peak=None)
    result2 = analyze.analyze_run(run_id2)
    res2 = result2.cells[0].resource
    assert res2.by_slot[SUBJECT].n_windows_without_swap_current == 2
    assert res2.by_slot[SUBJECT].n_windows_without_swap_peak == 2
    # THE verdict, not just the count: a kernel without CONFIG_MEMCG_SWAP (or
    # booted `swapaccount=0`) exposes no memory.swap.current at all, so every
    # window reads None and `all([])` would publish clean over zero checks.
    assert res2.by_slot[SUBJECT].swap_current_clean is False
    assert res2.by_slot[REFERENCE].swap_current_clean is False
    assert res2.to_dict()["by_slot"][SUBJECT]["swap_current_clean"] is False
    assert res2.to_dict()["by_slot"][SUBJECT]["swap_peak_clean"] is False
    md2 = result2.report_path.read_text(encoding="utf-8")
    assert "NOT ESTABLISHED" in md2 and "no window carried the counter" in md2
    # and an unread counter is never rendered as a violation either
    assert "NON-ZERO" not in md2


def test_a_window_that_swapped_and_faulted_back_is_not_clean_either(isolated_results):
    """`memory.swap.current` is two point samples; the episode is in between.

    A container that swapped 4 GiB at t=30 s and had it faulted back by t=110 s
    reads 0 at both window edges while `mem_anon_p99` -- sampled every 50 ms --
    was depressed for 80 s. The kernel's own `memory.swap.peak` is what still
    says so, and the verdict takes both.
    """
    run_id = "syn-swap-peak"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000)
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000, anon=150_000_000,
                            peak=180_000_000, swap=0,
                            swap_peak=4_294_967_296 if block == 0 else 0)

    res = analyze.analyze_run(run_id).cells[0].resource
    assert res.by_slot[REFERENCE].swap_peak_clean is True
    assert res.by_slot[SUBJECT].swap_peak_clean is False
    # and the LEVEL verdict is untouched: both edges really did read 0
    assert res.by_slot[SUBJECT].swap_current_clean is True


def test_a_kernel_with_no_memory_swap_peak_never_claims_a_lifetime_high_water_read(
    isolated_results,
):
    """The two swap keys carry ONE verdict each, because a kernel carries one each.

    `memory.swap.peak` landed in 6.5; a kernel without it (or a resumed tree
    written before this harness read it) exposes `memory.swap.current` alone.
    If the two reads were collapsed into one value, such a window would count
    as read and the report would assert "zero ... in the kernel's lifetime
    high-water mark", the half that is the ONLY thing catching a window that
    swapped and faulted back. A report must not claim a read the kernel cannot
    make.
    """
    run_id = "syn-swap-nopeak"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            _write_probe_window(base / f"block{block}", slot, reqs=1000, cpu_us=cpu,
                                anon=30_000_000, peak=60_000_000,
                                swap=0, swap_peak=None)

    result = analyze.analyze_run(run_id)
    lr = result.cells[0].resource.by_slot[SUBJECT]
    assert lr.swap_current_clean is True and lr.n_windows_without_swap_current == 0
    assert lr.swap_peak_clean is False and lr.n_windows_without_swap_peak == 2

    md = result.report_path.read_text(encoding="utf-8")
    assert ("`memory.swap.current` (SUT cgroups only: no window in scope carried "
            "a PostgreSQL co-sample) zero at every window edge") in md
    assert ("`memory.swap.peak` (no window in scope) NOT ESTABLISHED") in md
    # the sentence the collapsed verdict used to print over a file that does not
    # exist on this kernel
    assert "zero in the kernel's lifetime high-water mark" not in md


def test_the_swap_verdict_covers_the_co_sampled_postgres_window_not_only_the_sut(
    isolated_results,
):
    """A swapping DATABASE is what would move the DB CPU-ms/request column.

    That column and the `db_cpu_ratio` headline are published off
    `cpu_mem_<slot>.postgres.summary.json`, while the swap verdict read the
    SUT's own summary alone -- so PostgreSQL could swap for the whole run with
    the report one clause away printing that swap was zero everywhere.
    """
    run_id = "syn-swap-pg"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, db_cpu_us=40_000)
        # the SUT's own cgroup is spotless; PostgreSQL's is not
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000, anon=150_000_000,
                            peak=180_000_000, db_cpu_us=40_000,
                            db_swap=4_294_967_296 if block == 0 else 0)

    result = analyze.analyze_run(run_id)
    by_slot = result.cells[0].resource.by_slot
    assert by_slot[REFERENCE].swap_current_clean is True
    assert by_slot[SUBJECT].swap_current_clean is False
    md = result.report_path.read_text(encoding="utf-8")
    assert ("`memory.swap.current` (SUT and PostgreSQL cgroups) NON-ZERO in "
            f"headline.mixed.gc-default/{SUBJECT}") in md


def test_the_swap_phrase_renders_the_scope_it_actually_read_not_a_fixed_pair(
    isolated_results,
):
    """`swap_docs = [cm] if db is None else [cm, db]` NARROWS the verdict silently.

    A run with no PostgreSQL co-sample is a supported path (`_window_rows`
    refuses to gate the SUT rows on it), and on that path the verdict spans the
    SUT alone. The sentence used to name both cgroups regardless and close with
    "which is why the swap verdicts span PostgreSQL's window as well as the
    SUT's" -- a scope claim broader than the supported path delivers.
    """
    run_id = "syn-swap-nopg"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            _write_probe_window(base / f"block{block}", slot, reqs=1000, cpu_us=cpu,
                                anon=30_000_000, peak=60_000_000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert ("`memory.swap.current` (SUT cgroups only: no window in scope carried "
            "a PostgreSQL co-sample) zero at every window edge") in md
    assert ("`memory.swap.peak` (SUT cgroups only: no window in scope carried "
            "a PostgreSQL co-sample) zero in the kernel's") in md
    assert "the swap verdicts span PostgreSQL's window" not in md
    assert ("The DB CPU-ms/request column has no swap verdict here: no window in "
            "scope carried a PostgreSQL co-sample.") in md
    # and the section heading does not promise a DB column this run has not got
    assert "(cgroup v2 ground truth: SUT)" in md
    assert "SUT + PostgreSQL" not in md
    # the "not out of scope" bullet claims only what the harness's own target
    # table shows; the old wording asserted Redis, vegeta and both runtime units
    # were covered, which nothing in this run establishes.
    assert ("`live_runner._sample_targets` declares a PostgreSQL cgroup target "
            "for every probe") in md
    assert "the sampler covers PostgreSQL, Redis, the load generator" not in md


def test_the_swap_phrase_counts_the_windows_when_only_some_co_sampled_postgres(
    isolated_results,
):
    """Partial co-sampling is neither of the two clean stories, so it is counted.

    Blocking on it would delete the section; claiming full PostgreSQL coverage
    would overstate it. The phrase reports how many of the in-scope windows
    actually carried the DB summary.
    """
    run_id = "syn-swap-somepg"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            _write_probe_window(base / f"block{block}", slot, reqs=1000, cpu_us=cpu,
                                anon=30_000_000, peak=60_000_000,
                                db_cpu_us=40_000 if block == 0 else None)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert ("`memory.swap.current` (SUT cgroups in all 4 window(s), PostgreSQL in "
            "2 of them)") in md
    assert "the swap verdicts span PostgreSQL's window" in md
    assert "(cgroup v2 ground truth: SUT + PostgreSQL)" in md


def test_the_apparatus_bullet_names_the_cgroups_the_manifest_recorded(
    isolated_results,
):
    """"vegeta, dockerd and containerd are sampled" was FIXED TEXT.

    `manifest.load_generator_overhead` stays EMPTY for a run with no cgroup
    accounting, so the bullet asserted three measurements that do not exist.
    """
    from ledgerbench.manifest import LoadGeneratorOverhead, new_manifest

    run_id = "syn-apparatus"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            _write_probe_window(base / f"block{block}", slot, reqs=1000, cpu_us=cpu,
                                anon=30_000_000, peak=60_000_000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "container-runtime CPU is EXCLUDED, and was NOT measured this run" in md
    for absent in ("`vegeta` are sampled", "`dockerd`", "`containerd`"):
        assert absent not in md

    m = new_manifest(run_id)
    m.load_generator_overhead = [
        LoadGeneratorOverhead(label="vegeta", cpu_usage_usec_delta=1_000),
    ]
    m.save()
    md2 = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "`vegeta` is sampled (manifest `load_generator_overhead`)" in md2
    assert "container-runtime CPU is EXCLUDED, and was NOT measured" not in md2
    assert "`dockerd`" not in md2


def test_a_co_sampled_window_with_no_swap_read_is_unread_not_a_sut_only_verdict(
    isolated_results,
):
    """Half a read is not a read. A DB window whose swap counters did not read
    leaves the window's swap unestablished rather than quietly narrowing the
    verdict back to the SUT alone, which is what a `max` over "whatever was
    present" would have done."""
    run_id = "syn-swap-pg-unread"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            _write_probe_window(base / f"block{block}", slot, reqs=1000, cpu_us=cpu,
                                anon=30_000_000, peak=60_000_000, swap=0, swap_peak=0,
                                db_cpu_us=40_000, db_swap=None, db_swap_peak=None)

    lr = analyze.analyze_run(run_id).cells[0].resource.by_slot[SUBJECT]
    assert lr.swap_current_clean is False and lr.n_windows_without_swap_current == 2
    assert lr.swap_peak_clean is False and lr.n_windows_without_swap_peak == 2


def test_the_container_placement_verdict_reaches_the_report():
    """`_stamp_topology` computed `matches` and NOTHING downstream read it.

    A container that ran unpinned across every logical core while the run
    recorded a 6-core budget was a `false` in a JSON file no reader opens. A
    verdict that is not said where the numbers are said is not a verdict.
    """
    from ledgerbench.manifest import ContainerTopology, new_manifest

    m = new_manifest("topo-report")
    m.topology.containers = {
        "dotnet": ContainerTopology(cpuset_effective="0-15", expected_cpuset="0-5",
                                    matches=False),
        "postgres": ContainerTopology(cpuset_effective="8-10",
                                      expected_cpuset="8-10", matches=True),
    }
    bad = analyze._container_placement_bullet(m)
    assert "DIVERGED" in bad and "dotnet" in bad and "0-15" in bad

    m.topology.containers["dotnet"].matches = True
    m.topology.containers["dotnet"].cpuset_effective = "0-5"
    good = analyze._container_placement_bullet(m)
    assert "matches the resource ledger" in good
    assert "2 sampled cgroup(s)" in good

    m.topology.containers = {}
    assert "Not recorded this run" in analyze._container_placement_bullet(m)


def test_the_report_reads_the_recorded_memory_ceilings_instead_of_asserting_them(
    isolated_results,
):
    """`mem_limit_bytes` / `mem_swap_max` were recorded and read by NOTHING.

    The resource-validity paragraph asserted infra/compose.bench.yaml's
    `memswap_limit: mem_limit` rule as prose -- "the containers have no swap
    allowance to use; this is the evidence, not the setting" -- one paragraph
    from the read-back that could prove it. A setting is a claim until the
    kernel's own value is quoted.
    """
    from ledgerbench.manifest import ContainerTopology, new_manifest

    run_id = "syn-ceilings"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            _write_probe_window(base / f"block{block}", slot, reqs=1000, cpu_us=cpu,
                                anon=30_000_000, peak=60_000_000)
    m = new_manifest(run_id)
    m.topology.containers = {
        SUBJECT: ContainerTopology(cpuset_effective="0-5", expected_cpuset="0-5",
                                   matches=True, mem_limit_bytes=12884901888,
                                   mem_swap_max="0"),
    }
    m.save()

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert f"`{SUBJECT}` memory.max=12.00 GiB, memory.swap.max=0" in md
    assert "no swap allowance to spend" in md

    # a container that CAN swap is named, not glossed by the compose rule
    m.topology.containers[SUBJECT].mem_swap_max = "max"
    m.save()
    md2 = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "`memory.swap.max` is NOT 0 for" in md2 and SUBJECT in md2
    assert "no swap allowance to spend" not in md2


def test_the_report_reads_the_recorded_swap_outcomes_for_every_sampled_cgroup():
    """`swap_current_max_bytes` / `swap_peak_max_bytes` were written and read by NOTHING.

    They are the only swap evidence covering the sampled cgroups the per-window
    verdict does not reach -- here `redis`, which is `covered`'s complement, not
    a name this sentence spells. A 4 GiB high-water mark on any such cgroup used
    to sit in the manifest with the report one clause away printing that swap
    was zero everywhere.
    """
    from ledgerbench.manifest import ContainerTopology, new_manifest

    m = new_manifest("topo-swap")
    m.topology.containers = {
        "dotnet": ContainerTopology(mem_limit_bytes=12884901888, mem_swap_max="0",
                                    swap_current_max_bytes=0, swap_peak_max_bytes=0),
        "redis": ContainerTopology(mem_limit_bytes=2147483648, mem_swap_max="0",
                                   swap_current_max_bytes=0, swap_peak_max_bytes=0),
    }
    clean = analyze._container_ceiling_sentence(m, covered=["dotnet"])
    assert "max memory.swap.current=0" in clean and "max memory.swap.peak=0" in clean
    assert "`memory.swap.current` read 0 at every window edge for `dotnet`, `redis`" in clean
    assert "`memory.swap.peak`, the kernel's own lifetime high-water mark, read 0" in clean
    assert "That covers `redis`, which the per-window verdict above does not." in clean
    assert "SWAPPED BYTES RECORDED" not in clean

    # a cgroup that swapped and faulted back: the level is 0, the kernel's
    # lifetime high-water mark is not, and the sentence must name it
    m.topology.containers["redis"].swap_peak_max_bytes = 4_294_967_296
    swapped = analyze._container_ceiling_sentence(m, covered=["dotnet"])
    assert "SWAPPED BYTES RECORDED for `redis`" in swapped
    assert "4294967296" in swapped
    assert "read 0 at every window edge" not in swapped

    # and an unread counter is never rendered as either verdict
    m.topology.containers["redis"].swap_peak_max_bytes = None
    m.topology.containers["redis"].swap_current_max_bytes = None
    unread = analyze._container_ceiling_sentence(m, covered=["dotnet"])
    assert "`memory.swap.current` NOT ESTABLISHED for `redis`" in unread
    assert "`memory.swap.peak` NOT ESTABLISHED for `redis`" in unread
    assert "SWAPPED BYTES RECORDED" not in unread


def test_the_ceiling_sentence_never_claims_a_high_water_read_a_kernel_cannot_make():
    """`memory.swap.peak` landed in 6.5. On an older kernel it does not exist.

    A single `unread` set requiring BOTH swap keys to be None put every such row
    in neither the swapped nor the unread set, so the clean branch fired and the
    paragraph printed "max memory.swap.peak=unread" for a label and, in the SAME
    sentence, that no cgroup held a swapped byte "in its lifetime high-water
    mark" -- with no NOT ESTABLISHED clause. The two keys carry separate read
    sets, so a key that never read is never spoken for.
    """
    from ledgerbench.manifest import ContainerTopology, new_manifest

    m = new_manifest("topo-nopeak")
    m.topology.containers = {
        label: ContainerTopology(mem_limit_bytes=2147483648, mem_swap_max="0",
                                 swap_current_max_bytes=0, swap_peak_max_bytes=None)
        for label in ("dotnet", "postgres", "redis", "vegeta")
    }
    out = analyze._container_ceiling_sentence(m, covered=["dotnet", "postgres"])
    assert "max memory.swap.peak=unread" in out
    assert "lifetime high-water mark" not in out
    assert ("`memory.swap.peak` NOT ESTABLISHED for `dotnet`, `postgres`, `redis`, "
            "`vegeta`, whose counter never read.") in out
    assert "`memory.swap.current` read 0 at every window edge" in out
    assert "`memory.swap.current` NOT ESTABLISHED" not in out

    # symmetric: the level unread, the kernel's high-water mark read
    for row in m.topology.containers.values():
        row.swap_current_max_bytes, row.swap_peak_max_bytes = None, 0
    flipped = analyze._container_ceiling_sentence(m, covered=["dotnet", "postgres"])
    assert "`memory.swap.current` NOT ESTABLISHED for `dotnet`" in flipped
    assert "read 0 at every window edge" not in flipped
    assert "lifetime high-water mark, read 0 for `dotnet`" in flipped


def test_the_ceiling_sentence_names_only_cgroups_the_manifest_actually_holds():
    """`_stamp_topology` SKIPS any label whose cpuset and memory.max both read None.

    So the set of rows is a property of the run. The clean branch used to close
    with "including redis, vegeta and the container-runtime units" regardless,
    which names three cgroups a two-row manifest never sampled.
    """
    from ledgerbench.manifest import ContainerTopology, new_manifest

    m = new_manifest("topo-partial")
    m.topology.containers = {
        label: ContainerTopology(mem_limit_bytes=2147483648, mem_swap_max="0",
                                 swap_current_max_bytes=0, swap_peak_max_bytes=0)
        for label in ("dotnet", "redis")
    }
    out = analyze._container_ceiling_sentence(m, covered=["dotnet"])
    assert "That covers `redis`, which the per-window verdict above does not." in out
    for absent in ("vegeta", "dockerd", "containerd", "container-runtime"):
        assert absent not in out

    # every row already covered by the per-window verdict -> no "that covers"
    # clause at all, rather than a list of cgroups it does cover twice
    only = analyze._container_ceiling_sentence(m, covered=["dotnet", "redis"])
    assert "That covers" not in only


def test_the_placement_bullet_never_says_matches_over_zero_checked_rows():
    """A bold "matches" backed by a count of ZERO is the verdict a reader takes.

    `_stamp_topology` keeps any row whose `memory.max` read even when the cpuset
    did not, so rows with a populated `expected_cpuset` and `matches=None` are
    reachable -- and the old wording both led with "matches ... 0 sampled
    cgroup(s)" and attributed "declare no compose cpuset" to containers whose
    expectation was right there in the row.
    """
    from ledgerbench.manifest import ContainerTopology, new_manifest

    m = new_manifest("topo-unchecked")
    m.topology.containers = {
        "dotnet": ContainerTopology(expected_cpuset="0-5", mem_limit_bytes=12),
        "postgres": ContainerTopology(expected_cpuset="8-10", mem_limit_bytes=8),
    }
    bullet = analyze._container_placement_bullet(m)
    assert "matches" not in bullet
    assert "NOT verified" in bullet
    assert "declare no compose cpuset" not in bullet
    assert "never read back" in bullet

    # a row with NO expectation is the dockerd/containerd case the wording is
    # for, and it must not be confused with the unreadable one
    m.topology.containers["dockerd"] = ContainerTopology(cpuset_effective="0-15")
    m.topology.containers["redis"] = ContainerTopology(
        cpuset_effective="14", expected_cpuset="14", matches=True)
    mixed = analyze._container_placement_bullet(m)
    assert "**Container placement matches the resource ledger.** 1 sampled" in mixed
    assert "dockerd declare no compose cpuset" in mixed
    assert "dotnet, postgres were asked for a cpuset that never read back" in mixed


def test_resource_notes_split_throttle_and_oom_and_label_peak_memory(isolated_results):
    """The old report printed one `clean` flag for both counters and called
    memory.peak "peak RSS"; both are fixed and scoped to the aggregated windows."""
    run_id = "syn-notes"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000)
        # the subject took a CFS throttle in one window: the verdicts must diverge.
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000, anon=150_000_000,
                            peak=180_000_000, throttled=1 if block == 0 else 0)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res.by_slot[REFERENCE].throttle_clean is True
    assert res.by_slot[SUBJECT].throttle_clean is False
    assert res.by_slot[SUBJECT].oom_clean is True

    md = result.report_path.read_text(encoding="utf-8")
    assert "cgroup memory.peak (MB, container lifetime)" in md
    assert "peak RSS" not in md
    assert f"CPU throttle counters NON-ZERO in headline.mixed.gc-default/{SUBJECT}" in md
    assert "cgroup OOM events zero" in md
    assert "aggregated confirm + soak windows in the table above" in md
    # pool warmth prints total/max, not the vacuous total/total
    assert "open connections / configured max" in md


def test_a_flag_with_no_failed_gate_names_the_crashed_warmup_attack(isolated_results):
    """The report must not blame a missing artifact for a cause that is on disk.

    A failed warmup ATTACK flags a probe on its own -- no gate decides it -- so
    such a window is flagged with an empty failed-gate count. The note used to
    render "no per-window gate detail on disk" for it, which is a claim about
    the artifacts and false: the ledger records the cause.
    """
    run_id = "syn-attack-failed-note"
    rate = 1000
    flagged = {probe_key(rate, b, REFERENCE) for b in range(2)}
    confirm = _confirm_stage_at(rate, 2, flagged=flagged)
    for probe in confirm.probes:
        probe.gates_evaluated = ["p99_flat", "gc_steady"]
        probe.gates_failed = []
        probe.warmup_attack_failed = (
            probe_key(probe.rate, probe.block, probe.slot) in flagged
        )
    _ledger_with_confirm(run_id, confirm)
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, under_warmed=True)
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    ref = result.cells[0].resource.by_slot[REFERENCE]
    assert ref.posture_only is True
    assert ref.flagged_gate_counts == {}          # no gate decided the flag
    assert ref.n_warmup_attack_failed == 2

    md = result.report_path.read_text(encoding="utf-8")
    assert "no gate failed; 2 of 2 windows recorded a FAILED WARMUP ATTACK" in md
    assert "no per-window gate detail on disk" not in md


def test_a_gate_that_enforces_nothing_is_not_reported_as_having_failed(isolated_results):
    """B2/W21's per-gate `enforce` flag, read by the reader as well as the writer.

    A flatness gate below its quantile-resolution floor records
    `passed: false`, `status: insufficient_data` and `enforced: false`.
    `insufficient_data` is an ENFORCING status, so a reader filtering on the
    status alone calls that a gate that flagged the window -- while
    `ledger.json`, which asks the record's own `enforced`, records no failed
    gate. The two artifacts of one run would then disagree about which gate
    flagged which probe. `p999_flat` is the subject because it is the gate that
    behaved this way on every probe of every tier; W29 removed it from
    `spec/slo.yaml`, and the reader must still get this right for any gate a
    future spec declares above or below a floor.
    """
    run_id = "syn-non-enforcing-gate"
    rate = 1000
    # Block 1's REFERENCE probe is flagged and its ledger record names no
    # evaluated gate, which is the ONE path where the report falls back to
    # `<slot>.warmup.json` for the gate names -- i.e. the path where a reader
    # that trusts `status` alone puts `p999_flat` in the published count.
    fallback_key = probe_key(rate, 1, REFERENCE)
    confirm = _confirm_stage_at(rate, 2, flagged={fallback_key})
    for probe in confirm.probes:
        if probe_key(probe.rate, probe.block, probe.slot) == fallback_key:
            probe.gates_evaluated = []
            probe.gates_failed = []
            continue
        probe.gates_evaluated = ["per_endpoint_min_calls", "p99_flat",
                                 "gc_steady", "pools_warm"]
        probe.gates_failed = []
    _ledger_with_confirm(run_id, confirm)
    base = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
            / "confirm" / f"rate{rate}")
    for block in range(2):
        bd = base / f"block{block}"
        for slot, cpu in ((REFERENCE, 100_000), (SUBJECT, 300_000)):
            uw = slot == REFERENCE and block == 1
            _write_probe_window(bd, slot, reqs=1000, cpu_us=cpu, anon=30_000_000,
                                peak=60_000_000, under_warmed=uw,
                                failed_gates=("p99_flat",) if uw else (),
                                non_enforcing_gates=("p999_flat",))

    result = analyze.analyze_run(run_id)
    ref = result.cells[0].resource.by_slot[REFERENCE]
    assert ref.posture_only is False
    assert ref.n_flagged_excluded == 1
    # The fallback names the gate that DID flag the window and not the one whose
    # record says it enforced nothing.
    assert ref.flagged_gate_counts == {"p99_flat": 1}
    # ...and the non-enforcing gate is not silently dropped either: counted over
    # every window, so the run says out loud that a gate the spec declared
    # `true` enforced on none of them.
    assert ref.non_enforcing_gate_counts == {"p999_flat": 1}
    assert ref.waived_gate_counts == {}          # not a waiver; it was applied

    md = result.report_path.read_text(encoding="utf-8")
    assert "implemented, and ENFORCING NOTHING in this run" in md
    assert "`p999_flat` (3 of 3 windows)" in md
    assert "a probe recorded as warm was NOT held to them" in md


def test_the_reader_asks_the_record_whether_the_gate_enforced(isolated_results):
    """Driven through the real `WarmupState`, not a hand-written record dict.

    A synthetic gate dict can be written to say anything; this asserts the
    reader against what the WRITER actually emits at the harness's own warmup
    rate. `p999_flat` must be invisible to `_failed_gate_names` and named by
    `_non_enforcing_gate_names`, while a genuinely starved `p99_flat` -- same
    status, no `enforce` override -- must still flag.
    """
    from ledgerbench.config import load_slo
    from ledgerbench.runtimestats import RuntimeStats, Snapshot
    from ledgerbench.util.clock import rfc3339_micros

    def snap(i: int) -> Snapshot:
        return Snapshot(
            captured_utc=rfc3339_micros(), monotonic_s=float(i),
            stats=RuntimeStats.model_validate({
                "gc": {"collections": {"gen0": 10 * i, "gen1": i, "gen2": 0},
                       "heap_committed_bytes": 800},
                "db_pool": {"max": 24, "total": 24},
                "redis_pool": {"max": 16, "total": 16},
            }),
        )

    slo = load_slo()
    # W29 dropped `p999_flat` from spec/slo.yaml warmup.gates; the reader's
    # non-enforcing path is still live for any flatness gate below its
    # resolution floor, so the subject is declared here rather than assumed.
    cfg = slo.warmup.model_copy(
        update={"gates": {**slo.warmup.gates, "p999_flat": True}})
    state = warmup.WarmupState(cfg=cfg, slot=REFERENCE, rate=618)
    for i in range(40):
        state.observe(snap(i), p99_ms=5.0, p999_ms=9.0, samples=618)
    rec = {"gates": [json.loads(g.model_dump_json()) for g in state.evaluate()]}
    by_name = {g["name"]: g for g in rec["gates"]}
    assert by_name["p999_flat"]["status"] == warmup.STATUS_INSUFFICIENT_DATA
    assert by_name["p999_flat"]["passed"] is False
    assert by_name["p999_flat"]["enforced"] is False
    assert "p999_flat" not in analyze._failed_gate_names(rec)
    assert analyze._non_enforcing_gate_names(rec) == ["p999_flat"]
    # A record written before `enforced` existed still falls back to the status.
    legacy = {"gates": [{"name": "gc_steady", "passed": False,
                         "status": warmup.STATUS_INSUFFICIENT_DATA}]}
    assert analyze._failed_gate_names(legacy) == ["gc_steady"]
    assert analyze._non_enforcing_gate_names(legacy) == []


def test_a_flag_nothing_on_disk_explains_is_reported_as_unexplained(isolated_results):
    """The other branch: nothing on disk accounts for the flag.

    Neither a failed gate nor a failed warmup attack, so the note must say the
    warmup verdict does not explain it and point at the stage's `excluded` map,
    rather than inventing either cause.
    """
    run_id = "syn-unexplained-flag"
    rate = 1000
    flagged = {probe_key(rate, b, REFERENCE) for b in range(2)}
    confirm = _confirm_stage_at(rate, 2, flagged=flagged)
    for probe in confirm.probes:
        probe.gates_evaluated = ["p99_flat", "gc_steady"]
        probe.gates_failed = []
    _ledger_with_confirm(run_id, confirm)
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, under_warmed=True)
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    assert result.cells[0].resource.by_slot[REFERENCE].n_warmup_attack_failed == 0

    md = result.report_path.read_text(encoding="utf-8")
    assert "none recorded: these windows' records name neither a failed gate" in md
    assert "FAILED WARMUP ATTACK" not in md


def test_a_measured_failure_of_an_unimplemented_gate_is_called_inconsistent(
    isolated_results, monkeypatch
):
    """The cross-check the not-implemented bullet makes, exercised in the
    direction that contradicts it.

    The consistent direction has a test; this one -- a flagged window reporting
    a gate the registry says is not implemented as having FAILED -- did not, and
    after W23 deleted the two real unimplemented gates nothing exercises it by
    accident.
    """
    from ledgerbench.config import load_slo

    synthetic = "synthetic_gate_no_runner_implements"
    real = load_slo()
    patched = real.model_copy(update={
        "warmup": real.warmup.model_copy(
            update={"gates": {**real.warmup.gates, synthetic: True}}
        )
    })
    monkeypatch.setattr(analyze, "load_slo", lambda *a, **k: patched)

    run_id = "syn-scope-gate-contradicted"
    rate = 1000
    flagged = {probe_key(rate, b, REFERENCE) for b in range(2)}
    confirm = _confirm_stage_at(rate, 2, flagged=flagged)
    for probe in confirm.probes:
        key = probe_key(probe.rate, probe.block, probe.slot)
        probe.gates_evaluated = [synthetic]
        probe.gates_failed = [synthetic] if key in flagged else []
    _ledger_with_confirm(run_id, confirm)
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, under_warmed=True)
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert f"INCONSISTENT: this run's flagged windows report `{synthetic}` as failing" in md
    assert "trust the per-probe records over this bullet" in md
    assert "none of which is in the unimplemented list" not in md


def test_the_waiver_bullet_names_the_spec_posture_s_own_rationale(
    isolated_results, monkeypatch
):
    """A waiver declared in `spec/slo.yaml` carries its reason into the report.

    The bullet used to read the CELL's rationale only, so a waiver made in the
    spec layer -- the one inside `config_hash` -- rendered as "rationale: none
    declared" while a rationale was in fact required and present.
    """
    from ledgerbench.config import PostureWaiver, load_slo

    why = "SYNTHETIC: this posture cannot open the pool"
    real = load_slo()
    patched = real.model_copy(update={
        "warmup": real.warmup.model_copy(update={
            "gates_by_posture": {
                "headline.mixed.gc-default": PostureWaiver(
                    gates={"gc_steady": False}, rationale=why,
                )
            }
        })
    })
    monkeypatch.setattr(analyze, "load_slo", lambda *a, **k: patched)

    run_id = "syn-waived-spec"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, waived_gates=("gc_steady",))
        _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert f"rationale: spec posture: {why}" in md
    assert "rationale: none declared" not in md


# --------------------------------------------------------------------------- #
# rendered claims: every verdict sentence must be as strong as its own number
# --------------------------------------------------------------------------- #
def test_the_runtime_stats_bullet_never_claims_edges_or_a_trace_it_has_not_got(
    isolated_results,
):
    """"Each carries both edges and every counter ...; the trace is the `.jsonl`
    beside it" was a universal claim over summaries the NEXT clause reports as
    `not_measured`.

    Three counts, three ways to be wrong: a not_measured window has neither
    edge and no `.jsonl` (`PollWindow.write` writes it only `if self.snapshots`),
    and a one-poll window has both edges but an empty delta block
    (`PollWindow.deltas` returns `{}` when start is end).
    """
    run_id = "syn-rs-content"
    _scoped_ledger(run_id, "smoke")
    base = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
            / "confirm" / "rate1000" / "block0")
    base.mkdir(parents=True, exist_ok=True)
    # a full window: edges, a delta block, a trace
    _write_runtime_stats_summary(
        base / f"{REFERENCE}.runtime_stats.summary.json", phase="measured",
        measured=True, edges=True, deltas={"gc_collections_total": 3}, trace=True)
    # one successful poll: edges, NO delta, and a trace (one snapshot is one line)
    _write_runtime_stats_summary(
        base / f"{SUBJECT}.runtime_stats.summary.json", phase="measured",
        measured=True, edges=True, trace=True)
    # no poll at all: no edges, no delta, no trace
    _write_runtime_stats_summary(
        base / f"{SUBJECT}.warmup.runtime_stats.summary.json", phase="warmup",
        measured=False)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "2 of them carry both window edges" in md
    assert "1 carry a non-empty end-minus-start delta block" in md
    assert "2 per-poll `.jsonl` traces sit beside them" in md
    # the universal claim, in either of the two wordings it has worn
    assert "Each carries both edges" not in md
    assert "the per-poll trace is the `.jsonl` beside it" not in md


def test_the_runtime_stats_head_claims_no_poll_when_nothing_is_on_disk(
    isolated_results,
):
    """A run with zero poller artifacts evidences no poll, at 1 Hz or otherwise.

    The head asserted "The 1 Hz poller runs across warmup as well as the
    measured window" BEFORE the branch, so the no-summaries path rendered it and
    then said the run left nothing on disk.
    """
    run_id = "syn-rs-head"
    _scoped_ledger(run_id, "smoke")
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "left NO `*.runtime_stats.summary.json` on disk" in md
    assert "The 1 Hz poller is CONFIGURED to run across warmup" in md
    assert "this run evidences no poll at all" in md
    assert "The 1 Hz poller runs across warmup" not in md


def _curve_ledger(run_id: str, *, rates, sub_us, confirm_windows: bool = True) -> Ledger:
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET, scale="full")
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default",
                                ladder=_ladder_stage(), confirm=_confirm_stage()))
    led.save()
    base = (paths_run_dir(run_id) / "cells" / "headline.mixed.gc-default" / "ladder")
    # `us is None` at a rung writes the REFERENCE window only, so that rung
    # exists in the table with a hole in the subject column -- the shape the
    # rung-count claim has to survive.
    for rate, us in zip(rates, sub_us):
        for block in (0, 1):
            bd = base / f"rate{rate}" / f"block{block}"
            _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000,
                                anon=30_000_000, peak=60_000_000)
            if us is not None:
                _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=us,
                                    anon=150_000_000, peak=180_000_000)
    # Confirm windows too, so the roll-up section renders -- AND so the
    # operating point appears on the CPU-vs-rate curve, which since W24 walks
    # ladder+confirm+soak. `confirm_windows=False` is the ladder-only shape, kept
    # for the tests that need a curve of a chosen number of rungs.
    if confirm_windows:
        _write_confirm_windows(run_id, 1500)
    return led


def _write_confirm_windows(run_id: str, rate: int, blocks: int = 2) -> None:
    base = (paths_run_dir(run_id) / "cells" / "headline.mixed.gc-default"
            / "confirm" / f"rate{rate}")
    for block in range(blocks):
        _write_probe_window(base / f"block{block}", REFERENCE, reqs=1000,
                            cpu_us=100_000, anon=30_000_000, peak=60_000_000)
        _write_probe_window(base / f"block{block}", SUBJECT, reqs=1000,
                            cpu_us=300_000, anon=150_000_000, peak=180_000_000)


def paths_run_dir(run_id):
    from ledgerbench.util import paths

    return paths.run_dir(run_id)


def test_the_rate_dependence_verdict_is_read_off_this_runs_own_cov(isolated_results):
    """"It does move." was round 1's finding (R1-A1) as fixed text.

    CLAUDE.md forbids a round-1 number beside a round-2 one, and a verdict is a
    number quoted in words. Here the subject's column DOES move, so the verdict
    must name this run's own coefficient of variation rather than assert one.
    """
    run_id = "syn-rate-moves"
    _curve_ledger(run_id, rates=(1000, 2000), sub_us=(500_000, 300_000))
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    # 3 rungs, not 2: the CONFIRM window at 1500 rps is on this curve since W24.
    # Ladder-only left each slot's OPERATING POINT -- the one rate the headline
    # is actually read at -- off the curve entirely.
    assert (f"the largest is 31.5% ({SUBJECT_LABEL}, headline.mixed.gc-default, over "
            "the 3 rungs that slot has a value at)") in md
    # The roll-up's own sentence, rung count included. It had no assertion at
    # all: deleting `over {rd.cv_n_rungs} rungs` from the MOVES branch, or
    # getting the count wrong there, left the whole suite green while the
    # verdict beside it was pinned to the character.
    assert ("Per-request CPU MOVED across this run's ladder rungs (largest "
            f"per-slot coefficient of variation 31.5%, {SUBJECT_LABEL}, over 3 "
            "rungs)") in md
    assert "It does move." not in md
    assert "Per-request CPU is NOT rate-independent" not in md


def test_a_flat_curve_is_never_published_as_moving(isolated_results):
    """The defect in one line: a run whose columns are flat used to render
    "It does move." directly above its own derived "0.0%"."""
    run_id = "syn-rate-flat"
    _curve_ledger(run_id, rates=(1000, 2000), sub_us=(300_000, 300_000))
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert f"Coefficient of variation, per slot: {SUBJECT_LABEL} 0.0% over 3 rungs" in md
    assert "every per-slot coefficient of variation rounds to 0.0%" in md
    assert "no slot's per-request CPU moved measurably" in md
    assert "MOVED across this run's ladder rungs" not in md
    assert "It does move." not in md


def test_a_rung_count_beside_a_cov_counts_only_the_rungs_that_cov_saw(
    isolated_results,
):
    """A rung the winning slot has no window at is `n/a` in the table and was
    dropped by `_cv_pct`, so no sentence may count it.

    The verdict used to quote `len(cvr.rungs)` -- the height of the TABLE -- so a
    three-rung ladder with a hole in one column rendered "across this run's 3
    rungs" three lines under a row reading `| 4000 | n/a | 0/0 |`. That is the
    same class the whole `_RateDependence` dataclass exists to prevent, one
    field over.
    """
    run_id = "syn-rate-hole"
    _curve_ledger(run_id, rates=(1000, 2000, 4000), sub_us=(300_000, 300_000, None))
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")

    assert "| 4000 |" in md                       # the rung is in the table
    assert "Coefficient of variation, per slot: " in md
    # 3 vs 4: the subject has no window at 4000, the reference has one at every
    # ladder rung AND at the confirm rate. Each slot counts its own.
    assert f"{SUBJECT_LABEL} 0.0% over 3 rungs" in md   # the slot with the hole
    assert f"{REFERENCE_LABEL} 0.0% over 4 rungs" in md  # the slot without one
    assert "over the 3 rungs that slot has a value at" in md
    # The SAME count in the two other sentences that carry one. Both are checked
    # with their own surrounding words, because a single `"spans 2 rungs" in md`
    # passes as long as ONE of the two still renders it -- so dropping it from
    # either the roll-up or the cost section was invisible.
    assert ("every per-slot coefficient of variation rounds to 0.0%; the largest "
            "spans 3 rungs), which is a result about the rungs this ladder "
            "tested") in md
    assert ("the largest coefficient of variation spans 3 rungs), but these "
            "dollars are still taken at the operating rate(s)") in md
    assert "across this run's 4 rungs" not in md   # the old, table-height claim


def test_the_resource_lead_prints_rates_not_a_python_list(isolated_results):
    """`f"rate(s) {rates} rps"` over a `list[int]` rendered `[1000] rps`."""
    run_id = "syn-lead-rates"
    _curve_ledger(run_id, rates=(1000, 2000), sub_us=(300_000, 300_000))
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")

    assert "at the offered rate(s) 1,500 rps" in md
    assert "rate(s) [" not in md


def test_meets_slo_stays_a_separate_quantity_with_the_opposite_boundary(
    isolated_results,
):
    """At exactly 20.000 ms a slot has BOTH crossed and met its promise.

    The two verdicts are deliberately different quantities: the ladder brackets
    on an ISOTONIC fit of the LADDER rungs (`>=`), `meets_slo` is the arithmetic
    mean of the CONFIRM/SOAK windows (`<=`). A slot sitting exactly at its knee
    IS meeting the promise, and that has to stay expressible -- so collapsing the
    two, or pointing the inequalities the same way, is a defect.

    Two live sentences claimed `meets_slo` was "the same rung-mean rule the
    ladder bracketed on". Both were false the moment the ladder started
    bracketing isotonically, and both are gone.
    """
    run_id = "syn-boundary"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET, scale="full")
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    # The top ladder rung sits at EXACTLY the SLO, so the crossing is at 2000.
    for rate, val in ((1000, 10.0), (2000, 20.0)):
        for block in (0, 1):
            for slot in SLOTS:
                key = probe_key(rate, block, slot)
                rec.probes.append(_done_probe(rate, block, slot))
                p99[key] = val
                under_warmed[key] = False
    rec.data = _ladder_data(
        crossed_by_slot={sid: 2000 for sid in SLOTS},
        bracket_by_slot={sid: [1000, 2000] for sid in SLOTS},
        p99_ms=p99, under_warmed=under_warmed, rates=[1000, 2000],
    )
    confirm = StageRecord(state=State.DONE)
    cp99: dict[str, float] = {}
    cuw: dict[str, bool] = {}
    for block in range(3):
        for slot in SLOTS:
            key = probe_key(2000, block, slot)
            confirm.probes.append(_done_probe(2000, block, slot))
            cp99[key] = 20.0                      # EXACTLY the promise
            cuw[key] = False
    confirm.data = _confirm_data({sid: 2000 for sid in SLOTS},
                                 p99_ms=cp99, under_warmed=cuw)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default",
                                ladder=rec, confirm=confirm))
    led.save()

    result = analyze.analyze_run(run_id)
    cell = result.cells[0]
    for sid in SLOTS:
        assert cell.knee_by_slot[sid].crossed is True          # >= at the boundary
        assert abs(cell.knee_by_slot[sid].knee_rps - 2000.0) < 1.0
    rows = {(r.rate, r.slot): r for r in cell.slo_at_rates}
    for sid in SLOTS:
        assert rows[(2000, sid)].meets_slo is True             # <= at the boundary
    # exactly AT the knee is not above it, so nothing is flagged inconsistent
    assert cell.slo_consistency == []


def test_a_slot_that_meets_the_slo_above_its_own_knee_is_flagged_not_narrated(
    isolated_results,
):
    """The one combination a reader must not skim past, as a FLAG.

    `meets_slo=yes` at a rate strictly above that slot's own fitted knee means
    either the knee is fitted low (ladder reps are 2-3) or the confirm windows
    were kinder than the ladder rungs at the same rate. Round 1 narrated this
    contradiction in a resource-section footnote; it is now a record in
    fits.json with both numbers attached.
    """
    run_id = "syn-inconsistent"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET, scale="full")
    # knee at 1500 (10 ms -> 30 ms between 1000 and 2000), confirm at 2000 where
    # the windows nonetheless come in at 8 ms.
    confirm = StageRecord(state=State.DONE)
    cp99: dict[str, float] = {}
    cuw: dict[str, bool] = {}
    for block in range(3):
        for slot in SLOTS:
            key = probe_key(2000, block, slot)
            confirm.probes.append(_done_probe(2000, block, slot))
            cp99[key] = 8.0
            cuw[key] = False
    confirm.data = _confirm_data({sid: 2000 for sid in SLOTS},
                                 p99_ms=cp99, under_warmed=cuw)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default",
                                ladder=_ladder_stage(), confirm=confirm))
    led.save()

    result = analyze.analyze_run(run_id)
    flags = {f.slot: f for f in result.cells[0].slo_consistency}
    assert set(flags) == set(SLOTS)
    for sid in SLOTS:
        assert flags[sid].rate == 2000
        assert abs(flags[sid].knee_rps - 1500.0) < 1.0
        assert flags[sid].mean_p99_ms == 8.0

    md = result.report_path.read_text(encoding="utf-8")
    assert "CONSISTENCY FLAGS" in md
    assert "but its own fitted knee is 1500 rps" in md
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert len(fits["cells"][0]["slo_consistency"]) == len(SLOTS)


def test_a_posture_only_rung_is_not_evidence_about_rate_dependence(isolated_results):
    """A rung whose only windows were FLAGGED is out of the CV, and out of the
    count printed beside it.

    "Is per-request cost rate-independent?" is a question about a WARM stack. A
    rung carried entirely by under-warmed windows answers a different one, and
    letting it into the dispersion makes a warmup artefact read as rate
    sensitivity. The value itself stays in the table, marked `*`, so nothing is
    hidden -- it is only kept out of the statistic.
    """
    run_id = "syn-posture-cv"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET, scale="full")
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default",
                                ladder=_ladder_stage(), confirm=_confirm_stage()))
    led.save()
    base = paths_run_dir(run_id) / "cells" / "headline.mixed.gc-default" / "ladder"
    # Two WARM rungs at an identical 0.30 CPU-ms/req, and one flagged rung at
    # 0.90. Included, the CV is large; excluded, it is exactly 0.0%.
    for rate, us, flagged in ((1000, 300_000, False), (2000, 300_000, False),
                              (4000, 900_000, True)):
        for block_i in (0, 1):
            bd = base / f"rate{rate}" / f"block{block_i}"
            for slot in SLOTS:
                _write_probe_window(
                    bd, slot, reqs=1000, cpu_us=us, anon=30_000_000, peak=60_000_000,
                    under_warmed=flagged,
                    failed_gates=("gc_steady",) if flagged else (),
                )
    # the confirm windows sit at the warm rungs' EXACT cost, so the operating
    # point (which is on this curve since W24) adds no dispersion of its own
    cbase = paths_run_dir(run_id) / "cells" / "headline.mixed.gc-default" / "confirm"
    for block_i in (0, 1):
        for slot in SLOTS:
            _write_probe_window(cbase / "rate1500" / f"block{block_i}", slot,
                                reqs=1000, cpu_us=300_000, anon=30_000_000,
                                peak=60_000_000)

    result = analyze.analyze_run(run_id)
    cvr = result.cells[0].cpu_vs_rate
    rung4000 = next(r for r in cvr.rungs if r.rate == 4000)
    assert all(v.posture_only for v in rung4000.by_slot.values())
    for sid in SLOTS:
        assert cvr.cv_pct_by_slot[sid] == 0.0, sid
        # 3 warm rungs (1000, 1500, 2000), NOT the 4 the table shows
        assert cvr.n_values(sid) == 3, sid

    md = result.report_path.read_text(encoding="utf-8")
    assert "| 4000 |" in md                    # still in the table, marked
    assert "0.0% over 3 rungs" in md


def test_the_roll_up_selects_each_slot_s_own_knee_and_nothing_else(
    isolated_results,
):
    """The headline roll-up is at each slot's OWN knee: one operating point each.

    Two defects meet here. Until W24 `_resource_cost_for_cell` applied `rate <=
    the POOLED knee` to BOTH slots and `_window_rows` filtered language-blind,
    so one slot's degradation selected the other slot's rows. W24 made the bound
    per slot but LEFT IT A BOUND -- and in the same pass gave every slot whose
    knee is above the common rate a SECOND confirm probe at that common rate,
    which is sub-knee for it by construction. `rate <= own knee` admits both, so
    one slot's figure became a request-weighted blend of two operating points
    while the other's stayed a single point.

    The fixture below is that exact shape: the REFERENCE confirms at 2,500 (its
    own knee) AND at 1,500 (the common rate, which is the SUBJECT's knee), and
    its windows at the two rates cost DIFFERENT CPU per request, so a blend and
    a selection cannot produce the same number. Blended: 0.3 CPU-ms/req for the
    reference and a 0.67x ratio. Selected: 0.2 and 1.00x.
    """
    run_id = "syn-perslot-filter"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET, scale="full")
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    # SUBJECT crosses 20 ms at 1500; REFERENCE not until 2500.
    for rate, (ref_v, sub_v) in ((1000, (5.0, 10.0)), (2000, (10.0, 30.0)),
                                 (3000, (30.0, 60.0))):
        for block in (0, 1):
            for slot, val in ((REFERENCE, ref_v), (SUBJECT, sub_v)):
                key = probe_key(rate, block, slot)
                rec.probes.append(_done_probe(rate, block, slot))
                p99[key] = val
                under_warmed[key] = False
    rec.data = _ladder_data(
        crossed_by_slot={SUBJECT: 2000, REFERENCE: 3000},
        bracket_by_slot={SUBJECT: [1000, 2000], REFERENCE: [2000, 3000]},
        p99_ms=p99, under_warmed=under_warmed, rates=[1000, 2000, 3000],
    )
    confirm = StageRecord(state=State.DONE)
    cp99: dict[str, float] = {}
    cuw: dict[str, bool] = {}
    for slot, rate in ((SUBJECT, 1500), (REFERENCE, 2500)):
        for block in range(2):
            key = probe_key(rate, block, slot)
            confirm.probes.append(_done_probe(rate, block, slot))
            cp99[key] = 19.0
            cuw[key] = False
    confirm.data = _confirm_data({SUBJECT: 1500, REFERENCE: 2500},
                                 p99_ms=cp99, under_warmed=cuw)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default",
                                ladder=rec, confirm=confirm))
    led.save()

    # BOTH slots have windows at BOTH rates on disk, so the filter has real work.
    # The REFERENCE's 1,500 rps windows are its COMMON-RATE probes and cost 2x
    # per request what its own-knee windows do: admitting them moves the number.
    cbase = paths_run_dir(run_id) / "cells" / "headline.mixed.gc-default" / "confirm"
    for rate in (1500, 2500):
        for block in range(2):
            for slot in SLOTS:
                cpu_us = 400_000 if (slot == REFERENCE and rate == 1500) else 200_000
                _write_probe_window(cbase / f"rate{rate}" / f"block{block}", slot,
                                    reqs=1000, cpu_us=cpu_us, anon=30_000_000,
                                    peak=60_000_000)

    result = analyze.analyze_run(run_id)
    knees = result.cells[0].knee_by_slot
    assert abs(knees[SUBJECT].knee_rps - 1500.0) < 1.0
    assert abs(knees[REFERENCE].knee_rps - 2500.0) < 1.0
    res = result.cells[0].resource
    assert res is not None and res.available is True
    # ONE operating point per slot: the rate its own knee put it at.
    assert res.by_slot[SUBJECT].n_windows == 2          # rate 1500, 2 blocks
    assert res.by_slot[REFERENCE].n_windows == 2        # rate 2500 ONLY
    assert res.operating_rates == [1500, 2500]
    assert res.operating_rate_by_slot == {SUBJECT: 1500, REFERENCE: 2500}
    # The number itself, not just the counts: the blend would publish 0.3 / 0.67x.
    assert abs(res.by_slot[REFERENCE].cpu_ms_per_req - 0.2) < 1e-9
    assert abs(res.by_slot[SUBJECT].cpu_ms_per_req - 0.2) < 1e-9
    assert abs(res.cpu_ratio - 1.0) < 1e-9
    # ...and the roll-up SAYS which scope it is, and how it chose it.
    assert res.scope == analyze.SCOPE_OWN_KNEE
    assert res.operating_point_basis == "declared"

    md = result.report_path.read_text(encoding="utf-8")
    assert "SCOPE: `own_knee`" in md
    assert "are NOT in this roll-up" in md
    # the common-rate contrast is still published -- as the PAIRED verdict
    assert analyze.SCOPE_COMMON_RATE in {
        v.verdict_scope for v in result.cells[0].paired_verdicts
    }


def test_a_knee_that_rounds_up_does_not_empty_the_whole_resource_section(
    isolated_results,
):
    """THE ROUNDING TRAP, and it would have looked entirely honest.

    Each slot confirms at `int(round(knee_rps))`. For any knee with a fractional
    part >= 0.5 that rate is STRICTLY GREATER than the knee, so the roll-up's
    `rate <= knee` filter drops every confirm and soak window of that slot and
    publishes `available=False` with a not-captured line -- for a run that
    measured all of it.
    """
    run_id = "syn-round"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET, scale="full")
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    # 1000 -> 10 ms, 2000 -> 32 ms. Isotonic crossing of 20 ms is
    # 1000 + (20-10)/(32-10) * 1000 = 1454.5454..., so int(round(.)) = 1455 > knee.
    for rate, val in ((1000, 10.0), (2000, 32.0)):
        for block in (0, 1):
            for slot in SLOTS:
                key = probe_key(rate, block, slot)
                rec.probes.append(_done_probe(rate, block, slot))
                p99[key] = val
                under_warmed[key] = False
    rec.data = _ladder_data(
        crossed_by_slot={sid: 2000 for sid in SLOTS},
        bracket_by_slot={sid: [1000, 2000] for sid in SLOTS},
        p99_ms=p99, under_warmed=under_warmed, rates=[1000, 2000],
    )
    confirm = _confirm_stage_at(1455, 3)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default",
                                ladder=rec, confirm=confirm))
    led.save()
    _write_confirm_windows(run_id, 1455, blocks=3)

    result = analyze.analyze_run(run_id)
    knee = result.cells[0].knee_by_slot[SUBJECT]
    assert 1454.0 < knee.knee_rps < 1455.0        # the confirm rate is ABOVE it
    res = result.cells[0].resource
    assert res is not None and res.available is True
    assert res.operating_rates == [1455]
    md = result.report_path.read_text(encoding="utf-8")
    assert "Not available in this run: no SUT cgroup-v2 resource summaries" not in md


def test_one_rung_establishes_nothing_about_rate_dependence(isolated_results):
    """`_cv_pct` needs n >= 2, so a one-rung run has not tested the question."""
    run_id = "syn-rate-one"
    led = Ledger(run_id=run_id, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET, scale="full")
    # The confirm stage DECLARES 1000 rps, matching the single ladder rung: the
    # roll-up selects the declared operating point, so a confirm stage declaring
    # 1500 with windows at 1000 is a fixture that contradicts itself.
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default",
                                ladder=_ladder_stage(),
                                confirm=_confirm_stage_at(1000, 3)))
    led.save()
    bd = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
          / "ladder" / "rate1000" / "block0")
    _write_probe_window(bd, REFERENCE, reqs=1000, cpu_us=100_000,
                        anon=30_000_000, peak=60_000_000)
    _write_probe_window(bd, SUBJECT, reqs=1000, cpu_us=300_000,
                        anon=150_000_000, peak=180_000_000)
    # The confirm windows sit at the SAME rate as the single ladder rung. Since
    # W24 the curve walks ladder+confirm+soak, so a confirm window at a different
    # rate would ADD a rung and this fixture would no longer be a one-rung run --
    # which is the whole condition under test.
    _write_confirm_windows(run_id, 1000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "does not establish it either way" in md
    assert "is NOT ESTABLISHED by this run" in md
    assert "It does move." not in md


def test_the_driver_caveat_names_only_the_drivers_this_runs_slots_use(
    isolated_results,
):
    """The caveat is derived from the run's slots, never fixed text.

    (The paragraph once named "Npgsql ... while PgJDBC/HikariCP do not" as fixed
    text above a shakedown table whose two slots were both Npgsql.) The default
    pair is the headline java-vs-dotnet set now, so the rendered report must
    take the OTHER branch: two drivers, asymmetry named, nothing cancelling.
    """
    run_id = "syn-driver"
    _synthetic_ledger(run_id)                    # confirm declares 1500 rps
    base = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
            / "confirm" / "rate1500")
    for block in range(2):
        _write_probe_window(base / f"block{block}", REFERENCE, reqs=1000,
                            cpu_us=100_000, anon=30_000_000, peak=60_000_000,
                            db_cpu_us=40_000)
        _write_probe_window(base / f"block{block}", SUBJECT, reqs=1000,
                            cpu_us=300_000, anon=150_000_000, peak=180_000_000,
                            db_cpu_us=60_000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "do NOT share a driver" in md
    assert "PgJDBC + HikariCP" in md and "Npgsql" in md
    assert "they share one driver" not in md
    assert "cancelling out of the ratio between them" not in md
    assert _pooled_knee_text(md) == ""
    assert _pooled_knee_text(md + "\nthe pooled knee was fitted over both slots"
                             ) == "the pooled knee was fitted over both slots"


def test_a_two_language_slot_set_gets_the_driver_asymmetry_disclosed():
    """Direct check of the two-driver branch's wording."""
    java_dotnet = registry.get_slot_set("headline")
    text = "".join(analyze._db_cpu_caveats(java_dotnet))
    assert "do NOT share a driver" in text
    assert "PgJDBC + HikariCP" in text and "Npgsql" in text


def test_a_shared_driver_slot_set_says_the_confound_cancels():
    """The shakedown branch, kept covered now that the default pair is the
    headline one: both slots on Npgsql means one driver, no asymmetry, and the
    reset cost named as cancelling out of the ratio."""
    shakedown = registry.get_slot_set("shakedown")
    text = "".join(analyze._db_cpu_caveats(shakedown))
    assert "NOT a confound between this run's slots: they share one driver" in text
    assert "PgJDBC" not in text and "HikariCP" not in text
    assert "cancelling out of the ratio between them" in text
    # A CORRECT caveat says "pooled" here -- Npgsql's connection-pool reset.
    assert "pooled CHECKOUT" in text


def test_the_paired_section_counts_the_scopes_it_actually_rendered(isolated_results):
    """"this run carries N: <rates>" described a per-rate section that is gone.

    Re-pinned in W24: the section now carries SCOPES, and a run whose slots share
    a knee has only the own-knee one -- there is no second rate to contrast at,
    so claiming two would be the same class of overclaim the old fixed plural was.
    """
    one = "syn-paired-one"
    _synthetic_ledger(one, cost_windows=True)      # both slots confirm at 1500
    md = analyze.analyze_run(one).report_path.read_text(encoding="utf-8")
    assert "this run carries 1: `own_knee`." in md
    assert "no `common_rate` verdict exists" in md

    two = "syn-paired-two"
    led = Ledger(run_id=two, config_hash=_live_config_hash(), rng_seed=42,
                 slot_set=DEFAULT_SLOT_SET)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default",
                                ladder=_split_knee_ladder(),
                                confirm=_per_slot_rate_confirm()))
    led.save()
    _write_cost_confirm_windows(two, 0, blocks=3,
                                rates_by_slot={REFERENCE: 1500, SUBJECT: 2000})
    _write_cost_confirm_windows(two, 0, blocks=3, sub_us=150_000,
                                rates_by_slot={REFERENCE: 1500, SUBJECT: 1500})
    md2 = analyze.analyze_run(two).report_path.read_text(encoding="utf-8")
    assert "this run carries 2: `common_rate`, `own_knee`" in md2


# --------------------------------------------------------------------------- #
# the measurement tier: a smoke report may not read like a measured one
# --------------------------------------------------------------------------- #
def test_a_smoke_report_is_banner_marked_functional_only(isolated_results):
    """A `smoke` run published knee fits, SLO verdicts, a CPU ratio, a paired
    verdict and a cost roll-up with nothing anywhere saying the tier is
    functional-only. CLAUDE.md: smoke "proves plumbing only ... is not
    measurement-grade". Given round 1's failure mode this is the likeliest route
    by which a non-measurement number gets quoted as one, so the banner is the
    first thing on the page AND a field in fits.json."""
    run_id = "syn-tier-smoke"
    _scoped_ledger(run_id, "smoke")
    result = analyze.analyze_run(run_id)
    md = result.report_path.read_text(encoding="utf-8")
    body = md.split("\n")
    assert body[0].startswith("# Ledgerline analysis report")
    assert "FUNCTIONAL REHEARSAL ONLY -- NOT MEASUREMENT-GRADE" in body[2]
    assert "No figure in this report may be quoted as a measurement." in md
    # the deviations are DIFFED against `full`, not listed
    assert "`measure_s` 10 vs full's 120" in md
    assert "`warmup_cap_s` 15 vs full's 300" in md

    tier = json.loads(result.fits_path.read_text(encoding="utf-8"))["tier"]
    assert tier == {
        "scale": "smoke", "resolved": True, "functional_only": True,
        "measurement_grade": False,
        "deviations_from_full": tier["deviations_from_full"],
    }
    assert tier["deviations_from_full"]["measure_s"] == {"this_tier": 10, "full": 120}


def test_the_rehearsal_tier_says_it_is_measured_but_reduced(isolated_results):
    """`rehearsal` is measurement-grade (`functional_only=False`) and reduced;
    the banner must say BOTH, and name the dials that differ."""
    run_id = "syn-tier-rehearsal"
    _scoped_ledger(run_id, "rehearsal")
    result = analyze.analyze_run(run_id)
    md = result.report_path.read_text(encoding="utf-8")
    assert "Measurement tier `rehearsal`: measurement-grade, REDUCED" in md
    assert "`confirm_blocks` 2 vs full's 10" in md
    assert "FUNCTIONAL REHEARSAL ONLY" not in md
    tier = json.loads(result.fits_path.read_text(encoding="utf-8"))["tier"]
    assert tier["functional_only"] is False and tier["measurement_grade"] is True


def test_a_full_tier_report_says_nothing_is_reduced(isolated_results):
    run_id = "syn-tier-full"
    _scoped_ledger(run_id, "full")
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "Measurement tier `full`: measurement-grade." in md
    assert "matches the `full` profile" in md
    assert "REDUCED" not in md and "FUNCTIONAL REHEARSAL ONLY" not in md


def test_a_ledger_with_no_scale_is_not_silently_called_measured(isolated_results):
    """`Ledger.scale` is optional; an unattributed run must say so rather than
    inherit the full tier's banner by default."""
    run_id = "syn-tier-none"
    _synthetic_ledger(run_id)                    # no scale pinned
    result = analyze.analyze_run(run_id)
    md = result.report_path.read_text(encoding="utf-8")
    assert "MEASUREMENT TIER NOT RECORDED" in md
    tier = json.loads(result.fits_path.read_text(encoding="utf-8"))["tier"]
    assert tier["resolved"] is False and tier["measurement_grade"] is None


def test_an_unresolvable_scale_is_reported_not_assumed(isolated_results, monkeypatch):
    run_id = "syn-tier-bad"
    led = _scoped_ledger(run_id, "smoke")
    led.scale = "no-such-tier"
    led.save()
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "MEASUREMENT TIER `no-such-tier` COULD NOT BE RESOLVED" in md


# --------------------------------------------------------------------------- #
# R1-B3: the flushed GC counters reach a reader
# --------------------------------------------------------------------------- #
_GC_DELTAS = {
    "gc_collections_gen0": 10, "gc_collections_gen1": 4, "gc_collections_gen2": None,
    "gc_collections_total": 14,
    "gc_collections_generations_summed": ["gen0", "gen1"],
    "gc_pause_total_seconds": 0.5, "gc_allocated_total_bytes": 2 << 20,
}
_GC_LEVELS = {
    "gc_heap_committed_bytes": {"start": 1 << 20, "end": 3 << 20, "max": 3 << 20},
    "db_pool_total": {"start": 20, "end": 24, "max": 24},
    "db_pool_max": {"start": 24, "end": 24, "max": 24},
}


def test_the_flushed_gc_counters_reach_the_report(isolated_results):
    """R1-B3: round 1 polled these and discarded them, so its artifacts carry no
    GC evidence at all. W17 flushes them; this is the reader that publishes them,
    and it is the production caller of `analysis.report.gc_pool_table`."""
    run_id = "syn-gc"
    _scoped_ledger(run_id, "full")
    base = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
            / "confirm" / "rate1000" / "block0")
    base.mkdir(parents=True, exist_ok=True)
    _write_runtime_stats_summary(
        base / f"{REFERENCE}.runtime_stats.summary.json", phase="measured",
        measured=True, slot=REFERENCE, edges=True, deltas=_GC_DELTAS,
        levels=_GC_LEVELS, trace=True)
    # a WARMUP window with the same numbers: it must not be summed in
    _write_runtime_stats_summary(
        base / f"{REFERENCE}.warmup.runtime_stats.summary.json", phase="warmup",
        measured=True, slot=REFERENCE, edges=True, deltas=_GC_DELTAS,
        levels=_GC_LEVELS, trace=True)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "## GC and pool activity" in md
    assert "Summed over the 1 `phase: measured` window summaries" in md
    assert (f"| {REFERENCE_LABEL} | 1 | gen0, gen1 | 10 (1w) | 4 (1w) | not reported "
            "| 14 (1w) | 0.500 (1w) |") in md
    # a nullable generation is never summed as zero
    assert "| 0 (1w) |" not in md
    assert "24/24" in md


def test_a_runtime_that_reports_no_gc_counter_is_not_published_as_zero(
    isolated_results,
):
    """The JVM's honest answer, rendered honestly.

    All three generations are a present null on the JVM (G1's old-gen bean
    counts Full GC only and reads 0 where generational ZGC reads 3 on identical
    work), so the summed total is null too, and `pause_total_seconds` is null
    under ZGC because `getCollectionTime()` is millisecond-granular. Summed with
    `or 0` -- which is what this reader did -- the report published `0`
    collections and `0.000` s of pause: a collector that never collected and
    never paused, stated as a measurement. That is R1-B1's shape in the report
    rather than in a gate.
    """
    run_id = "syn-gc-null"
    _scoped_ledger(run_id, "full")
    base = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
            / "confirm" / "rate1000" / "block0")
    base.mkdir(parents=True, exist_ok=True)
    _write_runtime_stats_summary(
        base / f"{REFERENCE}.runtime_stats.summary.json", phase="measured",
        measured=True, slot=REFERENCE, edges=True,
        deltas={"gc_collections_gen0": None, "gc_collections_gen1": None,
                "gc_collections_gen2": None, "gc_collections_total": None,
                "gc_collections_generations_summed": [],
                "gc_pause_total_seconds": None,
                "gc_allocated_total_bytes": 2 << 20},
        levels=_GC_LEVELS, trace=True)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert (f"| {REFERENCE_LABEL} | 1 | none named | not reported | not reported "
            "| not reported | not reported | not reported |") in md
    # Scoped to the GC section: `| 0 |` is a legitimate cell elsewhere in the
    # report (the warmup census prints a flagged count of 0), and the claim
    # under test is about how THIS table renders an absent counter.
    gc_section = md.split("## GC and pool activity", 1)[1]
    assert "| 0 |" not in gc_section
    assert "0.000" not in gc_section


def test_the_gc_section_names_its_absence_rather_than_omitting_it(isolated_results):
    run_id = "syn-gc-none"
    _scoped_ledger(run_id, "full")
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "## GC and pool activity" in md
    assert "this run has no GC evidence at all" in md


# --------------------------------------------------------------------------- #
# the dollar figure PROGRESS commits to, with its price stated
# --------------------------------------------------------------------------- #
def test_the_dollar_figure_is_published_with_the_price_it_assumed(isolated_results):
    """`cost_table`/`cost_per_million`/`CostInputs` had no production caller while
    the scope section said the dollar figure was "not published" -- wired and
    withheld at once, which docs/p1-plan.md 5.5 forbids."""
    run_id = "syn-cost"
    _synthetic_ledger(run_id)                    # confirm declares 1500 rps
    base = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
            / "confirm" / "rate1500")
    for block in range(2):
        _write_probe_window(base / f"block{block}", REFERENCE, reqs=1000,
                            cpu_us=100_000, anon=30_000_000, peak=60_000_000)
        _write_probe_window(base / f"block{block}", SUBJECT, reqs=1000,
                            cpu_us=300_000, anon=150_000_000, peak=180_000_000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "## Illustrative $/M-request cost" in md
    assert "$0.0400 per vCPU-hour is a constant chosen by this harness" in md
    # 0.3 CPU-ms/req * 0.04 / 3.6 = 0.003333...; two decimals published $0.00
    assert "$0.0033" in md and "$0.0011" in md
    # and the scope section must not ALSO say it was withheld
    assert "Not published as a dollar figure" not in md
    assert "Not published in this run" not in md


def test_the_dollar_figure_is_absent_only_when_there_is_nothing_to_scale(
    isolated_results,
):
    run_id = "syn-cost-none"
    _synthetic_ledger(run_id)                    # no cgroup windows at all
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "## Illustrative $/M-request cost" not in md
    assert "no cell here has a request-weighted CPU-ms/request to scale" in md


# --------------------------------------------------------------------------- #
# W21: the WarmupCensus. Round 1's 59.5% was computed BY HAND after the run;
# nothing in the harness produced an under-warmed fraction, so PROGRESS.md's
# pre-registered P8 stop rule had nothing to read.
# --------------------------------------------------------------------------- #
def _census_probe(slot: str, *, rate: int, block: int,
                  gates_failed=(), attack_failed=False) -> ProbeRecord:
    p = _done_probe(rate, block, slot)
    p.gates_evaluated = ["p99_flat", "gc_steady", "pools_warm"]
    p.gates_failed = list(gates_failed)
    p.warmup_attack_failed = attack_failed
    return p


def _census_cell(**stages) -> CellRecord:
    cell = CellRecord(cell_id="headline.mixed.gc-default")
    for name, probes in stages.items():
        getattr(cell, name).probes.extend(probes)
    return cell


def test_the_warmup_census_covers_all_three_stages():
    """The census covers ladder, confirm and soak: a fraction over a subset of
    the stages is not what the P8 rule is written about, which is the whole
    cell."""
    cell = _census_cell(
        ladder=[_census_probe(SUBJECT, rate=1000, block=0, gates_failed=["p99_flat"]),
                _census_probe(SUBJECT, rate=1000, block=1)],
        confirm=[_census_probe(SUBJECT, rate=1500, block=0, gates_failed=["gc_steady"])],
        soak=[_census_probe(SUBJECT, rate=1500, block=0)],
    )
    (c,) = analyze.warmup_census_for_cell(cell, [SUBJECT])
    assert c.stages == ("ladder", "confirm", "soak")
    assert c.n_probes == 4 and c.n_flagged == 2
    assert c.flagged_fraction == pytest.approx(0.5)
    assert c.by_stage == {
        "ladder": {"n_probes": 2, "n_flagged": 1},
        "confirm": {"n_probes": 1, "n_flagged": 1},
        "soak": {"n_probes": 1, "n_flagged": 0},
    }
    assert c.by_gate == {"gc_steady": 1, "p99_flat": 1}


def test_the_warmup_census_is_per_slot_so_an_asymmetry_is_visible():
    """Round 1's finding was Go 67.3% vs .NET 51.8%: a single pooled fraction
    would have hidden the half of it that mattered."""
    cell = _census_cell(ladder=[
        _census_probe(SUBJECT, rate=1000, block=0, gates_failed=["p99_flat"]),
        _census_probe(SUBJECT, rate=1000, block=1, gates_failed=["p99_flat"]),
        _census_probe(REFERENCE, rate=1000, block=0),
        _census_probe(REFERENCE, rate=1000, block=1),
    ])
    by_slot = {c.slot: c for c in analyze.warmup_census_for_cell(cell, SLOTS)}
    assert by_slot[SUBJECT].flagged_fraction == pytest.approx(1.0)
    assert by_slot[REFERENCE].flagged_fraction == pytest.approx(0.0)


def test_the_census_reads_the_pre_registered_p8_abort_rule():
    """PROGRESS.md P8: "under-warmed fraction, either language, > 25% -> stop"."""
    assert analyze.WARMUP_ABORT_FLAGGED_FRACTION == 0.25
    over = _census_cell(ladder=[
        _census_probe(SUBJECT, rate=1000, block=b,
                      gates_failed=["p99_flat"] if b == 0 else [])
        for b in range(3)
    ])
    (c,) = analyze.warmup_census_for_cell(over, [SUBJECT])
    assert c.flagged_fraction == pytest.approx(1 / 3)
    assert c.exceeds_abort_threshold is True

    under = _census_cell(ladder=[
        _census_probe(SUBJECT, rate=1000, block=b,
                      gates_failed=["p99_flat"] if b == 0 else [])
        for b in range(4)
    ])
    (d,) = analyze.warmup_census_for_cell(under, [SUBJECT])
    assert d.flagged_fraction == pytest.approx(0.25)
    assert d.exceeds_abort_threshold is False        # the rule is STRICTLY greater


def test_a_cell_with_no_scored_probe_reports_no_fraction_rather_than_zero():
    """"0% under-warmed" and "nothing was scored" are different findings, and an
    abort rule that read the second as the first would wave a dead cell through."""
    pending = _done_probe(1000, 0, SUBJECT)
    pending.state = State.PENDING
    (c,) = analyze.warmup_census_for_cell(
        _census_cell(ladder=[pending]), [SUBJECT])
    assert c.n_probes == 0 and c.flagged_fraction is None
    assert c.exceeds_abort_threshold is False


def test_by_reason_attributes_one_cause_per_probe_and_a_crash_wins():
    """A crashed warmup leaves the series its gates would have been judged on
    unwritten, so naming one of those gates as the cause blames the SUT for a
    harness failure. `by_gate` still counts them; `by_reason` does not."""
    cell = _census_cell(ladder=[
        _census_probe(SUBJECT, rate=1000, block=0,
                      gates_failed=["p99_flat", "gc_steady"], attack_failed=True),
        _census_probe(SUBJECT, rate=1000, block=1, gates_failed=["p99_flat"]),
    ])
    (c,) = analyze.warmup_census_for_cell(cell, [SUBJECT])
    assert c.by_reason == {"gates_failed": 1, "warmup_attack_failed": 1}
    assert sum(c.by_reason.values()) == c.n_flagged
    assert c.by_gate == {"gc_steady": 1, "p99_flat": 2}   # sums to MORE, by design


def test_the_census_reaches_fits_json_and_the_report(isolated_results):
    run_id = "syn-census"
    led = _synthetic_ledger(run_id)
    # The census reads the PROBE RECORD, which is where the cause lives; the
    # stage's `under_warmed` map is the same verdict as a bare bool
    # (`phases.ProbeOutcome.under_warmed`), so setting the records is setting
    # both. Every SUBJECT probe of the ladder fails one gate.
    for probe in led.cells[0].ladder.probes:
        if probe.slot == SUBJECT:
            probe.gates_failed = ["p99_flat"]
    led.save()
    res = analyze.analyze_run(run_id)
    fits = json.loads(res.fits_path.read_text(encoding="utf-8"))
    census = {c["slot"]: c for c in fits["cells"][0]["warmup_census"]}
    assert set(census) == set(SLOTS)
    assert census[SUBJECT]["flagged_fraction"] > 0.0
    assert census[SUBJECT]["exceeds_abort_threshold"] is True
    assert set(census[SUBJECT]) >= {
        "n_probes", "n_flagged", "flagged_fraction", "by_gate", "by_reason",
    }
    md = res.report_path.read_text(encoding="utf-8")
    assert "## Warmup census (under-warmed fraction per cell per slot)" in md
    assert "above the pre-registered 25% stop rule" in md


# --------------------------------------------------------------------------- #
# the stage-mix disclosure states a CAUSE it can see and a CONCLUSION it computed
# --------------------------------------------------------------------------- #
def _slot_res(*, n_windows: int, excluded: int, stage_windows: dict) -> analyze.SlotResource:
    return analyze.SlotResource(
        n_windows=n_windows, n_flagged_excluded=excluded, posture_only=False,
        stage_windows=stage_windows,
    )


def _mixed_resource(*, excluded: dict, ratio: float, ratio_unweighted: float):
    return analyze.ResourceCost(
        available=True,
        by_slot={
            SUBJECT: _slot_res(n_windows=11, excluded=excluded[SUBJECT],
                               stage_windows={"confirm": 10, "soak": 1}),
            REFERENCE: _slot_res(n_windows=10, excluded=excluded[REFERENCE],
                                 stage_windows={"confirm": 10}),
        },
        cpu_ratio=ratio, cpu_ratio_unweighted=ratio_unweighted,
    )


def test_the_stage_mix_cause_is_read_off_the_counts_the_table_prints():
    """The clause said "the missing windows were flagged under-warmed" ALWAYS.

    On a two-slot run the table two lines below prints `11 / 0` and `21 / 0`
    flagged-excluded, so the report contradicted itself inside three lines. The
    cause has to come from the same counts the table does.
    """
    zero = _mixed_resource(excluded={SUBJECT: 0, REFERENCE: 0}, ratio=1.5,
                           ratio_unweighted=1.5)
    text = analyze._stage_mix_cause(zero)
    assert "NO window of either slot was flagged" in text
    assert "under-warmed / gate-excluded and dropped" not in text

    some = _mixed_resource(excluded={SUBJECT: 0, REFERENCE: 3}, ratio=1.5,
                           ratio_unweighted=1.5)
    text = analyze._stage_mix_cause(some)
    assert "flagged under-warmed / gate-excluded and dropped" in text
    assert f"{REFERENCE_LABEL} 3" in text and f"{SUBJECT_LABEL} 0" in text


def test_the_weighting_conclusion_is_derived_from_the_two_ratios_and_the_margin():
    """The sentence used to end "does not carry this cell's headline" ALWAYS.

    Measured on a two-slot run: 1.430x equal-weight against 1.556x published is
    8.8% apart -- 1.76x the +-5% margin the verdict is decided against -- and the
    sentence still said the choice did not carry the headline. Both numbers are
    on the same line as the conclusion, so the conclusion must come from them.
    """
    carries = analyze._weighting_sentence(
        _mixed_resource(excluded={SUBJECT: 0, REFERENCE: 0},
                        ratio=1.5557, ratio_unweighted=1.4299),
        {5.0},
    )
    assert "8.8% difference" in carries
    assert "1.76x the +-5% equivalence margin" in carries
    assert "DOES carry this cell's headline" in carries

    inside = analyze._weighting_sentence(
        _mixed_resource(excluded={SUBJECT: 0, REFERENCE: 0},
                        ratio=1.510, ratio_unweighted=1.500),
        {5.0},
    )
    assert "0.7% difference" in inside
    assert "does not carry this cell's headline" in inside
    assert "DOES carry" not in inside

    # No single margin to judge against => the conclusion is NOT asserted.
    unknown = analyze._weighting_sentence(
        _mixed_resource(excluded={SUBJECT: 0, REFERENCE: 0},
                        ratio=1.5557, ratio_unweighted=1.4299),
        set(),
    )
    assert "DOES carry" not in unknown and "does not carry" not in unknown
    assert "no single equivalence margin to judge that against" in unknown
    assert "left to the reader with both numbers in hand" in unknown


def test_a_ledger_with_no_declared_confirm_rate_says_the_bound_chose_the_windows(
    isolated_results,
):
    """The fallback is allowed; claiming it was a SELECTION is not.

    A ledger written before `confirm_rates_by_slot` existed has no declared
    operating point, so the roll-up falls back to the old `rate <= that slot's
    knee` bound. That is fine -- such a run has one confirm rate per slot anyway
    -- but `operating_point_basis` has to say so.

    The basis alone is not enough, so `scope` is asserted beside it. The bound
    admits any rate AT OR BELOW the knee, and this fixture's windows sit at
    1,000 rps against a 1,500 rps knee, a rate at which no SLO crossing was
    ever measured. `operating_point_basis` says how the rate was chosen;
    `scope` says what it MEANS, and both must be derived.
    """
    run_id = "syn-nodecl"
    confirm = _confirm_stage_at(1000, 2)
    del confirm.data["confirm_rates_by_slot"]      # the pre-W24 shape
    del confirm.data["common_rate_rps"]
    _ledger_with_confirm(run_id, confirm)          # ladder knee is 1500 rps
    base = (isolated_results / run_id / "cells" / "headline.mixed.gc-default"
            / "confirm" / "rate1000")
    for block in range(2):
        _write_probe_window(base / f"block{block}", REFERENCE, reqs=1000,
                            cpu_us=100_000, anon=30_000_000, peak=60_000_000)
        _write_probe_window(base / f"block{block}", SUBJECT, reqs=1000,
                            cpu_us=300_000, anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res.available is True and res.operating_rates == [1000]
    assert res.scope == analyze.SCOPE_DECLARED_POINT
    assert res.operating_point_basis == "knee_bound"
    assert set(res.scope_degraded_slots) == set(SLOTS)
    assert res.scope_degraded_slots[REFERENCE] == (
        "windows at 1,000 rps, which is not this slot's fitted knee (1,500 rps)"
    )
    md = result.report_path.read_text(encoding="utf-8")
    assert "otherwise bounded by that slot's fitted knee" in md
    assert "SCOPE: `own_knee`" not in md
    assert "SCOPE: `declared_operating_point`" in md
    assert "is not this slot's fitted knee (1,500 rps)" in md


def _no_crossing_ladder() -> StageRecord:
    """REFERENCE crosses 20 ms at 1500; SUBJECT never crosses inside the ladder.

    The shape the documented `LEDGERBENCH_ALLOW_SUB_KNEE_LADDER` opt-out records:
    `phases.run_fit` writes `knee_by_slot[SUBJECT] = None` and falls back to that
    slot's TOP RUNG as its confirm rate, so the confirm stage declares a ladder
    CEILING for it -- a lower bound on its knee, not a crossing rate.
    """
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    series: dict[str, list[float]] = {}
    for rate, (ref_v, sub_v) in ((1000, (10.0, 5.0)), (2000, (30.0, 8.0)),
                                 (3000, (50.0, 12.0))):
        for block in (0, 1):
            for slot, val in ((REFERENCE, ref_v), (SUBJECT, sub_v)):
                key = probe_key(rate, block, slot)
                rec.probes.append(_done_probe(rate, block, slot))
                p99[key] = val
                under_warmed[key] = False
                series[key] = [5.0, 5.0, 5.0, 5.0, 5.0]
    rec.data = _ladder_data(
        crossed_by_slot={REFERENCE: 2000, SUBJECT: None},
        bracket_by_slot={REFERENCE: [1000, 2000], SUBJECT: None},
        p99_ms=p99, under_warmed=under_warmed, p99_series=series,
        rates=[1000, 2000, 3000],
        degraded=True, degraded_allowed_by="LEDGERBENCH_ALLOW_SUB_KNEE_LADDER",
    )
    return rec


def test_a_slot_with_no_fitted_knee_never_publishes_an_own_knee_scope(
    isolated_results,
):
    """The scope label is derived per slot, never emitted unconditionally.

    With `scope=SCOPE_OWN_KNEE` emitted unconditionally, on the exact run the
    opt-out exists for (one slot at a ladder ceiling it never crossed) the
    report's scope line would name 3,000 rps as that slot's "OWN SLO-crossing
    rate", three sections after the no-crossing bullet tells the reader that
    same slot "has no operating point at the latency promise and no number below
    is cost at its crossing rate". `operating_point_basis` reads `declared`
    there, so not even the fallback caveat fires, and the same label would ride
    in fits.json and in the paired-verdict prose.

    So the scope is DERIVED from whether each slot actually has a fitted knee it
    was selected at, and the degraded case names the slot and the reason.
    """
    run_id = "syn-noknee-scope"
    _ledger_with_confirm(
        run_id, _confirm_at_rates({REFERENCE: 1500, SUBJECT: 3000}, 3),
        _no_crossing_ladder(),
    )
    _write_cost_confirm_windows(run_id, 0, blocks=3,
                                rates_by_slot={REFERENCE: 1500, SUBJECT: 3000})
    _write_cost_confirm_windows(run_id, 0, blocks=3, sub_us=150_000,
                                rates_by_slot={REFERENCE: 1500, SUBJECT: 1500})
    result = analyze.analyze_run(run_id)
    cell = result.cells[0]

    assert cell.knee_by_slot[SUBJECT].crossed is False
    res = cell.resource
    assert res.available is True
    # The declaration was honoured -- the windows really are at 3,000 rps -- so
    # the BASIS is `declared`. That is exactly why the basis could not carry
    # this: it says how the rate was chosen, not what the rate means.
    assert res.operating_point_basis == "declared"
    assert res.scope == analyze.SCOPE_DECLARED_POINT
    assert set(res.scope_degraded_slots) == {SUBJECT}
    assert "no SLO crossing in the ladder" in res.scope_degraded_slots[SUBJECT]
    assert "3,000 rps, its ladder ceiling" in res.scope_degraded_slots[SUBJECT]

    # The paired verdict carries the same label, from the same rule.
    scopes = {v.verdict_scope for v in cell.paired_verdicts}
    assert analyze.SCOPE_OWN_KNEE not in scopes
    assert analyze.SCOPE_DECLARED_POINT in scopes
    # ...and the cell still HAS a headline row: publishing it under its honest
    # scope is the fix, dropping it would be a different kind of dishonesty.
    assert cell.paired is not None
    assert cell.paired.verdict_scope == analyze.SCOPE_DECLARED_POINT

    md = result.report_path.read_text(encoding="utf-8")
    assert "SCOPE: `own_knee`" not in md
    assert "SCOPE: `declared_operating_point`" in md
    assert "no SLO crossing in the ladder" in md
    # the sentence it used to contradict, still there and no longer contradicted
    assert "has no measured knee" in md
    assert "`own_knee` is the headline the metric is defined as" not in md
    # the paired section's own by-construction claim is conditional too: only a
    # slot WITH a knee sits at the promise, and this run has one that does not
    assert "At their own knees both stacks sit at roughly 20 ms" not in md
    assert "a slot with NO knee sits below the promise at its ladder ceiling" in md

    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    fc = fits["cells"][0]
    assert fc["resource"]["scope"] == analyze.SCOPE_DECLARED_POINT
    assert SUBJECT in fc["resource"]["scope_degraded_slots"]
    assert all(v["verdict_scope"] != analyze.SCOPE_OWN_KNEE
               for v in fc["paired_verdicts"])


def test_the_knee_bound_fallback_cannot_call_two_rates_one_operating_point(
    isolated_results,
):
    """The same defect on the pre-W24 ledger path, where the number moves too.

    `operating_point_basis` was derived from the surviving rows; `scope` was not.
    So on the ledger the fallback exists for, a slot could keep windows at TWO
    rates -- the blend W24 removed -- under an `own_knee` label, while
    `operating_rate_by_slot` (which admits only slots with exactly one surviving
    rate) SILENTLY DROPPED that slot from the scope line. The one slot the
    sentence was false about was the one slot it did not name.
    """
    run_id = "syn-fallback-two-rates"
    confirm = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    for rate, slots in ((1000, SLOTS), (1400, (REFERENCE,))):
        for block in range(2):
            for slot in slots:
                key = probe_key(rate, block, slot)
                confirm.probes.append(_done_probe(rate, block, slot))
                p99[key] = 9.0
                under_warmed[key] = False
    # the pre-W24 shape: no declared operating point at all
    confirm.data = {"p99_ms": p99, "under_warmed": under_warmed}
    _ledger_with_confirm(run_id, confirm)          # ladder knee is 1500 rps
    base = paths_run_dir(run_id) / "cells" / "headline.mixed.gc-default" / "confirm"
    for block in range(2):
        for rate in (1000, 1400):
            _write_probe_window(base / f"rate{rate}" / f"block{block}", REFERENCE,
                                reqs=1000, cpu_us=100_000, anon=30_000_000,
                                peak=60_000_000)
        _write_probe_window(base / "rate1000" / f"block{block}", SUBJECT,
                            reqs=1000, cpu_us=300_000, anon=150_000_000,
                            peak=180_000_000)

    res = analyze.analyze_run(run_id).cells[0].resource
    assert res.available is True
    assert res.operating_point_basis == "knee_bound"
    assert res.scope == analyze.SCOPE_DECLARED_POINT
    # the multi-rate slot is the one `operating_rate_by_slot` cannot express...
    assert REFERENCE not in res.operating_rate_by_slot
    # ...so the degraded reasons are what has to name it, and they do
    assert res.scope_degraded_slots[REFERENCE] == (
        "windows at 2 rates (1,000, 1,400 rps), not one operating point"
    )
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "SCOPE: `own_knee`" not in md
    assert "windows at 2 rates (1,000, 1,400 rps), not one operating point" in md


# --------------------------------------------------------------------------- #
# W26: the capacity RE-gate reaches the report
# --------------------------------------------------------------------------- #
def _regate_md(run_id, **data):
    led = _synthetic_ledger(run_id)
    led.mark_phase("capacity_gate", State.DONE, verdict="pass", pg_knee_rps=3715.05)
    led.mark_phase(REGATE_PHASE, State.DONE, **data)
    return analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")


def test_a_passing_regate_is_reported_as_a_measurement_not_a_projection(isolated_results):
    md = _regate_md(
        "syn-regate-pass",
        verdict="pass-at-realized-rates",
        pg_utilization=0.512, pg_utilization_ceiling=0.7692307692, pg_cores=3,
        realized_rates_by_slot={SUBJECT: 1429.0, REFERENCE: 5714.0},
        cells_measured_before_regate=["headline.mixed.gc-default"],
        detail="within the ceiling",
    )
    assert "re-run at the REALIZED rates, and passed" in md
    assert "0.512 of its 3-core budget against a 0.769 ceiling" in md
    assert "5,714 rps" in md and "1,429 rps" in md
    assert "headline.mixed.gc-default" in md


def test_a_failing_regate_reports_which_cells_it_lands_on_retroactively(isolated_results):
    md = _regate_md(
        "syn-regate-fail",
        verdict="db-imposed-at-realized-rates",
        detail="PostgreSQL ran at 0.9000 of its 3-core budget",
        pg_utilization=0.90, pg_utilization_ceiling=0.7692307692, pg_cores=3,
        realized_rates_by_slot={SUBJECT: 1429.0, REFERENCE: 5714.0},
        cells_measured_before_regate=["headline.mixed.gc-default"],
    )
    assert "FAILED when re-run at the realized rates" in md
    assert "PostgreSQL ran at 0.9000 of its 3-core budget" in md
    assert "retroactive" in md and "headline.mixed.gc-default" in md
    assert "headroom AT THEIR OWN RATES is not established" in md


def test_an_unevaluable_regate_says_the_question_is_open_not_answered(isolated_results):
    md = _regate_md(
        "syn-regate-open",
        verdict="regate-not-evaluable",
        detail="the cell fitted no knee for any slot",
        pg_utilization=None, pg_utilization_ceiling=0.7692307692,
        realized_rates_by_slot={}, cells_measured_before_regate=[],
    )
    assert "could not be re-run at the realized rates" in md
    assert "the cell fitted no knee for any slot" in md
    assert "is OPEN in this run, not answered" in md
    # never a passing-shaped sentence, and never an invented number
    assert "passed" not in md.split("could not be re-run")[1].split("\n")[0]


def test_a_run_with_no_regate_record_gets_no_regate_bullet(isolated_results):
    led = _synthetic_ledger("syn-regate-absent")
    led.mark_phase("capacity_gate", State.DONE, verdict="pass", pg_knee_rps=3715.05)
    md = analyze.analyze_run("syn-regate-absent").report_path.read_text(encoding="utf-8")
    assert "realized rates" not in md
