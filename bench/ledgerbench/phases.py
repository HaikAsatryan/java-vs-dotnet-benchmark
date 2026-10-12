"""Phase orchestration + RCB interleaving.

This is the live, ledger-backed, resumable pipeline. Phase order:

    doctor -> host_setup -> host_readback -> seed(+invariants) -> capacity_gate
      -> validation -> cell 1 (ladder -> fit -> confirm -> soak)
      -> capacity_regate -> cells 2..N -> variants -> host_restore

`capacity_regate` sits INSIDE the cells phase, between cell 1 and cell 2: it
re-asks the capacity gate's headroom question against the rates cell 1 turned
out to run at, which do not exist until a cell has been fitted and confirmed
(R1-A6). It is a phase of its own so it is skip-if-done on resume and visible
to `status`.

Every phase consults the ledger first and SKIPS work whose state == done, with
the hardwired power-loss exceptions:
  (a) doctor ALWAYS re-runs on process start;
  (b) host_setup ALWAYS re-runs on Linux on process start (a reboot silently
      resets governor/boost/CPUAffinity; the script is idempotent): on Windows
      it is recorded done with data {"skipped": "non-linux"}, and host_readback
      follows it under the same rule;
  (c) a `done` seed phase, on RESUME, still re-runs the seeder with --verify-only
      (invariant preflight proves PG came back crash-consistent) before any probe.

RCB interleaving: probes are grouped in blocks holding one probe per slot of
the run's slot set (registry.SlotSet) at the same rate; the within-block order
comes from a seeded RNG (ledger.rng_seed) and is recorded as `slot_order`. On resume, any block with exactly one member
done is reset whole (Ledger.reset_orphan_blocks) so the paired slot is re-run
clean.

The live execution surface (docker compose up/down, vegeta attack, cgroup
sampling) is isolated behind `ProbeRunner` so the orchestration logic is unit
testable with a fake runner and no daemon. `LiveProbeRunner` is the real one.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Protocol

from . import capacity_gate as capgate
from . import checksums, compose, doctor, hostread, registry
from .analysis_bridge import estimate_knee_from_rungs, knee_crosses, stationarity_fails
from .config import SloConfig, WarmupCfg, Workload, config_hash, load_slo, load_workload
from .ledger import (
    Ledger,
    ProbeRecord,
    State,
    drop_probe_data,
    probe_key,
    split_probe_key,
)
from .manifest import NOT_MEASURED, RunManifest, git_identity, new_manifest
from .util import paths
from .util.clock import rfc3339_micros
from .util.cpuset import parse_cpuset
from .util.pcg64 import PCG64
from .util.proc import run
from .warmup import IMPLEMENTED_GATES, gate_set_for_cell

IS_LINUX = sys.platform.startswith("linux")

# The SUT core count pinned in infra/compose.bench.yaml (each language's
# parallelism carrier -- registry.LanguageSpec.parallelism_var -- is set from
# ${SUT_CORES:-6}). Kept consistent with that default so the manifest knobs and
# the Linux processor-count assertion agree with compose.
SUT_CORES = int(os.environ.get("SUT_CORES", "6"))


# --------------------------------------------------------------------------- #
# scale profiles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScaleProfile:
    """Resolved measurement numerics for a scale.

    `full` reads spec/slo.yaml verbatim. `smoke` is a clearly-labeled
    FUNCTIONAL-REHEARSAL-ONLY override block: tiny windows, few rungs, gc-default
    cell only, so a complete run-all rehearses every code path in ~20 min on the
    dev laptop without producing measurement-grade numbers.

    `rehearsal` is the third tier and the only one that validates NUMBERS before
    days of runtime are committed. It keeps every full-scale evidence dial --
    warmup cap, discard, measured window, min-calls, dataset, working set -- and
    reduces only the dials that buy wall clock: rungs, reps, confirm blocks,
    soak, cells. A dial that differs from `full` is a dial rehearsal does not
    validate, so each deviation below is justified where it is made.
    """

    scale: str
    functional_only: bool
    warmup_cap_s: int
    # Head-of-window discard (slo ladder.warmup_discard_seconds): traffic at the
    # MEASURED rate, run before the measured attack and excluded from the probe
    # report + cgroup window (guards against residual JIT re-tiering at the
    # measured rate; METHODOLOGY "Warmup").
    warmup_discard_s: int
    measure_s: int
    ladder_start_rps: int
    ladder_step_factor: float
    ladder_max_rungs: int
    ladder_reps_per_rung: int
    confirm_blocks: int
    soak_s: int
    capacity_gate_rungs: int
    capacity_gate_window_s: int
    cells_filter: set[str] | None      # None => all non-linux-only cells
    # attack shaping (kept modest; smoke is functional)
    max_workers: int = 64
    connections: int = 16
    timeout_s: float = 5.0
    # Requests the BODY POOL is sized for (the create/quote split comes from
    # the mix). A FLOOR on the working set, never the count of any one draw: a
    # probe draws `vegeta.probe_draw_count(rate, duration)` targets, which at
    # the top of a full ladder is larger than the whole pool.
    body_pool_requests: int = 6000
    # Scale on spec/slo.yaml warmup.per_endpoint_min_calls. The spec minimums
    # are full-scale evidence thresholds; the smoke profile (15 s cap) scales
    # them down so the warmup gates are exercised for real instead of being
    # unreachable-by-construction (which would flag EVERY smoke probe
    # under-warmed and empty every rung).
    warmup_min_calls_factor: float = 1.0
    # The DATASET dial, which is NOT the measurement tier. `scale` names the
    # tier; `seed_scale` names the keyspace the seeder writes and the target
    # generator draws ids from. Only the latter reaches spec/workload.yaml
    # `seeding.scales` and seed/internal/gen/scale.go, and both know exactly
    # full|smoke -- so a tier whose name is not a dataset name (rehearsal) must
    # carry the dataset separately or it KeyErrors in the target generator and
    # is rejected by the Go seeder. Defaults to `scale` for the two tiers whose
    # names are also dataset names.
    seed_scale: str = ""
    # The create-only GATE ladder's geometric step. Defaults to the measurement
    # ladder's, and `rehearsal` deliberately does not inherit it: the two
    # ladders look for different quantities. The measurement ladder must REACH a
    # mixed-workload knee near 10k rps in six rungs; the gate ladder must
    # accumulate `capacity_gate.MIN_PG_KNEE_RUNGS` usable rungs BELOW
    # PostgreSQL's create ceiling, which is a write ceiling in the low
    # thousands. Sharing one dial made a coarse measurement step silently
    # coarsen the gate: from 500 rps at 2.0, a PG ceiling under ~2500 rps yields
    # fewer than three usable rungs, the gate reports NOT MEASURED and `run-all`
    # ABORTS before a single probe -- while the same box passes `full`'s gate at
    # 1.2. A rehearsal that fails where the full run succeeds is the inversion
    # of what the tier is for.
    capacity_gate_step_factor: float | None = None

    def __post_init__(self) -> None:
        if not self.seed_scale:
            object.__setattr__(self, "seed_scale", self.scale)
        if self.capacity_gate_step_factor is None:
            object.__setattr__(self, "capacity_gate_step_factor", self.ladder_step_factor)

    @classmethod
    def resolve(cls, scale: str, slo: SloConfig) -> "ScaleProfile":
        try:
            build = SCALES[scale]
        except KeyError:
            raise ValueError(
                f"unknown scale {scale!r} (expected {'|'.join(SCALES)})"
            ) from None
        return build(slo)

    @staticmethod
    def _measured_body_pool_requests() -> int:
        """Requests the body pool is sized for, on every MEASURED tier.

        Bench-side working-set dial. The default is 1_000_000, which at the
        spec mix is 150,000 create bodies and 200,000 quote bodies.

        Bench dial, not spec/: no config_hash change. The value is pinned in the
        ledger on the first run and replayed on resume, so a mid-run env change
        cannot swap the pool the draws reference.

        The predecessor env var is REFUSED rather than ignored. It named the
        size of the single targets file a run replayed; under a per-probe draw
        there is no such file, and an operator whose shell still exports it
        would otherwise get the default silently while believing they had set
        the working set.
        """
        stale = os.environ.get(_LEGACY_TARGET_COUNT_ENV, "").strip()
        if stale:
            raise ValueError(
                f"{_LEGACY_TARGET_COUNT_ENV} is no longer read: it sized the ONE "
                f"targets file a run replayed, and W20 draws targets per probe. "
                f"Set {BODY_POOL_ENV} instead (it sizes the shared request-body "
                f"pool; the per-probe draw is sized from rate x duration)."
            )
        raw = os.environ.get(BODY_POOL_ENV, "").strip()
        try:
            requests = int(raw) if raw else 1_000_000
        except ValueError:
            raise ValueError(f"{BODY_POOL_ENV}={raw!r} is not an integer") from None
        if requests < 1000:
            raise ValueError(f"{BODY_POOL_ENV}={requests} is implausibly small (<1000)")
        return requests

    @classmethod
    def full(cls, slo: SloConfig) -> "ScaleProfile":
        lad = slo.ladder
        soak = slo.soak or {}
        body_pool_requests = cls._measured_body_pool_requests()
        return cls(
            scale="full",
            functional_only=False,
            warmup_cap_s=slo.warmup.hard_cap_seconds,          # 300
            warmup_discard_s=lad.warmup_discard_seconds,       # 30
            measure_s=lad.window_seconds,                      # 120
            ladder_start_rps=lad.start_rate_rps_default,       # 500
            ladder_step_factor=lad.step_factor,                # 1.2
            ladder_max_rungs=lad.rung_ceiling,                 # 40
            ladder_reps_per_rung=lad.reps_per_rung,            # 3
            confirm_blocks=lad.confirm_reps,                   # 10
            soak_s=int(soak.get("duration_seconds", 600)),
            capacity_gate_rungs=12,
            capacity_gate_window_s=lad.window_seconds,
            cells_filter=None,
            max_workers=512,
            connections=64,
            timeout_s=lad.attack_timeout_seconds,
            body_pool_requests=body_pool_requests,
        )

    @classmethod
    def smoke(cls, _slo: SloConfig) -> "ScaleProfile":
        # FUNCTIONAL-REHEARSAL-ONLY. Numbers chosen so a full
        # run-all completes in ~20 min on this laptop; NEVER measurement-grade.
        return cls(
            scale="smoke",
            functional_only=True,
            warmup_cap_s=15,
            warmup_discard_s=2,
            measure_s=10,
            ladder_start_rps=100,
            ladder_step_factor=2.0,
            ladder_max_rungs=3,
            ladder_reps_per_rung=2,
            confirm_blocks=2,
            soak_s=20,
            capacity_gate_rungs=2,
            capacity_gate_window_s=10,
            cells_filter={"headline.mixed.gc-default"},
            max_workers=64,
            connections=16,
            timeout_s=5.0,
            body_pool_requests=6000,
            warmup_min_calls_factor=0.02,
        )

    @classmethod
    def rehearsal(cls, slo: SloConfig) -> "ScaleProfile":
        # MEASUREMENT-GRADE, reduced. Every deviation from `full` is listed
        # here with the reason it is safe; anything not listed is full's value.
        lad = slo.ladder
        return cls(
            scale="rehearsal",
            # The dataset is FULL. Seeding at smoke's 1k/5k rows makes the whole
            # read path cache-resident, which answers G6's questions by
            # construction instead of by measurement.
            seed_scale="full",
            # A True here would gate out the CO/error exclusions and both
            # gate-enforcement paths, making this a slow smoke run.
            functional_only=False,
            warmup_cap_s=slo.warmup.hard_cap_seconds,
            warmup_discard_s=lad.warmup_discard_seconds,
            measure_s=lad.window_seconds,
            ladder_start_rps=lad.start_rate_rps_default,
            # DELIBERATE deviation from spec's 1.2. Six geometric rungs at 1.2
            # reach 1,244 rps, so with a knee anywhere near 10k EVERY rehearsal
            # probe is sub-knee and "do both stacks cross?" is answered `no` by
            # construction -- R1-A3's failure mode transplanted onto the ladder.
            # At 2.0 the six rungs span 500..16,000 and can bracket a real knee.
            # Coarse: rehearsal brackets, `full` at 1.2 resolves.
            ladder_step_factor=2.0,
            # The GATE ladder keeps the spec's fine step -- see the field
            # comment. Coarsening it does not save time (the gate ladder breaks
            # at the ceiling rather than running to its rung cap), it only
            # removes the rungs the knee needs.
            capacity_gate_step_factor=lad.step_factor,
            # Deviates from spec's `ladder.rung_ceiling` (40) for the same reason
            # the step factor does: six rungs at 2.0 span 500..16,000 rps, which
            # brackets a knee. Forty rungs at 2.0 would serve no purpose, and this tier
            # exists to bracket rather than resolve.
            ladder_max_rungs=6,
            # Wall-clock dials, and the only ones. 2 reps still pairs every rung
            # inside an RCB block; 2 confirm blocks still exercise the confirm
            # path without buying a CI anyone would publish.
            ladder_reps_per_rung=2,
            confirm_blocks=2,
            soak_s=300,
            # Raised above full's 12 so the ladder can BREAK rather than run out
            # of rungs: at the spec step, 12 rungs from 500 rps reach only 3,716
            # and a ceiling above that is recorded as a LOWER BOUND, never a
            # measured knee. 16 rungs reach 7,705. Rungs cost nothing unspent --
            # the ladder stops at the first rung that fails to track.
            capacity_gate_rungs=16,
            capacity_gate_window_s=lad.window_seconds,
            cells_filter={"headline.mixed.gc-default"},
            max_workers=512,
            connections=64,
            timeout_s=lad.attack_timeout_seconds,
            # NOT reduced. This is the
            # working-set dial: cutting it re-introduces exactly the cache
            # residency `seed_scale="full"` above exists to prevent, since it
            # decides how many distinct customers the create path can name.
            # Under W20 it is also cheaper than it was -- the pool is built and
            # shipped ONCE per run rather than once per (slot, cell). See
            # PROGRESS W20 for the measured generation cost.
            body_pool_requests=cls._measured_body_pool_requests(),
            warmup_min_calls_factor=1.0,
        )


# Single source of truth for the supported tiers: `resolve`'s error message, the
# CLI's `--scale` validation and its help text all read this, so none of them can
# drift from the set that actually builds.
SCALES: dict[str, Callable[[SloConfig], ScaleProfile]] = {
    "full": ScaleProfile.full,
    "smoke": ScaleProfile.smoke,
    "rehearsal": ScaleProfile.rehearsal,
}


# --------------------------------------------------------------------------- #
# declarative CELLS table (cells + variants)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Cell:
    cell_id: str
    extra_env: dict[str, str] = field(default_factory=dict)
    linux_only: bool = False
    deferred: bool = False          # declared; runs only when named in RUN_VARIANTS_ENV
    note: str = ""
    # Per-cell warmup gate override, MERGED over spec/slo.yaml's global set and
    # its `gates_by_posture` entry (warmup.gate_set_for_cell). None == this cell
    # is held to exactly the spec's set.
    warmup_gates: dict[str, bool] | None = None
    # Why. REQUIRED whenever `warmup_gates` deviates, and enforced below rather
    # than left to review: R1-A3 is a cell that ran ~30 h against a gate it
    # could not satisfy, and the only thing that makes a waiver different from
    # that failure is a stated, auditable reason travelling with it. An empty
    # rationale would put the waiver back where round 1 had it -- in the
    # operator's head, discoverable only from the report afterwards.
    gate_set_rationale: str = ""

    def __post_init__(self) -> None:
        if self.warmup_gates and not self.gate_set_rationale.strip():
            raise ValueError(
                f"cell {self.cell_id!r} overrides warmup gates "
                f"{sorted(self.warmup_gates)} with no gate_set_rationale; a "
                "posture-specific gate set is a claim about what this posture "
                "can satisfy and must say what that claim rests on"
            )


# Headline cell (always runnable) + variants (declared; deferred until named).
#
# ONE headline cell since G6 (2026-08-27). The heap axis PROGRESS D6 left open
# -- the CLR sizing from ~75% of the cgroup limit while the JVM's
# -XX:MaxRAMPercentage default is 25 -- was resolved BY MEASUREMENT, exactly as
# D6 prescribed: at the real 12 GiB mem_limit the JVM's heap committed peaked
# at 298 MB across every measured window of the G6 rehearsal
# (rehearsal-20260827T111408-fedora), against a ~3 GiB default ceiling. The
# ceiling never binds, so the equalized cell (`headline.mixed.gc-generous`,
# JVM 75% + generational ZGC vs CLR DATAS-off) measured nothing the defaults
# cell does not, and D6's own rule applied: one cell, defaults, done, and the
# other is deleted rather than declared and skipped.
CELLS: list[Cell] = [
    Cell(
        cell_id="headline.mixed.gc-default",
        extra_env={},  # compose.yaml defaults (JVM 25% + G1, CLR 75% + DATAS)
        note="flag defaults on both sides; the headline cell.",
    ),
    # ---- variants (linux_only; executed via LEDGERBENCH_RUN_VARIANTS) ------ #
    # No data-layer variant cells (D5). The ado/dapper DATA LAYERS remain in
    # services/dotnet as code: the P2 shakedown's second slot runs `ado`
    # through registry slot_env, no cell involved. This one cell is the final
    # variant matrix.
    # D4 INVERTED THIS CELL. Round 1's headline ran Npgsql at its default 0 (never
    # prepared) and used =20 as the "parity" variant. Round 2 sets 20 as the
    # BASELINE, because PgJDBC auto-prepares at prepareThreshold=5 by default and
    # a Java-prepared vs .NET-unprepared headline measures the prepare lever, not
    # the runtimes (round 1: the .NET knee moved 1,468 -> 10,426 rps on this knob
    # alone). So the variant is now the other direction: Npgsql's DOCUMENTED
    # DEFAULT, kept as a declared axis so the lever stays quantified.
    Cell(
        cell_id="variant.npgsql-default-prepare",
        extra_env={"MAX_AUTO_PREPARE": "0"},
        linux_only=True,
        deferred=True,
        note="Npgsql's default Max Auto Prepare=0 (off); the baseline is 20 per D4.",
    ),
]

HEADLINE_CELL_IDS = {"headline.mixed.gc-default"}


def cells_for(profile: ScaleProfile) -> list[Cell]:
    """Headline cells the active scale should run (variants handled separately)."""
    out: list[Cell] = []
    for c in CELLS:
        if c.cell_id not in HEADLINE_CELL_IDS:
            continue
        if profile.cells_filter is not None and c.cell_id not in profile.cells_filter:
            continue
        out.append(c)
    return out


def variant_cells() -> list[Cell]:
    return [c for c in CELLS if c.cell_id not in HEADLINE_CELL_IDS]


def gate_set_conflicts(
    cell: Cell, slots: Sequence[str], warmup: WarmupCfg
) -> list[str]:
    """Enforced gates this cell's own declared knobs make unsatisfiable, per slot.

    Pure, so the refusal below is testable with no ledger and no runner.
    """
    enforced = gate_set_for_cell(
        warmup, cell_id=cell.cell_id, cell_gates=cell.warmup_gates
    ).enforced
    out: list[str] = []
    for slot_id in slots:
        for gate, u in registry.unsatisfiable_gates_for(slot_id, cell.extra_env).items():
            if enforced.get(gate, False):
                out.append(
                    f"{cell.cell_id} / {slot_id}: warmup gate {gate!r} is ENFORCED, but "
                    f"this cell declares {u.env_var}={u.value} and {u.why}"
                )
    return out


def assert_gate_sets_satisfiable(ctx: RunContext, cells: Sequence[Cell]) -> None:
    """Refuse, BEFORE any probe, a cell that enforces a gate it cannot satisfy.

    The mechanism behind R1-A3 and PROGRESS's P8 abort criterion ("any cell
    whose gate set is unsatisfiable by construction -> stop, do not spend 15 h
    on posture evidence").

    Called at every door that EXECUTES probes -- `run_cell`, `run_cells_phase`,
    `run_ladder`, `run_confirm`, `run_soak` -- because `ledgerbench
    ladder|confirm|soak --cell X` enter the last three directly, without ever
    passing through `run_cell`. That is the same reason the capacity gate is
    re-asserted at each of them. `run_fit` is deliberately not a door here: it
    runs no probe, so there is no runtime for it to refuse to spend.
    """
    conflicts = [c for cell in cells for c in gate_set_conflicts(cell, ctx.slots, ctx.slo.warmup)]
    if conflicts:
        raise PhaseError(
            "cell gate set(s) unsatisfiable by construction: "
            + "; ".join(conflicts)
            + ". Declare the gate not applicable for that cell (Cell.warmup_gates "
              "+ gate_set_rationale) or do not run the cell."
        )


def posture_waiver_conflicts(warmup: WarmupCfg) -> list[str]:
    """`gates_by_posture` entries that name a cell this build does not have.

    The gate names are checked by `WarmupCfg` itself (it holds the declared
    set); the cell ids can only be checked here, where `CELLS` lives. Without
    this a misspelt cell id in `spec/slo.yaml` is a SILENT no-op: the waiver
    binds to nothing, the gate stays enforced, and the run spends its hours
    flagging a posture the operator believed was exempted -- R1-A3 again, behind
    a better-looking declaration. Pure, so the refusal is testable without a
    ledger.
    """
    known = {c.cell_id for c in CELLS}
    return [
        f"spec/slo.yaml warmup.gates_by_posture waives "
        f"{sorted(w.gates)} for cell {cell_id!r}, which is not in phases.CELLS "
        f"({sorted(known)})"
        for cell_id, w in warmup.gates_by_posture.items()
        if cell_id not in known
    ]


def assert_gate_plan_sound(ctx: RunContext, cells: Sequence[Cell]) -> None:
    """The whole plan-time gate refusal, at one call: waivers first, then conflicts.

    Waivers first because an unbound waiver CHANGES the second answer: a typo'd
    posture id leaves the gate enforced, so the satisfiability check would run
    against a gate set the operator does not think is in force, and its verdict
    -- pass or fail -- would be about the wrong declaration either way.
    """
    unbound = posture_waiver_conflicts(ctx.slo.warmup)
    if unbound:
        raise PhaseError(
            "posture gate waiver(s) bind to nothing: "
            + "; ".join(unbound)
            + ". A waiver that names no cell leaves the gate ENFORCED while "
              "reading as an exemption; fix the cell id or delete the entry."
        )
    assert_gate_sets_satisfiable(ctx, cells)


# --------------------------------------------------------------------------- #
# RCB block planning
# --------------------------------------------------------------------------- #
def slot_order_for_block(rng: PCG64, slots: Sequence) -> list:
    """Seeded within-block member order (RCB), by Fisher-Yates over the members.

    Generalised from the round-1 two-way coin flip so a slot set of any size is
    randomised uniformly. At n=2 it consumes exactly one draw, the same budget
    the coin flip had, so a stage's order stream stays aligned with its salt.

    It shuffles whatever it is handed. `plan_blocks` hands it member INDICES
    rather than slot ids, because a W24 confirm block can hold one slot twice
    (own knee + common rate) and a shuffle over ids could not tell those two
    probes apart.
    """
    order = list(slots)
    for i in range(len(order) - 1, 0, -1):
        j = rng.randint(0, i)
        order[i], order[j] = order[j], order[i]
    return order


def plan_blocks(
    *,
    n_blocks: int,
    rng_seed: int,
    salt: int,
    slots: Sequence[str] | None = None,
    rate: int | None = None,
    members: Sequence[tuple[str, int]] | None = None,
    block_offset: int = 0,
) -> list[ProbeRecord]:
    """Build the probe records for `n_blocks` RCB blocks.

    Two ways to say what a block holds. `slots=` + `rate=` is the classic form:
    one probe per slot, all at one rate. `members=[(slot, rate), ...]` is the
    W24 form, and it is what lets a block hold each slot at ITS OWN confirm rate
    -- and lets one slot appear twice, at its own knee and at the common rate,
    which is how the common-rate secondary verdict keeps its RCB adjacency
    instead of being appended as a separate block set an hour later.

    The within-block order is seeded by (rng_seed, salt) so it is reproducible
    and auditable in the ledger. `salt` distinguishes stages/rates so the
    gc-default confirm and a soak at the same rate don't share an order stream.

    `block_offset` makes block ids UNIQUE WITHIN A STAGE. That is not cosmetic:
    both `_execute_block_probes` and `ledger.reset_orphan_blocks` group probes by
    block id alone (they must, or a mixed-rate block splits into singleton groups
    and a crash that halves it is never repaired on resume), so two plan_blocks
    calls into one stage that both started at 0 would merge.
    """
    if members is None:
        if slots is None or rate is None:
            raise ValueError("plan_blocks needs either members= or slots= + rate=")
        members = [(slot, int(rate)) for slot in slots]
    members = list(members)
    rng = PCG64(rng_seed ^ (salt * 0x9E3779B97F4A7C15 & ((1 << 64) - 1)))
    probes: list[ProbeRecord] = []
    for b in range(n_blocks):
        block = block_offset + b
        idx_order = slot_order_for_block(rng, list(range(len(members))))
        order = [members[i][0] for i in idx_order]
        for pos, i in enumerate(idx_order):
            slot, member_rate = members[i]
            probes.append(
                ProbeRecord(
                    block=block, slot_order=list(order), slot=slot,
                    rate=int(member_rate), order_index=pos,
                )
            )
    return probes


# --------------------------------------------------------------------------- #
# probe execution surface (isolated so orchestration is testable)
# --------------------------------------------------------------------------- #
@dataclass
class ProbeOutcome:
    achieved: float
    error_rate: float
    p99_ms: float
    artifact: str                 # relative path under results/<run_id>/
    per_second_p99: list[float] = field(default_factory=list)
    # The warmup gate verdict, as the LIST it was decided from, so `ledger.json`
    # (the file `analyze` trusts) names the gate that flagged a probe (R1-B4).
    gates_evaluated: list[str] = field(default_factory=list)
    gates_failed: list[str] = field(default_factory=list)
    # Not a gate: the warmup ATTACK itself failed, which leaves the series the
    # gates would have been judged on unwritten (so they can pass vacuously).
    # Kept separate rather than folded into gates_failed, which would name a
    # gate as the cause of something no gate decided -- and PERSISTED to the
    # ledger beside them, because a cause that lives only in memory leaves the
    # artifact recording `under_warmed: true` with `gates_failed: []` and no
    # recoverable reason.
    warmup_attack_failed: bool = False
    # The MEASURED window's PCG64 stream selector, hex (W20). Carried to the
    # ledger so the request sequence a probe faced is regenerable from
    # `ledger.json` alone, and so a resume can be shown to have redrawn the same
    # one rather than a fresh sequence under the same probe key.
    draw_inc: str | None = None
    # Seconds this probe spent generating targets, summed over every attack it
    # ran (warmup segments + discard + measured). Recorded because "B8 silently
    # becomes the longest phase of the run" is a named risk of drawing per probe,
    # and a risk with no measurement is an assumption.
    draw_seconds: float = 0.0
    ok: bool = True
    detail: str = ""

    @property
    def under_warmed(self) -> bool:
        """DERIVED: the bool and its evidence cannot disagree.

        Mirrors `warmup.WarmupResult.under_warmed`, over the same two causes.
        It is a property, not a field, so no caller can set a verdict its own
        evidence contradicts -- and BOTH causes are written to the ledger
        (`ProbeRecord.gates_failed`, `ProbeRecord.warmup_attack_failed`), so the
        same holds of the artifact and not only of this object.
        """
        return bool(self.gates_failed) or self.warmup_attack_failed


class ProbeRunner(Protocol):
    """How a single warmed probe is executed + its artifacts pulled.

    The live implementation brings up a fresh SUT, warms it, attacks at `rate`
    for `duration_s`, pulls the vegeta JSON summary + per-second p99 CSV + cgroup
    CSVs into the cell/stage dir, writes their `<slot>.probe.sha256`, and returns
    the outcome. Tests pass a deterministic fake.
    """

    def ensure_body_pool(self, cell: Cell) -> None: ...

    def run_probe(
        self,
        *,
        cell: Cell,
        stage: str,
        lang: str,
        rate: int,
        block: int,
        duration_s: int,
        warmup_cap_s: int,
        artifact_dir: Path,
    ) -> ProbeOutcome: ...


# --------------------------------------------------------------------------- #
# run context
# --------------------------------------------------------------------------- #
@dataclass
class RunContext:
    led: Ledger
    profile: ScaleProfile
    workload: Workload          # EFFECTIVE workload (== spec until levers fire)
    slo: SloConfig
    manifest: RunManifest
    runner: ProbeRunner
    run_id: str
    # The frozen spec workload, kept alongside so spec-vs-effective is always
    # derivable (spec/ is never edited; the levers only change run state).
    spec_workload: Workload | None = None
    # Opt-out that lets a non-passing capacity gate proceed, stamping `degraded`
    # into the ledger + manifest. Default False: a failed gate stops the run.
    allow_db_imposed_knee: bool = False
    # Opt-out that lets a cell whose slot never crossed the SLO be recorded,
    # stamping `degraded` into that cell's ladder stage. Default False.
    allow_sub_knee_ladder: bool = False
    # Signature of the gate's effective-workload record already applied to THIS
    # process's runner. Lets every stage re-assert the gate (see
    # _ensure_gate_applied) without re-generating targets or recreating
    # containers on each call. None => nothing applied yet in this process.
    gate_applied_signature: str | None = None

    def __post_init__(self) -> None:
        if self.spec_workload is None:
            self.spec_workload = self.workload

    @property
    def slo_ms(self) -> float:
        return float(self.slo.slo.threshold_ms)

    @property
    def slots(self) -> tuple[str, ...]:
        """The compared slots, from the pin in this run's ledger."""
        return self.led.slots


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #
def run_doctor_phase(ctx: RunContext) -> None:
    """doctor ALWAYS re-runs on process start (power-loss exception a)."""
    report = doctor.run_doctor(require_docker=IS_LINUX)
    out = paths.run_dir(ctx.run_id) / "doctor.json"
    report.save(out)
    failing = [c.name for c in report.checks if not (c.ok or c.skipped)]
    # Checks that exist for the BENCH overlay's resource ledger, which a
    # functional-only smoke run never applies: Docker Desktop's VM engine voids
    # resource ground truth (not functionality), and the 16-core / 26 GiB
    # floors size the pinned cpusets and mem_limits smoke does not use. Each
    # waiver is recorded; a measured tier still fails on any of them.
    waivable = {"docker_native_engine", "cpu_cores", "host_ram"}
    if failing and set(failing) <= waivable and ctx.profile.functional_only:
        ctx.led.mark_phase(
            "doctor", State.DONE, artifact="doctor.json", all_ok=False,
            waived=", ".join(sorted(failing))
            + " (functional-only scale; bench-overlay resource checks)",
        )
        return
    state = State.DONE if report.all_ok else State.FAILED
    ctx.led.mark_phase("doctor", state, artifact="doctor.json", all_ok=report.all_ok)
    if not report.all_ok and IS_LINUX:
        raise PhaseError("doctor failed; preconditions not met (see doctor.json)")


