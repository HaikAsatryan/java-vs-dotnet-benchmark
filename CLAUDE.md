# CLAUDE.md: Ledgerline round 2 (Java vs .NET)

**Read `PROGRESS.md` first.** It carries the plan, the phase gates, the pre-registered
decisions, and the current status. This file is only the standing rules.

Open-source benchmark comparing **Java 25 LTS (Spring Boot 4 + Hibernate 7)** against
**.NET 10 (Minimal API + EF Core)** on a realistic invoicing/billing backend. Headline is
cost at a fixed latency promise: CPU-ms and steady-state memory per request at the rate
where mixed-workload p99 crosses 20 ms, as a Java:.NET ratio. Shared PostgreSQL 18.4 +
Redis, DB in its own cgroup, open-loop CO-safe load (vegeta), cgroup v2 ground truth.

Methodology: `docs/METHODOLOGY.md`. Fairness dials: `docs/FAIRNESS.md`.
Operations: `docs/RUNBOOK.md`.

## Benchmark run / resume

Measured runs are Linux-only. Commands run from `bench/`:

- `uv run ledgerbench run-all --scale full`: start a NEW measured run.
- `uv run ledgerbench run-all --scale smoke`: functional rehearsal (Windows-ok, ~20 min).
  Proves plumbing only. It is **not** measurement-grade and cannot validate JVM warmup.
- `uv run ledgerbench run-all --scale rehearsal`: reduced measured tier (added in P1; see
  PROGRESS.md). This is the tier that validates numbers before committing days.
- `uv run ledgerbench run-all --resume`: resume the most recent unfinished run. Omit
  `--scale`: the resume adopts the ledger's own pinned tier, and naming a different one
  is refused.
- `uv run ledgerbench status`: the latest run's ledger (phase states, probe progress, next pending).
- `uv run ledgerbench analyze <run_id>`: knee fits, stats, report tables.

State lives in `results/<run_id>/ledger.json` (atomic, crash-safe, probe-granular). Resume
re-runs doctor, re-applies host tuning on Linux, re-verifies DB invariants, and redoes any
interleave block split by the crash.

**If the user says "resume benchmark":** run `uv run ledgerbench status` from `bench/`,
report where it stopped, then run `uv run ledgerbench run-all --resume` (no `--scale`).

## Hard rules

- Never edit `spec/` mid-run: the ledger's `config_hash` (a hash of all of `spec/`) will
  refuse to resume.
- Never run measured tiers on Windows. `make bench-*` / `host-setup` are Linux-gated.
- No git commit or push without explicit user instruction.
- **Never quote a round-1 number next to a round-2 number.** Round 2 re-measures .NET from
  scratch and stands entirely on its own numbers. See PROGRESS.md.
- `seed/` is Go and stays Go. It is tooling, not a system under test.
- **Nothing enters `spec/` as a declared gate until its implementation exists and its test
  passes.** Round 1's failure mode was over-declaration: it pre-registered a methodology
  far larger than it ran and then had to disclose the gap. Anything not
  working at the P7 freeze is deleted from the spec, not declared and skipped.

## Code style for `services/java/`

Match `services/dotnet/`. Comments explain **why**, cite `spec/`, or pin a decision; they
never narrate what the next line does. No scaffolding comments, no javadoc on obvious
accessors, no defensive handling of what cannot throw, no speculative interfaces. Match the
surrounding file's density rather than a global rule. Full rationale in PROGRESS.md P4.
