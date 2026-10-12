# results/

Per-run artifacts land here as `results/<run_id>/`:

- `ledger.json`: crash-safe run state (phases, per-cell probe progress, per-probe p99,
  which warm-up gates were evaluated and which failed); drives `--resume`.
- `manifest.json`: full run provenance (versions and image digests read back, host state
  read back, knobs, capacity gate and re-gate, config hash, achieved-vs-offered per probe).
- `host_state.json`, `doctor.json`: the tuned host values as read off the box, and the
  preflight verdicts.
- `capacity-gate/`: the PostgreSQL capacity ladder and its verdict, run before any cell.
- `cells/<cell>/<stage>/rate<N>/block<M>/`: per-probe evidence. Vegeta summaries, warm-up
  gate records, cgroup CPU/memory deltas for every sampled cgroup, `/runtime-stats`
  window summaries.
- `report/`: analysis output (`make analyze RUN=<run_id>`): `report.md` and `fits.json`.
- `CHECKSUMS.sha256`: sha256 over exactly the files this repo publishes for the run.

Small text and JSON summaries are committed. Smoke runs are not.

**The raw per-window traces are not published.** The cgroup 50 ms cpu/mem CSVs, the
per-second p99 series, the per-poll `/runtime-stats` traces and the raw vegeta `.bin`
captures are gitignored, and no GitHub Release carries them. Everything needed to
re-derive every published number is in git: the per-probe `*.summary.json` and
`*.warmup.json` roll-ups, `ledger.json`, `manifest.json`, `capacity-gate/` and
`report/`. The traces add per-window detail behind those roll-ups and are regenerable by
re-running the tier. If you need them for a specific claim, open an issue and say which
probe.

## The committed run: `full-20260827T174611-fedora`

Full scale, Tier-1 tuned host, config hash `e9fef8d3…`. One cell,
`headline.mixed.gc-default` (flag defaults on both sides), **116 measured windows,
44,403,343 requests**, status codes `{200: 37,747,565, 201: 6,655,777, 500: 1}`. About
16.5 hours of wall-clock over 2026-08-27 and 2026-08-28, in one pass: no crash, no resume,
no operator escape hatch used.

| stage | what ran | probes |
|---|---|---|
| ladder | 14 rungs from 500 to 5,350 req/s (x1.2 steps), 3 interleaved Java/.NET blocks per rung, until **each** stack's own p99 crossed 20 ms | 84 |
| confirm | 10 randomized blocks: Java at its knee, .NET at its knee, and .NET again at Java's knee for the common-rate contrast | 30 |
| soak | one long window per stack at its own knee | 2 |

### Verifying it

Provenance is anchored two ways, and both resolve in this repository.

```sh
# Run from the repository root; each command runs in a subshell and leaves you there.

# 1. The spec the run was measured under is the spec in this tree.
(cd bench && uv run python -c "from ledgerbench.config import config_hash; print(config_hash())")
# e9fef8d3abbba8db034084d6295d3169a302a10a14ee1a13bc0d94b431fa3166

# 2. The published artifacts are the ones the run wrote.
(cd results/full-20260827T174611-fedora && sha256sum -c --quiet CHECKSUMS.sha256)
# or, with the scope check (fails on a tampered, missing OR unaccounted file):
(cd bench && uv run ledgerbench verify full-20260827T174611-fedora)
# ok 1516 published artifacts verified
```

`spec/` is frozen for exactly this reason: `config_hash` covers every byte of every file
under it, so a one-character edit anywhere in `spec/` breaks the first check. Errata
against the spec go in [`../docs/METHODOLOGY.md`](../docs/METHODOLOGY.md), never into
the file. `manifest.json`'s `identity.git_sha` names a pre-publication commit that is not
in this history.

### What it found

