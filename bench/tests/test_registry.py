"""Slot registry: cross-checked against infra/compose.yaml, never self-asserted.

A test that restates the registry's own literals proves nothing. These read
`infra/compose.yaml` and the tree, so the registry cannot drift from the topology
it claims to describe -- which is the failure mode the registry exists to close.

The P2 assertion is the load-bearing one: it proves that .NET can occupy BOTH
slots with disjoint services, ports, volumes and container names, which is what
the shakedown run needs.
"""

from __future__ import annotations

import pytest
import yaml

from ledgerbench import registry
from ledgerbench.util import paths


@pytest.fixture(scope="module")
def compose_doc() -> dict:
    return yaml.safe_load(
        (paths.infra_dir() / "compose.yaml").read_text(encoding="utf-8")
    )


def _sut_services(doc: dict) -> dict[str, dict]:
    """Compose services carrying a SUT profile (not the load/seed helpers)."""
    helpers = {"load", "seed"}
    return {
        name: svc
        for name, svc in doc.get("services", {}).items()
        if set(svc.get("profiles") or []) - helpers and svc.get("profiles")
    }


# --------------------------------------------------------------------------- #
# registry <-> compose topology
# --------------------------------------------------------------------------- #
def test_every_compose_sut_service_has_a_registry_slot(compose_doc):
    """A SUT compose can start but the harness cannot name is unrunnable."""
    registered = {s.service for s in registry.SLOTS.values()}
    assert set(_sut_services(compose_doc)) <= registered


def test_implemented_slots_match_their_compose_service(compose_doc):
    services = compose_doc["services"]
    for slot in registry.SLOTS.values():
        if not slot.implemented:
            continue
        assert slot.service in services, f"{slot.slot_id}: no compose service"
        svc = services[slot.service]
        assert slot.profile in (svc.get("profiles") or []), slot.slot_id
        # published port
        assert any(
            str(p).startswith(f"{slot.host_port}:") for p in (svc.get("ports") or [])
        ), f"{slot.slot_id}: {slot.host_port} not published by {slot.service}"
        # pdf volume
        assert any(
            str(v).startswith(f"{slot.pdf_volume}:") for v in (svc.get("volumes") or [])
        ), f"{slot.slot_id}: {slot.pdf_volume} not mounted by {slot.service}"
        assert slot.pdf_volume in (compose_doc.get("volumes") or {}), slot.slot_id


def test_implemented_slots_have_a_build_context_that_exists():
    """The `services/go` breakage as an assertion.

    A compose service pointing at a missing build context fails 30 minutes into
    a run, and `docker compose config` exits 0 on it, so nothing else catches
    this class.
    """
    root = paths.repo_root()
    for slot in registry.SLOTS.values():
        if slot.implemented:
            assert (root / slot.build_context).is_dir(), (
                f"{slot.slot_id}: build context {slot.build_context} missing"
            )


def test_unimplemented_slots_are_absent_from_compose(compose_doc):
    """Declared-but-not-built slots must not dangle in the topology.

    A slot is declared in the registry before its build context exists. A
    compose service for a slot that is not implemented would point at a missing
    path, the dangling-path breakage W2 repaired.
    """
    services = compose_doc["services"]
    for slot in registry.SLOTS.values():
        if not slot.implemented:
            assert slot.service not in services, (
                f"{slot.slot_id} is not implemented but {slot.service} is in compose"
            )


# --------------------------------------------------------------------------- #
# P2: two slots of ONE language must not collide
# --------------------------------------------------------------------------- #
def test_shakedown_slots_are_disjoint_everywhere_it_matters():
    """The P2 shakedown pair is runnable: one language, no collisions.

    Under a language-keyed registry these two would share a service, a port, a
    volume, a container name and -- worst -- artifact filenames, where they
    overwrite each other silently and leave a run that looks valid.
    """
    a, b = (registry.get_slot(s) for s in registry.get_slot_set("shakedown").slots)
    assert a.language is b.language          # the point: same runtime
    assert a.slot_id != b.slot_id
    assert a.service != b.service
    assert a.host_port != b.host_port
    assert a.pdf_volume != b.pdf_volume
    assert a.container_name() != b.container_name()
    assert a.url_env != b.url_env
    # Each slot drives its OWN compose variable: a shared DATA_LAYER would mean
    # configuring slot B silently reconfigured slot A.
    assert set(a.slot_env) != set(b.slot_env)
    assert a.expected_data_layer != b.expected_data_layer


