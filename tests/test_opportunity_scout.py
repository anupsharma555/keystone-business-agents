from __future__ import annotations

import json
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from keystone_agents.agents import opportunity_scout as scout_module
from keystone_agents.agents.opportunity_scout import (
    apply_opportunity_scout_synthesis,
    build_opportunity_scout_agent,
    build_opportunity_scout_synthesis_agent,
    handoff_to_business_research_analyst_placeholder_impl,
    load_existing_opportunity_state_impl,
    save_opportunity_placeholder_impl,
    score_opportunity_impl,
    scout_opportunities_fixture,
    scout_opportunities_live_search,
    search_clinical_trials_sources_impl,
    search_company_page_sources_impl,
    search_conference_publication_sources_impl,
    search_funding_news_sources_impl,
    search_grant_sources_impl,
    search_job_posting_sources_impl,
    search_opportunity_sources_placeholder_impl,
)
from keystone_agents.live_retrieval import run_opportunity_scout_live
from keystone_agents.schemas.opportunity import (
    ExistingOpportunityState,
    Opportunity,
    OpportunityRecord,
    OpportunityScoutResult,
    OpportunityScoutSynthesis,
    OpportunityScoutSynthesisDecision,
    OpportunitySource,
    OpportunitySourceBundle,
)
from keystone_agents.sdk import ToolGuardrailViolation, load_prompt
from keystone_agents.source_quality import SourceQualityScore, SourceQualitySummary
from keystone_agents.tools.html_review_tool import HtmlReviewResult
from keystone_agents.tools.search_provider import (
    DryRunSearchProvider,
    LiveSearchProviderRequiredError,
    SerperSearchError,
)
from keystone_agents.tools.serper_tool import SearchResult, SerperTool
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionBudget,
    WebsiteExtractionError,
    WebsiteExtractionResult,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeSerperTool:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, max_results: int = 10) -> dict[str, object]:
        self.queries.append(query)
        return {
            "status": "live",
            "query": query,
            "results": [
                {
                    "title": "NeuroFlow announces payer partnership outcomes study",
                    "url": "https://example.test/neuroflow-payer",
                    "snippet": (
                        "Behavioral health AI company announces payer partnership and "
                        "outcomes evidence."
                    ),
                    "source_type": "google_search",
                },
                {
                    "title": "NeuroFlow - clinical validation update",
                    "url": "https://example.test/neuroflow-validation",
                    "snippet": "Clinical validation study for behavioral health measurement.",
                    "source_type": "google_search",
                },
                {
                    "title": "Curebase hiring clinical operations for CNS trial AI",
                    "url": "https://example.test/curebase-hiring",
                    "snippet": (
                        "Trial technology company hiring clinical and AI roles for CNS "
                        "study delivery."
                    ),
                    "source_type": "google_search",
                },
            ][:max_results],
        }


def test_live_query_specs_run_with_bounded_parallelism(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_SEARCH_CONCURRENCY", "4")

    class SlowProvider:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            return [
                SearchResult(
                    title=f"{query} advisory opportunity",
                    link=f"https://example.test/{query}",
                    snippet="Psychiatry advisory consulting opportunity.",
                    source="searxng",
                )
            ]

    provider = SlowProvider()
    specs = [
        scout_module._OpportunityQuerySpec(  # noqa: SLF001
            lane="collaboration",
            time_window="recent",
            query=f"query-{index}",
            entity_hint="company",
        )
        for index in range(4)
    ]

    hits = scout_module._search_query_specs_with_provider(  # noqa: SLF001
        search_provider=provider,
        query_specs=specs,
        max_results=2,
    )

    assert provider.max_active > 1
    assert [hit["query"] for hit in hits] == [f"query-{index}" for index in range(4)]


def test_live_query_specs_skip_one_provider_error_without_failing_run() -> None:
    class FlakyProvider:
        def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
            if query == "bad-query":
                raise SerperSearchError("Serper search failed with HTTP 400.")
            return [
                SearchResult(
                    title=f"{query} company raises funding",
                    link=f"https://example.test/{query}",
                    snippet="Behavioral health AI company funding signal.",
                    source="serper",
                )
            ]

    specs = [
        scout_module._OpportunityQuerySpec(
            lane="company_growth",
            time_window="recent",
            query="good-query",
            entity_hint="company",
        ),
        scout_module._OpportunityQuerySpec(
            lane="company_growth",
            time_window="recent",
            query="bad-query",
            entity_hint="company",
        ),
    ]

    hits = scout_module._search_query_specs_with_provider(
        search_provider=FlakyProvider(),
        query_specs=specs,
        max_results=2,
    )

    assert [hit["query"] for hit in hits] == ["good-query"]


def test_strict_company_plan_uses_company_only_live_query_lanes() -> None:
    plan = scout_module.infer_opportunity_search_plan(
        "Identify 5 mental health AI companies with possible clinical validation needs; "
        "no outreach.",
        desired_count=5,
    )

    specs = scout_module._build_live_query_specs(
        "Identify 5 mental health AI companies with possible clinical validation needs; "
        "no outreach.",
        search_plan=plan,
    )

    assert specs
    assert {spec.entity_hint for spec in specs} == {"company"}
    assert "role" not in {spec.lane for spec in specs}
    assert {"researcher", "institute", "conference", "grant", "trial"}.isdisjoint(
        {spec.lane for spec in specs}
    )


def test_formal_opportunity_plan_expands_short_request_across_actionable_lanes() -> None:
    topic = (
        "Find source-backed behavioral-health AI grants, RFPs, pilots, or CFPs "
        "Keystone could act on."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=5)
    specs = scout_module._build_live_query_specs(topic, search_plan=plan)  # noqa: SLF001

    assert set(plan.target_entity_types) == {
        "grant_program",
        "contract_rfp",
        "conference",
        "institute",
    }
    assert {"grant_funding", "procurement_rfp", "pilot_partnership", "proposal_call"} <= {
        lane.lane_type for lane in plan.lanes
    }
    assert {"grant", "contract_rfp", "collaboration", "conference"}.issubset(
        {spec.lane for spec in specs}
    )
    assert any("site:grants.gov" in spec.query for spec in specs)
    assert any("site:sam.gov" in spec.query for spec in specs)
    assert any(
        "call for proposals" in spec.query.lower() or "cfp" in spec.query.lower() for spec in specs
    )


@pytest.mark.parametrize("identifier", ["PAR-25-310", "RFA-MH-27-180"])
def test_formal_identifier_request_searches_exact_identifier_first(identifier: str) -> None:
    topic = (
        f"Assess NIH grant {identifier} for Keystone and verify deadline, eligibility, "
        "application path, and official source."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=1)

    specs = scout_module._build_live_query_specs(topic, search_plan=plan)  # noqa: SLF001

    assert specs[0].query == identifier
    assert specs[0].lane == "grant"
    assert specs[0].entity_hint == "grant_program"
    assert sum(spec.query == identifier for spec in specs) == 1
    assert len(specs) == 1


def test_formal_opportunity_search_does_not_apply_publication_date_filter() -> None:
    grant_spec = scout_module._OpportunityQuerySpec(  # noqa: SLF001
        lane="grant",
        time_window="current",
        query="PAR-25-310",
        entity_hint="grant_program",
    )
    role_spec = scout_module._OpportunityQuerySpec(  # noqa: SLF001
        lane="role",
        time_window="recent",
        query="current remote psychiatry role",
    )

    grant_request = scout_module._search_request_from_query(  # noqa: SLF001
        grant_spec,
        max_results=1,
    )
    role_request = scout_module._search_request_from_query(  # noqa: SLF001
        role_spec,
        max_results=1,
    )

    assert grant_request.time_range is None
    assert grant_request.language is None
    assert grant_request.safe_search is None
    assert role_request.time_range == "recent"


def test_broad_personalized_plan_covers_full_keystone_opportunity_portfolio() -> None:
    topic = (
        "Find a broad range of current remote-accessible opportunities relevant to "
        "Keystone and its physician-scientist founder across conferences, workshops, "
        "certifications, grants, industry collaborations, consulting, and networking."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=8)
    specs = scout_module._build_live_query_specs(topic, search_plan=plan)  # noqa: SLF001

    lane_types = {lane.lane_type for lane in plan.lanes}
    assert {
        "conference_speaking",
        "workshop_training",
        "certification_professional_development",
        "grant_fellowship",
        "industry_collaboration",
        "consulting_advisory",
        "networking_community",
        "contract_procurement",
    } <= lane_types
    query_lanes = {spec.lane for spec in specs}
    assert {
        "workshop_training",
        "certification_professional_development",
        "networking_community",
        "consulting_advisory",
    } <= query_lanes
    new_portfolio_specs = [
        spec
        for spec in specs
        if spec.lane
        in {
            "workshop_training",
            "certification_professional_development",
            "networking_community",
            "consulting_advisory",
        }
    ]
    assert all("2026" in spec.query for spec in new_portfolio_specs)


def test_short_broad_portfolio_request_does_not_collapse_to_remote_roles() -> None:
    topic = (
        "Find a broad range of current remote-accessible opportunities relevant to "
        "Keystone and its physician-scientist founder."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=5)
    specs = scout_module._build_live_query_specs(topic, search_plan=plan)  # noqa: SLF001

    assert len(plan.lanes) == 8
    assert {"workshop_training", "networking_community", "grant"} <= {spec.lane for spec in specs}
    assert plan.strict_targeting is False


def test_precise_grant_request_stays_in_grant_lane_when_search_broadens() -> None:
    topic = (
        "Find up to 2 active U.S. grants for behavioral-health AI evaluation that a "
        "small consulting company could pursue. Require verified eligibility and a deadline."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=2)
    specs = scout_module._build_live_query_specs(topic, search_plan=plan)  # noqa: SLF001
    followups = scout_module._build_adaptive_followup_query_specs(  # noqa: SLF001
        topic,
        existing_specs=specs,
        search_plan=plan,
    )

    assert plan.target_entity_types == ["grant_program"]
    assert [lane.lane_type for lane in plan.lanes] == ["grant_funding"]
    assert {spec.lane for spec in specs} == {"grant"}
    assert {spec.lane for spec in followups} == {"grant"}


def test_precise_advisory_request_stays_in_role_lane_when_search_broadens() -> None:
    topic = (
        "Find current remote U.S. paid consulting, fractional, or advisory opportunities "
        "for a physician-scientist psychiatrist."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=3)
    specs = scout_module._build_live_query_specs(topic, search_plan=plan)  # noqa: SLF001
    followups = scout_module._build_adaptive_followup_query_specs(  # noqa: SLF001
        topic,
        existing_specs=specs,
        search_plan=plan,
    )

    assert plan.target_entity_types == ["role"]
    assert {spec.lane for spec in specs} == {"role"}
    assert {spec.lane for spec in followups} == {"role"}


def test_professional_development_fallback_does_not_add_company_lanes() -> None:
    topic = (
        "Find current remote workshops, certifications, professional-development programs, "
        "and networking communities in clinical AI or psychiatry."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=3)
    specs = scout_module._build_live_query_specs(topic, search_plan=plan)  # noqa: SLF001
    followups = scout_module._build_adaptive_followup_query_specs(  # noqa: SLF001
        topic,
        existing_specs=specs,
        search_plan=plan,
    )

    expected = {
        "workshop_training",
        "certification_professional_development",
        "networking_community",
    }
    assert {spec.lane for spec in specs} == expected
    assert {spec.lane for spec in followups} == expected


def test_professional_development_coverage_followup_does_not_add_company_news() -> None:
    topic = (
        "Find current remote workshops, certifications, professional-development programs, "
        "and networking communities in clinical AI or psychiatry."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=3)
    specs = scout_module._build_live_query_specs(topic, search_plan=plan)  # noqa: SLF001

    followups, coverage = scout_module._build_coverage_followup_query_specs(  # noqa: SLF001
        topic,
        existing_specs=specs,
        hits=[],
        search_plan=plan,
    )

    assert set(coverage["expected_lanes"]) == {
        "conference_events",
        "people_institutions",
    }
    assert {spec.lane for spec in followups} == {"conference", "researcher"}
    assert "company_growth" not in {spec.lane for spec in followups}


def test_professional_development_request_uses_dedicated_non_role_lanes() -> None:
    topic = (
        "Find current remote workshops, certifications, professional-development "
        "programs, and networking communities in clinical AI or psychiatry."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=5)
    specs = scout_module._build_live_query_specs(topic, search_plan=plan)  # noqa: SLF001

    assert {lane.lane_type for lane in plan.lanes} == {
        "workshop_training",
        "certification_professional_development",
        "networking_community",
    }
    assert {spec.lane for spec in specs} == {
        "workshop_training",
        "certification_professional_development",
        "networking_community",
    }
    assert "role" not in {spec.lane for spec in specs}
    assert scout_module._is_role_search(topic) is False  # noqa: SLF001
    assert all("clinical research operations" not in spec.query for spec in specs)
    followups = scout_module._build_adaptive_followup_query_specs(  # noqa: SLF001
        topic,
        existing_specs=specs,
        search_plan=plan,
    )
    assert any("site:ecornell.cornell.edu" in spec.query for spec in followups)


def test_professional_development_live_search_enables_page_verification_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic = (
        "Find current remote workshops, certifications, and networking communities "
        "in clinical AI or psychiatry."
    )
    observed: dict[str, object] = {}

    class Provider:
        provider_name = "searxng"
        dry_run = False

        def validate_configuration(self) -> None:
            return None

        def search(self, query: str, *, max_results: int = 3) -> list[dict[str, str]]:
            return []

    original = scout_module._process_candidate_hits  # noqa: SLF001

    def capture(*args: object, **kwargs: object) -> object:
        observed["verify_source_pages"] = kwargs.get("verify_source_pages")
        return original(*args, **kwargs)

    monkeypatch.setattr(scout_module, "_process_candidate_hits", capture)

    scout_opportunities_live_search(
        topic=topic,
        max_results=3,
        search_provider=Provider(),
    )

    assert observed["verify_source_pages"] is True


def test_verified_non_company_program_reaches_ranked_results_with_action_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_VERIFY_CAP", "4")
    monkeypatch.setenv("KEYSTONE_ENABLE_SEARCH_COVERAGE_FOLLOWUP", "false")

    class Provider:
        provider_name = "searxng"
        dry_run = False

        def validate_configuration(self) -> None:
            return None

        def search_structured(self, request) -> list[SearchResult]:
            if "certification" not in request.query.lower():
                return []
            return [
                SearchResult(
                    title="Certificate in Applied AI for Health Systems | Example University",
                    link="https://example.edu/applied-ai-certificate",
                    snippet="Online certificate program for healthcare professionals.",
                    source="searxng",
                )
            ]

    monkeypatch.setattr(
        scout_module,
        "extract_website_content",
        lambda *_args, **_kwargs: WebsiteExtractionResult(
            url="https://example.edu/applied-ai-certificate",
            title="Certificate in Applied AI for Health Systems",
            provider="trafilatura",
            status="success",
            text_or_markdown=(
                "Enrollment is open for this online applied clinical AI certificate. "
                "Eligible applicants include physicians, clinical researchers, and health "
                "system professionals. Apply by December 15, 2026. The program is virtual."
            ),
            claims=["Enrollment is open through December 15, 2026."],
        ),
    )

    result = scout_opportunities_live_search(
        topic=(
            "Find current remote workshops, certifications, and networking communities "
            "in clinical AI or psychiatry."
        ),
        max_results=3,
        search_provider=Provider(),
    )

    assert len(result.records) == 1, {
        "audit_notes": result.audit_notes,
        "filtered": [item.model_dump(mode="json") for item in result.filtered_candidates],
        "review": [item.model_dump(mode="json") for item in result.review_candidates],
    }
    record = result.records[0]
    assert record.entity_name == "Certificate in Applied AI for Health Systems"
    assert record.entity_kind == "institute"
    assert record.opportunity_kind == "certification_or_professional_development"
    assert record.opportunity_status == "open"
    assert record.deadline == "2026-12-15"
    assert "physicians" in record.eligibility_summary
    assert record.access_mode == "remote_or_virtual"
    assert record.detail_verification_status == "page_verified"
    assert record.application_or_contact_path == "https://example.edu/applied-ai-certificate"


def test_verified_open_wording_does_not_override_a_past_deadline() -> None:
    hit = {
        "company_name": "Expired Clinical AI Workshop",
        "entity_kind": "conference",
        "source_title": "Expired Clinical AI Workshop",
        "source_url": "https://example.test/expired-workshop",
        "signal": (
            "Registration open for a virtual psychiatry AI workshop. Register by June 30, 2026."
        ),
        "verified_excerpt": "Registration open. Register by June 30, 2026.",
        "source_category": "conference",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(  # noqa: SLF001
        hit,
        topic="Find current remote psychiatry AI workshops.",
    )

    assert any("deadline 2026-06-30 has passed" in reason for reason in reasons)


def test_active_grant_without_requested_eligibility_is_withheld_from_final_ranking() -> None:
    topic = (
        "Find active behavioral-health AI grants that a small consulting company "
        "is eligible to pursue."
    )
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=3)
    hit = {
        "company_name": "Behavioral Health AI Grant",
        "entity_kind": "grant_program",
        "source_title": "Behavioral Health AI Grant",
        "source_url": "https://example.test/grant",
        "signal": "Applications open. Apply by December 20, 2026.",
        "verified_excerpt": "Applications open. Apply by December 20, 2026.",
        "source_category": "grant",
    }

    reasons = scout_module._detail_completeness_rejection_reasons(  # noqa: SLF001
        hit,
        topic=topic,
        search_plan=plan,
    )

    assert reasons == ["applicant eligibility was not verified"]


def test_remote_professional_program_without_access_details_is_withheld() -> None:
    topic = "Find current remote clinical AI workshops and certification programs."
    plan = scout_module.infer_opportunity_search_plan(topic, desired_count=3)
    hit = {
        "company_name": "Clinical AI Workshop",
        "entity_kind": "conference",
        "source_title": "Clinical AI Workshop",
        "source_url": "https://example.test/workshop",
        "signal": "Registration open. Register by December 20, 2026.",
        "verified_excerpt": "Registration open. Register by December 20, 2026.",
        "source_category": "conference",
    }

    reasons = scout_module._detail_completeness_rejection_reasons(  # noqa: SLF001
        hit,
        topic=topic,
        search_plan=plan,
    )

    assert reasons == ["remote or virtual access was not verified"]


def test_non_company_opportunity_preserves_first_class_entity_name() -> None:
    record = OpportunityRecord(
        company_name="Clinical AI Workshop 2026",
        entity_kind="conference",
        opportunity_kind="workshop_or_training",
        opportunity_type="grant or collaboration opportunity",
        priority_score=80,
        why_now_signal="Registration is open for a virtual workshop.",
        recommended_next_step="Review registration details.",
        sources=[
            OpportunitySource(
                title="Workshop page",
                url="https://example.test/workshop",
                source_type="conference",
                supported_signal="Virtual registration is open.",
            )
        ],
        keystone_fit_reason="Relevant to clinical AI professional development.",
        outside_consulting_likelihood=50,
        handoff_to_business_research_analyst=False,
    )

    assert record.entity_name == "Clinical AI Workshop 2026"
    assert record.entity_kind == "conference"
    assert record.opportunity_kind == "workshop_or_training"


def test_portfolio_eval_dataset_covers_broad_and_precise_current_opportunities() -> None:
    path = Path("evals/static/opportunity_scout_portfolio_cases.json")
    cases = json.loads(path.read_text(encoding="utf-8"))

    assert [case["id"] for case in cases] == [
        "broad_remote_accessible_keystone_portfolio",
        "precise_current_grant_no_padding",
        "precise_remote_advisory_roles",
        "current_workshops_certifications_networking",
        "industry_collaboration_and_consulting",
    ]
    assert all(case["required_lanes"] for case in cases)
    assert all(isinstance(case["minimum_verified_results"], int) for case in cases)
    assert sum(case["minimum_verified_results"] for case in cases) >= 3
    assert all(len(case["requirements"]) >= 4 for case in cases)
    assert any("Reject expired" in item for case in cases for item in case["requirements"])
    assert any("Allow zero results" in item for case in cases for item in case["requirements"])


def test_portfolio_eval_prompts_route_to_every_required_opportunity_lane() -> None:
    cases = json.loads(
        Path("evals/static/opportunity_scout_portfolio_cases.json").read_text(encoding="utf-8")
    )

    for case in cases:
        plan = scout_module.infer_opportunity_search_plan(case["prompt"], desired_count=8)
        specs = scout_module._build_live_query_specs(  # noqa: SLF001
            case["prompt"], search_plan=plan
        )
        lane_aliases = {
            "conference_speaking": "conference",
            "grant_fellowship": "grant",
            "grant_funding": "grant",
            "industry_collaboration": "collaboration",
            "contract_procurement": "contract_rfp",
            "role": "consulting_advisory",
        }
        routed_lanes = {lane_aliases.get(spec.lane, spec.lane) for spec in specs}
        required_lanes = {lane_aliases.get(lane, lane) for lane in case["required_lanes"]}
        assert required_lanes <= routed_lanes, case["id"]


@pytest.mark.parametrize(
    ("text", "lane", "entity_kind", "expected"),
    [
        (
            "Online clinical AI certificate program",
            "certification_professional_development",
            "institute",
            "certification_or_professional_development",
        ),
        (
            "Virtual workshop seeking physician-scientist facilitators",
            "workshop_training",
            "conference",
            "workshop_or_training",
        ),
        (
            "Behavioral health professional society networking community",
            "networking_community",
            "conference",
            "networking_or_professional_community",
        ),
        (
            "Remote fractional clinical AI advisor",
            "consulting_advisory",
            "company",
            "consulting_or_advisory",
        ),
        (
            "Open NIH fellowship for clinical AI",
            "grant_fellowship",
            "grant_program",
            "grant_or_fellowship",
        ),
    ],
)
def test_actionable_opportunity_kind_is_separate_from_clinical_domain(
    text: str,
    lane: str,
    entity_kind: str,
    expected: str,
) -> None:
    assert (
        scout_module._opportunity_kind_from_text(  # noqa: SLF001
            text,
            lane=lane,
            entity_kind=entity_kind,
        )
        == expected
    )


def test_job_posting_from_researcher_lane_is_classified_as_role() -> None:
    assert (
        scout_module._entity_kind_from_lane(  # noqa: SLF001
            lane="researcher",
            title="Researcher Position - Adult Division",
            url="https://recruit.example.edu/JPF10980",
            snippet="Apply now for this open role in a university psychiatry department.",
            entity_hint="researcher",
        )
        == "role"
    )


def test_long_role_title_is_not_rejected_as_article_headline() -> None:
    hit = {
        "company_name": "Psychiatry Operations Associate at Two Chairs - Remote",
        "entity_kind": "role",
        "source_title": "Psychiatry Operations Associate at Two Chairs - Remote",
        "source_url": "https://jobs.example.test/two-chairs-operations",
        "signal": "Apply now for this paid remote U.S. role in behavioral health operations.",
        "source_category": "job_posting",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(  # noqa: SLF001
        hit,
        topic="Find current remote U.S. advisory roles in behavioral health.",
    )

    assert not any("article headline" in reason for reason in reasons)
    assert not any("not a real organization" in reason for reason in reasons)


def test_closed_and_stale_formal_opportunities_are_rejected_before_scoring() -> None:
    closed = {
        "company_name": "Example Workshop 2026",
        "entity_kind": "conference",
        "source_title": "Clinical AI Workshop",
        "source_url": "https://example.test/workshop",
        "signal": "Registration closed; this workshop is no longer accepting applications.",
        "source_category": "conference",
        "published_at": "2026-02-01",
    }
    stale = {
        "company_name": "Example Grant 2023",
        "entity_kind": "grant_program",
        "source_title": "Behavioral Health AI Grant",
        "source_url": "https://example.test/grant-2023",
        "signal": "Funding opportunity for behavioral health AI research.",
        "source_category": "grant",
        "published_at": "2023-03-01",
    }

    closed_reasons = scout_module._candidate_acceptance_rejection_reasons(  # noqa: SLF001
        closed,
        topic="find current workshops",
    )
    stale_reasons = scout_module._candidate_acceptance_rejection_reasons(  # noqa: SLF001
        stale,
        topic="find current grants",
    )

    assert any("closed, expired, or canceled" in reason for reason in closed_reasons)
    assert any("older than 18 months" in reason for reason in stale_reasons)


def test_specific_formal_candidate_without_status_is_retained_for_detail_verification() -> None:
    hit = {
        "company_name": "PAR-26-123",
        "entity_kind": "grant_program",
        "source_title": "Behavioral Health AI Research Opportunity PAR-26-123",
        "source_url": "https://grants.nih.gov/grants/guide/pa-files/PAR-26-123.html",
        "signal": "Research opportunity supporting behavioral health AI evaluation.",
        "source_category": "grant",
    }
    rejection_reasons = scout_module._candidate_acceptance_rejection_reasons(  # noqa: SLF001
        hit,
        topic="Find active behavioral-health AI grants.",
    )
    review_reasons = scout_module._candidate_acceptance_review_reasons(  # noqa: SLF001
        hit,
        rejection_reasons=rejection_reasons,
    )

    assert any("lacks active opportunity evidence" in reason for reason in rejection_reasons)
    assert any("page-level status" in reason for reason in review_reasons)


def test_formal_grant_identifier_is_not_rejected_as_article_or_company_name() -> None:
    hit = {
        "company_name": "PAR-25-310",
        "entity_kind": "grant_program",
        "source_title": (
            "PAR-25-310: Accelerating Solutions to Improve Access and Quality of "
            "Empirically-Supported Practices for Youth Mental Health"
        ),
        "source_url": "https://grants.nih.gov/grants/guide/pa-files/PAR-25-310.html",
        "signal": "Official NIH notice of funding opportunity; applications are open.",
        "source_category": "grant",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(
        hit,
        topic="Find one current U.S. mental-health grant.",
    )

    assert not any("article headline" in reason for reason in reasons)
    assert not any("real organization" in reason for reason in reasons)


def test_behavioral_health_grant_does_not_require_company_level_ai_keywords() -> None:
    hit = {
        "company_name": "RFA-MH-27-180",
        "entity_kind": "grant_program",
        "source_title": "RFA-MH-27-180: Behavioral Health Implementation Research",
        "source_url": "https://grants.nih.gov/example",
        "signal": (
            "Official NIMH notice of funding opportunity for behavioral-health "
            "implementation and evidence generation; applications are open."
        ),
        "source_category": "grant",
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Find a behavioral-health AI, clinical AI, neuroinformatics, or evidence "
            "generation grant relevant to KNI."
        ),
    )

    assert not any("AI company topic" in reason for reason in reasons)


def test_strict_role_live_search_does_not_pad_with_weak_adjacent_result() -> None:
    class WeakRoleProvider:
        provider_name = "searxng"
        dry_run = False

        def validate_configuration(self) -> None:
            return None

        def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Medical Science Liaison, Neuropsychiatry (NYC) | LinkedIn",
                    link="https://www.linkedin.com/jobs/view/medical-science-liaison-neuropsychiatry-nyc",
                    snippet=(
                        "Current MSL or clinical practice experience in psychiatry, mental "
                        "health. Medical Advisor jobs. New York City."
                    ),
                    source="searxng",
                )
            ][:num_results]

    result = scout_opportunities_live_search(
        topic=(
            "find active part-time or fractional remote U.S. chief medical officer "
            "or clinical advisor roles in behavioral health AI posted in the last 1 week"
        ),
        max_results=1,
        search_provider=WeakRoleProvider(),
    )

    assert result.records == []
    reasons = {reason for candidate in result.filtered_candidates for reason in candidate.reasons}
    assert "requested remote status was not verified" in reasons
    assert "requested U.S. location or eligibility was not verified" in reasons
    assert any("requested role-title evidence" in reason for reason in reasons)
    assert "Constraint to relax next" in " ".join(result.audit_notes)


