# services/dotnet: implementation notes & decisions

The .NET SUT for Ledgerline. spec/ is the single source of truth; decisions below resolve
ambiguities toward the LOWEST cross-language divergence risk.

## Build shape

- `dotnet publish -c Release` produces `Ledgerline.dll`; the test project and xunit are
  excluded from the publish output by the csproj's `Compile Remove`.
- No committed EF compiled model, no STJ source-gen context: stock EF builds the model at
  startup and System.Text.Json resolves contracts by reflection (see "Canonicality").

## Builder choice

`WebApplication.CreateBuilder` — the template default — with the full validation source-gen +
ProblemDetails pipeline. `CreateSlimBuilder` is not used (see "Canonicality"); the frozen
stdout contract holds because `Program.cs` clears the default logging providers and registers
only the custom formatter.

## JSON casing: snake_case, NOT camelCase

spec/openapi.yaml freezes snake_case property names ("JSON property names: snake_case
exactly as written here, both sides"). Every wire DTO
carries explicit `[JsonPropertyName("snake_case")]`, which overrides any naming policy, so
ASP.NET's Web-default options (camelCase policy, case-insensitive read, number-from-string)
still emit the exact frozen names. Verified on the wire:
the /pricing/quote 200 body emits `subtotal_minor`, `category_surcharge_minor`, etc.

## Canonicality (2026-08-27)

Each SUT is what an ordinary production team ships with the default project template and
mainstream practice — no benchmark-motivated optimizations. The Java side already follows
this rule (its Dockerfile refuses Spring Boot's CDS/AOT container guidance so both stacks
run JIT-from-cold). Removed from this service under it, each previously disclosed in
docs/FAIRNESS.md as a non-default:

- the committed EF compiled model (`dotnet ef dbcontext optimize` output + `.UseModel`),
  with its design-time factory, the `Microsoft.EntityFrameworkCore.Design` package and the
  `dotnet-ef` tool manifest;
- the STJ source-gen context (`LedgerJsonContext`) — serialization is now reflection-based
  at the template's Web defaults, wire bytes unchanged (`[JsonPropertyName]` carries the
  names);
