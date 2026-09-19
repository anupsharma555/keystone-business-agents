"""Opportunity scout agent builder and deterministic fixture-mode scout."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlparse

from keystone_agents.agent_decision_contracts import opportunity_scout_decision_contract
from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.capabilities.tool_scope import (
    ToolScopeMode,
    attach_tool_scope_receipt,
    default_tool_tier_for_request,
    scope_tools_for_request,
    tool_free_synthesis_attachment,
    tool_scope_receipt_for_agent,
    tool_scope_trace_metadata_for_agent,
)
from keystone_agents.guardrails import keystone_guardrails, keystone_tool_guardrail_kwargs
from keystone_agents.models import OpportunityScoutSDKInput, TypedAgentRunResult
from keystone_agents.opportunity_scout.scoring import (
    HANDOFF_PRIORITY_THRESHOLD,
    bounded_score,
    business_research_analyst_handoff_recommendation,
    clean_signals,
    keystone_fit_reason,
    normalize_type,
    score_from_signals,
    should_handoff_to_business_research_analyst,
)
from keystone_agents.opportunity_scout.scoring import (
    handoff_reason as build_handoff_reason,
)
from keystone_agents.opportunity_scout.scoring import (
    outside_consulting_likelihood as estimate_outside_consulting_likelihood,
)
from keystone_agents.opportunity_scout.scoring import (
    research_needed as build_research_needed,
)
from keystone_agents.opportunity_scout.search_plan import (
    infer_opportunity_search_plan,
    plan_has_objective,
    plan_targets_only,
)
from keystone_agents.opportunity_scout.state import (
    SKIP_EXISTING_STATUSES,
    UPDATE_EXISTING_STATUSES,
)
from keystone_agents.opportunity_scout.state import (
    load_existing_state_map as _load_existing_state_map,
)
from keystone_agents.opportunity_scout.state import (
    normalize_company_key as _normalize_company_key,
)
from keystone_agents.opportunity_scout.state import (
    state_items_from_payload as _state_items_from_payload,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.decision_validation import AgentDecisionContract
from keystone_agents.runtime.tool_call_budget import ToolCallBudgetContract
from keystone_agents.runtime.tool_execution import (
    ToolEvidenceGroup,
    ToolExecutionContract,
)
from keystone_agents.schemas.decision_trace import DecisionTrace
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.opportunity import (
    ExistingOpportunityState,
    FilteredOpportunityCandidate,
    OpportunityAssessmentBrief,
    OpportunityKind,
    OpportunityRecord,
    OpportunityScoutResult,
    OpportunityScoutSynthesis,
    OpportunitySignal,
    OpportunitySource,
    OpportunitySourceBundle,
    OpportunityStateDecision,
    OpportunityType,
    SuppliedOpportunityResult,
)
from keystone_agents.schemas.opportunity_search_plan import OpportunitySearchPlan
from keystone_agents.sdk import (
    Agent,
    build_model_settings,
    build_sdk_agent,
    compose_direct_instructions,
    compose_instructions,
    function_tool,
)
from keystone_agents.sdk_run_policy import (
    resolve_sdk_tool_call_limit,
    resolve_sdk_turn_policy,
)
from keystone_agents.skill_sets import select_agent_skill_names, skill_request_text
from keystone_agents.source_quality import (
    score_source_quality,
    summarize_source_quality,
)
from keystone_agents.source_registry import (
    assess_source_coverage,
    required_source_lanes_for_opportunity,
)
from keystone_agents.tools.browser_diagnostics_tool import (
    capture_browser_diagnostics,
    summarize_rendered_page_diagnostics,
)
from keystone_agents.tools.html_review_tool import (
    agent_html_review_enabled,
    agent_html_review_max_pages,
    agent_html_review_min_claims,
    extract_research_claims_from_html,
    run_agent_html_review,
)
from keystone_agents.tools.internal_data_tools import (
    airtable_get_base_schema,
    airtable_read_records,
    airtable_write_record,
    google_workspace_tools,
)
from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
)
from keystone_agents.tools.memory_tool import (
    check_workflow_duplicate,
    retrieve_memory,
    save_entity_memory,
    save_opportunity_memory,
)
from keystone_agents.tools.playwright_tool import render_page
from keystone_agents.tools.search_provider import (
    LiveSearchProviderRequiredError,
    SearchProviderError,
    SearchRequest,
    build_search_provider,
)
from keystone_agents.tools.serper_tool import search_web
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionBudget,
    WebsiteExtractionError,
    WebsiteExtractionResult,
    extract_selected_urls_to_source_bundle,
    extract_website_content,
    extract_website_content_with_fallbacks,
    project_web_source,
    read_web_source_window,
    website_extraction_budget,
    website_extraction_provider_sequence,
)

LIVE_SEARCH_QUERIES: tuple[str, ...] = (
    "behavioral health AI startup recent funding clinical validation",
    "digital mental health company payer partnership outcomes study",
    "clinical AI company psychiatry evidence generation",
    "trial technology company CNS study AI",
    "CRO behavioral health decentralized trial AI",
    "neurotechnology company clinical validation psychiatry",
    "CNS biotech precision psychiatry biomarker trial",
)
OPPORTUNITY_FIXTURE_ONLY_TOOL_NAMES = frozenset(
    {
        "search_opportunity_sources_placeholder",
        "load_existing_opportunity_state",
        "search_funding_news_sources",
        "search_job_posting_sources",
        "search_clinical_trials_sources",
        "search_grant_sources",
        "search_conference_publication_sources",
        "search_journal_call_sources",
        "search_contract_rfp_sources",
        "search_company_page_sources",
        "handoff_to_business_research_analyst_placeholder",
        "save_opportunity_placeholder",
    }
)

_OPPORTUNITY_SCOUT_REQUIRED_RESEARCH_TOOLS = (
    "search_web",
    "extract_research_claims_from_html",
    "score_opportunity",
)


def _required_opportunity_scout_tools_for_request(
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Keep supplied-fact comparisons tool-light while preserving discovery."""

    plan = ExecutionIntentAuthority.from_value(manual_request_plan).plan
    if (
        plan is not None
        and plan.intent == "opportunity_search"
        and plan.expected_artifact_type == "opportunity_record"
        and not plan.requires_live_search
        and plan.ask_shape.permission_state == "read_only"
        and "comparison-format" in plan.constraints
    ):
        return ("score_opportunity",)
    return _OPPORTUNITY_SCOUT_REQUIRED_RESEARCH_TOOLS
JOB_POSTING_URL_MARKERS = ("/jobs", "/job/", "/careers", "/positions", "/openings")
JOB_POSTING_TEXT_MARKERS = (
    "we're hiring",
    "we are hiring",
    "job posting",
    "career opportunity",
    "apply now",
    "open role",
)
REMOTE_MARKERS = ("remote", "work from home", "anywhere in the u.s")
ONSITE_MARKERS = ("on-site", "onsite", "in person", "hybrid", "office-based")
UNPAID_MARKERS = ("unpaid", "volunteer", "internship", "for credit", "equity only")
CLINICIAN_REQUIRED_MARKERS = (
    "practicing clinician",
    "practicing psychiatrist",
    "licensed psychiatrist",
    "licensed therapist",
    "active clinical practice",
    "active patient panel",
    "board-certified psychiatrist",
    "full-time practicing clinician",
)
ROLE_TITLE_PREFIX_RE = re.compile(
    r"\b(?:hiring|seeking|seeks|opening for|open role:?|position:?|role:?)\s+(.+)",
    re.I,
)
EMPLOYEE_RANGE_RE = re.compile(
    r"\b(?P<low>\d{1,4})(?:\s*[-–]\s*(?P<high>\d{1,4})|\+)?\s+employees?\b",
    re.I,
)
ROLE_HINT_PATTERNS = (
    "director",
    "manager",
    "lead",
    "head of",
    "chief",
    "officer",
    "scientist",
    "researcher",
    "operations",
    "product",
    "strategy",
    "advisor",
    "medical director",
    "clinical operations",
)
ROLE_SEARCH_MARKERS = (
    " role",
    " roles",
    " job",
    " jobs",
    " hiring",
    " position",
    " positions",
    " posted",
    " remote",
    " full-time",
    " part-time",
    " title",
    " titles",
)
TOPIC_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "only",
    "focus",
    "find",
    "give",
    "need",
    "many",
    "across",
    "time",
    "opportunities",
    "opportunity",
    "companies",
    "company",
    "based",
    "about",
    "their",
    "where",
    "also",
    "just",
    "through",
}
USA_TEXT_MARKERS = (
    "united states",
    "u.s.",
    "u.s ",
    " us ",
    " usa",
    "nih",
    "fda",
    "cms",
    "medicare",
    "medicaid",
    "veterans affairs",
)
USA_DOMAIN_MARKERS = (
    "clinicaltrials.gov",
    "reporter.nih.gov",
    "nih.gov",
    "fda.gov",
    "cms.gov",
    "hhs.gov",
    ".edu",
)
NON_US_MARKERS = (
    "united kingdom",
    "uk",
    "canada",
    "europe",
    "european",
    "australia",
    "germany",
    "france",
    "sweden",
    "netherlands",
)
INSTITUTE_MARKERS = (
    "university",
    "medical school",
    "school of medicine",
    "hospital",
    "health system",
    "institute",
    "center",
    "centre",
    "department",
    "laboratory",
    "lab",
)
CONFERENCE_MARKERS = (
    "conference",
    "symposium",
    "congress",
    "meeting",
    "summit",
    "forum",
    "workshop",
    "abstract",
    "poster",
)
JOURNAL_CALL_MARKERS = (
    "journal",
    "special issue",
    "call for papers",
    "call for manuscripts",
    "manuscript",
    "article collection",
)
CONTRACT_RFP_MARKERS = (
    "rfp",
    "request for proposal",
    "solicitation",
    "procurement",
    "contract opportunity",
    "sam.gov",
    "sources sought",
)
PERSON_NAME_RE = re.compile(
    r"^(?:dr\.?\s+)?([A-Z][a-z][A-Za-z'`.-]*(?:\s+(?:[A-Z]\.?|[A-Z][a-z][A-Za-z'`.-]*)){1,4})\b"
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")
PERSON_CONTEXT_MARKERS = (
    "principal investigator",
    "investigator",
    "faculty",
    "researcher",
    "scientist",
    "professor",
    "assistant professor",
    "associate professor",
    "phd",
    "md",
    "dr.",
    "dr ",
    ", phd",
    ", md",
)
INVALID_CANDIDATE_NAMES = {
    "",
    "unknown",
    "unknown company",
    "http",
    "https",
    "www",
    "pdf",
    "doi",
    "blog",
    "blogs",
    "insights",
    "news",
    "people",
    "our people",
    "team",
    "faculty",
    "speakers",
    "agenda",
    "schedule",
    "conferences",
    "pubmed",
    "clinicaltrials",
    "clinicaltrials.gov",
    "nih",
    "research",
    "article",
    "articles",
    "correction",
}
TITLE_NOISE_MARKERS = (
    "[pdf]",
    "(pdf)",
    " pdf ",
    ".pdf",
    "thesis",
    "dissertation",
    "doctoral thesis",
    "master's thesis",
    "masters thesis",
    "book chapter",
    "google books",
    "handbook of",
    "textbook",
)
GENERIC_RESEARCH_PAGE_MARKERS = (
    "research articles",
    "research topics",
    "latest research",
    "top articles",
    "review article",
    "systematic review",
    "literature review",
    "sciencedirect topics",
    "google scholar",
    "semantic scholar",
    "researchgate",
    "pubmed search",
    "frontiers research topic",
    "artificial intelligence for mental health monitoring",
)
GENERIC_RESEARCH_HOST_MARKERS = (
    "books.google.",
    "scholar.google.",
    "semanticscholar.org",
    "researchgate.net",
    "sciencedirect.com/topics",
    "pmc.ncbi.nlm.nih.gov/articles",
    "ncbi.nlm.nih.gov/pmc",
    "nature.com/articles",
    "science.org/doi",
)
GENERIC_PROGRAM_PAGE_MARKERS = (
    "seed funding projects",
    "seed fund project archive",
    "pathway programs",
    "industry affiliate program",
    "affiliate program",
    "student projects",
    "funding projects",
    "awards and scholarships recipients",
    "scholarships recipients",
    "volume ",
    "number ",
    "newsletter",
)
ACTIVE_OPPORTUNITY_MARKERS = (
    "raises",
    "raised",
    "funding round",
    "funding opportunity",
    "request for proposals",
    "request for proposal",
    "rfp",
    "solicitation",
    "sources sought",
    "applications open",
    "registration open",
    "enrollment open",
    "now enrolling",
    "membership open",
    "register now",
    "apply by",
    "deadline",
    "series a",
    "series b",
    "grant awarded",
    "sbir",
    "nih grant",
    "hiring",
    "careers",
    "open role",
    "job posting",
    "apply now",
    "launches",
    "launched",
    "selects",
    "selected",
    "announces",
    "announced",
    "partners",
    "partners with",
    "partner to",
    "partnership",
    "collaboration",
    "collaborates",
    "pilot",
    "conference",
    "symposium",
    "workshop",
    "call for papers",
    "call for manuscripts",
    "special issue",
    "manuscript submission",
    "validation study",
    "trial launch",
    "study sponsor",
    "recruiting",
    "outcomes evidence",
    "contract",
    "commercial",
)
PSYCHIATRY_TOPIC_MARKERS = (
    "psychiatry",
    "psychiatric",
    "psychiatrist",
    "psychiatrists",
    "neuropsychiatry",
    "mental health",
    "behavioral health",
    "behavioural health",
    "depression",
    "anxiety",
    "adhd",
    "mood disorder",
    "psychosis",
    "schizophrenia",
    "bipolar",
)
ADVISORY_TOPIC_MARKERS = (
    "advisor",
    "advisory",
    "consultant",
    "consulting",
    "fractional",
    "medical director",
    "clinical strategy",
    "strategy advisor",
    "expert network",
)
AI_TOPIC_MARKERS = (
    " ai ",
    "artificial intelligence",
    "machine learning",
    " ml ",
    "predictive",
    "automation",
    "algorithm",
    "analytics",
    "digital",
    "technology",
    "platform",
)
ADJACENT_HEALTHCARE_AI_MARKERS = (
    "healthcare",
    "health care",
    "clinical ai",
    "clinical workflow",
    "clinical automation",
    "clinical decision",
    "care delivery",
    "patient care",
    "provider workflow",
    "health system",
    "hospital",
    "payer",
    "medical ai",
    "digital health",
    "clinical operations",
    "clinical trial",
    "trial technology",
    "study delivery",
    "cns",
)
NEUROINFORMATICS_TOPIC_MARKERS = (
    "neuroinformatics",
    "neuroscience",
    "computational neuroscience",
    "biomedical informatics",
    "clinical informatics",
    "brain",
    "cns",
)
NON_CLINICAL_AI_MARKERS = (
    "workforce management",
    "human capital",
    "human resources",
    "hr technology",
    "employee experience",
    "payroll",
    "advertising",
    "marketing automation",
    "automaker",
    "automotive",
    "vehicle",
    "mobility",
    "payment",
    "payments",
    "fintech",
    "cloud infrastructure",
    "ai infrastructure",
    "model infrastructure",
    "developer platform",
)
EXPLICIT_EXCLUSION_MARKERS: dict[str, tuple[str, ...]] = {
    "payments": ("payment", "payments", "fintech"),
    "payment": ("payment", "payments", "fintech"),
    "advertising": ("advertising", "adtech", "marketing automation"),
    "automaker": ("automaker", "automotive", "vehicle", "stellantis", "ford", "gm"),
    "automotive": ("automaker", "automotive", "vehicle", "stellantis", "ford", "gm"),
    "generic ai infrastructure": (
        "generic ai infrastructure",
        "cloud infrastructure",
        "ai infrastructure",
        "model infrastructure",
        "developer platform",
    ),
    "ai infrastructure": (
        "cloud infrastructure",
        "ai infrastructure",
        "model infrastructure",
        "developer platform",
    ),
}
ORGANIZATION_SUFFIX_RE = re.compile(
    r"\b(?:"
    r"ai|analytics|biotech|bio|biosciences|care|clinic|clinics|company|corp|"
    r"corporation|health|healthcare|inc|institute|labs?|life sciences|llc|"
    r"medical|medicine|partners|pharma|research|school|systems?|technologies|"
    r"therapeutics|university|ventures"
    r")\b",
    re.I,
)


@dataclass(frozen=True)
class _OpportunityQuerySpec:
    lane: str
    time_window: str
    query: str
    entity_hint: str = "company"
    source: str = "web"
    country: str = "US"
    location: str = "United States"
    language: str | None = None
    safe_search: int | None = None
    page: int | None = None


@dataclass(frozen=True)
class _OpportunityHardFilters:
    exclude_under_employee_count: int | None = None
    exclude_onsite: bool = False
    exclude_unpaid: bool = False
    exclude_practicing_clinician: bool = False
    require_remote: bool = False
    require_us: bool = False
    require_part_time_or_fractional: bool = False
    required_role_markers: tuple[str, ...] = ()

    @property
    def strict_verification(self) -> bool:
        return any(
            [
                self.exclude_under_employee_count is not None,
                self.exclude_onsite,
                self.exclude_unpaid,
                self.exclude_practicing_clinician,
                self.require_remote,
                self.require_us,
                self.require_part_time_or_fractional,
                bool(self.required_role_markers),
            ]
        )


@dataclass(frozen=True)
class _RoleEvidence:
    is_role_candidate: bool
    role_title: str = ""
    role_location: str = ""
    role_remote: bool | None = None
    role_country: str = ""
    role_active: bool | None = None
    employee_count_range: tuple[int, int] | None = None
    unpaid: bool | None = None
    practicing_clinician_required: bool | None = None
    filter_notes: tuple[str, ...] = ()


DEFAULT_FIXTURE_HITS: list[dict[str, Any]] = [
    {
        "company_name": "NeuroFlow",
        "opportunity_type": "behavioral health AI",
        "signal": "Payer partnership and outcomes evidence for behavioral health measurement.",
        "signals": ["payer partnership", "publication or outcomes evidence"],
        "source_title": "Fixture payer partnership announcement",
        "source_url": "fixture://neuroflow-payer-partnership",
        "source_type": "fixture",
    },
    {
        "company_name": "Curebase",
        "opportunity_type": "trial technology",
        "signal": (
            "Hiring clinical operations and evidence roles tied to decentralized trial delivery."
        ),
        "signals": ["hiring clinical", "hiring evidence", "clinical trial launch"],
        "source_title": "Fixture clinical operations hiring signal",
        "source_url": "fixture://curebase-clinical-ops",
        "source_type": "fixture",
    },
    {
        "company_name": "Beacon CNS Therapeutics",
        "opportunity_type": "CNS biotech",
        "signal": "Protocol activity and validation planning around a CNS development program.",
        "signals": ["IRB or protocol activity", "validation study"],
        "source_title": "Fixture CNS protocol update",
        "source_url": "fixture://beacon-cns-protocol",
        "source_type": "fixture",
    },
    {
        "company_name": "MindSpan Digital Health",
        "opportunity_type": "digital mental health",
        "signal": "Product launch with research-facing evidence generation needs.",
        "signals": ["product launch", "validation study", "conference activity"],
        "source_title": "Fixture product launch brief",
        "source_url": "fixture://mindspan-product-launch",
        "source_type": "fixture",
    },
    {
        "company_name": "Synapse Trial Partners",
        "opportunity_type": "CRO",
        "signal": "Partnership announcement focused on CNS study operations.",
        "signals": ["partnership announcement", "hiring research"],
        "source_title": "Fixture CNS CRO partnership",
        "source_url": "fixture://synapse-cro-partnership",
        "source_type": "fixture",
    },
    {
        "company_name": "CortexBridge Labs",
        "opportunity_type": "grant or collaboration opportunity",
        "signal": (
            "Conference activity and publication evidence around translational neuroscience "
            "data workflows."
        ),
        "signals": ["conference activity", "publication or outcomes evidence"],
        "source_title": "Fixture translational neuroscience conference abstract",
        "source_url": "fixture://cortexbridge-conference",
        "source_type": "fixture",
    },
]

DEFAULT_STRUCTURED_SOURCE_HITS: list[dict[str, Any]] = [
    {
        "company_name": "Curebase",
        "opportunity_type": "trial technology",
        "signal": (
            "Careers page lists clinical operations hiring tied to decentralized trial delivery."
        ),
        "signals": ["hiring clinical", "hiring evidence", "clinical trial launch"],
        "source_title": "Curebase clinical operations careers fixture",
        "source_url": "fixture://company/curebase-careers-clinical-ops",
        "source_type": "job_posting",
        "source_category": "job_posting",
        "published_at": "2026-03-15",
        "recommended_next_actions": [
            "Confirm current clinical operations roles on the company careers page.",
        ],
    },
    {
        "company_name": "Curebase",
        "opportunity_type": "trial technology",
        "signal": (
            "Company materials describe decentralized trial platform work for lean study teams."
        ),
        "signals": ["clinical trial launch", "hiring evidence"],
        "source_title": "Curebase company page fixture",
        "source_url": "fixture://company/curebase-platform",
        "source_type": "company_site",
        "source_category": "company_page",
        "published_at": "2026-02-10",
    },
    {
        "company_name": "MindSpan Digital Health",
        "opportunity_type": "digital mental health",
        "signal": "Conference abstract highlights digital mental health validation work.",
        "signals": ["validation study", "conference activity"],
        "source_title": "MindSpan outcomes conference abstract fixture",
        "source_url": "fixture://conference/mindspan-validation-abstract",
        "source_type": "conference",
        "source_category": "conference",
        "published_at": "2026-01-18",
    },
    {
        "company_name": "MindSpan Digital Health",
        "opportunity_type": "digital mental health",
        "signal": "Company page describes evidence-generation roadmap for product launch.",
        "signals": ["product launch", "validation study"],
        "source_title": "MindSpan evidence roadmap fixture",
        "source_url": "fixture://company/mindspan-evidence-roadmap",
        "source_type": "company_site",
        "source_category": "company_page",
        "published_at": "2026-02-22",
    },
    {
        "company_name": "NeuroFlow",
        "opportunity_type": "behavioral health AI",
        "signal": "News item describes payer partnership and outcomes evidence.",
        "signals": ["payer partnership", "publication or outcomes evidence"],
        "source_title": "NeuroFlow payer outcomes news fixture",
        "source_url": "fixture://news/neuroflow-payer-outcomes",
        "source_type": "news",
        "source_category": "news",
        "published_at": "2026-02-05",
    },
    {
        "company_name": "Beacon CNS Therapeutics",
        "opportunity_type": "CNS biotech",
        "signal": (
            "ClinicalTrials fixture indicates CNS protocol activity requiring validation review."
        ),
        "signals": ["IRB or protocol activity", "validation study", "clinical trial launch"],
        "source_title": "Beacon CNS ClinicalTrials.gov fixture",
        "source_url": "https://clinicaltrials.gov/study/fixture-beacon-cns",
        "source_type": "clinical_trial",
        "source_category": "clinical_trial",
        "published_at": "2026-01-28",
    },
    {
        "company_name": "CortexBridge Labs",
        "opportunity_type": "grant or collaboration opportunity",
        "signal": (
            "NIH/SBIR-style grant fixture describes translational neuroscience data workflow."
        ),
        "signals": ["publication or outcomes evidence", "partnership announcement"],
        "source_title": "CortexBridge SBIR grant fixture",
        "source_url": "https://reporter.nih.gov/search/fixture-cortexbridge",
        "source_type": "government",
        "source_category": "grant",
        "published_at": "2025-11-30",
        "missing_evidence": ["No buyer title or commercial trigger has been confirmed."],
    },
    {
        "company_name": "Precision Psychiatry Special Issue",
        "entity_kind": "journal_call",
        "opportunity_type": "journal article or publication call",
        "signal": (
            "Special issue call seeks manuscripts on AI, digital mental health, and "
            "implementation evidence."
        ),
        "signals": ["journal article call", "publication or outcomes evidence"],
        "source_title": "Precision psychiatry AI special issue call fixture",
        "source_url": "fixture://journal/precision-psychiatry-ai-special-issue",
        "source_type": "publication",
        "source_category": "journal_call",
        "published_at": "2026-04-10",
        "recommended_next_actions": [
            "Confirm submission deadline, article scope, and editor contact path.",
        ],
    },
    {
        "company_name": "SAM.gov Behavioral Health AI Evaluation RFP",
        "entity_kind": "contract_rfp",
        "opportunity_type": "contract or RFP opportunity",
        "signal": (
            "Solicitation fixture requests behavioral health analytics and evaluation support "
            "for U.S. public-sector programs."
        ),
        "signals": ["contract or RFP", "validation study", "partnership announcement"],
        "source_title": "SAM.gov behavioral health analytics solicitation fixture",
        "source_url": "fixture://government/sam-behavioral-health-ai-rfp",
        "source_type": "government",
        "source_category": "contract_rfp",
        "published_at": "2026-04-15",
        "recommended_next_actions": [
            "Confirm eligibility, response deadline, and whether a teaming partner is needed.",
        ],
    },
]

SOURCE_TOOL_FIXTURE_HITS: dict[str, list[dict[str, Any]]] = {
    "funding_news": [
        {
            "company_name": "SignalPath NeuroAI",
            "opportunity_type": "clinical AI",
            "signal": "Recent seed funding and launch coverage for psychiatry evidence tooling.",
            "signals": ["recent funding", "product launch", "validation study"],
            "source_title": "SignalPath funding news fixture",
            "source_url": "fixture://news/signalpath-neuroai-seed",
            "source_type": "news",
            "source_category": "news",
            "published_at": "2026-03-20",
            "recommended_next_actions": [
                "Validate funding date and investor context from an independent source.",
            ],
        }
    ],
    "job_posting": [
        DEFAULT_STRUCTURED_SOURCE_HITS[0],
        {
            "company_name": "TrialOps Cortex",
            "opportunity_type": "trial technology",
            "signal": "Hiring head of clinical operations and evidence lead for CNS trials.",
            "signals": ["hiring clinical", "hiring evidence", "clinical trial launch"],
            "source_title": "TrialOps Cortex careers fixture",
            "source_url": "fixture://jobs/trialops-cortex-clinical-ops",
            "source_type": "job_posting",
            "source_category": "job_posting",
            "published_at": "2026-04-01",
        },
    ],
    "clinical_trial": [
        DEFAULT_STRUCTURED_SOURCE_HITS[5],
        {
            "company_name": "AffectAI Research",
            "opportunity_type": "behavioral health AI",
            "signal": "ClinicalTrials fixture shows behavioral health AI validation study launch.",
            "signals": ["clinical trial launch", "validation study", "IRB or protocol activity"],
            "source_title": "AffectAI ClinicalTrials.gov fixture",
            "source_url": "https://clinicaltrials.gov/study/fixture-affectai",
            "source_type": "clinical_trial",
            "source_category": "clinical_trial",
            "published_at": "2026-03-05",
        },
    ],
    "grant": [
        DEFAULT_STRUCTURED_SOURCE_HITS[6],
        {
            "company_name": "CNS Biomarker Analytics",
            "opportunity_type": "CNS biotech",
            "signal": (
                "NIH/SBIR fixture supports biomarker validation and clinical research operations."
            ),
            "signals": ["validation study", "publication or outcomes evidence"],
            "source_title": "CNS Biomarker Analytics NIH award fixture",
            "source_url": "https://reporter.nih.gov/search/fixture-cns-biomarker",
            "source_type": "government",
            "source_category": "grant",
            "published_at": "2026-02-12",
        },
    ],
    "conference_publication": [
        DEFAULT_STRUCTURED_SOURCE_HITS[2],
        {
            "company_name": "NeuroMeasure Labs",
            "opportunity_type": "neurotechnology",
            "signal": "Publication fixture reports outcomes evidence for neuroscience measurement.",
            "signals": ["publication or outcomes evidence", "validation study"],
            "source_title": "NeuroMeasure validation publication fixture",
            "source_url": "https://pubmed.ncbi.nlm.nih.gov/fixture-neuromeasure",
            "source_type": "publication",
            "source_category": "publication",
            "published_at": "2026-01-09",
        },
    ],
    "journal_call": [
        DEFAULT_STRUCTURED_SOURCE_HITS[7],
    ],
    "contract_rfp": [
        DEFAULT_STRUCTURED_SOURCE_HITS[8],
    ],
    "company_page": [
        DEFAULT_STRUCTURED_SOURCE_HITS[1],
        DEFAULT_STRUCTURED_SOURCE_HITS[3],
    ],
}


def _load_fixture_hits(fixture: str | Path | None) -> list[dict[str, Any]]:
    if fixture is None:
        return list(DEFAULT_FIXTURE_HITS)

    data = json.loads(Path(fixture).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("records"), list):
            return [item for item in data["records"] if isinstance(item, dict)]
        if isinstance(data.get("opportunities"), list):
            return [item for item in data["opportunities"] if isinstance(item, dict)]
        return [data]
    raise ValueError("Opportunity fixture must be a JSON object, records object, or list.")


def _topic_keywords(topic: str | None) -> list[str]:
    if not topic:
        return []
    normalized_topic = re.sub(r"[-_/]+", " ", topic.lower())
    tokens = [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9.+]*", normalized_topic)
        if len(token) >= 3 and token not in TOPIC_STOPWORDS
    ]
    ordered: list[str] = []
    for token in tokens:
        if token not in ordered:
            ordered.append(token)
    return ordered


