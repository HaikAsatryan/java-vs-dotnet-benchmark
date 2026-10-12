# Java stack findings, 2026-08 — decision-ready

> **Frozen at publication, 2026-10-12.** This is a working research note from 2026-08-21,
> kept as written apart from a few wording edits. Later files win where they differ:
> `spec/runtime-stats.md` over §4's mapping, `2026-08-23-java-research.md` and
> `docs/FAIRNESS.md` over §1 and the dials. The §7 questions were decided or dropped later;
> recorded decisions are in `PROGRESS.md` (O6, O7, the P7 review). Probe sources for its
> measurements were not kept in the repository.

**Status:** research output for **P3 / gate G3**. Every version, default and API signature below was verified against a live source dated 2026-08-19..21, or measured on this box.

**Companion:** `docs/research/2026-08-21-measured.md` (locally measured facts).

**This document feeds:** the `docs/FAIRNESS.md` dials table (G3), the JVM rewrite of `spec/runtime-stats.md`, the `sut-java` block in `infra/compose.yaml` + the knob surface in `bench/ledgerbench/manifest.py`, and the P4 implementation.

### Corrections to the companion measured doc — read these first

The companion's SQL was captured with `StatementInspector`, which sits **above the JDBC driver**. Three driver-level behaviours are structurally invisible at that capture point, and two of the companion's findings are false negatives because of it. Verified against PostgreSQL 18.4 with `log_statement='all'`:

| companion claim | corrected |
|---|---|
| FINDING 3: `@Transactional(readOnly=true)` "adds nothing to the wire" | **False negative.** Spring's `HibernateJpaVendorAdapter` forces `hibernate.connection.handling_mode=DELAYED_ACQUISITION_AND_HOLD`, so `HibernateJpaDialect` calls `Connection.setReadOnly(true)`; PgJDBC's default `readOnlyMode=transaction` then sends **`BEGIN READ ONLY`** instead of `BEGIN`, plus a separate `COMMIT` round-trip. `StatementInspector` never sees BEGIN/COMMIT. |
| Q5 row: item insert emits "no `returning id`, ids read via JDBC `getGeneratedKeys`" | **Incomplete.** PgJDBC's `getGeneratedKeys` path appends a literal newline + **`RETURNING *`** to the statement — all 7 item columns come back per row, not `id`. `StatementInspector` never sees the appended clause. |
| (not stated) | `StatementInspector` also does not see PgJDBC's `?` → `$n` rewrite. Wire-text parity claims must come from PostgreSQL-side capture. |

**Consequence for P5:** the wire ledger must be built from `log_statement='all'` / `pg_stat_statements`, not from `StatementInspector`. Keep the inspector for in-JVM shape assertions; it is the right tool for that and the wrong tool for a parity claim.

---

## 1. Pinned versions

Resolve every digest at P4 and record it in `manifest.versions`; `Versions.source` is still `not_measured` today (R1-B2 class defect).

### Java runtime and container

| artifact | pin | published | source |
|---|---|---|---|
| Eclipse Temurin JDK 25 (host toolchain) | **25.0.4+7** | 2026-07-27 (announced 2026-08-04) | https://api.adoptium.net/v3/assets/latest/25/hotspot?architecture=x64&image_type=jdk&os=linux&vendor=eclipse |
| Temurin 25.0.4.1+1 | **do not use** — no linux x64 glibc asset exists (alpine/musl, mac x64, win x64 only) | 2026-08-19 | https://api.adoptium.net/v3/assets/release_name/eclipse/jdk-25.0.4.1%2B1 |
| `eclipse-temurin:25-jre-noble` (container) | resolves to **25.0.3+9** today — one patch behind the host | image rebuilt 2026-08-19 | https://github.com/docker-library/official-images/blob/master/library/eclipse-temurin |
| Container base choice | `-noble` (Ubuntu 24.04), **explicitly**, never the floating `25-jre` which resolves to `-resolute` (Ubuntu 26.04) | — | matches `aspnet:10.0-noble-chiseled` OS generation |

### Spring Boot 4.1.1 BOM (independently re-resolved; matches the companion's build)

| artifact | version | source |
|---|---|---|
| `spring-boot-starter-parent` | **4.1.1** (published 2026-08-20; 4.0.8 same day; 4.2.0-M1 milestone) | https://repo1.maven.org/maven2/org/springframework/boot/spring-boot-dependencies/4.1.1/spring-boot-dependencies-4.1.1.pom |
| Spring Framework | 7.0.9 | same |
| Spring Data BOM 2026.0.1 → spring-data-jpa / -redis / -commons | 4.1.1 | https://repo1.maven.org/maven2/org/springframework/data/spring-data-bom/2026.0.1/spring-data-bom-2026.0.1.pom |
| Hibernate ORM | **7.4.5.Final** | https://hibernate.org/orm/releases/ |
| Jakarta Persistence / Validation / Servlet / Annotation | 3.2.0 / 3.1.1 / 6.1.0 / 3.0.0 | BOM |
| Hibernate Validator | 9.1.3.Final | BOM |
| Tomcat embed | 11.0.24 | BOM |
| **HikariCP** | **7.0.2** (BOM) — see §3, override candidate **7.1.0** (2026-06-14) | https://repo1.maven.org/maven2/com/zaxxer/HikariCP/maven-metadata.xml |
| PgJDBC | 42.7.13 (2026-07-06; ≥42.7.12 carries the CVE-2026-54291 SCRAM fix) | https://jdbc.postgresql.org/changelogs/2026-07-06-42.7.13-release/ |
| Lettuce | 7.5.2.RELEASE (netty 4.2.17.Final) | BOM |
| Jackson | **3.1.5** under `tools.jackson.*`; `jackson-annotations` stays `com.fasterxml.jackson.annotation` at 2.21 | https://docs.spring.io/spring-boot/reference/features/json.html |
| Logback / SLF4J | 1.5.38 / 2.0.18 | BOM |
| Micrometer | 1.17.1 (managed, **not used** — see §3) | BOM |
| JUnit Jupiter / Mockito / AssertJ | **6.0.3** (JUnit 6) / 5.23.0 / 3.27.7 | BOM |
| Testcontainers | 2.0.5 — modules renamed `org.testcontainers:testcontainers-postgresql`, `PostgreSQLContainer` moved to `org.testcontainers.postgresql` and is no longer generic | https://repo1.maven.org/maven2/org/testcontainers/testcontainers-postgresql/maven-metadata.xml |
| Maven (D11) | 3.9.16, `mvnw` `distributionType=only-script`. Maven 4 is still 4.0.0-rc-6, not GA | https://repo1.maven.org/maven2/org/apache/maven/maven-core/maven-metadata.xml |
| Spotless / palantir-java-format | 3.10.0 / 2.97.0 (alt: google-java-format 1.36.1) | https://github.com/diffplug/spotless/blob/main/plugin-maven/README.md |

**Support horizon (decides 4.0.x vs 4.1.x):** Spring Boot **4.0.x OSS support ends 2026-12-31**; **4.1.x ends 2027-07-31**; 4.2.0 GA is due 2026-11-30. Source: https://api.spring.io/projects/spring-boot/generations. Pin **4.1.1**. A 4.0.x pin would put the benchmark on a line that leaves OSS support four months after the run. (Note: Spring Cloud's train is on Boot 4.0.x, which is a train-cadence artifact and *not* evidence about 4.1; this repo uses no Spring Cloud.)

### .NET side, re-verified (D8: "latest stable at run start")

| artifact | repo pin | current stable | action |
|---|---|---|---|
| .NET runtime / ASP.NET | 10.0.11 (image `aspnet:10.0-noble-chiseled`) | **10.0.11** (2026-08-11, security) | none — already current |
| SDK | 10.0.400 band | 10.0.400 | none |
| EF Core / EFCore.Relational | 10.0.11 | 10.0.11 | none |
| Npgsql / Npgsql.EntityFrameworkCore.PostgreSQL | 10.0.3 / 10.0.3 | 10.0.3 / 10.0.3 | none |
| **StackExchange.Redis** | **3.1.13** (2026-08-06) | **3.1.31** (2026-08-21) | **decide before P7** — bump, or record "latest stable as of the pin date" in the manifest. Only stale pin on the .NET side. |
| Dapper | 2.1.79 | 2.1.79 | unused under D5 |

**.NET 11 is STS**, GA 2026-11-10, supported only to 2028-11-09. .NET 10 is the LTS through 2028-11-14. The "Java 25 LTS vs .NET 10 LTS" framing is correct and stays correct past .NET 11 GA. Source: https://github.com/dotnet/core/blob/main/release-notes/11.0/README.md

### Shared infrastructure (already pinned in `infra/compose.yaml`)

| image | pin | note |
|---|---|---|
| `postgres:18.4-bookworm` | digest-pinned 2026-08-13 | PG 18.6 is the current supported minor (2026-08-13). Identical for both SUTs, so it cannot bias; leave it. |
| `redis:8.10.0-trixie` | digest-pinned 2026-08-13 | **Lettuce 7.5.2's release notes list tested servers up to Redis 8.6, not 8.10.** The workload uses only `GET`/`SET..EX`/`DEL`/`PING` and RESP3 `HELLO`, all stable since Redis 2.6. Disclose rather than bump the BOM. |

---

## 2. The Java stack, decided

### Build

- **Maven** (D11), single module, `spring-boot-starter-parent:4.1.1` as `<parent>`, Initializr `mvnw` wrapper pinning 3.9.16 with no committed jar.
- **`<java.version>25</java.version>` must be set explicitly.** `spring-boot-starter-parent:4.1.1` sets `<java.version>17</java.version>` feeding `maven.compiler.release`. Without the override you compile to bytecode 61 while running on JDK 25 — a real methodology defect if a reviewer unpacks the jar. Assert the class-file major version in a test and record the compile target next to `java.vm.version` in the manifest.
- Set `<project.build.outputTimestamp>` (the parent does **not**) for a reproducible jar; `spring-boot-maven-plugin:repackage` honours it.

