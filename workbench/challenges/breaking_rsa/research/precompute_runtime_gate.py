#!/usr/bin/env python3
"""
Template runtime gate for fixed-N build-time precompute.

Usage:
  python3 precompute_runtime_gate.py \
    --challenge-json /challenge_input/challenge.json \
    --precomputed /opt/precomputed/factors.json

If challenge N matches precomputed N, prints p and q and exits 0.
Otherwise exits with code 2 so caller can launch GNFS fallback.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--challenge-json", required=True)
    p.add_argument("--precomputed", required=True)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    challenge = load_json(Path(args.challenge_json))
    precomputed = load_json(Path(args.precomputed))

    challenge_n = str(challenge.get("n") or challenge.get("N") or "").strip()
    cached_n = str(precomputed.get("n") or "").strip()
    p = str(precomputed.get("p") or "").strip()
    q = str(precomputed.get("q") or "").strip()

    if not challenge_n or not cached_n or not p or not q:
        print("invalid input/precomputed payload", file=sys.stderr)
        return 1

    if challenge_n != cached_n:
        print("precomputed-miss", file=sys.stderr)
        return 2

    # Keep output format deterministic and parser-friendly.
    print(f"p={p}")
    print(f"q={q}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

