from __future__ import annotations

import ast
import base64
import json
import sys
from email import message_from_bytes
from pathlib import Path
from types import SimpleNamespace

import pytest

from keystone_agents.agents.gmail_triage import (
    EmailFixture,
    build_gmail_contact_lookup_agent,
    build_gmail_triage_agent,
    group_gmail_envelopes_fixture,
    run_gmail_triage_fixture,
    triage_email_fixture,
    triage_gmail_message_envelope,
)
from keystone_agents.gmail_triage.contact_lookup import (
    GmailContactBindingError,
    bind_gmail_contact_lookup_result,
    gmail_contact_lookup_human_summary,
    gmail_contact_lookup_receipt,
)
from keystone_agents.gmail_triage.execution_plan import resolve_gmail_execution_plan
from keystone_agents.gmail_triage.priority_grouping import (
    gmail_priority_grouping_human_summary,
    rank_gmail_candidates_for_request,
    run_gmail_priority_grouping_workflow,
)
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.models import (
    GmailContactLookupSDKInput,
    GmailPriorityGroupingSDKInput,
    GmailTriageSDKInput,
    TypedAgentRunResult,
)
from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.email_triage import (
    GMAIL_PRIMARY_LABEL_SET,
    EmailTriageResult,
    GmailAttachmentMetadata,
    GmailCandidateRankingItem,
    GmailCandidateRankingResult,
    GmailClarificationResult,
    GmailContactLookupResult,
    GmailMessageEnvelope,
    GmailPriorityGroupedMessage,
    GmailPriorityGroupingResult,
    GmailResolvedContact,
    GmailThreadSummaryMessage,
    GmailThreadSummaryResult,
    normalize_managed_gmail_labels,
)
from keystone_agents.schemas.gmail_execution_plan import GmailExecutionPlan
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import gmail_tool
from keystone_agents.tools.email_style_tool import load_email_style_profile_fixture
from keystone_agents.tools.gmail_tool import (
    GMAIL_SCOPES,
    GmailConfigurationError,
    GmailTool,
    gmail_message_envelope_from_api,
    gmail_oauth_readiness,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def test_google_oauth_scope_supports_selected_shared_calendar_reads() -> None:
    assert "https://www.googleapis.com/auth/calendar.calendarlist.readonly" in GMAIL_SCOPES


def _fixture(name: str) -> Path:
    return FIXTURES / name


def test_gmail_semantic_candidate_ranking_uses_current_ask_and_binds_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator_request = (
        "Pick one email from today that is worth a KNI follow-up and draft the reply "
        "in this Slack thread only."
    )
    observed: dict[str, object] = {}
    summaries = [
        GmailThreadSummaryResult(
            thread_id="thread-auto",
            subject="Register for our AI event",
            summary="Automated event promotion with an unsubscribe link.",
            thread_context="Automated event promotion with an unsubscribe link.",
            message_count=1,
            latest_received_at="2026-07-25T14:00:00Z",
            messages=[
                GmailThreadSummaryMessage(
                    message_id="message-auto",
                    sender_name="Events",
                    sender_email="newsletter@example.test",
                    subject="Register for our AI event",
                    snippet="Register now and unsubscribe at any time.",
                )
            ],
        ),
        GmailThreadSummaryResult(
            thread_id="thread-human",
            subject="Clinical AI evaluation",
            summary="A health-tech founder asked about a possible KNI evaluation.",
            thread_context=(
                "The sender asked whether KNI could discuss evaluating a clinical AI "
                "workflow."
            ),
            message_count=1,
            latest_received_at="2026-07-25T13:00:00Z",
            messages=[
                GmailThreadSummaryMessage(
                    message_id="message-human",
                    sender_name="Jamie Lee",
                    sender_email="jamie@example.test",
                    subject="Clinical AI evaluation",
                    snippet="Could we discuss an evaluation of our clinical AI workflow?",
                )
            ],
        ),
    ]
    model_output = GmailCandidateRankingResult(
        request_summary="model-owned text is not authoritative",
        source_message_count=2,
        candidates=[
            GmailCandidateRankingItem(
                message_id="message-human",
                disposition="candidate",
                relevance_score=0.96,
                reasoning="The sender made a concrete, relevant collaboration request.",
                needs_reply=True,
            ),
            GmailCandidateRankingItem(
                message_id="message-auto",
                disposition="exclude",
                relevance_score=0.05,
                reasoning="This is an automated event promotion.",
            )
        ],
    )

    def fake_run(typed_input, **kwargs):
        observed["operator_request"] = typed_input.operator_request
        observed["source_label"] = typed_input.source_label
        observed["live"] = kwargs["live"]
        return TypedAgentRunResult(
            agent_name="gmail_triage",
            output=model_output,
            raw_result=SimpleNamespace(),
            live=True,
            usage={"requests": 1},
        )

    monkeypatch.setattr(
        "keystone_agents.gmail_triage.priority_grouping.run_gmail_candidate_ranking_sdk",
        fake_run,
    )

    ranking = rank_gmail_candidates_for_request(
        operator_request=operator_request,
        summaries=summaries,
        live_sdk=True,
    )

    assert observed == {
        "operator_request": operator_request,
        "source_label": "BOUNDED_PROVIDER_RESULT",
        "live": True,
    }
    assert ranking.ranked_thread_ids == ("thread-human",)
    assert ranking.result.candidates[0].message_id == "message-human"
    assert ranking.result.candidates[0].disposition == "candidate"
    assert ranking.result.candidates[1].message_id == "message-auto"
    candidate_fields = GmailCandidateRankingItem.model_json_schema()["properties"]
    assert {
        "draft_reply",
        "draft_created",
        "send_enabled",
        "sent",
        "mailbox_action",
    }.isdisjoint(candidate_fields)
    assert ranking.outcome.usage == {"requests": 1}


def test_gmail_candidate_ranking_schema_rejects_draft_content() -> None:
    with pytest.raises(ValueError):
        GmailCandidateRankingResult.model_validate(
            {
                "request_summary": "Choose one message for follow-up.",
                "source_message_count": 1,
                "candidates": [
                    {
                        "message_id": "message-1",
                        "disposition": "candidate",
                        "relevance_score": 0.9,
                        "needs_reply": True,
                        "reasoning": "The sender asked a direct question.",
                        "draft_reply": "This field is not part of selection.",
                    }
                ],
            }
        )


def _assert_no_em_dash(value: object) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert "\u2014" not in encoded


def _contact_lookup_result(*, email: str = "alex@acme.example") -> GmailContactLookupResult:
    return GmailContactLookupResult(
        found=True,
        answer="Alex is the contact supported by the account-onboarding message.",
        contacts=[
            GmailResolvedContact(
                message_id="msg-1",
                thread_id="thread-1",
                contact_name="Alex",
                contact_email=email,
                source_field="from",
                relationship="Account onboarding contact",
                evidence_summary="The subject and snippet describe the account setup.",
            )
        ],
        supporting_message_ids=["msg-1"],
        rationale="The selected message directly describes the requested relationship.",
    )


def _priority_message(
    *,
    message_id: str,
    bucket: str,
    subject: str = "Model supplied subject",
) -> GmailPriorityGroupedMessage:
    return GmailPriorityGroupedMessage.model_validate(
        {
            "message_id": message_id,
            "thread_id": "model-thread",
            "received_at": "2020-01-01T00:00:00Z",
            "subject": subject,
            "sender_name": "Model Sender",
            "sender_email": "model@example.test",
            "bucket": bucket,
            "category": "collaboration_opportunity",
            "confidence": 0.9,
            "priority": "high" if bucket in {"urgent", "important"} else "normal",
            "summary": "Model summary.",
            "reasoning": "The message requires the stated priority.",
            "needs_reply": bucket in {"urgent", "important"},
            "recommended_action": (
                "Review today." if bucket in {"urgent", "important"} else "Review later."
            ),
            "approval_required": False,
        }
    )


def test_provider_first_priority_grouping_binds_complete_read_only_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keystone_agents.gmail_triage import priority_grouping as workflow_module

    request = (
        "CoS, I've been away from email. What arrived today that actually needs me, "
        "and what can wait? Don't draft, label, archive, or send anything."
    )
    summaries = [
        {
            "id": "provider-important",
            "threadId": "thread-important",
            "received_at": "2026-07-24T09:00:00-04:00",
            "from": "Casey <casey@example.test>",
            "sender_name": "Casey",
            "sender_email": "casey@example.test",
            "subject": "Decision needed today",
            "snippet": "Please confirm the final choice today.",
            "labelIds": ["INBOX"],
        },
        {
            "id": "provider-wait",
            "threadId": "thread-wait",
            "received_at": "2026-07-24T10:00:00-04:00",
            "from": "Newsletter <news@example.test>",
            "sender_name": "Newsletter",
            "sender_email": "news@example.test",
            "subject": "Weekly roundup",
            "snippet": "This week's industry roundup.",
            "labelIds": ["INBOX"],
        },
    ]
    calls: dict[str, object] = {}

    class FakeGmail:
        def count_messages(self, *, label: str | None, query: str) -> dict[str, object]:
            calls["count"] = {"label": label, "query": query}
            return {"complete": True, "message_count": len(summaries)}

        def search_message_summaries(
            self,
            *,
            label: str | None,
            query: str,
            max_results: int,
        ) -> list[dict[str, object]]:
            calls["search"] = {
                "label": label,
                "query": query,
                "max_results": max_results,
            }
            return summaries

    def fake_synthesis(**kwargs: object) -> SimpleNamespace:
        retrieved = kwargs["retrieve"]()
        typed_input = kwargs["normalize"](retrieved)
        calls["typed_input"] = typed_input
        model_result = GmailPriorityGroupingResult(
            request_summary="Model rewrite",
            source_label="MODEL",
            lookback_days=7,
            important=[_priority_message(message_id="provider-important", bucket="important")],
            can_wait=[_priority_message(message_id="provider-wait", bucket="can_wait")],
        )
        final = kwargs["finalize_output"](retrieved, model_result)
        return SimpleNamespace(
            final_output=final,
            raw_context=retrieved,
            typed_input=typed_input,
            model_provider="local",
            model_name="fake-model",
            model_run_mode="local_sdk",
            usage={},
            cost={},
            request_cache={},
        )

    monkeypatch.setattr(workflow_module, "run_retrieved_sdk_synthesis", fake_synthesis)
    plan = GmailExecutionPlan(
        operation="priority_grouping",
        read_scope="collection",
        mailbox_direction="inbound",
        date_scope="today",
        provider_query="to:me -in:sent after:1784865599 before:1784952000",
        source_label="INBOX",
        lookback_days=1,
        max_messages=25,
        draft_replies_in_output=False,
        side_effect_policy="read_only",
    )

    execution = run_gmail_priority_grouping_workflow(
        operator_request=request,
        gmail_plan=plan,
        gmail_tool=FakeGmail(),
        run_config=object(),
    )

    typed_input = calls["typed_input"]
    assert typed_input.operator_request == request
    assert typed_input.request == request
    assert "Do not draft replies" in typed_input.draft_policy
    assert calls["count"] == {
        "label": "INBOX",
        "query": "to:me -in:sent after:1784865599 before:1784952000",
    }
    assert calls["search"]["max_results"] == 2
    assert execution.result.request_summary == request
    assert execution.result.source_message_count == 2
    assert execution.result.important[0].subject == "Decision needed today"
    assert execution.result.important[0].sender_name == "Casey"
    assert execution.result.important[0].thread_id == "thread-important"
    assert execution.provider_receipt["selected_message_ids"] == [
        "provider-important",
        "provider-wait",
    ]
    assert execution.provider_receipt["verified"] is True
    assert execution.provider_receipt["provider_write"] is False
    assert "Needs your attention:" in gmail_priority_grouping_human_summary(execution.result)
    assert "Can wait:" in execution.human_summary


def test_provider_first_priority_grouping_surfaces_omitted_message_for_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keystone_agents.gmail_triage import priority_grouping as workflow_module

    summaries = [
        {
            "id": "provider-1",
            "threadId": "thread-1",
            "from": "One <one@example.test>",
            "subject": "One",
        },
        {
            "id": "provider-2",
            "threadId": "thread-2",
            "from": "Two <two@example.test>",
            "subject": "Two",
        },
    ]

    class FakeGmail:
        def count_messages(self, **_: object) -> dict[str, object]:
            return {"complete": True, "message_count": 2}

        def search_message_summaries(self, **_: object) -> list[dict[str, object]]:
            return summaries

    def fake_synthesis(**kwargs: object) -> SimpleNamespace:
        retrieved = kwargs["retrieve"]()
        typed_input = kwargs["normalize"](retrieved)
        result = GmailPriorityGroupingResult(
            important=[_priority_message(message_id="provider-1", bucket="important")]
        )
        final = kwargs["finalize_output"](retrieved, result)
        return SimpleNamespace(
            final_output=final,
            raw_context=retrieved,
            typed_input=typed_input,
            model_provider="local",
            model_name="fake-model",
            model_run_mode="local_sdk",
            usage={},
            cost={},
            request_cache={},
        )

    monkeypatch.setattr(workflow_module, "run_retrieved_sdk_synthesis", fake_synthesis)
    plan = GmailExecutionPlan(
        operation="priority_grouping",
        read_scope="collection",
        provider_query="to:me -in:sent",
        source_label="INBOX",
        max_messages=25,
        side_effect_policy="read_only",
    )

    execution = run_gmail_priority_grouping_workflow(
        operator_request="What needs me and what can wait?",
        gmail_plan=plan,
        gmail_tool=FakeGmail(),
        run_config=object(),
    )

    assert [item.message_id for item in execution.result.important] == [
        "provider-1",
        "provider-2",
    ]
    repaired = execution.result.important[1]
    assert repaired.subject == "Two"
    assert repaired.confidence == 0.0
    assert repaired.recommended_action == "Review manually; no action was inferred."
    assert execution.provider_receipt["classification_repair_count"] == 1
    assert any("Coverage repair surfaced 1" in note for note in execution.result.audit_notes)


def test_provider_first_priority_grouping_rejects_unknown_model_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keystone_agents.gmail_triage import priority_grouping as workflow_module

    summary = {
        "id": "provider-1",
        "threadId": "thread-1",
        "from": "One <one@example.test>",
        "subject": "One",
    }

    class FakeGmail:
        def count_messages(self, **_: object) -> dict[str, object]:
            return {"complete": True, "message_count": 1}

        def search_message_summaries(self, **_: object) -> list[dict[str, object]]:
            return [summary]

    def fake_synthesis(**kwargs: object) -> SimpleNamespace:
        retrieved = kwargs["retrieve"]()
        result = GmailPriorityGroupingResult(
            important=[_priority_message(message_id="invented-id", bucket="important")]
        )
        kwargs["finalize_output"](retrieved, result)
        raise AssertionError("The unknown provider id should have been rejected.")

    monkeypatch.setattr(workflow_module, "run_retrieved_sdk_synthesis", fake_synthesis)
    plan = GmailExecutionPlan(
        operation="priority_grouping",
        read_scope="collection",
        provider_query="to:me -in:sent",
        source_label="INBOX",
        max_messages=25,
        side_effect_policy="read_only",
    )

    with pytest.raises(RuntimeError, match="outside the provider result set"):
        run_gmail_priority_grouping_workflow(
            operator_request="What needs me and what can wait?",
            gmail_plan=plan,
            gmail_tool=FakeGmail(),
            run_config=object(),
        )


def test_provider_first_priority_grouping_quarantines_one_unsafe_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keystone_agents.gmail_triage import priority_grouping as workflow_module
    from keystone_agents.sdk import ToolGuardrailViolation

    safe_message = {
        "id": "safe-message",
        "threadId": "safe-thread",
        "received_at": "2026-07-24T11:00:00-04:00",
        "from": "Casey <casey@example.test>",
        "subject": "Review request",
        "body": "Please review the attached proposal when convenient.",
        "labelIds": ["INBOX"],
    }
    calls: dict[str, object] = {}

    class FakeGmail:
        def count_messages(self, **_: object) -> dict[str, object]:
            return {"complete": True, "message_count": 2}

        def search_message_summaries(self, **_: object) -> list[dict[str, object]]:
            raise ToolGuardrailViolation("one summary contained blocked content")

        def list_recent_messages(self, **_: object) -> list[dict[str, str]]:
            return [
                {"id": "blocked-message", "threadId": "blocked-thread"},
                {"id": "safe-message", "threadId": "safe-thread"},
            ]

        def batch_get_messages(
            self,
            message_ids: list[str],
            *,
            skip_blocked: bool = False,
        ) -> list[dict[str, object]]:
            calls["message_ids"] = message_ids
            calls["skip_blocked"] = skip_blocked
            return [safe_message]

    def fake_synthesis(**kwargs: object) -> SimpleNamespace:
        retrieved = kwargs["retrieve"]()
        typed_input = kwargs["normalize"](retrieved)
        result = GmailPriorityGroupingResult(
            important=[_priority_message(message_id="safe-message", bucket="important")]
        )
        final = kwargs["finalize_output"](retrieved, result)
        return SimpleNamespace(
            final_output=final,
            raw_context=retrieved,
            typed_input=typed_input,
            model_provider="local",
            model_name="fake-model",
            model_run_mode="local_sdk",
            usage={},
            cost={},
            request_cache={},
        )

    monkeypatch.setattr(workflow_module, "run_retrieved_sdk_synthesis", fake_synthesis)
    plan = GmailExecutionPlan(
        operation="priority_grouping",
        read_scope="collection",
        provider_query="to:me -in:sent",
        source_label="INBOX",
        max_messages=25,
        side_effect_policy="read_only",
    )

    execution = run_gmail_priority_grouping_workflow(
        operator_request="What needs me and what can wait?",
        gmail_plan=plan,
        gmail_tool=FakeGmail(),
        run_config=object(),
    )

    assert calls["message_ids"] == ["blocked-message", "safe-message"]
    assert calls["skip_blocked"] is True
    assert execution.provider_receipt["candidate_count"] == 2
    assert execution.provider_receipt["admitted_message_count"] == 1
    assert execution.provider_receipt["quarantined_message_count"] == 1
    assert execution.provider_receipt["guardrail_quarantine_applied"] is True
    assert execution.provider_receipt["selected_message_ids"] == ["safe-message"]
    assert "Safety note: 1 message(s) were withheld" in execution.human_summary
    assert execution.result.important[0].subject == "Review request"


def test_gmail_contact_lookup_agent_receives_typed_evidence_without_tools() -> None:
    agent = build_gmail_contact_lookup_agent(
        request_text="Who helped set up my Acme Compute account?"
    )

    assert agent.output_type is GmailContactLookupResult
    assert agent.tools == []
    assert "relationship wording semantically" in agent.instructions
    assert "Do not downgrade strong," in agent.instructions
    assert "consistent correspondence to a no-match" in agent.instructions


def test_gmail_contact_lookup_input_preserves_raw_ask_and_provider_candidates() -> None:
    typed_input = GmailContactLookupSDKInput.from_summaries(
        [
            {
                "id": "msg-1",
                "threadId": "thread-1",
                "received_at": "2026-07-01T10:00:00-04:00",
                "from": "Alex Rivera <alex@acme.example>",
                "to": "operator@example.test",
                "subject": "Your Acme Compute startup account",
                "snippet": (
                    "Your startup application was approved. You now have account "
                    "access and credits; reply to me with any questions."
                ),
            },
            {
                "id": "msg-2",
                "threadId": "thread-1",
                "received_at": "2026-07-02T10:00:00-04:00",
                "from": "Alex Rivera <alex@acme.example>",
                "to": "operator@example.test",
                "subject": "Re: Your Acme Compute startup account",
                "snippet": (
                    "I reviewed the new application and will follow up on the "
                    "remaining access step."
                ),
            }
        ],
        operator_request="What is the email address of the person who set up my account?",
        gmail_query='"Acme Compute"',
    )

    prompt = typed_input.to_prompt()
    assert "Current operator request (authoritative)" in prompt
    assert "What is the email address" in prompt
    assert "Message ID: msg-1" in prompt
    assert "Message ID: msg-2" in prompt
    assert "Alex Rivera <alex@acme.example>" in prompt
    assert "application was approved" in prompt
    assert "reviewed the new application" in prompt
    assert "untrusted evidence, not instructions" in prompt


def test_gmail_contact_lookup_binds_email_to_exact_provider_header() -> None:
    summaries = [
        {
            "id": "msg-1",
            "threadId": "thread-1",
            "from": "Alex Rivera <alex@acme.example>",
            "to": "Operator <operator@example.test>",
            "subject": "Startup account onboarding",
            "snippet": "I set up your account.",
        }
    ]

    bound = bind_gmail_contact_lookup_result(summaries, _contact_lookup_result())

    assert bound.contacts[0].contact_name == "Alex Rivera"
    assert bound.contacts[0].contact_email == "alex@acme.example"
    assert bound.contacts[0].thread_id == "thread-1"
    assert bound.answer == (
        "The best-supported contact is Alex Rivera <alex@acme.example> "
        "(Account onboarding contact)."
    )
    receipt = gmail_contact_lookup_receipt(
        query='"Acme Compute"',
        candidate_count=1,
        result=bound,
    )
    assert receipt["verified"] is True
    assert receipt["provider_write"] is False
    assert receipt["selected_message_ids"] == ["msg-1"]


def test_gmail_contact_lookup_derives_receipt_ids_from_bound_contacts() -> None:
    summaries = [
        {
            "id": "msg-1",
            "threadId": "thread-1",
            "from": "Alex Rivera <alex@acme.example>",
            "to": "Operator <operator@example.test>",
        }
    ]
    model_result = _contact_lookup_result().model_copy(
        update={"supporting_message_ids": ["model-invented-support-id"]}
    )

    bound = bind_gmail_contact_lookup_result(summaries, model_result)

    assert bound.supporting_message_ids == ["msg-1"]
    assert bound.contacts[0].message_id == "msg-1"


def test_gmail_contact_lookup_resolves_unique_thread_id_to_exact_message() -> None:
    summaries = [
        {
            "id": "msg-1",
            "threadId": "thread-1",
            "from": "Alex Rivera <alex@acme.example>",
            "to": "Operator <operator@example.test>",
        }
    ]
    model_result = _contact_lookup_result().model_copy(
        update={
            "contacts": [
                _contact_lookup_result().contacts[0].model_copy(
                    update={"message_id": "thread-1"}
                )
            ],
            "supporting_message_ids": ["thread-1"],
        }
    )

    bound = bind_gmail_contact_lookup_result(summaries, model_result)

    assert bound.supporting_message_ids == ["msg-1"]
    assert bound.contacts[0].message_id == "msg-1"
    assert bound.contacts[0].thread_id == "thread-1"


def test_gmail_contact_lookup_rejects_ambiguous_thread_message_binding() -> None:
    summaries = [
        {
            "id": message_id,
            "threadId": "thread-1",
            "from": "Alex Rivera <alex@acme.example>",
            "to": "Operator <operator@example.test>",
        }
        for message_id in ("msg-1", "msg-2")
    ]
    model_result = _contact_lookup_result().model_copy(
        update={
            "contacts": [
                _contact_lookup_result().contacts[0].model_copy(
                    update={"message_id": "thread-1"}
                )
            ],
            "supporting_message_ids": ["thread-1"],
        }
    )

    with pytest.raises(GmailContactBindingError, match="multiple matching"):
        bind_gmail_contact_lookup_result(summaries, model_result)


def test_gmail_contact_lookup_summary_does_not_repeat_verified_single_contact() -> None:
    result = _contact_lookup_result().model_copy(
        update={
            "answer": (
                "The account contact appears to be Alex Rivera at a different "
                "transactional alias."
            )
        }
    )

    assert gmail_contact_lookup_human_summary(result) == (
        "The best-supported contact is Alex <alex@acme.example> "
        "(Account onboarding contact)."
    )


def test_gmail_contact_lookup_rejects_model_invented_address() -> None:
    summaries = [
        {
            "id": "msg-1",
            "threadId": "thread-1",
            "from": "Alex Rivera <alex@acme.example>",
            "to": "operator@example.test",
        }
    ]

    with pytest.raises(GmailContactBindingError, match="not present"):
        bind_gmail_contact_lookup_result(
            summaries,
            _contact_lookup_result(email="invented@acme.example"),
        )


def test_gmail_contact_lookup_execution_plan_is_bounded_and_read_only() -> None:
    request = (
        "CoS, what is the email address of the person from Acme Compute who set "
        "up my startup account?"
    )
    manual_plan = infer_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
    ).model_copy(update={"source": "llm"})

    execution = resolve_gmail_execution_plan(request, manual_plan=manual_plan)

    assert execution.operation == "contact_lookup"
    assert execution.read_scope == "collection"
    assert execution.max_messages == 10
    assert execution.gmail_query == '"Acme Compute"'
    assert execution.live_read_required is True
    assert execution.side_effect_policy == "read_only"
    assert execution.create_gmail_drafts is False
    assert "gmail_contact_source_binding" in execution.candidate_helpers


