# services/java: equivalence notes (measured, not asserted from source)

What the Java SUT ACTUALLY puts on the wire, captured from a live PostgreSQL 18.4 with
`log_statement='all'` and from the packaged jar's own stdout. Every line below is copied from a
capture. The counterpart of `services/dotnet/NOTES-equivalence.md`, which does the same job for
the .NET side.

Rows marked **FAIRNESS** are differences that could not be removed; each has a
`docs/FAIRNESS.md` entry naming the side it costs.

## Reproducing the captures

`./mvnw -B test` runs on a host with no database and no Redis, and reports `LiveEndpointTest`
as skipped. Those are the only tests that exercise the create transaction, the cache-aside read,
the paged list and the existence probes, so run them before trusting a green suite:

```sh
docker run -d --name ledger-pg -e POSTGRES_USER=ledger -e POSTGRES_PASSWORD=ledger \
    -e POSTGRES_DB=ledgerline -p 5432:5432 postgres:18.4
docker run -d --name ledger-redis -p 6379:6379 redis:8.10-alpine
LEDGERLINE_LIVE=1 ./mvnw -B test -Dtest=LiveEndpointTest        # 11 tests
LEDGERLINE_LIVE=1 ./mvnw -B test                                # the whole suite, none skipped
```

The application defaults already point at `localhost:5432` / `localhost:6379` with the
`ledger`/`ledger` credentials; `PG_HOST`, `PG_PORT`, `PG_DB`, `PG_USER`, `PG_PASSWORD`,
`REDIS_ADDR` and `PDF_DIR` override them. The class DROPS and recreates the three tables before
every test from `spec/schema.sql`, so never point it at a database that matters. Add
`-c log_statement=all` to the postgres container to reproduce the statement captures below.

## Statement ledger, per endpoint (captured 2026-08-23, PostgreSQL 18.4)

### `GET /invoices/{id}` — cache miss

```
select i1_0.id,i1_0.customer_id,i1_0.status,i1_0.currency,i1_0.total_minor,i1_0.created_at
  from invoices i1_0 where i1_0.id=$1
select ii1_0.id,ii1_0.sku,ii1_0.description,ii1_0.qty,ii1_0.unit_price_minor,ii1_0.line_total_minor
  from invoice_items ii1_0 where ii1_0.invoice_id=$1 order by ii1_0.id
```

Two statements, no `BEGIN`/`COMMIT`, no `LIMIT 1`. The frozen column ORDER is recovered by the
HQL constructor expression; the table alias is not reachable without native SQL. A cache hit
issues nothing.

**FAIRNESS.** EF opens the connection explicitly so Q1 and Q2 share ONE pooled acquisition; the
two repository calls here take two. A HikariCP checkout is a local handoff with no reset
statement, where an Npgsql checkout sends one (`spec/db-access.md`), so the round-trip counts
differ in opposite directions from the DB-CPU cost. Not equalized: holding one connection across
both reads needs a transaction, and a read transaction puts `BEGIN READ ONLY` / SELECT /
`COMMIT` on the wire — two extra round trips on 30% of the mix, captured through a Spring
`TransactionTemplate` with `readOnly=true`. (The read-only transaction ends with COMMIT.)

### `GET /customers/{id}/invoices` — TWO statement texts

```
page 1: ... order by i1_0.id desc fetch first $2 rows only              (2 parameters)
page 2: ... order by i1_0.id desc offset $2 rows fetch first $3 rows only (3 parameters)
```

**FAIRNESS.** Neither text is the frozen `LIMIT 20 OFFSET $2`. The split comes from Spring Data
skipping `setFirstResult` when the offset is zero, so Java sends one text on page 1 and another
on pages 2-5; both are pinned in `StatementShapeTest`. EF has two texts as well, split on a
different page: `LIMIT $2 OFFSET $3` on every page except page 2, where the two equal-valued
parameters collapse into `LIMIT $2 OFFSET $2`. Each stack therefore occupies TWO
`pg_stat_statements` rows and two prepared statements for this query (`spec/db-access.md`: the
Q3 row of the statement table and the paragraph below it). The plan is identical either way
(backward index range scan on `idx_invoices_customer_id_id_desc`).

The `CustomerExists` probe fires only when the page is empty, on both stacks:

```
SELECT EXISTS(SELECT 1 FROM customers WHERE id = $1) AS exists
```

### `POST /invoices` — the full transaction, captured

