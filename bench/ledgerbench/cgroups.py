"""cgroup v2 sampler: ONE thread, N cgroups, 50 ms ticks.

Path resolution is NEVER hardcoded. A container's cgroup comes from
`docker inspect` -> PID -> /proc/<pid>/cgroup unified `0::<path>` line ->
/sys/fs/cgroup/<path>/, resolved once per probe (a fresh container is a fresh
path). A host systemd unit has no container PID at all, so its path comes from
`systemctl show -p ControlGroup` instead.

ONE thread samples EVERY target. Round 1 sampled one container with one thread;
W16 adds five more cgroups, and one-thread-per-container would put six threads
on the single housekeeping core they all self-pin to (live_runner pins them
there so their 50 ms reads never land on SUT cores). Measured on this box:
reading cpu.stat + memory.stat for five cgroups costs 0.21 ms per tick -- a
0.4% duty cycle at 50 ms, i.e. the whole sampled set costs less than the
scheduler churn of the threads it replaces. cpu.stat is parsed ONCE per target
per tick for all three of its fields; round 1 opened and re-parsed it three
times.

WHY THE COUNTERS ARE DELTAS. cpu.stat's nr_throttled/throttled_usec and
memory.events' oom_kill are CUMULATIVE over a cgroup's lifetime. The SUT
container is force-recreated per probe, so reading them at window end happened
to equal that window's own count. postgres, redis, vegeta and the daemons
persist for the WHOLE RUN, so an end-only read summed across windows
(`phases._finalize_manifest` sums exactly these two fields) multiplies one
lifetime total by the window count and publishes a measured pathology that
never happened -- and `oom_kill` has the same shape, where live_runner's OOM
abort would then latch: every probe after the first aborts on history, resume
re-runs it against the same latched counter, and the run cannot recover.

Every cumulative counter is therefore read at BOTH window edges and the
artifacts carry the delta. That is a strict improvement for the SUT too, not a
concession to the new targets: the sampler starts after warmup AND after the
head-of-window discard, so a delta is the measured window alone, where the
end-only read covered the whole container's life including both.

`memory.peak` is the one that cannot become a delta: it is a high-water mark,
and the difference of two high-water marks is not a window figure. Both edges
are recorded under names that say so. It IS resettable on this kernel (the
write landed in 6.8; this box runs 7.1) and resetting is deliberately refused:
it needs root for a root-owned container cgroup, and clearing a persistent
container's peak mid-run destroys the run-level high-water every other window
reports. `memory.current` is sampled instead -- it is the only window-scoped
memory statement a never-recreated container has, and for PostgreSQL it is the
only honest one at all, because `memory.stat anon` excludes `shared_buffers`
(shmem/page cache) and would publish a small number under a plausible name.

Windows / non-Linux: identical interface, every target `enabled=False` with
`"disabled (non-Linux)"`, so a smoke run exercises the whole path with one code
path in the runner.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .compose import container_pid
from .util import paths
from .util.cpuset import parse_cpuset
from .util.proc import run

IS_LINUX = sys.platform.startswith("linux")
CGROUP_ROOT = Path("/sys/fs/cgroup")
SAMPLE_INTERVAL_S = 0.050  # 50 ms

NOT_MEASURED = "not_measured"

# systemd units carrying the container plumbing. Measured on this box (Docker
# 29.7, cgroup driver `systemd`, cgroup v2, kernel 7.1): a container lands in
# /system.slice/docker-<id>.scope, dockerd in /system.slice/docker.service and
# the containerd-shim in /system.slice/containerd.service. Two consequences,
# both of which the artifacts must state rather than leave to be inferred:
# containers do NOT nest inside either unit, so the daemon rows do not
# double-count the SUT; and sampling docker.service ALONE under-counts the
# runtime by exactly the shim -- the process the SUT's json-file stdout is
# relayed through, i.e. the hot logging path. Both units are sampled.
DOCKER_UNIT = "docker.service"
CONTAINERD_UNIT = "containerd.service"

# What a sampled cgroup IS, so the report can separate a per-request cost from
# measurement apparatus without pattern-matching on names.
ROLE_SUT = "sut"
ROLE_DATABASE = "database"
ROLE_CACHE = "cache"
ROLE_LOAD_GENERATOR = "load_generator"
ROLE_CONTAINER_RUNTIME = "container_runtime"

# Roles whose CPU may NEVER enter a per-request cost. The load generator and the
# container runtime are the apparatus: their cost is real, is worth publishing,
# and dividing it by the request count would price our own instrumentation as
# if the stack under test had spent it.
OVERHEAD_ROLES = frozenset({ROLE_LOAD_GENERATOR, ROLE_CONTAINER_RUNTIME})

# cpu.stat / memory.stat / memory.events keys read per tick or per edge. Named
# so the single parse below cannot drift from what the dataclasses expect.
_CPU_KEYS = frozenset({"usage_usec", "nr_throttled", "throttled_usec"})
_MEM_TICK_KEYS = frozenset({"anon"})
_MEM_EDGE_KEYS = frozenset({"anon", "file", "pgmajfault", "pgfault"})


# --------------------------------------------------------------------------- #
# path resolution
# --------------------------------------------------------------------------- #
def resolve_cgroup_path(container_name: str, *, pid: int | None = None) -> Path | None:
    """Resolve the unified cgroup dir for a container. Linux only.

    Returns None on non-Linux or if resolution fails (caller treats as no-op).
    """
    if not IS_LINUX:
        return None
    if pid is None:
        pid = container_pid(container_name)
    if not pid:
        return None
    cgline = Path(f"/proc/{pid}/cgroup")
    if not cgline.exists():
        return None
    for line in cgline.read_text().splitlines():
        # unified v2 line: "0::<path>"
        if line.startswith("0::"):
            rel = line.split("::", 1)[1].lstrip("/")
            return CGROUP_ROOT / rel
    return None


def resolve_systemd_cgroup_path(unit: str) -> Path | None:
    """Resolve a host systemd unit's cgroup dir. Linux only, None when unknown.

    A unit is not a container: there is no `docker inspect` PID to walk from,
    and matching processes by name is not identity (the daemon, the shim and
    the CLI all match "containerd"). systemd already owns the answer, so ask it.

    None -- never a guessed path -- when systemctl is absent, the unit is not
    loaded, or it reports the root cgroup `/`, which is what a unit with no
    delegated cgroup returns and would otherwise silently sample THE WHOLE HOST
    and publish it as the daemon's cost.
    """
    if not IS_LINUX:
        return None
    res = run(
        ["systemctl", "show", unit, "--property=ControlGroup", "--value"],
        check=False, timeout=10,
    )
    if not res.ok:
        return None
    rel = res.stdout.strip()
    if not rel or rel == "/":
        return None
    path = CGROUP_ROOT / rel.lstrip("/")
    return path if (path / "cpu.stat").exists() else None


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def read_flat_keyed(path: Path, keys: Iterable[str]) -> dict[str, int | None]:
    """`<key> <int>` lines -> the requested keys, in ONE open + ONE parse.

    Missing keys come back None, never 0: `nr_throttled` absent because the
    controller is not enabled is not the same claim as "never throttled".
    """
    out: dict[str, int | None] = {k: None for k in keys}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in out:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def _read_single_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# what is sampled
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SampleTarget:
    """One cgroup to sample, and what it is.

    `persistent` is the load-bearing flag: it says this cgroup OUTLIVES the
    probe window, so every cumulative counter it reports has history in it. It
    does not change how the sampler reads (both kinds are delta'd); it travels
    into the artifact so a reader never has to infer from a container name
    whether `memory.peak` covers one probe or the whole run.

    `trace` selects which targets get a 50 ms CSV. The rule, stated once: a
    target whose sampled SERIES backs a published figure is traced (the SUT's
    anon p99, PostgreSQL's in-window memory level); a target whose only
    published figure is a window scalar is not, because that scalar and both
    its raw edges are already in the summary and 2,400 unread rows per probe
    per target is 300 MB of trace nobody can check anything with.
    """

    label: str
    path: Path | None
    role: str
    persistent: bool
    trace: bool = False
    source: str = ""
    # The docker container name, when the target IS a container: what a
    # `docker update` repair addresses. Empty for systemd units and explicit
    # fixture paths, which are never repairable that way.
    container_name: str = ""

    def __post_init__(self) -> None:
        # The label is a filename segment (`cpu_mem_<slot>.<label>.summary.json`)
        # and the manifest's row key, so it takes the same restriction
        # registry.Slot puts on a slot id, for the same reason: a dot makes the
        # name ambiguous to split and a slash escapes the artifact directory,
        # both silently.
        if not self.label or "." in self.label or "/" in self.label:
            raise ValueError(
                f"sample label {self.label!r} must be non-empty and contain "
                "neither '.' (the artifact-name separator) nor '/'"
            )

    @property
    def enabled(self) -> bool:
        return self.path is not None

    @classmethod
    def for_path(
        cls, label: str, path: Path | None, *, role: str, persistent: bool,
        trace: bool = False, source: str = "explicit cgroup path",
    ) -> SampleTarget:
        """A target from an already-resolved path.

        This is what makes the sampler testable without docker and without
        root: a fixture cgroup tree under tmp_path is sampled through exactly
        the code a real run uses. Logic reachable only through `docker inspect`
        cannot be reached by the suite.
        """
        return cls(label=label, path=path, role=role, persistent=persistent,
                   trace=trace, source=source)

    @classmethod
    def for_container(
        cls, label: str, container_name: str, *, role: str, persistent: bool,
        trace: bool = False, pid: int | None = None,
    ) -> SampleTarget:
        path = resolve_cgroup_path(container_name, pid=pid)
        return cls(
            label=label, path=path, role=role, persistent=persistent, trace=trace,
            source=(f"docker inspect {container_name} -> /proc/<pid>/cgroup"
                    if path is not None
                    else f"{NOT_MEASURED}: no cgroup for container {container_name}"),
            container_name=container_name,
        )

    @classmethod
    def for_unit(
        cls, label: str, unit: str, *, role: str, trace: bool = False,
    ) -> SampleTarget:
        """A host systemd unit. Always persistent: it outlives every probe."""
        path = resolve_systemd_cgroup_path(unit)
        return cls(
            label=label, path=path, role=role, persistent=True, trace=trace,
            source=(f"systemctl show {unit} --property=ControlGroup"
                    if path is not None
                    else f"{NOT_MEASURED}: systemd unit {unit} has no readable cgroup"),
        )


@dataclass
class Sample:
    monotonic_s: float
    cpu_usage_usec: int | None
    nr_throttled: int | None
    throttled_usec: int | None
    mem_anon: int | None
    mem_current: int | None


@dataclass
class Edge:
    """Everything read once at a window boundary, for one target.

    Cumulative fields are LIFETIME totals as the kernel reports them; the deltas
    the artifacts publish are computed from a pair of these, and both edges stay
    in the artifact so every published delta is recomputable.
    """

    monotonic_s: float = 0.0
    cpu_usage_usec: int | None = None
    nr_throttled: int | None = None
    throttled_usec: int | None = None
    oom_kill: int | None = None
    mem_peak: int | None = None
    mem_current: int | None = None
    mem_anon: int | None = None
    mem_file: int | None = None
    swap_current: int | None = None
    swap_peak: int | None = None
    pgmajfault: int | None = None
    pgfault: int | None = None

    def to_dict(self) -> dict:
        return {
            "monotonic_s": self.monotonic_s,
            "cpu_usage_usec": self.cpu_usage_usec,
            "nr_throttled": self.nr_throttled,
            "throttled_usec": self.throttled_usec,
            "oom_kill": self.oom_kill,
            "mem_peak": self.mem_peak,
            "mem_current": self.mem_current,
            "mem_anon": self.mem_anon,
            "mem_file": self.mem_file,
            "swap_current": self.swap_current,
            "swap_peak": self.swap_peak,
            "pgmajfault": self.pgmajfault,
            "pgfault": self.pgfault,
        }


def read_edge(cg: Path) -> Edge:
    cpu = read_flat_keyed(cg / "cpu.stat", _CPU_KEYS)
    mem = read_flat_keyed(cg / "memory.stat", _MEM_EDGE_KEYS)
    events = read_flat_keyed(cg / "memory.events", ("oom_kill",))
    return Edge(
        monotonic_s=time.monotonic(),
        cpu_usage_usec=cpu["usage_usec"],
        nr_throttled=cpu["nr_throttled"],
        throttled_usec=cpu["throttled_usec"],
        oom_kill=events["oom_kill"],
        mem_peak=_read_single_int(cg / "memory.peak"),
        mem_current=_read_single_int(cg / "memory.current"),
        mem_anon=mem["anon"],
        mem_file=mem["file"],
        # THE evidence that the anon numbers are whole. A page swapped out
        # leaves `memory.stat anon`, so a container that swapped publishes a
        # smaller `mem_anon_p99` and nothing else in the artifact says why --
        # the headline is steady-state memory per request. compose.bench.yaml
        # pins memswap_limit to mem_limit so `memory.swap.max` is 0 and this
        # can only read 0; that is the point, and reading it is how the run
        # evidences the setting bit rather than asserting it.
        swap_current=_read_single_int(cg / "memory.swap.current"),
        # `swap_current` is a LEVEL at two instants; this is the kernel's
        # high-water mark over the cgroup's lifetime, and it is why the pair is
        # read rather than the level alone. A container that swapped 4 GiB at
        # t=30 s and faulted it all back by t=110 s reads 0 at both edges while
        # `mem_anon_p99` -- sampled every 50 ms -- was depressed for 80 s, which
        # is precisely the failure the swap read exists to catch. Same argument
        # as `memory.peak` above, same cost: one open per edge.
        swap_peak=_read_single_int(cg / "memory.swap.peak"),
        pgmajfault=mem["pgmajfault"],
        pgfault=mem["pgfault"],
    )


def _delta(start: int | None, end: int | None) -> int | None:
    """end - start, or None when either edge is unreadable.

    None, never 0: an unreadable counter is not a measured absence of events.
    """
    return None if start is None or end is None else end - start


# --------------------------------------------------------------------------- #
# window result
# --------------------------------------------------------------------------- #
@dataclass
class WindowResult:
    """One target's measured window.

    `label`, not `container`: the sampled set now includes host systemd units,
    which are not containers, and calling a unit's row `container` would put a
    false noun on the one field every downstream aggregation keys by.
    """

    label: str
    enabled: bool
    role: str = ""
    persistent: bool = False
    source: str = ""
    cgroup_path: str = ""
    cpuset_effective: str = ""
    mem_limit_bytes: int | None = None
    mem_swap_max: str = ""
    samples: list[Sample] = field(default_factory=list)
    start: Edge | None = None
    end: Edge | None = None
    cpu_usage_delta_usec: int | None = None
    nr_throttled_delta: int | None = None
    throttled_usec_delta: int | None = None
    oom_kill_delta: int | None = None
    major_faults_delta: int | None = None
    minor_faults_delta: int | None = None
    mem_anon_p99: int | None = None
    mem_current_p99: int | None = None
    ticks: int = 0
    tick_overruns: int = 0
    note: str = ""

    @property
    def window_seconds(self) -> float | None:
        if self.start is None or self.end is None:
            return None
        return self.end.monotonic_s - self.start.monotonic_s

    @property
    def swap_current_max(self) -> int | None:
        """The larger of the two edges' `memory.swap.current`, or None.

        A LEVEL, not a delta: swap usage falls when pages are faulted back in,
        so end-minus-start goes negative and means nothing. The max over the
        edges is the claim the artifact can actually support -- "this cgroup
        held at least this many swapped bytes at a window boundary" -- and 0
        across every window is what makes the anon figures whole. None (never 0)
        when neither edge was readable.
        """
        edges = [e.swap_current for e in (self.start, self.end)
                 if e is not None and e.swap_current is not None]
        return max(edges) if edges else None

    @property
    def swap_peak_max(self) -> int | None:
        """The larger of the two edges' `memory.swap.peak`, or None.

        The half of the swap evidence that survives a swap-in: the level above
        is 0 at both edges for a window that swapped in the middle and faulted
        everything back, and this is not. Lifetime-scoped like `memory.peak`,
        so for the SUT -- recreated per probe -- it covers that probe's warmup
        plus its window, and for a persistent cgroup it covers the whole run.
        None (never 0) when neither edge was readable.
        """
        edges = [e.swap_peak for e in (self.start, self.end)
                 if e is not None and e.swap_peak is not None]
        return max(edges) if edges else None

    def summary_dict(self) -> dict:
        """THE per-window artifact, owned here rather than in the runner.

        An inline dict inside `live_runner.run_probe` would sit behind a
        docker-only path no test can reach. Building it here means the delta
        semantics are asserted by the suite, not by review.
        """
        return {
            "label": self.label,
            "role": self.role,
            "persistent": self.persistent,
            "enabled": self.enabled,
            "cgroup_path": self.cgroup_path,
            "path_source": self.source,
            # Container ground truth, read once at window start: the placement
            # and ceilings the kernel actually applied, which is what
            # manifest.topology is built from. `memory.swap.max` is kept RAW --
            # "max", "0" or a byte count are three different claims and an int
            # cannot carry the first.
            "cpuset_effective": self.cpuset_effective,
            "mem_limit_bytes": self.mem_limit_bytes,
            "mem_swap_max": self.mem_swap_max,
            "window_seconds": self.window_seconds,
            "ticks": self.ticks,
            "tick_overruns": self.tick_overruns,
            # Window deltas: what every downstream sum must use.
            "cpu_usage_delta_usec": self.cpu_usage_delta_usec,
            "nr_throttled_delta": self.nr_throttled_delta,
            "throttled_usec_delta": self.throttled_usec_delta,
            "oom_kill_delta": self.oom_kill_delta,
            "major_faults_delta": self.major_faults_delta,
            "minor_faults_delta": self.minor_faults_delta,
            # In-window levels (sampled) and the high-water marks, which are NOT
            # deltas and are named so a reader cannot subtract them.
            "mem_anon_p99": self.mem_anon_p99,
            "mem_current_p99": self.mem_current_p99,
            "mem_peak_start": self.start.mem_peak if self.start else None,
            "mem_peak_end": self.end.mem_peak if self.end else None,
            "mem_current_start": self.start.mem_current if self.start else None,
            "mem_current_end": self.end.mem_current if self.end else None,
            "mem_file_end": self.end.mem_file if self.end else None,
            # `memory.swap.max` above is the SETTING; this is whether it bit.
            "swap_current_start": self.start.swap_current if self.start else None,
            "swap_current_end": self.end.swap_current if self.end else None,
            "swap_current_max": self.swap_current_max,
            "swap_peak_start": self.start.swap_peak if self.start else None,
            "swap_peak_end": self.end.swap_peak if self.end else None,
            "swap_peak_max": self.swap_peak_max,
            # Raw edges, so every published delta is recomputable from the
            # artifact and the lifetime totals stay visible instead of being
            # silently replaced by the window figure.
            "edges": {
                "start": self.start.to_dict() if self.start else None,
                "end": self.end.to_dict() if self.end else None,
            },
            "note": self.note,
        }

    def csv_lines(self) -> str:
        head = ("monotonic_s,cpu_usage_usec,nr_throttled,throttled_usec,"
                "mem_anon,mem_current")

        def _f(v: int | None) -> str:
            return "" if v is None else str(v)

        rows = [
            f"{s.monotonic_s:.6f},{_f(s.cpu_usage_usec)},{_f(s.nr_throttled)},"
            f"{_f(s.throttled_usec)},{_f(s.mem_anon)},{_f(s.mem_current)}"
            for s in self.samples
        ]
        return "\n".join([head, *rows]) + "\n"

    def write_csv(self, path: Path) -> None:
        paths.write_text_atomic(path, self.csv_lines())


# --------------------------------------------------------------------------- #
# sampler
# --------------------------------------------------------------------------- #
class MultiSampler:
    """50 ms cgroup v2 sampler over N targets, on ONE thread.

    Targets are independent: a cgroup that failed to resolve comes back
    `enabled=False` carrying its own reason and does not stop the others -- an
    unresolvable dockerd must never silently disable the SUT's measurement. The
    thread self-pins to the housekeeping core so its reads never run on SUT
    cores (`live_runner._housekeeping_core`).

    Reading /sys and /proc charges the READING process, so sampling PostgreSQL
    does not inflate PostgreSQL's own cpu.stat. What the sampler costs is
    charged to the runner on the housekeeping core, which is why it is pinned
    there and why every path is resolved BEFORE `start()` -- path resolution
    shells out to `docker inspect` and `systemctl`, and neither may run inside a
    measured window.
    """

    def __init__(self, targets: Sequence[SampleTarget], *, pin_core: int | None = None):
        seen: set[str] = set()
        for t in targets:
            if t.label in seen:
                raise ValueError(
                    f"duplicate sample label {t.label!r}: labels key the per-window "
                    "artifact names and the manifest rows, so two targets sharing "
                    "one would overwrite each other silently"
                )
            seen.add(t.label)
        self.targets = tuple(targets)
        self.pin_core = pin_core
        self._live = tuple(t for t in self.targets if t.enabled)
        self._samples: dict[str, list[Sample]] = {t.label: [] for t in self._live}
        self._start: dict[str, Edge] = {}
        self._cpuset: dict[str, str] = {}
        # Both forms of `memory.max`: the int the artifact publishes, and the
        # raw string, because "max" and an unreadable file are the same None
        # and only one of them is a container that came up with no ceiling.
        self._mem_max_raw: dict[str, str] = {}
        self._mem_max: dict[str, int | None] = {}
        self._swap_max: dict[str, str] = {}
        self._ticks = 0
        self._overruns = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._live)

    def _pin_self(self) -> None:
        if self.pin_core is not None and hasattr(os, "sched_setaffinity"):
            try:
                os.sched_setaffinity(0, {self.pin_core})
            except OSError:
                pass

    def _tick(self) -> None:
        now = time.monotonic()
        for t in self._live:
            assert t.path is not None
            cpu = read_flat_keyed(t.path / "cpu.stat", _CPU_KEYS)
            mem = read_flat_keyed(t.path / "memory.stat", _MEM_TICK_KEYS)
            self._samples[t.label].append(Sample(
                monotonic_s=now,
                cpu_usage_usec=cpu["usage_usec"],
                nr_throttled=cpu["nr_throttled"],
                throttled_usec=cpu["throttled_usec"],
                mem_anon=mem["anon"],
                mem_current=_read_single_int(t.path / "memory.current"),
            ))
        self._ticks += 1

    def _loop(self) -> None:
        self._pin_self()
        next_t = time.monotonic()
        while not self._stop.is_set():
            self._tick()
            next_t += SAMPLE_INTERVAL_S
            delay = next_t - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                # Fell behind. Round 1 resynced silently, so a starved sampler
                # showed up only as a thinner memory series -- indistinguishable
                # from a short window. Count it instead.
                self._overruns += 1
                next_t = time.monotonic()

    def start(self) -> None:
        if not self.enabled:
            return
        for t in self._live:
            assert t.path is not None
            self._start[t.label] = read_edge(t.path)
            # Read once, at the start: the DB-CPU column is uninterpretable
            # without the core budget the delta was spent against, and W26's
            # re-gate rule is exactly `cpu delta / (cores * window_seconds)`.
            # The ceilings ride along because this is the only moment the
            # containers exist -- the SUT is recreated per probe, so no phase
            # boundary can read them (B6 row 10).
            self._cpuset[t.label] = _read_str(t.path / "cpuset.cpus.effective")
            raw_mem_max = _read_str(t.path / "memory.max")
            self._mem_max_raw[t.label] = raw_mem_max
            self._mem_max[t.label] = (
                int(raw_mem_max) if raw_mem_max.isdigit() else None)
            self._swap_max[t.label] = _read_str(t.path / "memory.swap.max")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cgroup-sampler", daemon=True,
        )
        self._thread.start()

    def cpuset_mismatches(self, expected_by_role: Mapping[str, str]) -> list[str]:
        """Targets whose `cpuset.cpus.effective` is not the set the overlay asked for.

        B6 row 10, at the ONLY moment it is cheap. `start()` has just read every
        container's own cpuset, and this is the run's first window, so a slot
        missing from compose.bench.yaml (running unpinned across every logical
        core while the manifest records a 6-core budget) costs one probe to
        find instead of fifteen hours. Every CPU-ms-per-request and
        memory-per-request figure in the cell is invalid if it is wrong, so the
        caller raises; `_stamp_topology`'s finalization-time record is the
        backstop for a cpuset that moved after this check, not the check.

        Compared as SETS: `0-5` and `0,1,2,3,4,5` are the same placement.
        A role with no declared expectation (the container-runtime units, pinned
        by the systemd drop-in rather than a compose cpuset) and a target whose
        cpuset did not read are both skipped -- an unreadable cpuset is not a
        wrong one.
        """
        bad: list[str] = []
        for t in self._live:
            want = expected_by_role.get(t.role)
            got = self._cpuset.get(t.label, "")
            if not want or not got:
                continue
            try:
                if set(parse_cpuset(got)) != set(parse_cpuset(want)):
                    bad.append(f"{t.label} ({t.role}): cpuset.cpus.effective={got}, "
                               f"compose asked for {want}")
            except ValueError:
                bad.append(f"{t.label} ({t.role}): unparseable cpuset {got!r}")
        return bad

    def limit_mismatches(self, expected_bytes_by_role: Mapping[str, int]) -> list[str]:
        """Targets whose memory ceiling is not the one the overlay asked for.

        The other half of B6 row 10, checked where the cpuset is checked and for
        the same reason: `start()` has already read `memory.max` and
        `memory.swap.max` off every container, and a wrong ceiling invalidates
        the same figures a wrong cpuset does. It is not cosmetic -- both
        runtimes size their heap from the cgroup limit (PROGRESS.md, "The heap
        question"), so a container that came up unlimited is running a different
        experiment, and memory per request is a headline metric.

        `memory.swap.max` must be `0`: infra/compose.bench.yaml sets
        `memswap_limit` from the SAME variable as `mem_limit`, and docker's swap
        allowance is memswap minus mem. A non-zero one is a container that CAN
        swap, which understates `mem_anon_p99` with nothing in the artifact to
        say so.

        A role with no declared ceiling (the container-runtime units, which have
        no compose entry) is skipped, and so is a file that did not read -- an
        unreadable ceiling is not a wrong one. `memory.max` reading the literal
        `max` is NOT unreadable: that is the unlimited container this exists to
        catch, and it is reported by its raw value.
        """
        bad: list[str] = []
        for t in self._live:
            want = expected_bytes_by_role.get(t.role)
            if not want:
                continue
            raw = self._mem_max_raw.get(t.label, "")
            got = self._mem_max.get(t.label)
            if raw and got != want:
                bad.append(f"{t.label} ({t.role}): memory.max={raw}, compose asked "
                           f"for {want}")
            swap_max = self._swap_max.get(t.label, "")
            if swap_max and swap_max != "0":
                bad.append(f"{t.label} ({t.role}): memory.swap.max={swap_max}, but "
                           f"compose sets memswap_limit from the same variable as "
                           f"mem_limit, so the allowance must be 0")
        return bad

    def swap_allowance_misconfigured(self) -> list[SampleTarget]:
        """Container targets whose `memory.swap.max` read nonzero at start().

        Observed on Docker 29.2.1 under the systemd cgroup driver:
        `memory.swap.max=max` is intermittently left on container CREATE even
        though HostConfig has MemorySwap == Memory (on a resume, the recreate
        of postgres and redis both came up unlimited while the original
        creates were correct). `docker update --memory X
        --memory-swap X` rewrites both the cgroup and the stored unit property,
        so the caller repairs-and-verifies instead of aborting a multi-hour run
        on a create-path fault. Only targets with a `container_name` qualify --
        a systemd unit is not docker-updatable.
        """
        return [
            t for t in self._live
            if t.container_name and self._swap_max.get(t.label, "") not in ("", "0")
        ]

    def reread_placement(self) -> None:
        """Re-read the placement files after a repair, same reads as start()."""
        for t in self._live:
            assert t.path is not None
            self._cpuset[t.label] = _read_str(t.path / "cpuset.cpus.effective")
            raw_mem_max = _read_str(t.path / "memory.max")
            self._mem_max_raw[t.label] = raw_mem_max
            self._mem_max[t.label] = (
                int(raw_mem_max) if raw_mem_max.isdigit() else None)
            self._swap_max[t.label] = _read_str(t.path / "memory.swap.max")

    def stop(self) -> dict[str, WindowResult]:
        truncated = ""
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                truncated = ("sampler thread did not exit within 5 s; the sample "
                             "series is truncated")
            self._thread = None
        out: dict[str, WindowResult] = {}
        for t in self.targets:
            if not t.enabled:
                out[t.label] = disabled_result(
                    t.label, role=t.role, persistent=t.persistent, source=t.source,
                )
                continue
            assert t.path is not None
            start = self._start.get(t.label)
            end = read_edge(t.path)
            samples = list(self._samples[t.label])
            out[t.label] = WindowResult(
                label=t.label,
                enabled=True,
                role=t.role,
                persistent=t.persistent,
                source=t.source,
                cgroup_path=str(t.path),
                cpuset_effective=self._cpuset.get(t.label, ""),
                mem_limit_bytes=self._mem_max.get(t.label),
                mem_swap_max=self._swap_max.get(t.label, ""),
                samples=samples,
                start=start,
                end=end,
                cpu_usage_delta_usec=_delta(
                    start.cpu_usage_usec if start else None, end.cpu_usage_usec),
                nr_throttled_delta=_delta(
                    start.nr_throttled if start else None, end.nr_throttled),
                throttled_usec_delta=_delta(
                    start.throttled_usec if start else None, end.throttled_usec),
                oom_kill_delta=_delta(
                    start.oom_kill if start else None, end.oom_kill),
                major_faults_delta=_delta(
                    start.pgmajfault if start else None, end.pgmajfault),
                minor_faults_delta=_delta(
                    start.pgfault if start else None, end.pgfault),
                mem_anon_p99=_percentile(
                    sorted(s.mem_anon for s in samples if s.mem_anon is not None), 0.99),
                mem_current_p99=_percentile(
                    sorted(s.mem_current for s in samples if s.mem_current is not None),
                    0.99),
                ticks=len(samples),
                tick_overruns=self._overruns,
                note=truncated,
            )
        return out


def _percentile(sorted_vals: list[int], q: float) -> int | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[idx]


def disabled_result(
    label: str, *, role: str = "", persistent: bool = False, source: str = "",
) -> WindowResult:
    """The no-op record for a target that resolved no cgroup.

    Every counter stays None. A disabled target publishing zeros would be
    indistinguishable from a container that ran and burned no CPU.
    """
    return WindowResult(
        label=label, enabled=False, role=role, persistent=persistent,
        source=source or NOT_MEASURED,
        note="disabled (non-Linux)" if not IS_LINUX else "cgroup path unresolved",
    )
