# Breaking RSA / RSA-460 CADO-NFS Research Notes

This note focuses on the c139 / ~460-bit semiprime case described in the
research brief. The repo does **not** contain the tuned CADO-NFS miner from the
brief; it contains the challenge harness, validator-equivalent Docker runner,
and a reference msieve/ECM solver. The highest-value deliverable here is
therefore:

1. re-verify what the harness actually does;
2. document the strongest missed lever visible from the harness; and
3. provide exact experiments to run on AMD and Granite Rapids.

## Re-verified from this repo

### 1) Validator CPU limiting is quota-only, not a cpuset

The validator and workbench currently apply the CPU budget with `docker run
--cpus 24`; they do **not** pass `--cpuset-cpus`.

Relevant code:

- `qbittensor/validator/solution/run_solution.py`
- `qbittensor/validator/solution/constants.py`
- `workbench/runner/docker_runner.py`

Why this matters:

- `--cpus` is a CFS quota, not an affinity mask.
- Inside the container, `os.cpu_count()` may still report the host CPU count.
- The process may migrate across many host CPUs / LLC domains unless the miner
  pins itself.
- This is especially risky on Xeon 6980P / Granite Rapids-AP where topology is
  more fragmented (multiple SNC domains / LLC regions) than a simpler single-
  socket mental model suggests.

This repo now updates the reference solver to size itself from cgroup quota /
affinity rather than trusting `os.cpu_count()`.

### 2) Challenge generation matches the brief's "generic semiprime" claim

`qbittensor/challenges/breaking_rsa.py` generates two balanced primes by:

- drawing random half-width values,
- forcing top and bottom bits,
- applying `next_prime`,
- rejecting near-Fermat pairs via `abs(p - q) > 2^(bits/2 - 100)`.

`qbittensor/validator/solution/challenge_inputs/breaking_rsa_setup.py` seeds
generation with `secrets.randbits(256)`.

That does not suggest a structural shortcut through the validator-side
generator.

## Highest-probability missed lever

### H1: thread count and affinity are wrong inside the container

If a CADO wrapper defaults to `os.cpu_count()`, `nproc`, or `-t all`, then on a
validator that uses `--cpus 24`:

- the miner may think it has the host's full CPU count;
- CADO may spawn too many workers;
- worker placement may smear across NUMA / SNC / LLC boundaries; and
- Granite Rapids can lose more than AMD because the topology penalty is larger.

This hypothesis is cheap to falsify and can explain a double-digit Intel-only
loss without requiring any new mathematics.

### What to check first on both machines

Inside the actual miner container, print:

```bash
python3 - <<'PY'
import math, os, pathlib

def read(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return None

host = os.cpu_count()
aff = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
cpu_max = read("/sys/fs/cgroup/cpu.max")
quota = period = None
if cpu_max and cpu_max.split()[0] != "max":
    quota, period = map(int, cpu_max.split()[:2])
print({"host_visible": host, "affinity": aff, "cpu.max": cpu_max, "quota_over_period": (quota, period)})
PY
```

If `host_visible` is much larger than 24, audit the miner immediately for:

- `os.cpu_count()`
- `multiprocessing.cpu_count()`
- `nproc`
- CADO defaults such as `-t all`
- wrappers that derive client counts from visible host CPUs

### Affinity A/B that should be run before deeper GNFS tuning

For the same CADO build, polynomial, and sieve window:

1. **default scheduler placement**
2. **compact 24-core placement** via `taskset` / `sched_setaffinity`
3. **one-SNC-domain placement** if the per-domain core count permits a useful run
4. **two- or three-domain compact placement** with equal distribution

Measure:

- rel/s during the steady-state `las` window
- `perf stat` counters for instructions, cycles, branches, branch-misses
- projected full wall time from the same q-range slice

If compact placement buys a meaningful win on Granite Rapids but not AMD, the
4-hour gap may be mostly a topology-control problem, not a GNFS-parameter
problem.

## External evidence review

These are not proofs; they are priors for where to spend time.

### GPU full lattice sieve still looks weak

Public 2024-2026 material did not turn up a production-ready GNFS lattice sieve
that is likely to replace CADO `las` at c139 on an RTX PRO 6000. There is
plenty of GPU work on *lattice* sieving in the SVP sense, but that is a
different workload.

