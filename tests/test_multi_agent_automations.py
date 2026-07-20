from __future__ import annotations

import base64
from types import SimpleNamespace

from keystone_agents.multi_agent_automations import (
    run_announcements_research_synthesis,
    run_github_repo_opportunities,
    run_meeting_prep_automation,
)
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
)
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.schemas.research import (
    ResearchArticleSummary,
    ResearchBrief,
    ResearchBriefFact,
    ResearchSourceCitation,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.search_provider import SearchResult, SearxngSearchError
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    WebsiteExtractionResult,
)


def test_meeting_prep_caps_items_and_blocks_external_writes() -> None:
    payload = {
        "window": "Next 7 days",
        "events": [
            {
                "title": "Grant proposal review",
                "start": "2026-05-25T09:00:00-04:00",
                "note": "deadline prep",
            },
            {"title": "Partner intro call", "start": "2026-05-26T10:00:00-04:00"},
            {"title": "Clinical AI strategy meeting", "start": "2026-05-27T11:00:00-04:00"},
            {"title": "Board prep", "start": "2026-05-28T12:00:00-04:00"},
        ],
    }

    result = run_meeting_prep_automation(payload, max_items=3)

    assert result.status == "ok"
    assert result.selected_count == 3
    assert len(result.prep_items) == 3
    assert result.calendar_writes_enabled is False
    assert result.gmail_writes_enabled is False
    assert result.crm_writes_enabled is False
    assert result.orchestrator_review is not None
    assert result.orchestrator_review.approval_boundary_ok is True
    assert any("Orchestrator automation review:" in item for item in result.diagnostics)
    assert "No calendar writes" in result.slack_text


def test_meeting_prep_returns_no_prep_needed_backup() -> None:
    result = run_meeting_prep_automation({"window": "Next 7 days", "events": []})

    assert result.status == "no_prep_needed"
    assert result.selected_count == 0
    assert result.orchestrator_review is not None
    assert "No high-salience meeting preparation" in result.slack_text


def test_github_repo_opportunities_ranks_four_and_blocks_writes() -> None:
    payload = {
        "repositories": [
            {
                "full_name": "openai/openai-agents-python",
                "html_url": "https://github.com/openai/openai-agents-python",
                "description": "Python framework for building AI agents and workflows.",
                "stargazers_count": 15000,
                "forks_count": 1200,
                "license": {"spdx_id": "MIT"},
                "pushed_at": "2026-05-01T00:00:00Z",
                "language": "Python",
                "topics": ["agents", "openai", "workflow"],
            },
            {
                "full_name": "slackapi/python-slack-sdk",
                "html_url": "https://github.com/slackapi/python-slack-sdk",
                "description": "Slack SDK for Python bots and integrations.",
                "stargazers_count": 4000,
                "forks_count": 900,
                "license": {"spdx_id": "MIT"},
                "pushed_at": "2026-04-01T00:00:00Z",
                "language": "Python",
                "topics": ["slack", "python"],
            },
            {
                "full_name": "duckdb/duckdb",
                "html_url": "https://github.com/duckdb/duckdb",
                "description": "Analytical database for data analysis workflows.",
                "stargazers_count": 30000,
                "forks_count": 2500,
                "license": {"spdx_id": "MIT"},
                "pushed_at": "2026-05-03T00:00:00Z",
                "language": "C++",
                "topics": ["data", "analytics"],
            },
            {
                "full_name": "stanfordnlp/stanza",
                "html_url": "https://github.com/stanfordnlp/stanza",
                "description": "NLP tools useful for clinical text analysis.",
                "stargazers_count": 7000,
                "forks_count": 900,
                "license": {"spdx_id": "Apache-2.0"},
                "pushed_at": "2025-12-01T00:00:00Z",
                "language": "Python",
                "topics": ["nlp", "clinical"],
            },
            {
                "full_name": "stale/no-license",
                "html_url": "https://github.com/stale/no-license",
                "description": "Old unrelated project.",
                "stargazers_count": 10,
                "forks_count": 1,
                "archived": True,
                "pushed_at": "2020-01-01T00:00:00Z",
            },
        ]
    }

    result = run_github_repo_opportunities(payload, max_items=4)

    assert result.status == "ok"
    assert result.selected_count == 4
    assert len(result.repositories) == 4
    assert all(repo.full_name != "stale/no-license" for repo in result.repositories)
    assert result.github_writes_enabled is False
    assert result.orchestrator_review is not None
    assert result.orchestrator_review.approval_boundary_ok is True
    assert "No GitHub writes" in result.slack_text
    assert "openai/openai-agents-python" in result.slack_text
    assert "Keystone fit:" in result.slack_text
    assert "Use case:" in result.slack_text
    assert "fit/value score:" in result.slack_text
    assert all(repo.keystone_fit for repo in result.repositories)
    assert all(repo.implementation_use_case for repo in result.repositories)
    assert (
        "Relevant to Keystone's business-agent stack because it may improve agent orchestration"
        not in result.slack_text
    )
    assert any("stale by pushed date" in note for note in result.learning_notes)


