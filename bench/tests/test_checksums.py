"""The run checksum map: scope, order, bytes, and the verifier that fails.

Round 1's `CHECKSUMS.sha256` was an append log with no test at all, and the
reason was structural: the writer lived inside `run_probe`, a docker-only
method. Everything below drives pure functions against fixture run trees under
`tmp_path`, so it runs with no root, no docker and no cgroups.

The scope half of the module neither translates `.gitignore` into Python nor
subtracts an exclusion set from its own tree walk -- both were wrong, silently,
in the same direction. It asks `git ls-files --cached --others
--exclude-standard`, whose answer IS the publication scope. So the tests that
used to prove a translation agreed with git are gone (they would now be
tautologies), and what is pinned instead is everything the delegation does NOT
hand to git:

  - the one path dropped on this side: `CHECKSUMS.sha256`, and nothing else;
  - the single call itself -- path counts that matter, and names holding a
    space or a newline, which is what `-z` is for;
  - the failure mode: a git that cannot answer RAISES, and never narrows the
    scope quietly, and an in-scope path it cannot read is refused, not dropped;
  - end to end, that the map carried into the tree a reader actually obtains
    passes `sha256sum -c` there with exit 0;
  - both directions of map-versus-scope in `verify`, since `sha256sum -c` checks
    neither.

The cases the two deleted scope implementations got wrong -- a regular FILE
named like a directory rule, `.git/info/exclude`, `core.excludesFile`,
`results/.gitignore`, un-anchored rules, a `!negated` rule -- are each pinned
against real git below; they now hold by construction, and the tests exist so
that stays visible.
"""

from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
import sys

import pytest

from ledgerbench import checksums
from ledgerbench.registry import DEFAULT_SLOT_SET, get_slot_set
from ledgerbench.util import paths

_SET = get_slot_set(DEFAULT_SLOT_SET)
SUBJECT, REFERENCE = _SET.subject, _SET.reference

# One file per artifact kind the runner writes, split by the repo's OWN
# publication decision. `_ignored` is what round 1's file wrongly covered.
_COMMITTED = (
    "ledger.json",
    "manifest.json",
    "doctor.json",
    "host_state.json",
    "capacity-gate/verdict.json",
    "report/fits.json",
    "report/report.md",
    "cells/c/ladder/rate500/block0/{s}.summary.json",
    "cells/c/ladder/rate500/block0/{s}.warmup.json",
    "cells/c/ladder/rate500/block0/cache_stats_{s}.json",
    "cells/c/ladder/rate500/block0/cpu_mem_{s}.summary.json",
    "cells/c/ladder/rate500/block0/cpu_mem_{s}.postgres.summary.json",
    "cells/c/ladder/rate500/block0/{s}.runtime_stats.summary.json",
    "cells/c/ladder/rate500/block0/{s}.warmup.runtime_stats.summary.json",
)
_IGNORED = (
    "cells/c/ladder/rate500/block0/{s}.p99_per_second.csv",
    "cells/c/ladder/rate500/block0/{s}.warmup_latency_per_second.csv",
    "cells/c/ladder/rate500/block0/cpu_mem_{s}.csv",
    "cells/c/ladder/rate500/block0/{s}.runtime_stats.jsonl",
    "io/{s}/targets.txt",
    "capacity-gate/targets.txt",
    "capacity-gate/bodies/b1.json",
    "cells/c/ladder/rate500/block0/{s}.attack.bin",
    "pgdata.dump",
)
# git applies an UN-ANCHORED rule (no `/` but a trailing one) at EVERY depth, so
# these are ignored inside a run directory exactly as they are at the repo root.
# Reading only the `results/`-prefixed rules put every one of them in the map:
# `verify` green on the author's box, `sha256sum -c` exiting 1 with
# `FAILED open or read` on the tree a reader can actually obtain.
_UNANCHORED_NOISE = (
    ".DS_Store",
    "Thumbs.db",
    "cells/c/ladder/rate500/block0/.DS_Store",
    "cells/c/stray.pyc",
    ".idea/workspace.xml",
    "report/report.md.user",
    # `paths.write_bytes_atomic`'s scratch, left by a SIGKILL rather than an
    # exception; `test_a_leftover_atomic_write_temp_is_out_of_scope_and_git_agrees`
    # builds the name with the real writer instead of spelling it.
    ".ledger.json.a1b2c3.tmp",
    "cells/c/ladder/rate500/block0/.cpu_mem_{s}.summary.json.9z8y.tmp",
)
_IGNORED += _UNANCHORED_NOISE

# The five paths the deleted translator answered differently from git. Each is a
# regular FILE whose name equals a rule that git applies to DIRECTORIES only, so
# git publishes all five and the old map listed none of them.
_FILES_NAMED_LIKE_DIRECTORY_RULES = (
    "bodies",
    "cells/io",
    ".idea",
    ".vs",
    "cells/c/.vscode",
)


def _expand(templates) -> list[str]:
    return [t.format(s=SUBJECT) for t in templates]


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    """`tmp_path/tree` is a real git repo carrying the repo's own `.gitignore`.

    `conftest.git_backed_results` holds the why: the scope is git's answer, so a
    tree outside a repository has no publication scope and every writer raises.
    It also cuts off the global and system git configs, because a developer's own
    `core.excludesFile` is a rule source git WOULD apply and would make the
    fixture's scope depend on whose machine ran it --
    `test_a_core_excludesfile_rule_is_gits_answer_too` puts one back deliberately.
    """
    from tests.conftest import git_backed_results

    return git_backed_results(tmp_path, monkeypatch)


def _build_run(run_id: str, rels=None) -> "object":
    run_dir = paths.run_dir(run_id)
    for rel in rels if rels is not None else _expand(_COMMITTED + _IGNORED):
        p = run_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"content of {rel}\n", encoding="utf-8")
    return run_dir


