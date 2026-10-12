"""Live ProbeRunner: fresh SUT per probe, warmup, attack, pull+checksum artifacts.

Implements the `phases.ProbeRunner` protocol against real docker compose + the
vegeta container, reusing the patterns in smoke.py (compose up/exec, vegeta
attack, /runtime-stats + cgroup deltas).

Probe lifecycle:
  - FRESH SUT and vegeta containers per probe (`up --force-recreate` names
    both); only the bench-io VOLUME persists across probes, which is why
    `ensure_body_pool` re-ships bodies when the volume lost them.
  - `ensure_body_pool` is idempotent: the request BODIES are generated once per
    run, shipped into the vegeta container, and re-shipped if the bench-io
    volume lost them (e.g. after a reboot). It pins the mix/size/seed the pool
    was built from (`pool/pool.meta.json`), so a capacity-gate write_share lever
    forces a rebuild instead of leaving pre-lever bodies in place. Bodies carry
    no URL, so ONE pool serves both slots.
  - `draw_and_ship` writes ONE targets file per ATTACK -- warmup segment,
    head-of-window discard, measured window -- each from its own PCG64 stream
    (`util.pcg64.stream_for`) and each sized at `vegeta.DRAW_OVERDRAW` x the
    requests that attack will issue. Round 1 pointed all three at one file, and
    vegeta reads a targets file from the top on every invocation, so the warmup
    populated the just-FLUSHALL'd cache with exactly the ids the measured window
    was about to ask for (R1-A7). `slot` is deliberately NOT one of the stream
    labels: both halves of an RCB block must face the identical sequence.
  - warmup BEFORE every probe via warmup.py gates (poll /runtime-stats at 1 Hz,
    cap from the scale profile); under-warmed -> flagged in the outcome.
  - probe atomicity: write all probe artifacts (vegeta JSON summary + per-second
    p99 CSV + cgroup CSVs), sha256 the published ones into `<slot>.probe.sha256`,
    THEN the caller marks the probe done. The vegeta .bin stays on the bench-io
    volume.
  - cgroup sampling on Linux (cgroups.py); on Windows it is a no-op recording
    "disabled (non-Linux)".

On Windows (smoke) the cgroup CSVs are the disabled-marker record; everything
else runs functionally so a full smoke run-all exercises the whole path.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import capacity_gate as capgate
from . import cgroups, checksums, compose, registry, sut, vegeta
from .compose import ComposeEnv
from .config import SloConfig, WarmupCfg, Workload
from .phases import (
    Cell, PhaseError, ProbeOutcome, ScaleProfile, cpuset_env_by_role,
    gate_ladder_rates, mem_limit_env_by_role,
)
from .runtimestats import PHASE_MEASURED, PHASE_WARMUP, StatsPoller, fetch_once
from .util import paths
from .util.pcg64 import PCG64, stream_for
from .util.proc import run
from .warmup import (
    EXIT_ATTACK_FAILED_PREFIX,
    EXIT_CAP_REACHED,
    EXIT_GATES_PASSED,
    FLATNESS_TRAILING_SAMPLES,
    MANN_KENDALL_ALPHA,
    STATUS_NOT_APPLICABLE,
    WarmupResult,
    WarmupState,
    WarmupThresholds,
    gate_set_for_cell,
    not_applicable_gate,
    posture_waived_gate,
)
from .vegeta import AttackParams, BodyPool, DrawnTargets

IS_LINUX = sys.platform.startswith("linux")

# Slot topology (service, profile, ports, volumes, url env) comes from
# ledgerbench.registry, so a stale slot id RAISES instead of resolving to a dict
# miss.
_VEGETA_CONTAINER = compose.container_name(compose.SVC_VEGETA)

# Where the shared body pool lives inside the vegeta container, RELATIVE to the
# working directory every attack is invoked from (`/io`). vegeta resolves an
# `@bodyfile` reference against its cwd, so this prefix is baked into every
# targets file the draw writes and has to agree with where the pool is shipped.
POOL_REF_PREFIX = "pool/bodies"


def _slots() -> tuple[registry.Slot, ...]:
    """The SUT slots this run compares. Lazy: see registry.active_slot_set."""
    return tuple(registry.get_slot(s) for s in registry.active_slot_set().slots)


def _slot_services() -> list[str]:
    """Distinct compose services across the active slots (two slots of one
    language share a service, and compose must not be handed it twice)."""
    return list(dict.fromkeys(s.service for s in _slots()))


def _slot_profiles() -> list[str]:
    return list(dict.fromkeys(s.profile for s in _slots()))

# Warmup is driven in segments so it can EXIT AS SOON AS THE GATES PASS rather
# than always burning `hard_cap_seconds` (spec/slo.yaml warmup). The cap is a
# ceiling, not a duration -- which is what makes raising it 180 -> 300 for the
# JVM free in the normal case (PROGRESS O4). Without segmentation that claim is
# false and the raise costs +120 s on every probe: ~9.4 h per cell.
WARMUP_SEGMENT_S = 30
# Gates are first evaluated after this many segments, so the flatness window
# (warmup.FLATNESS_TRAILING_SAMPLES = 30 one-second samples) is always full
# before it can pass a probe. One segment would let a 30-sample window be
# scored against exactly its own 30 samples with no trailing context.
WARMUP_MIN_SEGMENTS = 2
# `_warmup_rate` divides the worst-case endpoint requirement by a FIXED target
# duration, not by the hard cap. Tying it to the cap coupled two unrelated
# dials: raising the cap 180 -> 300 silently dropped the warmup rate 618 -> 371
# rps, changing the traffic shape HotSpot and the JIT warm against while leaving
# the total request count ~111k either way. 180 preserves the round-1 rate.
WARMUP_RATE_TARGET_S = 180


def _housekeeping_core() -> int:
    """First core of HOUSEKEEPING_CPUS (host-setup default 15): the cgroup
    sampler thread pins itself there so its 50 ms reads never run on SUT cores."""
    raw = os.environ.get("HOUSEKEEPING_CPUS", "15")
    head = raw.split(",")[0].split("-")[0].strip()
    try:
        return int(head)
    except ValueError:
        return 15


def _sample_targets(slot: registry.Slot) -> list[cgroups.SampleTarget]:
    """Every cgroup sampled around one probe's measured window.

    The SUT and vegeta are the non-persistent targets: `run_probe`'s
    `up --force-recreate` names both, so their cgroups are new each probe.
    Everything else lives for the whole run, which is exactly why every counter
    is delta'd (see cgroups.py).

    The load generator and the container runtime are sampled but carry
    `ROLE_LOAD_GENERATOR` / `ROLE_CONTAINER_RUNTIME`, which is what keeps their
    CPU out of any per-request figure downstream. Both daemon units are
    sampled, not just dockerd: measured on this box, the containerd-shim -- the
    process the SUT's json-file stdout is relayed through -- is charged to
    containerd.service, so docker.service alone under-counts the runtime by the
    part of it that is on the logging path.
    """
    return [
        cgroups.SampleTarget.for_container(
            slot.slot_id, slot.container_name(),
            role=cgroups.ROLE_SUT, persistent=False, trace=True,
        ),
        cgroups.SampleTarget.for_container(
            compose.SVC_POSTGRES, compose.container_name(compose.SVC_POSTGRES),
            role=cgroups.ROLE_DATABASE, persistent=True, trace=True,
        ),
        cgroups.SampleTarget.for_container(
            compose.SVC_REDIS, compose.container_name(compose.SVC_REDIS),
            role=cgroups.ROLE_CACHE, persistent=True,
        ),
        cgroups.SampleTarget.for_container(
            compose.SVC_VEGETA, _VEGETA_CONTAINER,
            # NOT persistent: run_probe force-recreates vegeta beside the SUT,
            # and `persistent` travels into the artifact as the claim "this
            # cgroup's history spans the run" -- which would overstate what
            # vegeta's memory.peak covers by a factor of the probe count.
            role=cgroups.ROLE_LOAD_GENERATOR, persistent=False,
        ),
        cgroups.SampleTarget.for_unit(
            "dockerd", cgroups.DOCKER_UNIT, role=cgroups.ROLE_CONTAINER_RUNTIME,
        ),
        cgroups.SampleTarget.for_unit(
            "containerd", cgroups.CONTAINERD_UNIT,
            role=cgroups.ROLE_CONTAINER_RUNTIME,
        ),
    ]


def oom_labels(windows: dict[str, cgroups.WindowResult]) -> list[str]:
    """Sampled cgroups that took an OOM kill INSIDE the measured window.

    Extracted from `run_probe` deliberately: logic reachable only through docker
    cannot be reached by the suite, so it is a pure helper.

    The predicate is the window DELTA, and that is what makes widening it past
    the SUT safe. `memory.events oom_kill` is a lifetime counter, so testing its
    absolute value against a cgroup that outlives the probe would LATCH: every
    probe after the first aborts on history, `--resume` re-runs it against the
    same latched counter, and the run can never finish. With a delta, an OOM in
    PostgreSQL fails the probe it actually happened in and the resume recovers.
    """
    return [w.label for w in windows.values()
            if w.enabled and (w.oom_kill_delta or 0) > 0]


# The stream label list, verbatim, as it is published in the manifest and as
# `draw_and_ship` derives it. One definition: a formula the artifact states and
# the code does not follow is a claim broader than the code.
DRAW_SEED_FORMULA = (
    "PCG64(seed=workload.rng.target_generator_seed_default, "
    "inc=util.pcg64.stream_for(seed, run_id, cell_id, stage, rate, block, phase))"
)
DRAW_MODE = "per_probe_stream"
# What a probe's `prewarm_containment` is scored against: every attack that ran
# between the Redis FLUSHALL and the measured window. Named here so the artifact
# states its own scope -- the fraction is meaningless without it.
PREWARM_SCOPE = (
    "the union of the invoice ids drawn by every warmup segment and by the "
    "head-of-window discard, i.e. every attack between the Redis FLUSHALL and "
    "the measured window"
)


def warmup_segment_draw(*, seg: int, warm_rate: int, seg_s: int) -> tuple[str, int]:
    """(stream phase label, target count) for warmup SEGMENT `seg`.

    Extracted from `_warmup` rather than left inline, because that loop is
    reachable only through docker and the suite stubs the draw out entirely.
    Both halves are load-bearing and neither is obvious:

      - the label carries the SEGMENT INDEX. Segments are separate vegeta
        invocations, and vegeta reads a targets file from the top every time, so
        one draw for the whole warmup would have segment 2 replay segment 1 and
        leave warmup measuring a cache it had just populated itself.
      - the count is sized from the WARMUP rate, never the probe's measured
        rate. The stream LABELS carry the measured rate (that is what makes two
        rungs different probes); the SIZE is what the attack will actually
        issue, and at the top of a full ladder the two differ by ~35x -- which
        would draw a 1.6M-target file for an 11k-request attack.
    """
    return f"warmup.seg{seg}", vegeta.probe_draw_count(warm_rate, seg_s)


@dataclass(frozen=True)
class WarmupDraws:
    """Everything a warmup's per-segment draws contribute to the probe record.

    A pure fold over the segment draws, for the same reason `probe_draw_cost`
    and `warmup_segment_draw` are pure: `_warmup`'s segment loop is reachable
    only through docker and every gate test stubs the draw out, so statements
    inline there cannot be reached by the suite. The id union is load-bearing:
    the warmup term is most of `prewarm_containment`.

    `invoice_ids` is a union rather than a Counter: it is scored AGAINST a
    measured window's read counts (`vegeta.prewarm_containment`), where what
    matters is only whether an id was touched before the window opened.
    """

    incs: list[str]
    invoice_ids: set[int]
    seconds: float


def warmup_draws(drawn: Sequence[DrawnTargets]) -> WarmupDraws:
    """Fold one warmup's segment draws into what `WarmupResult` carries."""
    ids: set[int] = set()
    for d in drawn:
        ids |= set(d.invoice_ids)
    return WarmupDraws(
        incs=[f"{d.inc:032x}" for d in drawn],
        invoice_ids=ids,
        seconds=sum(d.generation_seconds + d.ship_seconds for d in drawn),
    )


