# RUNBOOK: operating a measured Ledgerline run

Operational guide for the measured tier. The Tier-1 tuning steps are written
against the intended measurement host, a Ryzen 9 7950X on Fedora 43; a different
Linux host runs the same procedure with the Tier-2 steps detecting and skipping
what does not apply. Everything here is
Linux/Fedora only: the measured tiers refuse to run on Windows. All `ledgerbench` commands
run from `bench/`; `make` targets run from the repo root.

## Prerequisites

- **Docker: NATIVE engine** (rootful; cgroup v2 unified) running on the host.
  Docker Desktop does NOT qualify: its linuxkit VM hides container cgroups/PIDs
  from the host, so the headline CPU/memory sampling reads nothing and cpusets
  pin to vCPUs. Install `docker-ce` (or `moby-engine`), `systemctl enable --now
  docker`, and `docker context use default`. `ledgerbench doctor` enforces this
  (`docker_native_engine`).
- **uv** (Python runner) on PATH.
- **Root or NOPASSWD sudo** for the operator: host tuning (`host-setup.sh --tune`)
  re-runs at every `run-all` start (and on resume after a reboot) and needs root;
  the runner invokes it via `sudo -n`, which must not prompt. `ledgerbench doctor`
  checks this (`root_or_sudo`).
- **No foreign workloads**: stop unrelated containers/compose projects before a
  measured run; they sit outside the benchmark cgroups but compete for cores,
  LLC, and memory bandwidth.
- **make** (the targets are thin wrappers over `uv run ledgerbench …` / `docker compose …`).
- **Host tuning** applied via `make host-setup` (runs `infra/host-setup.sh --tune`). What it
  changes, all reversible and recorded per-action as JSON into a state file:
  - CPU governor → performance; turbo **boost off**; shallow C-states.
  - THP → madvise; `default_smp_affinity` steered onto the housekeeping core (which
    governs only interrupts allocated *after* the write; the ones already on the SUT
    cores — the nvme blk-mq queues — are managed interrupts the kernel refuses to move).
  - A systemd `CPUAffinity` drop-in pinning host daemons (dockerd, containerd-shims,
    journald, systemd) to the CCD1 housekeeping core. It applies to units started *after*
    it: an already-running dockerd keeps its old mask until it restarts, which the
    read-back records per daemon rather than assuming away.
  - PGDATA on btrfs: `chattr +C` (CoW off). `noatime` is **detected and reported**, never
    applied — remounting a live docker volume is left to fstab.
  - Stops `tuned`, `power-profiles-daemon`, `irqbalance`.
  - Tier-1 steps are exact-to-this-hardware; Tier-2 steps detect-and-skip on a different box
    and are tagged as such in the run manifest.
  - **Read back** by the `host_readback` phase immediately after, on every run start and
    every resume: `manifest.host_state` carries a row per attribute (value, expected, tier,
    the paths read) and `results/<run_id>/host_state.json` is the standalone artifact. A
    Tier-1 mismatch aborts the run *after* the artifact is written. A Tier-1 attribute
    this box cannot evaluate at all (no `cpufreq/boost` knob, say) is neither: it lands in
    `host_state.tier1_not_evaluable`, so an empty `tier1_failures` means "checked and
    matched" and nothing else. Only values `host-setup.sh` actually sets are Tier-1; the
    rest are recorded with the reason they are not gated. The knobs the read-back compares
    against are forwarded to `host-setup.sh` explicitly through `sudo -n env ...`, so
    exporting `HOUSEKEEPING_CPUS` (or any other of the five: `SUT_CPUS`, `PGDATA_PATH`,
    `STOP_SERVICES`, `SYSTEMD_DROPIN`) retunes the box and the expectation together.
  - **Restore** with `make host-restore` (replays the state file to undo every mutation).
    A reboot also resets the live knobs (governor/boost/pinning); resume re-applies them.

## Full run sequence

Run these from the repo root, in order:

1. `make doctor`: preflight checks for docker, cgroup v2, core count and host RAM against
   the resource ledger, disk, every implemented slot's build context, binaries.
2. `make seed-full`: seed 1M customers / 5M invoices via the dockerized seeder
   (deterministic, seed 42, binary COPY, invariant-checked). Budget roughly an hour.
3. `make host-setup`: apply Tier-1 host tuning (see above).
4. `make bench-all`: the full measured run, in the order capacity gate → validation
   (equivalence) → the headline cell → selected variants. Budget ~15 h per cell
   (ONE headline cell since G6 decided D6, plus any `LEDGERBENCH_RUN_VARIANTS`
   cells) plus ~2 h of gates/seed.
5. `make analyze RUN=<run_id>`: isotonic knee fits, paired-difference stats, CIs from
   `results/<run_id>/`, written to `results/<run_id>/report/`. Never hand-edit
   `report.md`; re-run this instead.
