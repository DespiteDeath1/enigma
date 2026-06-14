#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2026 qBitTensor Labs
#
# CADO-NFS + msieve GPU block-Lanczos — Intel Granite Rapids optimised
#
# HOT-KERNEL PROFILE (from perf record on production params, single thread):
#   fill_in_buckets       28 %  — FK-walk scatter (serial loop, branchy)
#   sieve_small_region    22 %  — line sieve walk
#   plattice_info         14.5% — Franke-Kleinjung lattice reduction (GCD loop)
#   invmod_redc_32        11.9% — modular inverse (serial dependency chain)
#
# THE THREE LEVERS IMPLEMENTED HERE:
#
# 1. VENDOR-AWARE NATIVE BUILD (Dockerfile)
#    Intel: -march=native -mno-avx512f -mtune=sapphirerapids
#    AMD:   -march=native (AVX-512 + znver scheduling: +17.5%)
#    Fixes: current build uses -mtune=icelake-server (wrong for Redwood Cove).
#
# 2. SMT EXPLOITATION — THE HIGHEST-PRIORITY UNTESTED LEVER
#    GNR Xeon 6980P: 2-way SMT per Redwood Cove P-core.
#    invmod_redc_32 and reduce_plattice are both SERIAL DEPENDENCY CHAINS.
#    A second SMT thread fills the back-end while chain A stalls on GCD steps.
#    Implementation: with --cpus 24 CFS quota, run 48 single-threaded `las`
#    jobs (las.threads=1). CFS allows >quota threads when the physical cores
#    have idle cycles from dependency stalls; SMT threads fill those cycles.
#    Projected: 10-25% improvement on GNR for dependency-chain-bound kernel.
#
# 3. COMPACT SNC PINNING
#    --cpus 24 = CFS quota, NOT --cpuset-cpus. os.cpu_count() returns ~128.
#    Scheduler can migrate threads across SNC/LLC/NUMA domains on GNR-AP.
#    We sched_setaffinity to NUMA node 0 (compact set on one die).
#    Reduces cross-SNC coherency traffic for any shared data structures.
#    Projected: 2-5%.

from __future__ import annotations

import glob
import gzip
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import gmpy2
from gmpy2 import mpz, gcd

from enigma_challenges.breaking_rsa import Problem, Solution
from enigma_challenges.solution_output import build_solution_zip, write_solution_output

# ─────────────────────────────────────────────────────────────────────────────
# Globals
# ─────────────────────────────────────────────────────────────────────────────

_START = time.time()
WALL_LIMIT    = 14_400   # 4 h
SAFETY_MARGIN = 180      # stop 3 min before hard deadline
DEADLINE      = _START + WALL_LIMIT - SAFETY_MARGIN

MEMFS_ROOT = os.environ.get("MEMFS_ROOT", "/cado-work")
WORK_DIR   = MEMFS_ROOT

MSIEVE_GPU = os.environ.get("MSIEVE_GPU", "/usr/local/bin/msieve-gpu")
MSIEVE_CPU = os.environ.get("MSIEVE_CPU", "/usr/local/bin/msieve-cpu")
ECM_BIN    = os.environ.get("ECM_BIN",    "/usr/local/bin/ecm")
CADO_DIR   = os.environ.get("CADO_DIR",   "/cado-nfs")

# ─────────────────────────────────────────────────────────────────────────────
# CPU topology detection — THE most important runtime decision
# ─────────────────────────────────────────────────────────────────────────────

