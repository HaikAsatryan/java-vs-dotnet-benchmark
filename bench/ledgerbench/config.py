"""Load spec/workload.yaml + spec/slo.yaml into validated pydantic models.

These two files are config inputs to the runner (the services do NOT read them).
The models below are intentionally faithful to the YAML so the manifest can echo
every resolved value (full-disclosure manifest). Validation here catches a
malformed/edited spec early, before any container starts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .util import paths


# --------------------------------------------------------------------------- #
# workload.yaml
# --------------------------------------------------------------------------- #
class Mix(BaseModel):
    model_config = ConfigDict(extra="forbid")
    get_invoice: int
    list_invoices: int
    pricing_quote: int
    create_invoice: int
    pdf_stub: int
    healthz: int

    @model_validator(mode="after")
    def _sum_100(self) -> "Mix":
        total = (
            self.get_invoice
            + self.list_invoices
            + self.pricing_quote
            + self.create_invoice
            + self.pdf_stub
            + self.healthz
        )
        if total != 100:
            raise ValueError(f"mix percentages must sum to 100, got {total}")
        return self

    def as_pairs(self) -> list[tuple[str, int]]:
        """Stable (name, weight) order matching the workload file."""
        return [
            ("get_invoice", self.get_invoice),
            ("list_invoices", self.list_invoices),
            ("pricing_quote", self.pricing_quote),
            ("create_invoice", self.create_invoice),
            ("pdf_stub", self.pdf_stub),
            ("healthz", self.healthz),
        ]


class Zipf(BaseModel):
    model_config = ConfigDict(extra="ignore")
    alpha: float
    note: str | None = None


class Keyspace(BaseModel):
    """One keyspace's DRAW, checked by `vegeta.assert_keyspace_contract`.

    The corpus is `seeding.scales[<the run's seed_scale>]`.
    """

    model_config = ConfigDict(extra="ignore")
    selection: str
    used_by: list[str] = Field(default_factory=list)
    runtime_created_ids: str | None = None


class Keyspaces(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customers: Keyspace
    invoices: Keyspace


class PageDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights: dict[int, float]

    @model_validator(mode="after")
    def _check(self) -> "PageDistribution":
        if not self.weights:
            raise ValueError("page weights empty")
        s = sum(self.weights.values())
        if abs(s - 1.0) > 1e-6:
            raise ValueError(f"page weights must sum to 1.0, got {s}")
        return self

    def ordered(self) -> list[tuple[int, float]]:
        return sorted(self.weights.items())


class IntRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: int
    max: int

    @model_validator(mode="after")
    def _check(self) -> "IntRange":
        if self.min > self.max:
            raise ValueError(f"min {self.min} > max {self.max}")
        return self


class PayloadSpec(BaseModel):
    model_config = ConfigDict(extra="allow")  # fields block has nested shapes
    item_count: IntRange
    target_bytes: int
    fields: dict


class Payloads(BaseModel):
    model_config = ConfigDict(extra="forbid")
    create_invoice: PayloadSpec
    pricing_quote: PayloadSpec


class Rng(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_generator_seed_default: int
    algorithm: str


class CacheConstruction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    target_hit_rate: float
    capacity_gate_lever: str | None = None


class Scale(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customers: int
    invoices: int


class Seeding(BaseModel):
    model_config = ConfigDict(extra="ignore")
    seed: int
    scales: dict[str, Scale]
    items_per_invoice: IntRange


class Workload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mix: Mix
    zipf: Zipf
    keyspaces: Keyspaces
    page_distribution: PageDistribution
    payloads: Payloads
    rng: Rng
    cache_construction: CacheConstruction
    seeding: Seeding


# --------------------------------------------------------------------------- #
# slo.yaml
#
# Every model below is `extra="forbid"`, and that is the governing rule enforced
# at LOAD time rather than at review time: nothing enters spec/ as a declared
# gate ahead of the code that reads it. Under `extra="ignore"` a key added to
# spec/slo.yaml would bind to nothing, change `config_hash`, and be published as
# pre-registered method. Forbidding extras makes that edit fail at import,
# so a new gate cannot reach a run without a field here, and a field deleted
# while the spec still carries the key fails the same way.
#
# The cost is deliberate and is why this landed last in P1: a spec key and a
# model field must now be added in the same change, and until they are, `doctor`,
# `status`, `run-all` and the whole test suite refuse to import.
# --------------------------------------------------------------------------- #
class Slo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    threshold_ms: float
    applies_to: str
    error_rate_max: float


class Equivalence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    margin_pct: float
    # decision_rule / inside_margin / outside_margin are DESCRIPTIVE
    # pre-registration of method, not gates: they put the equivalence procedure
    # inside `config_hash` so it cannot be chosen after seeing the data. No code
    # branches on their values -- `analysis/paired.py` implements exactly the
    # procedure they name. The governing rule constrains declared GATES, so
    # these are declared here and kept; deleting them would move the method
    # out of the hashed spec without deleting the method.
    decision_rule: str
    inside_margin: str
    outside_margin: str


class Ladder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_rate_rps_default: int
    step_factor: float
    rung_ceiling: int
    reps_per_rung: int
    confirm_reps: int
    window_seconds: int
    warmup_discard_seconds: int
    attack_timeout_seconds: float
    # Descriptive pre-registration, exactly like equivalence's three above: each
    # names an implementation that exists (analysis/pava.py, analysis/knee.py,
    # analysis/paired.py) and no code branches on the string. Their job is to be
    # hashed, so the fit family and the pairing scheme are fixed before the data.
    interleave: str
    knee_fit: str
    knee_ci: str


class PostureWaiver(BaseModel):
    """One posture's declared gate overrides, WITH the reason for them.

    Shaped this way so the spec layer is at least as strict as the code layer:
    `phases.Cell` refuses a `warmup_gates` override with no `gate_set_rationale`
    because the stated reason is the only thing separating a waiver from R1-A3's
    ~30 h of runtime against a gate that could not pass. The same declaration
    made in `spec/slo.yaml` -- the layer inside `config_hash`, and therefore the
    harder one to notice -- carried no such requirement until P1/W24.
    """

    model_config = ConfigDict(extra="forbid")
    gates: dict[str, bool]
    rationale: str

    @model_validator(mode="after")
    def _states_what_it_rests_on(self) -> "PostureWaiver":
        if not self.gates:
            raise ValueError("a posture waiver that overrides no gate does nothing")
        if not self.rationale.strip():
            raise ValueError(
                f"posture waiver over {sorted(self.gates)} has no rationale; a "
                "posture-specific gate set is a claim about what this posture "
                "can satisfy and must say what that claim rests on"
            )
        return self


class WarmupCfg(BaseModel):
    # The whole SLO tree forbids extras; this block was flipped first because it
    # is where an ignored key is INVISIBLE RATHER THAN INERT. Every other
    # typo in a posture waiver is already refused -- a misspelt gate name by
    # `_waivers_name_declared_gates`, a misspelt cell id by
    # `phases.posture_waiver_conflicts`, a missing rationale and a stray key
    # inside the waiver by `PostureWaiver` -- but a misspelt BLOCK name
    # (`gates_by_postures:`) was dropped at load: the gate stayed enforced, the
    # waiver bound to nothing, and `config_hash` still changed, so the run
    # looked deliberately re-specified.
    model_config = ConfigDict(extra="forbid")
    hard_cap_seconds: int
    poll_hz: int
    per_endpoint_min_calls: dict[str, int]
    gates: dict[str, bool]
    # Per-posture (per-cell) overrides of which gates are ENFORCED, keyed by
    # cell id. Empty, and it stays empty until a posture is MEASURED to be
    # unable to satisfy a gate: round 1 ran a cell whose GC posture made
    # `gc_steady` unsatisfiable by construction and spent ~30 h flagging every
    # window of it (R1-A3). The resolver exists before the finding so that
    # finding lands in the pre-registered spec rather than in a disclosure note.
    # The cell ids are validated by `phases.posture_waiver_conflicts`, which is
    # where the cell table lives; the gate names are validated below.
    gates_by_posture: dict[str, PostureWaiver] = Field(default_factory=dict)
    note: str | None = None

    @model_validator(mode="after")
    def _waivers_name_declared_gates(self) -> "WarmupCfg":
        """A waiver for a gate `gates` never declared overrides nothing.

        It reads as an exemption and is a no-op, which is the misspelt-gate case:
        `{"gc_stedy": false}` leaves `gc_steady` enforced and shows up nowhere.
        """
        for cell_id, waiver in self.gates_by_posture.items():
            unknown = sorted(set(waiver.gates) - set(self.gates))
            if unknown:
                raise ValueError(
                    f"warmup.gates_by_posture[{cell_id!r}] overrides {unknown}, "
                    f"which warmup.gates does not declare "
                    f"(declared: {sorted(self.gates)})"
                )
        return self

    def gates_for(self, cell_id: str) -> dict[str, bool]:
        """The enforced-gate set for one posture.

        MERGED over `gates`, never replacing it: a posture that spelled out a
        full set would silently stop enforcing any gate added globally after it
        was written, which is the failure this override exists to prevent.
        """
        waiver = self.gates_by_posture.get(cell_id)
        return {**self.gates, **(waiver.gates if waiver else {})}

    def posture_rationale(self, cell_id: str) -> str:
        """The stated reason for this posture's waiver, or "" if it has none."""
        waiver = self.gates_by_posture.get(cell_id)
        return waiver.rationale if waiver else ""