def test_github_repo_opportunities_orders_by_keystone_fit_before_popularity() -> None:
    result = run_github_repo_opportunities(
        {
            "repositories": [
                {
                    "full_name": "popular/generic-ui",
                    "html_url": "https://github.com/popular/generic-ui",
                    "description": "Popular UI component library.",
                    "stargazers_count": 90000,
                    "forks_count": 10000,
                    "license": {"spdx_id": "MIT"},
                    "pushed_at": "2026-05-01T00:00:00Z",
                    "language": "TypeScript",
                    "topics": ["ui"],
                },
                {
                    "full_name": "example/agent-workflows",
                    "html_url": "https://github.com/example/agent-workflows",
                    "description": "Python agent workflow automation for Slack and Google Sheets.",
                    "stargazers_count": 250,
                    "forks_count": 40,
                    "license": {"spdx_id": "Apache-2.0"},
                    "pushed_at": "2026-05-02T00:00:00Z",
                    "language": "Python",
                    "topics": ["agents", "workflow", "slack", "google-sheets"],
                },
            ]
        },
        max_items=2,
    )

    assert result.repositories[0].full_name == "example/agent-workflows"
    assert all(repo.full_name != "popular/generic-ui" for repo in result.repositories)


def test_github_repo_opportunities_deprioritizes_low_fit_domain_matches() -> None:
    result = run_github_repo_opportunities(
        {
            "repositories": [
                {
                    "full_name": "example/genome-toolkit",
                    "html_url": "https://github.com/example/genome-toolkit",
                    "description": "Genome toolkit for data analysis pipelines.",
                    "stargazers_count": 5000,
                    "forks_count": 500,
                    "license": {"spdx_id": "MIT"},
                    "pushed_at": "2026-05-01T00:00:00Z",
                    "language": "Python",
                    "topics": ["genomics", "data", "pipeline"],
                },
                {
                    "full_name": "apache/hamilton",
                    "html_url": "https://github.com/apache/hamilton",
                    "description": "A micro-framework to create dataflows from Python functions.",
                    "stargazers_count": 2492,
                    "forks_count": 188,
                    "license": {"spdx_id": "Apache-2.0"},
                    "pushed_at": "2026-05-20T00:00:00Z",
                    "language": "Python",
                    "topics": ["dataflow", "python", "workflow"],
                },
            ]
        },
        max_items=2,
    )

    assert result.repositories[0].full_name == "apache/hamilton"
    assert "typed dataflow" in result.repositories[0].implementation_use_case
    assert all(repo.full_name != "example/genome-toolkit" for repo in result.repositories)


