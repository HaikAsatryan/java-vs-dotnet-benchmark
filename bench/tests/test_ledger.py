"""Resumable run-ledger: schema round-trip + probe-granular resume."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ledgerbench import ledger as ledmod
from ledgerbench.ledger import (
    REGATE_PHASE, Ledger, LedgerSchemaError, ProbeRecord, State,
)
from ledgerbench.registry import DEFAULT_SLOT_SET, get_slot_set
from ledgerbench.util import paths

# Slot ids come from the registry, never from a literal: these fixtures follow
# the default slot set without an edit.
SLOTS = get_slot_set(DEFAULT_SLOT_SET).slots
SLOT_A, SLOT_B = SLOTS


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    """Point results_dir at a tmp dir so tests never touch the repo's results/."""
    monkeypatch.setattr(paths, "results_dir", lambda: tmp_path / "results")
    return tmp_path / "results"


def test_create_save_load_roundtrip(isolated_results):
    led = Ledger.load_or_create("run-1", slot_set=DEFAULT_SLOT_SET, config_hash="abc", rng_seed=42)
    led.mark_phase("doctor", State.DONE, artifact="doctor.json")
    again = Ledger.load("run-1")
    assert again is not None
    assert again.run_id == "run-1"
    assert again.config_hash == "abc"
    assert again.phase_done("doctor")
    assert again.phases["doctor"].data["artifact"] == "doctor.json"


def test_config_hash_mismatch_refuses_resume(isolated_results):
    Ledger.load_or_create("run-2", slot_set=DEFAULT_SLOT_SET, config_hash="hash-A", rng_seed=42)
    with pytest.raises(RuntimeError, match="config_hash changed"):
        Ledger.load_or_create("run-2", slot_set=DEFAULT_SLOT_SET, config_hash="hash-B", rng_seed=42)


def test_probe_granular_resume(isolated_results):
    led = Ledger.load_or_create("run-3", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=7)
    cell_id = "headline.mixed.gc-default"
    stage = led.stage(cell_id, "confirm")
    for block in range(3):
        for slot in SLOTS:
            stage.probes.append(
                ProbeRecord(block=block, slot_order=list(SLOTS), slot=slot, rate=8120)
            )
    # mark the first two probes done
    led.mark_probe_done(
        cell_id, "confirm", stage.probes[0], artifact=f"b0.{SLOTS[0]}.bin", achieved=8118.0, error_rate=0.0
    )
    led.mark_probe_done(
        cell_id, "confirm", stage.probes[1], artifact=f"b0.{SLOTS[1]}.bin", achieved=8121.0, error_rate=0.0
    )
    # reload and confirm only the remaining 4 are pending
    reloaded = Ledger.load("run-3")
    pending = reloaded.pending_probes(cell_id, "confirm")
    assert len(pending) == 4
    assert all(p.state != State.DONE for p in pending)
    done = [p for p in reloaded.stage(cell_id, "confirm").probes if p.state == State.DONE]
    assert {p.artifact for p in done} == {f"b0.{SLOTS[0]}.bin", f"b0.{SLOTS[1]}.bin"}