def test_stored_read_only_gmail_plan_cannot_replay_draft_write_operations() -> None:
    execution = resolve_gmail_execution_plan(
        "Review the selected thread and put any useful reply here only.",
        manual_plan=ManualRequestPlan(
            source="canonical:stored_work_item",
            target_agent="gmail_triage",
            intent="business_system_write",
            task_objective="gmail_triage",
            target_type="gmail_thread",
            provider_system="gmail",
            provider_operations=["read", "create", "update"],
            draft_policy="draft_only_when_reply_needed",
            expected_artifact_type="outreach_draft",
            ask_shape={
                "permission_state": "read_only",
                "output_form": "draft",
            },
        ),
    )

    assert execution.operation == "draft_reply"
    assert execution.create_gmail_drafts is False
    assert execution.draft_replies_in_output is True
    assert execution.side_effect_policy == "read_only_or_draft_only"


def test_contact_lookup_objective_survives_inconsistent_lower_level_target_shape() -> None:
    request = "What is the email address of the person who set up my account?"
    manual_plan = infer_manual_request_plan(
        request,
        requested_agent="chief_of_staff",
    ).model_copy(
        update={
            "source": "llm",
            "target_agent": "gmail_triage",
            "intent": "gmail_triage",
            "provider_system": "gmail",
            "provider_operations": ["search", "read"],
            "task_objective": "contact_discovery",
            "target_type": "unknown",
            "gmail_query": '"Acme Compute"',
        }
    )

    execution = resolve_gmail_execution_plan(request, manual_plan=manual_plan)

    assert execution.operation == "contact_lookup"
    assert execution.read_scope == "collection"
    assert execution.gmail_query == '"Acme Compute"'
    assert execution.side_effect_policy == "read_only"


