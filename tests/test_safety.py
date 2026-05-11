from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from keystone_agents.agents.business_research_analyst import build_business_research_analyst_agent
from keystone_agents.agents.gmail_triage import (
    EmailFixture,
    build_gmail_triage_agent,
    run_gmail_triage_fixture,
    triage_email_fixture,
)
from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
from keystone_agents.agents.outreach_composer import (
    build_outreach_composer_agent,
    check_unsupported_claims,
)
from keystone_agents.config import Settings, require_live_mode
from keystone_agents.guardrails import assess_text_guardrails, assess_tool_payload_guardrails
from keystone_agents.models import AgentRunRequest, RunMode
from keystone_agents.run import run_agent_dry
from keystone_agents.sdk import LocalAgent, ToolGuardrailViolation
from keystone_agents.tools.approval_tool import post_approval_request
from keystone_agents.tools.gmail_tool import GmailTool, create_gmail_draft_reply
from keystone_agents.tools.serper_tool import SerperTool, search_web
from keystone_agents.tools.slack_tool import SlackTool
from keystone_agents.tools.storage_tool import StorageTool

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_require_live_mode_blocks_default_settings() -> None:
    try:
        require_live_mode(Settings())
    except RuntimeError as exc:
        assert "Live mode is disabled" in str(exc)
    else:
        raise AssertionError("Expected live mode guard to raise.")


def test_dry_run_rejects_live_request() -> None:
    agent = LocalAgent(name="test", instructions="test")

    try:
        run_agent_dry(agent, AgentRunRequest(mode=RunMode.LIVE))
    except RuntimeError as exc:
        assert "Live execution is not implemented" in str(exc)
    else:
        raise AssertionError("Expected live execution guard to raise.")


def test_gmail_tool_does_not_call_live_by_default() -> None:
    result = GmailTool().create_draft(
        to="person@example.com",
        subject="Draft",
        body="Offline draft body",
    )

    assert result["status"] == "dry-run"


