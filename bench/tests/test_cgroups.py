"""cgroup v2 sampling: window deltas, one thread, and the not-measured paths.

Every test here samples a FIXTURE CGROUP TREE under tmp_path through
`SampleTarget.for_path`, so the real sampling loop is exercised with no docker,
no root and no Linux dependency. That is why `for_path` exists: without it the
sampling logic is reachable only through `docker inspect`.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from ledgerbench import cgroups, registry
from ledgerbench.cgroups import (
    MultiSampler,
    Sample,
    SampleTarget,
    WindowResult,
    disabled_result,
)
from ledgerbench.live_runner import shared_summary_name


def _fake_cgroup(
    root: Path, name: str, *, usage_usec=0, nr_throttled=0, throttled_usec=0,
    anon=0, file=0, pgmajfault=0, pgfault=0, mem_current=0, mem_peak=0,
    oom_kill=0, cpuset="0-5", mem_max="12884901888", swap_max="0", swap_current=0,
    swap_peak=0,
) -> Path:
    """Write the files the sampler reads. Call again to advance the counters."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "cpu.stat").write_text(
        f"usage_usec {usage_usec}\nuser_usec 0\nsystem_usec 0\n"
        f"nr_periods 0\nnr_throttled {nr_throttled}\nthrottled_usec {throttled_usec}\n",
        encoding="utf-8",
    )
    (d / "memory.stat").write_text(
        f"anon {anon}\nfile {file}\nkernel 0\n"
        f"pgfault {pgfault}\npgmajfault {pgmajfault}\n",
        encoding="utf-8",
    )
    (d / "memory.current").write_text(f"{mem_current}\n", encoding="utf-8")
    (d / "memory.peak").write_text(f"{mem_peak}\n", encoding="utf-8")
    (d / "memory.events").write_text(
        f"low 0\nhigh 0\nmax 0\noom 0\noom_kill {oom_kill}\n", encoding="utf-8"
    )
    (d / "cpuset.cpus.effective").write_text(f"{cpuset}\n", encoding="utf-8")
    (d / "memory.max").write_text(f"{mem_max}\n", encoding="utf-8")
    (d / "memory.swap.max").write_text(f"{swap_max}\n", encoding="utf-8")
    if swap_current is not None:
        (d / "memory.swap.current").write_text(f"{swap_current}\n", encoding="utf-8")
    if swap_peak is not None:
        (d / "memory.swap.peak").write_text(f"{swap_peak}\n", encoding="utf-8")
    return d


def _window(sampler: MultiSampler, mutate=None) -> dict[str, WindowResult]:
    """start -> (optional counter advance) -> at least one tick -> stop."""
    sampler.start()
    time.sleep(cgroups.SAMPLE_INTERVAL_S * 1.5)
    if mutate is not None:
        mutate()
    time.sleep(cgroups.SAMPLE_INTERVAL_S * 1.5)
    return sampler.stop()


def _target(path, label="pg", *, persistent=True, role=cgroups.ROLE_DATABASE):
    return SampleTarget.for_path(label, path, role=role, persistent=persistent)


def _unlink(path: Path) -> None:
    """Delete a file the live sampler may have open.

    Windows refuses to delete an open file, and the sampler's first tick lands
    on exactly the instant the test reaches for it. Linux never takes the retry.
    """
    for _ in range(200):
        try:
            path.unlink()
            return
        except PermissionError:
            time.sleep(0.001)
    path.unlink()


