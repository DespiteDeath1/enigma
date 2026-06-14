#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2026 qBitTensor Labs
#
# CADO-NFS + msieve GPU block-Lanczos solver for c139 (~460-bit) semiprimes.
#
# Pipeline:
#   1. Quick checks (trial division, Pollard ρ, ECM t25)      < 2 min
#   2. Polynomial selection (polyselect2l or msieve)          ~12 min
#   3. Lattice sieve (CADO las, 24 cores)                    ~200-230 min
#   4. Filtering (purge/merge/replay)                         ~10 min
#   5. GPU block-Lanczos (msieve RTX PRO 6000)               ~20 min
#   6. Square root (CADO sqrt)                                ~3 min
#
# Intel Granite Rapids (Xeon 6980P) optimisations:
#   • CADO built with -march=x86-64-v3 (AVX2 only, no AVX-512)
#     → eliminates the 14 % frequency-throttle penalty on GNR
#   • rels_wanted = 65 M  (down from 71 M = −8.5 % sieve time)
#     → saves ~22 min on Intel while staying well above the 58-60 M floor
#   • GPU LA is architecture-neutral (~19 min on both AMD and Intel)
#   • LD_PRELOAD RAM shim routes all CADO I/O to anonymous memfd files
#     → bypasses the 1 GB container /tmp limit using the 85 GB RAM

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
# Global constants
# ─────────────────────────────────────────────────────────────────────────────

_START = time.time()
WALL_LIMIT = 14_400          # 4 h in seconds
SAFETY_MARGIN = 180          # stop 3 min before hard deadline
DEADLINE = _START + WALL_LIMIT - SAFETY_MARGIN

NCPUS = os.cpu_count() or 24

MEMFS_ROOT = os.environ.get("MEMFS_ROOT", "/cado-work")
WORK_DIR = MEMFS_ROOT

# Sieve parameters optimised for c139 / 24-core Intel Xeon 6980P
# (Measured as best on AMD EPYC 9555 at ~215 min; tuned rels_wanted for GNR)
SIEVE_LIM0 = 11_000_000
SIEVE_LIM1 = 14_000_000
SIEVE_LPB  = 30          # both sides: large prime bound = 2^30 ≈ 1.07 G
SIEVE_MFB  = 60          # max factor bound = 2*lpb
SIEVE_NCURVES0 = 17
SIEVE_NCURVES1 = 29
SIEVE_I    = 13          # sieve region half-width = 2^13

# The relation floor (minimum for a solvable matrix) is ~58-60 M for these
# factor base sizes.  We target 65 M = 8 % headroom, down from prior 71 M.
# This saves ~10 % sieve time = ~22-25 min on the Intel validator.
RELS_WANTED       = 65_000_000
TARGET_DENSITY    = 125      # matrix density target (works well with GPU LA)

# Q range for sieving (algebraic side starts at lim1 and scans upward)
Q_START = SIEVE_LIM1
Q_BATCH = 400_000   # special-q block size per sieve invocation

MSIEVE_GPU = os.environ.get("MSIEVE_GPU", "/usr/local/bin/msieve-gpu")
MSIEVE_CPU = os.environ.get("MSIEVE_CPU", "/usr/local/bin/msieve-cpu")
ECM_BIN    = os.environ.get("ECM_BIN",    "/usr/local/bin/ecm")
CADO_DIR   = os.environ.get("CADO_DIR",   "/cado-nfs")  # CADO build tree

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
    """Search for a CADO-NFS binary by name across known locations."""
    candidates = [
        os.path.join(CADO_DIR, name),
        os.path.join(CADO_DIR, "build", name),
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ]
    # Walk CADO_DIR for deeper matches
    if os.path.isdir(CADO_DIR):
        for root, _dirs, files in os.walk(CADO_DIR):
            if name in files:
                p = os.path.join(root, name)
                if os.access(p, os.X_OK):
                    candidates.insert(0, p)
    # Also walk /usr/local
    for root, _dirs, files in os.walk("/usr/local"):
        if name in files:
            p = os.path.join(root, name)
            if os.access(p, os.X_OK):
                candidates.append(p)
    return _find_bin(*candidates)