def _git(run_dir, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(run_dir), *args], capture_output=True, check=False
    )


def _git_publishes(run_dir, rel: str) -> bool:
    """git's own verdict, asked ONE path at a time, on the tree under test.

    A different git command from the one the module runs, asked the complementary
    way round, so it is an independent oracle for the scope rather than a
    restatement of it. Without `-v` the exit code IS the verdict: `-v` exits 0
    whenever a rule MATCHED, including a `!negation` that publishes the path, and
    subtracting that output from a tree walk is how the map came to drop a file
    git publishes.
    """
    res = _git(run_dir, "check-ignore", "-q", rel)
    assert res.returncode in (0, 1), res.stderr
    return res.returncode == 1


def _publish(root, run_id: str):
    """Materialise the tree a reader obtains: `git add -A`, then the INDEX.

    `add -A` shares git's exclude machinery with the module's `ls-files`, so this
    is not an independent oracle for the scope -- `_git_publishes` is. What it
    buys is the tree itself: the map is carried into a directory holding only
    what a clone holds, and `sha256sum -c` gets the last word there.
    """
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True)
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                         check=True, capture_output=True).stdout
    tracked = [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]
    prefix = f"results/{run_id}/"
    dest = root.parent / "published"
    rels = []
    for tp in tracked:
        if not tp.startswith(prefix):
            continue
        rel = tp[len(prefix):]
        rels.append(rel)
        target = dest / run_id / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / tp, target)
    return dest / run_id, sorted(rels)


# --------------------------------------------------------------------------- #
# the one path dropped on this side of the call
# --------------------------------------------------------------------------- #
def test_only_the_map_itself_is_dropped_from_gits_answer(isolated_results):
    """git's answer is taken whole, minus the one file that cannot list its hash.

    Every path removed on this side is a file published but unverified, with
    `verify` green over it -- the same false green as a bad rule, one layer
    later. `CHECKSUMS.sha256` is the only removal, and a dot-directory, a deep
    hidden file and a nested `.gitignore` are all kept because git keeps them.
    """
    rels = [
        "ledger.json",
        ".hidden/deep/a.json",
        "cells/c/.gitignore",
        "no_extension",
        "cells/c/ladder/rate500/block0/x.summary.json",
    ]
    run_dir = _build_run("full-x", rels=rels)
    (run_dir / checksums.CHECKSUM_FILE).write_text("stale map\n", encoding="utf-8")

    for rel in rels:
        assert _git_publishes(run_dir, rel), rel
    assert _git_publishes(run_dir, checksums.CHECKSUM_FILE)
    assert checksums.published_rels(run_dir) == sorted(rels, key=lambda r: r.split("/"))


def test_map_order_is_partwise_not_string(isolated_results):
    """`a-b/c.json` sorts BEFORE `a/b.json` as a string and AFTER it part-wise.

    `config.config_hash`'s discipline: sorting the Path object or the joined
    string yields a different map on Windows for the same tree.
    """
    rels = ["a-b/c.json", "a/b.json", "report/fits.json", "ledger.json"]
    expected = sorted(rels, key=lambda r: r.split("/"))
    assert expected != sorted(rels)

    run_dir = _build_run("full-x", rels=rels)
    assert checksums.published_rels(run_dir) == expected

    # The EMITTED bytes are what a reader verifies, and checksum_text sorts
    # again: pin the file's own line order, not just the walker's.
    checksums.write_run_checksums("full-x")
    emitted = (run_dir / checksums.CHECKSUM_FILE).read_text(encoding="utf-8").splitlines()
    assert [ln.split("  ", 1)[1] for ln in emitted] == expected


# --------------------------------------------------------------------------- #
# the single call: counts, framing, and what comes back
# --------------------------------------------------------------------------- #
def test_the_single_call_handles_a_path_count_that_matters(isolated_results):
    """One `ls-files` over a run-sized tree, split back into every path it named.

    A real full run is thousands of artifacts, and reading the reply is the last
    place this module can still lose one: a split that drops the tail, or a
    decode that truncates, narrows the map with `verify` still green.
    """
    rels = [f"cells/c/b{i // 100}/f{i}.summary.json" for i in range(2000)]
    rels += [f"cells/c/b{i // 100}/f{i}.csv" for i in range(2000)]
    run_dir = _build_run("full-x", rels=rels)

    scoped = checksums.published_rels(run_dir)
    assert len(scoped) == 2000
    assert all(r.endswith(".summary.json") for r in scoped)
    assert scoped == sorted(
        (r for r in rels if r.endswith(".summary.json")), key=lambda r: r.split("/")
    )


@pytest.mark.parametrize(
    "name",
    [
        "a file with spaces.summary.json",
        pytest.param(
            "a\nnewline.summary.json",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="newlines are not legal in NTFS names"
            ),
        ),
    ],
)
def test_odd_filenames_survive_the_round_trip_through_git(isolated_results, name):
    """NUL framing, against the two things that break every other framing.

    A space breaks any parse that splits the reply on whitespace. A newline
    breaks more than that: without `-z` git QUOTES the pathname
    (`"a\\nnewline.summary.json"`), and a newline-split reply turns one path into
    two. Either way the map carries a name no file has, so `sha256sum -c` on the
    published tree fails to open it while `verify` stays green.
    """
    published = f"cells/c/{name}"
    ignored = f"cells/c/{name.replace('.summary.json', '.csv')}"
    run_dir = _build_run("full-x", rels=["ledger.json", published, ignored])

    assert _git_publishes(run_dir, published)
    assert not _git_publishes(run_dir, ignored)
    assert set(checksums.published_rels(run_dir)) == {"ledger.json", published}