# --------------------------------------------------------------------------- #
# the delta model
# --------------------------------------------------------------------------- #
def test_persistent_cgroup_counters_are_deltas_not_lifetime_totals(tmp_path):
    """Two consecutive windows over ONE cgroup whose counters only ever rise.

    postgres/redis/vegeta/the daemons live for the whole run, so cpu.stat's
    nr_throttled and throttled_usec carry every earlier window's events. Round 1
    read them at window END and `_finalize_manifest` SUMMED that, which for a
    persistent cgroup multiplies one lifetime total by the window count. Each
    window must report only its own increment.
    """
    cg = _fake_cgroup(tmp_path, "pg", nr_throttled=100, throttled_usec=5_000,
                      usage_usec=1_000_000, oom_kill=3)

    w1 = _window(MultiSampler([_target(cg)]), mutate=lambda: _fake_cgroup(
        tmp_path, "pg", nr_throttled=107, throttled_usec=5_900,
        usage_usec=1_400_000, oom_kill=3))["pg"]
    assert w1.nr_throttled_delta == 7
    assert w1.throttled_usec_delta == 900
    assert w1.cpu_usage_delta_usec == 400_000
    assert w1.oom_kill_delta == 0

    w2 = _window(MultiSampler([_target(cg)]), mutate=lambda: _fake_cgroup(
        tmp_path, "pg", nr_throttled=110, throttled_usec=6_100,
        usage_usec=1_650_000, oom_kill=3))["pg"]
    assert w2.nr_throttled_delta == 3
    assert w2.throttled_usec_delta == 200
    assert w2.cpu_usage_delta_usec == 250_000

    # The sum over both windows is the true total across them, which is the
    # property _finalize_manifest depends on. With the lifetime read it would
    # have been 107 + 110 = 217 against a true 10.
    assert w1.nr_throttled_delta + w2.nr_throttled_delta == 110 - 100
    # The lifetime totals are still in the artifact, just not mistaken for the
    # window figure.
    assert w2.end.nr_throttled == 110
    assert w2.start.nr_throttled == 107


def test_window_deltas_are_null_when_a_boundary_read_fails(tmp_path):
    """A counter that could not be read is null, NEVER zero.

    A zero throttle count is a claim ("this window was never throttled"); an
    unreadable counter is the absence of one. `or 0` anywhere on the delta path
    turns the second into the first.
    """
    cg = _fake_cgroup(tmp_path, "pg", nr_throttled=4, usage_usec=10)
    sampler = MultiSampler([_target(cg)])
    sampler.start()
    _unlink(cg / "cpu.stat")
    _unlink(cg / "memory.events")
    w = sampler.stop()["pg"]
    assert w.nr_throttled_delta is None
    assert w.throttled_usec_delta is None
    assert w.cpu_usage_delta_usec is None
    assert w.oom_kill_delta is None
    # ...and the summary that reaches disk carries the nulls, not defaults.
    doc = w.summary_dict()
    assert doc["nr_throttled_delta"] is None and doc["oom_kill_delta"] is None


def test_fault_deltas_come_from_the_sampler_edges(tmp_path):
    """memory.stat pgmajfault/pgfault deltas, for every target not just the SUT.

    These used to be read in `live_runner` through a SECOND cgroup-path
    resolution (`docker inspect`) at each window boundary. Same delta, one
    source, and postgres/redis get one too.
    """
    cg = _fake_cgroup(tmp_path, "pg", pgmajfault=5, pgfault=9)
    w = _window(MultiSampler([_target(cg)]), mutate=lambda: _fake_cgroup(
        tmp_path, "pg", pgmajfault=12, pgfault=20))["pg"]
    assert w.major_faults_delta == 7
    assert w.minor_faults_delta == 11


def test_mem_peak_is_recorded_at_both_edges_and_never_differenced(tmp_path):
    """memory.peak is a high-water mark; a difference of two is not a window.

    The sampler records both edges under names that say which is which, and
    publishes no `mem_peak_delta` at all -- there is no such quantity, and a
    field with that name would be subtracted by someone.
    """
    cg = _fake_cgroup(tmp_path, "pg", mem_peak=900, mem_current=500)
    w = _window(MultiSampler([_target(cg)]), mutate=lambda: _fake_cgroup(
        tmp_path, "pg", mem_peak=1000, mem_current=600))["pg"]
    doc = w.summary_dict()
    assert doc["mem_peak_start"] == 900
    assert doc["mem_peak_end"] == 1000
    assert not any(k.startswith("mem_peak") and k.endswith("_delta") for k in doc)
    # memory.current IS window-scoped and is the level a persistent cgroup has.
    assert doc["mem_current_p99"] in (500, 600)
    assert doc["persistent"] is True


