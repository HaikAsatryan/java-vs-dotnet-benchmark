"""B6: read the tuned host state back off the box, so the run can evidence it.

Round 1 recorded `manifest.host_state` as `source: not_measured` on every run
and therefore could not show the governor, boost state, C-states, THP mode or
core placement it claimed to have measured under. Every value `infra/
host-setup.sh` writes is read back here after the tuning is applied, on every
run start and every resume, and a Tier-1 mismatch refuses the run.

TWO RULES, both mechanical rather than editorial:

  * SETTING vs OUTCOME. A setting is a value we asked the kernel for; it reads
    back deterministically and can be compared to a target. An outcome is a
    counter or level showing whether the setting bit -- load- and time-dependent,
    comparable to nothing. `evaluate` RAISES on a Tier-1 outcome.
  * A value may be Tier-1 only where `infra/host-setup.sh` demonstrably sets it.
    Governor, boost, THP `enabled`, the systemd drop-in and the service stops
    qualify. IRQ affinity, daemon affinity, `is-enabled`, THP `defrag` /
    `shmem_enabled`, C-states and isolcpus do NOT: measured on the bench box,
    nvme blk-mq managed interrupts sit on SUT cores 0/1/3/5 and the kernel
    refuses to move them, dockerd reads `0-31` because a system.conf drop-in
    does not migrate already-running units, and host-setup stops services
    without disabling them. Gating any of those aborts every run and the
    operator's only recourse is an opt-out. They are recorded instead.

The declared targets live HERE and never in host-setup.sh's state file: that
file records the ORIGINAL values so `host-restore.sh` can undo them, and
host-restore deletes it. `HOST_SETUP_ENV_DEFAULTS` is the whole contract with
the script, and it is enforced from both ends: `phases._privileged_argv`
forwards exactly these names through `sudo -n env ...` (sudo's `env_reset`
would otherwise leave the script on its own defaults while this table compared
against the operator's), and a test parses host-setup.sh's own `${VAR:-default}`
lines and fails when a default here and a default there disagree.

Every reader takes its roots as parameters, so a fixture tree under `tmp_path`
drives this exact code on a box with no root and no cgroups.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .manifest import HostReading
from .util.cpuset import format_cpuset, parse_cpuset
from .util.proc import run

SYSFS = Path("/sys")
PROCFS = Path("/proc")

KIND_SETTING = "setting"
KIND_OUTCOME = "outcome"
TIER_1 = "1"
TIER_2 = "2"

DEFAULT_SUT_CPUS = "0-5"
DEFAULT_HOUSEKEEPING_CPUS = "15"
DEFAULT_PGDATA = "/var/lib/docker/volumes/ledgerline_pgdata/_data"
DEFAULT_STOP_SERVICES = "tuned power-profiles-daemon irqbalance"
DEFAULT_SYSTEMD_DROPIN = "/etc/systemd/system.conf.d/99-ledgerline-affinity.conf"

# THE contract with infra/host-setup.sh: every env knob the script reads, with
# the script's own default. `phases` forwards these through sudo and a test
# parses the script's `${VAR:-default}` lines against this map, so neither side
# can move alone.
HOST_SETUP_ENV_DEFAULTS: dict[str, str] = {
    "SUT_CPUS": DEFAULT_SUT_CPUS,
    "HOUSEKEEPING_CPUS": DEFAULT_HOUSEKEEPING_CPUS,
    "PGDATA_PATH": DEFAULT_PGDATA,
    "STOP_SERVICES": DEFAULT_STOP_SERVICES,
    "SYSTEMD_DROPIN": DEFAULT_SYSTEMD_DROPIN,
}

# Daemons whose CPU placement matters to the measurement: dockerd relays the
# SUT's stdout, containerd carries the shim, journald sinks both.
DAEMON_UNITS = ("docker.service", "containerd.service", "systemd-journald.service")

# `cpupower idle-set -D 2` disables every state whose exit latency EXCEEDS 2 us.
# A comment in host-setup.sh's `tune_cstates` says "state index >= 2", which is a
# different rule that happens to pick the same states on this box (POLL/0, C1/1,
# C2/18, C3/350). The read-back records the parsed table so the artifact says
# which.
CSTATE_MAX_LATENCY_US = 2

ATTR_GOVERNOR = "governor"
ATTR_BOOST = "boost"
ATTR_CPU_FREQ = "cpu_freq_outcome"
ATTR_CSTATES = "cstates"
ATTR_THP_ENABLED = "thp_enabled"
ATTR_THP_DEFRAG = "thp_defrag"
ATTR_THP_SHMEM = "thp_shmem_enabled"
ATTR_IRQ_DEFAULT = "irq_default_smp_affinity"
ATTR_IRQ_TABLE = "irq_smp_affinity"
ATTR_IRQ_COUNTS = "irq_counts_on_sut_cores"
ATTR_DAEMON_AFFINITY = "daemon_cpus_allowed"
ATTR_PGDATA_ATTRS = "pgdata_attrs"
ATTR_PGDATA_MOUNT = "pgdata_mount_options"
ATTR_SERVICES_ACTIVE = "services_active"
ATTR_SERVICES_ENABLED = "services_enabled"
ATTR_ISOLCPUS = "isolcpus"
ATTR_NOHZ_FULL = "nohz_full"
ATTR_CMDLINE = "kernel_cmdline"
ATTR_TOPOLOGY = "topology"
ATTR_DROPIN = "systemd_cpuaffinity_dropin"


@dataclass(frozen=True)
class HostExpectation:
    """What the box is supposed to read back, from host-setup.sh's own env."""

    sut_cpus_raw: str
    sut_cpus: frozenset[int]
    housekeeping_raw: str
    housekeeping_cpus: frozenset[int]
    stop_services: tuple[str, ...]
    pgdata_path: str
    systemd_dropin: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HostExpectation:
        src = os.environ if env is None else env
        resolved = {
            name: (src.get(name, default).strip() or default)
            for name, default in HOST_SETUP_ENV_DEFAULTS.items()
        }
        return cls(
            sut_cpus_raw=resolved["SUT_CPUS"],
            sut_cpus=frozenset(parse_cpuset(resolved["SUT_CPUS"])),
            housekeeping_raw=resolved["HOUSEKEEPING_CPUS"],
            housekeeping_cpus=frozenset(parse_cpuset(resolved["HOUSEKEEPING_CPUS"])),
            stop_services=tuple(resolved["STOP_SERVICES"].split()),
            pgdata_path=resolved["PGDATA_PATH"],
            systemd_dropin=resolved["SYSTEMD_DROPIN"],
        )


