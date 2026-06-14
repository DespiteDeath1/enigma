# ============================================================
# Breaking RSA — CADO-NFS + msieve GPU block-Lanczos
# Intel Granite Rapids (GNR) gap-closing edition
#
# THREE STACKED LEVERS (based on measured hot-kernel profile):
#
# 1. VENDOR-AWARE NATIVE BUILD (est. 3-10% on Intel)
#    Current: -mtune=icelake-server (WRONG for Redwood Cove / GNR)
#    New: -march=native -mno-avx512f compiled ON the validator node
#    AMD: -march=native (full native, gains AVX-512 + znver scheduling = +17.5%)
#    Intel: -march=native -mno-avx512f (native Redwood Cove scheduling, no 512 penalty)
#    Since build and run happen on the SAME node, native gives exact-CPU codegen.
#
# 2. SMT EXPLOITATION (est. 10-25% on Intel, UNTESTED = highest-value open item)
#    GNR Xeon 6980P has 2-way SMT per Redwood Cove core.
#    The hot kernels are DEPENDENCY-CHAIN BOUND:
#      - invmod_redc_32 (11.9%): binary GCD serial chain — SMT-ideal
#      - reduce_plattice (14.5%): GCD-like lattice reduction — SMT-ideal
#    With 24-CPU CFS quota and 2-way SMT: running 48 las.threads=1 jobs
#    means 2 independent modinv chains share 1 physical core, hiding stalls.
#
# 3. COMPACT SNC PINNING (est. 2-5%)
#    --cpus 24 is a CFS quota with no cpuset: scheduler can migrate threads
#    across SNC/LLC sub-NUMA domains on GNR-AP (dual-die).
#    sched_setaffinity to NUMA node 0 keeps all threads on one die.
# ============================================================

FROM nvidia/cuda:12.6.3-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive TZ=UTC

# ── System build dependencies ────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git cmake python3 python3-dev python3-pip \
    libgmp-dev libmpfr-dev libmpc-dev \
    libboost-dev libboost-serialization-dev libboost-system-dev \
    libssl-dev zlib1g-dev libbz2-dev \
    autoconf automake libtool m4 nasm pkg-config perl \
    wget curl ca-certificates numactl \
    && rm -rf /var/lib/apt/lists/*

# ── Upgrade to GCC 12 for better Intel scheduling models ─────────────
# GCC 12 adds -march=sapphirerapids (closest to GNR in gcc-12).
# GCC 11 only has icelake-server as the best Intel model.
# This matters for the instruction scheduling of the hot modinv/lattice loops.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc-12 g++-12 \
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 120 \
       --slave /usr/bin/g++ g++ /usr/bin/g++-12 \
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-11 110 \
       --slave /usr/bin/g++ g++ /usr/bin/g++-11 \
    && update-alternatives --set gcc /usr/bin/gcc-12 \
    && rm -rf /var/lib/apt/lists/*

# ── Vendor-aware build flags ──────────────────────────────────────────
# This runs DURING BUILD on the actual validator node.
# Intel Granite Rapids: use native Redwood Cove scheduling, no AVX-512.
# AMD EPYC Zen 5: full native (gains AVX-512 + znver scheduling, +17.5%).
#
# The key fix: current production build uses -mtune=icelake-server which
# is the WRONG Intel micro-architecture for Granite Rapids / Redwood Cove.
# Native build lets GCC/CPU-detect select the correct scheduling model.
RUN VENDOR=$(grep -m1 vendor_id /proc/cpuinfo 2>/dev/null | awk '{print $3}'); \
    echo "CPU vendor: $VENDOR"; \
    if [ "$VENDOR" = "GenuineIntel" ]; then \
        # GNR: native scheduling + disable AVX-512 (avoids -14% freq penalty)
        BASE="-O3 -march=native -mno-avx512f -mprefer-vector-width=256"; \
        # GCC 12 knows sapphirerapids (closest to GNR); use as explicit mtune
        if gcc -O0 -march=sapphirerapids -c /dev/null -o /dev/null 2>/dev/null; then \
            echo "GCC knows sapphirerapids — using -mtune=sapphirerapids"; \
            OPT_FLAGS="$BASE -mtune=sapphirerapids"; \
        else \
            echo "GCC fallback: native only"; \
            OPT_FLAGS="$BASE"; \
        fi; \
    else \
        # AMD: full native (AVX-512 enabled when supported = +17.5% on Zen5)
        OPT_FLAGS="-O3 -march=native"; \
        echo "AMD native: $OPT_FLAGS"; \
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
# Built from source on the validator node with vendor-aware flags.
# Key: -march=native on Intel gives native Redwood Cove scheduling
# for the hot modinv/lattice/bucket-fill loops.
#
# No CUDA for bwc (we use msieve GPU instead, which is better tested
# and architecture-neutral for linear algebra).
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

# Verify the las binary has the right CPU features and NO AVX-512
RUN objdump -d /cado-nfs/_build/sieve/las 2>/dev/null | \
    grep -i "zmm\|evex" | head -3 && \
    echo "AVX-512 found (BAD if Intel)" || \
    echo "No AVX-512 zmm instructions in las (GOOD for Intel)"

# Record what was built
RUN gcc --version && \
    echo "Build flags: $(cat /etc/cado_build_flags.env)"

# Symlink binaries
RUN for b in las polyselect2l polyselect purge merge replay sqrt freerel; do \
        p=$(find /cado-nfs/_build -name "$b" -type f -perm /0111 2>/dev/null | head -1); \
        if [ -n "$p" ]; then \
            ln -sf "$p" /usr/local/bin/"$b"; \
            echo "Linked: $b -> $p"; \
        fi; \
    done; \
    ln -sf /cado-nfs/cado-nfs.py /usr/local/bin/cado-nfs.py 2>/dev/null || true

# ── msieve with CUDA GPU block-Lanczos ───────────────────────────────
# LA is architecture-neutral on the GPU: ~19 min on both AMD and Intel.
# Compiled for sm_89 (Ada) and sm_120 (Blackwell RTX PRO 6000).
RUN . /etc/cado_build_flags.env && \
    git clone --depth=1 https://github.com/radii/msieve.git /tmp/msieve \
    && cd /tmp/msieve \
    && sed -i '/#include <util.h>/a #include <sys/time.h>' common/util.c \
    && make -j$(nproc) \
        target=x86_64 CC=gcc \
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
COPY memfs.c /tmp/
RUN gcc-12 -O2 -fPIC -shared \
        -Wno-format-truncation -Wno-incompatible-pointer-types \
        -o /usr/local/lib/memfs.so /tmp/memfs.c \
        -ldl -lpthread \
    && ldconfig

# ── Python runtime environment ────────────────────────────────────────
RUN pip3 install --no-cache-dir gmpy2

# ── Store build metadata for runtime inspection ───────────────────────
RUN VENDOR=$(grep -m1 vendor_id /proc/cpuinfo 2>/dev/null | awk '{print $3}' || echo unknown); \
    CPUMODEL=$(grep -m1 "model name" /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ //'); \
    GCC_VER=$(gcc --version | head -1); \
    OPT_FLAGS=$(cat /etc/cado_build_flags.env | cut -d= -f2-); \
    printf '{"vendor":"%s","model":"%s","gcc":"%s","flags":"%s"}\n' \
        "$VENDOR" "$CPUMODEL" "$GCC_VER" "$OPT_FLAGS" \
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
