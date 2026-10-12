# METHODOLOGY: how the numbers are produced

> **Every procedure below is implemented.** This document describes what
> `bench/` does, not what it is planned to do. A step that has no code and no
> passing test does not appear here: the rule this round runs under
> (`PROGRESS.md`, "the governing rule") is that nothing is declared ahead of its
> implementation. Where a number is a frozen dial it is cited to `spec/`, which
> is hashed into every run's `config_hash`.

The sections below are the method the runner executes. The run it produced is
`results/full-20260827T174611-fedora`: its numbers, and everything it does not establish, are in that
run's `report/report.md` and in [`results/README.md`](../results/README.md).

## Headline metric

Cost at a fixed latency promise. At the rate where a stack's mixed-workload p99
crosses 20 ms (`spec/slo.yaml: slo.threshold_ms`), report

- **per-request CPU-ms** — the SUT cgroup's `cpu.stat` delta over the measured
  window, divided by the requests serviced in it;
- **steady-state anonymous memory** — the p99 of `memory.stat.anon` sampled at
  50 ms across the window, per request. Stated plainly: this divides a LEVEL by
  a FLOW, so at own-knee scope its Java:.NET ratio is mechanically coupled to
  the knee-rate ratio (a faster slot spreads the same footprint over more
  requests). The raw per-slot `anon_p99_mb` level is published beside it in
  every roll-up so a reader can separate footprint from rate;
- **an illustrative $/M-requests figure** — `analysis/report.py:cost_per_million`
  computes `usd_per_million = cpu_ms_per_req * vcpu_hour_usd / 3.6`, prints the
  formula, the price and where the price came from beside the number, and emits
  a sensitivity band. The price defaults to a module constant ($0.04/vCPU-hour)
  and the operator can override it with `LEDGERBENCH_VCPU_HOUR_USD`; a bad
  override is reported rather than silently ignored. The figure rescales the CPU
  number, it does not add information, and it is labelled that way.

as a Java:.NET ratio.

The p99 is the load generator's own report over the whole measured window — a
percentile of all request latencies — never a percentile of per-second
percentiles. The per-second p99 series is recorded too (`<slot>.p99_per_second.csv`)
and is used only for the stationarity test and the warmup flatness gates.

Two properties of the metric decide the design of everything below.

**Each stack must be measured at its own SLO-crossing rate.** Equal offered rate
is not an equal latency promise: if one stack meets the SLO at a shared rate and
the other does not, the resulting ratio is *cost at equal load*, which is a
different quantity. The ladder, the fit, the confirm windows and the soak are
therefore per slot (`knee_by_slot`, `confirm_rates_by_slot`).

**Per-request cost is not assumed rate-independent.** A single-operating-point
ratio is published *with its rate*, and the cost-vs-rate curve is published
beside it as a first-class result (`analyze._cpu_vs_rate_for_cell`), with each
slot's operating point marked on it. One concrete mechanism deserves naming:
under the fixed 300 s cache TTL the realized Redis hit rate RISES with the
offered rate (more requests inside one TTL window), so at own-knee operating
points the faster slot enjoys a higher hit rate and less DB work per request.
The realized hit rate is measured and printed per window, and the
`common_rate` paired scope removes the mechanism entirely.

## Load + knee procedure

Open-loop constant-rate attacks (vegeta), one probe per (cell, stage, rate,
block, slot).

**Ladder.** Geometric, seeded at `ladder.start_rate_rps_default` (500) and
stepped by `ladder.step_factor` (1.2), `ladder.reps_per_rung` (3) interleaved
reps per rung. The ladder climbs **per slot**: a slot that has bracketed its own
SLO crossing stops while the others keep climbing, so the rungs above the first
crosser's knee are actually offered rather than inferred. A rung above the first
crosser's knee is therefore a solo block — one probe, one slot — and nothing
stamps that as a flag: the block's own membership says it, and a derived flag
cannot disagree with the record.

**The rung ceiling is declared.** `ladder.rung_ceiling` (40) is in `spec/`, so it
is inside `config_hash`. The ladder stage records `rung_ceiling_rps` — the top
rate actually offered — beside `crossed_by_slot` and
`ceiling_reached_without_crossing`. A slot that reaches the ceiling without
crossing has no operating point at the latency promise: at measured scale the run
**aborts**, naming the slot and its top rate. The opt-out
(`LEDGERBENCH_ALLOW_SUB_KNEE_LADDER=1`) stamps the ladder stage `degraded` with
`degraded_allowed_by` naming the variable, rather than letting the gap go
unrecorded.

