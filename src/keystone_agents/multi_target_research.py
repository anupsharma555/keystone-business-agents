"""Multi-target research planning, discovery, and source sufficiency helpers."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from inspect import Parameter, signature
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from keystone_agents.agents.business_research_analyst import build_company_research_queries
from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.config import load_settings
from keystone_agents.contracts.completion import (
    bounded_search_receipt_from_provider_telemetry,
    build_count_request_coverage,
)
from keystone_agents.contracts.evidence import (
    ResearchEvidenceGapReceipt,
    compile_research_evidence_gap_receipt,
)
from keystone_agents.quality_budget import AgentQualityBudget
from keystone_agents.retrieval_policy import (
    HybridSearchProvider,
    derive_request_autonomy_hint,
    merge_search_results,
)
from keystone_agents.runtime.provider_context import ProviderExecutionContext
from keystone_agents.schemas.company_profile import CompanyProfile, SourceRecord
from keystone_agents.schemas.request_coverage import RequestCoverage
from keystone_agents.schemas.retrieval import RetrievalHint
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
_COMMON_SUBDOMAIN_LABELS = {
    "about",
    "blog",
    "help",
    "learn",
    "marketplace",
    "news",
    "newsroom",
    "policy",
    "press",
    "resources",
    "support",
    "www",
}
_HOSTED_CONTENT_ROOTS = {
    "blogspot.com",
    "github.io",
    "medium.com",
    "notion.site",
    "sites.google.com",
    "substack.com",
    "webflow.io",
    "wixsite.com",
    "wordpress.com",
}
_SOURCE_CONTAINER_PATH_MARKERS = (
    "/article",
    "/blog",
    "/categories/",
    "/company/",
    "/content/",
    "/industry-reports/",
    "/insights/",
    "/market",
    "/news",
    "/organization/",
    "/product/",
    "/profiles/",
    "/report",
    "/resources/",
)
_FIRST_PARTY_PRODUCT_PATH_MARKERS = (
    "/clinical-",
    "/platform",
    "/research",
    "/solution",
    "/technology",
)
_CANDIDATE_NOISE_WORDS = {
    "about",
    "advanced",
    "admissions",
    "ai",
    "alternatives",
    "ambient",
    "analysis",
    "apr",
    "are",
    "artificial",
    "behavioral",
    "best",
    "b",
    "blog",
    "but",
    "can",
    "care",
    "challenger",
    "clinical",
    "company",
    "competitor",
    "competitors",
    "corp",
    "current",
    "description",
    "diagnostics",
    "discover",
    "developer",
    "d",
    "driving",
    "dynamic",
    "employees",
    "enhancing",
    "esps",
    "feb",
    "financials",
    "funding",
    "further",
    "general",
    "growth",
    "health",
    "hipaa",
    "industry",
    "information",
    "intelligence",
    "investors",
    "jan",
    "jul",
    "jun",
    "leveraging",
    "market",
    "marketplace",
    "mar",
    "mental",
    "multimodal",
    "news",
    "nov",
    "oct",
    "official",
    "overview",
    "product",
    "profile",
    "preview",
    "powered",
    "relevant",
    "report",
    "r",
    "retail",
    "sep",
    "share",
    "similar",
    "size",
    "solutions",
    "style",
    "technology",
    "the",
    "these",
    "third",
    "party",
    "top",
    "track",
    "valuation",
    "users",
    "driven",
}
_DEFAULT_DIMENSIONS = (
    "product or service overlap",
    "target-market overlap",
    "public source evidence",
)
_RELATIONSHIP_DIMENSIONS = frozenset(
    {
        "alternative",
        "alternatives",
        "competition",
        "competitive landscape",
        "competitor",
        "competitors",
        "market landscape",
        "peer companies",
        "peers",
        "similar companies",
    }
)
_SOURCE_EVIDENCE_DIMENSION_RE = re.compile(
    r"\b(?:official|primary|independent|public|current|visible|reader[- ]usable)\b"
    r".{0,32}\b(?:source|sources|evidence|url|urls|citation|citations)\b"
    r"|\b(?:source|sources|evidence|url|urls|citation|citations)\b"
    r".{0,32}\b(?:quality|strength|official|primary|independent|public|current|visible)\b",
    re.I,
)
_MODALITY_PATTERNS = (
    r"\b(?:voice|speech|acoustic|prosody|audio)\b",
    r"\b(?:text|language|linguistic|nlp)\b",
    r"\b(?:image|imaging|video|visual|facial)\b",
    r"\b(?:sleep|actigraphy)\b",
    r"\b(?:activity|movement|mobility|wearable)\b",
    r"\b(?:heart\s+rate|hrv|eeg|physiolog\w*|biometric\w*)\b",
    r"\b(?:clinical\s+(?:history|context|record)|ehr|emr|medical\s+record)\b",
    r"\b(?:patient[- ]reported|questionnaire|survey|outcome\s+measure)\b",
)


class MultiTargetResearchPlan(BaseModel):
    """Executable plan for category/comparison research with multiple targets."""

    schema_: Literal["keystone.multi_target_research.v1"] = Field(
        default=MULTI_TARGET_RESEARCH_SCHEMA,
        alias="schema",
    )
    topic: str
    anchor_target: str = ""
    anchor_source: Literal["none", "explicit", "legacy_single_entity"] = "none"
    fixed_targets: list[str] = Field(default_factory=list)
    planning_blockers: list[str] = Field(default_factory=list)
    desired_count: int = Field(default=3, ge=2, le=6)
    desired_count_mode: Literal[
        "unspecified", "target", "maximum", "minimum", "exact"
    ] = "unspecified"
    desired_count_scope: Literal["unspecified", "total", "additional"] = "unspecified"
    requested_dimensions: list[str] = Field(default_factory=list)
    source_constraints: list[str] = Field(default_factory=list)
    pass_budget: int = Field(default=2, ge=1, le=3)
    cost_profile: str = ""
    request_text: str = ""

    @field_validator(
        "topic",
        "anchor_target",
        "cost_profile",
        "request_text",
        mode="before",
    )
    @classmethod
    def _clean_scalar(cls, value: Any) -> str:
        return " ".join(str(value or "").split()).strip()

    @field_validator(
        "fixed_targets",
        "planning_blockers",
        "requested_dimensions",
        "source_constraints",
        mode="before",
    )
    @classmethod
    def _clean_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(" ".join(str(item or "").split()) for item in value if item))

    @property
    def peer_goal(self) -> int:
        """Return the comparison-target count after accounting for the anchor."""

        if self.anchor_target and self.desired_count_scope == "total":
            return max(1, self.desired_count - 1)
        return self.desired_count


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
    dimension_source_ids: dict[str, list[str]] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    retrieval_diagnostics: dict[str, Any] = Field(default_factory=dict)


class MultiTargetReadiness(BaseModel):
    """Derived receipt proving whether the complete interpreted ask is ready."""

    assessed: bool = False
    rubric_dimensions: list[str] = Field(default_factory=list)
    anchor_required: bool = False
    anchor_ready: bool = False
    peer_goal: int = 0
    ready_peer_count: int = 0
    rubric_ready_peer_count: int = 0
    bounded_search_exhausted: bool = False
    count_contract_satisfied: bool = False
    peer_comparison_ready: bool = False
    whole_request_ready: bool = False
    gaps: list[str] = Field(default_factory=list)


class MultiTargetResearchResult(BaseModel):
    """Research output for a multi-target Business Research branch."""

    schema_: Literal["keystone.multi_target_research.v1"] = Field(
        default=MULTI_TARGET_RESEARCH_SCHEMA,
        alias="schema",
    )
    plan: MultiTargetResearchPlan
    candidate_targets: list[CandidateTarget] = Field(default_factory=list)
    selected_targets: list[str] = Field(default_factory=list)
    anchor_packet: PerTargetResearchPacket | None = None
    packets: list[PerTargetResearchPacket] = Field(default_factory=list)
    comparison_ready: bool = False
    readiness: MultiTargetReadiness = Field(default_factory=MultiTargetReadiness)
    request_coverage: RequestCoverage = Field(default_factory=RequestCoverage)
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
        contract = authority.compile_request_contract(raw_request=request_text)
        assert contract is not None
        return bool(
            contract.owner_agent == "business_research_analyst"
            and contract.intent in {"company_research", "research_brief"}
            and contract.task_objective in {"entity_research", "source_research"}
            and contract.output.artifact_type
            in {"research_brief", "source_summary"}
            and (
                (
                    contract.completion.desired_count is not None
                    and contract.completion.desired_count > 1
                )
                or len(set(contract.target.required_entities)) > 1
                or contract.target.discovery_required
            )
        )
    if authority.invalid:
        return False

    plan = manual_plan if isinstance(manual_plan, dict) else {}
    required_entities = {
        str(item).strip()
        for item in plan.get("required_entities") or []
        if str(item).strip()
    }
    if (
        str(plan.get("target_agent") or "") == "business_research_analyst"
        and str(plan.get("intent") or "") in {"company_research", "research_brief"}
        and len(required_entities) > 1
    ):
        return True
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

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.invalid:
        raise ValueError("invalid manual request plan cannot build multi-target research")
    canonical_plan = authority.plan if authority.canonical else None
    request_contract = (
        authority.compile_request_contract(raw_request=request_text)
        if canonical_plan is not None
        else None
    )
    plan = (
        canonical_plan.model_dump(mode="json")
        if canonical_plan is not None
        else manual_plan
        if isinstance(manual_plan, dict)
        else {}
    )
    required_entities = (
        list(request_contract.target.required_entities)
        if request_contract is not None
        else [
            str(item).strip()
            for item in plan.get("required_entities") or []
            if str(item).strip()
        ]
    )
    fixed_targets = (
        list(request_contract.target.fixed_targets)
        if request_contract is not None
        else (
            list(dict.fromkeys(required_entities))
            if plan.get("requires_target_discovery") is not True
            and len(set(required_entities)) > 1
            else []
        )
    )
    explicit_anchor = (
        request_contract.target.anchor_entity
        if request_contract is not None
        else (
            str(plan.get("anchor_entity") or "").strip()
            if plan.get("requires_target_discovery") is True
            else ""
        )
    )
    legacy_anchor = (
        required_entities[0]
        if not explicit_anchor
        and (
            request_contract.target.discovery_required
            if request_contract is not None
            else plan.get("requires_target_discovery") is True
        )
        and len(required_entities) == 1
        else ""
    )
    anchor_target = explicit_anchor or legacy_anchor
    anchor_source: Literal["none", "explicit", "legacy_single_entity"]
    if request_contract is not None:
        anchor_source = request_contract.target.anchor_source
    elif explicit_anchor:
        anchor_source = "explicit"
    elif legacy_anchor:
        anchor_source = "legacy_single_entity"
    else:
        anchor_source = "none"
    topic = str(
        anchor_target or plan.get("primary_target") or target or request_text or ""
    ).strip()
    dimensions = (
        list(request_contract.evidence.required_dimensions)
        if request_contract is not None
        else [
            str(item)
            for item in plan.get("required_terms") or []
            if str(item).strip()
        ]
    )
    if not dimensions and canonical_plan is None:
        dimensions = _request_dimensions(request_text)
    constraints = (
        list(request_contract.evidence.constraints)
        if request_contract is not None
        else [
            str(item)
            for item in plan.get("constraints") or []
            if str(item).strip()
        ]
    )
    if request_contract is not None:
        declared_count = request_contract.completion.desired_count or 3
    else:
        declared_count = _desired_count(plan, request_text)
    requested_count = len(fixed_targets) if fixed_targets else declared_count
    desired_count_explicit = (
        request_contract.completion.desired_count is not None
        if request_contract is not None
        else plan.get("desired_count_explicit") is True
    )
    desired_count_mode = (
        request_contract.completion.desired_count_mode
        if request_contract is not None and desired_count_explicit
        else str(plan.get("desired_count_mode") or "unspecified")
        if desired_count_explicit
        else "unspecified"
    )
    if desired_count_explicit and desired_count_mode == "unspecified":
        desired_count_mode = "target"
    if fixed_targets:
        desired_count_mode = "exact"
    desired_count_scope = (
        request_contract.completion.desired_count_scope
        if request_contract is not None and desired_count_explicit
        else str(plan.get("desired_count_scope") or "unspecified")
        if desired_count_explicit
        else "unspecified"
    )
    planning_blockers: list[str] = []
    if (
        fixed_targets
        and desired_count_explicit
        and declared_count != len(fixed_targets)
    ):
        planning_blockers.append(
            f"explicit count {declared_count} conflicts with the "
            f"{len(fixed_targets)} named comparison targets"
        )
    return MultiTargetResearchPlan(
        topic=topic[:180],
        anchor_target=anchor_target[:180],
        anchor_source=anchor_source,
        fixed_targets=fixed_targets,
        planning_blockers=planning_blockers,
        desired_count=requested_count,
        desired_count_mode=desired_count_mode,
        desired_count_scope=desired_count_scope,
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

    if live_search:
        from keystone_agents.live_retrieval import live_retrieval_request_context

        execution_scope = live_retrieval_request_context(
            agents_web_search_max_calls=agents_web_search_max_calls,
            agents_web_search_parallel=True,
            manage_searxng_runtime=search_provider_builder is None,
        )
    else:
        execution_scope = nullcontext(None)
    with execution_scope as execution_context:
        return _run_multi_target_research_in_context(
            plan,
            live_search=live_search,
            quality_budget=quality_budget,
            agents_web_search_max_calls=agents_web_search_max_calls,
            retrieval_hint=retrieval_hint,
            search_provider_builder=search_provider_builder,
            retrieve_profile=retrieve_profile,
            execution_context=execution_context,
        )


def _run_multi_target_research_in_context(
    plan: MultiTargetResearchPlan,
    *,
    live_search: bool,
    quality_budget: AgentQualityBudget | None,
    agents_web_search_max_calls: int | None,
    retrieval_hint: RetrievalHint | None,
    search_provider_builder: Callable[..., Any] | None,
    retrieve_profile: Callable[..., tuple[CompanyProfile, dict[str, Any]]] | None,
    execution_context: ProviderExecutionContext | None,
) -> MultiTargetResearchResult:
    """Execute one research plan inside a shared provider request context."""

    pass_types: list[str] = []
    pre_researched_anchor: PerTargetResearchPacket | None = None
    if plan.anchor_target and not plan.fixed_targets:
        pass_types.append("anchor_depth")
        anchor_candidate = CandidateTarget(
            name=plan.anchor_target,
            normalized_key=_candidate_key(plan.anchor_target),
        )
        anchor_packets = _research_selected_targets(
            plan,
            [anchor_candidate],
            live_search=live_search,
            quality_budget=quality_budget,
            agents_web_search_max_calls=agents_web_search_max_calls,
            retrieval_hint=retrieval_hint,
            retrieve_profile=retrieve_profile,
            execution_context=execution_context,
        )
        pre_researched_anchor = anchor_packets[0] if anchor_packets else None
        anchor_gap_receipt: ResearchEvidenceGapReceipt | None = None
        if (
            live_search
            and pre_researched_anchor is not None
            and not _packet_covers_rubric(pre_researched_anchor, plan=plan)
        ):
            anchor_gap_receipt = _research_evidence_gap_receipt(
                plan,
                pre_researched_anchor,
                anchor=True,
            )
            pass_types.append("repair_anchor_research")
            repair_candidate = anchor_candidate.model_copy(
                update={
                    "url_candidates": (
                        [pre_researched_anchor.canonical_url]
                        if pre_researched_anchor.canonical_url
                        else []
                    )
                }
            )
            repaired_packets = _research_selected_targets(
                plan,
                [repair_candidate],
                live_search=live_search,
                quality_budget=quality_budget,
                agents_web_search_max_calls=agents_web_search_max_calls,
                retrieval_hint=retrieval_hint,
                retrieve_profile=retrieve_profile,
                execution_context=execution_context,
                repair_receipt=anchor_gap_receipt,
            )
            if repaired_packets:
                pre_researched_anchor = _merge_research_packets(
                    pre_researched_anchor,
                    repaired_packets[0],
                )
        if not (
            pre_researched_anchor is not None
            and _packet_covers_rubric(pre_researched_anchor, plan=plan)
        ):
            if "repair_anchor_research" not in pass_types:
                pass_types.append("repair_anchor_research")
            readiness = _multi_target_readiness(
                plan,
                anchor_packet=pre_researched_anchor,
                packets=[],
                bounded_search_exhausted=False,
            )
            blockers = list(
                dict.fromkeys(
                    [
                        *readiness.gaps,
                        (
                            "repair_anchor_research: deepen the anchor's official, "
                            "target-user, product, and requested-dimension evidence "
                            "before discovering comparison peers"
                        ),
                    ]
                )
            )
            return MultiTargetResearchResult(
                plan=plan,
                candidate_targets=[],
                selected_targets=[],
                anchor_packet=pre_researched_anchor,
                packets=[],
                comparison_ready=False,
                readiness=readiness,
                request_coverage=_multi_target_request_coverage(
                    plan,
                    readiness=readiness,
                ),
                blockers=blockers,
                diagnostics={
                    "candidate_count": 0,
                    "packet_count": 0,
                    "ready_packet_count": 0,
                    "processed_peer_count": 0,
                    "candidate_discovery_query_count": 0,
                    "candidate_breadth_repair_passes": 0,
                    "parallel_target_depth_limit": _target_depth_concurrency(plan),
                    "anchor_source": plan.anchor_source,
                    "anchor_ready": False,
                    "whole_request_ready": False,
                    "bounded_search_exhausted": False,
                    "next_action": "repair_anchor_research",
                    "anchor_evidence_gap": (
                        anchor_gap_receipt.model_dump(mode="json")
                        if anchor_gap_receipt is not None
                        else {}
                    ),
                    "anchor_comparison_signature": _anchor_comparison_signature(
                        plan,
                        pre_researched_anchor,
                    ),
                    "target_selection": {
                        "selected_targets": [],
                        "top_candidates": [],
                    },
                },
                pass_types=pass_types,
            )

    if plan.fixed_targets:
        pass_types.append("fixed_target_set")
        discovery = [
            CandidateTarget(
                name=name,
                normalized_key=_candidate_key(name),
            )
            for name in plan.fixed_targets
        ]
        discovery_diagnostics = {
            "candidate_discovery_query_count": 0,
            "candidate_breadth_repair_passes": 0,
            "candidate_count_after_initial": len(discovery),
            "candidate_count_after_repair": len(discovery),
            "target_set_mode": "fixed",
        }
    else:
        pass_types.append("candidate_discovery")
        discovery, discovery_diagnostics = _discover_candidate_targets_with_diagnostics(
            plan,
            live_search=live_search,
            quality_budget=quality_budget,
            agents_web_search_max_calls=agents_web_search_max_calls,
            retrieval_hint=retrieval_hint,
            search_provider_builder=search_provider_builder,
            anchor_packet=pre_researched_anchor,
            execution_context=execution_context,
        )
        if discovery_diagnostics.get("candidate_breadth_repair_passes"):
            pass_types.append("candidate_breadth_repair")
    pass_types.append("target_selection")
    peer_goal = plan.peer_goal
    selected = _depth_candidate_pool(discovery, peer_goal=peer_goal)
    selected, anchor_packet, packets, processed_peer_count = (
        _research_with_single_substitution_round(
            plan,
            selected_candidates=selected,
            live_search=live_search,
            quality_budget=quality_budget,
            agents_web_search_max_calls=agents_web_search_max_calls,
            retrieval_hint=retrieval_hint,
            retrieve_profile=retrieve_profile,
            allow_substitution=not plan.fixed_targets,
            pre_researched_anchor=pre_researched_anchor,
            execution_context=execution_context,
        )
    )
    if plan.anchor_target and plan.fixed_targets:
        pass_types.append("anchor_depth")
    pass_types.append("per_target_depth")
    if (
        not plan.fixed_targets
        and (
            len(packets) < peer_goal
            or any(not packet.source_sufficient for packet in packets)
        )
    ):
        pass_types.append("target_substitution")
    ready_packets = [packet for packet in packets if packet.source_sufficient]
    planned_attempt_count = int(
        discovery_diagnostics.get("candidate_discovery_query_count") or 0
    )
    provider_telemetry = discovery_diagnostics.get("provider_telemetry")
    bounded_search_receipt = bounded_search_receipt_from_provider_telemetry(
        provider_telemetry if isinstance(provider_telemetry, dict) else {},
        planned_attempt_count=planned_attempt_count,
        discovered_candidate_count=len(discovery),
        processed_candidate_count=processed_peer_count,
        budget_or_deadline_stopped=not live_search,
    )
    bounded_search_exhausted = bool(
        not plan.fixed_targets and bounded_search_receipt.exhausted
    )
    readiness = _multi_target_readiness(
        plan,
        anchor_packet=anchor_packet,
        packets=packets,
        bounded_search_exhausted=bounded_search_exhausted,
    )
    comparison_ready = len(ready_packets) >= peer_goal
    if plan.planning_blockers:
        readiness = readiness.model_copy(
            update={
                "whole_request_ready": False,
                "gaps": list(
                    dict.fromkeys([*readiness.gaps, *plan.planning_blockers])
                ),
            }
        )
    blockers = _multi_target_blockers(
        plan,
        discovery,
        packets,
        anchor_packet=anchor_packet,
    )
    blockers = list(dict.fromkeys([*readiness.gaps, *blockers]))
    if readiness.whole_request_ready:
        blockers = []
        pass_types.append("final_synthesis")
    request_coverage = _multi_target_request_coverage(
        plan,
        readiness=readiness,
    )
    return MultiTargetResearchResult(
        plan=plan,
        candidate_targets=discovery,
        selected_targets=[candidate.name for candidate in selected[:peer_goal]],
        anchor_packet=anchor_packet,
        packets=packets[:peer_goal],
        comparison_ready=comparison_ready,
        readiness=readiness,
        request_coverage=request_coverage,
        blockers=blockers,
        diagnostics={
            "candidate_count": len(discovery),
            "packet_count": len(packets),
            "ready_packet_count": len(ready_packets),
            "processed_peer_count": processed_peer_count,
            "parallel_target_depth_limit": _target_depth_concurrency(plan),
            "anchor_source": plan.anchor_source,
            "anchor_ready": readiness.anchor_ready,
            "anchor_comparison_signature": _anchor_comparison_signature(
                plan,
                anchor_packet,
            ),
            "whole_request_ready": readiness.whole_request_ready,
            "bounded_search_exhausted": readiness.bounded_search_exhausted,
            "bounded_search_receipt": bounded_search_receipt.receipt(),
            "target_selection": _target_selection_diagnostics(
                discovery=discovery,
                selected=selected[:peer_goal],
                packets=packets[:peer_goal],
            ),
            **discovery_diagnostics,
        },
        pass_types=list(dict.fromkeys(pass_types)),
    )


def _depth_candidate_pool(
    discovery: Sequence[CandidateTarget],
    *,
    peer_goal: int,
) -> list[CandidateTarget]:
    """Reserve bounded repair depth for strong first-party rubric evidence."""

    pool_limit = max(peer_goal, min(len(discovery), peer_goal + 2))
    primary = list(discovery[:peer_goal])
    remaining = list(discovery[peer_goal:])
    official_rubric_matches = [
        candidate
        for candidate in remaining
        if candidate.url_candidates
        and "candidate evidence does not mention requested dimensions"
        not in candidate.gaps
    ]
    pool = [*primary, *official_rubric_matches]
    pool.extend(candidate for candidate in remaining if candidate not in pool)
    return pool[:pool_limit]


def discover_candidate_targets(
    plan: MultiTargetResearchPlan,
    *,
    live_search: bool,
    quality_budget: AgentQualityBudget | None = None,
    agents_web_search_max_calls: int | None = None,
    retrieval_hint: RetrievalHint | None = None,
    search_provider_builder: Callable[..., Any] | None = None,
    execution_context: ProviderExecutionContext | None = None,
) -> list[CandidateTarget]:
    """Run broad discovery queries and rank normalized candidate targets."""

    candidates, _diagnostics = _discover_candidate_targets_with_diagnostics(
        plan,
        live_search=live_search,
        quality_budget=quality_budget,
        agents_web_search_max_calls=agents_web_search_max_calls,
        retrieval_hint=retrieval_hint,
        search_provider_builder=search_provider_builder,
        execution_context=execution_context,
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
    anchor_packet: PerTargetResearchPacket | None = None,
    execution_context: ProviderExecutionContext | None = None,
) -> tuple[list[CandidateTarget], dict[str, Any]]:
    """Run broad discovery queries, one bounded breadth repair, and rank targets."""

    if not live_search:
        return [], {
            "candidate_discovery_query_count": 0,
            "candidate_breadth_repair_passes": 0,
            "candidate_count_after_initial": 0,
        }
    queries = _candidate_discovery_queries(plan, anchor_packet=anchor_packet)
    max_results = max(
        4,
        min(8, getattr(quality_budget, "retrieval_max_results", 6) or 6),
    )
    search_results: list[Any] = []
    result_query_by_url: dict[str, str] = {}
    search_provider_builder = search_provider_builder or build_search_provider
    client = _multi_target_search_client(
        plan,
        agents_web_search_max_calls=agents_web_search_max_calls,
        retrieval_hint=retrieval_hint,
        search_provider_builder=search_provider_builder,
        execution_context=execution_context,
    )
    search_results = _search_candidate_query_batch(
        client,
        queries=queries,
        max_results=max_results,
        search_results=search_results,
        result_query_by_url=result_query_by_url,
        execution_context=execution_context,
    )

    ledger = candidate_targets_from_search_results(
        search_results,
        plan=plan,
        result_query_by_url=result_query_by_url,
    )
    ranked = rank_candidate_targets(ledger, plan=plan)
    initial_count = len(ranked)
    repair_passes = 0
    if initial_count < plan.peer_goal and plan.pass_budget > 1:
        repair_passes = 1
        repair_queries = _candidate_discovery_repair_queries(
            plan,
            anchor_packet=anchor_packet,
        )
        search_results = _search_candidate_query_batch(
            client,
            queries=repair_queries,
            max_results=max_results,
            search_results=search_results,
            result_query_by_url=result_query_by_url,
            execution_context=execution_context,
        )
        ledger = candidate_targets_from_search_results(
            search_results,
            plan=plan,
            result_query_by_url=result_query_by_url,
        )
        ranked = rank_candidate_targets(ledger, plan=plan)
    total_query_count = len(queries)
    if repair_passes:
        total_query_count += len(repair_queries)
    telemetry = client.telemetry() if callable(getattr(client, "telemetry", None)) else {}
    return ranked, {
        "candidate_discovery_query_count": total_query_count,
        "candidate_breadth_repair_passes": repair_passes,
        "candidate_count_after_initial": initial_count,
        "candidate_count_after_repair": len(ranked),
        "provider_telemetry": telemetry,
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
                alias_key = _official_ai_alias_key(
                    ledger,
                    key=key,
                    url=url,
                )
                if alias_key:
                    existing = ledger[alias_key]
                    if _looks_like_official_target_url(url, key):
                        ledger.pop(alias_key)
                        existing.name = name
                        existing.normalized_key = key
                        ledger[key] = existing
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
    execution_context: ProviderExecutionContext | None = None,
) -> list[Any]:
    if not queries:
        return search_results
    merged = search_results
    def run_query(query: str) -> list[Any]:
        return client.search_web(query, max_results)

    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as executor:
        futures = {
            executor.submit(run_query, query): query for query in queries
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

    title = f"Research comparison: {result.plan.topic}"
    whole_request_ready = multi_target_whole_request_ready(result)
    if whole_request_ready:
        names = ", ".join(packet.target_name for packet in result.packets)
        anchor_prefix = (
            f"Characterized {result.plan.anchor_target} and "
            if result.plan.anchor_target
            else ""
        )
        answer = (
            f"{anchor_prefix}found {len(result.packets)} source-supported targets "
            f"for comparison: {names}."
        )
    else:
        candidate_names = ", ".join(packet.target_name for packet in result.packets)
        answer = (
            f"The strongest current candidates are {candidate_names}. "
            "Treat them as provisional or adjacent matches until the remaining "
            "target-specific evidence gaps are resolved."
            if candidate_names
            else (
                "No candidate has enough reader-usable evidence yet to support even "
                "a provisional comparison."
            )
        )
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
        "| Target | Evidence | Source URLs | Gaps |",
        "|---|---|---|---|",
    ]
    table_packets = [
        *([result.anchor_packet] if result.anchor_packet is not None else []),
        *result.packets,
    ]
    for packet in table_packets:
        urls = _markdown_table_cell(", ".join(_packet_source_urls(packet)[:3]) or "none")
        evidence = _markdown_table_cell(_packet_evidence_summary(packet))
        gaps = _markdown_table_cell("; ".join(packet.gaps[:3]) or "none")
        label = (
            f"{packet.target_name} (anchor)"
            if result.anchor_packet is packet
            else packet.target_name
        )
        lines.append(f"| {label} | {evidence} | {urls} | {gaps} |")
    if result.blockers:
        lines.extend(["", "Blockers", *[f"* {blocker}" for blocker in result.blockers[:5]]])
    lines.extend(
        [
            "",
            "What looks real vs marketing language",
            (
                "* Candidate-discovery snippets and listicles can name leads, but only "
                "reader-usable target-specific sources count as comparison evidence."
            ),
            "",
            "Keystone product/design implications",
            (
                "* Shortlist candidates only when retained sources substantiate the requested "
                "criteria: "
                + ", ".join(_evidence_dimensions(result.plan)[:4])
                + "."
            ),
            (
                "* Separate verified product or research overlap from positioning "
                "and marketing language."
            ),
            (
                "* Resolve visible evidence gaps before assigning KNI advisory fit "
                "or initiating outreach."
            ),
        ]
    )
    return "\n".join(lines).strip()


def _packet_evidence_summary(packet: PerTargetResearchPacket) -> str:
    claims: list[str] = []
    for source in packet.source_refs:
        claims.extend(
            str(claim or "").strip()
            for claim in source.get("supported_claims") or []
            if str(claim or "").strip()
        )
        excerpt = str(source.get("evidence_excerpt") or "").strip()
        if excerpt:
            claims.append(excerpt)
    if claims:
        compact = " ".join(claims[0].split())
        return compact[:180]
    return "sufficient" if packet.source_sufficient else packet.extraction_status


def _markdown_table_cell(value: str) -> str:
    return " ".join(str(value or "").split()).replace("|", r"\|")


def _multi_target_detailed_summary(result: MultiTargetResearchResult) -> str:
    ready_packets = [packet for packet in result.packets if packet.source_sufficient]
    weak_packets = [packet for packet in result.packets if not packet.source_sufficient]
    if not result.packets:
        return (
            "The branch could not build per-target evidence packets. It needs named "
            "candidate targets plus public product, help, policy, or safety pages before "
            "a comparison would be evidence-backed."
        )
    if multi_target_whole_request_ready(result):
        target_names = ", ".join(packet.target_name for packet in ready_packets[:4])
        dimension_text = ", ".join(_evidence_dimensions(result.plan)[:3])
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
        for packet in [
            *([result.anchor_packet] if result.anchor_packet is not None else []),
            *result.packets[: result.plan.peer_goal],
        ]
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
    allow_substitution: bool = True,
    pre_researched_anchor: PerTargetResearchPacket | None = None,
    execution_context: ProviderExecutionContext | None = None,
) -> tuple[
    list[CandidateTarget],
    PerTargetResearchPacket | None,
    list[PerTargetResearchPacket],
    int,
]:
    selected: list[CandidateTarget] = []
    packets: list[PerTargetResearchPacket] = []
    candidate_iter = iter(selected_candidates)
    while len(selected) < plan.peer_goal:
        try:
            selected.append(next(candidate_iter))
        except StopIteration:
            break
    processed_peer_count = len(selected)
    anchor_candidate = (
        CandidateTarget(
            name=plan.anchor_target,
            normalized_key=_candidate_key(plan.anchor_target),
        )
        if plan.anchor_target and pre_researched_anchor is None
        else None
    )
    initial_candidates = [
        *([anchor_candidate] if anchor_candidate is not None else []),
        *selected,
    ]
    initial_packets = _research_selected_targets(
        plan,
        initial_candidates,
        live_search=live_search,
        quality_budget=quality_budget,
        agents_web_search_max_calls=agents_web_search_max_calls,
        retrieval_hint=retrieval_hint,
        retrieve_profile=retrieve_profile,
        execution_context=execution_context,
    )
    anchor_packet = (
        initial_packets[0]
        if anchor_candidate is not None
        else pre_researched_anchor
    )
    packets = initial_packets[1:] if anchor_candidate is not None else initial_packets
    weak_indexes = [idx for idx, packet in enumerate(packets) if not packet.source_sufficient]
    if not weak_indexes or not allow_substitution:
        return selected, anchor_packet, packets, processed_peer_count
    remaining = [candidate for candidate in selected_candidates if candidate not in selected]
    if not remaining:
        return selected, anchor_packet, packets, processed_peer_count
    for weak_index in weak_indexes:
        if not remaining:
            break
        substitute = remaining.pop(0)
        processed_peer_count += 1
        substitute_packet = _research_selected_targets(
            plan,
            [substitute],
            live_search=live_search,
            quality_budget=quality_budget,
            agents_web_search_max_calls=agents_web_search_max_calls,
            retrieval_hint=retrieval_hint,
            retrieve_profile=retrieve_profile,
            execution_context=execution_context,
        )[0]
        if substitute_packet.source_sufficient:
            selected[weak_index] = substitute
            packets[weak_index] = substitute_packet
    return selected, anchor_packet, packets, processed_peer_count


def _research_selected_targets(
    plan: MultiTargetResearchPlan,
    candidates: list[CandidateTarget],
    *,
    live_search: bool,
    quality_budget: AgentQualityBudget | None,
    agents_web_search_max_calls: int | None,
    retrieval_hint: RetrievalHint | None,
    retrieve_profile: Callable[..., tuple[CompanyProfile, dict[str, Any]]] | None,
    execution_context: ProviderExecutionContext | None = None,
    repair_receipt: ResearchEvidenceGapReceipt | None = None,
) -> list[PerTargetResearchPacket]:
    if not candidates:
        return []
    retrieve_profile = retrieve_profile or _default_retrieve_profile
    max_results = max(
        4,
        min(8, getattr(quality_budget, "retrieval_max_results", 6) or 6),
    )

    def run_one(candidate: CandidateTarget) -> PerTargetResearchPacket:
        if not live_search:
            return PerTargetResearchPacket(
                target_name=candidate.name,
                canonical_url=(candidate.url_candidates[0] if candidate.url_candidates else ""),
                source_sufficient=False,
                gaps=["live search disabled for multi-target depth retrieval"],
            )
        is_anchor = bool(
            plan.anchor_target
            and candidate.normalized_key == _candidate_key(plan.anchor_target)
        )
        repair_anchor = bool(
            is_anchor
            and repair_receipt is not None
            and _candidate_key(repair_receipt.target_name)
            == candidate.normalized_key
        )
        if repair_anchor:
            def query_builder(
                company: str,
                company_url: str | None = None,
            ) -> list[str]:
                assert repair_receipt is not None
                return _anchor_repair_queries(
                    plan,
                    company,
                    company_url,
                    repair_receipt,
                )
        elif is_anchor:
            def query_builder(
                company: str,
                company_url: str | None = None,
            ) -> list[str]:
                return _anchor_depth_queries(plan, company, company_url)
        else:
            def query_builder(
                company: str,
                company_url: str | None = None,
            ) -> list[str]:
                return _target_depth_queries(plan, company, company_url)
        retrieve_kwargs: dict[str, Any] = {
            "company": candidate.name,
            "company_url": (
                candidate.url_candidates[0] if candidate.url_candidates else None
            ),
            "request_text": (
                _anchor_repair_request_text(plan, repair_receipt)
                if repair_anchor and repair_receipt is not None
                else _anchor_depth_request_text(plan)
                if is_anchor
                else _target_depth_request_text(plan, candidate.name)
            ),
            "max_results": max_results,
            "max_queries": 4 if repair_anchor else 3 if is_anchor else 4,
            "query_builder": query_builder,
            "agents_web_search_max_calls": agents_web_search_max_calls,
            "agents_web_search_parallel": False,
            "retrieval_hint": retrieval_hint,
        }
        if (
            execution_context is not None
            and _callable_accepts_keyword(retrieve_profile, "execution_context")
        ):
            retrieve_kwargs["execution_context"] = execution_context
        profile, metadata = retrieve_profile(
            **retrieve_kwargs,
        )
        return _packet_from_profile(plan, candidate, profile, metadata)

    ordered: list[PerTargetResearchPacket | None] = [None] * len(candidates)
    with ThreadPoolExecutor(
        max_workers=_target_depth_concurrency(plan)
    ) as executor:
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
    candidate_source_refs = _prioritized_packet_source_refs(
        [_source_record_payload(source) for source in profile.sources[:8]],
        plan=plan,
        candidate=candidate,
    )
    triage = triage_source_candidates(
        request_text=_target_depth_request_text(plan, candidate.name),
        candidates=candidate_source_refs,
        agent_name="business_research_analyst",
        max_retain=5,
    )
    retained_source_ids = set(triage.retained_source_ids)
    source_refs = [
        source
        for source in candidate_source_refs
        if str(source.get("source_id") or "") in retained_source_ids
    ]
    gaps: list[str] = []
    if not candidate_source_refs:
        gaps.append("missing public source evidence")
    elif not source_refs:
        gaps.append("no reader-usable retained source evidence")
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
    dimension_source_ids = _dimension_source_ids(
        official_source_refs,
        plan=plan,
    )
    if not any(dimension_source_ids.values()):
        gaps.append("missing target-specific requested feature evidence")
    extracted_count = sum(
        1
        for source in source_refs
        if str(source.get("evidence_excerpt") or "").strip()
        or source.get("supported_claims")
    )
    if extracted_count == 0:
        gaps.append("snippet-only or unextracted source packet")
    if triage.needs_broaden_or_deepen and not any(dimension_source_ids.values()):
        gaps.append("source triage requires broader or deeper evidence")
    if (
        _plan_requires_anchor_relationship(plan)
        and candidate.normalized_key != _candidate_key(plan.anchor_target)
        and not _packet_establishes_anchor_relationship(
            plan,
            candidate=candidate,
            source_refs=source_refs,
        )
    ):
        gaps.append("anchor relationship remains to be qualified")
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
        dimension_source_ids=dimension_source_ids,
        gaps=list(dict.fromkeys(gaps)),
        retrieval_diagnostics=metadata.get("retrieval_diagnostics") or {},
    )


def _research_evidence_gap_receipt(
    plan: MultiTargetResearchPlan,
    packet: PerTargetResearchPacket,
    *,
    anchor: bool,
) -> ResearchEvidenceGapReceipt:
    diagnostics = (
        packet.retrieval_diagnostics
        if isinstance(packet.retrieval_diagnostics, dict)
        else {}
    )
    return compile_research_evidence_gap_receipt(
        target_name=packet.target_name,
        anchor=anchor,
        gaps=packet.gaps,
        missing_dimensions=_packet_missing_dimensions(packet, plan=plan),
        retained_source_count=len(packet.retained_source_ids),
        result_bearing_query_count=int(diagnostics.get("query_result_count") or 0),
    )


def _merge_research_packets(
    initial: PerTargetResearchPacket,
    repaired: PerTargetResearchPacket,
) -> PerTargetResearchPacket:
    """Merge one bounded repair packet without weakening its readiness result."""

    source_refs: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for source in [*initial.source_refs, *repaired.source_refs]:
        key = str(source.get("source_id") or source.get("url") or "").strip()
        if key and key in seen_sources:
            continue
        if key:
            seen_sources.add(key)
        source_refs.append(source)
    dimension_source_ids = {
        dimension: list(dict.fromkeys(source_ids))
        for dimension in {
            *initial.dimension_source_ids,
            *repaired.dimension_source_ids,
        }
        if (
            source_ids := [
                *initial.dimension_source_ids.get(dimension, []),
                *repaired.dimension_source_ids.get(dimension, []),
            ]
        )
    }
    return repaired.model_copy(
        update={
            "canonical_url": repaired.canonical_url or initial.canonical_url,
            "source_refs": source_refs,
            "retained_source_ids": list(
                dict.fromkeys(
                    [
                        *initial.retained_source_ids,
                        *repaired.retained_source_ids,
                    ]
                )
            ),
            "deepened_source_ids": list(
                dict.fromkeys(
                    [
                        *initial.deepened_source_ids,
                        *repaired.deepened_source_ids,
                    ]
                )
            ),
            "rejected_source_ids": list(
                dict.fromkeys(
                    [
                        *initial.rejected_source_ids,
                        *repaired.rejected_source_ids,
                    ]
                )
            ),
            "dimension_source_ids": dimension_source_ids,
            "retrieval_diagnostics": {
                **initial.retrieval_diagnostics,
                **repaired.retrieval_diagnostics,
                "anchor_repair_attempted": True,
            },
        }
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
    execution_context: ProviderExecutionContext | None = None,
) -> HybridSearchProvider:
    from keystone_agents.live_retrieval import build_shared_search_provider_config

    if execution_context is not None:
        search_config = execution_context.provider_config
    else:
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
        provider_call_scope=(
            execution_context.network_slot
            if execution_context is not None
            else None
        ),
        provider_call_runner=(
            execution_context.run_provider_call
            if execution_context is not None
            else None
        ),
    )


def _callable_accepts_keyword(function: Callable[..., Any], keyword: str) -> bool:
    """Return whether a callback accepts one optional request-context keyword."""

    try:
        parameters = signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


def _candidate_discovery_quality(results: Sequence[Any], plan: MultiTargetResearchPlan) -> Any:
    from keystone_agents.retrieval_policy import RetrievalQualityAssessment

    candidates = rank_candidate_targets(
        candidate_targets_from_search_results(results, plan=plan),
        plan=plan,
    )
    reasons = []
    needs_precision = len(candidates) < plan.peer_goal
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


def _anchor_comparison_signature(
    plan: MultiTargetResearchPlan,
    anchor_packet: PerTargetResearchPacket | None,
) -> list[str]:
    """Return the typed dimensions the anchor packet actually supports."""

    dimensions = _evidence_dimensions(plan)
    if anchor_packet is None:
        return dimensions[:4]
    supported = [
        dimension
        for dimension in dimensions
        if anchor_packet.dimension_source_ids.get(dimension)
    ]
    return (supported or dimensions)[:4]


def _candidate_discovery_queries(
    plan: MultiTargetResearchPlan,
    *,
    anchor_packet: PerTargetResearchPacket | None = None,
) -> list[str]:
    dims = _candidate_discovery_terms(plan, anchor_packet=anchor_packet)
    topic = plan.topic
    anchor = plan.anchor_target
    queries = (
        [
            f'"{anchor}" competitors alternatives',
            f'companies similar to "{anchor}" {dims}',
            f"{dims} companies products official",
            f'"{anchor}" market competitors current public sources {dims}',
        ]
        if anchor
        else [
            f'"{topic}" {dims} organizations products official',
            f'"{topic}" {dims} companies independent coverage',
            f"{dims} organizations products current public sources",
            f'"{topic}" {dims} market participants',
        ]
    )
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _candidate_discovery_repair_queries(
    plan: MultiTargetResearchPlan,
    *,
    anchor_packet: PerTargetResearchPacket | None = None,
) -> list[str]:
    dims = _candidate_discovery_terms(plan, anchor_packet=anchor_packet)
    topic = plan.topic
    anchor = plan.anchor_target
    queries = (
        [
            f'"{anchor}" alternatives named companies',
            f'"{anchor}" competitor landscape official products {dims}',
            f'"{anchor}" versus companies {dims}',
            f"{dims} vendors independent coverage",
        ]
        if anchor
        else [
            f'"{topic}" {dims} named organizations',
            f"{dims} companies official products",
            f'"{topic}" independent coverage {dims}',
            f"{topic} platforms current public sources {dims}",
        ]
    )
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _candidate_discovery_terms(
    plan: MultiTargetResearchPlan,
    *,
    anchor_packet: PerTargetResearchPacket | None = None,
) -> str:
    """Return semantic rubric terms without source-format constraints."""

    dimensions = [
        dimension
        for dimension in _anchor_comparison_signature(plan, anchor_packet)
        if not _SOURCE_EVIDENCE_DIMENSION_RE.search(
            " ".join(str(dimension or "").lower().replace("-", " ").split())
        )
    ]
    return " ".join(dimensions)


def _target_depth_queries(
    plan: MultiTargetResearchPlan,
    company: str,
    company_url: str | None = None,
) -> list[str]:
    dims = " ".join(_evidence_dimensions(plan)[:4])
    queries = [
        f"{company} official product service {dims}",
        f"{company} official about technology {dims}",
        f"{company} evidence validation outcomes {dims}",
        f"{company} independent coverage customers partnerships {dims}",
        f"{company} {dims} current public source",
    ]
    if (
        plan.anchor_target
        and _candidate_key(company) != _candidate_key(plan.anchor_target)
    ):
        queries.extend(
            [
                f'"{company}" "{plan.anchor_target}" competitor alternative comparison',
                f'"{company}" "{plan.anchor_target}" product overlap {dims}',
            ]
        )
    seeded_queries = [*queries, *build_company_research_queries(company, company_url)[:4]]
    return list(dict.fromkeys(seeded_queries))


def _anchor_depth_queries(
    plan: MultiTargetResearchPlan,
    company: str,
    company_url: str | None = None,
) -> list[str]:
    """Build bounded anchor-characterization queries from the typed rubric."""

    dims = " ".join(_evidence_dimensions(plan)[:4])
    queries = [
        f"{company} official product technology {dims}",
        f"{company} official about customers clinical use {dims}",
        f"{company} independent validation coverage {dims}",
        *build_company_research_queries(company, company_url)[:3],
    ]
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _anchor_repair_queries(
    plan: MultiTargetResearchPlan,
    company: str,
    company_url: str | None,
    receipt: ResearchEvidenceGapReceipt,
) -> list[str]:
    """Build one bounded query round from the assessed anchor evidence gaps."""

    terms = " ".join(receipt.recommended_query_terms[:4]).strip()
    queries = [
        f"{company} {terms}",
        f"{company} official product target users {terms}",
        f"{company} technology documentation validation {terms}",
    ]
    if company_url:
        domain = _domain(company_url)
        if domain:
            queries.insert(0, f"site:{domain} {company} {terms}")
    queries.extend(_anchor_depth_queries(plan, company, company_url))
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _anchor_depth_request_text(plan: MultiTargetResearchPlan) -> str:
    dimensions = _evidence_dimensions(plan)
    return (
        f"Deeply characterize the anchor company {plan.anchor_target} before "
        "evaluating related companies. Establish its product, target users, "
        f"operating context, and source-backed comparison criteria: "
        f"{', '.join(dimensions)}."
    )


def _anchor_repair_request_text(
    plan: MultiTargetResearchPlan,
    receipt: ResearchEvidenceGapReceipt,
) -> str:
    gaps = ", ".join(receipt.recommended_query_terms) or "missing anchor evidence"
    return (
        f"Repair the source evidence for anchor company {plan.anchor_target}. "
        f"Retrieve reader-usable primary evidence for: {gaps}. Preserve the "
        "original comparison scope and do not discover peers in this pass."
    )


def _target_depth_request_text(plan: MultiTargetResearchPlan, target_name: str) -> str:
    dimensions = _evidence_dimensions(plan)
    relationship = (
        f" Classify its source-backed relationship to {plan.anchor_target} as "
        "direct, adjacent, or unsupported."
        if plan.anchor_target
        and _candidate_key(target_name) != _candidate_key(plan.anchor_target)
        else ""
    )
    return (
        f"Detailed source-backed comparison for {target_name}: "
        f"{', '.join(dimensions)}.{relationship}"
    )


def _candidate_names_from_result(
    mapping: dict[str, str],
    *,
    plan: MultiTargetResearchPlan,
) -> list[str]:
    names: list[str] = []
    url_name = _candidate_name_from_domain(mapping["url"])
    source_site_domain = bool(
        url_name and _candidate_is_source_site_name(url_name, mapping)
    )
    if url_name and not source_site_domain:
        names.append(
            _title_spelling_for_candidate(mapping["title"], candidate=url_name)
            or url_name
        )
    elif source_site_domain:
        names.extend(_candidate_title_subject_names(mapping["title"]))
    else:
        title = mapping["title"]
        names.extend(_capitalized_product_names(title))
        snippet_names = _capitalized_product_names(mapping["snippet"])
        names.extend(snippet_names[:8])
    # Relationship-list extraction is a bounded compatibility signal for
    # third-party result pages. It may add provenance-bearing candidates, but
    # it is never the semantic admission authority or a hard rejection gate.
    names.extend(_relationship_candidate_names(mapping, plan=plan))
    cleaned: list[str] = []
    topic_words = set(re.findall(r"\b[a-z0-9]{3,}\b", plan.topic.lower()))
    topic_key = _candidate_key(plan.topic)
    for name in names:
        normalized = " ".join(name.split()).strip(" .,:;-")
        normalized = _canonical_candidate_name(normalized, plan=plan)
        if not normalized or normalized in _ENTITY_STOPWORDS:
            continue
        key = _candidate_key(normalized)
        if (
            not key
            or key == topic_key
            or key in topic_words
            or key in _NON_PRODUCT_CANDIDATE_KEYS
            or _looks_like_category_target(normalized)
            or _looks_like_search_heading(normalized)
            or _looks_like_topic_metadata(normalized, plan=plan)
            or _candidate_is_source_site_name(normalized, mapping)
        ):
            continue
        cleaned.append(normalized)
    return list(dict.fromkeys(cleaned))[:8]


def _canonical_candidate_name(name: str, *, plan: MultiTargetResearchPlan) -> str:
    normalized = " ".join(str(name or "").split()).strip(" .,:;-")
    normalized = re.sub(r"^Inc\.?\s+", "", normalized, flags=re.I)
    normalized = re.sub(r"(?:\.\s*)?(?:Preview|View)$", "", normalized, flags=re.I)
    normalized = re.sub(r"['’]s$", "", normalized, flags=re.I)
    if _candidate_key(normalized) == "characterai":
        return "Character.AI"
    if _candidate_key(normalized) == "openai":
        return "OpenAI"
    if _candidate_key(normalized) == "chatgpt":
        return "ChatGPT"
    normalized = normalized.strip(" .,:;-")
    return normalized


def _capitalized_product_names(text: str) -> list[str]:
    matches = re.findall(
        r"\b([A-Z][A-Za-z0-9.&'-]*(?:\s+[A-Z][A-Za-z0-9.&'-]*){0,2})\b",
        str(text or ""),
    )
    return [match.strip() for match in matches]


def _candidate_title_subject_names(title: str) -> list[str]:
    """Extract the named subject from a directory or publisher result title."""

    normalized = " ".join(str(title or "").split()).strip()
    match = re.match(
        r"^(?:(?i:about|best|introducing|meet|top)\s+)?"
        r"(?P<name>[A-Z][A-Za-z0-9.&'-]*(?:\s+[A-Z][A-Za-z0-9.&'-]*){0,2})"
        r"(?=\s+(?i:alternatives?|competitors?|overview|profile|vs\.?|"
        r"uses?|builds?|develops?|offers?|provides?)\b|\s*(?:\||-|–|—|:|$))",
        normalized,
    )
    return [match.group("name").strip()] if match else []


def _title_spelling_for_candidate(title: str, *, candidate: str) -> str:
    """Preserve a domain-identified company's published title capitalization."""

    candidate_key = _candidate_key(candidate)
    normalized = " ".join(str(title or "").split()).strip()
    for name in _capitalized_product_names(normalized):
        title_key = _candidate_key(name)
        if title_key == candidate_key:
            if " " in candidate.strip() and " " not in name.strip():
                continue
            return name
        if title_key == f"{candidate_key}ai":
            return re.sub(r"(?:[.\s]+AI)$", "", name, flags=re.I)
    return ""