def _read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def get_cfs_cpu_quota() -> int:
    """
    Read the true CPU quota from cgroup (what --cpus N sets).
    os.cpu_count() returns the HOST core count (~128 on 6980P) which is wrong.
    """
    # cgroup v2
    max_file = _read_file("/sys/fs/cgroup/cpu.max")
    if max_file and max_file != "max 100000":
        parts = max_file.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                return max(1, round(int(parts[0]) / int(parts[1])))
            except ValueError:
                pass

    # cgroup v1
    quota  = _read_file("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_file("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period and quota != "-1":
        try:
            return max(1, round(int(quota) / int(period)))
        except ValueError:
            pass

    return os.cpu_count() or 24


def detect_cpu() -> dict:
    """Detect CPU vendor, model, SMT status, and NUMA topology."""
    info = {
        "vendor":    "Unknown",
        "model":     "Unknown",
        "is_intel":  False,
        "is_amd":    False,
        "smt_on":    False,
        "threads_per_core": 1,
        "quota_cpus": get_cfs_cpu_quota(),
        "numa_nodes": 1,
        "numa0_cpus": [],
    }

    cpuinfo = _read_file("/proc/cpuinfo")
    if "GenuineIntel" in cpuinfo:
        info["vendor"] = "Intel"
        info["is_intel"] = True
    elif "AuthenticAMD" in cpuinfo:
        info["vendor"] = "AMD"
        info["is_amd"] = True

    # Model name
    for line in cpuinfo.splitlines():
        if "model name" in line:
            info["model"] = line.split(":", 1)[-1].strip()
            break

    # SMT status
    smt = _read_file("/sys/devices/system/cpu/smt/active")
    info["smt_on"] = (smt == "1")

    # Threads per core
    try:
        for cpu_dir in glob.glob("/sys/devices/system/cpu/cpu0"):
            tpc = _read_file(f"{cpu_dir}/topology/thread_siblings_list")
            if tpc:
                siblings = _parse_cpulist(tpc)
                info["threads_per_core"] = len(siblings)
    except Exception:
        pass

    # NUMA topology
    numa_dirs = sorted(glob.glob("/sys/devices/system/node/node*"))
    info["numa_nodes"] = len(numa_dirs)
    for nd in numa_dirs:
        cpulist = _read_file(f"{nd}/cpulist")
        if cpulist:
            info["numa0_cpus"] = _parse_cpulist(cpulist)
            break  # just node 0

    return info


def _parse_cpulist(s: str) -> list[int]:
    """Parse Linux cpulist like '0-5,10-15,20' into sorted list of ints."""
    cpus = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            cpus.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            cpus.append(int(part))
    return sorted(set(cpus))


def pin_to_compact_cpus(cpu_info: dict) -> bool:
    """
    Pin this process to a compact set of CPUs within one NUMA domain.

    Rationale: --cpus 24 is a CFS *quota*, not --cpuset-cpus. The scheduler
    is free to migrate worker threads across all ~128 CPUs and across SNC/LLC
    sub-NUMA domains on GNR-AP (dual die). Pinning keeps threads on one die,
    reducing cross-SNC coherency traffic.
    """
    quota = cpu_info["quota_cpus"]
    numa0 = cpu_info["numa0_cpus"]

    # Try to stay within NUMA node 0 using only `quota` CPUs
    if len(numa0) >= quota:
        target_cpus = set(numa0[:quota])
    elif numa0:
        # NUMA node 0 smaller than quota; take all of node 0 plus extras
        target_cpus = set(numa0)
        extra_needed = quota - len(numa0)
        # Fill from other CPUs not in numa0
        all_cpus = list(range(os.cpu_count() or 128))
        extras = [c for c in all_cpus if c not in target_cpus]
        target_cpus.update(extras[:extra_needed])
    else:
        target_cpus = set(range(quota))

    try:
        os.sched_setaffinity(0, target_cpus)
        return True
    except (PermissionError, OSError):
        # May lack CAP_SYS_NICE; try without NUMA awareness
        try:
            os.sched_setaffinity(0, set(range(quota)))
            return True
        except Exception:
            return False


def get_optimal_job_count(cpu_info: dict) -> int:
    """
    Compute the optimal number of concurrent single-threaded `las` jobs.

    KEY INSIGHT — SMT EXPLOITATION:
    The hot kernels in las (invmod_redc_32 11.9%, reduce_plattice 14.5%)
    are SERIAL DEPENDENCY CHAINS. On GNR with 2-way SMT, the CPU can issue
    from a second thread while thread A stalls on the modular-inverse chain.

    With a 24-CPU CFS quota and 2-way SMT, running 48 single-threaded jobs
    means:
    - 48 jobs / 24 physical cores = 2 threads per core (perfect SMT)
    - When modinv chain A stalls, modinv chain B fills the back-end ports
    - The CFS quota (24 CPU-seconds/second) still applies, but SMT allows
      that CPU time to be MORE productive per cycle

    Reference: §6.B of the brief — "untested, high-potential for this kernel"
    """
    quota = cpu_info["quota_cpus"]
    if cpu_info["is_intel"] and cpu_info["smt_on"]:
        # 2-way SMT: 2× jobs to fill both SMT threads per physical core
        smt_factor = 2
        log(f"  Intel GNR + SMT active → using {quota * smt_factor} las jobs "
            f"(SMT exploitation for dependency-chain-bound modinv/lattice kernels)")
        return quota * smt_factor
    elif cpu_info["is_intel"]:
        # SMT status unclear; try 1.5× as a safe middle ground
        # Even without confirmed SMT, GNR has 2 threads/core per spec
        n = max(quota, int(quota * 1.5))
        log(f"  Intel GNR (SMT status unclear) → using {n} las jobs")
        return n
    else:
        # AMD: 1× quota (AMD with AVX-512 native build is already fast)
        return quota


# ─────────────────────────────────────────────────────────────────────────────
# Sieve parameters
# ─────────────────────────────────────────────────────────────────────────────

# Proven AMD baseline (EPYC 9555, ~215 min):
SIEVE_LIM0     = 11_000_000
SIEVE_LIM1     = 14_000_000
SIEVE_LPB      = 30
SIEVE_MFB      = 60
SIEVE_NCURVES0 = 17
SIEVE_NCURVES1 = 29
SIEVE_I        = 13
Q_START        = SIEVE_LIM1

# Relation targets:
# Floor ~58-60M for this factor-base size. 65M = 8% headroom.
# Reduced from prior 71M: saves ~10% sieve time (arch-neutral benefit).
RELS_WANTED    = 65_000_000
TARGET_DENSITY = 125

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    elapsed = time.time() - _START
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{elapsed:7.1f}s] {msg}", flush=True)


def time_left() -> float:
    return max(0.0, DEADLINE - time.time())


# ─────────────────────────────────────────────────────────────────────────────
# Binary discovery
# ─────────────────────────────────────────────────────────────────────────────

def _find_bin(*candidates: str) -> Optional[str]:
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
        if c:
            found = shutil.which(os.path.basename(c))
            if found:
                return found
    return None