def prewarmed_ids(warm: WarmupResult, *drawn: DrawnTargets) -> set[int]:
    """Every invoice id touched between the Redis FLUSHALL and the measured window.

    `PREWARM_SCOPE` in code: the union of the warmup's segment draws and the
    head-of-window discard's. Extracted from `run_probe` for the reason above;
    both terms are load-bearing:

      - the WARMUP term is the larger one and it runs immediately after the
        FLUSHALL, so dropping it understates residency by roughly half;
      - the DISCARD term is drawn at the probe's MEASURED rate, so it grows with
        the rung while the warmup's does not, and dropping it flattens the rate
        dependence the published envelope is an envelope over.

    Set-valued because `prewarm_containment` weights by the MEASURED window's
    request counts, not by how often an earlier attack asked for an id: one
    touch after a FLUSHALL is what makes an id resident.
    """
    ids = set(warm.target_draw_invoice_ids)
    for d in drawn:
        ids |= set(d.invoice_ids)
    return ids


def probe_draw_cost(warm: WarmupResult, *drawn: DrawnTargets) -> float:
    """A whole probe's target-draw cost: every attack, generation PLUS ship.

    A pure function rather than three `+=` sites inside `run_probe`, and for
    the reason `warmup_segment_draw` is one: that method is reachable only
    through docker, so an accumulation written inline there cannot be reached by
    the suite, and the artifact that answers "did B8 become the longest phase of
    the run" is exactly this number.

    Three terms, none of them optional:
      - `warm.target_draw_seconds`, itself summed over every warmup SEGMENT (a
        cap-length warmup is ten of them, so it is the largest term at low
        rates);
      - each remaining draw's GENERATION seconds;
      - each remaining draw's SHIP seconds. The `docker cp` is the half that
        scales with the file -- 109.3 MB at the top rung of a full ladder against
        4.1 MB at the bottom -- so a generation-only total would understate the
        cost exactly where it matters and would have missed the ship entirely.
    """
    return warm.target_draw_seconds + sum(
        d.generation_seconds + d.ship_seconds for d in drawn
    )


def probe_targets_name(
    *, cell_id: str, stage: str, rate: int, block: int, phase: str,
) -> str:
    """Container-side filename for ONE attack's targets, carrying its identity.

    Not `targets.<phase>.txt`. `/io` is a volume shared by every probe of the
    run, so a fixed name means every probe writes over the last one -- and a
    `docker cp` that failed would leave the previous probe's file there for
    vegeta to attack. The two defences are paired: the copy is checked (see
    `draw_and_ship`) and the name is unique per attack, so a stale file cannot
    be silently consumed even if a check were ever removed.

    The identity is the stream identity minus the slot, which is exactly right:
    the slot is the directory, and both slots of an RCB block draw the SAME
    sequence by design (only `base_url` differs), so their files are
    same-named in different directories and never collide.
    """
    safe = "-".join(
        str(part).replace("/", "_").replace(" ", "_")
        for part in (cell_id, stage, f"r{rate}", f"b{block}", phase)
    )
    return f"targets.{safe}.txt"


def _draw_record(
    drawn: DrawnTargets, *, slot: str, cell_id: str, stage: str, rate: int,
    block: int, measured_window_seconds: int, probe_draw_seconds: float,
    warmup_incs: list[str], containment: vegeta.Containment,
) -> dict:
    """The `<slot>.target_draw.json` body for one probe's MEASURED draw.

    Extracted from `run_probe` for the reason every such helper here is: logic
    reachable only through docker cannot be reached by the suite, so it is a
    pure helper.

    `probe_draw_seconds` is the WHOLE probe's draw cost -- every warmup segment,
    the discard and the measured window, GENERATION PLUS SHIP -- not just the
    measured draw. The design that introduced the per-probe draw named "B8
    silently becomes the longest phase of the run" as its own risk, and a probe
    makes as many draws as it runs warmup segments plus two, so a number
    covering one of them would not detect it. The ship is in it because the
    copy is the half that scales with the file (109.3 MB at the top rung of a
    full ladder), so a generation-only number would understate exactly the
    probes where the cost is largest.

    `prewarm_containment` is the OTHER number this artifact exists to carry, and
    it is published rather than argued away. Separate PCG64 streams make the
    warmup's sequence independent of the measured window's; they do not stop
    both streams drawing the Zipf head over one keyspace, so a real share of a
    window's reads is already resident when the window opens. The manifest and
    the report render this measured fraction; before the W20 correction they asserted it was
    nothing.
    """
    return {
        "slot": slot,
        "cell_id": cell_id,
        "stage": stage,
        "rate": rate,
        "block": block,
        "draw_mode": DRAW_MODE,
        "seed_formula": DRAW_SEED_FORMULA,
        "measured_window_seconds": measured_window_seconds,
        "measured": {
            "phase": "measured",
            "inc": f"{drawn.inc:032x}",
            "requests_drawn": drawn.count,
            "distinct_invoice_ids": len(drawn.invoice_ids),
            "distinct_customer_ids": drawn.distinct_customer_ids,
            "distinct_customer_ids_list_urls": len(drawn.customer_ids_list_urls),
            "distinct_customer_ids_create_bodies": len(drawn.customer_ids_create_bodies),
            "endpoint_counts": dict(drawn.endpoint_counts),
            "invoice_read_requests": drawn.invoice_read_requests,
            "generation_seconds": round(drawn.generation_seconds, 4),
            "ship_seconds": round(drawn.ship_seconds, 4),
        },
        "warmup_stream_incs": list(warmup_incs),
        "probe_draw_seconds": round(probe_draw_seconds, 4),
        "prewarm_containment": {
            "measured_against": PREWARM_SCOPE,
            "invoice_read_requests": containment.requests,
            "requests_hitting_a_prewarmed_id": containment.hits,
            "prewarmed_distinct_invoice_ids": containment.prewarmed_ids,
            "fraction": (
                None if containment.fraction is None
                else round(containment.fraction, 6)
            ),
        },
    }


