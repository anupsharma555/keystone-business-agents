"""Actual CoS/graph backend controls with scripted models and no live transport."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import json
import os
import subprocess
from contextlib import nullcontext, redirect_stdout
from contextvars import Context
from pathlib import Path
from unittest.mock import patch

import pytest
from agents import function_tool
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall
from test_lazy_slack_context_backend_matrix import (
    ARTICLE_URL,
    CHANNEL,
    OLDEST_TS,
    POST_TS,
    REQUEST_TS,
    ROOT_REQUEST,
    ContextModel,
    Provider,
    _context_file,
    _message,
    _response,
)
from test_lazy_slack_context_backend_matrix import (
    ks_backend as ks_backend,
)
from test_lazy_slack_context_backend_matrix import (
    strict_offline_boundaries as strict_offline_boundaries,
)
from test_slack_channel_context_backend_matrix import research_sdk_control as research_sdk_control

from keystone_agents import langgraph_workflow, workflow_runner
from keystone_agents.agents import orchestrator
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.orchestrator.preflight_context import compact_orchestrator_preflight_payload
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.memory import MemoryItem
from keystone_agents.schemas.orchestrator import (
    OrchestratorGeneralAnswer,
    OrchestratorPlanningResult,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import website_extraction_tool as extraction_tool
from keystone_agents.tools.search_provider import SearchResult


@pytest.fixture(autouse=True)
def isolate_sdk_session_storage(monkeypatch, tmp_path):
    """Keep all SDK session paths temporary, including an accidental enablement."""
    monkeypatch.setenv("KEYSTONE_SDK_SESSIONS", "false")
    monkeypatch.setenv("KEYSTONE_SDK_SESSION_DB", str(tmp_path / "isolated-sessions.sqlite3"))
    monkeypatch.setenv(
        "KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(tmp_path / "isolated-eval.sqlite3"),
    )
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_ID", raising=False)


class ChiefRouteModel(ContextModel):
    """Use the same actual query/read loop, then select the genuine CoS owner."""

    async def get_response(self, *args, **kwargs):
        response = await super().get_response(*args, **kwargs)
        if response.output[0].type != "message":
            return response
        output = OrchestratorPlanningResult(
            route="chief_of_staff",
            workflow=["chief_of_staff"],
            rationale="Assess internal priority from the selected saved preview only.",
            decision={
                "decision_owner": "orchestrator",
                "decision_stage": "orchestrator_route_selection",
                "selected_candidate_id": "chief_of_staff",
                "candidate_assessments": [
                    {
                        "candidate_id": "chief_of_staff",
                        "disposition": "selected",
                        "rationale": "An internal priority decision.",
                    }
                ],
                "reasoning": "The Chief owns this operating recommendation, not new research.",
            },
        )
        return _response([_message(output)])


class GeneralContextRouteModel(ContextModel):
    """A permitted Orchestrator owner must retain evidence from its first loop."""

    async def get_response(self, *args, **kwargs):
        response = await super().get_response(*args, **kwargs)
        if response.output[0].type != "message":
            return response
        output = OrchestratorPlanningResult(
            route="orchestrator",
            workflow=[],
            rationale="Answer the bounded historical-post question without additional agents.",
            decision={
                "decision_owner": "orchestrator",
                "decision_stage": "orchestrator_route_selection",
                "selected_candidate_id": "orchestrator",
                "candidate_assessments": [
                    {
                        "candidate_id": "orchestrator",
                        "disposition": "selected",
                        "rationale": "The post has already been read.",
                    }
                ],
                "reasoning": "The acquired post supports a bounded internal note.",
            },
        )
        return _response([_message(output)])


@pytest.mark.parametrize(
    "citation_case",
    [
        "retained-source",
        "invented-repaired",
        "invented-persistent",
        "missing-repaired",
        "missing-persistent",
        "budget-exhausted",
        "repair-error",
    ],
)
def test_general_answer_owner_retains_lazy_read_and_enforces_source_scope(
    tmp_path,
    monkeypatch,
    capsys,
    ks_backend,
    citation_case,
):
    """Native routing may choose Orchestrator; its final answer still needs the read."""
    request = (
        "@KNI CoS Using article #3 posted yesterday, give an internal readiness note from "
        "the saved post and link titles only. Do not open external pages, search, "
        "call other agents, create drafts, or post elsewhere."
    )
    path = _context_file(tmp_path)
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path),
        request_text=request,
    )
    route_model = GeneralContextRouteModel(request)
    preflight = orchestrator.run_orchestrator_preflight(
        request,
        requested_agent="chief_of_staff",
        workflow_state=state,
        run_config=build_local_run_config(Provider(route_model)),
    )
    assert preflight.execution_allowed, preflight.block_reason
    assert preflight.requested_agent == "chief_of_staff"
    assert preflight.selected_agent == "orchestrator"
    assert preflight.manual_request_plan.source == "canonical:orchestrator_general_answer"
    assert not preflight.manual_request_plan.requires_live_search
    assert not preflight.route_result.selected_source_url
    state.update(preflight.workflow_state)
    execution_context = cli._direct_specialist_execution_context(
        request,
        workflow_state=state,
        include_slack_scope=True,
    )
    read = preflight.workflow_state["channel_context_evidence"][0]
    candidate = read["messages"][0]
    model_inputs, answer_calls = [], []

    class AnswerModel(Model):
        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            **kwargs,
        ):
            assert not tools and not handoffs
            serialized = json.dumps(input)
            model_inputs.append(serialized)
            for retained in (
                request,
                ROOT_REQUEST,
                CHANNEL,
                POST_TS,
                REQUEST_TS,
                OLDEST_TS,
                candidate["candidate_id"],
                ARTICLE_URL,
                "historical_context_only",
                "not article reads",
                "coverage",
            ):
                assert retained in serialized
            assert len(model_inputs) <= 2, "Citation correction must be bounded to one repair."
            if len(model_inputs) == 2:
                assert "repair" in serialized.lower()
            corrected = citation_case == "retained-source" or (
                citation_case.endswith("repaired") and len(model_inputs) == 2
            )
            source = (
                f"Source: {ARTICLE_URL}"
                if corrected
                else "Source: https://example.org/articles/invented-source"
                if citation_case.startswith("invented")
                else ""
            )
            output = OrchestratorGeneralAnswer(
                answer=(
                    "The saved post offers a local-model-review link for provisional internal "
                    f"follow-up. The article itself remains unread. {source}"
                ),
                needs_source=False,
            )
            return _response([_message(output)])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only bounded non-streaming answers are expected.")

    actual = cli.run_typed_sdk_agent

    def answer(**kwargs):
        typed_input = kwargs["typed_input"]
        answer_calls.append(typed_input)
        assert typed_input["answer_scope"] == "retrieved_channel_messages_only"
        evidence = typed_input["channel_evidence"]
        assert evidence["authority"] == "historical_context_only"
        assert evidence["messages"][0]["candidate_id"] == candidate["candidate_id"]
        assert evidence["snapshot"]["channel_id"] == CHANNEL
        assert evidence["snapshot"]["request_ts"] == REQUEST_TS
        assert evidence["snapshot"]["oldest_ts"] == OLDEST_TS
        if "validation_feedback" in typed_input:
            feedback = typed_input["validation_feedback"]
            assert feedback["provider_reads_allowed"] is False
            assert feedback["source_selection_owner"] == "model"
            assert kwargs["max_turns"] == 1
            assert kwargs["tool_correction_max_turns"] == 0
            assert kwargs["decision_repair_max_turns"] == 0
            assert kwargs["inherit_env_session"] is False
            assert "https://" not in json.dumps(feedback)
            if citation_case == "repair-error":
                raise RuntimeError("Synthetic repair boundary error")
        return actual(
            **{
                **kwargs,
                "run_config": build_local_run_config(Provider(AnswerModel())),
            }
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", answer)
    with (
        activate_model_request_budget(1) if citation_case == "budget-exhausted" else nullcontext()
    ) as ledger:
        result = cli._run_orchestrator_general_answer_live(
            request,
            manual_plan=preflight.manual_request_plan,
            orchestrator_preflight=preflight,
            sdk_session_spec=None,
            database_url=f"sqlite:///{tmp_path / 'general-context.db'}",
            execution_context=execution_context,
            json_output=True,
            explicit_live_search=False,
        )
        if citation_case == "budget-exhausted":
            assert ledger.snapshot()["consumed"] == 1
            assert ledger.snapshot()["exhausted"] is True
    payload = json.loads(capsys.readouterr().out)
    expected_turns = (
        1 if citation_case in {"retained-source", "budget-exhausted", "repair-error"} else 2
    )
    assert len(route_model.inputs) == 3 and len(model_inputs) == expected_turns
    assert payload["usage"]["requests"] == expected_turns
    verdict = payload["answer_validation"]
    assert verdict["repair_attempted"] is (citation_case != "retained-source")
    assert len(answer_calls) == (1 if citation_case == "retained-source" else 2)
    assert "https://" not in json.dumps(verdict)
    assert verdict["initial"]["reason"] == (
        "passed"
        if citation_case == "retained-source"
        else "source_not_returned"
        if citation_case.startswith("invented")
        else "visible_source_missing"
    )
    assert verdict["initial"]["unsupported_url_count"] == int(citation_case.startswith("invented"))
    expected_pass = citation_case == "retained-source" or citation_case.endswith("repaired")
    assert verdict["final"]["passed"] is expected_pass
    if citation_case in {"budget-exhausted", "repair-error"}:
        assert verdict["repair_error_type"] == (
            "ModelRequestBudgetExhausted" if citation_case == "budget-exhausted" else "RuntimeError"
        )
    assert len(ks_backend) == 2
    assert payload["selected_agent"] == "orchestrator"
    assert payload["send_enabled"] is False
    persisted = SQLiteStore(f"sqlite:///{tmp_path / 'general-context.db'}").get_agent_run(
        payload["agent_run_id"]
    )
    assert persisted is not None
    assert persisted["output"]["answer_validation"] == verdict
    assert "invented-source" not in json.dumps(persisted["output"])
    assert (
        payload["channel_answer_evidence"]["messages"][0]["candidate_id"]
        == (candidate["candidate_id"])
    )
    if not expected_pass:
        assert payload["status"] == "blocked" and not payload["completion_confirmed"]
        assert "invented-source" not in payload["human_summary"]
    else:
        assert result == 0 and payload["status"] == "completed"
        assert payload["completion_confirmed"] is True
        assert ARTICLE_URL in payload["human_summary"]


def test_authorized_search_uses_its_own_receipts_after_historical_context_read(
    tmp_path,
    monkeypatch,
    capsys,
    ks_backend,
):
    """A historical read must not constrain a separately authorized public source."""
    request = "@KNI Review article #3 posted yesterday and check one current public release."
    path = _context_file(tmp_path)
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path),
        request_text=request,
    )
    route_model = GeneralContextRouteModel(request)
    preflight = orchestrator.run_orchestrator_preflight(
        request,
        workflow_state=state,
        run_config=build_local_run_config(Provider(route_model)),
    )
    assert preflight.execution_allowed
    assert preflight.workflow_state["channel_context_evidence"]
    # Supply the explicit search execution contract as a diagnostic control;
    # this case does not claim the preceding scripted route selected search.
    plan = preflight.manual_request_plan.model_copy(
        update={
            "requires_live_search": True,
            "provider_operations": ["search"],
        }
    )
    public_url = "https://example.org/current-public-release"
    search_calls, model_inputs = [], []

    @function_tool
    def search_web(query: str) -> str:
        """Return one injected current public result without provider IO."""
        search_calls.append(query)
        return json.dumps(
            [SearchResult(title="Current public release", link=public_url).model_dump()]
        )

    actual_builder = cli.build_orchestrator_general_answer_agent

    def builder(**kwargs):
        assert kwargs["live_search"] is True
        return actual_builder(**kwargs).clone(tools=[search_web])

    class SearchModel(Model):
        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            **kwargs,
        ):
            assert [tool.name for tool in tools] == ["search_web"] and not handoffs
            model_inputs.append(json.dumps(input))
            assert "channel_evidence" not in model_inputs[-1]
            if len(model_inputs) == 1:
                assert "one_bounded_current_public_search" in model_inputs[-1]
                return _response(
                    [
                        ResponseFunctionToolCall(
                            type="function_call",
                            name="search_web",
                            call_id="public-search",
                            status="completed",
                            arguments=json.dumps({"query": "current public release"}),
                        )
                    ]
                )
            assert len(model_inputs) == 2 and public_url in model_inputs[-1]
            return _response(
                [
                    _message(
                        OrchestratorGeneralAnswer(
                            answer=(
                                "The supplied search result describes the current release. "
                                f"Source: {public_url}"
                            ),
                            needs_source=False,
                        )
                    )
                ]
            )

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only a bounded non-streaming search loop is expected.")

    actual_runner = cli.run_typed_sdk_agent

    def run_answer(**kwargs):
        return actual_runner(
            **{
                **kwargs,
                "run_config": build_local_run_config(Provider(SearchModel())),
            }
        )

    monkeypatch.setattr(cli, "build_orchestrator_general_answer_agent", builder)
    monkeypatch.setattr(cli, "run_typed_sdk_agent", run_answer)
    monkeypatch.setattr(cli, "cli_default_live_research", lambda: True)
    result = cli._run_orchestrator_general_answer_live(
        request,
        manual_plan=plan,
        orchestrator_preflight=preflight,
        sdk_session_spec=None,
        database_url=f"sqlite:///{tmp_path / 'mixed-search.db'}",
        execution_context=None,
        json_output=True,
        explicit_live_search=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 0 and payload["status"] == "completed", payload
    assert payload["search_answer_evidence"]["passed"]
    assert "channel_answer_evidence" not in payload
    assert public_url in payload["human_summary"]
    assert search_calls == ["current public release"] and len(model_inputs) == 2
    assert len(ks_backend) == 2


@pytest.mark.parametrize("entrypoint", ["cli-direct", "slack-command"])
def test_lazy_preview_reaches_actual_chief_sdk_through_cli_child_contract(
    tmp_path,
    monkeypatch,
    capsys,
    ks_backend,
    entrypoint,
):
    """Replace process transport, execute real script.main and the native CoS SDK."""
    request = (
        "@KNI CoS Assess whether article #3 posted yesterday deserves internal follow-up. "
        "Use only its saved Slack preview; do not read the article, run new research, "
        "send outreach, or write externally."
    )
    path = _context_file(tmp_path)
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path),
        request_text=request,
    )
    route_model = ChiefRouteModel(request)
    preflight = orchestrator.run_orchestrator_preflight(
        request,
        requested_agent="chief_of_staff",
        workflow_state=state,
        run_config=build_local_run_config(Provider(route_model)),
    )
    assert preflight.execution_allowed, preflight.block_reason
    assert preflight.selected_agent == "chief_of_staff"
    assert len(route_model.inputs) == 3 and len(ks_backend) == 2
    # The CLI merges newly acquired fields into its existing root/current state;
    # preflight.workflow_state intentionally carries only the new channel fields.
    state.update(preflight.workflow_state)
    execution_context = cli._direct_specialist_execution_context(
        request,
        workflow_state=state,
        include_slack_scope=True,
    )
    assert execution_context["channel_context_evidence"]
    script_path = Path(__file__).resolve().parents[1] / "scripts/run_chief_of_staff.py"
    spec = importlib.util.spec_from_file_location("synthetic_chief_backend_script", script_path)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    model_inputs, child_calls, sdk_calls = [], [], []

    class ChiefModel(Model):
        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            **kwargs,
        ):
            serialized = json.dumps(input)
            model_inputs.append(serialized)
            assert not tools and not handoffs
            for retained in (
                request,
                ROOT_REQUEST,
                CHANNEL,
                POST_TS,
                REQUEST_TS,
                OLDEST_TS,
                ARTICLE_URL,
                "channel_context_evidence",
                "historical_context_only",
            ):
                assert retained in serialized
            assert "not article reads" in serialized
            output = ChiefOfStaffResult(
                mode="llm",
                intent="internal article follow-up priority assessment",
                summary=(
                    "The saved article preview deserves low-priority internal follow-up; "
                    "it does not establish an opportunity or verified article contents."
                ),
                synthesis="This assessment uses the saved Slack preview only.",
                recommended_route={
                    "workflow_type": "project-context-review",
                    "target_channel": "current-thread",
                    "rationale": "Preview-only internal review.",
                },
                sources=[
                    {
                        "title": "Saved article preview",
                        "url": ARTICLE_URL,
                        "source_type": "slack_message",
                        "note": "Article contents were not read.",
                    }
                ],
                recommended_actions=["Review the article later before making an external claim."],
                decision={
                    "decision_owner": "chief_of_staff",
                    "decision_stage": "chief_delegation_selection",
                    "selected_candidate_id": "workflow:project-context-review",
                    "candidate_assessments": [
                        {
                            "candidate_id": "workflow:project-context-review",
                            "disposition": "selected",
                            "rationale": "The current ask is internal review.",
                        }
                    ],
                    "reasoning": "A saved preview supports only a provisional operating priority.",
                    "needs_more_context": False,
                },
            )
            return _response([_message(output)])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only a non-streaming scripted CoS turn is expected.")

    actual_chief = script.run_chief_of_staff_sdk

    def invoke_chief(typed_input, **kwargs):
        sdk_calls.append(typed_input)
        assert (
            typed_input["execution_context"]["channel_context_evidence"]
            == (execution_context["channel_context_evidence"])
        )
        return actual_chief(
            typed_input,
            **{
                **kwargs,
                "run_config": build_local_run_config(Provider(ChiefModel())),
            },
        )

    monkeypatch.setattr(script, "run_chief_of_staff_sdk", invoke_chief)

    def child(command, *, cwd, env, timeout):
        child_calls.append(command)
        assert command[1] == "scripts/run_chief_of_staff.py"
        assert "--live-search" not in command
        stdout = io.StringIO()
        # Process isolation is the sole replaced boundary. The real script loads
        # exactly the parent-produced environment contract and calls the real SDK.
        with patch.dict(os.environ, env, clear=True), redirect_stdout(stdout):
            returncode = Context().run(script.main, command[2:])
        return subprocess.CompletedProcess(command, returncode, stdout.getvalue(), "")

    monkeypatch.setattr(cli, "run_isolated_child_process", child)
    database_url = f"sqlite:///{tmp_path / 'chief-direct.db'}"
    if entrypoint == "cli-direct":
        result = cli._run_ask_specialist_live(
            "chief_of_staff",
            request,
            json_output=True,
            manual_plan=preflight.manual_request_plan,
            orchestrator_preflight=preflight,
            execution_context=execution_context,
            context_file_path=str(path),
            database_url=database_url,
        )
        payload = json.loads(capsys.readouterr().out)
    else:
        bridge = importlib.import_module("kni_integrations.business_agents_bridge")
        bridge_calls, preflight_calls = [], []
        configuration = {
            "KNI_BUSINESS_AGENTS_LIVE_SDK": "true",
            "KNI_BUSINESS_AGENTS_LIVE_SEARCH": "false",
            "KNI_BUSINESS_AGENTS_MAX_OPENAI_REQUESTS": "8",
            "KNI_BUSINESS_AGENTS_SLACK_CONTEXT_DIR": str(tmp_path / "bridge-contexts"),
        }
        monkeypatch.setattr(
            bridge, "_env", lambda name, default=None: configuration.get(name, default)
        )
        monkeypatch.setattr(bridge, "_business_repo", lambda: Path(__file__).resolve().parents[1])
        monkeypatch.setattr(bridge, "_business_database_url", lambda repo: database_url)

        def admitted_preflight(input_text, **kwargs):
            # The native query/read loop above produced this result. Replay only
            # that result while testing the upstream transport and CLI dispatcher;
            # this variant does not claim a second model-selection run.
            supplied = kwargs["workflow_state"]
            assert supplied["channel_context_capability"] == state["channel_context_capability"]
            assert supplied["slack_thread_root"] == ROOT_REQUEST
            preflight_calls.append(input_text)
            return preflight

        def canonical_child(command, *, cwd, env_overrides):
            assert command[1:4] == ["-m", "keystone_agents.cli", "ask"]
            assert "--live-sdk" in command and "--live-search" not in command
            context_path = command[command.index("--context-file") + 1]
            carried = json.loads(Path(context_path).read_text())
            assert carried["channel_context_capability"] == state["channel_context_capability"]
            assert carried["channel_id"] == CHANNEL and carried["request_ts"] == REQUEST_TS
            assert not carried["read_context"]
            stdout = io.StringIO()
            with patch.dict(os.environ, env_overrides), redirect_stdout(stdout):
                returncode = cli.main(command[3:])
            bridge_calls.append(stdout.getvalue())
            return subprocess.CompletedProcess(command, returncode, stdout.getvalue(), "")

        monkeypatch.setattr(cli, "run_orchestrator_preflight", admitted_preflight)
        monkeypatch.setattr(bridge, "_run_command", canonical_child)
        context = bridge.SlackBusinessAgentContext(
            channel_id=CHANNEL,
            thread_ts=REQUEST_TS,
            request_ts=REQUEST_TS,
            thread_root_request=ROOT_REQUEST,
            channel_context_capability=state["channel_context_capability"],
        )
        bridged = bridge._handle_business_agents_command(
            request.removeprefix("@KNI "),
            slack_context=context,
        )
        assert len(bridge_calls) == 1 and len(preflight_calls) == 1
        assert bridged.handled and bridged.returncode == 0, bridge_calls[0]
        assert "preview" in bridged.text.lower()
        result = bridged.returncode
        payload = json.loads(bridge_calls[0])
    assert result == 0, payload
    assert len(child_calls) == 1 and len(sdk_calls) == 1 and len(model_inputs) == 1
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["output"]["mode"] == "llm"
    assert payload["output"]["recommended_route"]["workflow_type"] == "project-context-review"
    assert payload["output"]["sources"][0]["url"] == ARTICLE_URL
    assert "preview" in payload["human_summary"].lower()
    assert payload["send_enabled"] is False
    assert payload["sdk_session"] is None or payload["sdk_session"]["enabled"] is False
    persisted = SQLiteStore(database_url).get_agent_run(payload["agent_run_id"])
    assert persisted is not None and persisted["agent_name"] == "chief_of_staff"
    assert persisted["output"]["output"]["sources"][0]["url"] == ARTICLE_URL
    assert len(ks_backend) == 2, "Child handoff must reuse the accepted channel read."


@pytest.mark.parametrize(
    "durable,multistage,expected",
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
    ],
)
def test_graph_selection_follows_canonical_state_and_stage_contract(
    monkeypatch,
    durable,
    multistage,
    expected,
):
    """Selection control only; the separate native graph test proves execution."""
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    workflow = ["business_research_analyst"]
    if multistage:
        workflow.append("opportunity_scout")
    plan = ManualRequestPlan(
        source="canonical:synthetic",
        target_agent="business_research_analyst",
        intent="research_brief",
        task_objective="source_research",
        primary_target=ARTICLE_URL,
        target_type="url",
        expected_artifact_type="source_summary",
        requires_durable_state=durable,
        workflow=workflow,
    )
    request = WorkflowRunRequest(
        request_text="Read the selected source and assess the next internal step.",
        manual_request_plan=plan.model_dump(mode="json"),
    )
    assert (
        langgraph_workflow.should_use_langgraph_for_work_item(request, manager_loop=True)
        is expected
    )


@pytest.mark.parametrize(
    "permitted,requested_owner",
    [
        (None, ""),
        (("orchestrator", "business_research_analyst"), ""),
        (("orchestrator", "rag_retrieval_specialist"), ""),
        ((), ""),
        (("rag_retrieval_specialist",), "business_research_analyst"),
    ],
)
def test_route_prompt_and_validator_share_exact_effective_candidate_universe(
    permitted, requested_owner,
):
    from keystone_agents.agent_decision_contracts import orchestrator_decision_contract
    from keystone_agents.capabilities.profile import EffectiveRuntimeRouteScope, RuntimeOwnerScope

    scope = None if permitted is None else EffectiveRuntimeRouteScope(
        scope_source="synthetic_acceptance_scope",
        owner_scopes=tuple(RuntimeOwnerScope(route=route) for route in permitted),
    )
    advice = {"manual_request_plan": ManualRequestPlan(
        requested_agent=requested_owner,
    ).model_dump(mode="json")} if requested_owner else {}
    bounded = orchestrator._bounded_orchestrator_route_candidates(advice, scope)
    prompt = json.loads(orchestrator._llm_route_prompt(
        "Review the synthetic supplied context.", {}, routing_advice=advice,
        allowed_routes=bounded, runtime_scope=scope,
    ))
    contract = orchestrator_decision_contract(bounded)
    accepted = contract.evidence_resolver(None).candidate_ids
    assert set(prompt["allowed_routes"]) == set(accepted)
    assert len(prompt["allowed_routes"]) == len(set(prompt["allowed_routes"]))
    if permitted is not None:
        assert set(accepted) <= {*permitted, "clarification"}
    if requested_owner:
        assert accepted == ("clarification",)


def test_native_route_accepts_advertised_rag_owner_for_local_retrieval():
    """Native routing acceptance only; no claim that document retrieval executed."""
    request = "Find the saved synthetic runbook guidance and cite its local source."
    observed = []

    class LocalRetrievalRouteModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            observed.append(json.dumps(input))
            assert request in observed[-1] and "rag_retrieval_specialist" in observed[-1]
            assert not tools and not handoffs
            output = OrchestratorPlanningResult(
                route="rag_retrieval_specialist", workflow=["rag_retrieval_specialist"],
                rationale="The requested evidence is a saved local document.",
                decision={
                    "decision_owner": "orchestrator",
                    "decision_stage": "orchestrator_route_selection",
                    "selected_candidate_ids": ["rag_retrieval_specialist"],
                    "candidate_assessments": [{
                        "candidate_id": "rag_retrieval_specialist", "disposition": "selected",
                        "rationale": "Retrieve the local runbook evidence before answering.",
                    }],
                    "reasoning": "The advertised retrieval owner fits the supplied task.",
                },
            )
            return _response([_message(output)])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only non-streaming native routing is expected.")

    route = orchestrator._route_ambiguous_with_llm(
        request, approved_context_present=False, workflow_state={},
        run_config=build_local_run_config(Provider(LocalRetrievalRouteModel())),
    )
    assert len(observed) == 1
    assert route.route == "rag_retrieval_specialist"
    assert route.workflow == ["rag_retrieval_specialist"]


@pytest.mark.parametrize(
    "binding", [
        "exact", "exact-channel-rag", "exact-channel-repair", "exact-channel-budget",
        "exact-heuristic-source", "exact-different-source", "exact-source-ambiguous",
        "exact-write-plan", "exact-broad-search",
        "exact-artifact-unrelated", "exact-artifact-stale",
        "exact-pilot-no-auto-memory",
        "ambiguous", "other-channel", "other-thread",
    ],
)
def test_slack_public_continuation_binds_exact_saved_work_item_before_native_preflight(
    tmp_path, monkeypatch, ks_backend, research_sdk_control, binding,
):
    """Real public Slack dispatch and CLI must update the named row, never replace it."""
    from keystone_agents.agents import manual_request_planner
    from keystone_agents.orchestration.checkpoints import inspect_graph_execution

    bridge = importlib.import_module("kni_integrations.business_agents_bridge")
    valid_binding = binding.startswith("exact")
    pilot_no_auto_memory = binding == "exact-pilot-no-auto-memory"
    automatic_memory_reads = []
    artifact_variant = binding in {"exact-artifact-unrelated", "exact-artifact-stale"}
    retained_source_success = (
        binding == "exact-heuristic-source" or artifact_variant or pilot_no_auto_memory
    )
    saved_source_binding = binding in {
        "exact-heuristic-source", "exact-different-source", "exact-source-ambiguous",
        "exact-write-plan", "exact-broad-search",
    } or artifact_variant or pilot_no_auto_memory
    semantic_rejected = saved_source_binding and not retained_source_success
    with_channel_rag = binding.startswith("exact-channel") or retained_source_success
    needs_decision_repair = binding in {"exact-channel-repair", "exact-channel-budget"}
    repair_budget_blocked = binding == "exact-channel-budget"
    database_url = f"sqlite:///{tmp_path / 'exact-slack-resume.db'}"
    store = SQLiteStore(database_url)
    selected_id, second_id, quoted_id = ("wi_" + digit * 32 for digit in "123")
    root = f"Prior diagnostic concerned {quoted_id}; its identity is historical only."
    current = (
        f"Resume the exact existing WorkItem {selected_id} and finish its saved article "
        "evidence summary. Preserve its identity. Do not send or write externally."
    )
    if binding == "ambiguous":
        current += f" Also resume {second_id}."
    alternative_url = "https://example.org/articles/another-public-source"
    historical_artifact = historical_source = None
    if binding == "exact-different-source":
        current += f" Replace the selected source with {alternative_url}."
    if binding == "exact-write-plan":
        current += " Stage an external tracker update plan for approval."
    if binding == "exact-broad-search":
        current += " Run a broad public search for competing sources."
    wrapped = (
        "Continue this prior Slack thread. "
        f"Previous request: {root} Previous result: The previous source summary was blocked. "
        f"User follow-up: {current}"
    )
    item = WorkItem(
        id=selected_id,
        kind=WorkItemKind.COMPANY_RESEARCH if saved_source_binding else WorkItemKind.RESEARCH_BRIEF,
        status=WorkItemStatus.BLOCKED,
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        title="Saved exact article evidence summary",
        request_text="Read the selected public article and save its evidence summary.",
        target=WorkItemTarget(
            name=current if saved_source_binding else "Local model review",
            url=(f"{ARTICLE_URL} {alternative_url}"
                 if binding == "exact-source-ambiguous" else ARTICLE_URL),
            metadata={
            "slack_context": {
                "channel_id": "C222OTHER" if binding == "other-channel" else CHANNEL,
                "thread_ts": REQUEST_TS if binding == "other-thread" else POST_TS,
            },
            },
        ),
        sources=[
            WorkItemSourceRef(
                source_id="saved-public-source", url=ARTICLE_URL, source_type="company_site",
                provider="selected-url", title="Local model review", extraction_status="extracted",
                key_facts=["Separate local review is described."],
                evidence_excerpt=(
                    "A local drafting model is evaluated by a separate local reviewer."
                ),
            ),
            WorkItemSourceRef(
                source_id="unrelated-context", url="https://example.net/catalog/saved-context",
                source_type="dry_run_context_handoff", provider="zotero_context_agent",
                extraction_status="context_handoff", title="Unrelated saved context",
                evidence_excerpt="This other context must not replace the selected public source.",
            ),
        ] if saved_source_binding else [],
        blockers=[WorkItemBlocker(
            code="business_research_analyst_execution_failed",
            message="The prior synthetic run exhausted its model request budget.",
        )],
        next_action=WorkItemNextAction(
            action="review_business_research_analyst_execution_failure",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
    )
    if saved_source_binding:
        preview, access = extraction_tool.project_web_source(
            extraction_tool.WebsiteExtractionResult(
                url=ARTICLE_URL, title="Local model review", provider="trafilatura",
                status="success", text_or_markdown=(
                    "A local drafting model is evaluated by a separate local reviewer."
                    + " Bounded synthetic evidence." * 60
                ),
            ),
            source_id="saved-public-source", selected_url=ARTICLE_URL, max_chars=1200,
        )
        item.sources[0] = item.sources[0].model_copy(update={
            "evidence_excerpt": preview, "web_source_access": access,
        })
        if artifact_variant:
            old_id = "saved-public-source" if binding == "exact-artifact-stale" else "old-source"
            old_url = ARTICLE_URL if binding == "exact-artifact-stale" else alternative_url
            old_preview, old_access = extraction_tool.project_web_source(
                extraction_tool.WebsiteExtractionResult(
                    url=old_url, title="Historical artifact source", provider="trafilatura",
                    status="success", text_or_markdown=(
                        "STALE_ARTIFACT_EVIDENCE_SENTINEL " + "Outdated evidence. " * 100
                    ),
                ),
                source_id=old_id, selected_url=old_url, max_chars=1200,
            )
            historical_source = WorkItemSourceRef(
                source_id=old_id, url=old_url, title="Historical artifact source",
                source_type="company_site", provider="selected-url", extraction_status="extracted",
                evidence_excerpt=old_preview, web_source_access=old_access,
            )
            historical_artifact = WorkItemArtifactRef(
                artifact_id="historical-research-artifact", artifact_type="research_brief",
                source_agent="business_research_analyst", selected=True,
                summary="HISTORICAL_ARTIFACT_CLAIM_SENTINEL",
                metadata={
                    "source_refs": [historical_source.model_dump(mode="json")],
                    "web_source_access": old_access.model_dump(mode="json"),
                },
            )
            item.artifact_refs.append(historical_artifact)
            from keystone_agents.business_research_analyst.supplied_context import (
                supplied_research_sources,
            )
            # Ordinary artifact-discovery behavior remains supported. The real
            # resume below must explicitly narrow its evidence projection.
            ordinary = supplied_research_sources(item.model_copy(update={"sources": []}))
            assert ordinary == (historical_source,)
        item.blockers.append(WorkItemBlocker(
            code="business_research_analyst_agent_decision_invalid",
            message="The prior synthetic run did not satisfy its research tool contract.",
        ))
        item.next_action = WorkItemNextAction(
            action="retry_business_research_analyst_agent_decision",
            agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        )
    store.save_work_item(item)
    if pilot_no_auto_memory:
        store.save_memory_item(MemoryItem(
            memory_type="retrieval_tool_performance", object_type="workflow",
            object_key=item.target.name, title="AUTOMATIC_METRIC_MEMORY_SENTINEL",
            summary="Previously recorded article evidence summary retrieval metrics.",
            approval_state="approved_for_research",
        ))
        monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
        monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS", CHANNEL)

        def no_automatic_memory(*args, **kwargs):
            automatic_memory_reads.append(True)
            raise AssertionError("Pilot graph must use explicit scoped memory queries only.")

        monkeypatch.setattr(SQLiteStore, "retrieve_memory", no_automatic_memory)
    if binding == "ambiguous":
        store.save_work_item(item.model_copy(update={"id": second_id}))
    before_ids = {row.id for row in store.list_work_items(status=None)}
    initial_plan = ManualRequestPlan(
        source="llm", target_agent="business_research_analyst", intent="research_brief",
        task_objective="source_research", primary_target=ARTICLE_URL, target_type="url",
        expected_artifact_type="source_summary", requires_durable_state=True,
        side_effect_policy="draft_or_read_only", workflow=["business_research_analyst"],
        provider_operations=["read"],
        ask_shape=AskShapePolicy(
            prior_context_dependency="selected_context", permission_state="read_only",
        ),
    )
    if saved_source_binding:
        # Match the actual failed resume's current heuristic interpretation. The
        # latest stored plan was also overwritten by that failed interpretation.
        # Neither plan supplies a URL; only exact typed target/source state does.
        initial_plan = initial_plan.model_copy(update={
            "source": "heuristic", "primary_target": current, "target_type": "unknown",
            "provider_operations": [], "provider_read_scope": "unspecified",
            "requires_live_search": binding == "exact-broad-search",
        })
        item.target.metadata.update({
            "manual_request_plan": initial_plan.model_copy(update={
                "requires_live_search": False, "primary_target": "Prior source summary request.",
            }).model_dump(mode="json"),
            "manual_expected_artifact_type": "source_summary",
            "manual_task_objective": "source_research",
        })
        store.save_work_item(item)
        if binding == "exact-different-source":
            initial_plan = initial_plan.model_copy(update={
                "primary_target": alternative_url, "target_type": "url",
                "provider_operations": ["read"],
            })
        if binding == "exact-write-plan":
            initial_plan = initial_plan.model_copy(update={
                "provider_operations": ["update"], "intent": "business_system_write",
                "side_effect_policy": "approval_required",
            })
    monkeypatch.setattr(
        manual_request_planner, "resolve_manual_request_plan", lambda *a, **k: initial_plan,
    )
    native_inputs, preflight_states, commands, payloads, extraction_calls = [], [], [], [], []
    preflight_results = []
    diagnostic_budgets = []
    resumed_window_calls = []
    resumed_sdk_errors = []
    observed_local_reads, rejected_artifact_reads = [], []
    if artifact_variant:
        actual_local_read = extraction_tool.read_web_source_window_impl

        def local_read(**kwargs):
            observed_local_reads.append(kwargs)
            return actual_local_read(**kwargs)

        monkeypatch.setattr(extraction_tool, "read_web_source_window_impl", local_read)

    class ResumeModel(Model):
        query_receipt = None
        read_receipt = None

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            serialized = json.dumps(input)
            native_inputs.append(serialized)
            assert selected_id in serialized and current in serialized
            assert ARTICLE_URL in serialized
            assert "blocked" in serialized
            repairing = needs_decision_repair and len(native_inputs) == 4
            if repairing:
                assert not tools and not handoffs
                assert "synthetic_unavailable_route" in serialized
                assert "candidate_assessment_identity_not_in_candidate_set" in serialized
                assert self.read_receipt is not None
                recovery = next(
                    item["content"] for item in input if isinstance(item, dict)
                    and isinstance(item.get("content"), str)
                    and "Replayed tool evidence: " in item["content"]
                )
                replay, _ = json.JSONDecoder().raw_decode(
                    recovery.split("Replayed tool evidence: ", 1)[1].lstrip()
                )
                assert len(replay) == 2
                assert {entry["tool_name"]: entry["output"] for entry in replay} == {
                    "query_slack_channel_context": self.query_receipt,
                    "read_slack_channel_context": self.read_receipt,
                }
                message = self.read_receipt["messages"][0]
                body = message.get("excerpt") or message.get("text")
                assert body
                for retained in (
                    message["candidate_id"], *body.splitlines(), ARTICLE_URL,
                    "historical_context_only", "coverage", "allowed_routes",
                ):
                    assert retained in serialized
                assert len(ks_backend) == 2, "Repair must replay reads without reacquisition."
            elif with_channel_rag:
                assert "rag_retrieval_specialist" in serialized
                outputs = [item for item in input if isinstance(item, dict)
                           and item.get("type") == "function_call_output"]
                if not outputs:
                    names = {tool.name for tool in tools}
                    slack_tools = {
                        "query_slack_channel_context", "read_slack_channel_context",
                    }
                    if pilot_no_auto_memory:
                        assert slack_tools <= names
                    else:
                        assert names == slack_tools
                    return _response([ResponseFunctionToolCall(
                        type="function_call", name="query_slack_channel_context",
                        call_id="resume-query", status="completed", arguments=json.dumps({
                            "query": "", "from_ts": "2026-11-13T00:00:00-05:00",
                            "to_ts": "2026-11-14T00:00:00-05:00", "limit": 8,
                        }),
                    )])
                latest = json.loads(outputs[-1]["output"])
                assert latest["ok"] and ARTICLE_URL in json.dumps(latest), json.dumps(latest)
                if outputs[-1]["call_id"] == "resume-query":
                    self.query_receipt = latest
                    return _response([ResponseFunctionToolCall(
                        type="function_call", name="read_slack_channel_context",
                        call_id="resume-read", status="completed", arguments=json.dumps({
                            "candidate_id": latest["messages"][0]["candidate_id"],
                        }),
                    )])
                assert outputs[-1]["call_id"] == "resume-read"
                assert latest["messages"][0]["content_kind"] == "message"
                self.read_receipt = latest
            output = OrchestratorPlanningResult(
                route="business_research_analyst", workflow=["business_research_analyst"],
                selected_source_url=(
                    alternative_url if binding == "exact-different-source"
                    else ARTICLE_URL if with_channel_rag and not saved_source_binding else ""
                ),
                requires_live_search=binding == "exact-broad-search",
                rationale="Finish the authenticated existing source-summary WorkItem.",
                decision={
                    "decision_owner": "orchestrator",
                    "decision_stage": "orchestrator_route_selection",
                    "selected_candidate_id": "business_research_analyst",
                    "candidate_assessments": [{
                        "candidate_id": "business_research_analyst",
                        "disposition": "selected",
                        "rationale": "Finish the bound source-summary task on its owning route.",
                    }],
                    "reasoning": "The current operator explicitly selected the existing task.",
                },
            )
            if with_channel_rag:
                assessment = type(output.decision.candidate_assessments[0])
                output.decision.candidate_assessments.append(assessment(**{
                    "candidate_id": (
                        "synthetic_unavailable_route"
                        if needs_decision_repair and not repairing else "rag_retrieval_specialist"
                    ),
                    "disposition": "excluded",
                    "rationale": "The exact saved public source belongs to Business Research.",
                }))
            return _response([_message(output)])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only a non-streaming native preflight is expected.")

    actual_preflight = cli.run_orchestrator_preflight

    def preflight(input_text, **kwargs):
        supplied = kwargs["workflow_state"]
        preflight_states.append(supplied)
        assert valid_binding, "Invalid identity must fail before a model request."
        bound = supplied.get("current_work_item", {})
        assert bound.get("id") == selected_id
        assert bound.get("status") == "blocked" and bound["target"]["url"] == item.target.url
        assert supplied["execution_continuation"]["work_item_id"] == selected_id
        if pilot_no_auto_memory:
            from keystone_agents.runtime.continuity_policy import current_continuity_policy

            assert current_continuity_policy().eligible
        # This diagnostic deliberately exhausts a nested three-request ceiling;
        # the normal CLI transport/outer admission ceiling remains unchanged.
        budget_context = (
            activate_model_request_budget(3) if repair_budget_blocked else nullcontext()
        )
        with budget_context as budget:
            try:
                routed = actual_preflight(input_text, **{
                    **kwargs, "live_manual_plan": False,
                    "run_config": build_local_run_config(Provider(ResumeModel())),
                })
            finally:
                if repair_budget_blocked:
                    diagnostic_budgets.append(budget.snapshot())
        assert routed.manual_request_plan.target_agent == "business_research_analyst", (
            routed.model_dump(mode="json")
        )
        preflight_results.append(routed)
        return routed

    def extract(url, **kwargs):
        extraction_calls.append(url)
        assert url == ARTICLE_URL
        return extraction_tool.WebsiteExtractionResult(
            url=url, title="Local model review", provider="trafilatura", status="success",
            text_or_markdown=(
                "A local drafting model is evaluated by a separate local reviewer."
                + (" Bounded synthetic evidence." * 60
                   if binding == "exact-heuristic-source" else "")
            ),
            claims=["The article describes a local review workflow."],
        )

    def bundle(**kwargs):
        return extraction_tool.build_selected_url_source_bundle(
            **kwargs, extractor=extract, primary_provider="trafilatura", fallback_providers=[],
            **({"max_text_chars_per_source": 1200} if binding == "exact-heuristic-source" else {}),
        )

    if retained_source_success:
        from keystone_agents.agents.business_research_analyst import (
            run_business_research_analyst_research_brief_sdk,
        )
        from keystone_agents.schemas.research import ResearchBrief

        def resumed_research(typed_input, **kwargs):
            assert kwargs["provider_retrieval_required"] is False
            packet = json.loads(typed_input.source_context)
            sources = packet["source_catalog"]
            if pilot_no_auto_memory:
                assert all(packet["context_pack"][field] == [] for field in (
                    "relevant_memory_refs", "approved_company_facts", "retrieval_performance_notes",
                ))
            assert {(source["source_id"], source["url"]) for source in sources} == {
                ("saved-public-source", ARTICLE_URL),
            }
            pending, accesses = [packet], []
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if isinstance(value.get("web_source_access"), dict):
                        accesses.append(value["web_source_access"])
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
            access = next(value for value in accesses if not value["content_complete"])

            class ResumedSourceModel(Model):
                async def get_response(self, system_instructions, input, model_settings, tools,
                                       output_schema, handoffs, tracing, **model_kwargs):
                    serialized = json.dumps(input)
                    assert current in serialized and selected_id in serialized
                    if pilot_no_auto_memory:
                        assert "AUTOMATIC_METRIC_MEMORY_SENTINEL" not in serialized
                    assert not handoffs and "search_web" not in {tool.name for tool in tools}
                    outputs = [item for item in input if isinstance(item, dict)
                               and item.get("type") == "function_call_output"]
                    if not outputs:
                        research_sdk_control.append({
                            "typed_input": typed_input, "model_input": serialized,
                        })
                        assert {tool.name for tool in tools} == {"read_web_source_window"}
                        if artifact_variant:
                            old_access = historical_source.web_source_access
                            for marker in (
                                "STALE_ARTIFACT_EVIDENCE_SENTINEL",
                                "HISTORICAL_ARTIFACT_CLAIM_SENTINEL", old_access.snapshot_sha256,
                            ):
                                if marker in serialized:
                                    raise AssertionError(f"Historical input leak: {marker}")
                            if binding == "exact-artifact-unrelated":
                                assert historical_source.source_id not in serialized, (
                                    "Old ID leaked"
                                )
                                assert historical_source.url not in serialized, "Old URL leaked"
                            # Deliberately guessed historical arguments probe the
                            # actual advertised SDK reader, not admitted context.
                            return _response([ResponseFunctionToolCall(
                                type="function_call", name="read_web_source_window",
                                call_id="rejected-historical-window", status="completed",
                                arguments=json.dumps({
                                    "source_id": old_access.source_id,
                                    "selected_url": old_access.selected_url,
                                    "expected_snapshot_sha256": old_access.snapshot_sha256,
                                    "start_char": old_access.next_start_char, "max_chars": 8000,
                                }),
                            )])
                    if not outputs or outputs[-1]["call_id"] == "rejected-historical-window":
                        if outputs:
                            rejected_artifact_reads.append(outputs[-1]["output"])
                            assert "outside this input scope" in outputs[-1]["output"]
                            assert "STALE_ARTIFACT_EVIDENCE_SENTINEL" not in outputs[-1]["output"]
                            assert not observed_local_reads
                        arguments = {
                            "source_id": access["source_id"],
                            "selected_url": access["selected_url"],
                            "expected_snapshot_sha256": access["snapshot_sha256"],
                            "start_char": access["next_start_char"], "max_chars": 8000,
                        }
                        resumed_window_calls.append(arguments)
                        return _response([ResponseFunctionToolCall(
                            type="function_call", name="read_web_source_window",
                            call_id="resumed-source-window", status="completed",
                            arguments=json.dumps(arguments),
                        )])
                    try:
                        window = json.loads(outputs[-1]["output"])
                    except json.JSONDecodeError:
                        window = ast.literal_eval(outputs[-1]["output"])
                    assert window["reached_end"]
                    ids = [source["source_id"] for source in sources]
                    output = ResearchBrief(
                        target_name=typed_input.target_name, target_type=typed_input.target_type,
                        summary="The exact resumed source describes separate local review.",
                        facts=[{"text": "Separate local review is described.", "source_ids": ids}],
                        source_ids_used=ids, sources=sources,
                        limitations=["Scripted native resume wiring diagnostic only."],
                        decision={
                            "decision_owner": "specialist_agent",
                            "decision_stage": "research_source_selection",
                            "selected_candidate_ids": ids,
                            "candidate_assessments": [{
                                "candidate_id": identity, "disposition": "selected",
                                "rationale": "The exact source and final window were read.",
                            } for identity in ids],
                            "reasoning": "The supplied snapshot is now completely observed.",
                        },
                    )
                    return _response([_message(output)])

                def stream_response(self, *args, **kwargs):
                    raise AssertionError("Only non-streaming resumed research is expected.")

            try:
                return run_business_research_analyst_research_brief_sdk(typed_input, **{
                    **kwargs, "run_config": build_local_run_config(Provider(ResumedSourceModel())),
                })
            except Exception as exc:
                resumed_sdk_errors.append(f"{type(exc).__name__}: {exc}")
                raise

        monkeypatch.setattr(
            workflow_runner, "run_business_research_analyst_research_brief_sdk", resumed_research,
        )

    config = {
        "KNI_BUSINESS_AGENTS_LIVE_SDK": "true",
        "KNI_BUSINESS_AGENTS_LIVE_SEARCH": "true" if saved_source_binding else "false",
        "KNI_BUSINESS_AGENTS_MAX_OPENAI_REQUESTS": "8",
        "KNI_BUSINESS_AGENTS_SLACK_CONTEXT_DIR": str(tmp_path / "resume-contexts"),
        "KNI_BUSINESS_AGENTS_SLACK_RUN_DB_PATH": str(tmp_path / "slack-runs.sqlite"),
    }
    monkeypatch.setattr(bridge, "_env", lambda name, default=None: config.get(name, default))
    monkeypatch.setattr(bridge, "_business_repo", lambda: Path(__file__).resolve().parents[1])
    monkeypatch.setattr(bridge, "_business_database_url", lambda repo: database_url)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", preflight)
    # Dashboard enrollment/health is outside the resume transport contract. Its
    # optional post-run reporting must not probe localhost in this offline gate.
    monkeypatch.setattr(cli, "_record_eval_slack_run_if_requested", lambda *a, **k: None)
    monkeypatch.setattr(workflow_runner, "build_selected_url_source_bundle", bundle)
    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)

    def canonical_child(command, *, cwd, env_overrides):
        commands.append(command)
        assert command[1:4] == ["-m", "keystone_agents.cli", "ask"]
        context_path = command[command.index("--context-file") + 1]
        carried = json.loads(Path(context_path).read_text())
        assert carried["thread_root_request"] == root
        stdout = io.StringIO()
        stderr = ""
        with patch.dict(os.environ, env_overrides), redirect_stdout(stdout):
            try:
                returncode = Context().run(cli.main, command[3:])
            except SystemExit as exc:
                returncode = exc.code if isinstance(exc.code, int) else 1
                stderr = "" if isinstance(exc.code, int) else str(exc.code)
        payloads.append(json.loads(stdout.getvalue()) if stdout.getvalue().strip() else {})
        return subprocess.CompletedProcess(command, returncode, stdout.getvalue(), stderr)

    monkeypatch.setattr(bridge, "_run_command", canonical_child)
    context = bridge.SlackBusinessAgentContext(
        team_id="T111TEST" if pilot_no_auto_memory else "",
        user_id="U111TEST" if pilot_no_auto_memory else "",
        channel_id=CHANNEL, thread_ts=POST_TS, request_ts=REQUEST_TS,
        thread_root_request=root,
        channel_context_capability=(
            json.loads(_context_file(tmp_path).read_text())["channel_context_capability"]
            if with_channel_rag else {}
        ),
    )
    result = bridge.handle_business_agents_request(f"@KNI {wrapped}", slack_context=context)
    assert not automatic_memory_reads
    assert result.handled and len(commands) == 1
    assert {row.id for row in store.list_work_items(status=None)} == before_ids
    saved = store.get_work_item(selected_id)
    assert saved is not None
    slack_record = bridge._slack_run_store_for_context(context).get_by_context(context)
    assert slack_record is not None
    if not valid_binding:
        assert not native_inputs and not preflight_states and not research_sdk_control
        assert not extraction_calls and not ks_backend
        assert saved.model_dump(mode="json") == item.model_dump(mode="json")
        assert payloads[0].get("status") == "blocked" or result.returncode != 0
        assert not slack_record.work_item_id
        return
    if repair_budget_blocked:
        assert result.returncode != 0 and payloads[0]["status"] == "failed"
        assert len(native_inputs) == 3 and len(ks_backend) == 2
        assert len(preflight_states) == 1 and not preflight_results
        assert not extraction_calls and not research_sdk_control
        assert saved.model_dump(mode="json") == item.model_dump(mode="json")
        assert diagnostic_budgets[0]["consumed"] == diagnostic_budgets[0]["limit"] == 3
        assert diagnostic_budgets[0]["exhausted"]
        return
    if semantic_rejected:
        assert result.returncode != 0 or payloads[0].get("status") == "blocked"
        assert not extraction_calls and not research_sdk_control and not ks_backend
        assert saved.model_dump(mode="json") == item.model_dump(mode="json")
        return
    assert result.returncode == 0, payloads
    expected_route_calls = 4 if needs_decision_repair else 3 if with_channel_rag else 1
    assert len(native_inputs) == expected_route_calls and len(preflight_states) == 1
    sdk_event = preflight_results[0].sdk_usage_events[-1]
    assert sdk_event["usage"]["requests"] == expected_route_calls
    if needs_decision_repair:
        ownership = sdk_event["request_cache"]["decision_ownership"]
        assert ownership["repair_attempted"] and ownership["attempt_count"] == 2
        assert ownership["attempts"][0]["validator_outcome"]["reason_code"] == (
            "candidate_assessment_identity_not_in_candidate_set"
        )
        assert ownership["validator_outcome"]["status"] == "accepted"
        assert ownership["repair_evidence"]["provider_calls_during_repair"] == 0
        assert ownership["repair_evidence"]["candidate_universe_source"] == "original_model_input"
    expected_extractions = [] if retained_source_success else [ARTICLE_URL]
    assert len(research_sdk_control) == 1 and extraction_calls == expected_extractions, (
        json.dumps({"status": saved.status, "blockers": [b.model_dump() for b in saved.blockers],
                    "extractions": extraction_calls, "summary": payloads[0].get("human_summary")}),
    )
    assert payloads[0]["work_item"]["id"] == selected_id
    assert not resumed_sdk_errors, resumed_sdk_errors
    assert slack_record.work_item_id == selected_id and slack_record.status == "completed"
    assert saved.status == WorkItemStatus.DONE and saved.artifact_refs, json.dumps({
        "status": saved.status, "blockers": [b.model_dump() for b in saved.blockers],
        "artifact_count": len(saved.artifact_refs),
        "summary": payloads[0].get("human_summary"),
    })
    recovered = next(
        b for b in saved.blockers if b.code == "business_research_analyst_execution_failed"
    )
    assert recovered.resolved and recovered.resolved_at
    if retained_source_success:
        assert all(b.resolved and b.resolved_at for b in saved.blockers)
    assert current in research_sdk_control[0]["model_input"]
    assert selected_id in research_sdk_control[0]["model_input"]
    assert len(ks_backend) == (2 if with_channel_rag else 0)
    if retained_source_success:
        assert len(resumed_window_calls) == 1
        assert resumed_window_calls[0]["source_id"] == "saved-public-source"
        bound_plan = saved.target.metadata["manual_request_plan"]
        assert bound_plan["primary_target"] == ARTICLE_URL and bound_plan["target_type"] == "url"
        assert bound_plan["provider_operations"] == ["read"]
        assert bound_plan["ask_shape"]["strict_filter_mode"] in {"exact", "strict"}
    if artifact_variant:
        assert len(rejected_artifact_reads) == len(observed_local_reads) == 1
        assert observed_local_reads[0]["expected_snapshot_sha256"] == access.snapshot_sha256
        persisted_old = next(
            artifact for artifact in saved.artifact_refs
            if artifact.artifact_id == historical_artifact.artifact_id
        )
        assert persisted_old.model_dump(mode="json") == historical_artifact.model_dump(mode="json")
        accepted = [
            artifact for artifact in saved.artifact_refs
            if artifact.artifact_id != historical_artifact.artifact_id
            and artifact.metadata.get("model_synthesized")
        ]
        assert len(accepted) == 1
        accepted_refs = accepted[0].metadata["source_refs"]
        assert len(accepted_refs) == 1
        assert accepted_refs[0]["source_id"] == "saved-public-source"
        assert accepted_refs[0]["web_source_access"]["snapshot_sha256"] == access.snapshot_sha256
        assert "STALE_ARTIFACT_EVIDENCE_SENTINEL" not in json.dumps(accepted[0].model_dump())
        saved_source = next(
            source for source in saved.sources if source.source_id == "saved-public-source"
        )
        assert saved_source.evidence_excerpt == preview
        assert saved_source.web_source_access.snapshot_sha256 == access.snapshot_sha256
    graph_event = next(
        event for event in store.list_work_item_events(selected_id)
        if event.event_type == "langgraph_orchestration"
    )
    inspection = inspect_graph_execution(
        graph_event.metadata["execution_id"], database_url=database_url, include_state=True,
    )
    assert inspection["work_item_id"] == selected_id and inspection["status"] == "completed"
    assert ARTICLE_URL in json.dumps(inspection["history"])


def test_exact_source_durable_plan_automatically_runs_native_graph_and_saved_checkpoints(
    tmp_path,
    monkeypatch,
    ks_backend,
    research_sdk_control,
):
    """Actual query/read, canonicalization, policy selection, SDK, and SQLite graph state."""
    from keystone_agents.agents import manual_request_planner
    from keystone_agents.orchestration.checkpoints import inspect_graph_execution

    request_text = (
        "@KNI Read article #3 posted yesterday and return an evidence summary for review. "
        "Save this as a local resumable WorkItem. Do not send outreach or write externally."
    )
    path = _context_file(tmp_path)
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path),
        request_text=request_text,
    )
    # Only the upstream planner's typed interpretation is supplied. The exact
    # article identity still must be acquired in the actual routing SDK loop.
    initial_plan = ManualRequestPlan(
        source="llm",
        requested_agent="orchestrator",
        target_agent="business_research_analyst",
        intent="research_brief",
        task_objective="source_research",
        primary_target="article #3 posted yesterday",
        target_type="article_collection",
        expected_artifact_type="source_summary",
        requires_durable_state=True,
        workflow=["business_research_analyst"],
    )
    monkeypatch.setattr(
        manual_request_planner,
        "resolve_manual_request_plan",
        lambda *args, **kwargs: initial_plan,
    )
    model = ContextModel(request_text)
    preflight = orchestrator.run_orchestrator_preflight(
        request_text,
        workflow_state=state,
        run_config=build_local_run_config(Provider(model)),
    )
    assert preflight.execution_allowed, preflight.block_reason
    plan = preflight.manual_request_plan
    assert plan.source == "canonical:orchestrator_selected_source"
    assert plan.primary_target == ARTICLE_URL and plan.requires_durable_state
    assert plan.workflow == ["business_research_analyst"]
    assert len(model.inputs) == 3 and len(ks_backend) == 2
    calls = []

    def extract(url, **kwargs):
        calls.append(url)
        assert url == ARTICLE_URL
        return extraction_tool.WebsiteExtractionResult(
            url=url,
            title="Local model review",
            provider="trafilatura",
            status="success",
            text_or_markdown="A local drafting model is evaluated by a separate local reviewer.",
            claims=["The article describes a local review workflow."],
        )

    def bundle(**kwargs):
        return extraction_tool.build_selected_url_source_bundle(
            **kwargs,
            extractor=extract,
            primary_provider="trafilatura",
            fallback_providers=[],
        )

    monkeypatch.setattr(workflow_runner, "build_selected_url_source_bundle", bundle)
    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    database_url = f"sqlite:///{tmp_path / 'automatic-graph.db'}"
    request = WorkflowRunRequest(
        request_text=request_text,
        save=True,
        database_url=database_url,
        context_file_path=str(path),
        live_sdk=True,
        live_search=False,
        sdk_session_enabled=False,
        manual_request_plan=plan.model_dump(mode="json"),
        orchestrator_preflight=compact_orchestrator_preflight_payload(preflight),
    )
    assert langgraph_workflow.should_use_langgraph_for_work_item(request)
    result = langgraph_workflow.advance_work_item_with_optional_langgraph(request)
    assert result.advanced, [blocker.message for blocker in result.blockers]
    assert calls == [ARTICLE_URL] and len(research_sdk_control) == 1
    assert len(ks_backend) == 2
    store = SQLiteStore(database_url)
    events = store.list_work_item_events(result.work_item.id)
    graph_event = next(event for event in events if event.event_type == "langgraph_orchestration")
    assert graph_event.metadata["runtime"] == "langgraph"
    assert "run_business_research" in graph_event.metadata["node_path"]
    assert graph_event.metadata["execution_id"]
    inspection = inspect_graph_execution(
        graph_event.metadata["execution_id"],
        database_url=database_url,
        include_state=True,
    )
    assert inspection["status"] == "completed"
    assert inspection["work_item_id"] == result.work_item.id
    assert inspection["provider_calls_made"] == 0  # Read-only checkpoint inspection.
    assert len(inspection["history"]) > 1
    snapshots = json.dumps(inspection["history"])
    for retained in (
        request_text,
        ROOT_REQUEST,
        ARTICLE_URL,
        CHANNEL,
        POST_TS,
        REQUEST_TS,
        OLDEST_TS,
        "channel_context_evidence",
        "run_business_research",
    ):
        assert retained in snapshots
    persisted = store.get_work_item(result.work_item.id)
    assert persisted is not None and persisted.target.url == ARTICLE_URL
    assert persisted.target.metadata["channel_context_evidence"][0]["operation"] == "read"
    assert len(ks_backend) == 2 and calls == [ARTICLE_URL]


@pytest.mark.parametrize(
    "request_limit,source_chars,expected_windows,parallel_windows,escaped_content",
    [
        (4, 30639, 0, False, False),
        (5, 30639, 1, False, False),
        (8, 30639, 4, False, False),
        (8, 60000, 4, False, False),
        (5, 30639, 2, True, False),
        (8, 30639, 4, False, True),
    ],
)
def test_native_graph_retains_final_summary_when_source_windows_outlast_request_budget(
    tmp_path,
    monkeypatch,
    ks_backend,
    request_limit,
    source_chars,
    expected_windows,
    parallel_windows,
    escaped_content,
):
    """Actual source-window tools reserve the final typed response under one ledger."""
    from keystone_agents.agents import manual_request_planner
    from keystone_agents.orchestration.checkpoints import inspect_graph_execution
    from keystone_agents.schemas.research import ResearchBrief

    request_text = (
        "@KNI Read article #3 posted yesterday and save a local resumable evidence summary. "
        "Clearly state any unread coverage. Do not send outreach or write externally."
    )
    path = _context_file(tmp_path)
    state = cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(path),
        request_text=request_text,
    )
    initial_plan = ManualRequestPlan(
        source="llm",
        requested_agent="orchestrator",
        target_agent="business_research_analyst",
        intent="research_brief",
        task_objective="source_research",
        primary_target="article #3 posted yesterday",
        target_type="article_collection",
        expected_artifact_type="source_summary",
        requires_durable_state=True,
        workflow=["business_research_analyst"],
    )
    monkeypatch.setattr(
        manual_request_planner,
        "resolve_manual_request_plan",
        lambda *args, **kwargs: initial_plan,
    )
    fragment = (
        '方法 "quoted"\n'
        if escaped_content
        else "Synthetic public methods and measured limitations. "
    )
    body = (
        "A local drafting model is evaluated by a separate local reviewer. " + fragment * 10000
    )[:source_chars]
    extraction_calls, windows, model_inputs, typed_results, research_errors = [], [], [], [], []
    sdk_input, packet, initial_access = None, None, None

    def extract(url, **kwargs):
        extraction_calls.append(url)
        assert url == ARTICLE_URL
        return extraction_tool.WebsiteExtractionResult(
            url=url,
            title="Local model review",
            provider="trafilatura",
            status="success",
            text_or_markdown=body,
            claims=["The article describes a local review workflow."],
        )

    def bundle(**kwargs):
        return extraction_tool.build_selected_url_source_bundle(
            **{
                **kwargs,
                "extractor": extract,
                "primary_provider": "trafilatura",
                "fallback_providers": [],
                "max_text_chars_per_source": 1200,
            }
        )

    actual_window = extraction_tool.read_web_source_window_impl

    def read_window(**kwargs):
        result = actual_window(**kwargs)
        windows.append({"arguments": kwargs, "result": result})
        assert result["status"] == "success"
        assert kwargs["selected_url"] == ARTICLE_URL
        assert kwargs["expected_snapshot_sha256"] == initial_access["snapshot_sha256"]
        assert len(json.dumps(result["text"])) <= 8002
        return result

    class WindowModel(Model):
        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            **kwargs,
        ):
            serialized = json.dumps(input)
            model_inputs.append(serialized)
            assert not handoffs
            assert {tool.name for tool in tools} <= {"read_web_source_window"}
            for identity in (
                request_text,
                ROOT_REQUEST,
                CHANNEL,
                POST_TS,
                REQUEST_TS,
                ARTICLE_URL,
                initial_access["source_id"],
            ):
                assert identity in serialized
            outputs = [
                item
                for item in input
                if isinstance(item, dict) and item.get("type") == "function_call_output"
            ]
            latest = None
            if outputs:
                try:
                    latest = json.loads(outputs[-1]["output"])
                except json.JSONDecodeError:
                    latest = ast.literal_eval(outputs[-1]["output"])
            if latest is not None:
                assert latest["model_request_capacity"]["remaining_model_requests"] >= 1
                assert latest["model_request_capacity"]["reserved_final_response_requests"] == 1
                access = latest["web_source_access"]
            else:
                raw_input = input[0]["content"]
                visible_packet = json.JSONDecoder().raw_decode(
                    raw_input.split("Approved source-backed context:\n", 1)[1]
                )[0]
                capacity = visible_packet.get("model_request_capacity")
                if capacity is None:
                    capacity = json.JSONDecoder().raw_decode(
                        raw_input.split("Model request capacity: ", 1)[1]
                    )[0]
                assert capacity["remaining_model_requests"] == request_limit - 3
                assert capacity["reserved_final_response_requests"] == 1
                access = initial_access
            if tools and not (latest and latest["reached_end"]):
                return _response(
                    [
                        ResponseFunctionToolCall(
                            type="function_call",
                            name="read_web_source_window",
                            call_id=f"window-{len(model_inputs)}-{index}",
                            status="completed",
                            arguments=json.dumps(
                                {
                                    "source_id": access["source_id"],
                                    "selected_url": access["selected_url"],
                                    "expected_snapshot_sha256": access["snapshot_sha256"],
                                    "start_char": access["next_start_char"] + index * 8000,
                                    "max_chars": 8000,
                                }
                            ),
                        )
                        for index in range(2 if parallel_windows and not outputs else 1)
                    ]
                )
            # Selection follows the actual visible toolbox/EOF. No hidden ledger
            # inspection or fabricated final result bypasses the native SDK.
            partial = latest is None or not latest["reached_end"]
            sources = packet["source_catalog"]
            ids = [source["source_id"] for source in sources]
            output = ResearchBrief(
                target_name=sdk_input.target_name,
                target_type=sdk_input.target_type,
                summary="The read evidence describes a local drafting model with separate review.",
                facts=[
                    {"text": "The read source describes separate local review.", "source_ids": ids}
                ],
                source_ids_used=ids,
                sources=sources,
                limitations=[
                    "Later extracted text remains unread; this is a bounded partial summary."
                    if partial
                    else "The extracted snapshot was read; linked references remain unread.",
                    "Scripted native SDK wiring control, not live model-quality evidence.",
                ],
                decision={
                    "decision_owner": "specialist_agent",
                    "decision_stage": "research_source_selection",
                    "selected_candidate_ids": ids,
                    "needs_more_context": False,
                    "reasoning": "The exact selected snapshot supports these bounded facts.",
                    "candidate_assessments": [
                        {
                            "candidate_id": identity,
                            "disposition": "selected",
                            "rationale": "Selected from the exact read snapshot.",
                        }
                        for identity in ids
                    ],
                },
            )
            return _response([_message(output)])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only bounded non-streaming source reads are expected.")

    actual_research = workflow_runner.run_business_research_analyst_research_brief_sdk

    def research(typed_input, **kwargs):
        nonlocal sdk_input, packet, initial_access
        sdk_input = typed_input
        packet = json.loads(typed_input.source_context)
        accesses = []

        def visit(value):
            if isinstance(value, dict):
                if isinstance(value.get("web_source_access"), dict):
                    accesses.append(value["web_source_access"])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(packet)
        initial_access = accesses[0]
        assert initial_access["total_chars"] == source_chars
        assert initial_access["end_char"] == 1200 and not initial_access["content_complete"]
        try:
            result = actual_research(
                typed_input,
                **{
                    **kwargs,
                    "run_config": build_local_run_config(Provider(WindowModel())),
                },
            )
        except Exception as exc:
            research_errors.append(repr(exc))
            raise
        typed_results.append(result)
        return result

    monkeypatch.setattr(workflow_runner, "build_selected_url_source_bundle", bundle)
    monkeypatch.setattr(extraction_tool, "read_web_source_window_impl", read_window)
    monkeypatch.setattr(
        workflow_runner,
        "run_business_research_analyst_research_brief_sdk",
        research,
    )
    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    database_url = f"sqlite:///{tmp_path / 'window-budget-graph.db'}"
    with activate_model_request_budget(request_limit) as ledger:
        route_model = ContextModel(request_text)
        preflight = orchestrator.run_orchestrator_preflight(
            request_text,
            workflow_state=state,
            run_config=build_local_run_config(Provider(route_model)),
        )
        assert preflight.execution_allowed, preflight.block_reason
        assert ledger.snapshot()["consumed"] == 3
        request = WorkflowRunRequest(
            request_text=request_text,
            save=True,
            database_url=database_url,
            context_file_path=str(path),
            live_sdk=True,
            live_search=False,
            sdk_session_enabled=False,
            manual_request_plan=preflight.manual_request_plan.model_dump(mode="json"),
            orchestrator_preflight=compact_orchestrator_preflight_payload(preflight),
        )
        result = langgraph_workflow.advance_work_item_with_optional_langgraph(request)
        budget = ledger.snapshot()
    assert result.advanced, [research_errors, [blocker.message for blocker in result.blockers]]
    assert extraction_calls == [ARTICLE_URL] and len(ks_backend) == 2
    assert len(windows) == expected_windows
    assert len(model_inputs) == (2 if parallel_windows else expected_windows + 1)
    assert budget["consumed"] == 3 + len(model_inputs) <= request_limit
    assert not budget["exhausted"]
    assert len(typed_results) == 1 and result.artifact_refs
    cursor = 1200
    for window in sorted(windows, key=lambda item: item["arguments"]["start_char"]):
        assert window["arguments"]["start_char"] == cursor
        access = window["result"]["web_source_access"]
        cursor = access["end_char"]
        assert 0 < cursor - access["start_char"] <= 8000
    if cursor < source_chars:
        assert any(
            "Later extracted text remains unread" in text
            for text in typed_results[0].output.limitations
        )
    store = SQLiteStore(database_url)
    events = store.list_work_item_events(result.work_item.id)
    graph = next(event for event in events if event.event_type == "langgraph_orchestration")
    assert (
        graph.metadata["runtime"] == "langgraph"
        and "run_business_research" in graph.metadata["node_path"]
    )
    inspection = inspect_graph_execution(
        graph.metadata["execution_id"],
        database_url=database_url,
        include_state=True,
    )
    assert inspection["status"] == "completed" and len(inspection["history"]) > 1
    persisted = store.get_work_item(result.work_item.id)
    assert persisted is not None and persisted.target.url == ARTICLE_URL
    assert persisted.artifact_refs
    assert ARTICLE_URL in json.dumps(inspection["history"])
