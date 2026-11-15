"""Optional approved context is queried and retained in the actual route SDK loop."""

from __future__ import annotations

import json

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from keystone_agents.agent_registry import get_agent_spec
from keystone_agents.agents import manual_request_planner
from keystone_agents.agents.orchestrator import build_orchestrator_agent, run_orchestrator_preflight
from keystone_agents.orchestrator.preflight_context import compact_orchestrator_preflight_payload
from keystone_agents.runtime.continuity_policy import CHANNEL_IDS_ENV, ENABLED_ENV
from keystone_agents.runtime.operator_continuity import (
    compact_optional_context,
    operator_capability_from_state,
)
from keystone_agents.runtime.scoped_memory import ScopedMemoryCaptureInput, capture_scoped_memory
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.sdk import build_local_run_config
from keystone_agents.tools.storage_tool import StorageTool

REQUEST = "Recommend one concrete local review step suited to my saved preferences."
SUMMARY = "Prefer a reversible local review before adopting a new tool."


def _state():
    return {
        "slack_context": {
            "team_id": "T111TEST", "channel_id": "C111TEST", "user_id": "U111TEST",
            "thread_ts": "1700604700.000001", "request_ts": "1700604800.000001",
        },
        "channel_context_capability": {
            "schema_version": "keystone.slack.channel_context.v1", "channel_id": "C111TEST",
            "request_ts": "1700604800.000001", "oldest_ts": "1700000000.000001",
        },
        "current_work_item": {"id": "wi_synthetic_context", "status": "blocked"},
        "prior_agent_runs": [{"summary": "OLD_NARRATIVE_SENTINEL"}],
        "recent_slack_thread": [{"summary": "OLD_NARRATIVE_SENTINEL"}],
        "slack_thread_transcript": "OLD_NARRATIVE_SENTINEL",
        "historical_work_item_facts": ["OLD_NARRATIVE_SENTINEL"],
        "operator_context_evidence": {"model_context": {"facts": [
            {"summary": "STALE_OPERATOR_FACT"},
        ]}},
        "operator_context_usage": {"query_calls": 99},
    }


def _route(*, extra_assessment=False):
    assessments = [{
        "candidate_id": "orchestrator", "disposition": "selected",
        "rationale": "Owns this bounded informational recommendation.",
    }]
    if extra_assessment:
        assessments.append({
            "candidate_id": "unsupported_synthetic_owner", "disposition": "excluded",
            "rationale": "Excluded an invalid owner for this diagnostic.",
        })
    return {
        "route": "orchestrator", "target_agent": "orchestrator", "workflow": [],
        "routing_mode": "llm", "rationale": "Answer the current request from admitted context.",
        "requires_human_review": False, "approval_required": False,
        "decision": {
            "decision_owner": "orchestrator", "decision_stage": "orchestrator_route_selection",
            "selected_candidate_id": "orchestrator", "candidate_assessments": assessments,
            "reasoning": "Use the current request and only relevant approved context.",
            "needs_more_context": False,
        },
    }


class RouteModel(Model):
    def __init__(self, *, query=True, empty=False, repair=False):
        self.query, self.empty, self.repair = query, empty, repair
        self.calls = []

    async def get_response(self, system_instructions, input, model_settings, tools,
                           output_schema, handoffs, tracing, **kwargs):
        serialized = json.dumps(input)
        self.calls.append({"input": serialized, "tools": [tool.name for tool in tools]})
        assert REQUEST in serialized
        assert "OLD_NARRATIVE_SENTINEL" not in serialized
        assert "STALE_OPERATOR_FACT" not in serialized
        assert "capture_operator_memory" not in self.calls[-1]["tools"]
        if len(self.calls) == 1:
            strings = [input] if isinstance(input, str) else [
                item.get("content", "") for item in input if isinstance(item, dict)
            ]
            payload = next(
                json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
                for text in strings if isinstance(text, str) and "allowed_routes" in text
            )
            catalog = payload["downstream_operator_context_capabilities"]
            assert catalog["query_operator_context"]["owners"] == [
                owner for owner in ("orchestrator", "chief_of_staff")
                if "query_operator_context" in get_agent_spec(owner).optional_tools
            ]
            capture = catalog["capture_operator_memory"]
            assert capture["owners"] == ["chief_of_staff"]
            assert capture["preflight_may_execute"] is False
            assert capture["requires_current_affirmative_request"]
            assert capture["requires_typed_capture_plan"]
            assert capture["requires_verified_storage_readback"]
        if self.query and len(self.calls) == 1:
            assert SUMMARY not in serialized
            assert set(self.calls[-1]["tools"]) == {
                "query_slack_channel_context", "read_slack_channel_context",
                "query_operator_context",
            }
            output = [ResponseFunctionToolCall(
                type="function_call", name="query_operator_context", call_id="optional-query",
                arguments=json.dumps({
                    "query": "unrelated zqxv topic" if self.empty else "reversible local review",
                    "include_profile": False,
                }),
            )]
        else:
            if self.query:
                assert (SUMMARY in serialized) != self.empty
            if self.repair and len(self.calls) == 3:
                assert not tools
                assert "candidate_assessment_identity_not_in_candidate_set" in serialized
                assert "Replayed tool evidence" in serialized
                if self.empty:
                    assert '"facts":[]' in serialized.replace("\\", "")
            output = [ResponseOutputMessage(
                type="message", id="route", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", annotations=[], text=json.dumps(
                    _route(extra_assessment=self.repair and len(self.calls) == 2),
                ))],
            )]
        return ModelResponse(output=output, usage=Usage(requests=1), response_id="offline-route")

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


