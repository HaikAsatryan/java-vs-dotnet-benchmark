# Ledgerline analysis report - run `full-20260827T174611-fedora`

> **Measurement tier `full`: measurement-grade.** `ScaleProfile.functional_only` is False and every dial in `analyze._TIER_DIALS` matches the `full` profile, so nothing in this report is reduced relative to a full run.

## Knee fits (isotonic p99-vs-rate crossing 20 ms): ONE PER SLOT

Each of Java and .NET laddered until ITS OWN p99 crossed 20 ms, and each is fitted over its own rungs alone. There is no combined fit and no shared operating rate, because the headline metric is cost at a fixed LATENCY PROMISE and that rate is only defined per stack. A ladder that stops when a mixed-stack mean crosses measures the slower-degrading stack nowhere near its own knee -- and the rungs above that point are never offered at all, so no later analysis can recover them.

The crossing rule is ONE rule. `analysis.knee.crosses` -- the isotonic (PAVA) fit of the rung means -- is what the runner's ladder stops on and what this table reports, so where the ladder stopped and where the published fit crosses cannot be two different rates. A `scope` cell marked DISAGREES WITH THE LADDER would mean that invariant broke.

| cell | scope | knee rps @ 20ms | bootstrap interval | inferential? | rungs used | note |
| --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | Java | 4799 | [4331, >=5350] | NOT a CI (see note) | 14 | CI invalid: crossing bracket has 3 rep(s) < min_reps=10 |
| headline.mixed.gc-default | .NET | 4833 | [4674, 5000] | NOT a CI (see note) | 14 | CI invalid: crossing bracket has 2 rep(s) < min_reps=10 |


The bracketed interval is the 2.5/97.5 percentile of 2000 isotonic refits that resample the ladder's per-rung reps with replacement. It is NOT a valid bootstrap CI here: the SLO-crossing bracket carries as few as 2 measurement-grade rep(s), against the pre-registered minimum of `ladder.confirm_reps` = 10 (the `full` ladder runs 3 rep(s) per rung, and under-warmed exclusion thins the bracket further). Read it as a spread indicator over a handful of reps. An inferential CI needs the crossing bracket re-run at confirm-stage rep counts, which this run did not do. Resamples whose refit never crossed inside the ladder are counted as beyond the top rung (right-censored), and an upper bound printed as `>=` landed in that censored mass rather than resolving.


## The latency promise at the offered rate (p99 vs 20 ms SLO)

This is a CONFORMANCE readout, not a comparison. Each slot confirms at its own fitted knee, so both stacks sit at roughly the promise BY CONSTRUCTION and a head-to-head p99 verdict would be a guaranteed tie -- an artefact of the design. What a reader needs here is evidence the promise was met, per slot, at the rate that slot was measured at. (The head-to-head verdict is the paired COST verdict below.)

THE VERDICT RULE: the `meets 20 ms?` column compares the MEAN of the aggregated confirm/soak windows' p99 values against 20 ms. It is a DIFFERENT quantity from the ladder's crossing rule -- different stage, different windows, an arithmetic mean rather than an isotonic fit, and the opposite inequality at the boundary, so a slot sitting at exactly 20 ms has both `crossed` there and `meets` it. It is also lenient for a tail promise: a slot can be marked `yes` while individual windows miss badly. So the worst window and the count of windows whose OWN p99 was under 20 ms are printed in the same row: read those three columns together, never the verdict alone.

| cell | stage | offered rps | slot | mean p99 (ms) | worst window p99 (ms) | meets 20 ms? (mean rule) | windows under 20 ms | windows (used/total) | why dropped |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | confirm | 4799 | .NET | 26.62 | 32.81 | NO | 0/9 | 9/10 | 1 under-warmed |
| headline.mixed.gc-default | confirm | 4799 | Java | 27.54 | 39.22 | NO | 3/10 | 10/10 | - |
| headline.mixed.gc-default | confirm | 4833 | .NET | 28.86 | 33.66 | NO | 0/9 | 9/10 | 1 under-warmed |
| headline.mixed.gc-default | soak | 4799 | Java | 21.08 | 21.08 | NO | 0/1 | 1/1 | - |
| headline.mixed.gc-default | soak | 4833 | .NET | 45.02 | 45.02 | NO | 0/1 | 1/1 | - |