def _host_script_env(env: dict[str, str] | None = None) -> list[str]:
    """`NAME=value` for every knob infra/host-setup.sh reads, resolved from `env`.

    Forwarded EXPLICITLY because `sudo` resets the environment: without this the
    script ran on its own defaults while `hostread.HostExpectation.from_env`
    compared against the operator's, so a supported dial like
    `HOUSEKEEPING_CPUS=14` made the script write `CPUAffinity=15`, the read-back
    expect `CPUAffinity=14`, and every run and every resume abort on a Tier-1
    mismatch with no remedy but unsetting the variable.

    Always the full set, never only what is set: `sudo env NAME=` would pass an
    empty string, so the resolved value (the operator's or the shared default)
    is what crosses the boundary. `LEDGER_HOST_STATE` rides along because
    host-restore.sh reads the file host-setup.sh wrote, and the two must agree
    on where it is.
    """
    src = os.environ if env is None else env
    exp = hostread.HostExpectation.from_env(src)
    forwarded = {
        "SUT_CPUS": exp.sut_cpus_raw,
        "HOUSEKEEPING_CPUS": exp.housekeeping_raw,
        "PGDATA_PATH": exp.pgdata_path,
        "STOP_SERVICES": " ".join(exp.stop_services),
        "SYSTEMD_DROPIN": exp.systemd_dropin,
    }
    state_file = src.get("LEDGER_HOST_STATE", "").strip()
    if state_file:
        forwarded["LEDGER_HOST_STATE"] = state_file
    return [f"{k}={v}" for k, v in forwarded.items()]


def _privileged_argv(argv: list[str], *, forward_env: list[str] | None = None) -> list[str]:
    """Prefix `sudo -n` when not root (host tuning scripts require root).

    The bench box grants the operator NOPASSWD sudo (RUNBOOK prerequisite);
    `-n` makes a missing grant fail loudly at process start instead of hanging
    an unattended resume on a password prompt.

    `forward_env` crosses sudo's `env_reset` as an explicit `env NAME=value`
    prefix. As root there is no reset and the process environment already
    carries the same values, so the argv stays bare.
    """
    if os.geteuid() == 0:
        return argv
    return ["sudo", "-n", *(["env", *forward_env] if forward_env else []), *argv]


def run_host_setup_phase(ctx: RunContext) -> None:
    """host_setup: ALWAYS re-run on Linux (reboot resets tuning; idempotent).

    On Windows: record done with data {"skipped": "non-linux"} (exception b).
    """
    if not IS_LINUX:
        ctx.led.mark_phase("host_setup", State.DONE, skipped="non-linux", tier="n/a")
        return
    script = paths.infra_dir() / "host-setup.sh"
    res = run(
        _privileged_argv(["bash", str(script), "--tune"], forward_env=_host_script_env()),
        check=False, timeout=300,
    )
    state = State.DONE if res.ok else State.FAILED
    # Keep EVERY emitted action line, not a tail: the governor/boost/THP/IRQ/
    # affinity steps are the ones worth recording and a 5-line tail would
    # truncate them away. These lines say what the script DID; what
    # the box then reads back is `host_readback`, which runs next.
    ctx.led.mark_phase(
        "host_setup",
        state,
        applied=res.stdout.strip().splitlines(),
        tier="1",
        rc=res.returncode,
    )
    if not res.ok:
        raise PhaseError(f"host-setup.sh --tune failed: {res.stderr.strip()[-300:]}")


HOST_STATE_SOURCE = (
    "read back from sysfs/procfs/systemd after host_setup, on every run start "
    "and every resume"
)


def _accumulate_host_state(
    manifest: RunManifest, readings: list, tier1: list[str], tier2: list[str],
    tier1_not_evaluable: list[str],
) -> None:
    """Stamp this read into the manifest, keeping what earlier reads said.

    The provenance rule `_record_git_identity` already follows, applied to a
    SET of values: the first read is kept whole, the latest read replaces
    `readings`, and an attribute whose value changed between them is appended
    to `changed_during_run` -- a reboot mid-run that resets the governor is
    exactly what this section exists to make visible. Appending every resume's
    full set instead would grow without bound over a multi-day run, which the
    single-scalar git identity never has to worry about.

    SETTINGS ONLY. An outcome is load- and time-dependent by construction --
    `scaling_cur_freq` differs between any two samples and `/proc/interrupts`
    counts only climb -- so diffing outcomes put two fabricated rows at the top
    of `changed_during_run` on EVERY resume, and the governor flip this section
    exists to surface arrived as row 3 of a list whose first two rows are noise.
    """
    hs = manifest.host_state
    now = rfc3339_micros()
    if hs.first_read_utc is None:
        hs.first_read_utc = now
        hs.first_readings = list(readings)
    else:
        previous = {r.attribute: r.value for r in hs.readings}
        for r in readings:
            if r.kind == hostread.KIND_OUTCOME:
                continue
            if r.attribute in previous and previous[r.attribute] != r.value:
                hs.changed_during_run.append({
                    "at": now, "attribute": r.attribute,
                    "from": previous[r.attribute], "to": r.value,
                })
    hs.readings = list(readings)
    hs.last_read_utc = now
    hs.reads += 1
    hs.tier1_failures = list(tier1)
    hs.tier2_notes = list(tier2)
    hs.tier1_not_evaluable = list(tier1_not_evaluable)
    hs.source = "measured"
    hs.note = HOST_STATE_SOURCE


def run_host_readback_phase(ctx: RunContext) -> None:
    """B6: read every tuned value back, and refuse to measure on a mismatch.

    A phase of its own, AFTER host_setup, rather than a check inside doctor:
    doctor runs BEFORE the tuning is applied, so a Tier-1 comparison there
    fails every freshly-booted run before host-setup can fix anything (a reboot
    resets the live knobs and host-restore deletes the state file). Like doctor
    and host_setup it always re-runs -- the resume after a reboot is precisely
    when a read-back is load-bearing.

    The manifest is saved BEFORE the raise: an abort whose evidence did not
    reach disk leaves the operator with an error string and nothing to read.

    A Tier-1 attribute this box cannot evaluate travels in its own bucket and
    does NOT abort: an absent knob is not something an operator can fix, and
    hiding it among the Tier-2 notes made an empty `tier1_failures` claim a
    check that never happened.
    """
    if not IS_LINUX:
        ctx.led.mark_phase("host_readback", State.DONE, skipped="non-linux")
        return
    expectation = hostread.HostExpectation.from_env()
    readings = hostread.read_host_state(expectation)
    tier1, tier2, tier1_unevaluable = hostread.evaluate(readings)
    _accumulate_host_state(ctx.manifest, readings, tier1, tier2, tier1_unevaluable)
    groups, smt = hostread.topology_summary(readings)
    ctx.manifest.topology.l3_groups = groups
    ctx.manifest.topology.smt_control = smt
    ctx.manifest.save()
    paths.write_text_atomic(
        paths.run_dir(ctx.run_id) / "host_state.json",
        ctx.manifest.host_state.model_dump_json(indent=2),
    )
    ctx.led.mark_phase(
        "host_readback", State.FAILED if tier1 else State.DONE,
        artifact="host_state.json", tier1_failures=tier1, tier2_notes=len(tier2),
        tier1_not_evaluable=tier1_unevaluable,
    )
    if tier1:
        raise PhaseError(
            "host state does not match what host-setup.sh applied (Tier-1): "
            + "; ".join(tier1)
        )


def _measured_cell_ids(led: Ledger) -> list[str]:
    """Every cell with at least one DONE probe, in ledger order.

    The re-gate's retroactive stamp is derived from this rather than from the
    cell it was handed: it names the cells whose windows were ALREADY ON DISK
    when the verdict was reached, and on a resume of a ledger whose later cells
    ran before the re-gate existed (allowed, since the re-gate bumps no schema
    version) that set is not `[first_cell]`. The same is true
    after per-cell `ledgerbench ladder/confirm/soak --cell <other>` runs, which
    never invoke the re-gate at all.
    """
    return [
        c.cell_id for c in led.cells
        if any(p.state == State.DONE
               for st in ("ladder", "fit", "confirm", "soak")
               for p in getattr(c, st).probes)
    ]


def _any_cell_probe_done(led: Ledger) -> bool:
    """True if any cell stage has at least one DONE probe (DB has diverged)."""
    return bool(_measured_cell_ids(led))


def _db_has_diverged(led: Ledger) -> tuple[bool, str]:
    """Whether ANY write-probe has hit PG, so exact-count invariants no longer apply.

    The capacity gate's create-only ladder commits real invoices BEFORE any cell
    probe runs; it is marked RUNNING before its first write, so a crash anywhere
    after that point must NOT run the exact-count verify-only preflight (it
    would fail on the legitimately-grown invoice table and abort the resume).
    """
    gate = led.phases.get("capacity_gate")
    if gate is not None and gate.state != State.PENDING:
        return True, "capacity_gate create-only ladder wrote invoices"
    if _any_cell_probe_done(led):
        return True, "cells already wrote to PG (counts diverge by design)"
    return False, ""