# --------------------------------------------------------------------------- #
# one thread, one read
# --------------------------------------------------------------------------- #
def test_one_thread_samples_every_target(tmp_path):
    """Six cgroups, ONE thread.

    Round 1 spawned a thread per container and every one self-pinned to the
    same housekeeping core; W16's target set would have put six of them there.
    """
    labels = ["sut", "pg", "cache", "load", "dockerd", "containerd"]
    targets = [_target(_fake_cgroup(tmp_path, n, usage_usec=1), n) for n in labels]
    sampler = MultiSampler(targets)

    before = threading.active_count()
    sampler.start()
    time.sleep(cgroups.SAMPLE_INTERVAL_S * 3)
    during = threading.active_count()
    windows = sampler.stop()

    assert during - before == 1
    assert set(windows) == set(labels)
    assert all(windows[n].samples for n in labels)


def test_cpu_stat_is_read_once_per_tick(tmp_path, monkeypatch):
    """One parse per target per tick, not one per field.

    Round 1 opened and re-parsed cpu.stat three times per sample -- once each
    for usage_usec, nr_throttled and throttled_usec.
    """
    cg = _fake_cgroup(tmp_path, "pg", usage_usec=1)
    reads: list[str] = []
    real = cgroups.read_flat_keyed

    def counting(path, keys):
        reads.append(path.name)
        return real(path, keys)

    monkeypatch.setattr(cgroups, "read_flat_keyed", counting)
    sampler = MultiSampler([_target(cg)])
    w = _window(sampler)["pg"]

    # Per tick: exactly one cpu.stat and one memory.stat.
    tick_cpu = reads.count("cpu.stat")
    # The two window edges each read cpu.stat + memory.stat + memory.events.
    assert tick_cpu == w.ticks + 2
    assert reads.count("memory.stat") == w.ticks + 2


def test_a_slipped_tick_is_counted_not_hidden(tmp_path):
    """A starved sampler must be visible in the artifact.

    Round 1 resynced silently when it fell behind, so a thinned memory series
    was indistinguishable from a short window.
    """
    cg = _fake_cgroup(tmp_path, "pg")
    w = _window(MultiSampler([_target(cg)]))["pg"]
    assert w.summary_dict()["tick_overruns"] == w.tick_overruns
    assert w.ticks == len(w.samples)


# --------------------------------------------------------------------------- #
# not-measured paths
# --------------------------------------------------------------------------- #
def test_systemd_cgroup_path_returns_not_measured_when_unavailable(monkeypatch):
    """An unknown / undelegated unit resolves to None, never to a guessed path.

    A unit with no delegated cgroup reports the ROOT cgroup `/`, and turning
    that into a path would sample the whole host and publish it as the daemon's
    cost.
    """
    from ledgerbench.util.proc import ProcResult

    def fake(args, **kw):
        return ProcResult(args=args, returncode=0, stdout="/\n", stderr="")

    monkeypatch.setattr(cgroups, "IS_LINUX", True)
    monkeypatch.setattr(cgroups, "run", fake)
    assert cgroups.resolve_systemd_cgroup_path(cgroups.DOCKER_UNIT) is None

    def missing(args, **kw):
        return ProcResult(args=args, returncode=1, stdout="", stderr="no unit")

    monkeypatch.setattr(cgroups, "run", missing)
    assert cgroups.resolve_systemd_cgroup_path("nope.service") is None

    t = SampleTarget.for_unit("dockerd", "nope.service",
                              role=cgroups.ROLE_CONTAINER_RUNTIME)
    assert t.enabled is False
    assert t.source.startswith(cgroups.NOT_MEASURED)
    assert t.persistent is True


def test_an_unresolvable_target_does_not_disable_the_others(tmp_path):
    """One missing cgroup must not cost the measurement.

    dockerd is the most likely target to resolve to nothing (no systemctl, a
    container-in-container host). If that disabled the sampler, the headline
    CPU number would silently vanish with it.
    """
    ok = _target(_fake_cgroup(tmp_path, "pg", usage_usec=7), "pg")
    missing = SampleTarget.for_path(
        "dockerd", None, role=cgroups.ROLE_CONTAINER_RUNTIME, persistent=True,
    )
    windows = _window(MultiSampler([ok, missing]))
    assert windows["pg"].enabled is True and windows["pg"].samples
    assert windows["dockerd"].enabled is False
    assert windows["dockerd"].cpu_usage_delta_usec is None
    assert windows["dockerd"].summary_dict()["cpu_usage_delta_usec"] is None


