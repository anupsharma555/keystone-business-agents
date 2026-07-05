from __future__ import annotations

import json

import pytest

from keystone_agents.schemas.approval import ApprovalQueueItem, ApprovalQueueStatus, ApprovalState
from keystone_agents.schemas.review_card import ReviewCard, ReviewEvidenceItem, ReviewSourceItem
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.slack_action_contract import (
    BUSINESS_AGENT_ACTION_SCHEMA,
    KBA_APPROVE_EXTERNAL_USE,
    KBA_COS_CONTINUE_WORK_ITEM,
    KBA_COS_POST_SUMMARY,
    KBA_CREATE_GMAIL_DRAFT,
    KBA_FIND_CONTACT,
    KBA_INTENT_CONTINUE_WORK_ITEM,
    KBA_INTENT_MORE_RESEARCH,
    KBA_INTENT_OPEN_WORK_ITEM,
    KBA_INTENT_RESEARCH_ALL_CANDIDATES,
    KBA_INTENT_SHOW_SOURCES,
    KBA_INTENT_SKIP_COMPANY,
    KBA_MORE_RESEARCH,
    KBA_OVERFLOW,
    KBA_REVISE_DRAFT,
    KBA_REVISE_DRAFT_VIEW_CALLBACK_ID,
    business_agent_action_value,
    parse_business_agent_action_value,
)
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

    assert "Ready for approval" in message.root_text
    assert "approval-curebase-1" in message.root_text
    assert "Type: outreach draft" in message.root_text
    assert "Status: pending" in message.root_text
    assert "Scope: external use" in message.root_text
    assert "Risk flags: external copy" in message.root_text
    assert "Approval question: approve the action `external use`" in message.root_text
    assert "Button scope: Approve: external use" in message.root_text
    assert "Reject:" not in message.root_text
    assert "Feedback:" not in message.root_text
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


def test_slack_agent_mention_keeps_generic_kni_requests_on_orchestrator() -> None:
    result = parse_slack_agent_mention(
        "<@U123> run one opportunity-to-outreach loop for behavioral health AI. Top 1 only."
    )

    assert result.route == "orchestrator"
    assert result.agent_name == "Keystone Orchestrator Agent"
    assert result.input_text.startswith("run one opportunity-to-outreach loop")
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


def test_chief_post_summary_action_uses_source_channel(tmp_path) -> None:
    payload = {
        "user": {"username": "anup"},
        "channel": {"id": "CANNOUNCE", "name": "announcements"},
        "message": {"ts": "1715366400.000100", "thread_ts": "1715366400.000100"},
        "actions": [
            {
                "action_id": KBA_COS_POST_SUMMARY,
                "value": business_agent_action_value(intent="post_internal_summary"),
            }
        ],
    }

    result = handle_slack_approval_interaction(
        payload,
        database_url=f"sqlite:///{tmp_path / 'slack.db'}",
    )

    publish_result = result.read_only_payload["publish_result"]
    assert result.outcome == "slack_summary_ready"
    assert result.slack_channel_id == "CANNOUNCE"
    assert publish_result["slack_result"]["channel"] == "CANNOUNCE"
    assert publish_result["status"] == "dry-run"


def test_chief_post_summary_action_blocks_when_source_channel_missing(tmp_path) -> None:
    payload = {
        "user": {"username": "anup"},
        "actions": [
            {
                "action_id": KBA_COS_POST_SUMMARY,
                "value": business_agent_action_value(intent="post_internal_summary"),
            }
        ],
    }

    result = handle_slack_approval_interaction(
        payload,
        database_url=f"sqlite:///{tmp_path / 'slack.db'}",
    )

    publish_result = result.read_only_payload["publish_result"]
    assert result.outcome == "channel_context_required"
    assert result.slack_channel_id == ""
    assert publish_result["status"] == "blocked"
    assert publish_result["blocker"] == "missing_slack_source_channel"
    assert publish_result["slack_result"]["channel"] == ""


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
            "contact_path_label": "Organization contact page",
            "contact_path_value": "https://www.curebase.com/contact",
            "opportunity_type": "trial technology",
            "priority_score": 82,
            "why_now_signal": "Hiring and evidence-generation work suggest current fit.",
            "keystone_fit_reason": "Strong clinical research operations overlap.",
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
            "company_risks": ["Review \x1b]11;rgb:1/1/1\x1b\\ source freshness before outreach."],
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
    assert "Contact: Andy" in review.root_text
    assert "Clinical Operations Lead" in review.root_text
    assert "Destination: andy@example.com" in review.root_text
    assert "Fit score: 82/100" in review.root_text
    assert "Missing info: Confirm current buyer title." in review.root_text
    assert "Company link: https://www.curebase.com" not in review.root_text
    assert "Contact path: Organization contact page: https://www.curebase.com/contact" not in (
        review.root_text
    )
    assert len(review.thread_blocks) == 2
    assert "Company research" in thread_text
    assert "CR summary: Curebase has clinical research workflow fit." in thread_text
    assert "Source quality: 82/100" in thread_text
    assert "Research completeness: 70/100" in thread_text
    assert "Top sources:" in thread_text
    assert "Source-backed claims:" not in thread_text
    assert "CR facts:" not in thread_text
    assert "Confirm current buyer title." in thread_text
    assert "\x1b" not in thread_text
    assert "Review source freshness before outreach." in thread_text
    assert "Company link: https://www.curebase.com" in thread_text
    assert "Draft for approval" in thread_text
    assert "Destination: andy@example.com" in thread_text
    company_block = review.thread_blocks[0]
    draft_block = review.thread_blocks[1]
    assert company_block.index("CR summary:") < company_block.index("Metadata:")
    assert company_block.index("Metadata:") < company_block.index("Company link:")
    assert draft_block.index("Draft text:") < draft_block.index("Metadata:")
    assert draft_block.index("Metadata:") < draft_block.index("Destination:")


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

    assert "Destination: https://www.linkedin.com/in/andy-reviewer" in review.root_text
    assert "Destination: https://www.linkedin.com/in/andy-reviewer" in thread_text