def _matches_topic(hit: dict[str, Any], topic: str | None) -> bool:
    if not topic:
        return True
    haystack = " ".join(
        [
            str(hit.get("company_name", "")),
            str(hit.get("opportunity_type", "")),
            str(hit.get("entity_kind", "")),
            str(hit.get("source_category", "")),
            str(hit.get("signal", "")),
            " ".join(str(signal) for signal in hit.get("signals", [])),
        ]
    ).lower()
    needle = topic.lower().strip()
    if needle and needle in haystack:
        return True
    keywords = _topic_keywords(topic)
    if not keywords:
        return True
    match_count = sum(1 for token in keywords if token in haystack)
    threshold = min(2, len(keywords))
    return match_count >= threshold


def _source_from_hit(hit: dict[str, Any], signal: str) -> OpportunitySource:
    source_url = str(hit.get("source_url") or "fixture://opportunity-source")
    source_type = str(hit.get("source_type") or "fixture")
    source_id = str(hit.get("source_id") or "").strip()
    if not source_id and source_url.startswith("fixture://"):
        source_id = f"fixture:{source_url.removeprefix('fixture://')}"
    source_quality = score_source_quality(
        url=source_url,
        title=str(hit.get("source_title") or ""),
        declared_source_type=source_type,
        supported_text=signal,
        published_at=hit.get("published_at") or hit.get("date"),
        claim_context="opportunity_signal",
    )
    return OpportunitySource(
        source_id=source_id,
        title=str(
            hit.get("source_title") or f"{hit.get('company_name', 'Unknown')} fixture source"
        ),
        url=source_url,
        source_type=source_quality.source_type,
        supported_signal=signal,
        evidence_excerpt=str(hit.get("verified_excerpt") or "")[:1000],
        web_source_access=hit.get("web_source_access"),
        source_quality=source_quality,
    )


def _source_category_from_hit(hit: dict[str, Any]) -> str:
    category = str(hit.get("source_category") or "").strip().lower()
    aliases = {
        "funding": "funding",
        "funding_news": "news",
        "news": "news",
        "job": "job_posting",
        "jobs": "job_posting",
        "job_posting": "job_posting",
        "careers": "job_posting",
        "clinical_trial": "clinical_trial",
        "clinicaltrials": "clinical_trial",
        "grant": "grant",
        "sbir": "grant",
        "nih": "grant",
        "publication": "publication",
        "journal": "journal_call",
        "journal_call": "journal_call",
        "special_issue": "journal_call",
        "conference": "conference",
        "conference_publication": "publication",
        "contract": "contract_rfp",
        "contract_rfp": "contract_rfp",
        "rfp": "contract_rfp",
        "solicitation": "contract_rfp",
        "company": "company_page",
        "company_page": "company_page",
        "company_site": "company_page",
        "search": "search",
    }
    if category in aliases:
        return aliases[category]
    source_type = str(hit.get("source_type") or "").strip().lower()
    if source_type in {"job_posting", "careers"}:
        return "job_posting"
    if source_type in {"government"} and _contains_any_marker(
        " ".join(
            [
                str(hit.get("source_title") or ""),
                str(hit.get("signal") or ""),
                str(hit.get("source_url") or ""),
            ]
        ),
        CONTRACT_RFP_MARKERS,
    ):
        return "contract_rfp"
    if source_type in {"government"}:
        return "grant"
    if source_type in {"academic", "publication"}:
        if _contains_any_marker(
            " ".join(
                [
                    str(hit.get("source_title") or ""),
                    str(hit.get("signal") or ""),
                    str(hit.get("source_url") or ""),
                ]
            ),
            JOURNAL_CALL_MARKERS,
        ):
            return "journal_call"
        return "publication"
    if source_type in {"company_site", "website"}:
        return "company_page"
    if source_type in {"news", "funding_database"}:
        return "news" if source_type == "news" else "funding"
    if source_type in {"conference"}:
        return "conference"
    if source_type in {"google_search", "search", "metasearch"}:
        return "search"
    return "unknown"


def _signal_freshness(source: OpportunitySource) -> str:
    if source.source_quality is None:
        return "unknown"
    recency = source.source_quality.recency_score
    if recency >= 90:
        return "fresh"
    if recency >= 65:
        return "current"
    return "stale"


def _signal_from_hit(
    hit: dict[str, Any],
    *,
    source: OpportunitySource,
    signal_text: str,
) -> OpportunitySignal:
    confidence_score = source.source_quality.overall_score if source.source_quality else 45
    return OpportunitySignal(
        company_name=str(hit.get("company_name") or "Unknown company"),
        signal_type=_source_category_from_hit(hit),  # type: ignore[arg-type]
        signal_text=signal_text,
        source_id=source.source_id,
        source_type=source.source_type,
        published_at=(
            str(hit.get("published_at") or hit.get("date"))
            if hit.get("published_at") or hit.get("date")
            else None
        ),
        freshness=_signal_freshness(source),  # type: ignore[arg-type]
        confidence_score=confidence_score,
        supports_why_now=bool(hit.get("supports_why_now", True)),
        relevance_notes=str(hit.get("relevance_notes") or ""),
    )


def _source_bundles_from_hits(
    *,
    company_name: str,
    source_hits: list[dict[str, Any]],
    why_now: str,
) -> list[OpportunitySourceBundle]:
    grouped: dict[str, list[tuple[dict[str, Any], OpportunitySource]]] = {}
    for source_hit in source_hits:
        signal_text = str(source_hit.get("signal") or why_now)
        source = _source_from_hit(source_hit, signal_text)
        grouped.setdefault(_source_category_from_hit(source_hit), []).append((source_hit, source))

    bundles: list[OpportunitySourceBundle] = []
    for category in sorted(grouped):
        items = grouped[category]
        sources = [source for _hit, source in items]
        source_quality_summary = summarize_source_quality(
            [source.source_quality for source in sources if source.source_quality is not None]
        )
        signals = [
            _signal_from_hit(
                hit,
                source=source,
                signal_text=str(hit.get("signal") or why_now),
            )
            for hit, source in items
        ]
        missing_evidence: list[str] = []
        contradictions: list[str] = []
        weak_evidence_reasons: list[str] = []
        recommended_next_actions: list[str] = []
        for hit, source in items:
            missing_evidence.extend(str(item) for item in hit.get("missing_evidence", []))
            contradictions.extend(str(item) for item in hit.get("contradictions", []))
            weak_evidence_reasons.extend(str(item) for item in hit.get("weak_evidence_reasons", []))
            recommended_next_actions.extend(
                str(item)
                for item in (
                    hit.get("recommended_next_actions") or hit.get("recommended_follow_up") or []
                )
            )
            if source.source_quality and source.source_quality.overall_score < 50:
                weak_evidence_reasons.append(
                    f"{source.title} has low source quality "
                    f"({source.source_quality.overall_score}/100)."
                )
        stale_count = sum(1 for signal in signals if signal.freshness == "stale")
        if stale_count:
            missing_evidence.append("Refresh stale opportunity signals before outreach.")
        if category in {"search", "unknown"}:
            missing_evidence.append("Replace discovery pointers with primary sources.")
        if source_quality_summary.low_quality_source_count:
            weak_evidence_reasons.append(source_quality_summary.rationale)
        if not recommended_next_actions:
            recommended_next_actions.append(
                f"Validate {category.replace('_', ' ')} signal with a primary source."
            )

        bundles.append(
            OpportunitySourceBundle(
                bundle_id=f"{_normalize_company_key(company_name)}:{category}",
                company_name=company_name,
                source_category=category,  # type: ignore[arg-type]
                summary="; ".join(signal.signal_text for signal in signals),
                sources=sources,
                signals=signals,
                source_quality_summary=source_quality_summary,
                missing_evidence=list(dict.fromkeys(missing_evidence)),
                contradictions=list(dict.fromkeys(contradictions)),
                stale_signal_count=stale_count,
                weak_evidence_reasons=list(dict.fromkeys(weak_evidence_reasons)),
                recommended_next_actions=list(dict.fromkeys(recommended_next_actions)),
            )
        )
    return bundles


def _flatten_bundle_items(
    bundles: list[OpportunitySourceBundle],
    field: str,
) -> list[str]:
    items: list[str] = []
    for bundle in bundles:
        values = getattr(bundle, field)
        if isinstance(values, list):
            items.extend(str(item) for item in values)
    return list(dict.fromkeys(item for item in items if item))


def _source_bundle_quality_notes(bundles: list[OpportunitySourceBundle]) -> list[str]:
    notes: list[str] = []
    for bundle in bundles:
        prefix = f"{bundle.company_name} {bundle.source_category}"
        if bundle.missing_evidence:
            notes.append(f"{prefix}: missing evidence - {'; '.join(bundle.missing_evidence)}")
        if bundle.weak_evidence_reasons:
            notes.append(f"{prefix}: weak evidence - {'; '.join(bundle.weak_evidence_reasons)}")
        if bundle.contradictions:
            notes.append(f"{prefix}: contradictions - {'; '.join(bundle.contradictions)}")
        if bundle.stale_signal_count:
            notes.append(f"{prefix}: {bundle.stale_signal_count} stale signal(s) require refresh.")
    return list(dict.fromkeys(notes))


def _is_role_search(topic: str | None) -> bool:
    lowered = str(topic or "").lower()
    if _is_broad_multilane_request(topic):
        return False
    if _is_company_growth_discovery_request(topic):
        return False
    formal_opportunity_markers = (
        "grant",
        "fellowship",
        "conference",
        "workshop",
        "certification",
        "certificate",
        "accelerator",
        "challenge",
        "request for proposal",
        "rfp",
    )
    explicit_role_markers = (
        " role",
        " roles",
        " job",
        " jobs",
        " hiring",
        " position",
        " positions",
    )
    role_intent_negated = any(
        phrase in lowered
        for phrase in (
            "not a job",
            "not a role",
            "not a job or role",
            "not a role or job",
        )
    )
    if role_intent_negated and any(marker in lowered for marker in formal_opportunity_markers):
        return False
    if any(marker in lowered for marker in formal_opportunity_markers) and not any(
        marker in lowered for marker in explicit_role_markers
    ):
        return False
    professional_development_markers = (
        "workshop",
        "training",
        "certification",
        "certificate",
        "professional development",
        "networking",
        "professional community",
        "professional society",
    )
    if sum(1 for marker in professional_development_markers if marker in lowered) >= 2:
        return False
    return any(marker in lowered for marker in ROLE_SEARCH_MARKERS)


def _is_company_growth_discovery_request(topic: str | None) -> bool:
    if _is_broad_multilane_request(topic):
        return False
    lowered = str(topic or "").lower()
    if not lowered:
        return False
    company_markers = ("companies", "company", "startups", "startup", "vendors", "platforms")
    growth_markers = (
        "funding",
        "raises",
        "raised",
        "partnership",
        "partnerships",
        "launch",
        "launches",
        "launched",
        "hiring",
        "growth",
        "signals",
    )
    return any(marker in lowered for marker in company_markers) and any(
        marker in lowered for marker in growth_markers
    )


def _resolved_search_plan(
    topic: str | None,
    *,
    max_results: int = 5,
    search_plan: OpportunitySearchPlan | None = None,
) -> OpportunitySearchPlan:
    if search_plan is not None:
        return search_plan
    return infer_opportunity_search_plan(topic, desired_count=max_results)


def _plan_is_conference_request(plan: OpportunitySearchPlan | None) -> bool:
    return plan is not None and (
        plan_targets_only(plan, "conference")
        or (
            len(plan.target_entity_types) <= 1
            and plan_has_objective(plan, "presentation_opportunity")
        )
    )


def _plan_is_journal_call_request(plan: OpportunitySearchPlan | None) -> bool:
    return plan is not None and (
        plan_targets_only(plan, "journal_call")
        or (len(plan.target_entity_types) <= 1 and plan_has_objective(plan, "journal_article_call"))
    )


def _plan_is_contract_rfp_request(plan: OpportunitySearchPlan | None) -> bool:
    return plan is not None and (
        plan_targets_only(plan, "contract_rfp")
        or (len(plan.target_entity_types) <= 1 and plan_has_objective(plan, "contract_opportunity"))
    )


def _plan_is_researcher_request(plan: OpportunitySearchPlan | None) -> bool:
    return plan is not None and plan_targets_only(plan, "researcher")


def _plan_is_institute_request(plan: OpportunitySearchPlan | None) -> bool:
    return plan is not None and plan_targets_only(plan, "institute")


def _plan_is_broad_request(plan: OpportunitySearchPlan | None) -> bool:
    return bool(
        plan is not None and not plan.strict_targeting and len(plan.target_entity_types) > 2
    )


def _plan_is_role_request(plan: OpportunitySearchPlan | None) -> bool:
    return plan is not None and plan_targets_only(plan, "role")


def _plan_is_grant_request(plan: OpportunitySearchPlan | None) -> bool:
    return plan is not None and plan_targets_only(plan, "grant_program")


def _plan_is_github_repository_request(plan: OpportunitySearchPlan | None) -> bool:
    return plan is not None and (
        plan_targets_only(plan, "github_repository")
        or (len(plan.target_entity_types) <= 1 and plan_has_objective(plan, "open_source_tooling"))
    )


def _plan_has_lane(plan: OpportunitySearchPlan | None, lane_type: str) -> bool:
    if plan is None:
        return False
    return any(
        (lane.get("lane_type") if isinstance(lane, dict) else getattr(lane, "lane_type", ""))
        == lane_type
        for lane in plan.lanes
    )


def _plan_is_meeting_grant_request(plan: OpportunitySearchPlan | None) -> bool:
    return _plan_has_lane(plan, "meeting_conference") and _plan_has_lane(plan, "grant_funding")


def _plan_is_professional_development_request(
    plan: OpportunitySearchPlan | None,
) -> bool:
    if plan is None:
        return False
    lane_types = {str(lane.lane_type if hasattr(lane, "lane_type") else "") for lane in plan.lanes}
    return lane_types == {
        "workshop_training",
        "certification_professional_development",
        "networking_community",
    }


def _plan_is_industry_services_request(plan: OpportunitySearchPlan | None) -> bool:
    if plan is None:
        return False
    lane_types = {str(lane.lane_type) for lane in plan.lanes}
    return lane_types == {"industry_collaboration", "consulting_advisory"}


def _plan_is_formal_opportunity_request(plan: OpportunitySearchPlan | None) -> bool:
    return (
        plan is not None
        and _plan_has_lane(plan, "grant_funding")
        and _plan_has_lane(plan, "procurement_rfp")
        and (_plan_has_lane(plan, "pilot_partnership") or _plan_has_lane(plan, "proposal_call"))
    )


def _is_institute_discovery_request(topic: str | None) -> bool:
    if _is_broad_multilane_request(topic):
        return False
    lowered = str(topic or "").lower()
    return any(
        marker in lowered
        for marker in (
            "academic institutes",
            "institutes",
            "institute",
            "centers",
            "centres",
            "programs",
            "programmes",
        )
    ) and any(
        marker in lowered
        for marker in ("collaboration", "partnership", "advisory", "implementation")
    )


def _is_researcher_discovery_request(topic: str | None) -> bool:
    if _is_broad_multilane_request(topic):
        return False
    lowered = str(topic or "").lower()
    return any(
        marker in lowered
        for marker in (
            "researchers",
            "principal investigators",
            "investigators",
            "faculty",
            "labs",
            "laboratories",
        )
    ) and any(
        marker in lowered
        for marker in (
            "publications",
            "clinical trials",
            "grants",
            "implementation",
            "collaboration",
        )
    )


def _is_conference_discovery_request(topic: str | None) -> bool:
    lowered = str(topic or "").lower()
    if not lowered:
        return False
    non_conference_lane_markers = (
        "companies",
        "company",
        "researchers",
        "principal investigators",
        "institutes",
        "clinical trials",
        "grants",
        "journal",
        "journals",
        "special issue",
        "call for papers",
        "rfp",
        "rfps",
        "contracts",
        "solicitation",
        "funding",
        "launches",
        "hiring",
    )
    if (
        "across conferences" not in lowered
        and sum(1 for marker in non_conference_lane_markers if marker in lowered) >= 2
    ):
        return False
    conference_markers = (
        "conference",
        "conferences",
        "symposium",
        "symposia",
        "summit",
        "meeting",
        "workshop",
        "presentation",
        "presentations",
        "speaking",
        "speaker",
        "cfp",
        "call for proposals",
        "call for abstracts",
    )
    intent_markers = (
        "presentation",
        "presentations",
        "speaking",
        "speaker",
        "abstract",
        "cfp",
        "call for proposals",
        "call for abstracts",
        "implementation oriented",
        "implementation-oriented",
        "where keystone",
    )
    return any(marker in lowered for marker in conference_markers) and any(
        marker in lowered for marker in intent_markers
    )


def _is_journal_call_discovery_request(topic: str | None) -> bool:
    lowered = str(topic or "").lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in JOURNAL_CALL_MARKERS) and any(
        marker in lowered
        for marker in (
            "call",
            "calls",
            "special issue",
            "article request",
            "manuscript",
            "submission",
            "submit",
        )
    )


def _is_contract_rfp_discovery_request(topic: str | None) -> bool:
    lowered = str(topic or "").lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in CONTRACT_RFP_MARKERS)


def _is_broad_multilane_request(topic: str | None) -> bool:
    lowered = str(topic or "").lower()
    if not lowered:
        return False
    broad_markers = (
        "broad",
        "across",
        "multiquery",
        "multi-query",
        "multiple lanes",
        "all lanes",
    )
    lane_markers = (
        "funding",
        "partnership",
        "partnerships",
        "pilot",
        "pilots",
        "clinical trial",
        "clinical trials",
        "conference",
        "conferences",
        "grant",
        "grants",
        "journal",
        "journals",
        "special issue",
        "call for papers",
        "rfp",
        "rfps",
        "contract",
        "contracts",
        "solicitation",
        "advisory",
        "advisor",
        "roles",
    )
    lane_count = sum(1 for marker in lane_markers if marker in lowered)
    if _is_conference_discovery_request(topic) and not (
        "all lanes" in lowered or "multiple lanes" in lowered or lane_count >= 3
    ):
        return False
    explicit_portfolio_request = any(
        marker in lowered
        for marker in (
            "broad range of current",
            "broad range of opportunities",
            "broad opportunity portfolio",
            "opportunities across all",
        )
    )
    return explicit_portfolio_request or (
        any(marker in lowered for marker in broad_markers) and lane_count >= 2
    )


def _topic_search_context(topic: str | None) -> str:
    core = "behavioral health psychiatry neuroscience clinical AI clinical research operations"
    cleaned = " ".join(str(topic or "").split()).strip()
    if not cleaned:
        return core
    lowered = re.sub(r"[-_/]+", " ", cleaned.lower())
    if "digital health" in lowered and "opportunit" in lowered:
        return (
            "digital health behavioral health clinical AI evidence generation "
            "clinical research operations"
        )
    if len(cleaned.split()) > 18:
        terms: list[str] = []
        keyword_phrases = (
            "digital health",
            "behavioral health",
            "mental health",
            "clinical ai",
            "clinical research",
            "digital biomarkers",
            "neuroinformatics",
            "neuroscience",
            "trial technology",
            "medical director",
            "advisory",
            "consulting",
            "partnership",
            "small business",
            "grant",
            "pilot",
            "rfp",
            "procurement",
        )
        for phrase in keyword_phrases:
            if phrase in lowered:
                terms.append(phrase)
        if terms:
            return " ".join(dict.fromkeys([*terms, core]))
        return core
    return f"{cleaned} {core}"


def _is_broad_digital_health_request(topic: str | None) -> bool:
    lowered = str(topic or "").lower()
    return "digital health" in lowered and any(
        marker in lowered
        for marker in ("opportunit", "good opportunities", "find", "advisory", "consulting")
    )


def _researcher_query_specs() -> list[_OpportunityQuerySpec]:
    return [
        _OpportunityQuerySpec(
            lane="researcher",
            time_window="current",
            query=(
                'site:.edu psychiatry "artificial intelligence" "principal investigator" '
                '("digital mental health" OR "behavioral health")'
            ),
            entity_hint="researcher",
        ),
        _OpportunityQuerySpec(
            lane="researcher",
            time_window="current",
            query=(
                'site:.edu ("digital mental health" OR "behavioral health AI") '
                "faculty researcher psychiatry neuroscience collaboration"
            ),
            entity_hint="researcher",
        ),
        _OpportunityQuerySpec(
            lane="trial",
            time_window="current",
            query=(
                'site:clinicaltrials.gov psychiatry "artificial intelligence" '
                '"principal investigator" recruiting United States'
            ),
            entity_hint="researcher",
        ),
        _OpportunityQuerySpec(
            lane="grant",
            time_window="current",
            query=(
                'site:reporter.nih.gov psychiatry "artificial intelligence" '
                "principal investigator grant digital mental health"
            ),
            entity_hint="researcher",
        ),
        _OpportunityQuerySpec(
            lane="researcher",
            time_window="evergreen",
            query=(
                'site:pubmed.ncbi.nlm.nih.gov ("digital mental health" OR '
                '"behavioral health") "artificial intelligence" psychiatry investigator'
            ),
            entity_hint="researcher",
        ),
    ]


def _broad_multilane_query_specs(context: str) -> list[_OpportunityQuerySpec]:
    return [
        _OpportunityQuerySpec(
            lane="company_growth",
            time_window="recent",
            query=(
                '"behavioral health AI" '
                '("raises" OR "raised" OR "funding round" OR "seed" OR "Series A") '
                "2026"
            ),
            entity_hint="company",
            source="news",
        ),
        _OpportunityQuerySpec(
            lane="company_growth",
            time_window="recent",
            query=(
                '"digital mental health" ("AI" OR "artificial intelligence") '
                '("raises" OR "funding" OR "launches" OR "partners") 2026'
            ),
            entity_hint="company",
            source="news",
        ),
        _OpportunityQuerySpec(
            lane="collaboration",
            time_window="recent",
            query=(
                '"behavioral health" "AI" '
                '("partnership" OR "partners with" OR "pilot" OR "validation study") '
                "2026"
            ),
            entity_hint="company",
            source="news",
        ),
        _OpportunityQuerySpec(
            lane="researcher",
            time_window="current",
            query=(
                f"site:.edu {context} investigator lab collaboration "
                "psychiatry neuroscience United States"
            ),
            entity_hint="researcher",
        ),
        _OpportunityQuerySpec(
            lane="institute",
            time_window="current",
            query=(
                f"site:.edu {context} institute center program digital mental health "
                "clinical AI United States"
            ),
            entity_hint="institute",
        ),
        _OpportunityQuerySpec(
            lane="conference",
            time_window="recent",
            query=f"{context} conference symposium abstract sponsor workshop United States 2026",
            entity_hint="conference",
        ),
        _OpportunityQuerySpec(
            lane="workshop_training",
            time_window="current",
            query=(
                f'{context} (workshop OR training OR facilitation OR "continuing education") '
                "(speaker OR instructor OR participant OR application) remote 2026"
            ),
            entity_hint="conference",
        ),
        _OpportunityQuerySpec(
            lane="certification_professional_development",
            time_window="current",
            query=(
                f"{context} (certification OR certificate OR fellowship OR "
                '"professional development") (online OR remote OR virtual) application 2026'
            ),
            entity_hint="institute",
        ),
        _OpportunityQuerySpec(
            lane="networking_community",
            time_window="current",
            query=(
                f'{context} (networking OR community OR consortium OR "professional society") '
                "(virtual OR remote OR online OR membership) 2026"
            ),
            entity_hint="conference",
        ),
        _OpportunityQuerySpec(
            lane="consulting_advisory",
            time_window="recent",
            query=(
                f'{context} (consultant OR advisor OR fractional OR "advisory board" OR '
                "facilitator) (remote OR virtual OR contract) 2026"
            ),
            entity_hint="company",
        ),
        _OpportunityQuerySpec(
            lane="grant",
            time_window="current",
            query=f"site:reporter.nih.gov {context} NIH SBIR grant psychiatry neuroscience",
            entity_hint="grant_program",
        ),
        _OpportunityQuerySpec(
            lane="contract_rfp",
            time_window="current",
            query=(
                f'site:sam.gov {context} ("RFP" OR solicitation OR "sources sought") '
                "behavioral health"
            ),
            entity_hint="contract_rfp",
        ),
        _OpportunityQuerySpec(
            lane="trial",
            time_window="recent",
            query=f"site:clinicaltrials.gov {context} study sponsor recruiting United States 2026",
            entity_hint="trial",
        ),
    ]


