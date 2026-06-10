"""Compatibility wrappers for Serper search and the SDK search tool."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from keystone_agents.config import parse_bool
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.retrieval_policy import (
    HybridSearchProvider,
    ProviderRequestBudget,
    assess_company_search_quality,
    derive_request_autonomy_hint,
)
from keystone_agents.sdk import function_tool
from keystone_agents.tools.search_provider import (
    DEFAULT_TIMEOUT_SECONDS,
    SERPER_SEARCH_URL,
    ExaConfigurationError,
    ExaSearchError,
    ExaSearchProvider,
    FirecrawlConfigurationError,
    FirecrawlSearchError,
    FirecrawlSearchProvider,
    SearchProviderConfigurationError,
    SearchProviderError,
    SearchProviderName,
    SearchRequest,
    SearchResult,
    SearxngConfigurationError,
    SearxngSearchError,
    SearxngSearchProvider,
    SerperConfigurationError,
    SerperSearchError,
    SerperSearchProvider,
    build_search_provider,
    normalize_search_provider_name,
    requests,
)

PRIMARY_SEARCH_LANES = (
    SearchProviderName.SEARXNG.value,
    SearchProviderName.AGENTS_WEB_SEARCH.value,
    SearchProviderName.EXA.value,
    SearchProviderName.TAVILY.value,
)
_SDK_SEARCH_TELEMETRY: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "keystone_sdk_search_telemetry",
    default=None,
)
_SDK_SEARCH_REQUEST_CONTEXT: ContextVar[str] = ContextVar(
    "keystone_sdk_search_request_context",
    default="",
)
_SDK_SEARCH_REQUEST_BUDGET: ContextVar[ProviderRequestBudget | None] = ContextVar(
    "keystone_sdk_search_request_budget",
    default=None,
)


def reset_sdk_search_telemetry() -> None:
    """Start a fresh per-run SDK search telemetry buffer."""

    _SDK_SEARCH_TELEMETRY.set([])
    _SDK_SEARCH_REQUEST_CONTEXT.set("")
    _SDK_SEARCH_REQUEST_BUDGET.set(None)


def set_sdk_search_request_context(request_text: str) -> None:
    """Attach the original SDK request text for provider-selection hints."""

    _SDK_SEARCH_REQUEST_CONTEXT.set(str(request_text or ""))


def consume_sdk_search_telemetry() -> list[dict[str, Any]]:
    """Return and clear SDK search telemetry captured in this context."""

    packets = list(_SDK_SEARCH_TELEMETRY.get() or [])
    _SDK_SEARCH_TELEMETRY.set(None)
    _SDK_SEARCH_REQUEST_CONTEXT.set("")
    _SDK_SEARCH_REQUEST_BUDGET.set(None)
    return packets


def sdk_search_diagnostics_from_telemetry(
    telemetry_packets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build Slack-safe provider diagnostics from SDK search telemetry packets."""

    if not telemetry_packets:
        return {}
    provider_sequence: list[str] = []
    deepening_sequence: list[str] = []
    attempted: list[str] = []
    used: list[str] = []
    queries: list[str] = []
    errors: list[dict[str, Any]] = []
    provider_usage: dict[str, dict[str, Any]] = {}
    provider_result_samples: dict[str, list[dict[str, str]]] = {}
    for packet in telemetry_packets:
        provider_sequence.extend(_string_items(packet.get("search_provider_sequence")))
        deepening_sequence.extend(_string_items(packet.get("search_deepening_provider_sequence")))
        queries.extend(_string_items(packet.get("search_queries")))
        attempted.extend(_string_items(packet.get("search_providers_attempted")))
        used.extend(_string_items(packet.get("search_providers_used")))
        packet_errors = packet.get("search_provider_errors")
        if isinstance(packet_errors, list):
            errors.extend(item for item in packet_errors if isinstance(item, dict))
        usage = packet.get("provider_usage")
        if isinstance(usage, dict):
            _merge_provider_usage(provider_usage, usage)
        samples = packet.get("provider_result_samples")
        if isinstance(samples, dict):
            _merge_provider_result_samples(provider_result_samples, samples)
    provider_sequence = _dedupe(provider_sequence)
    deepening_sequence = _dedupe(deepening_sequence)
    attempted = _dedupe(attempted)
    used = _dedupe(used)
    queries = _dedupe(queries)
    configured = _dedupe([*provider_sequence, *deepening_sequence])
    provider_summary = "+".join(used or configured)
    return {
        "mode": "sdk_search_web",
        "live_search": bool(attempted or used),
        "provider_summary": provider_summary,
        "search_provider_sequence": provider_sequence,
        "search_deepening_provider_sequence": deepening_sequence,
        "search_queries": queries,
        "search_providers_attempted": attempted,
        "providers_used": used,
        "provider_usage": provider_usage,
        "provider_result_samples": provider_result_samples,
        "search_provider_errors": _compact_errors(errors),
        "primary_lane_statuses": _primary_lane_statuses(
            configured=configured,
            attempted=attempted,
            used=used,
            provider_usage=provider_usage,
        ),
    }


