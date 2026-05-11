"""Provider protocols for optional integrations.

The interfaces are intentionally small and vendor-neutral. Concrete providers
must support dry-run or mock mode and must not require optional dependencies
for tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from keystone_agents.schemas.contact_context import LocalAccountContext
from keystone_agents.schemas.crm import CRMLeadRecord, CRMWriteResult

CRMLead = CRMLeadRecord


@dataclass(frozen=True)
class SearchResult:
    title: str
    link: str
    snippet: str = ""
    source: str = "mock"


@dataclass(frozen=True)
class ScrapedPage:
    url: str
    text: str
    title: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source: str = "mock"


@dataclass(frozen=True)
class GmailMessage:
    id: str
    thread_id: str = ""
    sender: str = ""
    subject: str = ""
    snippet: str = ""
    body: str = ""
    labels: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class GmailDraftResult:
    draft_id: str
    message_id: str
    thread_id: str = ""
    approval_required: bool = True
    sent: bool = False


@dataclass(frozen=True)
class SlackMessageResult:
    channel: str
    message_ts: str = ""
    status: str = "dry-run"


@dataclass(frozen=True)
class KnowledgeDocument:
    id: str
    title: str
    text: str
    source_uri: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class ModelProvider(Protocol):
    """Model routing provider.

    Implementations may wrap direct OpenAI configuration, LiteLLM, or another
    OpenAI-compatible route while preserving OpenAI Agents SDK agent contracts.
    """

    dry_run: bool

    def model_name(self) -> str:
        """Return the selected model name without making a network call."""

    def build_run_config(self) -> Any:
        """Return provider-specific SDK run configuration for live execution."""


class SearchProvider(Protocol):
    """Source-attributed web search provider."""

    dry_run: bool

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        """Return normalized search results. Dry-run mode must avoid network calls."""


class ScrapeProvider(Protocol):
    """LLM-ready extraction provider for pages or URLs."""

    dry_run: bool

    def scrape_url(self, url: str) -> ScrapedPage:
        """Return extracted page text and metadata. Dry-run mode must avoid network calls."""


class GmailProvider(Protocol):
    """Gmail read, label, and draft-only provider."""

    dry_run: bool

    def list_recent_messages(
        self,
        label: str | None = None,
        max_results: int = 1,
        query: str | None = None,
    ) -> list[GmailMessage]:
        """List recent messages without processing the full inbox by default."""

    def get_message(self, message_id: str) -> GmailMessage:
        """Load one message."""

    def apply_labels(self, message_id: str, labels: Sequence[str]) -> Mapping[str, Any]:
        """Apply labels or return a dry-run intent."""

    def create_draft_reply(self, message_id: str, body: str) -> GmailDraftResult:
        """Create a reply draft only. Sending is outside this interface."""

    def create_draft(self, to: str, subject: str, body: str) -> GmailDraftResult:
        """Create a standalone outbound draft only. Sending is outside this interface."""


class SlackProvider(Protocol):
    """Slack notification and approval provider."""

    dry_run: bool

    def post_message(
        self, channel: str, text: str, metadata: Mapping[str, Any] | None = None
    ) -> SlackMessageResult:
        """Post or simulate a Slack message."""

    def create_approval_request(
        self,
        channel: str,
        text: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> SlackMessageResult:
        """Create or simulate an approval request."""


class CRMProvider(Protocol):
    """Local-first CRM provider seam.

    Version 1 supports only fixture/table-mirror-backed local providers. Live CRM
    vendors are intentionally outside this contract until a reviewed integration
    design exists.
    """

    dry_run: bool

    def list_leads(
        self,
        company_name: str | None = None,
        *,
        approved_only: bool = False,
    ) -> list[CRMLeadRecord]:
        """List local leads without calling external CRM APIs."""

    def update_status(
        self,
        lead_id: str,
        status: str,
        *,
        approval_state: str = "pending",
        reviewer: str = "",
        note: str = "",
    ) -> CRMWriteResult:
        """Return a dry-run, approval-aware status update intent."""

    def attach_report_link_or_note(
        self,
        lead_id: str,
        *,
        report_link: str | None = None,
        note: str = "",
        approval_state: str = "pending",
        reviewer: str = "",
    ) -> CRMWriteResult:
        """Return a dry-run, approval-aware report-link or note attachment intent."""

    def fetch_account_context(
        self,
        company_name: str,
        *,
        approved_only: bool = True,
    ) -> LocalAccountContext:
        """Fetch local account context for prompts without live CRM calls."""


class KnowledgeProvider(Protocol):
    """Document knowledge and retrieval provider."""

    dry_run: bool

    def retrieve(self, query: str, limit: int = 5) -> list[KnowledgeDocument]:
        """Return citation-ready documents from local or cloud knowledge sources."""


class StorageProvider(Protocol):
    """Audit and artifact storage provider."""

    dry_run: bool

    def save_agent_run(
        self,
        agent_name: str,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        *,
        status: str = "success",
    ) -> int:
        """Persist an agent run or dry-run audit record."""

    def save_source(
        self,
        object_type: str,
        object_id: int,
        title: str,
        url: str,
        snippet: str = "",
    ) -> int:
        """Persist a source record or dry-run audit intent."""
