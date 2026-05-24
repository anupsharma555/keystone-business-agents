"""Hybrid retrieval policy for live search escalation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from threading import RLock
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel

from keystone_agents.schemas.retrieval import RetrievalHint
from keystone_agents.source_registry import (
    assess_source_coverage,
    required_source_lanes_for_company,
    required_source_lanes_for_opportunity,
)
from keystone_agents.tools.search_provider import (
    SearchProviderConfigurationError,
    SearchProviderError,
    SearchProviderName,
    SearchRequest,
    build_search_provider,
)

_AGGREGATOR_DOMAINS = frozenset(
    {
        "crunchbase.com",
        "glassdoor.com",
        "indeed.com",
        "linkedin.com",
        "wellfound.com",
        "wikipedia.org",
    }
)
_PRIMARY_SOURCE_DOMAINS = frozenset(
    {
        "clinicaltrials.gov",
        "nih.gov",
        "reporter.nih.gov",
        "sec.gov",
    }
)
_PRECISION_HINT_TERMS = (
    "active",
    "exclude",
    "last ",
    "posted",
    "recent",
    "remote",
    "this week",
    "today",
    "u.s.",
    "united states",
    "yesterday",
)
_STRUCTURED_ENRICHMENT_TERMS = (
    "linkedin",
    "leadership",
    "profile",
    "team size",
)
_SEARCH_REVIEW_TERMS = (
    "conflict",
    "conflicting",
    "stealth",
    "thin-data",
    "thin data",
)
_RECENCY_TERMS = (
    "today",
    "yesterday",
    "this week",
    "last 7 days",
    "last 48 hours",
    "posted this week",
    "posted today",
    "days ago",
    "2026",
    "2025",
)
_SCOUT_ROLE_REQUEST_TERMS = (
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
_SCOUT_ROLE_RESULT_TERMS = (
    "/jobs",
    "/job/",
    "/careers",
    "/positions",
    "/openings",
    "career opportunity",
    "job posting",
    "apply now",
    "we're hiring",
    "we are hiring",
    "open role",
)
_SCOUT_COLLABORATION_TERMS = (
    "collaboration",
    "consortium",
    "co-development",
    "co development",
    "joint venture",
    "partnered",
    "partners with",
    "partnership",
)
_SCOUT_RESEARCHER_TERMS = (
    "/faculty/",
    "/people/",
    "/researchers/",
    "faculty",
    "investigator",
    "laboratory",
    "principal investigator",
    "professor",
    "researcher",
    "scientist",
)
_SCOUT_INSTITUTE_TERMS = (
    "center",
    "centre",
    "department",
    "hospital",
    "institute",
    "medical school",
    "research center",
    "university",
)
_SCOUT_CONFERENCE_TERMS = (
    "abstract",
    "annual meeting",
    "conference",
    "doi.org",
    "journal",
    "poster",
    "publication",
    "pubmed",
    "summit",
    "symposium",
    "workshop",
)
_SCOUT_GRANT_TERMS = (
    "award notice",
    "foa",
    "funding opportunity",
    "grant",
    "grants.nih.gov",
    "nih",
    "reporter.nih.gov",
    "rfa",
    "sbir",
    "sttr",
)
_SCOUT_TRIAL_TERMS = (
    "clinical trial",
    "clinicaltrials.gov",
    "irb",
    "nct0",
    "protocol",
    "study launch",
    "study start",
    "trial launch",
)
_SCOUT_COMPANY_TERMS = (
    "about",
    "company",
    "funding",
    "leadership",
    "platform",
    "press release",
    "product",
    "raises",
    "series a",
    "series b",
    "startup",
    "team",
)


@dataclass(frozen=True)
class RetrievalAutonomyHint:
    """Structured agent-side recommendation consumed by the harness."""

    source: str = "none"
    needs_precision_search: bool = False
    needs_structured_enrichment: bool = False
    needs_search_review: bool = False
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetrievalQualityAssessment:
    """Deterministic retrieval-quality gate result."""

    result_count: int
    unique_domain_count: int
    duplicate_ratio: float
    primary_source_count: int
    official_source_present: bool
    linkedin_source_present: bool
    recent_signal_count: int
    needs_precision_search: bool
    needs_structured_enrichment: bool
    needs_search_review: bool
    reasons: tuple[str, ...]
    opportunity_lane_count: int = 0
    opportunity_lane_labels: tuple[str, ...] = ()
    source_lane_count: int = 0
    source_lane_labels: tuple[str, ...] = ()
    missing_source_lanes: tuple[str, ...] = ()
    source_coverage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetrievalProviderUsage:
    """Per-provider execution counters for retrieval telemetry."""

    requests_attempted: int = 0
    requests_succeeded: int = 0
    raw_result_count: int = 0
    credits_used: int = 0
    total_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def provider_value_summary(
    provider_usage: Mapping[str, Mapping[str, int | float]],
) -> list[dict[str, int | float | str]]:
    """Rank retrieval providers by result yield and speed for run telemetry."""

    summaries: list[dict[str, int | float | str]] = []
    for provider_name, usage in provider_usage.items():
        attempted = int(float(usage.get("requests_attempted") or 0))
        succeeded = int(float(usage.get("requests_succeeded") or 0))
        raw_results = int(float(usage.get("raw_result_count") or 0))
        credits_used = int(float(usage.get("credits_used") or 0))
        seconds = float(usage.get("total_seconds") or 0.0)
        summaries.append(
            {
                "provider": str(provider_name),
                "requests_attempted": attempted,
                "requests_succeeded": succeeded,
                "raw_result_count": raw_results,
                "credits_used": credits_used,
                "results_per_success": round(raw_results / succeeded, 2) if succeeded else 0.0,
                "seconds_per_success": round(seconds / succeeded, 2) if succeeded else 0.0,
            }
        )
    return sorted(
        summaries,
        key=lambda item: (
            float(item["results_per_success"]),
            int(float(item["requests_succeeded"])),
        ),
        reverse=True,
    )


@dataclass(frozen=True)
class OpportunityScoutLaneCoverage:
    """Simple lane coverage summary for scout-style discovery packets."""

    lane_labels: tuple[str, ...]
    recognized_result_count: int
    specialized_lane_count: int


def coerce_retrieval_autonomy_hint(
    value: RetrievalAutonomyHint | RetrievalHint | Mapping[str, Any] | BaseModel | None,
) -> RetrievalAutonomyHint | None:
    """Normalize external retrieval-hint payloads into the internal dataclass shape."""

    if value is None:
        return None
    if isinstance(value, RetrievalAutonomyHint):
        return value

    payload: dict[str, Any]
    if isinstance(value, RetrievalHint):
        payload = value.model_dump(mode="json")
    elif isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json")
        payload = dumped if isinstance(dumped, dict) else {}
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("retrieval hint must be a RetrievalAutonomyHint, RetrievalHint, or mapping")

    return RetrievalAutonomyHint(
        source=str(payload.get("source") or "control_plane").strip() or "control_plane",
        needs_precision_search=bool(payload.get("needs_precision_search", False)),
        needs_structured_enrichment=bool(payload.get("needs_structured_enrichment", False)),
        needs_search_review=bool(payload.get("needs_search_review", False)),
        reasons=tuple(
            str(item).strip() for item in (payload.get("reasons") or []) if str(item or "").strip()
        ),
    )


def derive_request_autonomy_hint(
    *,
    agent_name: str,
    request_text: str,
    agent_hint: RetrievalAutonomyHint | RetrievalHint | Mapping[str, Any] | BaseModel | None = None,
) -> RetrievalAutonomyHint:
    """Return a hybrid retrieval hint from the request plus optional agent recommendation."""

    explicit_hint = coerce_retrieval_autonomy_hint(agent_hint)
    if explicit_hint is not None:
        return explicit_hint

    text = request_text.strip().lower()
    reasons: list[str] = []
    needs_precision = any(term in text for term in _PRECISION_HINT_TERMS)
    needs_structured = any(term in text for term in _STRUCTURED_ENRICHMENT_TERMS)
    needs_review = any(term in text for term in _SEARCH_REVIEW_TERMS)

    if agent_name == "opportunity_scout" and any(
        term in text for term in ("role", "roles", "job", "jobs")
    ):
        needs_precision = True
        reasons.append("role search requires stricter hard-filter verification")
    if needs_precision and not reasons:
        reasons.append("request includes strict filters, recency, or precision constraints")
    if needs_structured:
        reasons.append("request asks for structured profile or leadership context")
    if needs_review:
        reasons.append("request signals thin or conflicting evidence that may need review")

    return RetrievalAutonomyHint(
        source="request_heuristic",
        needs_precision_search=needs_precision,
        needs_structured_enrichment=needs_structured,
        needs_search_review=needs_review,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def build_provider_sequence(
    *,
    requested_provider: str | None,
    configured_provider: str | None = None,
    fallback_provider: str | None = None,
) -> tuple[str, ...]:
    """Return the deterministic provider order for the retrieval ladder."""

    if requested_provider:
        providers = [requested_provider]
    else:
        configured = (configured_provider or "").strip().lower()
        if configured in ("", SearchProviderName.DRY_RUN.value, SearchProviderName.SEARXNG.value):
            providers = [SearchProviderName.SEARXNG.value]
        else:
            providers = [configured]
    if (
        fallback_provider
        and fallback_provider != SearchProviderName.SERPER.value
        and fallback_provider not in providers
    ):
        providers.append(fallback_provider)
    return tuple(_dedupe_sequence(providers))


def assess_company_search_quality(
    *,
    results: Sequence[Any],
    company_name: str,
    company_url: str | None = None,
    request_text: str = "",
    autonomy_hint: RetrievalAutonomyHint | None = None,
) -> RetrievalQualityAssessment:
    """Score company-search quality for precision escalation decisions."""

    hint = autonomy_hint or derive_request_autonomy_hint(
        agent_name="business_research_analyst",
        request_text=request_text,
    )
    result_count, unique_domain_count, duplicate_ratio = _result_stats(results)
    official_source_present = _official_source_present(results, company_url=company_url)
    linkedin_source_present = _linkedin_source_present(results)
    primary_source_count = _primary_source_count(results, company_url=company_url)
    recent_signal_count = _recent_signal_count(results)
    source_coverage = assess_source_coverage(
        results,
        expected_lanes=required_source_lanes_for_company(
            company_url=company_url,
            request_text=request_text,
        ),
        expected_domains=[company_url] if company_url else [],
    )

    reasons: list[str] = list(hint.reasons)
    needs_precision = False
    if result_count < 4:
        needs_precision = True
        reasons.append("too few live search results")
    if unique_domain_count < 2:
        needs_precision = True
        reasons.append("insufficient domain diversity")
    if primary_source_count < 2:
        needs_precision = True
        reasons.append("not enough primary or official sources")
    if company_url and not official_source_present:
        needs_precision = True
        reasons.append("official company domain missing from search results")
    if hint.needs_precision_search and recent_signal_count == 0:
        needs_precision = True
        reasons.append("request needs higher-precision or more current evidence")
    if source_coverage.missing_lanes:
        reasons.append("source coverage missing lanes: " + ", ".join(source_coverage.missing_lanes))
        if result_count < 4 or "company_site" in source_coverage.missing_lanes:
            needs_precision = True
    if source_coverage.missing_expected_domains:
        reasons.append(
            "expected source domains missing: "
            + ", ".join(source_coverage.missing_expected_domains)
        )

    needs_structured = hint.needs_structured_enrichment and not linkedin_source_present
    if needs_structured:
        reasons.append("structured profile or leadership source is still missing")

    needs_review = hint.needs_search_review or duplicate_ratio >= 0.4
    if duplicate_ratio >= 0.4:
        reasons.append("search results are duplicate-heavy")
    if needs_review and unique_domain_count <= 1:
        reasons.append("search packet may need second-pass review")

    return RetrievalQualityAssessment(
        result_count=result_count,
        unique_domain_count=unique_domain_count,
        duplicate_ratio=duplicate_ratio,
        primary_source_count=primary_source_count,
        official_source_present=official_source_present,
        linkedin_source_present=linkedin_source_present,
        recent_signal_count=recent_signal_count,
        needs_precision_search=needs_precision,
        needs_structured_enrichment=needs_structured,
        needs_search_review=needs_review,
        reasons=tuple(dict.fromkeys(reasons)),
        source_lane_count=len(source_coverage.observed_lanes),
        source_lane_labels=source_coverage.observed_lanes,
        missing_source_lanes=source_coverage.missing_lanes,
        source_coverage=source_coverage.to_dict(),
    )


def assess_opportunity_search_quality(
    *,
    results: Sequence[Any],
    desired_results: int,
    request_text: str = "",
    autonomy_hint: RetrievalAutonomyHint | None = None,
) -> RetrievalQualityAssessment:
    """Score scout-search quality for broad multi-lane opportunity discovery."""

    hint = autonomy_hint or derive_request_autonomy_hint(
        agent_name="opportunity_scout",
        request_text=request_text,
    )
    result_count, unique_domain_count, duplicate_ratio = _result_stats(results)
    primary_source_count = _primary_source_count(results, company_url=None)
    recent_signal_count = _recent_signal_count(results)
    linkedin_source_present = _linkedin_source_present(results)
    lane_coverage = _opportunity_scout_lane_coverage(results)
    source_coverage = assess_source_coverage(
        results,
        expected_lanes=required_source_lanes_for_opportunity(request_text=request_text),
    )
    minimum_results = max(2, min(desired_results, 3))
    request_is_role_focused = _request_is_role_focused(request_text)
    reasons: list[str] = list(hint.reasons)

    needs_precision = False
    if result_count < minimum_results:
        needs_precision = True
        reasons.append("too few opportunity candidates survived initial retrieval")
    if unique_domain_count < min(2, result_count):
        needs_precision = True
        reasons.append("opportunity results come from too few distinct domains")
    if result_count >= 2 and primary_source_count < min(2, result_count):
        needs_precision = True
        reasons.append("opportunity results lack enough attributable sources")
    if result_count and lane_coverage.recognized_result_count == 0:
        needs_precision = True
        reasons.append(
            "opportunity results lack clear company, collaboration, researcher, institute, "
            "conference, grant, or trial evidence"
        )
    if request_is_role_focused and result_count and "role" not in lane_coverage.lane_labels:
        needs_precision = True
        reasons.append("opportunity search did not surface verifiable role evidence")
    if hint.needs_precision_search and recent_signal_count == 0:
        needs_precision = True
        reasons.append("opportunity search lacks clear recency evidence")
    if source_coverage.missing_lanes:
        reasons.append("source coverage missing lanes: " + ", ".join(source_coverage.missing_lanes))
        if source_coverage.expected_lane_recall < 0.5:
            needs_precision = True

    company_heavy = (
        result_count >= minimum_results
        and "company" in lane_coverage.lane_labels
        and lane_coverage.specialized_lane_count == 0
    )
    needs_structured = False
    if hint.needs_structured_enrichment and not (
        linkedin_source_present
        or any(
            lane in lane_coverage.lane_labels
            for lane in ("researcher", "institute", "collaboration")
        )
    ):
        needs_structured = True
        reasons.append("opportunity search may need structured people or institution enrichment")
    elif not request_is_role_focused and company_heavy and not linkedin_source_present:
        needs_structured = True
        reasons.append(
            "opportunity search is company-heavy and may need collaboration, researcher, "
            "grant, conference, or trial enrichment"
        )

    needs_review = hint.needs_search_review or duplicate_ratio >= 0.4
    if duplicate_ratio >= 0.4:
        reasons.append("opportunity results are duplicate-heavy")
    if company_heavy and primary_source_count <= 1:
        needs_review = True
        reasons.append(
            "search packet may need second-pass review for corroborating opportunity lanes"
        )

    return RetrievalQualityAssessment(
        result_count=result_count,
        unique_domain_count=unique_domain_count,
        duplicate_ratio=duplicate_ratio,
        primary_source_count=primary_source_count,
        official_source_present=False,
        linkedin_source_present=linkedin_source_present,
        recent_signal_count=recent_signal_count,
        needs_precision_search=needs_precision,
        needs_structured_enrichment=needs_structured,
        needs_search_review=needs_review,
        reasons=tuple(dict.fromkeys(reasons)),
        opportunity_lane_count=len(lane_coverage.lane_labels),
        opportunity_lane_labels=lane_coverage.lane_labels,
        source_lane_count=len(source_coverage.observed_lanes),
        source_lane_labels=source_coverage.observed_lanes,
        missing_source_lanes=source_coverage.missing_lanes,
        source_coverage=source_coverage.to_dict(),
    )


def assess_role_search_quality(
    *,
    results: Sequence[Any],
    desired_results: int,
    request_text: str = "",
    autonomy_hint: RetrievalAutonomyHint | None = None,
) -> RetrievalQualityAssessment:
    """Score role-search quality for precision escalation decisions."""

    hint = autonomy_hint or derive_request_autonomy_hint(
        agent_name="opportunity_scout",
        request_text=request_text,
    )
    result_count, unique_domain_count, duplicate_ratio = _result_stats(results)
    primary_source_count = _primary_source_count(results, company_url=None)
    recent_signal_count = _recent_signal_count(results)
    source_coverage = assess_source_coverage(
        results,
        expected_lanes=required_source_lanes_for_opportunity(request_text=request_text),
    )
    reasons: list[str] = list(hint.reasons)

    needs_precision = False
    if result_count < max(2, desired_results):
        needs_precision = True
        reasons.append("too few role candidates survived initial retrieval")
    if unique_domain_count < 2:
        needs_precision = True
        reasons.append("role results come from too few distinct domains")
    if hint.needs_precision_search and recent_signal_count == 0:
        needs_precision = True
        reasons.append("role search lacks clear recency evidence")
    if source_coverage.missing_lanes:
        reasons.append("source coverage missing lanes: " + ", ".join(source_coverage.missing_lanes))
        if "careers_jobs" in source_coverage.missing_lanes:
            needs_precision = True

    needs_structured = hint.needs_structured_enrichment
    if needs_structured:
        reasons.append("role search may need structured profile enrichment")

    needs_review = hint.needs_search_review or duplicate_ratio >= 0.4
    if duplicate_ratio >= 0.4:
        reasons.append("role results are duplicate-heavy")

    return RetrievalQualityAssessment(
        result_count=result_count,
        unique_domain_count=unique_domain_count,
        duplicate_ratio=duplicate_ratio,
        primary_source_count=primary_source_count,
        official_source_present=False,
        linkedin_source_present=_linkedin_source_present(results),
        recent_signal_count=recent_signal_count,
        needs_precision_search=needs_precision,
        needs_structured_enrichment=needs_structured,
        needs_search_review=needs_review,
        reasons=tuple(dict.fromkeys(reasons)),
        source_lane_count=len(source_coverage.observed_lanes),
        source_lane_labels=source_coverage.observed_lanes,
        missing_source_lanes=source_coverage.missing_lanes,
        source_coverage=source_coverage.to_dict(),
    )


def merge_search_results(*result_groups: Sequence[Any]) -> list[Any]:
    """Merge result groups by URL/title while preserving original order."""

    merged: list[Any] = []
    seen: set[str] = set()
    for group in result_groups:
        for result in group:
            mapping = _result_mapping(result)
            key = (_url(mapping) or mapping.get("title") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(result)
    return merged


class HybridSearchProvider:
    """Search provider wrapper that combines agent hints with deterministic escalation."""

    provider_name: str
    dry_run: bool = False

    def __init__(
        self,
        *,
        provider_sequence: Sequence[str],
        autonomy_hint: RetrievalAutonomyHint,
        quality_assessor: Callable[[Sequence[Any], str], RetrievalQualityAssessment],
        provider_factory: Callable[[str | None], Any] | None = None,
        parallel_provider_fanout: bool = False,
        deepening_provider_sequence: Sequence[str] = (),
        provider_request_budget: ProviderRequestBudget | None = None,
    ) -> None:
        resolved_sequence = tuple(_dedupe_sequence(provider_sequence))
        if not resolved_sequence:
            resolved_sequence = (SearchProviderName.SEARXNG.value,)
        self.provider_name = resolved_sequence[0]
        self._provider_sequence = resolved_sequence
        self._deepening_provider_sequence = tuple(
            provider_name
            for provider_name in _dedupe_sequence(deepening_provider_sequence)
            if provider_name not in resolved_sequence
        )
        self._autonomy_hint = autonomy_hint
        self._quality_assessor = quality_assessor
        self._provider_factory = provider_factory or (
            lambda provider_name: build_search_provider(provider=provider_name, live=True)
        )
        self._parallel_provider_fanout = parallel_provider_fanout
        self._provider_request_budget = provider_request_budget
        self._lock = RLock()
        self._providers: dict[str, Any] = {}
        self._provider_usage = {
            provider_name: RetrievalProviderUsage()
            for provider_name in (*self._provider_sequence, *self._deepening_provider_sequence)
        }
        self._provider_credit_contexts: dict[str, list[dict[str, Any]]] = {
            provider_name: []
            for provider_name in (*self._provider_sequence, *self._deepening_provider_sequence)
        }
        self._provider_errors: list[dict[str, str]] = []
        self._all_results: list[Any] = []
        self._precision_search_escalated = False
        self._deepening_search_used = False
        self._provider_error_fallback_used = False
        self._structured_enrichment_recommended = False
        self._search_review_recommended = False
        self._quality_reasons: list[str] = []

    @property
    def provider_sequence(self) -> tuple[str, ...]:
        return self._provider_sequence

    @property
    def deepening_provider_sequence(self) -> tuple[str, ...]:
        return self._deepening_provider_sequence

    @property
    def autonomy_hint(self) -> RetrievalAutonomyHint:
        return self._autonomy_hint

    def validate_configuration(self) -> None:
        """Validate the primary provider when the caller wants strict single-provider mode."""

        validate = getattr(self._get_provider(self.provider_name), "validate_configuration", None)
        if callable(validate):
            validate()

    def search_web(self, query: str, num_results: int = 5) -> list[Any]:
        """Run the retrieval ladder for one query and escalate only when quality gates fail."""

        return self.search_structured(SearchRequest(query=query, num_results=num_results))

    def search_structured(self, request: SearchRequest) -> list[Any]:
        """Run the retrieval ladder for one provider-aware query request."""

        if self._parallel_provider_fanout and len(self._provider_sequence) > 1:
            return self._search_structured_parallel_provider_fanout(request)

        merged: list[Any] = []
        last_error: SearchProviderConfigurationError | SearchProviderError | None = None
        recovered_from_error = False
        last_assessment: RetrievalQualityAssessment | None = None

        for index, provider_name in enumerate(self._provider_sequence):
            if not self._acquire_provider_request_budget(provider_name):
                continue
            self._increment_usage(provider_name, "requests_attempted")
            provider = self._get_provider(provider_name)
            started_at = perf_counter()
            try:
                results = self._provider_search(provider, request)
            except (SearchProviderConfigurationError, SearchProviderError) as exc:
                self._record_provider_elapsed(provider_name, perf_counter() - started_at)
                last_error = exc
                self._provider_errors.append(
                    {
                        "provider": provider_name,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                if merged and index >= len(self._provider_sequence) - 1:
                    break
                recovered_from_error = True
                continue

            self._record_provider_elapsed(provider_name, perf_counter() - started_at)
            self._increment_usage(provider_name, "requests_succeeded")
            self._increment_usage(provider_name, "raw_result_count", amount=len(results))
            self._record_provider_credit_usage(provider_name, provider)
            merged = list(results) if not merged else merge_search_results(merged, results)
            assessment = self._quality_assessor(merged, request.query)
            last_assessment = assessment
            self._record_assessment(assessment)
            if not self._should_use_backup_provider(assessment):
                self._all_results.extend(merged)
                if recovered_from_error and provider_name != self.provider_name:
                    self._provider_error_fallback_used = True
                return merged
            if index >= len(self._provider_sequence) - 1:
                break
            self._precision_search_escalated = True

        if merged:
            merged = self._run_deepening_providers_if_needed(
                merged,
                request,
                assessment=last_assessment,
            )
            self._all_results.extend(merged)
            if recovered_from_error:
                self._provider_error_fallback_used = True
            return merged
        if last_error is not None:
            raise last_error
        return []

    def _search_web_parallel_provider_fanout(self, query: str, num_results: int = 5) -> list[Any]:
        """Run all configured providers for one query and merge in configured order."""

        return self._search_structured_parallel_provider_fanout(
            SearchRequest(query=query, num_results=num_results)
        )

    def _search_structured_parallel_provider_fanout(self, request: SearchRequest) -> list[Any]:
        """Run all configured providers for one provider-aware query and merge in order."""

        result_groups: dict[str, list[Any]] = {}
        last_error: SearchProviderConfigurationError | SearchProviderError | None = None

        def run_provider(
            provider_name: str,
        ) -> tuple[str, list[Any], float, dict[str, Any], Exception | None]:
            provider = self._get_provider(provider_name)
            started_at = perf_counter()
            try:
                results = self._provider_search(provider, request)
                return (
                    provider_name,
                    results,
                    perf_counter() - started_at,
                    self._provider_credit_usage_context(provider),
                    None,
                )
            except (SearchProviderConfigurationError, SearchProviderError) as exc:
                return provider_name, [], perf_counter() - started_at, {}, exc

        runnable_providers: list[str] = []
        for provider_name in self._provider_sequence:
            if not self._acquire_provider_request_budget(provider_name):
                continue
            self._increment_usage(provider_name, "requests_attempted")
            runnable_providers.append(provider_name)

        if not runnable_providers:
            return []

        max_workers = max(1, min(len(runnable_providers), 4))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(run_provider, provider_name): provider_name
                for provider_name in runnable_providers
            }
            for future in as_completed(futures):
                provider_name, results, elapsed_seconds, credit_usage, error = future.result()
                self._record_provider_elapsed(provider_name, elapsed_seconds)
                if error is not None:
                    last_error = error
                    with self._lock:
                        self._provider_errors.append(
                            {
                                "provider": provider_name,
                                "error_type": type(error).__name__,
                                "message": str(error),
                            }
                        )
                    continue
                self._increment_usage(provider_name, "requests_succeeded")
                self._increment_usage(provider_name, "raw_result_count", amount=len(results))
                self._record_provider_credit_usage(provider_name, credit_usage=credit_usage)
                result_groups[provider_name] = list(results)

        ordered_result_groups = [
            result_groups[provider_name]
            for provider_name in self._provider_sequence
            if provider_name in result_groups
        ]
        merged = merge_search_results(*ordered_result_groups)
        if not merged:
            if last_error is not None and not result_groups:
                raise last_error
            return []

        assessment = self._quality_assessor(merged, request.query)
        self._record_assessment(assessment)
        if self._should_use_backup_provider(assessment):
            self._precision_search_escalated = True
            merged = self._run_deepening_providers_if_needed(
                merged,
                request,
                assessment=assessment,
            )
        if last_error is not None:
            self._provider_error_fallback_used = True
        with self._lock:
            self._all_results.extend(merged)
        return merged

    @staticmethod
    def _provider_search(provider: Any, request: SearchRequest) -> list[Any]:
        structured = getattr(provider, "search_structured", None)
        if callable(structured):
            return list(structured(request))
        return list(provider.search_web(request.query, num_results=request.num_results))

    def _run_deepening_providers_if_needed(
        self,
        merged: list[Any],
        request: SearchRequest,
        *,
        assessment: RetrievalQualityAssessment | None,
    ) -> list[Any]:
        if not self._deepening_provider_sequence:
            return merged
        current_assessment = assessment or self._quality_assessor(merged, request.query)
        if not self._should_use_backup_provider(current_assessment):
            return merged

        self._precision_search_escalated = True
        deepened = list(merged)
        for provider_name in self._deepening_provider_sequence:
            if not self._acquire_provider_request_budget(provider_name):
                continue
            self._increment_usage(provider_name, "requests_attempted")
            provider = self._get_provider(provider_name)
            started_at = perf_counter()
            try:
                results = self._provider_search(provider, request)
            except (SearchProviderConfigurationError, SearchProviderError) as exc:
                self._record_provider_elapsed(provider_name, perf_counter() - started_at)
                with self._lock:
                    self._provider_errors.append(
                        {
                            "provider": provider_name,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
                continue

            self._record_provider_elapsed(provider_name, perf_counter() - started_at)
            self._increment_usage(provider_name, "requests_succeeded")
            self._increment_usage(provider_name, "raw_result_count", amount=len(results))
            self._record_provider_credit_usage(provider_name, provider)
            if results:
                self._deepening_search_used = True
            deepened = merge_search_results(deepened, results)
            current_assessment = self._quality_assessor(deepened, request.query)
            self._record_assessment(current_assessment)
            if not self._should_use_backup_provider(current_assessment):
                break
        return deepened

    def search(
        self,
        query: str,
        num_results: int = 5,
        *,
        max_results: int | None = None,
    ) -> list[Any]:
        """Compatibility alias for legacy provider call sites."""

        result_count = max_results if max_results is not None else num_results
        return self.search_web(query=query, num_results=result_count)

    def collected_results(self) -> list[Any]:
        """Return the merged result packets seen across all search calls."""

        return list(self._all_results)

    def telemetry(self) -> dict[str, Any]:
        """Return retrieval execution metadata for CLI payloads and audits."""

        providers_attempted = [
            provider_name
            for provider_name, usage in self._provider_usage.items()
            if usage.requests_attempted > 0
        ]
        providers_used = [
            provider_name
            for provider_name, usage in self._provider_usage.items()
            if usage.requests_succeeded > 0
        ]
        provider_usage = {
            provider_name: usage.to_dict()
            for provider_name, usage in self._provider_usage.items()
            if usage.requests_attempted > 0
        }
        return {
            "search_provider_sequence": list(self._provider_sequence),
            "search_deepening_provider_sequence": list(self._deepening_provider_sequence),
            "primary_search_provider": self.provider_name,
            "search_providers_attempted": providers_attempted,
            "search_providers_used": providers_used,
            "search_provider_used": providers_used[-1] if providers_used else self.provider_name,
            "parallel_provider_fanout": self._parallel_provider_fanout,
            "precision_search_escalated": self._precision_search_escalated,
            "deepening_search_used": self._deepening_search_used,
            "provider_error_fallback_used": self._provider_error_fallback_used,
            "search_provider_errors": list(self._provider_errors),
            "provider_usage": provider_usage,
            "provider_value_summary": provider_value_summary(provider_usage),
            "tavily_estimated_credits_used": self._provider_usage[
                SearchProviderName.TAVILY.value
            ].credits_used
            or self._provider_usage[SearchProviderName.TAVILY.value].requests_succeeded
            if SearchProviderName.TAVILY.value in self._provider_usage
            else 0,
            "tavily_credit_budget": (
                self._provider_credit_contexts.get(SearchProviderName.TAVILY.value, [])[-1]
                if self._provider_credit_contexts.get(SearchProviderName.TAVILY.value)
                else {}
            ),
            "agents_web_search_estimated_calls_used": self._provider_usage[
                SearchProviderName.AGENTS_WEB_SEARCH.value
            ].requests_succeeded
            if SearchProviderName.AGENTS_WEB_SEARCH.value in self._provider_usage
            else 0,
            "serper_estimated_credits_used": self._provider_usage[
                SearchProviderName.SERPER.value
            ].requests_succeeded
            if SearchProviderName.SERPER.value in self._provider_usage
            else 0,
            "autonomy_hint": self._autonomy_hint.to_dict(),
            "structured_enrichment_recommended": self._structured_enrichment_recommended,
            "structured_enrichment_candidates": (
                ["apify", "browserless"] if self._structured_enrichment_recommended else []
            ),
            "search_review_recommended": self._search_review_recommended,
            "quality_reason_hints": list(dict.fromkeys(self._quality_reasons)),
        }

    def _get_provider(self, provider_name: str) -> Any:
        with self._lock:
            provider = self._providers.get(provider_name)
            if provider is None:
                provider = self._provider_factory(provider_name)
                self._providers[provider_name] = provider
            return provider

    def _acquire_provider_request_budget(self, provider_name: str) -> bool:
        if self._provider_request_budget is None:
            return True
        if self._provider_request_budget.try_acquire(provider_name):
            return True
        with self._lock:
            self._provider_errors.append(
                {
                    "provider": provider_name,
                    "error_type": "ProviderRequestCapExceeded",
                    "message": (
                        f"{provider_name} request cap reached "
                        f"({self._provider_request_budget.limit_for(provider_name)} per run)."
                    ),
                }
            )
        return False

    def _increment_usage(self, provider_name: str, field_name: str, amount: int = 1) -> None:
        with self._lock:
            usage = self._provider_usage.get(provider_name, RetrievalProviderUsage())
            self._provider_usage[provider_name] = RetrievalProviderUsage(
                requests_attempted=(
                    usage.requests_attempted + amount
                    if field_name == "requests_attempted"
                    else usage.requests_attempted
                ),
                requests_succeeded=(
                    usage.requests_succeeded + amount
                    if field_name == "requests_succeeded"
                    else usage.requests_succeeded
                ),
                raw_result_count=(
                    usage.raw_result_count + amount
                    if field_name == "raw_result_count"
                    else usage.raw_result_count
                ),
                credits_used=(
                    usage.credits_used + amount
                    if field_name == "credits_used"
                    else usage.credits_used
                ),
                total_seconds=usage.total_seconds,
            )

    def _record_provider_elapsed(self, provider_name: str, elapsed_seconds: float) -> None:
        with self._lock:
            usage = self._provider_usage.get(provider_name, RetrievalProviderUsage())
            self._provider_usage[provider_name] = RetrievalProviderUsage(
                requests_attempted=usage.requests_attempted,
                requests_succeeded=usage.requests_succeeded,
                raw_result_count=usage.raw_result_count,
                credits_used=usage.credits_used,
                total_seconds=round(usage.total_seconds + max(0.0, elapsed_seconds), 3),
            )

    def _record_assessment(self, assessment: RetrievalQualityAssessment) -> None:
        with self._lock:
            self._structured_enrichment_recommended = (
                self._structured_enrichment_recommended or assessment.needs_structured_enrichment
            )
            self._search_review_recommended = (
                self._search_review_recommended or assessment.needs_search_review
            )
            self._quality_reasons.extend(assessment.reasons)

    def _record_provider_credit_usage(
        self,
        provider_name: str,
        provider: Any | None = None,
        *,
        credit_usage: dict[str, Any] | None = None,
    ) -> None:
        usage_context = credit_usage or self._provider_credit_usage_context(provider)
        credits = _credit_usage_count(usage_context)
        if credits <= 0 and provider_name != SearchProviderName.TAVILY.value:
            return
        with self._lock:
            if credits > 0:
                usage = self._provider_usage.get(provider_name, RetrievalProviderUsage())
                self._provider_usage[provider_name] = RetrievalProviderUsage(
                    requests_attempted=usage.requests_attempted,
                    requests_succeeded=usage.requests_succeeded,
                    raw_result_count=usage.raw_result_count,
                    credits_used=usage.credits_used + credits,
                    total_seconds=usage.total_seconds,
                )
            if usage_context:
                contexts = self._provider_credit_contexts.setdefault(provider_name, [])
                contexts.append(dict(usage_context))
                del contexts[:-5]

    @staticmethod
    def _provider_credit_usage_context(provider: Any | None) -> dict[str, Any]:
        if provider is None:
            return {}
        usage = getattr(provider, "last_credit_usage", None)
        if callable(usage):
            try:
                usage = usage()
            except TypeError:
                usage = {}
        if isinstance(usage, Mapping):
            return dict(usage)
        return {}

    @staticmethod
    def _should_use_backup_provider(assessment: RetrievalQualityAssessment) -> bool:
        return (
            assessment.needs_precision_search
            or assessment.needs_structured_enrichment
            or assessment.needs_search_review
        )


@dataclass
class ProviderRequestBudget:
    """Thread-safe per-run request cap for optional live providers."""

    limits: Mapping[str, int]
    _used: dict[str, int] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def limit_for(self, provider_name: str) -> int | None:
        normalized = _dedupe_sequence([provider_name])[0] if provider_name else provider_name
        limit = self.limits.get(normalized)
        if limit is None:
            return None
        return int(limit)

    def try_acquire(self, provider_name: str) -> bool:
        normalized = _dedupe_sequence([provider_name])[0] if provider_name else provider_name
        limit = self.limit_for(normalized)
        if limit is None:
            return True
        if limit <= 0:
            return False
        with self._lock:
            used = self._used.get(normalized, 0)
            if used >= limit:
                return False
            self._used[normalized] = used + 1
            return True


def _credit_usage_count(usage_context: Mapping[str, Any] | None) -> int:
    if not usage_context:
        return 0
    for key in ("request_credits", "credits_used", "credits"):
        try:
            value = int(float(usage_context.get(key) or 0))
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _dedupe_sequence(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    aliases = {
        "openai-web-search": SearchProviderName.AGENTS_WEB_SEARCH.value,
        "openai-websearch": SearchProviderName.AGENTS_WEB_SEARCH.value,
        "agents-websearch": SearchProviderName.AGENTS_WEB_SEARCH.value,
        "sdk-web-search": SearchProviderName.AGENTS_WEB_SEARCH.value,
        "native-web-search": SearchProviderName.AGENTS_WEB_SEARCH.value,
    }
    for item in items:
        normalized = str(item or "").strip().lower().replace("_", "-")
        normalized = aliases.get(normalized, normalized)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _result_mapping(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        dumped = result.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(result, dict):
        return dict(result)
    return {
        "title": getattr(result, "title", ""),
        "link": getattr(result, "link", ""),
        "url": getattr(result, "url", ""),
        "snippet": getattr(result, "snippet", ""),
    }


def _url(mapping: dict[str, Any]) -> str:
    return str(mapping.get("url") or mapping.get("link") or "").strip()


def _normalized_netloc(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _domain(mapping: dict[str, Any]) -> str:
    url = _url(mapping)
    if not url:
        return ""
    return _normalized_netloc(url)


def _result_stats(results: Sequence[Any]) -> tuple[int, int, float]:
    count = len(results)
    if count == 0:
        return 0, 0, 0.0
    urls = [_url(_result_mapping(result)).rstrip("/").lower() for result in results]
    non_empty_urls = [url for url in urls if url]
    unique_urls = len(set(non_empty_urls)) if non_empty_urls else 0
    domains = {
        _domain(_result_mapping(result)) for result in results if _domain(_result_mapping(result))
    }
    duplicate_ratio = 0.0
    if count:
        unique_count = unique_urls or len(
            {
                str(_result_mapping(result).get("title") or "").strip().lower()
                for result in results
                if str(_result_mapping(result).get("title") or "").strip()
            }
        )
        duplicate_ratio = max(0.0, 1 - (unique_count / count))
    return count, len(domains), round(duplicate_ratio, 2)


def _official_source_present(results: Sequence[Any], *, company_url: str | None) -> bool:
    if not company_url:
        return False
    official_domain = _normalized_netloc(company_url)
    if not official_domain:
        return False
    return any(_domain(_result_mapping(result)) == official_domain for result in results)


def _linkedin_source_present(results: Sequence[Any]) -> bool:
    return any("linkedin.com" in _domain(_result_mapping(result)) for result in results)


def _primary_source_count(results: Sequence[Any], *, company_url: str | None) -> int:
    official_domain = ""
    if company_url:
        official_domain = _normalized_netloc(company_url)
    count = 0
    for result in results:
        domain = _domain(_result_mapping(result))
        if not domain:
            continue
        if official_domain and domain == official_domain:
            count += 1
            continue
        if domain in _PRIMARY_SOURCE_DOMAINS or "linkedin.com" in domain:
            count += 1
            continue
        if domain not in _AGGREGATOR_DOMAINS:
            count += 1
    return count


def _recent_signal_count(results: Sequence[Any]) -> int:
    count = 0
    for result in results:
        mapping = _result_mapping(result)
        text = " ".join(
            str(mapping.get(key) or "") for key in ("title", "snippet", "date", "published_at")
        ).lower()
        if any(term in text for term in _RECENCY_TERMS):
            count += 1
    return count


def _request_is_role_focused(request_text: str) -> bool:
    normalized = "".join(
        character if character.isalnum() else " " for character in request_text.strip().lower()
    )
    text = f" {normalized} "
    return any(term in text for term in _SCOUT_ROLE_REQUEST_TERMS)


def _opportunity_scout_lane_coverage(results: Sequence[Any]) -> OpportunityScoutLaneCoverage:
    lane_labels: list[str] = []
    recognized_result_count = 0
    for result in results:
        lanes = _opportunity_scout_lanes(_result_mapping(result))
        if lanes:
            recognized_result_count += 1
            lane_labels.extend(lanes)
    unique_lanes = tuple(dict.fromkeys(lane_labels))
    specialized_lane_count = sum(1 for lane in unique_lanes if lane not in {"company", "role"})
    return OpportunityScoutLaneCoverage(
        lane_labels=unique_lanes,
        recognized_result_count=recognized_result_count,
        specialized_lane_count=specialized_lane_count,
    )


def _opportunity_scout_lanes(mapping: Mapping[str, Any]) -> tuple[str, ...]:
    url = _url(dict(mapping)).lower()
    domain = _domain(dict(mapping))
    text = " ".join(
        str(mapping.get(key) or "") for key in ("title", "snippet", "date", "published_at")
    ).lower()
    haystack = " ".join(part for part in (text, url, domain) if part)
    lanes: list[str] = []

    if domain == "clinicaltrials.gov" or any(term in haystack for term in _SCOUT_TRIAL_TERMS):
        lanes.append("trial")
    if domain in {"grants.nih.gov", "nih.gov", "reporter.nih.gov"} or any(
        term in haystack for term in _SCOUT_GRANT_TERMS
    ):
        lanes.append("grant")
    if any(term in haystack for term in _SCOUT_CONFERENCE_TERMS):
        lanes.append("conference")
    if any(term in haystack for term in _SCOUT_COLLABORATION_TERMS):
        lanes.append("collaboration")
    if any(term in haystack for term in _SCOUT_RESEARCHER_TERMS):
        lanes.append("researcher")
    if domain.endswith(".edu") or any(term in haystack for term in _SCOUT_INSTITUTE_TERMS):
        lanes.append("institute")
    if any(term in haystack for term in _SCOUT_ROLE_RESULT_TERMS):
        lanes.append("role")
    if any(term in haystack for term in _SCOUT_COMPANY_TERMS):
        lanes.append("company")
    elif domain and domain not in _AGGREGATOR_DOMAINS and not domain.endswith((".edu", ".gov")):
        lanes.append("company")

    return tuple(dict.fromkeys(lanes))