def _official_ai_alias_key(
    ledger: dict[str, CandidateTarget],
    *,
    key: str,
    url: str,
) -> str:
    """Merge a narrow brand/brand-AI alias only when one side is official."""

    alias_key = key[:-2] if key.endswith("ai") and len(key) > 2 else f"{key}ai"
    existing = ledger.get(alias_key)
    if existing is None:
        return ""
    current_is_official = _looks_like_official_target_url(url, key)
    existing_is_official = bool(existing.url_candidates)
    return alias_key if current_is_official or existing_is_official else ""


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
        "openai": "OpenAI",
        "chatgpt": "ChatGPT",
        "character": "Character.AI",
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
    evidence_dimensions = _evidence_dimensions(plan)
    dimension_hits = sum(
        1
        for dimension in evidence_dimensions
        if all(token in haystack for token in re.findall(r"\b[a-z0-9]{3,}\b", dimension.lower()))
    )
    official = bool(candidate.url_candidates)
    relationship_supported = _candidate_has_relationship_evidence(candidate, plan=plan)
    title_subject_supported = any(
        _candidate_is_title_subject(candidate.name, item.title)
        for item in candidate.evidence
    )
    topic_tokens = {
        token
        for token in re.findall(r"\b[a-z0-9]{3,}\b", plan.topic.lower())
        if token
        not in {
            "and",
            "companies",
            "company",
            "compare",
            "comparison",
            "find",
            "for",
            "products",
            "product",
            "public",
            "the",
            "three",
            "two",
        }
    }
    topic_hit_count = sum(1 for token in topic_tokens if token in haystack)
    topic_supported = bool(topic_tokens) and topic_hit_count >= min(2, len(topic_tokens))
    typed_relevance_supported = bool(dimension_hits or topic_supported)
    anchor_key = _candidate_key(plan.anchor_target)
    anchor_supported = bool(
        not anchor_key
        or anchor_key in _candidate_key(haystack)
        or relationship_supported
    )
    provisional_official_match = bool(official and typed_relevance_supported)
    if (
        not relationship_supported
        and not provisional_official_match
        and (not typed_relevance_supported or not anchor_supported)
    ):
        return 0, [
            "candidate evidence does not establish an anchor relationship "
            "or match the requested topic/dimensions"
        ]
    if (
        not official
        and len(urls) == 1
        and not relationship_supported
        and not title_subject_supported
    ):
        return 0, [
            "single third-party mention does not establish the candidate as "
            "the result subject or an anchor-linked alternative"
        ]
    score = 20
    score += min(25, len(candidate.evidence) * 5)
    score += min(15, len(providers) * 5)
    score += min(15, len(queries) * 5)
    score += 25 if official else 0
    score += min(20, dimension_hits * 8)
    if any(_result_text_is_recent(item.title + " " + item.snippet) for item in candidate.evidence):
        score += 10
    if relationship_supported:
        score += 60
        if _candidate_has_direct_comparison_page_evidence(candidate, plan=plan):
            score += 10
    if not official:
        score -= 15
    if not dimension_hits:
        score -= 10
    gaps: list[str] = []
    if not official:
        gaps.append("no official or product-domain URL found during discovery")
    if not dimension_hits:
        gaps.append("candidate evidence does not mention requested dimensions")
    if plan.anchor_target and not anchor_supported:
        gaps.append("anchor relationship remains to be qualified")
    if len(urls) == 1 and not official:
        gaps.append("single weak discovery source")
    return max(0, min(100, score)), gaps