class FakeSearxngProvider:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        return [
            SearchResult(
                title="MindSpan launches digital mental health validation study",
                link="https://example.test/mindspan-validation",
                snippet=(
                    "Digital mental health company launches validation study with "
                    "outcomes evidence."
                ),
                source="searxng",
            )
        ][:num_results]


class FakeLiveSearchProvider:
    provider_name = "serper"
    dry_run = False

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.validated = False

    def validate_configuration(self) -> None:
        self.validated = True

    def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        return [
            SearchResult(
                title="AffectAI Research raises seed funding for behavioral health AI validation",
                link="https://www.businesswire.com/news/affectai-seed-validation",
                snippet=(
                    "AffectAI Research raises seed funding and launches behavioral health "
                    "AI validation work."
                ),
                source="serper",
            ),
            SearchResult(
                title="AffectAI Research validation study - ClinicalTrials.gov",
                link="https://clinicaltrials.gov/study/fixture-affectai-live",
                snippet=("Clinical trial launch for a behavioral health AI validation study."),
                source="serper",
            ),
            SearchResult(
                title="AffectAI Research validation study - ClinicalTrials.gov",
                link="https://clinicaltrials.gov/study/fixture-affectai-live",
                snippet="Duplicate clinical trial search result.",
                source="serper",
            ),
            SearchResult(
                title="Curebase hiring clinical operations for CNS trial AI",
                link="https://example.test/curebase-hiring",
                snippet=(
                    "Trial technology company hiring clinical and AI roles for CNS study delivery."
                ),
                source="serper",
            ),
        ][:num_results]


class FakeFilteredRoleProvider:
    provider_name = "serper"
    dry_run = False

    def __init__(self) -> None:
        self.queries: list[str] = []

    def validate_configuration(self) -> None:
        return None

    def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        return [
            SearchResult(
                title="AffectAI hiring Senior Clinical Strategy Advisor (Remote, United States)",
                link="https://jobs.affectai.example/senior-clinical-strategy-advisor",
                snippet=(
                    "Behavioral health AI company with 25 employees is hiring a paid remote "
                    "Senior Clinical Strategy Advisor."
                ),
                source="serper",
            ),
            SearchResult(
                title="TinyMind hiring Practicing Psychiatrist Intern (On-site)",
                link="https://jobs.tinymind.example/psychiatrist-intern",
                snippet=(
                    "Behavioral health startup with 5 employees is hiring an unpaid on-site "
                    "intern and requires a full-time practicing clinician."
                ),
                source="serper",
            ),
        ][:num_results]


class FakeBroadOpportunityProvider:
    provider_name = "serper"
    dry_run = False

    def __init__(self) -> None:
        self.queries: list[str] = []

    def validate_configuration(self) -> None:
        return None

    def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        results = [
            SearchResult(
                title="Jane Doe, PhD | Department of Psychiatry | Example University",
                link="https://example.edu/faculty/jane-doe",
                snippet=(
                    "Researcher in psychiatry and clinical AI collaborating on behavioral "
                    "health trials in Boston, MA, United States."
                ),
                source="serper",
            ),
            SearchResult(
                title="Example Institute for Digital Mental Health",
                link="https://example.edu/institute/digital-mental-health",
                snippet=(
                    "Institute launches collaboration program for digital mental health "
                    "implementation and research operations in the United States."
                ),
                source="serper",
            ),
            SearchResult(
                title="Neuropsychiatry Innovation Summit 2026",
                link="https://conference.example.org/neuropsychiatry-innovation-summit-2026",
                snippet=(
                    "United States conference featuring clinical AI, psychiatry, sponsors, "
                    "and translational neuroscience workshops."
                ),
                source="serper",
            ),
            SearchResult(
                title="Clinical AI collaboration study - ClinicalTrials.gov",
                link="https://clinicaltrials.gov/study/fixture-collab-study",
                snippet=(
                    "Recruiting United States study site with principal investigator Jane Doe "
                    "and industry-academic collaboration."
                ),
                source="serper",
            ),
        ]
        return results[:num_results]


