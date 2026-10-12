# ledgerbench

Python (uv) runner, validation, and analysis for the Java vs .NET "Ledgerline"
benchmark. `spec/` is the single source of truth for the workload and SLO; the
published methodology lives in `docs/METHODOLOGY.md` and `docs/FAIRNESS.md`.

## Layout

```
bench/
├── pyproject.toml          # uv project root; all deps, no optional groups
├── figures.py              # draws docs/figures/*.png from a run's artifacts (make figures)
├── ledgerbench/            # the runner package
│   ├── __main__.py         # typer app: doctor smoke run-all gate ladder confirm soak analyze verify status
│   ├── registry.py         # languages / slots / slot sets: the declared SUT topology every module reads
│   ├── config.py           # loads spec/workload.yaml + spec/slo.yaml into pydantic models
│   ├── vegeta.py           # targets/body generator (Zipf+seed), attack invocation, .bin parse
│   ├── zipf.py             # finite inverse-CDF Zipf over ranks 1..N (the hot-set construction)
│   ├── compose.py          # docker compose -f selection, profiles, up --wait, exec, digest capture
│   ├── sut.py              # /readyz readiness polling (per-probe lifecycle lives in live_runner)
│   ├── runtimestats.py     # 1 Hz /runtime-stats poller -> snapshots
│   ├── warmup.py           # warmup gates per spec/slo.yaml; writes warmup.json
│   ├── cgroups.py          # Linux 50ms cgroup v2 sampler; Windows no-op
│   ├── ledger.py           # resumable run-ledger (write-temp-rename, probe-granular)
│   ├── manifest.py         # full-disclosure RunManifest schema
│   ├── checksums.py        # CHECKSUMS.sha256: git-scoped map writer + the `verify` verdict
│   ├── hostread.py         # host_readback phase: reads the tuned host state back off the box
│   ├── capacity_gate.py    # create-only ladder + PG cpu/pg_stat probes + lever logic
│   ├── phases.py           # phase pipeline + RCB interleaving (resumable)
│   ├── live_runner.py      # LiveProbeRunner: real docker + vegeta probe execution
│   ├── analyze.py          # reads a finished ledger -> report/{fits.json,report.md}
│   ├── analysis_bridge.py  # lazy wrappers over bench/analysis (keeps the runner numpy-free)
│   ├── doctor.py           # per-OS preflight checks
│   ├── smoke.py            # smoke flow (Windows-ok)
│   └── util/               # proc / clock / paths / pcg64 / cpuset helpers
├── tests/                  # pure-logic pytest for the runner (no docker)
├── validation/             # cross-service equivalence suite
│   ├── normalize.py        # canonical JSON normalization
│   ├── pricing_reference.py# independent 3rd impl of the pricing algorithm
│   ├── golden.py / golden/ # hand-computed goldens (pricing A/B/C, error envelopes)
│   ├── shapecheck.py       # required-key + JSON-type gate (shapes.json)
│   ├── clients.py          # HTTP client; base URLs from each slot's registry url_env
│   └── tests/              # normalizer, golden self-consistency, shapecheck, equivalence
└── analysis/               # stats, knee fit, report (no figures)
    ├── pava.py             # in-repo PAVA isotonic regression (no sklearn)
    ├── mann_kendall.py     # direct S / var / z / p (no pymannkendall)
    ├── knee.py             # THE crossing predicate + isotonic knee + bootstrap-over-reps CI
    ├── paired.py           # paired-difference CI + +-5% margin verdict
    ├── report.py           # markdown tables
    └── tests/              # PAVA, Mann-Kendall, paired-CI, knee bootstrap, report
```

## CLI surface

`uv run ledgerbench <command>` (also `make` targets on Linux):

- `doctor`: per-OS preflight checks (docker, cgroup v2, cores, spec parse).
- `smoke`: functional rehearsal flow (Windows-ok), resource accounting off.
- `run-all`: the live, ledger-backed, resumable phase pipeline.
- `gate` / `ladder` / `confirm` / `soak`: per-phase re-run entry points
  (each requires `--run-id`).
- `analyze <run_id>`: reads a finished ledger and writes
  `results/<run_id>/report/{fits.json,report.md}`: one knee fit PER SLOT with
  its bracketed refit interval (these are percentile spreads over the ladder's
  reps, **not** inferential CIs: `report.md` says why they cannot be at ladder
  rep counts), the CPU-ms/request-vs-rate curve with each slot's operating point
  on it, stationarity, the per-slot p99 conformance readout, paired COST verdicts
  (CPU-ms/request, anon-RSS/request, $/M) at each slot's own knee AND at the
  common rate, and the request-weighted resource roll-up.
- `verify <run_id>`: recheck every published artifact against `CHECKSUMS.sha256`
  (exit 1 on any tampered, missing or unaccounted file).
- `status [run_id]`: phase/cell/stage table + a machine-readable summary line.

Seeding is not a subcommand: it runs through the compose `seed` profile (driven
by the Makefile / `run-all`).

## Equivalence + analysis usage

```powershell
# equivalence suite skips cleanly unless EVERY slot's URL is set. The var names
# are each slot's registry `url_env`, so they follow the active slot set:
#   uv run python -c "from ledgerbench import registry as r; print({s: r.get_slot(s).url_env for s in r.active_slot_set().slots})"
$env:LEDGER_JAVA_URL = "http://127.0.0.1:8081"
$env:LEDGER_DOTNET_URL = "http://127.0.0.1:8082"
uv run pytest validation/tests/test_equivalence.py

uv run pytest analysis/tests    # stats / knee fit / report assembly
```

## Quickstart

```sh
# from bench/
uv sync                       # installs every dependency
uv run pytest                 # pure-logic unit tests (no docker needed)
uv run ledgerbench --help     # CLI surface
uv run ledgerbench smoke      # smoke (needs Docker; not run by CI)
```

`uv sync` installs everything, including the scientific stack (numpy/scipy/pandas)
that `ledgerbench analyze` needs. There is no optional group to remember.

See `NOTES.md` for resolved spec ambiguities and cross-language-divergence
decisions.
