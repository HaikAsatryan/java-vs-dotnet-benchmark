# FAIRNESS: every dial, both sides, and why

Round 2 compares **Java 25 LTS (Spring Boot 4.1.1 + Hibernate 7)** against **.NET 10
(Minimal API + EF Core 10)**. This file is the disclosure list for that
comparison. It carries no round-1 number and no round-1 row: round 1 was Go against .NET,
on a different harness and a different day, and `CLAUDE.md` forbids putting the two next
to each other.

> **Status.** The dials below were frozen before the measured run and governed
> `results/full-20260827T174611-fedora`. What that run measured (gate outcomes, realized rates, cgroup
> accounting) lives in its `report/report.md`, not here: this file carries what was set
> and why, so that the two cannot be edited to agree.

## Principles

1. Idiomatic production stacks, not parity-forced lowest common denominators. Where the
   two frameworks genuinely differ, the asymmetry is kept, named, and measured rather
   than hidden.
2. Every knob on either side is either (a) the documented default, or (b) set identically
   on both sides, or (c) an explicitly labelled variant axis, or (d) a deliberate
   exception listed here with the direction it biases.
3. `spec/` is the authority. Where the spec pins a behaviour, both stacks match the spec —
   including changing `services/dotnet`, which predates this round and is not
   automatically right.
   Where the spec is silent the two must still be identical; where they cannot be, the
   difference is a row below **naming the side it costs**.
4. Nothing is declared here that is not implemented and tested. A dial that was specified
   but never verified supports nothing.

## Asymmetries kept on purpose

