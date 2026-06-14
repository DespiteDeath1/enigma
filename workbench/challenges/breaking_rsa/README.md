# Breaking RSA Challenge

## What is it

Given a semiprime N (the product of two prime numbers), find the prime factors
p and q such that p * q = N.

## Why it matters

Semiprime factorization is the hard mathematical problem underlying RSA
encryption. The security of RSA relies on the assumption that factoring large
semiprimes is computationally infeasible. Breaking larger semiprimes
demonstrates increasing capability against real-world cryptographic systems,
and is a key benchmark for both classical and quantum approaches.

## Status

**Live** — For current prize pools and milestones, visit the [Enigma page](https://www.qbittensorlabs.com/enigma).

## Runtime constraints

Solutions are executed by validators under these conditions:

- **Wall time:** 4 hours (14,400 seconds)
- **Network:** None (`--network none`, no internet access)
- **Platform:** `linux/amd64`
- **Compute:** NVIDIA RTX PRO 6000 (96 GB VRAM), 24 vCPU, 85 GB RAM
- **CPU limit semantics:** validators currently enforce the CPU budget with `docker run --cpus 24` (CFS quota). This is *not* the same as `--cpuset-cpus`: inside the container, `os.cpu_count()` may still report the host's full CPU count and threads may migrate across the host topology unless your solver sets affinity itself.
- **Filesystem:** Root filesystem is read-only. Use `/tmp` (tmpfs, currently sized to `VALIDATOR_DOCKER_TMPFS_DEFAULT` with noexec/nosuid) for any scratch space, temp files, or working directories needed at runtime.
- **User:** Runs as a non-root user (default `miner`). Your Dockerfile should create this user and ensure your solver and any required binaries are accessible to it (see the example_solution/Dockerfile).
- **Output contract:** In Docker/validator mode there is **no** writable `/output` volume mounted. Your solver must emit results exclusively via the stdout protocol (text logs, then the `SOLUTION_OUTPUT_SEPARATOR` line, then a base64-encoded zip of `result.json` + `solve_info.json` etc.). See `enigma_challenges/solution_output.py` (or the vendored copy at build time). The `OUTPUT_DIR` environment variable is only provided in direct (non-Docker) mode for local development convenience.

### CPU-budget guidance for solver authors

If your miner is CPU-bound, size worker pools from cgroup quota and/or process affinity rather than trusting `os.cpu_count()` alone. On Linux containers the most reliable order is:

1. cgroup v2 quota: `/sys/fs/cgroup/cpu.max`
2. cgroup v1 quota: `/sys/fs/cgroup/cpu/cpu.cfs_quota_us` and `cpu.cfs_period_us`
3. `os.sched_getaffinity(0)` / `taskset` / `sched_setaffinity`
4. `os.cpu_count()` only as a bare-metal fallback

For topology-sensitive workloads such as GNFS sieving, consider explicitly pinning long-running worker threads or subprocesses to a compact CPU set so the kernel does not spread them across distant NUMA / LLC domains.

## Challenge parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `difficulty` | Controls the bit-width of the semiprime to factor | 1 |

## Input

Your solver receives two positional CLI arguments:

1. `<challenge_id>` -- a UUID identifying the challenge instance
2. `<problem_json>` -- a JSON string with the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `difficulty` | int | Difficulty label |
| `num` | int | The semiprime to factor |
| `num_bits` | int | Bit-width of the semiprime |

## Output

**Important (Docker/validator mode):** There is no writable `/output` volume inside the container. The root filesystem is read-only. Your solver must deliver artifacts exclusively by writing to **stdout** using the defined protocol (human-readable logs, followed by the `SOLUTION_OUTPUT_SEPARATOR` line on its own, followed by a base64-encoded zip containing `result.json`, `solve_info.json`, etc.). The validator captures this via `docker logs` after the container exits.

The `OUTPUT_DIR` environment variable (and writing files directly to disk) is only provided in `--mode direct` (non-Docker subprocess) for local development convenience inside the workbench.

### `result.json` (must be inside the emitted zip)

```json
{
    "status": "success",
    "p": 7,
    "q": 11
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | str | Final solution status (e.g., `"success"` or `"failure"`) |
| `p` | int or null | First prime factor |
| `q` | int or null | Second prime factor |

### Other artifacts

Include `solve_info.json` (and any other files your challenge expects) inside the same base64 zip emitted after the separator.

`stdout.log` is captured automatically from the text logs you print before the separator (plus stderr).

See the example solver and `enigma_challenges/solution_output.py` (or the comment block in the mock example Dockerfile) for the exact protocol.

## Example

For a small semiprime N = 77:

- Input: `{"difficulty": 1, "num": 77, "num_bits": 7}`
- Expected output: `{"status": "success", "p": 7, "q": 11}`

## Example solution

A working reference implementation is provided in `example_solution/`. It uses
a multi-stage pipeline (trial division, Pollard's rho, algebraic attacks, ECM,
msieve SIQS) and includes a `Dockerfile` and `breaking_rsa.py` solver script.

## Testing

```bash
python -m workbench test breaking-rsa --solution ./my_solver/
```

Use `--mode direct` to skip Docker and run your solver script directly.
Use `--seed <int>` for reproducible challenge generation.
