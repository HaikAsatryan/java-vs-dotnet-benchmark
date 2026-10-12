"""The run's checksum map: one file, recomputed from scratch, that verifies.

Round 1 shipped an append-mode `CHECKSUMS.sha256` that could not verify a
published tree: `reset_orphan_blocks`/`_retry_excluded_rungs` make re-runs
routine, so 108 paths ended up carrying two hashes; it covered gitignored
traces, which are never published; and it never covered the roll-ups
(`ledger.json`, `manifest.json`, `doctor.json`, `capacity-gate/verdict.json`,
`report/*`). See PROGRESS.md's R1-B4 row.

Two properties fix that, and both are structural rather than disciplined:

  - a MAP, not a log. `write_run_checksums` emits every in-scope path exactly
    once, sorted, through `paths.write_text_atomic`. A resume, a re-run block or
    a retried rung simply supersedes the file wholesale; there is no state to
    double.
  - the checksum scope IS the publication scope, because **git states it**:
    `git ls-files --cached --others --exclude-standard` under the run directory
    is tracked-plus-untracked-not-ignored, which is the definition of what a
    reader who clones this repo obtains.

The scope comes from `ls-files`, so git resolves the ignore rules. Nothing here
enumerates candidates, subtracts an exclusion set, or reads a rule: negations,
nested `.gitignore` files, `.git/info/exclude`, `core.excludesFile`,
directory-only rules and the tracked-file-that-a-rule-would-ignore case are all
already resolved in the answer. git is already a hard dependency of this harness
(`phases._record_git_identity` shells to it to stamp `manifest.identity.git_sha`).

The one path dropped from git's answer is `CHECKSUMS.sha256`, which cannot list
its own hash. A path git publishes whose bytes cannot be read (a dangling
symlink; a tracked file deleted from the tree) is a refusal, never a quiet
omission -- an unreadable path fails `sha256sum -c` in the published tree too.

Taking git's answer literally has one consequence worth stating up front: it
excludes `results/smoke-*/` and `results/rehearsal-*/` wholesale, so those tiers
publish nothing and get no map at all (`unpublished_tier_fault`). An earlier
design classified those two rules out and wrote a map anyway, which asserted a
publication scope that does not exist.

`<slot>.probe.sha256` beside each probe's artifacts keeps probe atomicity
honest, and its lines are RUN-relative and pass the SAME scope filter, so every
one of them appears verbatim in the run map (`VerifyResult.probe_divergent`
fails the verification if they ever disagree). Trace integrity does not rest on
either file: `paths.write_bytes_atomic` is the only artifact writer in this
package, so a torn artifact is not a reachable state.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath

from .util import paths

CHECKSUM_FILE = "CHECKSUMS.sha256"
# Slot-keyed, because `phases._artifact_dir` has no slot component and hands the
# SAME block directory to both slots' probes: a bare `probe.sha256` would be
# written by the first slot and overwritten by the second, losing that probe's
# integrity record with the run still looking complete. Same collision, same
# resolution as `live_runner.shared_summary_name`.
PROBE_SUFFIX = ".probe.sha256"

# One process over a full run tree; a bound is here only so a wedged git cannot
# hang a finished measured run.
_GIT_TIMEOUT_S = 120


class ScopeError(RuntimeError):
    """git could not be asked, so the publication scope is unknown.

    Nothing may be written under this: a checksum map whose scope could not be
    established cannot verify a published tree (R1-B4), and there is no Python
    fallback that approximates git. Callers that produce a verdict rather than a
    file (`verify`) turn it into a fault: still not-ok, never green.
    """


@dataclass(frozen=True)
class VerifyResult:
    checked: int
    mismatched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unlisted: tuple[str, ...] = ()
    # Listed, present on disk, and NOT published: `sha256sum -c` passes here and
    # exits 1 with "FAILED open or read" in a fresh clone, which is the one
    # direction in which this verifier would otherwise be the weaker of the two.
    out_of_scope: tuple[str, ...] = ()
    probe_divergent: tuple[str, ...] = ()
    fault: str = ""

    @property
    def ok(self) -> bool:
        return (
            not self.fault
            and not self.mismatched
            and not self.missing
            and not self.unlisted
            and not self.out_of_scope
            and not self.probe_divergent
        )


def rel_posix(root: PurePath, p: PurePath) -> str:
    """Run-relative path with `/` separators on EVERY platform.

    `str(WindowsPath.relative_to(...))` renders backslashes, which `sha256sum -c`
    cannot resolve -- the defect the deleted append writer shipped.
    """
    return p.relative_to(root).as_posix()


def _partwise(rels: Iterable[str]) -> list[str]:
    """Sorted on the POSIX path SPLIT INTO PARTS, never on the Path object.

    `config.config_hash`'s discipline: `sorted()` over `WindowsPath` compares
    case-insensitively and reorders siblings against a nested directory, so a
    byte-identical tree would emit a different map off-Linux and the published
    file would be unverifiable there.
    """
    return sorted(rels, key=lambda rel: rel.split("/"))


def _git(
    cmd: list[str], ok_codes: tuple[int, ...], question: str
) -> subprocess.CompletedProcess[bytes]:
    """Run `cmd`, or raise `ScopeError`. There is no fallback, deliberately.

    Any exit code outside `ok_codes` (128: not a git repository, a broken index,
    a bad config) leaves the publication scope unknown, so nothing is written.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScopeError(f"cannot ask git {question} ({' '.join(cmd)}): {exc}") from exc
    if proc.returncode not in ok_codes:
        raise ScopeError(
            f"git exited {proc.returncode} asking {question} ({' '.join(cmd)}), so "
            f"the publication scope is unknown: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc


def published_rels(run_dir: Path) -> list[str]:
    """Every path git publishes under `run_dir`, run-relative, in map order.

    `--cached --others --exclude-standard` is tracked plus
    untracked-and-not-ignored, which IS the publication scope: no enumeration
    here, no exclusion set, no rule read. `-C run_dir` asks inside the tree being
    checksummed, so git discovers the repository that governs it, applies every
    rule source that repository owns, and prints paths already relative to the
    run directory -- the map's own vocabulary. `-z` is what makes a filename
    holding a space or a newline survive.

    `--cached` is what keeps a TRACKED artifact in the map even where a rule
    would ignore it: a committed path is in every clone, whatever the rules say.
    """
    proc = _git(
        ["git", "-C", str(run_dir), "ls-files",
         "--cached", "--others", "--exclude-standard", "-z"],
        (0,),
        f"what it publishes under {run_dir}",
    )
    rels = {
        rel for rel in proc.stdout.decode("utf-8", "surrogateescape").split("\0")
        if rel and rel != CHECKSUM_FILE
    }
    return _partwise(rels)


def published_subset(run_dir: Path, files: Iterable[Path]) -> list[str]:
    """The run-relative subset of `files` git publishes, in map order."""
    published = set(published_rels(run_dir))
    return _partwise(
        rel for rel in (rel_posix(run_dir, p) for p in files) if rel in published
    )


def unpublished_tier_fault(run_dir: Path) -> str:
    """Non-empty when git excludes the run DIRECTORY itself.

    `.gitignore` drops `results/smoke-*/` and `results/rehearsal-*/` wholesale:
    those tiers are rehearsal apparatus, never a data source, so git's answer for
    every path inside one is "not published" and the only honest checksum map
    over them is no file at all.

    Such a tier already comes back from `published_rels` as an empty scope, so
    this is asked separately for the one thing an empty scope cannot say: WHICH
    empty. A published run that wrote nothing is a bug the writers raise on; a
    tier git excludes by name is by design and they return None. The question is
    about the directory's own NAME rather than its contents, and a name is only a
    path from one level up -- hence the parent. Without `-v` the exit code is
    git's verdict itself, not a report that something was printed.
    """
    proc = _git(
        ["git", "-C", str(run_dir.parent), "check-ignore", "-q", "--", run_dir.name],
        (0, 1),
        f"whether it publishes {run_dir}",
    )
    if proc.returncode == 1:
        return ""
    return (
        f"git publishes nothing under {run_dir}: the run directory itself is "
        f"excluded, so this tier has no publication scope and carries no "
        f"{CHECKSUM_FILE} (`git check-ignore -v {run_dir.name}` in "
        f"{run_dir.parent} names the rule and the file it lives in)"
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def checksum_text(run_dir: Path, rels: Iterable[str]) -> str:
    """`sha256sum -c`-compatible lines (two spaces), sorted, one path each.

    An in-scope path whose bytes cannot be read RAISES rather than being dropped:
    git publishes it, so a map without it would be short and every check green,
    and `sha256sum -c` fails to open it in the published tree either way.
    """
    lines = []
    for rel in _partwise(rels):
        try:
            lines.append(f"{sha256_file(run_dir / Path(rel))}  {rel}")
        except OSError as exc:
            raise ValueError(
                f"git publishes {rel} under {run_dir} but its bytes cannot be "
                f"read, so no checksum map over it can verify: {exc}"
            ) from exc
    return "\n".join(lines) + "\n"


def write_run_checksums(run_id: str) -> Path | None:
    """The run's map, or None when git publishes no part of this tier."""
    run_dir = paths.run_dir(run_id)
    if unpublished_tier_fault(run_dir):
        return None
    rels = published_rels(run_dir)
    if not rels:
        raise ValueError(
            f"refusing to write an empty checksum map for {run_id}: git publishes "
            f"no file under {run_dir}"
        )
    out = run_dir / CHECKSUM_FILE
    paths.write_text_atomic(out, checksum_text(run_dir, rels))
    return out


def write_probe_checksums(
    run_id: str,
    artifact_dir: Path,
    slot_id: str,
    written: Iterable[Path],
) -> Path | None:
    """The probe's map, or None when git publishes no part of this tier."""
    run_dir = paths.run_dir(run_id)
    if unpublished_tier_fault(run_dir):
        return None
    published = published_subset(run_dir, written)
    if not published:
        raise ValueError(f"probe {slot_id} in {artifact_dir} wrote no published artifact")
    out = artifact_dir / f"{slot_id}{PROBE_SUFFIX}"
    paths.write_text_atomic(out, checksum_text(run_dir, published))
    return out


def _read_map(path: Path) -> tuple[dict[str, str], int, str]:
    """Entries, the LINE count, and the first fault that makes the map itself bad.

    A dict alone loses the very artifact B11 exists to eliminate: round 1's map
    gave 108 paths two hashes, and a later line silently replacing an earlier one
    left `verify` green on bytes `sha256sum -c` exits 1 on -- ordering-dependently,
    which is worse than either answer. The count is of LINES, so the CLI cannot
    report "N artifacts verified" for a map that says more than N things.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    listed = sum(1 for line in lines if line.strip())
    entries: dict[str, str] = {}
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        sha, sep, rel = line.partition("  ")
        if not sep or not rel:
            return entries, listed, f"{path}:{n}: not a '<sha256>  <path>' line: {line!r}"
        if rel in entries:
            return entries, listed, f"{path}:{n}: {rel} is listed twice"
        entries[rel] = sha
    return entries, listed, ""


def verify(run_id: str) -> VerifyResult:
    """Recheck the map against the tree. Not-ok is the answer, never an exception.

    The map and the publication scope are compared in BOTH directions, because
    `sha256sum -c` checks neither: `unlisted` is an in-scope artifact nothing
    accounts for, so "verified" cannot be confused with "partially verified";
    `out_of_scope` is a listed path git no longer publishes, which passes here
    and fails in the fresh clone that is the only tree a reader has. It is
    restricted to paths present on disk so that an absent one is reported once,
    as `missing`.

    A `ScopeError` becomes a fault rather than escaping, because this function's
    contract is a verdict; the verdict on an unknown scope is not-ok, and the CLI
    exits 1 on it exactly as it does on a mismatch.
    """
    run_dir = paths.run_dir(run_id)
    try:
        tier_fault = unpublished_tier_fault(run_dir)
    except ScopeError as exc:
        return VerifyResult(checked=0, fault=str(exc))
    if tier_fault:
        return VerifyResult(checked=0, fault=tier_fault)
    map_path = run_dir / CHECKSUM_FILE
    if not map_path.is_file():
        return VerifyResult(checked=0, fault=f"no {CHECKSUM_FILE} in {run_dir}")
    entries, listed, fault = _read_map(map_path)
    if fault:
        return VerifyResult(checked=listed, fault=fault)
    if not entries:
        return VerifyResult(checked=0, fault=f"{map_path} lists no files")

    try:
        published = set(published_rels(run_dir))
    except ScopeError as exc:
        return VerifyResult(checked=listed, fault=str(exc))

    mismatched, missing, out_of_scope = [], [], []
    for rel, sha in sorted(entries.items()):
        p = run_dir / Path(rel)
        if not p.is_file():
            missing.append(rel)
            continue
        if sha256_file(p) != sha:
            mismatched.append(rel)
        if rel not in published:
            out_of_scope.append(rel)

    unlisted = sorted(published - set(entries))

    listed_lines = {f"{sha}  {rel}" for rel, sha in entries.items()}
    divergent = []
    for rel in sorted(published):
        if not rel.endswith(PROBE_SUFFIX):
            continue
        for line in (run_dir / Path(rel)).read_text(encoding="utf-8").splitlines():
            if line.strip() and line not in listed_lines:
                divergent.append(f"{rel}: {line}")

    return VerifyResult(
        checked=listed,
        mismatched=tuple(mismatched),
        missing=tuple(missing),
        unlisted=tuple(unlisted),
        out_of_scope=tuple(out_of_scope),
        probe_divergent=tuple(divergent),
    )
