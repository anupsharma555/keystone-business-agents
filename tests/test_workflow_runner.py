from __future__ import annotations

import json
from pathlib import Path

import pytest

import keystone_agents.response_synthesis as response_synthesis
import keystone_agents.workflow_runner as workflow_runner
from keystone_agents.agents.business_research_analyst import research_company_fixture
from keystone_agents.agents.opportunity_scout import scout_opportunities_fixture
from keystone_agents.agents.orchestrator import run_orchestrator_preflight
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.multi_target_research import (
    MultiTargetResearchPlan,
    MultiTargetResearchResult,
    PerTargetResearchPacket,
)
from keystone_agents.orchestrator.preflight_context import compact_orchestrator_preflight_payload
from keystone_agents.reporting import render_work_item_result_text
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
    ChiefOfStaffSourceRef,
)
from keystone_agents.schemas.company_profile import SourceRecord
from keystone_agents.schemas.memory import MemoryItem
from keystone_agents.schemas.opportunity import (
    FilteredOpportunityCandidate,
    OpportunityRecord,
    OpportunityScoutResult,
    OpportunitySource,
)
from keystone_agents.schemas.research import (
    ResearchArticleSummary,
    ResearchBrief,
    ResearchBriefFact,
    ResearchSourceCitation,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.test_pack_specs import get_test_pack_spec
from keystone_agents.tools.website_extraction_tool import WebsiteExtractionResult
from keystone_agents.work_items import (
    approve_artifact_context,
    build_context_pack_for_route,
    drafting_ready,
    normalize_target_text,
    select_artifact,
    selected_artifacts,
    set_next_action,
)
from keystone_agents.workflow_runner import advance_work_item, advance_work_item_manager_loop


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'workflow_runner.db'}"


def test_normalize_target_text_strips_named_agent_prefixes() -> None:
    assert (
        normalize_target_text(
            "opportunity scout find 3 active behavioral health AI partnership opportunities",
            WorkItemRoute.OPPORTUNITY_SCOUT,
        )
        == "3 active behavioral health AI partnership opportunities"
    )


def test_natural_source_backed_synthesis_requests_selected_page_context() -> None:
    assert workflow_runner._request_requires_selected_web_source_context(
        "chief of staff what is OpenAI doing about mental health right now? "
        "Please give a clear Answer, a useful Detailed Summary that summarizes the "
        "source data first, key source URLs, and compact Metadata."
    )
    assert workflow_runner._request_requires_selected_web_source_context(
        "opportunity scout compare active behavioral health AI opportunities "
        "with source evidence, a compact table, visible URLs, and a synthesis "
        "that summarizes what the sources say."
    )
    assert workflow_runner._request_requires_selected_web_source_context(
        "chief of staff can you do a deeper read-only search on one focused "
        "question? Please give a concise Answer and a Detailed Summary that "
        "synthesizes what the retrieved link content says across sources before "
        "listing links. If the links were only snippets, say so; otherwise "
        "read/extract and summarize the source content."
    )


def test_lightweight_source_list_does_not_force_selected_page_context() -> None:
    assert not workflow_runner._request_requires_selected_web_source_context(
        "chief of staff find a few source URLs about OpenAI mental health."
    )


def test_slack_runtime_request_auto_attaches_reusable_query_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_REPO", "/tmp/kba")
    request = WorkflowRunRequest(
        request_text=(
            "business research analyst: reusable source-read test. Compare how three "
            "public AI companion or chatbot products describe teen safety. Do not draft, "
            "send, publish, schedule, write files, or post elsewhere."
        ),
        requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        cost_profile="slack_research_deep",
    )
    work_item = WorkItem(
        id="wi_test",
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Reusable Slack test",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )

    updated = workflow_runner._attach_reusable_slack_query_prompt(
        request,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        work_item=work_item,
    )

    assert isinstance(updated.slack_query_prompt, dict)
    assert updated.slack_query_prompt["kind"] == "research_summary"
    assert updated.slack_query_prompt["target_route"] == "business_research_analyst"
    assert updated.external_context["slack_query_prompt"]["context_flags"]["needs_source_triage"]


def test_reusable_slack_query_prompt_task_brief_reaches_specialist_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_REPO", "/tmp/kba")
    request = WorkflowRunRequest(
        request_text=(
            "business research analyst: reusable source-read test. Compare how three "
            "public AI companion or chatbot products describe teen safety. Do not draft, "
            "send, publish, schedule, write files, or post elsewhere."
        ),
        requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        cost_profile="slack_research_deep",
    )
    work_item = WorkItem(
        id="wi_test",
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Reusable Slack test",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    updated = workflow_runner._attach_reusable_slack_query_prompt(
        request,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        work_item=work_item,
    )

    payload = workflow_runner._specialist_orchestrator_context_payload(updated, work_item)
    prompt = payload["reusable_slack_query_prompt"]

    assert prompt["kind"] == "research_summary"
    assert "task_brief" in prompt
    assert "Resolve three named products" in prompt["task_brief"]
    assert "does not grant tool access" in prompt["specialist_use"]
    assert prompt["context_flags"]["needs_source_triage"] is True


def test_business_research_category_comparison_dispatches_multi_target_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_retrieve_company_profile_live(**_kwargs: object):
        raise AssertionError("single-company retrieval should not run")

    def fake_multi_target_research(plan: MultiTargetResearchPlan, **_kwargs: object):
        calls.append(plan.topic)
        return MultiTargetResearchResult(
            plan=plan,
            selected_targets=["Replika", "Character.AI", "Nomi"],
            packets=[
                PerTargetResearchPacket(
                    target_name="Replika",
                    source_refs=[
                        {
                            "source_id": "replika:safety",
                            "title": "Replika safety",
                            "url": "https://replika.com/safety",
                            "source_type": "company_site",
                            "supported_claims": ["Replika describes teen safety."],
                        }
                    ],
                    extraction_status="extracted",
                    source_sufficient=True,
                ),
                PerTargetResearchPacket(
                    target_name="Character.AI",
                    source_refs=[
                        {
                            "source_id": "character:safety",
                            "title": "Character.AI safety",
                            "url": "https://character.ai/safety",
                            "source_type": "company_site",
                            "supported_claims": ["Character.AI describes teen safety."],
                        }
                    ],
                    extraction_status="extracted",
                    source_sufficient=True,
                ),
                PerTargetResearchPacket(
                    target_name="Nomi",
                    source_refs=[
                        {
                            "source_id": "nomi:safety",
                            "title": "Nomi safety",
                            "url": "https://nomi.ai/safety",
                            "source_type": "company_site",
                            "supported_claims": ["Nomi describes teen safety."],
                        }
                    ],
                    extraction_status="extracted",
                    source_sufficient=True,
                ),
            ],
            comparison_ready=True,
            diagnostics={"ready_packet_count": 3},
            pass_types=["candidate_discovery", "target_selection", "per_target_depth"],
        )

    monkeypatch.setattr(
        workflow_runner,
        "retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )
    monkeypatch.setattr(workflow_runner, "run_multi_target_research", fake_multi_target_research)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "business research analyst: Compare how three public AI companion products "
                "describe teen safety."
            ),
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            manual_request_plan={
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "public AI companion products",
                "target_type": "company",
                "desired_count": 3,
                "required_terms": ["teen safety"],
                "planner_warnings": [
                    "Target is a product category rather than a single named company."
                ],
            },
        )
    )

    assert calls == ["public AI companion products"]
    assert result.status == WorkItemStatus.DONE
    assert result.artifact_refs[0].artifact_type == "multi_target_research"
    assert "Detailed Summary" in result.human_summary
    assert result.human_summary.index("Detailed Summary") < result.human_summary.index(
        "Source-backed comparison table"
    )
    assert "The comparison is supported for Replika, Character.AI, Nomi" in result.human_summary
    assert "Synthesis:" not in result.human_summary
    assert "no clean source list" not in result.human_summary
    assert result.human_summary.index("Source-backed comparison table") < result.human_summary.index(
        "Metadata"
    )
    assert "Multi-target pass types" in result.human_summary


def test_requested_opportunity_comparison_gets_artifact_aligned_table() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="1",
        source_agent="opportunity_scout",
        title="Sagent Behavioral Health",
        summary="Scaled measurement-based care across behavioral health clinics.",
        metadata={
            "source_refs": [
                {
                    "title": "Sagent partners with Greenspace",
                    "url": "https://example.com/sagent",
                    "supported_claim": "Sagent scaled measurement-based care.",
                    "evidence_excerpt": (
                        "Sagent and Greenspace describe measurement-based care deployment "
                        "across behavioral health clinics."
                    ),
                    "key_facts": [
                        "The source names behavioral health clinic implementation as the setting."
                    ],
                }
            ],
            "retrieval_diagnostics": {"provider_summary": "searxng+agents-web-search+exa"},
        },
    )
    artifact_two = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="2",
        source_agent="opportunity_scout",
        title="Eleos",
        summary="AI workflow tools for behavioral health settings.",
        metadata={
            "source_refs": [
                {
                    "title": "Eleos raises Series C",
                    "url": "https://example.com/eleos",
                    "supported_claim": "Eleos expands AI tools in behavioral health.",
                    "evidence_excerpt": (
                        "Eleos says its AI workflow tools support documentation and care "
                        "operations in behavioral health settings."
                    ),
                }
            ]
        },
    )
    work_item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Opportunity scan",
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        artifact_refs=[artifact, artifact_two],
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact, artifact_two],
        human_summary="Brief synthesis without a table.",
        manual_request_plan={"desired_count": 2},
    )

    text = workflow_runner._ensure_requested_opportunity_comparison_table(
        "Brief synthesis without a table.",
        result=result,
        request_text="Compare these in a compact comparison table with source URLs.",
    )

    assert text.startswith("Behavioral health clinic software comparison")
    assert "Brief synthesis without a table." not in text
    assert "Answer\nThe strongest source-backed matches surfaced" in text
    assert "Detailed Summary\n" in text
    assert "measurement-based care deployment across behavioral health clinics" in text
    assert "behavioral health clinic implementation as the setting" in text
    assert "AI workflow tools support documentation" in text
    assert text.index("Detailed Summary") < text.index("Keystone relevance")
    assert "| Company | Relevant signal | Source |" in text
    assert "| Sagent Behavioral Health |" in text
    assert "[Source](https://example.com/sagent)" in text
    assert "[Source](https://example.com/eleos)" in text
    assert "Source evidence\n* Sagent Behavioral Health / Sagent partners with Greenspace" in text
    assert "Metadata\n* Search providers: searxng+agents-web-search+exa" in text
    assert text.rfind("Metadata") > text.rfind("Run notes")
    assert (
        normalize_target_text(
            "business research analyst research Big Health",
            WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        )
        == "Big Health"
    )


def test_opportunity_synthesis_failure_uses_source_backed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="224",
        source_agent="opportunity_scout",
        title="State behavioral health AI pilot RFP",
        summary="Pilot RFP for AI-enabled behavioral health implementation.",
        metadata={
            "source_refs": [
                {
                    "title": "Behavioral Health Clinical AI Tools RFP",
                    "url": "https://example.gov/behavioral-health-ai-rfp",
                    "supported_claim": (
                        "The RFP seeks vendors for behavioral health clinical AI tools."
                    ),
                    "evidence_excerpt": (
                        "The source describes an active behavioral health clinical AI "
                        "pilot procurement with implementation and evaluation requirements."
                    ),
                }
            ],
            "retrieval_diagnostics": {"provider_summary": "searxng+tavily+exa"},
        },
    )
    artifact_two = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="225",
        source_agent="opportunity_scout",
        title="Digital psychiatry grant",
        summary="Grant opportunity for digital psychiatry implementation research.",
        metadata={
            "source_refs": [
                {
                    "title": "Digital Psychiatry Funding Opportunity",
                    "url": "https://example.nih.gov/digital-psychiatry-grant",
                    "supported_claim": (
                        "The funding announcement supports digital psychiatry evaluation."
                    ),
                    "evidence_excerpt": (
                        "The source asks for measurement-based digital mental health "
                        "projects with partner implementation sites."
                    ),
                }
            ],
        },
    )
    work_item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Opportunity scan",
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact, artifact_two],
        human_summary="Opportunity Scout attached 2 source-backed opportunity record(s).",
    )

    def fail_synthesis(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic test failure")

    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        fail_synthesis,
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text=(
                "opportunity scout find pilot, RFP, or grant opportunities in "
                "AI-enabled behavioral health. Please include a compact comparison table."
            ),
            live_sdk=True,
        ),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary.startswith("Behavioral health opportunity comparison")
    assert "Answer\nThe strongest source-backed matches surfaced" in updated.human_summary
    assert "Detailed Summary\n" in updated.human_summary
    assert "| Opportunity | Relevant signal | Source |" in updated.human_summary
    assert "https://example.gov/behavioral-health-ai-rfp" in updated.human_summary
    assert "Source evidence\n* State behavioral health AI pilot RFP" in updated.human_summary
    assert "Metadata\n* Search providers: searxng+tavily+exa" in updated.human_summary
    assert "User-facing response synthesis failed: RuntimeError" in " ".join(updated.audit_notes)
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes


def test_opportunity_no_live_sdk_uses_source_backed_user_facing_fallback() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="224",
        source_agent="opportunity_scout",
        title="State behavioral health AI pilot RFP",
        summary="Pilot RFP for AI-enabled behavioral health implementation.",
        metadata={
            "source_refs": [
                {
                    "title": "Behavioral Health Clinical AI Tools RFP",
                    "url": "https://example.gov/behavioral-health-ai-rfp",
                    "supported_claim": (
                        "The RFP seeks vendors for behavioral health clinical AI tools."
                    ),
                    "evidence_excerpt": (
                        "The source describes an active behavioral health clinical AI "
                        "pilot procurement with implementation and evaluation requirements."
                    ),
                }
            ],
            "retrieval_diagnostics": {"provider_summary": "searxng+tavily+exa"},
        },
    )
    artifact_two = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="225",
        source_agent="opportunity_scout",
        title="Digital psychiatry grant",
        summary="Grant opportunity for digital psychiatry implementation research.",
        metadata={
            "source_refs": [
                {
                    "title": "Digital Psychiatry Funding Opportunity",
                    "url": "https://example.nih.gov/digital-psychiatry-grant",
                    "supported_claim": (
                        "The funding announcement supports digital psychiatry evaluation."
                    ),
                }
            ],
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.OPPORTUNITY,
            title="Opportunity scan",
            current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        ),
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact, artifact_two],
        human_summary="Opportunity Scout attached 2 source-backed opportunity record(s).",
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text=(
                "opportunity scout find active pilot, RFP, or grant opportunities "
                "around AI-enabled behavioral health."
            ),
            live_sdk=False,
        ),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary.startswith("Behavioral health opportunity comparison")
    assert "Answer\nThe strongest source-backed matches surfaced" in updated.human_summary
    assert "Detailed Summary\n" in updated.human_summary
    assert "| Opportunity | Relevant signal | Source |" in updated.human_summary
    assert "Source evidence\n* State behavioral health AI pilot RFP" in updated.human_summary
    assert "Opportunity Scout attached 2 source-backed opportunity record(s)." not in (
        updated.human_summary
    )
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes


def test_opportunity_live_sdk_artifact_only_output_uses_source_backed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="224",
        source_agent="opportunity_scout",
        title="State behavioral health AI pilot RFP",
        summary="Pilot RFP for AI-enabled behavioral health implementation.",
        metadata={
            "source_refs": [
                {
                    "title": "Behavioral Health Clinical AI Tools RFP",
                    "url": "https://example.gov/behavioral-health-ai-rfp",
                    "supported_claim": (
                        "The RFP seeks vendors for behavioral health clinical AI tools."
                    ),
                    "evidence_excerpt": (
                        "The source describes an active behavioral health clinical AI "
                        "pilot procurement with implementation and evaluation requirements."
                    ),
                }
            ],
            "retrieval_diagnostics": {"provider_summary": "searxng+tavily+exa"},
        },
    )
    artifact_two = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="225",
        source_agent="opportunity_scout",
        title="Digital psychiatry grant",
        summary="Grant opportunity for digital psychiatry implementation research.",
        metadata={
            "source_refs": [
                {
                    "title": "Digital Psychiatry Funding Opportunity",
                    "url": "https://example.nih.gov/digital-psychiatry-grant",
                    "supported_claim": (
                        "The funding announcement supports digital psychiatry evaluation."
                    ),
                }
            ],
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.OPPORTUNITY,
            title="Opportunity scan",
            current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        ),
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact, artifact_two],
        human_summary="Opportunity Scout attached 2 source-backed opportunity record(s).",
    )

    class ArtifactOnlySynthesis:
        title = "Business Agents WorkItem Advanced"
        answer = "Opportunity Scout attached 2 source-backed opportunity record(s)."
        synthesis = ""
        source_evidence = []
        terms = []
        recommended_actions = []
        key_points = []
        caveats = []
        next_step = "review_opportunities"

    class FakeSDKResult:
        output = ArtifactOnlySynthesis()
        usage = {}
        cost = {}
        request_cache = {}

    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        lambda *_args, **_kwargs: FakeSDKResult(),
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text=(
                "opportunity scout find active pilot, RFP, or grant opportunities "
                "around AI-enabled behavioral health."
            ),
            live_sdk=True,
        ),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary.startswith("Behavioral health opportunity comparison")
    assert "Answer\nThe strongest source-backed matches surfaced" in updated.human_summary
    assert "Detailed Summary\n" in updated.human_summary
    assert "Business Agents WorkItem Advanced" not in updated.human_summary
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes
    assert "Live user-facing response synthesis executed." not in updated.audit_notes


def test_opportunity_live_sdk_metadata_like_synthesis_uses_source_backed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="224",
        source_agent="opportunity_scout",
        title="State behavioral health AI pilot RFP",
        summary="Pilot RFP for AI-enabled behavioral health implementation.",
        metadata={
            "source_refs": [
                {
                    "title": "Behavioral Health Clinical AI Tools RFP",
                    "url": "https://example.gov/behavioral-health-ai-rfp",
                    "supported_claim": (
                        "The RFP seeks vendors for behavioral health clinical AI tools."
                    ),
                    "evidence_excerpt": (
                        "The source describes an active behavioral health clinical AI "
                        "pilot procurement with implementation and evaluation requirements."
                    ),
                }
            ],
            "retrieval_diagnostics": {"provider_summary": "searxng+tavily+exa"},
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.OPPORTUNITY,
            title="Opportunity scan",
            current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        ),
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Opportunity Scout attached 1 source-backed opportunity record(s).",
    )

    class MetadataLikeSynthesis:
        title = "Ranked behavioral health AI opportunity signals"
        answer = "Source-backed shortlist from the current read-only run."
        synthesis = (
            "The ranked items are limited to retained artifacts whose title, "
            "summary, or attached source refs match the prompt's signal shape. "
            "This keeps the Slack answer focused on requested opportunity evidence "
            "instead of using generic source-backed artifacts as filler."
        )
        source_evidence = []
        terms = []
        recommended_actions = []
        key_points = []
        caveats = []
        next_step = "review_opportunities"

    class FakeSDKResult:
        output = MetadataLikeSynthesis()
        usage = {}
        cost = {}
        request_cache = {}

    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        lambda *_args, **_kwargs: FakeSDKResult(),
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text=(
                "opportunity scout find active pilot, RFP, or grant opportunities "
                "around AI-enabled behavioral health. Include a useful synthesis."
            ),
            live_sdk=True,
        ),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary.startswith("Behavioral health opportunity comparison")
    assert "Detailed Summary\n" in updated.human_summary
    assert "active behavioral health clinical AI pilot procurement" in updated.human_summary
    assert "retained artifacts whose title" not in updated.human_summary
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes
    assert "Live user-facing response synthesis executed." not in updated.audit_notes


def test_business_research_no_live_sdk_uses_source_backed_user_facing_fallback() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id="openai",
        source_agent="business_research_analyst",
        title="OpenAI",
        summary="AI research and deployment company.",
        metadata={
            "source_refs": [
                {
                    "title": "OpenAI mental health work update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "supported_claim": (
                        "OpenAI describes mental-health-related safety work for ChatGPT."
                    ),
                    "evidence_excerpt": (
                        "OpenAI says it is improving sensitive-conversation handling "
                        "and consulting external experts."
                    ),
                    "extraction_status": "article_read",
                },
                {
                    "title": "OpenAI Trusted Contact",
                    "url": "https://openai.com/index/introducing-trusted-contact-in-chatgpt/",
                    "supported_claim": (
                        "OpenAI introduced Trusted Contact for adult ChatGPT users."
                    ),
                    "extraction_status": "snippet_only",
                },
            ],
            "retrieval_diagnostics": {"provider_summary": "searxng+exa+agents-web-search"},
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="OpenAI research",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary=(
            "Business Research Analyst attached a source-backed company profile for OpenAI."
        ),
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text=(
                "business research analyst what is OpenAI doing about mental health "
                "and what is relevant for Keystone?"
            ),
            live_sdk=False,
        ),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary.startswith("OpenAI source-backed brief")
    assert "Answer\nOpenAI has source-backed company context" in updated.human_summary
    assert "Detailed Summary\n" in updated.human_summary
    assert "improving sensitive-conversation handling" in updated.human_summary
    assert "Source evidence\n* OpenAI mental health work update" in updated.human_summary
    assert "Metadata\n* Search providers: searxng+exa+agents-web-search" in (updated.human_summary)
    assert "Business Research Analyst attached" not in updated.human_summary
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes


def test_business_research_live_sdk_artifact_only_output_uses_source_backed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id="openai",
        source_agent="business_research_analyst",
        title="OpenAI",
        summary="AI research and deployment company.",
        metadata={
            "source_refs": [
                {
                    "title": "OpenAI mental health work update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "supported_claim": (
                        "OpenAI describes mental-health-related safety work for ChatGPT."
                    ),
                    "evidence_excerpt": (
                        "OpenAI says it is improving sensitive-conversation handling "
                        "and consulting external experts."
                    ),
                    "extraction_status": "article_read",
                },
            ],
            "retrieval_diagnostics": {"provider_summary": "searxng+exa"},
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="OpenAI research",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary=(
            "Business Research Analyst attached a source-backed company profile for OpenAI."
        ),
    )

    class ArtifactOnlySynthesis:
        title = "Business Agents WorkItem Advanced"
        answer = "Business Research Analyst attached a source-backed company profile for OpenAI."
        synthesis = ""
        source_evidence = []
        terms = []
        recommended_actions = []
        key_points = []
        caveats = []
        next_step = "review_company_profile"

    class FakeSDKResult:
        output = ArtifactOnlySynthesis()
        usage = {}
        cost = {}
        request_cache = {}

    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        lambda *_args, **_kwargs: FakeSDKResult(),
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text="business research analyst summarize OpenAI mental health work",
            live_sdk=True,
        ),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary.startswith("OpenAI source-backed brief")
    assert "Detailed Summary\n" in updated.human_summary
    assert "Business Agents WorkItem Advanced" not in updated.human_summary
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes
    assert "Live user-facing response synthesis executed." not in updated.audit_notes


def test_business_research_raw_result_is_source_backed_before_final_synthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_retrieve_company_profile_live(*, company: str, **_: object):
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    SourceRecord(
                        source_id="company:mental-health-update",
                        title="OpenAI mental health work update",
                        url="https://openai.com/index/update-on-mental-health-related-work/",
                        source_type="company_site",
                        supported_claims=[
                            "OpenAI describes mental-health-related safety work for ChatGPT."
                        ],
                        confidence=0.9,
                    )
                ]
            }
        )
        return profile, {
            "debug_notes": ["fake current retrieval"],
            "retrieval_diagnostics": {"provider_summary": "searxng+exa"},
        }

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )

    result = workflow_runner._advance_work_item_one_step(
        WorkflowRunRequest(
            request_text=(
                "business research analyst what is OpenAI doing about mental health "
                "right now? Give a source-backed synthesis."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            manual_request_plan={
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "OpenAI",
            },
        ),
        synthesize_user_response=False,
    )

    assert result.human_summary.startswith("OpenAI source-backed brief")
    assert "Answer\nOpenAI has source-backed company context" in result.human_summary
    assert "Detailed Summary\n" in result.human_summary
    assert "https://openai.com/index/update-on-mental-health-related-work/" in result.human_summary
    assert "Business Research Analyst attached" not in result.human_summary
    assert "Deterministic business research source-backed summary rendered." in result.audit_notes
    source_ref = result.artifact_refs[0].metadata["source_refs"][0]
    assert source_ref["supported_claim"] == (
        "OpenAI describes mental-health-related safety work for ChatGPT."
    )
    assert source_ref["extraction_status"] == "snippet_only"
    assert source_ref["key_facts"] == [
        "OpenAI describes mental-health-related safety work for ChatGPT."
    ]
    assert result.artifact_refs[0].metadata["source_context_status"] == {
        "selected_url_count": 1,
        "extracted_url_count": 0,
        "evidence_url_count": 1,
        "snippet_only_url_count": 1,
        "statuses": ["snippet_only"],
    }


def test_chief_no_live_sdk_uses_source_backed_user_facing_fallback() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="chief_of_staff_plan",
        artifact_id="chief-openai",
        source_agent="chief_of_staff",
        title="Chief of Staff plan",
        summary="Search completed.",
        metadata={
            "source_refs": [
                {
                    "title": "OpenAI mental health update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "supported_claim": (
                        "OpenAI describes mental-health-related safety work for ChatGPT."
                    ),
                    "evidence_excerpt": (
                        "OpenAI says it is improving emotionally sensitive conversation "
                        "handling, adding Trusted Contact workflows, and consulting clinicians."
                    ),
                    "extraction_status": "success",
                },
                {
                    "title": "OpenAI Trusted Contact",
                    "url": "https://openai.com/index/introducing-trusted-contact-in-chatgpt/",
                    "supported_claim": (
                        "OpenAI introduced Trusted Contact for adult ChatGPT users."
                    ),
                    "extraction_status": "source_linked",
                },
            ],
            "retrieval_diagnostics": {"provider_summary": "searxng+agents-web-search+exa"},
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Search completed.",
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text=(
                "chief of staff what is OpenAI doing about mental health, "
                "and what is relevant for Keystone?"
            ),
            live_sdk=False,
        ),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary.startswith("Chief of Staff source-backed brief")
    assert "Answer\nThe run found source-backed context" in updated.human_summary
    assert "Detailed Summary\n" in updated.human_summary
    assert "emotionally sensitive conversation handling" in updated.human_summary
    assert "Source evidence\n* OpenAI mental health update" in updated.human_summary
    assert "Metadata\n* Search providers: searxng+agents-web-search+exa" in (updated.human_summary)
    assert updated.human_summary.count("Search completed.") == 0
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes


def test_chief_live_sdk_plan_only_output_uses_source_backed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="chief_of_staff_plan",
        artifact_id="chief-openai",
        source_agent="chief_of_staff",
        title="Chief of Staff plan",
        summary="Chief of Staff plan.",
        metadata={
            "source_refs": [
                {
                    "title": "OpenAI mental health update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "supported_claim": (
                        "OpenAI describes mental-health-related safety work for ChatGPT."
                    ),
                    "evidence_excerpt": (
                        "OpenAI says it is improving emotionally sensitive conversation "
                        "handling and adding Trusted Contact workflows."
                    ),
                    "extraction_status": "success",
                }
            ],
            "retrieval_diagnostics": {
                "provider_result_samples": {
                    "exa": [
                        {
                            "title": "OpenAI mental health update",
                            "url": "https://openai.com/index/update-on-mental-health-related-work/",
                            "snippet": "OpenAI describes mental-health-related safety work.",
                        }
                    ]
                }
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Chief of Staff plan.",
    )

    class PlanOnlySynthesis:
        title = "Business Agents Chief of Staff"
        answer = "Chief of Staff plan."
        synthesis = ""
        source_evidence = []
        terms = []
        recommended_actions = []
        key_points = []
        caveats = []
        next_step = "review_chief_of_staff_plan"

    class FakeSDKResult:
        output = PlanOnlySynthesis()
        usage = {}
        cost = {}
        request_cache = {}

    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        lambda *_args, **_kwargs: FakeSDKResult(),
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text="chief of staff summarize OpenAI mental health work",
            live_sdk=True,
        ),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary.startswith("Chief of Staff source-backed brief")
    assert "Detailed Summary\n" in updated.human_summary
    assert "emotionally sensitive conversation handling" in updated.human_summary
    assert "Business Agents Chief of Staff" not in updated.human_summary
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes
    assert "Live user-facing response synthesis executed." not in updated.audit_notes


def test_chief_live_sdk_thin_synthesis_uses_source_backed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="chief_of_staff_plan",
        artifact_id="chief-ambient-scribes",
        source_agent="chief_of_staff",
        title="Chief of Staff plan",
        summary=(
            "There are public signals that ambient documentation is being evaluated "
            "in behavioral health."
        ),
        metadata={
            "source_refs": [
                {
                    "title": "American Psychiatric Association AI Scribe Tools",
                    "url": "https://www.psychiatry.org/psychiatrists/practice/artificial-intelligence/ai-scribe-tools",
                    "supported_claim": (
                        "APA provides psychiatrist-facing guidance on AI scribe tools."
                    ),
                    "evidence_excerpt": (
                        "The guidance discusses documentation assistance, consent, "
                        "privacy, and clinical responsibility for AI-generated notes."
                    ),
                    "key_facts": [
                        "Psychiatry practices are being advised to evaluate privacy and consent before adopting AI scribes.",
                        "Clinicians remain responsible for reviewing and correcting generated notes.",
                    ],
                    "extraction_status": "article_read",
                },
                {
                    "title": "Becker's Behavioral Health on Cleveland Clinic AI scribes",
                    "url": "https://www.beckersbehavioralhealth.com/ai-2/liberating-cleveland-clinics-experience-with-ai-scribes-in-behavioral-health/",
                    "supported_claim": (
                        "Becker's reports Cleveland Clinic experience with AI scribes in behavioral health."
                    ),
                    "key_facts": [
                        "The article frames AI scribes as reducing documentation burden in behavioral health encounters.",
                        "The signal is implementation-oriented rather than a formal RFP or grant opportunity.",
                    ],
                    "extraction_status": "snippet_only",
                },
            ],
            "retrieval_diagnostics": {
                "provider_summary": "searxng+agents-web-search+exa",
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Behavioral health AI scribes",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Chief of Staff plan.",
    )

    class ThinSynthesis:
        title = "Behavioral health ambient documentation"
        answer = "Yes, there are signals."
        synthesis = "The source set indicates ambient scribes are being evaluated."
        source_evidence = []
        terms = []
        recommended_actions = []
        key_points = []
        caveats = []
        next_step = ""

    class FakeSDKResult:
        output = ThinSynthesis()
        usage = {}
        cost = {}
        request_cache = {}

    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        lambda *_args, **_kwargs: FakeSDKResult(),
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text=(
                "chief of staff do a deeper search on AI scribes or ambient "
                "documentation tools in behavioral health clinics. Give Answer "
                "and Detailed Summary that summarizes the source data first."
            ),
            live_sdk=True,
        ),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary.startswith("Chief of Staff source-backed brief")
    assert "Psychiatry practices are being advised to evaluate privacy and consent" in (
        updated.human_summary
    )
    assert "Clinicians remain responsible for reviewing and correcting generated notes" in (
        updated.human_summary
    )
    assert "reducing documentation burden in behavioral health encounters" in (
        updated.human_summary
    )
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes
    assert "Live user-facing response synthesis executed." not in updated.audit_notes


def test_chief_fallback_uses_source_triage_retained_sources_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="chief_of_staff_plan",
        artifact_id="chief-safety",
        source_agent="chief_of_staff",
        title="Chief of Staff plan",
        summary="Search completed.",
        metadata={
            "source_refs": [
                {
                    "source_id": "selected:1",
                    "title": "Retained safety source",
                    "url": "https://example.com/retained-safety",
                    "supported_claim": "The source describes escalation workflows.",
                    "evidence_excerpt": (
                        "The retained source describes trusted-contact escalation "
                        "and youth safety controls."
                    ),
                    "extraction_status": "success",
                },
                {
                    "title": "Rejected infrastructure source",
                    "url": "https://example.com/rejected-cloud",
                    "supported_claim": "The source describes cloud infrastructure.",
                    "evidence_excerpt": (
                        "The rejected source discusses enterprise cloud infrastructure "
                        "rather than mental health safety."
                    ),
                    "extraction_status": "success",
                },
            ],
            "retrieval_diagnostics": {
                "provider_summary": "searxng+exa",
                "source_triage": {
                    "recommended_action": "synthesize_from_retained_sources",
                    "retained_source_ids": ["selected:1"],
                    "rejected_urls": ["https://example.com/rejected-cloud"],
                    "decision_counts": {"retain": 1, "reject": 1},
                },
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Mental health AI safety",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Chief of Staff plan.",
    )

    class ThinSynthesis:
        title = "Business Agents WorkItem Advanced"
        answer = "Search completed."
        synthesis = ""
        source_evidence = []
        terms = []
        recommended_actions = []
        key_points = []
        caveats = []
        next_step = ""

    class FakeSDKResult:
        output = ThinSynthesis()
        usage = {}
        cost = {}
        request_cache = {}

    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        lambda *_args, **_kwargs: FakeSDKResult(),
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(
            request_text=(
                "chief of staff do a deeper source-backed search on mental health "
                "AI safety. Give Answer and Detailed Summary."
            ),
            live_sdk=True,
        ),
        sdk_session=None,
        store=None,
    )

    assert "trusted-contact escalation and youth safety controls" in updated.human_summary
    assert "https://example.com/retained-safety" in updated.human_summary
    assert "cloud infrastructure" not in updated.human_summary
    assert "https://example.com/rejected-cloud" not in updated.human_summary
    assert "Deterministic user-facing response fallback executed." in updated.audit_notes


def test_chief_raw_result_is_source_backed_before_final_synthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_plan_chief_of_staff_request(*_args: object, **_kwargs: object) -> ChiefOfStaffResult:
        return ChiefOfStaffResult(
            intent="research_brief",
            summary="Search completed.",
            sources=[
                ChiefOfStaffSourceRef(
                    title="OpenAI mental health update",
                    url="https://openai.com/index/update-on-mental-health-related-work/",
                    source_type="company_site",
                    note=(
                        "OpenAI describes mental-health-related safety work for ChatGPT, "
                        "including sensitive-conversation handling."
                    ),
                )
            ],
            retrieval_diagnostics={"provider_summary": "searxng+agents-web-search+exa"},
            audit_notes=["fake chief planner"],
        )

    monkeypatch.setattr(
        workflow_runner,
        "plan_chief_of_staff_request",
        fake_plan_chief_of_staff_request,
    )

    result = workflow_runner._advance_work_item_one_step(
        WorkflowRunRequest(
            request_text=(
                "chief of staff what is OpenAI doing about mental health right now? "
                "Give a source-backed synthesis."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            requested_route=WorkItemRoute.CHIEF_OF_STAFF,
        ),
        synthesize_user_response=False,
    )

    assert result.human_summary.startswith("Chief of Staff source-backed brief")
    assert "Answer\nThe run found source-backed context" in result.human_summary
    assert "Detailed Summary\n" in result.human_summary
    assert "sensitive-conversation handling" in result.human_summary
    assert "https://openai.com/index/update-on-mental-health-related-work/" in result.human_summary
    assert result.human_summary.count("Search completed.") == 0
    assert "Deterministic Chief of Staff source-backed summary rendered." in result.audit_notes


def test_slack_history_context_promotes_visible_links_as_ordered_sources() -> None:
    work_item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Thread follow-up",
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
    )

    updated = workflow_runner._apply_slack_history_context(
        work_item,
        {
            "schema": "keystone.slack.history_context.v1",
            "channel_id": "C123",
            "thread_ts": "1781026340.935439",
            "read_context": (
                "Source evidence:\n"
                "- APA advisory: <https://www.apa.org/topics/artificial-intelligence-machine-learning/health-advisory-chatbots-wellness-apps>\n"
                "- RAND youth usage press release: <https://www.rand.org/news/press/2025/11/one-in-eight-adolescents-and-young-adults-use-ai-chatbots.html>\n"
            ),
        },
        context_file_path="/tmp/slack-context.json",
    )

    link_sources = [
        source for source in updated.sources if source.source_type == "slack_thread_link"
    ]

    assert [source.title for source in link_sources] == [
        "APA advisory",
        "RAND youth usage press release",
    ]
    assert link_sources[0].url == (
        "https://www.apa.org/topics/artificial-intelligence-machine-learning/"
        "health-advisory-chatbots-wellness-apps"
    )
    assert link_sources[0].source_id.endswith(":1")
    assert "Link 1 appeared" in link_sources[0].supported_claim


def test_chief_link_followup_summarizes_ordered_slack_source_without_new_search(
    monkeypatch,
) -> None:
    work_item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Thread follow-up",
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
    )
    work_item = workflow_runner._apply_slack_history_context(
        work_item,
        {
            "schema": "keystone.slack.history_context.v1",
            "channel_id": "C123",
            "thread_ts": "1781027229.914959",
            "read_context": (
                "Source evidence\n"
                "* APA advisory: Health advisory: Use of generative AI chatbots and wellness applications for mental health - "
                "<https://www.apa.org/topics/artificial-intelligence-machine-learning/health-advisory-chatbots-wellness-apps> - "
                "Primary APA advisory page; supports the core warning about evidence, oversight, and safety.\n"
            ),
        },
        context_file_path="/tmp/slack-context.json",
    )

    def fake_read_linked_article_impl(*_args, **_kwargs):
        return {
            "status": "success",
            "title": "APA advisory",
            "provider": "trafilatura",
            "text_or_markdown": (
                "APA says generative AI chatbots and wellness applications are being used "
                "for mental health needs faster than evidence and safeguards can support. "
                "The advisory warns clinicians and consumers to evaluate privacy and safety."
            ),
        }

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.read_linked_article_impl",
        fake_read_linked_article_impl,
    )

    result = workflow_runner._advance_chief_of_staff(
        work_item,
        request=WorkflowRunRequest(
            request_text="chief of staff Follow-up: can you summarize link 1 from above?",
            live_search=True,
            live_sdk=False,
        ),
        store=None,
    )

    assert result.status == WorkItemStatus.DONE
    assert "Link 1 summary" in result.human_summary
    assert "APA says generative AI chatbots" in result.human_summary
    assert "Search providers: not used for this narrow source follow-up" in result.human_summary
    assert "Deterministic source-link follow-up summary executed." in result.audit_notes
    source_ref = result.artifact_refs[0].metadata["source_refs"][0]
    assert source_ref["url"].startswith("https://www.apa.org/")
    assert source_ref["extraction_status"] == "success"


def test_link_followup_with_pending_approval_gate_completes_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    work_item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Thread follow-up",
        request_text="chief of staff source-backed research",
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.NEEDS_APPROVAL,
        target=WorkItemTarget(name="thread", object_type="slack_thread"),
        approval_gates=[
            WorkItemApprovalGate(
                scope="external_use",
                state="pending",
                required=True,
                rationale="Prior draft still needs external-use approval.",
                approval_id="approval-1",
            )
        ],
        sources=[
            WorkItemSourceRef(
                title="APA advisory",
                url=(
                    "https://www.apa.org/topics/artificial-intelligence-machine-learning/"
                    "health-advisory-chatbots-wellness-apps"
                ),
                source_type="slack_thread_link",
                supported_claim="Link 1 appeared in prior Slack thread context.",
                evidence_excerpt="APA advisory source from the prior Slack brief.",
            )
        ],
    )
    store.save_work_item(work_item)

    monkeypatch.setattr(
        workflow_runner,
        "read_linked_article_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "title": "APA advisory",
            "provider": "trafilatura",
            "text_or_markdown": (
                "APA cautions that AI chatbots and wellness apps used for mental health "
                "need evidence review, privacy safeguards, and clinician oversight."
            ),
        },
    )

    result = workflow_runner.advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="can u summarize link 1",
            work_item_id=work_item.id,
            save=True,
            database_url=database_url,
            live_search=True,
            live_sdk=False,
        )
    )

    assert result.status == WorkItemStatus.DONE
    assert result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert "Link 1 summary" in result.human_summary
    assert "AI chatbots and wellness apps" in result.human_summary
    assert "Key source details:" in result.human_summary
    assert "privacy safeguards" in result.human_summary
    assert "Use in this thread:" in result.human_summary
    assert "Search providers: not used for this narrow source follow-up" in result.human_summary
    assert "Deterministic source-link follow-up summary executed." in result.audit_notes
    assert result.work_item.approval_gates[0].state == "pending"


def test_slack_continue_link_followup_with_pending_approval_gate_completes_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    work_item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Slack thread source follow-up",
        request_text="chief of staff source-backed research",
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.NEEDS_APPROVAL,
        target=WorkItemTarget(name="thread", object_type="slack_thread"),
        approval_gates=[
            WorkItemApprovalGate(
                scope="external_use",
                state="pending",
                required=True,
                rationale="Prior draft still needs external-use approval.",
                approval_id="approval-1",
            )
        ],
        sources=[
            WorkItemSourceRef(
                title="Trusted contact article",
                url="https://example.com/trusted-contact",
                source_type="slack_thread_link",
                supported_claim="Link 1 appeared in prior Slack thread context.",
                evidence_excerpt="The article describes trusted-contact escalation.",
            )
        ],
    )
    store.save_work_item(work_item)

    monkeypatch.setattr(
        workflow_runner,
        "read_linked_article_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "title": "Trusted contact article",
            "provider": "trafilatura",
            "text_or_markdown": (
                "The article explains trusted-contact escalation, high-risk signals, "
                "and support notifications for safety workflows."
            ),
        },
    )

    result = workflow_runner.advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                "chief of staff continue this prior Slack thread. can u summarize link 1"
            ),
            work_item_id=work_item.id,
            save=True,
            database_url=database_url,
            live_search=True,
            live_sdk=False,
        )
    )

    assert result.status == WorkItemStatus.DONE
    assert result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert "Link 1 summary" in result.human_summary
    assert "trusted-contact escalation" in result.human_summary
    assert "support notifications for safety workflows" in result.human_summary
    assert "Pending approval gate must be resolved" not in result.human_summary
    assert "Deterministic source-link follow-up summary executed." in result.audit_notes
    assert result.work_item.approval_gates[0].state == "pending"


def test_source_link_followup_summary_uses_multiple_extracted_facts(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Ambient scribe thread",
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
        sources=[
            WorkItemSourceRef(
                title="Psychiatric ambient scribe evaluation",
                url="https://example.org/psychiatry-ambient-scribe",
                source_type="literature",
                supported_claim="The study evaluates documentation quality in psychiatric consultations.",
                evidence_excerpt=(
                    "The study evaluates an ambient artificial intelligence scribe in "
                    "psychiatric consultations. It focuses on documentation quality, "
                    "clinician efficiency, and whether drafted notes remain suitable "
                    "for clinician review. The evidence is simulation-based, so it is "
                    "a workflow signal rather than a real-world implementation result."
                ),
                extraction_status="article_read",
                key_facts=[
                    "Psychiatry-specific evaluation signal for ambient documentation tools.",
                    "The source focuses on documentation quality and clinician efficiency.",
                    "The study design is simulation-based, limiting implementation claims.",
                ],
            )
        ],
    )
    SQLiteStore(database_url).save_work_item(item)

    result = workflow_runner.advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="chief of staff continue this prior Slack thread. summarize URL 1",
            work_item_id=item.id,
            save=True,
            database_url=database_url,
            live_search=False,
            live_sdk=False,
        )
    )

    assert result.status == WorkItemStatus.DONE
    assert "Psychiatry-specific evaluation signal" in result.human_summary
    assert "documentation quality and clinician efficiency" in result.human_summary
    assert "simulation-based" in result.human_summary
    assert "Key source details:" in result.human_summary
    assert "Search providers: not used for this narrow source follow-up" in result.human_summary


def test_chief_link_followup_live_sdk_uses_read_only_source_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Thread follow-up",
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
        sources=[
            WorkItemSourceRef(
                title="APA advisory",
                url=(
                    "https://www.apa.org/topics/artificial-intelligence-machine-learning/"
                    "health-advisory-chatbots-wellness-apps"
                ),
                source_type="slack_thread_link",
                supported_claim=(
                    "APA advisory source from the prior Slack brief about mental health "
                    "chatbots and wellness applications."
                ),
                evidence_excerpt=(
                    "APA warns that generative AI chatbots and wellness applications "
                    "are being used faster than evidence and safeguards can support."
                ),
                extraction_status="article_read",
            )
        ],
    )

    def fail_run_chief_of_staff_sdk(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("narrow source-link follow-up should not call Chief SDK")

    def fake_read_linked_article_impl(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "success",
            "title": "APA advisory",
            "provider": "trafilatura",
            "text_or_markdown": (
                "APA warns that generative AI chatbots and wellness applications "
                "are being used faster than evidence and safeguards can support. "
                "The advisory emphasizes privacy, safety, and clinician oversight."
            ),
        }

    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", fail_run_chief_of_staff_sdk)
    monkeypatch.setattr(
        workflow_runner,
        "read_linked_article_impl",
        fake_read_linked_article_impl,
    )

    result = workflow_runner._advance_chief_of_staff(
        work_item,
        request=WorkflowRunRequest(
            request_text="chief of staff Follow-up: can you summarize link 1 from above?",
            live_search=True,
            live_sdk=True,
        ),
        store=None,
    )

    assert result.status == WorkItemStatus.DONE
    assert "Link 1 summary" in result.human_summary
    assert "generative AI chatbots" in result.human_summary
    assert "Search providers: not used for this narrow source follow-up" in result.human_summary
    assert "Deterministic source-link follow-up summary executed." in result.audit_notes
    assert result.artifact_refs[0].metadata["source_context_status"]["extracted_url_count"] == 1


def test_source_link_followup_works_for_business_research_route(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="OpenAI mental health brief",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.NEEDS_APPROVAL,
        sources=[
            WorkItemSourceRef(
                title="OpenAI mental health update",
                url="https://openai.com/index/update-on-mental-health-related-work/",
                source_type="company_site",
                supported_claim=("OpenAI describes mental-health-related safety work in ChatGPT."),
                evidence_excerpt=(
                    "OpenAI says it is improving responses in emotionally sensitive "
                    "conversations and working with clinicians and researchers."
                ),
                extraction_status="extracted",
            )
        ],
    )
    SQLiteStore(database_url).save_work_item(item)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="business research analyst Follow-up: summarize the first link from above",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            live_sdk=False,
        )
    )

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.status == WorkItemStatus.DONE
    assert result.work_item.last_agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert result.artifact_refs[0].artifact_type == "source_link_summary"
    assert result.artifact_refs[0].source_agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert "emotionally sensitive conversations" in result.human_summary
    assert result.next_action is not None
    assert result.next_action.agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert "Deterministic source-link follow-up summary executed." in result.audit_notes


def test_source_link_followup_uses_business_research_artifact_source_refs(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="OpenAI mental health brief",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.NEEDS_APPROVAL,
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="company_profile",
                artifact_id="openai",
                source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                title="OpenAI",
                summary="OpenAI source-backed profile.",
                metadata={
                    "source_refs": [
                        {
                            "title": "OpenAI mental health update",
                            "url": (
                                "https://openai.com/index/update-on-mental-health-related-work/"
                            ),
                            "source_type": "company_site",
                            "supported_claim": (
                                "OpenAI describes mental-health-related safety work for ChatGPT."
                            ),
                            "evidence_excerpt": (
                                "OpenAI says it is improving emotionally sensitive "
                                "conversation handling."
                            ),
                            "extraction_status": "article_read",
                        }
                    ]
                },
            )
        ],
    )
    SQLiteStore(database_url).save_work_item(item)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="business research analyst summarize source 1 from above",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            live_sdk=True,
        )
    )

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.status == WorkItemStatus.DONE
    assert result.artifact_refs[0].artifact_type == "source_link_summary"
    assert result.artifact_refs[0].source_agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert "Link 1 summary" in result.human_summary
    assert "emotionally sensitive conversation handling" in result.human_summary
    assert "Search providers: not used for this narrow source follow-up" in result.human_summary
    assert "Deterministic source-link follow-up summary executed." in result.audit_notes


def test_source_link_followup_uses_opportunity_artifact_source_refs(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Behavioral health opportunity scan",
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.NEEDS_APPROVAL,
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="opportunity",
                artifact_id="224",
                source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                title="Behavioral Health Clinical AI Tools RFP",
                summary="RFP for behavioral-health clinical AI tools.",
                metadata={
                    "source_refs": [
                        {
                            "title": "Behavioral Health Clinical AI Tools RFP",
                            "url": "https://example.gov/behavioral-health-ai-rfp",
                            "source_type": "government",
                            "supported_signal": (
                                "The RFP seeks vendors for behavioral health clinical AI tools."
                            ),
                            "evidence_excerpt": (
                                "The source describes a pilot procurement with "
                                "implementation and evaluation requirements."
                            ),
                            "extraction_status": "article_read",
                        }
                    ]
                },
            )
        ],
    )
    SQLiteStore(database_url).save_work_item(item)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="opportunity scout can you summarize link 1",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
            live_sdk=True,
        )
    )

    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert result.status == WorkItemStatus.DONE
    assert result.artifact_refs[0].artifact_type == "source_link_summary"
    assert result.artifact_refs[0].source_agent == WorkItemRoute.OPPORTUNITY_SCOUT.value
    assert "Link 1 summary" in result.human_summary
    assert "pilot procurement" in result.human_summary
    assert "Search providers: not used for this narrow source follow-up" in result.human_summary
    assert "Deterministic source-link follow-up summary executed." in result.audit_notes


def test_business_research_current_query_focus_keeps_domain_terms() -> None:
    request = WorkflowRunRequest(
        request_text=(
            "business research analyst Can you give me a source-backed brief on what "
            "OpenAI is doing around mental health right now, and what seems relevant "
            "for Keystone? Please do a deeper read-only search."
        )
    )

    builder = workflow_runner._business_research_query_builder_for_request(request)
    assert builder is not None

    queries = builder("OpenAI")

    assert queries[0] == "OpenAI 2026 mental health"
    assert "OpenAI mental health independent coverage 2026" in queries[:3]
    assert not any("give source backed brief" in query for query in queries[:3])


