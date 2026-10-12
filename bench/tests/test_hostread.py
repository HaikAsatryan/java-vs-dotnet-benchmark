"""B6 host-state read-back, against FIXTURE sysfs/procfs trees under tmp_path.

Every reader takes its roots as parameters precisely so these tests drive the
real code with no root, no cgroups and no Linux: the box that runs the suite is
not the box that gets tuned, and a parser only reachable through /sys is a
parser the suite cannot mutation-test.

The fixture values are the ones MEASURED on the bench box (C-state latencies
0/1/18/350, nvme managed IRQs on cores 0-5, both boost knobs present), because
the rows this module refuses to gate are exactly the ones that are
unsatisfiable there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ledgerbench import hostread
from ledgerbench.hostread import (
    HostExpectation,
    cpuidle_states,
    cstate_expectation,
    evaluate,
    parse_cmdline,
    parse_cpus_allowed,
    parse_hex_mask,
    parse_interrupts,
    parse_pg_settings,
    thp_active,
)
from ledgerbench.manifest import HostReading
from ledgerbench.util.proc import ProcResult

# The bench box's own cpuidle table: POLL/0, C1/1, C2/18, C3/350 us.
MEASURED_CSTATES = (("POLL", 0), ("C1", 1), ("C2", 18), ("C3", 350))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tuned_sysfs(
    root: Path, *, cpus: int = 8, governor: str = "performance", boost: str = "0",
    thp: str = "always [madvise] never", cstates: bool = True,
) -> Path:
    """A sysfs tree in the state `host-setup.sh --tune` leaves behind."""
    sysfs = root / "sys"
    base = sysfs / "devices" / "system" / "cpu"
    _write(base / "cpufreq" / "boost", f"{boost}\n")
    _write(base / "smt" / "control", "on\n")
    for i in range(cpus):
        cpu = base / f"cpu{i}"
        _write(cpu / "cpufreq" / "scaling_governor", f"{governor}\n")
        _write(cpu / "cpufreq" / "scaling_driver", "amd-pstate-epp\n")
        _write(cpu / "cpufreq" / "scaling_cur_freq", "3000000\n")
        _write(cpu / "topology" / "thread_siblings_list", f"{i},{i + cpus}\n")
        # L1 is per-core and L3 is the CCD discriminator; only level 3 is a
        # placement fact, so both are in the fixture.
        _write(cpu / "cache" / "index0" / "level", "1\n")
        _write(cpu / "cache" / "index0" / "shared_cpu_list", f"{i}\n")
        _write(cpu / "cache" / "index3" / "level", "3\n")
        _write(cpu / "cache" / "index3" / "shared_cpu_list",
               "0-3" if i < 4 else "4-7")
        _write(base / "cpufreq" / f"policy{i}" / "boost", f"{boost}\n")
        if cstates:
            for idx, (name, latency) in enumerate(MEASURED_CSTATES):
                state = cpu / "cpuidle" / f"state{idx}"
                _write(state / "name", f"{name}\n")
                _write(state / "latency", f"{latency}\n")
                _write(state / "disable", "1\n" if latency > 2 else "0\n")
    _write(sysfs / "kernel" / "mm" / "transparent_hugepage" / "enabled", f"{thp}\n")
    _write(sysfs / "kernel" / "mm" / "transparent_hugepage" / "defrag",
           "always defer defer+madvise [madvise] never\n")
    _write(sysfs / "kernel" / "mm" / "transparent_hugepage" / "shmem_enabled",
           "always within_size advise [never] deny force\n")
    return sysfs


def tuned_procfs(root: Path, *, irq_affinity: dict[str, str] | None = None) -> Path:
    """A procfs tree with the bench box's nvme managed-IRQ placement."""
    procfs = root / "proc"
    _write(procfs / "cmdline", "BOOT_IMAGE=/vmlinuz root=UUID=x quiet\n")
    _write(procfs / "irq" / "default_smp_affinity", "00000000,00008000\n")
    affinity = irq_affinity if irq_affinity is not None else {
        "83": "1", "85": "3", "87": "5", "111": "2", "113": "0", "9": "0-31",
    }
    for irq, cpus in affinity.items():
        _write(procfs / "irq" / irq / "smp_affinity_list", f"{cpus}\n")
    rows = "\n".join(
        f"{irq:>4}:{'       10' * 4}   IR-PCI-MSIX-0000:0e:00.0  nvme0q{irq}"
        for irq in affinity
    )
    _write(procfs / "interrupts",
           "           CPU0       CPU1       CPU2       CPU3\n" + rows + "\n")
    return procfs