def shared_summary_name(slot_id: str, label: str) -> str:
    """Artifact name for a co-sampled cgroup's window under one probe.

    NOT `cpu_mem_<label>.summary.json`. The block directory is shared by BOTH
    slots (`phases._artifact_dir` takes no slot component), and a block's two
    probes run sequentially, so one file per block would be written by the first
    probe and overwritten by the second -- and the DB cost of one slot would
    then be published as both slots'. Two identical DB columns read as a
    finding, not as a bug, which is why this is keyed by slot.

    The name cannot collide with a SUT summary (`cpu_mem_<slot>.summary.json`)
    because `registry.Slot` refuses a slot id containing '.', so no slot can
    ever be named `<slot>.<label>`.
    """
    return f"cpu_mem_{slot_id}.{label}.summary.json"


def _cache_delta(before: dict, after: dict) -> dict:
    """Redis keyspace hit/miss delta over the window, with explicit source.

    `source: "not_measured"` when either boundary sample failed; a null hit
    rate must never be read as a measured 0.
    """
    hits_b, miss_b = before.get("keyspace_hits"), before.get("keyspace_misses")
    hits_a, miss_a = after.get("keyspace_hits"), after.get("keyspace_misses")
    if None in (hits_b, miss_b, hits_a, miss_a):
        return {"keyspace_hits": None, "keyspace_misses": None, "hit_rate": None,
                "source": "not_measured",
                "note": "redis INFO stats unavailable at a window boundary"}
    hits, misses = hits_a - hits_b, miss_a - miss_b
    total = hits + misses
    return {
        "keyspace_hits": hits, "keyspace_misses": misses,
        "hit_rate": (hits / total) if total > 0 else None,
        "source": "redis INFO stats delta over the measured window",
        "note": "" if total > 0 else "no keyspace operations in the window",
    }


