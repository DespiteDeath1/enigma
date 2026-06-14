#!/usr/bin/env python3
"""Quantitative wall model for RSA-460 Granite Rapids feasibility.

The defaults are the user's measured best 6767P stack.  Throughput units are
intentionally generic: use rel/s, sq/s, or any fixed window throughput as long
as numerator and denominator use the same unit.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class WallModel:
    current_wall_min: float
    target_wall_min: float
    current_throughput: float
    sieve_fraction: float
    non_sieve_min: float
    current_sieve_min: float
    required_sieve_min: float
    required_total_speedup: float
    required_total_throughput: float
    required_sieve_speedup: float
    required_sieve_throughput: float
    sieve_time_cut_fraction: float
    cpu_sieve_offload_fraction: float
    bandwidth_traffic_cut_fraction_if_bw_bound: float


def compute_model(
    current_wall_min: float,
    target_wall_min: float,
    current_throughput: float,
    sieve_fraction: float,
) -> WallModel:
    non_sieve_min = current_wall_min * (1.0 - sieve_fraction)
    current_sieve_min = current_wall_min * sieve_fraction
    required_sieve_min = target_wall_min - non_sieve_min
    if required_sieve_min <= 0:
        raise ValueError("target wall is below non-sieve time; impossible without cutting non-sieve")

    required_total_speedup = current_wall_min / target_wall_min
    required_sieve_speedup = current_sieve_min / required_sieve_min
    sieve_time_cut_fraction = 1.0 - required_sieve_min / current_sieve_min

    return WallModel(
        current_wall_min=current_wall_min,
        target_wall_min=target_wall_min,
        current_throughput=current_throughput,
        sieve_fraction=sieve_fraction,
        non_sieve_min=non_sieve_min,
        current_sieve_min=current_sieve_min,
        required_sieve_min=required_sieve_min,
        required_total_speedup=required_total_speedup,
        required_total_throughput=current_throughput * required_total_speedup,
        required_sieve_speedup=required_sieve_speedup,
        required_sieve_throughput=current_throughput * required_sieve_speedup,
        sieve_time_cut_fraction=sieve_time_cut_fraction,
        cpu_sieve_offload_fraction=sieve_time_cut_fraction,
        bandwidth_traffic_cut_fraction_if_bw_bound=sieve_time_cut_fraction,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute required RSA-460 speedup/offload thresholds")
    parser.add_argument("--current-wall-min", type=float, default=321.0)
    parser.add_argument("--target-wall-min", type=float, default=240.0)
    parser.add_argument("--current-throughput", type=float, default=63.68)
    parser.add_argument("--sieve-fraction", type=float, default=0.90)
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    model = compute_model(
        current_wall_min=args.current_wall_min,
        target_wall_min=args.target_wall_min,
        current_throughput=args.current_throughput,
        sieve_fraction=args.sieve_fraction,
    )
    text = json.dumps(asdict(model), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
