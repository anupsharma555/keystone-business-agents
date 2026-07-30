from __future__ import annotations

from pathlib import Path

from keystone_agents import semantic_execution
from keystone_agents.authority import semantic

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "keystone_agents"


def test_legacy_semantic_execution_import_is_an_identity_facade() -> None:
    for name in semantic.__all__:
        assert getattr(semantic_execution, name) is getattr(semantic, name)


def test_internal_modules_import_the_canonical_authority_package() -> None:
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == PACKAGE_ROOT / "semantic_execution.py":
            continue
        if "from keystone_agents.semantic_execution import" in path.read_text(
            encoding="utf-8"
        ):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == []
