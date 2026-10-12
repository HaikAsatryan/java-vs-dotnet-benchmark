# PROGRESS.md — Ledgerline round 2: Java vs .NET

The working plan and its outcome. Status markers were updated in place as phases
completed; there is no changelog.

> **Frozen at publication, 2026-10-12.** This is the project's working record, published
> as it was written rather than tidied into a document, apart from the removal of a few
> instructions addressed to the AI coding assistant. It is kept in the public tree
> because it is the glossary for the identifiers cited across the code, the docs, the
> frozen `spec/` and the checksummed run report: decisions `D1`-`D12`, phases `P0`-`P10`,
> gates `G1`-`G8`, round-1 defects `R1-A*` / `R1-B*`. The work items `W1`-`W31` are in
> `docs/p1-plan.md`. For the result itself, start at `README.md`.

Status legend: `TODO` / `WIP` / `DONE` / `BLOCKED`.

---

## Where this ended, 2026-10-12

**P0 through P10 are DONE. The measured run is complete, analyzed and verified:
`results/full-20260827T174611-fedora` — one clean pass (2026-08-27 17:46 → 2026-08-28
~10:10 +04, zero crashes, zero resumes, `verify` ok over 1516 artifacts,
`finalization_error: null`). Headline, own-knee scope: Java serves the 20 ms promise
at ~52% LESS app CPU per request (0.2344 vs 0.4840 CPU-ms/req at knees 4,799 vs
4,833 rps) and ~3.7x MORE anon RSS (656 vs 176 MB in-window p99). Full evidence and
the caveats that must travel with those numbers are in the P8 section below and in
`results/full-20260827T174611-fedora/report/report.md` — read the caveats before
quoting anything.**

The standing rules outlive publication: never edit `spec/` (config_hash frozen at P7,
and `results/README.md` shows readers how to recompute it), and never quote a round-1
number next to a round-2 number.

The review decisions of 2026-08-27 are recorded in the decisions table: strict
canonicality with `AddDbContextPool` and `MAX_AUTO_PREPARE=20` kept (D12), **one** full
measured run + the rehearsal as replication (D10 revised).

| check | command | state |
|---|---|---|
| harness | `cd bench && uv run pytest -q` | **903 passed, 21 skipped** (Windows, 2026-10-09; CI runs the same suite on Linux on every push) |
| harness lint | `cd bench && uv run ruff check .` | clean |
| Java SUT | `cd services/java && ./mvnw -B test` | **245 passed, 11 skipped** (256 run), BUILD SUCCESS |
| .NET SUT | `cd services/dotnet && dotnet test Ledgerline.Tests -c Release` | **87 passed** |
| .NET format | `cd services/dotnet && dotnet format --verify-no-changes` | exit 0 |

**Fixed in the 2026-08-27 review sweep** (each adversarially verified before fixing):
the `--resume --run-id <typo>` path that silently started a new run and wiped the
crashed run's volumes; the vegeta per-second bucketer's issue-order assumption
(vegeta writes completion order); runtimestats absent-keys-as-zeros (vacuous
`gc_steady`); the paired anon None-as-0.0 fold; analyze re-fitting under an edited
spec without a hash check; the knee bootstrap dropping non-crossing resamples
(now right-censored); the unchecked head-of-window discard; the atomic writer's
missing directory fsync; host-restore truncating a pre-existing systemd drop-in;
smoke bypassing the env scrub; `sut-java` missing from compose entirely.

---

## What this is

A benchmark comparing **Java 25 LTS (Spring Boot 4 + Hibernate 7)** against **.NET 10
(Minimal API + EF Core)** on a realistic invoicing/billing backend ("Ledgerline").
Headline: **cost at a fixed latency promise** — CPU-ms and steady-state memory per request
at the rate where mixed-workload p99 crosses 20 ms, as a Java:.NET ratio. Shared
PostgreSQL 18.4 + Redis, DB in its own cgroup, open-loop CO-safe load (vegeta), cgroup v2
ground truth.

### Relationship to round 1: none

Round 1 (`HaikAsatryan/go-vs-dotnet-benchmark`) compared Go against .NET. This repo was
seeded from its skeleton and is otherwise **unrelated**: separate history, separate remote,
no shared code, no upstream, no cherry-picking in either direction. Round 1 is finished and
frozen; it will never receive fixes, so there is nothing to sync.

Round 2 re-measures .NET from scratch. **A round-1 number may never be quoted next to a
round-2 number.** Different harness, different patch levels, different day, different
thermal state.

Carried over: `spec/`, `bench/`, `infra/`, `docs/`, `seed/`, `services/dotnet/`, CI.
Dropped: round-1 `results/`, `services/go/`, the round-1 blog and figures, all history.

`seed/` is still a Go program. **Deliberate.** It loads PostgreSQL and never runs during a
measured window, so it is tooling, not a system under test. Rewriting it buys zero
measurement value and risks the DB invariants.

---

## Round-1 post-mortem: what actually went wrong

Round 1 shipped and its documentation is honest about its own gaps. This table is that
honesty converted into round-2 work. Sources are `docs/METHODOLOGY.md`,
`docs/FAIRNESS.md` and `results/README.md` in the round-1 repo.

### The one that invalidated the headline

**R1-A1. The knee was fit on Go and .NET p99 *pooled*, and both stacks were then measured
at one shared rate.** The headline metric is defined as cost at a fixed *latency promise*,
which requires each stack measured at **its own** SLO-crossing rate. That is not what ran.

The damage compounds:

- The headline cells laddered to **1,493 rps** (7 rungs) and **1,244 rps** (6 rungs),
  because the pooled fit found the knee where **.NET** crossed, and .NET was crippled by
  Npgsql's `Max Auto Prepare=0` default (knee 1,468 rps; at `=20` it is 10,426 rps).
- Go never crossed the SLO inside any measured ladder, in any cell. **Go has no measured
  knee.** The comparison ran at a rate set entirely by .NET's worst configuration, where
  Go was loafing.
- Per-request cost is **not** rate-independent for .NET: CoV 19.3%, falling from 0.648
  CPU-ms/req at 500 rps to 0.308 at 13,312 rps. The headline cells therefore measured in
  the steepest, most rate-sensitive part of that curve.

**Round-2 fix:** per-language isotonic fits drive per-language confirm rates; the ladder
runs until **both** stacks cross the SLO, not until a pooled fit brackets one of them; and
the cost-vs-rate curve is a published first-class result, not an erratum. Pre-registering
`Max Auto Prepare=20` (D4 below) removes the artificial ceiling that caused it.

### Measurement-quality failures

| id | Round-1 defect | Round-2 fix |
|---|---|---|
| R1-A2 | **59.5% of windows flagged under-warmed** (368/618), language-asymmetric: Go 67.3% vs .NET 51.8%. `p99_flat`/`p999_flat` on the per-second p99 series trip at high rates. Several roll-ups rest on one or two warm windows. | Re-derive the flatness gates against real per-second series before freezing. A gate that rejects two thirds of the data is miscalibrated, not strict. Validate at P6 against a measured distribution. |
| R1-A3 | **A whole cell was wasted.** In `gc-generous`, all 39 Go probes failed `gc_steady` **by construction** (`GOGC=off` can never satisfy it) and 26/39 .NET probes failed with no declared cause. ~30 h of runtime produced one directional sentence. Round 1's own prescription: drop the gate for that posture and say so, or drop the cell. | Any posture whose gates are unsatisfiable by construction is either given a posture-specific gate set (declared as such) or is not run. Checked at P6 before committing cell time. |
| R1-A4 | **Bootstrap CI is not inferential.** The SLO bracket carried as few as **one** measurement-grade rep against a pre-registered minimum of 10. Published as a spread indicator. | Downstream of R1-A2. Confirm reps must survive the gates; verified at P6. |
| R1-A5 | **Paired verdicts existed in only 1 of 5 cells**, and both intervals spanned zero. Direction and effect size only. | Downstream of R1-A2. |
| R1-A6 | **Capacity gate tested the wrong rates.** The 1.72x margin was taken against 2,160 rps of *projected* demand; cells then ran at 9,244–13,312 rps, implying ~5,000–7,200 rps of DB load. PG never broke, but the gate never tested it there. | Two-stage gate: pre-run projection as now, then a **re-gate at realized rates** once the first cell's knee is known, before the remaining cells run. |
| R1-A7 | **Targets file replays in order.** 1M requests cycling every 75–108 s against a 300 s cache TTL makes the read path more cache-resident than the constructed 0.80 hit rate implies. Round 1 named the fix and did not take it. | Draw targets per probe from `workload.rng` (already deterministic) instead of replaying a fixed file. |

### Integrity and evidence holes

| id | Round-1 defect | Round-2 fix |
|---|---|---|
| R1-B1 | **Two of six declared warmup gates were never implemented** (`hot_set_touched`, `major_faults_asserted_equal`), *and their absence was not surfaced in the per-probe records*. A probe could be recorded warm without them ever being evaluated. | See the governing rule below. Implement or delete; never declare. |
| R1-B2 | **Host state was never read back.** `manifest.host_state`, `topology`, `postgres_config` all record `source: not_measured`. The run cannot evidence the governor, boost, C-state, THP, IRQ or cpuset placement it ran under. "Same host" is inferred from procedure, not from artifacts. | Read back and record every tuned value at run start and on every resume. Fail the run on mismatch. |
| R1-B3 | **`/runtime-stats` GC counters were polled live but never flushed to disk**, so GC collection counts and pause totals do not exist in the artifacts. | Flush per window. Cheap, and it is the only GC evidence a reader gets. |
| R1-B4 | **`CHECKSUMS.run.sha256` is not a usable verification file**: 108 paths carry two hashes from resumes, it covers gitignored traces, and it never covered the roll-ups. | One checksum file that verifies, or none. |
| R1-B5 | **`manifest.identity.git_sha` no longer resolves**, because history was wiped at publication *after* the run. Provenance now rests entirely on `config_hash`. | History was reset to a single commit at publication on 2026-10-12; provenance rests on `config_hash` and `CHECKSUMS.sha256`. |
| R1-B6 | **Three claims in `docs/FAIRNESS.md` were overclaims** corrected in place after the fact, and **four live asymmetries were undisclosed** until after the run. | The P7 adversarial review exists for exactly this. Its brief is to find the fourth category. |

### Promised and never delivered

Round 1 pre-registered these, then retired them at a "scope freeze" without running any:
CO validation suite, oha tail cross-check, logging-sink microbench and no-drop test,
connection sweep, DB-CPU-per-request column, cold-start curve, descending hysteresis pass,
p999 appendix with order-statistic CIs, between-run dot plots, isolation runs,
quota-vs-cpuset, boost-on, memory-pressure sweep.

### The actual lesson

**Round 1's failure mode was over-declaration, not under-execution.** It promised a
methodology far larger than it ran, then spent its credibility on disclosing the gap.
Round 1 said it best: *"pre-registering checks that will never run is worse than disclosing
their absence."*

So the governing rule for round 2, and it is not negotiable:

> **Nothing enters `spec/` as a declared gate until its implementation exists and its test
> passes.** Scope is decided by what is working at the P7 freeze. Anything not implemented
> by then is deleted from the spec rather than declared and skipped.

This inverts round 1's order. It is why P1 and P2 come before the Java service, and why the
freeze is a phase of its own.

---

## Pre-registered decisions

Frozen at P7. Changing anything under `spec/` after a run starts invalidates `config_hash`
and the run cannot resume.

