# Ledgerline analysis report - run `rehearsal-20260827T111408-fedora`

> **Measurement tier `rehearsal`: measurement-grade, REDUCED.** `ScaleProfile.functional_only` is False, so the per-probe achieved-vs-offered and error-rate exclusions applied, the PostgreSQL capacity gate was enforced, and the `docker_native_engine` doctor check was not waived. The dials that differ from `full` are `confirm_blocks` 2 vs full's 10, `ladder_max_rungs` 6 vs full's 40, `ladder_reps_per_rung` 2 vs full's 3, `ladder_step_factor` 2.0 vs full's 1.2, `soak_s` 300 vs full's 600 -- a dial that differs is a dial this tier does not validate, so read every interval, rung count and roll-up below as resting on that reduced budget.

## Knee fits (isotonic p99-vs-rate crossing 20 ms): ONE PER SLOT

Each of Java and .NET laddered until ITS OWN p99 crossed 20 ms, and each is fitted over its own rungs alone. There is no combined fit and no shared operating rate, because the headline metric is cost at a fixed LATENCY PROMISE and that rate is only defined per stack. A ladder that stops when a mixed-stack mean crosses measures the slower-degrading stack nowhere near its own knee -- and the rungs above that point are never offered at all, so no later analysis can recover them.

The crossing rule is ONE rule. `analysis.knee.crosses` -- the isotonic (PAVA) fit of the rung means -- is what the runner's ladder stops on and what this table reports, so where the ladder stopped and where the published fit crosses cannot be two different rates. A `scope` cell marked DISAGREES WITH THE LADDER would mean that invariant broke.

| cell | scope | knee rps @ 20ms | bootstrap interval | inferential? | rungs used | note |
| --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | Java | 6439 | [5605, >=8000] | NOT a CI (see note) | 5 | CI invalid: crossing bracket has 2 rep(s) < min_reps=10 (ladder stage) |
| headline.mixed.gc-default | .NET | 6874 | [6630, 7164] | NOT a CI (see note) | 5 | CI invalid: crossing bracket has 2 rep(s) < min_reps=10 (ladder stage) |


The bracketed interval is the 2.5/97.5 percentile of 2000 isotonic refits that resample the ladder's per-rung reps with replacement. It is NOT a valid bootstrap CI here: the SLO-crossing bracket carries as few as 2 measurement-grade rep(s), against the pre-registered minimum of `ladder.confirm_reps` = 10 (the `rehearsal` ladder runs 2 rep(s) per rung, and under-warmed exclusion thins the bracket further). Read it as a spread indicator over a handful of reps. An inferential CI needs the crossing bracket re-run at confirm-stage rep counts, which this run did not do. Resamples whose refit never crossed inside the ladder are counted as beyond the top rung (right-censored), and an upper bound printed as `>=` landed in that censored mass rather than resolving.


## The latency promise at the offered rate (p99 vs 20 ms SLO)

This is a CONFORMANCE readout, not a comparison. Each slot confirms at its own fitted knee, so both stacks sit at roughly the promise BY CONSTRUCTION and a head-to-head p99 verdict would be a guaranteed tie -- an artefact of the design. What a reader needs here is evidence the promise was met, per slot, at the rate that slot was measured at. (The head-to-head verdict is the paired COST verdict below.)

THE VERDICT RULE: the `meets 20 ms?` column compares the MEAN of the aggregated confirm/soak windows' p99 values against 20 ms. It is a DIFFERENT quantity from the ladder's crossing rule -- different stage, different windows, an arithmetic mean rather than an isotonic fit, and the opposite inequality at the boundary, so a slot sitting at exactly 20 ms has both `crossed` there and `meets` it. It is also lenient for a tail promise: a slot can be marked `yes` while individual windows miss badly. So the worst window and the count of windows whose OWN p99 was under 20 ms are printed in the same row: read those three columns together, never the verdict alone.