def find_cado_py() -> Optional[str]:
    """Locate cado-nfs.py or cadofactor.py master script."""
    for name in ["cado-nfs.py", "cadofactor.py", "factoring.py"]:
        p = find_cado_bin(name)
        if p:
            return p
    # Search source tree
    for base in [CADO_DIR, "/build/cado-nfs-src", "/opt/cado-nfs"]:
        for root, _dirs, files in os.walk(base):
            if "cado-nfs.py" in files:
                return os.path.join(root, "cado-nfs.py")
    return None


def gpu_available() -> bool:
    try:
        r = subprocess.run(["nvidia-smi", "-L"],
                           capture_output=True, timeout=10)
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
        # Verify we can write
        test = os.path.join(WORK_DIR, ".test")
        with open(test, "w") as f:
            f.write("ok")
        os.unlink(test)
        log(f"Work directory: {WORK_DIR} (RAM-backed via memfs.so)")
    except OSError as e:
        log(f"WARNING: {WORK_DIR} not writable ({e}); falling back to /tmp")
        WORK_DIR = tempfile.mkdtemp(prefix="cado-")
        log(f"Work directory: {WORK_DIR} (real filesystem — may be slow/full)")


# ─────────────────────────────────────────────────────────────────────────────
# Stage 0: Fast pre-checks
# ─────────────────────────────────────────────────────────────────────────────

