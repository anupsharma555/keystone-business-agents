from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

import keystone_agents.entrypoints.cli_impl as cli

try:
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )
except ImportError:
    pytestmark = pytest.mark.skip(reason="OpenAI Agents SDK fake-model hooks unavailable.")

from keystone_agents.operator_failures import known_exception_to_operator_failure
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.execution_attempt import start_execution_attempt
from keystone_agents.runtime.request_budget import (
    MODEL_REQUEST_BUDGET_LIMIT_ENV,
    MODEL_REQUEST_BUDGET_SCHEMA,
    ModelRequestBudgetExhausted,
    activate_model_request_budget,
    current_model_request_budget_snapshot,
    model_request_count_from_payload,
    reserve_child_model_request_budget,
)
from keystone_agents.sdk import (
    build_local_run_config,
    build_sdk_agent,
    function_tool,
    run_sdk_sync_with_config,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


def _message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="message-1",
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


def _tool_call(name: str, arguments: dict[str, Any]) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name=name,
        call_id="call-1",
        arguments=json.dumps(arguments),
        status="completed",
    )


class _FakeModel(Model):
    def __init__(self, outputs: list[list[Any]]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> ModelResponse:
        del (
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )
        self.calls += 1
        return ModelResponse(
            output=self.outputs.pop(0),
            usage=Usage(requests=1),
            response_id=f"fake-response-{self.calls}",
        )

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> AsyncIterator[Any]:
        raise NotImplementedError


class _FakeProvider(ModelProvider):
    def __init__(self, model: _FakeModel) -> None:
        self.model = model

    def get_model(self, model_name: str | None) -> Model:
        del model_name
        return self.model


def test_sdk_hook_blocks_request_n_plus_one_before_model_and_does_not_replay_tool() -> None:
    mutation_calls: list[str] = []

    @function_tool(name_override="create_test_object")
    def create_test_object(marker: str) -> str:
        mutation_calls.append(marker)
        return json.dumps({"status": "created", "marker": marker})

    model = _FakeModel(
        outputs=[
            [_tool_call("create_test_object", {"marker": "KBA_TEST_OBJECT"})],
            [_message("Created exactly once.")],
        ]
    )
    agent = build_sdk_agent(
        name="request_budget_test_agent",
        instructions="Call the tool once, then report completion.",
        output_type=None,
        tools=[create_test_object],
        enforce_tool_policy=False,
    )

    with activate_model_request_budget(1) as ledger:
        with pytest.raises(ModelRequestBudgetExhausted) as raised:
            run_sdk_sync_with_config(
                agent,
                "Create the marked test object once.",
                build_local_run_config(_FakeProvider(model)),
                max_turns=3,
            )
        snapshot = ledger.snapshot()

    assert model.calls == 1
    assert mutation_calls == ["KBA_TEST_OBJECT"]
    assert raised.value.consumed == 1
    assert snapshot["consumed"] == 1
    assert snapshot["remaining"] == 0
    assert snapshot["exhausted"] is True
    assert snapshot["exhaustion_stage"] == "request_budget_test_agent:llm_start"


def test_child_budget_reserves_remaining_and_releases_unused_allowance() -> None:
    with activate_model_request_budget(5) as ledger:
        ledger.consume(stage="orchestrator:llm_start")
        ledger.consume(stage="orchestrator:repair")
        lease = reserve_child_model_request_budget(stage="gmail_triage:child_process")
        assert lease is not None
        assert lease.environment()[MODEL_REQUEST_BUDGET_LIMIT_ENV] == "3"
        assert ledger.remaining == 0

        lease.settle(2)
        snapshot = ledger.snapshot()

    assert snapshot["consumed"] == 4
    assert snapshot["remaining"] == 1
    assert snapshot["reserved"] == 0


def test_child_budget_preserves_child_exhaustion_stage() -> None:
    with activate_model_request_budget(2) as ledger:
        ledger.consume(stage="orchestrator:llm_start")
        lease = reserve_child_model_request_budget(stage="gmail_triage:child_process")
        assert lease is not None

        lease.settle(1, exhaustion_stage="gmail_triage:repair")
        snapshot = ledger.snapshot()

    assert snapshot["consumed"] == 2
    assert snapshot["remaining"] == 0
    assert snapshot["exhausted"] is True
    assert (
        snapshot["exhaustion_stage"]
        == "gmail_triage:child_process/gmail_triage:repair"
    )


def test_typed_runner_attaches_current_budget_snapshot_to_request_cache() -> None:
    model = _FakeModel(outputs=[[_message("Completed.")]])
    agent = build_sdk_agent(
        name="request_budget_snapshot_agent",
        instructions="Return a short completion.",
        output_type=None,
        tools=[],
        enforce_tool_policy=False,
    )

    with activate_model_request_budget(2):
        result = run_typed_sdk_agent(
            agent=agent,
            typed_input="Complete one bounded synthesis.",
            output_type=str,
            run_config=build_local_run_config(_FakeProvider(model)),
            max_turns=1,
        )

    assert result.request_cache["request_budget"]["limit"] == 2
    assert result.request_cache["request_budget"]["consumed"] == 1
    assert result.request_cache["request_budget"]["remaining"] == 1


def test_cli_child_process_receives_remaining_budget_and_reconciles_usage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_child(_command: list[str], **kwargs: Any) -> Any:
        captured.update(kwargs)
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "status": "blocked",
                        "summary": "No matching test object was found.",
                        "send_enabled": False,
                        "usage": {"requests": 2},
                    }
                ),
                "stderr": "",
            },
        )()

    monkeypatch.setattr(cli, "run_isolated_child_process", fake_child)
    with activate_model_request_budget(4) as ledger:
        ledger.consume(stage="orchestrator:llm_start")
        exit_code = cli._run_ask_script_live(
            "gmail_triage",
            "Find the bounded test thread.",
            ["unused-child-command"],
            json_output=True,
            manual_plan=None,
            database_url=f"sqlite:///{tmp_path / 'child-budget.sqlite'}",
        )
        snapshot = ledger.snapshot()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert captured["env"][MODEL_REQUEST_BUDGET_LIMIT_ENV] == "3"
    assert payload["request_budget"]["consumed"] == 3
    assert snapshot["consumed"] == 3
    assert snapshot["remaining"] == 1