# --------------------------------------------------------------------------- #
# pure parsers
# --------------------------------------------------------------------------- #
def thp_active(text: str) -> str | None:
    """`"always [madvise] never"` -> `"madvise"`; no bracket -> None.

    None, never `""` and never the first token: a THP file with no bracketed
    token is unreadable, and returning `always` (the first word) would publish
    the mode the box is furthest from as the mode it is in.
    """
    m = re.search(r"\[([^\]]*)\]", text)
    return m.group(1) if m else None


def parse_cmdline(text: str) -> dict[str, str]:
    """`/proc/cmdline` verbatim -> `{"isolcpus": "0-5", ...}`; bare flags -> `""`."""
    out: dict[str, str] = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        out[key] = value if sep else ""
    return out


def parse_hex_mask(text: str) -> frozenset[int]:
    """`"00000000,00008000"` -> {15}. /proc/irq masks are hex, comma-grouped.

    The comma groups are 32-bit words, most significant FIRST, so they cannot be
    parsed as one integer per group independently -- they are concatenated.
    """
    joined = text.strip().replace(",", "")
    if not joined:
        return frozenset()
    try:
        bits = int(joined, 16)
    except ValueError:
        return frozenset()
    return frozenset(i for i in range(bits.bit_length()) if bits >> i & 1)


def parse_cpus_allowed(status_text: str) -> str | None:
    """`Cpus_allowed_list:\t0-31` from a /proc/<pid>/status dump."""
    for line in status_text.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return line.split(":", 1)[1].strip()
    return None


