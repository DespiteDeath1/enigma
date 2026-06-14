#!/usr/bin/env bash
set -euo pipefail

# Build CADO variants focused on Granite Rapids.
#
# Usage:
#   ./build_cado_variants.sh /path/to/cado-nfs /path/to/build-root

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <cado-src> <build-root>" >&2
  exit 1
fi

CADO_SRC="$1"
BUILD_ROOT="$2"
mkdir -p "$BUILD_ROOT"

build_variant() {
  local name="$1"
  local cc="$2"
  local cxx="$3"
  local cflags="$4"
  local cxxflags="$5"

  local bdir="${BUILD_ROOT}/${name}"
  mkdir -p "$bdir"
  echo "==> Building ${name}"
  echo "    CC=${cc}"
  echo "    CFLAGS=${cflags}"
  cmake -S "$CADO_SRC" -B "$bdir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER="$cc" \
    -DCMAKE_CXX_COMPILER="$cxx" \
    -DCMAKE_C_FLAGS="$cflags" \
    -DCMAKE_CXX_FLAGS="$cxxflags"
  cmake --build "$bdir" -j"$(nproc)"
}

# Baseline-like control from the brief.
build_variant \
  "build-gcc-v3-icelake" \
  "gcc" "g++" \
  "-O3 -march=x86-64-v3 -mtune=icelake-server" \
  "-O3 -march=x86-64-v3 -mtune=icelake-server"

# Native Granite Rapids, AVX2 preference, AVX-512 explicitly disabled.
build_variant \
  "build-gcc-native-avx2" \
  "gcc" "g++" \
  "-O3 -march=native -mno-avx512f -mno-avx512vl -mno-avx512dq -mprefer-vector-width=256" \
  "-O3 -march=native -mno-avx512f -mno-avx512vl -mno-avx512dq -mprefer-vector-width=256"

# Clang alternative.
build_variant \
  "build-clang-native-avx2" \
  "clang" "clang++" \
  "-O3 -march=native -mno-avx512f -mno-avx512vl -mno-avx512dq -mprefer-vector-width=256" \
  "-O3 -march=native -mno-avx512f -mno-avx512vl -mno-avx512dq -mprefer-vector-width=256"

# Intel compiler alternative (if available).
if command -v icx >/dev/null 2>&1 && command -v icpx >/dev/null 2>&1; then
  build_variant \
    "build-icx-native-avx2" \
    "icx" "icpx" \
    "-O3 -xHost -qopt-zmm-usage=low -mno-avx512f -mno-avx512vl -mno-avx512dq" \
    "-O3 -xHost -qopt-zmm-usage=low -mno-avx512f -mno-avx512vl -mno-avx512dq"
else
  echo "==> icx not found; skipping build-icx-native-avx2"
fi

echo "Done. Built variants under: ${BUILD_ROOT}"
