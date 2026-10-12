"""Full-disclosure RunManifest schema + writer.

One `results/<run_id>/manifest.json` per run: the complete record needed to make
the headline numbers auditable (all knob values echoed). The runner phases
populate the sections as they execute. Optional fields default to None/empty so
a partially-completed run still serializes.

A field that was NEVER MEASURED must say so. Empty lists and bare nulls read as
"zero" to a reader, so the aggregate sections below carry an explicit `source` /
`note` and count the windows they were built from. The guarantee is per SECTION,
not only per row: a list-valued aggregate that stayed empty is named in
`aggregate_provenance` with the reason, and host_state / topology /
postgres_config carry their own `source` marker so a section whose read never
ran (a non-Linux run, or one that stopped early) cannot read as "measured and
empty".

schema_version 2 (2026-08): knobs are recorded PER CELL and never cross-copied
between languages; capacity_gate, effective_workload and working_set sections
added; the aggregate sections carry provenance.

schema_version 3 (P1/W7): every per-language pair became a per-SLOT map
(`knobs_by_slot`, `CellKnobs.by_slot`, `Versions.runtime_by_slot`), because P2
runs .NET in BOTH slots and a `go`/`dotnet` pair cannot express that at all.
Go-only knobs are gone and the JVM's replace them. The version is now READ on
load: `RunManifest` is `extra="forbid"` and `load()` did not catch
`ValidationError`, while its only caller writes `load(run_id) or new_manifest(...)`
-- and an `or` cannot rescue a raise, so a schema change after the first run
would have crashed every resume with a field-level error instead of naming the
real problem.

schema_version 4 (P1/W16): the cgroup rows are keyed by sampling LABEL, not by
container name. PostgreSQL, Redis, vegeta and the two container-runtime units
are sampled now, and a systemd unit is not a container, so `container` was a
false noun on the field every aggregation keys by. The throttle and OOM
counters became measured-window DELTAS: a v3 row's `nr_throttled` is a sum of
LIFETIME totals and a v4 row's is a sum of window increments, which are
different quantities under one field name -- that alone is why the version
moves. `memory.peak` stays a lifetime high-water mark; v4 said so per row in a
`basis` field, which v6 removes (below). `load_generator_overhead` is a separate section because vegeta and dockerd
CPU is measured, is worth publishing, and is NOT part of any per-request cost.

schema_version 5 (P1/W18): the three sections that said `not_measured` on every
round-1 run are populated. `host_state` carries per-attribute `HostReading`
rows read back off sysfs/procfs after the tuning is applied; `topology` gains
the cgroups' own `cpuset.cpus.effective` / `memory.max` / `memory.swap.max` plus
the L3 groups; `postgres_config.values` carries pg_settings. The version moves
because `Topology` and `ContainerTopology` are `extra="forbid"` (a v4 manifest
cannot hold the new fields) and because `host_state.source` inverts from
`not_measured` to `measured` under UNCHANGED field names -- a reader diffing two
runs would otherwise see the same schema saying two different things.
`HostState.turbostat_snapshot` is gone: nothing ever produced it, and
`scaling_cur_freq` + `core_throttle_count` evidence boost without root.
`governor`/`irq_mask`/`tier` are gone with it -- one string cannot carry the
governor SET across 32 CPUs, `irq_mask` mirrored the one knob that governs only
NEW interrupts, and the tier is per attribute, not per section.

schema_version 6 (P1): `Versions` loses `pgjdbc`, `npgsql`,
`redis`, `postgres` and `vegeta`. All five were written by nothing and published
null on every manifest ever produced; their stated job -- naming WHICH versions
went unrecorded -- is not done by five permanent nulls. The version moves
because the published shape shrinks: a reader diffing a v5 against a v6 manifest
must see that the fields were removed, not that this run failed to fill them.
The same paragraph applies to `basis` on `ThrottleCounters`, `FaultCounts`,
`ResourceDelta` and `LoadGeneratorOverhead`: no writer ever set it, so every row
ever published carried the default `"window_delta"` and the alternative value
the constant existed to express (`lifetime_at_window_end`) was unreachable. On
`ResourceDelta` the default was also WRONG for the row it sat on -- that row's
`mem_peak_bytes` is a lifetime high-water mark and its `mem_*_p99_bytes` are
maxes of in-window percentiles, none of them a window delta. Per-row basis
disclosure is a real need; a permanently-constant field is not a way to meet it,
and the field names plus this docstring carry it until a writer exists.

schema_version 7 (P1/W20): `WorkingSet` no longer describes a file. Round 1
generated one targets file per slot and replayed it, so the section counted the
distinct ids that ONE file named and disclosed the seconds it took to cycle
(`replay_period_seconds`). W20 draws a fresh stream per probe attack, so there
is no file to count, no cycle to time, and `replay_period_seconds` -- along with
`targets_file`, `target_count` and the three whole-file distinct-id counts -- is
DELETED rather than left publishing a period nothing replays. What replaces it
is per-probe: the spread of requests drawn, of distinct ids named and of
generation seconds paid, accumulated from the `<slot>.target_draw.json` each
probe writes. The version moves because the section's meaning inverts under a
name that does not.

schema_version 8 (P1/W20 correction): the same section stops asserting that nothing
pre-warms a measured window. Separate per-attack streams remove the
SEQUENCE-correlated pre-warm and nothing more; two independent Zipf draws over
one keyspace share the head by construction, and measured on this box 52-67% of
a window's invoice-id read requests -- over rung and warmup length, at the
warmup rate the harness offers -- name an id an earlier attack of the same probe
already drew. `prewarm_containment_per_probe` publishes that fraction from
the run's own draws. `draw_seconds_per_probe` moves in the same bump because it
changed MEANING -- generation only, now generation plus the `docker cp` that
ships each file, which is the half that scales with the rung.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .util import paths
from .util.clock import rfc3339_micros
from .util.proc import run

# 7 -> 8 (W20 correction): `working_set.draw_seconds_per_probe` changed MEANING -- it was
# generation only and now covers generation PLUS the `docker cp` that ships each
# file, which is the half that scales with the rung. Two v7 manifests and a v8
# one would have published the same field over different quantities with nothing
# to tell them apart. `prewarm_containment_per_probe` arrives in the same bump.
MANIFEST_SCHEMA_VERSION = 8

NOT_MEASURED = "not_measured"


class ManifestSchemaError(RuntimeError):
    """A results/<id>/manifest.json this build cannot read."""


class Identity(BaseModel):
    """WHICH code produced the numbers.

    `git_sha` / `git_dirty` are the values read when the run STARTED and are
    never overwritten afterwards: a resume runs from whatever is checked out
    then, and silently restamping the sha would erase the fact that the run
    began on different code. A resume from a different commit appends to
    `git_sha_changed_during_run` instead (empty on a single-commit run).
    """

    model_config = ConfigDict(extra="forbid")
    run_id: str
    git_sha: str | None = None
    git_dirty: bool | None = None
    # {"at": rfc3339, "git_sha": ..., "git_dirty": ...} per observed change.
    git_sha_changed_during_run: list[dict] = Field(default_factory=list)
    # sha256 of spec/schema.sql, populated at run start. Redundant with
    # config_hash (which covers the whole spec/ tree) and kept anyway, because a
    # reader checking "is this the schema I think it is" should not have to
    # reproduce a tree hash to do it.
    schema_sql_sha256: str | None = None
    config_hash: str | None = None            # sha256 over the whole spec/ tree


class Versions(BaseModel):
    """Runtime versions in force during the run. Every field here is READ BACK.

    `runtime_by_slot` and `service_version_by_slot` come off each SUT's
    `/runtime-stats` build block; `image_digests` comes off `docker inspect`.
    `source` flips to `measured` as soon as any of them lands, so an all-empty
    Versions still reads as the ABSENCE of a reading rather than "measured and
    unknown".

    Library and infrastructure versions are not fields here (removed at
    schema_version 6): the library pins live in the build files and the
    infrastructure images are digest-pinned in `infra/compose.yaml`, both of
    which `identity.git_sha` resolves exactly, and the SUT images' running
    digests are in `image_digests`. Reinstating a field here means writing the
    reader that fills it, in the same change.
    """

    model_config = ConfigDict(extra="allow")
    source: str = NOT_MEASURED
    note: str = (
        "runtime_by_slot / service_version_by_slot / image_digests are read off "
        "the running containers; library and infrastructure-image pins are not "
        "recorded here -- they are resolved by identity.git_sha against the "
        "build files and infra/compose.yaml"
    )
    # compose service -> image RepoDigest (compose.container_image_digest).
    image_digests: dict[str, str] = Field(default_factory=dict)
    # slot id -> /runtime-stats build block values.
    runtime_by_slot: dict[str, str] = Field(default_factory=dict)
    service_version_by_slot: dict[str, str] = Field(default_factory=dict)


class Knobs(BaseModel):
    """Every variant lever. null where not applicable to a slot's runtime.

    The attribute names are the RIGHT-HAND SIDE of `registry.SHARED_KNOB_ENV`
    and each `LanguageSpec.knob_env`; `test_knobs_echo_every_declared_lever`
    asserts the two cannot drift. A declared lever with no field here is a knob
    the manifest cannot evidence, which is the shape of round 1's
    `db_conn_lifetime_seconds`: spec-declared, compose-set, always null.
    """

    model_config = ConfigDict(extra="forbid")
    db_pool_max: int | None = None
    db_pool_min: int | None = None
    db_conn_lifetime_seconds: int | None = None
    # Recorded as the RAW string an operator set, not coerced to bool: the two
    # SUTs parse it with different rules (Spring relaxed binding vs EnvBool), and
    # a normalized `false` here would hide which spelling actually reached them.
    db_pool_warm_on_start: str | None = None
    redis_pool_max: int | None = None
    cache_ttl_seconds: int | None = None
    log_queue_len: int | None = None
    # --- JVM ---------------------------------------------------------------- #
    max_ram_percent: int | None = None        # -XX:MaxRAMPercentage (JDK 25 default 25)
    gc_engine: str | None = None              # the -XX:+UseG1GC / +UseZGC flag string
    prepare_threshold: int | None = None      # PgJDBC prepareThreshold (default 5)
    # --- CLR ---------------------------------------------------------------- #
    gc_dynamic_adaptation_mode: str | None = None
    max_auto_prepare: int | None = None       # Npgsql Max Auto Prepare (D4: 20)
    auto_prepare_min_usages: int | None = None  # Npgsql counterpart of prepareThreshold
    # --- both --------------------------------------------------------------- #
    data_layer: str | None = None             # jpa | efcore | ado | dapper (live-read, asserted)
    # `Runtime.availableProcessors()` / `Environment.ProcessorCount` as the SUT
    # itself reports it, asserted == SUT_CORES on Linux. There is deliberately no
    # second field for the -XX:ActiveProcessorCount FLAG: measured on JDK 25, the
    # flag reads back as -1 {DEFAULT} while availableProcessors() correctly
    # returns the cpuset size, so two fields would routinely disagree and a
    # reader could not tell which one was the assertion.
    processor_count: int | None = None


class CellKnobs(BaseModel):
    """The knob values a SINGLE cell ran with, per slot.

    Knobs are per cell, not per run: the headline cells differ in GC posture, so
    one run-level pair of Knobs rows can only ever describe the last cell
    executed (round 1's manifest did exactly that).

    Keyed by SLOT, not language, for the same reason the ledger is: P2 runs .NET
    in both slots, and a `go`/`dotnet` pair cannot represent two rows of one
    language. Runtime-specific knobs stay on their own side -- a JVM knob written
    onto the .NET row fabricates a value the .NET SUT never saw.
    """

    model_config = ConfigDict(extra="forbid")
    cell_id: str
    extra_env: dict[str, str] = Field(default_factory=dict)   # verbatim cell env
    by_slot: dict[str, Knobs] = Field(default_factory=dict)


class HostReading(BaseModel):
    """ONE host attribute, as it read back after the tuning was applied.

    `kind` is the load-bearing field. A SETTING is a value we asked the kernel
    for and can compare to a target; an OUTCOME is a counter or level that shows
    whether the setting bit, is load- and time-dependent, and can never be
    compared to anything. `hostread.evaluate` refuses a Tier-1 OUTCOME, so the
    rule is enforced by the suite rather than by review.

    `matches is None` means NOT EVALUABLE (the knob is absent on this box), which
    is a Tier-2 note in every tier -- never a pass and never a failure.
    """

    model_config = ConfigDict(extra="forbid")
    attribute: str
    kind: str
    tier: str
    paths: list[str] = Field(default_factory=list)
    value: str | int | bool | list | dict | None = None
    expected: str | int | bool | list | dict | None = None
    matches: bool | None = None
    detail: str = ""


class HostState(BaseModel):
    """Host tuning actually in force during the run, READ BACK from the box.

    Populated by the `host_readback` phase (B6), which runs after `host_setup`
    on every run start and every resume. Round 1 recorded `not_measured` here on
    every run and therefore could not evidence the governor, boost, C-states,
    THP or placement it claimed to have run under.

    `readings` is the LATEST full set; `first_readings` is the run's first, and
    a value that changed between them is appended to `changed_during_run` rather
    than overwritten -- the provenance rule `identity.git_sha` already follows.
    Appending every resume's full set instead would grow without bound across a
    multi-day run.

    A manifest whose read-back never ran keeps `source: not_measured`: an empty
    HostState is the ABSENCE of a reading, never "measured and default".

    `tier1_not_evaluable` is separate from `tier2_notes` on purpose. Without it
    `tier1_failures: []` meant both "every Tier-1 attribute matched" and "the
    knob is absent on this box and nothing was checked", and the RUNBOOK's "a
    Tier-1 mismatch aborts the run" read as a guarantee the artifact could not
    support.
    """

    model_config = ConfigDict(extra="allow")
    source: str = NOT_MEASURED
    note: str = (
        "not populated: the host_readback phase has not run in this manifest "
        "(non-Linux, or the run stopped before it)"
    )
    first_read_utc: str | None = None
    last_read_utc: str | None = None
    reads: int = 0
    readings: list[HostReading] = Field(default_factory=list)
    first_readings: list[HostReading] = Field(default_factory=list)
    changed_during_run: list[dict] = Field(default_factory=list)
    tier1_failures: list[str] = Field(default_factory=list)
    tier1_not_evaluable: list[str] = Field(default_factory=list)
    tier2_notes: list[str] = Field(default_factory=list)


class ContainerTopology(BaseModel):
    """One sampled cgroup's placement and memory ceiling, as the kernel reports.

    Read off `cpuset.cpus.effective` / `memory.max` / `memory.swap.max` /
    `memory.swap.current` by the per-window sampler and aggregated here, so the
    cpusets in force are evidenced by the run artifact rather than by the
    compose file that requested them.

    `mem_swap_max` is the SETTING, recorded verbatim (it is `"max"`, `"0"` or a
    byte count, and an int cannot carry the first).
    `swap_current_max_bytes` is the OUTCOME: the largest `memory.swap.current`
    seen at any window edge. infra/compose.bench.yaml pins `memswap_limit` to
    `mem_limit`, which makes `memory.swap.max` 0, and this is the evidence that
    the setting bit -- anon pages swapped out leave `memory.stat anon` and would
    understate the published steady-state-memory-per-request headline with
    nothing in the artifacts to say so.
    `swap_peak_max_bytes` is the same outcome read from the kernel's lifetime
    high-water mark, and it is the half that survives a swap-in: a cgroup that
    swapped mid-window and faulted everything back is 0 in the field above and
    non-zero here.
    """

    model_config = ConfigDict(extra="forbid")
    cpuset_effective: str | None = None
    mem_limit_bytes: int | None = None
    mem_swap_max: str | None = None
    swap_current_max_bytes: int | None = None
    swap_peak_max_bytes: int | None = None
    expected_cpuset: str | None = None
    matches: bool | None = None
    windows: int = 0


class Topology(BaseModel):
    """Per-container cpuset and memory limit, read off the cgroups.

    Populated in finalization from the per-window cgroup summaries, plus the L3
    `shared_cpu_list` groups and `smt/control` the read-back phase records --
    which is what turns compose.bench.yaml's core-id note into an
    artifact. An empty `containers` map means nothing was recorded, not "no
    pinning"; `source` says which.

    CCD placement is `l3_groups` and nothing else: the L3 shared_cpu_list groups
    ARE the CCD discriminator on Zen and they are measured.

    A cpuset mismatch reaching finalization is RECORDED, never fatal: the only
    fix is a restart, and aborting at probe 200 of 282 destroys a 15 h cell to
    report something the artifact already carries per window. It should never
    get that far -- `cgroups.MultiSampler.cpuset_mismatches` raises on the run's
    FIRST measured window, before any cell time is spent.
    """

    model_config = ConfigDict(extra="forbid")
    source: str = NOT_MEASURED
    note: str = (
        "not populated: no cgroup window summary carried a cpuset read (the run "
        "stopped before any probe, or resource accounting was disabled)"
    )
    containers: dict[str, ContainerTopology] = Field(default_factory=dict)
    l3_groups: dict[str, str] = Field(default_factory=dict)
    smt_control: str | None = None


class PostgresConfigEcho(BaseModel):
    """spec/postgres/README.md's three-way split, each value tagged
    inherited/rescaled/pg18-default.

    Filled in finalization from `pg_settings` (name, setting, unit, source,
    boot_val, reset_val) while PostgreSQL is still up. EVIDENCE, never a gate:
    the declared values live in spec/postgres/postgresql.conf and a measured
    column that gates would make the spec unchangeable mid-round.
    """

    model_config = ConfigDict(extra="allow")
    source: str = NOT_MEASURED
    note: str = (
        "not populated: pg_settings was not read (PostgreSQL was down at "
        "finalization, or the run stopped before it)"
    )
    values: dict[str, dict] = Field(default_factory=dict)


class AchievedVsOffered(BaseModel):
    model_config = ConfigDict(extra="forbid")
    probe_id: str
    offered_rate: float
    achieved_rate: float
    ratio: float
    max_workers: int | None = None
    connections: int | None = None
    timeout_s: float | None = None


class ThrottleCounters(BaseModel):
    """cpu.stat throttle counters for one cgroup, summed across probe windows.

    A per-MEASURED-WINDOW delta, since W16. cpu.stat's nr_throttled and
    throttled_usec are cumulative over a cgroup's lifetime, and round 1 read
    them once at each window's END: for the force-recreated SUT that silently
    included warmup and the head-of-window discard, and for a cgroup that
    persists across probes -- postgres, redis, vegeta, the daemons -- summing it
    would have multiplied one lifetime total by the window count and published
    a pathology that never happened. Both edges are read now and the sum is a
    sum of increments, so a non-zero value means "throttled inside a measured
    window", which is the claim the field name always implied.

    `windows` counts the windows that CONTRIBUTED a delta, which is not
    necessarily every window sampled: an unreadable counter contributes None,
    never a zero (see `nr_throttled`).
    """

    model_config = ConfigDict(extra="forbid")
    label: str
    role: str = ""
    nr_throttled: int | None = None
    throttled_usec: int | None = None
    windows: int = 0                          # probe windows summed here
    source: str = NOT_MEASURED


class FaultCounts(BaseModel):
    """memory.stat pgmajfault/pgfault deltas over the measured windows.

    None (not 0) when the run's probe artifacts carry no fault reads: a zero
    major-fault count is a strong claim (no page-cache misses at all) and must
    never be manufactured by a default.
    """

    model_config = ConfigDict(extra="forbid")
    label: str
    role: str = ""
    major_faults: int | None = None
    minor_faults: int | None = None
    windows: int = 0
    source: str = NOT_MEASURED


class ResourceDelta(BaseModel):
    """One cgroup's CPU and memory over the run's measured windows.

    Three different kinds of number live here and the names have to carry the
    difference. `cpu_usage_usec_delta` is a SUM of window deltas.
    `mem_anon_p99_bytes` and `mem_current_p99_bytes` are MAXES of per-window
    in-window percentiles. `mem_peak_bytes` is a high-water mark the kernel
    maintains over the cgroup's whole LIFETIME -- for the recreated SUT that is
    one probe, for postgres it is the entire run -- which is what `persistent`
    says and what round 1 disclosed only in the report.

    `mem_anon_p99_bytes` is the SUT's memory figure and is NOT a general one:
    PostgreSQL's working memory is `shared_buffers`, which is shmem and page
    cache, not anon. `mem_current_p99_bytes` is the level to read for a
    database or a cache.
    """

    model_config = ConfigDict(extra="forbid")
    label: str
    role: str = ""
    persistent: bool = False
    cpu_usage_usec_delta: int | None = None   # summed over the measured windows
    mem_anon_p99_bytes: int | None = None     # max of the per-window anon p99
    mem_current_p99_bytes: int | None = None  # max of the per-window memory.current p99
    mem_peak_start_bytes: int | None = None   # min of the per-window memory.peak at start
    mem_peak_bytes: int | None = None         # max of the per-window memory.peak
    windows: int = 0
    source: str = NOT_MEASURED


class LoadGeneratorOverhead(BaseModel):
    """vegeta / dockerd / containerd cgroup CPU over the measured windows.

    A separate section, not a `resource_deltas` row, and the separation is the
    point: these processes serve the MEASUREMENT, not the workload. Their cost
    is real and single-host accounting is only auditable if it is published, but
    dividing it by the request count would price the harness as though the stack
    under test had spent it. Nothing downstream may add these to a per-request
    figure.

    Measured on this box: containers do not nest inside either daemon unit
    (cgroup driver `systemd` puts them in /system.slice/docker-<id>.scope), so
    these rows do not double-count the SUT.
    """

    model_config = ConfigDict(extra="forbid")
    label: str
    role: str = ""
    cpu_usage_usec_delta: int | None = None
    windows: int = 0
    excluded_from_per_request_cost: bool = True
    source: str = NOT_MEASURED


class Errors(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = 0                            # non-2xx responses over all probes
    rate: float = 0.0
    total_requests: int = 0
    status_codes: dict[str, int] = Field(default_factory=dict)
    probe_summaries: int = 0                  # vegeta summaries aggregated
    source: str = NOT_MEASURED


class CacheStats(BaseModel):
    """Redis INFO keyspace_hits/misses delta, summed over measured windows.

    `source` is explicit: `not_measured` means no probe recorded a Redis INFO
    sample, which is NOT the same as "0 hits". spec/cache.md constructs a 0.80
    target hit rate and this section is RECORDED AND REPORTED beside it, never
    asserted against it: `note` carries the constructed target so the pair is
    readable from the manifest alone, and no code branches on the comparison.
    """

    model_config = ConfigDict(extra="forbid")
    keyspace_hits: int | None = None
    keyspace_misses: int | None = None
    hit_rate: float | None = None             # recorded vs the target in `note` (cache.md)
    windows: int = 0
    source: str = NOT_MEASURED
    note: str = ""


class AggregateProvenance(BaseModel):
    """Per-SECTION provenance for the list-valued aggregates.

    A row-level `source` cannot rescue an EMPTY list: `throttle_counters: []`
    reads as "measured, nothing to report". Each list section names here what it
    was built from, and stays `not_measured` when finalization found no artifact
    to build it from.
    """

    model_config = ConfigDict(extra="forbid")
    achieved_vs_offered: str = NOT_MEASURED
    throttle_counters: str = NOT_MEASURED
    fault_counts: str = NOT_MEASURED
    resource_deltas: str = NOT_MEASURED
    load_generator_overhead: str = NOT_MEASURED


class CapacityRegateStatus(BaseModel):
    """Stage 2: the headroom question re-asked at the rates the cells REALLY ran.

    Round 1's gate took its margin against projected demand and the cells then
    ran 4-6x higher; PG never broke, but nothing ever tested it there (R1-A6).
    This section is the test, and it is a DIRECT MEASUREMENT rather than a second
    projection: `pg_utilization` is PostgreSQL's own cgroup cpu.stat delta over
    the first cell's operating-point windows divided by (cores x wall seconds),
    and it must sit at or below `pg_utilization_ceiling` = 1/headroom_factor.

    It is stamped RETROACTIVELY. The first cell cannot be un-run -- it is the
    thing being measured -- so `cells_measured_before_regate` names every cell
    whose windows were already on disk when the verdict was reached. On a
    non-passing verdict those cells' numbers were measured against a DB whose
    headroom at their own rates was NOT established, and every artifact that
    quotes them has to carry that.

    `projected_db_rps_at_realized_rates` is CONTEXT unless
    `projection_evaluated` is true: the create-only ladder's knee is a lower
    bound whenever the ladder ran out of rungs while still tracking, and a lower
    bound cannot fail a headroom test honestly, so the projection rule is only
    part of the decision when the ladder actually broke.
    """

    model_config = ConfigDict(extra="forbid")
    verdict: str | None = None
    detail: str = ""
    rule: str = ""
    # The decision rule.
    pg_utilization: float | None = None       # MAX over slots; None => not measured
    pg_utilization_ceiling: float | None = None
    pg_utilization_by_slot: dict[str, float] = Field(default_factory=dict)
    pg_utilization_binding_slot: str | None = None
    pg_cpu_seconds: float | None = None
    window_seconds: float | None = None
    windows: int = 0
    windows_by_slot: dict[str, int] = Field(default_factory=dict)
    pg_cores: int | None = None
    pg_cores_source: str = ""
    utilization_ok: bool | None = None
    # The realized operating point the verdict is about.
    realized_rates_by_slot: dict[str, float] = Field(default_factory=dict)
    realized_knee_rps: float | None = None    # max over slots (PG serves both)
    # Scoped to the UTILIZATION measurement above, not to the whole record: the
    # cache hit rate below is taken over a WIDER set (all of the cell's confirm
    # windows) and carries its own window count in `cache_hit_rate_source`.
    window_selection: str = ""
    # The secondary projection rule (see the class docstring).
    measured_cache_hit_rate: float | None = None
    cache_hit_rate_source: str = ""
    projected_db_rps_at_realized_rates: float | None = None
    pg_capacity_rps: float | None = None
    projection_evaluated: bool = False
    projection_ok: bool | None = None
    projection_skipped_because: str = ""
    headroom_factor: float | None = None
    cells_measured_before_regate: list[str] = Field(default_factory=list)
    degraded: bool = False
    degraded_allowed_by: str | None = None


class CapacityGateStatus(BaseModel):
    """The gate verdict, stamped so every downstream artifact inherits it.

    `degraded` is True whenever the gate did NOT return `pass`, including a run
    that aborted before a single cell (it fails safe: an aborted run's partial
    artifacts must not read as clean). `degraded_allowed_by` is what separates
    the two cases: it names the opt-out flag that let the measured cells run
    ANYWAY, and is null on a run that stopped. The headline of a degraded run is
    not a clean cost-at-a-latency-promise number, so the caveat travels with the
    manifest as well as verdict.json.

    `pg_knee_rps` is the MEASURED create-only knee at `pg_knee_measured_at_cores`,
    never a scaled number. The linear core-scaling projection the lever logic
    used to decide whether to spend the pg_cores lever is
    `pg_capacity_projected_rps`, and `pg_capacity_projection_basis` spells out
    the arithmetic. The two must never share a field.
    """

    model_config = ConfigDict(extra="forbid")
    verdict: str | None = None
    detail: str = ""
    pg_knee_rps: float | None = None          # MEASURED, at pg_knee_measured_at_cores
    pg_knee_measured: bool | None = None
    pg_knee_usable_rungs: int | None = None
    pg_knee_measured_at_cores: int | None = None
    # Linear core-scaling PROJECTION (assumption, not a measurement).
    pg_capacity_projected_rps: float | None = None
    pg_capacity_projection_basis: str = ""
    projected_db_rps: float | None = None
    headroom_factor: float | None = None
    levers_fired: list[str] = Field(default_factory=list)
    levers_applied: list[str] = Field(default_factory=list)
    levers_not_applied: dict[str, str] = Field(default_factory=dict)
    remeasured_pg_knee_rps: float | None = None
    # The mixed-workload knee stage 1 projected DB load FROM, and the arithmetic
    # behind it. Round 1's proxy was an inline `ladder_start_rps * 8.0` that
    # reached no artifact, so no reader of that run could see that a 1.72x margin
    # had been taken against 2,160 rps while the cells ran at 9,244-13,312.
    projected_knee_rps: float | None = None
    projected_knee_basis: str = ""
    regate: CapacityRegateStatus | None = None
    degraded: bool = False
    degraded_allowed_by: str | None = None    # the flag that permitted the run


class EffectiveWorkload(BaseModel):
    """Spec-vs-effective workload: what the cells ACTUALLY ran.

    spec/ is frozen (config_hash), so the capacity-gate levers can only alter run
    state. This section is the disclosure of that alteration: read it next to
    identity.config_hash to see spec-vs-effective at a glance.
    """

    model_config = ConfigDict(extra="forbid")
    spec_mix: dict[str, int] = Field(default_factory=dict)
    effective_mix: dict[str, int] = Field(default_factory=dict)
    spec_write_share: float | None = None
    effective_write_share: float | None = None
    spec_cache_hit_rate: float | None = None
    effective_cache_hit_rate: float | None = None
    spec_pg_cores: int | None = None
    effective_pg_cores: int | None = None
    write_share_absorbed_by: str | None = None
    levers_fired: list[str] = Field(default_factory=list)
    levers_applied: list[str] = Field(default_factory=list)
    levers_not_applied: dict[str, str] = Field(default_factory=dict)
    env_overrides: dict[str, str] = Field(default_factory=dict)
    note: str = ""

    @property
    def differs_from_spec(self) -> bool:
        return (
            self.spec_mix != self.effective_mix
            or self.spec_cache_hit_rate != self.effective_cache_hit_rate
            or self.spec_pg_cores != self.effective_pg_cores
        )


class Spread(BaseModel):
    """min / median / max of a per-probe quantity, with the sample count.

    A single number cannot describe a per-probe draw: the ladder spans a 20x
    range of offered rates, so the requests one probe draws and the ids it names
    both move by that factor across a cell. `n` is the probes the spread was
    taken over, so an empty section can never read as a measured one.
    """

    model_config = ConfigDict(extra="forbid")
    n: int = 0
    min: float | None = None
    median: float | None = None
    max: float | None = None

    @classmethod
    def of(cls, values: "list[float] | list[int]") -> "Spread":
        if not values:
            return cls()
        ordered = sorted(float(v) for v in values)
        mid = len(ordered) // 2
        median = (ordered[mid] if len(ordered) % 2
                  else (ordered[mid - 1] + ordered[mid]) / 2.0)
        return cls(n=len(ordered), min=ordered[0], median=median, max=ordered[-1])


class WorkingSet(BaseModel):
    """The DISTINCT keys the READ path asks for, per probe.

    Round 1 generated ONE targets file per slot and pointed the warmup, the
    head-of-window discard and the measured attack at it. vegeta reads a targets
    file from the top on every invocation, so all three consumed a prefix of the
    same sequence -- and the warmup runs immediately after a Redis FLUSHALL,
    which means it did not merely leave a warm cache, it populated the cache
    with exactly the invoice ids the measured window was about to request
    (R1-A7). Every attack now draws its own PCG64 stream
    (`util.pcg64.stream_for`), sized at `vegeta.DRAW_OVERDRAW` x the requests it
    will issue so the stream never wraps inside its own window.

    Scope, precisely: these counts describe the ids the MEASURED draws named.
    The write path is not bounded by them -- create_invoice INSERTs rows whose
    ids appear in no target and are never read back -- so this is the read-side
    working set, not "every row the run touched".

    There is deliberately no union across probes. Accumulating one would have to
    hold every id the run drew in the finalizing process's memory, and a resume
    starts a new process: the union would come out silently short on exactly the
    runs that most need reading. The per-probe spread is what the artifacts can
    support, and it is also the quantity the residency question asks about.

    `prewarm_containment_per_probe` is the read path's measured residency floor
    and is NOT a claim that nothing pre-warms a window. See the field.

    `body_pool_*` describes the shared request bodies. They carry no URL, so one
    pool serves both slots and both halves of an RCB block send byte-identical
    payloads. It is also the one disclosure this design COSTS: a create body is
    drawn uniformly from the pool rather than built from a fresh Zipf draw, so
    the create path's customer_id distribution is the pool's empirical Zipf
    sample rather than the Zipf itself -- identical on the head, thinner in the
    tail. `body_pool_distinct_customer_ids` is that sample's size, published so
    the thinning is a number rather than an adjective. The READ path, which is
    what R1-A7 is about, is URL-only and unaffected.
    """

    model_config = ConfigDict(extra="forbid")
    # How targets were produced. `not_measured` means no probe recorded a draw.
    draw_mode: str = NOT_MEASURED
    # The label set the per-probe PCG64 stream is derived from, verbatim, so a
    # reader can regenerate any probe's exact request sequence.
    draw_seed_formula: str = ""
    # There is no `probes_recorded`: every spread below carries its own `n` over
    # the same sample, and a separate count is a field that can disagree with
    # the numbers it is rendered beside.
    requests_drawn_per_probe: Spread = Field(default_factory=Spread)
    distinct_invoice_ids_per_probe: Spread = Field(default_factory=Spread)
    distinct_customer_ids_per_probe: Spread = Field(default_factory=Spread)
    # GENERATION PLUS SHIP, per probe, over every attack it drew. The copy is
    # the half that scales with the file, so a generation-only figure would
    # understate exactly the rungs where the cost is largest.
    draw_seconds_per_probe: Spread = Field(default_factory=Spread)
    # The share of a measured window's invoice-id READ REQUESTS whose id an
    # attack before that window had already drawn -- request-weighted, per
    # probe, measured from the run's own draws (`vegeta.prewarm_containment`).
    #
    # Drawing a separate stream per attack removes the sequence-correlated
    # pre-warm, but two independent Zipf draws over one keyspace still share
    # the head, and the head is most of the traffic: measured on this box that
    # is 52-67% of a window's reads, over rung and warmup length (60.8% at
    # 500 rps and 66.9% at 13,312 after a full ten-segment warmup plus the
    # discard; 52.0% at 500 rps after a warmup that settled at the two-segment
    # minimum), at `live_runner._warmup_rate`'s value of 618 rps. A reader
    # cannot interpret the read path without this number, so it is published
    # beside the constructed target hit rate.
    prewarm_containment_per_probe: Spread = Field(default_factory=Spread)
    body_pool_create_bodies: int | None = None
    body_pool_quote_bodies: int | None = None
    body_pool_distinct_customer_ids: int | None = None
    seeded_invoices: int | None = None
    seeded_customers: int | None = None
    cache_ttl_seconds: int | None = None
    # WHERE cache_ttl_seconds came from (lever override / env / compose default);
    # `not_measured` means the effective TTL could not be established.
    cache_ttl_source: str = NOT_MEASURED
    # The measured window each draw was sized for: the TTL comparison is against
    # this, not against a replay period, because nothing replays.
    measured_window_seconds: int | None = None
    note: str = ""


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = MANIFEST_SCHEMA_VERSION
    created_utc: str = Field(default_factory=rfc3339_micros)
    identity: Identity
    versions: Versions = Field(default_factory=Versions)
    # Run-level, CELL-INDEPENDENT knobs only (core pinning). Per-cell knob values
    # live in knobs_by_cell; nothing cell-scoped may be written here.
    knobs_by_slot: dict[str, Knobs] = Field(default_factory=dict)
    knobs_by_cell: dict[str, CellKnobs] = Field(default_factory=dict)
    capacity_gate: CapacityGateStatus = Field(default_factory=CapacityGateStatus)
    effective_workload: EffectiveWorkload = Field(default_factory=EffectiveWorkload)
    working_set: WorkingSet = Field(default_factory=WorkingSet)
    # Gates spec/slo.yaml declares that the runner does NOT implement. Recorded
    # so an unimplemented gate can never be read as a passed one.
    unimplemented_warmup_gates: list[str] = Field(default_factory=list)
    host_state: HostState = Field(default_factory=HostState)
    topology: Topology = Field(default_factory=Topology)
    postgres_config: PostgresConfigEcho = Field(default_factory=PostgresConfigEcho)
    achieved_vs_offered: list[AchievedVsOffered] = Field(default_factory=list)
    throttle_counters: list[ThrottleCounters] = Field(default_factory=list)
    fault_counts: list[FaultCounts] = Field(default_factory=list)
    resource_deltas: list[ResourceDelta] = Field(default_factory=list)
    load_generator_overhead: list[LoadGeneratorOverhead] = Field(default_factory=list)
    # What each of the four list sections above was built from; a section that
    # stayed [] says so here instead of reading as a measured emptiness.
    aggregate_provenance: AggregateProvenance = Field(default_factory=AggregateProvenance)
    cache_stats: CacheStats = Field(default_factory=CacheStats)
    errors: Errors = Field(default_factory=Errors)
    # Resource accounting is OFF on non-Linux (smoke); recorded explicitly.
    resource_accounting: str = "enabled"
    # Set when finalize_manifest could not aggregate an artifact. Finalization
    # never raises (it must not lose a completed run), so the failure is recorded
    # here rather than swallowed.
    finalization_error: str | None = None

    # ----- persistence ----------------------------------------------------- #
    def path(self) -> Path:
        return paths.run_dir(self.identity.run_id) / "manifest.json"

    def save(self) -> None:
        paths.write_text_atomic(self.path(), self.model_dump_json(indent=2))

    @classmethod
    def load(cls, run_id: str) -> "RunManifest | None":
        """The run's manifest, or None if it does not exist.

        None means ABSENT and nothing else. Its only caller writes
        `RunManifest.load(run_id) or new_manifest(...)`, so returning None for an
        unreadable manifest would silently start a fresh one over a run already
        in progress; a schema problem raises, naming both versions.
        """
        p = paths.run_dir(run_id) / "manifest.json"
        if not p.exists():
            return None
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ManifestSchemaError(f"{p}: not valid JSON ({exc})") from exc
        found = doc.get("schema_version") if isinstance(doc, dict) else None
        if found != MANIFEST_SCHEMA_VERSION:
            raise ManifestSchemaError(
                f"{p}: manifest schema v{found!r}, this build writes "
                f"v{MANIFEST_SCHEMA_VERSION}. That run cannot be resumed by this "
                f"code; start a fresh run_id (the old manifest is left untouched)."
            )
        try:
            return cls.model_validate(doc)
        except ValidationError as exc:
            raise ManifestSchemaError(
                f"{p}: does not validate against manifest schema "
                f"v{MANIFEST_SCHEMA_VERSION}:\n{exc}"
            ) from exc


def new_manifest(run_id: str, *, config_hash: str | None = None) -> RunManifest:
    return RunManifest(
        identity=Identity(
            run_id=run_id, config_hash=config_hash,
            schema_sql_sha256=_schema_sql_sha256(),
        )
    )


def _schema_sql_sha256() -> str | None:
    """sha256 of `spec/schema.sql`, or None if it is unreadable."""
    p = paths.repo_root() / "spec" / "schema.sql"
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# provenance helpers
# --------------------------------------------------------------------------- #
def git_identity() -> tuple[str | None, bool | None]:
    """(git_sha, dirty) for the repo the runner is executing from.

    Read-only git plumbing; returns (None, None) when git is absent or the tree
    is not a repository (the manifest then records the absence, not a guess).

    `results/` is excluded from the dirty check: the run WRITES its own
    artifacts there (ledger.json exists before the identity stamp), so without
    the exclusion every run, including one launched from a pristine commit,
    would stamp itself dirty. Run outputs cannot alter the measured binaries;
    an untracked SOURCE file still counts as dirty (docker builds copy the
    tree, so it would ship).
    """
    root = str(paths.repo_root())
    sha = run(["git", "-C", root, "rev-parse", "HEAD"], timeout=15)
    if not sha.ok:
        return None, None
    status = run(
        ["git", "-C", root, "status", "--porcelain", "--", ":(exclude)results"],
        timeout=30,
    )
    dirty = bool(status.stdout.strip()) if status.ok else None
    return sha.stdout.strip(), dirty
