# P3 — Java research, 2026-08-23 (gate G3)

> **Frozen at publication, 2026-10-12.** This is a working research note from 2026-08-23,
> kept as written apart from a few wording edits. Its dials table was the input to
> `docs/FAIRNESS.md`, which is the published disclosure list and wins where they differ.
> The open questions in §7 were decided or dropped later: outcomes are in `PROGRESS.md` (O6
> for the version-pin rule J1, O7 for the StackExchange.Redis refresh J18, the P7 review),
> `docs/FAIRNESS.md` and `spec/`. Probe sources for its measurements were not kept.

Consolidation of five parallel research passes run today. Every version string, default and
API signature below is either **measured on this box today**, **fetched live today** (URL
given), or **labelled `[UNVERIFIED]` inline**. Nothing is recalled.

Inputs, still valid, not repeated here: `docs/research/2026-08-21-measured.md` (the
Hibernate SQL spike, the GC-bean mapping, the wire timestamp formatter, the cpuset
finding), `docs/research/2026-08-21-java-stack.md` (the A1–A31 asymmetry register) and
`docs/research/2026-08-21-critique.md` (the adversarial pass, several items still OPEN).
This file supersedes them where it says so and is the input to `docs/FAIRNESS.md`, which
W31 rewrites from a skeleton — **not edited here by design**.

> **G3 wording vs W31 ordering.** P3's gate says "findings written into `docs/FAIRNESS.md`
> as a dials table". W31 rewrites that file from a skeleton, deliberately last, because
> round-1 numbers are woven into its subordinate clauses. The dials table therefore lands
> **here** and W31 lifts it. Someone has to ratify that reading of G3; it is listed in §7.

---

## 0. What changed in the two days since `2026-08-21-*.md`

| # | change | consequence |
|---|---|---|
| 0.1 | **Hibernate ORM 7.4.6.Final published today**, 2026-08-23 00:23:28 GMT. Boot 4.1.1 pins 7.4.5.Final. | Override question, §2 / §7. The measured SQL spike was taken on 7.4.5. |
| 0.2 | **Temurin `jdk-25.0.4.1+1` now HAS linux x64 glibc JDK+JRE tarballs** (published 2026-08-21T11:40Z) and a Fedora 43 RPM. `2026-08-21-java-stack.md` §1's "do not use — no linux x64 glibc asset exists" is **STALE and must not reach `spec/` or the Dockerfile**. | §2. Recommendation is still *stay on 25.0.4+7*, for a different reason. |
| 0.3 | **`eclipse-temurin` images rebuilt to `25.0.4_7`**, matching this host exactly. `2026-08-21-measured.md`'s "container lags the host at 25.0.3_9" is **stale**. | P4 can pin `eclipse-temurin:25.0.4_7-jre-noble` and get host/container parity. |
| 0.4 | Tomcat 11.0.25 published 2026-08-12; Boot 4.1.1 pins 11.0.24 (2026-07-03). This row was flagged unverified on 2026-08-21; it is now checked. | Feeds the version-currency item, §1.1 [4-A]. |
| 0.5 | Lettuce **7.7.0.RELEASE** (2026-08-18) exists and lists Redis **8.10** among tested servers; `infra/compose.yaml` pins `redis:8.10.0-trixie`. On 2026-08-21 that option did not exist, so the prior doc chose disclosure. | §1.1 [4-A]; the disclosure/override choice is now live. |
| 0.6 | `spring.mvc.problemdetails.enabled` default `false` in Boot 4.1.1 — previously read through a search summary and labelled "likely"; now read out of the artifact. | Settled; category D. |
| 0.7 | The **errors-map key format**, the **`instance` back-fill** and the **advice shape** are all now measured working against real Boot 4.1.1 MVC (§6, §3). These were open. | Removes the three highest-probability P4 equivalence failures. |

Unchanged and re-confirmed today: Spring Boot **4.1.1** is `"current": true` GA
(`api.spring.io/projects/spring-boot/releases`, fetched 2026-08-23); Maven **3.9.16** is
still the newest 3.9.x and 4.0.0 is still `rc-6`, so **D11 holds**; Spring Framework 7.0.9
is both the BOM pin and the latest GA; PgJDBC 42.7.13 is both.

---

## 1. The dials table — the P3 gate

Categories are the three the P7 reviewer brief allows:

- **D** — documented default. Either unset, or pinned to the value the framework/runtime
  already defaults to, for auditability. Read back at runtime wherever a read-back exists.
- **I** — set identically on both sides. Same value, or the same observable behaviour with
  the .NET counterpart named. A `.NET counterpart` column entry of "none exists" makes this
  claim inadmissible — those rows are **[4]**.
- **V** — explicitly labelled variant axis, i.e. a declared second cell.
- **[4]** — **fits none of the three.** Flagged loudly. Enumerated with dispositions in §1.1.

Every JVM row is read back through `HotSpotDiagnosticMXBean.getVMOption(name)`, which
returns an `origin` (DEFAULT / ERGONOMIC / VM_CREATION) — that is what turns "this is a
default" into an artifact rather than a claim (measured 2026-08-21, `MxProbe.java`).

### 1a. JVM flags (composed once in `infra/compose.yaml` into `JDK_JAVA_OPTIONS`)

| dial | Java value | .NET counterpart | cat | evidence |
|---|---|---|---|---|
| GC engine | **nothing passed** | no GC flags; server GC | D | `UseG1GC=true {ergonomic}` measured in the target container under `--cpuset-cpus=0-5 -m 12g` (2026-08-21). Passing `-XX:+UseG1GC` would make a default look tuned. |
| `-XX:MaxRAMPercentage` | unset → **25.0** | CLR hard limit 75% (`GCHeapHardLimit = 9,663,676,416`) | D | `MaxRAMPercentage=25.0 {DEFAULT}`, `MaxHeapSize = 3,221,225,472` measured under `-m 12g`. D6 / "The heap question". |
| `-XX:MaxRAMPercentage=75` + `-XX:+UseZGC` | 75.0, ZGC | CLR 75% + DATAS | **V** | `headline.heap-equalized` only, pending D6 at G6. Never write `-XX:+ZGenerational` — measured: `Ignoring option ZGenerational; support was removed in 24.0`. |
| `-XX:ActiveProcessorCount` | `${SUT_CORES}` = 6 | `DOTNET_PROCESSOR_COUNT` | I | Provable no-op on JDK 25: `availableProcessors()==6` with the flag unset (measured). Set for symmetry. **Assert `availableProcessors()==SUT_CORES`, not the flag** — it reads back `-1 {DEFAULT}` when unset. |
| `-XX:+UseCompactObjectHeaders` | not set (false) | — | D | `false {DEFAULT}` measured. Product in 25 (JEP 519), default in 27. Enabling it in the defaults cell is gaming; legitimate **V** candidate only if a memory-tuned cell is ever declared. |
| `-XX:+UseTransparentHugePages` | not set (false) | — | D | `false {DEFAULT}` measured. Record `/sys/kernel/mm/transparent_hugepage/{enabled,shmem_enabled}` in `host_state`: a ZGC cell on `shmem_enabled=never` measures the kernel config, not the collector (A14). |
| NMT | off | — | D | Oracle's JDK 25 doc: 5–10% JVM performance drop. Never in a ledgered run. |
| `-Xlog:gc*` | not enabled | no EventPipe/ETW session | I | `/runtime-stats` carries collections/pause/heap per window and W17 flushes it per window. If ever used, write to a **file** — stdout is the frozen log stream. |
| JFR | not enabled | no EventPipe session | I | Assert no `-XX:StartFlightRecording` in `RuntimeMXBean.getInputArguments()` **and** `FlightRecorder.getFlightRecorder().getRecordings()` empty. See §4 for the one place this costs us. |
| `-XX:AOTMode` / `-XX:AOTCache` | unset (`auto`, no cache) | no ReadyToRun, no Native AOT | I | Symmetric by choice. **But this is now a deviation from Spring's own published advice** — see §1.1 [4-E]. `jdk.management.HotSpotAOTCacheMXBean` exists on 25 and is the structured read-back. |
| `jdk.virtualThreadScheduler.*` | not set | ThreadPool min/max unset | D | Defaults at 6 cores: `parallelism=6`, `maxPoolSize=256`. Read `parallelism` back from `VirtualThreadSchedulerMXBean` so the artifact proves it equals the cpuset width. Raising it to chase the knee is gaming (A16). |
| `-Xss` | unset (1024 KB) | — | D | `ThreadStackSize=1024 {DEFAULT}`. Reserved address space, not RSS: measured 218 threads → `reserved 223,244 KB / committed 21,644 KB`. Do not carry the "virtual threads save 200 MB" claim (A15). |
| `MALLOC_ARENA_MAX` | not set | — | D | A glibc knob with no .NET counterpart that directly lowers the reported RSS. Setting it is the shape of a gaming accusation. |
| `JAVA_TOOL_OPTIONS` / `JAVA_OPTS` | **must be empty** | — | D | Assert. `JDK_JAVA_OPTIONS` is the single carrier (registry.py already picks it). |
| `-XX:-EnableDynamicAgentLoading` | proposed on | **none exists** | **[4-D]** | Integrity guard, not a performance dial. See §1.1. |

### 1b. HTTP server surface

| dial | Java value | .NET counterpart | cat | evidence |
|---|---|---|---|---|
| `spring.threads.virtual.enabled` | **true** | Kestrel + `async`/`await` over the thread pool | I (in role) | D2. Measured today: on Boot 4.1.1 the property is the **only** trigger (`Threading.VIRTUAL.isActive == property && JavaVersion >= 21`), and for this service it has exactly **one** live effect — Tomcat's request executor. Full enumeration in §4. |
| `server.tomcat.threads.max` / `.min-spare` | **absent from the config file** | — | D | Metadata default 200 (read out of `spring-boot-tomcat-4.1.1.jar` today), and its own javadoc says "Doesn't have an effect if virtual threads are enabled". `AbstractEndpoint.getMaxThreads()` returns **-1** once an executor is attached. Writing `threads.max: 200` would be a **lie in the artifact**. Delete, do not set. |
| `server.tomcat.max-connections` | 8192 (default) | Kestrel `MaxConcurrentConnections` unset | D | Metadata default 8192, verified today. This — not `maxThreads` — is the real admission ceiling under virtual threads. Cannot bind at the p99=20 ms knee; disclose (A27). |
| `server.tomcat.accept-count` | 100 (default) | — | D | Metadata default 100, verified today. |
| `server.tomcat.max-keep-alive-requests` | **-1** | Kestrel has no per-connection request cap | I | Tomcat's default is **100** (metadata, verified today). At the default Java pays a connection re-establishment every 100 requests on a keep-alive generator — pure churn growing with rate. Assert by driving 200 requests on one connection and counting TCP connections (A26). |
| `server.tomcat.connection-timeout` / `keep-alive-timeout` | 5s / 120s | `RequestHeadersTimeout = 5s`, `KeepAliveTimeout = 120s` (`services/dotnet/Program.cs:27-28`) | I | Read from the .NET source today. |
| `server.port` | `${PORT:8080}` | `cfg.Port`, `PORT` default 8080 | I | `LedgerConfig.cs:61`. |
| `BODY_MAX_BYTES` enforcement | app-level `OncePerRequestFilter`, 65536 | Kestrel `Limits.MaxRequestBodySize`, **below** the app (`Program.cs:23`) | I (value) / **[4-C]** (layer) | Same value, same observable contract (413 + access line, **no** `request_error`). Tomcat has no container-level body cap for `application/json` (`maxPostSize` is form-only), so Java must enforce inside the app and must suppress the `request_error` it *could* log. Different layer; disclose. |
| `spring.main.banner-mode` | off | `ClearProviders()` + framework log filters | I | Log-contract parity, not performance. |
| `spring.mvc.problemdetails.enabled` | false (default), **not set** | `AddProblemDetails(ProblemDetailsConfig.Configure)` | D | Metadata default `False`, read out of `spring-boot-webmvc-4.1.1.jar` today (this was "likely" on 2026-08-21). Frozen envelopes are constructed explicitly on both sides. Enabling it adds a second envelope-producing path whose output nothing asserts. |

