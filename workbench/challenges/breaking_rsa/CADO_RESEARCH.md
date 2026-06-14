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

# compact placement on an explicit CPU list
CPUSET=0-23 \
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

### Experiment set D: GNFS work-balance only after A-C

Only if A-C do not close the gap:

1. relation floor sweep (`rels_wanted`)
2. `target_density` sweep with GPU LA enabled
3. `adjust_strategy` A/B
4. batch cofactorization / ECM split that is already verified on Blackwell
5. fresh polynomial validation by real `las` rel/q, not Murphy-E alone

## Suggested decision order

1. **Container CPU count / thread sizing**
2. **Affinity / compact placement on Granite Rapids**
3. **Compiler matrix with PGO**
4. **Relation-floor / LA trade-off**
5. **GPU cofactorization**
6. **GPU full sieve only with hard evidence**

If step 1 or 2 produces a double-digit Intel gain, it dominates everything
else and should be fixed in the miner wrapper before more exotic work.