| axis | Java | .NET | treatment |
|---|---|---|---|
| Data layer | Spring Data JPA + Hibernate 7.4.5 over PgJDBC | EF Core 10 over Npgsql | the headline pairing: two full ORMs, not an ORM against hand-written SQL. `DATA_LAYER` has no Java value: the .NET side keeps its `ado` and `dapper` data layers as code (the P2 shakedown runs `ado` as a slot; D5 declares no data-layer variant cells), the Java side has one layer, and no Java number is compared against a .NET variant |
| Prepared statements | PgJDBC `prepareThreshold=5`, the driver DEFAULT: the 5th execution of a statement becomes a named server-side prepared statement | Npgsql `Max Auto Prepare=20`, `Min Usages=5` — **explicitly set, NOT the Npgsql default (0/off)** | the baseline equalizes the BEHAVIOUR (both promote on the 5th execution) at the cost of one side's literal default. `variant.npgsql-default-prepare` keeps the default-vs-default contrast as a labelled cell. Rationale and the measured `execute <unnamed>` → `execute S_1` transition in `spec/db-access.md` |
| Redis client model | Lettuce, one shared multiplexed connection (its default) | StackExchange.Redis multiplexer | the same model on both sides. `REDIS_POOL_MAX` therefore applies literally to neither and is recorded, not applied; `redis_pool.total` is 0 or 1 on both |
| GC posture (headline) | G1; `MaxRAMPercentage` and `ActiveProcessorCount` are SET EXPLICITLY in the bench overlay to their default-equivalent values (25, the cpuset size) so the manifest can evidence them | Server GC + DATAS, both defaults | pure documented defaults under the same 12 GiB container limit, which give the CLR ≈75% of the limit and the JVM ≈25% — **unequal ceilings, kept and disclosed**. Resolved at G6 (2026-08-27) exactly as D6 pre-registered: the JVM's `gc.heap_committed_bytes` peaked at **298 MB** across every measured window of the G6 rehearsal and at 328 MB in the full run (each run's report, "GC and pool activity"), against the ~3 GiB default ceiling — the ceiling never binds, so the once-declared heap-equalised second cell (`gc-generous`: JVM `MaxRAMPercentage=75` + generational ZGC vs the CLR with `DOTNET_GCDynamicAdaptationMode=0`, DATAS off) measured nothing the defaults cell does not, and was deleted per D6's one-cell-defaults-done rule. |
| Compilation model | Temurin JDK, tiered JIT, no AOT cache, no CDS archive beyond the JDK's own default | `dotnet publish -c Release`: tiered JIT with dynamic PGO, **no ReadyToRun, no Native AOT** | both are the documented default for their stack and neither is tuned. Native AOT is not used: it does not cover the reflection surface EF Core depends on, so publishing AOT would measure a different application. JIT and JVM warm-up cost is what the warm-up gates exclude, and the time-to-settle figure (`docs/figures/fig5-warmup.png`) reports it separately rather than amortising it away; process start-up itself is not recorded |
| Concurrency model | thread-per-request on virtual threads (`spring.threads.virtual.enabled=true`) | `async`/`await` on the thread pool | both are the current idiomatic default for their stack. The consequence for `/runtime-stats` is a disclosed row below |
| Request scheduling under load | the JDK's virtual-thread scheduler does not time-share (JEP 444): a CPU-heavy handler holds its carrier thread until it finishes, with carriers equal to the cpuset size. `server.tomcat.threads.max` is inert under virtual threads, so nothing caps concurrent handlers or bounds the queue in front of them | the thread pool injects threads when it detects starvation | a structural property of each stack's default concurrency model, not a setting. Where it bites it costs Java's tail latency, and past the knee excess load on the Java side queues in the scheduler instead of being refused. Kept at defaults per D12 and not measured as a separate cell; `jdk.virtualThreadScheduler.parallelism` is left unset. A Jetty build of the same application would cap handlers at `server.jetty.threads.max` (default 200) and show a different tail (`docs/research/2026-08-23-java-research.md`). The connection-level cap has its own row in the non-defaults table |
| Entity modelling | `customer_id` and `invoice_id` are plain `long` columns; there is no `@ManyToOne` or `@OneToMany` association in `services/java` | the relationships are declared in `LedgerDbContext`; the only navigation property is `Invoice.Items`, used on the create path | the Java model is the closest mirror of the EF one, and it means Hibernate's association, proxy and collection machinery is never exercised: the benchmark measures Hibernate in its cheapest configuration. Direction: helps Java; size not measured. Hibernate's authors argue against this modelling style (quoted in `docs/research/2026-08-23-java-research.md`), so a service modelled with associations is not the one measured here |
| Runtime-internal threads | GC and JIT threads run inside the SUT cgroup: at the 6-core cpuset, `ParallelGCThreads=6`, `ConcGCThreads=2` and `CICompilerCount=3` (measured, `docs/research/2026-08-21-measured.md`) | Server GC and tiered-JIT background compilation threads, also inside the SUT cgroup, in a different number | counted on both sides: the measured CPU is the whole cgroup, so it includes what each runtime spends on collection and compilation. That is the intended accounting; it is named here because the thread counts differ, and both follow from the pinned core count and each runtime's defaults |
| Dependency versions | every dependency version in `services/java/pom.xml` comes from the Spring Boot 4.1.1 BOM and none is overridden | framework packages are SDK/metapackage-managed | one rule on both sides (`PROGRESS.md`, O6): each stack ships what its framework ships, with directly-referenced packages at latest stable on both sides at run start. The BOM is deliberately not overridden one artifact at a time, so where a newer standalone release of a BOM-managed library exists, the BOM's version is the one measured |

## Removed under the strict-canonicality rule (2026-08-27)

The rule: **each SUT is what an ordinary production team ships with the default project
template and mainstream practice — no benchmark-motivated optimizations.** The Java side
already followed it (its Dockerfile deliberately refuses Spring Boot's CDS/AOT container
guidance so both stacks run JIT-from-cold). Four .NET non-defaults were removed under it;
they are recorded here so they are neither re-introduced nor silently forgotten.

- **EF compiled model** (committed `dotnet ef dbcontext optimize` output, wired with
  `.UseModel(...)`). It was the largest single exception to principle 2: it removed EF's
  runtime model build and lowered first-query cost. Stock EF now builds the model at
  startup from `OnModelCreating`, exactly as Hibernate builds its metamodel at boot, so
  the model build sits on both sides' cold-start path. Removing it raises .NET's
  cold-start and first-query cost and touches no steady-state number.
