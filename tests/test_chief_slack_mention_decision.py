from __future__ import annotations

from pathlib import Path

DECISION_PATH = Path("docs/CHIEF_OF_STAFF_SLACK_MENTION_MODEL.md")


def test_chief_slack_mention_decision_preserves_central_execution_contract() -> None:
    text = DECISION_PATH.read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for required in (
        "defer a separate Slack profile or alias",
        "@KNI chief of staff",
        "Orchestrator preflight",
        "WorkItems",
        "authenticated direct operator command",
        "does not itself grant write authority",
        "raw Slack request",
        "selected route and backend",
        "WorkItem or run ID",
        "approval state",
        "no-send/no-write status",
        "keystone-slack",
        "ANU-60",
        "ANU-61",
        "ANU-124",
    ):
        assert required in normalized


def test_chief_slack_mention_decision_rejects_parallel_routing_authority() -> None:
    text = DECISION_PATH.read_text(encoding="utf-8").lower()

    assert "not separate routing or approval authorities" in text
    assert "must not fork the execution path" in text
    assert "raw private slack text, secrets, phi" in text
