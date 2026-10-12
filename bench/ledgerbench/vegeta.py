"""Vegeta target/body generation + attack invocation + report parsing.

Implements spec/workload.yaml EXACTLY and deterministically given the
`target_generator_seed`:
  - request mix (30 get / 25 list / 20 quote / 15 create / 8 pdf / 2 healthz),
  - finite inverse-CDF Zipf(alpha=1.0) id selection (zipf.py),
  - page distribution (1..5 skewed to 1),
  - create/quote body generation to spec sizes (~2 KB / ~8 KB), int64 minor
    units, no floats anywhere in money paths.

Generation is SPLIT in two (W20/R1-A7):

  - `generate_body_pool` writes the create/quote request bodies ONCE per run.
    A body carries no URL, so the pool is slot-independent and both slots of an
    RCB block send byte-identical payloads.
  - `draw_targets` writes ONE targets file per probe attack, streaming to an
    open handle, drawing bodies from the pool by reference.

Round 1 generated one targets file per slot and pointed all three attacks of
every probe at it. vegeta reads a targets file from the top on every
invocation, so the warmup, the head-of-window discard and the measured window
each replayed a PREFIX OF THE SAME SEQUENCE -- and since the warmup runs
immediately after a Redis FLUSHALL, it populated the cache with exactly the
invoice ids the measured window was about to ask for. `util.pcg64.stream_for`
gives each (run, cell, stage, rate, block, phase) its own PCG64 stream, which
is what makes those sequences independent, and `probe_draw_count` sizes each
draw at 1.02x the requests its attack will issue, which is what stops a stream
wrapping inside its own window.

Attack invocation is via `docker exec` into the running vegeta container, so
attack params are runner-controlled, not baked into the image.
The `.bin` is collected and `vegeta report -type=json` parsed; achieved-vs-offered
is asserted within spec/slo.yaml co_validation tolerance.

Determinism note: a draw is a pure function of (seed, stream labels, count,
scale, mix, distributions, pool), so the same probe identity reproduces the
same file byte for byte -- which is the resume guarantee. Two DIFFERENT probe
identities must NOT.
See bench/NOTES.md ("PRNG" and "Body sizing") for the PRNG and body-sizing decisions.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from collections.abc import Collection, Iterable, KeysView
from dataclasses import dataclass, field
from pathlib import Path

from .config import Workload
from .util import paths
from .util.pcg64 import PCG64
from .util.proc import ProcResult, run
from .zipf import ZipfTable, build_zipf_table

# Frozen route templates (match spec/openapi.yaml + spec/runtime-stats.md keys).
ENDPOINTS = {
    "get_invoice": ("GET", "/invoices/{id}"),
    "list_invoices": ("GET", "/customers/{id}/invoices"),
    "pricing_quote": ("POST", "/pricing/quote"),
    "create_invoice": ("POST", "/invoices"),
    "pdf_stub": ("POST", "/invoices/{id}/pdf-stub"),
    "healthz": ("GET", "/healthz"),
}

# Filler char for create-invoice descriptions (printable ASCII, 1 byte, no JSON
# escaping needed). Padding stays within CreateInvoiceItem.description maxLength.
_FILLER_CHAR = "x"
_DESCRIPTION_MAX = 200  # openapi CreateInvoiceItem.description maxLength


# The generator's OWN endpoint -> keyspace mapping: which of the two Zipf tables
# each branch of `draw_targets` samples from. `spec/workload.yaml keyspaces`
# declares the same mapping in `used_by`, and `assert_keyspace_contract` below
# is the reader that makes it a checked contract instead of a declaration --
# the governing rule in CLAUDE.md, applied to a spec block rather than a gate.
KEYSPACE_BY_ENDPOINT = {
    "get_invoice": "invoices",
    "pdf_stub": "invoices",
    "list_invoices": "customers",
    "create_invoice": "customers",
}

# The one selection this generator implements. `build_zipf_table` is the only
# draw `draw_targets` has, so a spec keyspace declaring anything else names a
# distribution no code samples from.
IMPLEMENTED_SELECTION = "zipf"


def assert_keyspace_contract(wl: Workload) -> None:
    """Refuse to generate targets whose draw diverges from spec/workload.yaml.

    Two ways it can diverge, and both are silent otherwise: an endpoint moved
    between keyspaces (`used_by`), or a keyspace declared a `selection` this
    generator does not implement. Raised before the file is written, because a
    targets file is the run's working set and a wrong one is not detectable
    from the artifacts afterwards.
    """
    declared: dict[str, str] = {}
    for name in ("customers", "invoices"):
        ks = getattr(wl.keyspaces, name)
        if ks.selection != IMPLEMENTED_SELECTION:
            raise ValueError(
                f"spec/workload.yaml keyspaces.{name}.selection is "
                f"{ks.selection!r}; this generator implements only "
                f"{IMPLEMENTED_SELECTION!r} (vegeta.build_zipf_table)"
            )
        for endpoint in ks.used_by:
            declared[endpoint] = name
    if declared != KEYSPACE_BY_ENDPOINT:
        raise ValueError(
            "spec/workload.yaml keyspaces.*.used_by declares "
            f"{declared} but the target generator draws {KEYSPACE_BY_ENDPOINT}"
        )


@dataclass
class ScaleRanges:
    """Resolved keyspace ranges for the active scale (full vs smoke)."""

    customer_max: int
    invoice_max: int

    @classmethod
    def from_workload(cls, wl: Workload, scale: str) -> "ScaleRanges":
        sc = wl.seeding.scales[scale]
        return cls(customer_max=sc.customers, invoice_max=sc.invoices)


# A draw is sized at this multiple of the requests its attack will issue, so
# vegeta never reaches the end of the file and starts over. Drawing per probe
# without this is only half the fix: a 120 s window at 13k rps issues 1.56M
# requests, which wraps any file sized from the old 1M working-set dial.
DRAW_OVERDRAW = 1.02


def probe_draw_count(rate: int, duration_s: float) -> int:
    """Targets to draw for an attack of `rate` x `duration_s` (with overdraw)."""
    return max(1, math.ceil(rate * duration_s * DRAW_OVERDRAW))


@dataclass(frozen=True)
class Containment:
    """How much of a measured window's read traffic was already pre-warmed.

    `hits / requests`, over the invoice-id READ requests of one measured draw,
    against the ids the attacks that ran BEFORE that window (every warmup
    segment and the head-of-window discard, after the Redis FLUSHALL) drew.
    `fraction` is None only when the draw asked for no invoice id at all.
    """

    requests: int
    hits: int
    prewarmed_ids: int

    @property
    def fraction(self) -> float | None:
        return self.hits / self.requests if self.requests else None


def prewarm_containment(
    measured: Counter[int], prewarmed: Collection[int]
) -> Containment:
    """Score one measured draw against the ids drawn before its window.

    REQUEST-WEIGHTED, and that is the whole point of the function. Separate
    PCG64 streams remove the SEQUENCE-CORRELATED pre-warm -- round 1's warmup
    replayed the prefix the measured window was about to consume, so containment
    was 1.0 by construction -- but they cannot remove the Zipf head, which both
    streams draw by construction over the same keyspace. A set-wise ratio hides
    exactly that: the head is a handful of ids and the large majority of the
    requests, so |warm & measured| / |measured| reads small while most of the
    window's reads are already resident. This number is what the report and the
    manifest publish in place of the "nothing pre-warms the window" claim they
    used to assert (p1-plan 5.1 states the same fact about `hot_set_touched`).
    """
    ids = prewarmed if isinstance(prewarmed, (set, frozenset, dict)) else set(prewarmed)
    hits = sum(n for inv_id, n in measured.items() if inv_id in ids)
    return Containment(
        requests=sum(measured.values()), hits=hits, prewarmed_ids=len(ids),
    )


@dataclass
class BodyPool:
    """The create/quote request bodies, written once and drawn from per probe.

    Bodies carry no URL, so ONE pool serves every slot: that is what keeps both
    halves of an RCB block sending byte-identical payloads while their targets
    files differ only in the base URL.

    `create_customer_ids[i]` is the customer_id inside `create_refs[i]`, held
    here so a draw can account for the customer ids it touched without reading
    a single body file back off disk.
    """

    bodies_dir: Path
    ref_prefix: str
    create_refs: list[str] = field(default_factory=list)
    quote_refs: list[str] = field(default_factory=list)
    create_customer_ids: list[int] = field(default_factory=list)
    create_body_bytes: list[int] = field(default_factory=list)
    quote_body_bytes: list[int] = field(default_factory=list)
    seed: int = 0
    scale: str = ""
    generation_seconds: float = 0.0

    @property
    def distinct_customer_ids(self) -> int:
        return len(set(self.create_customer_ids))


@dataclass
class DrawnTargets:
    """One probe attack's targets file and what it asks for.

    The distinct-id sets are carried out of the draw rather than recovered by
    re-reading the file (and, for the create path, by re-reading every body it
    referenced): the draw already holds them, and the manifest's working-set
    accounting is the only consumer.
    """

    targets_path: Path
    count: int
    inc: int
    endpoint_counts: dict[str, int] = field(default_factory=dict)
    # A COUNT per invoice id, not a set. The distinct count is what the working
    # set publishes, but the pre-warm containment this draw is scored for is
    # REQUEST-WEIGHTED (`prewarm_containment`): a Zipf head id the warmup
    # touched is one id and thousands of measured requests, and a set-wise
    # figure understates the read path's actual residency by that factor. A
    # Counter is the same one entry per distinct id the set was.
    invoice_id_reads: Counter[int] = field(default_factory=Counter)
    customer_ids_list_urls: set[int] = field(default_factory=set)
    customer_ids_create_bodies: set[int] = field(default_factory=set)
    generation_seconds: float = 0.0
    # Wall clock spent SHIPPING this file into the load-generator container.
    # Filled by the runner (the draw itself never ships), and carried here
    # because the cost W20 published covers generation only while the copy is
    # the half that scales with the file: measured on this box a measured-window
    # draw is 4.1 MB at 500 rps and 109.3 MB at 13,312, and a 410.6 MB soak draw
    # at 10,000 rps is larger again.
    ship_seconds: float = 0.0

    @property
    def invoice_ids(self) -> KeysView[int]:
        """The distinct invoice ids the READ path named."""
        return self.invoice_id_reads.keys()

    @property
    def invoice_read_requests(self) -> int:
        """Requests (not ids) this draw aims at an invoice id."""
        return sum(self.invoice_id_reads.values())

    @property
    def distinct_customer_ids(self) -> int:
        return len(self.customer_ids_list_urls | self.customer_ids_create_bodies)


# --------------------------------------------------------------------------- #
# mix expansion
# --------------------------------------------------------------------------- #
def build_mix_cdf(wl: Workload) -> tuple[list[str], list[float]]:
    """Cumulative-weight table over endpoint names in workload order."""
    pairs = wl.mix.as_pairs()
    names = [name for name, _ in pairs]
    total = sum(w for _, w in pairs)
    cdf: list[float] = []
    running = 0
    for _, w in pairs:
        running += w
        cdf.append(running / total)
    cdf[-1] = 1.0
    return names, cdf


def build_page_cdf(wl: Workload) -> tuple[list[int], list[float]]:
    """Cumulative table over pages 1..5 from the page_distribution weights."""
    ordered = wl.page_distribution.ordered()
    pages = [p for p, _ in ordered]
    cdf: list[float] = []
    running = 0.0
    for _, w in ordered:
        running += w
        cdf.append(running)
    cdf[-1] = 1.0
    return pages, cdf


# --------------------------------------------------------------------------- #
# sku / body generation
# --------------------------------------------------------------------------- #
def _sku_categories(field_spec: dict) -> list[str]:
    cats = field_spec.get("sku", {}).get("categories")
    if not cats:
        raise ValueError("workload sku field missing categories")
    return list(cats)


def gen_sku(rng: PCG64, categories: list[str]) -> str:
    """`<CAT>-<NNNN>` matching ^[A-Z]{2}-[0-9]{4}$ (openapi sku pattern)."""
    cat = categories[rng.randint(0, len(categories) - 1)]
    nnnn = rng.randint(0, 9999)
    return f"{cat}-{nnnn:04d}"


def gen_create_body(rng: PCG64, wl: Workload, customer_id: int) -> str:
    """Deterministic CreateInvoiceRequest JSON near payloads.create_invoice.target_bytes.

    Uses compact JSON; the per-item `description` is the size knob (filler:true).

    Spec tension (bench/NOTES.md ("Body sizing vs the wire schema")): item_count is uniform [3,8] but description is
    capped at maxLength 200, so a 3-item body cannot reach target-512 (~1536 B)
    even fully padded. Since item count does NOT cause cross-language divergence
    (both services just parse N items), we draw n uniformly and then GROW it
    toward maxItems=8 only as far as needed for the body to be able to reach the
    tolerance band; then we pad descriptions to land near target. Deterministic:
    the growth and padding consume no RNG.
    """
    spec = wl.payloads.create_invoice
    cats = _sku_categories(spec.fields)
    tol = 512  # spec asserts target +/- 512
    n_items = rng.randint(spec.item_count.min, spec.item_count.max)
    status = "draft" if rng.random() < 0.5 else "open"  # weights 0.5/0.5
    target = spec.target_bytes

    items: list[dict] = []
    for _ in range(n_items):
        items.append(
            {
                "sku": gen_sku(rng, cats),
                "description": "",  # filled below
                "qty": rng.randint(1, 50),
                "unit_price_minor": rng.randint(100, 5_000_000),
            }
        )

    body = {
        "customer_id": customer_id,
        "currency": "USD",
        "status": status,
        "items": items,
    }

    def maxed_size() -> int:
        for it in items:
            it["description"] = _FILLER_CHAR * _DESCRIPTION_MAX
        return len(_compact(body).encode("utf-8"))

    # Grow item count toward maxItems until the band is reachable (or capped).
    while maxed_size() < target - tol and len(items) < spec.item_count.max:
        items.append(
            {
                "sku": gen_sku(rng, cats),
                "description": _FILLER_CHAR * _DESCRIPTION_MAX,
                "qty": rng.randint(1, 50),
                "unit_price_minor": rng.randint(100, 5_000_000),
            }
        )

    # Reset descriptions to empty, then pad evenly to approach target. We solve
    # for the per-item filler length that minimizes |size - target| without
    # exceeding maxLength; a short bisection on length is exact and deterministic.
    for it in items:
        it["description"] = ""
    empty_size = len(_compact(body).encode("utf-8"))
    n = len(items)
    # Each filler char adds exactly 1 byte/item (ASCII, no JSON escaping).
    needed = target - empty_size
    per_item = 0 if needed <= 0 else min(_DESCRIPTION_MAX, (needed + n - 1) // n)
    per_item = max(1, per_item)  # minLength 1
    for it in items:
        it["description"] = _FILLER_CHAR * per_item

    # If padding overshot the upper bound, trim one char at a time (cheap, n<=8).
    while len(_compact(body).encode("utf-8")) > target + tol and per_item > 1:
        per_item -= 1
        for it in items:
            it["description"] = _FILLER_CHAR * per_item

    return _compact(body)


def gen_quote_body(rng: PCG64, wl: Workload) -> str:
    """Deterministic QuoteRequest JSON near payloads.pricing_quote.target_bytes.

    QuoteItem has no description field (openapi), so the only schema-faithful way
    to approach the 8 KB target with exactly 50 compact items is whitespace: we
    serialize with indentation. Both services discard insignificant JSON
    whitespace identically on parse, so this introduces zero behavioral skew.
    See bench/NOTES.md ("Body sizing vs the wire schema").
    """
    spec = wl.payloads.pricing_quote
    cats = _sku_categories(spec.fields)
    n_items = rng.randint(spec.item_count.min, spec.item_count.max)  # 50..50

    items = [
        {
            "sku": gen_sku(rng, cats),
            "qty": rng.randint(1, 200),
            "unit_price_minor": rng.randint(100, 1_000_000),
        }
        for _ in range(n_items)
    ]
    body = {"currency": "USD", "items": items}

    target = spec.target_bytes
    # Pick the indent that lands closest to target without exceeding +tolerance.
    best = _compact(body)
    best_size = len(best.encode("utf-8"))
    for indent in range(1, 12):
        candidate = json.dumps(body, separators=(",", ": "), indent=indent)
        size = len(candidate.encode("utf-8"))
        if abs(size - target) < abs(best_size - target):
            best, best_size = candidate, size
        if size >= target:
            break
    return best


def _compact(obj: object) -> str:
    """Compact JSON: no spaces, sorted-free (insertion order), UTF-8."""
    return json.dumps(obj, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# targets-file generation
# --------------------------------------------------------------------------- #
def zipf_tables(wl: Workload, scale: str) -> tuple[ZipfTable, ZipfTable]:
    """(customer, invoice) Zipf tables for `scale`. Build ONCE per run.

    `build_zipf_table` holds an N-entry weights list and an N-entry CDF at the
    same time, N being the seeded corpus (5,000,000 invoices at full scale):
    ~300 MB of peak allocation and seconds of pure-Python work. A per-probe draw
    calls the draw 600+ times, so the tables are built here and injected.
    """
    ranges = ScaleRanges.from_workload(wl, scale)
    return (build_zipf_table(ranges.customer_max, wl.zipf.alpha),
            build_zipf_table(ranges.invoice_max, wl.zipf.alpha))


def body_pool_sizes(wl: Workload, requests: int) -> tuple[int, int]:
    """(create, quote) body counts for a pool serving `requests` requests.

    `requests` is the run's working-set dial (`ScaleProfile.body_pool_requests`)
    read as a FLOOR on the pool, not as the count of any one draw: a probe draws
    `probe_draw_count(rate, duration)` targets, which at the top of a full
    ladder is larger than the whole pool.
    """
    shares = dict(wl.mix.as_pairs())
    def n(name: str) -> int:
        share = shares.get(name, 0)
        return 0 if share <= 0 else max(1, round(requests * share / 100.0))
    return n("create_invoice"), n("pricing_quote")


def generate_body_pool(
    wl: Workload,
    *,
    out_dir: Path,
    seed: int,
    n_create: int,
    n_quote: int,
    scale: str,
    ref_prefix: str = "bodies",
    customer_zipf: ZipfTable | None = None,
) -> BodyPool:
    """Write the run's create/quote bodies once. Pure function of its inputs.

    Bodies are written with PLAIN buffered writes, not `paths.write_bytes_atomic`:
    that helper mkstemps, writes, flushes, FSYNCS and renames, and at full scale
    this is ~350,000 files whose only reader is a vegeta container that never
    survives a crash boundary. The atomic-write guarantee exists for the ledger
    and the published artifacts; paying it here bought nothing and cost an fsync
    per body.

    `ref_prefix` is the path a targets file uses to reach a body, resolved by
    vegeta relative to its working directory. The runner ships one pool for the
    whole run and cds one level above it, so the refs it wants are
    `pool/bodies/...`; a local caller keeps the default.
    """
    assert_keyspace_contract(wl)
    bodies_dir = out_dir / "bodies"
    bodies_dir.mkdir(parents=True, exist_ok=True)
    ranges = ScaleRanges.from_workload(wl, scale)
    if customer_zipf is None:
        customer_zipf = build_zipf_table(ranges.customer_max, wl.zipf.alpha)

    rng = PCG64(seed)
    pool = BodyPool(bodies_dir=bodies_dir, ref_prefix=ref_prefix, seed=seed, scale=scale)
    t0 = time.perf_counter()
    for i in range(n_create):
        cust_id = customer_zipf.sample_id(rng)
        body = gen_create_body(rng, wl, cust_id)
        name = f"create_{i:08d}.json"
        paths.write_scratch_text(bodies_dir / name, body)
        pool.create_refs.append(f"{ref_prefix}/{name}")
        pool.create_customer_ids.append(cust_id)
        pool.create_body_bytes.append(len(body.encode("utf-8")))
    for i in range(n_quote):
        body = gen_quote_body(rng, wl)
        name = f"quote_{i:08d}.json"
        paths.write_scratch_text(bodies_dir / name, body)
        pool.quote_refs.append(f"{ref_prefix}/{name}")
        pool.quote_body_bytes.append(len(body.encode("utf-8")))
    pool.generation_seconds = time.perf_counter() - t0
    return pool


# Targets buffered before each write() to the open handle. Round 1 accumulated
# every line of the file in one `lines: list[str]` and joined it at the end:
# ~1.55M strings resident before a byte reached disk, which a per-probe draw at
# the top of a full ladder would make several times worse.
_DRAW_FLUSH_LINES = 8192


def draw_targets(
    wl: Workload,
    *,
    out_path: Path,
    count: int,
    rng: PCG64,
    base_url: str,
    pool: BodyPool,
    customer_zipf: ZipfTable,
    invoice_zipf: ZipfTable,
) -> DrawnTargets:
    """Stream ONE probe attack's targets file from `rng`. Pure; no I/O but the file.

    The Zipf tables are INJECTED. `build_zipf_table` allocates an N-entry weights
    list and an N-entry CDF for N = the seeded invoice count (5,000,000 at full
    scale); rebuilding them inside a per-probe draw would repeat ~300 MB of
    pure-Python work 600+ times in a run. The runner builds them once.

    `rng` carries the stream: see `util.pcg64.stream_for`. The caller derives it
    from the probe identity, and `slot` is deliberately NOT one of the labels --
    both halves of an RCB block must face the identical request sequence, which
    is the pairing contract, and `base_url` is the only thing that may differ.
    """
    assert_keyspace_contract(wl)
    mix_names, mix_cdf = build_mix_cdf(wl)
    pages, page_cdf = build_page_cdf(wl)
    n_create, n_quote = len(pool.create_refs), len(pool.quote_refs)

    endpoint_counts: dict[str, int] = {name: 0 for name in mix_names}
    invoice_id_reads: Counter[int] = Counter()
    url_customer_ids: set[int] = set()
    body_customer_ids: set[int] = set()

    t0 = time.perf_counter()
    buf: list[str] = []
    with paths.open_scratch_text(out_path) as fh:
        for _ in range(count):
            name = mix_names[rng.choice_index(mix_cdf)]
            endpoint_counts[name] += 1
            method, _template = ENDPOINTS[name]

            if name == "get_invoice":
                inv_id = invoice_zipf.sample_id(rng)
                invoice_id_reads[inv_id] += 1
                buf.append(f"{method} {base_url}/invoices/{inv_id}\n\n")
            elif name == "pdf_stub":
                inv_id = invoice_zipf.sample_id(rng)
                invoice_id_reads[inv_id] += 1
                buf.append(f"{method} {base_url}/invoices/{inv_id}/pdf-stub\n\n")
            elif name == "list_invoices":
                cust_id = customer_zipf.sample_id(rng)
                url_customer_ids.add(cust_id)
                page = pages[rng.choice_index(page_cdf)]
                buf.append(
                    f"{method} {base_url}/customers/{cust_id}/invoices?page={page}\n\n"
                )
            elif name == "healthz":
                buf.append(f"{method} {base_url}/healthz\n\n")
            elif name == "create_invoice":
                if n_create == 0:
                    raise ValueError(
                        "draw_targets: the mix draws create_invoice but the body "
                        "pool holds no create bodies"
                    )
                idx = rng.randint(0, n_create - 1)
                body_customer_ids.add(pool.create_customer_ids[idx])
                buf.append(
                    f"{method} {base_url}/invoices\n"
                    "Content-Type: application/json\n"
                    f"@{pool.create_refs[idx]}\n\n"
                )
            elif name == "pricing_quote":
                if n_quote == 0:
                    raise ValueError(
                        "draw_targets: the mix draws pricing_quote but the body "
                        "pool holds no quote bodies"
                    )
                idx = rng.randint(0, n_quote - 1)
                buf.append(
                    f"{method} {base_url}/pricing/quote\n"
                    "Content-Type: application/json\n"
                    f"@{pool.quote_refs[idx]}\n\n"
                )
            else:  # pragma: no cover - mix is exhaustive
                raise ValueError(f"unknown endpoint {name}")

            if len(buf) >= _DRAW_FLUSH_LINES:
                fh.write("".join(buf))
                buf.clear()
        if buf:
            fh.write("".join(buf))

    return DrawnTargets(
        targets_path=out_path,
        count=count,
        inc=rng.inc,
        endpoint_counts=endpoint_counts,
        invoice_id_reads=invoice_id_reads,
        customer_ids_list_urls=url_customer_ids,
        customer_ids_create_bodies=body_customer_ids,
        generation_seconds=time.perf_counter() - t0,
    )


# --------------------------------------------------------------------------- #
# attack invocation + report parsing
# --------------------------------------------------------------------------- #
@dataclass
class AttackParams:
    rate: int
    duration_s: int
    max_workers: int
    connections: int
    timeout_s: float
    keepalive: bool = True


@dataclass
class AttackResult:
    bin_path: Path
    report: "VegetaReport"
    params: AttackParams


@dataclass
class VegetaReport:
    """Parsed `vegeta report -type=json` summary (subset we record)."""

    requests: int
    rate: float          # achieved request rate (mean)
    throughput: float    # successful responses / second
    duration_ns: int
    success_ratio: float
    status_codes: dict[str, int]
    latency_mean_ns: int
    latency_p50_ns: int
    latency_p99_ns: int
    latency_max_ns: int
    errors: list[str]

    @property
    def error_rate(self) -> float:
        return 1.0 - self.success_ratio

    @classmethod
    def from_json(cls, text: str) -> "VegetaReport":
        d = json.loads(text)
        lat = d.get("latencies", {})
        return cls(
            requests=int(d.get("requests", 0)),
            rate=float(d.get("rate", 0.0)),
            throughput=float(d.get("throughput", 0.0)),
            duration_ns=int(d.get("duration", 0)),
            success_ratio=float(d.get("success", 0.0)),
            status_codes={str(k): int(v) for k, v in d.get("status_codes", {}).items()},
            latency_mean_ns=int(lat.get("mean", 0)),
            latency_p50_ns=int(lat.get("50th", 0)),
            latency_p99_ns=int(lat.get("99th", 0)),
            latency_max_ns=int(lat.get("max", 0)),
            errors=list(d.get("errors", []) or []),
        )


def assert_achieved_vs_offered(
    report: VegetaReport, offered_rate: float, tolerance_pct: float
) -> tuple[bool, float]:
    """Check achieved rate is within `tolerance_pct` of offered (slo co_validation).

    Returns (ok, ratio). Ratio = achieved/offered. The runner marks a probe
    invalid (not silently retried) when this fails; reporting is the caller's.
    """
    if offered_rate <= 0:
        raise ValueError("offered_rate must be > 0")
    ratio = report.rate / offered_rate
    ok = abs(ratio - 1.0) * 100.0 <= tolerance_pct
    return ok, ratio


def build_attack_argv(
    *,
    targets_in_container: str,
    out_bin_in_container: str,
    params: AttackParams,
) -> list[str]:
    """Argv for `vegeta attack` (container-relative paths).

    The caller runs this inside the vegeta container (compose.py); `-output`
    writes the .bin.

    `-lazy` is MANDATORY, not a preference: without it vegeta parses the whole
    targets file up front and inlines every `@body` into memory -- measured on
    a rehearsal run (2026-08-27), the 8,000 rps rung's ~979k-target
    draw put vegeta's anon RSS at 4.18 GiB and the memcg OOM-killed it against
    the 4g ledger limit, and a full-tier soak draw (~8M targets, W20) would
    need >15 GiB. Round 1 never hit this only because its one REPLAYED
    1M-target file (R1-A7) was smaller than W20's correctly rate-sized
    per-attack draws. Lazy streams targets from the bench-io volume instead;
    the draw already writes each attack's file in exactly the sequence the
    attack must issue, and the 1.02x overdraw (DRAW_OVERDRAW) keeps a lazy
    reader from wrapping.
    """
    argv = [
        "vegeta",
        "attack",
        f"-targets={targets_in_container}",
        "-lazy",
        f"-rate={params.rate}",
        f"-duration={params.duration_s}s",
        f"-max-workers={params.max_workers}",
        f"-connections={params.connections}",
        f"-timeout={params.timeout_s}s",
        f"-keepalive={'true' if params.keepalive else 'false'}",
        f"-output={out_bin_in_container}",
    ]
    return argv


def parse_report_proc(result: ProcResult) -> VegetaReport:
    """Parse a completed `vegeta report -type=json` invocation."""
    if not result.ok:
        raise RuntimeError(f"vegeta report failed: {result.stderr.strip()}")
    return VegetaReport.from_json(result.stdout)


def run_report_json(
    *, exec_prefix: list[str], bin_in_container: str
) -> VegetaReport:
    """Invoke `vegeta report -type=json` over a .bin via an exec prefix.

    `exec_prefix` is e.g. ["docker","exec","ledgerline-vegeta-1"]; kept injectable so
    tests pass a no-op prefix and integration passes the real one.
    """
    argv = [*exec_prefix, "vegeta", "report", "-type=json", bin_in_container]
    return parse_report_proc(run(argv))


# --------------------------------------------------------------------------- #
# per-second p99 series (stationarity input for the fit step)
# --------------------------------------------------------------------------- #
def _percentile_ns(sorted_ns: list[int], q: float) -> int:
    """Nearest-rank percentile of an ascending int list (q in [0,1])."""
    if not sorted_ns:
        return 0
    idx = min(len(sorted_ns) - 1, int(q * (len(sorted_ns) - 1) + 0.5))
    return sorted_ns[idx]


def per_second_buckets_from_ndjson(
    lines: "Iterable[str]",
    *,
    qs: tuple[float, ...] = (0.99,),
    reorder_horizon_s: int = 30,
) -> list[tuple[int, tuple[float, ...], int]]:
    """Bucket vegeta `encode --to json` NDJSON into per-second quantiles (ms) + n.

    Each NDJSON record carries `timestamp` (RFC3339, the ISSUE time) and
    `latency` (ns). We bucket by integer second RELATIVE to the earliest record's
    epoch second and emit (second_index, (q_ms, ...), n_samples) ascending: one
    value per requested quantile plus THE SIZE OF THE BUCKET THE QUANTILE WAS
    TAKEN OVER.

    vegeta writes results in COMPLETION order, not issue order: a slow request
    issued in second s lands in the stream after fast requests issued seconds
    later, so consecutive records step backwards across second boundaries
    routinely. (An earlier version assumed issue order and flushed its single
    active bucket on every second change, fragmenting a bucket at every
    inversion.) Buckets are therefore keyed by absolute second, and a bucket is
    finalized only once no record can still join it: a record's completion lags
    its issue second by at most the per-request attack timeout
    (spec/slo.yaml `ladder.attack_timeout_seconds`, 5 s), so once the stream
    has reached issue second t, buckets older than t - `reorder_horizon_s`
    (30 s, a wide margin over the timeout) are final. Memory stays bounded by
    horizon x rate rather than the whole window. A record arriving for an
    already-finalized bucket -- impossible under the timeout invariant --
    raises rather than silently mis-bucketing.

    The count is returned because `_percentile_ns` is NEAREST RANK: which order
    statistic a quantile resolves to is a function of the bucket size, and at
    q=0.999 it is the maximum of the second below 502 samples. A consumer that
    cannot see n cannot know what it is reading (warmup.MIN_SAMPLES_PER_SECOND_P999).
    """
    open_buckets: dict[int, list[int]] = {}
    finalized: dict[int, tuple[tuple[float, ...], int]] = {}
    max_sec: int | None = None

    def _finalize(sec: int) -> None:
        samples = open_buckets.pop(sec)
        samples.sort()
        finalized[sec] = (
            tuple(_percentile_ns(samples, q) / 1e6 for q in qs),
            len(samples),
        )

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        lat = rec.get("latency")
        ts = rec.get("timestamp")
        if lat is None or ts is None:
            continue
        sec_epoch = _epoch_second(ts)
        if sec_epoch is None:
            continue
        if sec_epoch in finalized:
            raise ValueError(
                f"NDJSON record at epoch second {sec_epoch} arrived more than "
                f"{reorder_horizon_s}s out of order -- impossible while the "
                f"attack timeout bounds latency; refusing to mis-bucket it"
            )
        if max_sec is None or sec_epoch > max_sec:
            max_sec = sec_epoch
            for sec in [s for s in open_buckets if s < max_sec - reorder_horizon_s]:
                _finalize(sec)
        open_buckets.setdefault(sec_epoch, []).append(int(lat))
    for sec in list(open_buckets):
        _finalize(sec)
    if not finalized:
        return []
    base_sec = min(finalized)
    return [
        (sec - base_sec, vals, n)
        for sec, (vals, n) in sorted(finalized.items())
    ]


def per_second_quantiles_from_ndjson(
    lines: "Iterable[str]", *, qs: tuple[float, ...] = (0.99,)
) -> list[tuple[int, tuple[float, ...]]]:
    """`per_second_buckets_from_ndjson` without the bucket sizes."""
    return [
        (sec, vals) for sec, vals, _n in per_second_buckets_from_ndjson(lines, qs=qs)
    ]


def per_second_p99_from_ndjson(lines: "Iterable[str]") -> list[tuple[int, float]]:
    """Per-second p99 (ms) series: thin wrapper over the quantile bucketer."""
    return [
        (sec, vals[0])
        for sec, vals in per_second_quantiles_from_ndjson(lines, qs=(0.99,))
    ]


def _epoch_second(ts: str) -> int | None:
    """Integer epoch second from a vegeta RFC3339 timestamp (tolerant)."""
    from datetime import datetime

    s = ts.strip()
    # vegeta emits e.g. 2026-06-06T01:30:00.123456789Z; trim sub-second + Z and
    # parse to epoch seconds. We only need integer-second bucketing.
    try:
        head = s.replace("Z", "+00:00")
        # datetime.fromisoformat accepts <=6 fractional digits; clamp nanos.
        if "." in head:
            date_part, frac = head.split(".", 1)
            off = ""
            for marker in ("+", "-"):
                # offset sign after the fractional part
                pos = frac.find(marker)
                if pos > 0:
                    off = frac[pos:]
                    frac = frac[:pos]
                    break
            head = f"{date_part}.{frac[:6]}{off}"
        return int(datetime.fromisoformat(head).timestamp())
    except (ValueError, TypeError):
        return None


def write_per_second_p99_csv(series: list[tuple[int, float]], path: Path) -> None:
    """Write a `second,p99_ms` CSV (small artifact per probe)."""
    head = "second,p99_ms"
    rows = [f"{sec},{p99:.6f}" for sec, p99 in series]
    paths.write_text_atomic(path, "\n".join([head, *rows]) + "\n")


def write_per_second_quantiles_csv(
    rows: list[tuple[int, int, float, float, int]], path: Path
) -> None:
    """Write a `segment,second,p99_ms,p999_ms,samples` CSV.

    The warmup flatness gates score exactly this series (R1-A2). `segment` is
    kept because warmup runs in segments and each segment's second index
    restarts at 0; concatenating them without it produces a time axis that goes
    backwards. `samples` is the size of the
    one-second bucket each row's quantiles were taken over: B2's calibration
    corpus is built from this file, and at q=0.999 nearest rank the bucket size
    is what says whether the row is a quantile or the second's maximum.
    """
    head = "segment,second,p99_ms,p999_ms,samples"
    body = [
        f"{seg},{sec},{p99:.6f},{p999:.6f},{n}" for seg, sec, p99, p999, n in rows
    ]
    paths.write_text_atomic(path, "\n".join([head, *body]) + "\n")


def encode_per_second_p99(
    *, exec_prefix: list[str], bin_in_container: str
) -> list[tuple[int, float]]:
    """Stream `vegeta encode --to json <bin>` and return per-second p99 (ms).

    `exec_prefix` is e.g. ["docker","exec","-T","ledgerline-vegeta-1"]. Uses a
    streaming subprocess so the NDJSON is consumed line-by-line (memory bounded);
    the .bin itself stays on the bench-io volume (only the small p99 series is
    pulled out).
    """
    return [
        (sec, vals[0])
        for sec, vals in encode_per_second_quantiles(
            exec_prefix=exec_prefix, bin_in_container=bin_in_container, qs=(0.99,)
        )
    ]


def encode_per_second_quantiles(
    *, exec_prefix: list[str], bin_in_container: str, qs: tuple[float, ...] = (0.99,)
) -> list[tuple[int, tuple[float, ...]]]:
    """`encode_per_second_buckets` without the bucket sizes."""
    return [
        (sec, vals)
        for sec, vals, _n in encode_per_second_buckets(
            exec_prefix=exec_prefix, bin_in_container=bin_in_container, qs=qs
        )
    ]


def encode_per_second_buckets(
    *, exec_prefix: list[str], bin_in_container: str, qs: tuple[float, ...] = (0.99,)
) -> list[tuple[int, tuple[float, ...], int]]:
    """Stream `vegeta encode --to json <bin>` into per-second quantiles (ms) + n.

    Same streaming contract as `encode_per_second_p99`, generalized so the
    warmup flatness gates can pull (p99, p999) and the bucket size behind them
    from the warmup .bin in one pass.
    """
    import subprocess

    argv = [*exec_prefix, "vegeta", "encode", "--to", "json", bin_in_container]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    try:
        series = per_second_buckets_from_ndjson(iter(proc.stdout.readline, ""), qs=qs)
    finally:
        proc.stdout.close()
        proc.wait()
    return series