6. `make figures RUN=<run_id>` (optional): redraws `docs/figures/*.png` from the run's
   own artifacts. `bench/figures.py` recomputes every series from `cells/**` under the
   exclusion rule `analyze` applies, reads the knees and the paired verdict from
   `report/fits.json`, and refuses to draw if its per-request CPU disagrees with the
   report's, so run `make analyze` first. No number on a chart is typed by hand; the cloud
   prices behind the two cost figures are a dated constant in that file, not a measurement.

**Check the phase verdicts before you trust the report.** The runner **aborts** the
measured cells on a non-passing capacity gate (`phases._enforce_gate`; opt out with
`LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE=1`, see the env vars below), and `analyze` excludes
under-warmed probes itself. Neither of those replaces reading the verdicts: a gate can
pass and a cell can still be posture-only. Before publishing anything from a run, read:

- `results/<run_id>/capacity-gate/verdict.json`: a `verdict` of `db-imposed-shared-knee`
  means the gate exhausted its levers without reaching headroom. On a non-passing verdict
  the levers are deliberately **not** applied (a partly-moved workload matches neither the
  spec mix nor a headroom-clean one) and the run stops; the verdict also carries
  `levers_applied` / `levers_not_applied`, and the manifest carries `degraded` plus
  `degraded_allowed_by`. `rungs: []` with a `pg_knee_rps` that is exactly the first rung's
  achieved rate scaled by the core ratio means the create-only ladder never located a knee.
  A healthy verdict reads `pass` with `usable_rungs` equal to `total_rungs` and
  `levers_fired: []`. `pg_knee_is_lower_bound: true` means the create-only ladder tracked
  every rung without breaking, so the PG figure is a floor rather than a located knee.
- `results/<run_id>/manifest.json` → `capacity_gate.regate`: the **second** gate, evaluated
  after cell 1 against the rates the cells actually chose. It is a direct utilization
  measurement (PostgreSQL cgroup `cpu.stat` over the operating-point windows ÷ pg cores ×
  wall seconds), fires no levers, and a failure stops the remaining cells and retroactively
  stamps cell 1.
- `results/<run_id>/ledger.json` → `phases.validation`: the cross-service equivalence
  suite is the gate.
- `results/<run_id>/ledger.json` → per-cell `under_warmed` maps: probes flagged here are
  excluded from fits, confirms and the resource roll-up: `analyze` filters them and
  prints the used-vs-flagged counts per row, marking rows that had no measurement-grade
  window at all as posture-only (`*`). Each probe's record names every gate it was
  evaluated against and every gate it failed, so a flagged probe can be read without
  opening its sidecar.
- `results/<run_id>/manifest.json` → `knobs_by_slot` is written once per run, not per cell,
  so it describes the last cell that ran, not the headline cell. Read `knobs_by_cell`
  instead, which records every slot's knobs per cell id.

Smoke rehearsal on any OS (measures nothing): `make smoke`.

### Operator environment variables

These knobs live in the operator's environment rather than in `spec/`, because `spec/` is
frozen at run start and no number in it may be invented after the fact. All are read by
`bench/ledgerbench/phases.py`, except `LEDGERBENCH_SLOT_SET`, which is read by
`bench/ledgerbench/registry.py`.