### 1c. Data layer

| dial | Java value | .NET counterpart | cat | evidence |
|---|---|---|---|---|
| `spring.jpa.hibernate.ddl-auto` | **`none`**, explicit + asserted | EF: no migrations, no `EnsureCreated` | D | Boot's own default for a non-embedded DB is `none` (`spring-boot-hibernate-4.1.1.jar` metadata, read today: *"Defaults to `create-drop` when using an embedded database … Otherwise, defaults to `none`"*). Pinned explicitly because the DB is shared and `create-drop` would be silent and catastrophic (critique E5). |
| `hibernate.jdbc.batch_size` / `order_inserts` / `order_updates` | **not set**, and their **absence asserted** | EF batches by default | D | Boot 4.1.1 writes none of them (measured: grep of every class in `spring-boot-hibernate-4.1.1.jar` finds zero `hibernate.jdbc.batch*` / `hibernate.order_*`). Setting `batch_size=50` would be a declared knob that **provably does nothing** for IDENTITY entities (§5, S1) — the over-declaration CLAUDE.md forbids. Note the name trap: the ordering keys are `hibernate.order_inserts`, **not** `hibernate.jdbc.order_inserts`; the wrong spelling is ignored silently. |
| Hibernate second-level cache | off; **asserted from the running `SessionFactory`**, not from a property | EF has no L2 cache | D | `RegionFactoryInitiator` returns `NoCachingRegionFactory.INSTANCE` with no provider; `use_second_level_cache` defaults true *only when a provider is named*; Boot writes no `hibernate.cache.*` key; no cache-provider jar on the resolved tree. Assert `sessionFactory.getSessionFactoryOptions().isSecondLevelCacheEnabled() == false` — strictly stronger than a property nobody can prove took effect. Protects the Redis hit-rate construction. |
| `hibernate.jdbc.use_get_generated_keys` | proposed **false** | — | **[4-B]** | Non-default. Shape fix for Q5. See §1.1 and §5. |
| `spring.jpa.open-in-view` | **false** | EF has no request-scoped session extension | **[4-B]** | Boot metadata default `True`, read today out of `spring-boot-jpa-4.1.1.jar`; Boot logs a startup WARN when left implicit. See §1.1. |
| `spring.jpa.properties.hibernate.session_factory.statement_inspector` | **test scope only**, absent from the measured image | EF `EfQueryShapeTests` capture | I | Correct property name verified in `JdbcSettings.java:368` (7.4 branch). Registered as a bean instance via `HibernatePropertiesCustomizer`. It is blind to BEGIN/COMMIT, to PgJDBC's appended `RETURNING *` and to the `?`→`$n` rewrite (measured 2026-08-21) — good for in-JVM assertions, **inadmissible** for a parity claim. |
| `spring.datasource.hikari.maximum-pool-size` / `minimum-idle` / `max-lifetime` | 24 / 24 / **3600000** | `DB_POOL_MAX` 24, `DB_POOL_MIN` = max, `DB_CONN_LIFETIME` 3600 (`LedgerConfig.cs:53-70`) | I | Single env surface (`spec/db-access.md`). **Unit trap: `max-lifetime` is milliseconds, `DB_CONN_LIFETIME` is seconds — ×1000.** |
| `spring.datasource.hikari.register-mbeans` | false (default) | — | D | Reach the pool MXBeans in-process via `HikariDataSource.getHikariPoolMXBean()`; no JMX registration needed. |
| `spring.datasource.connection-fetch` | `eager` (default) | — | D | Do **not** set `lazy` (new in 4.1): a Java-only knob with no EF counterpart that reduces measured connection contention. Textbook gaming shape. |
| PgJDBC `prepareThreshold` | **5**, the driver default, **not written anywhere** | Npgsql `Max Auto Prepare=20`, `Auto Prepare Min Usages=5`, explicitly set | I (behaviour) | D4. `PREPARE_THRESHOLD("prepareThreshold","5")` verified in `PGProperty.java` REL42.7.13; measured on PG 18.4 with `log_statement='all'` — executions 1-4 `execute <unnamed>`, the 5th `execute S_1`. Both stacks promote on the 5th execution. **Precedence trap:** PgJDBC gives JDBC-URL query parameters priority *over* the properties object, so set any driver knob in exactly one place and **read it back from a live connection** rather than echoing the compose env. |
| PgJDBC statement cache | 256 queries / 5 MiB (defaults) | Npgsql 20 (`Max Auto Prepare`) | D | `PREPARED_STATEMENT_CACHE_QUERIES("preparedStatementCacheQueries","256")`, `..._SIZE_MIB("preparedStatementCacheSizeMiB","5")` in REL42.7.13. The workload has under 10 distinct statement texts, so neither cache can evict — **assert the working-set size in a test** so the 256-vs-20 optic is a documented non-issue rather than a suspicion (A3). |

### 1d. Cache client

| dial | Java value | .NET counterpart | cat | evidence |
|---|---|---|---|---|
| `spring.data.redis.timeout` | **5s** | SE.Redis `SyncTimeout` 5000 ms, left at its default in `Program.cs` | I | Lettuce's default is **60 s** (`RedisURI.DEFAULT_TIMEOUT`) and Boot leaves the property null. A 12× gap changes tail behaviour past the knee for reasons unrelated to runtime cost. This is an equalization *toward the other side's default*, i.e. strictly **less** permissive for Java — say that, or "you set a timeout" reads as tuning (A22). |
| `spring.data.redis.lettuce.pool.*` | unset; `commons-pool2` **absent from the POM, asserted** | SE.Redis `ConnectionMultiplexer` | D | Default Lettuce is one shared multiplexed `StatefulRedisConnection` — the same model as the multiplexer. **Footgun:** Boot's gate is `enabled != null ? enabled : COMMONS_POOL2_AVAILABLE`, so mere classpath presence turns pooling on with no property set. |
| Lettuce retry / request queue | no command retry; unbounded request queue (`Integer.MAX_VALUE`); `DisconnectedBehavior.DEFAULT` | SE.Redis never retries commands | D | All three defaults, all left alone, all disclosed. A23/A24: this **removes** two round-1 asymmetries — and `spec/cache.md`'s Go-era client table is now wrong for round 2 (§7). |

### 1e. JSON