def test_live_slack_review_message_includes_scoped_legacy_actions_without_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeSlackResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"ok": True, "ts": "123.456"}

    def fake_post(url: str, **kwargs: object) -> FakeSlackResponse:
        calls.append({"url": url, **kwargs})
        return FakeSlackResponse()

    monkeypatch.setattr("requests.post", fake_post)
    message = format_slack_review_message(
        _review_card(),
        approval_item_id="approval-curebase-1",
        draft_text="Subject: Test\n\nHi Andy.",
    )

    result = SlackTool(
        live=True,
        bot_token="fake-slack-unit-test-token",
        approvals_channel="C123",
    ).post_review_message(message)

    assert result["status"] == "posted"
    root_blocks = calls[0]["json"]["blocks"]  # type: ignore[index]
    assert root_blocks[1]["type"] == "actions"
    action_ids = [element["action_id"] for element in root_blocks[1]["elements"]]
    assert action_ids == [
        "keystone_approval_yes",
        "keystone_approval_revise",
    ]
    action_texts = [element["text"]["text"] for element in root_blocks[1]["elements"]]
    assert action_texts == [
        "Approve: external use",
        "Request edits: external use",
    ]


def test_slack_review_message_for_gmail_draft_save_names_button_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeSlackResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"ok": True, "ts": "123.456"}

    def fake_post(url: str, **kwargs: object) -> FakeSlackResponse:
        calls.append({"url": url, **kwargs})
        return FakeSlackResponse()

    monkeypatch.setattr("requests.post", fake_post)
    message = slack_review_message_from_approval_item(
        ApprovalQueueItem(
            id="approval-gmail-draft-save",
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
                "slack_approval_allows_gmail_draft_creation": True,
                "gmail_draft_account": "operator@example.com",
            },
        )
    )

    result = SlackTool(
        live=True,
        bot_token="fake-slack-unit-test-token",
        approvals_channel="C123",
    ).post_review_message(message)
    root_blocks = calls[0]["json"]["blocks"]  # type: ignore[index]
    action_block = next(block for block in root_blocks if block["type"] == "actions")
    button_elements = [
        element for element in action_block["elements"] if element["type"] == "button"
    ]
    action_texts = [element["text"]["text"] for element in button_elements]
    action_ids = [element["action_id"] for element in button_elements]

    assert result["status"] == "posted"
    assert "Create Gmail draft in operator@example.com" in message.root_text
    assert action_texts == [
        "Create Gmail draft",
        "Revise draft",
        "Retry source pass",
        "Find contact",
    ]
    assert action_ids == [
        KBA_CREATE_GMAIL_DRAFT,
        KBA_REVISE_DRAFT,
        KBA_MORE_RESEARCH,
        KBA_FIND_CONTACT,
    ]
    assert action_block["elements"][-1]["type"] == "overflow"
    assert action_block["elements"][-1]["action_id"] == KBA_OVERFLOW


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
    assert result.object_type == "outreach_draft"


def test_slack_interactive_yes_does_not_create_email_draft_from_button(tmp_path) -> None:
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
        create_email_draft=True,
    )
    item = SQLiteStore(database_url).get_approval_item("approval-slack-email-yes")

    assert result.approval_status == "approved"
    assert result.outcome == "approved_email_draft"
    assert result.gmail_draft_result is None
    assert item is not None
    assert item.metadata["gmail_draft_result"]["status"] == "skipped"
    assert "explicit draft command" in item.metadata["gmail_draft_result"]["reason"]


def test_slack_interactive_yes_creates_gmail_draft_when_explicitly_allowed(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack.db'}"
    SQLiteStore(database_url).save_approval_item(
        ApprovalQueueItem(
            id="approval-slack-email-draft-save",
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
                "slack_approval_allows_gmail_draft_creation": True,
                "gmail_draft_account": "operator@example.com",
            },
        )
    )

    result = handle_slack_approval_interaction(
        {
            "user": {"username": "anup"},
            "actions": [
                {
                    "action_id": "keystone_approval_yes",
                    "value": "approval-slack-email-draft-save",
                }
            ],
        },
        database_url=database_url,
        create_email_draft=True,
        live_gmail=False,
    )
    item = SQLiteStore(database_url).get_approval_item("approval-slack-email-draft-save")

    assert result.approval_status == "approved"
    assert result.gmail_draft_result is not None
    assert result.gmail_draft_result["status"] == "dry-run"
    assert result.gmail_draft_result["to"] == "andy@example.com"
    assert result.gmail_draft_result["gmail_account"] == "operator@example.com"
    assert "Gmail draft creation in `operator@example.com`" in result.followup_text
    assert item is not None
    assert item.metadata["gmail_draft_result"]["gmail_account"] == "operator@example.com"


def test_kba_create_gmail_draft_creates_dry_run_when_explicitly_allowed(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack.db'}"
    SQLiteStore(database_url).save_approval_item(
        ApprovalQueueItem(
            id="approval-kba-email-draft-save",
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
                "slack_approval_allows_gmail_draft_creation": True,
                "gmail_draft_account": "operator@example.com",
            },
        )
    )

    result = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_CREATE_GMAIL_DRAFT,
            intent="create_gmail_draft",
            approval_id="approval-kba-email-draft-save",
            gate_scope="external_use",
        ),
        database_url=database_url,
        live_gmail=False,
    )

    assert result.approval_status == "approved"
    assert result.gmail_draft_result is not None
    assert result.gmail_draft_result["status"] == "dry-run"
    assert result.gmail_draft_result["gmail_account"] == "operator@example.com"
    assert result.email_sent is False