```
SELECT EXISTS(SELECT 1 FROM customers WHERE id = $1) AS exists       -- Q7, OUTSIDE the tx
BEGIN
insert into invoices (currency,customer_id,status,total_minor) values ($1,$2,$3,$4)
  returning id,created_at                                            -- Q4
insert into invoice_items (description,invoice_id,line_total_minor,qty,sku,unit_price_minor)
  values ($1,$2,$3,$4,$5,$6) returning id                            -- Q5, xN
  (repeated N times, one prepare/execute each)
UPDATE customers SET balance_minor = balance_minor + $1 WHERE id = $2 -- Q6, atomic increment
COMMIT
```

Two measured deviations remain, both disclosed rather than engineered away:

1. **FAIRNESS — N round trips for N items.** Hibernate's `InsertCoordinatorStandard` drops the
   batch key whenever the identifier is assigned by the insert, and `spec/schema.sql` pins
   `GENERATED ALWAYS AS IDENTITY`. EF batches the same inserts into one round trip.
   `spec/db-access.md`'s "round-trip counts ASSERTED EQUAL" cannot hold for the create path.
   Dropping to `JdbcTemplate.batchUpdate` is rejected: it would make this an
   ORM-against-hand-written-SQL comparison.
2. **Alphabetical insert column order** in Q4 and Q5, against the frozen
   `(customer_id, status, currency, total_minor)`. Semantically identical; a distinct statement
   text. Pinned in `StatementShapeTest`.

`returning id` is the frozen Q5 clause and it costs a non-default:
`hibernate.jdbc.use_get_generated_keys=false`. Left at Hibernate's default the id comes back
through JDBC `getGeneratedKeys`, which PgJDBC implements by appending a newline and
`RETURNING *` client-side — captured, all seven item columns per row against EF's one bigint.
The setting's direction helps Java, so it carries a `docs/FAIRNESS.md` row; it is asserted in
`HibernateSettingsTest` and visible in `StatementShapeTest`.

Isolation is left at `DEFAULT`, which reaches the wire as a bare `BEGIN`. Npgsql always spells
the level out, so .NET sends `BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED` — captured on
both sides, same level, same one round trip, different text. Naming READ_COMMITTED here would
not close it: Spring applies isolation through `Connection.setTransactionIsolation`, which
PgJDBC sends as its own `SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ
COMMITTED` statement ahead of the BEGIN (captured) — a round trip .NET does not pay.

### `POST /invoices/{id}/pdf-stub`

One statement, the Q1 header projection. The items are never loaded, matching every .NET data
layer.

## Log stream (captured from the packaged jar)

Startup stdout is exactly one line, the `config` echo — no banner, no Boot startup lines, no
HikariCP or Hibernate lines. Under traffic the stream carries only the frozen events:

```json
{"ts":"2026-08-23T12:54:58.228347Z","level":"info","msg":"http_access","request_id":"...","method":"GET","path":"/invoices/{id}","status":200,"duration_ms":76.066883}
{"ts":"2026-08-23T12:54:58.339908Z","level":"info","msg":"invoice_created","request_id":"...","customer_id":1,"invoice_id":26,"item_count":3,"total_minor":101499}
{"ts":"2026-08-23T12:54:58.359625Z","level":"error","msg":"request_error","request_id":"...","status":415,"error_class":"validation"}
```

`shutdown_complete` is the last line after a SIGTERM, emitted once in-flight requests have
drained (`server.shutdown: graceful`). Verified twelve times against the packaged jar — six with
stdout on a file and six with stdout on a pipe, which is what the container gets — with a
request served in between: the line is present and last in every run.

Nothing non-contract reaches stdout. The one framework category that measurably did is
`org.springframework.web.servlet.PageNotFound`, which wrote
`{"level":"warn","msg":"No mapping for GET /zzz-unmatched"}` for an unmatched path and
`{"level":"warn","msg":"Request method 'DELETE' is not supported"}` for a wrong method — free
text in the frozen event-name field, one of them carrying a raw URL. Floored at `error` in
application.yaml and asserted in `LoggingContractTest`; re-checked live, three such requests now
add zero lines.

**FAIRNESS — where the line is FORMATTED.** `spec/logging.md` says "the queued unit is a
formatted line both sides". Logback's `AsyncAppender` queues the EVENT and formats it on the
drain thread, so the JSON serialization of every access line runs off the request thread on Java
and on the calling thread on .NET. Total process CPU is unchanged (the headline is a cgroup
figure); per-request latency attribution is not. Replacing `AsyncAppender` with a
format-then-queue appender would be a hand-written sink on one side only.

