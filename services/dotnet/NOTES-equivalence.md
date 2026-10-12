# services/dotnet: equivalence contract (.NET emits, Java aligns)

This file documents what the .NET SUT ACTUALLY emits on the wire, captured from a real running
instance via WebApplicationFactory (`ValidationBodyCaptureTests`). The .NET `AddValidation()`
output is the canonical 400 errors-map shape; the Java service matches it byte-for-byte. All
strings below are copied from captured HTTP responses, not hand-written. The counterpart file is
`services/java/NOTES-equivalence.md`; the cross-service ledger of every kept divergence is
`docs/FAIRNESS.md`.

## 400 validation envelope (RFC 7807, application/problem+json)

Frozen top-level fields (x-error-strings.validation), byte-exact:

```json
{
  "type": "https://ledgerline.test/problems/validation",
  "title": "Validation failed",
  "status": 400,
  "detail": "The request payload failed validation.",
  "errors": { ... }
}
```

No `traceId`/`requestId`/`instance` extensions (stripped in ProblemDetailsConfig). Content-Type is
`application/problem+json`.

## errors-map KEY format (THE alignment target for the Java side)

Keys are **PascalCase CLR property names**, NOT the snake_case JSON names. Collection elements use
**`Items[i].Property`** indexing (zero-based). This is the native output of .NET 10
`AddValidation()` source-gen; it keys by the validation path built from CLR member names.

Captured example: POST /invoices with `customer_id=0, currency="us", status="closed"`, an items
array of length 2 whose `items[0]` is fully invalid:

```json
"errors": {
  "CustomerId": ["must be a positive integer"],
  "Currency": ["must match ^[A-Z]{3}$"],
  "Status": ["must be one of: draft, open"],
  "Items": ["must contain between 3 and 8 items"],
  "Items[0].Sku": ["must match ^[A-Z]{2}-[0-9]{4}$"],
  "Items[0].Description": ["is required"],
  "Items[0].Qty": ["must be between 1 and 1000"],
  "Items[0].UnitPriceMinor": ["must be between 0 and 100000000"]
}
```

Captured example: POST /pricing/quote with `currency="usd"`, 50 items, `items[0].qty=0`,
`items[2].sku="bad-sku"`:

```json
"errors": {
  "Currency": ["must match ^[A-Z]{3}$"],
  "Items[0].Qty": ["must be between 1 and 1000"],
  "Items[2].Sku": ["must match ^[A-Z]{2}-[0-9]{4}$"]
}
```

### Key → field mapping (CLR property ↔ wire JSON field)

| errors key (emitted) | wire JSON field | rule |
|---|---|---|
| `CustomerId` | `customer_id` | id_positive |
| `Currency` | `currency` | currency_pattern |
| `Status` | `status` | status_enum |
| `Items` | `items` | items_create_count / items_quote_count |
| `Items[i].Sku` | `items[i].sku` | sku_pattern |
| `Items[i].Description` | `items[i].description` | description_length / required |
| `Items[i].Qty` | `items[i].qty` | qty_range |
| `Items[i].UnitPriceMinor` | `items[i].unit_price_minor` | unit_price_range |

The Java service emits the same keys (`io.ledgerline.problem.FieldPath` builds them from the
bean-validation property path): `CustomerId`, `Currency`, `Status`, `Items`,
`Items[<i>].Sku`, `Items[<i>].Description`, `Items[<i>].Qty`, `Items[<i>].UnitPriceMinor`. The
index is zero-based, formatted `Items[0]`, `Items[2]`, etc.

### Manual-path keys (not source-gen)

Endpoints that validate path/query params by hand emit lowercase single-token keys (no CLR member
behind them):

| endpoint | condition | key | value |
|---|---|---|---|
| GET /invoices/{id}, POST /invoices/{id}/pdf-stub | id non-integer or `< 1` | `id` | `must be a positive integer` |
| GET /customers/{id}/invoices | id non-integer or `< 1` | `id` | `must be a positive integer` |
| GET /customers/{id}/invoices | page `< 1` | `page` | `must be >= 1` |
| GET /customers/{id}/invoices | page `> 100000` | `page` | `must be <= 100000` |

## errors-map VALUE strings (frozen x-validation-messages, verified on wire)

These EXACT strings are emitted (the regex ones required brace-doubling in the C# attribute so
string.Format leaves them literal: see NOTES.md):