- `WebApplication.CreateSlimBuilder` (now `CreateBuilder`);
- `<InvariantGlobalization>` and the chiseled runtime image (now the standard aspnet
  image with ICU, matching the Java side's full JRE).

Non-defaults that remain are wire/behaviour parity or disclosed exceptions, each with a
docs/FAIRNESS.md row: `AddDbContextPool`, `Max Auto Prepare=20`, `AutoSavepointsEnabled=false`,
`AddServerHeader=false`, the Kestrel timeout pair (set 1:1 with application.yaml), the
logging sink settings (spec/logging.md), `ThrowOnBadRequest`, `AbortOnConnectFail=false`,
and the `UNLINK`-less Redis command map.

## Validation message braces

DataAnnotations runs `string.Format(ErrorMessage, displayName)` on the message. The frozen messages
`must match ^[A-Z]{3}$` and `must match ^[A-Z]{2}-[0-9]{4}$` contain `{N}` sequences that
string.Format treats as positional placeholders → it threw an "Index (zero based)…" message instead
of the frozen text. Fix: brace-double in the C# attribute (`^[A-Z]{{3}}$`) so the EMITTED wire text
is the literal frozen `^[A-Z]{3}$`. Confirmed by WebApplicationFactory capture. The Java side
emits the un-doubled literal (Jakarta validation does not string.Format its messages), so its
source is the bare frozen string.

## Id path-param parsing (400 vs 404)

spec: "Non-positive or non-integer → 400; well-formed but absent → 404." A route constraint
`{id:long}` would 404 on non-integer (route miss), violating the spec. So `{id}` is bound as a
string and parsed manually (`InvoiceEndpoints.TryParseId`): parse-fail OR `< 1` → 400 validation
problem with errors map `{"id":["must be a positive integer"]}`; valid → proceed (404 if absent).
This is the lowest-divergence choice (the Java side binds `{id}` as a String and parses it with
the same grammar in `RequestParams.parseId`).

## 400-before-404 ordering

For POST /invoices the body is validated by AddValidation (400) before the handler runs the customer
existence probe (404). For GET/pdf the id is parsed (400) before the DB/cache lookup (404). Ordering
holds structurally.

## Create-path round-trip ledger (EF + raw ADO.NET)

- EF (headline efcore): Q7 existence via AnyAsync (EXISTS, outside the tx on both stacks) →
  build Invoice+Items graph → explicit tx (DB-default READ COMMITTED, auto-savepoints off) →
  ONE SaveChangesAsync (header RETURNING id+created_at + N items batched) → Q6 balance via
  ExecuteUpdateAsync (ATOMIC increment) → commit. A later revision replaced the original
  tracked read-modify-write balance: it wrote an absolute value and lost concurrent updates on
  hot customers (Hibernate's native Q6 and the ADO variant increment in place; the seeder
  invariant only survives the increment form). Cost disclosed in spec/db-access.md: EF cannot
  fold Q6 into the item batch, so the EF create carries one extra round-trip vs NpgsqlBatch.
- Raw ADO.NET variant: Q7 existence, then explicit READ COMMITTED tx: Q4 header (RETURNING id), then a
  second NpgsqlBatch for Q5×N items + Q6 balance, then commit. Items need the generated invoice id
  for their FK, which a single PG batch cannot supply to earlier commands, so header and item-batch
  are separate statements within ONE transaction.
- Wire round trips per create follow the round-trip ledger in spec/db-access.md: raw ADO.NET and
  Dapper send Q7, BEGIN, Q4, one NpgsqlBatch carrying Q5×N + Q6, COMMIT; EF sends Q7, BEGIN, Q4,
  one batched round trip for the N items, Q6, COMMIT.

## EF model build

Stock EF: the model is built once at startup from `OnModelCreating`, exactly what
`AddDbContextPool<LedgerDbContext>(o => o.UseNpgsql(dataSource))` does with nothing else
wired (see "Canonicality"). Hibernate likewise builds its metamodel at boot, so the model
build is on both sides' startup path and inside both cold-start curves.
NOTE: the descending-on-id of the list index is proven by the generated SQL
(`ORDER BY i.id DESC` in EfQueryShapeTests); EfSchemaMappingTests asserts name + column order.

## Logging

Stock JsonConsoleFormatter nests under State/Message and emits a 7-digit timestamp, so it cannot
produce the frozen schema. Custom `LedgerConsoleFormatter` (registered via
`AddConsole(o => o.FormatterName=...)` + `AddConsoleFormatter`) writes the exact flat JSON:
`{ts(6-frac UTC Z), level(lowercase), msg(event name), request_id, ...flat event fields...}`.
`{OriginalFormat}` is filtered out. ConsoleLoggerOptions set QueueFullMode=Wait + MaxQueueLength
from LOG_QUEUE_LEN (8192). [LoggerMessage] source-gen drives http_access + domain events; the
constant Message string is the event name (msg). SYSLIB1015 (args not in message template) is
expected and suppressed; the args are intentionally structured fields, not interpolated.

## Access log

Middleware logs the MATCHED ROUTE TEMPLATE (`/invoices/{id}`), not the raw URL, via
`RouteEndpoint.RoutePattern.RawText`. /healthz, /readyz, /runtime-stats excluded. Exactly one line
per request; duration measured handler-entry → response-write-complete with Stopwatch (monotonic).

## /runtime-stats

`RuntimeStatsCollector` singleton owns a MeterListener over the `System.Runtime` and `Npgsql`
meters (instrument names per the spec mapping table: dotnet.gc.collections{gc.heap.generation},
dotnet.gc.pause.time, dotnet.gc.last_collection.heap.size,
dotnet.gc.last_collection.memory.committed_size, dotnet.gc.heap.total_allocated,
dotnet.thread_pool.thread.count, db.client.connection.max, db.client.connection.count{state}).
Endpoint counters are atomic increments on the frozen route-template keys. Config echo block + nulls
where .NET has no value (the spec-frozen gogc and gomemlimit keys, gc.heap_goal_bytes, and the
Npgsql-unexposed acquire_count / empty_acquire_count / acquire_wait_total_seconds). Shape
verified by tests; values are runtime-specific and only meaningful against live infra.

## Redis / cache

StackExchange.Redis single multiplexer (idiomatic, and the same client model as Lettuce on
the Java side — spec/cache.md).
Serialize-then-cache: a miss serializes the InvoiceDto via System.Text.Json, SETs the bytes with
TTL, returns those same bytes; a hit returns the cached bytes verbatim via `Results.Bytes(..,
"application/json")`. No negative caching. DEL on create.

## NpgsqlDataSource registration

`AddNpgsqlDataSource` lives in the separate Npgsql.DependencyInjection package; to keep the
dependency surface to the pinned set, the data source is registered as a singleton built from
`NpgsqlDataSourceBuilder` directly. Same pool/connection-string as EF.

## Package versions (pins)

Npgsql.EntityFrameworkCore.PostgreSQL 10.0.3, Npgsql 10.0.3, StackExchange.Redis 3.1.31,
Microsoft.EntityFrameworkCore.Relational 10.0.11 (explicit runtime pin).
Tests: xunit 2.9.3, xunit.runner.visualstudio 3.1.5, Microsoft.NET.Test.Sdk 18.8.1,
Microsoft.AspNetCore.Mvc.Testing 10.0.11.
Dapper 2.1.79 (variant-only). Dapper is a THIRD data layer (DATA_LAYER=dapper): its
named-parameter model keeps the statement bodies byte-identical on the wire, because
Npgsql's client-side rewriting produces the identical positional wire text
(DapperSqlParityTests pins the substitution). The three layers are efcore (full ORM),
dapper (micro-ORM) and ado (floor); ado and dapper remain as code and no measured cell
uses them (PROGRESS.md D5).

## Notes

- InvoiceItem `id` in the POST 201 body: all data layers on both services return the real DB
  identity ids. spec/db-access.sql `InsertInvoiceItem` does `RETURNING id`; EF gets the ids
  from SaveChanges, the raw ADO.NET NpgsqlBatch reads the batch results, and Hibernate reads
  each Q5's `returning id`. See NOTES-equivalence.md for the full create-response equivalence
  contract.
- `char(3)` currency comes back space-padded from PG; reads `TrimEnd()` it so the wire value is the
  3-char code. Equivalence-safe.
