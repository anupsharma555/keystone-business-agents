from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import keystone_agents.entrypoints.cli_impl as cli

try:
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText
except ImportError:
    pytestmark = pytest.mark.skip(reason="OpenAI Agents SDK fake-model hooks unavailable.")

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.calendar_actions import CalendarActionPlan
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.execution_deadline import (
    EXECUTION_DEADLINE_CORRELATION_ENV,
    EXECUTION_FINALIZATION_RESERVE_MS_ENV,
    EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV,
    EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV,
    ExecutionDeadlineExceeded,
    ExecutionDeadlineLedger,
    activate_execution_deadline,
)
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.sdk import build_local_run_config, build_sdk_agent


class _BoundedOutput(BaseModel):
    summary: str


class _FakeModel(Model):
    def __init__(self) -> None:
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
        message = ResponseOutputMessage(
            id=f"deadline-integration-{self.calls}",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(
                    type="output_text",
                    text=json.dumps({"summary": "Bounded specialist result."}),
                    annotations=[],
                )
            ],
        )
        return ModelResponse(
            output=[message],
            usage=Usage(requests=1),
            response_id=f"deadline-integration-response-{self.calls}",
        )

    def stream_response(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError


class _FakeProvider(ModelProvider):
    def __init__(self, model: _FakeModel) -> None:
        self.model = model

    def get_model(self, _model_name: str | None) -> Model:
        return self.model


@pytest.mark.parametrize("route", sorted(AGENT_REGISTRY))
def test_all_registered_agents_receive_one_request_wide_deadline(route: str) -> None:
    deadline = cli._execution_deadline_for_ask(
        SimpleNamespace(agent=route, live_search=False),
        "Review the bounded evidence and return the requested specialist result.",
    )

    assert 0 < deadline.remaining_soft_ms <= 300_000
    assert deadline.hard_deadline_epoch_ms - deadline.soft_deadline_epoch_ms == 60_000
    assert deadline.finalization_reserve_ms == 15_000


@pytest.mark.parametrize("route", sorted(AGENT_REGISTRY))
def test_shared_typed_runner_records_deadline_boundaries_for_every_agent(route: str) -> None:
    model = _FakeModel()
    agent = build_sdk_agent(
        name=route,
        instructions="Return the bounded structured result without tools.",
        output_type=_BoundedOutput,
        tools=[],
        enforce_tool_policy=False,
    )
    deadline = ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=10,
        hard_timeout_seconds=15,
        finalization_reserve_seconds=2,
        correlation_id=f"deadline-{route}",
    )

    with activate_execution_deadline(deadline):
        result = run_typed_sdk_agent(
            agent=agent,
            typed_input={"request": "Return one bounded result."},
            output_type=_BoundedOutput,
            run_config=build_local_run_config(_FakeProvider(model)),
            max_turns=2,
        )

    snapshot = result.request_cache["execution_deadline"]
    phases = [event["phase"] for event in snapshot["events"]]
    boundaries = [event["boundary"] for event in snapshot["events"]]
    assert model.calls == 1
    assert snapshot["correlation_id"] == f"deadline-{route}"
    assert "semantic_attempt" in boundaries
    assert "llm" in boundaries
    assert "model_completed" in phases


def test_direct_child_inherits_absolute_deadline_and_parent_trace(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    captured: dict[str, Any] = {}

    def fake_child(_command: list[str], **kwargs: Any) -> Any:
        env = kwargs["env"]
        captured["env"] = env
        captured["timeout"] = kwargs["timeout"]
        inherited = ExecutionDeadlineLedger.from_environment(env)
        assert inherited is not None
        inherited.admit(stage="opportunity_scout:llm_start", boundary="llm")
        inherited.checkpoint(stage="opportunity_scout:llm_end", boundary="llm")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "OpportunityResult",
                    "send_enabled": False,
                    "human_summary": "One verified opportunity was selected.",
                    "output": {"summary": "One verified opportunity was selected."},
                    "request_cache": {"execution_deadline": inherited.snapshot()},
                }
            ),
            stderr="",
        )

    monkeypatch.delenv("KEYSTONE_CHILD_AGENT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_child)
    parent = ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=120,
        hard_timeout_seconds=180,
        finalization_reserve_seconds=15,
        correlation_id="parent-child-deadline",
    )
    with activate_execution_deadline(parent):
        exit_code = cli._run_ask_script_live(
            "opportunity_scout",
            "Find one current opportunity and keep this read-only.",
            ["unused-child-command"],
            json_output=True,
            manual_plan=None,
            database_url=f"sqlite:///{tmp_path / 'deadline.db'}",
        )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    child_env = captured["env"]
    for key in (
        EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV,
        EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV,
        EXECUTION_FINALIZATION_RESERVE_MS_ENV,
        EXECUTION_DEADLINE_CORRELATION_ENV,
    ):
        assert child_env[key] == parent.environment()[key]
    assert 170 <= captured["timeout"] <= 180
    assert payload["execution_deadline"]["correlation_id"] == "parent-child-deadline"
    assert payload["execution_deadline"]["child_available"] is True
    assert payload["execution_deadline"]["parent"]["events"][-1]["phase"] == "completed"