def find_cado_bin(name: str) -> Optional[str]:
    candidates = [
        f"/usr/local/bin/{name}",
        os.path.join(CADO_DIR, "_build", name),
        os.path.join(CADO_DIR, name),
        shutil.which(name) or "",
    ]
    for base in [CADO_DIR, "/usr/local"]:
        if os.path.isdir(base):
            for root, _dirs, files in os.walk(base):
                if name in files:
                    p = os.path.join(root, name)
                    if os.access(p, os.X_OK):
                        candidates.append(p)
    return _find_bin(*candidates)


def gpu_available() -> bool:
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=10)
        ok = r.returncode == 0
        if ok:
            log(f"  GPU: {r.stdout.decode().strip()[:80]}")
        return ok
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Work directory (RAM-backed via LD_PRELOAD shim)
# ─────────────────────────────────────────────────────────────────────────────

def setup_workdir() -> None:
    global WORK_DIR
    try:
        os.makedirs(WORK_DIR, exist_ok=True)
        test = os.path.join(WORK_DIR, ".rw_test")
        with open(test, "w") as f:
            f.write("ok")
        os.unlink(test)
        log(f"Work dir: {WORK_DIR} (RAM-backed via memfs.so)")
    except OSError as e:
        log(f"WARNING: {WORK_DIR} not writable ({e}); falling back to /tmp")
        WORK_DIR = tempfile.mkdtemp(prefix="cado-")


# ─────────────────────────────────────────────────────────────────────────────
# Stage 0: Fast pre-checks
# ─────────────────────────────────────────────────────────────────────────────

def _sieve_primes(bound: int) -> list[int]:
    s = bytearray(b"\x01") * (bound + 1)
    s[0] = s[1] = 0
    for i in range(2, int(bound**0.5) + 1):
        if s[i]:
            s[i*i::i] = bytearray(len(s[i*i::i]))
    return [i for i, v in enumerate(s) if v]

_SP: list[int] = []

def trial_division(n: int, bound: int = 2_000_000) -> Optional[int]:
    global _SP
    if not _SP:
        _SP = _sieve_primes(bound)
    for p in _SP:
        if p * p > n:
            break
        if n % p == 0:
            return p
    return None


def pollard_rho(n: int, iterations: int = 500_000) -> Optional[int]:
    n = mpz(n)
    for c in range(1, 20):
        x = y = mpz(2)
        d = mpz(1)
        c_v = mpz(c)
        for _ in range(iterations):
            x = (x * x + c_v) % n
            y = (y * y + c_v) % n
            y = (y * y + c_v) % n
            d = gcd(abs(x - y), n)
            if d != 1:
                break
        if 1 < d < n:
            return int(d)
    return None


def quick_ecm(n: int, timeout: int = 90) -> Optional[tuple[int, int]]:
    ecm = _find_bin(ECM_BIN, "/usr/local/bin/ecm", "ecm")
    if not ecm:
        return None
    try:
        r = subprocess.run(
            [ecm, "-c", "100", "50000"],
            input=str(n) + "\n",
            capture_output=True, text=True, timeout=timeout,
        )
        for line in (r.stdout + r.stderr).splitlines():
            if "Found" in line and "factor" in line.lower():
                m = re.search(r":\s*(\d+)", line)
                if m:
                    f = int(m.group(1))
                    if 1 < f < n and n % f == 0:
                        return f, n // f
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Polynomial selection
# ─────────────────────────────────────────────────────────────────────────────

def run_polyselect(n: int, workdir: str, budget: int) -> Optional[str]:
    poly_path = os.path.join(workdir, "cado.poly")
    polydir   = os.path.join(workdir, "polysel")
    os.makedirs(polydir, exist_ok=True)

    polysel = find_cado_bin("polyselect2l") or find_cado_bin("polyselect")
    if polysel:
        return _polyselect2l(n, polysel, polydir, poly_path, budget)

    msieve = _find_bin(MSIEVE_GPU, MSIEVE_CPU, "msieve")
    if msieve:
        return _polyselect_msieve(n, msieve, workdir, poly_path, budget)

    return _poly_base_m(n, poly_path)


def _polyselect2l(n: int, binary: str, polydir: str,
                  poly_path: str, budget: int) -> Optional[str]:
    log(f"Stage 1: polyselect2l ({budget}s, all cores)")
    n_str = str(n)
    out_file = os.path.join(polydir, "polysel.out")
    cmd = [
        binary,
        f"-N", n_str,
        f"-degree", "5",
        f"-admin", "120",
        f"-admax", "10800",
        f"-incr",  "60",
        f"-P",     "3200000",
        f"-t",     str(get_cfs_cpu_quota()),
        f"-o",     out_file,
    ]
    log(f"  {' '.join(cmd[:8])}...")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    deadline = time.time() + budget
    best_e = -1.0
    best_text = ""

    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(15)
        if os.path.exists(out_file):
            try:
                with open(out_file) as f:
                    content = f.read()
                for m in re.finditer(r"# MurphyE\s*=\s*([\d.e+\-]+)", content, re.I):
                    val = float(m.group(1))
                    if val > best_e:
                        best_e = val
                        best_text = content
                        log(f"  Murphy-E = {val:.4e}")
            except Exception:
                pass

    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    if os.path.exists(out_file):
        try:
            with open(out_file) as f:
                content = f.read()
            for m in re.finditer(r"# MurphyE\s*=\s*([\d.e+\-]+)", content, re.I):
                val = float(m.group(1))
                if val > best_e:
                    best_e = val
                    best_text = content
        except Exception:
            pass

    if best_e > 0 and best_text:
        blocks = re.findall(r"(skew[\s\S]*?Y1:\s*[^\n]+)", best_text, re.IGNORECASE)
        if blocks:
            with open(poly_path, "w") as f:
                f.write(f"# Murphy-E = {best_e:.6e}\n")
                f.write(blocks[-1] + "\n")
            log(f"  Polynomial written: Murphy-E = {best_e:.4e}")
            return poly_path

    return _poly_base_m(n, poly_path)