class StallTest(BaseModel):
    """The docker-pause CO sanity check (smoke.run_smoke step 5).

    Typed rather than a raw dict because the raw dict let the runner read
    `stall_test["timeout_s"]` -- a key this block has never defined -- for the
    vegeta ATTACK timeout, which is not a property of a stall test at all. The
    read silently returned its 5.0 fallback while looking spec-controlled; the
    attack timeout is now declared where the attack is, at
    `ladder.attack_timeout_seconds`. An attribute access on this model cannot
    fail silently.
    """

    model_config = ConfigDict(extra="forbid")
    freeze_seconds: float
    method: str


class CoValidation(BaseModel):
    # The two CO checks the harness scores, and nothing else: the tolerance is
    # read at `phases._execute_block_probes` (a probe outside it is EXCLUDED from
    # the fit input on measured tiers), and the stall test is performed by
    # `smoke.run_stall_check`. A third CO check cannot be declared here without a
    # field, which is the point of the flip.
    model_config = ConfigDict(extra="forbid")
    achieved_vs_offered_tolerance_pct: float
    stall_test: StallTest


class CapacityLever(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    # The two numeric levers carry from/to; the core-move lever adds taken_from.
    from_: float | int | None = Field(default=None, alias="from")
    to: float | int | None = None
    taken_from: str | None = None


class CapacityGate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headroom_factor: float
    levers_in_order: list[CapacityLever]
    never: str | None = None


class SloConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slo: Slo
    equivalence: Equivalence
    ladder: Ladder
    warmup: WarmupCfg
    co_validation: CoValidation
    capacity_gate: CapacityGate
    # stationarity / soak are read by the runner and analysis stages; kept as
    # raw dicts here so the runner core stays lean but the values are still
    # echoed. Their CONTENTS are therefore the one place in this file a stray key
    # still passes; both blocks are small enough to read, and `soak` is pinned
    # key-for-key by test_slo_config.
    stationarity: dict | None = None
    soak: dict | None = None


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_workload(path: Path | None = None) -> Workload:
    return Workload.model_validate(_load_yaml(path or paths.workload_path()))


def load_slo(path: Path | None = None) -> SloConfig:
    return SloConfig.model_validate(_load_yaml(path or paths.slo_path()))


def config_hash(spec_dir: Path | None = None) -> str:
    """SHA-256 over every file under spec/ (recursively).

    Goes into the run-ledger/manifest so a resumed run can detect a spec edit.
    The docs promise "a hash of spec/", so this covers the WHOLE spec tree
    (workload.yaml, slo.yaml, schema.sql, openapi.yaml, postgres/*, the .md
    contracts, etc.), not just the two YAMLs. Each entry mixes in the file's
    sorted POSIX-relative path then its raw bytes, so a rename, a content change,
    or a comment edit all change the hash; sorting makes it order-stable.

    Sorting is on the POSIX-relative path SPLIT INTO PARTS, never on the Path
    object: `sorted()` over `WindowsPath` compares case-insensitively, which
    reorders `postgres/README.md` against `postgres/initdb/...` and yields a
    different digest on Windows than on Linux for a byte-identical tree. That
    would make a published hash unverifiable off-Linux and would break `--resume`
    across platforms. Splitting on "/" reproduces PosixPath's part-wise ordering
    on every platform.
    """
    root = spec_dir or paths.spec_dir()
    h = hashlib.sha256()
    files = sorted(
        (p for p in root.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(root).as_posix().split("/"),
    )
    for p in files:
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()
