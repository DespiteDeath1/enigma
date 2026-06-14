#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def parse_cpu_list(text: str) -> list[int]:
    cpus: list[int] = []
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return sorted(set(cpus))


def format_cpu_list(cpus: Iterable[int]) -> str:
    data = sorted(set(cpus))
    if not data:
        return ""
    ranges: list[str] = []
    start = prev = data[0]
    for cpu in data[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = cpu
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def quota_cpu_count() -> int | None:
    cpu_max = read_text(Path("/sys/fs/cgroup/cpu.max"))
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) >= 2 and parts[0] != "max":
            quota = int(parts[0])
            period = int(parts[1])
            if quota > 0 and period > 0:
                return max(1, math.ceil(quota / period))

    quota_text = read_text(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
    period_text = read_text(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
    if quota_text and period_text:
        quota = int(quota_text)
        period = int(period_text)
        if quota > 0 and period > 0:
            return max(1, math.ceil(quota / period))
    return None


def sched_affinity_cpus() -> list[int]:
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))
    return list(range(os.cpu_count() or 1))


def node_cpulists() -> dict[int, list[int]]:
    result: dict[int, list[int]] = {}
    node_root = Path("/sys/devices/system/node")
    for node_dir in sorted(node_root.glob("node[0-9]*")):
        try:
            node_id = int(node_dir.name[4:])
        except ValueError:
            continue
        cpulist = read_text(node_dir / "cpulist")
        if cpulist:
            result[node_id] = parse_cpu_list(cpulist)
    return result


def thread_siblings(cpu: int) -> list[int]:
    text = read_text(Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"))
    if not text:
        return [cpu]
    return parse_cpu_list(text)


@dataclass(frozen=True)
class CoreGroup:
    node: int
    siblings: tuple[int, ...]


def build_core_groups(eligible_cpus: set[int]) -> list[CoreGroup]:
    nodes = node_cpulists()
    cpu_to_node: dict[int, int] = {}
    for node_id, cpus in nodes.items():
        for cpu in cpus:
            cpu_to_node[cpu] = node_id

    groups: dict[tuple[int, ...], CoreGroup] = {}
    for cpu in sorted(eligible_cpus):
        sibs = tuple(sorted(c for c in thread_siblings(cpu) if c in eligible_cpus))
        if not sibs:
            sibs = (cpu,)
        if sibs in groups:
            continue
        node = min((cpu_to_node.get(c, 0) for c in sibs), default=0)
        groups[sibs] = CoreGroup(node=node, siblings=sibs)

    return sorted(groups.values(), key=lambda g: (g.node, g.siblings[0]))


def choose_compact(groups: list[CoreGroup], count: int, smt: bool) -> tuple[list[int], list[int]]:
    selected_groups: list[CoreGroup] = []
    remaining = count
    for group in groups:
        if remaining <= 0:
            break
        selected_groups.append(group)
        remaining -= 1

    chosen: list[int] = [g.siblings[0] for g in selected_groups]
    if smt and len(chosen) < count:
        for sibling_index in range(1, max((len(g.siblings) for g in selected_groups), default=1)):
            for group in selected_groups:
                if sibling_index < len(group.siblings):
                    chosen.append(group.siblings[sibling_index])
                    if len(chosen) >= count:
                        break
            if len(chosen) >= count:
                break

    nodes_used = sorted({g.node for g in selected_groups})
    return chosen[:count], nodes_used


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Select a compact CPU set from visible host CPUs, respecting cgroup quota by default."
    )
    parser.add_argument("--count", type=int, default=None, help="Target logical CPU count. Default: detect from cgroup quota.")
    parser.add_argument(
        "--strategy",
        choices=("physical-compact", "smt-compact"),
        default="physical-compact",
        help="Whether to choose one thread per core or include SMT siblings after filling physical cores.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of a cpuset string.")
    parser.add_argument("--verbose", action="store_true", help="Emit human-readable metadata.")
    args = parser.parse_args()

    eligible = set(sched_affinity_cpus())
    quota_count = quota_cpu_count()
    target = args.count or quota_count or len(eligible)
    target = max(1, min(target, len(eligible)))

    groups = build_core_groups(eligible)
    selected, nodes_used = choose_compact(groups, target, smt=(args.strategy == "smt-compact"))
    payload = {
        "strategy": args.strategy,
        "target_logical_cpus": target,
        "host_visible_cpus": len(eligible),
        "quota_cpus": quota_count,
        "physical_core_groups_visible": len(groups),
        "nodes_used": nodes_used,
        "cpus": selected,
        "cpuset": format_cpu_list(selected),
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.verbose:
        for key in (
            "strategy",
            "target_logical_cpus",
            "host_visible_cpus",
            "quota_cpus",
            "physical_core_groups_visible",
            "nodes_used",
            "cpuset",
        ):
            print(f"{key}={payload[key]}")
    else:
        print(payload["cpuset"])


if __name__ == "__main__":
    main()