| cell | stage | offered rps | slot | mean p99 (ms) | worst window p99 (ms) | meets 20 ms? (mean rule) | windows under 20 ms | windows (used/total) | why dropped |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | confirm | 6439 | .NET | 21.25 | 26.68 | NO | 1/2 | 2/2 | - |
| headline.mixed.gc-default | confirm | 6439 | Java | 28.03 | 28.30 | NO | 0/2 | 2/2 | - |
| headline.mixed.gc-default | confirm | 6874 | .NET | 18.64 | 27.05 | yes | 1/2 | 2/2 | - |
| headline.mixed.gc-default | soak | 6439 | Java | 16.87 | 16.87 | yes | 1/1 | 1/1 | - |
| headline.mixed.gc-default | soak | 6874 | .NET | 31.73 | 31.73 | NO | 0/1 | 1/1 | - |


- **headline.mixed.gc-default** at 6439 rps offered: .NET mean p99 21.3 ms (MISSES the 20 ms promise on the mean rule; 1/2 windows individually under it, worst 26.7 ms); Java mean p99 28.0 ms (MISSES the 20 ms promise on the mean rule; 0/2 windows individually under it, worst 28.3 ms).


CONSISTENCY FLAGS -- a slot marked `yes` at a rate strictly ABOVE its own fitted knee. The two quantities are allowed to differ (an isotonic fit of 2-3-rep ladder rungs against a mean of confirm/soak windows), but a difference is information: either the knee is fitted low or the confirm windows were kinder than the ladder rungs at the same rate. Round 1 narrated this in prose; it is a flag, with both numbers.

- **headline.mixed.gc-default**: Java meets 20 ms at 6439 rps in `soak` (mean p99 16.87 ms), but its own fitted knee is 6439 rps.


## CPU-ms per request vs offered rate (is the cost rate-independent?)

A per-request cost quoted at one operating point is only a stack property if it does not move with rate. Whether it moves is READ OFF the per-slot coefficients of variation printed under the table(s) below, never asserted here: the largest is 19.5% (.NET, headline.mixed.gc-default, over the 7 rungs that slot has a value at), so at least one slot's per-request CPU is NOT constant across this run's rungs. Each row is a rate this cell measured windows at -- the ladder rungs PLUS each slot's confirm/soak operating point, so the rate the headline is read at is on its own curve; 'rung' in this section means a row of this table. A value is that row's TOTAL cgroup CPU delta divided by the TOTAL requests its windows serviced (the same request-weighted rule the roll-up below uses). `*` = no measurement-grade window at that rung for that slot, so the flagged windows were used (posture only).


**headline.mixed.gc-default**

| offered rps | Java CPU-ms/req | Java windows (used/total) | .NET CPU-ms/req | .NET windows (used/total) | Java : .NET |
| --- | --- | --- | --- | --- | --- |
| 500 | 0.2663 | 2/2 | 0.6694 | 2/2 | 0.40x |
| 1000 | 0.2697 | 2/2 | 0.6403 | 2/2 | 0.42x |
| 2000 | 0.2746 | 2/2 | 0.5687 | 1/2 | 0.48x |
| 4000 | 0.2342 | 2/2 | 0.5080 | 2/2 | 0.46x |
| 6439 | 0.2299 | 3/3 | 0.4462 | 2/2 | 0.52x |
| 6874 | n/a | 0/0 | 0.4350 | 3/3 | n/a |
| 8000 | 0.2324 | 2/2 | 0.4140 | 2/2 | 0.56x |


Coefficient of variation, per slot: Java 8.4% over 6 rungs, .NET 19.5% over 7 rungs. Java : .NET ratio spans 0.40x to 0.56x across the ladder.


The DB row is PostgreSQL's own cgroup CPU over the SAME windows, under the SAME request-weighted rule, and it is what separates *this runtime costs more CPU* from *this ORM's generated SQL costs the database more*. Attribution is clean without de-interleaving because a block's two probes run SEQUENTIALLY: while one slot is under load the other's container is stopped, so the PostgreSQL delta belongs to exactly one slot.