def _record_sdk_search_telemetry(provider: Any) -> None:
    telemetry = getattr(provider, "telemetry", None)
    if not callable(telemetry):
        return
    packet = telemetry()
    if not isinstance(packet, dict):
        return
    packets = _SDK_SEARCH_TELEMETRY.get()
    if packets is None:
        return
    packets.append(packet)
    _SDK_SEARCH_TELEMETRY.set(packets)


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


def _merge_provider_usage(
    target: dict[str, dict[str, Any]],
    usage: dict[str, Any],
) -> None:
    numeric_fields = {
        "requests_attempted",
        "requests_succeeded",
        "requests_failed",
        "raw_result_count",
        "credits_used",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "estimated_usd",
        "total_seconds",
    }
    for provider_name, raw in usage.items():
        if not isinstance(raw, dict):
            continue
        name = str(provider_name).strip()
        if not name:
            continue
        bucket = target.setdefault(name, {})
        for field in numeric_fields:
            value = raw.get(field)
            if value is None:
                continue
            bucket[field] = round(float(bucket.get(field) or 0) + _safe_float(value), 8)


def _merge_provider_result_samples(
    target: dict[str, list[dict[str, str]]],
    samples: dict[str, Any],
) -> None:
    for provider_name, raw_items in samples.items():
        name = str(provider_name).strip()
        if not name or not isinstance(raw_items, list):
            continue
        bucket = target.setdefault(name, [])
        seen = {item.get("url") or item.get("title") for item in bucket}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            item = {
                "title": str(raw_item.get("title") or "").strip()[:160],
                "url": str(raw_item.get("url") or "").strip()[:500],
                "snippet": str(raw_item.get("snippet") or "").strip()[:220],
            }
            key = item["url"] or item["title"]
            if not key or key in seen:
                continue
            bucket.append(item)
            seen.add(key)
            if len(bucket) >= 3:
                break


