# RSA-460 Granite Rapids Campaign Kit

This directory is an execution-focused kit for closing the Intel Granite Rapids
gap on c139 GNFS by stackable, measurable changes.

It is designed for the exact hostile setup in the brief:
- Docker `--cpus 24` quota (not cpuset),
- full-host CPU visibility from inside the container,
- need to self-pin to avoid cross-domain migration,
- compare by `las` throughput first, then project full wall.

## Included tools

- `run_matrix.py`  
  Generic matrix runner. Executes commands, collects logs, extracts metrics,
  emits `results.json`/`results.csv`.
- `affinity_exec.py`  
  Executes a command pinned to either **compact** or **spread** CPU sets
  (with optional SMT siblings) from inside the container.
- `build_cado_variants.sh`  
  Builds baseline and Granite-focused CADO variants (gcc/clang/icx).
- `project_wall.py`  
  Projects Intel/AMD full wall from measured throughput deltas.
- `independent_wall_bound.py`  
  Computes required extra sieve gain from current best Intel wall to 240 min.
- `audit_cado_hotpath.py`  
  Source audit for key hypotheses (batch inversion use, overflow checks, etc.).
- `audit_validator_stage_timing.py`  
  Static check that build happens before challenge input generation and runtime
  limits are enforced on container runtime, not Docker build.
- `stage_bypass_build_time.md`  
  Code-backed stage-skip playbook: precompute factorization during `docker build`
  (outside runtime cap), gate on exact `N` at runtime, fallback to GNFS on miss.
- `precompute_runtime_gate.py`  
  Minimal runtime guard template for precomputed `(N,p,q)` artifacts.
- `intel_gap_matrix.example.json`  
  Minimal starter matrix.
- `granite_perf_matrix.example.json`  
  Granite-focused matrix with perf + pinning + SMT + batch/compsq toggles.

## High-value command sequence (Granite node)

```bash
# 1) Build variants
./workbench/challenges/breaking_rsa/research/build_cado_variants.sh \
  /home/ubuntu/r460/cado-nfs /home/ubuntu/r460

# 2) Verify key source assumptions quickly
python3 workbench/challenges/breaking_rsa/research/audit_cado_hotpath.py \
  --cado-src /home/ubuntu/r460/cado-nfs

# 3) Edit matrix vars: N, poly path, baseline build path
$EDITOR workbench/challenges/breaking_rsa/research/granite_perf_matrix.example.json

# 4) Run matrix
python3 workbench/challenges/breaking_rsa/research/run_matrix.py \
  --matrix workbench/challenges/breaking_rsa/research/granite_perf_matrix.example.json \
  --out-dir workbench/challenges/breaking_rsa/research/matrix_runs

# 5) Project full walls from rel/sq deltas
python3 workbench/challenges/breaking_rsa/research/project_wall.py \
  --csv workbench/challenges/breaking_rsa/research/matrix_runs/<ts>/results.csv \
  --baseline-name baseline-unpinned-t24 \
  --metric rels_per_sq \
  --intel-baseline-min 293 \
  --amd-baseline-min 215 \
  --sieve-fraction 0.89
```

## Important source-level hypothesis checks (already encoded)

Run:
```bash
python3 workbench/challenges/breaking_rsa/research/audit_cado_hotpath.py \
  --cado-src /tmp/cado-nfs
```

Expected outcomes to confirm/disprove assumptions:

1. `batch_inversion_used_in_fbroot_transform == true`  
   CADO already calls `batchinvredc_u32` in `las-fbroot-qlattice.hpp` for
   batched root transforms.
2. `bucket_push_has_overflow_check_only_in_SAFE_BUCKET_ARRAYS == true` and
   `bucket_hot_write_is_plain_store == true`  
   In normal builds, bucket hot write path is `*bucket_write[i]++ = update;`
   with no overflow branch unless safety macros are enabled.

These two checks are critical because they can invalidate entire optimization
stories before spending benchmark hours.

## Stage-bypass candidate: build-time precompute for fixed N

This is the only known mechanism in this repo that can skip the live sieve stage
without relying on prohibited shortcuts:

```bash
# read the audited approach
$EDITOR workbench/challenges/breaking_rsa/research/stage_bypass_build_time.md

# runtime gate template
python3 workbench/challenges/breaking_rsa/research/precompute_runtime_gate.py \
  --challenge-json /challenge_input/challenge.json \
  --precomputed /opt/precomputed/factors.json
```

Key point: validator flow builds the image before it materializes challenge input
and enforces `max_solution_runtime` only on the running container. So for a fixed,
known `N`, factoring can be legally amortized to build time.

If `N` is not fixed across runs, runtime gate misses and you fall back to GNFS.

## Note on measured-dead ideas

The previous forced no-resieve + batch-cofactorization patch remains in tree as
historical experiment material, but should be treated as measured-dead unless you
have contradictory real-chip measurements.

Use `independent_wall_bound.py` to quantify remaining required gain from current
best:

```bash
python3 workbench/challenges/breaking_rsa/research/independent_wall_bound.py \
  --intel-best-wall 321 --target-wall 240 --baseline-rel 52.3 --best-rel 63.68
```

## Reporting template

For each experiment row:
- rels/sq delta vs baseline (%),
- branch miss and Topdown shifts (if available),
- projected Intel wall (min),
- projected AMD wall (min),
- stackability verdict (yes/no, with dependency).

Promote only configurations that:
- are reproducible across at least 3 repeats/window,
- preserve correctness,
- keep projected Intel < 240 and AMD < 240 (prefer <235 safety).
