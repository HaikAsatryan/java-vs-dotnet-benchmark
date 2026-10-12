# P1 work plan — harness de-language + round-1 defect fixes

> **Frozen at publication, 2026-10-12.** This is the working plan for phase P1, kept as
> written apart from a few wording edits. Its `path:line` anchors and commit `ecc0476`
> refer to a pre-publication tree that is not in the published history; §0 and §6 describe
> that tree, not the published one. Outcomes, including items left open here, are recorded
> in `PROGRESS.md` (phase P1 and the P7 review). Captures it mentions were not kept.

*Companion to `PROGRESS.md` (plan of record) and `CLAUDE.md` (standing rules). Every claim below is anchored to `path:line` in the tree as of `ecc0476`.*

---

## 0. State of play before the first edit

**Already landed at P1** (`git diff` against `ecc0476`):

- **O4 RESOLVED** — `spec/slo.yaml:33` `warmup.hard_cap_seconds` raised 180 → 300. **This plan treats that as a decision made and adds the implementation it presupposes** (W13). See §4.2: the comment it landed with asserts behaviour the code does not have.
- **D11 added** — Maven + `mvnw`, pinned 3.9.16.
- **`docs/research/2026-08-21-measured.md`** — a measured JVM/Hibernate spike against real PostgreSQL 18.4 and Temurin 25.0.4. It supplies the exact MXBean names, JVM flag defaults and origins, the timestamp formatter, and the Hibernate statement shapes that the P1 spec edits need. **Everything in §4 is written against measured strings from that file.** Three of its findings change P1 scope directly:
  - `GenerationType.IDENTITY` disables Hibernate JDBC batching (6 rows → 6 prepares). `spec/db-access.md`'s "round-trip counts ASSERTED EQUAL" pin **cannot hold** and must be rewritten in P1 (W28).
  - `heap_goal_bytes` has a real JVM analog (`SoftMaxHeapSize`, `origin=ERGONOMIC`) under ZGC, and equals `MaxHeapSize` under G1. **Do not delete the field** — map it, and report `null` under G1.
  - JDK 25 reads cpuset correctly (`--cpuset-cpus=0-5` → `availableProcessors()==6` at `ActiveProcessorCount=-1`). P4 trap 3 does not reproduce; the pin stays for symmetry and for `HotSpotDiagnosticMXBean` read-back evidence.

**The tree is red before P1 touches it.** Three failures unrelated to Java (see §6). Fix them first or "G1 green" is unmeasurable.

---

## 1. The registry — the spine of Workstream A

### 1.1 The decision: SLOT, not LANGUAGE

The plan text says "per-language registry". **That is the wrong key, and choosing it would block P2 on day one.**

P2 de-risks the entire P1 rewrite by running **.NET in both slots** (`DATA_LAYER=efcore` in A, `ado` in B) against a correctness oracle, weeks before any Java exists. Under a language-keyed registry the two slots collide on every axis the harness uses to tell them apart:

| collision | site |
|---|---|
| compose service / profile | `bench/ledgerbench/compose.py:25-33`, one `sut-dotnet` in `infra/compose.yaml:159` |
| host port | `bench/ledgerbench/live_runner.py:51` (`8082` for both) |
| pdf volume | `infra/compose.yaml:254-255` — a shared volume cross-warms the page cache on 8% of the mix |
| **artifact filenames** | `live_runner.py:387,394,398,404,426,439` — ten `f"{lang}..."` sites; two `dotnet` slots **overwrite each other silently and the run completes looking valid** |
| ledger type | `ledger.py:42` `lang: Literal["go","dotnet"]` — pydantic physically cannot record the run |
| per-cell env | `phases.py:191` `Cell.extra_env` is **one** dict; `live_runner.py:285` applies it to whichever SUT is starting |
| data-layer assertion | `phases.py:1048` `expected_dl = {"go": "sqlc", "dotnet": env.get("DATA_LAYER") or "efcore"}` → the `ado` slot **always** mismatches and raises `PhaseError` on Linux |

Meanwhile some properties genuinely *are* per-language and must not be duplicated per slot: GC-steady semantics, the knob→manifest env map, the `/runtime-stats` field mapping, the parallelism knob.

**Decision: a two-level registry.** `LanguageSpec` carries runtime semantics; `Slot` is *a named, buildable, configurable SUT variant* and points at a language. The registry is keyed by `slot_id`; a named `SlotSet` picks which slots a run uses. `ProbeRecord.lang` becomes `ProbeRecord.slot`.

This also removes a live bug: `warmup.py:264-269` dispatches gc-steady on `if self.language == "dotnet": … else: gc_steady_go(…)`. **Any string that is not literally `"dotnet"` silently receives Go's single-generation semantics.** A registry lookup that *raises* on an unknown key is the fix; a rename preserves the trap.

### 1.2 Module

`bench/ledgerbench/registry.py` — a leaf module importing nothing from the package, so it sits below `compose.py` and cannot cycle.

```python
@dataclass(frozen=True)
class GcSemantics:
    """What GC-STEADY MEANS for this runtime. Never a renamed gate."""
    model: str                       # "regional-generational" | "generational" | "concurrent-generational"
    cycle_beans: tuple[str, ...]     # G1: ("G1 Young Generation","G1 Concurrent GC","G1 Old Generation")
                                     # ZGC: ("ZGC Minor Cycles","ZGC Major Cycles")   <- Cycles ONLY
    pause_beans: tuple[str, ...]     # ZGC: ("ZGC Minor Pauses","ZGC Major Pauses"); G1: == cycle_beans
    committed_source: str            # "MemoryMXBean.getHeapMemoryUsage().committed"
    steady_fn: Callable[[list[float], list[float]], bool]   # RAISES for an unregistered runtime
    unsatisfiable_under: frozenset[str]                     # posture ids where this gate CANNOT pass (B3)

@dataclass(frozen=True)
class LanguageSpec:
    lang: str                        # "java" | "dotnet"
    display: str                     # "Java 25 (Spring Boot 4 + Hibernate 7)"
    knob_env: Mapping[str, str]      # LANG-ONLY env var -> manifest Knobs attribute
    int_knobs: frozenset[str]        # names cast to int by phases._set_knob
    parallelism: tuple[str, str]     # ("JDK_JAVA_OPTIONS", "-XX:ActiveProcessorCount={n}")
                                     #  vs ("DOTNET_PROCESSOR_COUNT", "{n}")  -- flag vs scalar
    gc: GcSemantics

@dataclass(frozen=True)
class Slot:
    slot_id: str                     # "java" | "dotnet" | "dotnet-efcore" | "dotnet-ado"
    language: LanguageSpec
    label: str                       # analysis/report column header
    service: str                     # compose service: "sut-java"
    profile: str                     # compose profile: "java"
    build_context: str               # "services/java"   (doctor asserts it exists)
    host_port: int                   # 8081 / 8082 / 8083
    container_port: int = 8080
    pdf_volume: str                  # "pdfstub-java"
    url_env: str                     # "LEDGER_JAVA_URL"  (bench/validation/clients.py:19-20)
    slot_env: Mapping[str, str]      # per-slot env, e.g. {"DATA_LAYER": "ado"}
    expected_data_layer: str         # asserted against /runtime-stats build.data_layer

    def container_name(self, project: str = "ledgerline") -> str:
        return f"{project}-{self.service}-1"
    @property
    def host_url(self) -> str: return f"http://localhost:{self.host_port}"
    @property
    def net_url(self)  -> str: return f"http://{self.service}:{self.container_port}"

@dataclass(frozen=True)
class SlotSet:
    name: str                        # "headline" | "shakedown"
    slots: tuple[str, ...]           # canonical order; replaces every ("go","dotnet") literal
    reference: str                   # the RATIO DENOMINATOR and the equivalence-margin reference

SHARED_KNOB_ENV: Mapping[str, str] = {...}   # DB_POOL_MAX .. DB_CONN_LIFETIME .. BODY_MAX_BYTES
SLOTS:    dict[str, Slot]    = {...}
SLOT_SETS: dict[str, SlotSet] = {
    "headline":  SlotSet("headline",  ("java", "dotnet"),                 reference="dotnet"),
    "shakedown": SlotSet("shakedown", ("dotnet-efcore", "dotnet-ado"),    reference="dotnet-ado"),
}
```

Three fields earn their place beyond the obvious:

- **`SlotSet.reference`.** `analysis/paired.py:22-25` states the ±5% margin is "expressed against the .NET (reference) mean", and `analyze.py:361/523/536` name the ratio `dotnet_over_go`. `PROGRESS.md:16` defines the headline as a **Java:.NET** ratio — the *opposite* direction. A mechanical rename produces an inverted headline that reads plausibly and is wrong by a reciprocal. Making the reference a declared registry field fixes the direction once and makes P2 (where both slots are .NET) expressible.
- **`parallelism` as `(var, template)`.** `compose.bench.yaml:42/48` set `GOMAXPROCS`/`DOTNET_PROCESSOR_COUNT` as scalars; the JVM analog `-XX:ActiveProcessorCount` is a **flag inside an env var string**. A `Mapping[str,str]` models the scalar shape only.
- **`GcSemantics.steady_fn` raising on an unknown runtime** — the direct fix for `warmup.py:264-269`.

### 1.3 Consumers, with anchors

| file | sites |
|---|---|
| `bench/ledgerbench/sut.py` | `:23` `Lang`, `:25-32` `_SVC_BY_LANG`/`_PROFILE_BY_LANG`, `:35-40` `SutHandle`, `:43-49` `env_for`, `:52-75` `start_fresh`, `:78-81` `stop` — **all dead** (grep-verified: zero callers). `:84-105` `wait_ready` is the only live symbol (`live_runner.py:306,607,665,701`). |
| `bench/ledgerbench/compose.py` | `:25-26,30-31` service/profile constants; `:108-112` `KNOB_ENV_VARS`; `:4` docstring; `:113-115` not-scrubbed comment; `:172-186` `container_pid` (needs a `host_unit_cgroup` sibling for dockerd) |
| `bench/ledgerbench/ledger.py` | `:41` `slot_order`, `:42` `lang: Literal`, `:222-255` `reset_orphan_blocks` docstring + two-member rule, `:112`/`:130` bare `except Exception: continue` |
| `bench/ledgerbench/live_runner.py` | `:51,52,53,54,55` four dicts + `_VEGETA_CONTAINER`; `:208,222,285,293-297,317,320`; `:387,394,398,404,426,439` artifact names; `:602-607,643,656-659,672-673,677-678,687-693` |
| `bench/ledgerbench/phases.py` | `:57-60` comment; `:176,198-213,216-241` cells; `:263-265` `slot_order_for_block`; `:268-284` `plan_blocks`; `:311-324` `ProbeRunner`; `:969-988` knob maps; `:1023-1052` knob rows + `expected_dl`; `:1100` `or ["go","dotnet"]` fallback; `:1125` probe key; `:1441-1444`, `:1607-1608` globs |
| `bench/ledgerbench/manifest.py` | `:61-90` `Versions` (go/pgx/sqlc); `:93-109` `Knobs`; `:112-128` `CellKnobs.go/.dotnet`; `:429-430` `knobs_go`/`knobs_dotnet` |
| `bench/ledgerbench/warmup.py` | `:11-14` docstring; `:127-147` `gc_steady_go`/`_dotnet`; `:229-236` `_gen_cadence`; `:264-271` dispatch |
| `bench/ledgerbench/analyze.py` | `:80` `_LANGS`; `:359-379,396-410,418-441,506-538` dataclass fields + JSON keys; `:586` `_lang_label` (bare `else` → `.NET`); `:816-833,837-867` pairing; `:901-932,982-1025,1084-1146` |
| `bench/analysis/paired.py` | `:3-5,40-43,49,105-131,152-164` |
| `bench/analysis/report.py` | `:21-25` `_VERDICT_WORD`; `:32-55,58-66,69-76,79-129` (all dead outside tests); `:132-151` second assembler |
| `bench/ledgerbench/capacity_gate.py` | `:688` `base_url="http://sut-go:8080"` |
| `bench/ledgerbench/smoke.py` | `:44-52,92,116-125,140-143,261,269-279` |
| `bench/validation/clients.py` | `:19-20,26-31,46-55,58-59,62-75` |
| `infra/` | `compose.yaml:106-152,254`; `compose.bench.yaml:36-42` |
| tests | `test_smoke_and_compose.py:29,45,46`; `test_live_runner.py:36,48`; `test_status_cli.py:32-33`; `test_ledger.py:41-58`; `test_cgroups.py:14,17,22`; `test_capacity_gate.py:347-362`; `test_phases.py` (46 sites); `test_analyze.py` (64 sites); `test_manifest.py` (17); `test_warmup.py` (10) |

**Do not touch:** `bench/ledgerbench/util/pcg64.py:20` ("the *seeder* (Go) uses ChaCha8…") is **still true** — `seed/` stays Go by decision. A blanket `s/Go/Java/` over `bench/` corrupts a correct statement into a false one. Same for `infra/vegeta/Dockerfile:8-14` (vegeta is Go tooling) and `seed/go.mod`.

---

## 2. Ordered work items

Nothing below is blocked by anything after it.

### Stage 0 — Unblock (the tree is red before P1)

**W1 · Restore `.editorconfig`** — closes a P0 curation loss.
`.editorconfig` (new, repo root or `services/dotnet/`): `[*.cs] indent_style=space, indent_size=3, end_of_line=lf`; exclude `services/dotnet/Data/CompiledModels/**` (generated at 4 spaces by `make regen-dotnet`). Add `.editorconfig text eol=lf` to `.gitattributes`.
*Why now:* `.github/workflows/ci.yml:65-66` **documents** the file and `:67` depends on it; the file does not exist (`git ls-files` confirms). Measured: `dotnet format --verify-no-changes` exits 2 with **1,697 `WHITESPACE` errors across 41 files**. `make fmt` (`Makefile:65`) would rewrite all 41 to 4-space indent and destroy the style template P4 is instructed to mirror.
*Test:* `cd services/dotnet && dotnet format --verify-no-changes` exits 0 **and** `git status --porcelain` is empty afterwards. — **S** — deps: none.

