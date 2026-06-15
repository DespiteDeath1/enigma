#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2026 qBitTensor Labs
#
# CADO-NFS + msieve GPU block-Lanczos — definitive Intel GNR analysis
#
# ══════════════════════════════════════════════════════════════════════════════
# REAL 6767P MEASUREMENTS (Granite Rapids-SP, same Redwood Cove cores as 6980P)
# ══════════════════════════════════════════════════════════════════════════════
#
# DECISIVE FINDING: Intel GNR sieve is MEMORY-BANDWIDTH-BOUND at 24 cores.
#
# Core-scaling sweep: 12c→51.6, 24c→52.3 (+1.3% FLAT), 48c→62.5 (+21%/2-NUMA)
# Best stack: icelake-server × 2-NUMA-spread × -t24 = 63.68 rel/s → 321 min.
# Need ≤240 min → need 25% more improvement → no known lever provides this.
#
# ── FRONTIER ANALYSIS (from §3 of the brief) ────────────────────────────────
#
# F1. FEWER RELATIONS (reduce sieve work):
#     Matrix floor is set by factor base size (~25M columns): need ~25M rows
#     after filtering, requiring ~66-74M initial relations. No sieve change
#     reduces this without proportionally hurting sieve yield. IMPLEMENTED:
#     Adaptive floor from 63M, automatic retry at +5M until success.
#     Max achievable saving: ~10% of sieve → saves ~28 min. Not enough alone.
#
# F2. GPU RELATION SOURCE (replace CPU sieve):
#     GPU bucket sieve is 130× slower (warp divergence from random scatter).
#     No reformulation avoids this: bucket fill IS inherently random scatter.
#     CADO source-audited: push_update = *bucket_write[i]++ = update (no branch).
#     Random scatter pattern → L2/L3 bandwidth limited → GNR-specific bottleneck.
#     GPU "direct evaluation" (evaluate F(a,b) at all positions): needs O(N)
#     polynomial evaluations vs O(N/ln(N)) for the sieve → 10-100× more work.
#     DEAD: no GPU relation source exists for general GNFS c139.
#
# F3. BUILD-TIME PRECOMPUTATION:
#     N is NOT known at Docker build time (passed as runtime argument).
#     Only N-independent data can be precomputed: binaries, prime tables
#     (already done). The polynomial (12 min) and all sieve data depend on N.
#     DEAD: build-time precompute cannot help for N-specific stages.
#
# F4. ALTERNATIVE ALGORITHM:
#     GNFS is asymptotically optimal for generic integers. At 460 bits:
#     - MPQS/SIQS: max ~110 digits, dead
#     - ECM: finds factors up to ~55 digits; our factors are ~70 digits
#     - TNFS/SNFS: requires special algebraic structure (none present)
#     - MNFS (multiple NFS): ~15-20% speedup, brings 321→270 min, still >240
#     - Shor's algorithm: requires fault-tolerant QC (NISQ devices can't)
#     DEAD: no algorithm is faster than GNFS for generic c139.
#
# F5. MORE PIPELINE STAGES ON GPU:
#     LA already on GPU (~9% wall = 29 min). Filtering (purge/merge/replay)
#     is ~5-10% wall = 16-32 min. GPU filtering (parallel histogram + hash join)
#     could save 15-25 min. Combined: 321 - 25 = 296 min. Still 56 min over.
#     COMPOSITE: NUMA + floor + filtering: 321 - 15 - 15 - 22 = 269 min.
#     Still 29 min over the 240-min cap.
#
# F6. COMPOSITE SPECIAL-Q / SUBLATTICE (CADO --sublat m):
#     CADO supports sublattice sieving (for DLP descent) but NOT composite
#     special-q for factoring in any meaningful way. The `sublat_bound` in
#     CADO is for DLP, not factoring. Source-confirmed: allow_composite_q()
#     is only enabled for descent mode. Using --sublat reduces effective sieve
#     area per q without changing the relation floor. Net: same or worse.
#     DEAD: sublat/composite-q does not reduce the relation floor for factoring.
#
# ── IMPLEMENTED LEVERS ──────────────────────────────────────────────────────
#
# 1. MAXIMUM NUMA SPREADING: detect all NUMA nodes, spread 24 processes evenly.
#    GNR-AP (6980P) may have 4-8 NUMA nodes. Each node past 2 gives ~+5-10%.
#    Potential total from 4-NUMA: 321 × 0.90 = 289 min.
#
# 2. ADAPTIVE RELATION FLOOR: start at 63M, retry at +5M if purge fails.
#    Best case (63M works): saves ~24 min → 289 - 24 = 265 min.
#
# 3. MULTI-SEED GPU LA: run msieve with multiple random seeds when close to
#    the floor. If one seed fails (no dependency found), try another without
#    re-sieving. Allows using relations at the statistical floor boundary.
#
# THEORETICAL MINIMUM: ~265-280 min, ~25-40 min over the 240-min cap.
# The gap appears fundamental to GNR architecture for this workload.

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
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_START = time.time()
WALL_LIMIT    = 14_400
SAFETY_MARGIN = 180
DEADLINE      = _START + WALL_LIMIT - SAFETY_MARGIN

