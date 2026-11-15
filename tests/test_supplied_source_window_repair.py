"""Native SDK correction keeps one exact supplied source and completed read."""

from __future__ import annotations

import json

import pytest
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall
from test_lazy_slack_context_backend_matrix import Provider, _message, _response
from test_lazy_slack_context_backend_matrix import (
    strict_offline_boundaries as strict_offline_boundaries,
)

from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.models import ResearchSDKInput
from keystone_agents.run import sdk_run_failure_metadata
from keystone_agents.runtime.decision_validation import AgentDecisionValidationError
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.schemas.work_item import WorkItemSourceRef
from keystone_agents.sdk import build_local_run_config
from keystone_agents.tools import website_extraction_tool as web

URL = "https://example.org/one-selected-study"
CHANGED_URL = "https://example.org/different-study"
SOURCE_ID = "selected-study"


def _source_input(tmp_path, monkeypatch):
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path / "source-state"))
    text = (
        "Background and methods. " * 75
        + "LATE_RESULT_SENTINEL: the study reported a bounded finding. "
        + "Limitations and follow-up. " * 50
    )
    preview, access = web.project_web_source(
        web.WebsiteExtractionResult(
            url=URL, title="One selected study", provider="trafilatura",
            status="success", text_or_markdown=text,
        ),
        source_id=SOURCE_ID, selected_url=URL, max_chars=600,
    )
    source = WorkItemSourceRef(
        source_id=SOURCE_ID, url=URL, title="One selected study",
        source_type="website", provider="selected-url",
        extraction_status="extracted", evidence_excerpt=preview,
        web_source_access=access,
    )
    packet = {
        "raw_request": "Review the one selected study and its evidence limits.",
        "source_catalog": [{"source_id": SOURCE_ID, "url": URL}],
        "source_refs": [source.model_dump(mode="json")],
    }
    typed_input = ResearchSDKInput(
        target_name="One selected study", target_type="other",
        research_goal="Summarize the selected study's findings and limitations.",
        source_context=json.dumps(packet),
    )
    plan = ManualRequestPlan(
        source="synthetic:one_selected_source", intent="research_brief",
        target_agent="business_research_analyst", task_objective="source_research",
        expected_artifact_type="research_brief", primary_target=URL,
        target_type="url", provider_operations=["read"],
        provider_read_scope="single_item", requires_durable_state=True,
        requires_live_search=False, side_effect_policy="draft_or_read_only",
        workflow=["business_research_analyst"],
    )
    return typed_input, source, access, plan


def _brief(source: WorkItemSourceRef, *, wrong_url: bool = False) -> ResearchBrief:
    return ResearchBrief(
        target_name="One selected study", target_type="other",
        summary="The selected study reports a bounded finding.",
        facts=[{
            "text": "A bounded finding is reported.",
            "source_ids": [source.source_id],
        }],
        source_ids_used=[source.source_id],
        sources=[{
            "source_id": source.source_id, "title": source.title,
            "url": CHANGED_URL if wrong_url else source.url,
            "source_type": source.source_type,
        }],
        limitations=["The synthetic study does not establish broader generalization."],
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "research_source_selection",
            "selected_candidate_ids": [source.source_id],
            "candidate_assessments": [{
                "candidate_id": source.source_id, "disposition": "selected",
                "rationale": "The exact selected study was read.",
            }],
            "reasoning": "Only the supplied source supports the brief.",
        },
    )