def test_github_repo_opportunities_enriches_before_and_after_selection(monkeypatch) -> None:
    def fake_github_search(query: str, *, per_page: int):
        return [
            {
                "full_name": "example/agent-dataflow",
                "html_url": "https://github.com/example/agent-dataflow",
                "description": "Workflow helper.",
                "stargazers_count": 300,
                "forks_count": 50,
                "license": {"spdx_id": "MIT"},
                "pushed_at": "2026-05-01T00:00:00Z",
                "language": "Python",
                "topics": ["workflow"],
            },
            {
                "full_name": "popular/generic-ui",
                "html_url": "https://github.com/popular/generic-ui",
                "description": "Popular UI library.",
                "stargazers_count": 90000,
                "forks_count": 10000,
                "license": {"spdx_id": "MIT"},
                "pushed_at": "2026-05-01T00:00:00Z",
                "language": "TypeScript",
                "topics": ["ui"],
            },
        ]

    def fake_github_api_json(path: str, *, params=None):
        if path == "/repos/example/agent-dataflow/readme":
            text = (
                "Agent workflow dataflow framework for Slack automation, Google Sheets ingestion, "
                "retrieval evaluation, approval gates, and typed Python pipelines."
            )
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if path == "/repos/popular/generic-ui/readme":
            text = "Generic UI components for web apps."
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if path == "/repos/example/agent-dataflow/contents":
            return [
                {"name": "examples", "type": "dir"},
                {"name": "src", "type": "dir"},
                {"name": "tests", "type": "dir"},
                {"name": "README.md", "type": "file"},
            ]
        if path == "/repos/popular/generic-ui/contents":
            return [{"name": "src", "type": "dir"}]
        if path.endswith("/issues"):
            return [
                {
                    "title": "Add Slack automation example",
                    "labels": [{"name": "enhancement"}, {"name": "documentation"}],
                }
            ]
        if path.endswith("/issues/comments"):
            return [{"body": "recent maintainer comment"}]
        return {}

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._github_search_repositories",
        fake_github_search,
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._github_api_json",
        fake_github_api_json,
    )

    result = run_github_repo_opportunities(
        {"queries": ["agent workflow in:name,description,topics,readme archived:false"]},
        live_search=True,
        max_items=1,
    )

    assert result.repositories[0].full_name == "example/agent-dataflow"
    assert "README reviewed" in result.repositories[0].quality_signals
    assert result.repositories[0].key_paths
    assert result.repositories[0].community_signals
    assert "README reviewed" in result.repositories[0].detailed_review
    assert "Reviewed:" in result.slack_text
    assert any("enrichment inspected" in item for item in result.diagnostics)
    assert any("detail review inspected 1 repo" in item for item in result.diagnostics)


def test_github_repo_opportunities_resorts_after_detail_enrichment(monkeypatch) -> None:
    def fake_github_api_json(path: str, *, params=None):
        if path == "/repos/example/slack-agent/readme":
            text = "Slack agent workflow automation with Google Sheets integrations, approvals, evals, and retrieval."
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if path == "/repos/example/generic-tool/readme":
            text = "Generic command-line helper."
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if path.endswith("/contents"):
            return [{"name": "README.md", "type": "file"}, {"name": "examples", "type": "dir"}]
        if path.endswith("/issues") or path.endswith("/issues/comments"):
            return []
        return {}

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._github_api_json",
        fake_github_api_json,
    )

    result = run_github_repo_opportunities(
        {
            "repositories": [
                {
                    "full_name": "example/generic-tool",
                    "html_url": "https://github.com/example/generic-tool",
                    "description": "Generic workflow utility.",
                    "stargazers_count": 10000,
                    "forks_count": 1000,
                    "license": {"spdx_id": "MIT"},
                    "pushed_at": "2026-05-01T00:00:00Z",
                    "language": "Python",
                    "topics": ["workflow"],
                },
                {
                    "full_name": "example/slack-agent",
                    "html_url": "https://github.com/example/slack-agent",
                    "description": "Small Slack helper.",
                    "stargazers_count": 100,
                    "forks_count": 20,
                    "license": {"spdx_id": "MIT"},
                    "pushed_at": "2026-05-01T00:00:00Z",
                    "language": "Python",
                    "topics": ["slack"],
                },
            ]
        },
        live_search=True,
        max_items=2,
    )

    assert result.repositories[0].full_name == "example/slack-agent"
    assert "Google Sheets" in result.repositories[0].readme_excerpt


def test_github_repo_noassertion_license_is_caveat() -> None:
    result = run_github_repo_opportunities(
        {
            "repositories": [
                {
                    "full_name": "example/noassertion-agent",
                    "html_url": "https://github.com/example/noassertion-agent",
                    "description": "Python agent workflow automation.",
                    "stargazers_count": 1000,
                    "forks_count": 100,
                    "license": {"spdx_id": "NOASSERTION"},
                    "pushed_at": "2026-05-01T00:00:00Z",
                    "language": "Python",
                    "topics": ["agents", "workflow"],
                }
            ]
        },
        max_items=1,
    )

    assert result.repositories[0].license == ""
    assert "license not visible" in "; ".join(result.repositories[0].caveats)


def test_github_repo_opportunities_live_search_uses_github_api_queries(monkeypatch) -> None:
    captured: list[str] = []

    def fake_github_search(query: str, *, per_page: int):
        captured.append(query)
        return [
            {
                "full_name": "example/agent-workflows",
                "html_url": "https://github.com/example/agent-workflows",
                "description": "AI agent workflow automation for data analysis.",
                "stargazers_count": 900,
                "forks_count": 120,
                "license": {"spdx_id": "Apache-2.0"},
                "pushed_at": "2026-05-01T00:00:00Z",
                "language": "Python",
                "topics": ["agents", "data", "workflow"],
            }
        ]

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._github_search_repositories",
        fake_github_search,
    )

    result = run_github_repo_opportunities(
        {
            "queries": [
                "business agents data analysis in:name,description,topics,readme stars:>=50 archived:false"
            ]
        },
        live_search=True,
    )

    assert result.status == "ok"
    assert captured
    assert "in:name,description,topics,readme" in captured[0]
    assert "stars:>=50" in captured[0]
    assert "archived:false" in captured[0]
    assert result.repositories[0].full_name == "example/agent-workflows"


def test_github_repo_opportunities_live_sdk_runs_orchestrator_scout_then_research(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_orchestrator(prompt: str, *, live: bool):
        calls.append("orchestrator")
        assert live is True
        assert "Expected agent chain" in prompt
        return SimpleNamespace(
            output=OrchestratorResult(
                route="opportunity_scout",
                target_agent="opportunity_scout",
                rationale="Opportunity Scout should rank repository opportunities before research synthesis.",
                requires_human_review=False,
                approval_required=False,
            )
        )

    def fake_scout(
        typed_input,
        *,
        live: bool,
        tool_tier: str | None = None,
        attach_tools: bool = True,
        compact_instructions: bool = False,
    ):
        calls.append("opportunity_scout")
        assert live is True
        assert tool_tier == "core_read"
        assert attach_tools is False
        assert compact_instructions is True
        assert "GitHub repository opportunities" in typed_input.topic
        return SimpleNamespace(
            output=OpportunityScoutResult(
                topic=typed_input.topic,
                records=[],
                audit_notes=["Opportunity Scout reviewed repository evidence."],
            )
        )

    def fake_research(
        typed_input,
        *,
        live: bool,
        tool_tier: str | None = None,
        attach_tools: bool = True,
        compact_instructions: bool = False,
    ):
        calls.append("business_research_analyst")
        assert live is True
        assert tool_tier == "core_read"
        assert attach_tools is False
        assert compact_instructions is True
        assert typed_input.target_type == "github_repository_collection"
        assert "Source ID: github_repo_1" in typed_input.source_context
        return SimpleNamespace(
            output=ResearchBrief(
                target_name="Weekly GitHub repository opportunities",
                target_type="github_repository_collection",
                research_goal="Review GitHub repositories.",
                summary="The selected repositories support agent workflow automation and data analysis.",
                key_findings=["Repository evidence is relevant to Keystone agent tooling."],
                sources=[
                    ResearchSourceCitation(
                        source_id="github_repo_1",
                        title="openai/openai-agents-python",
                        url="https://github.com/openai/openai-agents-python",
                        source_type="repository",
                    )
                ],
            )
        )

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations.run_orchestrator_sdk",
        fake_orchestrator,
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations.run_opportunity_scout_sdk",
        fake_scout,
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations.run_business_research_analyst_research_brief_sdk",
        fake_research,
    )

    result = run_github_repo_opportunities(
        {
            "repositories": [
                {
                    "full_name": "openai/openai-agents-python",
                    "html_url": "https://github.com/openai/openai-agents-python",
                    "description": "Python framework for building AI agents and workflows.",
                    "stargazers_count": 15000,
                    "forks_count": 1200,
                    "license": {"spdx_id": "MIT"},
                    "pushed_at": "2026-05-01T00:00:00Z",
                    "language": "Python",
                    "topics": ["agents", "openai", "workflow"],
                }
            ]
        },
        live_sdk=True,
    )

    assert calls == ["orchestrator", "opportunity_scout", "business_research_analyst"]
    assert (
        "Agent chain: Orchestrator -> Opportunity Scout -> Business Research Analyst"
        in result.slack_text
    )
    assert "Orchestrator route: opportunity_scout" in result.slack_text
    assert "Keystone fit:" in result.slack_text
    assert "Use case:" in result.slack_text
    assert (
        "appears to address Python framework for building AI agents and workflows"
        in result.slack_text
    )
    assert result.agent_chain == ["orchestrator", "opportunity_scout", "business_research_analyst"]
    assert any("Only 1 repository candidate" in note for note in result.learning_notes)
    assert result.future_query_suggestions
    assert any("Orchestrator SDK routing completed" in item for item in result.diagnostics)
    assert any("Opportunity Scout SDK synthesis completed." in item for item in result.diagnostics)
    assert any(
        "Business Research Analyst SDK synthesis completed" in item for item in result.diagnostics
    )


