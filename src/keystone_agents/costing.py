"""Usage cost estimation and optional provider billing lookups."""

from __future__ import annotations

import os
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

import requests

DEFAULT_PRICING_RESOURCE = "pricing/providers.json"
OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
OPENAI_ADMIN_KEY_ENV = "OPENAI_ADMIN_KEY"
AGENT_RUN_BUDGET_ENV = "KEYSTONE_AGENT_RUN_BUDGET_USD"
DEFAULT_AGENT_RUN_BUDGET_USD = Decimal("0.25")


class AgentRunBudgetExceededError(RuntimeError):
    """Raised when a completed SDK model call exceeds the configured run budget."""


def load_pricing_table(path: str | Path | None = None) -> dict[str, Any]:
    """Load the explicit local model pricing table."""

    if path:
        return _load_json(Path(path))
    resource = resources.files("keystone_agents").joinpath(DEFAULT_PRICING_RESOURCE)
    return _load_json(resource)


def estimate_usage_cost(
    *,
    provider: str,
    model: str,
    usage: dict[str, Any],
    pricing_table_path: str | Path | None = None,
) -> dict[str, Any]:
    """Estimate per-run model cost from response usage and the local pricing table."""

    if not usage.get("available"):
        return _cost_unavailable(
            source="usage_not_available",
            note="Provider response did not expose token usage for this SDK run.",
        )

    table = load_pricing_table(pricing_table_path)
    entry = _match_pricing_entry(table, provider=provider, model=model)
    if entry is None:
        return _cost_unavailable(
            source="pricing_table_no_match",
            note=(
                "No matching provider/model row was found in the local pricing table. "
                "Add an explicit pricing row before reporting an estimated dollar cost."
            ),
        )

    input_tokens = _int_value(usage.get("input_tokens"))
    cached_input_tokens = min(input_tokens, _int_value(usage.get("cached_input_tokens")))
    uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
    output_tokens = _int_value(usage.get("output_tokens"))
    reasoning_output_tokens = _int_value(usage.get("reasoning_output_tokens"))
    if str(provider).lower() == "gemini":
        output_tokens += reasoning_output_tokens

    input_rate = _decimal(entry.get("input_per_1m_usd"))
    cached_rate = _decimal(entry.get("cached_input_per_1m_usd"))
    output_rate = _decimal(entry.get("output_per_1m_usd"))
    input_usd = _token_cost(uncached_input_tokens, input_rate)
    cached_usd = _token_cost(cached_input_tokens, cached_rate)
    output_usd = _token_cost(output_tokens, output_rate)
    amount = input_usd + cached_usd + output_usd

    return {
        "amount_usd": _money_float(amount),
        "estimated_usd": _money_float(amount),
        "actual_usd": None,
        "currency": "USD",
        "source": "local_pricing_table",
        "confidence": "estimate",
        "pricing_provider": entry.get("provider", provider),
        "pricing_model": entry.get("model", model),
        "pricing_as_of": entry.get("as_of", table.get("as_of")),
        "pricing_source_url": entry.get("source_url", table.get("source_url", "")),
        "billable_tokens": {
            "input_tokens": uncached_input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
        },
        "components_usd": {
            "input": _money_float(input_usd),
            "cached_input": _money_float(cached_usd),
            "output": _money_float(output_usd),
        },
        "note": (
            "Estimated from provider response token usage and the checked-in pricing "
            "table. This is not an invoice record."
        ),
    }


def configured_agent_run_budget_usd(env: dict[str, str] | None = None) -> Decimal:
    """Return the centralized per-agent SDK run budget."""

    source = os.environ if env is None else env
    raw = str(source.get(AGENT_RUN_BUDGET_ENV) or "").strip()
    if not raw:
        return DEFAULT_AGENT_RUN_BUDGET_USD
    try:
        return max(Decimal("0"), Decimal(raw))
    except (InvalidOperation, ValueError):
        return DEFAULT_AGENT_RUN_BUDGET_USD


def agent_run_budget_guard(
    *,
    cost: dict[str, Any],
    budget_usd: Decimal | float | str | None = None,
) -> dict[str, Any]:
    """Return a structured per-run budget decision from estimated cost metadata."""

    budget = (
        configured_agent_run_budget_usd()
        if budget_usd is None
        else max(Decimal("0"), Decimal(str(budget_usd)))
    )
    amount = _cost_amount(cost)
    guard = {
        "budget_usd": _money_float(budget),
        "currency": "USD",
        "enforced": True,
        "cost_source": cost.get("source"),
        "estimated_usd": _money_float(amount) if amount is not None else None,
        "exceeded": False,
    }
    if amount is None:
        return {
            **guard,
            "enforceable": False,
            "status": "cost_unavailable",
            "note": (
                "Budget guard could not compare this run because provider usage or "
                "pricing metadata was unavailable."
            ),
        }
    exceeded = amount > budget
    return {
        **guard,
        "enforceable": True,
        "exceeded": exceeded,
        "status": "exceeded" if exceeded else "within_budget",
        "note": (
            "Estimated SDK run cost exceeded the configured per-agent budget."
            if exceeded
            else "Estimated SDK run cost is within the configured per-agent budget."
        ),
    }