def test_failed_child_reports_enforced_root_request_total(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    child_budget = {
        "schema": MODEL_REQUEST_BUDGET_SCHEMA,
        "limit": 3,
        "consumed": 3,
        "remaining": 0,
        "exhausted": True,
        "exhaustion_stage": "gmail_triage:llm_start",
    }
    child_payload = {
        "status": "failed",
        "output": {
            "failure": {
                "schema": "keystone.operator_failure.v1",
                "kind": "model_request_budget_exhausted",
                "summary": "Gmail Triage exhausted its child request budget.",
                "reason": "A fourth child request was rejected before dispatch.",
                "next_step": "Review the completed evidence before retrying.",
                "retryable": False,
                "safe_to_continue": True,
            }
        },
        "usage": {"requests": 3},
        "request_budget": child_budget,
        "request_cache": {"request_budget": child_budget},
        "sdk_failure": {
            "schema": "keystone.sdk_run_failure.v1",
            "failure_kind": "modelrequestbudgetexhausted",
            "usage": {"requests": 3},
            "request_cache": {"request_budget": child_budget},
        },
    }

    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_args, **_kwargs: type(
            "Completed",
            (),
            {
                "returncode": 1,
                "stdout": json.dumps(child_payload),
                "stderr": "",
            },
        )(),
    )

    with activate_model_request_budget(4) as ledger:
        ledger.consume(stage="orchestrator:llm_start")
        exit_code = cli._run_ask_script_live(
            "gmail_triage",
            "Find the bounded test thread.",
            ["unused-child-command"],
            json_output=True,
            manual_plan=None,
            database_url=f"sqlite:///{tmp_path / 'failed-child-budget.sqlite'}",
        )
        snapshot = ledger.snapshot()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["openai_requests_made"] == 4
    assert payload["entry_request_accounting"]["actual_child_requests"] == 3
    assert payload["entry_request_accounting"]["actual_total_requests"] == 4
    assert snapshot["consumed"] == 4


def test_execution_attempt_terminal_telemetry_uses_enforced_ledger(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'request-budget.sqlite'}")
    with activate_model_request_budget(1) as ledger:
        attempt = start_execution_attempt(
            store=store,
            request_text="Use one bounded model request.",
            route_hint="chief_of_staff",
            live=True,
            max_model_requests=1,
        )
        ledger.consume(stage="chief_of_staff:llm_start")
        with pytest.raises(ModelRequestBudgetExhausted) as raised:
            ledger.consume(stage="chief_of_staff:repair")
        payload = attempt.finalize(
            status="failed",
            exit_code=1,
            failure=raised.value,
        )

    assert payload["request_budget"]["limit"] == 1
    assert payload["request_budget"]["consumed"] == 1
    assert payload["request_budget"]["remaining"] == 0
    assert payload["request_budget"]["status"] == "exhausted"
    assert payload["request_budget"]["exhaustion_stage"] == "chief_of_staff:repair"
    assert (
        payload["execution_telemetry"]["model_request_budget"]["correlation_id"]
        == payload["request_budget"]["correlation_id"]
    )


