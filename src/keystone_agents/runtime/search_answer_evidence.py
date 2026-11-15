"""Verify that a simple searched answer cites the completed search output."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from keystone_agents.instruction_following import canonical_source_url, canonical_source_urls
from keystone_agents.receipts.normalization import identity_fingerprint
from keystone_agents.runtime.decision_validation import model_tool_output_payloads


@dataclass(frozen=True)
class SearchAnswerEvidenceVerdict:
    passed: bool
    reason: str
    returned_urls: tuple[str, ...] = ()
    visible_urls: tuple[str, ...] = ()

    @property
    def returned_url_count(self) -> int:
        return len(self.returned_urls)

    @property
    def visible_url_count(self) -> int:
        return len(self.visible_urls)


def validate_search_answer_evidence(
    raw_results: Iterable[Any],
    tool_invocations: Sequence[Mapping[str, Any]],
    tool_receipts: Sequence[Mapping[str, Any]],
    response_text: str,
) -> SearchAnswerEvidenceVerdict:
    """Require one completed search and citations from its attested returned links."""

    visible = canonical_source_urls(response_text)
    search_events = [
        item for item in tool_invocations if item.get("tool_name") == "search_web"
    ]
    started = [item for item in search_events if item.get("status") == "started"]
    completed = [item for item in search_events if item.get("status") == "completed"]
    if not completed:
        return SearchAnswerEvidenceVerdict(False, "search_call_missing", visible_urls=visible)
    if (
        len(started) != 1
        or len(completed) != 1
        or started[0].get("invocation_index") is None
        or started[0].get("invocation_index") != completed[0].get("invocation_index")
        or any(item.get("status") not in {"started", "completed"} for item in search_events)
    ):
        return SearchAnswerEvidenceVerdict(False, "search_call_count_invalid", visible_urls=visible)

    search_receipts = [
        receipt for receipt in tool_receipts if receipt.get("tool_name") == "search_web"
    ]
    success_receipts = [
        receipt for receipt in search_receipts
        if receipt.get("tool_name") == "search_web"
        and receipt.get("status") == "success"
        and receipt.get("operation") == "search"
        and isinstance(receipt.get("item_count"), int)
        and receipt["item_count"] > 0
    ]
    if len(search_receipts) != 1 or len(success_receipts) != 1:
        return SearchAnswerEvidenceVerdict(False, "search_receipt_missing", visible_urls=visible)
    attested_links = {
        str(value)
        for receipt in success_receipts
        for value in receipt.get("identity_fingerprints") or []
        if isinstance(value, str)
    }
    returned: list[str] = []
    for payload in model_tool_output_payloads(raw_results, tool_names=("search_web",)):
        if not isinstance(payload, list | tuple):
            continue
        for item in list(payload)[:100]:
            if hasattr(item, "model_dump"):
                item = item.model_dump(mode="json")
            if not isinstance(item, Mapping):
                continue
            raw_url = str(item.get("link") or item.get("url") or "").strip()
            canonical = canonical_source_url(raw_url)
            if canonical and identity_fingerprint(raw_url) in attested_links:
                returned.append(canonical)
    returned_urls = tuple(dict.fromkeys(returned))
    if not returned_urls:
        return SearchAnswerEvidenceVerdict(
            False, "search_result_missing", visible_urls=visible,
        )
    if not visible:
        return SearchAnswerEvidenceVerdict(
            False, "visible_source_missing", returned_urls=returned_urls,
        )
    if not set(visible).issubset(returned_urls):
        return SearchAnswerEvidenceVerdict(
            False, "source_not_returned", returned_urls=returned_urls, visible_urls=visible,
        )
    return SearchAnswerEvidenceVerdict(
        True, "passed", returned_urls=returned_urls, visible_urls=visible,
    )


__all__ = ["SearchAnswerEvidenceVerdict", "validate_search_answer_evidence"]