- **STJ source-gen context** (`LedgerJsonContext`). Serialization is now reflection-based
  System.Text.Json at ASP.NET's Web defaults — the template default. Wire bytes are
  unchanged: every DTO member carries an explicit `[JsonPropertyName]`, which overrides
  any naming policy, and the wire-contract tests assert the frozen bytes.
- **`WebApplication.CreateSlimBuilder`** (now `CreateBuilder`, the template default). The
  frozen stdout contract survives because `Program.cs` clears the default logging
  providers and registers only the custom formatter; the log tests assert it. Neither
  builder registers static-file middleware, so the unmatched-path posture the
  `add-mappings: false` row below equalizes against is unchanged.
- **Chiseled runtime image + `<InvariantGlobalization>true</InvariantGlobalization>`**
  (now the standard `mcr.microsoft.com/dotnet/aspnet:10.0-noble` with full ICU). The
  globalization posture is now SYMMETRIC: Java ships a full JRE with ICU, and the
  standard aspnet image is what ordinary teams deploy. This raises the .NET image size
  and memory baseline relative to the trimmed posture it replaces.

## Non-defaults, per side, with the direction each moves

Everything set in `services/java/src/main/resources/application.yaml` that is not a
framework default, and the .NET counterparts. (Three contract-motivated settings
carry in-file comments instead of rows — `property-naming-strategy: SNAKE_CASE`
mirrors .NET's per-member `[JsonPropertyName]`, `ddl-auto: none` pins schema
management OFF against the shared DB, `jdbc.time_zone: UTC` pins the timestamp
contract; none is a performance dial.)