def test_no_two_slots_share_a_port_volume_or_container_name():
    for attr in ("host_port", "pdf_volume", "url_env"):
        seen: dict[object, str] = {}
        for slot in registry.SLOTS.values():
            value = getattr(slot, attr)
            # dotnet and dotnet-efcore are the same physical SUT under two names
            # (headline vs shakedown), so they legitimately share a service.
            if value in seen and {seen[value], slot.slot_id} != {"dotnet", "dotnet-efcore"}:
                pytest.fail(f"{attr}={value!r} shared by {seen[value]} and {slot.slot_id}")
            seen[value] = slot.slot_id


def test_container_name_honours_a_non_default_project_name():
    """A hard-coded "ledgerline-" prefix resolves NO cgroup under `-p other`,
    which would void every resource number in the headline."""
    slot = registry.get_slot("dotnet")
    assert slot.container_name("ledgerline") == "ledgerline-sut-dotnet-1"
    assert slot.container_name("other") == "other-sut-dotnet-1"


# --------------------------------------------------------------------------- #
# lookups raise; they never default
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fn", [registry.get_slot, registry.gc_semantics, registry.knob_env_for,
           registry.int_knobs_for],
)
def test_every_lookup_raises_on_an_unregistered_slot(fn):
    """Round 1's `if lang == "dotnet": ... else: <Go semantics>` meant any
    unknown id silently inherited Go's single-generation GC model and still
    reported `passed`."""
    with pytest.raises(KeyError, match="unregistered slot"):
        fn("rust")


def test_gc_semantics_differ_per_runtime_and_are_not_a_rename():
    java = registry.gc_semantics("java")
    dotnet = registry.gc_semantics("dotnet")
    assert java is not dotnet
    assert java.model != dotnet.model
    # the ZGC Cycles-vs-Pauses trap has to survive into the frozen spec
    assert "Pauses" in java.collections_note


# --------------------------------------------------------------------------- #
# ratio direction — PROGRESS declares Java:.NET; round-1 code computed .NET:Go
# --------------------------------------------------------------------------- #
def test_headline_ratio_is_java_over_dotnet_not_the_reciprocal():
    hs = registry.get_slot_set("headline")
    assert hs.reference == "dotnet"
    assert hs.subject == "java"
    assert hs.ratio_label() == "Java : .NET"


def test_shakedown_reference_is_the_no_orm_floor():
    """EF Core is the thing being priced; raw ADO.NET is what it is priced against."""
    assert registry.get_slot_set("shakedown").subject == "dotnet-efcore"
    assert registry.get_slot_set("shakedown").reference == "dotnet-ado"


def test_a_slot_set_cannot_reference_a_slot_it_does_not_contain():
    with pytest.raises(KeyError, match="not one of its slots"):
        registry.SlotSet(name="bad", slots=("dotnet",), reference="java")


def test_a_slot_set_cannot_name_an_unregistered_slot():
    with pytest.raises(KeyError, match="unregistered slot"):
        registry.SlotSet(name="bad", slots=("dotnet", "rust"), reference="dotnet")


# --------------------------------------------------------------------------- #
# knob surface
# --------------------------------------------------------------------------- #
def test_language_only_knobs_never_leak_onto_the_other_language():
    java = registry.knob_env_for("java")
    dotnet = registry.knob_env_for("dotnet")
    assert "DATA_LAYER" in dotnet and "DATA_LAYER" not in java
    assert "JVM_GC_FLAGS" in java and "JVM_GC_FLAGS" not in dotnet
    # shared knobs are on both
    assert set(registry.SHARED_KNOB_ENV) <= set(java) & set(dotnet)


