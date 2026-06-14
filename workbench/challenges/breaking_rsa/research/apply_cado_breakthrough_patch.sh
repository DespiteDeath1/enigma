#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <cado-src-dir>" >&2
  exit 1
fi

CADO_SRC="$1"
PATCH_FILE="/workspace/workbench/challenges/breaking_rsa/research/patches/cado-force-no-resieve.patch"

if [[ ! -d "$CADO_SRC/.git" ]]; then
  echo "error: $CADO_SRC is not a git checkout" >&2
  exit 1
fi

echo "Applying patch: $PATCH_FILE"
git -C "$CADO_SRC" apply --check "$PATCH_FILE"
git -C "$CADO_SRC" apply "$PATCH_FILE"
echo "Patch applied successfully."
echo "Rebuild CADO, then run with CADO_FORCE_NO_RESIEVE=1 and tasks.sieve.las.batch=true."