def test_slack_interactive_duplicate_gmail_draft_approval_is_idempotent(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack.db'}"
    SQLiteStore(database_url).save_approval_item(
        ApprovalQueueItem(
            id="approval-slack-email-draft-duplicate",
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
                "slack_approval_allows_gmail_draft_creation": True,
                "gmail_draft_account": "operator@example.com",
            },
        )
    )
    payload = {
        "user": {"username": "anup"},
        "actions": [
            {
                "action_id": "keystone_approval_yes",
                "value": "approval-slack-email-draft-duplicate",
            }
        ],
    }

    first = handle_slack_approval_interaction(
        payload,
        database_url=database_url,
        create_email_draft=True,
        live_gmail=False,
    )
    duplicate = handle_slack_approval_interaction(
        payload,
        database_url=database_url,
        create_email_draft=True,
        live_gmail=False,
    )
    item = SQLiteStore(database_url).get_approval_item("approval-slack-email-draft-duplicate")

    assert first.gmail_draft_result is not None
    assert first.gmail_draft_result["status"] == "dry-run"
    assert duplicate.idempotent is True
    assert duplicate.gmail_draft_result is not None
    assert duplicate.gmail_draft_result["status"] == "duplicate_ignored"
    assert "No additional Gmail draft" in duplicate.followup_text
    assert item is not None
    assert item.metadata["gmail_draft_result"]["status"] == "dry-run"
    assert item.metadata["gmail_draft_duplicate_ignored"] is True


def test_slack_interactive_yes_approval_does_not_depend_on_recipient_for_gate_only(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack.db'}"
    SQLiteStore(database_url).save_approval_item(
        ApprovalQueueItem(
            id="approval-slack-email-missing-recipient",
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Email draft",
            draft_text="Subject: Test subject\n\nHi there.\n\nSincerely,\nAnup",
            source_agent="pytest",
            metadata={
                "outreach_channel": "email",
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
                    "value": "approval-slack-email-missing-recipient",
                }
            ],
        },
        database_url=database_url,
    )
    item = SQLiteStore(database_url).get_approval_item("approval-slack-email-missing-recipient")

    assert result.approval_status == "approved"
    assert result.gmail_draft_result is None
    assert "recorded scope" in result.followup_text
    assert item is not None
    assert item.approval_status == ApprovalQueueStatus.APPROVED


def _save_linked_work_item_approval(
    store: SQLiteStore,
    *,
    approval_id: str = "approval-workitem",
    second_approval_id: str = "approval-other-gate",
) -> WorkItem:
    item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        status=WorkItemStatus.NEEDS_APPROVAL,
        title="Review outreach draft",
        approval_gates=[
            WorkItemApprovalGate(
                scope="external_use",
                state=ApprovalState.PENDING.value,
                approval_id=approval_id,
                rationale="Draft requires external-use approval.",
            ),
            WorkItemApprovalGate(
                scope="drafting",
                state=ApprovalState.PENDING.value,
                approval_id=second_approval_id,
                rationale="Separate drafting approval must not be affected.",
            ),
        ],
    )
    store.save_work_item(item)
    store.save_approval_item(
        ApprovalQueueItem(
            id=approval_id,
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Email draft",
            draft_text="Subject: Test\n\nHi there.",
            source_agent="pytest",
            metadata={
                "work_item_id": item.id,
                "approval_scope": "external_use",
            },
        )
    )
    return item


def _kba_payload(
    *,
    action_id: str,
    intent: str,
    approval_id: str = "approval-workitem",
    work_item_id: str = "",
    artifact_id: str = "",
    gate_scope: str = "external_use",
    channel_id: str = "C123",
    message_ts: str = "1710000000.000100",
) -> dict[str, object]:
    value = business_agent_action_value(
        intent=intent,
        work_item_id=work_item_id,
        approval_id=approval_id,
        gate_scope=gate_scope,
        artifact_id=artifact_id,
    )
    action: dict[str, object] = {"action_id": action_id}
    if action_id == KBA_OVERFLOW:
        action["selected_option"] = {
            "text": {"type": "plain_text", "text": intent},
            "value": value,
        }
    else:
        action["value"] = value
    return {
        "user": {"id": "U123", "username": "anup"},
        "channel": {"id": channel_id, "name": "approvals"},
        "container": {"message_ts": message_ts, "thread_ts": message_ts},
        "actions": [action],
    }


def test_business_agent_card_buttons_use_first_class_payload_schema() -> None:
    message = slack_review_message_from_approval_item(
        ApprovalQueueItem(
            id="approval-card-contract",
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Approve only this draft gate.",
            draft_text="Subject: Test\n\nHi Andy.",
            source_agent="pytest",
            metadata={
                "work_item_id": "wi_card_contract",
                "approval_scope": "external_use",
                "outreach_channel": "email",
                "recipient_email": "andy@example.com",
                "company_name": "Curebase",
            },
        )
    )
    action_block = next(block for block in message.root_blocks if block["type"] == "actions")
    action_ids = [element["action_id"] for element in action_block["elements"]]
    primary = action_block["elements"][0]
    overflow = action_block["elements"][-1]
    parsed = json.loads(primary["value"])

    assert "keystone_approval_no" not in action_ids
    assert "keystone_approval_feedback" not in json.dumps(message.root_blocks)
    assert primary["action_id"] == KBA_APPROVE_EXTERNAL_USE
    assert parsed["schema"] == BUSINESS_AGENT_ACTION_SCHEMA
    assert parsed["approval_id"] == "approval-card-contract"
    assert parsed["work_item_id"] == "wi_card_contract"
    assert parsed["gate_scope"] == "external_use"
    assert all(
        len(element.get("text", {}).get("text", "")) <= 75
        for element in action_block["elements"]
        if element["type"] == "button"
    )
    assert overflow["type"] == "overflow"
    assert all(len(option["value"]) <= 150 for option in overflow["options"])
    assert {json.loads(option["value"])["intent"] for option in overflow["options"]} == {
        KBA_INTENT_SHOW_SOURCES,
        KBA_INTENT_OPEN_WORK_ITEM,
        "run_again",
        KBA_INTENT_SKIP_COMPANY,
    }
    assert all(
        json.loads(option["value"])["approval_id"] == "approval-card-contract"
        for option in overflow["options"]
    )


