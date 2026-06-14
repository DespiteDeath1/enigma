#!/usr/bin/env python3
"""Set CPU affinity inside a quota-only container, then exec a command."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .run_gnr_matrix import compact_cpus, format_cpu_list, parse_cpu_list


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Set sched affinity for the current process and exec a command. "
            "Use this when Docker was launched with --cpus but no cpuset."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--cpu-list", help="Explicit CPU list, e.g. 0-23 or 0-23,64-87")
    group.add_argument("--compact", type=int, help="Select this many compact logical CPUs")
    parser.add_argument("--prefer-llc-cpu", type=int, help="Prefer the LLC/SNC group containing this CPU")
    parser.add_argument("--write-env", type=Path, help="Write selected CPU list to this file and exit")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to exec after --")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.cpu_list:
        cpus = parse_cpu_list(args.cpu_list)
    else:
        cpus = compact_cpus(args.compact, args.prefer_llc_cpu)

    cpu_list = format_cpu_list(cpus)
    if args.write_env:
        args.write_env.parent.mkdir(parents=True, exist_ok=True)
        args.write_env.write_text(cpu_list + "\n")
        return

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing command to exec")

    os.sched_setaffinity(0, set(cpus))
    os.environ["RSA460_PINNED_CPUS"] = cpu_list
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