class FakeNoisyOpportunityProvider:
    provider_name = "serper"
    dry_run = False

    def __init__(self, *, include_valid: bool = True) -> None:
        self.include_valid = include_valid
        self.queries: list[str] = []

    def validate_configuration(self) -> None:
        return None

    def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        results = [
            SearchResult(
                title="https://example.org/digital-health-research",
                link="https://example.org/digital-health-research",
                snippet="Digital health research articles and literature review collection.",
                source="serper",
            ),
            SearchResult(
                title="[PDF] Digital mental health interventions: doctoral thesis",
                link="https://example.edu/theses/digital-mental-health.pdf",
                snippet="A dissertation PDF about psychiatry technology adoption.",
                source="serper",
            ),
            SearchResult(
                title="Handbook of Digital Mental Health - Google Books",
                link="https://books.google.com/books?id=fixture",
                snippet="Book chapter overview of digital mental health research.",
                source="serper",
            ),
            SearchResult(
                title="Digital mental health research articles and reviews",
                link="https://www.sciencedirect.com/topics/medicine/digital-mental-health",
                snippet="Generic research topic page with publication links.",
                source="serper",
            ),
        ]
        if self.include_valid:
            results.append(
                SearchResult(
                    title="Brightline Health announces payer partnership outcomes program",
                    link="https://example.test/brightline-payer-outcomes",
                    snippet=(
                        "Brightline Health announces a payer partnership and outcomes "
                        "evidence program for digital mental health."
                    ),
                    source="serper",
                )
            )
        return results[:num_results]


class FakeAdjacentClinicalAIProvider:
    provider_name = "serper"
    dry_run = False

    def __init__(self) -> None:
        self.queries: list[str] = []

    def validate_configuration(self) -> None:
        return None

    def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        return [
            SearchResult(
                title="Clario: Clinical Trial Endpoint Technology and Evidence Generation",
                link="https://clario.com/",
                snippet=(
                    "Clario announces a clinical advisory partnership for validated "
                    "clinical trial management software and Artificial Intelligence "
                    "solutions across Cardiac Safety."
                ),
                source="serper",
            )
        ][:num_results]


class FakeAdaptiveOpportunityProvider:
    provider_name = "serper"
    dry_run = False

    def __init__(self) -> None:
        self.queries: list[str] = []

    def validate_configuration(self) -> None:
        return None

    def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        if "advisory board" in query:
            return [
                SearchResult(
                    title="MindCare AI announces psychiatry advisory board opportunity",
                    link="https://example.test/mindcare-ai-advisory-board",
                    snippet=(
                        "MindCare AI announces an active psychiatry clinical advisory board "
                        "opportunity for behavioral health AI validation in the United States."
                    ),
                    source="serper",
                )
            ][:num_results]
        return [
            SearchResult(
                title="Clario: Clinical Trial Endpoint Technology and Evidence Generation",
                link="https://clario.com/",
                snippet=(
                    "Validated clinical trial management software and artificial "
                    "intelligence solutions across cardiac safety."
                ),
                source="serper",
            )
        ][:num_results]


class FakeCoverageFollowupProvider:
    provider_name = "serper"
    dry_run = False

    def __init__(self) -> None:
        self.queries: list[str] = []

    def validate_configuration(self) -> None:
        return None

    def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        if "site:reporter.nih.gov" in query:
            return [
                SearchResult(
                    title="AffectAI NIH SBIR grant project",
                    link="https://reporter.nih.gov/project-details/123",
                    snippet=(
                        "NIH SBIR grant project supports behavioral health AI evidence generation."
                    ),
                    source="serper",
                )
            ][:num_results]
        return [
            SearchResult(
                title="Generic behavioral health AI company page",
                link="https://example.test/generic-company",
                snippet="Generic company result without grant coverage.",
                source="serper",
            )
        ][:num_results]


class FakePublicationProgramNoiseProvider:
    provider_name = "serper"
    dry_run = False

    def validate_configuration(self) -> None:
        return None

    def search_web(self, query: str, *, num_results: int = 10) -> list[SearchResult]:
        return [
            SearchResult(
                title="Artificial Intelligence for Mental Health Monitoring",
                link="https://pmc.ncbi.nlm.nih.gov/articles/PMC12745907/",
                snippet=(
                    "Publication article about artificial intelligence for mental health "
                    "monitoring with funding acknowledgements."
                ),
                source="serper",
            ),
            SearchResult(
                title="Seed Funding Projects",
                link="https://newbrunswick.rutgers.edu/research/office/seed-funding-projects",
                snippet="University seed funding projects and pathway programs.",
                source="serper",
            ),
        ][:num_results]


class FakeDeepeningProvider:
    provider_name = "searxng"
    dry_run = False

    def __init__(self) -> None:
        self.requests = []

    def validate_configuration(self) -> None:
        return None

    def search_structured(self, request) -> list[SearchResult]:
        self.requests.append(request)
        if request.page == 2:
            return [
                SearchResult(
                    title="Jimini Health raises funding for behavioral health AI platform",
                    link="https://example.test/jimini-health-funding",
                    snippet=(
                        "Jimini Health raises a funding round and announces a behavioral "
                        "health AI partnership."
                    ),
                    source="searxng",
                )
            ][: request.num_results]
        return [
            SearchResult(
                title="Artificial Intelligence for Mental Health Monitoring",
                link="https://pmc.ncbi.nlm.nih.gov/articles/PMC12745907/",
                snippet=(
                    "Publication article about artificial intelligence for mental "
                    "health monitoring."
                ),
                source="searxng",
            )
        ][: request.num_results]


class FakeInstituteUnderfillProvider:
    provider_name = "searxng"
    dry_run = False

    def __init__(self) -> None:
        self.requests = []

    def validate_configuration(self) -> None:
        return None

    def search_structured(self, request) -> list[SearchResult]:
        self.requests.append(request)
        query = request.query.lower()
        if "industry affiliate" in query and "partner with us" in query:
            return [
                SearchResult(
                    title="Industry Affiliate Program | Artificial Intelligence for Mental Health",
                    link="https://med.stanford.edu/psychiatry/special-initiatives/ai4mh/industry.html",
                    snippet=(
                        "The program offers opportunities for collaboration and partnership "
                        "around AI for mental health."
                    ),
                    source="searxng",
                ),
                SearchResult(
                    title="Digital Mental Health Innovation Center | Example University",
                    link="https://example.edu/digital-mental-health-innovation-center",
                    snippet=(
                        "University center invites external partners to collaborate on "
                        "digital mental health implementation."
                    ),
                    source="searxng",
                ),
            ][: request.num_results]
        if "behavioral health technology" in query:
            return [
                SearchResult(
                    title="Behavioral Health Technology Program | Example Medical School",
                    link="https://example.edu/behavioral-health-technology-program",
                    snippet=(
                        "Academic program lists contact information for research "
                        "collaboration and clinical implementation partners."
                    ),
                    source="searxng",
                )
            ][: request.num_results]
        return [
            SearchResult(
                title="Industry Affiliate Program | Artificial Intelligence for Mental Health",
                link="https://med.stanford.edu/psychiatry/special-initiatives/ai4mh/industry.html",
                snippet=(
                    "The AI4MH Industry Affiliate Program offers collaboration across "
                    "research, talent development, and strategic innovation."
                ),
                source="searxng",
            )
        ][: request.num_results]


def test_build_opportunity_scout_agent() -> None:
    agent = build_opportunity_scout_agent()

    assert agent.name == "opportunity_scout"
    assert "Opportunity Scout Agent" in agent.instructions
    assert "Keystone Neuroinformatics LLC" in agent.instructions
    assert agent.output_type is OpportunityScoutResult
    assert {
        "search_web",
        "search_opportunity_sources_placeholder",
        "load_existing_opportunity_state",
        "search_funding_news_sources",
        "search_job_posting_sources",
        "search_clinical_trials_sources",
        "search_grant_sources",
        "search_conference_publication_sources",
        "search_company_page_sources",
        "extract_research_claims_from_html",
        "score_opportunity",
        "handoff_to_business_research_analyst_placeholder",
        "save_opportunity_placeholder",
    } <= {getattr(tool, "name", "") for tool in agent.tools}


def test_build_opportunity_scout_agent_can_detach_tools() -> None:
    agent = build_opportunity_scout_agent(attach_tools=False)

    assert agent.tools == []


def test_retrieved_synthesis_agent_is_compact_tool_free_and_output_capped() -> None:
    agent = build_opportunity_scout_synthesis_agent(max_results=1)

    assert agent.tools == []
    assert len(agent.instructions) < 35_000
    assert "Use only the supplied retrieved evidence" in agent.instructions
    assert "## Opportunity Scout Tools" not in agent.instructions
    assert getattr(agent.model_settings, "max_tokens", None) == 1800
    assert getattr(agent.model_settings, "verbosity", None) == "low"
    reasoning = getattr(agent.model_settings, "reasoning", None)
    assert getattr(reasoning, "effort", None) == "low"


def test_compact_synthesis_merges_judgment_onto_verified_record() -> None:
    retrieved = scout_opportunities_fixture(
        fixture=FIXTURES / "opportunity_scout_high_confidence_sources.json",
        max_results=1,
    )
    record = retrieved.records[0]
    decision = OpportunityScoutSynthesisDecision(
        record_key=record.canonical_entity_key or record.company_name,
        include=True,
        why_now_signal="The supplied evidence confirms a current decision window.",
        keystone_fit_reason="The verified opportunity matches KNI's evaluation capabilities.",
        recommended_next_step=(
            "Review the official requirements and prepare an internal go/no-go note."
        ),
    )
    synthesis = OpportunityScoutSynthesis(
        decisions=[decision],
        audit_summary="One verified opportunity is decision-ready.",
    )

    merged = apply_opportunity_scout_synthesis(retrieved, synthesis)

    assert len(merged.records) == 1
    assert merged.records[0].company_name == record.company_name
    assert merged.records[0].sources == record.sources
    assert merged.records[0].keystone_fit_reason == decision.keystone_fit_reason
    assert "One verified opportunity is decision-ready." in merged.audit_notes
    assert merged.outreach_generated is False


def test_compact_synthesis_cannot_select_unknown_or_generate_outreach() -> None:
    retrieved = scout_opportunities_fixture(
        fixture=FIXTURES / "opportunity_scout_high_confidence_sources.json",
        max_results=1,
    )
    synthesis = OpportunityScoutSynthesis(
        decisions=[
            OpportunityScoutSynthesisDecision(
                record_key="unknown-record",
                include=True,
                why_now_signal="Current.",
                keystone_fit_reason="Potential fit.",
                recommended_next_step="Review.",
            )
        ],
        audit_summary="Unknown selection.",
    )

    merged = apply_opportunity_scout_synthesis(retrieved, synthesis)

    assert merged.records == []
    assert any("unknown-record" in note for note in merged.audit_notes)
    with pytest.raises(ValueError, match="must not generate outreach"):
        OpportunityScoutSynthesis(
            decisions=[],
            audit_summary="Unsafe.",
            outreach_generated=True,
        )


def test_opportunity_prompt_keeps_event_type_and_geography_source_bound() -> None:
    instructions = build_opportunity_scout_agent(attach_tools=False).instructions
    normalized = " ".join(str(instructions).split())

    assert "hackathon or challenge opportunity" in normalized
    assert "requires a GitHub repository remains" in normalized
    assert "does not by itself establish event location" in normalized
    assert "report it as unknown" in normalized


def test_opportunity_schema_represents_hackathons_without_repository_misclassification() -> None:
    state = ExistingOpportunityState(
        company_name="Synthetic health challenge",
        status="candidate",
        opportunity_type="hackathon or challenge opportunity",
    )

    assert state.opportunity_type == "hackathon or challenge opportunity"


def test_opportunity_record_dedupes_bundles_and_counts_independent_domains() -> None:
    sources = [
        OpportunitySource(
            source_id=source_id,
            title="Synthetic challenge",
            url=url,
            source_type="conference",
            supported_signal="Official challenge evidence.",
            source_quality=SourceQualityScore(
                url=url,
                title="Synthetic challenge",
                source_type="conference",
                credibility_score=95,
                domain_credibility_score=95,
                recency_score=95,
                relevance_score=95,
                overall_score=95,
                rationale="Official source.",
            ),
        )
        for source_id, url in (
            ("source1", "https://challenge.example/details"),
            ("source2", "https://challenge.example/dates"),
        )
    ]
    quality = SourceQualitySummary(
        source_count=2,
        independent_source_count=2,
        average_credibility_score=95,
        average_domain_credibility_score=95,
        average_recency_score=95,
        average_relevance_score=95,
        overall_score=95,
        high_quality_source_count=2,
        low_quality_source_count=0,
        rationale="Model-provided summary.",
    )
    bundle = OpportunitySourceBundle(
        bundle_id="bundle1",
        company_name="Synthetic challenge",
        source_category="conference",
        summary="Official challenge evidence.",
        sources=sources,
        source_quality_summary=quality.model_copy(deep=True),
    )

    record = OpportunityRecord(
        company_name="Synthetic challenge",
        entity_kind="hackathon",
        opportunity_type="hackathon or challenge opportunity",
        priority_score=60,
        why_now_signal="Upcoming submission window.",
        recommended_next_step="Review before opening.",
        sources=sources,
        source_quality_summary=quality,
        source_bundles=[bundle, bundle.model_copy(deep=True)],
        keystone_fit_reason="Conditional prototype fit.",
        outside_consulting_likelihood=10,
        handoff_to_business_research_analyst=False,
    )

    assert len(record.source_bundles) == 1
    assert record.source_quality_summary is not None
    assert record.source_quality_summary.independent_source_count == 1
    assert record.source_bundles[0].source_quality_summary is not None
    assert record.source_bundles[0].source_quality_summary.independent_source_count == 1


def test_opportunity_result_normalizes_no_search_and_top_level_source_independence() -> None:
    source_urls = (
        "https://challenge.example/details",
        "https://challenge.example/dates",
    )
    sources = [
        OpportunitySource(
            source_id=f"source{index}",
            title="Synthetic challenge",
            url=url,
            source_type="conference",
            supported_signal="Official challenge evidence.",
            source_quality=SourceQualityScore(
                url=url,
                title="Synthetic challenge",
                source_type="conference",
                credibility_score=95,
                domain_credibility_score=95,
                recency_score=95,
                relevance_score=95,
                overall_score=95,
                rationale="Official source.",
            ),
        )
        for index, url in enumerate(source_urls, start=1)
    ]
    quality = SourceQualitySummary(
        source_count=2,
        independent_source_count=2,
        average_credibility_score=95,
        average_domain_credibility_score=95,
        average_recency_score=95,
        average_relevance_score=95,
        overall_score=95,
        high_quality_source_count=2,
        low_quality_source_count=0,
        rationale="Model-provided summary.",
    )
    bundle = OpportunitySourceBundle(
        bundle_id="bundle1",
        company_name="Synthetic challenge",
        source_category="conference",
        summary="Official challenge evidence.",
        sources=sources,
        source_quality_summary=quality.model_copy(deep=True),
    )

    result = OpportunityScoutResult(
        search_provider="none",
        search_queries=["planned but unexecuted query"],
        raw_search_result_count=0,
        source_bundles=[bundle, bundle.model_copy(deep=True)],
        source_quality_summary=quality,
    )

    assert result.search_queries == []
    assert len(result.source_bundles) == 1
    assert result.source_quality_summary is not None
    assert result.source_quality_summary.independent_source_count == 1
    assert result.source_bundles[0].source_quality_summary is not None
    assert result.source_bundles[0].source_quality_summary.independent_source_count == 1


def test_source_verification_can_use_agent_html_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_OPPORTUNITY_SOURCE_VERIFICATION", "true")
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_VERIFY_CAP", "1")
    monkeypatch.setenv("KEYSTONE_AGENT_HTML_REVIEW", "true")
    monkeypatch.setenv("KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES", "1")
    monkeypatch.setattr(
        scout_module,
        "extract_website_content",
        lambda *_args, **_kwargs: WebsiteExtractionResult(
            url="https://www.curebase.com",
            title="Curebase",
            provider="trafilatura",
            status="success",
            text_or_markdown="Curebase provides clinical trial software for research teams.",
            claims=[],
        ),
    )
    monkeypatch.setattr(
        scout_module,
        "run_agent_html_review",
        lambda **_kwargs: HtmlReviewResult(
            url="https://www.curebase.com",
            subject="Curebase",
            claims=["Curebase provides clinical trial software for research teams."],
        ),
    )

    verified, notes = scout_module._verify_source_hits(
        [
            {
                "company_name": "Curebase",
                "source_title": "Curebase",
                "source_url": "https://www.curebase.com",
                "signal": "Official site.",
            }
        ]
    )

    assert verified[0]["agent_html_review_claims"] == [
        "Curebase provides clinical trial software for research teams."
    ]
    assert "clinical trial software" in verified[0]["signal"]
    assert any("Agent HTML review added 1 claim" in note for note in notes)