Implication: treat "GPU full sieve" as unproven until it shows a rel/s win on a
real c139 window, not in synthetic microbenchmarks.

### GPU cofactorization remains plausible, but Blackwell is risky

Older literature reports meaningful speedups from GPU-assisted NFS
cofactorization, but recent public notes around CGBN on Blackwell indicate that
the standard library path is unstable or extremely slow on `sm_120` unless you
own the kernel stack and tune it specifically.

Implication: GPU cofactorization is still worth measuring, but only with an
integration path that is already known-good on Blackwell. Do not assume older
CUDA big-int code will transfer cleanly.

### Compiler / codegen work is still alive on Granite Rapids

Recent Granite Rapids compiler guidance points in a direction consistent with
the brief:

- prefer AVX2 / 256-bit width by default for sustained throughput;
- test GCC 15 `-march=diamondrapids`;
- test `-O2` vs `-O3` on branchy code;
- test PGO for front-end / branch-layout wins;
- use AVX-512 only when a measured kernel-level win survives clock changes.

Implication: the highest-probability CPU-side lever is not "generic more flags",
but a narrow matrix:

- GCC 15 vs Clang vs icx
- `-O2` vs `-O3`
- PGO vs non-PGO
- AVX2 baseline vs carefully bounded AVX-512 experiments

## Upstream CADO source audit: important contradictions

I cloned upstream CADO-NFS locally at:

- repo: `https://github.com/cado-nfs/cado-nfs`
- revision: `68af200f39b8f14fd53455750a387d3c504e6d68`

That source audit turned up two contradictions that are worth treating as
first-class re-verification targets.

### 1) Upstream bucket push has no production overflow branch

In upstream CADO:

- `sieve/bucket-push-update.hpp`
- `bucket_array_t<LEVEL, HINT>::push_update(...)`

the hot write path is simply:

```c++
*bucket_write[i]++ = update;
```

The capacity check exists only under `SAFE_BUCKET_ARRAYS`.

Implication:

- If your profile really shows a per-hit overflow/capacity branch in bucket
  fill, that is probably **not upstream CADO**.
- Either the build enables a safety mode, or the miner carries a local fork, or
  the symbolization attributed a different branch to that region.

This is exactly the kind of contradiction that can flip the optimization plan.
Do not spend days "removing the bucket overflow branch" before confirming that
your shipped binary actually contains one.

### 2) Upstream already batches the common simple root transforms

In upstream CADO:

- `sieve/fb.cpp`: `fb_entry_x_roots<Nr_roots>::transform_roots(...)`
- `sieve/las-fbroot-qlattice.hpp`: `fb_root_in_qlattice_31bits_batch(...)`
- `sieve/las-arith.hpp`: `batchinvredc_u32(...)`

the common simple multi-root path already uses one batch inversion for the
whole root set.

Implication:

- "Add Montgomery batch inversion to the common simple path" is **already done**
  upstream.
- If `invmod_redc_32` still burns ~12 % of cycles in your profile, that cost is
  likely coming from:
  - one-root transforms,
  - general-entry transforms,
  - projective fallback paths,
  - or some other call site outside the already-batched fast path.

### 3) But there are still real batching gaps in upstream

There are still upstream holes:

- `sieve/fb.cpp`: `fb_entry_general::transform_roots(...)` has:
  - `/* TODO: Use batch-inversion here */`
- `sieve/las-fbroot-qlattice.hpp`: under `SUPPORT_LARGE_Q`,
  `fb_root_in_qlattice_batch(...)` currently returns `false`, forcing scalar
  fallback.

Implication:

- If your c139 configuration encounters a meaningful fraction of general entries
  or large-q transforms, there is still a legitimate batching lever left.

### 4) Upstream plattice reduction is already hand-written asm

Upstream `plattice_info::reduce(...)` dispatches to:

- `sieve/las-reduce-plattice-production-asm.hpp`

on amd64/GCC-style inline asm builds.

Implication:

- "Just use PGO/LTO" is unlikely to move this path much.
- The real source-level levers are:
  - retuning the subtractive-block / divide schedule for Granite Rapids,
  - or changing the higher-level algorithmic mix, not asking the compiler to
    rediscover a different asm schedule.

## Source-level opportunities that are still alive

These are the highest-value upstream-backed patch ideas I found.

### S1) Batch the remaining `fb_entry_general` transforms

Patch target:

