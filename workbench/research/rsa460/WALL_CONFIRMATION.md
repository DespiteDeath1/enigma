# RSA-460 on Granite Rapids: breakthrough assessment and wall confirmation

This document supersedes the earlier conventional-tuning hypotheses in
`README.md` and `SOURCE_AUDIT.md`.  It incorporates the user's real Xeon 6767P
measurements:

- Best measured Granite Rapids stack: `icelake-server` build, 2-NUMA spread,
  `-t24`, **63.68 throughput units**, projected **321 min** full wall.
- Hard cap: **240 min**.
- Conventional stack measured dead: native GNR build, clang, SMT, compact
  pinning, PGO/LTO, AVX-512, parameter micro-sweeps, relation-floor rebalance,
  GPU lattice-sieve port, GPU cofactorization, and source hot-loop tweaks.

## 1. Required size of a real breakthrough

Using the measured 321 min wall and assuming 90% sieve time:

```bash
python3 -m workbench.research.rsa460.wall_model
```

Default output:

```json
{
  "current_wall_min": 321.0,
  "target_wall_min": 240.0,
  "current_throughput": 63.68,
  "required_total_speedup": 1.3375,
  "required_total_throughput": 85.172,
  "current_sieve_min": 288.9,
  "non_sieve_min": 32.1,
  "required_sieve_min": 207.9,
  "required_sieve_speedup": 1.3896103896103897,
  "required_sieve_throughput": 88.49038961038961,
  "sieve_time_cut_fraction": 0.28037383177570097
}
```

Interpretation:

- A whole-pipeline lever must raise the measured 63.68 throughput units to
  **85.17** (+33.75%).
- A sieve-only lever must raise the sieve throughput to **88.49** (+38.96%).
- A GPU/offload approach must remove **~28.0% of current CPU sieve time** while
  not adding equivalent GPU/filter/LA wall.
- If the GNR sieve is bandwidth-bound, a bandwidth-frugal layout must cut
  effective bytes per relation by **~28.0%** before overheads.

This is the main wall: any candidate that cannot plausibly remove roughly a
third of the remaining sieve cost cannot produce a 4-hour pass.

## 2. Bandwidth-frugal sieving: only credible structural CPU frontier

The real-chip result changes the target from instruction scheduling to memory
traffic.  CADO already implements multi-layer bucket sieving, which is the
standard cache-friendly transformation: generate `(bucket, x, hint)` updates,
radix-sort them through bucket levels, and apply them region-locally.

Source facts from upstream CADO (`sieve/bucket.hpp`):

| Update type | CADO size |
|---|---:|
| `bucket_update_t<1, shorthint_t>` | 4 B |
| `bucket_update_t<1, emptyhint_t>` | 2 B |
| `bucket_update_t<2, shorthint_t>` | 8 B |
| `bucket_update_t<2, emptyhint_t>` | 4 B |
| `bucket_update_t<3, shorthint_t>` | 8 B |
| `bucket_update_t<1, longhint_t>` | 8 B |
| `bucket_update_t<2, longhint_t>` | 16 B |

The one real source-level memory-layout candidate is already hinted by CADO:

```cpp
/* TODO: create a fake 24-bit type as uint8_t[3]. */
template <> struct bucket_update_size_per_level<2> {
    using type = uint32_t;
};
```

For level-2 short updates, the logical payload is 24-bit position + 16-bit
hint = 5 bytes, but CADO stores it as an 8-byte aligned type.  A packed 5-byte
or practical 6-byte layout would reduce bucket-update bytes for that one class.

### Static traffic bound

Best case, every bandwidth-significant update is level-2 short:

```bash
python3 -m workbench.research.rsa460.bucket_traffic_model \
  --packed-level2-shorthint-bytes 6
```

Result: 8 B -> 6 B, **25.0% traffic cut**, which is already below the required
28.0% before accounting for unpacking/unaligned-store overhead.

With a perfect 5-byte packed type:

```bash
python3 -m workbench.research.rsa460.bucket_traffic_model \
  --packed-level2-shorthint-bytes 5
```

Result: 8 B -> 5 B, **37.5% traffic cut**, but only in the unrealistic case
where all traffic is this update class.

A mixed scenario with 45% level-2 short, 10% level-2 long, 25% level-1 long,
20% level-1 short:

```bash
python3 -m workbench.research.rsa460.bucket_traffic_model \
  --packed-level2-shorthint-bytes 5 \
  --packed-level2-longhint-bytes 10 \
  --fraction level2_shorthint=0.45 \
  --fraction level2_longhint=0.10 \
  --fraction level1_longhint=0.25 \
  --fraction level1_shorthint=0.20
```

Result: **24.4% traffic cut**, still below the threshold even with optimistic
5-byte packing and no runtime penalty.

Closed-form threshold: if every non-level-2-short byte remains unchanged, a
5-byte packed level-2 short update must represent at least:

```text
required_fraction >= 0.2804 / (1 - 5/8) = 74.8%
```

of the bandwidth-significant update traffic to hit the required byte cut.  A
6-byte practical layout cannot hit the threshold even if level-2 short updates
are 100% of the traffic.

### Verdict on bandwidth-frugal CADO rewrite

This is the only CPU-side idea I found that is structurally different from the
measured-dead tuning ledger.  It is worth one measurement if the turnkey
harness can report update-class traffic shares, but it does **not** currently
project a pass:

- Best practical 6-byte level-2 packing: upper bound **25% traffic cut** in an
  impossible all-level-2-short scenario; below required 28%.
