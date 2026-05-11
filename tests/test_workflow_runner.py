from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.models import TypedAgentRunResult
from keystone_agents.reporting import render_work_item_result_text
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.research import (
    ResearchArticleSummary,
    ResearchBrief,
    ResearchBriefFact,
    ResearchSourceCitation,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.website_extraction_tool import WebsiteExtractionResult
from keystone_agents.work_items import approve_artifact_context, drafting_ready, set_next_action
from keystone_agents.workflow_runner import advance_work_item


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'workflow_runner.db'}"


def test_advance_work_item_research_creates_case_and_company_artifact(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    )

    store = SQLiteStore(database_url)
    loaded = store.get_work_item(result.work_item.id)

    assert result.advanced is True
    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.context_pack is not None
    assert result.context_pack["pack_type"] == "research"
    assert result.context_pack["route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert result.status == WorkItemStatus.IN_PROGRESS
    assert result.artifact_refs[0].artifact_type == "company_profile"
    assert result.artifact_refs[0].source_agent == "business_research_analyst"
    assert any(ref.artifact_type == "contact_candidates" for ref in result.artifact_refs)
    assert result.work_item.sources
    assert result.context_pack is not None
    assert result.context_pack["retrieved_sources"]
    assert loaded is not None
    assert loaded.artifact_refs[0].artifact_id == result.artifact_refs[0].artifact_id
    assert store.list_work_item_artifacts(result.work_item.id)[0].artifact_type == "company_profile"


def test_advance_work_item_zotero_collection_creates_research_brief_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    collection_key = "LTA3U8I8"
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS & Lindus Trial Context": collection_key}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "ITEM1",
                            "title": "Remote tDCS randomized trial",
                            "url": "https://pubmed.ncbi.nlm.nih.gov/example/",
                            "DOI": "10.1000/example",
                            "abstractNote": (
                                "This randomized sham-controlled trial tested home-based tDCS "
                                "for major depressive disorder. Depressive symptoms improved "
                                "and discontinuation rates did not differ."
                            ),
                            "itemType": "journalArticle",
                            "collections": [collection_key],
                        }
                    },
                    {
                        "data": {
                            "key": "ITEM2",
                            "title": "Lindus REACH-tDCS trial page",
                            "url": "https://www.lindushealth.com/research/reach-tdcs",
                            "abstractNote": "",
                            "itemType": "webpage",
                            "collections": [collection_key],
                        }
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "Ask the Business Research Analyst to summarize the Zotero collection "
                "'LH 01 - REACH-tDCS & Lindus Trial Context' with one paragraph per source"
            ),
            database_url=_database_url(tmp_path),
            save=True,
        )
    )

    assert result.advanced is True
    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.artifact_refs[0].artifact_type == "research_brief"
    assert result.artifact_refs[0].metadata["target_type"] == "zotero_collection"
    assert result.artifact_refs[0].metadata["source_count"] == 2
    assert "Source summaries" in result.human_summary
    assert "Remote tDCS randomized trial" in result.human_summary
    assert "Link: https://pubmed.ncbi.nlm.nih.gov/example/" in result.human_summary
    assert "\n\n- Lindus REACH-tDCS trial page\n  Link:" in result.human_summary
    rendered = render_work_item_result_text(result)
    assert "Source links:" in rendered
    assert "\n\n- Lindus REACH-tDCS trial page\n  Link:" in rendered
    assert "CompanyProfile" in " ".join(result.audit_notes)


def test_zotero_collection_resolves_full_title_against_shorter_cached_title(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    collection_key = "LTA3U8I8"
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS": collection_key}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "ITEM1",
                            "title": "Remote tDCS randomized trial",
                            "url": "https://pubmed.ncbi.nlm.nih.gov/example/",
                            "abstractNote": "A randomized trial tested home-based tDCS.",
                            "itemType": "journalArticle",
                            "collections": [collection_key],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "summarize the Zotero collection "
                "'LH 01 - REACH-tDCS & Lindus Trial Context'"
            ),
            database_url=_database_url(tmp_path),
            save=True,
        )
    )

    assert result.advanced is True
    assert result.artifact_refs[0].artifact_type == "research_brief"
    assert result.artifact_refs[0].title == "LH 01 - REACH-tDCS"