| setting | side | direction | why |
|---|---|---|---|
| `spring.jpa.open-in-view: false` | Java | **helps Java** (less pool occupancy per request) | Boot's default is `true` and it writes a startup WARN when left implicit, which would put a non-contract line on the frozen stdout stream. No .NET analogue exists to disable |
| `hibernate.jdbc.use_get_generated_keys: false` | Java | **helps Java** | spec conformance, not tuning: at Hibernate's default the item id comes back through JDBC `getGeneratedKeys`, and PgJDBC implements that by appending `RETURNING *` client-side — measured, all seven item columns per row where `spec/db-access.md` freezes `RETURNING id` and EF ships one bigint. Asserted in `HibernateSettingsTest` and `StatementShapeTest` |
| `server.tomcat.max-keep-alive-requests: -1` | Java | equalization | Tomcat's default caps a connection at 100 requests; Kestrel has no per-connection cap, so the default would charge Java a connection re-establishment every 100 requests at every offered rate |
| `spring.jackson.mapper.allow-coercion-of-scalars: false`, `accept-float-as-int: false`, plus `io.ledgerline.json.ScalarCoercion` | Java | contract equalization, both directions | measured on both live services: at Jackson's defaults `{"qty":5.7}` was a 201 with `qty` truncated to 5 where System.Text.Json throws; with the blanket switch alone `{"qty":"5"}` became a body-400 where `JsonSerializerDefaults.Web` accepts it, and `{"currency":840}` bound as `"840"` where System.Text.Json throws. All three now match |
| `spring.web.resources.add-mappings: false` | Java | equalization | left at its default Boot maps a resource handler at `/**`, so every unmatched path MATCHES and logs an access line whose `path` is the literal `/**` — unbounded work and a wrong route template. The .NET side registers no static-file middleware (`CreateBuilder` adds none without an explicit `UseStaticFiles`), so the unmatched request is free there |
| `spring.data.redis.repositories.enabled: false`, `spring.main.banner-mode: off`, `log-startup-info: false`, `logging.level.*` floors, `logging.level.org.springframework.web.servlet.PageNotFound: error` | Java | log-contract equalization | `spec/logging.md` freezes the event set as the ONLY lines on stdout. The `PageNotFound` floor is the fourth of the targeted category filters `Program.cs` carries three of: measured, an unmatched path wrote `{"level":"warn","msg":"No mapping for GET /zzz-unmatched"}` and a wrong method wrote `{"level":"warn","msg":"Request method 'DELETE' is not supported"}` — free text in the field the contract freezes as the event NAME, one of them carrying a raw URL into a stream pinned to route-template cardinality. .NET emitted neither |
| `spring.data.redis.timeout: 5s` | Java | **costs Java** | Lettuce's own default is 60 s; SE.Redis's `SyncTimeout` is 5000 ms and `Program.cs` leaves it there. Equalizing toward the other side's default is strictly less permissive for Java |
| `server.shutdown: graceful` | Java | equalization | Kestrel drains in-flight requests by default; Boot does not, and `spec/logging.md` pins `shutdown_complete` as the last line after the drain |
| `Max Auto Prepare=20`, `Auto Prepare Min Usages=5` | .NET | see the prepared-statement axis above | |
| `AddDbContextPool` (vs the baseline `AddDbContext`) | .NET | **helps .NET** (per-request allocation) | pools and resets `DbContext` instances instead of constructing one per request. Non-default, with no Hibernate analogue to set either way: Spring builds no per-request context object of this kind (the `EntityManager` is a proxy over a per-transaction session regardless). Ruled canonical-mainstream and kept; disclosed because it is not the documented baseline registration |
| `Database.AutoSavepointsEnabled = false` | .NET | statement-stream parity, costs neither | at EF's default, the create path's `SaveChanges` inside the explicit transaction wraps itself in a `SAVEPOINT` — an extra wire command inside the tx that Hibernate never issues, and `spec/db-access.md` freezes the .NET create at exactly Q7/BEGIN/Q4/one item batch/Q6/COMMIT ("Automatic savepoints are disabled"). What is given up is rollback granularity the workload never uses |
| Kestrel `RequestHeadersTimeout=5s`, `KeepAliveTimeout=120s` + `server.tomcat.connection-timeout: 5s`, `keep-alive-timeout: 120s` | both | equalization | neither framework's default (Kestrel: 30 s / 130 s); one pinned pair applied identically on both sides so neither stack times out a keep-alive connection the other keeps |
| `AddServerHeader = false` | .NET | wire parity, costs neither | Kestrel's default writes `Server: Kestrel` on every response; Tomcat sends no `Server` header. Removing it keeps the response header sets identical |
| `ConsoleLoggerOptions.QueueFullMode = Wait`, `MaxQueueLength = LOG_QUEUE_LEN`, plus the `Microsoft.EntityFrameworkCore` and `Microsoft.Hosting.Lifetime` `Warning` filters | .NET | log-contract equalization | `spec/logging.md`'s lossless bounded sink: the console logger's default `DropWrite` starts dropping access lines under exactly the load being measured. The two category filters beyond the template's `Microsoft.AspNetCore` floor serve the same objective as the Java side's `logging.level.*` floors: contract lines only on stdout |
| `AbortOnConnectFail = false` | .NET | behaviour parity, costs neither | SE.Redis's default aborts client construction when Redis is unreachable; Lettuce connects lazily and reconnects in the background. Readiness is `/readyz`'s Redis PING on both sides, and a measured window never runs with Redis down |
| `RouteHandlerOptions.ThrowOnBadRequest = true` | .NET | contract | defaults to true only in Development; without it the Production container emits a bare framework 400 with none of the frozen contract |
| `ConfigurationOptions.CommandMap` without `UNLINK` | .NET | **costs .NET, marginally** | `spec/cache.md` freezes the invalidation as `DEL`. SE.Redis prefers `UNLINK` where the server supports it, and `UNLINK` frees the value on a background thread where `DEL` frees it inline — a real difference in Redis-side cost on the create path, not a spelling |
| response `Content-Type` normalized to `application/json` | .NET | **costs neither; removes 15 bytes/response from .NET** | `spec/openapi.yaml` names the media type with no parameter, and the service was already internally split: `ProblemDetails` wrote a bare `application/problem+json` while `TypedResults` appended `; charset=utf-8`. Normalized in `RequestIdMiddleware`'s existing `OnStarting` callback because `TypedResults.Created` takes no content type |
| `server.tomcat.max-connections` (8192) vs Kestrel `MaxConcurrentConnections` (unlimited) | both, by DEFAULT | **costs Java at the ladder wall only** | neither side sets a connection cap; the DOCUMENTED DEFAULTS differ (Tomcat caps concurrent connections at 8192 with an accept backlog of 100; Kestrel is uncapped). Under the open-loop attack at supra-knee rungs, in-flight connections can exceed 8192 on the Java side only, and refused connections land in the `error_rate` fit-exclusion — an asymmetric mechanism at saturation. Near-knee concurrency is far below 8192, so the HEADLINE operating point is unaffected; kept at defaults per D12 and disclosed here (P7 review finding M4) |
| `redis_pool.in_use` reports different QUANTITIES | both | costs neither | SE.Redis exposes a live in-flight count (`GetCounters().TotalOutstanding`, measured 39 under load); Lettuce's multiplexer has no per-command checkout, so Java reports a structural 0. A reader holding two `/runtime-stats` bodies side by side must not read the gap as a pooling difference — it is a different quantity in kind, like `goroutines_or_threadpool`. Promised as a row by `spec/cache.md` and `spec/runtime-stats.md`; delivered here (P7 review finding M5) |