def test_atomic_write_leaves_no_tmp(isolated_results):
    led = Ledger.load_or_create("run-4", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    led.save()
    run_path = paths.run_dir("run-4")
    leftovers = list(run_path.glob(".ledger.json.*"))
    assert leftovers == []
    assert led.path().exists()


def test_schema_version_present(isolated_results):
    led = Ledger.load_or_create("run-5", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    assert led.schema_version == ledmod.LEDGER_SCHEMA_VERSION


def test_latest_and_latest_unfinished(isolated_results):
    old = Ledger.load_or_create("r-old", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    old.created_utc = "2026-01-01T00:00:00.000000Z"
    old.mark_phase("doctor", State.DONE)
    old.save()
    new = Ledger.load_or_create("r-new", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    new.created_utc = "2026-12-31T00:00:00.000000Z"
    new.save()
    assert Ledger.latest().run_id == "r-new"
    # neither is complete (no host_restore) -> latest_unfinished is the newest
    assert Ledger.latest_unfinished().run_id == "r-new"


def test_is_complete_requires_all_phases_and_stages(isolated_results):
    led = Ledger.load_or_create("r-c", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    assert not led.is_complete()  # empty
    for ph in ("doctor", "host_setup", "host_readback", "seed", "capacity_gate",
               "validation", "cells", "variants", "host_restore"):
        led.mark_phase(ph, State.DONE)
    cid = "headline.mixed.gc-default"
    led.mark_stage(cid, "ladder", State.DONE)
    led.mark_stage(cid, "fit", State.DONE)
    led.mark_stage(cid, "confirm", State.DONE)
    assert not led.is_complete()  # soak still pending
    led.mark_stage(cid, "soak", State.DONE)
    assert led.is_complete()


def test_next_pending_walks_phase_order(isolated_results):
    led = Ledger.load_or_create("r-np", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    assert led.next_pending() == "doctor"
    led.mark_phase("doctor", State.DONE)
    led.mark_phase("host_setup", State.DONE)
    # host_readback sits between the two: the tuning is applied, then read back.
    assert led.next_pending() == "host_readback"
    led.mark_phase("host_readback", State.DONE)
    assert led.next_pending() == "seed"


def test_next_pending_walks_the_regate_between_cell_one_and_cell_two(isolated_results):
    """The capacity re-gate runs INSIDE the cells loop, so the walker has to too.

    Reported only after the "cells" marker it would surface exactly never: a
    failing re-gate is what stops the later cells from running at all, so their
    stages would always be the earlier pending unit and the phase the operator
    has to act on would never be named.
    """
    led = Ledger.load_or_create("r-rg", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    for name in ("doctor", "host_setup", "host_readback", "seed",
                 "capacity_gate", "validation"):
        led.mark_phase(name, State.DONE)
    for cid in ("cell.one", "cell.two"):
        led.stage(cid, "ladder")                    # registers the cell, PENDING
    assert led.next_pending() == "cell.one/ladder"
    for st in ("ladder", "fit", "confirm", "soak"):
        led.mark_stage("cell.one", st, State.DONE)
    # cell 1 is done and cell 2 has not started: the re-gate is what runs next
    assert led.next_pending() == REGATE_PHASE
    led.mark_phase(REGATE_PHASE, State.DONE)
    assert led.next_pending() == "cell.two/ladder"


def test_next_pending_names_the_regate_on_a_single_cell_run(isolated_results):
    """One cell never reaches the in-loop check, and an unrecorded phase in
    PHASE_ORDER would otherwise pin `next_pending` at "complete" forever.

    The state constructed here -- `cells` DONE, re-gate not DONE -- is a RESUME
    of a ledger written before the re-gate existed. A fresh single-cell run
    cannot produce it: `run_cells_phase` calls the re-gate before marking cells
    DONE, so a raised re-gate leaves the walker returning "cells".
    """
    led = Ledger.load_or_create("r-rg1", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1)
    for name in ("doctor", "host_setup", "host_readback", "seed",
                 "capacity_gate", "validation"):
        led.mark_phase(name, State.DONE)
    led.stage("cell.only", "ladder")
    for st in ("ladder", "fit", "confirm", "soak"):
        led.mark_stage("cell.only", st, State.DONE)
    led.mark_phase("cells", State.DONE)
    assert led.next_pending() == REGATE_PHASE
    led.mark_phase(REGATE_PHASE, State.DONE)
    assert led.next_pending() == "variants"


# --------------------------------------------------------------------------- #
# W5: the schema is registry-validated, versioned, and fails LOUDLY
# --------------------------------------------------------------------------- #
def test_probe_slot_must_be_registered():
    """An unregistered slot id is refused by name, never silently accepted.

    Round 1's `lang: Literal["go","dotnet"]` made this a type error at the model
    and a SILENT wrong-semantics dispatch everywhere downstream
    (`warmup.py`: `if lang == "dotnet": ... else: <Go's collector model>`).
    """
    with pytest.raises(ValidationError) as exc:
        ProbeRecord(block=0, slot_order=[SLOT_A, "rust"], slot="rust", rate=100)
    assert "unregistered slot 'rust'" in str(exc.value)
    assert "registry.py" in str(exc.value)


def test_probe_slot_order_does_not_default():
    """No slot_order means no record of the RCB order the probe ran in.

    It used to default to `[]`, and `_execute_block_probes` then fell back to a
    hard-coded ("go","dotnet") literal -- so the probe still ran, in an order
    the artifact could not justify.
    """
    with pytest.raises(ValidationError):
        ProbeRecord(block=0, slot=SLOT_A, rate=100)


def test_probe_slot_must_be_inside_its_own_block_order():
    with pytest.raises(ValidationError, match="not in its block order"):
        ProbeRecord(block=0, slot_order=[SLOT_B], slot=SLOT_A, rate=100)


def test_ledger_slot_set_must_be_registered(isolated_results):
    with pytest.raises(ValidationError, match="unknown slot set"):
        Ledger(run_id="r", config_hash="c", rng_seed=1, slot_set="nonesuch")


def test_ledger_slots_come_from_the_pinned_set(isolated_results):
    led = Ledger.load_or_create(
        "r-slots", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1
    )
    assert led.slots == SLOTS


def test_slot_set_mismatch_refuses_resume(isolated_results):
    """Resuming under a different LEDGERBENCH_SLOT_SET would blend comparisons."""
    Ledger.load_or_create("r-ss", slot_set="shakedown", config_hash="c", rng_seed=1)
    with pytest.raises(RuntimeError, match="slot set changed"):
        Ledger.load_or_create("r-ss", slot_set="headline", config_hash="c", rng_seed=1)


def test_a_previous_schema_version_is_refused_by_name(isolated_results):
    """v1 ledgers do not silently vanish, and are not overwritten either."""
    led = Ledger.load_or_create(
        "r-v1", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1
    )
    doc = json.loads(led.path().read_text(encoding="utf-8"))
    doc["schema_version"] = 1
    led.path().write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(LedgerSchemaError) as exc:
        Ledger.load("r-v1")
    assert "schema v1" in str(exc.value)
    assert f"v{ledmod.LEDGER_SCHEMA_VERSION}" in str(exc.value)
    # and it is NOT clobbered by a fresh ledger: load_or_create propagates.
    with pytest.raises(LedgerSchemaError):
        Ledger.load_or_create(
            "r-v1", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1
        )
    assert json.loads(led.path().read_text(encoding="utf-8"))["schema_version"] == 1


def test_the_immediately_previous_schema_version_is_refused_by_name(isolated_results):
    """v2 is the dangerous one, and only the version check catches it.

    v3 added `ProbeRecord.gates_evaluated`/`gates_failed`, both defaulting to
    []. A v2 document therefore VALIDATES into the v3 model without complaint,
    and every probe in it then reads as "no gate was ever evaluated" -- which is
    indistinguishable from a probe whose gates all passed. Nothing in the shape
    can tell them apart, so the refusal has to come from the version.
    """
    led = Ledger.load_or_create(
        "r-v2", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1
    )
    led.cells.append(ledmod.CellRecord(cell_id="headline.mixed.gc-default"))
    led.stage("headline.mixed.gc-default", "ladder").probes.append(
        ProbeRecord(block=0, slot_order=list(SLOTS), slot=SLOT_A, rate=100)
    )
    led.save()
    doc = json.loads(led.path().read_text(encoding="utf-8"))
    doc["schema_version"] = 2
    for cell in doc["cells"]:
        for probe in cell["ladder"]["probes"]:
            probe.pop("gates_evaluated")
            probe.pop("gates_failed")
    led.path().write_text(json.dumps(doc), encoding="utf-8")
    # the v2 shape really does validate: only the version stops it
    assert ledmod.Ledger.model_validate(doc).cells[0].ladder.probes[0].gates_failed == []

    with pytest.raises(LedgerSchemaError) as exc:
        Ledger.load("r-v2")
    assert "schema v2" in str(exc.value)
    assert f"v{ledmod.LEDGER_SCHEMA_VERSION}" in str(exc.value)


def test_an_unreadable_ledger_is_reported_not_skipped(isolated_results, capsys):
    """The defect this closes: a schema problem read as 'nothing to resume'.

    `latest_unfinished()` swallowed every exception, so `ledgerbench run-all
    --resume` printed "no unfinished run to resume" for a ledger that existed
    and had pending work.
    """
    good = Ledger.load_or_create(
        "r-good", slot_set=DEFAULT_SLOT_SET, config_hash="c", rng_seed=1
    )
    good.created_utc = "2026-01-01T00:00:00.000000Z"
    good.save()
    bad_dir = isolated_results / "r-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "ledger.json").write_text("{not json", encoding="utf-8")

    assert Ledger.latest_unfinished().run_id == "r-good"
    err = capsys.readouterr().err
    assert "r-bad" in err and "unreadable ledger" in err


def test_probe_key_is_one_definition_shared_by_writer_and_reader():
    key = ledmod.probe_key(8120, 3, SLOT_A)
    assert key == f"r8120.b3.{SLOT_A}"
    assert ledmod.split_probe_key(key) == (8120, 3, SLOT_A)
    assert ledmod.split_probe_key("nonsense") is None


def test_no_slot_id_can_break_the_probe_key_format():
    """Slot ids are path- and key-safe by construction (registry.Slot)."""
    from ledgerbench.registry import SLOTS as REGISTERED

    for sid in REGISTERED:
        assert ledmod.split_probe_key(ledmod.probe_key(1, 0, sid)) == (1, 0, sid)