- **headline.mixed.gc-default** at 4799 rps offered: .NET mean p99 26.6 ms (MISSES the 20 ms promise on the mean rule; 0/9 windows individually under it, worst 32.8 ms); Java mean p99 27.5 ms (MISSES the 20 ms promise on the mean rule; 3/10 windows individually under it, worst 39.2 ms).


## CPU-ms per request vs offered rate (is the cost rate-independent?)

A per-request cost quoted at one operating point is only a stack property if it does not move with rate. Whether it moves is READ OFF the per-slot coefficients of variation printed under the table(s) below, never asserted here: the largest is 12.5% (.NET, headline.mixed.gc-default, over the 16 rungs that slot has a value at), so at least one slot's per-request CPU is NOT constant across this run's rungs. Each row is a rate this cell measured windows at -- the ladder rungs PLUS each slot's confirm/soak operating point, so the rate the headline is read at is on its own curve; 'rung' in this section means a row of this table. A value is that row's TOTAL cgroup CPU delta divided by the TOTAL requests its windows serviced (the same request-weighted rule the roll-up below uses). `*` = no measurement-grade window at that rung for that slot, so the flagged windows were used (posture only).


**headline.mixed.gc-default**

| offered rps | Java CPU-ms/req | Java windows (used/total) | .NET CPU-ms/req | .NET windows (used/total) | Java : .NET |
| --- | --- | --- | --- | --- | --- |
| 500 | 0.2242 | 3/3 | 0.6682 | 3/3 | 0.34x |
| 600 | 0.2399 | 3/3 | 0.6662 | 3/3 | 0.36x |
| 720 | 0.2619 | 3/3 | 0.6634 | 2/3 | 0.39x |
| 864 | 0.2657 | 3/3 | 0.6573 | 3/3 | 0.40x |
| 1037 | 0.2705 | 3/3 | 0.6371 | 3/3 | 0.42x |
| 1244 | 0.2795 | 3/3 | 0.6184 | 3/3 | 0.45x |
| 1493 | 0.2723 | 3/3 | 0.5872 | 3/3 | 0.46x |
| 1792 | 0.2768 | 3/3 | 0.5803 | 3/3 | 0.48x |
| 2150 | 0.2651 | 3/3 | 0.5601 | 3/3 | 0.47x |
| 2580 | 0.2567 | 3/3 | 0.5487 | 3/3 | 0.47x |
| 3096 | 0.2454 | 3/3 | 0.5365 | 3/3 | 0.46x |
| 3715 | 0.2487 | 3/3 | 0.5202 | 3/3 | 0.48x |
| 4458 | 0.2360 | 3/3 | 0.4953 | 2/3 | 0.48x |
| 4799 | 0.2334 | 11/11 | 0.4837 | 9/10 | 0.48x |
| 4833 | n/a | 0/0 | 0.4810 | 10/11 | n/a |
| 5350 | 0.2328 | 3/3 | 0.4701 | 3/3 | 0.50x |


Coefficient of variation, per slot: Java 7.1% over 15 rungs, .NET 12.5% over 16 rungs. Java : .NET ratio spans 0.34x to 0.50x across the ladder.


The DB row is PostgreSQL's own cgroup CPU over the SAME windows, under the SAME request-weighted rule, and it is what separates *this runtime costs more CPU* from *this ORM's generated SQL costs the database more*. Attribution is clean without de-interleaving because a block's two probes run SEQUENTIALLY: while one slot is under load the other's container is stopped, so the PostgreSQL delta belongs to exactly one slot.

