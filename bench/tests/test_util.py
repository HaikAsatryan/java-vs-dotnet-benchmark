"""Utilities: RFC3339 micros, atomic write, subprocess timeouts, doctor checks."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from ledgerbench import doctor, registry
from ledgerbench.util import paths
from ledgerbench.util.clock import rfc3339_micros
from ledgerbench.util.proc import TIMEOUT_RETURNCODE, run

RFC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def test_rfc3339_exactly_six_fractional_digits():
    s = rfc3339_micros(datetime(2026, 6, 6, 1, 2, 3, 4, tzinfo=timezone.utc))
    assert RFC.match(s), s
    assert s == "2026-06-06T01:02:03.000004Z"


def test_rfc3339_now_format():
    assert RFC.match(rfc3339_micros())


def test_write_text_atomic_no_leftover(tmp_path):
    p = tmp_path / "sub" / "x.json"
    paths.write_text_atomic(p, '{"a":1}')
    assert p.read_text() == '{"a":1}'
    assert list(tmp_path.glob("sub/.x.json.*")) == []


def test_repo_root_resolves():
    root = paths.repo_root()
    assert (root / "spec" / "workload.yaml").exists()
    assert (root / "bench").is_dir()


def test_doctor_spec_parses_ok():
    report = doctor.run_doctor(require_docker=False)
    by_name = {c.name: c for c in report.checks}
    assert by_name["spec_parses"].ok


def test_doctor_requires_the_full_cpuset_width(monkeypatch):
    """The documented cpusets reach id 15, so 16 logical cores are required.

    The old threshold was 8 with a comment claiming 13: both are the SUM of the
    assigned cores, and a box with 13 cores cannot start a container pinned to
    cpuset 15 no matter how the sum works out.
    """
    monkeypatch.setattr(doctor, "IS_WINDOWS", False)
    monkeypatch.setattr(os, "cpu_count", lambda: 13)
    assert doctor._check_free_cores().ok is False
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert doctor._check_free_cores().ok is True


def test_doctor_checks_host_ram_against_the_resource_ledger(tmp_path, monkeypatch):
    """-XX:MaxRAMPercentage reads the CGROUP limit, not host free memory.

    A box with less RAM than the ledger's mem_limits sum to oversubscribes
    silently: every SUT sizes its heap off a ceiling the host cannot honour.
    """
    monkeypatch.setattr(doctor, "IS_LINUX", True)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16000000 kB\nMemFree: 100 kB\n")
    assert doctor._check_host_ram(meminfo).ok is False
    meminfo.write_text("MemTotal:       65000000 kB\n")
    assert doctor._check_host_ram(meminfo).ok is True
    # a meminfo with no MemTotal is NOT a pass
    meminfo.write_text("MemFree: 100 kB\n")
    assert doctor._check_host_ram(meminfo).ok is False
    assert doctor.parse_mem_total_kb("MemTotal:  65000000 kB\n") == 65000000


def test_every_implemented_slot_has_a_build_context(monkeypatch):
    """`docker compose config` exits 0 on a missing build context.

    That is how `services/go` stayed referenced after deletion, and the failure
    surfaced as a build error ~30 min into a run. The `implemented` flag is
    load-bearing: a slot can be declared before its build context exists, so
    checking every slot regardless would fail the check on a healthy tree.
    """
    assert doctor._check_build_contexts().ok is True
    dangling = replace(
        registry.SLOTS[registry.runnable_slots()[0]],
        build_context="services/does-not-exist",
    )
    monkeypatch.setitem(registry.SLOTS, dangling.slot_id, dangling)
    check = doctor._check_build_contexts()
    assert check.ok is False and "services/does-not-exist" in check.detail


# --------------------------------------------------------------------------- #
# subprocess timeouts are a RESULT, not an exception
#
# Every live call site passes `check=False, timeout=N` because it wants to
# handle the failure. Before this, subprocess.TimeoutExpired propagated
# regardless of `check` and a wedged `docker stop` aborted the whole run
# instead of failing one probe.
# --------------------------------------------------------------------------- #
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(5)"]


def test_timeout_returns_a_failed_result_instead_of_raising():
    res = run(_SLEEPER, timeout=0.2)
    assert res.ok is False
    assert res.timed_out is True
    assert res.returncode == TIMEOUT_RETURNCODE
    assert "timed out after 0.2s" in res.stderr


def test_timeout_still_raises_under_check():
    with pytest.raises(RuntimeError, match="timed out after"):
        run(_SLEEPER, check=True, timeout=0.2)


def test_a_prompt_command_is_not_reported_as_timed_out():
    res = run([sys.executable, "-c", "print('hi')"], timeout=30)
    assert res.ok is True
    assert res.timed_out is False
    assert res.stdout.strip() == "hi"


def test_doctor_survives_a_timing_out_probe(monkeypatch):
    """A wedged daemon must make doctor report not-ok, not traceback out."""
    monkeypatch.setattr(
        doctor, "run", lambda *a, **k: run(_SLEEPER, timeout=0.2), raising=True
    )
    report = doctor.run_doctor(require_docker=False)
    assert report.all_ok is False