def parse_interrupts(text: str) -> tuple[list[int], dict[str, dict]]:
    """/proc/interrupts -> (cpu ids from the header, {irq: {name, counts}}).

    Rows whose id is not numeric (NMI, LOC, RES, ...) are kept: they are real
    per-cpu work and dropping them would understate the interrupt load on a SUT
    core. Their name is the row label itself.

    A row counts as per-cpu only when it carries a FULL header's worth of
    columns. `ERR` and `MIS` print one machine-wide total instead, and consuming
    that single field as a per-cpu column charged every spurious interrupt on
    the box to cpu 0 -- inflating the one artifact that exists to show interrupt
    load on the SUT cores. Such a row keeps its label and reports no counts.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], {}
    cpus = [int(tok[3:]) for tok in lines[0].split() if tok.startswith("CPU")]
    rows: dict[str, dict] = {}
    for line in lines[1:]:
        head, sep, rest = line.partition(":")
        if not sep:
            continue
        irq = head.strip()
        fields = rest.split()
        n = 0
        while n < len(cpus) and n < len(fields) and fields[n].isdigit():
            n += 1
        counts = ({} if n < len(cpus)
                  else {cpu: int(fields[i]) for i, cpu in enumerate(cpus)})
        rows[irq] = {"name": " ".join(fields[n:]).strip(), "counts": counts}
    return cpus, rows


def cpuidle_states(cpu_dir: Path) -> list[tuple[str, int, int]]:
    """`(name, disable, latency_us)` per `cpuidle/state*/`, in state-index order."""
    idle = cpu_dir / "cpuidle"
    out: list[tuple[str, int, int]] = []
    for state in sorted(idle.glob("state*"), key=_state_index):
        name = _read_text(state / "name")
        disable = _read_text(state / "disable")
        latency = _read_text(state / "latency")
        if name is None or disable is None or latency is None:
            continue
        try:
            out.append((name, int(disable), int(latency)))
        except ValueError:
            continue
    return out


def cstate_expectation(
    states: list[tuple[str, int, int]], max_latency_us: int = CSTATE_MAX_LATENCY_US
) -> list[str]:
    """Names that must read `disable=1` under `cpupower idle-set -D <max>`.

    BY LATENCY, not by state index: `-D 2` disables every state whose exit
    latency EXCEEDS 2 us wherever it sits in the list. The two rules agree on
    this box's table and disagree on any box whose C1 is slower than its C2.
    """
    return [name for name, _disable, latency in states if latency > max_latency_us]


def parse_pg_settings(tsv: str) -> dict[str, dict]:
    """`name\\tsetting\\tunit\\tsource\\tboot_val\\treset_val` rows -> {name: row}.

    Keyed by NAME. Keying by `setting` collapses every parameter sharing a value
    (`on` alone covers a dozen) into one row.
    """
    out: dict[str, dict] = {}
    cols = ("setting", "unit", "source", "boot_val", "reset_val")
    for line in tsv.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0].strip()
        if not name:
            continue
        out[name] = {c: (parts[i + 1] if i + 1 < len(parts) else "") for i, c in enumerate(cols)}
    return out


def _brief(paths_: list[str], limit: int = 4) -> str:
    """First few paths plus a count. A 32-CPU box names 33 boost knobs."""
    head = "; ".join(paths_[:limit])
    return head if len(paths_) <= limit else f"{head}; +{len(paths_) - limit} more"


def _short(value: object, limit: int = 200) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def evaluate(
    readings: list[HostReading],
) -> tuple[list[str], list[str], list[str]]:
    """(Tier-1 failures, Tier-2 notes, Tier-1 NOT EVALUABLE). Raises on a Tier-1 OUTCOME.

    Three buckets, not two. A Tier-1 knob this box does not have (no
    `cpufreq/boost` at all, no `policy*/boost` either) is neither a pass nor a
    failure, and folding it into the Tier-2 notes made `tier1_failures: []` mean
    two different things: "every Tier-1 attribute was checked and matched" and
    "the knob was absent and nothing was checked". The RUNBOOK's "a Tier-1
    mismatch aborts the run" then reads as a guarantee the artifact cannot
    support. The third bucket is recorded and does NOT abort -- a knob that does
    not exist is not something an operator can fix.

    An outcome is load-dependent, so gating on one produces a run that fails for
    reasons no operator can fix; the rule is enforced here rather than left to
    review of the reader table.
    """
    failures: list[str] = []
    notes: list[str] = []
    not_evaluable: list[str] = []
    for r in readings:
        if r.tier == TIER_1 and r.kind != KIND_SETTING:
            raise ValueError(
                f"{r.attribute}: a Tier-1 reading must be a {KIND_SETTING} "
                f"(got {r.kind!r}); an outcome is load-dependent and can gate nothing"
            )
        if r.matches is True:
            continue
        msg = f"{r.attribute}: read {_short(r.value)}, expected {_short(r.expected)}"
        if r.detail:
            msg += f" ({r.detail})"
        if r.matches is False:
            (failures if r.tier == TIER_1 else notes).append(msg)
        elif r.expected is not None:
            target = not_evaluable if r.tier == TIER_1 else notes
            target.append(f"{r.attribute}: not evaluable on this box ({r.detail})")
    return failures, notes, not_evaluable


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #
def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _state_index(path: Path) -> int:
    m = re.search(r"(\d+)$", path.name)
    return int(m.group(1)) if m else 0


def _cpu_set(raw: str) -> frozenset[int] | None:
    """A cpu list that may carry non-numeric flags -> the cpu ids, or None.

    `isolcpus=` takes flag tokens (`managed_irq`, `domain`, `nohz`) before the
    range, so the strict parser would raise on a perfectly ordinary cmdline.
    """
    ids: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ids.update(parse_cpuset(token))
        except ValueError:
            continue
    return frozenset(ids) or None


def _cpu_dirs(sysfs: Path) -> list[Path]:
    base = sysfs / "devices" / "system" / "cpu"
    return sorted(
        (p for p in base.glob("cpu[0-9]*") if p.is_dir()),
        key=lambda p: int(p.name[3:]),
    )


def read_governor(*, sysfs: Path = SYSFS) -> list[HostReading]:
    """Every online CPU's `scaling_governor`, as a SET (host-setup.sh `tune_governor`)."""
    read: dict[str, str] = {}
    for cpu in _cpu_dirs(sysfs):
        value = _read_text(cpu / "cpufreq" / "scaling_governor")
        if value is not None:
            read[str(cpu / "cpufreq" / "scaling_governor")] = value
    if not read:
        return [HostReading(
            attribute=ATTR_GOVERNOR, kind=KIND_SETTING, tier=TIER_1,
            expected="performance", matches=None,
            detail="no cpufreq/scaling_governor under this sysfs root",
        )]
    wrong = sorted(p for p, v in read.items() if v != "performance")
    return [HostReading(
        attribute=ATTR_GOVERNOR, kind=KIND_SETTING, tier=TIER_1,
        paths=sorted(read), value=sorted(set(read.values())),
        expected="performance", matches=not wrong,
        detail=(_brief([f"{p}={read[p]}" for p in wrong]) if wrong
                else f"{len(read)} cpu(s) read performance"),
    )]