def _expectation(**kw) -> HostExpectation:
    env = {"SUT_CPUS": "0-5", "HOUSEKEEPING_CPUS": "15", **kw}
    return HostExpectation.from_env(env)


def _fake_exec(mapping: dict[str, tuple[int, str]]):
    """A `run` stand-in keyed by the first two argv tokens."""

    def exec_(args, **kw):
        key = " ".join(args[:2])
        rc, out = mapping.get(key, (1, ""))
        return ProcResult(args=list(args), returncode=rc, stdout=out, stderr="")

    return exec_


# --------------------------------------------------------------------------- #
# pure parsers
# --------------------------------------------------------------------------- #
def test_thp_bracket_parser_extracts_the_active_token():
    """The active mode is the BRACKETED token, never the first one.

    `sed -n 's/.*\\[\\(.*\\)\\].*/\\1/p'` is what host-setup.sh reads; taking the
    first word instead publishes `always` for a box set to `madvise`, i.e. the
    mode it is furthest from.
    """
    assert thp_active("always [madvise] never") == "madvise"
    assert thp_active("[always] madvise never") == "always"
    # no bracket -> unreadable, and unreadable is None, never "" (which would
    # serialize as a measured empty mode)
    assert thp_active("always madvise never") is None
    assert thp_active("") is None


def test_cstate_expectation_is_by_latency_not_by_index():
    """`cpupower idle-set -D 2` disables by EXIT LATENCY, not by state index.

    host-setup.sh's `tune_cstates` passes `-D 2`, which is easy to misread as
    "state index >= 2". On this box the two rules pick the same states (POLL/0,
    C1/1, C2/18, C3/350), which is luck.
    The synthetic table below is where they disagree, and it is the only thing
    that can tell the two implementations apart.
    """
    measured = [(n, 0, lat) for n, lat in MEASURED_CSTATES]
    assert cstate_expectation(measured) == ["C2", "C3"]

    # index and latency disagree: state1 is SLOW, state2 is fast.
    inverted = [("POLL", 0, 0), ("C1", 0, 40), ("C2", 0, 1)]
    assert cstate_expectation(inverted) == ["C1"]


def test_cpuidle_states_are_read_in_state_index_order(tmp_path):
    """state10 must not sort between state1 and state2 (the lexical trap)."""
    sysfs = tuned_sysfs(tmp_path, cpus=1)
    cpu0 = sysfs / "devices/system/cpu/cpu0"
    for name, latency in (("C4", 900),):
        state = cpu0 / "cpuidle" / "state10"
        _write(state / "name", f"{name}\n")
        _write(state / "latency", f"{latency}\n")
        _write(state / "disable", "1\n")
    assert [n for n, _d, _lat in cpuidle_states(cpu0)] == ["POLL", "C1", "C2", "C3", "C4"]


def test_irq_default_mask_hex_parser():
    """/proc/irq/default_smp_affinity is hex, comma-grouped into 32-bit words.

    The groups are ONE number split for readability, most significant first --
    not independent masks. On a >32-cpu box the high word is where the cpus the
    SUT does not own live, so a per-group parse silently loses cpu 32 and up.
    """
    assert parse_hex_mask("00000000,00008000") == frozenset({15})
    assert parse_hex_mask("00000001,00000000") == frozenset({32})
    assert parse_hex_mask("ffffffff,ffffffff") == frozenset(range(64))
    assert parse_hex_mask("ffffffff") == frozenset(range(32))
    assert parse_hex_mask("") == frozenset()


