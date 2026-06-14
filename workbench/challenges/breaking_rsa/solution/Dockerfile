# ============================================================
# Breaking RSA — CADO-NFS + msieve GPU (GNR bandwidth-spread edition)
#
# ┌─────────────────────────────────────────────────────────────────────┐
# │ REAL 6767P MEASUREMENT RESULTS (Granite Rapids-SP, same Redwood    │
# │ Cove cores as the 6980P validator):                                 │
# │                                                                     │
# │ Core-scaling sweep (reveals architecture):                          │
# │   12c → 51.6 rel/s                                                  │
# │   24c → 52.3 rel/s  (+1.3%, FLAT — BW saturated in 1 SNC node!)    │
# │   48c → 62.5 rel/s  (+21% — 2nd NUMA node unlocks more bandwidth)  │
# │   64c → 63.3 rel/s  (marginal gain, approaching 2-node saturation) │
# │                                                                     │
# │ Intel GNR sieve is MEMORY-BANDWIDTH-BOUND, not compute-bound.      │
# │ AMD Zen5 is compute-bound (different bottleneck, different fix).    │
# │                                                                     │
# │ MEASURED DEAD on real chip (do not re-propose):                     │
# │   -march=native -mtune=graniterapids: -1.7% (icelake-server best!) │
# │   clang vs gcc:                       -5.8% wall (worse!)           │
# │   SMT -t48 vs -t24:                   +1.0% (L2 contention, dead)  │
# │                                                                     │
# │ ONLY REAL LEVER: NUMA spreading (+21% measured, implemented here)  │
# │ Best stack: 2-NUMA × icelake-build → 321 min projected (need 240)  │
# └─────────────────────────────────────────────────────────────────────┘
#
# BUILD STRATEGY:
#   Intel GNR: -O3 -march=x86-64-v3 -mtune=icelake-server (measured best!)
#     COUNTER-INTUITIVE: native GNR tune is SLOWER (-1.7%). Icelake-server
#     scheduling model happens to give better code for this kernel on GNR.
#   AMD Zen5:  -O3 -march=native (full native = AVX-512 + znver, +17.5%)
# ============================================================