def _build_live_query_specs(
    topic: str | None = None,
    *,
    search_plan: OpportunitySearchPlan | None = None,
) -> list[_OpportunityQuerySpec]:
    plan = _resolved_search_plan(topic, search_plan=search_plan)
    context = _topic_search_context(topic)
    if _plan_is_github_repository_request(plan):
        specs = [
            _OpportunityQuerySpec(
                lane="github_repository",
                time_window="current",
                query=(
                    f"{context} in:name,description,topics,readme "
                    "stars:>=100 forks:>=20 pushed:>=2025-01-01 archived:false"
                ),
                entity_hint="github_repository",
                source="github",
            ),
            _OpportunityQuerySpec(
                lane="github_repository",
                time_window="current",
                query=(
                    "agents sdk openai slack google drive sheets automation "
                    "in:name,description,topics,readme stars:>=50 pushed:>=2025-01-01 "
                    "archived:false language:Python"
                ),
                entity_hint="github_repository",
                source="github",
            ),
            _OpportunityQuerySpec(
                lane="github_repository",
                time_window="current",
                query=(
                    "data analysis workflow agents business intelligence "
                    "in:name,description,topics,readme stars:>=100 pushed:>=2025-01-01 "
                    "archived:false"
                ),
                entity_hint="github_repository",
                source="github",
            ),
            _OpportunityQuerySpec(
                lane="github_repository",
                time_window="current",
                query=(
                    "slack bot google workspace automation python "
                    "in:name,description,topics,readme stars:>=50 pushed:>=2025-01-01 "
                    "archived:false"
                ),
                entity_hint="github_repository",
                source="github",
            ),
        ]
    elif _plan_is_professional_development_request(plan):
        specs = [
            _OpportunityQuerySpec(
                lane="workshop_training",
                time_window="current",
                query=(
                    '("clinical AI" OR "psychiatry AI" OR neuroinformatics) '
                    "(workshop OR training) (virtual OR online) "
                    "(registration OR register) 2026"
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="certification_professional_development",
                time_window="current",
                query=(
                    '("AI in healthcare" OR "clinical AI" OR neuroinformatics) '
                    '(certificate OR certification OR "professional development") '
                    "(online OR virtual) (enroll OR registration)"
                ),
                entity_hint="institute",
            ),
            _OpportunityQuerySpec(
                lane="networking_community",
                time_window="current",
                query=(
                    '("clinical AI" OR psychiatry OR neuroinformatics) '
                    '("professional society" OR consortium OR community OR networking) '
                    "(virtual OR online OR membership)"
                ),
                entity_hint="conference",
            ),
        ]
    elif _plan_is_meeting_grant_request(plan):
        specs = [
            _OpportunityQuerySpec(
                lane="conference",
                time_window="current",
                query=(
                    '"neuroinformatics" OR "clinical AI" "conference" 2026 '
                    '("call for abstracts" OR "call for proposals" OR "speaker proposal")'
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="conference",
                time_window="current",
                query=(
                    '("digital mental health" OR psychiatry OR neuroscience) '
                    '("conference" OR "symposium" OR "workshop") 2026 '
                    '("call for abstracts" OR "speaker" OR "submission deadline")'
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="conference",
                time_window="current",
                query=(
                    'site:.org ("behavioral health" OR "mental health" OR psychiatry) '
                    '("AI" OR "artificial intelligence") 2026 '
                    '("call for speakers" OR "call for abstracts" OR "agenda")'
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    'site:grants.gov ("mental health" OR psychiatry OR neuroscience) '
                    '("artificial intelligence" OR informatics OR "data science") '
                    '("funding opportunity" OR NOFO OR "posted")'
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    "site:grants.nih.gov (NIMH OR NIH) "
                    '("digital mental health" OR neuroinformatics OR neuroscience) '
                    '("funding opportunity" OR NOFO OR "notice of funding")'
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    'site:reporter.nih.gov ("mental health" OR neuroscience) '
                    '("artificial intelligence" OR informatics) grant'
                ),
                entity_hint="grant_program",
            ),
        ]
    elif _plan_is_researcher_request(plan) or _is_researcher_discovery_request(topic):
        specs = _researcher_query_specs()
    elif _plan_is_broad_request(plan) or _is_broad_multilane_request(topic):
        specs = _broad_multilane_query_specs(context)
    elif _plan_is_grant_request(plan):
        specs = [
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    'site:grants.gov ("mental health" OR "behavioral health" OR psychiatry) '
                    '("artificial intelligence" OR AI OR analytics OR "digital health") '
                    '("funding opportunity" OR NOFO OR "closing date")'
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    'site:grants.nih.gov (NIMH OR NIH) ("digital mental health" OR '
                    '"behavioral health" OR psychiatry) (NOFO OR FOA OR RFA OR SBIR OR STTR)'
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    'site:sbir.gov ("mental health" OR "behavioral health" OR psychiatry) '
                    '(AI OR "artificial intelligence" OR analytics) (SBIR OR STTR)'
                ),
                entity_hint="grant_program",
            ),
        ]
    elif _plan_is_formal_opportunity_request(plan):
        specs = [
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    'site:grants.gov ("mental health" OR "behavioral health" OR psychiatry) '
                    '("artificial intelligence" OR AI OR analytics OR "digital health") '
                    '("funding opportunity" OR NOFO OR "posted" OR "closing date")'
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    "site:grants.nih.gov (NIMH OR NIH) "
                    '("digital mental health" OR "behavioral health" OR psychiatry) '
                    '("artificial intelligence" OR informatics OR "data science") '
                    '("notice of funding" OR NOFO OR FOA OR RFA)'
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    'site:sbir.gov ("mental health" OR "behavioral health" OR psychiatry) '
                    '("AI" OR "artificial intelligence" OR analytics) (SBIR OR STTR)'
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=(
                    'site:sam.gov ("mental health" OR "behavioral health") '
                    '("AI" OR "artificial intelligence" OR analytics OR evaluation) '
                    '("sources sought" OR solicitation OR RFP OR RFI)'
                ),
                entity_hint="contract_rfp",
            ),
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=(
                    'site:.gov ("behavioral health" OR "mental health") '
                    '("request for proposals" OR RFP OR procurement OR solicitation) '
                    '("vendor" OR contractor OR evaluator OR implementation)'
                ),
                entity_hint="contract_rfp",
            ),
            _OpportunityQuerySpec(
                lane="collaboration",
                time_window="current",
                query=(
                    '("behavioral health" OR "mental health") '
                    '("AI" OR "artificial intelligence" OR analytics OR "digital health") '
                    '("pilot program" OR "innovation challenge" OR accelerator OR '
                    '"implementation partner")'
                ),
                entity_hint="institute",
            ),
            _OpportunityQuerySpec(
                lane="collaboration",
                time_window="current",
                query=(
                    'site:.edu ("mental health" OR psychiatry OR "behavioral health") '
                    '("AI" OR "digital health") ("industry partner" OR "partner with us" OR '
                    '"pilot" OR collaboration)'
                ),
                entity_hint="institute",
            ),
            _OpportunityQuerySpec(
                lane="conference",
                time_window="current",
                query=(
                    '("behavioral health" OR "mental health" OR psychiatry) '
                    '("AI" OR "artificial intelligence" OR "digital health") '
                    '("call for proposals" OR CFP OR "call for abstracts" OR '
                    '"call for speakers") 2026'
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="journal_call",
                time_window="current",
                query=(
                    '("mental health" OR psychiatry OR "behavioral health") '
                    '("AI" OR "artificial intelligence" OR "digital health") '
                    '("call for papers" OR "special issue" OR "call for manuscripts")'
                ),
                entity_hint="journal_call",
            ),
        ]
    elif _plan_is_role_request(plan) or _is_role_search(topic):
        specs = [
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=f"{context} remote United States careers hiring posted recently",
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="current",
                query=(
                    f"{context} clinical operations evidence generation job United States 2026 2025"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=(
                    f"site:clinicaltrials.gov {context} sponsor hiring "
                    "clinical operations United States"
                ),
                entity_hint="trial",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="current",
                query=(
                    f"{context} medical director head of product research "
                    "strategy remote United States"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=(
                    f"site:jobs.ashbyhq.com {context} clinical strategy advisor "
                    "remote United States"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=(
                    f"site:boards.greenhouse.io {context} clinical research "
                    "advisor remote United States"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=(
                    f"site:jobs.lever.co {context} clinical AI product strategy "
                    "remote United States"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=(
                    f"site:linkedin.com/jobs {context} medical director clinical AI "
                    "remote United States"
                ),
                entity_hint="company",
            ),
        ]
    elif _plan_is_institute_request(plan) or _is_institute_discovery_request(topic):
        specs = [
            _OpportunityQuerySpec(
                lane="institute",
                time_window="current",
                query=(
                    'site:.edu ("digital mental health" OR "behavioral health AI" OR '
                    '"psychiatry AI") ("center" OR "institute" OR "program") '
                    '("collaboration" OR "implementation" OR "partnership") United States'
                ),
                entity_hint="institute",
            ),
            _OpportunityQuerySpec(
                lane="institute",
                time_window="current",
                query=(
                    'site:.edu ("clinical AI" OR neuroinformatics) '
                    '("mental health" OR psychiatry OR neuroscience) '
                    '("center" OR "institute" OR "program") United States'
                ),
                entity_hint="institute",
            ),
            _OpportunityQuerySpec(
                lane="collaboration",
                time_window="recent",
                query=(
                    '"digital mental health" "AI" university '
                    '("partnership" OR "pilot" OR "implementation" OR "collaboration") 2026'
                ),
                entity_hint="institute",
                source="news",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    'site:reporter.nih.gov ("digital mental health" OR "behavioral health") '
                    '("artificial intelligence" OR AI) university psychiatry'
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="trial",
                time_window="current",
                query=(
                    'site:clinicaltrials.gov ("digital mental health" OR psychiatry) '
                    '("artificial intelligence" OR AI) university sponsor recruiting'
                ),
                entity_hint="trial",
            ),
        ]
    elif _plan_is_researcher_request(plan) or _is_researcher_discovery_request(topic):
        specs = [
            _OpportunityQuerySpec(
                lane="researcher",
                time_window="current",
                query=(
                    'site:.edu psychiatry "artificial intelligence" "principal investigator" '
                    '("digital mental health" OR "behavioral health")'
                ),
                entity_hint="researcher",
            ),
            _OpportunityQuerySpec(
                lane="researcher",
                time_window="current",
                query=(
                    'site:.edu ("digital mental health" OR "behavioral health AI") '
                    "faculty researcher psychiatry neuroscience collaboration"
                ),
                entity_hint="researcher",
            ),
            _OpportunityQuerySpec(
                lane="trial",
                time_window="current",
                query=(
                    'site:clinicaltrials.gov psychiatry "artificial intelligence" '
                    '"principal investigator" recruiting United States'
                ),
                entity_hint="researcher",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    'site:reporter.nih.gov psychiatry "artificial intelligence" '
                    "principal investigator grant digital mental health"
                ),
                entity_hint="researcher",
            ),
            _OpportunityQuerySpec(
                lane="researcher",
                time_window="evergreen",
                query=(
                    'site:pubmed.ncbi.nlm.nih.gov ("digital mental health" OR '
                    '"behavioral health") "artificial intelligence" psychiatry investigator'
                ),
                entity_hint="researcher",
            ),
        ]
    elif _plan_is_journal_call_request(plan) or _is_journal_call_discovery_request(topic):
        specs = [
            _OpportunityQuerySpec(
                lane="journal_call",
                time_window="current",
                query=(
                    f'{context} ("call for papers" OR "call for manuscripts" OR '
                    '"special issue") psychiatry "digital mental health"'
                ),
                entity_hint="journal_call",
            ),
            _OpportunityQuerySpec(
                lane="journal_call",
                time_window="current",
                query=(
                    '"mental health" "artificial intelligence" "special issue" '
                    '("submit" OR "submission" OR "call for papers")'
                ),
                entity_hint="journal_call",
            ),
            _OpportunityQuerySpec(
                lane="journal_call",
                time_window="current",
                query=(
                    '"digital mental health" journal "call for papers" '
                    "(psychiatry OR behavioral health OR implementation)"
                ),
                entity_hint="journal_call",
            ),
            _OpportunityQuerySpec(
                lane="journal_call",
                time_window="strategic",
                query=(
                    'site:frontiersin.org "digital mental health" "research topic" '
                    '(AI OR "artificial intelligence")'
                ),
                entity_hint="journal_call",
            ),
            _OpportunityQuerySpec(
                lane="journal_call",
                time_window="strategic",
                query=(
                    'site:biomedcentral.com "call for papers" '
                    '("mental health" OR psychiatry) ("AI" OR "digital")'
                ),
                entity_hint="journal_call",
            ),
        ]
    elif _plan_is_contract_rfp_request(plan) or _is_contract_rfp_discovery_request(topic):
        specs = [
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=(
                    'site:sam.gov ("behavioral health" OR "mental health") '
                    '("artificial intelligence" OR AI OR analytics) '
                    '(solicitation OR "sources sought" OR RFP)'
                ),
                entity_hint="contract_rfp",
            ),
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=(
                    'site:sam.gov (psychiatry OR neuroscience OR "digital mental health") '
                    '("request for proposal" OR solicitation OR contract)'
                ),
                entity_hint="contract_rfp",
            ),
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=(
                    'site:.gov ("behavioral health" OR "mental health") '
                    '("RFP" OR "request for proposals" OR procurement) '
                    '("AI" OR analytics OR evaluation)'
                ),
                entity_hint="contract_rfp",
            ),
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=(
                    '"behavioral health" "request for proposals" '
                    '("evaluation" OR "clinical AI" OR analytics) United States'
                ),
                entity_hint="contract_rfp",
            ),
        ]
    elif _plan_is_industry_services_request(plan):
        specs = [
            _OpportunityQuerySpec(
                lane="collaboration",
                time_window="current",
                query=(
                    f'{context} ("industry collaboration" OR pilot OR "sponsored research") '
                    '("external partners" OR application OR "partner with us") 2026'
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="consulting_advisory",
                time_window="current",
                query=(
                    f'{context} (consultant OR "advisory board" OR facilitator OR fractional) '
                    '(application OR opening OR contract OR "express interest") 2026'
                ),
                entity_hint="company",
            ),
        ]
    elif _plan_is_conference_request(plan) or _is_conference_discovery_request(topic):
        specs = [
            _OpportunityQuerySpec(
                lane="conference",
                time_window="recent",
                query=(
                    '"behavioral health" "AI" conference 2026 '
                    '("call for speakers" OR "call for proposals" OR "abstract submission")'
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="conference",
                time_window="recent",
                query=(
                    '"mental health" "artificial intelligence" conference 2026 '
                    "(presentation OR speaker OR symposium OR workshop)"
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="conference",
                time_window="recent",
                query=(
                    '"psychiatry" "AI" conference 2026 '
                    '(presentation OR "call for abstracts" OR workshop)'
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="conference",
                time_window="recent",
                query=(
                    '"digital mental health" conference 2026 '
                    "(speaker OR presentation OR symposium OR workshop)"
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="conference",
                time_window="recent",
                query=(
                    '"clinical AI" "behavioral health" summit 2026 '
                    "(speaker OR presentation OR workshop)"
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="conference",
                time_window="current",
                query=(
                    'site:.org ("behavioral health" OR psychiatry OR "mental health") '
                    '("AI" OR "artificial intelligence") conference 2026 speaker'
                ),
                entity_hint="conference",
            ),
        ]
    elif _is_company_growth_discovery_request(topic) or _plan_is_strict_company_request(plan):
        specs = [
            _OpportunityQuerySpec(
                lane="company_growth",
                time_window="recent",
                query=(
                    '"behavioral health AI" '
                    '("raises" OR "raised" OR "funding round" OR "seed" OR "Series A") '
                    "2026"
                ),
                entity_hint="company",
                source="news",
            ),
            _OpportunityQuerySpec(
                lane="company_growth",
                time_window="recent",
                query=(
                    '"digital mental health" ("AI" OR "artificial intelligence") '
                    '("raises" OR "funding" OR "launches" OR "partners") 2026'
                ),
                entity_hint="company",
                source="news",
            ),
            _OpportunityQuerySpec(
                lane="collaboration",
                time_window="recent",
                query=(
                    '"behavioral health" "AI" '
                    '("partnership" OR "partners with" OR "pilot" OR "validation study") '
                    "2026"
                ),
                entity_hint="company",
                source="news",
            ),
            _OpportunityQuerySpec(
                lane="company_growth",
                time_window="recent",
                query=(
                    'site:businesswire.com ("behavioral health" OR "mental health") '
                    '("AI" OR "artificial intelligence") '
                    '("raises" OR "partners" OR "launches") 2026'
                ),
                entity_hint="company",
                source="news",
            ),
            _OpportunityQuerySpec(
                lane="company_growth",
                time_window="recent",
                query=(
                    'site:prnewswire.com ("behavioral health" OR "mental health") '
                    '("AI" OR "artificial intelligence") '
                    '("funding" OR "partnership" OR "launch") 2026'
                ),
                entity_hint="company",
                source="news",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=(
                    '"behavioral health" "AI" company '
                    '("hiring" OR "careers" OR "clinical strategy" OR "medical director") '
                    "United States remote 2026"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="collaboration",
                time_window="current",
                query=(
                    f"{context} company clinical AI evidence generation implementation "
                    "partnership United States 2026"
                ),
                entity_hint="company",
            ),
        ]
    else:
        specs = [
            *(
                [
                    _OpportunityQuerySpec(
                        lane="company_growth",
                        time_window="recent",
                        query=(
                            '"digital health" "behavioral health" '
                            '("announces" OR "raises" OR "funding" OR "partnership") '
                            "2026"
                        ),
                        entity_hint="company",
                    ),
                    _OpportunityQuerySpec(
                        lane="collaboration",
                        time_window="recent",
                        query=(
                            '"clinical AI" "behavioral health" '
                            '("pilot" OR "partnership" OR "validation study") 2026'
                        ),
                        entity_hint="company",
                    ),
                    _OpportunityQuerySpec(
                        lane="company_growth",
                        time_window="recent",
                        query=(
                            'site:businesswire.com ("digital health" OR "mental health") '
                            '("raises" OR "partners" OR "launches") 2026'
                        ),
                        entity_hint="company",
                    ),
                    _OpportunityQuerySpec(
                        lane="company_growth",
                        time_window="recent",
                        query=(
                            'site:prnewswire.com ("behavioral health" OR "digital health") '
                            '("partnership" OR "funding" OR "validation") 2026'
                        ),
                        entity_hint="company",
                    ),
                ]
                if _is_broad_digital_health_request(topic)
                else []
            ),
            *(
                [
                    _OpportunityQuerySpec(
                        lane="company_growth",
                        time_window="recent",
                        query=(
                            '"behavioral health AI" '
                            '("raises" OR "raised" OR "funding round" OR "seed" OR "Series A") '
                            "2026"
                        ),
                        entity_hint="company",
                        source="news",
                    ),
                    _OpportunityQuerySpec(
                        lane="company_growth",
                        time_window="recent",
                        query=(
                            '"digital mental health" ("AI" OR "artificial intelligence") '
                            '("raises" OR "funding" OR "launches" OR "partners") 2026'
                        ),
                        entity_hint="company",
                        source="news",
                    ),
                    _OpportunityQuerySpec(
                        lane="collaboration",
                        time_window="recent",
                        query=(
                            '"behavioral health" "AI" '
                            '("partnership" OR "partners with" OR "pilot" OR "validation study") '
                            "2026"
                        ),
                        entity_hint="company",
                        source="news",
                    ),
                    _OpportunityQuerySpec(
                        lane="company_growth",
                        time_window="recent",
                        query=(
                            'site:businesswire.com ("behavioral health" OR "mental health") '
                            '("AI" OR "artificial intelligence") '
                            '("raises" OR "partners" OR "launches") 2026'
                        ),
                        entity_hint="company",
                        source="news",
                    ),
                    _OpportunityQuerySpec(
                        lane="company_growth",
                        time_window="recent",
                        query=(
                            'site:prnewswire.com ("behavioral health" OR "mental health") '
                            '("AI" OR "artificial intelligence") '
                            '("funding" OR "partnership" OR "launch") 2026'
                        ),
                        entity_hint="company",
                        source="news",
                    ),
                ]
                if _is_broad_multilane_request(topic)
                else []
            ),
            _OpportunityQuerySpec(
                lane="company_growth",
                time_window="recent",
                query=(
                    f"{context} company funding hiring validation partnership "
                    "United States 2026 recent"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="company_growth",
                time_window="current",
                query=(
                    f"{context} company evidence generation clinical research operations "
                    "United States 2025 2026"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="collaboration",
                time_window="recent",
                query=(
                    f"{context} collaboration partnership consortium pilot program "
                    "United States 2026"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="collaboration",
                time_window="recent",
                query=(
                    f"{context} advisory consultant fractional medical director "
                    "clinical AI digital mental health 2026"
                ),
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="researcher",
                time_window="current",
                query=(
                    f"site:.edu {context} investigator lab collaboration "
                    "psychiatry neuroscience United States"
                ),
                entity_hint="researcher",
            ),
            _OpportunityQuerySpec(
                lane="researcher",
                time_window="evergreen",
                query=(
                    f"site:pubmed.ncbi.nlm.nih.gov {context} investigator "
                    "psychiatry neuroscience clinical trial"
                ),
                entity_hint="researcher",
            ),
            _OpportunityQuerySpec(
                lane="institute",
                time_window="current",
                query=(
                    f"site:.edu {context} institute center program digital mental health "
                    "clinical AI United States"
                ),
                entity_hint="institute",
            ),
            _OpportunityQuerySpec(
                lane="conference",
                time_window="recent",
                query=(
                    f"{context} conference symposium abstract sponsor workshop United States 2026"
                ),
                entity_hint="conference",
            ),
            _OpportunityQuerySpec(
                lane="journal_call",
                time_window="current",
                query=(
                    f'{context} ("call for papers" OR "special issue" OR '
                    '"call for manuscripts") psychiatry digital mental health'
                ),
                entity_hint="journal_call",
            ),
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=(
                    f'site:sam.gov {context} ("RFP" OR solicitation OR "sources sought") '
                    "behavioral health"
                ),
                entity_hint="contract_rfp",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(f"site:reporter.nih.gov {context} NIH SBIR grant psychiatry neuroscience"),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="strategic",
                query=(
                    f"site:grants.gov {context} funding opportunity behavioral health neuroscience"
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="trial",
                time_window="recent",
                query=(
                    f"site:clinicaltrials.gov {context} study sponsor recruiting United States 2026"
                ),
                entity_hint="trial",
            ),
            _OpportunityQuerySpec(
                lane="trial",
                time_window="current",
                query=(
                    f"site:clinicaltrials.gov {context} principal investigator "
                    "study site psychiatry United States"
                ),
                entity_hint="trial",
            ),
        ]
    seen: set[str] = set()
    formal_identifiers = _formal_opportunity_identifiers(topic)
    if _plan_is_strict_company_request(plan):
        specs = [
            spec for spec in specs if spec.entity_hint == "company" and spec.lane not in {"role"}
        ]
    elif formal_identifiers:
        # Start explicit-ID requests with the smallest high-precision query. The
        # retrieval loop may broaden later when this produces no usable result.
        specs = [
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=identifier,
                entity_hint="grant_program",
            )
            for identifier in formal_identifiers
        ]
    deduped: list[_OpportunityQuerySpec] = []
    for spec in specs:
        key = spec.query.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(spec)
    return deduped


def _formal_opportunity_identifiers(topic: str | None) -> list[str]:
    """Extract exact formal grant identifiers for high-precision first queries."""

    identifiers = re.findall(
        r"\b(?:PAR|PA|RFA|NOFO|FOA)(?:-[A-Z]{2,6})?-\d{2}-\d{3}\b",
        str(topic or "").upper(),
    )
    return list(dict.fromkeys(identifiers))


def _build_live_queries(topic: str | None = None) -> list[str]:
    return [spec.query for spec in _build_live_query_specs(topic)]


def _build_adaptive_followup_query_specs(
    topic: str | None,
    *,
    existing_specs: list[_OpportunityQuerySpec],
    search_plan: OpportunitySearchPlan | None = None,
) -> list[_OpportunityQuerySpec]:
    """Return a small second-pass ladder when strict search finds no accepted record."""

    context = _topic_search_context(topic)
    lowered = str(topic or "").lower()
    if not lowered.strip():
        return []

    specs: list[_OpportunityQuerySpec] = []
    if _plan_is_professional_development_request(search_plan):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="workshop_training",
                    time_window="current",
                    query=(
                        'site:psychiatry.org (AI OR "digital mental health") '
                        "(course OR workshop OR training) (virtual OR online) 2026"
                    ),
                    entity_hint="conference",
                ),
                _OpportunityQuerySpec(
                    lane="certification_professional_development",
                    time_window="current",
                    query=(
                        'site:ecornell.cornell.edu ("AI in healthcare" OR "healthcare AI") '
                        "(certificate OR certification) (online OR enroll)"
                    ),
                    entity_hint="institute",
                ),
                _OpportunityQuerySpec(
                    lane="networking_community",
                    time_window="current",
                    query=(
                        'site:amia.org ("clinical AI" OR informatics OR psychiatry) '
                        "(community OR working-group OR membership OR networking)"
                    ),
                    entity_hint="conference",
                ),
            ]
        )
    elif _plan_is_grant_request(search_plan):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="grant",
                    time_window="current",
                    query=(
                        'site:grants.gov "behavioral health" (AI OR evaluation) '
                        "(eligible OR eligibility) (open OR deadline) 2026"
                    ),
                    entity_hint="grant_program",
                ),
                _OpportunityQuerySpec(
                    lane="grant",
                    time_window="current",
                    query=(
                        'site:grants.nih.gov "mental health" (SBIR OR STTR OR small business) '
                        "(AI OR evaluation) (open OR due)"
                    ),
                    entity_hint="grant_program",
                ),
            ]
        )
    elif _plan_is_role_request(search_plan):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="role",
                    time_window="recent",
                    query=(
                        f"{context} (consultant OR advisor OR fractional) "
                        "(remote OR virtual) (apply OR hiring) United States"
                    ),
                    entity_hint="company",
                ),
                _OpportunityQuerySpec(
                    lane="role",
                    time_window="recent",
                    query=(f"site:jobs.ashbyhq.com {context} (advisor OR consultant) remote"),
                    entity_hint="company",
                ),
                _OpportunityQuerySpec(
                    lane="role",
                    time_window="recent",
                    query=(f"site:boards.greenhouse.io {context} (advisor OR consultant) remote"),
                    entity_hint="company",
                ),
            ]
        )
    elif "advisory" in lowered or "advisor" in lowered or "consult" in lowered:
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="collaboration",
                    time_window="recent",
                    query=(
                        f'{context} ("advisory board" OR advisor OR consultant OR '
                        '"fractional medical director") behavioral health AI'
                    ),
                    entity_hint="company",
                    source="news",
                ),
                _OpportunityQuerySpec(
                    lane="role",
                    time_window="recent",
                    query=(
                        f'{context} ("clinical advisor" OR "medical advisor" OR '
                        '"clinical strategy") remote United States'
                    ),
                    entity_hint="company",
                ),
                _OpportunityQuerySpec(
                    lane="company_growth",
                    time_window="recent",
                    query=(
                        f'{context} ("raises" OR "partnership" OR "launches") '
                        '"behavioral health" "AI"'
                    ),
                    entity_hint="company",
                    source="news",
                ),
            ]
        )
    if not (
        _plan_is_professional_development_request(search_plan)
        or _plan_is_grant_request(search_plan)
        or _plan_is_role_request(search_plan)
    ):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="collaboration",
                    time_window="current",
                    query=(
                        f"{context} clinical validation pilot partnership "
                        "behavioral health AI United States"
                    ),
                    entity_hint="company",
                ),
                _OpportunityQuerySpec(
                    lane="company_growth",
                    time_window="recent",
                    query=(
                        f"{context} startup funding clinical advisory behavioral health "
                        "mental health AI"
                    ),
                    entity_hint="company",
                    source="news",
                ),
            ]
        )
    seen = {spec.query.strip().lower() for spec in existing_specs}
    followups: list[_OpportunityQuerySpec] = []
    for spec in specs:
        key = spec.query.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        followups.append(spec)
    return followups[:5]


def _build_underfill_followup_query_specs(
    topic: str | None,
    *,
    existing_specs: list[_OpportunityQuerySpec],
    accepted_count: int,
    desired_count: int,
    search_plan: OpportunitySearchPlan | None = None,
) -> list[_OpportunityQuerySpec]:
    """Broaden targeted searches when the request asked for more than survived filters."""

    if accepted_count >= desired_count:
        return []
    lowered = str(topic or "").lower()
    if not lowered.strip():
        return []

    specs: list[_OpportunityQuerySpec] = []
    if _plan_is_institute_request(search_plan) or _is_institute_discovery_request(topic):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="institute",
                    time_window="current",
                    query=(
                        'site:.edu ("AI for mental health" '
                        'OR "artificial intelligence for mental health" '
                        'OR "digital mental health") ("industry affiliate" OR "affiliate program" '
                        'OR "partner with us" OR collaboration)'
                    ),
                    entity_hint="institute",
                ),
                _OpportunityQuerySpec(
                    lane="institute",
                    time_window="current",
                    query=(
                        'site:.edu ("digital mental health" OR "behavioral health technology") '
                        '("center" OR "program" OR initiative) '
                        '("partner" OR contact OR collaborate)'
                    ),
                    entity_hint="institute",
                ),
                _OpportunityQuerySpec(
                    lane="institute",
                    time_window="current",
                    query=(
                        'site:.edu (psychiatry OR "behavioral health") ("AI" OR "clinical AI") '
                        '("special initiative" OR "innovation center" OR "research center") '
                        "(collaboration OR partner)"
                    ),
                    entity_hint="institute",
                ),
                _OpportunityQuerySpec(
                    lane="institute",
                    time_window="current",
                    query=(
                        'site:.edu neuroinformatics ("center" OR "program" OR initiative) '
                        '(psychiatry OR neuroscience OR "mental health") (collaboration OR partner)'
                    ),
                    entity_hint="institute",
                ),
                _OpportunityQuerySpec(
                    lane="institute",
                    time_window="current",
                    query=(
                        'site:.edu ("mental health AI" OR "behavioral health AI") '
                        '("external partners" OR partnership OR "industry")'
                    ),
                    entity_hint="institute",
                ),
            ]
        )
    elif _plan_is_researcher_request(search_plan) or _is_researcher_discovery_request(topic):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="researcher",
                    time_window="current",
                    query=(
                        'site:.edu ("digital mental health" OR "mental health AI") '
                        '("principal investigator" OR investigator) psychiatry'
                    ),
                    entity_hint="researcher",
                ),
                _OpportunityQuerySpec(
                    lane="researcher",
                    time_window="current",
                    query=(
                        'site:.edu ("behavioral health" OR psychiatry) '
                        '("machine learning" OR "artificial intelligence") (PhD OR MD) grant'
                    ),
                    entity_hint="researcher",
                ),
                _OpportunityQuerySpec(
                    lane="researcher",
                    time_window="current",
                    query=(
                        'site:.edu ("digital phenotyping" OR "clinical AI") '
                        '(psychiatry OR neuroscience) ("principal investigator" OR faculty)'
                    ),
                    entity_hint="researcher",
                ),
            ]
        )
    elif _plan_is_industry_services_request(search_plan):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="collaboration",
                    time_window="current",
                    query=(
                        'site:.org ("clinical AI" OR "behavioral health") '
                        '("industry partners" OR "collaboration program" OR "pilot program")'
                    ),
                    entity_hint="institute",
                ),
                _OpportunityQuerySpec(
                    lane="consulting_advisory",
                    time_window="current",
                    query=(
                        '("behavioral health" OR psychiatry) '
                        "(consultant OR advisor OR facilitator) "
                        "(remote OR virtual OR contract) 2026"
                    ),
                    entity_hint="company",
                ),
            ]
        )
    elif _plan_is_conference_request(search_plan) or _is_conference_discovery_request(topic):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="conference",
                    time_window="recent",
                    query=(
                        '"behavioral health in the age of AI" conference 2026 '
                        "(speaker OR presentation OR abstract)"
                    ),
                    entity_hint="conference",
                ),
                _OpportunityQuerySpec(
                    lane="conference",
                    time_window="recent",
                    query=(
                        '"AI in behavioral health" "2026" '
                        "(conference OR summit OR symposium OR workshop)"
                    ),
                    entity_hint="conference",
                ),
                _OpportunityQuerySpec(
                    lane="conference",
                    time_window="recent",
                    query=(
                        '"mental health AI" "call for speakers" '
                        'OR "mental health AI" "call for abstracts"'
                    ),
                    entity_hint="conference",
                ),
            ]
        )
    elif _plan_is_journal_call_request(search_plan) or _is_journal_call_discovery_request(topic):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="journal_call",
                    time_window="current",
                    query=(
                        '"precision psychiatry" "special issue" '
                        '("call for papers" OR "call for manuscripts")'
                    ),
                    entity_hint="journal_call",
                ),
                _OpportunityQuerySpec(
                    lane="journal_call",
                    time_window="current",
                    query=(
                        '"AI for mental health" journal "special issue" '
                        "(submission OR submit OR manuscripts)"
                    ),
                    entity_hint="journal_call",
                ),
            ]
        )
    elif _plan_is_contract_rfp_request(search_plan) or _is_contract_rfp_discovery_request(topic):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="contract_rfp",
                    time_window="current",
                    query=(
                        'site:sam.gov "mental health" ("data analytics" OR "AI") '
                        '("sources sought" OR solicitation)'
                    ),
                    entity_hint="contract_rfp",
                ),
                _OpportunityQuerySpec(
                    lane="contract_rfp",
                    time_window="current",
                    query=(
                        '"behavioral health" ("notice of funding opportunity" OR RFP OR '
                        '"request for proposal") "evaluation"'
                    ),
                    entity_hint="contract_rfp",
                ),
            ]
        )
    elif (
        _plan_is_broad_request(search_plan)
        or _plan_is_company_growth_or_advisory_request(search_plan)
        or _is_broad_multilane_request(topic)
        or _is_company_growth_discovery_request(topic)
    ):
        specs.extend(
            [
                _OpportunityQuerySpec(
                    lane="company_growth",
                    time_window="recent",
                    query=(
                        '"behavioral health technology" ("AI" OR analytics OR platform) '
                        '("raises" OR "funding" OR "partners" OR "launches") 2026'
                    ),
                    entity_hint="company",
                    source="news",
                ),
                _OpportunityQuerySpec(
                    lane="collaboration",
                    time_window="recent",
                    query=(
                        '"mental health" "clinical AI" '
                        '("partnership" OR "pilot" OR "validation" OR "implementation") 2026'
                    ),
                    entity_hint="company",
                    source="news",
                ),
            ]
        )

    seen = {spec.query.strip().lower() for spec in existing_specs}
    followups: list[_OpportunityQuerySpec] = []
    for spec in specs:
        key = spec.query.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        followups.append(spec)
    return followups[:6]


