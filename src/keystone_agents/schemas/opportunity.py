"""Opportunity scouting schemas."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_serializer, model_validator
from pydantic.json_schema import SkipJsonSchema

from keystone_agents.schemas.company_profile import ClaimEvidenceRecord
from keystone_agents.schemas.decision_trace import DecisionTrace
from keystone_agents.schemas.request_coverage import RequestCoverage
from keystone_agents.source_quality import (
    SourceQualityScore,
    SourceQualitySummary,
    independent_source_count,
)

_DISCOVERY_METADATA_FIELDS = (
    "entity_kind",
    "canonical_entity_key",
    "usa_relevance",
    "novelty",
    "search_lanes",
    "search_time_windows",
)


def _is_empty_discovery_metadata(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    return False


def _drop_empty_discovery_metadata(data: dict[str, Any]) -> dict[str, Any]:
    for field_name in _DISCOVERY_METADATA_FIELDS:
        if _is_empty_discovery_metadata(data.get(field_name)):
            data.pop(field_name, None)
    return data


class Opportunity(BaseModel):
    company_name: str
    title: str
    score: float = Field(ge=0.0, le=1.0)
    rationale: str
    next_step: str
    blockers: list[str] = Field(default_factory=list)


OpportunityType = Literal[
    "behavioral health AI",
    "digital mental health",
    "clinical AI",
    "CRO",
    "trial technology",
    "CNS biotech",
    "neurotechnology",
    "grant or collaboration opportunity",
    "journal article or publication call",
    "contract or RFP opportunity",
    "open-source repository opportunity",
    "hackathon or challenge opportunity",
]

OpportunityKind = Literal[
    "company_or_partner",
    "consulting_or_advisory",
    "role",
    "conference",
    "workshop_or_training",
    "certification_or_professional_development",
    "grant_or_fellowship",
    "contract_or_rfp",
    "industry_collaboration_or_pilot",
    "networking_or_professional_community",
    "publication_call",
    "clinical_trial_or_research",
    "accelerator_or_challenge",
    "other",
]

OpportunitySourceType = Literal[
    "fixture",
    "academic",
    "google_search",
    "news",
    "company_site",
    "conference",
    "publication",
    "job_posting",
    "funding_database",
    "government",
    "linkedin",
    "github",
    "social",
    "unknown",
]

OpportunitySignalType = Literal[
    "funding",
    "news",
    "job_posting",
    "clinical_trial",
    "grant",
    "publication",
    "journal_call",
    "conference",
    "contract_rfp",
    "repository",
    "company_page",
    "pipeline_state",
    "search",
    "unknown",
]

OpportunityPipelineStatus = Literal[
    "candidate",
    "researched",
    "approved",
    "approved_for_drafting",
    "drafted",
    "rejected",
    "archived",
]

OpportunityStateAction = Literal[
    "new",
    "update_existing",
    "skipped_duplicate",
    "blocked_by_state",
]


class OpportunitySource(BaseModel):
    source_id: str = ""
    title: str
    url: str
    source_type: OpportunitySourceType
    supported_signal: str
    evidence_excerpt: str = ""
    source_quality: SourceQualityScore | None = None

    @model_validator(mode="after")
    def populate_source_id(self) -> OpportunitySource:
        if not self.source_id:
            self.source_id = _source_id(self.source_type, self.url or self.title)
        return self


class OpportunitySignal(BaseModel):
    """One normalized signal extracted from a purpose-built opportunity source."""

    company_name: str
    signal_type: OpportunitySignalType = "unknown"
    signal_text: str
    source_id: str = ""
    source_type: OpportunitySourceType = "unknown"
    published_at: str | None = None
    freshness: Literal["fresh", "current", "stale", "unknown"] = "unknown"
    confidence_score: int = Field(default=50, ge=0, le=100)
    supports_why_now: bool = True
    relevance_notes: str = ""


class OpportunitySourceBundle(BaseModel):
    """Structured evidence bundle for LLM synthesis after deterministic scoring."""

    bundle_id: str
    company_name: str
    source_category: OpportunitySignalType
    summary: str
    sources: list[OpportunitySource] = Field(default_factory=list)
    signals: list[OpportunitySignal] = Field(default_factory=list)
    source_quality_summary: SourceQualitySummary | None = None
    missing_evidence: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    stale_signal_count: int = Field(default=0, ge=0)
    weak_evidence_reasons: list[str] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)


class ExistingOpportunityState(BaseModel):
    """Local pipeline state used to avoid duplicate opportunity work."""

    company_name: str
    status: OpportunityPipelineStatus
    opportunity_type: OpportunityType | None = None
    notes: str = ""
    last_seen: str | None = None
    source: str = "local_pipeline"
    normalized_company_key: str = ""

    @model_validator(mode="after")
    def populate_normalized_company_key(self) -> ExistingOpportunityState:
        if not self.normalized_company_key:
            self.normalized_company_key = re.sub(
                r"[^a-z0-9]+",
                "",
                self.company_name.lower(),
            )
        return self


class OpportunityStateDecision(BaseModel):
    """How Scout handled a candidate relative to existing pipeline state."""

    company_name: str
    status: OpportunityPipelineStatus | None = None
    action: OpportunityStateAction
    reason: str


class FilteredOpportunityCandidate(BaseModel):
    """Candidate removed by deterministic Scout filters, kept for auditability."""

    company_name: str = "Unknown company"
    entity_kind: str = ""
    source_category: str = ""
    source_title: str = ""
    source_url: str = ""
    query: str = ""
    query_lane: str = ""
    query_time_window: str = ""
    role_title: str = ""
    role_location: str = ""
    role_remote: bool | None = None
    role_country: str = ""
    reasons: list[str] = Field(default_factory=list)
    role_filter_notes: list[str] = Field(default_factory=list)


class OpportunityScoreBreakdown(BaseModel):
    """Transparent Analyst scoring components for one opportunity."""

    relevance_score: int = Field(default=0, ge=0, le=100)
    keystone_fit_score: int = Field(default=0, ge=0, le=100)
    source_confidence_score: int = Field(default=0, ge=0, le=100)
    urgency_score: int = Field(default=0, ge=0, le=100)
    next_action_clarity_score: int = Field(default=0, ge=0, le=100)
    priority_score: int = Field(default=0, ge=0, le=100)
    rationale: str = ""
    component_rationales: list[str] = Field(default_factory=list)


class OpportunityRecord(BaseModel):
    company_name: str
    entity_name: str = ""
    opportunity_kind: OpportunityKind = "other"
    opportunity_status: Literal["open", "closed_or_expired", "unknown"] = "unknown"
    deadline: str = ""
    eligibility_summary: str = ""
    access_mode: Literal["remote_or_virtual", "in_person", "hybrid", "unknown"] = "unknown"
    application_or_contact_path: str = ""
    detail_verification_status: Literal["page_verified", "snippet_only", "unverified"] = (
        "unverified"
    )
    entity_kind: str | None = None
    canonical_entity_key: str | None = None
    opportunity_type: OpportunityType
    role_title: str = ""
    role_location: str = ""
    role_remote: bool | None = None
    role_country: str = ""
    role_posted_at: str | None = None
    role_active: bool | None = None
    role_fit_reason: str = ""
    role_filter_notes: list[str] = Field(default_factory=list)
    usa_relevance: str | None = None
    novelty: str | None = None
    search_lanes: list[str] = Field(default_factory=list)
    search_time_windows: list[str] = Field(default_factory=list)
    priority_score: int = Field(ge=0, le=100)
    why_now_signal: str
    recommended_next_step: str
    sources: list[OpportunitySource] = Field(min_length=1)
    source_quality_summary: SourceQualitySummary | None = None
    source_signals: list[str] = Field(default_factory=list)
    source_bundles: list[OpportunitySourceBundle] = Field(default_factory=list)
    existing_state: ExistingOpportunityState | None = None
    state_action: Literal["new", "update_existing"] = "new"
    missing_evidence: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    stale_signal_count: int = Field(default=0, ge=0)
    weak_evidence_reasons: list[str] = Field(default_factory=list)
    claims: list[ClaimEvidenceRecord] = Field(default_factory=list)
    score_breakdown: OpportunityScoreBreakdown = Field(default_factory=OpportunityScoreBreakdown)
    score_rationale: str = ""
    keystone_fit_reason: str
    outside_consulting_likelihood: int = Field(ge=0, le=100)
    handoff_to_business_research_analyst: bool
    handoff_reason: str = ""
    analyst_recommendation: str = ""
    business_research_analyst_handoff_recommendation: str = ""
    research_needed: list[str] = Field(default_factory=list)
    disqualification_reasons: list[str] = Field(default_factory=list)
    outreach_draft: None = None
    approval_required_before_outreach: bool = True
    approved_for_outreach: bool = False
    unsupported_claims_flagged: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_scoring_and_validate_claims(self) -> OpportunityRecord:
        if not self.entity_name:
            self.entity_name = self.company_name
        unique_bundles: list[OpportunitySourceBundle] = []
        seen_bundle_keys: set[tuple[str, str, str]] = set()
        for bundle in self.source_bundles:
            bundle_key = (
                bundle.bundle_id.strip(),
                bundle.company_name.strip().casefold(),
                bundle.source_category,
            )
            if bundle_key in seen_bundle_keys:
                continue
            seen_bundle_keys.add(bundle_key)
            unique_bundles.append(bundle)
        self.source_bundles = unique_bundles

        source_scores = [
            source.source_quality
            for source in self.sources
            if source.source_quality is not None
        ]
        if self.source_quality_summary is not None and len(source_scores) == len(self.sources):
            self.source_quality_summary.source_count = len(self.sources)
            self.source_quality_summary.independent_source_count = independent_source_count(
                source_scores
            )
        for bundle in self.source_bundles:
            bundle_scores = [
                source.source_quality
                for source in bundle.sources
                if source.source_quality is not None
            ]
            if (
                bundle.source_quality_summary is not None
                and len(bundle_scores) == len(bundle.sources)
            ):
                bundle.source_quality_summary.source_count = len(bundle.sources)
                bundle.source_quality_summary.independent_source_count = (
                    independent_source_count(bundle_scores)
                )

        if self.score_breakdown.priority_score == 0 and self.priority_score > 0:
            self.score_breakdown = OpportunityScoreBreakdown(
                relevance_score=self.priority_score,
                keystone_fit_score=self.priority_score,
                priority_score=self.priority_score,
                source_confidence_score=(
                    self.source_quality_summary.overall_score if self.source_quality_summary else 0
                ),
                rationale="Legacy score retained without component-level Analyst details.",
            )
        if not self.score_rationale and self.score_breakdown.rationale:
            self.score_rationale = self.score_breakdown.rationale
        if not self.score_rationale:
            self.score_rationale = self.keystone_fit_reason
        if not self.analyst_recommendation:
            self.analyst_recommendation = self.recommended_next_step
        if self.handoff_to_business_research_analyst and not self.handoff_reason:
            self.handoff_reason = (
                "Priority score or missing evidence warrants Business Research Analyst review."
            )
        if (
            self.handoff_to_business_research_analyst
            and not self.business_research_analyst_handoff_recommendation
        ):
            self.business_research_analyst_handoff_recommendation = (
                "Run Business Research Analyst to validate company profile, buyer context, "
                "recent signals, and source-backed fit before any outreach drafting."
            )
        if self.handoff_to_business_research_analyst and not self.research_needed:
            self.research_needed = [
                "Validate company segment and buyer context.",
                "Confirm recent opportunity signals with source records.",
                "Assess Keystone fit before drafting outbound copy.",
            ]
        if not self.claims:
            self.claims = [
                ClaimEvidenceRecord(
                    claim_text=source.supported_signal,
                    source_id=source.source_id,
                    confidence=(
                        (source.source_quality.overall_score / 100)
                        if source.source_quality is not None
                        else 0.7
                    ),
                    claim_type="opportunity_signal",
                )
                for source in self.sources
                if source.supported_signal.strip()
            ]
            if self.keystone_fit_reason.strip() and self.sources:
                self.claims.append(
                    ClaimEvidenceRecord(
                        claim_text=self.keystone_fit_reason,
                        source_id=self.sources[0].source_id,
                        confidence=max(
                            0.0,
                            min(1.0, self.score_breakdown.keystone_fit_score / 100),
                        ),
                        claim_type="keystone_fit",
                    )
                )
        source_ids = {source.source_id for source in self.sources}
        flagged = [
            f"unbacked opportunity claim: {claim.claim_text}"
            for claim in self.claims
            if claim.source_id not in source_ids
        ]
        self.unsupported_claims_flagged = list(
            dict.fromkeys([*self.unsupported_claims_flagged, *flagged])
        )
        return self

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: Any) -> dict[str, Any]:
        return _drop_empty_discovery_metadata(handler(self))


class OpportunityAssessmentFact(BaseModel):
    """One concise confirmed fact tied to retained source identities."""

    statement: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)


class OpportunityAssessmentBrief(BaseModel):
    """Compact operator-facing assessment for one supplied opportunity."""

    opportunity_name: str = Field(min_length=1)
    opportunity_type: OpportunityType
    confirmed_facts: list[OpportunityAssessmentFact] = Field(min_length=1, max_length=8)
    interpretation: str = Field(min_length=1)
    keystone_fit: str = Field(min_length=1)
    timing_status: str = Field(min_length=1)
    geography_status: str = Field(min_length=1)
    missing_evidence: list[str] = Field(default_factory=list, max_length=6)
    next_safe_action: str = Field(min_length=1)
    retained_sources: list[OpportunitySource] = Field(min_length=1, max_length=6)
    outreach_recommended: bool = False
    external_action_performed: bool = False

    @model_validator(mode="after")
    def validate_compact_evidence_and_safety(self) -> OpportunityAssessmentBrief:
        source_ids = {source.source_id for source in self.retained_sources}
        unknown_ids = {
            source_id
            for fact in self.confirmed_facts
            for source_id in fact.source_ids
            if source_id not in source_ids
        }
        if unknown_ids:
            raise ValueError("Confirmed facts must cite retained source identities.")
        if self.outreach_recommended or self.external_action_performed:
            raise ValueError("Compact supplied-source assessment must remain review-only.")
        return self


class OpportunityScoutSynthesisDecision(BaseModel):
    """One compact model judgment over an already verified opportunity record."""

    record_key: str = Field(min_length=1, max_length=240)
    include: bool
    why_now_signal: str = Field(min_length=1, max_length=500)
    keystone_fit_reason: str = Field(min_length=1, max_length=700)
    recommended_next_step: str = Field(min_length=1, max_length=500)
    missing_evidence: list[str] = Field(default_factory=list, max_length=6)


class OpportunityScoutSynthesis(BaseModel):
    """Compact judgments merged onto deterministic Opportunity Scout evidence."""

    decisions: list[OpportunityScoutSynthesisDecision] = Field(default_factory=list, max_length=5)
    audit_summary: str = Field(min_length=1, max_length=700)
    constraint_relaxation_suggestion: str = Field(default="", max_length=500)
    outreach_generated: bool = False

    @model_validator(mode="after")
    def preserve_synthesis_safety(self) -> OpportunityScoutSynthesis:
        if self.outreach_generated:
            raise ValueError("Opportunity Scout synthesis must not generate outreach.")
        keys = [decision.record_key.strip().casefold() for decision in self.decisions]
        if len(keys) != len(set(keys)):
            raise ValueError("Opportunity Scout synthesis decisions must use unique record keys.")
        return self


class OpportunityScoutResult(BaseModel):
    topic: str | None = None
    dry_run: bool = True
    search_provider: str = ""
    search_queries: list[str] = Field(default_factory=list)
    search_lanes: list[str] = Field(default_factory=list)
    search_time_windows: list[str] = Field(default_factory=list)
    raw_search_result_count: int = Field(default=0, ge=0)
    deduped_candidate_count: int = Field(default=0, ge=0)
    filtered_candidates: list[FilteredOpportunityCandidate] = Field(default_factory=list)
    review_candidates: list[FilteredOpportunityCandidate] = Field(default_factory=list)
    records: list[OpportunityRecord] = Field(default_factory=list)
    source_bundles: list[OpportunitySourceBundle] = Field(default_factory=list)
    source_bundle_quality_notes: list[str] = Field(default_factory=list)
    state_decisions: list[OpportunityStateDecision] = Field(default_factory=list)
    duplicate_companies_skipped: list[str] = Field(default_factory=list)
    source_quality_summary: SourceQualitySummary | None = None
    decision_trace: DecisionTrace | None = None
    retrieval_diagnostics: SkipJsonSchema[dict[str, Any]] = Field(default_factory=dict)
    audit_notes: list[str] = Field(default_factory=list)
    constraint_relaxation_suggestion: str = ""
    outreach_generated: bool = False
    request_coverage: RequestCoverage = Field(default_factory=RequestCoverage)

    @model_validator(mode="after")
    def normalize_result_evidence(self) -> OpportunityScoutResult:
        if self.search_provider.strip().lower() in {"", "none"} and (
            self.raw_search_result_count == 0
        ):
            self.search_queries = []

        unique_bundles: list[OpportunitySourceBundle] = []
        seen_bundle_keys: set[tuple[str, str, str]] = set()
        for bundle in self.source_bundles:
            bundle_key = (
                bundle.bundle_id.strip(),
                bundle.company_name.strip().casefold(),
                bundle.source_category,
            )
            if bundle_key in seen_bundle_keys:
                continue
            seen_bundle_keys.add(bundle_key)
            unique_bundles.append(bundle)
        self.source_bundles = unique_bundles

        sources_by_key: dict[str, OpportunitySource] = {}
        for bundle in self.source_bundles:
            for source in bundle.sources:
                sources_by_key.setdefault(source.url.strip() or source.source_id, source)
        if not sources_by_key:
            for record in self.records:
                for source in record.sources:
                    sources_by_key.setdefault(source.url.strip() or source.source_id, source)
        sources = list(sources_by_key.values())
        source_scores = [
            source.source_quality for source in sources if source.source_quality is not None
        ]
        if self.source_quality_summary is not None and len(source_scores) == len(sources):
            self.source_quality_summary.source_count = len(sources)
            self.source_quality_summary.independent_source_count = independent_source_count(
                source_scores
            )
        for bundle in self.source_bundles:
            bundle_scores = [
                source.source_quality
                for source in bundle.sources
                if source.source_quality is not None
            ]
            if (
                bundle.source_quality_summary is not None
                and len(bundle_scores) == len(bundle.sources)
            ):
                bundle.source_quality_summary.source_count = len(bundle.sources)
                bundle.source_quality_summary.independent_source_count = (
                    independent_source_count(bundle_scores)
                )
        return self

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: Any) -> dict[str, Any]:
        return _drop_empty_discovery_metadata(handler(self))


def _source_id(source_type: str, value: str) -> str:
    cleaned = re.sub(r"^https?://", "", value.strip().lower())
    cleaned = cleaned.removeprefix("fixture://")
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-") or "source"
    return f"{source_type}:{slug[:80]}"
