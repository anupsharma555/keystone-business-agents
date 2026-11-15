"""Prior owner hints must not replace a coherent current capability operation."""

from __future__ import annotations

import pytest

from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.planning.compatibility import infer_manual_request_plan

REQUESTS = (
    (
        "Remember this preference for future runs in this channel: keep state and cross-session "
        "memory compact, retrieve only relevant approved facts, and reuse cached source material "
        "when appropriate. Save it as an internal preference and confirm briefly.",
        "reference_capture",
    ),
    ("Save this reference for later: https://example.org/guide", "reference_capture"),
    (
        "Store this project constraint: use a reversible local review before adoption.",
        "reference_capture",
    ),
    (
        "Inspect this public page using backend browser diagnostics: https://example.org/dashboard",
        "browser_diagnostics",
    ),
)


@pytest.mark.parametrize(("request_text", "intent"), REQUESTS)
@pytest.mark.parametrize("prior", ["none", "same_owner", "different_owner", "provider_affinity"])
def test_current_semantic_operation_survives_old_owner_advice(request_text, intent, prior):
    fallback = infer_manual_request_plan(request_text, requested_agent="chief_of_staff")
    assert fallback.intent == intent
    state = {"slack_thread_root": request_text}
    if prior != "none":
        state["execution_continuation"] = {
            "prior_agent": "chief_of_staff" if prior == "same_owner" else "gmail_triage",
            "provider_affinity": "gmail" if prior == "provider_affinity" else "",
        }
    resolved = resolve_manual_request_plan(
        request_text, requested_agent="chief_of_staff", workflow_state=state,
    )
    assert resolved.model_dump(mode="json") == fallback.model_dump(mode="json")
    assert resolved.provider_system == "unspecified"
    assert resolved.provider_operations == []


def test_owner_hint_still_resolves_a_genuinely_context_dependent_request():
    resolved = resolve_manual_request_plan(
        "What does the sender need?", requested_agent="chief_of_staff",
        workflow_state={
            "execution_continuation": {"prior_agent": "gmail_triage", "provider_affinity": "gmail"},
        },
    )
    assert resolved.target_agent == "gmail_triage"
    assert resolved.intent == "gmail_triage"
    assert resolved.provider_system == "gmail"
