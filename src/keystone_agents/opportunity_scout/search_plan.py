"""Pre-retrieval planning for Opportunity Scout."""

from __future__ import annotations

from typing import Any

from keystone_agents.schemas.opportunity_search_plan import OpportunitySearchPlan


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

    if _broad_intent(lowered):
        plan.target_entity_types = [
            "company",
            "institute",
            "researcher",
            "conference",
            "grant_program",
            "trial",
        ]
        plan.objectives = ["broad_discovery", "company_growth", "research_collaboration"]
        plan.must_include_terms = ["behavioral health", "clinical AI", "partnership", "funding"]
        plan.strict_targeting = False
        return plan

    if _company_growth_intent(lowered):
        plan.target_entity_types = ["company"]
        plan.objectives = [
            "company_growth",
            "advisory" if "advisory" in lowered else "broad_discovery",
        ]
        plan.must_include_terms = ["funding", "partnership", "launch", "clinical AI"]
        plan.strict_targeting = False
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
        plan.target_entity_types = ["company"]
        plan.objectives = ["hiring", "advisory"]
        plan.must_include_terms = ["role", "hiring", "remote", "clinical strategy"]
        plan.exclude_entity_types = ["conference", "grant_program", "trial"]
        plan.strict_targeting = False
        return plan

    plan.target_entity_types = ["company"]
    plan.objectives = ["company_growth", "advisory" if "advisory" in lowered else "broad_discovery"]
    plan.must_include_terms = ["funding", "partnership", "launch", "clinical AI"]
    plan.strict_targeting = False
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
    update = plan.model_dump(mode="json")
    merged = base.model_copy(update=update)
    if not merged.target_entity_types:
        merged.target_entity_types = list(base.target_entity_types)
    if not merged.objectives:
        merged.objectives = list(base.objectives)
    if not merged.domains:
        merged.domains = list(base.domains)
    if not merged.must_include_terms:
        merged.must_include_terms = list(base.must_include_terms)
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


def _company_growth_intent(lowered: str) -> bool:
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


def _broad_intent(lowered: str) -> bool:
    broad_markers = ("broad", "across", "multiquery", "multi-query", "multiple lanes", "all lanes")
    lane_markers = (
        "funding",
        "partnership",
        "pilot",
        "clinical trial",
        "conference",
        "grant",
        "advisory",
        "researcher",
        "institute",
        "company",
    )
    return (
        any(marker in lowered for marker in broad_markers)
        and sum(1 for marker in lane_markers if marker in lowered) >= 2
    )