Read [`full-20260827T174611-fedora/report/report.md`](full-20260827T174611-fedora/report/report.md)
for the tables. In one paragraph: each stack's p99 crossed 20 ms at nearly the same rate
(fitted knees 4,799 req/s for Java, 4,833 for .NET); at those rates Java's app container
spent 0.233 CPU-ms per request against .NET's 0.481 (0.49x; paired -51.6%, 95% interval
[-51.8, -51.3]) and held 656 MB of in-window anonymous memory against .NET's 176 MB
(3.72x); PostgreSQL's own CPU per request was 0.101 against 0.115 ms (0.88x). The cost
ratio is a curve, not a constant: 0.34x at 500 req/s, 0.50x at 5,350.

**Both capacity gates passed.** The pre-run gate rests on a projected knee and says so.
The re-gate then measured PostgreSQL under the cell's realized rates: 0.185 of its
3-core budget against a 0.769 ceiling. No lever fired, so the effective workload equals
the frozen spec workload (15% write share, 0.80 target hit rate, 3 PG cores).

**Cache residency was measured, not assumed.** `manifest.cache_stats` records a realized
Redis hit rate of **0.629** over the 116 windows, against the 0.80 the workload was
constructed to target. The report's scope section gives the measured reason: each attack
draws its own target stream, and a median 60.4% of a window's invoice-id read requests
name an id an earlier attack of the same probe had already drawn.

**Erratum against the report.** Its sentence "A measured window is 600 s against a 300 s
cache TTL" describes only the two soak windows. The 114 ladder and confirm windows are
120 s (`spec/slo.yaml`, `ladder.window_seconds`), shorter than the TTL, so the argument
that follows it (an entry can expire before its window ends) applies to the soak windows
only. The report is checksummed and is not edited.

**Warm-up.** 0 of 53 Java probes and 4 of 63 .NET probes (6.3%) were flagged under-warmed
and excluded from every fit and roll-up, against a pre-registered abort threshold of 25%.

### Caveats that travel with these numbers

- **Nine paired blocks, against a pre-registered ten.** One confirm block was dropped for
  an under-warmed .NET probe. The paired verdicts are labelled *directional* for that
  reason; the intervals are printed beside them.
- **The knee bootstrap intervals are not confidence intervals.** The SLO-crossing bracket
  carries two or three measurement-grade reps per stack. Read them as a spread indicator.
- **The fitted knees are optimistic.** In the confirm stage, at exactly the fitted rates,
  mean p99 was 26.6 to 28.9 ms and the single soak windows read 21.1 ms (Java) and
  45.0 ms (.NET). Nothing here supports "the 20 ms promise was met at the knee". The cost
  ratio is insensitive to this: 0.46x to 0.50x on every rung from 1,493 to 5,350 req/s.
- **Time to settle is not start-up time.** The warm-up records show how long each probe
  ran before its gates passed. Process start to first healthy response is not captured
  and nothing is claimed about it.
- **One machine, one workload, flag defaults.** A cache-heavy CRUD and billing backend on
  one Ryzen 9 7950X. No GC, heap or data-layer variant cell was run; the heap-equalised
  cell was deleted before the run because the JVM's default ceiling never bound.
- **DB CPU is per container.** It includes autovacuum, the checkpointer and the WAL
  writer, and it is not attributable per statement.

## The rehearsal: `rehearsal-20260827T111408-fedora`

A reduced measured tier from earlier the same day, kept as the cross-tier replication
check. Rehearsal tiers are normally not published and get no checksum map. This one is the
exception: its `ledger.json`, `manifest.json` and `report/` were added by hand, still
without a map, so `ledgerbench verify` on it finds no map and exits 1.

It reproduces the ratios: 0.53x on app CPU, 0.89x on database CPU, 3.82x on anonymous
memory. It does **not** reproduce the absolute knees (6,439 and 6,874 req/s): its ladder
steps by x2.0 where the full tier steps by x1.2, so its knee is interpolated across a gap
the full run measured directly. The ordering is preserved and both tiers overshoot at
confirm the same way.