- `sieve/fb.cpp`: `fb_entry_general::transform_roots(...)`

Current state:

- root transforms are done one-by-one
- the file itself carries a TODO for batch inversion

Expected upside:

- low if general entries are rare
- meaningful if the chosen factor base / special-q regime generates many prime
  powers, projective roots, or non-simple entries

Projected Intel impact:

- **0-4 %** by itself, but only if general-entry traffic is nontrivial

### S2) Preserve batching when only one root in a batch is exceptional

Patch targets:

- `sieve/las-fbroot-qlattice.hpp`
- `sieve/fb.cpp`

Current state:

- if any denominator in a batch becomes noninvertible, the whole batch falls
  back to scalar root-by-root transforms

Patch idea:

- batch the affine subset
- scalar-handle only the exceptional/projective lanes

Projected Intel impact:

- **1-3 %**, probably stackable with S1

### S3) Retune `reduce_plattice_asm()` for Granite Rapids specifically

Patch target:

- `sieve/las-reduce-plattice-production-asm.hpp`

Why it is still alive:

- the source comments already state that the subtractive-block count vs division
  threshold is microarchitecture-dependent
- Granite Rapids is not the microarchitecture this asm was originally tuned for

Patch idea:

- keep the current path as default
- add a Granite-Rapids-specific variant selected at build time
- sweep the number of subtractive blocks before `divl`

Projected Intel impact:

- **3-6 %** if the 14.5 % plattice slice is truly hot on your measured binary

### S4) Make the FK walk more branch-light

Patch targets:

- `sieve/las-plattice.hpp`: `plattice_enumerator::next(...)`,
  `probably_coprime(...)`
- `sieve/las-fill-in-buckets.inl`: tight `while (!ple.done(F))` loops

Why this matters:

- even if bucket writes are branchless, the FK walk itself still contains
  control flow and coprimality tests in the per-hit path

Patch idea:

- convert the tiny per-step control flow to mask/select arithmetic or cmov-heavy
  sequences
- unroll 4-8 steps and buffer accepted hits before stores

Projected Intel impact:

- **3-7 %**, especially if Topdown on GNR reports bad speculation /
  front-end pressure rather than memory stalls

## A stack that can plausibly reach the missing ~20 %

Based on the brief, the harness, and the upstream audit, the most plausible
stack is:

1. **compact self-pinning on the real 6980P topology**  
   projected **+4-8 %**
2. **SMT oversubscription on the 6980P (`-t 32` / `-t 48`)**  
   projected **+5-10 %**
3. **native-on-node AVX2 build (`-march=native -mno-avx512f`)**  
   projected **+3-6 %**
4. **Granite-Rapids-specific `reduce_plattice_asm()` retune**  
   projected **+3-6 %**
5. **general-entry / partial-batch root-transform cleanup**  
   projected **+1-4 %**

Not all of these will hit their top end simultaneously, but a realistic stacked
path to **~20 %+** exists without invoking any dead GPU/full-sieve ideas.

## Experiments to run next

The goal is to falsify cheap, high-magnitude explanations before spending time
on lower-yield GNFS sweeps.

### Experiment set A: container CPU truth

For the exact miner image:

1. print host-visible CPUs, cgroup quota, and affinity;
2. log the exact CADO client/thread counts selected at runtime;
3. rerun with the runtime forcibly capped to 24 threads even if the host
   advertises more.

Success condition: prove that runtime sizing is already correct, or catch it
being wrong.

### Experiment set B: affinity/topology on Granite Rapids

Keep everything else fixed and compare:

```bash
# default placement
./workbench/challenges/breaking_rsa/cado_perf_probe.sh <your-cado-command...>

# compact placement chosen from quota + NUMA topology
CPUSET="$(python3 workbench/challenges/breaking_rsa/select_compact_cpuset.py)" \
./workbench/challenges/breaking_rsa/cado_perf_probe.sh <your-cado-command...>

# compact placement with SMT siblings added after one thread/core
CPUSET="$(python3 workbench/challenges/breaking_rsa/select_compact_cpuset.py \
  --count 32 --strategy smt-compact)" \
./workbench/challenges/breaking_rsa/cado_perf_probe.sh <your-cado-command...>
```

Also run `lscpu -e=cpu,node,socket` and `numactl -H` on the host to choose
compact CPU lists that stay within as few SNC / LLC domains as possible.