Three limits, all of them structural. It is a WHOLE-CONTAINER number, so it includes autovacuum, the checkpointer, the WAL writer and the background writer -- maintenance that is debt from earlier windows and is not proportional to this probe's requests. It is therefore NOT per-endpoint: only `pg_stat_statements` deltas can attribute cost to a statement, and this run does not do that. And it is driver-sensitive, and this run's slots do NOT share a driver: .NET uses Npgsql, Java uses PgJDBC + HikariCP. Npgsql resets a connection on every pooled CHECKOUT, which is DB-side work in PostgreSQL's cgroup (spec/db-access.md); PgJDBC + HikariCP sends a per-checkout aliveness probe only when the connection has been idle longer than HikariCP's 500 ms `aliveBypassWindowMs`, so at the rates this benchmark measures at it does not fire inside a measured window (spec/db-access.md). Part of the difference between the two DB columns is therefore the drivers' pooling behaviour rather than the languages'.


**App vs database CPU, per ladder rung.** One row per (cell, offered rate, slot). A rate is present only where that rung has a DB sample; `windows` is how many of that rung's windows carried one. `requests` and `db_requests` are the two columns' own DENOMINATORS -- the DB figure divides only the requests of the DB-sampled windows, so the two differ whenever `windows` is short of the rung's window count, and each CPU-ms/request column times its own denominator is that column's total CPU-ms.

| cell | offered_rps | slot | windows | requests | db_requests | app_cpu_ms_per_req | db_cpu_ms_per_req | total_cpu_ms_per_req | db_share_pct | source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | 500 | Java | 2 | 120000 | 120000 | 0.2663 | 0.1154 | 0.3816 | 30.2 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 500 | .NET | 2 | 120000 | 120000 | 0.6694 | 0.1335 | 0.803 | 16.6 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1000 | Java | 2 | 240000 | 240000 | 0.2697 | 0.1018 | 0.3716 | 27.4 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1000 | .NET | 2 | 240000 | 240000 | 0.6403 | 0.1233 | 0.7636 | 16.2 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 2000 | Java | 2 | 480000 | 480000 | 0.2746 | 0.1011 | 0.3756 | 26.9 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 2000 | .NET | 1 | 240000 | 240000 | 0.5687 | 0.1171 | 0.6858 | 17.1 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 4000 | Java | 2 | 960000 | 960000 | 0.2342 | 0.0996 | 0.3338 | 29.8 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 4000 | .NET | 2 | 959998 | 959998 | 0.508 | 0.1152 | 0.6231 | 18.5 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 6439 | Java | 3 | 3477075 | 3477075 | 0.2299 | 0.0981 | 0.328 | 29.9 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 6439 | .NET | 2 | 1545361 | 1545361 | 0.4462 | 0.1108 | 0.557 | 19.9 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 6874 | .NET | 3 | 3711972 | 3711972 | 0.435 | 0.1107 | 0.5457 | 20.3 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 8000 | Java | 2 | 1919996 | 1919996 | 0.2324 | 0.0988 | 0.3313 | 29.8 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 8000 | .NET | 2 | 1919999 | 1919999 | 0.414 | 0.1082 | 0.5222 | 20.7 | postgres cgroup cpu.stat delta over the measured window |


## Warmup census (under-warmed fraction per cell per slot)

Counted over every DONE probe of the `ladder`, `confirm`, `soak` stages, from the same per-probe evidence the fits excluded on (`gates_failed` plus `warmup_attack_failed` in `ledger.json`). PROGRESS.md's P8 abort rule is `> 25%` for either slot; the `over P8 rule?` column is that rule applied, not a judgement. `by gate` counts each failing gate separately, so it can exceed the flagged count when one probe failed several; `by reason` attributes exactly one cause per flagged probe and therefore sums to it.

| cell | slot | probes | flagged | flagged % | over P8 rule (>25%)? | by gate | by reason |
| --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | Java | 13 | 0 | 0.0 | no | - | - |
| headline.mixed.gc-default | .NET | 15 | 1 | 6.7 | no | gc_steady 1 | gates_failed 1 |


## Stationarity (per-second p99, Mann-Kendall + Theil-Sen band)

| cell | stationarity |
| --- | --- |
| headline.mixed.gc-default | all 22 series stationary |


## Paired Java : .NET COST verdict (confirm stage, RCB-paired by block)