Three limits, all of them structural. It is a WHOLE-CONTAINER number, so it includes autovacuum, the checkpointer, the WAL writer and the background writer -- maintenance that is debt from earlier windows and is not proportional to this probe's requests. It is therefore NOT per-endpoint: only `pg_stat_statements` deltas can attribute cost to a statement, and this run does not do that. And it is driver-sensitive, and this run's slots do NOT share a driver: .NET uses Npgsql, Java uses PgJDBC + HikariCP. Npgsql resets a connection on every pooled CHECKOUT, which is DB-side work in PostgreSQL's cgroup (spec/db-access.md); PgJDBC + HikariCP sends a per-checkout aliveness probe only when the connection has been idle longer than HikariCP's 500 ms `aliveBypassWindowMs`, so at the rates this benchmark measures at it does not fire inside a measured window (spec/db-access.md). Part of the difference between the two DB columns is therefore the drivers' pooling behaviour rather than the languages'.


**App vs database CPU, per ladder rung.** One row per (cell, offered rate, slot). A rate is present only where that rung has a DB sample; `windows` is how many of that rung's windows carried one. `requests` and `db_requests` are the two columns' own DENOMINATORS -- the DB figure divides only the requests of the DB-sampled windows, so the two differ whenever `windows` is short of the rung's window count, and each CPU-ms/request column times its own denominator is that column's total CPU-ms.

| cell | offered_rps | slot | windows | requests | db_requests | app_cpu_ms_per_req | db_cpu_ms_per_req | total_cpu_ms_per_req | db_share_pct | source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | 500 | Java | 3 | 180000 | 180000 | 0.2242 | 0.1126 | 0.3368 | 33.4 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 500 | .NET | 3 | 180000 | 180000 | 0.6682 | 0.1329 | 0.8011 | 16.6 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 600 | Java | 3 | 216000 | 216000 | 0.2399 | 0.1074 | 0.3473 | 30.9 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 600 | .NET | 3 | 216000 | 216000 | 0.6662 | 0.1331 | 0.7994 | 16.7 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 720 | Java | 3 | 259201 | 259201 | 0.2619 | 0.104 | 0.3659 | 28.4 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 720 | .NET | 2 | 172800 | 172800 | 0.6634 | 0.1288 | 0.7921 | 16.3 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 864 | Java | 3 | 311041 | 311041 | 0.2657 | 0.1061 | 0.3718 | 28.5 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 864 | .NET | 3 | 311040 | 311040 | 0.6573 | 0.1263 | 0.7837 | 16.1 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1037 | Java | 3 | 373321 | 373321 | 0.2705 | 0.1046 | 0.3752 | 27.9 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1037 | .NET | 3 | 373321 | 373321 | 0.6371 | 0.1234 | 0.7605 | 16.2 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1244 | Java | 3 | 447839 | 447839 | 0.2795 | 0.1032 | 0.3827 | 27.0 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1244 | .NET | 3 | 447840 | 447840 | 0.6184 | 0.1224 | 0.7408 | 16.5 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1493 | Java | 3 | 537479 | 537479 | 0.2723 | 0.1017 | 0.374 | 27.2 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1493 | .NET | 3 | 537479 | 537479 | 0.5872 | 0.1197 | 0.7069 | 16.9 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1792 | Java | 3 | 645121 | 645121 | 0.2768 | 0.1009 | 0.3778 | 26.7 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 1792 | .NET | 3 | 645121 | 645121 | 0.5803 | 0.1191 | 0.6994 | 17.0 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 2150 | Java | 3 | 773999 | 773999 | 0.2651 | 0.1004 | 0.3656 | 27.5 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 2150 | .NET | 3 | 774003 | 774003 | 0.5601 | 0.119 | 0.6792 | 17.5 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 2580 | Java | 3 | 928801 | 928801 | 0.2567 | 0.0992 | 0.3559 | 27.9 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 2580 | .NET | 3 | 928803 | 928803 | 0.5487 | 0.1211 | 0.6698 | 18.1 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 3096 | Java | 3 | 1114563 | 1114563 | 0.2454 | 0.1002 | 0.3456 | 29.0 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 3096 | .NET | 3 | 1114561 | 1114561 | 0.5365 | 0.1154 | 0.652 | 17.7 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 3715 | Java | 3 | 1337398 | 1337398 | 0.2487 | 0.0992 | 0.3479 | 28.5 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 3715 | .NET | 3 | 1337397 | 1337397 | 0.5202 | 0.1168 | 0.637 | 18.3 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 4458 | Java | 3 | 1604882 | 1604882 | 0.236 | 0.1001 | 0.3361 | 29.8 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 4458 | .NET | 2 | 1069921 | 1069921 | 0.4953 | 0.1144 | 0.6096 | 18.8 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 4799 | Java | 11 | 8636642 | 8636642 | 0.2334 | 0.1009 | 0.3344 | 30.2 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 4799 | .NET | 9 | 5181774 | 5181774 | 0.4837 | 0.1151 | 0.5988 | 19.2 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 4833 | .NET | 10 | 8119468 | 8119468 | 0.481 | 0.1152 | 0.5962 | 19.3 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 5350 | Java | 3 | 1924467 | 1924467 | 0.2328 | 0.1 | 0.3328 | 30.1 | postgres cgroup cpu.stat delta over the measured window |
| headline.mixed.gc-default | 5350 | .NET | 3 | 1925855 | 1925855 | 0.4701 | 0.1131 | 0.5832 | 19.4 | postgres cgroup cpu.stat delta over the measured window |


