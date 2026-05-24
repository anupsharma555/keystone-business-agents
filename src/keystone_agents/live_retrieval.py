"""Reusable live retrieval helpers for search-heavy specialist routes."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from keystone_agents.agents.business_research_analyst import (
    build_company_research_queries,
    research_account_from_search_results,
)
from keystone_agents.agents.opportunity_scout import scout_opportunities_live_search
from keystone_agents.config import load_settings
from keystone_agents.retrieval_policy import (
    HybridSearchProvider,
    ProviderRequestBudget,
    assess_company_search_quality,
    assess_opportunity_search_quality,
    build_provider_sequence,
    derive_request_autonomy_hint,
    provider_value_summary,
)
from keystone_agents.sandboxing import (
    SandboxAgentsUnavailable,
    SandboxHostedWebSearchConfig,
    SandboxSearchReviewSpec,
    run_sandbox_search_review,
    sandbox_agents_available,
)
from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.opportunity_search_plan import OpportunitySearchPlan
from keystone_agents.schemas.retrieval import RetrievalHint
from keystone_agents.tools.html_review_tool import (
    HtmlReviewError,
    agent_html_review_enabled,
    agent_html_review_max_pages,
    agent_html_review_min_claims,
    run_agent_html_review,
)
from keystone_agents.tools.serper_tool import build_search_provider
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    default_company_page_urls,
    discover_company_page_urls,
    extract_website_content,
    website_extraction_enabled,
)

DEFAULT_SANDBOX_SEARCH_REVIEW_CONTEXT_SIZE = "low"
DEFAULT_SANDBOX_SEARCH_REVIEW_SOURCE_LIMIT = 8
MAX_SANDBOX_SEARCH_REVIEW_SOURCE_LIMIT = 12
DEFAULT_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN = 2
DEFAULT_SEARXNG_TRANSIENT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class SharedSearchProviderConfig:
    """Resolved live-search provider layout for Keystone research agents."""

    provider_sequence: tuple[str, ...]
    deepening_provider_sequence: tuple[str, ...]
    provider_request_budget: ProviderRequestBudget | None
    parallel_provider_fanout: bool


def search_provider_label(metadata: dict[str, Any]) -> str:
    """Return a compact label for the providers actually used."""

    providers_used = [str(item) for item in (metadata.get("search_providers_used") or []) if item]
    if len(providers_used) > 1:
        return "+".join(providers_used)
    if providers_used:
        return providers_used[0]
    return str(metadata.get("primary_search_provider") or metadata.get("search_provider") or "")


def retrieval_diagnostics_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return Slack/CLI-safe structured diagnostics from retrieval metadata."""

    providers_used = [
        str(item).strip()
        for item in (metadata.get("search_providers_used") or [])
        if str(item).strip()
    ]
    provider_summary = search_provider_label(dict(metadata))
    if provider_summary and provider_summary not in providers_used:
        providers_used = [*providers_used, *_split_provider_summary(provider_summary)]

    quality = metadata.get("search_quality") if isinstance(metadata.get("search_quality"), dict) else {}
    source_coverage = (
        metadata.get("source_coverage")
        if isinstance(metadata.get("source_coverage"), dict)
        else quality.get("source_coverage")
        if isinstance(quality.get("source_coverage"), dict)
        else {}
    )
    website = (
        metadata.get("website_extraction")
        if isinstance(metadata.get("website_extraction"), dict)
        else {}
    )
    timing = metadata.get("timing") if isinstance(metadata.get("timing"), dict) else {}
    provider_usage = (
        metadata.get("provider_usage")
        if isinstance(metadata.get("provider_usage"), dict)
        else {}
    )
    diagnostics = {
        "mode": str(metadata.get("mode") or ""),
        "live_search": bool(metadata.get("live_search")),
        "provider_summary": provider_summary,
        "providers_used": list(dict.fromkeys(providers_used)),
        "provider_usage": _compact_provider_usage(provider_usage),
        "retrieval_ladder": _compact_retrieval_ladder(metadata.get("retrieval_ladder")),
        "search_quality_summary": _compact_quality_summary(quality),
        "source_coverage_summary": _compact_source_coverage(source_coverage),
        "fallback_used": bool(
            metadata.get("search_provider_fallback_used")
            or metadata.get("provider_error_fallback_used")
            or metadata.get("deepening_search_used")
        ),
        "precision_escalated": bool(metadata.get("precision_search_escalated")),
        "hosted_web_search_lane_used": "agents-web-search" in providers_used,
        "review_recommended": bool(
            metadata.get("search_review_recommended")
            or quality.get("needs_search_review")
        ),
        "website_extraction_summary": _compact_website_summary(website),
        "errors": _compact_retrieval_errors(metadata, website),
        "timing": _compact_timing(timing),
    }
    searxng = metadata.get("searxng_transient_runtime")
    if isinstance(searxng, dict):
        diagnostics["transient_searxng"] = {
            "enabled": bool(searxng.get("enabled")),
            "started": bool(searxng.get("started")),
            "stopped": bool(searxng.get("stopped")),
            "reason": str(searxng.get("reason") or ""),
        }
    return diagnostics


def _split_provider_summary(value: str) -> list[str]:
    return [item.strip() for item in value.replace("+", ",").split(",") if item.strip()]