def test_db_conn_lifetime_is_a_recorded_knob():
    """spec/db-access.md declares the lever and compose sets it, but round 1's
    knob map omitted it, so manifest.Knobs.db_conn_lifetime_seconds was ALWAYS
    null and an ambient export reached both SUTs unrecorded."""
    assert registry.SHARED_KNOB_ENV["DB_CONN_LIFETIME"] == "db_conn_lifetime_seconds"
    assert "DB_CONN_LIFETIME" in registry.all_knob_env_vars()


def test_the_scrub_set_covers_every_parallelism_carrier():
    """JDK_JAVA_OPTIONS is the single highest-value scrub entry on a Java run:
    an ambient export there silently re-tunes the JVM under measurement."""
    scrub = registry.all_knob_env_vars()
    assert "JDK_JAVA_OPTIONS" in scrub
    assert "DOTNET_PROCESSOR_COUNT" in scrub


def test_parallelism_env_models_a_flag_not_just_a_scalar():
    assert registry.JAVA.parallelism_env(6) == {
        "JDK_JAVA_OPTIONS": "-XX:ActiveProcessorCount=6"}
    assert registry.DOTNET.parallelism_env(6) == {"DOTNET_PROCESSOR_COUNT": "6"}


def test_int_knobs_are_declared_per_language():
    assert "max_auto_prepare" in registry.int_knobs_for("dotnet")
    assert "max_auto_prepare" not in registry.int_knobs_for("java")
    assert "max_ram_percent" in registry.int_knobs_for("java")


# --------------------------------------------------------------------------- #
# the scrub set, derived and cross-checked against compose
# --------------------------------------------------------------------------- #
# Compose variables the runner deliberately does NOT scrub, each with its reason.
# Every entry is either infrastructure wiring (identical for every SUT, not a
# comparison lever) or an operator-level topology knob the runner reads itself
# and the manifest discloses.
NOT_SCRUBBED = {
    "PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASSWORD", "REDIS_ADDR",
    "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "PDF_DIR", "SERVICE_VERSION", "PORT",
    "SUT_CPUS", "SUT_CORES", "SUT_MEM",
    "PG_CPUS", "PG_MEM", "VEGETA_CPUS", "VEGETA_MEM", "REDIS_CPUS", "REDIS_MEM",
}


def _compose_interpolations() -> set[str]:
    import re

    names: set[str] = set()
    for name in ("compose.yaml", "compose.bench.yaml"):
        text = (paths.infra_dir() / name).read_text(encoding="utf-8")
        names |= set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", text))
    return names


def test_the_scrub_set_covers_every_compose_interpolation():
    """An unscrubbed ${VAR} is an ambient export that silently re-tunes a SUT.

    compose interpolates ${VAR:-default} from the process environment, so a
    stray export in the operator's shell overrides the pinned baseline for BOTH
    SUTs while the manifest records the intended value. This is why
    KNOB_ENV_VARS is DERIVED from the registry: a hand-maintained list drifts,
    and the drift is invisible in the artifacts.
    """
    from ledgerbench import compose as compose_mod

    interpolated = _compose_interpolations()
    scrubbed = set(compose_mod.KNOB_ENV_VARS)
    unaccounted = interpolated - scrubbed - NOT_SCRUBBED
    assert not unaccounted, (
        f"compose interpolates {sorted(unaccounted)} but nothing scrubs them and "
        "they carry no documented exemption"
    )
    assert not (scrubbed & NOT_SCRUBBED)


def test_every_slot_env_var_is_scrubbed():
    """A slot's own env must be forced, or an ambient value wins over the pin."""
    scrub = registry.all_knob_env_vars()
    for slot in registry.SLOTS.values():
        assert set(slot.slot_env) <= scrub, slot.slot_id


def test_the_second_dotnet_slot_uses_its_own_compose_variable(compose_doc):
    """Setting DATA_LAYER for slot B would reconfigure slot A instead."""
    ado = registry.get_slot("dotnet-ado")
    assert "DATA_LAYER_B" in ado.slot_env and "DATA_LAYER" not in ado.slot_env
    raw = compose_doc["services"][ado.service]["environment"]["DATA_LAYER"]
    assert "DATA_LAYER_B" in raw