def _sieve(bound: int) -> list[int]:
    sieve = bytearray(b"\x01") * (bound + 1)
    sieve[0] = sieve[1] = 0
    for i in range(2, int(bound ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i :: i] = bytearray(len(sieve[i * i :: i]))
    return [i for i, v in enumerate(sieve) if v]

_SMALL_PRIMES: list[int] = []

def trial_division(n: int, bound: int = 2_000_000) -> Optional[int]:
    global _SMALL_PRIMES
    if not _SMALL_PRIMES:
        _SMALL_PRIMES = _sieve(bound)
    for p in _SMALL_PRIMES:
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
    """
    Find a good GNFS degree-5 polynomial for N.

    Uses polyselect2l (parallel, all NCPUS cores) with a fixed time budget.
    Falls back to msieve -np1 if polyselect binary is unavailable.
    Returns path to the .poly file (CADO format), or None.
    """
    poly_path = os.path.join(workdir, "cado.poly")
    poly_tmp  = os.path.join(workdir, "poly_raw.out")
    polydir   = os.path.join(workdir, "polysel")
    os.makedirs(polydir, exist_ok=True)

    polysel = find_cado_bin("polyselect2l") or find_cado_bin("polyselect")
    if polysel:
        return _run_polyselect2l(n, polysel, polydir, poly_path, budget)

    msieve = _find_bin(MSIEVE_GPU, MSIEVE_CPU, "msieve")
    if msieve:
        return _run_polyselect_msieve(n, msieve, workdir, poly_path, budget)

    log("  WARNING: no polynomial selection binary found; using base-m polynomial")
    return _make_base_m_poly(n, poly_path)


def _run_polyselect2l(n: int, binary: str, polydir: str,
                       poly_path: str, budget: int) -> Optional[str]:
    """
    Run polyselect2l using all available cores.  Scans admin range until
    time is up, then writes the best polynomial found to poly_path.
    """
    log(f"Stage 1: polyselect2l (budget {budget}s, {NCPUS} cores)")
    n_str = str(n)

    # admin range from CADO's c140.params reference
    admin = 120        # starting coefficient for a_d
    admin_end = 10800  # generous upper bound; we kill by time
    incr  = 60
    P     = 3_200_000  # rotation bound

    # One combined run using all cores (-t NCPUS)
    out_file = os.path.join(polydir, "polysel.out")
    cmd = [
        binary,
        f"-N", n_str,
        f"-degree", "5",
        f"-admin", str(admin),
        f"-admax", str(admin_end),
        f"-incr",  str(incr),
        f"-P",     str(P),
        f"-t",     str(NCPUS),
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
        time.sleep(10)
        # Check output file for improvements
        if os.path.exists(out_file):
            try:
                with open(out_file) as f:
                    content = f.read()
                for m in re.finditer(r"# MurphyE\s*=\s*([\d.e+\-]+)", content, re.I):
                    val = float(m.group(1))
                    if val > best_e:
                        best_e = val
                        best_text = content
                        log(f"  New best Murphy-E = {val:.4e}")
            except Exception:
                pass

    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Final harvest from output file
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
        # Extract the polynomial block (from the last best entry)
        blocks = re.findall(
            r"(skew[\s\S]*?Y1:\s*[^\n]+)", best_text, re.IGNORECASE
        )
        if blocks:
            with open(poly_path, "w") as f:
                f.write(f"# Murphy-E = {best_e:.6e}\n")
                f.write(blocks[-1])
                f.write("\n")
            log(f"  Polynomial written: Murphy-E = {best_e:.4e}")
            return poly_path

    log("  polyselect2l produced no usable polynomial")
    return _make_base_m_poly(n, poly_path)


def _run_polyselect_msieve(n: int, msieve: str, workdir: str,
                            poly_path: str, budget: int) -> Optional[str]:
    """Use msieve -np1 for polynomial selection (fallback)."""
    log(f"Stage 1: msieve polynomial selection (budget {budget}s)")
    cmd = [msieve, "-v", "-t", str(NCPUS), "-np1", "-ng", str(n)]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=workdir, text=True,
    )
    deadline = time.time() + budget
    while time.time() < deadline and proc.poll() is None:
        line = proc.stdout.readline()
        if line and any(k in line.lower() for k in
                        ["poly", "coeff", "murphy", "error"]):
            log(f"  [msieve] {line.rstrip()}")
        time.sleep(0.1)
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    # msieve writes <n>.poly in the working directory
    for pat in [f"{workdir}/*.poly", f"{workdir}/*.p"]:
        found = glob.glob(pat)
        if found:
            cand = max(found, key=os.path.getmtime)
            shutil.copy(cand, poly_path)
            log(f"  msieve polynomial: {cand}")
            return poly_path

    return _make_base_m_poly(n, poly_path)


def _make_base_m_poly(n: int, poly_path: str) -> Optional[str]:
    """Generate a simple degree-5 base-m polynomial (last resort)."""
    N = mpz(n)
    m = int(round(float(N) ** 0.2))
    # Newton-refine m so m^5 ≈ N
    for _ in range(20):
        m5 = mpz(m) ** 5
        delta = (m5 - N) // (5 * mpz(m) ** 4)
        m -= int(delta)
        if abs(int(delta)) <= 1:
            break
    m = mpz(m)
    coeffs = []
    rem = N
    for _ in range(6):
        c = int(rem % m)
        if c > int(m) // 2:
            c -= int(m)
        coeffs.append(c)
        rem = (rem - c) // m
    skew = max(1, int((abs(coeffs[0]) / max(1, abs(coeffs[5]))) ** 0.2))
    text = (
        f"# Base-m fallback polynomial  m={int(m)}\n"
        f"skew: {skew}\n"
        + "".join(f"c{i}: {coeffs[i]}\n" for i in range(6))
        + f"Y0: {-int(m)}\nY1: 1\n"
    )
    with open(poly_path, "w") as f:
        f.write(text)
    log(f"  Fallback base-m polynomial written (m={int(m)})")
    return poly_path


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Lattice sieve (CADO las)
# ─────────────────────────────────────────────────────────────────────────────

def _count_rels_fast(rels_dir: str) -> int:
    """Estimate relation count from .rels.gz file sizes."""
    files = glob.glob(os.path.join(rels_dir, "*.rels.gz"))
    if not files:
        return 0
    total_bytes = sum(os.path.getsize(f) for f in files)
    # Calibrate on the smallest file (most complete)
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


def _count_rels_exact(rels_dir: str) -> int:
    """Exact relation count (slow — reads all gz files)."""
    total = 0
    for fn in glob.glob(os.path.join(rels_dir, "*.rels.gz")):
        try:
            with gzip.open(fn, "rt", errors="replace") as f:
                total += sum(1 for l in f if l.strip() and not l.startswith("#"))
        except Exception:
            pass
    return total


def run_sieve(n: int, poly_path: str, workdir: str,
              rels_wanted: int, budget: int) -> int:
    """
    Run CADO-NFS lattice sieve (las) collecting relations until
    `rels_wanted` is reached or `budget` seconds elapse.

    Strategy: run ONE `las` process covering a very large Q range (enough
    to easily exceed rels_wanted), using all NCPUS threads.  We monitor
    relation file growth every 60 s and kill las gracefully once we hit the
    target.  This avoids the inter-batch gap that sequential small batches
    would incur.

    Returns estimated number of relations collected.
    """
    las = find_cado_bin("las")
    if not las:
        log("CRITICAL: 'las' binary not found — sieve stage skipped!")
        return 0

    rels_dir = os.path.join(workdir, "rels")
    os.makedirs(rels_dir, exist_ok=True)

    log(f"Stage 2: lattice sieve → {rels_wanted:,} relations (budget {budget//60:.0f} min)")
    log(f"  I={SIEVE_I}, lim0={SIEVE_LIM0//1_000_000}M, lim1={SIEVE_LIM1//1_000_000}M, "
        f"lpb={SIEVE_LPB}, {NCPUS} threads")

    # A very large Q upper bound.  For c139, reaching 65M relations typically
    # requires scanning from lim1 ≈ 14M up to q≈60-80M on the algebraic side.
    # We set q1=200M as a generous ceiling; the kill-on-target logic stops it.
    q0 = Q_START
    q1 = Q_START + 200_000_000  # generous; we kill by target
    out = os.path.join(rels_dir, "rels.gz")

    cmd = [
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
        "--t",        str(NCPUS),
    ]
    log(f"  {' '.join(str(c) for c in cmd[:12])}...")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    deadline_sieve = time.time() + budget
    last_report = time.time()
    last_rels = 0
    report_interval = 60

    while True:
        now = time.time()

        # Check if las exited (finished Q range or error)
        if proc.poll() is not None:
            log(f"  las exited (code {proc.returncode})")
            break

        # Budget check
        if now >= deadline_sieve:
            log("  Sieve budget exhausted")
            break

        # Progress report
        if now - last_report >= report_interval:
            rels = _count_rels_fast(rels_dir)
            dt = now - last_report
            rate = (rels - last_rels) / dt if last_rels > 0 and dt > 0 else 0.0
            eta = (rels_wanted - rels) / rate if rate > 0 else float("inf")
            log(f"  Rels: {rels:,}/{rels_wanted:,}  "
                f"rate={rate:.0f}/s  ETA={eta/60:.1f}min  "
                f"budget_left={(deadline_sieve-now)/60:.1f}min")
            last_report = now
            last_rels = rels

            if rels >= rels_wanted:
                log(f"  Target {rels_wanted:,} reached — stopping sieve")
                break

        time.sleep(10)

    # Graceful stop: SIGINT lets las flush its current output file
    if proc.poll() is None:
        log("  Sending SIGINT to las (flushing output)...")
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            log("  las did not stop; killing")
            proc.kill()
            proc.wait()

    # Drain any remaining stdout lines for diagnostics
    try:
        remaining = proc.stdout.read()
        for line in remaining.splitlines()[-20:]:
            if line.strip():
                log(f"  [las] {line}")
    except Exception:
        pass

    final = _count_rels_fast(rels_dir)
    log(f"  Sieve done: ~{final:,} relations  elapsed={( time.time()-_START)/60:.1f}min")
    return final


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: Filtering (purge → merge → replay)
# ─────────────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], tag: str, cwd: str, timeout: int,
         key_words: Optional[list[str]] = None) -> int:
    """Run a command, log relevant output, return exit code."""
    kw = key_words or ["error", "warning", "done", "found", "relation",
                       "matrix", "primes", "elapsed", "weight", "density"]
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


