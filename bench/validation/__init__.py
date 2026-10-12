"""bench/validation: the cross-service equivalence suite.

Sibling package of `ledgerbench` in the same uv project. Pure-logic
modules (normalize.py, golden self-consistency) are importable with no Docker
and no running services; the live equivalence tests skip cleanly when their
env vars (each slot's `registry.Slot.url_env`) are absent.
"""