def test_disabled_result_records_the_reason_and_no_zeros():
    r = disabled_result("dockerd", role=cgroups.ROLE_CONTAINER_RUNTIME,
                        persistent=True, source="not_measured: no systemctl")
    assert r.enabled is False and r.label == "dockerd"
    assert r.note
    doc = r.summary_dict()
    assert doc["cpu_usage_delta_usec"] is None
    assert doc["nr_throttled_delta"] is None
    assert doc["mem_peak_end"] is None


# --------------------------------------------------------------------------- #
# identity: labels are artifact names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["", "pg.1", "a/b"])
def test_a_label_that_cannot_be_a_filename_is_refused(bad):
    """Same restriction registry.Slot puts on a slot id, and for the same reason.

    The label is a filename segment and the manifest's row key; a dot makes
    `cpu_mem_<slot>.<label>.summary.json` ambiguous and a slash escapes the
    artifact directory.
    """
    with pytest.raises(ValueError):
        SampleTarget.for_path(bad, None, role=cgroups.ROLE_SUT, persistent=False)


def test_duplicate_labels_are_refused(tmp_path):
    cg = _fake_cgroup(tmp_path, "pg")
    with pytest.raises(ValueError, match="duplicate sample label"):
        MultiSampler([_target(cg, "pg"), _target(cg, "pg")])


def test_shared_summary_names_cannot_collide_with_a_slot_summary():
    """A co-sampled cgroup's artifact can never shadow a SUT's.

    The guarantee rests on registry.Slot refusing a '.' in a slot id: without
    it a slot could be named `<slot>.<label>` and `analyze` would read the
    wrong file. This test is what pins that dependency.
    """
    shared = ["postgres", "redis", "vegeta", "dockerd", "containerd"]
    sut_names = {f"cpu_mem_{s}.summary.json" for s in registry.SLOTS}
    for slot_id in registry.SLOTS:
        assert "." not in slot_id
        for label in shared:
            assert shared_summary_name(slot_id, label) not in sut_names


# --------------------------------------------------------------------------- #
# serialization
# --------------------------------------------------------------------------- #
def test_window_result_csv_roundtrip(tmp_path):
    wr = WindowResult(
        label="c",
        enabled=True,
        samples=[
            Sample(monotonic_s=0.0, cpu_usage_usec=100, nr_throttled=0,
                   throttled_usec=0, mem_anon=1024, mem_current=2048),
            Sample(monotonic_s=0.05, cpu_usage_usec=200, nr_throttled=0,
                   throttled_usec=0, mem_anon=2048, mem_current=None),
        ],
    )
    p = tmp_path / "cpu.csv"
    wr.write_csv(p)
    lines = p.read_text().strip().splitlines()
    assert lines[0] == (
        "monotonic_s,cpu_usage_usec,nr_throttled,throttled_usec,mem_anon,mem_current"
    )
    assert lines[1].endswith(",100,0,0,1024,2048")
    assert lines[2].endswith(",200,0,0,2048,")   # an unread field is empty, not 0
    assert len(lines) == 3


def test_summary_dict_keeps_both_raw_edges(tmp_path):
    """Every published delta must be recomputable from the artifact."""
    cg = _fake_cgroup(tmp_path, "pg", usage_usec=10, nr_throttled=1)
    w = _window(MultiSampler([_target(cg)]), mutate=lambda: _fake_cgroup(
        tmp_path, "pg", usage_usec=40, nr_throttled=4))["pg"]
    doc = w.summary_dict()
    edges = doc["edges"]
    assert edges["end"]["cpu_usage_usec"] - edges["start"]["cpu_usage_usec"] == (
        doc["cpu_usage_delta_usec"]
    )
    assert doc["cpuset_effective"] == "0-5"
    assert doc["role"] == cgroups.ROLE_DATABASE
    assert doc["window_seconds"] > 0


