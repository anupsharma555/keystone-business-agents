"""Pre-retrieval planning for Opportunity Scout."""

from __future__ import annotations

from typing import Any

from keystone_agents.schemas.opportunity_search_plan import (
    OpportunitySearchLane,
    OpportunitySearchPlan,
)


def infer_opportunity_search_plan(
    topic: str | None,
    *,
    desired_count: int = 5,
    source: str = "heuristic",
) -> OpportunitySearchPlan:
    """Infer a structured search plan from an operator request.

    This is the local fallback for the optional live LLM planner. It intentionally
    returns a schema that Python can enforce instead of scattering intent checks
    throughout query construction and filtering.
    """

    text = " ".join(str(topic or "").split()).strip()
    lowered = text.lower()
    plan = OpportunitySearchPlan(
        source=source,
        desired_count=desired_count,
        domains=_domains_from_topic(lowered),
        rationale=(
            "Local semantic fallback inferred target entities and objectives from the request."
        ),
    )

    if _mixed_meeting_grant_intent(lowered):
        return _meeting_grant_search_plan(plan)

    if _broad_intent(lowered):
        return _broad_search_plan(plan)

    if _grant_only_intent(lowered):
        return _grant_only_search_plan(plan)

    if _formal_opportunity_intent(lowered):
        return _formal_opportunity_search_plan(plan)

    if _industry_services_intent(lowered):
        return _industry_services_search_plan(plan)

    if _professional_development_intent(lowered):
        return _professional_development_search_plan(plan)

    if _github_repository_intent(lowered):
        plan.target_entity_types = ["github_repository"]
        plan.objectives = ["open_source_tooling"]
        plan.must_include_terms = ["github", "open source", "repository", "license"]
        plan.exclude_entity_types = [
            "company",
            "institute",
            "researcher",
            "conference",
            "journal_call",
            "contract_rfp",
            "grant_program",
            "trial",
            "role",
        ]
        plan.strict_targeting = True
        return plan

    if _conference_intent(lowered):
        plan.target_entity_types = ["conference"]
        plan.objectives = ["presentation_opportunity"]
        plan.must_include_terms = ["conference", "presentation", "speaker", "workshop"]
        plan.exclude_entity_types = [
            "company",
            "institute",
            "researcher",
            "grant_program",
            "trial",
            "role",
        ]
        plan.strict_targeting = True
        return plan

    if _journal_call_intent(lowered):
        plan.target_entity_types = ["journal_call"]
        plan.objectives = ["journal_article_call", "research_collaboration"]
        plan.must_include_terms = [
            "call for papers",
            "special issue",
            "journal",
            "article",
            "manuscript",
        ]
        plan.exclude_entity_types = ["company", "conference", "role"]
        plan.strict_targeting = True
        return plan

    if _contract_rfp_intent(lowered):
        plan.target_entity_types = ["contract_rfp"]
        plan.objectives = ["contract_opportunity", "broad_discovery"]
        plan.must_include_terms = ["RFP", "contract", "solicitation", "proposal", "procurement"]
        plan.exclude_entity_types = ["researcher", "conference", "role"]
        plan.strict_targeting = True
        return plan

    if _broad_intent(lowered):
        return _broad_search_plan(plan)

    if _company_growth_intent(lowered):
        plan.target_entity_types = ["company"]
        plan.objectives = [
            "company_growth",
            "advisory" if "advisory" in lowered else "broad_discovery",
        ]
        plan.must_include_terms = ["funding", "partnership", "launch", "clinical AI"]
        _apply_company_only_targeting(plan)
        return plan

    if _researcher_intent(lowered):
        plan.target_entity_types = ["researcher"]
        plan.objectives = ["research_collaboration"]
        plan.must_include_terms = ["principal investigator", "faculty", "grant", "clinical trial"]
        plan.exclude_entity_types = ["company", "conference", "role"]
        plan.strict_targeting = True
        return plan

    if _institute_intent(lowered):
        plan.target_entity_types = ["institute"]
        plan.objectives = ["institute_partnership", "research_collaboration"]
        plan.must_include_terms = ["center", "program", "collaboration", "partner"]
        plan.exclude_entity_types = ["company", "researcher", "conference", "role"]
        plan.strict_targeting = True
        return plan

    if _role_intent(lowered):
        plan.target_entity_types = ["role"]
        plan.objectives = ["hiring", "advisory"]
        plan.must_include_terms = ["role", "hiring", "remote", "clinical strategy"]
        plan.exclude_entity_types = [
            "company",
            "conference",
            "grant_program",
            "trial",
            "researcher",
            "institute",
        ]
        plan.strict_targeting = True
        return plan

    plan.target_entity_types = ["company"]
    plan.objectives = ["company_growth", "advisory" if "advisory" in lowered else "broad_discovery"]
    plan.must_include_terms = ["funding", "partnership", "launch", "clinical AI"]
    if _company_target_intent(lowered):
        _apply_company_only_targeting(plan)
    else:
        plan.strict_targeting = False
    return plan


