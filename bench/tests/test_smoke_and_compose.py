"""Smoke flow plan + compose command builders (no docker daemon touched)."""

from __future__ import annotations

from ledgerbench import compose, registry, smoke
from ledgerbench.compose import ComposeEnv


def test_smoke_dry_run_plan_shape():
    report = smoke.run_smoke(dry_run=True)
    details = [s.detail for s in report.steps]
    joined = "\n".join(details)
    # the smoke ordered flow appears
    assert "build" in joined
    assert "--scale smoke" in joined
    assert "--profile seed" in joined
    assert "30s vegeta probe" in joined or "30s" in joined
    assert "down" in joined and "-v" in joined
    assert report.ok  # dry-run plan is all-ok


def test_smoke_resource_accounting_off_non_linux():
    report = smoke.run_smoke(dry_run=True)
    assert "disabled" in report.resource_accounting or "off" in report.resource_accounting
    assert report.label == "smoke-only"


def test_compose_base_argv_profiles():
    slot = registry.get_slot("dotnet")
    env = ComposeEnv.base(profiles=[slot.profile, compose.PROFILE_LOAD])
    argv = env.base_argv()
    assert argv[:2] == ["docker", "compose"]
    assert "--profile" in argv and slot.profile in argv and "load" in argv
    assert "-p" in argv and "ledgerline" in argv


def test_compose_bench_overlay_adds_second_file():
    base = ComposeEnv.base()
    overlay = ComposeEnv.base(bench_overlay=True)
    assert len(base.files) == 1
    assert len(overlay.files) == 2
    assert overlay.files[1].name == "compose.bench.yaml"


def test_up_wait_and_down_builders():
    slot = registry.get_slot("dotnet")
    env = ComposeEnv.base(profiles=[slot.profile])
    up = compose.up_wait_argv(env, [slot.service])
    assert "up" in up and "--wait" in up and "-d" in up
    down = compose.down_argv(env, volumes=True)
    assert down[-1] == "-v" and "down" in down


def test_exec_argv_no_tty():
    env = ComposeEnv.base()
    argv = compose.exec_argv(env, compose.SVC_VEGETA, ["vegeta", "--version"])
    assert "exec" in argv and "-T" in argv
    assert argv[-2:] == ["vegeta", "--version"]


def test_seed_oneshot_argv():
    argv = smoke.seed_argv(scale="smoke", seed=42)
    joined = " ".join(argv)
    assert "run" in argv and "--rm" in argv
    assert "--scale smoke" in joined and "--seed 42" in joined


def test_every_bench_overlay_service_disables_swap():
    """`memswap_limit == mem_limit` is how docker spells "no swap allowance".

    Without it docker sets memory-swap to 2x memory, so the SUT carries a 12 GiB
    swap allowance against this box's 8 GiB of ACTIVE zram. A swapped-out anon
    page LEAVES cgroup v2 `memory.stat anon`, so the steady-state-memory-per-
    request headline would be silently understated -- and unlike an OOM (which
    the sampler's `oom_kill` delta fails the probe on) nothing would record it.

    The check is per SERVICE rather than a file-wide grep: a slot added to the
    overlay with `mem_limit` and no `memswap_limit` is exactly the case that
    would slip through.
    """
    import yaml

    from ledgerbench.util import paths

    doc = yaml.safe_load(
        (paths.infra_dir() / "compose.bench.yaml").read_text(encoding="utf-8"))
    services = doc["services"]
    assert services, "the overlay declares no services"
    for name, spec in services.items():
        assert "mem_limit" in spec, f"{name}: the overlay must pin a memory ceiling"
        assert spec.get("memswap_limit") == spec["mem_limit"], (
            f"{name}: memswap_limit must equal mem_limit ({spec['mem_limit']}) or "
            "docker grants the container a swap allowance equal to its memory limit"
        )