def test_cmdline_parser_keeps_bare_flags():
    parsed = parse_cmdline("BOOT_IMAGE=/vmlinuz quiet isolcpus=0-5 nohz_full=0-5")
    assert parsed["isolcpus"] == "0-5"
    assert parsed["quiet"] == ""


def test_cpus_allowed_parser():
    status = "Name:\tdockerd\nCpus_allowed:\tffffffff\nCpus_allowed_list:\t0-31\n"
    assert parse_cpus_allowed(status) == "0-31"
    assert parse_cpus_allowed("Name:\tx\n") is None


def test_interrupts_parser_keeps_names_and_per_cpu_counts():
    text = (
        "           CPU0       CPU1\n"
        "  83:       1000       2000   IR-PCI-MSIX-0000:0e:00.0  nvme0q1\n"
        " NMI:          3          4   Non-maskable interrupts\n"
    )
    cpus, rows = parse_interrupts(text)
    assert cpus == [0, 1]
    assert rows["83"]["counts"] == {0: 1000, 1: 2000}
    assert "nvme0q1" in rows["83"]["name"]
    # non-numeric rows are real per-cpu work and are kept
    assert rows["NMI"]["counts"] == {0: 3, 1: 4}


def test_the_machine_wide_err_and_mis_totals_are_not_charged_to_cpu0():
    """`ERR`/`MIS` print ONE total, not a per-cpu column.

    Measured: this box's /proc/interrupts ends with `ERR: 0` and `MIS: 0`.
    Consuming that single field as `counts[0]` charged every spurious interrupt
    on the whole machine to cpu 0, inflating the SUT's core 0 in the one
    artifact that exists to show interrupt load on the SUT cores. NMI/LOC/RES
    genuinely are per-cpu and stay.
    """
    text = (
        "           CPU0       CPU1\n"
        " NMI:          3          4   Non-maskable interrupts\n"
        " ERR:         17\n"
        " MIS:          0\n"
    )
    _cpus, rows = parse_interrupts(text)
    assert rows["NMI"]["counts"] == {0: 3, 1: 4}
    assert rows["ERR"]["counts"] == {}
    assert rows["MIS"]["counts"] == {}
    totals = {cpu: sum(r["counts"].get(cpu, 0) for r in rows.values())
              for cpu in (0, 1)}
    assert totals == {0: 3, 1: 4}


def test_pg_settings_parser_keys_on_name_and_keeps_source_and_reset_val():
    """Keyed by NAME. `setting` collapses every parameter sharing a value."""
    tsv = (
        "shared_buffers\t262144\t8kB\tconfiguration file\t16384\t262144\n"
        "fsync\ton\t\tdefault\ton\ton\n"
    )
    parsed = parse_pg_settings(tsv)
    assert parsed["shared_buffers"]["setting"] == "262144"
    assert parsed["shared_buffers"]["unit"] == "8kB"
    assert parsed["shared_buffers"]["source"] == "configuration file"
    assert parsed["shared_buffers"]["reset_val"] == "262144"
    assert set(parsed) == {"shared_buffers", "fsync"}


def test_the_expectation_comes_from_host_setups_own_env():
    """Same env vars host-setup.sh reads, so the two cannot drift apart."""
    exp = HostExpectation.from_env({})
    assert exp.sut_cpus == frozenset(range(6))
    assert exp.housekeeping_cpus == frozenset({15})
    assert "tuned" in exp.stop_services and "irqbalance" in exp.stop_services
    override = HostExpectation.from_env({"SUT_CPUS": "0-1", "HOUSEKEEPING_CPUS": "7"})
    assert override.sut_cpus == frozenset({0, 1})
    assert override.housekeeping_raw == "7"