def read_boost(*, sysfs: Path = SYSFS) -> list[HostReading]:
    """EVERY boost knob that answers, not just the first (host-setup.sh `tune_boost`).

    The script writes the global `cpufreq/boost` and returns; on this box the
    per-policy knobs exist TOO and both read 1 untuned, so a write that does not
    propagate is invisible to everything except this read.
    """
    base = sysfs / "devices" / "system" / "cpu" / "cpufreq"
    knobs: dict[str, str] = {}
    global_knob = base / "boost"
    value = _read_text(global_knob)
    if value is not None:
        knobs[str(global_knob)] = value
    for policy in sorted(base.glob("policy*")):
        value = _read_text(policy / "boost")
        if value is not None:
            knobs[str(policy / "boost")] = value
    if not knobs:
        return [HostReading(
            attribute=ATTR_BOOST, kind=KIND_SETTING, tier=TIER_1,
            expected="0", matches=None, detail="no boost knob on this box",
        )]
    on = sorted(p for p, v in knobs.items() if v != "0")
    return [HostReading(
        attribute=ATTR_BOOST, kind=KIND_SETTING, tier=TIER_1,
        paths=sorted(knobs), value=dict(sorted(knobs.items())),
        expected="0", matches=not on,
        detail=(f"{len(on)} of {len(knobs)} knob(s) still on: {_brief(on)}" if on
                else f"{len(knobs)} knob(s) read 0"),
    )]


def read_cpu_freq(exp: HostExpectation, *, sysfs: Path = SYSFS) -> list[HostReading]:
    """OUTCOME: current frequency + throttle counts on the SUT cores.

    Evidence that boost-off and the governor bit, sampled once at phase time.
    Load-dependent by construction, so it is recorded and never compared.
    """
    freqs: dict[str, str] = {}
    throttles: dict[str, str] = {}
    for cpu in _cpu_dirs(sysfs):
        if int(cpu.name[3:]) not in exp.sut_cpus:
            continue
        cur = _read_text(cpu / "cpufreq" / "scaling_cur_freq")
        if cur is not None:
            freqs[cpu.name] = cur
        thr = _read_text(cpu / "thermal_throttle" / "core_throttle_count")
        if thr is not None:
            throttles[cpu.name] = thr
    return [HostReading(
        attribute=ATTR_CPU_FREQ, kind=KIND_OUTCOME, tier=TIER_2,
        value={"scaling_cur_freq_khz": freqs, "core_throttle_count": throttles},
        detail="single sample at phase time; idle-box frequencies, not a load reading",
    )]