def enforce_agent_run_budget(
    *,
    agent_name: str,
    provider: str,
    model: str,
    cost: dict[str, Any],
    budget_usd: Decimal | float | str | None = None,
    strict_unknown_cost: bool = False,
) -> dict[str, Any]:
    """Raise if an SDK model call exceeds the centralized per-agent run budget."""

    guard = agent_run_budget_guard(cost=cost, budget_usd=budget_usd)
    if strict_unknown_cost and not guard["enforceable"]:
        raise AgentRunBudgetExceededError(
            f"{agent_name} SDK run cost could not be verified against the configured "
            f"${guard['budget_usd']:.2f} per-agent budget for {provider}/{model}: "
            f"{guard['cost_source'] or 'cost unavailable'}."
        )
    if guard["exceeded"]:
        raise AgentRunBudgetExceededError(
            f"{agent_name} estimated SDK run cost ${guard['estimated_usd']:.6f} "
            f"exceeded the configured ${guard['budget_usd']:.2f} per-agent budget "
            f"for {provider}/{model}."
        )
    return guard


def gemini_free_tier_usage_context(
    *,
    provider: str,
    model: str,
    usage: dict[str, Any],
    pricing_table_path: str | Path | None = None,
    observed_daily_requests: int | None = None,
    daily_usage_source: str = "current_run_only",
) -> dict[str, Any]:
    """Return Gemini free-tier request/day context for one run when locally known."""

    normalized_provider = provider.strip().lower()
    if normalized_provider != "gemini":
        return {
            "available": False,
            "provider": normalized_provider or provider,
            "source": "unsupported_provider",
            "note": "Gemini free-tier request tracking applies only to Gemini models.",
        }

    table = load_pricing_table(pricing_table_path)
    entry = _match_pricing_entry(table, provider=provider, model=model)
    free_tier = entry.get("free_tier") if entry else None
    if not isinstance(free_tier, dict):
        return {
            "available": False,
            "provider": "gemini",
            "model": model,
            "source": "free_tier_metadata_unavailable",
            "note": "No local Gemini free-tier quota metadata is available for this model.",
        }

    request_count = _int_value(usage.get("requests"))
    observed_request_count = (
        max(0, int(observed_daily_requests))
        if observed_daily_requests is not None
        else request_count
    )
    requests_per_day = _int_value(free_tier.get("requests_per_day"))
    remaining_after_run = (
        max(0, requests_per_day - observed_request_count) if requests_per_day else None
    )
    percent_of_daily_limit = (
        _ratio_percent(observed_request_count, requests_per_day) if requests_per_day else None
    )
    return {
        "available": True,
        "provider": "gemini",
        "model": entry.get("model", model),
        "source": "local_pricing_table_free_tier_metadata",
        "requests_this_run": request_count,
        "requests_observed_today": observed_request_count,
        "daily_usage_source": daily_usage_source,
        "requests_per_day_limit": requests_per_day or None,
        "requests_remaining_after_this_run": remaining_after_run,
        "requests_remaining_today": remaining_after_run,
        "percent_of_daily_request_limit": percent_of_daily_limit,
        "requests_per_minute_limit": _int_value(free_tier.get("requests_per_minute")) or None,
        "tokens_per_minute_limit": _int_value(free_tier.get("tokens_per_minute")) or None,
        "input_output_tokens": free_tier.get("input_output_tokens"),
        "used_to_improve_products": bool(free_tier.get("used_to_improve_products")),
        "rate_limit_source_url": entry.get("rate_limit_source_url", ""),
        "pricing_source_url": entry.get("source_url", table.get("source_url", "")),
        "as_of": entry.get("as_of", table.get("as_of")),
        "note": free_tier.get(
            "note",
            "Gemini free-tier quota context is informational and not an invoice record.",
        ),
    }