def _candidate_has_relationship_evidence(
    candidate: CandidateTarget,
    *,
    plan: MultiTargetResearchPlan,
) -> bool:
    """Return a compatibility boost for source-linked anchor relationships."""

    candidate_key = candidate.normalized_key
    relationship_anchor = plan.anchor_target or plan.topic
    topic_key = _candidate_key(relationship_anchor)
    if not candidate_key:
        return False
    for item in candidate.evidence:
        if _looks_like_official_target_url(item.url, candidate_key):
            continue
        mapping = {
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
            "source": item.provider,
        }
        relationship_keys = {
            _candidate_key(name)
            for name in _relationship_candidate_names(mapping, plan=plan)
        }
        if candidate_key in relationship_keys or any(
            _official_ai_aliases_equivalent(
                candidate_key,
                relationship_key,
                official_urls=candidate.url_candidates,
            )
            for relationship_key in relationship_keys
        ):
            return True
        title_key = _candidate_key(item.title)
        if (
            not plan.anchor_target
            and candidate_key in title_key
            and re.search(r"\b(?:vs\.?|versus)\b", item.title, flags=re.I)
        ):
            return True
        if (
            topic_key
            and topic_key in title_key
            and candidate_key in title_key
            and re.search(r"\b(?:vs\.?|versus)\b", item.title, flags=re.I)
        ):
            return True
    return False


