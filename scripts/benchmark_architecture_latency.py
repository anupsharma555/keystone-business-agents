#!/usr/bin/env python3
"""Measure credential-free KBA import and CLI startup latency."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_REF = "kba-baseline-pre-reorganization-2026-07-25"
SCHEMA_NAME = "keystone.architecture_latency_benchmark.v1"

BENCHMARKS: dict[str, tuple[str, ...]] = {
    "execution_request_import": (
        sys.executable,
        "-c",
        "import keystone_agents.execution_request",
    ),
    "manual_request_import": (
        sys.executable,
        "-c",
        "import keystone_agents.manual_request",
    ),
    "workflow_runner_import": (
        sys.executable,
        "-c",
        "import keystone_agents.workflow_runner",
    ),
    "langgraph_workflow_import": (
        sys.executable,
        "-c",
        "import keystone_agents.langgraph_workflow",
    ),
    "cli_import": (
        sys.executable,
        "-c",
        "import keystone_agents.cli",
    ),
    "cli_help": (
        sys.executable,
        "-m",
        "keystone_agents.cli",
        "--help",
    ),
}


def _offline_env(state_dir: Path) -> dict[str, str]:
    """Return a minimal child environment that cannot enable live providers."""

    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "LANG",
            "LC_ALL",
            "PATH",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "TMPDIR",
        }
    }
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "KEYSTONE_TEST_MODE": "1",
            "KEYSTONE_HOME": str(state_dir / ".keystone"),
            "KEYSTONE_RUNTIME_STATE_DIR": str(state_dir),
            "DATABASE_URL": "sqlite:///:memory:",
            "KEYSTONE_LIVE_MODE": "false",
            "KEYSTONE_DRY_RUN": "true",
            "KEYSTONE_ENABLE_LIVE_GMAIL": "false",
            "KEYSTONE_ENABLE_LIVE_SLACK": "false",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "false",
            "KEYSTONE_ENABLE_LIVE_CRM": "false",
            "KEYSTONE_ENABLE_WEBSITE_EXTRACTION": "false",
            "SEARCH_PROVIDER": "dry-run",
            "AUTO_SEND_EMAIL": "false",
        }
    )
    return env


def _git_value(*args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _percentile(samples: list[float], quantile: float) -> float:
    if not samples:
        raise ValueError("samples are required")
    ordered = sorted(samples)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _run_sample(command: tuple[str, ...], *, env: dict[str, str]) -> float:
    started_at = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    if completed.returncode != 0:
        stderr = completed.stderr.strip()[-2000:]
        raise RuntimeError(
            f"Benchmark command failed ({completed.returncode}): "
            f"{' '.join(command)}\n{stderr}"
        )
    return round(elapsed_ms, 3)


def _measure(
    name: str,
    *,
    iterations: int,
    warmups: int,
    env: dict[str, str],
) -> dict[str, Any]:
    command = BENCHMARKS[name]
    for _ in range(warmups):
        _run_sample(command, env=env)
    samples = [_run_sample(command, env=env) for _ in range(iterations)]
    return {
        "name": name,
        "command": list(command[1:]),
        "iterations": iterations,
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
        "samples_ms": samples,
    }


def benchmark(
    *,
    names: list[str],
    iterations: int,
    warmups: int,
) -> dict[str, Any]:
    """Run selected benchmarks and return one serializable report."""

    with tempfile.TemporaryDirectory(prefix="kba-architecture-benchmark-") as value:
        env = _offline_env(Path(value))
        metrics = [
            _measure(
                name,
                iterations=iterations,
                warmups=warmups,
                env=env,
            )
            for name in names
        ]
    return {
        "schema_name": SCHEMA_NAME,
        "baseline_ref": BASELINE_REF,
        "commit_sha": _git_value("rev-parse", "HEAD"),
        "tree_sha": _git_value("rev-parse", "HEAD^{tree}"),
        "python_version": sys.version.split()[0],
        "iterations": iterations,
        "warmups": warmups,
        "offline": True,
        "metrics": metrics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument(
        "--only",
        action="append",
        choices=tuple(BENCHMARKS),
        default=[],
        help="Run only this benchmark; repeat for multiple selections.",
    )
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")
    if args.warmups < 0:
        parser.error("--warmups cannot be negative")

    report = benchmark(
        names=args.only or list(BENCHMARKS),
        iterations=args.iterations,
        warmups=args.warmups,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.json_output:
        output_path = args.json_output
        if not output_path.is_absolute():
            output_path = PROJECT_ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