| # | Decision | Value | Rationale |
|---|---|---|---|
| D1 | Java version | **Temurin JDK 25 LTS**, HotSpot | LTS against .NET 10 LTS. Java 26/27 are non-LTS; "latest" would cost credibility, not buy it. |
| D2 | Framework | **Spring Boot 4.x, WebMVC, virtual threads on** | Mainstream production Java by a wide margin, same reason EF Core (not Dapper) carried the .NET headline. WebFlux rejected: reactive is not the analog of .NET's async model, virtual threads are. |
| D3 | Data layer | **Spring Data JPA + Hibernate 7** | Direct analog of EF Core: change tracking, dirty checking, query language over entities. ORM against ORM is a cleaner headline than round 1's ORM-against-hand-SQL. |
| D4 | `MAX_AUTO_PREPARE` on .NET | **20** (`AUTO_PREPARE_MIN_USAGES=5`) from the start | PgJDBC auto-prepares by default at `prepareThreshold=5`. Leaving Npgsql at `0` rebuilds the exact bias that capped round 1's headline ladder at 1,493 rps. |
| D5 | Data-layer variants | **None** | No ADO / Dapper / jOOQ cells. The blog points at round 1 for the ORM-cost axis. **Enforced 2026-08-27**: the `variant.ado`/`variant.dapper` cells that contradicted this were deleted from `phases.CELLS`; the data layers remain code (P2's shakedown runs `ado` as a slot). |
| D6 | Heap policy | **DECIDED at G6 (2026-08-27): one cell, defaults** | Measured: `gc.heap_committed_bytes` peaked at 298 MB across all Java measured windows of the G6 rehearsal, against the 25%-default ~3 GiB ceiling at the real 12 GiB limit. The ceiling never binds; the equalized cell was deleted per this decision's own rule. See "The heap question". |
| D7 | GC engine | G1 (JVM default) in the defaults cell | Most teams keep G1. Generational ZGC is the equalized-cell candidate. |
| D8 | .NET dependency versions | Latest stable at run start, recorded in manifest | Round 2 stands alone; nothing to preserve by pinning old versions. |
| D9 | Knee fitting | **Per-language fits, per-language confirm rates** | R1-A1. Non-negotiable; the headline metric is undefined without it. |
| D10 | Repeat runs | **REVISED 2026-08-27 (Haik): one full measured run**, with the P6 rehearsal (reduced tier) as the replication check. *As run: the rehearsal started about six and a half hours before the full run, on the same day, so the check is cross-tier and not between-day* | The original three-run plan bought a between-run-spread appendix at 2-3 weeks of wall clock; a blog does not need it, and the schedule risk of never shipping outweighs it. The rehearsal-vs-full comparison is published as the replication evidence, labelled as cross-tier. |
| D11 | Java build tool | **Maven + `mvnw` wrapper**, pinned 3.9.16 | Not in the original table; decided at P1. Maven 4.0.0 is still `rc-6` (2026-08-04), not GA, and 3.9.16 is the recommended production line — `start.spring.io`'s own generated wrapper pins exactly 3.9.16. A declarative POM is also cheaper for a fairness reviewer to audit than a Gradle build script. |
| D12 | Canonicality rule | **Strict template-canonical, both sides** (Haik, 2026-08-27) | Each SUT is what an ordinary production team ships. Removed from .NET on this rule: the committed EF compiled model, `CreateSlimBuilder` (→ `CreateBuilder`), the STJ source-gen context (→ reflection STJ), the chiseled/ICU-less image + `InvariantGlobalization` (→ standard `aspnet:10.0-noble` with ICU, matching Java's full JRE). Kept by Haik's explicit ruling: `AddDbContextPool` (canonical) and `MAX_AUTO_PREPARE=20` (behavior parity with PgJDBC's default `prepareThreshold=5`, disclosed, with `variant.npgsql-default-prepare` as the default-vs-default axis). The Java side already followed the rule (no CDS/AOT). Full inventory in `docs/FAIRNESS.md` ("Removed under the strict-canonicality rule"). |

### The heap question

Both containers get an identical 12 GiB `mem_limit`. One layer up, each runtime picks its
own heap ceiling from a **flag default**, and the defaults differ threefold: the CLR takes
≈75% of the cgroup limit plus DATAS shrinking toward live set, while the JVM's
`-XX:MaxRAMPercentage=25` gives ≈25%.

This matters because the two headline numbers sit on one tradeoff curve: more heap means
fewer collections means **less CPU and more RSS**. Different default ceilings put the two
runtimes at different points on that curve before any of our code runs.

Note the direction: a 25% cap makes Java look **good on memory and bad on CPU**. The
equalized cell would probably raise Java's RSS. Nobody is being flattered.

Round 1 already set the precedent, and it is in `docs/FAIRNESS.md`: Npgsql's
`Max Auto Prepare=0` was judged unrepresentative and **the headline was taken from the
parity cell** while the defaults cell shipped alongside. Same rule applies here.

**Resolution is empirical, at G6.** Ledgerline has a small live set (round 1 measured .NET
at 212 MB anon). If the JVM's steady-state heap settles far below a 3 GiB ceiling, the
ceiling never binds and the argument is moot: one cell, defaults, done. The
counter-possibility is that G1 sizes young gen toward a pause target and expands well above
live set given room, where DATAS shrinks toward it. Measure
`gc.heap_committed_bytes` under sustained load and decide. Do not litigate it in prose
again. If the axis is needed:

- `headline.mixed.gc-default` — JVM 25% + G1 vs CLR 75% + DATAS (pure flag defaults).
- ~~`headline.mixed.gc-generous`~~ — **DELETED at G6 (2026-08-27)** per this
  section's own rule. It paired JVM 75% + generational ZGC against the CLR with
  DATAS OFF; G6 measured the JVM's heap committed peaking at **298 MB** against the
  ~3 GiB default ceiling at the real 12 GiB limit, so the ceiling never binds and
  the axis is moot: one cell, defaults, done.

---

## Scope: in, out, and decided at the freeze

Round 1's descoped list, re-decided. "Full time available" is not a reason to declare
everything; the governing rule still applies, and anything below that is not working by P7
gets deleted rather than promised.

**In, and implemented in P1** (corrected 2026-08-27: this table once listed five items
as implemented that had no code — the exact over-declaration this file warns about,
one section up. Only what is actually built stays under this heading):

| item | why it is worth the work |
|---|---|
| DB / vegeta / dockerd cgroup sampling (the DB-CPU-per-request column) | **Now essential, not optional.** Round 2 is ORM vs ORM with different generated SQL. Without this, "Java costs more CPU" cannot be separated from "Hibernate's SQL plans cost PostgreSQL more". It is also cheap: sample two more cgroups. W16. |
| Redis cgroup sampling | Same mechanism, closes R1's "no Redis utilization figure". W16. |
| `/runtime-stats` flush per window (R1-B3) | The only GC evidence a reader gets. W17. |
| Host-state read-back (R1-B2) | Turns "we tuned the box" from a claim into an artifact. W18. |
| Per-probe target draw (R1-A7) | `workload.rng` already makes it deterministic. W20. |
| Dollar figure | Trivial once the CPU ratio exists; the framing is "cost", so publish it with the vCPU price stated. In `analyze`. |

**In, built at P9/P10** (publication acts, not measurement features, so nothing to freeze):

| item | plan | outcome, 2026-10-12 |
|---|---|---|
| Cold-start curve | Salvaged as a DERIVED statistic at P9: time-to-gates-pass per stack, read off the warmup artifacts the harness already writes per probe — not a new phase or cell. JVM startup is Java's most famous weakness and .NET's quiet strength, and it is nearly free from data already on disk. | **Delivered as time-to-settle, not as cold start.** `docs/figures/fig5-warmup.png`, drawn from the per-probe warm-up records. Process start to first healthy response is not in the artifacts and is claimed nowhere. |
| Between-run replication | D10 as revised: the P6 rehearsal against the full run, published as a cross-tier replication check. | **Delivered.** `results/README.md`, "The rehearsal": the ratios replicate, the absolute knees do not, and it says why. |
| Raw-artifact publication | GitHub Release with checksums for the traces, at P10. | **Not delivered.** No Release carries the traces. `results/README.md` says so and says what is in git instead: a disclosed absence, per the governing rule, rather than a standing promise. |

**Cut on 2026-08-27** (were listed as "implemented in P1" with no implementation;
per the governing rule they are deleted from scope, not promised — the same fate P7
would have handed them): descending hysteresis pass, p999 appendix with
order-statistic CIs, logging-sink microbench + no-drop test, oha tail cross-check
(the vegeta image ships no oha; the per-probe `achieved==offered` evidence stands).

**Reduced in scope, deliberately:**

- **Connection sweep.** A full per-stack pool-size optimum multiplies cells. Instead run it
  at rehearsal scale as a *sanity check* that 24/24 is not pathological for either stack,
  and publish it as that, not as a derived optimum.

**Out, and stays out:**

- CO validation suite beyond achieved==offered plus the docker-pause stall check.
  Round 1's per-probe `achieved==offered` at ratio 1.0000 is real evidence and the full
  suite is a large build for a small marginal claim.
- Isolation runs, quota-vs-cpuset, boost-on, memory-pressure sweep. Out of scope for a
  language comparison; they were host-tuning studies wearing a benchmark's clothes.

---

## Phases

Each phase ends at a gate. Do not start the next phase until its gate passes.

### P0 — Repo bootstrap — `DONE`

Skeleton copied and curated, `git init`, remote `HaikAsatryan/java-vs-dotnet-benchmark`
(private), `main` pushed.

### P1 — Harness: de-language and fix the round-1 defects — `DONE` (gate G1 passed)

The largest phase, and it comes first because everything downstream is written against it.
Two workstreams in one edit surface.

**The executable breakdown lives in `docs/p1-plan.md`** (31 work items, W1-W31, each with
its file anchors, its proving test and its dependencies). This section stays the summary;
that file is the working order. Measured findings that constrain it are in
`docs/research/2026-08-21-measured.md`.

**Done so far.** Baseline before P1: 324 passed / 20 skipped, ruff clean, `.NET` 87 passed,
but `make lint` and two CI jobs were **red**. Now: **806 passed / 19 skipped**, ruff clean,
`dotnet format --verify-no-changes` exit 0, `.NET` build 0 warnings / 0 errors, 87 passed.

Stage 0 (W1-W3), Stage 1 (W4-W12) and Stage 2 (W13-W19) are complete, plus part of W28.
Stage 3 (W20-W26) has W20, W21 (step 1), W24 and W26 done; W22, W23, W25 and
Stage 4 (W27-W31) remain.

| item | what it closed |
|---|---|
| W1 | **`.editorconfig` restored.** `ci.yml` documented the file and depended on it, but it was lost in the P0 seeding, so `dotnet format --verify-no-changes` had been failing since the first commit with 1,697 whitespace errors across 41 files. It pins C# to the **3-space** indent `services/dotnet/` is actually written in — `dotnet format`'s 4-space default would have rewritten the style template P4 is told to mirror. |
| W2 | **`Makefile` + `ci.yml` repaired for the deleted `services/go`.** The CI `go` job failed at `setup-go` before building anything (`go-version-file: services/go/go.mod`, a path that does not exist); `make lint` failed on `gofmt -l services/go`. The job is now `seed (go)` over `seed/` alone. The `java` job lands with `services/java/` at P4 rather than dangling now the way the Go steps did. |
| W3 | **`util.proc.run` honours its own timeout contract.** `subprocess.TimeoutExpired` propagated regardless of `check`, and there was not one handler in `bench/`. Every live call site passes `check=False, timeout=N` precisely because it wants to handle the failure, so a wedged `docker stop` inside `run_probe` aborted a 15 h cell instead of failing one probe. A timeout is now a result (`returncode == 124`, `timed_out`), raising only under `check=True`. |
| W13 | **Segmented warmup with early exit.** Warmup ran one blocking attack for the full `hard_cap_seconds` and evaluated the gates once, so the cap was a *duration*, not a ceiling — and O4's 180→300 raise would have cost +120 s on **every** probe (~9.4 h per cell, ~56 h across D10's three runs). It now runs in 30 s segments and stops at the first boundary where every enforced gate passes. Two coupled defects fixed with it: `_warmup_rate` divided by the cap, so raising it silently dropped warmup traffic 618→371 rps; and `under_warmed = under and elapsed >= cap_s` recorded a **crashed** warmup as warm, because a failed attack stops early. |
| W4 | **`bench/ledgerbench/registry.py`: the slot registry.** Workstream A's spine, and it is keyed by **slot, not language** — otherwise P2 cannot run at all, because its two slots are both .NET and would collide on service, port, pdf volume, ledger enum and *artifact filenames*, where they overwrite each other silently and leave a run that looks valid. GC semantics are declared as data per runtime, and every lookup **raises** on an unregistered id: round 1 dispatched `if lang == "dotnet": ... else: <Go semantics>`, so any other string silently inherited Go's single-generation model and still reported `passed`. Also fixes the **ratio direction**: PROGRESS declares a Java:.NET headline while the code computed `.NET : Go`, so a mechanical rename would have published the reciprocal — a number that reads plausibly and is wrong. |
| W6 | **`compose.py` registry-driven; the dead `sut.py` twin deleted.** `KNOB_ENV_VARS` is now *derived* from the registry rather than hand-listed, which adds two knobs that were never scrubbed: `DB_CONN_LIFETIME` (so `manifest.Knobs.db_conn_lifetime_seconds` was **always null** although `spec/db-access.md` declares the lever and compose sets it) and the JVM flag carrier `JDK_JAVA_OPTIONS`. A test regexes every `${VAR}` out of both compose files and asserts the remainder is scrubbed or carries a written exemption. `sut.py`'s lifecycle half was a second, **zero-caller** implementation of what `live_runner` does per probe — deleted rather than de-languaged. |
| W11 | **compose topology.** `sut-go` removed (its build context had been dangling since P0, and `docker compose config` exits 0 on a missing context, so nothing caught it). Shared SUT env hoisted into an `x-sut-env` anchor so field-for-field parity is structural instead of eyeballed. Added `sut-dotnet-b` (profile `dotnet-b`, port 8083, its own pdf volume and its own `${DATA_LAYER_B}`) so P2 has a real second slot. **D4 applied**: `MAX_AUTO_PREPARE` now defaults to 20 with `AUTO_PREPARE_MIN_USAGES=5`, not Npgsql's 0. `sut-java` deliberately does **not** exist yet — declaring it before `services/java/` would recreate exactly the dangling reference W2 just repaired. |
| W28 (part) | **`spec/db-access.md` exec/prepare table rewritten** for D4, with measured evidence: on PostgreSQL 18.4 with `log_statement='all'`, PgJDBC's first four executions log as `execute <unnamed>` and the fifth as `execute S_1`, so `prepareThreshold=5` does what the spec now claims. The table also states plainly that `Max Auto Prepare=20` is **not** an Npgsql default — it equalizes the *behaviour* (both stacks promote on the 5th execution) at the cost of one side's literal default. |
| W5 | **Ledger schema v2: slot ids, a version that is READ, loud load failures.** `ProbeRecord.lang: Literal["go","dotnet"]` → `slot: str` validated against the registry, `slot_order` non-defaulting (with a model validator that a probe's slot is *in* its own block order), and the compared pair pinned as `Ledger.slot_set` — so an artifact DECLARES which two SUTs produced it instead of `analyze` inferring it from whichever probes exist, and a resume under a different `LEDGERBENCH_SLOT_SET` is refused rather than blended. `LEDGER_SCHEMA_VERSION` was written since round 1 and never read; it is now checked **before** validation (a v1 file cannot be coerced into the v2 model, so a bare `ValidationError` would name a missing `slot` field rather than the real problem) and `load()` **raises** instead of returning None, because its only caller does `load(...) or create(...)` and would have written a fresh ledger over a partially-measured run. The bare `except Exception: continue` in both scan paths is replaced by a stderr report: a ledger the code could not validate simply **vanished**, and `--resume` answered "no unfinished run to resume" for what was a schema problem. `probe_key`/`split_probe_key` move here as the single definition of the `r<rate>.b<block>.<slot>` wire format (it was an f-string in `phases.py` and a private parser in `analyze.py`), and `registry.Slot` now refuses a slot id containing `.` or `/` so that format cannot become ambiguous. |
| W9 (part) | **RCB block planning is slot-driven.** `slot_order_for_block` was a two-way coin flip over `["go","dotnet"]`; it is now a seeded Fisher-Yates over the run's slot set, consuming exactly one draw at n=2 so a stage's order stream stays aligned with its salt. `_execute_block_probes` no longer falls back to a hard-coded pair when `slot_order` is empty — that fallback is what let a probe execute in an order no artifact could justify. |
| W10 | **`analyze.py` / `paired.py` / `report.py` de-languaged, and the ratio direction fixed.** ~50 sites: `_LANGS` → the run's pinned slot set; `_lang_label`'s bare `else` (which rendered **any** unknown id as `.NET`) → a registry lookup that raises; every `go_*`/`dotnet_*` dataclass field and fits.json key flattened to `by_slot`. **The direction fix is the substantive half.** PROGRESS defines a Java:.NET headline; the code computed `.NET : Go` for `cpu_ratio`, `cpu_ratio_unweighted`, `anon_ratio` and `peak_ratio`, so a mechanical rename would have published the reciprocal. Every ratio now divides `SlotSet.subject` by `SlotSet.reference` and carries a `RatioDirection` **in the artifact** — round 1 encoded direction in the key name (`cpu_ratio_dotnet_over_go`), which can drift away from the arithmetic; a field generated from the same `SlotSet` the division uses cannot. Note the trap: `paired.py`'s `mean_diff_pct` was the one published number whose round-1 direction already matched the round-2 declaration, so the flip is **not** uniform across the five. `paired.py` is now slot-agnostic (`subject`/`reference`, neutral verdict tokens) and `report.py`'s hard-coded `{"go_better": "Go better"}` map became a caller-labelled `verdict_word`. Deleted: `assemble_report` and `headline_ratio_table` (a second, dead report assembler); `_KNOB_EXPLAINS_GATE`'s single entry, which claimed "this cell sets `GOGC=off` for Go" about a language this round does not run (the machinery stays, tested against a synthetic entry, until W22 makes it refuse the cell at plan time). |
| W7 | **Manifest schema v3: per-slot rows, JVM knobs, `Versions` populated.** `CellKnobs.go/.dotnet` and `RunManifest.knobs_go/knobs_dotnet` → `by_slot` maps; the Go knobs replaced by `max_ram_percent` / `gc_engine` / `prepare_threshold`; `auto_prepare_min_usages` added so **both halves of D4 can be evidenced side by side** rather than asserted in the spec (`spec/db-access.md` may not claim the `prepareThreshold=5` ≡ `Min Usages=5` pin until the manifest can show it). Deliberately **no** `active_processor_count` field: measured on JDK 25 the flag reads back `-1 {DEFAULT}` while `availableProcessors()` correctly returns the cpuset size, so a second field would routinely disagree with the one carrying the assertion. **This had to land before the first run**, not after: `RunManifest` is `extra="forbid"`, `load()` did not catch `ValidationError`, and its only caller writes `load(...) or new_manifest(...)` — an `or` cannot rescue a raise. The version is now read on load (same guard as the ledger). `Versions` was `source: not_measured` on every round-1 run while `_record_cell_knobs` was already fetching the `/runtime-stats` build block and **discarding it**, and `compose.container_image_digest` was fully implemented with **no caller**; both are now recorded per slot. `Identity.generated_code_hashes` deleted — never written, no `not_measured` marker, so it published `{}` as "measured and empty", and its source (sqlc's generated Go) does not exist in this round; `schema_sql_sha256` is populated instead. Corrected in passing: the `Versions` docstring claimed the build block carries `npgsql_version`; it does not. |
| W9 (knobs) | **`_record_cell_knobs` registry-driven.** The two hand-kept knob maps and `_INT_KNOBS` are gone — a knob added to a language but forgotten in a second local list is the same defect one level down, and an int knob missing from a local set silently lands as a string. `expected_dl` was `{"go": "sqlc", "dotnet": ...}`, so **P2's `ado` slot would have mismatched on every probe and raised `PhaseError` before the shakedown could run one block**; it now reads the slot's own `expected_data_layer`, overridden by a cell env that names one. The `headline.mixed.gc-generous` cell's levers moved from Go's `GOGC=off` + `GOMEMLIMIT` (a posture that could never satisfy `gc_steady`, which is what R1-A3 spent ~30 h proving) to the JVM heap-equalised pair `MaxRAMPercentage=75` + generational ZGC against the CLR's DATAS-off — the two ends of the axis D6 leaves open, so the cell measures something until G6 decides. |
| W4 (gc dispatch) | **`warmup.gc_steady` is one function driven by declared data.** Round 1 had two near-identical predicates and `if self.language == "dotnet": ... else: <Go's single-generation model>`, so **any** other string silently got the wrong collector's semantics and the gate still reported `passed`. There is now one `gc_steady(series, GcSemantics)` and `registry.gc_semantics()` **raises** on an unregistered slot. The `_gen_cadence` twin (which `return`ed `_gc_cadence()` verbatim, so the ".NET generational" check was really the summed total cadence) is deleted rather than carried. The per-probe gate record now names the model and which series were tested. |
| W12 | **The anti-sed guard, and it fires.** `test_no_language_or_slot_literal_outside_the_registry` walks every module in `ledgerbench/` with `ast` and refuses a bare string constant equal to any slot id, service, profile, or round-1 language literal — **excluding docstrings**, because prose has to be able to name what was removed and why, while the defect being guarded is a literal the code *branches on* or *builds a path from*. Verified by planting `_PLANTED = "sut-go"` in `doctor.py`: the guard names the file and the literal. |
| — | **Two more live defects found and fixed while de-languaging:** `capacity_gate.create_only_targets` defaulted `base_url` to `http://sut-go:8080` — a service that no longer exists on the compose network — and **no caller ever overrode it**, so the create-only ladder pointed at nothing (and vegeta fails an unreachable target in ~10 µs without touching the SUT, which is exactly how round 1 published a rung of pure errors as a PG ceiling). `base_url` is now required, the driving slot is the slot set's `reference` side, and **which slot the ceiling was measured through is recorded in the phase record** — with Hibernate and EF emitting different INSERT shapes, PG's create ceiling is a per-slot quantity, not a property of PostgreSQL. |
| W10 (figures) | **`bench/figures.py` reduced to scaffolding.** It read its run id and parsed both artifacts **at import**, so `import figures` raised on any tree without round 1's `results/` — i.e. every tree in this round. Its six charts were round 1's published conclusions hard-coded as chart text ("C# burns 3-5x the CPU per request", "Go never crossed 20 ms anywhere in this run"), over `HEADLINE_CELL = "variant.prepared-parity"`, a cell D4 deletes. Keeping them "to adapt at P9" would have meant carrying another benchmark's findings inside this repo's figure generator, against CLAUDE.md's never-quote-round-1 rule. What survives is what is not about round 1: the palette (now keyed by **slot**, so P2's two .NET slots do not draw in one colour), the save/caption helpers, and artifact loading parameterised by run id. |
| — | **Bug caught by the adversarial review and fixed:** D4 made `MAX_AUTO_PREPARE=20` the baseline, which turned the existing `variant.prepared-parity` cell (also 20) into a silent no-op that would have burned hours re-measuring the headline. Inverted to `variant.npgsql-default-prepare` (`=0`, the Npgsql default), and `test_every_variant_cell_actually_differs_from_the_baseline` now regexes the compose defaults so no variant can go no-op again. |
| W14 | **The `rehearsal` tier — the one that validates NUMBERS before days are committed.** `resolve()` was `if/if/raise`; it is now a `SCALES` table that the CLI's validation, its help text and the error message all read, so none of them can name a set the code does not build. The substantive half is a **second dial**: `scale` names the TIER, `seed_scale` names the DATASET. They are not the same thing, and only `seed_scale` reaches `spec/workload.yaml seeding.scales` and the Go seeder's `ParseScale` — both of which know exactly `full\|smoke`, so a tier whose name is not a dataset name KeyErrors in the target generator and is rejected by the seeder. `rehearsal` sets `seed_scale="full"`: at smoke's 1k/5k rows the whole read path is cache-resident, which answers G6's questions by construction rather than by measurement. Deviations from `full` are confined to wall-clock dials (6 rungs, 2 reps, 2 confirm blocks, 300 s soak, one cell) plus one deliberate methodological one — `ladder_step_factor=2.0`, because six rungs at the spec's 1.2 top out at **1,244 rps** and with a knee near 10k every rehearsal probe would be sub-knee and "do both stacks cross?" answered *no* by construction, R1-A3's failure transplanted onto the ladder. **`target_count` was NOT reduced** as the P1 plan proposed: it is the working-set dial, so cutting it re-introduces exactly the residency `seed_scale="full"` prevents — and it was not a budget item then (measured at the 1M default: **67.7 s** of generation, 45 MB of targets and **1.93 GB** of request bodies **per slot**, against a 2.5-3 h tier). Matching `full` means rehearsal pays full's cost in full, which is the point: discovering that at hour 30 of a measured run is what this tier exists to prevent. **W20 renamed the dial `body_pool_requests` and re-measured it** — the numbers in this row describe the one-file-per-slot generator it replaced. |
| W14 (CLI) | **`--scale` is validated, and a resume adopts its own run's pin.** `run-all --scale rehearsal` raised an **uncaught `ValueError`** — `make_context` sits outside every `try/except PhaseError` — so a typo was a traceback. It is a `typer.BadParameter` now. More usefully, `--scale` omitted on an existing run adopts the ledger's pinned scale: the ledger pins it and `load_or_create` *refuses* a mismatch, so the old fixed defaults made `ladder --run-id <a full run>` fail with a RuntimeError naming a scale nobody typed. Run ids now lead with the scale (`rehearsal-<stamp>-<host>`), which is what makes `.gitignore`'s `results/smoke-*/` rule match at all — round 1 named runs timestamp-first, so that rule matched nothing and every functional rehearsal's artifacts were staged for commit beside measured ones. `results/rehearsal-*/` added beside it. |
| W15 | **Typed `StallTest`, and the phantom key it was hiding.** `co_validation.stall_test` was a raw `dict`, and the runner read `stall_test["timeout_s"]` from it for the **vegeta attack timeout** — a key that block has never defined, and not a property of a stall test in the first place. The read silently returned its `5.0` fallback while looking spec-controlled. The attack timeout is now declared where the attack is (`ladder.attack_timeout_seconds: 5.0`, the value that was always actually in force) and the typed model makes the phantom read an `AttributeError`. Second half of the same defect: `smoke.py` performed the stall check with a literal `sleep(1.0)` and a literal `"pause"` while the spec declared `freeze_seconds` and `method` one directory away — so a spec edit changed the dry-run plan text and nothing else. The executed step is now `run_stall_check()`, reading both from spec, and an unimplemented `method` **raises before touching the SUT** rather than silently performing a docker pause and reporting `co_stall ok`. |
| W15 (postures) | **`WarmupCfg.gates_for(cell_id)` — B3's precondition, wired.** Per-posture gate overrides **merge** over the global set rather than replacing it, so a posture cannot silently stop enforcing a gate added globally after it was written. `live_runner._warmup` now takes the cell id and resolves the set once at the boundary, so evaluation, the under-warmed verdict and the per-probe unimplemented list all read the same set. `spec/slo.yaml` declares **no** override yet, deliberately: a posture exemption is a *measured* finding (R1-A3 burned ~30 h on a posture whose GC settings made `gc_steady` unsatisfiable by construction), and the resolver lands first so that finding has a pre-registered home instead of a disclosure note. |
| — | **Defect the run-id prefix introduced, caught and fixed in the same session.** `Ledger.latest_unfinished()` and `Ledger.latest()` both sorted candidates by **run_id**, which was chronological only while ids led with a fixed-width timestamp. Leading with the scale makes that sort order by TIER — `full-…` < `rehearsal-…` < `smoke-…` — so a `full` run that crashed twenty hours in would lose `--resume` to *any* older unfinished smoke run, and because `--resume` now adopts the ledger's pinned scale it would have silently continued at smoke numerics. Both now sort on the ledger's own `created_utc`. `Ledger.latest()` matters just as much: it backs `ledgerbench status`, the first command an operator runs after a crash, so naming the wrong run there is how the wrong run gets resumed. |
| — | **Defect the coarse ladder introduced, found by the adversarial review and fixed.** `ladder_step_factor` was never a mixed-ladder-only dial: `measure_pg_create_knee` stepped the capacity gate's create-only ladder by the same field, so rehearsal's 2.0 silently coarsened the **gate** ladder from 500,600,…,3715 to 500,1000,2000,4000. Verified by running the real `pg_knee_from_rungs`/`evaluate_gate`/`realize_levers`: that pins rehearsal's measured PG knee to exactly 2000 rps for every true create ceiling in [2000, 4000) — the band containing the gate's own threshold (projected 4000 × db_touch 0.54 × headroom 1.3 = **2808**). In that band rehearsal fires all three pre-registered levers, cannot realize `cache_hit_rate` (spec pins no TTL), and **aborts before a single probe on a host where `full` passes cleanly** — or, given a TTL, passes only after re-coring PG 3→4 from vegeta, i.e. rehearsing a different workload on a different topology than the run it exists to de-risk. A rehearsal that fails where the full run succeeds is the inversion of the tier. The gate now carries its own `capacity_gate_step_factor`, which rehearsal pins to the spec's 1.2; coarsening it never saved time anyway, because the gate ladder breaks at the ceiling rather than running to its rung cap. Two follow-ons: the `capacity_gate_rungs=16` comment had reasoned from full's 1.2 step and was inert at 2.0 (it is now true — 16 rungs reach 7,705 against full's 3,716, and a ladder that runs out records a LOWER BOUND, never a knee), and the rung sequence moved out of the docker-only method into `phases.gate_ladder_rates`, because an inline `rate *= …` there was unreachable by every test in the suite, which is how the shared dial hid. |
| — | **A posture-waived warmup gate now leaves a trace.** Resolving `gates_for(cell_id)` makes a waived gate vanish from `evaluate()` — and a gate that silently stops being checked is R1-B1 exactly (round 1 declared six warmup gates, evaluated four, recorded nothing about the other two). `posture_waived_gate` mirrors the existing `not_implemented_gate` record: `passed=False` so a reader scanning for `passed: true` never finds one, `enforced=False` so it cannot flag the probe, and the cell id and the reason in `detail`. |
| — | **Every new test was mutation-checked.** Thirteen planted defects across W14/W15 each failed exactly the test written for it. **Three initially survived, and every one was hiding behind a docker-only code path** — the stall freeze, the stall verb, and the gate ladder's step. That is the pattern worth carrying forward: an inline value inside a method no test can reach is invisible to mutation testing and therefore to review. Each was fixed by extracting the unreachable logic (`smoke.run_stall_check`, `phases.gate_ladder_rates`) rather than by asserting harder around it. |
| W16 | **Multi-cgroup sampling, window deltas, and the DB-CPU-per-request column (closes B5).** One `MultiSampler` thread reads all six cgroups per 50 ms tick, where round 1 ran one thread per container and every one self-pinned to the *same* housekeeping core — six targets would have been six threads contending one core. Measured on this box: cpu.stat + memory.stat for five cgroups costs **0.21 ms per tick**, a 0.4% duty cycle, i.e. the whole sampled set costs less than the scheduling of the threads it replaces. `cpu.stat` is parsed once per target per tick (round 1 opened and re-parsed it three times, once per field). Sampled: the SUT, postgres, redis, vegeta, **and both container-runtime units** — measured here, a container lands in `/system.slice/docker-<id>.scope` (cgroup driver `systemd`), so the daemon rows do not double-count the SUT, and the containerd-**shim** is charged to `containerd.service`, which means sampling `docker.service` alone under-counts the runtime by exactly the process the SUT's json-file stdout is relayed through. `WindowResult.container` → `label`, because a systemd unit is not a container and that field is what every aggregation keys by. |
| W16 (deltas) | **Every cumulative counter is now a measured-window delta, and this was mandatory, not cosmetic.** `_finalize_manifest` **sums** `nr_throttled`/`throttled_usec` across windows and the OOM abort tests `oom_kill` — all three are kernel LIFETIME counters read at window end. That was sound only because the SUT is force-recreated per probe; postgres/redis/vegeta/the daemons persist for the whole run, so adding them unchanged would have multiplied one lifetime total by the window count and published a measured pathology that never happened. `oom_kill` was worse: an absolute test against a persistent container **latches** — every probe after the first aborts on history, `--resume` re-runs it against the same latched counter, and a healthy run can never finish. Both edges are read now and the artifacts carry the increment. **It is a strict improvement for the SUT too:** the sampler starts after warmup *and* after the head-of-window discard, so a delta is the measured window alone where the end-only read covered the container's whole life — which retires the caveat `THROTTLE_SOURCE` and `ThrottleCounters.__doc__` both had to disclose. Those two strings and their tests are **inverted deliberately**; leaving lifetime prose beside delta numbers would have understated the measurement exactly as round 1 overstated its own. `memory.peak` is the one thing that could NOT become a delta (a difference of two high-water marks is not a window figure): both edges are recorded under names that say so, and `basis` carries it per manifest row. It *is* resettable on kernel 7.1 and resetting is refused — it needs root, and clearing a shared container's peak mid-run destroys the run-level high-water every other window reports. `memory.current` is sampled instead, because for PostgreSQL `memory.stat anon` excludes `shared_buffers` and would publish a small number under a plausible name. |
| W16 (naming) | **Departure from the plan text, and the reason it had to be one.** `docs/p1-plan.md` says to read `cpu_mem_postgres.summary.json` at the **blockdir** level, before the per-slot loop. That is wrong and would have been invisible: `_artifact_dir` takes no slot component, so a block directory is shared by BOTH its probes, which run sequentially. One PG file per block is written by the first probe and **overwritten by the second**, and a blockdir-level read then stamps one slot's DB cost onto both — two identical DB columns, which read as a *finding*, not as a bug. Co-sampled windows are `cpu_mem_<slot>.<label>.summary.json`, keyed per (slot, block); the collision-freedom rests on `registry.Slot` refusing a `.` in a slot id, and `test_shared_summary_names_cannot_collide_with_a_slot_summary` is what pins that dependency. (An adversarial scout reached the same conclusion independently.) |
| W16 (DB column) | **The DB-CPU-per-request column, and the report row re-keyed before it was wired.** `analyze` reads the PostgreSQL window beside each slot's own and publishes `db_cpu_ms_per_req`, `total_cpu_ms_per_req`, the DB share, and subject:reference ratios for both — under `_cpu_ms_per_req`, the module's ONE declared aggregation rule, generalised by a `key=` parameter rather than duplicated (a second helper is how the report's "THE AGGREGATION RULE" paragraph becomes false for one row). Numerator and denominator are taken over the SAME subset: a full request denominator against a partial DB numerator understates the cost by exactly the fraction of missing windows and looks entirely plausible. The DB read is deliberately NOT in `_window_rows`' drop guard — gating on it would delete the whole resource section for any run without a PG sampler rather than one column of it. `analysis/report.py`'s `db_cpu_attribution_table` was written, never called, and **per-endpoint**; a cgroup `cpu.stat` delta is a whole-container number, so it is re-keyed to `(cell, offered rate, slot)` — the finest key the measurement supports — and wired into the CPU-vs-rate section, beside the per-rung curve it decomposes. vegeta and both daemons go to a separate `manifest.load_generator_overhead` with `excluded_from_per_request_cost: true`: measured, published for single-host accounting, never divided by a request count. |
| W16 (manifest v4) | **`MANIFEST_SCHEMA_VERSION` 3 → 4.** Forced four ways: `container` → `label` on three `extra="forbid"` models; the semantics of `nr_throttled` **invert under an unchanged field name** (a v3 row is a sum of lifetime totals, a v4 row a sum of window increments — different quantities, and the version is the only mechanism that stops them being compared); new fields (`role`, `persistent`, `basis`, `mem_current_p99_bytes`, `mem_peak_start_bytes`); and a new section plus its `AggregateProvenance` field. `results/` is empty today, so this had to land in P1 or never: `load()` refuses a mismatched version by name, so after the first measured run a bump breaks every resume. Two gaps closed while here — a manifest **version-refusal test** (the ledger has had one since W5; the manifest never got the matching test), and a frozen row-shape test, because nothing can detect a *forgotten* bump from the constant alone: editing that list is the moment to ask whether the version moved with it. |
| W16 (defects) | **Six live defects found and fixed.** (1) `_finalize_manifest` keyed rows on `str(doc.get("container", path.stem))` — three defects at once: `dict.get` does not apply its default to an EXPLICIT null, so a systemd unit would have published a row named `"None"`; `Path.stem` of `cpu_mem_java.summary.json` is the malformed `cpu_mem_java.summary`; and two slots sharing a compose service would have merged into one row. (2) `fault_windows` incremented only inside `if maj is not None`, so a run that read `pgfault` but not `pgmajfault` published a real minor-fault sum beside `windows: 0` and `source: not_measured` — a measured number wearing a not-measured label. (3) `live_runner` hard-coded `f"ledgerline-{svc}-1"` against `compose.container_name`'s own warning: under a non-default `-p` it resolves NO cgroup path and the headline resource numbers silently become empty. (4) `throttle_clean`/`oom_clean` were `all()` over an unfiltered list, and `all()` of an empty set is True — a run with no readable counters published "clean" as though it had checked; the verdicts now cover only windows that carry the counter, and `n_windows_without_counters` reports the rest. (5) `capacity_gate.RungResult.pg_cpu_delta_usec` was a declared field no code could ever fill, documented as null "permanently"; the create-only ladder now samples PG per rung, which is the input W26's utilization re-gate needs and the projection rule cannot supply. (6) The page-fault delta was read through a SECOND cgroup-path resolution (`docker inspect`) at each window boundary with a private copy of the flat-key parser — deleted; the sampler already reads memory.stat at both edges, so it is two fewer subprocesses per probe and postgres/redis get a fault delta too. |
| W16 (spec) | **`spec/db-access.md`: a standing *negative* declaration struck, and the driver asymmetry disclosed.** The file said the DB-CPU column "was descoped 2026-08-13" — the mirror image of round 1's failure, a permanent claim in the frozen tree that a measurement will not exist, which PROGRESS reverses and P1 schedules. Replaced with what the column measures, plus a new closing section stating its four structural limits: whole-container (autovacuum, checkpointer, WAL writer are in it), NOT per endpoint (that needs `pg_stat_statements`), driver-sensitive, and null-never-zero when absent. The driver note lands where its dial is declared: Npgsql resets on essentially every pooled CHECKOUT while PgJDBC/HikariCP's chatter is per CONNECTION and therefore lands in warmup, so D4 moves this cost in the same direction it moves the prepare behaviour — the two are not independent. Phrased as an observed **absence** on the PgJDBC side, not a positive measurement, with P5's wire ledger named as where it is confirmed. **No `spec/slo.yaml` key was added**: W16 delivers a measured column, never a gate. |
| W16 (tests) | **36 planted defects, every one caught by the test written for it, no survivors.** The suite had **no fake-cgroup idiom at all** — nothing monkeypatched `CGROUP_ROOT`, `IS_LINUX` or `container_pid`, and the one sampler test was `skipif` on Linux, i.e. a permanent skip on the measurement box. `SampleTarget.for_path` exists so a fixture cgroup tree under `tmp_path` is sampled through exactly the code a real run uses, and `WindowResult.summary_dict()` moved out of `live_runner` for the same reason: the three mutations that had survived earlier were all logic behind a docker-only path. Two more extractions on that principle: `live_runner.oom_labels` and `shared_summary_name`. The one test that mattered most was the one nothing covered before — every other test on that seam hand-writes the summary JSON, so a rename on the WRITER side would have left both halves passing while the real pipeline produced a run with no resource rows at all; `test_the_sampler_writes_exactly_what_the_manifest_and_analyze_read` starts at the sampler and ends at both readers. |
| W17 | **`/runtime-stats` flushed per window, and the measured window sampled at all (closes B7, unblocks B2).** This was never "flush what we poll": round 1's poller ran during **warmup only**, so the measured window was not sampled, and every snapshot was discarded afterwards — no measured window in its artifacts has GC evidence of any kind. A second poller now runs across the measured attack at the same boundaries as the cgroup sampler, self-pinned to the housekeeping core (the poller had no pinning; `cgroups.MultiSampler` is the pattern). Both edges are polled **explicitly**, before `start()` and after `stop()`, because every counter on this endpoint is a PROCESS-LIFETIME total against a container that is force-recreated per probe: without a start snapshot there is no window delta, only a total the process happens to have reached. Same argument as W16's cgroup counters, one layer up. Artifacts per probe: `<slot>.runtime_stats.summary.json` (both edges verbatim, every cumulative counter as end-minus-start, every level as start/end/in-window-max) and `<slot>.runtime_stats.jsonl` (one object per poll, the frozen stats object verbatim with `extra` preserved — line-per-poll rather than one array, so a kill mid-window leaves k valid records instead of one truncated document). The warmup window gets the same pair. |
| W17 (evidence) | **`n_polls` / `n_poll_errors`, and the series B2 cannot be calibrated without.** The poll loop swallowed every error with a bare `except: pass`, so an all-failed window, an unpolled window and a window whose counters were genuinely zero were the same artifact — nothing. Errors are counted with the last reason kept, and a zero-poll window writes `source: not_measured` with **nulls, never zeros**; the summary is written even when empty, because a missing file and a file saying "not measured" are different claims and only the second survives someone grepping the tree. The error accounting lives in `poll_once`, extracted so it is reachable with no thread and no SUT — the W14/W15 lesson applied. Separately: `state.p99_series`/`p999_series` are built every warmup segment, fed straight to the flatness gates, and **were never written**, so the gate that rejected two thirds of round 1's windows was tuned against a series that exists in no artifact. It is flushed as `<slot>.warmup_latency_per_second.csv`, carrying the **segment index** — each segment's second index restarts at 0, so a concatenation without it has a time axis that goes backwards. |
| W17 (disclosure) | **Polling inside the measured window is a cost with no denominator, and the bound is measured.** The polls' SUT-side CPU lands in the cgroup delta, while `/runtime-stats` is not one of the frozen `endpoint_calls` routes and is not in vegeta's request count either — so per-request CPU is overstated by exactly that. Each poll records its client-side round trip, an upper bound on the handler's own time, and `poll_cost` publishes the total; the disclosure is therefore derived from the artifact rather than being a constant asserted in prose. Boundary-only polling would avoid it and lose the within-window heap trace that decides D6, so the trade is stated rather than taken silently. `analyze`'s "descoped, GC counts unavailable" bullet is replaced by this. |
| W17 (defect) | **A W27 knock-on found by a unit test rather than by a live JVM.** `GcCollections.total()` is `gen0 + gen1 + gen2` and raises on a null. W27 must report `gc.collections.gen2` as **null** for the JVM (G1's old-gen bean counts Full GC only and reads 0 where ZGC reads 3 on identical work), so the first Java probe would have crashed the delta path. The window total is summed from the per-generation deltas that exist, with `gc_collections_generations_summed` naming which ones — the number never silently stands for all three. `total()` itself is left alone: it is `spec/runtime-stats.md`'s shape and W27 owns it, but the warmup cadence series is built from it and that is now a known, written-down dependency rather than a surprise. |
| W17 (tests) | **13 planted defects, all caught, and one degenerate assertion found by the mutation run itself.** `window_offset_s` was asserted against snapshots at monotonic 0.0 and 1.0, where an absolute clock reading and an offset are indistinguishable — the mutation that returned the raw clock survived. Fixed by using a realistic monotonic epoch, which is the point of mutation-testing a test rather than reading it. `.gitignore` gains `results/**/*.jsonl` beside the `.csv` rule: the traces are regenerable detail, the `.summary.json` roll-ups are committed. |

| W18 | **Host-state read-back (closes B2), and the plan's own placement was wrong.** `docs/p1-plan.md` says to invoke it from `run_doctor_phase` "which runs at every run start and every resume — exactly B6's cadence". It does, and it runs **before `run_host_setup_phase`**. A Tier-1 read-back there aborts **every** run on a freshly-booted box: `host-restore.sh` deletes the state file, a reboot resets the live knobs, and measured on this box untuned, `cpufreq/boost` reads `1`, dockerd's `Cpus_allowed_list` reads `0-31` and tuned/irqbalance are both active. It is therefore its own `host_readback` phase between setup and seed, with its own ledger record. **Settings are separated from outcomes as a typed field, and `evaluate()` refuses to let an OUTCOME gate anything** — a governor is a setting we wrote and can compare; a `scaling_cur_freq` sample is evidence it bit and is load-dependent. Tier-1 is allowed *only* where `infra/host-setup.sh` demonstrably sets the value: measured on this box, the plan's Tier-1 IRQ rule is unsatisfiable (nvme managed interrupts sit on cores 0,1,2,3,5), dockerd/containerd/journald read `0-31` with no setter in the script at all, and tuned/irqbalance read `enabled` while stopped — so four rows are Tier-2 **by the governing rule**, not to dodge it. Two defects the read-back itself surfaced: `sudo -n` **strips the environment**, so the script used its own defaults while the read-back compared against the operator's (now forwarded explicitly as `sudo -n env VAR=… bash …`, for restore as well as setup, with a test that parses the script's own `${VAR:-default}` lines and asserts they equal the read-back's — the docstring promised that and did not deliver it); and `SYSTEMD_DROPIN` was a hardcoded literal in the script but env-derived in the read-back, i.e. an **env-settable off switch for a Tier-1 gate**. |
| W18 (swap) | **The anon-RSS headline can now be *shown* swap-free rather than asserted.** This box runs 8 GiB of **active zram** swap, and docker's `mem_limit` with no `memswap_limit` grants memory-swap = 2× memory — so the SUT held 12 GiB of swap allowance against a *compressing* device. Anon pages swapped out during a measured window leave cgroup v2 `memory.stat anon`, and steady-state memory per request is a headline number, so the understatement would have been silent. `memswap_limit` is now pinned equal to `mem_limit` on all five services (a container that would have swapped is OOM-killed instead, which the harness already detects loudly via the `oom_kill` delta; at a ~212 MB live set against a 12 GiB limit it is not a realistic risk). Both `memory.swap.current` **and** `memory.swap.peak` are read at both window edges — the point samples alone read 0/0 for a window that swapped 4 GiB at t=30 s and faulted it back by t=110 s, which is exactly the case the field exists to catch — and the verdict spans the co-sampled PostgreSQL cgroup too, because the DB-CPU-per-request column and `db_cpu_ratio` are published off it. |
| W18 (fail-safe) | **`throttle_clean`/`oom_clean` were about to regress from fail-safe to fail-open.** Round 1's `all(r['throttled'] == 0 …)` failed safe *by accident* (`None == 0` is False). Filtering to readable windows inverted that, and the comment three lines above stated the guard the code no longer had. All three verdicts are now `bool(rows) and all(...)`, and the shortfall count — computed, stored in JSON and rendered **nowhere** — is printed. Reachable in production: a kernel without `CONFIG_MEMCG_SWAP` has no `memory.swap.current` at all, so every window reads null and the run would have published `swap_clean: true`. |
| W19 | **One checksum file that verifies (closes B4) — and the scope is now git's answer, not ours.** The append-mode writer is gone (six paths per probe, mode `"a"`, with `reset_orphan_blocks` and `_retry_excluded_rungs` making re-runs routine — mechanically how round 1 got 108 double-hashed paths). The run file is a **map**: recomputed from scratch, part-wise POSIX sort, `as_posix()`, atomic write, duplicate keys and malformed lines **refused** (round 1's file passed our own verifier while `sha256sum -c` on the same bytes exited 1), roll-ups covered, and `ledgerbench verify <run_id>` with a non-zero exit. The scope went through three implementations before it was right: a hand-written gitignore translator was wrong twice, always silently and always in the direction where `verify` stayed green while `sha256sum -c` in a fresh clone failed. `git check-ignore` was wrong too — with `-v` its exit code means "I printed a record", not "this is ignored", so a **negated** rule reads as an exclusion and a published file leaves the map. The scope is now one call: `git ls-files --cached --others --exclude-standard -z` from the run directory, which **is** the publication scope by definition and handles negations, nested `.gitignore`, `.git/info/exclude`, `core.excludesFile`, directory-only rules and dangling symlinks with no parsing on our side. If git cannot answer, it **raises** — a checksum file whose scope could not be determined is what round 1 shipped. `verify` also gained the reverse term: a path that is listed but has since left the scope, which is green under `verify` and `FAILED open or read` under `sha256sum -c` in the clone. |
| — | **Four adversarial rounds, and the same defect class in every one: a claim broader than the code.** Each round ran its own mutations rather than trusting the fix report, which is how round 2 caught a pass claiming three mutations it had not executed. The class that kept recurring is a *rendered report sentence* whose scope is fixed text rather than derived from what the run recorded — "no sampled cgroup held a swapped byte … or in its lifetime high-water mark" printed on a kernel with no `memory.swap.peak`; "including redis, vegeta and the container-runtime units" printed for a manifest holding neither; a seeded-corpus figure taken from `spec/workload.yaml`'s full-scale range and printed **1000× wrong** on a smoke/rehearsal run, in the sentence whose whole job is quantifying cache residency. Comments and docstrings were already covered by the house rule; **rendered output was not, and it is what a reader actually reads.** |

| W20 | **Per-probe target draw — R1-A7 retired, and the defect was larger than round 1 disclosed.** All three attacks of a probe passed `targets_in_container="targets.txt"`, and vegeta reads a targets file **from the top on every invocation**. So the measured window consumed the first `rate x duration` targets and the warmup that ran immediately before it — after a Redis `FLUSHALL` — consumed a **prefix of that same sequence**: it did not merely leave a warm cache, it populated the cache with exactly the invoice ids the measured window was about to request. Round 1 disclosed only the within-window wrap. Generation is now **split**: `generate_body_pool` writes the create/quote bodies ONCE per run (bodies carry no URL, so one pool serves both slots and both halves of an RCB block send byte-identical payloads), and `draw_targets` streams ONE targets file per ATTACK from its own PCG64 stream. `util.pcg64.stream_for(seed, run_id, cell_id, stage, rate, block, phase)` derives the increment `PCG64.__init__` has always accepted and nothing ever passed. Two label rules carry the whole fix: **`slot` is excluded** (both halves of a block must face the identical sequence — that IS the pairing contract, and the base URL is the only permitted difference), and **`phase` is included, per warmup SEGMENT** — segments are separate vegeta invocations against one file, so a single warmup draw would have segment 2 replay segment 1 and leave warmup measuring a cache it had just filled itself. Sizing is the other half and is not optional: `count = ceil(rate x duration x 1.02)`, because a 120 s window at 13,312 rps issues 1.6M requests and wraps any file sized from the old 1M dial. |
| W20 (cost) | **Measured before declared, because "B8 becomes the longest phase of the run" was a named risk.** On this box at the 1M dial: Zipf tables **0.88 s** built once per run (they were rebuilt inside every `generate_targets` call — an N-entry weights list plus an N-entry CDF for N=5,000,000, and a per-probe draw would have repeated that 600+ times); the body pool **~67 s** (150,000 create + 200,000 quote bodies; 1.9 GB logical, ~2.9 GB on disk at 4 KiB blocks), once per RUN rather than round 1's once per (slot, cell), which also drops the `docker cp` from one ~1.9 GB copy per (slot, cell) — four for the two headline cells, ten with the variants selected — to one; the draw itself **~450,000 targets/s**. A whole probe's draws — 10 warmup segments + discard + measured — cost **0.57 s at 500 rps, 1.63 s at 3,715 and 4.88 s at 13,312**, against a probe of ~450 s wall clock: **1.08% at the worst rung**. **Those are 120 s LADDER windows only, and the W20 correction extends the scoping**: a soak probe runs 600 s at the confirm rate, so its measured draw alone is 6.12M targets / **410.6 MB / 13.76 s at 10,000 rps** (8.15M / 546.5 MB / 18.29 s at 13,312), and the whole soak probe is **14.83 s and 443.8 MB** at 10,000 rps — against a soak probe of ~930 s, 1.59%. **Every second in this paragraph is GENERATION ONLY, and the `docker cp` is on top of all of them and is not measured here** — the first version of this row mixed generation-only percentages with a total that `probe_draw_cost` had already been widened to include the ship. It is also re-measured at the warmup rate the harness actually offers (618 rps, not the 371 the first pass used), which is what moved the warmup term from 113,530 to 189,110 targets and every ladder total with it. The parameters: full scale, `base_url=http://sut-dotnet:8080`, targets written to the filesystem `results/` lives on (nvme), 10 warmup segments, the 30 s discard and the window at the stated rate. `ProbeRecord.draw_seconds` and `working_set.draw_seconds_per_probe` record generation PLUS ship per probe, so the run's own artifacts supersede this paragraph the moment a run exists. Still not the longest phase, but an order of magnitude above the ladder figure this row published, and the number a reader would have extrapolated from it is wrong. Every probe records its own total in `ProbeRecord.draw_seconds` and in `working_set.draw_seconds_per_probe`, so this stops being an assumption the moment a real run exists. Two allocations went with it: the draw **streams** to an open handle (round 1 held ~1.55M strings before a byte reached disk) and the bodies are **not fsynced** — `paths.write_bytes_atomic` does mkstemp+write+flush+fsync+replace, ~350,000 times per run, for files that are gitignored, uncoverable by the checksum map and a pure function of the seed. `util.paths` grows two NAMED scratch writers and the AST guard that bans raw writes exempts those two by name, so the ban still holds over every other module. |
| W20 (surgery) | **What was deleted, and one field that was not added.** `replay_period_seconds` is gone with `_record_replay_period` and both its call sites: nothing replays, and the field would be a fabricated disclosure. `working_set_from_targets` is gone with it — it recovered the create path's customer ids by re-reading every referenced body file, which the draw already holds. `WorkingSet` stops describing a file (`targets_file`, `target_count` and the three whole-file distinct-id counts deleted) and becomes the per-probe SPREAD of requests drawn, ids named and seconds paid, accumulated from a `<slot>.target_draw.json` per probe rather than an in-process counter — a resume is a new process, and a counter that restarts is short on exactly the runs that crashed. Manifest schema 6 → 7. **`distinct_invoice_ids_union` was NOT added** though the design lists it: the only resume-safe way to accumulate it is to hold every id the run drew, so the field would have been silently short after a crash — the per-probe spread is what the artifacts can support, and it is also what the residency question asks. `probes_recorded` was dropped for the same class of reason: every spread carries its own `n` over the same sample, and a separate count can disagree with the numbers printed beside it. `analyze`'s residency bullet is rewritten from these fields — TTL against the measured WINDOW, since there is no replay period — and it states that the run publishes no union rather than claiming the windows are disjoint, which they are not. |
| W20 (ledger v4) | **The dial changed MEANING, so the pin and the env var changed with it.** `LEDGERBENCH_TARGET_COUNT` sized the one targets file a run replayed; the same integer now sizes the shared body pool, which is a different quantity with a different effect on the workload. `Ledger.target_count` → `body_pool_requests` and `LEDGER_SCHEMA_VERSION` 3 → 4: nothing about the field's TYPE forces a refusal, so without the bump a v3 ledger would resume into this build with 1,000,000 read as a pool floor — the exact mid-run working-set swap the pinning was added to prevent. The old env var is **refused with an error naming its replacement**, not ignored: an operator whose shell still exports it would otherwise get the default while believing they had set the working set. `ProbeRecord` gains `draw_inc` (hex) and `draw_seconds`, so `ledger.json` alone can regenerate the exact sequence a probe faced. |
| W20 (the cost this design PAYS) | **Drawing create bodies from a pool changes the create path's customer_id distribution**, from a fresh Zipf draw per request to a uniform draw over a Zipf-sampled pool: identical on the head, thinner in the tail. The exact-semantics fallback (regenerate create bodies per probe) was rejected on measurement — 15% of a 1.6M-target probe is ~245,000 bodies and ~490 MB written per probe, which is how B8 becomes the longest phase. It is **disclosed where it is derived**: `working_set.body_pool_distinct_customer_ids` is COUNTED from the pool on disk (at alpha=1.0 a 150,000-body pool holds far fewer than 150,000 distinct customers, and the gap IS the disclosure), and the report renders it as a sentence beside the numbers. It is deliberately **not** added to `docs/FAIRNESS.md`, which W31 rewrites from a skeleton and which today is wholesale round-1 prose; W31 lifts it, exactly as G3 decided for the P3 dials table. The READ path — which is what R1-A7 is about — is URL-only and unaffected either way. |
| W20 (tests) | **Three passing tests pinned the DEFECT and were inverted, not patched.** `test_targets_file_deterministic` asserted "one seed -> one byte-identical targets file", which is the defect stated as a contract; it becomes `test_same_probe_identity_reproduces_byte_identically` (the narrower guarantee a resume actually needs) plus `test_probe_draw_differs_across_blocks_and_phases` over eight label variants (run, cell, stage, rate, block and three phases). `test_phases.py`'s `ws.replay_period_seconds["100"] == 2.0` against a 300 s TTL was R1-A7 in one number; it becomes the per-probe spread. Three `test_manifest.py` tests went with `working_set_from_targets`, and the frozen-row-shape guard was extended to name the five deleted `WorkingSet` fields so restoring any of them has to argue with a list. The direct regression test is `test_separate_streams_remove_the_prefix_prewarm_not_the_zipf_head`, and the old behaviour is asserted beside the new one for contrast: under a prefix, containment is exactly 1.0 and must not be. **It was named `test_warmup_draw_does_not_prewarm_the_measured_window` and the W20 correction renamed it** — it asserted `0.0 < containment < 0.8` while its NAME stated an absolute that is false, and that absolute propagated verbatim into `analyze._residency_vs_ttl` and into `working_set.note` of every manifest. **35 planted mutations, every one caught by the test written for it; FOUR initially survived** — a draw that stopped accounting the customer ids its create bodies carried (nothing checked the body half of the working set); a TTL comparison taken against the SHORTEST measured window rather than the longest (every synthetic draw in the test used one window length); and **both halves of the warmup segment draw**, which is the W16 lesson repeating exactly — the loop is reachable only through docker and every gate test stubs the draw out, so a phase label that dropped the segment index and a count sized from the probe's measured rate instead of the warmup rate were both invisible. Fixed the same way it was fixed then: `warmup_segment_draw` is extracted and asserted, not asserted harder around an unreachable loop. |
| W20 (correction) | **The central claim W20 rendered was FALSE, and it is now a measured number.** `analyze._residency_vs_ttl` printed, in every tier, "the warmup, the head-of-window discard and the measured attack each draw a separate stream, so nothing before a window populates the cache with the ids that window is about to request", and `phases._record_working_set` serialised the same absolute into `working_set.note` of every manifest. **Re-measured after the first table was taken at the wrong warmup rate — see the correction row below.** Measured on this box at full scale, request-weighted, at the warmup rate the harness actually offers (`live_runner._warmup_rate` = **618 rps** for both measured tiers), against the ten warmup segments plus the 30 s discard: **60.5-60.8% of a measured window's invoice-id read requests at 500 rps, 62.6% at 3,715 and 66.9% at 13,312** name an id an earlier attack of the same probe already drew (two independent label sets). Against the WARMUP ALONE it is 59.6-60.3% at every one of those rungs — the term is the Zipf head and the head does not move with the rate — and a warmup that settles at the two-segment minimum still contains 49.6-50.6% (52.0% with the discard at 500 rps, 65.1% at 13,312, because the discard is drawn at the PROBE's rate and grows with the rung while the warmup's draw does not). The published envelope is **52-67%**, over rung and warmup length. The mechanism is right and unchanged: separate PCG64 streams remove the SEQUENCE-correlated pre-warm — R1-A7, where all three attacks replayed one file's prefix and containment was 1.0 by construction — but two independent Zipf draws over one keyspace share the head **by construction**, which `docs/p1-plan.md` 5.1 already says in as many words about `hot_set_touched`. So both claims are narrowed to what the draw achieves and the residual is PUBLISHED: `vegeta.prewarm_containment` scores each probe request-weighted (`DrawnTargets` carries a `Counter` of invoice-id reads, not a set — the same three draws score **27.4%, 16.9% and 14.9%** set-wise, understating the residency 2.2-4.5x, because the head is a few ids and most of the traffic; note the set-wise figure FALLS with the rung while the real one rises, since the measured window's distinct-id count grows with the rate and the pre-warmed set does not), `<slot>.target_draw.json` records it with its scope, `working_set.prewarm_containment_per_probe` is its spread, and the report renders it beside the constructed 0.80 target hit rate and the realized Redis hit rate. Manifest schema **7 → 8**. |
| W20 (correction, ordering + ship) | **Three defects around the same draw.** (1) The measured draw and its `docker cp` sat BETWEEN the head-of-window discard and the measured window — the gap the discard exists to keep transient-free — and the cost is rate-correlated: generation alone is 0.15 s for a 4.0 MB file at 500 rps against 3.67 s for 106.1 MB at 13,312, with the copy on top. The measured draw is now generated and shipped BEFORE the discard attack; the discard's own draw stays where it is, because the attack that follows it absorbs whatever transient it makes. (2) The per-probe ship was `check=False` against a FIXED container filename on a volume that persists across probes, with the return value discarded — a failed `docker cp` left the PREVIOUS probe's file for vegeta to attack, and a file drawn for a lower rung WRAPS inside the measured window, R1-A7 reintroduced with no error anywhere. The copy is checked and raises, and `probe_targets_name` puts the probe identity in the filename so a stale file cannot be silently consumed even if the check were removed. (3) The draw-cost accumulation had NO test — deleting any of the three `draw_seconds +=` sites left the full suite green — and it excluded the ship. `probe_draw_cost` is now a pure function of (warmup total, each draw's generation, each draw's ship), extracted for the same reason `warmup_segment_draw` was: `run_probe` is reachable only through docker. |
| W20 (residue) | **The published containment table was measured at a warmup rate the harness does not use, and the warmup half of the number had no test at all.** `live_runner._warmup_rate` returns **618 rps** for both measured tiers (worst-case endpoint `healthz` at a 2% mix share needs 100,000 requests; `int(100000/(180*0.9))+1`) — and the table this file published reproduced exactly at **371**, the value `_warmup_rate` and its own test name as the OLD, REJECTED one. Re-measured at 618 the whole table moves UP, because the pre-warm draw is ten segments of `probe_draw_count(618, 30)` = **189,110 targets** against 371's 113,530: the envelope is **52-67%**, not 46-66%, and the set-wise understatement is **2.2-4.5x**, not 2.5-4.8x. Two more corrections fell out of re-deriving it: **3,583 rps is not a ladder rung at all** (full's ladder is 500 x 1.2^k — rung 11 is 3,715, rung 18 is 13,312), and the cost row's seconds are **generation only** while `probe_draw_cost` had already been widened to include the `docker cp`, so its percentages were computed from one quantity and labelled with another. The rate is now PINNED by a test (`test_the_measured_tiers_warm_at_the_rate_the_published_figures_were_taken_at`) rather than left to be re-derived by the next reader: a `spec/slo.yaml per_endpoint_min_calls` edit moves it, and the published figures have to break. |
| W20 (residue, tests) | **Both halves of the containment number were untestable, and both mutations survived the FULL suite.** Deleting `draw_ids \|= set(w_drawn.invoice_ids)` in `_warmup`, and replacing `prewarmed_ids = set(warm.target_draw_invoice_ids)` in `run_probe` with `set()`, each left every test green while silently dropping the WARMUP term — most of the published quantity — from what the manifest and the report render. Same cause as the three that survived earlier: both live in code reachable only through docker, and the fix is the same one, extraction rather than asserting harder. `live_runner.warmup_draws` folds the segment draws into (incs, id union, seconds) and `live_runner.prewarmed_ids` builds `PREWARM_SCOPE` as a function; `_fake_draw` in the suite now varies its inc and its ids with the SEGMENT, so a fold that collapses or drops one is visible. The regression test's own contrast case was **scored with the arguments reversed** — `prewarm_containment(prefix, whole)` asserts that a set contains its own subset, which is 1.0 for any prefix and says nothing about round 1. Scored correctly, round 1's arrangement is **rate-dependent**, which is why the absolute was the wrong thing to assert: measured at full scale against the real 618 rps x 300 s prefix (189,108 targets), it is **100.0% at 500 and 1,244 rps, 76.5% at 3,715, 66.0% at 10,000 and 64.4% at 13,312** — exactly 1.0 wherever the warmup outran the measured window, and less above it. |
| W20 (residue, minors) | Two rendered rung counts had no assertion — dropping `over {rd.cv_n_rungs} rungs` from `_operating_point_clause`'s MOVES branch, or from the cost section's flat clause, or an off-by-one in either, all left the suite green (the one existing assertion was a bare `"the largest spans 2 rungs" in md`, which passes as long as ONE of the two sites still renders it). Each is now asserted with its surrounding words. And a failed `docker cp` in `draw_and_ship` raised `RuntimeError`, the one loud failure in the probe path that reached the operator as a raw traceback: `run-all` and `_guarded` catch `PhaseError`, so it now exits 1 with `run-all failed: ...` like everything else. |
| — | **RUN-STOPPER: the harness could not parse the Java SUT's `/runtime-stats` at all.** `services/java/.../RuntimeStatsWriter.java` reports `gc.collections.gen0/gen1/gen2` as a present null — the honest answer, since G1's old-generation bean counts Full GC only and reads 0 where generational ZGC reads 3 on identical work — and `gc.pause_total_seconds` as null under ZGC, where `getCollectionTime()` is millisecond-granular and the pauses are not. Both were non-nullable in `runtimestats`, so **every 1 Hz poll raised `ValidationError`** (measured: 3 errors under G1, 4 under ZGC), `StatsPoller.poll_once` caught it as a `ValueError` and counted a poll error, and the Java slot would have collected **zero snapshots for the whole run**. Fixed by extending W17's existing convention rather than inventing a second one: `sum_present` is now the single "sum what was reported, name which, answer None when nothing was" helper, used by `GcCollections.total()` and by the window-delta path alike, and the JVM's real per-bean counters ride through in `model_extra` into the flushed trace verbatim. Two downstream consumers stopped reading not-measured as zero: `WarmupState._gc_cadence` produces no value for an interval with a null edge (differencing across the gap would charge a whole unmeasured stretch to one interval; filling with 0 would manufacture the flat cadence the gate looks for), and `analyze._gc_pool_rows` renders `not reported` instead of summing null with `or 0` into a collector that never collected and never paused. |
| — | **The vacuous passes docs/p1-plan.md 3.2 names, closed — this is R1-B1 recurring.** Measured before the fix, on a slot with zero poller snapshots and an empty latency series, `p99_flat`, `p999_flat` and `gc_steady` all reported `passed: true, status: measured`: **three green gates on no evidence whatsoever**. `no_positive_trend` returned "flat" below three points because Mann-Kendall is undefined there, and `gc_steady` inherited it. Both now return a `TrendVerdict` carrying the status and the usable-sample count, and a new fifth gate status `insufficient_data` **enforces** — `ENFORCING_STATUSES = {measured, insufficient_data}` — so a gate with no evidence FLAGS the probe instead of joining the waivers in silence. The reader half is not symmetric and had to be changed with it: `analyze._failed_gate_names` filtered on `status == measured`, which is right for the three statuses that mean "not applied" and wrong for this one — a probe flagged because its `/runtime-stats` never parsed would have been flagged in the ledger and then reported as having no failed gate. No threshold moved: `MIN_TREND_SAMPLES = 3` is where MK is defined, and B2/W21 still owns the calibration. **The consequence, stated rather than routed around:** with all three generations null the JVM's `collection_cadence` series is empty at every poll, so `gc_steady` — which `registry.JVM_GC.requires` declares over both `heap_committed` AND `collection_cadence` — will return `insufficient_data` and flag **every Java probe**. That is the honest reading of the data the Java SUT publishes, and closing it is a DECLARATION (either `JVM_GC.requires` drops the cadence series with a measured rationale, or the per-bean counters beside the null triple become the declared cadence source, which needs the ZGC `*Cycles`/`*Pauses` double-count handled). It belongs to W27, which owns `spec/runtime-stats.md`, and is not decided here. |

| W21 (step 1) | **B2 step 1: the flatness verdict becomes a statistic vector, and it is OBSERVE-ONLY.** Round 1 kept pass/fail and nothing else, which is precisely why 59.5% of its windows could not be recalibrated: no artifact held a number that could say whether the threshold, the statistic or the SUT was responsible (R1-A2). `warmup.flatness_verdict(series, *, cfg) -> FlatnessResult` now returns `n`, `samples_per_second` (+ its source), `median`, Mann-Kendall `z / p / tau`, the Theil-Sen `slope_per_second`, `s_rel_pct_per_min`, `lag1_autocorrelation`, `half_vs_half_median_ratio`, `verdict`, `status` and `reason`; it lands under `GateResult.detail["flatness"]` and reaches every probe's `<slot>.warmup.json`. **No threshold moved and none entered `spec/`** — the enforced answer is still round 1's rule, Mann-Kendall significance at alpha with no practical-significance band, recorded beside every record as `FLATNESS_ENFORCED_STATISTIC`. `s_rel_pct_per_min` is `100 * slope * 60 / median`: `docs/p1-plan.md` 3.2.2 writes the ratio `slope*60/median` and LABELS it %/min, and the factor 100 is the conversion that label requires, applied so the recorded number is literally percent per minute. **A per-gate `enforce: bool` is the second axis**, and it was necessary rather than tidy: `enforced` is now `enforce and status in ENFORCING_STATUSES`, because `insufficient_data` enforces BY DESIGN and spelling a harness fact as a waiver would have put it in `spec/slo.yaml warmup.gates_by_posture`, which is for postures. |
| W21 (step 1, p999) | **`p999_flat` retired as an enforcer, kept as a statistic — and the argument re-measured rather than quoted.** `MIN_SAMPLES_PER_SECOND_P999 = 1000.0`; below it the gate returns `status=insufficient_data`, `reason=quantile_unresolved`, `enforce=False`, with every statistic still computed. Measured against this repo's own estimator (`vegeta._percentile_ns`, nearest rank, `idx = min(n-1, int(q*(n-1)+0.5))`) at q=0.999: **n <= 501 returns the MAXIMUM of the second, 502-1501 the second-largest, >= 1502 the third-largest or lower** — so even at the 1000 floor the estimate is only the second-largest, and the comment at the constant says exactly that instead of implying resolution the estimator does not have. **`docs/p1-plan.md` 5.2's "371 rps at the landed cap 300" is stale and was not repeated:** driven through `live_runner._warmup_rate` from `spec/`, the warmup rate is **618 rps on `full` and `rehearsal` and 149 rps on `smoke`** (W13 decoupled the rate from the cap after 5.2 was written), both below 1000, so the gate is inert on every tier this harness runs. The floor is checked BEFORE the short-series cause and the order is load-bearing — inverting it flags 100% of probes on a gate that says nothing about warmth. **`samples_per_second` is MEASURED, not the offered rate:** `vegeta.per_second_buckets_from_ndjson` returns the size of the bucket each quantile was taken over, `WarmupState.samples_series` is index-aligned with the quantile series, `_samples_per_second` takes the MEDIAN of the trailing window (the first and last bucket of every vegeta segment is a partial second) and records `samples_per_second_source`, falling back to the offered rate only when the encoder produced nothing and saying so. `<slot>.warmup_latency_per_second.csv` gains the column: `segment,second,p99_ms,p999_ms,samples`. |
| W21 (step 1, census) | **`analyze.WarmupCensus` — the aggregate `docs/p1-plan.md` 3.2 says P8 has nothing to read.** Per cell per slot across **all three** stages that run warmed probes (`_WARMUP_CENSUS_STAGES = ladder, confirm, soak`; no existing walk covered all three — `_STATIONARITY_STAGES` is ladder+soak, `_RESOURCE_STAGES` is confirm+soak). Carries `n_probes`, `n_flagged`, `flagged_fraction`, `by_gate`, `by_reason`, `by_stage`, `abort_threshold` and `exceeds_abort_threshold`, published in `fits.json` under `cells[].warmup_census` and as a report section. `WARMUP_ABORT_FLAGGED_FRACTION = 0.25`, strictly `>`, **in code and not in `spec/`**: it is an operator stop rule between cells, not a declared gate. Counted only over `state == DONE` probes and read from `ProbeRecord.gates_failed` / `warmup_attack_failed`, which is the identical evidence `phases.ProbeOutcome.under_warmed` is derived from, so the census cannot disagree with the exclusions the fits applied. `flagged_fraction` is **None, never 0.0**, when nothing was scored; `by_reason` attributes exactly one cause per flagged probe (a crashed warmup wins over the gates it left unscored) and therefore sums to `n_flagged`, while `by_gate` counts each gate and deliberately sums higher. |
| — | **REVIEW: the reader half of `enforce` was missed, and it is R1-B1 recurring one layer up.** W21 added the per-gate `enforce` flag to the WRITER and left `analyze._failed_gate_names` filtering on `status in ENFORCING_STATUSES` alone — the same shape as the W23 defect two rows above, in the opposite direction. Measured through the real `WarmupState` at the harness's own 618 rps: `p999_flat` records `passed: false, status: insufficient_data, enforced: false`, `WarmupResult.gates_failed` correctly names nothing, and the reader named `p999_flat` as a gate that FLAGGED the window — on the documented fallback path (`_ledger_failed_gates` covers only probes whose ledger record lists evaluated gates), so `ledger.json` and `report.md` of one run would disagree about which gate flagged which probe. The reader now asks the record its own `enforced`, falling back to the status filter for records written before that field existed. **The larger half is what nothing said at all:** `spec/slo.yaml` declares `p999_flat: true` inside `config_hash`, the gate is implemented, it is evaluated on every probe — and it enforces on NONE of them, on every tier, because no warmup rate this harness offers clears the q=0.999 resolution floor. That fact was in each `<slot>.warmup.json` and in no run-level artifact; the report's "declared but not implemented" bullet is keyed on `warmup.IMPLEMENTED_GATES` and stays silent, because `p999_flat` IS implemented. `analyze._non_enforcing_gate_names` and `SlotResource.non_enforcing_gate_counts` now count it over every roll-up window and the report states it: a probe recorded as warm was not held to that gate. Deletion from `spec/slo.yaml` is still the honest end state (5.2 says so, W29 owns the key) and is not taken here. Also corrected: the two W20 rows above published the 371 rps warmup draw as **113,570** targets where `10 x probe_draw_count(371, 30)` is **113,530**. |
| W24 | **B1 / R1-A1 / D9: the ladder, the fit, the confirm and the soak are PER SLOT.** This is the defect that invalidated round 1's headline, and it was upstream of `analyze` the whole time: the per-language isotonic fit already existed, already dropped rungs with no measurement-grade probe of that language, already reported `crossed=False` with a top rung — and drove nothing, because `run_ladder`'s stop check took `sum(v)/len(v)` over a rate-only parse of the probe key, i.e. the mean over BOTH slots. The ladder therefore stopped when the mixed mean crossed, and every rung above the faster-degrading stack's knee was **never offered**, so no later analysis could recover it. Now each slot stops at its own crossing while the others keep climbing; the rungs above the first crosser's knee are SOLO blocks, and nothing stamps that — `len(slot_order) > 1` is the statement, already persisted and validated, and a derived flag cannot disagree with the record. `_rung_values(rec, slot)` applies the slot filter **above** `seen_rates.add`, which is load-bearing: below it, every solo rung reports as "emptied by exclusion" for the slot that stopped there, `_retry_excluded_rungs` re-runs rungs that slot was never planned at, and the run aborts naming under-warming as the cause. **Four live definitions of "crossed the SLO" collapsed to one.** `analysis.knee.crosses` — the isotonic (PAVA) fit, bootstrap-free and tolerant of a single rung — is now both the ladder's stop rule and the published fit, so where the run stopped climbing and where the report says the knee is can no longer be two different rates (they agree on monotone rungs and diverge on a dip; the test uses a deliberate dip because nothing in the suite had one). `SloAtRate.meets_slo` stays a **separately named** quantity and must: it is the mean of confirm/soak windows, not an isotonic fit of ladder rungs, with the opposite inequality at the boundary — at exactly 20.000 ms a slot has both `crossed` (`>=`) and `meets` (`<=`), which is correct for a slot sitting at its knee. The two sentences that claimed `meets_slo` was "the same rung-mean rule the ladder bracketed on" (`_slo_at_rates`' docstring and the report body) were false the moment the ladder went isotonic and are gone; the contradiction round 1 narrated in prose — `meets_slo=yes` at a rate above that slot's own knee — is now a `SloConsistency` record in `fits.json` with both numbers. **Confirm and soak moved to the fitted knee**, one rate per slot (`confirm_rate_basis: fitted_knee`). `_confirm_rates_from_bracket` returned the bracket rungs verbatim and the roll-up's `rate <= knee` filter then admitted only the LOWER one, so the metric *defined* as cost at the crossing rate was measured one geometric step below it — within 16.7% at full's 1.2 step, within 2x at rehearsal's 2.0, against a per-request cost round 1 measured at CoV 19.3% across its ladder. Two consequences declared rather than absorbed: a confirm window at an interpolated knee can land marginally supra-knee (that IS the conformance statement), and the soak now measures drift AT the promise rather than under it. **A slot that reaches the rung ceiling without crossing aborts the run** by default, with `LEDGERBENCH_ALLOW_SUB_KNEE_LADDER` stamping `degraded` on the cell's ladder stage exactly the way `ALLOW_DB_IMPOSED_KNEE` does. Round 1's failure was not that Go never crossed — it was that nothing in the artifact said so; `ceiling_reached_without_crossing` beside `rung_ceiling_rps` is that sentence. Rung 0 already over the SLO is a `PhaseError` naming `ladder_start_rps` (it used to reach `estimate_knee`'s bare `ValueError` and escape the CLI's PhaseError-only guard as a traceback; P8 pre-registers exactly this case). **Both resume bugs, fixed here or B1 reproduces them per slot.** The crossing check now reads only rungs where every planned probe is DONE — `reset_orphan_blocks` resets a block with `0 < done < members`, so a rung with blocks 0-1 fully DONE and block 2 fully PENDING survived a crash untouched and looked complete, its mean taken over 4 probes where 6 were planned; if that partial mean crossed, the ladder broke, block 2 stayed PENDING and the stage was marked DONE anyway. That is a plausible mechanism for R1-A4's "as few as one measurement-grade rep", and a DONE ladder holding a PENDING probe is now an assertion. And `reset_orphan_blocks` flipped a probe to PENDING while leaving its `p99_ms`, `under_warmed`, `p99_series` and `excluded` entries in the stage data, so an aborted resume fitted a number belonging to a probe the ledger recorded as never having run; `ledger.drop_probe_data` over `PROBE_DATA_MAPS` is one definition shared with `_reset_rung_probes`, for the same reason `reset_to_pending` is one. **A new RCB hole, closed before it could open.** Both `_execute_block_probes` and `reset_orphan_blocks` grouped probes by `(block, rate)`; a confirm block now holds each slot at its own knee, and under that key such a block splits into singleton groups, `0 < done < members` is false for both, and a crash that halves it is **silently never repaired**. Grouping is by block id alone, and `plan_blocks(block_offset=...)` is what makes ids unique within a stage — a mutation removing the offset survived the whole suite until two tests were added for it, because the visible consequence is a resume that RESETS an already-measured rung. **The pairing question, in full (3.1.4).** Pairing is on `(cell, stage, block)`; the rate left the key because with java@4,000 and dotnet@11,000 the both-present test never fired, every rate was skipped, and the report rendered "Not available in this run" with no error and no warning. The metric is COST — CPU-ms/request primary, anon-RSS/request and $/M alongside — because at their own knees both stacks sit at ~20 ms by construction and a paired p99 verdict is a guaranteed tie; `analysis/paired.py` needed no change, its docstring has always named those metrics. A `common_rate` verdict at `min` over the slots' confirm rates runs the faster slot's extra probe INSIDE the same block, in the seeded order (a block of three members, `order_index` recording position because `slot_order.index` cannot distinguish a duplicated slot); it costs `confirm_blocks` probes per cell against the `confirm_blocks x 2 bracket rates` the knee move already removed. Every verdict carries `verdict_scope` and a `min_blocks` floor read off `spec/slo.yaml ladder.confirm_reps`; solo blocks are excluded BY `slot_order` and counted separately from blocks that lost a probe to exclusion. **~250 lines of pooled-disclosure prose deleted, not ported** (`_pooled_composition`, `_pooled_note`, `_knee_for_cell`, `_per_slot_knee_sentences`, `_knee_row`'s pooled branch, the report block, the module docstring's honesty rule 2, `CellAnalysis.knee` and the `knee_pooled` key), with an ast-level anti-resurrection test: a report that still explains what a pooled fit pooled, after there is no pooled fit, is worse than one that never had the section. The roll-up's rate filter is per slot (one shared bound selected the other slot's rows), and its rounding tolerance is 1 rps for a named reason — `int(round(knee))` is strictly greater than the knee for any fractional part >= 0.5, so `rate <= knee` would have dropped **every** confirm and soak window and published `available=False` with an entirely honest-looking not-captured line. `_cpu_vs_rate_for_cell` now walks ladder+confirm+soak so each slot's operating point is on its own curve, and posture-only rungs are out of the CV **and** out of the rung count printed beside it. Ledger schema **4 -> 5**. No `spec/` edit: `ladder.knee_fit: isotonic` and `knee_ci: bootstrap_over_reps` stay true and `confirm_reps: 10` is reused unchanged as the confirm block count, the CI `min_reps` and the paired floor. |
| W24 (terminology, first) | **The 3.1.3 rename that W10 was supposed to do and did not.** "Pooled" meant two unrelated things and one of them was a green test name: `test_pooled_p99_drives_crossing_not_per_second_series` defends vegeta's WITHIN-PROBE report p99 against a percentile-of-percentiles — the opposite sense from the cross-language pooling W24 deletes — so anyone grepping "pooled" during this work found a passing test that looked like it protected exactly what they were about to remove. Renamed to `probe_p99` across the test module and the `phases`/`live_runner` docstrings **before** any W24 code changed, so the anti-resurrection grep cannot hit it. |
| W26 | **B9 / R1-A6: the capacity gate runs TWICE, and the second one is a MEASUREMENT.** Round 1 took its 1.72x margin against `ladder_start_rps * 8.0` of projected demand — an inline literal that reached no artifact — and then ran cells several times higher; PG never broke, but the gate never tested it there. Stage 1 keeps the projection, with the 8x now a named `PROJECTED_KNEE_FACTOR` and a `projected_knee_basis` string in the ledger and the manifest. Stage 2, `run_capacity_regate_phase`, runs between cell 1 and cell 2 and asks the same question against PG's own `cpu.stat` delta over cell 1's operating-point windows / (cores x wall seconds) <= 1/headroom_factor (0.7692 at 1.3). **The projection rule could not have been re-used**: `capacity_gate_rungs=12` from 500 rps at 1.2 caps the create-only ladder at 500 x 1.2^11 = 3,715.04 rps, so its "knee" is a LOWER BOUND, and a lower bound cannot fail a headroom test honestly — on a 5,714 rps realized knee it needs 3,085.71 x 1.3 = 4,011.43 rps and fails by construction, which is how a gate becomes something the operator opts out of as routine. It is retained only where the ladder actually broke. The utilization rule also costs no runtime and re-runs no ladder, so `_db_has_diverged` never trips and the exact-count seed verification survives every resume. |
| W26 (three decisions the plan did not make) | (1) **Utilization is the MAX over slots, not the pooled mean.** With the per-slot knees W24 produces, a 0.30/0.90 split pools to 0.60, which clears the 0.7692 ceiling while PG was past it for every window of the faster slot — and PG serves whichever stack is resident. (2) **The window set is `rate == that slot's own declared confirm rate`,** not every confirm window: the confirm stage also runs the higher-knee slot at the COMMON rate for the paired verdict, and those windows sit below the operating point by construction, so including them measures the gate somewhere other than where the headline is taken — R1-A1's shape. (3) **A cell that fitted no knee is `REGATE_NOT_EVALUABLE`, not a pass**: its windows sit at a top rung, which says nothing about the rate the remaining cells will run at. The core budget comes from the windows' own `cpuset.cpus.effective`; windows that disagree about it refuse rather than reconcile. **No levers**: `LeverMachine` is never constructed, because a lever fired after cell 1 changes the workload for cells 2..N while cell 1 already ran the old one. |

Toolchain installed for this phase: Temurin JDK 25.0.4, Maven 3.9.16, Go 1.26.7 (the
seeder's `go.mod` requires >= 1.26.5; Fedora ships 1.25.12 with `GOTOOLCHAIN=local`, so
`make lint` could not run `go vet` at all).

**Workstream A: de-language — `DONE` for `bench/`.** Do not `sed s/go/java/`. `gc_steady_go`
checked "collection cadence flat", which encodes Go's single-generation model; renaming it
yields a gate that silently means the wrong thing. The coupling is replaced by a **slot**
registry (not a language registry — see W4), and
`test_no_language_or_slot_literal_outside_the_registry` is what keeps it replaced.

| File | Status |
|---|---|
| `ledger.py`, `manifest.py`, `warmup.py`, `analyze.py`, `analysis/paired.py`, `analysis/report.py`, `phases.py`, `live_runner.py`, `compose.py`, `smoke.py`, `capacity_gate.py`, `figures.py` | **DONE** — W5/W7/W9/W10/W12 above. `sut.py`'s dead half was deleted rather than de-languaged (W6). |
| `bench/tests/*` | **DONE** — fixtures take their slot ids from `registry.DEFAULT_SLOT_SET`, so they follow the flip to the headline pair at P4 without an edit. |
| `bench/validation/clients.py` | **DONE** — base URLs come from each slot's `registry.Slot.url_env`, and `skip_reason()` NAMES the unset vars instead of counting services (`len(...) == 2` is silently wrong for any set that is not a pair, and told an operator nothing). Deleted with it: `both_services_configured()`, the unused `services` conftest fixture (a *second* SUT-discovery path beside `test_equivalence._services_or_skip`), `normalize.deep_equal` (a one-line `a == b` used only by its own test, next to the real tool `first_difference`), and `normalize.py`'s "defensive global no-float-money guard" — a comment promising a float check *anywhere*, guarded by `is_money_key(_key)`, i.e. unreachable, in the one file whose whole job is cross-stack honesty. |
| `infra/compose.yaml`, `infra/compose.bench.yaml` | **PARTLY** — `sut-go` removed and `sut-dotnet-b` added (W11); `sut-java` lands with `services/java/` at **P4**, not before, or it recreates the dangling build context W2 just repaired. |
| `Makefile`, `.github/workflows/ci.yml` | **PARTLY** — repaired for the deleted `services/go` (W2); the `java` job and the re-derived collected-test floor land at P4/W31. |
| `README.md`, `CONTRIBUTING.md`, `docs/*` | **TODO** — W31, deliberately last: those files are rewritten from a skeleton, not edited, because round-1 numbers are woven into subordinate clauses. |

**Workstream B: the defect fixes.** In priority order:

1. **Per-language knee fitting (R1-A1, D9).** Deepest change. `phases.py` ladder control
   and `analyze.py` fit path. The ladder must continue until both stacks cross the SLO,
   with a declared rung ceiling and a documented outcome when one does not cross.
2. **Recalibrate `p99_flat`/`p999_flat` (R1-A2).** Currently rejects two thirds of windows.
   Derive thresholds from real per-second series rather than a chosen alpha.
3. **Posture-specific gate sets (R1-A3).** A gate that a posture cannot satisfy by
   construction is declared per posture or the posture is not run.
   *(W15: the mechanism is DONE — `WarmupCfg.gates_for(cell_id)`, merging over the
   global set, resolved once in `live_runner._warmup`. No override is declared yet,
   and none may be until a posture is measured to fail a gate; W22 adds the plan-time
   refusal so an unsatisfiable cell is rejected rather than run and narrated.)*
4. **Implement or delete `hot_set_touched` and `major_faults_asserted_equal` (R1-B1).**
   Whichever way it goes, the per-probe record must state which gates were evaluated.
5. **Extra cgroup sampling** (PG, vegeta, dockerd, containerd, Redis) and the
   DB-CPU-per-request column. **DONE (W16).**
6. **Host-state read-back (R1-B2)**, failing the run on mismatch. **DONE (W18)** — as its own
   `host_readback` phase *after* host_setup, not inside doctor; settings and outcomes are
   separated as a typed field and an outcome can never gate.
7. **`/runtime-stats` per-window flush (R1-B3). DONE (W17).**
8. **Per-probe target draw (R1-A7). DONE (W20).**
9. **Two-stage capacity gate (R1-A6)**: projection, then re-gate at realized rates.
10. **Third scale profile, `rehearsal`. DONE (W14).** `smoke` is `warmup_cap_s=15`,
    `measure_s=10`, 3 rungs, `functional_only=True`: HotSpot C2 will barely have started
    compiling at fifteen seconds of load, so **every Java smoke probe is under-warmed by
    construction and every smoke number is noise.** Smoke proves plumbing. `rehearsal`
    keeps every full-scale evidence dial — warmup cap, discard, measured window,
    min-calls, dataset and working set — and cuts only wall clock: 6 rungs, 2 reps,
    2 confirm blocks, 300 s soak, one cell. Two deliberate departures from the plan text:
    the ladder steps at **2.0, not the spec's 1.2** (six rungs at 1.2 reach 1,244 rps, so
    a ~10k knee is unreachable and G6's crossing question is answered `no` by
    construction), and the working-set dial is **not** reduced (~70 s of body-pool
    generation is not a budget item; W20 renamed it `body_pool_requests`). Two to three hours.
11. **Checksum file that actually verifies (R1-B4). DONE (W19).** A map, not a log; the scope
    is `git ls-files --cached --others --exclude-standard` from the run directory — git's own
    answer to "what gets published" — and it raises rather than emitting a narrowed map when
    git cannot answer.

**Spec edits, all of which must land here** because `config_hash` freezes at P7:

- `spec/runtime-stats.md` — rewrite the field mapping for the JVM. **Two rows cannot be
  mapped honestly and must not be forced** (measured, see FINDING 6/7 in
  `docs/research/2026-08-21-measured.md`): `gc.collections.gen2` means three different
  things across G1 / ZGC / .NET (G1's old-gen bean counts Full GC only, so it reads 0
  where ZGC reads 3 on identical work), and `gc.pause_total_seconds` is identically 0.0
  under ZGC because `getCollectionTime()` is millisecond-granular and ZGC pauses are not.
  Report `null` with the reason rather than a false number. Source from
  `java.lang.management` / `com.sun.management` MXBeans, **not JFR** (JFR overhead lands
  inside the measurement). `GarbageCollectorMXBean` for collections and pause totals,
  `MemoryPoolMXBean` for heap, HikariCP's pool MXBean for `db_pool.*` (it exposes more than
  Npgsql, so fields that are `null` on .NET will carry real values), Lettuce for
  `redis_pool.*`. Document what `goroutines_or_threadpool` means under virtual threads
  (carrier threads vs mounted virtual threads).
- `spec/slo.yaml` — `warmup.hard_cap_seconds` **raised 180 → 300. DONE.** A ceiling, not a
  duration: warmup exits as soon as gates pass, so raising it is free in the normal case
  and impossible after freeze. Worst case at 40 rungs × 3 reps × 2 languages costs ~8 h
  per cell. G6 confirms whether it ever binds (O4).
- `spec/db-access.md:107` — exec/prepare table: pgx column → PgJDBC (`prepareThreshold=5`),
  .NET column → `Max Auto Prepare=20` per D4. **DONE (W28 part).**
- `spec/db-access.md` — the DB-CPU column. **DONE (W16).** Two edits, in opposite
  directions. Struck: "the DB-CPU-per-request column that would have priced it was
  descoped 2026-08-13" — a standing declaration *in the frozen tree* that a measurement
  will not exist, which is round 1's failure mode with the sign flipped, and which
  PROGRESS reverses. Added: what the column measures, its four structural limits
  (whole-container, not per endpoint, driver-sensitive, null-never-zero), and the
  connection-reset asymmetry beside the D4 dial that moves it. **No `spec/slo.yaml` key**:
  a measured column is not a gate, and the governing rule bars declaring one without an
  implementation — the inverse (implementing without declaring a gate) is fine.
- `spec/slo.yaml` — `ladder.attack_timeout_seconds: 5.0` **added. DONE (W15).** Not a new
  dial: it is the value that was already in force, moved to where it is obeyed. The runner
  read it from `co_validation.stall_test["timeout_s"]` — a key that block has never
  defined and not a property of a stall test — so it silently resolved to the `5.0`
  fallback while reading as spec-controlled. A per-request attack timeout decides what
  counts as an error against `slo.error_rate_max`, so it belongs under `config_hash`, and
  the typed `StallTest` model now makes the phantom read impossible rather than merely
  wrong. `co_validation.stall_test` keeps `freeze_seconds` + `method` and the executed
  smoke step finally obeys both.

**Gate G1 — PASSED.** Audited against `docs/p1-plan.md` §7's seven criteria by an agent with
no context from the work, each settled by running the command rather than reading a report:

| # | criterion | evidence |
|---|---|---|
| 1 | suite green, ruff clean, CI floor re-derived | **892 passed / 19 skipped**, `ruff check .` clean; floor raised 340 → 888 with all four arithmetic terms reproduced |
| 2 | `dotnet format --verify-no-changes` exit 0, tree clean after | exit 0, no output; cleanliness proved by `diff -rq` against a byte-identical mirror |
| 3 | `make lint` + all three CI jobs | `make lint` exit 0; `seed (go)` gofmt/vet/test clean; `dotnet` 87 passed; **`java` 256 run / 11 skipped, BUILD SUCCESS** |
| 4 | `set(load_slo().warmup.gates) - IMPLEMENTED_GATES == set()` | True. Declared `{gc_steady, p99_flat, pools_warm}`; `p999_flat` is implemented-but-undeclared — the permitted direction |
| 5 | SLO models `extra="forbid"`, `spec/` loads | all ten models; `warmup.bogus_gate: true` in a tmp copy raises; the real spec loads |
| 6 | no slot literal outside the registry, no round-1 constant, links resolve | three guards pass — see the wording amendment below |
| 7 | every Workstream-B item closed or struck | ten of eleven closed with tests; B2 half-struck (§5.2, executed) and half-sequenced to P2/P6/P7 (§3.2) |

**Two amendments the audit forced, both to `docs/p1-plan.md` §7 rather than to the guards.**
Criterion 6 passes *as implemented* but was written broader than either guard: the AST walk
runs over `bench/ledgerbench/**`, not everything outside `registry.py`, and the round-1-constant
ban covers the prose surface, not the repo. Both narrowings are argued in the tests' own
docstrings; the plan text was not updated to match. **Widen the wording, never the guards** —
`bench/figures.py` and `test_shapecheck.py` legitimately carry slot ids. Criterion 7 offers a
binary ("closed with a passing test OR explicitly struck in §5") that B2 does not fit; it needs
a third disposition for work deliberately sequenced past P1.

**The last two defects, both caught by running code and both fixed before the freeze.**
`spec/slo.yaml`'s gc_steady comment claimed ZGC's `*Pauses` beans' time "does belong in
`gc.pause_total_seconds`" — measured, both read `getCollectionTime() == 0` because that
accessor is millisecond-granular and ZGC's pauses are not, so the field is **null** under ZGC,
which `spec/runtime-stats.md` already stated. **Two files inside the same `config_hash`
contradicted each other and the hashed one was wrong.** (It also said "two beans" where three
names are excluded, never all three in one run.) And `README.md`'s fairness paragraph still
asserted "both drivers run the extended protocol with binary formats" — the exact sentence P1
deleted from `spec/db-access.md` after a PG wire tap measured it false: parameters are binary
on both from the first execution, but **results** are the asymmetry (Npgsql binary immediately
for every column; PgJDBC text until `S_1` at `prepareThreshold=5`, then binary only for the
types it supports, so `status` and `currency` stay text).

**Thirteen consecutive adversarial rounds, and every one found a claim broader than the code.**
That is the finding of P1, more than any single work item. The house rule already covered
comments and docstrings; it did not cover **rendered report output, spec prose, or
machine-readable spec keys**, and those are what a reader actually reads. Round 1 died of
over-declaration; the same instinct kept reappearing one layer out — a report sentence
asserting a lifetime-high-water read on a kernel with no such counter, a seeded-corpus figure
printed 1000× wrong, `enforced: false` declared in the frozen spec about a field no artifact
carried, `asserted_in_manifest: true` hashed into `config_hash` with zero readers. Each was
found by running the code, never by reading it.

### P2 — Harness shakedown on .NET vs .NET — `DONE, gate G2 PASSED 2026-08-27`

Run `rehearsal-20260827T065102-fedora` (rehearsal tier, shakedown slot set), completed
after two instrument defects that only a tuned end-to-end run could surface — which is
this phase doing its job:

1. **vegeta eager target loading OOM'd at the 8,000 rps rung** (anon RSS 4.18 GiB vs
   the 4g ledger limit): vegeta parses the whole per-attack targets file and inlines
   every `@body` up front, and W20's correctly rate-sized draws made that unscalable
   (a full-tier soak draw needs >15 GiB eager). Fixed: `-lazy` on every attack.
2. **docker 29.2.1 (systemd driver) intermittently leaves `memory.swap.max=max` on
   container CREATE** despite HostConfig MemorySwap == Memory — the placement gate
   aborted exactly as designed when the resume's recreate of postgres/redis flaked.
   Fixed: the gate now repairs via `docker update` + re-read, aborting only if the
   repair did not take. It fired again on the very next recreate.

Both crashes were resumed with `--resume`, which is a live proof of the resume
machinery on real failures: completed phases held (full-scale seed, gate, validation),
the OOM-split ladder block was redone whole, and the run finished `complete=True`.

**G2 evidence:**
- Per-slot knees fitted independently: EF Core **6,259 rps**, raw ADO **6,964 rps**
  (D9/R1-A1 machinery live).
- **The correctness oracle reproduces: EF Core costs +26.2% CPU per request over raw
  ADO** (0.450 vs 0.356 ms/req, request-weighted over confirm+soak; round 1 measured
  ~20%): ordering and rough magnitude both hold, so the instrument reads numbers.
- Under-warmed flagging: **0% (EF) / 13.3% (ADO)** against round 1's 59.5% and the
  25% abort rule (R1-A2 validated at this tier).
- Capacity gate `pass`, re-gate `pass-at-realized-rates` (R1-A6 machinery live);
  DB-CPU column populated (~0.11-0.12 ms/req both slots).

**Observation carried to P8, not a defect:** at rehearsal's 2.0 ladder step the
interpolated knee is crude — confirm windows at the fitted knee ran p99 ≈ 25.8-28.5 ms
against the 20 ms promise, and the report's SloConsistency/conformance rows disclose
exactly that. Full's 1.2 step narrows the bracket to 16.7%; check the confirm-stage
conformance rows on the first full cell before trusting the operating point.

**De-risks P1 weeks before any Java exists.** The harness rewrite is the riskiest code in
this project and there is no reason to debug it and a new Java service simultaneously.

Run the two-slot harness with **.NET in both slots**: `DATA_LAYER=efcore` in slot A,
`DATA_LAYER=ado` in slot B. This exercises per-language knee fitting, the recalibrated
warmup gates, interleaving, RCB pairing, cgroup sampling, resume, and the whole analysis
path against code that already works.

It also comes with a **correctness oracle**: round 1 measured EF Core at about 20% more CPU
per request than raw ADO.NET, and Dapper within 2% of ADO. If the rebuilt harness does not
reproduce that ordering and rough magnitude, the harness is wrong, not the services.

**Gate G2:** rehearsal-scale efcore-vs-ado run completes; per-language knees are fitted
independently; under-warmed flagging is materially below round 1's 59.5%; the EF-vs-ADO
CPU ordering reproduces.

### P3 — Java research — `DONE`

Before writing code. The goal is that a Java engineer reading `services/java/` recognises
it as ordinary production code, not a benchmark harness cosplaying as an application.

Deliverable: **`docs/research/2026-08-23-java-research.md`** (five parallel research passes,
consolidated), on top of the three files from 2026-08-21 — `-measured.md` (the Hibernate SQL
spike, the GC-bean mapping, the wire formatter, the cpuset finding), `-java-stack.md` (the
A1-A31 asymmetry register) and `-critique.md` (the adversarial pass, several items still
open). Every version string is fetched or measured on the build date; nothing is recalled.

Settled here, and each removed a live P4 risk: the errors-map key format, the `instance`
back-fill and the exception-advice shape are now **measured working against real Boot 4.1.1
MVC** rather than assumed — those were the three highest-probability equivalence failures.
Spring Boot **4.1.1** GA confirmed as what `start.spring.io` generates today; Maven 4.0.0 is
still `rc-6`, so **D11 holds**.

Two prior research claims are **stale and must not reach `spec/` or the Dockerfile**:
`-java-stack.md`'s "Temurin 25.0.4.1+1 has no linux x64 glibc asset" (false since
2026-08-21T11:40Z) and `-measured.md`'s "the container lags the host at 25.0.3_9" (images
are at `25.0.4_7`, matching this host exactly).