def run_seed_phase(ctx: RunContext) -> None:
    """seed + invariants. On RESUME of a done seed, re-run --verify-only first.

    (power-loss exception c): a done seed phase still gets an invariant preflight
    on resume so we never probe against a PG that came back crash-inconsistent,
    UNLESS write-probes have already run (then the seeded counts diverge by
    design and the exact-count invariants no longer apply).
    """
    already_done = ctx.led.phase_done("seed")
    env = compose.ComposeEnv.base(profiles=[compose.PROFILE_SEED])
    dsn = "postgres://postgres:postgres@postgres:5432/ledgerline"
    if already_done:
        # Verify-only invariant preflight (cheap aggregates) proves PG came back
        # crash-consistent (power-loss exception c). BUT the exact-row-count
        # invariants only hold BEFORE any write-probe has run: the capacity
        # gate's create-only ladder and the measured mix (15% create_invoice)
        # both create invoices, so once either has started the DB has
        # LEGITIMATELY diverged from the seeded counts. So we only run the
        # preflight while no write-probe has run; afterward we skip it (the
        # divergence is intended, not corruption) and record why.
        diverged, why = _db_has_diverged(ctx.led)
        if diverged:
            ctx.led.mark_phase("seed", State.DONE, verify_only_skipped=why)
            return
        argv = compose.run_oneshot_argv(
            env, compose.SVC_SEED,
            ["--dsn", dsn, "--scale", ctx.profile.seed_scale, "--seed",
             str(ctx.workload.seeding.seed), "--verify-only"],
        )
        res = compose.compose(env, argv[2:], check=False, timeout=600)
        if not res.ok:
            ctx.led.mark_phase("seed", State.FAILED, verify_only=True, rc=res.returncode)
            raise PhaseError(
                "seed invariant preflight (--verify-only) failed on resume; PG may "
                f"be crash-inconsistent: {(res.stdout + res.stderr).strip()[-300:]}"
            )
        ctx.led.mark_phase("seed", State.DONE, verify_only_on_resume=True)
        return

    # Fresh seed wants a CLEAN DB: the seeder applies spec/schema.sql with bare
    # CREATE TABLE, so a leftover schema (e.g. from a prior run on the same
    # pgdata volume) makes it fail "relation already exists". Drop the volume
    # first on a fresh seed only (resume takes the verify-only branch above and
    # must never wipe).
    reset_db = getattr(ctx.runner, "reset_db", None)
    if callable(reset_db):
        reset_db()

    argv = compose.run_oneshot_argv(
        env, compose.SVC_SEED,
        ["--dsn", dsn, "--scale", ctx.profile.seed_scale, "--seed",
         str(ctx.workload.seeding.seed), "--schema", "/spec/schema.sql",
         "--grants", "--truncate", "--mode", "copy"],
    )
    res = compose.compose(env, argv[2:], check=False, timeout=3600)
    state = State.DONE if res.ok else State.FAILED
    # Recorded as `seed_scale`, NOT `scale`: `ledgerbench status` renders this
    # phase's data keys verbatim, so writing the dataset under `scale` made a
    # rehearsal run report `scale=full` -- the operator reads that as the run's
    # tier, which is the one thing it is not.
    ctx.led.mark_phase("seed", state, seed_scale=ctx.profile.seed_scale, rc=res.returncode)
    if not res.ok:
        raise PhaseError(f"seeder failed: {(res.stdout + res.stderr).strip()[-300:]}")


# Opt-out for a non-passing capacity gate. The DEFAULT is to abort: a
# db-imposed-shared-knee verdict must not yield a headline unless the operator
# opts in. Set to 1/true only when the
# degraded run is deliberate; it stamps `degraded` into the ledger AND the
# manifest so every downstream artifact carries the caveat.
ALLOW_DB_IMPOSED_KNEE_ENV = "LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE"
# Opt-out for a slot that reached the rung ceiling without crossing the SLO. The
# DEFAULT is to abort, for the same reason as above: the headline metric is cost
# AT the crossing rate, and round 1 published a comparison in which one stack had
# no measured knee at all. Set to 1/true only when the sub-knee cell is
# deliberate; it stamps `degraded` into the ladder stage record.
ALLOW_SUB_KNEE_LADDER_ENV = "LEDGERBENCH_ALLOW_SUB_KNEE_LADDER"
# Grants ONE more retry round to rungs the one-shot under-warm retry already
# consumed (the guard is ledger state, so a rung that stays excluded otherwise
# raises the identical PhaseError on every --resume). Recorded per rung in the
# stage data as `rewarmed_rungs_again`.
RETRY_EXCLUDED_AGAIN_ENV = "LEDGERBENCH_RETRY_EXCLUDED_RUNGS_AGAIN"
# spec/cache.md realizes the cache_hit_rate lever as a CACHE_TTL_SECONDS raise
# but spec/slo.yaml pre-registers no TTL value; the operator supplies it here
# (spec/ is frozen and no number may be invented).
CACHE_TTL_OVERRIDE_ENV = "LEDGERBENCH_CACHE_TTL_SECONDS"
# Working-set dial: requests the shared request-body pool is sized for on the
# measured tiers (default 1_000_000: see docs/RUNBOOK.md).
BODY_POOL_ENV = "LEDGERBENCH_BODY_POOL_REQUESTS"
# Refused, not ignored: see ScaleProfile._measured_body_pool_requests.
_LEGACY_TARGET_COUNT_ENV = "LEDGERBENCH_TARGET_COUNT"
# Comma-separated variant cell_ids to EXECUTE in the variants phase (default:
# declare-only). Example: LEDGERBENCH_RUN_VARIANTS=variant.npgsql-default-prepare
RUN_VARIANTS_ENV = "LEDGERBENCH_RUN_VARIANTS"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# STAGE 1's planning proxy for the mixed-workload knee, as a multiple of
# `ladder_start_rps`. It lives here and not in spec/ deliberately: it is a
# planning number, not a pre-registered gate threshold, so tuning it must not
# churn `config_hash` and invalidate every resumable run.
#
# It is NAMED because round 1's was an inline `ladder_start_rps * 8.0` that
# reached no artifact. At the full profile it is 500 x 8 = 4,000 rps, and round
# 1's cells then ran at 9,244-13,312 rps -- so the gate's 1.72x margin was taken
# against roughly a third of the demand the cells produced, and nothing in
# round 1's artifacts said so. Stage 2 (`run_capacity_regate_phase`) is what closes
# that; this constant plus `_projected_knee_basis` is what makes stage 1's
# assumption visible in the meantime.
PROJECTED_KNEE_FACTOR = 8.0


def _projected_knee_rps(ctx: RunContext) -> float:
    return float(ctx.profile.ladder_start_rps) * PROJECTED_KNEE_FACTOR


def _projected_knee_basis(ctx: RunContext) -> str:
    return (
        f"PLANNING PROXY, not a measurement: ladder_start_rps "
        f"{ctx.profile.ladder_start_rps} x {PROJECTED_KNEE_FACTOR:g} = "
        f"{_projected_knee_rps(ctx):.1f} rps of expected mixed-workload knee. "
        "The knee this run actually measures is not known until the first cell's "
        "fit, which is what the capacity re-gate (phase `capacity_regate`) tests "
        "against"
    )


