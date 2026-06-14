#!/usr/bin/env python3
"""Estimate CADO bucket-update traffic savings from packed update layouts.

This is a static what-if model.  It does not claim runtime speedup; it answers
the narrower question: "even with perfect bandwidth scaling, can a proposed
bucket update compression remove enough bytes to explain a 4-hour GNR pass?"
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_UPDATE_BYTES = {
    "level1_shorthint": 4,
    "level1_emptyhint": 2,
    "level2_shorthint": 8,  # CADO TODO: 24-bit x is stored as uint32 + padding.
    "level2_emptyhint": 4,
    "level3_shorthint": 8,
    "level3_emptyhint": 4,
    "level1_longhint": 8,
    "level1_logphint": 4,
    "level2_longhint": 16,
    "level2_logphint": 8,
}


@dataclass
class TrafficScenario:
    fractions: dict[str, float]
    baseline_weighted_bytes: float
    packed_weighted_bytes: float
    traffic_cut_fraction: float
    required_cut_fraction: float
    passes_required_cut: bool


def parse_fraction(value: str) -> tuple[str, float]:
    key, raw = value.split("=", 1)
    if key not in DEFAULT_UPDATE_BYTES:
        raise argparse.ArgumentTypeError(f"unknown update kind {key!r}")
    return key, float(raw)


def scenario(
    fractions: dict[str, float],
    packed_level2_shorthint_bytes: float,
    packed_level2_longhint_bytes: float,
    required_cut_fraction: float,
) -> TrafficScenario:
    total = sum(fractions.values())
    if total <= 0:
        raise ValueError("fractions must sum to a positive value")
    normalized = {k: v / total for k, v in fractions.items()}
    packed = dict(DEFAULT_UPDATE_BYTES)
    packed["level2_shorthint"] = packed_level2_shorthint_bytes
    packed["level2_longhint"] = packed_level2_longhint_bytes

    baseline_weighted = sum(normalized[k] * DEFAULT_UPDATE_BYTES[k] for k in normalized)
    packed_weighted = sum(normalized[k] * packed[k] for k in normalized)
    cut = 1.0 - packed_weighted / baseline_weighted
    return TrafficScenario(
        fractions=normalized,
        baseline_weighted_bytes=baseline_weighted,
        packed_weighted_bytes=packed_weighted,
        traffic_cut_fraction=cut,
        required_cut_fraction=required_cut_fraction,
        passes_required_cut=cut >= required_cut_fraction,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Model packed CADO bucket update byte savings")
    parser.add_argument(
        "--fraction",
        action="append",
        type=parse_fraction,
        help=(
            "Traffic fraction as kind=value. Repeatable. Defaults to a pessimistic "
            "upper-bound scenario where all bucket-update traffic is level2_shorthint."
        ),
    )
    parser.add_argument("--packed-level2-shorthint-bytes", type=float, default=6.0)
    parser.add_argument("--packed-level2-longhint-bytes", type=float, default=12.0)
    parser.add_argument("--required-cut-fraction", type=float, default=0.28)
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fractions = dict(args.fraction or [("level2_shorthint", 1.0)])
    result = scenario(
        fractions=fractions,
        packed_level2_shorthint_bytes=args.packed_level2_shorthint_bytes,
        packed_level2_longhint_bytes=args.packed_level2_longhint_bytes,
        required_cut_fraction=args.required_cut_fraction,
    )
    text = json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
