from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "codex-skills" / "kba-propagate-agent-fixes"


def test_propagation_skill_requires_shared_seam_and_cross_path_proof() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "$kba-agent-run-diagnosis" in text
    assert "SAME_DEFECT" in text
    assert "SHARED_SEAM_COVERED" in text
    assert "successful provider mutation followed by a failed final synthesis" in text
    assert "direct named-agent and orchestrated/WorkItem paths" in text
    assert "phrase matching" in text
    assert "inspect the Slack result and" in text


def test_propagation_skill_has_a_complete_matrix_and_ui_prompt() -> None:
    matrix = (SKILL_ROOT / "references" / "propagation-matrix.md").read_text(
        encoding="utf-8"
    )
    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    for seam in (
        "Canonical semantics",
        "Capability scope",
        "Direct runtime",
        "WorkItem and graph",
        "Provider tool",
        "Receipt and recovery",
        "Public result",
        "Persistence and trace",
    ):
        assert seam in matrix
    assert 'default_prompt: "Use $kba-propagate-agent-fixes ' in metadata