def _cache_ttl_override() -> int | None:
    raw = os.environ.get(CACHE_TTL_OVERRIDE_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise PhaseError(
            f"{CACHE_TTL_OVERRIDE_ENV}={raw!r} is not an integer number of seconds"
        ) from None


def _apply_effective_workload(
    ctx: RunContext,
    params: capgate.ResolvedWorkloadParams,
    env_overrides: dict[str, str],
) -> None:
    """Make the resolved lever values REAL for the cells.

    Rewrites ctx.workload into the effective workload and pushes it (plus the
    compose env the topology/TTL levers need) into the runner, which rebuilds
    the body pool under the new mix. Without this the levers were computed
    into a throwaway object and every cell kept replaying the frozen spec mix.
    """
    ctx.workload = capgate.apply_params_to_workload(ctx.spec_workload, params)
    apply = getattr(ctx.runner, "apply_effective", None)
    if callable(apply):
        apply(workload=ctx.workload, env_overrides=env_overrides)
    elif env_overrides:
        # A runner that cannot carry the env cannot honor the lever; never let
        # that pass silently as an applied lever.
        raise PhaseError(
            f"capacity-gate levers need compose env {sorted(env_overrides)} but "
            f"{type(ctx.runner).__name__} exposes no apply_effective()"
        )


def _record_gate(
    ctx: RunContext,
    result: capgate.CapacityGateResult,
    *,
    params: capgate.ResolvedWorkloadParams,
    realization: capgate.LeverRealization,
    remeasured: float | None,
    out_dir: Path,
) -> None:
    """Persist the gate outcome to verdict.json + the ledger + the manifest.

    `degraded` is set on ANY non-pass verdict, including a run that aborts here
    without executing a cell: it fails safe (partial artifacts of a stopped run
    must not read as clean). `degraded_allowed_by` is the field that separates
    "stopped" from "ran anyway"; it is non-null only when the operator opt-out
    let the cells proceed.
    """
    from .manifest import CapacityGateStatus

    degraded = result.verdict != capgate.Verdict.PASS
    allowed_by = ALLOW_DB_IMPOSED_KNEE_ENV if (degraded and ctx.allow_db_imposed_knee) else None
    result.levers_applied = list(realization.applied)
    result.levers_not_applied = dict(realization.not_applied)
    result.save(out_dir / "verdict.json")

    effective = capgate.effective_workload_record(
        ctx.spec_workload, ctx.workload, params,
        realization=realization, levers_fired=result.levers_fired,
    )
    ctx.manifest.effective_workload = effective
    ctx.manifest.capacity_gate = CapacityGateStatus(
        verdict=result.verdict.value,
        detail=result.detail,
        pg_knee_rps=result.pg_knee_rps,
        pg_knee_measured=result.pg_knee_measured,
        pg_knee_usable_rungs=result.pg_knee_usable_rungs,
        pg_knee_measured_at_cores=result.pg_knee_measured_at_cores,
        pg_capacity_projected_rps=result.pg_capacity_projected_rps,
        pg_capacity_projection_basis=result.pg_capacity_projection_basis,
        projected_db_rps=result.projected_db_rps,
        headroom_factor=result.headroom_factor,
        levers_fired=[lv.name for lv in result.levers_fired],
        levers_applied=list(realization.applied),
        levers_not_applied=dict(realization.not_applied),
        remeasured_pg_knee_rps=remeasured,
        projected_knee_rps=_projected_knee_rps(ctx),
        projected_knee_basis=_projected_knee_basis(ctx),
        degraded=degraded,
        degraded_allowed_by=allowed_by,
    )
    ctx.manifest.save()

    ctx.led.mark_phase(
        "capacity_gate", State.DONE,
        verdict=result.verdict.value,
        detail=result.detail,
        # Which slot's INSERT shapes the ceiling was measured THROUGH: the
        # create-only ladder reaches PG through a SUT, so its verdict is not a
        # property of PostgreSQL alone.
        driving_slot=registry.active_slot_set().reference,
        # MEASURED knee only; the linear core-scaling projection travels under
        # its own key so the ledger cannot publish an assumption as a reading.
        pg_knee_rps=result.pg_knee_rps,
        pg_knee_measured=result.pg_knee_measured,
        pg_knee_measured_at_cores=result.pg_knee_measured_at_cores,
        pg_knee_usable_rungs=result.pg_knee_usable_rungs,
        pg_capacity_projected_rps=result.pg_capacity_projected_rps,
        pg_capacity_projection_basis=result.pg_capacity_projection_basis,
        remeasured_pg_knee_rps=remeasured,
        # The mixed-workload knee the DB-load projection was taken FROM. Round
        # 1's was an inline literal that reached no artifact; without it a reader
        # cannot tell that the margin was taken against a third of the demand.
        projected_knee_rps=_projected_knee_rps(ctx),
        projected_knee_basis=_projected_knee_basis(ctx),
        levers_fired=[lv.name for lv in result.levers_fired],
        levers_applied=list(realization.applied),
        levers_not_applied=dict(realization.not_applied),
        # Replayed verbatim on resume so the cells run the SAME effective
        # workload after a crash (the gate phase itself is skipped when done).
        effective={
            "write_share": params.write_share,
            "cache_hit_rate": params.cache_hit_rate,
            "pg_cores": params.pg_cores,
            "env_overrides": dict(realization.env_overrides),
        },
        degraded=degraded,
        degraded_allowed_by=allowed_by,
    )


# The verdicts that let measured cells proceed. TWO members, because the
# re-gate answers a different question and so returns a different pass: stage 1
# passes when PG has projected headroom before any cell, stage 2 when PG's
# MEASURED utilization under the first cell leaves that headroom at the rates
# the cells really run at. Anything else -- including "not evaluable" -- is not
# a passing verdict; an absent answer has never been one here (`_no_gate_record`).
PASSING_GATE_VERDICTS = frozenset({
    capgate.Verdict.PASS.value,
    capgate.Verdict.PASS_AT_REALIZED_RATES.value,
})


def _enforce_gate(ctx: RunContext, verdict: str, detail: str) -> None:
    """Abort the measured cells on a non-passing gate, unless opted out."""
    if verdict in PASSING_GATE_VERDICTS:
        return
    if ctx.profile.functional_only:
        return          # smoke rehearses the path; it publishes nothing
    if ctx.allow_db_imposed_knee:
        return
    raise PhaseError(
        f"capacity gate verdict {verdict!r}: {detail or 'headroom not established'}. "
        "The measured cells would not be a language comparison at a fixed latency "
        f"promise. Set {ALLOW_DB_IMPOSED_KNEE_ENV}=1 to run anyway (the run is "
        "then stamped degraded in the ledger and the manifest)."
    )


def _no_gate_record(ctx: RunContext) -> None:
    """No capacity-gate verdict exists in this run: refuse, or stamp degraded.

    An empty verdict is not a passing one. Without this, entering a stage on a
    run whose gate phase never executed (`ledgerbench ladder/confirm/soak
    --run-id <fresh id>`) measured cells against a DB whose headroom was never
    established; the same hole the gate itself closes, reached by another door.
    """
    if ctx.profile.functional_only:
        return                      # smoke rehearses the path; it publishes nothing
    if not ctx.allow_db_imposed_knee:
        raise PhaseError(
            "the capacity gate has not run in this run (no verdict recorded): a "
            "measured cell may not start before PG's headroom is established. "
            "Run `ledgerbench gate --run-id <id> --scale full` first (`run-all` "
            f"runs it in order), or set {ALLOW_DB_IMPOSED_KNEE_ENV}=1 to measure "
            "ungated (the run is then stamped degraded)."
        )
    # Opted out: the run proceeds, but every downstream artifact carries it.
    gate = ctx.manifest.capacity_gate
    if gate.verdict is None and not gate.degraded:
        gate.degraded = True
        gate.degraded_allowed_by = ALLOW_DB_IMPOSED_KNEE_ENV
        gate.detail = (
            "no capacity gate ran in this run; the cells measured against a DB "
            "whose headroom was never established (operator opt-out)"
        )
        ctx.manifest.save()


def _ensure_gate_applied(ctx: RunContext) -> None:
    """Re-apply a completed gate's effective workload, then re-enforce it.

    Idempotent (the applied state is memoized on the context, so re-asserting it
    per stage costs nothing and never re-triggers a targets regeneration or a
    container recreate) and called from EVERY stage entry point: the gate phase
    is skipped on resume and skipped entirely by the per-stage `ledgerbench
    ladder / confirm / soak` commands, so without this the cells would run
    against the FROZEN spec workload while the manifest claimed the effective
    one, and a degraded verdict would stop blocking after the first crash.
    """
    data = ctx.led.phase("capacity_gate").data
    eff = data.get("effective")
    if isinstance(eff, dict):
        signature = json.dumps(eff, sort_keys=True, default=str)
        if signature != ctx.gate_applied_signature:
            prior_env = dict(getattr(ctx.runner, "env_overrides", {}) or {})
            params = capgate.ResolvedWorkloadParams.from_workload(ctx.spec_workload)
            params.write_share = float(eff.get("write_share", params.write_share))
            params.cache_hit_rate = float(eff.get("cache_hit_rate", params.cache_hit_rate))
            params.pg_cores = int(eff.get("pg_cores", params.pg_cores))
            env_overrides = {str(k): str(v) for k, v in (eff.get("env_overrides") or {}).items()}
            _apply_effective_workload(ctx, params, env_overrides)
            # Recreate PG/vegeta only if THIS process has not already placed them
            # on the lever's cpusets (a per-stage recreate would be pure churn).
            stale_topology = any(prior_env.get(k) != v for k, v in env_overrides.items())
            apply_topology = getattr(ctx.runner, "apply_topology", None)
            if env_overrides and stale_topology and callable(apply_topology):
                apply_topology()
            ctx.gate_applied_signature = signature
    verdict = str(data.get("verdict", ""))
    # The opt-out is RUN state, not shell state: when the completed gate
    # carries the degraded_allowed_by stamp, a resume replays that recorded
    # decision. The documented recovery flow is a naked shell: re-demanding
    # the env var there would re-block a run the operator already chose (and
    # stamped) to continue.
    if data.get("degraded_allowed_by") and not ctx.allow_db_imposed_knee:
        ctx.allow_db_imposed_knee = True
    if not verdict:
        _no_gate_record(ctx)
    elif not verdict.startswith("functional-only"):
        _enforce_gate(ctx, verdict, str(data.get("detail", "")))
        if (ctx.allow_db_imposed_knee and data.get("degraded")
                and not data.get("degraded_allowed_by")):
            # Late opt-in: the gate blocked the original start and the operator
            # resumed WITH the env var. The cells that follow run because of
            # that choice, so stamp it where _record_gate would have (ledger +
            # manifest) without touching the phase's completion timestamp.
            data["degraded_allowed_by"] = ALLOW_DB_IMPOSED_KNEE_ENV
            ctx.led.save()
            gate = ctx.manifest.capacity_gate
            gate.degraded = True
            gate.degraded_allowed_by = ALLOW_DB_IMPOSED_KNEE_ENV
            ctx.manifest.save()


def run_capacity_gate_phase(ctx: RunContext) -> None:
    """Create-only ladder + capacity_gate.evaluate_gate + LEVER APPLICATION.

    On Windows smoke: run the ladder MECHANICS but record verdict
    "functional-only (non-linux)" (no PG cgroup evidence). On Linux: measure
    PG's create-tx ceiling, evaluate the pre-registered levers, APPLY the ones
    that fired (effective workload + compose topology env), re-measure PG when
    the pg_cores lever moved cores, and refuse to continue on a non-pass verdict
    unless the operator opted out.
    """
    if ctx.led.phase_done("capacity_gate"):
        _ensure_gate_applied(ctx)
        return
    out_dir = paths.run_dir(ctx.run_id) / "capacity-gate"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate the create-only targets (proves the create path + body sizes).
    # Capped: the gate ladder runs minutes, not hours, and every target carries a
    # ~2 KB create body: LEDGERBENCH_BODY_POOL_REQUESTS=1000000 would make this
    # one file ~2 GB for no additional gate evidence. 100k ≥ every gate
    # ladder's request budget; below the cap the working-set dial applies
    # unchanged.
    # The create-only ladder drives PG THROUGH a SUT, so its ceiling is a
    # per-slot quantity: the two stacks emit different INSERT shapes and
    # different round-trip counts. The driving slot is the slot set's reference
    # side, declared by the registry and recorded in the phase record so the
    # verdict cannot be read as a property of PostgreSQL alone.
    # `base_url` is a REQUIRED argument: there is no default slot.
    drive = registry.get_slot(registry.active_slot_set().reference)
    capgate.create_only_targets(
        ctx.workload, out_dir=out_dir, count=min(ctx.profile.body_pool_requests, 100_000),
        seed=ctx.workload.rng.target_generator_seed_default, scale=ctx.profile.seed_scale,
        base_url=drive.net_url,
    )

    if not IS_LINUX:
        # Functional rehearsal: we still exercised create-only targets gen; the
        # cgroup-evidence-based knee + gate verdict are Linux-only.
        ctx.led.mark_phase(
            "capacity_gate", State.DONE,
            verdict="functional-only (non-linux)",
            note="create-only ladder mechanics exercised; PG cgroup evidence absent.",
            rungs=ctx.profile.capacity_gate_rungs,
            driving_slot=drive.slot_id,
        )
        return

    # Linux: drive the create-only ladder against PG, capture the cpu.stat
    # delta per rung, then evaluate the gate over the measured knee
    # (LiveProbeRunner fills the rung table on the measured run).
    # Mark RUNNING BEFORE the first write: a crash mid-gate must already count
    # as DB divergence (see _db_has_diverged) or the resume preflight aborts.
    ctx.led.mark_phase("capacity_gate", State.RUNNING)
    spec_params = capgate.ResolvedWorkloadParams.from_workload(ctx.spec_workload)
    lever_params = capgate.ResolvedWorkloadParams.from_workload(ctx.spec_workload)
    knee = ctx.runner.measure_pg_create_knee(  # type: ignore[attr-defined]
        out_dir=out_dir, rungs=ctx.profile.capacity_gate_rungs,
        window_s=ctx.profile.capacity_gate_window_s,
    )
    empty = capgate.LeverRealization()

    if not knee.measured:
        # No knee => no honest headroom statement. Never fall back to a number.
        result = capgate.unmeasured_knee_result(knee, cfg=ctx.slo.capacity_gate)
        _record_gate(ctx, result, params=spec_params, realization=empty,
                     remeasured=None, out_dir=out_dir)
        _enforce_gate(ctx, result.verdict.value, result.detail)
        return

    projected_knee = _projected_knee_rps(ctx)
    result = capgate.evaluate_gate(
        pg_knee_rps=knee.knee_rps, projected_knee_rps=projected_knee,
        cfg=ctx.slo.capacity_gate, params=lever_params,
    )
    result.pg_knee_measured = True
    result.pg_knee_usable_rungs = knee.usable_rungs
    result.pg_knee_measured_at_cores = knee.measured_at_cores
    result.pg_knee_is_lower_bound = knee.lower_bound
    result.rungs = knee.rungs

    if result.verdict != capgate.Verdict.PASS:
        # Levers exhausted without reaching headroom. Do NOT apply them: moving
        # the workload off spec buys nothing here and would leave a run that is
        # neither the spec workload nor a headroom-clean one.
        _record_gate(ctx, result, params=spec_params, realization=empty,
                     remeasured=None, out_dir=out_dir)
        _enforce_gate(ctx, result.verdict.value, result.detail)
        return

    realization = capgate.realize_levers(
        result.levers_fired, cache_ttl_seconds=_cache_ttl_override()
    )
    if not realization.fully_applied:
        # Headroom only held because a lever fired that cannot take effect. Apply
        # NOTHING (a partial application matches no evaluated projection) and
        # report which lever is only a claim.
        result.verdict = capgate.Verdict.LEVER_NOT_APPLIED
        result.detail = "; ".join(
            f"{name}: {why}" for name, why in realization.not_applied.items()
        )
        _record_gate(ctx, result, params=spec_params, realization=realization,
                     remeasured=None, out_dir=out_dir)
        _enforce_gate(ctx, result.verdict.value, result.detail)
        return

    applied_params = capgate.params_for_applied_levers(
        spec_params, lever_params, realization.applied
    )
    _apply_effective_workload(ctx, applied_params, realization.env_overrides)

    remeasured: float | None = None
    if realization.env_overrides:
        apply_topology = getattr(ctx.runner, "apply_topology", None)
        if callable(apply_topology):
            topo = apply_topology()
            ctx.led.phase("capacity_gate").data["topology"] = topo
    if "pg_cores" in realization.applied:
        # The linear core-scaling assumption decided WHETHER to spend the lever;
        # it must not reach a published number, so re-measure at the new cores.
        knee2 = ctx.runner.measure_pg_create_knee(  # type: ignore[attr-defined]
            out_dir=out_dir, rungs=ctx.profile.capacity_gate_rungs,
            window_s=ctx.profile.capacity_gate_window_s,
            pg_cores=applied_params.pg_cores,
        )
        if not knee2.measured:
            result.verdict = capgate.Verdict.NO_PG_KNEE_MEASURED
            result.pg_knee_measured = False
            result.detail = f"re-measure after pg_cores lever: {knee2.detail}"
        else:
            remeasured = knee2.knee_rps
            ok, projected = capgate.headroom_after_remeasure(
                remeasured_pg_knee_rps=knee2.knee_rps,
                projected_knee_rps=projected_knee,
                cfg=ctx.slo.capacity_gate, params=applied_params,
            )
            result.projected_db_rps = projected
            if not ok:
                result.verdict = capgate.Verdict.DB_IMPOSED_SHARED_KNEE
                result.detail = (
                    f"re-measured PG knee {knee2.knee_rps:.1f} rps at "
                    f"{applied_params.pg_cores} cores misses the "
                    f"{ctx.slo.capacity_gate.headroom_factor}x headroom over "
                    f"{projected:.1f} rps projected DB load (the linear "
                    "core-scaling projection was optimistic)"
                )

    _record_gate(ctx, result, params=applied_params, realization=realization,
                 remeasured=remeasured, out_dir=out_dir)
    _enforce_gate(ctx, result.verdict.value, result.detail)


REGATE_PHASE = "capacity_regate"
# The window set the re-gate measures over. NOT every confirm window: a cell's
# confirm stage also runs the higher-knee slot at the COMMON rate (the slower
# slot's knee) for the paired verdict, and those windows sit BELOW the operating
# point by construction. Averaging them in would drag the measured utilization
# down and make the gate pass on evidence taken at a rate the headline is not
# about -- the same "measured somewhere other than the operating point" defect
# R1-A1 is. So the selection is exactly `rate == that slot's declared confirm
# rate`, read off the confirm stage's own `confirm_rates_by_slot`.
REGATE_WINDOW_SELECTION = (
    "PG UTILIZATION ONLY: confirm-stage windows at each slot's OWN declared "
    "confirm rate (`confirm_rates_by_slot`); the common-rate windows the paired "
    "verdict uses sit below the operating point and are excluded. This does NOT "
    "describe `measured_cache_hit_rate`, which is taken over ALL of the cell's "
    "confirm windows (a hit rate is a property of the workload's key draw, not "
    "of the rate it was drawn at) -- `cache_hit_rate_source` states its own "
    "window count"
)
REGATE_RULE = (
    "PostgreSQL cgroup cpu.stat delta / (pg cores x window wall seconds), per "
    "slot, over the first cell's operating-point windows; the MAX over slots "
    "must be <= 1/headroom_factor. A direct measurement under the actual cells, "
    "not a second projection: see capacity_gate.regate_decision"
)


def _pg_windows_by_slot(
    run_id: str, cell_id: str, stage: str, rates_by_slot: dict[str, int],
) -> dict[str, list[capgate.PgWindowSample]]:
    """The PostgreSQL cgroup rows for each slot's own-rate windows of one stage.

    The PG summary is written per (block, SLOT) -- a block's probes run
    sequentially and share a directory -- so the per-slot split is exactly the
    file name, and summing wall seconds across windows never double-counts time
    two slots were both on the box.
    """
    base = paths.run_dir(run_id) / "cells" / cell_id / stage
    out: dict[str, list[capgate.PgWindowSample]] = {sid: [] for sid in rates_by_slot}
    if not base.is_dir():
        return out
    for ratedir in sorted(base.glob("rate*")):
        try:
            rate = int(ratedir.name[len("rate"):])
        except ValueError:
            continue
        for blockdir in sorted(ratedir.glob("block*")):
            for sid, own in rates_by_slot.items():
                if rate != own:
                    continue
                doc = _load_json(
                    blockdir / f"cpu_mem_{sid}.{compose.SVC_POSTGRES}.summary.json"
                )
                if not doc or not doc.get("enabled"):
                    continue
                out[sid].append(capgate.PgWindowSample(
                    cpu_delta_usec=doc.get("cpu_usage_delta_usec"),
                    window_seconds=doc.get("window_seconds"),
                    cpuset_effective=doc.get("cpuset_effective"),
                ))
    return out


def _realized_rates(ctx: RunContext, cell: Cell) -> dict[str, int]:
    """Each slot's DECLARED confirm rate for this cell, as the runner recorded it.

    Read from the confirm stage rather than re-derived from the fit: the confirm
    stage is what actually ran, and `_confirm_members` can fall back to a slot's
    top rung when the fit produced no knee. Reading the declaration is the same
    rule `analyze._declared_operating_rates` selects windows by, so the re-gate
    and the report cannot disagree about where the operating point was.
    """
    confirm = ctx.led.stage(cell.cell_id, "confirm").data.get("confirm_rates_by_slot") or {}
    fit = ctx.led.stage(cell.cell_id, "fit").data.get("confirm_rates_by_slot") or {}
    out: dict[str, int] = {}
    for sid in ctx.slots:
        raw = confirm.get(sid, fit.get(sid))
        if raw is not None:
            out[sid] = int(raw)
    return out


def _record_regate(
    ctx: RunContext,
    *,
    decision: capgate.RegateDecision,
    util: capgate.PgUtilizationMeasurement,
    cell: Cell,
    realized: dict[str, int],
    realized_knee: float | None,
    hit_rate: float | None,
    hit_rate_source: str,
    projected: float | None,
    capacity: float | None,
    projection_skipped: str,
) -> None:
    """Persist the re-gate to the ledger + the manifest, and stamp cell 1.

    Written BEFORE the enforcement raise, for the same reason `_record_gate` is:
    a stop whose evidence never reached disk leaves the operator with an error
    string and nothing to read.
    """
    from .manifest import CapacityRegateStatus

    degraded = decision.verdict != capgate.Verdict.PASS_AT_REALIZED_RATES
    allowed_by = ALLOW_DB_IMPOSED_KNEE_ENV if (degraded and ctx.allow_db_imposed_knee) else None
    status = CapacityRegateStatus(
        verdict=decision.verdict.value,
        detail=decision.detail,
        rule=REGATE_RULE,
        pg_utilization=util.utilization,
        pg_utilization_ceiling=capgate.utilization_ceiling(
            ctx.slo.capacity_gate.headroom_factor
        ),
        pg_utilization_by_slot=dict(util.by_slot),
        pg_utilization_binding_slot=util.binding_slot,
        pg_cpu_seconds=util.cpu_seconds if util.measured else None,
        window_seconds=util.window_seconds if util.measured else None,
        windows=util.windows,
        windows_by_slot=dict(util.windows_by_slot),
        pg_cores=util.cores,
        pg_cores_source=util.cores_source,
        utilization_ok=decision.utilization_ok,
        realized_rates_by_slot={sid: float(r) for sid, r in realized.items()},
        realized_knee_rps=realized_knee,
        window_selection=REGATE_WINDOW_SELECTION,
        measured_cache_hit_rate=hit_rate,
        cache_hit_rate_source=hit_rate_source,
        projected_db_rps_at_realized_rates=projected,
        pg_capacity_rps=capacity,
        projection_evaluated=decision.projection_ok is not None,
        projection_ok=decision.projection_ok,
        projection_skipped_because=projection_skipped,
        headroom_factor=ctx.slo.capacity_gate.headroom_factor,
        # RETROACTIVE by construction, and READ OFF THE LEDGER rather than from
        # the cell argument: on a resume of a pre-re-gate ledger, or after a
        # per-cell CLI run, more than the first cell can already be measured,
        # and a caveat that names one of them is a caveat the other's numbers
        # escape. `cell` is always in the list; it is not always the whole list.
        cells_measured_before_regate=_measured_cell_ids(ctx.led) or [cell.cell_id],
        degraded=degraded,
        degraded_allowed_by=allowed_by,
    )
    gate = ctx.manifest.capacity_gate
    gate.regate = status
    if degraded:
        # The FIRST gate's own verdict is untouched -- it passed on its own
        # terms. What travels is the run-level stamp, so a downstream artifact
        # that reads only `capacity_gate.degraded` cannot miss the re-gate.
        gate.degraded = True
        gate.degraded_allowed_by = gate.degraded_allowed_by or allowed_by
    ctx.manifest.save()
    ctx.led.mark_phase(
        REGATE_PHASE, State.DONE,
        **{k: v for k, v in status.model_dump().items() if k != "rule"},
    )


def run_capacity_regate_phase(ctx: RunContext, first_cell: Cell) -> None:
    """STAGE 2 of the capacity gate: re-ask headroom at the REALIZED rates (R1-A6).

    Runs once, straight after the first cell, and blocks the remaining cells the
    same way stage 1 blocks all of them -- through `_enforce_gate`, under the
    same `LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE` opt-out.

    NO LEVERS HERE, and that is the point of writing it separately from
    `evaluate_gate` rather than reusing it. `evaluate_gate` fires the
    pre-registered levers until headroom holds; a lever fired AFTER the first
    cell would change the workload for cells 2..N while cell 1 had already run
    the old one, so the cells would no longer be comparable to each other. A
    failure here is a stop, not a lever, and `LeverMachine` is never constructed.
    """
    if ctx.led.phase_done(REGATE_PHASE):
        return
    cfg = ctx.slo.capacity_gate
    realized = _realized_rates(ctx, first_cell)
    knees = [
        float(k) for k in
        (ctx.led.stage(first_cell.cell_id, "fit").data.get("knee_by_slot") or {}).values()
        if k is not None
    ]
    # PG serves whichever stack is resident, so the DB-load projection is taken
    # from the HIGHEST realized knee, not a mean: the binding case is the one the
    # gate is about.
    realized_knee = max(knees) if knees else None

    # Scoped to the CONFIRM stage, the same windows the utilization rule reads,
    # rather than to the whole cell: the ladder sweeps two decades of rate below
    # the operating point, and its residency is not the residency the realized
    # rates ran at. `manifest.cache_stats` is a run-wide figure and does not
    # exist yet here anyway -- it is filled at finalization, after every cell.
    hits, misses, hit_windows = _cache_keyspace_deltas(
        paths.run_dir(ctx.run_id) / "cells" / first_cell.cell_id / "confirm"
    )
    total = hits + misses
    hit_rate = (hits / total) if (hit_windows and total) else None
    hit_rate_source = (
        f"MEASURED: redis INFO keyspace deltas over {hit_windows} confirm "
        f"window(s) of {first_cell.cell_id}"
        if hit_rate is not None else
        "NOT measured over this cell's confirm windows; the projection falls "
        "back to spec/workload.yaml's constructed target hit rate"
    )

    if realized_knee is None:
        # No slot crossed the SLO in cell 1, so its confirm windows sit at a top
        # rung, not at an operating point. PG's utilization there is a fact about
        # a rate the headline is not taken at, and the remaining cells will run
        # at THEIR OWN fitted knees -- which this cell says nothing about. A
        # verdict from those windows would be a claim broader than the evidence.
        decision = capgate.RegateDecision(
            verdict=capgate.Verdict.REGATE_NOT_EVALUABLE,
            detail=(
                f"{first_cell.cell_id} fitted no knee for any slot, so its "
                "confirm windows are not at an operating point and PostgreSQL's "
                "utilization over them does not answer the headroom question at "
                "the rates the remaining cells will run at. Re-run the cell with "
                f"a ladder that crosses, or set {ALLOW_DB_IMPOSED_KNEE_ENV}=1 to "
                "continue with the re-gate unanswered (the run is then stamped "
                "degraded)."
            ),
        )
        _record_regate(
            ctx, decision=decision, util=capgate.PgUtilizationMeasurement(utilization=None),
            cell=first_cell, realized=realized, realized_knee=None,
            hit_rate=hit_rate, hit_rate_source=hit_rate_source,
            projected=None, capacity=None,
            projection_skipped="no fitted knee to project from",
        )
        _enforce_gate(ctx, decision.verdict.value, decision.detail)
        return

    params = capgate.ResolvedWorkloadParams.from_workload(ctx.workload)
    if hit_rate is not None:
        # The MEASURED hit rate, not spec/workload.yaml's constructed 0.80: the
        # get_invoice term of the DB-load projection is scaled by the miss rate,
        # so a run whose cache ran colder than it was built to ran a higher DB
        # load than the pre-registered projection ever modelled.
        params.cache_hit_rate = hit_rate
    projected = capgate.projected_db_load_rps(realized_knee, params)

    windows = _pg_windows_by_slot(ctx.run_id, first_cell.cell_id, "confirm", realized)
    # The gate's LEDGER record, not `params.pg_cores`: `ResolvedWorkloadParams`
    # always initialises pg_cores to the bench default, so reading it back would
    # report 3 on exactly the run where the pg_cores lever moved it to 4 -- an
    # under-counted denominator that inflates every utilization it computes.
    gate_data = ctx.led.phase("capacity_gate").data
    fallback_cores = (gate_data.get("effective") or {}).get("pg_cores")
    util = capgate.pg_utilization_from_windows(
        windows,
        fallback_cores=None if fallback_cores is None else int(fallback_cores),
        fallback_cores_source=(
            "the capacity gate's effective pg_cores (no measured window carried "
            "cpuset.cpus.effective)"
        ),
    )

    # The projection rule is SECONDARY and is only evaluated when stage 1's
    # ladder actually broke. A ladder that ran out of rungs while still tracking
    # reports a LOWER BOUND, and a lower bound cannot fail a headroom test
    # honestly -- comparing it against a realized load would fail every full-scale
    # run by construction, which is how a gate becomes decorative.
    gate_status = ctx.manifest.capacity_gate
    verdict_json = _read_gate_verdict_json(ctx) or {}
    # Ledger and verdict.json before the manifest: both are written by
    # `_record_gate` and both survive a resume, while an aborted finalization can
    # leave the manifest's copy behind. A re-measured knee wins over the 3-core
    # one whenever the pg_cores lever moved cores.
    capacity = (
        gate_status.remeasured_pg_knee_rps
        or gate_data.get("remeasured_pg_knee_rps")
        or gate_status.pg_knee_rps
        or gate_data.get("pg_knee_rps")
        or verdict_json.get("pg_knee_rps")
    )
    lower_bound = bool(verdict_json.get("pg_knee_is_lower_bound"))
    projection: tuple[float, float] | None = None
    projection_skipped = ""
    if capacity is None:
        projection_skipped = "stage 1 measured no PostgreSQL create-only knee"
    elif lower_bound:
        projection_skipped = (
            f"stage 1's create-only ladder never broke, so its {capacity:.1f} rps "
            "is a lower bound on PostgreSQL's ceiling, not a measurement of it"
        )
    else:
        projection = (float(capacity), projected)

    decision = capgate.regate_decision(util, cfg=cfg, projection=projection)
    _record_regate(
        ctx, decision=decision, util=util, cell=first_cell, realized=realized,
        realized_knee=realized_knee, hit_rate=hit_rate,
        hit_rate_source=hit_rate_source, projected=projected,
        capacity=None if capacity is None else float(capacity),
        projection_skipped=projection_skipped,
    )
    _enforce_gate(ctx, decision.verdict.value, decision.detail)


def _read_gate_verdict_json(ctx: RunContext) -> dict | None:
    return _load_json(paths.run_dir(ctx.run_id) / "capacity-gate" / "verdict.json")


def run_validation_phase(ctx: RunContext) -> None:
    """Equivalence pytest (both SUTs up); THE validation gate.

    The gate is cross-service response equivalence: every declared endpoint is
    driven against both SUTs and their normalized bodies must match. A failure
    stops the run here, before any measured window exists, because a stack that
    does not compute the same answer cannot be compared on what that answer
    costs.

    Coordinated-omission safety is a separate, implemented pair of checks and is
    NOT scored here: per-probe achieved-vs-offered tolerance
    (`spec/slo.yaml co_validation`) and smoke's `docker pause` stall test.

    The ledger note this stamps says what the phase DID. It makes no standing
    claim about measurements that do not exist.
    """
    if ctx.led.phase_done("validation"):
        return
    # Equivalence: the runner runs the equivalence pytest with both SUTs live
    # (`run_equivalence`); here the phase is recorded from its result, and a
    # failure aborts the run.
    eq = ctx.runner.run_equivalence()  # type: ignore[attr-defined]
    ctx.led.mark_phase(
        "validation", State.DONE if eq.get("ok", False) else State.FAILED,
        equivalence=eq,
        note="cross-service response equivalence over every declared endpoint, "
             "both SUTs live; this is the validation gate.",
    )
    if not eq.get("ok", False):
        raise PhaseError(f"equivalence validation failed: {eq.get('detail', '')[:300]}")


# --------------------------------------------------------------------------- #
# cell stages: ladder -> fit -> confirm -> soak
# --------------------------------------------------------------------------- #
def _ladder_rates(profile: ScaleProfile) -> list[int]:
    rates: list[int] = []
    r = float(profile.ladder_start_rps)
    for _ in range(profile.ladder_max_rungs):
        rates.append(int(round(r)))
        r *= profile.ladder_step_factor
    return rates


def gate_ladder_rates(profile: ScaleProfile, rungs: int) -> list[int]:
    """Offered rates for the create-only CAPACITY-GATE ladder.

    Deliberately separate from `_ladder_rates`: it climbs on
    `capacity_gate_step_factor`, which `rehearsal` sets independently (see the
    field comment). The runner consumes this list instead of multiplying inline,
    so the step the gate actually climbs is testable without a daemon -- the
    inline form was reachable only through docker, and the two ladders sharing
    one dial is exactly the defect that hid there.
    """
    rates: list[int] = []
    r = float(profile.ladder_start_rps)
    for _ in range(rungs):
        rates.append(int(round(r)))
        r *= profile.capacity_gate_step_factor
    return rates


def _artifact_dir(ctx: RunContext, cell: Cell, stage: str, *extra: str) -> Path:
    d = paths.run_dir(ctx.run_id) / "cells" / cell.cell_id / stage
    for e in extra:
        d = d / e
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_achieved_offered(
    ctx: RunContext, cell: Cell, stage: str, probe: ProbeRecord, rate: int, ratio: float
) -> None:
    """Append a per-probe achieved-vs-offered row to the manifest.

    Real data only: offered = the ladder/confirm rate, achieved = the vegeta
    report rate. Image digests / host state / cgroup deltas are left for the
    Linux bench-tier (clearly empty rather than fabricated on Windows).
    """
    from .manifest import AchievedVsOffered

    probe_id = f"{cell.cell_id}/{stage}/r{rate}/b{probe.block}/{probe.slot}"
    row = AchievedVsOffered(
        probe_id=probe_id,
        offered_rate=float(rate),
        achieved_rate=float(probe.achieved if probe.achieved is not None else 0.0),
        ratio=round(ratio, 4),
        max_workers=ctx.profile.max_workers,
        connections=ctx.profile.connections,
        timeout_s=ctx.profile.timeout_s,
    )
    # Idempotent on resume: a re-run probe (orphan-block reset / in-flight redo)
    # REPLACES its row instead of appending a duplicate audit record.
    rows = ctx.manifest.achieved_vs_offered
    for i, existing in enumerate(rows):
        if existing.probe_id == probe_id:
            rows[i] = row
            break
    else:
        rows.append(row)
    ctx.manifest.save()


# Which cell env override lands on which slot's knob row comes from the REGISTRY,
# never from a table here. A runtime-specific knob written onto the other slot's
# row fabricates a value that SUT never saw; and a knob added to a language but
# forgotten in a second hand-kept list is the same defect one level down.
# `int_knobs` is registry-driven for the same reason: a new int knob missing
# from a local set silently lands as a string.


def _set_knob(knobs, attr: str, raw: str, int_knobs: frozenset[str]) -> None:
    setattr(knobs, attr, int(raw) if attr in int_knobs else raw)


def _record_cell_knobs(ctx: RunContext, cell: Cell) -> None:
    """Echo THIS CELL's knob env overrides + pinned parallelism into the manifest.

    Knobs are recorded PER CELL (manifest.knobs_by_cell): a single run-level set
    of rows can only describe the last cell that ran, which is what round 1's
    manifest published (gc-generous values presented as the whole run's).
    Runtime-specific env lands only on its own slot's row, and which env var is
    runtime-specific is the registry's declaration, not a table here.

    Defaults (compose.yaml ${VAR} fallbacks) are not fabricated here. The
    core-parallelism knob is pinned in compose.bench.yaml and `processor_count`
    is read live from each SUT's /runtime-stats build block. On a Linux measured
    run the reported processor_count MUST equal SUT_CORES (compose pins it); a
    mismatch fails the run loudly. Windows smoke records whatever the SUT reports
    without asserting (no core pinning off-Linux).
    """
    from .manifest import CellKnobs, Knobs

    env = {**cell.extra_env}
    # Lever-realized env (e.g. CACHE_TTL_SECONDS from the capacity gate) applies
    # to every SUT and is part of what this cell actually ran with.
    runner_env = getattr(ctx.runner, "env_overrides", None)
    if isinstance(runner_env, dict):
        env.update({k: v for k, v in runner_env.items() if k in registry.SHARED_KNOB_ENV})

    row = ctx.manifest.knobs_by_cell.get(cell.cell_id) or CellKnobs(cell_id=cell.cell_id)
    row.extra_env = dict(cell.extra_env)
    for slot_id in ctx.slots:
        knobs = row.by_slot.setdefault(slot_id, Knobs())
        int_knobs = registry.int_knobs_for(slot_id)
        # knob_env_for is SHARED | this slot's language-only map, so a knob that
        # belongs to the other runtime is not in it and cannot land here.
        for var, attr in registry.knob_env_for(slot_id).items():
            if var in env:
                _set_knob(knobs, attr, env[var], int_knobs)

    # processor_count and data_layer are ground-truth from /runtime-stats (the
    # same build block the runtime-stats poller parses). The live runner exposes
    # the read; the fake test runner does not, so this stays best-effort (None
    # off the live path). The data_layer check exists because
    # an env override that never reaches the SUT must fail loudly, not record
    # the intended value next to different effective behavior.
    read_build = getattr(ctx.runner, "read_build", None)
    read_pc = getattr(ctx.runner, "read_processor_count", None)
    if callable(read_build) or callable(read_pc):
        for slot_id in ctx.slots:
            slot = registry.get_slot(slot_id)
            cell_knobs = row.by_slot[slot_id]
            run_knobs = ctx.manifest.knobs_by_slot.setdefault(slot_id, Knobs())
            if callable(read_build):
                build = read_build(slot_id, extra_env=cell.extra_env)
            else:
                pc_only = read_pc(slot_id)
                build = None if pc_only is None else SimpleNamespace(
                    processor_count=pc_only, data_layer=None,
                    runtime_version=None, service_version=None)
            if build is None:
                continue
            pc = build.processor_count
            if pc is not None:
                cell_knobs.processor_count = int(pc)
                run_knobs.processor_count = int(pc)
                if IS_LINUX and int(pc) != SUT_CORES:
                    raise PhaseError(
                        f"{slot_id} SUT reported processor_count={pc} but the pinned "
                        f"SUT_CORES is {SUT_CORES}; core parallelism is not pinned as "
                        "expected (check compose.bench.yaml + cpuset)"
                    )
            # The expected data layer is the SLOT'S declaration, overridden by a
            # cell env that names one. Round 1 hard-coded {"go": "sqlc", ...},
            # which makes P2's ado slot mismatch on every probe.
            expected_dl = env.get("DATA_LAYER") or slot.expected_data_layer
            if build.data_layer is not None:
                cell_knobs.data_layer = build.data_layer
                if IS_LINUX and build.data_layer != expected_dl:
                    raise PhaseError(
                        f"{slot_id} SUT reported data_layer={build.data_layer!r} but cell "
                        f"{cell.cell_id} expects {expected_dl!r}; the DATA_LAYER "
                        "env did not reach the SUT (check compose interpolation)"
                    )
            _record_slot_versions(ctx, slot, build)
    ctx.manifest.knobs_by_cell[cell.cell_id] = row
    ctx.manifest.save()


def _record_slot_versions(ctx: RunContext, slot: registry.Slot, build) -> None:
    """Keep the /runtime-stats build block's versions instead of discarding them.

    Records the runtime and service versions from the build block and the image
    digest from `compose.container_image_digest`. The image digest is
    best-effort because it needs a running container, which the fake test
    runner does not have.
    """
    versions = ctx.manifest.versions
    measured = False
    runtime = getattr(build, "runtime_version", None)
    if runtime:
        versions.runtime_by_slot[slot.slot_id] = str(runtime)
        measured = True
    service_version = getattr(build, "service_version", None)
    if service_version:
        versions.service_version_by_slot[slot.slot_id] = str(service_version)
        measured = True
    digest = compose.container_image_digest(slot.container_name())
    if digest:
        versions.image_digests[slot.service] = digest
        measured = True
    if measured:
        versions.source = "/runtime-stats build block + docker inspect RepoDigests"


def _execute_block_probes(
    ctx: RunContext, cell: Cell, stage: str, *, duration_s: int
) -> None:
    """Run all not-done probes in a stage, honoring RCB slot order + resume.

    Block integrity on resume is enforced by the caller (reset_orphan_blocks)
    before this runs. Each probe writes its artifacts + its `<slot>.probe.sha256`,
    THEN mark_probe_done (probe atomicity).
    """
    rec = ctx.led.stage(cell.cell_id, stage)
    # Group by BLOCK ID and execute in the recorded order. Block ids are unique
    # within a stage (`plan_blocks(block_offset=...)`), so the rate is not part
    # of the key -- it cannot be, now that a confirm block holds each slot at its
    # own knee.
    by_block: dict[int, list[ProbeRecord]] = {}
    for p in rec.probes:
        by_block.setdefault(p.block, []).append(p)

    for block in sorted(by_block.keys()):
        members = by_block[block]
        order = members[0].slot_order
        members.sort(
            key=lambda p: p.order_index if p.order_index is not None
            else order.index(p.slot)
        )
        for probe in members:
            if probe.state == State.DONE:
                continue
            rate = probe.rate
            probe.state = State.RUNNING
            ctx.led.save()
            adir = _artifact_dir(ctx, cell, stage, f"rate{rate}", f"block{block}")
            ctx.runner.ensure_body_pool(cell)
            outcome = ctx.runner.run_probe(
                cell=cell, stage=stage, lang=probe.slot, rate=rate, block=block,
                duration_s=duration_s, warmup_cap_s=ctx.profile.warmup_cap_s,
                artifact_dir=adir,
            )
            if not outcome.ok:
                probe.state = State.FAILED
                ctx.led.save()
                raise PhaseError(
                    f"probe failed: {cell.cell_id}/{stage} block{block} {probe.slot}"
                    f"@{rate}: {outcome.detail[:200]}"
                )
            # record the achieved rate on the probe now so the manifest row reads
            # the real value (mark_probe_done below also sets it; this just makes
            # it available to _record_achieved_offered which runs first).
            probe.achieved = outcome.achieved
            key = probe_key(rate, block, probe.slot)
            # Measurement gates (full scale): a probe whose achieved rate fell
            # outside the CO tolerance, or whose error rate exceeded the SLO
            # error_rate_max, is EXCLUDED from the fit input exactly like an
            # under-warmed probe (recorded keyed by probe so resume re-runs
            # overwrite idempotently, never duplicate). Smoke records only.
            ratio = outcome.achieved / rate if rate else 0.0
            tol = ctx.slo.co_validation.achieved_vs_offered_tolerance_pct
            err_max = float(ctx.slo.slo.error_rate_max)
            exclude_reason: str | None = None
            if not ctx.profile.functional_only:
                if abs(ratio - 1.0) * 100.0 > tol:
                    exclude_reason = f"achieved/offered {ratio:.4f} outside +/-{tol}%"
                elif outcome.error_rate > err_max:
                    exclude_reason = f"error_rate {outcome.error_rate:.4f} > {err_max}"
            exc_map = rec.data.setdefault("excluded", {})
            if exclude_reason is not None:
                exc_map[key] = exclude_reason
            else:
                exc_map.pop(key, None)
            rec.data.setdefault("under_warmed", {})[key] = bool(outcome.under_warmed)
            # Pooled per-probe p99 (vegeta report tail) is the SLO ground truth for
            # the knee fit / bracketing. The per-second series is persisted too but
            # feeds ONLY the stationarity / warmup gates.
            rec.data.setdefault("p99_ms", {})[key] = outcome.p99_ms
            if outcome.per_second_p99:
                rec.data.setdefault("p99_series", {})[key] = outcome.per_second_p99
            _record_achieved_offered(ctx, cell, stage, probe, rate, ratio)
            ctx.led.mark_probe_done(
                cell.cell_id, stage, probe,
                artifact=outcome.artifact, achieved=outcome.achieved,
                error_rate=outcome.error_rate,
                gates_evaluated=outcome.gates_evaluated,
                gates_failed=outcome.gates_failed,
                warmup_attack_failed=outcome.warmup_attack_failed,
                draw_inc=outcome.draw_inc,
                draw_seconds=outcome.draw_seconds,
            )


def _slot_crossing(rec, slot: str, slo_ms: float) -> tuple[list[int], bool]:
    """(bracket, crossed) for ONE slot over its COMPLETE rungs only.

    Non-raising -- an emptied rung is retried right after execution, not here --
    and it reads `_complete_rates`, so a rung a crash left half-run is invisible
    to the stop rule instead of contributing a mean over 4 probes where 6 were
    planned.
    """
    by_rate, _ = _rung_values(rec, slot)
    complete = _complete_rates(rec, slot)
    rungs = sorted(
        (r, sum(v) / len(v)) for r, v in by_rate.items() if r in complete
    )
    return _bracket_crossing(rungs, slo_ms)


def run_ladder(ctx: RunContext, cell: Cell) -> None:
    """Geometric ladder until EVERY slot's p99 crossing is bracketed (or cap hit).

    Rungs are planned and executed INCREMENTALLY (rung-major): after each rung's
    RCB blocks complete, the crossing check runs PER SLOT over the rungs measured
    so far, and a slot that has bracketed its crossing stops laddering while the
    others keep climbing. That is the whole of R1-A1/D9: round 1 stopped BOTH
    stacks the moment the mixed mean crossed, so the stack that degraded slower
    was never measured anywhere near its own knee and the headline compared cost
    at a rate one stack's worst configuration had chosen.

    A rung above the first crosser's knee is therefore a SOLO block -- one probe,
    one slot. Nothing stamps that; `len(slot_order) > 1` says it, and a derived
    flag cannot disagree with the record.

    The ladder stops when every slot has crossed, or at `ladder_max_rungs`. A
    slot still sub-knee at the ceiling is recorded in
    `ceiling_reached_without_crossing` and, at measured scale, ABORTS the run,
    so the artifact itself states that the slot never crossed.

    Resume: already-planned rungs keep their probes (orphan blocks reset whole,
    their recorded p99 dropped with them); the crossing check re-derives the stop
    point from the DONE probes, so a resumed ladder never plans past a crossing.

    Re-asserts the capacity gate AND the cell's gate plan first: `ledgerbench
    ladder` enters here without ever calling run_cell, so the gate's effective
    workload, its verdict, and the plan-time refusal of an unsatisfiable warmup
    gate set all have to be applied at THIS door too.
    """
    stage = "ladder"
    assert_gate_plan_sound(ctx, [cell])
    _ensure_gate_applied(ctx)
    rec = ctx.led.stage(cell.cell_id, stage)
    if rec.state == State.DONE:
        return

    if rec.probes:
        ctx.led.reset_orphan_blocks(cell.cell_id, stage)

    rates = _ladder_rates(ctx.profile)
    n_blocks = ctx.profile.ladder_reps_per_rung
    for ri, rate in enumerate(rates):
        # Crossing check over each slot's COMPLETED rungs, before planning the
        # next one (non-raising: emptied rungs are retried right after execution).
        remaining = [
            sid for sid in ctx.slots if not _slot_crossing(rec, sid, ctx.slo_ms)[1]
        ]
        if not remaining:
            break
        # The already-planned guard is keyed on (slot, rate): after the split,
        # a rate can be planned for one slot and not the other.
        todo = [
            sid for sid in remaining
            if all(not (p.rate == rate and p.slot == sid) for p in rec.probes)
        ]
        if todo:
            for p in plan_blocks(
                n_blocks=n_blocks, rate=rate,
                rng_seed=ctx.led.rng_seed, salt=_salt(cell.cell_id, stage, ri),
                slots=todo, block_offset=ri * n_blocks,
            ):
                rec.probes.append(p)
            ctx.led.mark_stage(
                cell.cell_id, stage, State.RUNNING,
                rates=sorted({p.rate for p in rec.probes}),
            )
        _execute_block_probes(ctx, cell, stage, duration_s=ctx.profile.measure_s)
        _retry_excluded_rungs(ctx, cell, stage)

    bracket_by_slot: dict[str, list[int]] = {}
    crossed_by_slot: dict[str, int | None] = {}
    rungs_summary_by_slot: dict[str, dict[str, float]] = {}
    for sid in ctx.slots:
        rungs = _collect_rungs(rec, sid)
        bracket, crossed = _bracket_crossing(rungs, ctx.slo_ms)
        bracket_by_slot[sid] = bracket
        crossed_by_slot[sid] = bracket[-1] if crossed and bracket else None
        rungs_summary_by_slot[sid] = {str(r): round(p99, 3) for r, p99 in rungs}
    sub_knee = [sid for sid in ctx.slots if crossed_by_slot[sid] is None]

    # A PENDING probe under a DONE ladder means the stop rule fired over an
    # incomplete rung -- the R1-A4 mechanism. `_complete_rates` is what prevents
    # it; this is the assertion that says so out loud rather than trusting it.
    pending = [
        probe_key(p.rate, p.block, p.slot)
        for p in rec.probes if p.state != State.DONE
    ]
    if pending:
        raise PhaseError(
            f"ladder for {cell.cell_id} would finish with {len(pending)} probe(s) "
            f"never run ({', '.join(sorted(pending)[:4])}): the crossing check "
            "must only read rungs whose every planned probe is DONE"
        )

    # The TOP RUNG ACTUALLY OFFERED, not the planned ceiling. The ladder stops
    # early the moment every slot has bracketed its crossing, so `rates[-1]` is
    # a rate no probe ever ran at: an 8-rung plan that stopped at 8,000 rps
    # persisted `rung_ceiling_rps: 64000`. (The two coincide whenever
    # `ceiling_reached_without_crossing` is non-empty -- the loop only breaks
    # when every slot has crossed -- which is why nothing rendered the wrong
    # number, but ledger.json carried it.)
    top_offered = max((p.rate for p in rec.probes), default=rates[-1])
    degraded = _enforce_ladder_crossing(ctx, cell, sub_knee, top_offered)
    ctx.led.mark_stage(
        cell.cell_id, stage, State.DONE,
        bracket_by_slot=bracket_by_slot,
        crossed_by_slot=crossed_by_slot,
        rungs_summary_by_slot=rungs_summary_by_slot,
        rung_ceiling_rps=top_offered,
        rung_ceiling_rungs=ctx.profile.ladder_max_rungs,
        ceiling_reached_without_crossing=sub_knee,
        **degraded,
    )


def _enforce_ladder_crossing(
    ctx: RunContext, cell: Cell, sub_knee: list[str], ceiling_rps: int
) -> dict:
    """Abort a measured run whose slot never crossed, unless opted out.

    The metric is cost AT the rate where p99 crosses 20 ms. A slot that never
    crossed inside the ladder has no such rate, so every number published for it
    is cost at some other rate.

    Same shape as `_enforce_gate`/`_no_gate_record`: smoke rehearses the path and
    publishes nothing, the operator opt-out is an env flag, and taking it stamps
    `degraded` into the artifact so nothing downstream can lose the caveat. The
    stamp is on the LADDER STAGE, not the manifest, because "which slot, in which
    cell" is a per-cell fact and a run-level flag could not carry it.
    """
    if not sub_knee or ctx.profile.functional_only:
        return {}
    if not ctx.allow_sub_knee_ladder:
        raise PhaseError(
            f"{cell.cell_id}: {', '.join(sub_knee)} never crossed the "
            f"{ctx.slo_ms:g} ms SLO inside the ladder (ceiling "
            f"{ceiling_rps} rps, {ctx.profile.ladder_max_rungs} rungs from "
            f"{ctx.profile.ladder_start_rps} rps). The headline metric is cost AT "
            "the crossing rate, so that slot has no operating point and the cell "
            f"is not a comparison at a fixed latency promise. Raise "
            f"ladder_max_rungs / ladder_start_rps, or set "
            f"{ALLOW_SUB_KNEE_LADDER_ENV}=1 to record the cell anyway (it is then "
            "stamped degraded and every downstream artifact carries it)."
        )
    return {
        "degraded": True,
        "degraded_allowed_by": ALLOW_SUB_KNEE_LADDER_ENV,
        "degraded_detail": (
            f"{', '.join(sub_knee)} reached the {ceiling_rps} rps rung ceiling "
            f"without crossing the {ctx.slo_ms:g} ms SLO; no measured knee, so no "
            "operating point at the latency promise (operator opt-out)"
        ),
    }


def run_fit(ctx: RunContext, cell: Cell) -> None:
    """Knee fit via analysis.knee + per-probe stationarity (analysis.mann_kendall).

    ONE FIT PER SLOT, over that slot's own rungs, into `knee_by_slot` /
    `confirm_rates_by_slot` (D9). The pooled fit is gone: it was what selected a
    shared confirm rate and therefore what made the headline a comparison at one
    stack's operating point (R1-A1).

    NO CROSSING for a slot: no knee to fit, so that slot's operating point is its
    top rung and `no_crossing_slots` names it. With a crossing: feed per-rung p99
    reps into estimate_knee (respecting min_reps/ci_valid) and run the
    stationarity gate per probe.

    CAPACITY-gate-guarded like every other stage entry point (`ledgerbench
    confirm` calls run_fit directly, skipping the gate phase): a fit is a
    published number, so a non-passing capacity gate must stop here too.

    It does NOT call `assert_gate_plan_sound`, and that asymmetry is deliberate:
    that guard refuses a WARMUP gate set no probe could satisfy, and run_fit
    runs no probe -- it refits rungs the ladder already measured under a gate set
    `run_ladder` had already checked.
    """
    stage = "fit"
    _ensure_gate_applied(ctx)
    fit_rec = ctx.led.stage(cell.cell_id, stage)
    if fit_rec.state == State.DONE:
        return
    ladder_rec = ctx.led.stage(cell.cell_id, "ladder")
    bracket_by_slot = ladder_rec.data.get("bracket_by_slot") or {}
    crossed_by_slot = ladder_rec.data.get("crossed_by_slot") or {}
    stat = _stationarity_summary(ctx, ladder_rec)

    knee_by_slot: dict[str, float | None] = {}
    ci_by_slot: dict[str, list[float] | None] = {}
    ci_valid_by_slot: dict[str, bool | None] = {}
    warning_by_slot: dict[str, str | None] = {}
    confirm_rates_by_slot: dict[str, int] = {}
    no_crossing: list[str] = []

    for sid in ctx.slots:
        if not crossed_by_slot.get(sid):
            # No crossing for this slot (smoke, or the opted-out sub-knee cell):
            # there is no knee to fit, so the operating point is the top rung and
            # the artifact says which it is. Never extrapolated.
            no_crossing.append(sid)
            knee_by_slot[sid] = None
            ci_by_slot[sid] = None
            ci_valid_by_slot[sid] = None
            warning_by_slot[sid] = None
            confirm_rates_by_slot[sid] = _top_rate(ladder_rec, sid)
            continue
        rungs = _rungs_by_rate(ladder_rec, sid)
        if len(rungs) < 2:
            # P8 pre-registers this exact case. Before W24 it reached
            # `estimate_knee`'s bare ValueError, which the CLI's PhaseError-only
            # guard let out as a traceback.
            raise PhaseError(
                f"{cell.cell_id}/{sid}: the ladder bracketed a crossing after "
                f"{len(rungs)} rung(s), so there is nothing to interpolate "
                f"between. The start rate is at or above this slot's knee: lower "
                f"ladder_start_rps (currently {ctx.profile.ladder_start_rps} rps) "
                "and rerun the cell."
            )
        knee = estimate_knee_from_rungs(
            rungs, slo_ms=ctx.slo_ms, min_reps=ctx.slo.ladder.confirm_reps,
        )
        knee_by_slot[sid] = knee.knee_rps
        ci_by_slot[sid] = [knee.ci_low_rps, knee.ci_high_rps]
        ci_valid_by_slot[sid] = knee.ci_valid
        warning_by_slot[sid] = knee.warning
        confirm_rates_by_slot[sid] = int(round(knee.knee_rps))

    ctx.led.mark_stage(
        cell.cell_id, stage, State.DONE,
        no_crossing_slots=no_crossing,
        bracket_by_slot=bracket_by_slot,
        knee_by_slot=knee_by_slot,
        ci_by_slot=ci_by_slot,
        ci_valid_by_slot=ci_valid_by_slot,
        warning_by_slot=warning_by_slot,
        confirm_rates_by_slot=confirm_rates_by_slot,
        # WHERE the confirm/soak windows sit, as a declared basis rather than an
        # inference from the numbers: the fitted knee, or `mixed` when a slot
        # had no crossing. The bracket rungs are still measured and remain the
        # cost-vs-rate anchors.
        confirm_rate_basis="fitted_knee" if not no_crossing else "mixed",
        stationarity=stat,
    )


def _confirm_members(ctx: RunContext, cell: Cell) -> tuple[list[tuple[str, int]], int]:
    """(block members, common rate) for the confirm stage.

    Each slot runs at its OWN fitted knee -- the rate the headline metric is
    defined at. The common-rate secondary verdict is `r_common = min` over those
    rates, i.e. the SLOWER slot's own knee, so that slot's own-knee probes ARE
    the common-rate probes and only the faster slot needs an extra one.

    That extra probe sits INSIDE the same block, in the seeded order, not in a
    block set of its own: RCB blocks the nuisance factor by adjacency in time,
    and a probe run an hour later under a different thermal state is not paired
    with anything. Cost is `confirm_blocks` extra probes per cell (10 at full, 2
    at rehearsal) -- against the `confirm_blocks x 2 bracket rates` that moving
    confirm to the knee already removed.
    """
    fit_rec = ctx.led.stage(cell.cell_id, "fit")
    by_slot = {
        sid: int(r) for sid, r in (fit_rec.data.get("confirm_rates_by_slot") or {}).items()
        if sid in ctx.slots
    }
    for sid in ctx.slots:
        by_slot.setdefault(sid, _top_rate(ctx.led.stage(cell.cell_id, "ladder"), sid))
    members = [(sid, by_slot[sid]) for sid in ctx.slots]
    r_common = min(by_slot.values())
    members += [(sid, r_common) for sid in ctx.slots if by_slot[sid] != r_common]
    return members, r_common


def run_confirm(ctx: RunContext, cell: Cell) -> None:
    """confirm_blocks RCB blocks, each slot at ITS OWN fitted knee.

    A block holds one probe per slot at that slot's knee, plus one extra probe
    for every slot whose knee is above the common rate (see `_confirm_members`),
    so both the own-knee headline verdict and the confound-free common-rate
    contrast come out of blocks that were adjacent in time.

    Gate-guarded, capacity and warmup-gate plan alike (the `ledgerbench confirm`
    command enters here directly).
    """
    stage = "confirm"
    assert_gate_plan_sound(ctx, [cell])
    _ensure_gate_applied(ctx)
    rec = ctx.led.stage(cell.cell_id, stage)
    if rec.state == State.DONE:
        return
    members, r_common = _confirm_members(ctx, cell)

    if not rec.probes:
        for p in plan_blocks(
            n_blocks=ctx.profile.confirm_blocks, members=members,
            rng_seed=ctx.led.rng_seed, salt=_salt(cell.cell_id, stage, 0),
        ):
            rec.probes.append(p)
        ctx.led.mark_stage(
            cell.cell_id, stage, State.RUNNING,
            confirm_rates_by_slot={sid: rate for sid, rate in members[:len(ctx.slots)]},
            common_rate_rps=r_common,
        )
    else:
        ctx.led.reset_orphan_blocks(cell.cell_id, stage)

    _execute_block_probes(ctx, cell, stage, duration_s=ctx.profile.measure_s)
    ctx.led.mark_stage(cell.cell_id, stage, State.DONE)


def run_soak(ctx: RunContext, cell: Cell) -> None:
    """Soak: one long attack per slot at THAT SLOT'S confirm rate + stationarity.

    Linux additionally records the cgroup anon-slope (the LiveProbeRunner pulls
    mem_anon CSV and computes the slope). Soak is a single RCB block.

    NOTE the change of meaning W24 makes: `rates[0]` used to be the LOWER bracket
    rung, so the soak measured drift comfortably UNDER the promise. It now runs
    at each slot's fitted knee, i.e. drift AT the promise. That is the drift a
    reader cares about, and it is stated in METHODOLOGY rather than slipped in.

    Gate-guarded, capacity and warmup-gate plan alike (the `ledgerbench soak`
    command enters here directly).
    """
    stage = "soak"
    assert_gate_plan_sound(ctx, [cell])
    _ensure_gate_applied(ctx)
    rec = ctx.led.stage(cell.cell_id, stage)
    if rec.state == State.DONE:
        return
    members, _ = _confirm_members(ctx, cell)
    soak_members = members[:len(ctx.slots)]

    if not rec.probes:
        for p in plan_blocks(
            n_blocks=1, members=soak_members,
            rng_seed=ctx.led.rng_seed, salt=_salt(cell.cell_id, stage, 0),
        ):
            rec.probes.append(p)
        ctx.led.mark_stage(
            cell.cell_id, stage, State.RUNNING,
            soak_rates_by_slot=dict(soak_members),
        )
    else:
        ctx.led.reset_orphan_blocks(cell.cell_id, stage)

    _execute_block_probes(ctx, cell, stage, duration_s=ctx.profile.soak_s)

    stat = _stationarity_summary(ctx, rec)
    ctx.led.mark_stage(cell.cell_id, stage, State.DONE, stationarity=stat)


def _reset_rung_probes(led: Ledger, cell_id: str, stage: str, rate: int) -> None:
    """Flip a whole rung back to pending and drop its recorded per-probe data.

    Whole-rung, not per-slot, even when only one slot emptied it: the rung's
    probes are RCB partners and re-running one of a pair leaves the survivor
    measured under different ambient conditions. `drop_probe_data` is shared with
    `ledger.reset_orphan_blocks` so both undo the same set of maps.
    """
    rec = led.stage(cell_id, stage)
    for p in rec.probes:
        if p.rate == rate:
            drop_probe_data(rec, p)
            p.reset_to_pending()
    led.save()


def _retry_excluded_rungs(ctx: RunContext, cell: Cell, stage: str) -> None:
    """One-shot retry of any rung emptied by under-warm/gate exclusion.

    PER SLOT since W24: a rung with every probe of ONE slot excluded is a gap in
    THAT slot's ladder, and the pooled test ("no warm probe from either slot")
    never retried it at all -- `analyze._knee_for_slot` just dropped the rung
    into `n_rungs_dropped`. The one-shot guard is keyed by (slot, rate) so the
    retry one slot's gap consumed cannot be charged to the other's.

    The retry resets the rung WHOLE (RCB pair integrity: re-running one member of
    a block leaves its partner measured under different ambient conditions) and
    re-runs it once.

    AFTER the retry the two outcomes are deliberately different. A rung that
    still has no warm probe from ANY slot that ran there produced no measurement
    at all: raise, because the ladder would otherwise leave a hole DONE in the
    ledger, the fit would raise, and every resume would raise again on the same
    baked-in flags -- an unrecoverable loop. A rung emptied for one slot only is
    RECORDED and dropped from that slot's fit; aborting the cell for it would
    make the run's completion depend on a per-probe flagging rate that round 1
    measured at 59.5%.
    """
    rec = ctx.led.stage(cell.cell_id, stage)
    retried = set(rec.data.get("rewarmed_rungs", []))

    def _holes() -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for sid in ctx.slots:
            _, empty = _rung_values(rec, sid)
            out += [(sid, rate) for rate in sorted(empty)]
        return sorted(out, key=lambda t: (t[1], t[0]))

    retry_again = bool(os.environ.get(RETRY_EXCLUDED_AGAIN_ENV))
    for sid, rate in _holes():
        tag = f"{sid}@{rate}"
        if tag in retried and not retry_again:
            continue
        if tag in retried:
            # Operator-granted extra round: recorded, so the artifact says how
            # many retries this rung actually consumed.
            again = set(rec.data.get("rewarmed_rungs_again", []))
            again.add(tag)
            rec.data["rewarmed_rungs_again"] = sorted(again)
        retried.add(tag)
        rec.data["rewarmed_rungs"] = sorted(retried)
        _reset_rung_probes(ctx.led, cell.cell_id, stage, rate)
        _execute_block_probes(ctx, cell, stage, duration_s=ctx.profile.measure_s)

    _, unmeasured = _rung_values(rec, None)
    if unmeasured:
        rate = sorted(unmeasured)[0]
        # The one-shot guard is ledger state, so without the env this raise
        # repeats on every --resume. A longer warmup cap is not an option
        # mid-run: the cap lives in spec/slo.yaml inside the frozen
        # config_hash. The honest exits are another operator-granted retry
        # round or a new run.
        raise PhaseError(
            f"all probes at rung {rate} rps were under-warmed/excluded after "
            f"a retry. Set {RETRY_EXCLUDED_AGAIN_ENV}=1 on the next --resume "
            "to grant one more retry round (recorded), or start a NEW run "
            "with a longer warmup cap (the cap is frozen inside this run's "
            "config_hash)"
        )
    dropped = {}
    for sid, rate in _holes():
        dropped.setdefault(sid, []).append(rate)
    if dropped:
        rec.data["dropped_rungs_by_slot"] = {k: sorted(v) for k, v in dropped.items()}
        ctx.led.save()


_COMPOSE_CACHE_TTL_RE = re.compile(
    r"CACHE_TTL_SECONDS:\s*\$\{CACHE_TTL_SECONDS:-(\d+)\}"
)


def _effective_cache_ttl(ctx: RunContext) -> tuple[int | None, str]:
    """(seconds, source) for the cache TTL the SUTs actually ran with.

    Precedence mirrors what compose resolves: a capacity-gate lever override on
    the runner, else the default baked into infra/compose.yaml (ambient env is
    scrubbed by compose and therefore never in force). The value is READ, never
    assumed: if the compose default cannot be parsed the TTL stays null with
    `not_measured` rather than acquiring a plausible number.
    """
    lever_env = getattr(ctx.runner, "env_overrides", {}) or {}
    if "CACHE_TTL_SECONDS" in lever_env:
        try:
            return int(lever_env["CACHE_TTL_SECONDS"]), (
                f"capacity-gate cache_hit_rate lever ({CACHE_TTL_OVERRIDE_ENV})"
            )
        except (TypeError, ValueError):
            return None, f"{NOT_MEASURED}: unparseable lever CACHE_TTL_SECONDS"
    # No ambient-environment branch: compose scrubs CACHE_TTL_SECONDS from every
    # invocation (compose.KNOB_ENV_VARS), so an ambient value NEVER reaches the
    # SUTs: recording it here would put a TTL in the manifest the run didn't use.
    try:
        text = (paths.infra_dir() / "compose.yaml").read_text(encoding="utf-8")
    except OSError:
        return None, f"{NOT_MEASURED}: infra/compose.yaml unreadable"
    m = _COMPOSE_CACHE_TTL_RE.search(text)
    if not m:
        return None, f"{NOT_MEASURED}: no CACHE_TTL_SECONDS default in infra/compose.yaml"
    return int(m.group(1)), "infra/compose.yaml default (no override in force)"


def _record_working_set(ctx: RunContext) -> None:
    """Accumulate the read-path working set from the per-probe draw artifacts.

    Round 1 generated ONE targets file per slot, replayed it, and this function
    counted the distinct ids that file named -- once, from the file. W20 draws a
    fresh stream per attack, so there is no file to count and the quantity is
    per probe: the spread of requests drawn, of distinct ids named, and of
    seconds spent drawing them.

    The source is `cells/**/<slot>.target_draw.json`, not an in-process counter,
    for the reason every W16 aggregate is built from artifacts: a resume is a
    new process, and a counter that restarts publishes a number that is short on
    exactly the runs that crashed.

    The effective cache TTL is recorded next to it (with its source): the whole
    point of the section is residency, and residency is unreadable while the TTL
    is null. So is `prewarm_containment_per_probe`, which is the same section's
    other half: separate streams make the warmup's SEQUENCE independent of the
    measured window's, and this is what remains after that -- the Zipf head both
    streams draw by construction, as a measured fraction rather than a claim.
    """
    from . import vegeta
    from .manifest import Spread, WorkingSet

    cells_dir = paths.run_dir(ctx.run_id) / "cells"
    requests: list[int] = []
    invoices: list[int] = []
    customers: list[int] = []
    seconds: list[float] = []
    containment: list[float] = []
    windows: set[int] = set()
    draw_mode = ""
    formula = ""
    for path in sorted(cells_dir.rglob("*.target_draw.json")):
        doc = _load_json(path)
        measured = (doc or {}).get("measured") or {}
        if not measured:
            continue
        requests.append(int(measured["requests_drawn"]))
        invoices.append(int(measured["distinct_invoice_ids"]))
        customers.append(int(measured["distinct_customer_ids"]))
        seconds.append(float(doc.get("probe_draw_seconds") or 0.0))
        # `.get`, not a hard lookup: for a draw artifact with no
        # containment block, the residency section must degrade to an empty
        # spread rather than take the whole finalization down. A probe whose
        # window drew no invoice-id read at all records `fraction: null` and is
        # correctly absent from the spread rather than entering it as a zero.
        frac = ((doc.get("prewarm_containment") or {}).get("fraction"))
        if frac is not None:
            containment.append(float(frac))
        if doc.get("measured_window_seconds"):
            windows.add(int(doc["measured_window_seconds"]))
        draw_mode = draw_mode or str(doc.get("draw_mode") or "")
        formula = formula or str(doc.get("seed_formula") or "")

    # Hard lookup, not `.get(...) or None`: seed_scale is always a dataset the
    # spec declares (a ScaleProfile that says otherwise is a bug, and the Go
    # seeder would already have refused it), so a miss here is not a case to
    # null-fill -- silently recording `seeded_invoices: null` is how the
    # residency section becomes unreadable without anyone noticing.
    seeded = ctx.workload.seeding.scales[ctx.profile.seed_scale]
    n_create, n_quote = vegeta.body_pool_sizes(
        ctx.workload, ctx.profile.body_pool_requests
    )
    ttl, ttl_source = _effective_cache_ttl(ctx)
    ws = WorkingSet(
        draw_mode=draw_mode or NOT_MEASURED,
        draw_seed_formula=formula,
        requests_drawn_per_probe=Spread.of(requests),
        distinct_invoice_ids_per_probe=Spread.of(invoices),
        distinct_customer_ids_per_probe=Spread.of(customers),
        draw_seconds_per_probe=Spread.of(seconds),
        prewarm_containment_per_probe=Spread.of(containment),
        body_pool_create_bodies=n_create,
        body_pool_quote_bodies=n_quote,
        body_pool_distinct_customer_ids=_pool_distinct_customers(ctx),
        seeded_invoices=seeded.invoices,
        seeded_customers=seeded.customers,
        cache_ttl_seconds=ttl,
        cache_ttl_source=ttl_source,
        # One window length unless a stage ran a different one (soak does), in
        # which case the section states the LONGEST: the residency question is
        # whether an id can survive its own window, and the longest window is
        # the one that answers it least favourably.
        measured_window_seconds=max(windows) if windows else None,
        note=(
            f"per-probe draw over {len(requests)} measured windows; every attack "
            "(each warmup segment, the head-of-window discard, the measured "
            "window) draws its own PCG64 stream, so no attack replays another's "
            "SEQUENCE. That is not the same as no pre-warming: two independent "
            "Zipf draws over one keyspace share the head by construction, and "
            "prewarm_containment_per_probe is the measured request-weighted "
            "share of each window's reads that an earlier attack had already "
            "touched. Counts are the ids the READ path asks for; the write "
            "path additionally inserts invoice ids that appear in no target. "
            f"Create bodies are drawn uniformly from a pool of {n_create} built "
            "from Zipf customer draws, so the create path's customer_id "
            "distribution is that pool's empirical sample rather than a fresh "
            "draw per request -- identical on the head, thinner in the tail. "
            "The read path is URL-only and unaffected"
        )
        if requests else
        f"{NOT_MEASURED}: no probe wrote a <slot>.target_draw.json",
    )
    ctx.manifest.working_set = ws
    ctx.manifest.save()


def _pool_distinct_customers(ctx: RunContext) -> int | None:
    """Distinct customer ids inside the shipped body pool, off the pool itself.

    The pool is a run artifact (`io/pool/pool.meta.json` names its size; the
    bodies beside it carry the ids), so this is measured rather than derived
    from the size: at Zipf alpha=1.0 a 150,000-body pool holds far fewer than
    150,000 distinct customers, and the gap is the disclosure.
    """
    pool_dir = paths.run_dir(ctx.run_id) / "io" / "pool" / "bodies"
    if not pool_dir.is_dir():
        return None
    ids: set[int] = set()
    for path in pool_dir.glob("create_*.json"):
        doc = _load_json(path)
        cid = (doc or {}).get("customer_id")
        if cid is not None:
            ids.add(int(cid))
    return len(ids) or None


def run_cell(ctx: RunContext, cell: Cell) -> None:
    """One headline cell: ladder -> fit -> confirm -> soak (each resumable).

    Starts by re-asserting the capacity gate and the cell's gate plan: the
    gate's effective workload must be in force before a single probe runs, a
    non-passing verdict must still stop the cell even when it is entered
    directly (per-stage CLI entry point or a resume that skipped the gate
    phase), and an unsatisfiable warmup gate set must stop it before any
    runtime is spent. Each stage below re-asserts both (idempotently), so the
    `ladder`/`confirm`/`soak` commands, which never come through here, are
    guarded as well.
    """
    assert_gate_plan_sound(ctx, [cell])
    _ensure_gate_applied(ctx)
    _record_cell_knobs(ctx, cell)
    run_ladder(ctx, cell)
    _record_working_set(ctx)      # the ladder's per-probe draw artifacts exist by now
    run_fit(ctx, cell)
    run_confirm(ctx, cell)
    run_soak(ctx, cell)


def run_cells_phase(ctx: RunContext) -> None:
    # The phase is DONE once every cell has run: finalization is bookkeeping over
    # artifacts already on disk and must never be what decides whether a
    # completed cells phase is recorded (it cannot raise either, but the order
    # makes the guarantee independent of that).
    cells = cells_for(ctx.profile)
    # EVERY cell is checked before the FIRST one runs. Per-cell checking would
    # discover cell 2's unsatisfiable gate set after cell 1 had already cost its
    # hours, which is R1-A3's shape one cell later.
    assert_gate_plan_sound(ctx, cells)
    for index, cell in enumerate(cells):
        run_cell(ctx, cell)
        if index == 0:
            # R1-A6: the headroom question, re-asked at the rates cell 1 turned
            # out to run at. HERE and not before the loop, because the realized
            # rates do not exist until a cell has been fitted and confirmed; and
            # AFTER the first cell rather than after all of them, because its
            # only remaining power is to stop cells 2..N.
            run_capacity_regate_phase(ctx, cell)
    ctx.led.mark_phase("cells", State.DONE, cell_ids=[c.cell_id for c in cells])
    finalize_manifest(ctx)


# --------------------------------------------------------------------------- #
# manifest finalization: aggregate what the probes already wrote to disk
# --------------------------------------------------------------------------- #
# What the cgroup rows actually are, since W16. cpu.stat's nr_throttled and
# throttled_usec are CUMULATIVE over a cgroup's lifetime; the sampler reads them
# at both edges of the measured window and the artifacts carry the increment, so
# a summed row is a sum of increments. Round 1 read only the end and summed
# that, which for the recreated SUT silently folded in warmup and the
# head-of-window discard, and for a cgroup that persists across probes would
# have multiplied one lifetime total by the window count.
THROTTLE_SOURCE = (
    "cpu.stat nr_throttled/throttled_usec, end-minus-start across the MEASURED "
    "window only (the baseline is taken after warmup and after the "
    "head-of-window discard), summed over the run's probe windows"
)
RESOURCE_SOURCE = (
    "cgroup v2 per-probe windows: CPU is a sum of measured-window deltas; the "
    "anon and memory.current figures are maxes of per-window in-window p99s; "
    "memory.peak is the kernel HIGH-WATER MARK over the cgroup's LIFETIME (one "
    "probe for the force-recreated SUT, the whole run for a persistent cgroup) "
    "and is not a window figure"
)
LOAD_GENERATOR_SOURCE = (
    "cgroup v2 measured-window CPU deltas for the load generator and the "
    "container runtime. Published for single-host accounting and EXCLUDED from "
    "every per-request cost: this is the apparatus, not the system under test"
)
# Aggregation scope, stated because two artifacts publish similar-looking
# numbers over different window sets. These rows cover EVERY window under
# cells/ -- all stages, all rates, flagged or not. The report's per-request
# figures cover confirm+soak, at or below the knee, measurement-grade only.
AGGREGATE_SCOPE = (
    "over every probe window under cells/ (all stages, all rates, including "
    "windows the analysis excludes as under-warmed); the report's per-request "
    "figures use a narrower set and the two are not interchangeable"
)


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def finalize_manifest(ctx: RunContext) -> None:
    """Fill the manifest's aggregate sections; NEVER raise.

    Finalization is bookkeeping over artifacts a completed run already wrote, so
    a malformed artifact must not cost the run: the aggregation is wrapped and
    any failure is recorded in `manifest.finalization_error` (visible, not
    swallowed) instead of propagating out of the cells phase.
    """
    try:
        _finalize_manifest(ctx)
    except Exception as exc:                    # noqa: BLE001 - see docstring
        ctx.manifest.finalization_error = f"{type(exc).__name__}: {exc}"
        try:
            ctx.manifest.save()
        except Exception:                       # noqa: BLE001, S110
            pass                                # nothing left to do but not lose the run


def cpuset_env_by_role(env: dict[str, str]) -> dict[str, str]:
    """The cpuset infra/compose.bench.yaml asked for, per sampled cgroup ROLE.

    Keyed by role, never by container name: the sampled set includes host
    systemd units, and the roles are what the sampler already records. The
    container-runtime role has no entry -- dockerd and containerd are pinned by
    the systemd drop-in, not by a compose cpuset.
    """
    from . import cgroups

    defaults = {
        cgroups.ROLE_SUT: ("SUT_CPUS", "0-5"),
        cgroups.ROLE_DATABASE: ("PG_CPUS", capgate.COMPOSE_CPUSET_DEFAULTS["PG_CPUS"]),
        cgroups.ROLE_CACHE: ("REDIS_CPUS", "14"),
        cgroups.ROLE_LOAD_GENERATOR: (
            "VEGETA_CPUS", capgate.COMPOSE_CPUSET_DEFAULTS["VEGETA_CPUS"]),
    }
    return {role: (env.get(var) or fallback) for role, (var, fallback) in defaults.items()}


_SIZE_MULTIPLIERS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}


