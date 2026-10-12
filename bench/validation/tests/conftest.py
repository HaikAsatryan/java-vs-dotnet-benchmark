"""Validation-suite fixtures.

The pure-logic tests (normalizer rules, golden self-consistency, shapecheck)
run with NO docker and NO services. The live equivalence tests skip cleanly
unless EVERY slot's base-URL env var is set (`registry.Slot.url_env`). No test
here carries the `docker` marker; the autouse fixture below skips one that does
when no daemon is reachable.

There is no `services` fixture: `test_equivalence._services_or_skip` is the one
SUT-discovery path, so "which services are live" has a single answer.
"""

from __future__ import annotations

import pytest

from ledgerbench import compose


@pytest.fixture(autouse=True)
def _skip_docker_without_daemon(request):
    if request.node.get_closest_marker("docker") and not compose.docker_available():
        pytest.skip("docker daemon not available")
