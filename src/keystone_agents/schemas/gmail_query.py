"""Bounded model-visible Gmail schema, query, and context contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from keystone_agents.schemas.email_triage import GmailLinkRecord

MAX_GMAIL_MODEL_QUERIES = 3


def _bounded_text(value: Any, *, max_length: int) -> str:
    cleaned = " ".join(str(value or "").replace("\u2014", "-").split())
    return cleaned[:max_length]


def _bounded_text_list(
    value: Any,
    *,
    max_items: int,
    max_length: int,
) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _bounded_text(item, max_length=max_length)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        output.append(cleaned)
        if len(output) >= max_items:
            break
    return output


class GmailMailboxResourceSchema(BaseModel):
    """One read-only Gmail resource shape exposed to the model."""

    model_config = ConfigDict(extra="forbid")

    resource_type: Literal["message_summary", "message_context", "thread_context"]
    identity_fields: list[str] = Field(min_length=1)
    returned_fields: list[str] = Field(min_length=1)
    omitted_fields: list[str] = Field(default_factory=list)


class GmailQueryParameterSchema(BaseModel):
    """One bounded Gmail query input and its operator-visible meaning."""

    model_config = ConfigDict(extra="forbid")

    name: Literal["query", "label", "max_results"]
    purpose: str = Field(min_length=1, max_length=300)
    constraints: list[str] = Field(default_factory=list, max_length=5)


class GmailExecutionParameterSchema(BaseModel):
    """One model-visible execution control that does not broaden the query."""

    model_config = ConfigDict(extra="forbid")

    name: Literal["live"]
    purpose: str = Field(min_length=1, max_length=300)
    constraints: list[str] = Field(default_factory=list, max_length=5)


class GmailMailboxReadSchema(BaseModel):
    """Stable discovery contract for bounded Gmail model tools."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.gmail.read_schema.v1"] = "keystone.gmail.read_schema.v1"
    query_parameters: list[str]
    query_parameter_details: list[GmailQueryParameterSchema] = Field(min_length=3, max_length=3)
    execution_parameters: list[str] = Field(default_factory=lambda: ["live"])
    execution_parameter_details: list[GmailExecutionParameterSchema] = Field(
        min_length=1,
        max_length=1,
    )
    label_parameter_contract: Literal["provider_label_id_or_system_label"] = (
        "provider_label_id_or_system_label"
    )
    max_query_results: Literal[20] = 20
    resources: list[GmailMailboxResourceSchema]
    live_argument_required: Literal[True] = True
    live_environment_gate: Literal["KEYSTONE_ENABLE_LIVE_GMAIL"] = (
        "KEYSTONE_ENABLE_LIVE_GMAIL"
    )
    read_only: Literal[True] = True
    sends_email: Literal[False] = False
    writes_mailbox_state: Literal[False] = False
    raw_message_bodies_returned: Literal[False] = False
    dry_run_mode: Literal["synthetic_fixture"] = "synthetic_fixture"


class GmailMessageSummaryRecord(BaseModel):
    """Minimal metadata for one exact Gmail provider message."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    received_at: str = Field(default="", max_length=40)
    sender_name: str = Field(default="", max_length=200)
    sender_email: str = Field(default="", max_length=320)
    subject: str = Field(default="", max_length=300)
    snippet: str = Field(default="", max_length=500)
    prior_labels: list[str] = Field(default_factory=list, max_length=20)

    @field_validator(
        "message_id",
        "thread_id",
        "received_at",
        "sender_name",
        "sender_email",
        "subject",
        "snippet",
    )
    @classmethod
    def normalize_text(cls, value: str, info: Any) -> str:
        limits = {
            "message_id": 200,
            "thread_id": 200,
            "received_at": 40,
            "sender_name": 200,
            "sender_email": 320,
            "subject": 300,
            "snippet": 500,
        }
        return _bounded_text(value, max_length=limits[info.field_name])

    @field_validator("prior_labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: Any) -> list[str]:
        return _bounded_text_list(value, max_items=20, max_length=100)


class GmailSafeAttachmentMetadata(BaseModel):
    """Allowlisted, bounded attachment metadata without content or provider secrets."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(default="", max_length=255)
    mime_type: str = Field(default="", max_length=160)
    size_bytes: int = Field(default=0, ge=0, le=100_000_000)
    attachment_id_present: bool = False
    risk_flags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("filename", "mime_type", mode="before")
    @classmethod
    def normalize_text(cls, value: Any, info: Any) -> str:
        return _bounded_text(
            value,
            max_length=255 if info.field_name == "filename" else 160,
        )

    @field_validator("risk_flags", mode="before")
    @classmethod
    def normalize_risk_flags(cls, value: Any) -> list[str]:
        return _bounded_text_list(value, max_items=10, max_length=120)

    @classmethod
    def from_provider(cls, value: Mapping[str, Any]) -> GmailSafeAttachmentMetadata:
        """Project one provider object onto the safe attachment allowlist."""

        raw_size = value.get("size_bytes", value.get("size", 0))
        try:
            size = max(0, min(int(raw_size or 0), 100_000_000))
        except (TypeError, ValueError):
            size = 0
        return cls(
            filename=value.get("filename", ""),
            mime_type=value.get("mime_type", ""),
            size_bytes=size,
            attachment_id_present=bool(
                value.get("attachment_id_present") or value.get("attachment_id")
            ),
            risk_flags=value.get("risk_flags", []),
        )