def parse_compose_size(raw: str) -> int | None:
    """A compose `mem_limit` string -> bytes, or None when it is not one.

    docker's size suffixes are BINARY, so `12g` is the 12884901888 that
    `memory.max` reads back; comparing against 12e9 would fail every window.
    """
    text = (raw or "").strip().lower()
    if text.endswith("b"):
        text = text[:-1]
    mult = 1
    if text and text[-1] in _SIZE_MULTIPLIERS:
        mult, text = _SIZE_MULTIPLIERS[text[-1]], text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return None


def mem_limit_env_by_role(env: dict[str, str]) -> dict[str, int]:
    """The `mem_limit` infra/compose.bench.yaml asked for, in BYTES, per role.

    The companion of `cpuset_env_by_role`, with the same key and the same
    omission: the container-runtime units carry no compose memory limit, so they
    get no entry and are not checked. The literals mirror the overlay's own
    `${VAR:-default}` defaults, which
    `test_the_expected_memory_ceilings_match_the_compose_overlay` pins.

    RAISES on an env value that is not a compose size, rather than dropping the
    role: `MultiSampler.limit_mismatches` skips a role with no expected ceiling,
    so an omitted role is a first-window ceiling gate that fails OPEN on exactly
    the container whose budget nobody can name. `checksums.published_rels` raises
    on a scope it cannot establish for the same reason.
    """
    from . import cgroups

    defaults = {
        cgroups.ROLE_SUT: ("SUT_MEM", "12g"),
        cgroups.ROLE_DATABASE: ("PG_MEM", "8g"),
        cgroups.ROLE_CACHE: ("REDIS_MEM", "2g"),
        cgroups.ROLE_LOAD_GENERATOR: ("VEGETA_MEM", "4g"),
    }
    out: dict[str, int] = {}
    for role, (var, fallback) in defaults.items():
        raw = env.get(var) or fallback
        want = parse_compose_size(raw)
        if not want:
            raise ValueError(
                f"{var}={raw!r} is not a compose mem_limit size, so the memory "
                f"ceiling expected of the {role} container cannot be stated and "
                f"its first-window ceiling check would not run"
            )
        out[role] = want
    return out