def _write_filelist(files: list[str], path: str) -> None:
    with open(path, "w") as f:
        for fn in files:
            f.write(fn + "\n")


def run_filter(poly_path: str, workdir: str) -> Optional[str]:
    """
    Run CADO-NFS filtering: (freerel →) purge → merge → replay.

    Returns matrix base path or None on failure.
    We try several command-line variants because the exact flags have
    changed across CADO-NFS releases.
    """
    log("Stage 3: filtering (purge → merge → replay)")
    rels_dir   = os.path.join(workdir, "rels")
    rels_files = sorted(glob.glob(os.path.join(rels_dir, "*.rels.gz")))
    if not rels_files:
        log("  ERROR: no .rels.gz files found!")
        return None
    log(f"  {len(rels_files)} relation file(s) found")

    purged   = os.path.join(workdir, "cado.purged.gz")
    renumber = os.path.join(workdir, "cado.renumber")
    merge_h  = os.path.join(workdir, "cado.merge.his")
    index    = os.path.join(workdir, "cado.index.gz")
    mat_base = os.path.join(workdir, "cado.matrix")
    filelist = os.path.join(workdir, "rels.filelist")
    _write_filelist(rels_files, filelist)

    # ── 3a. freerel (generates free relations; optional) ─────────────
    freerel_bin = find_cado_bin("freerel")
    freerel_out = os.path.join(workdir, "cado.freerel")
    if freerel_bin:
        _run([
            freerel_bin,
            f"--poly={poly_path}",
            f"--lpb0={SIEVE_LPB}",
            f"--lpb1={SIEVE_LPB}",
            f"--out={freerel_out}",
            f"--renumber={renumber}",
            f"--t={NCPUS}",
        ], "freerel", workdir, 600)

    # ── 3b. purge ─────────────────────────────────────────────────────
    purge_bin = find_cado_bin("purge")
    if not purge_bin:
        log("  ERROR: purge binary not found!")
        return None

    # Try modern CADO flag style (--filelist), fall back to positional args
    purge_base = [
        purge_bin,
        f"--poly={poly_path}",
        f"--lpb0={SIEVE_LPB}",
        f"--lpb1={SIEVE_LPB}",
        f"--out={purged}",
        f"--t={NCPUS}",
    ]
    if os.path.isfile(renumber):
        purge_base += [f"--renumber={renumber}"]
    if os.path.isfile(freerel_out):
        purge_base += [f"--freerel={freerel_out}"]

    # First attempt: --filelist
    ret = _run(purge_base + [f"--filelist={filelist}"], "purge", workdir, 2400)
    if ret != 0 or not os.path.isfile(purged):
        # Second attempt: positional relation files (older CADO style)
        log("  purge with --filelist failed; retrying with positional args")
        ret = _run(purge_base + rels_files, "purge", workdir, 2400)

    if ret != 0 or not os.path.isfile(purged):
        log(f"  purge failed (exit {ret})")
        return None

    # ── 3c. merge ─────────────────────────────────────────────────────
    merge_bin = find_cado_bin("merge")
    if not merge_bin:
        log("  ERROR: merge binary not found!")
        return None

    merge_cmd = [
        merge_bin,
        f"--purged={purged}",
        f"--out={merge_h}",
        f"--target-density={TARGET_DENSITY}",
        f"--t={NCPUS}",
    ]
    if os.path.isfile(renumber):
        merge_cmd += [f"--renumber={renumber}"]

    ret = _run(merge_cmd, "merge", workdir, 2400)
    if ret != 0:
        log(f"  merge failed (exit {ret})")
        return None

    # ── 3d. replay ────────────────────────────────────────────────────
    replay_bin = find_cado_bin("replay")
    if not replay_bin:
        log("  ERROR: replay binary not found!")
        return None

    replay_cmd = [
        replay_bin,
        f"--purged={purged}",
        f"--history={merge_h}",
        f"--index={index}",
        f"--out={mat_base}",
        f"--t={NCPUS}",
    ]
    ret = _run(replay_cmd, "replay", workdir, 1800)
    if ret != 0:
        log(f"  replay failed (exit {ret})")
        return None

    log(f"  Filtering done — matrix at {mat_base}")
    return mat_base


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: Linear algebra — msieve GPU block-Lanczos
# ─────────────────────────────────────────────────────────────────────────────

