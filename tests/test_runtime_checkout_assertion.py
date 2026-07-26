from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.assert_runtime_checkout import inspect_runtime_checkout

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_checkout_assertion_accepts_current_checkout() -> None:
    report = inspect_runtime_checkout(PROJECT_ROOT)

    assert report["status"] == "pass"
    assert report["mismatches"] == {}
    assert set(report["modules"]) == {
        "keystone_agents",
        "keystone_agents.cli",
        "keystone_agents.authority.semantic",
    }


def test_runtime_checkout_assertion_can_require_worktree_venv(tmp_path: Path) -> None:
    report = inspect_runtime_checkout(tmp_path, require_worktree_venv=True)

    assert report["status"] == "fail"
    assert "python_prefix" in report["mismatches"]


def test_runtime_checkout_assertion_fails_for_another_root(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "assert_runtime_checkout.py"),
            "--expected-root",
            str(tmp_path),
            "--json",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert '"status": "fail"' in completed.stdout
    assert "keystone_agents.authority.semantic" in completed.stdout
