"""End-to-end dry-run Keystone business workflow."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_agent,
    build_business_research_analyst_focused_brief_agent,
    focused_brief_input_from_profile,
)
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent, run_gmail_triage_fixture
from keystone_agents.agents.opportunity_scout import (
    build_opportunity_scout_agent,
    scout_opportunities_fixture,
)
from keystone_agents.agents.orchestrator import review_specialist_output, route_request
from keystone_agents.agents.outreach_composer import (
    build_approved_outreach_drafting_context,
    build_outreach_composer_agent,
    build_outreach_composer_compact_synthesis_agent,
    compose_outreach_draft_fixture,
    compose_outreach_draft_llm_constrained,
    load_style_profile,
)
from keystone_agents.cli_sdk import jsonable
from keystone_agents.company_research import research_company_fixture
from keystone_agents.contact_enrichment import (
    ContactCandidate,
    ContactEnrichmentArtifact,
    build_contact_enrichment_artifact,
)
from keystone_agents.costing import AgentRunBudgetExceededError
from keystone_agents.feedback import build_operator_feedback_request
from keystone_agents.live_retrieval import (
    retrieve_company_profile_live,
    run_opportunity_scout_live,
)
from keystone_agents.model_provider import (
    MissingOpenAIAPIKeyError,
    ModelProviderConfigurationError,
)
from keystone_agents.models import (
    OpportunityScoutSDKInput,
    OutreachComposerSDKInput,
)
from keystone_agents.presentation.renderers import render_pipeline_report
from keystone_agents.run import run_retrieved_sdk_synthesis
from keystone_agents.schemas.approval import (
    ApprovalCheckpoint,
    ApprovalScope,
    ApprovalState,
    normalize_approval_state,
    state_allows_drafting,
    state_allows_external_use,
)
from keystone_agents.schemas.company_profile import CompanyProfile, CompanyResearchFocusedBrief
from keystone_agents.schemas.contact_context import ContactRecord
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.feedback import OperatorFeedbackRequest
from keystone_agents.schemas.handoff import (
    HandoffContractName,
    HandoffContractResult,
    handoff_contracts_metadata,
    raise_for_invalid_handoffs,
    validate_pipeline_handoff_contracts,
)
from keystone_agents.schemas.opportunity import (
    OpportunityRecord as ScoutOpportunityRecord,
)
from keystone_agents.schemas.opportunity import (
    OpportunityScoutResult,
    OpportunitySource,
)
from keystone_agents.schemas.orchestrator import OrchestratorOutputReview, OrchestratorResult
from keystone_agents.schemas.outreach import OpportunityRecord as OutreachOpportunityRecord
from keystone_agents.schemas.outreach import OutreachDraft, OutreachLLMDraftPayload
from keystone_agents.schemas.recommendation import OpportunityContactPath
from keystone_agents.tools.approval_tool import build_approval_queue_item
from keystone_agents.tools.storage_tool import StorageTool

BUSINESS_OPPORTUNITY_CATEGORIES = {"consulting_opportunity", "collaboration_opportunity"}
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+|\b[\w.-]+\.(?:com|org|net|ai|io|health)\b", re.I)
OutreachApprovalChannel = Literal["email", "linkedin"]
ROBUST_OPPORTUNITY_TO_OUTREACH_TOPIC = (
    "Find high-fit Keystone collaboration opportunities across companies, conferences, "
    "journal article calls, contract RFPs, grants, clinical trials, researchers, and "
    "institutes. Prioritize behavioral health, psychiatry, neuroscience, clinical AI, "
    "digital mental health, evidence generation, and U.S.-relevant timing signals."
)
WEEKLY_OUTREACH_FALLBACK_GOAL = (
    "compare notes on clinical AI evaluation, research operations, and "
    "opportunity-fit collaboration after human approval"
)
DEFAULT_GMAIL_DRAFT_ACCOUNT = "operator@example.com"
GMAIL_DRAFT_ACCOUNT_ENV_KEYS = (
    "KEYSTONE_GMAIL_DRAFT_ACCOUNT",
    "KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT",
)


class KeystonePipelineResult(BaseModel):
    """Structured output for the first end-to-end dry-run workflow."""

    dry_run: bool = True
    sdk_agents_constructed: bool = False
    triage: EmailTriageResult
    company_profile: CompanyProfile | None = None
    opportunity_record: ScoutOpportunityRecord | None = None
    outreach_draft: OutreachDraft | None = None
    opportunity_scout: OpportunityScoutResult | None = None
    approval_state: ApprovalState = ApprovalState.PENDING
    research_approval_state: ApprovalState = ApprovalState.APPROVED_FOR_RESEARCH
    drafting_approval_state: ApprovalState = ApprovalState.PENDING
    external_use_approval_state: ApprovalState = ApprovalState.PENDING
    approval_scope: ApprovalScope = ApprovalScope.DRAFTING
    approval_rationale: str = ""
    approval_checkpoints: list[ApprovalCheckpoint] = Field(default_factory=list)
    drafting_approved: bool = False
    external_use_approved: bool = False
    approval_required: bool = True
    email_sent: bool = False
    send_enabled: bool = False
    live_apis_called: bool = False
    handoff_contracts: list[HandoffContractResult] = Field(default_factory=list)
    operator_feedback_requests: list[OperatorFeedbackRequest] = Field(default_factory=list)
    storage: dict[str, Any] = Field(default_factory=dict)
    audit_notes: list[str] = Field(default_factory=list)


class OrchestratedSearchHandoffResult(BaseModel):
    """Structured search-specialist execution after an orchestrator decision."""

    request_text: str = ""
    orchestrator_decision: OrchestratorResult
    specialist_route: str | None = None
    specialist_request: dict[str, Any] = Field(default_factory=dict)
    specialist_executed: bool = False
    specialist_output_type: str | None = None
    specialist_output: Any = None
    retrieval: dict[str, Any] = Field(default_factory=dict)
    live_search: bool = False
    dry_run: bool = True
    send_enabled: bool = False
    can_send_email: bool = False
    audit_notes: list[str] = Field(default_factory=list)


class WorkflowSourceLink(BaseModel):
    """Compact source link surfaced for human opportunity review."""

    title: str = ""
    url: str = ""
    source_type: str = ""
    source_id: str = ""
    supported_signal: str = ""


class WeeklyOpportunityContactCandidate(BaseModel):
    """Best available contact target for one weekly opportunity."""

    company_name: str
    contact_name: str = ""
    role_title: str = ""
    contact_email: str | None = None
    linkedin_url: str | None = None
    contact_path_type: str = ""
    contact_path_label: str = ""
    contact_path_value: str = ""
    alternate_contact_paths: list[OpportunityContactPath] = Field(default_factory=list)
    source_id: str = ""
    source_url: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_human_confirmation: bool = True
    missing_information: list[str] = Field(default_factory=list)
    notes: str = ""


class WeeklyOpportunityWorkflowItem(BaseModel):
    """One opportunity moving through Scout, Account Research, and Outreach gates."""

    company_name: str
    opportunity_record: ScoutOpportunityRecord
    company_profile: CompanyProfile
    company_brief: CompanyResearchFocusedBrief | None = None
    contact_candidate: WeeklyOpportunityContactCandidate
    source_links: list[WorkflowSourceLink] = Field(default_factory=list)
    outreach_draft: OutreachDraft | None = None
    orchestrator_reviews: list[OrchestratorOutputReview] = Field(default_factory=list)
    approval_required: bool = True
    drafting_approved: bool = False
    external_use_approved: bool = False
    gmail_draft_ready: bool = False
    missing_information_blockers: list[str] = Field(default_factory=list)
    audit_notes: list[str] = Field(default_factory=list)


class WeeklyOpportunityWorkflowResult(BaseModel):
    """Structured weekly opportunity discovery workflow output."""

    topic: str
    cadence: str = "weekly"
    dry_run: bool = True
    live_search: bool = False
    max_opportunities: int = 5
    orchestrator_decision: OrchestratorResult
    opportunity_scout: OpportunityScoutResult
    items: list[WeeklyOpportunityWorkflowItem] = Field(default_factory=list)
    approval_state: ApprovalState = ApprovalState.PENDING
    approval_required: bool = True
    approval_channel: str = "#ai-agents-workflow"
    outreach_channel: OutreachApprovalChannel = "email"
    approval_instruction: str = (
        "Review source links, contact clarity, and any draft text before approving the next step."
    )
    send_enabled: bool = False
    email_sent: bool = False
    gmail_drafts_created: bool = False
    live_apis_called: bool = False
    live_sdk_synthesis: bool = False
    retrieval: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)
    audit_notes: list[str] = Field(default_factory=list)


def _sender_domain(sender_email: str) -> str:
    if "@" not in sender_email:
        return ""
    return sender_email.rsplit("@", 1)[1].strip().lower()


def _request_text(value: str | Mapping[str, Any]) -> str:
    if isinstance(value, str):
        return value.strip()
    parts: list[str] = []
    for key in ("request", "input", "text", "message", "subject", "body", "url"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            parts.append(item.strip())
    return "\n".join(parts).strip()


def _extract_request_url(text: str) -> str | None:
    match = _URL_RE.search(text)
    if not match:
        return None
    value = match.group(0).strip()
    return value if value.startswith(("http://", "https://")) else f"https://{value}"


def _domain_label(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    label = host.split(".", 1)[0].replace("-", " ").replace("_", " ").strip()
    return label.title() if label else ""


def _company_target_from_request(
    request_text: str,
    *,
    company_name: str | None = None,
    company_url: str | None = None,
) -> tuple[str, str | None, list[str]]:
    notes: list[str] = []
    resolved_url = company_url or _extract_request_url(request_text)
    resolved_name = (company_name or "").strip()
    if not resolved_name:
        cleaned = _URL_RE.sub(" ", request_text)
        cleaned = re.sub(
            r"\b("
            r"please|research|evaluate|review|company|account|profile|website|url|domain|"
            r"for|possible|partnership|advisory|relevance|to|keystone|focus|on|product|"
            r"customers|traction|signals|leadership|and|why|it|may|matter"
            r")\b",
            " ",
            cleaned,
            flags=re.I,
        )
        cleaned = " ".join(cleaned.split()).strip(" .,:;-")
        if cleaned and len(cleaned.split()) <= 8:
            resolved_name = cleaned
            notes.append("Derived company name from the routed request text.")
        elif resolved_url:
            resolved_name = _domain_label(resolved_url)
            notes.append("Derived company name from the routed request URL/domain.")
    if not resolved_name:
        resolved_name = "Requested Company"
        notes.append("Company name was not explicit; using a generic search fallback.")
    return resolved_name, resolved_url, notes


def run_orchestrated_search_handoff(
    request: str | Mapping[str, Any],
    *,
    route_result: OrchestratorResult | None = None,
    company_name: str | None = None,
    company_url: str | None = None,
    topic: str | None = None,
    live_search: bool = False,
    max_results: int = 5,
    search_provider: str | None = None,
    fallback_search_provider: str | None = None,
    existing_state: Any = None,
    workflow_state: Any = None,
    database_url: str | None = None,
    approved_company_profile: CompanyProfile | None = None,
    approved_opportunity_record: ScoutOpportunityRecord | None = None,
    use_llm: bool = False,
    llm_router: Any = None,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    include_operator_feedback_request: bool = False,
) -> OrchestratedSearchHandoffResult:
    """Route a request, then execute the search-heavy specialist path with the same hint."""

    text = _request_text(request)
    decision = route_result or route_request(
        request,
        workflow_state=workflow_state,
        database_url=database_url,
        approved_company_profile=approved_company_profile,
        approved_opportunity_record=approved_opportunity_record,
        use_llm=use_llm,
        llm_router=llm_router,
        run_config=run_config,
        live=live,
        model=model,
        include_operator_feedback_request=include_operator_feedback_request,
    )
    result = OrchestratedSearchHandoffResult(
        request_text=text,
        orchestrator_decision=decision,
        specialist_route=decision.route,
        live_search=live_search,
        dry_run=not live_search,
        send_enabled=False,
        can_send_email=False,
    )
    if decision.route == "business_research_analyst":
        resolved_name, resolved_url, notes = _company_target_from_request(
            text,
            company_name=company_name,
            company_url=company_url,
        )
        result.specialist_request = {
            "company_name": resolved_name,
            "company_url": resolved_url,
        }
        result.audit_notes.extend(notes)
        if live_search:
            profile, retrieval = retrieve_company_profile_live(
                company=resolved_name,
                company_url=resolved_url,
                request_text=text,
                requested_provider=search_provider,
                max_results=max_results,
                retrieval_hint=decision.retrieval_hint,
            )
        else:
            profile = research_company_fixture(
                company_name=resolved_name,
                company_url=resolved_url,
            )
            retrieval = {"mode": "fixture", "live_search": False}
        result.specialist_executed = True
        result.specialist_output_type = type(profile).__name__
        result.specialist_output = profile
        result.retrieval = retrieval
        return result

    if decision.route == "opportunity_scout":
        resolved_topic = (topic or text or "Keystone-relevant business opportunities").strip()
        result.specialist_request = {
            "topic": resolved_topic,
            "max_results": max_results,
        }
        if live_search:
            scout_result, retrieval = run_opportunity_scout_live(
                topic=resolved_topic,
                max_results=max_results,
                requested_provider=search_provider,
                fallback_provider=fallback_search_provider,
                retrieval_hint=decision.retrieval_hint,
                existing_state=existing_state,
            )
        else:
            scout_result = scout_opportunities_fixture(
                topic=resolved_topic,
                max_results=max_results,
                dry_run=True,
                save=False,
                existing_state=existing_state,
            )
            retrieval = {"mode": "fixture", "live_search": False}
        result.specialist_executed = True
        result.specialist_output_type = type(scout_result).__name__
        result.specialist_output = scout_result
        result.retrieval = retrieval
        return result

    result.audit_notes.append("Route does not use the search-oriented specialist handoff runner.")
    return result


def _workflow_source_links(
    *,
    opportunity: ScoutOpportunityRecord,
    company_profile: CompanyProfile,
) -> list[WorkflowSourceLink]:
    links: list[WorkflowSourceLink] = []
    for source in opportunity.sources:
        links.append(
            WorkflowSourceLink(
                title=source.title,
                url=source.url,
                source_type=source.source_type,
                source_id=source.source_id,
                supported_signal=source.supported_signal,
            )
        )
    for source in company_profile.sources:
        links.append(
            WorkflowSourceLink(
                title=source.title,
                url=source.url,
                source_type=source.source_type,
                source_id=source.source_id,
                supported_signal=source.supported_claims[0] if source.supported_claims else "",
            )
        )
    deduped: dict[str, WorkflowSourceLink] = {}
    for link in links:
        key = link.url or link.source_id or link.title
        if key and key not in deduped:
            deduped[key] = link
    return list(deduped.values())


def _buyer_role_from_profile(
    *,
    company_profile: CompanyProfile,
    opportunity: ScoutOpportunityRecord,
) -> str:
    for feature in company_profile.source_backed_features:
        if feature.feature_name == "buyer_function" and feature.value.strip():
            return feature.value.strip()
    if opportunity.role_title.strip():
        return opportunity.role_title.strip()
    if opportunity.opportunity_type in {"clinical AI", "trial technology", "CRO"}:
        return "Clinical operations or evidence generation leader"
    if opportunity.opportunity_type in {"behavioral health AI", "digital mental health"}:
        return "Clinical product or behavioral health leader"
    if opportunity.opportunity_type in {"CNS biotech", "neurotechnology"}:
        return "Research, clinical development, or partnerships leader"
    if opportunity.opportunity_type == "journal article or publication call":
        return "Editor, guest editor, or special issue contact"
    if opportunity.opportunity_type == "contract or RFP opportunity":
        return "Procurement, program, or teaming contact"
    return "Partnerships, research, or clinical operations leader"


def _derive_contact_candidate(
    *,
    company_profile: CompanyProfile,
    opportunity: ScoutOpportunityRecord,
    contact_name: str | None = None,
    contact_title: str | None = None,
    contact_email: str | None = None,
    contact_linkedin_url: str | None = None,
) -> WeeklyOpportunityContactCandidate:
    enrichment = build_contact_enrichment_artifact(
        company_name=company_profile.name,
        sources=[*opportunity.sources, *company_profile.sources],
    )
    best_candidate = _best_contact_candidate(enrichment)
    best_path = enrichment.best_contact_path
    source = company_profile.sources[0] if company_profile.sources else None
    role_title = (contact_title or "").strip() or _buyer_role_from_profile(
        company_profile=company_profile,
        opportunity=opportunity,
    )
    resolved_contact_name = (contact_name or company_profile.lead_name or "").strip()
    resolved_contact_email = (
        (contact_email or "").strip()
        or (best_candidate.email if best_candidate is not None else "")
        or (best_path.value if best_path is not None and best_path.path_type == "email" else "")
    ) or None
    resolved_linkedin_url = (
        (contact_linkedin_url or "").strip()
        or (best_candidate.linkedin_url if best_candidate is not None else "")
        or company_profile.linkedin_url
        or (best_path.url if best_path is not None and best_path.path_type == "linkedin" else "")
    ).strip()
    source_url = (
        resolved_linkedin_url
        or (best_path.url if best_path is not None else "")
        or company_profile.website
        or (source.url if source else "")
    )
    source_id = (
        (best_path.source_id if best_path is not None else "")
        or (best_candidate.source_ids[0] if best_candidate and best_candidate.source_ids else "")
        or (source.source_id if source else "")
        or "workflow:contact_candidate"
    )
    contact_path_type = best_path.path_type if best_path is not None else ""
    contact_path_label = best_path.label if best_path is not None else ""
    contact_path_value = best_path.value or best_path.url if best_path is not None else ""
    missing_information: list[str] = []
    if not resolved_contact_name:
        missing_information.append("Confirm a named contact.")
    if not resolved_contact_email:
        if best_path is not None:
            missing_information.append(
                "No confirmed recipient email; use the source-backed organization contact "
                "path manually or confirm an email before Gmail draft creation."
            )
        else:
            missing_information.append(
                "Confirm recipient email address before Gmail draft creation."
            )
    if not resolved_linkedin_url:
        missing_information.append("Confirm contact or company LinkedIn/profile URL.")
    missing_information.extend(enrichment.missing)

    return WeeklyOpportunityContactCandidate(
        company_name=company_profile.name,
        contact_name=resolved_contact_name,
        role_title=role_title,
        contact_email=resolved_contact_email,
        linkedin_url=resolved_linkedin_url or None,
        contact_path_type=contact_path_type,
        contact_path_label=contact_path_label,
        contact_path_value=contact_path_value,
        alternate_contact_paths=enrichment.alternate_contact_paths,
        source_id=source_id,
        source_url=source_url,
        confidence=_workflow_contact_confidence(
            has_name=bool(resolved_contact_name),
            has_email=bool(resolved_contact_email),
            enrichment=enrichment,
            override_supplied=bool(contact_email or contact_linkedin_url or contact_name),
        ),
        needs_human_confirmation=bool(missing_information),
        missing_information=list(dict.fromkeys(missing_information)),
        notes=(
            "Derived from Business Research Analyst profile fields, source-backed contact "
            "paths, and buyer context; human confirmation is required before any live "
            "Gmail draft."
        ),
    )


def _best_contact_candidate(
    enrichment: ContactEnrichmentArtifact,
) -> ContactCandidate | None:
    if not enrichment.candidates:
        return None
    return sorted(
        enrichment.candidates,
        key=lambda candidate: (
            bool(candidate.email),
            bool(candidate.linkedin_url),
            candidate.confidence,
        ),
        reverse=True,
    )[0]


def _workflow_contact_confidence(
    *,
    has_name: bool,
    has_email: bool,
    enrichment: ContactEnrichmentArtifact,
    override_supplied: bool,
) -> float:
    if override_supplied and has_name and has_email:
        return 0.85
    base = enrichment.contact_confidence or 0.35
    if has_name:
        base += 0.08
    if has_email:
        base += 0.12
    return max(0.0, min(0.95, base))


def _contact_record_from_candidate(
    candidate: WeeklyOpportunityContactCandidate,
) -> ContactRecord | None:
    if candidate.needs_human_confirmation or not candidate.contact_name:
        return None
    return ContactRecord(
        company_name=candidate.company_name,
        contact_name=candidate.contact_name,
        role_title=candidate.role_title,
        contact_email=candidate.contact_email,
        linkedin_url=candidate.linkedin_url,
        source="workflow",
        source_id=candidate.source_id or "workflow:contact_candidate",
        source_url=candidate.source_url,
        confidence=candidate.confidence,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        approval_scope=ApprovalScope.DRAFTING,
        notes=candidate.notes,
    )


def _company_url_hint(opportunity: ScoutOpportunityRecord) -> str | None:
    for source in opportunity.sources:
        if source.source_type == "company_site" and source.url.startswith(("http://", "https://")):
            return source.url
    return None


def _synthesize_weekly_opportunity_scout(
    scout_result: OpportunityScoutResult,
    *,
    topic: str,
    max_opportunities: int,
    retrieval_hint: Any | None,
) -> OpportunityScoutResult:
    """Use Opportunity Scout SDK synthesis for human-facing opportunity records."""

    def retrieve() -> OpportunityScoutResult:
        return scout_result

    def normalize(result: OpportunityScoutResult) -> OpportunityScoutSDKInput:
        payload = {
            "topic": result.topic,
            "records": result.records,
            "source_bundles": result.source_bundles,
            "audit_notes": result.audit_notes,
            "constraint_relaxation_suggestion": result.constraint_relaxation_suggestion,
        }
        return OpportunityScoutSDKInput(
            topic=topic,
            max_results=max_opportunities,
            context=(
                "Approved retrieved opportunity context for weekly human review:\n"
                f"{json.dumps(jsonable(payload), ensure_ascii=True, sort_keys=True)}"
            ),
            retrieval_hint=retrieval_hint,
        )

    outcome = run_retrieved_sdk_synthesis(
        agent=build_opportunity_scout_agent(),
        output_type=OpportunityScoutResult,
        retrieve=retrieve,
        normalize=normalize,
        input_summary=f"weekly opportunity SDK synthesis for {topic}",
        input_audit_payload={
            "topic": topic,
            "max_opportunities": max_opportunities,
            "sdk_synthesis": True,
            "workflow": "weekly_opportunity_workflow",
        },
        live=True,
        save=False,
        model_label="sdk-live",
    )
    synthesized = outcome.final_output
    expected_count = min(len(scout_result.records), max_opportunities)
    if expected_count and len(synthesized.records) < expected_count:
        return scout_result.model_copy(
            update={
                "audit_notes": [
                    *scout_result.audit_notes,
                    (
                        "SDK synthesis returned fewer records than deterministic live "
                        "retrieval; retained source-backed Scout records as canonical."
                    ),
                ]
            }
        )
    return synthesized


def _synthesize_weekly_company_brief(
    company_profile: CompanyProfile,
) -> CompanyResearchFocusedBrief:
    """Use Business Research Analyst SDK synthesis for the company brief shown to humans."""

    outcome = run_retrieved_sdk_synthesis(
        agent=build_business_research_analyst_focused_brief_agent(),
        output_type=CompanyResearchFocusedBrief,
        retrieve=lambda: company_profile,
        normalize=lambda profile: focused_brief_input_from_profile(profile),
        input_summary=f"weekly company brief SDK synthesis for {company_profile.name}",
        input_audit_payload={
            "company": company_profile.name,
            "company_url": company_profile.website,
            "sdk_synthesis": True,
            "workflow": "weekly_opportunity_workflow",
        },
        live=True,
        save=False,
        model_label="sdk-live",
    )
    return outcome.final_output


def _synthesize_weekly_outreach_draft(
    *,
    company_profile: CompanyProfile,
    opportunity: ScoutOpportunityRecord,
    contact_candidate: WeeklyOpportunityContactCandidate,
) -> OutreachDraft:
    """Use Outreach Composer SDK synthesis for approval-gated human draft text."""

    style_profile = load_style_profile("sample_email_style_profile_anup_approved")
    objective = (
        "Write one concise approval-gated email and LinkedIn draft for human review. "
        "Use only the approved source-backed weekly opportunity context. "
        "Do not send, schedule, publish, or claim external approval."
    )
    outreach_opportunity = _to_outreach_opportunity(opportunity)
    contact_context = _contact_record_from_candidate(contact_candidate)
    approved_context = build_approved_outreach_drafting_context(
        company_profile=company_profile,
        opportunity_record=outreach_opportunity,
        contact_context=contact_context,
        email_style_profile=style_profile,
        objective=objective,
    )

    def retrieve() -> dict[str, Any]:
        return {
            "company_profile": company_profile,
            "opportunity_record": outreach_opportunity,
            "contact_context": contact_context,
            "email_style_profile": style_profile,
            "approved_context": approved_context,
            "context_policy": "Approved source-backed weekly opportunity context only.",
        }

    def normalize(context: dict[str, Any]) -> OutreachComposerSDKInput:
        return OutreachComposerSDKInput(
            company_name=company_profile.name,
            contact_name=contact_candidate.contact_name or None,
            contact_title=contact_candidate.role_title or None,
            recent_signal=opportunity.why_now_signal,
            outreach_goal=objective,
            approved_context=(
                "Approved source-backed weekly outreach context:\n"
                f"{json.dumps(jsonable(context), ensure_ascii=True, sort_keys=True)}"
            ),
            email_style_profile=json.dumps(
                jsonable(style_profile),
                ensure_ascii=True,
                sort_keys=True,
            ),
        )

    outcome = run_retrieved_sdk_synthesis(
        agent=build_outreach_composer_compact_synthesis_agent(
            request_text=objective,
        ),
        output_type=OutreachLLMDraftPayload,
        retrieve=retrieve,
        normalize=normalize,
        input_summary=f"weekly outreach SDK synthesis for {company_profile.name}",
        input_audit_payload={
            "company": company_profile.name,
            "opportunity_title": opportunity.role_title or opportunity.company_name,
            "contact_name": contact_candidate.contact_name,
            "contact_title": contact_candidate.role_title,
            "sdk_synthesis": True,
            "workflow": "weekly_opportunity_workflow",
        },
        live=True,
        save=False,
        model_label="sdk-live",
    )
    compact_payload = jsonable(outcome.final_output)
    if not isinstance(compact_payload, dict):
        raise RuntimeError("Weekly outreach SDK synthesis did not return a JSON object.")
    source_ids_used = compact_payload.get("source_ids_used")
    if isinstance(source_ids_used, list) and "keystone_profile" not in source_ids_used:
        compact_payload["source_ids_used"] = [*source_ids_used, "keystone_profile"]
    draft = compose_outreach_draft_llm_constrained(
        approved_context=approved_context,
        llm_draft_payload=compact_payload,
        fallback_to_fixture=False,
    )
    return draft.model_copy(
        update={
            "contact_name": contact_candidate.contact_name or draft.contact_name,
            "contact_title": contact_candidate.role_title or draft.contact_title,
            "recipient": contact_candidate.contact_email or draft.recipient,
            "drafting_mode": "llm_constrained",
        }
    )


def _compose_weekly_outreach_fixture_draft(
    *,
    company_profile: CompanyProfile,
    opportunity: ScoutOpportunityRecord,
    contact_candidate: WeeklyOpportunityContactCandidate,
) -> OutreachDraft:
    return compose_outreach_draft_fixture(
        company_profile=company_profile,
        opportunity_record=_to_outreach_opportunity(opportunity),
        contact_name=contact_candidate.contact_name or None,
        contact_title=contact_candidate.role_title or None,
        contact_context=_contact_record_from_candidate(contact_candidate),
        email_style_profile=load_style_profile("sample_email_style_profile_anup_approved"),
        recent_signal=opportunity.why_now_signal,
        outreach_goal=WEEKLY_OUTREACH_FALLBACK_GOAL,
    )


def _draft_blockers(
    *,
    approval_state: ApprovalState,
    contact_candidate: WeeklyOpportunityContactCandidate,
    draft: OutreachDraft | None,
) -> list[str]:
    blockers: list[str] = []
    if not state_allows_drafting(approval_state):
        blockers.append("Approve the opportunity for drafting.")
    if contact_candidate.missing_information:
        blockers.extend(contact_candidate.missing_information)
    if draft is not None and not contact_candidate.contact_email:
        blockers.append("Gmail draft creation needs a confirmed recipient email.")
    return list(dict.fromkeys(blockers))


def _review_weekly_item_outputs(
    item: WeeklyOpportunityWorkflowItem,
) -> list[OrchestratorOutputReview]:
    reviews = [
        review_specialist_output(
            agent_name="business_research_analyst",
            request_summary=f"Weekly opportunity research for {item.company_name}.",
            run_type="weekly opportunity workflow",
            output=item.company_profile.model_dump(mode="json"),
        )
    ]
    if item.outreach_draft is not None:
        reviews.append(
            review_specialist_output(
                agent_name="outreach_composer",
                request_summary=f"Weekly outreach draft for {item.company_name}.",
                run_type="weekly opportunity workflow",
                output=item.outreach_draft.model_dump(mode="json"),
            )
        )
    return reviews


def _workflow_provider_performance(metadata_items: list[dict[str, Any]]) -> dict[str, Any]:
    provider_usage: dict[str, dict[str, int | float]] = {}
    providers_used: list[str] = []
    serper_credits = 0
    tavily_credits = 0
    precision_escalations = 0
    provider_fallbacks = 0
    for metadata in metadata_items:
        for provider in metadata.get("search_providers_used") or []:
            provider_name = str(provider)
            if provider_name and provider_name not in providers_used:
                providers_used.append(provider_name)
        usage = metadata.get("provider_usage")
        if isinstance(usage, dict):
            for provider_name, provider_stats in usage.items():
                if not isinstance(provider_stats, dict):
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
                    target[key] = int(target[key]) + int(provider_stats.get(key) or 0)
                provider_seconds = float(provider_stats.get("total_seconds") or 0)
                target["total_seconds"] = round(
                    float(target["total_seconds"]) + provider_seconds,
                    3,
                )
        serper_credits += int(metadata.get("serper_estimated_credits_used") or 0)
        tavily_credits += int(metadata.get("tavily_estimated_credits_used") or 0)
        if metadata.get("precision_search_escalated"):
            precision_escalations += 1
        if metadata.get("provider_error_fallback_used") or metadata.get(
            "search_provider_fallback_used"
        ):
            provider_fallbacks += 1
    return {
        "providers_used": providers_used,
        "provider_usage": provider_usage,
        "serper_estimated_credits_used": serper_credits,
        "tavily_estimated_credits_used": tavily_credits,
        "precision_search_escalation_count": precision_escalations,
        "provider_error_fallback_count": provider_fallbacks,
    }


def _browser_escalation_recommended(metadata_items: list[dict[str, Any]]) -> bool:
    for metadata in metadata_items:
        if metadata.get("structured_enrichment_recommended"):
            return True
        candidates = metadata.get("structured_enrichment_candidates") or []
        if any(str(candidate) == "browserless" for candidate in candidates):
            return True
        website = metadata.get("website_extraction")
        if isinstance(website, dict) and website.get("enabled") and not website.get("page_count"):
            return True
    return False


def _is_unrecoverable_sdk_synthesis_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        AgentRunBudgetExceededError | MissingOpenAIAPIKeyError | ModelProviderConfigurationError,
    )


def run_weekly_opportunity_workflow(
    *,
    topic: str = "Find Keystone-relevant opportunities for weekly review.",
    max_opportunities: int = 5,
    approval_state: ApprovalState | str = ApprovalState.PENDING,
    dry_run: bool = True,
    live_search: bool = False,
    search_provider: str | None = None,
    fallback_search_provider: str | None = None,
    save: bool = False,
    database_url: str | None = None,
    approval_channel: str = "#ai-agents-workflow",
    include_operator_feedback_request: bool = True,
    contact_name: str | None = None,
    contact_title: str | None = None,
    contact_email: str | None = None,
    contact_linkedin_url: str | None = None,
    outreach_channel: OutreachApprovalChannel = "email",
    live_sdk_synthesis: bool = False,
) -> WeeklyOpportunityWorkflowResult:
    """Run the weekly OS -> CR -> OD loop with explicit approval gates."""

    if live_search and dry_run:
        raise RuntimeError("live_search requires dry_run=False for explicit live execution.")
    if max_opportunities < 1:
        raise ValueError("max_opportunities must be at least 1.")
    if outreach_channel not in {"email", "linkedin"}:
        raise ValueError("outreach_channel must be email or linkedin.")

    resolved_approval_state = normalize_approval_state(approval_state)
    orchestrator_request = f"Find Keystone-relevant opportunities for: {topic}"
    orchestrator_decision = route_request(
        {
            "request": orchestrator_request,
            "cadence": "weekly",
            "desired_workflow": [
                "opportunity_scout",
                "business_research_analyst",
                "outreach_composer",
            ],
            "approval_channel": approval_channel,
        },
        include_operator_feedback_request=include_operator_feedback_request,
    )
    audit_notes = [
        "Orchestrator reviewed the weekly opportunity request.",
        "No outbound email is sent by this workflow.",
    ]
    if orchestrator_decision.route != "opportunity_scout":
        audit_notes.append(
            f"Orchestrator route was {orchestrator_decision.route}; weekly loop continues "
            "through Opportunity Scout because the cadence contract is explicit."
        )

    if live_search:
        retrieval_result_limit = max(max_opportunities, 5)
        scout_result, scout_retrieval = run_opportunity_scout_live(
            topic=topic,
            max_results=retrieval_result_limit,
            requested_provider=search_provider,
            fallback_provider=fallback_search_provider,
            retrieval_hint=orchestrator_decision.retrieval_hint,
        )
    else:
        scout_result = scout_opportunities_fixture(
            topic=topic,
            max_results=max_opportunities,
            dry_run=True,
            save=False,
        )
        scout_retrieval = {
            "mode": "fixture",
            "live_search": False,
            "search_provider": "dry-run",
            "retrieval_ladder": [],
        }
    if live_sdk_synthesis:
        try:
            scout_result = _synthesize_weekly_opportunity_scout(
                scout_result,
                topic=topic,
                max_opportunities=max_opportunities,
                retrieval_hint=orchestrator_decision.retrieval_hint,
            )
            audit_notes.append("Opportunity Scout human-facing records synthesized through SDK.")
        except Exception as exc:
            if _is_unrecoverable_sdk_synthesis_error(exc):
                raise
            audit_notes.append(
                "Opportunity Scout live SDK synthesis failed with "
                f"{type(exc).__name__}; retained source-backed retrieval records."
            )

    items: list[WeeklyOpportunityWorkflowItem] = []
    retrieval_metadata: dict[str, Any] = {
        "opportunity_scout": scout_retrieval,
        "company_research": [],
        "provider_performance": _workflow_provider_performance([scout_retrieval]),
    }
    for opportunity in scout_result.records[:max_opportunities]:
        if live_search:
            company_profile, company_retrieval = retrieve_company_profile_live(
                company=opportunity.company_name,
                company_url=_company_url_hint(opportunity),
                request_text=topic,
                requested_provider=search_provider,
                max_results=5,
                retrieval_hint=orchestrator_decision.retrieval_hint,
            )
        else:
            company_profile = research_company_fixture(company_name=opportunity.company_name)
            company_retrieval = {
                "mode": "fixture",
                "live_search": False,
                "search_provider": "dry-run",
                "company": opportunity.company_name,
                "retrieval_ladder": [],
            }
        retrieval_metadata["company_research"].append(company_retrieval)
        company_brief: CompanyResearchFocusedBrief | None = None
        company_brief_audit_note = "Business Research Analyst focused brief was not requested."
        if live_sdk_synthesis:
            try:
                company_brief = _synthesize_weekly_company_brief(company_profile)
                company_brief_audit_note = (
                    "Business Research Analyst focused brief synthesized through SDK."
                )
            except Exception as exc:
                if _is_unrecoverable_sdk_synthesis_error(exc):
                    raise
                company_brief_audit_note = (
                    "Business Research Analyst focused brief SDK synthesis failed with "
                    f"{type(exc).__name__}; source-backed profile retained."
                )
        contact_candidate = _derive_contact_candidate(
            company_profile=company_profile,
            opportunity=opportunity,
            contact_name=contact_name,
            contact_title=contact_title,
            contact_email=contact_email,
            contact_linkedin_url=contact_linkedin_url,
        )
        outreach_draft: OutreachDraft | None = None
        draft_audit_note = "Outreach Composer draft blocked until drafting approval."
        if state_allows_drafting(resolved_approval_state):
            if live_sdk_synthesis:
                try:
                    outreach_draft = _synthesize_weekly_outreach_draft(
                        company_profile=company_profile,
                        opportunity=opportunity,
                        contact_candidate=contact_candidate,
                    )
                    draft_audit_note = "Outreach Composer draft produced for human review."
                except Exception as exc:
                    if _is_unrecoverable_sdk_synthesis_error(exc):
                        raise
                    outreach_draft = _compose_weekly_outreach_fixture_draft(
                        company_profile=company_profile,
                        opportunity=opportunity,
                        contact_candidate=contact_candidate,
                    )
                    draft_audit_note = (
                        "Outreach Composer live SDK draft failed with "
                        f"{type(exc).__name__}; "
                        "deterministic approval-gated fixture draft used instead."
                    )
            else:
                outreach_draft = _compose_weekly_outreach_fixture_draft(
                    company_profile=company_profile,
                    opportunity=opportunity,
                    contact_candidate=contact_candidate,
                )
                draft_audit_note = "Outreach Composer draft produced for human review."
        item = WeeklyOpportunityWorkflowItem(
            company_name=opportunity.company_name,
            opportunity_record=opportunity,
            company_profile=company_profile,
            company_brief=company_brief,
            contact_candidate=contact_candidate,
            source_links=_workflow_source_links(
                opportunity=opportunity,
                company_profile=company_profile,
            ),
            outreach_draft=outreach_draft,
            drafting_approved=state_allows_drafting(resolved_approval_state),
            external_use_approved=state_allows_external_use(resolved_approval_state),
            gmail_draft_ready=bool(outreach_draft and contact_candidate.contact_email),
            missing_information_blockers=_draft_blockers(
                approval_state=resolved_approval_state,
                contact_candidate=contact_candidate,
                draft=outreach_draft,
            ),
            audit_notes=[
                "Business Research Analyst profile and contact candidate produced for review.",
                company_brief_audit_note,
                (
                    draft_audit_note
                    if outreach_draft is not None
                    else "Outreach Composer draft blocked until drafting approval."
                ),
            ],
        )
        item.orchestrator_reviews = _review_weekly_item_outputs(item)
        items.append(item)

    result = WeeklyOpportunityWorkflowResult(
        topic=topic,
        dry_run=dry_run,
        live_search=live_search,
        max_opportunities=max_opportunities,
        orchestrator_decision=orchestrator_decision,
        opportunity_scout=scout_result,
        items=items,
        approval_state=resolved_approval_state,
        approval_channel=approval_channel,
        outreach_channel=outreach_channel,
        live_apis_called=live_search,
        live_sdk_synthesis=live_sdk_synthesis,
        retrieval={
            **retrieval_metadata,
            "provider_performance": _workflow_provider_performance(
                [
                    retrieval_metadata["opportunity_scout"],
                    *retrieval_metadata["company_research"],
                ]
            ),
            "browser_escalation_used": False,
            "browser_escalation_recommended": _browser_escalation_recommended(
                [
                    retrieval_metadata["opportunity_scout"],
                    *retrieval_metadata["company_research"],
                ]
            ),
        },
        audit_notes=audit_notes,
    )
    if save:
        result.storage = save_weekly_opportunity_workflow_result(
            result,
            database_url=database_url,
        )
    return result


def run_opportunity_to_outreach_loop(
    *,
    topic: str = ROBUST_OPPORTUNITY_TO_OUTREACH_TOPIC,
    top_n: int = 3,
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_DRAFTING,
    dry_run: bool = True,
    live_search: bool = False,
    search_provider: str | None = None,
    fallback_search_provider: str | None = None,
    save: bool = False,
    database_url: str | None = None,
    approval_channel: str = "#ai-agents-workflow",
    outreach_channel: OutreachApprovalChannel = "email",
    live_sdk_synthesis: bool = False,
) -> WeeklyOpportunityWorkflowResult:
    """Run the simple Top-N opportunity -> research -> collaboration draft loop."""

    if top_n < 1:
        raise ValueError("top_n must be at least 1.")
    result = run_weekly_opportunity_workflow(
        topic=topic,
        max_opportunities=top_n,
        approval_state=approval_state,
        dry_run=dry_run,
        live_search=live_search,
        search_provider=search_provider,
        fallback_search_provider=fallback_search_provider,
        save=save,
        database_url=database_url,
        approval_channel=approval_channel,
        include_operator_feedback_request=False,
        outreach_channel=outreach_channel,
        live_sdk_synthesis=live_sdk_synthesis,
    )
    return result.model_copy(
        update={
            "cadence": "top_3_opportunity_to_outreach",
            "audit_notes": [
                *result.audit_notes,
                (
                    "Top opportunity-to-outreach loop drafted one collaboration request "
                    "per selected opportunity; no outbound email or Gmail draft was created."
                ),
            ],
        }
    )


def save_weekly_opportunity_workflow_result(
    result: WeeklyOpportunityWorkflowResult,
    *,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Persist weekly loop artifacts and approval queue items."""

    storage = StorageTool(
        database_url,
        agent_name="weekly_opportunity_workflow",
        dry_run=result.dry_run,
    )
    saved: dict[str, Any] = {
        "agent_run": storage.save_agent_run(
            agent_name="weekly_opportunity_workflow",
            input_payload={"topic": result.topic, "max_opportunities": result.max_opportunities},
            input_summary=result.topic,
            output=_weekly_workflow_storage_summary(result),
            model="fixture" if result.dry_run else "live",
            dry_run=result.dry_run,
            status="success",
        ),
        "items": [],
    }
    storage.run_id = saved["agent_run"]["id"]
    saved["opportunity_scout"] = storage.save_opportunity_scout_result(
        result.opportunity_scout,
        status="candidate",
    )
    for item in result.items:
        item_saved: dict[str, Any] = {
            "company": storage.save_company(item.company_profile),
            "opportunity": storage.save_opportunity(item.opportunity_record),
        }
        if item.outreach_draft is not None:
            item_saved["outreach_draft"] = storage.save_outreach_draft(item.outreach_draft)
            approval_item = build_approval_queue_item(
                item.outreach_draft,
                context=_weekly_outreach_approval_context(
                    item=item,
                    outreach_draft_id=item_saved["outreach_draft"]["id"],
                    approval_channel=result.approval_channel,
                    outreach_channel=result.outreach_channel,
                ),
            )
        else:
            approval_item = build_approval_queue_item(
                item.opportunity_record,
                context={
                    "object_type": "opportunity",
                    "object_id": item_saved["opportunity"]["id"],
                    "title": f"Drafting approval: {item.company_name}",
                    "summary": item.opportunity_record.why_now_signal,
                    "decision": ApprovalState.PENDING.value,
                    "scope": ApprovalScope.DRAFTING.value,
                    "source_agent": "weekly_opportunity_workflow",
                    "risk_flags": item.opportunity_record.unsupported_claims_flagged,
                    "metadata": {
                        "approval_channel": result.approval_channel,
                        "contact_candidate": item.contact_candidate.model_dump(mode="json"),
                        "source_links": [
                            link.model_dump(mode="json") for link in item.source_links
                        ],
                        "send_enabled": False,
                    },
                },
            )
        item_saved["approval_queue"] = storage.save_approval_item(approval_item)
        item_saved["approval_queue_item"] = approval_item.model_dump(mode="json")
        saved["items"].append(item_saved)
    return saved