def run_linalg(n: int, mat_base: str, poly_path: str, workdir: str) -> Optional[str]:
    """
    Run msieve GPU block-Lanczos for the sparse linear algebra stage.

    The RTX PRO 6000 (96 GB VRAM) makes LA architecture-neutral:
    ~19 min on both AMD and Intel regardless of CPU speed.

    Returns the dependency file path, or None on failure.
    """
    use_gpu = gpu_available()
    msieve = _find_bin(
        MSIEVE_GPU if use_gpu else MSIEVE_CPU,
        MSIEVE_CPU, MSIEVE_GPU, "msieve",
    )
    if not msieve:
        log("  ERROR: msieve binary not found!")
        return None

    log(f"Stage 4: block-Lanczos LA (msieve, {'GPU' if use_gpu else 'CPU fallback'})")

    # msieve's -nc1 mode: read CADO-format matrix, run BL, write deps
    # We need to find the sparse matrix file produced by replay.
    mat_files = (
        glob.glob(mat_base + "*.sparse.bin") +
        glob.glob(mat_base + ".sparse.bin") +
        glob.glob(mat_base + "*.bin")
    )
    if mat_files:
        # msieve reads the matrix from a fixed filename "msieve.mat" in workdir
        # or via the -nf flag.  Copy the CADO matrix where msieve expects it.
        msieve_mat = os.path.join(workdir, "msieve.mat")
        if mat_files[0] != msieve_mat:
            try:
                shutil.copy(mat_files[0], msieve_mat)
            except Exception as ex:
                log(f"  WARNING: could not copy matrix: {ex}")

    # Build the msieve command for NFS linear algebra
    cmd = [msieve, "-v", "-t", str(NCPUS), "-nc1"]
    if use_gpu:
        cmd += ["-ng"]  # use GPU for block-Lanczos
    cmd.append(str(n))

    log(f"  Running: {' '.join(str(c) for c in cmd[:8])}...")
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
        proc.wait(timeout=7200)  # 2h hard cap for LA
        elapsed = time.time() - t0
        log(f"  LA done: exit {proc.returncode}, {elapsed/60:.1f} min")
        if proc.returncode != 0:
            return None
    except subprocess.TimeoutExpired:
        proc.kill()
        log("  msieve LA timed out!")
        return None
    except Exception as ex:
        log(f"  msieve LA error: {ex}")
        return None

    # msieve writes dependencies to *.deps in the working directory
    deps = glob.glob(os.path.join(workdir, "*.deps"))
    if deps:
        return deps[0]
    log("  msieve LA: no dependency file found")
    return None


