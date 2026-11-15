"""Native Chief tool loops bind optional memory to trusted child context only."""

import json

import pytest
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall
from test_lazy_slack_context_backend_matrix import Provider, _message, _response
from test_lazy_slack_context_backend_matrix import (
    strict_offline_boundaries as strict_offline_boundaries,
)

from keystone_agents import langgraph_workflow, workflow_runner
from keystone_agents.agents import chief_of_staff as chief
from keystone_agents.orchestrator.preflight_context import SPECIALIST_EXECUTION_CONTEXT_ENV
from keystone_agents.runtime.continuity_policy import (
    activate_continuity_policy,
    resolve_slack_compact_continuity_policy,
)
from keystone_agents.runtime.operator_continuity import operator_capability_from_state
from keystone_agents.runtime.scoped_memory import (
    memory_capture_fingerprint,
    project_scoped_memory_facts,
)
from keystone_agents.runtime.tool_execution import ToolExecutionContractError
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.memory import MemoryItem
from keystone_agents.schemas.memory_scope import MemoryCaptureProvenance, SlackMemoryScope
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItemStatus
from keystone_agents.sdk import (
    agent_with_stable_prompt_cache_key,
    build_local_run_config,
    prompt_cache_key_audit_metadata,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


@pytest.fixture
def host(tmp_path, monkeypatch):
    database = f"sqlite:///{tmp_path / 'chief-memory.db'}"
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("KEYSTONE_SDK_SESSIONS", "false")
    monkeypatch.setenv("KEYSTONE_SDK_SESSION_DB", str(tmp_path / "sessions.sqlite"))
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS", "C111TEST")
    context = {
        "slack_scope": {
            "team_id": "T111TEST", "channel_id": "C111TEST", "user_id": "U111TEST",
            "thread_ts": "1794675600.000001", "request_ts": "1794675601.000001",
        },
        "channel_context_capability": {
            "schema_version": "keystone.slack.channel_context.v1", "channel_id": "C111TEST",
            "request_ts": "1794675601.000001", "oldest_ts": "1794070801.000001",
        },
    }
    monkeypatch.setenv(SPECIALIST_EXECUTION_CONTEXT_ENV, json.dumps(context))
    return database, context


def _plan(capture=False):
    return ManualRequestPlan(
        source="canonical:synthetic_chief", target_agent="chief_of_staff",
        intent="reference_capture" if capture else "route_request",
        task_objective="reference_capture" if capture else "route_or_continue",
        expected_artifact_type="reference_note" if capture else "none",
    )


def _output(summary, *, workflow="memory-review"):
    return ChiefOfStaffResult(
        mode="llm", summary=summary,
        recommended_route={"workflow_type": workflow, "target_channel": "current-thread"},
        decision={
            "decision_owner": "chief_of_staff", "decision_stage": "chief_delegation_selection",
            "selected_candidate_ids": [f"workflow:{workflow}"],
            "candidate_assessments": [{
                "candidate_id": f"workflow:{workflow}", "disposition": "selected",
                "rationale": "The request concerns bounded internal operator guidance.",
            }],
            "reasoning": "The selected internal workflow preserves scope and permissions.",
        },
    )


class ChiefMemoryModel(Model):
    def __init__(self, request, *, capture=False, call_tool=True, capture_arguments=None,
                 extra_tools=()):
        self.request, self.capture, self.call_tool = request, capture, call_tool
        self.capture_arguments = capture_arguments
        self.extra_tools = set(extra_tools)
        self.inputs, self.outputs = [], []

    async def get_response(self, system_instructions, input, model_settings, tools,
                           output_schema, handoffs, tracing, **kwargs):
        self.inputs.append(json.dumps(input))
        assert self.request in self.inputs[-1]
        assert "<!-- operator_context.md -->" not in system_instructions
        names = {tool.name for tool in tools}
        assert "retrieve_chief_of_staff_memory" not in names and not handoffs
        assert names == (({"query_operator_context", "capture_operator_memory"}
                          if self.capture else {"query_operator_context"}) | self.extra_tools)
        outputs = [value for value in input if isinstance(value, dict)
                   and value.get("type") == "function_call_output"]
        if not outputs and self.call_tool:
            name = "capture_operator_memory" if self.capture else "query_operator_context"
            arguments = {
                "kind": "preference", "title": "Bounded experiments",
                "summary": "Prefer bounded experiments with a reviewable artifact.",
            } if self.capture else {"query": "bounded experiment", "include_profile": False}
            if self.capture_arguments is not None:
                assert "operator_memory_capture_sources" in self.inputs[-1]
                assert "request-url:1" in self.inputs[-1]
                assert "https://source.test/approved-reference" in self.inputs[-1]
                arguments = self.capture_arguments
            return _response([ResponseFunctionToolCall(
                type="function_call", name=name, call_id="chief-memory", status="completed",
                arguments=json.dumps(arguments),
            )])
        if outputs:
            self.outputs.append(json.loads(outputs[-1]["output"]))
        return _response([_message(_output(
            "Saved the scoped preference." if self.capture else "Try one bounded experiment.",
        ))])

    def stream_response(self, *args, **kwargs):
        raise AssertionError("Only non-streaming native Chief turns are expected.")


def test_default_host_env_capture_runs_real_sdk_tool_and_verified_local_save(host):
    database, _ = host
    request = "Remember this preference: prefer bounded experiments with a reviewable artifact."
    model = ChiefMemoryModel(request, capture=True)
    result = chief.run_chief_of_staff_sdk(
        {"request": request}, manual_request_plan=_plan(True), attach_tools=False,
        run_config=build_local_run_config(Provider(model)),
    )
    assert len(model.inputs) == 2 and model.outputs[0]["verified"]
    assert result.request_cache["operator_context_evidence"]["capture_count"] == 1
    assert result.request_cache["operator_context_usage"]["capture_saved_count"] == 1
    records = SQLiteStore(database).list_memory_items()
    assert len(records) == 1 and records[0].summary == (
        "Prefer bounded experiments with a reviewable artifact."
    )


@pytest.mark.parametrize("case", ["authorized", "read-only-plan", "no-authorization", "negated"])
def test_capture_tier_admission_requires_current_host_and_typed_capture_authority(host, case):
    from keystone_agents.agent_tool_policy import INTERNAL_WRITE_TOOL_NAMES
    from keystone_agents.capabilities.tool_scope import tool_scope_receipt_for_agent
    from keystone_agents.quality_budget import AgentQualityBudget
    from keystone_agents.runtime.request_budget import activate_model_request_budget

    database, context = host
    request = (
        "Do not remember this preference; just review a bounded experiment."
        if case == "negated" else "Remember this preference: favor bounded experiments."
    )
    plan = _plan(case != "read-only-plan").model_copy(update={
        "source": "heuristic", "requested_agent": "chief_of_staff",
    })
    budget = AgentQualityBudget(
        agent_name="chief_of_staff", mode="fast", max_turns=1,
        max_tool_calls=0, tool_tier="core_read",
    )
    capability = operator_capability_from_state(
        context, raw_request=request, database_url=database,
        capture_authorized=case != "no-authorization",
    )
    agent = chief.build_chief_of_staff_agent(
        request_text=request, manual_request_plan=plan, quality_budget=budget,
        include_specialist_tools=True, operator_context_capability=capability,
        tool_scope_mode="request_scoped",
    )
    names = {tool.name for tool in agent.tools}
    receipt = tool_scope_receipt_for_agent(agent)
    assert receipt["effective_mode"] == "request_scoped"
    if case == "authorized":
        assert names == {"query_operator_context", "capture_operator_memory"}
        assert receipt["max_tool_tier"] == "internal_write"
        assert names & INTERNAL_WRITE_TOOL_NAMES == {"capture_operator_memory"}
        model = ChiefMemoryModel(request, capture=True)
        with activate_model_request_budget(3) as ledger:
            result = chief.run_chief_of_staff_sdk(
                {"request": request}, manual_request_plan=plan, quality_budget=budget,
                include_specialist_tools=True, operator_context_capability=capability,
                run_config=build_local_run_config(Provider(model)),
            )
            assert ledger.limit == 3 and ledger.consumed == 2
        assert result.request_cache["operator_context_evidence"]["capture_count"] == 1
        assert len(SQLiteStore(database).list_memory_items()) == 1
    else:
        assert "capture_operator_memory" not in names
        assert not names & INTERNAL_WRITE_TOOL_NAMES
        assert receipt["max_tool_tier"] == "core_read"
        model = ChiefMemoryModel(
            request, call_tool=False, extra_tools=names - {"query_operator_context"},
        )

        def run():
            return chief.run_chief_of_staff_sdk(
                {"request": request}, manual_request_plan=plan, quality_budget=budget,
                include_specialist_tools=True, operator_context_capability=capability,
                run_config=build_local_run_config(Provider(model)),
            )

        if case == "no-authorization":
            with pytest.raises(ValueError, match="current host-bound capability"):
                run()
            assert model.inputs == []
        else:
            assert run().request_cache["operator_context_evidence"]["capture_count"] == 0
            assert len(model.inputs) == 1
        assert not SQLiteStore(database).list_memory_items()


@pytest.mark.parametrize("case", ["auto", "explicit-request-scoped", "different-owner"])
def test_unnamed_host_capture_uses_only_its_compiled_auto_surface(host, case):
    from keystone_agents.capabilities.tool_scope import tool_scope_receipt_for_agent
    from keystone_agents.quality_budget import AgentQualityBudget
    from keystone_agents.runtime.request_budget import activate_model_request_budget

    database, context = host
    request = "Remember this preference: favor bounded experiments."
    plan = _plan(True).model_copy(update={
        "source": "heuristic", "requested_agent": None,
        "target_agent": "business_research_analyst" if case == "different-owner"
        else "chief_of_staff",
    })
    budget = AgentQualityBudget(
        agent_name="chief_of_staff", mode="fast", max_turns=1,
        max_tool_calls=0, tool_tier="core_read",
    )
    capability = operator_capability_from_state(
        context, raw_request=request, database_url=database, capture_authorized=True,
    )
    model = ChiefMemoryModel(request, capture=True)
    if case == "different-owner":
        with pytest.raises(ValueError, match="current Chief of Staff ownership"):
            chief.run_chief_of_staff_sdk(
                {"request": request}, manual_request_plan=plan, quality_budget=budget,
                operator_context_capability=capability,
                run_config=build_local_run_config(Provider(model)),
            )
        assert model.inputs == []
    else:
        agent = chief.build_chief_of_staff_agent(
            request_text=request, manual_request_plan=plan, quality_budget=budget,
            include_specialist_tools=True, operator_context_capability=capability,
            tool_scope_mode="request_scoped" if case == "explicit-request-scoped" else "auto",
        )
        names = {tool.name for tool in agent.tools}
        receipt = tool_scope_receipt_for_agent(agent)
        if case == "explicit-request-scoped":
            assert not names
            assert receipt["effective_mode"] == "request_scoped"
            assert receipt["omitted_tool_count"] == 2
        else:
            assert names == {"query_operator_context", "capture_operator_memory"}
            assert receipt["effective_mode"] == "full"
            with activate_model_request_budget(3) as ledger:
                result = chief.run_chief_of_staff_sdk(
                    {"request": request}, manual_request_plan=plan, quality_budget=budget,
                    include_specialist_tools=True, operator_context_capability=capability,
                    run_config=build_local_run_config(Provider(model)),
                )
                assert ledger.limit == 3 and ledger.consumed == 2
            assert result.request_cache["operator_context_evidence"]["capture_count"] == 1
            assert model.outputs[0]["verified"] is True
    assert len(SQLiteStore(database).list_memory_items()) == int(case == "auto")


def test_optional_query_is_not_automatic_and_reads_only_scoped_facts(host):
    database, context = host
    scope = SlackMemoryScope(
        **{k: context["slack_scope"][k] for k in ("team_id", "channel_id", "user_id")},
        project_key="keystone-business-agents",
    )
    capture = MemoryCaptureProvenance(
        thread_ts="1794675500.000001", request_ts="1794675501.000001",
        request_fingerprint=memory_capture_fingerprint(
            scope, thread_ts="1794675500.000001", request_ts="1794675501.000001",
            raw_request="Save the bounded experiment preference.",
        ),
    )
    SQLiteStore(database).save_memory_item(MemoryItem(
        memory_type="operator_strategy", title="Bounded experiment",
        summary="Use one bounded experiment with a reviewable artifact.",
        source_ids=["synthetic:approved-preference"], approval_state="approved_for_research",
    ), trusted_scope=scope, capture_provenance=capture)
    request = "Suggest a useful next step based on the saved bounded experiment preference."
    for should_query in (False, True):
        model = ChiefMemoryModel(request, call_tool=should_query)
        result = chief.run_chief_of_staff_sdk(
            {"request": request}, manual_request_plan=_plan(), attach_tools=False,
            run_config=build_local_run_config(Provider(model)),
        )
        assert result.request_cache["operator_context_usage"]["query_calls"] == int(should_query)
        if should_query:
            assert model.outputs[0]["facts"][0]["source_ids"] == ["synthetic:approved-preference"]
            assert "reviewable artifact" in model.inputs[-1]


def test_capture_claim_without_actual_tool_call_is_blocked(host):
    database, _ = host
    request = "Remember this preference: prefer bounded experiments with a reviewable artifact."
    model = ChiefMemoryModel(request, capture=True, call_tool=False)
    with pytest.raises(ToolExecutionContractError):
        chief.run_chief_of_staff_sdk(
            {"request": request}, manual_request_plan=_plan(True), attach_tools=False,
            run_config=build_local_run_config(Provider(model)),
        )
    assert not SQLiteStore(database).list_memory_items()


@pytest.mark.parametrize(
    "case", ["wrong-plan", "authorized-cap-without-save", "negated", "nonpilot"],
)
def test_model_capture_selection_cannot_complete_without_verified_save(host, monkeypatch, case):
    from keystone_agents.run import sdk_run_failure_metadata

    database, context = host
    request = (
        "Do not remember this preference; just review it."
        if case == "negated" else "Remember this preference: favor bounded experiments."
    )
    plan = _plan().model_copy(update={
        "intent": "slack_operations", "task_objective": "slack_operations",
        "expected_artifact_type": "slack_ops_summary",
    })
    if case == "nonpilot":
        monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "false")
    capability = (
        operator_capability_from_state(
            context, raw_request=request, database_url=database, capture_authorized=True,
        ) if case == "authorized-cap-without-save" else None
    )

    class CaptureDecisionModel(Model):
        calls = 0

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            self.calls += 1
            assert request in json.dumps(input)
            assert "capture_operator_memory" not in {tool.name for tool in tools}
            assert not handoffs
            return _response([_message(_output(
                "I could not save the preference because no capture tool was available.",
                workflow="reference-capture",
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only native non-streaming turns are expected.")

    model = CaptureDecisionModel()

    def run():
        return chief.run_chief_of_staff_sdk(
            {"request": request}, manual_request_plan=plan, attach_tools=False,
            operator_context_capability=capability,
            run_config=build_local_run_config(Provider(model)),
        )

    if case in {"wrong-plan", "authorized-cap-without-save"}:
        with pytest.raises(ToolExecutionContractError) as caught:
            run()
        outcome = caught.value.outcome
        assert outcome.stage == "chief_scoped_memory_capture" and not outcome.satisfied
        assert "verified_scoped_memory_capture" in outcome.missing_groups
        assert ("current_host_capture_authorization" in outcome.missing_groups) == (
            case == "wrong-plan"
        )
        failure = sdk_run_failure_metadata(caught.value)
        assert failure["failure_kind"] == "required_tool_execution_missing"
        assert failure["usage"]["requests"] == 1
        assert failure["tool_execution_postcondition"] == outcome.receipt()
    else:
        assert run().output.recommended_route.workflow_type == "reference-capture"
    assert model.calls == 1
    assert not SQLiteStore(database).list_memory_items()


def test_capture_missing_readback_cannot_claim_saved(host, monkeypatch):
    database, context = host
    request = "Remember this preference: prefer bounded experiments with a reviewable artifact."
    capability = operator_capability_from_state(
        context, raw_request=request, database_url=database, capture_authorized=True,
    )
    monkeypatch.setattr(capability._storage.store, "save_memory_item", lambda *a, **k: 999)
    model = ChiefMemoryModel(request, capture=True)
    with pytest.raises((ToolExecutionContractError, ValueError)):
        chief.run_chief_of_staff_sdk(
            {"request": request}, manual_request_plan=_plan(True), attach_tools=False,
            operator_context_capability=capability,
            run_config=build_local_run_config(Provider(model)),
        )
    assert not capability.evidence["capture_receipts"]
    assert capability.evidence["capture_count"] == 0
    assert not SQLiteStore(database).list_memory_items()


@pytest.mark.parametrize("final_workflow", ["memory-review", "reference-capture"])
def test_native_capture_receipt_survives_structured_retry_without_another_save(
    host, monkeypatch, final_workflow,
):
    """Keep production retry policy, injecting only the native SDK model transport."""
    from keystone_agents import sdk
    from keystone_agents.runtime.request_budget import activate_model_request_budget

    database, context = host
    request = "Remember this preference: favor bounded experiments."
    capability = operator_capability_from_state(
        context, raw_request=request, database_url=database, capture_authorized=True,
    )

    def reply(outputs):
        response = _response(outputs)
        response.usage.total_tokens = 50
        response.raw_usage = {
            "input_tokens": 30, "output_tokens": 20, "total_tokens": 50,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        return response

    class SchemaRetryModel(Model):
        inputs = []
        saved_receipts = []

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            self.inputs.append(json.dumps(input))
            turn = len(self.inputs)
            assert request in self.inputs[-1] and not handoffs
            names = {tool.name for tool in tools}
            assert names <= {"capture_operator_memory", "query_operator_context"}
            if turn == 1:
                assert "capture_operator_memory" in names
                return reply([ResponseFunctionToolCall(
                    type="function_call", name="capture_operator_memory", call_id="save-once",
                    status="completed", arguments=json.dumps({
                        "kind": "preference", "title": "Bounded experiments",
                        "summary": "Favor one bounded experiment with a reviewable artifact.",
                    }),
                )])
            if turn == 2:
                outputs = [item for item in input if isinstance(item, dict)
                           and item.get("type") == "function_call_output"]
                receipt = json.loads(outputs[-1]["output"])
                assert receipt["status"] == "saved" and receipt["verified"] is True
                self.saved_receipts.append(receipt)
                invalid = _message(_output("Saved the scoped preference."))
                invalid.content[0].text = '{"summary":'
                return reply([invalid])
            if turn == 3:
                assert "query_operator_context" in names
                assert "capture_operator_memory" not in names
                return reply([ResponseFunctionToolCall(
                    type="function_call", name="query_operator_context", call_id="query-after-save",
                    status="completed", arguments=json.dumps({
                        "query": "bounded experiments", "include_profile": False,
                    }),
                )])
            assert turn <= 5
            return reply([_message(_output(
                "Saved the scoped preference after verifying the local record.",
                workflow=final_workflow,
            ))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only native non-streaming turns are expected.")

    model = SchemaRetryModel()
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "synthetic-local-sdk-key")
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "0")
    monkeypatch.setenv("KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES", "1")
    monkeypatch.setattr(sdk, "build_live_run_config", lambda *a, **k: (
        build_local_run_config(Provider(model))
    ))
    with activate_model_request_budget(8) as ledger:
        result = chief.run_chief_of_staff_sdk(
            {"request": request}, manual_request_plan=_plan(True), live=True,
            operator_context_capability=capability,
        )
        assert ledger.consumed == 4 and ledger.limit == 8
    assert len(model.inputs) == 4 and len(model.saved_receipts) == 1
    assert capability.usage["capture_calls"] == 1 and capability.evidence["capture_count"] == 1
    assert len(SQLiteStore(database).list_memory_items()) == 1
    assert result.request_cache["structured_output_retries"] == 1
    assert result.request_cache["tool_execution_postcondition"]["satisfied"] is True
    assert result.usage["requests"] == 4 and result.usage["complete"] is True
    assert result.cost["complete"] is True and result.budget_guard["exceeded"] is False
    assert result.budget_guard["enforceable"] is True


def test_pilot_input_uses_trusted_facts_once_and_ignores_typed_scope(host, monkeypatch):
    _, context = host
    fact = {"memory_id": 17, "title": "ADMITTED_FACT_SENTINEL", "summary": "Approved guidance."}
    context["approved_operator_context"] = {"facts": [fact]}
    monkeypatch.setenv(SPECIALIST_EXECUTION_CONTEXT_ENV, json.dumps(context))
    supplied = {
        "request": "Suggest a useful internal next step.",
        "approved_operator_context": {"facts": [{"title": "FORGED_TYPED_FACT"}]},
        "execution_context": context,
        "orchestrator_preflight": {"workflow_state": {
            "operator_context_evidence": {"model_context": {"facts": [fact]}},
            "current_work_item": {"id": "wi_duplicate_context"},
        }},
    }
    prepared = chief._chief_of_staff_sdk_input_for_request(
        supplied, raw_request_text=supplied["request"], manual_request_plan=_plan(),
    )
    serialized = json.dumps(prepared)
    assert serialized.count("ADMITTED_FACT_SENTINEL") == 1
    assert "FORGED_TYPED_FACT" not in serialized and "wi_duplicate_context" not in serialized


def test_pilot_deterministic_fallback_never_captures_or_auto_retrieves(host, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy memory fallback cannot run in the compact pilot.")

    monkeypatch.setattr(chief, "_plan_chief_memory_capture", forbidden)
    monkeypatch.setattr(chief, "_plan_reference_capture", forbidden)
    monkeypatch.setattr(chief, "build_chief_of_staff_memory_context", forbidden)
    result = chief.plan_chief_of_staff_request("Remember this preference for future work.")
    assert "Saved" not in result.summary
    chief.plan_chief_of_staff_request("Help prioritize the next internal operating step.")


def test_typed_execution_context_alone_cannot_create_capability(host, monkeypatch):
    database, context = host
    monkeypatch.delenv(SPECIALIST_EXECUTION_CONTEXT_ENV)
    request = "Remember this preference for future work."
    assert chief._chief_operator_capability(
        None, request_text=request, manual_request_plan=_plan(True),
    ) is None
    assert not SQLiteStore(database).list_memory_items()
    assert context["slack_scope"]["user_id"] == "U111TEST"


def test_current_negative_capture_request_overrides_capture_capability(host):
    database, context = host
    request = "Do not remember this preference; just review a bounded experiment."
    capability = operator_capability_from_state(
        context, raw_request=request, database_url=database, capture_authorized=True,
    )
    model = ChiefMemoryModel(request, call_tool=False)
    chief.run_chief_of_staff_sdk(
        {"request": request}, manual_request_plan=_plan(True), attach_tools=False,
        operator_context_capability=capability,
        run_config=build_local_run_config(Provider(model)),
    )
    assert capability.evidence["capture_count"] == 0
    assert not SQLiteStore(database).list_memory_items()


def test_inherited_facts_and_later_query_share_four_fact_budget_without_duplicate_text(
    host, monkeypatch,
):
    database, context = host
    scope = SlackMemoryScope(
        **{k: context["slack_scope"][k] for k in ("team_id", "channel_id", "user_id")},
        project_key="keystone-business-agents",
    )
    store = SQLiteStore(database)
    for turn in (1, 2, 3):
        store.save_memory_item(MemoryItem(
            memory_type="operator_strategy", title=f"INHERITED_EXPERIMENT_{turn}",
            summary="A bounded experiment preference.", source_ids=[f"source:experiment:{turn}"],
            approval_state="approved_for_research",
        ), trusted_scope=scope, capture_provenance=MemoryCaptureProvenance(
            thread_ts="1794675500.000001", request_ts=f"179467550{turn}.000001",
            request_fingerprint=memory_capture_fingerprint(
                scope, thread_ts="1794675500.000001", request_ts=f"179467550{turn}.000001",
                raw_request="Save the bounded experiment preference.",
            ),
        ))
    records = sorted(store.retrieve_memory(admission_scope=scope), key=lambda record: record.id)
    initial = project_scoped_memory_facts(records[:2]).model_context
    context["approved_operator_context"] = initial
    monkeypatch.setenv(SPECIALIST_EXECUTION_CONTEXT_ENV, json.dumps(context))
    request = "Suggest a next step based on the bounded experiment preferences."
    model = ChiefMemoryModel(request)
    result = chief.run_chief_of_staff_sdk(
        {"request": request}, manual_request_plan=_plan(), attach_tools=False,
        run_config=build_local_run_config(Provider(model)),
    )
    assert [fact["memory_id"] for fact in model.outputs[0]["facts"]] == [records[2].id]
    assert model.outputs[0]["already_admitted_ids"]
    assert set(model.outputs[0]["already_admitted_ids"]) <= {record.id for record in records[:2]}
    for text in model.inputs:
        assert text.count("INHERITED_EXPERIMENT_1") == 1
        assert text.count("INHERITED_EXPERIMENT_2") == 1
    assert "INHERITED_EXPERIMENT_3" not in model.inputs[0]
    assert model.inputs[1].count("INHERITED_EXPERIMENT_3") == 1
    evidence = result.request_cache["operator_context_evidence"]
    assert len(evidence["model_context"]["facts"]) == 3
    assert evidence["serialized_bytes"] <= 4096


def test_pilot_static_profile_cache_key_excludes_dynamic_facts_and_request_identity(
    host, monkeypatch,
):
    _, context = host
    monkeypatch.setenv("KEYSTONE_SDK_PROMPT_CACHE_SCOPE", "synthetic-chief-cache")
    request = "Suggest a practical internal next step."
    keys = []
    for turn in (1, 2):
        fact = {
            "memory_id": turn, "memory_type": "operator_strategy", "title": f"DYNAMIC_{turn}",
            "summary": "Bounded approved guidance.", "source_ids": [f"source:approved:{turn}"],
            "source_urls": [], "created_at": "2026-11-14T00:00:00Z",
        }
        context["approved_operator_context"] = {"facts": [fact]}
        context["slack_scope"]["request_ts"] = f"179467560{turn}.000001"
        context["channel_context_capability"]["request_ts"] = f"179467560{turn}.000001"
        context["channel_context_capability"]["oldest_ts"] = f"179407080{turn}.000001"
        monkeypatch.setenv(SPECIALIST_EXECUTION_CONTEXT_ENV, json.dumps(context))
        agent = chief.build_chief_of_staff_agent(
            model="synthetic-cache-model", request_text=request,
            manual_request_plan=_plan(), attach_tools=False,
        )
        assert "<!-- operator_context.md -->" not in agent.instructions
        assert "DYNAMIC_" not in agent.instructions
        agent = agent_with_stable_prompt_cache_key(
            agent, provider="openai", model_name="synthetic-cache-model",
        )
        audit = prompt_cache_key_audit_metadata(agent)
        assert audit["prompt_cache_key_present"]
        keys.append(audit["prompt_cache_key_hash"])
    assert keys[0] == keys[1]


def test_capability_from_another_current_request_is_rejected_before_model(host):
    database, context = host
    capability = operator_capability_from_state(
        context, raw_request="Remember a different preference.", database_url=database,
        capture_authorized=True,
    )
    with pytest.raises(ValueError, match="does not match"):
        chief.run_chief_of_staff_sdk(
            {"request": "Remember the current preference."}, manual_request_plan=_plan(True),
            operator_context_capability=capability,
        )
    assert not SQLiteStore(database).list_memory_items()


def test_native_reference_capture_receives_exact_host_source_ids_and_urls(host):
    database, _ = host
    request = "Remember this reference for future work: https://source.test/approved-reference"
    from keystone_agents.runtime.operator_continuity import capture_request_authorized

    assert capture_request_authorized(request, chief._chief_request_plan(request, _plan(True)))
    model = ChiefMemoryModel(request, capture=True, capture_arguments={
        "kind": "reference", "title": "Approved bounded reference",
        "summary": "Use this operator-supplied reference for future bounded reviews.",
        "source_ref_ids": ["request-url:1"],
    })
    result = chief.run_chief_of_staff_sdk(
        {"request": request, "operator_memory_capture_sources": [{
            "id": "forged-source", "url": "https://example.org/forged-reference",
        }]}, manual_request_plan=_plan(True), attach_tools=False,
        run_config=build_local_run_config(Provider(model)),
    )
    assert result.request_cache["operator_context_evidence"]["capture_count"] == 1
    assert model.outputs[0]["verified"] is True
    assert all("forged-source" not in value for value in model.inputs)
    [saved] = SQLiteStore(database).list_memory_items()
    assert saved.memory_type == "operator_reference"
    assert saved.source_ids == ["request-url:1"]
    assert saved.content["source_urls"] == ["https://source.test/approved-reference"]


def test_query_only_input_does_not_advertise_capture_sources(host):
    database, context = host
    request = "Review this reference: https://source.test/approved-reference"
    capability = operator_capability_from_state(
        context, raw_request=request, database_url=database, capture_authorized=False,
    )
    prepared = chief._chief_pilot_input_context({
        "request": request, "operator_memory_capture_sources": [{"id": "forged-source"}],
    }, capability)
    assert "operator_memory_capture_sources" not in prepared
    assert "allowed_capture_sources" not in capability.descriptor


@pytest.mark.parametrize("capture_request", [True, False], ids=["verified-save", "future-plan"])
def test_native_durable_chief_graph_preserves_capture_authority_without_child_env(
    host, tmp_path, monkeypatch, capture_request,
):
    """Real graph and Chief SDK; only upstream route advice and model transport are fixtures."""
    pytest.importorskip("langgraph.graph", reason="This proof requires the native graph.")
    pytest.importorskip(
        "langgraph.checkpoint.sqlite",
        reason="Native SQLite checkpoints require the orchestration extra.",
    )
    from keystone_agents import response_synthesis
    from keystone_agents.orchestration.checkpoints import inspect_graph_execution
    from keystone_agents.run import run_typed_sdk_agent

    database, context = host
    request = (
        "Remember this preference: prefer bounded experiments with a reviewable artifact."
        if capture_request else "Review a future bounded internal work plan."
    )
    chief_summary = (
        "Saved the scoped preference." if capture_request else "Try one bounded experiment."
    )
    raw_context = {
        **context["slack_scope"], "schema": "keystone.slack.history_context.v1",
        "channel_context_capability": context["channel_context_capability"],
        "thread_root_request": request, "request_text": request,
        "read_context": "", "thread_messages": [],
    }
    path = tmp_path / "current-graph-context.json"
    path.write_text(json.dumps(raw_context))
    monkeypatch.delenv(SPECIALIST_EXECUTION_CONTEXT_ENV)
    monkeypatch.delenv("KEYSTONE_WORKITEM_LANGGRAPH", raising=False)
    monkeypatch.delenv("KNI_BUSINESS_AGENTS_LANGGRAPH", raising=False)
    plan = _plan(capture_request).model_copy(update={
        "requires_durable_state": True, "workflow": ["chief_of_staff"],
    })
    class FinalResponseModel(Model):
        inputs = []

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            assert not tools and not handoffs
            self.inputs.append(json.dumps(input))
            assert request in self.inputs[-1] and chief_summary in self.inputs[-1]
            answer = (
                "Saved your preference to favor bounded experiments with a reviewable artifact."
                if capture_request else "Review one bounded internal experiment before acting."
            )
            return _response([_message(
                response_synthesis.UserFacingResponseSynthesis(answer=answer),
            )])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Only non-streaming final synthesis is expected.")

    model = ChiefMemoryModel(
        request, capture=capture_request, call_tool=capture_request,
        extra_tools=() if capture_request else ("inspect_active_work_item_execution_summary",),
    )
    final_model = FinalResponseModel()
    monkeypatch.setattr(response_synthesis, "run_typed_sdk_agent", lambda **kwargs: (
        run_typed_sdk_agent(**{
            **kwargs, "run_config": build_local_run_config(Provider(final_model)),
        })
    ))
    calls = []

    def native_chief(typed_input, **kwargs):
        capability = kwargs.get("operator_context_capability")
        assert capability is not None
        assert capability.scope.channel_id == "C111TEST" and capability.scope.user_id == "U111TEST"
        calls.append(typed_input)
        try:
            return chief.run_chief_of_staff_sdk(typed_input, **{
                **kwargs, "run_config": build_local_run_config(Provider(model)),
            })
        except Exception as exc:
            pytest.fail(f"Native Chief graph SDK failed: {type(exc).__name__}: {exc}")

    monkeypatch.setattr(workflow_runner, "run_chief_of_staff_sdk", native_chief)
    policy = resolve_slack_compact_continuity_policy(raw_context)
    assert policy.eligible
    workflow_request = WorkflowRunRequest(
        request_text=request, save=True, database_url=database, live_sdk=True, live_search=False,
        context_file_path=str(path), sdk_session_enabled=False,
        manual_request_plan=plan.model_dump(mode="json"),
        orchestrator_preflight={
            "execution_allowed": True, "manual_request_plan": plan.model_dump(mode="json"),
            "route_result": {"route": "chief_of_staff", "workflow": ["chief_of_staff"],
                             "routing_mode": "llm"},
        },
    )
    assert langgraph_workflow.should_use_langgraph_for_work_item(workflow_request)
    with activate_continuity_policy(policy):
        result = langgraph_workflow.advance_work_item_manager_loop_with_optional_langgraph(
            workflow_request, max_steps=1,
        )
    assert len(calls) == 1 and len(model.inputs) == (2 if capture_request else 1)
    assert len(final_model.inputs) == 1
    if capture_request:
        assert model.outputs[0]["verified"] is True
    store = SQLiteStore(database)
    assert len(store.list_memory_items()) == int(capture_request)
    assert result.artifact_refs, result.human_summary
    assert result.work_item.status == WorkItemStatus.DONE
    assert result.next_action.action == (
        "review_saved_memory" if capture_request else "review_chief_of_staff_plan"
    )
    assert result.next_action.requires_approval is not capture_request
    graph = next(event for event in store.list_work_item_events(result.work_item.id)
                 if event.event_type == "langgraph_orchestration")
    assert graph.metadata["runtime"] == "langgraph"
    inspection = inspect_graph_execution(
        graph.metadata["execution_id"], database_url=database, include_state=True,
    )
    assert inspection["status"] == ("completed" if capture_request else "interrupted")
    assert len(inspection["history"]) > 1
    captured = [operation for operation in inspection["operations"]
                if operation["tool_name"] == "capture_operator_memory"]
    assert len(captured) == int(capture_request)
    if capture_request:
        assert captured[0]["status"] == "verified"
        assert json.loads(captured[0]["receipt_json"])["provider"] == "local_memory"