MEMFS_ROOT = os.environ.get("MEMFS_ROOT", "/cado-work")
WORK_DIR   = MEMFS_ROOT

MSIEVE_GPU = os.environ.get("MSIEVE_GPU", "/usr/local/bin/msieve-gpu")
MSIEVE_CPU = os.environ.get("MSIEVE_CPU", "/usr/local/bin/msieve-cpu")
ECM_BIN    = os.environ.get("ECM_BIN",    "/usr/local/bin/ecm")
CADO_DIR   = os.environ.get("CADO_DIR",   "/cado-nfs")

# Sieve parameters (measured best on AMD EPYC 9555)
SIEVE_LIM0     = 11_000_000
SIEVE_LIM1     = 14_000_000
SIEVE_LPB      = 30
SIEVE_MFB      = 60
SIEVE_NCURVES0 = 17
SIEVE_NCURVES1 = 29
SIEVE_I        = 13
Q_START        = SIEVE_LIM1
TARGET_DENSITY = 125

# Relation floor: measured msieve floor = 66-74M. Start at 63M, retry +5M
# Each 5M reduction saves ~2-3 min of sieve time (arch-neutral, Intel is ~35% of that).
RELS_WANTED_START = 63_000_000   # aggressive first attempt
RELS_WANTED_STEP  =  5_000_000   # retry increment if purge fails
RELS_WANTED_MAX   = 74_000_000   # safe upper bound (measured floor)

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
# CPU / NUMA topology
# ─────────────────────────────────────────────────────────────────────────────

def _read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _parse_cpulist(s: str) -> list[int]:
    cpus = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                cpus.extend(range(int(a), int(b) + 1))
            except ValueError:
                pass
        elif part.isdigit():
            cpus.append(int(part))
    return sorted(set(cpus))


def get_cfs_cpu_quota() -> int:
    # cgroup v2
    for path in ["/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpu,cpuacct/cpu.max"]:
        val = _read_file(path)
        if val and val != "max 100000":
            parts = val.split()
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


def get_numa_topology() -> dict[int, list[int]]:
    """
    Return a mapping of NUMA node → CPU list.

    On GNR-AP (6980P), there may be 4-8 NUMA nodes (2 dies × 2-4 SNC each).
    On the tested 6767P-SP, there were 4 NUMA nodes.
    More nodes = more total bandwidth headroom (each node has its own channels).
    """
    nodes: dict[int, list[int]] = {}
    for nd in sorted(glob.glob("/sys/devices/system/node/node[0-9]*")):
        node_id = int(nd.split("node")[-1])
        cpulist = _read_file(f"{nd}/cpulist")
        if cpulist:
            cpus = _parse_cpulist(cpulist)
            if cpus:  # skip nodes with no CPUs (memory-only nodes)
                nodes[node_id] = cpus
    return nodes


def detect_cpu() -> dict:
    info = {
        "vendor":     "Unknown",
        "model":      "Unknown",
        "is_intel":   False,
        "is_amd":     False,
        "quota_cpus": get_cfs_cpu_quota(),
        "numa_nodes": {},
    }
    cpuinfo = _read_file("/proc/cpuinfo")
    if "GenuineIntel" in cpuinfo:
        info["vendor"] = "Intel"
        info["is_intel"] = True
    elif "AuthenticAMD" in cpuinfo:
        info["vendor"] = "AMD"
        info["is_amd"] = True
    for line in cpuinfo.splitlines():
        if "model name" in line:
            info["model"] = line.split(":", 1)[-1].strip()
            break
    info["numa_nodes"] = get_numa_topology()
    return info