def _official_ai_aliases_equivalent(
    left: str,
    right: str,
    *,
    official_urls: Sequence[str],
) -> bool:
    """Compare brand/brand-AI keys only when the same official root proves both."""

    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if not shorter or longer != f"{shorter}ai":
        return False
    return any(
        _looks_like_official_target_url(url, shorter)
        and _looks_like_official_target_url(url, longer)
        for url in official_urls
    )


def _plan_requires_anchor_relationship(plan: MultiTargetResearchPlan) -> bool:
    if not plan.anchor_target:
        return False
    text = " ".join(
        [
            plan.request_text,
            *plan.requested_dimensions,
        ]
    )
    return bool(
        re.search(
            r"\b(?:adjacent|alternatives?|compare|comparison|competitors?|"
            r"overlap|peers?|similar)\b",
            text,
            flags=re.I,
        )
    )


def _packet_establishes_anchor_relationship(
    plan: MultiTargetResearchPlan,
    *,
    candidate: CandidateTarget,
    source_refs: Sequence[dict[str, Any]],
) -> bool:
    if _candidate_has_relationship_evidence(candidate, plan=plan):
        return True
    anchor_key = _candidate_key(plan.anchor_target)
    candidate_key = candidate.normalized_key
    relationship_re = re.compile(
        r"\b(?:adjacent|alternatives?|compare|comparison|competitors?|"
        r"overlap|peers?|similar|versus|vs\.?)\b",
        flags=re.I,
    )
    for source in source_refs:
        text = " ".join(
            [
                str(source.get("title") or ""),
                " ".join(str(item) for item in source.get("supported_claims") or []),
                str(source.get("evidence_excerpt") or ""),
            ]
        )
        normalized = _candidate_key(text)
        if (
            anchor_key
            and anchor_key in normalized
            and candidate_key in normalized
            and relationship_re.search(text)
        ):
            return True
    return False


