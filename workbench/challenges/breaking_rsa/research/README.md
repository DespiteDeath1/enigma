# RSA-460 Intel Gap Research Kit

This directory contains a reproducible A/B harness for narrowing the Intel
Granite Rapids gap on the c139 (~460-bit) GNFS workload.

## What this kit does

- Runs a matrix of controlled experiments (same N, same poly, fixed q-window).
- Captures per-run logs and wall-time.
- Extracts throughput metrics (for example `r/sq` and `s/r`) with regex.
- Emits machine-readable `results.json` + `results.csv`.
- Computes percent delta against a declared baseline metric.

This does **not** assume one specific CADO layout; it executes whatever command
you put in the matrix.

## Files

- `run_matrix.py` - generic matrix runner.
- `intel_gap_matrix.example.json` - starter matrix you should edit.

## Usage

```bash
cd /workspace
python3 workbench/challenges/breaking_rsa/research/run_matrix.py \
  --matrix workbench/challenges/breaking_rsa/research/intel_gap_matrix.example.json \
  --out-dir workbench/challenges/breaking_rsa/research/matrix_runs
```

Outputs are written under:

`workbench/challenges/breaking_rsa/research/matrix_runs/<timestamp>/`

## Measurement protocol (important)

1. **Fix the problem instance** (`N`) for all A/B runs.
2. **Fix the polynomial** (`c140.poly`) for all A/B runs.
3. **Use at least two q-windows** (`QWIN_A`, `QWIN_B`) to avoid local bias.
4. **Compare by throughput first** (for example `rels_per_sq`), then project to
   full wall.
5. Keep CPU pinning and thermal state stable between runs.

## High-priority hypotheses to test first

These are ranked by potential to recover double-digit Intel wall-time:

1. **Codegen/profile mismatch on Intel**
   - Compare GCC/Clang/icx builds with explicit `-march=x86-64-v3`.
   - Keep AVX-512 disabled unless it wins on measured q-window throughput.
   - Add a PGO pass for `las` on Intel-only.

2. **Thread-count/frequency knee**
   - Sweep `tasks.threads` in `{24,22,20,18}`.
   - If Intel all-core frequency rises enough at lower thread count, total
     throughput can improve despite fewer workers.

3. **Batch cofactorization split**
   - Test `tasks.sieve.las.batch=true` with tuned `batchlpb*`, `batchmfb*`.
   - Goal: reduce expensive CPU-side per-survivor work and shift to batched
     post-processing.

4. **Composite special-q**
   - Test `tasks.sieve.allow_compsq=true` with bounded `qfac_min/qfac_max`.
   - Validate `rels/sq` and CPU cost, not just raw relation count.

5. **Relation-floor pressure**
   - Test lower `tasks.sieve.rels_wanted` and compensate by relaxing filter
     constraints only as much as needed for successful LA.

## Suggested Intel-vs-AMD reporting format

For each experiment:

- `rels_per_sq` (or equivalent throughput metric).
- `delta_vs_baseline_pct`.
- projected sieve wall contribution.
- projected full wall under your current sieve/LA split.

Then report:

- best **single config** that keeps AMD <4h and Intel <4h,
- and second-best fallback if first fails stability checks.

## Notes on CADO parameters used in the matrix template

- `tasks.sieve.las.batch=true` enables batch cofactorization mode in `las`.
- `tasks.batchlpb0/1`, `tasks.batchmfb0/1` are accepted by `las` and CADO's
  cadofactor wrappers.
- `tasks.sieve.allow_compsq=true` enables composite special-q mode.

All of these should still be validated on your local CADO checkout/version.
