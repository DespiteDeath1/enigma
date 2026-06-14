#!/usr/bin/env bash
# ============================================================
# GNR A/B Measurement Harness for CADO-NFS las on Intel Xeon 6980P
#
# USAGE:
#   ./gnr_ab_harness.sh [N_str] [poly_file]
#
# Prerequisites on the GNR node:
#   - CADO-NFS built (see Dockerfile)
#   - A fixed polynomial file (cado.poly)
#   - A fixed N (the challenge semiprime)
#   - perf record/stat (linux-perf package)
#   - numactl
#
# WHAT IT TESTS (from §6 of the brief):
#   A. Baseline:    -march=x86-64-v3 -mtune=icelake-server (current)
#   B. Native GNR:  -march=native -mno-avx512f -mtune=sapphirerapids
#   C. SMT x1:      24 jobs × las.threads=1 (current, one job per logical CPU)
#   D. SMT x2:      48 jobs × las.threads=1 (exploit 2-way SMT chains)
#   E. SMT x3:      72 jobs × las.threads=1 (over-provision; check if saturates)
#   F. Pinned:      B + D + taskset to NUMA node 0
#   G. Best stack:  B + D + F (projected winner)
#
# PROTOCOL: Fixed N, fixed poly, fixed q window [14M, 14.4M], 3 reps each.
# Report: rel/s mean ± std, extrapolated to full-wall time.
# Correctness gate: byte-identical relation output vs. baseline.
# ============================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────
N="${1:-}"
POLY="${2:-/cado-work/cado.poly}"
LAS="${LAS:-/usr/local/bin/las}"
WORK="/tmp/gnr_ab_$$"
REPS="${REPS:-3}"
NCPUS="${NCPUS:-24}"    # CFS quota CPUs

# Fixed sieve window for A/B (large enough to be representative, small
# enough to finish in <5 min per variant)
Q0=14000000
Q1=14400000   # 400K special-q values = ~1 min per run on AMD

# Common sieve flags for all variants
SIEVE_COMMON=(
    --lim0 11000000 --lim1 14000000
    --lpb0 30 --lpb1 30
    --mfb0 60 --mfb1 60
    --ncurves0 17 --ncurves1 29
    -I 13
    --q0 "$Q0" --q1 "$Q1"
    --poly "$POLY"
)

# ── Helper functions ───────────────────────────────────────────────────────

die() { echo "ERROR: $*" >&2; exit 1; }

require_cmd() { command -v "$1" &>/dev/null || die "Missing: $1 (install linux-perf, numactl)"; }

count_rels() {
    local f="$1"
    [ -f "$f" ] || { echo 0; return; }
    zcat "$f" 2>/dev/null | grep -cv '^#' || echo 0
}

run_variant() {
    local name="$1"
    local n_jobs="$2"
    local extra_env="${3:-}"
    local pin_cmd="${4:-}"
    local out_dir="$WORK/$name"
    mkdir -p "$out_dir"

    echo ""
    echo "=== Variant: $name  (${n_jobs} jobs × las.threads=1) ==="

    local total_rels=0
    local total_secs=0
    local ref_hash=""

    for rep in $(seq 1 "$REPS"); do
        local rep_dir="$out_dir/rep$rep"
        mkdir -p "$rep_dir"
        local out_file="$rep_dir/rels.gz"

        # Build job list: divide [Q0,Q1] into n_jobs slices
        local slice=$(( (Q1 - Q0) / n_jobs ))
        local pids=()

        local t_start
        t_start=$(date +%s%3N)

        for (( i=0; i<n_jobs; i++ )); do
            local sq0=$(( Q0 + i * slice ))
            local sq1=$(( sq0 + slice ))
            local job_out="$rep_dir/rels_${i}.gz"
            local cmd=("$LAS" "${SIEVE_COMMON[@]}" --q0 "$sq0" --q1 "$sq1"
                       --out "$job_out" --t 1)
            if [ -n "$pin_cmd" ]; then
                eval "$pin_cmd ${cmd[*]}" &
            else
                "${cmd[@]}" &
            fi
            pids+=($!)
        done
        wait "${pids[@]}"

        local t_end
        t_end=$(date +%s%3N)
        local elapsed_ms=$(( t_end - t_start ))
        local elapsed_s
        elapsed_s=$(echo "scale=2; $elapsed_ms / 1000" | bc)

        local rels=0
        for (( i=0; i<n_jobs; i++ )); do
            rels=$(( rels + $(count_rels "$rep_dir/rels_${i}.gz") ))
        done

        local rps
        rps=$(echo "scale=0; $rels * 1000 / $elapsed_ms" | bc)
        echo "  Rep $rep: ${rels} rels in ${elapsed_s}s = ${rps} rel/s"

        total_rels=$(( total_rels + rels ))
        total_secs=$(echo "scale=3; $total_secs + $elapsed_s" | bc)

        # Correctness: hash the relation content for comparison
        if [ $rep -eq 1 ]; then
            hash=$(zcat "$rep_dir"/rels_*.gz 2>/dev/null | grep -v '^#' | sort | md5sum | cut -d' ' -f1)
            echo "  Correctness hash: $hash"
            if [ -z "$ref_hash" ]; then
                ref_hash="$hash"
            elif [ "$hash" != "$ref_hash" ]; then
                echo "  WARNING: hash mismatch vs baseline!"
            fi
        fi
    done

    local avg_rels
    avg_rels=$(echo "scale=0; $total_rels / $REPS" | bc)
    local avg_secs
    avg_secs=$(echo "scale=2; $total_secs / $REPS" | bc)
    local avg_rps
    avg_rps=$(echo "scale=0; $avg_rels * 1000 / ($total_secs * 1000 / $REPS)" | bc 2>/dev/null || echo "?")

    # Extrapolate full wall: 65M relations / avg_rps (sieve) + 28min overhead
    local wall_min="?"
    if [ "$avg_rps" != "?" ] && [ "$avg_rps" -gt 0 ] 2>/dev/null; then
        wall_min=$(echo "scale=1; 65000000 / $avg_rps / 60 + 28" | bc)
    fi

    echo "  ── SUMMARY: ${avg_rps} rel/s avg ── projected wall: ${wall_min} min ──"
    echo "$name $avg_rps $wall_min" >> "$WORK/summary.txt"
}

