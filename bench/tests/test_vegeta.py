"""Targets/body generator: mix, pages, payload sizes, determinism, sku, reports.

Implements the spec/workload.yaml assertions the runner must satisfy
(bench/NOTES.md ("Body sizing" and "Zipf")). Uses smoke scale so the Zipf tables build fast.
"""

from __future__ import annotations

import json
import math
import re

import pytest

from ledgerbench import vegeta
from ledgerbench.config import load_workload
from ledgerbench.util.pcg64 import PCG64, stream_for

SKU_RE = re.compile(r"^[A-Z]{2}-[0-9]{4}$")


@pytest.fixture(scope="module")
def wl():
    return load_workload()


@pytest.fixture(scope="module")
def zipf(wl):
    return vegeta.zipf_tables(wl, "smoke")


def _draw(wl, zipf, out_dir, *, count, rng, base_url="", pool=None, n_create=None,
          n_quote=None, seed=12345):
    """A pool + one draw in `out_dir`: the two halves the runner also uses."""
    if pool is None:
        nc, nq = vegeta.body_pool_sizes(wl, count)
        pool = vegeta.generate_body_pool(
            wl, out_dir=out_dir, seed=seed,
            n_create=nc if n_create is None else n_create,
            n_quote=nq if n_quote is None else n_quote,
            scale="smoke", customer_zipf=zipf[0],
        )
    return pool, vegeta.draw_targets(
        wl, out_path=out_dir / "targets.txt", count=count, rng=rng,
        base_url=base_url, pool=pool, customer_zipf=zipf[0], invoice_zipf=zipf[1],
    )


@pytest.fixture(scope="module")
def generated(tmp_path_factory, wl, zipf):
    out = tmp_path_factory.mktemp("targets")
    pool, drawn = _draw(wl, zipf, out, count=8000, rng=PCG64(12345))
    drawn.create_body_bytes = pool.create_body_bytes
    drawn.quote_body_bytes = pool.quote_body_bytes
    return drawn


def test_mix_proportions_within_tolerance(generated, wl):
    total = sum(generated.endpoint_counts.values())
    assert total == 8000
    for name, weight in wl.mix.as_pairs():
        observed = generated.endpoint_counts[name] / total
        expected = weight / 100.0
        # 2 percentage points absolute tolerance at n=8000.
        assert abs(observed - expected) < 0.02, (name, observed, expected)


def test_create_body_sizes_within_tolerance(generated):
    spec_target = 2048
    tol = 512
    assert generated.create_body_bytes  # some were generated
    for b in generated.create_body_bytes:
        assert spec_target - tol <= b <= spec_target + tol, b


def test_quote_body_sizes_within_tolerance(generated):
    spec_target = 8192
    tol = 1024
    assert generated.quote_body_bytes
    for b in generated.quote_body_bytes:
        assert spec_target - tol <= b <= spec_target + tol, b


def test_same_probe_identity_reproduces_byte_identically(tmp_path, wl, zipf):
    """The resume guarantee, and the INVERSE of what this file used to pin.

    Until W20 the contract asserted here was "one seed -> one byte-identical
    targets file", which is exactly the defect: every attack of every probe drew
    the same file, so the warmup populated the cache with the ids the measured
    window was about to ask for (R1-A7). The reproducibility that has to survive
    is narrower -- the same PROBE IDENTITY reproduces its own file -- because a
    resume must redraw what the crashed attempt drew, not a fresh sequence.
    """
    labels = ("run-7", "headline.mixed.gc-default", "ladder", "500", "0", "measured")
    inc = stream_for(12345, *labels)
    _, a = _draw(wl, zipf, tmp_path / "a", count=3000, rng=PCG64(12345, inc))
    _, b = _draw(wl, zipf, tmp_path / "b", count=3000, rng=PCG64(12345, inc))
    assert a.targets_path.read_bytes() == b.targets_path.read_bytes()
    assert a.endpoint_counts == b.endpoint_counts
    assert a.inc == b.inc == inc


