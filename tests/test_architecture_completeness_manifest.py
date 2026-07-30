from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
from pathlib import Path

from keystone_agents.agent_registry import list_agent_specs
from keystone_agents.cli import build_parser

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    PROJECT_ROOT
    / "docs"
    / "architecture"
    / "pre_reorganization_completeness.json"
)


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _git(*args: str) -> bytes:
    completed = subprocess.run(
        ("git", *args),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def test_manifest_matches_immutable_baseline_tag() -> None:
    manifest = _manifest()
    baseline = manifest["baseline"]
    tag = baseline["tag"]

    assert _git("rev-parse", f"{tag}^{{}}").decode().strip() == baseline["commit_sha"]
    assert (
        _git("rev-parse", f"{tag}^{{}}^{{tree}}").decode().strip()
        == baseline["tree_sha"]
    )
    for path, expected in manifest["critical_files"].items():
        content = _git("show", f"{tag}^{{}}:{path}")
        assert hashlib.sha256(content).hexdigest() == expected["sha256"]
        assert len(content.splitlines()) == expected["lines"]


def test_current_entrypoints_registry_and_imports_cover_baseline() -> None:
    manifest = _manifest()
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        f'keystone = "{manifest["project_entrypoint"]}"'
        in pyproject
    )

    current_routes = {spec.route_name for spec in list_agent_specs()}
    assert set(manifest["registry_routes"]) <= current_routes

    parser = build_parser()
    command_action = next(
        action for action in parser._actions if getattr(action, "choices", None)
    )
    assert set(manifest["cli_commands"]) <= set(command_action.choices)

    for module_name, symbols in manifest["public_symbols"].items():
        module = importlib.import_module(module_name)
        for symbol in symbols:
            assert hasattr(module, symbol), f"{module_name}.{symbol} is missing"