def _candidate_is_title_subject(name: str, title: str) -> bool:
    """Return whether a result title presents ``name`` as its main subject."""

    normalized_name = " ".join(str(name or "").split()).strip(" .,:;-")
    normalized_title = " ".join(str(title or "").split()).strip()
    if not normalized_name or not normalized_title:
        return False
    return bool(
        re.match(
            rf"^(?:(?:about|best|introducing|meet|top)\s+)?"
            rf"{re.escape(normalized_name)}(?:['’]s)?\b",
            normalized_title,
            flags=re.I,
        )
    )


def _relationship_candidate_names(
    mapping: dict[str, str],
    *,
    plan: MultiTargetResearchPlan,
) -> list[str]:
    """Extract names from one explicit anchor-linked relationship list."""

    title = mapping.get("title", "")
    snippet = mapping.get("snippet", "")
    text = " ".join(part for part in (title, snippet) if part)
    relationship_anchor = plan.anchor_target or plan.topic
    topic_key = _candidate_key(relationship_anchor)
    if topic_key and topic_key not in _candidate_key(text):
        return []
    topic_pattern = r"\s+".join(
        re.escape(part) for part in relationship_anchor.split()
    )
    relationship_pattern = (
        r"(?:top\s+|leading\s+|closest\s+)?"
        r"(?:competitors?|alternatives?|challengers?|peers?|other companies)"
    )
    list_tail = (
        r"[^.!?]{0,100}?"
        r"(?:include|including|such as|:)\s*"
        r"(?P<names>[^.!?;]{1,260})"
    )
    patterns = [
        rf"\b{topic_pattern}(?:['’]s)?\s+{relationship_pattern}{list_tail}",
        rf"\b{relationship_pattern}\s+(?:to|for|of)\s+{topic_pattern}{list_tail}",
    ]
    if (
        topic_key
        and topic_key in _candidate_key(title)
        and re.search(relationship_pattern, title, flags=re.I)
    ):
        patterns.append(rf"\b{relationship_pattern}{list_tail}")
    names: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            names.extend(_capitalized_product_names(match.group("names")))
    return list(dict.fromkeys(names))[:10]