def test_source_verification_prefers_crawl4ai_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", raising=False)
    monkeypatch.setenv("KEYSTONE_ENABLE_OPPORTUNITY_SOURCE_VERIFICATION", "true")
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_VERIFY_CAP", "1")
    calls: list[str] = []

    def fake_extract(*_args, **kwargs):
        provider = kwargs.get("provider") or "trafilatura"
        calls.append(provider)
        if provider == "trafilatura":
            raise WebsiteExtractionError("empty trafilatura")
        return WebsiteExtractionResult(
            url="https://www.curebase.com",
            title="Curebase",
            provider=str(provider),
            status="success",
            text_or_markdown="Curebase supports decentralized clinical trial operations.",
            claims=["Curebase supports decentralized clinical trial operations."],
        )

    monkeypatch.setattr(scout_module, "extract_website_content", fake_extract)

    verified, notes = scout_module._verify_source_hits(
        [
            {
                "company_name": "Curebase",
                "source_title": "Curebase",
                "source_url": "https://www.curebase.com",
                "signal": "Official site.",
            }
        ],
        verify_source_pages=True,
    )

    assert calls[:2] == ["trafilatura", "crawl4ai"]
    assert "decentralized clinical trial operations" in verified[0]["signal"]
    source = scout_module._source_from_hit(verified[0], str(verified[0]["signal"]))
    assert "decentralized clinical trial operations" in source.evidence_excerpt
    assert any("Verification used crawl4ai" in note for note in notes)


def test_source_verification_uses_public_opportunity_guardrail_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_OPPORTUNITY_SOURCE_VERIFICATION", "true")
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_VERIFY_CAP", "1")
    contexts: list[str] = []

    def fake_extract(*_args, **kwargs):
        contexts.append(str(kwargs.get("guardrail_context") or ""))
        return WebsiteExtractionResult(
            url="https://sam.gov/opp/example",
            title="Example Solicitation",
            provider=str(kwargs.get("provider") or "trafilatura"),
            status="success",
            text_or_markdown=(
                "Behavioral health AI solicitation with proposal deadline June 30, 2026. "
                "For-profit vendors may respond. " * 40
            ),
            claims=["Behavioral health AI solicitation with proposal deadline June 30, 2026."],
        )

    monkeypatch.setattr(scout_module, "extract_website_content", fake_extract)

    verified, notes = scout_module._verify_source_hits(
        [
            {
                "company_name": "SAM.gov",
                "source_title": "Example Solicitation",
                "source_url": "https://sam.gov/opp/example",
                "signal": "Solicitation.",
            }
        ],
        verify_source_pages=True,
    )

    assert contexts == ["public_opportunity_source"]
    assert "For-profit vendors may respond" in verified[0]["signal"]
    assert any("Verified source page" in note for note in notes)


def test_source_verification_guardrail_failure_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_OPPORTUNITY_SOURCE_VERIFICATION", "true")
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_VERIFY_CAP", "1")

    def fake_extract(*_args, **_kwargs):
        raise ToolGuardrailViolation("blocked by safety guardrail")

    monkeypatch.setattr(scout_module, "extract_website_content", fake_extract)

    verified, notes = scout_module._verify_source_hits(
        [
            {
                "company_name": "SAM.gov",
                "source_title": "Example Solicitation",
                "source_url": "https://sam.gov/opp/example",
                "signal": "Solicitation.",
            }
        ],
        verify_source_pages=True,
    )

    assert verified[0]["signal"] == "Solicitation."
    assert any("Verification failed" in note for note in notes)
    assert any("guardrail blocked extraction" in note for note in notes)


def test_source_verification_cache_reuses_unique_page_across_search_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_VERIFY_CAP", "1")
    calls: list[str] = []

    def fake_extract(url: str, **_kwargs) -> WebsiteExtractionResult:
        calls.append(url)
        return WebsiteExtractionResult(
            url=url,
            title="Active grant",
            provider="trafilatura",
            status="success",
            text_or_markdown=(
                "Active behavioral health AI grant. Small businesses are eligible. "
                "Applications are due December 15, 2026."
            ),
            claims=[],
        )

    monkeypatch.setattr(scout_module, "_extract_opportunity_verification_page", fake_extract)
    hit = {
        "company_name": "Example Grant",
        "source_title": "Active grant",
        "source_url": "https://example.test/grant",
        "signal": "Official grant notice.",
    }
    cache: dict[str, dict[str, object]] = {}

    first, _first_notes = scout_module._verify_source_hits(
        [hit],
        verify_source_pages=True,
        verification_cache=cache,
    )
    second, second_notes = scout_module._verify_source_hits(
        [hit],
        verify_source_pages=True,
        verification_cache=cache,
    )

    assert calls == ["https://example.test/grant"]
    assert first[0]["verified_excerpt"] == second[0]["verified_excerpt"]
    assert any("Reused verified source page" in note for note in second_notes)


def test_source_verification_cache_canonicalizes_tracking_url_variants() -> None:
    first = {
        "source_url": "https://program.example.org/apply",
        "source_title": "Program application",
    }
    tracked = {
        "source_url": (
            "https://program.example.org/apply?utm_source=followup#deadline"
        ),
        "source_title": "Program application",
    }
    semantic = {
        "source_url": "https://program.example.org/apply?cycle=2027",
        "source_title": "Program application",
    }

    assert scout_module._source_hit_key(first) == scout_module._source_hit_key(
        tracked
    )
    assert scout_module._source_hit_key(first) != scout_module._source_hit_key(
        semantic
    )


def test_source_verification_reuses_one_managed_extraction_budget_across_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_VERIFY_CAP", "4")
    external_calls: list[str] = []
    budget = WebsiteExtractionBudget(firecrawl_max_calls=1)

    def fake_extract(
        url: str,
        *,
        budget: WebsiteExtractionBudget,
        **_kwargs,
    ) -> WebsiteExtractionResult:
        if not budget.reserve("firecrawl"):
            raise WebsiteExtractionError("Firecrawl extraction budget exhausted")
        external_calls.append(url)
        return WebsiteExtractionResult(
            url=url,
            title="Official program",
            provider="firecrawl",
            status="success",
            text_or_markdown=(
                "Official accelerator program now accepting applications. "
                "Small businesses are eligible through December 15, 2026."
            ),
            claims=["Official accelerator program now accepting applications."],
        )

    monkeypatch.setattr(
        scout_module,
        "_extract_opportunity_verification_page",
        fake_extract,
    )
    cache: dict[str, dict[str, object]] = {}
    first_hit = {
        "company_name": "Program One",
        "source_title": "Program One application",
        "source_url": "https://program-one.example.org/apply",
        "entity_kind": "grant_program",
        "source_type": "company_site",
        "source_category": "grant",
        "signal": "Official application page.",
    }
    second_hit = {
        **first_hit,
        "company_name": "Program Two",
        "source_title": "Program Two application",
        "source_url": "https://program-two.example.org/apply",
    }

    scout_module._verify_source_hits(
        [first_hit],
        verify_source_pages=True,
        verification_cache=cache,
        extraction_budget=budget,
    )
    scout_module._verify_source_hits(
        [second_hit],
        verify_source_pages=True,
        verification_cache=cache,
        extraction_budget=budget,
    )

    assert external_calls == ["https://program-one.example.org/apply"]
    assert budget.firecrawl_calls_attempted == 1
    diagnostics = scout_module._verification_diagnostics_from_cache(cache)
    assert diagnostics["status_counts"] == {"verified": 1, "failed": 1}


def test_source_verification_diagnostics_bound_every_persisted_field() -> None:
    oversized = "x" * 20_000
    diagnostics = scout_module._verification_diagnostics_from_cache(
        {
            "candidate": {
                "_verification_diagnostic": {
                    "url": f"https://example.org/{oversized}",
                    "title": oversized,
                    "entity_kind": oversized,
                    "source_type": oversized,
                    "source_category": oversized,
                    "status": oversized,
                    "provider": oversized,
                    "error_type": oversized,
                }
            }
        }
    )
    attempt = diagnostics["attempts"][0]

    assert len(attempt["url"]) == 500
    assert len(attempt["title"]) == 240
    assert len(attempt["entity_kind"]) == 80
    assert len(attempt["error_type"]) == 120


def test_source_verification_prioritizes_official_program_over_earlier_news(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_VERIFY_CAP", "1")
    calls: list[str] = []

    def fake_extract(url: str, **_kwargs) -> WebsiteExtractionResult:
        calls.append(url)
        return WebsiteExtractionResult(
            url=url,
            title="Behavioral Health AI Accelerator",
            provider="trafilatura",
            status="success",
            text_or_markdown=(
                "Behavioral Health AI Accelerator is now accepting applications. "
                "For-profit U.S. small businesses are eligible. "
                "Applications are accepted through December 15, 2026."
            ),
            claims=[],
        )

    monkeypatch.setattr(scout_module, "_extract_opportunity_verification_page", fake_extract)
    plan = scout_module.infer_opportunity_search_plan(
        "Find current U.S. accelerator or grant programs with official eligibility.",
        desired_count=3,
    )
    hits = [
        {
            "company_name": "Behavioral Health Funding News",
            "entity_kind": "grant_program",
            "source_title": "Five accelerators healthcare startups should watch",
            "source_url": "https://news.example.test/accelerator-list",
            "source_type": "news",
            "source_category": "news",
            "signal": "A roundup of accelerator and funding announcements.",
        },
        {
            "company_name": "Behavioral Health AI Accelerator",
            "entity_kind": "grant_program",
            "source_title": "Behavioral Health AI Accelerator | Apply",
            "source_url": "https://program.example.org/apply",
            "source_type": "company_site",
            "source_category": "grant",
            "signal": "Official program eligibility and application page.",
        },
    ]

    verification_cache: dict[str, dict[str, object]] = {}
    (
        accepted,
        _filtered_hits,
        filtered_candidates,
        _review_candidates,
        _audit_notes,
        _verification_notes,
    ) = scout_module._process_candidate_hits(
        hits,
        topic=(
            "Find current U.S. accelerator or grant programs for a behavioral-health "
            "AI company with official eligibility."
        ),
        search_plan=plan,
        verify_source_pages=True,
        verification_cache=verification_cache,
    )

    assert calls == ["https://program.example.org/apply"]
    assert [hit["company_name"] for hit in accepted] == [
        "Behavioral Health AI Accelerator"
    ]
    assert any(
        candidate["company_name"] == "Behavioral Health Funding News"
        for candidate in filtered_candidates
    )
    diagnostics = scout_module._verification_diagnostics_from_cache(
        verification_cache
    )
    assert diagnostics["status_counts"] == {"verified": 1}
    assert diagnostics["attempts"][0]["url"] == "https://program.example.org/apply"


def test_raw_accelerator_search_result_is_typed_and_prioritized_as_program() -> None:
    plan = scout_module.infer_opportunity_search_plan(
        "Find current U.S. accelerator or grant programs with official eligibility.",
        desired_count=3,
    )
    spec = scout_module._OpportunityQuerySpec(
        lane="grant",
        time_window="current",
        query="behavioral health AI accelerator or grant programs",
        entity_hint="grant_program",
    )
    accelerator = scout_module._search_result_to_hit(
        spec,
        {
            "title": "One Mind Accelerator Applications",
            "url": "https://onemindaccelerator.org/apply",
            "snippet": (
                "Official accelerator program eligibility and applications open "
                "through December 15, 2026."
            ),
            "source_type": "google_search",
        },
    )
    generic_index = scout_module._search_result_to_hit(
        spec,
        {
            "title": "NIMH Funding Opportunities",
            "url": "https://www.nimh.nih.gov/funding/opportunities-announcements",
            "snippet": "General index of NIMH funding opportunities and notices.",
            "source_type": "google_search",
        },
    )
    aggregator = scout_module._search_result_to_hit(
        spec,
        {
            "title": "Acme Accelerator Applications Announced",
            "url": "https://news.example/articles/acme-accelerator",
            "snippet": (
                "News coverage says the accelerator is accepting applications "
                "through December 15, 2026."
            ),
            "source_type": "google_search",
        },
    )

    assert accelerator["entity_kind"] == "grant_program"
    assert accelerator["source_category"] == "grant"
    assert accelerator["source_type"] == "company_site"
    assert aggregator["entity_kind"] == "grant_program"
    assert aggregator["source_category"] == "grant"
    assert aggregator["source_type"] == "google_search"
    assert scout_module._opportunity_verification_priority(
        accelerator,
        search_plan=plan,
    ) > scout_module._opportunity_verification_priority(
        generic_index,
        search_plan=plan,
    )
    assert scout_module._opportunity_verification_priority(
        accelerator,
        search_plan=plan,
    ) > scout_module._opportunity_verification_priority(
        aggregator,
        search_plan=plan,
    )


@pytest.mark.parametrize(
    "text",
    [
        "Now accepting applications for the 2026 accelerator cohort.",
        "The upcoming application window opens October 1, 2026.",
        "Applications accepted through December 15, 2026.",
    ],
)
def test_current_application_window_phrases_are_active(text: str) -> None:
    reasons = scout_module._active_opportunity_reasons(
        title="Behavioral Health AI Accelerator",
        url="https://program.example.org/apply",
        snippet=text,
    )
    details = scout_module._opportunity_detail_fields(
        {
            "source_title": "Behavioral Health AI Accelerator",
            "source_url": "https://program.example.org/apply",
            "signal": text,
            "verified_excerpt": text,
        }
    )

    assert reasons
    assert details["opportunity_status"] == "open"


def test_closed_application_window_overrides_active_wording() -> None:
    details = scout_module._opportunity_detail_fields(
        {
            "source_title": "Behavioral Health AI Accelerator",
            "source_url": "https://program.example.org/apply",
            "signal": (
                "The application window is closed and the program is no longer "
                "accepting applications."
            ),
            "verified_excerpt": "Applications were accepted through January 15, 2026.",
        }
    )

    assert details["opportunity_status"] == "closed_or_expired"


@pytest.mark.parametrize(
    "text",
    [
        "The application window for the 2025 cohort ran January through March 2025.",
        "Application period details will be announced later.",
        "Review application window requirements before the next cycle is announced.",
        "Archived opportunity: the application window is no longer active.",
    ],
)
def test_historical_or_unannounced_application_windows_are_not_active(
    text: str,
) -> None:
    reasons = scout_module._active_opportunity_reasons(
        title="Behavioral Health AI Accelerator",
        url="https://program.example.org/archive",
        snippet=text,
    )

    assert "source text includes active opportunity evidence" not in reasons


def test_candidate_admission_diagnostics_separate_filtered_and_review_reasons() -> None:
    filtered = [
        {
            "company_name": "Example Program",
            "entity_kind": "grant_program",
            "source_category": "grant",
            "source_url": "https://program.example.org/apply",
            "reasons": ["applicant eligibility was not verified"],
        }
    ]
    review = [
        {
            **filtered[0],
            "reasons": [
                "applicant eligibility was not verified",
                "official source requires review",
            ],
        }
    ]

    diagnostics = scout_module._candidate_admission_diagnostics(
        filtered_candidates=filtered,
        review_candidates=review,
    )

    assert diagnostics["reason_counts"] == {
        "applicant eligibility was not verified": 1
    }
    assert diagnostics["filtered_reason_counts"] == {
        "applicant eligibility was not verified": 1
    }
    assert diagnostics["review_reason_counts"] == {
        "applicant eligibility was not verified": 1,
        "official source requires review": 1,
    }
    assert {sample["disposition"] for sample in diagnostics["samples"]} == {
        "filtered",
        "review",
    }


def test_verification_excerpt_retains_late_formal_opportunity_details() -> None:
    text = (
        "Official NIH funding opportunity overview. "
        + ("General program description. " * 120)
        + "Application Due Dates\nJune 15, 2026\nOctober 15, 2026\n"
        + ("Additional requirements. " * 120)
        + "For-Profit Organizations\nSmall Businesses\n"
        + "Applications must be submitted electronically through the How to Apply guide."
    )

    excerpt = scout_module._opportunity_verification_excerpt(text)

    assert len(excerpt) <= 5000
    assert "October 15, 2026" in excerpt
    assert "For-Profit Organizations" in excerpt
    assert "Applications must be submitted electronically" in excerpt


def test_opportunity_deadline_uses_next_upcoming_date_from_due_date_table() -> None:
    text = (
        "Application Due Dates\nFebruary 18, 2025\nJune 15, 2026\n"
        "October 15, 2026\nFebruary 17, 2027"
    )

    assert scout_module._opportunity_deadline_from_text(text) == date(2026, 10, 15)


def test_opportunity_details_prioritize_explicit_for_profit_eligibility_block() -> None:
    details = scout_module._opportunity_detail_fields(
        {
            "source_title": "Official NIH funding opportunity",
            "source_url": "https://grants.nih.gov/example",
            "signal": "Applications are open.",
            "verified_excerpt": (
                "Application Due Dates October 15, 2026. "
                "For-Profit Organizations - Small Businesses - For-Profit Organizations "
                "(Other than Small Businesses). Applications must be submitted electronically."
            ),
        }
    )

    assert details["deadline"] == "2026-10-15"
    assert "For-Profit Organizations" in details["eligibility_summary"]
    assert "Small Businesses" in details["eligibility_summary"]
    assert details["access_mode"] == "remote_or_virtual"


def test_opportunity_fixture_can_validate() -> None:
    data = json.loads((FIXTURES / "sample_lead_curebase.json").read_text(encoding="utf-8"))
    opportunity = Opportunity(
        company_name=data["company_name"],
        title=data["title"],
        score=0.7,
        rationale=data["notes"],
        next_step="Run business research before any outreach.",
    )

    assert opportunity.company_name == "Curebase"


def test_scout_fixture_returns_top_5_or_fewer() -> None:
    result = scout_opportunities_fixture(max_results=5)

    assert len(result.records) <= 5
    assert result.outreach_generated is False


def test_diverse_behavioral_health_ai_fixture_returns_ranked_sources_without_padding() -> None:
    result = scout_opportunities_fixture(
        topic="Find behavioral-health AI opportunities relevant to KNI.",
        max_results=5,
    )

    assert 0 < len(result.records) <= 5
    assert [record.priority_score for record in result.records] == sorted(
        (record.priority_score for record in result.records),
        reverse=True,
    )
    assert all(record.sources for record in result.records)
    assert all(not record.weak_evidence_reasons for record in result.records)
    assert all(record.recommended_next_step for record in result.records)
    assert result.deduped_candidate_count == len(result.records)
    assert result.decision_trace is not None
    assert "no_outreach_generation" in result.decision_trace.safety_gates_applied
    assert result.dry_run is True
    assert result.outreach_generated is False


def test_priority_scores_are_bounded() -> None:
    result = scout_opportunities_fixture(max_results=10)

    assert result.records
    assert all(0 <= record.priority_score <= 100 for record in result.records)
    assert result.decision_trace is not None
    assert result.decision_trace.selected_route == "opportunity_scout"
    assert "no_outreach_generation" in result.decision_trace.safety_gates_applied
    assert result.decision_trace.scoring_components
    assert all(
        record.score_breakdown.priority_score == record.priority_score for record in result.records
    )


def test_fixture_ranking_uses_analyst_components() -> None:
    result = scout_opportunities_fixture(max_results=5)

    assert [record.company_name for record in result.records[:3]] == [
        "Curebase",
        "MindSpan Digital Health",
        "NeuroFlow",
    ]
    top = result.records[0]
    assert top.score_breakdown.relevance_score >= 90
    assert top.score_breakdown.keystone_fit_score >= 90
    assert top.score_breakdown.source_confidence_score > 0
    assert top.score_breakdown.urgency_score >= 90
    assert top.score_breakdown.next_action_clarity_score >= 80
    assert "Priority" in top.score_rationale
    assert top.score_breakdown.component_rationales


def test_each_record_has_signal_next_step_and_sources() -> None:
    result = scout_opportunities_fixture(max_results=5)

    for record in result.records:
        assert record.why_now_signal
        assert record.recommended_next_step
        assert record.sources
        assert record.source_quality_summary is not None
        assert record.score_breakdown.source_confidence_score == (
            record.source_quality_summary.overall_score
        )


def test_opportunity_claims_reference_source_ids() -> None:
    result = scout_opportunities_fixture(max_results=5)

    for record in result.records:
        source_ids = {source.source_id for source in record.sources}
        assert record.claims
        assert all(claim.claim_text for claim in record.claims)
        assert all(claim.source_id in source_ids for claim in record.claims)
        assert all(0.0 <= claim.confidence <= 1.0 for claim in record.claims)


def test_high_priority_records_handoff_to_business_research_analyst() -> None:
    result = scout_opportunities_fixture(max_results=10)
    high_priority = [record for record in result.records if record.priority_score >= 70]

    assert high_priority
    assert all(record.handoff_to_business_research_analyst for record in high_priority)
    assert all(
        "Business Research Analyst" in record.recommended_next_step for record in high_priority
    )
    assert all(record.business_research_analyst_handoff_recommendation for record in high_priority)
    assert all(record.research_needed for record in high_priority)


def test_no_outreach_draft_generated() -> None:
    result = scout_opportunities_fixture(max_results=10)

    assert result.outreach_generated is False
    assert all(record.outreach_draft is None for record in result.records)
    assert all(record.approval_required_before_outreach for record in result.records)
    assert all(record.approved_for_outreach is False for record in result.records)
    assert "No outreach drafts were generated." in result.audit_notes


def test_opportunity_scout_does_not_generate_outbound_copy() -> None:
    result = scout_opportunities_fixture(max_results=3)
    payload = result.model_dump()
    text = json.dumps(payload, sort_keys=True)

    assert result.outreach_generated is False
    assert "email_body" not in text
    assert "linkedin_note" not in text
    assert "follow_up" not in text
    assert "subject line" not in text.lower()


def test_custom_fixture_ranking_is_deterministic(tmp_path: Path) -> None:
    fixture = tmp_path / "opportunities.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "company_name": "Low Signal Lab",
                    "opportunity_type": "grant or collaboration opportunity",
                    "signal": "Conference mention without business trigger.",
                    "signals": ["conference activity"],
                    "source_title": "Fixture conference note",
                    "source_url": "fixture://low-signal",
                    "source_type": "fixture",
                },
                {
                    "company_name": "Strong Trial AI",
                    "opportunity_type": "trial technology",
                    "signal": "Hiring clinical and evidence teams for a clinical trial launch.",
                    "signals": [
                        "hiring clinical",
                        "hiring evidence",
                        "clinical trial launch",
                    ],
                    "source_title": "Fixture trial launch hiring",
                    "source_url": "fixture://strong-trial-ai",
                    "source_type": "fixture",
                },
            ]
        ),
        encoding="utf-8",
    )

    result = scout_opportunities_fixture(fixture=fixture, max_results=2)

    assert [record.company_name for record in result.records] == [
        "Strong Trial AI",
        "Low Signal Lab",
    ]
    assert result.records[0].priority_score > result.records[1].priority_score
    assert result.records[0].handoff_to_business_research_analyst is True
    assert result.records[1].handoff_to_business_research_analyst is False