def test_meeting_prep_live_sdk_uses_chief_of_staff(monkeypatch) -> None:
    captured = {}

    def fake_chief_sdk(prompt: str, *, live: bool):
        captured["prompt"] = prompt
        captured["live"] = live
        return SimpleNamespace(
            output=ChiefOfStaffResult(
                mode="llm",
                intent="weekly meeting prep",
                summary="Chief of Staff prioritized grant and partner preparation.",
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="meeting-prep",
                    command_text="local:kni-meeting-prep-brief",
                    target_channel="meetings",
                    rationale="Internal scheduled meeting prep.",
                    requires_human_approval_before_post=True,
                ),
                recommended_actions=[
                    "Review grant deadline requirements.",
                    "Prepare partner intro context.",
                ],
                slack_post_allowed=False,
                slack_post_policy="not_allowed",
                slack_target_channel="meetings",
                slack_post_reason="Internal automation target channel.",
            )
        )

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations.run_chief_of_staff_sdk",
        fake_chief_sdk,
    )

    result = run_meeting_prep_automation(
        {
            "window": "Next 7 days",
            "events": [
                {"title": "Grant proposal review", "start": "2026-05-25T09:00:00-04:00"},
                {"title": "Partner intro call", "start": "2026-05-26T10:00:00-04:00"},
            ],
        },
        live_sdk=True,
    )

    assert captured["live"] is True
    assert "Meeting 1: Grant proposal review" in captured["prompt"]
    assert "Chief of Staff prioritized grant" in result.slack_text
    assert "Review grant deadline requirements." in result.slack_text
    assert any("Chief of Staff SDK synthesis completed." in item for item in result.diagnostics)


def test_meeting_prep_suppresses_generic_internal_search_leads(monkeypatch) -> None:
    class GenericProvider:
        def search_web(self, query: str, *, num_results: int = 5):
            return [
                SearchResult(
                    title="Clinical trials | National Kidney Foundation",
                    link="https://www.kidney.org/treatment-support/clinical-trials",
                    snippet="General clinical trials information.",
                    source="searxng",
                )
            ]

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_provider",
        lambda: (GenericProvider(), ""),
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_diagnostics",
        lambda: ["Live search provider: searxng"],
    )

    result = run_meeting_prep_automation(
        {
            "window": "Next 7 days",
            "events": [
                {
                    "title": "KNI Clinical Trials Watch (#grants-and-funding)",
                    "start": "2026-05-27T09:00:00-04:00",
                    "note": "deadline prep",
                }
            ],
        },
        live_search=True,
    )

    assert result.status == "ok"
    assert result.prep_items[0].evidence == []
    assert "National Kidney Foundation" not in result.slack_text
    assert "Research lead:" not in result.slack_text
    assert any("Search skipped" in item for item in result.diagnostics)


def test_meeting_prep_keeps_specific_external_search_leads(monkeypatch) -> None:
    class SpecificProvider:
        def search_web(self, query: str, *, num_results: int = 5):
            return [
                SearchResult(
                    title="Michael Knight - DataScan - LinkedIn",
                    link="https://www.linkedin.com/in/michael-knight-a91a1a7",
                    snippet="Michael Knight works with DataScan.",
                    source="searxng",
                )
            ]

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_provider",
        lambda: (SpecificProvider(), ""),
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_diagnostics",
        lambda: ["Live search provider: searxng"],
    )

    result = run_meeting_prep_automation(
        {
            "window": "Next 7 days",
            "events": [
                {
                    "title": "Michael Knight DataScan collaboration prep",
                    "start": "2026-05-22T09:30:00-04:00",
                }
            ],
        },
        live_search=True,
    )

    assert result.status == "ok"
    assert result.prep_items[0].evidence
    assert "Michael Knight - DataScan - LinkedIn" in result.slack_text