def test_chief_link_followup_skips_generic_user_response_synthesis(monkeypatch) -> None:
    work_item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Thread follow-up",
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Link 1 summary",
        audit_notes=["Deterministic source-link follow-up summary executed."],
    )

    def fail_synthesis(*_args, **_kwargs):
        raise AssertionError("generic synthesis should not run")

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.synthesize_user_facing_work_item_response_sdk_result",
        fail_synthesis,
    )

    updated = workflow_runner._maybe_synthesize_user_facing_response(
        result,
        request=WorkflowRunRequest(request_text="summarize link 1", live_sdk=True),
        sdk_session=None,
        store=None,
    )

    assert updated.human_summary == "Link 1 summary"
    assert "Skipped generic user-facing response synthesis" in " ".join(updated.audit_notes)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("can you summarize link 1", 1),
        ("please summarize link #2", 2),
        ("can you summarize the first link", 1),
        ("review source two from above", 2),
        ("summarize URL 1", 1),
        ("read the 3rd source", 3),
    ],
)
def test_source_link_followup_index_accepts_natural_ordinals(
    text: str,
    expected: int,
) -> None:
    assert workflow_runner._source_link_followup_index(text) == expected


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
    assert result.context_pack["source_context_status"]["selected_url_count"] >= 1
    assert result.context_pack["source_context_status"]["evidence_url_count"] >= 1
    assert loaded is not None
    assert loaded.artifact_refs[0].artifact_id == result.artifact_refs[0].artifact_id
    assert store.list_work_item_artifacts(result.work_item.id)[0].artifact_type == "company_profile"
    events = store.list_work_item_events(result.work_item.id)
    event_types = [event.event_type for event in events]
    skills_event = next(event for event in events if event.event_type == "skills_selected")
    assert event_types.index("advance_started") < event_types.index("skills_selected")
    assert event_types.index("skills_selected") < event_types.index("artifact_attached")
    assert event_types.index("artifact_attached") < event_types.index(
        "skill_contract_gates_checked"
    )
    assert skills_event.metadata["schema"] == "keystone.skills_selected.v1"
    assert skills_event.metadata["agent_name"] == "business_research_analyst"
    assert "business_research_specialist_contracts" in skills_event.metadata["selected_skills"]
    assert "evidence_attribution_and_claim_mapping" in skills_event.metadata["selected_skills"]
    assert "outreach_composer_specialist_contracts" not in skills_event.metadata["selected_skills"]
    assert skills_event.metadata["selection_reasons"]["business_research_specialist_contracts"] == [
        "specialist"
    ]
    assert skills_event.metadata["selector_input_sha256"]
    gate_event = next(
        event for event in events if event.event_type == "skill_contract_gates_checked"
    )
    assert gate_event.metadata["schema"] == "keystone.skill_contract_gates.v1"
    assert gate_event.metadata["agent_name"] == "business_research_analyst"
    assert gate_event.metadata["counts"]["passed"] >= 1
    gate = gate_event.metadata["gates"][0]
    assert gate["gate_id"] == "business_research_claim_gate"
    assert gate["status"] == "passed"
    assert "business_research.claim_gate" in gate_event.metadata["eval_labels"]


def test_requested_context_sources_are_added_to_context_pack(tmp_path: Path) -> None:
    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "Chief of Staff: review Airtable, Google Docs, Google Drive, "
                "Google Sheets, Gmail, and KNI documents before answering."
            ),
            requested_route=WorkItemRoute.CHIEF_OF_STAFF,
            database_url=_database_url(tmp_path),
            save=True,
        )
    )

    assert result.context_pack is not None
    manifest = result.context_pack["target"]["metadata"]["context_source_manifest"]
    sources = {entry["source"]: entry for entry in manifest["sources"]}

    for source in (
        "airtable",
        "google_docs",
        "google_drive",
        "google_sheets",
        "gmail",
        "kni_documents",
    ):
        assert sources[source]["requested"] is True
        assert sources[source]["status"] == "requested_available_as_tool"
        assert sources[source]["tools"]


def test_context_pack_source_status_counts_extracted_read_statuses() -> None:
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="OpenAI mental health brief",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        sources=[
            WorkItemSourceRef(
                title="OpenAI sensitive conversations update",
                url="https://openai.com/index/chatgpt-recognize-context-in-sensitive-conversations/",
                source_type="company_site",
                supported_claim="OpenAI described sensitive-conversation safety work.",
                evidence_excerpt=(
                    "OpenAI says ChatGPT can better recognize warning signs over time."
                ),
                extraction_status="page_read",
            ),
            WorkItemSourceRef(
                title="Search result snippet",
                url="https://example.com/snippet",
                source_type="web",
                supported_claim="Snippet-only search result.",
                extraction_status="snippet_only",
            ),
        ],
    )

    pack = build_context_pack_for_route(item, WorkItemRoute.BUSINESS_RESEARCH_ANALYST)

    assert pack.source_context_status == {
        "selected_url_count": 2,
        "extracted_url_count": 1,
        "evidence_url_count": 2,
        "snippet_only_url_count": 1,
        "statuses": ["page_read", "snippet_only"],
    }
    assert pack.source_context_sample[0] == {
        "title": "OpenAI sensitive conversations update",
        "url": ("https://openai.com/index/chatgpt-recognize-context-in-sensitive-conversations/"),
        "source_type": "company_site",
        "provider": "",
        "extraction_status": "page_read",
        "supported_claim": "OpenAI described sensitive-conversation safety work.",
        "key_facts": [],
        "evidence_excerpt": "OpenAI says ChatGPT can better recognize warning signs over time.",
        "artifact_title": "",
    }
    assert pack.ordered_sources[0] == {
        "index": 1,
        "reference": "source 1",
        "title": "OpenAI sensitive conversations update",
        "url": ("https://openai.com/index/chatgpt-recognize-context-in-sensitive-conversations/"),
        "source_type": "company_site",
        "extraction_status": "page_read",
        "supported_claim": "OpenAI described sensitive-conversation safety work.",
        "evidence_excerpt": "OpenAI says ChatGPT can better recognize warning signs over time.",
    }
    assert pack.ordered_sources[1]["reference"] == "source 2"
    assert pack.ordered_sources[1]["url"] == "https://example.com/snippet"
    assert pack.source_context_focus["status"] == "matched_sample_sources"
    assert pack.source_context_focus["matching_sample_count"] == 1
    assert "mental" in pack.source_context_focus["terms"]


def test_orchestrator_requested_context_sources_are_added_to_specialist_memo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_chief_of_staff_sdk(
        sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[object]:
        captured.update(sdk_input)
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=workflow_runner.plan_chief_of_staff_request("summarize requested context"),
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="Summarize the account context.",
            requested_route=WorkItemRoute.CHIEF_OF_STAFF,
            database_url=_database_url(tmp_path),
            save=True,
            live_sdk=True,
            orchestrator_preflight={
                "selected_agent": "chief_of_staff",
                "route_result": {
                    "route": "chief_of_staff",
                    "rationale": "Use Airtable records and Gmail thread context before synthesis.",
                },
            },
        )
    )

    assert result.context_pack is not None
    orchestrator_context = captured["orchestrator_context"]
    assert isinstance(orchestrator_context, dict)
    manifest = orchestrator_context["context_source_manifest"]
    sources = {entry["source"]: entry for entry in manifest["sources"]}
    assert "orchestrator_route_result" in sources["airtable"]["requested_by"]
    assert "orchestrator_route_result" in sources["gmail"]["requested_by"]


def test_chief_of_staff_local_kni_packet_uses_latest_wrapped_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    latest_ask = "who was the broker for the CFC insurance?"
    wrapped_request = (
        "chief of staff continue this prior Slack thread.\n"
        f"Latest request: {latest_ask}\n"
        "Previous request: chief of staff who provides insurance for Keystone Neuroinformatics?\n"
        "Previous result: CFC Underwriting Limited.\n"
        "Slack thread context: previous local KNI insurance discussion."
    )

    work_item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="CFC insurance follow-up",
        request_text=wrapped_request,
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
        target=WorkItemTarget(metadata={"slack_context": {"channel_id": "C123"}}),
        status=WorkItemStatus.IN_PROGRESS,
    )

    def fake_plan_chief_of_staff_request(*_args: object, **_kwargs: object) -> ChiefOfStaffResult:
        raise AssertionError("local KNI live evidence must be built from the focused query")

    def fake_build_local_kni_evidence_packet_for_query(
        query_text: str,
        *,
        max_candidate_documents: int = 5,
    ) -> dict[str, object]:
        del max_candidate_documents
        captured["packet_query_text"] = query_text
        return {
            "packet_type": "bounded_local_kni_document_evidence",
            "local_only": True,
            "send_enabled": False,
            "candidate_documents": [
                {
                    "relative_path": "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf",
                    "content_excerpt": "PRODUCER IAO, Inc. DBA ProAssurance Agency",
                    "local_only": True,
                    "send_enabled": False,
                }
            ],
            "retrieval_diagnostics": {
                "local_only": True,
                "send_enabled": False,
                "effective_lookup_kind": "insurance",
                "effective_answer_focus": "broker",
            },
        }

    def fake_run_chief_of_staff_sdk(
        sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured["sdk_input"] = sdk_input
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "The broker/producer is ProAssurance. Evidence path: "
                    "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="project-context-review",
                    target_channel="current-thread",
                ),
                retrieval_diagnostics={
                    "local_only": True,
                    "send_enabled": False,
                    "evidence_path": "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf",
                },
            ),
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.setattr(workflow_runner, "plan_chief_of_staff_request", fake_plan_chief_of_staff_request)
    monkeypatch.setattr(
        workflow_runner,
        "build_local_kni_evidence_packet_for_query",
        fake_build_local_kni_evidence_packet_for_query,
    )
    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(
        workflow_runner,
        "runtime_source_layer_policy_context",
        lambda agent_name: {
            "agent_name": agent_name,
            "status": "declared",
            "available": True,
            "layers": [
                {
                    "layer": "local_kni_documents",
                    "runtime_status": "ready",
                    "runtime_available": True,
                },
                {
                    "layer": "public_web_search",
                    "runtime_status": "attached_live_gated",
                    "runtime_available": True,
                },
            ],
        },
    )

    result = workflow_runner._advance_chief_of_staff(
        work_item,
        request=WorkflowRunRequest(
            request_text=wrapped_request,
            database_url=_database_url(tmp_path),
            live_sdk=True,
        ),
        store=None,
    )

    assert result.status == WorkItemStatus.DONE
    assert captured["packet_query_text"] == latest_ask
    sdk_input = captured["sdk_input"]
    assert sdk_input["request"] == wrapped_request
    assert sdk_input["orchestrator_context"]["local_kni_evidence_query"] == latest_ask
    assert sdk_input["local_kni_evidence_packet"]["candidate_documents"][0][
        "relative_path"
    ].endswith("COI_AnupSharma_2026.pdf")
    assert sdk_input["runtime_source_layer_policy"]["agent_name"] == "chief_of_staff"
    layer_status = {
        layer["layer"]: layer["runtime_status"]
        for layer in sdk_input["runtime_source_layer_policy"]["layers"]
    }
    assert layer_status["local_kni_documents"] == "ready"
    assert layer_status["public_web_search"] == "attached_live_gated"


def test_chief_of_staff_workitem_repairs_local_kni_path_without_replacing_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_text = (
        "chief of staff what department provided the confirmation of organized "
        "documentation for Keystone Neuroinformatics llc in pennsylvania?"
    )
    work_item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Formation department",
        request_text=request_text,
        current_route=WorkItemRoute.CHIEF_OF_STAFF,
        target=WorkItemTarget(metadata={"slack_context": {"channel_id": "C123"}}),
        status=WorkItemStatus.IN_PROGRESS,
    )

    def fake_plan_chief_of_staff_request(*_args: object, **_kwargs: object) -> ChiefOfStaffResult:
        raise AssertionError("live local KNI answer should be repaired, not replaced")

    def fake_build_local_kni_evidence_packet_for_query(
        query_text: str,
        *,
        max_candidate_documents: int = 5,
    ) -> dict[str, object]:
        del query_text, max_candidate_documents
        return {
            "packet_type": "bounded_local_kni_document_evidence",
            "local_only": True,
            "send_enabled": False,
            "candidate_documents": [
                {
                    "relative_path": (
                        "00_Admin/Formation/2-12-26-PA-FormationDocument-"
                        "Keystone Neuroinformatics LLC.pdf"
                    ),
                    "content_excerpt": "Pennsylvania Department of State confirmation",
                    "local_only": True,
                    "send_enabled": False,
                }
            ],
            "retrieval_diagnostics": {
                "local_only": True,
                "send_enabled": False,
                "effective_lookup_kind": "formation",
                "effective_answer_focus": "filing_role",
            },
        }

    def fake_run_chief_of_staff_sdk(
        sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[ChiefOfStaffResult]:
        assert sdk_input["local_kni_evidence_packet"]["local_only"] is True
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "The confirmation appears to come from the Pennsylvania "
                    "Department of State."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="project-context-review",
                    target_channel="current-thread",
                ),
                retrieval_diagnostics={
                    "local_only": True,
                    "send_enabled": False,
                    "lookup_kind": "formation",
                },
            ),
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.setattr(workflow_runner, "plan_chief_of_staff_request", fake_plan_chief_of_staff_request)
    monkeypatch.setattr(
        workflow_runner,
        "build_local_kni_evidence_packet_for_query",
        fake_build_local_kni_evidence_packet_for_query,
    )
    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(
        workflow_runner,
        "runtime_source_layer_policy_context",
        lambda agent_name: {"agent_name": agent_name, "layers": []},
    )

    result = workflow_runner._advance_chief_of_staff(
        work_item,
        request=WorkflowRunRequest(
            request_text=request_text,
            database_url=_database_url(tmp_path),
            live_sdk=True,
        ),
        store=None,
    )

    assert result.status == WorkItemStatus.DONE
    assert "Pennsylvania Department of State" in result.human_summary
    artifact = result.artifact_refs[0]
    diagnostics = artifact.metadata["retrieval_diagnostics"]
    assert diagnostics["evidence_path"].startswith("00_Admin/Formation/")
    assert any(
        source["source_type"] == "local_kni_document"
        for source in artifact.metadata["source_refs"]
    )
    assert any("live model answer was preserved" in note for note in result.audit_notes)


def test_specialist_memo_includes_source_context_status() -> None:
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="OpenAI mental health brief",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        sources=[
            WorkItemSourceRef(
                title="OpenAI mental health work",
                url="https://openai.com/index/update-on-mental-health-related-work/",
                source_type="company_site",
                supported_claim="OpenAI described mental-health-related safety work.",
                evidence_excerpt="OpenAI says it is improving sensitive conversation handling.",
                extraction_status="article_read",
            )
        ],
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="company_profile",
                artifact_id="openai",
                source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                title="OpenAI",
                summary="Source-backed profile.",
                metadata={
                    "retrieval_diagnostics": {
                        "source_triage": {
                            "mode": "fixture_safe_source_triage",
                            "recommended_action": "synthesize_from_retained_sources",
                            "needs_broaden_or_deepen": False,
                            "retained_source_ids": ["selected:1"],
                            "review_source_ids": [],
                            "rejected_source_ids": [],
                            "deepen_source_ids": [],
                            "recall_gaps": [],
                            "decisions": [
                                {
                                    "source_id": "selected:1",
                                    "title": "OpenAI mental health work",
                                    "url": (
                                        "https://openai.com/index/"
                                        "update-on-mental-health-related-work/"
                                    ),
                                    "decision": "retain",
                                    "relevance_score": 90,
                                    "directness_score": 85,
                                    "rationale": "retain: source is extracted and on focus",
                                }
                            ],
                        }
                    }
                },
            )
        ],
    )

    payload = workflow_runner._specialist_orchestrator_context_payload(
        WorkflowRunRequest(request_text="business research analyst summarize this source"),
        item,
    )

    assert payload["context_pack"]["route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert payload["context_pack"]["source_context_status"] == {
        "selected_url_count": 1,
        "extracted_url_count": 1,
        "evidence_url_count": 1,
        "snippet_only_url_count": 0,
        "statuses": ["article_read"],
    }
    assert payload["context_pack"]["source_context_sample"] == [
        {
            "title": "OpenAI mental health work",
            "url": "https://openai.com/index/update-on-mental-health-related-work/",
            "source_type": "company_site",
            "provider": "",
            "extraction_status": "article_read",
            "supported_claim": "OpenAI described mental-health-related safety work.",
            "key_facts": [],
            "evidence_excerpt": ("OpenAI says it is improving sensitive conversation handling."),
            "artifact_title": "",
        }
    ]
    assert payload["context_pack"]["ordered_sources"] == [
        {
            "index": 1,
            "reference": "source 1",
            "title": "OpenAI mental health work",
            "url": "https://openai.com/index/update-on-mental-health-related-work/",
            "source_type": "company_site",
            "extraction_status": "article_read",
            "supported_claim": "OpenAI described mental-health-related safety work.",
            "evidence_excerpt": ("OpenAI says it is improving sensitive conversation handling."),
        }
    ]
    assert payload["context_pack"]["source_context_focus"]["status"] == "matched_sample_sources"
    assert payload["context_pack"]["source_context_focus"]["matching_sample_count"] == 1
    assert payload["context_pack"]["source_triage"]["recommended_action"] == (
        "synthesize_from_retained_sources"
    )
    assert payload["context_pack"]["source_triage"]["decision_counts"] == {"retain": 1}
    assert payload["context_pack"]["source_triage"]["retained_source_ids"] == ["selected:1"]


def test_chief_deep_web_brief_attaches_extracted_source_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[object]:
        output = workflow_runner.plan_chief_of_staff_request(
            "deeper source-backed web search on OpenAI mental health"
        ).model_copy(
            update={
                "summary": "OpenAI mental health source-backed brief.",
                "sources": [
                    ChiefOfStaffSourceRef(
                        title="OpenAI mental health update",
                        url="https://openai.com/index/update-on-mental-health-related-work/",
                        source_type="official_page",
                        note="Official OpenAI update identified by live search.",
                    )
                ],
                "retrieval_diagnostics": {
                    "provider_result_samples": {
                        "exa": [
                            {
                                "title": "OpenAI mental health update",
                                "url": "https://openai.com/index/update-on-mental-health-related-work/",
                                "snippet": "OpenAI describes mental-health-related safety work.",
                            }
                        ]
                    }
                },
            }
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result=None,
            live=True,
        )

    def fake_read_linked_article_impl(
        url: str,
        *,
        request_text: str = "",
        max_chars: int = 6000,
        live: bool = False,
    ) -> dict[str, object]:
        assert live is True
        assert "deeper" in request_text.lower()
        return {
            "status": "success",
            "url": url,
            "title": "OpenAI mental health update",
            "provider": "trafilatura",
            "text_or_markdown": (
                "OpenAI says it is improving ChatGPT behavior in emotionally "
                "sensitive conversations, adding Trusted Contact workflows, "
                "working with clinicians, and funding AI and mental health research."
            ),
            "claims": [
                "OpenAI is improving ChatGPT behavior in emotionally sensitive conversations.",
                "OpenAI is adding Trusted Contact workflows and consulting clinicians.",
            ],
        }

    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(
        workflow_runner,
        "read_linked_article_impl",
        fake_read_linked_article_impl,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "chief of staff do a deeper source-backed web search on what OpenAI "
                "is doing about mental health. Give Answer, Detailed Summary, source URLs, "
                "and provider metadata."
            ),
            requested_route=WorkItemRoute.CHIEF_OF_STAFF,
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
        )
    )

    metadata = result.artifact_refs[0].metadata
    source_ref = metadata["source_refs"][0]
    assert metadata["source_context_status"]["extracted_url_count"] == 1
    assert "emotionally sensitive conversations" in source_ref["evidence_excerpt"]
    assert source_ref["key_facts"][:2] == [
        "OpenAI is improving ChatGPT behavior in emotionally sensitive conversations.",
        "OpenAI is adding Trusted Contact workflows and consulting clinicians.",
    ]
    assert metadata["retrieval_diagnostics"]["provider_result_samples"]["exa"]


def test_chief_retrieved_link_content_prompt_reads_provider_sample_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_text = (
        "chief of staff can you do a deeper read-only search on one focused question: "
        "what do recent public sources say about trusted-contact, teen-safety, or "
        "escalation features in consumer AI chat tools that might be relevant to "
        "mental-health risk? Please give a concise Answer and a Detailed Summary "
        "that synthesizes what the retrieved link content says across sources before "
        "listing links. Include 3-5 source links. If the links were only snippets, "
        "say so; otherwise read/extract and summarize the source content."
    )
    output = workflow_runner.plan_chief_of_staff_request(request_text).model_copy(
        update={
            "sources": [],
            "retrieval_diagnostics": {
                "provider_result_samples": {
                    "agents-web-search": [
                        {
                            "title": "Introducing Trusted Contact in ChatGPT",
                            "url": "https://openai.com/index/introducing-trusted-contact-in-chatgpt/",
                            "snippet": "OpenAI describes Trusted Contact notifications.",
                        }
                    ]
                }
            },
        }
    )
    calls: list[tuple[str, bool]] = []

    def fake_read_chief_selected_source(
        url: str,
        *,
        request_text: str,
        live: bool,
    ) -> dict[str, object]:
        calls.append((url, live))
        return {
            "status": "success",
            "url": url,
            "title": "Introducing Trusted Contact in ChatGPT",
            "provider": "trafilatura",
            "text_or_markdown": (
                "OpenAI says Trusted Contact lets adults nominate someone who may "
                "be notified when automated systems detect serious self-harm risk."
            ),
            "claims": [
                "Trusted Contact is an optional escalation feature for serious self-harm risk.",
            ],
        }

    monkeypatch.setattr(
        workflow_runner,
        "_read_chief_selected_source",
        fake_read_chief_selected_source,
    )

    refs = workflow_runner._chief_of_staff_source_refs(
        output,
        request_text=request_text,
        live=True,
    )

    assert calls == [("https://openai.com/index/introducing-trusted-contact-in-chatgpt/", True)]
    assert len(refs) == 1
    assert refs[0].extraction_status == "success"
    assert refs[0].provider == "trafilatura"
    assert "serious self-harm risk" in refs[0].evidence_excerpt
    assert refs[0].key_facts == [
        "Trusted Contact is an optional escalation feature for serious self-harm risk.",
        (
            "OpenAI says Trusted Contact lets adults nominate someone who may be "
            "notified when automated systems detect serious self-harm risk."
        ),
    ]


def test_chief_explicit_url_read_extract_uses_user_urls_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = [
        "https://blog.character.ai/how-character-ai-prioritizes-teen-safety/",
        "https://arxiv.org/abs/2510.11185",
        "https://arxiv.org/abs/2406.10461",
    ]
    captured_sdk_input: dict[str, object] = {}

    def fake_run_chief_of_staff_sdk(
        sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[object]:
        captured_sdk_input.update(sdk_input)
        output = workflow_runner.plan_chief_of_staff_request(
            "read/extract three source URLs"
        ).model_copy(
            update={
                "summary": "Generic operations fallback should not override user URLs.",
                "audit_notes": [
                    "Full page extraction was unavailable; synthesis is snippet-based."
                ],
                "sources": [
                    ChiefOfStaffSourceRef(
                        title="Generic docs",
                        url="https://developers.openai.com/api/docs/guides/agents",
                        source_type="openai_docs",
                        note="Generic docs fallback.",
                    )
                ],
            }
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result=None,
            live=True,
        )

    read_urls: list[str] = []

    def fake_read_linked_article_impl(
        url: str,
        *,
        request_text: str = "",
        max_chars: int = 6000,
        live: bool = False,
    ) -> dict[str, object]:
        read_urls.append(url)
        assert live is True
        assert "read/extract" in request_text.lower()
        return {
            "status": "success",
            "url": url,
            "title": f"Extracted {len(read_urls)}",
            "provider": "trafilatura",
            "text_or_markdown": f"Extracted page content for source {len(read_urls)}.",
            "claims": [f"Claim from explicit source {len(read_urls)}."],
        }

    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(
        workflow_runner,
        "read_linked_article_impl",
        fake_read_linked_article_impl,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "chief of staff backend validation only. Read/extract these three URLs "
                f"and synthesize what they collectively say: {urls[0]} ; {urls[1]} ; {urls[2]}."
            ),
            requested_route=WorkItemRoute.CHIEF_OF_STAFF,
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
        )
    )

    metadata = result.artifact_refs[0].metadata
    source_refs = metadata["source_refs"]
    assert read_urls == urls
    assert [ref["url"] for ref in source_refs[:3]] == urls
    assert metadata["source_context_status"]["extracted_url_count"] >= 3
    assert "Extracted page content for source 1" in source_refs[0]["evidence_excerpt"]
    selected_context = captured_sdk_input["selected_source_context"]
    assert isinstance(selected_context, dict)
    assert selected_context["source_context_status"]["extracted_url_count"] == 3
    assert "Claim from explicit source 1" in str(selected_context)
    assert "Detailed Summary" in result.human_summary
    assert "Claim from explicit source 1" in result.human_summary
    assert "Extracted page content for source 1" in result.human_summary
    assert "User-supplied URL selected" not in result.human_summary
    assert "Generic docs" not in result.human_summary
    assert not any("snippet-based" in note for note in result.audit_notes)
    assert any("read/extracted" in note for note in result.audit_notes)


