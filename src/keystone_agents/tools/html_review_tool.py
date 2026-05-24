"""Bounded HTML/text review helpers for source-backed research claims."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from keystone_agents.config import parse_bool
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import (
    build_model_settings,
    build_sdk_agent,
    function_tool,
    run_typed_sdk_sync,
)
from keystone_agents.source_enrichment import extract_claim_candidates, extract_clean_text

AGENT_HTML_REVIEW_PROVIDER = "agents-sdk-html-review"
DEFAULT_AGENT_HTML_REVIEW_MAX_CHARS = 8_000
DEFAULT_AGENT_HTML_REVIEW_MAX_CLAIMS = 6
DEFAULT_AGENT_HTML_REVIEW_MAX_PAGES = 2


class HtmlReviewError(RuntimeError):
    """Raised when the optional agent HTML review lane fails."""


class HtmlReviewResult(BaseModel):
    """Agent-reviewed claim candidates from one bounded page payload."""

    url: str = ""
    title: str = ""
    subject: str = ""
    provider: str = AGENT_HTML_REVIEW_PROVIDER
    status: str = "success"
    claims: list[str] = Field(default_factory=list)
    confidence: float = 0.72
    notes: list[str] = Field(default_factory=list)


def agent_html_review_enabled() -> bool:
    """Return whether live retrieval may use the optional agent HTML review lane."""

    return parse_bool(os.getenv("KEYSTONE_AGENT_HTML_REVIEW"))


def agent_html_review_max_pages() -> int:
    """Return the per-run cap for agent HTML review pages."""

    return _bounded_env_int(
        "KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES",
        default=DEFAULT_AGENT_HTML_REVIEW_MAX_PAGES,
        minimum=0,
        maximum=4,
    )


def agent_html_review_max_chars() -> int:
    """Return the per-page input cap for agent HTML/text review."""

    return _bounded_env_int(
        "KEYSTONE_AGENT_HTML_REVIEW_MAX_CHARS",
        default=DEFAULT_AGENT_HTML_REVIEW_MAX_CHARS,
        minimum=1_000,
        maximum=20_000,
    )


def agent_html_review_min_claims() -> int:
    """Return claim count below which deterministic extraction is considered weak."""

    return _bounded_env_int(
        "KEYSTONE_AGENT_HTML_REVIEW_MIN_CLAIMS",
        default=1,
        minimum=0,
        maximum=5,
    )


def _bounded_env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _bounded_page_text(html_or_text: str) -> str:
    text = extract_clean_text(html_or_text)
    if not text:
        return ""
    return text[: agent_html_review_max_chars()]


def _coerce_review_output(output: Any) -> HtmlReviewResult:
    if hasattr(output, "final_output"):
        output = output.final_output
    if isinstance(output, HtmlReviewResult):
        return output
    if isinstance(output, BaseModel):
        output = output.model_dump(mode="json")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return HtmlReviewResult(status="empty", notes=["Agent returned unstructured text."])
    if isinstance(output, dict):
        if "claims" not in output and "claim_candidates" in output:
            output = {**output, "claims": output["claim_candidates"]}
        return HtmlReviewResult.model_validate(output)
    return HtmlReviewResult(status="empty", notes=["Agent returned no structured review."])


def deterministic_html_review(
    *,
    html_or_text: str,
    subject: str,
    url: str = "",
    title: str = "",
    max_claims: int = DEFAULT_AGENT_HTML_REVIEW_MAX_CLAIMS,
) -> HtmlReviewResult:
    """Extract deterministic claim candidates from bounded HTML/text without model calls."""

    bounded_text = _bounded_page_text(html_or_text)
    if not bounded_text:
        return HtmlReviewResult(
            url=url,
            title=title,
            subject=subject,
            status="empty",
            claims=[],
            confidence=0.0,
            notes=["No readable text was available for HTML review."],
        )
    claims = extract_claim_candidates(
        bounded_text,
        company_name=subject or title or url or "source",
        max_claims=max_claims,
    )
    return HtmlReviewResult(
        url=url,
        title=title,
        subject=subject,
        status="success" if claims else "empty",
        claims=claims,
        confidence=0.68 if claims else 0.0,
        notes=["Deterministic HTML/text extraction only; no model call."],
    )


def run_agent_html_review(
    *,
    html_or_text: str,
    subject: str,
    url: str = "",
    title: str = "",
    max_claims: int = DEFAULT_AGENT_HTML_REVIEW_MAX_CLAIMS,
    model: str | None = None,
    live: bool = True,
    runner: Callable[[str, dict[str, Any]], Any] | None = None,
) -> HtmlReviewResult:
    """Run a credential-gated SDK agent over one bounded page payload."""

    bounded_text = _bounded_page_text(html_or_text)
    if not bounded_text:
        return HtmlReviewResult(
            url=url,
            title=title,
            subject=subject,
            status="empty",
            claims=[],
            confidence=0.0,
            notes=["No readable text was available for agent HTML review."],
        )

    payload = {
        "subject": subject,
        "url": url,
        "title": title,
        "max_claims": max(1, min(10, max_claims)),
        "text": bounded_text,
    }
    prompt = _agent_html_review_prompt(payload)
    if runner is not None:
        return _coerce_review_output(runner(prompt, payload))
    if not live:
        raise HtmlReviewError("Agent HTML review requires live=True or an injected runner.")

    agent = build_sdk_agent(
        name="agent_html_review",
        instructions=(
            "You review one bounded HTML/text payload for Keystone research. "
            "Extract only concise claims directly supported by the supplied page text. "
            "Do not infer, browse, use private pages, or add facts from memory."
        ),
        output_type=HtmlReviewResult,
        tools=[],
        model=model,
        model_settings=build_model_settings(reasoning_effort="low", verbosity="low"),
    )
    _raw_result, typed = run_typed_sdk_sync(
        agent,
        prompt,
        HtmlReviewResult,
        live=True,
        workflow_name="Keystone agent HTML review",
        tracing_disabled=True,
        trace_include_sensitive_data=False,
    )
    return typed


def _agent_html_review_prompt(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Review this single source for source-backed research claims.",
            f"Subject: {payload.get('subject') or '(unknown)'}",
            f"URL: {payload.get('url') or '(unknown)'}",
            f"Title: {payload.get('title') or '(unknown)'}",
            f"Return at most {payload.get('max_claims')} claims.",
            "Rules:",
            "- Use only the supplied text.",
            "- Keep claims factual, concise, and directly supported.",
            "- Prefer current product, customer, partnership, research, funding, hiring, "
            "grant, trial, conference, or source-relevance signals.",
            "- Return an empty claims list if the text is marketing-only or unsupported.",
            "",
            "Page text:",
            str(payload.get("text") or ""),
        ]
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def extract_research_claims_from_html(
    html_or_text: str,
    subject: str,
    url: str | None = None,
    title: str | None = None,
    max_claims: int = DEFAULT_AGENT_HTML_REVIEW_MAX_CLAIMS,
) -> str:
    """Extract bounded source-backed claim candidates from supplied HTML or text."""

    result = deterministic_html_review(
        html_or_text=html_or_text,
        subject=subject,
        url=url or "",
        title=title or "",
        max_claims=max_claims,
    )
    return result.model_dump_json()