def _broad_search_plan(plan: OpportunitySearchPlan) -> OpportunitySearchPlan:
    plan.target_entity_types = [
        "company",
        "institute",
        "researcher",
        "conference",
        "journal_call",
        "contract_rfp",
        "grant_program",
        "trial",
    ]
    plan.objectives = [
        "broad_discovery",
        "company_growth",
        "research_collaboration",
        "presentation_opportunity",
        "journal_article_call",
        "contract_opportunity",
        "advisory",
        "funding",
    ]
    plan.must_include_terms = [
        "behavioral health",
        "clinical AI",
        "partnership",
        "funding",
        "conference",
        "RFP",
        "special issue",
        "workshop",
        "certification",
        "fellowship",
        "networking",
        "advisory board",
    ]
    plan.strict_targeting = False
    plan.lanes = [
        OpportunitySearchLane(
            lane_type="conference_speaking",
            target_entity_type="conference",
            objective="presentation_opportunity",
            acceptance_criteria=["current event or active call with a source-visible action path"],
        ),
        OpportunitySearchLane(
            lane_type="workshop_training",
            target_entity_type="conference",
            objective="presentation_opportunity",
            acceptance_criteria=[
                "specific current workshop, training, teaching, or facilitation opportunity"
            ],
        ),
        OpportunitySearchLane(
            lane_type="certification_professional_development",
            target_entity_type="institute",
            objective="advisory",
            acceptance_criteria=[
                "specific credential or professional-development program with enrollment "
                "or cycle details"
            ],
        ),
        OpportunitySearchLane(
            lane_type="grant_fellowship",
            target_entity_type="grant_program",
            objective="funding",
            acceptance_criteria=[
                "specific active grant or fellowship with status, timing, and eligibility evidence"
            ],
        ),
        OpportunitySearchLane(
            lane_type="industry_collaboration",
            target_entity_type="company",
            objective="research_collaboration",
            acceptance_criteria=[
                "specific external partner, pilot, sponsored-research, or advisory path"
            ],
        ),
        OpportunitySearchLane(
            lane_type="consulting_advisory",
            target_entity_type="company",
            objective="advisory",
            acceptance_criteria=[
                "specific consulting, fractional, expert, workshop-facilitation, or advisory need"
            ],
        ),
        OpportunitySearchLane(
            lane_type="networking_community",
            target_entity_type="conference",
            objective="research_collaboration",
            acceptance_criteria=[
                "specific current professional community, networking event, or introduction path"
            ],
        ),
        OpportunitySearchLane(
            lane_type="contract_procurement",
            target_entity_type="contract_rfp",
            objective="contract_opportunity",
            acceptance_criteria=["specific active solicitation or procurement path"],
        ),
    ]
    return plan


