"""Exa usage monitoring helpers for scheduled API usage reports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import requests
from pydantic import BaseModel, Field

from keystone_agents.config import load_settings

EXA_ADMIN_BASE_URL = "https://admin-api.exa.ai"
EXA_DASHBOARD_URL = "https://dashboard.exa.ai/home"
DEFAULT_EXA_MONTHLY_FREE_CREDIT_LIMIT = 1000
DEFAULT_EXA_MONTHLY_FREE_REQUEST_LIMIT = DEFAULT_EXA_MONTHLY_FREE_CREDIT_LIMIT
DEFAULT_EXA_USAGE_TIMEOUT_SECONDS = 10.0


class ExaUsageBreakdownItem(BaseModel):
    """One Exa usage/cost line item returned by the admin API."""

    price_id: str = ""
    price_name: str = ""
    quantity: int = 0
    amount_usd: float = 0.0


class ExaUsageSnapshot(BaseModel):
    """Dashboard-safe Exa usage status for weekly API usage reporting."""

    available: bool
    status: str
    source: str = "exa_admin_api"
    dashboard_url: str = EXA_DASHBOARD_URL
    api_key_id: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    total_cost_usd: float | None = None
    total_quantity_used: int = 0
    credits_used: int = 0
    search_requests_used: int = 0
    monthly_free_credit_limit: int = DEFAULT_EXA_MONTHLY_FREE_CREDIT_LIMIT
    monthly_free_request_limit: int = DEFAULT_EXA_MONTHLY_FREE_REQUEST_LIMIT
    estimated_free_credits_remaining: int | None = None
    estimated_free_requests_remaining: int | None = None
    cost_breakdown: list[ExaUsageBreakdownItem] = Field(default_factory=list)
    note: str = ""
    error: str | None = None


def exa_usage_snapshot(
    *,
    api_key_id: str | None = None,
    api_key_name: str | None = None,
    service_api_key: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    monthly_free_request_limit: int | None = None,
    admin_base_url: str = EXA_ADMIN_BASE_URL,
    timeout_seconds: float = DEFAULT_EXA_USAGE_TIMEOUT_SECONDS,
    http_get: Callable[..., Any] | None = None,
) -> ExaUsageSnapshot:
    """Fetch Exa usage, returning an explicit unavailable snapshot on setup gaps."""

    settings = load_settings()
    resolved_api_key_id = api_key_id or settings.exa_api_key_id
    resolved_api_key_name = api_key_name or settings.exa_api_key_name
    resolved_service_key = service_api_key or settings.exa_service_api_key
    resolved_limit = (
        monthly_free_request_limit
        if monthly_free_request_limit is not None
        else settings.exa_monthly_free_request_limit
    )
    if not resolved_api_key_id and resolved_service_key and resolved_api_key_name:
        try:
            resolved_api_key_id = _resolve_exa_api_key_id_by_name(
                api_key_name=resolved_api_key_name,
                service_api_key=resolved_service_key,
                admin_base_url=admin_base_url,
                timeout_seconds=timeout_seconds,
                http_get=http_get,
            )
        except Exception as exc:
            return ExaUsageSnapshot(
                available=False,
                status="error",
                monthly_free_request_limit=max(0, int(resolved_limit)),
                note=(
                    f"Could not resolve Exa API key named {resolved_api_key_name!r}; "
                    "dashboard review is required for authoritative balance."
                ),
                error=f"{type(exc).__name__}: {exc}",
            )

    if not resolved_api_key_id or not resolved_service_key:
        missing = [
            name
            for name, value in (
                ("EXA_API_KEY_ID", resolved_api_key_id),
                ("EXA_API_KEY_NAME", resolved_api_key_name),
                ("EXA_SERVICE_API_KEY", resolved_service_key),
            )
            if not value
        ]
        return ExaUsageSnapshot(
            available=False,
            status="missing_configuration",
            monthly_free_request_limit=max(0, int(resolved_limit)),
            note=(
                "Exa usage monitoring requires "
                + ", ".join(missing)
                + ". Check the dashboard for authoritative remaining credits."
            ),
        )

    period_start = start_date or _current_month_start_utc()
    period_end = end_date or datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    try:
        data = _fetch_exa_usage(
            api_key_id=resolved_api_key_id,
            service_api_key=resolved_service_key,
            start_date=period_start,
            end_date=period_end,
            admin_base_url=admin_base_url,
            timeout_seconds=timeout_seconds,
            http_get=http_get,
        )
    except Exception as exc:
        return ExaUsageSnapshot(
            available=False,
            status="error",
            api_key_id=resolved_api_key_id,
            period_start=period_start,
            period_end=period_end,
            monthly_free_request_limit=max(0, int(resolved_limit)),
            note="Exa usage API failed; dashboard review is required for authoritative balance.",
            error=f"{type(exc).__name__}: {exc}",
        )

    breakdown = _usage_breakdown(data)
    search_credits = _search_credit_quantity(breakdown)
    total_quantity = sum(item.quantity for item in breakdown)
    limit = max(0, int(resolved_limit))
    return ExaUsageSnapshot(
        available=True,
        status="ok",
        api_key_id=str(data.get("api_key_id") or resolved_api_key_id),
        period_start=str((data.get("period") or {}).get("start") or period_start),
        period_end=str((data.get("period") or {}).get("end") or period_end),
        total_cost_usd=_float_or_none(data.get("total_cost_usd")),
        total_quantity_used=total_quantity,
        credits_used=search_credits,
        search_requests_used=search_credits,
        monthly_free_credit_limit=limit,
        monthly_free_request_limit=limit,
        estimated_free_credits_remaining=max(0, limit - search_credits),
        estimated_free_requests_remaining=max(0, limit - search_credits),
        cost_breakdown=breakdown,
        note=(
            "Remaining free-tier credits are estimated from Exa usage quantities. "
            "Use the Exa dashboard for authoritative credit balance."
        ),
    )


def _fetch_exa_usage(
    *,
    api_key_id: str,
    service_api_key: str,
    start_date: str,
    end_date: str,
    admin_base_url: str,
    timeout_seconds: float,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    get = http_get or requests.get
    response = get(
        urljoin(
            f"{admin_base_url.rstrip('/')}/",
            f"team-management/api-keys/{api_key_id}/usage",
        ),
        headers={"x-api-key": service_api_key},
        params={"start_date": start_date, "end_date": end_date, "group_by": "day"},
        timeout=timeout_seconds,
    )
    status_code = getattr(response, "status_code", None)
    if status_code is not None and status_code >= 400:
        raise RuntimeError(f"Exa usage request failed with HTTP {status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Exa usage request returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Exa usage request returned invalid JSON.")
    return data


def _resolve_exa_api_key_id_by_name(
    *,
    api_key_name: str,
    service_api_key: str,
    admin_base_url: str,
    timeout_seconds: float,
    http_get: Callable[..., Any] | None = None,
) -> str:
    get = http_get or requests.get
    response = get(
        urljoin(f"{admin_base_url.rstrip('/')}/", "team-management/api-keys"),
        headers={"x-api-key": service_api_key},
        timeout=timeout_seconds,
    )
    status_code = getattr(response, "status_code", None)
    if status_code is not None and status_code >= 400:
        raise RuntimeError(f"Exa API key list request failed with HTTP {status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Exa API key list request returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Exa API key list request returned invalid JSON.")
    matches = [
        str(item.get("id") or "").strip()
        for item in data.get("apiKeys") or []
        if isinstance(item, dict)
        and str(item.get("name") or "").strip().lower() == api_key_name.strip().lower()
        and str(item.get("id") or "").strip()
    ]
    if not matches:
        raise RuntimeError(f"No Exa API key named {api_key_name!r} was found.")
    if len(matches) > 1:
        raise RuntimeError(f"Multiple Exa API keys named {api_key_name!r} were found.")
    return matches[0]


def _usage_breakdown(data: dict[str, Any]) -> list[ExaUsageBreakdownItem]:
    items: list[ExaUsageBreakdownItem] = []
    for item in data.get("cost_breakdown") or []:
        if not isinstance(item, dict):
            continue
        items.append(
            ExaUsageBreakdownItem(
                price_id=str(item.get("price_id") or ""),
                price_name=str(item.get("price_name") or ""),
                quantity=_int_or_zero(item.get("quantity")),
                amount_usd=_float_or_none(item.get("amount_usd")) or 0.0,
            )
        )
    return items


def _search_credit_quantity(items: list[ExaUsageBreakdownItem]) -> int:
    search_items = [
        item.quantity for item in items if "search" in f"{item.price_id} {item.price_name}".lower()
    ]
    return sum(search_items) if search_items else sum(item.quantity for item in items)


def _current_month_start_utc() -> str:
    now = datetime.now(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_EXA_MONTHLY_FREE_REQUEST_LIMIT",
    "EXA_DASHBOARD_URL",
    "ExaUsageBreakdownItem",
    "ExaUsageSnapshot",
    "exa_usage_snapshot",
]
