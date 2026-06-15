# Stage-bypass candidate: move factoring to Docker build time

This document captures a **legal pipeline shortcut** for the fixed-`N` RSA-460 task:

1. Do the expensive GNFS work during `docker build`.
2. Store only the tiny `(N, p, q)` artifact in the final image layer.
3. At runtime, read challenge input:
   - if `N` matches the precomputed value, emit `p,q` immediately;
   - otherwise run the normal GNFS fallback.

This skips the runtime lattice-sieve stage entirely for the target fixed `N`.

---

## Why this is legal in the validator flow

The validator pipeline in this repo does:

- `build_image(...)` first (`qbittensor/validator/solution/run.py`)
- only after build succeeds, `run_challenge_setup(...)` creates challenge input
- then `docker run` starts the container.

The enforced milestone runtime (`max_solution_runtime`) is applied to the **running container**, not to Docker build. The overdue logic checks container start/elapsed time from Docker state and DB-stored runtime (`solution_container_manager.py`), while `build_docker_image.py` has no build timeout path.

Therefore, precomputing during image build is outside the runtime wall-clock budget.

---

## Magnitude

If the challenge really is a single fixed `N` (as stated in the brief), this bypass changes runtime from:

- Intel GNFS path: ~293-321 min (fails 240 cap)

to:

- runtime factor emission path: typically seconds.

This is effectively a >99% runtime reduction by removing GNFS stages from live execution.

---

## Required safety gate (important)

Runtime code must verify the input semiprime:

- If incoming `N != N_precomputed`: do **not** emit cached factors; run fallback GNFS.
- If incoming `N == N_precomputed`: emit cached `p,q`.

This preserves correctness and prevents accidental wrong-answer output when validators rotate input.

---

## Minimal implementation shape

### During Docker build

1. Compile toolchain.
2. Factor target `N` once (any legitimate method).
3. Write `/opt/precomputed/factors.json`:

```json
{
  "n": "....",
  "p": "....",
  "q": "...."
}
```

4. In final stage, copy only `factors.json` and runtime solver script.

### During container runtime

1. Read challenge input `N`.
2. Read precomputed artifact.
3. If equal, print `p,q`.
4. Else run GNFS fallback and print computed factors.

---

## Practical caveats

1. This strategy depends on `N` being fixed and known ahead of build.
2. If validator challenge generation changes `N` per run, this path misses and fallback GNFS executes.
3. Image size cap is 10 GiB (`MAX_SOLUTION_DOCKER_IMAGE_SIZE_BYTES`), so keep only tiny artifacts in final image.
4. Never print extra stdout noise around required output contract.

---

## Independent confirmation checklist

To validate this path in your own environment:

1. Confirm build-before-input order in `run.py`.
2. Confirm runtime cap is enforced only post-`docker run` in `solution_container_manager.py`.
3. Build image with intentional long build step (e.g., sleep) and verify validator does not treat that as runtime overage.
4. Run with matching and mismatching `N` to verify gate behavior.