- Perfect 5-byte level-2 packing: needs **~75%** of traffic to be level-2
  short; likely not true once downsort, level-1 long hints, sieve-array walks,
  survivor scan, and factor-base/root data are included.
- Packed/unaligned updates add decode and store complexity; a bandwidth-bound
  loop may gain less than the static byte cut.

Exact measurement if pursuing:

1. Instrument CADO to count update bytes by `bucket_update_t<LEVEL,HINT>` class
   on a fixed q-window.
2. If `level2_shorthint` is >=75% of bandwidth-significant traffic, prototype a
   packed 5-byte type and gate byte-identical relations.
3. If it is <75%, stop; this path cannot close the GNR wall alone.

## 3. GPU-native relation generation

Recent public material still points to three categories:

1. CPU CADO lattice sieving with GPU cofactorization/follow-up.
2. GPU ECM/CGBN kernels for batches of independent cofactorization tasks.
3. Experimental/mining workflows that use GPU ECM to prefilter candidates
   before conventional GNFS.

Relevant references:

- CADO-NFS project documentation: relation search is CPU lattice sieving.
- Bernstein et al./EPFL-style GPU cofactorization work: offloads
  cofactorization/follow-up after CPU sieving emits candidates.
- CGBN/GPU GMP-ECM: good for many fixed-size big-number ECM tasks, not for
  replacing FK lattice walk + bucket update generation.
- FACT0RN/GPUDispersion: GPU ECM prefiltering for candidate mining, not a
  single fixed generic c139 semiprime relation generator.

The threshold from Section 1 is severe: the GPU must remove about **28% of CPU
sieve wall**, not merely accelerate the ~7% cofactorization slice.  A GPU
pipeline that only takes candidate norms after CPU lattice walking cannot
reach the target.  It would need to generate a material fraction of actual NFS
relations itself.

I found no 2024-2026 public GPU-shaped GNFS relation generator that avoids the
known failure mode: FK walk and bucket update generation are irregular,
branchy, and sparse; mapping the CPU lattice sieve directly to warps produces
divergence and uncoalesced writes.  A fundamentally different GPU formulation
would need to batch many special-q's and sort/coalesce updates on GPU, but that
recreates CADO's bucket-sort traffic with worse control flow and then still
needs CPU-compatible relation output.  Without measured GPU rel/s near at least
25-30% of the CPU relation rate, it cannot affect the wall.

Verdict: **no concrete GPU relation source found that projects below 240 min**.
GPU cofactorization/ECM remains useful but is too small after the measured
cofac share.

## 4. Faster-than-GNFS alternatives at 460 bits

For a balanced 230-bit x 230-bit generic semiprime:

- ECM targets the smaller prime.  Here the smaller prime is ~230 bits
  (~69 decimal digits), far outside a 4-hour single-GPU/24-core ECM search.
- Pollard rho is `O(sqrt(p)) ~= 2^115`, impossible.
- Fermat is ruled out by `|p-q| > 2^130`.
- p-1/p+1 require exceptional smoothness of `p±1`; no structure is present.
- SIQS/MPQS has `L[1/2]` complexity and is not competitive with GNFS at c139.
- SNFS requires algebraic form; the generator is random.

GNFS remains the only practical general algorithm.  The known degrees of
freedom inside GNFS (polyselect, large-prime variants, relation floor, matrix
density) are already in the measured-dead ledger or bounded to <12%.

Verdict: **no legitimate non-GNFS path projects to 4 hours** for this input
class on the stated hardware.

## 5. Mathematical work reduction

To pass from 321 min, a parameter or polynomial change must cut roughly 28% of
CPU sieve time end-to-end.  The measured data rules out the usual candidates:

- Better Murphy-E did not translate to real rel/q and was worse by 34%.
- Relation-floor cuts are bounded and risk hard purge failure around 65M.
- Three-large-prime/composite special-q regimes measured noise-level or worse.
- Target density and LA rebalance are dead; fewer relations make the matrix
  smaller, not a GPU-absorbed larger workload.

The only remaining mathematical escape would be a polynomial with **real**
`las` rel/q improvement of ~30% at the same matrix viability, not a Murphy-E
improvement.  For generic random c139 and an already tuned c140 polynomial, I
do not see a mechanism that would produce that size jump without contradicting
the existing real-sieve poly measurements.

## 6. Independent verdict

Given the user's real 6767P measurements and the source/literature audit here,
I do **not** find a legitimate method that projects both validators below 240
min.

The wall is not "GNFS is impossible"; AMD proves the algorithm is fine on Zen 5.
The wall is specifically:

```text
Granite Rapids best measured wall      321 min
Target                                240 min
Required total speedup                1.3375x
Required sieve-only speedup           1.3896x
Required CPU-sieve/offload cut        ~28.0%
Measured conventional stack left      no lever of that size
Known GPU/cofactor share              too small
Known CADO memory-layout candidate    likely below threshold
Known alternative algorithms          asymptotically/practically worse
```

So the honest conclusion is **B: independent confirmation of the wall**, with
one narrow falsification test:

> If instrumentation shows >=75% of bandwidth-significant bucket traffic is
> `bucket_update_t<2, shorthint_t>`, then a risky packed 5-byte update rewrite
> might be worth prototyping.  Otherwise, even the freshest bandwidth-frugal
> angle cannot supply the required cut.

If challenge rules require the same c139 instance to pass both AMD EPYC 9555
and Intel Xeon 6980P under a 24-core quota, the current evidence supports
requesting a rule/hardware adjustment rather than expecting further legitimate
software tuning to close the gap.
