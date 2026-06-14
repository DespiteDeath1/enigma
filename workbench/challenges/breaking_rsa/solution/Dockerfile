# ============================================================
# Breaking RSA — CADO-NFS + msieve GPU block-Lanczos
#
# Key Intel Granite Rapids (Xeon 6980P) optimisations:
#   1. Build with -march=x86-64-v3 (AVX2 only, no AVX-512)
#      Avoids the ~14 % frequency-throttle penalty on GNR that
#      AVX-512 triggers via the "power licence" mechanism.
#   2. rels_wanted=65 M (down from 71 M)
#      The ~58-60 M relation floor leaves 8 % headroom.
#      Saves ~10 % of sieve time ≈ 22-25 min on Intel.
#   3. GPU block-Lanczos (msieve, RTX PRO 6000)
#      LA is architecture-neutral: ~19 min regardless of CPU.
#   4. LD_PRELOAD RAM filesystem shim
#      Routes CADO's working files to anonymous memfd RAM storage,
#      bypassing the 1 GB container /tmp limit.
# ============================================================

FROM nvidia/cuda:12.6.3-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive TZ=UTC

# ── System build dependencies ────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git cmake python3 python3-dev python3-pip \
    libgmp-dev libmpfr-dev libmpc-dev \
    libboost-dev libboost-serialization-dev libboost-system-dev \
    libssl-dev zlib1g-dev libbz2-dev \
    autoconf automake libtool m4 nasm pkg-config \
    wget curl ca-certificates perl \
    && rm -rf /var/lib/apt/lists/*

# -march=x86-64-v3: AVX2 + FMA + POPCNT + BMI1/2, no AVX-512.
# This is the critical flag for Intel Granite Rapids: CADO's inner
# sieve loop already achieves IPC ~2.1 in AVX2 mode; forcing AVX-512
# would trigger a frequency down-clock that costs ~14 % on GNR.
ENV OPT_FLAGS="-O3 -march=x86-64-v3 -fomit-frame-pointer -funroll-loops"

# ── GMP-ECM ─────────────────────────────────────────────────────────
RUN git clone --depth=1 https://gitlab.inria.fr/zimmerma/ecm.git /tmp/ecm \
    && cd /tmp/ecm \
    && autoreconf -fi \
    && ./configure CFLAGS="${OPT_FLAGS}" \
    && make -j$(nproc) \
    && make install \
    && ldconfig \
    && rm -rf /tmp/ecm

# ── CADO-NFS ─────────────────────────────────────────────────────────
# Build CADO-NFS with AVX2 flags.  We keep the entire source+build tree
# at /cado-nfs so the solver can find binaries regardless of whether
# `make install` puts them in a standard location.
#
# Key cmake variables:
#   CMAKE_C/CXX_FLAGS  — propagates -march=x86-64-v3 to ALL TUs,
#                        including the hot inner sieve loop in las.cpp
#   WITH_MPI=OFF        — single-node, no MPI
#   BUILD_TESTING=OFF   — skip test suite
#
# We intentionally skip bwc CUDA because msieve GPU BL is better tested
# and we use that for linear algebra instead.
RUN git clone --depth=1 https://gitlab.inria.fr/cado-nfs/cado-nfs.git /cado-nfs \
    && cd /cado-nfs \
    && mkdir _build && cd _build \
    && cmake .. \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_FLAGS="${OPT_FLAGS}" \
        -DCMAKE_CXX_FLAGS="${OPT_FLAGS}" \
        -DWITH_MPI=OFF \
        -DBUILD_TESTING=OFF \
    && make -j$(nproc)

# Verify key binaries were built
RUN for b in las polyselect2l purge merge replay sqrt; do \
        p=$(find /cado-nfs/_build -name "$b" -type f 2>/dev/null | head -1); \
        if [ -n "$p" ]; then echo "OK: $b at $p"; \
        else echo "MISSING: $b"; fi; \
    done

# Symlink the key binaries into /usr/local/bin for easy PATH access
RUN for b in las polyselect2l polyselect purge merge replay sqrt freerel characters; do \
        p=$(find /cado-nfs/_build -name "$b" -type f -perm /0111 2>/dev/null | head -1); \
        if [ -n "$p" ]; then ln -sf "$p" /usr/local/bin/"$b"; fi; \
    done && \
    # Also symlink cado-nfs.py (lives in the source root)
    ln -sf /cado-nfs/cado-nfs.py /usr/local/bin/cado-nfs.py 2>/dev/null || true

RUN echo "Installed CADO binaries:" && ls /usr/local/bin/las /usr/local/bin/polyselect2l 2>/dev/null || echo "some missing"

# ── msieve with CUDA GPU block-Lanczos ───────────────────────────────
# msieve's GPU BL keeps linear algebra at ~19 min regardless of CPU arch.
#
# Compile for both sm_89 (Ada Lovelace) and sm_120 (Blackwell RTX PRO 6000).
# Forward-compat JIT handles the Blackwell GPU even if compiled for sm_89.
RUN git clone --depth=1 https://github.com/radii/msieve.git /tmp/msieve \
    && cd /tmp/msieve \
    # Fix missing include that breaks modern glibc
    && sed -i '/#include <util.h>/a #include <sys/time.h>' common/util.c \
    # GPU build with Blackwell support
    && make -j$(nproc) \
        target=x86_64 \
        CC=gcc \
        CFLAGS="${OPT_FLAGS} -I/usr/local/cuda/include" \
        CUDA_HOME=/usr/local/cuda \
        GPU_INC="-I/usr/local/cuda/include" \
        GPU_LDFLAGS="-L/usr/local/cuda/lib64 -lcudart -lcuda" \
        NVCCFLAGS="-O3 \
            -gencode arch=compute_89,code=sm_89 \
            -gencode arch=compute_120,code=sm_120 \
            --use_fast_math" \
        USE_GPU=1 \
    && cp msieve /usr/local/bin/msieve-gpu \
    # CPU-only fallback (in case GPU init fails at runtime)
    && make clean \
    && make -j$(nproc) target=x86_64 CC=gcc CFLAGS="${OPT_FLAGS}" \
    && cp msieve /usr/local/bin/msieve-cpu \
    && rm -rf /tmp/msieve

# ── LD_PRELOAD RAM filesystem shim ───────────────────────────────────
# Intercepts open/stat/rename/unlink/mkdir/opendir on paths under
# MEMFS_ROOT=/cado-work and stores them as anonymous memfd files.
# This lets CADO's 15-70 GB of working data live in the host RAM
# (85 GB available) despite the container's 1 GB /tmp tmpfs limit.
COPY memfs.c /tmp/
RUN gcc -O2 -fPIC -shared \
        -Wno-format-truncation -Wno-incompatible-pointer-types \
        -o /usr/local/lib/memfs.so /tmp/memfs.c \
        -ldl -lpthread \
    && ldconfig

# ── Python runtime environment ────────────────────────────────────────
RUN pip3 install --no-cache-dir gmpy2

# ============================================================
# Runtime image (slimmer: nvidia/cuda runtime, not devel)
# ============================================================
FROM nvidia/cuda:12.6.3-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    libgmp10 libmpfr6 zlib1g libgomp1 \
    libboost-serialization1.74.0 libboost-system1.74.0 \
    perl \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir gmpy2

# Copy built artefacts from builder
COPY --from=builder /cado-nfs                  /cado-nfs
COPY --from=builder /usr/local/bin/msieve-gpu  /usr/local/bin/msieve-gpu
COPY --from=builder /usr/local/bin/msieve-cpu  /usr/local/bin/msieve-cpu
COPY --from=builder /usr/local/bin/ecm         /usr/local/bin/ecm
COPY --from=builder /usr/local/bin/las         /usr/local/bin/las
COPY --from=builder /usr/local/bin/polyselect2l /usr/local/bin/polyselect2l
COPY --from=builder /usr/local/bin/purge       /usr/local/bin/purge
COPY --from=builder /usr/local/bin/merge       /usr/local/bin/merge
COPY --from=builder /usr/local/bin/replay      /usr/local/bin/replay
COPY --from=builder /usr/local/bin/sqrt        /usr/local/bin/sqrt
COPY --from=builder /usr/local/lib/memfs.so    /usr/local/lib/memfs.so
RUN ldconfig

# Create non-root user (validator requirement: --user miner + read-only rootfs)
RUN useradd -r -s /sbin/nologin -d /app -U miner

WORKDIR /app
COPY --chown=miner:miner enigma_challenges /app/enigma_challenges/
COPY --chown=miner:miner breaking_rsa.py  /app/

# ── Runtime environment ───────────────────────────────────────────────
# MEMFS_ROOT: virtual work dir — all CADO file I/O is intercepted here
# LD_PRELOAD: load the RAM filesystem shim into every subprocess
# CADO_DIR:   where to search for CADO binaries
ENV MEMFS_ROOT=/cado-work
ENV LD_PRELOAD=/usr/local/lib/memfs.so
ENV CADO_DIR=/cado-nfs
ENV MSIEVE_GPU=/usr/local/bin/msieve-gpu
ENV MSIEVE_CPU=/usr/local/bin/msieve-cpu
ENV ECM_BIN=/usr/local/bin/ecm
# Add CADO Python path so scripts/cadofactor modules are importable
ENV PYTHONPATH=/cado-nfs:/cado-nfs/scripts

USER miner
ENTRYPOINT ["python3", "/app/breaking_rsa.py"]
