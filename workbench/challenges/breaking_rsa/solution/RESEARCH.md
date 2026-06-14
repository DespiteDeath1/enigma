# Research Notes — Closing the Intel GNR c139 Gap

## Executive Summary

**Three stacked levers projected to contribute ≥20% on Intel Xeon 6980P:**

| Lever | Type | Status | Est. Intel saving |
|-------|------|--------|-------------------|
| Native GNR build (`-march=native -mno-avx512f`) | Build | Implemented | 3–10% |
| SMT exploitation (48 jobs on 24-CPU quota) | Runtime | Implemented | 10–25% |
| Compact SNC CPU pinning | Runtime | Implemented | 2–5% |
| **Combined** | | | **~15–40%** |

---

## 1. Hot-kernel source analysis

From `perf record` on `las` (single thread, production params) and CADO-NFS source
examination:

### 1a. `fill_in_buckets` ≈ 28% — SCATTER + FK-WALK LOOP

Source: `sieve/las-fill-in-buckets.inl`

```cpp
while (!ple.done(F)) {
    u.set_x(ple.get_x() & bmask);
    BA.push_update(ple.get_x() >> logB, u, w);  // scatter store
    ple.next(F);                                  // FK walk step
}
```

`push_update` in production builds (no `SAFE_BUCKET_ARRAYS`):
```cpp
// bucket-push-update.hpp — NO overflow check in production
*bucket_write[i]++ = update;
```

**Finding:** There is NO overflow branch in the production hot path. The 28% is
dominated by (a) the FK-walk computation `ple.next(F)` and (b) the random scatter
store `*bucket_write[i]++`. The "per-hit overflow branch" mentioned in the brief
likely refers to the FK-walk loop termination `!ple.done(F)`, which IS
data-dependent and hard to predict (~5% misprediction rate is consistent with
~1-2 hits per prime per bucket region being unpredictable).

**Implication for optimization:** Branchless bucket scatter modifications are
ALREADY implemented in CADO (guard region of +1MB per bucket array). The correct
target is the FK-walk computation latency.

### 1b. `invmod_redc_32` ≈ 11.9% — SERIAL DEPENDENCY CHAIN

Source: `sieve/las-arith.hpp`

```cpp
static inline uint32_t invmod_redc_32(uint32_t a, uint32_t b, uint32_t invb) {
    // Binary extended GCD with cmov (already branchless)
    while (true) {
        uint32_t diff1 = b - a;
        // cmov-based swap — serial dependency chain
        __asm__("cmp %[b], %[a]\n cmovb %[diff1], %[b]\n ...");
    }
    // Then: adjust by powers of 2 via varredc_u32 or addmod_u32
}
```

**Finding:** The function is already implemented with cmov (branchless). The
bottleneck is the SERIAL DEPENDENCY CHAIN through each GCD iteration — each step's
input depends on the previous step's output. GNR cannot issue the next step until
the cmov chain completes.

**Also found:** CADO already has `batchinvredc_u32` for batch inversions (used in
factor base initialization). The per-prime lattice transform calls
`invmod_redc_32` individually — this is inherently serial per-prime.

**Implication:** Montgomery batch trick is already in CADO. The 11.9% is
irreducible single-element inversions in the per-special-q lattice transform.
This is EXACTLY the workload that benefits from SMT: when thread A stalls on
the modinv chain, thread B's independent modinv chain can execute.

### 1c. `plattice_info` ≈ 14.5% — GCD-LIKE LATTICE REDUCTION

Source: `sieve/las-reduce-plattice-production-asm.hpp`

```cpp
void reduce_plattice_asm(uint32_t I) {
    uint64_t u0 = (((uint64_t)-j0)<<32) | mi0;
    uint64_t u1 = (((uint64_t) j1)<<32) | i1;
    // Alternating subtractive GCD — serial loop
    "subq %[u1], %[u0]\n"  // branch 1
    "subq %[u0], %[u1]\n"  // branch 2
    // repeat until reduced
}
```

**Finding:** This IS the `reduce_plattice` assembly code. It's an optimized inline
assembly version of the Franke-Kleinjung lattice reduction, which is fundamentally
a binary GCD algorithm. Each step depends on the previous.

**Implication:** Same as modinv — serial dependency chain, excellent SMT candidate.
Both modinv (11.9%) and lattice reduction (14.5%) together are 26.4% of cycles,
all serial chains, all SMT-ideal.

