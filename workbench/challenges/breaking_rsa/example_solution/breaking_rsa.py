#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2026 qBitTensor Labs
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

"""
Breaking RSA solver — multi-stage factoring pipeline.

Strategy:
  1. Small-factor sweep (trial division + Pollard's rho)
  2. Algebraic attacks (Pollard's p-1, Williams' p+1)
  3. ECM for medium-sized factors
  4. msieve SIQS for the general case

Each stage is progressively more expensive. We bail early if any
stage finds a factor, and fall through to SIQS as the last resort.

Prerequisites:
  - msieve binary (build from https://github.com/radii/msieve)
  - GMP-ECM binary (build from https://gitlab.inria.fr/zimmerma/ecm)
  - gmpy2 (pip install gmpy2)

Set MSIEVE_BIN and ECM_BIN env vars to override binary locations.

Output contract (stdout-only, no shared filesystem with the validator):

  1. Text logs are written to stdout as usual.
  2. After all logs, a magic separator line is written.
  3. After the separator, a base64-encoded zip of result.json and
     solve_info.json is written to stdout.

The validator captures stdout via ``docker logs``, splits at the separator,
and extracts the base64 zip into the solution_artifacts directory.

Usage: python breaking_rsa.py <challenge_id> <JSON-encoded Problem>
"""

from datetime import datetime, timezone
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import *

import gmpy2
from gmpy2 import mpz, gcd, powmod, isqrt

from enigma_challenges.breaking_rsa import Problem, Solution
from enigma_challenges.solution_output import build_solution_zip, write_solution_output


def _printlog(msg: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)