## Identical by construction (spec + tests)

Wire contract (response shapes, the frozen error strings, timestamps at exactly six
fractional digits, money as int64 minor units, snake_case names), the validation
errors-map keys and messages, the request content-type gate, the cache key/TTL/
invalidation order and the serialize-then-cache pin, the frozen log event set and the
bounded block-on-full sink (queue 8192, no discard, full drain on stop — asserted as
behaviour in `LoggingContractTest`, not only as settings), the create transaction's
statement set and ordering, pool sizes (single env var, min=max, warmed to max
CONCURRENTLY on both sides before traffic), the HTTP server limits actually pinned
(body-size cap, header/keep-alive timeouts, the keep-alive request cap — the
CONNECTION caps differ by documented default and have their own row in the non-defaults
table above), the container resource model, and core parallelism
(`-XX:ActiveProcessorCount` / `DOTNET_PROCESSOR_COUNT` pinned
to the SUT core count, asserted from `/runtime-stats build.processor_count`, and the pin
itself asserted to reach `availableProcessors()` in `ServerPostureTest`).

**SQL parity is narrower than "identical".** The equivalence suite asserts the wire
contract and the read-path statement SHAPES; it does not assert byte-identical SQL against
`spec/db-access.sql` on either side. The pinned deviations are in the divergence list
below and in each service's `NOTES-equivalence.md`.

## Known divergences (disclosed), each with the side it costs

Every entry was MEASURED against both services running on one shared PostgreSQL 18.4 and
one shared Redis, by diffing response bytes, `log_statement='all'` output, `MONITOR`
output and stdout.

### Inside the measured workload

- **Item-insert round trips — costs Java.** Hibernate's `InsertCoordinatorStandard` drops
  the batch key whenever the identifier is assigned by the insert, and `spec/schema.sql`
  pins `GENERATED ALWAYS AS IDENTITY`, so N items cost N prepare/execute round trips where
  EF batches the same inserts into one. `spec/db-access.md`'s "round-trip counts ASSERTED
  EQUAL" cannot hold for the create path (15% of the mix). Dropping to
  `JdbcTemplate.batchUpdate` is rejected: it would rebuild an ORM-against-hand-SQL
  asymmetry, which is the thing this round exists to avoid.
- **Two Q3 statement texts on each stack, split on different pages.** Spring Data omits
  `setFirstResult` when the offset is zero, so Java's page 1 ships
  `... fetch first $2 rows only` (two parameters) and its pages 2-5 ship
  `... offset $2 rows fetch first $3 rows only` (three). EF ships `LIMIT $2 OFFSET $3`
  on every page except page 2, where offset equals limit and EF/Npgsql collapse the two
  equal-valued parameters into `LIMIT $2 OFFSET $2`. Each stack therefore occupies two
  `pg_stat_statements` queryids and, under the prepare modes in force, two server-side
  prepared statements for one logical query (`spec/db-access.md`, "Generated-SQL
  deviations, both stacks"). The spec claims plan equivalence for these texts (a backward
  index range scan on `idx_invoices_customer_id_id_desc`); `docs/METHODOLOGY.md`, "Spec
  errata", says what evidence is published for that claim. The Java texts are pinned in
  `StatementShapeTest`; on the .NET side `EfQueryShapeTests` pins the `LIMIT $n`
  parameterization and not the page-2 collapse.
