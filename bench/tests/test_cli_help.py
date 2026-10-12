"""The CLI surface must DISCLOSE the operator escape hatches.

The runner now aborts by default on a non-passing capacity gate, and the only
two ways to change what it does are environment variables. If `--help` does not
name them, the operator's only source is the source code.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from ledgerbench import phases
from ledgerbench.__main__ import app

runner = CliRunner()

# typer forces rich into terminal mode whenever GITHUB_ACTIONS is set, and it
# reads that at import time, so no per-invocation env can turn it off. In that
# mode every `--flag` in the help text is wrapped in SGR escapes, which splits a
# quoted command like `git ls-files --cached --others` into pieces a substring
# assertion cannot find. That is what kept this suite red in CI and green on
# every developer machine.
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _help(*argv: str) -> str:
    res = runner.invoke(app, [*argv, "--help"])
    assert res.exit_code == 0, res.output
    # rich/click wraps help text at the terminal width; join it back up so the
    # assertions are about content, not line breaks or colour.
    return " ".join(_SGR.sub("", res.output).split())


def test_root_help_names_both_operator_env_vars():
    out = _help()
    assert phases.ALLOW_DB_IMPOSED_KNEE_ENV in out
    assert phases.CACHE_TTL_OVERRIDE_ENV in out


@pytest.mark.parametrize("cmd", ["run-all", "gate", "ladder", "confirm", "soak"])
def test_every_gate_touching_command_documents_the_opt_out(cmd):
    out = _help(cmd)
    assert phases.ALLOW_DB_IMPOSED_KNEE_ENV in out
    assert phases.CACHE_TTL_OVERRIDE_ENV in out


def test_run_all_help_states_the_abort_by_default_behaviour():
    out = _help("run-all")
    assert "ABORTS at the capacity gate" in out


@pytest.mark.parametrize("cmd", ["ladder", "confirm", "soak"])
def test_per_stage_help_says_it_re_asserts_the_gate(cmd):
    out = _help(cmd)
    assert "gate" in out.lower()


def test_verify_help_states_the_scope_it_actually_checks():
    """`verify --help` is the one place an operator reads the scope rule.

    It has three times named a scope other than the one the command applied --
    the repo's `results/` rules while un-anchored ones also narrowed the map; a
    `.gitignore` transcription while git was the authority; then `check-ignore`,
    whose output was being read as a verdict it does not give. It now names the
    command that IS the scope, which is also the only description that stays true
    when the rule lives in `.git/info/exclude` or a `core.excludesFile`.
    """
    out = _help("verify")
    assert "git ls-files --cached --others --exclude-standard" in out
    assert "derived from the repo's `results/` gitignore rules" not in out
    assert "root .gitignore" not in out
    assert "check-ignore" not in out
