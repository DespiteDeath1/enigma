#!/usr/bin/env python3
"""Topology-aware Granite Rapids las benchmark matrix.

The validator uses a CFS CPU quota (`--cpus 24`) rather than a cpuset.  This
runner explicitly pins the benchmark process to compact logical CPUs so CADO's
workers do not migrate across the whole host while comparing build variants and
SMT levels.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def parse_cpu_list(text: str) -> list[int]:
    cpus: list[int] = []
    for part in text.strip().split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return sorted(set(cpus))


def format_cpu_list(cpus: Iterable[int]) -> str:
    values = sorted(set(cpus))
    if not values:
        return ""
    ranges: list[str] = []
    start = prev = values[0]
    for cpu in values[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = cpu
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def online_cpus() -> list[int]:
    online = read_text(Path("/sys/devices/system/cpu/online"))
    if online:
        return parse_cpu_list(online)
    return list(range(os.cpu_count() or 1))


def cgroup_cpu_quota() -> float | None:
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    if cpu_max.exists():
        quota_s, period_s = read_text(cpu_max, "max 100000").split()[:2]
        if quota_s != "max":
            return int(quota_s) / int(period_s)
    quota = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota.exists() and period.exists():
        quota_v = int(read_text(quota, "-1"))
        if quota_v > 0:
            return quota_v / int(read_text(period, "100000"))
    return None


@dataclass(frozen=True)
class CpuInfo:
    cpu: int
    package: str
    die: str
    core: str
    siblings: tuple[int, ...]
    llc: tuple[int, ...]


def cpu_info(cpu: int) -> CpuInfo:
    root = Path(f"/sys/devices/system/cpu/cpu{cpu}")
    topo = root / "topology"
    cache = root / "cache"
    siblings = parse_cpu_list(read_text(topo / "thread_siblings_list", str(cpu)))
    llc = siblings
    for index in sorted(cache.glob("index*")):
        if read_text(index / "type") == "Unified":
            level = read_text(index / "level")
            if level == "3":
                llc = parse_cpu_list(read_text(index / "shared_cpu_list", str(cpu)))
                break
    return CpuInfo(
        cpu=cpu,
        package=read_text(topo / "physical_package_id", "0"),
        die=read_text(topo / "die_id", "0"),
        core=read_text(topo / "core_id", str(cpu)),
        siblings=tuple(siblings),
        llc=tuple(llc),
    )


def compact_cpus(logical_threads: int, prefer_llc: int | None = None) -> list[int]:
    infos = {cpu: cpu_info(cpu) for cpu in online_cpus()}
    llc_groups: dict[tuple[int, ...], list[int]] = {}
    for cpu, info in infos.items():
        llc_groups.setdefault(info.llc, []).append(cpu)

    groups = sorted(llc_groups.values(), key=lambda xs: (-len(xs), min(xs)))
    if prefer_llc is not None:
        groups = [g for g in groups if prefer_llc in g] + [g for g in groups if prefer_llc not in g]

    chosen_group = groups[0]
    core_groups: dict[tuple[int, ...], list[int]] = {}
    for cpu in sorted(chosen_group):
        sibs = tuple(x for x in infos[cpu].siblings if x in chosen_group)
        core_groups.setdefault(sibs, []).append(cpu)

    ordered_cores = sorted((sorted(v) for v in core_groups.values()), key=lambda xs: xs[0])
    selected: list[int] = []

    # First fill one logical thread per physical core, then add SMT siblings.
    for core in ordered_cores:
        if len(selected) >= logical_threads:
            break
        selected.append(core[0])
    sibling_round = 1
    while len(selected) < logical_threads:
        added = False
        for core in ordered_cores:
            if len(selected) >= logical_threads:
                break
            if sibling_round < len(core):
                selected.append(core[sibling_round])
                added = True
        if not added:
            break
        sibling_round += 1

    return sorted(selected[:logical_threads])


def parse_las_variant(value: str) -> tuple[str, Path]:
    if "=" not in value:
        path = Path(value)
        return path.parent.parent.name or path.name, path
    label, path = value.split("=", 1)
    return label, Path(path)


def run_command(cmd: list[str], dry_run: bool) -> int:
    print("$ " + " ".join(cmd), flush=True)
    if dry_run:
        return 0
    return subprocess.run(cmd, check=False).returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run pinned CADO las build x SMT benchmark matrix")
    parser.add_argument("--poly", type=Path, required=True, help="CADO .poly file")
    parser.add_argument("--q0", type=int, required=True)
    parser.add_argument("--q1", type=int, required=True)
    parser.add_argument("--las", action="append", required=True, help="label=/path/to/las; repeatable")
    parser.add_argument("--threads", default="24,32,48", help="Comma-separated las -t values")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out-dir", type=Path, default=Path("/dev/shm/rsa460-gnr-matrix"))
    parser.add_argument("--prefer-llc-cpu", type=int, help="Choose the LLC/SNC group containing this CPU first")
    parser.add_argument("--no-taskset", action="store_true", help="Do not prepend taskset")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--extra-las-arg", action="append", default=[], help="Extra raw las arg")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    quota = cgroup_cpu_quota()
    variants = [parse_las_variant(v) for v in args.las]
    thread_counts = [int(x) for x in args.threads.split(",") if x]

    manifest = {
        "host": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cgroup_cpu_quota": quota,
        "online_cpus": format_cpu_list(online_cpus()),
        "runs": [],
    }

    for threads in thread_counts:
        cpus = compact_cpus(threads, args.prefer_llc_cpu)
        cpu_list = format_cpu_list(cpus)
        if len(cpus) < threads:
            print(f"warning: requested {threads} logical CPUs but selected only {len(cpus)}", file=sys.stderr)
        for label, las in variants:
            run_label = f"{label}-t{threads}-cpus{cpu_list.replace(',', '_').replace('-', 'to')}"
            cmd = [
                sys.executable,
                "-m",
                "workbench.research.rsa460.las_window_bench",
                "--las",
                str(las),
                "--poly",
                str(args.poly),
                "--q0",
                str(args.q0),
                "--q1",
                str(args.q1),
                "--threads",
                str(threads),
                "--repeat",
                str(args.repeat),
                "--label",
                run_label,
                "--out-dir",
                str(args.out_dir),
            ]
            for extra in args.extra_las_arg:
                cmd.extend(["--extra-arg", extra])
            if not args.no_taskset:
                cmd = ["taskset", "-c", cpu_list] + cmd
            manifest["runs"].append({"label": run_label, "threads": threads, "cpus": cpu_list, "las": str(las)})
            rc = run_command(cmd, args.dry_run)
            if rc != 0:
                print(f"run failed with exit code {rc}: {run_label}", file=sys.stderr)
                if not args.dry_run:
                    raise SystemExit(rc)

    (args.out_dir / "matrix-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