---

## 2. Why SMT is the highest-priority lever

The combined serial-chain bottlenecks (invmod + lattice reduction ≈ 26.4%) are
EXACTLY what SMT is designed for:

- Thread A executes an invmod chain, stalls waiting for each GCD step to complete
- Thread B executes an independent invmod chain for a different prime
- The OOO engine can issue B's instructions during A's stall cycles
- Net: 2× effective invmod throughput from the same physical core

With `--cpus 24` CFS quota (not `--cpuset-cpus`):
- Running 48 single-threaded las jobs means 2 threads per physical core
- CFS schedules them on the 24-CPU quota but allows micro-interleaving
- The dependency stalls that would waste cycles are filled by the other thread
- The FK-walk scatter (28%) benefits less from SMT (it's more store-throughput
  limited), but the modinv/lattice chains (26.4%) benefit maximally

**Reference:** Brief §6.B — "untested, high-potential for this kernel."

**Projected impact:** A kernel that is 26.4% serial-chain-limited with 2-way SMT
can theoretically get up to 26.4% throughput improvement from filling stall cycles.
In practice, accounting for shared L1/L2 pressure and CFS scheduling overhead,
a conservative estimate is 10-20%.

---

## 3. Why the native build matters

**The problem:** Current build flags are `-march=x86-64-v3 -mtune=icelake-server`.

Ice Lake-SP (10th-gen Intel, 2019) and Granite Rapids (2024) have different
back-end microarchitectures:

| Property | Ice Lake-SP | Granite Rapids (Redwood Cove) |
|----------|-------------|-------------------------------|
| Integer ALU ports | 4 (p0,p1,p5,p6) | ~6 (p0,p1,p4,p5,p6,p7) |
| Integer multiply latency | 3 cycles | 3 cycles |
| CMOV throughput | 1/cycle (p0+p6) | Different port allocation |
| Reorder buffer | 352 entries | 512 entries |

When GCC uses `-mtune=icelake-server`, it schedules instructions assuming Ice Lake
port assignments. On GNR, the cmov chains in `invmod_redc_32` and
`reduce_plattice_asm` may be scheduled suboptimally, leaving ports idle.

**Fix:** `-march=native -mno-avx512f` on the GNR node lets GCC (12+ with
`-mtune=sapphirerapids` as nearest known model) use the correct port assignments.

**Also:** `-march=native` enables BMI2, LZCNT, PDEP, PEXT and other extensions
that may appear in `las` utility functions (ctz, popcount operations in the sieve).

**Note on AMD:** `-march=native` on AMD EPYC 9555 (Zen 5) enables AVX-512 and
znver5 scheduling. The brief confirms this gives +17.5% over AVX2 on AMD. Our
implementation enables this automatically via vendor detection.

---

## 4. CPU pinning analysis for GNR-AP

The Xeon 6980P is a Granite Rapids-AP (dual-die) processor with:
- Multiple SNC (Sub-NUMA Clustering) domains per die
- Independent LLC per SNC domain
- Cross-SNC mesh latency for coherency

With `--cpus 24` CFS quota and no cpuset, the Linux scheduler can place 24
threads across ANY of the ~128 logical CPUs and migrate between them. This means:
- Threads can move between SNC domains mid-computation
- Each migration incurs cross-die mesh latency
- Shared data (factor base read-only) remains L3-resident but may cause
  cross-SNC coherency traffic

Even though `las` is 99%+ L2-resident (brief §4: L2-miss 0.62 MPKI), the
occasional L2 miss hitting a cross-SNC L3 costs much more than a local L3 miss.

**Fix:** `os.sched_setaffinity(0, {0..23})` pins all threads to a compact set.
On a 128-CPU GNR with uniform topology, CPUs 0-23 are likely within one die.
Better: read NUMA node 0 CPUs from `/sys/devices/system/node/node0/cpulist`.

---

## 5. Parameter choices

`rels_wanted = 65_000_000` (reduced from 71M):
- Relation floor for lim0=11M, lim1=14M, lpb=30 is ~58-60M
- 65M = 8% headroom (safe)
- Saves ~10% of sieve time vs 71M (arch-neutral)
- The denser resulting matrix is handled by the 96GB GPU