# --------------------------------------------------------------------------- #
# the SETTING / OUTCOME rule
# --------------------------------------------------------------------------- #
def test_no_tier_1_reading_may_be_an_outcome():
    """The rule mechanised, rather than written in prose and hoped for.

    An outcome is load- and time-dependent: gating one produces a run that
    fails for a reason no operator can fix, and the only recourse is an
    opt-out, which is how round 1's capacity gate stopped being enforced.
    """
    outcome = HostReading(
        attribute=hostread.ATTR_IRQ_COUNTS, kind=hostread.KIND_OUTCOME,
        tier=hostread.TIER_1, value={0: 10},
    )
    with pytest.raises(ValueError, match="Tier-1 reading must be a setting"):
        evaluate([outcome])


def test_a_not_evaluable_tier_1_gets_its_own_bucket_not_the_tier_2_notes():
    """`matches is None` = the knob is absent. Not a pass, not a failure, and
    not a Tier-2 note either.

    A box with no `cpufreq/boost` and no `policy*/boost` (an Intel `no_turbo`
    box, or a VM) cannot evaluate boost at all. Folding that into `tier2_notes`
    made `tier1_failures: []` mean two different things -- "checked and matched"
    and "never checked" -- while the RUNBOOK's "a Tier-1 mismatch aborts the
    run" read as a guarantee that boost was verified off.
    """
    absent = HostReading(
        attribute=hostread.ATTR_BOOST, kind=hostread.KIND_SETTING,
        tier=hostread.TIER_1, expected="0", matches=None, detail="no boost knob",
    )
    tier2_absent = HostReading(
        attribute=hostread.ATTR_PGDATA_ATTRS, kind=hostread.KIND_SETTING,
        tier=hostread.TIER_2, expected="C (nodatacow)", matches=None,
        detail="lsattr unavailable",
    )
    tier1, tier2, unevaluable = evaluate([absent, tier2_absent])
    assert tier1 == []
    assert unevaluable and hostread.ATTR_BOOST in unevaluable[0]
    assert not any(hostread.ATTR_BOOST in note for note in tier2)
    assert tier2 and hostread.ATTR_PGDATA_ATTRS in tier2[0]


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #
def test_governor_records_the_set_and_fails_on_one_flipped_cpu(tmp_path):
    """The SET across every CPU, not cpu0's value published as the box's."""
    sysfs = tuned_sysfs(tmp_path)
    ok = hostread.read_governor(sysfs=sysfs)
    assert ok[0].value == ["performance"]
    assert ok[0].matches is True
    assert len(ok[0].paths) == 8

    flipped = sysfs / "devices/system/cpu/cpu5/cpufreq/scaling_governor"
    flipped.write_text("schedutil\n", encoding="utf-8")
    readings = hostread.read_governor(sysfs=sysfs)
    assert sorted(readings[0].value) == ["performance", "schedutil"]
    tier1, _, _ = evaluate(readings)
    assert len(tier1) == 1 and str(flipped) in tier1[0]


def test_boost_reads_every_knob_that_answered(tmp_path):
    """Both knobs exist on the bench box and host-setup.sh writes only one.

    `tune_boost` takes the global `cpufreq/boost` branch and RETURNS; the
    per-policy knobs are touched only when the global one is absent. A write
    that does not propagate is invisible to everything except this read.
    """
    sysfs = tuned_sysfs(tmp_path)
    (sysfs / "devices/system/cpu/cpufreq/policy3/boost").write_text("1\n", encoding="utf-8")
    readings = hostread.read_boost(sysfs=sysfs)
    assert len(readings[0].paths) == 9      # the global knob + 8 policies
    tier1, _, _ = evaluate(readings)
    assert len(tier1) == 1 and str(Path("policy3") / "boost") in tier1[0]


def test_boost_absent_is_not_evaluable_rather_than_a_failure(tmp_path):
    sysfs = tuned_sysfs(tmp_path, cpus=1)
    (sysfs / "devices/system/cpu/cpufreq/boost").unlink()
    (sysfs / "devices/system/cpu/cpufreq/policy0/boost").unlink()
    readings = hostread.read_boost(sysfs=sysfs)
    assert readings[0].matches is None
    assert evaluate(readings)[0] == []