**One crossing predicate.** `analysis/knee.py` holds the single definition of
"crossed the SLO", and both the ladder's stop rule and the published fit call it.
`SloAtRate.meets_slo` — mean confirm-window p99 against the threshold — is a
separately named conformance quantity, and the report flags a slot that reads
`meets_slo` at a rate above its own fitted knee rather than narrating the
contradiction in prose.

**Fit.** Isotonic (PAVA, `analysis/pava.py`) monotone fit of p99 against offered
rate, one fit per slot over that slot's own rungs; the knee is the SLO crossing
read off that fit. The interval around it is bootstrapped over reps
(`ladder.knee_ci: bootstrap_over_reps`) and is a spread indicator over the reps
that survived the gates, which is what the report calls it. Stated plainly: at
the spec's rep counts (`reps_per_rung: 3`) **no inferential knee CI exists in
any artifact**, the confirm stage runs at one rate per slot and cannot re-fit a
crossing, and the cost-at-knee verdicts condition on the point knee as if
known. The published cost-vs-rate curve is the mitigation: it shows how the
headline cost would move if the true knee sits elsewhere in the bracket.

**Confirm and soak.** `ladder.confirm_reps` (10) randomized complete blocks at
each slot's **fitted knee** — `confirm_rate_basis: fitted_knee` records where the
rate came from — then a `soak.duration_seconds` (600) window at the same rate. A
confirm window at an interpolated knee can land above the true crossing, with
p99 over the promise: that is the conformance statement, not a failure. In the
published run (`results/full-20260827T174611-fedora`) the confirm windows at the
fitted knees averaged 26.6 to 28.9 ms and the single soak windows read 21.1 ms
(Java) and 45.0 ms (.NET): the fitted knees are optimistic, and the report's
conformance table says so. The soak therefore measures drift at the fitted knee
rate, not comfortably under the promise.

**Stationarity.** Mann-Kendall on the per-second p99 series per probe
(`spec/slo.yaml: stationarity`), with a practical-significance band: the verdict
fails only if Mann-Kendall is significant at `stationarity.alpha` **and** the
Theil-Sen slope over the window exceeds `min_slope_ms_per_min`. **The verdict is
RECORD-ONLY**: per-probe fails and an `any_fail` roll-up are published in
`fits.json` and the report, and nothing is excluded on it -- a reader weighs a
flagged probe, the pipeline does not silently drop it. Supra-knee probes report
saturation behaviour only.

**Coordinated-omission safety** rests on ONE scored check: vegeta's achieved
rate must track the offered rate within
`co_validation.achieved_vs_offered_tolerance_pct` (2.0) on every probe -- an
open-loop generator that absorbed a stall would fall short of the offered
rate, and the per-probe ratio is recorded and gated. (`make smoke` also runs a
`docker pause`/unpause liveness step, `co_validation.stall_test`; no attack
runs across that freeze, so it proves the freeze plumbing and nothing about
the generator.)

## Warmup

Every probe is warmed before its measured window against the same gate set on
both stacks, up to `warmup.hard_cap_seconds` (300) — a ceiling, not a duration:
warmup exits the moment the gates pass. Four gates are scored per probe:

| gate | what it tests |
|---|---|
| `per_endpoint_min_calls` | `spec/slo.yaml: warmup.per_endpoint_min_calls`, per endpoint |
| `p99_flat` | no trend in the trailing per-second p99 window |
| `gc_steady` | heap committed flat **and** collection cadence flat, on both slots |
| `pools_warm` | `db_pool.total == max` (min=max policy) and Redis ready |

`gc_steady` reaches the cadence series differently per runtime, and each probe's
`<slot>.warmup.json` records which one it used under
`gc_steady.detail.cadence_basis`: the CLR publishes gen0/gen1/gen2 directly; the
JVM is counted off the per-collector cycle beans, because no JVM bean set maps
onto the CLR's three generations without meaning something different per
collector. The bean names are frozen in `registry.GcSemantics.cadence_beans` and
`spec/runtime-stats.md`.

