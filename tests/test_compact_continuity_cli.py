"""Offline transport checks for compact facts across real CLI context builders."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from contextvars import Context
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agents.models.interface import Model
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall
from test_lazy_slack_context_backend_matrix import Provider, _message, _response
from test_lazy_slack_context_backend_matrix import (
    strict_offline_boundaries as strict_offline_boundaries,
)

from keystone_agents import response_synthesis, workflow_runner
from keystone_agents.agents import chief_of_staff as chief
from keystone_agents.agents import orchestrator
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.orchestrator.preflight_context import (
    SPECIALIST_EXECUTION_CONTEXT_ENV,
    orchestrator_preflight_env,
)
from keystone_agents.runtime.continuity_policy import (
    activate_continuity_policy,
    resolve_slack_compact_continuity_policy,
)
from keystone_agents.runtime.operator_continuity import (
    compact_optional_context,
    compact_task_context,
    serialized_context_bytes,
)
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.memory_scope import MEMORY_CAPTURE_KEY, MEMORY_SCOPE_KEY
from keystone_agents.schemas.orchestrator import (
    OrchestratorGeneralAnswer,
    OrchestratorPlanningResult,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemTarget,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.workflow_runner import _specialist_orchestrator_context_payload

CHANNEL = "C111TEST"
REQUEST = "Define a preprint in plain language."
OLD_ANSWER = "UNACCEPTED_PRIOR_ANSWER_SENTINEL"


def _host_state() -> dict:
    scope = {
        "team_id": "T111TEST", "channel_id": CHANNEL,
        "user_id": "U111TEST", "thread_ts": "1794675600.000001",
        "request_ts": "1794675600.000002",
    }
    return {
        "slack_context": scope,
        "channel_context_capability": {
            "schema_version": "keystone.slack.channel_context.v1",
            "channel_id": CHANNEL, "request_ts": scope["request_ts"],
            "oldest_ts": "1794070800.000002",
        },
        "prior_agent_runs": [{"summary": OLD_ANSWER}],
        "slack_thread_transcript": OLD_ANSWER,
    }


def _invoke_native(tool, arguments: dict) -> dict:
    serialized = json.dumps(arguments)
    context = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="synthetic-query",
        tool_arguments=serialized,
    )
    return json.loads(asyncio.run(tool.on_invoke_tool(context, serialized)))


def _preflight_with_local_profile_query(monkeypatch, tmp_path):
    state = _host_state()
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS", CHANNEL)
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_PROFILE_USER_IDS", "U111TEST")
    observed: dict = {}

    def fake_route_sdk(*, agent, **kwargs):
        tool = next(tool for tool in agent.tools if tool.name == "query_operator_context")
        observed["query"] = _invoke_native(tool, {
            "query": "approved professional background and skills",
            "include_profile": True,
        })
        return SimpleNamespace(
            output=OrchestratorPlanningResult(
                route="orchestrator", workflow=[], routing_mode="llm",
                rationale="A short concept definition is enough.",
            ),
            usage={}, cost={}, request_cache={}, raw_result=SimpleNamespace(new_items=[]),
        )

    monkeypatch.setattr(orchestrator, "run_typed_sdk_agent", fake_route_sdk)
    policy = resolve_slack_compact_continuity_policy(state)
    assert policy.eligible
    with activate_continuity_policy(policy):
        preflight = orchestrator.run_orchestrator_preflight(
            REQUEST, live_orchestrator=True, workflow_state=state,
            database_url=f"sqlite:///{tmp_path / 'continuity.db'}",
        )
    assert observed["query"]["status"] == "ok"
    assert len(observed["query"]["facts"]) == 2
    facts = preflight.workflow_state["operator_context_evidence"]["model_context"]["facts"]
    assert facts == observed["query"]["facts"]
    assert OLD_ANSWER not in json.dumps(preflight.workflow_state)
    return state, policy, preflight, facts


def test_preflight_facts_reach_actual_cli_general_answer_input_once(
    tmp_path, monkeypatch, capsys,
) -> None:
    state, policy, preflight, facts = _preflight_with_local_profile_query(monkeypatch, tmp_path)
    observed: dict = {}

    def fake_answer_sdk(*, typed_input, **kwargs):
        observed["typed_input"] = typed_input
        return SimpleNamespace(
            output=OrchestratorGeneralAnswer(
                answer="A preprint is a research manuscript shared before peer review.",
                needs_source=False,
            ),
            usage={"requests": 1}, cost={}, request_cache={},
            raw_result=SimpleNamespace(new_items=[]),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_answer_sdk)
    monkeypatch.setattr(
        cli, "build_orchestrator_general_answer_agent",
        lambda **_kwargs: SimpleNamespace(tools=[]),
    )
    with activate_continuity_policy(policy):
        exit_code = cli._run_orchestrator_general_answer_live(
            REQUEST,
            manual_plan=preflight.manual_request_plan,
            orchestrator_preflight=preflight,
            sdk_session_spec=None,
            database_url=f"sqlite:///{tmp_path / 'continuity.db'}",
            execution_context=state,
            json_output=True,
            explicit_live_search=False,
        )
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["entrypoint"] == "orchestrator_general_answer"
    typed_input = observed["typed_input"]
    assert typed_input["approved_operator_context"] == {"facts": facts}
    optional = {key: typed_input[key] for key in (
        "approved_operator_context", "current_work_item",
    ) if key in typed_input}
    assert serialized_context_bytes(optional) <= 2048
    for fact in facts:
        assert json.dumps(typed_input).count(fact["summary"]) == 1
    assert OLD_ANSWER not in json.dumps(typed_input)


def test_direct_child_context_and_graph_payload_carry_one_fact_copy(
    tmp_path, monkeypatch,
) -> None:
    state, policy, preflight, facts = _preflight_with_local_profile_query(monkeypatch, tmp_path)
    direct_state = {
        **state,
        "operator_context_evidence": preflight.workflow_state["operator_context_evidence"],
    }
    child = cli._direct_specialist_execution_context(
        REQUEST,
        workflow_state=direct_state,
        include_slack_scope=True,
    )
    assert child["slack_scope"]["team_id"] == "T111TEST"
    assert child["slack_scope"]["user_id"] == "U111TEST"
    assert child["slack_scope"]["channel_id"] == CHANNEL
    assert child["channel_context_capability"] == state["channel_context_capability"]
    assert child["approved_operator_context"] == {"facts": facts}
    assert OLD_ANSWER not in json.dumps(child)
    child_env = orchestrator_preflight_env(preflight, execution_context=child)
    child_wire = json.loads(child_env[SPECIALIST_EXECUTION_CONTEXT_ENV])
    assert child_wire == child
    for fact in facts:
        assert json.dumps(child_wire).count(fact["summary"]) == 1
    assert OLD_ANSWER not in child_env[SPECIALIST_EXECUTION_CONTEXT_ENV]

    work_item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF, title="Synthetic source task",
        request_text=REQUEST, current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(metadata={
            "slack_context": {
                **state["slack_context"], "thread_transcript": OLD_ANSWER,
                "read_context": OLD_ANSWER,
            },
        }),
    )
    request = WorkflowRunRequest(
        request_text=REQUEST,
        orchestrator_preflight={"workflow_state": preflight.workflow_state},
        live_sdk=True,
    )
    with activate_continuity_policy(policy):
        graph = _specialist_orchestrator_context_payload(request, work_item)
    assert graph["current_work_item"] == compact_task_context(work_item)
    assert graph["approved_operator_context"] == {"facts": facts}
    assert OLD_ANSWER not in json.dumps(graph)
    for fact in facts:
        assert json.dumps(graph).count(fact["summary"]) == 1
    optional = {key: graph[key] for key in (
        "approved_operator_context", "current_work_item",
    ) if key in graph}
    assert serialized_context_bytes(optional) <= 2048


def test_growth_preserves_preflight_facts_when_optional_task_cannot_fit() -> None:
    facts = [
        {"memory_id": index, "summary": "é" * 230, "source_urls": []}
        for index in (1, 2, 3, 4)
    ]
    task = {"id": "wi_synthetic", "kind": "research_brief", "status": "blocked",
            "route": "business_research_analyst", "target": {
                "url": "https://example.org/" + "x" * 1000,
            }}
    packet = compact_optional_context(task, {"model_context": {"facts": facts}})
    assert packet.get("approved_operator_context", {}).get("facts") == facts
    assert "current_work_item" not in packet
    from keystone_agents.runtime.operator_continuity import MAX_OPTIONAL_CONTEXT_BYTES

    assert serialized_context_bytes(packet) <= MAX_OPTIONAL_CONTEXT_BYTES


def test_nonpilot_direct_context_does_not_gain_optional_facts(tmp_path, monkeypatch) -> None:
    state = _host_state()
    state["operator_context_evidence"] = {"model_context": {"facts": [
        {"memory_id": 1, "summary": "Synthetic preference"},
    ]}}
    monkeypatch.delenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", raising=False)
    child = cli._direct_specialist_execution_context(
        REQUEST, workflow_state=state, include_slack_scope=True,
    )
    assert "approved_operator_context" not in child
    assert "current_work_item" not in child


@pytest.mark.parametrize("capture_succeeds", [True, False], ids=["saved", "postcondition-failed"])
def test_actual_cli_slack_capture_routes_to_native_chief_and_persists_once(
    tmp_path, monkeypatch, capsys, strict_offline_boundaries, capture_succeeds,
) -> None:
    """Current raw Slack text supplies the plan; scripted models only choose actions."""

    request = (
        "Remember this preference for future runs in this channel: keep state and "
        "cross-session memory compact, retrieve only relevant approved facts, and "
        "reuse cached source material when appropriate. Save it as an internal "
        "preference and confirm briefly."
    )
    scope = _host_state()["slack_context"]
    context = {
        **scope, "schema": "keystone.slack.history_context.v1",
        "channel_context_capability": _host_state()["channel_context_capability"],
        "slack_thread_root": request, "thread_root_request": request,
        "request_text": request, "read_context": "", "thread_messages": [],
    }
    context_path = tmp_path / "current-slack-context.json"
    context_path.write_text(json.dumps(context))
    initial_state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(context_path), request_text=request,
    )
    assert initial_state.get("slack_context"), sorted(initial_state)
    database_url = f"sqlite:///{tmp_path / 'continuity-capture.db'}"
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS", CHANNEL)
    monkeypatch.setenv("KEYSTONE_SDK_SESSIONS", "false")
    monkeypatch.setenv("KEYSTONE_SDK_SESSION_DB", str(tmp_path / "sessions.sqlite"))
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(tmp_path / "eval.sqlite"))
    monkeypatch.delenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_PROFILE_USER_IDS", raising=False)
    # Keep the test's synthetic environment; the production decorator reloads
    # the developer's local .env before entering this otherwise real CLI path.
    monkeypatch.setattr(
        cli, "_run_live_ask_with_environment", cli._run_ask_with_current_environment,
    )
    observed: dict = {"route_inputs": [], "chief_inputs": [], "capture_outputs": []}

    class RouteModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            model_input = json.dumps(input)
            observed["route_inputs"].append(model_input)
            assert request in model_input
            assert "downstream_operator_context_capabilities" in model_input
            return _response([_message(OrchestratorPlanningResult(
                route="chief_of_staff", workflow=["chief_of_staff"],
                rationale="Chief owns the current internal preference capture.",
                decision={
                    "decision_owner": "orchestrator",
                    "decision_stage": "orchestrator_route_selection",
                    "selected_candidate_id": "chief_of_staff",
                    "candidate_assessments": [{
                        "candidate_id": "chief_of_staff", "disposition": "selected",
                        "rationale": "Current operator asked to save an internal preference.",
                    }],
                    "reasoning": "Chief can capture only with the current host gate.",
                },
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("No streaming route model call expected.")

    actual_preflight = cli.run_orchestrator_preflight

    def preflight(input_text, **kwargs):
        result = actual_preflight(input_text, **{
            **kwargs, "run_config": build_local_run_config(Provider(RouteModel())),
        })
        assert result.manual_request_plan.intent == "reference_capture"
        assert result.manual_request_plan.target_agent == "chief_of_staff"
        return result

    monkeypatch.setattr(cli, "run_orchestrator_preflight", preflight)

    class CaptureModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            model_input = json.dumps(input)
            observed["chief_inputs"].append(model_input)
            assert request in model_input
            tool_names = {tool.name for tool in tools}
            assert "capture_operator_memory" in tool_names
            outputs = [item for item in input if isinstance(item, dict)
                       and item.get("type") == "function_call_output"]
            if not outputs and capture_succeeds:
                return _response([ResponseFunctionToolCall(
                    type="function_call", name="capture_operator_memory",
                    call_id="synthetic-capture", status="completed",
                    arguments=json.dumps({
                        "kind": "preference", "title": "Compact continuity",
                        "summary": (
                            "Keep cross-session state compact; retrieve only relevant "
                            "approved facts; reuse cached source material when appropriate."
                        ),
                        "source_ref_ids": [],
                    }),
                )])
            if outputs:
                observed["capture_outputs"].append(json.loads(outputs[-1]["output"]))
            return _response([_message(ChiefOfStaffResult(
                mode="llm", summary="Saved the internal preference for future runs.",
                recommended_route={
                    "workflow_type": "memory-review", "target_channel": "current-thread",
                },
                decision={
                    "decision_owner": "chief_of_staff",
                    "decision_stage": "chief_delegation_selection",
                    "selected_candidate_ids": ["workflow:memory-review"],
                    "candidate_assessments": [{
                        "candidate_id": "workflow:memory-review", "disposition": "selected",
                        "rationale": "The verified scoped capture completes the request.",
                    }],
                    "reasoning": "The capture tool returned a read-back receipt.",
                },
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("No streaming Chief model call expected.")

    actual_chief = chief.run_chief_of_staff_sdk

    def native_chief(typed_input, **kwargs):
        assert kwargs["manual_request_plan"].intent == "reference_capture"
        assert request in typed_input["request"]
        return actual_chief(typed_input, **{
            **kwargs, "run_config": build_local_run_config(Provider(CaptureModel())),
        })

    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", native_chief)
    script_path = Path(__file__).resolve().parents[1] / "scripts/run_chief_of_staff.py"
    spec = importlib.util.spec_from_file_location("synthetic_continuity_chief", script_path)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "load_settings", lambda **_kwargs: None)
    monkeypatch.setattr(script, "run_chief_of_staff_sdk", native_chief)

    def child(command, *, cwd, env, timeout):
        assert command[1] == "scripts/run_chief_of_staff.py"
        child_context = json.loads(env["KEYSTONE_SPECIALIST_EXECUTION_CONTEXT_JSON"])
        assert child_context.get("slack_scope"), sorted(child_context)
        assert child_context.get("channel_context_capability"), sorted(child_context)
        assert env.get("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED") == "true"
        assert env.get("KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS") == CHANNEL
        assert env.get("DATABASE_URL") == database_url
        stdout = io.StringIO()
        with patch.dict(os.environ, env, clear=True), redirect_stdout(stdout):
            try:
                returncode = Context().run(script.main, command[2:])
            except Exception as exc:
                returncode = script._handle_uncaught_exception(exc, command[2:])
        return subprocess.CompletedProcess(command, returncode, stdout.getvalue(), "")

    monkeypatch.setattr(cli, "run_isolated_child_process", child)

    class FinalModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            assert not tools and not handoffs
            assert "Saved the internal preference" in json.dumps(input)
            return _response([_message(response_synthesis.UserFacingResponseSynthesis(
                answer="Saved the internal preference for this channel.",
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("No streaming final model call expected.")

    actual_synthesis = response_synthesis.run_typed_sdk_agent
    monkeypatch.setattr(response_synthesis, "run_typed_sdk_agent", lambda **kwargs: (
        actual_synthesis(**{
            **kwargs, "run_config": build_local_run_config(Provider(FinalModel())),
        })
    ))
    exit_code = cli.main([
        "ask", "--agent", "chief_of_staff", "--live-sdk", "--json",
        "--no-sdk-session", "--max-openai-requests", "8",
        "--max-manager-steps", "1", "--database-url", database_url,
        "--context-file", str(context_path), "--input", request,
    ])
    payload = json.loads(capsys.readouterr().out)
    assert observed["route_inputs"]
    assert len(observed["chief_inputs"]) == 2
    saved_items = SQLiteStore(database_url).list_memory_items()
    if capture_succeeds:
        assert exit_code == 0, payload.get("failure", {}).get("reason")
        assert len(observed["capture_outputs"]) == 1
        assert observed["capture_outputs"][0]["verified"] is True
        assert len(saved_items) == 1
        saved = saved_items[0]
        assert saved.title == "Compact continuity"
        assert saved.summary.startswith("Keep cross-session state compact")
        assert saved.metadata[MEMORY_SCOPE_KEY]["channel_id"] == CHANNEL
        assert saved.metadata[MEMORY_SCOPE_KEY]["user_id"] == "U111TEST"
        assert saved.metadata[MEMORY_CAPTURE_KEY]["request_ts"] == scope["request_ts"]
        assert "saved" in payload["human_summary"].lower()
    else:
        assert exit_code == 1
        assert not saved_items and not observed["capture_outputs"]
        assert payload["status"] == "failed"
        assert payload["sdk_failure"]["failure_kind"] == "required_tool_execution_missing"
        assert payload["sdk_failure"]["error_class"] == "ToolExecutionContractError"
        assert payload["sdk_failure"]["validation_schema"] == (
            "keystone.tool_execution_postcondition.v1"
        )
        assert request not in json.dumps(payload["sdk_failure"])
        assert "tool_receipts" not in payload["sdk_failure"]
        assert payload["usage"]["requests"] == 2
        assert len(payload["request_cache"]["model_attempt_usage"]) == 2
        stored = SQLiteStore(database_url).get_agent_run(payload["agent_run_id"])
        assert stored["status"] == "error"
        assert stored["output"]["usage"]["requests"] == 2