def test_probe_draw_differs_across_blocks_and_phases(tmp_path, wl, zipf):
    """Two probes that differ in ANY label face different request sequences.

    Each of these is a real collision round 1 had: block is the RCB repeat, so
    two blocks at one rung replayed one another; phase is the attack, so warmup
    and measured replayed one another; rate is the rung, so the whole ladder
    replayed one prefix.
    """
    base = ("run-7", "headline.mixed.gc-default", "ladder", "500", "0", "measured")
    files = {}
    for i, changed in enumerate([
        base,
        (*base[:3], "600", *base[4:]),        # a different rung
        (*base[:4], "1", base[5]),            # a different RCB block
        (*base[:5], "warmup.seg0"),           # a different attack
        (*base[:5], "discard"),
        ("run-8", *base[1:]),                 # a different run
        (base[0], "headline.mixed.gc-generous", *base[2:]),   # a different cell
        (*base[:2], "confirm", *base[3:]),    # a different stage
    ]):
        _, drawn = _draw(wl, zipf, tmp_path / f"d{i}", count=800,
                         rng=PCG64(12345, stream_for(12345, *changed)))
        files[changed] = drawn.targets_path.read_bytes()
    assert len(set(files.values())) == len(files)


def test_probe_draw_is_identical_for_both_slots(tmp_path, wl, zipf):
    """The RCB pairing contract: `slot` is NOT one of the stream labels.

    Both halves of a block must face the identical request sequence -- that is
    what makes the pair a pair -- so the ONLY difference between their files is
    the base URL. Adding `slot` to the labels would break the pairing while
    every other test here still passed.
    """
    inc = stream_for(12345, "run-7", "cell", "ladder", "500", "0", "measured")
    pool, java = _draw(wl, zipf, tmp_path / "java", count=1500,
                       rng=PCG64(12345, inc), base_url="http://sut-java:8080")
    _, dotnet = _draw(wl, zipf, tmp_path / "dotnet", count=1500,
                      rng=PCG64(12345, inc), base_url="http://sut-dotnet:8080",
                      pool=pool)
    a = java.targets_path.read_text().replace("http://sut-java:8080", "")
    b = dotnet.targets_path.read_text().replace("http://sut-dotnet:8080", "")
    assert a == b
    assert java.invoice_ids == dotnet.invoice_ids


