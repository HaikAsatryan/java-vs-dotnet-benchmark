"""Smoke entry (Windows-ok). Functional only, resource accounting OFF.

The flow, over the active slot set (registry.active_slot_set):
  1. build every slot's service + vegeta + seed (base compose, NO resource overlay).
  2. seed smoke scale via the seed container (applies spec/schema.sql, grants,
     COPY-loads seed-42 data, restarts sequences, runs invariants).
  3. up ALL SUTs + vegeta together (base compose; cross-service equivalence
     needs them live against the same DB) and poll /readyz on the published
     ports.
  4. equivalence: bench/validation pytest with each slot's `url_env`.
  5. CO stall sanity: freeze the first slot's service for
     spec/slo.yaml `co_validation.stall_test.freeze_seconds` by the method that
     block names, unfreeze, /healthz must answer.
  6. per-SUT 30 s probe at low rate via the vegeta container (in-network URLs,
     smoke Zipf seed); assert 2xx + achieved ~= offered. Latency numbers are
     smoke-only, never measurement-grade.
  7. teardown: down -v.

Resource accounting is OFF (cgroups.py records "disabled (non-Linux)" on
Windows). Output is labeled smoke-only.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import compose, registry, vegeta
from .compose import ComposeEnv
from .config import StallTest, Workload, load_slo, load_workload
from .util import paths
from .util.clock import rfc3339_micros
from .util.pcg64 import PCG64
from .util.proc import run

SMOKE_SCALE = "smoke"
SMOKE_PROBE_RATE = 50          # low fixed rate
SMOKE_PROBE_DURATION_S = 30
SMOKE_TARGET_COUNT = 5000      # pre-expanded targets list for the 30 s probe
SMOKE_ACHIEVED_TOLERANCE_PCT = 5.0   # relaxed vs slo co_validation 2% (smoke-only label)
SMOKE_MIN_SUCCESS_RATIO = 0.999

def _slots() -> tuple[registry.Slot, ...]:
    """The two SUTs this smoke run exercises, from the active slot set.

    Resolved lazily, never at import: `active_slot_set` raises when a slot's
    build context does not exist yet, and that must surface when a run starts,
    not when a test imports this module.
    """
    return tuple(registry.get_slot(s) for s in registry.active_slot_set().slots)


@dataclass
class SmokeStep:
    name: str
    ok: bool
    detail: str = ""
    deferred: bool = False


@dataclass
class SmokeReport:
    label: str = "smoke-only"
    resource_accounting: str = "disabled (non-Linux)" if not sys.platform.startswith("linux") else "off (smoke)"
    started_utc: str = field(default_factory=rfc3339_micros)
    steps: list[SmokeStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps if not s.deferred)


def smoke_targets(
    wl: Workload, *, out_dir: Path, seed: int, base_url: str, scale: str = SMOKE_SCALE
) -> vegeta.DrawnTargets:
    """Generate probe targets with the smoke Zipf seed and an in-network base URL.

    One pool and one draw in the same directory: the smoke probe is a single
    attack, so the per-probe stream derivation the measured runner uses buys
    nothing here and the plain seed keeps the dry run reproducible.
    """
    n_create, n_quote = vegeta.body_pool_sizes(wl, SMOKE_TARGET_COUNT)
    customer_zipf, invoice_zipf = vegeta.zipf_tables(wl, scale)
    pool = vegeta.generate_body_pool(
        wl, out_dir=out_dir, seed=seed, n_create=n_create, n_quote=n_quote,
        scale=scale, customer_zipf=customer_zipf,
    )
    return vegeta.draw_targets(
        wl, out_path=out_dir / "targets.txt", count=SMOKE_TARGET_COUNT,
        rng=PCG64(seed), base_url=base_url, pool=pool,
        customer_zipf=customer_zipf, invoice_zipf=invoice_zipf,
    )


def build_argv() -> list[str]:
    env = ComposeEnv.base()
    services = [s.service for s in _slots()]
    # dict.fromkeys, not set(): two slots of one language share a service
    # (the shakedown pair differ only by DATA_LAYER), and `compose build` must
    # not be handed the same service twice.
    return compose.build_argv(
        env, [*dict.fromkeys(services), compose.SVC_VEGETA, compose.SVC_SEED]
    )


def seed_argv(*, scale: str = SMOKE_SCALE, seed: int = 42) -> list[str]:
    """One-shot seeder: schema + grants + truncate + COPY + invariants."""
    env = ComposeEnv.base(profiles=[compose.PROFILE_SEED])
    return compose.run_oneshot_argv(
        env,
        compose.SVC_SEED,
        [
            "--dsn", "postgres://postgres:postgres@postgres:5432/ledgerline",
            "--scale", scale,
            "--seed", str(seed),
            "--schema", "/spec/schema.sql",
            "--grants",
            "--truncate",
            "--mode", "copy",
        ],
    )


def up_argv() -> list[str]:
    slots = _slots()
    env = ComposeEnv.base(
        profiles=[*dict.fromkeys(s.profile for s in slots), compose.PROFILE_LOAD]
    )
    return compose.up_wait_argv(
        env, [*dict.fromkeys(s.service for s in slots), compose.SVC_VEGETA]
    )


def teardown_argv() -> list[str]:
    env = ComposeEnv.base(
        profiles=[*dict.fromkeys(s.profile for s in _slots()),
                  compose.PROFILE_LOAD, compose.PROFILE_SEED]
    )
    return compose.down_argv(env, volumes=True)


def equivalence_argv() -> list[str]:
    return [sys.executable, "-m", "pytest", "validation/tests", "-q", "--no-header"]


# spec/slo.yaml `co_validation.stall_test.method` -> the compose verb that
# performs it. Both the freeze duration and the method were literals here while
# the spec declared them one directory away, so an edit to spec/ changed nothing
# and the two could disagree without any check noticing. An unimplemented method
# now RAISES: a CO check must not silently perform something other than what
# was pre-registered.
_STALL_VERBS = {"docker_pause": "pause"}


def _stall_verb(st: StallTest) -> str:
    try:
        return _STALL_VERBS[st.method]
    except KeyError:
        raise ValueError(
            f"spec/slo.yaml co_validation.stall_test.method={st.method!r} is not "
            f"implemented (known: {', '.join(sorted(_STALL_VERBS))})"
        ) from None


def _stall_plan_line() -> str:
    st = load_slo().co_validation.stall_test
    return (
        f"# compose {_stall_verb(st)} {_slots()[0].service} ; "
        f"sleep {st.freeze_seconds} ; unpause ; /healthz check"
    )


def run_stall_check() -> SmokeStep:
    """Freeze the first slot for the declared interval; it must answer after.

    Split out of `run_smoke` so the EXECUTED path is reachable without a docker
    daemon. Inline, the only tests that could reach the spec-derived duration
    and verb were the dry-run plan line -- and a plan line that reads the spec
    while the executed step uses literals is precisely the drift this closes.
    """
    st = load_slo().co_validation.stall_test
    slot = _slots()[0]
    env = ComposeEnv.base(profiles=[slot.profile])
    paused = compose.compose(env, [_stall_verb(st), slot.service], check=False)
    time.sleep(st.freeze_seconds)
    unpaused = compose.compose(env, ["unpause", slot.service], check=False)
    healthz_ok = False
    if paused.ok and unpaused.ok:
        try:
            healthz_ok = httpx.get(
                f"{slot.host_url}/healthz", timeout=10.0).status_code == 200
        except httpx.HTTPError:
            healthz_ok = False
    return SmokeStep(
        name="co_stall",
        ok=paused.ok and unpaused.ok and healthz_ok,
        detail=f"{st.method} {st.freeze_seconds}s on {slot.service}",
    )


def plan() -> list[str]:
    """Human-readable ordered command plan for the smoke flow (no execution)."""
    return [
        " ".join(build_argv()),
        " ".join(seed_argv()),
        " ".join(up_argv()),
        "# poll /readyz on " + " + ".join(s.host_url for s in _slots()),
        f"# {' '.join(equivalence_argv())}  "
        + "(env " + "/".join(s.url_env for s in _slots()) + ")",
        _stall_plan_line(),
        f"# per-SUT: vegeta attack @{SMOKE_PROBE_RATE} rps x {SMOKE_PROBE_DURATION_S}s via /io/<slot> (in-network)",
        " ".join(teardown_argv()),
    ]


def wait_ready(base_url: str, *, timeout_s: float = 120.0) -> bool:
    """Poll /readyz until 200 (the SUT images carry no container healthcheck)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/readyz", timeout=3.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def _probe(report: SmokeReport, wl: Workload, lang: str, net_url: str, io_dir: Path, seed: int) -> None:
    """Generate targets, ship them into the vegeta container, attack, assert."""
    env = ComposeEnv.base(profiles=[compose.PROFILE_LOAD])
    smoke_targets(wl, out_dir=io_dir / lang, seed=seed, base_url=net_url)

    res = compose.exec_in(env, compose.SVC_VEGETA, ["sh", "-c", f"mkdir -p /io/{lang}"], check=False)
    cp = compose.compose(
        env, ["cp", str(io_dir / lang) + "/.", f"{compose.SVC_VEGETA}:/io/{lang}/"], check=False, timeout=300
    )
    if not (res.ok and cp.ok):
        report.steps.append(SmokeStep(name=f"probe_{lang}_ship", ok=False, detail=(res.stderr + cp.stderr)[-400:]))
        return

    params = vegeta.AttackParams(
        rate=SMOKE_PROBE_RATE,
        duration_s=SMOKE_PROBE_DURATION_S,
        max_workers=64,
        connections=16,
        timeout_s=5.0,
    )
    attack = vegeta.build_attack_argv(
        targets_in_container="targets.txt",
        out_bin_in_container=f"/io/{lang}/out.bin",
        params=params,
    )
    res = compose.exec_in(
        env, compose.SVC_VEGETA, ["sh", "-c", f"cd /io/{lang} && {' '.join(attack)}"], check=False
    )
    if not res.ok:
        report.steps.append(SmokeStep(name=f"probe_{lang}_attack", ok=False, detail=res.stderr[-400:]))
        return

    rep_proc = compose.exec_in(
        env, compose.SVC_VEGETA, ["vegeta", "report", "-type=json", f"/io/{lang}/out.bin"], check=False
    )
    try:
        rep = vegeta.parse_report_proc(rep_proc)
    except RuntimeError as exc:
        report.steps.append(SmokeStep(name=f"probe_{lang}_report", ok=False, detail=str(exc)[:400]))
        return

    rate_ok, ratio = vegeta.assert_achieved_vs_offered(rep, SMOKE_PROBE_RATE, SMOKE_ACHIEVED_TOLERANCE_PCT)
    success_ok = rep.success_ratio >= SMOKE_MIN_SUCCESS_RATIO
    detail = (
        f"[smoke-only, not measurement-grade] achieved/offered={ratio:.3f} "
        f"success={rep.success_ratio:.4f} codes={rep.status_codes} "
        f"p50={rep.latency_p50_ns / 1e6:.1f}ms p99={rep.latency_p99_ns / 1e6:.1f}ms"
        + (f" errors={rep.errors[:3]}" if rep.errors else "")
    )
    report.steps.append(SmokeStep(name=f"probe_{lang}", ok=rate_ok and success_ok, detail=detail))