def test_gmail_collection_default_count_does_not_collapse_read_to_one_message() -> None:
    request = (
        "I've been away from email. What arrived today that actually needs me, "
        "and what can wait? Don't draft, label, archive, or send anything."
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")

    execution = resolve_gmail_execution_plan(request, manual_plan=plan)

    assert plan.target_agent == "gmail_triage"
    assert plan.intent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert plan.provider_operations == ["read"]
    assert plan.provider_read_scope == "bounded_collection"
    assert plan.gmail_mailbox_direction == "inbound"
    assert plan.gmail_date_scope == "today"
    assert plan.gmail_requested_fields == []
    assert plan.ask_shape.output_form != "draft"
    assert plan.ask_shape.permission_state == "read_only"
    assert execution.operation == "priority_grouping"
    assert execution.max_messages == 25
    assert execution.read_scope == "collection"
    assert execution.side_effect_policy == "read_only"


@pytest.mark.parametrize(
    "operator_text",
    [
        "Sort today's inbox into what needs my attention and what can wait. "
        "Don't change anything.",
        "What emails came in this morning that are important? Read only.",
        "Review my recent emails and summarize the priorities. "
        "Don't label, archive, draft, or send.",
    ],
)
def test_gmail_collection_triage_variations_use_one_read_only_contract(
    operator_text: str,
) -> None:
    plan = infer_manual_request_plan(operator_text, requested_agent="chief_of_staff")
    execution = resolve_gmail_execution_plan(operator_text, manual_plan=plan)

    assert plan.target_agent == "gmail_triage"
    assert plan.intent == "gmail_triage"
    assert plan.provider_system == "gmail"
    assert plan.provider_operations == ["read"]
    assert plan.target_type == "gmail_message_collection"
    assert plan.provider_read_scope == "bounded_collection"
    assert plan.ask_shape.output_form != "draft"
    assert execution.operation == "priority_grouping"
    assert execution.read_scope == "collection"
    assert execution.side_effect_policy == "read_only"
    assert execution.create_gmail_drafts is False


def test_gmail_collection_honors_an_explicit_one_message_limit() -> None:
    plan = ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        intent="gmail_triage",
        target_type="gmail_message_collection",
        provider_system="gmail",
        provider_operations=["read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        gmail_requested_fields=["subject", "sender"],
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        desired_count=1,
        desired_count_explicit=True,
    )

    execution = resolve_gmail_execution_plan(
        "Show one email that needs me.",
        manual_plan=plan,
    )

    assert execution.operation == "message_projection"
    assert execution.max_messages == 1


def test_gmail_triage_objective_uses_requested_fields_as_evidence_not_operation() -> None:
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="gmail_triage",
        intent="gmail_triage",
        target_type="gmail_message_collection",
        provider_system="gmail",
        provider_operations=["search", "read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        gmail_mailbox_direction="inbound",
        gmail_date_scope="today",
        gmail_requested_fields=["subject", "sender", "date", "snippet"],
        task_objective="gmail_triage",
        expected_artifact_type="gmail_triage_report",
        desired_count=1,
        desired_count_explicit=False,
        draft_policy="no_drafts_requested",
    )

    execution = resolve_gmail_execution_plan(
        "What arrived today that needs me, and what can wait?",
        manual_plan=plan,
    )

    assert execution.operation == "priority_grouping"
    assert execution.read_scope == "collection"
    assert execution.max_messages == 25
    assert execution.requested_fields == ["subject", "sender", "date", "snippet"]
    assert execution.side_effect_policy == "read_only"
    assert execution.draft_replies_in_output is False


def test_contact_lookup_cli_binds_sdk_answer_to_live_gmail_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_gmail_triage as cli
    from keystone_agents.gmail_triage import contact_lookup as contact_lookup_module

    operator_request = (
        "Who at Acme Compute helped set up my startup account, and what is their email?"
    )

    class FakeLiveGmail:
        def __init__(self, *, live: bool) -> None:
            assert live is True

        def search_message_summaries(
            self,
            *,
            label: str | None,
            max_results: int,
            query: str,
        ) -> list[dict[str, str]]:
            assert label is None
            assert max_results == 10
            assert query == '"Acme Compute"'
            return [
                {
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "received_at": "2026-07-01T10:00:00-04:00",
                    "from": "Alex Rivera <alex@acme.example>",
                    "to": "operator@example.test",
                    "subject": "Your Acme Compute startup account",
                    "snippet": "I set up the startup account and can help with onboarding.",
                }
            ]

    def fake_run_retrieved_sdk_synthesis(**kwargs: object) -> SimpleNamespace:
        raw_context = kwargs["retrieve"]()
        typed_input = kwargs["normalize"](raw_context)
        assert typed_input.operator_request == operator_request
        assert "Current operator request (authoritative)" in typed_input.to_prompt()
        final_output = kwargs["finalize_output"](
            raw_context,
            _contact_lookup_result(),
        )
        return SimpleNamespace(
            agent_name="gmail_triage",
            typed_input=typed_input,
            final_output=final_output,
            live=True,
            model_provider="openai",
            model_name="test-model",
            model_run_mode="live_sdk",
            usage={"requests": 1},
            cost={},
            budget_guard={},
            request_cache={},
            provider_usage_context={},
            started_at_unix=1.0,
            ended_at_unix=2.0,
            storage={},
            audit_notes=(),
        )

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        cli,
        "resolve_sdk_execution",
        lambda *_args, **_kwargs: (object(), True),
    )
    monkeypatch.setattr(
        contact_lookup_module,
        "run_retrieved_sdk_synthesis",
        fake_run_retrieved_sdk_synthesis,
    )
    args = cli.build_parser().parse_args(
        [
            "--contact-lookup",
            "--live-gmail",
            "--allow-inbox",
            "--no-dry-run",
            "--live-sdk",
            "--request",
            operator_request,
            "--gmail-query",
            '"Acme Compute"',
            "--max-messages",
            "10",
        ]
    )

    payload = cli._run_contact_lookup_sdk_synthesis(args)

    assert payload["status"] == "completed"
    assert payload["output"]["contacts"][0]["contact_email"] == "alex@acme.example"
    assert payload["human_summary"] == (
        "The best-supported contact is Alex Rivera <alex@acme.example> "
        "(Account onboarding contact)."
    )
    assert payload["tool_receipts"] == [
        {
            "provider": "gmail",
            "operation": "search_and_read_contact_evidence",
            "query": '"Acme Compute"',
            "candidate_count": 1,
            "selected_message_ids": ["msg-1"],
            "provider_read": True,
            "provider_write": False,
            "verified": True,
        }
    ]
    assert payload["public_result"]["provider_receipt_verified"] is True
    assert payload["side_effects"] == {
        "gmail_read": True,
        "gmail_write": False,
        "email_sent": False,
    }


def test_consulting_inquiry_creates_draft_and_requires_approval() -> None:
    result = run_gmail_triage_fixture(
        _fixture("sample_email_consulting.txt"),
        sender_name="Alex",
        sender_email="alex@example.com",
    )

    assert result.category == "consulting_opportunity"
    assert result.needs_reply is True
    assert result.draft_reply
    assert result.draft_created is True
    assert result.approval_required is True
    assert "Keystone/Draft Pending Approval" in result.recommended_labels
    assert "Keystone/Action Required" in result.recommended_labels
    assert result.style_profile_used is False


def test_optional_reply_draft_does_not_claim_the_email_requires_a_response() -> None:
    result = EmailTriageResult(
        category="vendor",
        confidence=0.9,
        reasoning="The message is informational and a response is optional.",
        needs_reply=False,
        recommended_action="Offer one optional reply for human review.",
        draft_reply="Thanks for sharing this. What is the best eligibility starting point?",
        draft_created=False,
        approval_required=True,
    )

    assert result.needs_reply is False
    assert result.draft_reply
    assert result.draft_created is False
    assert result.approval_required is True


def test_gmail_draft_can_use_approved_email_style_profile() -> None:
    style = load_email_style_profile_fixture("sample_email_style_profile_approved")

    result = run_gmail_triage_fixture(
        _fixture("sample_email_consulting.txt"),
        sender_name="Alex",
        sender_email="alex@example.com",
        email_style_profile=style,
    )

    assert result.draft_reply is not None
    assert result.style_profile_used is True
    assert result.style_profile_id == "default"
    assert result.draft_reply.startswith("Hi Alex,")
    assert "Happy to compare notes if useful" in result.draft_reply
    assert result.draft_reply.endswith("Sincerely,\nAnup")
    assert result.approval_required is True


def test_gmail_draft_can_use_approved_personal_signoff_style_profile() -> None:
    style = load_email_style_profile_fixture("sample_email_style_profile_anup_approved")

    result = run_gmail_triage_fixture(
        _fixture("sample_email_consulting.txt"),
        sender_name="Alex",
        sender_email="alex@example.com",
        email_style_profile=style,
    )

    assert result.draft_reply is not None
    assert result.style_profile_used is True
    assert result.style_profile_id == "anup-default"
    assert result.draft_reply.endswith("Sincerely,\nAnup")
    assert result.approval_required is True
    assert result.draft_created is True
    assert "\u2014" not in result.draft_reply


def test_gmail_draft_ignores_unapproved_email_style_profile() -> None:
    style = EmailStyleProfile(
        profile_id="pending-style",
        approval_state="pending",
        greeting_patterns=["Yo {name},"],
        signoffs=["Later,"],
        preferred_phrases=["unapproved phrase"],
    )

    result = run_gmail_triage_fixture(
        _fixture("sample_email_consulting.txt"),
        sender_name="Alex",
        email_style_profile=style,
    )

    assert result.draft_reply is not None
    assert result.style_profile_used is False
    assert "Yo Alex" not in result.draft_reply
    assert "unapproved phrase" not in result.draft_reply


def test_collaboration_inquiry_classified_correctly() -> None:
    result = run_gmail_triage_fixture(_fixture("sample_email_collaboration.txt"))

    assert result.category == "collaboration_opportunity"
    assert result.needs_reply is True
    assert result.priority == "high"
    assert "Keystone/Collaboration" in result.recommended_labels


def test_managed_labels_have_one_primary_label_and_overlays() -> None:
    for fixture in FIXTURES.glob("sample_email_*.txt"):
        result = run_gmail_triage_fixture(fixture)
        primary_labels = [
            label for label in result.recommended_labels if label in GMAIL_PRIMARY_LABEL_SET
        ]
        assert len(primary_labels) == 1
        assert "Keystone/Triage" in result.recommended_labels
        if result.needs_reply:
            assert "Keystone/Action Required" in result.recommended_labels
        if result.approval_required:
            assert "Keystone/Draft Pending Approval" in result.recommended_labels


def test_label_normalization_drops_extra_primary_labels() -> None:
    labels = normalize_managed_gmail_labels(
        [
            "Keystone/Triage",
            "Keystone/Consulting Opportunity",
            "Keystone/Vendor",
            "Keystone/Action Required",
        ]
    )

    primary_labels = [label for label in labels if label in GMAIL_PRIMARY_LABEL_SET]
    assert primary_labels == ["Keystone/Consulting Opportunity"]
    assert "Keystone/Action Required" in labels


def test_vendor_pitch_is_not_high_value_by_default() -> None:
    result = run_gmail_triage_fixture(_fixture("sample_email_vendor.txt"))

    assert result.category == "vendor"
    assert result.needs_reply is False
    assert result.priority == "low"
    assert "high-value" in result.summary


def test_suspicious_email_has_no_reply_and_security_risk_flag() -> None:
    result = run_gmail_triage_fixture(_fixture("sample_email_suspicious.txt"))

    assert result.category == "suspicious"
    assert result.needs_reply is False
    assert "security" in result.risk_flags
    assert result.draft_reply is None
    assert "Keystone/Suspicious" in result.recommended_labels
    assert "Keystone/Security Review" in result.recommended_labels
    assert "Keystone/Manual Review" in result.recommended_labels


def test_newsletter_has_no_reply() -> None:
    result = run_gmail_triage_fixture(_fixture("sample_email_newsletter.txt"))

    assert result.category == "newsletter"
    assert result.needs_reply is False
    assert result.draft_reply is None


def test_no_fixture_output_contains_em_dash() -> None:
    for fixture in FIXTURES.glob("sample_email_*.txt"):
        result = run_gmail_triage_fixture(fixture)
        _assert_no_em_dash(result.model_dump())


def test_fixture_triage_normalizes_body_and_extracts_links() -> None:
    result = triage_email_fixture(
        EmailFixture(
            subject="Consulting support",
            body=(
                "Could we discuss clinical operations? See https://example.com/context.\n"
                "On Tue, Alex wrote:\n"
                "> Prior quoted thread should not be used."
            ),
            sender_name="Alex",
            sender_email="alex@example.com",
            prior_labels=("INBOX", "UNREAD"),
        )
    )

    assert result.thread_id == "fixture-thread"
    assert result.prior_labels == ["INBOX", "UNREAD"]
    assert "Prior quoted thread" not in result.normalized_body
    assert "<a" not in result.thread_summary
    assert result.extracted_links[0].url == "https://example.com/context"
    assert result.recommended_next_agent == "business_research_analyst"
    assert any("deterministic local" in item for item in result.triage_limitations)


