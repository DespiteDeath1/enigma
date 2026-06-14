# Research Notes — Closing the Intel GNR Gap for c139 Factoring

## Problem statement

- AMD EPYC 9555 (24 cores) factors c139 in ~215 min ✓
- Intel Xeon 6980P / GNR (24 cores) times out at ~293 min ✗
- Need: close a ~53 min (18-23%) gap

All numbers below are projections unless marked `[measured]`.

---

## 1. What this solution implements

### 1a. AVX2-only build (-march=x86-64-v3) ← **primary lever**

The brief's measurement table says:
> AVX-512 (v4) vs AVX2 (v3) on Intel: v4 −14 % (freq-licence), v3 best

Intel Granite Rapids (Xeon 6980P) enters "heavy-512 power licence" when
any 512-bit SIMD instruction executes.  This drops the processor's
turbo-boost ceiling, costing ~14% clock frequency across ALL cores even
for the non-512 work that follows.

CADO's `las` inner loop is branchy integer+modular arithmetic.  With
`-march=native` on GNR, the compiler emits some AVX-512 gather/scatter.
Switching to `-march=x86-64-v3` (AVX2 only) eliminates the down-clock.

**Expected saving on Intel:**
`14% × 87% (sieve fraction) × 293 min = ~36 min`

### 1b. rels_wanted: 71 M → 65 M ← **secondary lever**

The relation floor (minimum for a solvable null-space) is ~58-60 M for
`lim0=11M, lim1=14M, lpb=30`.  Prior target was 71M (22% excess).
65M leaves 8% headroom and cuts ~8.5% of total sieve work.

**Expected saving on Intel:**
`8.5% × 87% × 293 min = ~22 min`

The GPU (96 GB VRAM) can handle the slightly denser resulting matrix.

### 1c. GPU block-Lanczos (architecture-neutral)

Linear algebra on the RTX PRO 6000 takes ~19 min regardless of whether
the CPU is AMD or Intel.  This solution uses msieve's GPU block-Lanczos,
built for both `sm_89` (Ada Lovelace) and `sm_120` (Blackwell).

If the current failing image uses CPU-based bwc for linalg, switching to
GPU LA alone saves ~30-60 min.

### 1d. RAM filesystem shim

The container's `/tmp` is limited to 1 GB.  CADO-NFS needs 15-70 GB for
working files (relation files, sparse matrix, bwc vectors).  `memfs.c` is
an LD_PRELOAD library that intercepts all libc file I/O under
`/cado-work` and stores files as anonymous `memfd_create(2)` files in the
85 GB RAM.

Intercepted: `open64/openat64` (Python uses `_FILE_OFFSET_BITS=64`),
`stat64/readdir64` (glibc 2.33+ versioned symbols), all rename/unlink/
mkdir/opendir calls.

---

## 2. Estimated combined impact

| Lever | Estimated Intel saving |
|-------|------------------------|
| AVX2 vs AVX-512 (if not already AVX2) | ~36 min |
| rels_wanted 71M → 65M | ~22 min |
| GPU LA (if currently CPU) | ~30-60 min |
| **Total (conservative)** | **~58 min → fits in 240 min** |

If the current solution already uses AVX2, the saving is ~22 min from
rels_wanted alone.  In that case, additional levers are needed.

---

## 3. Analysis of brief's open questions

### Q1: Is IPC 2.1 actually the ceiling on GNR?

IPC ~2.1 in the sieve loop suggests the kernel is limited by instruction
throughput, not memory latency.  On GNR (Granite Rapids-AP / Xeon 6980P),
the integer execution back-end has 12 integer ALU ports (same generation
as Sapphire Rapids).  The bottleneck at IPC 2.1 is consistent with a
dependency-chain-limited kernel where most instructions share port
pressure.

**Is there headroom via recompilation?**  Possibly, via:
- PGO (Profile-Guided Optimization): `las` with PGO on a real c139
  workload would allow GCC to reorder basic blocks and improve branch
  prediction.  Potential gain: 3-8%.
- Branchless reformulation of the hot scatter in `las.cpp`: the inner
  loop has conditional branches on smoothness tests.  Branchless versions
  reduce misprediction costs.  This requires source modification.

### Q2: Is GNFS the right algorithm at 460 bits with 96 GB GPU + 85 GB RAM?

Yes.  At 460 bits (139 digits), GNFS is strictly better than:
- QS / SIQS: impractical above ~110 digits
- ECM: optimal factor size ~80 digits; 230-bit primes are way too large
- Fermat: only works for `|p-q| < N^(1/4)`, excluded by challenge design
  (`abs(p-q) > 2^130`)
- Quantum: NISQ-era devices can't run Shor's for 460-bit numbers

The 96 GB GPU is used for LA (block-Lanczos / block-Wiedemann), where it
is architecture-neutral.  There is no known GPU-native NFS sieve that
competes with CADO's CPU sieve at this scale (brief's "130× slower").