def _compact_errors(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    compact: list[dict[str, str]] = []
    for error in errors[:8]:
        compact.append(
            {
                "provider": str(error.get("provider") or "").strip(),
                "error_type": str(error.get("error_type") or type(error).__name__).strip(),
            }
        )
    return [item for item in compact if item["provider"] or item["error_type"]]


def _primary_lane_statuses(
    *,
    configured: list[str],
    attempted: list[str],
    used: list[str],
    provider_usage: dict[str, dict[str, Any]],
) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for lane in PRIMARY_SEARCH_LANES:
        usage = provider_usage.get(lane) or {}
        if lane in used:
            statuses[lane] = "used"
        elif lane in attempted:
            failed = int(_safe_float(usage.get("requests_failed")))
            statuses[lane] = "failed" if failed else "attempted_no_results"
        elif lane in configured:
            statuses[lane] = "configured_not_attempted"
        else:
            statuses[lane] = "not_configured"
    return statuses


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class SerperTool:
    """Small Serper client preserved for existing call sites."""

    live: bool = False
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def search(
        self,
        query: str,
        num_results: int = 5,
        *,
        max_results: int | None = None,
        http_post: Callable[..., Any] | None = None,
    ) -> list[SearchResult] | dict[str, Any]:
        """Search Serper when live mode is enabled, otherwise return no results."""

        return SerperSearchProvider(
            live=self.live,
            api_key=self.api_key,
            timeout_seconds=self.timeout_seconds,
        ).search(
            query=query,
            num_results=num_results,
            max_results=max_results,
            http_post=http_post,
        )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_web(query: str, num_results: int = 5) -> list[SearchResult]:
    """SDK search tool with safe defaults and env-gated live retrieval.

    By default this tool is inert. When the operator explicitly enables live research through the
    Keystone environment flags, it follows the shared live search ladder for SDK runs:
    SearXNG broad recall plus capped hosted Agents web-search and Exa lanes when enabled,
    with Serper/Tavily/Firecrawl only when `SEARCH_PROVIDER` selects them explicitly.
    """

    provider_name = _sdk_live_search_provider_name()
    provider = _sdk_live_search_provider(query, provider_name=provider_name)
    results = provider.search_web(query=query, num_results=num_results)
    _record_sdk_search_telemetry(provider)
    return results


def _sdk_live_search_provider(
    query: str,
    *,
    provider_name: SearchProviderName | None,
) -> Any:
    if provider_name is None:
        return build_search_provider(provider=None, live=False)
    raw_provider = os.getenv("SEARCH_PROVIDER")
    requested_provider = (raw_provider or "").strip().lower()
    use_default_searxng_ladder = (
        provider_name == SearchProviderName.SEARXNG
        and requested_provider in {"", SearchProviderName.SEARXNG.value}
    )
    if use_default_searxng_ladder and (
        _sdk_agents_web_search_enabled()
        or _sdk_exa_search_enabled()
        or _sdk_tavily_search_enabled(query)
    ):
        parallel_enabled = _env_bool("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", default=True)
        provider_sequence = [SearchProviderName.SEARXNG.value]
        deepening_sequence: list[str] = []
        if _sdk_agents_web_search_enabled():
            if parallel_enabled:
                provider_sequence.append(SearchProviderName.AGENTS_WEB_SEARCH.value)
            else:
                deepening_sequence.append(SearchProviderName.AGENTS_WEB_SEARCH.value)
        if _sdk_exa_search_enabled():
            deepening_sequence.append(SearchProviderName.EXA.value)
        if _sdk_tavily_search_enabled(query):
            deepening_sequence.append(SearchProviderName.TAVILY.value)
        hint = derive_request_autonomy_hint(
            agent_name="sdk_search_web",
            request_text=query,
        )

        return HybridSearchProvider(
            provider_sequence=tuple(dict.fromkeys(provider_sequence)),
            deepening_provider_sequence=tuple(dict.fromkeys(deepening_sequence)),
            provider_request_budget=_sdk_optional_provider_request_budget(query),
            parallel_provider_fanout=parallel_enabled,
            autonomy_hint=hint,
            quality_assessor=lambda results, request_text: _sdk_assess_search_quality(
                results=results,
                company_name=query,
                request_text=request_text,
                autonomy_hint=hint,
            ),
            provider_factory=lambda provider: build_search_provider(
                provider=provider,
                live=True,
            ),
        )
    return build_search_provider(provider=provider_name, live=True)


def _sdk_live_search_provider_name() -> SearchProviderName | None:
    """Resolve the live SDK search provider from environment policy."""

    raw_live_mode = os.getenv("KEYSTONE_LIVE_MODE")
    if raw_live_mode is None or not parse_bool(raw_live_mode):
        return None
    raw_dry_run = os.getenv("KEYSTONE_DRY_RUN")
    if raw_dry_run is not None and parse_bool(raw_dry_run):
        return None
    raw_live_research = os.getenv("KEYSTONE_ENABLE_LIVE_RESEARCH")
    if raw_live_research is None and raw_dry_run is None:
        return None
    if raw_live_research is not None and not parse_bool(raw_live_research):
        return None
    raw_provider = os.getenv("SEARCH_PROVIDER")
    if raw_provider is None or not raw_provider.strip():
        return SearchProviderName.SEARXNG
    provider_name = normalize_search_provider_name(raw_provider)
    if provider_name == SearchProviderName.DRY_RUN:
        return None
    return provider_name


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return parse_bool(raw)


def _sdk_agents_web_search_enabled() -> bool:
    return _env_bool("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", default=True)


def _sdk_exa_search_enabled() -> bool:
    raw = os.getenv("KEYSTONE_EXA_SEARCH_FALLBACK")
    if raw is not None:
        return parse_bool(raw)
    return bool(os.getenv("EXA_API_KEY"))


def _sdk_tavily_search_enabled(query: str | None = None) -> bool:
    if _env_bool("KEYSTONE_TAVILY_SEARCH_FALLBACK", default=False):
        return True
    if not os.getenv("TAVILY_API_KEY"):
        return False
    return _query_requests_optional_deepening(_combined_search_request_text(query))


def _query_requests_optional_deepening(query: str | None) -> bool:
    text = f" {re.sub(r'[^a-z0-9]+', ' ', str(query or '').strip().lower())} "
    explicit_markers = (
        " deep search ",
        " deepened search ",
        " deepened research ",
        " deepened brief ",
        " deeper search ",
        " deeper research ",
        " deep research ",
        " beyond a first pass ",
        " provider comparison ",
        " compare search ",
        " compare providers ",
        " improve recall ",
        " improve precision ",
    )
    if any(marker in text for marker in explicit_markers):
        return True
    formal_markers = (
        " rfp ",
        " rfps ",
        " grant ",
        " grants ",
        " nofo ",
        " foa ",
        " rfa ",
        " sbir ",
        " sttr ",
        " pilot ",
        " pilots ",
        " procurement ",
        " solicitation ",
        " call for proposals ",
    )
    return sum(1 for marker in formal_markers if marker in text) >= 2


def _sdk_assess_search_quality(
    *,
    results: Any,
    company_name: str,
    request_text: str,
    autonomy_hint: Any,
) -> Any:
    assessment = assess_company_search_quality(
        results=results,
        company_name=company_name,
        request_text=request_text,
        autonomy_hint=autonomy_hint,
    )
    if not _query_requests_optional_deepening(_combined_search_request_text(request_text)):
        return assessment
    if "explicit deepening requested" in assessment.reasons:
        return assessment
    return replace(
        assessment,
        needs_precision_search=True,
        reasons=tuple([*assessment.reasons, "explicit deepening requested"]),
    )


def _combined_search_request_text(query: str | None) -> str:
    context = _SDK_SEARCH_REQUEST_CONTEXT.get()
    return f"{query or ''}\n{context or ''}".strip()


def _sdk_optional_provider_request_budget(query: str | None = None) -> ProviderRequestBudget:
    limits: dict[str, int] = {}
    if _sdk_agents_web_search_enabled():
        limits[SearchProviderName.AGENTS_WEB_SEARCH.value] = _sdk_agents_web_search_max_calls()
    if _sdk_exa_search_enabled():
        limits[SearchProviderName.EXA.value] = _sdk_exa_search_max_calls()
    if _sdk_tavily_search_enabled(query):
        limits[SearchProviderName.TAVILY.value] = _sdk_tavily_search_max_calls()
    return _sdk_shared_provider_request_budget(limits)


def _sdk_shared_provider_request_budget(limits: dict[str, int]) -> ProviderRequestBudget:
    """Reuse optional-provider caps across all search tool calls in one SDK run."""

    if _SDK_SEARCH_TELEMETRY.get() is None:
        return ProviderRequestBudget(dict(limits))
    budget = _SDK_SEARCH_REQUEST_BUDGET.get()
    if budget is None:
        budget = ProviderRequestBudget(dict(limits))
        _SDK_SEARCH_REQUEST_BUDGET.set(budget)
        return budget
    if isinstance(budget.limits, dict):
        for provider_name, limit in limits.items():
            current = budget.limits.get(provider_name)
            budget.limits[provider_name] = limit if current is None else min(int(current), limit)
    return budget


def _sdk_agents_web_search_request_budget() -> ProviderRequestBudget:
    return ProviderRequestBudget(
        {SearchProviderName.AGENTS_WEB_SEARCH.value: _sdk_agents_web_search_max_calls()}
    )


def _sdk_agents_web_search_max_calls() -> int:
    raw = os.getenv("KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN", "").strip()
    try:
        cap = int(raw) if raw else 2
    except ValueError:
        cap = 2
    return max(0, cap)


def _sdk_exa_search_max_calls() -> int:
    raw = os.getenv("KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN", "").strip()
    try:
        cap = int(raw) if raw else 2
    except ValueError:
        cap = 2
    return max(0, cap)


def _sdk_tavily_search_max_calls() -> int:
    raw = os.getenv("KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN", "").strip()
    try:
        cap = int(raw) if raw else 2
    except ValueError:
        cap = 2
    return max(0, cap)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ExaConfigurationError",
    "ExaSearchError",
    "ExaSearchProvider",
    "SERPER_SEARCH_URL",
    "FirecrawlConfigurationError",
    "FirecrawlSearchError",
    "FirecrawlSearchProvider",
    "SearchProviderConfigurationError",
    "SearchProviderError",
    "SearchProviderName",
    "SearchRequest",
    "SearchResult",
    "SearxngConfigurationError",
    "SearxngSearchError",
    "SearxngSearchProvider",
    "SerperConfigurationError",
    "SerperSearchError",
    "SerperSearchProvider",
    "SerperTool",
    "build_search_provider",
    "consume_sdk_search_telemetry",
    "normalize_search_provider_name",
    "reset_sdk_search_telemetry",
    "requests",
    "sdk_search_diagnostics_from_telemetry",
    "search_web",
    "set_sdk_search_request_context",
]
