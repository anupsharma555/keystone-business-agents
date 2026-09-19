from __future__ import annotations

import json
import subprocess

import pytest
from agents.exceptions import ModelBehaviorError

from keystone_agents.operator_failures import (
    known_exception_to_operator_failure,
    operator_failure_from_mapping,
)
from keystone_agents.runtime.execution_deadline import ExecutionDeadlineExceeded
from keystone_agents.sdk import ToolGuardrailViolation


def test_known_exception_to_operator_failure_redacts_and_classifies_guardrails() -> None:
    failure = known_exception_to_operator_failure(
        ToolGuardrailViolation("blocked token=SHOULD_NOT_APPEAR_000000000 by guardrail"),
        context="WorkItem run",
    )

    assert failure.kind == "guardrail_block"
    assert failure.to_dict()["schema"] == "keystone.operator_failure.v1"
    assert "safety guardrail" in failure.summary
    assert "[REDACTED]" in failure.reason
    assert failure.safe_to_continue is True
    assert "No send or external write" not in failure.summary


def test_known_exception_to_operator_failure_classifies_outer_deadline_truthfully() -> None:
    failure = known_exception_to_operator_failure(
        subprocess.TimeoutExpired(["agent"], timeout=30),
        context="Business Research SDK run",
    )

    assert failure.kind == "child_process_deadline_exceeded"
    assert failure.retryable is True
    assert "outer execution deadline" in failure.summary
    assert "provider" not in failure.summary.lower()


def test_soft_deadline_rejection_is_not_reported_as_a_provider_timeout() -> None:
    failure = known_exception_to_operator_failure(
        ExecutionDeadlineExceeded(
            stage="opportunity_scout:decision_repair",
            boundary="semantic_attempt",
            correlation_id="deadline-test",
            remaining_soft_ms=0,
            remaining_hard_ms=15_000,
        ),
        context="Opportunity Scout run",
    )

    assert failure.kind == "execution_soft_deadline_exceeded"
    assert "before starting more model or tool work" in failure.summary
    assert failure.retryable is False


def test_known_exception_to_operator_failure_preserves_provider_timeout_classification() -> None:
    class ProviderTimeout(RuntimeError):
        pass

    failure = known_exception_to_operator_failure(
        ProviderTimeout("search provider timeout after 10 seconds"),
        context="Business Research SDK run",
    )

    assert failure.kind == "provider_timeout"
    assert failure.retryable is True


def test_known_exception_to_operator_failure_classifies_workspace_scope_blocker() -> None:
    class GoogleWorkspaceScopeError(RuntimeError):
        pass

    failure = known_exception_to_operator_failure(
        GoogleWorkspaceScopeError("invalid_scope for presentations"),
        context="Google Workspace Context Agent run",
    )

    assert failure.kind == "provider_authorization_scope"
    assert failure.retryable is False
    assert "Reauthorize only the named provider service" in failure.next_step


def test_known_exception_to_operator_failure_classifies_max_turns_without_workitem_copy() -> None:
    MaxTurnsExceeded = type("MaxTurnsExceeded", (RuntimeError,), {})
    failure = known_exception_to_operator_failure(
        MaxTurnsExceeded("Max turns (3) exceeded"),
        context="Google Workspace Context Agent run",
    )

    assert failure.kind == "model_tool_turns_exhausted"
    assert "WorkItem" not in failure.next_step
    assert "last tool result" in failure.next_step


def test_operator_failure_from_mapping_round_trips_current_schema() -> None:
    failure = operator_failure_from_mapping(
        {
            "schema": "keystone.operator_failure.v1",
            "kind": "missing_credentials",
            "summary": "A credential is missing.",
            "reason": "OPENAI_API_KEY=SHOULD_NOT_APPEAR_1234567890",
            "next_step": "Configure credentials.",
            "safe_to_continue": True,
        }
    )

    assert failure is not None
    assert failure.kind == "missing_credentials"
    assert "[REDACTED]" in failure.reason
    assert failure.safe_to_continue is True


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("model_output_limit_reached", "output limit"),
        ("model_response_incomplete", "incomplete model response"),
        ("model_response_failed", "failed model response"),
    ],
)
def test_provider_terminal_failure_uses_safe_metadata_not_model_error_prose(kind, expected):
    canary = "PRIVATE_TERMINAL_SOURCE_JSON_CREDENTIAL_TIMEOUT"
    exc = ModelBehaviorError(canary)
    exc.keystone_sdk_run_failure = {
        "failure_kind": kind,
        "request_cache": {
            "response_terminal": {
                "schema": "keystone.response_terminal.v1",
                "observations": [
                    {
                        "status": "incomplete",
                        "reason": "max_output_tokens",
                        "max_output_tokens": 6000,
                        "untrusted_text": canary,
                    }
                ],
            }
        },
    }
    for include_reason in (False, True):
        failure = known_exception_to_operator_failure(
            exc,
            context="Opportunity Scout run",
            include_exception_reason=include_reason,
        )
        assert failure.kind == kind and expected in failure.summary
        assert failure.reason == "" and failure.retryable is False
        text = json.dumps(failure.to_dict())
        assert canary not in text
        assert "schema" not in failure.summary.lower()
        assert "missing context" not in text.lower()
        assert "No send" not in text
