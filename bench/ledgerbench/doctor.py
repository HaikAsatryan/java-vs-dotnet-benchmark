"""Preflight checks. Per-OS: Windows runs a subset.

Checks: docker present + daemon up; cgroup v2 unified (Linux only); free cores
and host RAM against the resource ledger; disk; every implemented slot's build
context; binaries reachable (uv; vegeta lives in the container, checked on the
measured run); spec files parse. Emits `doctor.json` and a rich table. On
Windows the cgroup/host-tuning checks are skipped and reported as such.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from . import compose, config, registry
from .util import paths
from .util.proc import run, which

IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform.startswith("win")


class Check(BaseModel):
    name: str
    ok: bool
    skipped: bool = False
    detail: str = ""


class DoctorReport(BaseModel):
    platform: str
    checks: list[Check] = Field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(c.ok or c.skipped for c in self.checks)

    def save(self, path: Path) -> None:
        paths.write_text_atomic(path, self.model_dump_json(indent=2))


def _check_docker() -> Check:
    if which("docker") is None:
        return Check(name="docker_cli", ok=False, detail="docker not on PATH")
    if not compose.docker_available():
        return Check(name="docker_daemon", ok=False, detail="docker daemon not responding")
    return Check(name="docker_daemon", ok=True, detail="daemon reachable")


def _check_docker_native() -> Check:
    """Linux: the engine must run NATIVELY on this host, not in Docker Desktop's VM.

    Docker Desktop on Linux runs containers inside a linuxkit VM: container PIDs
    are invisible to the host, /sys/fs/cgroup has no container cgroups (the
    sampler that produces the HEADLINE CPU/memory numbers resolves nothing),
    cpusets pin to vCPUs instead of the tuned physical cores, and host tuning
    never reaches the VM scheduler. A measured run on Desktop produces
    plausible-looking latency numbers with NO resource ground truth.
    """
    if not IS_LINUX:
        return Check(name="docker_native_engine", ok=True, skipped=True, detail="non-Linux: skipped")
    res = run(["docker", "info", "--format", "{{.OperatingSystem}}|{{.KernelVersion}}"], timeout=15)
    if not res.ok:
        return Check(name="docker_native_engine", ok=False, detail="docker info failed")
    osname, _, kernel = res.stdout.strip().partition("|")
    if "Docker Desktop" in osname or "linuxkit" in kernel:
        return Check(
            name="docker_native_engine", ok=False,
            detail=f"engine is Docker Desktop (VM kernel {kernel}); measured runs "
                   "need the NATIVE engine (install docker-ce/moby-engine, "
                   "`docker context use default`)",
        )
    return Check(name="docker_native_engine", ok=True, detail=f"native engine on {osname}")


def _check_compose() -> Check:
    res = run(["docker", "compose", "version"], timeout=15)
    return Check(name="docker_compose", ok=res.ok, detail=res.stdout.strip() or res.stderr.strip())


def _check_cgroup_v2() -> Check:
    if not IS_LINUX:
        return Check(name="cgroup_v2_unified", ok=True, skipped=True, detail="non-Linux: skipped")
    marker = Path("/sys/fs/cgroup/cgroup.controllers")
    ok = marker.exists()
    return Check(name="cgroup_v2_unified", ok=ok, detail="unified present" if ok else "cgroup v2 unified not mounted")


# The documented cpusets (infra/compose.bench.yaml + host-setup.sh) reach cpu
# id 15: SUT 0-5, spare 6-7, PG 8-10, vegeta 11-13, redis 14, housekeeping 15.
# What the box must have is the HIGHEST ID + 1, not the sum of the assigned
# cores -- a 13-core box satisfies "6+3+3+1" and still cannot start a container
# pinned to cpuset 15.
REQUIRED_CPU_IDS = 16

# The resource ledger commits SUT 12 + PG 8 + vegeta 4 + redis 2 = 26 GiB of
# mem_limit. The JVM's -XX:MaxRAMPercentage reads the CGROUP limit, not host
# free memory, so a box with less RAM oversubscribes silently: every SUT sizes
# its heap off a 12 GiB ceiling the host cannot honour, and the first evidence
# is an OOM kill or swap thrash mid-cell.
REQUIRED_HOST_RAM_GIB = 26


def parse_mem_total_kb(meminfo: str) -> int | None:
    """`MemTotal:  65_000_000 kB` -> 65000000. None when the key is absent."""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _check_free_cores() -> Check:
    import os

    n = os.cpu_count() or 0
    if IS_WINDOWS:
        return Check(name="cpu_cores", ok=n >= 2, detail=f"{n} logical cores (smoke)")
    return Check(
        name="cpu_cores", ok=n >= REQUIRED_CPU_IDS,
        detail=(f"{n} logical cores; the documented cpusets reach id "
                f"{REQUIRED_CPU_IDS - 1} (housekeeping)"),
    )


def _check_host_ram(meminfo: Path | None = None) -> Check:
    """Linux: enough RAM for the resource ledger's mem_limits. See the constant."""
    if not IS_LINUX:
        return Check(name="host_ram", ok=True, skipped=True, detail="non-Linux: skipped")
    path = meminfo or Path("/proc/meminfo")
    try:
        total_kb = parse_mem_total_kb(path.read_text(encoding="utf-8"))
    except OSError:
        total_kb = None
    if total_kb is None:
        return Check(name="host_ram", ok=False, detail=f"no MemTotal in {path}")
    gib = total_kb / (1024**2)
    return Check(
        name="host_ram", ok=gib >= REQUIRED_HOST_RAM_GIB,
        detail=f"{gib:.1f} GiB total; the resource ledger commits "
               f"{REQUIRED_HOST_RAM_GIB} GiB of container mem_limit",
    )