def test_citation_mismatch_repairs_once_with_same_source_and_window(
    tmp_path, monkeypatch, strict_offline_boundaries,
) -> None:
    typed_input, source, access, plan = _source_input(tmp_path, monkeypatch)
    reads: list[dict] = []
    actual_read = web.read_web_source_window_impl

    def recorded_read(**kwargs):
        reads.append(dict(kwargs))
        return actual_read(**kwargs)

    monkeypatch.setattr(web, "read_web_source_window_impl", recorded_read)

    class CitationRepairModel(Model):
        def __init__(self):
            self.inputs: list[str] = []

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            serialized = json.dumps(input)
            self.inputs.append(serialized)
            if len(self.inputs) == 1:
                assert [tool.name for tool in tools] == ["read_web_source_window"]
                assert SOURCE_ID in serialized and URL in serialized
                assert "LATE_RESULT_SENTINEL" not in serialized
                return _response([ResponseFunctionToolCall(
                    type="function_call", name="read_web_source_window",
                    call_id="source-window", status="completed",
                    arguments=json.dumps({
                        "source_id": SOURCE_ID,
                        "selected_url": URL,
                        "expected_snapshot_sha256": access.snapshot_sha256,
                        "start_char": access.next_start_char,
                        "max_chars": 8000,
                    }),
                )])
            if len(self.inputs) == 2:
                assert "LATE_RESULT_SENTINEL" in serialized
                return _response([_message(_brief(source, wrong_url=True))])
            assert len(self.inputs) == 3
            assert not tools and not handoffs
            assert SOURCE_ID in serialized and URL in serialized
            assert "LATE_RESULT_SENTINEL" in serialized
            assert access.snapshot_sha256 in serialized
            assert len(reads) == 1
            return _response([_message(_brief(source))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Offline acceptance uses non-streaming SDK turns.")

    model = CitationRepairModel()
    result = run_business_research_analyst_research_brief_sdk(
        typed_input, run_config=build_local_run_config(Provider(model)),
        manual_request_plan=plan, attach_tools=False,
        supplied_sources=(source,), max_turns=4,
    )
    assert len(model.inputs) == 3
    assert len(reads) == 1
    assert result.output.source_ids_used == [SOURCE_ID]
    assert [citation.url for citation in result.output.sources] == [URL]
    ownership = result.request_cache["decision_ownership"]
    assert ownership["attempt_count"] == 2 and ownership["repair_attempted"]
    first = ownership["attempts"][0]["validator_outcome"]
    assert first["reason_code"] == "supplied_research_citation_mismatch"
    diagnostics = json.loads(first["feedback"].split("citation_diagnostics=", 1)[1])
    assert diagnostics["issues"] == [{
        "field": "sources[0].url", "category": "url_mismatch",
    }]
    assert URL not in first["feedback"]
    assert "LATE_RESULT_SENTINEL" not in first["feedback"]
    assert ownership["attempts"][1]["validator_outcome"]["status"] == "accepted"
    replay = result.request_cache["decision_repair_evidence"]
    assert replay["status"] == "ready"
    assert replay["candidate_universe_source"] == "original_model_input"
    assert replay["replayed_tool_names"] == ["read_web_source_window"]
    assert replay["disabled_read_tool_names"] == ["read_web_source_window"]
    assert replay["source_tool_call_count"] == replay["replayed_output_count"] == 1
    assert replay["provider_calls_during_repair"] == 0


@pytest.mark.parametrize("case", ["foreign_access", "changed_catalog"])
def test_unbound_source_identity_fails_before_first_model_turn(
    tmp_path, monkeypatch, strict_offline_boundaries, case: str,
) -> None:
    typed_input, source, _access, plan = _source_input(tmp_path, monkeypatch)
    packet = json.loads(typed_input.source_context)
    if case == "foreign_access":
        packet["source_refs"][0]["web_source_access"]["selected_url"] = (
            "https://example.org/foreign-study"
        )
        typed_input = ResearchSDKInput(
            target_name=typed_input.target_name,
            research_goal=typed_input.research_goal,
            source_context=json.dumps(packet),
        )
    else:
        source = source.model_copy(update={"source_id": "changed-study"})

    class NoModel(Model):
        async def get_response(self, *args, **kwargs):
            raise AssertionError("Unbound source cannot start a model turn.")

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Unbound source cannot start a model turn.")

    with pytest.raises((ValueError, AgentDecisionValidationError)):
        run_business_research_analyst_research_brief_sdk(
            typed_input, run_config=build_local_run_config(Provider(NoModel())),
            manual_request_plan=plan, attach_tools=False,
            supplied_sources=(source,), max_turns=4,
        )


def test_unavailable_snapshot_does_not_become_repair_evidence(
    tmp_path, monkeypatch, strict_offline_boundaries,
) -> None:
    typed_input, source, access, plan = _source_input(tmp_path, monkeypatch)
    packet = json.loads(typed_input.source_context)
    packet["source_refs"][0]["web_source_access"]["snapshot_sha256"] = "f" * 64
    typed_input = ResearchSDKInput(
        target_name=typed_input.target_name,
        research_goal=typed_input.research_goal,
        source_context=json.dumps(packet),
    )

    class MissingSnapshotModel(Model):
        def __init__(self):
            self.inputs: list[str] = []

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            serialized = json.dumps(input)
            self.inputs.append(serialized)
            if len(self.inputs) == 1:
                assert [tool.name for tool in tools] == ["read_web_source_window"]
                return _response([ResponseFunctionToolCall(
                    type="function_call", name="read_web_source_window",
                    call_id="missing-window", status="completed",
                    arguments=json.dumps({
                        "source_id": SOURCE_ID, "selected_url": URL,
                        "expected_snapshot_sha256": "f" * 64,
                        "start_char": access.next_start_char, "max_chars": 8000,
                    }),
                )])
            assert len(self.inputs) == 2, "Unavailable source must not enter a repair turn."
            assert "unavailable" in serialized
            return _response([_message(_brief(source, wrong_url=True))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Offline acceptance uses non-streaming SDK turns.")

    model = MissingSnapshotModel()
    with pytest.raises(AgentDecisionValidationError) as error:
        run_business_research_analyst_research_brief_sdk(
            typed_input, run_config=build_local_run_config(Provider(model)),
            manual_request_plan=plan, attach_tools=False,
            supplied_sources=(source,), max_turns=4,
        )
    assert len(model.inputs) == 2
    assert error.value.outcome.reason_code == (
        "decision_repair_source_window_unavailable_or_mismatched"
    )
    failure = sdk_run_failure_metadata(error.value)
    ownership = failure["request_cache"]["decision_ownership"]
    original = ownership["initial_validator_outcome"]
    final = ownership["validator_outcome"]
    assert original["reason_code"] == "supplied_research_citation_mismatch"
    diagnostics = json.loads(original["feedback"].split("citation_diagnostics=", 1)[1])
    assert diagnostics["issues"] == [{
        "field": "sources[0].url", "category": "url_mismatch",
    }]
    assert URL not in original["feedback"]
    assert "LATE_RESULT_SENTINEL" not in original["feedback"]
    assert final["reason_code"] == error.value.outcome.reason_code
    assert failure["request_cache"]["decision_repair_evidence"]["status"] == "unsafe"


def test_changed_catalog_url_cannot_rebind_trusted_candidate_during_repair(
    tmp_path, monkeypatch, strict_offline_boundaries,
) -> None:
    typed_input, supplied_source, _access, plan = _source_input(tmp_path, monkeypatch)
    changed_preview, changed_access = web.project_web_source(
        web.WebsiteExtractionResult(
            url=CHANGED_URL, title="Different study", provider="trafilatura",
            status="success",
            text_or_markdown="Different study text. " * 120,
        ),
        source_id=SOURCE_ID, selected_url=CHANGED_URL, max_chars=600,
    )
    packet = json.loads(typed_input.source_context)
    packet["source_catalog"][0]["url"] = CHANGED_URL
    packet["source_refs"][0].update({
        "url": CHANGED_URL,
        "evidence_excerpt": changed_preview,
        "web_source_access": changed_access.model_dump(mode="json"),
    })
    typed_input = ResearchSDKInput(
        target_name=typed_input.target_name,
        research_goal=typed_input.research_goal,
        source_context=json.dumps(packet),
    )

    class ChangedCatalogModel(Model):
        def __init__(self):
            self.inputs: list[str] = []

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            serialized = json.dumps(input)
            self.inputs.append(serialized)
            if len(self.inputs) == 1:
                assert [tool.name for tool in tools] == ["read_web_source_window"]
                return _response([ResponseFunctionToolCall(
                    type="function_call", name="read_web_source_window",
                    call_id="changed-source-window", status="completed",
                    arguments=json.dumps({
                        "source_id": SOURCE_ID,
                        "selected_url": CHANGED_URL,
                        "expected_snapshot_sha256": changed_access.snapshot_sha256,
                        "start_char": changed_access.next_start_char,
                        "max_chars": 8000,
                    }),
                )])
            assert len(self.inputs) == 2, "Changed catalog cannot enter repair."
            assert "Different study text" in serialized
            return _response([_message(_brief(supplied_source, wrong_url=True))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Offline acceptance uses non-streaming SDK turns.")

    model = ChangedCatalogModel()
    with pytest.raises(AgentDecisionValidationError) as error:
        run_business_research_analyst_research_brief_sdk(
            typed_input, run_config=build_local_run_config(Provider(model)),
            manual_request_plan=plan, attach_tools=False,
            supplied_sources=(supplied_source,), max_turns=4,
        )
    assert len(model.inputs) == 2
    assert error.value.outcome.reason_code == (
        "decision_repair_model_input_source_scope_unavailable"
    )
    failure = sdk_run_failure_metadata(error.value)
    ownership = failure["request_cache"]["decision_ownership"]
    assert ownership["initial_validator_outcome"]["reason_code"] == (
        "supplied_research_citation_mismatch"
    )
    assert ownership["validator_outcome"]["reason_code"] == error.value.outcome.reason_code
    assert failure["request_cache"]["decision_repair_evidence"]["status"] == "unsafe"