### Experiment set C: compiler matrix on Intel only

For a fixed q-range slice and fixed polynomial:

1. GCC 15 `-O2 -march=diamondrapids -mno-avx512f`
2. GCC 15 `-O3 -march=diamondrapids -mno-avx512f`
3. GCC 15 PGO build from a representative `las` slice
4. Clang / icx builds with the same AVX2 floor
5. optional light-AVX-512 A/B with explicit vector-width control

Measure:

- rel/s
- instructions per relation
- branch-miss rate
- wall time on the same q-range

Exact build commands to start from:

```bash
# GCC 15 AVX2-first build on the Granite Rapids node
cmake -S /workspace/external/cado-nfs -B /tmp/cado-gcc15-gnr \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=gcc \
  -DCMAKE_CXX_COMPILER=g++ \
  -DCMAKE_C_FLAGS="-O3 -march=native -mno-avx512f -mprefer-vector-width=256 -falign-loops=32 -falign-functions=32" \
  -DCMAKE_CXX_FLAGS="-O3 -march=native -mno-avx512f -mprefer-vector-width=256 -falign-loops=32 -falign-functions=32"
cmake --build /tmp/cado-gcc15-gnr -j"$(nproc)"

# Clang AVX2-first build
cmake -S /workspace/external/cado-nfs -B /tmp/cado-clang-gnr \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++ \
  -DCMAKE_C_FLAGS="-O3 -march=native -mno-avx512f -mprefer-vector-width=256" \
  -DCMAKE_CXX_FLAGS="-O3 -march=native -mno-avx512f -mprefer-vector-width=256"
cmake --build /tmp/cado-clang-gnr -j"$(nproc)"

# GCC PGO build (same q-window you use for rel/s A/B)
cmake -S /workspace/external/cado-nfs -B /tmp/cado-gcc15-pgo-gen \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=gcc \
  -DCMAKE_CXX_COMPILER=g++ \
  -DCMAKE_C_FLAGS="-O3 -march=native -mno-avx512f -mprefer-vector-width=256 -fprofile-generate" \
  -DCMAKE_CXX_FLAGS="-O3 -march=native -mno-avx512f -mprefer-vector-width=256 -fprofile-generate"
cmake --build /tmp/cado-gcc15-pgo-gen -j"$(nproc)"
# run representative las slice here
cmake -S /workspace/external/cado-nfs -B /tmp/cado-gcc15-pgo-use \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=gcc \
  -DCMAKE_CXX_COMPILER=g++ \
  -DCMAKE_C_FLAGS="-O3 -march=native -mno-avx512f -mprefer-vector-width=256 -fprofile-use -fprofile-correction" \
  -DCMAKE_CXX_FLAGS="-O3 -march=native -mno-avx512f -mprefer-vector-width=256 -fprofile-use -fprofile-correction"
cmake --build /tmp/cado-gcc15-pgo-use -j"$(nproc)"
```

Perf command to pair with the same q-window:

```bash
perf stat -r 3 \
  -M TopdownL1 -M TopdownL2 \
  -e cycles,instructions,branches,branch-misses,cache-misses \
  -- <your-las-or-cado-command>
```

### Experiment set D: GNFS work-balance only after A-C

Only if A-C do not close the gap:

1. relation floor sweep (`rels_wanted`)
2. `target_density` sweep with GPU LA enabled
3. `adjust_strategy` A/B
4. batch cofactorization / ECM split that is already verified on Blackwell
5. fresh polynomial validation by real `las` rel/q, not Murphy-E alone

## Suggested decision order

1. **Reproduce the source-level contradictions on the shipped binary**
   - is bucket push really branchless or not?
   - is the hot `invmod_redc_32` share coming from already-batched or
     still-unbatched paths?
2. **Affinity / compact placement on the real Granite Rapids topology**
3. **SMT sweep on Granite Rapids**
4. **Compiler matrix with native AVX2 build on-node**
5. **Granite-Rapids-specific `reduce_plattice_asm()` retune**
6. **Root-transform batching cleanup for general / partial-fallback cases**
7. **Relation-floor / LA trade-off only after 1-6**
8. **GPU ideas only with hard rel/s evidence**

If step 1 or 2 produces a double-digit Intel gain, it dominates everything
else and should be fixed in the miner wrapper before more exotic work.