| env var | effect |
|---|---|
| `LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE` | `1`/`true`/`yes`/`on` lifts the capacity-gate abort: the run proceeds on a non-passing verdict and is stamped `degraded: true` + `degraded_allowed_by` in both the ledger and `manifest.json`, so every downstream artifact carries the caveat. Unset (the default) = a non-passing gate stops the run. Use only when a degraded run is deliberate. The stamp is run state: once recorded, a resume replays it: a recovery shell does not need the variable re-exported (an UNstamped degraded gate keeps blocking on resume until the operator opts out, and that late opt-in is then stamped too). |
| `LEDGERBENCH_RETRY_EXCLUDED_RUNGS_AGAIN` | `1` grants ONE more retry round to rungs the one-shot under-warm retry already consumed. The retry guard is ledger state, so a rung whose probes stay under-warmed/excluded after its retry raises the same `PhaseError` on every `--resume`; the warmup cap cannot be raised mid-run (it is inside the frozen `config_hash`), so the exits are this env on the next resume (recorded per rung as `rewarmed_rungs_again`) or a new run. |
| `LEDGERBENCH_ALLOW_SUB_KNEE_LADDER` | `1`/`true`/`yes`/`on` lifts the ladder-ceiling abort. A slot that reaches the ladder's rung ceiling WITHOUT crossing the p99 SLO has no operating point — the headline metric is cost AT the crossing rate — so the default is to abort the cell and name the slot. With the opt-out the cell records anyway and its **ladder stage** is stamped `degraded` + `degraded_allowed_by` in the ledger, with `ceiling_reached_without_crossing` naming the slot(s), so the caveat travels with the cell. The real fix is a higher rung ceiling or start rate, not the opt-out: on the full tier those are `ladder.rung_ceiling` and `ladder.start_rate_rps_default` in `spec/slo.yaml` (a spec edit, so a new run), and the reduced tiers set `ladder_max_rungs` in their `ScaleProfile` in `bench/ledgerbench/phases.py`. |
| `LEDGERBENCH_SLOT_SET` | Names the slot pair the run compares: `headline` (java vs dotnet) or `shakedown` (dotnet-efcore vs dotnet-ado — the P2 harness-shakedown pair; `headline` is the default since the Java slot landed). Asking for a pair whose build context does not exist is refused with the runnable sets named. The pair is pinned into the ledger at run start (`ledger.slot_set`); a resume under a different value is **refused**, not silently overridden, with a message naming the pinned set — a run must keep comparing the pair it started with. |
| `LEDGERBENCH_CACHE_TTL_SECONDS` | Integer seconds. The gate's `cache_hit_rate` lever is realized (per `spec/cache.md`) as a raise of `CACHE_TTL_SECONDS`, but `spec/slo.yaml` pre-registers no TTL value. Without this var the lever is recorded **not applied**, which fails the gate rather than letting a lever count as fired while nothing changed. Set it to make that lever real; it is pushed to the services as compose env alongside the cpuset move. It has no effect on a run where that lever never fires. |
| `LEDGERBENCH_BODY_POOL_REQUESTS` | Integer ≥ 1000, **default 1000000**. Requests the shared request-**body** pool is sized for on the measured tiers; the create/quote split comes from the mix. It is a FLOOR on the working set, not the size of any one targets file: each attack draws `rate × duration × 1.02` targets from its own PCG64 stream, so the pool only bounds how many distinct customers the create path can name. Lowering it shrinks that and makes the create path's customer lookups more cache-resident, which changes what the run measures rather than just how long it takes. Bench-side dial: no `spec/` edit, no `config_hash` change. Pinned into the ledger at run start; on resume the pin wins over the environment (a mismatch prints a NOTE), so set it BEFORE starting the run. The predecessor `LEDGERBENCH_TARGET_COUNT` sized the one targets file a run replayed; it is **refused with an error**, not ignored, so a shell that still exports it cannot silently fall back to the default. |
| `LEDGERBENCH_RUN_VARIANTS` | Comma-separated variant cell ids to EXECUTE in the variants phase (e.g. `variant.npgsql-default-prepare`); unknown ids abort. Default: variants are declared but not run. The selection is validated and pinned into the ledger at run start (before any phase runs), so a resume, including the documented naked-shell recovery, replays the original selection regardless of the environment. Each executed variant is a full cell (ladder→fit→confirm→soak, ~15 h at full scale). |

## Unattended operation

A full run takes about 17 hours with no operator present: ~15 h for the headline cell plus
~2 h of gates and seed (the published run took about 16.5 hours). Each opted-in variant
cell adds ~15 h. **A UPS is strongly recommended.** A power
cut is not catastrophic but it is not free: you lose the **in-flight probe** plus **one redone
interleave block** (the runner redoes any interleave block split by the crash to preserve
pair integrity), roughly **20-30 minutes** of work. Everything completed before that is
preserved by the ledger.

### Recovery after a power cut / reboot

1. **Start docker** (`systemctl start docker` if it is not set to start on boot).
2. **`cd bench`**.
3. **`uv run ledgerbench status`**: see which phase/cell/probe was in flight and what is
   pending next.
4. **`uv run ledgerbench run-all --scale full --resume`**: resumes the most recent unfinished
   run. Host re-tuning (the reboot reset governor/boost/CPU pinning) and DB invariant checks
   run automatically as part of the resume protocol; doctor is re-run; vegeta targets are
   re-shipped. To resume a specific run instead: `… --resume --run-id <ID>`.
5. **Verify** with `uv run ledgerbench status` that the redone interleave block shows up
   (the split block is re-queued and re-run, not skipped).

Equivalent make targets: `make bench-status` (status, any OS) and `make bench-resume`
(resume, Linux-gated).

## What the ledger guarantees

State lives in `results/<run_id>/ledger.json`, written atomically at probe granularity.

- **Done probes never re-run.** A completed probe is recorded and skipped on resume; only the
  in-flight probe and the crash-split interleave block are redone.
- **config_hash freeze.** The ledger pins a hash of `spec/` at run start. Resume refuses if
  `spec/` changed; the measured matrix must stay constant across the whole run.