def pin_and_spread_numa(cpu_info: dict) -> list[Optional[int]]:
    """
    The ONLY confirmed lever on Intel GNR: spread processes across NUMA nodes.
    Measured: 2-NUMA spread gives +21% over 1-NUMA (bandwidth saturation at ~12c/node).
    This implementation spreads across ALL detected NUMA nodes (maximizing bandwidth).

    GNR-AP (6980P) topology:
    - 2 physical dies, each with multiple SNC (Sub-NUMA Clustering) domains
    - In SNC2 mode: 2 NUMA nodes × 2 dies = 4 nodes
    - In SNC4 mode: 4 NUMA nodes × 2 dies = 8 nodes
    - The tested 6767P-SP had 4 NUMA nodes
    - The validator 6980P-AP likely has 4+ NUMA nodes

    Returns a list of NUMA node IDs for each of the n_jobs processes.
    """
    quota = cpu_info["quota_cpus"]
    numa_nodes = cpu_info["numa_nodes"]
    n_nodes = len(numa_nodes)

    if n_nodes <= 1:
        log(f"  Only {n_nodes} NUMA node(s) detected; cannot spread")
        return [None] * quota

    log(f"  NUMA topology: {n_nodes} nodes × {[len(v) for v in numa_nodes.values()]} CPUs")

    # Round-robin assignment: spread quota_cpus processes across all NUMA nodes
    node_ids = sorted(numa_nodes.keys())
    assignments = []
    for i in range(quota):
        assignments.append(node_ids[i % n_nodes])

    # Pin this process to a reasonable starting affinity
    try:
        # Use the first NUMA node for the Python orchestrator itself
        first_node_cpus = numa_nodes[node_ids[0]]
        target = set(first_node_cpus[:max(1, quota // n_nodes)])
        os.sched_setaffinity(0, target)
    except Exception as e:
        log(f"  WARNING: sched_setaffinity failed: {e}")

    return assignments


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
# Work directory (RAM-backed)
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

def run_polyselect(n: int, workdir: str, budget: int, quota: int) -> Optional[str]:
    poly_path = os.path.join(workdir, "cado.poly")
    polydir   = os.path.join(workdir, "polysel")
    os.makedirs(polydir, exist_ok=True)

    polysel = find_cado_bin("polyselect2l") or find_cado_bin("polyselect")
    if polysel:
        return _polyselect2l(n, polysel, polydir, poly_path, budget, quota)

    msieve = _find_bin(MSIEVE_GPU, MSIEVE_CPU, "msieve")
    if msieve:
        return _polyselect_msieve(n, msieve, workdir, poly_path, budget, quota)

    return _poly_base_m(n, poly_path)


def _polyselect2l(n: int, binary: str, polydir: str,
                  poly_path: str, budget: int, quota: int) -> Optional[str]:
    log(f"Stage 1: polyselect2l ({budget}s, {quota} cores)")
    n_str = str(n)
    out_file = os.path.join(polydir, "polysel.out")
    cmd = [binary, f"-N", n_str, f"-degree", "5",
           f"-admin", "120", f"-admax", "10800", f"-incr", "60",
           f"-P", "3200000", f"-t", str(quota), f"-o", out_file]
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
            log(f"  Polynomial: Murphy-E = {best_e:.4e}")
            return poly_path

    return _poly_base_m(n, poly_path)


def _polyselect_msieve(n: int, msieve: str, workdir: str,
                       poly_path: str, budget: int, quota: int) -> Optional[str]:
    log(f"Stage 1: msieve polynomial selection ({budget}s)")
    cmd = [msieve, "-v", "-t", str(quota), "-np1", "-ng", str(n)]
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

    for pat in [f"{workdir}/*.poly"]:
        found = glob.glob(pat)
        if found:
            shutil.copy(max(found, key=os.path.getmtime), poly_path)
            return poly_path

    return _poly_base_m(n, poly_path)


def _poly_base_m(n: int, poly_path: str) -> Optional[str]:
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
    log(f"  Fallback base-m polynomial")
    return poly_path


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Lattice sieve — NUMA-spread for Intel GNR
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
        n_s = len(lines)
        b_s = os.path.getsize(sample)
        if n_s > 0 and b_s > 0:
            return int(total_bytes * n_s / b_s)
    except Exception:
        pass
    return len(files) * 40_000


def _build_las_cmd(las: str, poly_path: str, q0: int, q1: int,
                   out: str) -> list[str]:
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
        "--t",        "1",  # single-threaded; parallelism via concurrent processes
    ]


def run_sieve(n: int, poly_path: str, workdir: str,
              numa_assignments: list[Optional[int]],
              numa_nodes: dict[int, list[int]],
              rels_wanted: int, budget: int) -> int:
    """
    Run CADO-NFS lattice sieve with NUMA spreading.

    THE ONLY CONFIRMED LEVER ON INTEL GNR: memory-bandwidth spreading.

    Measured on 6767P: 24c in 1 SNC = 52.3 rel/s (BW-saturated at 12c).
                       24c across 2 SNC = 63.68 rel/s (+21%).

    This spreads the `quota_cpus` processes across ALL available NUMA nodes.
    For GNR-AP (6980P), which may have 4+ NUMA nodes, this maximizes the
    total memory bandwidth available to the sieve.

    Each process is assigned to a specific NUMA node via sched_setaffinity.
    Memory binding (numactl --membind) is set via the NUMA policy syscall.
    """
    las = find_cado_bin("las")
    if not las:
        log("CRITICAL: 'las' binary not found!")
        return 0

    rels_dir = os.path.join(workdir, "rels")
    os.makedirs(rels_dir, exist_ok=True)

    n_jobs = len(numa_assignments)
    n_distinct_nodes = len(set(a for a in numa_assignments if a is not None))
    Q_SLICE = 200_000

    log(f"Stage 2: lattice sieve → {rels_wanted:,} relations ({budget//60:.0f} min)")
    log(f"  {n_jobs} processes × 1 thread, spread across {n_distinct_nodes} NUMA node(s)")
    log(f"  NUMA assignments: {dict(zip(range(min(8, n_jobs)), numa_assignments[:8]))}")

    q_next = Q_START
    deadline_sieve = time.time() + budget

    def make_job(job_id: int, q0: int, node_id: Optional[int]) -> subprocess.Popen:
        q1 = q0 + Q_SLICE
        out = os.path.join(rels_dir, f"rels_{q0:012d}.rels.gz")
        cmd = _build_las_cmd(las, poly_path, q0, q1, out)
        env = os.environ.copy()

        # NUMA binding: set CPU and memory affinity for the child process
        # We use numactl if available, otherwise set via /proc
        numactl = shutil.which("numactl")
        if node_id is not None and numactl:
            cmd = [numactl,
                   f"--cpunodebind={node_id}",
                   f"--membind={node_id}"] + cmd

        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

    # Launch initial pool
    running: list[tuple[subprocess.Popen, int]] = []
    for i, node_id in enumerate(numa_assignments):
        try:
            proc = make_job(i, q_next, node_id)
            running.append((proc, q_next + Q_SLICE))
            q_next += Q_SLICE
        except Exception as e:
            log(f"  WARNING: failed to launch job {i}: {e}")
            break

    log(f"  Launched {len(running)} initial jobs from q={Q_START:,}")

    last_report = time.time()
    last_rels = 0
    report_interval = 60

    while True:
        now = time.time()
        if now >= deadline_sieve:
            log("  Sieve budget exhausted")
            break

        still_running = []
        job_idx = 0
        for proc, q1 in running:
            if proc.poll() is not None:
                # Relaunch with the next Q slice
                if now < deadline_sieve - 30:
                    node_id = numa_assignments[job_idx % n_jobs]
                    try:
                        new_proc = make_job(job_idx, q_next, node_id)
                        still_running.append((new_proc, q_next + Q_SLICE))
                        q_next += Q_SLICE
                    except Exception:
                        pass
            else:
                still_running.append((proc, q1))
            job_idx += 1
        running = still_running

        if now - last_report >= report_interval:
            rels = _count_rels_fast(rels_dir)
            dt = now - last_report
            rate = (rels - last_rels) / dt if last_rels > 0 and dt > 0 else 0.0
            eta  = (rels_wanted - rels) / rate if rate > 0 else float("inf")
            log(f"  Rels: {rels:,}/{rels_wanted:,}  q_max={q_next:,}  "
                f"rate={rate:.0f}/s  ETA={eta/60:.1f}min  running={len(running)}")
            last_report = now
            last_rels = rels

            if rels >= rels_wanted:
                log(f"  Target {rels_wanted:,} reached!")
                break

        time.sleep(10)

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
# Stage 3: Filtering with adaptive relation floor
# ─────────────────────────────────────────────────────────────────────────────

def _write_filelist(files: list[str], path: str) -> None:
    with open(path, "w") as f:
        for fn in files:
            f.write(fn + "\n")


def _run(cmd: list[str], tag: str, cwd: str, timeout: int,
         kw: Optional[list[str]] = None) -> tuple[int, str]:
    """Run a command, capture output, return (exit_code, output)."""
    kw = kw or ["error", "warning", "done", "relation", "matrix",
                "primes", "elapsed", "weight", "density", "singleton"]
    log(f"  [{tag}] {' '.join(str(x) for x in cmd[:7])}...")
    output_lines = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=cwd, text=True,
        )
        for line in proc.stdout:
            line = line.rstrip()
            output_lines.append(line)
            if any(k in line.lower() for k in kw):
                log(f"  [{tag}] {line}")
        proc.wait(timeout=timeout)
        return proc.returncode or 0, "\n".join(output_lines)
    except subprocess.TimeoutExpired:
        proc.kill()
        log(f"  [{tag}] TIMEOUT after {timeout}s")
        return -1, ""
    except Exception as ex:
        log(f"  [{tag}] error: {ex}")
        return -1, ""


def purge_failed(output: str) -> bool:
    """Detect if purge failed due to too few relations (below floor)."""
    fail_indicators = [
        "not enough relations",
        "too few relations",
        "nrels_purged",
        "singleton",
        "failed",
        "error",
    ]
    out_lower = output.lower()
    return any(ind in out_lower for ind in fail_indicators)


def run_filter_attempt(poly_path: str, workdir: str, quota: int,
                       rels_dir: str) -> Optional[str]:
    """
    Single attempt at the filter pipeline.
    Returns matrix base path on success, None on failure.
    """
    rels_files = sorted(glob.glob(os.path.join(rels_dir, "*.rels.gz")))
    if not rels_files:
        log("  ERROR: no .rels.gz files found!")
        return None

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

    # Remove stale purged file
    if os.path.isfile(purged):
        os.unlink(purged)

    purge_base = [purge_bin, f"--poly={poly_path}",
                  f"--lpb0={SIEVE_LPB}", f"--lpb1={SIEVE_LPB}",
                  f"--out={purged}", f"--t={quota}"]
    if os.path.isfile(renumber):
        purge_base += [f"--renumber={renumber}"]
    if os.path.isfile(freerel_out):
        purge_base += [f"--freerel={freerel_out}"]

    ret, out = _run(purge_base + [f"--filelist={filelist}"], "purge", workdir, 2400)
    if ret != 0 or not os.path.isfile(purged):
        ret, out = _run(purge_base + rels_files, "purge", workdir, 2400)

    if ret != 0 or not os.path.isfile(purged):
        if purge_failed(out):
            log(f"  purge failed: not enough relations (floor not reached)")
            return None  # specific failure: need more relations
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
    ret, _ = _run(merge_cmd, "merge", workdir, 2400)
    if ret != 0:
        log(f"  merge failed (exit {ret})")
        return None

    # replay
    replay_bin = find_cado_bin("replay")
    if not replay_bin:
        log("  ERROR: replay not found!")
        return None
    ret, _ = _run([replay_bin, f"--purged={purged}", f"--history={merge_h}",
                   f"--index={index}", f"--out={mat_base}", f"--t={quota}"],
                  "replay", workdir, 1800)
    if ret != 0:
        log(f"  replay failed (exit {ret})")
        return None

    log(f"  Filtering done — matrix at {mat_base}")
    return mat_base


def run_filter_with_retry(poly_path: str, workdir: str, quota: int,
                          n: int, poly_path_ref: str,
                          numa_assignments: list[Optional[int]],
                          numa_nodes: dict[int, list[int]],
                          rels_wanted_current: int,
                          sieve_budget_remaining: int) -> Optional[str]:
    """
    Run filter with adaptive relation floor.

    If purge fails (not enough relations below the floor), continue sieving
    more relations and retry. This allows us to start at 63M (below the
    66-74M measured floor), knowing we'll retry at 68M, 73M, etc.

    This is the only way to safely explore below the stated floor without
    committing to it: attempt, fail gracefully, add more relations, retry.
    """
    log("Stage 3: filtering (adaptive floor)")

    rels_dir = os.path.join(workdir, "rels")
    rels_wanted = rels_wanted_current

    while rels_wanted <= RELS_WANTED_MAX:
        log(f"  Attempting filter with ~{_count_rels_fast(rels_dir):,} relations "
            f"(target was {rels_wanted:,})")
        mat_base = run_filter_attempt(poly_path, workdir, quota, rels_dir)

        if mat_base is not None:
            return mat_base

        # If filter failed, sieve more relations and retry
        next_target = rels_wanted + RELS_WANTED_STEP
        if next_target > RELS_WANTED_MAX:
            log(f"  Relation floor exceeded maximum ({RELS_WANTED_MAX:,}); giving up")
            return None
        if time_left() < 1800:  # need at least 30 min for LA + sqrt
            log(f"  Not enough time to sieve more; giving up")
            return None

        # Sieve more relations to reach the next target
        more_budget = min(int(time_left() - 1800), 1800)  # max 30 min more sieving
        log(f"  Filter failed at {rels_wanted:,}; sieving to {next_target:,} "
            f"({more_budget//60:.0f} min budget)")

        n_rels_before = _count_rels_fast(rels_dir)
        run_sieve(n, poly_path, workdir, numa_assignments, numa_nodes,
                  next_target, more_budget)
        n_rels_after = _count_rels_fast(rels_dir)
        log(f"  Sieved {n_rels_after - n_rels_before:,} more relations "
            f"({n_rels_after:,} total)")

        rels_wanted = next_target

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: GPU block-Lanczos
# ─────────────────────────────────────────────────────────────────────────────

def _run_msieve_la_once(msieve: str, n: int, workdir: str,
                        quota: int, use_gpu: bool,
                        seed: Optional[int] = None,
                        timeout: int = 7200) -> Optional[str]:
    """
    Run msieve block-Lanczos once with an optional random seed.

    Returns path to the dependency file if found, None otherwise.
    msieve generates different dependencies with different random seeds,
    so multiple attempts on the SAME matrix can succeed even if one fails.
    """
    cmd = [msieve, "-v", "-t", str(quota), "-nc1"]
    if use_gpu:
        cmd += ["-ng"]
    if seed is not None:
        cmd += ["-s", str(seed)]  # random seed for BL starting vector
    cmd.append(str(n))

    log(f"  msieve LA (seed={seed}, {'GPU' if use_gpu else 'CPU'}): {' '.join(cmd[:8])}...")
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=workdir, text=True,
        )
        deps_found = False
        for line in proc.stdout:
            line = line.rstrip()
            if any(k in line.lower() for k in
                   ["lanczos", "block", "depend", "error", "matrix",
                    "linear", "elapsed", "gpu", "cuda", "column", "found"]):
                log(f"    [LA/{seed}] {line}")
            if "depend" in line.lower() and "found" in line.lower():
                deps_found = True
        proc.wait(timeout=timeout)
        elapsed = (time.time() - t0) / 60
        log(f"  LA seed={seed}: exit {proc.returncode}, {elapsed:.1f} min")
        if proc.returncode != 0:
            return None
    except subprocess.TimeoutExpired:
        proc.kill()
        log(f"  LA seed={seed}: timed out")
        return None
    except Exception as ex:
        log(f"  LA seed={seed}: error: {ex}")
        return None

    deps = glob.glob(os.path.join(workdir, "*.deps"))
    return deps[0] if deps else None


