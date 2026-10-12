# infra/: decisions and divergence notes

Scope: the compose topology, the vegeta load-generator image, and the Fedora host
scripts. Records the non-obvious infra choices and where they diverge from the
obvious default. Conflicts resolve toward the LOWEST cross-language divergence
risk; `spec/` is the source of truth for any contract.

## Image tags (verified via `docker manifest inspect`, read-only)

| image | tag chosen | verified | digest-pin |
|---|---|---|---|
| postgres | `postgres:18.4-bookworm` | EXISTS | pinned |
| redis | `redis:8.10.0-trixie` | EXISTS (latest stable; 8.5+ ships trixie-based images only, no bookworm tags) | pinned |
| golang (vegeta build) | `golang:1.26.5-bookworm` | EXISTS | pinned |
| debian (vegeta runtime) | `debian:bookworm-slim` | EXISTS | pinned |
| prometheus / grafana / cadvisor | pinned tags, EXPLORATORY ONLY | not load-bearing | pinned |

Redis 8.10.0 is the newest stable tag (trixie-based; upstream stopped publishing
bookworm variants after 8.4). Valkey is documented as a drop-in in the project README
(spec/cache.md requirement); not used here.

All `FROM`/`image:` references carry an `@sha256:` digest pin so a tag re-point
upstream can never silently change what runs. Re-resolve digests deliberately,
never implicitly.

## Load-generator image

The image ships vegeta only. It is built from source via
`go install github.com/tsenart/vegeta/v12@v12.13.0` in the locked Go toolchain,
so its version is reproducible regardless of release artifacts.

## Compose topology

