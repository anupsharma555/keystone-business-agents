from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "benchmark_architecture_latency.py"
BASELINE_REPORT_PATH = PROJECT_ROOT / "docs" / "architecture" / "pre_reorganization_latency.json"
POST_REPORT_PATH = PROJECT_ROOT / "docs" / "architecture" / "post_reorganization_latency.json"


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


def test_recorded_cli_latency_exceeds_the_acceptance_target() -> None:
    baseline = json.loads(BASELINE_REPORT_PATH.read_text(encoding="utf-8"))
    post = json.loads(POST_REPORT_PATH.read_text(encoding="utf-8"))
    baseline_metrics = {item["name"]: item for item in baseline["metrics"]}
    post_metrics = {item["name"]: item for item in post["metrics"]}

    assert post["schema_name"] == baseline["schema_name"]
    assert post["baseline_ref"] == baseline["baseline_ref"]
    assert post["offline"] is True
    assert post["iterations"] == baseline["iterations"] == 30
    assert post["warmups"] == baseline["warmups"] == 3
    assert post["python_version"] == baseline["python_version"]

    for name in ("cli_import", "cli_help"):
        baseline_metric = baseline_metrics[name]
        post_metric = post_metrics[name]
        assert post_metric["iterations"] == 30
        assert len(post_metric["samples_ms"]) == 30
        assert post_metric["median_ms"] <= baseline_metric["median_ms"] * 0.60
        assert post_metric["p95_ms"] <= baseline_metric["p95_ms"] * 0.60