def test_chief_source_brief_does_not_get_false_research_stage_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_chief_of_staff_sdk(
        _sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[object]:
        output = workflow_runner.plan_chief_of_staff_request(
            "source-backed brief on OpenAI mental health"
        ).model_copy(
            update={
                "summary": "OpenAI mental health source-backed brief.",
                "sources": [
                    ChiefOfStaffSourceRef(
                        title="OpenAI mental health update",
                        url="https://openai.com/index/update-on-mental-health-related-work/",
                        source_type="official_page",
                        note="Official source for the brief.",
                    )
                ],
            }
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                "chief of staff what is OpenAI doing about mental health? Give a "
                "source-backed brief with Answer, Detailed Summary, links, and metadata."
            ),
            requested_route=WorkItemRoute.CHIEF_OF_STAFF,
            database_url=_database_url(tmp_path),
            save=True,
            live_sdk=True,
        ),
        max_steps=1,
    )

    blocker_codes = {blocker.code for blocker in result.blockers}
    assert "manager_loop_research_not_completed" not in blocker_codes


def test_manager_loop_records_orchestrator_review_feedback_for_agent_run(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    feedback_events: list[tuple[str, dict[str, object]]] = []

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        ),
        max_steps=2,
        feedback_callback=lambda event, payload: feedback_events.append((event, payload)),
    )

    store = SQLiteStore(database_url)
    events = store.list_work_item_events(result.work_item.id)
    review_events = [event for event in events if event.event_type == "manager_loop_review"]
    efficiency_events = [event for event in events if event.event_type == "manager_loop_efficiency"]

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert review_events
    assert review_events[0].metadata["route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert review_events[0].metadata["review_status"] in {"pass", "partial", "fail"}
    assert review_events[0].metadata["review_decision"] in {"pass", "warn", "block"}
    if review_events[0].metadata["review_status"] == "fail":
        assert review_events[0].metadata["blocking"] is False
        assert review_events[0].metadata["advisory"] is True
        assert review_events[0].metadata["review_decision"] == "warn"
    assert review_events[0].metadata["planner_memo"]["chosen_route"] == (
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    )
    feedback_review = next(
        payload for event, payload in feedback_events if event == "manager_loop_review"
    )
    assert feedback_review["review_decision"] == review_events[0].metadata["review_decision"]
    assert feedback_review["blocking"] == review_events[0].metadata["blocking"]
    assert feedback_review["advisory"] == review_events[0].metadata["advisory"]
    assert any(event == "manager_loop_completed" for event, _payload in feedback_events)
    assert efficiency_events
    efficiency = efficiency_events[-1].metadata
    assert efficiency["schema"] == "keystone.manager_loop_efficiency.v1"
    assert efficiency["metric_name"] == "keystone.manager_loop.efficiency"
    assert efficiency["metric_version"] == "v1"
    assert efficiency["latency_bucket"]
    assert efficiency["efficiency_signal"] in {
        "fast_completion",
        "completed_high_latency",
        "completed_after_repair",
        "incomplete_or_blocked",
    }
    assert efficiency["step_count"] >= 1
    assert efficiency["specialist_step_count"] >= 1
    assert efficiency["elapsed_seconds"] >= 0
    assert efficiency["memory_id"] > 0
    memory_items = store.list_memory_items(memory_type="manager_loop_efficiency")
    assert memory_items
    assert memory_items[-1].content["metric_name"] == "keystone.manager_loop.efficiency"
    assert memory_items[-1].content["final_route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert result.work_item.target.metadata["manager_loop_efficiency"]["final_route"] == (
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    )


def test_manager_loop_failed_review_blocks_otherwise_active_single_step(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeReview:
        status = "fail"
        overall_score = 40
        approval_boundary_ok = True
        observed_gaps = ["Output did not answer the request."]
        recommended_next_step = "Repair the output before presenting it."

    monkeypatch.setattr(workflow_runner, "review_specialist_output", lambda **_kwargs: FakeReview())

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=_database_url(tmp_path),
            save=True,
        ),
        max_steps=2,
    )

    assert result.status == WorkItemStatus.BLOCKED
    assert any(blocker.code == "manager_loop_review_failed" for blocker in result.blockers)
    assert result.next_action is not None
    assert result.next_action.action == "repair_or_deepen_specialist_output"


def test_manager_loop_marks_single_step_artifact_done_with_optional_next_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeReview:
        status = "pass"
        overall_score = 92
        approval_boundary_ok = True
        observed_gaps: list[str] = []
        recommended_next_step = "Ready for human review."

    monkeypatch.setattr(workflow_runner, "review_specialist_output", lambda **_kwargs: FakeReview())

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="opportunity scout find behavioral health AI companies",
            database_url=_database_url(tmp_path),
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "requested_agent": "opportunity_scout",
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": "behavioral health AI companies",
            },
        ),
        max_steps=1,
    )

    assert result.advanced is True
    assert result.artifact_refs
    assert result.next_action is not None
    assert result.next_action.action == "review_opportunities"
    assert result.status == WorkItemStatus.DONE


def test_manager_loop_repairs_done_opportunity_packet_after_authoritative_review_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)
    live_calls: list[str] = []
    retrieval_hints: list[object] = []
    review_calls = 0

    def fake_run_live(**kwargs: object):
        live_calls.append(str(kwargs.get("topic") or ""))
        retrieval_hints.append(kwargs.get("retrieval_hint"))
        pass_number = len(live_calls)
        return (
            OpportunityScoutResult(
                topic="formal opportunity scan",
                dry_run=False,
                records=[
                    OpportunityRecord(
                        company_name=f"Behavioral Health AI Pilot {pass_number}",
                        opportunity_type="grant or collaboration opportunity",
                        priority_score=92,
                        why_now_signal=(
                            "Active RFP for AI-enabled behavioral health tools with a "
                            "June 2026 deadline and vendor/partner participation."
                        ),
                        recommended_next_step="Review eligibility before outreach.",
                        keystone_fit_reason=(
                            "Keystone could participate as a small clinical AI evaluation "
                            "or implementation partner."
                        ),
                        outside_consulting_likelihood=65,
                        handoff_to_business_research_analyst=False,
                        sources=[
                            OpportunitySource(
                                title="Behavioral Health AI Pilot RFP",
                                url=f"https://example.gov/rfp-{pass_number}",
                                source_type="government",
                                supported_signal=(
                                    "Active RFP for AI-enabled behavioral health tools; "
                                    "deadline June 30, 2026; small business vendors and "
                                    "clinical implementation partners may participate."
                                ),
                                evidence_excerpt=(
                                    "The source describes an active behavioral health AI "
                                    "pilot RFP, deadline evidence, and vendor or partner "
                                    "eligibility."
                                ),
                            )
                        ],
                    )
                ],
            ),
            {
                "debug_notes": [f"fake opportunity retrieval pass {pass_number}"],
                "retrieval_diagnostics": {"provider_summary": "searxng+exa"},
            },
        )

    class FakeReview:
        def __init__(self, *, status: str) -> None:
            self.status = status
            self.overall_score = 40 if status == "fail" else 92
            self.approval_boundary_ok = True
            self.observed_gaps = ["Output did not answer the request."] if status == "fail" else []
            self.recommended_next_step = (
                "Repair the opportunity packet before presenting it."
                if status == "fail"
                else "Ready for review."
            )

    def fake_review_specialist_output(**_kwargs: object) -> FakeReview:
        nonlocal review_calls
        review_calls += 1
        return FakeReview(status="fail" if review_calls == 1 else "pass")

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_opportunity_scout_live",
        fake_run_live,
    )
    monkeypatch.setattr(
        workflow_runner,
        "review_specialist_output",
        fake_review_specialist_output,
    )

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout find source-backed grant, RFP, or pilot "
                "opportunities for AI-enabled behavioral health. Stop after an "
                "opportunity review packet."
            ),
            database_url=database_url,
            save=True,
            live_search=True,
            max_results=1,
            manual_request_plan={
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": "AI-enabled behavioral health formal opportunities",
                "constraints": ["grant", "RFP", "pilot", "formal opportunity"],
                "task_objective": "opportunity_discovery",
            },
        ),
        max_steps=2,
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    review_events = [event for event in events if event.event_type == "manager_loop_review"]

    assert len(live_calls) == 2
    assert retrieval_hints[0] is None
    assert retrieval_hints[1] is not None
    assert retrieval_hints[1].needs_precision_search is False
    assert retrieval_hints[1].needs_search_review is False
    assert result.status == WorkItemStatus.DONE
    assert not any(blocker.code == "manager_loop_review_failed" for blocker in result.blockers)
    assert review_events[0].metadata["review_decision"] == "repair"
    assert review_events[-1].metadata["review_decision"] == "pass"
    repair_started = next(
        event for event in events if event.event_type == "manager_loop_repair_started"
    )
    assert repair_started.metadata["search_repair_hint"] == (
        "repair_synthesis_from_existing_context"
    )
    assert any(event.event_type == "manager_loop_repair_completed" for event in events)


def test_manager_loop_search_repair_hint_requests_deepening_for_source_gaps() -> None:
    request = WorkflowRunRequest(
        request_text="business research analyst research OpenEvidence",
        external_context=workflow_runner._manager_loop_repair_external_context(
            WorkflowRunRequest(request_text="business research analyst research OpenEvidence"),
            review_context={
                "route": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                "review_status": "fail",
                "overall_score": 45,
                "observed_gaps": ["Source evidence is off-target and retrieval should broaden."],
                "recommended_next_step": "Run a broader independent-source pass.",
            },
        ),
    )

    hint = workflow_runner._retrieval_hint_for_request(request)

    assert hint is not None
    assert hint.needs_precision_search is True
    assert hint.needs_structured_enrichment is True
    assert hint.needs_search_review is True
    assert hint.source == "manager_loop_repair"


def test_manager_loop_search_repair_hint_keeps_presentation_gaps_synthesis_only() -> None:
    request = WorkflowRunRequest(
        request_text="opportunity scout find behavioral health opportunities",
        external_context=workflow_runner._manager_loop_repair_external_context(
            WorkflowRunRequest(
                request_text="opportunity scout find behavioral health opportunities"
            ),
            review_context={
                "route": WorkItemRoute.OPPORTUNITY_SCOUT.value,
                "review_status": "fail",
                "overall_score": 44,
                "observed_gaps": [
                    "Add concise summary or rationale.",
                    "Tie output more directly to request and evidence.",
                ],
                "recommended_next_step": "Repair the Slack-facing answer from existing evidence.",
            },
        ),
    )

    hint = workflow_runner._retrieval_hint_for_request(request)

    assert hint is not None
    assert hint.needs_precision_search is False
    assert hint.needs_structured_enrichment is False
    assert hint.needs_search_review is False
    assert hint.source == "manager_loop_repair"


def test_manager_loop_review_uses_latest_thread_followup_for_any_route(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeReview:
        status = "pass"
        overall_score = 95
        approval_boundary_ok = True
        observed_gaps: list[str] = []
        recommended_next_step = "Ready for human review."

    def fake_review_specialist_output(**kwargs):
        captured.update(kwargs)
        return FakeReview()

    monkeypatch.setattr(workflow_runner, "review_specialist_output", fake_review_specialist_output)

    work_item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Opportunity follow-up",
        request_text="Find behavioral health AI opportunities.",
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_id="opp-1",
                artifact_type="opportunity",
                source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                title="Example opportunity",
                summary="A source-backed opportunity.",
            )
        ],
    )
    result = workflow_runner.WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.IN_PROGRESS,
        advanced=True,
        artifact_refs=work_item.artifact_refs,
        human_summary="A broad opportunity summary.",
    )

    reviewed = workflow_runner._review_manager_loop_step(
        result,
        original_request=WorkflowRunRequest(
            request_text=(
                "Find behavioral health AI opportunities.\n"
                "Previous result: a broad opportunity summary.\n"
                "Follow-up: which of these are strongest for academic partnerships?"
            ),
        ),
        step_index=1,
        store=None,
        feedback_callback=None,
    )

    assert captured["request_summary"] == (
        "which of these are strongest for academic partnerships?"
    )
    assert captured["output"]["latest_user_request"] == captured["request_summary"]
    latest_review = reviewed.work_item.target.metadata["orchestrator_reviews"][-1]
    assert latest_review["latest_user_request"] == captured["request_summary"]


def test_manager_review_treats_limited_independent_sources_as_repairable() -> None:
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Company follow-up",
        request_text="Research OpenEvidence.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_id="company-1",
                artifact_type="company_profile",
                source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                title="OpenEvidence",
                summary="A source-backed company profile.",
            )
        ],
    )
    result = workflow_runner.WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.IN_PROGRESS,
        advanced=True,
        artifact_refs=work_item.artifact_refs,
        human_summary="A company profile was attached.",
    )
    review_context = {
        "review_status": "fail",
        "approval_boundary_ok": True,
        "observed_gaps": ["Independent sources are limited; deepen retrieval before outreach."],
    }

    assert workflow_runner._manager_review_failure_is_authoritative(review_context, result) is True


def test_manager_loop_repair_allows_review_only_approval_gate() -> None:
    next_action = WorkItemNextAction(
        action="review_chief_of_staff_plan",
        agent=WorkItemRoute.CHIEF_OF_STAFF,
        description="Review the source-backed Chief of Staff plan.",
        requires_approval=True,
    )

    assert workflow_runner._next_action_blocks_manager_loop_repair(next_action) is False


def test_manager_loop_repair_blocks_side_effect_approval_gate() -> None:
    next_action = WorkItemNextAction(
        action="approve_outreach_send",
        agent=WorkItemRoute.OUTREACH_COMPOSER,
        description="Approve external send for outreach.",
        requires_approval=True,
    )

    assert workflow_runner._next_action_blocks_manager_loop_repair(next_action) is True


def test_manager_loop_repairs_deep_chief_search_before_review_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_calls = 0
    review_calls = 0

    def fake_run_chief_of_staff_sdk(
        *_args: object, **_kwargs: object
    ) -> TypedAgentRunResult[object]:
        nonlocal sdk_calls
        sdk_calls += 1
        summary = (
            "Search completed."
            if sdk_calls == 1
            else (
                "Answer: source-backed safety features include teen-specific model "
                "limits, escalation pathways, and parental oversight.\n\n"
                "Detailed Summary\nThe repaired answer summarizes the source-backed "
                "evidence instead of stopping at metadata. Source: "
                "https://example.com/source"
            )
        )
        output = ChiefOfStaffResult(
            mode="llm",
            summary=summary,
            approval_required=True,
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="slack-article-review",
                target_channel="current Slack thread",
            ),
            audit_notes=[],
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"call": sdk_calls},
            live=True,
        )

    def fake_review_specialist_output(**_kwargs: object) -> object:
        nonlocal review_calls
        review_calls += 1

        class Review:
            status = "fail" if review_calls == 1 else "pass"
            overall_score = 45 if review_calls == 1 else 92
            approval_boundary_ok = True
            observed_gaps = (
                ["Output did not answer the request; deepen retrieval before finalizing."]
                if review_calls == 1
                else []
            )
            recommended_next_step = (
                "Repair before review." if review_calls == 1 else "Ready for review."
            )

        return Review()

    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(workflow_runner, "review_specialist_output", fake_review_specialist_output)

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                "chief of staff do a deeper source-backed search on mental health AI "
                "safety features. Return Answer, Detailed Summary, source URLs, and "
                "compact metadata."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan={
                "source": "test",
                "requested_agent": "chief_of_staff",
                "target_agent": "chief_of_staff",
                "intent": "research_brief",
            },
        ),
        max_steps=2,
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    review_events = [event for event in events if event.event_type == "manager_loop_review"]

    assert sdk_calls == 2
    assert result.status == WorkItemStatus.DONE
    assert "Detailed Summary" in result.human_summary
    assert review_events[0].metadata["review_decision"] == "repair"
    assert review_events[-1].metadata["review_decision"] == "pass"
    assert any(event.event_type == "manager_loop_repair_started" for event in events)


def test_manager_loop_review_repairs_when_source_triage_needs_deepening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PassingReview:
        status = "pass"
        overall_score = 95
        approval_boundary_ok = True
        observed_gaps: list[str] = []
        recommended_next_step = "Ready for human review."

    monkeypatch.setattr(
        workflow_runner,
        "review_specialist_output",
        lambda **_kwargs: PassingReview(),
    )
    artifact = WorkItemArtifactRef(
        artifact_id="company-1",
        artifact_type="company_profile",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        title="OpenAI",
        summary="A source-backed company profile.",
        metadata={
            "retrieval_diagnostics": {
                "source_triage": {
                    "recommended_action": "broaden_or_deepen_before_final_synthesis",
                    "needs_broaden_or_deepen": True,
                    "decision_counts": {"deepen": 1, "reject": 1},
                    "deepen_source_ids": ["selected:1"],
                    "rejected_source_ids": ["selected:2"],
                    "recall_gaps": ["missing expected source lane: press_news"],
                }
            }
        },
    )
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="OpenAI mental health",
        request_text="What is OpenAI doing about mental health right now?",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        artifact_refs=[artifact],
    )
    result = workflow_runner.WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="A company profile was attached.",
    )

    reviewed = workflow_runner._review_manager_loop_step(
        result,
        original_request=WorkflowRunRequest(
            request_text="What is OpenAI doing about mental health right now?",
            allow_manager_loop_repair=True,
        ),
        step_index=1,
        store=None,
        feedback_callback=None,
        defer_block_for_repair=True,
    )
    latest_review = reviewed.work_item.target.metadata["orchestrator_reviews"][-1]

    assert latest_review["review_decision"] == "repair"
    assert latest_review["repair_eligible"] is True
    assert latest_review["review_status"] == "fail"
    assert "Source triage recommended broader/deeper retrieval" in " ".join(
        latest_review["observed_gaps"]
    )
    assert workflow_runner._manager_loop_search_repair_hint(latest_review) == (
        "broaden_or_deepen_search_within_cost_profile"
    )
    assert reviewed.next_action is not None
    assert reviewed.next_action.action == "repair_or_deepen_specialist_output"


def test_stop_after_opportunity_packet_skips_false_research_stage_blocker() -> None:
    work_item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Opportunity packet",
        request_text=(
            "opportunity scout find source-backed grant, RFP, pilot, or call-for-proposals "
            "opportunities. Stop after an opportunity review packet."
        ),
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
    )
    result = workflow_runner.WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="No exact matches found.",
    )

    blockers = workflow_runner._manager_loop_missing_stage_blockers(
        original_request=WorkflowRunRequest(request_text=work_item.request_text),
        result=result,
        loop_steps=[
            {
                "route": WorkItemRoute.OPPORTUNITY_SCOUT.value,
                "status": WorkItemStatus.DONE.value,
                "advanced": True,
            }
        ],
    )

    assert "manager_loop_research_not_completed" not in {blocker.code for blocker in blockers}


def test_formal_opportunity_gate_note_counts_existing_filtered_candidates() -> None:
    scout_result = OpportunityScoutResult(
        topic="behavioral health AI grants",
        records=[],
        filtered_candidates=[
            FilteredOpportunityCandidate(
                company_name="Adjacent grant",
                source_title="Topic-relevant grant",
                reasons=["missing company/vendor path"],
            )
        ],
        review_candidates=[
            FilteredOpportunityCandidate(
                company_name="Review-only candidate",
                source_title="Possible pilot",
                reasons=["unclear timing"],
            )
        ],
    )

    _, notes = workflow_runner._apply_formal_opportunity_result_gates(
        scout_result,
        request_text="Find grants, RFPs, pilots, and calls for proposals.",
    )

    assert notes == [
        (
            "Formal-opportunity exact-match gates evaluated 0 retained record(s); "
            "retrieval already carried 1 filtered candidate(s) and 1 review candidate(s)."
        )
    ]


def test_manager_loop_generic_review_gaps_do_not_block_fixture_artifacts(
    tmp_path: Path,
) -> None:
    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="Find behavioral health AI opportunities.",
            database_url=_database_url(tmp_path),
            save=True,
        ),
        max_steps=2,
    )

    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert result.status == WorkItemStatus.DONE
    assert result.artifact_refs
    assert not any(blocker.code == "manager_loop_review_failed" for blocker in result.blockers)


def test_manager_loop_defers_live_synthesis_until_final_step(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)
    calls: list[str] = []

    def fake_synthesis(result, *, request, sdk_session, store=None):
        del request, sdk_session, store
        calls.append(result.route.value)
        return result

    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        fake_synthesis,
    )

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
            live_sdk=True,
        ),
        max_steps=3,
    )

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert calls == [WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value]


def test_manager_loop_final_synthesis_uses_configured_sdk_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)
    captured: dict[str, object] = {}

    def fake_build_sdk_session(spec):
        captured["session_spec"] = spec
        return {"session_id": spec.session_id, "database_path": spec.database_path}

    def fake_synthesis(result, *, request, sdk_session, store=None):
        del request, store
        captured["sdk_session"] = sdk_session
        return result

    monkeypatch.setattr(workflow_runner, "build_sdk_session", fake_build_sdk_session)
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        fake_synthesis,
    )

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
            live_sdk=True,
            sdk_session_enabled=True,
            sdk_session_id="operator-thread-123",
            sdk_session_db_path=str(tmp_path / "sessions.sqlite3"),
        ),
        max_steps=3,
    )

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    spec = captured["session_spec"]
    assert spec.enabled is True
    assert spec.source == "explicit"
    assert spec.database_path == str(tmp_path / "sessions.sqlite3")
    assert captured["sdk_session"] == {
        "session_id": spec.session_id,
        "database_path": str(tmp_path / "sessions.sqlite3"),
    }


def test_manager_loop_continues_compound_request_without_then(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="research NeuroFlow and find matching opportunities",
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
            },
        ),
        max_steps=2,
    )

    review_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_review"
    ]

    assert [event.metadata["route"] for event in review_events] == [
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
    ]
    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT


def test_manager_loop_does_not_continue_plain_summary_conjunction(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="research NeuroFlow and provide a summary",
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "NeuroFlow",
            },
        ),
        max_steps=2,
    )

    review_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_review"
    ]

    assert [event.metadata["route"] for event in review_events] == [
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    ]
    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST


def test_manager_loop_does_not_treat_leadership_as_lead_discovery(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = get_test_pack_spec("BR-1").natural_prompt
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=3,
    )
    review_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_review"
    ]
    completion_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_completed"
    ]

    assert [event.metadata["route"] for event in review_events] == [
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    ]
    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.status == WorkItemStatus.DONE
    assert not any(blocker.code == "manager_loop_review_failed" for blocker in result.blockers)
    assert "no multi-step workflow" in completion_events[-1].metadata["stop_reason"]


@pytest.mark.parametrize("spec_id", ["OS-1", "OS-3"])
def test_opportunity_only_domain_research_terms_do_not_trigger_research_handoff(
    tmp_path: Path,
    spec_id: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'{spec_id.lower()}.db'}"
    prompt = get_test_pack_spec(spec_id).natural_prompt
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=3,
    )
    review_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_review"
    ]

    assert manual_plan.target_agent == "opportunity_scout"
    assert [event.metadata["route"] for event in review_events] == [
        WorkItemRoute.OPPORTUNITY_SCOUT.value
    ]
    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT


def test_opportunity_no_result_prompt_keeps_search_target_not_quoted_output_label(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = get_test_pack_spec("OS-5").natural_prompt
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=3,
    )

    assert "chief medical officer" in manual_plan.primary_target
    assert "Adjacent but not exact matches" not in manual_plan.primary_target
    assert "chief medical officer" in result.work_item.target.name
    assert "Adjacent but not exact matches" not in result.work_item.target.name
    assert result.status == WorkItemStatus.BLOCKED
    assert not any(ref.artifact_type == "opportunity" for ref in result.artifact_refs)
    assert any(blocker.code == "no_strong_opportunity_matches" for blocker in result.blockers)


def test_opportunity_next_step_output_wording_not_in_workitem_target(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    prompt = "Find behavioral health AI opportunities and recommend the next step."
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=1,
    )

    assert manual_plan.target_agent == "opportunity_scout"
    assert manual_plan.primary_target == "behavioral health AI opportunities"
    assert result.work_item.target.name == "behavioral health AI opportunities"


def test_planning_first_workflow_returns_orchestrator_plan_not_polluted_scout_blocker(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = (
        "Plan the safest workflow to find companies, research the best candidate, "
        "and prepare outreach, but do not save or send anything."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=5,
    )
    blocker_codes = {blocker.code for blocker in result.blockers}

    assert manual_plan.primary_target == "behavioral health AI clinical research"
    assert result.route == WorkItemRoute.ORCHESTRATOR
    assert result.artifact_refs[0].artifact_type == "orchestrator_plan_summary"
    assert "Orchestrator workflow plan" in result.human_summary
    assert "no_opportunities_found" not in blocker_codes


def test_planning_first_workflow_with_preflight_still_returns_orchestrator_plan(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = (
        "Plan the safest workflow to find companies, research the best candidate, "
        "and prepare outreach, but do not save or send anything."
    )
    preflight = run_orchestrator_preflight(
        prompt,
        requested_agent="orchestrator",
        live_manual_plan=False,
    )

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=preflight.manual_request_plan.model_dump(mode="json"),
            orchestrator_preflight=compact_orchestrator_preflight_payload(preflight),
        ),
        max_steps=5,
    )

    assert preflight.selected_agent == WorkItemRoute.ORCHESTRATOR.value
    assert result.route == WorkItemRoute.ORCHESTRATOR
    assert result.artifact_refs[0].artifact_type == "orchestrator_plan_summary"
    assert "Orchestrator workflow plan" in result.human_summary


