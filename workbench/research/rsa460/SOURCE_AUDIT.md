# CADO `las` hot-path source audit for RSA-460

Audited upstream CADO-NFS source revision:
`68af200f39b8f14fd53455750a387d3c504e6d68`.

This note maps the mission's Granite Rapids profile to concrete source files
and distinguishes already-implemented paths from patch candidates.

## Modular inverse / root transform

Relevant files:

- `sieve/las-fbroot-qlattice.hpp`
- `sieve/fb.cpp`
- `sieve/fb.hpp`
- `tests/sieve/test_batchinv.cpp`
- `tests/sieve/test_invmod_redc_32.cpp`

Findings:

1. **Batch REDC inversion exists.** `fb_root_in_qlattice_31bits_batch()`
   transforms all roots for a simple factor-base entry with one
   `batchinvredc_u32()` call.  The comment states the intended cost model:
   "3 calls to redc_32 per root and 1 to batchinvredc_u32 for the whole
   batch."
2. **Production simple entries with 2+ roots try this batch path.**
   `fb_entry_x_roots<Nr_roots>::transform_roots()` calls
   `fb_root_in_qlattice_batch()` and falls back to scalar
   `fb_root_in_qlattice()` only if a transformed root is projective.
3. **One-root entries are deliberately scalar.**
   `fb_entry_x_roots<1>::transform_roots()` says batch "does not save
   anything and just adds a little overhead" and calls scalar
   `fb_root_in_qlattice()`.  This is true for isolated entries, but may be
   false on Granite Rapids when batching denominators across many consecutive
   one-root entries in a slice hides the serial inverse dependency chain.
4. **General entries are not batched.** `fb_entry_general::transform_roots()`
   has an explicit `TODO: Use batch-inversion here` and transforms each root
   one at a time.
5. **`SUPPORT_LARGE_Q` disables batch root transform.**
   `fb_root_in_qlattice_batch()` returns `false` under `SUPPORT_LARGE_Q`
   because the 127-bit batch implementation is disabled.  Normal `las` does
   not define `SUPPORT_LARGE_Q`; `las_descent` and SIQS do.  Confirm the
   production c139 binary prints no `SUPPORT_LARGE_Q` flag before assuming the
   31-bit batch path is active.

Patch candidates to measure:

- **A1: cross-entry batch for one-root simple entries.**
  In `fb_slice<fb_entry_x_roots<1>>` processing, collect a run of denominators
  for the same `qlattice_basis` and call `batchinvredc_u32()` once per block
  (e.g. 16 or 32 entries), then finish numerator multiplication per entry.
  This attacks the observed `invmod_redc_32` share that remains after existing
  per-entry batching.
- **A2: implement the existing TODO for `fb_entry_general`.**
  General entries are rarer, so this is lower expected magnitude unless perf
  shows `fb_entry_general::transform_roots()` inside the `invmod_redc_32`
  sample stack.
- **A3: runtime 31-bit path guard.**
  If any production wrapper accidentally builds with `SUPPORT_LARGE_Q`, add a
  runtime `basis.fits_31bits()` fast path that calls the existing 31-bit batch
  transform and falls back to the 127-bit scalar reference only for true
  large-q cases.

Expected magnitude:

- Existing batch support means "batch all inverses" is not a free 11.9% win.
  The measurable target is the residual scalar `invmod_redc_32` sample share.
  If one-root/simple entries dominate that residual, A1 can plausibly recover
  3-7% wall; otherwise it is likely <2%.

Required measurement:

```bash
perf record -F 997 -g --call-graph dwarf -- \
  "$LAS" -poly "$POLY" -q0 11000000 -q1 11200000 \
  -lim0 11000000 -lim1 14000000 -lpb0 30 -lpb1 30 \
  -mfb0 60 -mfb1 60 -I 13 -sqside 1 -t 1

perf report --stdio | rg 'invmod_redc_32|transform_roots|fb_root_in_qlattice'
```

If most `invmod_redc_32` samples are under
`fb_entry_x_roots<1>::transform_roots`, prioritize A1.  If they are under
`fb_entry_x_roots<Nr_roots>` with `Nr_roots >= 2`, the batch path is failing
or disabled and that is a larger correctness/configuration issue.

## Bucket scatter

Relevant files:

- `sieve/las-fill-in-buckets.inl`
- `sieve/bucket-push-update.hpp`
- `sieve/las-threads-work-data.cpp`
- `sieve/las.cpp`

Findings:

1. **Normal production `push_update()` is branchless for capacity.**
   `bucket_array_t<LEVEL,HINT>::push_update()` is just:
   `*bucket_write[i]++ = update;`
   unless `SAFE_BUCKET_ARRAYS` is defined.
2. **The capacity check is a debug/safety macro.**
   The only per-push capacity branch in `push_update()` is behind
   `#ifdef SAFE_BUCKET_ARRAYS`.  `las.cpp` prints a warning if this is on.
3. **Bucket fullness is handled after the fact.**
   `nfs_work::check_buckets_max_full*()` can throw `buckets_are_full`, and
   `las.cpp` grows `bkmult` and redoes the special-q.  That is not a branch
   per update on the hot path.
4. **The hot unpredictable branch in the visible fill loop is more likely
   `ple.probably_coprime(F)` or rare-shape checks, not overflow.**
   The ordinary loop in `fill_in_buckets_toplevel()` tests
   `if (LIKELY(ple.probably_coprime(F)))` before `BA.push_update()`.

Patch candidates to measure:

- **B1: verify production flags.** Ensure `SAFE_BUCKET_ARRAYS` and
  `SAFE_BUCKETS_SINGLE` are not in the shipped binary.  If either is enabled,
  disabling it is a direct hot-path win and also explains the brief's
  "overflow branch" observation.
- **B2: branch-source perf gate.** Use Last Branch Records or annotated perf to
  identify whether the mispredicts are from `probably_coprime`, special-shape
  checks, or a non-upstream patch.
- **B3: split coprime/no-coprime variants.** There are already sublattice paths
  that skip `probably_coprime`.  If GNR shows mispredicts concentrated there,
  specialize the no-sublattice hot loop by batching several `ple.next(F)` steps
  and materializing only accepted updates, or try a branchless mask/predicated
  store variant.  This is riskier than A1 because unconditional stores corrupt
  bucket contents; it needs a byte-identical relation gate.

Exact audit commands:

```bash
"$LAS" -v 2>&1 | rg 'SUPPORT_LARGE_Q|SAFE_BUCKET'
objdump -d "$LAS" > /dev/shm/las.objdump
rg 'bucket_array_t|SAFE_BUCKET|zmm|ymm1[6-9]|ymm2[0-9]|ymm3[0-1]' /dev/shm/las.objdump
```

## Practical stacked config to test first

The source audit reduces the highest-probability stack to:

1. **Build:** native Granite Rapids AVX2-only (`gcc-native-no512` or
   `clang-v3-native-no512`) to avoid AVX-512 frequency while retuning integer
   scheduling.
2. **Placement:** compact same-LLC/SNC pinning because validator `--cpus` is a
   quota, not a cpuset.
3. **SMT:** `-t 32` and `-t 48` under the 24-core quota, pinned so SMT siblings
   are deliberate.
4. **Parameter:** relation floor sweep (`71M, 66M, 62M, 60M`) only after the
   best build/placement/SMT is chosen.
5. **Code patch:** only pursue cross-entry one-root batch inversion if perf
   confirms residual `invmod_redc_32` samples are in the one-root scalar path.

This is the stack most likely to produce a real 20% without relying on a
large, risky rewrite of the scatter loop.