- **Artifacts checksummed before a probe counts.** A probe's published outputs are hashed into
  `<slot>.probe.sha256` beside them before the ledger marks the probe done. A half-written
  artifact is not a reachable state at all: every artifact writer in the runner goes through
  the atomic temp-file + rename in `util/paths.py`.
- **One checksum map, recomputed whole.** `CHECKSUMS.sha256` at the run root covers exactly
  the published artifacts, and **git states which those are**: the map's paths are `git
  ls-files --cached --others --exclude-standard` run in the run directory — tracked plus
  untracked-and-not-ignored, which is the definition of what a clone contains. Nothing on
  this side reads a rule, so nested `.gitignore` files, `.git/info/exclude`, a
  `core.excludesFile`, directory-only rules and `!negations` are all already resolved in the
  answer. The map is rewritten from scratch at the end of the run and again by `analyze` — a
  resume or a re-run block cannot give a path two hashes. The per-probe lines are run-relative
  and pass the same filter, so they appear verbatim in the map. Check it with `sha256sum -c
  CHECKSUMS.sha256` from the run directory, or `uv run ledgerbench verify <run_id>`, which
  gives the same verdict on the same bytes and additionally fails on an in-scope artifact the
  map does not account for, on a listed path git no longer publishes (which passes `sha256sum
  -c` here and fails it in a fresh clone), on a path listed twice, and on an absent or empty
  map.
  Two consequences worth knowing before you go looking for the file:
  - **Smoke and rehearsal runs get no map at all.** `.gitignore` excludes `results/smoke-*/`
    and `results/rehearsal-*/` wholesale, so those tiers have no publication scope to
    checksum. `verify` on one says so and exits 1. The one rehearsal this repository
    carries, `results/rehearsal-20260827T111408-fedora`, had its `ledger.json`,
    `manifest.json` and `report/` added by hand as the replication check; it has no map
    either, and `verify` on it exits 1 for the missing map.
  - **If git cannot answer, nothing is written.** No repository, no `git` on `PATH`, a broken
    index — the writer raises rather than fall back to a guess, because a map whose
    coverage cannot be stated cannot be checked.

## Troubleshooting

- **status says `config_hash mismatch`** → `spec/` was edited after the run started. The run
  cannot resume against a changed matrix. Start a fresh run (`make bench-all`); do not edit
  `spec/` mid-run.
- **vegeta targets missing after reboot** → expected and harmless; they are re-shipped
  automatically by the resume protocol. No manual step.
- **docker volumes after reboot** → volumes survive a reboot. PGDATA is crash-safe with
  `fsync=on` / `synchronous_commit=on`; on resume the DB invariants are re-verified
  (counts, balance = sum of items) before any probe records.
- **host knobs reset after reboot** (governor back to default, boost back on, pinning gone) →
  expected; resume re-applies host tuning automatically. To apply manually: `make host-setup`.
- **doctor fails on resume** → resume re-runs doctor by design; fix what it reports (docker
  down, a core busy, disk full) and re-run the resume command.
- **capacity gate says `db-imposed-shared-knee`** → the gate could not find 1.3× headroom
  even after its levers, and **the run aborts** rather than measure the cells against a DB
  whose headroom was never established. Options: give PG more cores in the compose overlay (`PG_CPUS`; the default
  in `compose.bench.yaml` is 3 cores; the gate's `pg_cores` lever now emits the `PG_CPUS`
  / `VEGETA_CPUS` move itself and the runner re-creates the containers on the new cpusets,
  but only on a run where the lever makes the gate pass), lower the create share in
  `spec/workload.yaml` *before* starting a run (never mid-run: `config_hash`), or re-run
  with `LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE=1` and publish a run stamped `degraded`
  alongside the verdict. Do not reach for `synchronous_commit=off`; it is pre-registered
  as never.
- **run aborts in the `host_readback` phase** → the box does not read back what
  `host-setup.sh` applied (governor, boost, THP `enabled`, the CPUAffinity drop-in, or a
  service that is still running). `results/<run_id>/host_state.json` names the attribute
  and the path; it is written *before* the abort. Re-run `make host-setup` and resume. Do
  not add an opt-out: every Tier-1 row has a setter in `host-setup.sh`, so a mismatch means
  the tuning did not take.
- **run aborts on the first measured window with "container placement does not match"** →
  a container came up on cores `infra/compose.bench.yaml` did not ask for, usually because
  a SUT slot has no entry in that overlay (it then runs unpinned across every logical core)
  or because a container predates a `PG_CPUS`/`VEGETA_CPUS` override. The message names the
  cgroup, what it read and what was asked for. Fix the overlay or recreate the container
  (`docker compose ... up -d --force-recreate <service>`) and resume; every CPU- and
  memory-per-request figure in the cell would otherwise be measured against a core budget
  the container does not have.
