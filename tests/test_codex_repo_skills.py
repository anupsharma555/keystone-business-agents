from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "codex-skills"
REPO_PATH = "<repo>"
EXPECTED_SKILLS = {
    "kba-agent-contract-change",
    "kba-eval-readiness-triage",
    "kba-new-agent",
    "kba-search-provider-eval",
    "kba-workitem-orchestrator-ops",
    "kba-live-sdk-smoke-and-cost",
}
LOCAL_PATH_PREFIXES = (
    "AGENTS.md",
    "README.md",
    "docs/",
    "scripts/",
    "src/",
    "tests/",
    "promptfoo/",
    "evals/",
    "contracts/",
    "codex-skills/",
)


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} missing YAML frontmatter"
    raw = text.split("---", 2)[1]
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


def _skill_dirs() -> list[Path]:
    return sorted(path.parent for path in SKILLS_ROOT.glob("*/SKILL.md"))


def _local_code_paths(text: str) -> set[str]:
    paths: set[str] = set()
    for token in re.findall(r"`([^`]+)`", text):
        cleaned = token.strip().strip(".,:;")
        if "*" in cleaned or "<" in cleaned or " " in cleaned:
            continue
        if cleaned.startswith(LOCAL_PATH_PREFIXES):
            paths.add(cleaned)
    return paths


def test_repo_local_codex_skills_are_registered_in_agents_guide() -> None:
    agents_text = (PROJECT_ROOT / "AGENTS.md").read_text()
    skill_names = {path.name for path in _skill_dirs()}

    assert skill_names == EXPECTED_SKILLS
    assert "## Repo-Local Codex Skills" in agents_text
    for skill_name in EXPECTED_SKILLS:
        assert f"codex-skills/{skill_name}/SKILL.md" in agents_text


def test_repo_local_codex_skills_are_repo_scoped_and_actionable() -> None:
    for skill_dir in _skill_dirs():
        skill_path = skill_dir / "SKILL.md"
        text = skill_path.read_text()
        frontmatter = _frontmatter(skill_path)

        assert frontmatter["name"] == skill_dir.name
        assert REPO_PATH in frontmatter["description"]
        assert "Use only in this repo" in frontmatter["description"]
        assert REPO_PATH in text
        assert "If the working directory is\nnot this repo" in text
        assert "## First Reads" in text
        assert "## Request Shapes" in text
        assert "## Workflow" in text
        assert "## Verification" in text
        assert "TODO" not in text
        assert "[TODO" not in text


def test_repo_local_codex_skill_ui_metadata_matches_skill_names() -> None:
    for skill_dir in _skill_dirs():
        metadata_path = skill_dir / "agents" / "openai.yaml"
        text = metadata_path.read_text()

        assert "display_name:" in text
        assert "short_description:" in text
        assert f"Use ${skill_dir.name} " in text


def test_repo_local_codex_skill_references_exist_and_point_to_existing_files() -> None:
    for skill_dir in _skill_dirs():
        skill_text = (skill_dir / "SKILL.md").read_text()
        reference_paths = sorted((skill_dir / "references").glob("*.md"))

        assert reference_paths, f"{skill_dir} should keep detailed workflow maps in references/"
        for reference_path in reference_paths:
            assert f"references/{reference_path.name}" in skill_text
            reference_text = reference_path.read_text()
            assert REPO_PATH in reference_text
            assert "TODO" not in reference_text
            assert "[TODO" not in reference_text

        reference_texts = "\n".join(p.read_text() for p in reference_paths)
        for path_text in _local_code_paths(skill_text + reference_texts):
            assert (PROJECT_ROOT / path_text).exists(), (
                f"{skill_dir.name} references missing {path_text}"
            )
