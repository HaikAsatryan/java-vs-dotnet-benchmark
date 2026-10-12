"""`spec/runtime-stats.md` <-> the .NET writer <-> the shape gate, pinned together.

The equivalence suite checked `/runtime-stats` with `shapecheck` ONLY and never
called `normalize()`, while `shapes.json` types `ts` as a bare `str`. A service
emitting `Instant.toString()` -- variable precision, which is what both
`Instant` and `OffsetDateTime` do -- therefore passed the equivalence suite HERE
while failing the same frozen format everywhere else. That gap is closed by
running `normalize()` over the body, and the two tests at the bottom are what
make that a checked claim rather than a call site nobody exercises: the live
equivalence test skips without both SUTs up, so the enforcement is proven here,
offline, against a body of the shape the .NET writer actually emits.

The first test is the drift guard. `spec/runtime-stats.md` freezes at P7 and is
hashed into `config_hash`; the writer is C# in another tree. Nothing but this
test connects them. A stale field table is a declaration nothing implements,
the same defect class as round 1's unimplemented warmup gates (R1-B1).
"""

from __future__ import annotations

import json
import re

import pytest

from ledgerbench.util import paths
from validation import shapecheck
from validation.normalize import NormalizationError, normalize

# Utf8JsonWriter calls in the order they execute. `WriteStartObject()` with no
# argument opens the root; every other form names the key it writes.
_WRITE_CALL = re.compile(
    r"""w\.Write(StartObject|EndObject|String|Number|Null|Boolean)\(\s*(?:"([^"]*)")?"""
)


def _writer_keys() -> dict:
    """The key tree `services/dotnet/Observability/RuntimeStatsWriter.cs` writes.

    Read from the source rather than from a captured body: capturing one needs
    the .NET SDK, which pytest does not have, and a committed capture would be a
    third artifact free to drift from the other two.
    """
    src = (paths.repo_root() / "services" / "dotnet" / "Observability"
           / "RuntimeStatsWriter.cs").read_text(encoding="utf-8")
    root: dict = {}
    stack = [root]
    for call, key in _WRITE_CALL.findall(src):
        if call == "EndObject":
            stack.pop()
            continue
        if call == "StartObject":
            if not key:                      # the root object; re-push so its
                stack.append(stack[-1])      # WriteEndObject balances
                continue
            child: dict = {}
            stack[-1][key] = child
            stack.append(child)
            continue
        stack[-1][key] = None
    assert stack == [root], "unbalanced WriteStartObject/WriteEndObject"
    return root


