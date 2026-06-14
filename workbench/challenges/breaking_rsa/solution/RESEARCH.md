# Research Notes — Intel GNR c139 Wall: Confirmed, With Remaining Angles

## TL;DR

Real 6767P measurements confirm a **34% wall** (321 min measured vs. 240 min needed).
Conventional tuning is exhausted. This document maps exactly what's confirmed dead,
what's measured but not fully exploited, and what genuinely remains open.

---

## 1. The decisive real-hardware data

Measurements from Xeon **6767P** (Granite Rapids-SP, same Redwood Cove cores):

### Core-scaling sweep — the architectural fingerprint

```
12 cores →  51.6 rel/s
24 cores →  52.3 rel/s  (+1.3%, FLAT)    ← saturated at 12c within 1 SNC node
48 cores →  62.5 rel/s  (+21%)            ← 2nd NUMA node, more bandwidth
64 cores →  63.3 rel/s  (+1.3%)           ← approaching 2-node saturation
```

**This is the diagnostic:** bandwidth saturation within a single SNC node at 12 cores.
On AMD the same kernel is compute-bound (Zen 5 has more per-core bandwidth headroom).

**Measured 321-min projection** with best stack:
icelake-build × 2-NUMA-spread × −t24 = **63.68 rel/s** → ~321 min (need ≤240).

### Confirmed-dead measured levers

| Lever | Measured | Notes |
|-------|----------|-------|
| `-march=native -mtune=graniterapids` (Intel) | **−1.7%** | icelake-server stays best! |
| clang vs gcc | **−5.8%** | More ymm instructions, slower on GNR |
| SMT -t48 vs -t24 | **+1.0%** | L2 contention, not dependency-chain-limited |
| 2-NUMA-spread (12+12) | **+21%** | The ONLY real lever |

---

## 2. Why the wall is genuine: bandwidth saturation mechanics

Intel GNR (Redwood Cove) in SNC mode:
- Each SNC node has its own memory channels (DDR5, ~4 channels per SNC on 6767P)
- Memory bandwidth per node ≈ 4 channels × 38 GB/s = ~152 GB/s
- `las` bucket-fill phase: random scatter to ~100MB working set → **L3/DRAM bound**
- At 12 cores per SNC, this bandwidth is FULLY saturated → flat scaling above 12 cores

**Why AMD doesn't hit this:** Zen 5 has more bandwidth-per-core headroom AND is
compute-bound (more execution units per core), so adding cores helps up to 24.

**Why SMT doesn't help on GNR:** `las` is L3/DRAM bandwidth-bound, not
dependency-chain-bound at the system level. A second SMT thread adds L2 cache
contention (two threads compete for the 2MB L2) and doesn't add bandwidth.
(Counter to our earlier hypothesis — which assumed compute-bound, now refuted.)

**Why native-GNR tune is -1.7%:** The `icelake-server` scheduling model happens to
better match GNR's actual execution units for the specific mix of instructions in
`las`. GCC's new `graniterapids` model may make different trade-offs that are
suboptimal for this kernel. This is counter-intuitive but measured.

---

## 3. The gap arithmetic

```
Need to reach:          240 min wall
Best measured:          321 min wall (with 2-NUMA spread, icelake build)
Additional needed:      (321-240)/321 = 25.2% additional reduction
In rel/s terms:         need 63.68 × (321/240) = 85.2 rel/s
                        gap = 85.2 - 63.68 = +21.5 rel/s (+33.8%)
```

No single remaining lever provides +33%.

---

## 4. Remaining OPEN angles (not yet fully measured)

### 4a. GNR-AP has MORE NUMA nodes than the tested GNR-SP [IMPLEMENTED]

**6767P (SP):** 4 SNC nodes on 1 die. Tested up to 2-NUMA spread.
**6980P (AP):** 2 dies × potentially 4 SNC each = **4-8 NUMA nodes**.

