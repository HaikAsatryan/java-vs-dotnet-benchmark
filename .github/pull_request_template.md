<!-- CONTRIBUTING.md has the long form. This is the short version of the same bar. -->

## What this changes

<!-- One or two sentences. If it is a methodology challenge, say what number it moves. -->

## Checklist

- [ ] **Symmetric.** A change to one service's behaviour has the equivalent change on the
      other side, or an explanation in `docs/FAIRNESS.md` of why the asymmetry is correct.
- [ ] **`spec/` untouched**, or the PR says plainly that it invalidates any run that
      cited it. `spec/` is hashed into every run's `config_hash`.
- [ ] **Nothing declared ahead of its implementation.** A new gate, column or check in
      `spec/` or `docs/` arrives with the code that produces it and a passing test.
- [ ] **The suites green locally**: `cd bench && uv sync && uv run pytest`,
      `cd services/java && ./mvnw -B test`,
      `cd services/dotnet && dotnet format --verify-no-changes && dotnet test -c Release Ledgerline.Tests`,
      `cd seed && gofmt -l . && go vet ./... && go test ./...`.
- [ ] **`report.md` not hand-edited.** If analysis output changed, it was regenerated with
      `make analyze RUN=<run_id>` and the diff is in the PR.
