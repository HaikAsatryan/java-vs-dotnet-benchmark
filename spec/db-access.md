# spec/db-access.md — DB access policy (both services, frozen)

The canonical SQL lives in `spec/db-access.sql`. It is the REFERENCE TEXT, not a
text either headline stack sends: the .NET raw ADO.NET variant uses those bodies
literally (`services/dotnet/Sql/SqlStatements.cs`), and both ORMs generate their
own. Every generated deviation is captured against a live database, pinned by a
test, and listed in the next section. None is hand-written away — the SQL an ORM
emits is part of what an ORM costs, and this round is ORM against ORM.

Two .NET variants exist beside the EF Core headline (2026-08-13 revision):

- **ado** — hand-written ADO.NET over Npgsql with positional `$1` parameters,
  statement bodies byte-identical to `spec/db-access.sql`. The no-ORM floor.
- **dapper** — idiomatic Dapper with named parameters; Npgsql client-side
  rewrites each `@name` to the identical positional text before it hits the
  wire (asserted by a substitution test). The micro-ORM middle point. Its
  per-command rewriting cost is part of what Dapper costs in production and is
  not tuned away. Dapper has no batch API, so the create path's item batch
  drops to NpgsqlBatch exactly like the ado variant.

## Generated-SQL deviations, both stacks

Captured 2026-08-23 on PostgreSQL 18.4 with `log_statement='all'`, driving each
service's real repository code. Every row below is wire text, not a rendering.
The .NET column is pinned offline by `EfQueryShapeTests`; the Java column by
`StatementShapeTest` for the Hibernate-generated statements and by
`SqlParityTest` for the two NATIVE queries, Q3b/Q7 and Q6, whose claim is
byte-identity with `spec/db-access.sql` rather than a generated shape. Neither
test covers every row — where a row is not pinned, it says so, because
"asserted by a test" was claimed here once for a row no test asserted, and
naming the wrong test is the same defect one step smaller.