def test_slack_interactive_yes_updates_matching_work_item_gate_only(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)

    result = handle_slack_approval_interaction(
        {
            "user": {"id": "U123", "username": "anup"},
            "channel": {"id": "C123", "name": "approvals"},
            "container": {"message_ts": "1710000000.000100"},
            "actions": [
                {
                    "action_id": "keystone_approval_yes",
                    "value": "approval-workitem",
                }
            ],
        },
        database_url=database_url,
    )

    updated = store.get_work_item(item.id)
    assert updated is not None
    assert result.work_item_id == item.id
    assert result.work_item_gate_state == "approved_for_external_use"
    assert result.work_item_gate_changed is True
    states = {gate.approval_id: gate.state for gate in updated.approval_gates}
    assert states["approval-workitem"] == "approved_for_external_use"
    assert states["approval-other-gate"] == "pending"
    assert updated.status == WorkItemStatus.NEEDS_APPROVAL
    events = store.list_work_item_events(item.id)
    assert len(events) == 1
    assert events[0].actor == "anup"
    assert events[0].metadata["approval_id"] == "approval-workitem"
    assert events[0].metadata["slack"]["channel_id"] == "C123"
    assert events[0].metadata["slack"]["message_ts"] == "1710000000.000100"


def test_slack_interactive_duplicate_approval_is_idempotent(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    payload = {
        "user": {"username": "anup"},
        "channel": {"id": "C123"},
        "container": {"message_ts": "1710000000.000100"},
        "actions": [{"action_id": "keystone_approval_yes", "value": "approval-workitem"}],
    }

    first = handle_slack_approval_interaction(payload, database_url=database_url)
    duplicate = handle_slack_approval_interaction(payload, database_url=database_url)

    assert first.idempotent is False
    assert duplicate.idempotent is True
    assert duplicate.work_item_gate_changed is False
    assert store.get_work_item(item.id) is not None
    assert len(store.list_work_item_events(item.id)) == 1


def test_kba_approve_external_use_updates_matching_work_item_gate_only(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)

    result = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_APPROVE_EXTERNAL_USE,
            intent="approve_external_use",
            approval_id="approval-workitem",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )

    updated = store.get_work_item(item.id)
    assert updated is not None
    assert result.action_id == KBA_APPROVE_EXTERNAL_USE
    assert result.approval_status == "approved"
    assert result.work_item_gate_state == "approved_for_external_use"
    states = {gate.approval_id: gate.state for gate in updated.approval_gates}
    assert states["approval-workitem"] == "approved_for_external_use"
    assert states["approval-other-gate"] == "pending"


def test_kba_duplicate_approval_is_idempotent(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    payload = _kba_payload(
        action_id=KBA_APPROVE_EXTERNAL_USE,
        intent="approve_external_use",
        approval_id="approval-workitem",
        work_item_id=item.id,
    )

    first = handle_slack_approval_interaction(payload, database_url=database_url)
    duplicate = handle_slack_approval_interaction(payload, database_url=database_url)

    assert first.idempotent is False
    assert duplicate.idempotent is True
    assert duplicate.work_item_gate_changed is False
    assert len(store.list_work_item_events(item.id)) == 1


def test_kba_revise_draft_modal_submission_records_feedback_and_queues_agent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    calls: list[object] = []

    def fake_advance(request, **_kwargs):
        calls.append(request)
        loaded = SQLiteStore(database_url).get_work_item(item.id)
        assert loaded is not None
        return WorkflowRunResult(
            work_item=loaded,
            route=WorkItemRoute.OUTREACH_COMPOSER,
            status=loaded.status,
            advanced=False,
        )

    monkeypatch.setattr(
        "keystone_agents.slack_interactions.advance_work_item_manager_loop_with_optional_langgraph",
        fake_advance,
    )
    click = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_REVISE_DRAFT,
            intent="revise_draft",
            approval_id="approval-workitem",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )

    assert click.stage == "modal"
    assert click.modal_view is not None
    assert click.modal_view["callback_id"] == KBA_REVISE_DRAFT_VIEW_CALLBACK_ID

    submitted = handle_slack_approval_interaction(
        {
            "type": "view_submission",
            "user": {"username": "reviewer"},
            "view": {
                "callback_id": KBA_REVISE_DRAFT_VIEW_CALLBACK_ID,
                "private_metadata": click.modal_view["private_metadata"],
                "state": {
                    "values": {
                        "feedback": {
                            "kba_revision_feedback": {
                                "type": "plain_text_input",
                                "value": "Use a softer CTA.",
                            }
                        }
                    }
                },
            },
        },
        database_url=database_url,
    )
    updated = store.get_work_item(item.id)
    queue_item = store.get_approval_item("approval-workitem")

    assert submitted.approval_status == "revise"
    assert submitted.queued_route == WorkItemRoute.OUTREACH_COMPOSER.value
    assert submitted.slack_view_response_payload is not None
    assert len(calls) == 1
    request = calls[0]
    assert request.manual_request_plan["source"] == "slack_business_agent_action"
    assert request.manual_request_plan["intent"] == "revise_draft"
    assert request.orchestrator_preflight["request_text"].startswith("Revise the outreach draft")
    assert request.orchestrator_preflight["manual_request_plan"]["requested_agent"] == (
        WorkItemRoute.OUTREACH_COMPOSER.value
    )
    assert updated is not None
    assert updated.status == WorkItemStatus.BLOCKED
    assert queue_item is not None
    assert queue_item.metadata["operator_feedback_for_agent"] == "Use a softer CTA."