class GmailSourceQuotationMetadata(BaseModel):
    """Bounded quotation classification without duplicating the quoted body text."""

    model_config = ConfigDict(extra="forbid")

    quote_kind: Literal["editorial_source", "reply_history", "ambiguous"]
    attribution: str = Field(default="", max_length=500)
    attribution_status: Literal[
        "not_applicable",
        "explicit",
        "inferred_reply_marker",
        "unknown",
    ] = "unknown"
    truncated: bool = False

    @field_validator("attribution", mode="before")
    @classmethod
    def normalize_attribution(cls, value: Any) -> str:
        return _bounded_text(value, max_length=500)


class GmailSourceWindowCoverage(BaseModel):
    """Exact character coverage for one sanitized MIME representation window."""

    model_config = ConfigDict(extra="forbid")

    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    full_char_count: int = Field(ge=0)
    complete: bool
    has_more: bool


class GmailSourceContinuationRequest(BaseModel):
    """Snapshot-pinned request for the next window of one exact Gmail MIME part."""

    model_config = ConfigDict(extra="forbid")

    resource_type: Literal["message"] = "message"
    resource_id: str = Field(min_length=1, max_length=200)
    body_part_path: str = Field(min_length=1, max_length=120)
    body_start_char: int = Field(ge=0)
    max_body_chars: int = Field(ge=1, le=3_000)
    expected_thread_id: str = Field(min_length=1, max_length=200)
    expected_account_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GmailSourceBodyEvidence(BaseModel):
    """Sanitized source representation tied to one exact Gmail message."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(default="", max_length=200)
    thread_id: str = Field(default="", max_length=200)
    received_at: str = Field(default="", max_length=40)
    sender_name: str = Field(default="", max_length=200)
    sender_email: str = Field(default="", max_length=320)
    part_path: str = Field(min_length=1, max_length=120)
    mime_type: str = Field(min_length=1, max_length=160)
    container_mime_type: str = Field(default="", max_length=160)
    alternative_group: str = Field(default="", max_length=120)
    representation: Literal["plain", "html", "other_text"]
    role: Literal[
        "single_representation",
        "selected_triage_view",
        "alternate_representation",
        "coexisting_section",
    ]
    source_text: str = Field(default="", max_length=3_000)
    quotation_metadata: list[GmailSourceQuotationMetadata] = Field(
        default_factory=list,
        max_length=20,
    )
    structure_annotations: bool = False
    content_complete: bool = True
    truncated: bool = False
    coverage: GmailSourceWindowCoverage | None = None
    next_request: GmailSourceContinuationRequest | None = None
    limitations: list[str] = Field(default_factory=list, max_length=10)

    @field_validator(
        "message_id",
        "thread_id",
        "received_at",
        "sender_name",
        "sender_email",
        "part_path",
        "mime_type",
        "container_mime_type",
        "alternative_group",
        mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: Any, info: Any) -> str:
        limits = {
            "message_id": 200,
            "thread_id": 200,
            "received_at": 40,
            "sender_name": 200,
            "sender_email": 320,
            "part_path": 120,
            "mime_type": 160,
            "container_mime_type": 160,
            "alternative_group": 120,
        }
        return _bounded_text(value, max_length=limits[info.field_name])

    @field_validator("source_text", mode="before")
    @classmethod
    def normalize_source_text(cls, value: Any) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        return text[:3_000]

    @field_validator("limitations", mode="before")
    @classmethod
    def normalize_limitations(cls, value: Any) -> list[str]:
        return _bounded_text_list(value, max_items=10, max_length=500)


class GmailModelRequestCapacity(BaseModel):
    """Dynamic request capacity returned inside the Gmail model tool loop."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.gmail.model_request_capacity.v1"] = (
        "keystone.gmail.model_request_capacity.v1"
    )
    configured: bool
    limit: int | None = Field(default=None, ge=0)
    consumed: int = Field(default=0, ge=0)
    parent_consumed: int = Field(default=0, ge=0)
    remaining_model_requests: int | None = Field(default=None, ge=0)
    reserved_final_response_requests: int = Field(default=1, ge=1, le=1)
    required_future_requests: int = Field(default=0, ge=0)
    capacity_sufficient: bool | None = None
    required_requests_for_candidate_read_and_final: int | None = Field(
        default=None,
        ge=2,
    )
    required_requests_for_corrective_query_read_and_final: int | None = Field(
        default=None,
        ge=3,
    )
    candidate_read_and_final_allowed: bool | None = None
    corrective_query_allowed: bool | None = None
    final_response_allowed: bool
    must_return_final_response_now: bool = False
    status: Literal[
        "not_configured",
        "candidate_read_and_final_admitted",
        "corrective_query_read_and_final_admitted",
        "final_response_only",
        "final_response_admitted",
        "exhausted",
    ]