def test_gmail_api_envelope_uses_html_fallback_and_attachment_metadata() -> None:
    html_body = base64.urlsafe_b64encode(
        b"<html><body><p>Could we discuss a clinical operations workflow?</p>"
        b"<a href='http://bit.ly/login-reset'>review details</a>"
        b"<blockquote>Older quoted reply should be ignored.</blockquote></body></html>"
    ).decode()
    envelope = gmail_message_envelope_from_api(
        {
            "id": "msg-html",
            "threadId": "thread-html",
            "internalDate": "1713744000000",
            "snippet": "clinical operations workflow",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Alex <alex@example.com>"},
                    {"name": "To", "value": "Keystone <hello@example.com>"},
                    {"name": "Subject", "value": "HTML consulting inquiry"},
                ],
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "text/html",
                        "body": {"data": html_body},
                    },
                    {
                        "filename": "invoice.xlsm",
                        "mimeType": "application/vnd.ms-excel.sheet.macroEnabled.12",
                        "body": {"attachmentId": "att-1", "size": 2048},
                    },
                ],
            },
        }
    )
    triage = triage_gmail_message_envelope(envelope)

    assert envelope.thread_id == "thread-html"
    assert envelope.received_at == "2024-04-22T00:00:00Z"
    assert "clinical operations workflow" in envelope.normalized_body
    assert "Older quoted reply" not in envelope.normalized_body
    assert "Older quoted reply" not in envelope.thread_summary
    assert envelope.extracted_links[0].suspicious is True
    assert "shortened URL" in envelope.extracted_links[0].reasons
    assert envelope.attachment_metadata[0].filename == "invoice.xlsm"
    assert "macro_enabled_attachment" in envelope.attachment_metadata[0].risk_flags
    assert "security" in triage.risk_flags
    assert triage.category == "suspicious"
    assert triage.draft_reply is None
    assert any("Attachments were screened" in item for item in triage.triage_limitations)


def test_gmail_typed_input_prompt_uses_sanitized_envelope_context() -> None:
    envelope = GmailMessageEnvelope(
        message_id="msg-typed",
        thread_id="thread-typed",
        received_at="2026-04-22T12:00:00Z",
        sender_name="Alex",
        sender_email="alex@example.com",
        subject="Consulting support",
        snippet="clinical operations workflow",
        prior_labels=["INBOX"],
        normalized_body="Could we discuss clinical operations?",
        attachment_metadata=[
            GmailAttachmentMetadata(
                filename="brief.pdf",
                mime_type="application/pdf",
                size_bytes=128,
                attachment_id_present=True,
            )
        ],
        thread_context="Only the selected message was available.",
        triage_limitations=["Attachments were not ingested; metadata only was screened."],
    )

    prompt = GmailTriageSDKInput.from_envelope(envelope).to_prompt()
    grouping_prompt = GmailPriorityGroupingSDKInput.from_envelopes([envelope]).to_prompt()

    assert "Thread ID: thread-typed" in prompt
    assert "Thread context:" in prompt
    assert "Only the selected message was available." in prompt
    assert "Attachment metadata only, no attachment bodies were ingested" in prompt
    assert "brief.pdf" in prompt
    assert "Normalized body:" in prompt
    assert "Could we discuss clinical operations?" in prompt
    assert "Message 1:" in grouping_prompt
    assert "Thread context:" in grouping_prompt
    assert "Only the selected message was available." in grouping_prompt


def test_priority_grouping_prompt_compacts_large_message_fields() -> None:
    envelope = GmailMessageEnvelope(
        message_id="msg-large",
        thread_id="thread-large",
        received_at="2026-04-22T12:00:00Z",
        sender_name="Alex",
        sender_email="alex@example.com",
        subject="Long thread",
        snippet="Please review",
        normalized_body="body " * 800,
        thread_context="thread " * 800,
    )

    prompt = GmailPriorityGroupingSDKInput.from_envelopes([envelope]).to_prompt()

    assert "Message ID: msg-large" in prompt
    assert "Subject: Long thread" in prompt
    assert "[truncated for batch triage; use the message/thread id for full review]" in prompt
    assert len(prompt) < 3500


def test_priority_grouping_output_normalizes_em_dash_text() -> None:
    result = GmailPriorityGroupingResult.model_validate(
        {
            "request_summary": "Review inbox — staged retrieval",
            "source_label": "INBOX",
            "lookback_days": 3,
            "source_message_count": 1,
            "urgent": [
                {
                    "message_id": "msg-1",
                    "thread_id": "thread-1",
                    "subject": "COI",
                    "bucket": "urgent",
                    "category": "vendor",
                    "confidence": 0.9,
                    "priority": "urgent",
                    "summary": "Needs reply — certificate received",
                    "reasoning": "Operational request.",
                    "needs_reply": True,
                    "recommended_action": "Acknowledge receipt.",
                    "draft_reply": "Hi Andy,\n\nReceived — thanks.\n\nBest,\nAnup",
                    "draft_created": False,
                    "approval_required": True,
                    "send_enabled": False,
                    "sent": False,
                }
            ],
            "draft_count": 1,
            "send_enabled": False,
            "sent": False,
            "live_side_effects_enabled": False,
            "audit_notes": ["No send — approval required."],
        }
    )

    _assert_no_em_dash(result.model_dump(mode="json"))
    assert "Received - thanks." in str(result.urgent[0].draft_reply)


def test_bounded_weekly_gmail_fixture_grouping_prioritizes_every_message_without_mutation() -> None:
    envelopes = [
        GmailMessageEnvelope(
            message_id="msg-security",
            thread_id="thread-security",
            received_at="2026-07-12T12:00:00Z",
            sender_name="Security Team",
            sender_email="security@example.com",
            subject="Unexpected credential reset request",
            normalized_body="Reset your password at http://bit.ly/login-reset immediately.",
            suspicious_signals=["shortened URL requires security review"],
        ),
        GmailMessageEnvelope(
            message_id="msg-consulting",
            thread_id="thread-consulting",
            received_at="2026-07-11T15:00:00Z",
            sender_name="Clinical Operations Lead",
            sender_email="lead@example.com",
            subject="Consulting support",
            normalized_body=(
                "Could Keystone advise on our clinical operations workflow this month?"
            ),
        ),
        GmailMessageEnvelope(
            message_id="msg-newsletter",
            thread_id="thread-newsletter",
            received_at="2026-07-10T09:00:00Z",
            sender_name="Industry Digest",
            sender_email="digest@example.com",
            subject="Weekly behavioral health newsletter",
            normalized_body="This week's industry roundup and webinar links.",
        ),
    ]

    result = group_gmail_envelopes_fixture(
        envelopes,
        operator_request="Review recent email and tell me what needs follow-up this week.",
        lookback_days=7,
        source_label="INBOX",
    )

    grouped = [*result.urgent, *result.important, *result.can_wait, *result.ignore]
    assert result.request_summary.endswith("follow-up this week.")
    assert result.lookback_days == 7
    assert result.source_message_count == 3
    assert [message.message_id for message in grouped] == [
        "msg-security",
        "msg-consulting",
        "msg-newsletter",
    ]
    assert "security" in result.urgent[0].risk_flags
    assert result.important[0].needs_reply is True
    assert result.ignore[0].category == "newsletter"
    assert all(message.draft_reply is None for message in grouped)
    assert all(message.draft_created is False for message in grouped)
    assert all(message.send_enabled is False for message in grouped)
    assert result.send_enabled is False
    assert result.live_side_effects_enabled is False


def test_build_gmail_triage_agent_returns_sdk_agent_like_object() -> None:
    agent = build_gmail_triage_agent()
    tool_names = {getattr(tool, "name", "") for tool in agent.tools}

    assert agent.name == "gmail_triage"
    assert agent.output_type is EmailTriageResult
    assert "Gmail Triage Agent" in str(agent.instructions)
    assert "Keystone Profile" in str(agent.instructions)
    assert {
        "get_gmail_message",
        "apply_gmail_labels",
        "create_gmail_draft_reply",
        "create_gmail_draft_with_attachment",
    } <= tool_names


def test_build_gmail_triage_agent_can_disable_tools_for_llm_only_synthesis() -> None:
    agent = build_gmail_triage_agent(include_tools=False)

    assert agent.name == "gmail_triage"
    assert agent.output_type is EmailTriageResult
    assert agent.tools == []
    assert agent.input_guardrails
    assert agent.output_guardrails


def test_prompt_file_is_loaded_not_hard_coded_inline() -> None:
    agent = build_gmail_triage_agent()
    module_path = PROJECT_ROOT / "src" / "keystone_agents" / "agents" / "gmail_triage.py"
    source = module_path.read_text(encoding="utf-8")

    assert "<!-- gmail_triage.md -->" in str(agent.instructions)
    assert "<!-- keystone_profile.md -->" in str(agent.instructions)
    assert "compose_instructions" in source
    assert "Classify each inbound email for business triage" not in source


def test_no_send_tool_is_exposed() -> None:
    module_path = PROJECT_ROOT / "src" / "keystone_agents" / "tools" / "gmail_tool.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    agent = build_gmail_triage_agent()
    tool_names = {getattr(tool, "name", "") for tool in agent.tools}

    assert {name for name in tool_names if name.startswith("send") or "send_email" in name} == {
        "send_gmail_test_draft"
    }
    assert "send_email" in function_names
    assert "create_gmail_draft_reply" in function_names


class FakeGmailResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self._payload


class FakeGmailSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeGmailResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        assert "/send" not in url

        if method == "GET" and url.endswith("/messages"):
            return FakeGmailResponse({"messages": [{"id": "msg-1", "threadId": "thread-1"}]})
        if method == "GET" and url.endswith("/messages/msg-1"):
            body = base64.urlsafe_b64encode(
                b"Could we discuss a clinical operations workflow?"
            ).decode()
            return FakeGmailResponse(
                {
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "snippet": "clinical operations workflow",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Alex <alex@example.com>"},
                            {"name": "To", "value": "Keystone <hello@example.com>"},
                            {"name": "Subject", "value": "Consulting support"},
                            {"name": "Message-ID", "value": "<original@example.com>"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": body},
                    },
                }
            )
        if method == "POST" and url.endswith("/drafts"):
            message = kwargs["json"]["message"]  # type: ignore[index]
            assert message["threadId"] == "thread-1"
            assert message["raw"]
            return FakeGmailResponse({"id": "draft-1", "message": {"id": "draft-message"}})
        if method == "GET" and url.endswith("/labels"):
            return FakeGmailResponse(
                {"labels": [{"name": "Keystone/Triage", "id": "Label_triage"}]}
            )
        if method == "POST" and url.endswith("/labels"):
            name = kwargs["json"]["name"]  # type: ignore[index]
            return FakeGmailResponse({"id": f"Label_{str(name).replace('/', '_')}", "name": name})
        if method == "POST" and url.endswith("/messages/msg-1/modify"):
            return FakeGmailResponse({"id": "msg-1", "labelIds": kwargs["json"]["addLabelIds"]})  # type: ignore[index]
        raise AssertionError(f"Unexpected Gmail API call: {method} {url}")


def test_gmail_api_mocked_list_get_label_and_create_draft_reply() -> None:
    session = FakeGmailSession()
    gmail = GmailTool(live=True, access_token="test-token", session=session)

    messages = gmail.list_recent_messages(label="UNREAD", max_results=1)
    summaries = gmail.search_message_summaries(label="UNREAD", max_results=1)
    batch = gmail.batch_get_messages(["msg-1"])
    message = gmail.get_message("msg-1")
    label_result = gmail.apply_labels("msg-1", ["Keystone/Triage", "Keystone/Action Required"])
    draft_result = gmail.create_draft_reply("msg-1", "Thanks for reaching out. I can review this.")

    assert messages == [{"id": "msg-1", "threadId": "thread-1"}]
    assert summaries[0]["subject"] == "Consulting support"
    assert "body" not in summaries[0]
    assert batch[0]["id"] == "msg-1"
    assert message["threadId"] == "thread-1"
    assert "clinical operations workflow" in message["body"]
    assert label_result["status"] == "labels_applied"
    assert draft_result["status"] == "draft_created"
    assert draft_result["sent"] is False
    assert any(str(call["url"]).endswith("/drafts") for call in session.calls)
    assert not any("/send" in str(call["url"]) for call in session.calls)


def test_gmail_message_count_paginates_to_an_exact_read_only_total() -> None:
    class PaginatedSession:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def request(self, method: str, url: str, **kwargs: object) -> FakeGmailResponse:
            self.calls.append({"method": method, "url": url, **kwargs})
            assert method == "GET"
            assert url.endswith("/messages")
            params = kwargs.get("params")
            assert isinstance(params, dict)
            assert params["q"] == "to:me -in:sent after:1 before:2"
            assert "labelIds" not in params
            if not params.get("pageToken"):
                return FakeGmailResponse(
                    {
                        "messages": [{"id": "m1"}, {"id": "m2"}],
                        "nextPageToken": "page-2",
                        "resultSizeEstimate": 99,
                    }
                )
            assert params["pageToken"] == "page-2"
            return FakeGmailResponse(
                {
                    "messages": [{"id": "m2"}, {"id": "m3"}],
                    "resultSizeEstimate": 99,
                }
            )

    session = PaginatedSession()
    gmail = GmailTool(live=True, access_token="test-token", session=session)

    receipt = gmail.count_messages(
        query="to:me -in:sent after:1 before:2",
        page_size=2,
    )

    assert receipt["message_count"] == 3
    assert receipt["page_count"] == 2
    assert receipt["complete"] is True
    assert receipt["provider_read"] is True
    assert receipt["provider_write"] is False
    assert len(session.calls) == 2