def test_cstates_read_the_parsed_table_and_flag_a_wrong_disable(tmp_path):
    sysfs = tuned_sysfs(tmp_path, cpus=2)
    ok = hostread.read_cstates(sysfs=sysfs)
    assert ok[0].matches is True
    assert ok[0].value["cpu0"] == [["POLL", 0, 0], ["C1", 0, 1], ["C2", 1, 18],
                                   ["C3", 1, 350]]
    assert ok[0].expected == ["C2", "C3"]

    (sysfs / "devices/system/cpu/cpu1/cpuidle/state3/disable").write_text("0\n")
    readings = hostread.read_cstates(sysfs=sysfs)
    tier1, tier2, _ = evaluate(readings)
    assert tier1 == []                     # cpupower may be absent: never Tier-1
    assert tier2 and "cpu1/C3" in tier2[0]


def test_missing_cpuidle_is_tier_2_skipped_not_a_failure(tmp_path):
    """A box without cpupower must still be able to pass.

    host-setup.sh's `tune_cstates` detect-and-skips when cpupower is absent, so
    a C-state gate would refuse a run the tuning script itself considers tuned.
    """
    sysfs = tuned_sysfs(tmp_path, cstates=False)
    readings = hostread.read_cstates(sysfs=sysfs)
    assert readings[0].tier == hostread.TIER_2
    assert readings[0].matches is None
    tier1, tier2, _ = evaluate(readings)
    assert tier1 == []
    assert "cpuidle" in tier2[0]


def test_thp_gates_enabled_and_only_records_defrag_and_shmem(tmp_path):
    """`shmem_enabled` is recorded because it can INVALIDATE the heap cell.

    A box that leaves it `never` gives huge pages to every collector except
    ZGC, whose heap is shmem-backed, so the heap-equalized cell would measure a
    kernel default rather than a collector. It is not gated: host-setup.sh sets
    only `enabled`, and gating a knob nothing sets is the over-declaration this
    round exists to remove.
    """
    sysfs = tuned_sysfs(tmp_path, thp="[always] madvise never")
    by_attr = {r.attribute: r for r in hostread.read_thp(sysfs=sysfs)}
    assert by_attr[hostread.ATTR_THP_ENABLED].tier == hostread.TIER_1
    assert by_attr[hostread.ATTR_THP_ENABLED].matches is False
    assert by_attr[hostread.ATTR_THP_SHMEM].value == "never"
    assert by_attr[hostread.ATTR_THP_SHMEM].tier == hostread.TIER_2
    assert by_attr[hostread.ATTR_THP_SHMEM].matches is None
    assert by_attr[hostread.ATTR_THP_DEFRAG].value == "madvise"


def test_managed_irqs_pinned_to_sut_cores_do_not_fail_the_run(tmp_path):
    """The plan's Tier-1 IRQ rule is unsatisfiable on this hardware.

    Measured: /proc/irq/{83,85,87,111,113}/smp_affinity_list = {1,3,5,2,0}, all
    nvme blk-mq queues. Those are MANAGED interrupts -- the kernel refuses
    writes to their smp_affinity -- so "fail when any IRQ mask intersects
    SUT_CPUS" cannot be satisfied by any operator action. Recorded in full,
    gated nowhere.
    """
    procfs = tuned_procfs(tmp_path)
    readings = hostread.read_irq(_expectation(), procfs=procfs)
    tier1, tier2, _ = evaluate(readings)
    assert tier1 == []
    by_attr = {r.attribute: r for r in readings}
    table = by_attr[hostread.ATTR_IRQ_TABLE]
    assert table.tier == hostread.TIER_2
    assert table.matches is False
    assert table.value == {"111": "2", "113": "0", "83": "1", "85": "3",
                           "87": "5", "9": "0-31"}
    assert "nvme" in table.detail
    # and the default mask, the ONE knob host-setup.sh writes, says what it
    # does not establish
    default = by_attr[hostread.ATTR_IRQ_DEFAULT]
    assert default.matches is True
    assert "after the write" in default.detail
    # the per-cpu counts are an OUTCOME and can gate nothing
    assert by_attr[hostread.ATTR_IRQ_COUNTS].kind == hostread.KIND_OUTCOME