def _stamp_topology(manifest: RunManifest, per_label: dict[str, dict], *, env: dict[str, str]) -> None:
    """B6 row 10: the containers' OWN placement and ceilings into the manifest.

    The evidence was already on disk and unaggregated: the sampler reads
    `cpuset.cpus.effective` / `memory.max` / `memory.swap.max` at every window
    start (no container exists at a phase boundary -- the SUT is recreated per
    probe), and every per-window summary carried them while `topology` still
    said `not_measured`.

    A cpuset that does not match what the overlay asked for is RECORDED here,
    never fatal: raising at finalization would destroy a 15 h cell to report
    something every window artifact already says. It should not reach here at
    all -- `cgroups.MultiSampler.cpuset_mismatches` raises on the run's first
    measured window, which is the cheap place. This row is the backstop for a
    cpuset that moved after that check, and `analyze` reports it.
    """
    from .manifest import ContainerTopology

    expected_by_role = cpuset_env_by_role(env)
    rows: dict[str, ContainerTopology] = {}
    for label, agg in sorted(per_label.items()):
        if agg["cpuset"] is None and agg["mem_limit_bytes"] is None:
            continue
        expected = expected_by_role.get(agg["role"])
        effective = agg["cpuset"]
        matches = None
        if expected and effective:
            matches = set(parse_cpuset(effective)) == set(parse_cpuset(expected))
        rows[label] = ContainerTopology(
            cpuset_effective=effective,
            mem_limit_bytes=agg["mem_limit_bytes"],
            mem_swap_max=agg["mem_swap_max"],
            swap_current_max_bytes=agg["swap_current_max"],
            swap_peak_max_bytes=agg["swap_peak_max"],
            expected_cpuset=expected,
            matches=matches,
            windows=agg["windows"],
        )
    if not rows:
        return
    manifest.topology.containers = rows
    manifest.topology.source = "measured"
    manifest.topology.note = (
        "cpuset.cpus.effective / memory.max / memory.swap.max read off each "
        "sampled cgroup at window start; the LAST window's values, with the "
        "cpuset the compose overlay asked for beside them. "
        "swap_current_max_bytes is the OUTCOME: the largest memory.swap.current "
        "seen at any window edge, over ALL windows, and swap_peak_max_bytes is "
        "the kernel's own high-water mark beside it, which is what catches a "
        "cgroup that swapped out and faulted back between two edges"
    )