**Gate G3 — met, with one wording correction.** P3's gate said the dials table lands in
`docs/FAIRNESS.md`; W31 rewrites that file **from a skeleton, deliberately last**, because
round-1 numbers are woven into its subordinate clauses. Editing it now and rewriting it later
would either lose the table or freeze the rewrite around it. The table therefore lands in the
research file and W31 lifts it. The gate is the table existing before any Java code exists,
and it does.

**The research found the fourth category before P7 had to.** The P7 reviewer brief says every
dial is a documented default, set identically on both sides, or a labelled variant axis —
"find the fourth category". Version-currency policy is one, and it is **O6** below.

### P4 — Java service — `DONE, gate G4 SIGNED OFF 2026-08-27` (P5 collapsed in)

**G4 evidence, all against the D12-reworked .NET build, all on the native engine:**
- Functional smoke on the headline pair end to end: both images build, both SUTs
  healthy, **live equivalence suite 61 passed**, CO stall check ok, probes on both
  slots byte-identical response-code distributions (`200:1242, 201:258` each) at
  achieved/offered 1.001, success 1.0000.
- Full Java suite with the live endpoints enabled: **256 run / 0 failures / 0
  skipped** against the compose PostgreSQL 18.4 + Redis (as the PG superuser: the
  compose `ledger` role deliberately cannot DDL, and `LiveEndpointTest` applies
  `spec/schema.sql` itself — its javadoc's throwaway-PG recipe assumes that).
- **SQL statement-stream diff on the wire** (`log_statement=all`, identical 6x
  create/read/list/quote sequence against each SUT, markers between segments):
  every difference is a DISCLOSED deviation and nothing else surfaced —
  .NET's per-checkout `DISCARD ALL` (24 of them; the registry's Npgsql pooling
  note, none on HikariCP), `BEGIN` vs `BEGIN ... READ COMMITTED`, Hibernate's
  per-item `insert ... returning id` under IDENTITY vs EF's batched insert,
  the Q3 paging-shape difference, `count(*)`-style existence probes on both.
  **D4's promotion measured on both sides**: named prepared executions Java 60,
  .NET 12 — both drivers promote, PgJDBC's survive the pool (HikariCP holds
  connections) while Npgsql's interact with the checkout reset; the imbalance
  is the disclosed reset asymmetry, re-confirmed on the wire.

