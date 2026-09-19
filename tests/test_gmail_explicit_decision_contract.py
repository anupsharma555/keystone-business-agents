"""Offline contract tests for model-owned Gmail semantic decisions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.gmail_triage.decision_ownership import (
    GmailDecisionEvidence,
    decision_record_from_gmail_result,
    gmail_decision_evidence,
    normalize_empty_gmail_abstention,
    validate_gmail_agent_decision,
    validate_verified_gmail_continuation_decision,
)
from keystone_agents.schemas.email_triage import EmailTriageResult


def _evidence() -> GmailDecisionEvidence:
    candidates = (
        {
            "message_id": "msg-current",
            "thread_id": "thread-current",
            "received_at": "2026-08-03T14:00:00Z",
            "sender_name": "Synthetic Recruiting",
            "sender_email": "recruiting@example.com",
            "subject": "Current interview invitation",
            "snippet": "The current interview remains scheduled.",
            "prior_labels": ["INBOX"],
        },
        {
            "message_id": "msg-old",
            "thread_id": "thread-old",
            "received_at": "2026-08-01T14:00:00Z",
            "sender_name": "Synthetic Recruiting",
            "sender_email": "recruiting@example.com",
            "subject": "Canceled interview invitation",
            "snippet": "The earlier interview was canceled.",
            "prior_labels": ["INBOX"],
        },
    )
    return GmailDecisionEvidence(
        candidate_message_ids=("msg-current", "msg-old"),
        candidate_thread_ids=("thread-current", "thread-old"),
        read_message_ids=("msg-current",),
        read_thread_ids=("thread-current",),
        candidate_summaries=candidates,
        read_context_summaries=(candidates[0],),
        query_call_count=1,
        query_output_count=1,
        context_read_call_count=1,
        context_read_output_count=1,
    )


def _decision(
    *,
    owner: str = "specialist_agent",
    stage: str = "gmail_candidate_selection",
    reasoning: str = "The active invitation matches the requested meeting.",
    needs_more_context: bool = False,
    select: bool = True,
) -> dict[str, Any]:
    selected = [
        {
            "candidate_id": "thread-current",
            "disposition": "selected",
            "rationale": "It is the current active invitation.",
        },
        {
            "candidate_id": "thread-old",
            "disposition": "excluded",
            "rationale": "It is an obsolete canceled invitation.",
        },
    ]
    return {
        "decision_owner": owner,
        "decision_stage": stage,
        "selected_candidate_id": "thread-current" if select else "",
        "selected_candidate_ids": ["thread-current"] if select else [],
        "candidate_assessments": selected if select else [],
        "reasoning": reasoning,
        "limitations": ["Only bounded sanitized Gmail context was read."],
        "needs_more_context": needs_more_context,
    }


def _result_payload(
    *,
    include_decision: bool = True,
    decision: dict[str, Any] | None = None,
    selected_output: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_id": "msg-current" if selected_output else "",
        "thread_id": "thread-current" if selected_output else "",
        "received_at": "2026-08-03T14:00:00Z" if selected_output else "",
        "subject": "Current interview invitation" if selected_output else "",
        "sender_name": "Synthetic Recruiting" if selected_output else "",
        "sender_email": "recruiting@example.com" if selected_output else "",
        "category": "collaboration_opportunity" if selected_output else "unrelated",
        "confidence": 0.95 if selected_output else 0.0,
        "priority": "high" if selected_output else "normal",
        "summary": "Current invitation selected." if selected_output else "No selection made.",
        "reasoning": (
            "The current invitation is relevant."
            if selected_output
            else "More context is needed."
        ),
        "needs_reply": selected_output,
        "recommended_labels": ["Keystone/Triage"] if selected_output else [],
        "risk_flags": [],
        "suspicious_signals": [],
        "recommended_next_agent": "human_review",
        "triage_limitations": [],
        "prior_labels": [],
        "snippet": "The interview remains scheduled." if selected_output else "",
        "normalized_body": "",
        "extracted_links": [],
        "attachment_metadata": [],
        "recommended_action": (
            "Review the reply copy." if selected_output else "Ask for more identifying context."
        ),
        "draft_reply": None,
        "draft_created": False,
        "style_profile_used": False,
        "style_profile_id": "",
        "approval_required": False,
        "requires_human_review": True,
    }
    if include_decision:
        payload["decision"] = decision if decision is not None else _decision()
    payload.update(overrides)
    return payload


def _result(**kwargs: Any) -> EmailTriageResult:
    return EmailTriageResult.model_validate(_result_payload(**kwargs))


def _continuation_raw_result() -> SimpleNamespace:
    return SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="read-current",
                tool_name="read_gmail_context",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="read-current",
                output=json.dumps(
                    {
                        "status": "read",
                        "resource_type": "thread",
                        "resource_id": "thread-current",
                    }
                ),
            ),
        ]
    )


def test_output_identity_does_not_become_a_python_authored_decision() -> None:
    result = _result(include_decision=False)

    decision = decision_record_from_gmail_result(result)
    outcome = validate_gmail_agent_decision(result, _evidence())

    assert decision is result.decision
    assert decision.selected_candidate_ids == []
    assert decision.decision_stage == "specialist_selection"
    assert outcome.status == "repair_required"
    assert outcome.reason_code == "decision_record_not_explicitly_returned"


@pytest.mark.parametrize(
    ("decision", "reason_code"),
    [
        (_decision(owner="orchestrator"), "gmail_decision_owner_mismatch"),
        (_decision(stage="specialist_selection"), "gmail_decision_stage_mismatch"),
        (_decision(reasoning=""), "gmail_decision_reasoning_missing"),
    ],
)
def test_normal_selection_requires_agent_owner_stage_and_reasoning(
    decision: dict[str, Any],
    reason_code: str,
) -> None:
    outcome = validate_gmail_agent_decision(
        _result(decision=decision),
        _evidence(),
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == reason_code


def test_output_identity_is_not_used_when_explicit_decision_selects_nothing() -> None:
    result = _result(decision=_decision(select=False))

    outcome = validate_gmail_agent_decision(result, _evidence())

    assert result.message_id == "msg-current"
    assert outcome.status == "repair_required"
    assert outcome.reason_code == "missing_decision_selection"


def test_missing_selection_feedback_leads_to_explicit_model_owned_selection() -> None:
    missing = _result(decision=_decision(select=False))

    rejected = validate_gmail_agent_decision(missing, _evidence())

    assert rejected.reason_code == "missing_decision_selection"
    assert "decision.selected_candidate_ids" in rejected.feedback
    assert "decision.selected_candidate_id" in rejected.feedback

    model_decision = _decision()
    corrected = _result(decision=model_decision)
    accepted = validate_gmail_agent_decision(corrected, _evidence())

    assert "selected_candidate_ids" in corrected.decision.model_fields_set
    assert corrected.decision.selected_candidate_ids == model_decision[
        "selected_candidate_ids"
    ]
    assert accepted.status == "accepted"
    assert accepted.reason_code == "agent_selection_bound_to_verified_candidate_set"


@pytest.mark.parametrize(
    "claimed_output",
    [
        {"message_id": "msg-current", "thread_id": "thread-current"},
        {"draft_reply": "Looking forward to it.", "approval_required": True},
        {"needs_reply": True},
        {"recommended_labels": ["Keystone/Triage"]},
    ],
)
def test_needs_more_context_cannot_claim_provider_or_action_output(
    claimed_output: dict[str, Any],
) -> None:
    result = _result(
        selected_output=False,
        decision=_decision(needs_more_context=True, select=False),
        **claimed_output,
    )

    outcome = validate_gmail_agent_decision(result, _evidence())

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "needs_more_context_with_claimed_gmail_output"


def test_needs_more_context_is_accepted_only_without_claimed_output() -> None:
    result = _result(
        selected_output=False,
        decision=_decision(needs_more_context=True, select=False),
    )

    outcome = validate_gmail_agent_decision(result, _evidence())

    assert outcome.status == "accepted"
    assert outcome.reason_code == "agent_requested_more_context"


def test_valid_explicit_agent_selection_is_bound_to_provider_evidence() -> None:
    outcome = validate_gmail_agent_decision(_result(), _evidence())

    assert outcome.status == "accepted"
    assert outcome.reason_code == "agent_selection_bound_to_verified_candidate_set"
    assert outcome.selected_candidate_id == "thread-current"


def test_blocked_fifth_context_attempt_does_not_poison_four_context_decision() -> None:
    evidence = _evidence()
    evidence = GmailDecisionEvidence(
        **{
            **evidence.__dict__,
            "context_read_call_count": 4,
            "context_read_output_count": 4,
            "model_context_read_call_count": 5,
            "blocked_context_read_call_count": 1,
        }
    )

    outcome = validate_gmail_agent_decision(_result(), evidence)

    assert outcome.status == "accepted"
    assert outcome.reason_code == "agent_selection_bound_to_verified_candidate_set"
    assert evidence.repair_context()["model_context_read_call_count"] == 5
    assert evidence.repair_context()["blocked_context_read_call_count"] == 1


def test_blocked_third_query_does_not_poison_two_distinct_query_decision() -> None:
    evidence = _evidence()
    evidence = GmailDecisionEvidence(
        **{
            **evidence.__dict__,
            "query_call_count": 2,
            "query_output_count": 2,
            "corrective_query_count": 1,
            "model_query_call_count": 3,
            "blocked_query_call_count": 1,
        }
    )

    outcome = validate_gmail_agent_decision(_result(), evidence)

    assert outcome.status == "accepted"
    assert outcome.reason_code == "agent_selection_bound_to_verified_candidate_set"
    assert evidence.repair_context()["blocked_query_call_count"] == 1


def test_mixed_message_and_thread_reads_form_one_canonical_decision_universe() -> None:
    evidence = _evidence()
    evidence = GmailDecisionEvidence(
        **{
            **evidence.__dict__,
            "read_message_ids": ("msg-old",),
            "read_thread_ids": ("thread-current",),
            "context_read_call_count": 2,
            "context_read_output_count": 2,
        }
    )

    assert evidence.decision_candidate_ids == ("thread-current", "thread-old")


def test_unknown_candidate_assessment_identity_is_rejected() -> None:
    decision = _decision()
    decision["candidate_assessments"] = [
        *decision["candidate_assessments"],
        {
            "candidate_id": "thread-never-returned",
            "disposition": "excluded",
            "rationale": "This identity was not returned by Gmail.",
        },
    ]

    outcome = validate_gmail_agent_decision(
        _result(decision=decision),
        _evidence(),
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "candidate_assessment_identity_not_in_set"
    assert outcome.selected_identity_in_candidate_set is True
    assert outcome.selected_identity_was_read is True


def test_assessment_failure_preserves_validated_selection_proof() -> None:
    decision = _decision()
    decision["candidate_assessments"] = [
        {
            "candidate_id": "thread-current",
            "disposition": "selected",
            "rationale": "The selected thread is current.",
        }
    ]
    evidence = _evidence()
    evidence = GmailDecisionEvidence(
        **{
            **evidence.__dict__,
            "read_message_ids": ("msg-current", "msg-old"),
            "read_thread_ids": ("thread-current", "thread-old"),
            "context_read_call_count": 2,
            "context_read_output_count": 2,
        }
    )

    outcome = validate_gmail_agent_decision(
        _result(decision=decision),
        evidence,
    )

    assert outcome.reason_code == "candidate_assessments_incomplete"
    assert outcome.selected_identity_in_candidate_set is True
    assert outcome.selected_identity_was_read is True


def test_live_selection_rejects_fixture_query_evidence() -> None:
    outcome = validate_gmail_agent_decision(
        _result(),
        _evidence(),
        require_live_provider=True,
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "gmail_live_query_evidence_missing"


def test_live_selection_rejects_fixture_context_after_live_query() -> None:
    evidence = _evidence()
    evidence = GmailDecisionEvidence(
        **{
            **evidence.__dict__,
            "query_provider_read_performed": True,
        }
    )

    outcome = validate_gmail_agent_decision(
        _result(),
        evidence,
        require_live_provider=True,
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "gmail_live_context_evidence_missing"


def test_live_selection_accepts_verified_query_and_context_reads() -> None:
    evidence = _evidence()
    evidence = GmailDecisionEvidence(
        **{
            **evidence.__dict__,
            "query_provider_read_performed": True,
            "context_provider_read_count": 1,
        }
    )

    outcome = validate_gmail_agent_decision(
        _result(),
        evidence,
        require_live_provider=True,
    )

    assert outcome.status == "accepted"
    assert outcome.reason_code == "agent_selection_bound_to_verified_candidate_set"


def test_second_invalid_attempt_is_rejected_without_python_substitution() -> None:
    outcome = validate_gmail_agent_decision(
        _result(decision=_decision(select=False)),
        _evidence(),
        repair_attempted=True,
    )

    assert outcome.status == "rejected"
    assert outcome.reason_code == "missing_decision_selection"


def test_verified_continuation_requires_one_explicit_exact_agent_decision() -> None:
    valid_decision = _decision(
        stage="gmail_verified_continuation",
    )
    valid_decision["candidate_assessments"] = [
        {
            "candidate_id": "thread-current",
            "disposition": "selected",
            "rationale": "This is the exact previously verified thread.",
        }
    ]
    accepted = validate_verified_gmail_continuation_decision(
        _result(decision=valid_decision),
        _continuation_raw_result(),
        expected_message_id="msg-current",
        expected_thread_id="thread-current",
    )
    omitted = validate_verified_gmail_continuation_decision(
        _result(include_decision=False),
        _continuation_raw_result(),
        expected_message_id="msg-current",
        expected_thread_id="thread-current",
    )

    assert accepted.status == "accepted"
    assert accepted.reason_code == "verified_gmail_continuation_read_and_preserved"
    assert omitted.status == "repair_required"
    assert omitted.reason_code == "verified_gmail_continuation_mismatch"


def _empty_query_entries():
    return tuple({
        "tool_name": "query_gmail_message_summaries",
        "arguments": {"query": query, "label": "", "max_results": 4},
        "output": {"status": "read", "provider_read_performed": True, "items": []},
    } for query in ("subject:planning", "planning"))


def _empty_abstention():
    return _result(
        selected_output=False, decision=_decision(select=False, needs_more_context=True),
        thread_summary="UNVERIFIED_SUMMARY", thread_context="UNVERIFIED_CONTEXT",
        normalized_body="UNVERIFIED_BODY",
    )


def test_verified_empty_abstention_discards_context_without_another_model_call(monkeypatch):
    from keystone_agents.agents import gmail_triage as agent
    from keystone_agents.models import GmailTriageSDKInput, TypedAgentRunResult

    initial = _empty_abstention()
    typed = TypedAgentRunResult(
        agent_name="gmail_triage", output=initial, raw_result=SimpleNamespace(new_items=[]),
        live=True, usage={"requests": 3},
    )
    monkeypatch.setattr(
        agent, "run_typed_sdk_agent",
        lambda **_kwargs: pytest.fail("Empty-result metadata must not trigger a model repair"),
    )
    result = agent._validate_and_repair_gmail_selection(
        typed, typed_input=GmailTriageSDKInput(subject="", body="", request="Find planning email"),
        run_config=None, live=True, model=None, session=None, compact_instructions=False,
        repair_invalid_selection=True, cumulative_tool_evidence=_empty_query_entries(),
    )
    assert result.output.decision == initial.decision
    assert result.output.reasoning == initial.reasoning
    assert result.output.summary == initial.summary
    assert result.output.thread_summary == result.output.thread_context == ""
    assert result.output.normalized_body == ""
    assert "UNVERIFIED" not in result.output.model_dump_json()
    assert initial.normalized_body == "UNVERIFIED_BODY"
    assert result.usage == {"requests": 3}
    assert result.request_cache.get("decision_repairs", 0) == 0
    telemetry = result.request_cache["decision_ownership"]
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert telemetry["validator_outcome"]["repair_attempted"] is False
    assert telemetry["repair_metadata_normalizations"] == [{
        "kind": "verified_empty_search_context_discarded",
        "fields": ["thread_summary", "thread_context", "normalized_body"],
    }]


@pytest.mark.parametrize("conflict", [
    {"message_id": "invented-message"}, {"thread_id": "invented-thread"},
    {"subject": "Invented subject"}, {"sender_email": "sender@example.test"},
    {"sender_name": "Synthetic Sender"},
    {"draft_reply": "Unverified reply", "approval_required": True},
    {"draft_created": True}, {"needs_reply": True}, {"recommended_labels": ["Important"]},
    {"recommended_next_agent": "outreach_composer"},
    {"decision": _decision(select=True, needs_more_context=True)},
])
def test_empty_search_normalization_never_clears_identity_or_action_claims(conflict):
    initial = EmailTriageResult.model_validate({**_empty_abstention().model_dump(), **conflict})
    evidence = gmail_decision_evidence(
        SimpleNamespace(new_items=[]), cumulative_tool_evidence=_empty_query_entries(),
    )
    normalized, fields = normalize_empty_gmail_abstention(initial, evidence)
    assert normalized is initial and not fields
    assert validate_gmail_agent_decision(
        normalized, evidence, require_live_provider=True,
    ).status == "repair_required"


@pytest.mark.parametrize("change", [
    {"query_provider_read_performed": False}, {"query_attempts": ()},
    {"query_output_count": 0}, {"candidate_message_ids": ("candidate",)},
    {"context_read_call_count": 1},
])
def test_empty_search_normalization_requires_actual_empty_provider_evidence(change):
    from dataclasses import replace

    evidence = gmail_decision_evidence(
        SimpleNamespace(new_items=[]), cumulative_tool_evidence=_empty_query_entries(),
    )
    initial = _empty_abstention()
    normalized, fields = normalize_empty_gmail_abstention(initial, replace(evidence, **change))
    assert normalized is initial and not fields


def test_repair_evidence_preserves_bounded_thread_chronology_and_source():
    source_url = "https://mail.google.com/mail/?authuser=reader%40example.test#all/thread-selected"
    timeline = [{
        "message_id": f"message-{i}", "thread_id": "thread-selected",
        "received_at": f"2026-01-{i + 1:02d}T12:00:00Z",
        "sender_email": "sender@example.test" if i == 0 else "reader@example.test",
        "subject": "Correspondence", "snippet": "Welcome" if i == 0 else "Following up",
        "prior_labels": ["INBOX"] if i == 0 else ["SENT"],
    } for i in range(14)]
    payload = {
        "status": "read", "provider_read_performed": True,
        "resource_type": "thread", "resource_id": "thread-selected",
        "messages": timeline, "source_url": source_url,
        "triage_limitations": ["Only a bounded timeline is available."],
    }
    evidence = gmail_decision_evidence(SimpleNamespace(new_items=[]), cumulative_tool_evidence=({
        "tool_name": "read_gmail_context", "output": payload,
    },))
    replay = evidence.repair_context()["verified_contexts"][0]
    assert replay["source_url"] == source_url
    assert replay["triage_limitations"] == payload["triage_limitations"]
    assert len(replay["messages"]) == 12
    for expected, actual in zip(timeline[:12], replay["messages"], strict=True):
        for field in ("message_id", "received_at", "sender_email", "snippet", "prior_labels"):
            assert actual[field] == expected[field]
    # Foreign/malformed records cannot add identities or arbitrary provider bodies.
    payload["messages"] = [
        {**timeline[0], "thread_id": "foreign-thread"},
        {**timeline[0], "body": "RAW_BODY_MUST_NOT_ENTER_REPAIR"},
        {"message_id": "missing-thread"},
    ]
    rejected = gmail_decision_evidence(SimpleNamespace(new_items=[]), cumulative_tool_evidence=({
        "tool_name": "read_gmail_context", "output": payload,
    },))
    assert rejected.repair_context()["verified_contexts"][0]["messages"] == []