def _candidate_has_direct_comparison_page_evidence(
    candidate: CandidateTarget,
    *,
    plan: MultiTargetResearchPlan,
) -> bool:
    topic_key = _candidate_key(plan.topic)
    for item in candidate.evidence:
        if _looks_like_official_target_url(item.url, candidate.normalized_key):
            continue
        path = urlparse(item.url).path.lower()
        text = f"{item.title} {item.snippet}".lower()
        if topic_key and topic_key not in _candidate_key(text):
            continue
        if "alternatives-competitors" in path or re.search(
            r"\btop\s+.+\s+(?:alternatives|competitors)\b",
            item.title,
            flags=re.I,
        ):
            return True
    return False


def _multi_target_readiness(
    plan: MultiTargetResearchPlan,
    *,
    anchor_packet: PerTargetResearchPacket | None,
    packets: Sequence[PerTargetResearchPacket],
    bounded_search_exhausted: bool = False,
) -> MultiTargetReadiness:
    """Derive whole-request readiness from the anchor and peer evidence."""

    ready_peer_count = sum(1 for packet in packets if packet.source_sufficient)
    peer_ready = ready_peer_count >= plan.peer_goal
    rubric_ready_peer_count = sum(
        1 for packet in packets if _packet_covers_rubric(packet, plan=plan)
    )
    count_coverage = build_count_request_coverage(
        interpreted_request=plan.request_text or plan.topic,
        expected_count=plan.peer_goal,
        observed_count=rubric_ready_peer_count,
        item_label="comparison candidate(s)",
        next_safe_action=(
            "Deepen or broaden the bounded candidate set, then reassess the same "
            "comparison rubric."
        ),
        count_mode=(
            plan.desired_count_mode
            if plan.desired_count_mode != "unspecified"
            else "target"
        ),
        bounded_search_exhausted=bounded_search_exhausted,
    )
    count_contract_satisfied = bool(
        count_coverage.status == "complete" and rubric_ready_peer_count > 0
    )
    anchor_required = bool(plan.anchor_target)
    anchor_ready = bool(
        anchor_packet and _packet_covers_rubric(anchor_packet, plan=plan)
    )
    gaps: list[str] = []
    if rubric_ready_peer_count == 0:
        gaps.append("no comparison candidates met the requested evidence rubric")
    if anchor_required and not anchor_ready:
        if anchor_packet is None:
            anchor_gaps = "anchor research packet missing"
        elif anchor_packet.gaps:
            anchor_gaps = "; ".join(anchor_packet.gaps[:3])
        else:
            missing_dimensions = _packet_missing_dimensions(
                anchor_packet,
                plan=plan,
            )
            anchor_gaps = (
                "missing rubric evidence: " + ", ".join(missing_dimensions[:4])
                if missing_dimensions
                else "anchor packet did not satisfy the comparison rubric"
            )
        gaps.append(f"anchor gap for {plan.anchor_target}: {anchor_gaps}")
    if not count_contract_satisfied:
        gaps.extend(count_coverage.unmet_dimensions)
        for packet in packets:
            missing = _packet_missing_dimensions(packet, plan=plan)
            if missing:
                gaps.append(
                    f"{packet.target_name} missing rubric evidence: "
                    + ", ".join(missing[:4])
                )
    whole_request_ready = count_contract_satisfied and (
        not anchor_required or anchor_ready
    )
    return MultiTargetReadiness(
        assessed=True,
        rubric_dimensions=_evidence_dimensions(plan),
        anchor_required=anchor_required,
        anchor_ready=anchor_ready,
        peer_goal=plan.peer_goal,
        ready_peer_count=ready_peer_count,
        rubric_ready_peer_count=rubric_ready_peer_count,
        bounded_search_exhausted=bounded_search_exhausted,
        count_contract_satisfied=count_contract_satisfied,
        peer_comparison_ready=peer_ready,
        whole_request_ready=whole_request_ready,
        gaps=gaps,
    )


