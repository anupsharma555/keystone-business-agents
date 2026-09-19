"""Real Responses transport failures retain only safe diagnostics and numeric usage."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from agents import _debug
from agents.exceptions import ModelBehaviorError
from openai import AsyncOpenAI
from pydantic import BaseModel

from keystone_agents import sdk
from keystone_agents.model_provider import ModelConfig
from keystone_agents.run import run_typed_sdk_agent, sdk_run_failure_metadata
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.runtime.response_terminal import response_terminal_diagnostics

PRIVATE = "SYNTHETIC_TERMINAL_TEXT_MUST_NOT_BE_RETAINED"


class Answer(BaseModel):
    answer: str


def response(
    *,
    status="incomplete",
    reason="max_output_tokens",
    code=None,
    inputs=100,
    outputs=60,
    usage=True,
    tool=False,
):
    return {
        "id": PRIVATE,
        "object": "response",
        "created_at": 1,
        "status": status,
        "model": "gpt-5.4-mini",
        "parallel_tool_calls": True,
        "max_output_tokens": 6000,
        "incomplete_details": {"reason": reason} if status == "incomplete" else None,
        "error": {"code": code, "message": PRIVATE} if code else None,
        "output": (
            [
                {
                    "type": "function_call",
                    "name": "read_synthetic_source",
                    "call_id": "synthetic-call",
                    "arguments": "{}",
                }
            ]
            if tool
            else [
                {
                    "id": PRIVATE,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps({"answer": "ready"})
                            if status == "completed"
                            else PRIVATE,
                            "annotations": [],
                        }
                    ],
                }
            ]
        ),
        "usage": (
            {
                "input_tokens": inputs,
                "output_tokens": outputs,
                "total_tokens": inputs + outputs,
                "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 50},
            }
            if usage
            else None
        ),
    }


def install_transport(monkeypatch, replies, *, barrier=None):
    calls = []
    pending = list(replies)

    def handle(request):
        calls.append(json.loads(request.content))
        if barrier is not None:
            barrier.wait(timeout=10)
        return httpx.Response(200, json=pending.pop(0))

    def factory(**kwargs):
        kwargs["base_url"] = "https://synthetic.example.test/v1"
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    monkeypatch.setattr(sdk, "AsyncOpenAI", factory)
    return calls


def run(*, tools=()):
    return run_typed_sdk_agent(
        agent=sdk.build_sdk_agent(
            "terminal_probe",
            "Return a short structured answer.",
            Answer,
            tools=list(tools),
            model="gpt-5.4-mini",
            enforce_tool_policy=False,
            model_settings=sdk.build_model_settings(max_tokens=6000),
        ),
        typed_input={"request": "Synthetic terminal-response probe."},
        output_type=Answer,
        live=True,
        config=ModelConfig(
            provider="openai", model="gpt-5.4-mini", api_key="synthetic-key", use_responses=True
        ),
        inherit_env_session=False,
        max_turns=3,
        tracing_disabled=True,
    )


@pytest.fixture(autouse=True)
def privacy(monkeypatch):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setenv("KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES", "1")
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "0")


@pytest.mark.parametrize(
    ("status", "reason", "code", "kind"),
    [
        ("incomplete", "max_output_tokens", None, "model_output_limit_reached"),
        ("incomplete", "max_messages", None, "model_output_limit_reached"),
        ("incomplete", "content_filter", None, "model_response_incomplete"),
        ("failed", None, "server_error", "model_response_failed"),
    ],
)
def test_terminal_response_captures_usage_and_does_not_retry_identical_schema(
    monkeypatch,
    status,
    reason,
    code,
    kind,
):
    calls = install_transport(monkeypatch, [response(status=status, reason=reason, code=code)])
    with activate_model_request_budget(3) as budget:
        with pytest.raises(ModelBehaviorError) as caught:
            run()
        assert budget.consumed == 1
    assert len(calls) == 1 and caught.value.run_data is None
    failure = sdk_run_failure_metadata(caught.value)
    assert failure["failure_kind"] == kind
    assert failure["usage"]["requests"] == 1
    assert failure["usage"]["input_tokens"] == 100
    assert failure["usage"]["output_tokens"] == 60
    assert failure["usage"]["cached_input_tokens"] == 20
    assert failure["usage"]["cache_write_input_tokens"] == 0
    assert failure["cost"]["estimated_usd"] > 0
    assert not failure["request_cache"].get("structured_output_retries")
    assert "validation_diagnostics" not in failure["request_cache"]
    observation = failure["request_cache"]["response_terminal"]["observations"][0]
    assert observation["status"] == status and observation["max_output_tokens"] == 6000
    assert PRIVATE not in json.dumps(failure)
    assert sdk._RESPONSE_TERMINAL_OBSERVER.get() is None


def test_prior_success_and_terminal_failure_count_each_response_once(monkeypatch):
    calls = install_transport(
        monkeypatch,
        [
            response(status="completed", inputs=40, outputs=10, tool=True),
            response(inputs=100, outputs=60),
        ],
    )
    reads = []

    @sdk.function_tool
    def read_synthetic_source() -> str:
        """Return one local synthetic source."""
        reads.append(1)
        return "Synthetic bounded source."

    with pytest.raises(ModelBehaviorError) as caught:
        run(tools=[read_synthetic_source])
    usage = sdk_run_failure_metadata(caught.value)["usage"]
    assert len(calls) == usage["requests"] == 2 and reads == [1]
    assert usage["input_tokens"] == 140 and usage["output_tokens"] == 70
    assert usage["complete"] is True


def test_success_is_observed_only_by_existing_sdk_end_hook(monkeypatch):
    calls = install_transport(monkeypatch, [response(status="completed")])
    result = run()
    assert result.output.answer == "ready" and len(calls) == result.usage["requests"] == 1
    assert result.usage["input_tokens"] == 100
    assert not result.request_cache.get("response_terminal")
    assert sdk._RESPONSE_TERMINAL_OBSERVER.get() is None


def test_absent_usage_and_unknown_enum_strings_remain_unknown_and_private(monkeypatch):
    calls = install_transport(monkeypatch, [response(reason=PRIVATE, code=PRIVATE, usage=False)])
    with pytest.raises(ModelBehaviorError) as caught:
        run()
    failure = sdk_run_failure_metadata(caught.value)
    assert len(calls) == 1
    assert failure["usage"].get("input_tokens") is None and not failure["usage"]["complete"]
    assert failure["usage"]["requests"] == 1
    observation = response_terminal_diagnostics(caught.value)["observations"][0]
    assert observation["reason"] == observation["error_code"] == "unknown"
    assert PRIVATE not in json.dumps(failure)
    assert PRIVATE not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None


def test_concurrent_terminal_observers_do_not_share_usage_or_reason(monkeypatch):
    barrier = Barrier(2)

    def factory(**kwargs):
        def handle(request):
            payload = json.loads(request.content)
            variant = "filtered" if "filtered" in json.dumps(payload["input"]) else "limited"
            barrier.wait(timeout=10)
            return httpx.Response(
                200,
                json=response(
                    reason="content_filter" if variant == "filtered" else "max_output_tokens",
                    inputs=170 if variant == "filtered" else 230,
                ),
            )

        kwargs["base_url"] = "https://synthetic.example.test/v1"
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    monkeypatch.setattr(sdk, "AsyncOpenAI", factory)

    def fail(variant):
        agent = sdk.build_sdk_agent(
            "terminal_probe",
            "Return a structured answer.",
            Answer,
            tools=[],
            model="gpt-5.4-mini",
            enforce_tool_policy=False,
        )
        with pytest.raises(ModelBehaviorError) as caught:
            run_typed_sdk_agent(
                agent=agent,
                typed_input={"request": variant},
                output_type=Answer,
                live=True,
                config=ModelConfig(
                    provider="openai",
                    model="gpt-5.4-mini",
                    api_key="synthetic-key",
                    use_responses=True,
                ),
                inherit_env_session=False,
                tracing_disabled=True,
            )
        assert sdk._RESPONSE_TERMINAL_OBSERVER.get() is None
        return sdk_run_failure_metadata(caught.value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        failures = list(pool.map(fail, ["filtered", "limited"]))
    assert [value["usage"]["input_tokens"] for value in failures] == [170, 230]
    assert [value["failure_kind"] for value in failures] == [
        "model_response_incomplete",
        "model_output_limit_reached",
    ]


def test_http_429_keeps_existing_bounded_retry_behavior(monkeypatch):
    calls = []

    def handle(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "Synthetic rate limit.",
                        "type": "rate_limit_error",
                        "code": "rate_limit",
                    }
                },
                headers={"retry-after": "0"},
            )
        return httpx.Response(200, json=response(status="completed"))

    def factory(**kwargs):
        kwargs["base_url"] = "https://synthetic.example.test/v1"
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    monkeypatch.setattr(sdk, "AsyncOpenAI", factory)
    monkeypatch.setenv(sdk.LIVE_MODEL_MAX_RETRIES_ENV, "0")
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "1")
    monkeypatch.setattr("keystone_agents.run._sdk_rate_limit_retry_delay_seconds", lambda *_: 0)
    with activate_model_request_budget(2) as budget:
        result = run()
        assert budget.consumed == 2
    assert len(calls) == 2 and result.output.answer == "ready"
    assert result.request_cache["rate_limit_retries"] == 1
    assert not result.request_cache.get("response_terminal")


def test_nested_failed_response_does_not_contaminate_parent_success(monkeypatch):
    from contextvars import copy_context

    child_failure = {}
    parent_responses = []

    def handle(request):
        payload = json.loads(request.content)
        if "child-terminal" in json.dumps(payload["input"]):
            return httpx.Response(200, json=response(inputs=700, reason="content_filter"))
        parent_responses.append(1)
        packet = response(
            status="completed", inputs=40, outputs=10, tool=len(parent_responses) == 1
        )
        if len(parent_responses) == 1:
            packet["output"][0]["name"] = "inspect_child_terminal"
        return httpx.Response(200, json=packet)

    def factory(**kwargs):
        kwargs["base_url"] = "https://synthetic.example.test/v1"
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    monkeypatch.setattr(sdk, "AsyncOpenAI", factory)

    def child():
        with pytest.raises(ModelBehaviorError) as caught:
            run_typed_sdk_agent(
                agent=sdk.build_sdk_agent(
                    "child",
                    "Return an answer.",
                    Answer,
                    tools=[],
                    model="gpt-5.4-mini",
                    enforce_tool_policy=False,
                ),
                typed_input={"request": "child-terminal"},
                output_type=Answer,
                live=True,
                config=ModelConfig(
                    provider="openai",
                    model="gpt-5.4-mini",
                    api_key="synthetic-key",
                    use_responses=True,
                ),
                inherit_env_session=False,
                tracing_disabled=True,
            )
        child_failure.update(sdk_run_failure_metadata(caught.value))

    @sdk.function_tool
    def inspect_child_terminal() -> str:
        """Run a synthetic child assessment with independent diagnostics."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(copy_context().run, child).result(timeout=10)
        return "Child assessment returned a bounded blocked result."

    with activate_model_request_budget(3) as budget:
        result = run(tools=[inspect_child_terminal])
        assert budget.consumed == 3
    assert result.output.answer == "ready"
    assert result.usage["requests"] == 2 and result.usage["input_tokens"] == 80
    assert not result.request_cache.get("response_terminal")
    assert child_failure["usage"]["requests"] == 1
    assert child_failure["usage"]["input_tokens"] == 700
    assert child_failure["failure_kind"] == "model_response_incomplete"
    assert sdk._RESPONSE_TERMINAL_OBSERVER.get() is None
