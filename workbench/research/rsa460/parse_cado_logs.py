#!/usr/bin/env python3
"""Parse CADO-NFS and las logs into compact benchmark summaries."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable


RELATION_PATTERNS = (
    re.compile(r"\b(?:total\s+)?(?P<count>[0-9][0-9,]*)\s+(?:relations|rels)\b", re.I),
    re.compile(r"\b(?:total\s+)?(?P<count>[0-9][0-9,]*)\s+(?:reports|survivors)\b", re.I),
    re.compile(r"\b(?:relations|rels)\s*[:=]\s*(?P<count>[0-9][0-9,]*)\b", re.I),
)

ELAPSED_PATTERNS = (
    re.compile(r"\b(?:real|elapsed(?:\s+time)?)\s*[:=]\s*(?P<seconds>[0-9]+(?:\.[0-9]+)?)\s*s?\b", re.I),
    re.compile(r"\b(?P<seconds>[0-9]+(?:\.[0-9]+)?)\s*(?:seconds|sec|s)\s+elapsed\b", re.I),
)

PHASE_PATTERNS = (
    ("polyselect", re.compile(r"\bpoly(?:nomial)?(?:\s*selection|select)\b", re.I)),
    ("sieve", re.compile(r"\b(?:sieving|las|relation collection)\b", re.I)),
    ("filter", re.compile(r"\bfilter(?:ing)?\b", re.I)),
    ("linear_algebra", re.compile(r"\b(?:linear algebra|linalg|bwc|block lanczos|block wiedemann)\b", re.I)),
    ("sqrt", re.compile(r"\b(?:square root|sqrt)\b", re.I)),
)


def _int_from_match(match: re.Match[str], name: str) -> int:
    return int(match.group(name).replace(",", ""))


def _iter_lines(paths: Iterable[Path]) -> Iterable[tuple[str, str]]:
    for path in paths:
        with path.open("r", errors="replace") as handle:
            for line in handle:
                yield str(path), line.rstrip("\n")


def parse_logs(paths: Iterable[Path]) -> dict[str, Any]:
    max_relations = 0
    relation_hits: list[dict[str, Any]] = []
    elapsed_hits: list[dict[str, Any]] = []
    phase_elapsed: dict[str, float] = {}

    for path, line in _iter_lines(paths):
        for pattern in RELATION_PATTERNS:
            match = pattern.search(line)
            if match:
                count = _int_from_match(match, "count")
                max_relations = max(max_relations, count)
                relation_hits.append({"path": path, "relations": count, "line": line.strip()})
                break

        elapsed_seconds: float | None = None
        for pattern in ELAPSED_PATTERNS:
            match = pattern.search(line)
            if match:
                elapsed_seconds = float(match.group("seconds"))
                elapsed_hits.append({"path": path, "elapsed_seconds": elapsed_seconds, "line": line.strip()})
                break

        if elapsed_seconds is None:
            continue

        for phase, phase_pattern in PHASE_PATTERNS:
            if phase_pattern.search(line):
                phase_elapsed[phase] = phase_elapsed.get(phase, 0.0) + elapsed_seconds
                break

    best_elapsed = elapsed_hits[-1]["elapsed_seconds"] if elapsed_hits else None
    rels_per_second = None
    if max_relations and best_elapsed and best_elapsed > 0:
        rels_per_second = max_relations / best_elapsed

    return {
        "max_relations_seen": max_relations,
        "last_elapsed_seconds": best_elapsed,
        "relations_per_second_from_last_elapsed": rels_per_second,
        "phase_elapsed_seconds": phase_elapsed,
        "relation_hits": relation_hits[-20:],
        "elapsed_hits": elapsed_hits[-20:],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize CADO-NFS/las benchmark logs as JSON")
    parser.add_argument("logs", nargs="+", type=Path, help="Log files to parse")
    parser.add_argument("--out", type=Path, help="Optional JSON summary output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = parse_logs(args.logs)
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
