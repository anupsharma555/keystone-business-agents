from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from keystone_agents.adapters import (
    GmailDraftResult,
    GmailMessage,
    KnowledgeDocument,
    ScrapedPage,
    SearchResult,
    SlackMessageResult,
    providers,
)
from keystone_agents.schemas.crm import (
    CRMLeadRecord,
    CRMProviderName,
    CRMWriteOperation,
    CRMWriteResult,
    CRMWriteStatus,
)


def test_provider_value_objects_have_safe_defaults() -> None:
    search = SearchResult(title="Result", link="https://example.com")
    scraped = ScrapedPage(url="https://example.com", text="Rendered text")
    gmail = GmailMessage(id="msg-1")
    draft = GmailDraftResult(draft_id="draft-1", message_id="msg-1")
    slack = SlackMessageResult(channel="approvals")
    document = KnowledgeDocument(id="doc-1", title="Doc", text="Evidence")
    lead = CRMLeadRecord(id="lead-1", company_name="Curebase")
    crm_result = CRMWriteResult(
        operation=CRMWriteOperation.UPDATE_STATUS,
        object_id="lead-1",
        status=CRMWriteStatus.BLOCKED_PENDING_APPROVAL,
    )

    assert search.snippet == ""
    assert search.source == "mock"
    assert scraped.metadata == {}
    assert scraped.source == "mock"
    assert gmail.labels == ()
    assert draft.approval_required is True
    assert draft.sent is False
    assert slack.status == "dry-run"
    assert document.source_uri == ""
    assert document.metadata == {}
    assert lead.status == "local_context"
    assert lead.approval_state == "pending"
    assert crm_result.provider == CRMProviderName.LOCAL_TABLE_MIRROR
    assert crm_result.dry_run is True
    assert crm_result.approval_required is True


def test_provider_value_objects_are_immutable() -> None:
    result = SearchResult(title="Result", link="https://example.com")

    with pytest.raises(FrozenInstanceError):
        result.title = "Changed"  # type: ignore[misc]


def test_provider_protocol_stubs_are_documented_noops() -> None:
    assert providers.CRMProvider.list_leads(object()) is None  # type: ignore[arg-type]
    assert providers.CRMProvider.update_status(object(), "lead-1", "qualified") is None  # type: ignore[arg-type]
    assert (
        providers.CRMProvider.attach_report_link_or_note(object(), "lead-1", note="Reviewed")
        is None
    )  # type: ignore[arg-type]
    assert providers.CRMProvider.fetch_account_context(object(), "Curebase") is None  # type: ignore[arg-type]
    assert providers.ModelProvider.model_name(object()) is None  # type: ignore[arg-type]
    assert providers.ModelProvider.build_run_config(object()) is None  # type: ignore[arg-type]
    assert providers.SearchProvider.search_web(object(), "query") is None  # type: ignore[arg-type]
    assert providers.ScrapeProvider.scrape_url(object(), "https://example.com") is None  # type: ignore[arg-type]
    assert providers.GmailProvider.list_recent_messages(object()) is None  # type: ignore[arg-type]
    assert providers.GmailProvider.get_message(object(), "msg-1") is None  # type: ignore[arg-type]
    assert providers.GmailProvider.apply_labels(object(), "msg-1", ["Label"]) is None  # type: ignore[arg-type]
    assert providers.GmailProvider.create_draft_reply(object(), "msg-1", "Body") is None  # type: ignore[arg-type]
    assert providers.SlackProvider.post_message(object(), "approvals", "Text") is None  # type: ignore[arg-type]
    assert (
        providers.SlackProvider.create_approval_request(object(), "approvals", "Approve?") is None
    )  # type: ignore[arg-type]
    assert providers.KnowledgeProvider.retrieve(object(), "query") is None  # type: ignore[arg-type]
    assert providers.StorageProvider.save_agent_run(object(), "agent", {}, {}) is None  # type: ignore[arg-type]
    assert (
        providers.StorageProvider.save_source(
            object(), "company", 1, "Title", "https://example.com"
        )
        is None
    )  # type: ignore[arg-type]