def _effective_cpuset_env(ctx: RunContext) -> dict[str, str]:
    """The process env, with the capacity gate's realized compose overrides on top.

    The `pg_cores` lever moves cores between PG and vegeta by exporting
    PG_CPUS/VEGETA_CPUS onto the runner, so reading os.environ alone would
    compare the containers against a topology the run stopped using.
    """
    env = dict(os.environ)
    overrides = getattr(ctx.runner, "env_overrides", None)
    if isinstance(overrides, dict):
        env.update({str(k): str(v) for k, v in overrides.items()})
    return env


def _read_pg_settings(ctx: RunContext) -> str | None:
    """pg_settings as TSV while PostgreSQL is still up, or None.

    Read in finalization because that is the only phase boundary where the DB
    is running: it comes up in the seed phase and host_restore does not stop it.
    A run whose seed phase never completed has no PostgreSQL to ask, so it
    records nothing rather than an empty echo. Evidence, never a gate -- the
    declared values live in spec/postgres/postgresql.conf.
    """
    if not ctx.led.phase_done("seed") or not compose.docker_available():
        return None
    env = compose.ComposeEnv.base()
    res = compose.exec_in(
        env, compose.SVC_POSTGRES,
        ["psql", "-U", "postgres", "-d", "ledgerline", "-At", "-F", "\t", "-c",
         "SELECT name,setting,unit,source,boot_val,reset_val FROM pg_settings "
         "ORDER BY name"],
        check=False,
    )
    return res.stdout if res.ok and res.stdout.strip() else None


def _stamp_postgres_config(manifest: RunManifest, tsv: str | None) -> None:
    """Parse a pg_settings dump into the manifest, or leave the marker alone."""
    if not tsv:
        return
    values = hostread.parse_pg_settings(tsv)
    if not values:
        return
    manifest.postgres_config.values = values
    manifest.postgres_config.source = "measured"
    manifest.postgres_config.note = (
        "SELECT name,setting,unit,source,boot_val,reset_val FROM pg_settings, "
        "read at finalization while PostgreSQL was still up"
    )


def _cache_keyspace_deltas(root: Path) -> tuple[int, int, int]:
    """(hits, misses, windows) summed over every `cache_stats_*.json` under `root`.

    Scoped by the caller's directory rather than fixed at the cells tree: the
    manifest's `cache_stats` covers the whole run, while the capacity re-gate
    needs the hit rate MEASURED BY THE FIRST CELL ALONE -- it runs before the
    later cells exist, and the number it feeds into the realized-rate projection
    has to describe the windows it is re-gating.
    """
    hits = misses = windows = 0
    for path in sorted(root.rglob("cache_stats_*.json")):
        doc = _load_json(path)
        if not doc or doc.get("keyspace_hits") is None:
            continue
        hits += int(doc["keyspace_hits"])
        misses += int(doc.get("keyspace_misses") or 0)
        windows += 1
    return hits, misses, windows


def _finalize_manifest(ctx: RunContext) -> None:
    """Aggregate the per-probe artifacts into the manifest's summary sections.

    Everything here already exists on disk after a run, and an empty section
    must not read as "measured, all zero".
    Sources:
      - cells/**/cpu_mem_<lang>.summary.json -> throttle / resource / fault rows
      - cells/**/<lang>.summary.json         -> vegeta status codes -> errors
      - cells/**/cache_stats_<lang>.json     -> redis keyspace hits/misses
    Sections with no data on disk say so: the row-level `source` stays
    "not_measured", AND the section itself is named in `aggregate_provenance`
    (an empty list has no rows to carry a marker).
    """
    from . import cgroups
    from .manifest import (
        CacheStats, Errors, FaultCounts, LoadGeneratorOverhead,
        ResourceDelta, ThrottleCounters,
    )

    cells_dir = paths.run_dir(ctx.run_id) / "cells"
    per_label: dict[str, dict] = {}
    for path in sorted(cells_dir.rglob("cpu_mem_*.summary.json")):
        doc = _load_json(path)
        if not doc or not doc.get("enabled"):
            continue
        # Key on the LABEL the sampler recorded, never on the container name and
        # never on the file name. A systemd unit has no container; `container`
        # could be an explicit null, and `doc.get(k, default)` does not default
        # on an explicit null, so round 1's form would have published a row
        # literally named "None". The file-name fallback exists only for a
        # pre-W16 artifact left in a resumed tree, where the name IS the slot.
        label = str(doc.get("label") or "").strip() or (
            path.name.removeprefix("cpu_mem_").removesuffix(".summary.json")
        )
        agg = per_label.setdefault(label, {
            "windows": 0, "role": "", "persistent": False,
            "cpu": None, "cpu_windows": 0,
            "nr_throttled": None, "throttled_usec": None, "throttle_windows": 0,
            "anon_p99": None, "current_p99": None, "peak": None, "peak_start": None,
            "major": None, "minor": None, "fault_windows": 0,
            "legacy_windows": 0,
            "cpuset": None, "mem_limit_bytes": None, "mem_swap_max": None,
            "swap_current_max": None, "swap_peak_max": None,
        })
        agg["windows"] += 1
        # Container ground truth (B6 row 10). The LAST window wins: a cpuset
        # that changed mid-run did so because the capacity gate's pg_cores lever
        # moved it, and the value the cells ran under is the later one.
        for key in ("cpuset_effective", "mem_limit_bytes", "mem_swap_max"):
            val = doc.get(key)
            if val not in (None, ""):
                agg["cpuset" if key == "cpuset_effective" else key] = val
        # Swap is the OUTCOME beside those SETTINGS, and it takes the MAX over
        # windows rather than the last one: one window that swapped poisons the
        # cell's anon figures whether or not the final window did. Both halves
        # of the evidence aggregate the same way -- the level at the window
        # edges, and the lifetime high-water mark that is the only thing left
        # when a window swapped out and faulted back inside itself.
        for key in ("swap_current_max", "swap_peak_max"):
            swap_seen = doc.get(key)
            if swap_seen is not None:
                agg[key] = (
                    int(swap_seen) if agg[key] is None
                    else max(agg[key], int(swap_seen))
                )
        agg["role"] = agg["role"] or str(doc.get("role") or "")
        agg["persistent"] = agg["persistent"] or bool(doc.get("persistent"))

        # Sum DELTAS, and never coerce a missing key to zero: `or 0` here is
        # exactly how an unmeasured window becomes a measured zero. A window
        # that carries no delta keys at all is a pre-W16 artifact in a resumed
        # tree; it is counted so the provenance can name it instead of letting
        # it dilute a sum silently.
        if doc.get("nr_throttled_delta") is None and doc.get("cpu_usage_delta_usec") is None:
            agg["legacy_windows"] += 1
        for src, dst, wkey in (
            ("cpu_usage_delta_usec", "cpu", "cpu_windows"),
            ("nr_throttled_delta", "nr_throttled", "throttle_windows"),
            ("throttled_usec_delta", "throttled_usec", None),
        ):
            val = doc.get(src)
            if val is None:
                continue
            agg[dst] = int(val) + (agg[dst] or 0)
            if wkey:
                agg[wkey] += 1
        for key, field_name in (
            ("anon_p99", "mem_anon_p99"),
            ("current_p99", "mem_current_p99"),
            ("peak", "mem_peak_end"),
        ):
            val = doc.get(field_name)
            if val is not None:
                agg[key] = int(val) if agg[key] is None else max(agg[key], int(val))
        start_peak = doc.get("mem_peak_start")
        if start_peak is not None:
            agg["peak_start"] = (
                int(start_peak) if agg["peak_start"] is None
                else min(agg["peak_start"], int(start_peak))
            )
        maj, minor = doc.get("major_faults_delta"), doc.get("minor_faults_delta")
        if maj is not None:
            agg["major"] = int(maj) + (agg["major"] or 0)
        if minor is not None:
            agg["minor"] = int(minor) + (agg["minor"] or 0)
        # EITHER counter is evidence the window was read. Counting only the
        # major one published a real minor-fault sum beside `windows: 0` and
        # `source: not_measured`, which reads as a fabricated number.
        if maj is not None or minor is not None:
            agg["fault_windows"] += 1

    throttles, deltas, faults, overhead = [], [], [], []
    legacy_total = sum(a["legacy_windows"] for a in per_label.values())
    legacy_note = (
        f"; {legacy_total} window(s) carried no delta keys and contributed "
        "nothing (pre-W16 artifacts in a resumed tree)" if legacy_total else ""
    )
    for label, agg in sorted(per_label.items()):
        # The apparatus is accounted separately. Without this split the rglob
        # above quietly folds vegeta and dockerd into the list a reader sums as
        # the system's cost.
        if agg["role"] in cgroups.OVERHEAD_ROLES:
            overhead.append(LoadGeneratorOverhead(
                label=label, role=agg["role"],
                cpu_usage_usec_delta=agg["cpu"], windows=agg["cpu_windows"],
                source=(f"{LOAD_GENERATOR_SOURCE} {AGGREGATE_SCOPE}{legacy_note}"
                        if agg["cpu_windows"] else NOT_MEASURED),
            ))
            continue
        throttles.append(ThrottleCounters(
            label=label, role=agg["role"],
            nr_throttled=agg["nr_throttled"], throttled_usec=agg["throttled_usec"],
            windows=agg["throttle_windows"],
            source=(f"{THROTTLE_SOURCE} {AGGREGATE_SCOPE}{legacy_note}"
                    if agg["throttle_windows"] else NOT_MEASURED),
        ))
        deltas.append(ResourceDelta(
            label=label, role=agg["role"], persistent=agg["persistent"],
            cpu_usage_usec_delta=agg["cpu"],
            mem_anon_p99_bytes=agg["anon_p99"],
            mem_current_p99_bytes=agg["current_p99"],
            mem_peak_start_bytes=agg["peak_start"], mem_peak_bytes=agg["peak"],
            windows=agg["cpu_windows"],
            source=(f"{RESOURCE_SOURCE} {AGGREGATE_SCOPE}{legacy_note}"
                    if agg["cpu_windows"] else NOT_MEASURED),
        ))
        faults.append(FaultCounts(
            label=label, role=agg["role"],
            major_faults=agg["major"], minor_faults=agg["minor"],
            windows=agg["fault_windows"],
            source=("memory.stat pgmajfault/pgfault deltas over the measured windows"
                    if agg["fault_windows"] else NOT_MEASURED),
        ))

    # errors: vegeta per-probe summaries (status codes are the ground truth).
    total_requests = 0
    status_codes: dict[str, int] = {}
    summaries = 0
    for slot_id in ctx.slots:
        for path in sorted(cells_dir.rglob(f"{slot_id}.summary.json")):
            doc = _load_json(path)
            if not doc:
                continue
            summaries += 1
            total_requests += int(doc.get("requests") or 0)
            for code, count in (doc.get("status_codes") or {}).items():
                status_codes[str(code)] = status_codes.get(str(code), 0) + int(count)
    non_2xx = sum(n for code, n in status_codes.items() if not code.startswith("2"))
    if summaries:
        ctx.manifest.errors = Errors(
            count=non_2xx,
            rate=(non_2xx / total_requests) if total_requests else 0.0,
            total_requests=total_requests, status_codes=dict(sorted(status_codes.items())),
            probe_summaries=summaries,
            source="vegeta per-probe report status_codes over all measured windows",
        )

    # cache: redis INFO keyspace deltas per probe window.
    hits, misses, windows = _cache_keyspace_deltas(cells_dir)
    if windows:
        total = hits + misses
        ctx.manifest.cache_stats = CacheStats(
            keyspace_hits=hits, keyspace_misses=misses,
            hit_rate=(hits / total) if total else None, windows=windows,
            source="redis INFO keyspace_hits/misses deltas over measured windows",
            note=f"target by construction: {ctx.workload.cache_construction.target_hit_rate}",
        )
    else:
        ctx.manifest.cache_stats = CacheStats(
            source=NOT_MEASURED,
            note="no probe recorded a redis INFO sample; NOT a measured zero",
        )

    if throttles:
        ctx.manifest.throttle_counters = throttles
    if deltas:
        ctx.manifest.resource_deltas = deltas
    if faults:
        ctx.manifest.fault_counts = faults
    if overhead:
        ctx.manifest.load_generator_overhead = overhead

    # Section-level provenance: a list that stayed EMPTY has no row to carry a
    # `source`, so it says here why it is empty rather than reading as a
    # measured nothing.
    no_cgroup = (
        f"{NOT_MEASURED}: no cpu_mem_*.summary.json with resource accounting "
        f"enabled under {cells_dir.name}/"
    )
    prov = ctx.manifest.aggregate_provenance
    prov.throttle_counters = (
        f"{THROTTLE_SOURCE} {AGGREGATE_SCOPE}{legacy_note}" if throttles else no_cgroup
    )
    prov.resource_deltas = (
        f"{RESOURCE_SOURCE} {AGGREGATE_SCOPE}{legacy_note}" if deltas else no_cgroup
    )
    prov.load_generator_overhead = (
        f"{LOAD_GENERATOR_SOURCE} {AGGREGATE_SCOPE}{legacy_note}" if overhead
        else no_cgroup
    )
    if not faults:
        prov.fault_counts = no_cgroup
    elif any(f.major_faults is not None for f in faults):
        prov.fault_counts = "memory.stat pgmajfault/pgfault deltas over the measured windows"
    else:
        prov.fault_counts = f"{NOT_MEASURED}: probe artifacts carry no fault reads"
    prov.achieved_vs_offered = (
        "vegeta achieved rate vs offered rate, one row per probe"
        if ctx.manifest.achieved_vs_offered
        else f"{NOT_MEASURED}: no probe recorded an achieved rate"
    )

    _stamp_topology(ctx.manifest, per_label, env=_effective_cpuset_env(ctx))
    _stamp_postgres_config(ctx.manifest, _read_pg_settings(ctx))
    _record_working_set(ctx)
    ctx.manifest.save()