**W2 · Repair `Makefile` + CI for the deleted `services/go`** — closes a hard break.
`Makefile:14,55,57-58` (delete `regen-go`), `:63-64` `fmt` → `cd seed && gofmt -w .` + a Java formatter, `:68` `gofmt -l seed`, `:71` (delete), `:72` (keep). `.github/workflows/ci.yml:36` rename job to `seed (go)`, `:43` `go-version-file: seed/go.mod`, `:46` `gofmt -l seed`, `:48-51` delete.
*Why now:* `services/go/` does not exist, so `setup-go` fails **before any build runs**; `make lint` fails at `Makefile:68`; `make smoke` fails because `smoke.py:92` builds `SVC_SUT_GO` against `infra/compose.yaml:109`'s dead context. Note `docker compose config` exits 0 on a nonexistent build context (verified), so `infra/NOTES.md:126`'s "parses cleanly" is true and misleading.
*Test:* a repo-hygiene test asserting (a) `services/go` appears in zero tracked files outside `PROGRESS.md`, and (b) every path named by a Makefile recipe or a `ci.yml` `working-directory`/`*-version-file` exists on disk. — **S** — deps: none.

**W3 · `util/proc.run` honours its own timeout contract** — closes an unattended-run killer.
`bench/ledgerbench/util/proc.py:45-53` — wrap `subprocess.run` in `try/except subprocess.TimeoutExpired`, return a `ProcResult` with a sentinel returncode and a stderr naming the timeout; raise only when `check=True`.
*Why now:* zero `TimeoutExpired` handlers exist in `bench/`. A slow `docker stop` inside `run_probe` (`check=False, timeout=120`) currently **aborts the whole run** instead of returning a failed `ProbeOutcome`; a wedged daemon tracebacks out of `ledgerbench doctor`. On a 15 h cell across D10's three runs this is the difference between a retried probe and a lost night.
*Test:* `run(["sleep","5"], timeout=0.1)` returns `ok is False` and does not raise; the same with `check=True` raises; `run_doctor()` reports `all_ok False` when `run` times out. — **S** — deps: none.

### Stage 1 — The registry

**W4 · `bench/ledgerbench/registry.py`** — Workstream A spine.
New file per §1.2. Both slot sets defined. Every lookup raises on an unregistered id.
*Test:* `bench/tests/test_registry.py` — (a) parse `infra/compose.yaml` and assert the set of services carrying a non-`load`/`seed` profile equals `{s.service for s in SLOTS.values()}`, with matching `build.context`, published port and pdf volume; (b) no two slots share a port, volume or container name; (c) `SLOT_SETS["shakedown"]` yields two distinct services and two distinct container names — *P2 proven before P2 starts*; (d) an unregistered slot id raises everywhere (gc-steady lookup, profile lookup, label lookup) rather than defaulting. — **M** — deps: none.

**W5 · Ledger schema: slot id, version guard, loud load failures** — unblocks everything.
`ledger.py:42` `lang: Literal[...]` → `slot: str` with a field validator against `registry.SLOTS`; `:41` `slot_order` non-defaulting (delete the `or ["go","dotnet"]` fallback at `phases.py:1100`); bump `LEDGER_SCHEMA_VERSION` (`:22`, written but never read) **and** add a guard in `load`; move `_probe_key`/`_split_key` (`analyze.py:86-96`, duplicated as an f-string at `phases.py:1125`) into `ledger.py` as the single definition; replace the bare `except Exception: continue` at `:112,130` with a stderr report.
*Why the swallow matters:* a ledger the current code cannot validate **disappears** from `latest_unfinished()`, and `--resume` reports "no unfinished run to resume" (`__main__.py:127-131`) for what is actually a schema problem.
*Test:* `ProbeRecord(slot="rust")` raises naming the registry; a probe built without `slot_order` fails rather than defaulting; an unparseable ledger.json is surfaced on stderr, not skipped; a v1 ledger is refused by the v2 loader. — **M** — deps: W4.

**W6 · `compose.py` / `sut.py`: delete the dead twin, derive the scrub set** — removes the drift risk the registry exists to close.
Delete `sut.py:23-81` entirely (grep-verified zero callers), keeping `wait_ready` (`:84-105`) and raising its default `timeout_s` 120 → 180 to match every live caller. Derive `KNOB_ENV_VARS` as `SHARED_KNOB_ENV | ⋃ lang.knob_env`. **Add `DB_CONN_LIFETIME`** (spec-declared at `spec/db-access.md:125`, present at `compose.yaml:123,173`, absent from both the scrub list and `phases._SHARED_KNOB_ENV:979-985`, so `manifest.Knobs.db_conn_lifetime_seconds:99` is *always null* and an ambient export reaches both SUTs unrecorded). **Add `AUTO_PREPARE_MIN_USAGES`** (read at `LedgerConfig.cs:72`, set nowhere, scrubbed nowhere, unrecorded — and it is the direct counterpart of PgJDBC `prepareThreshold=5` under D4). **Add the JVM flag carriers** `JAVA_TOOL_OPTIONS`/`JDK_JAVA_OPTIONS` — the single highest-value scrub entry on a Java run. **Drop `SUT_MEM`** (a topology knob, inconsistent with the `:113-115` comment). Add `compose.container_name(service, project)` (kills the hard-coded `"ledgerline-"` at `live_runner.py:317,55` — a non-default `-p` silently resolves **no** cgroup path, voiding the headline numbers) and `compose.host_unit_cgroup(unit)` for dockerd.
*Test:* `test_knob_scrub_covers_every_compose_interpolation` — regex every `${NAME` out of both compose files, subtract a module-level `NOT_SCRUBBED` allowlist (each entry with a one-line reason), assert the remainder equals `set(KNOB_ENV_VARS)`. **Must land in the same commit as the derivation** or the registry can silently *shrink* the scrub set. Plus `test_container_name_honours_the_project_name` and a grep test that no module contains a hard-coded `"ledgerline-"`. — **M** — deps: W4.

**W7 · Manifest schema: `by_slot` rows, JVM knobs, `Versions`** — free now, unloadable-on-resume later.
`manifest.py:127-128` `CellKnobs.go/.dotnet` → `by_slot: dict[str, Knobs]`; `:429-430` `knobs_go`/`knobs_dotnet` → `knobs_by_slot`; `:103,104,108` `gogc`/`gomemlimit`/`gomaxprocs` → JVM fields (`max_ram_percent`, `gc_engine`, `active_processor_count`, `jvm_options`); add `auto_prepare_min_usages` and `prepare_threshold`; `:61-90` `Versions` `go`→`java`, `pgx`→`pgjdbc`, drop `sqlc`; bump `MANIFEST_SCHEMA_VERSION` (`:35`) to 3. Populate `Versions` while here — `compose.container_image_digest` (`compose.py:145-169`) is fully implemented and **dead**, and `runtimestats.Build` is already fetched at `phases.py:1044-1060` and discarded. Delete or populate `Identity.schema_sql_sha256`/`generated_code_hashes` (`:56-57`), the last two never-written fields with **no** `not_measured` marker — `generated_code_hashes: {}` currently publishes as "measured and empty".
*Why now, not later:* `RunManifest` is `extra="forbid"` and `RunManifest.load` (`:464-469`) does not catch `ValidationError`, while its only caller writes `RunManifest.load(run_id) or new_manifest(...)` (`phases.py:1828`) — an `or` cannot rescue a raise. After the first run exists, this rename **crashes every resume**.
*Test:* `test_knobs_by_slot_roundtrips_and_never_cross_copies` (replacing `test_manifest.py:96-112`, preserving the no-cross-copy property verbatim); `test_knobs_echo_every_declared_lever` — every env var in the union of the registry knob maps has a `Knobs` attribute **and** appears in `KNOB_ENV_VARS`. — **M** — deps: W4, W6.