`services/java/` to spec parity. Target ~4k LOC, matching `services/dotnet/`. Acceptance is
the equivalence suite in `bench/validation/`, not eyeballing.

**State at 2026-08-25.** The service is complete and green: **256 tests pass, 11 skipped**
(0 skipped under `LEDGERLINE_LIVE=1`), `BUILD SUCCESS`. All eight `spec/openapi.yaml` paths,
the Redis cache-aside layer, the pricing engine, the PDF stub, the structured access log and
`/runtime-stats` are implemented. Spring Boot 4.1.1 + Hibernate 7.4.5 on Temurin 25.0.4+7,
BOM-pinned per O6's recommendation (nothing overridden).

Three adversarial rounds ran against it, the last two driving **both services against one
shared PostgreSQL 18.4 (`log_statement=all`) and one Redis 8.10, side by side**, diffing
response bytes, headers, statement logs, `MONITOR` output and stdout. What that established:

- Every 2xx and every error **body** is byte-identical to .NET. The `pricing_quote` golden
  vectors match byte-for-byte on both stacks, including `spec/pricing-algorithm.md`'s three
  worked examples padded to 50 items. Timestamps are exactly 6 fractional digits everywhere,
  including the all-zero-fraction case that `Instant.toString()` fails.
- The errors-map keys and frozen messages match; the `request_error`/`exception`/access-line
  stream is line-for-line identical on the 415, body-400 and 500 paths; `shutdown_complete` is
  the last line on both after SIGTERM.
