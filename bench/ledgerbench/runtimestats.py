"""GET /runtime-stats poller (1 Hz) -> snapshots (spec/runtime-stats.md).

Drives the warmup gates (GC-steady, pools-warm, per-endpoint call counts) and
the per-run GC/pool table + effective-config echo. The recorded RESOURCE numbers
always come from cgroup v2 (cgroups.py), never from this endpoint; runtime-stats
is corroboration + warmup signal only (spec/runtime-stats.md).

Models mirror the frozen shape exactly so the warmup controller and manifest
have one code path across both languages (fields a runtime lacks are null).

**What W17 changed, and why it is not just "flush what we poll".** Round 1
polled during WARMUP ONLY and discarded every snapshot; the measured window was
not sampled at all, so GC collection counts and pause totals do not exist in its
artifacts (R1-B3) and the only GC evidence a reader got was a pass/fail gate.
Two things follow. First, the measured window needs its own poller, at the same
boundaries as the cgroup sampler. Second, an end-only flush cannot work: every
counter here is a PROCESS-LIFETIME total and the SUT container is force-recreated
per probe, so a window figure only exists as end-minus-start -- the same
argument cgroups.py makes for cpu.stat, one layer up.

`n_polls` / `n_poll_errors` exist because the loop below swallowed every poll
error with a bare `pass`. An all-failed window was indistinguishable from an
unpolled one, and both were indistinguishable from a window whose counters were
genuinely zero.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .util import paths
from .util.clock import monotonic, rfc3339_micros

NOT_MEASURED = "not_measured"

PHASE_WARMUP = "warmup"
PHASE_MEASURED = "measured"

# Frozen route-template keys for endpoint_calls (spec/runtime-stats.md).
ENDPOINT_KEYS = [
    "GET /invoices/{id}",
    "GET /customers/{id}/invoices",
    "POST /pricing/quote",
    "POST /invoices",
    "POST /invoices/{id}/pdf-stub",
    "GET /healthz",
]


class Build(BaseModel):
    model_config = ConfigDict(extra="allow")
    language: str | None = None
    runtime_version: str | None = None
    service_version: str | None = None
    data_layer: str | None = None
    processor_count: int | None = None


GC_GENERATIONS = ("gen0", "gen1", "gen2")

# Where a snapshot's collection cadence came from. Recorded per probe beside the
# gate verdict, because "the cadence was flat" reads differently depending on
# whether the number counted CLR generations or JVM collector cycles.
CADENCE_GENERATIONS = "generations"
CADENCE_COLLECTOR_CYCLES = "collector_cycles"
CADENCE_UNREPORTED = "unreported"


def sum_present(values: Iterable[tuple[str, int | None]]) -> tuple[int | None, list[str]]:
    """Sum the values a runtime actually reported, and NAME which those were.

    The one convention for "some of this triple is not measured", used by both
    `GcCollections.total()` and `PollWindow.deltas()` so a reader never has to
    ask which of the two a number came from. A null generation contributes
    NOTHING rather than zero, and when every generation is null the answer is
    None -- a runtime that does not publish a count did not collect nothing.
    The companion list is what makes the total readable: `2` summed over
    `["gen0"]` and `2` summed over all three are different findings.
    """
    present = [(name, v) for name, v in values if v is not None]
    if not present:
        return None, []
    return sum(v for _, v in present), [name for name, _ in present]


class GcCollections(BaseModel):
    """Per-generation collection counts, any of which may be a PRESENT NULL.

    Null is the honest JVM answer for all three and it is not a defect to route
    around: measured on this box, G1's old-generation bean counts Full GC only
    and reads 0 where generational ZGC's major-cycle bean reads 3 on identical
    work, so a number in `gen2` means three different things across G1 / ZGC /
    the CLR (`services/java/.../JvmGcBeans.java`, and `registry.JVM_GC`). The
    real JVM figures arrive BESIDE the triple, one key per
    `GarbageCollectorMXBean` name, and land in `model_extra`. `total()` never
    touches them -- it is the generation triple and nothing else. `cadence()`
    does, over a DECLARED subset, because a sum across every bean present
    double-counts: ZGC splits cycles from pauses into separate beans over the
    same pools, and G1's 'G1 Concurrent GC' counts STW sub-pauses.
    """

    # Defaults are None, NEVER 0: `spec/runtime-stats.md` pins every key as
    # present (null where unmapped), so an ABSENT key is a contract violation --
    # and defaulting it to 0 turned `"gc": {}` into three measured zeros, a
    # perfectly flat cadence, and a vacuous `gc_steady` pass on no evidence at
    # all (R1-B1 through the absent-vs-null seam). None routes absent keys into
    # the same not-measured handling a present null gets: `sum_present` skips
    # them and an all-None series is `insufficient_data`, which FLAGS.
    model_config = ConfigDict(extra="allow")
    gen0: int | None = None
    gen1: int | None = None
    gen2: int | None = None

    def total(self) -> int | None:
        """Collections over the generations THIS runtime reports. None if none.

        None, never 0: the warmup GC-steady gate reads this series, and a
        constant 0 is a perfectly flat cadence, i.e. an unmeasured runtime would
        pass the gate on no evidence at all (R1-B1). `warmup.WarmupState` keeps
        the Nones in the series and drops the undefined deltas.
        """
        return sum_present((name, getattr(self, name)) for name in GC_GENERATIONS)[0]

    def generations_present(self) -> list[str]:
        return sum_present((name, getattr(self, name)) for name in GC_GENERATIONS)[1]

    def cadence(
        self, cycle_beans: Sequence[str],
    ) -> tuple[int | None, str, list[str]]:
        """Total collections for the GC-STEADY cadence series, and its basis.

        The declaration W27 makes, and the reason it is not `total()`: with the
        generation triple null the JVM's cadence series is empty at every poll,
        so `gc_steady` returns `insufficient_data` and flags 100% of Java
        probes. The dishonest part was only the gen0/gen1/gen2 MAPPING --
        `GarbageCollectorMXBean` publishes real per-collector cycle counts on
        both collectors, and their sum is the well-defined analog of the CLR's
        gen0+gen1+gen2.

        `cycle_beans` (`registry.GcSemantics.cadence_beans`) is an INCLUSION
        list, never an exclusion one: two of the beans a JVM publishes count
        pause EVENTS over the same pools as the cycle beans (ZGC's '*Pauses';
        G1's 'G1 Concurrent GC'), so a sum over everything present would
        multiply the cadence by three or four. An unrecognised new bean is
        therefore dropped -- which costs a little signal -- rather than silently
        double-counted, and if NOTHING matches the answer is None and the gate
        flags, which is the failure direction this whole module is built around.

        Generations win when a runtime publishes any of them, so the .NET path
        is untouched and no snapshot can be scored on two bases at once.
        """
        total, names = sum_present(
            (name, getattr(self, name)) for name in GC_GENERATIONS
        )
        if total is not None:
            return total, CADENCE_GENERATIONS, names
        extra = self.model_extra or {}
        total, names = sum_present(
            (name, extra[name]) for name in cycle_beans
            if isinstance(extra.get(name), int) and not isinstance(extra[name], bool)
        )
        if total is not None:
            return total, CADENCE_COLLECTOR_CYCLES, names
        return None, CADENCE_UNREPORTED, []


class Gc(BaseModel):
    """Every default is None (see `GcCollections`): absent is not-measured."""

    model_config = ConfigDict(extra="allow")
    collections: GcCollections = Field(default_factory=GcCollections)
    # Nullable on the wire too, not just when absent: `getCollectionTime()` is
    # whole milliseconds and ZGC records every pause as 0 ms (measured on this
    # box: 255 pauses, 0 ms), so the JVM reports a present null under ZGC rather
    # than a constant 0.0 in the one cell where pause time is the variable.
    pause_total_seconds: float | None = None
    heap_in_use_bytes: int | None = None
    heap_committed_bytes: int | None = None
    heap_goal_bytes: int | None = None
    allocated_total_bytes: int | None = None


class DbPool(BaseModel):
    """Every default is None (see `GcCollections`): absent is not-measured."""

    model_config = ConfigDict(extra="allow")
    max: int | None = None
    total: int | None = None
    idle: int | None = None
    in_use: int | None = None
    # Null on the wire from both stacks (neither HikariPoolMXBean nor Npgsql's
    # meters expose cumulative acquire counters); accepted so the rest of the
    # snapshot (total/max/gc), which the warmup gates actually read, still parses.
    acquire_count: int | None = None
    empty_acquire_count: int | None = None
    acquire_wait_total_seconds: float | None = None


class RedisPool(BaseModel):
    """Every default is None (see `GcCollections`): absent is not-measured."""

    model_config = ConfigDict(extra="allow")
    max: int | None = None
    total: int | None = None
    idle: int | None = None
    in_use: int | None = None


class RuntimeStats(BaseModel):
    """One /runtime-stats sample (frozen shape, both languages)."""

    model_config = ConfigDict(extra="allow")
    ts: str | None = None
    uptime_seconds: float | None = None
    build: Build = Field(default_factory=Build)
    config: dict = Field(default_factory=dict)
    gc: Gc = Field(default_factory=Gc)
    db_pool: DbPool = Field(default_factory=DbPool)
    redis_pool: RedisPool = Field(default_factory=RedisPool)
    goroutines_or_threadpool: int | None = None
    endpoint_calls: dict[str, int] = Field(default_factory=dict)

    def endpoint_total(self, key: str) -> int:
        return int(self.endpoint_calls.get(key, 0))


class Snapshot(BaseModel):
    """A timestamped sample as captured by the poller (wall + monotonic).

    `request_ms` is the client-side round trip. It is recorded because polling
    inside the MEASURED window adds SUT-side CPU that lands in the cgroup delta
    while `/runtime-stats` is not one of the frozen `endpoint_calls` routes and
    is not in vegeta's request count either -- so it is cost with no
    denominator. The round trip is an upper bound on the handler's own time,
    which turns that disclosure from an assumed constant into a measured one.
    """

    captured_utc: str
    monotonic_s: float
    stats: RuntimeStats
    request_ms: float | None = None


def fetch_once(base_url: str, *, client: httpx.Client | None = None) -> RuntimeStats:
    own = client is None
    cl = client or httpx.Client(timeout=5.0)
    try:
        resp = cl.get(f"{base_url}/runtime-stats")
        resp.raise_for_status()
        return RuntimeStats.model_validate(resp.json())
    finally:
        if own:
            cl.close()


def _delta(start, end):
    """end - start, or None when either edge is missing.

    None, never 0. A GC counter the runtime does not expose is not a runtime
    that collected nothing -- and two of these fields cannot be mapped honestly
    onto the JVM at all (`gc.collections.gen2` means three different things
    across G1 / ZGC / the CLR, and `gc.pause_total_seconds` is identically 0.0
    under ZGC because the bean is millisecond-granular), so the arithmetic here
    is null-safe.
    """
    if start is None or end is None:
        return None
    return end - start


class PollWindow(BaseModel):
    """One phase's /runtime-stats sampling, with BOTH edges kept.

    Every counter on this endpoint is a process-lifetime total and the SUT
    container is force-recreated per probe, so the window figure is
    end-minus-start and nothing else. `deltas` carries those; `levels` carries
    the quantities that are not cumulative (heap sizes, pool occupancy, thread
    counts), where start / end / max is the honest summary and the per-poll
    series in the JSONL is the trace.
    """

    model_config = ConfigDict(extra="forbid")
    phase: str
    slot: str
    snapshots: list[Snapshot] = Field(default_factory=list)
    n_polls: int = 0
    n_poll_errors: int = 0
    last_error: str = ""

    @property
    def start(self) -> Snapshot | None:
        return self.snapshots[0] if self.snapshots else None

    @property
    def end(self) -> Snapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    @property
    def window_seconds(self) -> float | None:
        if len(self.snapshots) < 2:
            return None
        return self.snapshots[-1].monotonic_s - self.snapshots[0].monotonic_s

    def deltas(self) -> dict:
        """Cumulative counters, end minus start. All null with <2 polls."""
        a, b = self.start, self.end
        if a is None or b is None or a is b:
            return {}
        sa, sb = a.stats, b.stats
        out: dict = {
            "uptime_seconds": _delta(sa.uptime_seconds, sb.uptime_seconds),
            "gc_collections_gen0": _delta(sa.gc.collections.gen0, sb.gc.collections.gen0),
            "gc_collections_gen1": _delta(sa.gc.collections.gen1, sb.gc.collections.gen1),
            "gc_collections_gen2": _delta(sa.gc.collections.gen2, sb.gc.collections.gen2),
            "gc_pause_total_seconds": _delta(sa.gc.pause_total_seconds,
                                             sb.gc.pause_total_seconds),
            "gc_allocated_total_bytes": _delta(sa.gc.allocated_total_bytes,
                                               sb.gc.allocated_total_bytes),
            "db_pool_acquire_count": _delta(sa.db_pool.acquire_count,
                                            sb.db_pool.acquire_count),
            "db_pool_empty_acquire_count": _delta(sa.db_pool.empty_acquire_count,
                                                  sb.db_pool.empty_acquire_count),
            "db_pool_acquire_wait_total_seconds": _delta(
                sa.db_pool.acquire_wait_total_seconds,
                sb.db_pool.acquire_wait_total_seconds),
        }
        # The total is summed from the per-generation DELTAS, not from
        # `GcCollections.total()` on either edge: a generation the runtime does
        # not publish is null on BOTH edges and its delta is null too, and
        # `sum_present` is the single convention for that (see its docstring).
        # Which generations the total actually covers travels beside it rather
        # than being inferred from the number.
        total, summed = sum_present(
            (g, out[f"gc_collections_{g}"]) for g in GC_GENERATIONS
        )
        out["gc_collections_total"] = total
        out["gc_collections_generations_summed"] = summed
        calls = {}
        for key in {*sa.endpoint_calls, *sb.endpoint_calls}:
            calls[key] = _delta(sa.endpoint_total(key), sb.endpoint_total(key))
        out["endpoint_calls"] = calls
        return out

    def levels(self) -> dict:
        """Non-cumulative quantities: start, end, and the in-window max."""
        if not self.snapshots:
            return {}

        def _row(get):
            vals = [get(s.stats) for s in self.snapshots]
            present = [v for v in vals if v is not None]
            return {
                "start": vals[0], "end": vals[-1],
                "max": max(present) if present else None,
            }

        return {
            "gc_heap_in_use_bytes": _row(lambda st: st.gc.heap_in_use_bytes),
            "gc_heap_committed_bytes": _row(lambda st: st.gc.heap_committed_bytes),
            "gc_heap_goal_bytes": _row(lambda st: st.gc.heap_goal_bytes),
            "db_pool_total": _row(lambda st: st.db_pool.total),
            "db_pool_in_use": _row(lambda st: st.db_pool.in_use),
            "db_pool_max": _row(lambda st: st.db_pool.max),
            "redis_pool_total": _row(lambda st: st.redis_pool.total),
            "goroutines_or_threadpool": _row(lambda st: st.goroutines_or_threadpool),
        }

    def poll_cost(self) -> dict:
        """What polling INSIDE the measured window cost, from the artifact.

        The polls' SUT-side CPU lands in the cgroup delta, but `/runtime-stats`
        is not one of the frozen `endpoint_calls` routes and is not in vegeta's
        request count, so it is cost with no denominator: per-request CPU is
        overstated by exactly this. The client-side round trip is an upper
        bound on the handler's own time, so the bound below is measured rather
        than assumed.
        """
        times = [s.request_ms for s in self.snapshots if s.request_ms is not None]
        return {
            "n_polls": self.n_polls,
            "round_trip_ms_total": round(sum(times), 3) if times else None,
            "round_trip_ms_max": round(max(times), 3) if times else None,
            "note": (
                "client-side round trip, an UPPER BOUND on the SUT-side CPU these "
                "polls added to the cgroup delta. /runtime-stats is not a frozen "
                "endpoint_calls route and is not in vegeta's request count, so "
                "this cost has no denominator and per-request CPU is overstated "
                "by at most (round_trip_ms_total / window CPU-ms)."
            ),
        }

    def summary_dict(self) -> dict:
        measured = self.n_polls > 0
        return {
            "phase": self.phase,
            "slot": self.slot,
            "source": (
                "GET /runtime-stats polled at the window boundaries and at 1 Hz "
                "between them; counters are process-lifetime totals and the "
                "published figures are end-minus-start" if measured else NOT_MEASURED
            ),
            "n_polls": self.n_polls,
            "n_poll_errors": self.n_poll_errors,
            "last_error": self.last_error,
            "window_seconds": self.window_seconds,
            # Both edges verbatim, so every delta below is recomputable and the
            # lifetime totals stay visible instead of being replaced by them.
            "start": self.start.model_dump() if self.start else None,
            "end": self.end.model_dump() if self.end else None,
            "deltas": self.deltas(),
            "levels": self.levels(),
            "poll_cost": self.poll_cost(),
            "note": "" if measured else (
                "no /runtime-stats poll succeeded in this window; every field is "
                "null and NOT a measured zero"
            ),
        }

    def jsonl_text(self) -> str:
        """One JSON object per poll, the frozen stats object verbatim.

        Line-per-poll rather than one array: a kill mid-window leaves k valid
        lines instead of one truncated document.
        """
        t0 = self.snapshots[0].monotonic_s if self.snapshots else 0.0
        return "".join(
            json.dumps({
                "phase": self.phase,
                "slot": self.slot,
                "captured_utc": s.captured_utc,
                "monotonic_s": s.monotonic_s,
                "window_offset_s": round(s.monotonic_s - t0, 6),
                "request_ms": s.request_ms,
                "stats": s.stats.model_dump(),
            }, separators=(",", ":")) + "\n"
            for s in self.snapshots
        )

    def write(self, artifact_dir: Path, stem: str) -> list[Path]:
        """`<stem>.runtime_stats.jsonl` (trace) + `.summary.json` (roll-up)."""
        written = []
        summary = artifact_dir / f"{stem}.runtime_stats.summary.json"
        paths.write_text_atomic(summary, json.dumps(self.summary_dict(), indent=2))
        written.append(summary)
        if self.snapshots:
            trace = artifact_dir / f"{stem}.runtime_stats.jsonl"
            paths.write_text_atomic(trace, self.jsonl_text())
            written.append(trace)
        return written


class StatsPoller:
    """Background 1 Hz poller. Thread-based (no asyncio); KISS.

    Collects snapshots into an in-memory list the warmup controller inspects,
    and hands them to `window()` for the per-window flush. Round 1 discarded
    them (R1-B3); the snapshots are the only GC evidence a reader ever gets.

    `pin_core` puts the poller thread on the housekeeping core, the way
    cgroups.MultiSampler does: the CLIENT half of a poll must not run on a SUT
    core. The server half unavoidably does, which is what `PollWindow.poll_cost`
    bounds.
    """

    def __init__(self, base_url: str, *, hz: float = 1.0, pin_core: int | None = None,
                 phase: str = "", slot: str = ""):
        self.base_url = base_url
        self.interval = 1.0 / hz if hz > 0 else 1.0
        self.pin_core = pin_core
        self.phase = phase
        self.slot = slot
        self.snapshots: list[Snapshot] = []
        self.n_poll_errors = 0
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _pin_self(self) -> None:
        if self.pin_core is not None and hasattr(os, "sched_setaffinity"):
            try:
                os.sched_setaffinity(0, {self.pin_core})
            except OSError:
                pass

    def poll_once(self, client: httpx.Client | None = None) -> Snapshot | None:
        """One poll, recorded. None on failure, with the reason counted.

        Extracted so the error accounting is reachable without a thread and
        without a live SUT: a bare `except: pass` inside a loop no test can
        enter is how an all-failed window became indistinguishable from an
        unpolled one in the first place.
        """
        t0 = monotonic()
        try:
            stats = fetch_once(self.base_url, client=client)
        except (httpx.HTTPError, ValueError) as exc:
            # transient HTTP, or a payload the frozen model rejects: skip this
            # tick (warmup degrades to under-warmed rather than killing the
            # poller thread). `ValueError` covers pydantic's `ValidationError`,
            # which is why a model that refused the JVM's present nulls did not
            # crash the run -- it silently discarded every snapshot of the Java
            # slot instead. Nothing here can distinguish "the SUT is not up yet"
            # from "this body never parses", which is what `n_poll_errors` and
            # `last_error` are for.
            with self._lock:
                self.n_poll_errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return None
        snap = Snapshot(
            captured_utc=rfc3339_micros(),
            monotonic_s=monotonic(),
            stats=stats,
            request_ms=(monotonic() - t0) * 1000.0,
        )
        with self._lock:
            self.snapshots.append(snap)
        return snap

    def _loop(self) -> None:
        self._pin_self()
        with httpx.Client(timeout=5.0) as client:
            next_t = monotonic()
            while not self._stop.is_set():
                self.poll_once(client)
                next_t += self.interval
                sleep_for = next_t - monotonic()
                if sleep_for > 0:
                    self._stop.wait(sleep_for)
                else:
                    next_t = monotonic()  # fell behind; resync

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("poller already started")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="runtime-stats-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def window(self) -> PollWindow:
        """This poller's window, ready to flush."""
        with self._lock:
            return PollWindow(
                phase=self.phase, slot=self.slot, snapshots=list(self.snapshots),
                n_polls=len(self.snapshots), n_poll_errors=self.n_poll_errors,
                last_error=self.last_error,
            )

    def latest(self) -> Snapshot | None:
        with self._lock:
            return self.snapshots[-1] if self.snapshots else None

    def all(self) -> list[Snapshot]:
        with self._lock:
            return list(self.snapshots)