**W8 · `live_runner.py` registry-driven; slot-keyed artifacts; per-slot env** — **P2 blocker.**
`:51-55` four dicts → registry; `:387-439` ten `f"{lang}…"` filename sites → `slot_id`; `:230,264,269,271` `io/{lang}` → `io/{slot_id}`; `:285` `env_sut.extra_env.update(cell.extra_env)` → `{**cell.extra_env, **slot.slot_env, **cell.env_by_slot.get(slot_id, {})}`; `:293-297` stop **every** registered slot's service (the invariant — an idle sibling on the shared cpuset perturbs the headline — must become total, not two-named); `:317,320` `container_name(project)`; `:602-607` the create-only ladder's driving slot becomes a **declared, disclosed** registry field (with Hibernate vs EF emitting different INSERT shapes, PG's create ceiling is now a per-slot quantity, not a property of PG); `:672-673` `LEDGER_*_URL` from `slot.url_env`; `:687-693` `read_build` receives the **slot's** env, not the cell's.
*Test:* `test_two_slots_of_one_language_write_disjoint_artifacts` — run two `dotnet-*` slots through the artifact-path builder and assert zero path collisions (today they overwrite silently and the run looks valid). — **L** — deps: W4, W5, W6.

**W9 · `phases.py` slot-driven** — **P2 blocker.**
`:263-265` seeded Fisher-Yates over the slot set (for n=2 it must consume exactly one draw); `:268-284` `plan_blocks(…, slots)`; `:969-988` knob maps from the registry (`_INT_KNOBS` from `LanguageSpec.int_knobs` — a new int knob not listed silently lands as a string); `:1023-1052` `by_slot` rows and `expected_dl = slot.expected_data_layer` (**keep the `PhaseError` at `:1071-1077`** — it is the only guard that a cell env override actually reached the SUT); `:1036-1037` `gomaxprocs = SUT_CORES` → the registry parallelism knob, **keeping the `IS_LINUX and int(pc) != SUT_CORES` assertion at `:1063-1068`**; `:191` `Cell` gains `env_by_slot: dict[str, dict[str,str]]`; `:1441-1444,1607-1608` slot globs.
*Test:* a two-slot cell records `data_layer="efcore"` on one knob row and `"ado"` on the other and does **not** raise; a deliberately mis-wired fake (returns `efcore` for both) **does** raise naming the offending slot. — **L** — deps: W4, W5, W7.

**W10 · `analyze.py` / `paired.py` / `report.py` de-languaged; ratio direction fixed** — (pooled-knee removal is W24).
`analyze.py:80` `_LANGS` → the run's slot set; `:586` `_lang_label`'s bare `else` (which renders **any** unknown id as `.NET`) → registry lookup that raises; `:359-379,396-410,418-441,506-538` flatten `go_*`/`dotnet_*` **fields and fits.json keys** to `by_slot: dict[...]`; **fix the ratio direction** to numerator/`SlotSet.reference` (PROGRESS defines Java:.NET; the code computes `.NET : Go`); `paired.py:40-49,152-164` → `mean_a`/`mean_b` + a `labels` field and registry-supplied verdict tokens (so P2's efcore-vs-ado works unchanged); delete `report.py:132-151` `assemble_report` (a second, dead report assembler) and its test.
*Test:* run the whole `analyze_run` wiring suite parametrized over **both** slot sets and assert fits.json keys and report headers follow the registry with zero literal `"go"`/`"Go"` in either output; `test_lang_label_raises_on_unknown_slot`. — **L** — deps: W4, W5.

**W11 · compose topology: `sut-java`, shared env anchor, P2's second .NET slot** — **P2 blocker.**
`infra/compose.yaml:106-152` → `sut-java` (profile `java`, context `../services/java`, port `8081:8080`, volume `pdfstub-java`, `:254` volume decl); `:130-131` `GOGC`/`GOMEMLIMIT` → `JAVA_TOOL_OPTIONS: >- -XX:MaxRAMPercentage=${JVM_MAX_RAM_PERCENT:-25} ${JVM_GC_FLAGS:--XX:+UseG1GC}`; `compose.bench.yaml:42` → `JDK_JAVA_OPTIONS: -XX:ActiveProcessorCount=${SUT_CORES:-6}`. **Two different variables deliberately:** compose *replaces* a scalar env key across `-f` files and never concatenates, so a single shared variable split across base and overlay would lose the base half; and the `java` launcher *prepends* `JDK_JAVA_OPTIONS` to the command line, so the overlay correctly wins. **`JAVA_OPTS` is inert** under an `ENTRYPOINT ["java","-jar",…]` — nothing reads it, and a compose block full of it would look configured while measuring JVM defaults. Add `PORT` to **both** SUTs (`LedgerConfig.cs:61` reads it; compose sets it on neither). Hoist the shared env into `x-sut-env: &sut-env` so field-for-field parity is structural, not reviewed. Add `sut-dotnet-b` (profile `dotnet-b`, port `8083:8080`, volume `pdfstub-dotnet-b`, `DATA_LAYER: ${DATA_LAYER_B:-ado}`) plus its `compose.bench.yaml` cpuset/mem entry — a slot without the overlay entry runs **unpinned and unlimited**, a silent invalidation. Amend `compose.yaml:18` ("never co-resident") to "exactly one SUT profile per **measured** run" — `smoke.py:116` already violates the current wording.
*Test:* extend the compose cross-check (W4) with a **build-context-exists** assertion — `docker compose config` exits 0 on a nonexistent context, so nothing catches this class today. — **M** — deps: W4.

**W12 · Fixture conversion + the anti-sed guard.**
Convert `test_smoke_and_compose.py:29,45,46`, `test_live_runner.py:36,48`, `test_status_cli.py:32-33`, `test_ledger.py:41-58`, `test_cgroups.py:14,17,22`, `test_capacity_gate.py:347-362` (keep the absolute-URL *shape* assertion — it pins the 2026-08-14 root cause — take only the host from the registry), plus the `test_phases.py`/`test_analyze.py`/`test_manifest.py`/`test_warmup.py` sites.
*Test:* `test_no_language_literal_outside_the_registry` — walk `ledgerbench/*.py` with `ast` and assert no bare string constant equals a slot id or `"go"`/`"sut-go"` outside `registry.py`. **This single test is what prevents the sed-rename failure mode from re-entering.** — **M** — deps: W4-W11.

### Stage 2 — Tiers, plumbing, evidence

**W13 · Segmented warmup with early exit** — makes the already-landed `spec/slo.yaml` comment true; prerequisite for W14's budget.
`live_runner.py:514-561` — replace the single blocking attack (`:524` `duration_s=cap_s`, `:532` one `exec_in`, `:549` one evaluation) with K-second segments (≈30 s), evaluating `WarmupState.all_pass()` from segment 2 onward and stopping at the first passing boundary, with a floor (≈45 s) so the 30-sample flatness window has data. Decouple `_warmup_rate` (`:492-512`) from `cap_s`: it currently divides by `cap_s * 0.9`, so the landed 180→300 change **drops the warmup rate from 618 to 371 rps** (binding endpoint is `healthz`: 2000/0.02 = 100,000 requests) while leaving total warmup requests ≈111k either way — HotSpot sees the same invocation count. Derive the rate from a target warmup *duration* instead. Fix `:558` `under_warmed = under and elapsed >= cap_s`: when the vegeta exec fails or returns early (its `ProcResult` is **discarded** at `:532`), every gate fails, `elapsed < cap_s`, and the probe is recorded **warm**.
*Cost this closes:* today every probe pays the full cap unconditionally. At 300 s that is ≈282 probes/cell × 120 s ≈ **+9.4 h per cell**, ≈**+56 h** across two cells × three runs (D10) — matching PROGRESS's own "~8 h per cell" estimate, but paid on *every* probe rather than on the ones that need it.
*Test:* gates passing at segment 2 → `elapsed_seconds ≈ 2K`, not `cap_s`, with the segment count in the record; gates never passing → exactly `ceil(cap_s/K)` segments and `under_warmed=True`; `_warmup_rate` invariant to `hard_cap_seconds`; a failing warmup attack yields `under_warmed=True` (today: a warm probe). — **M** — deps: W8.

**W14 · B10: the `rehearsal` tier** — closes B10.
`phases.py:106-112` — replace if/if/raise with `_BUILDERS = {"full":…, "smoke":…, "rehearsal":…}` so the error message cannot drift from the supported set. Add `seed_scale: str` to `ScaleProfile` (defaulting to `scale`) used at `:474,498,772,1450` and `live_runner.py:255`, because `scale` is **not** a bench-only dial: it reaches the Go seeder (`seed/internal/gen/scale.go:22` errors on anything but full|smoke), `vegeta.py:63` `wl.seeding.scales[scale]` (KeyError), and `phases.py:1450` `.get(scale)` (silently records `None`). `rehearsal` sets **`seed_scale="full"`** — seeding at smoke scale makes every read cache-resident and invalidates every question G6 asks.
Field set: `functional_only=False` (the flag gates the CO/error exclusions at `:1135-1139` and both gate-enforcement paths — a `True` here makes rehearsal a slow smoke run); `warmup_cap_s = slo.warmup.hard_cap_seconds`; `measure_s = slo.ladder.window_seconds`; `warmup_discard_s = 30`; `warmup_min_calls_factor = 1.0`; `ladder_start_rps = 500`; **`ladder_step_factor = 2.0`** (the one deliberate deviation: six rungs at 1.2 reach only 1,244 rps, so with .NET's D4 knee near 10k **every rehearsal probe would be sub-knee and G6's "do both stacks cross?" would be answered *no* by construction* — the R1-A3 failure mode transplanted onto the ladder); `ladder_max_rungs=6`; `reps=2`; `confirm_blocks=2`; `soak_s=300`; `capacity_gate_rungs` raised so the create-only ladder can **break** rather than run out (see W26); one cell; `target_count` reduced.
CLI: `__main__.py:154,207,223,241,260` `--scale` → `Option(None, …)` validated in the CLI with `typer.BadParameter` (today `run-all --scale rehearsal` raises an **uncaught** `ValueError`, because `make_context` at `:164` sits outside the `try/except PhaseError` at `:169`); on `--resume` with `--scale` omitted, adopt the ledger's pinned scale (`ledger.py:152-162` otherwise refuses every rehearsal resume); prefix `_new_run_id` (`:68-71`) with the scale so `.gitignore:29`'s `results/smoke-*/` rule can match (it currently cannot, so smoke artifacts are committed and rehearsal artifacts would be too). `analyze.py:1975-1981` — narrow the bare `except Exception` that silently **drops the working-set bullet** on an unknown scale.
*Budget, honestly:* per probe ≈30 s churn + warmup + 30 s discard + 120 s measure + ≈40 s post-processing. With W13's early exit at a typical 90 s warm: ≈5.2 min × 28-34 probes = **2.4-2.9 h** — inside the declared window. *Without* W13, at a flat 300 s cap: ≈8.7 min × 28-34 = **4.0-4.9 h**. W13 is therefore not optional.
*Test:* `test_rehearsal_is_measurement_grade` — `functional_only is False`, `warmup_cap_s == slo.warmup.hard_cap_seconds`, `measure_s == slo.ladder.window_seconds`, `warmup_min_calls_factor == 1.0`, `seed_scale == "full"`, one cell; `test_rehearsal_ladder_can_reach_a_10k_knee` — the six rungs span ≥500..16000 (the anti-R1-A3 assertion); `test_unknown_scale_is_a_clean_cli_error` (exit 2, no traceback); `test_resume_adopts_the_pinned_scale`; `test_seed_scale_is_full_for_rehearsal`. — **M** — deps: W13.

**W15 · Typed `StallTest`; posture-capable `WarmupCfg`.**
`config.py:200-206` — replace the raw `stall_test: dict` with a typed model, which immediately surfaces that `phases.py:153` reads `stall_test["timeout_s"]`, **a key `spec/slo.yaml:82` has never defined** — so the vegeta attack timeout has always silently been the 5.0 fallback while appearing spec-controlled. Either add the key to spec or drop the pretence. `config.py:191-197` — add `gates_by_posture: dict[str, dict[str,bool]]` with `gates` as the default, plus a `gates_for(cell_id)` resolver (B3's precondition).
*Test:* `slo.co_validation.stall_test.freeze_seconds == 1.0` and no code reads a key the spec does not define; a posture override resolves, others fall back. — **S** — deps: none.

**W16 · B5: multi-cgroup sampling + the DB-CPU-per-request column** — closes B5.
`cgroups.py` — one `MultiSampler` thread reading all cgroups per 50 ms tick (today each `CgroupSampler` spawns its own thread at `:178` and **all** self-pin to the same housekeeping core at `live_runner.py:58-66`; four more samplers means eight threads contending one core); read `cpu.stat` **once** per sample (`:151,155,156` open and re-parse it three times); rename `WindowResult.container` → `label`; add `for_path(label, path)`; add `resolve_systemd_cgroup_path(unit)` for dockerd (a host systemd unit with no `docker inspect` PID — declare explicitly whether the sampled set is `docker.service` alone or plus `containerd.service`).
**Mandatory, and the reason this is not a two-line change:** `phases.py:1570-1575` **sums** `nr_throttled_end`/`throttled_usec_end` across windows and **maxes** `mem_peak`. That is sound only because the SUT container is force-recreated per probe (`live_runner.py:294-301`). postgres/redis/vegeta persist for the whole run, so adding them without start/end deltas multiplies a cumulative counter by the window count and publishes it as a measured pathology; `oom_events` has the same shape and the OOM abort (`live_runner.py:431-437`) would trip on history.
DB-CPU/req: read `cpu_mem_postgres.summary.json` at the **blockdir** level in `analyze._window_rows` (`:916-920`, before the per-slot loop at `:921`) and stamp it onto each slot's row; reuse `_cpu_ms_per_req` (`:957-966`) verbatim as the single declared aggregation rule. Attribution is clean with no de-interleaving because `_execute_block_probes` runs the slots **sequentially** within a block (`phases.py:1098-1113`). Record vegeta/dockerd under a separate `load_generator_overhead` section, explicitly **not** part of any per-request cost.
**Re-key the report row before implementing:** `analysis/report.py:58-66` `db_cpu_attribution_table` is written, tested and never called, and its contract is per-**endpoint** — a cgroup `cpu.stat` delta is a whole-container number. Either re-key to (cell, rate, slot) or source per-endpoint cost from `pg_stat_statements` deltas. Shipping a per-rate number under an endpoint key is a fresh instance of round 1's failure mode.
*Two known .NET-side DB-CPU sources to disclose in the methodology note, or the column attributes driver behaviour to language:* Npgsql issues `DISCARD ALL` on essentially every pooled checkout (measured in the .NET inventory's PG capture; PgJDBC/HikariCP do not), and raising `MAX_AUTO_PREPARE` above 0 under D4 switches it to a narrower reset — so D4 changes this cost in the same direction as the prepare change.
*Test:* `test_persistent_cgroup_counters_are_deltas_not_lifetime_totals` — two consecutive windows over a fake cgroup tree with monotonically increasing counters; the second window reports the increment, and `_finalize_manifest`'s sum over both equals the true total. Plus `test_db_cpu_per_request_is_request_weighted`, `test_db_cpu_absent_is_reported_not_fabricated` (null, never 0.0), `test_systemd_cgroup_path_returns_not_measured_when_unavailable`. — **L** — deps: W6, W8.

**W17 · B7: `/runtime-stats` per-window flush** — closes B7; **also unblocks B2**.
`live_runner.py:520-534` — the poller runs **only during warmup**; the measured window is not sampled at all, so B7 is "add sampling, then flush", not "flush what we poll". Start a second `StatsPoller` around the measured attack at the same boundaries as the cgroup sampler (`:361,366`), self-pinned to the housekeeping core (`runtimestats.py:130-136` has no pinning; `cgroups.py:9` is the pattern). Write `<slot>.runtime_stats.jsonl` (one object per poll: `phase`, `captured_utc`, `monotonic_s`, `window_offset_s`, the frozen stats object verbatim) plus `<slot>.runtime_stats.summary.json` carrying **start and end** snapshots — the counters are process-lifetime totals and the container is recreated per probe, so an end-only flush cannot yield a window delta. Record `n_polls`/`n_poll_errors`: `runtimestats.py:151-155` swallows every poll error with a bare `pass`, so an all-failed window is today indistinguishable from an unpolled one. **Additionally flush the warmup-window per-second p99/p999 series** (`vegeta.encode_per_second_quantiles`, consumed live at `live_runner.py:541-548` and never written) — it is the series the flatness gates actually test and B2 cannot be calibrated without it. `.gitignore`: add `results/**/*.jsonl` beside the `.csv` rule; commit the summaries.
*Disclosure to make:* 1 poll/s inside the measured window adds SUT-cgroup CPU that is not in the vegeta request denominator — ≈0.017% at ~1 ms/poll on a 120 s six-core window. State it; the alternative (boundary-only) loses the within-window heap trace that decides D6.
*Test:* N synthetic snapshots → N JSONL lines that re-parse with `extra` fields preserved; deltas equal end-minus-start; a zero-poll window yields `source: not_measured` with **nulls, never zeros**; a kill after k lines leaves k valid lines; the flushed paths appear in the checksum file. — **M** — deps: W8, W16.

**W18 · B6: host-state read-back** — closes B6.
New `bench/ledgerbench/hostread.py`, invoked from `run_doctor_phase` (`phases.py:362-380`, which runs unconditionally at **every** run start and **every** resume — exactly B6's cadence). Compare against a **declared target table in the read-back module**, never against `infra/host-setup.sh`'s state file: that file records *originals*, not targets, and `host-restore.sh:124` deletes it.
Attributes, with the read that evidences each (settings vs outcomes distinguished):
| # | attribute | read | note |
|---|---|---|---|
| 1 | governor | `/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` — record the **set** | also `scaling_driver`, `amd_pstate/status`, `energy_performance_preference` |
| 2 | boost | `cpufreq/boost` else `cpufreq/policy*/boost`; record which answered | outcome: sustained `scaling_cur_freq` + `thermal_throttle/core_throttle_count` deltas |
| 3 | C-states | `cpuidle/state*/{name,disable,latency}` — **not** by re-running `cpupower` | `host-setup.sh:197`'s comment says "index ≥ 2" but `-D` disables by *latency*; the read-back settles it |
| 4 | THP | bracketed token of `transparent_hugepage/enabled` **and `/defrag`** | `host-setup.sh:210` blames defrag and never reads it |
| 5 | IRQ | `/proc/irq/*/smp_affinity_list` for **every** IRQ | `default_smp_affinity` governs only *new* IRQs, so the current knob does not establish the FAIRNESS claim at all |
| 6 | daemon affinity | `Cpus_allowed_list` from `/proc/<pid>/status` for dockerd/containerd/journald | the drop-in does not retroactively move running units; dockerd is on the hot logging path |
| 7 | PGDATA | `lsattr -d` (`C` flag) + `findmnt -no OPTIONS` | record what it does and does not establish (chattr +C only takes on an empty dir) |
| 8 | services | `systemctl is-active` **and `is-enabled`** for tuned/ppd/irqbalance | an enabled-but-stopped unit returns after a reboot — the resume case |
| 9 | isolcpus | `/sys/devices/system/cpu/isolated`, `.../nohz_full` + `/proc/cmdline` verbatim | compare cpu **sets**, not strings (`host-setup.sh:361`'s `grep -qw` fails on `0-5` vs `0,1,…`) |
| 10 | container ground truth | `cpuset.cpus.effective`, `memory.max`, `memory.swap.current` from each container's cgroup | closes `compose.bench.yaml:26-28`; swap must be 0 or the anon measurement is poisoned |
| 11 | topology / PG | L3 `shared_cpu_list` (the CCD discriminator on Zen), `smt/control`, `SELECT name,setting,source FROM pg_settings` | turns `compose.bench.yaml:21`'s PLACEHOLDER core ids into an artifact; fills `manifest.postgres_config` |
Tier-1 mismatch = `PhaseError` (governor, boost, THP, daemon affinity, services, cpuset, memory.max, swap==0); Tier-2 = recorded, run continues (C-states where `cpupower` is absent, chattr, noatime, isolcpus, EPP) — mirroring the tier field `host-setup.sh:57` already emits, or a box without `cpupower` can never pass. Stamp `host_state.mismatches` **before** raising so the artifact survives the abort; accumulate readings across resumes the way `_record_git_identity` (`phases.py:1773-1793`) already does.
*Test:* against a **fixture sysfs tree** under `tmp_path` so the parser is testable on Windows with no root: a tuned fixture all-passes; one governor flipped → Tier-1 fail naming the path; missing cpuidle → Tier-2 skipped, not fail; THP bracket parser extracts `madvise` from `always [madvise] never`; IRQ check fails when any per-IRQ mask intersects `SUT_CPUS` **even though `default_smp_affinity` is correct**; `cpuset.cpus.effective` `0-4` vs expected `0-5` fails. Plus **invert** `test_manifest.py:195-206`, which today asserts the defect (`host_state`/`topology`/`postgres_config` are `not_measured`) and will otherwise make B6 look like a regression. Delete `docs/RUNBOOK.md:44`'s "registry" check (an over-declaration: `doctor.py` has no such check) and `:160`'s null-fields troubleshooting entry.
*Also in doctor while here:* host-RAM check (the resource ledger commits 26 GiB and `-XX:MaxRAMPercentage` reads the **cgroup** limit, not host free memory); build-context existence (the missing `services/go` proves the class is live and surfaces 30 min into a run); `_check_free_cores` (`doctor.py:91-99`) — the comment says 13 cores, the assertion says `n >= 8`, the documented cpusets need 16. — **L** — deps: W4, W6.

**W19 · B11: one checksum file that verifies** — closes B11.
Delete the append-mode writer at `live_runner.py:443-449` (mode `"a"`, six paths per probe; `reset_orphan_blocks` and `_retry_excluded_rungs` make re-runs **routine**, which is mechanically how round 1 got 108 double-hashed paths). New `bench/ledgerbench/checksums.py`: a **map, not a log** — recompute from scratch, sort by POSIX relative path split on `/` (reuse `config.py:271-276`'s exact ordering discipline so the file is byte-identical on Windows and Linux), emit `sha256sum`-compatible lines with `as_posix()` (never `str(Path)`), write through `paths.write_text_atomic` (it is currently the **only** writer in the codebase that bypasses it, so a crash mid-append truncates a line). Scope: derive the covered set from the repo's own `results/` gitignore rules so the checksum scope and the publication scope are the same object — round 1's file covered gitignored `.csv` traces and therefore could never verify against a published tree — and **include the roll-ups** (`ledger.json`, `manifest.json`, `doctor.json`, `capacity-gate/*.json`, `report/*`), which round 1 never covered. Call it from the terminal step of `run_all` **after** `run_host_restore_phase` (`phases.py:1878` — host_restore writes the ledger) and from the end of `analyze_run` (`analyze.py:2074`). Keep per-probe atomicity honestly: hash the six artifacts into the probe's **own** `probe.sha256` (atomic, one file per probe, nothing to double), and let the run-level file cover those. Add `ledgerbench verify <run_id>` with a non-zero exit — *a checksum file nobody can check is what round 1 shipped*. Raw traces get a separate `RELEASE.sha256` produced only at P10; during a run there is exactly **one** file and it verifies. Correct `README.md:246-247` (two file names, neither written by any code) and `docs/RUNBOOK.md:132-134`.
*Test:* `test_no_path_appears_twice_after_a_simulated_resume`; `test_verify_detects_a_tampered_artifact` (flip one byte → non-zero exit naming the path); `test_gitignored_traces_are_excluded`; `test_rollups_are_covered`; `test_paths_are_posix_on_every_platform`. — **M** — deps: W8.
*Amended in review:* “derive the covered set from the repo's own `results/` gitignore rules” was implemented as a `.gitignore`-to-regex translator in `checksums.py`, and that translator was wrong twice in three review rounds — silently, and in the same direction each time. It is deleted. Its replacement — a total tree walk minus `git check-ignore -v`'s output — was wrong a third time, and in the same direction again: a record in that output means "a rule matched", not "the path is ignored", so a file kept by a `!negation` was subtracted from a map git publishes it in. The scope is now `git ls-files --cached --others --exclude-standard` run inside the run directory, which IS tracked-plus-untracked-not-ignored by definition. There is no enumeration, no exclusion set and no rule read on this side, so negations, nested `.gitignore` files, `.git/info/exclude`, `core.excludesFile`, directory-only rules and dangling symlinks are all already resolved in the answer; a git that cannot answer raises rather than narrowing the scope quietly. `verify` compares the map against that scope in BOTH directions — a listed path git no longer publishes passes `sha256sum -c` in the working tree and fails it in a fresh clone, which is the one direction in which `verify` was weaker than the check it claims to dominate. One consequence taken deliberately: `results/smoke-*/` and `results/rehearsal-*/` exclude those run directories entirely, so those tiers publish nothing and carry no map; that is still probed by name (`git check-ignore -q` on the directory itself) because an empty scope alone cannot tell an excluded tier, which writes no map, from a published run that produced nothing, which raises.

### Stage 3 — Measurement semantics

**W20 · B8: per-probe target draw** — closes B8. Design in §3.3. — **L** — deps: W8, W9.
**W21 · B2: recalibrated flatness gate** — closes B2. Design in §3.2. — **L** — deps: W17 (warmup series), W20 (or the draw artifact is baked into the threshold), P2 corpus.
**W22 · B3: posture-specific gate sets** — closes B3.
`phases.py:188-194` — `Cell` gains `warmup_gates: dict[str,bool] | None` and a **required** `gate_set_rationale`. No `ProbeRunner` change is needed: `_execute_block_probes` already passes the whole `cell` into `run_probe` (`:1109-1113`) and the protocol already declares `cell: Cell` (`:315`); only `live_runner.run_probe` → `_warmup` needs to forward it, merging at `:517-519` (which already does exactly this for `per_endpoint_min_calls`). Add `STATUS_NOT_APPLICABLE` beside the existing `measured`/`not_implemented` (`warmup.py:56-58`), non-enforced, carrying the posture and knob that disabled it. **Refuse at plan time**: a cell that enables a gate the registry marks unsatisfiable under that posture raises `PhaseError` before any probe runs — R1-A3's ~30 wasted hours, prevented rather than narrated. Move the knowledge in `analyze.py:1427-1433` (a literal `("go","GOGC","off")` → prose table, the **only** place the codebase reasons about by-construction unsatisfiability, and it does so in the *report*) into that predicate; the report then reads the recorded status.
*Round-2 note:* R1-A3's instance was `GOGC=off`. The heap-equalized cell's ZGC **does** collect, so the round-2 risk moves to the **default** cell: G1 under `MaxRAMPercentage=25` expanding toward a pause target fails `gc_steady`'s committed-flat half by construction on exactly the cell carrying the headline. The measured spike is directional evidence both ways (G1 committed 70 MB vs ZGC 1,040 MB after churn at `-Xmx1g`) and is confounded by the tiny heap — the mechanism must exist regardless; G6 exercises it.
*Test:* a posture-declared override reaches the probe and is recorded per cell with its rationale; a gate disabled with an **empty** rationale fails validation; a cell enabling an unsatisfiable gate raises with `_any_cell_probe_done(ctx.led) is False`. — **M** — deps: W15, W4.
**W23 · B4: per-probe gate ledger; delete the two undeclarable gates** — closes B4.
Two-thirds already exists and must **not** be rebuilt: `IMPLEMENTED_GATES` (`warmup.py:54`), `not_implemented_gate` (`:166-183`, `passed=False` deliberately), `GateResult.enforced` (`:160-163`), `unimplemented_gates()` (`:277-283`) appended to every probe record (`:274`), `manifest.unimplemented_warmup_gates` (`phases.py:1836-1841`), and `analyze.py:1909-1950` which cross-checks the registry against the run's own per-probe records and prints `INCONSISTENT` when they disagree. **That bullet is the model for how B3 should report; do not rewrite it.**
The hole is in the ledger: `ProbeOutcome` (`phases.py:290-299`) carries only `under_warmed: bool` and `:1145` persists only that bool, so the ledger — the file `analyze` trusts — cannot say which gates were evaluated without opening 618 sidecars. Add `gates_evaluated`/`gates_failed` to `ProbeRecord` and `ProbeOutcome` (a pass-through; `WarmupResult` already computes both) and derive `under_warmed` from `bool(gates_failed)` so the bool and the list cannot disagree. Also add `cell_id`/`stage`/`block`/`gate_set_id`/thresholds to `WarmupResult` (today only the path encodes them), populate `GateResult.at_seconds` (declared, never written), and rename `WarmupResult.rate` → `warmup_rate` (nothing reads it, so it is free now and a silent mis-join later).
Then **delete both undeclared gates** from `spec/slo.yaml:43-44` — see §5.1.
*Test:* `test_probe_record_names_every_gate_evaluated`; `test_under_warmed_cannot_disagree_with_gates_failed`; and the governing rule mechanised: `assert set(load_slo().warmup.gates) - IMPLEMENTED_GATES == set()`, retargeted at a synthetic declared-but-unimplemented gate so the machinery stays covered. — **S** — deps: W22, W5.
**W24 · B1: per-language ladder, fit, confirm, soak** — closes B1/R1-A1/D9. Design in §3.1. — **XL** — deps: W5, W9, W10, W23.
**W25 · B1: the paired verdict under per-language rates** — closes R1-A5's structural cause. Design in §3.1.4. — **L** — deps: W24, W16.
**W26 · B9: two-stage capacity gate** — closes B9/R1-A6.
Stage 1 unchanged (`phases.py:749-887`), except that `projected_knee = ladder_start_rps * 8.0  # planning proxy` (`:808`) becomes a **named module constant with a recorded `projection_basis`** — at full scale that is 4,000 rps against cells that ran at 9,244-13,312 in round 1, and the 8× is currently invisible in every artifact. It lives in `phases.py`, not `spec/`, so changing it costs no `config_hash` churn.
Stage 2: `run_capacity_regate_phase(ctx, first_cell)` called from `run_cells_phase` (`:1489-1498`) after cell 1, added to `PHASE_ORDER` (`ledger.py:331-340`) so it is skip-if-done on resume and visible to `status`, routed through the existing `_enforce_gate` (`:647-660`) so failure blocks the remaining cells under the **same** `LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE` semantics. It reads `knee_by_slot` from cell 1's fit, takes the **max** across slots (PG serves whichever stack is resident), and uses the **measured** cache hit rate (`manifest.cache_stats`) in place of the assumed one at `capacity_gate.py:230`.
**The rule must change, and this is the crux.** `capacity_gate_rungs=12` (`phases.py:148`) with start 500 and step 1.2 caps the create-only ladder at 500×1.2¹¹ = **3,715.05 rps** — precisely round 1's published PG "knee" with `lower_bound: true` and "ladder never broke". PostgreSQL's ceiling has never been measured. A projection re-gate against a lower bound of 3,715 versus 5,000-7,200 rps of realized DB load **cannot pass**, and the operator sets the opt-out as routine, making the second gate as decorative as the first. So the re-gate uses the **utilization rule** W16 enables: PG's measured `cpu.stat` delta over cell 1's confirm windows ÷ (pg_cores × window_seconds) ≤ 1/headroom_factor (0.769 at 1.3). That is a direct measurement under the actual cells, needs no ladder, costs no runtime, and is a strictly stronger claim. Retain the projection rule only if `capacity_gate_rungs` is raised far enough that the ladder actually breaks (W14 raises it for rehearsal).
**No levers at the re-gate**: `evaluate_gate` fires levers until headroom holds; a lever fired *after* cell 1 changes the workload for cells 2..N while cell 1 already ran the old one. The re-gate calls `headroom_ok` / the utilization rule directly and never touches `LeverMachine`. A failure is a stop, not a lever.
New enum members on `capacity_gate.Verdict` (`:41-50`): `PASS_AT_REALIZED_RATES`, `DB_IMPOSED_AT_REALIZED_RATES`, `REGATE_NOT_EVALUABLE`. Add to the **enum**, not the string space — `phases.py:780` already leaks one non-enum verdict. Cell 1 cannot be un-run, so a failing re-gate **retroactively stamps** cell 1: a `CapacityGateStatus.regate` sub-model (`manifest.py:306-342`) carrying the verdict, realized rates by slot, measured PG utilization and `cells_measured_before_regate: [cell_id]`, which `analyze` carries into the report tables.
*Note the cost:* `_db_has_diverged` (`phases.py:432-445`) returns True as soon as the gate leaves PENDING, because the create-only ladder commits real invoices. A re-gate that re-runs the ladder permanently disables the exact-count seed verification. The utilization rule avoids that entirely.
*Test:* `test_regate_uses_realized_rates_and_the_measured_hit_rate` (a cell-1 knee well above the 8× proxy → the re-gate projects from the realized knee, not 4,000, and from the measured hit rate, not `spec/workload.yaml`'s 0.80); `test_regate_never_fires_a_lever` (monkeypatch `LeverMachine.try_next` to raise); `test_regate_failure_stops_before_cell_two`; `test_regate_failure_stamps_cell_one`; `test_regate_is_idempotent_on_resume`; `test_regate_not_evaluable_when_cell_one_did_not_cross`. — **L** — deps: W16, W24.

### Stage 4 — Spec edits and freeze readiness

**W27 · `spec/runtime-stats.md` JVM rewrite** — §4.1. — **M** — deps: W17.
**W28 · `spec/db-access.md` exec/prepare + round-trip ledger** — §4.3. — **M** — deps: W6, W7.
**W29 · `spec/slo.yaml` gate deletions, gc_steady semantics, relative band, cap comment** — §4.2. — **S** — deps: W13, W21, W23.
**W30 · `config.py` `extra="forbid"` on the SLO tree** — the governing rule as a load-time assertion.
`config.py:162,170,178,192,203,209,218,225` — flip `ignore` → `forbid`, declaring every key currently in `spec/slo.yaml`. The workload models are already mostly `forbid` (`:24,73,79,96,115,121,134`); the SLO tree is the outlier and is the mechanism that let round 1 declare a methodology it did not run. After the flip, adding a gate to `spec/` without a model field fails at load, and deleting one that code still reads fails at load.
**Land this LAST.** It is a one-way door mid-P1: any spec edit that adds a key without a field takes `doctor`, `status` and the whole test suite down at import.
*Test:* `test_slo_rejects_an_undeclared_key` (add `warmup.bogus_gate: true` to a tmp copy → `ValidationError`; today it loads silently). Audit the six parsed-and-never-consumed keys while here: `ladder.interleave/knee_fit/knee_ci` and `equivalence.decision_rule/inside_margin/outside_margin` are **descriptive pre-registration**, not gates, and are legitimate — keep, with a comment saying so. `soak.pass_criteria` (`:69-73`) is never evaluated by any code and *reads* like a gate: wire it or delete it. — **S** — deps: W27, W28, W29.
**W31 · Docs purge, link guard, CI floor re-derivation.**
Delete, do not update in place: `README.md:28-53` (the headline block), `:249-257`, `:276-293`, `:295-334`; `CONTRIBUTING.md:7-11,20-24`; `docs/METHODOLOGY.md:229-254` and every `[not captured]` paragraph; `docs/FAIRNESS.md:6-10,137-153,162-170`. Rewrite `METHODOLOGY.md` and `FAIRNESS.md` **from the section skeleton, not by editing** — round-1 numbers are woven into subordinate clauses, and the `[not captured]` convention *is* round 1's declare-then-disclose posture, which the governing rule inverts. Fix ~20 dangling links (`results/README.md` ×9, `docs/figures/*` ×4, `services/go` ×12). Note four of the five "descoped at scope freeze" rows are **restored** by PROGRESS (oha, logging-sink, connection sweep, DB-CPU column) and must not survive as standing declarations.
Re-derive the CI collected-test floor (`.github/workflows/ci.yml:31`, currently 340 against 344 — **four tests of slack**) as a deliberate act in the final P1 commit.
*Test:* a forbidden-token guard failing on the round-1 run id `20260814T051533-fedora`, `services/go`, `sut-go`, `GOGC`, `GOMAXPROCS`, `sqlc`, `descoped at scope freeze`, and the round-1 constants (358,673,386 / 10,426 / 3,715 / 59.5%) anywhere outside `PROGRESS.md`, `CLAUDE.md`, `docs/research/` and `seed/`. Plus a relative-link checker over `README.md`, `CONTRIBUTING.md`, `docs/*.md`. This is the mechanical half of CLAUDE.md's never-quote-round-1 rule. — **M** — deps: W1-W30.

---

## 3. The deep ones, designed

### 3.1 B1 — per-language knee fitting (R1-A1, D9)

#### 3.1.1 Where the defect actually lives

**Not in `analyze.py`.** `analyze._knee_for_lang` (`:675-731`) already refits isotonic per language over `_rungs_by_rate_for_lang` (`:112-129`), already drops rungs with no measurement-grade probe of that language, already reports `crossed=False` with `max_rung_rps` and "not extrapolated" when a language never reaches the SLO, already has a posture-only path, is already tested (`test_analyze.py:452-479`), and already publishes `knee_by_language` in `fits.json`. **D9's fit is written.** It drives nothing.

The defect is upstream, and it is one line:

```
phases.py:1194-1198
    by_rate, _ = _rung_values(rec)
    rungs_so_far = sorted((r, sum(v) / len(v)) for r, v in by_rate.items())
    _, crossed = _bracket_crossing(rungs_so_far, ctx.slo_ms)
    if crossed:
        break
```

`_rung_values` (`:1891-1915`) parses `r<rate>.b<block>.<lang>` and at `:1908` takes **only the rate**, discarding the slot. `sum(v)/len(v)` therefore averages both stacks together, and the ladder stops when the *mixed* mean crosses — i.e. when the faster-degrading stack drags the pooled mean over 20 ms. The other stack's ladder is truncated below its own knee and **the rungs above it were never measured**. Any plan that budgets B1 as an analyze change produces a per-language fit over truncated data and reproduces round 1's result with better labelling.

#### 3.1.2 The change

1. **Rung math per slot.** `_rung_values(rec, slot=None)` filters on `key.rsplit(".",1)[1]` (the key already carries it); `_rungs_by_rate`, `_collect_rungs`, `_top_rate` gain the same parameter. `_bracket_crossing` is pure and unchanged.
2. **One crossing predicate.** Four mutually inconsistent definitions of "crossed the SLO" are live today: the raw pooled rung mean (`_bracket_crossing:1954-1963`, persisted as `crossed`), the isotonic pooled fit (`knee.py:66-82`, which **overwrites** `KneeFit.crossed` at `analyze.py:661` after the ledger flag gated it at `:641/646`), the isotonic per-language fit (`:714/722`, which never consults the ledger flag), and `SloAtRate.meets_slo` (`:765`, mean of confirm-window p99 ≤ SLO — a different stage and the opposite inequality at the boundary). Collapse to **one**: make `knee._crossing_rate` the declared predicate and have `phases._bracket_crossing` call into it. Keep `meets_slo` as a separately-named quantity, and emit an explicit consistency callout when a slot is `meets_slo=yes` at a rate above its own fitted knee — today `_per_lang_knee_sentences` (`:1540-1586`) narrates that contradiction in prose instead of flagging it.
3. **`plan_blocks(…, slots)`.** With two slots it emits today's paired RCB block; with one it emits a **solo** block for rungs above the faster crosser's knee. Verified safe on resume: `reset_orphan_blocks` resets only when `0 < len(done) < len(members)` (`ledger.py:242`), false for a 1-member block, and `_execute_block_probes` handles a 1-element `slot_order` (`phases.py:1100-1101`). **But** the stage data must stamp `paired: false` on such blocks, or `analyze`'s pairing walks them expecting a partner.
4. **Ladder loop.** Per rung: compute `crossed[slot]` from that slot's completed rungs; `remaining = [s for s in slots if not crossed[s]]`; break when empty **or** at `ladder_max_rungs`; plan and execute only for `remaining`, with the already-planned guard keyed on `(slot, rate)` rather than `rate` (`:1199`).
5. **Declared rung ceiling + documented non-crossing.** `ladder_max_rungs=40` is a hard-coded literal at `phases.py:143`, invisible to `spec/` and to `config_hash`. Record in the ladder stage: `rung_ceiling_rps`, `crossed_by_slot: {"java": 8120, "dotnet": null}`, `ceiling_reached_without_crossing: ["dotnet"]`. At measured scale a non-crossing slot raises `PhaseError` naming the slot and the top rate, with an opt-out that stamps `degraded` exactly the way `ALLOW_DB_IMPOSED_KNEE` does (`phases.py:514,647-660,693-746` is the pattern to copy verbatim). **Round 1's failure was not that Go never crossed — it was that nothing in the artifact said so.** This field is that sentence, and P8's abort criterion becomes code rather than a checklist.
6. **Fit / confirm / soak.** `run_fit` (`:1222-1267`) fits once per slot into `knee_by_slot`/`confirm_rates_by_slot`; `run_confirm` (`:1270-1299`) and `run_soak` (`:1302-1335`) plan per-slot blocks at that slot's rates. Bump `LEDGER_SCHEMA_VERSION` — `bracket`/`crossed`/`rungs_summary`/`confirm_rates` change shape.
7. **Delete, do not port**, ~160 lines whose only purpose is to disclose the pooling: `_pooled_composition` (`analyze.py:191-224`), `_pooled_note` (`:589-624`), `_knee_for_cell` (`:627-672`), the pooled branch of `_knee_row` (`:1193-1200`), `_per_lang_knee_sentences` (`:1540-1586`), the pooled report prose (`:1597-1633`), and the two tests that pin it (`test_analyze.py:481-511,513-523`). It is careful, well-written code and it will be tempting to keep. A report that still explains what a pooled fit pooled, after there is no pooled fit, is worse than one that never had the section. `__main__.py:300-320`'s headline column `knee rps (pooled)` becomes per-slot columns; **the pooled column is the one that has to justify its existence, not the per-slot ones.**
8. **Roll-up filter per slot.** `_resource_cost_for_cell` (`:1083-1147`) applies `rate <= knee_rps` with the **pooled** knee to **both** slots at `:1107`, and `_window_rows` (`:914`) filters language-blind. Both take a per-slot filter.
9. **Two resume bugs fixed in the same change, or B1 reproduces them per slot:**
   - *Under-repped crossing rung.* On resume the crossing check runs **before** any rung executes (`:1194-1198`). A rung left partially complete by a crash is not "orphaned" (`reset_orphan_blocks` resets only single-done blocks), so its mean is computed over 4 probes instead of 6; if that partial mean crosses, `break` fires, the remaining probes stay PENDING, and the stage is marked DONE anyway at `:1215-1219`. `_rungs_by_rate` does not raise (the rung is not empty), so the fit proceeds on an under-repped bracket. **This is a plausible mechanism for R1-A4's "as few as one measurement-grade rep."** Fix: the crossing check considers only rungs where every planned probe is DONE.
   - *Stale stage data.* `reset_orphan_blocks` (`ledger.py:239-255`) flips the done member to PENDING but leaves `rec.data["p99_ms"][key]`, `["under_warmed"]`, `["p99_series"]`, `["excluded"]` in place — unlike `_reset_rung_probes` (`phases.py:1337-1354`), which pops all four. Self-healing only if the re-run happens; if the run aborts first, `_rung_values` fits on a p99 belonging to a probe the ledger records as PENDING.
10. **Single-rung crash.** If rung 0 already exceeds the SLO, `_bracket_crossing` returns `([rate0], True)`, the ladder breaks after one rung, and `estimate_knee_from_rungs` raises a bare `ValueError("need at least 2 rungs")` (`analysis/knee.py:133-134`) that escapes the CLI's `PhaseError`-only guard (`__main__.py:190-201`) as a traceback. P8 pre-registers exactly this case ("knee outside the measured ladder range → stop and fix `ladder_start_rps`"); make it a clean `PhaseError` naming `ladder_start_rps`. Live risk at rehearsal scale with a cold Spring Boot at 500 rps.
11. **Confirm at the bracket, not the knee — decide explicitly.** `_confirm_rates_from_bracket` (`:1974-1977`) returns the bracket rungs verbatim and ignores `knee_rps`; `_resource_cost_for_cell` then filters `rate <= knee_rps`, which admits only the **lower** bracket rung; `run_soak` independently takes `rates[0]`, the same sub-SLO rung. So the headline "cost at the rate where p99 crosses 20 ms" is actually measured one geometric step *below* the crossing. At full's step 1.2 that is within 20%; at rehearsal's 2.0 it is within 2×, which is not good enough. **Decide in P1:** either confirm at each slot's fitted knee, or state the interpolation rule in `METHODOLOGY.md`. Recommendation: confirm at the fitted knee for the primary verdict; the bracket rungs remain as the cost-vs-rate anchors.

#### 3.1.3 Terminology — fix before starting

"Pooled" means two unrelated things and one of them is in a **test name**. `test_phases.py:176` `test_pooled_p99_drives_crossing_not_per_second_series` defends *vegeta's within-probe report p99* against a percentile-of-percentiles — that must survive B1 untouched. `analyze`'s `knee_pooled` / `scope=="pooled"` / `"pooled (go+.NET)"` means *pooled across languages* — that is the defect. Anyone grepping "pooled" during B1 finds a passing test that looks like it protects what they are about to delete. **Rename the within-probe sense to `probe_p99` in W10, before W24 starts.**

#### 3.1.4 The pairing question — what happens to the RCB verdict

This is the hardest question in P1, so state the answer plainly.

**The blocking still works; the metric does not.**

RCB blocks the *nuisance* factor — host thermal state, page cache, PG state, wall-clock position — and treats the stack as the treatment. `slot_order_for_block` (`phases.py:263-265`) randomises which slot runs first, so unequal slot durations do not bias order in expectation. Pairing by block therefore still controls the nuisance **regardless of the rate each slot runs at**: the two probes are adjacent in time in a randomised order. What changes is the treatment definition — no longer "language at a fixed rate" but "language at its own SLO-crossing rate", which is *exactly* what the headline metric is defined as.

Two things break, one mechanically and one semantically.

**(i) Mechanically, it fails silently.** `_paired_p99_by_block` (`analyze.py:816-833`) keys `by_rate_block[rate][block][lang]` and requires both slots inside one `(rate, block)` cell at `:830`. With java@4,000 and dotnet@11,000 the both-present test never fires, `out[rate] = ([], [])`, `len(go) < 2` at `:853` drops every rate, and the report renders "Not available in this run" (`:1707-1710`). **No error, no warning.** R1-A5 already records paired verdicts in only 1 of 5 cells; this takes it to zero.

**(ii) Semantically, a paired p99 verdict becomes degenerate.** Both stacks sit at ≈20 ms *by construction* at their own knees. The paired difference is a guaranteed "tie" — an artefact of the design, not a result.

**Resolution:**

1. **Drop the rate from the pairing key.** Pair on `(cell, stage, block)`. `PairedVerdict.rate: int|None` (`:422`) and the `rate_rps` JSON key (`:441`) become `rates_by_slot: dict[str,int]`, so a verdict states its own operating points.
2. **Change the paired metric from p99 to cost.** The primary equivalence verdict becomes the paired difference in **CPU-ms per request**, with **anon-RSS per request** and **$/M** alongside. `paired.py`'s math needs **no change** — its own docstring (`:5`) already names "(knee RPS, CPU-ms/req, anon-RSS/req, $/M)" as the headline metrics; only `analyze._paired_for_cell` (`:837-867`) hard-codes `metric="p99_ms"`. The ±5% margin stays relative to `SlotSet.reference`.
3. **Publish p99 as a *conformance* statement, not a verdict.** Per slot at its confirm rate: mean confirm-window p99 and its distance from 20 ms. That is what the reader needs — evidence the promise was met — and it is not a comparison.
4. **Add a same-rate secondary verdict, and cost it now.** Per-request cost is **not** rate-independent (round 1 measured .NET CoV 19.3%, 0.648 → 0.308 CPU-ms/req across the ladder), so a paired cost comparison at java@4,000 vs dotnet@11,000 confounds *language* with *position on the cost-vs-rate curve*. Mitigation, in two parts:
   - Publish the **cost-vs-rate curve** as a first-class result with each slot's operating point marked (PROGRESS already commits to this). Extend `_cpu_vs_rate_for_cell` (`:968-1032`) beyond `("ladder",)` at `:981` so the confirm/soak points — where the knees actually are — appear on the curve at all, and separate the posture-only fallback values (`:1005-1007`) out of the CV.
   - Run a **common-rate paired verdict** at `r_common = min over slots of (each slot's confirm rate)`. Because the slower slot's own-knee probes *are* the common-rate probes, this adds only the **faster** slot at the slower slot's rate: `confirm_reps` blocks of one slot per cell (10 probes at full scale, 2 at rehearsal), not a doubling. Record `verdict_scope: own_knee | common_rate` on every verdict so the two can never be conflated.
   
   Both verdicts are legitimate and they answer different questions: *own_knee* is the headline the metric is defined as ("cost at a fixed latency promise"); *common_rate* is the confound-free contrast. Publishing only one would be a choice a reader cannot audit.
5. **Enforce a minimum block count.** `_paired_for_cell` today skips a rate at `len(go) < 2` with no floor against `ladder.confirm_reps` (10), so a 2-block verdict renders identically to a 10-block one apart from the `blocks (n)` column. Refuse or loudly label below the pre-registered minimum.
6. **Solo blocks never enter pairing.** Blocks stamped `paired: false` (§3.1.2 step 3) are excluded by construction, not by a lucky key miss.

*Test:* `test_paired_verdict_survives_per_language_confirm_rates` — a confirm stage with slot A at 4,000 and slot B at 11,000, 10 blocks each, produces a CPU-ms/req verdict with `n_blocks == 10` and `rates_by_slot == {A:4000, B:11000}`. **The regression guard is that today this fixture yields zero verdicts, silently.** Plus `test_paired_metric_is_cost_not_p99`, `test_common_rate_verdict_is_labelled_and_distinct`, `test_paired_verdict_refuses_under_min_blocks`, `test_solo_blocks_are_never_paired`.

### 3.2 B2 — recalibrating `p99_flat` / `p999_flat` (R1-A2)

#### 3.2.1 Why 59.5% is not a threshold problem

`warmup.no_positive_trend` (`warmup.py:109-124`) fails a gate on Mann-Kendall significance alone at α=0.05. Driven through the repo's own `analysis.mann_kendall` in simulation:

- On **i.i.d. noise** MK's false-positive rate is exactly α — 0.043/0.041/0.043/0.043/0.053 at n=5/15/30/60/120, invariant to noise scale. **White noise cannot produce 59.5% at any window size.**
- On a **drift-free AR(1) series** — which is what a per-second p99 series is (GC cycles, pool churn, PG checkpoints, autovacuum all induce second-to-second dependence) — FPR at n=30 is 0.095 (φ=0.3), **0.158** (φ=0.5), **0.231** (φ=0.7), **0.288** (φ=0.85), 0.361 (φ=0.95). It does **not shrink with n** (0.162/0.237/0.322 at n=120) because it is a bias in the tie-corrected `var(S)`, not a small-sample effect.
- Two rank-correlated gates give a per-probe flag rate of 0.29 (φ=0.5) → 0.51 (φ=0.85). A single +10% mid-window level shift flags 38%; +20% flags 82%.

**59.5% is fully explained by autocorrelation plus level shifts plus two correlated gates, with the SUT perfectly warm.** The language asymmetry (Go 67.3 / .NET 51.8) is what you expect when one stack's p99 is dominated by a large per-request constant that stabilises the series while the other's is dominated by tail events.

Two structural aggravations:
- **No practical-significance band.** The *analysis-stage* stationarity gate on the measured window fails only if MK is significant **AND** the Theil-Sen slope exceeds `min_slope_ms_per_min: 1.0` (`spec/slo.yaml:59`, wired at `phases.py:1980-1991`). The warmup gate has no band. So a series drifting 0.05 ms/min **fails** `p99_flat` at warmup and **passes** stationarity at analysis — same data, two verdicts. The band-less, stricter test gates the window that does not matter.
- **The corpus got harder, not easier.** `FLATNESS_TRAILING_SAMPLES` was widened 5 → 30 **after** the 59.5% was measured, and its own comment (`warmup.py:71-76`) says the widening makes the gates "materially HARDER to pass" and to expect *more* flagging. B2's starting point is worse than 59.5%.

#### 3.2.2 The design, in four steps

**Step 1 (P1, code, no thresholds).** Replace the boolean with `flatness_verdict(series, *, cfg) -> FlatnessResult` returning the whole statistic vector, not a bool: `n`, `samples_per_second`, `median`, MK `z/p/tau`, Theil-Sen slope (units/s), **relative slope `s_rel = slope*60/median` in %/min**, lag-1 autocorrelation, half-vs-half median ratio, verdict, and `status ∈ {measured, insufficient_data}`. Record it in `GateResult.detail`. Add a per-gate `enforce: bool` and ship the new statistic **observe-only**: recorded on every probe, enforcing nothing. *This single change is what makes recalibration possible at all — round 1 kept only pass/fail and therefore could not recalibrate.*

Also close two silent passes in the same change: empty latency series make both gates pass vacuously (`no_positive_trend` returns True for `len<3`, and `live_runner.py:547-548` leaves the series empty on an encode failure), and `gc_steady` passes vacuously on <3 poller snapshots. `status: insufficient_data` must flag the probe, never pass it.

**Step 2 (P2, data).** During the .NET-vs-.NET shakedown, per slot, run a long warmup (≈900 s at the warmup rate) with the per-second series **persisted** (W17). Null corpus = every non-overlapping 30-sample window beginning ≥600 s into the attack during which `gc.heap_committed` did not increase; target ≥200 null windows per slot per cell. Known-transient corpus = the first 30-sample window of each attack.

**Step 3 (P2/P6, calibration — derived, not chosen).**
- **Statistic: `s_rel`, relative Theil-Sen slope in %/min.** Scale-free, therefore symmetric between two stacks with different absolute p99 — the concrete fix for the language asymmetry, which an absolute ms/min band cannot have.
- **Threshold τ = the 95th percentile of `s_rel` over the null corpus, per language.** The false-positive rate is then 5% **by measurement on real autocorrelated series**, with no independence assumption anywhere — which is exactly where MK broke.
- **Power against a derived effect size.** δ = the drift over a 120 s measured window that would move the fitted knee by more than the bootstrap CI half-width, computed from the measured p99-vs-rate slope near the knee (`analysis/knee.py` already produces both). Require flag rate ≥ 0.9 at 2δ, and report the flag rate on the known-transient corpus. **If the null spread makes that impossible, the honest conclusion is that a 30-sample window cannot detect δ — lengthen the window or delete the gate, and publish that.**
- **Optional second condition** keeping MK as a shape test with a data-derived critical value: moving-block bootstrap over the null corpus (block length = the lag where ACF crosses 1/e) for the empirical 95th percentile of `z`; fail only if `s_rel > τ` **and** `z > z_crit`.
- Publish τ, `z_crit`, the realised null FPR and the power per language in `report/`.

**Step 4 (P7).** Only now does it enter `spec/`:
```yaml
warmup:
  flatness:
    statistic: theil_sen_relative
    window_samples: 30
    max_slope_pct_per_min: <τ>
    mk_z_critical: <z_crit>
    calibrated_from: <run_id>
    null_fpr: <x>
    power_at_2delta: <y>
```

**Retire `p999_flat` as an enforced gate.** `vegeta.py:481-486` uses nearest rank over a one-second bucket: `idx = min(n-1, int(q*(n-1)+0.5))`. At the warmup rate (618 rps at cap 180, **371 rps at the landed cap 300**) a bucket holds ~371-618 samples, so q=0.999 → the second-largest sample of that second, and at ≤500 samples/s **exactly the maximum**. `p999_flat` therefore reads "the per-second maximum latency has no upward trend": no stable location parameter, dominated by single events (one GC pause, one PG checkpoint fsync), and strongly rank-correlated with p99 — roughly doubling the false-flag rate for almost no independent evidence. Gate it on ≥1000 samples/s and return `insufficient_data` otherwise; at the warmup rate that means it never enforces, so **deletion is the honest form**. Keep it as a recorded statistic.

**Sequencing constraints (both hard):** calibrate only *after* B8 lands, or the ordered-replay artifact is baked into the frozen threshold; and answer the run-order DB-growth question first — the DB is reset only in the seed phase (`phases.py:492-494`), and every probe's warmup alone writes ≈16.7k invoices plus ≈92k line items at 15% of ~111k requests, so across a cell's ~84 probes millions of rows land on a 5M-row seed **while the ladder ascends monotonically**. That looks exactly like "flagging trips at high rates" and must not be absorbed into τ.

**A miscalibrated gate does not merely flag — it aborts.** `_retry_excluded_rungs` (`phases.py:1357-1387`) grants exactly one retry for a rung emptied by exclusion, then raises `PhaseError`. At round 1's Go rate of 67.3% per probe, a 2-reps-per-rung rehearsal rung (4 probes) empties with probability ≈0.67⁴ ≈ 20% per attempt. **B2 gates B10's usability**, not just its reporting.

**Two attributions to re-derive rather than inherit.** `docs/METHODOLOGY.md:94-98` blames `p99_flat`/`p999_flat` with no per-gate counts, and the warmup runs at a **fixed rate for every probe at every rung**, so flagging should be nearly rate-independent — in tension with the published story. A leading alternative: `_warmup_rate` did not exist in round 1 (`live_runner.py:499-501` says the floor preserves "the old `max(50, start/2)`" = 250 rps), which over 180 s delivers ≈45k requests against per-endpoint minimums needing ≈100k — so `per_endpoint_min_calls` would have failed **by construction** on a large share of round-1 probes. Rebuild the attribution from P2's per-gate counts.

**Also add the aggregate that does not exist.** Nothing in the repo computes an under-warmed *fraction* — per-probe flagging is everywhere (`analyze.py:99-109,148-178,326-334,463-466`), the aggregate is nowhere, and round 1's 59.5% was computed by hand outside the harness. Add a `WarmupCensus` per cell per slot across **all three stages** (no existing walk covers all three: `_RESOURCE_STAGES` is confirm+soak at `:78`, `_cpu_vs_rate_for_cell` is ladder-only at `:981`, `_STATIONARITY_STAGES` is ladder+soak at `:75`) with `n_probes`, `n_flagged`, `flagged_fraction`, `by_gate`, `by_reason`, published in `fits.json` and wired to P8's pre-registered ">25% → stop" abort — which today has nothing to read.

### 3.3 B8 — per-probe target draw (R1-A7)

#### 3.3.1 The defect is larger than round 1 disclosed

All three attacks in a probe pass `targets_in_container="targets.txt"`: warmup (`live_runner.py:528`), head-of-window discard (`:337`), measured (`:353`). vegeta reads a targets file **from the top on every invocation**. So the measured window consumes the first `rate × duration` targets, and the warmup that ran immediately before consumed a *prefix of that same sequence* — after a Redis `FLUSHALL` at `:312-315`. **The warmup does not merely leave a warm cache; it populates the cache with exactly the invoice ids the measured window is about to request.** Round 1 disclosed only the within-window wrap ("cycles every 75-108 s against a 300 s TTL"); the head-of-file prefix effect is strictly larger and was never disclosed.

Second, unmeasured effect: the bottom rung (500 rps × 120 s) consumes 60,000 of 1,000,000 targets — **6% of the file** — while the manifest's working-set counts are computed over the whole file (`manifest.py:484-553`), so the published `distinct_invoice_ids` overstates a bottom-rung probe's read working set by ≈16×. And `_record_replay_period` is called only from `run_confirm` (`:1298`) and `run_soak` (`:1332`), so ladder rungs — where most of the run's time is spent — have no replay-period record at all.

#### 3.3.2 The design

**(1) Split generation.** A shipped-once **body pool** (`generate_body_pool(wl, out_dir, *, seed, n_create, n_quote) -> BodyPool`) plus a per-probe **target draw** (`draw_targets(wl, *, out_path, count, rng, base_url, pool, customer_zipf, invoice_zipf) -> DrawnTargets`). The draw **streams** to an open file handle instead of accumulating `lines: list[str]` (`vegeta.py:283,333` holds ≈1.55M strings before a byte reaches disk) and returns `endpoint_counts` plus the distinct-id sets it already holds, so working-set accounting never re-reads body files.

**(2) Stop fsyncing bodies.** `paths.write_bytes_atomic` (`util/paths.py:67-81`) does mkstemp+write+flush+**fsync**+replace for every file; at full scale that is ≈350,000 fsyncs per slot per run for files that are regenerable and never read across a crash boundary. Plain buffered writes.

**(3) Seed derivation** uses the existing, unused stream selector: `PCG64.__init__(seed, inc)` (`util/pcg64.py:41-48`) takes a 128-bit odd increment selecting an independent stream from the same seed. Add `util/pcg64.py::stream_for(seed, *labels)` deriving `inc` as a BLAKE2b digest of the labels folded to 128 bits and OR'd with 1. **No spec change** — `spec/workload.yaml`'s `rng` block is inside `config_hash` and stays as-is.

```
stream_for(wl.rng.target_generator_seed_default,
           run_id, cell_id, stage, str(rate), str(block), phase)
```

Two label rules, both load-bearing:
- **`slot` is deliberately EXCLUDED.** Both slots of an RCB block must face the identical request sequence — that is the pairing contract (`live_runner.py:251-256` achieves it today by generating both files from one seed). Only `base_url` differs.
- **`phase ∈ {warmup, discard, measured}` is INCLUDED**, and the three attacks pass three different filenames. This is what stops the warmup pre-warming the measured ids.

**(4) Size so the stream never wraps.** `count = ceil(rate * duration_s * 1.02)`. *That*, not "draw per probe" alone, is what actually retires R1-A7. `ScaleProfile.target_count` (`phases.py:98,127-135`) becomes a documented **floor**, not the count.

**(5) Zipf tables injected, not rebuilt.** `generate_targets` already accepts `customer_zipf`/`invoice_zipf` (`vegeta.py:260-261,274-277`) and **no caller passes them**, so it rebuilds a 5,000,000-entry CDF (plus a second 5M `weights` list held simultaneously at `zipf.py:59`) on every call — ~300 MB peak and seconds of pure-Python work. Today that happens once or twice per run; under a per-probe draw it would happen 600+ times. Build both tables once on the runner.

**(6) Ship per probe.** `ensure_targets` (`:228-274`) splits into `ensure_body_pool` (once per run; bodies carry no URL, so the pool is slot-independent) and `ship_probe_targets` (a small file `docker cp`-ed per probe). Extend the `_shipped` key (`:141`) with the probe identity. Record the derived `inc` in the per-probe record so a reader can regenerate the exact sequence.

**(7) Manifest surgery.** `WorkingSet` (`manifest.py:378-418`) gains `draw_mode`, `requests_drawn_per_probe`, `distinct_invoice_ids_per_probe {min,median,max}`, `distinct_invoice_ids_union`, `body_pool_size_create/quote`, and the seed formula. `replay_period_seconds` is **deleted** along with `_record_replay_period` (`phases.py:1461-1467`) and its two call sites — nothing replays, and leaving the field is a fabricated disclosure. `working_set_from_targets` (`manifest.py:484-553`) and its serialised note (`:543-552`, which asserts "The targets file is replayed in order" **verbatim in every published manifest**) are replaced by accumulation from `DrawnTargets`. Four tests depend on the current behaviour (`test_manifest.py:142-192`). `analyze._scope_bullets:1998-2010` — the standing cache-residency caveat — is rewritten from the measured draw, or the report keeps confessing a fixed defect.

**(8) The disclosure this design costs.** Drawing a create body from a fixed pool changes the create-path `customer_id` distribution from "fresh Zipf draw per request" to "uniform draw over a Zipf-sampled pool" — identical on the head, thinner in the tail. Either disclose it in `docs/FAIRNESS.md` or take the exact-semantics fallback (regenerate only create bodies per probe: 15% of the budget). **The read path, which is what R1-A7 is about, is URL-only and unaffected either way.**

**(9) Cost, measured before declared.** At ~2 KB create bodies and 8 KB quote bodies, the current 1M-target set is ≈1.95 GB per slot. Per-probe target files are small (URLs), but generation throughput must be measured at P2 and the per-probe generation time recorded in the ledger, or B8 silently becomes the longest phase of the run.

**(10) `LEDGERBENCH_TARGET_COUNT` semantics.** It is pinned in the ledger (`ledger.py:83,142,163-168`) and replayed on resume (`phases.py:1821-1827`). Repurposing it as the body-pool size **without a ledger schema bump** makes an old ledger resume with the wrong meaning and swap the working set mid-run — the exact failure the pinning was added to prevent.

*Tests:* `test_warmup_draw_does_not_prewarm_the_measured_window` (Jaccard overlap of the two distinct-id sets ≈ the Zipf-implied overlap, materially below 1.0 — **the direct regression test**); `test_probe_draw_is_identical_for_both_slots` (same key, java vs dotnet → identical request lines modulo the base-URL prefix — the pairing contract); `test_same_probe_identity_reproduces_byte_identically` (the resume guarantee, replacing `test_vegeta.py:58-68` which pins the *opposite* contract); `test_probe_draw_differs_across_blocks_and_phases`; `test_no_wrap_within_a_probe`; `test_per_probe_draw_preserves_mix_and_zipf` **within** a single draw (`test_vegeta.py:32-39,108-118` today assert it across the whole file — this catches a draw that destroys the workload shape); `test_stream_for_inc_is_always_odd`; `test_zipf_tables_built_once`; `test_bodies_are_not_fsynced_individually`.

---

## 4. Spec edits

Governing rule for all four: **implementation and passing test first, spec text last.**

### 4.1 `spec/runtime-stats.md` — the JVM rewrite (prereq: W17)

Every string below was **measured on this box** (`docs/research/2026-08-21-measured.md`).

| line | current | proposed |
|---|---|---|
| `:18` | `"language": "go\|dotnet"` | `"language": "java\|dotnet"` |
| `:20` | `"data_layer": "sqlc \| efcore \| ado"` | `"data_layer": "jpa \| efcore \| ado"` — `ado` survives as the P2 shakedown slot, not as a published cell (D5) |
| `:26-27` | config echo: `gogc`, `gomemlimit`, `max_auto_prepare: 0`, `gc_dynamic_adaptation_mode` | `max_ram_percent`, `gc_engine`, `active_processor_count`, `jvm_options`, `max_auto_prepare: 20`, `auto_prepare_min_usages: 5`, `prepare_threshold: 5`, `prepared_statement_cache_queries: 256`, `gc_dynamic_adaptation_mode` |
| `:61-71` | field→source table, Go column | JVM column below |
| `:63` | "Go has one generation; documented" | per-collector bean mapping below |
| `:73-76` | PIN: ".NET = gen0-2 counts cadence + heap_committed flat; Go = collections cadence flat" | per-collector text agreeing with `warmup.py` and `spec/slo.yaml:44` |

JVM sources — **`java.lang.management` / `com.sun.management` MXBeans, never JFR** (JFR overhead lands inside the measurement window):

- `gc.collections` — `GarbageCollectorMXBean.getCollectionCount()`, with a **declared** bean→generation mapping. Measured bean names: G1 = `G1 Young Generation`, `G1 Concurrent GC`, `G1 Old Generation`; generational ZGC = `ZGC Minor Cycles`, `ZGC Minor Pauses`, `ZGC Major Cycles`, `ZGC Major Pauses`. **TRAP, measured:** under ZGC *Cycles* and *Pauses* are **separate beans over the same pools** — summing counts across all four double-counts collections, and summing `getCollectionTime()` across all four adds concurrent-cycle wall time to stop-the-world pause time. `gc.collections` reads **only `*Cycles`**; `gc.pause_total_seconds` reads **only `*Pauses`**. Under G1 every bean is a real collection and pause time is STW.
- `gc.heap_in_use_bytes` / `gc.heap_committed_bytes` — `MemoryMXBean.getHeapMemoryUsage()` (one call, correct under both collectors; per-pool iteration is unnecessary). This is the `gc_steady` input and the D6 input, so pin it explicitly.
- `gc.allocated_total_bytes` — `com.sun.management.ThreadMXBean.getTotalThreadAllocatedBytes()`. **Measured: it DOES count virtual threads on JDK 25** (164,442,688 virtual vs 164,442,440 platform after a ~160 MB allocation, identical to noise). This was the single biggest risk in the mapping and it is resolved.
- `gc.heap_goal_bytes` — **do not delete.** `HotSpotDiagnosticMXBean.getVMOption("SoftMaxHeapSize")` reads back with `origin=ERGONOMIC` and under ZGC is the collector's actual target. **Under G1 it equals `MaxHeapSize`, so report `null` under G1 rather than pretend.** (.NET writes null at `RuntimeStatsWriter.cs:55`.)
- `db_pool.*` — HikariCP's `HikariPoolMXBean`, which exposes `acquire_count`, `empty_acquire_count` and `acquire_wait_total_seconds` that Npgsql does not (`RuntimeStatsWriter.cs:64-66`). **The round-1 asymmetry inverts**: these three carry real values on Java and null on .NET. That belongs in `docs/FAIRNESS.md`, not only in a code comment.
- `redis_pool.*` — Lettuce. Decide explicitly whether `max` is the configured value or 0: `pools_warm` (`warmup.py:97-106`) auto-passes on `max == 0`, so a Lettuce multiplexer reporting 0 makes the Java side pass unconditionally while .NET must show `total >= 1`.
- `goroutines_or_threadpool` — rename, and document. **Measured:** `ThreadMXBean.getThreadCount()` returned 39 while 2,000 virtual threads had just run — it counts **platform threads only**. `jdk.management.VirtualThreadSchedulerMXBean` **exists on JDK 25** (`ObjectName jdk.management:type=VirtualThreadScheduler`) exposing `getParallelism()`, `getPoolSize()`, `getMountedVirtualThreadCount()`, `getQueuedVirtualThreadCount()`. **Recommendation:** report `getMountedVirtualThreadCount()` (the "threads doing work" number, the closest analog to .NET's `dotnet.thread_pool.thread.count`) and carry parallelism/poolSize/queued in the `config` echo, with the model difference stated.
- `config` echo — every value read back via `HotSpotDiagnosticMXBean.getVMOption(...)`, which carries an **`origin`** (`DEFAULT`/`ERGONOMIC`/`VM_CREATION`). Verified working for `MaxRAMPercentage`, `MaxHeapSize`, `ActiveProcessorCount`, `SoftMaxHeapSize`, `UseG1GC`, `UseZGC`, `MaxGCPauseMillis`, `CICompilerCount`, `UseContainerSupport`, `ParallelGCThreads`, `ConcGCThreads`, `UseCompactObjectHeaders`. **That origin field is exactly the evidence `docs/FAIRNESS.md`'s "is this a default?" column needs — it can be *read back*, not asserted.** Measured JDK 25 defaults: `MaxRAMPercentage=25.0 DEFAULT`, `ActiveProcessorCount=-1 DEFAULT`, `UseCompactObjectHeaders=false DEFAULT`, `UseContainerSupport=true DEFAULT`.
- `build.processor_count` must keep JVM semantics (`Runtime.availableProcessors()` under `-XX:ActiveProcessorCount`) — `phases.py:1063-1068` asserts it equals `SUT_CORES` and raises on Linux.
- `ts` — the same 6-digit formatter as the wire (see the gap below).

**Disclosure to add:** `ParallelGCThreads=6`, `ConcGCThreads=2`, `CICompilerCount=3` are JVM-internal threads **inside the SUT cgroup**, so their CPU lands in the measured CPU-ms. Correct — it is what the runtime costs — but .NET's equivalent (GC + tiered-JIT background compilation) is a different thread count, and it must be named in FAIRNESS.

**Gap to close in the same pass:** `test_equivalence.py:309-314` checks `/runtime-stats` with `shapecheck` **only** and never calls `normalize()`, while `shapes.json:38` types `ts` as `"str"`. So a Java service emitting `Instant.toString()` (variable precision, measured) passes the equivalence suite *here* while failing the same format everywhere else. Run `normalize()` over the runtime-stats body. Also: `gc`, `db_pool`, `redis_pool` and `config` are typed as bare `object` in `shapes.json:36-51`, so nothing constrains their contents — a JVM emitting `gc: {}` passes. Add a test that parses the fenced JSON block out of `spec/runtime-stats.md` and asserts the .NET writer's key set equals it, so the doc and the golden descriptor cannot drift.

### 4.2 `spec/slo.yaml`

**(a) `warmup.hard_cap_seconds` — landed, but its comment over-declares.** Current (uncommitted, `:28-33`):

> `# A CEILING, not a duration: warmup exits the moment the gates pass, so raising`
> `# this is free in the normal case and impossible after the P7 freeze.`

**That is false against `live_runner.py:514-561`**: one `AttackParams(duration_s=cap_s)`, one blocking `exec_in`, gates evaluated **once** at the end. Every probe pays the full cap. This is a governing-rule violation sitting in `spec/` right now — a declared behaviour with no implementation. Two ways out, and only one is right: **implement W13 (segmented early exit), making the sentence true.** Then 300 is genuinely free and the rehearsal tier fits its budget. If W13 slips, the comment must be amended to say the cap is a fixed duration and the +≈9.4 h/cell must be accepted explicitly.

Second consequence to fix in W13 regardless: `_warmup_rate` divides by `cap_s * 0.9` (`live_runner.py:512`), so the landed raise drops the warmup rate **618 → 371 rps** (binding endpoint `healthz`: 2000/0.02 = 100,000 requests) while total warmup requests stay ≈111k. HotSpot's tiered thresholds are **invocation counts**, so the JIT sees the same data; what 300 s buys is wall clock for the C2 queue to drain and for heap/pool steady state. If more JVM warmth is wanted, the effective knob is `per_endpoint_min_calls`, not the cap.

Also stale in the same neighbourhood: `warmup.py:66-68`'s comment says "hard-capped at 180 s and normally ENDS EARLIER, once the gates settle" — wrong on both counts today.

**(b) `:44` gc_steady comment.** Current: `gc_steady: true  # .NET: DATAS heap count + committed stable; Go: cycle cadence flat`. Proposed: per-collector text naming the measured bean sets — `.NET: DATAS heap count + committed stable; JVM: per-collector cycle cadence (G1: Young/Mixed/Old beans; generational ZGC: *Cycles beans only) + heap committed stable`. Prereq: W22 + the registry `GcSemantics`.

Note `_gen_cadence()` (`warmup.py:229-236`) **returns `_gc_cadence()` verbatim** — the ".NET generational" check is really the summed total-collection cadence, so the current text already describes something the code does not do. On G1 the approximation is worse (young-only and mixed cadences move independently), which is why the registry needs a per-collector record rather than a function-name suffix.

**(c) `:43-44` delete the two undeclarable gates** — see §5.1. Prereq: W23 + the passing `set(gates) - IMPLEMENTED_GATES == set()` test.

**(d) `:55-59` `stationarity.min_slope_ms_per_min: 1.0` → a relative band.** *A fourth spec edit that P1's list does not name.* An absolute ms/min band is **language-asymmetric by construction**: the same relative drift trips whichever stack has the higher absolute p99 sooner. Replace with `min_slope_pct_per_min`, the same scale-free statistic B2 adopts, so the warmup gate and the analysis gate finally agree. Prereq: W21.

**(e) `ladder.rung_ceiling`** — declare `ladder_max_rungs`, today a hard-coded literal at `phases.py:143`, invisible to `config_hash`. Prereq: W24.

**(f) `co_validation.stall_test.timeout_s`** — add it, or stop reading it (`phases.py:153`). Prereq: W15.

### 4.3 `spec/db-access.md`

**(a) `:107-117` exec/prepare table** — pgx column → PgJDBC; .NET headline → D4. **Specify both axes**, not one: threshold-before-prepare is PgJDBC `prepareThreshold=5` vs Npgsql `Auto Prepare Min Usages=5` (already equal — pin both explicitly); cache size is PgJDBC `preparedStatementCacheQueries=256` (plus `preparedStatementCacheSizeMiB=5`) vs Npgsql `Max Auto Prepare 0 → 20`. Ledgerline issues ~7 distinct statements, so both ceilings are non-binding and the residual 256-vs-20 asymmetry is inert **here** — state that, do not leave it implicit; an unstated inert dial is exactly what P7's reviewer is briefed to find. Delete the now-empty "prepared parity variant" column rather than leaving it declared (D4 makes it vary nothing). Prereq: W6 + W7 (`auto_prepare_min_usages` and `prepare_threshold` must be settable, scrubbed and recorded first — declaring the table while the manifest cannot evidence half of it is over-declaration in the exact round-1 sense).

**(b) `:72-98` the create-path round-trip ledger — the pin cannot hold.** Current text claims round-trip counts are "ASSERTED EQUAL at integration". **Measured** (`docs/research/2026-08-21-measured.md`): `GenerationType.IDENTITY` disables Hibernate JDBC batching in 7.4.5 — a controlled experiment at `hibernate.jdbc.batch_size=8` gave 6 assigned-id rows → **1** prepared statement, 6 IDENTITY rows → **6**. `spec/schema.sql` pins `invoice_items.id GENERATED ALWAYS AS IDENTITY`, so this is not avoidable inside JPA. With 3-8 items per invoice and `create_invoice` at 15% of the mix, **Java pays 3-8 round trips where EF Core pays 1.** Rewrite the ledger to the real per-stack counts, add the row to `docs/FAIRNESS.md`. **Option A (accept and disclose) is the recommendation** — the headline is ORM vs ORM and this *is* what the ORM costs. Dropping to `JdbcTemplate.batchUpdate` recreates exactly the round-1 asymmetry FAIRNESS had to disclose for Go ("EF Core against hand-written pgx"); changing the schema is forbidden.

**(c) Correct two false claims about the .NET side while here.** `:81` says EF's Q7 uses "the same EXISTS statement shape as Go and raw ADO.NET" — captured wire text is `SELECT EXISTS ( SELECT 1 FROM customers AS c WHERE c.id = $1)` (aliased, qualified, embedded newlines, no output alias) against the canonical `SELECT EXISTS(SELECT 1 FROM customers WHERE id = $1) AS exists`. `:88-89` "the DB-side statement set is identical" is an overclaim. And `:8` says both EF deviations are "asserted by EfQueryShapeTests" — `EfQueryShapeTests.cs` has three facts and asserts **nothing** about Q1's literal `LIMIT 1`. Three further captured EF deviations are undisclosed: alphabetical INSERT column order; the aliased/reversed Q6; and **Q3's statement text varies with the page number** (`LIMIT $2 OFFSET $3` at page 1, `LIMIT $2 OFFSET $2` at page 2 where offset equals limit and EF/Npgsql collapse two equal-valued parameters). `spec/workload.yaml:38` weights page 2 at 0.15 of a 25%-of-mix endpoint, so **≈3.75% of all measured requests execute a second statement text** — two `pg_stat_statements` queryids and, under D4, two server-side prepared statements for one logical query. P5's "prepared-statement counts on the server" check will flag it with no explanation available unless it is disclosed in P1. Hibernate adds a **third** text (`offset ? rows fetch first ? rows only`, measured).

**(d) The create-path has no SQL-shape test at all on the .NET side.** `EfQueryShapeTests` covers reads only (`ToQueryString()` cannot express a write). Add `EfWriteShapeTests.cs` capturing the four-command sequence via a `DbCommandInterceptor` (verified working against a live PG), gated on a connection-string env var so it skips cleanly and runs under P2 which has a live DB by construction. Without it, P4 diffs Hibernate against nothing.

### 4.4 `spec/openapi.yaml`

`:16` `description: Small invoicing/billing API used as a Go vs .NET benchmark workload.` → `… Java vs .NET benchmark workload.` Trivial, but inside `spec/`, so it moves `config_hash` and must land in P1. Also `:5` and `:163-172`: the timestamp trap prose ("Go must not trim trailing zeros") becomes the JVM's, and the frozen errors-map **key format** (PascalCase, `.NET 10 AddValidation()`'s native emission) needs the note that Jakarta Bean Validation produces `items[0].sku` and Java must override the derivation — a P4 blocker discovered at P1 costs nothing.

---

## 5. Struck from the plan — argued

Round 1 died of over-declaration. Deleting is a valid outcome.

**5.1 `hot_set_touched` and `major_faults_asserted_equal` — DELETE both** (`spec/slo.yaml:43-44`).
`major_faults_asserted_equal` is **not per-probe evaluable as written**: "equal" is a cross-language statement and the two slots are separate probes paired only by RCB block. `WarmupResult.major_faults` (`warmup.py:196-200`) already records the pgmajfault delta as an explicit disclosure with a comment saying it does not stand in for the gate — that is the honest version, and asserting cross-runtime *equality* of page faults under different heap ceilings (D6) is not a well-formed claim anyway.
`hot_set_touched` has no implementation and no upstream machinery: `spec/workload.yaml:2` names a "seeder hot-set warm-touch" that does not exist in `seed/`, and `spec/cache.md:54-57` defines the 0.80 hit rate **by construction** (Zipf + TTL), not by a touch pass. It is tempting to say B8 rescues it — with a per-probe draw the drawn id set *is* the hot set — but with independent warmup and measured streams the Zipf head appears in any large draw **by construction**, so the gate would be trivially true and carry no information. If the intent is to be preserved it should be a differently-named `cache_hit_rate_reached` measured from Redis `INFO` at the warmup boundaries (the mechanism already exists at `live_runner.py:359,367,99-118`) — a *cache* gate, not a page-cache gate, and not under the old name.
**Keep the entire not-implemented machinery** (`warmup.py:54,160-183,277-283`; `analyze.py:1909-1950`) — it costs nothing and protects any future gate. Retarget its three tests (`test_warmup.py:148-171`) at a synthetic declared-but-unimplemented gate so the coverage survives the deletion.

**5.2 `p999_flat` as an *enforced* gate — STRIKE; keep the statistic.** §3.2. At the warmup rate nearest-rank q=0.999 is the second-largest sample of the second (the maximum at ≤500 rps): no stable location parameter, dominated by single events, rank-correlated with p99, roughly doubling the false-flag rate for almost no independent evidence.

**5.3 The pooled knee and its ~160 lines of disclosure prose — STRIKE.** §3.1.2 step 7. Including the two tests that pin it.

**5.4 `variant.prepared-parity` — DELETE** (`phases.py:218-238`). D4 makes `MAX_AUTO_PREPARE=20` the default, so the cell varies nothing. **`variant.ado` and `variant.dapper` survive until G2** — P2's shakedown *is* those `DATA_LAYER` values — then delete at P7 per D5. Deleting them in P1 removes P2's mechanism.

**5.5 `analysis/report.py`'s dead public API — wire or delete, do not leave both.** `headline_ratio_table`, `db_cpu_attribution_table`, `gc_pool_table`, `cost_table`/`cost_per_million`/`CostInputs`, and `assemble_report` (`:132-151`, a **second** report assembler) are complete, tested, and called by nothing outside their own tests — `analyze.py` imports only the *private* `_df_to_md` (`:1179`), which is the tell that the public API was abandoned. This is the inverse hazard: working code a reader could mistake for a delivered feature. Wire `db_cpu_attribution_table` (re-keyed per W16) and `cost_table` (PROGRESS commits to publishing the $/M figure); **delete `assemble_report` and `headline_ratio_table`.** Re-contract `gc_pool_table`'s `gen0/gen1/gen2` shape — it is CLR-native and does not survive G1/ZGC, where the natural form is a variable-length per-collector list.

**5.6 `bench/figures.py` — STRIKE from P1; defer to P9.** `RUN = ROOT/"results"/"20260814T051533-fedora"` (`:33`) and `FITS = json.loads(...)` at **import time** (`:65-66`); `results/` does not exist, so `make figures` raises today. It hard-codes round-1 findings as chart text (`:262-263` "Go never crossed 20 ms anywhere in this run"), which CLAUDE.md forbids quoting, and `HEADLINE_CELL = "variant.prepared-parity"` (`:35`) names a cell D4 deletes. It is also the only consumer of the `fits.json` wire keys (`:187-199,287-296`) and fails with a **KeyError, not a type error**, on any rename. **In P1 do exactly two things:** make `RUN` a parameter so importing the module reads no filesystem, and delete the round-1 chart text. Rebuild the figures at P9 against round-2 artifacts.

**5.7 Not P1 work, and must not enter `spec/` during P1.** PROGRESS restores four items round 1 descoped — oha tail cross-check, logging-sink microbench + no-drop test, a reduced connection sweep, and the DB-CPU column (which *is* W16). Only the DB-CPU column is a P1 item. The other three are P5/P6 work; declaring them in `spec/` now would be round 1's failure in a new costume. Delete the "descoped at scope freeze" rows from `docs/FAIRNESS.md:145-149` and `docs/METHODOLOGY.md:241` rather than leaving them as standing declarations.

**5.8 The two-file checksum scheme in `README.md:246-247` — STRIKE.** It documents `CHECKSUMS.committed.sha256` and `CHECKSUMS.run.sha256`, **neither of which any code writes** (the code writes `CHECKSUMS.sha256`), and tells the reader which one *not* to verify against. That came from a round-1 manual publication step that does not exist here. Anyone implementing B11 from the README builds the wrong thing. One run-time file that verifies (W19); a separate `RELEASE.sha256` only at P10.

**5.9 `soak.pass_criteria` (`spec/slo.yaml:69-73`) — wire or delete.** Four sub-keys evaluated by no code, and they read like gates. The other inert keys (`ladder.interleave/knee_fit/knee_ci`, `equivalence.decision_rule/inside_margin/outside_margin`) are legitimate **descriptive pre-registration of method** — the governing rule constrains declared *gates* — and should be kept with a comment saying so. `soak.pass_criteria` is not in that category.

**5.10 `SUT_MEM` in `KNOB_ENV_VARS` (`compose.py:111`) — STRIKE.** A leftover from the descoped memory-pressure sweep. It is a compose **topology** knob (`mem_limit: ${SUT_MEM:-12g}`), and scrubbing it silently ignores an operator's export — inconsistent with the `:113-115` comment, which explicitly declares SUT_CPUS/SUT_CORES topology knobs as *not* scrubbed.

**5.11 `gc.heap_goal_bytes` — do NOT strike.** One inventory recommended deleting it as null-on-both-sides. The measured spike found `SoftMaxHeapSize` reads back with `origin=ERGONOMIC` and *is* ZGC's target. Map it; report `null` under G1. Recorded here so the recommendation is not re-applied.

**5.12 `clients.both_services_configured()` (`bench/validation/clients.py:58-59`), the unused `services` fixture (`validation/tests/conftest.py:18-24`), `normalize.deep_equal` (`:168-170`) — DELETE.** All three are dead repo-wide; the first hard-codes `== 2`, and the second is a *second* SUT-discovery path (`test_equivalence.py:55-59` uses its own `_services_or_skip`). Removing them shrinks the registry's surface before the Java work starts. Note the equivalence suite's cross-compare (`_assert_cross_equal:66-74`) is **already N-slot safe** — it comprehends over `configured_services()` and compares `names[0]` against every other. Only `configured_services()`'s hard-coded pair and `_call_both`'s name are two-valued.

**5.13 `normalize.py:157-163` — DELETE the unreachable branch.** A comment claims a "defensive global no-float-money guard: a float ANYWHERE flags a likely money-path bug even if the key substring heuristic missed it", then guards the raise with `is_money_key(_key)` — every such key was already handled at `:148-155`. The stated protection does not exist. Small, but it is a comment promising a guard the code does not implement, in the file whose entire job is cross-stack honesty, and P7's reviewer is briefed to find exactly this.

---

## 6. Current test / lint baseline — what "green" means today

Measured on this box, 2026-08-21, at `ecc0476` + the uncommitted O4/D11/research changes.

| check | command | result |
|---|---|---|
| bench tests | `cd bench && uv run pytest -q` | **324 passed, 20 skipped, 8.10 s** — exit 0 |
| bench collected | `uv run pytest -q -m "not docker" --collect-only` | **344 collected** |
| bench lint | `cd bench && uv run ruff check .` | **All checks passed!** — exit 0 |
| CI floor | `.github/workflows/ci.yml:31` | `-lt 340` → **4 tests of slack** |
| .NET build | `cd services/dotnet && dotnet build -c Release` | 0 warnings, 0 errors (SDK 10.0.110) |
| .NET tests | `dotnet test Ledgerline.Tests -c Release` | **87 passed, 0 failed, 458 ms** (no live infra: `TestEnv.cs:12-18`) |
| **.NET format** | `dotnet format --verify-no-changes` | ❌ **exit 2, 1,697 `WHITESPACE` errors across 41 files** |
| **CI `go` job** | `.github/workflows/ci.yml:41-51` | ❌ fails at `setup-go` — `services/go/go.mod` does not exist |
| **`make lint` / `make fmt` / `make smoke`** | `Makefile:68,63,~20` | ❌ all reference `services/go` / the dead `sut-go` build context |

The 20 skips: 19 live-equivalence tests (`LEDGER_GO_URL`/`LEDGER_DOTNET_URL` unset — so **the entire wire-parity suite is invisible in CI and in a default local run**) plus one non-Linux cgroup no-op.

Per-file collected counts, so a deletion during the refactor is a deliberate act: `test_phases.py` 82, `test_capacity_gate.py` 26, `test_analyze.py` 26, `test_equivalence.py` 19 (all skipped), `test_manifest.py` 17, `test_vegeta.py` 16, `test_normalize.py` 14, `test_warmup.py` 14, `test_mann_kendall.py` 14, `test_golden_self_consistency.py` 12, `test_shapecheck.py` 10, `test_cli_help.py` 10, `test_paired.py` 9, `test_ledger.py` 8, `test_pava.py` 8, `test_zipf.py` 7, `test_smoke_and_compose.py` 7, `test_pcg64.py` 7, `test_config.py` 7, `test_knee.py` 7, `test_live_runner.py` 6, `test_util.py` 5, `test_cgroups.py` 5, `test_status_cli.py` 4, `test_report.py` 4.

**Three passing tests actively pin defects P1 must remove.** Each will fail *correctly* when its fix lands and must be **inverted**, not patched or deleted:
- `test_manifest.py:195-206` asserts `host_state`/`topology`/`postgres_config` are `not_measured` (W18).
- `test_vegeta.py:58-68` asserts one seed → one byte-identical targets file (W20).
- `test_phases.py:1225-1226` asserts `ws.replay_period_seconds["100"] == 2.0` against a 300 s TTL — R1-A7 in one number (W20).

---

## 7. P1 exit criteria (gate G1, made checkable)

1. `cd bench && uv run pytest -q` green and `uv run ruff check .` clean, with the collected-test floor re-derived and raised in the same commit.
2. `cd services/dotnet && dotnet format --verify-no-changes` exits 0 with an empty working tree.
3. `make lint` and CI's `seed (go)`, `dotnet` and `java` jobs all pass.
4. `set(load_slo().warmup.gates) - warmup.IMPLEMENTED_GATES == set()` — the governing rule as an assertion.
5. `config.py` SLO models are `extra="forbid"` and `spec/` loads.
6. No literal slot id or `"go"` outside `registry.py` **within `bench/ledgerbench/**`** (the `ast` walk's real scope — `bench/figures.py` and `bench/validation/tests/test_shapecheck.py` legitimately carry slot ids); no round-1 constant **on the prose surface** outside `PROGRESS.md`/`CLAUDE.md`/`docs/research/`/`seed/` (the guard's real scope, argued in its own module docstring); every relative doc link resolves. **Widen this wording, never the guards.**
7. Every Workstream-B item is either closed with a passing test, **or explicitly struck in §5, or explicitly sequenced past P1 with the phase that owns it named**. B2 is the third case: §5.2 strikes `p999_flat` as an enforced gate (executed), while §3.2 puts the flatness calibration in P2/P6/P7 because a threshold is a measured finding and cannot be pre-registered honestly here.
8. `SLOT_SETS["shakedown"]` resolves to two distinct compose services, ports, volumes and artifact prefixes — **P2 is unblocked before P2 begins.**