def test_percentile_helper():
    # Nearest-rank p99 over 1..100: index round(0.99*99)=98 -> value 99.
    vals = list(range(1, 101))
    assert cgroups._percentile(vals, 0.99) == 99
    assert cgroups._percentile(vals, 1.0) == 100
    assert cgroups._percentile([], 0.99) is None
    assert cgroups._percentile([5], 0.5) == 5


def test_resolve_cgroup_path_none_off_linux(monkeypatch):
    monkeypatch.setattr(cgroups, "IS_LINUX", False)
    assert cgroups.resolve_cgroup_path("whatever", pid=1) is None
    assert cgroups.resolve_systemd_cgroup_path(cgroups.DOCKER_UNIT) is None


def test_overhead_roles_are_the_apparatus_not_the_system():
    """The rule that keeps vegeta and dockerd out of every per-request cost."""
    assert cgroups.ROLE_LOAD_GENERATOR in cgroups.OVERHEAD_ROLES
    assert cgroups.ROLE_CONTAINER_RUNTIME in cgroups.OVERHEAD_ROLES
    assert cgroups.ROLE_SUT not in cgroups.OVERHEAD_ROLES
    assert cgroups.ROLE_DATABASE not in cgroups.OVERHEAD_ROLES


# --------------------------------------------------------------------------- #
# W18/B6 row 10: container ground truth rides the window the containers exist in
# --------------------------------------------------------------------------- #
def test_the_containers_ceilings_are_read_and_reach_the_summary(tmp_path):
    """cpuset + memory.max + memory.swap.max, read at window start.

    No phase boundary can read these: the SUT is force-recreated per probe and
    PostgreSQL is down until the seed phase, so the only moment the containers
    exist is inside a window. `manifest.topology` said `not_measured` while
    `cpuset.cpus.effective` was already in every per-window artifact.
    """
    cg = _fake_cgroup(tmp_path, "sut", cpuset="0-5", mem_max="12884901888",
                      swap_max="0")
    w = _window(MultiSampler([_target(cg, "sut", persistent=False,
                                      role=cgroups.ROLE_SUT)]))["sut"]
    assert w.cpuset_effective == "0-5"
    assert w.mem_limit_bytes == 12884901888
    assert w.mem_swap_max == "0"
    doc = w.summary_dict()
    assert doc["mem_limit_bytes"] == 12884901888
    assert doc["mem_swap_max"] == "0"
    assert doc["cpuset_effective"] == "0-5"


def test_a_wrong_cpuset_is_caught_at_the_first_window_not_at_finalization(tmp_path):
    """B6 row 10, where it is cheap: `start()` has already read the cpuset.

    A SUT slot missing from infra/compose.bench.yaml runs UNPINNED across every
    logical core while the run records a 6-core budget, which invalidates every
    CPU-ms- and memory-per-request figure in the cell. Left to `_stamp_topology`
    it was computed at finalization -- after all 15 h of a cell -- and then
    dropped: nothing downstream read `matches`.

    Compared as SETS (`0-5` and `0,1,2,3,4,5` are one placement), and a role
    with no compose cpuset is skipped rather than failed: dockerd and containerd
    are pinned by the systemd drop-in.
    """
    sut = _fake_cgroup(tmp_path, "sut", cpuset="0-15")
    pg = _fake_cgroup(tmp_path, "pg", cpuset="8,9,10")
    dockerd = _fake_cgroup(tmp_path, "dockerd", cpuset="0-15")
    sampler = MultiSampler([
        _target(sut, "sut", persistent=False, role=cgroups.ROLE_SUT),
        _target(pg, "pg"),
        _target(dockerd, "dockerd", role=cgroups.ROLE_CONTAINER_RUNTIME),
    ])
    sampler.start()
    try:
        bad = sampler.cpuset_mismatches({
            cgroups.ROLE_SUT: "0-5", cgroups.ROLE_DATABASE: "8-10",
        })
    finally:
        sampler.stop()
    assert len(bad) == 1
    assert bad[0].startswith("sut ")
    assert "0-15" in bad[0] and "0-5" in bad[0]


