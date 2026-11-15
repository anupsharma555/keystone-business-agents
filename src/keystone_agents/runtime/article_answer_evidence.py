"""Bounded selected-page evidence for an Orchestrator-authored short answer.

The shared extractor owns public URL access. This module projects its retained
text into model input and checks citation identity without composing a summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from keystone_agents.instruction_following import canonical_source_url, canonical_source_urls
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    build_selected_url_source_bundle,
    validate_public_http_url,
)


@dataclass(frozen=True)
class ArticleAnswerEvidence:
    source_url: str
    model_context: dict[str, Any]
    partial: bool

    def validates_answer(self, answer: str) -> bool:
        return set(canonical_source_urls(answer)) == {canonical_source_url(self.source_url)}


def acquire_article_answer_evidence(selected_url: str) -> ArticleAnswerEvidence:
    """Read one public page under the existing extraction gates and limits."""

    selected_url = validate_public_http_url(selected_url, resolve_hostname=False)
    extracted = build_selected_url_source_bundle(
        company_name="Selected public article",
        company_url=selected_url,
        selected_urls=[selected_url],
        live_extraction=True,
    )
    selected_identity = canonical_source_url(selected_url)
    sources = [
        source for source in extracted.source_bundle.sources
        if canonical_source_url(source.url) == selected_identity
        and source.evidence_excerpt.strip()
    ]
    if (
        extracted.mode != "live" or extracted.extracted_source_count != 1
        or extracted.deferred_selected_urls or len(sources) != 1
        or not any(
            item.included_in_bundle and item.status == "success"
            and canonical_source_url(item.selected_url) == selected_identity
            for item in extracted.diagnostics
        )
    ):
        raise WebsiteExtractionError(
            "The selected article could not be read as bounded source text. "
            "Provide its text or retry the same source when access is available."
        )
    source = sources[0]
    excerpt = source.evidence_excerpt[:8000]
    access = source.web_source_access
    partial = bool(
        len(source.evidence_excerpt) > len(excerpt)
        or access is None or not access.content_complete
    )
    return ArticleAnswerEvidence(
        source_url=selected_url,
        partial=partial,
        model_context={
            "source_id": source.source_id,
            "title": source.title,
            "source_url": selected_url,
            "extracted_text": excerpt,
            "evidence_scope": "extracted_text_excerpt" if partial else "complete_extracted_text",
            "limitations": [
                "Extracted text does not establish inspection of images, code execution, "
                "or linked sources.",
                *(
                    ["Only a bounded excerpt was read; do not claim a full article review."]
                    if partial else []
                ),
            ],
            "access": access.model_dump(mode="json") if access is not None else None,
            "acquisition": "workflow_called_selected_url_extraction",
            "broad_search_performed": False,
            "external_write_performed": False,
        },
    )
