"""Shared fixtures. Loads the real spec configs once (the single source of truth).

The pure-logic tests run with NO docker. No test currently carries
@pytest.mark.docker; one that does is skipped when the daemon is absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ledgerbench import compose, config
from ledgerbench.util import paths


def git_backed_results(tmp_path, monkeypatch) -> Path:
    """`paths.results_dir()` inside a throwaway git repo with the repo's own rules.

    Any test that reaches `checksums.write_run_checksums` -- `phases.run_all` and
    `analyze.analyze_run` both end there -- needs one: the checksum scope is
    `git ls-files`' answer, so a fixture tree under a bare `tmp_path` has no
    publication scope at all and the writer raises rather than guess one.
    Copying the REPO's `.gitignore` in keeps those tests pinned to the project's
    real publication decision.

    The global and system git configs are cut off for the child processes: a
    developer's own `core.excludesFile` is a rule source git would apply, and
    leaving it in would make a fixture's scope depend on whose machine ran it.
    """
    if shutil.which("git") is None:
        pytest.skip("git decides the publication scope; the checksum writer needs it")
    root = tmp_path / "tree"
    results = root / "results"
    results.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    shutil.copyfile(paths.repo_root() / ".gitignore", root / ".gitignore")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(paths, "results_dir", lambda: results)
    return results


@pytest.fixture(scope="session")
def workload():
    return config.load_workload()


@pytest.fixture(scope="session")
def slo():
    return config.load_slo()


@pytest.fixture(autouse=True)
def _skip_docker_without_daemon(request):
    """Auto-skip @pytest.mark.docker tests when no daemon is reachable."""
    if request.node.get_closest_marker("docker") and not compose.docker_available():
        pytest.skip("docker daemon not available")