# --------------------------------------------------------------------------- #
# the failure mode: raise, never narrow
# --------------------------------------------------------------------------- #
def test_a_run_directory_outside_any_git_repository_raises(tmp_path, monkeypatch):
    """No repo, no scope. The alternative is a map over a scope nobody knows.

    This is the shape round 1 shipped: a checksum file whose coverage could not
    be stated. A Python approximation of git as a fallback is the thing this
    module deleted, so there is nothing to fall back to.
    """
    if shutil.which("git") is None:
        pytest.skip("git decides the publication scope")
    results = tmp_path / "loose" / "results"
    (results / "full-x").mkdir(parents=True)
    (results / "full-x" / "ledger.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "results_dir", lambda: results)

    with pytest.raises(checksums.ScopeError, match="not a git repository"):
        checksums.published_rels(results / "full-x")
    with pytest.raises(checksums.ScopeError):
        checksums.write_run_checksums("full-x")


def test_an_absent_git_raises_rather_than_publishing_an_unscoped_map(
    isolated_results, monkeypatch
):
    run_dir = _build_run("full-x")

    def _no_git(*a, **k):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(checksums.subprocess, "run", _no_git)
    with pytest.raises(checksums.ScopeError, match="cannot ask git"):
        checksums.write_run_checksums("full-x")
    with pytest.raises(checksums.ScopeError, match="cannot ask git"):
        checksums.write_probe_checksums(
            "full-x", run_dir, SUBJECT, [run_dir / "ledger.json"]
        )


def test_verify_reports_an_unaskable_scope_as_a_fault_and_never_as_ok(
    isolated_results, monkeypatch
):
    """`verify` returns a verdict rather than raising -- and the verdict is not-ok.

    A green `verify` over a scope nobody could establish is the exact failure
    this module exists to make unreachable, so the ScopeError becomes a fault the
    CLI exits 1 on, not a swallowed exception and not a pass.
    """
    _build_run("full-x")
    checksums.write_run_checksums("full-x")
    assert checksums.verify("full-x").ok

    def _broken(*a, **k):
        raise OSError("git is wedged")

    monkeypatch.setattr(checksums.subprocess, "run", _broken)
    res = checksums.verify("full-x")
    assert not res.ok
    assert "cannot ask git" in res.fault and res.checked == 0


# --------------------------------------------------------------------------- #
# tracked vs untracked: the `--no-index` decision
# --------------------------------------------------------------------------- #
def test_a_tracked_file_is_published_even_when_a_rule_would_ignore_it(
    isolated_results, tmp_path
):
    """`--no-index` is deliberately NOT passed, and this is what that buys.

    The question is "will a reader who clones this repo obtain the file". A path
    already in the index is obtained whatever the ignore rules say, and git
    answers not-ignored for it. Under `--no-index` it would be dropped from the
    map while every clone still carried it -- round 1's divergence, sign flipped.
    """
    root = tmp_path / "tree"
    run_dir = _build_run("full-x", rels=["ledger.json", "cells/c/x.csv"])
    assert checksums.published_rels(run_dir) == ["ledger.json"]

    subprocess.run(["git", "-C", str(root), "add", "-f", "results/full-x/cells/c/x.csv"],
                   check=True, capture_output=True)
    assert _git_publishes(run_dir, "cells/c/x.csv")
    assert checksums.published_rels(run_dir) == ["cells/c/x.csv", "ledger.json"]

    checksums.write_run_checksums("full-x")
    listed = (run_dir / checksums.CHECKSUM_FILE).read_text(encoding="utf-8")
    assert "  cells/c/x.csv\n" in listed


# --------------------------------------------------------------------------- #
# the five cases the deleted translator got wrong
# --------------------------------------------------------------------------- #
def test_a_regular_file_named_like_a_directory_rule_is_published(isolated_results):
    """`results/**/io/`, `.idea/`, `.vs/`, `.vscode/` are DIRECTORY-only rules.

    The translator consumed the trailing `/` and then matched the name at any
    kind, so a regular file called `io` or `.idea` left the map while git
    published it. `sha256sum -c` on the obtained tree cannot catch that -- it
    only checks what is listed -- so the map was short and every check green.
    """
    run_dir = _build_run(
        "full-x",
        rels=["ledger.json", "io/a.txt", *_FILES_NAMED_LIKE_DIRECTORY_RULES],
    )
    for rel in _FILES_NAMED_LIKE_DIRECTORY_RULES:
        assert _git_publishes(run_dir, rel), rel
    assert not _git_publishes(run_dir, "io/a.txt")

    scoped = set(checksums.published_rels(run_dir))
    assert scoped == {"ledger.json", *_FILES_NAMED_LIKE_DIRECTORY_RULES}


def test_a_git_info_exclude_rule_is_gits_answer_too(isolated_results, tmp_path):
    """A whole rule source the translator could not see: `.git/info/exclude`.

    git applies it to exactly the paths `.gitignore` applies to. With `report/`
    excluded there, the old map went on listing `report/report.md`, `verify`
    stayed green, and the published tree did not contain the file.
    """
    run_dir = _build_run("full-x", rels=["ledger.json", "report/report.md"])
    assert _git_publishes(run_dir, "report/report.md")

    exclude = tmp_path / "tree" / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("report/\n", encoding="utf-8")

    assert not _git_publishes(run_dir, "report/report.md")
    assert checksums.published_rels(run_dir) == ["ledger.json"]


def test_a_core_excludesfile_rule_is_gits_answer_too(isolated_results, tmp_path,
                                                     monkeypatch):
    """The second unseeable source. Same paths, same authority, same answer."""
    run_dir = _build_run("full-x", rels=["ledger.json", "report/report.md"])
    excludes = tmp_path / "user-excludes"
    excludes.write_text("report/\n", encoding="utf-8")
    cfg = tmp_path / "user-gitconfig"
    # as_posix(): a Windows path's backslashes are escapes inside a git config value.
    cfg.write_text(f"[core]\n\texcludesFile = {excludes.as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))

    assert not _git_publishes(run_dir, "report/report.md")
    assert checksums.published_rels(run_dir) == ["ledger.json"]


def test_a_gitignore_beside_or_above_the_run_is_applied_not_refused(isolated_results):
    """`results/.gitignore` and a nested one are git's business now, not a fault.

    The module used to refuse both, because its own loader read the root file
    only and could not honour them. git honours them natively -- which is the
    whole point of asking it -- so they narrow the scope correctly, and the
    `.gitignore` files themselves are published artifacts like any other.
    """
    run_dir = _build_run(
        "full-x", rels=["ledger.json", "report/report.md", "cells/c/keep.summary.json"]
    )
    (isolated_results / ".gitignore").write_text("report/\n", encoding="utf-8")
    (run_dir / "cells" / ".gitignore").write_text("keep.summary.json\n", encoding="utf-8")

    assert not _git_publishes(run_dir, "report/report.md")
    assert not _git_publishes(run_dir, "cells/c/keep.summary.json")
    assert checksums.published_rels(run_dir) == ["cells/.gitignore", "ledger.json"]

    checksums.write_run_checksums("full-x")
    assert checksums.verify("full-x").ok


def test_a_negated_rule_keeps_its_file_in_the_map(isolated_results, tmp_path):
    """`!keepme.pyc` re-publishes a path an earlier rule ignored, and git says so.

    This is what killed the tree-walk-minus-`check-ignore -v` scope. A record in
    `-v` output means "a rule matched this path", and its exit code means "I
    printed something" -- so the record naming the NEGATION was subtracted like
    any other, dropping from the map a file every clone contains. `verify` stayed
    green over it, because the map only ever checked its own entries.
    `ls-files` has no such halfway answer: the path is in the scope or it is not.
    """
    root = tmp_path / "tree"
    run_dir = _build_run(
        "full-x", rels=["ledger.json", "cells/c/keepme.pyc", "cells/c/stray.pyc"]
    )
    (run_dir / "cells" / ".gitignore").write_text("!keepme.pyc\n", encoding="utf-8")

    assert _git_publishes(run_dir, "cells/c/keepme.pyc")
    assert not _git_publishes(run_dir, "cells/c/stray.pyc")
    assert checksums.published_rels(run_dir) == [
        "cells/.gitignore", "cells/c/keepme.pyc", "ledger.json"
    ]

    checksums.write_run_checksums("full-x")
    assert checksums.verify("full-x").ok
    published_dir, tracked = _publish(root, "full-x")
    assert "cells/c/keepme.pyc" in tracked
    if shutil.which("sha256sum"):
        assert _sha256sum_c(published_dir) == 0


def test_an_unanchored_rule_applies_at_every_depth(isolated_results):
    """`*.pyc`, `.DS_Store`, `.*.tmp` are ignored inside a run exactly as at root.

    Reading only the `results/`-prefixed rules put every one of these in the map:
    `verify` green on the author's box, `sha256sum -c` exiting 1 with `FAILED
    open or read` on the tree a reader can actually obtain.
    """
    run_dir = _build_run("full-x")
    for rel in _expand(_UNANCHORED_NOISE):
        assert not _git_publishes(run_dir, rel), rel
    assert set(checksums.published_rels(run_dir)) == set(_expand(_COMMITTED))


def test_a_leftover_atomic_write_temp_is_out_of_scope_and_git_agrees(
    isolated_results, monkeypatch
):
    """`write_bytes_atomic` names its scratch `.<target>.<rand>.tmp`.

    Its unlink runs on Python exceptions only, so a SIGKILL between mkstemp and
    rename -- the class the resume protocol exists to survive -- leaves one on
    disk. The fix is the `.gitignore` rule, not an exclusion in checksums.py: a
    Python-only exclusion would leave git committing the scratch file while the
    map stopped listing it, with `verify` green over the published tree. Killing
    the rename (rather than spelling a name) keeps this pinned to the REAL one.
    """
    run_dir = _build_run("full-x")
    before = {p.name for p in run_dir.iterdir()}
    monkeypatch.setattr(paths.os, "replace", lambda src, dst: None)
    paths.write_text_atomic(run_dir / "ledger.json", "half-written\n")
    leftover, = [p for p in run_dir.iterdir() if p.name not in before]

    rel = checksums.rel_posix(run_dir, leftover)
    assert leftover.is_file()
    assert not _git_publishes(run_dir, rel)
    assert rel not in checksums.published_rels(run_dir)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on NTFS")
def test_a_published_path_that_cannot_be_read_is_refused_not_dropped(isolated_results):
    """A dangling symlink is a git object like any other: git publishes it.

    Filtering the scope by `Path.is_file()` would drop it -- one more published
    file the map does not cover, `verify` green, and `sha256sum -c` in the clone
    exiting 1 on a path nothing warned about. Its bytes cannot be hashed, so the
    only honest map over this tree is no map, said loudly.
    """
    run_dir = _build_run("full-x")
    (run_dir / "dangling.json").symlink_to("nowhere.json")

    assert _git_publishes(run_dir, "dangling.json")
    assert "dangling.json" in checksums.published_rels(run_dir)
    with pytest.raises(ValueError, match="dangling.json"):
        checksums.write_run_checksums("full-x")


# --------------------------------------------------------------------------- #
# tiers git publishes nothing of
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("run_id", ["smoke-x", "rehearsal-x"])
def test_an_unpublished_tier_gets_no_map_at_all(isolated_results, run_id):
    """`results/smoke-*/` and `results/rehearsal-*/` exclude the whole run dir.

    git's answer for every path inside one is "not published", so the only
    honest map is no file. Writing one anyway -- which classifying those two
    rules out used to do -- asserts a publication scope that does not exist, and
    `verify` would then have to be green over a tree no reader can obtain.
    """
    run_dir = _build_run(run_id)
    assert checksums.write_run_checksums(run_id) is None
    assert checksums.write_probe_checksums(
        run_id, run_dir, SUBJECT, [run_dir / "ledger.json"]
    ) is None
    assert not (run_dir / checksums.CHECKSUM_FILE).exists()

    res = checksums.verify(run_id)
    assert not res.ok and res.checked == 0
    # The fault names the results directory by its native path, so no separator here.
    assert "publishes nothing" in res.fault and "results" in res.fault


def test_a_full_run_is_a_published_tier(isolated_results):
    run_dir = _build_run("full-x")
    assert checksums.unpublished_tier_fault(run_dir) == ""
    assert checksums.write_run_checksums("full-x") is not None


# --------------------------------------------------------------------------- #
# what the map covers
# --------------------------------------------------------------------------- #
def test_rollups_are_covered(isolated_results):
    """The roll-ups are in the map (R1-B4: round 1's file never covered them)."""
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    listed = (run_dir / checksums.CHECKSUM_FILE).read_text(encoding="utf-8")
    for rel in ("ledger.json", "manifest.json", "doctor.json",
                "capacity-gate/verdict.json", "report/fits.json", "report/report.md"):
        assert f"  {rel}\n" in listed


def test_the_repo_gitignore_still_excludes_every_trace_kind_the_scope_relies_on(
    isolated_results,
):
    """Against the repo's REAL `.gitignore`, one artifact name per kind.

    The scope is git's answer now, so this no longer pins rule TEXT -- it pins
    the publication decision itself: these kinds stay out, those stay in, and a
    `.gitignore` edit that moves one shows up here rather than in a run.
    """
    run_dir = _build_run("full-x")
    scoped = set(checksums.published_rels(run_dir))
    assert scoped == set(_expand(_COMMITTED))
    assert scoped.isdisjoint(_expand(_IGNORED))


# --------------------------------------------------------------------------- #
# a map, not a log
# --------------------------------------------------------------------------- #
def test_no_path_appears_twice_after_a_simulated_resume(isolated_results):
    """108 paths carried two hashes in round 1 because re-runs are ROUTINE."""
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")

    retried = run_dir / _expand(("cells/c/ladder/rate500/block0/{s}.summary.json",))[0]
    retried.write_text("re-run of an excluded rung\n", encoding="utf-8")
    extra = run_dir / "cells/c/ladder/rate500/block1/redone.summary.json"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("redone block\n", encoding="utf-8")
    checksums.write_run_checksums("full-x")

    lines = (run_dir / checksums.CHECKSUM_FILE).read_text(encoding="utf-8").splitlines()
    rels = [ln.split("  ", 1)[1] for ln in lines]
    assert len(rels) == len(set(rels))
    by_rel = {ln.split("  ", 1)[1]: ln.split("  ", 1)[0] for ln in lines}
    assert by_rel[checksums.rel_posix(run_dir, retried)] == checksums.sha256_file(retried)
    assert checksums.rel_posix(run_dir, extra) in by_rel


def test_write_run_checksums_refuses_an_empty_scope(isolated_results):
    """A published tier that publishes nothing is a bug, not an empty map."""
    run_dir = paths.run_dir("full-empty")
    run_dir.mkdir(parents=True)
    (run_dir / "trace.csv").write_text("only a gitignored trace\n", encoding="utf-8")
    with pytest.raises(ValueError, match="full-empty"):
        checksums.write_run_checksums("full-empty")


# --------------------------------------------------------------------------- #
# bytes and ordering: byte-identical on Windows and Linux
# --------------------------------------------------------------------------- #
def test_rel_posix_is_separator_independent():
    from pathlib import PureWindowsPath

    root = PureWindowsPath(r"C:\results\full-x")
    p = root / "cells" / "c" / "a.summary.json"
    assert checksums.rel_posix(root, p) == "cells/c/a.summary.json"


def test_the_written_file_is_sha256sum_compatible_lf_bytes(isolated_results):
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    raw = (run_dir / checksums.CHECKSUM_FILE).read_bytes()
    assert b"\r" not in raw and b"\\" not in raw
    assert raw.endswith(b"\n")
    import re
    for line in raw.decode("utf-8").splitlines():
        assert re.fullmatch(r"[0-9a-f]{64}  \S.*", line), line


# --------------------------------------------------------------------------- #
# per-probe files
# --------------------------------------------------------------------------- #
def test_two_slots_in_one_block_write_disjoint_probe_checksum_files(isolated_results):
    """`_artifact_dir` has no slot component: a bare `probe.sha256` is overwritten."""
    run_dir = _build_run("full-x", rels=[])
    adir = run_dir / "cells/c/ladder/rate500/block0"
    adir.mkdir(parents=True)
    written = {}
    for slot in (SUBJECT, REFERENCE):
        p = adir / f"{slot}.summary.json"
        p.write_text(f"{slot} summary\n", encoding="utf-8")
        written[slot] = checksums.write_probe_checksums("full-x", adir, slot, [p])

    assert written[SUBJECT] != written[REFERENCE]
    for slot, path in written.items():
        text = path.read_text(encoding="utf-8")
        assert text.count("\n") == 1
        assert f"{slot}.summary.json" in text
        other = REFERENCE if slot == SUBJECT else SUBJECT
        assert f"{other}.summary.json" not in text


def test_a_probe_checksum_covers_only_what_git_publishes(isolated_results):
    """The probe file passes the SAME scope filter, so it cannot list a trace.

    A probe writes its `.csv`/`.jsonl`/`.bin` traces alongside the roll-ups; if
    the probe file listed those, its lines could not all appear in the run map
    and `probe_divergent` would fire on every clean run.
    """
    run_dir = _build_run("full-x", rels=[])
    adir = run_dir / "cells/c/ladder/rate500/block0"
    adir.mkdir(parents=True)
    written = []
    for name in (f"{SUBJECT}.summary.json", f"{SUBJECT}.p99_per_second.csv",
                 f"{SUBJECT}.attack.bin", f"{SUBJECT}.runtime_stats.jsonl"):
        p = adir / name
        p.write_text("x\n", encoding="utf-8")
        written.append(p)

    out = checksums.write_probe_checksums("full-x", adir, SUBJECT, written)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [ln.split("  ", 1)[1] for ln in lines] == [
        f"cells/c/ladder/rate500/block0/{SUBJECT}.summary.json"
    ]


def test_probe_checksum_paths_are_relative_to_the_run_dir(isolated_results):
    run_dir = _build_run("full-x", rels=[])
    adir = run_dir / "cells/c/ladder/rate500/block0"
    adir.mkdir(parents=True)
    p = adir / f"{SUBJECT}.summary.json"
    p.write_text("x\n", encoding="utf-8")
    out = checksums.write_probe_checksums("full-x", adir, SUBJECT, [p])
    assert out.read_text(encoding="utf-8").split("  ", 1)[1].strip() == (
        f"cells/c/ladder/rate500/block0/{SUBJECT}.summary.json"
    )


def test_probe_checksum_lines_appear_verbatim_in_the_run_map(isolated_results):
    """Two file KINDS are still one verifiable object only if they cannot disagree."""
    run_dir = _build_run("full-x")
    adir = run_dir / "cells/c/ladder/rate500/block0"
    probe_inputs = sorted(p for p in adir.iterdir() if p.is_file())
    checksums.write_probe_checksums("full-x", adir, SUBJECT, probe_inputs)
    checksums.write_run_checksums("full-x")

    map_lines = set(
        (run_dir / checksums.CHECKSUM_FILE).read_text(encoding="utf-8").splitlines()
    )
    probe_file = adir / f"{SUBJECT}{checksums.PROBE_SUFFIX}"
    probe_lines = probe_file.read_text(encoding="utf-8").splitlines()
    assert probe_lines and set(probe_lines) <= map_lines
    assert checksums.verify("full-x").probe_divergent == ()


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def test_verify_passes_a_clean_run(isolated_results):
    _build_run("full-x")
    checksums.write_run_checksums("full-x")
    res = checksums.verify("full-x")
    assert res.ok and res.checked == len(_COMMITTED)


def test_verify_detects_a_tampered_artifact(isolated_results):
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    (run_dir / "capacity-gate/verdict.json").write_text("tampered\n", encoding="utf-8")
    res = checksums.verify("full-x")
    assert not res.ok
    assert res.mismatched == ("capacity-gate/verdict.json",)


def test_verify_detects_a_missing_artifact(isolated_results):
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    (run_dir / "report/report.md").unlink()
    res = checksums.verify("full-x")
    assert not res.ok
    assert res.missing == ("report/report.md",)


def test_verify_detects_an_artifact_present_but_unlisted(isolated_results):
    """Without this the map cannot tell `verified` from `partially verified`."""
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    new = run_dir / "cells/c/ladder/rate500/block0/cpu_mem_x.redis.summary.json"
    new.write_text("added after the map\n", encoding="utf-8")
    res = checksums.verify("full-x")
    assert not res.ok
    assert res.unlisted == ("cells/c/ladder/rate500/block0/cpu_mem_x.redis.summary.json",)


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum not available")
def test_verify_detects_a_listed_path_git_no_longer_publishes(
    isolated_results, tmp_path
):
    """The direction that made this verifier WEAKER than `sha256sum -c`, not stronger.

    RUNBOOK.md offers the two checks as equivalent on the same bytes, with three
    faults `verify` catches on top. Comparing the map against the scope in one
    direction only (an in-scope file nothing lists) left the other silent: an
    ignore-rule edit after the map was written leaves the file on disk, so
    `sha256sum -c` passes HERE and `verify` returns ok -- while the tree a reader
    actually clones no longer contains it and `sha256sum -c` there exits 1 with
    "FAILED open or read". The fresh-clone half of that is what `_publish` runs
    below.
    """
    root = tmp_path / "tree"
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    assert checksums.verify("full-x").ok and _sha256sum_c(run_dir) == 0

    (isolated_results / ".gitignore").write_text("report/\n", encoding="utf-8")

    res = checksums.verify("full-x")
    assert not res.ok
    assert res.out_of_scope == ("report/fits.json", "report/report.md")
    assert res.missing == () and res.mismatched == () and res.unlisted == ()
    # Still green under the check `verify` is supposed to dominate.
    assert _sha256sum_c(run_dir) == 0

    published_dir, tracked = _publish(root, "full-x")
    assert not any(t.startswith("report/") for t in tracked)
    assert _sha256sum_c(published_dir) == 1


def test_verify_detects_a_probe_file_that_diverges_from_the_map(isolated_results):
    run_dir = _build_run("full-x")
    adir = run_dir / "cells/c/ladder/rate500/block0"
    target = adir / f"{SUBJECT}.summary.json"
    checksums.write_probe_checksums("full-x", adir, SUBJECT, [target])
    checksums.write_run_checksums("full-x")
    target.write_text("rewritten after the probe recorded it\n", encoding="utf-8")
    checksums.write_run_checksums("full-x")

    res = checksums.verify("full-x")
    assert not res.ok
    assert len(res.probe_divergent) == 1
    assert f"{SUBJECT}.summary.json" in res.probe_divergent[0]


def test_verify_ignores_the_checksum_file_itself(isolated_results):
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    listed = (run_dir / checksums.CHECKSUM_FILE).read_text(encoding="utf-8")
    assert checksums.CHECKSUM_FILE not in listed
    assert checksums.verify("full-x").ok


def _sha256sum_c(run_dir) -> int:
    return subprocess.run(
        ["sha256sum", "-c", checksums.CHECKSUM_FILE],
        cwd=str(run_dir), capture_output=True,
    ).returncode


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum not available")
@pytest.mark.parametrize("where", ["first", "last"])
def test_a_map_listing_one_path_twice_fails_here_exactly_as_sha256sum_c_does(
    isolated_results, where
):
    """Round 1's artifact: 108 paths carrying two hashes.

    Reading the map into a dict deduped it -- last line won -- so `verify`
    returned ok on bytes `sha256sum -c` exits 1 on, and WHICH answer you got
    depended on line order. README.md offers the two checks as equivalent; they
    have to be. `checked` counts LINES, so the CLI cannot report "N verified"
    for a map that says more than N things.
    """
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    map_path = run_dir / checksums.CHECKSUM_FILE
    good = map_path.read_text(encoding="utf-8")
    assert checksums.verify("full-x").ok and _sha256sum_c(run_dir) == 0

    bogus = f"{'0' * 64}  ledger.json\n"
    map_path.write_text(bogus + good if where == "first" else good + bogus, encoding="utf-8")
    res = checksums.verify("full-x")
    assert not res.ok
    assert "ledger.json is listed twice" in res.fault
    assert res.checked == len(_COMMITTED) + 1
    assert _sha256sum_c(run_dir) == 1


def test_a_line_without_the_two_space_separator_is_a_malformed_map(isolated_results):
    """GNU's binary form (`<sha> *path`) is not this map's format.

    Partitioning on `"  "` regardless made `rel` empty and the whole line the
    hash: the CLI printed a bare `MISSING ` and a spurious `UNLISTED` for a file
    that IS listed, and N unparseable lines collapsed into the one empty key.
    """
    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    map_path = run_dir / checksums.CHECKSUM_FILE
    lines = [
        line.replace("  ", " *", 1) if line.endswith("  ledger.json") else line
        for line in map_path.read_text(encoding="utf-8").splitlines()
    ]
    map_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = checksums.verify("full-x")
    assert not res.ok
    assert "not a '<sha256>  <path>' line" in res.fault and "ledger.json" in res.fault
    assert res.missing == () and res.unlisted == ()


@pytest.mark.parametrize("body", [None, "", "\n"])
def test_verify_fails_on_an_absent_or_empty_map(isolated_results, body):
    """A checker that exits 0 having checked nothing is what B11 closes."""
    run_dir = _build_run("full-x")
    if body is not None:
        (run_dir / checksums.CHECKSUM_FILE).write_text(body, encoding="utf-8")
    res = checksums.verify("full-x")
    assert not res.ok and res.checked == 0 and res.fault


# --------------------------------------------------------------------------- #
# end to end, in the tree a reader actually obtains
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum not available")
def test_the_map_verifies_in_the_tree_a_reader_actually_obtains(
    isolated_results, tmp_path
):
    """The whole point, on an adversarial tree, checked by the tool README offers.

    `add -A` shares git's exclude machinery with the module's `ls-files`, so the
    set equality below is close to a restatement; what cannot be restated is the
    last line, where the map is checked by `sha256sum -c` inside a directory
    holding ONLY what a clone holds. Both failure directions were shipped and
    both land there: a map listing what git drops exits 1 with "FAILED open or
    read" (round 1), and a map short of what git publishes leaves artifacts
    unverified with every check green (the directory-rule bug -- the five names
    below are exactly those).
    """
    root = tmp_path / "tree"
    # `.idea` is one of the five, so the fixture's `.idea/workspace.xml` cannot
    # also exist -- a name is a file or a directory, not both.
    rels = [
        *(r for r in _expand(_COMMITTED + _IGNORED) if r != ".idea/workspace.xml"),
        *_FILES_NAMED_LIKE_DIRECTORY_RULES,
        "a file with spaces.summary.json",
        "cells/c/deep/.gitignore",
    ]
    run_dir = _build_run("full-x", rels=rels)
    checksums.write_run_checksums("full-x")
    assert checksums.verify("full-x").ok

    published_dir, tracked = _publish(root, "full-x")
    listed = sorted(
        ln.split("  ", 1)[1]
        for ln in (run_dir / checksums.CHECKSUM_FILE).read_text(
            encoding="utf-8"
        ).splitlines()
    )
    assert listed == sorted(r for r in tracked if r != checksums.CHECKSUM_FILE)
    assert set(_FILES_NAMED_LIKE_DIRECTORY_RULES) <= set(listed)
    assert _sha256sum_c(published_dir) == 0


# --------------------------------------------------------------------------- #
# the writer discipline itself
# --------------------------------------------------------------------------- #
def _is_write_mode(node: ast.Call, mode_index: int) -> bool:
    mode = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
    if mode is None and len(node.args) > mode_index:
        mode = node.args[mode_index]
    return isinstance(mode, ast.Constant) and bool(set("wax+") & set(str(mode.value)))


# The ONE exemption, by name and by file: `util.paths`' two scratch writers.
# `results/**/io/` is gitignored, uncoverable by the checksum map and a pure
# function of (seed, stream labels, mix), so a torn file there is regenerated
# rather than repaired -- and at full scale it is ~350,000 request bodies whose
# fsyncs bought nothing. Exempting them BY NAME keeps the ban total everywhere
# else: a third scratch writer has to be added to this list to pass.
_SCRATCH_WRITERS = {("paths.py", "write_scratch_text"), ("paths.py", "open_scratch_text")}


def _scratch_writer_lines(path, tree) -> set[int]:
    """Line numbers inside the named scratch writers of `path` (else empty)."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and (path.name, node.name) in _SCRATCH_WRITERS):
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


def test_the_atomic_writer_is_the_only_writer_in_the_package():
    """`paths.write_bytes_atomic` is the SOLE artifact writer, asserted by AST.

    That is what makes it defensible for the checksum files to cover only the
    published scope: a torn artifact is not a reachable state, so trace
    integrity does not depend on hashing the traces. Banning append mode alone
    left that invariant untested -- swapping `paths.write_text_atomic(out, ...)`
    for `out.write_text(...)` inside `checksums.py` passed the whole suite, and a
    truncating write is torn across a crash exactly as an append is.
    (`paths.py`'s `os.fdopen` on an `mkstemp` fd IS the atomic writer, not a
    bypass, and is not an `open` call.)

    The vegeta io tree is the sole exemption -- see `_SCRATCH_WRITERS`. It is
    granted to two NAMED functions in one file, so the ban still holds over
    every module that generates targets: `vegeta.py` reaches a plain write only
    by calling one of them.
    """
    offences = []
    for path in sorted((paths.repo_root() / "bench" / "ledgerbench").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exempt = _scratch_writer_lines(path, tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or node.lineno in exempt:
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("write_text", "write_bytes"):
                offences.append(f"{path.name}:{node.lineno} .{func.attr}()")
            elif (isinstance(func, ast.Attribute) and func.attr == "open"
                    and _is_write_mode(node, 0)):
                offences.append(f"{path.name}:{node.lineno} .open(<write mode>)")
            elif (isinstance(func, ast.Name) and func.id == "open"
                    and _is_write_mode(node, 1)):
                offences.append(f"{path.name}:{node.lineno} open(<write mode>)")
    assert not offences, (
        "every artifact write goes through util.paths.write_text_atomic / "
        "write_bytes_atomic: a raw write leaves a torn file after a crash and an "
        "append makes a checksum a log instead of a map (R1-B4): "
        + ", ".join(offences)
    )


# --------------------------------------------------------------------------- #
# call sites
# --------------------------------------------------------------------------- #
def test_run_all_writes_the_map_after_host_restore(isolated_results, monkeypatch):
    """host_restore's mark_phase rewrites ledger.json; a map written one line
    earlier records a hash of a file that no longer matches."""
    from ledgerbench import phases
    from tests.test_phases import _ctx

    for name in ("run_doctor_phase", "run_host_setup_phase", "run_host_readback_phase",
                 "run_seed_phase", "run_capacity_gate_phase", "run_validation_phase",
                 "run_cells_phase", "run_variants_phase", "finalize_manifest"):
        monkeypatch.setattr(phases, name, lambda ctx: None)
    monkeypatch.setattr(phases, "IS_LINUX", False)

    ctx = _ctx("full-runall-checksums")
    phases.run_all(ctx)

    run_dir = paths.run_dir("full-runall-checksums")
    lines = (run_dir / checksums.CHECKSUM_FILE).read_text(encoding="utf-8").splitlines()
    recorded = {ln.split("  ", 1)[1]: ln.split("  ", 1)[0] for ln in lines}
    assert recorded["ledger.json"] == checksums.sha256_file(run_dir / "ledger.json")
    assert checksums.verify("full-runall-checksums").ok


@pytest.mark.skipif(
    any(importlib.util.find_spec(m) is None for m in ("numpy", "scipy", "pandas")),
    reason="analysis stack missing",
)
def test_analyze_run_writes_the_map_covering_report_outputs(isolated_results):
    """report/ does not exist when run_all finishes, so its map can never cover it."""
    from ledgerbench import analyze
    from ledgerbench.ledger import Ledger

    from ledgerbench.config import config_hash as _live_config_hash

    Ledger.load_or_create(
        "full-analyze-checksums", slot_set=DEFAULT_SLOT_SET,
        config_hash=_live_config_hash(), rng_seed=42,
    ).save()
    analyze.analyze_run("full-analyze-checksums")

    run_dir = paths.run_dir("full-analyze-checksums")
    lines = (run_dir / checksums.CHECKSUM_FILE).read_text(encoding="utf-8").splitlines()
    recorded = {ln.split("  ", 1)[1]: ln.split("  ", 1)[0] for ln in lines}
    for rel in ("report/fits.json", "report/report.md"):
        assert recorded[rel] == checksums.sha256_file(run_dir / rel)
    assert checksums.verify("full-analyze-checksums").ok


def test_verify_cli_exits_non_zero_and_names_the_path(isolated_results):
    from typer.testing import CliRunner

    from ledgerbench.__main__ import app

    run_dir = _build_run("full-x")
    checksums.write_run_checksums("full-x")
    assert CliRunner().invoke(app, ["verify", "full-x"]).exit_code == 0

    (run_dir / "manifest.json").write_text("tampered\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["verify", "full-x"])
    assert res.exit_code == 1
    assert "MISMATCH" in res.output and "manifest.json" in res.output
    # A tampered file is not a scope question, so the operator is not sent to
    # `check-ignore` for it.
    assert "check-ignore" not in res.output


def test_verify_cli_points_a_scope_failure_at_the_rule_that_caused_it(isolated_results):
    """The map carries no record of the rules it was built against.

    An unlisted or out-of-scope path almost always means the ignore rules moved
    between the run and the check, and `check-ignore -v` is the only form of the
    answer that names the rule AND the file it lives in -- which may be
    `.git/info/exclude` or a `core.excludesFile`, where no reader would look.
    """
    from typer.testing import CliRunner

    from ledgerbench.__main__ import app

    _build_run("full-x")
    checksums.write_run_checksums("full-x")
    (isolated_results / ".gitignore").write_text("report/\n", encoding="utf-8")

    res = CliRunner().invoke(app, ["verify", "full-x"])
    assert res.exit_code == 1
    # rich wraps at the terminal width; join it back up so the assertions are
    # about content, not line breaks (`test_cli_help._help` does the same).
    out = " ".join(res.output.split())
    assert "OUT-OF-SCOPE" in out and "report/report.md" in out
    assert "git check-ignore -v" in out


def test_verify_cli_exits_non_zero_on_an_absent_map(isolated_results):
    from typer.testing import CliRunner

    from ledgerbench.__main__ import app

    _build_run("full-x")
    res = CliRunner().invoke(app, ["verify", "full-x"])
    assert res.exit_code == 1