def _plan_is_company_growth_or_advisory_request(plan: OpportunitySearchPlan | None) -> bool:
    """Return whether a structured plan should get broad company-growth fallback lanes."""

    if plan is None:
        return False
    if not any(target in plan.target_entity_types for target in ("company", "startup", "vendor")):
        return False
    return any(
        plan_has_objective(plan, objective)
        for objective in (
            "company_growth",
            "advisory",
            "research_collaboration",
            "institute_partnership",
            "trial_collaboration",
        )
    )


def _build_coverage_followup_query_specs(
    topic: str | None,
    *,
    existing_specs: list[_OpportunityQuerySpec],
    hits: list[dict[str, Any]],
    search_plan: OpportunitySearchPlan | None = None,
) -> tuple[list[_OpportunityQuerySpec], dict[str, Any]]:
    """Target missing high-value source lanes after the first search pass."""

    if not _coverage_followup_enabled():
        return [], {}
    expected_lanes = _expected_source_lanes_for_plan(topic, search_plan=search_plan)
    if not expected_lanes:
        return [], {}
    coverage = assess_source_coverage(hits, expected_lanes=expected_lanes)
    if not coverage.missing_lanes:
        return [], coverage.to_dict()

    context = _topic_search_context(topic)
    seen_queries = {spec.query.strip().lower() for spec in existing_specs}
    followups: list[_OpportunityQuerySpec] = []
    for lane in coverage.missing_lanes:
        for spec in _coverage_specs_for_source_lane(lane, context=context):
            key = spec.query.strip().lower()
            if key in seen_queries:
                continue
            seen_queries.add(key)
            followups.append(spec)
            if len(followups) >= _coverage_followup_query_cap():
                return followups, coverage.to_dict()
    return followups[: _coverage_followup_query_cap()], coverage.to_dict()


def _expected_source_lanes_for_plan(
    topic: str | None,
    *,
    search_plan: OpportunitySearchPlan | None,
) -> tuple[str, ...]:
    entity_lanes = {
        "company": ("company_site", "press_news"),
        "institute": ("people_institutions",),
        "researcher": ("people_institutions", "literature"),
        "conference": ("conference_events",),
        "journal_call": ("literature",),
        "contract_rfp": ("procurement_rfp",),
        "grant_program": ("grants_funding",),
        "trial": ("clinical_trials",),
        "role": ("careers_jobs",),
        "github_repository": ("github_repository",),
    }
    lanes: list[str] = []
    if search_plan is not None:
        for entity_type in search_plan.target_entity_types:
            lanes.extend(entity_lanes.get(entity_type, ()))
    lanes.extend(
        required_source_lanes_for_opportunity(
            request_text=topic or "",
            target_entity_types=search_plan.target_entity_types if search_plan else (),
            objectives=search_plan.objectives if search_plan else (),
        )
    )
    return tuple(dict.fromkeys(lanes))


def _coverage_specs_for_source_lane(
    lane: str,
    *,
    context: str,
) -> tuple[_OpportunityQuerySpec, ...]:
    if lane == "github_repository":
        return (
            _OpportunityQuerySpec(
                lane="github_repository",
                time_window="current",
                query=(
                    f"{context} in:name,description,topics,readme "
                    "stars:>=50 pushed:>=2025-01-01 archived:false"
                ),
                entity_hint="github_repository",
                source="github",
            ),
        )
    if lane == "company_site":
        return (
            _OpportunityQuerySpec(
                lane="company_growth",
                time_window="current",
                query=f"{context} official company website about product platform partners",
                entity_hint="company",
            ),
        )
    if lane == "press_news":
        return (
            _OpportunityQuerySpec(
                lane="company_growth",
                time_window="recent",
                query=(f'site:businesswire.com {context} ("funding" OR "partnership" OR launch)'),
                entity_hint="company",
                source="news",
            ),
            _OpportunityQuerySpec(
                lane="company_growth",
                time_window="recent",
                query=f'site:prnewswire.com {context} ("funding" OR partnership OR launch)',
                entity_hint="company",
                source="news",
            ),
        )
    if lane == "careers_jobs":
        return (
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=f"site:boards.greenhouse.io {context} clinical strategy remote",
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=f"site:jobs.lever.co {context} clinical AI strategy remote",
                entity_hint="company",
            ),
            _OpportunityQuerySpec(
                lane="role",
                time_window="recent",
                query=f"site:jobs.ashbyhq.com {context} clinical research advisor remote",
                entity_hint="company",
            ),
        )
    if lane == "clinical_trials":
        return (
            _OpportunityQuerySpec(
                lane="trial",
                time_window="current",
                query=f"site:clinicaltrials.gov {context} recruiting study sponsor",
                entity_hint="trial",
            ),
        )
    if lane == "grants_funding":
        return (
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=f"site:reporter.nih.gov {context} NIH SBIR grant project",
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=f"site:grants.gov {context} funding opportunity",
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=(
                    f'site:grants.nih.gov {context} ("notice of funding" OR NOFO OR FOA OR RFA)'
                ),
                entity_hint="grant_program",
            ),
            _OpportunityQuerySpec(
                lane="grant",
                time_window="current",
                query=f"site:sbir.gov {context} SBIR STTR funding opportunity",
                entity_hint="grant_program",
            ),
        )
    if lane == "literature":
        return (
            _OpportunityQuerySpec(
                lane="journal_call",
                time_window="evergreen",
                query=f"site:pubmed.ncbi.nlm.nih.gov {context} psychiatry artificial intelligence",
                entity_hint="journal_call",
            ),
        )
    if lane == "procurement_rfp":
        return (
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=f'site:sam.gov {context} ("RFP" OR solicitation OR "sources sought")',
                entity_hint="contract_rfp",
            ),
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=(
                    f'{context} ("request for proposals" OR RFP OR RFI) '
                    '("vendor" OR contractor OR evaluator OR implementation)'
                ),
                entity_hint="contract_rfp",
            ),
        )
    if lane == "conference_events":
        return (
            _OpportunityQuerySpec(
                lane="conference",
                time_window="recent",
                query=f"{context} conference symposium workshop speaker abstract 2026",
                entity_hint="conference",
            ),
        )
    if lane == "people_institutions":
        return (
            _OpportunityQuerySpec(
                lane="researcher",
                time_window="current",
                query=f"site:.edu {context} faculty principal investigator collaboration",
                entity_hint="researcher",
            ),
        )
    if lane == "regulatory":
        return (
            _OpportunityQuerySpec(
                lane="contract_rfp",
                time_window="current",
                query=f"site:fda.gov {context} digital health medical device AI",
                entity_hint="contract_rfp",
            ),
        )
    return ()


def _build_result_deepening_query_specs(
    *,
    topic: str | None,
    existing_specs: list[_OpportunityQuerySpec],
    accepted_count: int,
    desired_count: int,
    search_plan: OpportunitySearchPlan | None = None,
) -> list[_OpportunityQuerySpec]:
    """Request additional result pages for broad searches that under-fill."""

    if accepted_count >= desired_count:
        return []
    if not _result_deepening_allowed(topic, search_plan=search_plan):
        return []
    deepenable_lanes = {
        "company_growth",
        "collaboration",
        "grant",
        "journal_call",
        "contract_rfp",
        "trial",
        "role",
        "institute",
        "researcher",
        "conference",
    }
    specs: list[_OpportunityQuerySpec] = []
    seen: set[tuple[str, int]] = {
        (spec.query.strip().lower(), spec.page) for spec in existing_specs if spec.page is not None
    }
    max_page = _result_deepening_max_page()
    for spec in existing_specs:
        if spec.lane not in deepenable_lanes:
            continue
        page = 2 if spec.page in {None, 1} else spec.page + 1
        if page > max_page:
            continue
        key = (spec.query.strip().lower(), page)
        if key in seen:
            continue
        seen.add(key)
        specs.append(
            _OpportunityQuerySpec(
                lane=spec.lane,
                time_window=spec.time_window,
                query=spec.query,
                entity_hint=spec.entity_hint,
                source=spec.source,
                country=spec.country,
                location=spec.location,
                language=spec.language,
                page=page,
            )
        )
        if len(specs) >= 6:
            break
    return specs


def _result_deepening_allowed(
    topic: str | None,
    *,
    search_plan: OpportunitySearchPlan | None = None,
) -> bool:
    return (
        _plan_is_broad_request(search_plan)
        or _plan_is_conference_request(search_plan)
        or _plan_is_journal_call_request(search_plan)
        or _plan_is_contract_rfp_request(search_plan)
        or _plan_is_institute_request(search_plan)
        or _plan_is_researcher_request(search_plan)
        or _is_broad_multilane_request(topic)
        or _is_company_growth_discovery_request(topic)
        or _is_institute_discovery_request(topic)
        or _is_researcher_discovery_request(topic)
        or _is_conference_discovery_request(topic)
        or _is_journal_call_discovery_request(topic)
        or _is_contract_rfp_discovery_request(topic)
    )


def _coverage_followup_enabled() -> bool:
    return os.getenv("KEYSTONE_ENABLE_SEARCH_COVERAGE_FOLLOWUP", "true").strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }


def _coverage_followup_query_cap() -> int:
    raw = os.getenv("KEYSTONE_SEARCH_COVERAGE_FOLLOWUP_QUERY_CAP", "6").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 6
    return max(0, min(12, value))


def _result_deepening_max_page() -> int:
    raw = os.getenv("KEYSTONE_OPPORTUNITY_DEEPENING_MAX_PAGE", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 3
    return max(2, min(3, value))


def _extract_company_name(title: str, url: str = "") -> str:
    text = " ".join(title.split()).strip()
    for separator in (" | ", " - ", ":"):
        if separator in text:
            left, right = (part.strip() for part in text.split(separator, 1))
            if (
                left.lower().strip(".")
                in {
                    "blog",
                    "blogs",
                    "exclusive",
                    "insights",
                    "news",
                    "press",
                    "press release",
                    "press releases",
                    "resources",
                }
                and right
            ):
                text = right
            else:
                text = left
            break
    selected_vendor_match = re.match(
        r"^.+?\s+selects\s+([A-Z][A-Za-z0-9&.' -]{2,80}?)(?:\s+as\b|\s+for\b|\s+to\b|$)",
        text,
        flags=re.I,
    )
    if selected_vendor_match:
        text = selected_vendor_match.group(1).strip()
    lowered = f" {text.lower()} "
    markers = (
        " raises ",
        " announces ",
        " launches ",
        " partners ",
        " partner ",
        " partners with ",
        " selects ",
        " selected ",
        " teams with ",
        " publishes ",
        " hiring ",
        " hires ",
        " secures ",
        " reports ",
        " begins ",
        " starts ",
        " expands ",
        " unveils ",
        " validation ",
        " clinical trial ",
        " study ",
    )
    cut_points = [lowered.find(marker) for marker in markers if lowered.find(marker) > 0]
    action_cut = bool(cut_points)
    if cut_points:
        text = text[: min(cut_points)].strip()
    if action_cut and " and " in text:
        first, second = (part.strip() for part in text.split(" and ", 1))
        if first and len(second.split()) >= 3:
            text = first
    text = re.sub(r"\b(inc\.?|llc|ltd\.?|corp\.?|corporation|company)\b\.?$", "", text).strip()
    descriptor_match = re.match(
        (
            r"^(?:(?:ai[- ](?:powered|augmented|driven|enabled)|"
            r"technology[- ](?:driven|enabled)|tech[- ]enabled)\s+)?"
            r"(?:(?:behavioral|behavioural|mental|digital|clinical|healthcare|health)\s+)*"
            r"(?:company|provider|platform|startup|vendor)\s+(.+)$"
        ),
        text,
        flags=re.I,
    )
    if descriptor_match:
        candidate = descriptor_match.group(1).strip()
        if 1 <= len(candidate.split()) <= 3:
            text = candidate
    if text:
        return text[:100]
    host = re.sub(r"^https?://", "", url).split("/", 1)[0]
    return host.split(".")[0].replace("-", " ").title() or "Unknown company"


def _title_segment(title: str) -> str:
    text = " ".join(title.split()).strip()
    for separator in (" | ", " - ", ":"):
        if separator in text:
            return text.split(separator, 1)[0].strip()
    return text


def _conference_name_from_title(title: str) -> str:
    segment = _title_segment(title)
    if segment:
        return segment[:140]
    return "Unknown conference"


def _person_name_from_title(title: str) -> str:
    segment = _title_segment(title)
    segment = re.sub(
        r"(?:,\s*|\s+)(?:MD|PhD|ScD|MS|MBA|OTR/L|BCMH|CPRP|FAOTA)\b.*$",
        "",
        segment,
        flags=re.I,
    ).strip()
    match = PERSON_NAME_RE.match(segment)
    if match is not None:
        return match.group(1).strip()[:140]
    return ""


def _looks_like_named_person(title: str, snippet: str) -> bool:
    person_name = _person_name_from_title(title)
    if not person_name:
        return False
    lowered = f" {title.lower()} {snippet.lower()} "
    return any(marker in lowered for marker in PERSON_CONTEXT_MARKERS)


def _entity_kind_from_lane(
    *,
    lane: str,
    title: str,
    url: str,
    snippet: str,
    entity_hint: str,
) -> str:
    haystack = " ".join([title, url, snippet]).lower()
    if lane == "github_repository" or "github.com/" in haystack:
        return "github_repository"
    if lane == "role" or _looks_like_job_posting(title=title, url=url, snippet=snippet):
        return "role"
    if lane == "grant":
        if any(
            marker in haystack
            for marker in ("grants.gov", "reporter.nih.gov", "nih", "sbir", "grant")
        ):
            return "grant_program"
        if _contains_any_marker(haystack, INSTITUTE_MARKERS):
            return "institute"
        return "company"
    if _looks_like_named_person(title, snippet):
        return "researcher"
    if lane == "conference" or _contains_any_marker(haystack, CONFERENCE_MARKERS):
        if _contains_any_marker(haystack, CONFERENCE_MARKERS):
            return "conference"
        if _contains_any_marker(haystack, INSTITUTE_MARKERS):
            return "institute"
        return "company"
    if lane == "journal_call" or _contains_any_marker(haystack, JOURNAL_CALL_MARKERS):
        return "journal_call"
    if lane == "contract_rfp" or _contains_any_marker(haystack, CONTRACT_RFP_MARKERS):
        return "contract_rfp"
    if lane == "researcher":
        if _looks_like_named_person(title, snippet):
            return "researcher"
        if _contains_any_marker(haystack, INSTITUTE_MARKERS):
            return "institute"
        return "company"
    if lane == "institute":
        if ".edu" in haystack and any(
            marker in haystack
            for marker in ("program", "affiliate", "initiative", "special-initiatives")
        ):
            return "institute"
        return "institute" if _contains_any_marker(haystack, INSTITUTE_MARKERS) else "company"
    if lane == "trial":
        if "clinicaltrials.gov" in haystack and any(
            marker in haystack for marker in ("trial site", "study site", "principal investigator")
        ):
            return "trial"
        if "clinicaltrials.gov" in haystack and _looks_like_named_person(title, snippet):
            return "researcher"
        if _contains_any_marker(haystack, INSTITUTE_MARKERS):
            return "institute"
        return "company"
    if _contains_any_marker(haystack, INSTITUTE_MARKERS):
        return "institute"
    return "company"


def _extract_candidate_name(
    *,
    title: str,
    url: str,
    snippet: str,
    entity_kind: str,
) -> str:
    if entity_kind == "researcher":
        person = _person_name_from_title(title)
        if person:
            return person
    if entity_kind == "conference":
        return _conference_name_from_title(title)
    if entity_kind == "github_repository":
        parsed = urlparse(url)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"[:140]
        segment = _title_segment(title)
        return segment[:140] if segment else _extract_company_name(title, url)
    if entity_kind in {"institute", "grant_program", "trial", "journal_call", "contract_rfp"}:
        segment = _title_segment(title)
        if segment:
            return segment[:140]
    return _extract_company_name(title, url)


def _normalize_entity_key(entity_name: str, entity_kind: str) -> str:
    base = _normalize_company_key(entity_name)
    return f"{entity_kind}:{base}" if base else entity_kind


def _looks_us_focused(text: str) -> bool:
    lowered = f" {text.lower()} "
    if any(marker in lowered for marker in USA_TEXT_MARKERS):
        return True
    if re.search(r"\b[A-Z][a-zA-Z .'-]+,\s*[A-Z]{2}\b", text):
        return True
    return False


def _contains_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    for marker in markers:
        if " " in marker:
            if marker in lowered:
                return True
            continue
        if re.search(rf"\b{re.escape(marker)}\b", lowered):
            return True
    return False


def _usa_relevance_from_hit(hit: dict[str, Any]) -> tuple[int, list[str]]:
    title = str(hit.get("source_title") or hit.get("title") or "")
    snippet = str(hit.get("signal") or hit.get("snippet") or "")
    url = str(hit.get("source_url") or hit.get("url") or hit.get("link") or "")
    combined = " ".join([title, snippet, url])
    lowered = combined.lower()
    score = 45
    reasons: list[str] = []
    if any(marker in lowered for marker in USA_DOMAIN_MARKERS):
        score += 25
        reasons.append("Source domain is U.S.-anchored or U.S.-regulatory/research-facing.")
    if _looks_us_focused(combined):
        score += 25
        reasons.append("Result text includes explicit United States or U.S. location evidence.")
    if any(marker in lowered for marker in ("fda", "nih", "cms", "medicare", "medicaid")):
        score += 10
        reasons.append("Result references U.S. regulatory, payer, or funding context.")
    if any(marker in lowered for marker in NON_US_MARKERS) and not _looks_us_focused(combined):
        score -= 15
        reasons.append("No clear U.S. operating signal was found in a non-U.S. context.")
    if "clinicaltrials.gov" in lowered:
        score += 10
        reasons.append("ClinicalTrials.gov commonly signals U.S.-relevant trial activity.")
    return (bounded_score(score), list(dict.fromkeys(reasons)))


def _source_freshness_text(source_hit: dict[str, Any]) -> str:
    published_at = str(source_hit.get("published_at") or source_hit.get("date") or "")
    if published_at.startswith("2026-"):
        return "fresh"
    if published_at.startswith("2025-") or published_at.startswith("2026"):
        return "current"
    if published_at:
        return "stale"
    return "unknown"


def _novelty_score_from_hits(
    *,
    source_hits: list[dict[str, Any]],
    existing_state: ExistingOpportunityState | None,
) -> int:
    categories = {
        _source_category_from_hit(hit)
        for hit in source_hits
        if _source_category_from_hit(hit) != "unknown"
    }
    fresh_count = sum(
        1 for hit in source_hits if _source_freshness_text(hit) in {"fresh", "current"}
    )
    score = 48 + min(20, len(categories) * 8) + min(16, fresh_count * 4)
    if existing_state is not None:
        score -= 18
    return bounded_score(score)


def _usa_relevance_summary(score: int, reasons: list[str]) -> str:
    if score >= 85:
        label = "high"
    elif score >= 65:
        label = "moderate"
    else:
        label = "limited"
    detail = reasons[0] if reasons else "No explicit U.S. operating signal was identified."
    return f"{label} U.S. relevance ({score}/100): {detail}"


def _novelty_summary(
    *,
    novelty_score: int,
    existing_state: ExistingOpportunityState | None,
    search_lanes: list[str],
    time_windows: list[str],
) -> str:
    state_note = (
        f"existing pipeline state {existing_state.status} lowers novelty"
        if existing_state is not None
        else "not present in local pipeline state"
    )
    lane_note = ", ".join(search_lanes) if search_lanes else "default lanes"
    window_note = ", ".join(time_windows) if time_windows else "current window"
    return (
        f"novelty {novelty_score}/100 from lane coverage ({lane_note}), "
        f"time windows ({window_note}), and {state_note}."
    )


def _signals_from_text(text: str) -> list[str]:
    lowered = text.lower()
    signals: list[str] = []
    keyword_map = (
        ("recent funding", ("funding", "series a", "series b", "seed round", "raises")),
        ("hiring clinical", ("hiring clinical", "clinical role", "clinical operations")),
        ("hiring research", ("hiring research", "research scientist", "research role")),
        ("hiring AI", ("hiring ai", "machine learning", "artificial intelligence")),
        ("hiring product", ("hiring product", "product manager", "product role")),
        ("hiring evidence", ("evidence role", "evidence generation", "real-world evidence")),
        ("payer partnership", ("payer partnership", "health plan", "insurer", "payer")),
        ("product launch", ("product launch", "launches", "launched", "unveils")),
        ("validation study", ("validation", "validated", "validation study")),
        ("clinical trial launch", ("clinical trial", "trial launch", "study launch")),
        ("IRB or protocol activity", ("irb", "protocol")),
        ("conference activity", ("conference", "symposium", "poster", "abstract")),
        ("publication or outcomes evidence", ("publication", "published", "outcomes", "evidence")),
        (
            "journal article call",
            ("call for papers", "call for manuscripts", "special issue", "manuscript"),
        ),
        (
            "contract or RFP",
            ("rfp", "request for proposal", "solicitation", "procurement", "sources sought"),
        ),
        ("partnership announcement", ("partnership", "partners with", "collaboration")),
    )
    for signal, keywords in keyword_map:
        if any(keyword in lowered for keyword in keywords):
            signals.append(signal)
    return list(dict.fromkeys(signals)) or ["partnership announcement"]


def _opportunity_type_from_text(text: str) -> OpportunityType:
    lowered = text.lower()
    if "github.com" in lowered or "open source" in lowered or "repository" in lowered:
        return "open-source repository opportunity"
    if "behavioral health" in lowered and "ai" in lowered:
        return "behavioral health AI"
    if "digital mental health" in lowered:
        return "digital mental health"
    if "clinical ai" in lowered or ("clinical" in lowered and "ai" in lowered):
        return "clinical AI"
    if "cro" in lowered or "contract research" in lowered:
        return "CRO"
    if "trial technology" in lowered or ("trial" in lowered and "technology" in lowered):
        return "trial technology"
    if "cns biotech" in lowered or ("cns" in lowered and "biotech" in lowered):
        return "CNS biotech"
    if "neurotechnology" in lowered or "neurotech" in lowered:
        return "neurotechnology"
    if any(marker in lowered for marker in JOURNAL_CALL_MARKERS):
        return "journal article or publication call"
    if any(marker in lowered for marker in CONTRACT_RFP_MARKERS):
        return "contract or RFP opportunity"
    return "grant or collaboration opportunity"


def _opportunity_kind_from_text(
    text: str,
    *,
    lane: str = "",
    entity_kind: str = "",
) -> OpportunityKind:
    """Classify the actionable shape separately from the clinical domain."""

    lowered = f" {lane} {entity_kind} {text} ".lower()
    if any(marker in lowered for marker in ("certification", "certificate program", "credential")):
        return "certification_or_professional_development"
    if any(marker in lowered for marker in ("workshop", "training", "facilitation", "instructor")):
        return "workshop_or_training"
    if any(
        marker in lowered
        for marker in ("networking", "professional society", "community", "consortium")
    ):
        return "networking_or_professional_community"
    if any(marker in lowered for marker in ("fellowship", "grant", "nofo", "sbir", "sttr")):
        return "grant_or_fellowship"
    if any(marker in lowered for marker in CONTRACT_RFP_MARKERS):
        return "contract_or_rfp"
    if any(marker in lowered for marker in JOURNAL_CALL_MARKERS):
        return "publication_call"
    if any(marker in lowered for marker in ("clinical trial", "research study", "study site")):
        return "clinical_trial_or_research"
    if any(marker in lowered for marker in ("accelerator", "hackathon", "challenge")):
        return "accelerator_or_challenge"
    if any(
        marker in lowered
        for marker in ("consultant", "consulting", "advisor", "advisory", "fractional")
    ):
        return "consulting_or_advisory"
    if any(
        marker in lowered
        for marker in ("pilot", "sponsored research", "partnership", "collaboration")
    ):
        return "industry_collaboration_or_pilot"
    if entity_kind == "role" or _looks_like_job_posting(title=text, url="", snippet=text):
        return "role"
    if entity_kind == "conference" or any(
        marker in lowered for marker in ("conference", "summit", "symposium")
    ):
        return "conference"
    if entity_kind in {"company", "institute", "researcher"}:
        return "company_or_partner"
    return "other"


def _looks_like_job_posting(*, title: str, url: str, snippet: str) -> bool:
    url_lower = url.lower()
    title_lower = title.lower()
    snippet_lower = snippet.lower()
    if any(marker in url_lower for marker in JOB_POSTING_URL_MARKERS):
        return True
    if any(marker in title_lower or marker in snippet_lower for marker in JOB_POSTING_TEXT_MARKERS):
        return True
    if "hiring" in title_lower and any(pattern in title_lower for pattern in ROLE_HINT_PATTERNS):
        return True
    if any(pattern in title_lower for pattern in ROLE_HINT_PATTERNS) and any(
        marker in snippet_lower for marker in REMOTE_MARKERS + ONSITE_MARKERS
    ):
        return True
    return False


def _employee_count_range(text: str) -> tuple[int, int] | None:
    match = EMPLOYEE_RANGE_RE.search(text)
    if match is None:
        return None
    try:
        low = int(match.group("low"))
        high = int(match.group("high") or low)
    except ValueError:
        return None
    if "+" in match.group(0):
        high = max(high, low)
    return (low, high)


def _role_title_from_text(title: str, snippet: str) -> str:
    match = ROLE_TITLE_PREFIX_RE.search(title)
    if match is not None:
        candidate = match.group(1)
    else:
        candidate = title
        for separator in (" | ", " - ", ":"):
            if separator in candidate:
                candidate = candidate.split(separator, 1)[-1]
                break
    candidate = re.sub(r"\((?:remote|hybrid|onsite|on-site)[^)]+\)", "", candidate, flags=re.I)
    candidate = re.sub(r"\s+", " ", candidate).strip(" -,:")
    if len(candidate.split()) <= 1 and snippet:
        snippet_match = ROLE_TITLE_PREFIX_RE.search(snippet)
        if snippet_match is not None:
            candidate = snippet_match.group(1).strip(" -,:")
    return candidate[:140]


def _role_location_from_text(title: str, snippet: str) -> tuple[str, bool | None, str]:
    combined = " ".join([title, snippet]).lower()
    if any(marker in combined for marker in REMOTE_MARKERS):
        return ("Remote, United States", True, "United States")
    if any(marker in combined for marker in ONSITE_MARKERS):
        location_match = re.search(
            r"\b(?:in|based in)\s+([A-Z][A-Za-z .,&-]+(?:,\s*[A-Z]{2})?)",
            " ".join([title, snippet]),
        )
        location = location_match.group(1).strip() if location_match else "On-site"
        country = "United States" if "u.s" in combined or "united states" in combined else ""
        return (location, False, country)
    country = "United States" if "u.s" in combined or "united states" in combined else ""
    return ("", None, country)


def _practicing_clinician_requirement(text: str, *, is_role_candidate: bool) -> bool | None:
    lowered = text.lower()
    if any(marker in lowered for marker in CLINICIAN_REQUIRED_MARKERS):
        return True
    if not is_role_candidate:
        return None
    clinician_title_markers = ("psychiatrist", "therapist", "physician", "clinician")
    if any(marker in lowered for marker in clinician_title_markers):
        return None
    return False


def _role_evidence_from_hit(hit: dict[str, Any]) -> _RoleEvidence:
    title = str(hit.get("source_title") or hit.get("title") or "").strip()
    snippet = str(hit.get("signal") or hit.get("snippet") or "").strip()
    url = str(hit.get("source_url") or hit.get("url") or "").strip()
    is_role_candidate = _looks_like_job_posting(title=title, url=url, snippet=snippet) or (
        str(hit.get("source_category") or "") == "job_posting"
    )
    role_title = _role_title_from_text(title, snippet) if is_role_candidate else ""
    role_location, role_remote, role_country = _role_location_from_text(title, snippet)
    combined = " ".join([title, snippet])
    employee_count_range = _employee_count_range(combined)
    unpaid = True if any(marker in combined.lower() for marker in UNPAID_MARKERS) else None
    if unpaid is None and is_role_candidate:
        unpaid = False
    practicing_clinician_required = _practicing_clinician_requirement(
        combined,
        is_role_candidate=is_role_candidate,
    )
    filter_notes: list[str] = []
    if is_role_candidate and role_remote is True:
        filter_notes.append("Remote status verified from the source snippet.")
    if employee_count_range is not None:
        low, high = employee_count_range
        filter_notes.append(
            f"Company size evidence suggests approximately {low}-{high} employees."
            if low != high
            else f"Company size evidence suggests approximately {low} employees."
        )
    if unpaid is False:
        filter_notes.append("The posting reads like a standard paid role; no unpaid marker found.")
    if practicing_clinician_required is False:
        filter_notes.append("No full-time practicing clinician requirement was stated.")
    return _RoleEvidence(
        is_role_candidate=is_role_candidate,
        role_title=role_title,
        role_location=role_location,
        role_remote=role_remote,
        role_country=role_country,
        role_active=True if is_role_candidate else None,
        employee_count_range=employee_count_range,
        unpaid=unpaid,
        practicing_clinician_required=practicing_clinician_required,
        filter_notes=tuple(dict.fromkeys(note for note in filter_notes if note)),
    )


def _parse_hard_filters(topic: str | None) -> _OpportunityHardFilters:
    if not topic:
        return _OpportunityHardFilters()
    lowered = topic.lower()
    role_focused = _is_role_search(topic)
    employee_threshold = None
    match = re.search(r"under\s+(\d+)\s+employees", lowered)
    if match is not None:
        employee_threshold = int(match.group(1))
    return _OpportunityHardFilters(
        exclude_under_employee_count=employee_threshold,
        exclude_onsite=("exclude on-site" in lowered or "exclude onsite" in lowered),
        exclude_unpaid=("exclude unpaid" in lowered or "paid only" in lowered),
        exclude_practicing_clinician=(
            "full-time practicing clinician" in lowered or "practicing clinician" in lowered
        ),
        require_remote=role_focused and bool(re.search(r"\bremote\b", lowered)),
        require_us=role_focused
        and bool(re.search(r"\b(?:u\.?s\.?|united states|us-based|u\.s\.-based)\b", lowered)),
        require_part_time_or_fractional=role_focused
        and bool(re.search(r"\b(?:part[- ]time|fractional|advisory|advisor|contract)\b", lowered)),
        required_role_markers=_requested_role_markers(lowered) if role_focused else (),
    )


def _requested_role_markers(lowered_topic: str) -> tuple[str, ...]:
    markers: list[str] = []
    if "chief medical officer" in lowered_topic or re.search(r"\bcmo\b", lowered_topic):
        markers.extend(["chief medical officer", "cmo"])
    if "medical director" in lowered_topic:
        markers.append("medical director")
    has_clinical_advisor = (
        "clinical advisor" in lowered_topic or "clinical adviser" in lowered_topic
    )
    if has_clinical_advisor:
        markers.extend(["clinical advisor", "clinical adviser"])
    if not has_clinical_advisor and ("advisor" in lowered_topic or "adviser" in lowered_topic):
        markers.extend(["advisor", "adviser"])
    if "fractional" in lowered_topic:
        markers.append("fractional")
    return tuple(dict.fromkeys(markers))


def _apply_hard_filters_to_hits(
    hits: list[dict[str, Any]],
    *,
    topic: str | None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    filters = _parse_hard_filters(topic)
    if not filters.strict_verification:
        return hits, [], []

    filtered: list[dict[str, Any]] = []
    excluded_notes: list[str] = []
    filtered_candidates: list[dict[str, Any]] = []
    excluded_count = 0
    for hit in hits:
        role = _role_evidence_from_hit(hit)
        haystack = " ".join(
            str(value or "")
            for value in (
                hit.get("company_name"),
                hit.get("source_title") or hit.get("title"),
                hit.get("source_url") or hit.get("url"),
                hit.get("signal") or hit.get("snippet"),
                role.role_title,
                role.role_location,
            )
        ).lower()
        enriched = dict(hit)
        enriched.update(
            {
                "role_title": role.role_title,
                "role_location": role.role_location,
                "role_remote": role.role_remote,
                "role_country": role.role_country,
                "role_active": role.role_active,
                "role_filter_notes": list(role.filter_notes),
            }
        )
        reasons: list[str] = []
        if not role.is_role_candidate:
            reasons.append("not a verifiable role posting")
        if filters.exclude_under_employee_count is not None:
            if role.employee_count_range is None:
                reasons.append("company size not verified for the employee-count filter")
            else:
                low, high = role.employee_count_range
                threshold = filters.exclude_under_employee_count
                if threshold is not None and (high < threshold or low < threshold):
                    reasons.append(f"company size may be under {threshold} employees")
        if filters.exclude_onsite and role.role_remote is not True:
            reasons.append("remote status was not verified as remote")
        if filters.require_remote and role.role_remote is not True:
            reasons.append("requested remote status was not verified")
        if (
            filters.require_us
            and role.role_country != "United States"
            and not re.search(r"\b(?:u\.?s\.?|united states|us-based|u\.s\.-based)\b", haystack)
        ):
            reasons.append("requested U.S. location or eligibility was not verified")
        if filters.exclude_unpaid and role.unpaid is not False:
            reasons.append("compensation was not verified as paid")
        if filters.exclude_practicing_clinician and role.practicing_clinician_required is True:
            reasons.append("role requires a full-time practicing clinician")
        if filters.require_part_time_or_fractional and not re.search(
            r"\b(?:part[- ]time|fractional|advisory|advisor|adviser|contract)\b", haystack
        ):
            reasons.append(
                "source lacks requested part-time, fractional, advisory, or contract evidence"
            )
        if filters.required_role_markers and not any(
            marker in haystack for marker in filters.required_role_markers
        ):
            reasons.append(
                "source lacks requested role-title evidence: "
                + ", ".join(filters.required_role_markers[:4])
            )
        if reasons:
            excluded_count += 1
            company_name = str(hit.get("company_name") or "Unknown company")
            excluded_notes.append(
                f"Filtered out {company_name}: {'; '.join(dict.fromkeys(reasons))}."
            )
            filtered_candidates.append(
                {
                    "company_name": company_name,
                    "source_title": str(hit.get("source_title") or hit.get("title") or ""),
                    "source_url": str(hit.get("source_url") or hit.get("url") or ""),
                    "query": str(hit.get("query") or ""),
                    "query_lane": str(hit.get("query_lane") or ""),
                    "role_title": role.role_title,
                    "role_location": role.role_location,
                    "role_remote": role.role_remote,
                    "role_country": role.role_country,
                    "reasons": list(dict.fromkeys(reasons)),
                    "role_filter_notes": list(role.filter_notes),
                }
            )
            continue
        filtered.append(enriched)

    notes = [
        "Applied strict hard filters for role discovery.",
        (
            f"Hard filters removed {excluded_count} candidate(s) before scoring."
            if excluded_count
            else "No candidates were removed by the requested hard filters."
        ),
        *excluded_notes[:5],
    ]
    if not filtered:
        notes.append(
            "No candidates satisfied the requested exclusions with enough evidence to verify "
            "role type, company size, remote status, and compensation."
        )
    return filtered, notes, filtered_candidates


def _filtered_candidate_from_hit(
    hit: dict[str, Any],
    *,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "company_name": str(hit.get("company_name") or "Unknown company"),
        "entity_kind": str(hit.get("entity_kind") or ""),
        "source_category": str(hit.get("source_category") or ""),
        "source_title": str(hit.get("source_title") or hit.get("title") or ""),
        "source_url": str(hit.get("source_url") or hit.get("url") or ""),
        "query": str(hit.get("query") or ""),
        "query_lane": str(hit.get("query_lane") or ""),
        "query_time_window": str(hit.get("query_time_window") or ""),
        "role_title": str(hit.get("role_title") or ""),
        "role_location": str(hit.get("role_location") or ""),
        "role_remote": hit.get("role_remote"),
        "role_country": str(hit.get("role_country") or ""),
        "reasons": list(dict.fromkeys(reason for reason in reasons if reason)),
        "role_filter_notes": [
            str(note) for note in hit.get("role_filter_notes", []) if str(note).strip()
        ],
    }


def _candidate_name_rejection_reason(company_name: str) -> str:
    stripped = " ".join(company_name.split()).strip()
    lowered = stripped.lower().strip(" .:/-_")
    normalized = re.sub(r"[^a-z0-9]+", "", lowered)
    if lowered == "correction" or lowered.startswith(("correction ", "correction:")):
        return "candidate name is a correction notice rather than an organization"
    if lowered in INVALID_CANDIDATE_NAMES or normalized in INVALID_CANDIDATE_NAMES:
        return "candidate name is URL/title noise rather than an organization"
    if lowered.startswith(("http://", "https://")) or "://" in lowered:
        return "candidate name is a URL rather than an organization"
    if re.fullmatch(r"https?", lowered):
        return "candidate name is URL/title noise rather than an organization"
    if re.fullmatch(r"(?:www\.)?[a-z0-9-]+\.[a-z]{2,}(?:/.*)?", lowered):
        return "candidate name is a domain or URL rather than an organization"
    allowed_acronyms = {"APA", "FDA", "NIH", "NIMH", "NCQA", "VA"}
    if re.fullmatch(r"[A-Z]{2,4}", stripped) and stripped not in allowed_acronyms:
        return "candidate name is an unexplained ticker or acronym rather than an organization"
    if len(normalized) < 3:
        return "candidate name is too short to verify as an organization"
    if re.fullmatch(r"[A-Z][a-z]+ \d{1,2}, 20\d{2}", stripped):
        return "candidate name is a dated newsletter or archive title"
    if re.fullmatch(r"H\.\s*Rept\.\s*\d+[-–]\d+", stripped, flags=re.I):
        return "candidate name is a congressional report title rather than an organization"
    if re.search(r"\b20\d{2}\b", stripped) and re.search(
        r"\b(?:volume|number)\b",
        stripped,
        flags=re.I,
    ):
        return "candidate name is a dated newsletter or archive title"
    if "roundup" in lowered or "rundown" in lowered or "funding and news" in lowered:
        return "candidate name is an article, news roundup, or rundown rather than an organization"
    if "trends" in lowered and re.search(r"\b20\d{2}\b|\b(?:vc|funding|market)\b", lowered):
        return "candidate name is a report or trend article rather than an organization"
    if "..." in stripped or lowered.endswith(" ..."):
        return "candidate name is an article headline rather than an organization"
    if stripped.endswith("?") or len(stripped.split()) > 8:
        return "candidate name is an article headline rather than an organization"
    headline_markers = (
        "field misfires",
        "raises red flags",
        "red flags",
        "look inside",
        "how well",
        "what works",
        "what lasts",
        "what matters",
        "using ",
        "making sense",
        "emergency readiness",
        "advancing ",
        "predict ",
        "study tools",
        "study:",
        "guides @",
        "next phase",
    )
    if any(marker in lowered for marker in headline_markers):
        return "candidate name is an article headline rather than an organization"
    if lowered.startswith("from ") and " to " in lowered:
        return "candidate name is an article headline rather than an organization"
    if lowered.startswith("top ") and any(
        marker in lowered for marker in ("startup", "startups", "compan", "vendors", "platforms")
    ):
        return "candidate name is a listicle title rather than an organization"
    if lowered.startswith("new national ") and "study" in lowered:
        return "candidate name is an article headline rather than an organization"
    if lowered.startswith("top ") and "conferences" in lowered:
        return "candidate name is a conference directory article rather than an event"
    return ""


def _title_noise_rejection_reasons(*, title: str, url: str, snippet: str) -> list[str]:
    haystack = f" {title} {url} {snippet} ".lower()
    reasons: list[str] = []
    if any(marker in haystack for marker in TITLE_NOISE_MARKERS):
        reasons.append("source appears to be a PDF, book, thesis, or dissertation title")
    if "raises red flags" in haystack or " red flags" in haystack:
        reasons.append("source appears to be an article headline rather than an opportunity")
    if " study:" in haystack or " national construction safety study" in haystack:
        reasons.append("source appears to be an article headline rather than an opportunity")
    if any(marker in haystack for marker in GENERIC_RESEARCH_PAGE_MARKERS) or any(
        marker in haystack for marker in GENERIC_RESEARCH_HOST_MARKERS
    ):
        reasons.append("source appears to be a generic research page rather than an opportunity")
    if any(marker in haystack for marker in GENERIC_PROGRAM_PAGE_MARKERS):
        reasons.append(
            "source appears to be a generic program page rather than a specific opportunity"
        )
    if re.search(r"\bH\.\s*Rept\.\s*\d+[-–]\d+\b", title, flags=re.I) or (
        "congress.gov" in haystack and "committee-report" in haystack
    ):
        reasons.append("source appears to be a congressional report rather than an opportunity")
    return reasons


def _has_real_organization_evidence(*, company_name: str, entity_kind: str, text: str) -> bool:
    if entity_kind in {"company", "institute"}:
        return True
    if entity_kind in {
        "researcher",
        "conference",
        "journal_call",
        "contract_rfp",
        "grant_program",
        "trial",
    }:
        return False
    if ORGANIZATION_SUFFIX_RE.search(company_name):
        return True
    return bool(ORGANIZATION_SUFFIX_RE.search(text))


def _active_opportunity_reasons(*, title: str, url: str, snippet: str) -> list[str]:
    haystack = f" {title} {url} {snippet} ".lower()
    active_haystack = haystack.replace("raises red flags", "")
    reasons: list[str] = []
    if any(marker in active_haystack for marker in ACTIVE_OPPORTUNITY_MARKERS):
        reasons.append("source text includes active opportunity evidence")
    if _looks_like_job_posting(title=title, url=url, snippet=snippet):
        reasons.append("source is a verifiable role or careers posting")
    return list(dict.fromkeys(reasons))


def _contextual_activity_reasons(
    *,
    entity_kind: str,
    topic: str | None,
    search_plan: OpportunitySearchPlan | None = None,
    title: str,
    url: str,
    snippet: str,
    company_name: str,
) -> list[str]:
    haystack = f" {company_name} {title} {url} {snippet} ".lower()
    reasons: list[str] = []
    if entity_kind == "researcher" and (
        _plan_is_researcher_request(search_plan) or _is_researcher_discovery_request(topic)
    ):
        researcher_activity_markers = (
            "principal investigator",
            "co-investigator",
            "investigator",
            "federally funded",
            "funded research",
            "grant",
            "clinical trial",
            "trial",
            "implementation",
            "project",
            "projects",
            "published",
            "publications",
        )
        if any(marker in haystack for marker in researcher_activity_markers):
            reasons.append("source text includes researcher activity evidence")
    if entity_kind == "institute" and (
        _plan_is_institute_request(search_plan) or _is_institute_discovery_request(topic)
    ):
        institute_activity_markers = (
            "industry affiliate",
            "partnership",
            "partner",
            "collaboration",
            "collaborates",
            "implementation",
            "contact",
        )
        if any(marker in haystack for marker in institute_activity_markers):
            reasons.append("source text includes institute collaboration evidence")
    if entity_kind == "conference" and (
        _plan_is_conference_request(search_plan) or _is_conference_discovery_request(topic)
    ):
        conference_activity_markers = (
            "call for speakers",
            "call for proposals",
            "call for abstracts",
            "abstract submission",
            "presentation",
            "presentations",
            "speaker",
            "speakers",
            "symposium",
            "workshop",
            "conference",
            "summit",
        )
        if any(marker in haystack for marker in conference_activity_markers):
            reasons.append("source text includes conference presentation evidence")
    if entity_kind == "journal_call" and (
        _plan_is_journal_call_request(search_plan) or _is_journal_call_discovery_request(topic)
    ):
        if any(marker in haystack for marker in JOURNAL_CALL_MARKERS) and any(
            marker in haystack
            for marker in ("call", "submission", "submit", "deadline", "special issue")
        ):
            reasons.append("source text includes journal article call evidence")
    if entity_kind == "contract_rfp" and (
        _plan_is_contract_rfp_request(search_plan) or _is_contract_rfp_discovery_request(topic)
    ):
        if any(marker in haystack for marker in CONTRACT_RFP_MARKERS):
            reasons.append("source text includes contract or RFP evidence")
    return reasons


def _parse_source_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text)
        if not match:
            return None
        try:
            return date(*(int(part) for part in match.groups()))
        except ValueError:
            return None


def _stale_or_closed_opportunity_reason(hit: dict[str, Any]) -> str:
    """Reject clearly closed or stale formal opportunities before scoring."""

    title = str(hit.get("source_title") or hit.get("title") or "")
    url = str(hit.get("source_url") or hit.get("url") or "")
    snippet = str(hit.get("signal") or hit.get("snippet") or "")
    haystack = " ".join([title, url, snippet]).lower()
    if any(
        marker in haystack
        for marker in (
            "applications closed",
            "application closed",
            "submissions closed",
            "submission closed",
            "registration closed",
            "opportunity closed",
            "solicitation closed",
            "no longer accepting",
            "deadline has passed",
            "expired opportunity",
            "archived opportunity",
            "cancelled",
            "canceled",
        )
    ):
        return "source explicitly describes a closed, expired, or canceled opportunity"

    deadline = _opportunity_deadline_from_text(haystack)
    if deadline is not None and deadline < date.today():
        return f"source deadline {deadline.isoformat()} has passed"

    entity_kind = str(hit.get("entity_kind") or "").strip().lower()
    if entity_kind not in {"conference", "journal_call", "contract_rfp", "grant_program", "role"}:
        return ""
    published = _parse_source_date(hit.get("published_at") or hit.get("date"))
    if published is None:
        return ""
    current_markers = (
        "applications open",
        "submissions open",
        "registration open",
        "now accepting",
        "rolling deadline",
        "open until",
        "apply by",
    )
    if published < date.today() - timedelta(days=548) and not any(
        marker in haystack for marker in current_markers
    ):
        return (
            "formal opportunity source is older than 18 months without current open-status evidence"
        )
    return ""


def _opportunity_deadline_from_text(text: str) -> date | None:
    patterns = (
        r"(?:deadline|apply by|applications? due|submissions? due|register by)\s*[:\-]?\s*"
        r"([A-Z][a-z]+\s+\d{1,2},?\s+20\d{2})",
        r"(?:deadline|apply by|applications? due|submissions? due|register by)\s*[:\-]?\s*"
        r"(20\d{2}-\d{2}-\d{2})",
    )
    candidate_dates: list[date] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I | re.S):
            value = match.group(1).replace(",", "")
            for fmt in ("%B %d %Y", "%b %d %Y", "%Y-%m-%d"):
                try:
                    candidate_dates.append(datetime.strptime(value, fmt).date())
                    break
                except ValueError:
                    continue
    due_table = re.search(r"application due dates?", text, flags=re.I)
    if due_table is not None:
        table_window = text[due_table.start() : due_table.start() + 2000]
        for match in re.finditer(
            r"([A-Z][a-z]+\s+\d{1,2},?\s+20\d{2})",
            table_window,
            flags=re.I,
        ):
            value = match.group(1).replace(",", "")
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    candidate_dates.append(datetime.strptime(value, fmt).date())
                    break
                except ValueError:
                    continue
    if not candidate_dates:
        return None
    upcoming = [candidate for candidate in candidate_dates if candidate >= date.today()]
    return min(upcoming) if upcoming else max(candidate_dates)


