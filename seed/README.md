# seed/: canonical deterministic Ledgerline seeder

One Go binary that loads a byte-stable, reproducible dataset into PostgreSQL for
the Java-vs-.NET benchmark. Every row is a pure function of `(--seed, id)`; two
runs at the same scale and seed produce identical data. Canonical language is Go
because the fast load path is pgx binary `COPY`.

Spec sources (single source of truth): `spec/db-access.md` "Seeder contract",
`spec/workload.yaml` `seeding`, `spec/schema.sql`,
`spec/postgres/initdb/01-role.sql`. spec/ is the single source of truth.

## Build / test

```
go build ./...
go vet ./...
go test ./...      # no live PG needed; determinism / balance / Zipf / CSV-shape
```

Module: `ledgerline/seed`. Sole direct dependency: `github.com/jackc/pgx/v5
v5.10.0`.

## CLI

```
seed \
  --dsn       postgres://postgres:...@host:5432/ledgerline  # required for copy/verify
  --scale     full|smoke      # full=1e6 customers/5e6 invoices; smoke=1e3/5e3
  --seed      42              # RNG seed (frozen default 42)
  --items-min 3 --items-max 8 # invoice line-item count range (inclusive)
  --mode      copy|dump       # copy=load live PG (default); dump=RFC4180 CSVs
  --dump-dir  dump            # output dir for --mode dump
  --truncate                  # TRUNCATE ... RESTART IDENTITY before load
  --verify-only               # run invariants against an already-loaded DB, exit
  --schema    /spec/schema.sql# apply this DDL first (single DDL source)
  --grants                    # apply ledger-role GRANTs (01-role.sql)
```

Typical container invocation (compose `seed` profile):

```
seed --mode copy --scale smoke --dsn "$SEED_DSN" \
     --schema /spec/schema.sql --grants
```

`--mode dump` needs no DB connection (debug path).

## Load pipeline (`--mode copy`)

1. (optional) `--schema`: apply `spec/schema.sql` verbatim.
2. (optional) `--truncate`: `TRUNCATE ... RESTART IDENTITY CASCADE`.
3. (optional) `--grants`: ledger-role table + sequence grants.
4. PASS 1: iterate the deterministic invoice stream once, accumulating each
   customer's `balance_minor` into one `int64` slice (8 MB at 1M customers).
5. PASS 2: three ordered binary `COPY`s in PK/FK order: customers (with the
   accumulated balances), invoices, invoice_items. Each pass regenerates the
   pure-function stream; **no temp files**, memory stays bounded.
6. `ALTER TABLE ... ALTER COLUMN id RESTART WITH <seeded_max+1>` for both
   IDENTITY sequences (invoices, invoice_items) so runtime POSTs never collide
   with seeded ids (spec/schema.sql header).
7. Post-load invariants (below). Any failure → non-zero exit.

`customers.id` is app-assigned dense `1..N` (required for Zipf selection).
`invoices.id` / `invoice_items.id` are IDENTITY columns; the seeder writes
explicit dense ids into them during COPY, then restarts the sequence past the
max.

## Determinism

- Root source: `math/rand/v2` `rand.NewChaCha8(key)`.
- **Seed-to-key rule (frozen):** the `[32]byte` ChaCha8 key is the `uint64`
  `--seed` written **little-endian** into 8 bytes, **repeated 4×**. Example:
  seed `42` → `2a 00 00 00 00 00 00 00` × 4. Fully specified so any language can
  reproduce the key.
- **Per-entity child streams:** customer `i` and invoice `j` each derive an
  independent `*rand.Rand` keyed by `(seed, kind, id)` via a SplitMix64 avalanche
  feeding a fresh ChaCha8. Generation order, goroutine scheduling, and COPY chunk
  boundaries never change any row: every row is a pure function of its id. This
  is what makes the multi-pass re-iteration byte-stable.
- All money is `int64` minor units; the only floats in the whole generator are
  the Zipf cumulative weights, which never feed a money value.

## Distributions (frozen; spec/workload.yaml + seeder contract)

| field | rule |
|---|---|
| `invoice.customer_id` | finite **inverse-CDF Zipf(alpha=1.0)** over ranks `1..N`; rank 1 = customer id 1. Hot customers get many invoices so list pages for Zipf-hot customers are full 20-row pages (the ~6KB list-page assumption). |
| `invoice.status` | weighted: `draft 0.10 / open 0.30 / paid 0.50 / void 0.10` (integer permille ladder). |
| item `sku` | `<CAT>-<NNNN>`, `^[A-Z]{2}-[0-9]{4}$`. CAT drawn from the pricing categories `EL GR HZ LX DG FU MD AP BK TL` plus occasional `ZZ` (exercises the unknown→0 pricing branch). NNNN uniform `0000..9999`. |
| item `qty` | uniform int `[1, 50]`. |
| item `unit_price_minor` | uniform int `[100, 5_000_000]`. |
| item `description` | deterministic filler keyed by sku+qty (NOT NULL only; never asserted). |
| `currency` | constant `USD` (char(3)). |
| `created_at` | `2025-01-01T00:00:00Z + id seconds`, per table (any fixed epoch progression; equivalence never compares seeded timestamps across DB loads). |
| `line_total_minor` | `qty * unit_price_minor`. |
| `invoice.total_minor` | `sum(items.line_total_minor)`. |
| `customer.balance_minor` | `sum(invoice totals)` for that customer (accumulated in PASS 1). |

### Why finite inverse-CDF Zipf and not `rand.Zipf` / `numpy.random.zipf`

The standard generators parameterize differently (`numpy` uses `a = s + 1`;
passing `1.0` is illegal) and are infinite-support. The workload needs an
explicit finite Zipf over the dense customer keyspace so rank 1 maps to customer
id 1 and hot customers receive enough invoices to fill 20-row list pages. The
cumulative-weight array is built once (O(N)); each draw is an O(log N) binary
search with no transcendental on the draw path at alpha=1.0.

## Post-load invariants (spec/db-access.md; non-zero exit on any failure)

- Exact row counts: `customers == scale`, `invoices == scale`,
  `invoice_items > 0` (its total is data-derived; its content is pinned by the
  line/total invariants below).
- Per customer: `balance_minor == COALESCE(sum(invoices.total_minor), 0)`.
- Per invoice: `total_minor == sum(items.line_total_minor)`.
- Per item: `line_total_minor == qty * unit_price_minor`.
- FK integrity: no orphan invoices or items.
- Index `idx_invoices_customer_id_id_desc` exists (`pg_indexes`).

Re-run standalone against a loaded DB with `--verify-only`.

## Docker

Multi-stage `golang:1.26.5-bookworm` → `gcr.io/distroless/static-debian13:nonroot`,
`CGO_ENABLED=0`. **The compose build context must be the repo root** so
`spec/schema.sql` can be COPYed into the image at `/spec/schema.sql`. In
`infra/compose.yaml` that is `..`, relative to the compose file's directory:

```yaml
seed:
  build:
    context: ..
    dockerfile: seed/Dockerfile
```