- **Result wire formats: different per driver, side not established.** Both drivers send
  binary parameters from the first execution. Npgsql requests binary results for every
  column from the first execution. PgJDBC requests text results until the statement is
  server-named at `prepareThreshold=5`, and after that binary only for the types it has
  binary support for: on Q3, `status` and `currency` stay text on every execution.
  Captured on Q3 through a wire tap that decodes `Bind` format codes; Q1 selects the same
  six columns and was not put on the tap. It is a per-column encode/decode difference in
  PostgreSQL's cgroup as well as the SUT's. Not equalized: both are their driver's stock
  behaviour (`spec/db-access.md`, "Wire formats, measured").
- **Cache-miss read path — costs Java in checkouts, costs .NET in DB CPU.** EF opens the
  connection explicitly so Q1 and Q2 share one pooled acquisition; the two Spring Data
  repository calls take two. A HikariCP checkout is a local handoff that sends nothing,
  where an Npgsql checkout sends a reset statement (`DISCARD ALL` at `Max Auto Prepare=0`,
  narrowing once statements are auto-prepared) — measured, two `DISCARD ALL` per create on
  the .NET side and none on the Java side. Not equalized: holding one connection across
  both reads needs a transaction, and a Spring read-only transaction reaches the wire as
  `BEGIN READ ONLY` / SELECT / `COMMIT` — measured — i.e. two extra round trips on 30% of
  the mix.
- **JSON serialization — both runtime-built, runtime-cached.** .NET serializes through
  System.Text.Json's reflection-based resolver at ASP.NET's Web defaults; Jackson 3 builds
  and caches serializers at runtime. Both are the template default for their stack, both
  pay their metadata build once per type at first use, and the cached read path is byte
  passthrough on both sides. No STJ source-gen context is used (see the removal list
  above). What remains is the two stacks' serializers being different code with different
  costs: priced by the measurement, not disclosed as a configuration asymmetry.
- **`Connection: close` on Java error responses — costs Java.** Tomcat's
  `Http11Processor.statusDropsConnection` is hard-coded to drop keep-alive on 400, 408,
  411, 413, 414, 500, 501 and 503, and there is no connector setting for it (verified by
  disassembly of tomcat-embed-core 11.0.24). Kestrel sends no `Connection` header on any of
  them. Measured on both live services: a Java 400 closes the connection and the next
  request opens a new one; .NET's does not. The frozen steady-state mix draws only seeded
  ids and never reaches an error path, so the headline is unaffected — but a `/readyz` 503
  during a capacity-gate stall, and every rung at the ladder wall where the SUT starts
  returning 500s, charges Java a connection re-establishment per error that .NET does not
  pay. Cannot be removed without subclassing `Http11Processor`.
- **Response framing — costs .NET a little, costs Java a buffer.** Java sends
  `Content-Length` for anything that fits Tomcat's 8 KB response buffer and
  `Transfer-Encoding: chunked` above it; .NET sends `chunked` for everything written
  through `TypedResults` and `Content-Length` on the two `Results.Bytes` paths
  (`GET /invoices/{id}`, `/runtime-stats`). Measured: `GET /healthz` is
  `Content-Length: 15` against chunked; the 20-row list page is `Content-Length: 2463`
  against chunked; `GET /invoices/{id}` is `Content-Length: 230` on BOTH. Chunk framing is
  a handful of bytes per response and one more buffer write on the .NET side; forcing the
  other direction is what is rejected — making Java always chunk means flushing the buffer
  by hand on every response, and making .NET always length-prefix means serializing every
  response into a fresh `byte[]` first, an allocation regression on the incumbent's hot
  paths. Framing changes vegeta's byte accounting, so it is recorded here rather than
  treated as cosmetic.
