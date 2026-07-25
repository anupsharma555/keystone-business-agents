"""Multi-target research planning, discovery, and source sufficiency helpers."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from keystone_agents.agents.business_research_analyst import build_company_research_queries
from keystone_agents.config import load_settings
from keystone_agents.quality_budget import AgentQualityBudget
from keystone_agents.retrieval_policy import (
    HybridSearchProvider,
    derive_request_autonomy_hint,
    merge_search_results,
)
from keystone_agents.schemas.company_profile import CompanyProfile, SourceRecord
from keystone_agents.schemas.retrieval import RetrievalHint
from keystone_agents.semantic_execution import ExecutionIntentAuthority
from keystone_agents.source_triage import triage_source_candidates
from keystone_agents.tools.search_provider import build_search_provider

MULTI_TARGET_RESEARCH_SCHEMA = "keystone.multi_target_research.v1"

_GENERIC_SOURCE_DOMAINS = {
    "aicompanionpick",
    "aicompanionrank",
    "aicompaniontoday",
    "apnews",
    "axios",
    "brookings",
    "business-standard",
    "businesswire",
    "cnn",
    "companionrank",
    "companionwise",
    "commonsensemedia",
    "consumer",
    "crescendo",
    "cpsc",
    "digitbin",
    "facebook",
    "fiercehealthcare",
    "forbes",
    "ftc",
    "instagram",
    "learningpolicyinstitute",
    "linkedin",
    "mashable",
    "marketing-interactive",
    "openaitoolshub",
    "pcmag",
    "prnewswire",
    "qbs",
    "researchandmarkets",
    "rev",
    "safe-sound",
    "techraisal",
    "techcrunch",
    "toolworthy",
    "wikipedia",
    "zdnet",
}
_ENTITY_STOPWORDS = {
    "AI",
    "Big Three",
    "Best",
    "Consumer",
    "Contact",
    "Compare",
    "Comparison",
    "Current",
    "Public",
    "Research",
    "Safety",
    "Teen Safety",
    "Top",
    "Training",
}
_NON_PRODUCT_CANDIDATE_KEYS = {
    "aicompaniontoday",
    "apnews",
    "brookings",
    "businessstandard",
    "citizen",
    "cnn",
    "commonsensemedia",
    "consumer",
    "contactbrookings",
    "cpsc",
    "facebook",
    "ftc",
    "geoffreyafowler",
    "help",
    "instagram",
    "learningpolicyinstitute",
    "linkedin",
    "marketinteractive",
    "mashable",
    "qbs",
    "rev",
    "safesound",
    "techcrunch",
    "training",
}
_AI_COMPANION_PRODUCT_KEYS = {
    "candyai",
    "characterai",
    "chatgpt",
    "holanolis",
    "microsoftcopilot",
    "nomi",
    "replika",
}
_AI_COMPANION_TOPIC_MARKERS = (
    "ai companion",
    "ai companions",
    "companion chatbot",
    "companion chatbots",
    "chatbot companion",
    "chatbot companions",
    "character.ai",
    "replika",
    "nomi",
    "candy ai",
    "holanolis",
)
_COMMON_SUBDOMAIN_LABELS = {
    "about",
    "blog",
    "help",
    "learn",
    "news",
    "newsroom",
    "policy",
    "press",
    "support",
    "www",
}
_PRODUCT_DOMAIN_ALIASES = {
    "chatgpt": {"chatgpt", "openai"},
    "characterai": {"character", "characterai"},
    "microsoftcopilot": {"copilot", "microsoft"},
}
_DEFAULT_DIMENSIONS = (
    "teen safety",
    "escalation",
    "trusted contact",
    "public source evidence",
)


class MultiTargetResearchPlan(BaseModel):
    """Executable plan for category/comparison research with multiple targets."""

    schema_: Literal["keystone.multi_target_research.v1"] = Field(
        default=MULTI_TARGET_RESEARCH_SCHEMA,
        alias="schema",
    )
    topic: str
    desired_count: int = Field(default=3, ge=2, le=6)
    requested_dimensions: list[str] = Field(default_factory=list)
    source_constraints: list[str] = Field(default_factory=list)
    pass_budget: int = Field(default=2, ge=1, le=3)
    cost_profile: str = ""
    request_text: str = ""

    @field_validator("topic", "cost_profile", "request_text", mode="before")
    @classmethod
    def _clean_scalar(cls, value: Any) -> str:
        return " ".join(str(value or "").split()).strip()

    @field_validator("requested_dimensions", "source_constraints", mode="before")
    @classmethod
    def _clean_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(" ".join(str(item or "").split()) for item in value if item))


class CandidateEvidence(BaseModel):
    """One search result that mentioned or represented a candidate target."""

    query: str = ""
    provider: str = ""
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""


class CandidateTarget(BaseModel):
    """Normalized candidate target with provenance from all discovery lanes."""

    name: str
    normalized_key: str
    url_candidates: list[str] = Field(default_factory=list)
    evidence: list[CandidateEvidence] = Field(default_factory=list)
    ranking_score: int = 0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    gaps: list[str] = Field(default_factory=list)

    @field_validator("url_candidates", "gaps", mode="before")
    @classmethod
    def _clean_string_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(" ".join(str(item or "").split()) for item in value if item))


class PerTargetResearchPacket(BaseModel):
    """Source packet for one selected target after per-target depth retrieval."""

    target_name: str
    canonical_url: str = ""
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    extraction_status: str = "not_requested"
    retained_source_ids: list[str] = Field(default_factory=list)
    deepened_source_ids: list[str] = Field(default_factory=list)
    rejected_source_ids: list[str] = Field(default_factory=list)
    source_sufficient: bool = False
    gaps: list[str] = Field(default_factory=list)
    retrieval_diagnostics: dict[str, Any] = Field(default_factory=dict)


class MultiTargetResearchResult(BaseModel):
    """Research output for a multi-target Business Research branch."""

    schema_: Literal["keystone.multi_target_research.v1"] = Field(
        default=MULTI_TARGET_RESEARCH_SCHEMA,
        alias="schema",
    )
    plan: MultiTargetResearchPlan
    candidate_targets: list[CandidateTarget] = Field(default_factory=list)
    selected_targets: list[str] = Field(default_factory=list)
    packets: list[PerTargetResearchPacket] = Field(default_factory=list)
    comparison_ready: bool = False
    blockers: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    pass_types: list[str] = Field(default_factory=list)


def should_run_multi_target_research(
    *,
    request_text: str,
    manual_plan: dict[str, Any] | None,
    target: str,
) -> bool:
    """Return true when a Business Research request needs target discovery first."""

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical:
        assert authority.plan is not None
        plan = authority.plan
        return bool(
            plan.target_agent == "business_research_analyst"
            and plan.intent in {"company_research", "research_brief"}
            and (
                (plan.desired_count_explicit and plan.desired_count > 1)
                or (
                    plan.target_type == "company"
                    and len(set(plan.required_entities)) > 1
                )
            )
        )
    if authority.invalid:
        return False

    plan = manual_plan if isinstance(manual_plan, dict) else {}
    explicit_desired_count = _explicit_desired_count(plan, request_text)
    desired_count = explicit_desired_count or 1
    text = " ".join(
        str(item or "")
        for item in (
            request_text,
            target,
            plan.get("objective"),
            plan.get("primary_target"),
            " ".join(str(item) for item in plan.get("planner_warnings") or []),
        )
    ).lower()
    if desired_count > 1 and re.search(r"\b(compare|comparison|versus|vs\.?)\b", text):
        return True
    if desired_count > 1 and _looks_like_category_target(target):
        return True
    if "target is a product category" in text:
        return True
    return False


def build_multi_target_research_plan(
    *,
    request_text: str,
    manual_plan: dict[str, Any] | None,
    target: str,
    cost_profile: str,
) -> MultiTargetResearchPlan:
    """Build a bounded multi-target research plan from request metadata."""

    plan = manual_plan if isinstance(manual_plan, dict) else {}
    topic = str(plan.get("primary_target") or target or request_text or "").strip()
    dimensions = [str(item) for item in plan.get("required_terms") or [] if str(item).strip()]
    if not dimensions:
        dimensions = _request_dimensions(request_text)
    constraints = [str(item) for item in plan.get("constraints") or [] if str(item).strip()]
    return MultiTargetResearchPlan(
        topic=topic[:180],
        desired_count=_desired_count(plan, request_text),
        requested_dimensions=dimensions or list(_DEFAULT_DIMENSIONS),
        source_constraints=constraints,
        pass_budget=2,
        cost_profile=cost_profile,
        request_text=request_text,
    )


def run_multi_target_research(
    plan: MultiTargetResearchPlan,
    *,
    live_search: bool,
    quality_budget: AgentQualityBudget | None = None,
    agents_web_search_max_calls: int | None = None,
    retrieval_hint: RetrievalHint | None = None,
    search_provider_builder: Callable[..., Any] | None = None,
    retrieve_profile: Callable[..., tuple[CompanyProfile, dict[str, Any]]] | None = None,
) -> MultiTargetResearchResult:
    """Execute multi-target breadth/depth retrieval with bounded parallelism."""

    pass_types = ["candidate_discovery"]
    discovery, discovery_diagnostics = _discover_candidate_targets_with_diagnostics(
        plan,
        live_search=live_search,
        quality_budget=quality_budget,
        agents_web_search_max_calls=agents_web_search_max_calls,
        retrieval_hint=retrieval_hint,
        search_provider_builder=search_provider_builder,
    )
    if discovery_diagnostics.get("candidate_breadth_repair_passes"):
        pass_types.append("candidate_breadth_repair")
    pass_types.append("target_selection")
    selected = discovery[: max(plan.desired_count, min(len(discovery), plan.desired_count + 2))]
    selected, packets = _research_with_single_substitution_round(
        plan,
        selected_candidates=selected,
        live_search=live_search,
        quality_budget=quality_budget,
        agents_web_search_max_calls=agents_web_search_max_calls,
        retrieval_hint=retrieval_hint,
        retrieve_profile=retrieve_profile,
    )
    pass_types.append("per_target_depth")
    if len(packets) < plan.desired_count or any(not packet.source_sufficient for packet in packets):
        pass_types.append("target_substitution")
    ready_packets = [packet for packet in packets if packet.source_sufficient]
    comparison_ready = len(ready_packets) >= plan.desired_count
    blockers = _multi_target_blockers(plan, discovery, packets)
    if comparison_ready:
        blockers = []
        pass_types.append("final_synthesis")
    return MultiTargetResearchResult(
        plan=plan,
        candidate_targets=discovery,
        selected_targets=[candidate.name for candidate in selected[: plan.desired_count]],
        packets=packets[: plan.desired_count],
        comparison_ready=comparison_ready,
        blockers=blockers,
        diagnostics={
            "candidate_count": len(discovery),
            "packet_count": len(packets),
            "ready_packet_count": len(ready_packets),
            "parallel_target_depth_limit": _target_depth_concurrency(plan),
            "target_selection": _target_selection_diagnostics(
                discovery=discovery,
                selected=selected[: plan.desired_count],
                packets=packets[: plan.desired_count],
            ),
            **discovery_diagnostics,
        },
        pass_types=list(dict.fromkeys(pass_types)),
    )


def discover_candidate_targets(
    plan: MultiTargetResearchPlan,
    *,
    live_search: bool,
    quality_budget: AgentQualityBudget | None = None,
    agents_web_search_max_calls: int | None = None,
    retrieval_hint: RetrievalHint | None = None,
    search_provider_builder: Callable[..., Any] | None = None,
) -> list[CandidateTarget]:
    """Run broad discovery queries and rank normalized candidate targets."""

    candidates, _diagnostics = _discover_candidate_targets_with_diagnostics(
        plan,
        live_search=live_search,
        quality_budget=quality_budget,
        agents_web_search_max_calls=agents_web_search_max_calls,
        retrieval_hint=retrieval_hint,
        search_provider_builder=search_provider_builder,
    )
    return candidates


def _discover_candidate_targets_with_diagnostics(
    plan: MultiTargetResearchPlan,
    *,
    live_search: bool,
    quality_budget: AgentQualityBudget | None = None,
    agents_web_search_max_calls: int | None = None,
    retrieval_hint: RetrievalHint | None = None,
    search_provider_builder: Callable[..., Any] | None = None,
) -> tuple[list[CandidateTarget], dict[str, Any]]:
    """Run broad discovery queries, one bounded breadth repair, and rank targets."""

    if not live_search:
        return [], {
            "candidate_discovery_query_count": 0,
            "candidate_breadth_repair_passes": 0,
            "candidate_count_after_initial": 0,
        }
    queries = _candidate_discovery_queries(plan)
    max_results = max(4, min(8, getattr(quality_budget, "max_results", 6) or 6))
    search_results: list[Any] = []
    result_query_by_url: dict[str, str] = {}
    search_provider_builder = search_provider_builder or build_search_provider
    client = _multi_target_search_client(
        plan,
        agents_web_search_max_calls=agents_web_search_max_calls,
        retrieval_hint=retrieval_hint,
        search_provider_builder=search_provider_builder,
    )
    search_results = _search_candidate_query_batch(
        client,
        queries=queries,
        max_results=max_results,
        search_results=search_results,
        result_query_by_url=result_query_by_url,
    )

    ledger = candidate_targets_from_search_results(
        search_results,
        plan=plan,
        result_query_by_url=result_query_by_url,
    )
    ranked = rank_candidate_targets(ledger, plan=plan)
    initial_count = len(ranked)
    repair_passes = 0
    if initial_count < plan.desired_count and plan.pass_budget > 1:
        repair_passes = 1
        repair_queries = _candidate_discovery_repair_queries(plan)
        search_results = _search_candidate_query_batch(
            client,
            queries=repair_queries,
            max_results=max_results,
            search_results=search_results,
            result_query_by_url=result_query_by_url,
        )
        ledger = candidate_targets_from_search_results(
            search_results,
            plan=plan,
            result_query_by_url=result_query_by_url,
        )
        ranked = rank_candidate_targets(ledger, plan=plan)
    return ranked, {
        "candidate_discovery_query_count": len(queries),
        "candidate_breadth_repair_passes": repair_passes,
        "candidate_count_after_initial": initial_count,
        "candidate_count_after_repair": len(ranked),
    }


def candidate_targets_from_search_results(
    search_results: Sequence[Any],
    *,
    plan: MultiTargetResearchPlan,
    result_query_by_url: dict[str, str] | None = None,
) -> list[CandidateTarget]:
    """Build a normalized candidate ledger from raw search results."""

    ledger: dict[str, CandidateTarget] = {}
    result_query_by_url = result_query_by_url or {}
    for result in search_results:
        mapping = _result_mapping(result)
        url = mapping["url"]
        query = result_query_by_url.get(url, "")
        provider = mapping["source"]
        names = _candidate_names_from_result(mapping, plan=plan)
        for name in names:
            name = _canonical_candidate_name(name, plan=plan)
            key = _candidate_key(name)
            if not key:
                continue
            evidence = CandidateEvidence(
                query=query,
                provider=provider,
                title=mapping["title"],
                url=url,
                snippet=mapping["snippet"],
                source=provider,
            )
            existing = ledger.get(key)
            if existing is None:
                existing = CandidateTarget(name=name, normalized_key=key)
                ledger[key] = existing
            existing.evidence.append(evidence)
            if url and _looks_like_official_target_url(url, key):
                existing.url_candidates = list(dict.fromkeys([*existing.url_candidates, url]))
    return list(ledger.values())


def _search_candidate_query_batch(
    client: HybridSearchProvider,
    *,
    queries: Sequence[str],
    max_results: int,
    search_results: list[Any],
    result_query_by_url: dict[str, str],
) -> list[Any]:
    if not queries:
        return search_results
    merged = search_results
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as executor:
        futures = {
            executor.submit(client.search_web, query, max_results): query for query in queries
        }
        for future in as_completed(futures):
            query = futures[future]
            results = future.result()
            for result in results:
                url = _result_url(result)
                if url:
                    result_query_by_url[url] = query
            merged = merge_search_results(merged, results)
    return merged


def rank_candidate_targets(
    candidates: Sequence[CandidateTarget],
    *,
    plan: MultiTargetResearchPlan,
) -> list[CandidateTarget]:
    """Rank candidates using evidence coverage and source quality."""

    ranked: list[CandidateTarget] = []
    for candidate in candidates:
        score, gaps = _candidate_score(candidate, plan=plan)
        ranked.append(
            candidate.model_copy(
                update={
                    "ranking_score": score,
                    "confidence": min(0.95, max(0.0, score / 100)),
                    "gaps": gaps,
                }
            )
        )
    return sorted(
        [candidate for candidate in ranked if candidate.ranking_score > 0],
        key=lambda item: (-item.ranking_score, item.name.lower()),
    )


def render_multi_target_research_summary(result: MultiTargetResearchResult) -> str:
    """Render a Slack/CLI-safe deterministic summary for multi-target research."""

    title = f"Multi-target research: {result.plan.topic}"
    if result.comparison_ready:
        answer = (
            f"The run found source-backed packets for {len(result.packets)} "
            "separate targets and can support the requested comparison."
        )
    else:
        answer = "The run did not yet have enough per-target source evidence for the comparison."
    detailed_summary = _multi_target_detailed_summary(result)
    lines = [
        title,
        "",
        "Answer",
        answer,
        "",
        "Detailed Summary",
        detailed_summary,
        "",
        "Source-backed comparison table",
        "| Target | Evidence status | Source URLs | Gaps |",
        "|---|---|---|---|",
    ]
    for packet in result.packets:
        urls = ", ".join(_packet_source_urls(packet)[:3]) or "none"
        gaps = "; ".join(packet.gaps[:3]) or "none"
        status = "sufficient" if packet.source_sufficient else packet.extraction_status
        lines.append(f"| {packet.target_name} | {status} | {urls} | {gaps} |")
    if result.blockers:
        lines.extend(["", "Blockers", *[f"* {blocker}" for blocker in result.blockers[:5]]])
    lines.extend(
        [
            "",
            "What looks real vs marketing language",
            (
                "* Treat target-specific official/help/policy pages as the strongest evidence; "
                "listicles and adjacent enterprise AI announcements are candidate discovery "
                "signals, not enough for feature claims."
            ),
            "",
            "Keystone product/design implications",
            "* Require source-visible safety claims before comparing teen-safety feature depth.",
            "* Track trusted-contact and escalation claims separately from generic safety wording.",
            "* Keep evidence gaps visible when product pages are missing or snippet-only.",
            "",
            "Metadata",
            f"* Multi-target pass types: {', '.join(result.pass_types)}",
            (
                "* Multi-target sufficiency: "
                f"{result.diagnostics.get('ready_packet_count', 0)}/"
                f"{result.plan.desired_count} target packet(s) sufficient"
            ),
        ]
    )
    return "\n".join(lines).strip()


def _multi_target_detailed_summary(result: MultiTargetResearchResult) -> str:
    ready_packets = [packet for packet in result.packets if packet.source_sufficient]
    weak_packets = [packet for packet in result.packets if not packet.source_sufficient]
    if not result.packets:
        return (
            "The branch could not build per-target evidence packets. It needs named "
            "candidate targets plus public product, help, policy, or safety pages before "
            "a comparison would be evidence-backed."
        )
    if result.comparison_ready:
        target_names = ", ".join(packet.target_name for packet in ready_packets[:4])
        dimension_text = ", ".join(result.plan.requested_dimensions[:3])
        summary = (
            f"The comparison is supported for {target_names}. Each retained target has "
            "target-specific public source evidence tied to the requested dimensions"
        )
        if dimension_text:
            summary += f" ({dimension_text})"
        summary += "."
    else:
        ready_text = (
            ", ".join(packet.target_name for packet in ready_packets)
            if ready_packets
            else "none of the selected targets"
        )
        summary = (
            f"The comparison is still incomplete: {ready_text} had sufficient "
            "target-specific evidence, but at least one selected target is missing a "
            "product-specific source or requested feature evidence."
        )
    source_strengths = [
        _packet_source_strength_sentence(packet)
        for packet in result.packets[: result.plan.desired_count]
    ]
    source_strength_text = " ".join(item for item in source_strengths if item)
    if source_strength_text:
        summary = f"{summary} {source_strength_text}"
    if weak_packets:
        gaps = "; ".join(
            f"{packet.target_name}: {', '.join(packet.gaps[:2])}" for packet in weak_packets[:3]
        )
        if gaps:
            summary = f"{summary} Remaining gaps: {gaps}."
    return summary


def _target_selection_diagnostics(
    *,
    discovery: Sequence[CandidateTarget],
    selected: Sequence[CandidateTarget],
    packets: Sequence[PerTargetResearchPacket],
) -> dict[str, Any]:
    """Return compact target-selection diagnostics for traces and artifacts."""

    packet_by_name = {packet.target_name: packet for packet in packets}
    selected_rows: list[dict[str, Any]] = []
    for candidate in selected:
        packet = packet_by_name.get(candidate.name)
        selected_rows.append(
            {
                "name": candidate.name,
                "ranking_score": candidate.ranking_score,
                "confidence": round(candidate.confidence, 3),
                "evidence_count": len(candidate.evidence),
                "official_url_count": len(candidate.url_candidates),
                "source_sufficient": bool(packet and packet.source_sufficient),
                "gaps": list(packet.gaps[:3]) if packet else [],
            }
        )
    return {
        "selected_targets": selected_rows,
        "top_candidates": [
            {
                "name": candidate.name,
                "ranking_score": candidate.ranking_score,
                "confidence": round(candidate.confidence, 3),
                "evidence_count": len(candidate.evidence),
                "official_url_count": len(candidate.url_candidates),
                "gaps": list(candidate.gaps[:3]),
            }
            for candidate in discovery[:6]
        ],
        "weak_targets": [
            {
                "name": packet.target_name,
                "gaps": list(packet.gaps[:3]),
            }
            for packet in packets
            if not packet.source_sufficient
        ],
    }


def _packet_source_strength_sentence(packet: PerTargetResearchPacket) -> str:
    urls = _packet_source_urls(packet)
    source_count = len(urls)
    if packet.source_sufficient:
        return (
            f"{packet.target_name} has {source_count} retained public source"
            f"{'' if source_count == 1 else 's'} for this comparison."
        )
    gaps = ", ".join(packet.gaps[:2]) or "insufficient source evidence"
    return f"{packet.target_name} is not comparison-ready yet because {gaps}."


def _research_with_single_substitution_round(
    plan: MultiTargetResearchPlan,
    *,
    selected_candidates: list[CandidateTarget],
    live_search: bool,
    quality_budget: AgentQualityBudget | None,
    agents_web_search_max_calls: int | None,
    retrieval_hint: RetrievalHint | None,
    retrieve_profile: Callable[..., tuple[CompanyProfile, dict[str, Any]]] | None,
) -> tuple[list[CandidateTarget], list[PerTargetResearchPacket]]:
    selected: list[CandidateTarget] = []
    packets: list[PerTargetResearchPacket] = []
    candidate_iter = iter(selected_candidates)
    while len(selected) < plan.desired_count:
        try:
            selected.append(next(candidate_iter))
        except StopIteration:
            break
    packets = _research_selected_targets(
        plan,
        selected,
        live_search=live_search,
        quality_budget=quality_budget,
        agents_web_search_max_calls=agents_web_search_max_calls,
        retrieval_hint=retrieval_hint,
        retrieve_profile=retrieve_profile,
    )
    weak_indexes = [idx for idx, packet in enumerate(packets) if not packet.source_sufficient]
    if not weak_indexes:
        return selected, packets
    remaining = [candidate for candidate in selected_candidates if candidate not in selected]
    if not remaining:
        return selected, packets
    for weak_index in weak_indexes:
        if not remaining:
            break
        substitute = remaining.pop(0)
        substitute_packet = _research_selected_targets(
            plan,
            [substitute],
            live_search=live_search,
            quality_budget=quality_budget,
            agents_web_search_max_calls=agents_web_search_max_calls,
            retrieval_hint=retrieval_hint,
            retrieve_profile=retrieve_profile,
        )[0]
        if substitute_packet.source_sufficient:
            selected[weak_index] = substitute
            packets[weak_index] = substitute_packet
    return selected, packets


def _research_selected_targets(
    plan: MultiTargetResearchPlan,
    candidates: list[CandidateTarget],
    *,
    live_search: bool,
    quality_budget: AgentQualityBudget | None,
    agents_web_search_max_calls: int | None,
    retrieval_hint: RetrievalHint | None,
    retrieve_profile: Callable[..., tuple[CompanyProfile, dict[str, Any]]] | None,
) -> list[PerTargetResearchPacket]:
    if not candidates:
        return []
    retrieve_profile = retrieve_profile or _default_retrieve_profile
    max_results = max(4, min(8, getattr(quality_budget, "max_results", 6) or 6))

    def run_one(candidate: CandidateTarget) -> PerTargetResearchPacket:
        if not live_search:
            return PerTargetResearchPacket(
                target_name=candidate.name,
                canonical_url=(candidate.url_candidates[0] if candidate.url_candidates else ""),
                source_sufficient=False,
                gaps=["live search disabled for multi-target depth retrieval"],
            )
        profile, metadata = retrieve_profile(
            company=candidate.name,
            company_url=(candidate.url_candidates[0] if candidate.url_candidates else None),
            request_text=_target_depth_request_text(plan, candidate.name),
            max_results=max_results,
            query_builder=lambda company, company_url=None: _target_depth_queries(
                plan,
                company,
                company_url,
            ),
            agents_web_search_max_calls=agents_web_search_max_calls,
            agents_web_search_parallel=False,
            retrieval_hint=retrieval_hint,
        )
        return _packet_from_profile(plan, candidate, profile, metadata)

    ordered: list[PerTargetResearchPacket | None] = [None] * len(candidates)
    with ThreadPoolExecutor(max_workers=_target_depth_concurrency(plan)) as executor:
        futures = {
            executor.submit(run_one, candidate): index
            for index, candidate in enumerate(candidates)
        }
        for future in as_completed(futures):
            index = futures[future]
            ordered[index] = future.result()
    return [packet for packet in ordered if packet is not None]


def _packet_from_profile(
    plan: MultiTargetResearchPlan,
    candidate: CandidateTarget,
    profile: CompanyProfile,
    metadata: dict[str, Any],
) -> PerTargetResearchPacket:
    source_refs = _prioritized_packet_source_refs(
        [_source_record_payload(source) for source in profile.sources[:8]],
        plan=plan,
        candidate=candidate,
    )
    triage = triage_source_candidates(
        request_text=_target_depth_request_text(plan, candidate.name),
        candidates=source_refs,
        agent_name="business_research_analyst",
        max_retain=5,
    )
    gaps: list[str] = []
    if not source_refs:
        gaps.append("missing public source evidence")
    canonical_url = candidate.url_candidates[0] if candidate.url_candidates else ""
    if not canonical_url:
        canonical_url = _first_official_source_url(candidate, profile.sources)
    if not canonical_url:
        gaps.append("missing official or canonical public URL")
    official_source_refs = [
        source
        for source in source_refs
        if _looks_like_official_target_url(str(source.get("url") or ""), candidate.normalized_key)
    ]
    if not official_source_refs:
        gaps.append("missing target-specific official/product source")
    target_dimension_source_refs = [
        source
        for source in official_source_refs
        if _source_matches_requested_dimensions(source, plan)
    ]
    if not target_dimension_source_refs:
        gaps.append("missing target-specific requested feature evidence")
    extracted_count = sum(
        1
        for source in source_refs
        if str(source.get("evidence_excerpt") or "").strip()
        or source.get("supported_claims")
    )
    if extracted_count == 0:
        gaps.append("snippet-only or unextracted source packet")
    source_sufficient = bool(source_refs and not gaps)
    return PerTargetResearchPacket(
        target_name=candidate.name,
        canonical_url=canonical_url,
        source_refs=source_refs,
        extraction_status=("extracted" if extracted_count else "snippet_only"),
        retained_source_ids=list(triage.retained_source_ids),
        deepened_source_ids=list(triage.deepen_source_ids),
        rejected_source_ids=list(triage.rejected_source_ids),
        source_sufficient=source_sufficient,
        gaps=list(dict.fromkeys(gaps)),
        retrieval_diagnostics=metadata.get("retrieval_diagnostics") or {},
    )


def _prioritized_packet_source_refs(
    source_refs: list[dict[str, Any]],
    *,
    plan: MultiTargetResearchPlan,
    candidate: CandidateTarget,
) -> list[dict[str, Any]]:
    """Prefer target-specific feature evidence over generic official/help pages."""

    def rank(source: dict[str, Any]) -> tuple[int, str]:
        url = str(source.get("url") or "")
        official = _looks_like_official_target_url(url, candidate.normalized_key)
        dimension = _source_matches_requested_dimensions(source, plan)
        extracted = bool(
            str(source.get("evidence_excerpt") or "").strip()
            or source.get("supported_claims")
        )
        if official and dimension:
            bucket = 0
        elif official:
            bucket = 1
        elif dimension:
            bucket = 2
        else:
            bucket = 3
        if not extracted:
            bucket += 4
        return bucket, url

    return sorted(source_refs, key=rank)


def _multi_target_search_client(
    plan: MultiTargetResearchPlan,
    *,
    agents_web_search_max_calls: int | None,
    retrieval_hint: RetrievalHint | None,
    search_provider_builder: Callable[..., Any],
) -> HybridSearchProvider:
    from keystone_agents.live_retrieval import build_shared_search_provider_config

    settings = load_settings()
    search_config = build_shared_search_provider_config(
        requested_provider=None,
        configured_provider=settings.search_provider,
        serper_enabled=bool(getattr(settings, "serper_enabled", False)),
        agents_web_search_max_calls=agents_web_search_max_calls,
        agents_web_search_parallel=True,
    )
    autonomy_hint = derive_request_autonomy_hint(
        agent_name="business_research_analyst",
        request_text=plan.request_text or plan.topic,
        agent_hint=retrieval_hint,
    )
    return HybridSearchProvider(
        provider_sequence=search_config.provider_sequence,
        deepening_provider_sequence=search_config.deepening_provider_sequence,
        autonomy_hint=autonomy_hint,
        quality_assessor=lambda results, _query: _candidate_discovery_quality(results, plan),
        provider_factory=lambda provider_name: search_provider_builder(
            provider=provider_name,
            live=True,
        ),
        parallel_provider_fanout=search_config.parallel_provider_fanout,
        provider_request_budget=search_config.provider_request_budget,
    )


def _candidate_discovery_quality(results: Sequence[Any], plan: MultiTargetResearchPlan) -> Any:
    from keystone_agents.retrieval_policy import RetrievalQualityAssessment

    candidates = candidate_targets_from_search_results(results, plan=plan)
    reasons = []
    needs_precision = len(candidates) < plan.desired_count
    if needs_precision:
        reasons.append("too few candidate targets discovered")
    return RetrievalQualityAssessment(
        result_count=len(results),
        unique_domain_count=len({_domain(_result_url(result)) for result in results}),
        duplicate_ratio=0.0,
        primary_source_count=len(results),
        official_source_present=any(candidate.url_candidates for candidate in candidates),
        linkedin_source_present=False,
        recent_signal_count=sum(1 for result in results if _result_is_recent(result)),
        needs_precision_search=needs_precision,
        needs_structured_enrichment=False,
        needs_search_review=needs_precision,
        reasons=tuple(reasons),
        source_lane_count=0,
        source_lane_labels=(),
        missing_source_lanes=(),
        source_coverage={},
    )


def _candidate_discovery_queries(plan: MultiTargetResearchPlan) -> list[str]:
    dims = " ".join(plan.requested_dimensions[:4])
    topic = plan.topic
    queries = [
        f"{topic} named products official safety policy pages {dims}",
        f"{topic} official products {dims} current public source",
        f"{topic} product safety pages teen users trusted contact escalation",
        f"{topic} comparison {dims} 2026",
        f"{topic} teen safety escalation trusted contact product pages",
        f"{topic} public safety policy help center teen users",
    ]
    if _topic_is_ai_companion_or_chatbot(plan):
        queries.extend(
            [
                "Character.AI Replika Nomi ChatGPT teen safety trusted contact escalation official",
                "AI companion chatbot official safety center teen users Character.AI Replika Nomi",
            ]
        )
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _candidate_discovery_repair_queries(plan: MultiTargetResearchPlan) -> list[str]:
    dims = " ".join(plan.requested_dimensions[:4])
    topic = plan.topic
    queries = [
        f"{topic} official safety pages product help policy {dims}",
        f"{topic} official product safety center named products {dims}",
        f"{topic} named products public help center safety policy",
        f"{topic} current product pages teen users safety controls",
    ]
    if _topic_is_ai_companion_or_chatbot(plan):
        queries.append(
            "AI chatbot companion products official teen safety policy pages "
            "ChatGPT Character.AI Replika Nomi"
        )
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _target_depth_queries(
    plan: MultiTargetResearchPlan,
    company: str,
    company_url: str | None = None,
) -> list[str]:
    dims = " ".join(plan.requested_dimensions[:4])
    queries = [
        f"{company} official product safety page teen users trusted contact escalation",
        f"{company} official safety policy teen trusted contact escalation",
        f"{company} help center teen safety escalation trusted contact",
        f"{company} public policy minors safety AI chatbot",
        f"{company} {dims} current public source",
    ]
    seeded_queries = [*queries, *build_company_research_queries(company, company_url)[:4]]
    return list(dict.fromkeys(seeded_queries))


def _target_depth_request_text(plan: MultiTargetResearchPlan, target_name: str) -> str:
    return (
        f"Read public sources for {target_name} as one target in this multi-target comparison. "
        f"Requested dimensions: {', '.join(plan.requested_dimensions)}. "
        "Use target-specific public product, safety, policy, help, or credible public pages."
    )


def _candidate_names_from_result(
    mapping: dict[str, str],
    *,
    plan: MultiTargetResearchPlan,
) -> list[str]:
    names: list[str] = []
    url_name = _candidate_name_from_domain(mapping["url"])
    if url_name:
        names.append(url_name)
    title = mapping["title"]
    names.extend(_names_from_comparison_title(title))
    names.extend(_capitalized_product_names(title))
    snippet_names = _capitalized_product_names(mapping["snippet"])
    names.extend(snippet_names[:3])
    cleaned: list[str] = []
    topic_words = set(re.findall(r"\b[a-z0-9]{3,}\b", plan.topic.lower()))
    for name in names:
        normalized = " ".join(name.split()).strip(" .,:;-")
        normalized = _canonical_candidate_name(normalized, plan=plan)
        if not normalized or normalized in _ENTITY_STOPWORDS:
            continue
        key = _candidate_key(normalized)
        if (
            not key
            or key in topic_words
            or key in _NON_PRODUCT_CANDIDATE_KEYS
            or _looks_like_category_target(normalized)
        ):
            continue
        cleaned.append(normalized)
    return list(dict.fromkeys(cleaned))[:6]


def _canonical_candidate_name(name: str, *, plan: MultiTargetResearchPlan) -> str:
    normalized = " ".join(str(name or "").split()).strip(" .,:;-")
    key = _candidate_key(normalized)
    if key in {"chatgpt", "openai"} and _topic_is_ai_companion_or_chatbot(plan):
        return "ChatGPT"
    return normalized


def _names_from_comparison_title(title: str) -> list[str]:
    if not re.search(r"\b(?:vs\.?|versus)\b", title, flags=re.I):
        return []
    head = re.split(r"[:|()-]", title, maxsplit=1)[0]
    parts = re.split(r"\s+(?:vs\.?|versus)\s+", head, flags=re.I)
    return [part.strip() for part in parts if part.strip()]


def _capitalized_product_names(text: str) -> list[str]:
    matches = re.findall(
        r"\b([A-Z][A-Za-z0-9.&'-]*(?:\s+[A-Z][A-Za-z0-9.&'-]*){0,2})\b",
        str(text or ""),
    )
    return [match.strip() for match in matches]


def _candidate_name_from_domain(url: str) -> str:
    domain = _domain(url)
    if not domain:
        return ""
    if _looks_like_source_domain(domain):
        return ""
    label = _brand_label_from_domain(domain)
    if label in _GENERIC_SOURCE_DOMAINS or len(label) <= 2:
        return ""
    special = {
        "openai": "ChatGPT",
        "chatgpt": "ChatGPT",
        "character": "Character.AI",
        "copilot": "Microsoft Copilot",
        "replika": "Replika",
    }
    return special.get(label, label.replace("-", " ").title())


def _candidate_score(
    candidate: CandidateTarget,
    *,
    plan: MultiTargetResearchPlan,
) -> tuple[int, list[str]]:
    providers = {item.provider for item in candidate.evidence if item.provider}
    queries = {item.query for item in candidate.evidence if item.query}
    urls = {item.url for item in candidate.evidence if item.url}
    haystack = " ".join(
        f"{item.title} {item.snippet} {item.url}" for item in candidate.evidence
    ).lower()
    dimension_hits = sum(
        1
        for dimension in plan.requested_dimensions
        if all(token in haystack for token in re.findall(r"\b[a-z0-9]{3,}\b", dimension.lower()))
    )
    official = bool(candidate.url_candidates)
    score = 20
    score += min(25, len(candidate.evidence) * 5)
    score += min(15, len(providers) * 5)
    score += min(15, len(queries) * 5)
    score += 25 if official else 0
    score += min(20, dimension_hits * 8)
    if any(_result_text_is_recent(item.title + " " + item.snippet) for item in candidate.evidence):
        score += 10
    if not official:
        score -= 15
    if not dimension_hits:
        score -= 10
    gaps: list[str] = []
    if _topic_is_ai_companion_or_chatbot(plan) and not _candidate_matches_ai_companion_topic(
        candidate
    ):
        score = 0
        gaps.append("candidate evidence does not match AI companion/chatbot product topic")
    if not official:
        gaps.append("no official or product-domain URL found during discovery")
    if not dimension_hits:
        gaps.append("candidate evidence does not mention requested dimensions")
    if len(urls) == 1 and not official:
        gaps.append("single weak discovery source")
    return max(0, min(100, score)), gaps


def _candidate_matches_ai_companion_topic(candidate: CandidateTarget) -> bool:
    if candidate.normalized_key in _AI_COMPANION_PRODUCT_KEYS:
        return True
    haystack = " ".join(
        f"{candidate.name} {item.title} {item.snippet} {item.url}"
        for item in candidate.evidence
    ).lower()
    return any(marker in haystack for marker in _AI_COMPANION_TOPIC_MARKERS)


def _multi_target_blockers(
    plan: MultiTargetResearchPlan,
    candidates: list[CandidateTarget],
    packets: list[PerTargetResearchPacket],
) -> list[str]:
    blockers: list[str] = []
    if len(candidates) < plan.desired_count:
        blockers.append(
            f"breadth gap: found {len(candidates)} candidate target(s), need {plan.desired_count}"
        )
    ready = [packet for packet in packets if packet.source_sufficient]
    if len(ready) < plan.desired_count:
        blockers.append(
            "depth gap: "
            f"{len(ready)}/{plan.desired_count} target packet(s) "
            "have sufficient source evidence"
        )
    for packet in packets:
        if not packet.source_sufficient:
            blockers.append(f"{packet.target_name}: " + "; ".join(packet.gaps[:3]))
    return list(dict.fromkeys(blockers))


def _source_record_payload(source: SourceRecord) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "title": source.title,
        "url": source.url,
        "source_type": source.source_type,
        "supported_claims": list(source.supported_claims),
        "evidence_excerpt": source.evidence_excerpt,
        "confidence": source.confidence,
        "published_at": source.published_at,
    }


def _source_matches_requested_dimensions(
    source: dict[str, Any],
    plan: MultiTargetResearchPlan,
) -> bool:
    haystack = " ".join(
        [
            str(source.get("title") or ""),
            str(source.get("url") or ""),
            " ".join(str(item) for item in source.get("supported_claims") or []),
            str(source.get("evidence_excerpt") or ""),
        ]
    ).lower()
    for dimension in plan.requested_dimensions:
        tokens = re.findall(r"\b[a-z0-9]{3,}\b", dimension.lower())
        if not tokens:
            continue
        if len(tokens) == 1 and tokens[0] in haystack:
            return True
        if len(tokens) > 1 and all(token in haystack for token in tokens):
            return True
    return False


def _first_official_source_url(candidate: CandidateTarget, sources: Sequence[SourceRecord]) -> str:
    key = candidate.normalized_key
    for source in sources:
        if _looks_like_official_target_url(source.url, key):
            return source.url
    return ""


def _packet_source_urls(packet: PerTargetResearchPacket) -> list[str]:
    return list(
        dict.fromkeys(
            str(item.get("url") or "").strip()
            for item in packet.source_refs
            if str(item.get("url") or "").strip()
        )
    )


def _result_mapping(result: Any) -> dict[str, str]:
    if isinstance(result, dict):
        title = result.get("title") or ""
        url = result.get("url") or result.get("link") or ""
        snippet = result.get("snippet") or result.get("content") or ""
        source = result.get("source") or result.get("provider") or ""
    else:
        title = getattr(result, "title", "") or ""
        url = getattr(result, "url", "") or getattr(result, "link", "") or ""
        snippet = getattr(result, "snippet", "") or getattr(result, "content", "") or ""
        source = getattr(result, "source", "") or getattr(result, "provider", "") or ""
    return {
        "title": str(title),
        "url": str(url),
        "snippet": str(snippet),
        "source": str(source),
    }


def _result_url(result: Any) -> str:
    return _result_mapping(result)["url"]


def _result_is_recent(result: Any) -> bool:
    mapping = _result_mapping(result)
    return _result_text_is_recent(" ".join(mapping.values()))


def _result_text_is_recent(text: str) -> bool:
    return bool(re.search(r"\b(?:2025|2026|current|latest|recent|new)\b", text, flags=re.I))


def _domain(url: str) -> str:
    parsed = urlparse(str(url or ""))
    return parsed.netloc.lower().removeprefix("www.")


def _candidate_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _looks_like_official_target_url(url: str, key: str) -> bool:
    domain = _domain(url)
    if not domain or not key:
        return False
    if _looks_like_source_domain(domain):
        return False
    label = _brand_label_from_domain(domain)
    if label in _GENERIC_SOURCE_DOMAINS:
        return False
    normalized_label = re.sub(r"[^a-z0-9]+", "", label)
    official_keys = {key, *_PRODUCT_DOMAIN_ALIASES.get(key, set())}
    return any(official_key in normalized_label for official_key in official_keys)


def _looks_like_source_domain(domain: str) -> bool:
    clean = str(domain or "").lower().removeprefix("www.")
    if clean.endswith((".gov", ".edu")):
        return True
    label = _brand_label_from_domain(clean)
    normalized_label = re.sub(r"[^a-z0-9]+", "", label)
    return label in _GENERIC_SOURCE_DOMAINS or normalized_label in _NON_PRODUCT_CANDIDATE_KEYS


def _brand_label_from_domain(domain: str) -> str:
    labels = [part for part in str(domain or "").split(".") if part]
    if not labels:
        return ""
    if labels[0] in _COMMON_SUBDOMAIN_LABELS and len(labels) >= 2:
        return labels[1]
    return labels[0]


def _looks_like_category_target(value: str) -> bool:
    text = str(value or "").lower()
    return bool(
        re.search(
            r"\b(?:products?|companies|tools?|chatbots?|companions?|platforms?|category)\b",
            text,
        )
    )


def _topic_is_ai_companion_or_chatbot(plan: MultiTargetResearchPlan) -> bool:
    text = f"{plan.topic} {plan.request_text}".lower()
    return bool(re.search(r"\b(?:ai\s+)?(?:companions?|chatbots?|chatgpt)\b", text))


def _desired_count(plan: dict[str, Any], request_text: str) -> int:
    explicit = _explicit_desired_count(plan, request_text)
    if explicit is not None:
        return explicit
    return 3


def _explicit_desired_count(plan: dict[str, Any], request_text: str) -> int | None:
    raw = plan.get("desired_count")
    if isinstance(raw, int):
        if raw <= 1:
            return None
        return max(2, min(6, raw))
    match = re.search(
        r"\b(?:compare|find|identify|return|list)\s+(?:how\s+)?(\d{1,2})\b",
        request_text,
        re.I,
    )
    if match:
        return max(2, min(6, int(match.group(1))))
    word_match = re.search(
        r"\b(?:compare|find|identify|return|list)\s+(?:how\s+)?"
        r"(two|three|four|five|six)\b",
        request_text,
        re.I,
    )
    if word_match:
        return {
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
        }[word_match.group(1).lower()]
    return None


def _request_dimensions(request_text: str) -> list[str]:
    dimensions = []
    lower = str(request_text or "").lower()
    for dimension in _DEFAULT_DIMENSIONS:
        if any(token in lower for token in dimension.split()):
            dimensions.append(dimension)
    return dimensions


def _target_depth_concurrency(plan: MultiTargetResearchPlan) -> int:
    return max(1, min(plan.desired_count, 3))


def _default_retrieve_profile(**_kwargs: Any) -> tuple[CompanyProfile, dict[str, Any]]:
    raise RuntimeError("retrieve_profile must be supplied by the workflow runner")