def _polyselect_msieve(n: int, msieve: str, workdir: str,
                       poly_path: str, budget: int) -> Optional[str]:
    log(f"Stage 1: msieve polynomial selection ({budget}s)")
    cmd = [msieve, "-v", "-t", str(get_cfs_cpu_quota()), "-np1", "-ng", str(n)]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=workdir, text=True,
    )
    deadline = time.time() + budget
    while time.time() < deadline and proc.poll() is None:
        line = proc.stdout.readline()
        if line and any(k in line.lower() for k in ["poly", "murphy", "error"]):
            log(f"  [msieve] {line.rstrip()}")
        time.sleep(0.1)
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    for pat in [f"{workdir}/*.poly", f"{workdir}/*.p"]:
        found = glob.glob(pat)
        if found:
            cand = max(found, key=os.path.getmtime)
            shutil.copy(cand, poly_path)
            return poly_path

    return _poly_base_m(n, poly_path)


def _poly_base_m(n: int, poly_path: str) -> Optional[str]:
    """Last-resort: base-m polynomial (low quality)."""
    N = mpz(n)
    m = int(round(float(N)**0.2))
    for _ in range(20):
        m5 = mpz(m)**5
        delta = (m5 - N) // (5 * mpz(m)**4)
        m -= int(delta)
        if abs(int(delta)) <= 1:
            break
    m = mpz(m)
    coeffs, rem = [], N
    for _ in range(6):
        c = int(rem % m)
        if c > int(m) // 2:
            c -= int(m)
        coeffs.append(c)
        rem = (rem - c) // m
    skew = max(1, int((abs(coeffs[0]) / max(1, abs(coeffs[5])))**0.2))
    with open(poly_path, "w") as f:
        f.write(f"# Base-m fallback  m={int(m)}\nskew: {skew}\n")
        for i, c in enumerate(coeffs):
            f.write(f"c{i}: {c}\n")
        f.write(f"Y0: {-int(m)}\nY1: 1\n")
    log(f"  Fallback base-m polynomial (m={int(m)})")
    return poly_path


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Lattice sieve — SMT-aware
# ─────────────────────────────────────────────────────────────────────────────

def _count_rels_fast(rels_dir: str) -> int:
    files = glob.glob(os.path.join(rels_dir, "*.rels.gz"))
    if not files:
        return 0
    total_bytes = sum(os.path.getsize(f) for f in files)
    sample = min(files, key=os.path.getsize)
    try:
        with gzip.open(sample, "rt", errors="replace") as f:
            lines = [l for l in f if l.strip() and not l.startswith("#")]
        n_sample = len(lines)
        b_sample = os.path.getsize(sample)
        if n_sample > 0 and b_sample > 0:
            return int(total_bytes * n_sample / b_sample)
    except Exception:
        pass
    return len(files) * 40_000