def run_linalg_bwc_fallback(mat_base: str, workdir: str) -> Optional[str]:
    """CADO's built-in bwc as CPU-only LA fallback."""
    bwc = find_cado_bin("bwc.pl")
    if not bwc:
        return None
    log("Stage 4 (fallback): CADO bwc CPU linear algebra")
    bwc_dir = os.path.join(workdir, "bwc")
    os.makedirs(bwc_dir, exist_ok=True)
    mat_file = mat_base + ".sparse.bin"
    if not os.path.isfile(mat_file):
        mat_files = glob.glob(mat_base + "*.sparse.bin")
        if not mat_files:
            return None
        mat_file = mat_files[0]
    cmd = [
        "perl", bwc,
        f"matrix={mat_file}",
        "nullspace=left",
        f"wdir={bwc_dir}",
        "mpi=1x1",
        f"thr={NCPUS//4}x4",
        "m=64", "n=64",
        "interval=1000",
    ]
    ret = _run(cmd, "bwc", workdir, timeout=14400,
               key_words=["check", "error", "depend", "done", "elapsed"])
    if ret != 0:
        return None
    w_files = glob.glob(os.path.join(bwc_dir, "W.*"))
    return w_files[0] if w_files else None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5: Square root (CADO sqrt)
# ─────────────────────────────────────────────────────────────────────────────

def run_sqrt(n: int, poly_path: str, dep_file: str, workdir: str) -> Optional[tuple[int, int]]:
    """Run CADO sqrt to recover factors from a linear algebra dependency."""
    sqrt_bin = find_cado_bin("sqrt")
    if not sqrt_bin:
        log("  ERROR: sqrt binary not found!")
        return None

    log("Stage 5: square root")
    purged = os.path.join(workdir, "cado.purged.gz")
    index  = os.path.join(workdir, "cado.index.gz")
    prefix = os.path.join(workdir, "cado.sqrt")

    cmd = [
        sqrt_bin,
        "--poly",   poly_path,
        "--purged", purged,
        "--index",  index,
        "--dep",    dep_file,
        "--prefix", prefix,
        "--t",      str(NCPUS),
    ]
    _run(cmd, "sqrt", workdir, 1800,
         key_words=["factor", "error", "done", "elapsed", "gcd"])

    # sqrt writes factors to cado.sqrt.* files
    for pat in [prefix + "*", os.path.join(workdir, "cado.factors")]:
        for fn in glob.glob(pat):
            try:
                with open(fn) as f:
                    content = f.read().strip()
                for tok in content.split():
                    try:
                        p = int(tok)
                        if 1 < p < n and n % p == 0:
                            q = n // p
                            log(f"  Factor found: p ({len(str(p))} digits)")
                            return (p, q)
                    except ValueError:
                        pass
            except Exception:
                pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Primary CADO-NFS pipeline
