"""bench/analysis: statistics, knee fit, report.

Sibling package of `ledgerbench`. These modules depend on the scientific stack
(numpy/scipy/pandas), which `uv sync` installs. Importing a submodule pulls its
heavy deps lazily; this package __init__ stays import-light so `import analysis`
alone never pays for them.

All public functions are pure and typed: they take arrays/DataFrames and return
values, with no I/O except report.py, which writes markdown. This package
produces no figures; `analyze` emits `report.md` + `fits.json` and nothing else.
"""
