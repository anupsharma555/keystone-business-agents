from __future__ import annotations

import re
from pathlib import Path

from keystone_agents.skill_sets import SHARED_REASONING_SKILL_NAMES, SPECIALIST_SKILL_NAMES

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = PROJECT_ROOT / "docs" / "SKILL_INVENTORY.md"
ARCHITECTURE_PATH = PROJECT_ROOT / "docs" / "SKILLS_ARCHITECTURE.md"
CODEX_SKILLS_ROOT = PROJECT_ROOT / "codex-skills"
RUNTIME_SKILLS_ROOT = PROJECT_ROOT / "src" / "keystone_agents" / "skills"

ALLOWED_DISPOSITIONS = {
    "Keep",
    "Update now",
    "Split candidate",
    "Merge candidate",
    "Deprecate candidate",
}


def _skill_dirs(root: Path) -> set[str]:
    return {path.parent.name for path in root.glob("*/SKILL.md")}


def _inventory_disposition(text: str, skill_name: str) -> str:
    match = re.search(rf"^\| `{re.escape(skill_name)}` \| ([^|]+) \|", text, re.MULTILINE)
    assert match, f"{skill_name} is missing from docs/SKILL_INVENTORY.md"
    return match.group(1).strip()


def test_skill_inventory_covers_codex_and_runtime_skill_surfaces() -> None:
    text = INVENTORY_PATH.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    expected_skills = _skill_dirs(CODEX_SKILLS_ROOT) | _skill_dirs(RUNTIME_SKILLS_ROOT)

    for skill_name in expected_skills:
        assert _inventory_disposition(text, skill_name) in ALLOWED_DISPOSITIONS

    assert "ANU-171" in text
    assert "Orchestrator-first" in text
    assert "WorkItems remain the canonical business state" in normalized
    assert "optional LangGraph orchestration wraps WorkItem advancement" in normalized
    assert "`KEYSTONE_OPENAI_API_KEY`" in text
    assert "no-send, or no-write gates" in text


def test_skill_architecture_catalog_matches_current_shared_and_specialist_skills() -> None:
    text = ARCHITECTURE_PATH.read_text(encoding="utf-8")

    for skill_name in SHARED_REASONING_SKILL_NAMES:
        assert f"`{skill_name}`" in text
    for skill_name in SPECIALIST_SKILL_NAMES.values():
        assert f"`{skill_name}`" in text
    assert "`writing_style_adaptation`" in text
    assert "`request_to_specialist_brief`" in text
    assert "`docs/SKILL_INVENTORY.md`" in text