def test_announcements_research_selects_three_to_five_and_keeps_summaries_bounded() -> None:
    payload = {
        "links": [
            {
                "title": "NIMH announces AI psychiatry research program",
                "url": "https://example.org/nimh-ai",
                "snippet": "Program supports clinical AI and mental health research.",
                "source": "NIMH",
                "relevance": ["research_update", "clinical_ai_safety"],
            },
            {
                "title": "FDA digital health guidance update",
                "url": "https://example.org/fda-digital-health",
                "snippet": "Updated guidance for healthcare AI and digital health.",
                "source": "FDA",
            },
            {
                "title": "Behavioral health company launches clinical AI platform",
                "url": "https://example.org/bhb-ai",
                "snippet": "Company launches platform for mental health care operations.",
                "source": "Behavioral Health Business",
            },
            {
                "title": "Depression biomarker clinical trial opens",
                "url": "https://example.org/depression-trial",
                "snippet": "Clinical trial evaluates biomarkers in depression.",
                "source": "ClinicalTrials.gov",
            },
            {
                "title": "Schizophrenia machine learning study published",
                "url": "https://example.org/schizophrenia-ml",
                "snippet": "Machine learning model for psychosis research.",
                "source": "Journal",
            },
            {
                "title": "General market forecast",
                "url": "https://example.org/market",
                "snippet": "Broad market report.",
                "source": "Newswire",
            },
        ]
    }

    result = run_announcements_research_synthesis(payload)

    assert result.status == "ok"
    assert 3 <= result.selected_count <= 5
    assert len(result.summaries) == result.selected_count
    assert all(summary.word_count <= 150 for summary in result.summaries)
    assert result.external_writes_enabled is False
    assert result.orchestrator_review is not None
    assert result.orchestrator_review.approval_boundary_ok is True
    assert any("Orchestrator automation review:" in item for item in result.diagnostics)
    assert "Business Research Analyst weekly announcement synthesis" in result.slack_text


def test_announcements_research_persists_seen_items_and_dedupes(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'announcements.db'}"
    payload = {
        "links": [
            {
                "title": "medRxiv depression AI preprint",
                "url": "https://doi.org/10.1101/2026.01.02.234567?utm_campaign=rss",
                "snippet": "Preprint evaluates AI methods for depression research.",
                "source": "medRxiv",
                "doi": "10.1101/2026.01.02.234567",
                "relevance": ["clinical_ai", "preprint"],
            },
            {
                "title": "medRxiv depression AI preprint duplicate",
                "url": "https://doi.org/10.1101/2026.01.02.234567",
                "snippet": "Duplicate feed item.",
                "source": "medRxiv",
                "doi": "10.1101/2026.01.02.234567",
                "relevance": ["clinical_ai"],
            },
            {
                "title": "FDA AI guidance update",
                "url": "https://example.org/fda-ai-guidance",
                "snippet": "Guidance for healthcare AI.",
                "source": "FDA",
            },
        ]
    }

    result = run_announcements_research_synthesis(
        payload,
        min_items=1,
        max_items=2,
        database_url=database_url,
        automation_run_id="auto_run_123",
    )
    run_announcements_research_synthesis(
        payload,
        min_items=1,
        max_items=2,
        database_url=database_url,
        automation_run_id="auto_run_124",
    )
    store = SQLiteStore(database_url)
    items = store.list_announcement_feed_items(query="depression")

    assert result.status == "ok"
    assert any("Persisted 3 announcement feed item" in item for item in result.diagnostics)
    assert len(items) == 1
    assert items[0].canonical_key == "doi:10.1101/2026.01.02.234567"
    assert items[0].seen_count == 4
    assert items[0].selected is True
    assert items[0].automation_run_id == "auto_run_124"


