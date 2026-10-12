"""`ledgerbench status` rendering + the machine-readable summary line (CLI)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ledgerbench.__main__ import app
from ledgerbench.ledger import REGATE_PHASE, Ledger, ProbeRecord, State
from ledgerbench.registry import DEFAULT_SLOT_SET, get_slot_set
from ledgerbench.util import paths

SLOTS = get_slot_set(DEFAULT_SLOT_SET).slots

runner = CliRunner()


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "results_dir", lambda: tmp_path / "results")
    return tmp_path / "results"


def _running_ledger(run_id: str) -> Ledger:
    led = Ledger.load_or_create(run_id, slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=42)
    led.mark_phase("doctor", State.DONE)
    led.mark_phase("host_setup", State.DONE, skipped="non-linux")
    led.mark_phase("seed", State.DONE, scale="smoke")
    led.mark_phase("capacity_gate", State.DONE, verdict="functional-only (non-linux)")
    led.mark_phase("validation", State.DONE, equivalence={"ok": True})
    cid = "headline.mixed.gc-default"
    led.mark_stage(cid, "ladder", State.DONE)
    s = led.stage(cid, "confirm")
    for block in range(2):
        for slot in SLOTS:
            s.probes.append(
                ProbeRecord(block=block, slot_order=list(SLOTS), slot=slot, rate=400)
            )
    led.mark_probe_done(cid, "confirm", s.probes[0], artifact="a", achieved=1, error_rate=0)
    led.mark_stage(cid, "confirm", State.RUNNING)
    return led


def test_status_renders_and_emits_summary_line(isolated_results):
    _running_ledger("st-1")
    result = runner.invoke(app, ["status", "st-1"])
    assert result.exit_code == 0
    out = result.stdout
    # phase table + per-cell counts + the machine-readable summary line
    assert "phases" in out
    assert "headline.mixed.gc-default" in out
    assert "1/4" in out  # confirm: 1 of 4 probes done
    assert "STATUS run_id=st-1" in out
    assert "state=running" in out
    assert "next=" in out


def test_status_latest_when_no_run_id(isolated_results):
    _running_ledger("st-old")
    led_new = _running_ledger("st-new")
    # make st-new clearly newer
    led_new.created_utc = "2099-01-01T00:00:00.000000Z"
    led_new.save()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "STATUS run_id=st-new" in result.stdout


def test_status_complete_state(isolated_results):
    led = Ledger.load_or_create("st-done", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    for ph in ("doctor", "host_setup", "host_readback", "seed", "capacity_gate",
               "validation", "cells", "variants", "host_restore"):
        led.mark_phase(ph, State.DONE)
    cid = "headline.mixed.gc-default"
    for st in ("ladder", "fit", "confirm", "soak"):
        led.mark_stage(cid, st, State.DONE)
    result = runner.invoke(app, ["status", "st-done"])
    assert result.exit_code == 0
    assert "state=complete" in result.stdout


def test_status_no_ledger_errors(isolated_results):
    result = runner.invoke(app, ["status", "does-not-exist"])
    assert result.exit_code == 1
    assert "no ledger found" in result.stdout


def test_status_shows_the_capacity_regate_and_its_verdict(isolated_results):
    """W26: the re-gate is a phase the operator has to be able to see. It runs
    between cell 1 and cell 2, so a run stopped by it has a completed first cell
    and nothing obviously wrong in the cell table."""
    led = _running_ledger("st-rg")
    cid = "headline.mixed.gc-default"
    for st in ("ladder", "fit", "confirm", "soak"):
        led.mark_stage(cid, st, State.DONE)
    led.mark_phase(REGATE_PHASE, State.DONE,
                   verdict="db-imposed-at-realized-rates",
                   detail="PostgreSQL ran at 0.9000 of its 3-core budget")
    out = runner.invoke(app, ["status", "st-rg"]).stdout
    assert REGATE_PHASE in out
    assert "db-imposed-at-realized-rates" in out.replace("\n", "")


def test_status_names_the_regate_as_next_pending_after_cell_one(isolated_results):
    led = _running_ledger("st-rg2")
    led.mark_phase("host_readback", State.DONE)
    cid = "headline.mixed.gc-default"
    for st in ("ladder", "fit", "confirm", "soak"):
        led.mark_stage(cid, st, State.DONE)
    led.stage("headline.mixed.gc-generous", "ladder")     # a second cell, pending
    led.save()
    out = runner.invoke(app, ["status", "st-rg2"]).stdout
    assert f"next={REGATE_PHASE}" in out