def run_sieve(n: int, poly_path: str, workdir: str,
              n_jobs: int, rels_wanted: int, budget: int) -> int:
    """
    Run CADO-NFS lattice sieve with `n_jobs` concurrent single-threaded jobs.

    SMT STRATEGY (Intel GNR):
    - n_jobs = 2 × quota_cpus (e.g., 48 for --cpus 24)
    - Each job runs las with --t 1 (single thread)
    - 48 jobs on 24 physical cores = 2 threads/core via CFS scheduling
    - CFS interleaves the threads; when modinv chain A stalls, chain B runs
    - The dependency-chain-bound inner loops (invmod 11.9%, lattice 14.5%)
      are the exact use case where SMT provides maximum benefit

    AMD STRATEGY:
    - n_jobs = quota_cpus (e.g., 24 for --cpus 24)
    - AMD already fast via native AVX-512 + znver5 scheduling
    """
    las = find_cado_bin("las")
    if not las:
        log("CRITICAL: 'las' binary not found!")
        return 0

    rels_dir = os.path.join(workdir, "rels")
    os.makedirs(rels_dir, exist_ok=True)

    # Each job covers one Q slice; we rotate through slices until we have enough rels
    Q_SLICE = 200_000  # each job covers 200K special-q values
    q_next = Q_START
    deadline_sieve = time.time() + budget

    log(f"Stage 2: lattice sieve")
    log(f"  I={SIEVE_I}, lim0={SIEVE_LIM0//1_000_000}M, lim1={SIEVE_LIM1//1_000_000}M, "
        f"lpb={SIEVE_LPB}")
    log(f"  {n_jobs} concurrent jobs × 1 thread = {n_jobs} sieve workers")
    log(f"  (SMT: {n_jobs} threads across {get_cfs_cpu_quota()} quota CPUs)")
    log(f"  target: {rels_wanted:,} relations, budget {budget//60:.0f} min")

    def make_las_cmd(q0: int, job_id: int) -> list[str]:
        q1 = q0 + Q_SLICE
        out = os.path.join(rels_dir, f"rels_{q0:012d}.rels.gz")
        return [
            las,
            "--poly",     poly_path,
            "--lim0",     str(SIEVE_LIM0),
            "--lim1",     str(SIEVE_LIM1),
            "--lpb0",     str(SIEVE_LPB),
            "--lpb1",     str(SIEVE_LPB),
            "--mfb0",     str(SIEVE_MFB),
            "--mfb1",     str(SIEVE_MFB),
            "--ncurves0", str(SIEVE_NCURVES0),
            "--ncurves1", str(SIEVE_NCURVES1),
            "-I",         str(SIEVE_I),
            "--q0",       str(q0),
            "--q1",       str(q1),
            "--out",      out,
            "--t",        "1",   # single-threaded; parallelism via n_jobs processes
        ], q1

    # Launch initial pool of n_jobs concurrent processes
    running: list[tuple[subprocess.Popen, int]] = []  # (proc, q1)
    for i in range(min(n_jobs, 200)):  # cap to avoid fork bomb
        cmd, q1 = make_las_cmd(q_next, i)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            running.append((proc, q1))
            q_next = q1
        except Exception as e:
            log(f"  WARNING: failed to launch job {i}: {e}")
            break

    log(f"  Launched {len(running)} initial jobs, q starting from {Q_START:,}")

    last_report = time.time()
    last_rels = 0
    report_interval = 60

    while True:
        now = time.time()
        if now >= deadline_sieve:
            log("  Sieve budget exhausted")
            break

        # Reap finished jobs and launch replacements
        still_running = []
        for proc, q1 in running:
            if proc.poll() is not None:
                # This job finished; launch a new one for the next Q slice
                if now < deadline_sieve - 30:
                    cmd, new_q1 = make_las_cmd(q_next, 0)
                    try:
                        new_proc = subprocess.Popen(
                            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                        still_running.append((new_proc, new_q1))
                        q_next = new_q1
                    except Exception:
                        pass
            else:
                still_running.append((proc, q1))
        running = still_running

        if now - last_report >= report_interval:
            rels = _count_rels_fast(rels_dir)
            dt = now - last_report
            rate = (rels - last_rels) / dt if last_rels > 0 and dt > 0 else 0.0
            eta  = (rels_wanted - rels) / rate if rate > 0 else float("inf")
            log(f"  Rels: {rels:,}/{rels_wanted:,}  q_max={q_next:,}  "
                f"rate={rate:.0f}/s  ETA={eta/60:.1f}min  "
                f"jobs_running={len(running)}")
            last_report = now
            last_rels = rels

            if rels >= rels_wanted:
                log(f"  Target {rels_wanted:,} reached!")
                break

        time.sleep(10)

    # Gracefully stop all running jobs
    log(f"  Stopping {len(running)} sieve jobs...")
    for proc, _ in running:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
    for proc, _ in running:
        if proc.poll() is None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()

    final = _count_rels_fast(rels_dir)
    log(f"  Sieve done: ~{final:,} relations  elapsed={( time.time()-_START)/60:.1f}min")
    return final


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: Filtering (purge → merge → replay)
# ─────────────────────────────────────────────────────────────────────────────

def _write_filelist(files: list[str], path: str) -> None:
    with open(path, "w") as f:
        for fn in files:
            f.write(fn + "\n")


def _run(cmd: list[str], tag: str, cwd: str, timeout: int,
         kw: Optional[list[str]] = None) -> int:
    kw = kw or ["error", "warning", "done", "relation", "matrix",
                "primes", "elapsed", "weight", "density"]
    log(f"  [{tag}] {' '.join(str(x) for x in cmd[:7])}...")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=cwd, text=True,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if any(k in line.lower() for k in kw):
                log(f"  [{tag}] {line}")
        proc.wait(timeout=timeout)
        return proc.returncode or 0
    except subprocess.TimeoutExpired:
        proc.kill()
        log(f"  [{tag}] TIMEOUT after {timeout}s")
        return -1
    except Exception as ex:
        log(f"  [{tag}] error: {ex}")
        return -1


def run_filter(poly_path: str, workdir: str) -> Optional[str]:
    log("Stage 3: filtering (purge → merge → replay)")
    quota = get_cfs_cpu_quota()
    rels_dir   = os.path.join(workdir, "rels")
    rels_files = sorted(glob.glob(os.path.join(rels_dir, "*.rels.gz")))
    if not rels_files:
        log("  ERROR: no .rels.gz files found!")
        return None
    log(f"  {len(rels_files)} relation file(s)")

    purged   = os.path.join(workdir, "cado.purged.gz")
    renumber = os.path.join(workdir, "cado.renumber")
    merge_h  = os.path.join(workdir, "cado.merge.his")
    index    = os.path.join(workdir, "cado.index.gz")
    mat_base = os.path.join(workdir, "cado.matrix")
    filelist = os.path.join(workdir, "rels.filelist")
    _write_filelist(rels_files, filelist)

    # freerel (optional)
    freerel_bin = find_cado_bin("freerel")
    freerel_out = os.path.join(workdir, "cado.freerel")
    if freerel_bin:
        _run([freerel_bin, f"--poly={poly_path}", f"--lpb0={SIEVE_LPB}",
              f"--lpb1={SIEVE_LPB}", f"--out={freerel_out}",
              f"--renumber={renumber}", f"--t={quota}"],
             "freerel", workdir, 600)

    # purge
    purge_bin = find_cado_bin("purge")
    if not purge_bin:
        log("  ERROR: purge not found!")
        return None
    purge_base = [purge_bin, f"--poly={poly_path}",
                  f"--lpb0={SIEVE_LPB}", f"--lpb1={SIEVE_LPB}",
                  f"--out={purged}", f"--t={quota}"]
    if os.path.isfile(renumber):
        purge_base += [f"--renumber={renumber}"]
    if os.path.isfile(freerel_out):
        purge_base += [f"--freerel={freerel_out}"]

    ret = _run(purge_base + [f"--filelist={filelist}"], "purge", workdir, 2400)
    if ret != 0 or not os.path.isfile(purged):
        log("  retrying purge with positional args")
        ret = _run(purge_base + rels_files, "purge", workdir, 2400)
    if ret != 0 or not os.path.isfile(purged):
        log(f"  purge failed (exit {ret})")
        return None

    # merge
    merge_bin = find_cado_bin("merge")
    if not merge_bin:
        log("  ERROR: merge not found!")
        return None
    merge_cmd = [merge_bin, f"--purged={purged}", f"--out={merge_h}",
                 f"--target-density={TARGET_DENSITY}", f"--t={quota}"]
    if os.path.isfile(renumber):
        merge_cmd += [f"--renumber={renumber}"]
    ret = _run(merge_cmd, "merge", workdir, 2400)
    if ret != 0:
        log(f"  merge failed (exit {ret})")
        return None

    # replay
    replay_bin = find_cado_bin("replay")
    if not replay_bin:
        log("  ERROR: replay not found!")
        return None
    ret = _run([replay_bin, f"--purged={purged}", f"--history={merge_h}",
                f"--index={index}", f"--out={mat_base}", f"--t={quota}"],
               "replay", workdir, 1800)
    if ret != 0:
        log(f"  replay failed (exit {ret})")
        return None

    log(f"  Filtering done — matrix at {mat_base}")
    return mat_base


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: Linear algebra — msieve GPU block-Lanczos
# ─────────────────────────────────────────────────────────────────────────────

def run_linalg(n: int, mat_base: str, workdir: str) -> Optional[str]:
    use_gpu = gpu_available()
    msieve = _find_bin(
        MSIEVE_GPU if use_gpu else MSIEVE_CPU,
        MSIEVE_CPU, MSIEVE_GPU, "msieve",
    )
    if not msieve:
        log("  ERROR: msieve not found!")
        return None

    log(f"Stage 4: block-Lanczos LA ({'GPU' if use_gpu else 'CPU fallback'})")
    quota = get_cfs_cpu_quota()

    mat_files = (glob.glob(mat_base + "*.sparse.bin") +
                 glob.glob(mat_base + ".sparse.bin") +
                 glob.glob(mat_base + "*.bin"))
    if mat_files:
        msieve_mat = os.path.join(workdir, "msieve.mat")
        if mat_files[0] != msieve_mat:
            try:
                shutil.copy(mat_files[0], msieve_mat)
            except Exception:
                pass

    cmd = [msieve, "-v", "-t", str(quota), "-nc1"]
    if use_gpu:
        cmd += ["-ng"]
    cmd.append(str(n))

    log(f"  {' '.join(str(c) for c in cmd[:8])}...")
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=workdir, text=True,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if any(k in line.lower() for k in
                   ["lanczos", "block", "depend", "error", "matrix",
                    "linear", "elapsed", "gpu", "cuda", "column", "found"]):
                log(f"  [msieve LA] {line}")
        proc.wait(timeout=7200)
        log(f"  LA done: exit {proc.returncode}, {(time.time()-t0)/60:.1f} min")
        if proc.returncode != 0:
            return None
    except subprocess.TimeoutExpired:
        proc.kill()
        log("  msieve LA timed out!")
        return None
    except Exception as ex:
        log(f"  msieve LA error: {ex}")
        return None

    deps = glob.glob(os.path.join(workdir, "*.deps"))
    return deps[0] if deps else None


def run_linalg_bwc_fallback(mat_base: str, workdir: str) -> Optional[str]:
    bwc = find_cado_bin("bwc.pl")
    if not bwc:
        return None
    quota = get_cfs_cpu_quota()
    log("Stage 4 (fallback): CADO bwc CPU LA")
    bwc_dir = os.path.join(workdir, "bwc")
    os.makedirs(bwc_dir, exist_ok=True)
    mat_file = mat_base + ".sparse.bin"
    if not os.path.isfile(mat_file):
        mf = glob.glob(mat_base + "*.sparse.bin")
        if not mf:
            return None
        mat_file = mf[0]
    cmd = ["perl", bwc, f"matrix={mat_file}", "nullspace=left",
           f"wdir={bwc_dir}", "mpi=1x1",
           f"thr={quota//4}x4", "m=64", "n=64", "interval=1000"]
    ret = _run(cmd, "bwc", workdir, timeout=14400,
               kw=["check", "error", "depend", "done", "elapsed"])
    if ret != 0:
        return None
    w_files = glob.glob(os.path.join(bwc_dir, "W.*"))
    return w_files[0] if w_files else None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5: Square root (CADO sqrt)
# ─────────────────────────────────────────────────────────────────────────────

def run_sqrt(n: int, poly_path: str, dep_file: str, workdir: str) -> Optional[tuple[int, int]]:
    sqrt_bin = find_cado_bin("sqrt")
    if not sqrt_bin:
        log("  ERROR: sqrt not found!")
        return None
    quota = get_cfs_cpu_quota()
    log("Stage 5: square root")
    purged = os.path.join(workdir, "cado.purged.gz")
    index  = os.path.join(workdir, "cado.index.gz")
    prefix = os.path.join(workdir, "cado.sqrt")
    _run([sqrt_bin, f"--poly={poly_path}", f"--purged={purged}",
          f"--index={index}", f"--dep={dep_file}",
          f"--prefix={prefix}", f"--t={quota}"],
         "sqrt", workdir, 1800,
         kw=["factor", "error", "done", "elapsed", "gcd"])

    for pat in [prefix + "*", os.path.join(workdir, "*.factors")]:
        for fn in glob.glob(pat):
            try:
                with open(fn) as f:
                    content = f.read().strip()
                for tok in content.split():
                    try:
                        p = int(tok)
                        if 1 < p < n and n % p == 0:
                            log(f"  Factor found ({len(str(p))} digits)")
                            return p, n // p
                    except ValueError:
                        pass
            except Exception:
                pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Full GNFS pipeline
# ─────────────────────────────────────────────────────────────────────────────

def cado_pipeline(n: int, num_bits: int, cpu_info: dict) -> Optional[tuple[int, int]]:
    setup_workdir()
    wd = WORK_DIR

    # Pin threads to compact SNC domain (reduces cross-die migration on GNR-AP)
    pinned = pin_to_compact_cpus(cpu_info)
    log(f"CPU pinning: {'OK' if pinned else 'failed (continuing unpinned)'}")

    n_jobs = get_optimal_job_count(cpu_info)

    # Time allocation
    total = time_left()
    poly_budget  = int(min(720, total * 0.05))   # ~5% = 12 min
    filter_time  = 600
    la_time      = 2100     # GPU LA: 19 min + margin
    sqrt_time    = 300
    reserved     = filter_time + la_time + sqrt_time + 120
    sieve_budget = int(total - poly_budget - reserved)
    sieve_budget = max(300, sieve_budget)

    log(f"\n{'='*60}")
    log(f"GNFS pipeline for c{len(str(n))} / {num_bits}-bit")
    log(f"{'='*60}")
    log(f"CPU: {cpu_info['vendor']} {cpu_info['model'][:60]}")
    log(f"Quota: {cpu_info['quota_cpus']} CPUs, NUMA node 0: {len(cpu_info['numa0_cpus'])} CPUs")
    log(f"SMT: {'ON' if cpu_info['smt_on'] else 'OFF/unknown'}, "
        f"threads/core: {cpu_info['threads_per_core']}")
    log(f"Jobs: {n_jobs} × las.threads=1 (SMT exploitation on Intel GNR)")
    log(f"Budget: poly={poly_budget//60:.0f}m  sieve={sieve_budget//60:.0f}m  "
        f"filter={filter_time//60:.0f}m  LA={la_time//60:.0f}m")

    # ── 1. Polynomial selection ───────────────────────────────────────
    poly_path = run_polyselect(n, wd, poly_budget)
    if not poly_path:
        log("Polynomial selection failed; aborting")
        return None

    # ── 2. Lattice sieve (SMT-exploiting, GNR-native binary) ─────────
    rels = run_sieve(n, poly_path, wd, n_jobs, RELS_WANTED, sieve_budget)
    if rels < 30_000_000:
        log(f"WARNING: only {rels:,} relations collected")

    # ── 3. Filtering ─────────────────────────────────────────────────
    mat_base = run_filter(poly_path, wd)
    if not mat_base:
        log("Filtering failed; aborting")
        return None

    # ── 4. Linear algebra (GPU) ───────────────────────────────────────
    dep_file = run_linalg(n, mat_base, wd)
    if not dep_file:
        log("GPU LA failed; trying CPU bwc fallback")
        dep_file = run_linalg_bwc_fallback(mat_base, wd)
    if not dep_file:
        log("Linear algebra failed; aborting")
        return None

    # ── 5. Square root ────────────────────────────────────────────────
    result = run_sqrt(n, poly_path, dep_file, wd)
    if result:
        return result

    for extra in glob.glob(os.path.join(wd, "*.deps")):
        if extra == dep_file:
            continue
        result = run_sqrt(n, poly_path, extra, wd)
        if result:
            return result

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Fallback: msieve standalone GNFS
# ─────────────────────────────────────────────────────────────────────────────

def msieve_gnfs(n: int, quota: int) -> Optional[tuple[int, int]]:
    msieve = _find_bin(MSIEVE_GPU, MSIEVE_CPU, "msieve")
    if not msieve:
        return None
    use_gpu = gpu_available()
    log(f"Fallback: msieve GNFS ({'GPU' if use_gpu else 'CPU'})")
    tmp = tempfile.mkdtemp(prefix="msieve-gnfs-")
    try:
        cmd = [msieve, "-v", "-t", str(quota)]
        if use_gpu:
            cmd += ["-ng"]
        cmd.append(str(n))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=tmp, text=True,
        )
        factors = []
        for line in proc.stdout:
            line = line.rstrip()
            if any(k in line.lower() for k in
                   ["factor", "elapsed", "error", "siqs", "gnfs", "lanczos"]):
                log(f"  [msieve] {line}")
            m = re.match(r"(?:prp|p)(\d+):\s+(\d+)", line.strip())
            if m:
                try:
                    f = int(m.group(2))
                    if 1 < f < n:
                        factors.append(f)
                except ValueError:
                    pass
        proc.wait()
        if len(factors) >= 2:
            return factors[0], factors[1]
        if len(factors) == 1:
            q = n // factors[0]
            if factors[0] * q == n:
                return factors[0], q
    except Exception as ex:
        log(f"  msieve error: {ex}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def factor(n: int, num_bits: int, cpu_info: dict) -> tuple[Optional[int], Optional[int], str]:
    n_digits = len(str(n))
    log(f"N = {str(n)[:72]}{'...' if n_digits > 72 else ''}")
    log(f"    {n_digits} digits, {num_bits} bits")

    if gmpy2.is_prime(n):
        log("ERROR: N is prime — not a valid semiprime")
        return None, None, "failed"

    log("Stage 0a: trial division (2M)...")
    f = trial_division(n, 2_000_000)
    if f:
        return f, n // f, "trial_division"

    log("Stage 0b: Pollard ρ (500K steps)...")
    f = pollard_rho(n, 500_000)
    if f:
        return f, n // f, "pollard_rho"

    log("Stage 0c: quick ECM (t25, 100 curves)...")
    res = quick_ecm(n, timeout=90)
    if res:
        return res[0], res[1], "ecm_quick"

    if n_digits >= 80:
        result = cado_pipeline(n, num_bits, cpu_info)
        if result:
            return result[0], result[1], "cado_gnfs"

    quota = cpu_info["quota_cpus"]
    if time_left() > 300:
        result = msieve_gnfs(n, quota)
        if result:
            return result[0], result[1], "msieve_gnfs"

    return None, None, "failed"


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: breaking_rsa.py <challenge_id> <JSON-encoded Problem>")
        sys.exit(1)

    challenge_id = sys.argv[1].strip()
    try:
        problem = Problem.from_json(sys.argv[2].strip())
    except Exception as err:
        print(f"Error parsing problem: {err}")
        sys.exit(1)

    if problem.num < 6:
        print("Error: N must be a positive non-trivial semiprime")
        sys.exit(1)

    timestamp_start = datetime.now(timezone.utc).isoformat()

    # Detect CPU topology immediately
    cpu_info = detect_cpu()

    # Log build metadata
    try:
        build_meta = json.loads(
            _read_file("/cado-nfs/build_meta.json") or "{}"
        )
    except Exception:
        build_meta = {}

    log("=" * 70)
    log("Breaking RSA — CADO-NFS + msieve GPU (Intel GNR optimised)")
    log(f"Challenge : {challenge_id}")
    log(f"CPU       : {cpu_info['vendor']} {cpu_info['model'][:50]}")
    log(f"Quota CPUs: {cpu_info['quota_cpus']}  SMT: {cpu_info['smt_on']}  "
        f"threads/core: {cpu_info['threads_per_core']}")
    log(f"NUMA node0: {len(cpu_info['numa0_cpus'])} CPUs")
    log(f"GPU       : {gpu_available()}")
    log(f"Build     : {build_meta.get('flags','(unknown)')[:80]}")
    log(f"LD_PRELOAD: {os.environ.get('LD_PRELOAD','(not set)')}")
    log(f"Deadline  : {(WALL_LIMIT-SAFETY_MARGIN)//60} min")
    log("=" * 70)

    p, q, method = factor(problem.num, problem.num_bits, cpu_info)
    solve_time = time.time() - _START

    if p is not None and q is not None:
        log(f"\nSUCCESS via {method} in {solve_time:.1f}s ({solve_time/60:.2f} min)")
        log(f"p ({len(str(p))} digits): {str(p)[:50]}...")
        log(f"q ({len(str(q))} digits): {str(q)[:50]}...")
        solution = Solution("success", p, q)
    else:
        log(f"\nFAILED after {solve_time:.1f}s ({solve_time/60:.2f} min)")
        solution = Solution("failed", None, None)

    result_json = json.dumps(solution.to_dict(), indent=2)
    solve_info  = json.dumps({
        "solution_status": solution.status,
        "challenge_id":    challenge_id,
        "timestamp_utc":   timestamp_start,
        "solve_time_s":    solve_time,
        "method":          method,
        "num_bits":        problem.num_bits,
        "num_digits":      len(str(problem.num)),
        "rels_wanted":     RELS_WANTED,
        "cpu_vendor":      cpu_info["vendor"],
        "cpu_model":       cpu_info["model"],
        "quota_cpus":      cpu_info["quota_cpus"],
        "smt_on":          cpu_info["smt_on"],
        "n_jobs":          get_optimal_job_count(cpu_info),
        "gpu":             gpu_available(),
        "build_flags":     build_meta.get("flags", "unknown"),
    }, indent=2)

    output_dir = os.environ.get("OUTPUT_DIR")
    if output_dir:
        try:
            Path(output_dir).mkdir(exist_ok=True)
            (Path(output_dir) / "result.json").write_text(result_json)
            (Path(output_dir) / "solve_info.json").write_text(solve_info)
        except OSError:
            pass

    zip_bytes = build_solution_zip({
        "result.json":     result_json,
        "solve_info.json": solve_info,
    })
    write_solution_output(zip_bytes)
    os._exit(0 if solution.status == "success" else 1)


if __name__ == "__main__":
    main()