def test_kba_revise_draft_modal_metadata_stays_valid_with_oversized_action_payload(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    value = business_agent_action_value(
        intent="revise_draft",
        approval_id="approval-workitem",
        work_item_id=item.id,
        gate_scope="external_use",
        metadata={
            "approval_title": "Email draft",
            "source_context": "x" * 6000,
        },
    )

    click = handle_slack_approval_interaction(
        {
            "user": {"id": "U123", "username": "anup"},
            "channel": {"id": "C123", "name": "approvals"},
            "container": {"message_ts": "1710000000.000100"},
            "actions": [{"action_id": KBA_REVISE_DRAFT, "value": value}],
        },
        database_url=database_url,
    )
    private_metadata = click.modal_view["private_metadata"]
    parsed = parse_business_agent_action_value(private_metadata)

    assert click.stage == "modal"
    assert len(private_metadata) < 2900
    assert parsed.intent == "revise_draft"
    assert parsed.approval_id == "approval-workitem"
    assert parsed.work_item_id == item.id
    assert parsed.metadata == {"approval_title": "Email draft"}
    assert "source_context" not in private_metadata


def test_kba_more_research_records_event_and_duplicate_is_idempotent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    item = item.model_copy(
        update={"target": WorkItemTarget(name="OpenEvidence", object_type="company")}
    )
    store.save_work_item(item)
    calls: list[object] = []

    def fake_advance(request, **_kwargs):
        calls.append(request)
        loaded = SQLiteStore(database_url).get_work_item(item.id)
        assert loaded is not None
        return WorkflowRunResult(
            work_item=loaded,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=loaded.status,
            advanced=False,
        )

    monkeypatch.setattr(
        "keystone_agents.slack_interactions.advance_work_item_manager_loop_with_optional_langgraph",
        fake_advance,
    )
    payload = _kba_payload(
        action_id=KBA_MORE_RESEARCH,
        intent=KBA_INTENT_MORE_RESEARCH,
        approval_id="approval-workitem",
        work_item_id=item.id,
    )

    first = handle_slack_approval_interaction(payload, database_url=database_url)
    duplicate = handle_slack_approval_interaction(payload, database_url=database_url)

    assert first.outcome == "research_retry_completed"
    assert first.queued_route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert calls
    assert "independent current-year source pass for OpenEvidence" in calls[0].request_text
    assert "funding, partnerships, product updates, hiring, roadmap signals" in (
        calls[0].request_text
    )
    assert calls[0].cost_profile == "slack_research_deep"
    assert calls[0].allow_manager_loop_repair is True
    assert calls[0].hosted_web_search_max_calls == 2
    assert calls[0].slack_query_prompt["kind"] == "deeper_research"
    assert calls[0].external_context["slack_query_prompt"]["task_brief"]
    assert duplicate.idempotent is True
    assert len(calls) == 1
    events = store.list_work_item_events(item.id)
    assert any(event.event_type == "slack_action_intent" for event in events)
    action_events = [
        event for event in events if event.event_type == "slack_action_intent"
    ]
    assert [event.metadata["status"] for event in action_events] == [
        "started",
        "completed",
    ]


def test_kba_more_research_retry_after_started_event_is_not_deduped(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    calls: list[object] = []

    def fake_advance(request, **_kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("simulated crash after action intent")
        loaded = SQLiteStore(database_url).get_work_item(item.id)
        assert loaded is not None
        return WorkflowRunResult(
            work_item=loaded,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=loaded.status,
            advanced=False,
        )

    monkeypatch.setattr(
        "keystone_agents.slack_interactions.advance_work_item_manager_loop_with_optional_langgraph",
        fake_advance,
    )
    payload = _kba_payload(
        action_id=KBA_MORE_RESEARCH,
        intent=KBA_INTENT_MORE_RESEARCH,
        approval_id="approval-workitem",
        work_item_id=item.id,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        handle_slack_approval_interaction(payload, database_url=database_url)
    retry = handle_slack_approval_interaction(payload, database_url=database_url)
    duplicate = handle_slack_approval_interaction(payload, database_url=database_url)

    assert retry.outcome == "research_retry_completed"
    assert duplicate.idempotent is True
    assert len(calls) == 2
    events = [
        event
        for event in store.list_work_item_events(item.id)
        if event.event_type == "slack_action_intent"
    ]
    assert [event.metadata["status"] for event in events] == [
        "started",
        "started",
        "completed",
    ]


def test_kba_more_research_reports_still_blocked_after_retry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)

    def fake_advance(request, **_kwargs):
        loaded = SQLiteStore(database_url).get_work_item(item.id)
        assert loaded is not None
        return WorkflowRunResult(
            work_item=loaded,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.BLOCKED,
            advanced=True,
            blockers=[
                WorkItemBlocker(
                    code="manager_loop_review_failed",
                    message="Independent sources are still insufficient.",
                )
            ],
        )

    monkeypatch.setattr(
        "keystone_agents.slack_interactions.advance_work_item_manager_loop_with_optional_langgraph",
        fake_advance,
    )

    result = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_MORE_RESEARCH,
            intent=KBA_INTENT_MORE_RESEARCH,
            approval_id="approval-workitem",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )

    assert result.outcome == "research_retry_still_blocked"
    assert "still blocked" in result.followup_text
    assert "manager_loop_review_failed" in result.followup_text


def test_kba_more_research_selects_current_candidate_from_button_payload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    item = item.model_copy(
        update={
            "current_route": WorkItemRoute.OPPORTUNITY_SCOUT,
            "target": WorkItemTarget(name="behavioral health AI", object_type="topic"),
            "artifact_refs": [
                WorkItemArtifactRef(
                    artifact_type="opportunity",
                    artifact_id="38",
                    source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                    approval_state="pending",
                    title="Theris",
                    summary="AI-augmented behavioral health provider.",
                ),
                WorkItemArtifactRef(
                    artifact_type="opportunity",
                    artifact_id="39",
                    source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                    approval_state="pending",
                    title="ARPA-H",
                    summary="Behavioral health program source.",
                ),
            ],
        }
    )
    store.save_work_item(item)
    selected_titles: list[str] = []

    def fake_advance(request, **_kwargs):
        loaded = SQLiteStore(database_url).get_work_item(item.id)
        assert loaded is not None
        selected_titles.extend(ref.title for ref in loaded.artifact_refs if ref.selected)
        return WorkflowRunResult(
            work_item=loaded,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=loaded.status,
            advanced=False,
        )

    monkeypatch.setattr(
        "keystone_agents.slack_interactions.advance_work_item_manager_loop_with_optional_langgraph",
        fake_advance,
    )

    result = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_MORE_RESEARCH,
            intent=KBA_INTENT_MORE_RESEARCH,
            approval_id="approval-workitem",
            work_item_id=item.id,
            artifact_id="opportunity:38",
        ),
        database_url=database_url,
    )

    assert result.outcome in {"research_retry_completed", "research_retry_still_blocked"}
    assert selected_titles == ["Theris"]
    events = store.list_work_item_events(item.id)
    assert any(
        event.event_type == "artifact_selected" and event.metadata["artifact_id"] == "38"
        for event in events
    )