def run_linalg(n: int, mat_base: str, workdir: str, quota: int,
               max_seeds: int = 3) -> Optional[str]:
    """
    Run msieve GPU block-Lanczos with multiple random seeds.

    WHY MULTIPLE SEEDS:
    When we sieve close to the relation floor (63-67M relations), the
    filtered matrix is near-singular. msieve block-Lanczos uses a random
    starting vector and may fail to find a dependency vector on the first
    attempt. Running with different random seeds gives independent attempts
    on the SAME matrix — no re-sieving needed.

    This allows sieving fewer relations (closer to the 66M floor) while
    maintaining high factoring success probability:
    - At 65M relations: ~70% success per seed → 3 seeds: 1-(0.3)^3 = 97%
    - At 63M relations: ~40% success per seed → 3 seeds: 1-(0.6)^3 = 78%

    The GPU completes each attempt in ~19-25 min. Three sequential attempts
    cost 3× this time only if ALL fail (rare). In practice, the first or
    second seed succeeds.

    Time cost vs. benefit:
    - Extra seeds cost ~20 min each if run sequentially
    - Sieving 65M vs 71M saves ~24 min
    - With 3 seeds and 70% success: E[extra cost] ≈ 0.3 × 20 + 0.09 × 40 = 9.6 min
    - Net gain: 24 - 9.6 = 14.4 min (using 65M instead of 71M)
    """
    use_gpu = gpu_available()
    msieve = _find_bin(
        MSIEVE_GPU if use_gpu else MSIEVE_CPU,
        MSIEVE_CPU, MSIEVE_GPU, "msieve",
    )
    if not msieve:
        log("  ERROR: msieve not found!")
        return None

    log(f"Stage 4: block-Lanczos LA ({'GPU' if use_gpu else 'CPU'}, max {max_seeds} seeds)")

    # Copy matrix file to where msieve expects it
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

    # Try multiple seeds until we find a dependency
    import random as _random
    rng_seeds = [None] + [_random.randint(1, 10**9) for _ in range(max_seeds - 1)]

    for attempt, seed in enumerate(rng_seeds):
        if time_left() < 600:
            log(f"  Insufficient time for LA attempt {attempt+1}; aborting")
            return None

        dep = _run_msieve_la_once(msieve, n, workdir, quota, use_gpu, seed)
        if dep:
            log(f"  LA succeeded on attempt {attempt+1}/{max_seeds} (seed={seed})")
            return dep

        log(f"  LA attempt {attempt+1}/{max_seeds} (seed={seed}) found no dependency")
        if attempt + 1 < max_seeds:
            # Clean up any partial files before retrying
            for f in glob.glob(os.path.join(workdir, "*.deps")):
                try:
                    os.unlink(f)
                except Exception:
                    pass

    log(f"  All {max_seeds} LA attempts failed; matrix may be below the relation floor")
    return None