def test_gmail_message_count_marks_a_page_capped_total_incomplete() -> None:
    class MorePagesSession:
        def request(self, method: str, url: str, **_kwargs: object) -> FakeGmailResponse:
            assert method == "GET"
            assert url.endswith("/messages")
            return FakeGmailResponse(
                {
                    "messages": [{"id": "m1"}, {"id": "m2"}],
                    "nextPageToken": "more-results",
                }
            )

    gmail = GmailTool(
        live=True,
        access_token="test-token",
        session=MorePagesSession(),
    )

    receipt = gmail.count_messages(query="to:me", page_size=2, max_pages=1)

    assert receipt["message_count"] == 2
    assert receipt["page_count"] == 1
    assert receipt["complete"] is False
    assert receipt["provider_write"] is False


def test_gmail_message_projection_uses_complete_same_query_and_requested_fields() -> None:
    class ProjectionSession:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def request(self, method: str, url: str, **kwargs: object) -> FakeGmailResponse:
            self.calls.append({"method": method, "url": url, **kwargs})
            assert method == "GET"
            if url.endswith("/messages"):
                params = kwargs.get("params")
                assert isinstance(params, dict)
                assert params["q"] == "to:me -in:sent after:1 before:2"
                return FakeGmailResponse({"messages": [{"id": "m1"}, {"id": "m2"}]})
            message_id = url.rsplit("/", maxsplit=1)[-1]
            return FakeGmailResponse(
                {
                    "id": message_id,
                    "threadId": f"t-{message_id}",
                    "internalDate": "1784649600000",
                    "snippet": f"snippet {message_id}",
                    "payload": {
                        "headers": [
                            {
                                "name": "From",
                                "value": f"Sender {message_id} <{message_id}@example.com>",
                            },
                            {"name": "Subject", "value": f"Subject {message_id}"},
                            {"name": "Date", "value": "Tue, 21 Jul 2026 12:00:00 -0400"},
                        ]
                    },
                }
            )

    session = ProjectionSession()
    gmail = GmailTool(live=True, access_token="test-token", session=session)

    receipt = gmail.project_message_summaries(
        requested_fields=["subject"],
        query="to:me -in:sent after:1 before:2",
        max_items=4,
    )

    assert receipt["complete"] is True
    assert receipt["item_count"] == 2
    assert receipt["items"] == [
        {"subject": "Subject m1"},
        {"subject": "Subject m2"},
    ]
    assert receipt["provider_read"] is True
    assert receipt["provider_write"] is False
    assert len(session.calls) == 3