def _opportunity_verification_excerpt(text: str, *, max_chars: int = 5000) -> str:
    """Keep compact decision-critical windows from long formal opportunity pages."""

    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    windows = [cleaned[:800]]
    markers = (
        "application due date",
        "expiration date",
        "applications must be submitted",
        "how to apply",
        "eligible organizations",
        "eligible applicants",
        "for-profit organizations",
        "small businesses",
        "eligibility information",
        "registration open",
        "applications open",
    )
    lowered = cleaned.lower()
    for marker in markers:
        index = lowered.find(marker)
        if index < 0:
            continue
        start = max(0, index - 160)
        windows.append(cleaned[start : index + 1200])
    return "\n\n".join(dict.fromkeys(window.strip() for window in windows if window.strip()))[
        :max_chars
    ]


def _opportunity_detail_fields(hit: dict[str, Any]) -> dict[str, str]:
    text = " ".join(
        str(hit.get(key) or "") for key in ("source_title", "signal", "verified_excerpt")
    )
    lowered = text.lower()
    closed = bool(_stale_or_closed_opportunity_reason(hit))
    active = bool(
        _active_opportunity_reasons(
            title=str(hit.get("source_title") or ""),
            url=str(hit.get("source_url") or ""),
            snippet=text,
        )
    )
    status = "closed_or_expired" if closed else "open" if active else "unknown"
    deadline = _opportunity_deadline_from_text(text)
    access_mode = "unknown"
    if any(
        marker in lowered
        for marker in ("remote", "virtual", "online", "electronic", "electronically")
    ):
        access_mode = "remote_or_virtual"
    elif any(marker in lowered for marker in ("in-person", "in person", "on-site", "onsite")):
        access_mode = "in_person"
    eligibility = _opportunity_eligibility_summary(text)
    return {
        "opportunity_status": status,
        "deadline": deadline.isoformat() if deadline else "",
        "eligibility_summary": eligibility,
        "access_mode": access_mode,
        "application_or_contact_path": str(hit.get("source_url") or ""),
        "detail_verification_status": (
            "page_verified" if str(hit.get("verified_excerpt") or "").strip() else "snippet_only"
        ),
    }


def _opportunity_eligibility_summary(text: str) -> str:
    lowered = text.lower()
    for marker in (
        "for-profit organizations",
        "eligible applicants",
        "eligible organizations",
        "are eligible to apply",
    ):
        index = lowered.find(marker)
        if index >= 0:
            return " ".join(text[index : index + 700].split())[:500]
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if any(
            marker in sentence.lower()
            for marker in ("eligible", "eligibility", "applicant", "for-profit", "vendor")
        ):
            return " ".join(sentence.split())[:500]
    return ""


def _candidate_acceptance_rejection_reasons(
    hit: dict[str, Any],
    *,
    topic: str | None = None,
    search_plan: OpportunitySearchPlan | None = None,
) -> list[str]:
    company_name = str(hit.get("company_name") or "")
    entity_kind = str(hit.get("entity_kind") or "company")
    title = str(hit.get("source_title") or hit.get("title") or "")
    url = str(hit.get("source_url") or hit.get("url") or "")
    snippet = str(hit.get("signal") or hit.get("snippet") or "")
    source_category = str(hit.get("source_category") or "").lower()
    combined = " ".join([company_name, title, url, snippet])
    is_conference_topic = _plan_is_conference_request(
        search_plan
    ) or _is_conference_discovery_request(topic)
    reasons: list[str] = []

    negative_result_reason = _search_result_page_rejection_reason(
        title=title,
        url=url,
        snippet=snippet,
    )
    if negative_result_reason:
        reasons.append(negative_result_reason)
    name_reason = _candidate_name_rejection_reason(company_name)
    formal_opportunity_kinds = {
        "grant_program",
        "journal_call",
        "contract_rfp",
        "trial",
    }
    if name_reason and entity_kind not in {"role", *formal_opportunity_kinds}:
        reasons.append(name_reason)
    if source_category == "publication":
        reasons.append("source is a publication rather than an active opportunity")
    stale_reason = _stale_or_closed_opportunity_reason(hit)
    if stale_reason:
        reasons.append(stale_reason)
    if is_conference_topic and entity_kind == "conference" and "linkedin.com" in url.lower():
        reasons.append("source is a social post rather than a conference or event page")
    title_noise_reasons = _title_noise_rejection_reasons(title=title, url=url, snippet=snippet)
    contextual_activity = _contextual_activity_reasons(
        entity_kind=entity_kind,
        topic=topic,
        search_plan=search_plan,
        title=title,
        url=url,
        snippet=snippet,
        company_name=company_name,
    )
    if contextual_activity and (
        _plan_is_institute_request(search_plan) or _is_institute_discovery_request(topic)
    ):
        title_noise_reasons = [
            reason for reason in title_noise_reasons if "generic program page" not in reason
        ]
    reasons.extend(title_noise_reasons)
    if not _has_real_organization_evidence(
        company_name=company_name,
        entity_kind=entity_kind,
        text=combined,
    ) and not _entity_kind_allowed_by_topic(
        entity_kind=entity_kind,
        topic=topic,
        search_plan=search_plan,
    ):
        reasons.append("candidate is not a real organization")
    if is_conference_topic and entity_kind == "conference":
        active_reasons = contextual_activity
    else:
        active_reasons = [
            *_active_opportunity_reasons(title=title, url=url, snippet=snippet),
            *contextual_activity,
        ]
    if not active_reasons:
        reasons.append("source lacks active opportunity evidence")
    return list(dict.fromkeys(reasons))


def _search_result_page_rejection_reason(*, title: str, url: str, snippet: str) -> str:
    haystack = " ".join([title, url, snippet]).lower()
    if not haystack.strip():
        return ""
    negative_markers = (
        "no results found",
        "no matching results",
        "no indexed results",
        "no opportunities found",
        "your search did not match",
        "returned no results",
        "returned no indexed results",
    )
    if any(marker in haystack for marker in negative_markers):
        return "negative search-results page rather than an actionable opportunity"
    lowered_title = title.strip().lower()
    lowered_url = url.strip().lower()
    if (
        lowered_title in {"search", "search results", "search | simpler.grants.gov"}
        or "/search?" in lowered_url
        or "search-results.html" in lowered_url
    ):
        return "search-results page rather than a specific opportunity record"
    return ""


def _entity_kind_allowed_by_topic(
    *,
    entity_kind: str,
    topic: str | None,
    search_plan: OpportunitySearchPlan | None = None,
) -> bool:
    if search_plan is not None:
        if entity_kind in search_plan.target_entity_types:
            return True
        if search_plan.strict_targeting:
            return False
    lowered = str(topic or "").lower()
    if not lowered:
        return False
    if _is_broad_multilane_request(topic):
        return entity_kind in {
            "researcher",
            "conference",
            "journal_call",
            "contract_rfp",
            "grant_program",
            "trial",
            "institute",
        }
    if entity_kind == "conference":
        return (
            _is_conference_discovery_request(topic)
            or "conference" in lowered
            or "conferences" in lowered
        )
    if entity_kind == "researcher":
        return _is_researcher_discovery_request(topic)
    if entity_kind == "grant_program":
        return "grant" in lowered or "grants" in lowered
    if entity_kind == "journal_call":
        return _is_journal_call_discovery_request(topic)
    if entity_kind == "contract_rfp":
        return _is_contract_rfp_discovery_request(topic)
    if entity_kind == "trial":
        return "clinical trial" in lowered or "clinical trials" in lowered
    if entity_kind == "role":
        return _is_role_search(topic)
    return False


def _plan_is_strict_company_request(search_plan: OpportunitySearchPlan | None) -> bool:
    return bool(
        search_plan is not None
        and search_plan.strict_targeting
        and plan_targets_only(search_plan, "company")
    )


def _strict_company_target_rejection_reason(*, company_name: str, haystack: str) -> str:
    name = " ".join(str(company_name or "").split()).strip()
    lowered_name = name.lower()
    lowered_haystack = str(haystack or "").lower()
    if not lowered_name:
        return "strict company search requires a named company entity"
    non_company_name_patterns = (
        r"\bagenc(?:y|ies)\b",
        r"\badministration\b",
        r"\bbureau\b",
        r"\bdepartment\b",
        r"\bministry\b",
        r"\boffice of\b",
        r"\buniversity\b",
        r"\bcollege\b",
        r"\bschool of\b",
        r"\binstitute\b",
        r"\blab(?:oratory)?\b",
        r"\bprograms?\b",
        r"\bprojects?\b",
        r"\bstud(?:y|ies)\b",
        r"\btrials?\b",
        r"\bgrants?\b",
        r"\binitiative\b",
    )
    if any(re.search(pattern, lowered_name) for pattern in non_company_name_patterns):
        return "strict company search rejected a non-company agency, program, project, or institute"
    if re.fullmatch(r"(?:hhs|nih|nimh|cms|ahrq|fda|va)(?:\s+.+)?", lowered_name):
        return "strict company search rejected a government or agency entity"
    if (
        lowered_name in {"hhs", "nih", "nimh", "cms", "ahrq", "fda", "va"}
        and ".gov" in lowered_haystack
    ):
        return "strict company search rejected a government or agency entity"
    if "breakthrough" in lowered_name and re.search(
        r"\b(?:award winners?|awards program|annual awards)\b", lowered_haystack
    ):
        return "strict company search rejected an awards program or media entity"
    return ""


def _candidate_acceptance_review_reasons(
    hit: dict[str, Any],
    *,
    rejection_reasons: list[str],
) -> list[str]:
    """Preserve borderline active opportunities for review without scoring them."""

    if not rejection_reasons:
        return []
    blocked_noise_markers = (
        "URL/title noise",
        "PDF, book, thesis, or dissertation",
        "generic research page",
        "negative search-results page",
        "closed, expired, or canceled opportunity",
        "older than 18 months",
    )
    if any(
        any(marker in reason for marker in blocked_noise_markers) for reason in rejection_reasons
    ):
        return []
    title = str(hit.get("source_title") or hit.get("title") or "")
    url = str(hit.get("source_url") or hit.get("url") or "")
    snippet = str(hit.get("signal") or hit.get("snippet") or "")
    if not url or url.startswith("search://"):
        return []
    if any(domain in url.lower() for domain in ("instagram.com/", "facebook.com/", "x.com/")):
        return []
    source_category = str(hit.get("source_category") or "").strip().lower()
    entity_kind = str(hit.get("entity_kind") or "").strip().lower()
    lacks_active_status = any(
        "source lacks active opportunity evidence" in reason for reason in rejection_reasons
    )
    formal_review_kinds = {"grant_program", "contract_rfp", "conference", "journal_call", "role"}
    if lacks_active_status and entity_kind not in formal_review_kinds:
        return []
    if not lacks_active_status and not _active_opportunity_reasons(
        title=title,
        url=url,
        snippet=snippet,
    ):
        return []
    reviewable_categories = {
        "news",
        "clinical_trial",
        "grant",
        "journal_call",
        "contract_rfp",
        "conference",
        "job_posting",
        "company_page",
        "search",
    }
    reviewable_entity_kinds = {
        "researcher",
        "conference",
        "journal_call",
        "contract_rfp",
        "grant_program",
        "trial",
        "institute",
        "company",
    }
    if source_category not in reviewable_categories and entity_kind not in reviewable_entity_kinds:
        return []

    review_note = (
        "formal opportunity candidate retained for page-level status, deadline, and "
        "eligibility verification"
        if lacks_active_status
        else "borderline active opportunity preserved for orchestrator or Business Research review"
    )
    return [review_note, *rejection_reasons]