# --------------------------------------------------------------------------- #
# tripwires
# --------------------------------------------------------------------------- #
def test_default_slot_set_flips_when_java_lands():
    """The default pair is the headline pair once the java slot is implemented;
    without it the shakedown pair is the only pair that can actually run."""
    if registry.SLOTS["java"].implemented:
        assert registry.DEFAULT_SLOT_SET == "headline"
    else:
        assert registry.DEFAULT_SLOT_SET == "shakedown"


def test_implemented_mirrors_the_tree():
    """`implemented` is a hand-set flag, so this pins it to DISK truth both
    ways: a slot flagged implemented must have its build context in the tree,
    and a build context in the tree must not sit behind implemented=False.
    (The original tripwire keyed the default-pair flip off the FLAG alone, so
    services/java/ landed at P4 and nothing fired -- the headline pair stayed
    unrunnable behind implemented=False with every suite green.)"""
    from ledgerbench.util import paths as _paths

    root = _paths.repo_root()
    for sid, slot in registry.SLOTS.items():
        assert slot.implemented == (root / slot.build_context).is_dir(), (
            f"slot {sid!r}: implemented={slot.implemented} but build context "
            f"{slot.build_context!r} {'exists' if (root / slot.build_context).is_dir() else 'is missing'}"
        )


def test_active_slot_set_refuses_an_unbuildable_pair(monkeypatch):
    monkeypatch.setenv(registry.SLOT_SET_ENV, "headline")
    if registry.SLOTS["java"].implemented:
        assert registry.active_slot_set().name == "headline"
    else:
        with pytest.raises(RuntimeError, match="build context does not exist"):
            registry.active_slot_set()


def test_active_slot_set_defaults_to_a_runnable_pair(monkeypatch):
    monkeypatch.delenv(registry.SLOT_SET_ENV, raising=False)
    assert registry.active_slot_set().name == registry.DEFAULT_SLOT_SET


def test_every_variant_cell_actually_differs_from_the_baseline():
    """A variant whose env equals the compose default measures nothing.

    D4 made MAX_AUTO_PREPARE=20 the baseline, which turned the old
    `variant.prepared-parity` (also 20) into a silent no-op: it would have run
    for hours and produced a second copy of the headline cell.
    """
    import re

    from ledgerbench import phases
    from ledgerbench.util import paths

    compose_text = (paths.infra_dir() / "compose.yaml").read_text(encoding="utf-8")
    defaults = dict(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}", compose_text))
    for cell in phases.CELLS:
        for var, value in cell.extra_env.items():
            if var in defaults:
                assert value != defaults[var], (
                    f"{cell.cell_id} sets {var}={value}, which is already the "
                    f"compose default -- the cell would measure the baseline twice"
                )


# --------------------------------------------------------------------------- #
# W12: the anti-sed guard
# --------------------------------------------------------------------------- #
# Modules allowed to contain a bare slot id or a round-1 language literal, with
# the reason. Everything else must resolve identity through the registry.
_LITERAL_EXEMPT = {
    "registry.py": "the registry IS the declaration",
}

# Literals that would make a module reason about a stack by name. Slot ids are
# added from the registry itself, so a new slot is covered the day it lands.
_ROUND_ONE_LITERALS = frozenset({
    "go", "Go", "sut-go", "golang", "sqlc", "pgx",
    "GOGC", "GOMEMLIMIT", "GOMAXPROCS",
})