def test_prompt_is_loaded_from_markdown() -> None:
    agent = build_opportunity_scout_agent()
    prompt = load_prompt("opportunity_scout.md")

    assert prompt.strip() in agent.instructions
    assert "`search_web`" in prompt
    assert "SearXNG plus a capped Agents hosted web-search lane" in prompt
    assert "Apify or Browserless enrichment" in prompt


def test_explicit_tools_are_fixture_safe() -> None:
    search_result = json.loads(
        search_opportunity_sources_placeholder_impl(
            topic="behavioral health",
            max_results=2,
            dry_run=True,
        )
    )
    scored = json.loads(
        score_opportunity_impl(
            company_name="Fixture Co",
            opportunity_type="behavioral health AI",
            signals=["payer partnership", "validation study"],
        )
    )
    handoff = json.loads(
        handoff_to_business_research_analyst_placeholder_impl(
            company_name="Fixture Co",
            reason="High-priority fixture score.",
        )
    )
    saved = json.loads(
        save_opportunity_placeholder_impl(
            json.dumps({"company_name": "Fixture Co"}),
            dry_run=True,
        )
    )
    state = json.loads(
        load_existing_opportunity_state_impl(
            json.dumps([{"company_name": "Fixture Co", "status": "candidate"}]),
            dry_run=True,
        )
    )

    assert search_result["mode"] == "dry_run"
    assert scored["handoff_to_business_research_analyst"] is True
    assert handoff["handoff_to_business_research_analyst"] is True
    assert saved["outreach_generated"] is False
    assert state["state_count"] == 1


def test_structured_source_tools_return_dry_run_bundles() -> None:
    tool_results = [
        json.loads(search_funding_news_sources_impl(max_results=1)),
        json.loads(search_job_posting_sources_impl(max_results=1)),
        json.loads(search_clinical_trials_sources_impl(max_results=1)),
        json.loads(search_grant_sources_impl(max_results=1)),
        json.loads(search_conference_publication_sources_impl(max_results=1)),
        json.loads(search_company_page_sources_impl(max_results=1)),
    ]

    assert all(result["mode"] == "dry_run" for result in tool_results)
    assert all(result["live_status"] == "not_implemented" for result in tool_results)
    assert all(result["source_bundles"] for result in tool_results)
    assert {result["source_bundles"][0]["source_category"] for result in tool_results} >= {
        "news",
        "job_posting",
        "clinical_trial",
        "grant",
        "conference",
        "company_page",
    }


def test_structured_source_live_paths_are_gated() -> None:
    with pytest.raises(RuntimeError, match="Live funding news source search"):
        search_funding_news_sources_impl(dry_run=False)

    with pytest.raises(RuntimeError, match="Live opportunity pipeline state loading"):
        load_existing_opportunity_state_impl(dry_run=False)


def test_existing_opportunity_state_skips_and_updates() -> None:
    existing_state = json.dumps(
        [
            {"company_name": "Curebase", "status": "drafted"},
            {"company_name": "NeuroFlow", "status": "candidate"},
            {"company_name": "Beacon CNS Therapeutics", "status": "rejected"},
        ]
    )

    result = scout_opportunities_fixture(max_results=5, existing_state=existing_state)
    company_names = [record.company_name for record in result.records]
    neuroflow = next(record for record in result.records if record.company_name == "NeuroFlow")

    assert "Curebase" not in company_names
    assert "Beacon CNS Therapeutics" not in company_names
    assert sorted(result.duplicate_companies_skipped) == [
        "Beacon CNS Therapeutics",
        "Curebase",
    ]
    assert neuroflow.state_action == "update_existing"
    assert neuroflow.existing_state is not None
    assert neuroflow.existing_state.status == "candidate"


def test_stale_and_weak_evidence_are_structured_for_review(tmp_path: Path) -> None:
    fixture = tmp_path / "weak-opportunities.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "company_name": "Low Proof AI",
                    "opportunity_type": "grant or collaboration opportunity",
                    "signal": "Old conference mention with no current buyer or validation trigger.",
                    "signals": ["conference activity"],
                    "source_title": "Old aggregator mention",
                    "source_url": "https://unknown.example/old-low-proof-ai",
                    "source_type": "unknown",
                    "source_category": "search",
                    "published_at": "2019-01-01",
                    "missing_evidence": ["No recent hiring, funding, or validation source."],
                }
            ]
        ),
        encoding="utf-8",
    )

    result = scout_opportunities_fixture(fixture=fixture, max_results=1)
    record = result.records[0]

    assert record.source_quality_summary is not None
    assert record.source_quality_summary.overall_score < 50
    assert record.stale_signal_count == 1
    assert "Refresh stale opportunity signals before outreach." in record.missing_evidence
    assert record.weak_evidence_reasons
    assert record.priority_score < 70
    assert record.handoff_to_business_research_analyst is False


def test_high_confidence_source_backed_opportunity_has_bundles(tmp_path: Path) -> None:
    fixture = tmp_path / "strong-opportunities.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "company_name": "AffectAI Research",
                    "opportunity_type": "behavioral health AI",
                    "signal": "Company page describes behavioral health AI validation services.",
                    "signals": ["validation study", "product launch"],
                    "source_title": "AffectAI company validation page",
                    "source_url": "https://affectai.example/platform",
                    "source_type": "company_site",
                    "source_category": "company_page",
                    "published_at": "2026-03-01",
                },
                {
                    "company_name": "AffectAI Research",
                    "opportunity_type": "behavioral health AI",
                    "signal": "ClinicalTrials fixture shows a psychiatry validation study launch.",
                    "signals": ["clinical trial launch", "validation study"],
                    "source_title": "AffectAI ClinicalTrials.gov fixture",
                    "source_url": "https://clinicaltrials.gov/study/fixture-affectai-research",
                    "source_type": "clinical_trial",
                    "source_category": "clinical_trial",
                    "published_at": "2026-03-15",
                },
                {
                    "company_name": "AffectAI Research",
                    "opportunity_type": "behavioral health AI",
                    "signal": "Hiring clinical evidence lead and research operations owner.",
                    "signals": ["hiring clinical", "hiring evidence", "hiring research"],
                    "source_title": "AffectAI evidence careers fixture",
                    "source_url": "https://affectai.example/careers/evidence-lead",
                    "source_type": "job_posting",
                    "source_category": "job_posting",
                    "published_at": "2026-04-01",
                },
            ]
        ),
        encoding="utf-8",
    )

    result = scout_opportunities_fixture(fixture=fixture, max_results=1)
    record = result.records[0]

    assert record.company_name == "AffectAI Research"
    assert record.priority_score >= 80
    assert record.source_quality_summary is not None
    assert record.source_quality_summary.independent_source_count >= 2
    assert {bundle.source_category for bundle in record.source_bundles} == {
        "clinical_trial",
        "company_page",
        "job_posting",
    }
    assert result.source_bundles == record.source_bundles


def test_live_search_uses_targeted_serper_queries_and_sources() -> None:
    fake_serper = FakeSerperTool()

    result = scout_opportunities_live_search(
        topic=None,
        max_results=5,
        serper_tool=fake_serper,
    )

    assert fake_serper.queries
    assert any("site:clinicaltrials.gov" in query for query in fake_serper.queries)
    assert any("site:reporter.nih.gov" in query for query in fake_serper.queries)
    assert any("site:.edu" in query for query in fake_serper.queries)
    assert result.dry_run is False
    assert result.records
    assert all(record.sources for record in result.records)


def test_live_search_accepts_normalized_searxng_provider_results() -> None:
    provider = FakeSearxngProvider()

    result = scout_opportunities_live_search(
        topic="digital mental health",
        max_results=1,
        search_provider=provider,
    )

    assert provider.queries
    assert result.dry_run is False
    assert result.records
    source = result.records[0].sources[0]
    assert source.url == "https://example.test/mindspan-validation"
    assert source.source_type == "google_search"
    assert result.source_bundle_quality_notes
    assert any("Replace discovery pointers" in note for note in result.source_bundle_quality_notes)


def test_live_search_rejects_dry_run_provider() -> None:
    with pytest.raises(LiveSearchProviderRequiredError, match="requires a live"):
        scout_opportunities_live_search(
            max_results=1,
            search_provider=DryRunSearchProvider(),
        )


def test_live_search_enriches_source_bundles_dedupes_state_and_explains_handoff() -> None:
    provider = FakeLiveSearchProvider()
    existing_state = json.dumps([{"company_name": "Curebase", "status": "drafted"}])

    result = scout_opportunities_live_search(
        topic="behavioral health AI",
        max_results=4,
        search_provider=provider,
        existing_state=existing_state,
    )

    company_names = [record.company_name for record in result.records]
    assert provider.validated is True
    assert result.search_provider == "serper"
    assert provider.queries
    assert set(result.search_queries) == set(provider.queries)
    assert result.raw_search_result_count == len(provider.queries) * 4
    assert result.deduped_candidate_count == 2
    assert "Curebase" not in company_names
    assert "Curebase" in result.duplicate_companies_skipped
    assert result.records
    record = result.records[0]
    assert record.company_name == "AffectAI Research"
    assert record.handoff_to_business_research_analyst is True
    assert "Business Research Analyst" in record.recommended_next_step
    assert "handoff criteria" in record.business_research_analyst_handoff_recommendation
    assert "before any outreach" in record.business_research_analyst_handoff_recommendation
    assert record.score_breakdown.component_rationales
    assert record.score_breakdown.source_confidence_score == (
        record.source_quality_summary.overall_score
    )
    assert {bundle.source_category for bundle in record.source_bundles} >= {
        "clinical_trial",
        "news",
    }
    assert all(bundle.source_quality_summary is not None for bundle in record.source_bundles)
    source_urls = [source.url for source in record.sources]
    assert source_urls.count("https://clinicaltrials.gov/study/fixture-affectai-live") == 1
    assert result.outreach_generated is False
    assert all(record.outreach_draft is None for record in result.records)