def _check_build_contexts() -> Check:
    """Every IMPLEMENTED slot's build context must exist in the tree.

    `docker compose config` exits 0 on a missing build context, so a dangling
    service would otherwise surface as a build failure ~30 minutes into a run.
    Slots with `implemented=False` are skipped.
    """
    root = paths.repo_root()
    missing = [
        f"{sid} -> {slot.build_context}"
        for sid, slot in sorted(registry.SLOTS.items())
        if slot.implemented and not (root / slot.build_context).is_dir()
    ]
    return Check(
        name="build_contexts", ok=not missing,
        detail=("missing: " + ", ".join(missing)) if missing
        else f"{len(registry.runnable_slots())} implemented slot(s) present",
    )


def _check_disk(min_gb: int = 20) -> Check:
    root = paths.repo_root()
    usage = shutil.disk_usage(str(root))
    free_gb = usage.free / (1024**3)
    return Check(name="disk_free", ok=free_gb >= min_gb, detail=f"{free_gb:.1f} GB free at {root}")


def _check_uv() -> Check:
    found = which("uv")
    if found is None:
        return Check(name="uv", ok=False, detail="uv not on PATH")
    res = run(["uv", "--version"], timeout=15)
    return Check(name="uv", ok=res.ok, detail=res.stdout.strip())


def _check_root_or_sudo() -> Check:
    """Linux: host-setup.sh --tune (re-run on every run-all start) needs root.

    Passes when running as root or when `sudo -n true` succeeds (NOPASSWD grant,
    a RUNBOOK prerequisite). Failing here keeps a measured run from dying at the
    host_setup phase after doctor already passed.
    """
    import os

    if not IS_LINUX:
        return Check(name="root_or_sudo", ok=True, skipped=True, detail="non-Linux: skipped")
    if os.geteuid() == 0:
        return Check(name="root_or_sudo", ok=True, detail="running as root")
    res = run(["sudo", "-n", "true"], timeout=15)
    return Check(
        name="root_or_sudo", ok=res.ok,
        detail="sudo -n ok (NOPASSWD)" if res.ok
        else "not root and `sudo -n true` failed; host tuning needs a NOPASSWD sudo grant",
    )


def _check_spec_parses() -> Check:
    try:
        config.load_workload()
        config.load_slo()
    except Exception as exc:  # noqa: BLE001 - surfaced to the report
        return Check(name="spec_parses", ok=False, detail=f"{type(exc).__name__}: {exc}")
    return Check(name="spec_parses", ok=True, detail="workload.yaml + slo.yaml valid")


def run_doctor(*, require_docker: bool = False) -> DoctorReport:
    """Run all checks. On Windows, docker checks are soft unless require_docker."""
    report = DoctorReport(platform=sys.platform)
    docker_check = _check_docker()
    if not require_docker and not docker_check.ok and IS_WINDOWS:
        docker_check = Check(name=docker_check.name, ok=True, skipped=True, detail="docker optional for non-run checks")
    report.checks.append(docker_check)
    if docker_check.ok and not docker_check.skipped:
        report.checks.append(_check_docker_native())
        report.checks.append(_check_compose())
    report.checks.append(_check_cgroup_v2())
    report.checks.append(_check_free_cores())
    report.checks.append(_check_host_ram())
    report.checks.append(_check_disk())
    report.checks.append(_check_build_contexts())
    report.checks.append(_check_uv())
    report.checks.append(_check_root_or_sudo())
    report.checks.append(_check_spec_parses())
    return report