def test_planning_first_workflow_with_generic_preflight_returns_orchestrator_plan(
    tmp_path: Path,
) -> None:
    prompt = (
        "Plan the safest workflow to find companies, research the best candidate, "
        "and prepare outreach, but do not save or send anything."
    )
    preflight = run_orchestrator_preflight(prompt, live_manual_plan=False)

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=_database_url(tmp_path),
            save=True,
            manual_request_plan=preflight.manual_request_plan.model_dump(mode="json"),
            orchestrator_preflight=compact_orchestrator_preflight_payload(preflight),
        ),
        max_steps=5,
    )

    assert preflight.manual_request_plan.requested_agent is None
    assert result.route == WorkItemRoute.ORCHESTRATOR
    assert result.artifact_refs[0].artifact_type == "orchestrator_plan_summary"
    assert "Orchestrator workflow plan" in result.human_summary


@pytest.mark.parametrize(
    ("prompt", "expected_route", "expected_blocker"),
    [
        (
            "Create a LinkedIn variant only and include the facts used plus source ids used.",
            WorkItemRoute.OUTREACH_COMPOSER,
            "outreach_requires_approved_context",
        ),
        (
            "Send the strongest version to the CEO.",
            WorkItemRoute.OUTREACH_COMPOSER,
            "outreach_requires_approved_context",
        ),
        (
            "Label selected messages as follow-up candidates.",
            WorkItemRoute.GMAIL_TRIAGE,
            "gmail_context_required",
        ),
        (
            "Audit why a previous @KNI response felt unrelated and tell me which agent path should have handled it.",
            WorkItemRoute.CHIEF_OF_STAFF,
            "",
        ),
    ],
)
def test_agent_specific_variant_requests_reach_owning_workitem_gate(
    tmp_path: Path,
    prompt: str,
    expected_route: WorkItemRoute,
    expected_blocker: str,
) -> None:
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=_database_url(tmp_path),
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=1,
    )
    blocker_codes = {blocker.code for blocker in result.blockers}

    assert result.route == expected_route
    assert "route_not_supported_in_workitem_phase" not in blocker_codes
    if expected_blocker:
        assert expected_blocker in blocker_codes


def test_source_bundle_only_research_blocks_without_attached_sources(tmp_path: Path) -> None:
    prompt = (
        "Research an obscure behavioral health vendor from the provided source bundle only; "
        "say if there is not enough evidence."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=_database_url(tmp_path),
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=1,
    )
    blocker_codes = {blocker.code for blocker in result.blockers}

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert "source_bundle_required" in blocker_codes
    assert not result.artifact_refs


def test_attached_source_bundle_research_blocks_without_attached_sources(tmp_path: Path) -> None:
    prompt = (
        "Do not use live search; summarize only the attached source bundle and cite every "
        "factual claim to a source id."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=_database_url(tmp_path),
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=1,
    )
    blocker_codes = {blocker.code for blocker in result.blockers}

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert "source_bundle_required" in blocker_codes
    assert "manager_loop_review_failed" not in blocker_codes
    assert not result.artifact_refs


def test_hard_filtered_partnership_search_blocks_weak_fixture_matches(tmp_path: Path) -> None:
    prompt = (
        "Find 3 remote US behavioral health AI partnerships from the last 30 days, "
        "exclude staffing agencies, and keep hard filters in scoring."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=_database_url(tmp_path),
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=1,
    )
    blocker_codes = {blocker.code for blocker in result.blockers}

    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert "weak_adjacent_matches" in blocker_codes
    assert not result.artifact_refs


def test_manager_loop_stops_before_repeating_specialist_for_orchestrator_workflow(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = get_test_pack_spec("OR-3").natural_prompt
    manual_plan = {
        "source": "heuristic",
        "requested_agent": "orchestrator",
        "target_agent": "opportunity_scout",
        "intent": "opportunity_search",
        "primary_target": "behavioral health AI clinical research",
        "target_type": "topic",
        "task_objective": "opportunity_discovery",
        "expected_artifact_type": "opportunity_record",
        "side_effect_policy": "draft_or_read_only",
    }

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan,
        ),
        max_steps=5,
    )
    review_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_review"
    ]
    completion_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_completed"
    ]

    assert [event.metadata["route"] for event in review_events] == [
        WorkItemRoute.OPPORTUNITY_SCOUT.value,
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    ]
    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.status == WorkItemStatus.BLOCKED
    assert result.advanced is True
    assert completion_events
    assert "before repeating a specialist" in completion_events[-1].metadata["stop_reason"]
    assert any(
        item["code"] == "manager_loop_crm_write_blocked"
        for item in completion_events[-1].metadata["missing_required_stages"]
    )


def test_orchestrator_plan_missing_draft_is_limited_not_blocked(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    prompt = (
        "orchestrator plan the safest workflow to find 3 behavioral health AI companies, "
        "research the best candidate, and prepare draft-only outreach. Do not save, send, "
        "post elsewhere, or use external writes. State the route, handoff order, blockers, "
        "and what evidence is needed before outreach."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=5,
    )
    completion_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_completed"
    ]
    blocker_codes = {blocker.code for blocker in result.blockers}
    advisory_codes = {
        item["code"] for item in completion_events[-1].metadata["advisory_limitations"]
    }

    assert result.route == WorkItemRoute.ORCHESTRATOR
    assert result.status == WorkItemStatus.DONE
    assert "manager_loop_outreach_not_drafted" not in blocker_codes
    assert "manager_loop_outreach_not_drafted" in advisory_codes
    assert "orchestrator_plan_summary" in {
        artifact.artifact_type for artifact in result.artifact_refs
    }


def test_manager_loop_answers_state_followup_without_rerunning(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    prompt = (
        "orchestrator plan the safest workflow to find 3 behavioral health AI companies, "
        "research the best candidate, and prepare draft-only outreach. Do not save, send, "
        "post elsewhere, or use external writes. State the route, handoff order, blockers, "
        "and what evidence is needed before outreach."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")
    initial = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=5,
    )
    store = SQLiteStore(database_url)
    completed_before = [
        event
        for event in store.list_work_item_events(initial.work_item.id)
        if event.event_type == "manager_loop_completed"
    ]

    followup = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                f"Previous request: {prompt}\n"
                "Follow-up: answer only from the prior run state. Did Orchestrator "
                "only plan, or did it execute specialist work? Explain why the run "
                "label is blocked even though useful candidates were returned, and "
                "list the next safe step without doing more research or drafting outreach. "
                "Also keep track of this run costs."
            ),
            work_item_id=initial.work_item.id,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=5,
    )
    events = store.list_work_item_events(initial.work_item.id)
    completed_after = [event for event in events if event.event_type == "manager_loop_completed"]

    assert followup.route == WorkItemRoute.ORCHESTRATOR
    assert followup.advanced is True
    assert "Orchestrator selected or managed the route" in followup.human_summary
    assert "specialist step(s)" in followup.human_summary
    assert "No new research" in followup.human_summary
    assert "Cost tracking remains backend/audit-only" in followup.human_summary
    assert len(completed_after) == len(completed_before)
    assert any(event.event_type == "manager_loop_state_followup_answered" for event in events)


def test_direct_work_item_advance_answers_state_followup_without_rerouting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = (
        "orchestrator decide the safest workflow to compare Abridge and Eleos Health "
        "as potential Keystone partnership targets. Do not send or draft outreach."
    )
    initial = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=infer_manual_request_plan(
                prompt,
                requested_agent="orchestrator",
            ).model_dump(mode="json"),
        ),
        max_steps=2,
    )
    store = SQLiteStore(database_url)
    events_before = store.list_work_item_events(initial.work_item.id)
    monkeypatch.setattr(workflow_runner, "build_sdk_session", lambda _spec: None)

    def fake_synthesis(result, **_: object):
        class FakeResult:
            output = response_synthesis.UserFacingResponseSynthesis(
                title="Prior run state",
                answer=result.human_summary,
            )
            usage = {}
            cost = None
            request_cache = None

        return FakeResult()

    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        fake_synthesis,
    )

    followup = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                f"{prompt}\n"
                "Follow-up: answer only from the prior run state. Did Orchestrator "
                "execute specialist work or only choose a planning path? List the next "
                "safe step without doing new research or drafting outreach. Also keep "
                "track of this run costs."
            ),
            work_item_id=initial.work_item.id,
            database_url=database_url,
            save=True,
            live_search=True,
            live_sdk=True,
        )
    )
    events_after = store.list_work_item_events(initial.work_item.id)

    assert followup.route == WorkItemRoute.ORCHESTRATOR
    assert followup.advanced is True
    assert "No new research" in followup.human_summary
    assert "Cost tracking remains backend/audit-only" in followup.human_summary
    assert not any(
        event.event_type == "advance_started" for event in events_after[len(events_before) :]
    )
    assert any(
        event.event_type == "manager_loop_state_followup_answered"
        for event in events_after[len(events_before) :]
    )


def test_opportunity_state_followup_includes_filters_sources_and_cost_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_live(**_: object):
        return (
            OpportunityScoutResult(
                topic="strict opportunity scan",
                dry_run=False,
                records=[
                    OpportunityRecord(
                        company_name="Modern Health",
                        opportunity_type="behavioral health AI",
                        role_title="Medical Director (Part-Time)",
                        role_location="Remote, United States",
                        role_remote=True,
                        role_country="United States",
                        priority_score=94,
                        why_now_signal=(
                            "Remote U.S. part-time medical director role focused on "
                            "psychiatry and digital mental health."
                        ),
                        recommended_next_step=("Run company research before any outreach."),
                        keystone_fit_reason=(
                            "Keystone could review clinical evaluation and workflow fit."
                        ),
                        outside_consulting_likelihood=70,
                        handoff_to_business_research_analyst=True,
                        sources=[
                            OpportunitySource(
                                title="Medical Director (Part-Time) at Modern Health",
                                url=(
                                    "https://jobs.behavioralhealthtech.com/jobs/"
                                    "168163270-medical-director-part-time"
                                ),
                                source_type="job_posting",
                                supported_signal=("Remote U.S. part-time medical director role."),
                            )
                        ],
                    )
                ],
            ),
            {
                "debug_notes": ["fake live retrieval"],
                "retrieval_diagnostics": {"provider_summary": "searxng+agents-web-search"},
            },
        )

    monkeypatch.setattr("keystone_agents.workflow_runner.run_opportunity_scout_live", fake_run_live)

    prompt = (
        "opportunity scout find active part-time or fractional remote U.S. chief medical "
        "officer or fractional medical director roles in behavioral health AI posted in the "
        "last 1 week. Use strict criteria: posted or refreshed within the last 1 week, "
        "remote U.S., part-time/fractional/advisory/contract, and behavioral health relevance. "
        "If none are strong matches, do not pad weak results."
    )
    initial = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            live_search=True,
            max_results=1,
            cost_profile="slack_opportunity_balanced",
            manual_request_plan={
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": "fractional medical director roles",
                "constraints": [
                    "posted or refreshed within the last 1 week",
                    "remote U.S.",
                    "part-time/fractional/advisory/contract",
                    "no weak padding",
                ],
                "task_objective": "opportunity_discovery",
            },
        ),
        max_steps=2,
    )

    followup = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                f"Previous request: {prompt}\n"
                "Follow-up: answer only from the prior run state. Which hard filters shaped "
                "the result, did you exclude weak matches instead of padding, what adjacent "
                "matches were retained, what source evidence was available, what cost profile "
                "was used, and what is the next safe step? Also keep track of this run costs."
            ),
            work_item_id=initial.work_item.id,
            database_url=database_url,
            save=True,
            cost_tracking_requested=True,
        ),
        max_steps=2,
    )

    assert followup.route == WorkItemRoute.ORCHESTRATOR
    assert "Hard filters" in followup.human_summary
    assert "posted or refreshed within the last 1 week" in followup.human_summary
    assert "No-padding check" in followup.human_summary
    assert "Retained matches" in followup.human_summary
    assert "Modern Health" in followup.human_summary
    assert "Primary source links" in followup.human_summary
    assert (
        "https://jobs.behavioralhealthtech.com/jobs/168163270-medical-director-part-time"
        in followup.human_summary
    )
    assert "Cost profile: slack_opportunity_balanced" in followup.human_summary
    assert "Retrieval providers: searxng+agents-web-search" in followup.human_summary


def test_formal_opportunity_slack_request_uses_deep_profile_and_repair() -> None:
    request = workflow_runner._normalize_workflow_request_for_context(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout find source-backed grant, RFP, pilot, or "
                "call-for-proposals opportunities with deadline evidence"
            ),
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan={
                "source": "test",
                "requested_agent": "opportunity_scout",
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
            },
        )
    )

    assert request.cost_profile == "slack_opportunity_deep"
    assert request.hosted_web_search_max_calls == 4
    assert request.allow_manager_loop_repair is True
    assert request.include_contact_enrichment is False
    assert request.reuse_existing_research is False


def test_deep_source_backed_opportunity_request_verifies_source_pages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured_kwargs: dict[str, object] = {}

    def fake_run_live(**kwargs: object):
        captured_kwargs.update(kwargs)
        return (
            OpportunityScoutResult(
                topic="behavioral health software companies",
                dry_run=False,
                records=[
                    OpportunityRecord(
                        company_name="Example Behavioral Health",
                        opportunity_type="digital mental health",
                        priority_score=72,
                        why_now_signal=(
                            "Example Behavioral Health announced a measurement-based care "
                            "partnership relevant to clinics."
                        ),
                        recommended_next_step="Review extracted source evidence.",
                        keystone_fit_reason="Relevant to clinic-facing digital psychiatry workflows.",
                        outside_consulting_likelihood=55,
                        handoff_to_business_research_analyst=False,
                        sources=[
                            OpportunitySource(
                                title="Example Behavioral Health partnership",
                                url="https://example.com/behavioral-health-partnership",
                                source_type="news",
                                supported_signal=(
                                    "Measurement-based care partnership for behavioral "
                                    "health clinics."
                                ),
                                evidence_excerpt=(
                                    "The extracted source describes the clinic partnership "
                                    "and measurement-based care deployment."
                                ),
                            )
                        ],
                    ),
                    OpportunityRecord(
                        company_name="Example Digital Psychiatry Grant",
                        opportunity_type="grant or collaboration opportunity",
                        priority_score=68,
                        why_now_signal=(
                            "Example Digital Psychiatry Grant funds implementation "
                            "research for measurement-based digital mental health."
                        ),
                        recommended_next_step="Check eligibility and deadline details.",
                        keystone_fit_reason="Relevant to clinical AI evaluation partnerships.",
                        outside_consulting_likelihood=50,
                        handoff_to_business_research_analyst=False,
                        sources=[
                            OpportunitySource(
                                title="Digital psychiatry funding announcement",
                                url="https://example.gov/digital-psychiatry-funding",
                                source_type="government",
                                supported_signal=(
                                    "Funding supports measurement-based digital mental "
                                    "health implementation research."
                                ),
                                evidence_excerpt=(
                                    "The extracted funding announcement describes partner "
                                    "implementation sites and measurement-based outcomes."
                                ),
                            )
                        ],
                    ),
                ],
            ),
            {
                "debug_notes": ["fake deep source-backed retrieval"],
                "retrieval_diagnostics": {"provider_summary": "searxng+exa+tavily"},
            },
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_opportunity_scout_live",
        fake_run_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout run a deeper search for behavioral health software "
                "companies with measurement-based care tools. Give a source-backed "
                "synthesis with visible source URLs and provider comparison."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
            manual_request_plan={
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": "behavioral health software companies",
                "constraints": [
                    "deeper-search",
                    "source-backed",
                    "visible-source-urls",
                    "provider-diagnostics",
                ],
            },
        )
    )

    assert captured_kwargs["verify_source_pages"] is True
    assert captured_kwargs["max_results"] == 8
    assert captured_kwargs["agents_web_search_max_calls"] == 2
    assert result.artifact_refs
    assert any(
        "quality budget applied for opportunity_scout" in note.lower()
        and "mode=deep" in note.lower()
        and "tool_tier=deep_retrieval" in note.lower()
        for note in result.audit_notes
    )
    source_ref = result.artifact_refs[0].metadata["source_refs"][0]
    assert "extracted source describes" in source_ref["evidence_excerpt"]
    assert source_ref["extraction_status"] == "extracted"
    assert result.human_summary.startswith("Behavioral health clinic software comparison")
    assert "Answer\nThe source-backed match surfaced" in result.human_summary
    assert "Detailed Summary\n" in result.human_summary
    assert "https://example.com/behavioral-health-partnership" in result.human_summary
    assert "Source evidence" in result.human_summary
    assert "Opportunity Scout attached" not in result.human_summary
    assert "Deterministic opportunity source-backed summary rendered." in result.audit_notes
    assert result.artifact_refs[0].metadata["source_context_status"] == {
        "selected_url_count": 1,
        "extracted_url_count": 1,
        "evidence_url_count": 1,
        "snippet_only_url_count": 0,
        "statuses": ["extracted"],
    }


def test_formal_opportunity_gates_filter_adjacent_or_untimed_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)
    captured_kwargs: dict[str, object] = {}

    def record(
        *,
        company_name: str,
        opportunity_type: str,
        source_title: str,
        supported_signal: str,
        url: str,
        evidence_excerpt: str = "",
    ) -> OpportunityRecord:
        return OpportunityRecord(
            company_name=company_name,
            opportunity_type=opportunity_type,
            priority_score=88,
            why_now_signal=supported_signal,
            recommended_next_step="Review eligibility and source evidence before action.",
            keystone_fit_reason="Keystone could assess clinical validation fit.",
            outside_consulting_likelihood=60,
            handoff_to_business_research_analyst=True,
            sources=[
                OpportunitySource(
                    title=source_title,
                    url=url,
                    source_type="government",
                    supported_signal=supported_signal,
                    evidence_excerpt=evidence_excerpt,
                )
            ],
        )

    def fake_run_live(**kwargs: object):
        captured_kwargs.update(kwargs)
        return (
            OpportunityScoutResult(
                topic="formal opportunity scan",
                dry_run=False,
                records=[
                    record(
                        company_name="NIMH",
                        opportunity_type="grant or collaboration opportunity",
                        source_title="NIMH SBIR funding opportunity",
                        supported_signal=(
                            "NIMH SBIR grant applications for small businesses are due "
                            "June 20, 2026."
                        ),
                        url="https://www.nimh.nih.gov/funding/sbir",
                        evidence_excerpt=(
                            "The NIMH SBIR source lists June 20, 2026 as a due date "
                            "for small-business grant applications."
                        ),
                    ),
                    record(
                        company_name="NIMH",
                        opportunity_type="grant or collaboration opportunity",
                        source_title=(
                            "Advancing Learning Health Care Research in Outpatient "
                            "Mental Health Treatment Settings"
                        ),
                        supported_signal=(
                            "NIMH R34 clinical trial optional applications are due "
                            "June 20, 2026 for outpatient mental health research."
                        ),
                        url="https://simpler.grants.gov/opportunity/357327",
                    ),
                    record(
                        company_name="Example Health",
                        opportunity_type="behavioral health AI",
                        source_title="Example Health partnership announcement",
                        supported_signal=(
                            "Example Health announced a behavioral health AI partnership "
                            "in May 2026."
                        ),
                        url="https://example.com/news/partnership",
                    ),
                    record(
                        company_name="Digital Health Fund",
                        opportunity_type="grant or collaboration opportunity",
                        source_title="Digital health grant opportunity",
                        supported_signal=(
                            "Digital health grant opportunity for measurement-based care."
                        ),
                        url="https://example.org/grants/digital-health",
                    ),
                ],
            ),
            {
                "debug_notes": ["fake formal opportunity retrieval"],
                "retrieval_diagnostics": {"provider_summary": "searxng+agents-web-search"},
            },
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_opportunity_scout_live",
        fake_run_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout find up to 3 source-backed grant, RFP, pilot, "
                "or call-for-proposals opportunities related to behavioral health AI. "
                "Include only opportunities with sponsor, deadline or timing signal, "
                "fit rationale, and source URL. Do not pad weak results."
            ),
            database_url=database_url,
            save=True,
            live_search=True,
            max_results=3,
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
        )
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    advance_started = next(event for event in events if event.event_type == "advance_started")
    gate_event = next(
        event for event in events if event.event_type == "opportunity_candidate_gates_applied"
    )

    assert captured_kwargs["agents_web_search_max_calls"] == 4
    assert captured_kwargs["agents_web_search_parallel"] is False
    assert captured_kwargs["verify_source_pages"] is True
    assert advance_started.metadata["cost_profile"] == "slack_opportunity_deep"
    assert advance_started.metadata["allow_manager_loop_repair"] is True
    assert len(result.artifact_refs) == 1
    assert result.artifact_refs[0].title == "NIMH"
    retained_source = result.artifact_refs[0].metadata["source_refs"][0]
    assert "June 20, 2026" in retained_source["evidence_excerpt"]
    assert gate_event.metadata["retained_record_count"] == 1
    assert gate_event.metadata["review_candidate_count"] == 3
    assert gate_event.metadata["gates"] == [
        "formal_opportunity_type_evidence",
        "deadline_or_timing_evidence",
        "source_url",
        "sponsor",
        "keystone_applicability_evidence",
    ]
    assert result.next_action is not None
    assert result.next_action.agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST


def test_formal_opportunity_gates_filter_explicit_non_nofo_topic_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)

    def fake_run_live(**_kwargs: object):
        return (
            OpportunityScoutResult(
                topic="youth mental health AI safety opportunities",
                dry_run=False,
                records=[
                    OpportunityRecord(
                        company_name="School Mental and Behavioral Health",
                        opportunity_type="grant or collaboration opportunity",
                        priority_score=82,
                        why_now_signal=(
                            "Apr 22, 2026. This is not a notice of funding opportunity "
                            "(NOFO). Apply through an appropriate NIH Parent Funding "
                            "Announcement or another broad NIH opportunity."
                        ),
                        recommended_next_step=(
                            "Review broad NIH parent announcements before taking action."
                        ),
                        keystone_fit_reason=(
                            "Keystone could evaluate behavioral health AI safety if a "
                            "concrete eligible opportunity exists."
                        ),
                        outside_consulting_likelihood=40,
                        handoff_to_business_research_analyst=False,
                        sources=[
                            OpportunitySource(
                                title="School Mental and Behavioral Health",
                                url=(
                                    "https://grants.nih.gov/funding/find-a-fit-for-your-"
                                    "research/highlighted-topics/11"
                                ),
                                source_type="government",
                                supported_signal=(
                                    "This is not a notice of funding opportunity (NOFO)."
                                ),
                                evidence_excerpt=(
                                    "This is not a notice of funding opportunity (NOFO). "
                                    "Apply through an appropriate NIH Parent Funding "
                                    "Announcement."
                                ),
                            )
                        ],
                    )
                ],
            ),
            {
                "debug_notes": ["fake highlighted-topic retrieval"],
                "retrieval_diagnostics": {"provider_summary": "searxng+exa+tavily"},
            },
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_opportunity_scout_live",
        fake_run_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout do a deeper read-only search for active or recently "
                "announced grant, pilot, RFP, or partnership opportunities around youth "
                "mental health AI safety where Keystone could plausibly participate or "
                "partner. Do not pad weak results."
            ),
            database_url=database_url,
            save=True,
            live_search=True,
            max_results=3,
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        )
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    gate_event = next(
        event for event in events if event.event_type == "opportunity_candidate_gates_applied"
    )

    assert result.artifact_refs == []
    assert result.next_action is not None
    assert result.next_action.action == "broaden_opportunity_search"
    assert "no strong exact matches" in result.human_summary.lower()
    assert "not a concrete funding/RFP/pilot opportunity" in result.human_summary
    assert gate_event.metadata["retained_record_count"] == 0
    assert gate_event.metadata["review_candidate_count"] == 1


def test_manager_loop_blocks_crm_write_boundary_on_opportunity_request(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = get_test_pack_spec("OS-4").natural_prompt
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=3,
    )
    blocker_codes = {blocker.code for blocker in result.blockers}
    completion_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_completed"
    ]
    missing_codes = {
        item["code"] for item in completion_events[-1].metadata["missing_required_stages"]
    }

    assert "manager_loop_crm_write_blocked" in blocker_codes
    assert "manager_loop_crm_write_blocked" in missing_codes
    assert "No CRM write was performed" in result.blockers[-1].message


