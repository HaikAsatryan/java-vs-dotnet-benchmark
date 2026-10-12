"""Repository path resolution and atomic write-temp-rename.

The runner is invoked from anywhere (PowerShell from bench/, make from root).
All spec/results paths are derived from the repo root, found by walking up from
this file until a directory containing both `spec/` and `infra/` is seen (both
are PERMANENT repo dirs, so root detection never rests on a transient one).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def repo_root() -> Path:
    """Walk up from this module until spec/ and infra/ are both present.

    (Sentinels must be PERMANENT repo dirs; transient working dirs must never be
    load-bearing here.)
    """
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "spec").is_dir() and (parent / "infra").is_dir():
            return parent
    # Fallback: bench/ is two levels up from util/. Repo root is its parent.
    return here.parents[3]


def bench_dir() -> Path:
    return repo_root() / "bench"


def spec_dir() -> Path:
    return repo_root() / "spec"


def workload_path() -> Path:
    return spec_dir() / "workload.yaml"


def slo_path() -> Path:
    return spec_dir() / "slo.yaml"


def results_dir() -> Path:
    return repo_root() / "results"


def run_dir(run_id: str) -> Path:
    return results_dir() / run_id


def infra_dir() -> Path:
    return repo_root() / "infra"


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file + os.replace (atomic rename).

    Used by the run-ledger and any artifact that must never be observed
    half-written across a crash/resume boundary.
    """
    write_bytes_atomic(path, text.encode("utf-8"))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # fsync the DIRECTORY too: os.replace orders the rename in memory, but a
        # power cut (the failure the ledger exists for, distinct from a process
        # kill) can drop an un-synced directory entry and revert `path` to its
        # previous content. The consequence here would be benign -- a reverted
        # probe re-runs -- but the docstring promise is durability, so keep it.
        # POSIX-only; on Windows a directory cannot be opened with os.open.
        if os.name == "posix":
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# scratch writes (results/<run_id>/io/, gitignored and regenerable)
# --------------------------------------------------------------------------- #
# The atomic writer above exists so a published artifact is never observed torn
# across a crash, and `test_the_atomic_writer_is_the_only_writer_in_the_package`
# keeps it the only writer in the package. The vegeta io tree is the one thing
# that is neither published nor recoverable-by-reading: `.gitignore` excludes
# `results/**/io/`, the checksum map cannot cover it, and every file in it is a
# pure function of the seed, the stream labels and the mix -- so a torn one is
# regenerated, not repaired. At full scale it is ~350,000 body files, and
# mkstemp+write+flush+FSYNC+rename per body bought nothing for that.
#
# These two functions are the ONLY exemption, and they are named so the guard
# can allow them by name.


def write_scratch_text(path: Path, text: str) -> None:
    """Plain buffered write of a REGENERABLE scratch file. Never an artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def open_scratch_text(path: Path):
    """Open a REGENERABLE scratch file for streaming text writes.

    The per-probe targets draw streams into this handle rather than building the
    whole file in memory first: round 1 held every line of a 1M-request file in
    one list before a byte reached disk, and a draw at the top of a full ladder
    is several times larger than that.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("w", encoding="utf-8", newline="\n")
