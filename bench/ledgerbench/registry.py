"""Slot registry: what a SUT variant is, and what its runtime's gates mean.

Every lookup here raises on an unregistered slot id, and no behaviour is
dispatched on a language string: a branch of the form `if lang == "dotnet": ...
else: ...` gives any other id the wrong runtime's GC model while its gate still
reports `passed`.

**The key is a SLOT, not a language.** The `shakedown` slot set runs .NET in
BOTH slots (`DATA_LAYER=efcore` against `ado`) to validate the harness
independently of Java, against a correctness oracle: round 1 measured EF Core at
about 20% more CPU per request than raw ADO.NET. Under a language key those two
slots collide on compose service, published port, pdf volume, ledger enum and,
worst, artifact filenames, where they would overwrite each other silently and
leave a run that looks valid. So: `LanguageSpec` carries what is true of a
*runtime*, `Slot` carries a named, buildable, configurable SUT variant, and a
`SlotSet` names the pair a run compares.

Nothing here imports from the rest of `ledgerbench`, so it can sit below every
consumer without a cycle. GC-steady semantics are declared as DATA (which series
must be flat), not as callables: `warmup.py` owns the trend test, and a
declarative form is also what gets recorded per probe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

# --------------------------------------------------------------------------- #
# GC semantics — the thing a rename would have silently corrupted
# --------------------------------------------------------------------------- #
# Series names a gc_steady gate can require. Both are per-poll series built by
# warmup.WarmupState from /runtime-stats (spec/runtime-stats.md).
SERIES_HEAP_COMMITTED = "heap_committed"
SERIES_COLLECTION_CADENCE = "collection_cadence"


@dataclass(frozen=True)
class GcSemantics:
    """What GC-STEADY MEANS for one runtime. Never a renamed gate.

    `requires` lists the series that must show no positive trend. Both runtimes
    currently require both, and that symmetry is deliberate rather than
    incidental: the headline memory metric is steady-state anon RSS, so neither
    stack may pass warmup with a still-climbing heap even if its collection
    cadence has settled.

    By-construction unsatisfiability is NOT declared here: it is a property of
    a KNOB SETTING rather than of the collector, and it applies to gates other
    than `gc_steady`, so it lives in `LanguageSpec.unsatisfiable_gates`.
    """

    model: str                              # how the collector generationally divides work
    requires: tuple[str, ...]
    collections_note: str                   # how gc.collections maps onto gen0/1/2, honestly
    # `gc.collections` keys whose value is a COLLECTION-CYCLE count, used as the
    # cadence source when a runtime publishes no gen0/1/2 triple. Empty for a
    # runtime whose triple IS the source. Every name here was read off a running
    # JVM; the exclusions are the point of the field and are argued at JVM_GC.
    cadence_beans: tuple[str, ...] = ()


# Measured on Temurin 25.0.4, both collectors, 2026-08-21 (see
# docs/research/2026-08-21-measured.md). The bean names are recorded there
# because spec/runtime-stats.md freezes them at P7 and a wrong string is
# unrecoverable. The trap worth naming here: under generational ZGC, *Cycles and
# *Pauses are SEPARATE MXBeans over the same pools, so a naive sum over all four
# double-counts collections and adds concurrent wall time to pause time.
JVM_GC = GcSemantics(
    model="regional-generational (G1) or concurrent-generational (ZGC)",
    requires=(SERIES_HEAP_COMMITTED, SERIES_COLLECTION_CADENCE),
    collections_note=(
        "gen0/gen1/gen2 are a present NULL on the JVM: no bean set maps onto "
        "the CLR's three generations without meaning something different per "
        "collector. The real counters ride beside the triple, one key per "
        "GarbageCollectorMXBean name, and `cadence_beans` names the subset that "
        "counts COLLECTIONS. Excluded, both measured on this box: ZGC's "
        "'*Pauses' beans, which are separate beans over the same pools as the "
        "'*Cycles' beans (1,029 minor cycles against 3,094 minor pauses in one "
        "18 s run), and 'G1 Concurrent GC', whose count is the Remark + Cleanup "
        "STW sub-pauses of a concurrent mark cycle, not a collection (86 events "
        "against exactly 43 Remark + 43 Cleanup in -Xlog:gc). Its TIME is real "
        "stop-the-world time and does belong in gc.pause_total_seconds, which "
        "is why the two bean sets cannot be one list."
    ),
    # G1: 'G1 Young Generation' == every Pause Young event (measured 736 =
    # 558 Normal + 92 Mixed + 43 Concurrent Start + 43 Prepare Mixed), and
    # 'G1 Old Generation' == Full GC only (measured 0 against 0 Pause Full,
    # while 92 mixed collections reclaimed old regions under the young bean --
    # which is exactly why it cannot be published as gen2).
    cadence_beans=(
        "G1 Young Generation", "G1 Old Generation",
        "ZGC Minor Cycles", "ZGC Major Cycles",
    ),
)

CLR_GC = GcSemantics(
    model="generational (gen0/1/2) with DATAS heap-count adaptation",
    requires=(SERIES_HEAP_COMMITTED, SERIES_COLLECTION_CADENCE),
    collections_note="System.Runtime dotnet.gc.collections, already per generation.",
    # Empty, and not an omission: the CLR publishes the triple, so gen0+gen1+gen2
    # IS its cadence source and there is no per-collector list to fall back to.
)


# --------------------------------------------------------------------------- #
# by-construction unsatisfiability — the knowledge R1-A3 paid ~30 h for
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UnsatisfiableGate:
    """A knob setting under which a warmup gate CANNOT pass, whatever the SUT does.

    A cell whose knob makes a gate unsatisfiable is refused at plan time
    (R1-A3): `phases.assert_gate_sets_satisfiable` refuses a cell that enables
    `gate` while declaring `env_var=value`, before its first probe runs.

    SCOPE, stated because it is narrower than the name suggests: the match is
    against the cell's DECLARED `Cell.extra_env` only. A knob sitting at its
    runtime DEFAULT is not in that dict and cannot be caught here -- the JVM's
    `-XX:MaxRAMPercentage=25` on the gc-default cell is exactly such a knob.
    That case is answered by MEASURING the cell (G6) and, if it cannot satisfy
    the gate, declaring `Cell.warmup_gates` + `gate_set_rationale`, which this
    module's predicate does not and must not pre-empt.
    """

    gate: str
    env_var: str
    value: str                              # compared case-insensitively
    why: str                                # the report-grade reason, one clause


# --------------------------------------------------------------------------- #
# language: what is true of a runtime, independent of how many slots run it
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LanguageSpec:
    """Runtime-level facts shared by every slot of this language."""

    lang: str
    display: str
    # env var -> manifest.Knobs attribute, for knobs that exist ONLY on this
    # runtime. Writing a Java knob onto the .NET row fabricates a value the .NET
    # SUT never saw.
    knob_env: Mapping[str, str] = field(default_factory=dict)
    int_knobs: frozenset[str] = frozenset()
    # Core parallelism is pinned in infra/compose.bench.yaml and asserted against
    # /runtime-stats build.processor_count. It is a (var, template) pair, not a
    # scalar map, because the JVM analog is a FLAG INSIDE an env var string
    # (-XX:ActiveProcessorCount=N) while .NET takes a bare integer.
    parallelism_var: str = ""
    parallelism_template: str = "{n}"
    gc: GcSemantics = CLR_GC
    # The PostgreSQL driver + pool this runtime uses, and the one pooling
    # behaviour that lands in the DB's cgroup CPU. Declared here because the
    # report's DB-CPU caveat used to name the Npgsql/PgJDBC pair as fixed text:
    # in a slot set where BOTH slots are .NET (the P2 shakedown) that paragraph
    # disclosed an asymmetry the run does not contain and named a driver that
    # never ran. `analyze._db_cpu_caveats` reads these off the run's own slots.
    db_driver: str = ""
    db_driver_pooling_note: str = ""
    # Warmup gates a knob of THIS runtime makes unsatisfiable by construction.
    #
    # EMPTY for both runtimes, and that is a claim about round 2 rather than an
    # unfinished table: R1-A3's only instance was Go's `GOGC=off`, which this
    # round does not run, and no round-2 posture has been MEASURED to be unable
    # to satisfy a gate. An entry here refuses a cell outright, so declaring one
    # on suspicion would stop the headline cell on an unmeasured belief -- the
    # over-declaration the governing rule forbids, pointed at the run instead of
    # at the report. G6 is where an entry gets earned.
    unsatisfiable_gates: tuple[UnsatisfiableGate, ...] = ()

    def parallelism_env(self, n: int) -> dict[str, str]:
        if not self.parallelism_var:
            return {}
        return {self.parallelism_var: self.parallelism_template.format(n=n)}


JAVA = LanguageSpec(
    lang="java",
    display="Java 25 (Spring Boot 4 + Hibernate 7)",
    knob_env={
        "JVM_MAX_RAM_PERCENT": "max_ram_percent",
        "JVM_GC_FLAGS": "gc_engine",
        # PgJDBC's prepareThreshold, the direct counterpart of Npgsql's
        # AUTO_PREPARE_MIN_USAGES under D4 (both promote on the 5th execution).
        # Declared here so it is scrubbed and recorded, which is the evidence
        # behind the pin spec/db-access.md states.
        "JDBC_PREPARE_THRESHOLD": "prepare_threshold",
    },
    int_knobs=frozenset({"max_ram_percent", "prepare_threshold"}),
    # JDK_JAVA_OPTIONS, not JAVA_TOOL_OPTIONS and not JAVA_OPTS: the `java`
    # launcher PREPENDS JDK_JAVA_OPTIONS to the command line, so a compose
    # overlay can override a base-file value; JAVA_OPTS is inert under an
    # ENTRYPOINT ["java","-jar",...] and a block full of it would look
    # configured while measuring JVM defaults.
    parallelism_var="JDK_JAVA_OPTIONS",
    parallelism_template="-XX:ActiveProcessorCount={n}",
    gc=JVM_GC,
    db_driver="PgJDBC + HikariCP",
    # VERBATIM from spec/db-access.md's per-checkout table; `test_db_access_spec`
    # fails if the two drift. It said "issues no reset on a pooled checkout -- an
    # observed ABSENCE" until 2026-08-24, which the spec had already retracted,
    # and it renders straight into the headline report's DB-CPU caveats.
    db_driver_pooling_note=(
        "sends a per-checkout aliveness probe only when the connection has been "
        "idle longer than HikariCP's 500 ms `aliveBypassWindowMs`, so at the "
        "rates this benchmark measures at it does not fire inside a measured "
        "window (spec/db-access.md)"
    ),
)

DOTNET = LanguageSpec(
    lang="dotnet",
    display=".NET 10 (Minimal API + EF Core)",
    knob_env={
        "DOTNET_GCDynamicAdaptationMode": "gc_dynamic_adaptation_mode",
        "MAX_AUTO_PREPARE": "max_auto_prepare",
        "AUTO_PREPARE_MIN_USAGES": "auto_prepare_min_usages",
        "DATA_LAYER": "data_layer",
    },
    int_knobs=frozenset({"max_auto_prepare", "auto_prepare_min_usages"}),
    parallelism_var="DOTNET_PROCESSOR_COUNT",
    parallelism_template="{n}",
    gc=CLR_GC,
    db_driver="Npgsql",
    # VERBATIM from spec/db-access.md's per-checkout table (see JAVA above).
    db_driver_pooling_note=(
        "resets a connection on every pooled CHECKOUT, which is DB-side work in "
        "PostgreSQL's cgroup (spec/db-access.md)"
    ),
)

LANGUAGES: dict[str, LanguageSpec] = {spec.lang: spec for spec in (JAVA, DOTNET)}

# Knobs that apply identically to every slot. DB_CONN_LIFETIME is here and was
# not in the round-1 map, which is why manifest.Knobs.db_conn_lifetime_seconds
# was ALWAYS null despite spec/db-access.md declaring the lever and compose
# setting it: an ambient export reached both SUTs and was never recorded.
SHARED_KNOB_ENV: Mapping[str, str] = {
    "DB_POOL_MAX": "db_pool_max",
    "DB_POOL_MIN": "db_pool_min",
    "DB_CONN_LIFETIME": "db_conn_lifetime_seconds",
    # Both SUTs read it (application.yaml `pool-warm-on-start`,
    # LedgerConfig.EnvBool) and spec/db-access.md rests the `pools_warm` gate's
    # MEANING on it being on: "both services open all DB_POOL_MAX connections
    # CONCURRENTLY at startup". Unscrubbed, an ambient export turned it off for
    # both SUTs, made that spec sentence false for the run, and left the manifest
    # unable to say so -- db_conn_lifetime_seconds' defect exactly.
    "DB_POOL_WARM_ON_START": "db_pool_warm_on_start",
    "REDIS_POOL_MAX": "redis_pool_max",
    "CACHE_TTL_SECONDS": "cache_ttl_seconds",
    "LOG_QUEUE_LEN": "log_queue_len",
}
SHARED_INT_KNOBS = frozenset({
    "db_pool_max", "db_pool_min", "db_conn_lifetime_seconds",
    "redis_pool_max", "cache_ttl_seconds", "log_queue_len",
})


# --------------------------------------------------------------------------- #
# slot: a named, buildable, configurable SUT variant
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Slot:
    slot_id: str
    language: LanguageSpec
    label: str                  # report column header and analysis label
    service: str                # infra/compose.yaml service
    profile: str                # infra/compose.yaml profile
    build_context: str          # repo-relative; asserted to exist before a run
    host_port: int
    pdf_volume: str
    url_env: str                # bench/validation/clients.py reads this
    expected_data_layer: str    # asserted against /runtime-stats build.data_layer
    slot_env: Mapping[str, str] = field(default_factory=dict)
    container_port: int = 8080
    # False only while a slot's build context is absent from the tree:
    # `active_slot_set` then refuses a slot set that needs it, instead of
    # letting compose fail at build time.
    implemented: bool = True

    def __post_init__(self) -> None:
        # The slot id is not just a label: it is the last dot-separated field of
        # the per-probe ledger key `r<rate>.b<block>.<slot>` and a path segment
        # in every per-probe artifact name. A dot would make the key ambiguous
        # to split and a slash would escape the artifact directory, both
        # silently -- which is the failure class this registry exists to remove.
        if "." in self.slot_id or "/" in self.slot_id or not self.slot_id:
            raise ValueError(
                f"slot id {self.slot_id!r} must be non-empty and contain neither "
                "'.' (the probe-key separator) nor '/' (an artifact path segment)"
            )

    def container_name(self, project: str = "ledgerline") -> str:
        return f"{project}-{self.service}-1"

    @property
    def host_url(self) -> str:
        return f"http://localhost:{self.host_port}"

    @property
    def net_url(self) -> str:
        return f"http://{self.service}:{self.container_port}"


SLOTS: dict[str, Slot] = {
    "java": Slot(
        slot_id="java", language=JAVA, label="Java",
        service="sut-java", profile="java", build_context="services/java",
        host_port=8081, pdf_volume="pdfstub-java", url_env="LEDGER_JAVA_URL",
        expected_data_layer="jpa",
        # P4 built services/java/ and the compose wiring landed with it;
        # `test_implemented_mirrors_the_tree` pins this flag to the build
        # context actually existing, in both directions.
        implemented=True,
    ),
    "dotnet": Slot(
        slot_id="dotnet", language=DOTNET, label=".NET",
        service="sut-dotnet", profile="dotnet", build_context="services/dotnet",
        host_port=8082, pdf_volume="pdfstub-dotnet", url_env="LEDGER_DOTNET_URL",
        expected_data_layer="efcore",
    ),
    # --- P2 shakedown: .NET in both slots -------------------------------- #
    # Same image, different DATA_LAYER. They are separate SLOTS (not one slot
    # re-parameterised) because they need disjoint artifact paths, and separate
    # compose SERVICES because a shared pdf volume would let one slot's writes
    # warm the page cache for the other on 8% of the mix.
    "dotnet-efcore": Slot(
        slot_id="dotnet-efcore", language=DOTNET, label=".NET/EF Core",
        service="sut-dotnet", profile="dotnet", build_context="services/dotnet",
        host_port=8082, pdf_volume="pdfstub-dotnet", url_env="LEDGER_DOTNET_URL",
        expected_data_layer="efcore", slot_env={"DATA_LAYER": "efcore"},
    ),
    "dotnet-ado": Slot(
        slot_id="dotnet-ado", language=DOTNET, label=".NET/ADO",
        service="sut-dotnet-b", profile="dotnet-b", build_context="services/dotnet",
        host_port=8083, pdf_volume="pdfstub-dotnet-b", url_env="LEDGER_DOTNET_B_URL",
        # NOTE the variable name: infra/compose.yaml gives sut-dotnet-b its own
        # ${DATA_LAYER_B} so setting it cannot reconfigure the first .NET slot.
        expected_data_layer="ado", slot_env={"DATA_LAYER_B": "ado"},
    ),
}


@dataclass(frozen=True)
class SlotSet:
    """The pair a run compares, and which side is the ratio denominator."""

    name: str
    slots: tuple[str, ...]
    # THE DENOMINATOR. PROGRESS.md defines the headline as a Java:.NET ratio.
    # Declaring the reference fixes the direction once, so it is never encoded
    # in a key name, and is also what makes P2 (both slots .NET) expressible at
    # all.
    reference: str

    def __post_init__(self) -> None:
        for sid in self.slots:
            get_slot(sid)
        if self.reference not in self.slots:
            raise KeyError(
                f"slot set {self.name!r}: reference {self.reference!r} is not one "
                f"of its slots {self.slots}"
            )

    @property
    def subject(self) -> str:
        """The ratio NUMERATOR: the non-reference slot."""
        others = [s for s in self.slots if s != self.reference]
        if len(others) != 1:
            raise ValueError(f"slot set {self.name!r} is not a clean pair: {self.slots}")
        return others[0]

    def ratio_label(self) -> str:
        return f"{get_slot(self.subject).label} : {get_slot(self.reference).label}"


# --------------------------------------------------------------------------- #
# lookups — every one raises rather than defaulting
# --------------------------------------------------------------------------- #
def get_slot(slot_id: str) -> Slot:
    try:
        return SLOTS[slot_id]
    except KeyError:
        raise KeyError(
            f"unregistered slot {slot_id!r}; known slots: {sorted(SLOTS)}. "
            "Slots are declared in ledgerbench/registry.py, never inferred."
        ) from None


def get_slot_set(name: str) -> SlotSet:
    try:
        return SLOT_SETS[name]
    except KeyError:
        raise KeyError(
            f"unknown slot set {name!r}; known sets: {sorted(SLOT_SETS)}"
        ) from None


def unsatisfiable_gates_for(
    slot_id: str, env: Mapping[str, str]
) -> dict[str, UnsatisfiableGate]:
    """Gates this slot cannot satisfy under `env`, keyed by gate name.

    THE predicate behind B3's plan-time refusal. It is also the only place the
    codebase reasons about by-construction unsatisfiability: the round-1 form of
    this knowledge was a literal table in `analyze.py` consulted while rendering
    the report, i.e. after the runtime had already been spent.
    """
    out: dict[str, UnsatisfiableGate] = {}
    for u in get_slot(slot_id).language.unsatisfiable_gates:
        if env.get(u.env_var, "").strip().casefold() == u.value.strip().casefold():
            out[u.gate] = u
    return out


def gc_semantics(slot_id: str) -> GcSemantics:
    """GC-steady semantics for a slot. Raises for an unregistered id.

    This raise IS the fix for round 1's `if lang == "dotnet": ... else: <Go>`
    dispatch, where a typo or a new language silently got the wrong collector's
    model and the gate still reported `passed`.
    """
    return get_slot(slot_id).language.gc


def knob_env_for(slot_id: str) -> dict[str, str]:
    """Every env var whose value belongs on this slot's knob row."""
    slot = get_slot(slot_id)
    return {**SHARED_KNOB_ENV, **slot.language.knob_env}


