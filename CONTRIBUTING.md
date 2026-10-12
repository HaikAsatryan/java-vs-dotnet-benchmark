# Contributing

Issues and PRs are welcome. This is a benchmark, so the bar for changes is different
from a normal project: **every dial must stay fair on both sides**, and the measured
contract (`spec/`) is frozen once a published run cites it.

One rule shapes the docs and the spec alike: **nothing is declared ahead of its
implementation.** If a check, a gate or a column is described in `spec/` or in
`docs/`, the code that produces it exists and its test passes. A PR that adds a
declaration without the implementation will be asked to drop the declaration.

## "Your benchmark is unfair / wrong"

Best possible kind of report. To make it answerable:

1. Read [docs/FAIRNESS.md](docs/FAIRNESS.md) first. Every deliberate asymmetry is
   listed there with its treatment (disclosed, measured, or pinned identical). If your
   concern is already listed, say why the treatment is insufficient.
2. Point at the **specific** dial: a file/line in `spec/`, `services/java`,
   `services/dotnet`, or `infra/`, not "Java is configured badly" in general.
3. Propose the **symmetric** fix: what changes on *both* sides (or why touching one
   side is the fair move). One-sided tuning requests are rejected on principle: that
   is the failure mode this benchmark is designed against.
4. If your claim is "this biases the numbers", say in which direction and roughly how
   much. Reproducible evidence (a run, a profile, a microbench) beats reasoning.

## Code / methodology PRs

- Both services implement `spec/` exactly; golden + cross-service equivalence tests
  enforce it. A change to either service that alters wire behavior needs a matching
  spec change and a matching change on the other side.
- `spec/` is versioned by content: every run manifest records a `config_hash` over all
  of `spec/`. Changing `spec/` after a published run means the published numbers no
  longer describe this tree; do not be surprised when such PRs are deferred until the
  next measured run.
- Run the suites locally before opening a PR:
  - `cd bench && uv sync && uv run pytest` (runner + validation + analysis; Docker-gated tests skip)
  - `cd services/java && ./mvnw -B test` (live-infra tests skip without `LEDGERLINE_LIVE=1`)
  - `cd services/dotnet && dotnet format --verify-no-changes && dotnet test -c Release Ledgerline.Tests`
  - `cd seed && gofmt -l . && go vet ./... && go test ./...`
- `make smoke` (any OS, needs Docker) is the end-to-end functional gate.

## What is out of scope

- Rewriting either service in a faster but non-idiomatic style. The point is the
  idiomatic production stack, not the fastest possible implementation per language.
- Adding new languages: forks welcome, this repo stays a two-stack controlled experiment.