def test_direct_calendar_write_is_rejected_before_provider_call_after_soft_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_create(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(cli, "create_google_calendar_event_impl", fake_create)
    deadline = ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=0,
        hard_timeout_seconds=30,
        finalization_reserve_seconds=5,
        correlation_id="expired-calendar-write",
    )
    plan = CalendarActionPlan(
        operation="create",
        title="Synthetic deadline test",
        start_date="2026-09-15",
        complete=True,
    )

    with activate_execution_deadline(deadline):
        payload = cli.execute_direct_calendar_action(
            "Create the synthetic deadline test on September 15, 2026.",
            plan,
            live=True,
        )

    assert called is False
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "calendar_write_blocked"
    assert payload["tool_execution"]["model_called_tool_names"] == []
    event = deadline.snapshot()["events"][-1]
    assert event["boundary"] == "provider_call"
    assert event["phase"] == "rejected"


def test_explicit_child_timeout_shortens_child_envelope_and_keeps_finalize_window(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    captured: dict[str, Any] = {}

    def fake_child(_command: list[str], **kwargs: Any) -> Any:
        inherited = ExecutionDeadlineLedger.from_environment(kwargs["env"])
        assert inherited is not None
        captured["deadline"] = inherited
        captured["timeout"] = kwargs["timeout"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "OpportunityResult",
                    "send_enabled": False,
                    "human_summary": "Bounded child result.",
                    "output": {"summary": "Bounded child result."},
                    "request_cache": {"execution_deadline": inherited.snapshot()},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_CHILD_AGENT_TIMEOUT_SECONDS", "75")
    monkeypatch.setenv("KEYSTONE_LIVE_MODEL_TIMEOUT_SECONDS", "45")
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_child)
    parent = ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=120,
        hard_timeout_seconds=180,
        finalization_reserve_seconds=15,
        correlation_id="short-child-deadline",
    )
    with activate_execution_deadline(parent):
        exit_code = cli._run_ask_script_live(
            "opportunity_scout",
            "Return one bounded synthetic opportunity.",
            ["unused-child-command"],
            json_output=True,
            manual_plan=None,
            database_url=f"sqlite:///{tmp_path / 'short-child.db'}",
        )

    assert exit_code == 1
    capsys.readouterr()
    child_deadline = captured["deadline"]
    assert captured["timeout"] == 75.0
    assert child_deadline.correlation_id == parent.correlation_id
    assert child_deadline.hard_deadline_epoch_ms < parent.hard_deadline_epoch_ms
    assert child_deadline.finalization_reserve_ms == 15_000
    assert (
        child_deadline.hard_deadline_epoch_ms - child_deadline.soft_deadline_epoch_ms
        == 60_000
    )


def test_expired_child_deadline_rejects_before_model_budget_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_called = False

    def fake_child(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal child_called
        child_called = True
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(cli, "run_isolated_child_process", fake_child)
    deadline = ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=0,
        hard_timeout_seconds=10,
        finalization_reserve_seconds=2,
        correlation_id="reject-before-reserve",
    )

    with activate_execution_deadline(deadline), activate_model_request_budget(5) as budget:
        with pytest.raises(ExecutionDeadlineExceeded):
            cli._run_ask_script_live(
                "opportunity_scout",
                "Return one bounded synthetic opportunity.",
                ["unused-child-command"],
                json_output=True,
                manual_plan=None,
            )
        snapshot = budget.snapshot()

    assert child_called is False
    assert snapshot["reserved"] == 0
    assert snapshot["events"] == []


def test_mismatched_child_deadline_is_not_labeled_correlated() -> None:
    parent = ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=120,
        hard_timeout_seconds=180,
        finalization_reserve_seconds=15,
        correlation_id="parent-deadline",
    )
    child = ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=60,
        hard_timeout_seconds=90,
        finalization_reserve_seconds=10,
        correlation_id="different-child-deadline",
    )
    payload: dict[str, Any] = {"input": "bounded request"}
    with activate_execution_deadline(parent):
        cli._attach_direct_specialist_entry_observability(
            payload,
            route="opportunity_scout",
            child_payload={"request_cache": {"execution_deadline": child.snapshot()}},
            orchestrator_preflight=None,
            child_process_ms=1.0,
            manual_plan=None,
            live_search=False,
        )

    trace = payload["execution_deadline"]
    assert trace["schema"] == "keystone.execution_deadline_observation.v1"
    assert trace["correlated"] is False
    assert trace["correlation_status"] == "mismatched"
    assert trace["correlation_id_match"] is False
    assert trace["correlation_id"] == "parent-deadline"


@pytest.mark.parametrize(
    ("public_status", "persisted_status"),
    [
        ("verified", "success"),
        ("completed", "success"),
        ("recovered", "success"),
        ("failed", "error"),
        ("canceled", "error"),
        ("partial", "partial"),
        ("needs_approval", "needs_approval"),
        ("needs_input", "needs_input"),
        ("blocked", "blocked"),
        ("clarification", "clarification"),
        ("timeout", "timeout"),
    ],
)
def test_child_public_status_maps_truthfully_to_persisted_run_status(
    public_status: str,
    persisted_status: str,
) -> None:
    assert cli._persisted_agent_run_status(
        {"status": "done", "public_result": {"status": public_status}}
    ) == persisted_status


@pytest.mark.parametrize(
    ("public_status", "expected_exit_code"),
    [
        ("completed", 0),
        ("partial", 0),
        ("needs_approval", 0),
        ("needs_input", 0),
        ("failed", 1),
        ("error", 1),
        ("timeout", 1),
    ],
)
def test_zero_returncode_child_uses_validated_public_terminal_status(
    public_status: str,
    expected_exit_code: int,
) -> None:
    assert cli._validated_child_exit_code(
        {"public_result": {"status": public_status}},
        child_returncode=0,
    ) == expected_exit_code