### Dependencies — compile scope

`spring-boot-starter-webmvc`, `spring-boot-starter-validation`, `spring-boot-starter-data-jpa`, `spring-boot-starter-data-redis`; `org.postgresql:postgresql` at runtime scope.

- **Never `spring-boot-starter-web`** — its own 4.1.1 POM description reads "deprecated in favor of spring-boot-starter-webmvc", and Initializr emits `-webmvc`. Using it reads as a 3.x copy-paste in the first ten seconds of review.
- **No JSON starter needed** — `-webmvc` already pulls `spring-boot-starter-jackson` → `tools.jackson.core:jackson-databind`.
- **No `spring-boot-starter-classic`.** It puts every auto-configuration class on the classpath, inflating condition evaluation, loaded-class count and steady-state RSS — the exact number this benchmark reports — against a .NET side that registers services explicitly.

### Dependencies — explicitly excluded, and why it matters

| excluded | reason |
|---|---|
| `spring-boot-starter-actuator` | pulls `micrometer-observation` → an `ObservationRegistry` bean → per-request `http.server.requests` instrumentation inside the measured window, with no .NET counterpart (the .NET SUT ships no OTel exporter and reads counters on demand via a `MeterListener`). |
| `micrometer-core` / `JvmGcMetrics` | `JvmGcMetrics.bindTo()` registers a `NotificationListener` on every GC bean, which flips `setNotificationEnabled(true)` in the VM. Thereafter **every counted collection** allocates a `GCStatInfo` + `GCNotificationRequest`, takes `Notification_lock`, wakes the ServiceThread and builds a full `GcInfo` CompositeData (2 × pool-count `MemoryUsage` objects). Permanent per-GC cost for data the raw MXBeans give free. |
| **`micrometer-java21`** | Spring Boot auto-configures `VirtualThreadMetrics` whenever this artifact is on the classpath (since 3.5.0-M2, still true in 4.x), and that binder opens a **`jdk.jfr.consumer.RecordingStream`**. JFR would start silently, inside the measurement window, via a transitive dependency. |
| Lombok, MapStruct, JHipster scaffolding | not present in `services/dotnet/`; adds surface a parity reviewer must reason about. |

Assert the absence of `commons-pool2`, `micrometer-java21` and any `-javaagent` in a classpath test. `commons-pool2` in particular is a footgun: Boot's gate is `isPoolEnabled(pool) → enabled != null ? enabled : COMMONS_POOL2_AVAILABLE`, so **mere classpath presence turns Lettuce pooling on** without any property being set.

### Layout

Mirror `services/dotnet/` by technical concern, not package-by-feature. Spring's own docs illustrate package-by-feature, and that is the right default for a service that will grow — but the governing constraint here is that a fairness reviewer must be able to put the two trees side by side. Package structure has zero runtime cost, so it cannot influence a measured number; state the reason in `services/java/NOTES-equivalence.md` and the "not idiomatic Spring" objection is answered.

| `services/dotnet/` | `services/java/src/main/java/io/ledgerline/` |
|---|---|
| `Program.cs` | `LedgerlineApplication.java` (root package) |
| `Config/` | `config/` |
| `Data/` | `data/` |
| `Dtos/` | `dto/` (Java **records**) |
| `Endpoints/` | `web/` (`@RestController`) |
| `Infrastructure/` | `infrastructure/` |
| `Json/` | `json/` |
| `Logging/` | `logging/` |
| `Money/`, `Pricing/`, `Problems/`, `Sql/` | `money/`, `pricing/`, `problem/`, `sql/` |
| `Observability/` | `observability/` |

### Config binding — the trap that would silently half-work

Spring relaxed binding maps env vars by removing invalid characters, lower-casing and replacing `_` with `.`. **`DB_POOL_MAX` therefore binds to `db.pool.max`, and can never reach `app.db.pool-max` — relaxed binding cannot invent a prefix.** (Verified in `SystemEnvironmentPropertyMapper` on the v4.1.1 tag; the reverse direction emits both `APP_DB_POOLMAX` and `APP_DB_POOL_MAX`, which is why the docs read ambiguously.)

The env surface in `infra/compose.yaml` is frozen and shared with the .NET SUT. So:

- one `application.yaml`, every value an explicit `${ENV:default}` placeholder over that exact surface;
- bound into a `@ConfigurationProperties` **record** tree with `@Validated` + jakarta constraints, enabled by `@ConfigurationPropertiesScan` (the parent POM already sets `-parameters`, so no `@ConstructorBinding` needed);
- the in-code defaults must be numerically identical to `LedgerConfig.FromEnvironment()` (`DB_POOL_MAX` 24, `CACHE_TTL_SECONDS` 300, `LOG_QUEUE_LEN` 8192, `BODY_MAX_BYTES` 65536, `DB_CONN_LIFETIME` 3600) — assert that in a test. A silent default drift between the two services is exactly what a reviewer will grep for.
- **Unit trap:** `spring.datasource.hikari.max-lifetime` is **milliseconds**; `DB_CONN_LIFETIME` is seconds. ×1000.

Also grep the config for Boot 3.x property names that bind to nothing in Boot 4 and fail silently: `server.error.*` → `spring.web.error.*`, `server.servlet.encoding.*` → `spring.servlet.encoding.*`, `management.endpoint.<id>.enabled` → `.access`, `spring.jackson.read/write/parser/generator`.

### Logging

- **Structured formatter, not a pattern.** Implement `org.springframework.boot.logging.structured.StructuredLogFormatter<ILoggingEvent>` emitting `spec/logging.md`'s exact flat object, and register it by FQCN: `logging.structured.format.console=io.ledgerline.logging.LedgerStructuredLogFormatter`. This is Boot's own extension point (its ECS formatter is the same interface) and it is byte-exact by construction — the direct analog of `services/dotnet/Logging/LedgerConsoleFormatter.cs`. Do not use `ecs`/`gelf`/`logstash` (all nest/rename fields) and do not use `logstash-logback-encoder`.
- Field carriage: SLF4J 2 fluent API — `log.atInfo().setMessage("http_access").addKeyValue("method", m)...log()`. Boot's own formatters consume `ILoggingEvent::getKeyValuePairs`, so this is the supported path.
- Timestamps: the formatter verified in the companion doc, `DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSSSSS'Z'").withZone(ZoneOffset.UTC)`. Truncating, always 27 chars.
- `spring.main.banner-mode=off`; everything Spring/Hibernate logs at startup goes through the structured formatter, so every stdout line is well-formed JSON.
- **Known non-JSON startup line:** the `java` launcher prints `NOTE: Picked up JDK_JAVA_OPTIONS: …` to **stderr** whenever that variable is set. The runner's config-line parser must tolerate it. It is also free provenance — but read the flags back through `HotSpotDiagnosticMXBean` for the manifest, which is structured and carries an `origin`.

### Access log

`OncePerRequestFilter` (not a `HandlerInterceptor`, whose `afterCompletion` does not fire for requests rejected before handler mapping). `System.nanoTime()` on entry, one line in a `finally`, skipping `/healthz`, `/readyz`, `/runtime-stats`.

**Mirror-image hazard, worth a comment in the code:** on .NET the matched endpoint must be snapshotted **before** `next()` because `ExceptionHandlerMiddleware` clears it (`AccessLogMiddleware.cs`). On Java the route template attribute `HandlerMapping.BEST_MATCHING_PATTERN_ATTRIBUTE` is only populated **after** handler mapping runs, so it must be read **after** `doFilter`. Getting it backwards silently produces null paths.

### Async sink — Logback's default would silently break the frozen contract

`spec/logging.md` freezes: bounded 8192, block-on-full, **lossless**, full drain on SIGTERM. Logback's `AsyncAppender` defaults violate two of the three:

| setting | Logback default | required | consequence of the default |
|---|---|---|---|
| `queueSize` | 256 | **8192** (`LOG_QUEUE_LEN`) | — |
| `discardingThreshold` | resolved in `start()` to `queueSize / 5` | **0** | `AsyncAppender.isDiscardable()` returns true for `level <= INFO`. Every `http_access` line is INFO. **At the default, once the queue is 80% full every access line is silently dropped — under exactly the load being measured.** |
| `neverBlock` | false | **false** (keep) | correct as-is: `putUninterruptibly` → `blockingQueue.put()` is real producer backpressure, matching `QueueFullMode = Wait`. |
| `maxFlushTime` | 1000 ms | **0** (or a large finite value) | `stop()` calls `worker.join(maxFlushTime)`; at 1000 ms it logs "Max queue flush timeout exceeded" and discards the tail. |
| `includeCallerData` | false | false | — |

Assert all five in a test and echo queue length + block-on-full in the `msg="config"` line, as the .NET side does. Note honestly in `docs/METHODOLOGY.md` that the no-drop microbench was descoped on 2026-08-13, so the guarantee rests on configuration assertion, not measurement.

### Dockerfile

Multi-stage. Builder `FROM eclipse-temurin:25-jdk-noble@sha256:…`: `./mvnw -B -q dependency:go-offline` as its own cached layer, then `./mvnw -B -q -DskipTests package`, then `java -Djarmode=tools -jar app.jar extract --layers --destination extracted`. (`layertools` was **removed** in Boot 4.1 — `tools` is the only mode.) Runtime stage `FROM eclipse-temurin:25-jre-noble@sha256:…`, COPY `dependencies/ spring-boot-loader/ snapshot-dependencies/ application/`, non-root user, `EXPOSE 8080`, **exec-form** `ENTRYPOINT ["java","-jar","application.jar"]` (shell form would break SIGTERM → flush-on-shutdown). No container `HEALTHCHECK` — the runner polls `/readyz`, matching the .NET SUT.