## Warmup census (under-warmed fraction per cell per slot)

Counted over every DONE probe of the `ladder`, `confirm`, `soak` stages, from the same per-probe evidence the fits excluded on (`gates_failed` plus `warmup_attack_failed` in `ledger.json`). PROGRESS.md's P8 abort rule is `> 25%` for either slot; the `over P8 rule?` column is that rule applied, not a judgement. `by gate` counts each failing gate separately, so it can exceed the flagged count when one probe failed several; `by reason` attributes exactly one cause per flagged probe and therefore sums to it.

| cell | slot | probes | flagged | flagged % | over P8 rule (>25%)? | by gate | by reason |
| --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | Java | 53 | 0 | 0.0 | no | - | - |
| headline.mixed.gc-default | .NET | 63 | 4 | 6.3 | no | gc_steady 2, p99_flat 3 | gates_failed 4 |


## Stationarity (per-second p99, Mann-Kendall + Theil-Sen band)

| cell | stationarity |
| --- | --- |
| headline.mixed.gc-default | all 86 series stationary |


## Paired Java : .NET COST verdict (confirm stage, RCB-paired by block)

The paired metric is COST, not p99. At their own knees both stacks sit at roughly 20 ms by construction, so a paired p99 verdict is a guaranteed tie; the p99 conformance readout above is where the latency promise is evidenced. Blocking still works exactly as before: `slot_order_for_block` randomises which member of a block runs first, so the two probes are adjacent in time in a randomised order, and that controls the nuisance factor REGARDLESS of the rate each slot runs at. What changed is the treatment definition -- no longer "language at a fixed rate" but "language at its own SLO-crossing rate", which is what the headline metric is defined as.

SCOPES ARE NEVER MERGED, and this run carries 2: `common_rate`, `own_knee`. `common_rate` adds the faster slot at the SLOWER slot's knee INSIDE the same blocks, because per-request cost is not rate-independent, so an own-knee cost comparison confounds language with position on the cost-vs-rate curve. `own_knee` is the headline the metric is defined as: each slot at its own crossing rate. Both are published because they answer different questions and choosing one for the reader is a choice they cannot audit; the cost-vs-rate curve above is the third view of the same fact.

The interval is the paired-t interval on the per-block differences at its stated confidence level; the equivalence margin is the separate, pre-registered +-band the interval is compared against. The verdict word is that equivalence decision (interval not contained in +-margin -> direction of the mean difference); an interval that ALSO spans 0% is not a significant difference at its confidence level, so read the interval, not only the word. `$/M` is the CPU column priced, not independent evidence, so its verdict is the CPU verdict in dollars.