- Cache key, TTL and invalidation ORDER match. The create transaction's statement set and
  ordering match.

**One measured finding rejected a brief rather than obeying it, and it was right.** The
cross-stack review was told `G1 Concurrent GC`'s time is concurrent wall time and must be
excluded from `gc.pause_total_seconds`. It measured instead: 30 forced concurrent cycles gave
`count=60` (exactly the two Remark + Cleanup **stop-the-world** sub-pauses per cycle) and
`timeMs=3`, against 40.8 ms of concurrent-mark wall time. So that bean's time IS STW and DOES
belong in the field. `spec/slo.yaml`'s gc_steady comment was corrected to match.

**What remains for G4** (corrected 2026-08-27 — the original text here said "none of
it is code" while `sut-java` existed in no compose file and the registry pinned the
slot `implemented=False`, i.e. the headline pair could not run at all; that wiring
landed with the review sweep):
1. The final read-only audit was stopped mid-run at shutdown; re-run it to sign off —
   **against the D12-reworked .NET service**, whose build posture changed
   (CreateBuilder, reflection STJ, no compiled model, ICU image), so the audit's
   byte-equivalence evidence must be re-established, not carried forward.
2. Fold the remaining disclosures into `docs/FAIRNESS.md` — the file was rewritten during P4
   (and again under D12) and is clean of round-1 numbers, but the P5-class asymmetries
   below need rows.