- **HTTP status reason phrase — costs .NET a few bytes.** Tomcat writes `HTTP/1.1 400 `
  with no reason phrase (RFC 9110 permits it and Tomcat has no switch); Kestrel writes
  `HTTP/1.1 400 Bad Request`. Two to fifteen bytes per response, on every response, and it
  is on the same wire-byte ledger as the framing row above.
- **Where the log line is FORMATTED — costs neither in total CPU, differs in
  attribution.** `spec/logging.md` says "the queued unit is a formatted line both sides".
  Logback's `AsyncAppender` queues the EVENT and formats it on the drain thread, so the
  JSON serialization of every access line runs off the request thread on Java and on the
  calling thread on .NET. Total process CPU is unchanged and the headline is a cgroup
  figure, so this moves per-request latency attribution only. Replacing `AsyncAppender`
  with a format-then-queue appender would be a hand-written sink on one side only.
- **`BODY_MAX_BYTES` enforcement layer — symmetric outcome, different layer.** Kestrel
  enforces the limit below the app; Tomcat has no container-level cap for
  `application/json`, so Java enforces it in a filter INSIDE the app and deliberately does
  not log the `request_error` it is positioned to log. Same value, same observable contract
  (413 plus an access line, no event).

### Statement text, same semantics

- **`BEGIN` against `BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED` — costs neither.**
  Measured: Java sends a bare `BEGIN`, .NET sends the explicit form. Both are READ
  COMMITTED (PostgreSQL's default) and both are one round trip. Neither can be moved:
  Npgsql always spells the level out, and naming `READ_COMMITTED` on the Spring side makes
  PgJDBC send its own `SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ
  COMMITTED` statement ahead of the BEGIN (measured) — a round trip .NET does not pay.
  It shows up as two rows in a `pg_stat_statements` diff and nothing else.
- **Alphabetical insert column order — costs neither.** Hibernate orders insert columns
  alphabetically, so Q4 and Q5 differ from the frozen
  `(customer_id, status, currency, total_minor)` text and from EF's. Semantically
  identical; a distinct statement text. Pinned in `StatementShapeTest`.
- **Redis `SET k v EX 300` against `SETEX k 300 v` — costs neither.** `spec/cache.md`
  writes the read-path store as `SET … EX`, which is what Lettuce sends; SE.Redis's typed
  API compiles a whole-second TTL at `When.Always` to `SETEX` and offers no route to the
  other spelling. They are the same operation with the same effect and the same one round
  trip — unlike `DEL`/`UNLINK`, which is a real behavioural difference and was fixed on the
  .NET side. Reaching `SET … EX` from SE.Redis needs a raw `Execute` with a boxed argument
  array on the cache-fill path, and it would also give up key-based server selection; the
  command name is left different and recorded so a `commandstats` diff is not read as an
  asymmetry.

### Outside the measured workload

- **Explicit JSON `null` in a numeric field — costs neither; different bytes.**
  `{"qty":null}` is a deserialization error on .NET (non-nullable `int`), so it takes the
  undeserializable-body path: the frozen `{"body":["is required"]}` plus
  `msg="request_error" error_class="validation"`. A boxed Java component binds the null and
  Jakarta answers with the field-level `{"Items[0].Qty":["must be between 1 and 1000"]}` and
  no `request_error`. Neither a primitive component nor `@JsonSetter(nulls = FAIL)` closes
  it: both also reject the ABSENT field, which is the case the workload and the golden
  vectors actually exercise and where the two stacks already agree. Asserted in
  `ErrorContractTest`.
- **`goroutines_or_threadpool` — a different quantity in kind, not a cost.**
  `spec/runtime-stats.md` maps the field to .NET's `dotnet.thread_pool.thread.count`, a
  pool size that persists between requests. The JVM analogue reported here is
  `VirtualThreadSchedulerMXBean.getMountedVirtualThreadCount()`, an instantaneous
  carrier-occupancy gauge bounded by parallelism, which reads 1 at idle against .NET's 5.
  The field is comparable in DIRECTION under load and not in absolute value; it is a
  warm-up and corroboration signal, and no published number is computed from it. The
  alternative — reporting `ThreadMXBean.getThreadCount()` — would count carrier threads and
  be equally incomparable while looking more like the .NET number.
- **`gc.collections.gen0/gen1/gen2` — null on Java.** G1's `G1 Old Generation` bean
  increments on Full GC ONLY, so it reads 0 where ZGC's `ZGC Major Cycles` reads 3 on
  identical work, and neither is the CLR's generation triple. The frozen keys are present
  and null, with the real per-collector counters beside them keyed by
  `GarbageCollectorMXBean` name and a `collections_note` carrying the reason. A fabricated
  triple would be a number that means a different thing on each side.
- **`gc.pause_total_seconds` — null under ZGC only.** `getCollectionTime()` is whole
  milliseconds and ZGC records every pause as 0 ms (measured: 255 major pauses, 0 ms), so
  the field would read as a constant 0.0 under the collector where pause time is the
  interesting variable. No published cell runs ZGC, so this applies only when the service
  is started under it. Under G1 — the headline collector — it IS reported, as the sum over
  every bean including `G1 Concurrent GC`: that bean's time is not concurrent wall time but
  the Remark and Cleanup stop-the-world sub-pauses (measured: 60 events / 3 ms against
  40.8 ms of concurrent marking in the same run), so excluding it would undercount the
  pause total. Pinned in `JvmGcBeansTest`.
- **`gc.heap_goal_bytes` — null under G1, `SoftMaxHeapSize` under ZGC.** Under G1
  `SoftMaxHeapSize` reads back equal to `MaxHeapSize`, which is a ceiling and not a goal.
  .NET reports null for the same field (no DATAS analogue). The published cell runs G1,
  so the field is null on both sides there.
- **`db_pool.acquire_count` / `empty_acquire_count` / `acquire_wait_total_seconds` — null
  on both.** `HikariPoolMXBean` exposes four gauges and no cumulative counters; reaching
  them needs a `MetricsTrackerFactory`, i.e. per-acquire timing work the Npgsql side never
  pays. Npgsql's meters do not expose them either. Symmetric null.
- **Two saturation gauges have no home in the frozen shape.**
  `VirtualThreadSchedulerMXBean.getQueuedVirtualThreadCount()` and
  `HikariPoolMXBean.getThreadsAwaitingConnection()` are the Java-side numbers that move with
  offered load near the knee, and `spec/runtime-stats.md` has no field for either. Adding
  one is a spec edit, so neither gauge is published, under a new key or an existing one.

### Eliminated during the P5 parity pass (recorded so they are not re-introduced)

| was | now |
|---|---|
| Java `application/json`, .NET `application/json; charset=utf-8` — 15 bytes on every response | both bare `application/json`; `EndpointContractTest` asserts it |
| Q5 shipped `RETURNING *` on the Java wire against EF's `RETURNING id` | both `RETURNING id`; `HibernateSettingsTest` + `StatementShapeTest` pin it |
| .NET invalidated with `UNLINK` where `spec/cache.md` freezes `DEL` | both `DEL`, verified with `MONITOR` |
| OPTIONS on a mapped route: Java 200 with `Allow: GET,HEAD,OPTIONS` and an empty `Accept-Patch` header, .NET the frozen 405 | both 405 with `Allow: GET` and the frozen internal envelope |
| Non-contract Spring WARN lines on stdout carrying the raw URL in `msg` | suppressed; asserted in `LoggingContractTest` |
| `{"qty":"5"}` refused by Java and accepted by .NET; `{"currency":840}` accepted by Java and refused by .NET | both match System.Text.Json's Web defaults; asserted in `ErrorContractTest` |
| Startup pool warm opened `DB_POOL_MAX` connections sequentially on Java (measured 277/75/74 ms) against .NET's concurrent `Task.WhenAll` (18/11/11 ms) — ~60-260 ms charged to Java on the cold-start curve | both concurrent |