def _professional_development_search_plan(
    plan: OpportunitySearchPlan,
) -> OpportunitySearchPlan:
    plan.target_entity_types = ["conference", "institute"]
    plan.objectives = ["presentation_opportunity", "advisory", "research_collaboration"]
    plan.must_include_terms = [
        "workshop",
        "training",
        "certification",
        "professional development",
        "networking",
        "membership",
        "remote",
        "virtual",
    ]
    plan.strict_targeting = True
    plan.lanes = [
        OpportunitySearchLane(
            lane_type="workshop_training",
            target_entity_type="conference",
            objective="presentation_opportunity",
            acceptance_criteria=["specific current workshop or training with access details"],
        ),
        OpportunitySearchLane(
            lane_type="certification_professional_development",
            target_entity_type="institute",
            objective="advisory",
            acceptance_criteria=["specific current program with enrollment or cycle details"],
        ),
        OpportunitySearchLane(
            lane_type="networking_community",
            target_entity_type="conference",
            objective="research_collaboration",
            acceptance_criteria=["specific current community or event with a joining path"],
        ),
    ]
    return plan


def _industry_services_search_plan(plan: OpportunitySearchPlan) -> OpportunitySearchPlan:
    plan.target_entity_types = ["company", "institute"]
    plan.objectives = ["research_collaboration", "advisory"]
    plan.must_include_terms = [
        "industry collaboration",
        "pilot",
        "consulting",
        "advisory board",
        "facilitation",
    ]
    plan.strict_targeting = False
    plan.lanes = [
        OpportunitySearchLane(
            lane_type="industry_collaboration",
            target_entity_type="company",
            objective="research_collaboration",
            acceptance_criteria=[
                "specific active external partner, pilot, sponsored-research, or advisory path"
            ],
        ),
        OpportunitySearchLane(
            lane_type="consulting_advisory",
            target_entity_type="company",
            objective="advisory",
            acceptance_criteria=[
                "specific current consulting, expert, facilitation, fractional, or advisory need"
            ],
        ),
    ]
    return plan


def _grant_only_search_plan(plan: OpportunitySearchPlan) -> OpportunitySearchPlan:
    plan.target_entity_types = ["grant_program"]
    plan.objectives = ["funding"]
    plan.must_include_terms = [
        "grant",
        "funding opportunity",
        "deadline",
        "eligibility",
        "open",
    ]
    plan.exclude_entity_types = [
        "company",
        "contract_rfp",
        "conference",
        "role",
        "researcher",
        "institute",
        "trial",
    ]
    plan.strict_targeting = True
    plan.lanes = [
        OpportunitySearchLane(
            lane_type="grant_funding",
            target_entity_type="grant_program",
            objective="funding",
            required_fields=[
                "title",
                "funder",
                "deadline_or_status",
                "eligibility",
                "source_url",
            ],
            acceptance_criteria=[
                "specific active grant, NOFO, FOA, RFA, SBIR/STTR, or funding program",
                "must verify current status or cycle timing",
                "must preserve business eligibility gaps instead of assuming qualification",
            ],
        )
    ]
    return plan


def _meeting_grant_search_plan(plan: OpportunitySearchPlan) -> OpportunitySearchPlan:
    plan.target_entity_types = ["conference", "grant_program"]
    plan.objectives = ["presentation_opportunity", "funding"]
    plan.must_include_terms = [
        "conference",
        "meeting",
        "call for abstracts",
        "speaker",
        "grant",
        "funding opportunity",
        "deadline",
    ]
    plan.exclude_entity_types = ["company", "role", "github_repository"]
    plan.strict_targeting = True
    plan.desired_count = max(2, plan.desired_count)
    plan.lanes = [
        OpportunitySearchLane(
            lane_type="meeting_conference",
            desired_count=1,
            target_entity_type="conference",
            objective="presentation_opportunity",
            required_fields=["title", "organizer", "date_or_deadline", "source_url"],
            acceptance_criteria=[
                "specific event, meeting, conference, workshop, abstract call, or speaker call",
                "must include event date or submission deadline when available",
                "do not satisfy with a generic conferences funding category page",
            ],
        ),
        OpportunitySearchLane(
            lane_type="grant_funding",
            desired_count=1,
            target_entity_type="grant_program",
            objective="funding",
            required_fields=["title", "funder", "deadline_or_status", "source_url"],
            acceptance_criteria=[
                "specific active grant, funding notice, forecast, or program page",
                "must include deadline, due date, posted date, or active status when available",
                "do not satisfy with a generic funding topic page alone",
            ],
        ),
    ]
    return plan


