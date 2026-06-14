#!/usr/bin/env python3
"""Run or print CADO-NFS RSA-460 parameter sweeps."""

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


def _param(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return f"{name}={value}"


def build_cado_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        str(args.cado),
        "--client-threads",
        str(args.client_threads),
        "--server-threads",
        str(args.server_threads),
        "--slaves",
        str(args.slaves),
        str(args.n),
    ]
    params = [
        _param("name", args.label),
        _param("workdir", args.work_dir),
        _param("tasks.polyselect.import", args.poly),
        _param("lim0", args.lim0),
        _param("lim1", args.lim1),
        _param("lpb0", args.lpb0),
        _param("lpb1", args.lpb1),
        _param("mfb0", args.mfb0),
        _param("mfb1", args.mfb1),
        _param("lambda0", args.lambda0),
        _param("lambda1", args.lambda1),
        _param("I", args.I),
        _param("tasks.sieve.rels_wanted", args.rels_wanted),
        _param("tasks.filter.target_density", args.target_density),
        _param("tasks.linalg.bwc.threads", args.linalg_threads),
    ]
    if args.qmin is not None:
        params.append(_param("tasks.sieve.qmin", args.qmin))
    if args.qrange is not None:
        params.append(_param("tasks.sieve.qrange", args.qrange))
    if args.allow_compsq:
        params.extend(
            [
                "tasks.sieve.allow_compsq=true",
                _param("tasks.sieve.qfac_min", args.qfac_min),
                _param("tasks.sieve.qfac_max", args.qfac_max),
            ]
        )
    params.extend(args.extra_param)
    cmd.extend(p for p in params if p)
    return [str(part) for part in cmd]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run CADO-NFS with c140def-style defaults and write a machine-readable "
            "summary. Use --dry-run to print exact operator commands."
        )
    )
    parser.add_argument("--cado", type=Path, required=True, help="Path to cado-nfs.py")
    parser.add_argument("--n", required=True, help="Semiprime to factor")
    parser.add_argument("--poly", type=Path, required=True, help="Imported tuned CADO .poly file")
    parser.add_argument("--label", default="c140def", help="CADO name= label")
    parser.add_argument("--work-dir", type=Path, default=Path("rsa460_cado_work"), help="CADO workdir")
    parser.add_argument("--log", type=Path, help="Log file path")

    parser.add_argument("--client-threads", type=int, default=1)
    parser.add_argument("--server-threads", type=int, default=24)
    parser.add_argument("--slaves", type=int, default=24)
    parser.add_argument("--linalg-threads", type=int, default=24)

    parser.add_argument("--lim0", type=int, default=11_000_000)
    parser.add_argument("--lim1", type=int, default=14_000_000)
    parser.add_argument("--lpb0", type=int, default=30)
    parser.add_argument("--lpb1", type=int, default=30)
    parser.add_argument("--mfb0", type=int, default=60)
    parser.add_argument("--mfb1", type=int, default=60)
    parser.add_argument("--lambda0", type=float, default=1.1)
    parser.add_argument("--lambda1", type=float, default=1.1)
    parser.add_argument("--I", type=int, default=13)
    parser.add_argument("--rels-wanted", type=int, default=71_000_000)
    parser.add_argument("--target-density", type=int, default=125)
    parser.add_argument("--qmin", type=int)
    parser.add_argument("--qrange", type=int)

    parser.add_argument("--allow-compsq", action="store_true", help="Enable composite special-q testing")
    parser.add_argument("--qfac-min", type=int, default=50)
    parser.add_argument("--qfac-max", type=int)
    parser.add_argument("--extra-param", action="append", default=[], help="Additional raw CADO key=value parameter")
    parser.add_argument("--dry-run", action="store_true", help="Print command instead of running it")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cmd = build_cado_command(args)
    if args.dry_run:
        print(" ".join(cmd))
        return

    args.work_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log or (args.work_dir / f"{args.label}.log")
    start = time.monotonic()
    with log_path.open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.run(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
            check=False,
        )
    elapsed = time.monotonic() - start
    parsed = parse_logs([log_path])
    summary = {
        "label": args.label,
        "command": cmd,
        "exit_code": proc.returncode,
        "wall_seconds": elapsed,
        "host": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "log": str(log_path),
        "parsed": parsed,
    }
    summary_path = args.work_dir / f"{args.label}.summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
