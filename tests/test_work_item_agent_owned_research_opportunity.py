"""Focused WorkItem tests for agent-owned Research and Opportunity decisions."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agents import Agent
from agents.items import ToolCallItem, ToolCallOutputItem
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall

from keystone_agents import workflow_runner
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.company_profile import CompanyProfile, SourceRecord
from keystone_agents.schemas.decision_ownership import (
    AgentDecisionRecord,
    DecisionCandidateAssessment,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.opportunity import (
    OpportunityRecord,
    OpportunityScoutResult,
    OpportunitySource,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.tools.search_provider import (
    SearchResult,
    search_result_candidate_id,
)

SELECTED_URL = "https://selected.example.test/evidence"
DECOY_URL = "https://decoy.example.test/old"
SELECTED_CANDIDATE_ID = search_result_candidate_id(SELECTED_URL)
DECOY_CANDIDATE_ID = search_result_candidate_id(DECOY_URL)


def _raw_search_result(*, candidate_count: int = 2) -> SimpleNamespace:
    candidates = [
        SearchResult(
            title="Northstar Behavioral current evidence",
            link=SELECTED_URL,
            snippet="Current Northstar Behavioral evidence relevant to the request.",
        ).model_dump(mode="json"),
        SearchResult(
            title="Older decoy",
            link=DECOY_URL,
            snippet="An older result that should be excluded.",
        ).model_dump(mode="json"),
    ]
    candidates.extend(
        SearchResult(
            title=f"Additional result {index}",
            link=f"https://additional-{index}.example.test/source",
            snippet="Additional provider result.",
        ).model_dump(mode="json")
        for index in range(3, candidate_count + 1)
    )
    return SimpleNamespace(
        new_items=[
            {
                "type": "tool_call_item",
                "call_id": "search-1",
                "tool_name": "search_web",
            },
            {
                "type": "tool_call_output_item",
                "call_id": "search-1",
                "output": json.dumps(
                    {
                        "query": "natural-language research query",
                        "results": candidates,
                        "location": "New York, NY",
                    }
                ),
            },
        ]
    )


def _decision(
    *,
    stage: str,
    selected_id: str,
    excluded_ids: tuple[str, ...] = (),
) -> AgentDecisionRecord:
    return AgentDecisionRecord(
        decision_stage=stage,
        selected_candidate_ids=[selected_id],
        candidate_assessments=[
            DecisionCandidateAssessment(
                candidate_id=selected_id,
                disposition="selected",
                rationale="This identity best matches the current request and evidence.",
            ),
            *[
                DecisionCandidateAssessment(
                    candidate_id=candidate_id,
                    disposition="excluded",
                    rationale="This provider alternative is weaker or outdated.",
                )
                for candidate_id in excluded_ids
            ],
        ],
        reasoning="Selected the strongest current source-backed candidate.",
    )


def _company_profile(
    *,
    excluded_ids: tuple[str, ...] = (),
    provider_candidate_id: str = SELECTED_CANDIDATE_ID,
) -> CompanyProfile:
    return CompanyProfile(
        name="Northstar Behavioral",
        description="Northstar supports behavioral-health care operations.",
        fit_summary="The current source supports a bounded research follow-up.",
        confidence_score=0.8,
        sources=[
            SourceRecord(
                source_id="selected-source",
                provider_candidate_id=provider_candidate_id,
                title="Northstar current evidence",
                url=SELECTED_URL,
                source_type="company_site",
                supported_claims=["Northstar supports behavioral-health care operations."],
                confidence=0.9,
            )
        ],
        decision=_decision(
            stage="research_source_selection",
            selected_id=provider_candidate_id,
            excluded_ids=excluded_ids,
        ),
    )


def _opportunity_result(
    *,
    excluded_ids: tuple[str, ...] = (),
    handoff: bool = True,
) -> OpportunityScoutResult:
    record = OpportunityRecord(
        company_name="Northstar Behavioral",
        canonical_entity_key="northstar-behavioral",
        opportunity_type="behavioral health AI",
        priority_score=70,
        why_now_signal="A current public signal supports a bounded follow-up.",
        recommended_next_step="Run Business Research before any outreach.",
        sources=[
            OpportunitySource(
                source_id="selected-opportunity-source",
                provider_candidate_id=SELECTED_CANDIDATE_ID,
                title="Northstar current signal",
                url=SELECTED_URL,
                source_type="company_site",
                supported_signal="A current public signal supports follow-up.",
            )
        ],
        source_signals=["current public company signal"],
        keystone_fit_reason="The signal is relevant to behavioral-health evidence work.",
        outside_consulting_likelihood=50,
        handoff_to_business_research_analyst=handoff,
        handoff_reason="Company and buyer context need deeper source validation.",
        business_research_analyst_handoff_recommendation=(
            "Validate the company, buyer context, and evidence needs."
        ),
        research_needed=["Validate company and buyer context."],
    )
    return OpportunityScoutResult(
        topic="behavioral-health opportunities",
        dry_run=False,
        search_provider="agent_tool_loop",
        raw_search_result_count=2,
        deduped_candidate_count=2,
        records=[record],
        decision=_decision(
            stage="opportunity_candidate_selection",
            selected_id=SELECTED_CANDIDATE_ID,
            excluded_ids=excluded_ids,
        ),
    )


def _typed_result(output: CompanyProfile | OpportunityScoutResult, raw_result: object):
    return TypedAgentRunResult(
        agent_name="specialist",
        output=output,
        raw_result=raw_result,
        live=True,
        usage={"available": True, "requests": 1},
        cost={"available": True, "estimated_usd": 0.001},
        request_cache={
            "decision_ownership": {"repair_attempted": False},
            "tool_execution": {
                "model_tool_call_count": 1,
                "model_called_tool_names": ["search_web"],
            },
        },
        execution_telemetry={"agent_runs": 1, "model_requests": 1},
        tool_receipts=[{"tool_name": "search_web", "status": "verified"}],
    )


def _plan(agent: str) -> dict[str, object]:
    opportunity = agent == "opportunity_scout"
    return ManualRequestPlan(
        source="llm",
        requested_agent=agent,
        target_agent=agent,
        workflow=[agent],
        intent="opportunity_search" if opportunity else "company_research",
        primary_target="Northstar Behavioral",
        target_type="topic" if opportunity else "company",
        provider_operations=["search", "read"],
        objective="Find and assess evidence relevant to the operator's request.",
        task_objective="opportunity_discovery" if opportunity else "entity_research",
        expected_artifact_type=(
            "opportunity_record" if opportunity else "research_brief"
        ),
        requires_live_search=True,
        side_effect_policy="draft_or_read_only",
    ).model_dump(mode="json")


def test_candidate_universe_uses_tool_outputs_not_location_and_fails_if_truncated() -> None:
    raw_result = _raw_search_result(candidate_count=25)

    universe = workflow_runner._work_item_sdk_candidate_universe(raw_result)
    validation = workflow_runner._work_item_decision_universe_validation(
        _company_profile(),
        universe,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )

    assert universe["included_candidate_count"] == 20
    assert universe["observed_unique_candidate_count"] == 25
    assert universe["truncated"] is True
    assert all(candidate["url"] != "New York, NY" for candidate in universe["candidates"])
    assert "raw_provider_candidate_universe_truncated" in validation["reason_codes"]


def test_search_candidate_identity_is_model_visible_and_canonical() -> None:
    with_tracking_fragment = (
        f"{SELECTED_URL}/?utm_source=provider&fbclid=tracking-value#details"
    )

    result = SearchResult(title="Selected", link=with_tracking_fragment)
    meaningful_query = SearchResult(
        title="Selected document",
        link=f"{SELECTED_URL}?document=active&utm_campaign=provider",
    )

    assert result.candidate_id == SELECTED_CANDIDATE_ID
    assert result.model_dump(mode="json")["candidate_id"] == SELECTED_CANDIDATE_ID
    assert meaningful_query.candidate_id == search_result_candidate_id(
        f"{SELECTED_URL}?document=active"
    )
    assert meaningful_query.candidate_id != search_result_candidate_id(
        f"{SELECTED_URL}?document=archived"
    )
    assert search_result_candidate_id(
        f"{SELECTED_URL}?view=full&document=active"
    ) == search_result_candidate_id(
        f"{SELECTED_URL}?document=active&view=full"
    )
    assert search_result_candidate_id(
        f"{SELECTED_URL}?tag=current&tag=evidence"
    ) != search_result_candidate_id(f"{SELECTED_URL}?tag=current")


def test_real_sdk_tool_items_preserve_search_candidate_universe() -> None:
    agent = Agent(name="candidate-universe-test", instructions="")
    raw_call = ResponseFunctionToolCall(
        arguments='{"query":"current evidence"}',
        call_id="call-search-1",
        name="search_web",
        type="function_call",
        id="function-search-1",
        status="completed",
    )
    results = [
        SearchResult(title="Selected", link=SELECTED_URL).model_dump(mode="json"),
        SearchResult(title="Decoy", link=DECOY_URL).model_dump(mode="json"),
    ]
    raw_result = SimpleNamespace(
        new_items=[
            ToolCallItem(agent=agent, raw_item=raw_call),
            ToolCallOutputItem(
                agent=agent,
                raw_item={
                    "call_id": "call-search-1",
                    "output": json.dumps(results),
                    "type": "function_call_output",
                },
                output=json.dumps(results),
            ),
        ]
    )

    universe = workflow_runner._work_item_sdk_candidate_universe(raw_result)

    assert [item["candidate_id"] for item in universe["candidates"]] == [
        SELECTED_CANDIDATE_ID,
        DECOY_CANDIDATE_ID,
    ]
    assert universe["identity_missing_count"] == 0


def test_candidate_universe_does_not_promote_nested_citations() -> None:
    selected = SearchResult(title="Selected", link=SELECTED_URL).model_dump(mode="json")
    selected["citations"] = [
        SearchResult(
            title="Nested citation",
            link="https://citation.example.test/support",
        ).model_dump(mode="json")
    ]
    raw_result = SimpleNamespace(
        new_items=[
            {
                "type": "tool_call_item",
                "call_id": "search-1",
                "tool_name": "search_web",
            },
            {
                "type": "tool_call_output_item",
                "call_id": "search-1",
                "output": json.dumps([selected]),
            },
        ]
    )

    universe = workflow_runner._work_item_sdk_candidate_universe(raw_result)

    assert universe["included_candidate_count"] == 1
    assert universe["candidates"][0]["candidate_id"] == SELECTED_CANDIDATE_ID


def test_validation_rejects_fabricated_provider_mapping_despite_matching_url() -> None:
    universe = workflow_runner._work_item_sdk_candidate_universe(_raw_search_result())
    output = _company_profile(provider_candidate_id="web-candidate:fabricated")

    validation = workflow_runner._work_item_decision_universe_validation(
        output,
        universe,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )

    assert validation["status"] == "repair_required"
    assert "returned_identity_not_selected_by_specialist" in validation["reason_codes"]


def test_validation_maps_benign_url_variants_without_substitution() -> None:
    raw_result = _raw_search_result()
    payload = json.loads(raw_result.new_items[1]["output"])
    payload["results"][0]["link"] = f"{SELECTED_URL}/#details"
    raw_result.new_items[1]["output"] = json.dumps(payload)
    universe = workflow_runner._work_item_sdk_candidate_universe(raw_result)
    output = _company_profile(excluded_ids=(DECOY_CANDIDATE_ID,))

    validation = workflow_runner._work_item_decision_universe_validation(
        output,
        universe,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )

    assert validation["status"] == "accepted"
    assert validation["selected_candidate_ids"] == [SELECTED_CANDIDATE_ID]


def test_opportunity_selects_all_supporting_raw_ids_and_rejects_fabricated_entity() -> None:
    raw_result = _raw_search_result(candidate_count=4)
    universe = workflow_runner._work_item_sdk_candidate_universe(raw_result)
    third_url = "https://additional-3.example.test/source"
    fourth_url = "https://additional-4.example.test/source"
    third_id = search_result_candidate_id(third_url)
    fourth_id = search_result_candidate_id(fourth_url)
    output = _opportunity_result(
        excluded_ids=(DECOY_CANDIDATE_ID, fourth_id),
    )
    record = output.records[0].model_copy(
        update={
            "sources": [
                *output.records[0].sources,
                OpportunitySource(
                    source_id="corroborating-source",
                    provider_candidate_id=third_id,
                    title="Corroborating current signal",
                    url=third_url,
                    source_type="news",
                    supported_signal="A second source corroborates the current signal.",
                ),
            ]
        }
    )
    output = output.model_copy(
        update={
            "records": [record],
            "decision": _decision(
                stage="opportunity_candidate_selection",
                selected_id=SELECTED_CANDIDATE_ID,
                excluded_ids=(DECOY_CANDIDATE_ID, fourth_id),
            ).model_copy(
                update={
                    "selected_candidate_ids": [SELECTED_CANDIDATE_ID, third_id],
                    "candidate_assessments": [
                        DecisionCandidateAssessment(
                            candidate_id=SELECTED_CANDIDATE_ID,
                            disposition="selected",
                            rationale="Primary current evidence.",
                        ),
                        DecisionCandidateAssessment(
                            candidate_id=third_id,
                            disposition="selected",
                            rationale="Independent corroborating evidence.",
                        ),
                        DecisionCandidateAssessment(
                            candidate_id=DECOY_CANDIDATE_ID,
                            disposition="excluded",
                            rationale="Older evidence.",
                        ),
                        DecisionCandidateAssessment(
                            candidate_id=fourth_id,
                            disposition="excluded",
                            rationale="Less relevant evidence.",
                        ),
                    ],
                }
            ),
        }
    )

    accepted = workflow_runner._work_item_decision_universe_validation(
        output,
        universe,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
    )
    fabricated = output.model_copy(
        update={
            "records": [
                record.model_copy(
                    update={
                        "company_name": "Fabricated Care Collective",
                        "entity_name": "Fabricated Care Collective",
                        "canonical_entity_key": "fabricated-care-collective",
                    }
                )
            ]
        }
    )
    rejected = workflow_runner._work_item_decision_universe_validation(
        fabricated,
        universe,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
    )

    assert accepted["status"] == "accepted"
    assert accepted["selected_candidate_ids"] == sorted(
        [SELECTED_CANDIDATE_ID, third_id]
    )
    assert rejected["status"] == "repair_required"
    assert "returned_entity_not_grounded_in_provider_candidates" in rejected["reason_codes"]
    assert rejected["ungrounded_entities"] == ["Fabricated Care Collective"]


def test_research_work_item_repairs_over_raw_universe_without_second_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_text = (
        "Please look into Northstar Behavioral's current care model and tell me what "
        "seems most relevant for us; use current public sources and keep it read-only."
    )
    raw_result = _raw_search_result()
    captured: dict[str, object] = {"repairs": 0}

    def fake_specialist(sdk_input: object, **kwargs: object):
        captured["sdk_input"] = sdk_input
        captured["sdk_kwargs"] = kwargs
        return _typed_result(_company_profile(), raw_result)

    def fake_repair(**kwargs: object):
        captured["repairs"] = int(captured["repairs"]) + 1
        captured["repair_kwargs"] = kwargs
        universe = kwargs["universe"]
        decoy_id = next(
            candidate["candidate_id"]
            for candidate in universe["candidates"]
            if candidate["url"] == DECOY_URL
        )
        return SimpleNamespace(
            final_output=_company_profile(excluded_ids=(decoy_id,)),
            usage={"available": True, "requests": 1},
            cost={"available": True, "estimated_usd": 0.001},
            request_cache={"tool_execution": {"model_tool_call_count": 0}},
            execution_telemetry={"agent_runs": 1, "model_requests": 1},
        )

    monkeypatch.setattr(
        workflow_runner,
        "run_business_research_analyst_sdk",
        fake_specialist,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_repair_work_item_specialist_decision",
        fake_repair,
    )
    monkeypatch.setattr(
        workflow_runner,
        "retrieve_company_profile_live",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("deterministic retrieval must not replace the live SDK agent")
        ),
    )
    work_item = WorkItem(
        id="wi_agent_owned_research",
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research Northstar",
        request_text=request_text,
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Northstar Behavioral", object_type="company"),
    )
    plan = _plan("business_research_analyst")

    result = workflow_runner._advance_research(
        work_item,
        request=WorkflowRunRequest(
            request_text=request_text,
            live_search=True,
            live_sdk=True,
            save=False,
            include_contact_enrichment=False,
            manual_request_plan=plan,
        ),
        store=None,
    )

    assert result.advanced is True
    assert result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert captured["repairs"] == 1
    assert captured["repair_kwargs"]["output"].name == "Northstar Behavioral"
    sdk_input = captured["sdk_input"]
    assert sdk_input.company_name == "Northstar Behavioral"
    assert request_text in json.loads(sdk_input.context)["raw_request"]
    assert captured["sdk_kwargs"]["provider_retrieval_required"] is True
    artifact = result.artifact_refs[0]
    ownership = artifact.metadata["agent_decision_ownership"]
    assert ownership["repair_mode"] == "tool_free_evidence_replay"
    assert ownership["terminal_validation"]["status"] == "accepted"
    assert result.tool_execution["model_called_tool_names"] == ["search_web"]


def test_research_work_item_can_decide_over_visible_ids_without_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_result = _raw_search_result()
    request_text = "Look into Northstar with current public evidence and keep it read-only."
    monkeypatch.setattr(
        workflow_runner,
        "run_business_research_analyst_sdk",
        lambda *_args, **_kwargs: _typed_result(
            _company_profile(excluded_ids=(DECOY_CANDIDATE_ID,)),
            raw_result,
        ),
    )
    monkeypatch.setattr(
        workflow_runner,
        "_repair_work_item_specialist_decision",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("visible provider IDs should avoid a repair")
        ),
    )
    work_item = WorkItem(
        id="wi_visible_ids_no_repair",
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research Northstar",
        request_text=request_text,
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Northstar Behavioral", object_type="company"),
    )

    result = workflow_runner._advance_research(
        work_item,
        request=WorkflowRunRequest(
            request_text=request_text,
            live_search=True,
            live_sdk=True,
            save=False,
            include_contact_enrichment=False,
            manual_request_plan=_plan("business_research_analyst"),
        ),
        store=None,
    )

    ownership = result.artifact_refs[0].metadata["agent_decision_ownership"]
    assert ownership["repair_attempted"] is False
    assert ownership["terminal_validation"]["selected_candidate_ids"] == [
        SELECTED_CANDIDATE_ID
    ]


def test_truncated_universe_blocks_before_futile_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_result = _raw_search_result(candidate_count=25)
    monkeypatch.setattr(
        workflow_runner,
        "run_business_research_analyst_sdk",
        lambda *_args, **_kwargs: _typed_result(_company_profile(), raw_result),
    )
    monkeypatch.setattr(
        workflow_runner,
        "_repair_work_item_specialist_decision",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("an incomplete universe cannot be repaired")
        ),
    )
    work_item = WorkItem(
        id="wi_truncated_universe",
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research Northstar",
        request_text="Research Northstar with current public evidence.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Northstar Behavioral", object_type="company"),
    )

    result = workflow_runner._advance_research(
        work_item,
        request=WorkflowRunRequest(
            request_text=work_item.request_text,
            live_search=True,
            live_sdk=True,
            save=False,
            manual_request_plan=_plan("business_research_analyst"),
        ),
        store=None,
    )

    assert result.status == WorkItemStatus.BLOCKED
    assert "truncated" in result.work_item.blockers[-1].message.lower()


def test_research_work_item_blocks_when_shared_repair_was_already_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_result = _raw_search_result()
    typed_result = _typed_result(_company_profile(), raw_result)
    typed_result = TypedAgentRunResult(
        **{
            **typed_result.__dict__,
            "request_cache": {
                **typed_result.request_cache,
                "decision_ownership": {"repair_attempted": True},
            },
        }
    )
    monkeypatch.setattr(
        workflow_runner,
        "run_business_research_analyst_sdk",
        lambda *_args, **_kwargs: typed_result,
    )
    monkeypatch.setattr(
        workflow_runner,
        "_repair_work_item_specialist_decision",
        lambda **_: (_ for _ in ()).throw(AssertionError("a second repair is forbidden")),
    )
    work_item = WorkItem(
        id="wi_consumed_repair",
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Research Northstar",
        request_text="Research Northstar using current public sources.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Northstar Behavioral", object_type="company"),
    )

    result = workflow_runner._advance_research(
        work_item,
        request=WorkflowRunRequest(
            request_text=work_item.request_text,
            live_search=True,
            live_sdk=True,
            save=False,
            manual_request_plan=_plan("business_research_analyst"),
        ),
        store=None,
    )

    assert result.status == WorkItemStatus.BLOCKED
    assert result.artifact_refs == []
    assert result.work_item.blockers[-1].code.endswith("agent_decision_invalid")


def test_repair_is_tool_free_and_reuses_identical_candidate_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = workflow_runner._work_item_sdk_candidate_universe(_raw_search_result())
    expected_fingerprint = workflow_runner._work_item_candidate_universe_fingerprint(
        universe
    )
    captured: dict[str, object] = {"calls": 0}

    def fake_synthesis(**kwargs: object):
        captured["calls"] = int(captured["calls"]) + 1
        agent = kwargs["agent"]
        assert agent.tools == []
        replayed = kwargs["retrieve"]()
        assert workflow_runner._work_item_candidate_universe_fingerprint(
            replayed
        ) == expected_fingerprint
        prompt = kwargs["normalize"](replayed)
        assert SELECTED_CANDIDATE_ID in prompt
        assert DECOY_CANDIDATE_ID in prompt
        audit = kwargs["input_audit_payload"]
        assert audit["candidate_universe_sha256"] == expected_fingerprint
        assert audit["provider_read_repeated"] is False
        assert audit["attached_tool_count"] == 0
        return SimpleNamespace(
            final_output=_company_profile(excluded_ids=(DECOY_CANDIDATE_ID,)),
            execution_telemetry={"agent_runs": 1, "model_requests": 1},
        )

    monkeypatch.setattr(workflow_runner, "run_retrieved_sdk_synthesis", fake_synthesis)
    monkeypatch.setattr(
        workflow_runner,
        "retrieve_company_profile_live",
        lambda **_: (_ for _ in ()).throw(AssertionError("provider reread is forbidden")),
    )
    work_item = WorkItem(
        id="wi_tool_free_repair",
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Repair source selection",
        request_text="Research Northstar with current public evidence.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Northstar Behavioral", object_type="company"),
    )
    validation = workflow_runner._work_item_decision_universe_validation(
        _company_profile(),
        universe,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )

    outcome = workflow_runner._repair_work_item_specialist_decision(
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        request=WorkflowRunRequest(
            request_text=work_item.request_text,
            live_search=True,
            live_sdk=True,
            save=False,
            manual_request_plan=_plan("business_research_analyst"),
        ),
        work_item=work_item,
        output=_company_profile(),
        universe=universe,
        validation=validation,
        sdk_session=None,
    )

    assert captured["calls"] == 1
    assert outcome.execution_telemetry["model_requests"] == 1


def test_opportunity_work_item_agent_selects_ranks_and_handoff_without_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_text = (
        "Find a promising behavioral-health company signal that is worth deeper "
        "research, explain the fit, and keep this read-only."
    )
    raw_result = _raw_search_result()
    universe = workflow_runner._work_item_sdk_candidate_universe(raw_result)
    decoy_id = next(
        candidate["candidate_id"]
        for candidate in universe["candidates"]
        if candidate["url"] == DECOY_URL
    )
    captured: dict[str, object] = {}

    def fake_specialist(sdk_input: object, **kwargs: object):
        captured["sdk_input"] = sdk_input
        captured["sdk_kwargs"] = kwargs
        return _typed_result(_opportunity_result(excluded_ids=(decoy_id,)), raw_result)

    monkeypatch.setattr(workflow_runner, "run_opportunity_scout_sdk", fake_specialist)
    monkeypatch.setattr(
        workflow_runner,
        "run_opportunity_scout_live",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("deterministic retrieval must not replace the live SDK agent")
        ),
    )
    monkeypatch.setattr(
        workflow_runner,
        "_repair_work_item_specialist_decision",
        lambda **_: (_ for _ in ()).throw(AssertionError("valid decisions need no repair")),
    )
    work_item = WorkItem(
        id="wi_agent_owned_opportunity",
        kind=WorkItemKind.OPPORTUNITY,
        title="Find a research-worthy signal",
        request_text=request_text,
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        target=WorkItemTarget(name="behavioral-health company signal", object_type="topic"),
    )

    result = workflow_runner._advance_opportunity(
        work_item,
        request=WorkflowRunRequest(
            request_text=request_text,
            live_search=True,
            live_sdk=True,
            save=False,
            manual_request_plan=_plan("opportunity_scout"),
        ),
        store=None,
    )

    assert result.advanced is True
    assert result.artifact_refs[0].title == "Northstar Behavioral"
    assert result.next_action is not None
    assert result.next_action.agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    sdk_input = captured["sdk_input"]
    assert request_text in json.loads(sdk_input.context)["raw_request"]
    assert captured["sdk_kwargs"]["provider_retrieval_required"] is True
    ownership = result.artifact_refs[0].metadata["agent_decision_ownership"]
    assert ownership["repair_attempted"] is False
    assert ownership["terminal_validation"]["status"] == "accepted"
    assert result.tool_execution["model_called_tool_names"] == ["search_web"]


@pytest.mark.parametrize(
    ("handoff", "expected_next_agent"),
    [
        (False, WorkItemRoute.OPPORTUNITY_SCOUT),
        (True, WorkItemRoute.BUSINESS_RESEARCH_ANALYST),
    ],
)
def test_opportunity_honors_validated_agent_handoff_decision(
    monkeypatch: pytest.MonkeyPatch,
    handoff: bool,
    expected_next_agent: WorkItemRoute,
) -> None:
    raw_result = _raw_search_result()
    monkeypatch.setattr(
        workflow_runner,
        "run_opportunity_scout_sdk",
        lambda *_args, **_kwargs: _typed_result(
            _opportunity_result(
                excluded_ids=(DECOY_CANDIDATE_ID,),
                handoff=handoff,
            ),
            raw_result,
        ),
    )
    request_text = (
        "Find and rank one promising behavioral-health company signal, explain the "
        "fit, and keep it read-only."
    )
    work_item = WorkItem(
        id=f"wi_opportunity_handoff_{handoff}",
        kind=WorkItemKind.OPPORTUNITY,
        title="Rank one opportunity signal",
        request_text=request_text,
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        target=WorkItemTarget(name="behavioral-health company signal", object_type="topic"),
    )

    result = workflow_runner._advance_opportunity(
        work_item,
        request=WorkflowRunRequest(
            request_text=request_text,
            live_search=True,
            live_sdk=True,
            save=False,
            manual_request_plan=_plan("opportunity_scout"),
        ),
        store=None,
    )

    assert result.next_action is not None
    assert result.next_action.agent == expected_next_agent


def test_no_live_research_remains_explicit_fixture_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow_runner,
        "run_business_research_analyst_sdk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fixture WorkItems must not invoke the SDK")
        ),
    )
    work_item = WorkItem(
        id="wi_fixture_research",
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="Fixture research",
        request_text="Research Northstar without live providers.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Northstar Behavioral", object_type="company"),
    )

    result = workflow_runner._advance_research(
        work_item,
        request=WorkflowRunRequest(
            request_text=work_item.request_text,
            live_search=False,
            live_sdk=False,
            save=False,
            include_contact_enrichment=False,
        ),
        store=None,
    )

    assert result.advanced is True
    assert any("Fixture company research" in note for note in result.audit_notes)
    assert result.tool_execution == {}