def _weekly_outreach_approval_context(
    *,
    item: WeeklyOpportunityWorkflowItem,
    outreach_draft_id: str | int,
    approval_channel: str,
    outreach_channel: OutreachApprovalChannel,
) -> dict[str, Any]:
    if item.outreach_draft is None:
        raise ValueError("outreach draft is required for outreach approval context")
    gmail_draft_ready = outreach_channel == "email" and item.gmail_draft_ready
    if outreach_channel == "email":
        title = f"Email draft: {item.outreach_draft.email_subject}"
        draft_text = (
            f"Subject: {item.outreach_draft.email_subject}\n\n{item.outreach_draft.email_body}"
        )
    else:
        title = f"LinkedIn draft: {item.company_name}"
        draft_text = item.outreach_draft.linkedin_note
    return {
        "object_type": "outreach_draft",
        "object_id": outreach_draft_id,
        "title": title,
        "summary": title,
        "draft_text": draft_text,
        "decision": ApprovalState.PENDING.value,
        "scope": ApprovalScope.EXTERNAL_USE.value,
        "source_agent": "outreach_composer",
        "risk_flags": item.outreach_draft.unsupported_claims_flagged,
        "metadata": {
            "approval_channel": approval_channel,
            "outreach_channel": outreach_channel,
            "company_name": item.company_name,
            "opportunity_type": item.opportunity_record.opportunity_type,
            "priority_score": item.opportunity_record.priority_score,
            "why_now_signal": item.opportunity_record.why_now_signal,
            "keystone_fit_reason": item.opportunity_record.keystone_fit_reason,
            "opportunity_next_step": item.opportunity_record.recommended_next_step,
            **_company_research_approval_metadata(item),
            "gmail_draft_ready": gmail_draft_ready,
            "slack_approval_allows_gmail_draft_creation": gmail_draft_ready,
            "gmail_draft_account": _gmail_draft_account() if gmail_draft_ready else "",
            "approval_action_label": "save Gmail draft" if gmail_draft_ready else "external use",
            "recipient_email": item.contact_candidate.contact_email,
            "linkedin_url": item.contact_candidate.linkedin_url,
            "contact_name": item.contact_candidate.contact_name,
            "contact_title": item.contact_candidate.role_title,
            "contact_path_type": item.contact_candidate.contact_path_type,
            "contact_path_label": item.contact_candidate.contact_path_label,
            "contact_path_value": item.contact_candidate.contact_path_value,
            "alternate_contact_paths": [
                path.model_dump(mode="json")
                for path in item.contact_candidate.alternate_contact_paths[:5]
            ],
            "email_subject": item.outreach_draft.email_subject,
            "missing_information_blockers": item.missing_information_blockers,
            "send_enabled": False,
        },
    }