def test_gmail_message_projection_refuses_partial_over_limit_set() -> None:
    class OverLimitSession:
        def request(self, method: str, url: str, **_kwargs: object) -> FakeGmailResponse:
            assert method == "GET"
            assert url.endswith("/messages")
            return FakeGmailResponse({"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]})

    gmail = GmailTool(live=True, access_token="test-token", session=OverLimitSession())

    receipt = gmail.project_message_summaries(
        requested_fields=["subject"],
        query="to:me",
        max_items=2,
    )

    assert receipt["complete"] is False
    assert receipt["items"] == []
    assert receipt["item_count"] == 0


def test_live_cli_message_count_returns_verified_read_only_receipt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_gmail_triage as cli

    captured: dict[str, object] = {}

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def count_messages(self, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {
                "message_count": 7,
                "page_count": 2,
                "complete": True,
                "query": kwargs.get("query"),
                "label": "",
                "provider_read": True,
                "provider_write": False,
            }

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--allow-inbox",
            "--no-dry-run",
            "--message-count",
            "--mailbox-direction",
            "inbound",
            "--date-scope",
            "today",
            "--provider-timezone",
            "America/New_York",
            "--window-start",
            "2026-07-21T00:00:00-04:00",
            "--window-end",
            "2026-07-22T00:00:00-04:00",
            "--gmail-query",
            "to:me -in:sent after:1 before:2",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured == {
        "label": None,
        "query": "to:me -in:sent after:1 before:2",
    }
    assert payload["status"] == "completed"
    assert payload["human_summary"] == "You received 7 emails today."
    assert payload["output"]["message_count"] == 7
    assert payload["tool_receipts"][0]["page_count"] == 2
    assert payload["tool_receipts"][0]["verified"] is True
    assert payload["user_facing_result_verified"] is True
    assert payload["public_result"]["completion_confirmed"] is True
    assert payload["side_effects"] == {
        "gmail_draft_created": False,
        "gmail_draft_updated": False,
        "labels_modified": False,
        "email_sent": False,
        "send_enabled": False,
    }


def test_live_cli_message_projection_renders_only_requested_subjects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_gmail_triage as cli

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def project_message_summaries(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["requested_fields"] == ["subject"]
            assert kwargs["query"] == "to:me -in:sent after:1 before:2"
            return {
                "items": [{"subject": "First"}, {"subject": "Second"}],
                "item_count": 2,
                "page_count": 1,
                "complete": True,
                "requested_fields": ["subject"],
                "query": kwargs["query"],
                "label": "",
                "provider_read": True,
                "provider_write": False,
            }

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--allow-inbox",
            "--no-dry-run",
            "--message-projection",
            "--requested-field",
            "subject",
            "--expected-result-count",
            "2",
            "--max-messages",
            "2",
            "--gmail-query",
            "to:me -in:sent after:1 before:2",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["human_summary"] == "- First\n- Second"
    assert payload["output"]["items"] == [
        {"subject": "First"},
        {"subject": "Second"},
    ]
    assert "sender" not in payload["human_summary"].lower()
    assert payload["user_facing_result_verified"] is True
    assert payload["side_effects"]["email_sent"] is False


def test_live_create_draft_posts_draft_without_reading_message() -> None:
    class DraftOnlySession:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def request(self, method: str, url: str, **kwargs: object) -> FakeGmailResponse:
            self.calls.append({"method": method, "url": url, **kwargs})
            assert "/send" not in url
            assert method != "GET"
            if method == "POST" and url.endswith("/drafts"):
                message = kwargs["json"]["message"]  # type: ignore[index]
                assert "threadId" not in message
                raw = str(message["raw"])
                decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                mime = message_from_bytes(decoded)
                assert mime["To"] == "person@example.com"
                assert mime["Subject"] == "Draft subject"
                assert "Standalone draft body" in mime.get_payload()
                return FakeGmailResponse(
                    {"id": "draft-standalone", "message": {"id": "draft-message"}}
                )
            raise AssertionError(f"Unexpected Gmail API call: {method} {url}")

    session = DraftOnlySession()
    gmail = GmailTool(live=True, access_token="test-token", session=session)

    result = gmail.create_draft(
        to="person@example.com",
        subject="Draft subject",
        body="Standalone draft body",
    )

    assert result["status"] == "draft_created"
    assert result["draft_id"] == "draft-standalone"
    assert result["message_id"] == "draft-message"
    assert result["sent"] is False
    assert [call["method"] for call in session.calls] == ["POST"]
    assert str(session.calls[0]["url"]).endswith("/drafts")


def test_gmail_get_thread_returns_summary_fields() -> None:
    class ThreadSession:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def request(self, method: str, url: str, **kwargs: object) -> FakeGmailResponse:
            self.calls.append({"method": method, "url": url, **kwargs})
            if method == "GET" and url.endswith("/threads/thread-1"):
                first = base64.urlsafe_b64encode(
                    b"Thanks for the note. Please send the draft SOW by next Tuesday. "
                    b"Can you also confirm who owns the clinical validation work?"
                ).decode()
                second = base64.urlsafe_b64encode(
                    b"We can review the scope this week. What timeline are you targeting?"
                ).decode()
                return FakeGmailResponse(
                    {
                        "id": "thread-1",
                        "messages": [
                            {
                                "id": "msg-1",
                                "threadId": "thread-1",
                                "internalDate": "1713744000000",
                                "snippet": "Please send the draft SOW by next Tuesday",
                                "labelIds": ["SENT"],
                                "payload": {
                                    "headers": [
                                        {"name": "From", "value": "Anup <operator@example.com>"},
                                        {"name": "To", "value": "Alex <alex@example.com>"},
                                        {"name": "Subject", "value": "Clinical validation scope"},
                                    ],
                                    "mimeType": "text/plain",
                                    "body": {"data": first},
                                },
                            },
                            {
                                "id": "msg-2",
                                "threadId": "thread-1",
                                "internalDate": "1713830400000",
                                "snippet": "What timeline are you targeting?",
                                "labelIds": ["INBOX", "UNREAD"],
                                "payload": {
                                    "headers": [
                                        {"name": "From", "value": "Alex <alex@example.com>"},
                                        {"name": "To", "value": "Anup <operator@example.com>"},
                                        {
                                            "name": "Subject",
                                            "value": "Re: Clinical validation scope",
                                        },
                                    ],
                                    "mimeType": "text/plain",
                                    "body": {"data": second},
                                },
                            },
                        ],
                    }
                )
            raise AssertionError(f"Unexpected Gmail API call: {method} {url}")

    gmail = GmailTool(live=True, access_token="test-token", session=ThreadSession())

    thread = gmail.get_thread("thread-1")

    assert thread["status"] == "read"
    assert thread["thread_id"] == "thread-1"
    assert thread["subject"] == "Re: Clinical validation scope"
    assert thread["message_count"] == 2
    assert thread["send_enabled"] is False
    assert thread["draft_created"] is False
    assert thread["labels_modified"] is False
    assert any(
        "Please send the draft SOW by next Tuesday." in item for item in thread["action_items"]
    )
    assert any("next Tuesday" in item for item in thread["deadlines"])
    assert any(item.endswith("?") for item in thread["open_questions"])
    assert thread["thread_context"].startswith("What timeline are you targeting?")
    assert thread["summary"].startswith("Latest status: We can review the scope this week.")
    assert any(
        "Read-only thread summary used sanitized Gmail message bodies" in item
        for item in thread["triage_limitations"]
    )


def test_thread_overview_leads_with_latest_status_after_completed_scheduling() -> None:
    envelopes = [
        GmailMessageEnvelope(
            sender_name="Eze",
            sender_email="eze@example.test",
            subject="NeuroBlu discussion",
            snippet="Please share a few times for a brief conversation.",
            normalized_body="Please share a few times for a brief conversation.",
            prior_labels=["INBOX"],
        ),
        GmailMessageEnvelope(
            sender_name="Anup",
            sender_email="operator@example.test",
            subject="Re: NeuroBlu discussion",
            snippet="Would Thursday between 1 and 3 PM work?",
            normalized_body="Would Thursday between 1 and 3 PM work?",
            prior_labels=["SENT"],
        ),
        GmailMessageEnvelope(
            sender_name="Anup",
            sender_email="operator@example.test",
            subject="Re: NeuroBlu discussion",
            snippet="Thank you for the discussion. I will keep the platform in mind.",
            normalized_body="Thank you for the discussion. I will keep the platform in mind.",
            prior_labels=["SENT"],
        ),
        GmailMessageEnvelope(
            sender_name="Eze",
            sender_email="eze@example.test",
            subject="Re: NeuroBlu discussion",
            snippet="Thanks for your time. Reach out if collaboration opportunities arise.",
            normalized_body=(
                "Thanks for your time. Reach out if collaboration opportunities arise."
            ),
            prior_labels=["INBOX"],
        ),
    ]

    summary, _participants, actions, _deadlines, questions = gmail_tool._thread_overview(envelopes)

    assert summary.startswith("Latest status: Thanks for your time.")
    assert "Please share a few times" not in summary
    assert actions == []
    assert questions == []


def test_labeled_quoted_context_preserves_original_interest_and_message() -> None:
    raw = """Hi Anup, thanks for reaching out.

On Sun, Jul 5, 2026 at 4:44 PM, Forms <forms@example.test> wrote:
I'm interested in

Neuropsychiatry data analytics solution

Message

What does the dataset contain and is it available via license? I want to assess fit.

View submission in HubSpot
This email was sent to the form owner.
"""

    context = gmail_tool._labeled_quoted_context(raw)

    assert context == [
        "Original interest: Neuropsychiatry data analytics solution",
        (
            "Original message: What does the dataset contain and is it available via "
            "license? I want to assess fit."
        ),
    ]


def test_gmail_get_thread_does_not_promote_onboarding_ctas_to_action_items() -> None:
    class ThreadSession:
        def request(self, method: str, url: str, **_kwargs: object) -> FakeGmailResponse:
            if method == "GET" and url.endswith("/threads/thread-halo"):
                body = base64.urlsafe_b64encode(
                    b"Hi Anup,\n\n"
                    b"We built Halo to help innovators find partners. "
                    b"Start by creating a Partner Listing. "
                    b"Discover partnering requests from top companies. "
                    b"Submit a short, non-confidential proposal in less than an hour.\n\n"
                    b"Best,\nAnna"
                ).decode()
                return FakeGmailResponse(
                    {
                        "id": "thread-halo",
                        "messages": [
                            {
                                "id": "msg-halo",
                                "threadId": "thread-halo",
                                "internalDate": "1772280113000",
                                "snippet": "Start by creating a Partner Listing",
                                "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"],
                                "payload": {
                                    "headers": [
                                        {"name": "From", "value": "Anna <anna@halo.science>"},
                                        {"name": "To", "value": "Anup <operator@example.com>"},
                                        {"name": "Subject", "value": "Welcome to Halo!"},
                                    ],
                                    "mimeType": "text/plain",
                                    "body": {"data": body},
                                },
                            },
                        ],
                    }
                )
            raise AssertionError(f"Unexpected Gmail API call: {method} {url}")

    gmail = GmailTool(live=True, access_token="test-token", session=ThreadSession())

    thread = gmail.get_thread("thread-halo")

    assert thread["status"] == "read"
    assert thread["subject"] == "Welcome to Halo!"
    assert thread["action_items"] == []
    assert any(
        "Promotional or onboarding CTAs were not treated as operator action items." == item
        for item in thread["triage_limitations"]
    )


def test_label_cleanup_preview_is_dry_run_by_default() -> None:
    gmail = GmailTool(live=False)

    preview = gmail.apply_labels(
        "msg-1",
        ["Keystone/Triage", "Keystone/Consulting Opportunity", "Keystone/Action Required"],
        existing_labels=["Keystone/Triage", "Keystone/Vendor", "Keystone/Manual Review"],
        cleanup_obsolete=True,
    )

    assert preview["status"] == "dry-run"
    assert preview["add_labels"] == ["Keystone/Consulting Opportunity", "Keystone/Action Required"]
    assert preview["remove_labels"] == ["Keystone/Vendor", "Keystone/Manual Review"]


def test_live_label_preview_makes_no_modify_call() -> None:
    session = FakeGmailSession()
    gmail = GmailTool(live=True, access_token="test-token", session=session)

    preview = gmail.apply_labels(
        "msg-1",
        ["Keystone/Triage", "Keystone/Newsletter"],
        existing_labels=["Keystone/Triage", "Keystone/Vendor"],
        cleanup_obsolete=True,
        dry_run_preview=True,
    )

    assert preview["status"] == "dry-run"
    assert preview["remove_labels"] == ["Keystone/Vendor"]
    assert session.calls == []


def test_live_cli_read_only_skips_label_and_draft_mutations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.run_gmail_triage as cli

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert label == "INBOX"
            assert max_results == 1
            assert query in {None, ""}
            return [{"id": "msg-1", "threadId": "thread-1"}]

        def get_message(self, message_id: str) -> dict[str, str]:
            return {
                "id": message_id,
                "threadId": "thread-1",
                "from": "Alex <alex@example.com>",
                "subject": "Consulting support",
                "body": "We need consulting support for clinical operations workflow.",
            }

        def get_thread(self, thread_id: str) -> dict[str, object]:
            assert thread_id == "thread-1"
            return {
                "status": "read",
                "thread_id": "thread-1",
                "message_count": 3,
                "summary": "Prior back-and-forth discussed scope and timing.",
                "thread_context": "Prior sent replies discussed scope, timing, and next steps.",
                "messages": [
                    {
                        "id": "msg-1",
                        "envelope": GmailMessageEnvelope(
                            message_id="msg-1",
                            thread_id="thread-1",
                            sender_name="Alex",
                            sender_email="alex@example.com",
                            subject="Consulting support",
                            normalized_body=(
                                "We need consulting support for clinical operations workflow."
                            ),
                            thread_summary="Latest message asks for consulting support.",
                            thread_context=(
                                "Prior sent replies discussed scope, timing, and next steps."
                            ),
                            thread_message_count=3,
                        ).model_dump(mode="json"),
                    }
                ],
                "send_enabled": False,
                "draft_created": False,
                "labels_modified": False,
            }

        def apply_labels(self, message_id: str, labels: list[str]) -> dict[str, object]:
            raise AssertionError("labels require --preview-labels or --apply-labels")

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            raise AssertionError("draft creation requires --create-draft and approval")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_gmail_triage.py", "--live-gmail", "--no-dry-run", "--allow-inbox"],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    message = payload["messages"][0]

    assert message["labels_api"]["status"] == "skipped"
    assert "Prior sent replies discussed scope" in message["thread_context"]
    assert "--preview-labels" in message["labels_api"]["reason"]
    assert message["draft_api"]["status"] == "skipped"
    assert message["draft_api"]["approval_required"] is True


def test_gmail_sdk_payload_repairs_mixed_script_recommendation_noise() -> None:
    import scripts.run_gmail_triage as cli

    payload = {
        "input": (
            "Use only this inline context. Subject: Partnership follow-up for remote "
            "patient monitoring validation From: Alex Rivera Body: Could Keystone help?"
        ),
        "output_type": "EmailTriageResult",
        "output": {
            "subject": "Manual Gmail triage request",
            "category": "collaboration_opportunity",
            "needs_reply": True,
            "recommended_action": (
                "Reply with a brief note and առաջարկing a short call if helpful."
            ),
            "triage_limitations": ["Only inline sanitized context was provided."],
        },
    }

    repaired = cli._repair_gmail_triage_output_hygiene(payload)

    action = repaired["output"]["recommended_action"]
    assert repaired["output"]["subject"] == (
        "Partnership follow-up for remote patient monitoring validation"
    )
    assert "առաջարկing" not in action
    assert "suggesting a short call" in action
    assert repaired["output_hygiene"]["repaired_fields"] == [
        "subject",
        "recommended_action",
    ]
    assert any("mixed-script noise" in item for item in repaired["output"]["triage_limitations"])


def test_sdk_reply_draft_execution_uses_operator_approval_and_message_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_gmail_triage as cli

    captured: dict[str, object] = {}
    fake_gmail = object()

    monkeypatch.setattr(cli, "GmailTool", lambda *, live: fake_gmail if live else None)

    def fake_execute(gmail, **kwargs):
        captured["gmail"] = gmail
        captured.update(kwargs)
        return {
            "status": "draft_created",
            "draft_id": "draft-provider-1",
            "verification": {"passed": True},
            "sent": False,
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "execute_approved_gmail_draft_reply_action", fake_execute)
    outcome = SimpleNamespace(
        raw_context=GmailMessageEnvelope(
            message_id="message-source-1",
            thread_id="thread-source-1",
            sender_name="Example Sender",
            sender_email="sender@example.com",
            subject="Project follow-up",
            normalized_body="Could you send a brief response?",
        ),
        final_output=SimpleNamespace(
            draft_reply="Thanks for the note. I will review this and follow up."
        ),
    )
    args = SimpleNamespace(
        expected_account="operator@example.com",
        approval_reference="operator-command:gmail-draft:abc123",
    )

    result = cli._create_verified_sdk_reply_draft(args, outcome)

    assert result["verification"]["passed"] is True
    assert captured["gmail"] is fake_gmail
    assert captured["message_id"] == "message-source-1"
    assert captured["expected_to"] == "sender@example.com"
    assert captured["expected_subject"] == "Re: Project follow-up"
    assert captured["expected_account"] == "operator@example.com"
    assert captured["approval_reference"] == "operator-command:gmail-draft:abc123"


def test_sdk_draft_update_reuses_internally_resolved_provider_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_gmail_triage as cli

    captured: dict[str, object] = {}
    fake_gmail = object()
    monkeypatch.setattr(cli, "GmailTool", lambda *, live: fake_gmail if live else None)

    def fake_execute(gmail, **kwargs):
        captured["gmail"] = gmail
        captured.update(kwargs)
        return {
            "status": "draft_updated",
            "draft_id": kwargs["draft_id"],
            "verification": {"passed": True},
            "sent": False,
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "execute_approved_gmail_draft_action", fake_execute)
    outcome = SimpleNamespace(
        final_output=SimpleNamespace(draft_reply="Thanks for the note. I will follow up next week.")
    )
    args = SimpleNamespace(
        expected_account="operator@example.com",
        approval_reference="operator-command:gmail-draft:update123",
    )
    resolved_draft = {
        "draft_id": "draft-provider-1",
        "to": "reviewer@example.com",
        "subject": "Re: Project follow-up",
    }

    result = cli._update_verified_sdk_draft(args, outcome, resolved_draft)

    assert result["verification"]["passed"] is True
    assert captured["gmail"] is fake_gmail
    assert captured["draft_id"] == "draft-provider-1"
    assert captured["to"] == "reviewer@example.com"
    assert captured["subject"] == "Re: Project follow-up"
    assert captured["approval_reference"] == "operator-command:gmail-draft:update123"

    clean_payload = {
        "output_type": "EmailTriageResult",
        "output": {
            "recommended_action": "Reply to José and review 株式会社 context manually.",
        },
    }
    assert cli._repair_gmail_triage_output_hygiene(clean_payload) == clean_payload


def test_live_cli_passes_gmail_query_for_targeted_read_only_triage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_gmail_triage as cli

    seen_queries: list[str | None] = []

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert label == "UNREAD"
            assert max_results == 1
            seen_queries.append(query)
            return [{"id": "msg-coi", "threadId": "thread-coi"}]

        def get_message(self, message_id: str) -> dict[str, str]:
            return {
                "id": message_id,
                "threadId": "thread-coi",
                "from": "Broker <broker@example.com>",
                "subject": "COI request for consulting coverage",
                "body": (
                    "Thanks. Please send the current certificate of insurance for the "
                    "consulting engagement when ready."
                ),
            }

        def apply_labels(self, message_id: str, labels: list[str]) -> dict[str, object]:
            raise AssertionError("labels were not requested")

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            raise AssertionError("draft creation requires --create-draft")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--label-filter",
            "UNREAD",
            "--gmail-query",
            "subject:(COI OR certificate of insurance) broker",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert seen_queries == ["subject:(COI OR certificate of insurance) broker"]
    assert payload["query"] == "subject:(COI OR certificate of insurance) broker"
    assert payload["messages"][0]["draft_api"]["status"] == "skipped"


def test_priority_grouping_live_query_defaults_to_recent_non_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    import scripts.run_gmail_triage as cli

    seen_queries: list[str | None] = []

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert label == "INBOX"
            assert max_results == 10
            seen_queries.append(query)
            return []

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    args = argparse.Namespace(
        label_filter=None,
        allow_inbox=True,
        gmail_query="",
        lookback_days=3,
        max_messages=10,
    )

    assert cli._live_gmail_envelopes_for_priority_grouping(args) == []
    assert seen_queries == ["newer_than:3d"]


def test_priority_grouping_live_query_honors_explicit_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    import scripts.run_gmail_triage as cli

    seen_queries: list[str | None] = []

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert label == "INBOX"
            seen_queries.append(query)
            return []

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    args = argparse.Namespace(
        label_filter=None,
        allow_inbox=True,
        gmail_query="newer_than:3d",
        lookback_days=3,
        max_messages=10,
    )

    assert cli._live_gmail_envelopes_for_priority_grouping(args) == []
    assert seen_queries == ["newer_than:3d"]


def test_priority_grouping_live_query_skips_guardrail_blocked_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    import scripts.run_gmail_triage as cli
    from keystone_agents.sdk import ToolGuardrailViolation

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            return [
                {"id": "blocked", "threadId": "thread-blocked"},
                {"id": "safe", "threadId": "thread-safe"},
            ]

        def get_message(self, message_id: str) -> dict[str, str]:
            if message_id == "blocked":
                raise ToolGuardrailViolation("blocked")
            return {
                "id": "safe",
                "threadId": "thread-safe",
                "from": "Alex <alex@example.com>",
                "subject": "Clinical AI collaboration",
                "body": "Would Keystone be open to comparing notes next week?",
            }

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    args = argparse.Namespace(
        label_filter=None,
        allow_inbox=True,
        gmail_query="newer_than:3d",
        lookback_days=3,
        max_messages=10,
    )

    envelopes = cli._live_gmail_envelopes_for_priority_grouping(args)

    assert [envelope.message_id for envelope in envelopes] == ["safe"]


def test_priority_grouping_search_page_guardrail_falls_back_to_per_message_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    import scripts.run_gmail_triage as cli
    from keystone_agents.sdk import ToolGuardrailViolation

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def search_message_summaries(self, **_kwargs: object) -> list[dict[str, object]]:
            raise ToolGuardrailViolation("one metadata summary was unsafe")

        def list_recent_messages(self, **_kwargs: object) -> list[dict[str, str]]:
            return [
                {"id": "blocked", "threadId": "thread-blocked"},
                {"id": "safe", "threadId": "thread-safe"},
            ]

        def batch_get_messages(
            self,
            message_ids: list[str],
            *,
            skip_blocked: bool = False,
        ) -> list[dict[str, object]]:
            assert message_ids == ["blocked", "safe"]
            assert skip_blocked is True
            return [
                {
                    "id": "safe",
                    "threadId": "thread-safe",
                    "from": "Alex <alex@example.com>",
                    "subject": "Clinical AI collaboration",
                    "body": "Would Keystone be open to comparing notes next week?",
                }
            ]

        def get_thread(self, thread_id: str) -> dict[str, object]:
            raise AssertionError(f"thread expansion was not needed: {thread_id}")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    args = argparse.Namespace(
        label_filter=None,
        allow_inbox=True,
        gmail_query="newer_than:3d",
        lookback_days=3,
        max_messages=10,
    )

    envelopes = cli._live_gmail_envelopes_for_priority_grouping(args)

    assert [envelope.message_id for envelope in envelopes] == ["safe"]


def test_live_sdk_message_resolution_expands_exact_subject_to_all_mail() -> None:
    import scripts.run_gmail_triage as cli

    calls: list[tuple[str | None, str, int]] = []

    class FakeGmail:
        def list_recent_messages(
            self,
            *,
            label: str | None,
            max_results: int,
            query: str,
        ) -> list[dict[str, str]]:
            calls.append((label, query, max_results))
            if label is None:
                return [{"id": "message-1", "threadId": "thread-1"}]
            return []

    query = 'subject:"Why Healthtech Needs a New Kind of Product Leader"'
    refs, diagnostics = cli._resolve_live_sdk_message_refs(
        FakeGmail(),
        request_text=(
            'Find the Gmail email with subject "Why Healthtech Needs a New Kind '
            'of Product Leader" and draft a reply here.'
        ),
        label="INBOX",
        query=query,
        max_results=1,
    )

    assert refs == [{"id": "message-1", "threadId": "thread-1"}]
    assert calls == [
        ("INBOX", query, 5),
        (None, query, 5),
    ]
    assert diagnostics["candidate_count"] == 1
    assert diagnostics["provider_write"] is False


def test_live_sdk_message_resolution_deduplicates_relaxed_matches_by_thread() -> None:
    import scripts.run_gmail_triage as cli

    calls: list[tuple[str | None, str]] = []

    class FakeGmail:
        def list_recent_messages(
            self,
            *,
            label: str | None,
            max_results: int,
            query: str,
        ) -> list[dict[str, str]]:
            del max_results
            calls.append((label, query))
            if "subject:healthtech" in query:
                return [
                    {"id": "message-new", "threadId": "thread-1"},
                    {"id": "message-old", "threadId": "thread-1"},
                ]
            return []

    refs, diagnostics = cli._resolve_live_sdk_message_refs(
        FakeGmail(),
        request_text=(
            'Find the Gmail email with subject "Why Healthtech Needs a New Kind '
            'of Product Leader" and draft a reply here.'
        ),
        label="INBOX",
        query='subject:"Why Healthtech Needs a New Kind of Product Leader"',
        max_results=1,
    )

    assert refs == [{"id": "message-new", "threadId": "thread-1"}]
    assert len(calls) == 3
    assert diagnostics["attempts"][-1]["scope"] == "all_mail_relaxed_subject"


def test_live_sdk_message_resolution_returns_safe_no_match_blocker() -> None:
    import scripts.run_gmail_triage as cli

    class FakeGmail:
        def list_recent_messages(self, **_kwargs: object) -> list[dict[str, str]]:
            return []

    with pytest.raises(cli.GmailTargetResolutionError) as exc_info:
        cli._resolve_live_sdk_message_refs(
            FakeGmail(),
            request_text='Find the Gmail email with subject "Missing Example".',
            label="INBOX",
            query='subject:"Missing Example"',
            max_results=1,
        )

    payload = exc_info.value.payload
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "gmail_target_not_found"
    assert payload["retrieval_diagnostics"]["provider_read"] is True
    assert payload["retrieval_diagnostics"]["provider_write"] is False
    assert len(payload["retrieval_diagnostics"]["attempts"]) == 3
    assert "message-" not in json.dumps(payload)


def test_priority_grouping_live_retrieval_uses_staged_search_batch_and_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    import scripts.run_gmail_triage as cli

    batch_reads: list[list[str]] = []
    thread_reads: list[str] = []

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def search_message_summaries(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, object]]:
            assert label == "INBOX"
            assert max_results == 3
            assert query == "newer_than:3d"
            return [
                {
                    "id": "newsletter",
                    "threadId": "thread-newsletter",
                    "from": "Events <events@example.com>",
                    "subject": "Weekly Roundup",
                    "snippet": "Newsletter digest.",
                    "labelIds": ["INBOX"],
                },
                {
                    "id": "quote",
                    "threadId": "thread-quote",
                    "from": "Andy <andy@example.com>",
                    "subject": "RE: CFC Quote for Keystone Neuroinformatics LLC",
                    "snippet": "Please review the certificate of insurance.",
                    "labelIds": ["INBOX"],
                },
                {
                    "id": "onboarding",
                    "threadId": "thread-onboarding",
                    "from": "Alex <alex@example.com>",
                    "subject": "Initial onboarding email",
                    "snippet": "Login instructions are included.",
                    "labelIds": ["INBOX"],
                },
            ]

        def batch_get_messages(
            self,
            message_ids: list[str],
            *,
            skip_blocked: bool = False,
        ) -> list[dict[str, object]]:
            assert skip_blocked is True
            batch_reads.append(message_ids)
            return [
                {
                    "id": message_id,
                    "threadId": f"thread-{message_id}",
                    "from": "Sender <sender@example.com>",
                    "subject": "Full message",
                    "body": f"Full body for {message_id}",
                    "labelIds": ["INBOX"],
                }
                for message_id in message_ids
            ]

        def get_thread(self, thread_id: str) -> dict[str, object]:
            thread_reads.append(thread_id)
            message_id = thread_id.replace("thread-", "")
            return {
                "thread_id": thread_id,
                "message_count": 2,
                "summary": f"Thread summary for {thread_id}",
                "thread_context": f"Full thread context for {thread_id}",
                "messages": [
                    {
                        "id": message_id,
                        "threadId": thread_id,
                        "from": "Sender <sender@example.com>",
                        "subject": "Full thread message",
                        "body": f"Thread body for {message_id}",
                        "thread_context": f"Full thread context for {thread_id}",
                        "thread_message_count": 2,
                    }
                ],
            }

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    args = argparse.Namespace(
        label_filter=None,
        allow_inbox=True,
        gmail_query="newer_than:3d",
        lookback_days=3,
        max_messages=3,
    )

    envelopes = cli._live_gmail_envelopes_for_priority_grouping(args)

    assert batch_reads == [["quote", "onboarding"]]
    assert thread_reads == ["thread-quote", "thread-onboarding"]
    assert [envelope.message_id for envelope in envelopes] == ["newsletter", "quote", "onboarding"]
    assert "search summary only" in " ".join(envelopes[0].triage_limitations).lower()
    assert "Full thread context" in envelopes[1].thread_context


def test_live_cli_thread_summary_reads_sent_label(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_gmail_triage as cli

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert label == "SENT"
            assert max_results == 1
            assert query == "subject:(clinical validation)"
            return [{"id": "msg-sent", "threadId": "thread-sent"}]

        def get_thread(self, thread_id: str) -> dict[str, object]:
            assert thread_id == "thread-sent"
            return {
                "status": "read",
                "thread_id": "thread-sent",
                "subject": "Clinical validation scope",
                "summary": "Thread about Clinical validation scope. Participants: Anup, Alex.",
                "thread_context": "Clinical validation scope follow-up.",
                "message_count": 2,
                "latest_received_at": "2026-04-22T14:00:00Z",
                "participants": ["Anup", "Alex"],
                "action_items": ["Please send the draft SOW by next Tuesday."],
                "deadlines": ["Please send the draft SOW by next Tuesday."],
                "open_questions": ["What timeline are you targeting?"],
                "triage_limitations": [
                    "Read-only thread summary used sanitized Gmail message bodies "
                    "from the selected thread."
                ],
                "messages": [
                    {
                        "id": "msg-sent",
                        "received_at": "2026-04-22T12:00:00Z",
                        "sender_name": "Anup",
                        "sender_email": "operator@example.com",
                        "subject": "Clinical validation scope",
                        "snippet": "Please send the draft SOW by next Tuesday.",
                        "prior_labels": ["SENT"],
                        "thread_summary": "Please send the draft SOW by next Tuesday.",
                    }
                ],
                "send_enabled": False,
                "draft_created": False,
                "labels_modified": False,
            }

        def apply_labels(self, message_id: str, labels: list[str]) -> dict[str, object]:
            raise AssertionError("thread summary mode must not apply labels")

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            raise AssertionError("thread summary mode must not create drafts")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--thread-summary",
            "--label-filter",
            "SENT",
            "--gmail-query",
            "subject:(clinical validation)",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    thread = GmailThreadSummaryResult.model_validate(payload["threads"][0])

    assert payload["mode"] == "live-gmail-thread-summary"
    assert payload["label"] == "SENT"
    assert payload["send_enabled"] is False
    assert thread.source_label == "SENT"
    assert thread.query == "subject:(clinical validation)"
    assert thread.action_items == ["Please send the draft SOW by next Tuesday."]
    assert thread.open_questions == ["What timeline are you targeting?"]


def test_live_cli_thread_summary_blocks_mutation_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_gmail_triage as cli

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(self, *args: object, **kwargs: object) -> list[dict[str, str]]:
            raise AssertionError("blocked thread summary must not read Gmail messages")

        def get_thread(self, thread_id: str) -> dict[str, object]:
            raise AssertionError("blocked thread summary must not fetch Gmail threads")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--thread-summary",
            "--allow-inbox",
            "--create-draft",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = GmailClarificationResult.model_validate(json.loads(capsys.readouterr().out))

    assert payload.status == "blocked"
    assert payload.reason_code == "thread_summary_read_only"
    assert payload.send_enabled is False
    assert payload.draft_created is False
    assert "read-only" in payload.message.lower()


def test_live_cli_create_draft_multiple_messages_returns_clarification(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_gmail_triage as cli

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert label == "INBOX"
            assert max_results == 2
            assert query in {None, ""}
            return [
                {"id": "msg-1", "threadId": "thread-1"},
                {"id": "msg-2", "threadId": "thread-2"},
            ]

        def get_message(self, message_id: str) -> dict[str, str]:
            raise AssertionError("clarification path must not fetch message bodies")

        def apply_labels(self, message_id: str, labels: list[str]) -> dict[str, object]:
            raise AssertionError("clarification path must not apply labels")

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            raise AssertionError("clarification path must not create Gmail drafts")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--allow-inbox",
            "--create-draft",
            "--max-messages",
            "2",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = GmailClarificationResult.model_validate(json.loads(capsys.readouterr().out))

    assert payload.status == "clarification_required"
    assert payload.reason_code == "draft_target_ambiguous"
    assert payload.candidate_count == 2
    assert payload.missing_inputs == ["specific gmail_query or thread_id"]
    assert payload.send_enabled is False


def test_live_cli_preview_labels_preserves_sanitized_html_attachment_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.run_gmail_triage as cli

    envelope = GmailMessageEnvelope(
        message_id="msg-html",
        thread_id="thread-html",
        received_at="2026-04-22T14:00:00Z",
        sender_name="Alex",
        sender_email="alex@example.com",
        subject="HTML consulting inquiry",
        snippet="clinical operations workflow",
        prior_labels=["INBOX", "Keystone/Vendor"],
        normalized_body="Could we discuss a clinical operations workflow?",
        attachment_metadata=[
            GmailAttachmentMetadata(
                filename="brief.pdf",
                mime_type="application/pdf",
                size_bytes=2048,
                attachment_id_present=True,
            )
        ],
        thread_summary="Could we discuss a clinical operations workflow?",
        thread_context="Only the selected message was available.",
        triage_limitations=[
            "Attachments were not ingested; metadata only was screened.",
            "Quoted prior replies were stripped before triage.",
        ],
    )
    label_calls: list[dict[str, object]] = []

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert query in {None, ""}
            return [{"id": "msg-html", "threadId": "thread-html"}]

        def get_message(self, message_id: str) -> dict[str, object]:
            return {
                "id": message_id,
                "threadId": "thread-html",
                "labelIds": ["INBOX", "Keystone/Vendor"],
                "envelope": envelope.model_dump(mode="json"),
            }

        def apply_labels(
            self,
            message_id: str,
            labels: list[str],
            *,
            existing_labels: list[str] | None = None,
            cleanup_obsolete: bool = False,
            dry_run_preview: bool | None = None,
        ) -> dict[str, object]:
            label_calls.append(
                {
                    "message_id": message_id,
                    "labels": labels,
                    "existing_labels": existing_labels or [],
                    "cleanup_obsolete": cleanup_obsolete,
                    "dry_run_preview": dry_run_preview,
                }
            )
            return {
                "status": "dry-run",
                "message_id": message_id,
                "labels": labels,
                "existing_labels": existing_labels or [],
                "add_labels": ["Keystone/Consulting Opportunity"],
                "remove_labels": ["Keystone/Vendor"],
                "cleanup_obsolete": cleanup_obsolete,
            }

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            raise AssertionError("preview-labels must not create drafts")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--allow-inbox",
            "--preview-labels",
            "--cleanup-labels",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    message = payload["messages"][0]

    assert label_calls[0]["dry_run_preview"] is True
    assert label_calls[0]["cleanup_obsolete"] is True
    assert message["labels_api"]["status"] == "dry-run"
    assert message["labels_api"]["remove_labels"] == ["Keystone/Vendor"]
    assert "Older quoted reply" not in message["normalized_body"]
    assert message["attachment_metadata"][0]["filename"] == "brief.pdf"
    assert any("Attachments were not ingested" in item for item in message["triage_limitations"])
    assert any("Quoted prior replies" in item for item in message["triage_limitations"])
    assert message["draft_api"]["status"] == "skipped"


def test_gmail_live_request_refreshes_token_after_401(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    credentials_file = tmp_path / "credentials.json"
    token_file.write_text(
        json.dumps({"refresh_token": "refresh-token", "access_token": "expired-token"}),
        encoding="utf-8",
    )
    credentials_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test-client-id",
                    "client_secret": "test-client-secret",
                }
            }
        ),
        encoding="utf-8",
    )

    class RefreshingSession:
        def __init__(self) -> None:
            self.request_calls: list[dict[str, object]] = []
            self.post_calls: list[dict[str, object]] = []

        def request(self, method: str, url: str, **kwargs: object) -> FakeGmailResponse:
            self.request_calls.append({"method": method, "url": url, **kwargs})
            auth = str((kwargs.get("headers") or {}).get("Authorization") or "")
            if len(self.request_calls) == 1:
                assert auth == "Bearer expired-token"
                return FakeGmailResponse({}, status_code=401)
            assert auth == "Bearer refreshed-token"
            return FakeGmailResponse({"messages": [{"id": "msg-1", "threadId": "thread-1"}]})

        def post(self, url: str, **kwargs: object) -> FakeGmailResponse:
            self.post_calls.append({"url": url, **kwargs})
            return FakeGmailResponse({"access_token": "refreshed-token"})

    session = RefreshingSession()
    gmail = GmailTool(
        live=True,
        token_file=token_file,
        credentials_file=credentials_file,
        access_token="expired-token",
        session=session,
    )

    messages = gmail.list_recent_messages(label="UNREAD", max_results=1)

    assert messages == [{"id": "msg-1", "threadId": "thread-1"}]
    assert len(session.request_calls) == 2
    assert len(session.post_calls) == 1
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == "refreshed-token"


def test_gmail_live_request_401_refresh_failure_has_reauth_guidance(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    credentials_file = tmp_path / "credentials.json"
    token_file.write_text(
        json.dumps({"refresh_token": "refresh-token", "access_token": "expired-token"}),
        encoding="utf-8",
    )
    credentials_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test-client-id",
                    "client_secret": "test-client-secret",
                }
            }
        ),
        encoding="utf-8",
    )

    class FailingRefreshSession:
        def request(self, method: str, url: str, **kwargs: object) -> FakeGmailResponse:
            return FakeGmailResponse({}, status_code=401)

        def post(self, url: str, **kwargs: object) -> FakeGmailResponse:
            return FakeGmailResponse({"error": "invalid_grant"}, status_code=400)

    gmail = GmailTool(
        live=True,
        token_file=token_file,
        credentials_file=credentials_file,
        access_token="expired-token",
        session=FailingRefreshSession(),
    )

    with pytest.raises(
        GmailConfigurationError,
        match=r"gmail_oauth_login\.py --force",
    ):
        gmail.list_recent_messages(label="UNREAD", max_results=1)