def _formal_opportunity_search_plan(plan: OpportunitySearchPlan) -> OpportunitySearchPlan:
    plan.target_entity_types = ["grant_program", "contract_rfp", "conference", "institute"]
    plan.objectives = [
        "funding",
        "contract_opportunity",
        "presentation_opportunity",
        "institute_partnership",
    ]
    plan.must_include_terms = [
        "behavioral health",
        "mental health",
        "AI",
        "grant",
        "RFP",
        "pilot",
        "call for proposals",
        "deadline",
        "eligibility",
    ]
    plan.exclude_entity_types = ["role", "github_repository"]
    plan.strict_targeting = True
    plan.desired_count = max(3, plan.desired_count)
    plan.lanes = [
        OpportunitySearchLane(
            lane_type="grant_funding",
            desired_count=2,
            target_entity_type="grant_program",
            objective="funding",
            required_fields=["title", "funder", "deadline_or_status", "eligibility", "source_url"],
            acceptance_criteria=[
                "specific active grant, NOFO, FOA, RFA, SBIR/STTR, or funding program",
                "must include due date, posted date, open status, or cycle timing when available",
                "must preserve business eligibility gaps instead of assuming Keystone qualifies",
            ],
        ),
        OpportunitySearchLane(
            lane_type="procurement_rfp",
            desired_count=2,
            target_entity_type="contract_rfp",
            objective="contract_opportunity",
            required_fields=["title", "buyer", "deadline_or_status", "vendor_path", "source_url"],
            acceptance_criteria=[
                "specific solicitation, RFP, RFI, sources-sought notice, or procurement page",
                "must show a vendor, subcontractor, evaluator, or implementation partner path",
                "must not satisfy with generic policy, news, or expired-award pages alone",
            ],
        ),
        OpportunitySearchLane(
            lane_type="pilot_partnership",
            desired_count=1,
            target_entity_type="institute",
            objective="institute_partnership",
            required_fields=["title", "sponsor", "application_or_partner_path", "source_url"],
            acceptance_criteria=[
                "specific pilot, innovation program, partner call, accelerator, "
                "or implementation call",
                "must show how an external small business, vendor, evaluator, or partner can act",
                "must flag if timing or eligibility is not source-visible",
            ],
        ),
        OpportunitySearchLane(
            lane_type="proposal_call",
            desired_count=1,
            target_entity_type="conference",
            objective="presentation_opportunity",
            required_fields=["title", "organizer", "deadline_or_status", "source_url"],
            acceptance_criteria=[
                "specific call for proposals, abstracts, speakers, applications, or presentations",
                "must include deadline, event date, cycle timing, or active submission "
                "status when available",
                "must not treat broad content pages as actionable opportunities",
            ],
        ),
    ]
    return plan


