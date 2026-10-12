"""HTTP client config for the equivalence suite, driven by env.

Each slot of the active slot set declares its own base-URL env var
(`registry.Slot.url_env`). When any of them is unset the equivalence tests skip
cleanly with a clear reason naming the missing vars (the suite is
pure-importable and CI-green with no services running; it only exercises the
live services when the env points at them, i.e. during smoke/integration).

The client is a thin httpx wrapper that returns (status, headers, parsed-json)
and never raises on non-2xx (the suite asserts on status explicitly).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from ledgerbench import registry

# Default per-request timeout for equivalence calls (generous; not a perf path).
DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Service:
    """One service under test, identified by its SLOT id."""

    name: str        # a registry slot id
    base_url: str    # e.g. http://127.0.0.1:8082


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    json_body: object | None
    text: str

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


def configured_services() -> list[Service]:
    """The active slot set's services whose base-URL env var is set.

    Order follows the slot set, so the cross-compare below always uses the same
    slot as its baseline within a run.
    """
    out: list[Service] = []
    for slot_id in registry.active_slot_set().slots:
        url = os.environ.get(registry.get_slot(slot_id).url_env)
        if url:
            out.append(Service(name=slot_id, base_url=url.rstrip("/")))
    return out


def skip_reason() -> str | None:
    """A clear skip message when the suite cannot run live, else None.

    Names the env vars that are missing rather than counting services, so the
    message holds for any slot-set size and tells an operator which URL to set.
    """
    have = {s.name for s in configured_services()}
    missing = [
        registry.get_slot(sid).url_env
        for sid in registry.active_slot_set().slots
        if sid not in have
    ]
    if missing:
        return (
            f"equivalence suite needs every slot live; unset: "
            f"{', '.join(missing)} (set them to the SUT base URLs to run)"
        )
    return None


def request(
    svc: Service,
    method: str,
    path: str,
    *,
    json_body: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Response:
    """Issue one request to a service; never raises on HTTP status."""
    url = f"{svc.base_url}{path}"
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method, url, json=json_body, headers=headers)
    parsed: object | None
    try:
        parsed = resp.json()
    except Exception:
        parsed = None
    return Response(
        status=resp.status_code,
        headers={k.lower(): v for k, v in resp.headers.items()},
        json_body=parsed,
        text=resp.text,
    )
