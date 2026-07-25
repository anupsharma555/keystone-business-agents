#!/usr/bin/env python3
"""Fail unless the active Keystone package is loaded from the intended checkout."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

MODULE_NAMES = (
    "keystone_agents",
    "keystone_agents.cli",
    "keystone_agents.authority.semantic",
)


def _module_path(module: ModuleType) -> Path:
    raw_path = getattr(module, "__file__", None)
    if not raw_path:
        raise RuntimeError(f"{module.__name__} does not expose a filesystem path")
    return Path(raw_path).resolve()


def inspect_runtime_checkout(
    expected_root: Path, *, require_worktree_venv: bool = False
) -> dict[str, object]:
    resolved_root = expected_root.expanduser().resolve()
    module_paths = {
        module_name: _module_path(importlib.import_module(module_name))
        for module_name in MODULE_NAMES
    }
    mismatches = {
        module_name: str(module_path)
        for module_name, module_path in module_paths.items()
        if not module_path.is_relative_to(resolved_root)
    }
    python_prefix = Path(sys.prefix).resolve()
    if require_worktree_venv and not python_prefix.is_relative_to(resolved_root):
        mismatches["python_prefix"] = str(python_prefix)
    return {
        "status": "pass" if not mismatches else "fail",
        "expected_root": str(resolved_root),
        "python": sys.executable,
        "python_prefix": str(python_prefix),
        "modules": {
            module_name: str(module_path) for module_name, module_path in module_paths.items()
        },
        "mismatches": mismatches,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify that the active Keystone package resolves under one checkout."
    )
    parser.add_argument(
        "--expected-root",
        type=Path,
        required=True,
        help="Repository checkout that must own the imported Keystone modules.",
    )
    parser.add_argument(
        "--require-worktree-venv",
        action="store_true",
        help="Also require the active Python environment to live under the expected checkout.",
    )
    parser.add_argument("--json", action="store_true", help="Print the proof as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = inspect_runtime_checkout(
        args.expected_root,
        require_worktree_venv=args.require_worktree_venv,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"runtime checkout: {report['status']}")
        print(f"expected root: {report['expected_root']}")
        for module_name, module_path in report["modules"].items():
            print(f"{module_name}: {module_path}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