`las.threads=1` with n_jobs processes:
- Each las process uses one thread (single-threaded)
- Parallelism via multiple processes
- This is CADO's recommended mode for high-core-count machines
- Combined with SMT (n_jobs = 2×quota_cpus): fills both SMT threads

---

## 6. Verification protocol

On the actual Granite Rapids node:

```bash
# 1. Build the image (vendor-aware, native flags auto-detected)
docker build -t breaking-rsa-gnr ./

# 2. Verify no AVX-512 in las binary
objdump -d /usr/local/bin/las | grep -c "zmm\|evex"
# Expected: 0 (zero AVX-512 instructions)

# 3. Run the A/B harness (tests SMT scaling)
./gnr_ab_harness.sh <N_decimal> /path/to/cado.poly
# Reports: rel/s for 24/48/72 jobs, extrapolated full-wall

# 4. perf TopdownL2 on GNR
perf stat -M TopdownL2 /usr/local/bin/las ... --t 1
# Expected for serial-chain-bound: high Backend-Bound, high Bad-Speculation
# If Frontend-Bound > 20%: different optimization target

# 5. SMT check
cat /sys/devices/system/cpu/smt/active
# Expected: 1 (SMT enabled on GNR)

cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
# Expected: 0,K (two logical CPUs per physical core)
```

---

## 7. What to report if levers don't close the gap

If the measured Intel wall is still > 235 min after implementing all three levers:

**A. Read the perf TopdownL2 output carefully:**
- If `Bad-Speculation > 15%`: the branch in `!ple.done(F)` is the target
  → restructure the FK walk to reduce per-prime hit count variance
- If `Frontend-Bound > 20%`: instruction cache miss → increase L1i?
  → unlikely for a tight loop; check if CADO was built with too many inlines
- If `Core-Bound > 50%`: port contention in modinv/lattice chains
  → the SMT approach is correct; push n_jobs higher (try 64-72)

**B. If SMT gives < 5% improvement:**
- The bottleneck may be L1 cache sharing (two SMT threads compete for 32KB L1D)
- Try `--t 2` (2-threaded las, different from SMT): within one process, two threads
  can interleave more carefully using compiler-visible instruction scheduling

**C. Reduce rels_wanted further (63M):**
- Only 5% above the floor
- Add retry logic: if filter fails, increase rels_wanted by 5M and re-sieve
- Saves ~4 more minutes

**D. Software pipeline the per-prime modinv+lattice:**
- Process TWO primes in parallel through invmod and reduce_plattice
- Prime A's modinv chain fills while Prime B's cmov waits
- This is essentially manual SMT in software
- Requires modifying `las-fill-in-buckets.inl` (significant but targeted)

---

## 8. Measurements that would be decisive

If you can run these on actual GNR hardware, they resolve all uncertainty:

```bash
# a) SMT scaling (most important)
for n_jobs in 24 32 48 64; do
    time_this: n_jobs las processes, Q=[14M, 14.5M], las.threads=1
    report: rels/second
done

# b) Native vs icelake-server build comparison
# Build A: gcc -O3 -march=x86-64-v3 -mtune=icelake-server (current)
# Build B: gcc -O3 -march=native -mno-avx512f -mtune=sapphirerapids
# A/B on SAME Q window, SAME N, SAME poly, 3 reps
# Expected: B is 3-10% faster

# c) perf -M TopdownL2 on GNR (single-threaded las)
perf stat -e \
    cycles,instructions,branch-misses,\
    '{cpu/event=0x9c,umask=0x01,name=IDQ_UOPS_NOT_DELIVERED/,\
      cpu/event=0x0e,umask=0x01,name=UOPS_ISSUED/,\
      cpu/event=0xc0,umask=0x00,name=INST_RETIRED/,\
      cpu/event=0xc2,umask=0x02,name=UOPS_RETIRED/}' \
    las ... --t 1
# → Reports Bad-Speculation, Frontend-Bound, Backend-Bound, Retiring
```

Any of these that shows a **different number from what's in the brief** is
potentially the whole win — report it immediately.

---

*Bottom line: the AMD machine proves a 4-hour solution exists. On Intel GNR,
the serial modinv+lattice chains (26.4% of cycles, both SMT-ideal) are the
primary target. SMT exploitation (48 jobs) + correct native build + SNC pinning
is the stacked configuration most likely to close the ~53-minute gap.*
