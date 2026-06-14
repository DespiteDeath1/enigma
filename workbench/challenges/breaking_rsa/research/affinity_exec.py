#!/usr/bin/env python3
"""Pin and execute a command on a compact or spread CPU set.

This is designed for docker `--cpus` quota environments where cpuset is not
restricted and threads may migrate across many host NUMA/SNC domains.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CpuRec:
    cpu: int
    node: int
    socket: int
    core: int


def _run_json(cmd: list[str]) -> dict:
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def _load_topology() -> list[CpuRec]:
    # `lscpu -J --extended=CPU,NODE,SOCKET,CORE` gives a stable parse format.
    payload = _run_json(["lscpu", "-J", "--extended=CPU,NODE,SOCKET,CORE"])
    rows = payload.get("cpus", [])
    recs: list[CpuRec] = []
    for row in rows:
        # Keys are uppercase in lscpu JSON.
        try:
            cpu = int(row["cpu"])
            node = int(row["node"])
            socket = int(row["socket"])
            core = int(row["core"])
        except (KeyError, ValueError):
            continue
        if cpu >= 0 and node >= 0 and socket >= 0 and core >= 0:
            recs.append(CpuRec(cpu=cpu, node=node, socket=socket, core=core))
    if not recs:
        raise RuntimeError("No CPU topology records parsed from lscpu")
    return recs


def _cgroup_quota_cpus() -> int | None:
    # cgroup v2: cpu.max = "<quota> <period>" or "max <period>"
    path = "/sys/fs/cgroup/cpu.max"
    if not os.path.exists(path):
        return None
    raw = open(path, "r", encoding="utf-8").read().strip()
    parts = raw.split()
    if len(parts) != 2:
        return None
    quota, period = parts
    if quota == "max":
        return None
    try:
        q = int(quota)
        p = int(period)
        if q <= 0 or p <= 0:
            return None
        # ceil division
        return (q + p - 1) // p
    except ValueError:
        return None


def _iter_unique_cores(recs: Iterable[CpuRec]) -> list[CpuRec]:
    seen: set[tuple[int, int]] = set()
    out: list[CpuRec] = []
    for r in recs:
        k = (r.socket, r.core)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _compact_selection(recs: list[CpuRec], n_threads: int, use_smt: bool) -> list[int]:
    # Sort by (node, socket, core, cpu) to keep placement compact.
    ordered = sorted(recs, key=lambda r: (r.node, r.socket, r.core, r.cpu))
    if not use_smt:
        return [r.cpu for r in _iter_unique_cores(ordered)[:n_threads]]

    # SMT mode: fill one thread/core first, then siblings.
    primaries = _iter_unique_cores(ordered)
    selected: list[int] = [r.cpu for r in primaries[:n_threads]]
    if len(selected) >= n_threads:
        return selected[:n_threads]

    by_core: dict[tuple[int, int], list[int]] = {}
    for r in ordered:
        by_core.setdefault((r.socket, r.core), []).append(r.cpu)
    for p in primaries:
        siblings = [c for c in by_core[(p.socket, p.core)] if c != p.cpu]
        for s in siblings:
            selected.append(s)
            if len(selected) >= n_threads:
                return selected[:n_threads]
    return selected[:n_threads]


def _spread_selection(recs: list[CpuRec], n_threads: int, use_smt: bool) -> list[int]:
    # Round-robin by NUMA node to intentionally spread.
    by_node: dict[int, list[CpuRec]] = {}
    for r in sorted(recs, key=lambda x: (x.node, x.socket, x.core, x.cpu)):
        by_node.setdefault(r.node, []).append(r)
    nodes = sorted(by_node.keys())

    if not use_smt:
        for n in nodes:
            by_node[n] = _iter_unique_cores(by_node[n])

    selected: list[int] = []
    idx = 0
    while len(selected) < n_threads:
        progressed = False
        for n in nodes:
            arr = by_node[n]
            if idx < len(arr):
                selected.append(arr[idx].cpu)
                progressed = True
                if len(selected) >= n_threads:
                    break
        if not progressed:
            break
        idx += 1
    return selected[:n_threads]


def main() -> int:
    parser = argparse.ArgumentParser(description="Set CPU affinity and exec command")
    parser.add_argument("--mode", choices=["compact", "spread"], default="compact")
    parser.add_argument("--threads", type=int, default=0, help="Pinned thread count")
    parser.add_argument(
        "--threads-from-cgroup",
        action="store_true",
        help="Infer thread count from cgroup cpu.max quota",
    )
    parser.add_argument(
        "--smt",
        action="store_true",
        help="Allow selecting SMT siblings when needed",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print selected CPUs and exit",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    args = parser.parse_args()

    if args.threads_from_cgroup:
        q = _cgroup_quota_cpus()
        if q is None:
            raise RuntimeError("Could not infer cgroup quota CPUs from /sys/fs/cgroup/cpu.max")
        n_threads = q
    else:
        n_threads = args.threads
    if n_threads <= 0:
        raise RuntimeError("threads must be > 0 (or use --threads-from-cgroup)")

    recs = _load_topology()
    if args.mode == "compact":
        cpus = _compact_selection(recs, n_threads, args.smt)
    else:
        cpus = _spread_selection(recs, n_threads, args.smt)
    if len(cpus) < n_threads:
        raise RuntimeError(f"Requested {n_threads} CPUs, selected only {len(cpus)}")

    print(
        json.dumps(
            {
                "mode": args.mode,
                "threads": n_threads,
                "smt": args.smt,
                "selected_cpus": cpus,
            },
            indent=2,
        )
    )

    if args.print_only:
        return 0

    if not args.command:
        raise RuntimeError("No command provided. Use: affinity_exec.py ... -- <cmd>")
    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise RuntimeError("No command provided after --")

    os.sched_setaffinity(0, set(cpus))
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main())