@pytest.fixture
def scoped_context(monkeypatch, tmp_path):
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(CHANNEL_IDS_ENV, "C111TEST")
    database_url = f"sqlite:///{tmp_path / 'operator-preflight.db'}"
    state = _state()
    capability = operator_capability_from_state(
        state, raw_request=REQUEST, database_url=database_url,
    )
    capture_scoped_memory(
        StorageTool(database_url=database_url), scope=capability.scope,
        provenance=capability.provenance, capture_authorized=True,
        capture=ScopedMemoryCaptureInput(kind="preference", title="Local review", summary=SUMMARY),
        source_refs={},
    )
    plan = ManualRequestPlan(
        source="heuristic", target_agent="orchestrator", intent="route_request",
        ask_shape=AskShapePolicy(permission_state="read_only"),
    )
    monkeypatch.setattr(manual_request_planner, "resolve_manual_request_plan", lambda *a, **k: plan)

    def forbidden(*args, **kwargs):
        raise AssertionError("This optional-memory diagnostic must not read Slack or a provider.")

    monkeypatch.setattr(
        "keystone_agents.tools.slack_channel_context_tools._invoke_channel_context_backend",
        forbidden,
    )
    return state, database_url


@pytest.mark.parametrize("mode", ["relevant", "stable", "empty-repair", "facts-repair"])
def test_actual_preflight_query_results_enter_same_route_loop_and_transport(scoped_context, mode):
    state, database_url = scoped_context
    model = RouteModel(
        query=mode != "stable", empty=mode == "empty-repair", repair="repair" in mode,
    )
    preflight = run_orchestrator_preflight(
        REQUEST, workflow_state=state, database_url=database_url,
        run_config=build_local_run_config(Provider(model)),
    )
    assert preflight.execution_allowed
    evidence = preflight.workflow_state["operator_context_evidence"]
    usage = preflight.workflow_state["operator_context_usage"]
    assert usage["query_calls"] == (0 if mode == "stable" else 1)
    assert usage["capture_calls"] == 0
    if mode in {"relevant", "facts-repair"}:
        facts = evidence["model_context"]["facts"]
        assert len(facts) == 1 and facts[0]["summary"] == SUMMARY
    else:
        assert evidence["model_context"] is None
    packet = compact_orchestrator_preflight_payload(preflight)
    assert packet["workflow_state"]["operator_context_evidence"] == evidence
    optional = compact_optional_context(state["current_work_item"], evidence)
    assert len(json.dumps(optional, ensure_ascii=False, separators=(",", ":")).encode()) <= 2048
    assert json.dumps(optional).count(SUMMARY) == (1 if evidence["model_context"] else 0)
    # Caller-owned transport remains intact; stale facts were excluded only from
    # this new preflight's model context, not edited in the saved input file.
    assert state["operator_context_usage"]["query_calls"] == 99
    assert len(model.calls) == (3 if "repair" in mode else 1 if mode == "stable" else 2)


def test_route_only_builder_excludes_capture_even_from_capture_capable_owner(scoped_context):
    state, database_url = scoped_context
    capability = operator_capability_from_state(
        state, raw_request=REQUEST, database_url=database_url, capture_authorized=True,
    )
    assert "capture_operator_memory" in [tool.name for tool in capability.tools]
    agent = build_orchestrator_agent(
        route_only=True, include_tools=False, operator_context_capability=capability,
    )
    assert [tool.name for tool in agent.tools] == ["query_operator_context"]