def int_knobs_for(slot_id: str) -> frozenset[str]:
    return SHARED_INT_KNOBS | get_slot(slot_id).language.int_knobs


def all_knob_env_vars() -> frozenset[str]:
    """Union across every registered slot: the compose scrub set (KNOB_ENV_VARS).

    Derived, never hand-maintained: a knob added to a language but missing from
    the scrub list means an ambient export in the operator's shell silently
    overrides the pinned baseline for both SUTs while the manifest records the
    intended value.
    """
    out = set(SHARED_KNOB_ENV)
    for spec in LANGUAGES.values():
        out |= set(spec.knob_env)
        if spec.parallelism_var:
            out.add(spec.parallelism_var)
    for slot in SLOTS.values():
        out |= set(slot.slot_env)
    return frozenset(out)


def runnable_slots() -> tuple[str, ...]:
    return tuple(sid for sid, s in SLOTS.items() if s.implemented)


SLOT_SETS: dict[str, SlotSet] = {
    "headline": SlotSet(name="headline", slots=("java", "dotnet"), reference="dotnet"),
    "shakedown": SlotSet(
        name="shakedown", slots=("dotnet-efcore", "dotnet-ado"), reference="dotnet-ado",
    ),
}

# The headline pair, now that `services/java/` exists and is wired. Phase P2
# (the .NET-vs-.NET harness shakedown) runs under an explicit
# LEDGERBENCH_SLOT_SET=shakedown rather than owning the default: the default
# names what the repo IS, not what runs next.
DEFAULT_SLOT_SET = "headline"

SLOT_SET_ENV = "LEDGERBENCH_SLOT_SET"


def active_slot_set() -> SlotSet:
    """The slot pair this process compares. Resolved lazily, never at import.

    Raises rather than silently falling back: a run that asks for the headline
    pair before the Java service exists must stop with a message naming why,
    not quietly measure something else.
    """
    name = os.environ.get(SLOT_SET_ENV, "").strip() or DEFAULT_SLOT_SET
    slot_set = get_slot_set(name)
    missing = [s for s in slot_set.slots if not SLOTS[s].implemented]
    if missing:
        raise RuntimeError(
            f"slot set {name!r} needs slot(s) {missing}, whose build context does "
            f"not exist yet. Set {SLOT_SET_ENV} to a runnable set "
            f"({sorted(n for n, ss in SLOT_SETS.items() if all(SLOTS[s].implemented for s in ss.slots))})."
        )
    return slot_set