def test_orchestrator_test_pack_prompts_enter_safe_specialist_manager_loop(
    tmp_path: Path,
) -> None:
    expected_first_routes = {
        "OR-1": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        "OR-2": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        "OR-3": WorkItemRoute.OPPORTUNITY_SCOUT.value,
        "OR-4": WorkItemRoute.OPPORTUNITY_SCOUT.value,
        "OR-5": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    }

    for spec_id, expected_first_route in expected_first_routes.items():
        database_url = f"sqlite:///{tmp_path / f'{spec_id.lower()}.db'}"
        prompt = (
            get_test_pack_spec(spec_id)
            .natural_prompt.replace("[Company]", "Lindus Health")
            .replace(
                "[CEO, Head of Clinical Operations, Head of Partnerships, or Medical Director]",
                "Head of Partnerships",
            )
        )
        manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

        result = advance_work_item_manager_loop(
            WorkflowRunRequest(
                request_text=prompt,
                database_url=database_url,
                save=True,
                manual_request_plan=manual_plan.model_dump(mode="json"),
            ),
            max_steps=5,
        )
        review_events = [
            event
            for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
            if event.event_type == "manager_loop_review"
        ]

        assert result.advanced is True, spec_id
        if spec_id == "OR-4":
            assert result.status == WorkItemStatus.DONE, spec_id
            assert result.route == WorkItemRoute.ORCHESTRATOR
            assert result.blockers == [], spec_id
            assert result.artifact_refs[0].artifact_type == "orchestrator_plan_summary"
            assert "Assumptions made:" in result.human_summary
            assert "Selected workflow:" in result.human_summary
            assert "Agents used or proposed:" in result.human_summary
            assert "Top findings:" in result.human_summary
            assert "Additional input that would improve the next run:" in result.human_summary
        elif spec_id == "OR-3":
            assert result.status == WorkItemStatus.BLOCKED, spec_id
            assert result.blockers, spec_id
        elif spec_id in {"OR-1", "OR-2", "OR-5"}:
            assert result.status == WorkItemStatus.DONE, spec_id
            assert result.blockers == [], spec_id
        else:
            assert result.status == WorkItemStatus.IN_PROGRESS, spec_id
            assert result.blockers == [], spec_id
        assert review_events, spec_id
        assert review_events[0].metadata["route"] == expected_first_route
        assert review_events[0].metadata["route"] != WorkItemRoute.OUTREACH_COMPOSER.value
        assert all(
            gate.approval_state.value != "approved" for gate in result.work_item.approval_gates
        )


def test_orchestrator_connected_workflow_records_missing_downstream_stage_blockers(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = (
        get_test_pack_spec("OR-5")
        .natural_prompt.replace("[Company]", "Lindus Health")
        .replace(
            "[CEO, Head of Clinical Operations, Head of Partnerships, or Medical Director]",
            "Head of Partnerships",
        )
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=5,
    )
    completion_events = [
        event
        for event in SQLiteStore(database_url).list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_completed"
    ]
    blocker_codes = {blocker.code for blocker in result.blockers}
    missing_codes = {
        item["code"] for item in completion_events[-1].metadata["missing_required_stages"]
    }

    assert result.status == WorkItemStatus.DONE
    assert blocker_codes == set()
    assert missing_codes == set()


def test_find_and_send_workitem_preserves_count_and_send_blocker(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = "Find and send outreach to the best three companies."
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=5,
    )
    store = SQLiteStore(database_url)
    loaded = store.get_work_item(result.work_item.id)
    completion_events = [
        event
        for event in store.list_work_item_events(result.work_item.id)
        if event.event_type == "manager_loop_completed"
    ]
    blocker_codes = {blocker.code for blocker in result.blockers}
    missing_codes = {
        item["code"] for item in completion_events[-1].metadata["missing_required_stages"]
    }

    assert manual_plan.desired_count == 3
    assert loaded is not None
    assert loaded.target.metadata["manual_desired_count"] == 3
    assert result.status == WorkItemStatus.BLOCKED
    assert "manager_loop_send_blocked" in blocker_codes
    assert "manager_loop_outreach_not_drafted" in blocker_codes
    assert {"manager_loop_send_blocked", "manager_loop_outreach_not_drafted"} <= missing_codes


def test_gmail_workitem_route_blocks_for_context_without_unsupported_route(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = (
        "A Gmail consulting inquiry came in. Triage it, research the company, "
        "create an opportunity record, and draft a response only after approval."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=5,
    )
    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    blocker_codes = {blocker.code for blocker in result.blockers}
    completion_events = [event for event in events if event.event_type == "manager_loop_completed"]
    missing_codes = {
        item["code"] for item in completion_events[-1].metadata["missing_required_stages"]
    }

    assert manual_plan.target_agent == "gmail_triage"
    assert result.route == WorkItemRoute.GMAIL_TRIAGE
    assert result.status == WorkItemStatus.BLOCKED
    assert "gmail_context_required" in blocker_codes
    assert "route_not_supported_in_workitem_phase" not in blocker_codes
    assert {
        "manager_loop_research_not_completed",
        "manager_loop_opportunity_not_created",
        "manager_loop_outreach_not_drafted",
    } <= blocker_codes
    assert missing_codes <= blocker_codes
    assert result.work_item.target.metadata["gmail_execution_plan"]["operation"] in {
        "draft_reply",
        "single_message_triage",
        "priority_grouping",
    }
    assert completion_events


def test_gmail_workitem_inline_email_completes_read_only_triage_without_gmail_writes(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = (
        "gmail triage classify this sanitized inbound email as read-only triage only. "
        "Email: From: Jordan Lee, Operations at Mindful Care. "
        "Subject: Follow-up on measurement support. "
        "Body: Hi Anup, our team is reviewing measurement-based care workflows and "
        "may need advisory help on evaluation design. Could you let me know if this "
        "is relevant for Keystone? Return priority, reply-needed yes/no, action owner, "
        "and risks. Do not create a Gmail draft, apply labels, send, save, post elsewhere, "
        "or use external writes. Also keep track of this run costs."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=3,
    )
    store = SQLiteStore(database_url)
    events = store.list_work_item_events(result.work_item.id)
    advance_started = next(event for event in events if event.event_type == "advance_started")
    artifact_events = [event for event in events if event.event_type == "artifact_attached"]

    assert result.route == WorkItemRoute.GMAIL_TRIAGE
    assert result.status == WorkItemStatus.DONE
    assert result.advanced is True
    assert not result.blockers
    assert result.artifact_refs[0].artifact_type == "gmail_triage_report"
    assert result.artifact_refs[0].metadata["draft_created"] is False
    assert result.artifact_refs[0].metadata["labels_modified"] is False
    assert result.artifact_refs[0].metadata["send_enabled"] is False
    assert "risk_flags" in result.artifact_refs[0].metadata
    assert "No Gmail draft, label, send" in result.human_summary
    assert result.next_action is not None
    assert result.next_action.action == "review_gmail_triage"
    assert advance_started.metadata["cost_profile"] == "slack_context_light"
    assert advance_started.metadata["hosted_web_search_max_calls"] == 0
    assert artifact_events
    gate_event = next(
        event for event in events if event.event_type == "skill_contract_gates_checked"
    )
    gate = gate_event.metadata["gates"][0]
    assert gate["gate_id"] == "gmail_sensitive_message_gate"
    assert gate["status"] == "passed"
    assert gate["evidence"]["unsafe_flags"] == []


@pytest.mark.parametrize("spec_id", ["GT-1", "GT-2", "GT-3", "GT-4"])
def test_gmail_only_workitems_do_not_get_false_downstream_stage_blockers(
    tmp_path: Path,
    spec_id: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'{spec_id.lower()}.db'}"
    prompt = get_test_pack_spec(spec_id).natural_prompt
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=3,
    )
    blocker_codes = {blocker.code for blocker in result.blockers}

    assert result.route == WorkItemRoute.GMAIL_TRIAGE
    assert "gmail_context_required" in blocker_codes
    assert "manager_loop_research_not_completed" not in blocker_codes
    assert "manager_loop_opportunity_not_created" not in blocker_codes
    assert "manager_loop_outreach_not_drafted" not in blocker_codes


def test_outreach_variants_from_existing_research_brief_do_not_request_new_research(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = get_test_pack_spec("OC-2").natural_prompt
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=3,
    )
    blocker_codes = {blocker.code for blocker in result.blockers}

    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert "outreach_requires_approved_context" in blocker_codes
    assert "manager_loop_research_not_completed" not in blocker_codes


def test_company_comparison_workitem_creates_comparison_not_fake_profile(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    prompt = get_test_pack_spec("BR-3").natural_prompt
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=prompt,
            database_url=database_url,
            save=True,
            manual_request_plan=manual_plan.model_dump(mode="json"),
        ),
        max_steps=3,
    )

    assert manual_plan.target_agent == "business_research_analyst"
    assert manual_plan.primary_target == "Lindus Health vs Holmusk"
    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.advanced is True
    assert [artifact.artifact_type for artifact in result.artifact_refs] == ["company_comparison"]
    assert result.artifact_refs[0].title == "Lindus Health vs Holmusk"
    assert result.work_item.target.name == "Lindus Health vs Holmusk"
    assert "Compare Lindus Health and Holmusk" not in result.work_item.target.name


def test_manager_loop_reviews_chief_of_staff_runs(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="chief of staff summarize open Slack follow-ups",
            database_url=database_url,
            save=True,
            manual_request_plan={
                "source": "heuristic",
                "target_agent": "chief_of_staff",
                "intent": "slack_operations",
                "primary_target": "open Slack follow-ups",
            },
        ),
        max_steps=2,
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)
    review_events = [event for event in events if event.event_type == "manager_loop_review"]

    assert result.route == WorkItemRoute.CHIEF_OF_STAFF
    assert result.artifact_refs[0].artifact_type == "chief_of_staff_plan"
    assert review_events
    assert review_events[0].metadata["route"] == WorkItemRoute.CHIEF_OF_STAFF.value
    assert "Manager loop review" in " ".join(result.work_item.audit_notes)
    gate_event = next(
        event for event in events if event.event_type == "skill_contract_gates_checked"
    )
    gate = gate_event.metadata["gates"][0]
    assert gate["gate_id"] == "chief_artifact_publish_gate"
    assert gate["status"] == "passed"


def test_advance_work_item_context_pack_includes_approved_memory_without_live_mode(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_memory_item(
        MemoryItem(
            memory_type="company_fact",
            object_type="company",
            object_id="NeuroFlow",
            object_key="NeuroFlow",
            title="NeuroFlow approved memory",
            summary="Previously approved NeuroFlow fact.",
            content={"claim_text": "NeuroFlow has prior approved local memory."},
            source_ids=["fixture:memory"],
            approval_state=ApprovalState.APPROVED_FOR_RESEARCH,
            confidence=0.8,
        )
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
            live_search=False,
            live_sdk=False,
        )
    )

    assert result.context_pack is not None
    assert result.context_pack["approved_company_facts"][0]["title"] == "NeuroFlow approved memory"
    assert result.context_pack["approved_company_facts"][0]["source_ids"] == ["fixture:memory"]
    assert result.context_pack["readiness_gates"][0]["ready"] is True


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
                "summarize the Zotero collection 'LH 01 - REACH-tDCS & Lindus Trial Context'"
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
        assert "Orchestrator memo for this specialist WorkItem run" in prompt
        assert "Ask the Business Research Analyst to find and summarize" in prompt
        assert '"selected_agent": "business_research_analyst"' in prompt
        assert "Source ID: zotero:item:AP9SKRPZ" in prompt
        assert "Source ID: web_search:1" in prompt
        assert "randomized pivotal trial" in prompt
        assert kwargs["live"] is True
        assert kwargs["tool_tier"] == "deep_retrieval"
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
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "Lindus SOOMA trial",
            },
            orchestrator_preflight={
                "request_text": (
                    "Ask the Business Research Analyst to find and summarize the Zotero "
                    "article on the Lindus SOOMA trial."
                ),
                "advisory_only": True,
                "selected_agent": "business_research_analyst",
                "manual_request_plan": {
                    "source": "llm",
                    "requested_agent": "business_research_analyst",
                    "target_agent": "business_research_analyst",
                    "intent": "company_research",
                    "primary_target": "Lindus SOOMA trial",
                },
                "route_result": {
                    "route": "business_research_analyst",
                    "routing_mode": "deterministic",
                    "rationale": "Explicit analyst request.",
                    "refused": False,
                    "send_enabled": False,
                },
            },
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
    assert result.work_item.sources
    assert result.artifact_refs[0].metadata["source_refs"]
    assert result.artifact_refs[0].metadata["source_refs"][0]["url"]
    assert result.artifact_refs[0].metadata["source_context_status"]["selected_url_count"] >= 1
    assert result.artifact_refs[0].metadata["source_context_status"]["extracted_url_count"] == 0
    assert result.artifact_refs[0].metadata["source_refs"][0]["extraction_status"] == "snippet_only"
    assert result.next_action is not None
    assert result.next_action.agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST


def test_work_item_cost_tracking_directive_is_recorded_and_removed_from_task(
    tmp_path: Path,
) -> None:
    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout find behavioral health AI companies. "
                "Also keep track of this run costs."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            max_results=1,
        )
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    advance_started = next(event for event in events if event.event_type == "advance_started")

    assert advance_started.metadata["cost_tracking_requested"] is True
    assert "keep track" not in result.work_item.request_text.lower()
    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT


def test_strict_opportunity_no_match_is_limited_done_not_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_live(**_: object):
        return (
            OpportunityScoutResult(
                topic="strict role search",
                dry_run=False,
                filtered_candidates=[
                    FilteredOpportunityCandidate(
                        company_name="Medical Science Liaison, Neuropsychiatry (NYC)",
                        source_title="Medical Science Liaison, Neuropsychiatry (NYC) | LinkedIn",
                        source_url="https://www.linkedin.com/jobs/view/msl",
                        reasons=[
                            "requested remote status was not verified",
                            "requested U.S. location or eligibility was not verified",
                            "source lacks requested role-title evidence: chief medical officer",
                        ],
                    )
                ],
                constraint_relaxation_suggestion=(
                    "Relax role title before relaxing remote/U.S. verification."
                ),
            ),
            {"debug_notes": ["fake live retrieval"]},
        )

    monkeypatch.setattr("keystone_agents.workflow_runner.run_opportunity_scout_live", fake_run_live)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout find active part-time or fractional remote U.S. "
                "chief medical officer or clinical advisor roles in behavioral health AI "
                "posted in the last 1 week. If none are strong matches, do not pad weak results."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
        )
    )

    assert result.advanced is True
    assert result.status == WorkItemStatus.DONE
    assert result.blockers == []
    assert not result.artifact_refs
    assert "no strong exact matches" in result.human_summary.lower()
    assert "Adjacent but not exact matches" in result.human_summary
    assert "Relax role title" in result.human_summary


def test_strict_opportunity_no_match_handles_posted_or_refreshed_one_week_phrase(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_live(**_: object):
        return (
            OpportunityScoutResult(
                topic=(
                    "active part-time or fractional remote U.S. chief medical officer or "
                    "fractional medical director roles posted or refreshed in the last 1 week"
                ),
                dry_run=False,
                constraint_relaxation_suggestion=(
                    "Relax recency from the last 1 week to the last 30 days."
                ),
            ),
            {"debug_notes": ["fake live retrieval"]},
        )

    monkeypatch.setattr("keystone_agents.workflow_runner.run_opportunity_scout_live", fake_run_live)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout find active part-time or fractional remote U.S. "
                "chief medical officer or fractional medical director roles in behavioral health AI "
                "posted in the last 1 week. Use strict criteria: posted or refreshed within the "
                "last 1 week, remote U.S., part-time/fractional/advisory/contract, and behavioral "
                "health/psychiatry/mental health/AI/digital health relevance. If none are strong "
                "matches, do not pad weak results."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
        )
    )

    assert result.advanced is True
    assert result.status == WorkItemStatus.DONE
    assert result.blockers == []
    assert "no strong exact matches" in result.human_summary.lower()
    assert "last 30 days" in result.human_summary


def test_manager_loop_keeps_strict_opportunity_no_match_advisory_not_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_live(**_: object):
        return (
            OpportunityScoutResult(
                topic=(
                    "active part-time or fractional remote U.S. chief medical officer or "
                    "fractional medical director roles posted or refreshed in the last 1 week"
                ),
                dry_run=False,
                filtered_candidates=[
                    FilteredOpportunityCandidate(
                        company_name="Generic behavioral health company",
                        source_title="Behavioral Health Medical Director | LinkedIn",
                        source_url="https://www.linkedin.com/jobs/view/generic",
                        reasons=[
                            "source lacks requested part-time, fractional, advisory, or contract evidence",
                            "requested remote status was not verified",
                        ],
                    )
                ],
                constraint_relaxation_suggestion=(
                    "Relax recency from the last 1 week to the last 30 days."
                ),
            ),
            {"debug_notes": ["fake live retrieval"]},
        )

    class FakeReview:
        status = "fail"
        overall_score = 35
        approval_boundary_ok = True
        observed_gaps = ["Include scored records with sources and recommended next steps."]
        recommended_next_step = "Address observed gaps, then rerun the specialist review."

    monkeypatch.setattr("keystone_agents.workflow_runner.run_opportunity_scout_live", fake_run_live)
    monkeypatch.setattr(workflow_runner, "review_specialist_output", lambda **_kwargs: FakeReview())

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout find active part-time or fractional remote U.S. "
                "chief medical officer or fractional medical director roles in behavioral health AI "
                "posted in the last 1 week. Use strict criteria: posted or refreshed within the "
                "last 1 week, remote U.S., part-time/fractional/advisory/contract, and behavioral "
                "health/psychiatry/mental health/AI/digital health relevance. If none are strong "
                "matches, do not pad weak results; list adjacent matches separately and say which "
                "constraint to relax."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
        ),
        max_steps=2,
    )

    assert result.status == WorkItemStatus.DONE
    assert result.blockers == []
    assert "no strong exact matches" in result.human_summary.lower()

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    review_event = next(event for event in events if event.event_type == "manager_loop_review")
    assert review_event.metadata["review_decision"] == "warn"
    assert review_event.metadata["advisory"] is True

    completed_event = next(
        event for event in events if event.event_type == "manager_loop_completed"
    )
    assert completed_event.metadata["missing_required_stages"] == []


def test_manager_loop_broadens_natural_opportunity_no_match_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    retrieval_hints: list[object] = []

    def fake_run_live(**kwargs: object):
        calls.append(str(kwargs.get("topic") or ""))
        retrieval_hints.append(kwargs.get("retrieval_hint"))
        if len(calls) == 1:
            return (
                OpportunityScoutResult(
                    topic="AI-enabled behavioral health opportunities",
                    dry_run=False,
                    constraint_relaxation_suggestion=(
                        "Broaden from exact RFP/grant wording to pilots, partner programs, "
                        "and recently announced implementation opportunities."
                    ),
                ),
                {"debug_notes": ["fake empty first pass"]},
            )
        return (
            OpportunityScoutResult(
                topic="AI-enabled behavioral health opportunities",
                dry_run=False,
                records=[
                    OpportunityRecord(
                        company_name="Behavioral Health AI Pilot Program",
                        opportunity_type="contract or RFP opportunity",
                        priority_score=89,
                        why_now_signal=(
                            "Recently announced behavioral health AI pilot with "
                            "implementation partner participation."
                        ),
                        recommended_next_step="Review eligibility and sponsor fit.",
                        keystone_fit_reason=(
                            "Keystone could plausibly support clinical AI evaluation "
                            "and measurement-based care implementation."
                        ),
                        outside_consulting_likelihood=70,
                        handoff_to_business_research_analyst=False,
                        sources=[
                            OpportunitySource(
                                title="Behavioral Health AI Pilot Notice",
                                url="https://example.gov/behavioral-health-ai-pilot",
                                source_type="government",
                                supported_signal=(
                                    "The notice describes a behavioral health AI pilot "
                                    "and invites implementation partners."
                                ),
                                evidence_excerpt=(
                                    "Pilot notice for AI-enabled behavioral health "
                                    "implementation, measurement-based care evaluation, "
                                    "and partner participation."
                                ),
                            )
                        ],
                    )
                ],
            ),
            {
                "debug_notes": ["fake broadened second pass"],
                "retrieval_diagnostics": {"provider_summary": "searxng+exa+tavily"},
            },
        )

    class FakeReview:
        status = "pass"
        overall_score = 90
        approval_boundary_ok = True
        observed_gaps: list[str] = []
        recommended_next_step = "Ready for review."

    monkeypatch.setattr("keystone_agents.workflow_runner.run_opportunity_scout_live", fake_run_live)
    monkeypatch.setattr(workflow_runner, "review_specialist_output", lambda **_kwargs: FakeReview())

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout do a deeper read-only search for active or recently "
                "announced pilot, RFP, or grant opportunities around AI-enabled behavioral "
                "health, measurement-based care, or digital psychiatry where Keystone could "
                "plausibly participate or partner."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            max_results=1,
            manual_request_plan={
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": "AI-enabled behavioral health opportunities",
                "constraints": ["pilot", "RFP", "grant", "recent"],
                "task_objective": "opportunity_discovery",
            },
        ),
        max_steps=2,
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    review_events = [event for event in events if event.event_type == "manager_loop_review"]

    assert calls == [
        "AI-enabled behavioral health opportunities",
        "AI-enabled behavioral health opportunities",
    ]
    assert retrieval_hints[0] is None
    assert retrieval_hints[1] is not None
    assert retrieval_hints[1].needs_precision_search is True
    assert retrieval_hints[1].needs_search_review is True
    assert result.status == WorkItemStatus.DONE
    assert result.artifact_refs
    assert result.artifact_refs[0].title == "Behavioral Health AI Pilot Program"
    assert "Behavioral health opportunity comparison" in result.human_summary
    assert "Behavioral Health AI Pilot Program" in result.human_summary
    assert "https://example.gov/behavioral-health-ai-pilot" in result.human_summary
    assert review_events[0].metadata["review_decision"] == "repair"
    assert review_events[0].metadata["observed_gaps"] == [
        (
            "Opportunity Scout found no retained source-backed opportunities for "
            "this broad/deep search; broaden or deepen retrieval before finalizing "
            "the opportunity scan."
        )
    ]
    assert review_events[-1].metadata["review_decision"] == "pass"
    repair_started = next(
        event for event in events if event.event_type == "manager_loop_repair_started"
    )
    assert repair_started.metadata["search_repair_hint"] == (
        "broaden_or_deepen_search_within_cost_profile"
    )


def test_work_item_records_orchestrator_preflight_sdk_usage(
    tmp_path: Path,
) -> None:
    preflight = {
        "selected_agent": "business_research_analyst",
        "sdk_usage_events": [
            {
                "agent_name": "manual_request_planner",
                "run_stage": "orchestrator_preflight.manual_request_planner",
                "usage": {
                    "requests": 1,
                    "input_tokens": 1000,
                    "cached_input_tokens": 250,
                    "output_tokens": 100,
                    "reasoning_output_tokens": 25,
                    "total_tokens": 1100,
                    "cache_hit_rate": 0.25,
                    "prompt_cache_key_present": True,
                    "prompt_cache_key_hash": "preflight-key",
                },
                "cost": {
                    "estimated_usd": 0.004,
                    "pricing_model": "gpt-5.4-mini",
                    "source": "local_pricing_table",
                },
                "request_cache": {
                    "static_prefix_sha256": "preflight-static",
                    "dynamic_prompt_sha256": "preflight-dynamic",
                    "dynamic_prompt_chars": 500,
                },
            }
        ],
    }

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="business research analyst research Big Health",
            database_url=_database_url(tmp_path),
            save=True,
            orchestrator_preflight=preflight,
        )
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    sdk_event = next(event for event in events if event.event_type == "workflow_sdk_usage")

    assert sdk_event.metadata["agent_name"] == "manual_request_planner"
    assert sdk_event.metadata["run_stage"] == "orchestrator_preflight.manual_request_planner"
    assert sdk_event.metadata["usage"]["cache_hit_rate"] == 0.25
    assert sdk_event.metadata["cost"]["estimated_usd"] == 0.004
    assert sdk_event.metadata["request_cache"]["static_prefix_sha256"] == "preflight-static"


def test_state_followup_records_orchestrator_preflight_sdk_usage(
    tmp_path: Path,
) -> None:
    initial = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="opportunity scout find behavioral health AI companies",
            database_url=_database_url(tmp_path),
            save=True,
            max_results=1,
        )
    )
    preflight = {
        "selected_agent": "opportunity_scout",
        "sdk_usage_events": [
            {
                "agent_name": "manual_request_planner",
                "run_stage": "orchestrator_preflight.manual_request_planner",
                "usage": {
                    "requests": 1,
                    "input_tokens": 500,
                    "cached_input_tokens": 400,
                    "output_tokens": 50,
                    "total_tokens": 550,
                    "cache_hit_rate": 0.8,
                    "prompt_cache_key_hash": "followup-key",
                },
                "cost": {"estimated_usd": 0.001},
                "request_cache": {
                    "static_prefix_sha256": "followup-static",
                    "dynamic_prompt_sha256": "followup-dynamic",
                },
            }
        ],
    }

    followup = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                "Prior request: opportunity scout find behavioral health AI companies\n"
                "Follow-up: answer only from the prior run state. What happened?"
            ),
            work_item_id=initial.work_item.id,
            database_url=_database_url(tmp_path),
            save=True,
            orchestrator_preflight=preflight,
            cost_tracking_requested=True,
        )
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(followup.work_item.id)
    sdk_events = [event for event in events if event.event_type == "workflow_sdk_usage"]

    assert len(sdk_events) == 1
    assert sdk_events[0].metadata["usage"]["cache_hit_rate"] == 0.8
    assert "existing WorkItem state" in followup.audit_notes[0]