def test_isolcpus_compares_sets_not_strings(tmp_path):
    """`0,1,2,3,4,5` and `0-5` are the same isolation.

    host-setup.sh's `check_isolate` greps for the literal
    `isolcpus=${SUT_CPUS}`, so a kernel cmdline written the other way reads as
    "absent".
    """
    sysfs = tuned_sysfs(tmp_path, cpus=1)
    procfs = tuned_procfs(tmp_path)
    (procfs / "cmdline").write_text("quiet isolcpus=0,1,2,3,4,5 nohz_full=0-5\n")
    by_attr = {r.attribute: r
               for r in hostread.read_isolation(_expectation(), sysfs=sysfs, procfs=procfs)}
    assert by_attr[hostread.ATTR_ISOLCPUS].matches is True
    assert by_attr[hostread.ATTR_NOHZ_FULL].matches is True
    assert "isolcpus=0,1,2,3,4,5" in by_attr[hostread.ATTR_CMDLINE].value


def test_isolcpus_absent_reads_as_absent_and_never_as_a_failure(tmp_path):
    """Measured: `isolated` and `nohz_full` are both empty on the bench box."""
    sysfs = tuned_sysfs(tmp_path, cpus=1)
    procfs = tuned_procfs(tmp_path)
    readings = hostread.read_isolation(_expectation(), sysfs=sysfs, procfs=procfs)
    by_attr = {r.attribute: r for r in readings}
    assert by_attr[hostread.ATTR_ISOLCPUS].matches is None
    assert "not isolated" in by_attr[hostread.ATTR_ISOLCPUS].detail
    assert evaluate(readings)[0] == []


def test_sysfs_outranks_the_cmdline_and_the_reading_says_which_answered(tmp_path):
    """A cmdline the kernel IGNORED must not answer for the kernel.

    `isolcpus=0-5` with an empty `/sys/devices/system/cpu/isolated` is a request
    the kernel did not honour (a bad flag token, the option compiled out).
    Falling back to the cmdline there published `matches: True` and
    `compared as sets (0-5)` on cores the only authority says are not isolated.
    Where sysfs has no file at all the cmdline may answer, and the reading has
    to say so -- `read_boost` records which knob answered and this did not.
    """
    sysfs = tuned_sysfs(tmp_path, cpus=1)
    procfs = tuned_procfs(tmp_path)
    (procfs / "cmdline").write_text("quiet isolcpus=0-5 nohz_full=0-5\n")

    # no sysfs file: the cmdline answers, LABELLED as unconfirmed
    declared = {r.attribute: r for r in
                hostread.read_isolation(_expectation(), sysfs=sysfs, procfs=procfs)}
    assert declared[hostread.ATTR_ISOLCPUS].matches is True
    assert "declared, unconfirmed" in declared[hostread.ATTR_ISOLCPUS].detail

    # sysfs present and EMPTY: the kernel's answer is "not isolated"
    _write(sysfs / "devices" / "system" / "cpu" / "isolated", "\n")
    live = {r.attribute: r for r in
            hostread.read_isolation(_expectation(), sysfs=sysfs, procfs=procfs)}
    reading = live[hostread.ATTR_ISOLCPUS]
    assert reading.matches is None
    assert "not isolated" in reading.detail
    assert "did not honour it" in reading.detail
    assert evaluate([reading])[0] == []          # Tier-2: recorded, never fatal