def _spec_body() -> dict:
    """The single fenced JSON block of spec/runtime-stats.md."""
    text = (paths.spec_dir() / "runtime-stats.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, flags=re.DOTALL)
    assert len(blocks) == 1, f"expected one fenced json block, found {len(blocks)}"
    return json.loads(blocks[0])


def _key_tree(value: dict) -> dict:
    return {
        k: _key_tree(v) if isinstance(v, dict) else None
        for k, v in value.items()
    }


def test_the_spec_shape_block_is_exactly_what_the_dotnet_writer_emits():
    """The required key set has one definition, and it is checkable.

    `endpoint_calls` is in scope deliberately: its keys are frozen ROUTE
    TEMPLATES, and a route renamed in the writer without the spec would silently
    zero the per-endpoint warmup gate for that endpoint rather than fail it.
    """
    assert _key_tree(_spec_body()) == _writer_keys()


def test_the_spec_shape_block_satisfies_the_golden_shape_descriptor():
    """...and the descriptor the live gate runs is the same contract again."""
    assert shapecheck.check(_spec_body(), "RuntimeStats") == []


def test_the_runtime_stats_timestamp_is_held_to_the_frozen_wire_format():
    """`ts` at anything but exactly six fractional digits is a contract breach.

    `Instant.toString()` emits `...T00:00:00Z` for a whole second and
    `...T00:00:00.123456789Z` for a nanosecond one; both are below and above the
    frozen precision and both used to pass the runtime-stats equivalence check,
    which never called this code.
    """
    body = _spec_body()
    assert normalize(body)["ts"] == body["ts"]
    for bad in ("2026-06-06T00:00:00Z", "2026-06-06T00:00:00.100Z",
                "2026-06-06T00:00:00.123456789Z", "2026-06-06T00:00:00.000000+00:00"):
        with pytest.raises(NormalizationError, match=r"\$\.ts:"):
            normalize({**body, "ts": bad})


def test_normalizing_the_body_does_not_reject_the_per_runtime_additions():
    """The JVM's extra keys must survive the gate the previous test adds.

    `gc.collections` carries one integer per `GarbageCollectorMXBean` and two
    prose notes ride beside the null rows. Bean names contain spaces and the
    notes are long strings; neither is a timestamp or a money key, so
    normalization must pass them through untouched rather than trip on them.
    """
    body = _spec_body()
    body["gc"]["collections"] = {
        "gen0": None, "gen1": None, "gen2": None,
        "ZGC Minor Cycles": 40, "ZGC Minor Pauses": 120,
        "ZGC Major Cycles": 3, "ZGC Major Pauses": 9,
    }
    body["gc"]["collections_note"] = "gen0/gen1/gen2 are null on the JVM: ..."
    body["gc"]["pause_total_seconds"] = None
    body["gc"]["pause_total_seconds_note"] = "ms-granular bean, ZGC pauses are not"
    assert normalize(body)["gc"]["collections"]["ZGC Minor Cycles"] == 40
    assert shapecheck.check(body, "RuntimeStats") == []


# --------------------------------------------------------------------------- #
# the Java half of the same contract
# --------------------------------------------------------------------------- #
# `RuntimeStatsWriter.java` builds LinkedHashMaps, so its key tree is a set of
# `<map>.put("<key>", <value>)` calls, one method per nested object.
_JAVA_PUT = re.compile(r'\w+\.put\(\s*"([^"]+)"\s*,\s*(.*?)\);', re.DOTALL)
_JAVA_MAP_METHOD = re.compile(
    r'^\s+(?:private\s+|public\s+)?(?:static\s+)?Map<.*?>\s+(\w+)\(.*\)\s*\{$', re.MULTILINE
)
# Two keys' children are not literals in the writer, and both are declared here
# rather than inferred, because a silently-empty branch would make this test pass
# by describing nothing.
_JAVA_EXTERNAL = {
    # JvmGcBeans.collections(): the frozen null triple, then one dynamic key per
    # GarbageCollectorMXBean -- the declared per-runtime addition, not part of
    # the required set.
    "snapshot.gcCollections()": ("JvmGcBeans.java", "collections"),
}
_JAVA_DYNAMIC = {
    # A loop over EndpointKind, whose constants ARE the frozen route templates.
    "endpointCalls": ("EndpointKind.java", None),
}


def _java_src(name: str) -> str:
    return (paths.repo_root() / "services" / "java" / "src" / "main" / "java" / "io"
            / "ledgerline" / "observability" / name).read_text(encoding="utf-8")


def _java_method_bodies(src: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for m in _JAVA_MAP_METHOD.finditer(src):
        depth, i = 0, src.index("{", m.start())
        start = i
        while True:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        bodies[m.group(1)] = src[start + 1:i]
    return bodies


def _endpoint_routes() -> dict:
    src = _java_src("EndpointKind.java")
    return dict.fromkeys(re.findall(r'^\s+[A-Z_]+\("([^"]+)"\)[,;]$', src, re.MULTILINE))


def _java_keys() -> dict:
    """The key tree `services/java/.../RuntimeStatsWriter.java` writes."""
    bodies = _java_method_bodies(_java_src("RuntimeStatsWriter.java"))
    gc_bean_body = _java_method_bodies(_java_src("JvmGcBeans.java"))

    def tree(body: str) -> dict:
        out: dict = {}
        for key, value in _JAVA_PUT.findall(body):
            value = value.strip()
            call = re.match(r"(\w+)\(", value)
            if call and call.group(1) in bodies:
                out[key] = tree(bodies[call.group(1)])
            elif value in _JAVA_EXTERNAL:
                out[key] = tree(gc_bean_body[_JAVA_EXTERNAL[value][1]])
            else:
                out[key] = None
        return out

    root = tree(bodies["write"])
    for name, (_, _) in _JAVA_DYNAMIC.items():
        assert not _JAVA_PUT.findall(bodies[name]), f"{name} gained literal keys"
    root["endpoint_calls"] = _endpoint_routes()
    return root


def _flat(tree: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, child in tree.items():
        path = f"{prefix}{key}"
        out.add(path)
        if isinstance(child, dict):
            out |= _flat(child, f"{path}.")
    return out


def _declared_java_additions() -> set[str]:
    """The `Per-runtime additions` table's Java row, minus its one dynamic entry."""
    text = (paths.spec_dir() / "runtime-stats.md").read_text(encoding="utf-8")
    row = next(ln for ln in text.splitlines() if ln.startswith("| Java |"))
    return {k for k in re.findall(r"`([^`]+)`", row) if "<" not in k}


def test_the_spec_shape_block_is_what_the_java_writer_emits_too():
    """The .NET writer was pinned against the spec block and the Java one was not.

    So the whole Java column of a file that freezes into `config_hash` could go
    stale silently. The Java body is a SUPERSET by design (spec/runtime-stats.md
    "Per-runtime additions"), so this asserts both directions: every required
    key is emitted, and every key emitted beyond the required set is one the
    spec declares.
    """
    required = _flat(_key_tree(_spec_body()))
    emitted = _flat(_java_keys())
    assert not (required - emitted), (
        "spec/runtime-stats.md requires keys the Java writer does not emit: "
        f"{sorted(required - emitted)}"
    )
    assert (emitted - required) == _declared_java_additions()


def test_the_java_endpoint_call_counters_are_the_frozen_route_templates():
    """A route renamed in EndpointKind alone would zero that endpoint's warmup
    gate rather than fail it, on the Java side only."""
    assert set(_endpoint_routes()) == set(_spec_body()["endpoint_calls"])