def test_live_sdk_opportunity_work_item_uses_named_agent_search_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    fake_plan = object()

    def fake_resolve_plan(
        topic: str | None,
        *,
        desired_count: int = 5,
        live: bool = False,
        cost_callback=None,
        **_: object,
    ) -> object:
        captured["planned_topic"] = topic
        captured["desired_count"] = desired_count
        captured["planner_live"] = live
        if cost_callback is not None:
            cost_callback(
                type(
                    "FakePlannerSDKResult",
                    (),
                    {
                        "usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 500,
                            "output_tokens": 100,
                            "cache_hit_rate": 0.5,
                        },
                        "cost": {
                            "estimated_usd": 0.01,
                            "pricing_provider": "openai",
                            "pricing_model": "gpt-5.4-mini",
                        },
                        "request_cache": {
                            "static_prefix_sha256": "planner-static",
                            "dynamic_prompt_chars": 500,
                        },
                    },
                )()
            )
        return fake_plan

    def fake_run_live(
        *,
        topic: str | None,
        max_results: int = 5,
        search_plan: object | None = None,
        **_: object,
    ):
        captured["retrieval_topic"] = topic
        captured["search_plan"] = search_plan
        return (
            scout_opportunities_fixture(topic=topic, max_results=max_results),
            {
                "debug_notes": ["fake live retrieval"],
                "search_provider": "searxng",
                "search_queries": ["behavioral health AI opportunities"],
                "raw_search_result_count": 2,
                "provider_usage": {
                    "searxng": {
                        "requests_attempted": 1,
                        "requests_succeeded": 1,
                        "raw_result_count": 2,
                    }
                },
                "retrieval_diagnostics": {
                    "provider_summary": "searxng",
                    "retrieval_ladder": [{"rung": "search_discovery", "raw_result_count": 2}],
                },
            },
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.resolve_opportunity_search_plan",
        fake_resolve_plan,
    )
    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_opportunity_scout_live",
        fake_run_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="find 2 behavioral health AI opportunities",
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
            max_results=2,
        )
    )

    assert result.advanced is True
    assert captured["planned_topic"] == captured["retrieval_topic"]
    assert captured["desired_count"] == 2
    assert captured["planner_live"] is True
    assert captured["search_plan"] is fake_plan
    assert "named-agent live search planning path" in " ".join(result.audit_notes)
    assert (
        result.artifact_refs[0].metadata["retrieval_diagnostics"]["provider_summary"] == "searxng"
    )
    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    retrieval_event = next(
        event for event in events if event.event_type == "workflow_retrieval_usage"
    )
    planner_event = next(
        event
        for event in events
        if event.event_type == "workflow_sdk_usage"
        and event.metadata["agent_name"] == "opportunity_search_planner"
    )
    assert retrieval_event.actor == "opportunity_scout"
    assert retrieval_event.metadata["agent_name"] == "opportunity_scout"
    assert retrieval_event.metadata["query_count"] == 1
    assert retrieval_event.metadata["aggregate_usage"]["requests_succeeded"] == 1
    assert planner_event.metadata["usage"]["input_tokens"] == 1000


def test_live_sdk_opportunity_work_item_prefers_manual_primary_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_resolve_plan(
        topic: str | None,
        *,
        desired_count: int = 5,
        live: bool = False,
        **_: object,
    ) -> None:
        captured["planned_topic"] = topic
        captured["desired_count"] = desired_count
        captured["planner_live"] = live
        captured["planner_context"] = _.get("planner_context")
        return None

    def fake_run_live(
        *,
        topic: str | None,
        max_results: int = 5,
        search_plan: object | None = None,
        **_: object,
    ):
        captured["retrieval_topic"] = topic
        captured["search_plan"] = search_plan
        return (
            scout_opportunities_fixture(topic=topic, max_results=max_results),
            {"debug_notes": ["fake live retrieval"]},
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.resolve_opportunity_search_plan",
        fake_resolve_plan,
    )
    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_opportunity_scout_live",
        fake_run_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "opportunity scout find 3 active behavioral health AI partnership "
                "or advisory opportunities relevant to Keystone. Use live SDK and live search. "
                "No outreach, no Gmail, no external writes. Test candidate-specific buttons."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
            max_results=3,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "opportunity_scout",
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": (
                    "behavioral health AI partnership or advisory opportunities relevant "
                    "to Keystone"
                ),
                "desired_count": 3,
            },
        )
    )

    assert result.advanced is True
    assert captured["planned_topic"] == (
        "behavioral health AI partnership or advisory opportunities relevant to Keystone"
    )
    assert captured["retrieval_topic"] == captured["planned_topic"]
    assert captured["desired_count"] == 3
    assert captured["planner_live"] is True
    assert "Orchestrator memo for this specialist WorkItem run" in captured["planner_context"]
    assert '"target_agent": "opportunity_scout"' in captured["planner_context"]


def test_opportunity_source_summary_request_creates_source_summary_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_live(
        *,
        topic: str | None,
        max_results: int = 5,
        search_plan: object | None = None,
        **_: object,
    ):
        return (
            scout_opportunities_fixture(topic=topic, max_results=max_results),
            {
                "debug_notes": ["fake live retrieval"],
                "retrieved_source_candidates": [
                    {
                        "source_id": "retrieval:1",
                        "title": "APA 2026 Annual Meeting in San Francisco highlights",
                        "url": "https://example.org/apa-2026-san-francisco",
                        "snippet": (
                            "American Psychiatric Association 2026 San Francisco meeting "
                            "summary with program highlights."
                        ),
                        "source": "fixture",
                    }
                ],
            },
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.resolve_opportunity_search_plan",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_opportunity_scout_live",
        fake_run_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="opportunity scout find summaries of APA 2026 meeting in San Francisco",
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "opportunity_scout",
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": "APA 2026 meeting in San Francisco",
                "target_type": "conference",
                "task_objective": "source_research",
                "expected_artifact_type": "source_summary",
                "required_terms": ["APA", "2026", "San Francisco"],
            },
        )
    )

    assert result.advanced is True
    assert [artifact.artifact_type for artifact in result.artifact_refs] == ["source_summary"]
    assert "not opportunity records" in result.human_summary
    assert result.work_item.sources


def test_opportunity_source_summary_request_uses_fixture_candidate_in_dry_run(
    tmp_path: Path,
) -> None:
    result = advance_work_item(
        WorkflowRunRequest(
            request_text="opportunity scout find summaries of APA 2026 meeting in San Francisco",
            database_url=_database_url(tmp_path),
            save=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "opportunity_scout",
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": "APA 2026 meeting in San Francisco",
                "target_type": "conference",
                "task_objective": "source_research",
                "expected_artifact_type": "source_summary",
                "required_terms": ["APA", "2026", "San Francisco"],
            },
        )
    )

    assert result.advanced is True
    assert [artifact.artifact_type for artifact in result.artifact_refs] == ["source_summary"]
    assert "not live source-backed evidence" in result.human_summary
    assert result.work_item.sources
    assert result.work_item.sources[0].provider == "fixture"
    assert result.work_item.sources[0].extraction_status == "fixture_fallback"
    assert any("Fixture source-summary candidate generated" in note for note in result.audit_notes)


def test_opportunity_source_summary_request_blocks_when_required_terms_do_not_match(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_live(
        *,
        topic: str | None,
        max_results: int = 5,
        search_plan: object | None = None,
        **_: object,
    ):
        return (
            scout_opportunities_fixture(topic=topic, max_results=max_results),
            {
                "debug_notes": ["fake live retrieval"],
                "retrieved_source_candidates": [
                    {
                        "source_id": "retrieval:1",
                        "title": "AI Mental Health Safety Workshop",
                        "url": "https://example.org/ai-workshop",
                        "snippet": "Workshop on generative AI chatbots and mental health.",
                    }
                ],
            },
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.resolve_opportunity_search_plan",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_opportunity_scout_live",
        fake_run_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="opportunity scout find summaries of APA 2026 meeting in San Francisco",
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "opportunity_scout",
                "target_agent": "opportunity_scout",
                "intent": "opportunity_search",
                "primary_target": "APA 2026 meeting in San Francisco",
                "target_type": "conference",
                "task_objective": "source_research",
                "expected_artifact_type": "source_summary",
                "required_terms": ["APA", "2026", "San Francisco"],
            },
        )
    )

    assert result.advanced is False
    assert result.status == WorkItemStatus.BLOCKED
    assert result.blockers[0].code == "source_summary_target_not_found"
    assert not result.artifact_refs


def test_business_research_work_item_prefers_manual_primary_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_retrieve_company_profile_live(*, company: str, **_: object):
        captured["company"] = company
        return research_company_fixture(company_name=company), {
            "debug_notes": ["fake live retrieval"]
        }

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "business research analyst research Big Health. Use live SDK and live search. "
                "No outreach, no Gmail, no external writes."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "Big Health",
                "desired_count": 1,
            },
        )
    )

    assert result.advanced is True
    assert captured["company"] == "Big Health"


def test_current_year_business_research_deepens_initial_query_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_retrieve_company_profile_live(*, company: str, **kwargs: object):
        query_builder = kwargs.get("query_builder")
        assert callable(query_builder)
        captured["company"] = company
        captured["max_results"] = kwargs.get("max_results")
        captured["queries"] = query_builder(company, None)
        return research_company_fixture(company_name=company), {
            "debug_notes": ["fake live retrieval"]
        }

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="business research analyst Summarize what OpenEvidence is doing in 2026",
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "OpenEvidence",
            },
        )
    )

    query_text = "\n".join(captured["queries"]).lower()
    assert result.advanced is True
    assert captured["company"] == "OpenEvidence"
    assert captured["max_results"] >= 8
    assert "openevidence 2026 company update" in query_text
    assert "funding valuation revenue growth" in query_text
    assert "partnership customers product roadmap" in query_text
    assert "current-activity query deepening" in " ".join(result.audit_notes).lower()


def test_deeper_business_research_uses_deep_quality_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_retrieve_company_profile_live(*, company: str, **kwargs: object):
        captured["company"] = company
        captured["max_results"] = kwargs.get("max_results")
        captured["agents_web_search_max_calls"] = kwargs.get("agents_web_search_max_calls")
        return research_company_fixture(company_name=company), {
            "debug_notes": ["fake deep retrieval"]
        }

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "business research analyst do a deeper source-backed search on OpenAI "
                "mental health work. Please synthesize the source data, include visible "
                "source URLs, and compare what the deeper lanes add."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "OpenAI",
            },
        )
    )

    assert result.advanced is True
    assert captured["company"] == "OpenAI"
    assert captured["max_results"] == 8
    assert captured["agents_web_search_max_calls"] == 2
    audit_text = " ".join(result.audit_notes).lower()
    assert "quality budget applied for business_research_analyst" in audit_text
    assert "mode=deep" in audit_text
    assert "tool_tier=deep_retrieval" in audit_text


def test_current_year_business_research_focuses_latest_followup_query_terms(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_retrieve_company_profile_live(*, company: str, **kwargs: object):
        query_builder = kwargs.get("query_builder")
        assert callable(query_builder)
        captured["queries"] = query_builder(company, None)
        return research_company_fixture(company_name=company), {
            "debug_notes": ["fake live retrieval"]
        }

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "business research analyst research OpenEvidence in 2026.\n"
                "Follow-up: can u look at recent partnership details between "
                "OpenEvidence and other companies/journals in 2026?"
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "OpenEvidence",
            },
        )
    )

    query_text = "\n".join(captured["queries"]).lower()
    assert result.advanced is True
    assert "openevidence 2026 partnership company journal" in query_text
    assert "openevidence partnership company journal independent coverage 2026" in query_text


def test_manager_loop_warns_current_research_with_only_company_controlled_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_retrieve_company_profile_live(*, company: str, **_: object):
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    SourceRecord(
                        source_id="company:about",
                        title="About OpenEvidence",
                        url="https://www.openevidence.com/",
                        source_type="company_site",
                        supported_claims=["OpenEvidence describes its medical AI product."],
                        confidence=0.9,
                    ),
                    SourceRecord(
                        source_id="fixture:openevidence",
                        title="Fixture record for OpenEvidence",
                        url="fixture://input",
                        source_type="fixture",
                        supported_claims=["Fixture input identifies the company."],
                        confidence=0.7,
                    ),
                ]
            }
        )
        return profile, {"debug_notes": ["fake shallow retrieval"]}

    class FakeReview:
        status = "pass"
        overall_score = 88
        approval_boundary_ok = True
        observed_gaps: list[str] = []
        recommended_next_step = "Looks acceptable."

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )
    monkeypatch.setattr(workflow_runner, "review_specialist_output", lambda **_kwargs: FakeReview())

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="business research analyst Summarize what OpenEvidence is doing in 2026",
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "OpenEvidence",
            },
        ),
        max_steps=2,
    )

    assert result.status == WorkItemStatus.BLOCKED
    assert any(blocker.code == "manager_loop_review_failed" for blocker in result.blockers)
    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    review_events = [event for event in events if event.event_type == "manager_loop_review"]
    review_event = review_events[-1]
    assert review_event.metadata["review_decision"] == "block"
    assert review_event.metadata["blocking"] is True
    assert any(
        "Limited independent evidence" in gap for gap in review_event.metadata["observed_gaps"]
    )


def test_manager_loop_repairs_current_research_once_before_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    retrieval_hints: list[object] = []
    second_pass_queries: list[str] = []

    def fake_retrieve_company_profile_live(*, company: str, **kwargs: object):
        calls.append(company)
        retrieval_hints.append(kwargs.get("retrieval_hint"))
        query_builder = kwargs.get("query_builder")
        if len(calls) == 2 and callable(query_builder):
            second_pass_queries.extend(query_builder(company, None))
        source = (
            SourceRecord(
                source_id="company:about",
                title="About OpenEvidence",
                url="https://www.openevidence.com/",
                source_type="company_site",
                supported_claims=["OpenEvidence describes its medical AI product."],
                confidence=0.9,
            )
            if len(calls) == 1
            else SourceRecord(
                source_id="news:funding",
                title="OpenEvidence 2026 funding update",
                url="https://example.org/openevidence-2026",
                source_type="news",
                supported_claims=["Independent coverage describes 2026 activity."],
                confidence=0.8,
            )
        )
        profile = research_company_fixture(company_name=company).model_copy(
            update={"sources": [source]}
        )
        return profile, {"debug_notes": [f"fake retrieval pass {len(calls)}"]}

    class FakeReview:
        status = "pass"
        overall_score = 88
        approval_boundary_ok = True
        observed_gaps: list[str] = []
        recommended_next_step = "Looks acceptable."

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )
    monkeypatch.setattr(workflow_runner, "review_specialist_output", lambda **_kwargs: FakeReview())

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="business research analyst Summarize what OpenEvidence is doing in 2026",
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "OpenEvidence",
            },
        ),
        max_steps=2,
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    review_events = [event for event in events if event.event_type == "manager_loop_review"]

    assert calls == ["OpenEvidence", "OpenEvidence"]
    assert retrieval_hints[0] is None
    assert retrieval_hints[1] is not None
    assert retrieval_hints[1].needs_precision_search is True
    assert any("independent coverage funding partnership" in query for query in second_pass_queries)
    assert result.status == WorkItemStatus.DONE
    assert not any(blocker.code == "manager_loop_review_failed" for blocker in result.blockers)
    assert review_events[0].metadata["review_decision"] == "repair"
    assert review_events[-1].metadata["review_decision"] == "pass"
    repair_started = next(
        event for event in events if event.event_type == "manager_loop_repair_started"
    )
    assert repair_started.metadata["search_repair_hint"] == (
        "broaden_or_deepen_search_within_cost_profile"
    )
    repair_advance_started = [event for event in events if event.event_type == "advance_started"][
        -1
    ]
    assert (
        repair_advance_started.metadata["external_context"]["manager_loop_repair"][
            "search_repair_hint"
        ]
        == "broaden_or_deepen_search_within_cost_profile"
    )
    assert any(event.event_type == "manager_loop_repair_completed" for event in events)
    assert "Metadata" in result.human_summary
    assert "Manager loop steps:" in result.human_summary
    assert "1 repair/deepen pass(es) attempted" in result.human_summary


def test_slack_conservative_research_caps_fanout_skips_contacts_and_repair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_retrieve_company_profile_live(
        *,
        company: str,
        query_builder,
        agents_web_search_max_calls: int | None = None,
        agents_web_search_parallel: bool | None = None,
        **_: object,
    ):
        queries = query_builder(company, None)
        calls.append(
            {
                "company": company,
                "query_count": len(queries),
                "agents_web_search_max_calls": agents_web_search_max_calls,
                "agents_web_search_parallel": agents_web_search_parallel,
            }
        )
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    SourceRecord(
                        source_id="company:about",
                        title=f"About {company}",
                        url=f"https://www.{company.lower()}.com/",
                        source_type="company_site",
                        supported_claims=[f"{company} describes its platform."],
                        confidence=0.9,
                    )
                ]
            }
        )
        return profile, {
            "debug_notes": ["fake conservative retrieval"],
            "search_provider": "searxng+agents-web-search",
            "search_queries": queries,
            "raw_search_result_count": 7,
            "provider_usage": {
                "searxng": {
                    "requests_attempted": len(queries),
                    "requests_succeeded": len(queries),
                    "raw_result_count": 7,
                },
                "agents-web-search": {
                    "requests_attempted": 1,
                    "requests_succeeded": 1,
                    "credits_used": 1,
                    "input_tokens": 100,
                    "cached_input_tokens": 50,
                    "output_tokens": 20,
                    "estimated_usd": 0.001,
                },
            },
            "search_quality": {"source_coverage": {}},
        }

    class FakeReview:
        status = "pass"
        overall_score = 88
        approval_boundary_ok = True
        observed_gaps: list[str] = []
        recommended_next_step = "Looks acceptable."

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )
    monkeypatch.setattr(workflow_runner, "review_specialist_output", lambda **_kwargs: FakeReview())

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="business research analyst research Spring Health in 2026 and cite sources",
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "team_id": "T123",
                "channel_id": "C123",
                "selected_message_ts": "1715366400.000100",
                "thread_ts": "1715366400.000100",
                "selected_message": {
                    "ts": "1715366400.000100",
                    "user_id": "U123",
                    "text": "Can someone research Spring Health?",
                },
            },
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "Spring Health",
            },
        ),
        max_steps=2,
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    review_events = [event for event in events if event.event_type == "manager_loop_review"]
    retrieval_events = [event for event in events if event.event_type == "workflow_retrieval_usage"]
    advance_started = next(event for event in events if event.event_type == "advance_started")

    assert len(calls) == 1
    assert advance_started.metadata["cost_profile"] == "slack_research_balanced"
    assert advance_started.metadata["allow_manager_loop_repair"] is False
    assert advance_started.metadata["include_contact_enrichment"] is False
    assert advance_started.metadata["hosted_web_search_max_calls"] == 1
    assert advance_started.metadata["reuse_existing_research"] is True
    assert calls[0]["query_count"] <= 12
    assert calls[0]["agents_web_search_max_calls"] == 1
    assert calls[0]["agents_web_search_parallel"] is False
    assert all(ref.artifact_type != "contact_candidates" for ref in result.artifact_refs)
    assert result.status == WorkItemStatus.BLOCKED
    assert review_events[-1].metadata["review_decision"] == "block"
    assert review_events[-1].metadata["blocking"] is True
    assert not any(event.event_type == "manager_loop_repair_started" for event in events)
    assert retrieval_events[-1].metadata["query_count"] <= 12
    assert retrieval_events[-1].metadata["aggregate_usage"]["credits_used"] == 1
    assert retrieval_events[-1].metadata["aggregate_usage"]["cache_hit_rate"] == 0.5


def test_slack_bridge_env_applies_conservative_cost_controls_without_context_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_REPO", str(tmp_path / "business-agents"))
    monkeypatch.setenv("KNI_BUSINESS_AGENTS_LIVE_SEARCH", "true")
    calls: list[dict[str, object]] = []

    def fake_retrieve_company_profile_live(
        *,
        company: str,
        query_builder,
        agents_web_search_max_calls: int | None = None,
        agents_web_search_parallel: bool | None = None,
        **_: object,
    ):
        queries = query_builder(company, None)
        calls.append(
            {
                "company": company,
                "query_count": len(queries),
                "agents_web_search_max_calls": agents_web_search_max_calls,
                "agents_web_search_parallel": agents_web_search_parallel,
            }
        )
        return research_company_fixture(company_name=company), {
            "debug_notes": ["fake Slack bridge env retrieval"],
            "search_provider": "searxng+agents-web-search",
            "search_queries": queries,
            "raw_search_result_count": 3,
            "provider_usage": {
                "searxng": {
                    "requests_attempted": len(queries),
                    "requests_succeeded": len(queries),
                    "raw_result_count": 3,
                },
                "agents-web-search": {
                    "requests_attempted": 1,
                    "requests_succeeded": 1,
                    "credits_used": 1,
                },
            },
        }

    class FakeReview:
        status = "fail"
        overall_score = 44
        approval_boundary_ok = True
        observed_gaps = ["Needs independent current-year sources."]
        recommended_next_step = "Deepen research only if explicitly requested."

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )
    monkeypatch.setattr(workflow_runner, "review_specialist_output", lambda **_kwargs: FakeReview())

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text="business research analyst research Spring Health in 2026 and cite sources",
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "Spring Health",
            },
        ),
        max_steps=2,
    )

    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    advance_started = next(event for event in events if event.event_type == "advance_started")

    assert len(calls) == 1
    assert advance_started.metadata["cost_profile"] == "slack_research_balanced"
    assert advance_started.metadata["allow_manager_loop_repair"] is False
    assert advance_started.metadata["include_contact_enrichment"] is False
    assert advance_started.metadata["hosted_web_search_max_calls"] == 1
    assert advance_started.metadata["reuse_existing_research"] is True
    assert advance_started.metadata["sdk_session"]["enabled"] is True
    assert calls[0]["query_count"] <= 12
    assert calls[0]["agents_web_search_max_calls"] == 1
    assert calls[0]["agents_web_search_parallel"] is False
    assert all(ref.artifact_type != "contact_candidates" for ref in result.artifact_refs)
    assert not any(event.event_type == "manager_loop_repair_started" for event in events)


@pytest.mark.parametrize(
    ("route", "expected_profile", "expected_hosted_cap"),
    [
        ("orchestrator", "slack_manager_balanced", 1),
        ("chief_of_staff", "slack_manager_balanced", 1),
        ("gmail_triage", "slack_context_light", 0),
        ("outreach_composer", "slack_context_light", 0),
        ("opportunity_scout", "slack_opportunity_balanced", 2),
        ("business_research_analyst", "slack_research_balanced", 1),
    ],
)
def test_slack_context_uses_route_aware_cost_profiles(
    route: str,
    expected_profile: str,
    expected_hosted_cap: int,
) -> None:
    request = workflow_runner._normalize_workflow_request_for_context(
        WorkflowRunRequest(
            request_text=f"{route} handle this Slack request",
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan={
                "source": "test",
                "requested_agent": route,
                "target_agent": route,
                "intent": route,
            },
        )
    )

    assert request.cost_profile == expected_profile
    assert request.hosted_web_search_max_calls == expected_hosted_cap
    assert request.allow_manager_loop_repair is False
    assert request.include_contact_enrichment is False
    assert request.reuse_existing_research is True


def test_slack_deep_research_request_can_escalate_cost_profile() -> None:
    request = workflow_runner._normalize_workflow_request_for_context(
        WorkflowRunRequest(
            request_text="business research analyst do deeper research and find contact",
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan={
                "source": "test",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
            },
        )
    )

    assert request.cost_profile == "slack_research_deep"
    assert request.hosted_web_search_max_calls == 2
    assert request.allow_manager_loop_repair is True
    assert request.include_contact_enrichment is True
    assert request.reuse_existing_research is False


def test_slack_chief_deep_search_request_gets_bounded_manager_repair_profile() -> None:
    request = workflow_runner._normalize_workflow_request_for_context(
        WorkflowRunRequest(
            request_text=(
                "chief of staff do a deeper source-backed search on mental health AI "
                "safety features. Return Answer, Detailed Summary, source URLs, and "
                "compact metadata."
            ),
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan={
                "source": "test",
                "requested_agent": "chief_of_staff",
                "target_agent": "chief_of_staff",
                "intent": "research_brief",
            },
        )
    )

    assert request.cost_profile == "slack_manager_deep"
    assert request.hosted_web_search_max_calls == 2
    assert request.allow_manager_loop_repair is True
    assert request.include_contact_enrichment is False
    assert request.reuse_existing_research is False


def test_slack_gmail_request_does_not_escalate_to_deep_research_cost_profile() -> None:
    request = workflow_runner._normalize_workflow_request_for_context(
        WorkflowRunRequest(
            request_text=(
                "gmail triage classify this email and provide draft-only reply guidance; "
                "do not send or create a Gmail draft"
            ),
            external_context={
                "schema": "keystone.slack.selected_message_context.v1",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
            },
            manual_request_plan={
                "source": "test",
                "requested_agent": "gmail_triage",
                "target_agent": "gmail_triage",
                "intent": "gmail_triage",
            },
        )
    )

    assert request.cost_profile == "slack_context_light"
    assert request.hosted_web_search_max_calls == 0
    assert request.allow_manager_loop_repair is False
    assert request.include_contact_enrichment is False
    assert request.reuse_existing_research is True


def test_orchestrator_memo_asks_specialist_to_reason_about_success_criteria() -> None:
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research OpenEvidence",
        request_text="business research analyst Summarize what OpenEvidence is doing in 2026",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="OpenEvidence", object_type="company"),
    )

    payload = workflow_runner._specialist_orchestrator_context_payload(
        WorkflowRunRequest(
            request_text="business research analyst Summarize what OpenEvidence is doing in 2026"
        ),
        work_item,
    )

    checklist = payload["response_quality_checklist"]
    memo_text = " ".join(checklist).lower()
    assert "advisory guidance" in memo_text
    assert "derive task-specific success criteria" in memo_text
    assert "available tools" in memo_text
    assert "precise blocker" in memo_text
    assert "freshness" in memo_text