def test_isolcpus_tolerates_the_kernels_flag_tokens(tmp_path):
    """`isolcpus=managed_irq,domain,0-5` is an ordinary cmdline, not a parse error."""
    sysfs = tuned_sysfs(tmp_path, cpus=1)
    procfs = tuned_procfs(tmp_path)
    (procfs / "cmdline").write_text("isolcpus=managed_irq,domain,0-5\n")
    by_attr = {r.attribute: r
               for r in hostread.read_isolation(_expectation(), sysfs=sysfs, procfs=procfs)}
    assert by_attr[hostread.ATTR_ISOLCPUS].matches is True


def test_services_gate_is_active_and_only_record_is_enabled():
    """host-setup.sh STOPS these units; it never disables them.

    Measured on the bench box: tuned and irqbalance are `enabled` while
    stopped, so gating `is-enabled` fails every run forever. `is-active` has a
    setter and is Tier-1.
    """
    exec_ = _fake_exec({
        "systemctl is-active": (3, "inactive"),
        "systemctl is-enabled": (0, "enabled"),
    })
    readings = hostread.read_services(_expectation(), exec_=exec_)
    by_attr = {r.attribute: r for r in readings}
    assert by_attr[hostread.ATTR_SERVICES_ACTIVE].tier == hostread.TIER_1
    assert by_attr[hostread.ATTR_SERVICES_ACTIVE].matches is True
    enabled = by_attr[hostread.ATTR_SERVICES_ENABLED]
    assert enabled.tier == hostread.TIER_2 and enabled.matches is None
    assert all(v == "enabled" for v in enabled.value.values())
    assert evaluate(readings)[0] == []


def test_a_still_running_service_is_a_tier_1_failure_naming_the_unit():
    exec_ = _fake_exec({
        "systemctl is-active": (0, "active"),
        "systemctl is-enabled": (0, "enabled"),
    })
    tier1, _, _ = evaluate(hostread.read_services(_expectation(), exec_=exec_))
    assert len(tier1) == 1 and "tuned.service" in tier1[0]


def test_daemon_affinity_is_recorded_not_gated(tmp_path):
    """dockerd measures 0-31 and no setter exists to move it.

    The system.conf.d drop-in applies to units started AFTER it, and
    host-setup.sh issues no `systemctl set-property`, so a Tier-1 row here
    would abort every run until the script grew a setter (with a state_record so
    host-restore.sh can undo it).
    """
    _write(tmp_path / "proc" / "4242" / "status",
           "Name:\tdockerd\nCpus_allowed_list:\t0-31\n")
    exec_ = _fake_exec({"systemctl show": (0, "4242")})
    readings = hostread.read_daemon_affinity(
        ("docker.service",), procfs=tmp_path / "proc", exec_=exec_)
    assert readings[0].tier == hostread.TIER_2
    assert readings[0].value == {"docker.service": "0-31"}
    assert evaluate(readings) == ([], [], [])


def test_pgdata_records_the_mount_options_verbatim():
    """Transparent btrfs compression on the DB volume is a fairness-relevant fact.

    Measured: `rw,relatime,seclabel,compress=zstd:1,ssd,discard=async,...` --
    and `relatime`, not `noatime`, which host-setup.sh only ever detects.
    """
    options = "rw,relatime,seclabel,compress=zstd:1,ssd,discard=async"
    exec_ = _fake_exec({
        "lsattr -d": (0, "---------------C---- /var/lib/pg\n"),
        "findmnt -no": (0, options + "\n"),
    })
    by_attr = {r.attribute: r for r in hostread.read_pgdata(_expectation(), exec_=exec_)}
    assert by_attr[hostread.ATTR_PGDATA_ATTRS].matches is True
    mount = by_attr[hostread.ATTR_PGDATA_MOUNT]
    assert mount.value == options              # verbatim, not a boolean
    assert mount.matches is False and mount.tier == hostread.TIER_2


