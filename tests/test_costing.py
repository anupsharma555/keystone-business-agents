from __future__ import annotations

from typing import Any

import pytest
import requests

from keystone_agents.costing import (
    AgentRunBudgetExceededError,
    agent_run_budget_guard,
    compare_estimated_to_actual_cost,
    configured_agent_run_budget_usd,
    enforce_agent_run_budget,
    estimate_usage_cost,
    fetch_provider_cost_window,
    gemini_free_tier_usage_context,
    pricing_metadata_available,
    summarize_cache_experiment,
)


def test_estimate_usage_cost_uses_local_pricing_table_for_openai() -> None:
    cost = estimate_usage_cost(
        provider="openai",
        model="gpt-5.4-mini",
        usage={
            "available": True,
            "requests": 1,
            "input_tokens": 1_000,
            "cached_input_tokens": 200,
            "output_tokens": 500,
            "reasoning_output_tokens": 0,
            "total_tokens": 1_500,
        },
    )

    assert cost["source"] == "local_pricing_table"
    assert cost["pricing_model"] == "gpt-5.4-mini"
    assert cost["estimated_usd"] == 0.002865
    assert cost["components_usd"] == {
        "input": 0.0006,
        "cached_input": 0.000015,
        "output": 0.00225,
    }


def test_pricing_metadata_available_accepts_priced_prefix_aliases() -> None:
    assert pricing_metadata_available(provider="openai", model="gpt-5.4-mini")
    assert pricing_metadata_available(provider="openai", model="gpt-5.4-mini-2026-06-01")
    assert pricing_metadata_available(provider="gemini", model="gemini-2.5-flash")
    assert pricing_metadata_available(provider="openai", model="gpt-5.5") is False


def test_compare_estimated_to_actual_cost_reports_delta_without_secret_inputs() -> None:
    comparison = compare_estimated_to_actual_cost(
        cost={"source": "local_pricing_table", "estimated_usd": 0.16},
        actual_usd="0.150000",
        reference_id="openai-platform-run-copy",
    )

    assert comparison["available"] is True
    assert comparison["estimated_usd"] == 0.16
    assert comparison["actual_usd"] == 0.15
    assert comparison["delta_usd"] == 0.01
    assert comparison["delta_percent_of_actual"] == 6.67
    assert comparison["reference_id"] == "openai-platform-run-copy"


def test_summarize_cache_experiment_compares_repeated_runs() -> None:
    summary = summarize_cache_experiment(
        [
            {
                "run_id": "first",
                "usage": {
                    "available": True,
                    "input_tokens": 10_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000,
                },
                "cost": {"estimated_usd": 0.16},
            },
            {
                "run_id": "repeat",
                "usage": {
                    "available": True,
                    "input_tokens": 11_000,
                    "cached_input_tokens": 8_800,
                    "output_tokens": 700,
                },
                "cost": {"estimated_usd": 0.06},
            },
        ]
    )

    assert summary["available"] is True
    assert summary["first_cache_hit_rate"] == 0.0
    assert summary["last_cache_hit_rate"] == 0.8
    assert summary["cache_hit_rate_delta"] == 0.8
    assert summary["estimated_usd_delta"] == -0.1
    assert summary["runs"][1]["run_id"] == "repeat"


def test_estimate_usage_cost_does_not_assume_free_gemini() -> None:
    cost = estimate_usage_cost(
        provider="gemini",
        model="gemini-unknown",
        usage={
            "available": True,
            "requests": 1,
            "input_tokens": 1_000,
            "output_tokens": 500,
            "total_tokens": 1_500,
        },
    )

    assert cost["amount_usd"] is None
    assert cost["source"] == "pricing_table_no_match"
    assert "No matching provider/model row" in cost["note"]


def test_estimate_usage_cost_uses_local_pricing_table_for_gemini_flash() -> None:
    cost = estimate_usage_cost(
        provider="gemini",
        model="gemini-2.5-flash",
        usage={
            "available": True,
            "requests": 1,
            "input_tokens": 1_000,
            "cached_input_tokens": 200,
            "output_tokens": 500,
            "reasoning_output_tokens": 20,
            "total_tokens": 1_520,
        },
    )

    assert cost["source"] == "local_pricing_table"
    assert cost["pricing_model"] == "gemini-2.5-flash"
    assert cost["estimated_usd"] == 0.001546
    assert cost["billable_tokens"] == {
        "input_tokens": 800,
        "cached_input_tokens": 200,
        "output_tokens": 520,
    }


def test_agent_run_budget_defaults_to_twenty_five_cents(monkeypatch: Any) -> None:
    monkeypatch.delenv("KEYSTONE_AGENT_RUN_BUDGET_USD", raising=False)

    assert str(configured_agent_run_budget_usd()) == "0.25"


