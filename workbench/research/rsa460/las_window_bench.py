#!/usr/bin/env python3
"""Run short, repeatable CADO las windows for CPU/build A/B tests."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

from .parse_cado_logs import parse_logs


DEFAULTS = {
    "lim0": 11_000_000,
    "lim1": 14_000_000,
    "lpb0": 30,
    "lpb1": 30,
    "mfb0": 60,
    "mfb1": 60,
    "lambda0": 1.1,
    "lambda1": 1.1,
    "I": 13,
    "sqside": 1,
}


def _append_option(cmd: list[str], name: str, value: Any) -> None:
    if value is None:
        return
    cmd.extend([f"-{name}", str(value)])


def build_las_command(args: argparse.Namespace) -> list[str]:
    cmd = [str(args.las), "-poly", str(args.poly), "-q0", str(args.q0), "-q1", str(args.q1)]
    for name in ("lim0", "lim1", "lpb0", "lpb1", "mfb0", "mfb1", "lambda0", "lambda1", "I", "sqside"):
        _append_option(cmd, name, getattr(args, name))
    _append_option(cmd, "t", args.threads)
    for extra in args.extra_arg:
        cmd.append(extra)
    return cmd


def run_once(cmd: list[str], log_path: Path, env: dict[str, str]) -> dict[str, Any]:
    start = time.monotonic()
    with log_path.open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.run(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            check=False,
        )
    elapsed = time.monotonic() - start
    parsed = parse_logs([log_path])
    relations = parsed["max_relations_seen"]
    return {
        "command": cmd,
        "exit_code": proc.returncode,
        "wall_seconds": elapsed,
        "relations": relations,
        "relations_per_second_wall": (relations / elapsed) if relations and elapsed > 0 else None,
        "log": str(log_path),
        "parsed": parsed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark one fixed CADO las special-q window. Use identical q0/q1 "
            "and polynomial across machines/builds; compare relations/s and wall."
        )
    )
    parser.add_argument("--las", type=Path, required=True, help="Path to CADO las binary")
    parser.add_argument("--poly", type=Path, required=True, help="Path to tuned .poly file")
    parser.add_argument("--label", default="las-window", help="Label stored in output JSON")
    parser.add_argument("--out-dir", type=Path, default=Path("rsa460_las_bench"), help="Output directory")
    parser.add_argument("--q0", type=int, required=True, help="First special-q")
    parser.add_argument("--q1", type=int, required=True, help="End special-q")
    parser.add_argument("--threads", type=int, default=24, help="las worker threads")
    parser.add_argument("--repeat", type=int, default=3, help="Number of repetitions")
    parser.add_argument("--extra-arg", action="append", default=[], help="Additional raw las argument")

    for name, value in DEFAULTS.items():
        value_type = float if isinstance(value, float) else int
        parser.add_argument(f"--{name}", type=value_type, default=value, help=f"las -{name} value")

    parser.add_argument("--dry-run", action="store_true", help="Print the command and exit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cmd = build_las_command(args)
    if args.dry_run:
        print(" ".join(cmd))
        return

    args.out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    results: list[dict[str, Any]] = []

    for idx in range(args.repeat):
        log_path = args.out_dir / f"{args.label}.rep{idx + 1}.log"
        result = run_once(cmd, log_path, env)
        result.update(
            {
                "label": args.label,
                "repeat_index": idx + 1,
                "host": platform.node(),
                "platform": platform.platform(),
                "processor": platform.processor(),
            }
        )
        results.append(result)
        print(json.dumps(result, sort_keys=True))

    valid_rates = [r["relations_per_second_wall"] for r in results if r["relations_per_second_wall"]]
    summary = {
        "label": args.label,
        "runs": results,
        "best_relations_per_second_wall": max(valid_rates) if valid_rates else None,
        "mean_relations_per_second_wall": (sum(valid_rates) / len(valid_rates)) if valid_rates else None,
    }
    (args.out_dir / f"{args.label}.summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
