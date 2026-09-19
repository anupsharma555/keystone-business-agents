"""Actual SDK reasoning on fixed supplied Opportunity evidence, without provider calls."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from keystone_agents import sdk
from keystone_agents import workflow_runner as workflow
from keystone_agents.runtime.durable_execution import ExecutionStore, execution_database_path
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore


class _Model(Model):
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.instructions = []

    async def get_response(self, *args, **kwargs):
        assert kwargs["tools"] == [] and kwargs["handoffs"] == []
        self.calls.append(kwargs["input"])
        self.instructions.append(kwargs["system_instructions"])
        payload = self.outputs.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"synthetic-opportunity-{len(self.calls)}",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text",
                            text=text,
                            annotations=[],
                        )
                    ],
                )
            ],
            usage=Usage(requests=1, input_tokens=120, output_tokens=80, total_tokens=200),
            response_id=f"synthetic-response-{len(self.calls)}",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class _Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def _sources():
    return [
        WorkItemSourceRef(
            source_id="source-old",
            title="Earlier Example Systems notice",
            url="https://example.test/old",
            source_type="company_site",
            provider="supplied",
            extraction_status="supplied_material",
            supported_claim="Example Systems closed the earlier application window.",
        ),
        WorkItemSourceRef(
            source_id="source-current",
            title="Current Example Systems evidence",
            url="https://example.test/current",
            source_type="company_site",
            provider="supplied",
            extraction_status="supplied_material",
            evidence_excerpt="Example Systems now invites independent evaluation-design advice.",
        ),
    ]


def _output(*, subject="Example Systems", selected="source-current", no_action=False, more=False):
    sources = _sources()
    source = sources[1]
    records = (
        []
        if no_action
        else [
            {
                "company_name": subject,
                "opportunity_type": "clinical AI",
                "opportunity_kind": "other",
                "opportunity_status": "unknown",
                "detail_verification_status": "unverified",
                "priority_score": 60,
                "outside_consulting_likelihood": 50,
                "why_now_signal": (
                    "The supplied current notice describes evaluation-design interest."
                ),
                "recommended_next_step": "Propose a bounded evaluation discussion for review.",
                "keystone_fit_reason": (
                    "The evidence supports a proposed evaluation-design discussion."
                ),
                "missing_evidence": ["The proposed scope and its impact have not been validated."],
                "sources": [
                    {
                        "source_id": source.source_id,
                        "provider_candidate_id": selected,
                        "title": source.title,
                        "url": source.url,
                        "source_type": "company_site",
                        "supported_signal": source.evidence_excerpt,
                    }
                ],
                "source_signals": ["evaluation-design interest"],
                "handoff_to_business_research_analyst": False,
                "approval_required_before_outreach": True,
                "approved_for_outreach": False,
            }
        ]
    )
    return OpportunityScoutResult.model_validate(
        {
            "topic": subject,
            "dry_run": True,
            "search_provider": "source-provided",
            "human_summary": (
                "The supplied sources conflict; request clarification before acting."
                if more
                else "No action is warranted from these supplied sources."
                if no_action
                else (
                    "Propose an evaluation-design discussion using the current notice; "
                    "its scope remains unverified."
                )
            ),
            "records": records,
            "decision": {
                "decision_owner": "specialist_agent",
                "decision_stage": "opportunity_candidate_selection",
                "selected_candidate_ids": [] if no_action else [selected],
                "candidate_assessments": [
                    {
                        "candidate_id": item.source_id,
                        "disposition": "needs_more_context"
                        if more
                        else "selected"
                        if not no_action and item.source_id == selected
                        else "excluded",
                        "rationale": "Compared supplied timing, scope, and uncertainty.",
                    }
                    for item in sources
                ],
                "reasoning": "The current evidence and its limits govern this recommendation.",
                "limitations": ["No external retrieval or eligibility verification was performed."],
                "needs_more_context": more,
            },
        }
    ).model_dump(mode="json")


def _case(tmp_path, *, sources=None, handoff=False, request_text=None):
    database = f"sqlite:///{tmp_path / 'business.db'}"
    store = SQLiteStore(database)
    text = request_text or (
        "Use only this supplied evidence to assess one practical direction for Example Systems. "
        "Do not retrieve sources, draft outreach, or write externally."
    )
    plan = {
        "source": "test",
        "target_agent": "opportunity_scout",
        "requested_agent": "opportunity_scout",
        "intent": "opportunity_search",
        "primary_target": "Use only this supplied evidence",
        "target_type": "company",
        "requires_live_search": False,
        "desired_count": 1,
        "workflow": ["business_research_analyst", "opportunity_scout"] if handoff else [],
    }
    evidence = _sources() if sources is None else sources
    item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Synthetic supplied opportunity",
        request_text=text,
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        target=WorkItemTarget(
            name="Example Systems",
            object_type="company",
            metadata={
                "manual_request_plan": plan,
            },
        ),
        sources=evidence,
    )
    if handoff:
        item.artifact_refs = [
            WorkItemArtifactRef(
                artifact_type="research_brief",
                artifact_id="prior-research",
                selected=True,
                source_agent="business_research_analyst",
                approval_state="approved_for_research",
                title="Example Systems",
                summary="Accepted Research summary.",
                metadata={
                    "schema": "keystone.supplied_business_research_sdk.v1",
                    "model_synthesized": True,
                    "research_brief": {
                        "target_name": "Example Systems",
                        "target_type": "company",
                        "summary": "The model-owned prior finding must reach Opportunity.",
                        "key_findings": ["A specific evaluation gap remains unresolved."],
                        "source_ids_used": [source.source_id for source in evidence],
                    },
                    "source_refs": [source.model_dump(mode="json") for source in evidence],
                    "sdk_agent_run_id": "prior-research",
                },
            )
        ]
    store.save_work_item(item)
    request = WorkflowRunRequest(
        request_text="continue" if handoff else text,
        work_item_id=item.id,
        requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        manual_request_plan=plan,
        database_url=database,
        save=True,
        live_sdk=True,
        live_search=False,
        sdk_session_enabled=False,
        execution_id="synthetic-opportunity-execution",
    )
    return store, item, request


def _inject(monkeypatch, outputs):
    model = _Model(outputs)
    monkeypatch.setattr(
        sdk, "build_live_run_config", lambda *_a, **_kw: build_local_run_config(_Provider(model))
    )
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_SCOUT_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "0")
    monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries", lambda **_: 0)
    for name in (
        "_source_provided_opportunity_scout_result",
        "_planned_company_profile_opportunity_result",
        "run_opportunity_scout_live",
        "scout_opportunities_fixture",
    ):
        monkeypatch.setattr(
            workflow,
            name,
            lambda *_a, **_kw: pytest.fail("Live supplied path used retrieval or a template"),
        )
    return model


def _execute(request, lane):
    if lane == "direct":
        return workflow.advance_work_item(request)
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from keystone_agents.langgraph_workflow import run_work_item_langgraph

    return run_work_item_langgraph(request, manager_loop=False).result


@pytest.mark.parametrize("lane", ["direct", "graph"])
@pytest.mark.parametrize("handoff", [False, True])
def test_supplied_opportunity_uses_registered_sdk_without_tools_and_persists_exact_run(
    tmp_path,
    monkeypatch,
    lane,
    handoff,
):
    store, item, request = _case(tmp_path, handoff=handoff)
    output = _output()
    model = _inject(monkeypatch, [output])
    with activate_model_request_budget(3) as budget:
        result = _execute(request, lane)
        assert budget.consumed == 1
    assert len(model.calls) == 1
    assert "No tools are attached in this phase" in model.instructions[0]
    assert "<!-- opportunity_scout_supplied_evidence.md -->" in model.instructions[0]
    assert "/SKILL.md -->" not in model.instructions[0]
    assert "shared output normalizer owns deterministic numeric calculation" in (
        " ".join(model.instructions[0].split())
    )
    model_input = str(model.calls[0])
    assert item.request_text in model_input
    assert "OpportunityContextPack" in model_input
    assert "source-old" in model_input and "source-current" in model_input
    assert "closed the earlier application" in model_input
    assert "invites independent evaluation-design advice" in model_input
    if handoff:
        assert "model-owned prior finding must reach Opportunity" in model_input
        assert "A specific evaluation gap remains unresolved" in model_input
        assert result.next_action.agent != WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.work_item.target.name == "Example Systems"
    assert result.human_summary == output["human_summary"]
    assert len(result.artifact_refs) == 1
    artifact = result.artifact_refs[0]
    run = store.get_agent_run(artifact.metadata["sdk_agent_run_id"])
    assert run["agent_name"] == "opportunity_scout" and run["dry_run"] == 0
    assert run["output"]["usage"]["requests"] == 1
    assert run["output"]["usage"]["total_tokens"] == 200
    assert run["output"]["cost"]["estimated_usd"] > 0
    assert run["output"]["request_cache"]["tool_execution"]["model_tool_call_count"] == 0
    assert artifact.metadata["source_provided"] is True
    assert artifact.approval_state == "pending"
    journal = ExecutionStore(execution_database_path(request.database_url))
    assert journal.get(request.execution_id)["consumed"] == 1


@pytest.mark.parametrize("kind", ["empty", "identity_conflict"])
def test_empty_or_conflicting_supplied_identity_packet_blocks_before_model(
    tmp_path, monkeypatch, kind
):
    sources = (
        []
        if kind == "empty"
        else [
            _sources()[0],
            _sources()[1].model_copy(update={"source_id": "source-old"}),
        ]
    )
    store, item, request = _case(tmp_path, sources=sources)
    model = _inject(monkeypatch, [])
    with activate_model_request_budget(2) as budget:
        result = _execute(request, "direct")
        assert budget.consumed == 0
    assert model.calls == []
    assert result.status == WorkItemStatus.BLOCKED and result.artifact_refs == []
    expected_code = (
        "source_sufficiency_required" if kind == "empty" else "opportunity_scout_execution_failed"
    )
    assert expected_code in {blocker.code for blocker in result.blockers}


@pytest.mark.parametrize("more", [False, True])
def test_supplied_model_can_choose_no_action_or_report_conflicting_evidence(
    tmp_path, monkeypatch, more
):
    store, item, request = _case(tmp_path)
    output = _output(no_action=True, more=more)
    model = _inject(monkeypatch, [output])
    result = _execute(request, "direct")
    assert len(model.calls) == 1
    instructions = " ".join(model.instructions[0].split())
    assert "supported no-action result: `records=[]`" in instructions
    assert "no selected IDs, and `needs_more_context=false`" in instructions
    assert "required evidence is missing and prevents a responsible decision" in instructions
    assert result.artifact_refs == []
    assert result.status == (WorkItemStatus.NEEDS_CONTEXT if more else WorkItemStatus.DONE)
    assert result.human_summary == output["human_summary"]
    rows = [
        row for row in store.fetch_all("agent_runs") if row["agent_name"] == "opportunity_scout"
    ]
    assert len(rows) == 1 and json.loads(rows[0]["output_json"])["usage"]["requests"] == 1


def test_wrong_evidence_identity_gets_one_tool_free_sdk_repair(tmp_path, monkeypatch):
    store, item, request = _case(tmp_path)
    invalid = _output(selected="invented-source")
    model = _inject(monkeypatch, [invalid, _output()])
    with activate_model_request_budget(3) as budget:
        result = _execute(request, "direct")
        assert budget.consumed == 2
    assert len(model.calls) == 2 and len(result.artifact_refs) == 1
    run = store.get_agent_run(result.artifact_refs[0].metadata["sdk_agent_run_id"])
    assert run["output"]["usage"]["requests"] == 2
    assert run["output"]["request_cache"]["decision_ownership"]["repair_attempted"] is True


def test_repeated_invalid_subject_never_falls_back_to_canned_opportunities(tmp_path, monkeypatch):
    store, item, request = _case(tmp_path)
    invalid = _output(subject="Fabricated External Company")
    model = _inject(monkeypatch, [invalid, invalid])
    result = _execute(request, "direct")
    assert len(model.calls) == 2
    assert result.status == WorkItemStatus.BLOCKED and result.artifact_refs == []
    rows = [
        row for row in store.fetch_all("agent_runs") if row["agent_name"] == "opportunity_scout"
    ]
    assert len(rows) == 1 and rows[0]["status"] == "error"
    assert json.loads(rows[0]["output_json"])["sdk_run_failure"]["usage"]["requests"] == 2


def test_new_proposal_can_use_supplied_subject_without_claiming_an_established_program(
    tmp_path,
    monkeypatch,
):
    sources = [
        source.model_copy(
            update={
                "title": f"Supplied workflow guide {index}",
                "supported_claim": "",
                "evidence_excerpt": (
                    "The guide describes measuring restart correctness before deployment."
                ),
            }
        )
        for index, source in enumerate(_sources())
    ]
    store, item, request = _case(tmp_path, sources=sources)
    output = _output()
    output["human_summary"] = (
        "Propose a measured replay experiment for Example Systems. "
        "This is an unvalidated local idea, not an established external program."
    )
    record = output["records"][0]
    record["why_now_signal"] = "The supplied guide describes a pattern worth testing locally."
    record["recommended_next_step"] = "Propose an offline replay experiment for human review."
    record["sources"][0]["title"] = sources[1].title
    record["sources"][0]["supported_signal"] = sources[1].evidence_excerpt
    record["missing_evidence"] = [
        "No trial establishes whether this proposed change improves outcomes."
    ]
    model = _inject(monkeypatch, [output])
    result = _execute(request, "direct")
    assert len(model.calls) == 1 and len(result.artifact_refs) == 1
    assert result.human_summary == output["human_summary"]
    metadata = result.artifact_refs[0].metadata
    assert metadata["opportunity_status"] == "unknown"
    assert metadata["detail_verification_status"] == "unverified"
    assert metadata["deadline"] == metadata["application_or_contact_path"] == ""
    assert metadata["agent_run_id"] == metadata["sdk_agent_run_id"]


@pytest.mark.parametrize("field", ["citation", "approval", "entity_key"])
def test_supplied_output_must_preserve_citations_and_cannot_grant_authority_or_ids(
    tmp_path,
    monkeypatch,
    field,
):
    store, item, request = _case(tmp_path)
    output = _output()
    if field == "citation":
        output["records"][0]["sources"][0]["source_id"] = "invented-citation"
    elif field == "approval":
        output["records"][0]["approved_for_outreach"] = True
    else:
        output["records"][0]["canonical_entity_key"] = "invented-provider-object"
    model = _inject(monkeypatch, [output, output])
    result = _execute(request, "direct")
    assert len(model.calls) == 2
    assert result.status == WorkItemStatus.BLOCKED and result.artifact_refs == []


def test_conflicting_citation_ids_with_distinct_provider_ids_block_before_model(
    tmp_path,
    monkeypatch,
):
    sources = [
        source.model_copy(
            update={
                "source_id": "same-citation",
                "provider_candidate_id": f"p-{index}",
            }
        )
        for index, source in enumerate(_sources())
    ]
    store, item, request = _case(tmp_path, sources=sources)
    model = _inject(monkeypatch, [])
    result = _execute(request, "direct")
    assert model.calls == []
    assert result.status == WorkItemStatus.BLOCKED
    assert "conflicting source identity" in result.human_summary


@pytest.mark.parametrize("oversized", ["source", "artifact"])
def test_oversized_supplied_material_blocks_without_truncation_or_model_call(
    tmp_path,
    monkeypatch,
    oversized,
):
    store, item, request = _case(tmp_path, handoff=oversized == "artifact")
    if oversized == "source":
        item.sources[1].evidence_excerpt = "x" * 24001
    else:
        item.artifact_refs[0].metadata["research_brief"]["key_findings"] = ["x" * 48001]
    store.save_work_item(item)
    model = _inject(monkeypatch, [])
    result = _execute(request, "direct")
    assert model.calls == []
    assert result.status == WorkItemStatus.BLOCKED and result.artifact_refs == []
    assert "bound" in result.human_summary


@pytest.mark.parametrize("lane", ["direct", "graph"])
def test_completed_research_brief_is_not_repeated_by_manager_continuation(
    tmp_path,
    monkeypatch,
    lane,
):
    store, item, request = _case(tmp_path, handoff=True)
    model = _inject(monkeypatch, [_output()])
    monkeypatch.setattr(
        workflow,
        "_advance_research",
        lambda *_a, **_kw: pytest.fail("Repeated accepted Research"),
    )
    if lane == "direct":
        result = workflow.advance_work_item_manager_loop(request, max_steps=3)
    else:
        pytest.importorskip("langgraph.checkpoint.sqlite")
        from keystone_agents.langgraph_workflow import run_work_item_langgraph

        result = run_work_item_langgraph(request, manager_loop=True, max_manager_steps=3).result
    assert len(model.calls) == 1
    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT
    assert (
        result.next_action is None
        or result.next_action.agent != WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    )


def test_formal_gate_cannot_replace_supplied_sdk_selection_when_live_search_is_disabled(
    tmp_path,
    monkeypatch,
):
    store, item, request = _case(
        tmp_path,
        request_text="Find an active grant or RFP with deadline and eligibility evidence.",
    )
    output = _output()  # Intentionally lacks the requested formal-opportunity proof.
    model = _inject(monkeypatch, [output])
    result = _execute(request, "direct")
    assert len(model.calls) == 1
    assert result.status == WorkItemStatus.BLOCKED and result.artifact_refs == []
    assert output["human_summary"] not in result.human_summary
    assert "formal-opportunity selection" in result.human_summary


def test_inference_projection_preserves_every_distinct_value_without_mutating_pack():
    pack = {
        "target": {"name": "Example Systems", "metadata": {"approved": False}},
        "approved_facts": [{"text": "Source-backed finding", "source_ids": ["source-current"]}],
        "source_refs": [{"source_id": "source-old"}, {"source_id": "source-current"}],
        "selected_artifacts": [{"metadata": {"research_brief": {"summary": "Model answer"}}}],
        "approval_gates": [{"approved": False}],
        "limitation_notes": ["No permission for external actions."],
    }
    pack["summary"] = {
        "title": "Distinct title retained",
        "target": {"metadata": {"approved": False}, "name": "Example Systems"},
        "facts": deepcopy(pack["approved_facts"]),
        "sources": deepcopy(pack["source_refs"]),
        "artifact_refs": deepcopy(pack["selected_artifacts"]),
        "next_action": {"requires_approval": True},
    }
    before = json.dumps(pack, sort_keys=True)

    projected = workflow._supplied_opportunity_context_pack_projection(pack)

    assert json.dumps(pack, sort_keys=True) == before
    assert projected["summary"] == {
        "title": "Distinct title retained",
        "next_action": {"requires_approval": True},
    }
    assert {key: value for key, value in projected.items() if key != "summary"} == {
        key: value for key, value in pack.items() if key != "summary"
    }


@pytest.mark.parametrize(
    "pack,summary",
    [
        ({}, {"sources": None}),
        ({"source_refs": [{"approved": 1}]}, {"sources": [{"approved": True}]}),
        ({"source_refs": [{"count": 1}]}, {"sources": [{"count": 1.0}]}),
        ({"source_refs": ["first", "second"]}, {"sources": ["second", "first"]}),
        ({"approved_facts": ["approved finding"]}, {"facts": ["unapproved distinct finding"]}),
        ({"selected_artifacts": []}, {"artifact_refs": [{"metadata": {"unique": "Keep this"}}]}),
    ],
)
def test_inference_projection_retains_missing_counterparts_and_type_or_content_differences(
    pack,
    summary,
):
    pack = {**pack, "summary": summary}
    before = json.dumps(pack, sort_keys=True)
    projected = workflow._supplied_opportunity_context_pack_projection(pack)
    assert json.dumps(projected, sort_keys=True) == before
    assert json.dumps(pack, sort_keys=True) == before


def test_inference_projection_does_not_hide_unique_oversized_summary_evidence():
    pack = {
        "selected_artifacts": [],
        "summary": {"artifact_refs": [{"metadata": {"unique_evidence": "x" * 48001}}]},
    }
    projected = workflow._supplied_opportunity_context_pack_projection(pack)
    with pytest.raises(ValueError, match="48000-character"):
        workflow._check_supplied_opportunity_context_bound({"context_pack": projected})
    assert projected == pack


@pytest.mark.parametrize("lane", ["direct", "graph"])
def test_large_accepted_research_handoff_reaches_sdk_once_with_complete_evidence(
    tmp_path,
    monkeypatch,
    lane,
):
    from keystone_agents.work_items import build_opportunity_context_pack

    store, item, request = _case(tmp_path, handoff=True)
    brief = ResearchBrief(
        target_name="Example Systems",
        target_type="company",
        summary="The accepted Research answer remains model-owned.",
        key_findings=[
            f"Finding {index}: Evaluation-design interest is supplied, but scope, impact, "
            "eligibility, and approval remain unverified."
            for index in range(250)
        ],
        limitations=["No external retrieval or outreach authority was granted."],
        source_ids_used=[source.source_id for source in item.sources],
        sources=[
            {
                "source_id": source.source_id,
                "title": source.title,
                "url": source.url,
                "source_type": source.source_type,
            }
            for source in item.sources
        ],
        decision={**_output()["decision"], "decision_stage": "research_source_selection"},
    )
    item.artifact_refs[0].metadata["research_brief"] = brief.model_dump(mode="json")
    store.save_work_item(item)
    artifact_before = item.artifact_refs[0].model_dump(mode="json")
    pack_before = build_opportunity_context_pack(item).model_dump(mode="json")
    with pytest.raises(ValueError, match="48000-character"):
        workflow._check_supplied_opportunity_context_bound({"context_pack": pack_before})
    model = _inject(monkeypatch, [_output()])

    with activate_model_request_budget(2) as budget:
        result = _execute(request, lane)
        assert budget.consumed == 1

    assert len(model.calls) == 1 and len(result.artifact_refs) == 1
    prompt = next(entry["content"] for entry in model.calls[0] if entry.get("role") == "user")
    context, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"') :])
    pack = context["context_pack"]
    workflow._check_supplied_opportunity_context_bound(context)
    assert pack["selected_artifacts"][0] == artifact_before
    assert pack["source_refs"] == pack_before["source_refs"]
    assert pack["ordered_sources"] == pack_before["ordered_sources"]
    assert pack["approval_gates"] == pack_before["approval_gates"]
    assert pack["limitation_notes"] == pack_before["limitation_notes"]
    assert context["raw_request"] == item.request_text
    assert "artifact_refs" not in pack["summary"]
    saved = store.get_work_item(item.id)
    assert saved is not None
    assert saved.artifact_refs[0].model_dump(mode="json") == artifact_before
    assert build_opportunity_context_pack(item).model_dump(mode="json") == pack_before


def test_tool_free_score_placeholders_are_normalized_without_changing_the_selection(
    tmp_path,
    monkeypatch,
):
    from keystone_agents.agents.opportunity_scout import score_opportunity_impl

    store, item, request = _case(tmp_path)
    output = _output()
    output["records"][0]["priority_score"] = 0
    output["records"][0]["outside_consulting_likelihood"] = 0
    model = _inject(monkeypatch, [output])
    result = _execute(request, "direct")
    assert result.advanced and len(model.calls) == 1
    row = store.get_agent_run(result.artifact_refs[0].metadata["agent_run_id"])["output"]
    record = row["records"][0]
    expected = json.loads(
        score_opportunity_impl(
            company_name=output["records"][0]["company_name"],
            opportunity_type=output["records"][0]["opportunity_type"],
            signals=output["records"][0]["source_signals"],
        )
    )
    assert record["priority_score"] == expected["priority_score"] > 0
    assert record["outside_consulting_likelihood"] == expected["outside_consulting_likelihood"]
    assert record["source_signals"] == output["records"][0]["source_signals"]
    assert row["decision"] == output["decision"]
    assert row["human_summary"] == output["human_summary"]
    assert row["request_cache"]["tool_execution"]["model_tool_call_count"] == 0
    assert row["request_cache"]["decision_normalizations"]


def test_no_action_repair_does_not_invent_a_missing_context_requirement(tmp_path, monkeypatch):
    store, item, request = _case(tmp_path)
    invalid = _output(no_action=True)
    invalid["decision"]["candidate_assessments"] = invalid["decision"]["candidate_assessments"][:1]
    model = _inject(monkeypatch, [invalid, _output(no_action=True)])
    result = _execute(request, "direct")
    assert len(model.calls) == 2
    repair_input = " ".join(str(model.calls[1]).split())
    assert "a supported no-action decision may return no selection" in repair_input
    assert "needs_more_context=false" in repair_input
    assert result.status == WorkItemStatus.DONE and not result.artifact_refs
    assert result.human_summary == _output(no_action=True)["human_summary"]


@pytest.mark.parametrize("more", [False, True])
def test_compact_synthesis_preserves_supported_no_action_vs_missing_context(monkeypatch, more):
    from keystone_agents.agent_decision_contracts import (
        opportunity_scout_synthesis_decision_contract,
    )
    from keystone_agents.agents.opportunity_scout import build_opportunity_scout_synthesis_agent
    from keystone_agents.models import OpportunityScoutSDKInput
    from keystone_agents.run import run_typed_sdk_agent
    from keystone_agents.schemas.opportunity import OpportunityScoutSynthesis

    retrieved = OpportunityScoutResult.model_validate(_output())
    payload = OpportunityScoutSynthesis(
        decisions=[],
        audit_summary="Required evidence is missing."
        if more
        else "The supplied evidence supports no action.",
        decision={
            "decision_stage": "opportunity_candidate_selection",
            "needs_more_context": more,
            "candidate_assessments": [
                {
                    "candidate_id": "source-current",
                    "disposition": "needs_more_context" if more else "excluded",
                    "rationale": "Decision-critical evidence missing."
                    if more
                    else "The available evidence supports exclusion.",
                }
            ],
            "reasoning": "Assessed the supplied record before choosing no selection.",
        },
    )
    model = _Model([payload.model_dump(mode="json")])
    result = run_typed_sdk_agent(
        agent=build_opportunity_scout_synthesis_agent(max_results=1),
        typed_input=OpportunityScoutSDKInput(
            topic="Supplied review", max_results=1, context=retrieved.model_dump_json()
        ),
        output_type=OpportunityScoutSynthesis,
        run_config=build_local_run_config(_Provider(model)),
        decision_contract=opportunity_scout_synthesis_decision_contract(retrieved),
    )
    assert len(model.calls) == 1
    assert result.output.decision.needs_more_context is more
    assert result.output.decision.selected_candidate_ids == []
    assert "supported no-action decision" in " ".join(model.instructions[0].split())


def test_required_selection_repair_still_requires_an_explicit_missing_context_outcome():
    from keystone_agents.agent_decision_contracts import opportunity_scout_decision_contract
    from keystone_agents.runtime.decision_validation import (
        SpecialistDecisionEvidence,
        decision_repair_prompt,
    )
    from keystone_agents.schemas.decision_ownership import DecisionValidatorOutcome

    prompt = decision_repair_prompt(
        opportunity_scout_decision_contract(),
        SpecialistDecisionEvidence.build(("source-current",), selection_required=True),
        DecisionValidatorOutcome(
            status="repair_required", feedback="Assess the required evidence."
        ),
    )
    assert "return no selection and set needs_more_context=true" in prompt
    assert "a supported no-action decision may return" not in prompt


def test_supplied_profile_scopes_bookkeeping_without_changing_model_or_tools():
    from dataclasses import asdict

    from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
    from keystone_agents.schemas.opportunity import SuppliedOpportunityResult

    options = {
        "request_text": "An inspectable field/attachment/approval/handoff artifact; no writes.",
        "attach_tools": False,
        "model": "gpt-5.4-mini",
        "compact_instructions": True,
    }
    default = build_opportunity_scout_agent(**options)
    supplied = build_opportunity_scout_agent(**options, instruction_profile="supplied_evidence")
    assert default.name == supplied.name == "opportunity_scout"
    assert default.output_type is OpportunityScoutResult
    assert supplied.output_type is SuppliedOpportunityResult
    assert issubclass(supplied.output_type, default.output_type)
    assert default.model == supplied.model
    assert asdict(default.model_settings) == asdict(supplied.model_settings)
    assert default.tools == supplied.tools == []
    assert "<!-- opportunity_scout.md -->" in default.instructions
    assert "<!-- opportunity_scout.md -->" not in supplied.instructions
    assert "/SKILL.md -->" not in supplied.instructions
    for policy in (
        "repo_runtime_policy.md",
        "memory_policy.md",
        "writing_style.md",
        "safety_policy.md",
        "keystone_profile.md",
    ):
        assert f"<!-- {policy} -->" in supplied.instructions
    assert len(supplied.instructions) < 25_000


@pytest.mark.parametrize(
    "options",
    [
        {"attach_tools": True},
        {"attach_tools": False, "include_fixture_tools": True},
    ],
)
def test_supplied_profile_cannot_attach_tools(options):
    from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent

    with pytest.raises(ValueError, match="requires a tool-free agent"):
        build_opportunity_scout_agent(instruction_profile="supplied_evidence", **options)


def test_supplied_profile_preserves_exact_actual_input_without_loading_source_triggered_skills(
    tmp_path,
    monkeypatch,
):
    store, item, request = _case(tmp_path, handoff=True)
    marker = "An inspectable attachment has table/field/artifact/approval/handoff labels."
    item.sources[0].evidence_excerpt = marker
    output = _output()
    model = _inject(monkeypatch, [output, output])
    original_sdk = workflow.run_opportunity_scout_sdk
    workflow._run_supplied_opportunity_sdk(
        item,
        request=request,
        topic=item.target.name,
        store=None,
        sdk_session=None,
    )

    def discovery_input_control(typed_input, **kwargs):
        kwargs["instruction_profile"] = "discovery"
        return original_sdk(typed_input, **kwargs)

    monkeypatch.setattr(workflow, "run_opportunity_scout_sdk", discovery_input_control)
    workflow._run_supplied_opportunity_sdk(
        item,
        request=request,
        topic=item.target.name,
        store=None,
        sdk_session=None,
    )
    assert len(model.calls) == 2
    assert model.calls[0] == model.calls[1]
    assert marker in str(model.calls[0])
    assert "model-owned prior finding must reach Opportunity" in str(model.calls[0])
    assert "/SKILL.md -->" not in model.instructions[0]
    assert "<!-- data_schema_mapping/SKILL.md -->" in model.instructions[1]
    assert len(model.instructions[0]) < len(model.instructions[1])


def test_supplied_public_opportunity_retains_supported_deadline_and_application_details(
    tmp_path,
    monkeypatch,
):
    source = _sources()[1].model_copy(
        update={
            "title": "Example Systems official evaluation call",
            "source_type": "government",
            "evidence_excerpt": (
                "Example Systems accepts evaluation proposals through 2031-08-12. "
                "Eligible applicants are independent evaluation organizations. "
                "Apply at https://example.test/apply-evaluation."
            ),
        }
    )
    store, item, request = _case(tmp_path, sources=[_sources()[0], source])
    output = _output()
    record = output["records"][0]
    record.update(
        {
            "opportunity_kind": "contract_or_rfp",
            "opportunity_status": "open",
            "deadline": "2031-08-12",
            "detail_verification_status": "page_verified",
            "eligibility_summary": "Independent evaluation organizations.",
            "application_or_contact_path": "https://example.test/apply-evaluation",
        }
    )
    record["sources"][0].update(
        title=source.title, source_type=source.source_type, supported_signal=source.evidence_excerpt
    )
    model = _inject(monkeypatch, [output])
    result = _execute(request, "direct")
    assert result.advanced and len(model.calls) == 1
    assert "2031-08-12" in str(model.calls[0])
    saved = store.get_agent_run(result.artifact_refs[0].metadata["agent_run_id"])["output"][
        "records"
    ][0]
    for field in (
        "opportunity_kind",
        "opportunity_status",
        "deadline",
        "detail_verification_status",
        "eligibility_summary",
        "application_or_contact_path",
    ):
        assert saved[field] == record[field]
    assert "preserve its supported sponsor, kind, status, deadline, eligibility" in (
        " ".join(model.instructions[0].split())
    )


@pytest.mark.parametrize("lane", ["direct", "graph"])
@pytest.mark.parametrize("legacy_profile", [False, True])
def test_substantive_assessment_without_formal_records_survives_manager_and_rendering(
    tmp_path,
    monkeypatch,
    supplied_research_fake_sdk,
    lane,
    legacy_profile,
):
    from keystone_agents.presentation.public_result import ensure_work_item_user_facing_summary
    from keystone_agents.presentation.renderers import render_work_item_result_text
    from keystone_agents.schemas.work_item import UserFacingSummaryAuthority

    store, item, request = _case(tmp_path, handoff=True)
    text = (
        "Use only the supplied facts and limitations for Example Systems. Have Business "
        "Research review them, then have Opportunity Scout assess whether an internal "
        "implementation experiment is useful. Include source links. Do not search, "
        "draft outreach, or write externally."
    )
    plan = {
        **request.manual_request_plan,
        "primary_target": "Example Systems",
        "target_agent": "business_research_analyst",
        "intent": "research_brief",
    }
    item.request_text = text
    item.current_route = WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    item.target.metadata["manual_request_plan"] = plan
    item.artifact_refs = []
    if legacy_profile:
        item.artifact_refs.append(
            WorkItemArtifactRef(
                artifact_type="company_profile",
                artifact_id="legacy-profile",
                title="Previous Example Systems profile",
                source_agent="business_research_analyst",
                approval_state="approved_for_research",
                selected=False,
                summary="A historical profile; it does not contain this assessment.",
                metadata={"source_refs": [source.model_dump(mode="json") for source in _sources()]},
            )
        )
    store.save_work_item(item)
    request = request.model_copy(
        update={
            "request_text": text,
            "manual_request_plan": plan,
            "requested_route": WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        }
    )
    summary = (
        "A small internal replay experiment is justified, but no existing business "
        "category accurately describes it. Pending-input recovery remains uncertain; "
        "self-hosted compute ownership is a separate limitation. "
        "Sources: https://example.test/current and https://example.test/old."
    )
    output = _output(no_action=True)
    output["human_summary"] = summary
    model = _inject(monkeypatch, [output])
    if lane == "graph":
        pytest.importorskip("langgraph.checkpoint.sqlite")
        from keystone_agents.langgraph_workflow import run_work_item_langgraph

        result = run_work_item_langgraph(request, manager_loop=True, max_manager_steps=3).result
    else:
        result = workflow.advance_work_item_manager_loop(request, max_steps=3)
    assert len(supplied_research_fake_sdk) == 1 and len(model.calls) == 1
    assert result.route == WorkItemRoute.OPPORTUNITY_SCOUT and result.status == WorkItemStatus.DONE
    assert result.user_facing_summary_authority == UserFacingSummaryAuthority.CANONICAL
    assert result.artifact_refs == []
    assert result.human_summary == summary
    guarded, verified = ensure_work_item_user_facing_summary(result)
    assert verified and guarded.human_summary == summary
    assert summary in render_work_item_result_text(guarded)


def _unobserved_quality():
    return {
        "url": "https://example.test/current",
        "title": "Current Example Systems evidence",
        "source_type": "company_site",
        "credibility_score": 95,
        "domain_credibility_score": 95,
        "recency_score": 95,
        "relevance_score": 95,
        "overall_score": 95,
        "rationale": "Model-invented numerical quality from an official-looking source.",
    }


def _unobserved_quality_summary():
    return {
        "source_count": 1,
        "independent_source_count": 1,
        "average_credibility_score": 95,
        "average_domain_credibility_score": 95,
        "average_recency_score": 95,
        "average_relevance_score": 95,
        "overall_score": 95,
        "high_quality_source_count": 1,
        "low_quality_source_count": 0,
        "rationale": "Model-invented aggregate quality.",
    }


@pytest.mark.parametrize(
    "field",
    [
        "source_quality",
        "record_quality",
        "root_quality",
        "bundle_source_quality",
        "bundle_quality",
        "root_bundle_quality",
        "raw_count",
        "deduped_count",
        "lanes",
        "windows",
        "record_lanes",
    ],
)
def test_supplied_provenance_rejects_or_repairs_without_silent_correction(
    tmp_path, monkeypatch, field
):
    store, item, request = _case(tmp_path)
    invalid = _output()
    record = invalid["records"][0]
    if field == "source_quality":
        record["sources"][0]["source_quality"] = _unobserved_quality()
    elif field == "record_quality":
        record["source_quality_summary"] = _unobserved_quality_summary()
    elif field == "root_quality":
        invalid["source_quality_summary"] = _unobserved_quality_summary()
    elif field in {"bundle_source_quality", "bundle_quality", "root_bundle_quality"}:
        bundle = {
            "bundle_id": "supplied-bundle",
            "company_name": "Example Systems",
            "source_category": "company_page",
            "summary": "Supplied context only.",
            "sources": deepcopy(record["sources"]),
        }
        if field == "bundle_source_quality":
            bundle["sources"][0]["source_quality"] = _unobserved_quality()
        else:
            bundle["source_quality_summary"] = _unobserved_quality_summary()
        if field == "root_bundle_quality":
            invalid["source_bundles"] = [bundle]
        else:
            record["source_bundles"] = [bundle]
    elif field == "raw_count":
        invalid["raw_search_result_count"] = 4
    elif field == "deduped_count":
        invalid["deduped_candidate_count"] = 4
    elif field == "lanes":
        invalid["search_lanes"] = ["pretended-search"]
    elif field == "windows":
        invalid["search_time_windows"] = ["pretended-window"]
    else:
        record["search_lanes"] = ["pretended-search"]
    model = _inject(monkeypatch, [invalid, _output()])
    result = _execute(request, "direct")
    if field in {"raw_count", "deduped_count", "lanes", "windows", "record_lanes"}:
        # These fields are no longer emitted by the strict phase schema. A fake
        # provider violating it is rejected at parsing, before semantic repair.
        assert not result.advanced
        assert not result.artifact_refs
        assert len(model.calls) == 1
        return
    assert result.advanced and len(model.calls) == 2
    repair_input = str(model.calls[1])
    assert "supplied_provenance_unobserved" in repair_input
    expected_path = {
        "source_quality": "records[0].sources[0].source_quality expected null",
        "record_quality": "records[0].source_quality_summary expected null",
        "root_quality": "output.source_quality_summary expected null",
        "bundle_source_quality": (
            "records[0].source_bundles[0].sources[0].source_quality expected null"
        ),
        "bundle_quality": "records[0].source_bundles[0].source_quality_summary expected null",
        "root_bundle_quality": "output.source_bundles[0].source_quality_summary expected null",
        "raw_count": "raw_search_result_count expected 0",
        "deduped_count": "deduped_candidate_count expected 0",
        "lanes": "search_lanes expected []",
        "windows": "search_time_windows expected []",
        "record_lanes": "records[0].search_lanes expected []",
    }[field]
    assert expected_path in repair_input
    assert "source-quality acquisition" in repair_input
    row = store.get_agent_run(result.artifact_refs[0].metadata["agent_run_id"])["output"]
    assert row["usage"]["requests"] == 2
    assert row["request_cache"]["decision_ownership"]["repair_attempted"] is True
    assert row["records"][0]["source_quality_summary"] is None
    assert row["records"][0]["sources"][0]["source_quality"] is None
    assert row["source_quality_summary"] is None
    assert row["deduped_candidate_count"] == row["raw_search_result_count"] == 0


def test_prior_model_artifact_and_descriptive_source_label_cannot_launder_numeric_quality(
    tmp_path,
    monkeypatch,
):
    store, item, request = _case(tmp_path)
    item.sources[1].source_quality = "Descriptive official-source label; no numeric acquisition."
    item.target.metadata["prior_model_artifact"] = {
        "source_quality": _unobserved_quality(),
        "source_quality_summary": _unobserved_quality_summary(),
    }
    store.save_work_item(item)
    invalid = _output()
    invalid["records"][0]["sources"][0]["source_quality"] = _unobserved_quality()
    model = _inject(monkeypatch, [deepcopy(invalid), deepcopy(invalid)])
    result = _execute(request, "direct")
    assert len(model.calls) == 2 and not result.advanced and not result.artifact_refs
    assert "prior_model_artifact" in str(model.calls[0])
    rows = [
        row for row in store.fetch_all("agent_runs") if row["agent_name"] == "opportunity_scout"
    ]
    assert len(rows) == 1 and rows[0]["status"] == "error"
    failure = json.loads(rows[0]["output_json"])["sdk_run_failure"]
    assert failure["usage"]["requests"] == 2
    assert failure["request_cache"]["decision_ownership"]["validator_outcome"]["reason_code"] == (
        "supplied_provenance_unobserved"
    )


def test_claim_confidence_and_deterministic_normalization_remain_distinct_from_quality(
    tmp_path,
    monkeypatch,
):
    store, item, request = _case(tmp_path)
    output = _output()
    output["records"][0]["claims"][0]["confidence"] = 0.37
    output["records"][0]["priority_score"] = 0
    output["records"][0]["outside_consulting_likelihood"] = 0
    model = _inject(monkeypatch, [output])
    result = _execute(request, "direct")
    assert result.advanced and len(model.calls) == 1
    row = store.get_agent_run(result.artifact_refs[0].metadata["agent_run_id"])["output"]
    assert row["records"][0]["claims"][0]["confidence"] == 0.37
    assert row["records"][0]["priority_score"] > 0
    assert row["records"][0]["outside_consulting_likelihood"] > 0
    assert row["records"][0]["sources"][0]["source_quality"] is None
    assert row["request_cache"]["decision_normalizations"]
    instructions = " ".join(model.instructions[0].split())
    assert "Original supplied source text takes precedence over prior summaries" in instructions
    assert "Do not force the subject into an inaccurate `opportunity_type`" in instructions
    assert "Keep independent caveats separate" in instructions