def test_zotero_collection_resolution_failure_blocks_instead_of_raising(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS": "LTA3U8I8"}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="summarize the Zotero collection 'LH 99' with one paragraph per source",
            database_url=_database_url(tmp_path),
            save=True,
        )
    )

    assert result.advanced is False
    assert result.status == WorkItemStatus.BLOCKED
    assert result.blockers[0].code == "zotero_collection_resolution_failed"
    assert "could not resolve" in result.human_summary


def test_advance_work_item_zotero_article_search_finds_lindus_sooma_trial_item(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS": "LTA3U8I8"}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "AP9SKRPZ",
                            "title": (
                                "Study Details | NCT06976697 | Home-Based tDCS "
                                "Treatment Of Major Depressive Disorder"
                            ),
                            "url": "https://clinicaltrials.gov/study/NCT06976697",
                            "abstractNote": "",
                            "itemType": "webpage",
                            "collections": ["LTA3U8I8"],
                        }
                    },
                    {
                        "data": {
                            "key": "3F7WIKW8",
                            "title": (
                                "Lindus Health and Sooma Medical announce pivotal "
                                "device clinical trial for treatment of MDD"
                            ),
                            "url": (
                                "https://www.lindushealth.com/news/lindus-health-and-"
                                "sooma-medical-announce-pivotal-device-clinical-trial"
                            ),
                            "abstractNote": "",
                            "itemType": "webpage",
                            "collections": ["LTA3U8I8"],
                        }
                    },
                    {
                        "data": {
                            "key": "MD8NCSX9",
                            "title": (
                                "Home-based transcranial direct current stimulation "
                                "treatment for major depressive disorder: a fully "
                                "remote phase 2 randomized sham-controlled trial."
                            ),
                            "url": "https://pubmed.ncbi.nlm.nih.gov/39433921/",
                            "DOI": "10.1038/s41591-024-03305-y",
                            "abstractNote": (
                                "This fully remote randomized sham-controlled trial "
                                "tested home-based tDCS in major depressive disorder."
                            ),
                            "itemType": "journalArticle",
                            "collections": ["LTA3U8I8"],
                        }
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "Ask the Business Research Analysit to find and summarize the Zotero "
                "article on the Lindus SOOMA trial. Include one paragraph summary, "
                "methods/design, inclusion/exclusion, and other relevant trial info"
            ),
            database_url=_database_url(tmp_path),
            save=True,
        )
    )

    assert result.advanced is True
    assert result.artifact_refs[0].artifact_type == "research_brief"
    assert result.artifact_refs[0].metadata["target_type"] == "zotero_article"
    assert "NCT06976697" in result.artifact_refs[0].title
    assert "clinicaltrials.gov/study/NCT06976697" in result.human_summary
    assert "Source ID: zotero:item:AP9SKRPZ" in result.human_summary
    assert "Zotero key: AP9SKRPZ" in result.human_summary
    assert "Requested details" in result.human_summary
    assert "Methods/design" in result.human_summary
    assert "Inclusion and exclusion criteria" in result.human_summary
    assert "Lindus Health and Sooma Medical announce" in result.human_summary
    assert "Next steps" in result.human_summary
    assert "zotero_collection_resolution_failed" not in result.human_summary