def run_variants_phase(ctx: RunContext) -> None:
    """Variants matrix: declare everything; EXECUTE the requested subset.

    LEDGERBENCH_RUN_VARIANTS=<cell_id,cell_id,...> selects which variant cells
    run this run, through the same run_cell path as the headline cells (full
    ladder -> fit -> confirm -> soak, probe-granular resume). The default is
    declare-only, so a plain run-all never silently spends 15 h per variant.
    The selection is pinned in the ledger on first execution: a resume replays
    the PINNED list, so a changed environment cannot reshape a run in flight.
    """
    if ctx.led.phase_done("variants"):
        return
    requested = _requested_variants(ctx)
    by_id = {c.cell_id: c for c in variant_cells()}
    for cell_id in requested:
        run_cell(ctx, by_id[cell_id])

    declared = {c.cell_id: {"linux_only": c.linux_only,
                            "deferred": c.cell_id not in requested,
                            "env": c.extra_env, "note": c.note}
                for c in variant_cells()}
    ctx.led.mark_phase(
        "variants", State.DONE,
        declared=declared,
        executed=list(requested),
        note=(f"executed {sorted(requested)} via {RUN_VARIANTS_ENV}" if requested
              else "variants declared linux_only + deferred; set "
                   f"{RUN_VARIANTS_ENV} to execute a subset."),
    )


def _requested_variants(ctx: RunContext) -> list[str]:
    """The variant selection, VALIDATED and then pinned on the ledger.

    Probe records for an executing variant land in the ledger under the cell_id,
    so a crashed variant run resumes through run_cell like any headline cell,
    but only if the SELECTION survives the resume. The env var is therefore
    recorded on first read and the recorded value wins from then on. Validation
    happens BEFORE the pin: recording a typo'd selection would re-raise the same
    error on every resume with no way out short of hand-editing ledger.json.
    """
    rec = ctx.led.phase("variants")
    pinned = rec.data.get("requested")
    if pinned is not None:
        raw = os.environ.get(RUN_VARIANTS_ENV, "").strip()
        env_now = [v.strip() for v in raw.split(",") if v.strip()] if raw else []
        if raw and env_now != list(pinned):
            print(
                f"NOTE: run {ctx.run_id} pinned variants={list(pinned)}; "
                f"ignoring the environment's {env_now}.", file=sys.stderr,
            )
        return list(pinned)
    raw = os.environ.get(RUN_VARIANTS_ENV, "").strip()
    requested = [v.strip() for v in raw.split(",") if v.strip()] if raw else []
    by_id = {c.cell_id: c for c in variant_cells()}
    unknown = [v for v in requested if v not in by_id]
    if unknown:
        raise PhaseError(
            f"{RUN_VARIANTS_ENV} names unknown variant cell(s) {unknown}; "
            f"declared: {sorted(by_id)}"
        )
    non_linux = [v for v in requested if by_id[v].linux_only and not IS_LINUX]
    if non_linux:
        raise PhaseError(f"variant cell(s) {non_linux} are linux_only")
    rec.data["requested"] = requested
    ctx.led.save()
    return requested


def run_host_restore_phase(ctx: RunContext) -> None:
    """host_restore: reverse host tuning (Linux). Windows: nothing to restore."""
    if not IS_LINUX:
        ctx.led.mark_phase("host_restore", State.DONE, skipped="non-linux")
        return
    script = paths.infra_dir() / "host-restore.sh"
    res = run(
        _privileged_argv(["bash", str(script)], forward_env=_host_script_env()),
        check=False, timeout=300,
    )
    ctx.led.mark_phase(
        "host_restore", State.DONE if res.ok else State.FAILED, rc=res.returncode,
    )


# --------------------------------------------------------------------------- #
# top-level orchestration
# --------------------------------------------------------------------------- #
class PhaseError(RuntimeError):
    """A phase failed loudly (no silent skip)."""


def _record_git_identity(manifest: RunManifest) -> None:
    """Stamp the run's starting commit once; record later changes, never hide them.

    `make_context` runs on every process start, so an unconditional write would
    replace the sha the run STARTED at with the sha of whatever is checked out at
    resume time; a run measured across a code change would look single-commit.
    The first reading wins; any different reading afterwards is appended to
    `identity.git_sha_changed_during_run`.
    """
    ident = manifest.identity
    sha, dirty = git_identity()
    if ident.git_sha is None:
        ident.git_sha = sha
        ident.git_dirty = dirty
        return
    if sha is None or (sha == ident.git_sha and dirty == ident.git_dirty):
        return
    seen = ident.git_sha_changed_during_run
    if seen and seen[-1].get("git_sha") == sha and seen[-1].get("git_dirty") == dirty:
        return                      # already recorded this state
    seen.append({"at": rfc3339_micros(), "git_sha": sha, "git_dirty": dirty})


def make_context(
    *,
    run_id: str,
    scale: str,
    runner: ProbeRunner | None = None,
    allow_db_imposed_knee: bool | None = None,
    allow_sub_knee_ladder: bool | None = None,
) -> RunContext:
    """Build the run context: load specs, resolve scale, load-or-create ledger.

    `allow_db_imposed_knee` (default False; overridable by the
    LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE env var so the operator has an opt-out
    without a code change) lets a non-passing capacity gate proceed as a
    DEGRADED run; the choice is stamped into the ledger and the manifest.
    """
    wl = load_workload()
    slo = load_slo()
    profile = ScaleProfile.resolve(scale, slo)
    led = Ledger.load_or_create(
        run_id, config_hash=config_hash(), rng_seed=wl.seeding.seed, scale=scale,
        body_pool_requests=profile.body_pool_requests,
        slot_set=registry.active_slot_set().name,
    )
    # The PINNED pool size wins on resume: the body pool is regenerated whenever
    # the effective size differs from the pool meta on disk, so letting a resume
    # shell's LEDGERBENCH_BODY_POOL_REQUESTS through would swap the working set
    # mid-run.
    if (led.body_pool_requests is not None
            and led.body_pool_requests != profile.body_pool_requests):
        print(
            f"NOTE: run {run_id} pinned body_pool_requests={led.body_pool_requests}; "
            f"ignoring the environment's {profile.body_pool_requests}.",
            file=sys.stderr,
        )
        from dataclasses import replace as _dc_replace
        profile = _dc_replace(profile, body_pool_requests=led.body_pool_requests)
    manifest = RunManifest.load(run_id) or new_manifest(run_id, config_hash=led.config_hash)
    manifest.resource_accounting = "enabled" if IS_LINUX else "disabled (non-Linux)"
    # Identity provenance: WHICH COMMIT produced these numbers. Recorded ONCE,
    # at the start of the run:
    # a resume runs from whatever is checked out then, and overwriting the sha
    # would quietly rewrite the provenance of probes already measured. A resume
    # from different code is appended instead, so the change is visible.
    _record_git_identity(manifest)
    # Spec-declared warmup gates the runner does not implement, surfaced at the
    # manifest level as well as per probe.
    manifest.unimplemented_warmup_gates = [
        name for name, enabled in slo.warmup.gates.items()
        if enabled and name not in IMPLEMENTED_GATES
    ]
    manifest.save()
    if runner is None:
        from .live_runner import LiveProbeRunner
        runner = LiveProbeRunner(run_id=run_id, profile=profile, workload=wl, slo=slo)
    allow = (
        _env_flag(ALLOW_DB_IMPOSED_KNEE_ENV)
        if allow_db_imposed_knee is None else bool(allow_db_imposed_knee)
    )
    allow_sub_knee = (
        _env_flag(ALLOW_SUB_KNEE_LADDER_ENV)
        if allow_sub_knee_ladder is None else bool(allow_sub_knee_ladder)
    )
    return RunContext(
        led=led, profile=profile, workload=wl, slo=slo,
        manifest=manifest, runner=runner, run_id=run_id,
        spec_workload=wl, allow_db_imposed_knee=allow,
        allow_sub_knee_ladder=allow_sub_knee,
    )


def run_all(ctx: RunContext) -> None:
    """Execute the full phase pipeline in order, each resumable."""
    # The variant selection is pinned HERE, before any phase runs: not when
    # the variants phase first executes. At full scale that phase starts ~30 h
    # in, and the documented crash recovery is a `run-all --resume` from a
    # shell with no env vars: a late pin would let such a resume
    # reach the variants phase with an empty environment and mark every
    # variant deferred, silently dropping ~15 h cells. Pinning first also
    # fails a typo'd selection in seconds instead of at hour 30.
    _requested_variants(ctx)
    prepare = getattr(ctx.runner, "prepare", None)
    if callable(prepare):
        prepare()
    run_doctor_phase(ctx)
    run_host_setup_phase(ctx)
    run_host_readback_phase(ctx)
    run_seed_phase(ctx)
    run_capacity_gate_phase(ctx)
    run_validation_phase(ctx)
    run_cells_phase(ctx)
    run_variants_phase(ctx)
    finalize_manifest(ctx)      # last word: aggregates every artifact on disk
    run_host_restore_phase(ctx)
    # AFTER host_restore: its mark_phase rewrites ledger.json, and a map written
    # one line earlier would record a hash of a file that no longer matches.
    checksums.write_run_checksums(ctx.run_id)


# --------------------------------------------------------------------------- #
# helpers (rung math, salts, stationarity)
# --------------------------------------------------------------------------- #
def _salt(cell_id: str, stage: str, idx: int) -> int:
    h = 1469598103934665603
    for ch in f"{cell_id}|{stage}|{idx}":
        h = (h ^ ord(ch)) * 1099511628211 & ((1 << 64) - 1)
    return h


def _rung_values(
    rec, slot: str | None = None
) -> tuple[dict[int, list[float]], set[int]]:
    """(measurement-grade p99 values by rate, rates emptied by exclusion).

    `slot` restricts BOTH halves to one slot's probes. Since W24 the ladder
    climbs per slot, so a rung above the faster-degrading stack's knee carries
    that stack's probes only, and the rung math has to be askable one slot at a
    time -- the pooled form is what made the ladder stop for BOTH stacks the
    moment the mixed mean crossed (R1-A1).

    The slot filter is applied BEFORE `seen_rates.add`, and that order is
    load-bearing: `seen_rates - by_rate.keys()` is the "emptied by exclusion"
    set `_rungs_by_rate` raises on and `_retry_excluded_rungs` re-runs. Filtering
    after would report every SOLO rung as emptied for the slot that stopped
    laddering there -- rungs it was never planned at -- and abort the run naming
    under-warming as the cause.

    Excluded from the fit input: under-warmed probes (warmup hard-capped before
    the gates settled) and gate-excluded probes (`excluded` map: error rate over
    slo error_rate_max, or achieved-vs-offered outside the CO tolerance: both
    full-scale only). Never raises; callers decide how to treat empty rungs
    (the ladder retries them once, the fit refuses to fit around the hole).
    """
    p99_by_probe = rec.data.get("p99_ms", {})
    under_warmed = rec.data.get("under_warmed", {})
    excluded = rec.data.get("excluded", {})
    seen_rates: set[int] = set()
    by_rate: dict[int, list[float]] = {}
    for key, p99 in p99_by_probe.items():
        parsed = split_probe_key(key)
        if parsed is None:
            continue
        rate, _block, key_slot = parsed
        if slot is not None and key_slot != slot:
            continue
        seen_rates.add(rate)
        if under_warmed.get(key) or key in excluded:
            continue
        by_rate.setdefault(rate, []).append(float(p99))
    return by_rate, {r for r in seen_rates if r not in by_rate}


def _complete_rates(rec, slot: str | None = None) -> set[int]:
    """Rates where EVERY planned probe (of `slot`, if given) is DONE.

    The crossing check reads only these. Resume bug: `reset_orphan_blocks` resets
    a block with 0 < done < members, so a rung whose blocks 0-1 are fully DONE
    and whose block 2 is fully PENDING is left alone and looks complete to
    `_rung_values`. Its mean is then taken over 4 probes instead of 6; if THAT
    partial mean crosses, the ladder breaks, block 2 stays PENDING, and the stage
    is marked DONE anyway -- a plausible mechanism for round 1's crossing bracket
    carrying "as few as one measurement-grade rep" (R1-A4).
    """
    planned: dict[int, list[ProbeRecord]] = {}
    for p in rec.probes:
        if slot is not None and p.slot != slot:
            continue
        planned.setdefault(p.rate, []).append(p)
    return {
        rate for rate, probes in planned.items()
        if all(p.state == State.DONE for p in probes)
    }


def _rungs_by_rate(rec, slot: str | None = None) -> dict[int, list[float]]:
    """Per-rung list of per-probe REPORT p99 (ms) for the SLO crossing + fit.

    The SLO is defined on vegeta's report p99 -- ONE number per probe, over the
    whole measured window -- so that is what feeds the isotonic fit and rate
    bracketing. The per-second p99 series is NOT used here (it backs the
    stationarity/warmup gates only).

    RAISES on a rung that produced NO measurement-grade probe from any slot that
    ran there: that rung was offered and yielded nothing, and fitting around it
    silently is the hole this guard exists for.

    It does NOT raise on a rung emptied for ONE slot while another kept a warm
    probe. That is a real gap in one slot's ladder, but it is a gap the fit can
    report rather than die on: `_retry_excluded_rungs` has already re-run the
    rung once, `run_fit` records what survived in `dropped_rungs_by_slot`, and
    `analyze._knee_for_slot` counts it in `n_rungs_dropped`. Raising instead
    would abort a 15-hour cell on the flagging rate R1-A2 measured at 59.5% --
    with 3 reps a rung, a per-slot hole is a p^3 event per rung per slot, so at
    round 1's rate roughly one rung in five. A hole that is recorded is not the
    silent hole this guard was written against.
    """
    by_rate, _ = _rung_values(rec, slot)
    _, unmeasured = _rung_values(rec, None)
    if unmeasured:
        rate = sorted(unmeasured)[0]
        raise PhaseError(
            f"all probes at rung {rate} rps were under-warmed/excluded; "
            "cannot fit around the hole (start a NEW run with a longer warmup cap; "
            "the cap is frozen inside this run's config_hash)"
        )
    return by_rate


def _collect_rungs(rec, slot: str | None = None) -> list[tuple[int, float]]:
    """Mean report-p99 per rung (warm probes only). Ascending (rate, mean_p99_ms).

    Feeds the SLO crossing predicate. Same per-probe report p99 the fit consumes,
    averaged across the warm reps at each rung.
    """
    by_rate = _rungs_by_rate(rec, slot)
    if not by_rate:
        # nothing measured (e.g. no probe done yet) -> empty
        return []
    out = [(rate, sum(v) / len(v)) for rate, v in by_rate.items()]
    out.sort(key=lambda t: t[0])
    return out


def _bracket_crossing(rungs: list[tuple[int, float]], slo_ms: float) -> tuple[list[int], bool]:
    """Bracket the SLO crossing. No crossing -> ([top rung], False).

    A THIN CALLER of `analysis.knee.crosses`, which is the single declared
    crossing predicate. Before W24 this scanned the raw rung means for the first
    `p99 >= slo_ms` while `analyze` published an isotonic fit of the same rungs:
    two rules, agreeing only while the sequence is monotone. The ladder's stop
    rule is now literally the fit the report prints.
    """
    if not rungs:
        return [], False
    point, bracket = knee_crosses([(float(r), float(p)) for r, p in rungs], slo_ms)
    return [int(round(b)) for b in bracket], point is not None


def _top_rate(rec, slot: str | None = None) -> int:
    rungs = _collect_rungs(rec, slot)
    if rungs:
        return rungs[-1][0]
    rates = rec.data.get("rates") or []
    return int(rates[-1]) if rates else 0


def _stationarity_summary(ctx: RunContext, rec) -> dict:
    """Per-probe stationarity gate over each probe's per-second p99 (analysis)."""
    series = rec.data.get("p99_series", {})
    st_cfg = ctx.slo.stationarity or {}
    min_slope = st_cfg.get("min_slope_ms_per_min", 1.0)
    alpha = st_cfg.get("alpha", 0.05)
    results: dict[str, bool] = {}
    for key, vals in series.items():
        if len(vals) >= 3:
            results[key] = stationarity_fails(
                vals, min_slope_ms_per_min=float(min_slope), alpha=float(alpha),
            )
    any_fail = any(results.values())
    return {"per_probe_fails": results, "any_fail": any_fail,
            "min_slope_ms_per_min": float(min_slope), "alpha": float(alpha)}