def _packet_missing_dimensions(
    packet: PerTargetResearchPacket,
    *,
    plan: MultiTargetResearchPlan,
) -> list[str]:
    missing = [
        dimension
        for dimension in _evidence_dimensions(plan)
        if not packet.dimension_source_ids.get(dimension)
    ]
    for alternatives in _alternative_dimension_groups(plan):
        if any(packet.dimension_source_ids.get(dimension) for dimension in alternatives):
            missing = [dimension for dimension in missing if dimension not in alternatives]
    return missing


def _packet_covers_rubric(
    packet: PerTargetResearchPacket,
    *,
    plan: MultiTargetResearchPlan,
) -> bool:
    return packet.source_sufficient and not _packet_missing_dimensions(
        packet,
        plan=plan,
    )


def _multi_target_request_coverage(
    plan: MultiTargetResearchPlan,
    *,
    readiness: MultiTargetReadiness,
) -> RequestCoverage:
    """Project deterministic readiness into the shared specialist coverage contract."""

    satisfied: list[str] = []
    if readiness.anchor_required and readiness.anchor_ready:
        satisfied.append(f"characterized anchor entity: {plan.anchor_target}")
    if readiness.peer_comparison_ready:
        satisfied.append(
            f"researched {readiness.ready_peer_count} comparison candidate(s)"
        )
    if readiness.rubric_dimensions and readiness.rubric_ready_peer_count > 0:
        satisfied.append(
            "evaluated comparison rubric: "
            + ", ".join(readiness.rubric_dimensions)
        )
    unmet = list(readiness.gaps)
    if readiness.whole_request_ready:
        status: Literal["complete", "partial", "blocked"] = "complete"
        next_safe_action = ""
        stop_condition_status = "satisfied"
    else:
        status = "partial" if satisfied else "blocked"
        next_safe_action = (
            "Deepen the missing anchor or candidate evidence, then reassess the "
            "same comparison rubric."
        )
        stop_condition_status = "blocked"
    return RequestCoverage(
        interpreted_request=plan.request_text or plan.topic,
        status=status,
        satisfied_dimensions=satisfied,
        unmet_dimensions=unmet,
        output_form_status="satisfied",
        stop_condition_status=stop_condition_status,
        broadened_beyond_request=False,
        next_safe_action=next_safe_action,
    )


def multi_target_whole_request_ready(result: MultiTargetResearchResult) -> bool:
    """Return whole-request truth while preserving legacy result compatibility."""

    if result.readiness.assessed:
        return result.readiness.whole_request_ready
    return result.comparison_ready


def _multi_target_blockers(
    plan: MultiTargetResearchPlan,
    candidates: list[CandidateTarget],
    packets: list[PerTargetResearchPacket],
    *,
    anchor_packet: PerTargetResearchPacket | None = None,
) -> list[str]:
    blockers: list[str] = []
    if plan.anchor_target and not (
        anchor_packet is not None and anchor_packet.source_sufficient
    ):
        anchor_gaps = (
            "; ".join(anchor_packet.gaps[:3])
            if anchor_packet is not None and anchor_packet.gaps
            else "anchor research packet missing"
        )
        blockers.append(f"anchor gap for {plan.anchor_target}: {anchor_gaps}")
    if len(candidates) < plan.peer_goal:
        blockers.append(
            f"breadth gap: found {len(candidates)} candidate target(s), need {plan.peer_goal}"
        )
    ready = [packet for packet in packets if packet.source_sufficient]
    if len(ready) < plan.peer_goal:
        blockers.append(
            "depth gap: "
            f"{len(ready)}/{plan.peer_goal} target packet(s) "
            "have sufficient source evidence"
        )
    for packet in packets:
        if not packet.source_sufficient:
            blockers.append(f"{packet.target_name}: " + "; ".join(packet.gaps[:3]))
    return list(dict.fromkeys(blockers))


def _source_record_payload(source: SourceRecord) -> dict[str, Any]:
    has_bounded_evidence = bool(
        source.evidence_excerpt.strip() or source.source_type in {"fixture", "user_provided"}
    )
    return {
        "source_id": source.source_id,
        "title": source.title,
        "url": source.url,
        "source_type": source.source_type,
        "supported_claims": list(source.supported_claims),
        "evidence_excerpt": source.evidence_excerpt,
        "extraction_status": "extracted" if has_bounded_evidence else "snippet_only",
        "confidence": source.confidence,
        "published_at": source.published_at,
    }


def _source_matches_requested_dimensions(
    source: dict[str, Any],
    plan: MultiTargetResearchPlan,
) -> bool:
    return any(
        _source_matches_dimension(source, dimension)
        for dimension in _evidence_dimensions(plan)
    )