| cell | scope | metric | blocks (n) | Java rps | .NET rps | Java | .NET | diff % | 95% CI (paired t) | equiv. margin | verdict | effect (d_z) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| headline.mixed.gc-default | own_knee | cpu_ms_per_req | 9 **BELOW the pre-registered 10** | 4799 | 4833 | 0.2344 | 0.484 | -51.56% | [-51.83, -51.29] | +-5% | Java better | -147.04 |
| headline.mixed.gc-default | own_knee | anon_mb_per_req | 9 **BELOW the pre-registered 10** | 4799 | 4833 | 0.001141 | 0.0003032 | +276.22% | [+272.65, +279.80] | +-5% | .NET better | 59.34 |
| headline.mixed.gc-default | own_knee | usd_per_million | 9 **BELOW the pre-registered 10** | 4799 | 4833 | 0.002605 | 0.005377 | -51.56% | [-51.83, -51.29] | +-5% | Java better | -147.04 |
| headline.mixed.gc-default | common_rate | cpu_ms_per_req | 9 **BELOW the pre-registered 10** | 4799 | 4799 | 0.2348 | 0.4837 | -51.45% | [-51.86, -51.03] | +-5% | Java better | -95.00 |
| headline.mixed.gc-default | common_rate | anon_mb_per_req | 9 **BELOW the pre-registered 10** | 4799 | 4799 | 0.00114 | 0.0002988 | +281.63% | [+275.42, +287.84] | +-5% | .NET better | 34.84 |
| headline.mixed.gc-default | common_rate | usd_per_million | 9 **BELOW the pre-registered 10** | 4799 | 4799 | 0.002609 | 0.005374 | -51.45% | [-51.86, -51.03] | +-5% | Java better | -95.00 |


- **headline.mixed.gc-default** (own_knee): 9 paired block(s), 1 dropped for a missing or flagged probe. This is BELOW the pre-registered minimum of 10 (spec/slo.yaml `ladder.confirm_reps`): read the verdict as directional only.

- **headline.mixed.gc-default** (common_rate): 9 paired block(s), 1 dropped for a missing or flagged probe. This is BELOW the pre-registered minimum of 10 (spec/slo.yaml `ladder.confirm_reps`): read the verdict as directional only.

## Resource cost at the operating rate (cgroup v2 ground truth: SUT + PostgreSQL)

Steady-state SUT cgroup CPU + memory per serviced request at the offered rate(s) 4,799, 4,833 rps, aggregated over the confirm + soak probe windows. SCOPE: `own_knee` -- every window here was measured with its slot AT ITS OWN SLO-crossing rate (headline.mixed.gc-default: Java 4,799 rps, .NET 4,833 rps), read off the confirm/soak stage records. The confirm stage also runs the higher-knee slot at the COMMON rate for the confound-free contrast; those windows are NOT in this roll-up (they would make one slot's figure a blend of two operating points against the other's one) and are published as the `common_rate` paired verdict instead. Under-warmed / gate-excluded windows are EXCLUDED, the same exclusion the knee fit applies; the counts used vs excluded are a row of the table. `Java : .NET` > 1 means Java spends more. Per-request CPU MOVED across this run's ladder rungs (largest per-slot coefficient of variation 12.5%, .NET, over 16 rungs); see the CPU-vs-rate section. These are therefore values AT these rates.

THE AGGREGATION RULE: `CPU-ms / request` is REQUEST-WEIGHTED: the total cgroup CPU delta of the windows in the roll-up divided by the total requests they serviced, so the request count in the table really is its denominator. The memory rows are plain means over the same windows (a memory level is not a per-request quantity, so there is nothing to weight it by).


