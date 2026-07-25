from __future__ import annotations

from keystone_agents.execution_request import attach_execution_public_result


def test_failed_deterministic_review_cannot_publish_completed_result() -> None:
    payload = {
        "status": "done",
        "human_summary": "Reviewed email priorities are ready.",
        "orchestrator_review": {
            "status": "fail",
            "review_mode": "deterministic",
            "observed_gaps": ["synthetic internal diagnostic"],
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "failed"
    assert result.completion_confirmed is False
    assert result.failure_code == "orchestrator_review_failed"
    assert payload["public_result"]["status"] == "failed"
    assert payload["slack_display_title"] == "Business Agents Run Failed"
    assert payload["slack_display_text"] == (
        "Business Agents could not verify a reader-ready result."
    )
    assert "synthetic internal diagnostic" not in payload["slack_display_text"]


def test_failed_review_reconciles_an_existing_public_success_result() -> None:
    payload = {
        "orchestrator_review": {
            "status": "pass",
            "test_pack_checks": {"deterministic_review_status": "fail"},
        },
        "public_result": {
            "status": "verified",
            "title": "Business Agents Result Ready",
            "text": "Unreviewed result text.",
            "completion_confirmed": True,
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "failed"
    assert result.completion_confirmed is False
    assert payload["public_result"]["status"] == "failed"
    assert payload["slack_display_text"] != "Unreviewed result text."


def test_failed_review_after_verified_provider_mutation_is_partial() -> None:
    payload = {
        "status": "done",
        "human_summary": "The Gmail draft was created and verified.",
        "tool_receipt": {
            "operation": "create_draft",
            "verification": {"passed": True},
        },
        "orchestrator_review": {
            "status": "fail",
            "review_mode": "deterministic",
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "partial"
    assert result.completion_confirmed is False
    assert result.provider_write_attempted is True
    assert result.provider_receipt_verified is True
    assert payload["slack_display_title"] == "Business Agents Partially Completed"
    assert "completed and was verified" in payload["slack_display_text"]


def test_latest_review_pass_preserves_success_after_historical_failure() -> None:
    payload = {
        "status": "done",
        "human_summary": "The repaired result passed review.",
        "work_item": {
            "target": {
                "metadata": {
                    "orchestrator_reviews": [
                        {"status": "fail"},
                        {"status": "pass"},
                    ]
                }
            }
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "completed"
    assert result.completion_confirmed is True


def test_existing_non_success_state_is_not_collapsed_by_failed_review() -> None:
    for status in ("partial", "blocked", "failed"):
        payload = {
            "orchestrator_review": {"status": "fail"},
            "public_result": {
                "status": status,
                "title": "Existing terminal state",
                "text": "Existing safe reader-facing text.",
                "completion_confirmed": False,
                "provider_write_attempted": status == "partial",
                "provider_receipt_verified": True if status == "partial" else None,
            },
        }

        result = attach_execution_public_result(payload)

        assert result.status == status
        assert result.title == "Existing terminal state"
        assert result.text == "Existing safe reader-facing text."