The paired metric is COST, not p99. At their own knees both stacks sit at roughly 20 ms by construction, so a paired p99 verdict is a guaranteed tie; the p99 conformance readout above is where the latency promise is evidenced. Blocking still works exactly as before: `slot_order_for_block` randomises which member of a block runs first, so the two probes are adjacent in time in a randomised order, and that controls the nuisance factor REGARDLESS of the rate each slot runs at. What changed is the treatment definition -- no longer "language at a fixed rate" but "language at its own SLO-crossing rate", which is what the headline metric is defined as.

SCOPES ARE NEVER MERGED, and this run carries 2: `common_rate`, `own_knee`. `common_rate` adds the faster slot at the SLOWER slot's knee INSIDE the same blocks, because per-request cost is not rate-independent, so an own-knee cost comparison confounds language with position on the cost-vs-rate curve. `own_knee` is the headline the metric is defined as: each slot at its own crossing rate. Both are published because they answer different questions and choosing one for the reader is a choice they cannot audit; the cost-vs-rate curve above is the third view of the same fact.

The interval is the paired-t interval on the per-block differences at its stated confidence level; the equivalence margin is the separate, pre-registered +-band the interval is compared against. The verdict word is that equivalence decision (interval not contained in +-margin -> direction of the mean difference); an interval that ALSO spans 0% is not a significant difference at its confidence level, so read the interval, not only the word. `$/M` is the CPU column priced, not independent evidence, so its verdict is the CPU verdict in dollars.

| cell | scope | metric | blocks (n) | Java rps | .NET rps | Java | .NET | diff % | 95% CI (paired t) | equiv. margin | verdict | effect (d_z) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | own_knee | cpu_ms_per_req | 2 **BELOW the pre-registered 10** | 6439 | 6874 | 0.2322 | 0.4376 | -46.93% | [-54.85, -39.02] | +-5% | Java better | -53.28 |
| headline.mixed.gc-default | own_knee | anon_mb_per_req | 2 **BELOW the pre-registered 10** | 6439 | 6874 | 0.0008217 | 0.0001988 | +313.39% | [+204.56, +422.23] | +-5% | .NET better | 25.87 |
| headline.mixed.gc-default | own_knee | usd_per_million | 2 **BELOW the pre-registered 10** | 6439 | 6874 | 0.00258 | 0.004862 | -46.93% | [-54.85, -39.02] | +-5% | Java better | -53.28 |
| headline.mixed.gc-default | common_rate | cpu_ms_per_req | 2 **BELOW the pre-registered 10** | 6439 | 6439 | 0.2322 | 0.4462 | -47.96% | [-48.58, -47.33] | +-5% | Java better | -686.80 |
| headline.mixed.gc-default | common_rate | anon_mb_per_req | 2 **BELOW the pre-registered 10** | 6439 | 6439 | 0.0008217 | 0.0002187 | +275.70% | [+228.37, +323.04] | +-5% | .NET better | 52.33 |
| headline.mixed.gc-default | common_rate | usd_per_million | 2 **BELOW the pre-registered 10** | 6439 | 6439 | 0.00258 | 0.004958 | -47.96% | [-48.58, -47.33] | +-5% | Java better | -686.80 |


- **headline.mixed.gc-default** (own_knee): 2 paired block(s). This is BELOW the pre-registered minimum of 10 (spec/slo.yaml `ladder.confirm_reps`): read the verdict as directional only.

- **headline.mixed.gc-default** (common_rate): 2 paired block(s). This is BELOW the pre-registered minimum of 10 (spec/slo.yaml `ladder.confirm_reps`): read the verdict as directional only.

## Resource cost at the operating rate (cgroup v2 ground truth: SUT + PostgreSQL)