@dataclass
class LiveProbeRunner:
    run_id: str
    profile: ScaleProfile
    workload: Workload
    slo: SloConfig
    bench_overlay: bool = field(default=IS_LINUX)
    # Compose env forced onto EVERY invocation this runner makes: the capacity
    # gate's realized levers (PG/vegeta cpuset move, CACHE_TTL_SECONDS). Empty
    # unless a lever fired; set through `apply_effective`.
    env_overrides: dict[str, str] = field(default_factory=dict)
    # The shared request-body pool, its Zipf tables and whether the pool has
    # been shipped into the vegeta container. All three are per RUN, not per
    # slot: bodies carry no URL, and `build_zipf_table` allocates an N-entry
    # weights list plus an N-entry CDF for N = the seeded invoice count, which a
    # per-probe draw would otherwise repeat 600+ times.
    _pool: BodyPool | None = None
    _zipf: tuple[object, object] | None = None
    _pool_shipped: bool = False
    # B6 row 10 is checked ONCE, on this process's first measured window. Per
    # process, not per run: a resume follows a restart, which is exactly when a
    # container can come back somewhere else.
    _placement_checked: bool = False

    # ----- effective workload / topology (capacity-gate levers) ----------- #
    def apply_effective(
        self,
        *,
        workload: Workload | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        """Adopt the EFFECTIVE workload/topology the capacity gate resolved.

        The gate's levers change what the cells run (mix, cache TTL, PG cores).
        Before this existed the levers were computed into a throwaway object and
        the cells kept generating targets from the frozen spec workload, so a
        fired lever changed nothing. Called by the capacity-gate phase on a
        fresh run AND on every resume (the ledger replays the same values).
        """
        if workload is not None:
            self.workload = workload
            # Force a pool rebuild: the mix the bodies were drawn under predates
            # the lever, and the pool meta is what detects that.
            self._pool = None
            self._pool_shipped = False
        if env_overrides:
            self.env_overrides.update(env_overrides)

    def _env(self, *, profiles: list[str] | None = None, bench_overlay: bool | None = None) -> ComposeEnv:
        """ComposeEnv with the realized lever env merged in (single choke point)."""
        env = ComposeEnv.base(
            bench_overlay=self.bench_overlay if bench_overlay is None else bench_overlay,
            profiles=list(profiles or []),
        )
        env.extra_env.update(self.env_overrides)
        return env

    def apply_topology(self) -> dict:
        """Recreate the containers whose cpuset the pg_cores lever moved.

        A cpuset is baked into a container at create time, so PG (and the donor,
        vegeta) must be RECREATED for the lever to take effect: an `up -d`
        without --force-recreate would leave PG on its old 3 cores while the
        manifest claimed 4. Returns a small record for the ledger.
        """
        moved = {k: v for k, v in self.env_overrides.items() if k.endswith("_CPUS")}
        if not moved:
            return {"recreated": [], "cpusets": {}}
        env = self._env(profiles=[compose.PROFILE_LOAD])
        res = compose.compose(
            env,
            ["up", "-d", "--wait", "--force-recreate",
             compose.SVC_POSTGRES, compose.SVC_VEGETA],
            check=False, timeout=600,
        )
        return {
            "recreated": [compose.SVC_POSTGRES, compose.SVC_VEGETA],
            "cpusets": moved,
            "ok": res.ok,
            "detail": res.stderr.strip()[-300:] if not res.ok else "",
        }

    # ----- one-time image build (idempotent) ------------------------------ #
    def prepare(self) -> None:
        """Build the SUT + vegeta + seed images once (idempotent; cached).

        Called by run_all before any phase so a fresh checkout works without a
        separate `make build`. docker reuses the layer cache, so a re-run is fast.
        """
        compose.compose(
            self._env(),
            ["build", *_slot_services(), compose.SVC_VEGETA, compose.SVC_SEED],
            check=False, timeout=3600,
        )

    # ----- clean DB for a fresh seed -------------------------------------- #
    def reset_db(self) -> None:
        """Drop the pgdata volume so a fresh seed applies schema.sql cleanly.

        Only invoked on the fresh-seed branch (resume verify-only never calls
        this). `down -v` removes the named pgdata volume; the next compose `up`
        re-initializes PG from initdb, and the seeder applies the schema fresh.
        """
        env = self._env(
            profiles=[*_slot_profiles(), compose.PROFILE_LOAD, compose.PROFILE_SEED],
        )
        compose.compose(env, ["down", "-v"], check=False, timeout=300)

    # ----- body pool + per-probe target draw (W20 / R1-A7) ---------------- #
    def _pool_dir(self) -> Path:
        return paths.run_dir(self.run_id) / "io" / "pool"

    def _pool_meta(self) -> dict:
        n_create, n_quote = vegeta.body_pool_sizes(
            self.workload, self.profile.body_pool_requests
        )
        return {
            "mix": dict(self.workload.mix.as_pairs()),
            "body_pool_requests": self.profile.body_pool_requests,
            "n_create": n_create,
            "n_quote": n_quote,
            "seed": self.workload.rng.target_generator_seed_default,
            "scale": self.profile.seed_scale,
            "ref_prefix": POOL_REF_PREFIX,
        }

    def ensure_body_pool(self, cell: Cell) -> None:
        """Generate + ship the run's shared request bodies. Idempotent.

        The pool is slot-independent (a body carries no URL) and cell-independent
        (the mix is a property of the effective workload, not of the posture), so
        this is once per run. Round 1 keyed the ship on (slot, cell), so the
        same ~1.9 GB of bodies was `docker cp`-ed once for every pair a run
        executed -- four times for the two headline cells, ten with the variants
        selected.

        Regeneration is driven by the meta on disk, not by an in-process flag: a
        resume is a new process, and the question "were these bodies drawn under
        the mix now in force" is answerable only from the artifact.
        """
        del cell                      # posture does not change the bodies
        pool_dir = self._pool_dir()
        meta_path = pool_dir / "pool.meta.json"
        meta = self._pool_meta()
        stale = True
        if meta_path.exists():
            try:
                stale = json.loads(meta_path.read_text(encoding="utf-8")) != meta
            except (OSError, ValueError):
                stale = True
        if self._zipf is None:
            self._zipf = vegeta.zipf_tables(self.workload, self.profile.seed_scale)
        if stale or self._pool is None:
            if stale:
                shutil.rmtree(pool_dir / "bodies", ignore_errors=True)
                self._pool_shipped = False
            self._pool = vegeta.generate_body_pool(
                self.workload, out_dir=pool_dir,
                seed=meta["seed"], n_create=meta["n_create"],
                n_quote=meta["n_quote"], scale=self.profile.seed_scale,
                ref_prefix=POOL_REF_PREFIX, customer_zipf=self._zipf[0],
            )
            if stale:
                paths.write_text_atomic(meta_path, json.dumps(meta, indent=2))
        env = self._env(profiles=[compose.PROFILE_LOAD])
        present = compose.exec_in(
            env, compose.SVC_VEGETA,
            ["sh", "-c", f"test -d /io/{POOL_REF_PREFIX} && echo yes || echo no"],
            check=False,
        )
        if self._pool_shipped and "yes" in present.stdout:
            return
        compose.exec_in(env, compose.SVC_VEGETA, ["sh", "-c", "mkdir -p /io/pool"],
                        check=False)
        compose.compose(
            env, ["cp", str(pool_dir) + "/.", f"{compose.SVC_VEGETA}:/io/pool/"],
            check=False, timeout=3600,
        )
        self._pool_shipped = True

    def draw_and_ship(
        self, *, slot: str, cell: Cell, stage: str, rate: int, block: int,
        phase: str, count: int,
    ) -> DrawnTargets:
        """Draw ONE attack's targets file and ship it to the vegeta container.

        The stream labels are (run, cell, stage, rate, block, phase). Two rules
        in that list carry the whole fix:

          - `slot` is EXCLUDED. Both slots of an RCB block must face the
            identical request sequence -- that is the pairing contract, and the
            base URL is the only thing allowed to differ between their files.
          - `phase` is INCLUDED, and every warmup segment is its own phase. That
            is what stops the warmup pre-warming the measured window's ids, and
            what stops segment 2 of a warmup replaying segment 1.

        `count` comes from `vegeta.probe_draw_count(attack rate, duration)`, so
        vegeta never reaches the end of the file and starts over. Drawing per
        probe without that is only half of R1-A7's fix. It is the ATTACK's rate,
        which for a warmup segment is the sub-knee warmup rate, while `rate` in
        the labels is always the PROBE's measured rate -- keying the stream on
        the warmup rate would collapse every rung of a cell onto one stream.
        """
        assert self._pool is not None and self._zipf is not None, (
            "draw_and_ship before ensure_body_pool"
        )
        seed = self.workload.rng.target_generator_seed_default
        inc = stream_for(
            seed, self.run_id, cell.cell_id, stage, str(rate), str(block), phase,
        )
        io_dir = paths.run_dir(self.run_id) / "io" / slot
        out_path = io_dir / probe_targets_name(
            cell_id=cell.cell_id, stage=stage, rate=rate, block=block, phase=phase,
        )
        drawn = vegeta.draw_targets(
            self.workload, out_path=out_path, count=count,
            rng=PCG64(seed, inc), base_url=registry.get_slot(slot).net_url,
            pool=self._pool,
            customer_zipf=self._zipf[0], invoice_zipf=self._zipf[1],
        )
        env = self._env(profiles=[compose.PROFILE_LOAD])
        compose.exec_in(env, compose.SVC_VEGETA,
                        ["sh", "-c", f"mkdir -p /io/{slot}"], check=False)
        t0 = time.perf_counter()
        cp = compose.compose(
            env, ["cp", str(out_path), f"{compose.SVC_VEGETA}:/io/{slot}/{out_path.name}"],
            check=False, timeout=600,
        )
        drawn.ship_seconds = time.perf_counter() - t0
        # CHECKED, and the filename carries the probe identity, for one reason:
        # /io is a volume that outlives a probe. Under a fixed name an
        # unchecked `docker cp` that failed would leave the PREVIOUS probe's
        # file in place, vegeta would attack it, and if that file had
        # been drawn for a lower rung it would WRAP inside the measured window
        # -- R1-A7 reintroduced with no error anywhere in the run. Raising is
        # right rather than returning a failed probe: a probe that never
        # received its targets has nothing to retry against.
        if not cp.ok:
            # `PhaseError`, not `RuntimeError`: this is the only loud failure in
            # the probe path that reached the CLI as a bare traceback, because
            # `run-all` (and every per-phase command, via `_guarded`) catches
            # exactly `PhaseError` and turns it into "run-all failed: ..." with
            # exit 1. A run that dies here dies 20 hours in, and the operator
            # reads the last line.
            raise PhaseError(
                f"docker cp of {out_path.name} to {compose.SVC_VEGETA}:/io/{slot}/ "
                f"failed (rc={cp.returncode}): {cp.stderr[-300:]}"
            )
        return drawn

    # ----- container ground truth (B6 row 10) ----------------------------- #
    def _assert_container_placement(
        self, sampler: cgroups.MultiSampler, env_sut: ComposeEnv, svc: str,
    ) -> None:
        """Refuse the run when a container did not get the budget it was given.

        `sampler.start()` has just read every container's
        `cpuset.cpus.effective`, `memory.max` and `memory.swap.max` off its own
        cgroup, so this costs nothing and it happens on the FIRST measured
        window rather than at finalization. A SUT slot missing from
        infra/compose.bench.yaml runs unpinned across every logical core while
        the run records a 6-core budget, which invalidates every CPU-ms- and
        memory-per-request figure in the cell -- so this raises rather than
        returning a failed probe: a failed probe is retried, and a wrong cpuset
        is not something a retry fixes.

        The memory ceiling is checked here for the same reason and at the same
        price. It is the one both runtimes size their heap from, so a container
        that came up unlimited (or with a swap allowance) is not a cosmetic
        divergence from the ledger: it is a different experiment, and memory
        per request is a headline metric.

        The sampler thread and the SUT container are stopped first: raising past
        a running attack would leave both behind for the operator to find.
        """
        if self._placement_checked:
            return
        self._placement_checked = True
        env = {**os.environ, **self.env_overrides}
        limits = mem_limit_env_by_role(env)
        bad = (sampler.cpuset_mismatches(cpuset_env_by_role(env))
               + sampler.limit_mismatches(limits))
        if bad:
            # One repair attempt for the ONE mismatch class that is a docker
            # create-path fault rather than a wrong config: observed on Docker
            # 29.2.1 under the systemd driver, `memory.swap.max=max` is left
            # intermittently despite HostConfig MemorySwap == Memory (see
            # cgroups.MultiSampler.swap_allowance_misconfigured). `docker
            # update` rewrites the cgroup AND the stored unit property; the
            # re-read below is what decides, so a repair that did not take
            # still aborts.
            if self._repair_swap_allowances(sampler, limits):
                sampler.reread_placement()
                bad = (sampler.cpuset_mismatches(cpuset_env_by_role(env))
                       + sampler.limit_mismatches(limits))
        if not bad:
            return
        sampler.stop()
        compose.compose(env_sut, ["stop", svc], check=False)
        raise PhaseError(
            "container placement does not match infra/compose.bench.yaml on the "
            "first measured window; every resource number in the cell would be "
            "measured against a core or memory budget the containers do not "
            "have: " + "; ".join(bad)
        )

    def _repair_swap_allowances(
        self, sampler: cgroups.MultiSampler,
        limits_by_role: "dict[str, int]",
    ) -> bool:
        """`docker update --memory X --memory-swap X` for flaked containers.

        Returns True when at least one update was issued; the caller re-reads
        the cgroup and re-decides, so this can never wave a still-wrong
        container through. Each repair is printed: the run log is where an
        operator reconstructs why a container's swap ceiling has two values in
        its lifetime.
        """
        issued = False
        for t in sampler.swap_allowance_misconfigured():
            want = limits_by_role.get(t.role)
            if not want:
                continue
            res = run(
                ["docker", "update", f"--memory={want}", f"--memory-swap={want}",
                 t.container_name],
                check=False, timeout=30,
            )
            print(
                f"[placement-repair] {t.label}: memory.swap.max was nonzero at "
                f"window start (docker create-path flake); "
                f"docker update --memory={want} --memory-swap={want} "
                f"{t.container_name} -> {'ok' if res.ok else res.stderr.strip()[:200]}"
            )
            issued = issued or res.ok
        return issued

    # ----- one probe ------------------------------------------------------- #
    def run_probe(
        self, *, cell: Cell, stage: str, lang: str, rate: int, block: int,
        duration_s: int, warmup_cap_s: int, artifact_dir: Path,
    ) -> ProbeOutcome:
        slot = registry.get_slot(lang)
        profile_obj = slot.profile
        svc = slot.service
        host_url = slot.host_url
        env_sut = self._env(profiles=[profile_obj, compose.PROFILE_LOAD])
        env_sut.extra_env.update(cell.extra_env)
        env_load = self._env(profiles=[compose.PROFILE_LOAD])

        # FRESH SUT: stop+recreate so no prior-probe heap leaks across the pair.
        # EVERY registered SUT service is stopped, not just this probe's: a
        # sibling may be resident from the knob-recording read or a prior phase,
        # and they share one cpuset, so an idle sibling on the SUT cores during a
        # measured window perturbs the headline. The invariant is TOTAL rather
        # than a list of two names, so adding a slot cannot silently leave one
        # running.
        env_all = self._env(profiles=_slot_profiles())
        compose.compose(
            env_all, ["stop", *_slot_services()], check=False, timeout=120,
        )
        up = compose.compose(
            env_sut, ["up", "-d", "--wait", "--force-recreate", svc, compose.SVC_VEGETA],
            check=False, timeout=600,
        )
        if not up.ok:
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail=f"up failed: {up.stderr[-300:]}")
        try:
            sut.wait_ready(host_url, timeout_s=180.0)
        except TimeoutError as exc:
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail=str(exc))

        # Same cold cache for every probe; warmup repopulates by construction.
        compose.compose(
            self._env(),
            ["exec", "-T", compose.SVC_REDIS, "redis-cli", "FLUSHALL"], check=False,
        )

        self.ensure_body_pool(cell)
        # Every draw this probe makes, so the cost is summed in ONE place by a
        # function the suite can reach (`probe_draw_cost`).
        probe_draws: list[DrawnTargets] = []

        # cgroup samplers (Linux): resolve every path NOW, against this fresh
        # SUT container, because resolution shells out to `docker inspect` and
        # `systemctl` and neither may run inside the measured window. ONE thread
        # serves all six targets (see cgroups.MultiSampler); one thread each
        # would put six of them on the single housekeeping core they all pin to.
        sampler = cgroups.MultiSampler(
            _sample_targets(slot), pin_core=_housekeeping_core(),
        )

        # warmup: short attack while polling /runtime-stats at 1 Hz.
        warm = self._warmup(
            env_load, lang, host_url, warmup_cap_s, cell,
            stage=stage, block=block, rate=rate,
        )

        # The MEASURED draw is generated and shipped HERE, before the discard
        # attack, and it is not an ordering detail. The discard exists so the
        # measured window contains no transient; drawing between the discard and
        # the window would put a rate-correlated one right back inside that gap
        # -- measured on this box, generation alone is 0.13 s for a 4.1 MB file
        # at 500 rps against 3.59 s for 109.3 MB at 13,312, and the `docker cp`
        # of that file is on top. The discard's own draw stays where it is: the
        # transient it makes is absorbed by the attack that follows it.
        drawn = self.draw_and_ship(
            slot=lang, cell=cell, stage=stage, rate=rate, block=block,
            phase="measured", count=vegeta.probe_draw_count(rate, duration_s),
        )
        probe_draws.append(drawn)

        # Head-of-window discard at the MEASURED rate (slo
        # ladder.warmup_discard_seconds): runs before the cgroup sampler starts
        # and before the measured .bin, so neither the probe p99 nor the CPU/
        # memory window includes the JIT-retiering transient at this rate.
        pre_draws: list[DrawnTargets] = []
        if self.profile.warmup_discard_s > 0:
            d_drawn = self.draw_and_ship(
                slot=lang, cell=cell, stage=stage, rate=rate, block=block,
                phase="discard",
                count=vegeta.probe_draw_count(rate, self.profile.warmup_discard_s),
            )
            probe_draws.append(d_drawn)
            pre_draws.append(d_drawn)
            d_params = AttackParams(
                rate=rate, duration_s=self.profile.warmup_discard_s,
                max_workers=self.profile.max_workers,
                connections=self.profile.connections, timeout_s=self.profile.timeout_s,
            )
            d_argv = vegeta.build_attack_argv(
                targets_in_container=f"{lang}/{d_drawn.targets_path.name}",
                out_bin_in_container=f"/io/{lang}/discard.bin", params=d_params,
            )
            d_res = compose.exec_in(
                env_load, compose.SVC_VEGETA,
                ["sh", "-c", f"cd /io && {' '.join(d_argv)}"], check=False,
            )
            if not d_res.ok:
                # A failed discard leaves the at-rate JIT/re-tiering transient
                # INSIDE the measured window -- the exact contamination the
                # discard exists to keep out. Fail the probe the same way a
                # failed measured attack does; the ledger re-runs it.
                compose.compose(env_sut, ["stop", svc], check=False)
                return ProbeOutcome(
                    achieved=0, error_rate=1.0, p99_ms=0, artifact="", ok=False,
                    detail=f"discard attack failed: {d_res.stderr[-300:]}",
                )

        # What share of this window's read traffic the attacks BEFORE it already
        # touched. Separate streams stop the warmup replaying the window's
        # prefix; they do not (and cannot) stop both streams drawing the Zipf
        # head. Measured here rather than asserted away -- see
        # `vegeta.prewarm_containment`.
        containment = vegeta.prewarm_containment(
            drawn.invoice_id_reads, prewarmed_ids(warm, *pre_draws),
        )
        draw_seconds = probe_draw_cost(warm, *probe_draws)

        # measured attack.
        out_bin = f"/io/{lang}/{stage}_r{rate}_b{block}.bin"
        params = AttackParams(
            rate=rate, duration_s=duration_s,
            max_workers=self.profile.max_workers,
            connections=self.profile.connections, timeout_s=self.profile.timeout_s,
        )
        attack = vegeta.build_attack_argv(
            targets_in_container=f"{lang}/{drawn.targets_path.name}",
            out_bin_in_container=out_bin, params=params,
        )
        # Window-boundary samples (never DURING the measured window, so they
        # cannot perturb it): Redis keyspace hit/miss counters (spec/cache.md:
        # measured, recorded and reported). The page-fault counters are edges of
        # the sampler itself.
        cache_before = self._redis_keyspace_stats()
        # /runtime-stats over the MEASURED window (R1-B3). Both edges are polled
        # explicitly, because the counters are process-lifetime totals: without
        # a start snapshot there is no window delta, only a total the recreated
        # container happens to have reached.
        #
        # The two boundary polls sit just OUTSIDE the cgroup window (before
        # start, after stop), so this window strictly CONTAINS the cgroup one
        # and their own cost is not charged to the measured CPU delta. Only the
        # 1 Hz polls between them are inside it, which is what `poll_cost`
        # bounds.
        stats_poller = StatsPoller(
            host_url, hz=float(self.slo.warmup.poll_hz or 1),
            pin_core=_housekeeping_core(), phase=PHASE_MEASURED, slot=lang,
        )
        stats_poller.poll_once()
        sampler.start()
        self._assert_container_placement(sampler, env_sut, svc)
        stats_poller.start()
        res = compose.exec_in(
            env_load, compose.SVC_VEGETA,
            ["sh", "-c", f"cd /io && {' '.join(attack)}"], check=False,
        )
        stats_poller.stop()
        windows = sampler.stop()
        stats_poller.poll_once()
        window = windows[lang]
        cache_after = self._redis_keyspace_stats()
        if not res.ok:
            compose.compose(env_sut, ["stop", svc], check=False)
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail=f"attack failed: {res.stderr[-300:]}")

        # pull artifacts: JSON summary + per-second p99 CSV + cgroup CSV.
        rep_proc = compose.exec_in(
            env_load, compose.SVC_VEGETA,
            ["vegeta", "report", "-type=json", out_bin], check=False,
        )
        try:
            report = vegeta.parse_report_proc(rep_proc)
        except RuntimeError as exc:
            compose.compose(env_sut, ["stop", svc], check=False)
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail=str(exc)[:300])

        artifact_dir.mkdir(parents=True, exist_ok=True)
        summary_path = artifact_dir / f"{lang}.summary.json"
        paths.write_text_atomic(summary_path, rep_proc.stdout)

        per_sec = vegeta.encode_per_second_p99(
            exec_prefix=[*env_load.base_argv(), "exec", "-T", compose.SVC_VEGETA],
            bin_in_container=out_bin,
        )
        p99_csv = artifact_dir / f"{lang}.p99_per_second.csv"
        vegeta.write_per_second_p99_csv(per_sec, p99_csv)
        per_sec_values = [v for _, v in per_sec]

        # Window scalars are the HEADLINE inputs (CPU delta, anon p99) and the
        # methodology gates (memory.peak, OOM, throttle deltas): persist them
        # for EVERY sampled cgroup; the raw 50 ms CSV alone loses both edges.
        # The SUT keeps the bare `cpu_mem_<slot>.summary.json` name; the
        # co-sampled cgroups are slot-keyed, because the block directory is
        # shared by both slots (see shared_summary_name).
        cgroup_artifacts: list[Path] = []
        for label, win in windows.items():
            name = (f"cpu_mem_{lang}.summary.json" if label == lang
                    else shared_summary_name(lang, label))
            p = artifact_dir / name
            paths.write_text_atomic(p, json.dumps(win.summary_dict(), indent=2))
            cgroup_artifacts.append(p)
            target = next(t for t in sampler.targets if t.label == label)
            if target.trace and win.samples:
                csv_name = (f"cpu_mem_{lang}.csv" if label == lang
                            else f"cpu_mem_{lang}.{label}.csv")
                csv_path = artifact_dir / csv_name
                win.write_csv(csv_path)
                cgroup_artifacts.append(csv_path)

        # /runtime-stats: the measured window's JSONL trace + summary, and the
        # warmup window's, which carries the heap and collection series the
        # gc_steady gate actually tested. Both are written even when empty: the
        # summary then says `not_measured` with nulls, so an all-failed window
        # is distinguishable from an unpolled one and from a real zero.
        runtime_artifacts = stats_poller.window().write(artifact_dir, lang)
        runtime_artifacts += warm.poll_window.write(artifact_dir, f"{lang}.warmup")
        # The per-second p99/p999 series the flatness gates scored; B2's
        # recalibration cannot start without it.
        warm_q_csv = artifact_dir / f"{lang}.warmup_latency_per_second.csv"
        vegeta.write_per_second_quantiles_csv(warm.quantile_series, warm_q_csv)
        runtime_artifacts.append(warm_q_csv)

        # Redis keyspace hit/miss delta for THIS window (spec/cache.md hit-rate
        # assertion input). Written even when unavailable, with source recorded,
        # so the manifest can say "not measured" instead of implying zero.
        cache_path = artifact_dir / f"cache_stats_{lang}.json"
        paths.write_text_atomic(cache_path, json.dumps(
            _cache_delta(cache_before, cache_after), indent=2))

        # METHODOLOGY: OOM = reported failure (never a silently-degraded probe).
        # Every sampled cgroup is checked, not just the SUT: an OOM kill in
        # PostgreSQL or Redis during a measured window invalidates the probe
        # just as thoroughly. See `oom_labels` for why that is only safe now.
        oomed = oom_labels(windows)
        if oomed:
            compose.compose(env_sut, ["stop", svc], check=False)
            detail = ", ".join(
                f"{label}={windows[label].oom_kill_delta}" for label in oomed
            )
            return ProbeOutcome(
                achieved=0, error_rate=1.0, p99_ms=0, artifact="", ok=False,
                detail=f"OOM kill(s) during the measured window: {detail}",
            )

        warm_path = artifact_dir / f"{lang}.warmup.json"
        warm.major_faults = window.major_faults_delta
        warm.save(warm_path)

        # The MEASURED draw, per probe, as its own artifact. The manifest's
        # working-set section is accumulated from these files rather than from
        # an in-process counter, for the reason every W16 aggregate is: a resume
        # is a new process, and a counter that restarts publishes a number that
        # is silently short on exactly the runs that crashed.
        draw_path = artifact_dir / f"{lang}.target_draw.json"
        paths.write_text_atomic(draw_path, json.dumps(_draw_record(
            drawn, slot=lang, cell_id=cell.cell_id, stage=stage, rate=rate,
            block=block, measured_window_seconds=duration_s,
            probe_draw_seconds=draw_seconds,
            warmup_incs=warm.target_draw_incs,
            containment=containment,
        ), indent=2))

        # checksum the pulled artifacts (probe atomicity). Every cgroup artifact
        # is enumerated from what was actually written: a hand-kept list is how
        # a new artifact becomes the one unchecksummed file in a run where
        # everything else is, which reads as nothing at all. checksums.py drops
        # the trace kinds the run map cannot publish, so both files agree.
        checksums.write_probe_checksums(
            self.run_id, artifact_dir, lang,
            (summary_path, p99_csv, *cgroup_artifacts, *runtime_artifacts,
             cache_path, warm_path, draw_path),
        )

        compose.compose(env_sut, ["stop", svc], check=False, timeout=120)

        rel_artifact = str(summary_path.relative_to(paths.run_dir(self.run_id)))
        return ProbeOutcome(
            achieved=report.rate, error_rate=report.error_rate,
            p99_ms=report.latency_p99_ns / 1e6, artifact=rel_artifact,
            per_second_p99=per_sec_values, ok=True,
            # The gate verdict travels as the LIST, so the ledger can say which
            # gates were evaluated and which flagged the probe without opening
            # this probe's warmup.json (R1-B4). `ProbeOutcome.under_warmed` is
            # derived from it exactly as `WarmupResult.under_warmed` is.
            gates_evaluated=warm.gates_evaluated,
            gates_failed=warm.gates_failed,
            warmup_attack_failed=warm.exit_reason.startswith(EXIT_ATTACK_FAILED_PREFIX),
            # The measured window's stream selector, so the ledger alone is
            # enough to regenerate the exact request sequence this probe faced.
            draw_inc=f"{drawn.inc:032x}",
            draw_seconds=draw_seconds,
        )

    def _redis_keyspace_stats(self) -> dict[str, int | None]:
        """`redis-cli INFO stats` -> keyspace_hits / keyspace_misses (cumulative).

        Cheap (one exec) and taken only at window boundaries. Returns Nones when
        Redis is unreachable so the caller records "not measured".
        """
        res = compose.compose(
            self._env(),
            ["exec", "-T", compose.SVC_REDIS, "redis-cli", "INFO", "stats"],
            check=False, timeout=30,
        )
        if not res.ok:
            return {"keyspace_hits": None, "keyspace_misses": None}
        out: dict[str, int | None] = {"keyspace_hits": None, "keyspace_misses": None}
        for line in res.stdout.splitlines():
            key, _, value = line.partition(":")
            if key.strip() in out:
                try:
                    out[key.strip()] = int(value.strip())
                except ValueError:
                    pass
        return out

    def _scaled_min_calls(self) -> dict[str, int]:
        """spec warmup minimums scaled by the profile (smoke shrinks them)."""
        factor = self.profile.warmup_min_calls_factor
        return {
            name: max(1, int(round(minimum * factor)))
            for name, minimum in self.slo.warmup.per_endpoint_min_calls.items()
        }

    def _warmup_rate(self, min_calls: dict[str, int], cap_s: int) -> int:
        """Fixed sub-knee warmup rate at which the minimums are REACHABLE.

        The spec minimums are per-endpoint; an endpoint with mix share s and
        minimum m needs m/s total requests. The rate must deliver the worst-case
        endpoint inside a TARGET warmup duration (with 10% headroom): otherwise
        every probe is under-warmed by construction and the rung-emptying
        exclusion kills the run at the first rung. Floored at the old
        max(50, start/2) so the rate never drops below the prior behavior.
        Recorded per probe in WarmupResult.warmup_rate (full disclosure).

        The denominator is WARMUP_RATE_TARGET_S, never `cap_s`: the cap is a
        ceiling for the slow case, and letting it set the rate meant a longer
        cap produced SLOWER warmup traffic. `cap_s` is still accepted (and used
        as an upper bound on the target) so a profile with a cap below the
        target cannot ask for a rate it has no time to deliver.
        """
        shares = {name: w / 100.0 for name, w in self.workload.mix.as_pairs()}
        required = 0.0
        for name, minimum in min_calls.items():
            share = shares.get(name, 0.0)
            if share > 0:
                required = max(required, minimum / share)
        floor = max(50, self.profile.ladder_start_rps // 2)
        target_s = min(WARMUP_RATE_TARGET_S, cap_s) if cap_s > 0 else WARMUP_RATE_TARGET_S
        if target_s <= 0 or required <= 0:
            return floor
        return max(floor, int(required / (target_s * 0.9)) + 1)

    def _warmup(
        self, env_load, lang, host_url, cap_s, cell: Cell,
        *, rate: int, stage: str = "", block: int | None = None,
    ) -> WarmupResult:
        """Segmented sub-knee attack while polling /runtime-stats; exits on gates.

        The attack is split into WARMUP_SEGMENT_S slices instead of one blocking
        run of `cap_s`. From WARMUP_MIN_SEGMENTS onward the gate set is evaluated
        at each segment boundary and warmup STOPS at the first boundary where
        every enforced gate passes. `cap_s` therefore behaves as the ceiling
        spec/slo.yaml says it is, and a probe that warms in 90 s costs 90 s.
        """
        min_calls = self._scaled_min_calls()
        # Resolve the posture's gate set ONCE, here, so everything downstream --
        # evaluation, the enforced/under-warmed verdict, and the unimplemented
        # list on the probe record -- reads the same set and the record states
        # which gates this cell was actually held to. Merged in the same place
        # and the same way as `per_endpoint_min_calls` just below.
        gate_set = gate_set_for_cell(
            self.slo.warmup, cell_id=cell.cell_id, cell_gates=cell.warmup_gates
        )
        cfg: WarmupCfg = self.slo.warmup.model_copy(
            update={"per_endpoint_min_calls": min_calls, "gates": gate_set.enforced}
        )
        # A waived gate must leave a trace. Resolving the posture makes the gate
        # vanish from `evaluate()`, and a gate that silently stops being checked
        # is R1-B1.
        unsatisfiable = registry.unsatisfiable_gates_for(lang, cell.extra_env)
        waived = []
        for name, status in gate_set.waived.items():
            if status == STATUS_NOT_APPLICABLE:
                u = unsatisfiable.get(name)
                waived.append(not_applicable_gate(
                    name, cell.cell_id, cell.gate_set_rationale,
                    knob=None if u is None else f"{u.env_var}={u.value}",
                ))
            else:
                waived.append(posture_waived_gate(name, cell.cell_id))
        poller = StatsPoller(
            host_url, hz=float(cfg.poll_hz or 1), pin_core=_housekeeping_core(),
            phase=PHASE_WARMUP, slot=lang,
        )
        # `warm_rate` is the sub-knee rate warmup traffic is OFFERED at, and
        # `rate` is the probe's MEASURED rate. They were one name until W20 gave
        # the draw a stream keyed on the probe identity, and the identity is the
        # measured rate: keying on the warmup rate would collapse every rung of
        # a cell onto one warmup stream.
        warm_rate = self._warmup_rate(min_calls, cap_s)
        state = WarmupState(cfg=cfg, slot=lang, rate=warm_rate)
        exec_prefix = [*env_load.base_argv(), "exec", "-T", compose.SVC_VEGETA]

        quantile_rows: list[tuple[int, int, float, float, int]] = []
        seg_draws: list[DrawnTargets] = []
        n_segments = max(1, math.ceil(cap_s / WARMUP_SEGMENT_S))
        consumed = 0            # poller snapshots already folded into `state`
        segments_run = 0
        gates = state.evaluate(at_seconds=0.0)
        exit_reason = EXIT_CAP_REACHED

        poller.start()
        t0 = time.monotonic()
        try:
            for seg in range(n_segments):
                # The final segment is short when cap_s is not a multiple of the
                # segment length, so the total never exceeds the declared cap.
                seg_s = min(WARMUP_SEGMENT_S, cap_s - seg * WARMUP_SEGMENT_S)
                if seg_s <= 0:
                    break
                out_bin = f"/io/{lang}/warmup.seg{seg}.bin"
                # A stream PER SEGMENT, not per warmup. Segments are separate
                # vegeta invocations against one file, so a single warmup draw
                # would have segment 2 replay segment 1 verbatim -- the same
                # head-of-file defect one level down, and it would leave warmup
                # measuring a cache it had just filled itself.
                w_phase, w_count = warmup_segment_draw(
                    seg=seg, warm_rate=warm_rate, seg_s=seg_s,
                )
                w_drawn = self.draw_and_ship(
                    slot=lang, cell=cell, stage=stage, rate=rate, block=block or 0,
                    phase=w_phase, count=w_count,
                )
                seg_draws.append(w_drawn)
                params = AttackParams(rate=warm_rate, duration_s=seg_s,
                                      max_workers=self.profile.max_workers,
                                      connections=self.profile.connections,
                                      timeout_s=self.profile.timeout_s)
                argv = vegeta.build_attack_argv(
                    targets_in_container=f"{lang}/{w_drawn.targets_path.name}",
                    out_bin_in_container=out_bin, params=params,
                )
                res = compose.exec_in(
                    env_load, compose.SVC_VEGETA,
                    ["sh", "-c", f"cd /io && {' '.join(argv)}"], check=False,
                )
                segments_run = seg + 1
                if not res.ok:
                    # A failed attack leaves empty series, so EVERY gate fails
                    # and the probe would otherwise be recorded warm purely
                    # because it stopped before the cap. Fail it explicitly.
                    exit_reason = f"{EXIT_ATTACK_FAILED_PREFIX}{res.returncode}"
                    break

                snaps = poller.all()
                for snap in snaps[consumed:]:
                    state.observe(snap)
                consumed = len(snaps)
                # Real latency-flatness input: per-second p99/p999 from this
                # segment's .bin, appended so the trailing window spans segment
                # boundaries (the gates would otherwise pass vacuously on empty
                # series).
                try:
                    qseries = vegeta.encode_per_second_buckets(
                        exec_prefix=exec_prefix, bin_in_container=out_bin,
                        qs=(0.99, 0.999),
                    )
                    state.p99_series.extend(v[0] for _, v, _n in qseries)
                    state.p999_series.extend(v[1] for _, v, _n in qseries)
                    # Index-aligned with the two quantile series: the bucket
                    # size decides which order statistic q=0.999 resolved to,
                    # and `p999_flat` enforces only above that resolution floor.
                    state.samples_series.extend(n for _, _v, n in qseries)
                    # Kept with its SEGMENT index so the flushed series has a
                    # monotone time axis: each segment's second index restarts
                    # at 0, and the gates score the concatenation.
                    quantile_rows.extend(
                        (seg, sec, v[0], v[1], n) for sec, v, n in qseries
                    )
                except (OSError, RuntimeError):
                    pass  # gates fall back to the (empty-series) neutral result

                gates = state.evaluate(at_seconds=time.monotonic() - t0)
                if segments_run >= WARMUP_MIN_SEGMENTS and all(
                    g.passed for g in gates if g.enforced
                ):
                    exit_reason = EXIT_GATES_PASSED
                    break
        finally:
            poller.stop()
        elapsed = time.monotonic() - t0

        # `under_warmed` is DERIVED from these records (WarmupResult), not passed
        # in: only ENFORCED gates decide it, and a gate the runner does not
        # implement or the posture waived abstains rather than passing silently
        # or flagging every probe.
        latest = poller.latest()
        draws = warmup_draws(seg_draws)
        return WarmupResult(
            language=lang, cell_id=cell.cell_id, stage=stage, block=block,
            gate_set_id=gate_set.gate_set_id,
            warmup_rate=warm_rate, elapsed_seconds=elapsed,
            thresholds=WarmupThresholds(
                hard_cap_seconds=cap_s,
                flatness_trailing_samples=FLATNESS_TRAILING_SAMPLES,
                mann_kendall_alpha=MANN_KENDALL_ALPHA,
                per_endpoint_min_calls=min_calls,
            ),
            gates=[*gates, *waived],
            segments=segments_run, segment_seconds=WARMUP_SEGMENT_S,
            exit_reason=exit_reason,
            final_db_pool_total=latest.stats.db_pool.total if latest else None,
            final_db_pool_max=latest.stats.db_pool.max if latest else None,
            poll_window=poller.window(),
            quantile_series=quantile_rows,
            target_draw_incs=draws.incs,
            target_draw_seconds=draws.seconds,
            target_draw_invoice_ids=draws.invoice_ids,
        )

    # ----- capacity gate (Linux) ------------------------------------------ #
    def measure_pg_create_knee(
        self, *, out_dir: Path, rungs: int, window_s: int, pg_cores: int | None = None
    ) -> capgate.PgKneeMeasurement:
        """Measure PG's create-tx ceiling via the create-only ladder (Linux).

        Brings up the slot set's REFERENCE SUT + vegeta, drives POST /invoices
        at a geometric ladder, records EVERY rung, and hands the rung list to
        `capacity_gate.pg_knee_from_rungs`, which decides whether the ladder
        measured a knee at all. It does not return a bare float: a ladder that
        breaks on its first rung has measured ONE point, not a PG ceiling. Every
        early-return path below is a NOT-MEASURED outcome, not a fallback number.

        Per-rung PG cpu.stat deltas are MEASURED (W16): `pg_cpu_delta_usec` was
        a declared field that no code could ever fill, because round 1 sampled
        only the SUT cgroup. The sampler now covers PostgreSQL, so each rung
        carries the DB CPU it actually cost, which is the input W26's
        utilization re-gate needs and which the projection rule cannot supply.
        It stays null off Linux, and null is still a disclosed null.

        The rung table is persisted either way: INCLUDING the paths that never
        reach a rung, so `capacity-gate/pg_knee.json` always exists after this
        call and records why the ladder produced nothing.
        """
        cores = pg_cores if pg_cores is not None else capgate._BENCH_PG_CORES
        collected: list[capgate.RungResult] = []

        def _result(detail: str) -> capgate.PgKneeMeasurement:
            """Build the measurement AND persist it (every exit goes through here)."""
            out = capgate.pg_knee_from_rungs(collected, measured_at_cores=cores)
            if detail and not out.measured:
                out.detail = f"{out.detail}; {detail}"
            paths.write_text_atomic(
                out_dir / "pg_knee.json",
                json.dumps({
                    "knee_rps": out.knee_rps, "usable_rungs": out.usable_rungs,
                    "total_rungs": out.total_rungs, "measured_at_cores": cores,
                    "lower_bound": out.lower_bound, "detail": out.detail,
                    "rungs": out.rungs,
                }, indent=2),
            )
            return out

        # The create-only ladder measures PostgreSQL's write ceiling, but it has
        # to drive it THROUGH a SUT, so the result is a per-slot quantity, not a
        # property of PG: Hibernate and EF Core emit different INSERT shapes and
        # different round-trip counts. The driving slot is the slot set's
        # reference side, declared here and recorded in the artifact.
        drive = registry.get_slot(registry.active_slot_set().reference)
        env_sut = self._env(profiles=[drive.profile, compose.PROFILE_LOAD])
        up = compose.compose(env_sut, ["up", "-d", "--wait", drive.service,
                                       compose.SVC_VEGETA], check=False, timeout=600)
        if not up.ok:
            return _result("SUT/vegeta failed to come up for the create-only ladder")
        sut.wait_ready(drive.host_url, timeout_s=180.0)
        env_load = self._env(profiles=[compose.PROFILE_LOAD])
        # ship the create-only targets (already in out_dir from the phase).
        compose.exec_in(env_load, compose.SVC_VEGETA, ["sh", "-c", "mkdir -p /io/cap"], check=False)
        compose.compose(env_load, ["cp", str(out_dir) + "/.", f"{compose.SVC_VEGETA}:/io/cap/"],
                        check=False, timeout=600)
        stop_reason = ""
        for rate in gate_ladder_rates(self.profile, rungs):
            params = AttackParams(rate=rate, duration_s=window_s,
                                  max_workers=self.profile.max_workers,
                                  connections=self.profile.connections,
                                  timeout_s=self.profile.timeout_s)
            argv = vegeta.build_attack_argv(targets_in_container="targets.txt",
                                            out_bin_in_container="/io/cap/cap.bin", params=params)
            pg_sampler = cgroups.MultiSampler(
                [cgroups.SampleTarget.for_container(
                    compose.SVC_POSTGRES, compose.container_name(compose.SVC_POSTGRES),
                    role=cgroups.ROLE_DATABASE, persistent=True,
                )],
                pin_core=_housekeeping_core(),
            )
            pg_sampler.start()
            r = compose.exec_in(env_load, compose.SVC_VEGETA,
                                ["sh", "-c", f"cd /io/cap && {' '.join(argv)}"], check=False)
            pg_window = pg_sampler.stop()[compose.SVC_POSTGRES]
            if not r.ok:
                stop_reason = f"vegeta attack failed at rung {rate} rps"
                break
            rep = compose.exec_in(env_load, compose.SVC_VEGETA,
                                  ["vegeta", "report", "-type=json", "/io/cap/cap.bin"], check=False)
            try:
                report = vegeta.parse_report_proc(rep)
            except RuntimeError as exc:
                stop_reason = f"vegeta report unparseable at rung {rate} rps: {exc}"
                break
            collected.append(capgate.RungResult(
                rate=rate, achieved=report.rate,
                p99_ms=report.latency_p99_ns / 1e6, error_rate=report.error_rate,
                pg_cpu_delta_usec=pg_window.cpu_usage_delta_usec,
            ))
            if (report.rate < rate * capgate.RUNG_TRACKING_TOLERANCE
                    or report.error_rate > capgate.RUNG_ERROR_MAX):
                stop_reason = f"rung {rate} rps stopped tracking (PG ceiling)"
                break
        compose.compose(env_sut, ["stop", drive.service], check=False)
        return _result(stop_reason)

    # ----- equivalence (both SUTs up) ------------------------------------- #
    def run_equivalence(self) -> dict:
        """Run the existing equivalence pytest with both SUTs live.

        Brings both SUTs up under base compose (no resource conflict for the
        functional check), points the suite at their published ports via each
        slot's declared url env, runs it, tears the SUTs down.
        """
        slots = _slots()
        env = self._env(
            bench_overlay=False,
            profiles=[*_slot_profiles(), compose.PROFILE_LOAD],
        )
        up = compose.compose(env, ["up", "-d", "--wait", *_slot_services(),
                                   compose.SVC_VEGETA],
                             check=False, timeout=600)
        if not up.ok:
            return {"ok": False, "detail": f"up both SUTs failed: {up.stderr[-300:]}"}
        for slot in slots:
            try:
                sut.wait_ready(slot.host_url, timeout_s=180.0)
            except TimeoutError as exc:
                return {"ok": False, "detail": str(exc)}
        res = run(
            [sys.executable, "-m", "pytest", "validation/tests/test_equivalence.py",
             "-q", "--no-header"],
            check=False, timeout=900, cwd=str(paths.bench_dir()),
            extra_env={slot.url_env: slot.host_url for slot in slots},
        )
        tail = (res.stdout.strip().splitlines() or [""])[-1]
        # stop every SUT so none is resident for the measured phases.
        compose.compose(env, ["stop", *_slot_services()], check=False)
        return {"ok": res.ok, "detail": tail[-300:]}

    # ----- build-block disclosure (core parallelism + data layer) --------- #
    def read_build(self, lang: str, extra_env: dict[str, str] | None = None):
        """Read the SUT's /runtime-stats build block (one up/poll/stop cycle).

        Used by _record_cell_knobs to populate (and on Linux assert) the pinned
        core count AND the effective data_layer for the cell. Brings the lang's
        SUT up under the bench overlay with the CELL's knob env (so cpuset,
        DATA_LAYER etc. are in effect exactly as the probes will see them),
        polls the build block once, and returns it (None if unavailable).
        """
        slot = registry.get_slot(lang)
        svc = slot.service
        host_url = slot.host_url
        env_sut = self._env(profiles=[slot.profile, compose.PROFILE_LOAD])
        env_sut.extra_env.update(extra_env or {})
        up = compose.compose(
            env_sut, ["up", "-d", "--wait", svc], check=False, timeout=600,
        )
        if not up.ok:
            return None
        try:
            sut.wait_ready(host_url, timeout_s=180.0)
            stats = fetch_once(host_url)
        except (TimeoutError, RuntimeError, OSError):
            return None
        finally:
            # Never leave the knob-read SUT resident on the SUT cpuset; the
            # next probe's fresh-SUT stop also covers this, but a probe of the
            # OTHER language would otherwise run with this one idling alongside.
            compose.compose(env_sut, ["stop", svc], check=False, timeout=120)
        return stats.build

    def read_processor_count(self, lang: str) -> int | None:
        build = self.read_build(lang)
        return None if build is None else build.processor_count
