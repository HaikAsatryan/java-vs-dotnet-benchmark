"""Time helpers: UTC RFC3339 with exactly 6 fractional digits, and monotonic.

The wire/logging contract (spec/openapi.yaml, spec/logging.md) freezes the
timestamp format at RFC3339 UTC, EXACTLY 6 fractional digits, 'Z' suffix. The
runner emits its own timestamps (manifest, ledger, runtime-stats snapshots) in
the same format so artifacts read uniformly.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def rfc3339_micros(dt: datetime | None = None) -> str:
    """RFC3339 UTC, exactly 6 fractional digits, trailing 'Z'.

    Mirrors the frozen wire format so runner-emitted timestamps line up with
    service-emitted ones byte-for-byte.
    """
    if dt is None:
        dt = utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    # %f always yields 6 digits; strip the +00:00 offset and force Z.
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def monotonic() -> float:
    """Monotonic clock in seconds; for warmup windows and sampler cadence."""
    return time.monotonic()
