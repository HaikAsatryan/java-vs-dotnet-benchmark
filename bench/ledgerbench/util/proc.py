"""Thin subprocess wrapper with consistent decoding and error surfacing.

KISS: synchronous subprocess.run only. The runner has no need for asyncio; the
one place a tight cadence matters (cgroups sampler) uses a dedicated thread, not
async I/O.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


# Exit code for a command killed by `timeout`, matching coreutils' timeout(1) so
# the value means the same thing in a ledger as it does in a shell.
TIMEOUT_RETURNCODE = 124


@dataclass(frozen=True)
class ProcResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def timed_out(self) -> bool:
        return self.returncode == TIMEOUT_RETURNCODE


def run(
    args: list[str],
    *,
    check: bool = False,
    timeout: float | None = None,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> ProcResult:
    """Run `args`, capture text stdout/stderr.

    Never uses shell=True (avoids quoting drift across Windows/Linux). On
    `check=True` a non-zero exit raises RuntimeError with captured stderr.
    `extra_env` is merged over the inherited environment.

    A `timeout` expiry is a RESULT, not an exception: it comes back as
    `returncode == TIMEOUT_RETURNCODE` with whatever output the process had
    produced, and only `check=True` turns it into a raise. Every caller here
    passes `check=False, timeout=N` precisely because it wants to handle the
    failure -- a `docker stop` that hangs inside `run_probe` has to become a
    failed ProbeOutcome the ledger can record and resume past, not a
    `TimeoutExpired` unwinding a 15-hour cell.
    """
    env = None
    if extra_env:
        env = {**os.environ, **extra_env}
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env=env,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired carries whatever was captured before the kill; with
        # text=True those are already str, but the stubs allow bytes.
        partial_out = _as_text(exc.stdout)
        partial_err = _as_text(exc.stderr)
        result = ProcResult(
            args=list(args),
            returncode=TIMEOUT_RETURNCODE,
            stdout=partial_out,
            stderr=(
                f"{partial_err}\ntimed out after {timeout}s: {' '.join(args)}"
                if partial_err
                else f"timed out after {timeout}s: {' '.join(args)}"
            ),
        )
        if check:
            raise RuntimeError(result.stderr.strip()) from exc
        return result
    result = ProcResult(
        args=list(args),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if check and not result.ok:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def _as_text(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else raw.decode("utf-8", "replace")


def which(name: str) -> str | None:
    """Locate an executable on PATH (cross-platform)."""
    import shutil

    return shutil.which(name)
