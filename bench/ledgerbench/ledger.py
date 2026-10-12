"""Resumable run-ledger: a single pydantic-validated JSON file.

`results/<run_id>/ledger.json`. Append-safe via write-temp-rename (util.paths).
Not a DB: human-readable, git-diffable, probe-granular resume. On start the
runner loads the ledger and skips any phase/cell/probe whose state == "done";
re-runs running/pending. A probe is `done` only after its artifacts are flushed
and checksummed (the runner enforces that ordering; this module is the schema +
the resume helpers).
"""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import registry
from .util import paths
from .util.clock import rfc3339_micros

# v2 (P1/W5): `ProbeRecord.lang: Literal["go","dotnet"]` became `slot: str`
# validated against the registry, `slot_order` became non-defaulting, and the
# compared pair is pinned as `Ledger.slot_set`. The version is now READ on load
# (it was written and never checked), because a v1 file cannot be validated into
# a v2 model at all and the honest answer is a named refusal, not a fresh
# ledger written over a partially-measured run.
#
# v3 (P1/W23-W24): `ProbeRecord` gained `gates_evaluated`, `gates_failed` and
# `warmup_attack_failed` -- the per-probe warmup verdict that `analyze` now
# reads the LEDGER for rather than the sidecars. Unlike v2, this bump is a
# POLICY choice and not a shape necessity: all three fields default, so a v2
# file would validate into this model and DEGRADE QUIETLY instead of failing --
# every window would fall back to its own `<slot>.warmup.json` for the
# failed-gate names, and every probe whose warmup attack crashed would read as
# `warmup_attack_failed: false`. A ledger whose probes cannot answer the
# question this build asks them is a fresh run, not a resumed one, so the
# version check refuses it by name instead.
#
# v4 (P1/W20): `Ledger.target_count` -- the size of the ONE targets file a run
# generated and replayed -- became `body_pool_requests`, the size of the request
# BODY POOL that per-probe draws reference. The rename is the point: the two are
# different quantities, and a v3 ledger carrying 1_000_000 under the old name
# would resume into this build as a pool floor, silently swapping the working
# set mid-run -- the exact failure the pinning was added to prevent. Nothing
# about the field's TYPE forces a refusal, so the version check is what refuses.
# v5 (P1/W24): the ladder climbs PER SLOT, so `ladder.data`'s flat `bracket` /
# `crossed` / `rungs_summary` became `bracket_by_slot` / `crossed_by_slot` /
# `rungs_summary_by_slot`, the fit's `confirm_rates` became
# `confirm_rates_by_slot`, and `ProbeRecord` gained `order_index` (a block can
# now hold one slot twice, at its own knee and at the common rate, and
# `slot_order` alone cannot say which of the two ran first). A v4 ladder read by
# this build would present `crossed_by_slot` as absent -- i.e. "no slot crossed"
# -- for a run that did cross, so the version check is what refuses it.
LEDGER_SCHEMA_VERSION = 5


class LedgerSchemaError(RuntimeError):
    """A results/<id>/ledger.json this build cannot read.

    Round 1's loaders swallowed every parse failure with a bare
    `except Exception: continue`, so a ledger the code could not validate simply
    VANISHED from `latest_unfinished()` and `--resume` reported "no unfinished
    run to resume" -- for what was actually a schema problem. Every load path
    now surfaces the reason.
    """


# --------------------------------------------------------------------------- #
# per-probe key: the single definition
# --------------------------------------------------------------------------- #
# The stage `data` maps (p99_ms, under_warmed, p99_series, excluded) are keyed by
# this string. It used to be built by an f-string in phases.py and parsed by a
# private helper in analyze.py -- two definitions of one wire format, which is
# how a writer and a reader drift apart without a test noticing.
def probe_key(rate: int, block: int, slot: str) -> str:
    return f"r{rate}.b{block}.{slot}"


def split_probe_key(key: str) -> tuple[int, int, str] | None:
    """`r<rate>.b<block>.<slot>` -> (rate, block, slot); None if unparseable.

    Slot ids carry no dot (enforced by `registry.Slot.__post_init__`), so a
    three-way split is unambiguous.
    """
    try:
        rate_s, block_s, slot = key.split(".")
        return int(rate_s[1:]), int(block_s[1:]), slot
    except (ValueError, IndexError):
        return None


