#!/usr/bin/env python3
"""Run reproducible A/B command matrices and summarize metrics.

This is intentionally generic: it executes arbitrary shell commands,
captures logs, extracts regex metrics, and writes JSON/CSV summaries.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class RunResult:
    name: str
    command: str
    cwd: str
    exit_code: int
    duration_s: float
    log_file: str
    metrics: dict[str, Any]


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_matrix(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if "experiments" not in payload or not isinstance(payload["experiments"], list):
        raise ValueError("matrix JSON must contain an 'experiments' list")
    if len(payload["experiments"]) == 0:
        raise ValueError("'experiments' must not be empty")
    return payload


def _extract_metrics(log_text: str, patterns: dict[str, str]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for key, pattern in patterns.items():
        matches = list(re.finditer(pattern, log_text, flags=re.MULTILINE))
        if not matches:
            metrics[key] = None
            continue
        m = matches[-1]
        value: Any
        if m.groupdict():
            if "value" in m.groupdict():
                value = m.group("value")
            else:
                # First named group if "value" isn't provided.
                group_name = next(iter(m.groupdict().keys()))
                value = m.group(group_name)
        elif m.groups():
            value = m.group(1)
        else:
            value = m.group(0)
        try:
            metrics[key] = float(value)
        except (TypeError, ValueError):
            metrics[key] = value
    return metrics


def _run_one(
    exp: dict[str, Any],
    matrix_vars: dict[str, str],
    default_cwd: Path,
    out_dir: Path,
) -> RunResult:
    name = exp["name"]
    command_template = exp["command"]
    command = command_template.format(**matrix_vars)
    cwd = Path(exp.get("cwd", str(default_cwd))).expanduser()
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in exp.get("env", {}).items()})

    started = time.perf_counter()
    log_file = out_dir / f"{name}.log"
    with log_file.open("w", encoding="utf-8") as f:
        f.write(f"# name: {name}\n")
        f.write(f"# cwd: {cwd}\n")
        f.write(f"# command: {command}\n")
        f.write(f"# started_utc: {_now_utc()}\n\n")
        f.flush()
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    duration_s = time.perf_counter() - started
    log_text = log_file.read_text(encoding="utf-8")
    metrics = _extract_metrics(log_text, exp.get("metrics", {}))

    return RunResult(
        name=name,
        command=command,
        cwd=str(cwd),
        exit_code=proc.returncode,
        duration_s=duration_s,
        log_file=str(log_file),
        metrics=metrics,
    )


def _write_results(
    results: list[RunResult],
    out_dir: Path,
    primary_metric: str | None,
    baseline_name: str | None,
) -> None:
    baseline_value = None
    if primary_metric and baseline_name:
        for r in results:
            if r.name == baseline_name:
                v = r.metrics.get(primary_metric)
                if isinstance(v, (int, float)) and v > 0:
                    baseline_value = float(v)
                break

    json_path = out_dir / "results.json"
    csv_path = out_dir / "results.csv"
    payload: list[dict[str, Any]] = []
    all_metric_keys = sorted({k for r in results for k in r.metrics.keys()})

    for r in results:
        row: dict[str, Any] = {
            "name": r.name,
            "exit_code": r.exit_code,
            "duration_s": round(r.duration_s, 3),
            "cwd": r.cwd,
            "command": r.command,
            "log_file": r.log_file,
        }
        row.update(r.metrics)
        if primary_metric and baseline_value:
            v = r.metrics.get(primary_metric)
            if isinstance(v, (int, float)):
                row[f"{primary_metric}_vs_baseline_pct"] = round(
                    (float(v) / baseline_value - 1.0) * 100.0, 3
                )
            else:
                row[f"{primary_metric}_vs_baseline_pct"] = None
        payload.append(row)

    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    columns = [
        "name",
        "exit_code",
        "duration_s",
        "cwd",
        "log_file",
    ] + all_metric_keys
    if primary_metric and baseline_value:
        columns.append(f"{primary_metric}_vs_baseline_pct")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in payload:
            writer.writerow(row)

    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run command experiment matrices")
    parser.add_argument(
        "--matrix",
        required=True,
        help="Path to matrix JSON (see intel_gap_matrix.example.json)",
    )
    parser.add_argument(
        "--out-dir",
        default="matrix_runs",
        help="Output root directory (default: matrix_runs)",
    )
    args = parser.parse_args()

    matrix_path = Path(args.matrix).expanduser().resolve()
    matrix = _load_matrix(matrix_path)
    runs_root = Path(args.out_dir).expanduser().resolve()
    out_dir = runs_root / _now_utc()
    out_dir.mkdir(parents=True, exist_ok=True)

    matrix_vars = {str(k): str(v) for k, v in matrix.get("vars", {}).items()}
    default_cwd = Path(matrix.get("default_cwd", ".")).expanduser().resolve()
    primary_metric = matrix.get("primary_metric")
    baseline_name = matrix.get("baseline_name")

    results: list[RunResult] = []
    for exp in matrix["experiments"]:
        name = exp["name"]
        print(f"=== Running: {name} ===")
        print(f"Command: {exp['command']}")
        if exp.get("cwd"):
            print(f"CWD: {exp['cwd']}")
        result = _run_one(exp, matrix_vars, default_cwd, out_dir)
        print(
            f"Done: {name} (exit={result.exit_code}, "
            f"duration={result.duration_s:.2f}s, log={result.log_file})"
        )
        results.append(result)

    _write_results(results, out_dir, primary_metric, baseline_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