def test_separate_streams_remove_the_prefix_prewarm_not_the_zipf_head(tmp_path, wl, zipf):
    """What the per-attack draw actually achieves -- and what it CANNOT.

    Round 1 pointed the warmup, the head-of-window discard and the measured
    attack at ONE targets file, and vegeta reads a targets file from the top on
    every invocation. So the warmup -- which runs immediately after a Redis
    FLUSHALL -- did not merely leave a warm cache: it populated the cache with
    exactly the invoice ids the measured window was about to request, in order.
    Separate PCG64 streams retire THAT, and this test pins it: the warmup's ids
    are no longer a subset of the measured window's.

    They retire nothing else. Both streams are Zipf(alpha=1.0) draws over the
    SAME keyspace, so the hot head appears in both by construction;
    `docs/p1-plan.md` 5.1 says exactly this as the reason `hot_set_touched`
    could not be rescued as a gate.
    The residual is real and large, and it is REQUEST-WEIGHTED: the head is a
    few ids and most of the traffic, so a set-wise ratio understates it several
    fold. That is why `vegeta.prewarm_containment` counts requests, and why the
    manifest and the report publish the fraction instead of denying it.
    """
    def drawn(labels, count):
        _, d = _draw(wl, zipf, tmp_path / "_".join(labels), count=count,
                     rng=PCG64(12345, stream_for(12345, *labels)))
        return d

    base = ("run-7", "cell", "ladder", "500", "0")
    warm = drawn((*base, "warmup.seg0"), 4000)
    measured = drawn((*base, "measured"), 12000)

    # THE fix: the warmup's ids are not a prefix of the measured window's.
    assert not set(warm.invoice_ids) <= set(measured.invoice_ids)
    assert len(set(warm.invoice_ids) & set(measured.invoice_ids)) / len(warm.invoice_ids) < 0.8

    # THE residual, stated as the measurement that is published. Request-weighted
    # it is a large minority even at this toy scale, and strictly larger than the
    # set-wise ratio computed over the same two draws.
    # Every invoice-id read is COUNTED, not merely seen: a draw that recorded
    # ids as a set (which is what this carried before) would make the published
    # fraction set-wise and understate the read path's residency several fold.
    # Asserted against the endpoint mix, so the check is tight on BOTH read
    # routes rather than on whichever one the test happens to exercise.
    for d in (warm, measured):
        assert d.invoice_read_requests == (
            d.endpoint_counts["get_invoice"] + d.endpoint_counts["pdf_stub"]
        ) > len(d.invoice_ids)

    c = vegeta.prewarm_containment(measured.invoice_id_reads, warm.invoice_ids)
    assert c.requests == measured.invoice_read_requests > 0
    assert c.prewarmed_ids == len(warm.invoice_ids)
    set_wise = (len(set(warm.invoice_ids) & set(measured.invoice_ids))
                / len(set(measured.invoice_ids)))
    assert 0.1 < c.fraction < 1.0
    assert c.fraction > set_wise

    # The defect itself, for contrast: all three attacks of a round-1 probe read
    # ONE targets file from the top, so the warmup consumed a PREFIX of the
    # sequence the measured window then consumed whole.
    #
    # The argument order is the whole assertion. `prewarm_containment(measured,
    # prewarmed)` scores the MEASURED window's read requests against the ids an
    # earlier attack drew; passing the prefix as the measured window and the
    # whole file as the pre-warm asserts that a set contains its own subset,
    # which is 1.0 for any two draws in that relation and says nothing about
    # round 1.
    whole = drawn((*base, "prefix-demo"), 12000)
    prefix = drawn((*base, "prefix-demo"), 4000)
    assert set(prefix.invoice_ids) < set(whole.invoice_ids)
    round1 = vegeta.prewarm_containment(whole.invoice_id_reads, prefix.invoice_ids)
    reversed_args = vegeta.prewarm_containment(prefix.invoice_id_reads, whole.invoice_ids)
    assert reversed_args.fraction == 1.0        # trivially, and for any prefix
    assert round1.fraction < 1.0                # the quantity actually at issue

    # 1.0 is RATE-DEPENDENT, not a property of the arrangement, which is why the
    # absolute belongs nowhere. The prefix has a fixed length (the warmup rate x
    # the cap) and the measured window's length grows with the rung, so round 1
    # scored exactly 1.0 wherever the warmup outran the window and less above
    # it. Measured on this box at full scale with the harness's real warmup rate
    # of 618 rps over the 300 s cap -- a 189,108-target prefix -- round 1's
    # arrangement gives 100.0% at 500 and 1,244 rps, 76.5% at 3,715, 66.0% at
    # 10,000 and 64.4% at 13,312 rps.
    short_window = drawn((*base, "prefix-demo"), 2000)
    assert vegeta.prewarm_containment(
        short_window.invoice_id_reads, prefix.invoice_ids).fraction == 1.0
    # ...and it stays strictly above the separate-stream containment scored on
    # the same measured window, which is the comparison the fix is judged by.
    assert round1.fraction > c.fraction