def test_kba_more_research_dedupes_per_candidate_artifact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    item = item.model_copy(
        update={
            "current_route": WorkItemRoute.OPPORTUNITY_SCOUT,
            "target": WorkItemTarget(name="behavioral health AI", object_type="topic"),
            "artifact_refs": [
                WorkItemArtifactRef(
                    artifact_type="opportunity",
                    artifact_id="38",
                    source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                    approval_state="pending",
                    title="Theris",
                ),
                WorkItemArtifactRef(
                    artifact_type="opportunity",
                    artifact_id="39",
                    source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                    approval_state="pending",
                    title="ARPA-H",
                ),
            ],
        }
    )
    store.save_work_item(item)
    selected_titles: list[str] = []

    def fake_advance(request, **_kwargs):
        loaded = SQLiteStore(database_url).get_work_item(item.id)
        assert loaded is not None
        selected_titles.extend(ref.title for ref in loaded.artifact_refs if ref.selected)
        return WorkflowRunResult(
            work_item=loaded,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=loaded.status,
            advanced=False,
        )

    monkeypatch.setattr(
        "keystone_agents.slack_interactions.advance_work_item_manager_loop_with_optional_langgraph",
        fake_advance,
    )

    for artifact_id in ("opportunity:38", "opportunity:39"):
        result = handle_slack_approval_interaction(
            _kba_payload(
                action_id=KBA_MORE_RESEARCH,
                intent=KBA_INTENT_MORE_RESEARCH,
                approval_id="approval-workitem",
                work_item_id=item.id,
                artifact_id=artifact_id,
            ),
            database_url=database_url,
        )
        assert result.outcome == "research_retry_completed"

    assert selected_titles == ["Theris", "ARPA-H"]


def test_kba_research_all_candidates_runs_each_attached_opportunity(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    item = item.model_copy(
        update={
            "current_route": WorkItemRoute.OPPORTUNITY_SCOUT,
            "target": WorkItemTarget(name="behavioral health AI", object_type="topic"),
            "artifact_refs": [
                WorkItemArtifactRef(
                    artifact_type="opportunity",
                    artifact_id="38",
                    source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                    approval_state="pending",
                    title="Theris",
                ),
                WorkItemArtifactRef(
                    artifact_type="opportunity",
                    artifact_id="39",
                    source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                    approval_state="pending",
                    title="ARPA-H",
                ),
            ],
        }
    )
    store.save_work_item(item)
    selected_titles: list[str] = []

    def fake_advance(request, **_kwargs):
        loaded = SQLiteStore(database_url).get_work_item(item.id)
        assert loaded is not None
        selected_titles.extend(ref.title for ref in loaded.artifact_refs if ref.selected)
        return WorkflowRunResult(
            work_item=loaded,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=loaded.status,
            advanced=True,
            human_summary="Research ran.",
        )

    monkeypatch.setattr(
        "keystone_agents.slack_interactions.advance_work_item_manager_loop_with_optional_langgraph",
        fake_advance,
    )

    result = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_OVERFLOW,
            intent=KBA_INTENT_RESEARCH_ALL_CANDIDATES,
            approval_id="approval-workitem",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )

    assert result.outcome == "research_all_queued"
    assert result.queued_route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert selected_titles == ["Theris", "ARPA-H"]
    assert result.agent_activity_result is not None
    assert result.agent_activity_result["researched_candidates"] == ["Theris", "ARPA-H"]


