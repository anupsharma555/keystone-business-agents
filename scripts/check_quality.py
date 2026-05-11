"""Run high-signal local quality checks."""

from __future__ import annotations

import subprocess
import sys

CHECKS = (
    ("pytest with coverage", [sys.executable, "-m", "coverage", "run", "-m", "pytest"]),
    ("coverage report", [sys.executable, "-m", "coverage", "report"]),
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    (
        "pip-audit",
        [sys.executable, "-m", "pip_audit", "--skip-editable", "--progress-spinner", "off"],
    ),
)


def main() -> int:
    for label, command in CHECKS:
        print(f"\n== {label} ==", flush=True)
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