def test_announcements_research_reports_live_search_diagnostics(
    monkeypatch,
) -> None:
    class FailingProvider:
        def search_web(self, _query: str, *, num_results: int = 5):
            raise SearxngSearchError(
                "SearXNG search request failed for http://127.0.0.1:18080/search: "
                "ConnectionError: connection refused"
            )

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_provider",
        lambda: (FailingProvider(), ""),
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_diagnostics",
        lambda: [
            "Live search provider: searxng",
            "SearXNG base URL: http://127.0.0.1:18080",
            "SearXNG reachability: failed for http://127.0.0.1:18080/search?q=searxng+health+check&format=json: URLError: connection refused",
        ],
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations.extract_website_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(WebsiteExtractionError("offline")),
    )

    result = run_announcements_research_synthesis(
        {
            "links": [
                {
                    "title": "AI safety for mental health chatbots",
                    "url": "https://example.org/a",
                    "snippet": "Mental health AI safety validation.",
                    "source": "Journal",
                },
                {
                    "title": "Clinical AI implementation barriers",
                    "url": "https://example.org/b",
                    "snippet": "Healthcare AI deployment barriers.",
                    "source": "News",
                },
                {
                    "title": "Depression biomarker machine learning",
                    "url": "https://example.org/c",
                    "snippet": "Machine learning biomarkers for depression.",
                    "source": "Preprint",
                },
            ]
        },
        live_search=True,
    )

    assert result.status == "ok"
    assert "Live search provider: searxng" in result.diagnostics
    assert "SearXNG base URL: http://127.0.0.1:18080" in result.diagnostics
    assert any("SearXNG reachability: failed" in item for item in result.diagnostics)
    assert any(
        "Search failed for `AI safety for mental health chatbots`" in item
        for item in result.diagnostics
    )


def test_announcements_research_renders_search_evidence(monkeypatch) -> None:
    class EvidenceProvider:
        def search_web(self, query: str, *, num_results: int = 5):
            return [
                SearchResult(
                    title=f"Primary source for {query}",
                    link="https://example.org/source",
                    snippet="A concrete source snippet about clinical AI safety validation.",
                    source="searxng",
                )
            ]

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_provider",
        lambda: (EvidenceProvider(), ""),
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_diagnostics",
        lambda: ["Live search provider: searxng"],
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations.extract_website_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(WebsiteExtractionError("offline")),
    )

    result = run_announcements_research_synthesis(
        {
            "links": [
                {
                    "title": "AI safety for mental health chatbots",
                    "url": "https://example.org/a",
                    "snippet": "Mental health AI safety validation.",
                    "source": "Journal",
                },
                {
                    "title": "Clinical AI implementation barriers",
                    "url": "https://example.org/b",
                    "snippet": "Healthcare AI deployment barriers.",
                    "source": "News",
                },
                {
                    "title": "Depression biomarker machine learning",
                    "url": "https://example.org/c",
                    "snippet": "Machine learning biomarkers for depression.",
                    "source": "Preprint",
                },
            ]
        },
        live_search=True,
    )

    assert result.summaries[0].evidence
    assert "Search evidence only:" in result.summaries[0].summary
    assert "concrete source snippet" in result.summaries[0].summary


def test_announcements_research_reads_selected_article_pages(monkeypatch) -> None:
    class EvidenceProvider:
        def search_web(self, query: str, *, num_results: int = 5):
            return [
                SearchResult(
                    title=f"Search source for {query}",
                    link="https://example.org/search-source",
                    snippet="Search snippet.",
                    source="searxng",
                )
            ]

    def fake_extract(url: str, **_kwargs):
        return WebsiteExtractionResult(
            url=url,
            title="Readable article page",
            provider="trafilatura",
            status="success",
            text_or_markdown=(
                "The article describes a validated clinical AI safety monitoring workflow "
                "for mental health chatbots and reports deployment-relevant evaluation criteria. "
                "The page explains how automated testing, scenario coverage, risk capture, "
                "and reporting can support safer operational review before mental health "
                "chatbot deployments. It also distinguishes source-backed validation "
                "signals from broader implementation assumptions."
            ),
            claims=[
                "The article describes a validated clinical AI safety monitoring workflow.",
                "It reports deployment-relevant evaluation criteria for mental health chatbots.",
            ],
            metadata={"status_code": 200},
        )

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_provider",
        lambda: (EvidenceProvider(), ""),
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations._live_search_diagnostics",
        lambda: ["Live search provider: searxng"],
    )
    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations.extract_website_content",
        fake_extract,
    )

    result = run_announcements_research_synthesis(
        {
            "links": [
                {
                    "title": "AI safety for mental health chatbots",
                    "url": "https://example.org/a",
                    "snippet": "Mental health AI safety validation.",
                    "source": "Journal",
                },
                {
                    "title": "Clinical AI implementation barriers",
                    "url": "https://example.org/b",
                    "snippet": "Healthcare AI deployment barriers.",
                    "source": "News",
                },
                {
                    "title": "Depression biomarker machine learning",
                    "url": "https://example.org/c",
                    "snippet": "Machine learning biomarkers for depression.",
                    "source": "Preprint",
                },
            ]
        },
        live_search=True,
    )

    assert result.summaries[0].evidence[0].kind == "search"
    assert any(evidence.kind == "article" for evidence in result.summaries[0].evidence)
    assert "Read source:" in result.summaries[0].summary
    assert "validated clinical AI safety monitoring workflow" in result.summaries[0].summary
    assert any(
        "Read article for `AI safety for mental health chatbots`" in item
        for item in result.diagnostics
    )


