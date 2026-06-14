#!/usr/bin/env python3
"""Independent wall-gap derivation from measured throughput points."""

from __future__ import annotations

import argparse


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--intel-best-wall", type=float, required=True, help="Measured/proj best Intel wall in minutes")
    p.add_argument("--target-wall", type=float, default=240.0)
    p.add_argument("--baseline-rel", type=float, required=True, help="Baseline rel/s")
    p.add_argument("--best-rel", type=float, required=True, help="Best rel/s after known stack")
    p.add_argument("--sieve-fraction", type=float, default=0.89)
    args = p.parse_args()

    # Required multiplicative reduction in full wall.
    full_reduction = args.intel_best_wall / args.target_wall

    # Throughput gain needed when only sieve scales.
    fixed = args.intel_best_wall * (1.0 - args.sieve_fraction)
    sieve = args.intel_best_wall * args.sieve_fraction
    # target = fixed + sieve / g  -> g = sieve / (target - fixed)
    need_gain_from_best = sieve / (args.target_wall - fixed)
    need_rel_from_best = args.best_rel * need_gain_from_best
    need_rel_from_base = args.baseline_rel * need_gain_from_best * (args.best_rel / args.baseline_rel)

    print(f"intel_best_wall_min={args.intel_best_wall:.3f}")
    print(f"target_wall_min={args.target_wall:.3f}")
    print(f"sieve_fraction={args.sieve_fraction:.3f}")
    print(f"full_wall_reduction_needed_x={full_reduction:.4f}")
    print(f"required_sieve_gain_from_best_x={need_gain_from_best:.4f}")
    print(f"required_sieve_gain_from_best_pct={(need_gain_from_best - 1.0) * 100.0:.2f}")
    print(f"required_rel_per_s_from_best={need_rel_from_best:.4f}")
    print(f"baseline_rel_per_s={args.baseline_rel:.4f}")
    print(f"best_rel_per_s={args.best_rel:.4f}")
    print(f"best_vs_baseline_pct={(args.best_rel / args.baseline_rel - 1.0) * 100.0:.2f}")
    print(f"additional_rel_gain_needed_vs_best_pct={(need_rel_from_best / args.best_rel - 1.0) * 100.0:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