def run_smoke(*, dry_run: bool = False, seed: int | None = None) -> SmokeReport:
    """Execute (or dry-run) the smoke flow."""
    wl = load_workload()
    if seed is None:
        seed = wl.rng.target_generator_seed_default
    report = SmokeReport()

    if dry_run:
        for line in plan():
            report.steps.append(SmokeStep(name="plan", ok=True, detail=line))
        return report

    if not compose.docker_available():
        report.steps.append(
            SmokeStep(name="docker", ok=False, detail="docker daemon not available; cannot run smoke")
        )
        return report

    try:
        # 1. build
        res = compose.run_scrubbed(build_argv(), check=False, timeout=3600)
        report.steps.append(SmokeStep(name="build", ok=res.ok, detail=res.stderr.strip()[-500:]))
        if not res.ok:
            return report

        # 2. seed smoke + invariants (the seeder runs invariants itself)
        res = compose.run_scrubbed(seed_argv(seed=wl.seeding.seed), check=False, timeout=900)
        report.steps.append(SmokeStep(name="seed_smoke", ok=res.ok, detail=(res.stdout + res.stderr).strip()[-500:]))
        if not res.ok:
            return report

        # 3. up both SUTs + vegeta; poll /readyz
        res = compose.run_scrubbed(up_argv(), check=False, timeout=600)
        report.steps.append(SmokeStep(name="up", ok=res.ok, detail=res.stderr.strip()[-500:]))
        if not res.ok:
            return report
        for slot in _slots():
            ok = wait_ready(slot.host_url)
            report.steps.append(
                SmokeStep(name=f"ready_{slot.slot_id}", ok=ok, detail=slot.host_url))
        if not report.ok:
            return report

        # 4. equivalence (both SUTs live against the same smoke DB)
        res = run(
            equivalence_argv(),
            check=False,
            timeout=900,
            cwd=str(paths.bench_dir()),
            extra_env={s.url_env: s.host_url for s in _slots()},
        )
        tail = (res.stdout.strip().splitlines() or [""])[-1]
        report.steps.append(SmokeStep(name="equivalence", ok=res.ok, detail=tail[-400:]))

        # 5. CO stall sanity (this stall check plus per-probe achieved-vs-offered
        #    IS the CO evidence)
        report.steps.append(run_stall_check())

        # 6. per-SUT 30 s probe (in-network URLs through the vegeta container)
        io_dir = paths.run_dir(f"smoke-{rfc3339_micros().replace(':', '').replace('.', '')}") / "io"
        for slot in _slots():
            _probe(report, wl, slot.slot_id, slot.net_url, io_dir, seed)
    finally:
        # 7. teardown (always; volumes dropped for a clean next run)
        res = compose.run_scrubbed(teardown_argv(), check=False, timeout=600)
        report.steps.append(SmokeStep(name="teardown", ok=res.ok))
    return report