def _gmail_draft_account() -> str:
    for key in GMAIL_DRAFT_ACCOUNT_ENV_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return DEFAULT_GMAIL_DRAFT_ACCOUNT


def _company_research_approval_metadata(item: WeeklyOpportunityWorkflowItem) -> dict[str, Any]:
    profile = item.company_profile
    company_website = profile.website or next(
        (link.url for link in item.source_links if link.source_type in {"company_site", "website"}),
        None,
    )
    completed_points = [
        {
            "label": point.label,
            "value": point.value,
            "source_ids": point.source_ids,
        }
        for point in profile.research_data_points
        if point.completed and point.value
    ]
    if not completed_points:
        completed_points = [
            {
                "label": feature.feature_name.replace("_", " ").title(),
                "value": feature.value,
                "source_ids": [feature.source_id],
            }
            for feature in profile.source_backed_features
        ]
    if not completed_points:
        completed_points = [
            {
                "label": claim.claim_type.replace("_", " ").title(),
                "value": claim.claim_text,
                "source_ids": [claim.source_id],
            }
            for claim in profile.claims
            if claim.approved
        ]
    source_quality = (
        profile.source_quality_summary.model_dump(mode="json")
        if (profile.source_quality_summary)
        else {}
    )
    research_completeness = (
        profile.research_completeness.model_dump(mode="json")
        if (profile.research_completeness)
        else {}
    )
    return {
        "company_website": company_website,
        "company_description": profile.description,
        "company_fit_summary": profile.fit_summary,
        "company_scores": {
            "consulting_fit": profile.consulting_fit_score,
            "clinical_ai": profile.clinical_ai_relevance,
            "behavioral_health": profile.behavioral_health_relevance,
            "evidence_generation": profile.evidence_generation_need,
            "outside_consulting": profile.outside_consulting_likelihood,
            "confidence": profile.confidence_score,
        },
        "company_source_quality": source_quality,
        "company_research_completeness": research_completeness,
        "company_research_points": completed_points[:4],
        "company_claims": [
            {
                "text": claim.claim_text,
                "source_id": claim.source_id,
                "confidence": claim.confidence,
                "type": claim.claim_type,
            }
            for claim in profile.claims
            if claim.approved
        ][:4],
        "company_risks": profile.risks[:4],
        "company_missing_information": profile.missing_information[:6],
        "sources": [link.model_dump(mode="json") for link in item.source_links[:6]],
    }


