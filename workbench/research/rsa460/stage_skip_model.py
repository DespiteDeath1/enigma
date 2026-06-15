#!/usr/bin/env python3
"""Model whether skipping GNFS stages can close the RSA-460 GNR wall.

This model is deliberately simple: it answers "if this stage were free, would
the 321 min measured Intel wall fit 240 min?"  It is useful because many legal
precomputations (poly import, factor-base cache) can only remove small
non-sieve stages.
"""

from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_STAGE_SHARES = {
    "polyselect": 0.05,
    "sieve": 0.89,
    "filter": 0.02,
    "linear_algebra": 0.03,
    "sqrt": 0.01,
}


@dataclass
class SkipScenario:
    skipped_stages: list[str]
    skipped_minutes: float
    remaining_wall_min: float
    passes_target: bool
    additional_sieve_cut_needed_min: float
    additional_sieve_cut_needed_fraction: float


def parse_stage_share(value: str) -> tuple[str, float]:
    name, raw = value.split("=", 1)
    return name, float(raw)


def normalize_stage_shares(shares: dict[str, float]) -> dict[str, float]:
    total = sum(shares.values())
    if total <= 0:
        raise ValueError("stage shares must sum to a positive value")
    return {k: v / total for k, v in shares.items()}


def scenario(
    wall_min: float,
    target_min: float,
    shares: dict[str, float],
    skipped: list[str],
) -> SkipScenario:
    skipped_minutes = wall_min * sum(shares.get(stage, 0.0) for stage in skipped)
    remaining = wall_min - skipped_minutes
    sieve_min = wall_min * shares.get("sieve", 0.0)
    additional = max(0.0, remaining - target_min)
    return SkipScenario(
        skipped_stages=skipped,
        skipped_minutes=skipped_minutes,
        remaining_wall_min=remaining,
        passes_target=remaining <= target_min,
        additional_sieve_cut_needed_min=additional,
        additional_sieve_cut_needed_fraction=(additional / sieve_min) if sieve_min else 0.0,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute stage-skip wall scenarios")
    parser.add_argument("--wall-min", type=float, default=321.0)
    parser.add_argument("--target-min", type=float, default=240.0)
    parser.add_argument(
        "--stage-share",
        action="append",
        type=parse_stage_share,
        help="Override/add stage share as name=fraction. Repeatable.",
    )
    parser.add_argument(
        "--skip",
        action="append",
        help="Comma-separated skipped stage list. Repeatable. Defaults to all non-empty combinations.",
    )
    parser.add_argument("--no-normalize", action="store_true", help="Do not normalize stage shares to sum to 1")
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    shares = dict(DEFAULT_STAGE_SHARES)
    if args.stage_share:
        shares.update(dict(args.stage_share))
    if not args.no_normalize:
        shares = normalize_stage_shares(shares)

    if args.skip:
        skips = [[stage for stage in item.split(",") if stage] for item in args.skip]
    else:
        names = [stage for stage in shares if stage != "sieve"]
        skips = []
        for r in range(1, len(names) + 1):
            skips.extend(list(combo) for combo in itertools.combinations(names, r))

    results = [asdict(scenario(args.wall_min, args.target_min, shares, skipped)) for skipped in skips]
    output = {"wall_min": args.wall_min, "target_min": args.target_min, "stage_shares": shares, "scenarios": results}
    text = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
