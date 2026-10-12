"""Analysis-suite fixtures.

The analysis modules depend on the scientific stack (numpy/scipy/pandas), which
`uv sync` installs. In the unusual case that it is absent, these test modules
must not even be COLLECTED, because they
import numpy at module top level and collection happens before any skip marker
can run. `collect_ignore_glob` drops them entirely with a visible reason; with
the stack installed they collect and run normally. Run `uv sync` to install
it.
"""

from __future__ import annotations

import importlib.util

_ANALYSIS_DEPS = ("numpy", "scipy", "pandas")
_MISSING = [m for m in _ANALYSIS_DEPS if importlib.util.find_spec(m) is None]

# When the scientific stack is absent, ignore every test module in this package.
collect_ignore_glob = ["test_*.py"] if _MISSING else []


def pytest_report_header(config):
    if _MISSING:
        return (
            f"analysis tests SKIPPED (collection-ignored): missing "
            f"{', '.join(_MISSING)} -- run `uv sync` to install them"
        )
    return None