def _read_text_if_exists(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _effective_cpu_count() -> int:
    """
    Estimate usable CPU parallelism inside containers.

    The validator applies ``docker run --cpus 24`` which is a CFS quota, not a
    cpuset. ``os.cpu_count()`` may therefore report the full host topology
    rather than the miner's actual budget. We take the minimum of:
      - scheduler affinity (cpuset-aware when present)
      - cgroup v2 CPU quota
      - cgroup v1 CPU quota
      - host-visible CPU count (fallback)
    """
    counts: list[int] = []

    if hasattr(os, "sched_getaffinity"):
        try:
            affinity_count = len(os.sched_getaffinity(0))
        except OSError:
            affinity_count = 0
        if affinity_count > 0:
            counts.append(affinity_count)

    cpu_max = _read_text_if_exists("/sys/fs/cgroup/cpu.max")
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) >= 2 and parts[0] != "max":
            try:
                quota = int(parts[0])
                period = int(parts[1])
            except ValueError:
                quota = 0
                period = 0
            if quota > 0 and period > 0:
                counts.append(max(1, math.ceil(quota / period)))

    quota_text = _read_text_if_exists("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period_text = _read_text_if_exists("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota_text and period_text:
        try:
            quota = int(quota_text)
            period = int(period_text)
        except ValueError:
            quota = 0
            period = 0
        if quota > 0 and period > 0:
            counts.append(max(1, math.ceil(quota / period)))

    host_count = os.cpu_count() or 1
    counts.append(host_count)
    return max(1, min(counts))


# ---------------------------------------------------------------------------
# Stage 1a: Trial division
# ---------------------------------------------------------------------------

SMALL_PRIMES = []


def _init_small_primes(bound: int = 1_000_000) -> list[int]:
    """Sieve of Eratosthenes up to bound."""
    global SMALL_PRIMES
    if SMALL_PRIMES:
        return SMALL_PRIMES
    sieve = bytearray(b'\x01') * (bound + 1)
    sieve[0] = sieve[1] = 0
    for i in range(2, int(bound**0.5) + 1):
        if sieve[i]:
            sieve[i*i::i] = bytearray(len(sieve[i*i::i]))
    SMALL_PRIMES = [i for i, v in enumerate(sieve) if v]
    return SMALL_PRIMES


def trial_division(n: int, bound: int = 1_000_000) -> Optional[int]:
    """Try dividing by all primes up to bound."""
    primes = _init_small_primes(bound)
    for p in primes:
        if p * p > n:
            break
        if n % p == 0:
            return p
    return None


# ---------------------------------------------------------------------------
# Stage 1b: Pollard's rho (Brent's improvement)
# ---------------------------------------------------------------------------

def pollard_rho_brent(n: int, max_iterations: int = 1_000_000) -> Optional[int]:
    """
    Brent's improvement to Pollard's rho.

    Uses cycle detection with periodic GCD accumulation for speed.
    The random polynomial is x^2 + c (mod n) with varying c on restart.
    """
    if n % 2 == 0:
        return 2
    n = mpz(n)

    for c in range(1, 20):
        y, r, q = mpz(1), mpz(1), mpz(1)
        d = mpz(1)
        c_mpz = mpz(c)

        x = y
        iterations = 0
        while d == 1 and iterations < max_iterations:
            x = y
            for _ in range(int(r)):
                y = (y * y + c_mpz) % n

            k = mpz(0)
            while k < r and d == 1:
                ys = y
                batch = min(128, int(r - k))
                for _ in range(batch):
                    y = (y * y + c_mpz) % n
                    q = (q * abs(x - y)) % n
                d = gcd(q, n)
                k += batch
                iterations += batch

            r *= 2

        if d == n:
            # Backtrack
            while True:
                ys = (ys * ys + c_mpz) % n
                d = gcd(abs(x - ys), n)
                if d > 1:
                    break

        if 1 < d < n:
            return int(d)

    return None


# ---------------------------------------------------------------------------
# Stage 2a: Pollard's p-1 (two-stage)
# ---------------------------------------------------------------------------

def pollard_pm1(n: int, B1: int = 1_000_000, B2: int = 100_000_000) -> Optional[int]:
    """
    Pollard's p-1: finds p if (p-1) is B1-powersmooth (stage 1)
    or has at most one factor between B1 and B2 (stage 2).
    """
    n = mpz(n)
    a = mpz(2)

    # Stage 1: multiply a by all prime powers up to B1
    p = mpz(2)
    while p <= B1:
        pp = int(p)
        while pp * int(p) <= B1:
            pp *= int(p)
        a = powmod(a, pp, n)
        p = gmpy2.next_prime(p)

    g = gcd(a - 1, n)
    if 1 < g < n:
        return int(g)
    if g == n:
        return None

    # Stage 2: check individual primes between B1 and B2
    # Use baby-step giant-step style accumulation
    product = mpz(1)
    count = 0
    p = gmpy2.next_prime(mpz(B1))
    while p <= B2:
        a_p = powmod(a, int(p), n)
        product = (product * (a_p - 1)) % n
        count += 1
        if count % 2000 == 0:
            g = gcd(product, n)
            if 1 < g < n:
                return int(g)
            if g == n:
                return None
        p = gmpy2.next_prime(p)

    g = gcd(product, n)
    if 1 < g < n:
        return int(g)
    return None


# ---------------------------------------------------------------------------
# Stage 2b: Williams' p+1
# ---------------------------------------------------------------------------

def williams_pp1(n: int, B1: int = 1_000_000) -> Optional[int]:
    """
    Williams' p+1: finds p if (p+1) is B1-smooth.

    Uses Lucas sequences. Tries multiple seeds since the method
    only works if the Jacobi symbol condition is met.
    """
    n = mpz(n)
    for seed in [3, 5, 7, 11, 13, 17, 19, 23, 29, 31]:
        v = mpz(seed)
        p = mpz(2)
        while p <= B1:
            pp = int(p)
            while pp * int(p) <= B1:
                pp *= int(p)
            # Compute V_{pp}(v) mod n using the Lucas chain
            v = _lucas_chain(v, pp, n)
            p = gmpy2.next_prime(p)

        g = gcd(v - 2, n)
        if 1 < g < n:
            return int(g)

    return None


def _lucas_chain(v: mpz, k: int, n: mpz) -> mpz:
    """Compute V_k(v, 1) mod n using the binary Lucas chain."""
    if k == 0:
        return mpz(2)
    if k == 1:
        return v

    v_prev = mpz(2)  # V_0
    v_curr = v        # V_1

    for bit in bin(k)[3:]:  # skip '0b1', iterate remaining bits
        if bit == '1':
            v_prev = (v_curr * v_prev - v) % n
            v_curr = (v_curr * v_curr - 2) % n
        else:
            v_curr = (v_curr * v_prev - v) % n
            v_prev = (v_prev * v_prev - 2) % n

    return v_curr


# ---------------------------------------------------------------------------
# Stage 3: GMP-ECM (parallel workers)
# ---------------------------------------------------------------------------

def _find_ecm() -> Optional[str]:
    """Find the GMP-ECM binary."""
    env_path = os.environ.get("ECM_BIN")
    if env_path and os.path.isfile(env_path):
        return env_path
    for loc in ["/usr/local/bin/ecm", os.path.expanduser("~/gmp-ecm/ecm")]:
        if os.path.isfile(loc):
            return loc
    return None


def _ecm_worker(ecm_bin: str, n_str: str, B1: str, curves: int,
                result_queue: mp.Queue, stop_event: mp.Event) -> None:
    """Run GMP-ECM with given parameters."""
    if stop_event.is_set():
        return
    try:
        proc = subprocess.run(
            [ecm_bin, "-c", str(curves), B1],
            input=n_str + "\n",
            capture_output=True,
            text=True,
            timeout=3600,
        )
        output = proc.stdout + "\n" + proc.stderr
        for line in output.splitlines():
            if "Found" in line and "factor" in line.lower():
                parts = line.split(":")
                if len(parts) >= 2:
                    try:
                        factor = int(parts[-1].strip())
                        if factor > 1:
                            n = int(n_str)
                            result_queue.put(("ecm", factor, n // factor))
                            stop_event.set()
                            return
                    except ValueError:
                        pass
    except Exception:
        pass


def run_ecm_parallel(n: int, num_workers: int, log=None) -> Optional[tuple[int, int]]:
    """Run ECM with escalating B1 bounds across parallel workers."""
    ecm_bin = _find_ecm()
    if not ecm_bin:
        if log:
            log("ECM binary not found, skipping")
        return None

    result_queue = mp.Queue()
    stop_event = mp.Event()
    all_workers = []
    n_str = str(n)

    # Escalating B1 bounds with curves tuned per level
    ecm_stages = [
        ("50000",    100),   # t25 — fast sweep
        ("250000",   200),   # t30
        ("1000000",  300),   # t35
        ("3000000",  400),   # t40
        ("11000000", 500),   # t45
        ("43000000", 300),   # t50
    ]

    for B1, total_curves in ecm_stages:
        if stop_event.is_set():
            break
        curves_per_worker = max(1, total_curves // num_workers)
        if log:
            log(f"  ECM B1={B1}: {num_workers} workers x {curves_per_worker} curves")
        for _ in range(num_workers):
            p = mp.Process(
                target=_ecm_worker,
                args=(ecm_bin, n_str, B1, curves_per_worker,
                      result_queue, stop_event),
                daemon=True,
            )
            p.start()
            all_workers.append(p)

    # Wait for result or all workers to finish
    while True:
        try:
            result = result_queue.get(timeout=5)
            if result is not None:
                for w in all_workers:
                    if w.is_alive():
                        w.kill()
                _, p, q = result
                return (int(p), int(q))
        except Exception:
            if all(not w.is_alive() for w in all_workers):
                break

    return None


# ---------------------------------------------------------------------------
# Stage 4: msieve SIQS
# ---------------------------------------------------------------------------

def _find_msieve() -> Optional[str]:
    """Find the msieve binary."""
    env_path = os.environ.get("MSIEVE_BIN")
    if env_path and os.path.isfile(env_path):
        return env_path
    path = shutil.which("msieve")
    if path:
        return path
    for loc in ["/usr/local/bin/msieve",
                os.path.expanduser("~/msieve/msieve")]:
        if os.path.isfile(loc):
            return loc
    return None


def run_msieve(n: int, log=None) -> Optional[tuple[int, int]]:
    """
    Run msieve's SIQS implementation.

    msieve automatically selects SIQS for numbers in the right range.
    For very large numbers it would use GNFS, but that requires
    additional polynomial selection setup.
    """
    msieve_bin = _find_msieve()
    if not msieve_bin:
        if log:
            log("msieve binary not found, skipping")
        return None

    import tempfile
    work_dir = tempfile.mkdtemp(prefix="msieve-")

    try:
        proc = subprocess.run(
            [msieve_bin, "-q", "-v", str(n)],
            capture_output=True,
            text=True,
            timeout=None,  # let Docker wall time handle this
            cwd=work_dir,
        )

        output = proc.stdout + "\n" + proc.stderr
        if log:
            # Log key msieve output lines
            for line in output.splitlines():
                if any(kw in line.lower() for kw in
                       ["factor", "elapsed", "error", "siqs", "gnfs",
                        "matrix", "sieving", "relations"]):
                    log(f"  msieve: {line.strip()}")

        # Parse factors: msieve outputs "prpNN: DIGITS" or "pNN: DIGITS"
        factors = []
        for line in output.splitlines():
            m = re.match(r'(?:prp|p)\d+:\s+(\d+)', line.strip())
            if m:
                factors.append(int(m.group(1)))

        if len(factors) >= 2:
            return (factors[0], factors[1])
        elif len(factors) == 1:
            q = n // factors[0]
            if factors[0] * q == n:
                return (factors[0], q)

    except subprocess.TimeoutExpired:
        if log:
            log("msieve timed out")
    except Exception as e:
        if log:
            log(f"msieve error: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return None


# ---------------------------------------------------------------------------
# Fermat's factorization (near-sqrt attack)
# ---------------------------------------------------------------------------

def fermat_factor(n: int, max_iterations: int = 100_000) -> Optional[int]:
    """
    Fermat's factorization: works when p and q are close together.

    Tries to express n = a^2 - b^2 = (a+b)(a-b) by searching for
    a starting from ceil(sqrt(n)).
    """
    n = mpz(n)
    a = isqrt(n)
    if a * a < n:
        a += 1

    for _ in range(max_iterations):
        b2 = a * a - n
        b = isqrt(b2)
        if b * b == b2:
            p = int(a + b)
            q = int(a - b)
            if q > 1:
                return q
        a += 1

    return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def factor_semiprime(n: int, num_bits: int, log=None) -> tuple[Optional[int], Optional[int], str]:
    """
    Multi-stage factoring pipeline.
    Returns (p, q, method_used) or (None, None, "failed").
    """
    n_digits = len(str(n))
    host_cpus = os.cpu_count() or 1
    num_cpus = _effective_cpu_count()

    if log:
        log(f"Factoring {num_bits}-bit ({n_digits}-digit) semiprime")
        if num_cpus != host_cpus:
            log(f"Available CPUs: effective={num_cpus}, host_visible={host_cpus}")
        else:
            log(f"Available CPUs: {num_cpus}")

    # Stage 1a: Trial division
    if log:
        log("Stage 1a: Trial division (primes < 1M)...")
    t0 = time.time()
    f = trial_division(n, 1_000_000)
    if f is not None:
        if log:
            log(f"Found factor via trial division in {time.time()-t0:.2f}s")
        return (f, n // f, "trial_division")

    # Stage 1b: Pollard's rho (Brent)
    if log:
        log("Stage 1b: Pollard's rho (Brent's variant, 1M iterations)...")
    t0 = time.time()
    f = pollard_rho_brent(n, max_iterations=1_000_000)
    if f is not None:
        if log:
            log(f"Found factor via Pollard's rho in {time.time()-t0:.2f}s")
        return (f, n // f, "pollard_rho")

    # Stage 1c: Fermat's method (quick check for close primes)
    if log:
        log("Stage 1c: Fermat's factorization (100K iterations)...")
    t0 = time.time()
    f = fermat_factor(n, max_iterations=100_000)
    if f is not None:
        if log:
            log(f"Found factor via Fermat in {time.time()-t0:.2f}s")
        return (f, n // f, "fermat")

    # Stage 2a: Pollard's p-1
    if log:
        log("Stage 2a: Pollard's p-1 (B1=1M, B2=100M)...")
    t0 = time.time()
    f = pollard_pm1(n, B1=1_000_000, B2=100_000_000)
    if f is not None:
        if log:
            log(f"Found factor via Pollard's p-1 in {time.time()-t0:.2f}s")
        return (int(f), int(n // f), "pollard_pm1")

    # Stage 2b: Williams' p+1
    if log:
        log("Stage 2b: Williams' p+1 (B1=1M, 10 seeds)...")
    t0 = time.time()
    f = williams_pp1(n, B1=1_000_000)
    if f is not None:
        if log:
            log(f"Found factor via Williams' p+1 in {time.time()-t0:.2f}s")
        return (int(f), int(n // f), "williams_pp1")

    # Stage 3: ECM (parallel)
    if log:
        log(f"Stage 3: ECM (escalating B1, {num_cpus} workers)...")
    t0 = time.time()
    ecm_result = run_ecm_parallel(n, num_workers=max(1, num_cpus - 1), log=log)
    if ecm_result is not None:
        p, q = ecm_result
        if log:
            log(f"Found factors via ECM in {time.time()-t0:.2f}s")
        return (p, q, "ecm")

    # Stage 4: msieve SIQS (general-purpose, last resort)
    if log:
        log("Stage 4: msieve SIQS (general case)...")
    t0 = time.time()
    msieve_result = run_msieve(n, log=log)
    if msieve_result is not None:
        p, q = msieve_result
        if log:
            log(f"Found factors via msieve SIQS in {time.time()-t0:.2f}s")
        return (p, q, "msieve_siqs")

    if log:
        log("All stages exhausted — factoring failed")
    return (None, None, "failed")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) != 3:
        print("Missing args: <challenge_id> <JSON-encoded breaking_rsa.Problem>")
        sys.exit(1)
    challenge_id = sys.argv[1].strip()
    try:
        problem = Problem.from_json(sys.argv[2].strip())
    except Exception as err:
        print(f"Error parsing input problem:\n{err}")
        sys.exit(1)
    if problem.num < 6:
        print("Error: number must be positive non-trivial semi-prime")
        sys.exit(1)

    timestamp_start = datetime.now(timezone.utc).isoformat()
    start_time = time.time()

    _printlog(f"Starting Breaking RSA challenge: {challenge_id}")
    _printlog(f"Number size: {problem.num_bits} bits")
    numstr = str(problem.num)
    _printlog(f"N = {numstr[:40]}{'...' if len(numstr) > 40 else ''}")

    p, q, method = factor_semiprime(problem.num, problem.num_bits, log=_printlog)

    solve_time = time.time() - start_time

    if p is not None and q is not None:
        _printlog(f"SUCCESS via {method} in {solve_time:.2f}s")
        solution = Solution("success", p, q)
    else:
        _printlog(f"FAILED after {solve_time:.2f}s")
        solution = Solution("failed", None, None)

    result_json = json.dumps(solution.to_dict(), indent=2)
    solve_info_json = json.dumps({
        "solution_status": solution.status,
        "challenge_id": challenge_id,
        "timestamp_utc": timestamp_start,
        "solve_time_seconds": solve_time,
        "method": method,
        "num_bits": problem.num_bits,
    })

    # Write to OUTPUT_DIR if set (direct/dev mode backward compat)
    output_dir = os.environ.get("OUTPUT_DIR")
    if output_dir:
        try:
            Path(output_dir).mkdir(exist_ok=True)
            Path(output_dir, "result.json").write_text(result_json)
            Path(output_dir, "solve_info.json").write_text(solve_info_json)
        except OSError:
            pass

    # Emit solution via stdout protocol (docker/validator mode)
    zip_bytes = build_solution_zip({
        "result.json": result_json,
        "solve_info.json": solve_info_json,
    })
    write_solution_output(zip_bytes)

    exit_code = 0 if solution.status == "success" else 1
    os._exit(exit_code)


if __name__ == "__main__":
    main()