3. Known open asymmetries, each measured, each needing elimination or a FAIRNESS row naming
   the side it costs: Tomcat sends `Connection: close` on every 4xx/5xx where Kestrel does not;
   Content-Type charset and Content-Length-vs-chunked framing (~35 bytes/response);
   `redis_pool.in_use` reports SE.Redis's live in-flight count on .NET (measured 39 under load)
   against a structural 0 on Lettuce; binary RESULT formats (Npgsql immediately for every
   column, PgJDBC text until `S_1` then binary only for supported types, so `status` and
   `currency` stay text); Java's create path costs 5+N round trips against EF's 6, because
   `GenerationType.IDENTITY` disables Hibernate's JDBC batching and `spec/schema.sql` pins
   IDENTITY.

Known traps, in the order they will bite:

1. **Hibernate SQL shapes.** The biggest implementation risk. **Partly de-risked at P1 by a
   measured spike against real PostgreSQL 18.4 — see `docs/research/2026-08-21-measured.md`.**
   What that spike settled:
   - Modelling the FKs as scalar `Long` columns rather than `@ManyToOne` removes the
     EAGER-by-default trap entirely, and is what the frozen SQL wants anyway.
   - Q1/Q2 are shape-equivalent, and Hibernate does **not** add EF's trailing `LIMIT 1`.
   - Q4 emits `... returning id, created_at`, so the DB-generated `created_at` comes back
     with no second SELECT (`@Generated(event = INSERT)`).
   - Q3 emits `offset ? rows fetch first ? rows only` — a THIRD statement text, distinct
     from both the spec's `LIMIT 20 OFFSET $2` and EF's `LIMIT $2 OFFSET $3`.
   - `existsById` emits `count(*)`, not `EXISTS`; only a native query gives the spec shape.
   - **`GenerationType.IDENTITY` disables JDBC insert batching in Hibernate 7.4.5**
     (measured: 6 assigned-id rows -> 1 prepared statement; 6 IDENTITY rows -> 6). The item
     inserts therefore cost N round trips where EF Core costs 1, and `spec/db-access.md`'s
     "round-trip counts ASSERTED EQUAL" pin cannot hold. Rewrite that ledger at P1 and
     disclose; do not drop to hand-JDBC to hide it (that would rebuild round 1's
     ORM-vs-hand-written asymmetry).
   Expect a list of pinned, test-asserted deviations exactly like the three EF deviations in
   `docs/FAIRNESS.md`. Budget more time here than for the endpoints.
