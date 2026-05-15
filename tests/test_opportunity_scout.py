from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from keystone_agents.agents import opportunity_scout as scout_module
from keystone_agents.agents.opportunity_scout import (
    build_opportunity_scout_agent,
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
from keystone_agents.schemas.opportunity import Opportunity, OpportunityScoutResult
from keystone_agents.sdk import load_prompt
from keystone_agents.tools.search_provider import (
    DryRunSearchProvider,
    LiveSearchProviderRequiredError,
    SerperSearchError,
)
from keystone_agents.tools.serper_tool import SearchResult, SerperTool

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
                        "NIH SBIR grant project supports behavioral health AI "
                        "evidence generation."
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
        "score_opportunity",
        "handoff_to_business_research_analyst_placeholder",
        "save_opportunity_placeholder",
    } <= {getattr(tool, "name", "") for tool in agent.tools}


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
    assert "SearXNG first for broad recall" in prompt
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


def test_company_search_rejects_listicle_and_roundup_titles_as_company_names() -> None:
    assert "listicle" in scout_module._candidate_name_rejection_reason(
        "Top mental health startups 2026"
    )
    assert "roundup" in scout_module._candidate_name_rejection_reason(
        "Mental Health Funding and News Roundup"
    )
    assert "rundown" in scout_module._candidate_name_rejection_reason(
        "Health Tech Weekly Rundown"
    )
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


def test_strict_mental_health_ai_company_prompt_rejects_health_system_without_behavioral_focus() -> None:
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


def test_strict_mental_health_ai_company_prompt_does_not_count_query_derived_behavioral_signal() -> None:
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
    assert {request.language for request in requests} == {"en"}
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


def test_cli_live_search_requires_api_key_before_network(
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
        raise AssertionError("network must not be reached without SERPER_API_KEY")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_network)

    with pytest.raises(SystemExit, match="SERPER_API_KEY is required"):
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