Steady-state SUT cgroup CPU + memory per serviced request at the offered rate(s) 6,439, 6,874 rps, aggregated over the confirm + soak probe windows. SCOPE: `own_knee` -- every window here was measured with its slot AT ITS OWN SLO-crossing rate (headline.mixed.gc-default: Java 6,439 rps, .NET 6,874 rps), read off the confirm/soak stage records. The confirm stage also runs the higher-knee slot at the COMMON rate for the confound-free contrast; those windows are NOT in this roll-up (they would make one slot's figure a blend of two operating points against the other's one) and are published as the `common_rate` paired verdict instead. Under-warmed / gate-excluded windows are EXCLUDED, the same exclusion the knee fit applies; the counts used vs excluded are a row of the table. `Java : .NET` > 1 means Java spends more. Per-request CPU MOVED across this run's ladder rungs (largest per-slot coefficient of variation 19.5%, .NET, over 7 rungs); see the CPU-vs-rate section. These are therefore values AT these rates.

THE AGGREGATION RULE: `CPU-ms / request` is REQUEST-WEIGHTED: the total cgroup CPU delta of the windows in the roll-up divided by the total requests they serviced, so the request count in the table really is its denominator. The memory rows are plain means over the same windows (a memory level is not a per-request quantity, so there is nothing to weight it by).

| cell | metric | Java | .NET | Java : .NET |
| --- | --- | --- | --- | --- |
| headline.mixed.gc-default | app CPU-ms / request (SUT cgroup) | 0.230 | 0.435 | 0.53x |
| headline.mixed.gc-default | DB CPU-ms / request (postgres cgroup) | 0.098 | 0.111 | 0.89x |
| headline.mixed.gc-default | app + DB CPU-ms / request | 0.328 | 0.546 | 0.60x |
| headline.mixed.gc-default | DB share of that total (%) | 29.9 | 20.3 | - |
| headline.mixed.gc-default | windows carrying a DB sample | 3/3 | 3/3 | - |
| headline.mixed.gc-default | anon memory p99 (MB, in-window) | 646 | 169 | 3.82x |
| headline.mixed.gc-default | cgroup memory.peak (MB, container lifetime) | 2240 | 1939 | 1.16x |
| headline.mixed.gc-default | windows used / flagged-excluded | 3 / 0 | 3 / 0 | - |
| headline.mixed.gc-default | stage mix of the windows used | 2 confirm, 1 soak | 2 confirm, 1 soak | - |
| headline.mixed.gc-default | requests serviced (the CPU denominator) | 3477075 | 3711972 | - |


`anon memory p99` is the p99 of `memory.stat anon` sampled inside the measurement windows. `cgroup memory.peak` is the kernel's high-water mark of `memory.current` over the CONTAINER'S WHOLE LIFETIME: anonymous memory plus page cache plus kernel memory, including warmup, so it is not an in-window RSS figure and is not comparable to the anon p99 above it.


Pool warmth at the end of warmup (open connections / configured max): headline.mixed.gc-default: Java 24/24, .NET 24/24.


Resource-accounting validity, over the 6 aggregated confirm + soak windows in the table above (NOT over the ladder windows, which are not aggregated here): CPU throttle counters zero; cgroup OOM events zero; `memory.swap.current` (SUT and PostgreSQL cgroups) zero at every window edge; `memory.swap.peak` (SUT and PostgreSQL cgroups) zero in the kernel's lifetime high-water mark. A throttled window would deflate CPU-ms/request; an OOM window would invalidate the memory figures; a window that swapped would UNDERSTATE them, because a swapped-out anon page leaves `memory.stat anon` -- and a swapping database would move the DB CPU-ms/request column, which is why the swap verdicts span PostgreSQL's window as well as the SUT's. Each sampled cgroup's own ceilings, read at window start, and its swap outcome maximised over ALL the run's windows, recorded in `manifest.topology.containers`: `containerd` memory.max=unread, memory.swap.max=max, max memory.swap.current=0 B, max memory.swap.peak=0 B; `dockerd` memory.max=unread, memory.swap.max=max, max memory.swap.current=0 B, max memory.swap.peak=0 B; `dotnet` memory.max=12.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B; `java` memory.max=12.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B; `postgres` memory.max=8.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B; `redis` memory.max=2.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B; `vegeta` memory.max=4.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B. `memory.swap.max` is NOT 0 for `containerd`, `dockerd`: those containers had a swap allowance to spend, so the swap verdict above is all that stands between the memory figures and swapped-out anon pages. `memory.swap.current` read 0 at every window edge for `containerd`, `dockerd`, `dotnet`, `java`, `postgres`, `redis`, `vegeta`; `memory.swap.peak`, the kernel's own lifetime high-water mark, read 0 for `containerd`, `dockerd`, `dotnet`, `java`, `postgres`, `redis`, `vegeta`. That covers `containerd`, `dockerd`, `redis`, `vegeta`, which the per-window verdict above does not.


