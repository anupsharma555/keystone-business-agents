from __future__ import annotations

import pytest

from keystone_agents.schemas.approval import ApprovalQueueItem
from keystone_agents.schemas.review_card import ReviewCard, ReviewEvidenceItem, ReviewSourceItem
from keystone_agents.slack_interactions import (
    handle_slack_approval_interaction,
    parse_slack_agent_mention,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.slack_tool import (
    SlackTool,
    format_slack_review_message,
    slack_review_message_from_approval_item,
)


def _review_card() -> ReviewCard:
    return ReviewCard(
        title="Curebase outreach review",
        object_type="outreach_draft",
        object_id="curebase-draft-1",
        decision_summary="Ready for human review.",
        reason="Trial technology fit is supported by source-backed evidence.",
        evidence=[
            ReviewEvidenceItem(
                text="Curebase supports decentralized clinical trial operations.",
                source_id="fixture:curebase",
                confidence="high",
            )
        ],
        risks=["external_copy"],
        approval_status="pending",
        approval_scope="external_use",
        next_action="Review the evidence and approve or request revision outside Slack.",
        sources=[
            ReviewSourceItem(
                source_id="fixture:curebase",
                title="Curebase fixture",
                url="fixture://sample_company_curebase.json",
            )
        ],
        outbound_copy=True,
    )


def test_slack_review_message_has_short_root_and_thread_blocks() -> None:
    message = format_slack_review_message(
        _review_card(),
        approval_item_id="approval-curebase-1",
        draft_text="Subject: Quick note\n\nHi Alex, this is draft copy for review.",
    )
    payload = message.as_payload()
    thread_text = "\n\n".join(payload["thread_blocks"])

    assert "Keystone Business Agents Workflow review" in message.root_text
    assert "approval-curebase-1" in message.root_text
    assert "Type: outreach draft" in message.root_text
    assert "Status: pending" in message.root_text
    assert "Scope: external use" in message.root_text
    assert "Risk flags: external copy" in message.root_text
    assert "Approval question: Yes or No" in message.root_text
    assert "Feedback:" in message.root_text
    assert "Next safe action:" in message.root_text
    assert "Hi Alex" not in message.root_text
    assert "fixture:curebase" not in message.root_text
    assert len(message.thread_blocks) == 2
    assert "Review details" in thread_text
    assert "Evidence" in thread_text
    assert "Sources" in thread_text
    assert "Draft text" in thread_text
    assert "fixture:curebase" not in thread_text
    assert "Hi Alex" in thread_text
    assert payload["object_type"] == "outreach_draft"
    assert payload["scope"] == "external_use"
    assert payload["risk_flags"] == ["external_copy"]
    assert payload["send_enabled"] is False
    assert payload["interactive_actions_enabled"] is True
    assert "\u2014" not in message.root_text
    assert "\u2014" not in thread_text


def test_slack_agent_mention_uses_shared_kni_alias_parser() -> None:
    result = parse_slack_agent_mention("<@U123> business agent analyst research Lindus")

    assert result.route == "business_research_analyst"
    assert result.agent_name == "Business Research Analyst"
    assert result.input_text == "research Lindus"
    assert result.send_enabled is False


def test_slack_review_message_redacts_secrets_and_omits_phi() -> None:
    secret_message = format_slack_review_message(
        _review_card(),
        approval_item_id="approval-secret",
        draft_text="authorization: bearer redaction-fixture-value",
    )
    secret_thread = "\n\n".join(secret_message.thread_blocks)

    assert "redaction-fixture-value" not in secret_thread
    assert "[REDACTED]" in secret_thread

    phi_message = format_slack_review_message(
        _review_card(),
        approval_item_id="approval-phi",
        draft_text="Patient Jane Doe was diagnosed with anxiety and needs therapy details.",
    )
    phi_thread = "\n\n".join(phi_message.thread_blocks)

    assert "Patient Jane Doe" not in phi_thread
    assert "sensitive content flagged" in phi_thread


def test_slack_tool_dry_run_posts_only_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_APPROVALS", raising=False)

    result = SlackTool(live=False).post_message(
        channel=None,
        text="Keystone review | approval-id | outreach_draft",
    )

    assert result["status"] == "dry-run"
    assert result["ts"] == "dry-run-slack-ts"
    assert result["channel"] == "dry-run-approvals"


def test_slack_review_message_posts_thread_preview_in_dry_run() -> None:
    message = format_slack_review_message(
        _review_card(),
        approval_item_id="approval-curebase-1",
        draft_text="Subject: Quick note\n\nHi Alex, this is draft copy for review.",
    )

    result = SlackTool(live=False).post_review_message(message)

    assert result["status"] == "dry-run"
    assert result["thread_blocks_posted"] == len(message.thread_blocks)
    assert result["send_enabled"] is False


def test_slack_review_message_from_approval_item_includes_contact_fields() -> None:
    item = ApprovalQueueItem(
        id="approval-contact-fields",
        object_type="outreach_draft",
        object_id="1",
        title="Email draft: Curebase research workflow discussion",
        summary="Email draft: Curebase research workflow discussion",
        draft_text="Subject: Test\n\nHi Andy.",
        source_agent="pytest",
        metadata={
            "company_name": "Curebase",
            "company_website": "https://www.curebase.com",
            "contact_name": "Andy",
            "contact_title": "Clinical Operations Lead",
            "recipient_email": "andy@example.com",
            "outreach_channel": "email",
            "company_fit_summary": "Curebase has clinical research workflow fit.",
            "company_scores": {"consulting_fit": 75, "clinical_ai": 60},
            "company_source_quality": {
                "overall_score": 82,
                "independent_source_count": 2,
                "high_quality_source_count": 2,
            },
            "company_research_completeness": {"score": 70},
            "company_research_points": [
                {
                    "label": "Business model",
                    "value": "Clinical trial software.",
                    "source_ids": ["fixture:curebase"],
                }
            ],
            "company_claims": [
                {
                    "text": "Curebase supports decentralized clinical trial operations.",
                    "source_id": "fixture:curebase",
                }
            ],
            "company_missing_information": ["Confirm current buyer title."],
            "company_risks": ["Review source freshness before outreach."],
            "sources": [
                {
                    "title": "Curebase",
                    "url": "https://www.curebase.com",
                    "source_id": "fixture:curebase",
                }
            ],
        },
    )

    review = slack_review_message_from_approval_item(item)
    thread_text = "\n\n".join(review.thread_blocks)

    assert "Company: Curebase" in review.root_text
    assert "Company link: https://www.curebase.com" in review.root_text
    assert "Contact: Andy" in review.root_text
    assert "Contact title: Clinical Operations Lead" in review.root_text
    assert "Email: andy@example.com" in review.root_text
    assert len(review.thread_blocks) == 2
    assert "Company research" in thread_text
    assert "CR summary: Curebase has clinical research workflow fit." in thread_text
    assert "Source quality: 82/100" in thread_text
    assert "Research completeness: 70/100" in thread_text
    assert "Source-backed claims:" in thread_text
    assert "Confirm current buyer title." in thread_text
    assert "Review source freshness before outreach." in thread_text
    assert "Company link: https://www.curebase.com" in thread_text
    assert "Draft for approval" in thread_text
    assert "Destination: andy@example.com" in thread_text


def test_slack_review_message_from_approval_item_includes_linkedin_destination() -> None:
    item = ApprovalQueueItem(
        id="approval-linkedin-fields",
        object_type="outreach_draft",
        object_id="1",
        title="LinkedIn draft: Curebase",
        summary="LinkedIn draft: Curebase",
        draft_text="Hi Andy - brief LinkedIn note.",
        source_agent="pytest",
        metadata={
            "company_name": "Curebase",
            "company_website": "https://www.curebase.com",
            "contact_name": "Andy",
            "contact_title": "Clinical Operations Lead",
            "linkedin_url": "https://www.linkedin.com/in/andy-reviewer",
            "outreach_channel": "linkedin",
            "company_fit_summary": "Curebase has clinical research workflow fit.",
        },
    )

    review = slack_review_message_from_approval_item(item)
    thread_text = "\n\n".join(review.thread_blocks)

    assert "LinkedIn: https://www.linkedin.com/in/andy-reviewer" in review.root_text
    assert "Destination: https://www.linkedin.com/in/andy-reviewer" in thread_text


def test_slack_interactive_yes_updates_local_approval(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack.db'}"
    item = ApprovalQueueItem(
        id="approval-slack-yes",
        object_type="outreach_draft",
        object_id="1",
        title="Email draft",
        summary="Email draft",
        draft_text="Subject: Test\n\nHi there.",
        source_agent="pytest",
    )
    SQLiteStore(database_url).save_approval_item(item)

    result = handle_slack_approval_interaction(
        {
            "user": {"username": "anup"},
            "actions": [
                {
                    "action_id": "keystone_approval_yes",
                    "value": "approval-slack-yes",
                }
            ],
        },
        database_url=database_url,
    )

    assert result.approval_status == "approved"
    assert result.reviewer == "anup"


def test_slack_interactive_yes_creates_email_draft_preview(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack.db'}"
    SQLiteStore(database_url).save_approval_item(
        ApprovalQueueItem(
            id="approval-slack-email-yes",
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Email draft",
            draft_text="Subject: Test subject\n\nHi Andy.\n\nSincerely,\nAnup",
            source_agent="pytest",
            metadata={
                "outreach_channel": "email",
                "recipient_email": "andy@example.com",
                "email_subject": "Test subject",
            },
        )
    )

    result = handle_slack_approval_interaction(
        {
            "user": {"username": "anup"},
            "actions": [
                {
                    "action_id": "keystone_approval_yes",
                    "value": "approval-slack-email-yes",
                }
            ],
        },
        database_url=database_url,
    )
    item = SQLiteStore(database_url).get_approval_item("approval-slack-email-yes")

    assert result.approval_status == "approved"
    assert result.outcome == "approved_email_draft"
    assert result.gmail_draft_result is not None
    assert result.gmail_draft_result["status"] == "dry-run"
    assert result.gmail_draft_result["to"] == "andy@example.com"
    assert item is not None
    assert item.metadata["gmail_draft_result"]["status"] == "dry-run"


def test_slack_interactive_feedback_marks_revision(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack.db'}"
    SQLiteStore(database_url).save_approval_item(
        ApprovalQueueItem(
            id="approval-slack-revise",
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Email draft",
            draft_text="Subject: Test\n\nHi there.",
            source_agent="pytest",
        )
    )

    result = handle_slack_approval_interaction(
        {
            "user": {"id": "U123"},
            "actions": [
                {
                    "action_id": "keystone_approval_revise",
                    "value": "approval-slack-revise",
                }
            ],
            "state": {
                "values": {
                    "feedback": {
                        "keystone_approval_feedback_text": {
                            "type": "plain_text_input",
                            "value": "Shorten the CTA before approval.",
                        }
                    }
                }
            },
        },
        database_url=database_url,
    )

    assert result.approval_status == "revise"
    assert result.notes == "Shorten the CTA before approval."
    assert result.outcome == "revision_requested"
    assert "Shorten the CTA before approval." in result.revision_prompt
    item = SQLiteStore(database_url).get_approval_item("approval-slack-revise")
    assert item is not None
    assert "llm_revision_prompt" in item.metadata


def test_slack_interactive_no_requests_rejection_feedback(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack.db'}"
    SQLiteStore(database_url).save_approval_item(
        ApprovalQueueItem(
            id="approval-slack-no",
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Email draft",
            draft_text="Subject: Test\n\nHi there.",
            source_agent="pytest",
            metadata={"company_name": "Curebase"},
        )
    )

    result = handle_slack_approval_interaction(
        {
            "user": {"id": "U123"},
            "actions": [
                {
                    "action_id": "keystone_approval_no",
                    "value": "approval-slack-no",
                }
            ],
        },
        database_url=database_url,
    )
    item = SQLiteStore(database_url).get_approval_item("approval-slack-no")

    assert result.approval_status == "rejected"
    assert result.outcome == "rejected_feedback_requested"
    assert "why this approval was rejected" in result.feedback_prompt
    assert item is not None
    assert item.metadata["rejection_feedback_required"] is True