def test_orchestrator_memo_includes_latest_review_feedback_for_repair() -> None:
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research OpenEvidence",
        request_text="business research analyst Summarize what OpenEvidence is doing in 2026",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(
            name="OpenEvidence",
            object_type="company",
            metadata={
                "orchestrator_reviews": [
                    {
                        "route": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                        "review_status": "fail",
                        "review_decision": "repair",
                        "overall_score": 55,
                        "observed_gaps": ["Needs independent current-year sources."],
                        "recommended_next_step": "Deepen retrieval before finalizing.",
                    }
                ]
            },
        ),
    )

    payload = workflow_runner._specialist_orchestrator_context_payload(
        WorkflowRunRequest(
            request_text="business research analyst Summarize what OpenEvidence is doing in 2026"
        ),
        work_item,
    )

    feedback = payload["orchestrator_feedback"]
    assert feedback["review_decision"] == "repair"
    assert feedback["observed_gaps"] == ["Needs independent current-year sources."]
    assert "Address these Orchestrator review gaps" in feedback["repair_instruction"]


def test_live_work_item_runs_user_facing_response_synthesis(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_retrieve_company_profile_live(*, company: str, **_: object):
        return research_company_fixture(company_name=company), {
            "debug_notes": ["fake live retrieval"]
        }

    class FakeSynthesis:
        title = "Big Health summary"
        answer = "Big Health is a digital mental health company with source-backed context."
        key_points = ["Deterministic profile and contact artifacts were created."]
        caveats = ["No outreach was sent."]
        next_step = "Review the profile before drafting anything."

    def fake_synthesize(result, *, user_request: str, live: bool, session=None):
        captured["user_request"] = user_request
        captured["live"] = live
        captured["route"] = result.route.value
        captured["artifact_types"] = [artifact.artifact_type for artifact in result.artifact_refs]

        class FakeSDKResult:
            output = FakeSynthesis()
            usage = {
                "input_tokens": 2000,
                "cached_input_tokens": 1500,
                "output_tokens": 300,
                "cache_hit_rate": 0.75,
                "prompt_cache_key_present": True,
                "prompt_cache_key_hash": "abc123def456",
            }
            cost = {
                "estimated_usd": 0.03,
                "pricing_provider": "openai",
                "pricing_model": "gpt-5.4",
            }
            request_cache = {
                "static_prefix_sha256": "static-hash",
                "instructions_sha256": "instructions-hash",
                "tool_names_sha256": "tools-hash",
                "output_schema_sha256": "schema-hash",
                "dynamic_prompt_sha256": "prompt-hash",
                "dynamic_prompt_chars": 1234,
                "session_attached": True,
                "session_id_hash": "session-hash",
                "session_scope": "workitem",
                "session_source": "derived",
            }

        return FakeSDKResult()

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )
    monkeypatch.setattr(
        "keystone_agents.workflow_runner.synthesize_user_facing_work_item_response_sdk_result",
        fake_synthesize,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="business research analyst research Big Health",
            database_url=_database_url(tmp_path),
            save=True,
            live_search=True,
            live_sdk=True,
            manual_request_plan={
                "source": "llm",
                "requested_agent": "business_research_analyst",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "primary_target": "Big Health",
            },
        )
    )

    assert captured["live"] is True
    assert captured["route"] == "business_research_analyst"
    assert "company_profile" in captured["artifact_types"]
    assert result.human_summary.startswith("Big Health summary")
    assert "Live user-facing response synthesis executed." in result.audit_notes
    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    sdk_event = next(event for event in events if event.event_type == "workflow_sdk_usage")
    assert sdk_event.metadata["usage"]["cache_hit_rate"] == 0.75
    assert sdk_event.metadata["cost"]["pricing_model"] == "gpt-5.4"
    assert sdk_event.metadata["request_cache"]["static_prefix_sha256"] == "static-hash"
    assert sdk_event.metadata["request_cache"]["dynamic_prompt_chars"] == 1234
    assert sdk_event.metadata["request_cache"]["session_scope"] == "workitem"


def test_user_response_synthesis_receives_orchestrator_review_feedback(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeSDKResult:
        output = response_synthesis.UserFacingResponseSynthesis(
            answer="Summarized with review feedback.",
            key_points=[],
            caveats=[],
            next_step="",
        )

    def fake_run_typed_sdk_agent(*, typed_input, **_kwargs):
        captured["typed_input"] = typed_input
        return FakeSDKResult()

    monkeypatch.setattr(
        "keystone_agents.response_synthesis.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    result = workflow_runner.WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Architecture review",
            request_text="review the agent architecture",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            target=WorkItemTarget(
                metadata={
                    "orchestrator_reviews": [
                        {
                            "step": 1,
                            "route": "chief_of_staff",
                            "status": "done",
                            "review_status": "partial",
                            "overall_score": 72,
                            "approval_boundary_ok": True,
                            "observed_gaps": ["Needs source-backed architecture evidence."],
                            "recommended_next_step": "Deepen with repo context.",
                        }
                    ]
                }
            ),
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Deterministic summary",
    )

    response_synthesis.synthesize_user_facing_work_item_response(
        result,
        user_request="review the agent architecture",
        live=True,
    )

    typed_input = captured["typed_input"]
    assert typed_input.orchestrator_reviews[0]["review_status"] == "partial"
    assert typed_input.orchestrator_reviews[0]["observed_gaps"] == [
        "Needs source-backed architecture evidence."
    ]
    assert typed_input.orchestrator_reviews[0]["recommended_next_step"] == (
        "Deepen with repo context."
    )
    prompt = typed_input.to_prompt()
    assert "Orchestrator review feedback" in prompt
    assert "Needs source-backed architecture evidence." in prompt
    assert "Answer in KNI's operator voice" in prompt
    assert "the evidence does answer the core request" in prompt


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
    orchestrator_preflight = {
        "request_text": "find 5 conference opportunities",
        "advisory_only": False,
        "selected_agent": "opportunity_scout",
        "manual_request_plan": manual_plan,
        "route_result": {
            "route": "opportunity_scout",
            "routing_mode": "deterministic",
            "rationale": "Planner selected Opportunity Scout.",
            "refused": False,
            "send_enabled": False,
        },
    }

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="find 5 conference opportunities",
            database_url=database_url,
            save=True,
            max_results=3,
            manual_request_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
        )
    )

    store = SQLiteStore(database_url)
    loaded = store.get_work_item(result.work_item.id)
    events = store.list_work_item_events(result.work_item.id)

    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert result.manual_request_plan == manual_plan
    assert result.orchestrator_preflight == orchestrator_preflight
    assert result.artifact_refs
    assert loaded is not None
    assert loaded.target.metadata["manual_request_plan"]["desired_count"] == 5
    assert loaded.target.metadata["manual_desired_count"] == 5
    assert events[0].metadata["manual_request_plan"]["target_agent"] == "opportunity_scout"
    assert events[0].metadata["orchestrator_preflight"]["selected_agent"] == "opportunity_scout"


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


def test_thread_local_outreach_draft_can_proceed_without_approved_context(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "outreach composer Read the current Halo Gmail thread, then draft a "
                "short operator-voice reply in Slack only. Thread-local Slack draft "
                "only; external delivery, scheduling, publishing, and provider-side "
                "draft creation are out of scope. Use the operator default writing "
                "style profile."
            ),
            database_url=database_url,
            save=True,
        )
    )

    draft_row = store.fetch_all("outreach_drafts")[0]

    assert result.advanced is True
    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.status == WorkItemStatus.DONE
    assert result.artifact_refs[0].metadata["thread_local_slack_draft"] is True
    assert result.artifact_refs[0].metadata["approval_queue_created"] is False
    assert draft_row["email_body"].endswith("Sincerely,\nAnup")
    assert "Gmail thread body was not available" in result.human_summary
    assert "no external message was sent" in result.human_summary
    assert store.count("outreach_drafts") == 1
    assert store.list_approval_items(object_type="outreach_draft") == []


def test_gmail_triage_live_retrieval_reads_recent_matching_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    class FakeGmailTool:
        def __init__(self, *, live: bool) -> None:
            assert live is True

        def list_recent_messages(
            self,
            *,
            label: str | None = None,
            max_results: int = 1,
            query: str | None = None,
        ) -> list[dict[str, str]]:
            assert label is None
            assert max_results >= 3
            queries.append(query or "")
            return [
                {"id": "msg-old", "threadId": "thread-old"},
                {"id": "msg-new", "threadId": "thread-new"},
            ]

        def get_thread(self, thread_id: str) -> dict[str, object]:
            if thread_id == "thread-new":
                return {
                    "thread_id": "thread-new",
                    "subject": "Halo follow up",
                    "summary": "Halo asked for a quick reply about next steps.",
                    "thread_context": "Most recent Halo note asks whether Anup can review.",
                    "message_count": 2,
                    "latest_received_at": "2026-05-31T14:30:00Z",
                    "participants": ["Halo <hello@halo.example>", "Anup <wisegrow05@gmail.com>"],
                    "action_items": ["Reply to Halo with availability."],
                    "open_questions": ["Can Anup take a look?"],
                    "messages": [
                        {
                            "id": "msg-new",
                            "received_at": "2026-05-31T14:30:00Z",
                            "sender_name": "Halo",
                            "sender_email": "hello@halo.example",
                            "subject": "Halo follow up",
                            "snippet": "Can you take a look?",
                            "thread_summary": "Halo asked Anup to take a look.",
                        }
                    ],
                }
            return {
                "thread_id": "thread-old",
                "subject": "Older Halo note",
                "summary": "Older Halo context.",
                "thread_context": "Older Halo context.",
                "message_count": 1,
                "latest_received_at": "2026-05-20T12:00:00Z",
                "participants": ["Halo <hello@halo.example>"],
                "messages": [],
            }

    monkeypatch.setattr(workflow_runner, "GmailTool", FakeGmailTool)
    monkeypatch.setattr(workflow_runner, "cli_default_live_gmail", lambda: True)

    result = workflow_runner._advance_work_item_one_step(
        WorkflowRunRequest(
            request_text=(
                "Read the current Halo email in my wisegrow05@gmail.com inbox, then "
                "draft a short reply here."
            ),
            requested_route=WorkItemRoute.GMAIL_TRIAGE,
            database_url=_database_url(tmp_path),
            save=True,
            live_sdk=True,
            max_results=3,
        ),
        synthesize_user_response=False,
    )

    assert result.route == WorkItemRoute.GMAIL_TRIAGE
    assert result.status == WorkItemStatus.IN_PROGRESS
    assert result.next_action is not None
    assert result.next_action.agent == WorkItemRoute.OUTREACH_COMPOSER
    assert "Halo" in queries[0]
    assert "in:inbox" in queries[0]
    assert "wisegrow05@gmail.com" not in queries[0]
    assert result.artifact_refs[0].metadata["selected_thread_id"] == "thread-new"
    assert result.artifact_refs[0].metadata["matched_thread_count"] == 2


def test_chief_of_staff_email_reply_workflow_delegates_to_gmail_then_outreach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGmailTool:
        def __init__(self, *, live: bool) -> None:
            assert live is True

        def list_recent_messages(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"id": "msg-halo", "threadId": "thread-halo"}]

        def get_thread(self, thread_id: str) -> dict[str, object]:
            assert thread_id == "thread-halo"
            return {
                "thread_id": "thread-halo",
                "subject": "Halo partnership note",
                "summary": "Halo asked if Anup can review a short partnership note.",
                "thread_context": "Halo wants a concise acknowledgement and next step.",
                "message_count": 1,
                "latest_received_at": "2026-05-31T15:00:00Z",
                "participants": ["Halo <hello@halo.example>", "Anup <wisegrow05@gmail.com>"],
                "action_items": ["Acknowledge and say Anup can take a look."],
                "open_questions": ["Can Anup review the partnership note?"],
                "messages": [
                    {
                        "id": "msg-halo",
                        "received_at": "2026-05-31T15:00:00Z",
                        "sender_name": "Halo",
                        "sender_email": "hello@halo.example",
                        "subject": "Halo partnership note",
                        "snippet": "Could you review this?",
                        "thread_summary": "Halo asked for review.",
                    }
                ],
            }

    def fake_run_chief_of_staff_sdk(
        sdk_input: dict[str, object],
        **_kwargs: object,
    ) -> TypedAgentRunResult[object]:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=workflow_runner.plan_chief_of_staff_request(str(sdk_input["request"])),
            raw_result=None,
            live=True,
            usage={
                "requests": 1,
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
            },
            cost={
                "estimated_usd": 0.0042,
                "amount_usd": 0.0042,
                "pricing_provider": "openai",
                "pricing_model": "gpt-test",
            },
            request_cache={"prompt_cache_key_hash": "chief-cost-test"},
        )

    monkeypatch.setattr(workflow_runner, "GmailTool", FakeGmailTool)
    monkeypatch.setattr(workflow_runner, "cli_default_live_gmail", lambda: True)
    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                "chief of staff read the current Halo email in my inbox and draft a "
                "short reply here."
            ),
            database_url=_database_url(tmp_path),
            save=True,
            live_sdk=True,
        ),
        max_steps=3,
    )

    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.status == WorkItemStatus.DONE
    assert "whether Anup can review the partnership note" in result.human_summary
    assert "Heading: Draft email for Halo" in result.human_summary
    assert "To: hello@halo.example" in result.human_summary
    assert "Context:" not in result.human_summary
    assert "Triage: direct ask/action detected" not in result.human_summary
    assert "Sincerely,\nAnup" in result.human_summary
    assert result.artifact_refs[0].metadata["thread_local_slack_draft"] is True
    assert result.artifact_refs[0].metadata["recipient_email"] == "hello@halo.example"
    assert result.artifact_refs[0].metadata["boundary_summary"].startswith("Slack-thread-only")
    assert any(
        "Triage: direct ask/action detected" in line
        for line in result.artifact_refs[0].metadata["thread_context_lines"]
    )
    assert "Source IDs used" not in result.human_summary
    assert "Missing or blocked context" not in result.human_summary
    events = SQLiteStore(_database_url(tmp_path)).list_work_item_events(result.work_item.id)
    chief_cost_event = next(
        event
        for event in events
        if event.event_type == "workflow_sdk_usage"
        and event.metadata.get("run_stage") == "chief_of_staff.live_sdk"
    )
    assert chief_cost_event.metadata["cost"]["estimated_usd"] == 0.0042


def test_chief_of_staff_email_reply_recovers_from_live_schema_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGmailTool:
        def __init__(self, *, live: bool) -> None:
            assert live is True

        def list_recent_messages(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"id": "msg-halo", "threadId": "thread-halo"}]

        def get_thread(self, thread_id: str) -> dict[str, object]:
            assert thread_id == "thread-halo"
            return {
                "thread_id": "thread-halo",
                "subject": "Halo partnership note",
                "summary": "Halo asked whether Anup can review a short note.",
                "thread_context": "Halo wants a concise acknowledgement.",
                "message_count": 1,
                "latest_received_at": "2026-05-31T15:00:00Z",
                "participants": ["Anna <anna@halo.example>", "Anup <wisegrow05@gmail.com>"],
                "open_questions": ["Can Anup review the note?"],
                "messages": [
                    {
                        "id": "msg-halo",
                        "received_at": "2026-05-31T15:00:00Z",
                        "sender_name": "Anna",
                        "sender_email": "anna@halo.example",
                        "subject": "Halo partnership note",
                        "snippet": "Can you review this?",
                        "thread_summary": "Halo asked for review.",
                    }
                ],
            }

    def broken_live_chief_of_staff(*_args: object, **_kwargs: object) -> object:
        raise ValueError('Invalid JSON when parsing {"agent_name":"chief_of_staff"')

    database_url = _database_url(tmp_path)
    monkeypatch.setattr(workflow_runner, "GmailTool", FakeGmailTool)
    monkeypatch.setattr(workflow_runner, "cli_default_live_gmail", lambda: True)
    monkeypatch.setattr(
        workflow_runner,
        "run_chief_of_staff_sdk",
        broken_live_chief_of_staff,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_maybe_synthesize_user_facing_response",
        lambda result, **_kwargs: result,
    )

    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=(
                "chief of staff read the most recent Halo email thread and draft "
                "a short Slack-thread-only reply. Also keep track of this run costs."
            ),
            database_url=database_url,
            save=True,
            live_sdk=True,
        ),
        max_steps=3,
    )

    events = SQLiteStore(database_url).list_work_item_events(result.work_item.id)

    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.status == WorkItemStatus.DONE
    assert "Hi Anna," in result.human_summary
    assert "Cost tracking: requested" not in result.human_summary
    assert result.artifact_refs[0].metadata["cost_tracking_requested"] is True
    fallback_event = next(
        event for event in events if event.event_type == "chief_of_staff_live_sdk_fallback"
    )
    assert fallback_event.metadata["safe_to_continue"] is True
    assert "Invalid JSON" in fallback_event.metadata["reason"]


def test_thread_local_gmail_draft_omits_marketing_snippet_from_reply_focus() -> None:
    summary = workflow_runner.GmailThreadSummaryResult(
        thread_id="thread-halo",
        subject="Welcome to Halo!",
        summary=(
            "Our platform makes it easy for innovators to work with industry partners "
            "and move their science forward. Start by creating a profile."
        ),
        participants=["Anna <anna@halo.science>", "Anup <wisegrow05@gmail.com>"],
        message_count=1,
    )

    draft = workflow_runner._thread_local_outreach_draft(
        "draft a short reply here",
        gmail_thread_context=summary,
    )

    assert draft.email_body.startswith("Hi Anna,")
    assert "Our platform makes it easy" not in draft.email_body
    assert "Start by creating" not in draft.email_body
    assert "Thanks for reaching out and for the overview of Halo." in draft.email_body
    assert "if there is a useful fit" in draft.email_body
    assert draft.blocked_facts == []


def test_thread_local_gmail_draft_uses_safe_concrete_detail_when_available() -> None:
    summary = workflow_runner.GmailThreadSummaryResult(
        thread_id="thread-halo",
        subject="Welcome to Halo!",
        thread_context=(
            "Start by creating a Partner Listing. Or, respond to active requests on Halo."
        ),
        participants=["Anna <anna@halo.science>", "Anup <wisegrow05@gmail.com>"],
        message_count=1,
    )

    draft = workflow_runner._thread_local_outreach_draft(
        "draft a short reply here",
        gmail_thread_context=summary,
    )

    assert "Halo's partner listings and active requests" in draft.email_body
    assert "Start by creating" not in draft.email_body


def test_thread_local_gmail_summary_keeps_email_fields_in_main_body() -> None:
    summary = workflow_runner.GmailThreadSummaryResult(
        thread_id="thread-halo",
        subject="Welcome to Halo!",
        participants=["Anna <anna@halo.science>", "Anup <wisegrow05@gmail.com>"],
        message_count=1,
        latest_received_at="2026-05-31T12:01:53Z",
    )
    draft = workflow_runner._thread_local_outreach_draft(
        "draft a short reply here",
        gmail_thread_context=summary,
    )

    rendered = workflow_runner._format_outreach_draft_work_item_summary(
        draft,
        company_name="Anna",
        compact_thread_local=True,
        gmail_thread_context=summary,
        cost_tracking_requested=True,
    )

    assert "Heading: Draft email for Anna" in rendered
    assert "To: anna@halo.science" in rendered
    assert "Context:" not in rendered
    assert "Read: 1 message, subject 'Welcome to Halo!', from Anna" not in rendered
    assert "no direct question or personal action item detected" not in rendered
    assert "Cost tracking: requested" not in rendered
    assert "Source IDs used" not in rendered


def test_outreach_natural_language_source_context_creates_draft_only_artifact(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text=(
                "outreach composer write a draft-only email to NeuroFlow using this approved "
                "source-backed context. Company: NeuroFlow. Source: <https://neuroflow.com>. "
                "Facts: NeuroFlow supports behavioral health care teams with measurement and "
                "care navigation workflows; Keystone could help pressure-test evaluation design "
                "and clinical operations for a pilot. Do not send, save externally, post, or use "
                "external writes."
            ),
            database_url=database_url,
            save=True,
        )
    )

    loaded = store.get_work_item(result.work_item.id)
    events = store.list_work_item_events(result.work_item.id)

    assert result.advanced is True
    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.status == WorkItemStatus.NEEDS_APPROVAL
    assert result.artifact_refs[0].artifact_type == "outreach_draft"
    draft_row = store.fetch_all("outreach_drafts")[0]
    assert draft_row["email_subject"] in result.human_summary
    assert draft_row["email_body"] in result.human_summary
    assert "no external message was sent" in result.human_summary
    assert "no Gmail draft was created" in result.human_summary
    assert loaded is not None
    company_refs = selected_artifacts(loaded, "company_profile")
    assert company_refs
    assert company_refs[0].approval_state == ApprovalState.APPROVED_FOR_DRAFTING.value
    assert company_refs[0].metadata["inline_natural_language_context"] is True
    assert company_refs[0].metadata["source_url"] == "https://neuroflow.com"
    assert store.count("outreach_drafts") == 1
    assert any(event.event_type == "inline_outreach_context_attached" for event in events)
    gate_event = next(
        event for event in events if event.event_type == "skill_contract_gates_checked"
    )
    gate = gate_event.metadata["gates"][0]
    assert gate["gate_id"] == "outreach_approval_claim_gate"
    assert gate["status"] == "passed"
    assert gate["evidence"]["external_approval_gate"] is True
    assert gate["evidence"]["unsafe_flags"] == []


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


def test_generic_research_action_uses_selected_opportunity_candidate(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Opportunity scan: behavioral health AI",
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        target=WorkItemTarget(name="behavioral health AI", object_type="topic"),
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="opportunity",
                artifact_id="38",
                source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                approval_state="pending",
                title="Theris",
                summary="AI-augmented behavioral health provider.",
            ),
            WorkItemArtifactRef(
                artifact_type="opportunity",
                artifact_id="39",
                source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
                approval_state="pending",
                title="ARPA-H",
                summary="Behavioral health program source.",
            ),
        ],
    )
    item = select_artifact(item, "opportunity", "38")
    store.save_work_item(item)

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="Run deeper source-backed business research for this WorkItem.",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
            requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        )
    )

    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.artifact_refs[0].artifact_type == "company_profile"
    assert result.artifact_refs[0].title == "Theris"


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


def test_live_outreach_handoff_includes_orchestrator_memo_and_raw_request(
    tmp_path: Path,
    monkeypatch,
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
    item = set_next_action(
        item,
        WorkItemNextAction(
            action="draft_outreach",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
        ),
    )
    store = SQLiteStore(database_url)
    store.save_work_item(item)
    captured: dict[str, str] = {}

    def fake_run_retrieved_sdk_synthesis(**kwargs):
        context = kwargs["retrieve"]()
        typed_input = kwargs["normalize"](context)
        captured["approved_context"] = typed_input.approved_context

        class Outcome:
            final_output = {
                "company_name": "NeuroFlow",
                "email_subject": "Comparing notes",
                "email_body": "Hi,\n\nOpen to compare notes?\n\nSincerely,\nAnup",
                "linkedin_note": "Open to compare notes?",
                "personalization_rationale": "Used only approved WorkItem context.",
                "source_ids_used": ["keystone_profile"],
            }

        return Outcome()

    def fake_compose_outreach_draft_llm_constrained(*, approved_context, **_kwargs):
        return workflow_runner.compose_outreach_draft_fixture(
            company_profile=approved_context.company_profile,
            opportunity_record=approved_context.opportunity_record,
            outreach_goal=approved_context.objective,
        ).model_copy(update={"drafting_mode": "llm_constrained"})

    monkeypatch.setattr(
        workflow_runner,
        "run_retrieved_sdk_synthesis",
        fake_run_retrieved_sdk_synthesis,
    )
    monkeypatch.setattr(
        workflow_runner,
        "compose_outreach_draft_llm_constrained",
        fake_compose_outreach_draft_llm_constrained,
    )

    def fail_user_response_synthesis(*_args, **_kwargs):
        raise AssertionError("generic response synthesis must not rewrite outreach drafts")

    monkeypatch.setattr(
        workflow_runner,
        "synthesize_user_facing_work_item_response_sdk_result",
        fail_user_response_synthesis,
    )

    result = advance_work_item(
        WorkflowRunRequest(
            request_text="draft outreach that references the original NeuroFlow research request",
            work_item_id=item.id,
            database_url=database_url,
            save=True,
            live_sdk=True,
        )
    )

    assert result.advanced is True
    assert result.route == WorkItemRoute.OUTREACH_COMPOSER
    assert result.artifact_refs[0].artifact_type == "outreach_draft"
    assert any("Outreach Composer live SDK draft created" in note for note in result.audit_notes)
    assert any("draft artifact is canonical" in note for note in result.audit_notes)
    assert "Orchestrator memo for this specialist WorkItem run" in captured["approved_context"]
    assert (
        '"raw_request": "draft outreach that references the original NeuroFlow research request"'
        in captured["approved_context"]
    )
    assert '"work_item_id":' in captured["approved_context"]
    assert "no send" in captured["approved_context"].lower()


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