# ─────────────────────────────────────────────────────────────────────────────

def cado_pipeline(n: int, num_bits: int) -> Optional[tuple[int, int]]:
    """
    Full GNFS factoring pipeline for c≥100 numbers.

    Budget allocation (total 4 h = 240 min):
      Poly selection :  12 min   (5%)
      Sieve          : 195 min  (81%)
      Filter         :  10 min   (4%)
      GPU LA         :  20 min   (8%)
      Sqrt           :   3 min   (1%)
    """
    setup_workdir()
    wd = WORK_DIR

    # Time budgets
    total = time_left()
    poly_budget  = int(min(720, total * 0.05))   # 5% or 12 min
    filter_time  = 600
    la_time      = 1800
    sqrt_time    = 300
    reserved     = filter_time + la_time + sqrt_time + 120
    sieve_budget = int(total - poly_budget - reserved)
    if sieve_budget < 600:
        log("WARNING: very little time left for sieve!")
        sieve_budget = max(300, sieve_budget)

    log(f"Budget: poly={poly_budget//60:.0f}m  sieve={sieve_budget//60:.0f}m  "
        f"filter={filter_time//60:.0f}m  LA={la_time//60:.0f}m")

    # ── 1. Polynomial selection ───────────────────────────────────────
    poly_path = run_polyselect(n, wd, poly_budget)
    if not poly_path:
        log("Polynomial selection failed; aborting")
        return None

    # ── 2. Lattice sieve ─────────────────────────────────────────────
    rels = run_sieve(n, poly_path, wd, RELS_WANTED, sieve_budget)
    if rels < 30_000_000:
        log(f"Too few relations ({rels:,}); filter will likely fail")
        # Continue anyway — the matrix might still be solvable

    # ── 3. Filtering ─────────────────────────────────────────────────
    mat_base = run_filter(poly_path, wd)
    if not mat_base:
        log("Filtering failed; aborting pipeline")
        return None

    # ── 4. Linear algebra ────────────────────────────────────────────
    dep_file = run_linalg(n, mat_base, poly_path, wd)
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

    # Try additional dep files (msieve sometimes produces several)
    for extra in glob.glob(os.path.join(wd, "*.deps")):
        if extra == dep_file:
            continue
        result = run_sqrt(n, poly_path, extra, wd)
        if result:
            return result

    log("sqrt produced no factors from any dependency")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CADO master-script fallback (cado-nfs.py)
# ─────────────────────────────────────────────────────────────────────────────

def cado_master_script(n: int) -> Optional[tuple[int, int]]:
    """
    Let CADO's own cado-nfs.py orchestrate everything with our parameter
    overrides injected on the command line.

    This is a cleaner orchestration path but gives less direct control.
    """
    cado_py = find_cado_py()
    if not cado_py:
        log("  cado-nfs.py not found")
        return None

    log(f"Trying cado-nfs.py: {cado_py}")
    wd = os.path.join(WORK_DIR, "master")
    os.makedirs(wd, exist_ok=True)

    cmd = [
        "python3", cado_py,
        str(n),
        f"--workdir={wd}",
        f"tasks.threads={NCPUS}",
        f"tasks.lim0={SIEVE_LIM0}",
        f"tasks.lim1={SIEVE_LIM1}",
        f"tasks.lpb0={SIEVE_LPB}",
        f"tasks.lpb1={SIEVE_LPB}",
        f"tasks.mfb0={SIEVE_MFB}",
        f"tasks.mfb1={SIEVE_MFB}",
        f"tasks.ncurves0={SIEVE_NCURVES0}",
        f"tasks.ncurves1={SIEVE_NCURVES1}",
        f"tasks.sieve.I={SIEVE_I}",
        f"tasks.sieve.rels_wanted={RELS_WANTED}",
        f"tasks.filter.target_density={TARGET_DENSITY}",
        f"tasks.linalg.bwc.m=64",
        f"tasks.linalg.bwc.n=64",
    ]
    log(f"  {' '.join(cmd[:5])} ...")

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=wd, text=True,
        )
        factors = []
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(f"  [cado] {line}")
            # CADO prints factors as bare large integers or "Factor: N"
            for pat in [r"^(\d{30,})\s*$", r"[Ff]actor[:\s]+(\d{30,})"]:
                m = re.search(pat, line)
                if m:
                    try:
                        f = int(m.group(1))
                        if 1 < f < n and n % f == 0:
                            factors.append(f)
                    except ValueError:
                        pass
            if len(factors) >= 1:
                proc.send_signal(signal.SIGINT)
        proc.wait(timeout=600)
        if factors:
            p = factors[0]
            return p, n // p
    except Exception as ex:
        log(f"  cado-nfs.py error: {ex}")

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Last-resort: msieve standalone GNFS
# ─────────────────────────────────────────────────────────────────────────────

