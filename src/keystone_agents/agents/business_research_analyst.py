"""Account business research analyst builder."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from keystone_agents.business_research_analyst.context import (
    coerce_contact_context as _coerce_contact_context,
)
from keystone_agents.business_research_analyst.context import (
    coerce_crm_context as _coerce_crm_context,
)
from keystone_agents.business_research_analyst.context import (
    read_fixture_json as _read_fixture_json,
)
from keystone_agents.company_research import (
    build_llm_ready_source_bundle,
    compare_company_profiles,
    refresh_company_profile_trust,
    research_company_fixture,
)
from keystone_agents.company_research import (
    synthesize_company_profile_from_source_bundle as synthesize_profile_from_bundle,
)
from keystone_agents.file_search import append_configured_file_search_tools
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.models import (
    BusinessResearchComparisonSDKInput,
    BusinessResearchFocusedBriefSDKInput,
    BusinessResearchSDKInput,
    ResearchSDKInput,
    TypedAgentRunResult,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.company_profile import (
    DEFAULT_RESEARCH_DATA_POINT_KEYS,
    ClaimEvidenceRecord,
    CompanyProfile,
    CompanyResearchComparison,
    CompanyResearchFocusedBrief,
    ResearchDataPoint,
    SourceRecord,
    research_data_point_label,
)
from keystone_agents.schemas.contact_context import ContactRecord, CRMAccountContext
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions
from keystone_agents.source_enrichment import (
    dedupe_and_rank_source_records,
    normalize_source_record,
)
from keystone_agents.tools.apify_tool import fetch_linkedin_or_profile_placeholder
from keystone_agents.tools.browser_diagnostics_tool import (
    capture_browser_diagnostics,
    summarize_rendered_page_diagnostics,
)
from keystone_agents.tools.browserless_tool import extract_company_signals, fetch_company_page
from keystone_agents.tools.html_review_tool import extract_research_claims_from_html
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
    save_company_profile_memory,
    save_retrieval_tool_performance_memory,
)
from keystone_agents.tools.playwright_tool import render_page
from keystone_agents.tools.serper_tool import SearchResult, search_web
from keystone_agents.tools.storage_tool import (
    load_approved_contact_context,
    load_approved_crm_context,
)
from keystone_agents.tools.web_structuring_tool import structure_web_data_for_schema

DEFAULT_CONTACT_FIXTURE = "sample_contact_curebase_approved"
DEFAULT_CRM_CONTEXT_FIXTURE = "sample_crm_context_curebase"


def _with_tool_name(func: Any) -> Any:
    func.name = func.__name__
    return func


def _append_source_once(sources: list[SourceRecord], source: SourceRecord) -> None:
    if source.source_id not in {existing.source_id for existing in sources}:
        sources.append(source)


def _append_claims(
    claims: list[ClaimEvidenceRecord],
    evidence: list[str],
    context_claims: list[ClaimEvidenceRecord],
) -> None:
    existing_claims = {claim.claim_text for claim in claims}
    for claim in context_claims:
        if claim.claim_text not in existing_claims:
            claims.append(claim)
            existing_claims.add(claim.claim_text)
        if claim.claim_text not in evidence:
            evidence.append(claim.claim_text)


def _refresh_research_data_points(profile: CompanyProfile) -> list[ResearchDataPoint]:
    refreshed = research_company_fixture(
        company_name=profile.name,
        company_url=profile.website,
        lead_name=profile.lead_name,
        linkedin_url=profile.linkedin_url,
        fixture_json=profile.model_dump(mode="json"),
    )
    refreshed_by_key = {data_point.key: data_point for data_point in refreshed.research_data_points}
    existing_by_key = {data_point.key: data_point for data_point in profile.research_data_points}
    merged: list[ResearchDataPoint] = []
    for key in DEFAULT_RESEARCH_DATA_POINT_KEYS:
        existing = existing_by_key.get(key)
        refreshed_point = refreshed_by_key.get(key)
        if existing is not None and existing.completed:
            merged.append(existing)
        elif refreshed_point is not None:
            merged.append(refreshed_point)
        elif existing is not None:
            merged.append(existing)
        else:
            merged.append(
                ResearchDataPoint(
                    key=key,
                    label=research_data_point_label(key),
                    missing_reason="No source-backed value available.",
                )
            )
    return merged


def build_company_research_queries(company: str, company_url: str | None = None) -> list[str]:
    """Build targeted Serper queries for source-attributed company research."""

    normalized_company = company.strip()
    company_domain = _company_domain_for_search(company_url)
    queries = [
        f"{normalized_company} official website",
        f"{normalized_company} about product platform",
        f"{normalized_company} LinkedIn company profile",
        f"site:linkedin.com/company {normalized_company}",
        f"{normalized_company} recent news funding",
        f"{normalized_company} funding valuation headcount providers clinicians",
        f"{normalized_company} business model customers payer provider employer",
        f"{normalized_company} partners customers health systems employers payer",
        f"{normalized_company} leadership founders executives",
        f"{normalized_company} contact email business development partnerships",
        f"site:prnewswire.com {normalized_company} funding partnership",
        f"site:businesswire.com {normalized_company} funding partnership",
        f"site:fiercehealthcare.com {normalized_company} funding mental health",
        f"site:hitconsultant.net {normalized_company} funding partnership mental health",
        f"site:mobihealthnews.com {normalized_company} funding partnership mental health",
        f"site:behavolve.com {normalized_company} mental health validation",
        (
            f"{normalized_company} clinical validation trials outcomes payer partnerships "
            "product launch"
        ),
        f"{normalized_company} peer reviewed validation study outcomes",
        f"{normalized_company} differentiator unique approach diagnostic evaluation",
    ]
    if company_domain:
        queries.insert(0, f"site:{company_domain} {normalized_company} about product diagnostic")
        queries.insert(
            1,
            f"site:{company_domain} {normalized_company} employers partners health systems",
        )
        queries.insert(
            2,
            f"site:{company_domain} {normalized_company} press release funding validation",
        )
        queries.insert(3, f"site:{company_domain} {normalized_company} contact leadership")
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _company_domain_for_search(company_url: str | None) -> str:
    if not company_url:
        return ""
    parsed = urlparse(company_url if "://" in company_url else f"https://{company_url}")
    return parsed.netloc.removeprefix("www.")


@_with_tool_name
def load_contact_context(fixture: str = DEFAULT_CONTACT_FIXTURE) -> ContactRecord:
    """Load local contact context from a JSON fixture without live CRM calls."""

    return ContactRecord.model_validate(_read_fixture_json(fixture))


@_with_tool_name
def load_crm_account_context(fixture: str = DEFAULT_CRM_CONTEXT_FIXTURE) -> CRMAccountContext:
    """Load local CRM/account context from a JSON fixture without live CRM calls."""

    return CRMAccountContext.model_validate(_read_fixture_json(fixture))


def apply_local_account_context(
    profile: CompanyProfile,
    *,
    contact_context: ContactRecord | dict[str, Any] | None = None,
    crm_context: CRMAccountContext | dict[str, Any] | None = None,
) -> CompanyProfile:
    """Attach approved local contact and CRM context to a source-backed profile."""

    contact_record = _coerce_contact_context(contact_context)
    crm_record = _coerce_crm_context(crm_context)
    sources = list(profile.sources)
    claims = list(profile.claims)
    evidence = list(profile.evidence)
    unsupported = list(profile.unsupported_claims_flagged)
    lead_name = profile.lead_name

    if contact_record is not None:
        if contact_record.approved_for_personalization and contact_record.source_backed_claims():
            _append_source_once(sources, contact_record.to_source_record())
            _append_claims(claims, evidence, contact_record.source_backed_claims())
            lead_name = lead_name or contact_record.contact_name
        else:
            unsupported.append(f"unapproved contact context ignored: {contact_record.contact_name}")
        unsupported.extend(contact_record.unsupported_claims_flagged)

    if crm_record is not None:
        if crm_record.approved_for_personalization and crm_record.source_backed_claims():
            _append_source_once(sources, crm_record.to_source_record())
            _append_claims(claims, evidence, crm_record.source_backed_claims())
        else:
            unsupported.append(f"unapproved CRM account context ignored: {crm_record.company_name}")
        unsupported.extend(crm_record.unsupported_claims_flagged)

    payload = profile.model_dump(mode="python")
    payload.update(
        {
            "lead_name": lead_name,
            "sources": sources,
            "claims": claims,
            "evidence": list(dict.fromkeys(evidence)),
            "unsupported_claims_flagged": list(dict.fromkeys(unsupported)),
        }
    )
    refreshed_points = _refresh_research_data_points(CompanyProfile.model_validate(payload))
    payload["research_data_points"] = refreshed_points
    return refresh_company_profile_trust(CompanyProfile.model_validate(payload))


def research_account_from_search_results(
    *,
    company_name: str,
    company_url: str | None = None,
    search_results: list[SearchResult] | None = None,
    website_inputs: list[dict[str, object]] | None = None,
    profile_inputs: list[dict[str, object]] | None = None,
    contact_context: ContactRecord | dict[str, Any] | None = None,
    crm_context: CRMAccountContext | dict[str, Any] | None = None,
) -> CompanyProfile:
    """Build a CompanyProfile from already-fetched, source-linked search results."""

    profile = research_company_fixture(
        company_name=company_name,
        company_url=company_url,
        search_results=search_results,
        website_inputs=website_inputs,
        profile_inputs=profile_inputs,
    )
    return apply_local_account_context(
        profile,
        contact_context=contact_context,
        crm_context=crm_context,
    )


@_with_tool_name
def compare_company_profiles_for_decision(
    primary_profile: CompanyProfile | dict[str, Any],
    comparison_profile: CompanyProfile | dict[str, Any],
    *,
    decision_goal: str | None = None,
    criteria: list[str] | str | None = None,
    decision_criteria: list[str] | tuple[str, ...] | str | None = None,
    requested_output_format: str | None = None,
) -> CompanyResearchComparison:
    """Compare two source-backed company profiles in a decision-oriented format."""

    primary = CompanyProfile.model_validate(primary_profile)
    comparison = CompanyProfile.model_validate(comparison_profile)
    resolved_criteria = criteria if criteria is not None else decision_criteria
    return compare_company_profiles(
        primary,
        comparison,
        decision_goal=decision_goal,
        criteria=resolved_criteria,
        requested_output_format=requested_output_format,
    )


@_with_tool_name
def dedupe_and_rank_sources(
    source_payloads: list[dict[str, Any]],
    company_name: str,
    company_url: str | None = None,
    max_sources: int = 8,
) -> list[dict[str, Any]]:
    """Normalize, dedupe, and rank source records without network calls."""

    sources: list[SourceRecord] = []
    for index, payload in enumerate(source_payloads, start=1):
        source = normalize_source_record(
            payload,
            company_name=company_name,
            company_url=company_url,
            default_source_id=f"source:{index}",
            default_source_type=str(payload.get("source_type") or "unknown"),
        )
        if source is not None:
            sources.append(source)
    ranked = dedupe_and_rank_source_records(
        sources,
        company_url=company_url,
        max_sources=max_sources,
    )
    return [source.model_dump(mode="json") for source in ranked]


@_with_tool_name
def build_source_bundle_for_synthesis(
    source_payloads: list[dict[str, Any]],
    company_name: str,
    company_url: str | None = None,
    max_sources: int = 8,
) -> dict[str, Any]:
    """Create an LLM-ready source bundle from normalized source records only."""

    ranked_sources = [
        SourceRecord.model_validate(source)
        for source in dedupe_and_rank_sources(
            source_payloads=source_payloads,
            company_name=company_name,
            company_url=company_url,
            max_sources=max_sources,
        )
    ]
    bundle = build_llm_ready_source_bundle(
        company_name=company_name,
        company_url=company_url,
        sources=ranked_sources,
        max_sources=max_sources,
    )
    return bundle.model_dump(mode="json")


@_with_tool_name
def synthesize_company_profile_from_source_bundle(
    company_name: str,
    source_bundle: dict[str, Any],
    company_url: str | None = None,
    linkedin_url: str | None = None,
) -> CompanyProfile:
    """Synthesize a profile from source records only, without invented facts."""

    return synthesize_profile_from_bundle(
        company_name=company_name,
        company_url=company_url,
        linkedin_url=linkedin_url,
        source_bundle=source_bundle,
    )


def build_business_research_analyst_agent(model: str | None = None) -> Agent:
    """Build the business research analyst agent."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
        "local_context.md",
        "business_research_analyst.md",
    )
    return build_sdk_agent(
        name="business_research_analyst",
        instructions=instructions,
        output_type=CompanyProfile,
        tools=_business_research_analyst_company_profile_tools(),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="business_research_analyst",
        handoff_description=(
            "Use for source-attributed company, account, contact, and CRM context research "
            "with Keystone fit scoring."
        ),
    )