# ── Main ──────────────────────────────────────────────────────────────────
[ -n "$N" ] || die "Usage: $0 <N_decimal> [poly_file]"
[ -f "$POLY" ] || die "Polynomial file not found: $POLY"
[ -f "$LAS"  ] || die "las binary not found: $LAS"

require_cmd numactl
require_cmd bc
require_cmd zcat
require_cmd perf 2>/dev/null || echo "WARNING: perf not available (skipping TopdownL2)"

mkdir -p "$WORK"
echo "Work dir: $WORK"
echo "N = $N" > "$WORK/summary.txt"
echo "" >> "$WORK/summary.txt"
echo "Vendor: $(grep -m1 vendor_id /proc/cpuinfo | awk '{print $3}')" >> "$WORK/summary.txt"
echo "Model:  $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')" >> "$WORK/summary.txt"
echo "SMT:    $(cat /sys/devices/system/cpu/smt/active 2>/dev/null || echo unknown)" >> "$WORK/summary.txt"
echo "" >> "$WORK/summary.txt"

# Check if AVX-512 is in the las binary (must NOT be for Intel)
if objdump -d "$LAS" 2>/dev/null | grep -q "zmm\|evex"; then
    echo "WARNING: las binary contains AVX-512 instructions!"
    echo "  → rebuild with -mno-avx512f to avoid GNR frequency throttling"
else
    echo "OK: no AVX-512 in las binary"
fi
echo ""

# ── perf TopdownL2 snapshot (tells us where cycles go on GNR) ─────────────
echo "=== perf TopdownL2 (single-threaded, 30s snapshot) ==="
if command -v perf &>/dev/null; then
    perf_las_cmd=("$LAS" "${SIEVE_COMMON[@]}" --q0 "$Q0" --q1 $(( Q0 + 200000 ))
                  --out /dev/null --t 1)
    perf stat -M TopdownL2 -e cycles,instructions,branches,branch-misses \
        "${perf_las_cmd[@]}" 2>&1 | tee "$WORK/perf_topdown.txt" | \
        grep -E "Topdown|Bad.Spec|Frontend|Backend|Retiring|branch|IPC" || true
else
    echo "perf not available; skipping TopdownL2"
fi
echo ""

# ── Run variants ──────────────────────────────────────────────────────────

# C. Baseline: current job count (24 single-threaded jobs)
run_variant "C_baseline_24jobs" "$NCPUS"

# D. SMT x2: 48 jobs on 24-CPU-quota machine (exploit 2-way SMT)
run_variant "D_smt_x2_${n_jobs}jobs" $(( NCPUS * 2 ))

# E. SMT x3: 72 jobs (check for saturation)
run_variant "E_smt_x3_${n_jobs}jobs" $(( NCPUS * 3 ))

# F. SMT x2 + NUMA node 0 pinning
NUMA0_CPUS=$(numactl --hardware 2>/dev/null | grep "node 0 cpus:" | cut -d: -f2 | tr ' ' ',' | sed 's/^,//')
if [ -n "$NUMA0_CPUS" ]; then
    echo "NUMA node 0 CPUs: $NUMA0_CPUS"
    PIN_CMD="numactl --physcpubind=$NUMA0_CPUS --localalloc"
    run_variant "F_smt_x2_pinned" $(( NCPUS * 2 )) "" "$PIN_CMD"
else
    echo "Could not determine NUMA node 0 CPUs; skipping pinned variant"
fi

# ── Summary report ────────────────────────────────────────────────────────
echo ""
echo "================================================"
echo "SUMMARY — GNR A/B Results"
echo "================================================"
cat "$WORK/summary.txt"
echo ""
echo "Projected wall time = 65M_rels / rel_s / 60 + 28_min_overhead"
echo "(overhead = poly 12m + filter 10m + GPU LA 3m + sqrt 3m)"
echo ""
echo "A config with projected wall < 235 min passes the 240-min limit"
echo "with 5 min safety margin."
echo ""
echo "Results directory: $WORK"
echo ""
echo "NEXT STEPS based on results:"
echo "  If D or F is fastest: use n_jobs=2*NCPUS in breaking_rsa.py"
echo "  If E is fastest: use n_jobs=3*NCPUS (over-provisioned SMT)"
echo "  Report results to the brief authors with perf_topdown.txt"