def test_missing_oauth_credentials_gives_clear_error(tmp_path: Path) -> None:
    gmail = GmailTool(
        live=True,
        token_file=tmp_path / "missing-token.json",
        credentials_file=tmp_path / "missing-credentials.json",
    )

    with pytest.raises(GmailConfigurationError, match="Gmail OAuth token file"):
        gmail.list_recent_messages(label="UNREAD", max_results=1)


def test_gmail_oauth_readiness_disabled_does_not_require_files(tmp_path: Path) -> None:
    readiness = gmail_oauth_readiness(
        {
            "KEYSTONE_ENABLE_LIVE_GMAIL": "false",
            "GOOGLE_TOKEN_FILE": str(tmp_path / "missing-token.json"),
            "GOOGLE_CREDENTIALS_FILE": str(tmp_path / "missing-credentials.json"),
        }
    )

    assert readiness["readiness"] == "disabled"
    assert readiness["configured"] is False
    assert readiness["token_file_present"] is False
    assert readiness["credentials_file_present"] is False
    assert readiness["send_supported"] is False


def test_gmail_oauth_readiness_live_validates_files_without_network(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    credentials_file = tmp_path / "credentials.json"
    token_file.write_text(json.dumps({"refresh_token": "test-refresh-token"}), encoding="utf-8")
    credentials_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test-client-id",
                    "client_secret": "test-client-secret",
                }
            }
        ),
        encoding="utf-8",
    )

    readiness = gmail_oauth_readiness(
        {
            "KEYSTONE_ENABLE_LIVE_GMAIL": "true",
            "GOOGLE_TOKEN_FILE": str(token_file),
            "GOOGLE_CREDENTIALS_FILE": str(credentials_file),
        }
    )

    assert readiness["readiness"] == "ready"
    assert readiness["configured"] is True
    assert readiness["token_has_refresh_token"] is True
    assert readiness["allowed_live_operations"] == [
        "read",
        "label",
        "mailbox_state",
        "create_draft",
        "update_draft",
        "send_test_draft",
    ]
    assert readiness["send_supported"] is False