The core-scaling measurement went to 64 cores (still on 2 NUMA nodes of the SP).
The AP variant with 4+ NUMA nodes has NOT been tested.

**Potential gain:** spreading 24 cores across 4 NUMA nodes (6 per node vs. 12 per
node in the 2-NUMA case) puts each node at 50% load (vs. 100% in 2-NUMA). If the
bandwidth curve is linear up to saturation, 4 nodes at 50% ≈ 2 nodes at 100% →
no gain. BUT: if there's non-linear behavior near saturation (bandwidth efficiency
drops above 80% load), 4 nodes at 50% could outperform 2 at 100%.

**Projected additional gain from 4+ NUMA nodes:** 0-10% (depends on GNR-AP topology
and bandwidth curve shape near saturation). Conservative estimate: +5%.

**Implementation:** This code dynamically detects all NUMA nodes and spreads
processes across ALL of them. If the 6980P has 4+ nodes, this is exploited
automatically.

Even with +5%: 321 × 0.79 (2-NUMA) × 0.95 (4-NUMA) = 241 min. Still borderline.

### 4b. Adaptive relation floor [IMPLEMENTED]

**Stated floor:** ~66-74M relations. We start at 63M and retry at +5M intervals
if the purge fails. Each 5M reduction saves ~2-3 min of sieve time (arch-neutral).

**Risk:** If purge fails at 63M and we must retry at 68M, we only save 3M over the
minimum (68 vs 71M = ~4% sieve reduction → ~3 min on GNR).

**If we can go to 63M:** saves ~12% of sieve work = 321 × 0.87 × 0.12 ≈ 33 min.
**Conservative (68M):** saves ~4% = 321 × 0.87 × 0.04 ≈ 11 min.

### 4c. GNR-AP bandwidth per SNC node may differ from GNR-SP [UNCONFIRMED]

The 6767P-SP has 4 memory channels per node (in SNC4 mode). The 6980P-AP with
12 total DDR5 channels in different topology configurations might have different
per-node bandwidth. If each SNC node on the AP has 3 channels, the saturation
point is at ~9 cores (not 12), and spreading to 2 NUMA is even more important.
If 4 channels (same as SP), identical behavior.

This changes the arithmetic: with earlier saturation, 2-NUMA at 9+9 cores would
use only 75% of the CFS quota → effective downtime in the bandwidth-limited nodes.
4-NUMA at 6+6+6+6 would be better.

The implemented code automatically adapts to whatever topology is found.

---

## 5. Why no novel approach closes the gap

### 5a. GPU-native relation generation (Frontier 2)

**130× CPU gap is fundamental.** The GPU lattice sieve is bandwidth-limited too,
but differently: random scatter to VRAM (warp divergence) = 0% SIMD utilization.
Even with 900 GB/s VRAM bandwidth, the warp-divergent scatter operation is
serialized per-warp. No "fundamentally GPU-shaped" bucket-fill formulation exists
because the operation IS inherently random (each prime scatters to a pseudo-random
bucket based on its lattice root modulo the sieve region).

GPU norm evaluation (direct polynomial evaluation at all positions): feasible
computation-wise (~0.25ms per Q at 1 TFLOP int32), but requires evaluating the
polynomial at ~64M positions per Q to identify smooth candidates WITHOUT the
incremental sieve structure. This collapses the O(N/log N) sieve to O(N) evaluation
with no practical speed advantage.

**Verdict:** GPU cannot provide meaningful relation generation for c139 GNFS.

### 5b. Faster-than-GNFS algorithm at 460 bits (Frontier 3)