| statement | .NET / EF Core 10 | Java / Hibernate 7 |
|---|---|---|
| Q1 | table alias `AS i`, PascalCase output aliases (`i.id AS "Id"` …), trailing `LIMIT 1` from the `FirstOrDefault` idiom. The `LIMIT 1` is NOT pinned by a test. | table alias `i1_0`, no output aliases, NO row limit — closer to the canonical text than EF here. Pinned. |
| Q2 | table alias `AS i`, no output aliases | table alias `ii1_0`. Pinned. |
| Q3 | `LIMIT $2 OFFSET $3` on every page EXCEPT page 2, where offset equals limit and EF/Npgsql collapse the two equal-valued parameters into `LIMIT $2 OFFSET $2`. Pages 1, 3, 4, 5 all carry the offset clause. Only the `LIMIT $n` parameterization is pinned; the page-2 collapse is not. | `offset $2 rows fetch first $3 rows only` (PostgreSQLDialect's `OffsetFetchLimitHandler`, not reachable by any property), EXCEPT page 1, which drops the offset clause entirely to `fetch first $2 rows only` — Spring Data skips `setFirstResult` when the offset is zero. Both texts pinned. |
| Q3b / Q7 | `SELECT EXISTS (⏎ SELECT 1⏎ FROM customers AS c⏎ WHERE c.id = $1)` — aliased, qualified, embedded newlines, and NO `AS exists` output alias. Not pinned by any test. | byte-identical to the canonical `SELECT EXISTS(SELECT 1 FROM customers WHERE id = $1) AS exists` (native query). |
| Q4 | column list ALPHABETICAL — `(currency, customer_id, status, total_minor)` against the canonical `(customer_id, status, currency, total_minor)`; `RETURNING id, created_at` as frozen. Not pinned. | the SAME alphabetical list, lower-cased: `insert into invoices (currency,customer_id,status,total_minor) … returning id,created_at`. Pinned. |
| Q5 | alphabetical `(description, invoice_id, line_total_minor, qty, sku, unit_price_minor)`; `RETURNING id` as frozen. Not pinned. | the SAME alphabetical list. Pinned, together with the `use_get_generated_keys=false` setting the `returning id` depends on. |
| Q6 | `UPDATE customers AS c SET balance_minor = c.balance_minor + $1 WHERE c.id = $2` — aliased, qualified, and the parameters in the reverse of the canonical order. Not pinned. | `UPDATE customers SET balance_minor = balance_minor + $1 WHERE id = $2` — parameters reversed for the same reason (JDBC placeholders are positional), no alias. |
| transaction start | `BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED` | bare `BEGIN` |

Both ORMs alphabetize insert columns, so Q4 and Q5 diverge from the canonical
text in the SAME direction by the SAME amount; that pair is a deviation from
`spec/db-access.sql`, not an asymmetry between the stacks. The Q1, Q3b/Q7 and Q6
rows ARE asymmetries: Java reaches the canonical text on the two native queries
and adds no row limit to Q1, where EF aliases, qualifies and appends. Plan
equivalence is what is claimed for all of them — same index paths, no extra sort
or filter nodes — and P5 confirms it with EXPLAIN against the real database. The
DB-side statement SET is NOT identical, and this file no longer says it is.

**Q3 costs each stack two statement texts, and they split on different pages.**
`spec/workload.yaml page_distribution` weights pages 1..5 at 0.70 / 0.15 / 0.08 /
0.04 / 0.03, and `list_invoices` is 25% of the mix. Java's split is page 1 versus
pages 2-5, so 17.5% of all measured requests execute one text and 7.5% the other.
.NET's split is page 2 versus everything else, so 3.75% of all measured requests
execute one text and 21.25% the other. Consequences, and they must be read
together: each stack occupies TWO `pg_stat_statements` queryids for one logical
query, and under the exec/prepare modes below each stack promotes TWO server-side
prepared statements for it. Counted across both stacks the endpoint has THREE
distinct texts. P5's prepared-statement census will see all of this; it is
declared here so the census confirms a disclosure rather than discovering a
surprise.

## Queries (bodies in db-access.sql)

| id | name | used by | notes |
|---|---|---|---|
| Q1 | GetInvoice | GET /invoices/{id} (cache miss) | by PK |
| Q2 | GetInvoiceItems | GET /invoices/{id} (cache miss) | WHERE invoice_id ORDER BY id |
| Q3 | ListInvoicesByCustomer | GET /customers/{id}/invoices | ORDER BY id DESC LIMIT 20 OFFSET $2; must hit idx_invoices_customer_id_id_desc with no sort node |
| Q3b | CustomerExists | list endpoint, ONLY when the page is empty | happy path stays one query; both sides identical |
| Q4 | InsertInvoice | POST /invoices | RETURNING id, created_at (both DB-generated; needed for the 201 body, no extra query) |
| Q5 | InsertInvoiceItem | POST /invoices | xN (3-8) |
| Q6 | UpdateCustomerBalance | POST /invoices | balance_minor = balance_minor + $2 |
| Q7 | CustomerExists | POST /invoices, before the write batch | same statement as Q3b |

GET /invoices/{id} on a cache miss issues Q1 then Q2, SEQUENTIALLY, on both
headline stacks: Q2 binds the header's id, and neither ORM pipelines. Two round
trips each. What differs is the CONNECTION: EF opens one explicitly and holds it
across both queries (without that, each query would rent and return the pooled
connection separately), while the Java service takes a HikariCP checkout per
repository call. That is a pooling difference, not a wire-round-trip difference,
and it is priced by the DB-CPU column below rather than argued about. The ado and
dapper variants batch Q1+Q2 into one NpgsqlBatch round trip; they are variants,
not the headline.

POST /invoices/{id}/pdf-stub probes invoice existence with the Q1 header lookup
ONLY — one statement, items never loaded — identically on every data layer.

Absent-id note (unexercised by the workload; the GET keyspace is seeded-only):
the batched ado/dapper variants pre-queue Q1+Q2, so a 404 still executes Q2
server-side; both headline stacks issue the queries sequentially and skip Q2 when
the header is absent. Disclosed, not contorted away.

## POST /invoices transaction (the fairness-critical mirror)

One transaction, READ COMMITTED (PG default; pinned; neither side elevates).
Order inside: Q7 existence -> Q4 header (RETURNING id, created_at) ->
Q5 xN items (RETURNING id) -> Q6 balance.

Q6 is an ATOMIC increment (`balance_minor = balance_minor + $n`) on every data
layer. A tracked read-modify-write (load balance, add, write the absolute value)
is NOT equivalent: under the workload's Zipfian hot-customer contention it loses
concurrent updates and breaks the seeder invariant
`balance_minor == sum(total_minor)`. (v1 pinned the EF tracked-entity form; the
2026-08-13 revision replaced it with `ExecuteUpdate` for exactly this reason.)

The header id is IDENTITY-generated and the N item inserts bind it as a
parameter, so NO driver can put header + items in one pipelined batch: the
RETURNING value cannot propagate across statements inside a batch. That much is
shared. **The item inserts are not.**

### The round-trip ledger (measured, per stack)

| stack | round trips per create, N items | why |
|---|---|---|
| Java / Hibernate 7 (headline) | **5 + N** | Q7, BEGIN, Q4, **N separate item inserts**, Q6, COMMIT |
| .NET / EF Core 10 (headline) | **6** | Q7, BEGIN, Q4, **one batched round trip for all N items**, Q6, COMMIT |
| .NET / raw ADO.NET (`ado`), Dapper (`dapper`) | **5** | Q7, BEGIN, Q4, **one NpgsqlBatch carrying Q5 xN + Q6**, COMMIT |

The two headline rows are measured below. The variant row is read off
`AdoInvoiceRepository.CreateAsync`, which queues Q5 xN and Q6 on one
`NpgsqlBatch`; the variants exist for the P2 shakedown and are not part of the
published comparison, so nobody has driven a stopwatch through them.

`spec/workload.yaml`'s generator REALIZES an item-count distribution of
{6: ~67%, 7: ~16%, 8: ~17%} (mean 6.50) -- the uniform 3..8 draw is grown
toward 8 until the padded body can reach `target_bytes` (corrected at the P7
freeze, review finding C1; the arithmetic below was previously computed from
the uniform draw that does not run). The mean Java create therefore costs
**11.5** round trips against EF Core's 6, and `create_invoice` is 15% of the
mix.

**Measured, not asserted.** Both services were driven against the same
PostgreSQL 18.4 through a proxy adding 25 ms in each direction (50 ms per round
trip), 5 requests per point after 6 warm requests, `DB_POOL_MAX=4` on both sides:

```
Java   N=3  0.392 0.393 0.367 0.393 0.393 s   ->  ~8 round trips  (5 + 3)
Java   N=8  0.625 0.652 0.652 0.652 0.651 s   -> ~13 round trips  (5 + 8)
.NET   N=3  0.309 0.308 0.309 0.283 0.308 s   ->   ~6 round trips
.NET   N=8  0.309 0.308 0.283 0.283 0.307 s   ->   ~6 round trips
```

Java's cost is linear in N at 51.8 ms per extra item — one 50 ms round trip
each. .NET's is FLAT in N. Averaged over the REALIZED mix that is **0.825**
extra database round trips per measured request (5.5 extra per create at the
realized mean N=6.50, `create_invoice` at 15% of the mix), which is the
largest per-request wire asymmetry this file declares — and it costs Java
~22% more than the uniform-draw arithmetic this paragraph previously carried
(0.675/req at mean N=5.5).

**The cause, and why it is not tuned away.** `GenerationType.IDENTITY` makes
Hibernate's insert immediate at `persist()` and drops the JDBC batch key, so
`hibernate.jdbc.batch_size` provably does nothing for `InvoiceItem` (a controlled
experiment at `batch_size=8` inserted 6 assigned-id rows in 1 prepared statement
and 6 IDENTITY rows in 6). `spec/schema.sql` pins
`invoice_items.id GENERATED ALWAYS AS IDENTITY`, and the schema is frozen, so
this is not avoidable inside JPA. Dropping to `JdbcTemplate.batchUpdate` would
recreate the exact ORM-against-hand-written-SQL asymmetry this round exists to
avoid. It is therefore ACCEPTED AND DISCLOSED: it is what the ORM costs on a
frozen IDENTITY schema, it is in `docs/FAIRNESS.md`, and the DB-CPU column below
prices the database half of it.

Per-stack detail:

- **Java / Hibernate:** `customerExists` (native, and OUTSIDE the transaction,
  so the existence probe is the first round trip on both stacks) ->
  `@Transactional` opens a bare `BEGIN` -> `invoices.save` (Q4) -> a loop of
  `items.save` (Q5, one per item; no `saveAll`, which would read as if it
  batched) -> `addToBalance` (Q6, native atomic increment) -> COMMIT. Isolation
  stays JDBC-default: naming READ_COMMITTED would make Spring call
  `Connection.setTransactionIsolation`, which PgJDBC sends as its own
  `SET SESSION CHARACTERISTICS` statement ahead of the BEGIN — one more round
  trip, captured in `services/java/NOTES-equivalence.md`.
- **.NET / EF Core:** Q7 via `AnyAsync`; explicit `BeginTransactionAsync`
  (DB-default READ COMMITTED, spelled out on the wire); ONE `SaveChangesAsync`
  for the invoice graph, which sends the header insert, then all N item inserts
  as a single batched command; then Q6 as `ExecuteUpdateAsync`; commit.
  Automatic savepoints are disabled. EF cannot fold Q6 into the item batch the
  way `NpgsqlBatch` does, which is the one round trip the ado/dapper variants
  save.

All stacks return the REAL DB-generated item ids in the 201 body (pinned at
integration 2026-06-06; EF from SaveChanges, Hibernate from each Q5's
`returning id`, ADO/Dapper from the batch results).

After commit (outside the tx): Redis `DEL invoice:{new_id}` (spec/cache.md);
then the `invoice_created` + `cache_invalidated` log events.

Hot-row note: Zipfian customer selection makes Q6 contend on hot customer rows.
Realistic, identical for both languages; lock waits visible in pg_stat and
reported.

## Exec / prepare modes per variant

| stack | baseline | declared variant |
|---|---|---|
| Java PgJDBC | `prepareThreshold=5` (PgJDBC DEFAULT: the 5th execution of a statement becomes a named server-side prepared statement) | none — PgJDBC is never de-tuned |
| .NET Npgsql | `Max Auto Prepare=20`, `Auto Prepare Min Usages=5` — **explicitly set, NOT the Npgsql default** | `variant.npgsql-default-prepare`: `Max Auto Prepare=0` (the Npgsql default, off). Declared and runnable on request (`phases.CELLS`, `linux_only` + `deferred`, executed via `LEDGERBENCH_RUN_VARIANTS`); NOT part of a default run |

This dial is **not** a default on both sides, so it is declared here rather than
buried.

Npgsql's documented default is `0` (never auto-prepares); PgJDBC's is
`prepareThreshold=5`. Running both at their defaults would put Java on
server-side prepared statements and .NET on unprepared ones, and the resulting
Java:.NET headline would be dominated by prepared-vs-unprepared rather than by
runtime cost: at `Max Auto Prepare=0` every execution of every statement is
parsed and planned again on the server, which is a per-request cost the Java
side stops paying at its fifth execution. So the baseline **equalizes the
BEHAVIOUR** (`Min Usages=5` matches `prepareThreshold=5`: both stacks promote a
statement on its 5th execution) at the cost of one side's literal default, and
the default-vs-default contrast is kept as a declared variant cell that would
quantify it. Like `variant.ado` and `variant.dapper`, that cell is
`deferred=True`: a plain `run-all` never executes it, and the ledger's
`variants` phase records it as declared-not-executed. It is quantified when the
operator asks for it by name, and the published comparison does not rest on it.

Verified on PostgreSQL 18.4 with `log_statement='all'` (2026-08-21): PgJDBC's
first four executions of a statement log as `execute <unnamed>` and the fifth
logs as `execute S_1`, i.e. the documented threshold is what actually happens.

**A second, asymmetric cost rides on this dial, and it lands in the DB-CPU
column.** Npgsql resets a connection on every pooled CHECKOUT (`DISCARD ALL` at
`Max Auto Prepare=0`, narrowing to a lighter reset once statements are
auto-prepared, because a full `DISCARD ALL` would throw the prepared statements
away). Measured 2026-08-23 with `log_statement='all'`: `DISCARD ALL` precedes the
first statement of every checkout, including checkouts taken microseconds apart.

The Java side is NOT a bare absence, and this file previously said it was.
HikariCP sends a per-checkout aliveness probe — PgJDBC's `isValid()`, which
reaches the server as an EMPTY query and logs as `statement:` with nothing after
it — but only when the connection has been idle longer than
`com.zaxxer.hikari.aliveBypassWindowMs`, whose default is 500 ms (read off the
shipped HikariCP 7.0.2 `HikariPool.getConnection`). Measured both ways: requests
1 s apart each pay one empty-query round trip; six back-to-back requests pay
exactly one, at the head of the burst. At every rate this benchmark measures at,
connections are reused far inside 500 ms, so the Java probe does not fire inside a
measured window and the Npgsql reset fires on every request in one. Per-CONNECTION
PgJDBC setup chatter (`SET application_name`, a `pg_settings` lookup,
`pg_get_keywords()`) is a separate cost and, with `DB_POOL_MIN == DB_POOL_MAX`,
is paid during warmup and outside every measured window.

Two consequences that must be read together with the DB-CPU column: some of any
.NET-side DB CPU is the driver's pooling behaviour rather than the runtime's, and
D4 moves that cost in the SAME direction as it moves the prepare behaviour, so
the two are not independent. P5's `pg_stat_statements` wire ledger is where both
sides are counted under load, at the rates the headline is taken at.

### The per-checkout sentence the report renders (verbatim)

The DB-CPU caveats in the generated report print one of these rows per driver in
this run's slot set, as `<driver> <behaviour>`. The table is the SINGLE
definition: `registry.LanguageSpec.db_driver` / `.db_driver_pooling_note` carry
the same two strings and `test_db_access_spec.py` fails if the pair drifts.
This paragraph exists because the Java row said the opposite of this section for
one phase, in a sentence that renders into the headline report and that no test
read.

| driver | behaviour |
|---|---|
| Npgsql | resets a connection on every pooled CHECKOUT, which is DB-side work in PostgreSQL's cgroup (spec/db-access.md) |
| PgJDBC + HikariCP | sends a per-checkout aliveness probe only when the connection has been idle longer than HikariCP's 500 ms `aliveBypassWindowMs`, so at the rates this benchmark measures at it does not fire inside a measured window (spec/db-access.md) |

### Wire formats, measured

Both drivers use the extended query protocol (Parse / Bind / Execute). This file
said until 2026-08-24 that "parameter/result formats go binary once a statement
is named ... asserted rather than assumed"; nothing asserted it and, captured
2026-08-24 through a PostgreSQL 18.4 wire tap that decodes `Bind` format codes
(0 = text, 1 = binary), it is false in both halves.

| | Java / PgJDBC | .NET / Npgsql |
|---|---|---|
| PARAMETER formats, 1st execution (unnamed statement) | `[1, 1]` — binary already | `[1]` — one code applied to every parameter, binary already |
| RESULT formats, 1st execution (unnamed statement) | `[]` — the empty list, i.e. ALL COLUMNS TEXT; the `RowDescription` comes back `[0,0,0,0,0,0]` | `[1]` — one code for every column, binary already; the `RowDescription` comes back `[1,1,1,1,1,1]` |
| RESULT formats once named (`S_1` / `_auto0`) | `[1, 1, 0, 0, 1, 1]` over Q3's `id, customer_id, status, currency, total_minor, created_at` — binary for the four numeric/timestamp columns, TEXT for `status` and `currency` | `[1]` — unchanged; naming the statement changes nothing about formats |

So parameters are binary on both stacks from the first execution, before any
statement is named, and RESULTS are the asymmetry: Npgsql requests binary
results immediately and for every column, while PgJDBC requests text until the
statement is server-named at `prepareThreshold=5` and then requests binary only
for the types it has binary support for — on Q3, `status` (text) and `currency`
(char(3)) stay text on every execution.

Captured on Q3 (`GET /customers/{id}/invoices`, 25% of the mix). Q1 selects the
SAME six columns, so the same per-column split is what the read path pays there
too; only Q3 was put on the tap. Either way it is a live per-column
encode/decode difference, in PostgreSQL's cgroup as well as the SUT's. Not
equalized: both are their driver's stock behaviour, and PgJDBC's
`binaryTransfer` list is not a dial this benchmark turns.

Npgsql multiplexing stays OFF (experimental).

## Pools (single env surface, both services)

| env | default | Java | .NET |
|---|---|---|---|
| DB_POOL_MAX | 24 | HikariCP maximumPoolSize | Npgsql Maximum Pool Size |
| DB_POOL_MIN | = DB_POOL_MAX | HikariCP minimumIdle | Npgsql Minimum Pool Size |
| DB_CONN_LIFETIME | 3600 (s) | HikariCP maxLifetime | Connection Lifetime (explicit pin; doc-default ambiguity resolved by always setting it) |
| REDIS_POOL_MAX | 16 | Lettuce (multiplexer; same documented asymmetry) | SE.Redis (multiplexer; documented asymmetry, spec/cache.md) |

`REDIS_POOL_MAX` binds to no pool on EITHER side: both clients are shared
multiplexers. Both services echo the value in `/runtime-stats` `config` and
`redis_pool.max`, and neither sizes anything with it. It is a recorded knob, not
a live one, and it is symmetric.

min = max so neither pool cold-starts or idle-prunes connections mid-run.
DB_POOL_MAX < PG max_connections (100). The pinned values are echoed in the
manifest, identical across both services. The pin is a declared choice, applied
identically to both stacks; nothing in this file claims it is an optimum.

Pool warmup: both services open all `DB_POOL_MAX` connections CONCURRENTLY at
startup (`DB_POOL_WARM_ON_START`, default on), so the pool is full before the
first request rather than filled by the runner's warmup traffic. The warmup gate
still requires `/runtime-stats` db_pool.total == max, which is what makes the
startup warm verifiable instead of assumed.

## Seeder contract (consumed by seed/)

- One canonical deterministic generator (seed 42), exact counts per scale
  (spec/workload.yaml `seeding`), money int64, no floats.
- COPY order: customers -> invoices -> invoice_items, ids explicit and dense,
  rows emitted in PK order. Binary COPY (pgx CopyFrom).
- After COPY: restart both IDENTITY sequences past the seeded max
  (see spec/schema.sql header).
- customers.created_at / invoices.created_at: deterministic timestamps derived
  from the seed stream (any fixed epoch progression; equivalence never compares
  seeded timestamps across DB loads, only across services on the SAME load).
- Post-load invariants (all must hold, non-zero exit otherwise):
  - exact row counts per scale;
  - per customer: balance_minor == COALESCE(sum(invoices.total_minor), 0);
  - per invoice: total_minor == sum(items.line_total_minor);
  - per item: line_total_minor == qty * unit_price_minor;
  - idx_invoices_customer_id_id_desc exists (pg_indexes).

## Error reporting

Error rate is always reported alongside latency (headline valid only when
errors < 0.1%, spec/slo.yaml). DB errors map to 500/internal (RFC 7807);
statement_timeout (5 s) and lock_timeout (1 s) come from the PG config and
surface as 500s, counted as errors.

## DB CPU per request (what the column is, and what it is not)

PostgreSQL runs in its own cgroup (infra/compose.bench.yaml), and its
`cpu.stat usage_usec` delta over each measured window is published per request
beside the SUT's, under the same request-weighted rule. It exists because this
round is ORM against ORM: without it, "one stack costs more CPU" cannot be
separated from "one stack's generated SQL costs the database more".

Attribution is clean without de-interleaving: a block's two probes run
SEQUENTIALLY, and the other slot's container is stopped while one is under
load, so a window's PostgreSQL delta belongs to exactly one slot. It is
recorded per (slot, block), never per block.

Four limits, all structural, all of which must travel with the number:

1. **Whole-container, not per statement.** The delta includes autovacuum, the
   checkpointer, the WAL writer and the background writer — maintenance that is
   debt from earlier windows and is not proportional to this probe's requests.
2. **Not per endpoint.** Attributing DB cost to a statement needs
   `pg_stat_statements` deltas; a cgroup counter cannot do it, and this round
   does not claim it. `(cell, offered rate, slot)` is the finest key the
   measurement supports.
3. **Driver-sensitive.** See the connection-reset asymmetry above: part of any
   .NET-side DB cost is Npgsql's per-checkout reset, not the runtime.
4. **Absent is null, never zero.** A run with no PostgreSQL cgroup sample (a
   non-Linux run) reports the column as not measured and omits it; it never
   publishes a database that cost nothing.

Redis is sampled the same way. vegeta and the container-runtime units
(docker.service and containerd.service — the shim, and therefore the SUT's
stdout relay, is charged to the latter) are sampled and recorded as
`load_generator_overhead`: published so single-host accounting is auditable,
and never part of any per-request cost.