def test_agent_run_budget_guard_marks_exceeded_cost() -> None:
    guard = agent_run_budget_guard(
        cost={"source": "local_pricing_table", "estimated_usd": 0.251},
        budget_usd="0.25",
    )

    assert guard["enforced"] is True
    assert guard["enforceable"] is True
    assert guard["exceeded"] is True
    assert guard["status"] == "exceeded"


def test_agent_run_budget_can_require_known_cost_for_live_runs() -> None:
    with pytest.raises(AgentRunBudgetExceededError, match="could not be verified"):
        enforce_agent_run_budget(
            agent_name="Gmail Triage Agent",
            provider="openai",
            model="unknown-model",
            cost={"source": "pricing_table_no_match", "estimated_usd": None},
            budget_usd="0.25",
            strict_unknown_cost=True,
        )


def test_gemini_free_tier_usage_context_reports_request_day_limit() -> None:
    context = gemini_free_tier_usage_context(
        provider="gemini",
        model="gemini-2.5-flash",
        usage={"available": True, "requests": 2},
    )

    assert context["available"] is True
    assert context["model"] == "gemini-2.5-flash"
    assert context["requests_this_run"] == 2
    assert context["requests_observed_today"] == 2
    assert context["daily_usage_source"] == "current_run_only"
    assert context["requests_per_day_limit"] == 250
    assert context["requests_remaining_after_this_run"] == 248
    assert context["requests_remaining_today"] == 248
    assert context["percent_of_daily_request_limit"] == 0.8
    assert context["used_to_improve_products"] is True


def test_gemini_free_tier_usage_context_can_use_observed_daily_requests() -> None:
    context = gemini_free_tier_usage_context(
        provider="gemini",
        model="gemini-2.5-flash",
        usage={"available": True, "requests": 1},
        observed_daily_requests=12,
        daily_usage_source="local_sqlite_agent_runs_current_et_day",
    )

    assert context["requests_this_run"] == 1
    assert context["requests_observed_today"] == 12
    assert context["daily_usage_source"] == "local_sqlite_agent_runs_current_et_day"
    assert context["requests_remaining_today"] == 238
    assert context["percent_of_daily_request_limit"] == 4.8


def test_gemini_free_tier_usage_context_is_gemini_only() -> None:
    context = gemini_free_tier_usage_context(
        provider="openai",
        model="gpt-5.4-mini",
        usage={"available": True, "requests": 1},
    )

    assert context["available"] is False
    assert context["source"] == "unsupported_provider"


def test_openai_cost_window_requires_admin_key(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)

    cost_window = fetch_provider_cost_window(
        provider="openai",
        run_started_at=1000,
        run_ended_at=1010,
    )

    assert cost_window["available"] is False
    assert cost_window["source"] == "missing_openai_admin_key"


def test_openai_cost_window_uses_costs_api_without_returning_secret(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "data": [
                    {
                        "results": [
                            {"amount": {"value": 0.012345, "currency": "usd"}},
                            {"amount": {"value": 0.001, "currency": "usd"}},
                        ]
                    }
                ]
            }

    def fake_get(*args: Any, **kwargs: Any) -> FakeResponse:
        calls.append({"args": args, "kwargs": kwargs})
        return FakeResponse()

    monkeypatch.setattr("keystone_agents.costing.requests.get", fake_get)

    cost_window = fetch_provider_cost_window(
        provider="openai",
        run_started_at=1000,
        run_ended_at=1010,
        window_seconds=30,
        openai_project_id="proj_123",
        openai_admin_key="admin-secret",
    )

    assert cost_window["available"] is True
    assert cost_window["aggregate_amount_usd"] == 0.013345
    assert cost_window["exact_request_cost"] is False
    assert cost_window["project_id_filter"] == "proj_123"
    assert "admin-secret" not in str(cost_window)
    assert calls
    assert calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer admin-secret"
    assert ("project_ids[]", "proj_123") in calls[0]["kwargs"]["params"]


def test_openai_cost_window_failure_is_reported_not_raised(monkeypatch: Any) -> None:
    def fake_get(*_args: Any, **_kwargs: Any) -> Any:
        raise requests.Timeout("timed out")

    monkeypatch.setattr("keystone_agents.costing.requests.get", fake_get)

    raise_result = fetch_provider_cost_window(
        provider="openai",
        run_started_at=1000,
        run_ended_at=1010,
        openai_admin_key="admin-secret",
    )

    assert raise_result["source"] == "openai_costs_api_error"
    assert raise_result["error_type"] == "Timeout"