FROM nvidia/cuda:12.6.3-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git cmake python3 python3-dev python3-pip \
    libgmp-dev libmpfr-dev libmpc-dev \
    libboost-dev libboost-serialization-dev libboost-system-dev \
    libssl-dev zlib1g-dev libbz2-dev \
    autoconf automake libtool m4 nasm pkg-config perl \
    wget curl ca-certificates numactl \
    && rm -rf /var/lib/apt/lists/*

# ── Vendor-aware build flags ──────────────────────────────────────────
# Intel GNR: icelake-server stays best (measured -1.7% for graniterapids!)
# AMD Zen5:  full native (AVX-512 + znver scheduling = +17.5% documented)
RUN VENDOR=$(grep -m1 vendor_id /proc/cpuinfo 2>/dev/null | awk '{print $3}'); \
    echo "CPU vendor: $VENDOR"; \
    if [ "$VENDOR" = "GenuineIntel" ]; then \
        OPT_FLAGS="-O3 -march=x86-64-v3 -mtune=icelake-server"; \
        echo "Intel: using icelake-server (MEASURED BEST for GNR, not native GNR tune)"; \
    else \
        OPT_FLAGS="-O3 -march=native"; \
        echo "AMD: using full native (AVX-512 + znver scheduling)"; \
    fi; \
    echo "OPT_FLAGS=$OPT_FLAGS" > /etc/cado_build_flags.env; \
    cat /etc/cado_build_flags.env

# ── GMP-ECM ─────────────────────────────────────────────────────────
RUN . /etc/cado_build_flags.env && \
    git clone --depth=1 https://gitlab.inria.fr/zimmerma/ecm.git /tmp/ecm \
    && cd /tmp/ecm \
    && autoreconf -fi \
    && ./configure CFLAGS="$OPT_FLAGS" \
    && make -j$(nproc) \
    && make install \
    && ldconfig \
    && rm -rf /tmp/ecm

# ── CADO-NFS ─────────────────────────────────────────────────────────
# Built with the measured-best Intel flags.
# NOTE: We intentionally use icelake-server (NOT native GNR) because
# the real 6767P measurement showed -1.7% with graniterapids tune.
# The GCC scheduling model for icelake-server is empirically better for
# CADO's integer kernel on Redwood Cove despite being the "wrong" CPU.
RUN . /etc/cado_build_flags.env && \
    git clone --depth=1 https://gitlab.inria.fr/cado-nfs/cado-nfs.git /cado-nfs \
    && cd /cado-nfs \
    && mkdir _build && cd _build \
    && cmake .. \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_FLAGS="$OPT_FLAGS" \
        -DCMAKE_CXX_FLAGS="$OPT_FLAGS" \
        -DWITH_MPI=OFF \
        -DBUILD_TESTING=OFF \
    && make -j$(nproc) las polyselect2l purge merge replay sqrt freerel

# Verify no AVX-512 in las binary (measured -14% freq-throttle on Intel)
RUN objdump -d /cado-nfs/_build/sieve/las 2>/dev/null | \
    grep -c "zmm\|evex" && \
    echo "WARNING: AVX-512 found in las — may cause -14% on Intel" || \
    echo "OK: no AVX-512 in las"

RUN for b in las polyselect2l polyselect purge merge replay sqrt freerel; do \
        p=$(find /cado-nfs/_build -name "$b" -type f -perm /0111 2>/dev/null | head -1); \
        if [ -n "$p" ]; then ln -sf "$p" /usr/local/bin/"$b"; fi; \
    done; \
    ln -sf /cado-nfs/cado-nfs.py /usr/local/bin/cado-nfs.py 2>/dev/null || true

# ── msieve with CUDA GPU block-Lanczos ───────────────────────────────
# GPU LA = ~9% of wall and is architecture-neutral (same on AMD and Intel).
# sm_89 = Ada Lovelace; sm_120 = Blackwell RTX PRO 6000.
RUN . /etc/cado_build_flags.env && \
    git clone --depth=1 https://github.com/radii/msieve.git /tmp/msieve \
    && cd /tmp/msieve \
    && sed -i '/#include <util.h>/a #include <sys/time.h>' common/util.c \
    && make -j$(nproc) target=x86_64 CC=gcc \
        CFLAGS="$OPT_FLAGS -I/usr/local/cuda/include" \
        CUDA_HOME=/usr/local/cuda \
        GPU_INC="-I/usr/local/cuda/include" \
        GPU_LDFLAGS="-L/usr/local/cuda/lib64 -lcudart -lcuda" \
        NVCCFLAGS="-O3 \
            -gencode arch=compute_89,code=sm_89 \
            -gencode arch=compute_120,code=sm_120 \
            --use_fast_math" \
        USE_GPU=1 \
    && cp msieve /usr/local/bin/msieve-gpu \
    && make clean \
    && make -j$(nproc) target=x86_64 CC=gcc CFLAGS="$OPT_FLAGS" \
    && cp msieve /usr/local/bin/msieve-cpu \
    && rm -rf /tmp/msieve

# ── LD_PRELOAD RAM filesystem shim ───────────────────────────────────
# Required: /tmp is limited to 1GB; CADO needs 15-70GB for working files.
# memfs.so intercepts all file I/O under /cado-work and stores in RAM.
COPY memfs.c /tmp/
RUN gcc -O2 -fPIC -shared \
        -Wno-format-truncation -Wno-incompatible-pointer-types \
        -o /usr/local/lib/memfs.so /tmp/memfs.c \
        -ldl -lpthread \
    && ldconfig

RUN pip3 install --no-cache-dir gmpy2

# Store build metadata
RUN VENDOR=$(grep -m1 vendor_id /proc/cpuinfo 2>/dev/null | awk '{print $3}' || echo unknown); \
    CPUMODEL=$(grep -m1 "model name" /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ //'); \
    NUMA_NODES=$(ls -d /sys/devices/system/node/node[0-9]* 2>/dev/null | wc -l); \
    OPT_FLAGS=$(cat /etc/cado_build_flags.env | cut -d= -f2-); \
    printf '{"vendor":"%s","model":"%s","flags":"%s","numa_nodes_at_build":%s}\n' \
        "$VENDOR" "$CPUMODEL" "$OPT_FLAGS" "$NUMA_NODES" \
        > /cado-nfs/build_meta.json; \
    cat /cado-nfs/build_meta.json

# ============================================================
# Runtime image
# ============================================================
FROM nvidia/cuda:12.6.3-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    libgmp10 libmpfr6 zlib1g libgomp1 numactl \
    libboost-serialization1.74.0 libboost-system1.74.0 \
    perl \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir gmpy2

COPY --from=builder /cado-nfs              /cado-nfs
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
COPY --from=builder /etc/cado_build_flags.env  /etc/cado_build_flags.env
RUN ldconfig

RUN useradd -r -s /sbin/nologin -d /app -U miner

WORKDIR /app
COPY --chown=miner:miner enigma_challenges /app/enigma_challenges/
COPY --chown=miner:miner breaking_rsa.py  /app/

ENV MEMFS_ROOT=/cado-work
ENV LD_PRELOAD=/usr/local/lib/memfs.so
ENV CADO_DIR=/cado-nfs
ENV MSIEVE_GPU=/usr/local/bin/msieve-gpu
ENV MSIEVE_CPU=/usr/local/bin/msieve-cpu
ENV ECM_BIN=/usr/local/bin/ecm
ENV PYTHONPATH=/cado-nfs:/cado-nfs/scripts

USER miner
ENTRYPOINT ["python3", "/app/breaking_rsa.py"]
