"""SUT readiness polling.

Per-probe lifecycle lives in `live_runner.LiveProbeRunner`; slot topology in
`ledgerbench.registry`.
"""

from __future__ import annotations

import time

import httpx


def wait_ready(base_url: str, *, timeout_s: float = 180.0, interval_s: float = 0.5) -> None:
    """Poll GET /readyz until 200 (DB pool + Redis PING) or timeout.

    /readyz (not /healthz) is the app-level readiness gate: it checks the DB pool
    and Redis PING (spec/openapi.yaml). compose `up --wait` covers container
    health; this covers app readiness before warmup starts.

    The default matches the live runner's own readiness budget.
    """
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/readyz")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError as exc:  # not ready yet
                last_err = exc
            time.sleep(interval_s)
    raise TimeoutError(
        f"SUT at {base_url} not ready within {timeout_s}s"
        + (f" (last error: {last_err})" if last_err else "")
    )