The per-probe record names **every** gate evaluated and every gate failed, and
`under_warmed` is derived from `bool(gates_failed)` so the flag and the list
cannot disagree. A gate declared in `spec/slo.yaml` that
`bench/ledgerbench/warmup.py` does not implement is emitted per probe as
`status: not_implemented` rather than being silently absent; a test asserts
`set(spec.warmup.gates) - warmup.IMPLEMENTED_GATES == set()`, so a gate cannot be
declared ahead of the code that scores it. A posture may waive a gate — via a
cell's `warmup_gates` with a required rationale — and the waiver is recorded per
probe as `posture_waived` / `not_applicable` with `enforced: false`. A cell that
enables a gate the registry marks unsatisfiable under its posture is refused at
plan time, before any probe runs.

Probes that fail a gate are excluded from fits, confirms and the resource
roll-ups; `analyze` prints the used and flagged-excluded counts on every row and
marks rows with no measurement-grade window at all as posture-only (`*`). A rung
that loses all its reps fails the run rather than being fitted around.

p999 is **recorded, not gated**: every probe writes
`<slot>.warmup_latency_per_second.csv` with `segment,second,p99_ms,p999_ms,samples`,
so the per-second q=0.999 series and the bucket size behind each row are both in
the artifacts.

## Statistics

The primary equivalence verdict is a **paired-difference CI on per-request cost**
— CPU-ms per request, with anon-RSS per request and $/M alongside — paired by
interleave block (`ladder.interleave: randomized_complete_blocks`), against the
pre-registered `equivalence.margin_pct` (±5%, relative to the reference slot).
Inside the margin is a tie; outside it, direction plus a Cohen's `d_z` effect
size.

The CI method is pre-registered and there is no researcher degree of freedom in
it: the verdict is **always** read off the paired-t interval on the per-block
differences, whatever the differences look like. Auto-switching to a bootstrap on
a normality screen would be a forking path, and the small-n percentile bootstrap
under-covers at n≈10 anyway. Shapiro-Wilk and the percentile bootstrap are still
computed and published (`shapiro_p`, `boot_ci_low_pct`, `boot_ci_high_pct`), as
diagnostics that let a reader see whether the assumption is strained and where a
bootstrap would have put the interval — they never gate the verdict.

Pairing is on `(cell, stage, block)`, not on rate: with each slot at its own
knee, a rate-keyed pairing would find no partner and render "not available"
without an error. Each verdict states its own operating points in
`rates_by_slot`, and carries `verdict_scope`:

- **`own_knee`** — each slot at its own SLO-crossing rate. This is the headline,
  because it is what "cost at a fixed latency promise" is defined as.
- **`common_rate`** — every slot at one shared rate. Per-request cost is not
  rate-independent, so an own-knee cost comparison confounds *language* with
  *position on the cost-vs-rate curve*; the common-rate contrast removes that
  confound and answers the other question. It is emitted whenever the slots'
  own rates actually differ.
- **`declared_operating_point`** — the fallback label. `own_knee` claims an SLO
  crossing per slot; if a slot's ladder reached the ceiling without crossing, its
  confirm rate is a top rung rather than a knee, and the verdict says so instead
  of borrowing the headline's name.

`verdict_scope` is on every verdict, so the scopes can never be conflated. The
three paired metrics are `cpu_ms_per_req`, `anon_mb_per_req` and
`usd_per_million`; all three are lower-is-better, and `SlotSet.reference` decides
which side the percentage is *of*.

p99 itself is published as a **conformance statement, not a verdict**: per slot
at its confirm rate, the mean confirm-window p99 and its distance from 20 ms.
Both stacks are placed at their own fitted crossing by construction, so a paired
p99 difference there would be a guaranteed tie manufactured by the design.

A verdict computed over fewer blocks than the pre-registered
`ladder.confirm_reps` floor carries `under_min_blocks` in its record and is
labelled as such in the report, so a two-block verdict cannot render identically
to a ten-block one.

## Resource accounting

cgroup v2 is the ground truth, with paths resolved through `/proc/<pid>/cgroup`.
Six cgroups are sampled on the same tick around every measured window:

| cgroup | role | used for |
|---|---|---|
| the SUT container | `sut` | the headline CPU and anon-RSS figures |
| `postgres` | `database` | the DB-CPU-per-request column |
| `redis` | `cache` | cache utilization |
| `vegeta` | `load_generator` | generator overhead |
| `docker.service` | `container_runtime` | runtime overhead |
| `containerd.service` | `container_runtime` | runtime overhead |

