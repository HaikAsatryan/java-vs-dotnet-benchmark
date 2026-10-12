# spec/logging.md — logging contract (both services, frozen)

JSON lines to stdout; docker json-file driver with identical compose config on
both services. Field ORDER inside a line is irrelevant; field NAMES and VALUES
are frozen.

## Common fields on every line

| field | type | value |
|---|---|---|
| ts | string | RFC3339 UTC, exactly 6 fractional digits, Z (same format as wire timestamps) |
| level | string | "info" \| "warn" \| "error" |
| msg | string | event name (below) |
| request_id | string | the request's id (UUIDv4 or inbound X-Request-Id); empty for lifecycle lines |

## Access line — exactly one per HTTP request

`msg="http_access"`, level `info`, additional fields:

| field | type | pin |
|---|---|---|
| method | string | uppercase |
| path | string | the MATCHED ROUTE TEMPLATE, e.g. "/invoices/{id}", never the raw URL (identical cardinality both sides) |
| status | int | response status |
| duration_ms | number | float ms, monotonic clock, measured handler-entry -> response-write-complete (same boundary both sides) |

Excluded from access logging on BOTH sides: `/healthz`, `/readyz`,
`/runtime-stats` (health is the HTTP floor; runtime-stats is bench
instrumentation). Everything else logs exactly one line.

## Domain events (level info, on successful writes)

| msg | fields |
|---|---|
| invoice_created | customer_id, invoice_id, item_count, total_minor |
| cache_invalidated | invoice_id |
| pdf_stub_written | invoice_id, bytes_written |
| quote_computed | item_count, total_minor |

Errors: `msg="request_error"`, level `error`, fields `status`,
`error_class` (validation | internal). `validation` fires on exactly two paths
(2026-08-13 revision): the undeserializable/malformed request-body path — a
non-object/invalid-JSON or unreadable body, which becomes the frozen 400
`{"body":["is required"]}` — and the non-JSON Content-Type path, which becomes
the frozen 415 (spec/openapi.yaml x-error-strings.unsupported_media_type).
Identical on both sides. An OVERSIZED body is the frozen 413 with NO
request_error on either side (the access line's `status` records it; .NET's
Kestrel enforces the limit below the point where the app can log a rejection
event, so both sides pin the access-line-only shape). Field-level validation
400s and all 404s produce NO request_error (recorded via the access line's
`status`). Internal errors additionally log `msg="exception"` (level error,
field `detail`); never inside the access line.

Lifecycle: `msg="config"` once at startup (the effective-config line the runner
captures into the manifest); `msg="shutdown_complete"` last line before exit.

No other lines during measured traffic. No sampling. Level floor `info`, debug
disabled in benchmark builds. Identical levels both sides.

## Async sink contract (frozen)

- Bounded queue, capacity 8192 (`LOG_QUEUE_LEN`), on both sides.
- BLOCK-ON-FULL, lossless: the producer blocks when the queue is full. Never drop.
  - Java: Logback `AsyncAppender` in front of the `ConsoleAppender`, with
    `queueSize = ${LOG_QUEUE_LEN:-8192}`, `discardingThreshold = 0` and
    `neverBlock = false`. All three DEFAULTS BREAK THIS CONTRACT:
    `discardingThreshold` resolves to `queueSize/5` and every `http_access`
    line is INFO, i.e. discardable, so the stock appender starts dropping
    access lines at 80% queue depth — under exactly the load being measured.
  - .NET: console logger `QueueFullMode = Wait`, `MaxQueueLength = 8192`
    (`ConsoleLoggerQueueFullMode.Wait`; the DEFAULT is `DropWrite`).
- Flush-on-shutdown: on SIGTERM stop accepting requests, drain the queue fully,
  then exit. Java bounds that drain with `maxFlushTime = 60000` ms (the default
  is 1000 ms, which would cap the drain the line above requires) behind
  `server.shutdown: graceful`; .NET drains in the console processor's dispose.
- **The two stacks do not queue the same thing, and it is not symmetric.**
  .NET's `ConsoleLogger.Log` runs the `ConsoleFormatter` synchronously and
  enqueues an ALREADY-FORMATTED entry, so each access line's JSON serialization
  is charged to the request thread. Logback's `AsyncAppender` enqueues an
  `ILoggingEvent` and runs the encoder — and therefore
  `LedgerStructuredLogFormatter` — on its own `AsyncAppender-Worker` thread.
  Measured 2026-08-24 on the shipped versions with each service's exact sink
  settings: Logback 1.5.38 encoded on `AsyncAppender-Worker` while `log()` was
  called on the request thread and the queued object was
  `ch.qos.logback.classic.spi.LoggingEvent`;
  Microsoft.Extensions.Logging.Console 10.0.0 ran `ConsoleFormatter.Write` on
  the request thread. Both are their framework's stock async console and
  neither drops a line. The CPU is inside the SUT cgroup either way, so the
  CPU-ms headline is unaffected; the LATENCY exposure is not symmetric, on 100%
  of measured requests. Recorded as a FAIRNESS row, not equalized: matching
  either side to the other means abandoning its framework's stock async sink.
  This file asserted the opposite until 2026-08-24.

## What is asserted, and by what

The bounded lossless contract above is the implemented behaviour on both sides.
The EVIDENCE for it is not symmetric, and this section names which half rests on
what rather than claiming one guard for both.

- **Queue LENGTH** is a scrubbed knob (`LOG_QUEUE_LEN`, in
  `registry.SHARED_KNOB_ENV` and `compose.KNOB_ENV_VARS`) recorded as
  `manifest.Knobs.log_queue_len` whenever a cell sets it; a compose default is
  deliberately not fabricated into the manifest, so the null there means "not
  overridden", not "unknown". The value the SUT is actually running with reaches
  the artifacts live, from `/runtime-stats` `config.log_queue_len` — measured
  8192 on both services 2026-08-24 — which the per-window flush writes verbatim
  into `<slot>.runtime_stats.summary.json` (both edges) and
  `<slot>.runtime_stats.jsonl` (every poll).
- **Queue-full MODE** is asserted on Java off the RUNNING appender by
  `LoggingContractTest`: `queueSize` 8192, `discardingThreshold` 0,
  `neverBlock` false, `maxFlushTime` >= 60000 ms, plus a block-and-drain
  behaviour test. On .NET the two settings are pinned in source
  (`services/dotnet/Program.cs`: `QueueFullMode = Wait`,
  `MaxQueueLength = LOG_QUEUE_LEN`) and their evidence is that source. The
  difference is a `docs/FAIRNESS.md` row, and it is an evidence asymmetry rather
  than a behaviour one: both sinks are lossless as shipped.
- **The `config` line is a startup ARTIFACT, not a harness input.** Measured
  2026-08-24 against both live services: .NET's line carries
  `log_queue_len` and no queue-full field; Java's carries `log_queue_len` plus
  `log_block_on_full: true`, which `EffectiveConfig` writes as a constant rather
  than reading it back off the appender. Nothing in `bench/` parses a `config`
  line — `phases._record_cell_knobs` builds `manifest.Knobs` from the scrubbed
  environment plus `/runtime-stats` `build` — so no manifest field is derived
  from it, and neither service's line is load-bearing for any published number.
  This file claimed the opposite until 2026-08-24.
