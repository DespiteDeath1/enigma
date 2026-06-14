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
- `apply_cado_breakthrough_patch.sh` + `patches/cado-force-no-resieve.patch`  
  Experimental structural change: force hintless/no-resieve path and pair with
  batch cofactorization.
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

## Breakthrough candidate: forced hintless sieve + batch cofactorization

### Why this is structurally different

Conventional tuning touched flags/placement/SMT, but this changes the memory
shape of `las` itself:

- normal resieve path uses larger bucket updates (`shorthint_t`/`longhint_t`)
- hintless path uses `emptyhint_t`/`logphint_t`, cutting update size roughly
  2x on active levels (`bucket_update_t<1,shorthint_t>` is 4 bytes vs
  `bucket_update_t<1,emptyhint_t>` 2 bytes; similarly 8->4 at level 2/3)
- this can materially reduce memory traffic in fill/downsort phases on
  bandwidth-bound Granite Rapids nodes

### Patch and test

```bash
# apply patch into your cado checkout
./workbench/challenges/breaking_rsa/research/apply_cado_breakthrough_patch.sh \
  /home/ubuntu/r460/cado-nfs

# rebuild variants
./workbench/challenges/breaking_rsa/research/build_cado_variants.sh \
  /home/ubuntu/r460/cado-nfs /home/ubuntu/r460

# run matrix entry:
#   force-no-resieve-batch-t24
```

Patch behavior:
- adds `CADO_FORCE_NO_RESIEVE=1` env override in `siever_config::needs_resieving()`
- requires running with `tasks.sieve.las.batch=true` (already enforced in matrix)

Risk:
- more work in cofactor/batch path; may regress if survivor-side work explodes.
- must pass your byte-identical correctness gate.

## Practical stack to pursue for >=20% Intel cut

Target is cumulative, not single-lever:

1. **Build/codegen** (Granite-native AVX2, avoid AVX-512 downclock):  
   compare `build-gcc-v3-icelake` vs `build-gcc-native-avx2` vs clang/icx.
2. **Placement** (compact pinning in quota-only container):  
   unpinned vs compact vs spread.
3. **SMT throughput knee** (dependency-chain + branchy kernel):  
   sweep `tasks.threads=24,32,48` with compact+SMT pinning.
4. **Batch/cofactor split and composq**:
   `tasks.sieve.las.batch=true` (+ batchlpb/mfb) and
   `tasks.sieve.allow_compsq=true`.
5. **Re-validate relation floor assumptions** only after steps 1-4.

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