def test_live_search_deduplicates_companies() -> None:
    result = scout_opportunities_live_search(max_results=5, serper_tool=FakeSerperTool())

    company_names = [record.company_name for record in result.records]
    assert company_names.count("NeuroFlow") == 1
    assert "NeuroFlow announces payer partnership outcomes study" not in company_names


def test_live_search_rejects_noisy_url_pdf_book_and_generic_research_hits() -> None:
    provider = FakeNoisyOpportunityProvider(include_valid=True)

    result = scout_opportunities_live_search(
        topic="Find good opportunities for me in digital health.",
        max_results=5,
        search_provider=provider,
    )

    company_names = [record.company_name for record in result.records]
    assert company_names == ["Brightline Health"]
    assert "https" not in company_names
    rejected = {candidate.company_name: candidate for candidate in result.filtered_candidates}
    assert "https" in rejected
    rejected_reasons = " ".join(
        reason for candidate in result.filtered_candidates for reason in candidate.reasons
    )
    assert "URL/title noise" in rejected_reasons
    assert "PDF, book, thesis, or dissertation" in rejected_reasons
    assert "generic research page" in rejected_reasons
    assert any("Candidate acceptance removed" in note for note in result.audit_notes)


def test_live_search_abstains_when_all_candidates_fail_acceptance() -> None:
    provider = FakeNoisyOpportunityProvider(include_valid=False)

    result = scout_opportunities_live_search(
        topic="Find good opportunities for me in digital health.",
        max_results=5,
        search_provider=provider,
    )

    assert result.records == []
    assert result.deduped_candidate_count == 0
    assert result.filtered_candidates
    assert any(
        "No candidates satisfied deterministic acceptance" in note for note in result.audit_notes
    )
    assert result.constraint_relaxation_suggestion


def test_live_search_hard_filters_exclude_noncompliant_roles() -> None:
    provider = FakeFilteredRoleProvider()

    result = scout_opportunities_live_search(
        topic=(
            "Find opportunities, but exclude startups under 10 employees, exclude "
            "on-site roles, exclude unpaid roles, and exclude roles requiring a "
            "full-time practicing clinician."
        ),
        max_results=5,
        search_provider=provider,
    )

    assert provider.queries
    assert [record.company_name for record in result.records] == ["AffectAI"]
    record = result.records[0]
    assert record.role_remote is True
    assert record.role_title == "Senior Clinical Strategy Advisor"
    assert any("25 employees" in note for note in record.role_filter_notes)
    assert all("TinyMind" not in note for note in record.role_filter_notes)
    assert any("Filtered out TinyMind" in note for note in result.audit_notes)
    assert result.filtered_candidates
    assert result.filtered_candidates[0].company_name == "TinyMind"
    assert "full-time practicing clinician" in " ".join(result.filtered_candidates[0].reasons)


def test_live_search_broad_queries_surface_multi_entity_us_relevant_results() -> None:
    provider = FakeBroadOpportunityProvider()

    result = scout_opportunities_live_search(
        topic="USA-focused collaborations across behavioral health, psychiatry, and clinical AI",
        max_results=5,
        search_provider=provider,
    )

    assert any("site:clinicaltrials.gov" in query for query in provider.queries)
    assert any("site:reporter.nih.gov" in query for query in provider.queries)
    assert any("site:.edu" in query for query in provider.queries)
    company_names = [record.company_name for record in result.records]
    assert "Example Institute for Digital Mental Health" in company_names
    assert "Jane Doe" not in company_names
    assert "Neuropsychiatry Innovation Summit 2026" not in company_names
    assert {"Jane Doe", "Neuropsychiatry Innovation Summit 2026"} <= {
        candidate.company_name for candidate in result.filtered_candidates
    }
    assert "Neuropsychiatry Innovation Summit 2026" in {
        candidate.company_name for candidate in result.review_candidates
    }
    assert all(
        "orchestrator or Business Research review" in " ".join(candidate.reasons)
        for candidate in result.review_candidates
    )
    if result.records:
        record_fields = type(result.records[0]).model_fields
        if "entity_kind" in record_fields:
            entity_kinds = {getattr(record, "entity_kind", "") for record in result.records}
            assert "institute" in entity_kinds
            assert "researcher" not in entity_kinds
            assert "conference" not in entity_kinds
        if "usa_relevance" in record_fields:
            assert all(
                "U.S. relevance" in str(getattr(record, "usa_relevance", ""))
                for record in result.records
            )
        if "search_lanes" in record_fields:
            assert any(
                "researcher" in getattr(record, "search_lanes", []) for record in result.records
            )


def test_broad_digital_health_queries_are_compact_and_precision_oriented() -> None:
    topic = (
        "Find good opportunities for me in digital health. Use my known background: "
        "physician-scientist, psychiatry/neuropsychiatry, clinical research, "
        "AI/data science, behavioral health, and Keystone Neuroinformatics."
    )

    queries = scout_module._build_live_queries(topic)
    joined = "\n".join(queries).lower()

    assert "use my known background" not in joined
    assert "physician-scientist" not in joined
    assert "site:businesswire.com" in joined
    assert "site:prnewswire.com" in joined
    assert '"digital health"' in joined


def test_broad_multilane_prompt_does_not_collapse_to_role_only() -> None:
    topic = (
        "find broad behavioral health AI opportunities across funding partnerships "
        "pilots clinical trials conferences grants and advisory roles"
    )

    specs = scout_module._build_live_query_specs(topic)
    lanes = {spec.lane for spec in specs}

    assert "role" not in lanes or len(lanes) > 1
    assert {"company_growth", "collaboration", "grant", "trial"} <= lanes
    assert "institute" in lanes


def test_broad_multilane_company_prompt_does_not_collapse_to_company_growth() -> None:
    topic = (
        "Find 5 broad behavioral health AI opportunities across companies, institutes, "
        "researchers, conferences, grants, clinical trials, partnerships, pilots, "
        "launches, funding, and advisory roles where Keystone could plausibly offer "
        "clinical AI, psychiatry, evidence-generation, or implementation advisory support."
    )

    specs = scout_module._build_live_query_specs(topic)
    lanes = {spec.lane for spec in specs}
    queries = "\n".join(spec.query for spec in specs).lower()

    assert {"company_growth", "collaboration", "researcher", "institute", "grant", "trial"} <= lanes
    assert '"behavioral health ai"' in queries


def test_conference_prompt_uses_conference_first_queries() -> None:
    topic = (
        "Find 5 broad behavioral health AI opportunities across conferences where "
        "Keystone could plausibly offer clinical AI, psychiatry, evidence-generation, "
        "or implementation oriented presentations."
    )

    specs = scout_module._build_live_query_specs(topic)
    lanes = {spec.lane for spec in specs}

    assert lanes == {"conference"}
    assert specs[0].entity_hint == "conference"
    assert "conference" in specs[0].query.lower()
    assert scout_module._is_broad_multilane_request(topic) is False


def test_company_growth_prompt_does_not_collapse_to_role_only() -> None:
    topic = (
        "Find 3 current behavioral health AI companies with recent funding, "
        "partnerships, launches, or hiring signals where Keystone could plausibly "
        "offer clinical AI, psychiatry, evidence-generation, or implementation "
        "advisory support."
    )

    specs = scout_module._build_live_query_specs(topic)
    lanes = {spec.lane for spec in specs}

    assert "company_growth" in lanes
    assert "collaboration" in lanes
    assert len([spec for spec in specs if spec.lane == "role"]) <= 1


def test_institute_prompt_uses_institute_first_queries() -> None:
    topic = (
        "Find 5 U.S.-relevant academic institutes, centers, or programs working on "
        "digital mental health, psychiatry AI, clinical AI, or neuroinformatics where "
        "Keystone could explore research collaboration, implementation support, or "
        "advisory partnership."
    )

    specs = scout_module._build_live_query_specs(topic)
    lanes = {spec.lane for spec in specs}

    assert "institute" in lanes
    assert "role" not in lanes
    assert specs[0].entity_hint == "institute"
    assert scout_module._result_deepening_allowed(topic) is True


def test_researcher_prompt_uses_researcher_first_queries() -> None:
    topic = (
        "Find 5 researchers or principal investigators in psychiatry, behavioral health, "
        "neuroscience, or digital mental health AI who have recent publications, clinical "
        "trials, grants, or implementation projects that could be relevant for "
        "Keystone collaboration."
    )

    specs = scout_module._build_live_query_specs(topic)
    lanes = {spec.lane for spec in specs}

    assert "researcher" in lanes
    assert "role" not in lanes
    assert specs[0].entity_hint == "researcher"
    assert scout_module._result_deepening_allowed(topic) is True


def test_company_growth_advisory_support_does_not_require_advisory_role() -> None:
    hit = {
        "company_name": "Jimini Health",
        "source_title": "Jimini Health raises funding for AI behavioral health platform",
        "source_url": "https://example.test/jimini-health-funding",
        "signal": "Behavioral health AI platform raised funding and launched a partnership.",
        "signals": ["recent funding", "partnership announcement"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Find 3 current behavioral health AI companies with recent funding, "
            "partnerships, launches, or hiring signals where Keystone could plausibly "
            "offer clinical AI, psychiatry, evidence-generation, or implementation "
            "advisory support."
        ),
    )

    assert reasons == []