def _string_constants(path):
    """Every bare `str` constant in a module, EXCLUDING docstrings.

    Docstrings and comments are prose about the code and routinely have to name
    what was removed and why ("round 1 dispatched `if lang == \"dotnet\"`"); the
    defect this test guards is a literal the code BRANCHES ON or BUILDS A PATH
    FROM. So the walk skips the module/class/function docstring nodes and keeps
    every other string constant.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_language_or_slot_literal_outside_the_registry():
    """Slot and language identity is looked up in the registry, never spelled.

    `s/go/java/` over this package produces a harness that runs with the wrong
    model: `gc_steady_go` encoded Go's single-generation collector, and its call
    site dispatched `if lang == "dotnet": ... else: <Go semantics>`, so any
    other string silently inherited the wrong model AND still reported `passed`.
    This test keeps identity looked up for every module added later.
    """
    from ledgerbench.util import paths

    banned = _ROUND_ONE_LITERALS | set(registry.SLOTS) | {
        s.service for s in registry.SLOTS.values()
    } | {s.profile for s in registry.SLOTS.values()}

    offences: list[str] = []
    pkg = paths.repo_root() / "bench" / "ledgerbench"
    for path in sorted(pkg.rglob("*.py")):
        if path.name in _LITERAL_EXEMPT:
            continue
        for value in _string_constants(path):
            if value in banned:
                offences.append(f"{path.relative_to(pkg)}: {value!r}")
    assert not offences, (
        "slot/language identity must come from ledgerbench.registry, never a "
        "literal:\n  " + "\n  ".join(offences)
    )


def test_the_guard_would_actually_catch_a_reintroduced_literal(tmp_path):
    """The guard is only worth having if it fires. Prove it on a planted literal."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        '"""A docstring naming sut-go must NOT trip the guard."""\n'
        'SERVICE = "sut-dotnet"\n',
        encoding="utf-8",
    )
    values = _string_constants(planted)
    assert "sut-dotnet" in values
    assert not any("docstring" in v for v in values)


# --------------------------------------------------------------------------- #
# by-construction unsatisfiability (W22/B3)
# --------------------------------------------------------------------------- #
def test_no_runtime_declares_an_unsatisfiable_gate_yet():
    """The table is EMPTY, and that is the claim -- not an unfinished chore.

    R1-A3's only instance was Go's `GOGC=off`, a language this round does not
    run, and no round-2 posture has been MEASURED to be unable to satisfy a
    gate. An entry here refuses a cell outright, so declaring one on suspicion
    would stop the headline cell on an unmeasured belief. An entry needs a
    measurement.
    """
    assert {
        lang: spec.unsatisfiable_gates for lang, spec in registry.LANGUAGES.items()
    } == {lang: () for lang in registry.LANGUAGES}


def test_unsatisfiable_gates_for_matches_only_the_declaring_slot_and_value(monkeypatch):
    """The predicate keys on (slot, env var, value), case-insensitively on value.

    Synthetic entry: the real table is empty (above), so the machinery stays
    covered either way. The other slot must NOT inherit it -- round 1's
    `if lang == "dotnet": ... else: <Go>` dispatch is the defect class this
    registry exists to remove, and a predicate that matched every slot would
    reintroduce it as a refusal that stops the wrong cell.
    """
    import dataclasses

    subject, other = registry.SLOT_SETS["shakedown"].subject, registry.SLOT_SETS["shakedown"].reference
    entry = registry.UnsatisfiableGate(
        gate="gc_steady", env_var="SYNTHETIC_KNOB", value="off",
        why="SYNTHETIC TEST ENTRY: collection is disabled outright",
    )
    slot = registry.SLOTS[subject]
    monkeypatch.setitem(registry.SLOTS, subject, dataclasses.replace(
        slot, language=dataclasses.replace(slot.language, unsatisfiable_gates=(entry,)),
    ))

    assert registry.unsatisfiable_gates_for(subject, {"SYNTHETIC_KNOB": "OFF"}) == {
        "gc_steady": entry
    }
    assert registry.unsatisfiable_gates_for(subject, {"SYNTHETIC_KNOB": "on"}) == {}
    assert registry.unsatisfiable_gates_for(subject, {}) == {}
    assert registry.unsatisfiable_gates_for(other, {"SYNTHETIC_KNOB": "off"}) == {}


def test_the_jvm_cadence_beans_name_only_the_beans_that_count_collections():
    """The cadence source W27 declares, and the two exclusions it turns on.

    Measured on Temurin 25.0.4 (spec/runtime-stats.md): under ZGC `*Cycles` and
    `*Pauses` are separate beans over the same pools -- 1,029 minor cycles
    against 3,094 minor pauses -- and under G1 `G1 Concurrent GC` counts the
    Remark and Cleanup STW sub-pauses of a mark cycle, 86 events against exactly
    43 + 43 in `-Xlog:gc`. Summing every bean present would multiply the JVM's
    collection cadence by three to four; a list that quietly readmits one of
    them would do it silently, which is what this pins.
    """
    java = registry.gc_semantics("java")
    assert java.cadence_beans == (
        "G1 Young Generation", "G1 Old Generation",
        "ZGC Minor Cycles", "ZGC Major Cycles",
    )
    assert "G1 Concurrent GC" not in java.cadence_beans
    assert not any(b.endswith("Pauses") for b in java.cadence_beans)
    # Empty for the CLR, and not an omission: gen0+gen1+gen2 IS its source, so
    # a fallback list here would be a second way to score the same runtime.
    assert registry.gc_semantics("dotnet").cadence_beans == ()


# Phrases that make a claim about a measurement that does NOT and WILL NOT
# exist. Prose (docstrings, comments) may say them -- explaining what round 1
# removed is legitimate history. A non-docstring string constant is different:
# it is what gets stamped into a ledger, a manifest note or a report bullet, and
# ships to a reader as this run's own record.
_STANDING_NEGATIVE = (
    "will not run", "never will", "never ran", "no longer declared",
    "descoped", "will not be measured", "removed from scope", "were removed",
)


def test_no_run_artifact_string_carries_a_standing_negative_declaration():
    """The governing rule with the sign flipped, mechanised.

    `run_validation_phase` stamped `note="... CO/oha/logging-sink/conn-sweep
    descoped at scope freeze"` into EVERY ledger, and `analyze` printed a report
    bullet saying those checks "never ran" -- inherited round-1 claims about
    work PROGRESS.md restores to round-2 scope, published inside round 2's own
    artifacts. A permanent claim in a published record that some measurement
    does not and will not exist is round 1's over-declaration with the sign
    flipped: `spec/` and the artifacts declare what IS, and are silent about
    what is not. Where a run genuinely lacks something, the honest form is a
    statement about THIS RUN ("the ledger carries X as deferred"), which the
    banned phrases below are not.
    """
    from ledgerbench.util import paths

    offences: list[str] = []
    for path in sorted((paths.repo_root() / "bench" / "ledgerbench").rglob("*.py")):
        for value in _string_constants(path):
            lowered = value.lower()
            for phrase in _STANDING_NEGATIVE:
                if phrase in lowered:
                    offences.append(f"{path.name}: {phrase!r} in {value[:90]!r}")
    assert not offences, (
        "these strings reach a run artifact and declare a measurement's permanent "
        "absence:\n  " + "\n  ".join(offences)
    )


def test_no_frozen_spec_file_carries_a_standing_negative_declaration():
    """The same rule, applied to the tree that hashes into `config_hash`.

    Five of these sat in `spec/` at the P1 review -- the hysteresis pass, the
    bench-tier CO checks, the logging-sink microbench, the connection sweep and
    the isolation cells -- four of them contradicting PROGRESS.md's own round-2
    scope table, which restores them. `spec/` freezes at P7 and is then
    unchangeable for the life of the run, so a "will not run" in it is a
    permanent claim that no later work can retract. The spec declares what IS;
    where round 2 genuinely does not do something, silence is correct.
    """
    from ledgerbench.util import paths

    offences: list[str] = []
    for path in sorted(paths.spec_dir().rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for lineno, line in enumerate(text.splitlines(), 1):
            for phrase in _STANDING_NEGATIVE:
                if phrase in line:
                    offences.append(f"{path.name}:{lineno}: {phrase!r}")
    assert not offences, (
        "spec/ freezes into config_hash and may not declare what will never be "
        "measured:\n  " + "\n  ".join(offences)
    )