def fetch_provider_cost_window(
    *,
    provider: str,
    run_started_at: float | None,
    run_ended_at: float | None,
    window_seconds: int = 600,
    openai_project_id: str | None = None,
    openai_admin_key: str | None = None,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Fetch an explicit provider admin cost window when supported and configured."""

    normalized_provider = provider.strip().lower()
    if normalized_provider != "openai":
        return {
            "available": False,
            "provider": normalized_provider or provider,
            "source": "unsupported_provider",
            "note": "Provider cost-window lookup is currently implemented only for OpenAI.",
        }
    api_key = openai_admin_key or os.getenv(OPENAI_ADMIN_KEY_ENV)
    if not api_key:
        return {
            "available": False,
            "provider": "openai",
            "source": "missing_openai_admin_key",
            "note": f"Set {OPENAI_ADMIN_KEY_ENV} to query the OpenAI organization Costs API.",
        }

    now = time.time()
    started = run_started_at or now
    ended = run_ended_at or now
    bounded_window = max(0, int(window_seconds))
    start_time = max(0, int(started) - bounded_window)
    end_time = max(start_time + 1, int(ended) + bounded_window)

    params: list[tuple[str, str | int]] = [
        ("start_time", start_time),
        ("end_time", end_time),
        ("bucket_width", "1d"),
        ("limit", 1),
    ]
    if openai_project_id:
        params.append(("project_ids[]", openai_project_id))

    try:
        response = requests.get(
            OPENAI_COSTS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            params=params,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {
            "available": False,
            "provider": "openai",
            "source": "openai_costs_api_error",
            "start_time": start_time,
            "end_time": end_time,
            "error_type": type(exc).__name__,
            "note": (
                "OpenAI Costs API lookup failed. The SDK run output and local cost "
                "estimate remain usable; retry the admin lookup separately if needed."
            ),
        }
    amount, currency, result_count = _sum_openai_cost_results(payload)
    return {
        "available": True,
        "provider": "openai",
        "source": "openai_organization_costs_api",
        "aggregate_amount_usd": _money_float(amount),
        "currency": currency.upper(),
        "start_time": start_time,
        "end_time": end_time,
        "bucket_width": "1d",
        "project_id_filter": openai_project_id,
        "result_count": result_count,
        "exact_request_cost": False,
        "note": (
            "OpenAI Costs API returns organization/project aggregate buckets, not exact "
            "per-request billing. Use this as a window check alongside per-run token "
            "estimates."
        ),
    }


def provider_cost_window_unqueried() -> dict[str, Any]:
    return {
        "available": False,
        "source": "not_queried",
        "note": "Provider admin cost-window lookup was not requested.",
    }


def _load_json(path: Any) -> dict[str, Any]:
    import json

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Pricing table must be a JSON object.")
    return data


def _match_pricing_entry(
    table: dict[str, Any],
    *,
    provider: str,
    model: str,
) -> dict[str, Any] | None:
    normalized_provider = provider.strip().lower()
    normalized_model = model.strip().lower()
    entries = [
        entry
        for entry in table.get("models", [])
        if isinstance(entry, dict)
        and str(entry.get("provider", "")).strip().lower() == normalized_provider
    ]
    entries.sort(key=lambda item: len(str(item.get("model", ""))), reverse=True)
    for entry in entries:
        row_model = str(entry.get("model", "")).strip().lower()
        aliases = [str(alias).strip().lower() for alias in entry.get("aliases", [])]
        candidates = [row_model, *aliases]
        for candidate in candidates:
            if normalized_model == candidate or normalized_model.startswith(f"{candidate}-"):
                return entry
    return None


def _cost_unavailable(*, source: str, note: str) -> dict[str, Any]:
    return {
        "amount_usd": None,
        "estimated_usd": None,
        "actual_usd": None,
        "currency": "USD",
        "source": source,
        "confidence": "not_available",
        "note": note,
    }


def _cost_amount(cost: dict[str, Any]) -> Decimal | None:
    for key in ("estimated_usd", "amount_usd", "actual_usd"):
        value = cost.get(key)
        if value is None:
            continue
        try:
            return max(Decimal("0"), Decimal(str(value)))
        except (InvalidOperation, ValueError):
            continue
    return None


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _token_cost(tokens: int, per_1m_rate: Decimal) -> Decimal:
    return (Decimal(tokens) * per_1m_rate) / Decimal("1000000")


def _money_float(value: Decimal) -> float:
    rounded = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    return float(rounded)


def _ratio_percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    rounded = (Decimal(numerator) * Decimal("100") / Decimal(denominator)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(rounded)


def _sum_openai_cost_results(payload: dict[str, Any]) -> tuple[Decimal, str, int]:
    total = Decimal("0")
    currency = "USD"
    result_count = 0
    for bucket in payload.get("data", []):
        if not isinstance(bucket, dict):
            continue
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            amount = result.get("amount") or {}
            if not isinstance(amount, dict):
                continue
            total += _decimal(amount.get("value"))
            currency = str(amount.get("currency") or currency)
            result_count += 1
    return total, currency, result_count