def test_advance_work_item_zotero_article_live_sdk_synthesizes_extracted_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS": "LTA3U8I8"}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "AP9SKRPZ",
                            "title": (
                                "Study Details | NCT06976697 | Home-Based tDCS "
                                "Treatment Of Major Depressive Disorder"
                            ),
                            "url": "https://clinicaltrials.gov/study/NCT06976697",
                            "abstractNote": "",
                            "itemType": "webpage",
                            "collections": ["LTA3U8I8"],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))

    def fake_extract(url: str, **kwargs):
        assert url == "https://clinicaltrials.gov/study/NCT06976697"
        assert kwargs["live"] is True
        return WebsiteExtractionResult(
            url=url,
            title="ClinicalTrials.gov NCT06976697",
            provider="trafilatura",
            status="success",
            text_or_markdown=(
                "This is a randomized pivotal trial of remotely supervised "
                "home-based tDCS for major depressive disorder. Eligibility "
                "includes adults with MDD; exclusion criteria include conditions "
                "that make tDCS unsafe."
            ),
        )

    class FakeSearchProvider:
        provider_name = "serper"

        def search_web(self, query: str, num_results: int = 5):
            assert "NCT06976697" in query or "Lindus" in query
            return [
                type(
                    "SearchHit",
                    (),
                    {
                        "title": "ClinicalTrials.gov NCT06976697 trial record",
                        "link": "https://clinicaltrials.gov/study/NCT06976697",
                        "snippet": "Randomized home-based tDCS trial for MDD.",
                        "source": "serper",
                    },
                )()
            ]

    def fake_sdk(typed_input, **kwargs):
        prompt = typed_input.to_prompt()
        assert "Source ID: zotero:item:AP9SKRPZ" in prompt
        assert "Source ID: web_search:1" in prompt
        assert "randomized pivotal trial" in prompt
        assert kwargs["live"] is True
        output = ResearchBrief(
            target_name="NCT06976697 Lindus/Sooma trial",
            target_type="zotero_article",
            research_goal=typed_input.research_goal,
            summary=(
                "The Lindus/Sooma trial is a remotely supervised home-based tDCS "
                "study for major depressive disorder."
            ),
            article_summaries=[
                ResearchArticleSummary(
                    title="NCT06976697 Lindus/Sooma trial",
                    source_ids=["zotero:item:AP9SKRPZ"],
                    research_question="Can remotely supervised home-based tDCS treat MDD?",
                    methods_or_design="Randomized pivotal trial using home-based tDCS.",
                    key_findings=["Trial details were extracted from ClinicalTrials.gov."],
                    limitations=["Eligibility summary should be verified against the registry."],
                    relevance_to_goal="Directly answers the requested trial-summary question.",
                )
            ],
            facts=[
                ResearchBriefFact(
                    text="The study concerns remotely supervised home-based tDCS for MDD.",
                    source_ids=["zotero:item:AP9SKRPZ"],
                    confidence=0.9,
                )
            ],
            sources=[
                ResearchSourceCitation(
                    source_id="zotero:item:AP9SKRPZ",
                    title="Study Details | NCT06976697",
                    url="https://clinicaltrials.gov/study/NCT06976697",
                    source_type="local_zotero:webpage",
                )
            ],
        )
        return TypedAgentRunResult(
            agent_name="business_research_analyst",
            output=output,
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.zotero_research.extract_website_content",
        fake_extract,
    )
    monkeypatch.setattr(
        "keystone_agents.zotero_research.build_search_provider",
        lambda **_kwargs: FakeSearchProvider(),
    )
    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_business_research_analyst_research_brief_sdk",
        fake_sdk,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "Ask the Business Research Analyst to find and summarize the Zotero "
                "article on the Lindus SOOMA trial. Include one paragraph summary, "
                "methods/design, inclusion/exclusion, and other relevant trial info"
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
        )
    )

    assert result.advanced is True
    assert "remotely supervised home-based tDCS" in result.human_summary
    assert "Randomized pivotal trial" in result.human_summary
    assert "Source link: https://clinicaltrials.gov/study/NCT06976697" in result.human_summary
    assert "Source ID: zotero:item:AP9SKRPZ" in result.human_summary
    assert "Zotero key: AP9SKRPZ" in result.human_summary
    assert "Live SDK synthesis executed" in " ".join(result.audit_notes)
    assert result.artifact_refs[0].metadata["retrieval"]["search_provider"] == "serper"


def test_zotero_article_resolution_failure_blocks_instead_of_raising(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS": "LTA3U8I8"}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="summarize the Zotero article on an unknown Lindus SOOMA source",
            database_url=_database_url(tmp_path),
            save=True,
        )
    )

    assert result.advanced is False
    assert result.status == WorkItemStatus.BLOCKED
    assert result.blockers[0].code == "zotero_article_resolution_failed"
    assert "could not resolve" in result.human_summary


def test_advance_work_item_opportunity_scout_attaches_opportunity_artifacts(
    tmp_path: Path,
) -> None:
    result = advance_work_item(
        WorkflowRunRequest(
            request_text="find behavioral health AI companies",
            database_url=_database_url(tmp_path),
            save=True,
            max_results=2,
        )
    )

    assert result.advanced is True
    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert result.artifact_refs
    assert all(ref.artifact_type == "opportunity" for ref in result.artifact_refs)
    assert result.next_action is not None
    assert result.next_action.agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST


def test_advance_work_item_persists_manual_plan_and_uses_requested_route(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    manual_plan = {
        "source": "heuristic",
        "target_agent": "opportunity_scout",
        "intent": "opportunity_search",
        "primary_target": "digital mental health conference opportunities",
        "objective": "find 5 conference opportunities",
        "desired_count": 5,
        "constraints": ["conference"],
    }

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="find 5 conference opportunities",
            database_url=database_url,
            save=True,
            max_results=3,
            manual_request_plan=manual_plan,
        )
    )

    store = SQLiteStore(database_url)
    loaded = store.get_work_item(result.work_item.id)
    events = store.list_work_item_events(result.work_item.id)

    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert result.manual_request_plan == manual_plan
    assert result.artifact_refs
    assert loaded is not None
    assert loaded.target.metadata["manual_request_plan"]["desired_count"] == 5
    assert loaded.target.metadata["manual_desired_count"] == 5
    assert events[0].metadata["manual_request_plan"]["target_agent"] == "opportunity_scout"


def test_advance_work_item_outreach_blocks_without_approved_context(tmp_path: Path) -> None:
    result = advance_work_item(
        WorkflowRunRequest(
            request_text="draft outreach to Lindus Health",
            database_url=_database_url(tmp_path),
            save=True,
        )
    )

    assert result.advanced is False
    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.status == WorkItemStatus.BLOCKED
    assert result.blockers[0].code == "outreach_requires_approved_context"
    assert "Outreach Composer did not have the required context" in result.human_summary
    assert "selected source-backed company profile" in result.human_summary
    assert result.context_pack is not None
    assert result.context_pack["can_synthesize"] is False
    assert result.context_pack["missing_requirements"]


def test_outreach_blocks_from_research_until_context_approved(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    research = advance_work_item(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    )

    draft_attempt = advance_work_item(
        WorkflowRunRequest(
            request_text="draft outreach",
            work_item_id=research.work_item.id,
            database_url=database_url,
            save=True,
        )
    )

    assert draft_attempt.advanced is False
    assert draft_attempt.route == WorkItemRoute.OUTREACH_COMPOSER
    assert draft_attempt.blockers[0].code == "outreach_requires_approved_context"


def test_continue_uses_saved_next_action(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    first = advance_work_item(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    )

    continued = advance_work_item(
        WorkflowRunRequest(
            request_text="continue",
            work_item_id=first.work_item.id,
            database_url=database_url,
            save=True,
            max_results=1,
        )
    )

    assert continued.advanced is True
    assert continued.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert any(ref.artifact_type == "opportunity" for ref in continued.work_item.artifact_refs)


def test_approved_context_continue_creates_draft_only_outreach_artifact(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    research = advance_work_item(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    )
    company_ref = research.artifact_refs[0]
    item = approve_artifact_context(
        research.work_item,
        company_ref.artifact_type,
        company_ref.artifact_id,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )
    assert drafting_ready(item).ready is True
    item = set_next_action(
        item,
        WorkItemNextAction(
            action="draft_outreach",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
        ),
    )
    store = SQLiteStore(database_url)
    store.save_work_item(item)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="continue",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
        )
    )

    assert result.advanced is True
    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.status == WorkItemStatus.NEEDS_APPROVAL
    assert result.artifact_refs[0].artifact_type == "outreach_draft"
    assert store.count("outreach_drafts") == 1
    approvals = store.list_approval_items(object_type="outreach_draft")
    assert len(approvals) == 1
    assert "no external message was sent" in result.human_summary


def test_context_approval_resolves_prior_outreach_context_blocker(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    research = advance_work_item(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    )
    blocked = advance_work_item(
        WorkflowRunRequest(
            request_text="draft outreach",
            work_item_id=research.work_item.id,
            database_url=database_url,
            save=True,
        )
    )

    company_ref = blocked.work_item.artifact_refs[0]
    approved = approve_artifact_context(
        blocked.work_item,
        company_ref.artifact_type,
        company_ref.artifact_id,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )

    assert all(
        blocker.resolved
        for blocker in approved.blockers
        if blocker.code == "outreach_requires_approved_context"
    )
