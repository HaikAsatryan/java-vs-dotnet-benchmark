"""Config loading + validation against the real spec files."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ledgerbench import config
from ledgerbench.config import Mix, PageDistribution


def test_workload_loads(workload):
    assert workload.zipf.alpha == 1.0
    assert workload.rng.algorithm == "PCG64"
    assert workload.rng.target_generator_seed_default == 12345
    assert workload.seeding.seed == 42
    assert workload.seeding.scales["smoke"].customers == 1000
    assert workload.seeding.scales["full"].invoices == 5_000_000


def test_slo_loads(slo):
    assert slo.slo.threshold_ms == 20
    assert slo.slo.error_rate_max == 0.001
    assert slo.equivalence.margin_pct == 5.0
    assert slo.ladder.confirm_reps == 10
    assert slo.ladder.step_factor == 1.2
    assert slo.capacity_gate.headroom_factor == 1.3
    names = [lv.name for lv in slo.capacity_gate.levers_in_order]
    assert names == ["write_share", "cache_hit_rate", "pg_cores"]


def test_lever_from_to_alias(slo):
    write = slo.capacity_gate.levers_in_order[0]
    assert write.from_ == 0.15
    assert write.to == 0.10
    cores = slo.capacity_gate.levers_in_order[2]
    assert cores.taken_from == "vegeta"


def test_mix_must_sum_100():
    with pytest.raises(ValidationError):
        Mix(get_invoice=10, list_invoices=10, pricing_quote=10,
            create_invoice=10, pdf_stub=10, healthz=10)  # sums to 60


def test_page_weights_must_sum_1():
    with pytest.raises(ValidationError):
        PageDistribution(weights={1: 0.5, 2: 0.2})  # 0.7


def test_config_hash_stable_and_changes(tmp_path):
    h1 = config.config_hash()
    h2 = config.config_hash()
    assert h1 == h2 and len(h1) == 64

    # config_hash covers the WHOLE spec tree, not just the two YAMLs. Copy the
    # real spec/ into a temp dir, hash it (must match), then mutate ANY file
    # (here a non-YAML contract) and confirm the hash moves.
    import shutil
    from ledgerbench.util import paths
    spec_copy = tmp_path / "spec"
    shutil.copytree(paths.spec_dir(), spec_copy)
    assert config.config_hash(spec_dir=spec_copy) == h1

    contract = spec_copy / "schema.sql"
    contract.write_text(contract.read_text(encoding="utf-8") + "\n-- edit\n", encoding="utf-8")
    assert config.config_hash(spec_dir=spec_copy) != h1


def test_config_hash_sees_a_new_spec_file(tmp_path):
    """A file added anywhere under spec/ changes the hash (full-tree coverage)."""
    import shutil
    from ledgerbench.util import paths
    spec_copy = tmp_path / "spec"
    shutil.copytree(paths.spec_dir(), spec_copy)
    base = config.config_hash(spec_dir=spec_copy)
    (spec_copy / "postgres" / "extra.conf").write_text("x=1\n", encoding="utf-8")
    assert config.config_hash(spec_dir=spec_copy) != base


# The reverse direction for the WORKLOAD tree, and the reason it exists.
#
# `slo.yaml`'s blocks all forbid extras, so a key with no field is refused at
# load. `workload.yaml`'s do not: `CacheConstruction` is `extra="ignore"`, and
# an OPTIONAL field with a default loads forever against a spec that stopped
# carrying its key -- which is how `cache_construction.asserted_in_manifest:
# true` stood in the hashed spec until 2026-08-24 with a field to bind it, a
# default of `True`, and zero readers anywhere in the tree. It read as a
# pre-registered check and performed none. Same defect as the SLO tree's dead
# `ladder.start_rate_strategy`, one file over and in machine-readable form.
_WORKLOAD_BLOCK_FIELDS = [
    ("cache_construction", lambda w: w.cache_construction),
    ("rng", lambda w: w.rng),
    ("zipf", lambda w: w.zipf),
    ("seeding", lambda w: w.seeding),
]


@pytest.mark.parametrize("block,get", _WORKLOAD_BLOCK_FIELDS,
                         ids=[b for b, _ in _WORKLOAD_BLOCK_FIELDS])
def test_every_field_of_the_workload_tree_has_a_key_in_the_frozen_spec(block, get):
    import yaml

    from ledgerbench.util import paths

    raw = yaml.safe_load(paths.workload_path().read_text(encoding="utf-8"))
    model = get(config.load_workload())
    assert set(type(model).model_fields) == set(raw[block]), (
        f"workload.yaml[{block}] and its model have drifted; a field with no key "
        "is a declaration nothing can honour, and a key with no field is inert"
    )