def test_swap_is_read_as_an_outcome_at_both_edges_not_only_as_a_ceiling(tmp_path):
    """`memory.swap.max` is the PERMISSION; `memory.swap.current` is the EVENT.

    A container that swapped 4 GiB and one that never swapped record the same
    `memory.swap.max`, and a reader cannot tell them apart from the ceiling. It
    matters because a swapped-out anon page LEAVES cgroup v2 `memory.stat anon`:
    `mem_anon_p99` drops and the run publishes an understated
    steady-state-memory-per-request headline with nothing in any artifact saying
    it happened.

    A LEVEL, not a delta: swap usage falls when pages fault back in, so
    end-minus-start goes negative and means nothing. The max over the edges is
    the claim the evidence supports.
    """
    cg = _fake_cgroup(tmp_path, "sut", swap_max="0", swap_current=0)
    sampler = MultiSampler([_target(cg, "sut", persistent=False,
                                    role=cgroups.ROLE_SUT)])

    def swap_out():
        _fake_cgroup(tmp_path, "sut", swap_max="0", swap_current=4294967296)

    w = _window(sampler, swap_out)["sut"]
    assert w.start.swap_current == 0
    assert w.end.swap_current == 4294967296
    assert w.swap_current_max == 4294967296
    doc = w.summary_dict()
    assert doc["swap_current_start"] == 0
    assert doc["swap_current_end"] == 4294967296
    assert doc["swap_current_max"] == 4294967296
    assert doc["edges"]["end"]["swap_current"] == 4294967296


def test_an_unreadable_swap_counter_is_null_and_never_a_measured_zero(tmp_path):
    """A cgroup with no `memory.swap.current` never swapped ZERO; it is unread.

    `swap_current_clean` is taken over the windows that HAVE the counter, so
    publishing 0 here would turn "we could not check" into "we checked and it was clean" --
    the same empty-set discipline `throttle_clean` and `oom_clean` already
    follow.
    """
    cg = _fake_cgroup(tmp_path, "pg", swap_current=None, swap_peak=None)
    w = _window(MultiSampler([_target(cg)]))["pg"]
    assert w.start.swap_current is None and w.end.swap_current is None
    assert w.swap_current_max is None
    assert w.summary_dict()["swap_current_max"] is None
    # a kernel with no swap accounting at all (no CONFIG_MEMCG_SWAP, or booted
    # `swapaccount=0`) has NEITHER file, and neither may read as a zero
    assert w.swap_peak_max is None
    assert w.summary_dict()["swap_peak_max"] is None


def test_swap_peak_catches_the_window_that_swapped_and_faulted_back(tmp_path):
    """Two point samples cannot see an 80-second swap episode between them.

    `memory.swap.current` is read at the window edges only -- swap is not in the
    50 ms tick set -- so a container that swapped 4 GiB at t=30 s and had it all
    faulted back by t=110 s reads 0 at BOTH edges and publishes
    `swap_current_clean` while `mem_anon_p99` (which IS sampled at 50 ms) was
    depressed for 80 s.
    That is precisely the failure the swap read exists to catch.
    `memory.swap.peak` is the kernel's own monotonic high-water mark over the
    cgroup's lifetime and it is read at both edges for the same reason
    `memory.peak` already is.
    """
    cg = _fake_cgroup(tmp_path, "sut", swap_current=0, swap_peak=0)
    sampler = MultiSampler([_target(cg, "sut", persistent=False,
                                    role=cgroups.ROLE_SUT)])

    def swapped_and_faulted_back():
        _fake_cgroup(tmp_path, "sut", swap_current=0, swap_peak=4294967296)

    w = _window(sampler, swapped_and_faulted_back)["sut"]
    assert w.swap_current_max == 0            # both edges saw nothing
    assert w.swap_peak_max == 4294967296      # the kernel remembers anyway
    doc = w.summary_dict()
    assert doc["swap_peak_start"] == 0
    assert doc["swap_peak_end"] == 4294967296
    assert doc["swap_peak_max"] == 4294967296
    assert doc["edges"]["end"]["swap_peak"] == 4294967296


