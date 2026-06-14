#!/usr/bin/env python3
"""Audit CADO hotpath hypotheses against source text.

This script checks whether some common assumptions are true in the inspected
CADO source tree (for example: batch inversion usage, overflow checks in the
bucket write hot path).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cado-src", required=True, help="Path to cado-nfs source root")
    args = parser.parse_args()

    root = Path(args.cado_src).resolve()

    las_fbroot = _read(root / "sieve" / "las-fbroot-qlattice.hpp")
    las_arith = _read(root / "sieve" / "las-arith.hpp")
    bucket_push = _read(root / "sieve" / "bucket-push-update.hpp")

    findings = {
        "batch_inversion_symbol_present": "batchinvredc_u32" in las_arith,
        "batch_inversion_used_in_fbroot_transform": "batchinvredc_u32" in las_fbroot,
        "scalar_invmod_calls_in_fbroot_transform": "invmod_redc_32" in las_fbroot,
        "bucket_push_has_overflow_check_only_in_SAFE_BUCKET_ARRAYS": (
            "#ifdef SAFE_BUCKET_ARRAYS" in bucket_push
            and "if (bucket_write[i] >= bucket_start[i + 1])" in bucket_push
        ),
        "bucket_hot_write_is_plain_store": "*bucket_write[i]++ = update;" in bucket_push,
    }

    print(json.dumps(findings, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
