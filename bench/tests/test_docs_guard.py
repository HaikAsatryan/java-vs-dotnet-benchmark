"""W31: the mechanical half of CLAUDE.md's never-quote-round-1 rule, plus a
relative-link checker for the prose a reader actually lands on.

Two guards, with deliberately different scopes.

**The round-1 run id** (`_ROUND_ONE_RUN_ID`) is banned in every file the repo
publishes, outside the planning record. A round-1 run *directory name* has no
legitimate use anywhere in round 2: it does not name a path that exists, it does
not explain why any round-2 code has its shape, and the only thing it can do in
this tree is make a reader believe a run is available to read.

**The wider token list** is banned in the PROSE SURFACE only -- the Markdown a
reader treats as this repository's claims about itself, plus the GitHub issue and
PR templates. Source files under `bench/`, `infra/` and `services/` are outside
it ON PURPOSE, and the reason is not laziness: comments there routinely have to
say *why* round-2 code is shaped the way it is, and round 1's defects are that
reason ("recalibrate: round 1's gate rejected 59.5% of its windows", "the knee
moved 1,468 -> 10,426 rps on this knob alone"). Deleting those sentences would
delete the rationale and leave the constant nowhere. Two guards already cover
`bench/ledgerbench/` from the other direction: `test_registry.py`'s anti-sed
literal guard, which bans a language or slot literal the code could BRANCH on,
and its standing-negative guard, which bans "descoped"-shaped claims from strings
that reach a run artifact.

`services/**` is not walked: its Markdown and its source comments are outside
the prose surface this guard covers.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from ledgerbench.util import paths

# --------------------------------------------------------------------------- #
# scope
# --------------------------------------------------------------------------- #
# The round-2 planning record. These files exist to catalogue round 1's defects
# so they can be fixed, so they are where its numbers BELONG. `docs/p1-plan.md`
# is on the list because PROGRESS.md names it as its own executable breakdown of
# P1 ("the executable breakdown lives in docs/p1-plan.md"); it is the same
# object as the PROGRESS section it expands, not a published document.
PLANNING_RECORD = ("PROGRESS.md", "CLAUDE.md", "docs/p1-plan.md")
PLANNING_DIRS = ("docs/research/", "seed/")

# This module, which has to spell every banned token in order to ban it. Same
# exemption `test_registry.py` gives `registry.py`, and for the same reason: the
# declaration cannot be subject to itself.
GUARD_SELF = "bench/tests/test_docs_guard.py"

# The Markdown a reader treats as this repo's claims about itself.
PROSE_GLOBS = (
    "README.md",
    "CONTRIBUTING.md",
    "docs/*.md",
    ".github/*.md",
    ".github/ISSUE_TEMPLATE/*.md",
    "bench/README.md",
    "bench/NOTES.md",
    "infra/NOTES.md",
    "spec/*.md",
)

# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #
ROUND_ONE_RUN_ID = "20260814T051533-fedora"

# Identifiers of the stack round 2 does not run, and the four measured constants
# round 1's README and METHODOLOGY led with. A reader who meets any of these in
# round-2 prose is being handed a round-1 fact dressed as a round-2 one, or
# pointed at a directory that does not exist.
FORBIDDEN_TOKENS = (
    ROUND_ONE_RUN_ID,
    "services/go",
    "sut-go",
    "GOGC",
    "GOMAXPROCS",
    "sqlc",
    "descoped at scope freeze",
    "358,673,386",   # round-1 run total requests
    "10,426",        # round-1 .NET knee rps
    "3,715",         # round-1 PG create-only floor
    "59.5%",         # round-1 under-warmed flagging rate
)


def _repo_files() -> list[str]:
    """Every file the repo publishes: tracked, plus untracked and not ignored.

    The same definition `checksums.py` uses for a run directory, and for the same
    reason -- git already resolves negations, nested ignore files and
    `core.excludesFile`, so this side enumerates nothing and cannot drift.
    """
    root = paths.repo_root()
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return sorted(set(out.stdout.split("\n")) - {""})


def _in_planning_record(rel: str) -> bool:
    return rel in PLANNING_RECORD or rel.startswith(PLANNING_DIRS)


def _prose_files() -> list[str]:
    root = paths.repo_root()
    seen: list[str] = []
    for pattern in PROSE_GLOBS:
        for path in sorted(root.glob(pattern)):
            rel = path.relative_to(root).as_posix()
            if not _in_planning_record(rel) and rel not in seen:
                seen.append(rel)
    return seen


def tokens_in(text: str, tokens) -> list[str]:
    """Which of `tokens` appear in `text`. The whole matcher, so it is testable."""
    return [t for t in tokens if t in text]


# --------------------------------------------------------------------------- #
# the guards
# --------------------------------------------------------------------------- #
def test_the_prose_surface_is_non_empty_and_covers_the_published_docs():
    """A glob that matches nothing would make the guard below vacuously green."""
    found = _prose_files()
    assert {"README.md", "CONTRIBUTING.md", "docs/METHODOLOGY.md",
            "docs/FAIRNESS.md", "docs/RUNBOOK.md"} <= set(found)
    assert "docs/p1-plan.md" not in found


def test_no_round_one_token_survives_in_the_prose_surface():
    """CLAUDE.md's never-quote-round-1 rule, made mechanical.

    Round 2 re-measures .NET from scratch and stands on its own numbers. A
    round-1 constant next to a round-2 one is not a comparison, it is two
    different harnesses on two different days; and `services/go` names a
    directory this repo does not have.
    """
    root = paths.repo_root()
    offences = [
        f"{rel}: {tok!r}"
        for rel in _prose_files()
        for tok in tokens_in((root / rel).read_text(encoding="utf-8"), FORBIDDEN_TOKENS)
    ]
    assert not offences, (
        "round-1 tokens in round-2 prose (see CLAUDE.md, 'never quote a round-1 "
        "number next to a round-2 number'):\n  " + "\n  ".join(offences)
    )


def test_the_round_one_run_id_appears_nowhere_the_repo_publishes():
    """Wider than the guard above, and it can afford to be.

    The four constants and the Go identifiers earn their place in a source
    comment that explains why round-2 code has its shape. A round-1 RUN
    DIRECTORY name cannot: it resolves to nothing, explains nothing, and its only
    effect on a reader is to suggest a run they can go and read.
    """
    root = paths.repo_root()
    offences = []
    for rel in _repo_files():
        if _in_planning_record(rel) or rel == GUARD_SELF:
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if ROUND_ONE_RUN_ID in text:
            offences.append(rel)
    assert not offences, (
        f"the round-1 run id {ROUND_ONE_RUN_ID} names no directory in this "
        "repo:\n  " + "\n  ".join(offences)
    )


@pytest.mark.parametrize("token", FORBIDDEN_TOKENS)
def test_the_token_guard_fires_on_a_planted_token(token):
    """A guard nobody has seen fail is a guard nobody knows works."""
    assert tokens_in(f"prose about {token} in the middle", FORBIDDEN_TOKENS) == [token]
    assert tokens_in("prose with nothing round-1 in it", FORBIDDEN_TOKENS) == []


def test_the_planning_record_is_exempt_and_actually_uses_the_exemption():
    """The exemption is load-bearing, not defensive.

    If PROGRESS.md stopped carrying round-1 constants the exemption would be
    dead weight and should go; while it carries them, the guard above would fail
    on the very file whose job is to record them.
    """
    root = paths.repo_root()
    progress = (root / "PROGRESS.md").read_text(encoding="utf-8")
    assert tokens_in(progress, FORBIDDEN_TOKENS), (
        "PROGRESS.md carries no round-1 token; the planning-record exemption "
        "has nothing left to protect and should be deleted"
    )


# --------------------------------------------------------------------------- #
# relative links
# --------------------------------------------------------------------------- #
_LINK = re.compile(r'!?\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.M)


def heading_slugs(markdown: str) -> set[str]:
    """GitHub's anchor slugs for a document's headings.

    Lowercase, drop inline `code`/emphasis markers, drop everything that is not
    a word character, space or hyphen, then spaces to hyphens.
    """
    out = set()
    for heading in _HEADING.findall(markdown):
        slug = heading.strip().lower()
        slug = re.sub(r"[`*_]", "", slug)
        slug = re.sub(r"[^\w\s-]", "", slug)
        out.add(re.sub(r"\s+", "-", slug))
    return out


def broken_links(rel: str, text: str, root) -> list[str]:
    """Relative links in one document that resolve to nothing.

    Both halves are checked: the path, and -- when the target is Markdown in
    this repo -- the `#anchor`. A live path with a dead anchor lands the reader
    at the top of the wrong document, which is the failure a link checker that
    stops at `exists()` waves through.
    """
    here = (root / rel).parent
    out = []
    for match in _LINK.finditer(text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path, _, fragment = target.partition("#")
        resolved = (root / rel) if not path else (here / path)
        line = text[: match.start()].count("\n") + 1
        if not resolved.exists():
            out.append(f"{rel}:{line}: dead path -> {target}")
        elif fragment and resolved.suffix == ".md":
            if fragment not in heading_slugs(resolved.read_text(encoding="utf-8")):
                out.append(f"{rel}:{line}: dead anchor -> {target}")
    return out


def test_every_relative_link_in_the_prose_surface_resolves():
    """Round 1 published a README whose every results link 404'd after the purge.

    The dangling references were not a cosmetic problem: `results/README.md` was
    cited as the authority for the run's own known limits, and `services/go` was
    cited as the place to point at a specific dial.
    """
    root = paths.repo_root()
    offences = [
        problem
        for rel in _prose_files()
        for problem in broken_links(rel, (root / rel).read_text(encoding="utf-8"), root)
    ]
    assert not offences, "dangling relative links:\n  " + "\n  ".join(offences)


def test_the_link_checker_fires_on_a_dead_path_and_on_a_dead_anchor(tmp_path):
    """Both halves, on planted links, so neither can rot into a no-op."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "target.md").write_text("# Real Heading\n", encoding="utf-8")
    doc = (
        "[dead](docs/missing.md)\n"
        "[dead anchor](docs/target.md#no-such-heading)\n"
        "[live](docs/target.md#real-heading)\n"
        "[live path](docs/target.md)\n"
        "[external](https://example.com/missing.md)\n"
    )
    (tmp_path / "page.md").write_text(doc, encoding="utf-8")
    problems = broken_links("page.md", doc, tmp_path)
    assert problems == [
        "page.md:1: dead path -> docs/missing.md",
        "page.md:2: dead anchor -> docs/target.md#no-such-heading",
    ]


def test_heading_slugs_match_the_anchors_the_readme_actually_links_to():
    """The slug rule is only right if it agrees with a real document.

    README's "Start here" table is entirely same-file anchors, so it is the
    fixture: if the rule drifted, every one of them would read as dead.
    """
    root = paths.repo_root()
    readme = (root / "README.md").read_text(encoding="utf-8")
    slugs = heading_slugs(readme)
    assert {"functional-tier-any-os", "measured-tier-linux", "whats-in-here",
            "disagreeing-productively"} <= slugs