def test_prewarm_containment_is_request_weighted_and_states_its_own_scope():
    """The unit that makes the published fraction mean what it says.

    Two failure modes it exists to exclude. Counting DISTINCT ids instead of
    requests reports the head as one hit and understates residency by orders of
    magnitude -- the head is a handful of ids and most of the traffic. And a
    draw that asked for no invoice id at all must report `None`, never 0.0: an
    unmeasured window and a window nothing pre-warmed are different findings,
    and the field this replaces was an asserted zero.
    """
    from collections import Counter

    # one hot id (900 requests) pre-warmed, 100 cold ids at one request each
    measured = Counter({7: 900, **{i: 1 for i in range(100, 200)}})
    c = vegeta.prewarm_containment(measured, {7})
    assert (c.requests, c.hits, c.prewarmed_ids) == (1000, 900, 1)
    assert c.fraction == 0.9
    # the set-wise figure over the same data, for contrast: 1 of 101 ids
    assert 1 / len(measured) < 0.01

    assert vegeta.prewarm_containment(Counter(), {7}).fraction is None
    assert vegeta.prewarm_containment(measured, []).fraction == 0.0
    # any collection of ids works, and a list is not scanned per id
    assert vegeta.prewarm_containment(measured, [7, 7]).hits == 900


def test_no_wrap_within_a_probe():
    """`probe_draw_count` overdraws, which is the other half of the R1-A7 fix.

    Drawing per probe is not enough on its own: a 120 s window at 13,312 rps
    issues 1.6M requests, and a file sized from the old 1,000,000 working-set
    dial wraps inside its own measured window.
    """
    assert vegeta.probe_draw_count(13312, 120) > 13312 * 120
    assert vegeta.probe_draw_count(500, 120) == math.ceil(500 * 120 * 1.02)
    # a zero-length or zero-rate attack still gets a file vegeta can open
    assert vegeta.probe_draw_count(0, 120) == 1


def test_a_single_probe_draw_preserves_the_mix_and_the_zipf_skew(tmp_path, wl, zipf):
    """The shape assertions have to hold WITHIN one draw now, not across a file.

    Until W20 the mix and the id skew were properties of the one targets file a
    run replayed, and they were asserted over the whole of it. A probe consumes
    a draw of its own, so a draw that quietly destroyed the workload shape --
    say by drawing bodies and ids from one exhausted stream -- would still have
    passed a whole-file assertion.
    """
    _, drawn = _draw(wl, zipf, tmp_path, count=30000, rng=PCG64(999))
    total = sum(drawn.endpoint_counts.values())
    assert total == 30000
    for name, weight in wl.mix.as_pairs():
        assert abs(drawn.endpoint_counts[name] / total - weight / 100.0) < 0.02, name

    # Zipf(alpha=1.0) over the smoke corpus: rank 1 is the hottest id and the
    # head dominates. Counted from the drawn URLs, so this scores the DRAW and
    # not the table it sampled from.
    ranks = [
        int(ln.rsplit("/invoices/", 1)[1].split("/")[0])
        for ln in drawn.targets_path.read_text().splitlines()
        if "/invoices/" in ln and ln.startswith("GET ")
    ]
    assert ranks
    top10 = sum(1 for r in ranks if r <= 10) / len(ranks)
    exact = sum(zipf[1].probability(r) for r in range(1, 11))
    assert abs(top10 - exact) < 0.02, (top10, exact)
    assert min(ranks) == 1


def test_the_draw_accounts_the_customers_its_create_bodies_carry(tmp_path, wl, zipf):
    """The create path carries its customer id in the BODY, not in the URL.

    Round 1's working-set accounting recovered those ids by re-reading every
    referenced body file off disk after the fact. The draw already holds them --
    it chose the body -- so it carries them out, and the manifest's per-probe
    customer spread is exactly as good as that accounting. Checked against the
    POOL (ground truth for what each ref means) and against the FILE (what
    vegeta will actually send), so a draw that reported an unrelated set, or an
    empty one, fails here rather than in a manifest nobody diffs.
    """
    pool, drawn = _draw(wl, zipf, tmp_path, count=4000, rng=PCG64(31337))
    by_ref = dict(zip(pool.create_refs, pool.create_customer_ids, strict=True))
    refs = [
        ln[1:] for ln in drawn.targets_path.read_text().splitlines()
        if ln.startswith("@") and "create_" in ln
    ]
    assert refs
    assert drawn.customer_ids_create_bodies == {by_ref[r] for r in refs}
    assert drawn.distinct_customer_ids == len(
        drawn.customer_ids_list_urls | drawn.customer_ids_create_bodies
    )
    assert drawn.distinct_customer_ids > len(drawn.customer_ids_list_urls)


