from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "benchmark_architecture_latency.py"


def _load_benchmark_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "benchmark_architecture_latency",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percentile_uses_nearest_rank() -> None:
    module = _load_benchmark_module()

    assert module._percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert module._percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.0


def test_benchmark_runs_one_credential_free_import() -> None:
    completed = subprocess.run(
        (
            sys.executable,
            str(SCRIPT_PATH),
            "--iterations",
            "1",
            "--warmups",
            "0",
            "--only",
            "execution_request_import",
        ),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)

    assert report["schema_name"] == "keystone.architecture_latency_benchmark.v1"
    assert report["offline"] is True
    assert report["iterations"] == 1
    assert report["metrics"][0]["name"] == "execution_request_import"
    assert report["metrics"][0]["median_ms"] > 0
