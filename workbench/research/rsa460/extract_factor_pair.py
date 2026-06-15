#!/usr/bin/env python3
"""Extract a factor pair for N from CADO/msieve text artifacts.

The parser is intentionally conservative: it scans decimal integers in the
provided files and returns the first pair whose product is exactly N.  This is
useful for build-time precompute scripts where CADO's final output location may
vary across versions/wrappers.
"""

from __future__ import annotations

import argparse
import json
import re
from itertools import combinations
from pathlib import Path


INTEGER_RE = re.compile(r"\b[0-9]{2,}\b")


def collect_integers(paths: list[Path]) -> list[int]:
    values: set[int] = set()
    for path in paths:
        if path.is_dir():
            files = [p for p in path.rglob("*") if p.is_file()]
        else:
            files = [path]
        for file_path in files:
            try:
                text = file_path.read_text(errors="ignore")
            except OSError:
                continue
            for match in INTEGER_RE.finditer(text):
                values.add(int(match.group(0)))
    return sorted(values)


def find_factor_pair(n: int, values: list[int]) -> tuple[int, int] | None:
    candidates = [v for v in values if 1 < v < n and n % v == 0]
    for p in candidates:
        q = n // p
        if q in candidates or p * q == n:
            return (min(p, q), max(p, q))
    # Fallback for logs containing both factors but with formatting that hid one
    # divisibility check above should already catch the normal case.
    for p, q in combinations(candidates, 2):
        if p * q == n:
            return (min(p, q), max(p, q))
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find p,q in CADO/msieve text artifacts")
    parser.add_argument("--n", required=True, type=int, help="Target semiprime")
    parser.add_argument("paths", nargs="+", type=Path, help="Files or directories to scan")
    parser.add_argument("--out", type=Path, help="Optional JSON output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    values = collect_integers(args.paths)
    pair = find_factor_pair(args.n, values)
    if pair is None:
        raise SystemExit("no factor pair found")
    p, q = pair
    result = {"n": str(args.n), "p": str(p), "q": str(q), "status": "success"}
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
