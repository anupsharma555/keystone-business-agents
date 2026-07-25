from __future__ import annotations

from pathlib import Path

from keystone_agents import manual_request
from keystone_agents.planning import compatibility

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "keystone_agents"


def test_legacy_manual_request_import_is_an_identity_facade() -> None:
    public_names = {
        name for name in vars(compatibility) if not name.startswith("_")
    }
    assert public_names
    for name in public_names:
        assert getattr(manual_request, name) is getattr(compatibility, name)


def test_internal_modules_import_the_bounded_compatibility_package() -> None:
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == PACKAGE_ROOT / "manual_request.py":
            continue
        if "keystone_agents.manual_request import" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == []
