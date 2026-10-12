"""Docker/kernel cpuset list syntax: `"8-10,14"` <-> [8, 9, 10, 14].

Lives here, not in `capacity_gate`, because the host read-back compares cpu
SETS rather than strings: `/proc/cmdline` may say `isolcpus=0,1,2,3,4,5` where
the compose overlay says `0-5`, and the `grep -qw` in host-setup.sh's
`check_isolate` calls those two different. Importing the capacity gate to
compare two cpu lists would pull config/vegeta/manifest into a sysfs parser.
"""

from __future__ import annotations


def parse_cpuset(spec: str) -> list[int]:
    """`"8-10,14"` -> [8, 9, 10, 14] (docker cpuset syntax, ascending, unique)."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return sorted(out)


def format_cpuset(ids: list[int]) -> str:
    """[8, 9, 10, 14] -> `"8-10,14"` (contiguous runs collapsed)."""
    ids = sorted(set(ids))
    if not ids:
        raise ValueError("empty cpuset")
    runs: list[str] = []
    start = prev = ids[0]
    for cpu in ids[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = cpu
    runs.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(runs)