The last three carry roles that keep their CPU out of every per-request figure.
Both daemon units are sampled rather than dockerd alone: the containerd-shim
relays the SUT's stdout, so `docker.service` by itself under-counts the runtime
by the part of it on the logging path.

Every counter is a **window delta**, which is what makes sampling long-lived
cgroups sound: an OOM kill inside the window fails the probe it happened in, and
a lifetime counter would latch and make the run unresumable.

PostgreSQL's `cpu.stat` delta over each measured window is published per request
beside the SUT's as the **DB-CPU-per-request column**. It exists because round 2
is ORM against ORM with different generated SQL: without it, "Java costs more
CPU" cannot be separated from "Hibernate's SQL plans cost PostgreSQL more".
`spec/db-access.md` names what that column is and the limits that travel with it;
it is an attribution at the `(cell, offered rate, slot)` key and no finer.

Window scalars are persisted for **every** sampled cgroup — CPU delta, anon p99,
`memory.peak`, OOM counts, throttle deltas, swap current and peak — because the
raw 50 ms trace alone loses both window edges. An OOM kill inside a measured
window **fails that probe outright** — the SUT is stopped and the probe returns
not-ok — and `throttle_clean` / `oom_clean` / `swap_*_clean` verdicts ride with
every resource roll-up, because a throttled window deflates CPU and a swapping
one invalidates the memory figure. Both are stated in the artifacts rather than
inferred from the layout.

`/runtime-stats` is polled through warmup and the measured window and **every
poll is flushed**: each probe writes `<slot>.runtime_stats.jsonl` plus a
`.summary.json` carrying the start and end snapshots, so GC collection counts and
pause totals are window deltas in the artifacts rather than live-only readings.
The warmup window's trace is written too, since it carries the heap and cadence
series `gc_steady` actually tested.

Core parallelism is pinned and asserted rather than inferred: each stack's
processor-count knob is set to the SUT core count and read back from
`/runtime-stats` per run.

## Host state

`host_setup` tunes the box; the `host_readback` phase then **reads every tuned
value back off it**, on run start and on every resume, into `manifest.host_state`.
A Tier-1 mismatch aborts the run. An attribute the box does not expose is
recorded as `tier1_not_evaluable` rather than as a pass, and a manifest whose
read-back never ran keeps `source: not_measured` — an empty `HostState` is the
absence of a reading, never "measured and default". Values that changed between
the run's first reading and its latest are appended to `changed_during_run`
rather than overwritten.

## Run matrix

One headline cell, mixed workload:

| cell | posture |
|---|---|
| `headline.mixed.gc-default` | flag defaults on both sides: JVM `MaxRAMPercentage=25` + G1, CLR ≈75% + DATAS |