# The stage `data` maps a completed probe writes. ONE list, because two callers
# undo a probe (`reset_orphan_blocks` for a block split by a crash,
# `phases._reset_rung_probes` for a rung emptied by exclusion) and a fifth map
# added later must not be dropped by only one of them -- the same argument that
# made `ProbeRecord.reset_to_pending` a single definition.
PROBE_DATA_MAPS = ("p99_ms", "under_warmed", "p99_series", "excluded")


def drop_probe_data(rec, probe) -> None:
    """Forget everything a previous run of `probe` recorded in the stage data.

    `reset_to_pending` clears the PROBE record; this clears the stage-level maps
    keyed by that probe. Without it a reset probe reads PENDING while its old
    p99 still sits in `rec.data["p99_ms"]`, so `_rung_values` fits a number
    belonging to a probe the ledger says never ran. Self-healing only if the
    re-run happens -- and a resume that aborts before it is exactly the case
    that matters.
    """
    key = probe_key(probe.rate, probe.block, probe.slot)
    for name in PROBE_DATA_MAPS:
        d = rec.data.get(name)
        if isinstance(d, dict):
            d.pop(key, None)


class State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PhaseRecord(BaseModel):
    state: State = State.PENDING
    ts: str | None = None
    # Free-form per-phase payload (e.g. dump_sha256, levers_fired, verdict).
    data: dict = Field(default_factory=dict)