The two quantities the CPU rows divide: total cgroup CPU over the aggregated windows, and the requests those windows serviced: headline.mixed.gc-default: Java 3477075 requests (0 non-2xx) over 799533 CPU-ms, .NET 3711972 requests (0 non-2xx) over 1614606 CPU-ms. Dividing the second into the first reproduces the `CPU-ms / request` row exactly; that is the aggregation rule stated above.


## GC and pool activity (`/runtime-stats`, flushed per window)

Summed over the 28 `phase: measured` window summaries on disk that carry a non-empty delta block, across EVERY stage and rate -- ladder, confirm and soak alike. **This is not the roll-up's window set.** A `/runtime-stats` summary records no warmup verdict, so under-warmed and gate-excluded windows cannot be dropped here and are not; read these as run-total activity, never as a gated per-request figure. Every counter is a process-lifetime total differenced across that window's OWN edges, so summing them adds per-window increments and never adds a lifetime total twice. `(Nw)` after a per-generation count is how many of those windows reported that generation at all: `generations summed` is the writer's own list of which ones `collections total` covers, and a collector that does not expose a generation reports `not reported` rather than 0. `collections total` and `GC pause total` carry the same `(Nw)` and the same `not reported`: the JVM publishes a present null for every generation (so the summed total is null too) and for `pause_total_seconds` under ZGC, and a null summed as zero would publish a collector that never collected and never paused.

| slot | windows | generations summed | collections gen0 | collections gen1 | collections gen2 | collections total | GC pause total (s) | allocated (MB) | heap committed max (MB) | db pool max open / configured |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Java | 13 | none named | not reported | not reported | not reported | not reported | 4.656 (13w) | 617,060 (13w) | 298 | 24/24 |
| .NET | 15 | gen0, gen1, gen2 | 40,634 (15w) | 3,670 (15w) | 132 (15w) | 44,436 (15w) | 34.654 (15w) | 634,597 (15w) | 144 | 24/24 |


## Illustrative $/M-request cost

**Illustrative, and the price is an assumption.** $0.0400 per vCPU-hour is a constant chosen by this harness (from `analyze._VCPU_HOUR_USD_DEFAULT`), not a vendor quote and not a measurement. The figure is linear in it, so a reader with a different price rescales by the ratio of the two; the SLOT-TO-SLOT ratio does not depend on the price at all. The only measured input is the request-weighted `CPU-ms / request` from the roll-up above, which means every caveat on that number -- the windows it excludes, the cgroup it is taken from, the apparatus CPU it leaves out -- is a caveat on this one. Per-request CPU moved across this run's ladder rungs (largest per-slot coefficient of variation 19.5%), so these dollars belong to the operating rate(s) the roll-up used and to no other.


**headline.mixed.gc-default** at 6439, 6874 rps offered

| slot | $/M (mid) | +-20% band |
| --- | --- | --- |
| Java | $0.0026 | [$0.0020, $0.0031] |
| .NET | $0.0048 | [$0.0039, $0.0058] |

*Illustrative only.* `usd_per_million = cpu_ms_per_req * vcpu_hour_usd / 3.6`; pinned vCPU-hour = $0.0400; sensitivity band +-20%.


## Not captured in this run (scope)

- **The capacity gate was re-run at the REALIZED rates, and passed.** PostgreSQL ran at 0.252 of its 3-core budget against a 0.769 ceiling over the operating-point windows of headline.mixed.gc-default (.NET 6,874 rps, Java 6,439 rps). This is a MEASUREMENT of PostgreSQL under the actual cells, not a second projection: the pre-run gate rests on a PROJECTED knee, and round 1's did so while its cells then ran several times higher.