def _apply_candidate_acceptance_filters_to_hits(
    hits: list[dict[str, Any]],
    *,
    topic: str | None = None,
    search_plan: OpportunitySearchPlan | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    filtered_candidates: list[dict[str, Any]] = []
    review_candidates: list[dict[str, Any]] = []
    audit_notes: list[str] = []

    for hit in hits:
        reasons = _candidate_acceptance_rejection_reasons(
            hit,
            topic=topic,
            search_plan=search_plan,
        )
        if not reasons:
            accepted.append(hit)
            continue
        review_reasons = _candidate_acceptance_review_reasons(
            hit,
            rejection_reasons=reasons,
        )
        if review_reasons:
            review_candidates.append(_filtered_candidate_from_hit(hit, reasons=review_reasons))
        filtered_candidates.append(_filtered_candidate_from_hit(hit, reasons=reasons))

    removed_count = len(filtered_candidates)
    if removed_count:
        audit_notes.append(
            f"Candidate acceptance removed {removed_count} noisy or unsupported candidate(s)."
        )
        for candidate in filtered_candidates[:5]:
            audit_notes.append(
                f"Rejected {candidate['company_name']}: {'; '.join(candidate['reasons'])}."
            )
    if hits and not accepted:
        audit_notes.append(
            "No candidates satisfied deterministic acceptance: Scout requires a real "
            "organization plus active opportunity evidence before scoring."
        )
    if review_candidates:
        audit_notes.append(
            f"Preserved {len(review_candidates)} borderline active candidate(s) for "
            "orchestrator or Business Research review."
        )
    return accepted, audit_notes, filtered_candidates, review_candidates


def _topic_relevance_rejection_reasons(
    hit: dict[str, Any],
    *,
    topic: str | None,
    search_plan: OpportunitySearchPlan | None = None,
) -> list[str]:
    lowered_topic = str(topic or "").lower()
    if not lowered_topic and search_plan is None:
        return []
    title = str(hit.get("source_title") or hit.get("title") or "")
    url = str(hit.get("source_url") or hit.get("url") or "")
    snippet = str(hit.get("signal") or hit.get("snippet") or "")
    company_name = str(hit.get("company_name") or "")
    signals = " ".join(str(signal) for signal in hit.get("signals", []) if str(signal).strip())
    source_haystack = " ".join([company_name, title, url, snippet]).lower()
    haystack = " ".join([source_haystack, signals]).lower()
    entity_kind = str(hit.get("entity_kind") or "").strip().lower()
    reasons: list[str] = []
    reasons.extend(
        _explicit_exclusion_rejection_reasons(
            haystack=haystack,
            topic=topic,
        )
    )
    if search_plan is not None and search_plan.strict_targeting:
        if entity_kind not in search_plan.target_entity_types:
            reasons.append("source entity type does not match the structured search plan target")
        if _plan_is_strict_company_request(search_plan):
            company_target_reason = _strict_company_target_rejection_reason(
                company_name=company_name,
                haystack=haystack,
            )
            if company_target_reason:
                reasons.append(company_target_reason)
    else:
        if _is_researcher_discovery_request(topic) and entity_kind != "researcher":
            reasons.append("source is not a researcher or principal investigator required by topic")
        if _is_institute_discovery_request(topic) and entity_kind != "institute":
            reasons.append(
                "source is not an academic institute, center, or program required by topic"
            )
        if _is_conference_discovery_request(topic) and entity_kind != "conference":
            reasons.append(
                "source is not a conference, symposium, workshop, or presentation "
                "opportunity required by topic"
            )
    alternative_domain_request = bool(
        " or " in lowered_topic
        and "psychiatr" in lowered_topic
        and any(
            marker in lowered_topic
            for marker in ("clinical ai", "neuroinformatics", "clinical research")
        )
    )
    if (
        "psychiatr" in lowered_topic
        and not alternative_domain_request
        and not any(marker in haystack for marker in PSYCHIATRY_TOPIC_MARKERS)
    ):
        reasons.append(
            "source lacks direct psychiatry or behavioral-health relevance required by topic"
        )
    requires_behavioral_relevance = _requires_behavioral_health_or_healthcare_ai_relevance(
        topic,
        search_plan=search_plan,
    )
    if requires_behavioral_relevance and _plan_is_strict_company_request(search_plan):
        if not any(marker in source_haystack for marker in PSYCHIATRY_TOPIC_MARKERS):
            reasons.append(
                "source lacks direct behavioral-health or psychiatry relevance required by topic"
            )
    elif (
        requires_behavioral_relevance
        and entity_kind == "grant_program"
        and _has_neuroinformatics_or_neuroscience_relevance(haystack)
    ):
        pass
    elif requires_behavioral_relevance and not (
        _has_behavioral_health_or_adjacent_healthcare_ai_relevance(haystack)
    ):
        reasons.append(
            "source lacks behavioral-health or adjacent healthcare AI relevance required by topic"
        )
    formal_opportunity_kinds = {
        "grant_program",
        "conference",
        "journal_call",
        "contract_rfp",
        "trial",
    }
    if (
        entity_kind not in formal_opportunity_kinds
        and _requires_ai_company_relevance(topic)
        and not _has_explicit_ai_company_relevance(source_haystack)
    ):
        reasons.append(
            "source lacks explicit AI, ML, analytics, automation, or algorithm evidence "
            "required by an AI company topic"
        )
    if _requires_advisory_topic_constraint(topic) and not any(
        marker in haystack for marker in ADVISORY_TOPIC_MARKERS
    ):
        reasons.append(
            "source lacks advisory, consulting, or fractional-role evidence required by topic"
        )
    return reasons


def _explicit_exclusion_rejection_reasons(
    *,
    haystack: str,
    topic: str | None,
) -> list[str]:
    lowered_topic = str(topic or "").lower()
    if "exclude" not in lowered_topic and "excluding" not in lowered_topic:
        return []
    reasons: list[str] = []
    for request_marker, candidate_markers in EXPLICIT_EXCLUSION_MARKERS.items():
        if request_marker not in lowered_topic:
            continue
        if any(marker in haystack for marker in candidate_markers):
            reasons.append(f"source matches explicitly excluded {request_marker} domain")
    return list(dict.fromkeys(reasons))


def _requires_behavioral_health_or_healthcare_ai_relevance(
    topic: str | None,
    *,
    search_plan: OpportunitySearchPlan | None = None,
) -> bool:
    domain_text = " ".join(search_plan.domains if search_plan else ()).lower()
    lowered = " ".join([str(topic or "").lower(), domain_text])
    return any(
        marker in lowered
        for marker in (
            "behavioral health",
            "behavioural health",
            "mental health",
            "digital mental health",
        )
    )


def _has_behavioral_health_or_adjacent_healthcare_ai_relevance(haystack: str) -> bool:
    if any(marker in haystack for marker in PSYCHIATRY_TOPIC_MARKERS):
        return True
    has_adjacent_healthcare = any(marker in haystack for marker in ADJACENT_HEALTHCARE_AI_MARKERS)
    has_ai_signal = any(marker in f" {haystack} " for marker in AI_TOPIC_MARKERS)
    has_clinical_signal = any(
        marker in haystack
        for marker in (
            "clinical",
            "patient",
            "provider",
            "care",
            "trial",
            "evidence",
            "outcomes",
        )
    )
    has_non_clinical_focus = any(marker in haystack for marker in NON_CLINICAL_AI_MARKERS)
    return (
        has_adjacent_healthcare
        and (has_ai_signal or has_clinical_signal)
        and not (has_non_clinical_focus)
    )


def _has_neuroinformatics_or_neuroscience_relevance(haystack: str) -> bool:
    return any(marker in haystack for marker in NEUROINFORMATICS_TOPIC_MARKERS)


def _has_explicit_ai_company_relevance(haystack: str) -> bool:
    padded = f" {haystack} "
    return any(
        marker in padded
        for marker in (
            " ai ",
            " artificial intelligence ",
            " machine learning ",
            " ml ",
            " predictive ",
            " automation ",
            " algorithm ",
            " analytics ",
            " ambient ai ",
            " copilot ",
            " copilots ",
        )
    )


def _requires_ai_company_relevance(topic: str | None) -> bool:
    lowered = str(topic or "").lower()
    if not re.search(r"\bai\b|artificial intelligence", lowered):
        return False
    return any(marker in lowered for marker in ("companies", "company", "startup", "startups"))


def _requires_advisory_topic_constraint(topic: str | None) -> bool:
    lowered = str(topic or "").lower()
    if (
        not lowered
        or _is_broad_multilane_request(topic)
        or _is_company_growth_discovery_request(topic)
        or _allows_partnership_or_advisory_topic(lowered)
    ):
        return False
    advisory_needles = (
        "advisory opportunity",
        "advisory opportunities",
        "advisor opportunity",
        "advisor opportunities",
        "advisory role",
        "advisory roles",
        "advisor role",
        "advisor roles",
        "consulting opportunity",
        "consulting opportunities",
        "fractional medical director",
    )
    return any(needle in lowered for needle in advisory_needles)


def _allows_partnership_or_advisory_topic(lowered_topic: str) -> bool:
    """Return whether partnership evidence can satisfy an advisory-adjacent request."""

    has_partnership = any(
        marker in lowered_topic
        for marker in (
            "partnership",
            "partnerships",
            "partner",
            "pilot",
            "collaboration",
            "implementation",
            "validation",
        )
    )
    has_advisory = any(
        marker in lowered_topic
        for marker in (
            "advisory",
            "advisor",
            "consulting",
            "fractional",
            "medical director",
        )
    )
    return has_partnership and has_advisory


def _apply_topic_relevance_filters_to_hits(
    hits: list[dict[str, Any]],
    *,
    topic: str | None,
    search_plan: OpportunitySearchPlan | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    filtered_candidates: list[dict[str, Any]] = []
    audit_notes: list[str] = []
    for hit in hits:
        reasons = _topic_relevance_rejection_reasons(
            hit,
            topic=topic,
            search_plan=search_plan,
        )
        if not reasons:
            accepted.append(hit)
            continue
        filtered_candidates.append(_filtered_candidate_from_hit(hit, reasons=reasons))
    if filtered_candidates:
        audit_notes.append(
            f"Topic relevance removed {len(filtered_candidates)} adjacent candidate(s)."
        )
        for candidate in filtered_candidates[:5]:
            audit_notes.append(
                f"Rejected {candidate['company_name']}: {'; '.join(candidate['reasons'])}."
            )
    if hits and not accepted:
        audit_notes.append(
            "No candidates satisfied the requested topic constraints strongly enough to score."
        )
    return accepted, audit_notes, filtered_candidates


def _detail_completeness_rejection_reasons(
    hit: dict[str, Any],
    *,
    topic: str | None,
    search_plan: OpportunitySearchPlan | None,
) -> list[str]:
    """Require decision-critical details only after wide candidate discovery."""

    details = _opportunity_detail_fields(hit)
    lowered_topic = str(topic or "").lower()
    entity_kind = str(hit.get("entity_kind") or "").lower()
    requires_current = any(marker in lowered_topic for marker in ("current", "active", "open"))
    professional_development = _plan_is_professional_development_request(search_plan)
    formal_entity = entity_kind in {"grant_program", "contract_rfp", "conference", "journal_call"}
    reasons: list[str] = []
    if (professional_development or (requires_current and formal_entity)) and (
        details["detail_verification_status"] != "page_verified"
    ):
        reasons.append("page-level opportunity details were not verified")
    if requires_current and details["opportunity_status"] != "open":
        reasons.append("current open status was not verified")
    if (
        professional_development
        and "remote" in lowered_topic
        and (details["access_mode"] != "remote_or_virtual")
    ):
        reasons.append("remote or virtual access was not verified")
    if (
        entity_kind == "grant_program"
        and any(
            marker in lowered_topic
            for marker in ("eligibility", "eligible", "small business", "consulting company")
        )
        and not details["eligibility_summary"]
    ):
        reasons.append("applicant eligibility was not verified")
    return reasons


def _apply_detail_completeness_filters_to_hits(
    hits: list[dict[str, Any]],
    *,
    topic: str | None,
    search_plan: OpportunitySearchPlan | None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    notes: list[str] = []
    for hit in hits:
        reasons = _detail_completeness_rejection_reasons(
            hit,
            topic=topic,
            search_plan=search_plan,
        )
        if not reasons:
            accepted.append(hit)
            continue
        filtered.append(_filtered_candidate_from_hit(hit, reasons=reasons))
    if filtered:
        notes.append(
            f"Detail verification withheld {len(filtered)} candidate(s) from final ranking."
        )
        for candidate in filtered[:5]:
            notes.append(
                f"Withheld {candidate['company_name']}: {'; '.join(candidate['reasons'])}."
            )
    return accepted, notes, filtered


def _search_result_to_hit(
    query: str | _OpportunityQuerySpec,
    result: dict[str, Any],
) -> dict[str, Any]:
    query_text = query.query if isinstance(query, _OpportunityQuerySpec) else query
    lane = query.lane if isinstance(query, _OpportunityQuerySpec) else "company_growth"
    time_window = query.time_window if isinstance(query, _OpportunityQuerySpec) else "current"
    entity_hint = query.entity_hint if isinstance(query, _OpportunityQuerySpec) else "company"
    title = str(result.get("title", "")).strip()
    url = str(result.get("url") or result.get("link") or "").strip()
    content = str(result.get("content") or "").strip()
    snippet = str(result.get("snippet", "") or content[:500]).strip()
    combined = " ".join([query_text, title, snippet])
    signals = _signals_from_text(combined)
    source_category = _source_category_from_search_result(title=title, url=url, snippet=snippet)
    source_type = _source_type_for_search_category(source_category, result)
    entity_kind = _entity_kind_from_lane(
        lane=lane,
        title=title,
        url=url,
        snippet=snippet,
        entity_hint=entity_hint,
    )
    entity_name = _extract_candidate_name(
        title=title,
        url=url,
        snippet=snippet,
        entity_kind=entity_kind,
    )
    role = _role_evidence_from_hit(
        {
            "source_title": title,
            "source_url": url,
            "signal": snippet,
            "source_category": source_category,
        }
    )
    usa_relevance_score, usa_relevance_reasons = _usa_relevance_from_hit(
        {
            "source_title": title,
            "source_url": url,
            "signal": snippet,
        }
    )
    return {
        "company_name": entity_name,
        "opportunity_kind": _opportunity_kind_from_text(
            combined,
            lane=lane,
            entity_kind=entity_kind,
        ),
        "opportunity_type": _opportunity_type_from_text(combined),
        "signal": snippet or title or "Search result requires review.",
        "signals": signals,
        "source_title": title or "Search result",
        "source_url": url or "search://result",
        "source_type": source_type,
        "source_category": source_category,
        "published_at": result.get("published_at") or result.get("date"),
        "verified_excerpt": content[:1000],
        "query": query_text,
        "query_lane": lane,
        "query_time_window": time_window,
        "search_lanes": [lane],
        "time_windows": [time_window],
        "entity_kind": entity_kind,
        "canonical_entity_key": _normalize_entity_key(entity_name, entity_kind),
        "usa_relevance_score": usa_relevance_score,
        "usa_relevance_reasons": usa_relevance_reasons,
        "role_title": role.role_title,
        "role_location": role.role_location,
        "role_remote": role.role_remote,
        "role_country": role.role_country,
        "role_active": role.role_active,
        "role_filter_notes": list(role.filter_notes),
    }


def _source_category_from_search_result(*, title: str, url: str, snippet: str) -> str:
    haystack = " ".join([title, url, snippet]).lower()
    if "github.com/" in haystack:
        return "repository"
    if _contains_any_marker(haystack, CONTRACT_RFP_MARKERS):
        return "contract_rfp"
    if _contains_any_marker(haystack, JOURNAL_CALL_MARKERS) and any(
        term in haystack for term in ("call", "submit", "submission", "deadline", "special issue")
    ):
        return "journal_call"
    if "clinicaltrials.gov" in haystack or "clinical trial" in haystack:
        return "clinical_trial"
    if any(
        term in haystack
        for term in (
            "pubmed",
            "pmc.ncbi",
            "ncbi.nlm.nih.gov/pmc",
            "doi.org",
            "science.org/doi",
            "nature.com/articles",
            "publication",
            "journal",
        )
    ):
        return "publication"
    if any(term in haystack for term in ("nih.gov", "reporter.nih.gov", "sbir", "grant")):
        return "grant"
    if any(term in haystack for term in ("conference", "symposium", "poster", "abstract")):
        return "conference"
    if _looks_like_job_posting(title=title, url=url, snippet=snippet):
        return "job_posting"
    if any(term in haystack for term in ("funding", "raises", "seed round", "series a")):
        return "news"
    if any(
        term in haystack
        for term in ("about", "platform", "product page", "/about", "/team", "/research", ".edu/")
    ):
        return "company_page"
    return "search"


def _source_type_for_search_category(category: str, result: dict[str, Any]) -> str:
    explicit = str(result.get("source_type") or "").strip()
    if explicit and explicit not in {"search", "google_search", "metasearch"}:
        return explicit
    if category == "repository":
        return "github"
    if category == "clinical_trial":
        return "clinical_trial"
    if category == "grant":
        return "government"
    if category == "contract_rfp":
        return "government"
    if category == "journal_call":
        return "publication"
    if category == "publication":
        return "publication"
    if category == "conference":
        return "conference"
    if category == "job_posting":
        return "job_posting"
    if category == "company_page":
        return "company_site"
    if category == "news":
        return "news"
    return explicit or "google_search"


def _search_with_provider(
    search_provider: Any,
    query: str | _OpportunityQuerySpec,
    max_results: int,
) -> list[dict[str, Any]]:
    request = _search_request_from_query(query, max_results=max_results)
    structured = getattr(search_provider, "search_structured", None)
    if callable(structured):
        response = structured(request)
    elif hasattr(search_provider, "search_web"):
        response = search_provider.search_web(request.query, num_results=max_results)
    else:
        try:
            response = search_provider.search(request.query, max_results=max_results)
        except TypeError:
            response = search_provider.search(request.query, num_results=max_results)

    if isinstance(response, dict):
        results = response.get("results", [])
    else:
        results = response

    normalized: list[dict[str, Any]] = []
    for result in results or []:
        if isinstance(result, dict):
            normalized.append(result)
            continue
        if hasattr(result, "model_dump"):
            dumped = result.model_dump()
            source = str(dumped.get("source") or "search")
            source_type = "metasearch" if source == "searxng" else "google_search"
            normalized.append(
                {
                    "title": dumped.get("title", ""),
                    "url": dumped.get("url") or dumped.get("link", ""),
                    "snippet": dumped.get("snippet", ""),
                    "source": source,
                    "source_type": source_type,
                    "date": dumped.get("date"),
                    "content": dumped.get("content"),
                }
            )
            continue
        normalized.append(
            {
                "title": str(getattr(result, "title", "")),
                "url": str(getattr(result, "url", "") or getattr(result, "link", "")),
                "snippet": str(getattr(result, "snippet", "")),
                "source_type": "google_search",
            }
        )
    return normalized


def _search_request_from_query(
    query: str | _OpportunityQuerySpec,
    *,
    max_results: int,
) -> SearchRequest:
    if isinstance(query, _OpportunityQuerySpec):
        source = _search_source_for_spec(query)
        return SearchRequest(
            query=query.query,
            num_results=max_results,
            source=source,
            time_range=(
                query.time_window if _query_spec_uses_provider_time_filter(query) else None
            ),
            country=query.country,
            location=query.location,
            language=query.language,
            page=query.page,
            safe_search=query.safe_search,
            scrape=False,
        )
    return SearchRequest(query=query, num_results=max_results)


def _query_spec_uses_provider_time_filter(spec: _OpportunityQuerySpec) -> bool:
    """Use publication-date filters only when recency is itself the source signal."""

    return spec.source == "news" or spec.lane in {"role", "company_growth"}


def _search_source_for_spec(spec: _OpportunityQuerySpec) -> str:
    if spec.source != "web":
        return spec.source
    query = spec.query.lower()
    if spec.lane in {"company_growth", "collaboration"} and any(
        marker in query
        for marker in (
            "announces",
            "raises",
            "funding",
            "businesswire.com",
            "prnewswire.com",
            "press release",
        )
    ):
        return "news"
    return "web"


def _opportunity_search_concurrency() -> int:
    raw = os.getenv("KEYSTONE_OPPORTUNITY_SEARCH_CONCURRENCY", "4").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 4
    return max(1, min(8, value))


def _opportunity_followup_result_cap() -> int:
    raw = os.getenv("KEYSTONE_OPPORTUNITY_FOLLOWUP_RESULT_CAP", "8").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 8
    return max(1, min(8, value))


def _opportunity_retrieval_deadline_seconds() -> float:
    raw = os.getenv("KEYSTONE_OPPORTUNITY_RETRIEVAL_DEADLINE_SECONDS", "90").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 90.0
    return max(1.0, min(600.0, value))


@dataclass
class _OpportunityRetrievalBudget:
    deadline_seconds: float
    clock: Callable[[], float]
    started_at: float
    stopped_before_stage: str = ""

    def allows(self, stage: str) -> bool:
        if self.clock() - self.started_at < self.deadline_seconds:
            return True
        if not self.stopped_before_stage:
            self.stopped_before_stage = stage
        return False

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self.started_at)


def _env_flag(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _search_query_specs_with_provider(
    *,
    search_provider: Any,
    query_specs: list[_OpportunityQuerySpec],
    max_results: int,
) -> list[dict[str, Any]]:
    """Run Opportunity Scout query lanes with bounded concurrency and stable ordering."""

    if len(query_specs) <= 1:
        return [
            _search_result_to_hit(spec, result)
            for spec in query_specs
            for result in _search_with_provider_safe(search_provider, spec, max_results)
        ]

    max_workers = min(_opportunity_search_concurrency(), len(query_specs))
    if max_workers <= 1:
        return [
            _search_result_to_hit(spec, result)
            for spec in query_specs
            for result in _search_with_provider_safe(search_provider, spec, max_results)
        ]

    ordered_hits: list[list[dict[str, Any]]] = [[] for _spec in query_specs]

    def run_query(index: int, spec: _OpportunityQuerySpec) -> tuple[int, list[dict[str, Any]]]:
        results = _search_with_provider_safe(search_provider, spec, max_results)
        return index, [_search_result_to_hit(spec, result) for result in results]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_query, index, spec): index for index, spec in enumerate(query_specs)
        }
        for future in as_completed(futures):
            index, hits = future.result()
            ordered_hits[index] = hits

    return [hit for hits in ordered_hits for hit in hits]


def _search_with_provider_safe(
    search_provider: Any,
    spec: _OpportunityQuerySpec,
    max_results: int,
) -> list[dict[str, Any]]:
    try:
        return _search_with_provider(search_provider, spec, max_results)
    except SearchProviderError:
        return []


def _require_live_search_provider(search_provider: Any) -> None:
    provider_name = str(getattr(search_provider, "provider_name", "") or "").lower()
    if getattr(search_provider, "dry_run", False) or provider_name == "dry-run":
        raise LiveSearchProviderRequiredError(
            "Opportunity Scout live search requires a live Serper, SearXNG, or Firecrawl provider. "
            "Use --live-search --no-dry-run with --search-provider serper, searxng, or firecrawl."
        )
    validate = getattr(search_provider, "validate_configuration", None)
    if callable(validate):
        validate()


def _search_provider_label(search_provider: Any) -> str:
    provider_name = str(getattr(search_provider, "provider_name", "") or "").strip()
    if provider_name:
        return provider_name
    class_name = type(search_provider).__name__.replace("SearchProvider", "").replace("Tool", "")
    return re.sub(r"[^a-z0-9]+", "_", class_name.lower()).strip("_") or "custom"


def _source_hit_key(hit: dict[str, Any]) -> str:
    url = str(hit.get("source_url") or hit.get("url") or hit.get("link") or "").strip().lower()
    if url:
        return url.rstrip("/")
    title = str(hit.get("source_title") or hit.get("title") or "").strip().lower()
    signal = str(hit.get("signal") or hit.get("snippet") or "").strip().lower()
    return f"{title}|{signal}"


def _opportunity_verification_cap(*, enabled: bool | None = None) -> int:
    if enabled is False:
        return 0
    if enabled is None and not _env_flag("KEYSTONE_ENABLE_OPPORTUNITY_SOURCE_VERIFICATION"):
        return 0
    raw = os.getenv("KEYSTONE_OPPORTUNITY_VERIFY_CAP", "4").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 4
    return max(0, min(8, value))


def _verify_source_hits(
    hits: list[dict[str, Any]],
    *,
    verify_source_pages: bool | None = None,
    verification_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Optionally verify top candidate pages with capped clean-text extraction."""

    cap = _opportunity_verification_cap(enabled=verify_source_pages)
    if cap <= 0:
        return hits, []

    cache = verification_cache if verification_cache is not None else {}
    verified: list[dict[str, Any]] = []
    notes: list[str] = [f"Verified up to {cap} unique source page(s) with capped extraction."]
    fallback_providers = _opportunity_verification_fallback_providers()
    extraction_budget = website_extraction_budget()
    attempts = 0
    html_review_attempts = 0
    for hit in hits:
        enriched = dict(hit)
        url = str(hit.get("source_url") or "").strip()
        hit_key = _source_hit_key(hit)
        cached = cache.get(hit_key) if hit_key else None
        if cached is not None:
            enriched.update(cached)
            notes.append(f"Reused verified source page for {hit.get('company_name') or url}.")
        elif (
            len(cache) < cap
            and attempts < cap
            and hit_key
            and url.startswith(("http://", "https://"))
        ):
            attempts += 1
            company_name = str(hit.get("company_name") or "candidate")
            try:
                extraction = _extract_opportunity_verification_page(
                    url,
                    company_name=company_name,
                    fallback_providers=fallback_providers,
                    budget=extraction_budget,
                )
            except WebsiteExtractionError as exc:
                notes.append(f"Verification failed for {hit.get('company_name') or url}: {exc}")
                cache[hit_key] = {}
            else:
                if extraction.provider != os.getenv("KEYSTONE_WEBSITE_EXTRACTOR", "trafilatura"):
                    notes.append(
                        f"Verification used {extraction.provider} for "
                        f"{hit.get('company_name') or url}."
                    )
                text = extraction.text_or_markdown.strip()
                if text:
                    excerpt = _opportunity_verification_excerpt(text)
                    enriched["verified_excerpt"] = excerpt
                    # This relevance-selected excerpt is not an exact prefix.
                    # Start saved-source continuation at zero so no text is skipped.
                    _, access = project_web_source(
                        extraction, selected_url=url, max_chars=0,
                    )
                    enriched["web_source_access"] = access.model_dump(mode="json")
                    review_claims: list[str] = []
                    if (
                        agent_html_review_enabled()
                        and html_review_attempts < agent_html_review_max_pages()
                        and len(extraction.claims) <= agent_html_review_min_claims()
                    ):
                        html_review_attempts += 1
                        try:
                            review_subject = str(
                                hit.get("company_name") or hit.get("source_title") or url
                            )
                            review = run_agent_html_review(
                                html_or_text=text,
                                subject=review_subject,
                                url=url,
                                title=str(hit.get("source_title") or ""),
                                live=True,
                            )
                        except Exception as exc:
                            notes.append(
                                "Agent HTML review failed for "
                                f"{hit.get('company_name') or url}: {exc}"
                            )
                        else:
                            review_claims = [
                                claim
                                for claim in review.claims
                                if claim and claim not in extraction.claims
                            ]
                            if review_claims:
                                enriched["agent_html_review_claims"] = review_claims
                                notes.append(
                                    "Agent HTML review added "
                                    f"{len(review_claims)} claim(s) for "
                                    f"{hit.get('company_name') or url}."
                                )
                    enriched["signal"] = "; ".join(
                        dict.fromkeys(
                            [
                                str(enriched.get("signal") or "").strip(),
                                excerpt[:500],
                                *review_claims,
                            ]
                        )
                    ).strip("; ")
                    enriched["signals"] = _signals_from_text(
                        " ".join(
                            [
                                str(enriched.get("query") or ""),
                                str(enriched.get("source_title") or ""),
                                excerpt,
                            ]
                        )
                    )
                    cache[hit_key] = {
                        key: enriched[key]
                        for key in (
                            "verified_excerpt",
                            "web_source_access",
                            "agent_html_review_claims",
                            "signal",
                            "signals",
                        )
                        if key in enriched
                    }
                    notes.append(f"Verified source page for {hit.get('company_name') or url}.")
        verified.append(enriched)
    return verified, list(dict.fromkeys(note for note in notes if note))


def _opportunity_verification_fallback_providers() -> tuple[str, ...]:
    primary = os.getenv("KEYSTONE_WEBSITE_EXTRACTOR", "trafilatura")
    return tuple(website_extraction_provider_sequence(primary_provider=primary)[1:])


def _extract_opportunity_verification_page(
    url: str,
    *,
    company_name: str,
    fallback_providers: tuple[str, ...],
    budget: WebsiteExtractionBudget | None = None,
) -> WebsiteExtractionResult:
    primary_provider = os.getenv("KEYSTONE_WEBSITE_EXTRACTOR", "trafilatura").strip().lower()
    return extract_website_content_with_fallbacks(
        url,
        company_name=company_name,
        primary_provider=primary_provider,
        fallback_providers=fallback_providers,
        guardrail_context="public_opportunity_source",
        live=True,
        budget=budget,
        extractor=extract_website_content,
    )


def _filter_and_dedupe_candidate_hits(
    hits: list[dict[str, Any]],
    *,
    topic: str | None,
    search_plan: OpportunitySearchPlan | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    list[dict[str, Any]],
    list[str],
    list[dict[str, Any]],
    list[str],
]:
    (
        accepted_hits,
        acceptance_audit_notes,
        acceptance_filtered_candidates,
        acceptance_review_candidates,
    ) = _apply_candidate_acceptance_filters_to_hits(
        hits,
        topic=topic,
        search_plan=search_plan,
    )
    topic_hits, topic_audit_notes, topic_filtered_candidates = (
        _apply_topic_relevance_filters_to_hits(
            accepted_hits,
            topic=topic,
            search_plan=search_plan,
        )
    )
    detail_hits, detail_audit_notes, detail_filtered_candidates = (
        _apply_detail_completeness_filters_to_hits(
            topic_hits,
            topic=topic,
            search_plan=search_plan,
        )
    )
    filtered_hits, filter_audit_notes, role_filtered_candidates = _apply_hard_filters_to_hits(
        detail_hits,
        topic=topic,
    )
    deduped = _dedupe_candidate_hits(filtered_hits)
    filtered_candidates = [
        *acceptance_filtered_candidates,
        *topic_filtered_candidates,
        *detail_filtered_candidates,
        *role_filtered_candidates,
    ]
    audit_notes = [
        *acceptance_audit_notes,
        *topic_audit_notes,
        *detail_audit_notes,
        *filter_audit_notes,
    ]
    return (
        deduped,
        filtered_hits,
        filtered_candidates,
        acceptance_review_candidates,
        audit_notes,
        acceptance_filtered_candidates,
        acceptance_audit_notes,
        role_filtered_candidates,
        filter_audit_notes,
    )


def _process_candidate_hits(
    hits: list[dict[str, Any]],
    *,
    topic: str | None,
    search_plan: OpportunitySearchPlan | None = None,
    verify_source_pages: bool | None = None,
    verification_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    list[str],
]:
    deduped_hits = _dedupe_source_hits(hits)
    verified_hits, verification_notes = _verify_source_hits(
        deduped_hits,
        verify_source_pages=verify_source_pages,
        verification_cache=verification_cache,
    )
    (
        deduped,
        filtered_hits,
        filtered_candidates,
        acceptance_review_candidates,
        candidate_audit_notes,
        _acceptance_filtered_candidates,
        _acceptance_audit_notes,
        _role_filtered_candidates,
        _filter_audit_notes,
    ) = _filter_and_dedupe_candidate_hits(
        verified_hits,
        topic=topic,
        search_plan=search_plan,
    )
    return (
        deduped,
        filtered_hits,
        filtered_candidates,
        acceptance_review_candidates,
        candidate_audit_notes,
        verification_notes,
    )


def _dedupe_source_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for hit in hits:
        key = _source_hit_key(hit)
        if not key:
            continue
        if key not in deduped:
            deduped[key] = dict(hit)
            continue
        existing = deduped[key]
        existing["signals"] = list(
            dict.fromkeys([*existing.get("signals", []), *hit.get("signals", [])])
        )
        existing["signal"] = "; ".join(
            dict.fromkeys([str(existing.get("signal", "")), str(hit.get("signal", ""))])
        ).strip("; ")
        existing["search_lanes"] = list(
            dict.fromkeys(
                [
                    *[str(item) for item in existing.get("search_lanes", []) if str(item).strip()],
                    *[str(item) for item in hit.get("search_lanes", []) if str(item).strip()],
                ]
            )
        )
        existing["time_windows"] = list(
            dict.fromkeys(
                [
                    *[str(item) for item in existing.get("time_windows", []) if str(item).strip()],
                    *[str(item) for item in hit.get("time_windows", []) if str(item).strip()],
                ]
            )
        )
        existing["usa_relevance_score"] = max(
            int(existing.get("usa_relevance_score") or 0),
            int(hit.get("usa_relevance_score") or 0),
        )
        existing["usa_relevance_reasons"] = list(
            dict.fromkeys(
                [
                    *[
                        str(item)
                        for item in existing.get("usa_relevance_reasons", [])
                        if str(item).strip()
                    ],
                    *[
                        str(item)
                        for item in hit.get("usa_relevance_reasons", [])
                        if str(item).strip()
                    ],
                ]
            )
        )
    return list(deduped.values())


def _dedupe_candidate_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for hit in hits:
        entity_kind = str(hit.get("entity_kind") or "company")
        entity_name = str(hit.get("company_name", ""))
        key = str(
            hit.get("canonical_entity_key") or _normalize_entity_key(entity_name, entity_kind)
        )
        if key not in deduped:
            merged = dict(hit)
            merged["_source_hits"] = [hit]
            merged["canonical_entity_key"] = key
            deduped[key] = merged
            continue
        existing = deduped[key]
        existing["_source_hits"].append(hit)
        existing["_source_hits"] = _dedupe_source_hits(existing["_source_hits"])
        existing["signals"] = list(
            dict.fromkeys([*existing.get("signals", []), *hit.get("signals", [])])
        )
        existing["signal"] = "; ".join(
            dict.fromkeys([str(existing.get("signal", "")), str(hit.get("signal", ""))])
        ).strip("; ")
        existing["search_lanes"] = list(
            dict.fromkeys(
                [
                    *[str(item) for item in existing.get("search_lanes", []) if str(item).strip()],
                    *[str(item) for item in hit.get("search_lanes", []) if str(item).strip()],
                ]
            )
        )
        existing["time_windows"] = list(
            dict.fromkeys(
                [
                    *[str(item) for item in existing.get("time_windows", []) if str(item).strip()],
                    *[str(item) for item in hit.get("time_windows", []) if str(item).strip()],
                ]
            )
        )
        existing["usa_relevance_score"] = max(
            int(existing.get("usa_relevance_score") or 0),
            int(hit.get("usa_relevance_score") or 0),
        )
        existing["usa_relevance_reasons"] = list(
            dict.fromkeys(
                [
                    *[
                        str(item)
                        for item in existing.get("usa_relevance_reasons", [])
                        if str(item).strip()
                    ],
                    *[
                        str(item)
                        for item in hit.get("usa_relevance_reasons", [])
                        if str(item).strip()
                    ],
                ]
            )
        )
    return list(deduped.values())


def _records_from_hits(
    hits: list[dict[str, Any]],
    *,
    topic: str | None,
    max_results: int,
    dry_run: bool,
    save: bool = False,
    existing_state_by_company: dict[str, ExistingOpportunityState] | None = None,
) -> OpportunityScoutResult:
    records: list[OpportunityRecord] = []
    state_decisions: list[OpportunityStateDecision] = []
    duplicate_companies_skipped: list[str] = []
    existing_state_by_company = existing_state_by_company or {}
    for hit in hits:
        opportunity_type = normalize_type(str(hit.get("opportunity_type", "")))
        signals = clean_signals([str(signal) for signal in hit.get("signals", [])])
        if not signals and hit.get("signal"):
            signals = [str(hit["signal"])]
        company_name = str(hit.get("company_name") or "Unknown company")
        company_key = _normalize_company_key(company_name)
        existing_state = existing_state_by_company.get(company_key)
        if existing_state and existing_state.status in SKIP_EXISTING_STATUSES:
            state_decisions.append(
                OpportunityStateDecision(
                    company_name=company_name,
                    status=existing_state.status,
                    action="skipped_duplicate",
                    reason=(
                        f"Existing pipeline state is {existing_state.status}; Scout will "
                        "not create a duplicate opportunity."
                    ),
                )
            )
            duplicate_companies_skipped.append(company_name)
            continue
        state_action = "new"
        if existing_state and existing_state.status in UPDATE_EXISTING_STATUSES:
            state_action = "update_existing"
            state_decisions.append(
                OpportunityStateDecision(
                    company_name=company_name,
                    status=existing_state.status,
                    action="update_existing",
                    reason=(
                        f"Existing pipeline state is {existing_state.status}; enrich the "
                        "current opportunity instead of creating a new one."
                    ),
                )
            )
        else:
            state_decisions.append(
                OpportunityStateDecision(
                    company_name=company_name,
                    status=None,
                    action="new",
                    reason="No existing pipeline state matched this company.",
                )
            )
        why_now = str(hit.get("signal") or "; ".join(signals) or "Signal requires review.")
        source_hits = (
            hit.get("_source_hits") if isinstance(hit.get("_source_hits"), list) else [hit]
        )
        source_hits = _dedupe_source_hits(source_hits)
        search_lanes = list(
            dict.fromkeys(
                str(item)
                for source_hit in source_hits
                for item in (
                    source_hit.get("search_lanes")
                    if isinstance(source_hit.get("search_lanes"), list)
                    else [source_hit.get("query_lane") or hit.get("query_lane") or ""]
                )
                if str(item).strip()
            )
        )
        time_windows = list(
            dict.fromkeys(
                str(item)
                for source_hit in source_hits
                for item in (
                    source_hit.get("time_windows")
                    if isinstance(source_hit.get("time_windows"), list)
                    else [source_hit.get("query_time_window") or hit.get("query_time_window") or ""]
                )
                if str(item).strip()
            )
        )
        usa_relevance_score = max(
            int(hit.get("usa_relevance_score") or 0),
            *[int(source_hit.get("usa_relevance_score") or 0) for source_hit in source_hits],
        )
        usa_relevance_reasons = list(
            dict.fromkeys(
                [
                    str(item)
                    for source_hit in source_hits
                    for item in source_hit.get("usa_relevance_reasons", [])
                    if str(item).strip()
                ]
            )
        )
        sources = [
            _source_from_hit(source_hit, str(source_hit.get("signal") or why_now))
            for source_hit in source_hits
        ]
        source_bundles = _source_bundles_from_hits(
            company_name=company_name,
            source_hits=source_hits,
            why_now=why_now,
        )
        missing_evidence = _flatten_bundle_items(source_bundles, "missing_evidence")
        contradictions = _flatten_bundle_items(source_bundles, "contradictions")
        weak_evidence_reasons = _flatten_bundle_items(source_bundles, "weak_evidence_reasons")
        stale_signal_count = sum(bundle.stale_signal_count for bundle in source_bundles)
        source_quality_summary = summarize_source_quality(
            [source.source_quality for source in sources if source.source_quality is not None]
        )
        score_breakdown = score_from_signals(
            signals,
            opportunity_type,
            source_quality_summary=source_quality_summary,
        )
        outside_likelihood = estimate_outside_consulting_likelihood(
            score_breakdown,
            signal_count=len(signals),
            signals=signals,
        )
        handoff = should_handoff_to_business_research_analyst(
            breakdown=score_breakdown,
            source_quality_summary=source_quality_summary,
            missing_evidence=missing_evidence,
            contradictions=contradictions,
            weak_evidence_reasons=weak_evidence_reasons,
        )
        handoff_reason = build_handoff_reason(
            breakdown=score_breakdown,
            source_quality_summary=source_quality_summary,
            missing_evidence=missing_evidence,
            contradictions=contradictions,
            weak_evidence_reasons=weak_evidence_reasons,
        )
        research_needed = build_research_needed(
            source_quality_summary=source_quality_summary,
            source_count=len(sources),
        )
        recommended_next_actions = _flatten_bundle_items(
            source_bundles,
            "recommended_next_actions",
        )
        novelty_score = _novelty_score_from_hits(
            source_hits=source_hits,
            existing_state=existing_state,
        )
        research_needed = list(
            dict.fromkeys(
                [
                    *research_needed,
                    *missing_evidence,
                    *weak_evidence_reasons,
                    *recommended_next_actions,
                ]
            )
        )
        analyst_recommendation = (
            "Run Business Research Analyst before any Outreach Composer work; "
            "do not draft copy yet."
            if handoff
            else "Keep in Scout queue and gather stronger evidence before business research."
        )
        record_kwargs: dict[str, Any] = {
            "company_name": company_name,
            "opportunity_kind": str(hit.get("opportunity_kind") or "")
            or _opportunity_kind_from_text(
                " ".join(
                    [
                        company_name,
                        why_now,
                        " ".join(signals),
                    ]
                ),
                lane=str(hit.get("query_lane") or ""),
                entity_kind=str(hit.get("entity_kind") or ""),
            ),
            "opportunity_type": opportunity_type,
            **_opportunity_detail_fields(hit),
            "role_title": str(hit.get("role_title") or ""),
            "role_location": str(hit.get("role_location") or ""),
            "role_remote": hit.get("role_remote"),
            "role_country": str(hit.get("role_country") or ""),
            "role_posted_at": (
                str(hit.get("published_at")) if hit.get("published_at") is not None else None
            ),
            "role_active": hit.get("role_active"),
            "role_fit_reason": (
                "Role evidence matches Keystone's buyer-facing clinical, research, product, "
                "or strategy profile."
                if str(hit.get("role_title") or "").strip()
                else ""
            ),
            "role_filter_notes": [
                str(note) for note in hit.get("role_filter_notes", []) if str(note).strip()
            ],
            "priority_score": score_breakdown.priority_score,
            "why_now_signal": why_now,
            "recommended_next_step": (
                "Hand off to Business Research Analyst for source-attributed company "
                "research before outreach."
                if handoff
                else "Keep in scout queue and gather stronger evidence before outreach."
            ),
            "sources": sources,
            "source_quality_summary": source_quality_summary,
            "source_signals": signals,
            "source_bundles": source_bundles,
            "existing_state": existing_state,
            "state_action": state_action,  # type: ignore[arg-type]
            "missing_evidence": missing_evidence,
            "contradictions": contradictions,
            "stale_signal_count": stale_signal_count,
            "weak_evidence_reasons": weak_evidence_reasons,
            "score_breakdown": score_breakdown,
            "score_rationale": score_breakdown.rationale,
            "keystone_fit_reason": keystone_fit_reason(
                opportunity_type=opportunity_type,
                breakdown=score_breakdown,
                signals=signals,
            ),
            "outside_consulting_likelihood": outside_likelihood,
            "handoff_to_business_research_analyst": handoff,
            "handoff_reason": handoff_reason,
            "analyst_recommendation": analyst_recommendation,
            "business_research_analyst_handoff_recommendation": (
                business_research_analyst_handoff_recommendation(
                    handoff_reason=handoff_reason,
                    research_needed=research_needed,
                )
                if handoff
                else ""
            ),
            "research_needed": (
                research_needed if handoff or missing_evidence or weak_evidence_reasons else []
            ),
            "outreach_draft": None,
            "approval_required_before_outreach": True,
            "approved_for_outreach": False,
        }
        if "entity_kind" in OpportunityRecord.model_fields:
            record_kwargs["entity_kind"] = str(hit.get("entity_kind") or "company")
        if "canonical_entity_key" in OpportunityRecord.model_fields:
            record_kwargs["canonical_entity_key"] = str(
                hit.get("canonical_entity_key")
                or _normalize_entity_key(
                    company_name,
                    str(hit.get("entity_kind") or "company"),
                )
            )
        if "usa_relevance" in OpportunityRecord.model_fields:
            record_kwargs["usa_relevance"] = _usa_relevance_summary(
                usa_relevance_score,
                usa_relevance_reasons,
            )
        if "novelty" in OpportunityRecord.model_fields:
            record_kwargs["novelty"] = _novelty_summary(
                novelty_score=novelty_score,
                existing_state=existing_state,
                search_lanes=search_lanes,
                time_windows=time_windows,
            )
        if "search_lanes" in OpportunityRecord.model_fields:
            record_kwargs["search_lanes"] = search_lanes
        if "search_time_windows" in OpportunityRecord.model_fields:
            record_kwargs["search_time_windows"] = time_windows
        record = OpportunityRecord(**record_kwargs)
        records.append(record)

    records.sort(
        key=lambda item: (
            item.priority_score,
            item.score_breakdown.urgency_score,
            item.score_breakdown.next_action_clarity_score,
            item.score_breakdown.keystone_fit_score,
        ),
        reverse=True,
    )
    limited = records[: max(0, max_results)]
    for record in limited:
        if record.handoff_to_business_research_analyst:
            handoff_to_business_research_analyst_placeholder_impl(
                company_name=record.company_name,
                reason=record.why_now_signal,
            )
        if save:
            save_opportunity_placeholder_impl(record.model_dump_json(), dry_run=True)
    mode_note = (
        "Fixture mode only; no live APIs were called."
        if dry_run
        else "Live search provider was used."
    )
    limited_bundles = [bundle for record in limited for bundle in record.source_bundles]
    top_record = limited[0] if limited else None
    scoring_components = (
        top_record.score_breakdown.model_dump(mode="json") if top_record is not None else {}
    )
    source_quality = summarize_source_quality(
        [
            source.source_quality
            for record in limited
            for source in record.sources
            if source.source_quality is not None
        ]
    )
    return OpportunityScoutResult(
        topic=topic,
        dry_run=dry_run,
        records=limited,
        source_bundles=limited_bundles,
        source_bundle_quality_notes=_source_bundle_quality_notes(limited_bundles),
        state_decisions=state_decisions,
        duplicate_companies_skipped=list(dict.fromkeys(duplicate_companies_skipped)),
        source_quality_summary=source_quality,
        decision_trace=DecisionTrace(
            selected_route="opportunity_scout",
            source_quality_reasons=(
                [source_quality.rationale] if source_quality is not None else []
            ),
            scoring_components=scoring_components,
            safety_gates_applied=[
                "dedupe_before_scoring",
                "no_outreach_generation",
                "business_research_analyst_handoff_before_outreach",
            ],
            missing_information_blockers=list(
                dict.fromkeys(
                    blocker
                    for record in limited
                    for blocker in [
                        *record.missing_evidence,
                        *record.weak_evidence_reasons,
                    ]
                )
            ),
            handoff_readiness=(
                "business_research_analyst_handoff_recommended"
                if any(record.handoff_to_business_research_analyst for record in limited)
                else "no_handoff_recommended"
            ),
            notes=[
                "Analyst scoring used relevance, Keystone fit, source confidence, urgency, "
                "and next-action clarity.",
                "No outreach drafts were generated.",
            ],
        ),
        audit_notes=[
            mode_note,
            "Scout discovered and deduplicated candidates before Analyst scoring.",
            "Analyst scoring used relevance, Keystone fit, source confidence, urgency, "
            "and next-action clarity.",
            "No outreach drafts were generated.",
            "High-priority records require Business Research Analyst handoff before any outreach.",
            "Business Research Analyst handoff is represented as a placeholder in this phase.",
        ],
        outreach_generated=False,
    )


def search_opportunity_sources_placeholder_impl(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    if not dry_run:
        raise RuntimeError("Live opportunity source search is not implemented.")

    hits = json.loads(fixture_json) if fixture_json else DEFAULT_FIXTURE_HITS
    if isinstance(hits, dict):
        hits = hits.get("records") or hits.get("opportunities") or [hits]
    filtered = [hit for hit in hits if isinstance(hit, dict) and _matches_topic(hit, topic)]
    return json.dumps(
        {
            "mode": "dry_run",
            "topic": topic,
            "hits": filtered[: max(0, max_results)],
        },
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_opportunity_sources_placeholder(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Search opportunity sources in dry-run mode without live API calls."""

    return search_opportunity_sources_placeholder_impl(
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


def load_existing_opportunity_state_impl(
    state_json: str | None = None,
    company_names: list[str] | None = None,
    dry_run: bool = True,
) -> str:
    """Load local opportunity pipeline state without live side effects."""

    if not dry_run:
        raise RuntimeError(
            "Live opportunity pipeline state loading is not implemented. "
            "Provide local state_json in dry-run mode."
        )
    states = _load_existing_state_map(state_json)
    company_keys = {
        _normalize_company_key(company_name)
        for company_name in (company_names or [])
        if company_name.strip()
    }
    filtered = [
        state for key, state in sorted(states.items()) if not company_keys or key in company_keys
    ]
    return json.dumps(
        {
            "mode": "dry_run",
            "states": [state.model_dump(mode="json") for state in filtered],
            "state_count": len(filtered),
            "respected_skip_statuses": sorted(SKIP_EXISTING_STATUSES),
            "respected_update_statuses": sorted(UPDATE_EXISTING_STATUSES),
        },
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def load_existing_opportunity_state(
    state_json: str | None = None,
    company_names: list[str] | None = None,
    dry_run: bool = True,
) -> str:
    """Load local opportunity pipeline state to avoid duplicate Scout records."""

    return load_existing_opportunity_state_impl(
        state_json=state_json,
        company_names=company_names,
        dry_run=dry_run,
    )


def _search_structured_source_category_impl(
    *,
    tool_key: str,
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    if not dry_run:
        raise RuntimeError(
            f"Live {tool_key.replace('_', ' ')} source search is not implemented. "
            "Use dry-run fixture mode or add an explicit live integration."
        )
    raw_hits = json.loads(fixture_json) if fixture_json else SOURCE_TOOL_FIXTURE_HITS[tool_key]
    hits = _state_items_from_payload(raw_hits)
    filtered = [hit for hit in hits if _matches_topic(hit, topic)]
    limited_hits = filtered[: max(0, max_results)]
    source_bundles: list[OpportunitySourceBundle] = []
    for hit in _dedupe_candidate_hits(limited_hits):
        company_name = str(hit.get("company_name") or "Unknown company")
        why_now = str(hit.get("signal") or "Signal requires review.")
        source_hits = (
            hit.get("_source_hits") if isinstance(hit.get("_source_hits"), list) else [hit]
        )
        source_bundles.extend(
            _source_bundles_from_hits(
                company_name=company_name,
                source_hits=source_hits,
                why_now=why_now,
            )
        )
    return json.dumps(
        {
            "mode": "dry_run",
            "source_tool": tool_key,
            "topic": topic,
            "hits": limited_hits,
            "source_bundles": [bundle.model_dump(mode="json") for bundle in source_bundles],
            "live_status": "not_implemented",
        },
        sort_keys=True,
    )


def search_funding_news_sources_impl(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    return _search_structured_source_category_impl(
        tool_key="funding_news",
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_funding_news_sources(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Search local dry-run funding and news opportunity fixtures."""

    return search_funding_news_sources_impl(
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


def search_job_posting_sources_impl(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    return _search_structured_source_category_impl(
        tool_key="job_posting",
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_job_posting_sources(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Search local dry-run job posting and hiring signal fixtures."""

    return search_job_posting_sources_impl(
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


def search_clinical_trials_sources_impl(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    return _search_structured_source_category_impl(
        tool_key="clinical_trial",
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_clinical_trials_sources(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Search local dry-run ClinicalTrials.gov-style opportunity fixtures."""

    return search_clinical_trials_sources_impl(
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


def search_grant_sources_impl(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    return _search_structured_source_category_impl(
        tool_key="grant",
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_grant_sources(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Search local dry-run NIH/SBIR/grant opportunity fixtures."""

    return search_grant_sources_impl(
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


def search_conference_publication_sources_impl(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    return _search_structured_source_category_impl(
        tool_key="conference_publication",
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_conference_publication_sources(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Search local dry-run conference and publication signal fixtures."""

    return search_conference_publication_sources_impl(
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


def search_journal_call_sources_impl(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    return _search_structured_source_category_impl(
        tool_key="journal_call",
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_journal_call_sources(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Search local dry-run journal call and special issue fixtures."""

    return search_journal_call_sources_impl(
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


def search_contract_rfp_sources_impl(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    return _search_structured_source_category_impl(
        tool_key="contract_rfp",
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_contract_rfp_sources(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Search local dry-run contract, RFP, and solicitation fixtures."""

    return search_contract_rfp_sources_impl(
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


def search_company_page_sources_impl(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    return _search_structured_source_category_impl(
        tool_key="company_page",
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_company_page_sources(
    topic: str | None = None,
    max_results: int = 5,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Search local dry-run company page opportunity fixtures."""

    return search_company_page_sources_impl(
        topic=topic,
        max_results=max_results,
        fixture_json=fixture_json,
        dry_run=dry_run,
    )


def score_opportunity_impl(
    company_name: str,
    opportunity_type: str,
    signals: list[str],
) -> str:
    normalized_type = normalize_type(opportunity_type)
    score_breakdown = score_from_signals(signals, normalized_type)
    priority_score = score_breakdown.priority_score
    outside_consulting_likelihood = estimate_outside_consulting_likelihood(
        score_breakdown,
        signal_count=len(signals),
        signals=signals,
    )
    return json.dumps(
        {
            "company_name": company_name,
            "opportunity_type": normalized_type,
            "priority_score": priority_score,
            "score_breakdown": score_breakdown.model_dump(mode="json"),
            "score_rationale": score_breakdown.rationale,
            "outside_consulting_likelihood": outside_consulting_likelihood,
            "handoff_to_business_research_analyst": priority_score >= HANDOFF_PRIORITY_THRESHOLD,
        },
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def score_opportunity(
    company_name: str,
    opportunity_type: str,
    signals: list[str],
) -> str:
    """Score an opportunity deterministically from target type and why-now signals."""

    return score_opportunity_impl(
        company_name=company_name,
        opportunity_type=opportunity_type,
        signals=signals,
    )


def handoff_to_business_research_analyst_placeholder_impl(company_name: str, reason: str) -> str:
    return json.dumps(
        {
            "mode": "dry_run",
            "company_name": company_name,
            "handoff_to_business_research_analyst": True,
            "reason": reason,
        },
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def handoff_to_business_research_analyst_placeholder(company_name: str, reason: str) -> str:
    """Record that a high-priority opportunity should be handed to Business Research Analyst."""

    return handoff_to_business_research_analyst_placeholder_impl(
        company_name=company_name,
        reason=reason,
    )


def save_opportunity_placeholder_impl(record_json: str, dry_run: bool = True) -> str:
    if not dry_run:
        raise RuntimeError("Live opportunity persistence is not implemented.")
    record = json.loads(record_json)
    return json.dumps(
        {
            "mode": "dry_run",
            "status": "saved",
            "company_name": record.get("company_name"),
            "outreach_generated": False,
        },
        sort_keys=True,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def save_opportunity_placeholder(record_json: str, dry_run: bool = True) -> str:
    """Persist an opportunity placeholder in dry-run mode only."""

    return save_opportunity_placeholder_impl(record_json=record_json, dry_run=dry_run)


def scout_opportunities_fixture(
    *,
    fixture: str | Path | None = None,
    topic: str | None = None,
    max_results: int = 5,
    dry_run: bool = True,
    save: bool = False,
    existing_state: Any = None,
) -> OpportunityScoutResult:
    """Run deterministic opportunity scouting from local fixture data."""

    if not dry_run:
        raise RuntimeError(
            "Live opportunity scouting is not implemented. Use dry-run fixture mode."
        )

    fixture_hits = _load_fixture_hits(fixture)
    if fixture is None:
        default_structured_hits = [
            hit
            for hit in DEFAULT_STRUCTURED_SOURCE_HITS
            if hit.get("company_name") != "Beacon CNS Therapeutics"
        ]
        fixture_hits = [*fixture_hits, *default_structured_hits]
    hits = _dedupe_candidate_hits([hit for hit in fixture_hits if _matches_topic(hit, topic)])
    existing_state_by_company = _load_existing_state_map(existing_state)
    result = _records_from_hits(
        hits,
        topic=topic,
        max_results=max_results,
        dry_run=True,
        save=save,
        existing_state_by_company=existing_state_by_company,
    )
    return result.model_copy(
        update={
            "raw_search_result_count": len(fixture_hits),
            "deduped_candidate_count": len(hits),
            "audit_notes": [
                "Fixture mode only; no live APIs were called.",
                "Purpose-built dry-run source bundles were used for funding/news, jobs, "
                "clinical trials, grants, conference/publication, journal call, "
                "contract/RFP, and company page signals.",
                "Existing opportunity state was loaded from local input only."
                if existing_state_by_company
                else "No existing opportunity state was supplied.",
                "Scout discovered and deduplicated candidates before Analyst scoring.",
                "Analyst scoring used relevance, Keystone fit, source confidence, urgency, "
                "and next-action clarity.",
                "No outreach drafts were generated.",
                "High-priority records require Business Research Analyst handoff "
                "before any outreach.",
            ],
        }
    )


def scout_opportunities_live_search(
    *,
    topic: str | None = None,
    max_results: int = 5,
    search_plan: OpportunitySearchPlan | None = None,
    search_provider: Any | None = None,
    serper_tool: Any | None = None,
    save: bool = False,
    existing_state: Any = None,
    verify_source_pages: bool | None = None,
    retrieval_deadline_seconds: float | None = None,
    clock: Callable[[], float] = perf_counter,
) -> OpportunityScoutResult:
    """Run explicit live search, then score and rank candidates locally."""

    provider = search_provider or serper_tool or build_search_provider(live=True)
    _require_live_search_provider(provider)
    provider_label = _search_provider_label(provider)
    resolved_search_plan = _resolved_search_plan(
        topic,
        max_results=max_results,
        search_plan=search_plan,
    )
    retrieval_budget = _OpportunityRetrievalBudget(
        deadline_seconds=(
            _opportunity_retrieval_deadline_seconds()
            if retrieval_deadline_seconds is None
            else max(0.0, retrieval_deadline_seconds)
        ),
        clock=clock,
        started_at=clock(),
    )
    if verify_source_pages is None and (
        _plan_is_broad_request(resolved_search_plan)
        or _plan_is_formal_opportunity_request(resolved_search_plan)
        or _plan_is_professional_development_request(resolved_search_plan)
        or _plan_is_grant_request(resolved_search_plan)
    ):
        verify_source_pages = True
    query_specs = _build_live_query_specs(topic, search_plan=resolved_search_plan)
    verification_cache: dict[str, dict[str, Any]] = {}
    queries = [spec.query for spec in query_specs]
    hits = _search_query_specs_with_provider(
        search_provider=provider,
        query_specs=query_specs,
        max_results=max_results,
    )
    raw_hit_count = len(hits)
    (
        deduped,
        filtered_hits,
        filtered_candidates,
        acceptance_review_candidates,
        candidate_audit_notes,
        verification_audit_notes,
    ) = _process_candidate_hits(
        hits,
        topic=topic,
        search_plan=resolved_search_plan,
        verify_source_pages=verify_source_pages,
        verification_cache=verification_cache,
    )

    coverage_audit_notes: list[str] = []
    coverage_specs, source_coverage = _build_coverage_followup_query_specs(
        topic,
        existing_specs=query_specs,
        hits=hits,
        search_plan=resolved_search_plan,
    )
    if coverage_specs and retrieval_budget.allows("coverage_followup"):
        coverage_hits = _search_query_specs_with_provider(
            search_provider=provider,
            query_specs=coverage_specs,
            max_results=min(8, max(max_results, _opportunity_followup_result_cap())),
        )
        raw_hit_count += len(coverage_hits)
        hits = [*hits, *coverage_hits]
        (
            deduped,
            filtered_hits,
            filtered_candidates,
            acceptance_review_candidates,
            candidate_audit_notes,
            coverage_verification_notes,
        ) = _process_candidate_hits(
            hits,
            topic=topic,
            search_plan=resolved_search_plan,
            verify_source_pages=verify_source_pages,
            verification_cache=verification_cache,
        )
        query_specs = [*query_specs, *coverage_specs]
        queries = [spec.query for spec in query_specs]
        missing_lanes = ", ".join(source_coverage.get("missing_lanes") or [])
        coverage_audit_notes = [
            (
                "Coverage-aware search ran "
                f"{len(coverage_specs)} targeted follow-up query/query lane(s)"
                + (f" for missing source lanes: {missing_lanes}." if missing_lanes else ".")
            ),
            *coverage_verification_notes,
        ]
    else:
        coverage_audit_notes = [
            "Coverage-aware search found no missing required source lanes."
            if source_coverage
            else "Coverage-aware search had no applicable source-lane requirements."
        ]

    adaptive_audit_notes: list[str] = []
    adaptive_specs: list[_OpportunityQuerySpec] = []
    if not deduped:
        adaptive_specs = _build_adaptive_followup_query_specs(
            topic,
            existing_specs=query_specs,
            search_plan=resolved_search_plan,
        )
    if adaptive_specs and retrieval_budget.allows("adaptive_followup"):
        adaptive_hits = _search_query_specs_with_provider(
            search_provider=provider,
            query_specs=adaptive_specs,
            max_results=min(8, max(max_results, _opportunity_followup_result_cap())),
        )
        raw_hit_count += len(adaptive_hits)
        hits = [*hits, *adaptive_hits]
        (
            deduped,
            filtered_hits,
            filtered_candidates,
            acceptance_review_candidates,
            candidate_audit_notes,
            adaptive_verification_notes,
        ) = _process_candidate_hits(
            hits,
            topic=topic,
            search_plan=resolved_search_plan,
            verify_source_pages=verify_source_pages,
            verification_cache=verification_cache,
        )
        query_specs = [*query_specs, *adaptive_specs]
        queries = [spec.query for spec in query_specs]
        adaptive_audit_notes = [
            (
                "Adaptive search ladder ran "
                f"{len(adaptive_specs)} follow-up query/query lane(s) after the first pass "
                "found no accepted candidates."
            ),
            *adaptive_verification_notes,
        ]
    else:
        adaptive_audit_notes = [
            "Adaptive search ladder was not needed."
            if deduped
            else "Adaptive search ladder had no applicable follow-up queries."
        ]
    deepening_audit_notes: list[str] = []
    deepening_rounds = 0
    while True:
        if not retrieval_budget.allows("result_deepening"):
            break
        deepening_specs = _build_result_deepening_query_specs(
            topic=topic,
            existing_specs=query_specs,
            accepted_count=len(deduped),
            desired_count=max_results,
            search_plan=resolved_search_plan,
        )
        if not deepening_specs:
            break
        deepening_rounds += 1
        deepening_hits = _search_query_specs_with_provider(
            search_provider=provider,
            query_specs=deepening_specs,
            max_results=min(8, max(max_results, _opportunity_followup_result_cap())),
        )
        raw_hit_count += len(deepening_hits)
        hits = [*hits, *deepening_hits]
        (
            deduped,
            filtered_hits,
            filtered_candidates,
            acceptance_review_candidates,
            candidate_audit_notes,
            deepening_verification_notes,
        ) = _process_candidate_hits(
            hits,
            topic=topic,
            search_plan=resolved_search_plan,
            verify_source_pages=verify_source_pages,
            verification_cache=verification_cache,
        )
        query_specs = [*query_specs, *deepening_specs]
        queries = [spec.query for spec in query_specs]
        deepening_audit_notes = [
            (
                "Result deepening requested additional SearXNG/compatible result pages "
                f"for {len(deepening_specs)} high-yield broad-search lane(s) "
                f"(round {deepening_rounds}, max page {_result_deepening_max_page()})."
            ),
            *deepening_verification_notes,
        ]
        if len(deduped) >= max_results:
            break
    if not deepening_audit_notes:
        deepening_audit_notes = [
            "Result deepening was not needed."
            if len(deduped) >= max_results or not _result_deepening_allowed(topic)
            else "Result deepening had no eligible broad-search lanes."
        ]
    underfill_audit_notes: list[str] = []
    underfill_specs = _build_underfill_followup_query_specs(
        topic,
        existing_specs=query_specs,
        accepted_count=len(deduped),
        desired_count=max_results,
        search_plan=resolved_search_plan,
    )
    if underfill_specs and retrieval_budget.allows("underfill_followup"):
        underfill_hits = _search_query_specs_with_provider(
            search_provider=provider,
            query_specs=underfill_specs,
            max_results=min(8, max(max_results, _opportunity_followup_result_cap())),
        )
        raw_hit_count += len(underfill_hits)
        hits = [*hits, *underfill_hits]
        (
            deduped,
            filtered_hits,
            filtered_candidates,
            acceptance_review_candidates,
            candidate_audit_notes,
            underfill_verification_notes,
        ) = _process_candidate_hits(
            hits,
            topic=topic,
            search_plan=resolved_search_plan,
            verify_source_pages=verify_source_pages,
            verification_cache=verification_cache,
        )
        query_specs = [*query_specs, *underfill_specs]
        queries = [spec.query for spec in query_specs]
        underfill_audit_notes = [
            (
                "Underfilled search broadened with "
                f"{len(underfill_specs)} targeted follow-up query/query lane(s)."
            ),
            *underfill_verification_notes,
        ]
    else:
        underfill_audit_notes = [
            "Underfilled search broadening was not needed."
            if len(deduped) >= max_results
            else "Underfilled search broadening had no applicable follow-up queries."
        ]
    constraint_relaxation_suggestion = _constraint_relaxation_suggestion(
        topic=topic,
        records_found=bool(deduped),
    )
    search_lanes_covered = list(dict.fromkeys(spec.lane for spec in query_specs))
    time_windows_covered = list(dict.fromkeys(spec.time_window for spec in query_specs))
    result = _records_from_hits(
        deduped,
        topic=topic,
        max_results=max_results,
        dry_run=False,
        save=save,
        existing_state_by_company=_load_existing_state_map(existing_state),
    )
    result = _split_mixed_meeting_grant_records(result, search_plan=resolved_search_plan)
    update: dict[str, Any] = {
        "search_provider": provider_label,
        "search_queries": queries,
        "raw_search_result_count": raw_hit_count,
        "deduped_candidate_count": len(deduped),
        "filtered_candidates": [
            FilteredOpportunityCandidate.model_validate(candidate)
            for candidate in filtered_candidates
        ],
        "review_candidates": [
            FilteredOpportunityCandidate.model_validate(candidate)
            for candidate in acceptance_review_candidates
        ],
        "constraint_relaxation_suggestion": constraint_relaxation_suggestion,
        "retrieval_diagnostics": {
            "status": "partial" if retrieval_budget.stopped_before_stage else "complete",
            "deadline_seconds": retrieval_budget.deadline_seconds,
            "elapsed_seconds": round(retrieval_budget.elapsed_seconds, 3),
            "stopped_before_stage": retrieval_budget.stopped_before_stage or None,
            "query_count": len(queries),
            "unique_pages_cached": len(verification_cache),
        },
        "audit_notes": [
            "Live search provider was used.",
            f"Provider: {provider_label}.",
            (
                "Search plan: "
                f"{', '.join(resolved_search_plan.target_entity_types or ['unspecified'])} "
                f"for {', '.join(resolved_search_plan.objectives or ['broad_discovery'])} "
                f"({resolved_search_plan.source})."
            ),
            (
                f"Ran {len(queries)} targeted search queries across lanes: "
                f"{', '.join(search_lanes_covered or [spec.lane for spec in query_specs])}."
            ),
            (
                "Coverage windows: "
                f"{', '.join(time_windows_covered or [spec.time_window for spec in query_specs])}."
            ),
            (
                f"Deduplicated {len(filtered_hits)} filter-passing search results to "
                f"{len(deduped)} candidate entities."
            ),
            *verification_audit_notes,
            "Broad Scout retrieval covered companies, collaborations, researchers, "
            "institutes, conferences, grants, and trials when the topic was not role-only.",
            *coverage_audit_notes,
            *adaptive_audit_notes,
            *deepening_audit_notes,
            *underfill_audit_notes,
            *(
                [
                    "Retrieval deadline reached before "
                    f"{retrieval_budget.stopped_before_stage}; returning bounded partial evidence."
                ]
                if retrieval_budget.stopped_before_stage
                else []
            ),
            *candidate_audit_notes,
            *(
                [
                    "Mixed meeting/grant lane postprocess split combined source bundles into "
                    "separate lane records."
                ]
                if _plan_is_meeting_grant_request(resolved_search_plan) and len(result.records) >= 2
                else []
            ),
            *(
                [f"Constraint to relax next: {constraint_relaxation_suggestion}"]
                if constraint_relaxation_suggestion
                else []
            ),
            "Analyst scoring used relevance, Keystone fit, source confidence, urgency, "
            "and next-action clarity.",
            "No outreach drafts were generated.",
            "High-priority records require Business Research Analyst handoff before any outreach.",
        ],
    }
    if "search_lanes" in OpportunityScoutResult.model_fields:
        update["search_lanes"] = search_lanes_covered or [spec.lane for spec in query_specs]
    if "search_time_windows" in OpportunityScoutResult.model_fields:
        update["search_time_windows"] = time_windows_covered or [
            spec.time_window for spec in query_specs
        ]
    return result.model_copy(update=update)


def _split_mixed_meeting_grant_records(
    result: OpportunityScoutResult,
    *,
    search_plan: OpportunitySearchPlan | None,
) -> OpportunityScoutResult:
    if not _plan_is_meeting_grant_request(search_plan):
        return result
    split_records: list[OpportunityRecord] = []
    changed = False
    for record in result.records:
        category_bundles: dict[str, list[OpportunitySourceBundle]] = {
            "conference": [],
            "grant": [],
        }
        for bundle in record.source_bundles:
            category = str(bundle.source_category or "").lower()
            if category in category_bundles:
                category_bundles[category].append(bundle)
        if not any(category_bundles.values()):
            split_records.append(record)
            continue
        if sum(1 for bundles in category_bundles.values() if bundles) > 1:
            changed = True
        for category in ("conference", "grant"):
            bundles = category_bundles[category]
            if not bundles:
                continue
            primary = bundles[0]
            sources = [source for bundle in bundles for source in bundle.sources] or list(
                record.sources
            )
            payload = record.model_dump(mode="python")
            payload.update(
                {
                    "company_name": primary.company_name or record.company_name,
                    "canonical_entity_key": (
                        f"{category}:"
                        f"{_normalize_company_key(primary.company_name or record.company_name)}"
                    ),
                    "entity_kind": "conference" if category == "conference" else "grant_program",
                    "opportunity_type": (
                        "journal article or publication call"
                        if category == "conference"
                        else "grant or collaboration opportunity"
                    ),
                    "role_title": primary.company_name or record.role_title,
                    "role_fit_reason": primary.summary or record.role_fit_reason,
                    "search_lanes": [category],
                    "source_bundles": bundles,
                    "sources": sources,
                    "source_quality_summary": primary.source_quality_summary,
                    "source_signals": list(
                        dict.fromkeys(
                            signal.signal_type for bundle in bundles for signal in bundle.signals
                        )
                    ),
                    "why_now_signal": primary.summary or record.why_now_signal,
                    "recommended_next_step": (
                        primary.recommended_next_actions[0]
                        if primary.recommended_next_actions
                        else record.recommended_next_step
                    ),
                    "missing_evidence": list(
                        dict.fromkeys(
                            [
                                *record.missing_evidence,
                                *[item for bundle in bundles for item in bundle.missing_evidence],
                            ]
                        )
                    ),
                    "weak_evidence_reasons": list(
                        dict.fromkeys(
                            [
                                *record.weak_evidence_reasons,
                                *[
                                    item
                                    for bundle in bundles
                                    for item in bundle.weak_evidence_reasons
                                ],
                            ]
                        )
                    ),
                    "claims": [],
                    "unsupported_claims_flagged": [],
                }
            )
            split_records.append(OpportunityRecord.model_validate(payload))
    if not changed or len(split_records) <= 1:
        return result
    ordered = sorted(
        split_records,
        key=lambda item: (0 if item.entity_kind == "conference" else 1, -item.priority_score),
    )
    return result.model_copy(update={"records": ordered[:2]})


def _constraint_relaxation_suggestion(
    *,
    topic: str | None,
    records_found: bool,
) -> str:
    """Suggest one practical relaxation when a tightly scoped live search returns nothing."""

    if records_found:
        return ""
    text = str(topic or "").strip().lower()
    if not text:
        return ""
    suggestions = (
        (
            "last 1 week",
            "Relax recency from the last 1 week to the last 30 days.",
        ),
        (
            "last one week",
            "Relax recency from the last 1 week to the last 30 days.",
        ),
        (
            "last 7 days",
            "Relax recency from the last 7 days to the last 30 days.",
        ),
        (
            "last 48 hours",
            "Relax recency from the last 48 hours to the last 7 days.",
        ),
        (
            "last 24 hours",
            "Relax recency from the last 24 hours to the last 7 days.",
        ),
        (
            "part-time",
            "Relax part-time to contract, advisory, or fractional leadership roles.",
        ),
        (
            "chief medical officer",
            (
                "Relax the title from chief medical officer to medical director or "
                "head of clinical strategy."
            ),
        ),
        (
            "behavioral health ai",
            (
                "Expand behavioral health AI to broader clinical AI or digital mental "
                "health leadership roles."
            ),
        ),
        (
            "remote",
            "Broaden remote-only requirements to include hybrid roles.",
        ),
    )
    for needle, suggestion in suggestions:
        if needle in text:
            return suggestion
    return (
        "Broaden one hard constraint and rerun with a wider title, recency window, or "
        "geography filter."
    )


def opportunity_scout_markdown(result: OpportunityScoutResult) -> str:
    """Render scout results as concise markdown."""

    lines = [
        "# Opportunity Scout Results",
        "",
        f"- Topic: {result.topic or 'all'}",
        f"- Dry run: {result.dry_run}",
        f"- Outreach generated: {result.outreach_generated}",
        f"- Records: {len(result.records)}",
    ]
    for index, record in enumerate(result.records, start=1):
        source_confidence = (
            record.source_quality_summary.overall_score if record.source_quality_summary else 0
        )
        source_bundle_labels = (
            ", ".join(bundle.source_category for bundle in record.source_bundles) or "none"
        )
        lines.extend(
            [
                "",
                f"## {index}. {record.company_name}",
                "",
                f"- Type: {record.opportunity_type}",
                f"- Priority score: {record.priority_score}",
                f"- Why now: {record.why_now_signal}",
                f"- Score rationale: {record.score_rationale}",
                f"- Next step: {record.recommended_next_step}",
                "- Business Research Analyst handoff: "
                f"{record.handoff_to_business_research_analyst}",
                f"- Handoff reason: {record.handoff_reason}",
                f"- Pipeline action: {record.state_action}",
                f"- Source confidence: {source_confidence}/100",
                f"- Source bundles: {source_bundle_labels}",
                f"- Sources: {', '.join(source.title for source in record.sources)}",
            ]
        )
    return "\n".join(lines)


def _opportunity_scout_active_tools() -> list[Any]:
    return [
        list_local_context_sources,
        search_local_context,
        read_local_context_file,
        retrieve_memory,
        check_workflow_duplicate,
        airtable_get_base_schema,
        airtable_read_records,
        airtable_write_record,
        search_web,
        extract_selected_urls_to_source_bundle,
        read_web_source_window,
        render_page,
        capture_browser_diagnostics,
        summarize_rendered_page_diagnostics,
        extract_research_claims_from_html,
        score_opportunity,
        save_entity_memory,
        save_opportunity_memory,
        *google_workspace_tools(),
    ]


def build_opportunity_scout_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
    attach_tools: bool = True,
    include_fixture_tools: bool = False,
    compact_instructions: bool = False,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    tool_scope_mode: ToolScopeMode | str = ToolScopeMode.AUTO,
    instruction_profile: Literal["discovery", "supplied_evidence"] = "discovery",
) -> Agent:
    """Build the scout with active tools and optional offline fixture scaffolding."""

    if instruction_profile not in {"discovery", "supplied_evidence"}:
        raise ValueError("Unknown Opportunity instruction profile.")
    if instruction_profile == "supplied_evidence":
        if attach_tools or include_fixture_tools:
            raise ValueError(
                "The supplied-evidence instruction profile requires a tool-free agent."
            )
        instructions = compose_direct_instructions(
            "keystone_profile.md", "safety_policy.md", "opportunity_scout_supplied_evidence.md",
        )
    else:
        skill_files = select_agent_skill_names(
            "opportunity_scout",
            request_text=request_text,
            context_flags=context_flags,
            include_all=include_all_skills,
            compact=compact_instructions,
        )
        composer = compose_direct_instructions if compact_instructions else compose_instructions
        prompt_files = (
            ("keystone_profile.md", "safety_policy.md", "opportunity_scout.md")
            if compact_instructions
            else (
                "keystone_profile.md",
                "safety_policy.md",
                "tools.md",
                "opportunity_scout.md",
            )
        )
        instructions = composer(*prompt_files, skill_files=skill_files)
    active_tools = _opportunity_scout_active_tools()
    fixture_tools = [
        search_opportunity_sources_placeholder,
        load_existing_opportunity_state,
        search_funding_news_sources,
        search_job_posting_sources,
        search_clinical_trials_sources,
        search_grant_sources,
        search_conference_publication_sources,
        search_journal_call_sources,
        search_contract_rfp_sources,
        search_company_page_sources,
        handoff_to_business_research_analyst_placeholder,
        save_opportunity_placeholder,
    ]
    tools = [*active_tools, *(fixture_tools if include_fixture_tools else [])]
    resolved_scope_mode = tool_scope_mode
    if include_fixture_tools:
        # This flag is an explicit offline-test capability. The live runtime
        # rejects it before building the agent.
        resolved_scope_mode = ToolScopeMode.FULL
    elif str(tool_scope_mode) == ToolScopeMode.AUTO.value and (
        request_text or manual_request_plan is not None or tool_tier is not None
    ):
        resolved_scope_mode = ToolScopeMode.REQUEST_SCOPED
    attachment = scope_tools_for_request(
        "opportunity_scout",
        tools if attach_tools else [],
        manual_request_plan=manual_request_plan,
        tool_tier=tool_tier,
        mode=resolved_scope_mode,
        required_tool_names=_required_opportunity_scout_tools_for_request(
            manual_request_plan
        ),
    )
    agent = build_sdk_agent(
        name="opportunity_scout",
        instructions=instructions,
        output_type=(
            SuppliedOpportunityResult
            if instruction_profile == "supplied_evidence"
            else OpportunityScoutResult
        ),
        tools=list(attachment.tools),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="opportunity_scout",
        handoff_description=(
            "Use to discover, source, deduplicate, score, and prioritize opportunities "
            "without generating outreach."
        ),
    )
    return attach_tool_scope_receipt(agent, attachment.scope)


def build_opportunity_scout_synthesis_agent(
    model: str | None = None,
    *,
    max_results: int = 1,
) -> Agent:
    """Build the compact, tool-free agent used after deterministic retrieval."""

    result_count = max(1, min(5, int(max_results)))
    max_tokens = min(3000, 1400 + (400 * result_count))
    instructions = compose_instructions(
        "opportunity_scout_synthesis_compact.md",
        shared_prompt_files=("memory_policy.md", "writing_style.md"),
    )
    attachment = tool_free_synthesis_attachment(
        "opportunity_scout",
        _opportunity_scout_active_tools(),
    )
    agent = build_sdk_agent(
        name="opportunity_scout",
        instructions=instructions,
        output_type=OpportunityScoutSynthesis,
        tools=[],
        guardrails=keystone_guardrails(),
        model=model,
        model_settings=build_model_settings(
            reasoning_effort="low",
            verbosity="low",
            max_tokens=max_tokens,
        ),
        policy_agent_name="opportunity_scout",
        handoff_description=(
            "Synthesize already retrieved and verified opportunity evidence without "
            "running tools, search, outreach, or writes."
        ),
    )
    return attach_tool_scope_receipt(agent, attachment.scope)


def apply_opportunity_scout_synthesis(
    retrieved: OpportunityScoutResult,
    synthesis: OpportunityScoutSynthesis,
) -> OpportunityScoutResult:
    """Merge compact model judgments onto deterministic verified records."""

    record_by_key: dict[str, OpportunityRecord] = {}
    for record in retrieved.records:
        for value in (
            record.canonical_entity_key or "",
            record.company_name,
            record.entity_name,
        ):
            key = value.strip().casefold()
            if key:
                record_by_key.setdefault(key, record)

    selected_records: list[OpportunityRecord] = []
    selected_ids: set[int] = set()
    synthesis_notes: list[str] = []
    for decision in synthesis.decisions:
        key = decision.record_key.strip().casefold()
        record = record_by_key.get(key)
        if record is None:
            synthesis_notes.append(
                f"Compact synthesis ignored unknown record key: {decision.record_key}."
            )
            continue
        if not decision.include:
            synthesis_notes.append(
                f"Compact synthesis withheld {record.company_name} from final ranking."
            )
            continue
        record_identity = id(record)
        if record_identity in selected_ids:
            continue
        selected_ids.add(record_identity)
        selected_records.append(
            record.model_copy(
                update={
                    "why_now_signal": decision.why_now_signal,
                    "keystone_fit_reason": decision.keystone_fit_reason,
                    "recommended_next_step": decision.recommended_next_step,
                    "missing_evidence": list(
                        dict.fromkeys([*record.missing_evidence, *decision.missing_evidence])
                    ),
                },
                deep=True,
            )
        )

    merged = retrieved.model_copy(
        update={
            "records": selected_records,
            "human_summary": synthesis.audit_summary,
            "audit_notes": list(
                dict.fromkeys(
                    [
                        *retrieved.audit_notes,
                        synthesis.audit_summary,
                        *synthesis_notes,
                    ]
                )
            ),
            "constraint_relaxation_suggestion": (
                synthesis.constraint_relaxation_suggestion
                or retrieved.constraint_relaxation_suggestion
            ),
            "outreach_generated": False,
            "decision": synthesis.decision,
        },
        deep=True,
    )
    return OpportunityScoutResult.model_validate(merged.model_dump(mode="json"))


def build_opportunity_assessment_agent(
    model: str | None = None,
    *,
    request_text: str = "",
) -> Agent:
    """Build a no-tool Scout variant for one compact supplied-source assessment."""

    instructions = compose_instructions(
        "safety_policy.md",
        "opportunity_assessment_compact.md",
        skill_files=(
            "evidence_attribution_and_claim_mapping",
            "context_permission_gating",
            "action_boundary_enforcement",
            "unsupported_claim_and_gap_handling",
        ),
        shared_prompt_files=("memory_policy.md", "writing_style.md"),
    )
    return build_sdk_agent(
        name="opportunity_scout",
        instructions=instructions,
        output_type=OpportunityAssessmentBrief,
        tools=[],
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="opportunity_scout",
        handoff_description=(
            "Use for a concise review-only assessment of one supplied opportunity packet."
        ),
    )


def run_opportunity_scout_sdk(
    typed_input: OpportunityScoutSDKInput | str,
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    context_flags: Mapping[str, bool] | None = None,
    tool_tier: str | int | None = None,
    max_turns: int | None = None,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    attach_tools: bool = True,
    include_fixture_tools: bool = False,
    compact_instructions: bool = False,
    provider_retrieval_required: bool | None = None,
    decision_contract: AgentDecisionContract | None = None,
    instruction_profile: Literal["discovery", "supplied_evidence"] = "discovery",
) -> TypedAgentRunResult[OpportunityScoutResult]:
    """Run the Scout while preventing fixture-only tools from entering live profiles."""

    if live and include_fixture_tools:
        raise ValueError("Opportunity fixture-only tools cannot be attached to a live SDK run.")

    plan = ExecutionIntentAuthority.from_value(manual_request_plan).plan
    requires_provider_retrieval = (
        bool(plan and plan.requires_live_search)
        if provider_retrieval_required is None
        else bool(provider_retrieval_required)
    )
    if instruction_profile == "supplied_evidence" and requires_provider_retrieval:
        raise ValueError("Supplied-evidence assessment cannot require provider retrieval.")
    resolved_tool_tier = tool_tier or (
        "deep_retrieval"
        if requires_provider_retrieval
        else _default_opportunity_scout_sdk_tool_tier(
            typed_input,
            live=live,
            manual_request_plan=manual_request_plan,
        )
    )
    turn_policy = resolve_sdk_turn_policy(
        "opportunity_scout",
        request_text=skill_request_text(typed_input),
        live_search=live or requires_provider_retrieval,
        manual_request_plan=manual_request_plan,
        explicit_max_turns=max_turns,
    )
    # Prompt compaction reduces input size; it must not reduce the model's tool-loop
    # opportunity. The central quality budget remains authoritative for search,
    # optional deepening, deterministic scoring, and final synthesis.
    initial_max_turns = turn_policy.max_turns
    agent = build_opportunity_scout_agent(
        model=model,
        request_text=skill_request_text(typed_input),
        context_flags=context_flags,
        tool_tier=resolved_tool_tier,
        attach_tools=attach_tools,
        include_fixture_tools=include_fixture_tools,
        compact_instructions=compact_instructions,
        manual_request_plan=manual_request_plan,
        tool_scope_mode=ToolScopeMode.REQUEST_SCOPED,
        instruction_profile=instruction_profile,
    )
    scope_receipt = tool_scope_receipt_for_agent(agent)
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=OpportunityScoutResult,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=initial_max_turns,
        tool_call_budget_contract=ToolCallBudgetContract(
            max_total_calls=resolve_sdk_tool_call_limit(
                "opportunity_scout",
                request_text=skill_request_text(typed_input),
                live_search=live or requires_provider_retrieval,
                manual_request_plan=manual_request_plan,
            ),
            stage="opportunity_scout_current_evidence",
        ),
        tool_correction_max_turns=min(initial_max_turns, 2),
        decision_repair_max_turns=1,
        trace_metadata=tool_scope_trace_metadata_for_agent(agent),
        tool_execution_contract=(
            ToolExecutionContract.required(
                ToolEvidenceGroup("current_opportunity_search", ("search_web",)),
                ToolEvidenceGroup(
                    "deterministic_opportunity_score",
                    ("score_opportunity",),
                ),
                stage="opportunity_scout_current_evidence",
            )
            if requires_provider_retrieval and attach_tools
            else None
        ),
        decision_contract=decision_contract or opportunity_scout_decision_contract(),
    )
    request_cache = getattr(result, "request_cache", None)
    if isinstance(request_cache, dict):
        request_cache["request_tool_scope"] = scope_receipt
    return result


def _default_opportunity_scout_sdk_tool_tier(
    typed_input: OpportunityScoutSDKInput | str,
    *,
    live: bool,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
) -> str:
    """Infer a read-only tool tier for default Opportunity Scout SDK runs."""

    del typed_input, live
    if _required_opportunity_scout_tools_for_request(manual_request_plan) == (
        "score_opportunity",
    ):
        return "deep_retrieval"
    return default_tool_tier_for_request(manual_request_plan)