def test_the_expected_memory_ceilings_match_the_compose_overlay():
    """The gate's expectation and the overlay's defaults must not drift apart.

    `phases.mem_limit_env_by_role` is what the first measured window compares
    each container's `memory.max` against, and it carries the overlay's defaults
    as literals (the same shape `cpuset_env_by_role` already had). A ceiling
    raised here and a stale literal there aborts every run over a change the
    operator did make -- or passes a container that never got the ceiling.
    """
    import re

    import yaml

    from ledgerbench import cgroups, phases
    from ledgerbench.util import paths

    doc = yaml.safe_load(
        (paths.infra_dir() / "compose.bench.yaml").read_text(encoding="utf-8"))
    role_of = {
        "sut-java": cgroups.ROLE_SUT,
        "sut-dotnet": cgroups.ROLE_SUT, "sut-dotnet-b": cgroups.ROLE_SUT,
        "postgres": cgroups.ROLE_DATABASE, "vegeta": cgroups.ROLE_LOAD_GENERATOR,
        "redis": cgroups.ROLE_CACHE,
    }
    expected = phases.mem_limit_env_by_role({})
    for name, spec in doc["services"].items():
        # KeyError here is the point: a service added to the overlay with no
        # role in this map has no declared ceiling for the gate to check.
        role = role_of[name]
        m = re.fullmatch(r"\$\{(\w+):-([^}]+)\}", str(spec["mem_limit"]))
        assert m, f"{name}: mem_limit must stay env-parameterized"
        var, default = m.groups()
        assert expected[role] == phases.parse_compose_size(default), (
            f"{name}: the overlay defaults to {default} and the gate expects "
            f"{expected[role]} bytes for role {role}"
        )
        # and the gate reads the SAME variable the overlay does
        assert phases.mem_limit_env_by_role({var: "1g"})[role] == 1024 ** 3


def test_an_unparseable_memory_ceiling_raises_instead_of_dropping_the_role():
    """A role omitted from the expectation is a ceiling gate that fails OPEN.

    `MultiSampler.limit_mismatches` skips a role with no expected ceiling, so
    silently dropping the one role whose env value did not parse is exactly the
    container whose `memory.max` then goes unchecked on the run's first measured
    window. `checksums.published_rels` raises rather than guess a scope it cannot
    establish; this is the same shape and gets the same answer.
    """
    import pytest

    from ledgerbench import cgroups, phases

    with pytest.raises(ValueError, match="SUT_MEM"):
        phases.mem_limit_env_by_role({"SUT_MEM": "12gib"})
    # a zero ceiling is not a ceiling either
    with pytest.raises(ValueError, match="PG_MEM"):
        phases.mem_limit_env_by_role({"PG_MEM": "0"})
    # and the roles that DO parse are still all four
    assert set(phases.mem_limit_env_by_role({"SUT_MEM": "12g"})) == {
        cgroups.ROLE_SUT, cgroups.ROLE_DATABASE, cgroups.ROLE_CACHE,
        cgroups.ROLE_LOAD_GENERATOR,
    }


def test_run_scrubbed_blanks_every_knob_env_var(monkeypatch):
    """The scrub is the whole point of the wrapper: every registry knob var is
    forced to "" so an ambient operator export cannot reach compose
    interpolation on the smoke path."""
    captured = {}

    def _fake_run(argv, *, check=True, timeout=None, extra_env=None):
        captured["extra_env"] = extra_env

        class _R:
            ok = True
            stdout = stderr = ""
        return _R()

    monkeypatch.setattr(compose, "run", _fake_run)
    compose.run_scrubbed(["docker", "compose", "ps"])
    for var in compose.KNOB_ENV_VARS:
        assert captured["extra_env"][var] == ""


def test_smoke_never_calls_the_unscrubbed_runner():
    """smoke's build/seed/up/teardown went through bare `compose.run` and let a
    stray DATA_LAYER / JVM_GC_FLAGS export reach the plumbing check; pin the
    module to the scrubbed wrapper."""
    import inspect

    from ledgerbench import smoke as smoke_mod

    src = inspect.getsource(smoke_mod)
    assert "compose.run(" not in src
    assert "compose.run_scrubbed(" in src