def test_announcements_research_live_sdk_uses_business_research_analyst(monkeypatch) -> None:
    captured = {}

    def fake_sdk(
        typed_input,
        *,
        live: bool,
        tool_tier: str | None = None,
        attach_tools: bool = True,
        compact_instructions: bool = False,
    ):
        captured["typed_input"] = typed_input
        captured["live"] = live
        captured["tool_tier"] = tool_tier
        captured["attach_tools"] = attach_tools
        captured["compact_instructions"] = compact_instructions
        return SimpleNamespace(
            output=ResearchBrief(
                target_name="Weekly #announcements selected links",
                target_type="article_collection",
                research_goal="Research selected links.",
                summary="The selected links point to clinical AI safety and implementation signals.",
                key_findings=["Clinical AI safety validation is the strongest near-term theme."],
                article_summaries=[
                    ResearchArticleSummary(
                        title="AI safety for mental health chatbots",
                        source_ids=["announcement_1"],
                        key_findings=[
                            "The article describes a validated safety monitoring workflow for mental health chatbots."
                        ],
                        limitations=["Needs primary-source review before operational use."],
                        relevance_to_goal="Relevant to Keystone's clinical AI safety monitoring interests.",
                    )
                ],
                facts=[
                    ResearchBriefFact(
                        text="The source discusses safety monitoring for mental health chatbots.",
                        source_ids=["announcement_1"],
                        confidence=0.82,
                    )
                ],
                inferences=["This may be worth tracking for clinical AI governance."],
                next_steps=["Track the source in research notes."],
                sources=[
                    ResearchSourceCitation(
                        source_id="announcement_1",
                        title="AI safety for mental health chatbots",
                        url="https://example.org/a",
                        source_type="article",
                    )
                ],
            )
        )

    monkeypatch.setattr(
        "keystone_agents.multi_agent_automations.run_business_research_analyst_research_brief_sdk",
        fake_sdk,
    )

    result = run_announcements_research_synthesis(
        {
            "links": [
                {
                    "title": "AI safety for mental health chatbots",
                    "url": "https://example.org/a",
                    "snippet": "Mental health AI safety validation.",
                    "source": "Journal",
                },
                {
                    "title": "Clinical AI implementation barriers",
                    "url": "https://example.org/b",
                    "snippet": "Healthcare AI deployment barriers.",
                    "source": "News",
                },
                {
                    "title": "Depression biomarker machine learning",
                    "url": "https://example.org/c",
                    "snippet": "Machine learning biomarkers for depression.",
                    "source": "Preprint",
                },
            ]
        },
        live_sdk=True,
    )

    assert captured["live"] is True
    assert captured["tool_tier"] == "core_read"
    assert captured["attach_tools"] is False
    assert captured["compact_instructions"] is True
    assert captured["typed_input"].target_type == "article_collection"
    assert "Source ID: announcement_1" in captured["typed_input"].source_context
    assert any(
        "Business Research Analyst SDK synthesis completed." in item for item in result.diagnostics
    )
    assert "validated safety monitoring workflow" in result.summaries[0].summary