def merge_opportunity_search_plan(
    base: OpportunitySearchPlan,
    candidate: OpportunitySearchPlan | dict[str, Any] | None,
) -> OpportunitySearchPlan:
    """Merge an LLM plan over the local fallback while preserving safe defaults."""

    if candidate is None:
        return base
    plan = (
        candidate
        if isinstance(candidate, OpportunitySearchPlan)
        else OpportunitySearchPlan.model_validate(candidate)
    )
    update = plan.model_dump(mode="python")
    merged_payload = base.model_dump(mode="python")
    merged_payload.update(update)
    merged = OpportunitySearchPlan.model_validate(merged_payload)
    if not merged.target_entity_types:
        merged.target_entity_types = list(base.target_entity_types)
    if not merged.objectives:
        merged.objectives = list(base.objectives)
    if not merged.domains:
        merged.domains = list(base.domains)
    if not merged.must_include_terms:
        merged.must_include_terms = list(base.must_include_terms)
    if not merged.lanes:
        merged.lanes = list(base.lanes)
    if (
        base.strict_targeting
        and plan_targets_only(base, "company")
        and plan_targets_only(merged, "company")
    ):
        _apply_company_only_targeting(merged)
    merged.desired_count = max(1, min(10, merged.desired_count or base.desired_count))
    return merged


def plan_targets_only(plan: OpportunitySearchPlan, *targets: str) -> bool:
    return bool(plan.target_entity_types) and set(plan.target_entity_types) <= set(targets)


def plan_has_target(plan: OpportunitySearchPlan | None, target: str) -> bool:
    return plan is not None and target in plan.target_entity_types


def plan_has_objective(plan: OpportunitySearchPlan | None, objective: str) -> bool:
    return plan is not None and objective in plan.objectives


def _domains_from_topic(lowered: str) -> list[str]:
    phrases = (
        "behavioral health AI",
        "behavioral health",
        "mental health",
        "digital mental health",
        "psychiatry AI",
        "psychiatry",
        "clinical AI",
        "neuroinformatics",
        "neuroscience",
        "evidence generation",
    )
    return [phrase for phrase in phrases if phrase in lowered]


def _mixed_meeting_grant_intent(lowered: str) -> bool:
    if not lowered:
        return False
    if "across" in lowered or "all lanes" in lowered or "multiple lanes" in lowered:
        return False
    meeting = any(
        marker in lowered
        for marker in (
            "meeting",
            "meetings",
            "conference",
            "conferences",
            "workshop",
            "symposium",
            "speaker",
            "abstract",
        )
    )
    funding = any(
        marker in lowered for marker in ("grant", "grants", "funding", "funder", "nofo", "sbir")
    )
    return (
        meeting
        and funding
        and any(marker in lowered for marker in ("1 ", "one ", "exactly", "each", " and "))
    )


def _professional_development_intent(lowered: str) -> bool:
    markers = (
        "workshop",
        "training",
        "certification",
        "certificate",
        "professional development",
        "networking",
        "professional community",
        "professional society",
    )
    return sum(1 for marker in markers if marker in lowered) >= 2


def _industry_services_intent(lowered: str) -> bool:
    collaboration_markers = (
        "industry-sponsored collaboration",
        "industry collaboration",
        "collaboration",
        "pilot",
        "sponsored research",
        "external partner",
    )
    advisory_markers = (
        "consulting",
        "consultant",
        "advisory board",
        "advisory-board",
        "facilitation",
        "facilitator",
        "fractional",
    )
    return any(marker in lowered for marker in collaboration_markers) and any(
        marker in lowered for marker in advisory_markers
    )


def _grant_only_intent(lowered: str) -> bool:
    if not any(marker in lowered for marker in ("grant", "grants", "funding opportunity")):
        return False
    other_lane_markers = (
        "conference",
        "meeting",
        "workshop",
        "contract",
        "rfp",
        "procurement",
        "pilot",
        "collaboration",
        "speaker",
        "call for papers",
    )
    return not any(marker in lowered for marker in other_lane_markers)


