#!/usr/bin/env bash
#
# Profile a fixed las window on Granite Rapids.
#
# Usage:
#   ./profile_las_gnr.sh -- ./las -poly c140.poly -q0 11000000 -q1 11200000 ...
#
set -euo pipefail

if [[ "${1:-}" != "--" ]]; then
  echo "usage: $0 -- <las command...>" >&2
  exit 2
fi
shift

if [[ $# -eq 0 ]]; then
  echo "missing las command" >&2
  exit 2
fi

OUT_DIR="${OUT_DIR:-rsa460_gnr_profile_$(date -u +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_DIR"

echo "Profile output: $OUT_DIR"
echo "Command: $*"

if command -v lscpu >/dev/null 2>&1; then
  lscpu > "$OUT_DIR/lscpu.txt" || true
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -q > "$OUT_DIR/nvidia-smi.txt" || true
fi

if command -v perf >/dev/null 2>&1; then
  perf stat -d -d -d \
    -e cycles,instructions,branches,branch-misses,cache-references,cache-misses,L1-dcache-loads,L1-dcache-load-misses,dTLB-loads,dTLB-load-misses \
    -o "$OUT_DIR/perf-stat-core.txt" -- "$@" || true

  perf stat -M TopdownL1 -M TopdownL2 \
    -o "$OUT_DIR/perf-stat-topdown.txt" -- "$@" || true

  perf record -F 997 -g --call-graph dwarf -o "$OUT_DIR/perf.data" -- "$@" || true
  perf report --stdio -i "$OUT_DIR/perf.data" > "$OUT_DIR/perf-report.txt" 2>/dev/null || true
else
  echo "perf not found; skipping perf profiling" | tee "$OUT_DIR/perf-missing.txt"
fi

if command -v toplev >/dev/null 2>&1; then
  toplev -l3 --no-desc -- "$@" > "$OUT_DIR/toplev-l3.txt" 2>&1 || true
elif command -v toplev.py >/dev/null 2>&1; then
  toplev.py -l3 --no-desc -- "$@" > "$OUT_DIR/toplev-l3.txt" 2>&1 || true
else
  echo "toplev not found; perf Topdown metrics are the fallback" | tee "$OUT_DIR/toplev-missing.txt"
fi

echo "Done. Inspect:"
echo "  $OUT_DIR/perf-stat-core.txt"
echo "  $OUT_DIR/perf-stat-topdown.txt"
echo "  $OUT_DIR/perf-report.txt"
echo "  $OUT_DIR/toplev-l3.txt"
