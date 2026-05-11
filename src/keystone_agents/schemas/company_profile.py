"""Company profile schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from keystone_agents.source_quality import (
    ResearchCompletenessScore,
    SourceQualityScore,
    SourceQualitySummary,
)

ResearchDataPointKey = Literal[
    "business_model",
    "customer_segment",
    "behavioral_health_relevance",
    "ai_data_science_relevance",
    "research_signals",
    "funding_growth_signal",
    "compliance_sensitivity",
    "consulting_fit",
]

CompanyFeatureName = Literal[
    "employee_count_range",
    "market_segment",
    "clinical_research_relevance",
    "ai_maturity",
    "data_asset_signal",
    "funding_growth_signal",
    "partnership_signal",
    "implementation_complexity",
    "compliance_sensitivity",
    "buyer_function",
]

ClaimType = Literal[
    "company_identity",
    "company_description",
    "company_fit",
    "company_signal",
    "opportunity_signal",
    "opportunity_rationale",
    "keystone_fit",
    "keystone_profile",
    "outreach_fact",
    "contact_context",
    "user_provided",
    "unsupported",
]

ComparisonCriterionKey = Literal[
    "consulting_fit",
    "evidence_strength",
    "evidence_generation_need",
    "outside_consulting_likelihood",
    "clinical_relevance",
]

ComparisonOutcome = Literal["company_a", "company_b", "tie", "unclear"]


DEFAULT_RESEARCH_DATA_POINT_KEYS: tuple[ResearchDataPointKey, ...] = (
    "business_model",
    "customer_segment",
    "behavioral_health_relevance",
    "ai_data_science_relevance",
    "research_signals",
    "funding_growth_signal",
    "compliance_sensitivity",
    "consulting_fit",
)

DEFAULT_COMPANY_FEATURE_NAMES: tuple[CompanyFeatureName, ...] = (
    "employee_count_range",
    "market_segment",
    "clinical_research_relevance",
    "ai_maturity",
    "data_asset_signal",
    "funding_growth_signal",
    "partnership_signal",
    "implementation_complexity",
    "compliance_sensitivity",
    "buyer_function",
)

DEFAULT_COMPARISON_CRITERIA: tuple[ComparisonCriterionKey, ...] = (
    "consulting_fit",
    "evidence_strength",
    "evidence_generation_need",
    "outside_consulting_likelihood",
    "clinical_relevance",
)


class ResearchDataPoint(BaseModel):
    """Configurable company research target with source-backed completion state."""

    key: ResearchDataPointKey
    label: str = Field(min_length=1)
    value: str = ""
    completed: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    missing_reason: str = ""

    @field_validator("source_ids")
    @classmethod
    def dedupe_source_ids(cls, source_ids: list[str]) -> list[str]:
        return list(
            dict.fromkeys(source_id.strip() for source_id in source_ids if source_id.strip())
        )

    @model_validator(mode="after")
    def validate_completion(self) -> ResearchDataPoint:
        self.label = self.label.strip()
        self.value = self.value.strip()
        self.rationale = self.rationale.strip()
        self.missing_reason = self.missing_reason.strip()
        self.completed = bool(self.completed and self.value and self.source_ids)
        if self.completed and self.confidence <= 0:
            self.confidence = 0.5
        if not self.completed:
            self.confidence = 0.0
            if not self.missing_reason:
                self.missing_reason = "No source-backed value available."
        return self


class CompanyFeatureRecord(BaseModel):
    """Structured company signal backed by one explicit source claim."""

    feature_name: CompanyFeatureName
    value: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    evidence_text: str = Field(min_length=1)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    approved: bool = True

    @field_validator("value", "source_id", "evidence_text")
    @classmethod
    def strip_text_fields(cls, value: str) -> str:
        return value.strip()

    def has_source_support(self, source_by_id: dict[str, SourceRecord]) -> bool:
        source = source_by_id.get(self.source_id)
        if source is None:
            return False
        evidence = " ".join(self.evidence_text.lower().split())
        if not evidence:
            return False
        for claim in source.supported_claims:
            claim_text = " ".join(claim.lower().split())
            if evidence == claim_text or evidence in claim_text or claim_text in evidence:
                return True
        return False

    def is_source_backed(self, source_by_id: dict[str, SourceRecord]) -> bool:
        return self.approved and self.has_source_support(source_by_id)


class ClaimEvidenceRecord(BaseModel):
    """Claim-level evidence record tied to one explicit source."""

    claim_text: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    claim_type: ClaimType
    approved: bool = True


class SourceRecord(BaseModel):
    """Auditable source supporting one or more company research claims."""

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source_type: Literal[
        "fixture",
        "academic",
        "company_site",
        "funding_database",
        "government",
        "website",
        "linkedin",
        "google_search",
        "news",
        "database",
        "social",
        "unknown",
        "user_provided",
    ]
    supported_claims: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    published_at: str | None = None
    source_quality: SourceQualityScore | None = None


class CompanyBriefFact(BaseModel):
    """One factual brief claim with explicit source citations."""

    text: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_ids")
    @classmethod
    def clean_source_ids(cls, source_ids: list[str]) -> list[str]:
        return list(
            dict.fromkeys(source_id.strip() for source_id in source_ids if source_id.strip())
        )


class CompanyBriefSourceCitation(BaseModel):
    """Compact source citation safe for a human-facing company brief."""

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source_type: str = ""

    @field_validator("source_id", "title", "url", "source_type")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class CompanyBriefContactCandidate(BaseModel):
    """Potential outreach contact surfaced from source-backed company research."""

    name: str = ""
    title: str = ""
    email: str = ""
    linkedin_url: str = ""
    source_url: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_ids: list[str] = Field(default_factory=list)
    verification_status: Literal["source_backed", "needs_confirmation"] = "needs_confirmation"

    @field_validator("name", "title", "email", "linkedin_url", "source_url", mode="before")
    @classmethod
    def clean_text(cls, value: str | None) -> str:
        return str(value or "").strip()

    @field_validator("source_ids", mode="before")
    @classmethod
    def clean_source_ids(cls, source_ids: list[str] | None) -> list[str]:
        return list(
            dict.fromkeys(
                str(source_id).strip() for source_id in source_ids or [] if str(source_id).strip()
            )
        )

    @model_validator(mode="after")
    def validate_contact(self) -> CompanyBriefContactCandidate:
        if self.email and "@" not in self.email:
            self.email = ""
            self.verification_status = "needs_confirmation"
        if not self.source_ids:
            self.verification_status = "needs_confirmation"
        if self.verification_status == "source_backed" and self.confidence <= 0:
            self.confidence = 0.5
        return self


class CompanyResearchFocusedBrief(BaseModel):
    """LLM-generated BR-1 focused brief from source-backed company context."""

    company_name: str = Field(min_length=1)
    brief_purpose: Literal["partnership_or_advisory_relevance"] = (
        "partnership_or_advisory_relevance"
    )
    product: str = ""
    customers: str = ""
    traction_signals: str = ""
    leadership: str = ""
    why_it_matters: str = ""
    facts: list[CompanyBriefFact] = Field(default_factory=list)
    contact_candidates: list[CompanyBriefContactCandidate] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    source_ids_used: list[str] = Field(default_factory=list)
    sources: list[CompanyBriefSourceCitation] = Field(default_factory=list)
    raw_source_content_included: bool = False
    send_enabled: bool = False

    @field_validator(
        "company_name",
        "product",
        "customers",
        "traction_signals",
        "leadership",
        "why_it_matters",
        mode="before",
    )
    @classmethod
    def clean_text(cls, value: str | None) -> str:
        return str(value or "").strip()

    @field_validator("inferences", "unknowns", "source_ids_used", mode="before")
    @classmethod
    def clean_text_list(cls, values: list[str] | None) -> list[str]:
        return list(
            dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip())
        )

    @field_validator("raw_source_content_included", "send_enabled")
    @classmethod
    def unsafe_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("focused company briefs must not include raw source text or sends")
        return value

    @model_validator(mode="after")
    def validate_source_citations(self) -> CompanyResearchFocusedBrief:
        fact_source_ids = [source_id for fact in self.facts for source_id in fact.source_ids]
        self.source_ids_used = list(dict.fromkeys([*self.source_ids_used, *fact_source_ids]))
        cited_ids = {source.source_id for source in self.sources}
        uncited = sorted(
            source_id for source_id in self.source_ids_used if source_id not in cited_ids
        )
        if self.sources and uncited:
            self.source_ids_used = [
                source_id for source_id in self.source_ids_used if source_id in cited_ids
            ]
            self.unknowns = list(
                dict.fromkeys(
                    [
                        *self.unknowns,
                        (
                            "Some model-cited source ids were not returned in the "
                            "validated source list and require source review: "
                            f"{', '.join(uncited)}."
                        ),
                    ]
                )
            )
        return self


class CompanyResearchComparisonEntry(BaseModel):
    """One explicit criterion comparing two companies for a Keystone decision."""

    criterion_key: ComparisonCriterionKey
    criterion_label: str = Field(min_length=1)
    company_a_summary: str = ""
    company_a_source_ids: list[str] = Field(default_factory=list)
    company_a_unknowns: list[str] = Field(default_factory=list)
    company_b_summary: str = ""
    company_b_source_ids: list[str] = Field(default_factory=list)
    company_b_unknowns: list[str] = Field(default_factory=list)
    better_fit: ComparisonOutcome = "unclear"
    rationale: str = ""

    @field_validator(
        "criterion_label",
        "company_a_summary",
        "company_b_summary",
        "rationale",
        mode="before",
    )
    @classmethod
    def clean_text(cls, value: str | None) -> str:
        return str(value or "").strip()

    @field_validator(
        "company_a_source_ids",
        "company_a_unknowns",
        "company_b_source_ids",
        "company_b_unknowns",
        mode="before",
    )
    @classmethod
    def clean_text_list(cls, values: list[str] | None) -> list[str]:
        return list(
            dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip())
        )


class CompanyResearchComparison(BaseModel):
    """Decision-oriented comparison between two company profiles."""

    company_a: CompanyProfile
    company_b: CompanyProfile
    decision_goal: str = ""
    decision_criteria: list[ComparisonCriterionKey] = Field(default_factory=list)
    side_by_side_entries: list[CompanyResearchComparisonEntry] = Field(default_factory=list)
    recommendation: str = ""
    recommended_company: ComparisonOutcome = "unclear"
    evidence_gaps: list[str] = Field(default_factory=list)
    next_step: str = ""
    requested_output_format: str | None = None

    @field_validator("decision_goal", "recommendation", "next_step", mode="before")
    @classmethod
    def clean_text(cls, value: str | None) -> str:
        return str(value or "").strip()

    @field_validator("requested_output_format", mode="before")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("decision_criteria", "evidence_gaps", mode="before")
    @classmethod
    def clean_text_list(cls, values: list[str] | None) -> list[str]:
        return list(
            dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip())
        )

    @model_validator(mode="after")
    def sync_criteria_from_entries(self) -> CompanyResearchComparison:
        if not self.decision_criteria and self.side_by_side_entries:
            self.decision_criteria = [entry.criterion_key for entry in self.side_by_side_entries]
        return self


class CompanyProfile(BaseModel):
    name: str = Field(min_length=1)
    website: str | None = None
    lead_name: str | None = None
    linkedin_url: str | None = None
    description: str = ""
    fit_summary: str = ""
    behavioral_health_relevance: int = Field(default=0, ge=0, le=100)
    clinical_ai_relevance: int = Field(default=0, ge=0, le=100)
    cns_neuro_relevance: int = Field(default=0, ge=0, le=100)
    evidence_generation_need: int = Field(default=0, ge=0, le=100)
    outside_consulting_likelihood: int = Field(default=0, ge=0, le=100)
    consulting_fit_score: int = Field(default=0, ge=0, le=100)
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_explanation: str = ""
    sources: list[SourceRecord] = Field(default_factory=list)
    source_quality_summary: SourceQualitySummary | None = None
    research_completeness: ResearchCompletenessScore | None = None
    features: list[CompanyFeatureRecord] = Field(default_factory=list)
    research_data_points: list[ResearchDataPoint] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    claims: list[ClaimEvidenceRecord] = Field(default_factory=list)
    unsupported_claims_flagged: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)

    @property
    def company_name(self) -> str:
        return self.name

    @property
    def source_backed_features(self) -> list[CompanyFeatureRecord]:
        source_by_id = {source.source_id: source for source in self.sources}
        return [feature for feature in self.features if feature.is_source_backed(source_by_id)]

    @field_validator("sources")
    @classmethod
    def reject_empty_supported_claims(cls, sources: list[SourceRecord]) -> list[SourceRecord]:
        for source in sources:
            if not source.supported_claims:
                raise ValueError("Every source must support at least one claim.")
        return sources

    @model_validator(mode="after")
    def populate_and_validate_claims(self) -> CompanyProfile:
        if not self.claims and self.sources:
            self.claims = [
                ClaimEvidenceRecord(
                    claim_text=claim,
                    source_id=source.source_id,
                    confidence=source.confidence,
                    claim_type=_company_claim_type(claim),
                )
                for source in self.sources
                for claim in source.supported_claims
            ]
        if not self.evidence and self.claims:
            self.evidence = list(dict.fromkeys(claim.claim_text for claim in self.claims))

        source_ids = {source.source_id for source in self.sources}
        source_by_id = {source.source_id: source for source in self.sources}
        if source_ids:
            flagged = [
                f"unbacked company claim: {claim.claim_text}"
                for claim in self.claims
                if claim.source_id not in source_ids
            ]
            self.unsupported_claims_flagged = list(
                dict.fromkeys([*self.unsupported_claims_flagged, *flagged])
            )
        feature_flags = []
        for feature in self.features:
            if not feature.has_source_support(source_by_id):
                feature.approved = False
                feature_flags.append(f"unbacked company feature: {feature.feature_name}")
        if feature_flags:
            self.unsupported_claims_flagged = list(
                dict.fromkeys([*self.unsupported_claims_flagged, *feature_flags])
            )
        completed_data_points = []
        data_point_flags = []
        for data_point in self.research_data_points:
            unknown_sources = [
                source_id for source_id in data_point.source_ids if source_id not in source_ids
            ]
            if unknown_sources:
                data_point_flags.append(f"unbacked research data point: {data_point.key}")
                data_point.completed = False
                data_point.confidence = 0.0
                data_point.source_ids = [
                    source_id for source_id in data_point.source_ids if source_id in source_ids
                ]
                if not data_point.missing_reason:
                    data_point.missing_reason = "Referenced source is not available."
            if data_point.completed:
                completed_data_points.append(data_point.key)
        missing_keys = [
            key for key in DEFAULT_RESEARCH_DATA_POINT_KEYS if key not in completed_data_points
        ]
        for key in missing_keys:
            if not any(data_point.key == key for data_point in self.research_data_points):
                self.research_data_points.append(
                    ResearchDataPoint(
                        key=key,
                        label=research_data_point_label(key),
                        missing_reason="No source-backed value available.",
                    )
                )
        if data_point_flags:
            self.unsupported_claims_flagged = list(
                dict.fromkeys([*self.unsupported_claims_flagged, *data_point_flags])
            )
        return self


def research_data_point_label(key: ResearchDataPointKey) -> str:
    labels: dict[ResearchDataPointKey, str] = {
        "business_model": "Business model",
        "customer_segment": "Customer segment",
        "behavioral_health_relevance": "Behavioral health relevance",
        "ai_data_science_relevance": "AI/data science relevance",
        "research_signals": "Research signals",
        "funding_growth_signal": "Funding/growth signal",
        "compliance_sensitivity": "Compliance sensitivity",
        "consulting_fit": "Consulting fit",
    }
    return labels[key]


def company_feature_label(name: CompanyFeatureName) -> str:
    labels: dict[CompanyFeatureName, str] = {
        "employee_count_range": "Employee count range",
        "market_segment": "Market segment",
        "clinical_research_relevance": "Clinical/research relevance",
        "ai_maturity": "AI maturity",
        "data_asset_signal": "Data asset signal",
        "funding_growth_signal": "Funding/growth signal",
        "partnership_signal": "Partnership signal",
        "implementation_complexity": "Implementation complexity",
        "compliance_sensitivity": "Compliance sensitivity",
        "buyer_function": "Buyer function",
    }
    return labels[name]


def _company_claim_type(claim: str) -> ClaimType:
    lowered = claim.lower()
    if "fixture input identifies" in lowered or "company as" in lowered:
        return "company_identity"
    if "fit" in lowered or "relevant" in lowered or "keystone" in lowered:
        return "company_fit"
    if any(
        term in lowered
        for term in (
            "funding",
            "hiring",
            "partnership",
            "pilot",
            "trial",
            "validation",
            "outcomes",
            "conference",
            "publication",
        )
    ):
        return "company_signal"
    return "company_description"