**Stage-mix asymmetry in headline.mixed.gc-default.** The windows behind the two CPU figures do not have the same stage composition: Java 10 confirm, 1 soak, .NET 9 confirm, 1 soak. Windows flagged under-warmed / gate-excluded and dropped from the roll-up: Java 0, .NET 1 -- which is what the two sets differ by. A soak window is the run's longest probe and services several times a confirm window's requests, so the two figures aggregate different stage mixes. Measured effect on the published ratio: the equal-weight-per-window alternative gives 0.485x against the published request-weighted 0.485x, a 0.1% difference, inside the +-5% equivalence margin this cell's verdict is decided against, so the weighting choice does not carry this cell's headline. It is still the reader's to check, not ours to leave implicit.

| cell | metric | Java | .NET | Java : .NET |
| --- | --- | --- | --- | --- |
| headline.mixed.gc-default | app CPU-ms / request (SUT cgroup) | 0.233 | 0.481 | 0.49x |
| headline.mixed.gc-default | DB CPU-ms / request (postgres cgroup) | 0.101 | 0.115 | 0.88x |
| headline.mixed.gc-default | app + DB CPU-ms / request | 0.334 | 0.596 | 0.56x |
| headline.mixed.gc-default | DB share of that total (%) | 30.2 | 19.3 | - |
| headline.mixed.gc-default | windows carrying a DB sample | 11/11 | 10/10 | - |
| headline.mixed.gc-default | anon memory p99 (MB, in-window) | 656 | 176 | 3.72x |
| headline.mixed.gc-default | cgroup memory.peak (MB, container lifetime) | 1645 | 1202 | 1.37x |
| headline.mixed.gc-default | windows used / flagged-excluded | 11 / 0 | 10 / 1 | - |
| headline.mixed.gc-default | stage mix of the windows used | 10 confirm, 1 soak | 9 confirm, 1 soak | - |
| headline.mixed.gc-default | requests serviced (the CPU denominator) | 8636642 | 8119468 | - |


`anon memory p99` is the p99 of `memory.stat anon` sampled inside the measurement windows. `cgroup memory.peak` is the kernel's high-water mark of `memory.current` over the CONTAINER'S WHOLE LIFETIME: anonymous memory plus page cache plus kernel memory, including warmup, so it is not an in-window RSS figure and is not comparable to the anon p99 above it.


Pool warmth at the end of warmup (open connections / configured max): headline.mixed.gc-default: Java 24/24, .NET 24/24.


Resource-accounting validity, over the 21 aggregated confirm + soak windows in the table above (NOT over the ladder windows, which are not aggregated here): CPU throttle counters zero; cgroup OOM events zero; `memory.swap.current` (SUT and PostgreSQL cgroups) zero at every window edge; `memory.swap.peak` (SUT and PostgreSQL cgroups) zero in the kernel's lifetime high-water mark. A throttled window would deflate CPU-ms/request; an OOM window would invalidate the memory figures; a window that swapped would UNDERSTATE them, because a swapped-out anon page leaves `memory.stat anon` -- and a swapping database would move the DB CPU-ms/request column, which is why the swap verdicts span PostgreSQL's window as well as the SUT's. Each sampled cgroup's own ceilings, read at window start, and its swap outcome maximised over ALL the run's windows, recorded in `manifest.topology.containers`: `containerd` memory.max=unread, memory.swap.max=max, max memory.swap.current=0 B, max memory.swap.peak=0 B; `dockerd` memory.max=unread, memory.swap.max=max, max memory.swap.current=0 B, max memory.swap.peak=0 B; `dotnet` memory.max=12.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B; `java` memory.max=12.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B; `postgres` memory.max=8.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B; `redis` memory.max=2.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B; `vegeta` memory.max=4.00 GiB, memory.swap.max=0, max memory.swap.current=0 B, max memory.swap.peak=0 B. `memory.swap.max` is NOT 0 for `containerd`, `dockerd`: those containers had a swap allowance to spend, so the swap verdict above is all that stands between the memory figures and swapped-out anon pages. `memory.swap.current` read 0 at every window edge for `containerd`, `dockerd`, `dotnet`, `java`, `postgres`, `redis`, `vegeta`; `memory.swap.peak`, the kernel's own lifetime high-water mark, read 0 for `containerd`, `dockerd`, `dotnet`, `java`, `postgres`, `redis`, `vegeta`. That covers `containerd`, `dockerd`, `redis`, `vegeta`, which the per-window verdict above does not.


