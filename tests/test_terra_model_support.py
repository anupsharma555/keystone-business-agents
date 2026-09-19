from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from agents import ModelProvider
from agents.models.openai_responses import OpenAIResponsesModel
from agents.usage import Usage
from openai import AsyncOpenAI
from pydantic import BaseModel

from keystone_agents.costing import estimate_usage_cost, pricing_metadata_available
from keystone_agents.model_provider import (
    CANARY_LUNA_MODEL,
    CANARY_TERRA_MODEL,
    DEFAULT_MODEL,
    ModelConfig,
    uses_gpt56_cache_controls,
)
from keystone_agents.run import _aggregate_sdk_attempt_usage, extract_sdk_usage, run_typed_sdk_agent
from keystone_agents.sdk import (
    build_local_run_config,
    build_model_settings,
    build_sdk_agent,
    numeric_sdk_request_usage,
    private_context_sdk_profile,
)

TERRA = CANARY_TERRA_MODEL
LUNA = CANARY_LUNA_MODEL


def usage(input_tokens=1000, cached=200, writes=300, output=100):
    return {
        "available": True,
        "requests": 1,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": writes,
        "output_tokens": output,
        "total_tokens": input_tokens + output,
    }


@pytest.mark.parametrize(
    ("model", "input_tokens", "amount", "long_count"),
    [
        (TERRA, 1000, 0.00299, 0),
        (TERRA, 272000, 0.54499, 0),
        (TERRA, 272001, 1.089384, 1),
        (LUNA, 1000, 0.000299, 0),
        (LUNA, 272000, 0.054499, 0),
        (LUNA, 272001, 0.1089384, 1),
    ],
)
def test_reviewed_models_price_cache_writes_and_exact_context_boundary(
    model, input_tokens, amount, long_count
):
    cost = estimate_usage_cost(provider="openai", model=model, usage=usage(input_tokens))
    assert cost["estimated_usd"] == amount
    assert cost["long_context_requests"] == long_count
    assert cost["billable_tokens"]["input_tokens"] == input_tokens - 500
    assert cost["billable_tokens"]["cache_write_input_tokens"] == 300
    assert cost["pricing_source_url"] == "https://developers.openai.com/api/docs/pricing"


def test_terra_prices_each_request_not_combined_context_and_never_counts_entries_twice():
    entries = [usage(200000, cached=0, writes=0), usage(200000, cached=0, writes=0)]
    combined = _aggregate_sdk_attempt_usage([{"usage": entry} for entry in entries])
    cost = estimate_usage_cost(provider="openai", model=TERRA, usage=combined)
    assert cost["estimated_usd"] == 0.8024
    assert cost["long_context_requests"] == 0
    assert cost["billable_tokens"]["input_tokens"] == 400000
    entries[1] = usage(300000, cached=0, writes=0)
    combined = _aggregate_sdk_attempt_usage([{"usage": entry} for entry in entries])
    assert (
        estimate_usage_cost(provider="openai", model=TERRA, usage=combined)["estimated_usd"]
        == 1.603
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"cache_write_input_tokens": None},
        {"requests": 2},
        {"complete": False},
        {"cached_input_tokens": 900, "cache_write_input_tokens": 200},
        {"service_tier": "fast"},
    ],
)
@pytest.mark.parametrize("model", [TERRA, LUNA])
def test_reviewed_models_refuse_incomplete_or_unsupported_accounting(updates, model):
    cost = estimate_usage_cost(provider="openai", model=model, usage={**usage(), **updates})
    assert cost["amount_usd"] is None
    assert cost["complete"] is False


@pytest.mark.parametrize("model", [TERRA, LUNA])
def test_reviewed_model_support_is_explicit_and_does_not_change_defaults(model):
    assert DEFAULT_MODEL == "gpt-5.4-mini"
    assert pricing_metadata_available(provider="openai", model=model)
    assert not pricing_metadata_available(provider="openai", model=f"{model}-pro")
    assert uses_gpt56_cache_controls(model)
    assert not uses_gpt56_cache_controls(f"{model}-pro")
    assert not uses_gpt56_cache_controls(DEFAULT_MODEL)
    ModelConfig(
        provider="openai", model=model, api_key="synthetic-test-key"
    ).require_live_execution_ready()


def test_sdk_normalized_missing_cache_write_is_unknown_but_provider_zero_is_preserved():
    normalized = Usage(requests=1, input_tokens=1000, output_tokens=100, total_tokens=1100)
    assert numeric_sdk_request_usage(normalized)["cache_write_input_tokens"] is None
    original = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "total_tokens": 1100,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    }
    projected = numeric_sdk_request_usage(normalized, raw_usage=original)
    assert projected["cache_write_input_tokens"] == 0
    del original["input_tokens_details"]["cache_write_tokens"]
    assert (
        numeric_sdk_request_usage(normalized, raw_usage=original)["cache_write_input_tokens"]
        is None
    )


class Answer(BaseModel):
    answer: str


