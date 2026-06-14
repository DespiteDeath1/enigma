#!/usr/bin/env python3
"""Project full-wall time from measured sieve throughput deltas.

Assumes sieve dominates wall and scales ~inversely with rel/sq (or chosen metric).
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True, help="results.csv from run_matrix.py")
    p.add_argument("--baseline-name", required=True)
    p.add_argument("--metric", default="rels_per_sq")
    p.add_argument("--intel-baseline-min", type=float, default=293.0)
    p.add_argument("--amd-baseline-min", type=float, default=215.0)
    p.add_argument("--sieve-fraction", type=float, default=0.89)
    args = p.parse_args()

    rows = list(csv.DictReader(Path(args.csv).open("r", encoding="utf-8")))
    if not rows:
        raise RuntimeError("empty CSV")

    base = None
    for r in rows:
        if r.get("name") == args.baseline_name:
            base = r
            break
    if base is None:
        raise RuntimeError(f"baseline name '{args.baseline_name}' not found")

    b = float(base[args.metric])

    fixed_intel = args.intel_baseline_min * (1.0 - args.sieve_fraction)
    fixed_amd = args.amd_baseline_min * (1.0 - args.sieve_fraction)
    sieve_intel = args.intel_baseline_min * args.sieve_fraction
    sieve_amd = args.amd_baseline_min * args.sieve_fraction

    print("name,metric,delta_pct,proj_intel_min,proj_amd_min")
    for r in rows:
        try:
            m = float(r[args.metric])
        except (KeyError, ValueError):
            continue
        scale = b / m
        proj_intel = fixed_intel + sieve_intel * scale
        proj_amd = fixed_amd + sieve_amd * scale
        delta = (m / b - 1.0) * 100.0
        print(f"{r['name']},{m:.6f},{delta:.3f},{proj_intel:.2f},{proj_amd:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
