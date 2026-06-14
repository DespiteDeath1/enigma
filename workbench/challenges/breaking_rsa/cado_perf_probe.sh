#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <command> [args...]" >&2
    echo "" >&2
    echo "Optional environment:" >&2
    echo "  OUTDIR=/path/to/output-dir" >&2
    echo "  CPUSET=0-23              # passed to taskset -c" >&2
    echo "  PERF_EVENTS=cycles,instructions,branches,branch-misses" >&2
    exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
outdir="${OUTDIR:-cado-perf-${timestamp}}"
mkdir -p "$outdir"

perf_events="${PERF_EVENTS:-cycles,instructions,branches,branch-misses,cache-references,cache-misses,L1-dcache-load-misses,LLC-load-misses,dTLB-load-misses,iTLB-load-misses}"

meta_file="$outdir/metadata.txt"
stdout_file="$outdir/stdout.log"
stderr_file="$outdir/stderr.log"
perf_file="$outdir/perf.csv"
env_file="$outdir/environment.txt"

{
    echo "timestamp_utc=$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "cwd=$(pwd)"
    echo "cpuset=${CPUSET:-}"
    echo "perf_events=$perf_events"
    echo "command=$*"
    echo

    echo "## uname"
    uname -a || true
    echo

    echo "## cpu.max"
    if [[ -r /sys/fs/cgroup/cpu.max ]]; then
        cat /sys/fs/cgroup/cpu.max
    else
        echo "unavailable"
    fi
    echo

    echo "## lscpu"
    lscpu || true
    echo

    echo "## lscpu -e"
    lscpu -e=cpu,node,socket,core,online 2>/dev/null || true
    echo

    echo "## numactl -H"
    numactl -H 2>/dev/null || echo "numactl unavailable"
} > "$meta_file"

{
    env | sort
} > "$env_file"

runner=("$@")
if [[ -n "${CPUSET:-}" ]]; then
    runner=(taskset -c "$CPUSET" "${runner[@]}")
fi

echo "Writing probe output to: $outdir"
echo "Command: ${runner[*]}"

if command -v perf >/dev/null 2>&1; then
    set +e
    perf stat -x, -o "$perf_file" -e "$perf_events" -- "${runner[@]}" >"$stdout_file" 2>"$stderr_file"
    status=$?
    set -e
else
    echo "perf unavailable; running command without perf" | tee "$stderr_file"
    set +e
    "${runner[@]}" >"$stdout_file" 2>>"$stderr_file"
    status=$?
    set -e
fi

echo "exit_code=$status" >> "$meta_file"
echo "stdout=$stdout_file"
echo "stderr=$stderr_file"
if [[ -f "$perf_file" ]]; then
    echo "perf=$perf_file"
fi

exit "$status"
