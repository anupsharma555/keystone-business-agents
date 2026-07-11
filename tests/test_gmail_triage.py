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
    build_gmail_triage_agent,
    run_gmail_triage_fixture,
    triage_email_fixture,
    triage_gmail_message_envelope,
)
from keystone_agents.models import GmailPriorityGroupingSDKInput, GmailTriageSDKInput
from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.email_triage import (
    GMAIL_PRIMARY_LABEL_SET,
    EmailTriageResult,
    GmailAttachmentMetadata,
    GmailClarificationResult,
    GmailMessageEnvelope,
    GmailPriorityGroupingResult,
    GmailThreadSummaryResult,
    normalize_managed_gmail_labels,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import gmail_tool
from keystone_agents.tools.email_style_tool import load_email_style_profile_fixture
from keystone_agents.tools.gmail_tool import (
    GmailConfigurationError,
    GmailTool,
    gmail_message_envelope_from_api,
    gmail_oauth_readiness,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _fixture(name: str) -> Path:
    return FIXTURES / name


def _assert_no_em_dash(value: object) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert "\u2014" not in encoded


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

    summary, _participants, actions, _deadlines, questions = gmail_tool._thread_overview(
        envelopes
    )

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
    assert any(
        "mixed-script noise" in item
        for item in repaired["output"]["triage_limitations"]
    )


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
        final_output=SimpleNamespace(
            draft_reply="Thanks for the note. I will follow up next week."
        )
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