def read_cstates(*, sysfs: Path = SYSFS) -> list[HostReading]:
    """cpuidle tables per CPU. Tier-2: host-setup.sh skips when cpupower is absent."""
    tables: dict[str, list] = {}
    wrong: list[str] = []
    expected: list[str] = []
    for cpu in _cpu_dirs(sysfs):
        states = cpuidle_states(cpu)
        if not states:
            continue
        tables[cpu.name] = [[n, d, lat] for n, d, lat in states]
        must_disable = cstate_expectation(states)
        expected = expected or must_disable
        for name, disable, latency in states:
            if (name in must_disable) != bool(disable):
                wrong.append(f"{cpu.name}/{name}(latency={latency}us,disable={disable})")
    if not tables:
        return [HostReading(
            attribute=ATTR_CSTATES, kind=KIND_SETTING, tier=TIER_2,
            expected="deep states disabled", matches=None,
            detail=f"no cpuidle sysfs under {sysfs / 'devices/system/cpu'}",
        )]
    return [HostReading(
        attribute=ATTR_CSTATES, kind=KIND_SETTING, tier=TIER_2,
        paths=[str(sysfs / "devices/system/cpu/cpu*/cpuidle")],
        value=tables, expected=expected, matches=not wrong,
        detail=(_brief(wrong) if wrong
                else f"disabled by exit latency > {CSTATE_MAX_LATENCY_US} us"),
    )]


def read_thp(*, sysfs: Path = SYSFS) -> list[HostReading]:
    """THP `enabled` (Tier-1), `defrag` and `shmem_enabled` (recorded).

    `shmem_enabled` is here because it decides whether the heap-equalized cell
    measures a collector or a kernel default: a box that leaves it `never` gives
    huge pages to every collector EXCEPT ZGC, whose heap is shmem-backed. It is
    Tier-2 because host-setup.sh sets only `enabled`.
    """
    base = sysfs / "kernel" / "mm" / "transparent_hugepage"
    out: list[HostReading] = []
    for attr, knob, tier, expected in (
        (ATTR_THP_ENABLED, "enabled", TIER_1, "madvise"),
        (ATTR_THP_DEFRAG, "defrag", TIER_2, None),
        (ATTR_THP_SHMEM, "shmem_enabled", TIER_2, None),
    ):
        raw = _read_text(base / knob)
        active = thp_active(raw) if raw is not None else None
        out.append(HostReading(
            attribute=attr, kind=KIND_SETTING, tier=tier,
            paths=[str(base / knob)], value=active, expected=expected,
            matches=(None if active is None else (active == expected if expected else None)),
            detail=("absent or unbracketed" if active is None else f"raw: {raw}"),
        ))
    return out


def read_irq(exp: HostExpectation, *, procfs: Path = PROCFS) -> list[HostReading]:
    """IRQ placement: recorded in full, gated nowhere. See the module docstring.

    `default_smp_affinity` -- the one knob host-setup.sh writes -- governs only
    interrupts allocated AFTER the write, so it evidences nothing about the
    interrupts already on the SUT cores. Those are recorded per IRQ with their
    names, and the per-cpu counts are the outcome. Measured on the bench box,
    the nvme blk-mq queues are managed interrupts pinned to cores 0-5 that the
    kernel refuses to move at all.
    """
    out: list[HostReading] = []
    default_path = procfs / "irq" / "default_smp_affinity"
    raw = _read_text(default_path)
    mask = parse_hex_mask(raw) if raw is not None else None
    out.append(HostReading(
        attribute=ATTR_IRQ_DEFAULT, kind=KIND_SETTING, tier=TIER_2,
        paths=[str(default_path)], value=raw,
        expected=exp.housekeeping_raw,
        matches=(None if mask is None else mask == exp.housekeeping_cpus),
        detail=("absent" if mask is None else
                "governs only interrupts allocated after the write; it does not "
                "move the ones already on the SUT cores"),
    ))

    cpus, rows = parse_interrupts(_read_text(procfs / "interrupts") or "")
    table: dict[str, str] = {}
    on_sut: list[str] = []
    for entry in sorted((procfs / "irq").glob("*"), key=lambda p: p.name):
        if not entry.name.isdigit():
            continue
        value = _read_text(entry / "smp_affinity_list")
        if value is None:
            continue
        table[entry.name] = value
        if (_cpu_set(value) or frozenset()) & exp.sut_cpus:
            name = rows.get(entry.name, {}).get("name", "")
            on_sut.append(f"{entry.name}({name})={value}")
    out.append(HostReading(
        attribute=ATTR_IRQ_TABLE, kind=KIND_SETTING, tier=TIER_2,
        paths=[str(procfs / "irq" / "*" / "smp_affinity_list")],
        value=table, expected=f"no intersection with SUT cpus {exp.sut_cpus_raw}",
        matches=not on_sut,
        detail=(_brief(on_sut, 8) if on_sut else "no IRQ is steered onto a SUT core"),
    ))

    totals = {
        cpu: sum(r["counts"].get(cpu, 0) for r in rows.values())
        for cpu in cpus if cpu in exp.sut_cpus
    }
    out.append(HostReading(
        attribute=ATTR_IRQ_COUNTS, kind=KIND_OUTCOME, tier=TIER_2,
        paths=[str(procfs / "interrupts")], value=totals,
        detail="lifetime counts at phase time; a delta needs two reads",
    ))
    return out