def test_stream_for_inc_is_always_odd(wl):
    """An even increment collapses the LCG's period; PCG64 requires odd.

    `PCG64.__init__` forces the low bit set for the published default, so a
    `stream_for` that returned an even value would be silently corrected and
    two label sets differing only in that bit would collide onto ONE stream --
    which is the pairing/independence contract, quietly broken.
    """
    seen = set()
    for i in range(400):
        inc = stream_for(12345, "run", "cell", "ladder", str(i), "0", "measured")
        assert inc % 2 == 1
        assert inc < (1 << 128)
        seen.add(inc)
        assert PCG64(12345, inc).inc == inc
    assert len(seen) == 400
    # the seed participates too: the same labels under a different seed select a
    # different STREAM, not merely a different position in one stream
    labels = ("run", "cell", "ladder", "500", "0", "measured")
    assert stream_for(1, *labels) != stream_for(2, *labels)
    # and the label boundary is real: ("a","bc") must not digest as ("ab","c")
    assert stream_for(1, "a", "bc") != stream_for(1, "ab", "c")


def test_sku_format(wl):
    rng = PCG64(5)
    cats = vegeta._sku_categories(wl.payloads.create_invoice.fields)
    for _ in range(5000):
        sku = vegeta.gen_sku(rng, cats)
        assert SKU_RE.match(sku), sku
        assert sku[:2] in cats


def test_quote_has_exactly_50_items(tmp_path, wl):
    rng = PCG64(11)
    body = vegeta.gen_quote_body(rng, wl)
    obj = json.loads(body)
    assert len(obj["items"]) == 50
    assert obj["currency"] == "USD"
    for it in obj["items"]:
        assert set(it.keys()) == {"sku", "qty", "unit_price_minor"}
        assert SKU_RE.match(it["sku"])
        assert 1 <= it["qty"] <= 200
        assert 100 <= it["unit_price_minor"] <= 1_000_000


def test_create_body_schema(tmp_path, wl):
    rng = PCG64(13)
    body = vegeta.gen_create_body(rng, wl, customer_id=42)
    obj = json.loads(body)
    assert obj["customer_id"] == 42
    assert obj["currency"] == "USD"
    assert obj["status"] in ("draft", "open")
    assert 3 <= len(obj["items"]) <= 8
    for it in obj["items"]:
        assert SKU_RE.match(it["sku"])
        assert 1 <= len(it["description"]) <= 200
        assert 1 <= it["qty"] <= 50
        assert 100 <= it["unit_price_minor"] <= 5_000_000


def test_page_distribution_skew(tmp_path, wl, zipf):
    # All list_invoices targets carry ?page=N; page 1 should dominate (~0.70).
    _, gt = _draw(wl, zipf, tmp_path, count=20000, rng=PCG64(12345))
    pages = []
    for line in gt.targets_path.read_text().splitlines():
        if line.startswith("GET ") and "/invoices?page=" in line:
            pages.append(int(line.rsplit("=", 1)[1]))
    assert pages
    frac1 = pages.count(1) / len(pages)
    assert abs(frac1 - 0.70) < 0.03
    assert all(1 <= p <= 5 for p in pages)


def test_targets_file_shape(generated):
    text = generated.targets_path.read_text()
    lines = text.splitlines()
    # Every GET line begins with a method token; body refs use @bodies/.
    methods = {ln.split(" ", 1)[0] for ln in lines if ln and not ln.startswith(("@", "Content-Type"))}
    assert methods <= {"GET", "POST"}
    assert any(ln.startswith("@bodies/") for ln in lines)


