# AGENTS.md

## Cursor Cloud specific instructions

Enigma (Bittensor Subnet 63) is a Python 3.12 project. Standard setup, lint, test,
and run commands are documented in `CONTRIBUTING.md`, `README.md`, and
`workbench/README.md`; the notes below only cover non-obvious cloud caveats.

### Environment layout
- Dependencies are installed into a virtualenv at `.venv` (created by the update
  script). Activate it before any work: `source .venv/bin/activate`.
- The package is installed editable (`pip install -e .`), so source changes take
  effect without reinstalling. Console scripts `mine-enigma` and
  `check-validation` are on `PATH` inside the venv.

### Lint
- Run `flake8 .` per `CONTRIBUTING.md`. Caveat: the `.flake8` config file
  referenced by the docs is NOT present in the repo, and `.github/workflows/ci.yml`
  is also absent. With no config, flake8 falls back to defaults (79-char lines, no
  exclusions) and will report many violations and even recurse into `.venv`. To
  lint only project code, exclude the venv, e.g.
  `flake8 . --exclude .venv,.git,build,dist`. Treat reported violations as a repo
  gap (missing config), not an environment problem.

### Tests
- Default suite: `pytest .` (unit only; integration is deselected via `pytest.ini`).
  All unit tests pass in this environment. Note one "unit" test
  (`tests/unit/validator/utils/gpu_verification/test_gpu_access.py`) actually
  builds a Docker image, so Docker must be running for the full suite to pass; its
  first run takes ~1 min to build.
- Integration tests (`pytest -m integration`) require Docker AND the validator's
  hardened-container runtime, which sets `--memory`/`--memory-swap` limits. These
  CANNOT run in the cloud VM: the delegated cgroup-v2 namespace root is in
  "threaded" mode and does not expose the `memory`/`io` controllers, so the
  container fails with `cannot enter cgroupv2 ... it is in threaded mode`. This is
  a Firecracker/nested-virtualization limitation, not a code issue.

### Docker
- Docker is NOT auto-started by the update script. Start it manually when needed:
  `sudo dockerd > /tmp/dockerd.log 2>&1 &` then `sudo chmod 666 /var/run/docker.sock`.
  It must use the `fuse-overlayfs` storage driver (already configured in
  `/etc/docker/daemon.json`) because the kernel lacks full overlay2 support.
- Only the dependency installs are in the update script; system packages
  (`docker-ce`, `fuse-overlayfs`, `iptables`, `libgmp/mpfr/mpc-dev`,
  `python3.12-venv`) are baked into the VM image, not reinstalled per run.

### Running the product (fully local, no chain/GPU)
- The Developer Workbench is the only end-to-end path that runs without a
  subtensor chain connection, wallet, or GPU. It generates a challenge, runs the
  example solver, and validates+verifies the result. Example hello-world:
  `python -m workbench test breaking-rsa --solution workbench/challenges/breaking_rsa/example_solution/ --difficulty 64 --mode direct`
  Use `--mode docker` to exercise the full containerized pipeline (needs Docker
  running; workbench relaxes memory/cpu limits by default so the cgroup limitation
  above does not affect it).
- The validator/miner neurons (`neurons/validator.py`, `neurons/miner.py`) and
  `mine-enigma` CLI require a Bittensor wallet, chain (netuid 63) registration,
  and—for the validator—an NVIDIA GPU. None of these are available in the cloud
  VM, so only `--help`/import smoke tests are feasible here; use the workbench for
  functional testing.