(A second, heap-equalised cell — JVM 75% + generational ZGC vs the CLR with
DATAS off — was declared while PROGRESS.md D6 left the heap axis open, and was
deleted when the G6 rehearsal (`results/rehearsal-20260827T111408-fedora`)
measured the JVM's heap committed peaking at 298 MB against the ~3 GiB default
ceiling: the ceiling never binds, so the equalised cell measured nothing the
defaults cell does not. The full run's maximum was 328 MB (its report, "GC and
pool activity"). D6's pre-registered rule: one cell, defaults, done.)

One variant is declared and is executed only when the operator names it in
`LEDGERBENCH_RUN_VARIANTS`, because it is a full cell:
`variant.npgsql-default-prepare`, which measures Npgsql's documented
`Max Auto Prepare=0` default against the pre-registered baseline of 20 (D4 —
PgJDBC auto-prepares at `prepareThreshold=5` by default, so a Java-prepared vs
.NET-unprepared headline would measure the prepare lever rather than the
runtimes). Decision D5 removed the once-declared `variant.ado` /
`variant.dapper` cells: the ORM-cost axis is the round-1 post's result, and the
`ado` data layer is exercised only by the harness shakedown slot set
(`LEDGERBENCH_SLOT_SET=shakedown`), not as a cell.

Phase order is `doctor` → `host_setup` → `host_readback` → `seed` →
`capacity_gate` → `validation` (the cross-service equivalence suite) → the cells,
with the capacity re-gate after the first cell (guarding any later cell — an
opted-in variant, in the one-cell headline matrix) → the opted-in variants →
`host_restore`. Runs are resumable at probe granularity, and a resume re-runs
`doctor`, re-applies host tuning, re-reads host state, re-verifies the database
invariants and redoes whole any interleave block a crash split.

## The capacity gate

The gate exists to prove the database is not the thing being measured, and it is
**two-stage**.

**Stage 1, before the cells.** Drive a create-only ladder until PostgreSQL's own
knee appears, project the mixed workload's DB load, and require
`capacity_gate.headroom_factor` (1.3) of margin. The mixed-workload knee the
projection assumes is a named module constant recorded as `projected_knee_rps`
with its arithmetic in `projected_knee_basis`, and the linear core-scaling
projection of PG's own capacity is `pg_capacity_projected_rps` with
`pg_capacity_projection_basis` — kept in separate fields from the MEASURED
`pg_knee_rps`, so no reader can mistake a scaled number for a measured one, and
so the assumption is visible in the artifact rather than buried in a multiplier.
If the margin is missing the gate fires the pre-registered levers in order —
write share 0.15→0.10, constructed cache hit rate 0.80→0.90, PG cores 3→4 taken
from vegeta — and never `synchronous_commit=off`.

**Stage 2, after the first cell.** A projection made before the ladder ran cannot
speak for the rates the cells actually chose. The re-gate is therefore a direct
measurement, not a second projection: PostgreSQL's cgroup `cpu.stat` delta over
the first cell's operating-point windows, divided by
`pg_cores × window wall seconds`, per slot; the **maximum** across slots must be
at most `1/headroom_factor`. It needs no ladder, costs no runtime, and is a
strictly stronger claim than a projection. It also fires **no levers**: a lever
fired after cell 1 would change the workload for cells 2..N while cell 1 already
ran the old one, so a failure here is a stop, not an adjustment.

The first cell cannot be un-run, so a failing re-gate **retroactively stamps** it:
the verdict, the realized rates by slot, the measured PostgreSQL utilization and
`cells_measured_before_regate` are recorded in the manifest and carried into the
report tables. A non-passing verdict at either stage aborts the remaining
measured cells unless the operator sets `LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE=1`,
which stamps the run `degraded` in the ledger and the manifest.

What a passing gate licenses: that PostgreSQL had headroom under the load the
cells actually placed on it. It is not an attribution of where a supra-knee
service's time goes — the DB-CPU-per-request column speaks to that, at the key it
supports. The comparison between the two stacks is internally valid either way:
both face the identical database under identical load, interleaved in time.

## Working set: the seed is not the workload

The full-scale dataset is 1M customers / 5M invoices. The **measured working set
is smaller**, and it is per probe rather than per run: every attack — each warmup
segment, the head-of-window discard and the measured window — draws its own
targets from its own PCG64 stream, sized at 1.02× the requests it will issue so
the stream never wraps inside its own window. `manifest.working_set` records the
spread across probes of the requests drawn, the distinct ids named and the
seconds spent drawing them.

This matters because vegeta reads a targets file from the top on every
invocation. One file shared by all three attacks would mean the warmup — run
immediately after a Redis `FLUSHALL` — did not merely leave a warm cache: it
populated it with exactly the invoice ids the measured window was about to
request. Independent streams retire that. What remains is the Zipf skew itself
(alpha 1.0), which is what the 0.80 construction target in `spec/workload.yaml`
is built on, and the realized hit rate is **measured** per window from Redis INFO
keyspace deltas rather than assumed. Both numbers are printed side by side in the
report.

The request **bodies** are shared: one pool per run, drawn from uniformly, so
both slots of a paired block send byte-identical payloads. That costs one
disclosure and the report states it: the create path's `customer_id` distribution
becomes the pool's empirical Zipf sample rather than a fresh Zipf draw per
request — identical on the head, thinner in the tail. The read path is URL-only
and unaffected.

One disclosed drift: create traffic grows the invoice table over a run, so the
database is not byte-identical between early and late probes. Interleaved
randomized blocks put the two slots adjacent in time, which keeps the headline
ratio drift-immune; absolute knee rates carry the caveat.

## Artifacts and verification

A run directory carries one checksum file, `CHECKSUMS.sha256`, and it verifies.
It is a **map, not a log**: recomputed from scratch at the end of the run and
again by `analyze`, sorted by POSIX relative path so it is byte-identical on
Linux and Windows, and written atomically. Its scope is
`git ls-files --cached --others --exclude-standard` inside the run directory —
tracked-plus-untracked-not-ignored by definition — so the checksum scope and the
publication scope are the same object, and the roll-ups (`ledger.json`,
`manifest.json`, `doctor.json`, the capacity-gate verdicts, `report/`) are
covered.

Per-probe atomicity is kept separately: each probe hashes its own artifacts into
its own `<slot>.probe.sha256`, so a resumed or re-run probe rewrites one file
instead of appending a second hash for the same path.

`uv run ledgerbench verify <run_id>` checks the map in **both** directions — a
tampered or missing file, and a published file the map does not list — and exits
non-zero naming the path.

## Not captured

Three analyses are not part of this method, and no run directory holds an
artifact for any of them:

- **Hysteresis**: a descending pass after the ascending ladder.
- **p999 order-statistic confidence intervals.** The per-second p999 series is
  recorded during warmup (see Warmup); no interval is computed from it.
- **Isolation runs.**

`PROGRESS.md` records the decisions: the descending pass and the p999 appendix
were cut on 2026-08-27, and isolation runs were left out of scope. The scope
section of each run's `report/report.md` points here for this statement.

## Spec errata

`spec/` is frozen and hashed into `config_hash`, so a sentence in it that turned
out wrong or stale is corrected here and not in the file (see
[`results/README.md`](../results/README.md)). Line numbers are those of the
published `spec/`.

- **`spec/slo.yaml:58`** says "G6 confirms whether it ever binds" about the 300 s
  warmup cap. G6 has run: the rehearsal flagged 0 of 13 Java and 1 of 15 .NET
  probes as under-warmed (its report's warmup census). In the full run the cap
  was reached with a gate still failing on 0 of 53 Java and 4 of 63 .NET probes:
  each flagged probe's `dotnet.warmup.json` records 10 segments of 30 s.
- **`spec/workload.yaml:2-3`** names "the vegeta target generator (bench/runner)"
  and "the seeder hot-set warm-touch" as consumers. The target generator is
  `bench/ledgerbench/vegeta.py`; there is no `bench/runner`, and `seed/` has no
  hot-set warm-touch step.
- **`spec/openapi.yaml:88-89`** describes `POST /invoices` as a "single batched
  round-trip per spec/db-access.md". That holds for the item inserts on .NET
  only: `spec/db-access.md`'s round-trip ledger gives EF Core 6 round trips per
  create and Hibernate 5 + N for N items.
- **`spec/db-access.md:50`** and **`spec/schema.sql:65-66`** say "P5 confirms it
  with EXPLAIN" about plan equivalence. P5 was folded into gate G4
  (`PROGRESS.md`), whose recorded evidence is a `log_statement=all` statement
  diff. No EXPLAIN output is published in this repository. What the tests pin is
  statement text (`StatementShapeTest`, `EfQueryShapeTests`).
- **`spec/db-access.md:62`** and **`:250-251`** refer to "P5's prepared-statement
  census" and to "P5's `pg_stat_statements` wire ledger", the second counted
  "under load, at the rates the headline is taken at". The G4 pass counted named
  prepared executions on a short fixed request sequence captured with
  `log_statement=all` (`PROGRESS.md`, G4 evidence). No `pg_stat_statements`
  ledger exists: the full run's `manifest.postgres_config` records
  `shared_preload_libraries` as empty, and its report states that
  `pg_stat_statements` deltas are not captured.
- **`spec/db-access.md:194-195`** says lock waits are "visible in pg_stat and
  reported". No lock-wait figure is in the published run's report or manifest.
- **`spec/db-access.md:217`** says "Like `variant.ado` and `variant.dapper`, that
  cell is `deferred=True`". Those two cells were removed (D5, see Run matrix);
  `variant.npgsql-default-prepare` is the only declared variant.
- **`spec/postgres/README.md:28`** cites "PLAN". No file of that name is in this
  repository; the rule it supports is in `spec/slo.yaml`:
  `capacity_gate.never: synchronous_commit_off`.
- **`spec/postgres/README.md:32-33`** says PGDATA gets `chattr +C` "and noatime.
  Applied once by infra/host-setup.sh". The script applies `chattr +C`; it
  detects and reports the `noatime` mount option and never applies it (see
  `docs/RUNBOOK.md`, Prerequisites).