The two quantities the CPU rows divide: total cgroup CPU over the aggregated windows, and the requests those windows serviced: headline.mixed.gc-default: Java 8636642 requests (0 non-2xx) over 2016140 CPU-ms, .NET 8119468 requests (1 non-2xx) over 3905462 CPU-ms. Dividing the second into the first reproduces the `CPU-ms / request` row exactly; that is the aggregation rule stated above.


## GC and pool activity (`/runtime-stats`, flushed per window)

Summed over the 116 `phase: measured` window summaries on disk that carry a non-empty delta block, across EVERY stage and rate -- ladder, confirm and soak alike. **This is not the roll-up's window set.** A `/runtime-stats` summary records no warmup verdict, so under-warmed and gate-excluded windows cannot be dropped here and are not; read these as run-total activity, never as a gated per-request figure. Every counter is a process-lifetime total differenced across that window's OWN edges, so summing them adds per-window increments and never adds a lifetime total twice. `(Nw)` after a per-generation count is how many of those windows reported that generation at all: `generations summed` is the writer's own list of which ones `collections total` covers, and a collector that does not expose a generation reports `not reported` rather than 0. `collections total` and `GC pause total` carry the same `(Nw)` and the same `not reported`: the JVM publishes a present null for every generation (so the summed total is null too) and for `pause_total_seconds` under ZGC, and a null summed as zero would publish a collector that never collected and never paused.

| slot | windows | generations summed | collections gen0 | collections gen1 | collections gen2 | collections total | GC pause total (s) | allocated (MB) | heap committed max (MB) | db pool max open / configured |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Java | 53 | none named | not reported | not reported | not reported | not reported | 16.525 (53w) | 1,657,801 (53w) | 328 | 24/24 |
| .NET | 63 | gen0, gen1, gen2 | 154,942 (63w) | 16,686 (63w) | 614 (63w) | 172,242 (63w) | 130.856 (63w) | 1,714,184 (63w) | 148 | 24/24 |


## Illustrative $/M-request cost

**Illustrative, and the price is an assumption.** $0.0400 per vCPU-hour is a constant chosen by this harness (from `analyze._VCPU_HOUR_USD_DEFAULT`), not a vendor quote and not a measurement. The figure is linear in it, so a reader with a different price rescales by the ratio of the two; the SLOT-TO-SLOT ratio does not depend on the price at all. The only measured input is the request-weighted `CPU-ms / request` from the roll-up above, which means every caveat on that number -- the windows it excludes, the cgroup it is taken from, the apparatus CPU it leaves out -- is a caveat on this one. Per-request CPU moved across this run's ladder rungs (largest per-slot coefficient of variation 12.5%), so these dollars belong to the operating rate(s) the roll-up used and to no other.


**headline.mixed.gc-default** at 4799, 4833 rps offered

| slot | $/M (mid) | +-20% band |
| --- | --- | --- |
| Java | $0.0026 | [$0.0021, $0.0031] |
| .NET | $0.0053 | [$0.0043, $0.0064] |

*Illustrative only.* `usd_per_million = cpu_ms_per_req * vcpu_hour_usd / 3.6`; pinned vCPU-hour = $0.0400; sensitivity band +-20%.


## Not captured in this run (scope)

- **The capacity gate was re-run at the REALIZED rates, and passed.** PostgreSQL ran at 0.185 of its 3-core budget against a 0.769 ceiling over the operating-point windows of headline.mixed.gc-default (.NET 4,833 rps, Java 4,799 rps). This is a MEASUREMENT of PostgreSQL under the actual cells, not a second projection: the pre-run gate rests on a PROJECTED knee, and round 1's did so while its cells then ran several times higher.