def read_daemon_affinity(
    units: tuple[str, ...] = DAEMON_UNITS, *, procfs: Path = PROCFS, exec_=run,
) -> list[HostReading]:
    """`Cpus_allowed_list` per daemon. RECORDED, not gated: no setter exists.

    host-setup.sh writes a `system.conf.d` CPUAffinity drop-in, which applies to
    units started AFTERWARDS; already-running dockerd keeps its old mask
    (measured: `0-31`). Gating this would abort every run until host-setup also
    issued `systemctl set-property`, with a state_record so restore can undo it.
    """
    found: dict[str, str] = {}
    missing: list[str] = []
    for unit in units:
        res = exec_(["systemctl", "show", "-p", "MainPID", "--value", unit], timeout=15)
        pid = res.stdout.strip() if res.ok else ""
        if not pid.isdigit() or pid == "0":
            missing.append(unit)
            continue
        status = _read_text(procfs / pid / "status")
        allowed = parse_cpus_allowed(status) if status else None
        if allowed is None:
            missing.append(unit)
            continue
        found[unit] = allowed
    return [HostReading(
        attribute=ATTR_DAEMON_AFFINITY, kind=KIND_SETTING, tier=TIER_2,
        paths=[str(procfs / "<pid>" / "status")], value=found,
        detail=("recorded, not gated: the system.conf.d drop-in applies to units "
                "started after it, and host-setup.sh issues no set-property, so a "
                "running dockerd keeps its old mask"
                + (f"; unreadable: {', '.join(missing)}" if missing else "")),
    )]


def read_pgdata(exp: HostExpectation, *, exec_=run) -> list[HostReading]:
    """PGDATA's CoW attribute and mount options, verbatim.

    Recorded, never gated: `chattr +C` only takes on a (near-)empty directory
    and host-setup.sh explicitly refuses to remount a live docker volume, so
    both are detect-only there too. The options string is stored whole -- the
    bench box mounts PGDATA's filesystem with `compress=zstd:1`, transparent
    compression on the DB volume that no other artifact records.
    """
    attrs_res = exec_(["lsattr", "-d", exp.pgdata_path], timeout=15)
    attrs = attrs_res.stdout.split()[0] if attrs_res.ok and attrs_res.stdout.split() else None
    mount_res = exec_(
        ["findmnt", "-no", "OPTIONS", "--target", exp.pgdata_path], timeout=15
    )
    options = mount_res.stdout.strip() if mount_res.ok else None
    return [
        HostReading(
            attribute=ATTR_PGDATA_ATTRS, kind=KIND_SETTING, tier=TIER_2,
            paths=[exp.pgdata_path], value=attrs, expected="C (nodatacow)",
            matches=(None if attrs is None else "C" in attrs),
            detail=("lsattr unavailable or path absent" if attrs is None else ""),
        ),
        HostReading(
            attribute=ATTR_PGDATA_MOUNT, kind=KIND_SETTING, tier=TIER_2,
            paths=[exp.pgdata_path], value=options, expected="noatime",
            matches=(None if options is None else "noatime" in options.split(",")),
            detail=("findmnt unavailable" if options is None
                    else "host-setup.sh detects only; it never remounts a live volume"),
        ),
    ]