2. **Timestamps.** The wire contract is 6-digit-micros RFC3339. Confirmed at P1:
   `Instant.toString()` and `OffsetDateTime.toString()` both emit *variable* precision and
   fail the golden vectors on the first all-zero fraction. The formatter that holds the
   contract is
   `DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSSSSS'Z'").withZone(ZoneOffset.UTC)`
   — always 27 chars, truncating (not rounding), matching .NET's `ffffff`.
3. **Container CPU awareness.** Measured at P1: JDK 25 reads `cpuset` correctly —
   `--cpuset-cpus=0-5` gives `availableProcessors() == 6` with `ActiveProcessorCount` left
   at its `-1` default, so this trap does **not** reproduce on 25. Set
   `-XX:ActiveProcessorCount=${SUT_CORES}` anyway: it is free, it mirrors
   `DOTNET_PROCESSOR_COUNT` on the .NET side, and `HotSpotDiagnosticMXBean.getVMOption`
   reads it back with an `origin`, which turns "this is a default" into an artifact.
4. **Money.** int64 minor units → `long`. No `BigDecimal` on the hot path.

#### Code style

Match `services/dotnet/`. Measured density in the seeded repo: the round-1 Go service ran
13–25% comment lines, .NET 3–17%. Those comments are **load-bearing**: they pin decisions,
cite `spec/` sections, and record why a knob is set. A comment-free Java service next to
them would look more anomalous, not less.

The instruction is not "no comments". It is:

- Comments explain **why**, cite `spec/`, or pin a decision. Never narrate **what** the next
  line does.
- No `// Step 1:` scaffolding, no javadoc on obvious accessors, no emoji, no
  "comprehensive"/"robust"/"enhanced" in names.
- No defensive `try/catch` around code that cannot throw. No speculative abstraction: one
  implementation means no interface.
- Match the surrounding file's density rather than a global rule.

**Gate G4:** equivalence suite green against both SUTs. Golden vectors, frozen error
strings and access-log format byte-identical. `docs/FAIRNESS.md` deviation list updated
with every Hibernate shape that could not be held identical.

### P5 — Cross-stack parity audit — collapsed into G4 (2026-08-27)

No longer a standalone week: P4's three adversarial rounds already drove both services
against one shared PostgreSQL (`log_statement=all`) and Redis (`MONITOR`), byte-diffing
responses, statement logs and stdout — the evidence P5 was chartered to produce. What
remains is a SCRIPTED re-run of that diffing against the D12-reworked .NET build, its
findings written into `docs/FAIRNESS.md`, executed as part of G4's sign-off. The
checklist below is the scope of that script. Round 1 found four undisclosed live
asymmetries *after* publishing; the checklist exists so that cannot recur.

- Every SQL statement both stacks emit, captured from `pg_stat_statements` and diffed.
- Every access-log line, byte-compared on the same request sequence.
- Every error body and status code across the frozen error set.
- Round-trip counts per endpoint, both stacks.
- Cache key format, TTL behaviour, and invalidation ordering.
- Connection and statement lifecycle under load: pool acquire counts, prepared-statement
  counts on the server.

**Gate G5:** diff report committed. Every remaining difference is either eliminated or in
`docs/FAIRNESS.md` with the side it costs named.

### P6 — Rehearsal + resume drill — `DONE, gate G6 PASSED 2026-08-27`

Run `rehearsal-20260827T111408-fedora` (rehearsal tier, headline slot set), complete.
One more instrument defect surfaced and fixed on the way in — **host-setup.sh wrote
the CPU knobs before stopping `tuned`, whose shutdown restores `powersave`**, so
every first tune on a tuned-active box ended untuned and only a second run
self-healed; `host_readback` aborted on 32x `powersave` exactly as designed, and the
script now stops services first. The docker swap-allowance flake recurred on this
run's recreates too; the placement gate's `docker update` repair handled it live.

**The G6 questions, answered:**
- *JVM GC-steady inside the cap?* Yes — warmup census **Java 0% flagged** (the
  one `gc_steady` flag of the run was on .NET, 6.7%); the per-bean cadence source
  (`registry.JVM_GC.cadence_beans`) worked live; the 300 s cap binds nowhere (O4).
- *Where does heap committed settle?* **298 MB peak across all 13 Java measured
  windows**, against the 25%-default ~3 GiB ceiling at the real 12 GiB limit.
  **The ceiling never binds → D6 decided: one cell, defaults, done.** The
  `gc-generous` cell is deleted per D6's own rule (code, README, METHODOLOGY,
  FAIRNESS all updated; P8 halves in wall clock).
- *Both stacks cross inside the ladder?* Yes — per-slot knees **Java 6,439 rps /
  .NET 6,874 rps** (R1-A1 validated on the real pair).
- *Any unsatisfiable gate set?* No (R1-A3): every gate evaluable on both postures.
- *Rehearsal-grade direction* (NOT the headline; coarse 2.0-step ladder, n=2):
  Java ~47% LESS CPU per request (0.230 vs 0.435 ms/req), ~3-4x MORE anon RSS
  (646 vs 169 MB) — the classic tradeoff, cleanly measured, with the DB column
  showing near-parity DB cost (0.098 vs 0.111 ms/req).