def test_a_wrong_memory_ceiling_is_caught_at_the_first_window_too(tmp_path):
    """The other half of B6 row 10, checked where the cpuset is checked.

    `memory.max` and `memory.swap.max` were read at every window start, written
    into the manifest and compared against nothing: the report asserted
    compose's `memswap_limit: mem_limit` rule as prose while the read-back that
    could prove it sat one field away. An unlimited SUT is not cosmetic -- both
    runtimes size their heap from the cgroup limit -- so it aborts on the first
    window, like a wrong cpuset does.

    `max` is a mismatch, not an unreadable value; a role with no declared
    ceiling (the container-runtime units) is skipped, and so is a cgroup whose
    file does not read at all.
    """
    sut = _fake_cgroup(tmp_path, "sut", mem_max="max", swap_max="max")
    pg = _fake_cgroup(tmp_path, "pg", mem_max="8589934592", swap_max="0")
    redis = _fake_cgroup(tmp_path, "redis", mem_max="2147483648", swap_max="2147483648")
    dockerd = _fake_cgroup(tmp_path, "dockerd", mem_max="max", swap_max="max")
    sampler = MultiSampler([
        _target(sut, "sut", persistent=False, role=cgroups.ROLE_SUT),
        _target(pg, "pg"),
        _target(redis, "redis", role=cgroups.ROLE_CACHE),
        _target(dockerd, "dockerd", role=cgroups.ROLE_CONTAINER_RUNTIME),
    ])
    sampler.start()
    try:
        bad = sampler.limit_mismatches({
            cgroups.ROLE_SUT: 12884901888,
            cgroups.ROLE_DATABASE: 8589934592,
            cgroups.ROLE_CACHE: 2147483648,
        })
    finally:
        sampler.stop()
    joined = "\n".join(bad)
    # the unlimited SUT: BOTH the ceiling and the swap allowance it implies
    assert sum(b.startswith("sut ") for b in bad) == 2
    assert "memory.max=max" in joined and "12884901888" in joined
    # postgres got exactly what the overlay asked for; dockerd declares nothing
    assert not [b for b in bad if b.startswith("pg ") or b.startswith("dockerd ")]
    # redis has the right ceiling and a swap allowance it must not have
    redis_bad = [b for b in bad if b.startswith("redis ")]
    assert len(redis_bad) == 1 and "memory.swap.max=2147483648" in redis_bad[0]


def test_an_unlimited_cgroup_reports_max_verbatim_and_not_a_number(tmp_path):
    """`memory.max: max` is the ABSENCE of a limit, not a byte count.

    An int field alone cannot carry it, so the raw swap string is kept beside
    the parsed limit -- "max", "0" and a byte count are three different claims.
    """
    cg = _fake_cgroup(tmp_path, "pg", mem_max="max", swap_max="max")
    w = _window(MultiSampler([_target(cg)]))["pg"]
    assert w.mem_limit_bytes is None
    assert w.mem_swap_max == "max"


def test_swap_allowance_misconfigured_names_only_repairable_containers(tmp_path):
    """The docker-29 create-path flake: a container's memory.swap.max reads
    `max` despite HostConfig MemorySwap == Memory. Only CONTAINER targets are
    repairable via `docker update`; a systemd unit (no container_name) and a
    correctly-limited container must both stay off the repair list."""
    flaked = _fake_cgroup(tmp_path, "pg", swap_max="max")
    clean = _fake_cgroup(tmp_path, "redis", swap_max="0")
    unit = _fake_cgroup(tmp_path, "dockerd", swap_max="max")
    sampler = MultiSampler([
        SampleTarget(label="pg", path=flaked, role=cgroups.ROLE_DATABASE,
                     persistent=True, container_name="ledgerline-postgres-1"),
        SampleTarget(label="redis", path=clean, role=cgroups.ROLE_CACHE,
                     persistent=True, container_name="ledgerline-redis-1"),
        SampleTarget(label="dockerd", path=unit,
                     role=cgroups.ROLE_CONTAINER_RUNTIME, persistent=True),
    ])
    sampler.start()
    try:
        assert [t.label for t in sampler.swap_allowance_misconfigured()] == ["pg"]
        # the repair writes the file; reread_placement is what un-flags it
        (flaked / "memory.swap.max").write_text("0\n", encoding="utf-8")
        assert [t.label for t in sampler.swap_allowance_misconfigured()] == ["pg"]
        sampler.reread_placement()
        assert sampler.swap_allowance_misconfigured() == []
    finally:
        sampler.stop()
