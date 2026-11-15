"""Explicit scoped capture and whole-fact admission for small optional context.

The host supplies scope, provenance, storage and validated source identities.
Model arguments never choose a database, destination, approval or replacement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from keystone_agents.memory import chief_of_staff_memory_item, operator_reference_memory_item
from keystone_agents.schemas.memory import MemoryItem
from keystone_agents.schemas.memory_scope import (
    MEMORY_CAPTURE_KEY,
    MEMORY_SCOPE_KEY,
    SCOPED_FACT_TYPES,
    MemoryCaptureProvenance,
    SlackMemoryScope,
)
from keystone_agents.tools.storage_tool import StorageTool
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    validate_public_http_url,
)


class ScopedMemoryCaptureInput(BaseModel):
    """The bounded content choices available to a capture-capable model."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["preference", "reference"]
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=350)
    source_ref_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=2,
    )

    @field_validator("title", "summary")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Capture content cannot be blank.")
        return value

    @field_validator("source_ref_ids")
    @classmethod
    def exact_unique_refs(cls, value: list[str]) -> list[str]:
        if any(ref != ref.strip() for ref in value) or len(set(value)) != len(value):
            raise ValueError("Capture references must be exact unique identities.")
        return value


@dataclass(frozen=True)
class ScopedMemoryProjection:
    model_context: dict[str, Any] | None
    serialized_bytes: int
    omitted_count: int


def memory_capture_fingerprint(
    scope: SlackMemoryScope, *, thread_ts: str, request_ts: str, raw_request: str,
) -> str:
    """Bind exact operator text to one authenticated capture event."""

    scope = SlackMemoryScope.model_validate(scope)
    MemoryCaptureProvenance(
        thread_ts=thread_ts, request_ts=request_ts, request_fingerprint="0" * 64,
    )
    if not isinstance(raw_request, str) or not raw_request.strip():
        raise ValueError("Capture fingerprint requires the exact current operator request.")
    payload = {
        "scope": scope.model_dump(mode="json"), "thread_ts": thread_ts,
        "request_ts": request_ts, "raw_request": raw_request,
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def capture_scoped_memory(
    storage: StorageTool, *, scope: SlackMemoryScope, provenance: MemoryCaptureProvenance,
    capture_authorized: bool, capture: ScopedMemoryCaptureInput,
    source_refs: Mapping[str, str],
) -> dict[str, Any]:
    """Create one audited memory only for the current explicit capture request."""

    if capture_authorized is not True:
        raise ValueError("Memory capture requires explicit current operator authorization.")
    scope = SlackMemoryScope.model_validate(scope)
    provenance = MemoryCaptureProvenance.model_validate(provenance)
    capture = ScopedMemoryCaptureInput.model_validate(capture)
    if any(ref not in source_refs for ref in capture.source_ref_ids):
        raise ValueError("Capture selected an unknown source reference.")
    urls = [source_refs[ref] for ref in capture.source_ref_ids]
    for url in urls:
        try:
            valid = isinstance(url, str) and validate_public_http_url(
                url, resolve_hostname=False,
            ) == url
        except WebsiteExtractionError:
            valid = False
        if not valid:
            raise ValueError("Capture references require exact validated public URLs.")
    if capture.kind == "reference":
        if len(urls) != 1:
            raise ValueError("A reference capture requires one exact validated URL.")
        item = operator_reference_memory_item(
            title=capture.title, summary=capture.summary, url=urls[0],
            source="scoped_operator_capture",
        )
    else:
        item = chief_of_staff_memory_item(
            memory_type="operator_strategy", title=capture.title, summary=capture.summary,
            source_ids=capture.source_ref_ids,
        )
    item = item.model_copy(update={
        "source_ids": capture.source_ref_ids or [
            f"operator-request:{provenance.request_fingerprint}"
        ],
        "content": {**item.content, "source_urls": urls, "capture_kind": capture.kind},
    })
    return storage.save_memory_item(
        item, trusted_scope=scope, capture_provenance=provenance,
    )


def _serialized_size(value: Any) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))


def project_scoped_memory_facts(
    records: Sequence[MemoryItem | Mapping[str, Any]], *, max_bytes: int = 4096,
    max_facts: int = 4,
) -> ScopedMemoryProjection:
    """Pack already scope-admitted records without truncating identities.

    Scoped retrieval owns destination, expiry and supersession eligibility; this
    pure projection only rechecks basic shape/safety and bounds the payload.
    """

    ceiling = max(0, min(4096, int(max_bytes)))
    facts: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in records:
        item = MemoryItem.model_validate(raw)
        if (
            not item.id or item.id in seen or item.memory_type not in SCOPED_FACT_TYPES
            or not item.approved_for_reuse or not item.safe_for_prompt
        ):
            continue
        try:
            SlackMemoryScope.model_validate(item.metadata.get(MEMORY_SCOPE_KEY))
            MemoryCaptureProvenance.model_validate(item.metadata.get(MEMORY_CAPTURE_KEY))
        except ValueError:
            continue
        source_urls = item.content.get("source_urls", [])
        if not isinstance(source_urls, list) or any(
            not isinstance(url, str) for url in source_urls
        ):
            continue
        fact = {
            "memory_id": item.id, "memory_type": item.memory_type,
            "title": item.title, "summary": item.summary,
            "source_ids": list(item.source_ids), "source_urls": list(source_urls),
            "created_at": item.created_at,
        }
        if len(facts) >= max(0, min(4, int(max_facts))):
            continue
        if _serialized_size({"facts": [*facts, fact]}) > ceiling:
            continue
        facts.append(fact)
        seen.add(item.id)
    context = {"facts": facts} if facts else None
    return ScopedMemoryProjection(
        model_context=context, serialized_bytes=_serialized_size(context) if context else 0,
        omitted_count=len(records) - len(facts),
    )


def query_scoped_memory_facts(
    storage: StorageTool, *, scope: SlackMemoryScope, query: str = "", max_bytes: int = 4096,
) -> ScopedMemoryProjection:
    """Filter authorized useful records before ranking, then admit at most four facts."""

    records = storage.retrieve_memory(
        query=query, limit=8, approved_only=True, safe_for_prompt=True, admission_scope=scope,
    )
    return project_scoped_memory_facts(records, max_bytes=max_bytes)


def revalidate_scoped_memory_facts(
    storage: StorageTool, *, scope: SlackMemoryScope,
    facts: Sequence[Mapping[str, Any]], max_bytes: int = 4096,
) -> list[dict[str, Any]]:
    """Keep only seed facts still identical to current scope-admitted records."""

    memory_ids = [fact["memory_id"] for fact in facts]
    if not memory_ids:
        return []
    try:
        records = storage.retrieve_memory(
            memory_ids=memory_ids, limit=4, admission_scope=scope,
        )
    except Exception:
        # A failed store read cannot make a saved projection authoritative.
        return []
    current_by_id: dict[int, dict[str, Any]] = {}
    for record in records:
        projected = project_scoped_memory_facts([record], max_bytes=max_bytes)
        for current in (projected.model_context or {}).get("facts") or []:
            current_by_id[current["memory_id"]] = current
    return [dict(fact) for fact in facts if fact == current_by_id.get(fact["memory_id"])]