- **Effective working set and cache residency.** Targets are DRAWN PER ATTACK (`working_set.draw_mode` = `per_probe_stream`), Zipf-hot from 5,000,000 seeded invoices / 1,000,000 seeded customers, from a shared body pool of 150,000 create bodies sized from the mix, counted off the pool on disk to hold 52,097 distinct customer ids, and 200,000 quote bodies. Bodies carry no URL, so one pool serves both slots and both halves of an RCB block send byte-identical payloads. It costs one disclosure: a create body is drawn UNIFORMLY from that pool, so the create path's customer_id distribution is the pool's empirical Zipf sample rather than a fresh Zipf draw per request -- identical on the head, thinner in the tail. The read path is URL-only and unaffected. Each of the 116 measured windows drew its OWN stream of 61,200-2,957,796 (median 315,792) requests, naming 12,323-330,163 (median 51,642) distinct invoice ids (1.03% of the seeded invoices at the median) and 11,335-198,531 (median 43,270) distinct customer ids. Those are PER-PROBE counts and this run publishes no union across probes: two independent streams still overlap on the Zipf head, so the counts may not be summed, and a union accumulated in the finalizing process would come out short on exactly the runs that resumed. A measured window is 600 s against a 300 s cache TTL, so an entry populated at the head of a window can expire before that window ends: within-window residency is WEAKER than the Zipf skew alone implies, and weaker than the constructed target hit rate of 0.80 implies. The warmup, the head-of-window discard and the measured attack each draw a separate stream, so no attack replays the SEQUENCE another is about to consume -- which is what R1-A7 was, the warmup consuming the measured window's own prefix after a FLUSHALL. It does NOT mean a window opens cold: the streams are independent draws from the same Zipf keyspace, so the hot head appears in both by construction, and that residual is measured rather than asserted away. Measured over 116 windows, 57.0-63.5% (median 60.4%) of a window's invoice-id READ REQUESTS name an id an earlier attack of the same probe had already drawn, against a constructed target hit rate of 0.80. That is the read path's residency floor and it is a property of the workload, not of the harness. The REALIZED Redis hit rate this run was 0.629 (manifest `cache_stats`, INFO keyspace deltas over measured windows).

- **DB CPU is per CONTAINER, not per endpoint or per statement.** The PostgreSQL column is a whole-cgroup `cpu.stat` delta, so it includes autovacuum, the checkpointer and the WAL writer -- maintenance that is debt from earlier windows and is not proportional to this probe's requests. Attributing cost to a statement needs `pg_stat_statements` deltas, which this run does not capture.

- **Container placement matches the resource ledger.** 5 sampled cgroup(s) read back the `cpuset.cpus.effective` infra/compose.bench.yaml asked for. containerd, dockerd declare no compose cpuset (pinned by the systemd drop-in instead).

- **Load-generator and container-runtime CPU is measured and EXCLUDED.** `containerd`, `dockerd`, `vegeta` are sampled (manifest `load_generator_overhead`) so the single-host accounting is auditable, but their CPU is apparatus and never enters a per-request figure. The CO-safety of the load rests on the per-probe achieved==offered evidence (manifest `achieved_vs_offered`).

- **/runtime-stats polling is inside the measured window, and its cost has no denominator.** This run flushed 232 window summaries (`<slot>[.warmup].runtime_stats.summary.json`): 116 measured, 116 warmup. 232 of them carry both window edges and 232 carry a non-empty end-minus-start delta block (a window with a single successful poll has edges but no delta, since a delta needs two); 232 per-poll `.jsonl` traces sit beside them (a window with no successful poll writes none). Every one of them records at least one successful poll. The polls' SUT-side CPU lands in the cgroup delta, while `/runtime-stats` is not one of the frozen `endpoint_calls` routes and is not in vegeta's request count either -- so per-request CPU is overstated by that much. 232 of them carry a MEASURED upper bound on that cost (`poll_cost.round_trip_ms_total`, the client-side round trip), totalling 38,803 ms across the run. The alternative, polling only at the boundaries, would lose the within-window heap trace that decides the heap-policy question.

- **Hysteresis, p999 order-statistic CIs, and isolation runs.** Not captured in this build (see docs/METHODOLOGY.md).

