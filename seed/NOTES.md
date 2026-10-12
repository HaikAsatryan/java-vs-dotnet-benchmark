# seed/: decision notes

Design decisions for the seeder that are not already covered in full by
`README.md`. The seed-key derivation, per-entity child streams, `created_at`
progression, and the field distributions are documented in `README.md`; this
file records the choices that need their reasoning written down. `spec/` is the
single source of truth.

## invoice_items: exact-count invariant relaxed to `> 0` + content pins

spec/db-access.md requires "exact row counts per scale". customers and invoices
have fixed scale constants (1e6/5e6, 1e3/5e3) and are asserted `==`. The
invoice_items total is NOT a fixed scale constant: it depends on the
deterministic per-invoice item-count draws in `[items-min, items-max]`. Rather
than hard-code a precomputed magic number that would silently rot if the item
draw ever changed, the invariant asserts `invoice_items > 0` AND pins its CONTENT
exactly via the line-total invariant (`line_total = qty*unit_price` on every row)
and the invoice-total invariant (`invoice.total = sum(items.line_total)`). The
content pins are strictly stronger than a row-count constant for catching load
corruption. The deterministic count for a given (seed, scale, items range) is
reproducible and could be snapshotted if an exact-count gate is later desired;
the generator exposes it implicitly through the COPY tag.

## No long transaction around the three COPYs

The three `COPY`s run as separate pgx operations, not wrapped in one explicit
transaction. Rationale: the seeder is a one-shot offline tool, not the measured
path; the correctness gate is the post-load invariant suite (which fails the
process non-zero on any inconsistency), not transactional atomicity of the load.
`--truncate` + a fresh load is the recovery path. This keeps the loader simple
(KISS) with no behavioral difference to either SUT, which never reads partial
loads (seeding completes before any service starts).

## Grants executor

spec/postgres/initdb/01-role.sql creates the `ledger` role at container init but
explicitly defers the table/sequence GRANTs to the seeder ("the seeder executes
the following"). Implemented in `schema.Grants`, gated behind `--grants`. The two
documented statements are copied verbatim (SELECT/INSERT/UPDATE on the three
tables; USAGE/SELECT on all sequences; no DELETE). Idempotent.

## Dump CSV timestamp format

`--mode dump` writes `created_at` as RFC3339 UTC (`time.RFC3339`). PostgreSQL
parses this for `timestamptz` on a `\copy` fallback. The dump path is debug-only
(not the hot load path), so this choice has no bearing on the measured COPY load,
which passes `time.Time` values directly through the pgx binary protocol.

## Not exercised without a live PG

The COPY/restart/invariant SQL needs a running PostgreSQL to exercise and so is
not covered by the offline unit tests; it runs on the measured run. Determinism,
balance accumulation, Zipf skew, and CSV shape ARE covered by offline unit tests.