def test_budget_exhaustion_has_stable_operator_failure() -> None:
    error = ModelRequestBudgetExhausted(
        limit=2,
        consumed=2,
        stage="gmail_triage:repair",
        correlation_id="budget-test",
    )

    failure = known_exception_to_operator_failure(error, context="KBA run")

    assert failure.kind == "model_request_budget_exhausted"
    assert failure.retryable is False
    assert failure.safe_to_continue is True


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "request_budget": {
                    "schema": MODEL_REQUEST_BUDGET_SCHEMA,
                    "consumed": 3,
                }
            },
            3,
        ),
        (
            {
                "request_cache": {
                    "request_budget": {
                        "schema": MODEL_REQUEST_BUDGET_SCHEMA,
                        "consumed": 4,
                    }
                }
            },
            4,
        ),
        (
            {
                "request_cache": {
                    "request_budget": {
                        "schema": MODEL_REQUEST_BUDGET_SCHEMA,
                        "consumed": 3,
                    }
                },
                "usage": {"requests": 4},
            },
            4,
        ),
        ({"usage": {"requests": 2}}, 2),
        ({"openai_requests": 1}, 1),
        ({"provider_usage": {"requests_attempted": 9}}, None),
    ],
)
def test_child_request_count_ignores_non_model_provider_usage(
    payload: dict[str, Any],
    expected: int | None,
) -> None:
    assert model_request_count_from_payload(payload) == expected


def test_budget_snapshot_is_scoped() -> None:
    assert current_model_request_budget_snapshot() is None
    with activate_model_request_budget(2) as ledger:
        ledger.consume(stage="orchestrator:llm_start")
        assert current_model_request_budget_snapshot()["consumed"] == 1
    assert current_model_request_budget_snapshot() is None


@pytest.mark.parametrize("completes_within_allowance", [True, False])
def test_chief_admission_uses_shared_allowance_and_sdk_still_blocks_ninth_request(
    completes_within_allowance,
):
    from types import SimpleNamespace

    request = "Open the selected email and give three bullets and an internal outreach template."
    plan = cli.ManualRequestPlan(
        source="heuristic", requested_agent="chief_of_staff", target_agent="chief_of_staff",
        intent="route_request", objective=request, requires_durable_state=True,
        ask_shape={"output_constraints": {
            "item_count_mode": "exact", "minimum_items": 3, "maximum_items": 3,
        }},
    )
    estimate = cli._estimate_ask_openai_requests(
        SimpleNamespace(context_file="", agent="chief_of_staff", max_manager_steps=5,
                        live_search=False),
        input_text=request, live_sdk=True, live_manual_plan=False,
        requested_route="chief_of_staff", manual_plan=plan,
        effective_live_search=False, observed_orchestrator_requests=1,
    )
    assert estimate["max"] == 10
    assert cli._ask_request_estimate_exceeds_ceiling(
        estimate, requested_limit=8, openai_requests_made=1,
    ) is False
    assert cli._ask_request_estimate_exceeds_ceiling(
        estimate, requested_limit=1, openai_requests_made=1,
    ) is True

    reads = []

    @function_tool
    def read_block(block_index: int) -> str:
        reads.append(block_index)
        return f"Synthetic evidence block {block_index}."

    outputs = [
        [_tool_call("read_block", {"block_index": i}).model_copy(
            update={"call_id": f"read-{i}"}
        )] for i in range(6)
    ]
    outputs.append(
        [_message("Completed within allowance.")]
        if completes_within_allowance else [
            _tool_call("read_block", {"block_index": 6}).model_copy(
                update={"call_id": "read-6"}
            )
        ]
    )
    outputs.append([_message("Must not reach this ninth logical request.")])
    model = _FakeModel(outputs)
    agent = build_sdk_agent(
        name="budgeted_parent_loop", instructions="Read evidence, then answer.",
        output_type=None, tools=[read_block], enforce_tool_policy=False,
    )
    with activate_model_request_budget(8) as ledger:
        ledger.consume(stage="orchestrator:preflight")
        if completes_within_allowance:
            result = run_typed_sdk_agent(
                agent=agent, typed_input=request, output_type=str,
                run_config=build_local_run_config(_FakeProvider(model)), max_turns=8,
            )
            assert result.output == "Completed within allowance."
        else:
            with pytest.raises(ModelRequestBudgetExhausted):
                run_typed_sdk_agent(
                    agent=agent, typed_input=request, output_type=str,
                    run_config=build_local_run_config(_FakeProvider(model)), max_turns=8,
                )
        assert ledger.consumed == 8
    assert model.calls == 7
    assert reads == list(range(6 if completes_within_allowance else 7))