def test_serper_sdk_tool_does_not_call_live_by_default(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key-that-must-not-be-used")

    def fail_post(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("default SDK Serper tool must not call the network")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_post)

    assert search_web("digital health funding", num_results=3) == []


def test_phi_like_fixture_is_flagged_and_no_substantive_draft() -> None:
    result = triage_email_fixture(
        EmailFixture(
            subject="Patient Jane Doe diagnosis follow-up",
            body=(
                "Patient Jane Doe was diagnosed with depression and needs identifiable "
                "treatment details."
            ),
            sender_name="Clinician",
            sender_email="clinician@example.org",
        )
    )

    assert "possible_phi" in result.risk_flags
    assert result.needs_reply is False
    assert result.draft_reply is None
    assert result.requires_human_review is True


def test_legal_fixture_is_flagged_and_only_acknowledged() -> None:
    result = triage_email_fixture(
        EmailFixture(
            subject="NDA and BAA signature requested",
            body=(
                "Please review the contract, indemnification, HIPAA agreement, terms, "
                "and liability language."
            ),
            sender_name="Morgan",
            sender_email="morgan@example.org",
        )
    )

    assert "legal_review" in result.risk_flags
    assert result.needs_reply is True
    assert result.draft_reply is not None
    assert "review it before responding further" in result.draft_reply
    assert "indemnification" not in result.draft_reply.lower()


def test_finance_fixture_is_flagged() -> None:
    result = triage_email_fixture(
        EmailFixture(
            subject="Invoice and W-9",
            body="Please confirm payment by ACH. The invoice includes bank and tax details.",
            sender_email="billing@example.org",
        )
    )

    assert "finance_review" in result.risk_flags
    assert result.requires_human_review is True
    assert result.draft_reply is None


def test_suspicious_fixture_is_flagged() -> None:
    result = run_gmail_triage_fixture(FIXTURES / "sample_email_suspicious.txt")

    assert "security" in result.risk_flags
    assert result.needs_reply is False
    assert result.draft_reply is None


def test_urgent_financial_credential_request_is_security_flagged() -> None:
    assessment = assess_text_guardrails(
        "Urgent payment needed today. Login to verify account credentials and update "
        "ACH bank details."
    )

    assert "finance_review" in assessment.risk_flags
    assert "security" in assessment.risk_flags
    assert not assessment.allowed


def test_urgent_payment_security_pattern_does_not_cross_unrelated_lines() -> None:
    assessment = assess_text_guardrails(
        "Please reply today about the research call.\n"
        "The onboarding portal includes a login for tomorrow.\n"
        "The invoice is attached for your records."
    )

    assert "finance_review" in assessment.risk_flags
    assert "credential_context" in assessment.risk_flags
    assert "security" not in assessment.risk_flags
    assert assessment.allowed


def test_standalone_credential_context_requires_review_without_blocking() -> None:
    assessment = assess_text_guardrails(
        "Initial onboarding email. Use the temporary password and login link from the portal."
    )

    assert assessment.allowed
    assert "credential_context" in assessment.risk_flags
    assert "security" not in assessment.risk_flags
    assert assessment.manual_review_required


def test_phishing_credential_context_still_blocks() -> None:
    assessment = assess_tool_payload_guardrails(
        "gmail_get_message",
        {
            "subject": "Verify account",
            "body": "Click this link to verify now: https://bit.ly/login-reset",
        },
        output=True,
    )

    assert "credential_context" in assessment.risk_flags
    assert "security" in assessment.risk_flags
    assert not assessment.allowed


def test_phi_guardrail_does_not_cross_unrelated_lines() -> None:
    assessment = assess_text_guardrails(
        "Patient Engagement Platform\n"
        "Depression research jobs\n"
        "Remote medical director opportunity."
    )

    assert "possible_phi" not in assessment.risk_flags
    assert assessment.allowed


def test_unsupported_outreach_claim_is_flagged() -> None:
    assessment = assess_text_guardrails(
        "We have helped voice-AI teams achieve proven results and guaranteed ROI."
    )
    tool_result = check_unsupported_claims(
        "We have worked with companies like yours and delivered proven results.",
        allowed_claims=[],
    )

    assert "unsupported_claim" in assessment.risk_flags
    assert not assessment.allowed
    assert tool_result["has_unsupported_claims"] is True
    assert tool_result["unsupported_claims"]


def test_unsupported_claim_guardrail_allows_negative_instructions() -> None:
    assessment = assess_text_guardrails(
        "Do not invent customers, case studies, or a track record absent from the brief."
    )

    assert "unsupported_claim" not in assessment.risk_flags
    assert assessment.allowed


def test_tool_guardrail_assessment_blocks_secrets_and_send_like_actions() -> None:
    secret_assessment = assess_tool_payload_guardrails(
        "slack_post_message",
        {"text": "api_key=REDACTED_TEST_SECRET"},
    )
    send_assessment = assess_tool_payload_guardrails(
        "send_email",
        {"send_enabled": True, "body": "Send the email now"},
    )

    assert not secret_assessment.allowed
    assert "secret" in secret_assessment.risk_flags
    assert not send_assessment.allowed
    assert "send_like_action" in send_assessment.risk_flags


def test_tool_guardrail_allows_storing_no_send_policy_metadata() -> None:
    assessment = assess_tool_payload_guardrails(
        "storage_save_agent_run",
        {
            "output": {
                "send_enabled": False,
                "can_send_email": False,
                "forbidden_actions": ["send_email"],
                "safety_gates_applied": ["no_send_enforced"],
            }
        },
    )

    assert assessment.allowed
    assert "send_like_action" not in assessment.risk_flags


def test_gmail_tool_guardrails_block_unsafe_drafts() -> None:
    with pytest.raises(ToolGuardrailViolation, match="unsupported outreach claim"):
        create_gmail_draft_reply(
            "fixture-message",
            "We have helped voice-AI teams achieve guaranteed ROI.",
        )

    with pytest.raises(ToolGuardrailViolation, match="possible PHI"):
        GmailTool().create_draft(
            to="person@example.com",
            subject="Draft",
            body="Patient Jane Doe was diagnosed with depression and needs treatment details.",
        )


def test_serper_tool_guardrails_block_unsafe_queries() -> None:
    with pytest.raises(ToolGuardrailViolation, match="secret-like content"):
        search_web("api_key=REDACTED_TEST_SECRET", num_results=1)

    with pytest.raises(ToolGuardrailViolation, match="possible PHI"):
        SerperTool(live=False).search(
            "Patient Jane Doe was diagnosed with anxiety and needs therapy details.",
            num_results=1,
        )


def test_slack_and_approval_tool_guardrails_block_unsafe_copy() -> None:
    with pytest.raises(ToolGuardrailViolation, match="secret-like content"):
        SlackTool(live=False).post_message(None, "token=redactionfixture123")

    with pytest.raises(ToolGuardrailViolation, match="unsupported outreach claim"):
        post_approval_request(
            {
                "message_id": "dry-msg",
                "draft_reply": (
                    "We have worked with companies like yours and delivered proven results."
                ),
            },
            live=False,
        )


def test_storage_tool_guardrails_block_unsafe_writes(tmp_path: Path) -> None:
    storage = StorageTool(f"sqlite:///{tmp_path / 'guardrails.db'}")
    storage.init_db()

    with pytest.raises(ToolGuardrailViolation, match="possible PHI"):
        storage.save(
            {
                "summary": (
                    "Patient Jane Doe was diagnosed with depression and needs "
                    "identifiable treatment details."
                )
            }
        )


def test_safe_consulting_inquiry_still_gets_draft() -> None:
    result = run_gmail_triage_fixture(
        FIXTURES / "sample_email_consulting.txt",
        sender_name="Alex",
        sender_email="alex@example.com",
    )

    assert result.category == "consulting_opportunity"
    assert result.needs_reply is True
    assert result.draft_reply
    assert result.approval_required is True
    assert result.risk_flags == []


def test_build_agent_functions_reference_guardrails() -> None:
    for build_agent in (
        build_gmail_triage_agent,
        build_business_research_analyst_agent,
        build_opportunity_scout_agent,
        build_outreach_composer_agent,
    ):
        agent = build_agent()
        assert agent.input_guardrails
        assert agent.output_guardrails


def test_no_fixture_output_gives_professional_advice() -> None:
    outputs = [
        triage_email_fixture(
            EmailFixture(
                subject="NDA signature requested",
                body="Please review the contract terms and liability language.",
            )
        ).model_dump(),
        triage_email_fixture(
            EmailFixture(
                subject="Patient Jane Doe diagnosis follow-up",
                body="Patient Jane Doe was diagnosed with anxiety and needs treatment details.",
            )
        ).model_dump(),
        run_gmail_triage_fixture(FIXTURES / "sample_email_consulting.txt").model_dump(),
    ]
    text = json.dumps(outputs, ensure_ascii=False).lower()

    forbidden = (
        "medical advice",
        "legal advice",
        "tax advice",
        "regulatory advice",
        "liability opinion",
        "tax strategy",
        "treatment plan",
    )
    assert not any(phrase in text for phrase in forbidden)


def test_no_email_send_path_exists_in_approval_or_slack_tools() -> None:
    for path in (
        Path("src/keystone_agents/tools/approval_tool.py"),
        Path("src/keystone_agents/tools/slack_tool.py"),
        Path("scripts/run_gmail_triage.py"),
        Path("scripts/run_outreach_draft.py"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert not any(name.startswith("send") or "send_email" in name for name in names)
