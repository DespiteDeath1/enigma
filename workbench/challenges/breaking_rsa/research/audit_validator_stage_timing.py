#!/usr/bin/env python3
"""
Quick static audit: validator build/run ordering and runtime-limit scope.

This script does not execute Docker. It inspects source files and reports whether:
  1) build_image() appears before run_challenge_setup() in run.py
  2) container runtime enforcement uses StartedAt/runtime checks (not build time)
"""

from __future__ import annotations

import argparse
from pathlib import Path


def index_of(text: str, needle: str) -> int:
    i = text.find(needle)
    return i


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root containing qbittensor/",
    )
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()
    run_py = root / "qbittensor/validator/solution/run.py"
    scm_py = root / "qbittensor/validator/solution/solution_container_manager.py"
    build_py = root / "qbittensor/validator/solution/build_docker_image.py"

    for p in (run_py, scm_py, build_py):
        if not p.exists():
            print(f"missing: {p}")
            return 2

    run_txt = run_py.read_text(encoding="utf-8")
    scm_txt = scm_py.read_text(encoding="utf-8")
    build_txt = build_py.read_text(encoding="utf-8")

    build_pos = index_of(run_txt, "build_image(")
    setup_pos = index_of(run_txt, "run_challenge_setup(")
    run_detached_pos = index_of(run_txt, "run_image_detached(")

    build_before_setup = build_pos != -1 and setup_pos != -1 and build_pos < setup_pos
    setup_before_run = setup_pos != -1 and run_detached_pos != -1 and setup_pos < run_detached_pos

    has_build_timeout = "timeout=" in build_txt and "popen(" in build_txt
    runtime_enforced_on_container = (
        ".State.StartedAt" in scm_txt
        and "max_solution_runtime_seconds" in scm_txt
        and "_get_overdue_containers" in scm_txt
    )

    print("validator_stage_timing_audit")
    print(f"build_before_challenge_setup={str(build_before_setup).lower()}")
    print(f"challenge_setup_before_container_run={str(setup_before_run).lower()}")
    print(f"build_script_mentions_timeout_literal={str(has_build_timeout).lower()}")
    print(f"runtime_limit_enforced_on_running_container={str(runtime_enforced_on_container).lower()}")

    return 0 if build_before_setup and setup_before_run and runtime_enforced_on_container else 1


if __name__ == "__main__":
    raise SystemExit(main())