def _source_matches_dimension(
    source: dict[str, Any],
    dimension: str,
) -> bool:
    haystack = " ".join(
        [
            str(source.get("title") or ""),
            str(source.get("url") or ""),
            " ".join(str(item) for item in source.get("supported_claims") or []),
            str(source.get("evidence_excerpt") or ""),
        ]
    ).lower().replace("-", " ")
    normalized_dimension = " ".join(
        str(dimension or "").lower().replace("-", " ").split()
    )
    evidence_present = bool(
        str(source.get("url") or "").strip()
        and (
            str(source.get("evidence_excerpt") or "").strip()
            or source.get("supported_claims")
        )
    )
    if _SOURCE_EVIDENCE_DIMENSION_RE.search(normalized_dimension):
        return evidence_present
    if normalized_dimension == "product or service overlap":
        return evidence_present and bool(
            re.search(
                r"\b(?:product|service|platform|software|tool|model|solution|system)\b",
                haystack,
            )
        )
    if normalized_dimension == "target market overlap":
        return evidence_present and bool(
            re.search(
                r"\b(?:patient|clinician|provider|clinic|hospital|customer|user|"
                r"health(?:care)?|care\s+team|organization|enterprise|market)\b",
                haystack,
            )
        )
    if re.search(
        r"\bmultimodal\b|\bmultiple\b.{0,24}\b(?:data|signal|input|modalit)",
        normalized_dimension,
    ):
        return "multimodal" in haystack or sum(
            1 for pattern in _MODALITY_PATTERNS if re.search(pattern, haystack)
        ) >= 2
    if re.fullmatch(
        r"(?:behavioral|behavioural|mental)\s+health|psychiatr(?:y|ic)",
        normalized_dimension,
    ):
        return bool(
            re.search(
                r"\b(?:behavioral|behavioural|mental)\s+health\b|\bpsychiatr\w*\b",
                haystack,
            )
        )
    if re.search(
        r"\b(?:behavioral|behavioural|mental|psychiatr\w*)\b.{0,24}"
        r"\b(?:measure|measurement|assess|assessment|monitor|monitoring|"
        r"symptom|biomarker|screen|screening)\w*\b",
        normalized_dimension,
    ):
        return bool(
            re.search(
                r"\b(?:behavioral|behavioural|mental)\s+health\b|\bpsychiatr\w*\b",
                haystack,
            )
            and re.search(
                r"\b(?:measure|assess|monitor|symptom|biomarker|screen|"
                r"outcome|severity|risk)\w*\b",
                haystack,
            )
        )
    if normalized_dimension == "clinical decision support":
        return bool(
            re.search(
                r"\b(?:clinical|clinician|care|diagnos|treatment|patient)\w*\b",
                haystack,
            )
            and re.search(
                r"\b(?:decision|support|assess|monitor|risk|recommend|diagnos|"
                r"treatment|symptom|triage)\w*\b",
                haystack,
            )
        )
    tokens = [
        token
        for token in re.findall(r"\b[a-z0-9]{3,}\b", normalized_dimension)
        if token not in {"and", "for", "from", "into", "the", "with"}
    ]
    if not tokens:
        return False
    if len(tokens) == 1:
        return tokens[0] in haystack
    return all(token in haystack for token in tokens)


def _alternative_dimension_groups(
    plan: MultiTargetResearchPlan,
) -> list[frozenset[str]]:
    """Preserve explicit ``A or B`` rubric alternatives from the current ask."""

    request_text = " ".join(
        str(plan.request_text or "").lower().replace("-", " ").split()
    )
    dimensions = _evidence_dimensions(plan)
    groups: list[frozenset[str]] = []
    for index, left in enumerate(dimensions):
        normalized_left = " ".join(left.lower().replace("-", " ").split())
        for right in dimensions[index + 1 :]:
            normalized_right = " ".join(right.lower().replace("-", " ").split())
            alternatives = (
                rf"\b{re.escape(normalized_left)}\s+or\s+"
                rf"{re.escape(normalized_right)}\b"
                rf"|\b{re.escape(normalized_right)}\s+or\s+"
                rf"{re.escape(normalized_left)}\b"
            )
            if re.search(alternatives, request_text):
                groups.append(frozenset({left, right}))
    return groups


def _dimension_source_ids(
    sources: Sequence[dict[str, Any]],
    *,
    plan: MultiTargetResearchPlan,
) -> dict[str, list[str]]:
    """Map each substantive rubric dimension to reader-usable source evidence."""

    coverage: dict[str, list[str]] = {}
    for dimension in _evidence_dimensions(plan):
        source_ids = [
            str(source.get("source_id") or "").strip()
            for source in sources
            if str(source.get("source_id") or "").strip()
            and _source_matches_dimension(source, dimension)
        ]
        coverage[dimension] = list(dict.fromkeys(source_ids))
    return coverage


def _evidence_dimensions(plan: MultiTargetResearchPlan) -> list[str]:
    """Return substantive comparison criteria, excluding target relationships."""

    dimensions = [
        dimension
        for dimension in plan.requested_dimensions
        if " ".join(str(dimension or "").casefold().split())
        not in _RELATIONSHIP_DIMENSIONS
    ]
    return dimensions or list(_DEFAULT_DIMENSIONS)


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
    if _looks_like_source_domain(domain) or _domain_is_hosted_content(domain):
        return False
    label = _brand_label_from_domain(domain)
    if label in _GENERIC_SOURCE_DOMAINS:
        return False
    normalized_label = re.sub(r"[^a-z0-9]+", "", label)
    return bool(
        key == normalized_label
        or (
            key.endswith("ai")
            and len(key) > 2
            and key[:-2] == normalized_label
        )
    )


def _looks_like_source_domain(domain: str) -> bool:
    clean = str(domain or "").lower().removeprefix("www.")
    if _domain_is_hosted_content(clean):
        return True
    if clean.endswith((".gov", ".edu")):
        return True
    label = _brand_label_from_domain(clean)
    normalized_label = re.sub(r"[^a-z0-9]+", "", label)
    return label in _GENERIC_SOURCE_DOMAINS or normalized_label in _NON_PRODUCT_CANDIDATE_KEYS


def _domain_is_hosted_content(domain: str) -> bool:
    clean = str(domain or "").lower().removeprefix("www.")
    return any(
        clean == root or clean.endswith(f".{root}")
        for root in _HOSTED_CONTENT_ROOTS
    )


def _brand_label_from_domain(domain: str) -> str:
    labels = [part for part in str(domain or "").split(".") if part]
    if not labels:
        return ""
    index = 0
    while index < len(labels) - 1 and labels[index] in _COMMON_SUBDOMAIN_LABELS:
        index += 1
    return labels[index]


def _candidate_is_source_site_name(name: str, mapping: dict[str, str]) -> bool:
    """Reject publisher/directory brands mistaken for the result's subject."""

    domain = _domain(mapping.get("url", ""))
    path = urlparse(str(mapping.get("url") or "")).path.lower()
    if not domain:
        return False
    site_key = _candidate_key(_brand_label_from_domain(domain))
    candidate_key = _candidate_key(name)
    if candidate_key in _COMMON_SUBDOMAIN_LABELS and any(
        marker in path for marker in _SOURCE_CONTAINER_PATH_MARKERS
    ):
        return True
    same_site_brand = candidate_key == site_key or (
        min(len(candidate_key), len(site_key)) >= 4
        and (candidate_key in site_key or site_key in candidate_key)
    )
    if not site_key or not same_site_brand:
        return False

    title_head = re.split(
        r"\s+(?:\||-|–|—)\s+",
        str(mapping.get("title") or ""),
        maxsplit=1,
    )[0]
    if candidate_key and candidate_key in _candidate_key(title_head):
        return False

    subject_pattern = re.compile(
        rf"\b{re.escape(str(name).strip())}\b\s+"
        r"(?:builds?|develops?|is|offers?|provides?|uses?)\b",
        flags=re.I,
    )
    snippet = str(mapping.get("snippet") or "")
    if subject_pattern.search(snippet):
        return False
    source_context = f"{mapping.get('title', '')} {snippet}".lower()
    if re.search(
        r"\b(?:directory|guide|landscape|listing|marketplace|market report|"
        r"profile page|vendor list)\b",
        source_context,
    ):
        return True
    ownership_names = re.findall(
        r"\b(?:by|from)\s+"
        r"([A-Z][A-Za-z0-9.&'-]*(?:\s+[A-Z][A-Za-z0-9.&'-]*){0,2})",
        f"{mapping.get('title', '')}. {snippet}",
    )
    ownership_prefix_keys = {
        _candidate_key(" ".join(owner.split()[:word_count]))
        for owner in ownership_names
        for word_count in range(1, len(owner.split()) + 1)
    }
    if candidate_key in ownership_prefix_keys:
        return False
    if any(marker in path for marker in _FIRST_PARTY_PRODUCT_PATH_MARKERS):
        return False
    return any(marker in path for marker in _SOURCE_CONTAINER_PATH_MARKERS)


def _looks_like_search_heading(value: str) -> bool:
    """Reject article headings and metadata fragments that are not named targets."""

    words = re.findall(r"[a-z0-9]+", str(value or "").lower())
    if not words or (len(words) == 1 and len(words[0]) <= 2):
        return True
    if words[0] in {"about", "best", "comparing", "describes", "discover", "top"}:
        return True
    if words[-1] in {"blog", "news", "overview", "profile", "report"}:
        return True
    return all(word in _CANDIDATE_NOISE_WORDS for word in words)


def _looks_like_topic_metadata(
    value: str,
    *,
    plan: MultiTargetResearchPlan,
) -> bool:
    value_key = _candidate_key(value)
    topic_key = _candidate_key(plan.topic)
    if not topic_key or topic_key not in value_key or value_key == topic_key:
        return False
    remainder = value_key.replace(topic_key, "", 1).strip("s")
    return remainder in {
        "",
        "company",
        "esp",
        "esps",
        "general",
        "information",
        "overview",
        "profile",
    }


def _looks_like_category_target(value: str) -> bool:
    text = str(value or "").lower()
    return bool(
        re.search(
            r"\b(?:products?|companies|tools?|chatbots?|companions?|platforms?|category)\b",
            text,
        )
    )


def _desired_count(plan: dict[str, Any], request_text: str) -> int:
    explicit = _explicit_desired_count(plan, request_text)
    if explicit is not None:
        return explicit
    return 3


def _explicit_desired_count(plan: dict[str, Any], request_text: str) -> int | None:
    raw = plan.get("desired_count")
    explicit = plan.get("desired_count_explicit")
    legacy_plan = "desired_count_explicit" not in plan and not plan.get("source")
    if isinstance(raw, int) and (explicit is True or legacy_plan):
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
    task_count = plan.peer_goal + (1 if plan.anchor_target else 0)
    return max(1, min(task_count, 4))


def _default_retrieve_profile(**_kwargs: Any) -> tuple[CompanyProfile, dict[str, Any]]:
    raise RuntimeError("retrieve_profile must be supplied by the workflow runner")
