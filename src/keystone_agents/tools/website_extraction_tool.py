"""Website content extraction tools for company research."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import os
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urljoin, urlparse

import requests
from pydantic import BaseModel, Field

from keystone_agents.config import load_settings, parse_bool, runtime_state_dir
from keystone_agents.guardrails import (
    enforce_public_source_output_guardrails,
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
    keystone_tool_guardrail_kwargs,
)
from keystone_agents.schemas.web_source import WebSourceAccess
from keystone_agents.sdk import (
    ToolGuardrailFunctionOutput,
    function_tool,
    tool_input_guardrail,
    tool_output_guardrail,
)
from keystone_agents.source_enrichment import (
    SourceBundle,
    build_source_bundle,
    extract_claim_candidates,
    extract_clean_text,
    normalize_source_record,
)

WebsiteExtractorProvider = Literal["trafilatura", "crawl4ai", "firecrawl"]
WebsiteExtractionGuardrailContext = Literal[
    "default",
    "public_opportunity_source",
    "public_web_source",
]
MAX_SELECTED_URLS_PER_BUNDLE = 8
MAX_PUBLIC_URL_CHARS = 2048
MAX_PUBLIC_REDIRECTS = 5
DEFAULT_SOURCE_EXCERPT_CHARS = 4000
MAX_SOURCE_EXCERPT_CHARS = 8000
MAX_WEB_BUNDLE_RESPONSE_CHARS = 48000
MAX_WEB_SNAPSHOT_CHARS = 2_000_000
MAX_WEB_PREVIEW_TOTAL_CHARS = 16000
CRAWL4AI_EXPERIMENTAL_LIVE_ENV = "KEYSTONE_ENABLE_EXPERIMENTAL_CRAWL4AI"
_LOCAL_HOSTNAME_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


class WebsiteExtractionError(RuntimeError):
    """Raised when a configured website extraction provider fails."""

    def __init__(self, message: str, *, attempts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.attempts = list(attempts or [])


class WebsiteExtractionResult(BaseModel):
    """Clean extracted page content ready for company research source records."""

    url: str
    title: str = ""
    provider: str
    status: str
    text_or_markdown: str = ""
    claims: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebsiteExtractionAttemptDiagnostic(BaseModel):
    """One bounded provider attempt made for a selected URL."""

    provider: str
    status: str
    text_length: int = Field(default=0, ge=0)
    claim_count: int = Field(default=0, ge=0)
    error: str = ""


class SelectedUrlExtractionDiagnostic(BaseModel):
    """Per-URL extraction evidence without losing the selected URL identity."""

    source_id: str
    selected_url: str = Field(max_length=MAX_PUBLIC_URL_CHARS)
    resolved_url: str = Field(max_length=MAX_PUBLIC_URL_CHARS)
    provider: str
    status: str
    extraction_strategy: str
    included_in_bundle: bool = False
    text_length: int = Field(default=0, ge=0)
    claim_count: int = Field(default=0, ge=0)
    attempts: list[WebsiteExtractionAttemptDiagnostic] = Field(default_factory=list)
    error: str = ""


class SelectedUrlSourceBundleResult(BaseModel):
    """Typed, LLM-ready evidence bundle built from bounded selected URLs."""

    mode: Literal["dry_run", "live"]
    company_name: str = Field(min_length=1, max_length=300)
    selected_url_count: int = Field(ge=0)
    extracted_source_count: int = Field(ge=0)
    source_bundle: SourceBundle
    diagnostics: list[SelectedUrlExtractionDiagnostic] = Field(default_factory=list)
    deferred_selected_urls: list[str] = Field(default_factory=list)
    coverage_note: str = (
        "Claims are selected previews, not complete evidence. For partial sources call "
        "read_web_source_window with source_id, selected_url, expected_snapshot_sha256 "
        "from snapshot_sha256, and start_char from next_start_char. Repeat until complete "
        "before ruling out later qualifications. Deferred URLs require another selected-URL call."
    )
    firecrawl_max_calls: int = Field(default=0, ge=0)
    firecrawl_calls_attempted: int = Field(default=0, ge=0)


def _web_snapshot_dir() -> Path:
    return (runtime_state_dir() or Path(".keystone/state")) / "web-source-snapshots"


def _web_snapshot_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def project_web_source(
    result: WebsiteExtractionResult,
    *,
    source_id: str = "",
    selected_url: str,
    max_chars: int = DEFAULT_SOURCE_EXCERPT_CHARS,
) -> tuple[str, WebSourceAccess]:
    """Retain exact extracted text locally and return bounded versioned access.

    This saves only an already selected/extracted public page. It never follows
    links, repeats extraction, or substitutes a newer page for a saved snapshot.
    """

    enforce_public_source_output_guardrails("project_web_source", result)
    selected = validate_public_http_url(selected_url, resolve_hostname=False)
    source_id = source_id or _selected_url_source_id(selected)
    resolved = validate_public_http_url(
        str(result.metadata.get("final_url") or result.url), resolve_hostname=False
    )
    text = str(result.text_or_markdown or "").strip()
    payload = {
        "source_id": source_id,
        "selected_url": selected,
        "resolved_url": resolved,
        "text": text,
    }
    digest = _web_snapshot_hash(payload)
    limit = max(0, min(MAX_SOURCE_EXCERPT_CHARS, int(max_chars)))
    # JSON escaping is part of the response budget as well as source length.
    end = min(limit, len(text))
    while len(json.dumps(text[:end])) > MAX_SOURCE_EXCERPT_CHARS + 2:
        end //= 2
    access = WebSourceAccess(
        source_id=source_id,
        selected_url=selected,
        resolved_url=resolved,
        snapshot_sha256=digest,
        total_chars=len(text),
        start_char=0,
        end_char=end,
        next_start_char=end if end < len(text) else None,
        content_complete=end == len(text),
    )
    if not text:
        return "", access.model_copy(
            update={
                "available": False,
                "content_complete": False,
                "limitation": "Extraction returned no readable text; content is unavailable.",
            }
        )
    if len(text) > MAX_WEB_SNAPSHOT_CHARS:
        return text[:end], access.model_copy(
            update={
                "available": False,
                "next_start_char": None,
                "limitation": "Text exceeds the snapshot limit; later content is unavailable.",
            }
        )
    try:
        folder = _web_snapshot_dir()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f"{digest}.json"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or _web_snapshot_hash(json.loads(path.read_text())) != digest:
                raise ValueError("Stored source snapshot failed integrity validation.") from None
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
    except (OSError, ValueError):
        access = access.model_copy(
            update={
                "available": False,
                "next_start_char": None,
                "limitation": "Snapshot storage unavailable; later evidence cannot be retrieved.",
            }
        )
    return text[:end], access


def read_web_source_window_impl(
    *,
    source_id: str,
    selected_url: str,
    expected_snapshot_sha256: str,
    start_char: int = 0,
    max_chars: int = DEFAULT_SOURCE_EXCERPT_CHARS,
) -> dict[str, Any]:
    """Read an exact immutable local snapshot without fetching any URL."""

    digest = str(expected_snapshot_sha256)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WebsiteExtractionError("A valid expected source snapshot SHA-256 is required.")
    selected = validate_public_http_url(selected_url, resolve_hostname=False)
    if not 0 <= start_char or not 1 <= max_chars <= MAX_SOURCE_EXCERPT_CHARS:
        raise WebsiteExtractionError("Invalid source window bounds.")
    path = _web_snapshot_dir() / f"{digest}.json"
    try:
        if path.is_symlink() or path.stat().st_size > MAX_WEB_SNAPSHOT_CHARS * 6 + 10000:
            raise ValueError("Invalid snapshot file.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "status": "unavailable",
            "source_id": source_id,
            "selected_url": selected,
            "snapshot_sha256": digest,
            "content_complete": False,
            "limitation": "Exact local snapshot unavailable; no newer source was fetched.",
        }
    if (
        _web_snapshot_hash(payload) != digest
        or payload.get("source_id") != source_id
        or payload.get("selected_url") != selected
    ):
        raise WebsiteExtractionError("Source identity or snapshot integrity mismatch.")
    text = payload["text"]
    if not isinstance(text, str) or start_char > len(text):
        raise WebsiteExtractionError("Source window starts beyond the extracted snapshot.")
    end = min(len(text), start_char + max_chars)
    while len(json.dumps(text[start_char:end])) > MAX_SOURCE_EXCERPT_CHARS + 2:
        end = start_char + (end - start_char) // 2
    access = WebSourceAccess(
        source_id=source_id,
        selected_url=selected,
        resolved_url=payload["resolved_url"],
        snapshot_sha256=digest,
        total_chars=len(text),
        start_char=start_char,
        end_char=end,
        next_start_char=end if end < len(text) else None,
        content_complete=start_char == 0 and end == len(text),
    )
    result = {
        "status": "success",
        "text": text[start_char:end],
        "web_source_access": access.model_dump(mode="json"),
        "reached_end": end == len(text),
    }
    enforce_public_source_output_guardrails("read_web_source_window", result)
    return result


@function_tool(**keystone_tool_guardrail_kwargs())
def read_web_source_window(
    source_id: Annotated[str, Field(min_length=1, max_length=200)],
    selected_url: Annotated[str, Field(min_length=1, max_length=MAX_PUBLIC_URL_CHARS)],
    expected_snapshot_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    start_char: Annotated[int, Field(ge=0)] = 0,
    max_chars: Annotated[
        int, Field(ge=1, le=MAX_SOURCE_EXCERPT_CHARS)
    ] = DEFAULT_SOURCE_EXCERPT_CHARS,
) -> dict[str, Any]:
    """Read later evidence from an already extracted public page, without network.

    Copy source_id, selected_url and snapshot_sha256 from web_source_access;
    pass the latter as expected_snapshot_sha256. Start at next_start_char and
    repeat until reached_end. Adjacent windows concatenate exactly; read an
    overlapping range if a qualification or citation crosses a boundary.
    """

    return read_web_source_window_impl(
        source_id=source_id,
        selected_url=selected_url,
        expected_snapshot_sha256=expected_snapshot_sha256,
        start_char=start_char,
        max_chars=max_chars,
    )


def scoped_web_source_read_tool(accesses: Sequence[WebSourceAccess]) -> Any:
    """Bind a local continuation tool to only the snapshots in this model input."""

    allowed = frozenset(
        (access.source_id, access.selected_url, access.snapshot_sha256)
        for access in accesses if access.available
    )

    @function_tool(**keystone_tool_guardrail_kwargs())
    def read_web_source_window(
        source_id: Annotated[str, Field(min_length=1, max_length=200)],
        selected_url: Annotated[str, Field(min_length=1, max_length=MAX_PUBLIC_URL_CHARS)],
        expected_snapshot_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        start_char: Annotated[int, Field(ge=0)] = 0,
        max_chars: Annotated[
            int, Field(ge=1, le=MAX_SOURCE_EXCERPT_CHARS)
        ] = DEFAULT_SOURCE_EXCERPT_CHARS,
    ) -> dict[str, Any]:
        """Read a bounded local window from a web snapshot supplied in this run.

        Copy source_id, selected_url, snapshot_sha256 and next_start_char from
        web_source_access. No network, new URLs, or other saved sources are allowed.
        """
        if (source_id, selected_url, expected_snapshot_sha256) not in allowed:
            raise WebsiteExtractionError("Web source snapshot is outside this input scope.")
        return read_web_source_window_impl(
            source_id=source_id, selected_url=selected_url,
            expected_snapshot_sha256=expected_snapshot_sha256,
            start_char=start_char, max_chars=max_chars,
        )

    return read_web_source_window


@dataclass(frozen=True)
class WebsiteExtractionTool:
    """Explicitly live-gated website extractor."""

    live: bool = False
    provider: WebsiteExtractorProvider = "trafilatura"

    def extract(self, url: str, *, company_name: str) -> WebsiteExtractionResult:
        return extract_website_content(
            url,
            company_name=company_name,
            provider=self.provider,
            live=self.live,
        )


@dataclass
class WebsiteExtractionBudget:
    """Per-workflow cap for managed extraction calls."""

    firecrawl_max_calls: int = 0
    firecrawl_calls_attempted: int = 0

    def reserve(self, provider: WebsiteExtractorProvider) -> bool:
        if provider != "firecrawl":
            return True
        if self.firecrawl_calls_attempted >= self.firecrawl_max_calls:
            return False
        self.firecrawl_calls_attempted += 1
        return True


def extract_website_content(
    url: str,
    *,
    company_name: str,
    provider: str | None = None,
    live: bool = False,
    guardrail_context: WebsiteExtractionGuardrailContext = "default",
    http_get: Callable[..., Any] | None = None,
    http_post: Callable[..., Any] | None = None,
) -> WebsiteExtractionResult:
    """Extract readable page text with Trafilatura, Crawl4AI, or Firecrawl."""

    normalized_url = _normalize_url(url)
    resolved_provider = _normalize_provider(provider or load_settings().website_extractor)
    enforce_tool_input_guardrails(
        "website_extract_content",
        {
            "url": normalized_url,
            "company_name": company_name,
            "provider": resolved_provider,
            "live": live,
            "guardrail_context": guardrail_context,
        },
    )
    if not live:
        return _enforce_extraction_output_guardrails(
            WebsiteExtractionResult(
                url=normalized_url,
                provider=resolved_provider,
                status="dry-run",
                metadata={"guardrail_context": guardrail_context},
            ),
            guardrail_context=guardrail_context,
        )
    if resolved_provider == "firecrawl":
        return _extract_with_firecrawl(
            normalized_url,
            company_name=company_name,
            guardrail_context=guardrail_context,
            http_post=http_post,
        )
    if resolved_provider == "crawl4ai":
        return _extract_with_crawl4ai(
            normalized_url,
            company_name=company_name,
            guardrail_context=guardrail_context,
        )
    return _extract_with_trafilatura(
        normalized_url,
        company_name=company_name,
        guardrail_context=guardrail_context,
        http_get=http_get,
    )


def extract_website_content_with_fallbacks(
    url: str,
    *,
    company_name: str,
    primary_provider: str | None = None,
    fallback_providers: Sequence[str] | None = None,
    live: bool = False,
    guardrail_context: WebsiteExtractionGuardrailContext = "default",
    minimum_useful_chars: int = 3000,
    budget: WebsiteExtractionBudget | None = None,
    extractor: Callable[..., WebsiteExtractionResult] | None = None,
) -> WebsiteExtractionResult:
    """Extract one page through the shared static, local-rendered, and paid ladder.

    Trafilatura is the safe default. Crawl4AI and Firecrawl are attempted only
    when explicitly named in ``KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK`` or supplied
    by the caller; Crawl4AI remains gated to controlled experimental evaluations.
    """

    page_profile = website_extraction_page_profile(url)
    if page_profile == "structured_api_preferred":
        raise WebsiteExtractionError(
            f"Structured source API is preferred over rendered page extraction for {url}."
        )
    uses_default_extractor = extractor is None
    extract = extractor or extract_website_content
    providers = website_extraction_provider_sequence(
        primary_provider=primary_provider,
        fallback_providers=fallback_providers,
        url=url,
    )
    active_budget = budget or website_extraction_budget()
    attempts: list[dict[str, Any]] = []
    candidates: list[WebsiteExtractionResult] = []
    errors: list[tuple[str, str]] = []
    for provider in providers:
        if not active_budget.reserve(provider):
            message = "managed extraction budget exhausted"
            attempts.append({"provider": provider, "status": "budget_blocked", "error": message})
            errors.append((provider, message))
            continue
        try:
            result = extract(
                url,
                company_name=company_name,
                provider=provider,
                live=live,
                guardrail_context=guardrail_context,
            )
            result = _validate_extraction_result_urls(
                result,
                resolve_hostname=live and uses_default_extractor,
            )
        except Exception as exc:
            if type(exc).__name__ == "ToolGuardrailViolation":
                message = f"guardrail blocked extraction: {exc}"[:500]
            else:
                message = f"{type(exc).__name__}: {exc}"[:500]
            attempts.append({"provider": provider, "status": "error", "error": message})
            errors.append((provider, message))
            continue
        text_length = len(result.text_or_markdown.strip())
        attempts.append(
            {
                "provider": result.provider,
                "status": result.status,
                "text_length": text_length,
                "claim_count": len(result.claims),
            }
        )
        candidates.append(result)
        if _website_extraction_is_useful(result, minimum_useful_chars=minimum_useful_chars):
            return result.model_copy(
                update={
                    "metadata": {
                        **result.metadata,
                        "extraction_attempts": attempts,
                        "fallback_used": len(attempts) > 1,
                        "extraction_strategy": page_profile,
                    }
                }
            )

    if candidates:
        best = max(candidates, key=_website_extraction_candidate_score)
        return best.model_copy(
            update={
                "metadata": {
                    **best.metadata,
                    "extraction_attempts": attempts,
                    "fallback_used": len(attempts) > 1,
                    "quality_gate": "no provider met the useful-content threshold",
                    "extraction_strategy": page_profile,
                }
            }
        )
    if errors:
        primary_error = f"{errors[0][0]}: {errors[0][1]}"
        fallback_errors = [
            f"fallback {provider}: {_compact_fallback_error(message)}"
            for provider, message in errors[1:]
        ]
        raise WebsiteExtractionError(
            "; ".join([primary_error, *fallback_errors]),
            attempts=attempts,
        )
    raise WebsiteExtractionError(f"No website extraction provider returned content for {url}.")


def build_selected_url_source_bundle(
    *,
    company_name: str,
    selected_urls: Sequence[str],
    company_url: str | None = None,
    live_extraction: bool = False,
    primary_provider: str | None = None,
    fallback_providers: Sequence[str] | None = None,
    max_text_chars_per_source: int = DEFAULT_SOURCE_EXCERPT_CHARS,
    minimum_useful_chars: int = 3000,
    budget: WebsiteExtractionBudget | None = None,
    extractor: Callable[..., WebsiteExtractionResult] | None = None,
) -> SelectedUrlSourceBundleResult:
    """Fetch selected URLs through one shared ladder and return typed source evidence.

    This helper is the implementation boundary behind the SDK function tool. It
    deliberately accepts URLs selected by discovery rather than performing a
    second search, shares one managed-provider budget across the entire batch,
    and degrades one failed URL without discarding evidence from the others.
    """

    normalized_company = str(company_name or "").strip()
    if not normalized_company:
        raise WebsiteExtractionError("A non-empty company name is required.")
    if len(normalized_company) > 300:
        raise WebsiteExtractionError("Company name must be at most 300 characters.")
    normalized_urls = _normalize_selected_urls(selected_urls)
    if company_url:
        company_url = validate_public_http_url(company_url, resolve_hostname=False)
    if live_extraction and not website_extraction_enabled():
        raise WebsiteExtractionError(
            "Live selected-URL extraction requires "
            "KEYSTONE_ENABLE_WEBSITE_EXTRACTION=true."
        )

    excerpt_limit = max(200, min(
        MAX_SOURCE_EXCERPT_CHARS, int(max_text_chars_per_source),
        MAX_WEB_PREVIEW_TOTAL_CHARS // len(normalized_urls),
    ))
    active_budget = budget or website_extraction_budget()
    source_records = []
    diagnostics: list[SelectedUrlExtractionDiagnostic] = []
    for selected_url in normalized_urls:
        source_id = _selected_url_source_id(selected_url)
        try:
            selected_url = validate_public_http_url(
                selected_url,
                resolve_hostname=live_extraction and extractor is None,
            )
            result = extract_website_content_with_fallbacks(
                selected_url,
                company_name=normalized_company,
                primary_provider=primary_provider,
                fallback_providers=fallback_providers,
                live=live_extraction,
                guardrail_context="public_web_source",
                minimum_useful_chars=minimum_useful_chars,
                budget=active_budget,
                extractor=extractor if live_extraction else None,
            )
            result = _validate_extraction_result_urls(
                result,
                resolve_hostname=live_extraction and extractor is None,
            )
        except Exception as exc:
            attempts = _selected_url_attempt_diagnostics(getattr(exc, "attempts", None))
            diagnostics.append(
                SelectedUrlExtractionDiagnostic(
                    source_id=source_id,
                    selected_url=selected_url,
                    resolved_url=selected_url,
                    provider=attempts[-1].provider if attempts else "",
                    status="error",
                    extraction_strategy=website_extraction_page_profile(selected_url),
                    attempts=attempts,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
            )
            continue

        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        attempts = _selected_url_attempt_diagnostics(metadata.get("extraction_attempts"))
        extracted_text = str(result.text_or_markdown or "").strip()
        excerpt, access = project_web_source(
            result, source_id=source_id, selected_url=selected_url, max_chars=excerpt_limit,
        )
        source = normalize_source_record(
            {
                "source_id": source_id,
                "title": (result.title or selected_url)[:300],
                # Preserve the URL selected by discovery as the canonical evidence URL.
                "url": selected_url,
                "source_type": "website",
                "supported_claims": [claim for claim in result.claims if len(claim) <= 280][:10],
                "evidence_excerpt": excerpt,
                "web_source_access": access.model_dump(mode="json"),
            },
            company_name=normalized_company,
            company_url=company_url,
            default_source_id=source_id,
            default_source_type="website",
        )
        if source is not None:
            source_records.append(source)
        status = result.status
        error = ""
        if source is None and result.status != "dry-run":
            status = "no_source_claims"
            error = "Extraction returned no source-backed claim candidates."
        diagnostics.append(
            SelectedUrlExtractionDiagnostic(
                source_id=source_id,
                selected_url=selected_url,
                resolved_url=str(metadata.get("final_url") or result.url or selected_url),
                provider=result.provider,
                status=status,
                extraction_strategy=str(
                    metadata.get("extraction_strategy")
                    or website_extraction_page_profile(selected_url)
                ),
                included_in_bundle=source is not None,
                text_length=len(extracted_text),
                claim_count=len(result.claims),
                attempts=attempts,
                error=error,
            )
        )

    bundle = build_source_bundle(
        company_name=normalized_company,
        company_url=company_url,
        sources=source_records,
        max_sources=len(normalized_urls),
    )
    output = SelectedUrlSourceBundleResult(
        mode="live" if live_extraction else "dry_run",
        company_name=normalized_company,
        selected_url_count=len(normalized_urls),
        extracted_source_count=len(bundle.sources),
        source_bundle=bundle,
        diagnostics=diagnostics,
        firecrawl_max_calls=active_budget.firecrawl_max_calls,
        firecrawl_calls_attempted=active_budget.firecrawl_calls_attempted,
    )
    # Bound the entire serialized response, including long URLs and diagnostics.
    # Defer whole sources instead of silently deleting qualifications or links.
    while len(json.dumps(output.model_dump(mode="json"))) > MAX_WEB_BUNDLE_RESPONSE_CHARS:
        deferred = output.diagnostics.pop()
        output.deferred_selected_urls.insert(0, deferred.selected_url)
        source_records = [s for s in source_records if s.source_id != deferred.source_id]
        output.source_bundle = build_source_bundle(
            company_name=normalized_company, company_url=company_url, sources=source_records,
        )
        output.extracted_source_count = len(output.source_bundle.sources)
    return output


@function_tool(**keystone_tool_guardrail_kwargs())
def extract_selected_urls_to_source_bundle(
    company_name: Annotated[str, Field(min_length=1, max_length=300)],
    selected_urls: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=MAX_PUBLIC_URL_CHARS)]],
        Field(min_length=1, max_length=MAX_SELECTED_URLS_PER_BUNDLE),
    ],
    company_url: Annotated[str | None, Field(max_length=MAX_PUBLIC_URL_CHARS)] = None,
    live_extraction: bool = False,
    primary_provider: WebsiteExtractorProvider | None = None,
    fallback_providers: Annotated[
        list[WebsiteExtractorProvider] | None,
        Field(max_length=3),
    ] = None,
    max_text_chars_per_source: Annotated[
        int,
        Field(ge=200, le=MAX_SOURCE_EXCERPT_CHARS),
    ] = DEFAULT_SOURCE_EXCERPT_CHARS,
) -> dict[str, Any]:
    """Extract up to eight already-selected URLs into one source-attributed bundle.

    Call this after discovery has selected the pages worth reading. It uses the
    shared extraction ladder and one provider budget for the batch, returns
    per-URL diagnostics, and makes no network calls unless live extraction and
    the repository's explicit extraction gate are both enabled.
    """

    result = build_selected_url_source_bundle(
        company_name=company_name,
        selected_urls=selected_urls,
        company_url=company_url,
        live_extraction=live_extraction,
        primary_provider=primary_provider,
        fallback_providers=fallback_providers,
        max_text_chars_per_source=max_text_chars_per_source,
    )
    return result.model_dump(mode="json")


def selected_url_extraction_tools(selected_urls: Sequence[str]) -> tuple[Any, Any]:
    """Bind an exact-source tool pair to selected URLs and handles returned in this run.

    Empty or invalid authoritative targets leave a closed scope. The local
    reader cannot promote a handle learned from a previous request into it.
    """

    try:
        allowed_urls = frozenset(_normalize_selected_urls(selected_urls))
    except WebsiteExtractionError:
        allowed_urls = frozenset()
    returned_handles: set[tuple[str, str, str]] = set()
    extraction_signature = inspect.signature(extract_selected_urls_to_source_bundle)
    read_signature = inspect.signature(read_web_source_window)

    def payload_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        return value if isinstance(value, dict) else {}

    def enroll_returned_handles(result: dict[str, Any]) -> None:
        bundle = result.get("source_bundle") or {}
        for source in bundle.get("sources") or []:
            raw_access = source.get("web_source_access")
            if not raw_access:
                continue
            access = WebSourceAccess.model_validate(raw_access)
            if access.available and access.selected_url in allowed_urls:
                returned_handles.add((
                    access.source_id, access.selected_url, access.snapshot_sha256,
                ))

    @tool_input_guardrail(name="keystone_selected_web_source_scope")
    def scope_guardrail(data: Any) -> ToolGuardrailFunctionOutput:
        # SDK input guardrails precede invocation wrappers, including durable
        # receipt replay, so cached output cannot bypass source admission.
        # Nested Agent.as_tool contexts can retain the parent's tool_input.
        # tool_arguments belongs to this actual child tool invocation.
        value = getattr(data.context, "tool_arguments", None)
        if value is None:
            value = getattr(data.context, "tool_input", "")
        payload = payload_dict(value)
        if getattr(data.context, "tool_name", "") == "extract_selected_urls_to_source_bundle":
            try:
                requested = _normalize_selected_urls(payload.get("selected_urls") or [])
                allowed = bool(allowed_urls) and set(requested).issubset(allowed_urls)
            except WebsiteExtractionError:
                allowed = False
        else:
            allowed = (
                payload.get("source_id"), payload.get("selected_url"),
                payload.get("expected_snapshot_sha256"),
            ) in returned_handles
        if allowed:
            return ToolGuardrailFunctionOutput.allow({"selected_source_scope_verified": True})
        return ToolGuardrailFunctionOutput.reject_content(
            "Web source is outside the current request scope.",
            {"selected_source_scope_verified": False},
        )

    @tool_output_guardrail(name="keystone_selected_web_returned_handles")
    def returned_handle_guardrail(data: Any) -> ToolGuardrailFunctionOutput:
        # A successful cached extraction is also evidence returned in this run.
        enroll_returned_handles(payload_dict(data.output))
        return ToolGuardrailFunctionOutput.allow({"returned_handles_recorded": True})

    @wraps(extract_selected_urls_to_source_bundle)
    def extract_in_scope(*args: Any, **kwargs: Any) -> dict[str, Any]:
        parameters = extraction_signature.bind(*args, **kwargs)
        requested_urls = _normalize_selected_urls(parameters.arguments["selected_urls"])
        if not allowed_urls or not set(requested_urls).issubset(allowed_urls):
            raise WebsiteExtractionError("Selected URLs are outside the current request scope.")
        result = extract_selected_urls_to_source_bundle(*args, **kwargs)
        enroll_returned_handles(result)
        return result

    @wraps(read_web_source_window)
    def read_in_scope(*args: Any, **kwargs: Any) -> dict[str, Any]:
        parameters = read_signature.bind(*args, **kwargs).arguments
        identity = (
            parameters["source_id"], parameters["selected_url"],
            parameters["expected_snapshot_sha256"],
        )
        if identity not in returned_handles:
            raise WebsiteExtractionError("Web snapshot is outside the current request scope.")
        return read_web_source_window(*args, **kwargs)

    extraction_guards = keystone_tool_guardrail_kwargs()
    extraction_guards["tool_input_guardrails"].append(scope_guardrail)
    extraction_guards["tool_output_guardrails"].append(returned_handle_guardrail)
    read_guards = keystone_tool_guardrail_kwargs()
    read_guards["tool_input_guardrails"].append(scope_guardrail)
    return (
        function_tool(**extraction_guards)(extract_in_scope),
        function_tool(**read_guards)(read_in_scope),
    )


def _normalize_selected_urls(selected_urls: Sequence[str]) -> list[str]:
    urls = list(selected_urls or ())
    if not urls:
        raise WebsiteExtractionError("At least one selected URL is required.")
    if len(urls) > MAX_SELECTED_URLS_PER_BUNDLE:
        raise WebsiteExtractionError(
            f"Selected URL extraction is capped at {MAX_SELECTED_URLS_PER_BUNDLE} URLs per call."
        )
    normalized = list(
        dict.fromkeys(validate_public_http_url(url, resolve_hostname=False) for url in urls)
    )
    if not normalized:
        raise WebsiteExtractionError("At least one selected URL is required.")
    if len(json.dumps(normalized)) > MAX_WEB_BUNDLE_RESPONSE_CHARS // 2:
        raise WebsiteExtractionError(
            "Selected URL identifiers exceed the response budget; split into smaller batches."
        )
    return normalized


def _selected_url_source_id(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"selected-url:{digest}"


def _selected_url_attempt_diagnostics(value: Any) -> list[WebsiteExtractionAttemptDiagnostic]:
    if not isinstance(value, list):
        return []
    diagnostics: list[WebsiteExtractionAttemptDiagnostic] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        diagnostics.append(
            WebsiteExtractionAttemptDiagnostic(
                provider=str(row.get("provider") or ""),
                status=str(row.get("status") or "unknown"),
                text_length=max(0, int(row.get("text_length") or 0)),
                claim_count=max(0, int(row.get("claim_count") or 0)),
                error=str(row.get("error") or "")[:500],
            )
        )
    return diagnostics


def validate_public_http_url(
    url: str,
    *,
    resolve_hostname: bool,
    resolver: Callable[..., Any] | None = None,
) -> str:
    """Validate one public HTTP(S) URL before a live extraction provider sees it."""

    normalized = _normalize_url(url)
    if len(normalized) > MAX_PUBLIC_URL_CHARS:
        raise WebsiteExtractionError(
            f"Website extraction URLs must be at most {MAX_PUBLIC_URL_CHARS} characters."
        )
    parsed = urlparse(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise WebsiteExtractionError("Website extraction URLs must not contain credentials.")
    hostname = str(parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        raise WebsiteExtractionError("Website extraction requires a hostname.")
    if hostname == "localhost" or hostname.endswith(_LOCAL_HOSTNAME_SUFFIXES):
        raise WebsiteExtractionError("Website extraction requires a public hostname.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise WebsiteExtractionError("Website extraction URL has an invalid port.") from exc

    address = _ip_address_or_none(hostname)
    if address is not None:
        _require_global_ip_address(address)
        return normalized
    if not resolve_hostname:
        return normalized

    lookup = resolver or socket.getaddrinfo
    try:
        answers = lookup(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebsiteExtractionError(
            f"Public hostname resolution failed for {hostname}."
        ) from exc
    resolved_addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for answer in answers or ():
        try:
            sockaddr = answer[4]
            raw_address = str(sockaddr[0]).split("%", 1)[0]
            resolved_addresses.add(ipaddress.ip_address(raw_address))
        except (IndexError, TypeError, ValueError):
            continue
    if not resolved_addresses:
        raise WebsiteExtractionError(
            f"Public hostname resolution returned no usable addresses for {hostname}."
        )
    for resolved_address in resolved_addresses:
        _require_global_ip_address(resolved_address)
    return normalized


def _ip_address_or_none(
    hostname: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return None


def _require_global_ip_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    if (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        raise WebsiteExtractionError(
            "Website extraction blocks loopback, private, link-local, reserved, "
            "unspecified, and multicast addresses."
        )


def _validate_extraction_result_urls(
    result: WebsiteExtractionResult,
    *,
    resolve_hostname: bool,
) -> WebsiteExtractionResult:
    validated_url = validate_public_http_url(
        result.url,
        resolve_hostname=resolve_hostname,
    )
    metadata = dict(result.metadata) if isinstance(result.metadata, dict) else {}
    raw_final_url = str(
        metadata.get("final_url")
        or metadata.get("sourceURL")
        or metadata.get("sourceUrl")
        or metadata.get("source_url")
        or metadata.get("url")
        or ""
    ).strip()
    if raw_final_url:
        metadata["final_url"] = validate_public_http_url(
            raw_final_url,
            resolve_hostname=resolve_hostname,
        )
    return result.model_copy(update={"url": validated_url, "metadata": metadata})


def website_extraction_provider_sequence(
    *,
    primary_provider: str | None = None,
    fallback_providers: Sequence[str] | None = None,
    url: str = "",
) -> tuple[WebsiteExtractorProvider, ...]:
    """Resolve a deduplicated provider ladder without silently enabling paid calls."""

    primary = _normalize_provider(primary_provider or load_settings().website_extractor)
    if fallback_providers is None:
        configured = os.getenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", "").strip()
        raw_fallbacks = [item for item in configured.split(",") if item.strip()]
    else:
        raw_fallbacks = list(fallback_providers)
    normalized = [primary, *(_normalize_provider(item) for item in raw_fallbacks)]
    if website_extraction_page_profile(url) == "rendered_first" and "crawl4ai" in normalized:
        normalized = ["crawl4ai", *(item for item in normalized if item != "crawl4ai")]
    return tuple(dict.fromkeys(normalized))


def website_extraction_page_profile(url: str) -> str:
    """Classify selected URLs for deterministic extraction routing."""

    parsed = urlparse(str(url or ""))
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host == "clinicaltrials.gov" and path.startswith("/search"):
        return "structured_api_preferred"
    rendered_hosts = {
        "sam.gov",
        "reporter.nih.gov",
        "boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
    }
    rendered_path_markers = (
        "/search",
        "/careers",
        "/jobs",
        "/job/",
        "/funding/searchguide",
    )
    if host in rendered_hosts or any(marker in path for marker in rendered_path_markers):
        return "rendered_first"
    return "static_first"


def website_extraction_budget() -> WebsiteExtractionBudget:
    """Build the explicit per-workflow managed extraction budget."""

    raw = os.getenv("KEYSTONE_FIRECRAWL_EXTRACTION_MAX_CALLS_PER_RUN", "0").strip()
    try:
        maximum = int(raw)
    except ValueError:
        maximum = 0
    return WebsiteExtractionBudget(firecrawl_max_calls=max(0, min(8, maximum)))


def _compact_fallback_error(message: str) -> str:
    if message.startswith("guardrail blocked extraction:"):
        return message
    return message.split(": ", 1)[-1]


def _website_extraction_is_useful(
    result: WebsiteExtractionResult,
    *,
    minimum_useful_chars: int,
) -> bool:
    text = result.text_or_markdown.strip()
    return (
        result.status == "success"
        and bool(result.claims)
        and len(text) >= max(200, int(minimum_useful_chars))
    )


def _website_extraction_candidate_score(result: WebsiteExtractionResult) -> tuple[int, int, int]:
    return (
        int(result.status == "success"),
        len(result.claims),
        len(result.text_or_markdown.strip()),
    )


def extract_website_content_from_html(
    html: str,
    *,
    url: str,
    company_name: str,
) -> WebsiteExtractionResult:
    """Extract readable text from already-fetched HTML for tests and local adapters."""

    normalized_url = _normalize_url(url)
    text = _trafilatura_extract(html, url=normalized_url) or extract_clean_text(html)
    title = _metadata_title(html)
    claims = extract_claim_candidates(text, company_name=company_name, max_claims=10)
    return WebsiteExtractionResult(
        url=normalized_url,
        title=title,
        provider="trafilatura",
        status="success" if text else "empty",
        text_or_markdown=text,
        claims=claims,
        metadata={"title": title} if title else {},
    )


def _extract_with_trafilatura(
    url: str,
    *,
    company_name: str,
    guardrail_context: WebsiteExtractionGuardrailContext,
    http_get: Callable[..., Any] | None,
) -> WebsiteExtractionResult:
    get = http_get or requests.get
    resolve_hostname = http_get is None
    try:
        response, final_url = _get_public_url_without_unsafe_redirects(
            get,
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 KeystoneBusinessAgents/0.1 (company research; contact: operator)"
                )
            },
            timeout=15,
            resolve_hostname=resolve_hostname,
        )
    except requests.RequestException as exc:
        raise WebsiteExtractionError(f"Website extraction request failed for {url}.") from exc
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 400:
        raise WebsiteExtractionError(
            f"Website extraction failed for {url} with HTTP {status_code}."
        )
    html = str(getattr(response, "text", "") or "")
    result = extract_website_content_from_html(html, url=final_url, company_name=company_name)
    return _enforce_extraction_output_guardrails(
        result.model_copy(
            update={
                "metadata": {
                    **result.metadata,
                    "status_code": status_code,
                    "final_url": final_url,
                    "guardrail_context": guardrail_context,
                }
            }
        ),
        guardrail_context=guardrail_context,
    )


def _extract_with_firecrawl(
    url: str,
    *,
    company_name: str,
    guardrail_context: WebsiteExtractionGuardrailContext,
    http_post: Callable[..., Any] | None,
) -> WebsiteExtractionResult:
    settings = load_settings()
    if not settings.firecrawl_api_key:
        raise WebsiteExtractionError(
            "FIRECRAWL_API_KEY is required when KEYSTONE_WEBSITE_EXTRACTOR=firecrawl."
        )
    validate_public_http_url(url, resolve_hostname=http_post is None)
    post = http_post or requests.post
    endpoint = f"{settings.firecrawl_base_url.rstrip('/')}/v2/scrape"
    try:
        response = post(
            endpoint,
            headers={
                "Authorization": f"Bearer {settings.firecrawl_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "removeBase64Images": True,
                "blockAds": True,
                "timeout": 30000,
            },
            timeout=35,
        )
    except requests.RequestException as exc:
        raise WebsiteExtractionError(f"Firecrawl extraction request failed for {url}.") from exc
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 400:
        raise WebsiteExtractionError(
            f"Firecrawl extraction failed for {url} with HTTP {status_code}."
        )
    data = response.json()
    payload = data.get("data") if isinstance(data, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    text = str(payload.get("markdown") or payload.get("summary") or "").strip()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    raw_final_url = str(
        metadata.get("final_url")
        or metadata.get("sourceURL")
        or metadata.get("sourceUrl")
        or metadata.get("source_url")
        or metadata.get("url")
        or ""
    ).strip()
    if raw_final_url:
        metadata = {
            **metadata,
            "final_url": validate_public_http_url(
                raw_final_url,
                resolve_hostname=http_post is None,
            ),
        }
    title = str(metadata.get("title") or "").strip()
    claims = extract_claim_candidates(text, company_name=company_name, max_claims=10)
    return _enforce_extraction_output_guardrails(
        WebsiteExtractionResult(
            url=url,
            title=title,
            provider="firecrawl",
            status="success" if text else "empty",
            text_or_markdown=text,
            claims=claims,
            metadata={
                **metadata,
                "status_code": status_code,
                "guardrail_context": guardrail_context,
            },
        ),
        guardrail_context=guardrail_context,
    )


def _extract_with_crawl4ai(
    url: str,
    *,
    company_name: str,
    guardrail_context: WebsiteExtractionGuardrailContext,
) -> WebsiteExtractionResult:
    if not parse_bool(os.getenv(CRAWL4AI_EXPERIMENTAL_LIVE_ENV)):
        raise WebsiteExtractionError(
            "Crawl4AI live extraction is experimental and blocked by default because "
            "browser redirects are not yet intercepted before navigation. Use "
            f"{CRAWL4AI_EXPERIMENTAL_LIVE_ENV}=true only for explicit controlled evals; "
            "production selected-URL extraction should use Trafilatura or Firecrawl."
        )
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    except ImportError as exc:
        raise WebsiteExtractionError(
            "Install crawl4ai to use Crawl4AI website extraction."
        ) from exc

    validate_public_http_url(url, resolve_hostname=True)

    async def _crawl() -> Any:
        browser_config = BrowserConfig(
            browser_type="chromium",
            headless=True,
            text_mode=True,
        )
        run_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            excluded_tags=["script", "style", "nav", "footer"],
            remove_forms=True,
            word_count_threshold=10,
            page_timeout=30000,
        )
        async with AsyncWebCrawler(config=browser_config) as crawler:
            return await crawler.arun(url=url, config=run_config)

    try:
        result = asyncio.run(_crawl())
    except RuntimeError as exc:
        raise WebsiteExtractionError(f"Crawl4AI extraction failed for {url}.") from exc
    except Exception as exc:
        raise WebsiteExtractionError(f"Crawl4AI extraction failed for {url}.") from exc

    success = bool(getattr(result, "success", True))
    if not success:
        message = str(getattr(result, "error_message", "") or "").strip()
        raise WebsiteExtractionError(
            f"Crawl4AI extraction failed for {url}" + (f": {message}" if message else ".")
        )
    markdown = getattr(result, "markdown", None)
    text = str(
        getattr(markdown, "fit_markdown", "")
        or getattr(markdown, "raw_markdown", "")
        or getattr(result, "cleaned_html", "")
        or getattr(result, "html", "")
        or ""
    ).strip()
    if text.startswith("<"):
        text = extract_clean_text(text)
    final_url = validate_public_http_url(
        str(getattr(result, "url", "") or url),
        resolve_hostname=True,
    )
    title = _crawl4ai_title(result)
    claims = extract_claim_candidates(text, company_name=company_name, max_claims=10)
    status_code = int(getattr(result, "status_code", 0) or 0)
    return _enforce_extraction_output_guardrails(
        WebsiteExtractionResult(
            url=final_url,
            title=title,
            provider="crawl4ai",
            status="success" if text else "empty",
            text_or_markdown=text,
            claims=claims,
            metadata={
                **({"status_code": status_code} if status_code else {}),
                **(
                    {"final_url": final_url}
                    if getattr(result, "url", "")
                    else {}
                ),
                "guardrail_context": guardrail_context,
            },
        ),
        guardrail_context=guardrail_context,
    )


def _get_public_url_without_unsafe_redirects(
    get: Callable[..., Any],
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    resolve_hostname: bool,
) -> tuple[Any, str]:
    """Follow a small redirect chain only after validating every next target."""

    current_url = validate_public_http_url(url, resolve_hostname=resolve_hostname)
    for _redirect_count in range(MAX_PUBLIC_REDIRECTS + 1):
        response = get(
            current_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        response_url = validate_public_http_url(
            str(getattr(response, "url", "") or current_url),
            resolve_hostname=resolve_hostname,
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code not in {301, 302, 303, 307, 308}:
            return response, response_url
        response_headers = getattr(response, "headers", {})
        location = str(
            response_headers.get("Location", "")
            if isinstance(response_headers, Mapping)
            else ""
        ).strip()
        if not location:
            raise WebsiteExtractionError("Website redirect did not provide a Location header.")
        current_url = validate_public_http_url(
            urljoin(response_url, location),
            resolve_hostname=resolve_hostname,
        )
    raise WebsiteExtractionError(
        f"Website extraction exceeded the {MAX_PUBLIC_REDIRECTS}-redirect limit."
    )


def _enforce_extraction_output_guardrails(
    result: WebsiteExtractionResult,
    *,
    guardrail_context: WebsiteExtractionGuardrailContext,
) -> WebsiteExtractionResult:
    if guardrail_context in {"public_opportunity_source", "public_web_source"}:
        return enforce_public_source_output_guardrails("website_extract_content", result)
    return enforce_tool_output_guardrails("website_extract_content", result)


def _crawl4ai_title(result: Any) -> str:
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        title = str(metadata.get("title") or "").strip()
        if title:
            return title
    html = str(getattr(result, "html", "") or getattr(result, "cleaned_html", "") or "")
    return _metadata_title(html) if html else ""


def default_company_page_urls(company_url: str) -> list[str]:
    """Return a small, polite set of likely high-value company pages."""

    origin = _url_origin(_normalize_url(company_url))
    paths = ("", "/about", "/about-us", "/solutions", "/partners", "/newsroom", "/contact")
    return list(dict.fromkeys(urljoin(origin, path) for path in paths))


def discover_company_page_urls(
    company_url: str,
    *,
    company_name: str = "",
    live: bool = False,
    max_urls: int = 6,
    http_get: Callable[..., Any] | None = None,
) -> list[str]:
    """Discover high-value internal company pages from the homepage."""

    origin = _url_origin(_normalize_url(company_url))
    if not live:
        return default_company_page_urls(origin)[:max_urls]
    get = http_get or requests.get
    try:
        response = get(
            origin,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 KeystoneBusinessAgents/0.1 (page discovery; contact: operator)"
                )
            },
            timeout=15,
        )
    except Exception:
        return default_company_page_urls(origin)[:max_urls]
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        return default_company_page_urls(origin)[:max_urls]
    html = str(getattr(response, "text", "") or "")
    candidates = [origin, *_rank_internal_links(origin, html, company_name=company_name)]
    candidates.extend(default_company_page_urls(origin))
    return list(dict.fromkeys(candidates))[:max_urls]


def website_extraction_enabled() -> bool:
    raw = os.getenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION")
    if raw is None:
        return False
    return parse_bool(raw)


def _normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        raise WebsiteExtractionError("A non-empty URL is required for website extraction.")
    if text.startswith("www."):
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebsiteExtractionError("Website extraction requires an http(s) URL.")
    return text


def _normalize_provider(value: str) -> WebsiteExtractorProvider:
    provider = str(value or "trafilatura").strip().lower()
    if provider in {"local", "trafilatura"}:
        return "trafilatura"
    if provider in {"crawl4ai", "crawl-4-ai", "crawl_4_ai"}:
        return "crawl4ai"
    if provider in {"firecrawl", "firecrawl-cloud"}:
        return "firecrawl"
    raise WebsiteExtractionError(
        "KEYSTONE_WEBSITE_EXTRACTOR must be 'trafilatura', 'crawl4ai', or 'firecrawl'."
    )


def _trafilatura_extract(html: str, *, url: str) -> str:
    try:
        import trafilatura
    except ImportError as exc:  # pragma: no cover - dependency is declared.
        raise WebsiteExtractionError(
            "Install trafilatura to use local website extraction."
        ) from exc
    extracted = trafilatura.extract(
        _preserve_table_inline_content(html, url=url),
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        favor_recall=True,
    )
    return str(extracted or "").strip()


def _preserve_table_inline_content(html: str, *, url: str) -> str:
    """Keep ordered cell content through Trafilatura's nested-table traversal.

    Its installed table path can consume inline descendants before their text
    and tails reach the output. Normalize only leaf cells to literal Markdown;
    leave table structure and the rest of the document to the existing parser.
    """

    from lxml import etree
    from lxml import html as lxml_html

    if not re.search(r"<t[dh]\b", html, re.IGNORECASE):
        return html
    try:
        tree = lxml_html.fromstring(html)
    except (etree.ParserError, ValueError):
        return html

    def inline_content(element: Any) -> str:
        tag = str(element.tag).lower()
        if tag in {"script", "style", "noscript"} or not isinstance(element.tag, str):
            return ""
        pieces = [element.text or ""]
        for child in element:
            pieces.extend((inline_content(child), child.tail or ""))
        text = "".join(pieces)
        if tag in {"del", "s", "strike"}:
            return f"~~{text}~~" if text.strip() else text
        if tag == "a" and element.get("href") and text.strip():
            return f"[{text}]({urljoin(url, element.get('href'))})"
        if tag == "code" and text.strip() and not element.xpath(".//a[@href]"):
            fence = "``" if "`" in text else "`"
            return f"{fence}{text}{fence}"
        if tag in {"strong", "b"} and text.strip():
            return f"**{text}**"
        if tag in {"em", "i"} and text.strip():
            return f"*{text}*"
        if tag in {"sub", "sup"} and text.strip():
            marker = "_" if tag == "sub" else "^"
            return f"{marker}{{{text}}}"
        if tag in {"p", "div", "br", "li", "ul", "ol"}:
            return f" {text} "
        return text

    changed = False
    for cell in tree.xpath("//table//td | //table//th"):
        if not len(cell) or cell.find(".//table") is not None:
            continue
        text = re.sub(r"\s+", " ", inline_content(cell)).strip().replace("|", r"\|")
        for child in list(cell):
            cell.remove(child)
        cell.text = text
        changed = True
    return lxml_html.tostring(tree, encoding="unicode") if changed else html


def _metadata_title(html: str) -> str:
    try:
        import trafilatura

        metadata = trafilatura.extract_metadata(html)
    except Exception:
        return ""
    return str(getattr(metadata, "title", "") or "").strip()


def _url_origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _rank_internal_links(origin: str, html: str, *, company_name: str) -> list[str]:
    origin_host = urlparse(origin).netloc.lower()
    hrefs = re.findall(r"""href=["']([^"'#]+)["']""", html, flags=re.I)
    scored: list[tuple[int, str]] = []
    company_token = company_name.lower().split()[0] if company_name else ""
    path_weights = {
        "about": 100,
        "team": 95,
        "leadership": 95,
        "contact": 90,
        "partners": 85,
        "solutions": 80,
        "customers": 78,
        "news": 75,
        "press": 75,
        "research": 70,
        "clinical": 70,
        "science": 70,
        "careers": 55,
    }
    for href in hrefs:
        url = urljoin(origin, href.strip())
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != origin_host:
            continue
        lowered = parsed.path.lower()
        score = max(
            (weight for marker, weight in path_weights.items() if marker in lowered),
            default=0,
        )
        if company_token and company_token in url.lower():
            score += 5
        if score:
            scored.append((score, url))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [url for _score, url in scored]
