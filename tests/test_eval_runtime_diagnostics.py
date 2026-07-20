from __future__ import annotations

from keystone_agents.eval_runtime_diagnostics import (
    slack_eval_blocker_diagnostics,
    slack_eval_child_step_summary,
)


def test_slack_eval_blocker_diagnostics_preserves_actionable_block_state() -> None:
    diagnostics = slack_eval_blocker_diagnostics(
        {
            "status": "blocked",
            "block_kind": "missing_context",
            "block_reason": "Select the exact Gmail thread before continuing.",
            "blockers": [
                {
                    "code": "gmail_context_required",
                    "message": "The selected Gmail thread is required.",
                }
            ],
            "next_safe_action": "Select one Gmail thread and retry.",
            "context_pack": {
                "readiness_gates": [
                    {"name": "gmail_thread_readiness", "ready": False},
                    {"name": "external_use_approval", "ready": True},
                ]
            },
        }
    )

    assert diagnostics["diagnostic_category"] == "workflow_blocker"
    assert diagnostics["block_kind"] == "missing_context"
    assert diagnostics["block_reason"] == (
        "Select the exact Gmail thread before continuing."
    )
    assert diagnostics["blocker_codes"] == ["gmail_context_required"]
    assert diagnostics["readiness_gate_names"] == ["gmail_thread_readiness"]
    assert diagnostics["next_action"]["description"] == (
        "Select one Gmail thread and retry."
    )


def test_slack_eval_blocker_diagnostics_redacts_sensitive_text() -> None:
    diagnostics = slack_eval_blocker_diagnostics(
        {
            "status": "blocked",
            "block_reason": (
                "Email jane@example.com with token=SHOULD_NOT_APPEAR_123456789"
            ),
        }
    )

    assert "SHOULD_NOT_APPEAR" not in str(diagnostics)
    assert "jane@example.com" not in str(diagnostics)
    assert "[REDACTED_EMAIL]" in diagnostics["block_reason"]
    assert "[REDACTED]" in diagnostics["block_reason"]


def test_slack_eval_child_steps_are_bounded_and_exclude_raw_payloads() -> None:
    steps = slack_eval_child_step_summary(
        {
            "execution_steps": [
                {
                    "step_index": 1,
                    "category": "model",
                    "name": "workflow_sdk_usage",
                    "status": "completed",
                    "provider": "openai",
                    "request_count": 1,
                    "estimated_cost_usd": 0.01,
                    "prompt": "raw prompt must not persist",
                    "output": "raw output must not persist",
                }
            ]
        },
        {
            "tool_call_summary": [
                {
                    "name": "search_web",
                    "count": 1,
                    "failed_count": 0,
                    "status": "completed",
                    "arguments": {"query": "private query"},
                }
            ]
        },
    )

    assert [step["category"] for step in steps] == ["model", "tool"]
    assert steps[0]["provider"] == "openai"
    assert steps[1]["name"] == "search_web"
    assert "raw prompt" not in str(steps)
    assert "raw output" not in str(steps)
    assert "private query" not in str(steps)
