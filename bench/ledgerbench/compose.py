"""docker compose helpers: -f selection, profiles, up --wait, down, exec, digests.

Base `infra/compose.yaml` (portable) optionally merged with
`infra/compose.bench.yaml` (Linux resource overlay). Each SUT slot carries its
own profile (registry.Slot.profile), so exactly one SUT is resident at a time.

This module holds the command builders + thin runners; the runner sequences
them. No `docker compose up`/`run` is invoked here during build/test (measured
tiers never run on Windows); the read-only `docker inspect` readers are guarded
so unit tests never touch the daemon.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import registry
from .util import paths
from .util.proc import ProcResult, run, which

# Shared-tier service names mirror infra/compose.yaml. SUT service and profile
# names are NOT constants here: they belong to a slot and come from
# ledgerbench.registry, so adding or renaming a SUT is one edit in one file and
# a stale name raises instead of resolving to nothing.
SVC_POSTGRES = "postgres"
SVC_REDIS = "redis"
SVC_VEGETA = "vegeta"
SVC_SEED = "seed"

PROFILE_LOAD = "load"
PROFILE_SEED = "seed"


def container_name(service: str, project: str = "ledgerline") -> str:
    """Compose's container name for a service under `project`.

    Never hard-code the "ledgerline-" prefix: under a non-default `-p` a
    hard-coded name resolves NO cgroup path, and the resource numbers -- which
    are the headline -- silently become empty.
    """
    return f"{project}-{service}-1"


@dataclass
class ComposeEnv:
    """Resolved compose invocation context."""

    files: list[Path]
    profiles: list[str] = field(default_factory=list)
    project_name: str = "ledgerline"
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def base(cls, *, bench_overlay: bool = False, **kw) -> "ComposeEnv":
        infra = paths.infra_dir()
        files = [infra / "compose.yaml"]
        if bench_overlay:
            files.append(infra / "compose.bench.yaml")
        return cls(files=files, **kw)

    def base_argv(self) -> list[str]:
        argv = ["docker", "compose", "-p", self.project_name]
        for f in self.files:
            argv += ["-f", str(f)]
        for prof in self.profiles:
            argv += ["--profile", prof]
        return argv


def docker_available() -> bool:
    """True if a docker CLI exists and the daemon answers (guarded)."""
    if which("docker") is None:
        return False
    res = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
    return res.ok


# --------------------------------------------------------------------------- #
# command builders (pure; tested without a daemon)
# --------------------------------------------------------------------------- #
def build_argv(env: ComposeEnv, services: list[str]) -> list[str]:
    return [*env.base_argv(), "build", *services]


def up_wait_argv(env: ComposeEnv, services: list[str]) -> list[str]:
    """`up --wait -d` so healthchecks gate readiness."""
    return [*env.base_argv(), "up", "-d", "--wait", *services]


def down_argv(env: ComposeEnv, *, volumes: bool = False) -> list[str]:
    argv = [*env.base_argv(), "down"]
    if volumes:
        argv.append("-v")
    return argv


def exec_argv(env: ComposeEnv, service: str, command: list[str]) -> list[str]:
    """`exec -T` (no TTY) so the runner can pipe/capture output."""
    return [*env.base_argv(), "exec", "-T", service, *command]


def run_oneshot_argv(env: ComposeEnv, service: str, command: list[str]) -> list[str]:
    """`run --rm` for one-shot containers (e.g. the seeder, profile seed)."""
    return [*env.base_argv(), "run", "--rm", service, *command]


# --------------------------------------------------------------------------- #
# thin runners (touch the daemon; guarded by callers)
# --------------------------------------------------------------------------- #
# Cell-controlled knob vars. compose.yaml interpolates ${VAR:-default} from the
# process environment, so an AMBIENT value in the operator's shell (a stray
# JDK_JAVA_OPTIONS export, a leftover DATA_LAYER) would silently override the
# pinned baseline for BOTH SUTs while the manifest records the intended values.
# Every compose invocation forces these to "" (=> the compose.yaml default)
# unless the active cell explicitly sets them via extra_env.
#
# DERIVED from the registry, never hand-listed: a knob added to a language but
# forgotten here is exactly that silent override. BODY_MAX_BYTES is scrubbed too
# (a shared limit with no Knobs field yet); SUT_MEM is not, because it is a
# topology knob the runner itself reads, like SUT_CPUS / PG_* / VEGETA_*.
KNOB_ENV_VARS = tuple(sorted(registry.all_knob_env_vars() | {"BODY_MAX_BYTES"}))
# Deliberately NOT scrubbed: SUT_CPUS / SUT_CORES / PG_* / VEGETA_*: documented
# operator-level topology knobs the runner itself reads (phases.SUT_CORES) and
# asserts via the processor_count check; the manifest discloses them.


def _effective_env(extra_env: dict[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {k: "" for k in KNOB_ENV_VARS}
    if extra_env:
        merged.update(extra_env)
    return merged


def compose(env: ComposeEnv, args: list[str], *, check: bool = True, timeout: float | None = None) -> ProcResult:
    # env.extra_env carries the per-cell knob overrides (JVM_MAX_RAM_PERCENT,
    # JVM_GC_FLAGS, DOTNET_GCDynamicAdaptationMode, DATA_LAYER, ...). compose.yaml
    # interpolates
    # ${VAR} from the process environment, so they MUST be passed through here or
    # the cell overrides silently fall back to the compose.yaml defaults.
    return run(
        [*env.base_argv(), *args],
        check=check,
        timeout=timeout,
        extra_env=_effective_env(env.extra_env),
    )


def exec_in(env: ComposeEnv, service: str, command: list[str], *, check: bool = True) -> ProcResult:
    return run(exec_argv(env, service, command), check=check, extra_env=_effective_env(env.extra_env))


def run_scrubbed(
    argv: list[str], *, check: bool = True, timeout: float | None = None
) -> ProcResult:
    """`proc.run` with the ambient-knob scrub, for callers holding a prebuilt argv.

    smoke's build/seed/up/teardown used to call `proc.run` directly, which let a
    stray operator export (DATA_LAYER, JVM_GC_FLAGS, MAX_AUTO_PREPARE, ...)
    reach compose interpolation on exactly the code path that exists to prove
    the plumbing works at its defaults.
    """
    return run(argv, check=check, timeout=timeout, extra_env=_effective_env(None))


# --------------------------------------------------------------------------- #
# digest capture (manifest versions/digests)
# --------------------------------------------------------------------------- #
def container_image_digest(container_name: str) -> str | None:
    """Resolve the running container's image RepoDigest via `docker inspect`.

    Read-only: never mutates the daemon. Returns None if docker is absent or
    the container/image has no digest yet.
    """
    if which("docker") is None:
        return None
    res = run(
        ["docker", "inspect", "--format", "{{json .Image}}", container_name],
        timeout=15,
    )
    if not res.ok:
        return None
    image_id = json.loads(res.stdout.strip() or '""')
    if not image_id:
        return None
    res2 = run(
        ["docker", "inspect", "--format", "{{json .RepoDigests}}", image_id],
        timeout=15,
    )
    if not res2.ok:
        return None
    digests = json.loads(res2.stdout.strip() or "[]")
    return digests[0] if digests else image_id


def container_pid(container_name: str) -> int | None:
    """Container main PID via `docker inspect` (used by cgroups path resolution)."""
    if which("docker") is None:
        return None
    res = run(
        ["docker", "inspect", "--format", "{{.State.Pid}}", container_name],
        timeout=15,
    )
    if not res.ok:
        return None
    try:
        pid = int(res.stdout.strip())
    except ValueError:
        return None
    return pid or None