def test_ai_company_prompt_requires_source_side_ai_or_platform_relevance() -> None:
    hit = {
        "company_name": "MusiCares",
        "source_title": "MusiCares launches suicide prevention resources",
        "source_url": "https://example.test/musicares-resources",
        "signal": "MusiCares launches suicide prevention resources for artists.",
        "signals": ["product launch"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic="Find 3 current behavioral health AI companies with recent funding.",
    )

    assert any("explicit AI" in reason for reason in reasons)


def test_behavioral_health_prompt_allows_adjacent_healthcare_ai() -> None:
    hit = {
        "company_name": "ClinicalFlow AI",
        "source_title": "ClinicalFlow AI partners with health systems on care delivery",
        "source_url": "https://example.test/clinicalflow-partnership",
        "signal": (
            "Healthcare AI company announces a clinical workflow partnership with "
            "provider organizations."
        ),
        "signals": ["partnership announcement"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Find current behavioral health AI partnership or advisory opportunities; "
            "general healthcare AI companies may be relevant when clinically adjacent."
        ),
    )

    assert reasons == []


def test_behavioral_health_prompt_rejects_generic_workforce_ai_partnership() -> None:
    hit = {
        "company_name": "Tech Mahindra and UKG",
        "source_title": "Tech Mahindra and UKG partner on AI workforce management",
        "source_url": "https://example.test/tech-mahindra-ukg",
        "signal": (
            "Partnership uses AI for human capital, payroll, and workforce management "
            "for enterprise customers."
        ),
        "signals": ["partnership announcement"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Find current US behavioral health AI partnership or advisory opportunities; "
            "exclude payments advertising automaker and generic AI infrastructure news."
        ),
    )

    assert any(
        "behavioral-health or adjacent healthcare AI relevance" in reason for reason in reasons
    )


def test_explicit_exclusion_terms_reject_matching_sector_noise() -> None:
    hit = {
        "company_name": "Stellantis and Microsoft",
        "source_title": "Stellantis expands automotive AI partnership",
        "source_url": "https://example.test/stellantis-microsoft-ai",
        "signal": "Automaker announces AI partnership for vehicle customer experience.",
        "signals": ["partnership announcement"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Find current US behavioral health AI partnership or advisory opportunities; "
            "exclude payments advertising automaker and generic AI infrastructure news."
        ),
    )

    assert any("explicitly excluded automaker" in reason for reason in reasons)


def test_unpaid_does_not_trigger_ai_company_constraint() -> None:
    assert (
        scout_module._requires_ai_company_relevance(
            "exclude startups under 10 employees and exclude unpaid roles"
        )
        is False
    )


def test_broad_multilane_advisory_prompt_does_not_require_advisory_for_every_hit() -> None:
    hit = {
        "company_name": "Jimini Health",
        "source_title": "Jimini Health raises funding for behavioral health AI platform",
        "source_url": "https://example.test/jimini-health-funding",
        "signal": "Behavioral health AI company raises funding for expansion.",
        "signals": ["recent funding"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "find broad behavioral health AI opportunities across funding partnerships "
            "pilots clinical trials conferences grants and advisory roles"
        ),
    )

    assert reasons == []


def test_faculty_profile_title_with_credentials_is_not_scored_as_company() -> None:
    hit = scout_module._search_result_to_hit(
        scout_module._OpportunityQuerySpec(
            lane="researcher",
            time_window="current",
            query="behavioral health AI faculty collaboration",
            entity_hint="researcher",
        ),
        {
            "title": "Deborah Pitts PhD, OTR/L, BCMH, CPRP, FAOTA",
            "url": "https://example.edu/people/faculty/deborah-pitts",
            "snippet": "Faculty profile for a researcher in mental health and rehabilitation.",
        },
    )

    assert hit["entity_kind"] == "researcher"
    assert hit["company_name"] == "Deborah Pitts"
    assert scout_module._candidate_acceptance_rejection_reasons(hit)


def test_dated_newsletter_archive_title_is_not_scored_as_company() -> None:
    hit = scout_module._search_result_to_hit(
        scout_module._OpportunityQuerySpec(
            lane="conference",
            time_window="recent",
            query="behavioral health AI conference workshop",
        ),
        {
            "title": "April 10, 2026 - Volume 27, Number 28",
            "url": "https://example.edu/news-events/twihst/volume-27-number-28",
            "snippet": "Newsletter archive with event and workshop notes.",
        },
    )

    reasons = scout_module._candidate_acceptance_rejection_reasons(hit)

    assert any("newsletter or archive" in reason for reason in reasons)


def test_negative_search_results_page_is_not_scored_as_opportunity() -> None:
    hit = scout_module._search_result_to_hit(
        scout_module._OpportunityQuerySpec(
            lane="grant_funding",
            time_window="current",
            query="site:grants.gov behavioral health AI grant",
            entity_hint="grant_program",
        ),
        {
            "title": "No results found",
            "url": "https://www.grants.gov/search-results.html?keywords=mental+health+ai",
            "snippet": (
                "A site-restricted search for grants.gov with the requested mental health / "
                "AI / funding opportunity terms returned no indexed results."
            ),
        },
    )

    reasons = scout_module._candidate_acceptance_rejection_reasons(hit)

    assert any("negative search-results page" in reason for reason in reasons)
    review_reasons = scout_module._candidate_acceptance_review_reasons(
        hit,
        rejection_reasons=reasons,
    )
    assert review_reasons == []


def test_generic_grants_search_page_is_not_scored_as_specific_opportunity() -> None:
    hit = scout_module._search_result_to_hit(
        scout_module._OpportunityQuerySpec(
            lane="grant_funding",
            time_window="current",
            query="site:simpler.grants.gov mental health grants",
            entity_hint="grant_program",
        ),
        {
            "title": "Search | Simpler.Grants.gov",
            "url": "https://simpler.grants.gov/search?query=programs+mental+health",
            "snippet": (
                "Forecasted opportunity for psychiatry residents appears in a search results table."
            ),
        },
    )

    reasons = scout_module._candidate_acceptance_rejection_reasons(hit)

    assert any("search-results page" in reason for reason in reasons)


def test_article_headline_is_not_scored_as_company_name() -> None:
    reason = scout_module._candidate_name_rejection_reason(
        "There are more AI health tools than ever—but how well do they work?"
    )

    assert "article headline" in reason


def test_short_article_headline_is_not_scored_as_company_name() -> None:
    reason = scout_module._candidate_name_rejection_reason(
        "Mental Health AI Field Misfires on Key Question"
    )

    assert "article headline" in reason


def test_truncated_article_headline_is_not_scored_as_institute_name() -> None:
    reason = scout_module._candidate_name_rejection_reason(
        "Flourishing therapists predict lower premature terminations, recent ..."
    )

    assert "article headline" in reason


def test_generic_study_tools_title_is_not_scored_as_institute_name() -> None:
    reason = scout_module._candidate_name_rejection_reason("Neuroscience : Additional Study Tools")

    assert "article headline" in reason


def test_red_flags_headline_is_not_treated_as_funding_signal() -> None:
    hit = {
        "company_name": "AI Chatbot Prescribing Psychiatric Medications",
        "entity_kind": "company",
        "source_title": "AI Chatbot Prescribing Psychiatric Medications Raises Red Flags",
        "source_url": "https://example.test/ai-chatbot-red-flags",
        "signal": (
            "Article says an AI chatbot prescribing psychiatric medications raises red flags."
        ),
        "source_category": "search",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(hit)

    assert any("article headline" in reason for reason in reasons)
    assert any("source lacks active opportunity evidence" in reason for reason in reasons)


def test_emergency_readiness_video_headline_is_not_scored_as_company_name() -> None:
    reason = scout_module._candidate_name_rejection_reason(
        "Simulations make emergency readiness a regular practice"
    )

    assert "article headline" in reason


def test_next_phase_headline_is_not_scored_as_company_name() -> None:
    reason = scout_module._candidate_name_rejection_reason("From Copilots to Clinical Judgment")

    assert "article headline" in reason


def test_company_search_rejects_listicle_and_roundup_titles_as_company_names() -> None:
    assert "listicle" in scout_module._candidate_name_rejection_reason(
        "Top mental health startups 2026"
    )
    assert "roundup" in scout_module._candidate_name_rejection_reason(
        "Mental Health Funding and News Roundup"
    )
    assert "rundown" in scout_module._candidate_name_rejection_reason("Health Tech Weekly Rundown")
    assert "trend article" in scout_module._candidate_name_rejection_reason(
        "Q1 2026 Healthtech VC Trends"
    )


def test_descriptor_prefixed_company_title_extracts_actual_company_name() -> None:
    name = scout_module._extract_company_name(
        "AI-augmented behavioral health provider Theris launches clinical validation pilot"
    )

    assert name == "Theris"


def test_generic_news_page_title_extracts_domain_company_name() -> None:
    name = scout_module._extract_company_name("News - Modality.AI")

    assert name == "Modality.AI"


def test_technology_driven_company_title_extracts_actual_company_name() -> None:
    name = scout_module._extract_company_name(
        "Technology-Driven Behavioral Health Company SonderMind Raises $150 Million"
    )

    assert name == "SonderMind"


def test_exclusive_prefix_company_title_extracts_actual_company_name() -> None:
    name = scout_module._extract_company_name(
        "Exclusive: Blossom Health raises $20 million to bring an AI operating system"
    )

    assert name == "Blossom Health"


def test_partnership_title_extracts_primary_company_name() -> None:
    name = scout_module._extract_company_name(
        "Talkiatry and New York Cancer & Blood Specialists Partner to Expand Mental Health Access"
    )

    assert name == "Talkiatry"


def test_selects_title_extracts_selected_vendor_name() -> None:
    name = scout_module._extract_company_name(
        "CalMHSA Selects Eleos as Statewide AI Technology Partner for Behavioral Health"
    )

    assert name == "Eleos"


def test_strict_company_prompt_rejects_awards_program_records() -> None:
    plan = scout_module.infer_opportunity_search_plan(
        "Identify 5 mental health AI companies with possible clinical validation needs; "
        "no outreach.",
        desired_count=5,
    )
    hit = {
        "company_name": "MedTech Breakthrough",
        "entity_kind": "company",
        "source_title": (
            "MedTech Breakthrough Announces 2026 Award Winners: Celebrating a Decade "
            "of Health Technology Innovation"
        ),
        "source_url": "https://example.test/2026-medtech-breakthrough-award-winners",
        "signal": "Awards program recognizes digital health and medical technology companies.",
        "signals": ["award winners", "annual awards"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Identify 5 mental health AI companies with possible clinical validation needs; "
            "no outreach."
        ),
        search_plan=plan,
    )

    assert any("awards program" in reason for reason in reasons)


def test_strict_mental_health_ai_company_prompt_rejects_health_system_without_focus() -> None:
    plan = scout_module.infer_opportunity_search_plan(
        "Identify 5 mental health AI companies with possible clinical validation needs; "
        "no outreach.",
        desired_count=5,
    )
    hit = {
        "company_name": "Beth Israel Lahey Health",
        "entity_kind": "company",
        "source_title": (
            "Beth Israel Lahey Health Partners with Heidi to Roll Out Ambient AI Scribing "
            "to All Physicians"
        ),
        "source_url": "https://example.test/bilh-heidi-ambient-ai-rollout",
        "signal": (
            "Integrated delivery network deploys ambient AI scribing across its physician "
            "network to reduce clinician administrative burden."
        ),
        "signals": ["ambient AI", "health system"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Identify 5 mental health AI companies with possible clinical validation needs; "
            "no outreach."
        ),
        search_plan=plan,
    )

    assert any("direct behavioral-health" in reason for reason in reasons)


def test_strict_mental_health_ai_company_prompt_ignores_query_derived_signal() -> None:
    plan = scout_module.infer_opportunity_search_plan(
        "Identify 5 mental health AI companies with possible clinical validation needs; "
        "no outreach.",
        desired_count=5,
    )
    hit = {
        "company_name": "GovWell",
        "entity_kind": "company",
        "source_title": "GovWell Raises $25M Series A for AI Operating System",
        "source_url": "https://example.test/govwell-series-a",
        "signal": "AI operating system for modern government workflows.",
        "signals": ["behavioral health", "AI"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Identify 5 mental health AI companies with possible clinical validation needs; "
            "no outreach."
        ),
        search_plan=plan,
    )

    assert any("direct behavioral-health" in reason for reason in reasons)


def test_strict_ai_company_prompt_rejects_non_ai_behavioral_health_platform() -> None:
    plan = scout_module.infer_opportunity_search_plan(
        "Identify 5 mental health AI companies with possible clinical validation needs; "
        "no outreach.",
        desired_count=5,
    )
    hit = {
        "company_name": "Triad",
        "entity_kind": "company",
        "source_title": "Triad Partners with CCBHC Workforce Accelerator",
        "source_url": "https://example.test/triad-ccbhc",
        "signal": "Behavioral health education platform expands clinician workforce support.",
        "signals": ["behavioral health", "platform"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Identify 5 mental health AI companies with possible clinical validation needs; "
            "no outreach."
        ),
        search_plan=plan,
    )

    assert any("explicit AI" in reason for reason in reasons)


def test_strict_ai_company_prompt_does_not_count_query_derived_ai_signal() -> None:
    plan = scout_module.infer_opportunity_search_plan(
        "Identify 5 mental health AI companies with possible clinical validation needs; "
        "no outreach.",
        desired_count=5,
    )
    hit = {
        "company_name": "Triad",
        "entity_kind": "company",
        "source_title": "Triad Partners with CCBHC Workforce Accelerator",
        "source_url": "https://example.test/triad-ccbhc",
        "signal": "Behavioral health education platform expands clinician workforce support.",
        "signals": ["behavioral health", "hiring AI"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Identify 5 mental health AI companies with possible clinical validation needs; "
            "no outreach."
        ),
        search_plan=plan,
    )

    assert any("explicit AI" in reason for reason in reasons)


def test_study_headline_is_not_scored_as_company_name() -> None:
    reason = scout_module._candidate_name_rejection_reason(
        "New National Construction Safety Study: Small Contractors Improving Safety"
    )

    assert "article headline" in reason


def test_study_headline_is_rejected_when_candidate_name_is_truncated() -> None:
    hit = {
        "company_name": "New National Construction Safety",
        "entity_kind": "company",
        "source_title": (
            "New National Construction Safety Study: Small Contractors Improving Safety "
            "but Gaps Remain in Preconstruction Planning, Technology and Mental Health Support"
        ),
        "source_url": "https://example.test/construction-safety-study",
        "signal": "Study announcement mentions technology and mental health support.",
        "source_category": "search",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(hit)

    assert any("article headline" in reason for reason in reasons)


def test_correction_notice_is_not_scored_as_company_name() -> None:
    reason = scout_module._candidate_name_rejection_reason("CORRECTION")

    assert "correction notice" in reason


def test_unexplained_ticker_like_candidate_name_is_rejected() -> None:
    reason = scout_module._candidate_name_rejection_reason("KOO")

    assert "ticker or acronym" in reason


def test_generic_people_page_is_not_scored_as_institute_name() -> None:
    reason = scout_module._candidate_name_rejection_reason("People")

    assert "URL/title noise" in reason


def test_generic_speakers_page_is_not_scored_as_conference_name() -> None:
    reason = scout_module._candidate_name_rejection_reason("Speakers")

    assert "URL/title noise" in reason


def test_generic_conferences_page_is_not_scored_as_event_name() -> None:
    reason = scout_module._candidate_name_rejection_reason("CONFERENCES")

    assert "URL/title noise" in reason


def test_conference_directory_article_is_not_scored_as_event() -> None:
    reason = scout_module._candidate_name_rejection_reason("Top Mental Health Conferences 2026")

    assert "conference directory" in reason


def test_congressional_report_is_not_scored_as_company_name() -> None:
    reason = scout_module._candidate_name_rejection_reason("H. Rept. 119-271")

    assert "congressional report" in reason


def test_researcher_entity_can_be_accepted_for_researcher_prompt_with_active_evidence() -> None:
    hit = {
        "company_name": "Jane Doe",
        "entity_kind": "researcher",
        "source_title": "Jane Doe, PhD | Department of Psychiatry",
        "source_url": "https://example.edu/faculty/jane-doe",
        "signal": "Principal investigator on a behavioral health AI clinical trial collaboration.",
        "source_category": "company_page",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(
        hit,
        topic=(
            "Find 5 researchers or principal investigators in psychiatry, behavioral health, "
            "neuroscience, or digital mental health AI who have recent publications, clinical "
            "trials, grants, or implementation projects that could be relevant for "
            "Keystone collaboration."
        ),
    )

    assert reasons == []


def test_researcher_profile_with_middle_initial_is_detected() -> None:
    assert scout_module._looks_like_named_person(  # noqa: SLF001
        "Amber W. Childs, PhD | Yale School of Medicine",
        "Principal investigator on digital mental health implementation projects.",
    )


def test_researcher_prompt_accepts_pi_profile_activity() -> None:
    hit = {
        "company_name": "Amber W. Childs",
        "entity_kind": "researcher",
        "source_title": "Amber W. Childs, PhD | Yale School of Medicine",
        "source_url": "https://example.edu/profile/amber-childs",
        "signal": "Principal investigator on digital mental health implementation projects.",
        "source_category": "company_page",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(
        hit,
        topic=(
            "Find 5 researchers or principal investigators in psychiatry, behavioral health, "
            "neuroscience, or digital mental health AI who have recent publications, clinical "
            "trials, grants, or implementation projects that could be relevant for "
            "Keystone collaboration."
        ),
    )

    assert reasons == []


def test_institute_prompt_can_accept_affiliate_program_collaboration_page() -> None:
    hit = {
        "company_name": "Artificial Intelligence for Mental Health",
        "entity_kind": "institute",
        "source_title": "Industry Affiliate Program | Artificial Intelligence for Mental Health",
        "source_url": "https://example.edu/ai4mh/industry.html",
        "signal": (
            "Industry affiliate program invites partnership contact for deploying "
            "clinical AI systems."
        ),
        "source_category": "company_page",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(
        hit,
        topic=(
            "Find 5 U.S.-relevant academic institutes, centers, or programs working on "
            "digital mental health, psychiatry AI, clinical AI, or neuroinformatics where "
            "Keystone could explore research collaboration, implementation support, or "
            "advisory partnership."
        ),
    )

    assert reasons == []


def test_institute_lane_treats_edu_affiliate_program_as_institute() -> None:
    entity_kind = scout_module._entity_kind_from_lane(  # noqa: SLF001
        lane="institute",
        title="Industry Affiliate Program | Artificial Intelligence for Mental Health",
        url="https://med.stanford.edu/psychiatry/special-initiatives/ai4mh/industry.html",
        snippet="Partner with the program on clinical AI implementation.",
        entity_hint="institute",
    )

    assert entity_kind == "institute"


def test_researcher_prompt_rejects_company_records_before_scoring() -> None:
    hit = {
        "company_name": "Jimini Health",
        "entity_kind": "company",
        "source_title": "Jimini Health raises funding for AI behavioral health platform",
        "source_url": "https://example.test/jimini-health-funding",
        "signal": "Behavioral health AI platform raised funding and launched a partnership.",
        "signals": ["recent funding", "partnership announcement"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Find 5 researchers or principal investigators in psychiatry, behavioral health, "
            "neuroscience, or digital mental health AI who have recent publications, clinical "
            "trials, grants, or implementation projects that could be relevant for "
            "Keystone collaboration."
        ),
    )

    assert any("researcher or principal investigator" in reason for reason in reasons)


def test_institute_prompt_rejects_company_records_before_scoring() -> None:
    hit = {
        "company_name": "Jimini Health",
        "entity_kind": "company",
        "source_title": "Jimini Health launches AI behavioral health platform partnership",
        "source_url": "https://example.test/jimini-health-partnership",
        "signal": "Behavioral health AI platform launched an implementation partnership.",
        "signals": ["partnership announcement"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Find 5 U.S.-relevant academic institutes, centers, or programs working on "
            "digital mental health, psychiatry AI, clinical AI, or neuroinformatics where "
            "Keystone could explore research collaboration, implementation support, or "
            "advisory partnership."
        ),
    )

    assert any("academic institute" in reason for reason in reasons)


def test_strict_company_prompt_rejects_agency_records_mislabeled_as_company() -> None:
    plan = scout_module.infer_opportunity_search_plan(
        "Identify 5 mental health AI companies with possible clinical validation needs; "
        "no outreach.",
        desired_count=5,
    )
    hit = {
        "company_name": "HHS agency",
        "entity_kind": "company",
        "source_title": "HHS agency announces behavioral health AI initiative",
        "source_url": "https://www.hhs.gov/example",
        "signal": "Government program discussing behavioral health AI validation needs.",
        "signals": ["AI", "clinical validation"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Identify 5 mental health AI companies with possible clinical validation needs; "
            "no outreach."
        ),
        search_plan=plan,
    )

    assert any("strict company search rejected" in reason for reason in reasons)


def test_conference_prompt_rejects_company_records_before_scoring() -> None:
    hit = {
        "company_name": "Jimini Health",
        "entity_kind": "company",
        "source_title": "Jimini Health raises funding for AI behavioral health platform",
        "source_url": "https://example.test/jimini-health-funding",
        "signal": "Behavioral health AI platform raised funding and launched a partnership.",
        "signals": ["recent funding", "partnership announcement"],
    }

    reasons = scout_module._topic_relevance_rejection_reasons(
        hit,
        topic=(
            "Find 5 broad behavioral health AI opportunities across conferences where "
            "Keystone could plausibly offer clinical AI, psychiatry, evidence-generation, "
            "or implementation oriented presentations."
        ),
    )

    assert any("conference" in reason for reason in reasons)


def test_conference_prompt_accepts_conference_presentation_activity() -> None:
    hit = {
        "company_name": "Behavioral Health in the Age of AI",
        "entity_kind": "conference",
        "source_title": "2026 Behavioral Health in the Age of AI Conference",
        "source_url": "https://example.test/behavioral-health-ai-conference",
        "signal": "Conference includes speaker presentations and workshops on clinical AI.",
        "source_category": "conference",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(
        hit,
        topic=(
            "Find 5 broad behavioral health AI opportunities across conferences where "
            "Keystone could plausibly offer clinical AI, psychiatry, evidence-generation, "
            "or implementation oriented presentations."
        ),
    )

    assert reasons == []


def test_conference_prompt_rejects_linkedin_social_posts() -> None:
    hit = {
        "company_name": "AI-Driven Clinical Trial Feasibility Analysis in Minutes",
        "entity_kind": "conference",
        "source_title": "AI-Driven Clinical Trial Feasibility Analysis in Minutes - LinkedIn",
        "source_url": "https://www.linkedin.com/posts/example",
        "signal": "LinkedIn post mentions a presentation.",
        "source_category": "search",
    }

    reasons = scout_module._candidate_acceptance_rejection_reasons(
        hit,
        topic=(
            "Find 5 broad behavioral health AI opportunities across conferences where "
            "Keystone could plausibly offer clinical AI, psychiatry, evidence-generation, "
            "or implementation oriented presentations."
        ),
    )

    assert any("social post" in reason for reason in reasons)


def test_broad_search_deepens_result_pages_when_underfilled() -> None:
    provider = FakeDeepeningProvider()

    result = scout_opportunities_live_search(
        topic=(
            "find broad behavioral health AI opportunities across funding partnerships "
            "pilots clinical trials conferences grants and advisory roles"
        ),
        max_results=3,
        search_provider=provider,
    )

    assert len(result.records) == 1
    assert result.records[0].company_name == "Jimini Health"
    assert any(request.page == 2 for request in provider.requests)
    assert any(request.page == 2 and request.num_results == 8 for request in provider.requests)
    assert any("Result deepening requested" in note for note in result.audit_notes)


def test_institute_search_broadens_when_requested_count_is_underfilled() -> None:
    provider = FakeInstituteUnderfillProvider()

    result = scout_opportunities_live_search(
        topic=(
            "Find 5 U.S.-relevant academic institutes, centers, or programs working on "
            "digital mental health, psychiatry AI, clinical AI, or neuroinformatics where "
            "Keystone could explore research collaboration, implementation support, or "
            "advisory partnership."
        ),
        max_results=5,
        search_provider=provider,
    )

    assert len(result.records) >= 2
    assert any("Underfilled search broadened" in note for note in result.audit_notes)
    assert any("partner with us" in request.query.lower() for request in provider.requests)


def test_company_growth_plan_broadens_partnership_advisory_underfill() -> None:
    class FakeCompanyGrowthUnderfillProvider:
        provider_name = "searxng"
        dry_run = False

        def __init__(self) -> None:
            self.requests = []

        def validate_configuration(self) -> None:
            return None

        def search_structured(self, request) -> list[SearchResult]:
            self.requests.append(request)
            query = request.query.lower()
            if "behavioral health technology" in query and "funding" in query:
                return [
                    SearchResult(
                        title="Jimini Health raises funding for behavioral health AI platform",
                        link="https://example.test/jimini-health-funding",
                        snippet=(
                            "Jimini Health raises a 2026 funding round and announces a "
                            "behavioral health AI partnership for clinical validation."
                        ),
                        source="searxng",
                    )
                ][: request.num_results]
            return [
                SearchResult(
                    title="Artificial Intelligence for Mental Health Monitoring",
                    link="https://pmc.ncbi.nlm.nih.gov/articles/PMC12745907/",
                    snippet=(
                        "Publication article about artificial intelligence for mental "
                        "health monitoring."
                    ),
                    source="searxng",
                )
            ][: request.num_results]

    provider = FakeCompanyGrowthUnderfillProvider()
    search_plan = scout_module.OpportunitySearchPlan(
        source="test",
        desired_count=3,
        target_entity_types=["company"],
        objectives=["company_growth", "advisory"],
        domains=["behavioral health"],
    )

    result = scout_opportunities_live_search(
        topic=(
            "active behavioral health AI partnership or advisory opportunities relevant to Keystone"
        ),
        max_results=3,
        search_plan=search_plan,
        search_provider=provider,
    )

    assert len(result.records) == 1
    assert result.records[0].company_name == "Jimini Health"
    assert any("Underfilled search broadened" in note for note in result.audit_notes)
    assert any(
        "behavioral health technology" in request.query.lower() for request in provider.requests
    )


def test_live_search_passes_provider_specific_query_hints() -> None:
    requests = []

    class StructuredProvider:
        dry_run = False

        def validate_configuration(self) -> None:
            return None

        def search_structured(self, request):
            requests.append(request)
            return [
                SearchResult(
                    title="MindCare AI announces psychiatry advisory board",
                    link="https://example.com/mindcare-ai-advisory",
                    snippet=(
                        "MindCare AI announced a psychiatry clinical advisory board "
                        "opportunity in the United States."
                    ),
                    source="serper",
                    date="2026-04-20",
                )
            ]

    scout_opportunities_live_search(
        topic="find one psychiatry AI advisory opportunities",
        max_results=1,
        search_provider=StructuredProvider(),
    )

    assert requests
    assert {request.country for request in requests} == {"US"}
    assert {request.language for request in requests} == {None}
    assert any(request.source == "news" for request in requests)
    assert any(request.time_range == "recent" for request in requests)


def test_live_search_does_not_preserve_obvious_noise_for_review() -> None:
    provider = FakeNoisyOpportunityProvider(include_valid=False)

    result = scout_opportunities_live_search(
        topic="Find good opportunities for me in digital health.",
        max_results=5,
        search_provider=provider,
    )

    assert result.review_candidates == []


def test_live_retrieval_metadata_preserves_source_candidates_for_sdk_synthesis() -> None:
    provider = FakeBroadOpportunityProvider()

    _result, metadata = run_opportunity_scout_live(
        topic="Find good opportunities for me in digital health.",
        max_results=5,
        search_provider_builder=lambda **_kwargs: provider,
    )

    assert metadata["retrieval_ladder"][0]["raw_result_count"] > 0
    assert metadata["retrieved_source_candidates"]
    assert {
        "source_id",
        "title",
        "url",
        "snippet",
    } <= set(metadata["retrieved_source_candidates"][0])


def test_live_search_hard_filters_can_return_no_records_with_explanatory_notes() -> None:
    provider = FakeFilteredRoleProvider()

    result = scout_opportunities_live_search(
        topic=(
            "Find opportunities, but exclude startups under 30 employees, exclude "
            "on-site roles, exclude unpaid roles, and exclude roles requiring a "
            "full-time practicing clinician."
        ),
        max_results=5,
        search_provider=provider,
    )

    assert result.records == []
    assert any("Hard filters removed" in note for note in result.audit_notes)
    assert any("No candidates satisfied" in note for note in result.audit_notes)
    assert {item.company_name for item in result.filtered_candidates} == {
        "AffectAI",
        "TinyMind",
    }
    assert result.constraint_relaxation_suggestion
    assert result.constraint_relaxation_suggestion.startswith("Broaden one hard constraint")


def test_live_search_topic_constraints_reject_adjacent_non_psychiatry_advisory_result() -> None:
    provider = FakeAdjacentClinicalAIProvider()

    result = scout_opportunities_live_search(
        topic="find one psychiatry AI advisory opportunities",
        max_results=1,
        search_provider=provider,
    )

    assert result.records == []
    assert {candidate.company_name for candidate in result.filtered_candidates} == {"Clario"}
    reasons = " ".join(
        reason for candidate in result.filtered_candidates for reason in candidate.reasons
    )
    assert "psychiatry" in reasons
    assert any("Topic relevance removed" in note for note in result.audit_notes)


def test_live_search_adaptive_ladder_runs_after_zero_accepted_candidates() -> None:
    provider = FakeAdaptiveOpportunityProvider()

    result = scout_opportunities_live_search(
        topic="find one psychiatry AI advisory opportunities",
        max_results=1,
        search_provider=provider,
    )

    assert len(result.records) == 1
    assert result.records[0].company_name == "MindCare AI"
    assert any("Adaptive search ladder ran" in note for note in result.audit_notes)
    assert any("advisory board" in query for query in provider.queries)


def test_live_search_runs_coverage_followup_for_missing_source_lanes() -> None:
    provider = FakeCoverageFollowupProvider()

    result = scout_opportunities_live_search(
        topic="find behavioral health AI grant opportunities",
        max_results=2,
        search_provider=provider,
    )

    assert any("site:reporter.nih.gov" in query for query in provider.queries)
    assert any("Coverage-aware search ran" in note for note in result.audit_notes)


def test_remote_compatible_grant_request_does_not_activate_role_filters() -> None:
    topic = "Find one current remote-compatible grant or fellowship opportunity."

    assert scout_module._is_role_search(topic) is False
    assert scout_module._parse_hard_filters(topic).strict_verification is False


def test_explicitly_negated_role_wording_keeps_grant_request_out_of_role_lane() -> None:
    topic = (
        "Find one current grant or fellowship opportunity. Remote-compatible "
        "administration is preferred, but this is not a job or role search."
    )

    assert scout_module._is_role_search(topic) is False
    assert scout_module._parse_hard_filters(topic).strict_verification is False


def test_grant_lane_precedes_incidental_conference_or_trial_markers() -> None:
    assert (
        scout_module._entity_kind_from_lane(
            lane="grant",
            title="PAR-25-182: Mental Disorders R61/R33 Clinical Trial Required",
            url="https://grants.nih.gov/grants/guide/pa-files/PAR-25-182.html",
            snippet="NIH notice of funding opportunity.",
            entity_hint="grant_program",
        )
        == "grant_program"
    )


def test_live_search_deadline_returns_initial_partial_evidence_without_followups() -> None:
    provider = FakeCoverageFollowupProvider()

    result = scout_opportunities_live_search(
        topic="find behavioral health AI grant opportunities",
        max_results=2,
        search_provider=provider,
        retrieval_deadline_seconds=0,
        clock=lambda: 0.0,
    )

    diagnostics = result.retrieval_diagnostics
    assert diagnostics["status"] == "partial"
    assert diagnostics["deadline_seconds"] == 0.0
    assert diagnostics["elapsed_seconds"] == 0.0
    assert diagnostics["stopped_before_stage"] == "coverage_followup"
    assert diagnostics["query_count"] == len(result.search_queries)
    assert diagnostics["raw_search_result_count"] == result.raw_search_result_count
    assert diagnostics["unique_pages_cached"] == 1
    assert diagnostics["candidate_admission"]["filtered_count"] >= 1
    assert diagnostics["verification"]["attempt_count"] == 1
    assert diagnostics["resolved_search_plan"]["target_entity_types"] == [
        "grant_program"
    ]
    assert not any("site:reporter.nih.gov" in query for query in provider.queries)
    assert any("returning bounded partial evidence" in note for note in result.audit_notes)


def test_live_search_rejects_publication_and_generic_program_noise() -> None:
    result = scout_opportunities_live_search(
        topic="find one behavioral health AI partnership or funding opportunity",
        max_results=2,
        search_provider=FakePublicationProgramNoiseProvider(),
    )

    assert result.records == []
    reasons = " ".join(
        reason for candidate in result.filtered_candidates for reason in candidate.reasons
    )
    assert "publication rather than an active opportunity" in reasons
    assert "generic program page" in reasons


def test_live_search_high_relevance_handoff_and_no_outreach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_calls: list[str] = []

    def fake_handoff(company_name: str, reason: str) -> str:
        handoff_calls.append(company_name)
        return json.dumps({"company_name": company_name, "reason": reason})

    monkeypatch.setattr(
        scout_module, "handoff_to_business_research_analyst_placeholder_impl", fake_handoff
    )

    result = scout_opportunities_live_search(max_results=5, serper_tool=FakeSerperTool())
    high_relevance = [record for record in result.records if record.priority_score >= 70]

    assert high_relevance
    assert all(record.handoff_to_business_research_analyst for record in high_relevance)
    assert set(handoff_calls) >= {record.company_name for record in high_relevance}
    assert result.outreach_generated is False
    assert all(record.outreach_draft is None for record in result.records)


def test_live_search_tests_do_not_require_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("pytest must not call the network")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_network)

    result = scout_opportunities_live_search(max_results=2, serper_tool=FakeSerperTool())

    assert len(result.records) <= 2


def test_cli_result_output_is_atomic_and_preserves_complete_payload(tmp_path: Path) -> None:
    import scripts.run_opportunity_scout as cli

    output = tmp_path / "opportunity-result.json"
    payload = {
        "status": "partial",
        "usage": {"requests": 1},
        "output": {
            "search_provider": "none",
            "search_queries": [],
            "source_quality_summary": {"independent_source_count": 1},
        },
    }

    cli._write_result_output_atomic(output, payload)

    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert not output.with_suffix(".json.tmp").exists()


def test_cli_result_output_option_persists_before_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_opportunity_scout as cli

    output = tmp_path / "opportunity-result.json"
    monkeypatch.setattr(sys, "argv", ["run_opportunity_scout.py", "--result-output", str(output)])
    args = cli.build_parser().parse_args()
    payload = {"status": "pass", "usage": {"requests": 1}}

    cli._persist_requested_result(args, payload)

    assert json.loads(output.read_text(encoding="utf-8")) == payload


def test_next_normalization_plan_uses_exact_recovered_source_packet(
    require_local_evidence,
) -> None:
    plan = json.loads(
        require_local_evidence(
            "artifacts/test-pack/next-live-opportunity-normalization-plan.json"
        ).read_text(encoding="utf-8")
    )

    assert plan["execution_ready"] is False
    assert plan["approval_status"] == "completed_within_eight_sequential_run_allowance"
    assert plan["result"]["status"] == "pass"
    assert plan["result"]["openai_requests"] == 1
    assert plan["model"] == "gpt-5.4-mini"
    assert plan["expected_openai_requests"] == 1
    assert plan["budget_usd"] == 0.05
    assert plan["live_search"] is False
    assert plan["provider_reads"] == 0
    assert plan["provider_writes"] == 0
    assert plan["source_packet"]["status"] == "recovered_and_verified"
    assert plan["source_packet"]["source_pages"] == 2
    assert plan["source_packet"]["independent_source_domains"] == 1
    assert set(plan["source_packet"]["urls"]) == {
        "https://hack-for-humanity-summer-26.devpost.com/",
        "https://hack-for-humanity-summer-26.devpost.com/details/dates",
    }


def test_cli_live_search_blocks_disabled_serper_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_opportunity_scout as cli

    monkeypatch.setattr("keystone_agents.config.load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_opportunity_scout.py",
            "--live-search",
            "--no-dry-run",
            "--search-provider",
            "serper",
        ],
    )

    def fail_network(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("network must not be reached while Serper is disabled")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_network)

    with pytest.raises(SystemExit, match="Serper search is disabled"):
        cli.main()


def test_cli_live_search_requires_no_dry_run_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_opportunity_scout as cli

    monkeypatch.setattr("keystone_agents.config.load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("KEYSTONE_LIVE_MODE", raising=False)
    monkeypatch.delenv("KEYSTONE_DRY_RUN", raising=False)
    monkeypatch.delenv("KEYSTONE_ENABLE_LIVE_RESEARCH", raising=False)
    monkeypatch.setenv("SERPER_API_KEY", "test-key-that-must-not-be-used")
    monkeypatch.setattr(sys, "argv", ["run_opportunity_scout.py", "--live-search"])

    def fail_network(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("network must not be reached while dry-run is true")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_network)

    with pytest.raises(SystemExit, match="--no-dry-run"):
        cli.main()


def test_cli_live_search_accepts_existing_state_file_with_mocked_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_opportunity_scout as cli

    state_file = tmp_path / "existing-opportunities.json"
    state_file.write_text(
        json.dumps([{"company_name": "Curebase", "status": "drafted"}]),
        encoding="utf-8",
    )
    provider = FakeLiveSearchProvider()
    monkeypatch.setattr(cli, "build_search_provider", lambda **_kwargs: provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_opportunity_scout.py",
            "--topic",
            "behavioral health AI",
            "--live-search",
            "--no-dry-run",
            "--search-provider",
            "serper",
            "--existing-state",
            str(state_file),
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert provider.validated is True
    assert payload["search_provider"] == "serper"
    assert payload["raw_search_result_count"] == len(provider.queries) * 4
    assert payload["deduped_candidate_count"] == 2
    assert "Curebase" in payload["duplicate_companies_skipped"]
    assert all(record["outreach_draft"] is None for record in payload["records"])


def test_cli_no_dry_run_requires_live_search(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.run_opportunity_scout as cli

    monkeypatch.setattr("keystone_agents.config.load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("KEYSTONE_LIVE_MODE", raising=False)
    monkeypatch.delenv("KEYSTONE_DRY_RUN", raising=False)
    monkeypatch.delenv("KEYSTONE_ENABLE_LIVE_RESEARCH", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_opportunity_scout.py", "--no-dry-run"])

    with pytest.raises(SystemExit, match="requires --live-search"):
        cli.main()


def test_serper_tool_live_accepts_mocked_http_response() -> None:
    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "organic": [
                    {
                        "title": "MindSpan launches validation study",
                        "link": "https://example.test/mindspan",
                        "snippet": "Digital mental health company launches validation study.",
                    }
                ]
            }

    captured: dict[str, object] = {}

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fake_post)
    try:
        result = SerperTool(live=True, api_key="test-key").search(
            "digital mental health validation",
            num_results=1,
        )
    finally:
        monkeypatch.undo()

    assert result
    assert result[0].title == "MindSpan launches validation study"
    assert captured["kwargs"]["json"]["q"] == "digital mental health validation"