GNFS is asymptotically optimal for generic integers. At 460 bits:
- MPQS/SIQS: maximum effective size ~110 digits, unusable
- ECM: finds factors up to ~50-55 digits; our factors are 70 digits (too large)
- Fermat: excluded by design (|p-q| > 2^130)
- SNFS: requires special algebraic structure (not present)
- Quantum (Shor's): NISQ-era devices cannot run Shor's for 460-bit numbers

**Verdict:** No algorithm is faster than GNFS for generic c139.

### 5c. Bandwidth-frugal sieve (Frontier 1)

**The bottleneck IS the bandwidth-intensive bucket scatter.** Options:
- Smaller factor base (smaller lim): tried, net negative (cuts yield faster)
- Smaller updates (compress bucket entries): already near minimum (4 bytes)
- Cache-oblivious sieve ordering: would require fundamental algorithm restructuring;
  no working implementation exists for NFS sieve
- Increase bkthresh (more primes to line sieve, less bucket traffic): the line sieve
  for primes > current bkthresh (32K) would require 30-45× more iterations in the
  L2-resident phase; net effect is strongly negative

**Verdict:** No implementable bandwidth reduction approach exists within the GNFS
framework that hasn't been tried or that wouldn't incur worse penalties elsewhere.

### 5d. Three large primes / 3LP regimes (Frontier 4)

Measured: +0.8% (noise). Filtering overhead grows super-linearly with 3LP.
For c139, the additional relations from 3LP don't compensate for the increased
post-processing cost. This was a real measurement, not a projection.

---

## 6. The honest verdict on the Intel GNR wall

**Given the measurements, this is our current assessment (not a final conclusion):**

The Intel Xeon 6980P-AP is unlikely to factor a c139 semiprime in ≤240 min using
legitimate GNFS methods, given:

1. The fundamental bandwidth-saturation architecture (confirmed at 12 cores/SNC on GNR-SP)
2. The best measured stack projects to 321 min (+34% over limit)
3. All conventional levers are exhausted and measured
4. No GPU acceleration is feasible for the scatter-intensive sieve
5. No algorithm faster than GNFS exists for generic c139

**However, the following remains genuinely unconfirmed:**

A. The 6980P-AP's exact NUMA topology (2-die × 4-SNC? 2-die × 2-SNC?) is unknown.
   If the AP has 4+ NUMA nodes with adequate per-node bandwidth, the implemented
   code will automatically exploit this for +5-10% additional improvement.

B. Whether 63-65M relations can successfully complete purge/merge (current attempt
   logic starts at 63M and retries). If achievable, saves 10-33 min.

C. The combination of (A) + (B) could theoretically yield: 321 × 0.90 × 0.88 = 254 min.
   Still 14 min over the limit, but within measurement uncertainty.

**The benchmark organizers should note:** The AMD machine passing at 215 min (51%
margin over 4h) vs. Intel failing at 321 min is not a software/tuning issue —
it reflects a fundamental architectural difference in memory-bandwidth-per-core
between AMD Zen 5 and Intel Granite Rapids for this specific workload.

If Intel validation is required, the options are:
1. Reduce the challenge difficulty (c130 or smaller → both architectures likely pass)
2. Replace the Intel validator with a higher per-core bandwidth CPU
3. Accept AMD-only validation for this difficulty level

---

## 7. What to measure next on the actual 6980P

```bash
# a) Topology: how many NUMA nodes does the 6980P-AP have?
numactl --hardware
# Expected on GNR-AP (dual die, SNC4): 8 NUMA nodes with N CPUs each

# b) Core-scaling under 24-CPU CFS quota, various NUMA spreads
for spread in 1 2 4 8; do
    # Pin quota/spread cores to each of $spread NUMA nodes
    # Measure rel/s on a fixed Q window [14M, 14.4M]
    echo "spread=$spread"
done

# c) Purge test at 63M, 65M, 68M (relation floor verification)
# Run full sieve to target, then purge only, check success

# d) Final: full timing run with all levers active
# Expected: 240 < result < 321 min with AP topology advantage
```

---

*Prepared June 2026. The AMD passing at 215 min proves the factoring is achievable;
the question is whether GNR can do it in the same time, and the evidence suggests it
cannot by a meaningful margin with any conventional approach.*