def test_the_systemd_dropin_is_compared_by_content(tmp_path):
    dropin = tmp_path / "99-ledgerline-affinity.conf"
    dropin.write_text("[Manager]\nCPUAffinity=15\n", encoding="utf-8")
    exp = _expectation(SYSTEMD_DROPIN=str(dropin))
    assert hostread.read_dropin(exp)[0].matches is True

    dropin.write_text("[Manager]\nCPUAffinity=14\n", encoding="utf-8")
    stale = hostread.read_dropin(exp)
    assert stale[0].paths == [str(dropin)]
    assert stale[0].value == "[Manager]\nCPUAffinity=14"
    tier1, _, _ = evaluate(stale)
    assert len(tier1) == 1 and "CPUAffinity=15" in tier1[0]

    dropin.unlink()
    absent = hostread.read_dropin(exp)
    assert absent[0].matches is None                # absent != wrong
    assert evaluate(absent)[0] == []


def test_topology_reads_the_l3_groups_the_compose_cpusets_assume(tmp_path):
    """compose.bench.yaml's PLACEHOLDER core ids become an artifact.

    `shared_cpu_list` at cache level 3 is the CCD discriminator on Zen: it is
    the only read that can show the SUT's cores share one CCD's L3 and that
    everything else sits on the other.
    """
    sysfs = tuned_sysfs(tmp_path, cpus=12)
    # a cpu whose cache tree exposes no level-3 index contributes NOTHING: its
    # per-core L1 `shared_cpu_list` is not a CCD and must never be read as one
    for f in ("level", "shared_cpu_list"):
        (sysfs / f"devices/system/cpu/cpu7/cache/index3/{f}").unlink()
    reading = hostread.read_topology(sysfs=sysfs)[0]
    # ordered by cpu INDEX: cpu10 after cpu9, or the grouping is unreadable in
    # the artifact it exists to evidence
    assert reading.value["l3_groups"] == {
        "0-3": ["cpu0", "cpu1", "cpu2", "cpu3"],
        "4-7": ["cpu4", "cpu5", "cpu6", "cpu8", "cpu9", "cpu10", "cpu11"],
    }
    assert reading.value["smt_control"] == "on"
    assert reading.value["scaling_driver"] == ["amd-pstate-epp"]
    assert reading.value["thread_siblings"]["cpu0"] == "0,12"
    groups, smt = hostread.topology_summary([reading])
    assert groups["4-7"] == "cpu4,cpu5,cpu6,cpu8,cpu9,cpu10,cpu11"
    assert smt == "on"


def test_cpu_freq_is_an_outcome_and_covers_only_the_sut_cores(tmp_path):
    sysfs = tuned_sysfs(tmp_path)
    reading = hostread.read_cpu_freq(_expectation(), sysfs=sysfs)[0]
    assert reading.kind == hostread.KIND_OUTCOME
    assert set(reading.value["scaling_cur_freq_khz"]) == {f"cpu{i}" for i in range(6)}


# --------------------------------------------------------------------------- #
# the whole pass
# --------------------------------------------------------------------------- #
def test_a_tuned_box_passes_tier_1_end_to_end(tmp_path):
    sysfs = tuned_sysfs(tmp_path)
    procfs = tuned_procfs(tmp_path)
    dropin = tmp_path / "affinity.conf"
    dropin.write_text("[Manager]\nCPUAffinity=15\n", encoding="utf-8")
    exec_ = _fake_exec({
        "systemctl is-active": (3, "inactive"),
        "systemctl is-enabled": (1, "disabled"),
        "systemctl show": (0, "0"),
        "lsattr -d": (0, "---------------C---- /var/lib/pg\n"),
        "findmnt -no": (0, "rw,noatime\n"),
    })
    readings = hostread.read_host_state(
        _expectation(SYSTEMD_DROPIN=str(dropin)),
        sysfs=sysfs, procfs=procfs, exec_=exec_,
    )
    tier1, _, _ = evaluate(readings)
    assert tier1 == []
    # every attribute the module declares is present exactly once
    attrs = [r.attribute for r in readings]
    assert len(attrs) == len(set(attrs))
    assert hostread.ATTR_THP_SHMEM in attrs and hostread.ATTR_TOPOLOGY in attrs