# --------------------------- report parsing -------------------------------- #
def test_vegeta_report_parsing():
    sample = {
        "requests": 6000,
        "rate": 199.8,
        "throughput": 199.5,
        "duration": 30_000_000_000,
        "success": 0.999,
        "status_codes": {"200": 5994, "500": 6},
        "latencies": {"mean": 1_000_000, "50th": 800_000, "99th": 5_000_000, "max": 9_000_000},
        "errors": [],
    }
    rep = vegeta.VegetaReport.from_json(json.dumps(sample))
    assert rep.requests == 6000
    assert rep.latency_p99_ns == 5_000_000
    assert abs(rep.error_rate - 0.001) < 1e-9
    assert rep.status_codes["500"] == 6


def test_achieved_vs_offered():
    rep = vegeta.VegetaReport(
        requests=100, rate=198.0, throughput=198.0, duration_ns=1, success_ratio=1.0,
        status_codes={}, latency_mean_ns=0, latency_p50_ns=0, latency_p99_ns=0,
        latency_max_ns=0, errors=[],
    )
    ok, ratio = vegeta.assert_achieved_vs_offered(rep, 200.0, tolerance_pct=2.0)
    assert ok and abs(ratio - 0.99) < 1e-9
    bad, ratio2 = vegeta.assert_achieved_vs_offered(rep, 250.0, tolerance_pct=2.0)
    assert not bad


def test_attack_argv_pins_params():
    params = vegeta.AttackParams(rate=4000, duration_s=120, max_workers=200, connections=200, timeout_s=5.0)
    argv = vegeta.build_attack_argv(
        targets_in_container="/io/targets.txt", out_bin_in_container="/io/out.bin", params=params
    )
    joined = " ".join(argv)
    assert "-rate=4000" in joined
    assert "-duration=120s" in joined
    assert "-keepalive=true" in joined
    assert "-timeout=5.0s" in joined
    assert "-output=/io/out.bin" in joined


def test_per_second_p99_buckets_by_integer_second():
    # three records in second 0, two in second 1 (RFC3339 + ns latency).
    ndjson = [
        '{"timestamp":"2026-06-06T01:30:00.100000000Z","latency":1000000}',
        '{"timestamp":"2026-06-06T01:30:00.500000000Z","latency":2000000}',
        '{"timestamp":"2026-06-06T01:30:00.900000000Z","latency":9000000}',
        '{"timestamp":"2026-06-06T01:30:01.100000000Z","latency":3000000}',
        '{"timestamp":"2026-06-06T01:30:01.900000000Z","latency":4000000}',
        "",  # blank line tolerated
    ]
    series = vegeta.per_second_p99_from_ndjson(iter(ndjson))
    secs = [s for s, _ in series]
    assert secs == [0, 1]
    # second 0 p99 (nearest-rank of [1,2,9] ms) -> the 9 ms tail
    assert abs(series[0][1] - 9.0) < 1e-6
    # second 1 p99 of [3,4] ms -> 4 ms
    assert abs(series[1][1] - 4.0) < 1e-6


