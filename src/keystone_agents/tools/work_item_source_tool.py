"""Request-bound, provider-free reads of retained WorkItem source pages."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.schemas.source_evidence import source_evidence_access
from keystone_agents.schemas.work_item import WorkItemSourceRef
from keystone_agents.sdk import function_tool

MAX_SOURCE_EVIDENCE_RESPONSE_BYTES = 16_000


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_work_item_source_evidence_tool(sources: Sequence[WorkItemSourceRef]) -> Any:
    """Capture selected immutable snapshots; never accept a database or filesystem path."""
    retained: dict[str, WorkItemSourceRef] = {}
    for source in sources:
        if not source.evidence_pages:
            continue
        if (
            source.source_id in retained
            and retained[source.source_id].evidence_access != source.evidence_access
        ):
            raise ValueError("One source identity has conflicting retained evidence snapshots.")
        retained[source.source_id] = source.model_copy(deep=True)

    @function_tool(**keystone_tool_guardrail_kwargs())
    def read_work_item_source_evidence(
        source_id: str,
        expected_snapshot_sha256: str,
        page_index: int = 0,
        max_pages: int = 3,
        page_offset: int = 0,
    ) -> str:
        """Read retained source pages, following next_request without changing its cursor.

        Supply source_id and evidence_access.snapshot_sha256 from the context. At most
        three pages and 16000 UTF-8 bytes are returned. Oversized pages return consecutive
        page_fragment strings of their JSON representation; page_offset resumes the same
        page before advancing. page_count counts retained windows, not unread source
        material. Coverage and omissions remain in each page's provenance_json.
        This local tool cannot fetch a provider, access paths, change state or send.
        """
        if len(source_id) > 200:
            return _json(
                {
                    "status": "not_found",
                    "success": False,
                    "provider_read_performed": False,
                    "provider_write_performed": False,
                    "send_enabled": False,
                }
            )
        source = retained.get(source_id)
        output = {
            "source_id": source_id,
            "success": False,
            "provider_read_performed": False,
            "provider_write_performed": False,
            "send_enabled": False,
        }
        if source is None:
            return _json({**output, "status": "not_found"})
        access = source_evidence_access(source.evidence_pages)
        if access is None or expected_snapshot_sha256 != access.snapshot_sha256:
            return _json({**output, "status": "source_changed"})
        if not 0 <= page_index < access.page_count or not 1 <= max_pages <= 3 or page_offset < 0:
            return _json({**output, "status": "out_of_range"})

        def continuation(index: int, offset: int = 0) -> dict[str, object] | None:
            return (
                {
                    "source_id": source_id,
                    "expected_snapshot_sha256": access.snapshot_sha256,
                    "page_index": index,
                    "max_pages": max_pages,
                    "page_offset": offset,
                }
                if index < access.page_count
                else None
            )

        base = {
            **output,
            "status": "read",
            "success": True,
            "url": source.url,
            "provider_candidate_id": source.provider_candidate_id,
            "evidence_access": access.model_dump(mode="json"),
            "page_index": page_index,
        }
        # Return whole pages while they fit. The next cursor never skips a deferred page.
        if page_offset == 0:
            for end in range(min(page_index + max_pages, access.page_count), page_index, -1):
                result = _json(
                    {
                        **base,
                        "pages": [
                            page.model_dump(mode="json")
                            for page in source.evidence_pages[page_index:end]
                        ],
                        "next_request": continuation(end),
                    }
                )
                if len(result.encode("utf-8")) <= MAX_SOURCE_EVIDENCE_RESPONSE_BYTES:
                    return result
        # A large metadata/Unicode page is recoverable as exact serialized fragments.
        page_json = _json(source.evidence_pages[page_index].model_dump(mode="json"))
        if page_offset >= len(page_json):
            return _json({**output, "status": "out_of_range"})

        def fragment(end: int) -> str:
            return _json(
                {
                    **base,
                    "page_fragment": page_json[page_offset:end],
                    "page_offset": page_offset,
                    "page_end": end,
                    "page_json_char_count": len(page_json),
                    "next_request": (
                        continuation(page_index, end)
                        if end < len(page_json)
                        else continuation(page_index + 1)
                    ),
                }
            )

        low, high = page_offset + 1, len(page_json)
        while low < high:
            middle = (low + high + 1) // 2
            if len(fragment(middle).encode("utf-8")) <= MAX_SOURCE_EVIDENCE_RESPONSE_BYTES:
                low = middle
            else:
                high = middle - 1
        result = fragment(low)
        if len(result.encode("utf-8")) > MAX_SOURCE_EVIDENCE_RESPONSE_BYTES:
            return _json({**output, "status": "source_metadata_too_large"})
        return result

    return read_work_item_source_evidence
