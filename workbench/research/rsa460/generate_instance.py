#!/usr/bin/env python3
"""Generate deterministic Breaking RSA benchmark instances.

This is intended for benchmarking only.  It uses the public challenge
generator so AMD/GNR A/B runs can share the exact same semiprime.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from qbittensor.challenges.breaking_rsa import BreakingRSA


def generate_instance(bits: int, seed: int, difficulty: int) -> dict[str, Any]:
    challenge = BreakingRSA(difficulty=difficulty, num_bits=bits)
    problem, verif = challenge.generate(seed)
    p = int(verif.p)
    q = int(verif.q)
    n = int(problem.num)
    return {
        "difficulty": int(problem.difficulty),
        "seed": int(seed),
        "num_bits": int(problem.num_bits),
        "digits": len(str(n)),
        "n": n,
        "p": p,
        "q": q,
        "p_bits": p.bit_length(),
        "q_bits": q.bit_length(),
        "abs_p_minus_q_bits": abs(p - q).bit_length(),
        "fermat_gap_min_bits": max(bits // 2 - 100, 1),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a deterministic Breaking RSA semiprime for CADO/msieve "
            "benchmarking.  Do not ship the emitted factors inside solver images."
        )
    )
    parser.add_argument("--bits", type=int, default=460, help="Semiprime bit width")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic generator seed")
    parser.add_argument("--difficulty", type=int, default=460, help="Challenge difficulty label")
    parser.add_argument("--out", type=Path, help="Optional JSON output path")
    parser.add_argument(
        "--no-factors",
        action="store_true",
        help="Omit p and q from the JSON output for solver-facing artifacts",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    instance = generate_instance(args.bits, args.seed, args.difficulty)
    if args.no_factors:
        instance = {key: value for key, value in instance.items() if key not in {"p", "q"}}

    text = json.dumps(instance, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