def test_gmail_oauth_readiness_live_without_refresh_token_is_misconfigured(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "token.json"
    credentials_file = tmp_path / "credentials.json"
    token_file.write_text(json.dumps({"access_token": "temporary-token"}), encoding="utf-8")
    credentials_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test-client-id",
                    "client_secret": "test-client-secret",
                }
            }
        ),
        encoding="utf-8",
    )

    readiness = gmail_oauth_readiness(
        {
            "KEYSTONE_ENABLE_LIVE_GMAIL": "true",
            "GOOGLE_TOKEN_FILE": str(token_file),
            "GOOGLE_CREDENTIALS_FILE": str(credentials_file),
        }
    )

    assert readiness["readiness"] == "misconfigured"
    assert readiness["configured"] is False
    assert readiness["token_has_access_or_refresh_token"] is True
    assert readiness["token_has_refresh_token"] is False
    assert any("cannot recover from HTTP 401" in item for item in readiness["messages"])


def test_create_draft_flag_required_for_live_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.run_gmail_triage as cli

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True
            self.created = False

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert label == "INBOX"
            assert max_results == 1
            assert query in {None, ""}
            return [{"id": "msg-1", "threadId": "thread-1"}]

        def get_message(self, message_id: str) -> dict[str, str]:
            return {
                "id": message_id,
                "threadId": "thread-1",
                "from": "Alex <alex@example.com>",
                "subject": "Consulting support",
                "body": "We need consulting support for clinical operations workflow.",
            }

        def apply_labels(self, message_id: str, labels: list[str]) -> dict[str, object]:
            return {"status": "labels_applied", "message_id": message_id, "labels": labels}

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            self.created = True
            return {"status": "draft_created", "message_id": message_id, "approval_required": True}

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--allow-inbox",
            "--apply-labels",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["messages"][0]["draft_api"]["status"] == "skipped"
    assert "--create-draft" in payload["messages"][0]["draft_api"]["reason"]
    assert payload["messages"][0]["labels_api"]["status"] == "labels_applied"


def test_live_cli_cleanup_labels_requires_label_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_gmail_triage as cli

    monkeypatch.setattr(
        cli,
        "GmailTool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("GmailTool must not be constructed before label gate validation")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--allow-inbox",
            "--cleanup-labels",
        ],
    )

    with pytest.raises(SystemExit, match="--cleanup-labels requires"):
        cli.main()


def test_live_cli_create_draft_requires_approved_local_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scripts.run_gmail_triage as cli

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert query in {None, ""}
            return [{"id": "msg-1", "threadId": "thread-1"}]

        def get_message(self, message_id: str) -> dict[str, str]:
            return {
                "id": message_id,
                "threadId": "thread-1",
                "from": "Alex <alex@example.com>",
                "subject": "Consulting support",
                "body": "We need consulting support for clinical operations workflow.",
            }

        def apply_labels(self, message_id: str, labels: list[str]) -> dict[str, object]:
            return {"status": "labels_applied", "message_id": message_id, "labels": labels}

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            raise AssertionError("create_draft_reply must not run without approval")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--allow-inbox",
            "--create-draft",
            "--database-url",
            f"sqlite:///{tmp_path / 'missing-approval.db'}",
        ],
    )

    with pytest.raises(SystemExit, match="approved_for_send approval record"):
        cli.main()


@pytest.mark.parametrize(
    ("decision", "scope"),
    [
        (ApprovalState.APPROVED_FOR_DRAFTING, ApprovalScope.DRAFTING),
        (ApprovalState.APPROVED_FOR_EXTERNAL_USE, ApprovalScope.EXTERNAL_USE),
    ],
)
def test_live_cli_non_send_approval_scope_blocks_draft_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    decision: ApprovalState,
    scope: ApprovalScope,
) -> None:
    import scripts.run_gmail_triage as cli

    database_url = f"sqlite:///{tmp_path / f'{decision.value}.db'}"
    SQLiteStore(database_url).save_approval(
        object_type="gmail_draft",
        object_id="msg-1",
        decision=decision,
        scope=scope,
    )

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert query in {None, ""}
            return [{"id": "msg-1", "threadId": "thread-1"}]

        def get_message(self, message_id: str) -> dict[str, str]:
            return {
                "id": message_id,
                "threadId": "thread-1",
                "from": "Alex <alex@example.com>",
                "subject": "Consulting support",
                "body": "We need consulting support for clinical operations workflow.",
            }

        def apply_labels(self, message_id: str, labels: list[str]) -> dict[str, object]:
            raise AssertionError("labels were not requested")

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            raise AssertionError("non-send approval scopes must not create Gmail drafts")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--allow-inbox",
            "--create-draft",
            "--database-url",
            database_url,
        ],
    )

    with pytest.raises(SystemExit, match="approved_for_send approval record"):
        cli.main()


@pytest.mark.parametrize("decision", [ApprovalState.REJECTED, ApprovalState.EXPIRED])
def test_live_cli_rejected_or_expired_approval_blocks_draft_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    decision: ApprovalState,
) -> None:
    import scripts.run_gmail_triage as cli

    database_url = f"sqlite:///{tmp_path / f'{decision.value}.db'}"
    store = SQLiteStore(database_url)
    store.save_approval(
        object_type="gmail_draft",
        object_id="msg-1",
        decision=ApprovalState.APPROVED_FOR_SEND,
        scope=ApprovalScope.SEND,
    )
    store.save_approval(
        object_type="gmail_draft",
        object_id="msg-1",
        decision=decision,
        scope=ApprovalScope.SEND,
    )

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert query in {None, ""}
            return [{"id": "msg-1", "threadId": "thread-1"}]

        def get_message(self, message_id: str) -> dict[str, str]:
            return {
                "id": message_id,
                "threadId": "thread-1",
                "from": "Alex <alex@example.com>",
                "subject": "Consulting support",
                "body": "We need consulting support for clinical operations workflow.",
            }

        def apply_labels(self, message_id: str, labels: list[str]) -> dict[str, object]:
            return {"status": "labels_applied", "message_id": message_id, "labels": labels}

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            raise AssertionError("terminal approvals must block draft creation")

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--allow-inbox",
            "--create-draft",
            "--database-url",
            database_url,
        ],
    )

    with pytest.raises(SystemExit, match=rf"{decision.value}/send"):
        cli.main()


def test_live_cli_approved_for_send_can_create_draft_but_never_sends(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import scripts.run_gmail_triage as cli

    database_url = f"sqlite:///{tmp_path / 'approved.db'}"
    SQLiteStore(database_url).save_approval(
        object_type="gmail_draft",
        object_id="msg-1",
        decision=ApprovalState.APPROVED_FOR_SEND,
        scope=ApprovalScope.SEND,
        reviewer="reviewer@example.com",
    )
    created: list[tuple[str, str]] = []

    class FakeLiveGmail:
        def __init__(self, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            label: str | None,
            max_results: int,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert query in {None, ""}
            return [{"id": "msg-1", "threadId": "thread-1"}]

        def get_message(self, message_id: str) -> dict[str, str]:
            return {
                "id": message_id,
                "threadId": "thread-1",
                "from": "Alex <alex@example.com>",
                "subject": "Consulting support",
                "body": "We need consulting support for clinical operations workflow.",
            }

        def apply_labels(self, message_id: str, labels: list[str]) -> dict[str, object]:
            return {"status": "labels_applied", "message_id": message_id, "labels": labels}

        def create_draft_reply(self, message_id: str, body: str) -> dict[str, object]:
            created.append((message_id, body))
            return {
                "status": "draft_created",
                "message_id": message_id,
                "draft_id": "draft-1",
            }

    monkeypatch.setattr(cli, "GmailTool", FakeLiveGmail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--live-gmail",
            "--no-dry-run",
            "--allow-inbox",
            "--create-draft",
            "--database-url",
            database_url,
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    draft_api = payload["messages"][0]["draft_api"]

    assert created and created[0][0] == "msg-1"
    assert draft_api["status"] == "draft_created"
    assert draft_api["sent"] is False
    assert draft_api["approval_required"] is True
    assert payload["messages"][0]["draft_approval"]["decision"] == "approved_for_send"


def test_live_cli_requires_label_filter_unless_allow_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.run_gmail_triage as cli

    monkeypatch.setattr(sys, "argv", ["run_gmail_triage.py", "--live-gmail", "--no-dry-run"])

    with pytest.raises(SystemExit, match="Use --label-filter"):
        cli.main()


def test_send_email_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        GmailTool().send_email("person@example.com", "Subject", "Body")
    with pytest.raises(NotImplementedError):
        gmail_tool.send_email("person@example.com", "Subject", "Body")