| rule | emitted value |
|---|---|
| required | `is required` |
| sku_pattern | `must match ^[A-Z]{2}-[0-9]{4}$` |
| currency_pattern | `must match ^[A-Z]{3}$` |
| status_enum | `must be one of: draft, open` |
| qty_range | `must be between 1 and 1000` |
| unit_price_range | `must be between 0 and 100000000` |
| items_create_count | `must contain between 3 and 8 items` |
| items_quote_count | `must contain exactly 50 items` |
| description_length | `must be between 1 and 200 characters` |
| page_min | `must be >= 1` |
| page_max | `must be <= 100000` |
| id_positive | `must be a positive integer` |

## Content-Type (alignment)

- Every JSON SUCCESS body is emitted with bare `application/json` on BOTH sides
  (spec/openapi.yaml names the media type with no parameter; ASP.NET's `; charset=utf-8`
  default is normalized away in `RequestIdMiddleware` — see docs/FAIRNESS.md's eliminated
  list). Problem responses are `application/problem+json` (no charset) on both.
- POST request bodies must DECLARE a JSON content type. The gate grammar is FROZEN and
  implemented as the same trivial code on both sides (`MediaTypeGateMiddleware.IsJsonMediaType`
  / the Java side's `JsonMediaTypeGate.isJsonMediaType`): value up to the first `;`,
  space/tab-trimmed, lowercased, equals
  `application/json` or ends `+json` with a `/`; parameters never validated. The gate runs
  only when the request HAS a body (declared non-zero length, or chunked); a body-less POST
  falls through to binding → the frozen 400. (.NET's native binding would 415 silently and
  with a subtly different grammar, which is WHY the explicit middleware exists; Spring's
  MediaType parsing likewise accepts a different set, which is why the Java gate exists.)

## 415 envelope (no errors map)

```json
{ "type":"https://ledgerline.test/problems/unsupported-media-type", "title":"Unsupported media type",
  "status":415, "detail":"The request content type must be application/json." }
```

Logged as one `request_error` (status 415, error_class "validation") plus the access line;
NOT counted in `endpoint_calls`. Both sides gate in their own code before the handler.

## 413 envelope (no errors map; NO request_error)

```json
{ "type":"https://ledgerline.test/problems/payload-too-large", "title":"Payload too large",
  "status":413, "detail":"The request body exceeds the configured limit." }
```

A body over BODY_MAX_BYTES. Access line only: Kestrel enforces the limit below the point
where the app can log a rejection event, so BOTH sides pin the access-line-only shape
(spec/logging.md). The Java side enforces the same limit in a filter INSIDE the app (Tomcat
has no container-level cap for `application/json`) and deliberately logs no event; .NET's
envelope comes from the StatusCodePages path via the ProblemDetails 413 mapping.

## endpoint_calls semantics (alignment)

On the two body-decoding POSTs, the counter increments AFTER content-type/decode/field
validation on both sides ("valid requests"). On GET/pdf-stub routes it increments before id
parsing ("routed requests") on both sides.

## GET /customers/{id}/invoices: aggregated 400

When BOTH the path id and ?page are invalid, one errors map carries both keys
(`{"id":[...],"page":[...]}`), both sides. `page` is read from the FIRST query value
(repeated `?page=` params are not comma-joined); `X-Request-Id` likewise takes the first
header value.

## /readyz

200 `{"status":"ok"}` / 503 `{"status":"unavailable"}` on both sides; DB probed first,
Redis only if the DB is reachable. Only "ok" is spec-frozen; "unavailable" is the aligned
failure string.

## 404 / 500 envelopes (no errors map)

```json
{ "type":"https://ledgerline.test/problems/not-found", "title":"Not found",
  "status":404, "detail":"The requested resource does not exist." }
```
```json
{ "type":"https://ledgerline.test/problems/internal", "title":"Internal server error",
  "status":500, "detail":"An unexpected error occurred." }
```

## Success bodies: snake_case, integer minor units, 6-frac timestamps

- All money fields are bare JSON integers (int64); no floats, no decimal points.
- `created_at` is RFC3339 UTC with EXACTLY 6 fractional digits + `Z` (custom format defeats .NET's
  7-digit 'O'). Example: `2026-06-06T00:00:00.000000Z`.
- /pricing/quote 200 body keys verified on wire: `currency`, `lines[]`
  (`sku, category, qty, unit_price_minor, category_surcharge_minor, qty_discount_minor,
  line_total_minor`), `subtotal_minor`, `order_discount_minor`, `total_minor`: all snake_case.
- /healthz 200 body is exactly `{"status":"ok"}` (verified).

## request_id

Accepted from inbound `X-Request-Id` else a server-generated UUIDv4; echoed in the `X-Request-Id`
RESPONSE HEADER only; never in any response body; excluded from JSON equivalence.

## Create-response InvoiceItem ids (resolved 2026-06-06)

Every data layer on both services returns the REAL DB identity ids in the 201 body:
spec/db-access.sql `InsertInvoiceItem` is `:one ... RETURNING id`; the raw ADO.NET
NpgsqlBatch reads the N returned ids, efcore gets them from SaveChanges, and Hibernate
reads each Q5's `returning id`. The cross-service equivalence normalizer still
placeholders server-generated ids/timestamps on create responses (different DB
sequence states between paired calls).

## Wire-level divergences kept + disclosed

Each is either framework-idiomatic on both sides or outside the frozen workload, so it is
pinned and disclosed rather than "fixed" into non-idiomatic code. The measured
cross-service ledger — captured against both live services on one shared PostgreSQL and
Redis — is `docs/FAIRNESS.md`; the rows here carry the .NET-side detail.

- **EF Q3 `LIMIT $n` / Q1 `LIMIT 1`**: see spec/db-access.md (pinned by
  EfQueryShapeTests, which capture against the production model).
- **Transaction start text**: Npgsql always spells the level out, so .NET sends
  `BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED` where the Java side sends a bare
  `BEGIN` — same level (the server default), same one round trip, different text
  (spec/db-access.md). Create-path round trips: Java 5 + N items against EF's 6
  (EF batches the N item inserts into one round trip); ado/dapper 5. The full measured
  ledger and the reason neither side is tuned away is spec/db-access.md's.
- **Connection hold**: ado/dapper run the Q7 probe and the tx on ONE held
  connection; EF and the Java side both check out twice (probe, then tx). Statement and
  round-trip ledgers are unaffected; pool-hold profiles differ slightly.
- **Client abort (load-generator timeout)**: .NET's ExceptionHandlerMiddleware
  converts an in-flight cancellation into status 499 with NO error lines and
  no body, and the in-flight DB work is cancelled. Bounded exposure: the frozen
  steady-state mix draws only seeded ids, and any probe with error rate > 0.1% is
  excluded from fits by the pre-registered gate, which is what happens at the
  ladder walls.
- **Response framing**: .NET's JSON-writer paths emit `Transfer-Encoding:
  chunked` (including the 15-byte /healthz body) while its `Results.Bytes`
  paths (GET /invoices/{id}, /runtime-stats) set `Content-Length`; the Java side
  sets `Content-Length` for anything that fits Tomcat's 8 KB response buffer and
  chunks above it. Body bytes are identical; framing and a few dozen wire bytes
  differ per endpoint and size (measured rows in docs/FAIRNESS.md).
- **Out-of-workload surface**: OPTIONS on a mapped route was aligned in the P5 parity
  pass (both sides 405, `Allow: GET`, the frozen internal envelope). The measured
  divergences that could not be removed — the Java side's `Connection: close` on
  400/408/411/413/414/500/501/503 (hard-coded in Tomcat) and Kestrel's reason phrase
  on the status line where Tomcat writes none — are docs/FAIRNESS.md rows. The
  remaining unexercised surface (unmatched 404 body, HEAD, trailing slash) follows
  each framework's default and is NOT asserted aligned.
- **Redis client behavior**: SE.Redis never retries commands and its 5000 ms
  SyncTimeout is left at default (the Java side pins Lettuce's 60 s default down to
  the same 5 s — a docs/FAIRNESS.md row). The invalidation is forced to `DEL` by
  removing `UNLINK` from the command map (spec/cache.md); the cache-fill spelling
  stays `SETEX` against Lettuce's `SET … EX`, the same operation, recorded in
  docs/FAIRNESS.md. The .NET cache calls take no CancellationToken (differs only
  for already-aborted requests). A healthy local Redis exercises none of the
  timeout/retry surface.
- **Config parsing (aligned 2026-08-14)**: both sides refuse startup on a
  set-but-malformed integer env value (.NET's silent TryParse-fallback was
  replaced; Spring's binder refuses on the Java side) so a typo'd knob can never
  run the two services at different effective configs.
