from __future__ import annotations

import subprocess

from keystone_agents.operator_failures import (
    known_exception_to_operator_failure,
    operator_failure_from_mapping,
)
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


def test_known_exception_to_operator_failure_classifies_timeout_as_retryable() -> None:
    failure = known_exception_to_operator_failure(
        subprocess.TimeoutExpired(["agent"], timeout=30),
        context="Business Research SDK run",
    )

    assert failure.kind == "provider_timeout"
    assert failure.retryable is True
    assert "timed out" in failure.summary


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