class GmailMessageQueryResult(BaseModel):
    """Bounded query response that never includes a complete message body."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.gmail.message_query.v1"] = (
        "keystone.gmail.message_query.v1"
    )
    status: Literal["fixture", "read"]
    query: str = Field(default="", max_length=500)
    executed_queries: list[str] = Field(default_factory=list, max_length=2)
    label: str = Field(default="", max_length=100)
    requested_max_results: int = Field(ge=1, le=20)
    item_count: int = Field(ge=0, le=20)
    items: list[GmailMessageSummaryRecord] = Field(max_length=20)
    provider_read_performed: bool = False
    provider_write_performed: Literal[False] = False
    send_enabled: Literal[False] = False
    raw_message_bodies_returned: Literal[False] = False
    limitations: list[str] = Field(default_factory=list, max_length=10)
    model_request_capacity: GmailModelRequestCapacity | None = None

    @field_validator("query", "label", mode="before")
    @classmethod
    def normalize_query_scope(cls, value: Any, info: Any) -> str:
        return _bounded_text(value, max_length=500 if info.field_name == "query" else 100)

    @field_validator("limitations", mode="before")
    @classmethod
    def normalize_limitations(cls, value: Any) -> list[str]:
        return _bounded_text_list(value, max_items=10, max_length=500)


class GmailReadContextResult(BaseModel):
    """Safe context projection for one exact Gmail message or thread."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.gmail.read_context.v1"] = (
        "keystone.gmail.read_context.v1"
    )
    status: Literal[
        "fixture",
        "read",
        "not_found",
        "out_of_range",
        "source_changed",
        "source_inaccessible",
    ]
    resource_type: Literal["message", "thread"]
    resource_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(default="", max_length=200)
    account_identity_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    provider_history_id: str = Field(default="", max_length=200)
    source_snapshot_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    source_restart_required: bool = False
    message: GmailMessageSummaryRecord | None = None
    messages: list[GmailMessageSummaryRecord] = Field(default_factory=list, max_length=12)
    message_count: int = Field(default=0, ge=0)
    subject: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=1_200)
    thread_context: str = Field(default="", max_length=6_000)
    source_url: str = Field(default="", max_length=2048)
    extracted_links: list[GmailLinkRecord] = Field(default_factory=list, max_length=10)
    latest_received_at: str = Field(default="", max_length=40)
    participants: list[str] = Field(default_factory=list, max_length=12)
    action_items: list[str] = Field(default_factory=list, max_length=5)
    deadlines: list[str] = Field(default_factory=list, max_length=5)
    open_questions: list[str] = Field(default_factory=list, max_length=5)
    suspicious_signals: list[str] = Field(default_factory=list, max_length=10)
    attachment_metadata: list[GmailSafeAttachmentMetadata] = Field(
        default_factory=list,
        max_length=10,
    )
    body_evidence: list[GmailSourceBodyEvidence] = Field(default_factory=list, max_length=16)
    body_content_status: Literal["complete", "partial", "conflicting", "empty"] = (
        "empty"
    )
    body_content_complete: bool = False
    triage_limitations: list[str] = Field(default_factory=list, max_length=20)
    provider_read_performed: bool = False
    provider_write_performed: Literal[False] = False
    send_enabled: Literal[False] = False
    raw_message_bodies_returned: Literal[False] = False
    model_request_capacity: GmailModelRequestCapacity | None = None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        if value:
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or parsed.netloc != "mail.google.com"
                    or parsed.path != "/mail/" or not parsed.fragment.startswith("all/")):
                raise ValueError("source_url must identify a Gmail message or thread")
        return value

    @field_validator("extracted_links", mode="before")
    @classmethod
    def normalize_extracted_links(cls, value: Any) -> list[GmailLinkRecord]:
        if not isinstance(value, list | tuple):
            return []
        links = []
        seen = set()
        for item in value[:10]:
            item = item.model_dump() if isinstance(item, GmailLinkRecord) else item
            if not isinstance(item, Mapping):
                continue
            url = str(item.get("url") or "").strip()
            if not url or len(url) > 2048 or url in seen:
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    continue
                if parsed.username or parsed.password:
                    continue
            except ValueError:
                continue
            seen.add(url)
            links.append(GmailLinkRecord(
                url=url, domain=str(parsed.hostname)[:253],
                suspicious=item.get("suspicious") is True,
                reasons=_bounded_text_list(item.get("reasons", []), max_items=10, max_length=200),
            ))
        return links

    @field_validator(
        "resource_id",
        "thread_id",
        "provider_history_id",
        "subject",
        "summary",
        "thread_context",
        "latest_received_at",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: Any, info: Any) -> str:
        limits = {
            "resource_id": 200,
            "thread_id": 200,
            "provider_history_id": 200,
            "subject": 300,
            "summary": 1_200,
            "thread_context": 6_000 if info.data.get("resource_type") == "message" else 1_500,
            "latest_received_at": 40,
        }
        return _bounded_text(value, max_length=limits[info.field_name])

    @field_validator("participants", mode="before")
    @classmethod
    def normalize_participants(cls, value: Any) -> list[str]:
        return _bounded_text_list(value, max_items=12, max_length=200)

    @field_validator(
        "action_items",
        "deadlines",
        "open_questions",
        "suspicious_signals",
        "triage_limitations",
        mode="before",
    )
    @classmethod
    def normalize_text_lists(cls, value: Any, info: Any) -> list[str]:
        limits = {
            "action_items": (5, 500),
            "deadlines": (5, 500),
            "open_questions": (5, 500),
            "suspicious_signals": (10, 500),
            "triage_limitations": (20, 500),
        }
        max_items, max_length = limits[info.field_name]
        return _bounded_text_list(value, max_items=max_items, max_length=max_length)


class GmailSourceEvidencePage(BaseModel):
    """One retained page of already-read, sanitized Gmail source windows."""

    model_config = ConfigDict(extra="forbid")

    windows: list[GmailSourceBodyEvidence] = Field(min_length=1, max_length=16)


class GmailSelectedContextResult(GmailReadContextResult):
    """Selected ledger evidence; the read response remains independently bounded."""

    source_evidence_pages: list[GmailSourceEvidencePage] = Field(default_factory=list)
    source_read_count: int = Field(default=0, ge=0)
    source_windows_read: int = Field(default=0, ge=0)
    source_windows_retained: int = Field(default=0, ge=0)
    source_evidence_complete: bool = False


__all__ = [
    "GmailQueryParameterSchema",
    "GmailMailboxReadSchema",
    "GmailMailboxResourceSchema",
    "GmailMessageQueryResult",
    "GmailMessageSummaryRecord",
    "GmailReadContextResult",
    "GmailSelectedContextResult",
    "GmailSourceEvidencePage",
    "GmailSafeAttachmentMetadata",
    "GmailSourceBodyEvidence",
    "GmailSourceContinuationRequest",
    "GmailSourceQuotationMetadata",
    "GmailSourceWindowCoverage",
]