def _compact_provider_usage(provider_usage: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    compact: dict[str, dict[str, Any]] = {}
    for name, usage in provider_usage.items():
        if not isinstance(usage, Mapping):
            continue
        compact[str(name)] = {
            "requests_attempted": int(_safe_float(usage.get("requests_attempted"))),
            "requests_succeeded": int(_safe_float(usage.get("requests_succeeded"))),
            "requests_failed": int(_safe_float(usage.get("requests_failed"))),
            "total_seconds": round(_safe_float(usage.get("total_seconds")), 3),
        }
    return compact


def _compact_retrieval_ladder(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    ladder: list[dict[str, Any]] = []
    for rung in value[:6]:
        if not isinstance(rung, Mapping):
            continue
        ladder.append(
            {
                "rung": str(rung.get("rung") or "retrieval"),
                "providers": [str(item) for item in (rung.get("providers") or []) if item],
                "raw_result_count": int(_safe_float(rung.get("raw_result_count"))),
                "queries": int(_safe_float(rung.get("queries"))),
                "pages_considered": int(_safe_float(rung.get("pages_considered"))),
                "pages_extracted": int(_safe_float(rung.get("pages_extracted"))),
                "claim_count": int(_safe_float(rung.get("claim_count"))),
                "seconds": round(_safe_float(rung.get("seconds")), 3),
                "useful": bool(rung.get("useful")),
            }
        )
    return ladder


def _compact_quality_summary(quality: Mapping[str, Any]) -> dict[str, Any]:
    if not quality:
        return {}
    reasons = quality.get("reasons")
    reason_values = reasons if isinstance(reasons, list) else []
    return {
        "sufficient": quality.get("sufficient"),
        "official_source_present": quality.get("official_source_present"),
        "needs_search_review": bool(quality.get("needs_search_review")),
        "reasons": [str(item) for item in reason_values[:5]],
    }


def _compact_source_coverage(source_coverage: Mapping[str, Any]) -> dict[str, Any]:
    if not source_coverage:
        return {}
    return {
        "selected_source_count": int(_safe_float(source_coverage.get("selected_source_count"))),
        "credible_source_count": int(_safe_float(source_coverage.get("credible_source_count"))),
        "official_source_count": int(_safe_float(source_coverage.get("official_source_count"))),
        "sufficiency_status": str(source_coverage.get("sufficiency_status") or ""),
    }


def _compact_website_summary(website: Mapping[str, Any]) -> dict[str, Any]:
    if not website:
        return {}
    return {
        "enabled": bool(website.get("enabled")),
        "provider": str(website.get("provider") or ""),
        "fallback_provider": str(website.get("fallback_provider") or ""),
        "pages_considered": int(_safe_float(website.get("pages_considered"))),
        "pages_extracted": int(_safe_float(website.get("page_count"))),
        "claim_count": int(_safe_float(website.get("claim_count"))),
        "extraction_failures": len(website.get("errors") or []),
        "agent_html_review_page_count": int(
            _safe_float(website.get("agent_html_review_page_count"))
        ),
        "agent_html_review_claim_count": int(
            _safe_float(website.get("agent_html_review_claim_count"))
        ),
    }


def _compact_retrieval_errors(
    metadata: Mapping[str, Any],
    website: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    for key in ("errors", "warnings"):
        value = metadata.get(key)
        if isinstance(value, list):
            errors.extend(str(item) for item in value if str(item).strip())
    website_errors = website.get("errors")
    if isinstance(website_errors, list):
        errors.extend(str(item) for item in website_errors if str(item).strip())
    return errors[:8]


def _compact_timing(timing: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: round(_safe_float(timing.get(key)), 3)
        for key in (
            "total_seconds",
            "search_seconds",
            "quality_assessment_seconds",
            "website_extraction_seconds",
            "profile_build_seconds",
        )
        if timing.get(key) is not None
    }


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _slugify(value: str, *, fallback: str) -> str:
    cleaned = "".join(
        character.lower() if character.isalnum() else "-" for character in value.strip()
    )
    collapsed = "-".join(part for part in cleaned.split("-") if part)
    return collapsed[:80] or fallback


def _jsonable_search_result(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        mapping = result.model_dump(mode="json")
    elif isinstance(result, dict):
        mapping = dict(result)
    else:
        mapping = {
            "title": getattr(result, "title", ""),
            "url": getattr(result, "url", "") or getattr(result, "link", ""),
            "snippet": getattr(result, "snippet", ""),
            "source": getattr(result, "source", ""),
            "source_type": getattr(result, "source_type", ""),
        }
    return {
        "title": str(mapping.get("title") or ""),
        "url": str(mapping.get("url") or mapping.get("link") or ""),
        "snippet": str(mapping.get("snippet") or ""),
        "source": str(mapping.get("source") or ""),
        "source_type": str(mapping.get("source_type") or ""),
        "published_at": mapping.get("published_at") or mapping.get("date"),
    }


def _compact_search_candidates(results: list[Any], *, limit: int = 12) -> list[dict[str, Any]]:
    """Return compact retrieval evidence for downstream LLM synthesis."""

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, result in enumerate(results, start=1):
        item = _jsonable_search_result(result)
        url = item.get("url", "").rstrip("/")
        key = url or item.get("title", "")
        if not key or key in seen:
            continue
        seen.add(key)
        snippet = item.get("snippet", "")
        candidates.append(
            {
                "source_id": f"retrieval:{index}",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": snippet[:700],
                "source": item.get("source", ""),
                "source_type": item.get("source_type", ""),
                "published_at": item.get("published_at"),
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def _sandbox_run_dir(
    *,
    artifact_root: str | Path | None,
    topic: str | None,
) -> Path:
    if artifact_root is not None:
        root = Path(artifact_root)
        root.mkdir(parents=True, exist_ok=True)
        return root
    base = Path(
        tempfile.mkdtemp(
            prefix="keystone_opportunity_scout_search_review_",
        )
    )
    slug = _slugify(topic or "opportunity-scout", fallback="opportunity-scout")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = base / f"{timestamp}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _stage_opportunity_scout_search_review_packet(
    *,
    topic: str | None,
    result: OpportunityScoutResult,
    metadata: dict[str, Any],
    collected_results: list[Any],
    artifact_root: str | Path | None,
    source_limit: int,
) -> tuple[SandboxSearchReviewSpec, dict[str, Any]]:
    run_dir = _sandbox_run_dir(artifact_root=artifact_root, topic=topic)
    packet_dir = run_dir / "search_packets"
    reports_dir = run_dir / "reports"
    review_workspace_dir = run_dir / "review_workspace"
    for path in (packet_dir, reports_dir, review_workspace_dir):
        path.mkdir(parents=True, exist_ok=True)

    packet_file = packet_dir / "opportunity_scout_search_packet.json"
    staged_results = collected_results[:source_limit]
    packet_payload = {
        "agent_name": "opportunity_scout",
        "topic": topic,
        "search_queries": result.search_queries,
        "search_provider": result.search_provider or metadata.get("search_provider"),
        "retrieved_result_count_total": len(collected_results),
        "retrieved_result_limit": source_limit,
        "retrieval_metadata": {
            key: value for key, value in metadata.items() if key != "sandbox_search_review"
        },
        "retrieved_results": [_jsonable_search_result(item) for item in staged_results],
        "opportunity_scout_result": result.model_dump(mode="json"),
    }
    packet_file.write_text(
        json.dumps(packet_payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    spec = SandboxSearchReviewSpec.from_paths(
        packet_files=[packet_file],
        prior_reports_dir=reports_dir,
        review_workspace_dir=review_workspace_dir,
        additional_instructions=(
            "Focus on whether the search packet needs broader opportunity lanes, "
            "source corroboration, or stronger U.S.-relevance validation."
        ),
    )
    review_payload = {
        "requested": False,
        "executed": False,
        "recommended": bool(metadata.get("search_review_recommended")),
        "sandbox_available": sandbox_agents_available(),
        "packet_file": str(packet_file),
        "run_dir": str(run_dir),
        "reports_dir": str(reports_dir),
        "review_workspace_dir": str(review_workspace_dir),
        "retrieved_result_count_total": len(collected_results),
        "retrieved_result_limit": source_limit,
        "status": "staged",
    }
    return spec, review_payload


def maybe_run_opportunity_scout_sandbox_review(
    *,
    topic: str | None,
    result: OpportunityScoutResult,
    metadata: dict[str, Any],
    collected_results: list[Any],
    requested: bool = False,
    execute: bool = False,
    live: bool = False,
    force: bool = False,
    artifact_root: str | Path | None = None,
    model: str | None = None,
    runner: Callable[[Any, str, Any], Any] | None = None,
    hosted_web_search: bool | None = None,
    hosted_web_search_external_web_access: bool = True,
    hosted_web_search_context_size: str = DEFAULT_SANDBOX_SEARCH_REVIEW_CONTEXT_SIZE,
    source_limit: int = DEFAULT_SANDBOX_SEARCH_REVIEW_SOURCE_LIMIT,
) -> dict[str, Any] | None:
    should_prepare = requested or force or bool(metadata.get("search_review_recommended"))
    if not should_prepare:
        return None
    effective_hosted_web_search = True if hosted_web_search is None else hosted_web_search

    spec, review_payload = _stage_opportunity_scout_search_review_packet(
        topic=topic,
        result=result,
        metadata=metadata,
        collected_results=collected_results,
        artifact_root=artifact_root,
        source_limit=source_limit,
    )
    review_payload["requested"] = requested
    review_payload["execute_requested"] = execute
    review_payload["forced"] = force
    review_payload["hosted_web_search"] = effective_hosted_web_search
    review_payload["hosted_web_search_external_web_access"] = hosted_web_search_external_web_access
    review_payload["hosted_web_search_context_size"] = hosted_web_search_context_size

    if not execute and not requested and not force:
        return review_payload

    try:
        sandbox_result = run_sandbox_search_review(
            spec,
            execute=execute,
            live=live,
            model=model,
            runner=runner,
            hosted_web_search=effective_hosted_web_search,
            hosted_web_search_config=SandboxHostedWebSearchConfig(
                external_web_access=hosted_web_search_external_web_access,
                search_context_size=hosted_web_search_context_size,
            ),
        )
    except SandboxAgentsUnavailable as exc:
        review_payload.update(
            {
                "sandbox_available": False,
                "status": "unavailable",
                "error": str(exc),
            }
        )
        return review_payload

    review_payload.update(
        {
            "status": "executed" if sandbox_result.executed else "preview",
            "executed": sandbox_result.executed,
            "live": sandbox_result.live,
            "approval_required": sandbox_result.approval_required,
            "artifact_review_required": sandbox_result.artifact_review_required,
            "send_enabled": sandbox_result.send_enabled,
            "can_send_email": sandbox_result.can_send_email,
            "audit_notes": list(sandbox_result.audit_notes),
            "mount_summary": dict(sandbox_result.setup.mount_summary),
            "final_output": (
                str(sandbox_result.final_output)
                if sandbox_result.final_output is not None
                else None
            ),
        }
    )
    return review_payload


def company_research_request_text(company: str, company_url: str | None = None) -> str:
    """Return the canonical live-search request text for company research."""

    base = (
        f"Research {company} for possible partnership or advisory relevance to Keystone. "
        "Focus on product, customers, traction signals, leadership, and why it may matter."
    )
    if company_url:
        return f"{base} Official website: {company_url}."
    return base


def retrieve_company_profile_live(
    *,
    company: str,
    company_url: str | None = None,
    requested_provider: str | None = None,
    max_results: int = 5,
    retrieval_hint: RetrievalHint | None = None,
    settings_loader: Callable[[], Any] | None = None,
    query_builder: Callable[[str, str | None], list[str]] | None = None,
    search_provider_builder: Callable[..., Any] | None = None,
    profile_builder: Callable[..., CompanyProfile] | None = None,
) -> tuple[CompanyProfile, dict[str, Any]]:
    """Run the hybrid live-search ladder for company research."""

    total_started_at = perf_counter()
    settings_loader = settings_loader or load_settings
    query_builder = query_builder or build_company_research_queries
    search_provider_builder = search_provider_builder or build_search_provider
    profile_builder = profile_builder or research_account_from_search_results
    request_text = company_research_request_text(company, company_url)
    autonomy_hint = derive_request_autonomy_hint(
        agent_name="business_research_analyst",
        request_text=request_text,
        agent_hint=retrieval_hint,
    )
    settings = settings_loader()
    search_config = build_shared_search_provider_config(
        requested_provider=requested_provider,
        configured_provider=settings.search_provider,
    )

    def build_client() -> HybridSearchProvider:
        return HybridSearchProvider(
            provider_sequence=search_config.provider_sequence,
            deepening_provider_sequence=search_config.deepening_provider_sequence,
            autonomy_hint=autonomy_hint,
            quality_assessor=lambda results, _query: assess_company_search_quality(
                results=results,
                company_name=company,
                company_url=company_url,
                request_text=request_text,
                autonomy_hint=autonomy_hint,
            ),
            provider_factory=lambda provider_name: search_provider_builder(
                provider=provider_name,
                live=True,
            ),
            parallel_provider_fanout=search_config.parallel_provider_fanout,
            provider_request_budget=search_config.provider_request_budget,
        )

    client = build_client()
    if len(search_config.provider_sequence) == 1:
        client.validate_configuration()

    queries = query_builder(company, company_url)
    search_results: list[Any] = []
    query_timings: list[dict[str, Any]] = []
    telemetry_packets: list[dict[str, Any]] = []
    with _maybe_transient_searxng_runtime(
        provider_sequence=search_config.provider_sequence,
        settings=settings,
        enabled=search_provider_builder is build_search_provider,
    ) as searxng_runtime:
        search_started_at = perf_counter()
        search_concurrency = min(_company_search_concurrency(), max(1, len(queries)))
        if search_concurrency <= 1 or len(queries) <= 1:
            for query in queries:
                query_started_at = perf_counter()
                results = client.search_web(query, num_results=max_results)
                query_timings.append(
                    {
                        "query": query,
                        "seconds": round(perf_counter() - query_started_at, 3),
                        "result_count": len(results),
                    }
                )
                search_results.extend(results)
            telemetry_packets.append(client.telemetry())
        else:
            ordered_results: list[list[Any]] = [[] for _query in queries]

            def run_query(
                index: int,
                query: str,
            ) -> tuple[int, str, list[Any], float, dict[str, Any]]:
                query_client = build_client()
                query_started_at = perf_counter()
                results = query_client.search_web(query, num_results=max_results)
                return (
                    index,
                    query,
                    results,
                    perf_counter() - query_started_at,
                    query_client.telemetry(),
                )

            with ThreadPoolExecutor(max_workers=search_concurrency) as executor:
                futures = {
                    executor.submit(run_query, index, query): (index, query)
                    for index, query in enumerate(queries)
                }
                for future in as_completed(futures):
                    index, query, results, seconds, telemetry = future.result()
                    ordered_results[index] = results
                    telemetry_packets.append(telemetry)
                    query_timings.append(
                        {
                            "query": query,
                            "seconds": round(seconds, 3),
                            "result_count": len(results),
                        }
                    )
            for results in ordered_results:
                search_results.extend(results)

        search_seconds = perf_counter() - search_started_at
    quality_started_at = perf_counter()
    search_quality = assess_company_search_quality(
        results=search_results,
        company_name=company,
        company_url=company_url,
        request_text=request_text,
        autonomy_hint=autonomy_hint,
    )
    quality_seconds = perf_counter() - quality_started_at
    metadata = _merge_company_search_telemetry(
        provider_sequence=search_config.provider_sequence,
        telemetry_packets=telemetry_packets or [client.telemetry()],
    )
    website_started_at = perf_counter()
    website_inputs, website_errors, website_stats = _extract_company_website_inputs(
        company=company,
        company_url=company_url,
        search_results=search_results,
        provider=str(getattr(settings, "website_extractor", "trafilatura") or "trafilatura"),
    )
    website_seconds = perf_counter() - website_started_at
    profile_started_at = perf_counter()
    profile = profile_builder(
        company_name=company,
        company_url=company_url,
        search_results=search_results,
        website_inputs=website_inputs,
    )
    profile_seconds = perf_counter() - profile_started_at
    metadata.update(
        {
            "mode": "live_search",
            "live_search": True,
            "search_provider": search_provider_label(metadata),
            "search_queries": queries,
            "raw_search_result_count": len(search_results),
            "max_results": max_results,
            "searxng_transient_runtime": dict(searxng_runtime),
            "search_quality": search_quality.to_dict(),
            "source_coverage": search_quality.source_coverage,
            "timing": {
                "total_seconds": round(perf_counter() - total_started_at, 3),
                "search_seconds": round(search_seconds, 3),
                "quality_assessment_seconds": round(quality_seconds, 3),
                "website_extraction_seconds": round(website_seconds, 3),
                "website_extraction_count": len(website_inputs),
                "profile_build_seconds": round(profile_seconds, 3),
                "query_count": len(queries),
                "search_concurrency": search_concurrency,
                "slowest_queries": sorted(
                    query_timings,
                    key=lambda item: float(item.get("seconds") or 0),
                    reverse=True,
                )[:5],
            },
            "website_extraction": {
                **website_stats,
                "errors": website_errors[:5],
            },
                "retrieval_ladder": [
                    {
                        "rung": "search_discovery",
                        "providers": list(search_config.provider_sequence),
                        "queries": len(queries),
                        "raw_result_count": len(search_results),
                        "seconds": round(search_seconds, 3),
                    "useful": bool(search_results),
                },
                {
                    "rung": "website_extraction",
                    "providers": website_stats.get("providers_used", []),
                    "pages_considered": website_stats.get("pages_considered", 0),
                    "pages_extracted": website_stats.get("page_count", 0),
                    "claim_count": website_stats.get("claim_count", 0),
                    "seconds": round(website_seconds, 3),
                    "useful": bool(website_inputs),
                },
            ],
        }
    )
    metadata["retrieval_diagnostics"] = retrieval_diagnostics_from_metadata(metadata)
    return profile, metadata


def _extract_company_website_inputs(
    *,
    company: str,
    company_url: str | None,
    search_results: list[Any],
    provider: str,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    fallback_provider = _website_extraction_fallback_provider(primary_provider=provider)
    base_stats = {
        "enabled": website_extraction_enabled(),
        "provider": provider,
        "fallback_provider": fallback_provider,
        "agent_html_review_enabled": agent_html_review_enabled(),
        "agent_html_review_max_pages": agent_html_review_max_pages(),
        "agent_html_review_page_count": 0,
        "agent_html_review_claim_count": 0,
        "pages_considered": 0,
        "internal_page_discovery_count": 0,
        "page_count": 0,
        "claim_count": 0,
        "providers_used": [],
    }
    if not website_extraction_enabled():
        return [], [], base_stats
    urls = _company_website_extraction_urls(
        company=company,
        company_url=company_url,
        search_results=search_results,
    )
    if company_url:
        discovered_urls = discover_company_page_urls(
            company_url,
            company_name=company,
            live=True,
            max_urls=_website_extraction_max_pages(),
        )
        base_stats["internal_page_discovery_count"] = len(discovered_urls)
        urls = list(dict.fromkeys([*discovered_urls, *urls]))[: _website_extraction_max_pages()]
    base_stats["pages_considered"] = len(urls)
    website_inputs: list[dict[str, Any]] = []
    errors: list[str] = []
    html_review_attempts = 0
    html_review_claim_count = 0
    for index, url in enumerate(urls, start=1):
        try:
            result = extract_website_content(
                url,
                company_name=company,
                provider=provider,
                live=True,
            )
        except WebsiteExtractionError as exc:
            if not fallback_provider:
                errors.append(f"{url}: {exc}")
                continue
            try:
                result = extract_website_content(
                    url,
                    company_name=company,
                    provider=fallback_provider,
                    live=True,
                )
            except WebsiteExtractionError as fallback_exc:
                errors.append(f"{url}: {exc}; fallback {fallback_provider}: {fallback_exc}")
                continue
        if not result.claims and result.provider != "firecrawl":
            if fallback_provider:
                try:
                    fallback_result = extract_website_content(
                        url,
                        company_name=company,
                        provider=fallback_provider,
                        live=True,
                    )
                except WebsiteExtractionError as exc:
                    errors.append(
                        f"{url}: empty {result.provider}; fallback {fallback_provider}: {exc}"
                    )
                else:
                    result = fallback_result
        if (
            agent_html_review_enabled()
            and html_review_attempts < agent_html_review_max_pages()
            and len(result.claims) <= agent_html_review_min_claims()
            and result.text_or_markdown.strip()
        ):
            html_review_attempts += 1
            try:
                review = run_agent_html_review(
                    html_or_text=result.text_or_markdown,
                    subject=company,
                    url=result.url,
                    title=result.title,
                    live=True,
                )
            except HtmlReviewError as exc:
                errors.append(f"{url}: agent HTML review failed: {exc}")
            except Exception as exc:
                errors.append(f"{url}: agent HTML review unavailable: {exc}")
            else:
                review_claims = [
                    claim for claim in review.claims if claim and claim not in result.claims
                ]
                if review_claims:
                    result = result.model_copy(
                        update={
                            "provider": f"{result.provider}+{review.provider}",
                            "claims": [*result.claims, *review_claims],
                            "metadata": {
                                **result.metadata,
                                "agent_html_review": review.model_dump(mode="json"),
                            },
                        }
                    )
                    html_review_claim_count += len(review_claims)
        if not result.claims:
            continue
        website_inputs.append(
            {
                "source_id": f"website_extract:{index}",
                "title": result.title or f"{company} website page",
                "url": result.url,
                "source_type": "website",
                "supported_claims": result.claims,
                "text_or_markdown": result.text_or_markdown[:6000],
                "provider": result.provider,
                "confidence": 0.78 if result.provider == "trafilatura" else 0.82,
            }
        )
    providers_used = list(
        dict.fromkeys(
            str(item.get("provider") or "") for item in website_inputs if item.get("provider")
        )
    )
    stats = {
        **base_stats,
        "page_count": len(website_inputs),
        "claim_count": sum(len(item.get("supported_claims") or []) for item in website_inputs),
        "providers_used": providers_used,
        "agent_html_review_page_count": html_review_attempts,
        "agent_html_review_claim_count": html_review_claim_count,
    }
    return website_inputs, errors, stats


def _website_extraction_fallback_provider(*, primary_provider: str) -> str:
    fallback = os.getenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", "").strip().lower()
    if not fallback or fallback == primary_provider.strip().lower():
        return ""
    if fallback not in {"firecrawl", "trafilatura"}:
        return ""
    return fallback


def _company_website_extraction_urls(
    *,
    company: str,
    company_url: str | None,
    search_results: list[Any],
) -> list[str]:
    max_pages = _website_extraction_max_pages()
    candidates: list[str] = []
    if company_url:
        candidates.extend(default_company_page_urls(company_url))
    for result in search_results:
        url = str(getattr(result, "link", "") or getattr(result, "url", "") or "").strip()
        if not url or not _looks_like_company_page(url, company=company):
            continue
        candidates.append(url)
    return list(dict.fromkeys(candidates))[:max_pages]


def _website_extraction_max_pages() -> int:
    raw = os.getenv("KEYSTONE_WEBSITE_EXTRACTION_MAX_PAGES", "4").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 4
    return max(1, min(8, value))


def _looks_like_company_page(url: str, *, company: str) -> bool:
    lowered = url.lower()
    excluded = (
        "linkedin.com",
        "crunchbase.com",
        "pitchbook.com",
        "facebook.com",
        "x.com",
        "twitter.com",
        "youtube.com",
    )
    if any(domain in lowered for domain in excluded):
        return False
    company_token = company.lower().split()[0] if company else ""
    if company_token and company_token in lowered:
        return True
    likely_paths = ("/about", "/solutions", "/partners", "/news", "/contact")
    return any(path in lowered for path in likely_paths)


def _company_search_concurrency() -> int:
    raw = os.getenv("KEYSTONE_COMPANY_SEARCH_CONCURRENCY", "4").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 4
    return max(1, min(8, value))


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def build_shared_search_provider_config(
    *,
    requested_provider: str | None,
    configured_provider: str | None = None,
    fallback_provider: str | None = None,
) -> SharedSearchProviderConfig:
    """Resolve the shared live-search policy for search-heavy Keystone agents."""

    provider_sequence = build_provider_sequence(
        requested_provider=requested_provider,
        configured_provider=configured_provider,
        fallback_provider=fallback_provider,
    )
    agents_enabled = _env_bool("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", default=True)
    parallel_agents_enabled = (
        agents_enabled and _env_bool("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", default=True)
    )
    provider_sequence = _with_agents_web_search_parallel_lane(
        provider_sequence=provider_sequence,
        requested_provider=requested_provider,
        enabled=parallel_agents_enabled,
    )
    deepening_provider_sequence = _deepening_search_providers(
        requested_provider=requested_provider,
        provider_sequence=provider_sequence,
        agents_enabled=agents_enabled,
    )
    return SharedSearchProviderConfig(
        provider_sequence=provider_sequence,
        deepening_provider_sequence=deepening_provider_sequence,
        provider_request_budget=_agents_web_search_request_budget(enabled=agents_enabled),
        parallel_provider_fanout=_parallel_provider_fanout_enabled(
            requested_provider=requested_provider,
            provider_sequence=provider_sequence,
        ),
    )


def _parallel_provider_fanout_enabled(
    *,
    requested_provider: str | None,
    provider_sequence: tuple[str, ...],
) -> bool:
    if len(provider_sequence) <= 1:
        return False
    requested = (requested_provider or "").strip().lower()
    if requested and requested != "searxng":
        return False
    return True


def _with_agents_web_search_parallel_lane(
    *,
    provider_sequence: tuple[str, ...],
    requested_provider: str | None,
    enabled: bool,
) -> tuple[str, ...]:
    if not enabled:
        return provider_sequence
    requested = (requested_provider or "").strip().lower()
    if requested not in {"", "searxng"}:
        return provider_sequence
    if "searxng" not in provider_sequence or "agents-web-search" in provider_sequence:
        return provider_sequence
    expanded: list[str] = []
    for provider_name in provider_sequence:
        expanded.append(provider_name)
        if provider_name == "searxng":
            expanded.append("agents-web-search")
    return tuple(dict.fromkeys(expanded))


def _deepening_search_providers(
    *,
    requested_provider: str | None,
    provider_sequence: tuple[str, ...],
    agents_enabled: bool,
) -> tuple[str, ...]:
    requested = (requested_provider or "").strip().lower()
    providers: list[str] = []
    if _env_bool("KEYSTONE_TAVILY_SEARCH_FALLBACK", default=False) and requested != "tavily":
        providers.append("tavily")
    if agents_enabled and requested in {"", "searxng"}:
        providers.append("agents-web-search")
    return tuple(
        dict.fromkeys(
            provider_name
            for provider_name in providers
            if provider_name not in provider_sequence
        )
    )


def _agents_web_search_request_budget(*, enabled: bool) -> ProviderRequestBudget | None:
    if not enabled:
        return None
    raw = os.getenv("KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN", "").strip()
    try:
        cap = int(raw) if raw else DEFAULT_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN
    except ValueError:
        cap = DEFAULT_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN
    return ProviderRequestBudget({"agents-web-search": max(0, cap)})


@contextmanager
def _maybe_transient_searxng_runtime(
    *,
    provider_sequence: tuple[str, ...],
    settings: Any,
    enabled: bool = True,
) -> Any:
    """Start repo-local SearXNG for one live run, then stop it if we started it."""

    metadata: dict[str, Any] = {
        "enabled": False,
        "started": False,
        "stopped": False,
        "reason": "not_needed",
    }
    base_url = str(getattr(settings, "searxng_base_url", "") or "")
    if not enabled:
        metadata["reason"] = "custom_search_provider_builder"
        yield metadata
        return
    if "searxng" not in provider_sequence:
        yield metadata
        return
    if not _env_bool("KEYSTONE_SEARXNG_TRANSIENT", default=True):
        metadata["reason"] = "disabled"
        yield metadata
        return
    if not _is_local_searxng_base_url(base_url):
        metadata["reason"] = "non_local_base_url"
        yield metadata
        return
    metadata.update({"enabled": True, "base_url": base_url})
    if _searxng_endpoint_reachable(base_url):
        metadata["reason"] = "already_running"
        yield metadata
        return

    metadata["reason"] = "started_for_run"
    _run_searxng_lifecycle_command("start")
    metadata["started"] = True
    try:
        yield metadata
    finally:
        _run_searxng_lifecycle_command("stop", check=False)
        metadata["stopped"] = True


def _is_local_searxng_base_url(base_url: str) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url)
    return (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


def _searxng_endpoint_reachable(base_url: str) -> bool:
    endpoint = base_url.rstrip("/") + "/search?q=searxng%20health%20check&format=json"
    try:
        with urlopen(endpoint, timeout=DEFAULT_SEARXNG_TRANSIENT_TIMEOUT_SECONDS) as response:
            return 200 <= int(getattr(response, "status", 200)) < 500
    except Exception:
        return False


def _run_searxng_lifecycle_command(command: str, *, check: bool = True) -> None:
    root = Path(os.getenv("KBA_REPO_ROOT") or Path(__file__).resolve().parents[2])
    script = root / "scripts" / "manage_searxng_headless.sh"
    if not script.exists():
        if check:
            raise RuntimeError(f"SearXNG lifecycle script not found: {script}")
        return
    completed = subprocess.run(
        [str(script), command],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        details = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part and part.strip()
        )
        raise RuntimeError(f"SearXNG transient {command} failed: {details}")


def _sandbox_search_review_source_limit(max_results: int) -> int:
    raw = os.getenv("KEYSTONE_SANDBOX_SEARCH_REVIEW_SOURCE_LIMIT", "").strip()
    default_limit = max(DEFAULT_SANDBOX_SEARCH_REVIEW_SOURCE_LIMIT, max_results * 2)
    try:
        value = int(raw) if raw else default_limit
    except ValueError:
        value = default_limit
    return max(1, min(MAX_SANDBOX_SEARCH_REVIEW_SOURCE_LIMIT, value))


def _sandbox_search_review_context_size(explicit: str | None) -> str:
    candidate = (
        (
            explicit
            or os.getenv("KEYSTONE_SANDBOX_SEARCH_REVIEW_WEB_SEARCH_CONTEXT")
            or DEFAULT_SANDBOX_SEARCH_REVIEW_CONTEXT_SIZE
        )
        .strip()
        .lower()
    )
    if candidate not in {"low", "medium", "high"}:
        return DEFAULT_SANDBOX_SEARCH_REVIEW_CONTEXT_SIZE
    return candidate


def _should_auto_recommend_sandbox_search_review(
    *,
    result: OpportunityScoutResult,
    metadata: dict[str, Any],
    max_results: int,
) -> bool:
    if metadata.get("search_review_recommended"):
        return True
    target_floor = max(1, min(max_results, 2))
    if len(result.records) < target_floor:
        return True
    quality = metadata.get("search_quality")
    if isinstance(quality, dict) and quality.get("needs_search_review"):
        return True
    return False


def _merge_company_search_telemetry(
    *,
    provider_sequence: tuple[str, ...],
    telemetry_packets: list[dict[str, Any]],
) -> dict[str, Any]:
    providers_attempted: list[str] = []
    providers_used: list[str] = []
    deepening_provider_sequence: list[str] = []
    provider_usage: dict[str, dict[str, float | int]] = {}
    provider_errors: list[dict[str, Any]] = []
    quality_reasons: list[str] = []
    structured_enrichment_recommended = False
    search_review_recommended = False
    precision_search_escalated = False
    deepening_search_used = False
    provider_error_fallback_used = False
    serper_credits = 0
    tavily_credits = 0
    agents_web_search_credits = 0
    autonomy_hint: dict[str, Any] = {}

    for packet in telemetry_packets:
        for provider_name in packet.get("search_providers_attempted") or []:
            name = str(provider_name)
            if name and name not in providers_attempted:
                providers_attempted.append(name)
        for provider_name in packet.get("search_providers_used") or []:
            name = str(provider_name)
            if name and name not in providers_used:
                providers_used.append(name)
        for provider_name in packet.get("search_deepening_provider_sequence") or []:
            name = str(provider_name)
            if name and name not in deepening_provider_sequence:
                deepening_provider_sequence.append(name)
        raw_usage = packet.get("provider_usage")
        if isinstance(raw_usage, dict):
            for provider_name, usage in raw_usage.items():
                if not isinstance(usage, dict):
                    continue
                target = provider_usage.setdefault(
                    str(provider_name),
                    {
                        "requests_attempted": 0,
                        "requests_succeeded": 0,
                        "raw_result_count": 0,
                        "credits_used": 0,
                        "total_seconds": 0.0,
                    },
                )
                for key in (
                    "requests_attempted",
                    "requests_succeeded",
                    "raw_result_count",
                    "credits_used",
                ):
                    target[key] = int(target[key]) + int(float(usage.get(key) or 0))
                target["total_seconds"] = round(
                    float(target["total_seconds"]) + float(usage.get("total_seconds") or 0),
                    3,
                )
        for error in packet.get("search_provider_errors") or []:
            if isinstance(error, dict):
                provider_errors.append(error)
        for reason in packet.get("quality_reason_hints") or []:
            reason_text = str(reason).strip()
            if reason_text and reason_text not in quality_reasons:
                quality_reasons.append(reason_text)
        structured_enrichment_recommended = structured_enrichment_recommended or bool(
            packet.get("structured_enrichment_recommended")
        )
        search_review_recommended = search_review_recommended or bool(
            packet.get("search_review_recommended")
        )
        precision_search_escalated = precision_search_escalated or bool(
            packet.get("precision_search_escalated")
        )
        deepening_search_used = deepening_search_used or bool(packet.get("deepening_search_used"))
        provider_error_fallback_used = provider_error_fallback_used or bool(
            packet.get("provider_error_fallback_used")
        )
        serper_credits += int(float(packet.get("serper_estimated_credits_used") or 0))
        tavily_credits += int(float(packet.get("tavily_estimated_credits_used") or 0))
        agents_web_search_credits += int(
            float(packet.get("agents_web_search_estimated_calls_used") or 0)
        )
        if not autonomy_hint and isinstance(packet.get("autonomy_hint"), dict):
            autonomy_hint = dict(packet["autonomy_hint"])

    return {
        "search_provider_sequence": list(provider_sequence),
        "search_deepening_provider_sequence": deepening_provider_sequence,
        "primary_search_provider": provider_sequence[0] if provider_sequence else "",
        "search_providers_attempted": providers_attempted,
        "search_providers_used": providers_used,
        "search_provider_used": providers_used[-1] if providers_used else "",
        "precision_search_escalated": precision_search_escalated,
        "deepening_search_used": deepening_search_used,
        "provider_error_fallback_used": provider_error_fallback_used,
        "search_provider_errors": provider_errors,
        "provider_usage": provider_usage,
        "provider_value_summary": provider_value_summary(provider_usage),
        "tavily_estimated_credits_used": tavily_credits,
        "tavily_credit_budget": _latest_tavily_credit_budget(telemetry_packets),
        "agents_web_search_estimated_calls_used": agents_web_search_credits,
        "serper_estimated_credits_used": serper_credits,
        "autonomy_hint": autonomy_hint,
        "structured_enrichment_recommended": structured_enrichment_recommended,
        "structured_enrichment_candidates": (
            ["trafilatura", "firecrawl", "browserless", "apify"]
            if structured_enrichment_recommended
            else []
        ),
        "search_review_recommended": search_review_recommended,
        "quality_reason_hints": quality_reasons,
    }


def _latest_tavily_credit_budget(telemetry_packets: list[dict[str, Any]]) -> dict[str, Any]:
    for packet in reversed(telemetry_packets):
        budget = packet.get("tavily_credit_budget")
        if isinstance(budget, dict) and budget:
            return dict(budget)
    return {}


def opportunity_scout_request_text(topic: str | None) -> str:
    """Return the canonical live-search request text for opportunity scouting."""

    topic_text = (topic or "Keystone-relevant business opportunities").strip()
    return (
        f"Find high-fit live opportunities for Keystone. Topic: {topic_text}. "
        "Prioritize current, well-sourced results and avoid padding weak matches."
    )


def build_opportunity_search_provider(
    *,
    topic: str | None,
    requested_provider: str | None = None,
    fallback_provider: str | None = None,
    desired_results: int,
    retrieval_hint: RetrievalHint | None = None,
    settings_loader: Callable[[], Any] | None = None,
    search_provider_builder: Callable[..., Any] | None = None,
) -> HybridSearchProvider:
    """Build the hybrid live-search provider for Opportunity Scout."""

    settings_loader = settings_loader or load_settings
    search_provider_builder = search_provider_builder or build_search_provider
    request_text = opportunity_scout_request_text(topic)
    autonomy_hint = derive_request_autonomy_hint(
        agent_name="opportunity_scout",
        request_text=request_text,
        agent_hint=retrieval_hint,
    )
    search_config = build_shared_search_provider_config(
        requested_provider=requested_provider,
        configured_provider=settings_loader().search_provider,
        fallback_provider=fallback_provider,
    )
    provider = HybridSearchProvider(
        provider_sequence=search_config.provider_sequence,
        deepening_provider_sequence=search_config.deepening_provider_sequence,
        autonomy_hint=autonomy_hint,
        quality_assessor=lambda results, _query: assess_opportunity_search_quality(
            results=results,
            desired_results=desired_results,
            request_text=request_text,
            autonomy_hint=autonomy_hint,
        ),
        provider_factory=lambda provider_name: search_provider_builder(
            provider=provider_name,
            live=True,
        ),
        parallel_provider_fanout=search_config.parallel_provider_fanout,
        provider_request_budget=search_config.provider_request_budget,
    )
    if len(search_config.provider_sequence) == 1:
        provider.validate_configuration()
    return provider


def _topic_is_role_focused(topic: str | None) -> bool:
    normalized = f" {str(topic or '').strip().lower()} "
    return any(
        marker in normalized
        for marker in (
            " role ",
            " roles ",
            " job ",
            " jobs ",
            " hiring ",
            " career ",
            " careers ",
            " position ",
            " positions ",
        )
    )


def opportunity_search_quality(
    *,
    topic: str | None,
    max_results: int,
    results: list[Any],
    retrieval_hint: RetrievalHint | None = None,
) -> dict[str, Any]:
    """Return deterministic quality metadata for opportunity live search."""

    request_text = opportunity_scout_request_text(topic)
    autonomy_hint = derive_request_autonomy_hint(
        agent_name="opportunity_scout",
        request_text=request_text,
        agent_hint=retrieval_hint,
    )
    return assess_opportunity_search_quality(
        results=results,
        desired_results=max_results,
        request_text=request_text,
        autonomy_hint=autonomy_hint,
    ).to_dict()


def retrieval_audit_notes(metadata: dict[str, Any]) -> list[str]:
    """Return human-readable retrieval notes for Opportunity Scout audit trails."""

    notes: list[str] = []
    providers_used = metadata.get("search_providers_used") or []
    if providers_used:
        notes.append(f"Retrieval ladder used: {' -> '.join(str(item) for item in providers_used)}.")
    if metadata.get("precision_search_escalated"):
        notes.append("Precision search escalation was triggered after initial quality checks.")
    if metadata.get("provider_error_fallback_used"):
        notes.append("Retrieval recovered from a provider error by falling back to the next stage.")
    quality = metadata.get("search_quality") or {}
    reasons = quality.get("reasons") or metadata.get("quality_reason_hints") or []
    if reasons:
        notes.append(f"Retrieval quality notes: {', '.join(str(item) for item in reasons[:4])}.")
    if metadata.get("structured_enrichment_recommended"):
        notes.append(
            "Structured enrichment may help next: "
            f"{', '.join(metadata.get('structured_enrichment_candidates') or [])}."
        )
    if metadata.get("search_review_recommended"):
        notes.append("Sandbox second-pass review is recommended for the staged search packet.")
    sandbox_review = metadata.get("sandbox_search_review") or {}
    status = str(sandbox_review.get("status") or "").strip().lower()
    if status == "staged":
        notes.append("Sandbox search-review packet was staged for optional second-pass review.")
        if sandbox_review.get("hosted_web_search"):
            notes.append(
                "Sandbox review is configured to allow bounded hosted web_search for corroboration."
            )
    elif status == "preview":
        notes.append("Sandbox search-review preview was built; execution remains optional.")
        if sandbox_review.get("hosted_web_search"):
            notes.append(
                "Sandbox preview includes OpenAI hosted web_search for narrow follow-up checking."
            )
    elif status == "executed":
        notes.append(
            "Sandbox second-pass review executed; host review of draft artifacts is still required."
        )
    elif status == "unavailable":
        notes.append("Sandbox search review was requested but sandbox SDK support is unavailable.")
    return notes


def run_opportunity_scout_live(
    *,
    topic: str | None = None,
    max_results: int = 5,
    requested_provider: str | None = None,
    fallback_provider: str | None = None,
    retrieval_hint: RetrievalHint | None = None,
    search_plan: OpportunitySearchPlan | None = None,
    save: bool = False,
    existing_state: Any = None,
    settings_loader: Callable[[], Any] | None = None,
    search_provider_builder: Callable[..., Any] | None = None,
    sandbox_search_review: bool = False,
    force_sandbox_search_review: bool = False,
    execute_sandbox_search_review: bool = False,
    sandbox_search_review_live: bool = False,
    sandbox_search_review_artifact_root: str | Path | None = None,
    sandbox_search_review_model: str | None = None,
    sandbox_runner: Callable[[Any, str, Any], Any] | None = None,
    sandbox_search_review_hosted_web_search: bool | None = None,
    sandbox_search_review_hosted_web_search_external_web_access: bool = True,
    sandbox_search_review_hosted_web_search_context_size: str | None = None,
) -> tuple[OpportunityScoutResult, dict[str, Any]]:
    """Run Opportunity Scout live search with orchestrator-aware retrieval hints."""

    settings_loader = settings_loader or load_settings
    settings = settings_loader()
    provider = build_opportunity_search_provider(
        topic=topic,
        requested_provider=requested_provider,
        fallback_provider=fallback_provider,
        desired_results=max_results,
        retrieval_hint=retrieval_hint,
        settings_loader=lambda: settings,
        search_provider_builder=search_provider_builder,
    )
    effective_provider_builder = search_provider_builder or build_search_provider
    provider_sequence = tuple(getattr(provider, "provider_sequence", ()) or ())
    with _maybe_transient_searxng_runtime(
        provider_sequence=provider_sequence,
        settings=settings,
        enabled=(
            effective_provider_builder is build_search_provider
            and bool(provider_sequence)
        ),
    ) as searxng_runtime:
        result = scout_opportunities_live_search(
            topic=topic,
            max_results=max_results,
            search_plan=search_plan,
            search_provider=provider,
            save=save,
            existing_state=existing_state,
        )
        collected_results = provider.collected_results()
        metadata = provider.telemetry()
    metadata.update(
        {
            "search_provider": search_provider_label(metadata),
            "fallback_search_provider": fallback_provider,
            "searxng_transient_runtime": dict(searxng_runtime),
            "search_provider_fallback_used": metadata.get(
                "provider_error_fallback_used",
                False,
            ),
            "search_quality": opportunity_search_quality(
                topic=topic,
                max_results=max_results,
                results=collected_results,
                retrieval_hint=retrieval_hint,
            ),
            "search_plan": search_plan.model_dump(mode="json") if search_plan is not None else None,
            "retrieved_source_candidates": _compact_search_candidates(
                collected_results,
                limit=_sandbox_search_review_source_limit(max_results),
            ),
            "retrieval_ladder": [
                {
                    "rung": "search_discovery",
                    "providers": metadata.get("search_providers_used") or [],
                    "raw_result_count": len(collected_results),
                    "seconds": _provider_usage_seconds(metadata),
                    "useful": bool(result.records),
                }
            ],
        }
    )
    metadata["source_coverage"] = (
        metadata["search_quality"].get("source_coverage")
        if isinstance(metadata.get("search_quality"), dict)
        else None
    )
    if _should_auto_recommend_sandbox_search_review(
        result=result,
        metadata=metadata,
        max_results=max_results,
    ):
        metadata["search_review_recommended"] = True
        quality = metadata.get("search_quality")
        if isinstance(quality, dict):
            reasons = [str(item) for item in (quality.get("reasons") or []) if str(item).strip()]
            if len(result.records) < max(1, min(max_results, 2)):
                reasons.append("opportunity objective was not met after deterministic retrieval")
            quality["needs_search_review"] = True
            quality["reasons"] = list(dict.fromkeys(reasons))
    sandbox_source_limit = _sandbox_search_review_source_limit(max_results)
    sandbox_context_size = _sandbox_search_review_context_size(
        sandbox_search_review_hosted_web_search_context_size
    )
    auto_live_sandbox = _env_bool("KEYSTONE_SANDBOX_SEARCH_REVIEW_LIVE")
    auto_execute_sandbox = (
        _env_bool("KEYSTONE_SANDBOX_SEARCH_REVIEW_AUTO_EXECUTE") and auto_live_sandbox
    )
    sandbox_review = maybe_run_opportunity_scout_sandbox_review(
        topic=topic,
        result=result,
        metadata=metadata,
        collected_results=collected_results,
        requested=sandbox_search_review,
        execute=execute_sandbox_search_review or auto_execute_sandbox,
        live=sandbox_search_review_live or auto_live_sandbox,
        force=force_sandbox_search_review,
        artifact_root=sandbox_search_review_artifact_root,
        model=sandbox_search_review_model,
        runner=sandbox_runner,
        hosted_web_search=sandbox_search_review_hosted_web_search,
        hosted_web_search_external_web_access=(
            sandbox_search_review_hosted_web_search_external_web_access
        ),
        hosted_web_search_context_size=sandbox_context_size,
        source_limit=sandbox_source_limit,
    )
    if sandbox_review is not None:
        metadata["sandbox_search_review"] = sandbox_review
    metadata["retrieval_diagnostics"] = retrieval_diagnostics_from_metadata(metadata)
    result = result.model_copy(
        update={
            "search_provider": metadata["search_provider"],
            "audit_notes": [*result.audit_notes, *retrieval_audit_notes(metadata)],
        }
    )
    return result, metadata


def _provider_usage_seconds(metadata: dict[str, Any]) -> float:
    usage = metadata.get("provider_usage")
    if not isinstance(usage, dict):
        return 0.0
    return round(
        sum(
            float(item.get("total_seconds") or 0)
            for item in usage.values()
            if isinstance(item, dict)
        ),
        3,
    )