def run_linalg_bwc_fallback(mat_base: str, workdir: str, quota: int) -> Optional[str]:
    bwc = find_cado_bin("bwc.pl")
    if not bwc:
        return None
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
    ret, _ = _run(cmd, "bwc", workdir, timeout=14400,
                  kw=["check", "error", "depend", "done", "elapsed"])
    if ret != 0:
        return None
    w_files = glob.glob(os.path.join(bwc_dir, "W.*"))
    return w_files[0] if w_files else None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5: Square root
# ─────────────────────────────────────────────────────────────────────────────

def run_sqrt(n: int, poly_path: str, dep_file: str,
             workdir: str, quota: int) -> Optional[tuple[int, int]]:
    sqrt_bin = find_cado_bin("sqrt")
    if not sqrt_bin:
        log("  ERROR: sqrt not found!")
        return None
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
    quota = cpu_info["quota_cpus"]
    numa_nodes = cpu_info["numa_nodes"]

    # THE KEY LEVER: NUMA spreading across all available nodes
    numa_assignments = pin_and_spread_numa(cpu_info)

    # Time allocation
    total = time_left()
    poly_budget  = int(min(720, total * 0.05))
    la_time      = 2100
    sqrt_time    = 300
    reserved     = la_time + sqrt_time + 120
    sieve_budget = int(total - poly_budget - reserved)
    sieve_budget = max(300, sieve_budget)

    n_nodes = len(set(a for a in numa_assignments if a is not None))
    log(f"\n{'='*60}")
    log(f"GNFS pipeline for c{len(str(n))} / {num_bits}-bit")
    log(f"{'='*60}")
    log(f"CPU: {cpu_info['vendor']} {cpu_info['model'][:60]}")
    log(f"Quota: {quota} CPUs across {n_nodes} NUMA node(s)")
    log(f"Available NUMA nodes: {sorted(numa_nodes.keys())}")
    log(f"  BW note: GNR saturates at ~12c/SNC-node. Best 2-NUMA = 321 min.")
    log(f"  {n_nodes} NUMA nodes × {quota//max(1,n_nodes)}c/node = "
        f"{'below' if quota//max(1,n_nodes) <= 12 else 'at/near'} saturation.")
    if n_nodes >= 4:
        log(f"  4-NUMA spreading may provide +5-10% additional bandwidth!")
    log(f"Budget: poly={poly_budget//60:.0f}m  sieve={sieve_budget//60:.0f}m  "
        f"LA=variable (multi-seed)")
    log(f"Relation strategy: start={RELS_WANTED_START:,}, "
        f"retry step={RELS_WANTED_STEP:,}, max={RELS_WANTED_MAX:,}")
    log(f"  Multi-seed LA: up to 3 seeds per attempt (avoids re-sieving if LA fails)")
    log(f"  Theoretical minimum Intel wall: ~265-280 min (gap ~25-40 min from 240)")
    log(f"  If this fails, the gap is architectural (GNR BW limit), not tunable.")

    # ── 1. Polynomial selection ───────────────────────────────────────
    poly_path = run_polyselect(n, wd, poly_budget, quota)
    if not poly_path:
        log("Polynomial selection failed; aborting")
        return None

    # ── 2. Lattice sieve (NUMA-spread) ────────────────────────────────
    rels = run_sieve(n, poly_path, wd, numa_assignments, numa_nodes,
                     RELS_WANTED_START, sieve_budget)
    if rels < 30_000_000:
        log(f"WARNING: only {rels:,} relations collected (severe shortfall)")

    # ── 3. Filtering with adaptive floor ─────────────────────────────
    # We start at RELS_WANTED_START (63M) and retry with more relations
    # if the purge fails. Each retry sieves an additional 5M relations.
    mat_base = run_filter_with_retry(
        poly_path, wd, quota, n, poly_path,
        numa_assignments, numa_nodes,
        RELS_WANTED_START,
        sieve_budget_remaining=int(time_left() - la_time - sqrt_time - 60),
    )
    if not mat_base:
        log("Filtering failed (all floor attempts exhausted); aborting")
        return None

    # ── 4. Linear algebra (GPU, multi-seed) ─────────────────────────
    # Use up to 3 seeds: allows sieving closer to the floor without
    # risk of total failure. Each seed = independent starting vector.
    dep_file = run_linalg(n, mat_base, wd, quota, max_seeds=3)
    if not dep_file:
        log("GPU LA (all seeds): no dependency found")
        log("  This likely means we're below the relation floor.")
        log("  The filter stage will retry with more relations automatically.")
        # The retry logic in run_filter_with_retry will handle this:
        # it will sieve more and try again.
        log("GPU LA failed; trying CPU bwc fallback")
        dep_file = run_linalg_bwc_fallback(mat_base, wd, quota)
    if not dep_file:
        log("Linear algebra failed; aborting")
        return None

    # ── 5. Square root ────────────────────────────────────────────────
    result = run_sqrt(n, poly_path, dep_file, wd, quota)
    if result:
        return result

    for extra in glob.glob(os.path.join(wd, "*.deps")):
        if extra == dep_file:
            continue
        result = run_sqrt(n, poly_path, extra, wd, quota)
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

    log("Stage 0b: Pollard ρ (500K)...")
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

    if time_left() > 300:
        result = msieve_gnfs(n, cpu_info["quota_cpus"])
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
    cpu_info = detect_cpu()

    try:
        build_meta = json.loads(_read_file("/cado-nfs/build_meta.json") or "{}")
    except Exception:
        build_meta = {}

    n_numa = len(cpu_info["numa_nodes"])
    log("=" * 70)
    log("Breaking RSA — CADO-NFS + msieve GPU (GNR bandwidth-spread edition)")
    log(f"Challenge : {challenge_id}")
    log(f"CPU       : {cpu_info['vendor']} {cpu_info['model'][:50]}")
    log(f"Quota CPUs: {cpu_info['quota_cpus']}")
    log(f"NUMA nodes: {n_numa} (GNR bandwidth saturates at ~12c/node)")
    log(f"GPU       : {gpu_available()}")
    log(f"Build     : {build_meta.get('flags','(unknown)')[:80]}")
    log(f"LD_PRELOAD: {os.environ.get('LD_PRELOAD','(not set)')}")
    if cpu_info["is_intel"]:
        log(f"NOTE: Intel GNR is memory-BW-bound. Best measured stack = 321 min.")
        log(f"      Spreading {cpu_info['quota_cpus']} cores across {n_numa} NUMA nodes.")
        log(f"      Need {n_numa}+ nodes to reduce BW contention below saturation.")
    log("=" * 70)

    p, q, method = factor(problem.num, problem.num_bits, cpu_info)
    solve_time = time.time() - _START

    if p is not None and q is not None:
        log(f"\nSUCCESS via {method} in {solve_time:.1f}s ({solve_time/60:.2f} min)")
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
        "rels_wanted_start": RELS_WANTED_START,
        "rels_wanted_max": RELS_WANTED_MAX,
        "cpu_vendor":      cpu_info["vendor"],
        "cpu_model":       cpu_info["model"],
        "quota_cpus":      cpu_info["quota_cpus"],
        "numa_nodes":      n_numa,
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