def read_services(exp: HostExpectation, *, exec_=run) -> list[HostReading]:
    """`is-active` (Tier-1: host-setup.sh stops them) and `is-enabled` (recorded).

    `is-enabled` is not gated because host-setup.sh never disables anything:
    tuned and irqbalance read `enabled` on the bench box even while stopped, so
    a gate there fails every run. It is worth recording -- an enabled-but-stopped
    unit comes back after a reboot, which is exactly the resume case.
    """
    active: dict[str, str] = {}
    enabled: dict[str, str] = {}
    for svc in exp.stop_services:
        unit = f"{svc}.service"
        active[unit] = (exec_(["systemctl", "is-active", unit], timeout=15).stdout.strip()
                        or "unknown")
        enabled[unit] = (exec_(["systemctl", "is-enabled", unit], timeout=15).stdout.strip()
                         or "unknown")
    running = sorted(u for u, s in active.items() if s == "active")
    return [
        HostReading(
            attribute=ATTR_SERVICES_ACTIVE, kind=KIND_SETTING, tier=TIER_1,
            value=active, expected="not active", matches=not running,
            detail=("still running: " + ", ".join(running)) if running else "",
        ),
        HostReading(
            attribute=ATTR_SERVICES_ENABLED, kind=KIND_SETTING, tier=TIER_2,
            value=enabled,
            detail="recorded, not gated: host-setup.sh stops these units, never disables them",
        ),
    ]


def read_isolation(
    exp: HostExpectation, *, sysfs: Path = SYSFS, procfs: Path = PROCFS,
) -> list[HostReading]:
    """isolcpus / nohz_full, compared as cpu SETS, SAYING WHICH SOURCE ANSWERED.

    `0,1,2,3,4,5` and `0-5` are the same isolation and different strings; the
    `grep -qw "isolcpus=${SUT_CPUS}"` in host-setup.sh's `check_isolate` calls the
    first absent.

    Sysfs OUTRANKS `/proc/cmdline`, and that is the whole point of reading both.
    The cmdline is a request; sysfs is the kernel's answer. A cmdline carrying
    `isolcpus=0-5` that the kernel ignored (a bad flag token, the option
    compiled out) leaves `/sys/.../isolated` empty, and falling back to the
    request there published a demand as the state -- `matches: True` on cores
    the only authority says are not isolated. The cmdline answers only where
    sysfs has no file at all, and the reading records which one it was.
    """
    cmdline_raw = _read_text(procfs / "cmdline") or ""
    cmdline = parse_cmdline(cmdline_raw)
    base = sysfs / "devices" / "system" / "cpu"
    out: list[HostReading] = []
    for attr, key, sysfs_name in (
        (ATTR_ISOLCPUS, "isolcpus", "isolated"),
        (ATTR_NOHZ_FULL, "nohz_full", "nohz_full"),
    ):
        live = _read_text(base / sysfs_name)
        declared = cmdline.get(key, "")
        if live is None:
            raw, source = declared, f"{procfs / 'cmdline'} (declared, unconfirmed)"
        else:
            raw, source = live, f"{base / sysfs_name} (the kernel's own answer)"
        parsed = _cpu_set(raw) if raw else None
        if parsed is None:
            detail = f"source: {source}; the SUT cores are not isolated"
            if live is not None and declared:
                detail += (f"; the cmdline asks for {key}={declared} and the "
                           "kernel did not honour it")
        else:
            detail = (f"source: {source}; compared as sets "
                      f"({format_cpuset(sorted(parsed))})")
        out.append(HostReading(
            attribute=attr, kind=KIND_SETTING, tier=TIER_2,
            paths=[str(base / sysfs_name), str(procfs / "cmdline")],
            value=raw or None, expected=exp.sut_cpus_raw,
            matches=(None if parsed is None else parsed == exp.sut_cpus),
            detail=detail,
        ))
    out.append(HostReading(
        attribute=ATTR_CMDLINE, kind=KIND_SETTING, tier=TIER_2,
        paths=[str(procfs / "cmdline")], value=cmdline_raw or None,
        detail="recorded verbatim; no expectation is declared for the whole line",
    ))
    return out