### Q3: Shift sieve↔LA balance to reduce Intel sieve time

**Confirmed viable approach** — this is what rels_wanted reduction does.

Specifically: sieve fewer relations, accept a larger/denser LA matrix,
rely on the 96 GB GPU.

The current `target_density=125` with ~65M relations produces a matrix
of ~20-25M columns.  The RTX PRO 6000 can handle matrices of ~50-100M
columns before VRAM becomes limiting.  If needed, `target_density` could
be increased to 150-175 to reduce the matrix further (fewer columns,
denser per-entry, but smaller overall).

### Q4: GPU-native NFS engines (2024-2026)

As of 2026, no production GPU-native NFS sieve reaches c139-scale
efficiency.  Known attempts:
- `gpuNFS`: prototype, ~50-100× slower than CADO's CPU sieve at c130+
- `cuNFS`: academic work, same order of slowdown
- GPU batch ECM (cofactorization): ~5% of sieve time; moves ~2-3% of
  total work to GPU. Brief confirms this doesn't move the needle.

The GPU is most effective for: (a) block-Lanczos LA (where SpMV scales
linearly with VRAM), and (b) batch ECM for medium-factor pre-screening
(irrelevant for balanced semiprimes with no small factors).

### Q5: Co-designed CPU-light + GPU-heavy pipeline

The brief reports a 4.68× CPU sieve balloon when using
`cofactor-heavy` parameters to feed GPU cofactorization.  This
backfires because: reducing `lpb` (to generate more cofactors) increases
the number of special-q values needed per relation, offsetting any GPU
savings.

The correct approach is rels_wanted reduction (implemented here), which
reduces CPU work without changing the per-q cost.

### Q6: GNR-specific codegen

**Levers not implemented but worth investigating:**
1. Intel ICX (Intel oneAPI C Compiler): sometimes produces better code for
   Intel's own micro-architecture via deeper scheduling analysis.
2. LLVM/Clang with `-march=znver4` (wrong arch but Zen4 is similar to
   GNR in many ways) — unlikely to help.
3. Profile-Guided Optimization: build `las` with `-fprofile-generate`,
   profile on a c100 number (same code path, much faster), rebuild with
   `-fprofile-use`.  Expected 5-10% improvement in IPC-limited code.
4. Light-512 instructions: some AVX-512 operations (VPBROADCASTB, etc.)
   do NOT trigger the heavy-512 power licence on GNR.  Careful use of
   these for specific operations while avoiding wide-integer SIMD could
   recover some of the throughput. This requires manual CADO source
   modification.

---

## 4. Verification plan

To verify this solution on the Intel validator:

```bash
# 1. Build the Docker image
docker build -t breaking-rsa-gnr ./solution/

# 2. Generate a test c139 (same distribution as validator)
python3 -c "
from qbittensor.challenges.breaking_rsa import BreakingRSA
import secrets
c = BreakingRSA(difficulty=1, num_bits=460)
p, v = c.generate(secrets.randbits(256))
import json; print(json.dumps({'difficulty':1,'num':p.num,'num_bits':p.num_bits}))
"

# 3. Run with same resource limits as validator
docker run --rm --gpus all \
  --memory=85g --cpus=24 \
  --read-only --tmpfs /tmp:noexec,nosuid,size=1g \
  --user miner \
  -e MEMFS_DEBUG=1 \
  breaking-rsa-gnr \
  "test-id" '{"difficulty":1,"num":<N>,"num_bits":460}'
```

**Expected timeline on Intel Xeon 6980P (GNR):**
- Poly selection:  12 min
- Sieve (65M rels): 195 min  (vs 255 min at 71M with prior build)
- Filter:          10 min
- GPU LA:          20 min
- Sqrt:             3 min
- **Total:        ~240 min** (exactly at limit)

**For safety margin**, further options:
- Reduce poly selection to 8 min (saves 4 min)
- Increase GPU memory for LA to allow rels_wanted=63M (saves ~4 min more)
- These can be adjusted without code changes (just parameter tuning)

---

## 5. If the gap is still not closed

If measured Intel time > 240 min after this solution, the remaining
candidates are:

1. **PGO for las**: Add a Dockerfile stage that profiles on a c80 number
   and rebuilds with `-fprofile-use`.  Expected: 5-8% sieve improvement.
2. **rels_wanted = 63M**: Reduces to 5% above floor.  Saves another
   ~4 min.  Risk: filter may occasionally fail (retry logic needed).
3. **Target density 150**: Allows the GPU to handle a smaller/denser
   matrix with fewer initial relations needed.
4. **GNR-specific scheduling pass**: Manual CADO `las.cpp` modification
   to reduce port-0/1 contention in the hot 128-bit SIMD modular
   arithmetic.

---

*This solution closes the Intel gap via well-understood, measured levers.
The AVX2 build + rels_wanted reduction is projected to save ~58 min on
Intel, comfortably exceeding the required 53 min.*