| dial | Java value | .NET counterpart | cat | evidence |
|---|---|---|---|---|
| `spring.jackson.property-naming-strategy` | **`SNAKE_CASE`** | `[JsonPropertyName]` on every member, `PropertyNamingPolicy` unspecified | I (wire) | Measured today over all **36** frozen contract names — every one reproduced exactly, including `gen0`/`gen1`/`gen2` (SNAKE_CASE does not split on digits) and `p99_micros`. Map **keys** are untouched, so `endpoint_calls` keys and the PascalCase `errors` keys survive. STJ source-gen has no global-naming lever, so the two mechanisms differ and the **wire** is identical. |
| Jackson accelerators | **none** — no Blackbird, no Afterburner | System.Text.Json source-generated context (`Json/LedgerJsonContext.cs`) | D (pre-registered disclosure) | Boot's auto-configured `JsonMapper` registers exactly two modules, both Spring's own (`JacksonMixinModule`, `JacksonComponentModule`) — measured from a live 4.1.1 context. **Enforced by the POM alone**: `find-and-add-modules` defaults true (verified in the artifact) and `jackson-module-blackbird` ships a `ServiceLoader` entry, so a one-line dependency add enables it with no code change. Assert `registeredModules()` rather than asserting prose. |
| `spring.jackson.default-property-inclusion` | **not set** (`USE_DEFAULTS`) | STJ writes nulls; `ProblemWriter.cs` omits `errors` conditionally | D | `/runtime-stats` **requires** present-but-null fields; a global `non_null` would silently delete them. `@JsonInclude(NON_NULL)` goes on the `Problem` record **only**. Measured both ways today. |
| `spring.jackson.use-jackson2-defaults` | false (default) | — | D | Metadata default `False`, verified today. Setting it true re-enables Jackson-2 semantics wholesale and is known to re-enable `FAIL_ON_UNKNOWN_PROPERTIES` (spring-boot#49951), breaking the frozen "unknown fields ignored" rule. |
| `WRITE_NUMBERS_AS_STRINGS`, `Shape.STRING` | **absent**, and P5 greps for them | STJ writes bare numbers | D | Default false, measured. When on it quotes **every** number including `long` — a contract violation `normalize.py` would surface as a type mismatch, not a JSON error. |
| scalar coercion | proposed `mapper.allow-coercion-of-scalars=false` + `deserialization.accept-float-as-int=false` | STJ throws `JsonException` on both | **[4-F]** | Measured on both sides today. See §1.1. |
| `Instant` serialization | one `@JacksonComponent ValueSerializer<Instant>` using `DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSSSSS'Z'").withZone(ZoneOffset.UTC)` | `Json/Rfc3339Micros.cs` | I | Measured through the real MVC pipeline today: `"created_at":"2026-06-06T00:00:00.000000Z"`. Truncates, does not round — same direction as `ffffff`. Jackson's default emits **variable** precision and breaks the golden vectors (critique E2, now closed). |

### 1f. Logging

| dial | Java value | .NET counterpart | cat | evidence |
|---|---|---|---|---|
| structured formatter | `StructuredLogFormatter<ILoggingEvent>` registered by FQCN via `logging.structured.format.console` | `Logging/LedgerConsoleFormatter.cs` | I | Boot's own extension point (its ECS formatter is the same interface). Never `ecs`/`gelf`/`logstash` — all nest or rename fields. |
| async sink | `queueSize=8192`, `discardingThreshold=0`, `neverBlock=false`, `maxFlushTime` finite-and-large | `ConsoleLoggerQueueFullMode.Wait`, `MaxQueueLength = LOG_QUEUE_LEN` (`Program.cs:39-40`) | I | Logback's defaults break two of the three frozen guarantees: `discardingThreshold` resolves to `queueSize/5` and `isDiscardable()` is true for `level <= INFO` — **every `http_access` line is INFO**, so at the default every access line is silently dropped once the queue is 80% full, under exactly the load being measured. Assert all five and echo queue length + block-on-full in `msg="config"`. |
| framework log suppression | Boot logs everything through the structured formatter; `banner-mode=off` | three explicit `AddFilter(..., Warning)` calls (`Program.cs:47,52,58`) | I | Same objective, different mechanism. `spec/logging.md`: contract lines only. |

### 1g. Build and image

| dial | Java value | .NET counterpart | cat | evidence |
|---|---|---|---|---|
| `<java.version>` | **25**, explicit | `net10.0` | I | `spring-boot-starter-parent:4.1.1` sets `<java.version>17</java.version>` feeding `maven.compiler.release` (verified in the parent POM today). Without the override you compile to bytecode 61 while running on JDK 25. **Assert the class-file major version in a test.** |
| build tool | Maven 3.9.16, Initializr `mvnw`, `distributionType=only-script`, no committed jar | `dotnet build` | I | D11. Verified today: no 3.9.16 successor exists (`maven-core/3.9.17` → 404), 4.0.0-rc-6 is not GA, and a project generated today from start.spring.io pins exactly `apache-maven-3.9.16` with `wrapperVersion=3.3.4`. |
| runtime image | `eclipse-temurin:25.0.4_7-jre-noble@sha256:…` | `aspnet:10.0-noble-chiseled` | I (OS generation) | Verified today in `docker-library/official-images`: `Tags: 25.0.4_7-jre-noble, 25-jre-noble` — host/container parity, which is new. `-noble` must stay **explicit**: the floating `25`/`25-jre` tags resolve to `-resolute` (Ubuntu 26.04). Two live asymmetries ride on this and stay disclosures: full CLDR/ICU vs `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=true` (A18) and full userland vs chiseled (A19). |
| AOT cache / CDS / Spring AOT | **none** in the measured image | no ReadyToRun, no Native AOT | I | Symmetric. But see §1.1 [4-E] — this is now a deviation from Spring's own published Dockerfile advice, not a silent omission. |
| diagnostics image | second build target (`--target diag`) whose final stage is the **JDK** image | — | D | Temurin **JRE images ship no `jcmd`** (adoptium/temurin-build#3336, wontfix), and §4's zero-recording diagnostics need it. Diagnostics must never be in the measured image. |
| **framework-managed dependency versions** | Boot 4.1.1 BOM pins, taken as-is | D8: "latest stable at run start" | **[4-A]** | The single largest fourth-category item. See §1.1. |
| HikariCP version | 7.0.2 (BOM) vs 7.1.0 | — | **V** if run as a labelled A/B; **[4-A]** if silently pinned either way | See §1.1 [4-A] and §4. |

---

## 1.1 THE FOURTH CATEGORY — six items that do not fit

P7's brief says "every dial is either a documented default, set identically on both sides,
or an explicitly labelled variant axis. There is no fourth category. **Find the fourth
category.**" Here it is. None of these six is resolvable by research; each needs a ruling.

### [4-A] Two different version-pinning policies across the two SUTs — **the big one**

**D8 gives the .NET side "latest stable at run start". The Java side has no counterpart
decision, and the practical default is "whatever the Spring Boot BOM pins."** Those are not
the same rule, and today they diverge on five artifacts at once:

| artifact | Boot 4.1.1 BOM pin | standalone latest (verified today) | gap |
|---|---|---|---|
| Hibernate ORM | 7.4.5.Final (2026-07-12) | **7.4.6.Final** (2026-08-23, today) | 1 patch, hours old |
| HikariCP | 7.0.2 (**2025-08-19**) | **7.1.0** (2026-06-14) | 1 minor, **one year old** |
| Lettuce | 7.5.2.RELEASE (2026-05-15) | **7.7.0.RELEASE** (2026-08-18) | 2 minors |
| Jackson (`tools.jackson`) | 3.1.5 (2026-07-08) | **3.2.2** (2026-08-14) | 1 minor |
| Tomcat embed | 11.0.24 (2026-07-03) | **11.0.25** (2026-08-12) | 1 patch |

All five verified today from `repo1.maven.org` metadata + `Last-Modified` headers, and the
BOM pins re-read out of `spring-boot-dependencies-4.1.1.pom` on this box.

This is a fourth category because it is neither "documented default" (the BOM pin is
Spring's default, but the *policy* is not the .NET side's policy), nor "set identically"
(the two sides use different selection rules), nor a labelled variant.

Two of the five have live consequences rather than optics:

- **HikariCP 7.0.2 → 7.1.0.** The 7.0.2 `ConcurrentBag` yield-spin is now **source-verified**
  rather than issue-numbered: `ConcurrentBag.java:191-201` in the 7.0.2 sources jar runs
  `Thread.yield()` in a loop **gated on `waiters.get() > 0`**, and 7.1.0's
  `ConcurrentBag.java:220-229 / :352-361` adds exactly a virtual-thread branch
  (`isCurrentThreadVirtual()` → `parkNanos(10µs)` instead of `yield()`). The gating is the
  new detail and it makes this worse, not better: the cost is **contention-conditional**, so
  it appears exactly at and beyond pool saturation — which is where the p99=20 ms knee is
  measured. Note it is *not* a pinning issue: `Thread.yield()` on a virtual thread unmounts
  cleanly.
- **Lettuce 7.5.2 → 7.7.0.** 7.5.2's release notes list tested servers to Redis **8.6**;
  `infra/compose.yaml` pins `redis:8.10.0-trixie`; 7.7.0 lists **8.10**. The workload uses
  only `GET`/`SET..EX`/`DEL`/`PING` + RESP3 `HELLO`, so practical risk is low either way —
  but "our client's tested matrix does not cover our server" is a free hit for a reviewer.

An override of Hibernate to 7.4.6 additionally **re-opens every measured SQL-shape finding
in `2026-08-21-measured.md`** (they were taken on 7.4.5.Final) and costs a re-run of the
spike. The `InsertCoordinatorStandard` batching predicate is unchanged on the 7.4 branch
head *and* on `main` (read today), so S1 at least survives — but S2/S4/S7 were not re-checked.

**Recommendation (needs ratification):** write a **symmetric, pre-registered version rule**
into FAIRNESS before the freeze — this is the java-stack doc's Q13 and it is now overdue —
of the shape *"both stacks pin the newest GA published at least N days before the freeze;
where a framework BOM manages a version, the BOM pin is taken unless it is more than one
minor behind the standalone latest, in which case the override is declared."* Under that
rule: keep Hibernate 7.4.5 and Tomcat 11.0.24 and Jackson 3.1.5; override HikariCP and
Lettuce, declared. Whatever is chosen, the manifest must carry a **pinned-vs-latest column
for every dependency on both sides**, or the artifact silently mixes two policies.

**Also under [4-A], and unresolved from 2026-08-21:** `services/dotnet/` pins
StackExchange.Redis **3.1.13** while **3.1.31** is current (both verified today on
`api.nuget.org`). D8 says latest stable at run start. That is a D8 violation sitting in the
tree right now.

### [4-B] Two non-default Hibernate settings whose direction helps Java

| setting | non-default value | direction | why it is not category D or I |
|---|---|---|---|
| `spring.jpa.open-in-view` | **false** (Boot default: `true`) | **helps Java** — less pool occupancy per request | .NET has no request-scoped session extension at all, so there is no value to "set identically". The counter-argument that must ride with it: OSIV *on* would mean the two stacks do different work per request. |
| `hibernate.jdbc.use_get_generated_keys` | **false** (default: true when the driver supports `getGeneratedKeys`, which PgJDBC does) | **helps Java** — Q5 returns 1 column instead of 7, × 3-8 items × 15% of the mix | Shape-parity setting, not a speed setting, but it is not a default and no team sets it. |

Both are the textbook accusation shape: *a non-default setting that helps the measured side,
justified by spec conformance.* The honest handling is to run each once at rehearsal with
and without, publish the delta, and declare them as **V** (labelled variant axes) rather
than quietly shipping them as if they were defaults.

**New this pass, and it matters for the second row:** `use_get_generated_keys=false` **cannot
restore insert batching**. `InsertCoordinatorStandard` reads only
`isIdentifierAssignedByInsert() || hasInsertGeneratedProperties()`, and
`isIdentifierAssignedByInsert()` is `identifierGenerator.generatedOnExecution()`
(`IdentifierProperty.java:43`) — the delegate choice is downstream and never consulted. So
S1 (round trips) and S2 (columns returned) are **independent knobs**, and the flag buys
only the shape. `[SOURCE REASONING, NOT MEASURED]` — settle it by re-running the 6-row
spike with the flag set.

### [4-C] `BODY_MAX_BYTES` is enforced at a different layer

Kestrel enforces `MaxRequestBodySize` **below** the app, which is why the frozen contract
says a 413 produces an access line and **no** `request_error`. Tomcat's `maxPostSize` covers
form posts only; there is no container-level body cap for `application/json`. Java must
enforce it in a filter — *inside* the app — and must be careful not to emit the
`request_error` it is perfectly positioned to emit. Same value, same observable contract,
different enforcement layer. Not category I in any strict reading. Disclose.

### [4-D] `-XX:-EnableDynamicAgentLoading`

An integrity guard with no .NET analog: a JVMTI agent reintroduces the virtual-thread
pinning JEP 491 otherwise eliminated (`Continuation::try_preempt` returns
`freeze_pinned_native` under `JvmtiUnmountBeginMark`). .NET has no equivalent attach
mechanism that silently changes scheduling, so there is nothing to set identically.

**Recommendation: drop the flag and assert an empty agent list instead.** An assertion is
not a dial and does not need a category. Measured today, this costs nothing on the
diagnostics side: `jcmd <pid> Thread.vthread_scheduler` and `Thread.vthread_pollers` both
still work against a JVM launched with `-XX:-EnableDynamicAgentLoading` (verified via
`jcmd VM.flags`), and JFR is not a JVMTI agent. The critique's item 7 already predicted a
reviewer would demand this concession; take it pre-emptively.

### [4-E] Excluding the AOT cache now contradicts Spring's own published advice

Spring Boot 4.1's container-images page (fetched today) **recommends** an AOT training run
for Java 25+ — `RUN java -XX:AOTCacheOutput=app.aot -Dspring.context.exit=onRefresh …` then
`ENTRYPOINT ["java","-XX:AOTCache=app.aot",…]` — and its example base image is
`bellsoft/liberica-openjre-debian:25-cds`, not Temurin.

Excluding it is still **correct**: it is Java's structural analog of ReadyToRun, and
`docs/FAIRNESS.md` pins .NET at "tiered JIT with dynamic PGO on, no ReadyToRun and no
Native AOT". But it is no longer an omission — it is a **declared, reasoned deviation from
the vendor's documented default**, and it will cost Java measurably on the cold-start curve,
which PROGRESS lists as a first-class result. Two lines in FAIRNESS, not one: the exclusion
and its reason, and the fact that the reference Dockerfile's base image is Liberica so
`eclipse-temurin` is a deliberate departure to match the host JDK vendor.

### [4-F] The scalar-coercion divergence: equalize or disclose, but not neither

Measured on both sides today. `POST {"qty":5.7,"money":"123"}`:

- **Java / Jackson 3 defaults:** `HTTP/1.1 200`, body `{"qty":5,"money":123}` — silently coerced.
- **.NET / System.Text.Json:** `JsonException` on both — `The JSON value could not be
  converted … Path: $.qty` — which `services/dotnet` turns into the frozen body-400.

Direction: **Java accepts input .NET refuses.** No vector in
`bench/validation/tests/test_equivalence.py` exercises it today. It is equalizable with two
Boot properties and no code — measured working: with
`spring.jackson.mapper.allow-coercion-of-scalars=false` and
`spring.jackson.deserialization.accept-float-as-int=false`, the same POST returns 400, a
valid body still binds 200, and all serialization output is byte-unchanged.

It is a fourth-category item because it is a **wire-contract equalization achieved by
non-default settings on one side only**. Under the governing rule it may not enter `spec/`
before a vector asserts it. **Add the vector and the two properties together at P4, or
disclose the divergence in FAIRNESS. Doing neither is the round-1 failure mode.**

### Residual: two boxed-vs-primitive binding divergences that no dial can close

Measured today. With **primitive** numeric record components, an absent field is a hard
Jackson binding failure (`FAIL_ON_NULL_FOR_PRIMITIVES` defaults true) → Java emits a
body-level 400 where .NET binds `0` and emits the frozen **field**-level message. With
**boxed** `Long`/`Integer`, absent and explicit-null both bind to null and Jakarta
validation emits the frozen per-field string.

**P4 constraint:** request DTOs use boxed wrappers with `@NotNull`; response DTOs, entities
and the pricing engine keep primitive `long` (P4 trap #4 unaffected). The residual: with
boxed types an **explicit** JSON null on a numeric field gives Java a field-level message
where .NET gives a body-400. That is the existing `docs/FAIRNESS.md` bullet "Explicit JSON
null on a numeric field", with Java landing on Go's side of it. Carry the bullet forward
with Java named.

---

## 2. Version pins

All rows fetched or measured **2026-08-23**.

### Runtime and container

| artifact | pin | published | source |
|---|---|---|---|
| Temurin JDK 25 (host) | **25.0.4+7** | 2026-08-04 announcement | `java -version` on this box; `rpm -q temurin-25-jdk` → `25.0.4.0.0.7-0` |
| Temurin `jdk-25.0.4.1+1` | **exists, is GA, and linux x64 glibc assets now exist** — but **do not adopt** | GH release 2026-08-19T14:49Z; linux x64 binaries 2026-08-21T11:40Z | `api.adoptium.net/v3/assets/latest/25/hotspot?...` → `jdk-25.0.4.1+1`, semver `25.0.4+101.0.LTS`, verified today |
| container | **`eclipse-temurin:25.0.4_7-jre-noble`**, digest-pinned at P4 | images rebuilt 2026-08-21/22 | `docker-library/official-images/library/eclipse-temurin`, fetched today: `Tags: 25.0.4_7-jre-noble, 25-jre-noble` |

**Why not bump to 25.0.4.1+1**, despite it being GA and now installable: no
`eclipse-temurin` container image for `25.0.4.1_1` exists (images are at `25.0.4_7`), so
bumping the host alone **reintroduces the host/container skew that just closed**; and
Adoptium publishes no news post and returns an **empty** release-notes body for it
(`api.adoptium.net/v3/info/release_notes/jdk-25.0.4.1%2B1`), so what it fixes is
undocumented at the vendor. Record that reasoning; re-check at the P4 Dockerfile freeze.

Second-order trap, from the 2026-08-21 critique and still live: `25.0.4+7-LTS` is a
**3-component** build string; `25.0.4.1+1` is 4-component. Any regex or manifest assertion
shaped on the current string breaks on a 4-component release.

### Spring Boot 4.1.1 BOM — re-resolved on this box today

`spring-boot-dependencies-4.1.1.pom`, read from `~/.m2` and re-fetched from Maven Central:

| artifact | BOM pin | standalone latest today | note |
|---|---|---|---|
| `spring-boot-starter-parent` | **4.1.1** | 4.1.1 (4.2.0-M1 is a milestone) | `api.spring.io/.../releases` → `"current": true`. **Maven Central's `<latest>` for `spring-boot-dependencies` is `4.2.0-M1` — do not read that field as the GA pointer.** |
| Spring Framework | 7.0.9 | 7.0.9 | no divergence |
| Spring Data BOM 2026.0.1 → jpa/redis/commons | 4.1.1 | 4.1.1 | — |
| Hibernate ORM | **7.4.5.Final** | **7.4.6.Final** (2026-08-23) | [4-A] |
| Jakarta Persistence | 3.2.0 | 3.2.0 | 4.0.0-M6 is a milestone, targeted by the unreleased Hibernate 8.0.0.Beta1 |
| HikariCP | **7.0.2** (2025-08-19) | **7.1.0** (2026-06-14) | [4-A]; Boot 4.2.0-M1 already moves to 7.1.0 |
| PgJDBC | **42.7.13** | 42.7.13 | no divergence. **Sources disagree:** `jdbc.postgresql.org/changelogs/` (index) still names 42.7.11 as most recent; Maven Central and the GitHub release say 42.7.13 (2026-07-06). The index is stale — cite Maven Central. |
| Lettuce | **7.5.2.RELEASE** | **7.7.0.RELEASE** (2026-08-18) | [4-A]. Ignore the GitHub releases page's rendered years (it printed "2024" for 7.7.0); Maven Central `Last-Modified` is authoritative. |
| Jackson (`tools.jackson`) | **3.1.5** | **3.2.2** (2026-08-14) | [4-A] |
| `com.fasterxml.jackson.core:jackson-annotations` | **2.21** | 2.21 | **Not `2.21.5`** — corrects `2026-08-21-critique.md` §A ("pin the patch"): Jackson 2.20 deliberately dropped the patch component from `jackson-annotations`, and `jackson-bom-2.21.5.pom:62` sets `<jackson.version.annotations>2.21</jackson.version.annotations>`. The manifest needs **two** Jackson rows, not one. |
| Tomcat embed | **11.0.24** (2026-07-03) | **11.0.25** (2026-08-12) | [4-A]; previously unverified |
| Hibernate Validator | 9.1.3.Final | — | measured working today for the frozen messages |
| Maven | **3.9.16** (2026-05-13) | 3.9.16; 4.0.0-rc-6 not GA | `maven-core/3.9.17` → **404**. **D11 holds.** |

**Support horizon.** 4.1.x OSS ends **2027-07-31**; 4.0.x ends **2026-12-31** — four months
after a run made today. 4.2.0 GA due 2026-11-30. `api.spring.io/projects/spring-boot/generations`,
fetched today. Independent of that argument, a 4.0.8 pin would silently move Hibernate to
**7.2.24.Final** and Lettuce to **6.8.2.RELEASE**, invalidating the entire measured SQL
spike. **4.1.1 is the only defensible pin.**

**start.spring.io's Java default is 17, not 25**, and the parent POM sets
`<java.version>17</java.version>`. Verified today. See §1g.

---

## 3. Layout

### The repositories that justified it

Read today; stars and last-push verified via the GitHub API. Tutorials
(`eugenp/tutorials`, `in28minutes`) and stale repos (`thombergs/buckpal`, last push
2024-06; `piomin/sample-spring-microservices`, **archived** 2024-02) were deliberately not
read.

| repo | stars | last push | parent |
|---|---|---|---|
| `spring-projects/spring-petclinic` | 9,472 | 2026-07-30 | `spring-boot-starter-parent` 4.1.0 |
| `spring-petclinic/spring-petclinic-rest` | 601 | 2026-08-01 | 4.1.0; already on Jackson 3 (`tools.jackson.databind.ObjectMapper`) |
| `codecentric/spring-boot-admin` | 12,849 | **2026-08-23 (today)** | — |

**Honest caveat, and it must travel with the citation:** petclinic is Thymeleaf-MVC, not
REST; petclinic-rest is a 2016-vintage codebase kept current; spring-boot-admin is an ops
tool. **No genuinely popular, recently-maintained greenfield Boot 4 REST API exists at
meaningful star count in August 2026.** GitHub's code-search API requires authentication
(401 without a token) and `gh` is not installed on this box, so a dependency-level search
for `spring-boot-starter-webmvc` was not possible. The layout recommendation rests on those
three repos plus the Boot 4.1.1 reference docs, and the write-up should say so rather than
implying a wider survey.

### Decisions

**Package by technical concern**, mirroring `services/dotnet/` — `config/ data/ dto/ web/
infrastructure/ json/ logging/ money/ pricing/ problem/ sql/ observability/`. This is
defensible as idiomatic, not merely convenient: **2 of the 3 repos do exactly this**
(petclinic-rest: `config/ mapper/ model/ repository/ rest/{advice,controller,validation}
service/ security/ util/`; spring-boot-admin: `server/{config,domain,eventstore,notify,
services,ui,web,utils}`). Only the Spring team's own petclinic is package-by-feature, and
it is not a REST service. Package structure has zero runtime cost, so it cannot influence a
measured number; state the mirroring reason in `services/java/NOTES-equivalence.md`.

Two cheap additions that make it read as Spring rather than as a transliteration, both
lifted from petclinic and both free on parity because .NET has no analog: a
`package-info.java` per package, and **package-private** controllers and repositories
(petclinic writes `class OwnerController`, not `public class`).

**No service layer for the read paths.** `spring-petclinic` has none at all — controllers
inject the Spring Data repository directly (`class OwnerController { private final
OwnerRepository owners; … this.owners.findById(ownerId) }`, read in full today).
petclinic-rest *does* have one: a single service behind an interface
(`ClinicService`/`ClinicServiceImpl`), a shape that follows from its three
swappable repository implementations (`jdbc/`, `jpa/`, `springdatajpa/`). Go
`@RestController` → Spring Data repository for reads, and put a `@Transactional @Service`
only where there is real orchestration — the create path (pricing + header insert + N item
inserts + balance update in one transaction).

**And therefore no `IInvoiceRepository` analog.** `services/dotnet/Data/IInvoiceRepository.cs`
exists because .NET carries `efcore`/`ado`/`dapper` slots for P2. **D5 gives the Java side
one slot.** Copying the interface would add an abstraction with one
implementation, against CLAUDE.md's "one implementation means no interface".

**`@ConfigurationProperties` records** with `@Validated` + `@ConfigurationPropertiesScan`.
Downgrade the claim honestly: this is what the **docs** recommend ("Constructor binding can
be used with records. Unless your record has multiple constructors, there is no need to use
`@ConstructorBinding`"), **not** what the popular repos do — spring-boot-admin uses a
mutable Lombok `@Data` class and petclinic uses no `@ConfigurationProperties` at all. It is
still the right call here for a second reason the repos do not face: it mirrors the
immutability of `services/dotnet/Config/LedgerConfig.cs`.

One setting taken from petclinic's `application.properties`: `spring.jpa.open-in-view=false`
(see [4-B]).

### The error envelope — three measured findings that would each have cost a P4 day

All three measured today on Boot 4.1.1 / Temurin 25.0.4+7 / hibernate-validator 9.1.3.Final,
via MockMvc and a live server.

1. **The frozen PascalCase `errors` keys are reachable with a 6-line global transform.**
   Overriding `protected String determineField(ConstraintViolation<Object>)` in a
   `LocalValidatorFactoryBean` subclass produces `CustomerId`, `Currency`, `Status`,
   `Items`, `Items[0].Sku`, `Items[0].Qty`, `Items[0].Description`, `Items[0].UnitPriceMinor`
   — byte-for-byte the set `spec/openapi.yaml` freezes. Without it: `customerId`,
   `items[0].sku`, … Jakarta paths use the **Java** component name and ignore
   `@JsonProperty` entirely, so the transform is purely `capitalize(first char of each
   dot-segment)`. Hook source: `SpringValidatorAdapter.java:206-232` (v7.0.9), `@since 4.2`.
   **`MessageInterpolator` is the wrong hook** — it maps message *template* to message
   *string* and never sees the key, which is built from `getPropertyPath()`.
   Put the transform in its own class (`io.ledgerline.problem.FieldPath`) so it is
   unit-testable without booting a context. When replacing Boot's `defaultValidator`
   (`@ConditionalOnMissingBean(Validator.class)`, so declaring the bean is enough), carry
   over the two things the auto-config does: `setMessageInterpolator(new
   MessageInterpolatorFactory(ctx).getObject())` and the `ValidationConfigurationCustomizer`
   beans. Drop them and you silently change interpolation behaviour.

2. **Spring MVC back-fills `ProblemDetail.instance` from the request URI**, so a stock
   `@RestControllerAdvice` emits an extra `"instance":"/invoices"` that
   `services/dotnet/Problems/ProblemDetailsConfig.cs` explicitly nulls (`pd.Instance =
   null`). It happens on **both** return paths (`RequestResponseBodyMethodProcessor:203-210`
   and `HttpEntityMethodProcessor:209-213`), each guarded only by `if
   (detail.getInstance() == null)` — so you cannot suppress it by setting null in the
   handler. The fix that works is `ErrorResponse.Interceptor`, which runs *after* the
   back-fill. **It must be registered via `WebMvcConfigurer.addErrorResponseInterceptors(List)`
   — a plain `@Bean ErrorResponse.Interceptor` is silently ignored** (measured: the key was
   still emitted). `normalize.py` R1 sorts keys, so *order* is free, but an extra key is a
   divergence that fails every 4xx assertion at G4. Build it in from the start.
   One comment worth writing: `AbstractMessageConverterMethodProcessor` wraps the
   interceptor loop in `catch (Throwable ex) { /* ignore */ }`, so an interceptor that
   throws is swallowed and the envelope silently reverts — the R1-B1 failure shape exactly.
   **Assert the emitted body, never the bean's existence.**

3. **You cannot both extend `ResponseEntityExceptionHandler` and declare your own
   `@ExceptionHandler` for an exception it covers** — the context fails at startup with
   `IllegalStateException: Ambiguous @ExceptionHandler method mapped for
   [MethodArgumentNotValidException]`, because `handleException` is `public final`. Override
   the protected hooks: `handleMethodArgumentNotValid`, `handleHttpMessageNotReadable`,
   `handleHttpMediaTypeNotSupported`. **Without those overrides the 400-on-unreadable-body
   and the 415 return with an EMPTY body** (measured) — Boot 4 produces no `ProblemDetail`
   for them unless `spring.mvc.problemdetails.enabled=true`, so the frozen 415 envelope
   would simply never have been emitted.

Bonus, and it removes a plausible-looking P4 bug: the frozen messages containing regex
quantifiers — `must match ^[A-Z]{2}-[0-9]{4}$` — pass through Hibernate Validator
interpolation **verbatim** when written into `@Pattern(message = …)`. No brace doubling.
This is the **opposite** of `services/dotnet/Dtos/CreateInvoiceRequest.cs`, which must
write `{{2}}`/`{{4}}` because DataAnnotations runs `string.Format` over `ErrorMessage`.
Worth one comment in the Java DTO citing the frozen key, because the asymmetry with the C#
file next to it will otherwise read as an oversight. (Watch: `requiresMessageFormat()`
applies `MessageFormat` only when the message contains a Spring-style `{0}`; none of the
twelve frozen messages do.)

---

## 4. Virtual-thread posture, and what it costs to evidence

**D2 is confirmed as current recommended posture.** Boot 4.1.1's reference doc: *"Virtual
threads require Java 21 or later. For the best experience, Java 24 or later is strongly
recommended."* JDK 25 (D1) satisfies that, because JDK 24 is where JEP 491 landed. Boot
still defaults to **platform** threads: `Threading.VIRTUAL.isActive ==
environment.getProperty("spring.threads.virtual.enabled", boolean, false) && JavaVersion >=
21` (read from the 4.1.1 sources jar today). The property must be set explicitly and echoed
into the manifest knobs; it cannot be asserted from the JDK version.

### What the flag actually switches — the complete list

Derived today by scanning **every Boot 4.1.1 jar on this project's resolved classpath** for
`@ConditionalOnThreading` (hits: `spring-boot-autoconfigure`, `-micrometer-metrics`,
`-data-redis`, `-tomcat`):

1. **Tomcat** — `protocolHandler.setExecutor(new VirtualThreadExecutor("tomcat-handler-"))`.
2. `applicationTaskExecutor` → `SimpleAsyncTaskExecutor` with `virtualThreads(true)` (backs MVC async and `@Async`).
3. default `taskScheduler` → `SimpleAsyncTaskScheduler` on virtual threads.
4. `SimpleAsyncTaskExecutorBuilder` / `SimpleAsyncTaskSchedulerBuilder` get `virtualThreads(true)`.
5. Lettuce — `LettuceConnectionFactory.setExecutor(...)`. **No-op here**: that executor is passed only to `createClusterCommandExecutor(...)`, i.e. Redis CLUSTER mode only.
6. `RedisMessageListenerContainer` gets a `VirtualThreadTaskExecutor`.
7. The OTLP meter registry gets a virtual thread factory.

Nothing in JDBC, JPA, Hikari or transaction management is switched. **For a service with no
`@Async`, no `@Scheduled`, no Redis listener and standalone Redis, the flag has exactly ONE
live effect: Tomcat's request executor.** That is a clean one-mechanism FAIRNESS row rather
than a diffuse "virtual threads everywhere" claim, and it makes the .NET-side analog
precisely *"requests are not bound to a pooled OS thread"*.

### JEP 491: confirmed by measurement, and the residual is not zero

**PROGRESS P3's bullet closes YES.** Measured on this box with `parallelism=1` and
`maxPoolSize=1` (so exactly one carrier exists): a virtual thread that slept 800 ms inside a
`synchronized` block released its carrier — the second virtual thread ran at 102 ms. JEP 491
is Status Closed/Delivered, Release 24. **No `ReentrantLock` workaround, no parallelism
inflation, no driver substitution is justified on pinning grounds.** HikariCP's remaining
monitors are per-connection and uncontended by construction (one thread owns a checked-out
connection) or lifecycle-only; PgJDBC 42.7.13 has no `native` method on the query path.
`[likely — source reading of both jars, not a load measurement]`

**But pinning is not fully retired.** JEP 491's own Future Work names class loading, class
initializers, and waiting for another thread's class init. Measured here on 25.0.4: a
virtual thread blocking in `<clinit>` **held the only carrier for the full 800 ms**, and the
scheduler did **not** compensate — `poolSize` stayed 1. JDK 25's core docs add native
methods and foreign functions. For the JDBC path this does not bite; the residual risk is
**warmup-shaped** (class load/init), which warmup covers by construction — except for a
class that first initializes *inside* a measured window (a rarely-taken error path, a lazily
loaded serializer). That is a real p999 mechanism.

**`-Djdk.tracePinnedThreads` no longer exists** — JEP 491 removed it in JDK 24, and it
produced no output here even in the one case that genuinely pins. Do not put it in the
Dockerfile or RUNBOOK; advice to use it does not apply to JDK 24 and later.

### What it costs to evidence

The runtime pinning detector on 25 is the **`jdk.VirtualThreadPinned` JFR event**, and the
20 ms threshold comes from the **recording configuration**, not the event type. Measured:
the event type's own defaults are `enabled=false, threshold=0 ns`; `$JAVA_HOME/lib/jfr/default.jfc`
overrides to `enabled=true, stackTrace=true, threshold=20 ms`. End-to-end recipe verified —
a `settings=default` recording captured the clinit pin with `pinnedReason = "VM call to
VtProbe$SlowInit.<clinit> on stack"`, duration 800 ms, carrier `ForkJoinPool-1-worker-1`,
full stack trace.

**It requires an active recording, which the measured window forbids.** So the cost is:

- one **rehearsal-scale diagnostic probe per slot**, run with
  `-XX:StartFlightRecording:settings=default`, over the full Ledgerline mix, asserting
  **zero `jdk.VirtualThreadPinned` events**; commit the assertion as the artifact;
- the measured cells run with **no** recording;
- **do not declare a pinning gate in `spec/`** that a measured run cannot evaluate. That is
  the governing rule verbatim.

Cheaper, zero-recording diagnostics exist and were not previously named:
`jcmd <pid> Thread.vthread_scheduler` prints the carrier ForkJoinPool state (parallelism,
size, active, running, steals, tasks, submissions, delayed) and `Thread.vthread_pollers`
prints the epoll topology. Both are one-shot attach commands, both verified working with
`-XX:-EnableDynamicAgentLoading` set. Neither detects pinning. Both need `jcmd`, which the
JRE image does not ship — hence the `--target diag` build (§1g).

### `VirtualThreadSchedulerMXBean` — correction and a typing constraint

ObjectName `jdk.management:type=VirtualThreadScheduler`, `@since 24`. Exact signatures,
verified against the JDK 25 javadoc and read off a live bean today:
`int getParallelism()`, `int getPoolSize()`, `int getMountedVirtualThreadCount()`,
**`long getQueuedVirtualThreadCount()`** — *long, not int; this corrects
`2026-08-21-measured.md`'s table* — plus `void setParallelism(int)`.

The javadoc says poolSize/mounted/queued return **`-1` if not known**, and that mounted and
queued are **estimates** that "may overestimate". **`spec/runtime-stats.md` must type
`queued` as a 64-bit integer and must permit `-1` as a value distinct from 0.** A `-1`
coerced to a count is exactly the not-measured-wearing-a-measured-label defect W16 fixed
elsewhere. Publish them as estimates, in the javadoc's own words.

### What each number actually means (measured, and it settles `goroutines_or_threadpool`'s options)

| observation | reading |
|---|---|
| 2000 **sleeping** virtual threads | `mounted=0`, `poolSize=32`, `queued=0`, `ThreadMXBean.getThreadCount()=40` |
| 64 **CPU-burning** virtual threads | `mounted=32` (== parallelism), `queued=32` |
| 256 virtual threads doing **file I/O** at `parallelism=6` | `mounted=6`, `queued` spiking to **174**, `getThreadCount()=13` |

`getMountedVirtualThreadCount()` is a **utilization** gauge bounded by parallelism, not a
concurrency gauge. `ThreadMXBean.getThreadCount()` tracks carriers, not load. **The only
scheduler number that moves with offered load is `getQueuedVirtualThreadCount()`** — it is
the Java-side analog of thread-pool queue depth, it costs one ForkJoinPool field read, and
without it the artifacts show a p99 knee with no explanation on the Java side. Sample it in
the per-window runtime-stats poll and use it (with HikariCP's `getThreadsAwaitingConnection()`)
as the saturation signal near the knee. The frozen-field mapping itself is still open (§7).

### Three tail mechanisms, none of them a bug to fix

1. **No time-slicing** — JEP 444: *"The scheduler does not currently implement time sharing
   for virtual threads"*; the JDK 25 `Thread` javadoc: virtual threads *"are not intended
   for long running CPU intensive operations"*. With carriers == cpuset width (6), a
   CPU-heavy handler occupies a carrier to completion (A16).
2. **No compensation for pinning** — JEP 444: *"The scheduler does not compensate for
   pinning by expanding its parallelism"*, confirmed by the clinit measurement above. A
   pinned carrier is a permanent −1 on capacity for its duration.
3. **No admission control.** `AbstractEndpoint.setExecutor()` sets `internalExecutor =
   (executor == null)`; thereafter `getMaxThreads()` returns **-1**, and Tomcat's
   `StandardVirtualThreadExecutor` supports exactly one attribute, `namePrefix` — no cap, no
   queue (`tomcat.apache.org/tomcat-11.0-doc/config/executor.html`). Past the knee, offered
   load becomes unbounded FJP queue growth rather than a refusal, and that growth is
   invisible to `ThreadMXBean` and to Tomcat's own metrics.

**Container choice is load-bearing here and Tomcat is the right one.** Boot's
`JettyVirtualThreadsWebServerFactoryCustomizer` builds `new VirtualThreadPool(maxTasks)`
from `server.jetty.threads.max` (default 200), and Jetty's `VirtualThreadPool` enforces it
with a `Semaphore` acquired inside each task — so the same `200` that is **inert** on Tomcat
becomes a **hard concurrency cap with queueing** on Jetty. Stay on the Boot default
(Tomcat), and record in FAIRNESS that the Java side runs with **no admission control** while
a Jetty build of the same application would cap at 200: a reviewer who reproduces on Jetty
gets a different tail and needs to know why.

**File I/O does not pin.** 256 virtual threads × 40 × 32 KiB writes to NVMe-backed ext4 at
`parallelism=6` produced **zero** `jdk.VirtualThreadPinned` events under a default JFR
recording, and `poolSize` stayed at exactly 6 (no compensation observed). *Sources disagree:*
an InfoQ article of 2026-07-31 lists "file I/O on Linux systems" among residual pinning
sources; JEP 444 classes filesystem blocking as a **compensated carrier capture**, not
pinning, and the JDK 25 core docs list only native methods and foreign functions. **The
measurement above and the JDK documentation disagree with that article for JDK 25**, and
this note follows them.
The consequence for `POST /invoices/{id}/pdf-stub` (8% of the mix) is *carrier occupancy,
not pinning*: with 6 carriers and no time-slicing, a write of t ms removes 1/6 of
request-processing capacity for t ms. Keep the 32 KiB write identical to the .NET side (no
fsync, same buffer) and state in FAIRNESS that the Java write occupies a **scheduler
carrier** while the .NET write occupies a **thread-pool thread the pool can replace**.

**The micrometer JFR trap is still live in 4.1.1** and is guarded by **two** conditions, not
one: `JvmMetricsAutoConfiguration$VirtualThreadMetricsConfiguration` is
`@ConditionalOnClass("io.micrometer.java21...VirtualThreadMetrics")` inside an
auto-configuration that is `@ConditionalOnClass(MeterRegistry.class)
@ConditionalOnBean(MeterRegistry.class)`. Both must hold — so a service with no actuator and
no `MeterRegistry` cannot start a `RecordingStream` by accident, but adding
`micrometer-java21` *plus any registry* would silently start JFR inside the measured window.
Phrase the classpath assertion as **"no `MeterRegistry` bean AND no `VirtualThreadMetrics`
class"** so it fails loudly if either arrives, in the same test as the no-JFR-recording
assertion.

---

## 5. Hibernate statement-shape control, against the frozen Q1..Q7

Baseline: entities model FKs as scalar `Long`, no `@ManyToOne`/`@OneToMany` — which mirrors
`LedgerDbContext` (`HasOne<Customer>().WithMany().HasForeignKey(...)`, no navigation on
either side). All shapes below were measured on **Hibernate 7.4.5.Final** against real
PostgreSQL 18.4 with `log_statement='all'` (2026-08-21). 7.4.6 landed today and the batching
predicate is unchanged on both the 7.4 branch head and `main`; the other shapes were **not**
re-checked on 7.4.6.

| Q | frozen text (`spec/db-access.sql`) | can Java match? | what Java emits | disposition |
|---|---|---|---|---|
| **Q1** | `SELECT id, customer_id, status, currency, total_minor, created_at FROM invoices WHERE id = $1` | **shape yes, text no** | `select i1_0.id,i1_0.created_at,i1_0.currency,i1_0.customer_id,i1_0.status,i1_0.total_minor from invoices i1_0 where i1_0.id=$1` — aliased, id-then-alphabetical | Shape-equivalent, and **closer to spec than EF**, which appends a trailing `LIMIT 1`. Column *order* is recoverable with an HQL constructor-expression DTO query (`select new InvoiceHeader(i.id, i.customerId, …)`), which is the direct analog of EF's `.AsNoTracking().Select(...)` and also keeps the persistence context empty. Aliases are not recoverable without native SQL. **Do not go native.** |
| **Q2** | `… FROM invoice_items WHERE invoice_id = $1 ORDER BY id` | shape yes, text no | `select ii1_0.… from invoice_items ii1_0 where ii1_0.invoice_id=$1 order by ii1_0.id` | Same treatment as Q1. Q1+Q2 are issued as **two sequential statements, same as EF Core** — one fewer asymmetry than round 1 carried. |
| **Q3** | `… ORDER BY id DESC LIMIT 20 OFFSET $2` | **no** | `… order by i1_0.id desc offset $2 rows fetch first $3 rows only` — a **third** distinct text (spec/sqlc/ado use `LIMIT 20 OFFSET $2`, EF uses `LIMIT $2 OFFSET $3`) | **The third pinned deviation.** There is no property and no query hint: `PostgreSQLDialect.getLimitHandler()` returns `OffsetFetchLimitHandler.INSTANCE` as a hard-coded constant, and the whole `QuerySettings` surface contains nothing pagination-related except `fail_on_pagination_over_collection_fetch`. The one supported non-native hook — a `Dialect` subclass returning `LimitOffsetLimitHandler` — yields ` limit ? offset ?`, i.e. **EF's text order, still not the spec literal**; it is a contortion written to make one side look like the comparator, and it is **rejected**. Plan-identical (backward index range scan on `idx_invoices_customer_id_id_desc`, no sort node) — assert with `EXPLAIN`. |
| **Q3b / Q7** | `SELECT EXISTS(SELECT 1 FROM customers WHERE id = $1)` | **only via native** | `existsById` → `select count(*) from customers c1_0 where c1_0.id=$1`; JPQL `count(c) > 0` → `select count(c1_0.id)>0 …` | **Use a one-line native `@Query`.** `ParameterRecognizerImpl` copies native SQL character-for-character (`?1`→`?`) and PgJDBC rewrites `?`→`$1`, so the wire text is byte-identical. `[claim needs the P5 capture to confirm — it is source reasoning today]` Declared `@Query` methods are **not transactional by default**, so no `BEGIN`/`COMMIT`, matching EF's `AnyAsync`. **Why native here but not for Q3:** a one-line existence probe is ordinary production Java and reaches exact parity; replacing the paged list query — the one place Spring Data's derived-query machinery is doing real ORM work — with hand SQL would delete the thing being measured and rebuild round 1's ORM-vs-hand-SQL asymmetry. |
| **Q4** | `INSERT INTO invoices (customer_id, status, currency, total_minor) VALUES … RETURNING id, created_at` | **semantics yes, column order no** | `insert into invoices (currency,customer_id,status,total_minor) values ($1..$4) returning id,created_at` | Exact semantics: Hibernate 7 uses `RETURNING` on PG for IDENTITY, and `@Generated(event = INSERT)` brings `created_at` back with **no second SELECT**. **`@Column(updatable = false)` is load-bearing** — `@Generated(event=INSERT)` alone leaves the column writable, so any later dirty update overwrites the DB timestamp. **Also: IDENTITY forces an immediate insert at `persist()`** (`ActionQueue.addResolvedEntityInsertAction`), so `totalMinor` must be computed **before** `persist(invoice)` or a full-column `UPDATE` follows at flush. Invisible in code review; must be a test. |
| **Q5** | `INSERT INTO invoice_items (…) VALUES ($1..$6) RETURNING id`, **N in ONE batched round trip** | **no, on two counts** | (a) **N separate prepares/executes** — measured 6 assigned-id rows → 1 statement, 6 IDENTITY rows → 6; (b) PgJDBC's `getGeneratedKeys` path appends a literal newline + **`RETURNING *`**, so all 7 columns come back per row | **(a) Accept and disclose.** No fix exists inside JPA: `InsertCoordinatorStandard` sets `batchKey = null` whenever `isIdentifierAssignedByInsert()`, and that flag is `identifierGenerator.generatedOnExecution()` — a property of the generator. **Unchanged in 7.4.6 and on the 8.0 dev branch** (both read today), so there is no upstream fix to wait for. `spec/schema.sql` pins `GENERATED ALWAYS AS IDENTITY`. `spec/db-access.md`'s "round-trip counts ASSERTED EQUAL" **cannot hold** and must become per-stack counts. Dropping to `JdbcTemplate.batchUpdate` is rejected — it rebuilds round 1's ORM-vs-hand-written asymmetry. **(b)** fixable with `use_get_generated_keys=false` → `InsertReturningDelegate` → `… returning id`. Non-default: **[4-B]**. The two are **independent**; (b) does not restore (a). |
| **Q6** | `UPDATE customers SET balance_minor = balance_minor + $2 WHERE id = $1` | **yes, byte-identical** | native `@Modifying @Query` → identical; JPQL bulk → `update customers c1_0 set balance_minor=(c1_0.balance_minor+$1) where c1_0.id=$2` | Use native. Both forms are true atomic increments (no read-modify-write), so both satisfy the Zipfian-contention requirement — but only native matches the frozen text. Leave `flushAutomatically`/`clearAutomatically` at their `false` defaults; `true` injects a flush and changes the ledger. |

### Transaction envelope — the asymmetry that is not in any Q row

`@Transactional(readOnly = true)` **does** reach the wire: Spring's
`HibernateJpaVendorAdapter` holds the connection, `HibernateJpaDialect` calls
`Connection.setReadOnly(true)`, and PgJDBC's default `readOnlyMode=transaction` turns that
into **`BEGIN READ ONLY`**, ended with **`ROLLBACK`**, not `COMMIT`. If EF opens no explicit
transaction for a single read, Java pays **two extra round trips per cache-miss read on 30%
of the mix**. **Disposition: do not annotate the read endpoints**; leave declared `@Query`
methods untransacted. With DTO projections there are no managed entities, so `FlushMode.MANUAL`
and `setDefaultReadOnly` buy nothing. Quantify at P5 either way.

### Fetch strategy: the question is moot by construction, and that is worth saying

Hibernate's own guide ranks outer join fetch first and advises **against** the
`FetchProfile` API (*"Hibernate offers alternatives that we think are more compelling most
of the time"*) and against `@NamedEntityGraph` (*"the annotation-based API is so verbose
that it's just not worth using"*). Spring Data JPA 4.1 supports `@EntityGraph` in named and
ad-hoc `attributePaths` forms and does not mention fetch profiles at all.

**But `services/java/` should contain no `@EntityGraph`, no join fetch, no `@BatchSize` and
no fetch profile.** `spec/db-access.md` pins the cache-miss read as **two** statements (Q1
then Q2), and the .NET model it mirrors has no invoice→customer navigation and reads both
sides through projected no-tracking queries. Loading invoice+items in one statement would be
a **spec deviation, not an optimization**. FAIRNESS gets a "fetch strategy: not applicable"
row with that reasoning — a reader will expect N+1 machinery to be where Hibernate's cost
lives, and it needs saying that it contributes nothing here.

**Two things a reviewer will raise, and the honest answers:**

- The scalar-`Long`-FK model is a style Hibernate's own authors argue against at length
  (*"one context where it doesn't usually make sense is when accessing a relational database
  via Hibernate … To put it mildly: this is not how JPA was ever intended to be used"*).
  It is the right choice for **parity** (it is the closest mirror of the EF model, which
  genuinely has no customer navigation), and it also means the Java service avoids
  Hibernate's association, proxy and collection machinery entirely — i.e. **this benchmark
  measures Hibernate on its cheapest configuration**. Direction: *helps Java*. Both halves
  go in FAIRNESS.
- `hibernate.query.fail_on_pagination_over_collection_fetch` defaults **false** in 7.4
  (`QuerySettings.java:210-232`, `"no exception is thrown, and the possibility of terrible
  performance is left as a problem for the client to avoid"`), contradicting widely repeated
  lore that it throws. It cannot bite here (no association exists) but it is the failure
  mode that would silently destroy the Q3 index-scan plan if someone "fixed" an N+1 later.

### Open modelling question for P4

Should `InvoiceItem` carry a scalar `Long invoiceId` (what the spike measured) or should
`Invoice` carry `@OneToMany(cascade = PERSIST)` (what EF's model actually has —
`HasMany(x => x.Items).WithOne()`, the one collection navigation, used solely to build the
insert graph)? Statement-shape-wise both give the same N unbatched IDENTITY inserts. The
collection form is the truer structural mirror; the scalar form is safer (a collection can
be accidentally touched on a read path). **Decide by capturing both against
`log_statement='all'`, not by argument.**

### How the P5 audit must be run

- **Use PostgreSQL-side `log_statement='all'` in a dedicated non-measured pass.** Not
  `pg_stat_statements` (needs `shared_preload_libraries` + a server restart, is not
  configured here at all, and folds prepared and unprepared executions of the same text into
  one entry) and not `org.hibernate.SQL` (writes into the frozen log stream). `log_statement`
  is superuser-settable at session/role/database level with no restart.
- **No capture mechanism may be enabled during a measured probe**, and the reason differs
  per mechanism: `org.hibernate.SQL` at DEBUG writes into the SUT's stdout, which the
  containerd shim relays and W16 charges to `containerd.service`; `log_statement='all'`
  writes inside the PostgreSQL container and lands **directly in the PG cgroup `cpu.stat`
  delta**, which is published as `db_cpu_ms_per_req` — a first-class result.
- Concretely: the audit is **its own low-rate pass with its own run id**, `analyze` must
  refuse to publish a DB-CPU column from any window where `log_statement` was on, and there
  should be a plan-time assertion reading `SHOW log_statement` at probe start that fails the
  run if it is not `none` — same shape as the host-state read-back (R1-B2).

---

## 6. JSON posture, and the FAIRNESS row it implies

Everything in this section was measured today against a live Boot 4.1.1 MVC stack on
Temurin 25.0.4, with the .NET side measured in parallel on `net10.0`.

**The complete configuration that hits the frozen contract:**

```yaml
spring.jackson.property-naming-strategy: SNAKE_CASE
# only if [4-F] is equalized rather than disclosed:
spring.jackson.mapper.allow-coercion-of-scalars: false
spring.jackson.deserialization.accept-float-as-int: false
```

plus **one** `@JacksonComponent ValueSerializer<Instant>` and **one**
`@JsonInclude(NON_NULL)` on the `Problem` record. Everything else — modules, inclusion,
unknown-field handling, number writing, key order, recycler pool, `use-jackson2-defaults` —
is the shipped default and is written down nowhere. *That is the point: "ship the default"
is a real posture here, not a slogan.*

Six defaults that land on the right side of the contract for free, all measured:

- `FAIL_ON_UNKNOWN_PROPERTIES` is **false by default in Jackson 3** (it was true in Jackson 2
  and Boot 3 had to disable it) — matching `spec/openapi.yaml`'s "unknown fields IGNORED
  both sides". **Do not add `fail-on-unknown-properties: false`**: redundant in Boot 4 and it
  would read like a tuning decision in the file the fairness reviewer audits.
- `FAIL_ON_TRAILING_TOKENS` true; STJ also rejects trailing content — symmetric.
- Duplicate keys last-wins on both.
- `int64` money and ids serialize as **bare JSON numbers with no configuration**, including
  `Long.MAX_VALUE` (`9223372036854775807`, verified on both sides).
- java.time support is **folded into `jackson-databind`** in Jackson 3
  (`tools.jackson.databind.ext.javatime`) — there is no `JavaTimeModule`/jsr310 dependency
  to add or forget.
- **Records serialize in declaration order, not alphabetically** — Jackson 3 does flip
  `SORT_PROPERTIES_ALPHABETICALLY` to true, but it also sets `SORT_CREATOR_PROPERTIES_FIRST=true`,
  and for a record every component is a creator property. (Non-record getter beans *do* sort
  alphabetically.) **This answers Q6 of the java-stack doc and makes A29 doubly moot.**
  Consequence worth having: if the Java response records declare components in the same
  order as `services/dotnet/Dtos/InvoiceDtos.cs`, the two wires are **byte-identical**, not
  merely normalize-equal — so the serialize-then-cache Redis payloads are the same size on
  both sides and the cache-hit path compares cleanly. **Use records for every wire DTO.**

One default that must be named but not tuned: Jackson 3's buffer-recycler pool is
`ConcurrentDequePool`, not Jackson 2's ThreadLocal pool — the Loom-appropriate choice, which
matters because D2 turns virtual threads on. It is not reachable from any `spring.jackson.*`
property. `[a third-party JMH harness (FasterXML/jackson discussion #304) reports ~12.7% vs
~2.1% of time for the two pools on platform threads — NOT measured here; attribute it or
drop the numbers and just name the pool.]`

### The FAIRNESS row, as proposed text

> **JSON serialization and binding** (costs Java): .NET binds and writes through a
> System.Text.Json source-generated context (`services/dotnet/Json/LedgerJsonContext.cs`,
> `GenerationMode.Default` — metadata and fast-path writers emitted at compile time, no
> runtime reflection). Java uses the Spring Boot 4.1.1 default, Jackson 3.1.5
> (`tools.jackson.core:jackson-databind`), which resolves properties at runtime through
> MethodHandles. The bytecode-generating accelerators that would close the gap —
> `jackson-module-blackbird` and `jackson-module-afterburner`, both published for Jackson 3
> and both version-managed by the Spring Boot BOM — are deliberately **not** on the
> classpath (pre-registered, PROGRESS.md P3). Because Boot 4 defaults
> `spring.jackson.find-and-add-modules=true` and Blackbird ships a `ServiceLoader` entry,
> adding the dependency alone would enable it, so the absence is asserted **from the
> artifact** rather than from prose: the auto-configured `JsonMapper` registers exactly two
> modules, both Spring's own (`JacksonMixinModule`, `JacksonComponentModule`). Both stacks
> run their framework's default JSON stack and neither was tuned. The serialize-then-cache
> pin (`spec/cache.md`) removes serialization from cache **hits** only — 0.80 × 30% = 24% of
> the mix — so roughly 76% of requests serialize a response body and 35% deserialize a
> request body; `POST /pricing/quote` (20% of the mix, 50 items in and 50 lines out, no DB
> and no Redis) is where the serializer difference is least diluted. Jackson's
> buffer-recycler default is also `ConcurrentDequePool` rather than Jackson 2's ThreadLocal
> pool, chosen upstream for virtual threads (D2) and left alone.

Two deliberate strengthenings over the round-1 wording: it covers **deserialization** as
well as serialization (STJ source-gen accelerates both, and 35% of the mix posts a body —
the round-1 row said "serialization" only), and it **quantifies the cache exemption** rather
than letting "byte-passthrough on both sides" read as though it neutralised the row. Mix
arithmetic from `spec/workload.yaml:5-11`; cache exemption from `spec/cache.md:8-18`.

**Structural difference a reviewer will notice, and the answer:** Java uses one global
naming strategy where `services/dotnet/` puts `[JsonPropertyName]` on every member. STJ
source-gen has no global-naming equivalent, so the two are **idiomatic-for-their-stack**,
not divergent. One sentence in `NOTES-equivalence.md`.

**Optional strengthening:** put the resolved module list (`JsonMapper.registeredModules()`)
into the `/runtime-stats` `config` echo so "no Blackbird" is an artifact rather than a
claim. `shapecheck.py` does not reject extra keys, so it is additive and safe — but
`spec/runtime-stats.md` freezes at P7 and nothing may enter it before its test passes.

---

## 7. Open questions

Each with what would settle it. Nothing here is papered over.

| # | question | what would settle it | by |
|---|---|---|---|
| **J1** | **The version-pinning rule ([4-A]).** D8 gives .NET "latest stable at run start"; the Java side has no counterpart, and five artifacts diverge today. A symmetric, pre-registered rule must be written into FAIRNESS **before** the freeze, and the manifest must carry a pinned-vs-latest column for both sides. | **Owner decision.** This is the java-stack doc's Q13, now overdue and concrete. | **before P7** |
| **J2** | **HikariCP 7.0.2 vs 7.1.0.** The yield-spin is now source-verified and gated on `waiters > 0`, i.e. it appears exactly at pool saturation — where the knee is measured. Does it move CPU-ms per request at 24 connections and 6 carriers? | Rehearsal-tier A/B, published as a labelled variant axis either way. Note Boot 4.2.0 GA (2026-11-30) is expected to carry 7.1.0 — if the run slips past it the axis changes from "override" to "the framework fixed it". | **G6** |
| **J3** | **Hibernate 7.4.5 vs 7.4.6** (released today). An override re-opens every measured SQL shape. 7.4.6's changelog was **not** checked for statement-generation changes. | Decide under J1's rule; if overriding, re-run the 6-row spike against `log_statement='all'`. | **P4 / before P7** |
| **J4** | **`use_get_generated_keys=false` ([4-B]).** Source reasoning says it cannot restore batching; that is reasoning, not measurement. | Re-run the 6-row spike with the flag set, capturing both the round-trip count and the returned column list. Settles S1 and S2 together. | **P4 / G6** |
| **J5** | **`spring.jpa.open-in-view=false` ([4-B]).** Non-default, direction helps Java. Ship as a declared variant with a published delta, or as an equalization with the counter-argument attached? | Rehearsal A/B, or a P7 ruling. | **G6 / G7** |
| **J6** | **Scalar coercion ([4-F]).** Equalize with two properties + a new equivalence vector, or disclose? The governing rule forbids doing neither, and forbids equalizing in `spec/` before a vector asserts it. | **Owner decision.** Recommendation: add the vector and the two properties together at P4 and record the pair as a contract equalization, not a tuning knob. | **P4** |
| **J7** | **`goroutines_or_threadpool` mapping.** Three candidates, and the two committed research docs **disagree**: `2026-08-21-measured.md` recommends `getMountedVirtualThreadCount()`, `2026-08-21-java-stack.md` recommends `ThreadMXBean.getThreadCount()`. The measurements in §4 add a third consideration: `getQueuedVirtualThreadCount()` is the only scheduler number that moves with offered load. **Reading:** `getThreadCount()` is the better like-for-like for the frozen field ("OS threads the runtime holds"), `mounted` is a utilization gauge that reads 0 under pure I/O load and would look like a bug, and `queued` belongs in the extras block as the saturation signal. Someone still has to rule. | P7 reviewer. | **P7** |
| **J8** | **`-1` in `spec/runtime-stats.md`.** The MXBean documents `-1` = "not known" for poolSize/mounted/queued. Coercing it to 0 publishes a measured-looking zero; mapping it to null matches the shape contract used elsewhere. | W27 decision + a test. | **P1/W27** |
| **J9** | **Does the JFR pinning-diagnostic probe get built?** It is the only way to convert the class-init residual from theoretical to an evidenced absence, and per CLAUDE.md **nothing about pinning may enter `spec/` until it exists and passes**. Cost: one rehearsal-scale probe per slot. | Build it, or say nothing about pinning in `spec/`. | **P5/P6** |
| **J10** | **Does any class in the request path initialize lazily inside a measured window?** The only residual pinning source with a plausible route into the tail. | JFR diagnostic probe plus an `-Xlog:class+load` diff between warmup end and window end. | **P6** |
| **J11** | **`determineField` override vs advice-side transform.** Two unprobed interactions: (a) `processConstraintViolations` pre-collects `failedFields` from binding-failure `FieldError`s using the **unrenamed** path and skips violations whose `determineField` result is in that set — with the override the two never match, so a field that both fails type binding **and** has a constraint could emit **two** entries (e.g. `"qty":"abc"` plus a range violation); (b) `getRejectedValue` calls `bindingResult.getRawFieldValue(renamedPath)`, which cannot resolve — the `InvalidPropertyException` is caught and it degrades to `violation.getInvalidValue()`, but that is a **per-violation throw/catch on the 400 path**. `spec/workload.yaml`'s steady-state mix carries **no** error-path share, so this is a ladder-wall cost, not a headline cost — but it should be probed, and doing the transform in the advice instead avoids both. | Two MockMvc probes at P4. | **P4** |
| **J12** | **Path/query parameter validation.** `spec/openapi.yaml` says path/query params use lowercase single tokens (`id`, `page`), which .NET produces by validating manually. `@Valid` on `@PathVariable`/`@RequestParam` throws `HandlerMethodValidationException`, whose `ParameterValidationResult` has a different shape entirely and does **not** go through `determineField`. Unprobed. | One MockMvc probe. | **P4** |
| **J13** | **Response framing** (critique E4, still OPEN and now sharper). Measured today on the same server: the `@RestController` return-value path emitted `Content-Length: 238` while the `ResponseEntity` path emitted `Transfer-Encoding: chunked` for a 134-byte body. **Framing is decided by which MVC return path is used, not by body size alone**, so E4's "check with `curl -sD-`" must test each endpoint's actual return style, not just the 8 KB Tomcat buffer threshold. | `curl -sD-` per endpoint at P4, both stacks. | **P4** |
| **J14** | **DB-pool fill schedule** (critique E6, still OPEN). `warmup.py` gates on `db_pool.total >= db_pool.max`. HikariCP at min=max=24 does not open 24 connections synchronously at startup (on-demand plus housekeeper backfill); Npgsql's `Minimum Pool Size` fills on a different schedule and `services/dotnet/Program.cs` warms it explicitly. Java needs an equivalent explicit warm, or the gate times out asymmetrically. | Implement + measure at P4/P6. | **P4** |
| **J15** | **`spring.main.keep-alive`.** Boot's docs raise it as a virtual-threads side effect (virtual threads are daemon threads). Almost certainly moot with an embedded Tomcat, which holds a non-daemon thread — but it is a one-line verification, not an assumption. | One P4 check. | **P4** |
| **J16** | **`gc.allocated_total_bytes` and virtual-thread `StackChunk`s.** Nobody has measured how much a virtual thread's stack-chunk allocation adds to GC allocation rate for a real Spring MVC + Hibernate request. Every public figure is a toy microbenchmark. | This repo's own per-window `gc.allocated_total_bytes` at G6. **Do not carry a number in from the web.** | **G6** |
| **J17** | **Liberica `-cds` vs `eclipse-temurin`.** Spring's recommended Dockerfile base is `bellsoft/liberica-openjre-debian:25-cds`, and the `-cds` tag ships a layout tuned for AOT cache/CDS. If `eclipse-temurin:25-jre-noble` lacks something that tag provides, the **cold-start number is measuring the image choice as much as the runtime**. | One measured comparison at P4, and a FAIRNESS row either way. | **P4** |
| **J18** | **StackExchange.Redis 3.1.13 → 3.1.31.** Verified today: repo pins 3.1.13, current is 3.1.31. D8 says latest stable at run start, so this is a live D8 violation in the tree. Also under J1. | Haik. | **before P7** |
| **J19** | **`spec/` edits this research implies but does not make.** (a) `spec/cache.md`'s client-pins table is Go-era — its `REDIS_POOL_MAX` row still reads "go-redis PoolSize" and the "go-redis uses a connection pool" asymmetry paragraph are both wrong for round 2 (Lettuce's default is a shared multiplexed connection, the same model as SE.Redis), and the SET-vs-SETEX wire-text difference (A25) needs naming so per-command accounting sums both. (b) `spec/db-access.md`'s binary-format sentence must distinguish **parameters** (binary from execution 1) from **results** (binary only once the statement is named) and make the assertion steady-state (A4). (c) `spec/db-access.md`'s round-trip ledger must become per-stack (Q5/S1). All three land in P1 before the P7 freeze. | W-item assignment. | **P1** |
| **J20** | **G3's wording.** P3's gate says the dials table lands in `docs/FAIRNESS.md`; W31 rewrites that file from a skeleton, last. The table is here instead. Ratify, or move it. | Haik. | **now** |

### Gaps not closed, stated as gaps

- **No greenfield Boot 4 REST reference repo exists** at meaningful star count (§3). The
  layout claim rests on three repos and the reference docs. Installing `gh` or exporting a
  `GITHUB_TOKEN` makes dependency-level code search available and is a ten-minute job if
  more coverage is wanted.
- **`spec/runtime-stats.md` still carries Go-era enums** (`"language": "go|dotnet"`,
  `"data_layer": "sqlc | efcore | ado"`, a `| field | Go | .NET |` header, and a closing
  "Warmup GC-steady signals: Go = …" sentence). Named in the 2026-08-21 critique as OPEN;
  still open. W27 owns it.
- **The fourteen SQL shapes are still asserted without a committed capture artifact**
  (critique §D, still OPEN). The `log_statement='all'` capture must be committed under
  `docs/research/` with line ranges cited per row, or §5 declares verified shapes with zero
  artifacts before `services/java/` exists — the exact over-declaration failure mode.
- **`"G1 Concurrent GC"` STW-vs-concurrent time** (java-stack Q3) was not re-measured today
  and the two prior measurements disagree at the sub-millisecond level (`timeMs 1` vs
  `timeMs 0`), which is below the bean's granularity. The proposed acceptance criterion
  `time ≈ Σ(Remark+Cleanup)` **cannot discriminate**; rewrite it as `count == 2N` plus
  `time <= ceil(Σ STW)`.
- **Boot 4.1 `@RedisListener` auto-configuration** (java-stack Q7) was not checked: does a
  default `RedisMessageListenerContainer` get registered with no listener methods? If so it
  opens a second Redis connection and a listener thread with no .NET counterpart. One
  `CLIENT LIST` at runtime settles it.