def test_kba_more_research_uses_langgraph_when_enabled(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LANGGRAPH", "true")
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)

    result = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_MORE_RESEARCH,
            intent=KBA_INTENT_MORE_RESEARCH,
            approval_id="approval-workitem",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )

    events = store.list_work_item_events(item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    review_event = next(
        event for event in events if event.event_type == "orchestrator_action_review"
    )

    assert result.outcome in {"research_retry_completed", "research_retry_still_blocked"}
    assert result.send_enabled is False
    assert review_event.metadata["intent"] == KBA_INTENT_MORE_RESEARCH
    assert review_event.metadata["route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert graph_event.metadata["checkpoint_key"] == f"work-item:{item.id}"
    assert graph_event.metadata["runtime"] in {"langgraph", "dependency_free_fallback"}
    assert graph_event.metadata["node_path"][:3] == [
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
    ]
    assert "prepare_work_item" in graph_event.metadata["node_path"]
    assert "run_business_research" in graph_event.metadata["node_path"]
    assert "finalize_step" in graph_event.metadata["node_path"]
    if graph_event.metadata.get("checkpoint_required"):
        assert "approval_checkpoint" in graph_event.metadata["node_path"]
        assert graph_event.metadata.get("checkpoint_reason")
    else:
        assert "approval_checkpoint" not in graph_event.metadata["node_path"]


def test_kba_continue_work_item_uses_langgraph_thread_when_enabled(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LANGGRAPH", "true")
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        status=WorkItemStatus.IN_PROGRESS,
        title="Research NeuroFlow",
        request_text="research NeuroFlow",
        target=WorkItemTarget(name="NeuroFlow", object_type="company"),
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    store.save_work_item(item)

    result = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_COS_CONTINUE_WORK_ITEM,
            intent=KBA_INTENT_CONTINUE_WORK_ITEM,
            approval_id="",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )

    events = store.list_work_item_events(item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    advance_event = next(event for event in events if event.event_type == "advance_started")
    review_event = next(
        event for event in events if event.event_type == "orchestrator_action_review"
    )

    assert result.outcome == "continued"
    assert result.work_item_id == item.id
    assert result.send_enabled is False
    assert review_event.metadata["intent"] == KBA_INTENT_CONTINUE_WORK_ITEM
    assert review_event.metadata["route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert advance_event.metadata["orchestrator_preflight"]["request_text"] == "continue"
    assert advance_event.metadata["orchestrator_preflight"]["advisory_only"] is True
    assert (
        advance_event.metadata["orchestrator_preflight"]["manual_request_plan"]["requested_agent"]
        == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    )
    assert graph_event.metadata["checkpoint_key"] == f"work-item:{item.id}"
    assert graph_event.metadata["runtime"] in {"langgraph", "dependency_free_fallback"}


def test_kba_continue_work_item_preserves_live_execution_flags(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LIVE_SEARCH", "true")
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LIVE_SDK", "true")
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        status=WorkItemStatus.IN_PROGRESS,
        title="Research NeuroFlow",
        request_text="research NeuroFlow",
        target=WorkItemTarget(name="NeuroFlow", object_type="company"),
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    store.save_work_item(item)
    captured: list[object] = []

    def fake_advance(request, **_kwargs):
        captured.append(request)
        loaded = SQLiteStore(database_url).get_work_item(item.id)
        assert loaded is not None
        return WorkflowRunResult(
            work_item=loaded,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=loaded.status,
            advanced=False,
        )

    monkeypatch.setattr(
        "keystone_agents.slack_interactions.advance_work_item_manager_loop_with_optional_langgraph",
        fake_advance,
    )

    result = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_COS_CONTINUE_WORK_ITEM,
            intent=KBA_INTENT_CONTINUE_WORK_ITEM,
            approval_id="",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )

    assert result.outcome == "continued"
    assert result.send_enabled is False
    assert captured
    request = captured[0]
    assert request.live_search is True
    assert request.live_sdk is True


def test_kba_overflow_show_sources_and_open_work_item_are_read_only(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)
    queue_item = store.get_approval_item("approval-workitem")
    assert queue_item is not None
    store.save_approval_item(
        queue_item.model_copy(
            update={
                "metadata": {
                    **queue_item.metadata,
                    "sources": [{"title": "Curebase", "url": "https://www.curebase.com"}],
                }
            }
        )
    )

    sources = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_OVERFLOW,
            intent=KBA_INTENT_SHOW_SOURCES,
            approval_id="approval-workitem",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )
    opened = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_OVERFLOW,
            intent=KBA_INTENT_OPEN_WORK_ITEM,
            approval_id="approval-workitem",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )

    assert sources.outcome == "sources_ready"
    assert sources.read_only_payload is not None
    assert opened.outcome == "work_item_ready"
    assert opened.read_only_payload is not None
    assert len(store.list_work_item_events(item.id)) == 0


def test_kba_overflow_skip_company_blocks_without_approving_gate(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)

    result = handle_slack_approval_interaction(
        _kba_payload(
            action_id=KBA_OVERFLOW,
            intent=KBA_INTENT_SKIP_COMPANY,
            approval_id="approval-workitem",
            work_item_id=item.id,
        ),
        database_url=database_url,
    )

    updated = store.get_work_item(item.id)
    assert result.outcome == "skipped"
    assert updated is not None
    assert updated.status == WorkItemStatus.BLOCKED
    assert updated.approval_gates[0].state == "pending"
    assert any(blocker.code == "company_skipped_from_slack" for blocker in updated.blockers)


def test_slack_interactive_reject_leaves_work_item_blocked(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)

    result = handle_slack_approval_interaction(
        {
            "user": {"username": "reviewer"},
            "actions": [{"action_id": "keystone_approval_no", "value": "approval-workitem"}],
        },
        database_url=database_url,
    )

    updated = store.get_work_item(item.id)
    assert updated is not None
    assert result.work_item_gate_state == "rejected"
    assert updated.status == WorkItemStatus.BLOCKED
    assert updated.next_action is not None
    assert updated.next_action.action == "resolve_rejected_approval"
    assert any(not blocker.resolved for blocker in updated.blockers)


def test_slack_interactive_revise_leaves_work_item_blocked_with_feedback(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = _save_linked_work_item_approval(store)

    result = handle_slack_approval_interaction(
        {
            "user": {"username": "reviewer"},
            "actions": [{"action_id": "keystone_approval_revise", "value": "approval-workitem"}],
            "state": {
                "values": {
                    "feedback": {
                        "keystone_approval_feedback_text": {
                            "type": "plain_text_input",
                            "value": "Use a softer CTA.",
                        }
                    }
                }
            },
        },
        database_url=database_url,
    )

    updated = store.get_work_item(item.id)
    assert updated is not None
    assert result.work_item_gate_state == "revision_requested"
    assert "Use a softer CTA." in result.revision_prompt
    assert updated.status == WorkItemStatus.BLOCKED
    assert updated.next_action is not None
    assert updated.next_action.action == "revise_approval_artifact"


def test_slack_interactive_missing_work_item_does_not_update_queue(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    store.save_approval_item(
        ApprovalQueueItem(
            id="approval-missing-workitem",
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Email draft",
            source_agent="pytest",
            metadata={"work_item_id": "wi_missing"},
        )
    )

    with pytest.raises(KeyError, match="WorkItem not found"):
        handle_slack_approval_interaction(
            {
                "user": {"username": "anup"},
                "actions": [
                    {
                        "action_id": "keystone_approval_yes",
                        "value": "approval-missing-workitem",
                    }
                ],
            },
            database_url=database_url,
        )

    item = store.get_approval_item("approval-missing-workitem")
    assert item is not None
    assert item.approval_status == ApprovalQueueStatus.PENDING


def test_kba_missing_work_item_does_not_update_queue(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    store.save_approval_item(
        ApprovalQueueItem(
            id="approval-kba-missing-workitem",
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Email draft",
            source_agent="pytest",
            metadata={"work_item_id": "wi_missing", "approval_scope": "external_use"},
        )
    )

    with pytest.raises(KeyError, match="WorkItem not found"):
        handle_slack_approval_interaction(
            _kba_payload(
                action_id=KBA_APPROVE_EXTERNAL_USE,
                intent="approve_external_use",
                approval_id="approval-kba-missing-workitem",
                work_item_id="wi_missing",
            ),
            database_url=database_url,
        )

    item = store.get_approval_item("approval-kba-missing-workitem")
    assert item is not None
    assert item.approval_status == ApprovalQueueStatus.PENDING


def test_slack_interactive_stale_approval_id_does_not_update_queue(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        status=WorkItemStatus.NEEDS_APPROVAL,
        title="Review outreach draft",
        approval_gates=[
            WorkItemApprovalGate(
                scope="external_use",
                state=ApprovalState.PENDING.value,
                approval_id="approval-current",
            )
        ],
    )
    store.save_work_item(item)
    store.save_approval_item(
        ApprovalQueueItem(
            id="approval-stale",
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Email draft",
            source_agent="pytest",
            metadata={"work_item_id": item.id},
        )
    )

    with pytest.raises(ValueError, match="stale WorkItem approval id"):
        handle_slack_approval_interaction(
            {
                "user": {"username": "anup"},
                "actions": [{"action_id": "keystone_approval_yes", "value": "approval-stale"}],
            },
            database_url=database_url,
        )

    queue_item = store.get_approval_item("approval-stale")
    loaded = store.get_work_item(item.id)
    assert queue_item is not None
    assert loaded is not None
    assert queue_item.approval_status == ApprovalQueueStatus.PENDING
    assert loaded.approval_gates[0].state == "pending"


def test_kba_stale_approval_id_does_not_update_queue(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'slack-workitem.db'}"
    store = SQLiteStore(database_url)
    item = WorkItem(
        kind=WorkItemKind.OUTREACH,
        status=WorkItemStatus.NEEDS_APPROVAL,
        title="Review outreach draft",
        approval_gates=[
            WorkItemApprovalGate(
                scope="external_use",
                state=ApprovalState.PENDING.value,
                approval_id="approval-current",
            )
        ],
    )
    store.save_work_item(item)
    store.save_approval_item(
        ApprovalQueueItem(
            id="approval-kba-stale",
            object_type="outreach_draft",
            object_id="1",
            title="Email draft",
            summary="Email draft",
            source_agent="pytest",
            metadata={"work_item_id": item.id, "approval_scope": "external_use"},
        )
    )

    with pytest.raises(ValueError, match="stale WorkItem approval id"):
        handle_slack_approval_interaction(
            _kba_payload(
                action_id=KBA_APPROVE_EXTERNAL_USE,
                intent="approve_external_use",
                approval_id="approval-kba-stale",
                work_item_id=item.id,
            ),
            database_url=database_url,
        )

    queue_item = store.get_approval_item("approval-kba-stale")
    loaded = store.get_work_item(item.id)
    assert queue_item is not None
    assert loaded is not None
    assert queue_item.approval_status == ApprovalQueueStatus.PENDING
    assert loaded.approval_gates[0].state == "pending"


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
    assert item.metadata["operator_feedback_for_agent"] == "Shorten the CTA before approval."
    assert item.metadata["feedback_available_for_llm"] is True


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