def _weekly_workflow_storage_summary(
    result: WeeklyOpportunityWorkflowResult,
) -> dict[str, Any]:
    """Store reconstructable workflow state without forbidden action strings."""

    return {
        "topic": result.topic,
        "cadence": result.cadence,
        "dry_run": result.dry_run,
        "live_search": result.live_search,
        "max_opportunities": result.max_opportunities,
        "orchestrator": {
            "route": result.orchestrator_decision.route,
            "routing_mode": result.orchestrator_decision.routing_mode,
            "rationale": result.orchestrator_decision.rationale,
            "approval_required": result.orchestrator_decision.approval_required,
        },
        "opportunity_count": len(result.items),
        "approval_state": result.approval_state.value,
        "approval_required": result.approval_required,
        "approval_channel": result.approval_channel,
        "outreach_draft_count": sum(1 for item in result.items if item.outreach_draft),
        "gmail_drafts_created": False,
        "live_apis_called": result.live_apis_called,
        "live_sdk_synthesis": result.live_sdk_synthesis,
        "retrieval": {
            "provider_performance": result.retrieval.get("provider_performance", {}),
            "browser_escalation_recommended": result.retrieval.get(
                "browser_escalation_recommended",
                False,
            ),
            "browser_escalation_used": result.retrieval.get("browser_escalation_used", False),
        },
        "items": [
            {
                "company_name": item.company_name,
                "priority_score": item.opportunity_record.priority_score,
                "opportunity_type": item.opportunity_record.opportunity_type,
                "source_link_count": len(item.source_links),
                "company_brief_present": item.company_brief is not None,
                "contact_candidate": item.contact_candidate.model_dump(mode="json"),
                "draft_created": item.outreach_draft is not None,
                "gmail_draft_ready": item.gmail_draft_ready,
                "missing_information_blockers": item.missing_information_blockers,
                "orchestrator_review_statuses": [
                    review.status for review in item.orchestrator_reviews
                ],
            }
            for item in result.items
        ],
        "audit_notes": result.audit_notes,
    }