- *Connection-sweep sanity:* db pool 24/24 fully opened on both (`pools_warm`).

**Crash-resume drill: PASSED on a real SIGKILL.** The runner was killed -9
mid-RCB-block (r1000.b2: java DONE, dotnet RUNNING). Resume re-ran doctor,
re-applied and read back host tuning, re-verified DB invariants, **reset the split
block WHOLE** (the done java probe's data dropped, both members re-run — fresh
artifacts timestamped after the resume), and the run completed `complete=True`.
Plus two unplanned real-crash resumes earlier the same day (P2's vegeta OOM and
placement abort), both continuing exactly where they stopped.

Full rehearsal tier with the real Java service. Answers:

- Does the JVM reach GC-steady inside `hard_cap_seconds`, or does the P1 cap need raising?
- Where does `gc.heap_committed_bytes` settle? **This decides D6.** Directional evidence
  already in hand (`docs/research/2026-08-21-measured.md`): on the same workload and the
  same `-Xmx`, G1 shrank committed to 70 MB while ZGC held ~1,040 MB. Confounded by a tiny
  heap on a 61 GiB host; re-measure at the real 12 GiB limit.
- Is the under-warmed flagging rate acceptable, per language? **This validates R1-A2.**
- Do both stacks cross the SLO inside the ladder? **This validates R1-A1.**
- Is any posture's gate set unsatisfiable by construction? **This validates R1-A3.**
- Connection-sweep sanity check: is 24/24 pathological for either stack?

Crash-resume drill, same phase: `SIGKILL` the runner mid-block, power-cut the box if
practical, then `--resume`. Verify probe-granular ledger state, that the interleave block
split by the crash is redone whole, that host tuning is re-applied and read back, and that
DB invariants re-verify.

**Gate G6:** rehearsal completes, numbers plausible, resume verified from a hard kill, D6
decided and written into this file and `docs/FAIRNESS.md`.

### P7 — Spec freeze + adversarial pre-registration review — `DONE, gate G7 PASSED 2026-08-27`

**`config_hash` at freeze: `e9fef8d3abbba8db034084d6295d3169a302a10a14ee1a13bc0d94b431fa3166`.**
Nothing under `spec/` may change past this point; a change refuses every resume
and re-analysis (`SpecDriftError`).

**The governing-rule walk found nothing to delete**: every gate `spec/slo.yaml`
declares (`p99_flat`, `gc_steady`, `pools_warm`) has a live enforcing
implementation, exercised on every probe of the two completed measured-tier runs;
`p999_flat` had already been honestly demoted to recorded-not-gated.

**The fresh-eyes adversarial review (a context-free agent, per this phase's
charter) found 1 critical + 5 major + 7 minor — all resolved before the freeze:**

- **C1 (critical): the registered workload was not the workload that runs.** The
  create generator GROWS the drawn item count toward 8 until the padded body can
  reach `target_bytes`-512, so live traffic realizes {6: ~67%, 7: ~16%, 8: ~17%}
  (mean 6.50, never <6) while the spec said "uniform 3..8" and priced Java's
  IDENTITY round-trip asymmetry from the distribution that does not run.
  Re-registered truthfully: mean Java create = **11.5** round trips vs EF's 6,
  **0.825** extra DB round trips per measured request (+22% over the old
  arithmetic, in the direction that costs Java). The guard test now derives the
  distribution from the real generator, so the doc cannot drift again.
- **M1**: the CO "stall test" claim overstated — no attack runs across the docker
  pause, so it proves freeze plumbing, not generator honesty. Spec + METHODOLOGY
  rewritten to claim only the scored per-probe achieved==offered check.
- **M2**: two stationarity keys hashed into `config_hash` with zero consumers
  (`supra_knee_policy`, `window_length_sensitivity_rate`) — deleted; and
  `stationarity.alpha` was never actually read (the value in force was a
  hard-coded default while a comment claimed the spec key was consumed) — now
  threaded through `analysis_bridge` by both consumers and recorded in the
  stationarity summary.
- **M3**: the knee-CI warning promised "the confirm stage produces the
  inferential CI" — a computation that does not exist (confirm runs ONE rate per
  slot). Warning and METHODOLOGY now state plainly that no inferential knee CI
  exists at the spec's rep counts and the cost-vs-rate curve is the mitigation.
- **M4 (the fifth category, found)**: Tomcat's `max-connections` default (8192)
  vs Kestrel's unlimited — a differing default, unequalized, undisclosed, with an
  asymmetric error-rate mechanism at the ladder wall. Kept at defaults per D12,
  now a FAIRNESS row; the "HTTP server limits identical by construction" phrase
  narrowed to the limits actually pinned.
- **M5**: the `redis_pool.in_use` FAIRNESS row two spec files promised — added.
- Minors: "placeholder" wording on frozen min-calls (frozen, with G6 evidence);
  stale two-cell/RUNBOOK/default-slot-set prose; a stale `CreateSlimBuilder`
  comment; the memory-metric's mechanical knee-rate coupling now stated in
  METHODOLOGY with the raw level published beside it; the TTL hit-rate
  rate-dependence mechanism named; the stationarity verdict explicitly labelled
  RECORD-ONLY; the GC row's "no flags set" tightened to "set explicitly to
  default-equivalent values".

The review's "not yet fit to freeze" items are the list above, all resolved;
frozen now.

**Freeze `spec/` here. Everything after this point is measurement.**

First, apply the governing rule: walk every declared gate in `spec/` and delete any without
a passing implementation. Round 1 shipped two declared-but-unimplemented gates and did not
surface their absence per probe.

Then a fresh AI agent, with no context from the work, reviews the frozen `spec/` and
`docs/` for fairness, correctness and internal consistency. **Before P8, not after.**
Pre-registration is the entire credibility claim; a fairness review conducted after the
numbers exist reviews rationalisations.

Reviewer brief: every dial is either a documented default, set identically on both sides,
or an explicitly labelled variant axis. There is no fourth category. **Find the fourth
category.** Round 1 had three overclaims and four undisclosed asymmetries; assume this one
has some too.

**Gate G7:** review findings resolved or explicitly accepted and recorded in
`docs/FAIRNESS.md`. `config_hash` recorded in this file.

### P8 — Capacity gate + measured run — `DONE 2026-08-28, gate G8 PASSED` (one run per revised D10)

The capacity gate is a real phase, not a formality. Hibernate's statement shapes differ
from both EF's and sqlc's, so round 1's verdict carries no information here. It can fire
its pre-registered levers (write share 0.15→0.10, cache hit rate 0.80→0.90, PG cores 3→4),
and those change the workload for every cell after it.

Per R1-A6, it runs **twice**: the projection gate before cell 1, and a re-gate at realized
rates once cell 1's knee is known.

Then the measured cells, three independent runs per D10.

**Abort criteria, pre-registered.** Round 1 discovered its 59.5% flagging at analysis time,
after every hour was spent. After cell 1 completes, run `analyze` on that cell alone:

- under-warmed fraction, either language, **> 25% → stop and fix** (round 1 was 59.5%);
- either stack fails to cross the SLO inside the ladder → **stop**, this is R1-A1 recurring;
- knee outside the measured ladder range → stop and fix `ladder_start_rps`;
- error rate above the 0.1% exclusion gate on more than a couple of probes → stop;
- capacity-gate verdict not `pass` → stop, unless deliberately setting
  `LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE=1`, which stamps the run `degraded`;
- any cell whose gate set is unsatisfiable by construction → stop, do not spend 15 h on
  posture evidence.

**Gate G8:** all cells done across three runs, ledger clean, `analyze` produces a complete
`report/`. *(Read through D10 as revised: ONE run. Passed 2026-08-28 — ledger
`state=complete`, `analyze` wrote `report/`, `verify` ok over 1516 artifacts.)*

**P8 evidence (run `full-20260827T174611-fedora`):**

- **One clean pass end to end.** Launched 2026-08-27 17:46 +04 (doctor 12/12 ok,
  native engine), `complete=True` 2026-08-28 ~10:10 +04. Zero crashes, zero resumes,
  no `LEDGERBENCH_ALLOW_*` / retry escapes used, `finalization_error: null`.
  Manifest identity: git_sha `ff1d98e` (clean, unchanged during run), config_hash
  matches the P7 freeze. Runtimes recorded: Temurin 25.0.4+7-LTS, .NET 10.0.11.
  Topology read back measured: each SUT cpuset 0-5 (6 cores) + 12 GiB, PostgreSQL
  8-10 + 8 GiB, vegeta 11-13, Redis 14; swap 0 everywhere, throttle counters 0.
- **Both capacity gates passed.** Projection `pass`; re-gate at REALIZED rates
  `pass-at-realized-rates` — PostgreSQL at 0.185 of its 3-core budget against the
  0.769 ceiling over the operating-point windows. R1-A6 closed as designed.
- **Cell complete.** Ladder 84/84 probes, per-language isotonic fits, confirm 30/30,
  soak 2/2; the in-run validation phase re-ran the equivalence suite (19 passed).
- **No pre-registered abort criterion fired.** Under-warmed: Java 0/53 = 0%,
  .NET 4/63 = 6.3% (rule: >25%). Both stacks crossed the SLO inside the ladder,
  knees inside the ladder range, error rate 1 non-2xx in 44.4M measured requests,
  all 86 stationarity series stationary.
- **Headline (own_knee scope, RCB-paired confirm blocks).** Knees: Java 4,799 rps,
  .NET 4,833 rps. App CPU: Java 0.2344 vs .NET 0.4840 CPU-ms/req → **−51.6%**
  [−51.8, −51.3] (Java cheaper); $0.0026 vs $0.0053 per million requests at the
  assumed $0.04/vCPU-h. Anon RSS: Java **+276%** (656 vs 176 MB in-window p99;
  `.NET better`). DB CPU: 0.101 vs 0.115 CPU-ms/req (**0.88x**, near parity —
  Hibernate's extra IDENTITY round trips did not move PostgreSQL's cost
  materially). `common_rate` scope agrees: −51.5%.
- **Caveats that must travel with the headline** (full text in `report/report.md`):
  (1) paired n=9 blocks against the pre-registered 10 (one block dropped for an
  under-warmed .NET probe) — verdicts labelled **directional only**; (2) the knee
  bootstrap intervals are **not inferential CIs** (crossing-bracket reps < 10);
  (3) **confirm-stage conformance overshoot**: at the fitted knees the realized
  mean p99 was 26.6–28.9 ms, not ~20. P2's expectation that the 1.2 step would
  land conformance near 20 ms did NOT hold — the fitted knee overestimates the
  rate a sustained 600 s window holds 20 ms at, for both slots by the same
  mechanism (worst single window: .NET soak 45.0 ms; Java soak 21.1 ms). The cost
  ratio is insensitive to this — 0.46x–0.50x everywhere from 1,493 to 5,350 rps —
  but "the promise was met at the knee" may NOT be claimed.
- **Replication vs the G6 rehearsal (D10's cross-tier check).** CPU ratio (~−47% →
  −51.6%), DB near-parity (0.885x → 0.88x) and anon-RSS ratio (~3–4x → 3.72x) all
  replicate across tiers. Absolute knees are 25–30% lower at full tier
  (4,799/4,833 vs 6,439/6,874): the rehearsal knee was interpolated across its
  deliberate 2.0-step ladder's 4,000→8,000 gap where the full knee sits on a
  measured 1.2-step grid — a tier artifact, ordering preserved, and both tiers
  overshoot at confirm the same way. Publish as the cross-tier replication check
  with exactly this framing.
- **P9 input — cold-start as pre-planned (time-to-gates-pass off the warmup
  artifacts).** Java passed its warmup gates at the structural floor (~183 s set
  by min-calls + flatness sampling) on **53/53** probes; .NET median 213 s,
  p90 305 s, **11/63** probes ran to the 300 s hard cap, 4 of them excluded
  under-warmed. The famous direction INVERTS at this workload. Pure process
  cold start (start → first healthy) is NOT captured in the artifacts and may not
  be claimed. GC activity for the same story: pause totals Java 16.5 s / 53
  windows vs .NET 130.9 s / 63 windows; heap committed max 328 vs 148 MB.

### P9 — Analysis + blog — `DONE 2026-10-12`

Editorial direction carries over from round 1: speed-focused, junior-readable, ends with a
how-to-choose guide. Point at the round-1 post for the EF Core / ADO / Dapper axis rather
than re-deriving it.

Publish the cost-vs-rate curve alongside the single-operating-point ratio, with the rate
stated (R1-A1). Verify round 1's post and repo are published and stable before linking.

**P9 evidence.**

- **Figures.** `docs/figures/fig0` to `fig7`, drawn by `bench/figures.py`
  (`make figures RUN=<run_id>`). Every series is recomputed from `cells/**` under
  `analyze`'s exclusion rule, every number on a chart is computed or read from
  `report/fits.json`, titles included, and the script refuses to draw when its
  per-request CPU disagrees with the report's. The cost-vs-rate curve is `fig2`, with each
  stack's own-knee point marked (R1-A1).
- **Post.** Written at `docs/blog/round2-devto.md`, a local working copy that is
  gitignored, as round 1's was. Title framed as Java vs C#; no round-1 number appears in
  it; the cloud list prices in it were re-read from AWS, Azure and Google on 2026-10-12
  and had not moved.
- **O5 resolved.** The warm-up result is a section of the post, claimed as time-to-settle
  under traffic and explicitly not as start-up time.
- Round 1's post and repository were confirmed live before linking.

### P10 — Publication — `DONE 2026-10-12`

Remove `PROGRESS.md`, `CLAUDE.md`, scratch and dead code. Public README as an onboarding
document with the headline attached, not a results announcement. Ship the raw artifacts as
a GitHub Release with checksums.

**What was done instead of the first sentence above, and why.** On Haik's decision of
2026-10-09, `PROGRESS.md`, `CLAUDE.md`, `docs/p1-plan.md` and `docs/research/` **stay**,
frozen. About 600 comments across `bench/`, `services/`, `infra/` and `spec/` cite the
identifiers these files define; `spec/slo.yaml` cites this file and cannot be edited
without breaking `config_hash`; and the checksummed `report.md` cites it too. Deleting
the glossary would have orphaned every one of those citations to tidy a root directory.
Round 1 could delete its equivalents because its code did not cite them.

**P10 evidence.**

- The superseded blog drafts are deleted and the post source is local-only.
- `README.md` is an onboarding document with the headline and its three caveats attached.
  `results/README.md` (what is and is not published, how to verify it, the caveats) and
  `CITATION.cff` exist.
- CI is green. It had been red on every push since the seed commit, on one help-text
  assertion that failed only under GitHub's forced-colour terminal. Six tests that
  assumed POSIX paths or `os.geteuid` now pass on Windows too, which the README's any-OS
  claim for the pure-logic suite needs.
- The repository description and topics are set. The homepage waits for the post's URL.
- **Not done:** no GitHub Release carries the raw traces (see the scope table).

---

## Timeline

Closed 2026-10-12. P0–P8 ran from 2026-08-20 to 2026-08-28 (the measured run itself took
~16.5 h wall clock, not the estimated week). P9–P10 closed on 2026-10-12; most of that
gap was the post waiting for review, not work. (The original plan read 9–12 weeks
end-to-end with three measured runs; P1's tripled estimate was real — the round-1 defect
fixes were engineering, not renaming.)

---

## Open decisions

| # | Question | Owner | Needed by |
|---|---|---|---|
| O1 | ~~Repo name and visibility~~ | RESOLVED | `HaikAsatryan/java-vs-dotnet-benchmark`, private. Public at P10. |
| O2 | ~~D6 heap policy: one cell or two~~ | RESOLVED at G6: **one cell, defaults** (heap committed peaked at 298 MB vs the ~3 GiB default ceiling; see D6) | — |
| O3 | **CLOSED 2026-10-09: no variant cell was run; the published result is the one headline cell.** ~~Quarkus or native-image variant cell?~~ Doubles P4 if yes. Recommendation: decide after G6, only if headline cells are already running clean. | Haik | after G6 |
| O4 | ~~`warmup.hard_cap_seconds`: keep 180 or raise to 300~~ | RESOLVED | **300**, landed in `spec/slo.yaml` at P1. G6 confirmed it binds nowhere: 0% of Java probes and 6.7% of .NET probes flagged, none on the cap. |
| O5 | **RESOLVED 2026-10-09: its own section of the post, claimed as time-to-settle only.** ~~Does the cold-start curve get its own blog section, or an appendix?~~ This row used to say "Java loses this badly" — **P8 measured the opposite**: time-to-gates-pass Java ~183 s (floor) on 53/53 probes vs .NET median 213 s with 11/63 at the 300 s cap. Pure process cold start is not captured, so the section can only claim time-to-warm-under-load. | Haik | P9 |
| O6 | ~~Version-currency policy~~ | RESOLVED 2026-08-27, as recommended: **each stack ships what its framework ships** (BOM-managed on Java, SDK/metapackage-managed on .NET), directly-referenced packages at latest stable on both sides at run start, both recorded in the manifest. *(As run: the manifest records runtime versions and image digests; library versions are the pins in `pom.xml` and `Ledgerline.csproj`.)* D8 is read through this rule. Overriding the BOM on five artifacts would be non-idiomatic and is ranked #2 on the round's own hostile-reviewer list ("they patched Java until the number improved"). | — | — |
| O7 | ~~SE.Redis 3.1.13 refresh timing~~ | RESOLVED 2026-08-27: refresh the directly-referenced packages (both stacks) immediately before P8's measured run, with the manifest recording the versions — so the pin is not chosen after seeing numbers. A P8 pre-step, not a P1 edit. | — | P8 |

---

## Operating notes

`docs/RUNBOOK.md` is the operating reference: measured runs are Linux-only, never run
measured tiers on Windows, never edit `spec/` mid-run, state lives in
`results/<run_id>/ledger.json`.