- **Effective working set and cache residency.** Targets are DRAWN PER ATTACK (`working_set.draw_mode` = `per_probe_stream`), Zipf-hot from 5,000,000 seeded invoices / 1,000,000 seeded customers, from a shared body pool of 150,000 create bodies sized from the mix, counted off the pool on disk to hold 52,097 distinct customer ids, and 200,000 quote bodies. Bodies carry no URL, so one pool serves both slots and both halves of an RCB block send byte-identical payloads. It costs one disclosure: a create body is drawn UNIFORMLY from that pool, so the create path's customer_id distribution is the pool's empirical Zipf sample rather than a fresh Zipf draw per request -- identical on the head, thinner in the tail. The read path is URL-only and unaffected. Each of the 28 measured windows drew its OWN stream of 61,200-2,103,444 (median 489,600) requests, naming 12,355-251,880 (median 75,454) distinct invoice ids (1.51% of the seeded invoices at the median) and 11,431-161,543 (median 60,245) distinct customer ids. Those are PER-PROBE counts and this run publishes no union across probes: two independent streams still overlap on the Zipf head, so the counts may not be summed, and a union accumulated in the finalizing process would come out short on exactly the runs that resumed. A measured window is 300 s against a 300 s cache TTL, so an entry populated at the head of a window can expire before that window ends: within-window residency is WEAKER than the Zipf skew alone implies, and weaker than the constructed target hit rate of 0.80 implies. The warmup, the head-of-window discard and the measured attack each draw a separate stream, so no attack replays the SEQUENCE another is about to consume -- which is what R1-A7 was, the warmup consuming the measured window's own prefix after a FLUSHALL. It does NOT mean a window opens cold: the streams are independent draws from the same Zipf keyspace, so the hot head appears in both by construction, and that residual is measured rather than asserted away. Measured over 28 windows, 57.3-65.1% (median 61.3%) of a window's invoice-id READ REQUESTS name an id an earlier attack of the same probe had already drawn, against a constructed target hit rate of 0.80. That is the read path's residency floor and it is a property of the workload, not of the harness. The REALIZED Redis hit rate this run was 0.654 (manifest `cache_stats`, INFO keyspace deltas over measured windows).

- **DB CPU is per CONTAINER, not per endpoint or per statement.** The PostgreSQL column is a whole-cgroup `cpu.stat` delta, so it includes autovacuum, the checkpointer and the WAL writer -- maintenance that is debt from earlier windows and is not proportional to this probe's requests. Attributing cost to a statement needs `pg_stat_statements` deltas, which this run does not capture.

- **Container placement matches the resource ledger.** 5 sampled cgroup(s) read back the `cpuset.cpus.effective` infra/compose.bench.yaml asked for. containerd, dockerd declare no compose cpuset (pinned by the systemd drop-in instead).

- **Load-generator and container-runtime CPU is measured and EXCLUDED.** `containerd`, `dockerd`, `vegeta` are sampled (manifest `load_generator_overhead`) so the single-host accounting is auditable, but their CPU is apparatus and never enters a per-request figure. The CO-safety of the load rests on the per-probe achieved==offered evidence (manifest `achieved_vs_offered`).

- **/runtime-stats polling is inside the measured window, and its cost has no denominator.** This run flushed 56 window summaries (`<slot>[.warmup].runtime_stats.summary.json`): 28 measured, 28 warmup. 56 of them carry both window edges and 56 carry a non-empty end-minus-start delta block (a window with a single successful poll has edges but no delta, since a delta needs two); 56 per-poll `.jsonl` traces sit beside them (a window with no successful poll writes none). Every one of them records at least one successful poll. The polls' SUT-side CPU lands in the cgroup delta, while `/runtime-stats` is not one of the frozen `endpoint_calls` routes and is not in vegeta's request count either -- so per-request CPU is overstated by that much. 56 of them carry a MEASURED upper bound on that cost (`poll_cost.round_trip_ms_total`, the client-side round trip), totalling 9,974 ms across the run. The alternative, polling only at the boundaries, would lose the within-window heap trace that decides the heap-policy question.

- **Hysteresis, p999 order-statistic CIs, and isolation runs.** Not captured in this build (see docs/METHODOLOGY.md).