def weekly_opportunity_workflow_markdown(
    result: WeeklyOpportunityWorkflowResult,
) -> str:
    """Render a human review packet for the weekly opportunity loop."""

    providers_used = result.retrieval.get("provider_performance", {}).get("providers_used") or [
        "dry-run"
    ]
    lines = [
        "# Weekly Opportunity Workflow",
        "",
        "## Metadata",
        f"- Topic: {result.topic}",
        f"- Cadence: {result.cadence}",
        f"- Dry run: {result.dry_run}",
        f"- Live search: {result.live_search}",
        f"- Live SDK synthesis: {result.live_sdk_synthesis}",
        f"- Approval state: {result.approval_state.value}",
        f"- Approval channel: {result.approval_channel}",
        f"- Send enabled: {result.send_enabled}",
        f"- Gmail drafts created: {result.gmail_drafts_created}",
        f"- Search providers used: {', '.join(providers_used)}",
        (
            "- Browser escalation recommended: "
            f"{result.retrieval.get('browser_escalation_recommended', False)}"
        ),
        "",
        "## Orchestrator",
        f"- Route: {result.orchestrator_decision.route}",
        f"- Routing mode: {result.orchestrator_decision.routing_mode}",
        f"- Rationale: {result.orchestrator_decision.rationale}",
        "",
        "## Opportunities",
    ]
    if not result.items:
        lines.extend(
            [
                "- No opportunities met the current criteria.",
                "",
                "## Feedback Question",
                "Should the scout broaden the criteria, change search lanes, or stay strict?",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    for index, item in enumerate(result.items, start=1):
        candidate = item.contact_candidate
        lines.extend(
            [
                "",
                f"### {index}. {item.company_name}",
                f"- Priority: {item.opportunity_record.priority_score}",
                f"- Opportunity type: {item.opportunity_record.opportunity_type}",
                f"- Why now: {item.opportunity_record.why_now_signal}",
                f"- Keystone fit: {item.opportunity_record.keystone_fit_reason}",
                f"- Recommended next step: {item.opportunity_record.recommended_next_step}",
                "- Source links:",
            ]
        )
        for link in item.source_links[:6]:
            lines.append(f"  - {link.title}: {link.url}")
        if item.company_brief is not None:
            brief = item.company_brief
            lines.extend(
                [
                    "",
                    "#### Company Brief",
                    f"- Product: {brief.product or 'Needs confirmation'}",
                    f"- Customers: {brief.customers or 'Needs confirmation'}",
                    f"- Traction signals: {brief.traction_signals or 'Needs confirmation'}",
                    f"- Leadership: {brief.leadership or 'Needs confirmation'}",
                    f"- Why it may matter: {brief.why_it_matters or 'Needs confirmation'}",
                ]
            )
            if brief.facts:
                lines.append("- Source-backed facts:")
                for fact in brief.facts[:4]:
                    lines.append(f"  - {fact.text}")
            if brief.inferences:
                lines.append("- Inferences:")
                lines.extend(f"  - {inference}" for inference in brief.inferences[:3])
            if brief.unknowns:
                lines.append("- Unknowns:")
                lines.extend(f"  - {unknown}" for unknown in brief.unknowns[:3])
        profile_source = candidate.linkedin_url or candidate.source_url or "Needs confirmation"
        contact_path_label = (
            candidate.contact_path_label or candidate.contact_path_type or "Needs confirmation"
        )
        contact_path_suffix = (
            f" ({candidate.contact_path_value})" if candidate.contact_path_value else ""
        )
        lines.extend(
            [
                "- Contact candidate:",
                f"  - Name: {candidate.contact_name or 'Needs confirmation'}",
                f"  - Role/title: {candidate.role_title or 'Needs confirmation'}",
                f"  - Email: {candidate.contact_email or 'Needs confirmation'}",
                f"  - Profile/source: {profile_source}",
                f"  - Best contact path: {contact_path_label}{contact_path_suffix}",
            ]
        )
        if item.missing_information_blockers:
            lines.append("- Blockers:")
            lines.extend(f"  - {blocker}" for blocker in item.missing_information_blockers)
        if item.outreach_draft is not None:
            lines.extend(
                [
                    "",
                    "#### Outreach Draft",
                    f"Subject: {item.outreach_draft.email_subject}",
                    "",
                    item.outreach_draft.email_body,
                    "",
                    "LinkedIn note:",
                    item.outreach_draft.linkedin_note,
                ]
            )
        review_statuses = ", ".join(review.status for review in item.orchestrator_reviews)
        if review_statuses:
            lines.append(f"- Orchestrator review status: {review_statuses}")

    lines.extend(
        [
            "",
            "## Feedback Question",
            (
                "For each opportunity, should the next step be approve for drafting, revise "
                "research/contact targeting, reject/archive, or approve external use for a "
                "specific draft after edits?"
            ),
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _fallback_company_name(triage: EmailTriageResult) -> str:
    domain = _sender_domain(triage.sender_email)
    if not domain:
        return "Inbound Opportunity"
    label = domain.split(".", 1)[0].replace("-", " ").replace("_", " ").strip()
    return label.title() if label else "Inbound Opportunity"


def _opportunity_type_from_profile(profile: CompanyProfile) -> str:
    if profile.behavioral_health_relevance >= 70:
        return "behavioral health AI"
    if profile.cns_neuro_relevance >= 70:
        return "CNS biotech"
    if profile.clinical_ai_relevance >= 60 or profile.evidence_generation_need >= 60:
        return "trial technology"
    return "grant or collaboration opportunity"


def _fallback_opportunity(profile: CompanyProfile) -> ScoutOpportunityRecord:
    source = profile.sources[0] if profile.sources else None
    priority_score = max(40, min(100, profile.consulting_fit_score or 50))
    why_now = (
        profile.fit_summary
        or profile.description
        or "Inbound business opportunity requires review."
    )
    return ScoutOpportunityRecord(
        company_name=profile.name,
        opportunity_type=_opportunity_type_from_profile(profile),  # type: ignore[arg-type]
        priority_score=priority_score,
        why_now_signal=why_now,
        recommended_next_step="Review the company profile before any draft outreach is used.",
        sources=[
            OpportunitySource(
                title=source.title if source else f"Fixture profile for {profile.name}",
                url=source.url if source else "fixture://pipeline-company-profile",
                source_type="fixture",
                supported_signal=why_now,
            )
        ],
        source_signals=[why_now],
        keystone_fit_reason=(
            "Company profile intersects Keystone focus areas or was supplied through an inbound "
            "business opportunity."
        ),
        outside_consulting_likelihood=max(
            40, min(100, profile.outside_consulting_likelihood or priority_score)
        ),
        handoff_to_business_research_analyst=False,
        outreach_draft=None,
        approval_required_before_outreach=True,
    )


def _select_opportunity(
    *,
    scout_result: OpportunityScoutResult,
    company_profile: CompanyProfile,
) -> ScoutOpportunityRecord:
    for record in scout_result.records:
        if record.company_name.lower() == company_profile.name.lower():
            return record
    if scout_result.records:
        return scout_result.records[0]
    return _fallback_opportunity(company_profile)


def _to_outreach_opportunity(record: ScoutOpportunityRecord) -> OutreachOpportunityRecord:
    source = record.sources[0] if record.sources else None
    return OutreachOpportunityRecord(
        company_name=record.company_name,
        title=f"{record.opportunity_type} opportunity",
        source=source.url if source else "fixture",
        source_id=source.source_id if source else "fixture:pipeline-opportunity",
        notes=record.why_now_signal,
        score=record.priority_score / 100,
        rationale=record.keystone_fit_reason,
        next_step=record.recommended_next_step,
        claims=record.claims,
        unsupported_claims_flagged=record.unsupported_claims_flagged,
    )


def _should_draft(
    triage: EmailTriageResult,
    opportunity: ScoutOpportunityRecord | None,
    approval_state: ApprovalState,
) -> bool:
    if opportunity is None:
        return False
    if triage.category not in BUSINESS_OPPORTUNITY_CATEGORIES:
        return False
    if triage.risk_flags:
        return False
    return triage.needs_reply and state_allows_drafting(approval_state)


def _pipeline_approval_scope(approval_state: ApprovalState) -> ApprovalScope:
    if approval_state == ApprovalState.APPROVED_FOR_RESEARCH:
        return ApprovalScope.RESEARCH
    return ApprovalScope.DRAFTING


def _pipeline_approval_rationale(
    *,
    approval_state: ApprovalState,
    draft_created: bool,
    safe_business_path: bool,
) -> str:
    if not safe_business_path:
        return "No safe business opportunity path was available for outreach drafting."
    if draft_created:
        return (
            "Outreach drafting was allowed by approved_for_drafting; the resulting draft "
            "still requires separate human approval for external use."
        )
    return (
        f"Outreach drafting is blocked because approval state {approval_state.value} does "
        "not grant the drafting checkpoint."
    )


def _pipeline_approval_checkpoints(
    *,
    drafting_state: ApprovalState,
    external_use_state: ApprovalState,
    draft_created: bool,
    safe_business_path: bool,
) -> list[ApprovalCheckpoint]:
    """Return explicit human-control checkpoints for the pipeline."""

    return [
        ApprovalCheckpoint(
            scope=ApprovalScope.RESEARCH,
            state=ApprovalState.APPROVED_FOR_RESEARCH,
            required=False,
            approved=True,
            rationale="Dry-run fixture research is allowed without live integrations.",
        ),
        ApprovalCheckpoint(
            scope=ApprovalScope.DRAFTING,
            state=drafting_state,
            required=safe_business_path,
            approved=state_allows_drafting(drafting_state),
            rationale=("Drafting is allowed only after the opportunity is approved for drafting."),
        ),
        ApprovalCheckpoint(
            scope=ApprovalScope.EXTERNAL_USE,
            state=external_use_state,
            required=draft_created,
            approved=state_allows_external_use(external_use_state),
            rationale=(
                "Draft external use requires a separate approval and never enables "
                "automatic email sending."
            ),
        ),
    ]


def _construct_sdk_agents() -> None:
    build_gmail_triage_agent()
    build_business_research_analyst_agent()
    build_opportunity_scout_agent()
    build_outreach_composer_agent()


def _handoff_metadata(
    result: KeystonePipelineResult,
    contract_name: HandoffContractName,
) -> list[dict[str, Any]]:
    return handoff_contracts_metadata(result.handoff_contracts, contract_name=contract_name)


def _operator_feedback_metadata(
    *,
    result: KeystonePipelineResult,
    object_type: str,
    object_id: str | int,
    source_agent: str,
) -> dict[str, Any] | None:
    if not result.operator_feedback_requests:
        return None
    return build_operator_feedback_request(
        object_type=object_type,
        object_id=object_id,
        source_agent=source_agent,
        review_stage="approval_review",
    ).model_dump(mode="json")


def _approval_review_metadata(
    *,
    result: KeystonePipelineResult,
    object_type: str,
    object_id: str | int,
    source_agent: str,
    contract_name: HandoffContractName,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "send_enabled": False,
        "handoff_contracts": _handoff_metadata(result, contract_name),
    }
    feedback_request = _operator_feedback_metadata(
        result=result,
        object_type=object_type,
        object_id=object_id,
        source_agent=source_agent,
    )
    if feedback_request is not None:
        metadata["operator_feedback_request"] = feedback_request
    return metadata


def _save_pipeline_step_logs(
    *,
    storage: StorageTool,
    result: KeystonePipelineResult,
    run_id: str | int,
    input_summary: str,
) -> list[dict[str, Any]]:
    """Persist reconstructable high-level pipeline step logs."""

    steps: list[dict[str, Any]] = [
        {
            "step_name": "gmail_triage",
            "agent_name": "gmail_triage",
            "input_summary": input_summary,
            "input_payload": {
                "fixture": input_summary,
                "sender_email": result.triage.sender_email,
                "subject": result.triage.subject,
            },
            "output": {
                "category": result.triage.category,
                "needs_reply": result.triage.needs_reply,
                "risk_flags": result.triage.risk_flags,
                "approval_required": result.triage.approval_required,
            },
            "output_summary": (
                f"{result.triage.category}; needs_reply={result.triage.needs_reply}; "
                f"risk_flags={len(result.triage.risk_flags)}"
            ),
        }
    ]
    if result.company_profile is not None:
        steps.append(
            {
                "step_name": "account_research",
                "agent_name": "business_research_analyst",
                "input_summary": result.company_profile.name,
                "input_payload": {"company_name": result.company_profile.name},
                "output": {
                    "company_name": result.company_profile.name,
                    "consulting_fit_score": result.company_profile.consulting_fit_score,
                    "confidence_score": result.company_profile.confidence_score,
                    "source_count": len(result.company_profile.sources),
                    "source_ids": [source.source_id for source in result.company_profile.sources],
                    "unsupported_claims_flagged": (
                        result.company_profile.unsupported_claims_flagged
                    ),
                    "missing_evidence": result.company_profile.missing_evidence,
                    "missing_information": result.company_profile.missing_information,
                    "handoff_contracts": _handoff_metadata(
                        result,
                        "business_research_analyst_to_outreach_composer",
                    ),
                },
                "output_summary": (
                    f"{result.company_profile.name}; fit="
                    f"{result.company_profile.consulting_fit_score}; "
                    f"sources={len(result.company_profile.sources)}"
                ),
            }
        )
    if result.opportunity_record is not None:
        steps.append(
            {
                "step_name": "opportunity_scout",
                "agent_name": "opportunity_scout",
                "input_summary": result.opportunity_record.company_name,
                "input_payload": {"company_name": result.opportunity_record.company_name},
                "output": {
                    "company_name": result.opportunity_record.company_name,
                    "opportunity_type": result.opportunity_record.opportunity_type,
                    "priority_score": result.opportunity_record.priority_score,
                    "handoff_to_business_research_analyst": (
                        result.opportunity_record.handoff_to_business_research_analyst
                    ),
                    "source_ids": [
                        source.source_id for source in result.opportunity_record.sources
                    ],
                    "unsupported_claims_flagged": (
                        result.opportunity_record.unsupported_claims_flagged
                    ),
                    "missing_evidence": result.opportunity_record.missing_evidence,
                    "handoff_contracts": _handoff_metadata(
                        result,
                        "opportunity_scout_to_business_research_analyst",
                    ),
                },
                "output_summary": (
                    f"{result.opportunity_record.company_name}; priority="
                    f"{result.opportunity_record.priority_score}"
                ),
            }
        )
    if result.outreach_draft is not None:
        steps.append(
            {
                "step_name": "outreach_composer",
                "agent_name": "outreach_composer",
                "input_summary": result.outreach_draft.company_name,
                "input_payload": {
                    "company_name": result.outreach_draft.company_name,
                    "approval_scope": result.outreach_draft.approval_scope.value,
                },
                "output": {
                    "company_name": result.outreach_draft.company_name,
                    "email_subject": result.outreach_draft.email_subject,
                    "approval_required": result.outreach_draft.approval_required,
                    "send_enabled": False,
                    "source_ids_used": result.outreach_draft.source_ids_used,
                    "unsupported_claims_flagged": (
                        result.outreach_draft.unsupported_claims_flagged
                    ),
                    "unsupported_claim_explanations": (
                        result.outreach_draft.unsupported_claim_explanations
                    ),
                    "handoff_contracts": _handoff_metadata(
                        result,
                        "outreach_composer_to_orchestrator",
                    ),
                },
                "output_summary": (
                    f"{result.outreach_draft.company_name}; draft_created=true; send_enabled=false"
                ),
            }
        )
    steps.append(
        {
            "step_name": "approval_gate",
            "agent_name": "keystone_pipeline",
            "input_summary": result.approval_state.value,
            "input_payload": {
                "approval_state": result.approval_state.value,
                "approval_required": result.approval_required,
            },
            "output": {
                "approval_required": result.approval_required,
                "send_enabled": result.send_enabled,
                "email_sent": result.email_sent,
            },
            "output_summary": (
                f"approval={result.approval_state.value}; "
                f"send_enabled={result.send_enabled}; email_sent={result.email_sent}"
            ),
        }
    )

    saved_logs: list[dict[str, Any]] = []
    for step in steps:
        saved_logs.append(
            storage.save_agent_run_log(
                run_id=run_id,
                step_name=step["step_name"],
                agent_name=step["agent_name"],
                input_payload=step["input_payload"],
                input_summary=step["input_summary"],
                output=step["output"],
                output_summary=step["output_summary"],
                dry_run=result.dry_run,
                status="success",
            )
        )
    return saved_logs


def save_pipeline_result(
    result: KeystonePipelineResult,
    *,
    database_url: str | None = None,
    input_summary: str = "keystone pipeline dry run",
    create_outreach_tracking: bool = False,
) -> dict[str, Any]:
    """Persist pipeline artifacts explicitly when requested."""

    storage = StorageTool(
        database_url,
        agent_name="keystone_pipeline",
        dry_run=result.dry_run,
    )
    saved: dict[str, Any] = {
        "agent_run": storage.save_agent_run(
            agent_name="keystone_pipeline",
            input_payload={"input_summary": input_summary},
            input_summary=input_summary,
            output=result.model_dump(exclude={"storage"}),
            model="fixture",
            dry_run=result.dry_run,
            status="success",
        )
    }
    storage.run_id = saved["agent_run"]["id"]
    saved["agent_run_logs"] = _save_pipeline_step_logs(
        storage=storage,
        result=result,
        run_id=saved["agent_run"]["id"],
        input_summary=input_summary,
    )
    saved["triage"] = storage.save_email(result.triage)
    if result.company_profile is not None:
        saved["company"] = storage.save_company(result.company_profile)
        company_risk_flags = [
            *result.company_profile.risks,
            *result.company_profile.unsupported_claims_flagged,
        ]
        company_queue_item = build_approval_queue_item(
            result.company_profile,
            context={
                "object_type": "company_profile",
                "object_id": saved["company"]["id"],
                "title": f"Company profile: {result.company_profile.name}",
                "summary": result.company_profile.fit_summary
                or result.company_profile.description
                or f"Review company profile for {result.company_profile.name}.",
                "source_agent": "business_research_analyst",
                "risk_flags": company_risk_flags,
                "metadata": _approval_review_metadata(
                    result=result,
                    object_type="company_profile",
                    object_id=saved["company"]["id"],
                    source_agent="business_research_analyst",
                    contract_name="business_research_analyst_to_outreach_composer",
                ),
            },
        )
        saved.setdefault("approval_queue", {})["company_profile"] = storage.save_approval_item(
            company_queue_item
        )
    if result.opportunity_record is not None:
        saved["opportunity"] = storage.save_opportunity(result.opportunity_record)
        opportunity_queue_item = build_approval_queue_item(
            result.opportunity_record,
            context={
                "object_type": "opportunity",
                "object_id": saved["opportunity"]["id"],
                "title": f"Opportunity: {result.opportunity_record.company_name}",
                "summary": result.opportunity_record.why_now_signal,
                "decision": result.approval_state.value,
                "scope": ApprovalScope.DRAFTING.value,
                "source_agent": "opportunity_scout",
                "risk_flags": result.opportunity_record.unsupported_claims_flagged,
                "metadata": _approval_review_metadata(
                    result=result,
                    object_type="opportunity",
                    object_id=saved["opportunity"]["id"],
                    source_agent="opportunity_scout",
                    contract_name="opportunity_scout_to_business_research_analyst",
                ),
            },
        )
        saved.setdefault("approval_queue", {})["opportunity"] = storage.save_approval_item(
            opportunity_queue_item
        )
    if result.outreach_draft is not None:
        saved["outreach_draft"] = storage.save_outreach_draft(result.outreach_draft)
        if create_outreach_tracking:
            saved["outreach_tracking"] = storage.save_initial_outreach_tracking(
                draft_id=saved["outreach_draft"]["id"],
                draft=result.outreach_draft,
            )
        outreach_queue_item = build_approval_queue_item(
            result.outreach_draft,
            context={
                "object_type": "outreach_draft",
                "object_id": saved["outreach_draft"]["id"],
                "summary": result.outreach_draft.email_subject,
                "decision": ApprovalState.PENDING.value,
                "scope": ApprovalScope.EXTERNAL_USE.value,
                "source_agent": "outreach_composer",
                "risk_flags": result.outreach_draft.unsupported_claims_flagged,
                "metadata": _approval_review_metadata(
                    result=result,
                    object_type="outreach_draft",
                    object_id=saved["outreach_draft"]["id"],
                    source_agent="outreach_composer",
                    contract_name="outreach_composer_to_orchestrator",
                ),
            },
        )
        saved.setdefault("approval_queue", {})["outreach_draft"] = storage.save_approval_item(
            outreach_queue_item
        )
    if result.opportunity_record is not None:
        object_id = (
            saved["opportunity"]["id"]
            if "opportunity" in saved
            else result.opportunity_record.company_name
        )
        saved["approval"] = storage.save_approval(
            object_type="opportunity",
            object_id=object_id,
            decision=result.approval_state.value,
            scope=_pipeline_approval_scope(result.approval_state).value,
            notes="Pipeline approval state for drafting gate.",
            risk_flags=result.opportunity_record.unsupported_claims_flagged,
            source_agent="keystone_pipeline",
        )
    return saved


def run_keystone_pipeline(
    *,
    email_fixture: str | Path,
    company_fixture: str | Path | None = None,
    approval_state: ApprovalState | str = ApprovalState.PENDING,
    dry_run: bool = True,
    sdk: bool = False,
    save: bool = False,
    database_url: str | None = None,
    create_outreach_tracking: bool = False,
    include_operator_feedback_request: bool = False,
    mocked_search_results: list[Any] | None = None,
) -> KeystonePipelineResult:
    """Run the first end-to-end Keystone workflow without live APIs or sending."""

    if not dry_run:
        raise RuntimeError("Live Keystone pipeline execution is not implemented. Use dry-run mode.")
    if create_outreach_tracking and not save:
        raise RuntimeError("create_outreach_tracking requires save=True.")
    if sdk:
        _construct_sdk_agents()
    resolved_approval_state = normalize_approval_state(approval_state)
    if resolved_approval_state in {
        ApprovalState.APPROVED_FOR_EXTERNAL_USE,
        ApprovalState.APPROVED_FOR_SEND,
    }:
        raise RuntimeError(
            "external-use approval does not apply to pipeline drafting; sending is not implemented."
        )

    triage = run_gmail_triage_fixture(email_fixture)
    audit_notes = [
        "Dry-run pipeline only; no live APIs were called.",
        "No email was sent.",
        f"Pipeline approval state: {resolved_approval_state.value}.",
    ]

    company_profile: CompanyProfile | None = None
    opportunity_record: ScoutOpportunityRecord | None = None
    opportunity_scout: OpportunityScoutResult | None = None
    outreach_draft: OutreachDraft | None = None

    safe_business_path = (
        triage.category in BUSINESS_OPPORTUNITY_CATEGORIES and not triage.risk_flags
    )
    if safe_business_path:
        company_profile = research_company_fixture(
            company_name=_fallback_company_name(triage),
            fixture_json=Path(company_fixture) if company_fixture else None,
            search_results=mocked_search_results,
        )
        opportunity_scout = scout_opportunities_fixture(
            topic=company_profile.name,
            max_results=1,
            dry_run=True,
            save=False,
        )
        opportunity_record = _select_opportunity(
            scout_result=opportunity_scout,
            company_profile=company_profile,
        )
        if _should_draft(triage, opportunity_record, resolved_approval_state):
            outreach_draft = compose_outreach_draft_fixture(
                company_profile=company_profile,
                opportunity_record=_to_outreach_opportunity(opportunity_record),
                contact_name=triage.sender_name or None,
                recent_signal=opportunity_record.why_now_signal,
                outreach_goal="respond to the inbound business opportunity after human approval",
            )
        else:
            audit_notes.append(
                f"Approval state {resolved_approval_state.value} does not allow drafting."
            )
    else:
        audit_notes.append("Triage did not identify a safe business opportunity path for drafting.")

    approval_rationale = _pipeline_approval_rationale(
        approval_state=resolved_approval_state,
        draft_created=outreach_draft is not None,
        safe_business_path=safe_business_path,
    )
    approval_checkpoints = _pipeline_approval_checkpoints(
        drafting_state=resolved_approval_state,
        external_use_state=ApprovalState.PENDING,
        draft_created=outreach_draft is not None,
        safe_business_path=safe_business_path,
    )
    audit_notes.append(approval_rationale)
    handoff_contracts = validate_pipeline_handoff_contracts(
        company_profile=company_profile,
        opportunity_record=opportunity_record,
        outreach_draft=outreach_draft,
    )
    raise_for_invalid_handoffs(handoff_contracts)
    operator_feedback_requests: list[OperatorFeedbackRequest] = []
    if include_operator_feedback_request and company_profile is not None:
        operator_feedback_requests.append(
            build_operator_feedback_request(
                object_type="company_profile",
                object_id=company_profile.name,
                source_agent="business_research_analyst",
                review_stage="pipeline_review",
            )
        )
    if include_operator_feedback_request and opportunity_record is not None:
        operator_feedback_requests.append(
            build_operator_feedback_request(
                object_type="opportunity",
                object_id=opportunity_record.company_name,
                source_agent="opportunity_scout",
                review_stage="pipeline_review",
            )
        )
    if include_operator_feedback_request and outreach_draft is not None:
        operator_feedback_requests.append(
            build_operator_feedback_request(
                object_type="outreach_draft",
                object_id=outreach_draft.company_name,
                source_agent="outreach_composer",
                review_stage="approval_review",
            )
        )
    if handoff_contracts:
        audit_notes.append(f"Validated {len(handoff_contracts)} cross-agent handoff contract(s).")
    if operator_feedback_requests:
        audit_notes.append(
            f"Attached {len(operator_feedback_requests)} optional operator feedback request(s)."
        )

    result = KeystonePipelineResult(
        dry_run=True,
        sdk_agents_constructed=sdk,
        triage=triage,
        company_profile=company_profile,
        opportunity_record=opportunity_record,
        outreach_draft=outreach_draft,
        opportunity_scout=opportunity_scout,
        approval_state=resolved_approval_state,
        research_approval_state=ApprovalState.APPROVED_FOR_RESEARCH,
        drafting_approval_state=resolved_approval_state,
        external_use_approval_state=ApprovalState.PENDING,
        approval_scope=ApprovalScope.DRAFTING,
        approval_rationale=approval_rationale,
        approval_checkpoints=approval_checkpoints,
        drafting_approved=state_allows_drafting(resolved_approval_state),
        external_use_approved=False,
        approval_required=bool(
            triage.approval_required
            or not state_allows_drafting(resolved_approval_state)
            or outreach_draft is not None
        ),
        email_sent=False,
        send_enabled=False,
        live_apis_called=False,
        handoff_contracts=handoff_contracts,
        operator_feedback_requests=operator_feedback_requests,
        audit_notes=audit_notes,
    )
    if save:
        result.storage = save_pipeline_result(
            result,
            database_url=database_url,
            input_summary=f"pipeline from {Path(email_fixture).name}",
            create_outreach_tracking=create_outreach_tracking,
        )
    return result


def pipeline_markdown_report(result: KeystonePipelineResult) -> str:
    """Render an approval-gated markdown report with no send action."""

    return render_pipeline_report(result)