def mock_provider(requests, *, valid=True, answer_payload=None, model=TERRA):
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_synthetic",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": model,
                "parallel_tool_calls": True,
                "output": [
                    {
                        "id": "msg_synthetic",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    answer_payload
                                    if answer_payload is not None
                                    else {"answer": "ready"}
                                    if valid
                                    else {"wrong": "invalid"}
                                ),
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "total_tokens": 1100,
                    "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300},
                    "output_tokens_details": {"reasoning_tokens": 20},
                },
            },
        )

    client = AsyncOpenAI(
        api_key="synthetic-test-key",
        base_url="https://synthetic.example.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )

    class Provider(ModelProvider):
        def get_model(self, model_name):
            return OpenAIResponsesModel(model_name, openai_client=client)

    return Provider()


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("model,expected_cost", [(TERRA, 0.00299), (LUNA, 0.000299)])
def test_real_sdk_serializes_reviewed_controls_and_preserves_accounting_without_network(
    private, model, expected_cost, monkeypatch
):
    monkeypatch.setenv("OPENAI_BASE_URL", "inherited.example.test/v1")
    requests = []
    # Build under the existing default, then override the actual model at dispatch.
    agent = build_sdk_agent("synthetic_terra_test", "Return a short answer.", Answer, tools=[])
    with private_context_sdk_profile() if private else nullcontext():
        result = run_typed_sdk_agent(
            agent=agent,
            typed_input="Synthetic request",
            output_type=Answer,
            live=False,
            config=ModelConfig(provider="openai", model=model),
            run_config=build_local_run_config(mock_provider(requests, model=model), model=model),
            max_turns=1,
        )
    assert len(requests) == 1
    payload = requests[0]
    assert payload["model"] == model
    assert "prompt_cache_retention" not in payload
    if private:
        assert payload["prompt_cache_options"] == {"mode": "explicit"}
        assert payload["store"] is False
    else:
        assert "prompt_cache_options" not in payload
    assert result.usage["cache_write_input_tokens"] == 300
    assert len(result.usage["request_usage_entries"]) == 1
    assert result.cost["estimated_usd"] == expected_cost
    assert result.output.answer == "ready"


@pytest.mark.parametrize("model,expected_cost", [(TERRA, 0.00299), (LUNA, 0.000299)])
def test_failed_reviewed_model_output_retains_cache_write_usage(model, expected_cost):
    requests = []
    agent = build_sdk_agent(
        "synthetic_terra_test", "Return a short answer.", Answer, tools=[], model=model
    )
    with pytest.raises(Exception) as caught:
        run_typed_sdk_agent(
            agent=agent,
            typed_input="Synthetic request",
            output_type=Answer,
            live=False,
            config=ModelConfig(provider="openai", model=model),
            run_config=build_local_run_config(
                mock_provider(requests, valid=False, model=model), model=model,
            ),
            max_turns=1,
        )
    failure = caught.value.keystone_sdk_run_failure
    assert failure["usage"]["requests"] == len(requests) == 1
    assert failure["usage"]["cache_write_input_tokens"] == 300
    assert failure["cost"]["estimated_usd"] == expected_cost


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("model", [TERRA, LUNA])
def test_existing_sdk_settings_accept_documented_reasoning_efforts(effort, model):
    agent = build_sdk_agent(
        "synthetic_terra_test",
        "Return a short answer.",
        Answer,
        tools=[],
        model=model,
        model_settings=build_model_settings(reasoning_effort=effort),
    )
    assert agent.model_settings.reasoning.effort == effort
    assert agent.model_settings.prompt_cache_retention is None


def test_provider_request_entries_are_preserved_without_summing_aggregate_usage_again():
    entries = [usage(100000, cached=0, writes=0), usage(200000, cached=0, writes=0)]
    raw = SimpleNamespace(
        usage={"requests": 2, "input_tokens": 300000, "output_tokens": 200, "total_tokens": 300200},
        keystone_sdk_numeric_usage={"responses": entries},
    )
    projected = extract_sdk_usage(raw)
    assert projected["input_tokens"] == 300000
    assert projected["cache_write_input_tokens"] == 0
    assert len(projected["request_usage_entries"]) == 2


def test_existing_experiment_harness_accepts_terra_override_with_scripted_http_responses():
    from keystone_agents.experiments import v2

    case = v2.load_catalog()[1][0]
    requests = []
    report = v2.run_experiments(
        experiment_id="context_memory",
        case_id=case.case_id,
        model=TERRA,
        max_model_requests=2,
        run_config=build_local_run_config(
            mock_provider(requests, answer_payload=case.scripted_answer.model_dump()),
            model=TERRA,
        ),
    )
    assert report.model_requests == len(requests) == 2
    assert report.provider_writes == 0
    assert all(row.status == "completed" for row in report.observations)
    assert all(row.stages[0]["configured_model"] == TERRA for row in report.observations)
    assert all(row.stages[0]["cost"]["estimated_usd"] == 0.00299 for row in report.observations)