class ProbeRecord(BaseModel):
    block: int
    # The within-block RCB order this probe belongs to, e.g.
    # ["dotnet-ado","dotnet-efcore"]. NON-DEFAULTING on purpose: an empty
    # default meant `_execute_block_probes` fell back to a hard-coded
    # ("go","dotnet") literal, so a probe whose order was never recorded still
    # executed -- in an order nothing in the artifact could justify.
    slot_order: list[str]
    slot: str
    rate: int
    state: State = State.PENDING
    artifact: str | None = None         # relative path to the .bin
    achieved: float | None = None
    error_rate: float | None = None
    ts: str | None = None
    # The warmup gate verdict for THIS probe (v3). `gates_evaluated` is the set
    # the probe was actually held to -- posture waivers and unimplemented gates
    # are excluded, because they were recorded but not scored -- and
    # `gates_failed` is the subset that flagged it. The stage's `under_warmed`
    # map is the same verdict as a bool.
    gates_evaluated: list[str] = Field(default_factory=list)
    gates_failed: list[str] = Field(default_factory=list)
    # The OTHER cause of that bool, and not a gate: the warmup ATTACK itself
    # died, leaving the series the gates would have been judged on unwritten so
    # they can pass vacuously. Persisted because without it the ledger can
    # record `under_warmed: true` beside `gates_failed: []` and name no cause at
    # all -- the reader is then left to blame a missing artifact for something
    # that is right here. `warmup.WarmupResult.exit_reason` carries the return
    # code; this bool is only the fact that it happened.
    warmup_attack_failed: bool = False
    # The MEASURED window's PCG64 stream selector, hex (v4/W20). Round 1's
    # request sequence was `PCG64(seed)` from position zero for every attack of
    # every probe, so the seed in the manifest identified it; a per-probe draw
    # needs the increment too or the ledger cannot say which of 600 sequences a
    # probe faced. None on a probe that has not run.
    draw_inc: str | None = None
    # Seconds this probe spent generating targets, over every attack it ran.
    draw_seconds: float | None = None
    # This probe's POSITION in `slot_order` (v5). `slot_order.index(slot)` was
    # the position until a block could hold one slot twice -- the common-rate
    # verdict puts the faster slot in its own block at both its own knee and the
    # slower slot's, and `.index` returns the same answer for both, so the
    # execution order of the pair would be decided by list order rather than by
    # the seeded draw. None on records planned before this field existed.
    order_index: int | None = None

    @field_validator("slot")
    @classmethod
    def _slot_is_registered(cls, v: str) -> str:
        try:
            registry.get_slot(v)
        except KeyError as exc:
            raise ValueError(str(exc.args[0])) from None
        return v

    @field_validator("slot_order")
    @classmethod
    def _order_is_registered(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("slot_order must name the block's slots, never be empty")
        for sid in v:
            try:
                registry.get_slot(sid)
            except KeyError as exc:
                raise ValueError(str(exc.args[0])) from None
        return v

    def reset_to_pending(self) -> None:
        """Drop every field a completed run of this probe wrote.

        ONE definition, because two callers reset probes (`reset_orphan_blocks`
        for a block split by a crash, `_reset_rung_probes` for a rung emptied by
        exclusion) and a field added to the record but to only one of them
        leaves a re-run probe carrying the previous attempt's verdict.
        """
        self.state = State.PENDING
        self.artifact = None
        self.achieved = None
        self.error_rate = None
        self.gates_evaluated = []
        self.gates_failed = []
        self.warmup_attack_failed = False
        self.draw_inc = None
        self.draw_seconds = None
        self.ts = None

    @model_validator(mode="after")
    def _slot_is_in_its_own_order(self) -> "ProbeRecord":
        if self.slot not in self.slot_order:
            raise ValueError(
                f"probe slot {self.slot!r} is not in its block order "
                f"{self.slot_order}: the record cannot say where in the block it ran"
            )
        # A duplicated slot id in one block is legal (own knee + common rate),
        # and it is exactly the case `slot in slot_order` cannot police: the
        # position has to name THIS probe's slot or the record says the block ran
        # in an order it did not.
        if self.order_index is not None:
            if not 0 <= self.order_index < len(self.slot_order):
                raise ValueError(
                    f"order_index {self.order_index} is outside this probe's block "
                    f"order {self.slot_order}"
                )
            if self.slot_order[self.order_index] != self.slot:
                raise ValueError(
                    f"probe slot {self.slot!r} does not sit at order_index "
                    f"{self.order_index} of {self.slot_order}"
                )
        return self


class StageRecord(BaseModel):
    """A ladder/fit/confirm/soak stage within a cell."""

    state: State = State.PENDING
    data: dict = Field(default_factory=dict)
    probes: list[ProbeRecord] = Field(default_factory=list)


class CellRecord(BaseModel):
    cell_id: str
    ladder: StageRecord = Field(default_factory=StageRecord)
    fit: StageRecord = Field(default_factory=StageRecord)
    confirm: StageRecord = Field(default_factory=StageRecord)
    soak: StageRecord = Field(default_factory=StageRecord)


class Ledger(BaseModel):
    run_id: str
    schema_version: int = LEDGER_SCHEMA_VERSION
    created_utc: str = Field(default_factory=rfc3339_micros)
    config_hash: str
    rng_seed: int                       # interleave / within-block order seed
    # The scale profile the run was started with (phases.SCALES). Pinned like
    # config_hash: resuming with a different --scale would silently mix
    # measurement windows (120 s vs 10 s) inside the same rungs. None only on
    # pre-field ledgers (backfilled on first load_or_create).
    scale: str | None = None
    # The BODY POOL size the run STARTED with, in requests
    # (LEDGERBENCH_BODY_POOL_REQUESTS or the profile default; the create/quote
    # split comes from the mix). Pinned like `scale`: the pool is generated once
    # and referenced by every per-probe draw, so an env change on resume would
    # swap the working set half way through a run. The pinned value wins on
    # resume. None only on pre-field ledgers (backfilled).
    body_pool_requests: int | None = None
    # The slot pair this run compares, pinned exactly like `scale`. Without it
    # nothing in the artifacts DECLARES which two SUTs produced the numbers --
    # `analyze` would have to infer the pair from whichever probes happen to
    # exist, and a resume under a different LEDGERBENCH_SLOT_SET would blend two
    # comparisons inside one ledger.
    slot_set: str
    phases: dict[str, PhaseRecord] = Field(default_factory=dict)
    cells: list[CellRecord] = Field(default_factory=list)

    @field_validator("slot_set")
    @classmethod
    def _slot_set_is_registered(cls, v: str) -> str:
        try:
            registry.get_slot_set(v)
        except KeyError as exc:
            raise ValueError(str(exc.args[0])) from None
        return v

    @property
    def slots(self) -> tuple[str, ...]:
        """Canonical slot order for this run. Declared, never inferred."""
        return registry.get_slot_set(self.slot_set).slots

    # ----- persistence ----------------------------------------------------- #
    def path(self) -> Path:
        return paths.run_dir(self.run_id) / "ledger.json"

    def save(self) -> None:
        """Atomically persist (write-temp-rename) so a crash never truncates."""
        paths.write_text_atomic(self.path(), self.model_dump_json(indent=2))

    @classmethod
    def parse_file(cls, path: Path) -> "Ledger":
        """Read one ledger.json, checking the schema version FIRST.

        The version has been written since round 1 and never read. It has to be
        checked before validation, not after: a v1 file cannot be coerced into
        the v2 model at all, so a bare ValidationError would name a missing
        `slot` field rather than the actual problem, which is that this build
        cannot resume that run.
        """
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LedgerSchemaError(f"{path}: not valid JSON ({exc})") from exc
        found = doc.get("schema_version") if isinstance(doc, dict) else None
        if found != LEDGER_SCHEMA_VERSION:
            raise LedgerSchemaError(
                f"{path}: ledger schema v{found!r}, this build reads "
                f"v{LEDGER_SCHEMA_VERSION}. That run cannot be resumed by this "
                f"code; start a fresh run_id (the old ledger is left untouched)."
            )
        try:
            return cls.model_validate(doc)
        except ValidationError as exc:
            raise LedgerSchemaError(
                f"{path}: does not validate against ledger schema "
                f"v{LEDGER_SCHEMA_VERSION}:\n{exc}"
            ) from exc

    @classmethod
    def load(cls, run_id: str) -> "Ledger | None":
        """The run's ledger, or None if it does not exist.

        None means ABSENT and nothing else. An unreadable ledger raises: the
        only caller that treats None as "start fresh" is `load_or_create`, and
        returning None for a schema problem would have it write a new ledger
        over a partially measured run.
        """
        p = paths.run_dir(run_id) / "ledger.json"
        if not p.exists():
            return None
        return cls.parse_file(p)

    @classmethod
    def _scan(cls) -> list[tuple[str, "Ledger"]]:
        """Every readable ledger under results/, reporting the ones that are not.

        The report is the point. A ledger this build cannot read is a resume
        blocker, and round 1's `except Exception: continue` turned it into
        silence -- `--resume` then said "no unfinished run to resume", which
        reads as "nothing to do" and is the opposite of the truth.
        """
        rdir = paths.results_dir()
        if not rdir.is_dir():
            return []
        out: list[tuple[str, Ledger]] = []
        for led_path in sorted(rdir.glob("*/ledger.json")):
            try:
                led = cls.parse_file(led_path)
            except (OSError, LedgerSchemaError) as exc:
                print(f"WARNING: skipping unreadable ledger: {exc}", file=sys.stderr)
                continue
            out.append((led.created_utc, led))
        return out

    @classmethod
    def latest(cls) -> "Ledger | None":
        """Most recently created ledger across results/*/ledger.json (or None).

        By `created_utc`, for the reason spelled out on `latest_unfinished`: a
        run_id sort orders by scale prefix, not by time. This one backs
        `ledgerbench status`, which is the first thing an operator runs after a
        crash -- reporting the wrong run there is how the wrong run gets resumed.
        """
        candidates = [led for _, led in cls._scan()]
        if not candidates:
            return None
        candidates.sort(key=lambda led: (led.created_utc, led.run_id))
        return candidates[-1]

    @classmethod
    def latest_unfinished(cls) -> "Ledger | None":
        """Most recent ledger that is NOT fully complete (resume target).

        Ordered by each ledger's OWN `created_utc`, never by run_id. Run ids
        lead with the scale (`rehearsal-<stamp>-<host>`) so `.gitignore` can
        exclude the non-publishable tiers by prefix -- which makes a
        lexicographic run_id sort order by TIER, not by time. A `full` run that
        crashed twenty hours in would lose `--resume` to any older unfinished
        `smoke` run, and since `--resume` adopts the ledger's pinned scale it
        would have silently continued at smoke numerics.
        """
        candidates = [led for _, led in cls._scan() if not led.is_complete()]
        if not candidates:
            return None
        candidates.sort(key=lambda led: (led.created_utc, led.run_id))
        return candidates[-1]

    @classmethod
    def load_or_create(
        cls, run_id: str, *, config_hash: str, rng_seed: int, slot_set: str,
        scale: str | None = None, body_pool_requests: int | None = None,
    ) -> "Ledger":
        existing = cls.load(run_id)
        if existing is not None:
            if existing.config_hash != config_hash:
                raise RuntimeError(
                    f"config_hash changed for run {run_id}: spec edited since the "
                    f"run started ({existing.config_hash} != {config_hash}). "
                    f"Start a fresh run_id."
                )
            if existing.slot_set != slot_set:
                raise RuntimeError(
                    f"slot set changed for run {run_id}: started as "
                    f"{existing.slot_set!r} ({registry.get_slot_set(existing.slot_set).slots}), "
                    f"resume requested {slot_set!r}. Resume with "
                    f"{registry.SLOT_SET_ENV}={existing.slot_set}."
                )
            if scale is not None:
                if existing.scale is None:
                    existing.scale = scale  # backfill a pre-field ledger
                    existing.save()
                elif existing.scale != scale:
                    raise RuntimeError(
                        f"scale mismatch for run {run_id}: started as "
                        f"{existing.scale!r}, resume requested {scale!r}. Mixing "
                        f"scales would blend measurement windows inside the same "
                        f"rungs. Resume with --scale {existing.scale}."
                    )
            if body_pool_requests is not None and existing.body_pool_requests is None:
                existing.body_pool_requests = body_pool_requests  # backfill a pre-field ledger
                existing.save()
            return existing
        led = cls(run_id=run_id, config_hash=config_hash, rng_seed=rng_seed,
                  slot_set=slot_set, scale=scale,
                  body_pool_requests=body_pool_requests)
        led.save()
        return led

    # ----- phase helpers --------------------------------------------------- #
    def phase(self, name: str) -> PhaseRecord:
        rec = self.phases.get(name)
        if rec is None:
            rec = PhaseRecord()
            self.phases[name] = rec
        return rec

    def phase_done(self, name: str) -> bool:
        rec = self.phases.get(name)
        return rec is not None and rec.state == State.DONE

    def mark_phase(self, name: str, state: State, **data: object) -> None:
        rec = self.phase(name)
        rec.state = state
        rec.ts = rfc3339_micros()
        if data:
            rec.data.update(data)
        self.save()

    # ----- cell helpers ---------------------------------------------------- #
    def cell(self, cell_id: str) -> CellRecord:
        for c in self.cells:
            if c.cell_id == cell_id:
                return c
        c = CellRecord(cell_id=cell_id)
        self.cells.append(c)
        return c

    def stage(self, cell_id: str, stage: str) -> StageRecord:
        cell = self.cell(cell_id)
        rec: StageRecord = getattr(cell, stage)
        return rec

    def mark_stage(self, cell_id: str, stage: str, state: State, **data: object) -> None:
        rec = self.stage(cell_id, stage)
        rec.state = state
        if data:
            rec.data.update(data)
        self.save()

    # ----- resume logic ---------------------------------------------------- #
    def pending_probes(self, cell_id: str, stage: str) -> list[ProbeRecord]:
        """Probes not yet `done` (resume re-runs running/pending/failed)."""
        return [p for p in self.stage(cell_id, stage).probes if p.state != State.DONE]

    def reset_orphan_blocks(self, cell_id: str, stage: str) -> list[int]:
        """RCB block-integrity rule on resume.

        A block is one interleave slot holding the probes that must run adjacent
        in time under a seeded order. If a crash leaves a block with SOME members
        done and some not, the paired comparison for that block is compromised:
        the surviving members ran under different ambient conditions than the
        re-run ones will. So the whole block is reset: every done member is
        flipped back to pending, ITS RECORDED MEASUREMENT IS DROPPED, and the
        stage is flagged `redone_after_resume=true`.

        Returns the list of block ids that were reset. Persists if anything
        changed. Idempotent: a block that is fully done or fully pending is left
        alone.

        Blocks are keyed by BLOCK ID ALONE, and `phases.plan_blocks` is what
        makes that safe by offsetting block ids so they are unique within a
        stage. The old (block, rate) key was correct only while every block held
        exactly one rate; since W24 a confirm block holds each slot at its own
        knee, and under the old key such a block split into per-rate singleton
        groups, `0 < done < members` was false for both, and a crash that halved
        the block was SILENTLY never repaired.
        """
        rec = self.stage(cell_id, stage)
        by_block: dict[int, list[ProbeRecord]] = {}
        for p in rec.probes:
            by_block.setdefault(p.block, []).append(p)

        reset_blocks: list[int] = []
        for block, members in by_block.items():
            done = [p for p in members if p.state == State.DONE]
            if 0 < len(done) < len(members):
                for p in done:
                    drop_probe_data(rec, p)
                    p.reset_to_pending()
                reset_blocks.append(block)

        if reset_blocks:
            rec.data["redone_after_resume"] = True
            rec.data["redone_blocks"] = sorted(set(reset_blocks))
            self.save()
        return sorted(set(reset_blocks))

    def mark_probe_done(
        self,
        cell_id: str,
        stage: str,
        probe: ProbeRecord,
        *,
        artifact: str,
        achieved: float,
        error_rate: float,
        gates_evaluated: list[str] | None = None,
        gates_failed: list[str] | None = None,
        warmup_attack_failed: bool = False,
        draw_inc: str | None = None,
        draw_seconds: float | None = None,
    ) -> None:
        """Flip a probe to done after all its artifacts are flushed.

        The caller must have already written the .bin + cgroup CSV + manifest
        fragment and checksummed them. This only records the
        terminal state and persists the ledger.
        """
        probe.state = State.DONE
        probe.artifact = artifact
        probe.achieved = achieved
        probe.error_rate = error_rate
        probe.gates_evaluated = list(gates_evaluated or [])
        probe.gates_failed = list(gates_failed or [])
        probe.warmup_attack_failed = bool(warmup_attack_failed)
        probe.draw_inc = draw_inc
        probe.draw_seconds = draw_seconds
        probe.ts = rfc3339_micros()
        self.save()

    # ----- status / completion -------------------------------------------- #
    def cell_ids(self) -> list[str]:
        return [c.cell_id for c in self.cells]

    def add_probe(self, cell_id: str, stage: str, probe: ProbeRecord) -> None:
        self.stage(cell_id, stage).probes.append(probe)

    def stage_counts(self, cell_id: str, stage: str) -> tuple[int, int]:
        """(done, total) probe counts for a cell stage."""
        rec = self.stage(cell_id, stage)
        done = sum(1 for p in rec.probes if p.state == State.DONE)
        return done, len(rec.probes)

    def is_complete(self) -> bool:
        """True iff every recorded phase is done AND every cell stage is done.

        A run with no cells yet recorded is NOT complete (cells phase pending).
        """
        if not self.phases:
            return False
        if any(rec.state != State.DONE for rec in self.phases.values()):
            return False
        # host_restore is the terminal phase; require it present + done.
        if not self.phase_done("host_restore"):
            return False
        for c in self.cells:
            for st in ("ladder", "fit", "confirm", "soak"):
                if getattr(c, st).state != State.DONE:
                    return False
        return True

    def next_pending(self) -> str:
        """Human/machine-readable label of the next not-done unit (phase/cell/stage)."""
        for name in PHASE_ORDER:
            rec = self.phases.get(name)
            if name == "cells":
                # the synthetic "cells" marker: descend into cell stages
                for index, c in enumerate(self.cells):
                    # The capacity re-gate runs INSIDE this loop, between cell 1
                    # and cell 2 (phases.run_cells_phase), so the walker has to
                    # descend to it too. Listed after "cells" in PHASE_ORDER it
                    # would be reported only once every cell was done -- i.e.
                    # exactly never, since a failing re-gate is what stops the
                    # later cells from running at all.
                    if index == 1:
                        rg = self.phases.get(REGATE_PHASE)
                        if rg is None or rg.state != State.DONE:
                            return REGATE_PHASE
                    for st in ("ladder", "fit", "confirm", "soak"):
                        if getattr(c, st).state != State.DONE:
                            return f"{c.cell_id}/{st}"
                if rec is None or rec.state != State.DONE:
                    return "cells"
                continue
            if name == REGATE_PHASE:
                # Already walked above, in cell order. A single-cell profile
                # never reaches the in-loop check (it fires at index 1), so its
                # re-gate is reported here instead of nowhere.
                # The state this actually rescues is `cells` DONE with the
                # re-gate not DONE, which a FRESH single-cell run cannot be in:
                # `phases.run_cells_phase` calls the re-gate BEFORE marking
                # cells DONE, so a raised re-gate leaves cells un-DONE and the
                # walker returns "cells". It is reachable on a RESUME of a
                # ledger written before the re-gate existed (W26 bumps no schema
                # version deliberately) -- there `cells` is already DONE and
                # without this branch the re-gate would be pinned at "complete".
                if self.cells and len(self.cells) > 1:
                    continue
            if rec is None or rec.state != State.DONE:
                return name
        return "complete"


# Canonical phase order for status/next-pending. "cells" is a
# synthetic marker the status walker expands into per-cell stages.
# The capacity re-gate's phase name. Declared here rather than imported from
# phases.py: the walker below has to know where it sits in the order, and
# ledger.py is imported BY phases.py.
REGATE_PHASE = "capacity_regate"

PHASE_ORDER: list[str] = [
    "doctor",
    "host_setup",
    "host_readback",
    "seed",
    "capacity_gate",
    "validation",
    "cells",
    # Executed BETWEEN cell 1 and cell 2, not after the cells phase; it is listed
    # here so `status` and `next_pending` know it exists at all, and `next_pending`
    # walks it in its real position (see the "cells" branch above).
    REGATE_PHASE,
    "variants",
    "host_restore",
]