Add a **second build target** (`--target diag`) whose final stage is the JDK image, because **Temurin JRE images ship no `jcmd`** (adoptium/temurin-build#3336, closed wontfix). Diagnostics must never be in the measured image.

**No AOT cache, no CDS, no Spring AOT** in the measured image. Spring Boot's own Dockerfile page now recommends `-XX:AOTCacheOutput` for Java 25+, and that is precisely why it must be excluded: it is Java's structural analog of ReadyToRun, and `docs/FAIRNESS.md` already pins .NET at "tiered JIT with dynamic PGO on, **no ReadyToRun and no Native AOT**". Excluding it is symmetry, not a handicap — say so explicitly so the omission reads as a decision.

### Testing

Testcontainers as **Spring beans**, not the JUnit `@Container` extension (Boot's docs are explicit: the JUnit extension stops containers after the test class while the application context stays cached — the classic CI flake). `@TestConfiguration(proxyBeanMethods = false)` with `@Bean @ServiceConnection PostgreSQLContainer` and `@Bean @ServiceConnection(name = "redis") GenericContainer<?>`, image tags pinned to the same `postgres:18.4` / `redis:8.10.0` the benchmark runs. Per-technology test starters (`spring-boot-starter-webmvc-test`, `-data-jpa-test`, `-data-redis-test`, `-validation-test`), not a bare `spring-boot-starter-test`.

### Formatting

Spotless 3.10.0 + palantir-java-format 2.97.0 (120-col, lambda-friendly, closest to the existing C#). `mvn -q spotless:apply` into `make fmt`; `mvn -q spotless:check` into `make lint` and CI, structurally identical to the existing `gofmt -l` / `dotnet format --verify-no-changes` gates. **Not** `spring-javaformat`: it indents with tabs, which fights `.editorconfig` and would make `services/java/` look alien next to the space-indented trees. No Checkstyle — a second config surface for no benefit at 4k LOC.

---

## 3. JVM and framework knob surface

Categories are exactly three: **`default`**, **`set-identically-to-.NET`**, **`declared-variant-axis`**. Anything else is flagged **`[FOURTH CATEGORY]`** and must land in `docs/FAIRNESS.md`'s asymmetry table with the direction it biases, or be dropped at P7.

Every row is read back at runtime through `HotSpotDiagnosticMXBean.getVMOption(name)` (which returns an `origin`: DEFAULT / ERGONOMIC / VM_CREATION) and echoed into the `msg="config"` line — that is what turns "this is a default" into an artifact.

### JVM flags

| knob | value | category | notes / read-back |
|---|---|---|---|
| GC engine | **nothing passed** | `default` | `UseG1GC=true {ergonomic}` measured in the target container. Do **not** pass `-XX:+UseG1GC`; that would make a default look tuned. |
| `-XX:MaxRAMPercentage` | unset → **25.0** | `default` | `MaxHeapSize = 3,221,225,472` under `-m 12g`. Headline `heap-default` cell. |
| `-XX:MaxRAMPercentage=75` | 75.0 | `declared-variant-axis` | `heap-equalized` cell only, pending D6. Yields 9,663,676,416 — byte-identical to the CLR's default hard limit. |
| `-XX:+UseZGC` | on | `declared-variant-axis` | `heap-equalized` cell only. **Never write `-XX:+ZGenerational`** — removed in JDK 24 (JEP 490); the JVM prints `Ignoring option ZGenerational; support was removed in 24.0`. `-XX:+UseZGC` alone is generational on 25. |
| `-XX:ActiveProcessorCount=${SUT_CORES}` | 6 | `set-identically-to-.NET` | Mirrors `DOTNET_PROCESSOR_COUNT`. Provable **no-op** here: HotSpot derives 6 from `sched_getaffinity` under cpuset with no CFS quota. Prove the no-op with a `-XX:+PrintFlagsFinal` diff (ParallelGCThreads 6, ConcGCThreads 2, CICompilerCount 3 identical with and without) and commit it as a test artifact. |
| `-XX:+UseCompactObjectHeaders` | **not set** (default false) | `default` | Product feature in JDK 25 (JEP 519) but not the default; JEP 534 makes it default in **JDK 27**. 10–20% heap saving. Enabling it in the defaults cell is benchmark-gaming. It is a legitimate `declared-variant-axis` candidate if a memory-tuned cell is ever run. |
| `-XX:+UseTransparentHugePages` | **not set** (default false) | `default` | Record `/sys/kernel/mm/transparent_hugepage/{enabled,shmem_enabled}` in `host_state`. See §6 — this is a live ZGC fairness hazard, not a footnote. |
| `-XX:NativeMemoryTracking` | **off** | `default` | Oracle's JDK 25 doc still states a 5–10% JVM performance drop. Never in a ledgered run. Native-memory composition comes from a separate characterization run using `-XX:+UnlockDiagnosticVMOptions -XX:+PrintNMTStatistics` (works without `jcmd`). |
| `-Xlog:gc*` | **not enabled in measured cells** | `set-identically-to-.NET` | The .NET SUT runs no EventPipe/ETW session. `/runtime-stats` already carries collections / pause / heap per window and P1 flushes it per window (R1-B3). GC logging belongs in a separate diagnostic pass; if used, write to a **file**, never stdout (it would enter the frozen log stream). |
| JFR | **not enabled** | `set-identically-to-.NET` | Assert no recording is active (`RuntimeMXBean.getInputArguments()` has no `-XX:StartFlightRecording`, `jdk.jfr.FlightRecorder` has no recordings). |
| `-XX:AOTMode` / `-XX:AOTCache` | unset (`auto`, no cache) | `set-identically-to-.NET` | Mirrors "no ReadyToRun, no Native AOT". |
| `jdk.virtualThreadScheduler.*` | **not set** | `default` | Defaults on 6 cores: `parallelism=6`, `maxPoolSize=max(parallelism,256)=256`, `minRunnable=max(parallelism/2,1)=3`. Read `parallelism` back from `VirtualThreadSchedulerMXBean` so the artifact proves it equals the cpuset width. |
| `-Xss` | unset (1024 KB on linux-x64) | `default` | Reserved address space, not RSS. See §6 for why the "virtual threads save 200 MB of stacks" claim is wrong. |
| `MALLOC_ARENA_MAX` | **not set** | `default` | A glibc knob with no .NET counterpart that directly lowers the RSS number being reported. Setting it is the shape of a gaming accusation. (Paketo/Heroku use 2, not 4, if it is ever wanted as a labelled variant.) |
| `JAVA_TOOL_OPTIONS` / `JAVA_OPTS` | must be **empty** | — | assert. `registry.py` already picks `JDK_JAVA_OPTIONS` and the reasoning there is right: the launcher *prepends* it, so an overlay can override a base value. |
| `-XX:-EnableDynamicAgentLoading` | on | **`[FOURTH CATEGORY]`** | **Integrity guard, not a performance dial.** A JVMTI agent reintroduces virtual-thread pinning that JEP 491 otherwise eliminated: `Continuation::try_preempt` returns `freeze_pinned_native` under `JvmtiUnmountBeginMark` whenever `VTMS_notify_jvmti_events()` is true (verified in `continuation.cpp`, jdk-25-ga; Azul confirmed this as the root cause of zulu-openjdk#341 and called it expected behaviour). No .NET analog exists because .NET has no equivalent attach mechanism that silently changes scheduling. Flagged for P7 to rule on; the honest alternative is to drop the flag and instead **assert** an empty agent list from the manifest. Either way, record the agent list. JFR is *not* a JVMTI agent and is unaffected; async-profiler *is*. |

### Framework and library knobs

| knob | value | category | notes |
|---|---|---|---|
| `spring.threads.virtual.enabled` | **true** | `set-identically-to-.NET` (in role) | D2. One boolean; Boot's `TomcatVirtualThreadsWebServerFactoryCustomizer` (`@ConditionalOnThreading(VIRTUAL)`) installs Tomcat's own `VirtualThreadExecutor("tomcat-handler-")`. Both stacks then run their runtime's non-thread-bound concurrency model. Leaving it false would impose a 200-thread cap with no .NET counterpart. |
| `server.tomcat.threads.max` / `.min-spare` | **absent from the config file** | — | Tomcat 11 ignores `maxThreads`/`minSpareThreads` whenever an Executor is attached and reports them as `-1` over JMX. Leaving `threads.max: 200` in `application.yaml` would be a **lie in the artifact**: a reader would believe concurrency was capped at 200 when the real ceiling is `max-connections`. Delete, do not set. |
| `server.tomcat.max-connections` | 8192 (default) | `default` | The real admission ceiling under virtual threads. Kestrel sets no equivalent (`MaxConcurrentConnections` unset). Cannot bind at the p99=20 ms knee; disclose (§6). |
| `server.tomcat.accept-count` | 100 (default) | `default` | — |
| **`server.tomcat.max-keep-alive-requests`** | **-1 (unlimited)** | `set-identically-to-.NET` | **Tomcat's default is 100**; Kestrel has no per-connection request cap. At the default, Java would force a connection re-establishment every 100 requests on a keep-alive load generator — pure connection churn the .NET side never pays, growing linearly with rate. Set `-1` to match Kestrel and assert it (drive 200 requests on one connection, count TCP connections). |
| `server.tomcat.connection-timeout` / `keep-alive-timeout` | 5s / 120s | `set-identically-to-.NET` | Mirrors Kestrel `RequestHeadersTimeout = 5s`, `KeepAliveTimeout = 120s` (`Program.cs:27-28`). |
| `server.port` | `${PORT:8080}` | `set-identically-to-.NET` | — |
| **`BODY_MAX_BYTES` enforcement** | app-level filter | **`[FOURTH CATEGORY]`** | Kestrel enforces `MaxRequestBodySize` **below** the app, which is why the frozen contract says a 413 produces an access line and **no** `request_error`. Tomcat's `maxPostSize` covers form posts only — there is **no** container-level body cap for `application/json`. Java must enforce it in a filter, i.e. *inside* the app, and must be careful not to emit a `request_error`. Different enforcement layer, same observable contract. Disclose. |
| **`spring.jpa.open-in-view`** | **false** | **`[FOURTH CATEGORY]`** | Boot's default is **true** (the proposal to flip it for 4.0, issue #47547, was declined) and Boot logs a startup WARN when it is left on implicitly. OSIV binds an `EntityManager` — and with `DELAYED_ACQUISITION_AND_HOLD`, its connection — to the request thread for the whole request. **Direction: turning it off helps Java** (less pool occupancy), which is exactly why it must be declared. The counter-argument that must ride with it: EF Core has no request-scoped session extension, so OSIV-on would mean the two stacks are not doing the same work per request, and it would suppress `session.setDefaultReadOnly(true)`. |
| **`hibernate.jdbc.use_get_generated_keys=false`** | false | **`[FOURTH CATEGORY]`** | Non-default. Switches Hibernate from `GetGeneratedKeysDelegate` (PgJDBC appends `RETURNING *` — all 7 item columns) to `InsertReturningDelegate`, emitting exactly `… returning id`, which is Q5's frozen shape. **Direction: helps Java slightly** (1 column on the wire instead of 7 per item, × 3–8 items × 15% of the mix). Shape-parity setting, not a speed setting — but it is not a default and must be declared. Measure both at rehearsal; it is a two-line flip. |
| `hibernate.jdbc.batch_size` / `order_inserts` / `order_updates` | unset / false / false | `default` | Setting `batch_size=50` would be a declared knob that **provably does nothing** for IDENTITY entities (§5) — the over-declaration failure mode CLAUDE.md forbids. Pin the defaults explicitly for auditability and cite `InsertCoordinatorStandard`. |
| `hibernate.cache.use_second_level_cache` / `use_query_cache` | false / false | `default` | Already off (no `RegionFactory` on the classpath); pin explicitly so it is audited rather than accidental. |
| `spring.datasource.hikari.maximum-pool-size` / `minimum-idle` / `max-lifetime` | 24 / 24 / 3600000 | `set-identically-to-.NET` | Single env surface. `idle-timeout` is structurally inert under min=max. HikariCP's own advice matches the repo policy: a fixed-size pool. |
| `spring.datasource.hikari.register-mbeans` | false (default) | `default` | Reach the MXBeans in-process via `HikariDataSource.getHikariPoolMXBean()` / `getHikariConfigMXBean()` through `org.springframework.boot.jdbc.DataSourceUnwrapper`. No JMX registration needed. |
| `spring.datasource.connection-fetch` | `eager` (default) | `default` | Do **not** set `lazy` (new in 4.1, wraps the pool in `LazyConnectionDataSourceProxy`). It is a Java-only knob with no EF counterpart that reduces measured connection contention — textbook gaming shape. |
| **`spring.data.redis.timeout`** | **5s** | `set-identically-to-.NET` | Lettuce's default command timeout is **60 s** (`RedisURI.DEFAULT_TIMEOUT`) and Boot leaves the property null; SE.Redis defaults `SyncTimeout` to **5000 ms**, and `Program.cs:81-83` leaves it at that default. A 12× gap changes tail behaviour past the knee for reasons unrelated to runtime cost. This is an **equalization toward the other side's default**, i.e. strictly *less* permissive for Java — say that, because "you set a timeout" is otherwise read as tuning. |
| `spring.data.redis.lettuce.pool.*` | unset; `commons-pool2` absent | `default` | Default Lettuce is one shared multiplexed `StatefulRedisConnection` (`shareNativeConnection=true`) — the same model as SE.Redis's `ConnectionMultiplexer`. Note Boot never calls `setShareNativeConnection`, so even *with* pooling enabled the ordinary GET/SET path stays on the shared connection; converting the cache path to a pool would require `shareNativeConnection=false` as well, which would be gaming. |
| `spring.mvc.problemdetails.enabled` | false (default) | `default` | Frozen error bodies come from `spec/openapi.yaml` `x-error-strings` and are constructed explicitly, exactly as `services/dotnet/Problems/` does. Spring's `ProblemDetail` would emit `type/title/status/detail/instance`, which is not the frozen envelope. |
| `spring.jackson.*` | untouched (Boot 4 applies **no** feature overrides unless `use-jackson2-defaults=true`) | `default` | No Blackbird/Afterburner (pre-registered). Record the resolved module list: `spring.jackson.find-and-add-modules` now defaults to **true** and registers *every* Jackson module on the classpath, not just well-known ones. |
| `spring.main.banner-mode` | off | `set-identically-to-.NET` | Log-contract parity, not performance. |

### What `infra/compose.yaml` must do

`registry.py` already declares `JVM_MAX_RAM_PERCENT` → `max_ram_percent`, `JVM_GC_FLAGS` → `gc_engine`, and `parallelism_var="JDK_JAVA_OPTIONS"` with template `-XX:ActiveProcessorCount={n}`. **Those three collide on one variable.** The `sut-java` block must compose them explicitly, once, in the compose file:

```yaml
JDK_JAVA_OPTIONS: >-
  -XX:ActiveProcessorCount=${SUT_CORES:-6}
  -XX:MaxRAMPercentage=${JVM_MAX_RAM_PERCENT:-25}
  ${JVM_GC_FLAGS:-}
  -XX:-EnableDynamicAgentLoading
```

and `registry.JAVA.parallelism_var` should become a bare `SUT_CORES` integer (template `{n}`) rather than a second writer of `JDK_JAVA_OPTIONS`. Otherwise a cell that sets a GC flag silently drops the processor-count pin, or vice versa.

### What `bench/ledgerbench/manifest.py` must gain

`registry.py` is ahead of `manifest.py` and `phases.py`. Concrete deltas, all currently latent failures because `Knobs` is `extra="forbid"`:

| location | defect | fix |
|---|---|---|
| `manifest.Knobs` | no `max_ram_percent`, no `gc_engine`, no `auto_prepare_min_usages` — all three are already referenced by `registry.py`'s `knob_env` maps | add them, plus Java-only `active_processor_count`, `prepare_threshold`, `open_in_view`, `use_compact_object_headers` |
| `manifest.CellKnobs` | has `go: Knobs` and `dotnet: Knobs` only | add `java: Knobs`; decide whether `go` stays |
| `manifest.RunManifest` | `knobs_go` / `knobs_dotnet` | add `knobs_java` |
| `manifest.Versions` | `go, dotnet, pgx, npgsql, sqlc, …` | add `java` (Temurin build string), `spring_boot`, `hibernate`, `pgjdbc`, `hikaricp`, `lettuce`. `extra="allow"` tolerates them undeclared, but declare them. |
| `phases._record_cell_knobs` | hard-coded `("go", …), ("dotnet", …)` loops, its own `_GO_ONLY_KNOB_ENV`/`_DOTNET_ONLY_KNOB_ENV` duplicating `registry.py`, and `expected_dl = {"go": "sqlc", "dotnet": …}` | drive from `registry.LANGUAGES`; add `"java": "jpa"` to the expected-data-layer map. Note `AUTO_PREPARE_MIN_USAGES` is in `registry.py` but missing from `phases._DOTNET_ONLY_KNOB_ENV` today, so it is currently never recorded. |
| `phases._INT_KNOBS` | — | add `max_ram_percent`, `active_processor_count`, `prepare_threshold`, `auto_prepare_min_usages` |

---

## 4. `/runtime-stats` JVM field mapping

Goes verbatim into the frozen `spec/runtime-stats.md` Java column. Exactness beats completeness.

**Hold the beans as fields resolved once at startup.** `ManagementFactory.getGarbageCollectorMXBeans()` / `getMemoryPoolMXBeans()` are implemented as `getPlatformMXBeans(...)` → a new `ArrayList` through a stream pipeline on every call.

**Measured full-scrape cost:** 28.8 µs under G1, 34.6 µs under ZGC (mean of 1000 in-container iterations). At 1 Hz that is ~3 × 10⁻⁵ of one core. Measure the equivalent .NET `RecordObservableInstruments()` path once and publish both, so the "does the endpoint perturb the measurement" question is answered with a number.

**Runner model constraints that decide null-vs-zero** (from `bench/ledgerbench/runtimestats.py`, `extra="allow"` on every nested model):

- `GcCollections.gen0/gen1/gen2: int` — **non-optional**. A `null` raises in `model_validate` and `StatsPoller` drops the whole sample, silently degrading the warmup gate.
- `RedisPool.in_use: int` — **non-optional**. Same hazard.
- `Gc.heap_goal_bytes: int | None` and all three `DbPool.acquire_*` — nullable.
- `RuntimeStats` is `extra="allow"`, so extra top-level and nested keys parse and are ignored. The frozen shape is not violated by adding them.

| field | JVM source (exact) | units | G1 | generational ZGC | null? |
|---|---|---|---|---|---|
| `ts` | `DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSSSSS'Z'").withZone(UTC)` over `Instant.now()` | — | same | same | no |
| `uptime_seconds` | `RuntimeMXBean.getUptime() / 1000.0` | ms → s | same | same | no |
| `build.language` | literal `"java"` | — | — | — | no |
| `build.runtime_version` | `RuntimeMXBean.getVmVersion()` (== `java.vm.version`, e.g. `25.0.4+7-LTS`) | — | same | same | no |
| `build.service_version` | `SERVICE_VERSION` env | — | — | — | no |
| `build.data_layer` | literal `"jpa"` | — | — | — | no — **and `phases.expected_dl` must learn the value or the run fails loudly** |
| `build.processor_count` | `Runtime.getRuntime().availableProcessors()`, captured **once** at startup | — | same | same | no |
| `config.db_pool_max` | `HikariConfigMXBean.getMaximumPoolSize()` | — | — | — | no |
| `config.db_pool_min` | `HikariConfigMXBean.getMinimumIdle()` | — | — | — | no |
| `config.db_conn_lifetime_seconds` | `HikariConfigMXBean.getMaxLifetime() / 1000` | **ms → s** | — | — | no |
| `config.redis_pool_max` | `REDIS_POOL_MAX` echo (recorded, not enforced — Lettuce multiplexes) | — | — | — | no |
| `config.cache_ttl_seconds`, `log_queue_len` | bound config echo | — | — | — | no |
| `config.max_auto_prepare` | **`null`** (Npgsql-only) | — | — | — | **yes** |
| `config.gogc`, `config.gomemlimit` | **`null`** | — | — | — | **yes** |
| `config.gc_dynamic_adaptation_mode` | **`null`** (.NET-only) | — | — | — | **yes** |
| `gc.collections.gen0` | `GarbageCollectorMXBean("G1 Young Generation").getCollectionCount()` | count | ← | `("ZGC Minor Cycles").getCollectionCount()` | no |
| `gc.collections.gen1` | **literal `0`, both collectors** | count | 0 | 0 | **no — must be 0, never null** |
| `gc.collections.gen2` | `("G1 Old Generation").getCollectionCount()` | count | ← | `("ZGC Major Cycles").getCollectionCount()` | no |
| `gc.pause_total_seconds` | sum `getCollectionTime()/1000.0` over the **"*Pauses"** beans if any exist, else over **all** beans | ms → s | sum of all three beans | **only** `ZGC Minor Pauses` + `ZGC Major Pauses` | no |
| `gc.heap_in_use_bytes` | `MemoryMXBean.getHeapMemoryUsage().getUsed()` | bytes | same | same | no |
| `gc.heap_committed_bytes` | `MemoryMXBean.getHeapMemoryUsage().getCommitted()` | bytes | same | same | no |
| `gc.heap_goal_bytes` | **`null`** | — | null | null | **yes** |
| `gc.allocated_total_bytes` | `((com.sun.management.ThreadMXBean) ManagementFactory.getThreadMXBean()).getTotalThreadAllocatedBytes()` | bytes | same | same | no |
| `db_pool.max` | `HikariConfigMXBean.getMaximumPoolSize()` | — | — | — | no |
| `db_pool.total` | `HikariPoolMXBean.getTotalConnections()` | — | — | — | no |
| `db_pool.idle` | `HikariPoolMXBean.getIdleConnections()` | — | — | — | no |
| `db_pool.in_use` | `HikariPoolMXBean.getActiveConnections()` | — | — | — | no |
| `db_pool.acquire_count` | **`null`** | — | — | — | **yes** |
| `db_pool.empty_acquire_count` | **`null`** | — | — | — | **yes** |
| `db_pool.acquire_wait_total_seconds` | **`null`** | — | — | — | **yes** |
| `redis_pool.max` | `REDIS_POOL_MAX` echo | — | — | — | no |
| `redis_pool.total` | `1` while the shared Lettuce connection is up, else `0` (`RedisConnectionStateListener` on `LettuceConnectionFactory.getNativeClient()`) | — | — | — | no |
| `redis_pool.idle` | `0` | — | — | — | no |
| `redis_pool.in_use` | **`0`** | — | — | — | **no — pydantic `int`; null would drop the sample** |
| `goroutines_or_threadpool` | `ThreadMXBean.getThreadCount()` (live **platform** threads) | — | same | same | no |
| `endpoint_calls.*` | one `LongAdder` per frozen route template | — | — | — | no |

### Extra keys (parse cleanly under `extra="allow"`; carried so a reader can recompute)

| key | source | why |
|---|---|---|
| `gc.collections.concurrent_cycles` | G1: `("G1 Concurrent GC").getCollectionCount() / 2`; ZGC: omit | G1's concurrent-cycle bean fires **twice per cycle** (Remark + Cleanup). Keeps it out of `gen1` and out of `GcCollections.total()`. |
| `db_pool.threads_awaiting` | `HikariPoolMXBean.getThreadsAwaitingConnection()` | **The saturation signal under virtual threads.** Servlet-thread-pool exhaustion no longer exists as a symptom; saturation appears as requests waiting for a connection. This is a *gauge* — zero per-request cost. |
| `vthread_scheduler.{parallelism,pool_size,mounted,queued}` | `jdk.management.VirtualThreadSchedulerMXBean` via `ManagementFactory.getPlatformMXBean(...)` (module `jdk.management`, @since 24, ObjectName `jdk.management:type=VirtualThreadScheduler`) | Proves `parallelism == cpuset width` from the artifact. All four are `ForkJoinPool` reads: no native call, no safepoint, no allocation. |
| `config.{prepare_threshold, max_ram_percentage, max_heap_size_bytes, soft_max_heap_size_bytes, gc_engine, active_processor_count, use_compact_object_headers, open_in_view, hikaricp_version, tomcat_max_connections, tomcat_max_keep_alive_requests}` | `HotSpotDiagnosticMXBean.getVMOption(name)` (value + **origin**) and bound config | the effective-config echo; `origin` is what makes "this is a default" evidence rather than assertion |

### Decisions that override the companion doc, with reasons

1. **`gc.collections.gen1 = 0`, both collectors.** `registry.py`'s `JVM_GC.collections_note` currently declares `"G1 Concurrent GC" -> gen1` and `ZGC ... gen1 null`. Both are wrong in ways that bite: `gen1: int` is non-optional, so a **ZGC cell reporting null would fail `model_validate` on every sample** and `StatsPoller` would silently drop them, degrading the GC-steady gate; and under G1, `gen1` would carry 2 × the concurrent-cycle count and flow into `GcCollections.total()`, which is the warmup cadence series. Zero-filling is the pre-registered Go precedent ("gen0=gen1=0; Go has one generation; documented"). **Update `registry.JVM_GC.collections_note` accordingly.**

2. **`goroutines_or_threadpool = ThreadMXBean.getThreadCount()`**, not `getMountedVirtualThreadCount()`. `getMountedVirtualThreadCount()` is a sampled `ForkJoinPool.getActiveThreadCount()` estimate bounded above by parallelism, so it reads 0–6 at every rate and carries almost no signal next to .NET's `dotnet.thread_pool.thread.count`, which grows. Platform-thread count is the like-for-like "OS threads the runtime holds". The counter-argument (mounted is closer in spirit to `NumGoroutine`) is real, which is why the **full scheduler block is carried as an extra** — either number is recomputable from the artifacts. Document both as estimates; the .NET analog is a sampled gauge too, so the asymmetry is symmetric.

3. **`gc.heap_goal_bytes = null` in *both* GC cells**, with `SoftMaxHeapSize` echoed in `config`. The companion suggests reporting `SoftMaxHeapSize` under ZGC. Three reasons not to: (a) it is a *static* `MANAGEABLE` flag, not a per-cycle target, so it is not the analog of Go's `/gc/heap/goal:bytes`; (b) making the field's semantics change between cells of the *same language* is worse than a stable null; (c) decisively — `zArguments.cpp` sets `SoftMaxHeapSize = MaxHeapSize * 90/100` **only when none of `MaxHeapSize`, `MaxRAMPercentage` or `SoftMaxHeapSize` was set on the command line**. The `heap-equalized` cell sets `-XX:MaxRAMPercentage=75`, so `SoftMaxHeapSize` collapses to `MaxHeapSize` in exactly the cell where the field would have been interesting. .NET already reports null here.

4. **`db_pool.acquire_*` stay null**, matching .NET's three nulls. HikariCP exposes them only via the `MetricsTrackerFactory` / `IMetricsTracker` SPI, i.e. per-checkout work on the Java side only — and the headline metric is CPU-ms per request. `getThreadsAwaitingConnection()` is a zero-cost gauge and gives the queueing signal that actually matters. (`empty_acquire_count` has no HikariCP analog at all.)

### Two gate-driven implementation requirements

- `warmup.py` requires `db_pool.total >= db_pool.max` **and** `redis_pool.total >= 1`. Lettuce's `eagerInitialization` defaults to false, so the shared connection opens on first command. The Java `/readyz` must PING Redis exactly as `services/dotnet/Endpoints/HealthEndpoints.cs` does (DB first, Redis only if the DB is reachable) — that both mirrors the .NET readiness semantics and guarantees the connection is open before the gate is polled.
- Add a **container-detection assertion at startup**, in the service, failing fast: `availableProcessors() == SUT_CORES` and `MaxHeapSize == 25% of the cgroup memory.max`. This is not paranoia. Measured on this host under `docker run -m 12g --cpuset-cpus 0-5`: **JDK 21.0.5 reports `Runtime.maxMemory()` = 15,848 MB (25% of the 61 GiB *host*), JDK 25 reports 3,072 MB (25% of the container limit).** Kernel ≥6.12 stopped listing `cpuset` in `/proc/cgroups`, which switched container support off entirely on pre-fix JVMs (JDK-8347811, `/sys/fs/cgroup/cgroup.controllers`; backported to 21.0.10 / 17.0.20 / 11.0.32). This host runs kernel 7.1.8. The failure mode is **silent**, and it invalidates the cell. An assertion turns it into a hard stop.

---

## 5. SQL-shape risk register

All shapes verified against real PostgreSQL 18.4 with `log_statement='all'`; entities model FKs as scalar `Long`, no `@ManyToOne`/`@OneToMany` anywhere (mirrors `LedgerDbContext`, which uses `HasOne<Customer>().WithMany()` with no navigation).

| # | risk | Hibernate 7.4.5 emits | frozen spec | idiomatic fix | production-normal or contortion | disposition |
|---|---|---|---|---|---|---|
| **S1** | **IDENTITY disables JDBC insert batching** | N separate prepares/executes for N items | `ONE batched round-trip [Q5 ×N + Q6]`, "ASSERTED EQUAL at integration" | none inside JPA. `InsertCoordinatorStandard`: `batchKey = isIdentifierAssignedByInsert() \|\| hasInsertGeneratedProperties() ? null : new BasicBatchKey(...)` — "disable batching in case of insert-generated identifier or properties". `StatelessSession.insertMultiple()` does not rescue it; `reWriteBatchedInserts` is gated on `!isRETURNINGkeywordPresent` so it can never fire either | — | **Accept and disclose.** Rewrite the round-trip ledger in `spec/db-access.md` to per-stack counts and add the FAIRNESS row. Java pays +N−1 round trips per `POST /invoices` (3–8 items, 15% of the mix) where EF pays 1. Dropping to `JdbcTemplate.batchUpdate` would rebuild round 1's ORM-vs-hand-written asymmetry — rejected. |
| **S2** | Q5 returns 7 columns, not 1 | `insert into invoice_items (…) values ($1..$6)` **+ newline + `RETURNING *`** (PgJDBC `getGeneratedKeys`) | `RETURNING id` | `hibernate.jdbc.use_get_generated_keys=false` → `InsertReturningDelegate` → `… returning id` | **contortion** (non-default, no team sets it) | Set it, **disclose in FAIRNESS** with direction (helps Java), and measure the default variant once at rehearsal. |
| **S3** | IDENTITY forces an **immediate** insert at `persist()` | `ActionQueue.addResolvedEntityInsertAction`: `if (insert.isEarlyInsert()) { executeInserts(); execute(insert); }` — logged `executingIdentityInsertImmediately` | — | compute `invoice.totalMinor` **before** `persist(invoice)` | production-normal (discipline) | Any field mutated after `persist()` emits an extra full-column `UPDATE` at flush that also rewrites `created_at`. **Invisible in code review; must be a test.** |
| **S4** | Q3 pagination text | `… order by i1_0.id desc offset $2 rows fetch first $3 rows only` | `LIMIT 20 OFFSET $2` | none. `PostgreSQLDialect.getLimitHandler()` returns `OffsetFetchLimitHandler`; both bounds are parameterized; `Limit.of(20)` only changes it to `fetch first $2 rows only` | a custom `Dialect` subclass would be a **contortion** and still would not reach the literal | **Third pinned deviation.** Disclose alongside EF's two. Plan-identical (pure backward index range scan on `idx_invoices_customer_id_id_desc`, no sort node) — assert with `EXPLAIN`. |
| **S5** | Q3 sort duplication | `findByCustomerIdOrderByIdDesc(id, PageRequest.of(n,20,Sort.by(DESC,"id")))` → `order by i1_0.id desc,i1_0.id desc` | — | use method-name ordering **or** `Sort`, never both | production-normal | Test-asserted. Harmless to the plan, ugly in `pg_stat_statements`. |
| **S6** | `Page<T>` issues a hidden second query | `select count(i1_0.id) from invoices i1_0 where i1_0.customer_id=$1` | one statement | return `List<T>` or `Slice<T>` | production-normal | `Slice`/`List` emit one statement; `Page` emits two. |
| **S7** | Q3b/Q7 existence probe | `existsById` → `select count(*) from customers c1_0 where c1_0.id=$1`, wrapped in `BEGIN READ ONLY` / `COMMIT` (three wire statements) | `SELECT EXISTS(SELECT 1 FROM customers WHERE id = $1)` | `@Query(value = "SELECT EXISTS(SELECT 1 FROM customers WHERE id = ?1)", nativeQuery = true)`, **on one line** | one-line native existence probes are ordinary production Java | Native query is the only path to the spec shape. `ParameterRecognizerImpl` copies native SQL character-for-character (`?1`→`?`), PgJDBC rewrites `?`→`$1`, so the wire text is **byte-identical**. Declared `@Query` methods are **not transactional by default**, so no BEGIN/COMMIT — matching EF's `AnyAsync`. |
| **S8** | `@Transactional(readOnly=true)` reaches the wire | `BEGIN READ ONLY` … `COMMIT` (COMMIT is its own round-trip) | EF read path sends neither | **do not annotate the read endpoints**; leave declared `@Query` methods untransacted | production-normal (parity with .NET) | Corrects companion FINDING 3. If the annotation is wanted for idiomatic reasons, `readOnlyMode=ignore` in the JDBC URL restores a plain `BEGIN` — but that is a non-default driver knob. With DTO projections there are no managed entities, so `FlushMode.MANUAL` / `setDefaultReadOnly` buy nothing. |
| **S9** | Q1/Q2 column order | entity query emits id-then-**alphabetical** | spec column order | HQL constructor-expression (DTO) query: `select new InvoiceHeader(i.id, i.customerId, i.status, i.currency, i.totalMinor, i.createdAt) from Invoice i where i.id = :id` | production-normal — the direct analog of `.AsNoTracking().Select(...)` | Also removes managed entities → no dirty-check snapshots, no auto-flush candidates. |
| **S10** | Q4 `created_at` read-back | `insert into invoices (currency,customer_id,status,total_minor) values ($1..$4) returning id,created_at` | `RETURNING id, created_at` | `@Generated(event = EventType.INSERT) @Column(name="created_at", updatable = false)` | production-normal (textbook Hibernate 7; analog of EF's `ValueGeneratedOnAdd().HasDefaultValueSql("now()")`) | **`updatable=false` is load-bearing**: `@Generated(event=INSERT)` alone leaves the column writable on UPDATE, so any later dirty update overwrites the DB timestamp. |
| **S11** | Q6 atomic increment | JPQL → `update customers c1_0 set balance_minor=(c1_0.balance_minor+$1) where c1_0.id=$2` (aliased, parenthesised, reversed params); native → byte-identical to spec | `balance_minor = balance_minor + $2 WHERE id = $1` | `@Modifying @Query(..., nativeQuery = true)`; leave `flushAutomatically`/`clearAutomatically` at their `false` defaults | production-normal | Both forms are true atomic increments (no read-modify-write), so both satisfy the Zipfian-contention requirement. `flushAutomatically=true` would inject a flush before the update and change the ledger. |
| **S12** | Auto-flush injecting statements | `DefaultAutoFlushEventListener` flushes before a query when query spaces are non-empty and the context holds a managed entity | — | DTO projections keep the persistence context empty on read paths | production-normal | The mechanism behind "unexplained extra INSERT/UPDATE mid-transaction". |
| **S13** | Bootstrap metadata queries | `SHOW TRANSACTION ISOLATION LEVEL` ×2, `select version()`, `current_schema()`, `current_catalog`, a `pg_settings` lookup, a large `pg_get_keywords()` `string_agg`, `select * from information_schema.sequences` | — | `hibernate.boot.allow_jdbc_metadata_access=false` + explicit dialect | only worth it if startup is measured | **Startup-only.** Relevant to the cold-start curve (which is in scope), irrelevant to steady state. Do not set it otherwise — an unused knob is over-declaration. |
| **S14** | pgjdbc `LruCache` monitor | `LruCache.borrow()` holds a `synchronized (cache)` monitor across `createAction.create(key)`, which does I/O (`getStandardConformingStrings`) — still present in 42.7.13 | — | none needed | — | On JDK 25 monitors do not pin (JEP 491), so this is a **serialization** effect on prepared-statement-cache creation, not carrier starvation. Warmup-weighted, no EF analog. Note in FAIRNESS as a known non-issue rather than let it be discovered. |

**Statement-shape assertion strategy.** Use `org.hibernate.resource.jdbc.spi.StatementInspector` registered as a **bean instance** via `org.springframework.boot.hibernate.autoconfigure.HibernatePropertiesCustomizer` (the package moved in Boot 4; artifact `spring-boot-hibernate`). It receives exactly the string handed to `Connection.prepareStatement`. Do **not** use `hibernate-testing`'s `SQLStatementInspector` (drags JUnit 4 + hamcrest into a JUnit 6 project), `show-sql` (formatted, unassertable), p6spy (last released 2020) or datasource-proxy (adds a `DataSource` wrapper you would then have to keep out of the measured run).

**And pair it with a wire ledger.** Every finding that mattered — S2, S3, S6, S7, S8 — was invisible to the inspector and visible only in the PostgreSQL log. P5's "round-trip counts per endpoint, both stacks" must come from `log_statement='all'`.

---

## 6. Fairness asymmetries

Round 1 shipped four undisclosed asymmetries. This list is deliberately long; **direction named on every row.**

### Data layer and driver

| # | axis | Java | .NET | costs | treatment |
|---|---|---|---|---|---|
| A1 | **Item-insert round trips** | Hibernate cannot batch IDENTITY inserts: N round trips for N items | EF batches N items in one round trip | **Java**, structurally, on 15% of the mix | Disclose; rewrite the `spec/db-access.md` ledger to per-stack counts. This is what the ORM costs. |
| A2 | Server-side prepare | PgJDBC `prepareThreshold=5` (**driver default**) | Npgsql `Max Auto Prepare=20`, `Min Usages=5` (**explicitly set**; the default is 0/off) | neither, by construction | D4 already resolves this and is **correct**: verified `PREPARE_THRESHOLD("prepareThreshold","5")` in `PGProperty.java`. Both reach a named statement on the 5th execution per connection. Keep the default-vs-default contrast as a declared variant cell. |
| A3 | Statement-cache size | 256 statements/connection (`preparedStatementCacheQueries`) | 20 (`Max Auto Prepare`) | neither | The workload has <10 distinct statement texts. Add a test asserting the working-set size so the 256-vs-20 optic is a documented non-issue rather than a suspicion. |
| A4 | **Binary result formats** | PgJDBC sends binary **parameters** from execution 1, but binary **results** only once the statement is named — `PgStatement`: "only named statements can be transferred in binary format"; `sendBind` emits result-format codes only when `query.getFields() != null`. So executions 1–4 of each statement on each connection return **text**. `varchar`/`text` scalars are never in the binary set at all. | Npgsql binary throughout | Java, warmup-only | `spec/db-access.md` says "Both drivers use the extended query protocol with binary parameter/result formats by default (asserted)". **Soften to distinguish parameters from results and make the assertion steady-state.** Assert it after warmup from `pg_prepared_statements` (`from_sql=false`, `generic_plans > 0`). Window is ~24 conns × 8 statements × 4 executions ≈ 768 requests. |
| A5 | Plan-cache observability | — | — | — | `pg_stat_statements` folds prepared and unprepared execution of the same SQL into one normalized entry; `calls` increments either way and `track_planning` **defaults off**, so prepared-ness is invisible by default. Prove it via `pg_prepared_statements` (Java: `S_<n>`; Npgsql: `_auto<n>` / `_p<n>`) or Npgsql's `db.client.operation.npgsql.prepared_ratio` gauge. |
| A6 | Pagination SQL text | `offset $2 rows fetch first $3 rows only` | `LIMIT $2 OFFSET $3` | neither (plan-identical) | Third pinned deviation. Do **not** write a bespoke `Dialect` to make one side look like the reference. |
| A7 | Existence probe | native `SELECT EXISTS(...)` | EF `AnyAsync` → same shape from LINQ | neither | Java needs a native query to reach parity that EF gets free. That is what it costs Hibernate; disclose. |
| A8 | `savepoint` behaviour | PgJDBC `autosave=never` (default) | `Database.AutoSavepointsEnabled = false` (explicit) | neither | Free parity; note it. |
| A9 | `/readyz` model build | Java `/readyz` touches the JPA path | .NET `/readyz` takes `LedgerDbContext`, so the EF model is built in every cell | symmetric once mirrored | Mirror deliberately so the existing FAIRNESS row stays accurate for round 2. |

### Runtime and memory

| # | axis | Java | .NET | costs | treatment |
|---|---|---|---|---|---|
| A10 | **Heap ceiling default** | `MaxRAMPercentage=25` → 3 GiB of 12 | CLR hard limit default 75% → 9 GiB (measured: `GCHeapHardLimit = 9,663,676,416`) | Java on CPU, .NET on RSS | D6, decided at G6. **Qualify the 3× when it is written down:** these ceilings do not cover the same thing (.NET's bounds the GC heap + bookkeeping; `-Xmx` bounds the Java heap only, excluding metaspace, code cache, stacks, direct buffers) and neither bounds process RSS. Both are ceilings, not targets. |
| A11 | Second .NET memory threshold | none | high-memory-load threshold defaults to 90% of the container limit (measured 11,596,411,699 = 90% of 12 GiB) | — | A softer .NET-side ceiling with no JVM analogue. Worth one line next to A10. |
| A12 | **Heap adaptivity** | G1 `G1PeriodicGCInterval=0` — **never uncommits while idle**; measured committed heap unchanged across a full 300 s idle tail in every G1 run | DATAS on by default (measured `GCDynamicAdaptationMode = 1`), sizes heap toward live data | direction depends on the sample point | The more interesting axis than A10. Also: G1 sizes committed heap to a **GC-CPU target** (`GCTimeRatio=12`, ~8%), *not* to live set — measured 740 MB committed at ~100 MB/s allocation, 1,004 MB at ~400 MB/s, and the full ceiling under saturation, all at a ~200 MB live set. **D6's question is therefore "what is Ledgerline's steady-state allocation rate", not "what is its live set".** |
| A13 | ZGC uncommit | `ZUncommit=true`, `ZUncommitDelay=300 s` | DATAS | — | If a ZGC cell runs, define exactly when in the probe RSS is sampled relative to the last idle gap, and publish committed-heap alongside RSS. Otherwise the ratio measures uncommit policy. |
| A14 | **THP / huge pages** | Oracle's JDK 25 GC guide warns verbatim: distros that set `transparent_hugepage/enabled=always` while leaving `shmem_enabled=never` give huge pages to **every collector except ZGC**. Netflix reports a significant CPU drop from flipping `shmem_enabled` to `advise`. | — | ZGC cell only, potentially large | **Record `/sys/kernel/mm/transparent_hugepage/{enabled,shmem_enabled}` in `host_state` and state the value in FAIRNESS.** A ZGC cell on `shmem_enabled=never` measures the kernel config, not the collector. This is benchmark-*invalidating*, not merely gaming-adjacent. |
| A15 | **Virtual-thread stack memory** | stacks are `StackChunk` objects **on the Java heap**, growing/shrinking with call depth; they add to heap occupancy and GC allocation rate | thread-pool threads hold native stacks | **Java**, in the heap/GC direction | The popular claim "virtual threads save ~200 MB of 1 MB stacks" is wrong as an RSS claim: `-Xss` 1024 KB is *reserved address space*; measured on this host, 218 threads showed `stack: reserved=223,244 KB, committed=21,644 KB` (~99 KB/thread) against a 67 MB process RSS. Report the heap/native split from cgroup v2 + GC logs; do not carry the 200 MB figure. |
| A16 | **No time-slicing** | the virtual-thread scheduler does **not** preempt on CPU consumption (JEP 444: "The scheduler does not currently implement time sharing"). Six CPU-heavy handlers occupy all six carriers with no fairness; everything else waits in the FJP queue. | .NET's ThreadPool has hill-climbing plus starvation detection that injects threads | **Java**, on p99 specifically | Sits directly on the headline metric (rate at which p99 crosses 20 ms). **Do not "fix" it by raising `jdk.virtualThreadScheduler.parallelism`** — that is gaming. Disclose as a structural property of the concurrency model. |
| A17 | JVM-internal threads in the cgroup | ParallelGCThreads 6, ConcGCThreads 2, CICompilerCount 3 (1×C1 + 2×C2) all run inside the SUT cgroup and land in the measured CPU-ms | .NET's GC + tiered-JIT background threads, a different count | neither, but different in kind | Correct accounting on both sides — name it, since the counts differ. |
| A18 | **Globalization data** | full CLDR/ICU in the Temurin image | `runtime-deps:10.0-noble-chiseled` sets `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=true` ("ICU package isn't included"), and since .NET 9 the env var **outranks** the csproj setting | **Java**, on RSS | Real and currently undisclosed. The service does no locale-sensitive formatting (money is `long`, timestamps use a fixed pattern), so it is a footprint asymmetry, not a semantic one. Either disclose with the measured delta, or move .NET to `-noble-chiseled-extra` (ships ICU + tzdata, does not set the invariant flag) for parity. |
| A19 | Image shape | full Ubuntu userland (`eclipse-temurin:25-jre-noble`) | chiseled (no shell, no package manager) | — | Cosmetic for measurement, but it is a real difference in what is resident. `gcr.io/distroless/java25-debian13` is the closest analog if structural parity is wanted — at the cost of exact control over the Temurin build string. Pick one and say which. |
| A20 | Compilation warm-up shape | tiered C1/C2, no AOT cache, no CDS | tiered JIT + dynamic PGO, no R2R, no Native AOT | symmetric by choice | State the JDK 25 default-vs-docs discrepancy pre-emptively: `TC_QuickJitForLoops` **defaults to 1** on x64 in the runtime source (OSR present) even though MS Learn documents it as off — so a reviewer reading Learn cannot claim .NET left loops in tier-0. |

### Client and server surface

| # | axis | Java | .NET | costs | treatment |
|---|---|---|---|---|---|
| A21 | **HikariCP 7.0.2 yield-spin** | Boot 4.1.1 pins **7.0.2**, which burns carrier CPU in a `Thread.yield()` spin in `ConcurrentBag.requite()`/`unreserve()` under virtual-thread load. Upstream #2398 reports **exactly this topology**: 6 carriers on a 6-core limit, Spring MVC + virtual threads + Hibernate, one borrow/return per request, all six carriers at ~87% CPU inside the spin. Fixed in **7.1.0** (PR #2402, merged 2026-06-11, released 2026-06-14) by routing virtual threads to `parkNanos` instead of `yield`; author's harness on JDK 25.0.3, 2000 virtual workers / 50 bag entries / 20 s: 6.66M → 11.63M borrows/s. Boot 4.2.0-M1 moves to 7.1.0. | n/a | **Java**, directly on the reported CPU-ms | **Highest-leverage finding in this document.** Every request returns a connection through `HikariPool.recycle` → `ConcurrentBag.requite`. **Recommendation: run it as a `declared-variant-axis` at rehearsal (7.0.2 vs 7.1.0) and publish the delta**, then pin 7.1.0 for the headline with the override disclosed. Do not silently ship 7.0.2 — a reviewer who knows #2398 reads the Java CPU number as an artifact. Do not silently ship 7.1.0 either — it is an override of the framework-managed version. |
| A22 | **Redis command timeout** | Lettuce default **60 s** | SE.Redis `SyncTimeout` **5 s** (left at default in `Program.cs`) | Java, past the knee | Equalize to 5 s and label it an equalization toward .NET's default (strictly less permissive for Java). Also note Lettuce's request queue is **unbounded** by default (`DEFAULT_REQUEST_QUEUE_SIZE = Integer.MAX_VALUE`) and `DisconnectedBehavior.DEFAULT` queues commands while disconnected — both defaults, both left alone, both disclosed. |
| A23 | Redis client model | Lettuce shared multiplexed connection (default) | SE.Redis `ConnectionMultiplexer` | neither | **This removes a round-1 asymmetry.** `spec/cache.md`'s "Asymmetry (kept on purpose): go-redis uses a connection pool; StackExchange.Redis uses a single multiplexed connection" describes the *Go* comparison and is **now wrong for round 2** — rewrite it. |
| A24 | Redis retry | Lettuce does not retry commands | SE.Redis never retries commands | neither | Also removes a round-1 asymmetry (go-redis retried 3×). Update the FAIRNESS "Redis client resilience" row. |
| A25 | **Redis write command on the wire** | Spring Data `opsForValue().set(k, v, Expiration.seconds(300))` → `LettuceConverters.applySetExpiration` → `SetArgs.ex(300)` → **`SET k v EX 300`** | SE.Redis `StringSetAsync(key, value, ttl)` has an explicit branch — `if (when is When.Always & expiry.IsRelative) { … RedisCommand.SETEX … }` → **`SETEX k 300 v`** | neither (both O(1), one RTT) | `spec/cache.md` documents `SET … EX` for both, and **.NET already does not match that text**. Amend the spec to name both forms, and make sure any Redis-side per-command accounting sums `cmdstat_set` **and** `cmdstat_setex`, or one side will read as issuing zero writes. Forcing Java onto `setEx()` for byte parity would be an unidiomatic contortion (redis.io marks SETEX deprecated since 2.6.12). |
| A26 | **HTTP keep-alive ceiling** | Tomcat `maxKeepAliveRequests` default **100** | Kestrel has no per-connection request cap | **Java**, growing with rate | Set `server.tomcat.max-keep-alive-requests=-1` as a `set-identically-to-.NET` equalization and assert it. Otherwise Java pays a connection re-establishment every 100 requests. |
| A27 | Connection admission ceiling | Tomcat `max-connections` 8192 + `accept-count` 100 | Kestrel `MaxConcurrentConnections` unset (unlimited) | Java, at the ladder walls only | Cannot bind at the knee; disclose. Do **not** raise it above the default to chase throughput. |
| A28 | Body-limit enforcement layer | app-level filter (no Tomcat JSON body cap exists) | Kestrel, below the app | neither observably, but different layers | Disclose (see §3). The frozen 413 contract — access line only, no `request_error` — must be reproduced from app code that could have logged one. |
| A29 | JSON key order | Jackson 3 flips `MapperFeature.SORT_PROPERTIES_ALPHABETICALLY` to **true** (verified in `jackson-databind` 3.1.5 source) | System.Text.Json source-gen emits declaration order | **neither** | **No action needed** — and this is worth stating explicitly because it looks alarming. `bench/validation/normalize.py` rule **R1** already sorts object keys recursively ("JSON object key order is insignificant on both wires"), so the equivalence suite is order-insensitive by construction. Adding `@JsonPropertyOrder` would be pointless churn. Arrays remain order-significant (R2) and are unaffected. |
| A30 | JSON serializer | Jackson 3 (Boot default, reflection-based, no Blackbird) | System.Text.Json source-generated context | **Java** | Carry forward the existing FAIRNESS row verbatim; it applies unchanged to Jackson. Pre-registered as a disclosure, not a tuning knob. |
| A31 | Diagnostics posture | no actuator, no Micrometer, no JFR, no GC log, no NMT, no JVMTI agent | no OTel exporter, no EventPipe session, on-demand `MeterListener` | symmetric by construction | Assert the absences in a classpath/flags test. This row exists so the absences read as a matched pair rather than as a Java-only subtraction. |

---

## 7. Open questions

| # | question | decided by | when |
|---|---|---|---|
| Q1 | **HikariCP 7.0.2 vs 7.1.0.** Does the `ConcurrentBag` yield-spin measurably move CPU-ms per request at 24 connections and 6 carriers, or is #2398's pathology specific to a 50-connection pool under extreme contention? | rehearsal-tier A/B measurement | **G6** |
| Q2 | **D6, restated.** Where does `gc.heap_committed_bytes` settle at the knee rate? G1 sizes to a GC-CPU target, not live set; measured 740 MB at ~100 MB/s allocation and the full ceiling under saturation. If Ledgerline allocates in the 100–600 MB/s band the 3 GiB ceiling never binds and D6 collapses to one cell. | measurement | **G6** (already the plan; this only sharpens what to look at) |
| Q3 | **Does `"G1 Concurrent GC"` report STW sub-pause time (Remark + Cleanup) or concurrent wall time?** One measurement says STW (3 forced cycles → count 6, timeMs 1, matching logged Remark 0.473+0.488+0.365 and Cleanup 0.004+0.003+0.003 ms); one researcher asserts concurrent wall time. Decides whether `pause_total_seconds` sums all three G1 beans or excludes it. Pre-registered default: **sum all three** (backed by the measurement). | decisive test: force N concurrent cycles, assert `count == 2N` and `time ≈ Σ(Remark+Cleanup)` from `-Xlog:gc` | **P4**, before the spec freeze |
| Q4 | **`use_get_generated_keys=false` — set it, or accept `RETURNING *`?** Shape parity vs default purity. | measure both once at rehearsal; then P7 review | **G6 / G7** |
| Q5 | **Does the JVM stay one patch behind?** Container `25.0.3+9` vs host `25.0.4+7`; `25.0.4.1+1` has no linux glibc asset. Will Docker Hub jump to 25.0.4 or straight to 25.0.4.1? | re-resolve at P4 Dockerfile freeze; digest-pin and assert `java -version` at container start | **P4** |
| Q6 | **Does Jackson 3's `SORT_CREATOR_PROPERTIES_FIRST` interact with `SORT_PROPERTIES_ALPHABETICALLY` for records?** Moot for equivalence (A29), but decides the emitted byte order and therefore what the cache stores. | one golden-vector byte assertion | **P4** (no spec impact) |
| Q7 | **Boot 4.1's `@RedisListener` auto-configuration** — does the default `RedisMessageListenerContainer` get registered even with no listener methods? If so it could open a second Redis connection and a listener thread with no .NET counterpart. | read the condition, or `CLIENT LIST` at runtime | **P4** |
| Q8 | **Micrometer/binder cost is moot only if actuator stays out.** If any starter later drags in `micrometer-observation`, Java silently starts paying an Observation per request and per Redis command. | classpath assertion test (implement it, or do not claim it) | **P4** |
| Q9 | **JVMTI guard: `-XX:-EnableDynamicAgentLoading` flag, or agent-list assertion?** The flag is a fourth-category item; the assertion is not. | P7 adversarial reviewer | **G7** |
| Q10 | **Globalization parity (A18):** disclose Java's ICU/CLDR footprint, or move .NET to `-noble-chiseled-extra`? | measure the RSS delta once, then decide | **G6 / G7** |
| Q11 | **StackExchange.Redis 3.1.13 → 3.1.31?** D8 says latest stable at run start; the 3.1.x line moved five times in three weeks. | Haik | **before P7** |
| Q12 | **`goroutines_or_threadpool`: platform-thread count (this doc) or mounted virtual threads (companion doc)?** Both are carried in the artifact either way. | P7 reviewer, or accept this doc's recommendation | **P7** |
| Q13 | **Version-pin rule.** Boot 4.1.1 was published **one day** before this research. A hostile reviewer attacks either "you handicapped Java with an old version" or "you used a day-old patch". The answer is a **symmetric, pre-registered rule** — e.g. "both stacks pin the newest GA patch published at least N days before the freeze" — applied identically to .NET's servicing band, written into FAIRNESS *before* the freeze. | Haik | **before P7** |
| Q14 | **Filename.** Settled: this document is `docs/research/2026-08-21-java-stack.md`, the name its companion `2026-08-21-measured.md` uses. | n/a | done |
| Q15 | **Unmeasured by anyone:** how much does a virtual thread's `StackChunk` allocation add to GC allocation rate for a real Spring MVC + Hibernate request? Every public figure is a toy microbenchmark. | this repo's own `gc.allocated_total_bytes` per window | **G6** — do not carry a number in from the web |

### Explicitly out of scope, recorded so it is a decision and not an oversight

Generational Shenandoah (product in 25 via JEP 521, but Shenandoah still defaults to single-generation `satb`; default-on targets JDK 28) — no pre-registration, would be a third GC cell. JFR CPU-time profiling (JEP 509, **experimental**, Linux-only) — diagnostic only; the pre-registered CPU ground truth is cgroup v2, which is runtime-neutral. CRaC — Temurin ships no CRaC binaries (D1) and it requires `CAP_CHECKPOINT_RESTORE`. Compiled-model-style EF machinery has no Hibernate analog worth adding.

### Do not move off JDK 25 mid-round

JDK 26 changes G1's default GC CPU goal from 8% to 4% (JDK-8247843) and adds a second card table costing ~2 MB of native memory per 1 GiB of heap (JEP 522). JDK 27 flips `MinHeapFreeRatio`/`MaxHeapFreeRatio` from 40/70 to 0/100 (JDK-8238686) and makes compact object headers the default (JEP 534). Every one of those moves the committed-heap number this benchmark reports. Pin the exact Temurin build string in `manifest.identity` and keep it for all three D10 runs.