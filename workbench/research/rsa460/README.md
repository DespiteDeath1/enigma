# RSA-460 / c139 CADO-NFS research harness

This directory contains reproducibility helpers for the c139/RSA-460
investigation described in the research brief.  The checked-in workbench does
not contain the private CADO-NFS image, tuned `c140.poly`, msieve GPU linear
algebra bridge, or the SSH key for the AMD benchmark host, so this branch does
not claim a new passing configuration.  It packages the fixed inputs, short
window benchmarks, CADO command sweeps, and Granite Rapids profiling commands
needed to re-test the hypotheses on the target machines.

## What I could re-verify in this repository

- The live validator checks real factors.  `validate_breaking_rsa_solution`
  requires `p * q == n` and matches the generated verification factors.
- The generator matches the brief for 460-bit instances: two ~230-bit primes
  are generated from `gmpy2.random_state(seed)` with top/bottom bits set, then
  `next_prime`, and the Fermat gap threshold is `2^(230 - 100) = 2^130`.
- The public example solver is not the c139 baseline.  It is a small/medium
  factoring pipeline ending in msieve SIQS and is not expected to solve a
  generic 139-digit semiprime inside the milestone wall time.

## Highest-priority measurement

The most plausible lever from the brief is still the sieve-to-LA trade: cut
Intel-bound relation collection and let the arch-neutral RTX PRO 6000 absorb a
larger/denser matrix.  The baseline says Intel wall is about 293 min with
~87-91% in sieving.  Dropping `rels_wanted` from 71M to 60M removes about 15.5%
of nominal sieve work; 58M removes about 18.3%.  This only wins if GPU LA and
filtering add less wall time than the saved sieve minutes, so it must be
measured as an end-to-end run, not just a `las` rate.

Run these first on Granite Rapids, using the exact c140 polynomial and the
existing msieve GPU-LA bridge:

```bash
# Fixed public benchmark instance; keep factors out of solver artifacts.
python -m workbench.research.rsa460.generate_instance \
  --bits 460 --seed 42 --difficulty 460 --no-factors \
  --out /dev/shm/rsa460_seed42_public.json

N="$(python - <<'PY'
import json
print(json.load(open('/dev/shm/rsa460_seed42_public.json'))['n'])
PY
)"

# Replace CADO and POLY with the local checkout/poly paths.
CADO="$HOME/r460/cado-nfs/cado-nfs.py"
POLY="$HOME/r460/c140.poly"

for rels in 71000000 66000000 62000000 60000000 58000000; do
  python -m workbench.research.rsa460.cado_sweep \
    --cado "$CADO" --poly "$POLY" --n "$N" \
    --label "gnr-c140def-rels-${rels}" \
    --work-dir "/dev/shm/gnr-c140def-rels-${rels}" \
    --rels-wanted "$rels" \
    --target-density 125 \
    --extra-param tasks.sieve.dup=false
done
```

If the existing solver wrapper is required for msieve GPU-LA, use the same
`rels_wanted` list in that wrapper instead of `cado_sweep.py`; keep the fixed
N and record sieve, filter, LA, sqrt, and total wall.

## Short `las` window A/B

Use a fixed special-q window for build/codegen experiments.  These runs are
short enough to repeat several times and should be run with no miner/watchers
on the measured cores.

```bash
LAS="$HOME/r460/cado-nfs/build/sieve/las"
POLY="$HOME/r460/c140.poly"

python -m workbench.research.rsa460.las_window_bench \
  --las "$LAS" --poly "$POLY" \
  --q0 11000000 --q1 11200000 \
  --threads 24 --repeat 5 \
  --label gnr-gcc-v3-c140def \
  --out-dir /dev/shm/rsa460-las-ab
```

Use the same command for each CADO build variant.  Compare
`mean_relations_per_second_wall` from the summary JSON files.

Recommended build matrix:

1. GCC x86-64-v3 tuned for GNR:
   `-O3 -march=x86-64-v3 -mtune=graniterapids`
2. GCC x86-64-v4:
   `-O3 -march=x86-64-v4 -mtune=graniterapids`
3. Clang x86-64-v3:
   `-O3 -march=x86-64-v3 -mtune=graniterapids`
4. PGO on the `las` window:
   build once with `-fprofile-generate`, run the fixed window, rebuild with
   `-fprofile-use`.

Reject a build only after checking both relation rate and frequency.  A
slower AVX-512 build may still show useful codegen in isolated functions; use
the profile below to find whether it is a frequency-license loss or a hot loop
regression.

## Granite Rapids profiling

After selecting a fixed `las` window, collect top-down counters:

```bash
OUT_DIR=/dev/shm/rsa460-gnr-profile \
  workbench/research/rsa460/profile_las_gnr.sh -- \
  "$LAS" -poly "$POLY" -q0 11000000 -q1 11200000 \
  -lim0 11000000 -lim1 14000000 -lpb0 30 -lpb1 30 \
  -mfb0 60 -mfb1 60 -lambda0 1.1 -lambda1 1.1 \
  -I 13 -sqside 1 -t 24
```

Report these, not IPC alone:

- TopdownL1/TopdownL2: retiring, frontend bound, bad speculation, backend
  bound, core bound vs memory bound.
- Branch miss rate and branch-miss cycles.
- Effective frequency during the region.
- L1/L2/L3 MPKI and dTLB misses.
- `perf report` top symbols and annotated hottest basic block if available.

Decision rules:

- **Bad speculation >~10-15%**: prioritize branchless or batched hot scatter
  changes in `las`.
- **Frontend bound >~15%**: inspect instruction cache/uop-cache pressure and
  compiler inlining/unrolling differences.
- **Backend core bound dominates with low memory bound**: codegen/scheduling
  and port pressure are the likely Intel-specific levers.
- **Frequency drops materially in v4/AVX-512 builds**: keep v3 for the final
  image unless a targeted light-AVX512 patch wins end-to-end.

## Full result table to fill in

For every run, record:

| Host | CPU | Build | Params | Rels wanted | Unique rels | Sieve wall | Filter wall | GPU LA wall | Total wall | Pass |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| AMD | EPYC 9555 | baseline | c140def | 71M |  |  |  |  |  |  |
| GNR | Xeon 6980P/6776P | gcc-v3 | c140def | 71M |  |  |  |  |  |  |
| GNR | Xeon 6980P/6776P | gcc-v3 | c140def | 60M |  |  |  |  |  |  |
| GNR | Xeon 6980P/6776P | gcc-v3 | c140def | 58M |  |  |  |  |  |  |

## Open experiments to extend

- `target_density` sweep only after the relation-floor sweep:
  `100, 125, 150, 175`.
- Composite special-q:
  `--allow-compsq --qfac-min 50 --qfac-max <below q0>` with the same `las`
  window before attempting a full run.
- Cofactor-heavy/GPU-heavy split: only pursue if profiling confirms CPU
  cofactorization is still a visible fraction on GNR; the brief's prior
  4.68x CPU-sieve blow-up makes this high risk.
- Latest GPU cofactorization: FACT0RN/GPUDispersion and related ECM-on-GPU
  tools are worth a throughput test, but current public GPU lattice-sieving
  work is not a drop-in replacement for CADO relation collection at c139.

## Operator checklist

1. Confirm no miner tmux sessions are using the cores selected for benchmarks.
2. Run all workdirs under `/dev/shm` or the existing memfd RAM filesystem.
3. Pin runs consistently (`taskset`/`numactl`) when comparing compilers.
4. Use the same fixed N, polynomial, special-q windows, and relation targets.
5. Keep raw logs plus the JSON summaries emitted by these scripts.
6. Report a configuration as passing only with end-to-end wall under 240 min on
   both AMD and Intel validators.