def _conference_intent(lowered: str) -> bool:
    if not lowered:
        return False
    non_conference_lanes = (
        "companies",
        "researchers",
        "institutes",
        "clinical trials",
        "grants",
        "funding",
        "launches",
    )
    if (
        "across conferences" not in lowered
        and sum(1 for marker in non_conference_lanes if marker in lowered) >= 2
    ):
        return False
    return any(
        marker in lowered
        for marker in (
            "across conferences",
            "conference",
            "conferences",
            "symposium",
            "summit",
            "workshop",
            "call for speakers",
            "call for abstracts",
        )
    ) and any(
        marker in lowered
        for marker in (
            "presentation",
            "presentations",
            "speaking",
            "speaker",
            "abstract",
            "workshop",
            "where keystone",
            "implementation oriented",
            "implementation-oriented",
        )
    )


def _journal_call_intent(lowered: str) -> bool:
    if not lowered:
        return False
    return any(
        marker in lowered
        for marker in (
            "journal call",
            "article request",
            "call for papers",
            "call for manuscripts",
            "special issue",
            "journal article",
            "publication call",
        )
    )


def _contract_rfp_intent(lowered: str) -> bool:
    if not lowered:
        return False
    return any(
        marker in lowered
        for marker in (
            "rfp",
            "request for proposal",
            "contract opportunity",
            "contract opportunities",
            "solicitation",
            "procurement",
            "sam.gov",
            "government contract",
        )
    )


def _formal_opportunity_intent(lowered: str) -> bool:
    if not lowered:
        return False
    formal_markers = (
        "grant",
        "grants",
        "funding opportunity",
        "nofo",
        "foa",
        "rfa",
        "sbir",
        "sttr",
        "rfp",
        "rfps",
        "request for proposal",
        "request for proposals",
        "solicitation",
        "procurement",
        "pilot",
        "pilots",
        "pilot program",
        "call for proposals",
        "call-for-proposals",
        "calls for proposals",
        "call for applications",
        "cfp",
        "cfps",
    )
    if sum(1 for marker in formal_markers if marker in lowered) < 2:
        return False
    return any(
        marker in lowered
        for marker in (
            "keystone",
            "company",
            "companies",
            "vendor",
            "partner",
            "partners",
            "act on",
            "actionable",
            "apply",
            "source-backed",
            "source backed",
            "exact",
        )
    )


def _researcher_intent(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in ("researchers", "principal investigators", "investigators", "faculty", "labs")
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


def _institute_intent(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in ("academic institutes", "institutes", "institute", "centers", "programs")
    ) and any(
        marker in lowered
        for marker in ("collaboration", "partnership", "advisory", "implementation")
    )


def _role_intent(lowered: str) -> bool:
    return any(
        marker in f" {lowered} "
        for marker in (" role ", " roles ", " job ", " jobs ", " hiring ", " remote ")
    )


def _github_repository_intent(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in (
            "github repo",
            "github repository",
            "github repositories",
            "open source repo",
            "open-source repo",
            "open source repository",
            "open-source repository",
            "repositories",
            "repos",
            "developer tooling",
        )
    )


def _company_target_intent(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in ("companies", "company", "startups", "startup", "vendors", "platforms")
    )


def _apply_company_only_targeting(plan: OpportunitySearchPlan) -> None:
    plan.strict_targeting = True
    plan.exclude_entity_types = [
        "institute",
        "researcher",
        "conference",
        "journal_call",
        "contract_rfp",
        "grant_program",
        "trial",
        "role",
    ]


def _company_growth_intent(lowered: str) -> bool:
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
    return _company_target_intent(lowered) and any(marker in lowered for marker in growth_markers)


def _broad_intent(lowered: str) -> bool:
    broad_markers = ("broad", "across", "multiquery", "multi-query", "multiple lanes", "all lanes")
    lane_markers = (
        "funding",
        "partnership",
        "pilot",
        "clinical trial",
        "conference",
        "journal",
        "special issue",
        "call for papers",
        "rfp",
        "contract",
        "solicitation",
        "grant",
        "advisory",
        "researcher",
        "institute",
        "company",
    )
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
        any(marker in lowered for marker in broad_markers)
        and sum(1 for marker in lane_markers if marker in lowered) >= 2
    )