def _business_research_analyst_tools() -> list[Any]:
    return append_configured_file_search_tools(
        "business_research_analyst",
        [
            load_contact_context,
            load_crm_account_context,
            load_approved_contact_context,
            load_approved_crm_context,
            list_local_context_sources,
            search_local_context,
            read_local_context_file,
            retrieve_memory,
            check_workflow_duplicate,
            airtable_get_base_schema,
            airtable_read_records,
            airtable_write_record,
            search_web,
            fetch_company_page,
            extract_research_claims_from_html,
            structure_web_data_for_schema,
            render_page,
            capture_browser_diagnostics,
            summarize_rendered_page_diagnostics,
            fetch_linkedin_or_profile_placeholder,
            extract_company_signals,
            dedupe_and_rank_sources,
            build_source_bundle_for_synthesis,
            synthesize_company_profile_from_source_bundle,
            compare_company_profiles_for_decision,
            save_company_profile_memory,
            save_retrieval_tool_performance_memory,
            *google_workspace_tools(),
        ],
    )


def _business_research_analyst_company_profile_tools() -> list[Any]:
    return append_configured_file_search_tools(
        "business_research_analyst",
        [
            load_contact_context,
            load_crm_account_context,
            load_approved_contact_context,
            load_approved_crm_context,
            list_local_context_sources,
            search_local_context,
            read_local_context_file,
            retrieve_memory,
            check_workflow_duplicate,
            airtable_get_base_schema,
            airtable_read_records,
            airtable_write_record,
            search_web,
            fetch_company_page,
            extract_research_claims_from_html,
            structure_web_data_for_schema,
            render_page,
            capture_browser_diagnostics,
            summarize_rendered_page_diagnostics,
            fetch_linkedin_or_profile_placeholder,
            extract_company_signals,
            dedupe_and_rank_sources,
            build_source_bundle_for_synthesis,
            synthesize_company_profile_from_source_bundle,
            compare_company_profiles_for_decision,
            save_company_profile_memory,
            *google_workspace_tools(),
        ],
    )