**FAIRNESS — BODY_MAX_BYTES enforcement layer.** Kestrel enforces the limit BELOW the app;
Tomcat has no container-level cap for `application/json`, so Java enforces it in a filter INSIDE
the app and deliberately does not log the `request_error` it is positioned to log. Same value,
same observable contract (413 + access line, no event), different layer.

## `/runtime-stats`

Shape-identical to the .NET body. Java-side values that are NOT a straight mapping:

| field | Java | why |
|---|---|---|
| `gc.collections.gen0/gen1/gen2` | **null**, with the per-collector bean counters beside them and `collections_note` carrying the reason | G1's old-gen bean counts Full GC only and reads 0 where ZGC's major-cycle bean reads 3 on identical work; neither is the CLR's triple |
| `gc.pause_total_seconds` | G1: the sum over EVERY bean, `G1 Concurrent GC` included. ZGC: **null** + `pause_total_seconds_note` | measured over 30 forced concurrent cycles, `G1 Concurrent GC` reported 60 events / 3 ms, which is the Remark + Cleanup STW sub-pauses (3.42 + 0.10 ms in `-Xlog:gc`) and not the 40.8 ms of concurrent marking — excluding the bean would UNDERCOUNT the pause total. Under ZGC `getCollectionTime()` is whole milliseconds and every pause reports 0 (measured: 255 major pauses, 0 ms), so the field would be a constant 0.0 |
| `gc.heap_goal_bytes` | ZGC: `SoftMaxHeapSize`. G1: **null** | under G1 it reads back equal to `MaxHeapSize`, which is a ceiling, not a goal |
| `db_pool.acquire_count`, `empty_acquire_count`, `acquire_wait_total_seconds` | **null**, as on .NET | `HikariPoolMXBean` exposes four gauges and no cumulative counters; reaching them needs a `MetricsTrackerFactory`, i.e. per-acquire timing work the Npgsql side never pays |
| `goroutines_or_threadpool` | `getMountedVirtualThreadCount()` | the "threads doing work" number, bounded by carrier parallelism the way `dotnet.thread_pool.thread.count` is bounded by the pool |
| `redis_pool.total` | 1 when the Lettuce client exists, else 0 | Lettuce's default is one shared multiplexed connection, the SE.Redis model (`spec/cache.md`). It reports the client's existence, not socket liveness, where .NET reads `IConnectionMultiplexer.IsConnected` |
| `config.max_auto_prepare` | **null**; `config.jdbc_prepare_threshold` = 5 | the behavioural counterpart on this side (`spec/db-access.md`): both stacks promote a statement on its 5th execution |

**Two saturation gauges have no home in the frozen shape.**
`VirtualThreadSchedulerMXBean.getQueuedVirtualThreadCount()` and
`HikariPoolMXBean.getThreadsAwaitingConnection()` are the only Java-side numbers that move with
offered load near the knee (`docs/research/2026-08-23-java-research.md` §4), and
`spec/runtime-stats.md` has no field for either. Adding one is a spec edit, so neither is
reported.

**The frozen keys are all present.** Nothing is omitted and nothing is renamed: the four nulls
above are present-with-a-null, and the Java-only keys (`collections_note`, the per-bean
counters, `pause_total_seconds_note`, `jdbc_prepare_threshold`, `gc_engine`,
`max_ram_percentage`, `max_heap_bytes`, `virtual_threads_enabled`,
`vt_scheduler_parallelism`) sit beside them. A full body is reproducible with
`curl -s localhost:8080/runtime-stats`.

## Response headers (captured from the packaged jar, both services side by side)

| | Java | .NET |
|---|---|---|
| success media type | `application/json` | `application/json` (normalized; it was `; charset=utf-8`) |
| error media type | `application/problem+json` | `application/problem+json` |
| framing | `Content-Length` below Tomcat's 8 KB response buffer, `chunked` above | `chunked`, except the two `Results.Bytes` paths |
| status line | `HTTP/1.1 400 ` (Tomcat writes no reason phrase) | `HTTP/1.1 400 Bad Request` |
| 400/413/500/503 | `Connection: close` | no `Connection` header |
| OPTIONS on a mapped route | 405, `Allow: GET`, frozen internal envelope | identical |

The last three rows are **FAIRNESS** entries. `Connection: close` is Tomcat's
`Http11Processor.statusDropsConnection`, hard-coded for 400/408/411/413/414/500/501/503 with no
connector setting behind it (verified by disassembling tomcat-embed-core 11.0.24); it charges
Java a connection re-establishment per error response, which the steady-state mix never reaches
and the ladder walls do.
