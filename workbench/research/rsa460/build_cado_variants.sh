#!/usr/bin/env bash
#
# Build CADO las variants for RSA-460 Granite Rapids A/B tests.
#
# Required:
#   CADO_SRC=/path/to/cado-nfs
# Optional:
#   BUILD_ROOT=/dev/shm/cado-builds
#   JOBS=24
#
set -euo pipefail

CADO_SRC="${CADO_SRC:-}"
BUILD_ROOT="${BUILD_ROOT:-/dev/shm/cado-builds}"
JOBS="${JOBS:-24}"

if [[ -z "$CADO_SRC" || ! -d "$CADO_SRC" ]]; then
  echo "Set CADO_SRC to a CADO-NFS checkout" >&2
  exit 2
fi

if ! command -v cmake >/dev/null 2>&1; then
  echo "cmake not found" >&2
  exit 2
fi

mkdir -p "$BUILD_ROOT"

build_one() {
  local label="$1"
  local cc="$2"
  local cxx="$3"
  local flags="$4"
  local build_dir="$BUILD_ROOT/$label"

  if ! command -v "$cc" >/dev/null 2>&1; then
    echo "skip $label: compiler $cc not found" >&2
    return 0
  fi
  if ! command -v "$cxx" >/dev/null 2>&1; then
    echo "skip $label: compiler $cxx not found" >&2
    return 0
  fi

  echo "=== building $label ==="
  echo "CC=$cc CXX=$cxx"
  echo "FLAGS=$flags"

  cmake -S "$CADO_SRC" -B "$build_dir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER="$cc" \
    -DCMAKE_CXX_COMPILER="$cxx" \
    -DCMAKE_C_FLAGS_RELEASE="$flags -DNDEBUG" \
    -DCMAKE_CXX_FLAGS_RELEASE="$flags -DNDEBUG" \
    -DCMAKE_EXE_LINKER_FLAGS_RELEASE="-Wl,-O1" \
    "$@"

  cmake --build "$build_dir" --target las -j "$JOBS"

  if [[ ! -x "$build_dir/sieve/las" ]]; then
    echo "las not found after build: $build_dir/sieve/las" >&2
    exit 1
  fi

  "$build_dir/sieve/las" -v 2>&1 | tee "$build_dir/las-version.txt" || true
  objdump -d "$build_dir/sieve/las" > "$build_dir/las.objdump" || true
  if python3 - "$build_dir/las.objdump" <<'PY'
import re
import sys
text = open(sys.argv[1], errors="ignore").read()
raise SystemExit(0 if re.search(r"\bzmm[0-9]|\bymm1[6-9]|\bymm2[0-9]|\bymm3[0-1]", text) else 1)
PY
  then
    echo "WARN $label: objdump contains zmm or high ymm registers; audit AVX-512/frequency license" >&2
  fi
}

# Baseline-compatible Intel AVX2 tune, matching the shipped style.
build_one gcc-v3-icelake gcc g++ "-O3 -march=x86-64-v3 -mtune=icelake-server"

# Granite Rapids scheduler, AVX2 only.  Use this as the primary Intel candidate.
build_one gcc-native-no512 gcc g++ "-O3 -march=native -mno-avx512f -mno-avx512vl -mno-avx512bw -mno-avx512dq -mprefer-vector-width=256"

# GCC may not know graniterapids on older distro images.  If it does, this
# isolates mtune from native feature selection.
if gcc -Q --help=target 2>/dev/null | python3 -c 'import sys; raise SystemExit(0 if "graniterapids" in sys.stdin.read() else 1)'; then
  build_one gcc-v3-gnr gcc g++ "-O3 -march=x86-64-v3 -mtune=graniterapids -mprefer-vector-width=256"
else
  build_one gcc-v3-sapphirerapids gcc g++ "-O3 -march=x86-64-v3 -mtune=sapphirerapids -mprefer-vector-width=256"
fi

# Clang scheduling often differs on branchy integer loops.
build_one clang-v3-native-no512 clang clang++ "-O3 -march=native -mno-avx512f -mno-avx512vl -mno-avx512bw -mno-avx512dq -mprefer-vector-width=256"

echo
echo "Built variants under $BUILD_ROOT"
echo "Use run_gnr_matrix.py with --las label=$BUILD_ROOT/<label>/sieve/las"