def build_business_research_analyst_focused_brief_agent(model: str | None = None) -> Agent:
    """Build Business Research Analyst for BR-1 focused brief synthesis."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
        "local_context.md",
        "business_research_analyst.md",
    )
    return build_sdk_agent(
        name="business_research_analyst",
        instructions=instructions,
        output_type=CompanyResearchFocusedBrief,
        tools=_business_research_analyst_tools(),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="business_research_analyst",
        handoff_description=(
            "Use for concise source-cited company research briefs for Keystone "
            "partnership or advisory relevance."
        ),
    )


def build_business_research_analyst_comparison_agent(model: str | None = None) -> Agent:
    """Build Business Research Analyst for source-backed company comparison synthesis."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
        "local_context.md",
        "business_research_analyst.md",
    )
    return build_sdk_agent(
        name="business_research_analyst",
        instructions=instructions,
        output_type=CompanyResearchComparison,
        tools=_business_research_analyst_tools(),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="business_research_analyst",
        handoff_description=(
            "Use for concise source-backed side-by-side company comparisons for "
            "Keystone partnership, advisory, or outreach prioritization decisions."
        ),
    )


def build_business_research_analyst_research_brief_agent(model: str | None = None) -> Agent:
    """Build the broader Business Research Analyst for non-company research briefs."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
        "local_context.md",
        "business_research_analyst.md",
    )
    return build_sdk_agent(
        name="business_research_analyst",
        instructions=instructions,
        output_type=ResearchBrief,
        tools=_business_research_analyst_tools(),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="business_research_analyst",
        handoff_description=(
            "Use for source-attributed research on companies, institutes, conferences, "
            "labs, people, topics, Zotero collections, and article collections."
        ),
    )


def focused_brief_context_from_profile(profile: CompanyProfile) -> str:
    """Return compact source-backed context for an LLM-generated BR-1 brief."""

    payload = {
        "company_name": profile.name,
        "website": profile.website,
        "description": profile.description,
        "fit_summary": profile.fit_summary,
        "scores": {
            "behavioral_health_relevance": profile.behavioral_health_relevance,
            "clinical_ai_relevance": profile.clinical_ai_relevance,
            "cns_neuro_relevance": profile.cns_neuro_relevance,
            "evidence_generation_need": profile.evidence_generation_need,
            "outside_consulting_likelihood": profile.outside_consulting_likelihood,
            "consulting_fit_score": profile.consulting_fit_score,
            "confidence_score": profile.confidence_score,
        },
        "facts": [claim.model_dump(mode="json") for claim in profile.claims],
        "research_data_points": [
            data_point.model_dump(mode="json") for data_point in profile.research_data_points
        ],
        "sources": [
            {
                "source_id": source.source_id,
                "title": source.title,
                "url": source.url,
                "source_type": source.source_type,
                "supported_claims": source.supported_claims,
                "confidence": source.confidence,
            }
            for source in profile.sources
        ],
        "contradictions": profile.contradictions,
        "missing_evidence": profile.missing_evidence,
        "missing_information": profile.missing_information,
        "risks": profile.risks,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def focused_brief_input_from_profile(
    profile: CompanyProfile,
    *,
    brief_goal: str | None = None,
) -> BusinessResearchFocusedBriefSDKInput:
    """Build typed SDK input for an LLM-focused company research brief."""

    return BusinessResearchFocusedBriefSDKInput(
        company_name=profile.name,
        company_url=profile.website,
        source_context=focused_brief_context_from_profile(profile),
        brief_goal=brief_goal
        or (
            "Prepare a concise research brief on the company for possible partnership "
            "or advisory relevance to Keystone. Focus on product, buyer segments, traction "
            "signals, leadership, and why it may matter."
        ),
    )


def comparison_context_from_result(comparison: CompanyResearchComparison) -> str:
    """Return compact structured evidence for an LLM-generated company comparison."""

    def evidence_gaps_for_prompt() -> list[str]:
        gaps: list[str] = []
        for gap in comparison.evidence_gaps:
            lowered = gap.lower()
            mentions_a = comparison.company_a.name.lower() in lowered
            mentions_b = comparison.company_b.name.lower() in lowered
            both_companies = "both companies" in lowered
            if "website url not supplied" in lowered:
                if both_companies and comparison.company_a.website and comparison.company_b.website:
                    continue
                if mentions_a and comparison.company_a.website:
                    continue
                if mentions_b and comparison.company_b.website:
                    continue
            if "linkedin or profile url not supplied" in lowered:
                if (
                    both_companies
                    and comparison.company_a.linkedin_url
                    and comparison.company_b.linkedin_url
                ):
                    continue
                if mentions_a and comparison.company_a.linkedin_url:
                    continue
                if mentions_b and comparison.company_b.linkedin_url:
                    continue
            gaps.append(gap)
        return gaps

    def profile_missing_information(profile: CompanyProfile) -> list[str]:
        missing: list[str] = []
        for item in profile.missing_information:
            lowered = item.lower()
            if "company website url not supplied" in lowered and profile.website:
                continue
            if "linkedin or profile url not supplied" in lowered and profile.linkedin_url:
                continue
            missing.append(item)
        return missing

    def profile_payload(profile: CompanyProfile) -> dict[str, Any]:
        return {
            "name": profile.name,
            "website": profile.website,
            "linkedin_url": profile.linkedin_url,
            "description": profile.description,
            "fit_summary": profile.fit_summary,
            "scores": {
                "behavioral_health_relevance": profile.behavioral_health_relevance,
                "clinical_ai_relevance": profile.clinical_ai_relevance,
                "cns_neuro_relevance": profile.cns_neuro_relevance,
                "evidence_generation_need": profile.evidence_generation_need,
                "outside_consulting_likelihood": profile.outside_consulting_likelihood,
                "consulting_fit_score": profile.consulting_fit_score,
                "confidence_score": profile.confidence_score,
                "source_quality_score": (
                    profile.source_quality_summary.overall_score
                    if profile.source_quality_summary
                    else None
                ),
                "research_completeness_score": (
                    profile.research_completeness.score if profile.research_completeness else None
                ),
            },
            "facts": [claim.model_dump(mode="json") for claim in profile.claims[:16]],
            "research_data_points": [
                data_point.model_dump(mode="json") for data_point in profile.research_data_points
            ],
            "sources": [
                {
                    "source_id": source.source_id,
                    "title": source.title,
                    "url": source.url,
                    "source_type": source.source_type,
                    "supported_claims": source.supported_claims[:6],
                    "confidence": source.confidence,
                }
                for source in profile.sources[:12]
            ],
            "missing_information": profile_missing_information(profile),
            "missing_evidence": profile.missing_evidence,
            "risks": profile.risks,
        }

    payload = {
        "decision_goal": comparison.decision_goal,
        "decision_criteria": comparison.decision_criteria,
        "requested_output_format": comparison.requested_output_format,
        "deterministic_baseline": {
            "recommended_company": comparison.recommended_company,
            "recommendation": comparison.recommendation,
            "next_step": comparison.next_step,
            "evidence_gaps": evidence_gaps_for_prompt(),
            "side_by_side_entries": [
                entry.model_dump(mode="json") for entry in comparison.side_by_side_entries
            ],
        },
        "company_a": profile_payload(comparison.company_a),
        "company_b": profile_payload(comparison.company_b),
        "stage_data_checks": {
            "company_a_source_count": len(comparison.company_a.sources),
            "company_b_source_count": len(comparison.company_b.sources),
            "company_a_claim_count": len(comparison.company_a.claims),
            "company_b_claim_count": len(comparison.company_b.claims),
            "comparison_entry_count": len(comparison.side_by_side_entries),
            "evidence_gap_count": len(evidence_gaps_for_prompt()),
        },
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def comparison_input_from_result(
    comparison: CompanyResearchComparison,
    *,
    retrieval_hint: Any | None = None,
) -> BusinessResearchComparisonSDKInput:
    """Build typed SDK input for an LLM-synthesized company comparison."""

    return BusinessResearchComparisonSDKInput(
        company_a=comparison.company_a.name,
        company_b=comparison.company_b.name,
        decision_goal=comparison.decision_goal,
        decision_criteria=tuple(comparison.decision_criteria),
        requested_output_format=comparison.requested_output_format,
        source_context=comparison_context_from_result(comparison),
        retrieval_hint=retrieval_hint,
    )


def run_business_research_analyst_sdk(
    typed_input: BusinessResearchSDKInput | str,
    *,
    run_config: object | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
) -> TypedAgentRunResult[CompanyProfile]:
    """Run Business Research Analyst through the typed SDK harness."""

    return run_typed_sdk_agent(
        agent=build_business_research_analyst_agent(model=model),
        typed_input=typed_input,
        output_type=CompanyProfile,
        run_config=run_config,
        live=live,
        session=session,
    )


def run_business_research_analyst_focused_brief_sdk(
    typed_input: BusinessResearchFocusedBriefSDKInput | str,
    *,
    run_config: object | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
) -> TypedAgentRunResult[CompanyResearchFocusedBrief]:
    """Run Business Research Analyst through the SDK for a BR-1 focused brief."""

    return run_typed_sdk_agent(
        agent=build_business_research_analyst_focused_brief_agent(model=model),
        typed_input=typed_input,
        output_type=CompanyResearchFocusedBrief,
        run_config=run_config,
        live=live,
        session=session,
    )


def run_business_research_analyst_research_brief_sdk(
    typed_input: ResearchSDKInput | str,
    *,
    run_config: object | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
) -> TypedAgentRunResult[ResearchBrief]:
    """Run the broader Business Research Analyst through the typed SDK harness."""

    return run_typed_sdk_agent(
        agent=build_business_research_analyst_research_brief_agent(model=model),
        typed_input=typed_input,
        output_type=ResearchBrief,
        run_config=run_config,
        live=live,
        session=session,
    )