def read_topology(*, sysfs: Path = SYSFS) -> list[HostReading]:
    """L3 groups, SMT siblings and the pstate driver -- the CCD discriminator.

    This is what turns compose.bench.yaml's core-id note into an
    artifact: the cpusets assume the SUT's six cores share one CCD's L3 and that
    everything else sits on the other, and `shared_cpu_list` at cache level 3 is
    the only thing that can show it.
    """
    base = sysfs / "devices" / "system" / "cpu"
    l3: dict[str, str] = {}
    siblings: dict[str, str] = {}
    epp: dict[str, str] = {}
    driver: dict[str, str] = {}
    for cpu in _cpu_dirs(sysfs):
        for index in sorted((cpu / "cache").glob("index*")) if (cpu / "cache").is_dir() else []:
            if _read_text(index / "level") == "3":
                shared = _read_text(index / "shared_cpu_list")
                if shared:
                    l3[cpu.name] = shared
        sib = _read_text(cpu / "topology" / "thread_siblings_list")
        if sib:
            siblings[cpu.name] = sib
        pref = _read_text(cpu / "cpufreq" / "energy_performance_preference")
        if pref:
            epp[cpu.name] = pref
        drv = _read_text(cpu / "cpufreq" / "scaling_driver")
        if drv:
            driver[cpu.name] = drv
    value = {
        # cpu ids sort NUMERICALLY: a lexical sort puts cpu10 before cpu2 and
        # makes a CCD grouping unreadable in the artifact it exists to evidence.
        "l3_groups": {group: sorted((c for c, g in l3.items() if g == group),
                                    key=lambda name: int(name[3:]))
                      for group in sorted(set(l3.values()))},
        "thread_siblings": siblings,
        "smt_control": _read_text(base / "smt" / "control"),
        "scaling_driver": sorted(set(driver.values())),
        "energy_performance_preference": sorted(set(epp.values())),
        "amd_pstate_status": _read_text(base / "amd_pstate" / "status"),
    }
    return [HostReading(
        attribute=ATTR_TOPOLOGY, kind=KIND_SETTING, tier=TIER_2,
        paths=[str(base / "cpu*/cache/index*/shared_cpu_list")], value=value,
        detail="recorded as evidence for the compose cpusets; no target declared",
    )]


def read_dropin(exp: HostExpectation) -> list[HostReading]:
    """The systemd CPUAffinity drop-in's CONTENT (host-setup.sh `tune_systemd_affinity`).

    A settings compare, not a process compare: whether running daemons actually
    moved is `read_daemon_affinity`, and they do not.

    `SYSTEMD_DROPIN` is in `HOST_SETUP_ENV_DEFAULTS`, so the path this reads is
    the path the script was told to write. It used to be a literal in the script
    and a variable here, which made the env var an OFF SWITCH for a Tier-1 gate:
    point it anywhere and the script still wrote /etc/..., this read `None`, and
    `matches=None` filed the one attribute evidencing daemon placement under
    Tier-2 notes while `tier1_failures` stayed empty.
    """
    want = f"[Manager]\nCPUAffinity={exp.housekeeping_raw}\n"
    got = _read_text(Path(exp.systemd_dropin))
    return [HostReading(
        attribute=ATTR_DROPIN, kind=KIND_SETTING, tier=TIER_1,
        paths=[exp.systemd_dropin], value=got, expected=want.strip(),
        matches=(None if got is None else got == want.strip()),
        detail=("drop-in absent" if got is None else ""),
    )]


def read_host_state(
    exp: HostExpectation, *, sysfs: Path = SYSFS, procfs: Path = PROCFS, exec_=run,
) -> list[HostReading]:
    """Every attribute, in one pass. The order is the artifact's row order."""
    return [
        *read_governor(sysfs=sysfs),
        *read_boost(sysfs=sysfs),
        *read_cpu_freq(exp, sysfs=sysfs),
        *read_cstates(sysfs=sysfs),
        *read_thp(sysfs=sysfs),
        *read_irq(exp, procfs=procfs),
        *read_daemon_affinity(procfs=procfs, exec_=exec_),
        *read_pgdata(exp, exec_=exec_),
        *read_services(exp, exec_=exec_),
        *read_isolation(exp, sysfs=sysfs, procfs=procfs),
        *read_topology(sysfs=sysfs),
        *read_dropin(exp),
    ]


def topology_summary(readings: list[HostReading]) -> tuple[dict[str, str], str | None]:
    """(L3 group -> cpu list, smt/control) out of the topology reading."""
    for r in readings:
        if r.attribute == ATTR_TOPOLOGY and isinstance(r.value, dict):
            groups = r.value.get("l3_groups") or {}
            return (
                {str(k): ",".join(v) for k, v in groups.items()},
                r.value.get("smt_control"),
            )
    return {}, None