- Build contexts are relative to `infra/` (the compose file's dir), so `..` ==
  repo root and `../spec/...` reaches the spec mounts. Compose is always invoked
  from repo root (`-f infra/compose.yaml ...`), matching the Makefile.
- Profiles: postgres + redis have NO profile (always start). One profile per
  SUT (`java`, `dotnet`, `dotnet-b`), plus `load` and `seed`, gate the rest.
  The runner starts exactly ONE SUT profile per probe, so two SUTs are never
  co-resident.
- Port map (host->container): postgres 54320->5432, redis 63790->6379,
  sut-java 8081->8080, sut-dotnet 8082->8080, sut-dotnet-b 8083->8080. SUTs on
  distinct host ports so the smoke path can run them sequentially without
  clashes.
- Identical docker `json-file` logging (max-size 512m, max-file 3) on BOTH SUTs
  and every infra service (spec/logging.md: identical compose log config).
- Per-SUT pdf volumes (`pdfstub-java`, `pdfstub-dotnet`, `pdfstub-dotnet-b`,
  one per SUT slot) so the pdf-stub write target is isolated per slot and
  recreated per run.
- Env passthrough: every spec env var has a default matching spec
  (DB_POOL_MAX=24, DB_POOL_MIN=24, DB_CONN_LIFETIME=3600, REDIS_POOL_MAX=16,
  CACHE_TTL_SECONDS=300, LOG_QUEUE_LEN=8192, BODY_MAX_BYTES=65536,
  MAX_AUTO_PREPARE=20, AUTO_PREPARE_MIN_USAGES=5, DATA_LAYER=efcore,
  DOTNET_GCDynamicAdaptationMode=1, PDF_DIR=/data/pdf). `MAX_AUTO_PREPARE=20` is
  D4, not Npgsql's default of 0: PgJDBC auto-prepares out of the box, so leaving
  Npgsql off would measure the prepare lever rather than the runtimes. The
  runner overrides per cell; defaults are the headline values. On the bench
  overlay each SUT's processor-count knob is pinned to the SUT core count and
  the runner asserts the SUT's reported `processor_count` from
  `/runtime-stats`.

## SUT liveness: the runner polls `/readyz`; SUTs carry no container healthcheck

Neither SUT image declares a `HEALTHCHECK` or ships a healthcheck helper. The
SUTs carry NO container healthcheck in the base compose, and the runner polls
`/readyz` over the published port before any traffic: the identical convention
for every SUT, with zero image divergence, so readiness stays runner-observed.
postgres and redis keep their own healthchecks, which is what
`depends_on: service_healthy` and `up --wait` gate on.

## Connection env vars: discrete `PG_*` names via the `x-sut-env` anchor

The compose passes every SUT the SAME environment through the `x-sut-env` YAML
anchor in `compose.yaml`: discrete connection vars (`PG_HOST`, `PG_PORT`,
`PG_DB`, `PG_USER`, `PG_PASSWORD`, `REDIS_ADDR` as `host:port`) plus the
pool/cache/logging knobs and `PORT`. Each service assembles its own
driver-native connection string from those inside its config layer (Npgsql
keyword form, JDBC URL form). A pre-assembled `DATABASE_URL` would need a VALUE
shape that differs per driver; the discrete names make the env contract
identical in both name AND value, and the anchor makes that parity structural
(compose merges language-specific vars over it per service) rather than
something a reviewer diffs by eye.

The seeder reads no connection env at all: it is flag-driven, and the runner
invokes it as `compose run --rm seed --dsn <superuser DSN> ...` because it
needs DDL + COPY + sequence restart + grants (spec/db-access.md seeder
contract); the SUTs use the least-privilege `ledger` role
(spec/postgres/initdb/01-role.sql).

## Overlay model

- `compose.bench.yaml`: cpuset + mem_limit only. NO `cpus:` quota: a CFS `cpus:`
  quota throttles in fixed periods and injects p99 stalls, which cpuset pinning
  avoids.
  There is one bench overlay and it pins cpusets; coordinated-omission safety
  rests on the two checks `spec/slo.yaml` pre-registers (per-probe
  achieved-vs-offered, and smoke's `docker pause` stall check), so the overlay
  set is exactly `compose.bench.yaml` plus the observability stub below.
  The cpuset ids (SUT 0-5, PG 8-10, vegeta 11-13, redis 14, housekeeping 15)
  assume CCD0 = cores 0-7 and CCD1 = cores 8-15 on the 7950X with SMT siblings
  idle; the `host_readback` phase records the topology each run reads back in
  the manifest. All cpusets are env-parameterized, so the capacity-gate
  core-rebalance lever (vegeta core -> PG, used if PG becomes the capacity
  gate) is an env edit, never a file edit.
- `compose.observability.yaml`: prom+grafana+cadvisor stub, EXPLORATORY ONLY,
  NEVER a recorded data source (cgroup v2 is the only source). Big warning
  banner in the file.

## Host scripts (Fedora-only)

- `host-setup.sh` / `host-restore.sh`: bash, LF, idempotent, `--tune`-gated
  (no `--tune` = detect-and-report only, zero mutation). Every action emits one
  JSON line for the manifest. `--isolate` only CHECKS the kernel cmdline
  (isolcpus/nohz_full need a reboot; never edited live). On a non-Linux
  platform both scripts emit one skip line and exit 0.
- Reversal is driven by a JSON-lines state file (`/var/tmp/ledgerline-host-state.json`,
  override `LEDGER_HOST_STATE`); restore replays it in reverse and clears it
  (idempotent). Tier-1 (exact) vs Tier-2 (directional, detect-and-skip) recorded
  per action via the `tier` field.
- Covered: governor->performance, boost off (cpufreq/boost or amd_pstate
  per-policy), shallow C-states (cpupower idle-set), THP->madvise, IRQ steering
  off SUT cores, global systemd `CPUAffinity` drop-in (the key host-hygiene fix:
  pins dockerd/shims/journald/systemd to the housekeeping core so container
  cpusets are not undermined by host daemons), pgdata chattr +C + noatime report,
  conflicting-service stops (tuned, power-profiles-daemon, irqbalance).
- noatime and isolcpus are detect-and-report (live remount of a docker volume's
  backing fs and live bootloader edits are too intrusive; left to documented
  manual/fstab/reboot steps). Recorded as Tier-2/checked.