def test_per_second_p99_csv_roundtrip(tmp_path):
    series = [(0, 9.0), (1, 4.0)]
    path = tmp_path / "p99.csv"
    vegeta.write_per_second_p99_csv(series, path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "second,p99_ms"
    assert lines[1].startswith("0,9.0")
    assert lines[2].startswith("1,4.0")


def test_warmup_quantile_csv_keeps_the_segment_index(tmp_path):
    """The series the flatness gates actually score, written for the first time.

    Round 1 built this series every warmup segment, fed it straight to the
    gates, and never wrote it — so the gate that rejected 59.5% of its windows
    was tuned against data that exists in no artifact, and B2's recalibration
    has nothing to recalibrate against. The segment index has to survive:
    each segment's second index restarts at 0, so concatenating without it
    produces a time axis that goes backwards.
    """
    rows = [(0, 0, 9.0, 21.0, 618), (0, 1, 8.5, 20.0, 617), (1, 0, 8.4, 19.5, 300)]
    path = tmp_path / "warmup_q.csv"
    vegeta.write_per_second_quantiles_csv(rows, path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "segment,second,p99_ms,p999_ms,samples"
    assert lines[1].startswith("0,0,9.0")
    assert lines[3].startswith("1,0,8.4")
    # the bucket SIZE survives too: without it a q=0.999 row cannot be told
    # from that second's maximum (warmup.MIN_SAMPLES_PER_SECOND_P999), and the
    # last row here -- a partial boundary second of 300 samples -- is exactly
    # the case where q=0.999 IS the maximum.
    assert [int(x.split(",")[4]) for x in lines[1:]] == [618, 617, 300]
    # the second column alone is NOT monotone; segment+second is the ordering
    seconds = [int(x.split(",")[1]) for x in lines[1:]]
    assert seconds == [0, 1, 0]


def test_per_second_p99_skips_malformed_lines():
    ndjson = ["not json", '{"latency":5}', '{"timestamp":"2026-06-06T01:30:00Z","latency":7000000}']
    series = vegeta.per_second_p99_from_ndjson(iter(ndjson))
    assert series == [(0, 7.0)]


# --------------------------------------------------------------------------- #
# spec/workload.yaml keyspaces: a CHECKED contract, not a declaration
# --------------------------------------------------------------------------- #
def test_the_spec_keyspace_block_matches_the_generator(wl):
    """`keyspaces` had no production reader once `seeded_range` stopped being
    read. Under CLAUDE.md's governing rule that made it a
    declared-but-unimplemented spec block, so `seeded_range` is deleted and what
    remains -- which endpoint draws from which keyspace, and how -- is read."""
    vegeta.assert_keyspace_contract(wl)
    declared = {
        e: name
        for name in ("customers", "invoices")
        for e in getattr(wl.keyspaces, name).used_by
    }
    assert declared == vegeta.KEYSPACE_BY_ENDPOINT
    # `seeded_range` is gone from BOTH layers, and both halves have to be
    # asserted: the model field alone would let the spec keep an ignored key,
    # and the spec text alone would let the model keep an unfilled field.
    import yaml

    from ledgerbench.config import Keyspace
    from ledgerbench.util import paths

    assert "seeded_range" not in Keyspace.model_fields
    raw = yaml.safe_load(
        (paths.repo_root() / "spec" / "workload.yaml").read_text(encoding="utf-8")
    )
    assert set(raw["keyspaces"]) == {"customers", "invoices"}
    for ks in raw["keyspaces"].values():
        assert "seeded_range" not in ks


def test_an_endpoint_moved_between_keyspaces_refuses_to_generate(wl, tmp_path):
    """The drift this reader exists to catch: a targets file is the run's working
    set, and a wrong one is not detectable from the artifacts afterwards."""
    drifted = wl.model_copy(deep=True)
    drifted.keyspaces.invoices.used_by = ["get_invoice"]      # pdf_stub moved away
    with pytest.raises(ValueError, match="used_by declares"):
        vegeta.assert_keyspace_contract(drifted)
    with pytest.raises(ValueError, match="used_by declares"):
        vegeta.generate_body_pool(drifted, out_dir=tmp_path, seed=1, n_create=2,
                                  n_quote=2, scale="smoke")


def test_a_selection_this_generator_cannot_draw_refuses_to_generate(wl):
    drifted = wl.model_copy(deep=True)
    drifted.keyspaces.customers.selection = "uniform"
    with pytest.raises(ValueError, match="implements only 'zipf'"):
        vegeta.assert_keyspace_contract(drifted)


def test_the_per_second_bucketer_reports_the_size_of_each_bucket():
    """W21: which order statistic a nearest-rank quantile resolved to is a
    function of the bucket size, so the size travels with the quantile.

    `_percentile_ns` is `idx = min(n-1, int(q*(n-1)+0.5))`. At q=0.999 that is
    the MAXIMUM of the second for n <= 501 and the second-largest for
    502 <= n <= 1501 -- so a consumer that cannot see n cannot know whether it is
    reading a tail quantile or one sample. `warmup.MIN_SAMPLES_PER_SECOND_P999`
    is the rule built on top of this, and it needs a measured n, not a nominal
    rate.
    """
    ndjson = [
        '{"timestamp":"2026-06-06T01:30:00.100000000Z","latency":1000000}',
        '{"timestamp":"2026-06-06T01:30:00.500000000Z","latency":2000000}',
        '{"timestamp":"2026-06-06T01:30:00.900000000Z","latency":9000000}',
        '{"timestamp":"2026-06-06T01:30:01.100000000Z","latency":3000000}',
        '{"timestamp":"2026-06-06T01:30:01.900000000Z","latency":4000000}',
    ]
    buckets = vegeta.per_second_buckets_from_ndjson(iter(ndjson), qs=(0.99, 0.999))
    assert [(sec, n) for sec, _v, n in buckets] == [(0, 3), (1, 2)]
    # and the quantiles are unchanged by carrying the count
    assert vegeta.per_second_quantiles_from_ndjson(iter(ndjson), qs=(0.99, 0.999)) == [
        (sec, vals) for sec, vals, _n in buckets
    ]
    # at n=3, q=0.999 IS the maximum of the second (docs/p1-plan.md 5.2)
    assert buckets[0][1][1] == pytest.approx(9.0)


def test_the_bucketer_survives_completion_order_ndjson():
    """vegeta writes results in COMPLETION order: a slow request issued in
    second 0 lands in the stream after fast requests issued in second 1. The
    original single-active-bucket flush fragmented second 0 into two rows at
    that inversion (rows [(0,2),(1,1),(0,1),(1,1)] under the old code); the
    keyed bucketer must produce one row per second regardless of arrival
    order, and the base second must be the EARLIEST issue second seen, not the
    first record's."""
    completion_order = [
        # issued @1 but completed first (fast)
        '{"timestamp":"2026-06-06T01:30:01.100000000Z","latency":1000000}',
        '{"timestamp":"2026-06-06T01:30:00.100000000Z","latency":1200000000}',
        '{"timestamp":"2026-06-06T01:30:01.900000000Z","latency":2000000}',
        # issued @0, 2.1 s latency, completes after everything above
        '{"timestamp":"2026-06-06T01:30:00.500000000Z","latency":2100000000}',
    ]
    buckets = vegeta.per_second_buckets_from_ndjson(iter(completion_order), qs=(0.99,))
    assert [(sec, n) for sec, _v, n in buckets] == [(0, 2), (1, 2)]
    # identical input in issue order gives the identical series
    issue_order = sorted(completion_order)  # ISO timestamps sort chronologically
    assert buckets == vegeta.per_second_buckets_from_ndjson(iter(issue_order), qs=(0.99,))


def test_the_bucketer_refuses_a_record_beyond_the_reorder_horizon():
    """A record more than `reorder_horizon_s` older than the newest issue
    second seen is impossible while the attack timeout bounds latency
    (spec/slo.yaml ladder.attack_timeout_seconds = 5 s); mis-bucketing it
    silently would corrupt the flatness-gate series, so the bucketer raises."""
    stream = [
        '{"timestamp":"2026-06-06T01:30:00.000000000Z","latency":1000000}',
        # max advances 60 s -> the 01:30:00 bucket is finalized and emitted
        '{"timestamp":"2026-06-06T01:31:00.000000000Z","latency":1000000}',
        # a straggler for the finalized bucket cannot be merged any more
        '{"timestamp":"2026-06-06T01:30:00.500000000Z","latency":1000000}',
    ]
    with pytest.raises(ValueError, match="out of order"):
        vegeta.per_second_buckets_from_ndjson(iter(stream), qs=(0.99,), reorder_horizon_s=30)