def msieve_gnfs(n: int) -> Optional[tuple[int, int]]:
    """Run msieve's built-in GNFS pipeline as a final fallback."""
    msieve = _find_bin(MSIEVE_GPU, MSIEVE_CPU, "msieve")
    if not msieve:
        return None
    use_gpu = gpu_available()
    log(f"Last resort: msieve standalone GNFS ({'GPU' if use_gpu else 'CPU'})")
    tmp = tempfile.mkdtemp(prefix="msieve-gnfs-")
    try:
        cmd = [msieve, "-v", "-t", str(NCPUS)]
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
                   ["factor", "elapsed", "error", "siqs", "gnfs",
                    "matrix", "sieving", "lanczos"]):
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

def factor(n: int, num_bits: int) -> tuple[Optional[int], Optional[int], str]:
    n_digits = len(str(n))
    log(f"N = {str(n)[:72]}{'...' if n_digits > 72 else ''}")
    log(f"    {n_digits} decimal digits, {num_bits} bits")

    if gmpy2.is_prime(n):
        log("ERROR: N is prime — not a valid semiprime input")
        return None, None, "failed"

    # Stage 0a: Trial division up to 2 M
    log("Stage 0a: trial division (bound=2M)...")
    f = trial_division(n, 2_000_000)
    if f:
        return f, n // f, "trial_division"

    # Stage 0b: Pollard ρ
    log("Stage 0b: Pollard ρ (500K steps)...")
    f = pollard_rho(n, 500_000)
    if f:
        return f, n // f, "pollard_rho"

    # Stage 0c: Quick ECM (t25, ~1 min)
    log("Stage 0c: quick ECM (t25, 100 curves)...")
    res = quick_ecm(n, timeout=90)
    if res:
        return res[0], res[1], "ecm_quick"

    # For c100+, we need GNFS.  Our primary path is the manual CADO pipeline.
    if n_digits >= 80:
        log(f"\n{'='*60}")
        log(f"Entering CADO-NFS GNFS pipeline for c{n_digits}")
        log(f"{'='*60}")

        result = cado_pipeline(n, num_bits)
        if result:
            return result[0], result[1], "cado_gnfs"

        # Try CADO master script if we have time
        if time_left() > 1200:
            log("Manual pipeline failed; trying cado-nfs.py master script")
            result = cado_master_script(n)
            if result:
                return result[0], result[1], "cado_gnfs_master"

    # Last resort: msieve
    if time_left() > 300:
        result = msieve_gnfs(n)
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

    log("=" * 70)
    log("Breaking RSA — CADO-NFS + msieve GPU block-Lanczos")
    log(f"Challenge : {challenge_id}")
    log(f"CPUs      : {NCPUS}")
    log(f"GPU       : {gpu_available()}")
    log(f"MEMFS_ROOT: {MEMFS_ROOT}")
    log(f"LD_PRELOAD: {os.environ.get('LD_PRELOAD', '(not set)')}")
    log(f"CADO_DIR  : {CADO_DIR}")
    log(f"Deadline  : {(WALL_LIMIT - SAFETY_MARGIN)/60:.0f} min from start")
    log("=" * 70)

    p, q, method = factor(problem.num, problem.num_bits)
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
        "ncpus":           NCPUS,
        "gpu":             gpu_available(),
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
        "result.json":    result_json,
        "solve_info.json": solve_info,
    })
    write_solution_output(zip_bytes)
    os._exit(0 if solution.status == "success" else 1)


if __name__ == "__main__":
    main()
