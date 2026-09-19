"""Offline proof that Gmail reply decisions preserve the operator's event phase."""

from __future__ import annotations

from keystone_agents.gmail_triage.decision_ownership import (
    GmailDecisionEvidence,
    validate_gmail_agent_decision,
)
from keystone_agents.models import GmailTriageSDKInput
from keystone_agents.schemas.decision_ownership import (
    AgentDecisionRecord,
    DecisionCandidateAssessment,
)
from keystone_agents.schemas.email_triage import EmailTriageResult


def _evidence() -> GmailDecisionEvidence:
    candidate = {
        "message_id": "message-current",
        "thread_id": "thread-current",
        "received_at": "2026-08-03T23:53:57Z",
        "sender_name": "Synthetic Recruiter",
        "sender_email": "recruiter@example.com",
        "subject": "Interview conversation",
        "snippet": "Talk tomorrow at 10:30am ET.",
        "prior_labels": ["INBOX"],
    }
    return GmailDecisionEvidence(
        candidate_message_ids=("message-current",),
        candidate_thread_ids=("thread-current",),
        read_message_ids=(),
        read_thread_ids=("thread-current",),
        candidate_summaries=(candidate,),
        read_context_summaries=(candidate,),
        query_call_count=1,
        query_output_count=1,
        context_read_call_count=1,
        context_read_output_count=1,
    )


def _result(*, draft_reply: str, reasoning: str | None = None) -> EmailTriageResult:
    return EmailTriageResult(
        message_id="message-current",
        thread_id="thread-current",
        received_at="2026-08-03T23:53:57Z",
        subject="Interview conversation",
        category="collaboration_opportunity",
        confidence=0.9,
        reasoning=reasoning or "This is the selected human interview conversation.",
        needs_reply=True,
        recommended_action="Review the reply copy in Slack.",
        draft_reply=draft_reply,
        approval_required=True,
        decision=AgentDecisionRecord(
            decision_owner="specialist_agent",
            decision_stage="gmail_candidate_selection",
            selected_candidate_id="thread-current",
            candidate_assessments=[
                DecisionCandidateAssessment(
                    candidate_id="thread-current",
                    disposition="selected",
                    rationale="This is the human interview conversation.",
                )
            ],
            reasoning="This is the human interview conversation requested by the operator.",
        ),
    )


def test_completed_event_request_rejects_pre_event_reply_copy() -> None:
    outcome = validate_gmail_agent_decision(
        _result(
            draft_reply=(
                "Thanks for the update. I am glad the interview is set for tomorrow."
            )
        ),
        _evidence(),
        original_request="Help me follow up after my recent interview.",
    )

    assert outcome.status == "repair_required"
    assert outcome.reason_code == "gmail_reply_lifecycle_mismatch"
    assert "completed event" in outcome.feedback


def test_completed_event_request_accepts_post_event_reply_copy() -> None:
    outcome = validate_gmail_agent_decision(
        _result(
            draft_reply=(
                "Thank you again for the conversation. I enjoyed learning about the role."
            )
        ),
        _evidence(),
        original_request="I finished the interview and want to follow up.",
    )

    assert outcome.status == "accepted"
    assert outcome.reason_code == "agent_selection_bound_to_verified_candidate_set"


def test_upcoming_event_request_allows_future_scheduling_copy() -> None:
    outcome = validate_gmail_agent_decision(
        _result(
            draft_reply=(
                "Thanks for the update. I am glad the interview is set for tomorrow."
            )
        ),
        _evidence(),
        original_request="Confirm the interview I have tomorrow.",
    )

    assert outcome.status == "accepted"


def test_second_lifecycle_mismatch_fails_closed() -> None:
    outcome = validate_gmail_agent_decision(
        _result(
            draft_reply=(
                "Thanks for the update. I am glad the interview is set for tomorrow."
            )
        ),
        _evidence(),
        original_request="I completed my interview and need a follow-up.",
        repair_attempted=True,
    )

    assert outcome.status == "rejected"
    assert outcome.reason_code == "gmail_reply_lifecycle_mismatch"


def test_model_input_exposes_evaluation_time_and_relative_date_rule() -> None:
    prompt = GmailTriageSDKInput(
        subject="",
        body="",
        request="Find the thread from yesterday.",
        request_evaluated_at="2026-08-05T05:00:00Z",
    ).to_prompt()

    assert "Request evaluation time (UTC): 2026-08-05T05:00:00Z" in prompt
    assert "Operator timezone: America/New_York" in prompt
    assert "against that message's received_at timestamp" in prompt
