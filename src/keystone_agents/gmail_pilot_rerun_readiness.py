"""Offline readiness contract for a fresh ANU-61 Gmail pilot observation."""

from __future__ import annotations

from dataclasses import dataclass

from keystone_agents.controlled_pilot import (
    controlled_pilot_cases,
    controlled_pilot_natural_request_sha256,
)
from keystone_agents.model_provider import OPENAI_BUSINESS_AGENT_DEFAULT_MODEL


@dataclass(frozen=True)
class GmailPilotRerunReadiness:
    case_id: str
    natural_request: str
    natural_request_sha256: str
    backend: str
    model: str
    max_openai_requests: int
    max_cost_usd: float
    proof_nodeids: tuple[str, ...]
    required_human_review_checks: tuple[str, ...]
    live_stop_conditions: tuple[str, ...]


def gmail_pilot_rerun_readiness() -> GmailPilotRerunReadiness:
    """Return the exact no-write rerun contract for the failed Gmail pilot row."""

    case = next(
        item
        for item in controlled_pilot_cases()
        if item.case_id == "selected_gmail_thread_followup"
    )
    if case.max_openai_requests != 2 or case.allowed_provider_writes != 0:
        raise ValueError("Gmail pilot rerun requires exactly two requests and zero writes.")
    return GmailPilotRerunReadiness(
        case_id=case.case_id,
        natural_request=case.natural_ask,
        natural_request_sha256=controlled_pilot_natural_request_sha256(case),
        backend=case.backend,
        model=OPENAI_BUSINESS_AGENT_DEFAULT_MODEL,
        max_openai_requests=case.max_openai_requests,
        max_cost_usd=case.max_cost_usd,
        proof_nodeids=(
            "tests/test_workflow_runner.py::"
            "test_live_gmail_retrieval_promotes_selected_thread_without_raw_body",
            "tests/test_langgraph_workflow.py::"
            "test_backend_selected_manager_loop_uses_graph_for_gmail_research_outreach_checkpoint",
            "tests/test_workflow_runner.py::"
            "test_recommendation_only_gmail_summary_does_not_imply_a_draft_exists",
            "tests/test_workflow_runner.py::"
            "test_recommendation_only_mismatch_does_not_spend_repair_call",
            "tests/test_workflow_runner.py::"
            "test_compact_outreach_sdk_context_excludes_full_nested_provider_objects",
        ),
        required_human_review_checks=(
            "Answer starts with the current conversation state, not route or model metadata.",
            "Recommended next step is KNI-specific and supported by the complete chronology.",
            "Reply copy appears only when replying now would move the relationship forward.",
            "No raw body, recipient, provider identity, or private thread detail is exposed.",
            "Exactly one final response is visible and no approval is invented.",
        ),
        live_stop_conditions=(
            "Stop before execution if the exact configured test-sender thread is "
            "unavailable or ambiguous.",
            "Stop on any retry or more than two OpenAI requests.",
            "Stop on live search, Gmail write, draft, send, label change, or "
            "unrelated provider action.",
            "Stop if provider identity or raw private content reaches persisted or "
            "Slack-visible output.",
            "Stop rather than repair manually when the first live output fails human review.",
        ),
    )


__all__ = ["GmailPilotRerunReadiness", "gmail_pilot_rerun_readiness"]
