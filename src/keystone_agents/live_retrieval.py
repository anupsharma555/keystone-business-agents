"""Reusable live retrieval helpers for search-heavy specialist routes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import Parameter, signature
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
    build_provider_use_ladder,
    derive_request_autonomy_hint,
    filter_company_search_results_for_entity,
    provider_value_summary,
    resolve_requested_search_provider,
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
from keystone_agents.source_triage import triage_source_candidates
from keystone_agents.tools.html_review_tool import (
    HtmlReviewError,
    agent_html_review_enabled,
    agent_html_review_max_pages,
    agent_html_review_min_claims,
    run_agent_html_review,
)
from keystone_agents.tools.serper_tool import build_search_provider
from keystone_agents.tools.website_extraction_tool import (
    MAX_SELECTED_URLS_PER_BUNDLE,
    MAX_WEB_BUNDLE_RESPONSE_CHARS,
    MAX_WEB_PREVIEW_TOTAL_CHARS,
    default_company_page_urls,
    discover_company_page_urls,
    extract_website_content,
    extract_website_content_with_fallbacks,
    project_web_source,
    website_extraction_budget,
    website_extraction_enabled,
    website_extraction_provider_sequence,
)

DEFAULT_SANDBOX_SEARCH_REVIEW_CONTEXT_SIZE = "low"
DEFAULT_SANDBOX_SEARCH_REVIEW_SOURCE_LIMIT = 8
MAX_SANDBOX_SEARCH_REVIEW_SOURCE_LIMIT = 12
DEFAULT_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN = 2
DEFAULT_EXA_SEARCH_MAX_CALLS_PER_RUN = 10
DEFAULT_TAVILY_SEARCH_MAX_CALLS_PER_RUN = 2
DEFAULT_SEARXNG_TRANSIENT_TIMEOUT_SECONDS = 2.0
DEFAULT_SEARXNG_LIFECYCLE_TIMEOUT_SECONDS = 15.0

@dataclass(frozen=True)
class SharedSearchProviderConfig:
    """Resolved live-search provider layout for Keystone research agents."""

    provider_sequence: tuple[str, ...]
    deepening_provider_sequence: tuple[str, ...]
    provider_request_budget: ProviderRequestBudget | None
    parallel_provider_fanout: bool


@dataclass
class _ResearchRetrievalDeadline:
    """Stage-aware deadline that preserves already retrieved partial evidence."""

    deadline_seconds: float | None
    clock: Callable[[], float]
    started_at: float
    stopped_before_stage: str = ""

    @classmethod
    def start(
        cls,
        deadline_seconds: float | None,
        *,
        clock: Callable[[], float],
    ) -> _ResearchRetrievalDeadline:
        normalized = (
            None if deadline_seconds is None else max(0.0, float(deadline_seconds))
        )
        return cls(
            deadline_seconds=normalized,
            clock=clock,
            started_at=clock(),
        )

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    @property
    def remaining_seconds(self) -> float | None:
        if self.deadline_seconds is None:
            return None
        return max(0.0, self.deadline_seconds - self.elapsed_seconds)

    def allows(self, stage: str) -> bool:
        remaining = self.remaining_seconds
        if remaining is None or remaining > 0.0:
            return True
        if not self.stopped_before_stage:
            self.stopped_before_stage = stage
        return False

    def bounded_timeout(self, configured_seconds: float) -> float:
        remaining = self.remaining_seconds
        if remaining is None:
            return configured_seconds
        return max(0.01, min(configured_seconds, remaining))

    def metadata(self) -> dict[str, Any]:
        return {
            "status": "partial" if self.stopped_before_stage else "complete",
            "deadline_seconds": self.deadline_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "stopped_before_stage": self.stopped_before_stage or None,
        }


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

    quality = (
        metadata.get("search_quality") if isinstance(metadata.get("search_quality"), dict) else {}
    )
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
        metadata.get("provider_usage") if isinstance(metadata.get("provider_usage"), dict) else {}
    )
    diagnostics = {
        "mode": str(metadata.get("mode") or ""),
        "live_search": bool(metadata.get("live_search")),
        "provider_summary": provider_summary,
        "providers_used": list(dict.fromkeys(providers_used)),
        "provider_policy": _compact_provider_policy(providers_used),
        "provider_use_ladder": _compact_provider_use_ladder(metadata, source_coverage, quality),
        "search_queries": _compact_search_queries(metadata.get("search_queries")),
        "provider_queries": _compact_provider_queries(metadata.get("provider_queries")),
        "provider_usage": _compact_provider_usage(provider_usage),
        "provider_result_samples": _compact_provider_result_samples(
            metadata.get("provider_result_samples")
        ),
        "source_triage": _compact_source_triage(metadata.get("source_triage")),
        "source_focus": _compact_source_focus(metadata.get("source_focus")),
        "retrieval_ladder": _compact_retrieval_ladder(metadata.get("retrieval_ladder")),
        "search_quality_summary": _compact_quality_summary(quality),
        "source_coverage_summary": _compact_source_coverage(source_coverage),
        "source_limits": _compact_source_limits(source_coverage, quality),
        "fallback_used": bool(
            metadata.get("search_provider_fallback_used")
            or metadata.get("provider_error_fallback_used")
            or metadata.get("deepening_search_used")
        ),
        "precision_escalated": bool(metadata.get("precision_search_escalated")),
        "hosted_web_search_lane_used": "agents-web-search" in providers_used,
        "review_recommended": bool(
            metadata.get("search_review_recommended") or quality.get("needs_search_review")
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


def _compact_provider_policy(providers_used: Sequence[str]) -> list[dict[str, str]]:
    specs = {
        "dry-run": {
            "role": "fixture validation",
            "budget_class": "free/local",
            "default_use": "dry-run only",
        },
        "searxng": {
            "role": "local broad-recall baseline",
            "budget_class": "free/local",
            "default_use": "primary live baseline",
        },
        "agents-web-search": {
            "role": "OpenAI hosted corroboration",
            "budget_class": "OpenAI metered",
            "default_use": "capped lane; not expanded by default",
        },
        "exa": {
            "role": "semantic deepening",
            "budget_class": "configured free-tier or metered",
            "default_use": "policy-capped deepening",
        },
        "tavily": {
            "role": "research deepening",
            "budget_class": "free-tier-aware or metered",
            "default_use": "policy-capped deepening",
        },
        "firecrawl": {
            "role": "managed extraction/search",
            "budget_class": "configured free-tier or metered",
            "default_use": "explicit extraction/search lane",
        },
        "serper": {
            "role": "Google-style fallback",
            "budget_class": "metered and disabled by policy",
            "default_use": "disabled unless credits are restored",
        },
        "browserless": {
            "role": "rendered-page boundary",
            "budget_class": "metered candidate",
            "default_use": "eval/placeholder unless promoted",
        },
        "playwright": {
            "role": "read-only rendered diagnostics",
            "budget_class": "local compute",
            "default_use": "explicit diagnostics only",
        },
    }
    policy: list[dict[str, str]] = []
    for provider in dict.fromkeys(
        str(item).strip() for item in providers_used if str(item).strip()
    ):
        spec = specs.get(provider)
        if spec is None:
            continue
        policy.append({"provider": provider, **spec})
    return policy


def _compact_provider_use_ladder(
    metadata: Mapping[str, Any],
    source_coverage: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> list[dict[str, Any]]:
    website = (
        metadata.get("website_extraction")
        if isinstance(metadata.get("website_extraction"), Mapping)
        else {}
    )
    missing_lanes = (
        source_coverage.get("missing_lanes")
        or source_coverage.get("missing_source_lanes")
        or quality.get("missing_source_lanes")
        or []
    )
    request_text = str(
        metadata.get("request_text")
        or metadata.get("topic")
        or " ".join(_compact_search_queries(metadata.get("search_queries")))
    )
    extraction_status = str(website.get("status") or "").strip().lower()
    extraction_required = bool(
        metadata.get("requires_extraction")
        or website
        or metadata.get("website_extraction_summary")
    )
    ladder = build_provider_use_ladder(
        request_text=request_text,
        missing_source_lanes=[str(item) for item in missing_lanes if str(item).strip()],
        autonomy_hint=metadata.get("autonomy_hint") or metadata.get("retrieval_hint"),
        requires_extraction=extraction_required,
        extraction_quality=extraction_status or None,
        rendered_diagnostics_requested=bool(metadata.get("rendered_diagnostics_requested")),
        serper_enabled=bool(metadata.get("serper_enabled")),
    )
    return [
        {
            "provider": rule.provider,
            "stage": rule.stage,
            "use_frequency": rule.use_frequency,
            "use_now": rule.use_now,
            "trigger": rule.trigger,
            "reason_codes": list(rule.reason_codes),
        }
        for rule in ladder.rules
        if rule.use_now
        or rule.use_frequency
        in {
            "always_default",
            "default_capped",
            "conditional_deepening",
            "selected_url_baseline",
            "specific_fallback",
            "diagnostic_only",
            "disabled",
            "eval_only",
            "future_boundary",
        }
    ]


def _compact_source_limits(
    source_coverage: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> dict[str, Any]:
    missing_lanes = [
        str(item)
        for item in (
            source_coverage.get("missing_lanes")
            or source_coverage.get("missing_source_lanes")
            or quality.get("missing_source_lanes")
            or []
        )
        if str(item).strip()
    ]
    missing_domains = [
        str(item)
        for item in (
            source_coverage.get("missing_expected_domains")
            or quality.get("missing_expected_domains")
            or []
        )
        if str(item).strip()
    ]
    reasons = quality.get("reasons")
    reason_values = reasons if isinstance(reasons, list) else []
    return {
        "missing_lanes": missing_lanes[:8],
        "missing_expected_domains": missing_domains[:8],
        "official_source_present": bool(quality.get("official_source_present")),
        "needs_search_review": bool(quality.get("needs_search_review")),
        "source_verification_needed": bool(missing_lanes or missing_domains),
        "reasons": [str(item) for item in reason_values[:5]],
    }


def _compact_source_triage(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    decisions = value.get("decisions")
    decision_counts: dict[str, int] = {}
    decision_urls: dict[str, list[str]] = {
        "retain": [],
        "review": [],
        "deepen": [],
        "reject": [],
    }
    if isinstance(decisions, list):
        for item in decisions:
            if not isinstance(item, Mapping):
                continue
            decision = str(item.get("decision") or "").strip()
            if decision:
                decision_counts[decision] = decision_counts.get(decision, 0) + 1
                url = str(item.get("url") or "").strip()
                if url and decision in decision_urls and url not in decision_urls[decision]:
                    decision_urls[decision].append(url)
    return {
        "mode": str(value.get("mode") or ""),
        "decision_counts": decision_counts,
        "retained_count": len(value.get("retained_source_ids") or []),
        "review_count": len(value.get("review_source_ids") or []),
        "rejected_count": len(value.get("rejected_source_ids") or []),
        "deepen_count": len(value.get("deepen_source_ids") or []),
        "retained_urls": decision_urls["retain"][:8],
        "review_urls": decision_urls["review"][:8],
        "rejected_urls": decision_urls["reject"][:8],
        "deepen_urls": decision_urls["deepen"][:8],
        "needs_broaden_or_deepen": bool(value.get("needs_broaden_or_deepen")),
        "recommended_action": str(value.get("recommended_action") or ""),
        "recall_gaps": [
            str(item) for item in (value.get("recall_gaps") or []) if str(item).strip()
        ][:5],
    }


def _compact_provider_usage(provider_usage: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    compact: dict[str, dict[str, Any]] = {}
    for name, usage in provider_usage.items():
        if not isinstance(usage, Mapping):
            continue
        compact[str(name)] = {
            "requests_attempted": int(_safe_float(usage.get("requests_attempted"))),
            "requests_succeeded": int(_safe_float(usage.get("requests_succeeded"))),
            "requests_failed": int(_safe_float(usage.get("requests_failed"))),
            "credits_used": int(_safe_float(usage.get("credits_used"))),
            "input_tokens": int(_safe_float(usage.get("input_tokens"))),
            "cached_input_tokens": int(_safe_float(usage.get("cached_input_tokens"))),
            "output_tokens": int(_safe_float(usage.get("output_tokens"))),
            "reasoning_output_tokens": int(_safe_float(usage.get("reasoning_output_tokens"))),
            "estimated_usd": round(_safe_float(usage.get("estimated_usd")), 8),
            "total_seconds": round(_safe_float(usage.get("total_seconds")), 3),
        }
    return compact


def _compact_search_queries(value: Any) -> list[str]:
    if isinstance(value, str):
        queries = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        queries = [str(item or "") for item in value]
    else:
        return []
    return [query.strip()[:240] for query in queries[:6] if query.strip()]


def _compact_provider_queries(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(provider): _compact_search_queries(queries)
        for provider, queries in value.items()
        if str(provider).strip()
    }


def _compact_provider_result_samples(value: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(value, Mapping):
        return {}
    compact: dict[str, list[dict[str, str]]] = {}
    for provider, samples in value.items():
        provider_name = str(provider or "").strip()
        if not provider_name or not isinstance(samples, list):
            continue
        provider_samples: list[dict[str, str]] = []
        for sample in samples[:3]:
            if not isinstance(sample, Mapping):
                continue
            title = str(sample.get("title") or "").strip()
            url = str(sample.get("url") or "").strip()
            snippet = str(sample.get("snippet") or "").strip()
            if not title and not url:
                continue
            provider_samples.append(
                {
                    "title": title[:160],
                    "url": url[:500],
                    "snippet": snippet[:220],
                }
            )
        if provider_samples:
            compact[provider_name] = provider_samples
    return compact


def _compact_source_focus(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        "status": str(value.get("status") or ""),
        "terms": [str(item) for item in (value.get("terms") or [])[:8] if str(item).strip()],
        "selected_source_count": int(_safe_float(value.get("selected_source_count"))),
        "matching_source_count": int(_safe_float(value.get("matching_source_count"))),
        "matching_urls": [
            str(item)[:500] for item in (value.get("matching_urls") or [])[:5] if str(item).strip()
        ],
        "top_selected_urls": [
            str(item)[:500]
            for item in (value.get("top_selected_urls") or [])[:5]
            if str(item).strip()
        ],
    }


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
    lane_counts = (
        source_coverage.get("lane_counts")
        if isinstance(source_coverage.get("lane_counts"), dict)
        else {}
    )
    observed_lanes = source_coverage.get("observed_lanes")
    missing_lanes = source_coverage.get("missing_lanes")
    observed_domains = source_coverage.get("observed_domains")
    return {
        "selected_source_count": int(
            _safe_float(
                source_coverage.get("selected_source_count")
                or source_coverage.get("primary_source_count")
            )
        ),
        "credible_source_count": int(
            _safe_float(
                source_coverage.get("credible_source_count")
                or source_coverage.get("primary_source_count")
            )
        ),
        "official_source_count": int(
            _safe_float(
                source_coverage.get("official_source_count") or lane_counts.get("company_site")
            )
        ),
        "sufficiency_status": str(source_coverage.get("sufficiency_status") or ""),
        "primary_source_count": int(_safe_float(source_coverage.get("primary_source_count"))),
        "useful_unique_domain_count": int(
            _safe_float(source_coverage.get("useful_unique_domain_count"))
        ),
        "observed_lane_count": (
            len(observed_lanes) if isinstance(observed_lanes, list | tuple) else 0
        ),
        "observed_lanes": [str(item) for item in (observed_lanes or [])][:8],
        "missing_lanes": [str(item) for item in (missing_lanes or [])][:8],
        "observed_domain_count": (
            len(observed_domains) if isinstance(observed_domains, list | tuple) else 0
        ),
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
    universal_search_failure = _universal_search_failure_summary(metadata)
    if universal_search_failure:
        errors.append(universal_search_failure)
    for key in ("errors", "warnings"):
        value = metadata.get(key)
        if isinstance(value, list):
            errors.extend(str(item) for item in value if str(item).strip())
    website_errors = website.get("errors")
    if isinstance(website_errors, list):
        errors.extend(str(item) for item in website_errors if str(item).strip())
    return errors[:8]


def _universal_search_failure_summary(metadata: Mapping[str, Any]) -> str | None:
    provider_errors = metadata.get("search_provider_errors")
    if not isinstance(provider_errors, list) or not provider_errors:
        return None
    if int(_safe_float(metadata.get("raw_search_result_count"))) > 0:
        return None
    attempted = [
        str(item).strip()
        for item in (metadata.get("search_providers_attempted") or [])
        if str(item).strip()
    ]
    if not attempted:
        return None
    provider_usage = (
        metadata.get("provider_usage")
        if isinstance(metadata.get("provider_usage"), Mapping)
        else {}
    )
    succeeded = 0
    for provider_name in attempted:
        usage = provider_usage.get(provider_name)
        if isinstance(usage, Mapping):
            succeeded += int(_safe_float(usage.get("requests_succeeded")))
    failed_providers = {
        str(error.get("provider")).strip()
        for error in provider_errors
        if isinstance(error, Mapping) and str(error.get("provider")).strip()
    }
    if succeeded > 0 or not set(attempted).issubset(failed_providers):
        return None
    return (
        "Live search failed across all attempted providers; "
        "backend retrieval telemetry has provider details."
    )


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
                "extraction_status": item.get("extraction_status") or "snippet_only",
                "evidence_excerpt": item.get("evidence_excerpt", ""),
                "key_facts": item.get("key_facts") or [],
                "supported_claims": item.get("supported_claims") or [],
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def _company_source_triage_candidates(
    *,
    profile: CompanyProfile,
    search_results: list[Any],
    limit: int = 16,
) -> list[dict[str, Any]]:
    """Return selected/extracted company sources plus search candidates for triage."""

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, source in enumerate(list(getattr(profile, "sources", []) or [])[:8], start=1):
        url = str(getattr(source, "url", "") or "").strip()
        title = str(getattr(source, "title", "") or "").strip()
        key = url.rstrip("/") or title
        if not key or key in seen:
            continue
        seen.add(key)
        claims = [
            str(item or "").strip()
            for item in (getattr(source, "supported_claims", []) or [])
            if str(item or "").strip()
        ]
        excerpt = str(getattr(source, "evidence_excerpt", "") or "").strip()
        snippet = " ".join(part for part in [excerpt, *claims[:3]] if part).strip()
        extraction_status = _profile_source_extraction_status(
            source_type=str(getattr(source, "source_type", "") or ""),
            excerpt=excerpt,
            claims=claims,
        )
        candidates.append(
            {
                "source_id": f"selected:{index}",
                "title": title,
                "url": url,
                "snippet": snippet[:900],
                "source": str(getattr(source, "provider", "") or ""),
                "source_type": str(getattr(source, "source_type", "") or ""),
                "published_at": getattr(source, "published_at", None),
                "extraction_status": extraction_status,
                "evidence_excerpt": excerpt,
                "key_facts": [],
                "supported_claims": claims,
            }
        )
        if len(candidates) >= limit:
            return candidates

    for candidate in _compact_search_candidates(search_results, limit=limit):
        key = str(candidate.get("url") or "").rstrip("/") or str(candidate.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def _profile_source_extraction_status(
    *,
    source_type: str,
    excerpt: str,
    claims: Sequence[str],
) -> str:
    normalized = str(source_type or "").strip().lower()
    if normalized in {"google_search", "news", "unknown"}:
        return "snippet_only"
    if excerpt or claims:
        return "extracted"
    return "snippet_only"


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


def infer_official_company_url(
    *,
    company: str,
    search_results: Sequence[Any],
    request_text: str = "",
) -> str:
    """Infer a first-party base URL from brand identity plus request context."""

    company_slug = re.sub(r"[^a-z0-9]", "", str(company or "").lower())
    if len(company_slug) < 3:
        return ""
    request_focus_terms = {
        term
        for term in re.findall(r"[a-z0-9]{4,}", str(request_text or "").lower())
        if term
        not in {
            "about",
            "agent",
            "analyst",
            "answer",
            "brief",
            "bullet",
            "bullets",
            "business",
            "company",
            "compare",
            "concise",
            "create",
            "directly",
            "include",
            "modify",
            "official",
            "research",
            "source",
            "sources",
            "substantive",
            "verified",
        }
    }
    candidates: dict[str, dict[str, Any]] = {}
    for result in search_results:
        if isinstance(result, Mapping):
            raw_url = str(result.get("link") or result.get("url") or "").strip()
            title = str(result.get("title") or "")
            snippet = str(result.get("snippet") or result.get("content") or "")
        else:
            raw_url = str(
                getattr(result, "link", "") or getattr(result, "url", "") or ""
            ).strip()
            title = str(getattr(result, "title", "") or "")
            snippet = str(
                getattr(result, "snippet", "")
                or getattr(result, "content", "")
                or ""
            )
        if not raw_url:
            continue
        try:
            parsed = urlparse(raw_url)
            _ = parsed.port
            host = (parsed.hostname or "").lower().removeprefix("www.")
        except ValueError:
            continue
        registrable_domain = _registrable_domain(host)
        if not registrable_domain:
            continue
        registrable_label = registrable_domain.split(".", 1)[0]
        label_slug = re.sub(r"[^a-z0-9]", "", registrable_label)
        exact_brand_domain = label_slug == company_slug
        expanded_brand_domain = (
            label_slug.startswith(company_slug)
            and len(label_slug) >= len(company_slug) + 3
        )
        if not exact_brand_domain and not expanded_brand_domain:
            continue
        result_text = " ".join((title, snippet, raw_url)).lower()
        context_matches = {
            term for term in request_focus_terms if term in result_text
        }
        suffix = label_slug[len(company_slug) :] if expanded_brand_domain else ""
        suffix_matches_request = bool(
            suffix and any(term in suffix or suffix in term for term in request_focus_terms)
        )
        candidate = candidates.setdefault(
            registrable_domain,
            {
                "url": f"https://{registrable_domain}",
                "result_count": 0,
                "context_matches": set(),
                "exact_brand_domain": exact_brand_domain,
                "suffix_matches_request": False,
            },
        )
        candidate["result_count"] += 1
        candidate["context_matches"].update(context_matches)
        candidate["suffix_matches_request"] = bool(
            candidate["suffix_matches_request"] or suffix_matches_request
        )
    if not candidates:
        return ""
    eligible = [
        candidate
        for candidate in candidates.values()
        if candidate["exact_brand_domain"]
        or (
            candidate["context_matches"]
            and candidate["suffix_matches_request"]
        )
    ]
    if not eligible:
        return ""
    ranked = sorted(
        eligible,
        key=lambda item: (
            -len(item["context_matches"]),
            -int(item["suffix_matches_request"]),
            -item["result_count"],
            -int(item["exact_brand_domain"]),
            len(item["url"]),
            item["url"],
        ),
    )
    if len(company_slug) < 5 and ranked[0]["result_count"] < 2:
        return ""
    return str(ranked[0]["url"])


def company_source_matches_official_url(source_url: str, official_url: str) -> bool:
    """Return whether two URLs share the same registrable company domain."""

    try:
        source_parsed = urlparse(source_url)
        official_parsed = urlparse(official_url)
        # Accessing ``port`` validates malformed/non-numeric and out-of-range
        # values that ``hostname`` alone intentionally tolerates.
        _validated_ports = (source_parsed.port, official_parsed.port)
        source_domain = _registrable_domain(
            (source_parsed.hostname or "").lower()
        )
        official_domain = _registrable_domain(
            (official_parsed.hostname or "").lower()
        )
    except ValueError:
        return False
    return bool(source_domain and official_domain and source_domain == official_domain)


def _registrable_domain(host: str) -> str:
    labels = [
        label
        for label in str(host or "").lower().removeprefix("www.").split(".")
        if label
    ]
    if len(labels) < 2:
        return ""
    if len(labels) >= 3 and ".".join(labels[-2:]) in {
        "co.jp",
        "co.nz",
        "co.uk",
        "com.au",
    }:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def retrieve_company_profile_live(
    *,
    company: str,
    company_url: str | None = None,
    request_text: str | None = None,
    source_triage_agent_name: str = "business_research_analyst",
    requested_provider: str | None = None,
    max_results: int = 5,
    agents_web_search_max_calls: int | None = None,
    agents_web_search_parallel: bool | None = None,
    tavily_search_fallback: bool | None = None,
    exa_search_fallback: bool | None = None,
    extract_selected_pages: bool = True,
    website_extraction_max_pages: int | None = None,
    discover_internal_company_pages: bool = True,
    official_company_sources_only: bool = False,
    max_queries: int | None = None,
    retrieval_hint: RetrievalHint | None = None,
    settings_loader: Callable[[], Any] | None = None,
    query_builder: Callable[[str, str | None], list[str]] | None = None,
    search_provider_builder: Callable[..., Any] | None = None,
    profile_builder: Callable[..., CompanyProfile] | None = None,
    retrieval_deadline_seconds: float | None = None,
    clock: Callable[[], float] = perf_counter,
) -> tuple[CompanyProfile, dict[str, Any]]:
    """Run the hybrid live-search ladder for company research."""

    deadline = _ResearchRetrievalDeadline.start(
        retrieval_deadline_seconds,
        clock=clock,
    )
    total_started_at = clock()
    settings_loader = settings_loader or load_settings
    default_query_builder = query_builder is None
    query_builder = query_builder or build_company_research_queries
    search_provider_builder = search_provider_builder or build_search_provider
    profile_builder = profile_builder or research_account_from_search_results
    canonical_request_text = company_research_request_text(company, company_url)
    request_text = str(request_text or "").strip() or canonical_request_text
    autonomy_hint = derive_request_autonomy_hint(
        agent_name="business_research_analyst",
        request_text=request_text,
        agent_hint=retrieval_hint,
    )
    settings = settings_loader()
    search_config = build_shared_search_provider_config(
        requested_provider=requested_provider,
        configured_provider=settings.search_provider,
        serper_enabled=bool(getattr(settings, "serper_enabled", False)),
        agents_web_search_max_calls=agents_web_search_max_calls,
        agents_web_search_parallel=agents_web_search_parallel,
        tavily_search_fallback=tavily_search_fallback,
        exa_search_fallback=exa_search_fallback,
        request_text=request_text,
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

    queries = (
        query_builder(
            company,
            company_url,
            request_text=request_text,
        )
        if default_query_builder and _query_builder_accepts_request_text(query_builder)
        else query_builder(company, company_url)
    )
    if max_queries is not None:
        queries = queries[: max(1, max_queries)]
    request_focus_terms = _company_extraction_query_terms(company=company, queries=queries)
    search_results: list[Any] = []
    query_timings: list[dict[str, Any]] = []
    telemetry_packets: list[dict[str, Any]] = []
    with _maybe_transient_searxng_runtime(
        provider_sequence=search_config.provider_sequence,
        fallback_provider_sequence=search_config.deepening_provider_sequence,
        settings=settings,
        enabled=search_provider_builder is build_search_provider,
    ) as searxng_runtime:
        search_started_at = clock()
        search_concurrency = min(_company_search_concurrency(), max(1, len(queries)))
        if search_concurrency <= 1 or len(queries) <= 1:
            for index, query in enumerate(queries):
                if index > 0 and not deadline.allows(f"search_query_{index + 1}"):
                    break
                query_started_at = clock()
                results = client.search_web(query, num_results=max_results)
                query_timings.append(
                    {
                        "query": query,
                        "seconds": round(clock() - query_started_at, 3),
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
                query_started_at = clock()
                results = query_client.search_web(query, num_results=max_results)
                return (
                    index,
                    query,
                    results,
                    clock() - query_started_at,
                    query_client.telemetry(),
                )

            executor = ThreadPoolExecutor(max_workers=search_concurrency)
            futures = {
                executor.submit(run_query, index, query): (index, query)
                for index, query in enumerate(queries)
            }
            completed_futures: set[Any] = set()
            timed_out = False
            try:
                for future in as_completed(
                    futures,
                    timeout=deadline.bounded_timeout(
                        _company_search_total_timeout_seconds()
                    ),
                ):
                    completed_futures.add(future)
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
            except FuturesTimeoutError:
                timed_out = True
                for future, (_index, query) in futures.items():
                    if future in completed_futures:
                        continue
                    future.cancel()
                    query_timings.append(
                        {
                            "query": query,
                            "seconds": deadline.bounded_timeout(
                                _company_search_total_timeout_seconds()
                            ),
                            "result_count": 0,
                            "error": "company_search_total_timeout",
                        }
                    )
            finally:
                executor.shutdown(wait=not timed_out, cancel_futures=True)
            for results in ordered_results:
                search_results.extend(results)

        search_seconds = clock() - search_started_at
    inferred_company_url = (
        ""
        if company_url
        else infer_official_company_url(
            company=company,
            search_results=search_results,
            request_text=request_text,
        )
    )
    resolved_company_url = company_url or inferred_company_url or None
    entity_relevant_search_results = filter_company_search_results_for_entity(
        search_results,
        company_name=company,
        company_url=resolved_company_url,
        request_text=request_text,
    )
    profile_search_results = (
        [
            result
            for result in entity_relevant_search_results
            if resolved_company_url
            and company_source_matches_official_url(
                str(_jsonable_search_result(result).get("url") or ""),
                resolved_company_url,
            )
        ]
        if official_company_sources_only
        else entity_relevant_search_results
    )
    quality_started_at = clock()
    search_quality = assess_company_search_quality(
        results=search_results,
        company_name=company,
        company_url=resolved_company_url,
        request_text=request_text,
        autonomy_hint=autonomy_hint,
    )
    quality_seconds = clock() - quality_started_at
    metadata = _merge_company_search_telemetry(
        provider_sequence=search_config.provider_sequence,
        telemetry_packets=telemetry_packets or [client.telemetry()],
    )
    website_started_at = clock()
    if extract_selected_pages and deadline.allows("website_extraction"):
        website_inputs, website_errors, website_stats = _extract_company_website_inputs(
            company=company,
            company_url=resolved_company_url,
            search_results=search_results,
            queries=queries,
            provider=str(
                getattr(settings, "website_extractor", "trafilatura") or "trafilatura"
            ),
            total_timeout_seconds=deadline.remaining_seconds,
            clock=clock,
            max_pages=website_extraction_max_pages,
            discover_internal_pages=discover_internal_company_pages,
            official_company_only=official_company_sources_only,
        )
    elif extract_selected_pages:
        website_inputs, website_errors, website_stats = [], [
            "Website extraction skipped because the research deadline was reached."
        ], {
            "mode": "skipped_research_deadline",
            "providers_used": [],
            "pages_considered": 0,
            "page_count": 0,
            "claim_count": 0,
            "timed_out": True,
            "total_timeout_seconds": 0.0,
        }
    else:
        website_inputs, website_errors, website_stats = [], [], {
            "mode": "skipped_quick_retrieval",
            "providers_used": [],
            "pages_considered": 0,
            "page_count": 0,
            "claim_count": 0,
        }
    website_seconds = clock() - website_started_at
    profile_started_at = clock()
    profile_kwargs: dict[str, Any] = {
        "company_name": company,
        "company_url": resolved_company_url,
        "search_results": profile_search_results,
        "website_inputs": website_inputs,
    }
    if _profile_builder_accepts_request_focus(profile_builder):
        profile_kwargs["request_focus_terms"] = request_focus_terms
    profile = profile_builder(**profile_kwargs)
    profile_seconds = clock() - profile_started_at
    source_focus = _profile_source_focus_diagnostics(
        profile,
        request_focus_terms=request_focus_terms,
    )
    retrieved_source_candidates = _company_source_triage_candidates(
        profile=profile,
        search_results=profile_search_results,
        limit=max(12, max_results * 4),
    )
    source_triage = triage_source_candidates(
        request_text=request_text,
        candidates=retrieved_source_candidates,
        agent_name=source_triage_agent_name,
        max_retain=max_results,
    )
    metadata.update(
        {
            "mode": "live_search",
            "live_search": True,
            "search_provider": search_provider_label(metadata),
            "search_queries": queries,
            "request_focus_terms": request_focus_terms,
            "source_focus": source_focus,
            "retrieved_source_candidates": retrieved_source_candidates,
            "source_triage": source_triage.model_dump(mode="json"),
            "raw_search_result_count": len(search_results),
            "profile_search_result_count": len(profile_search_results),
            "entity_rejected_search_result_count": max(
                0,
                len(search_results) - len(entity_relevant_search_results),
            ),
            "resolved_company_url": resolved_company_url or "",
            "company_url_inferred": bool(inferred_company_url),
            "max_results": max_results,
            "searxng_transient_runtime": dict(searxng_runtime),
            "search_quality": search_quality.to_dict(),
            "source_coverage": search_quality.source_coverage,
            "timing": {
                "total_seconds": round(clock() - total_started_at, 3),
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
            "research_deadline": deadline.metadata(),
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
    if source_triage.needs_broaden_or_deepen:
        metadata["search_review_recommended"] = True
        quality = metadata.get("search_quality")
        if isinstance(quality, dict):
            reasons = [str(item) for item in (quality.get("reasons") or []) if str(item).strip()]
            reasons.extend(source_triage.recall_gaps)
            reasons.append("source triage recommended broader/deeper retrieval")
            quality["needs_search_review"] = True
            quality["reasons"] = list(dict.fromkeys(reasons))
    metadata["retrieval_diagnostics"] = retrieval_diagnostics_from_metadata(metadata)
    return profile, metadata


def _extract_company_website_inputs(
    *,
    company: str,
    company_url: str | None,
    search_results: list[Any],
    queries: list[str] | None = None,
    provider: str,
    total_timeout_seconds: float | None = None,
    clock: Callable[[], float] = perf_counter,
    max_pages: int | None = None,
    discover_internal_pages: bool = True,
    official_company_only: bool = False,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    provider_sequence = website_extraction_provider_sequence(primary_provider=provider)
    fallback_provider = ",".join(provider_sequence[1:])
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
        queries=queries or [],
        max_pages=max_pages,
        official_company_only=official_company_only,
    )
    resolved_max_pages = max_pages or _website_extraction_max_pages()
    if company_url and discover_internal_pages:
        discovered_urls = discover_company_page_urls(
            company_url,
            company_name=company,
            live=True,
            max_urls=resolved_max_pages,
        )
        base_stats["internal_page_discovery_count"] = len(discovered_urls)
        urls = list(dict.fromkeys([*discovered_urls, *urls]))[:resolved_max_pages]
    base_stats["deferred_selected_urls"] = urls[
        MAX_SELECTED_URLS_PER_BUNDLE:MAX_SELECTED_URLS_PER_BUNDLE * 2
    ]
    base_stats["additional_unprocessed_url_count"] = max(
        0, len(urls) - MAX_SELECTED_URLS_PER_BUNDLE * 2
    )
    urls = urls[:MAX_SELECTED_URLS_PER_BUNDLE]
    base_stats["pages_considered"] = len(urls)
    if len(json.dumps([*urls, *base_stats["deferred_selected_urls"]])) > 24000:
        return [], ["Company URL identifiers exceed the response budget; reduce the page scope."], {
            **base_stats, "deferred_selected_urls": [], "partial": True,
            "additional_unprocessed_url_count": (
                len(urls) + len(base_stats["deferred_selected_urls"])
                + base_stats["additional_unprocessed_url_count"]
            ),
        }
    website_inputs: list[dict[str, Any]] = []
    errors: list[str] = []
    html_review_attempts = 0
    html_review_claim_count = 0
    configured_timeout = _website_extraction_total_timeout_seconds()
    effective_timeout = (
        configured_timeout
        if total_timeout_seconds is None
        else max(0.0, min(configured_timeout, total_timeout_seconds))
    )
    extraction_started_at = clock()
    extraction_timed_out = False
    extraction_budget = website_extraction_budget()
    for index, url in enumerate(urls, start=1):
        if clock() - extraction_started_at >= effective_timeout:
            extraction_timed_out = True
            errors.append(
                "Website extraction stopped after "
                f"{effective_timeout:.1f}s total budget; "
                f"{max(0, len(urls) - index + 1)} page(s) were not extracted."
            )
            break
        try:
            result = extract_website_content_with_fallbacks(
                url,
                company_name=company,
                primary_provider=provider,
                guardrail_context="public_web_source",
                live=True,
                budget=extraction_budget,
                extractor=extract_website_content,
            )
        except Exception as exc:
            errors.append(f"{url}: {exc}"[:500])
            continue
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
                errors.append(f"{url}: agent HTML review failed: {exc}"[:500])
            except Exception as exc:
                errors.append(f"{url}: agent HTML review unavailable: {exc}"[:500])
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
        if not result.text_or_markdown.strip() and not result.claims:
            errors.append(f"{url}: extraction returned no readable source text."[:500])
            continue
        excerpt, access = project_web_source(
            result, source_id=f"website_extract:{index}", selected_url=url,
            max_chars=min(6000, MAX_WEB_PREVIEW_TOTAL_CHARS // max(1, len(urls))),
        )
        website_inputs.append(
            {
                "source_id": f"website_extract:{index}",
                "title": (result.title or f"{company} website page")[:300],
                "url": url,
                "source_type": "website",
                "supported_claims": [claim for claim in result.claims if len(claim) <= 280][:10],
                "text_or_markdown": excerpt,
                "evidence_excerpt": excerpt,
                "web_source_access": access.model_dump(mode="json"),
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
        "timed_out": extraction_timed_out,
        "total_timeout_seconds": effective_timeout,
        "firecrawl_call_cap": extraction_budget.firecrawl_max_calls,
        "firecrawl_calls_attempted": extraction_budget.firecrawl_calls_attempted,
    }
    while len(json.dumps([website_inputs, errors, stats])) > MAX_WEB_BUNDLE_RESPONSE_CHARS:
        deferred = website_inputs.pop()
        stats["deferred_selected_urls"].insert(0, deferred["url"])
        stats["page_count"] = len(website_inputs)
        stats["claim_count"] = sum(len(item["supported_claims"]) for item in website_inputs)
        stats["providers_used"] = list(dict.fromkeys(item["provider"] for item in website_inputs))
    return website_inputs, errors, stats


def _company_website_extraction_urls(
    *,
    company: str,
    company_url: str | None,
    search_results: list[Any],
    queries: list[str] | None = None,
    max_pages: int | None = None,
    official_company_only: bool = False,
) -> list[str]:
    resolved_max_pages = max_pages or _website_extraction_max_pages()
    if official_company_only and not company_url:
        return []
    candidates: list[tuple[str, int, int]] = []
    if company_url:
        for position, url in enumerate(default_company_page_urls(company_url)):
            candidates.append((url, 0, position))
    query_terms = _company_extraction_query_terms(company=company, queries=queries or [])
    for position, result in enumerate(search_results, start=len(candidates)):
        item = _jsonable_search_result(result)
        url = str(item.get("url") or "").strip()
        if not url or not _looks_like_company_page(url, company=company):
            continue
        if (
            official_company_only
            and company_url
            and not company_source_matches_official_url(url, company_url)
        ):
            continue
        score = _company_page_focus_score(item, query_terms=query_terms)
        if official_company_only:
            # Prefer discovered official pages that match the request over
            # generic homepage/about guesses. Default URLs remain bounded
            # fallbacks when search does not expose enough official pages.
            score += 1000
        candidates.append((url, score, position))
    deduped: dict[str, tuple[str, int, int]] = {}
    for url, score, position in candidates:
        key = url.rstrip("/")
        existing = deduped.get(key)
        if existing is None or (score, -position) > (existing[1], -existing[2]):
            deduped[key] = (url, score, position)
    ordered = sorted(deduped.values(), key=lambda item: (-item[1], item[2]))
    return [url for url, _score, _position in ordered[:resolved_max_pages]]


def _company_extraction_query_terms(*, company: str, queries: list[str]) -> list[str]:
    company_terms = {
        part.lower() for part in re.findall(r"\b[A-Za-z][A-Za-z0-9-]{2,}\b", str(company or ""))
    }
    generic = {
        "2026",
        "about",
        "announcement",
        "company",
        "coverage",
        "current",
        "independent",
        "latest",
        "news",
        "official",
        "recent",
        "site",
        "update",
    }
    terms: list[str] = []
    for query in queries[:6]:
        for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9-]{3,}\b", str(query or "")):
            term = match.group(0).lower()
            if term in company_terms or term in generic or re.fullmatch(r"20\d{2}", term):
                continue
            terms.append(term)
    return list(dict.fromkeys(terms))[:8]


def _profile_builder_accepts_request_focus(builder: Callable[..., Any]) -> bool:
    try:
        parameters = signature(builder).parameters
    except (TypeError, ValueError):
        return True
    return "request_focus_terms" in parameters or any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _query_builder_accepts_request_text(builder: Callable[..., Any]) -> bool:
    try:
        parameters = signature(builder).parameters
    except (TypeError, ValueError):
        return True
    return "request_text" in parameters or any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _profile_source_focus_diagnostics(
    profile: CompanyProfile,
    *,
    request_focus_terms: list[str],
) -> dict[str, Any]:
    terms = [str(term or "").strip().lower() for term in request_focus_terms if str(term).strip()]
    selected_sources = list(getattr(profile, "sources", []) or [])[:12]
    if not terms:
        status = "no_focus_terms"
    elif not selected_sources:
        status = "no_selected_sources"
    else:
        status = "matched_selected_sources"
    matching: list[Any] = []
    for source in selected_sources:
        if _source_record_focus_score(source, query_terms=terms) > 0:
            matching.append(source)
    if terms and selected_sources and not matching:
        status = "no_selected_source_matches_focus"
    return {
        "status": status,
        "terms": terms[:8],
        "selected_source_count": len(selected_sources),
        "matching_source_count": len(matching),
        "matching_urls": [str(getattr(source, "url", "") or "") for source in matching[:5]],
        "top_selected_urls": [
            str(getattr(source, "url", "") or "") for source in selected_sources[:5]
        ],
    }


def _source_record_focus_score(source: Any, *, query_terms: list[str]) -> int:
    if not query_terms:
        return 0
    url = str(getattr(source, "url", "") or "").lower()
    title = str(getattr(source, "title", "") or "").lower()
    claims = " ".join(str(item or "") for item in (getattr(source, "supported_claims", []) or []))
    excerpt = str(getattr(source, "evidence_excerpt", "") or "")
    haystack = f"{url} {title} {claims.lower()} {excerpt.lower()}"
    score = 0
    for term in query_terms:
        if term in url:
            score += 4
        if term in title:
            score += 3
        if term in claims.lower():
            score += 2
        if term in excerpt.lower():
            score += 2
        if term in haystack:
            score += 1
    return score


def _company_page_focus_score(item: Mapping[str, Any], *, query_terms: list[str]) -> int:
    if not query_terms:
        return 0
    url = str(item.get("url") or "").lower()
    title = str(item.get("title") or "").lower()
    snippet = str(item.get("snippet") or "").lower()
    haystack = f"{url} {title} {snippet}"
    score = 0
    for term in query_terms:
        if term in url:
            score += 4
        if term in title:
            score += 3
        if term in snippet:
            score += 2
        if term in haystack:
            score += 1
    if "/index/" in url or "/news" in url or "/blog" in url:
        score += 1
    if url.rstrip("/").endswith(("openai.com", "openai.com/about")):
        score -= 1
    return score


def _website_extraction_max_pages() -> int:
    raw = os.getenv("KEYSTONE_WEBSITE_EXTRACTION_MAX_PAGES", "8").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 4
    return max(1, min(8, value))


def _company_search_total_timeout_seconds() -> float:
    raw = os.getenv("KEYSTONE_COMPANY_SEARCH_TOTAL_TIMEOUT_SECONDS", "60").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 60.0
    return max(5.0, min(300.0, value))


def _website_extraction_total_timeout_seconds() -> float:
    raw = os.getenv("KEYSTONE_WEBSITE_EXTRACTION_TOTAL_TIMEOUT_SECONDS", "75").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 75.0
    return max(5.0, min(300.0, value))


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
    agents_web_search_max_calls: int | None = None,
    agents_web_search_parallel: bool | None = None,
    tavily_search_fallback: bool | None = None,
    exa_search_fallback: bool | None = None,
    exa_search_max_calls: int | None = None,
    serper_enabled: bool = False,
    request_text: str = "",
) -> SharedSearchProviderConfig:
    """Resolve the shared live-search policy for search-heavy Keystone agents."""

    requested_provider = resolve_requested_search_provider(
        request_text=request_text,
        requested_provider=requested_provider,
    )
    provider_sequence = build_provider_sequence(
        requested_provider=requested_provider,
        configured_provider=configured_provider,
        fallback_provider=fallback_provider,
        serper_enabled=serper_enabled,
    )
    agents_enabled = _env_bool("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", default=True)
    parallel_agents_enabled = agents_enabled and (
        agents_web_search_parallel
        if agents_web_search_parallel is not None
        else _env_bool("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", default=True)
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
        tavily_enabled=tavily_search_fallback,
        exa_enabled=exa_search_fallback,
    )
    return SharedSearchProviderConfig(
        provider_sequence=provider_sequence,
        deepening_provider_sequence=deepening_provider_sequence,
        provider_request_budget=_optional_search_request_budget(
            agents_enabled=agents_enabled,
            agents_max_calls=agents_web_search_max_calls,
            tavily_enabled=tavily_search_fallback,
            exa_enabled=exa_search_fallback,
            exa_max_calls=exa_search_max_calls,
        ),
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
    if requested == "agents-web-search":
        return provider_sequence
    if "agents-web-search" in provider_sequence:
        return provider_sequence
    if "searxng" not in provider_sequence:
        return (*provider_sequence, "agents-web-search")
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
    tavily_enabled: bool | None = None,
    exa_enabled: bool | None = None,
) -> tuple[str, ...]:
    requested = (requested_provider or "").strip().lower()
    providers: list[str] = []
    use_exa = (
        _env_bool("KEYSTONE_EXA_SEARCH_FALLBACK", default=False)
        if exa_enabled is None
        else exa_enabled
    )
    if use_exa and requested != "exa":
        providers.append("exa")
    use_tavily = (
        _env_bool("KEYSTONE_TAVILY_SEARCH_FALLBACK", default=False)
        if tavily_enabled is None
        else tavily_enabled
    )
    if use_tavily and requested != "tavily":
        providers.append("tavily")
    if agents_enabled and requested in {"", "searxng"}:
        providers.append("agents-web-search")
    return tuple(
        dict.fromkeys(
            provider_name for provider_name in providers if provider_name not in provider_sequence
        )
    )


def _optional_search_request_budget(
    *,
    agents_enabled: bool,
    agents_max_calls: int | None = None,
    tavily_enabled: bool | None = None,
    exa_enabled: bool | None = None,
    exa_max_calls: int | None = None,
) -> ProviderRequestBudget | None:
    limits: dict[str, int] = {}
    if agents_enabled:
        limits["agents-web-search"] = _agents_web_search_max_calls(agents_max_calls)
    use_tavily = (
        _env_bool("KEYSTONE_TAVILY_SEARCH_FALLBACK", default=False)
        if tavily_enabled is None
        else tavily_enabled
    )
    if use_tavily:
        limits["tavily"] = _tavily_search_max_calls()
    use_exa = (
        _env_bool("KEYSTONE_EXA_SEARCH_FALLBACK", default=False)
        if exa_enabled is None
        else exa_enabled
    )
    if use_exa:
        limits["exa"] = _exa_search_max_calls(exa_max_calls)
    return ProviderRequestBudget(limits) if limits else None


def _agents_web_search_request_budget(
    *,
    enabled: bool,
    max_calls: int | None = None,
) -> ProviderRequestBudget | None:
    if not enabled:
        return None
    return ProviderRequestBudget({"agents-web-search": _agents_web_search_max_calls(max_calls)})


def _agents_web_search_max_calls(max_calls: int | None = None) -> int:
    if max_calls is None:
        raw = os.getenv("KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN", "").strip()
        try:
            cap = int(raw) if raw else DEFAULT_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN
        except ValueError:
            cap = DEFAULT_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN
    else:
        cap = int(max_calls)
    return max(0, cap)


def _exa_search_max_calls(max_calls: int | None = None) -> int:
    if max_calls is None:
        raw = os.getenv("KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN", "").strip()
        try:
            cap = int(raw) if raw else DEFAULT_EXA_SEARCH_MAX_CALLS_PER_RUN
        except ValueError:
            cap = DEFAULT_EXA_SEARCH_MAX_CALLS_PER_RUN
    else:
        cap = int(max_calls)
    return max(0, cap)


def _tavily_search_max_calls() -> int:
    raw = os.getenv("KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN", "").strip()
    try:
        cap = int(raw) if raw else DEFAULT_TAVILY_SEARCH_MAX_CALLS_PER_RUN
    except ValueError:
        cap = DEFAULT_TAVILY_SEARCH_MAX_CALLS_PER_RUN
    return max(0, cap)


@contextmanager
def _maybe_transient_searxng_runtime(
    *,
    provider_sequence: tuple[str, ...],
    fallback_provider_sequence: tuple[str, ...] = (),
    settings: Any,
    enabled: bool = True,
) -> Any:
    """Start repo-local SearXNG for one live run, then stop it if we started it."""

    metadata: dict[str, Any] = {
        "enabled": False,
        "started": False,
        "stopped": False,
        "start_attempted": False,
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
    metadata["start_attempted"] = True
    try:
        _run_searxng_lifecycle_command("start")
    except RuntimeError as exc:
        fallback_providers = tuple(
            dict.fromkeys(
                provider_name
                for provider_name in (*provider_sequence, *fallback_provider_sequence)
                if provider_name != "searxng"
            )
        )
        if not fallback_providers:
            raise
        metadata.update(
            {
                "reason": "start_failed_fallback_available",
                "startup_error_type": type(exc).__name__,
                "startup_error": "SearXNG transient startup did not become ready.",
                "fallback_providers": list(fallback_providers),
            }
        )
        yield metadata
        return
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
    raw_timeout = os.getenv("KEYSTONE_SEARXNG_LIFECYCLE_TIMEOUT_SECONDS", "").strip()
    try:
        timeout_seconds = (
            float(raw_timeout)
            if raw_timeout
            else DEFAULT_SEARXNG_LIFECYCLE_TIMEOUT_SECONDS
        )
    except ValueError:
        timeout_seconds = DEFAULT_SEARXNG_LIFECYCLE_TIMEOUT_SECONDS
    timeout_seconds = max(1.0, min(60.0, timeout_seconds))
    try:
        completed = subprocess.run(
            [str(script), command],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        if check:
            raise RuntimeError(
                "SearXNG transient "
                f"{command} exceeded its {timeout_seconds:.1f}s startup budget; "
                "continue with configured fallback providers."
            ) from exc
        return
    if check and completed.returncode != 0:
        details = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip()
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
    provider_queries: dict[str, list[str]] = {}
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
                        "input_tokens": 0,
                        "cached_input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_output_tokens": 0,
                        "estimated_usd": 0.0,
                        "total_seconds": 0.0,
                    },
                )
                for key in (
                    "requests_attempted",
                    "requests_succeeded",
                    "raw_result_count",
                    "credits_used",
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                ):
                    target[key] = int(target[key]) + int(float(usage.get(key) or 0))
                target["estimated_usd"] = round(
                    float(target.get("estimated_usd") or 0.0)
                    + float(usage.get("estimated_usd") or 0.0),
                    8,
                )
                target["total_seconds"] = round(
                    float(target["total_seconds"]) + float(usage.get("total_seconds") or 0),
                    3,
                )
        raw_provider_queries = packet.get("provider_queries")
        if isinstance(raw_provider_queries, dict):
            for provider_name, queries in raw_provider_queries.items():
                target_queries = provider_queries.setdefault(str(provider_name), [])
                for query in _compact_search_queries(queries):
                    if query not in target_queries:
                        target_queries.append(query)
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
        "provider_queries": provider_queries,
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
    agents_web_search_max_calls: int | None = None,
    agents_web_search_parallel: bool | None = None,
    retrieval_hint: RetrievalHint | None = None,
    settings_loader: Callable[[], Any] | None = None,
    search_provider_builder: Callable[..., Any] | None = None,
) -> HybridSearchProvider:
    """Build the hybrid live-search provider for Opportunity Scout."""

    settings_loader = settings_loader or load_settings
    search_provider_builder = search_provider_builder or build_search_provider
    request_text = opportunity_scout_request_text(topic)
    settings = settings_loader()
    autonomy_hint = derive_request_autonomy_hint(
        agent_name="opportunity_scout",
        request_text=request_text,
        agent_hint=retrieval_hint,
    )
    tavily_search_fallback = _opportunity_tavily_deepening_enabled(
        topic=topic,
        settings=settings,
    )
    search_config = build_shared_search_provider_config(
        requested_provider=requested_provider,
        configured_provider=settings.search_provider,
        fallback_provider=fallback_provider,
        serper_enabled=bool(getattr(settings, "serper_enabled", False)),
        agents_web_search_max_calls=agents_web_search_max_calls,
        agents_web_search_parallel=agents_web_search_parallel,
        tavily_search_fallback=tavily_search_fallback,
        request_text=request_text,
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


def _opportunity_tavily_deepening_enabled(*, topic: str | None, settings: Any) -> bool | None:
    if _env_bool("KEYSTONE_TAVILY_SEARCH_FALLBACK", default=False):
        return True
    if not _formal_opportunity_topic(topic):
        return None
    return bool(str(getattr(settings, "tavily_api_key", "") or "").strip())


def _formal_opportunity_topic(topic: str | None) -> bool:
    text = f" {str(topic or '').lower()} "
    formal_markers = (
        " grant",
        " grants",
        " nofo",
        " foa",
        " rfa",
        " sbir",
        " sttr",
        " rfp",
        " rfps",
        " request for proposal",
        " solicitation",
        " procurement",
        " pilot",
        " pilots",
        " call for proposals",
        " call-for-proposals",
        " cfp",
        " cfps",
    )
    return sum(1 for marker in formal_markers if marker in text) >= 2


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
    agents_web_search_max_calls: int | None = None,
    agents_web_search_parallel: bool | None = None,
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
    verify_source_pages: bool | None = None,
    retrieval_deadline_seconds: float | None = None,
    clock: Callable[[], float] = perf_counter,
) -> tuple[OpportunityScoutResult, dict[str, Any]]:
    """Run Opportunity Scout live search with orchestrator-aware retrieval hints."""

    settings_loader = settings_loader or load_settings
    settings = settings_loader()
    provider = build_opportunity_search_provider(
        topic=topic,
        requested_provider=requested_provider,
        fallback_provider=fallback_provider,
        desired_results=max_results,
        agents_web_search_max_calls=agents_web_search_max_calls,
        agents_web_search_parallel=agents_web_search_parallel,
        retrieval_hint=retrieval_hint,
        settings_loader=lambda: settings,
        search_provider_builder=search_provider_builder,
    )
    effective_provider_builder = search_provider_builder or build_search_provider
    provider_sequence = tuple(getattr(provider, "provider_sequence", ()) or ())
    with _maybe_transient_searxng_runtime(
        provider_sequence=provider_sequence,
        fallback_provider_sequence=tuple(
            getattr(provider, "deepening_provider_sequence", ()) or ()
        ),
        settings=settings,
        enabled=(effective_provider_builder is build_search_provider and bool(provider_sequence)),
    ) as searxng_runtime:
        result = scout_opportunities_live_search(
            topic=topic,
            max_results=max_results,
            search_plan=search_plan,
            search_provider=provider,
            save=save,
            existing_state=existing_state,
            verify_source_pages=verify_source_pages,
            retrieval_deadline_seconds=retrieval_deadline_seconds,
            clock=clock,
        )
        collected_results = provider.collected_results()
        metadata = provider.telemetry()
    retrieved_source_candidates = _compact_search_candidates(
        collected_results,
        limit=_sandbox_search_review_source_limit(max_results),
    )
    source_triage = triage_source_candidates(
        request_text=topic or "",
        candidates=retrieved_source_candidates,
        agent_name="opportunity_scout",
        max_retain=max_results,
    )
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
            "retrieved_source_candidates": retrieved_source_candidates,
            "source_triage": source_triage.model_dump(mode="json"),
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
    if (
        _should_auto_recommend_sandbox_search_review(
            result=result,
            metadata=metadata,
            max_results=max_results,
        )
        or source_triage.needs_broaden_or_deepen
    ):
        metadata["search_review_recommended"] = True
        quality = metadata.get("search_quality")
        if isinstance(quality, dict):
            reasons = [str(item) for item in (quality.get("reasons") or []) if str(item).strip()]
            if len(result.records) < max(1, min(max_results, 2)):
                reasons.append("opportunity objective was not met after deterministic retrieval")
            if source_triage.needs_broaden_or_deepen:
                reasons.extend(source_triage.recall_gaps)
                reasons.append("source triage recommended broader/deeper retrieval")
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
