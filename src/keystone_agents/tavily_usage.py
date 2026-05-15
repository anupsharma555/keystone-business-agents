"""Local Tavily credit tracking and budget context."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from keystone_agents.config import runtime_state_dir

DEFAULT_TAVILY_MONTHLY_CREDIT_LIMIT = 1000
DEFAULT_TAVILY_MONTHLY_SOFT_LIMIT_RATIO = 0.85

_LOCK = RLock()


@dataclass(frozen=True)
class TavilyCreditDecision:
    """Budget decision for one Tavily request."""

    available: bool
    allowed: bool
    status: str
    enforcement: str
    month: str
    monthly_credit_limit: int
    monthly_soft_limit: int
    observed_monthly_credits: int
    request_credits: int
    projected_monthly_credits: int
    projected_monthly_remaining: int
    usage_path: str | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def tavily_search_credit_estimate(search_depth: str | None) -> int:
    """Return Tavily Search API credit cost for a request depth."""

    return 2 if str(search_depth or "").strip().lower() == "advanced" else 1


def tavily_credit_preflight(estimated_credits: int) -> TavilyCreditDecision:
    """Return whether a Tavily request should proceed under local budget policy."""

    config = _budget_config()
    if config["enforcement"] == "off":
        return TavilyCreditDecision(
            available=False,
            allowed=True,
            status="tracking_disabled",
            enforcement="off",
            month=_current_month(),
            monthly_credit_limit=config["monthly_credit_limit"],
            monthly_soft_limit=config["monthly_soft_limit"],
            observed_monthly_credits=0,
            request_credits=max(0, estimated_credits),
            projected_monthly_credits=max(0, estimated_credits),
            projected_monthly_remaining=config["monthly_credit_limit"],
            usage_path=None,
            note="Tavily credit tracking is disabled.",
        )

    observed = _read_monthly_credits(config["usage_path"], month=_current_month())
    return _decision_from_observed(
        observed_monthly_credits=observed,
        request_credits=max(0, estimated_credits),
        config=config,
        recorded=False,
    )


def record_tavily_credit_usage(
    *,
    credits: int,
    estimated_credits: int,
    usage_source: str,
) -> TavilyCreditDecision:
    """Record one Tavily request and return updated monthly budget context."""

    config = _budget_config()
    request_credits = max(0, int(credits or estimated_credits or 0))
    if config["enforcement"] == "off":
        return TavilyCreditDecision(
            available=False,
            allowed=True,
            status="tracking_disabled",
            enforcement="off",
            month=_current_month(),
            monthly_credit_limit=config["monthly_credit_limit"],
            monthly_soft_limit=config["monthly_soft_limit"],
            observed_monthly_credits=request_credits,
            request_credits=request_credits,
            projected_monthly_credits=request_credits,
            projected_monthly_remaining=max(0, config["monthly_credit_limit"] - request_credits),
            usage_path=None,
            note="Tavily credit tracking is disabled.",
        )

    month = _current_month()
    observed = _increment_monthly_credits(
        config["usage_path"],
        month=month,
        credits=request_credits,
        usage_source=usage_source,
    )
    return _decision_from_observed(
        observed_monthly_credits=max(0, observed - request_credits),
        request_credits=request_credits,
        config=config,
        recorded=True,
    )


def tavily_credit_budget_snapshot() -> dict[str, Any]:
    """Return local Tavily monthly budget state without mutating it."""

    config = _budget_config()
    observed = 0
    if config["enforcement"] != "off":
        observed = _read_monthly_credits(config["usage_path"], month=_current_month())
    decision = _decision_from_observed(
        observed_monthly_credits=observed,
        request_credits=0,
        config=config,
        recorded=False,
    )
    return decision.to_dict()


def _decision_from_observed(
    *,
    observed_monthly_credits: int,
    request_credits: int,
    config: dict[str, Any],
    recorded: bool,
) -> TavilyCreditDecision:
    monthly_limit = int(config["monthly_credit_limit"])
    soft_limit = int(config["monthly_soft_limit"])
    projected = observed_monthly_credits + request_credits
    remaining = max(0, monthly_limit - projected)
    status = "ok"
    note = "Tavily monthly credit usage is within the configured budget."
    if projected > monthly_limit:
        status = "over_monthly_limit"
        note = "Tavily monthly credit usage is above the configured limit."
    elif projected >= soft_limit:
        status = "near_monthly_limit"
        note = "Tavily monthly credit usage is near the configured soft limit."
    allowed = config["enforcement"] != "block" or status != "over_monthly_limit"
    if recorded:
        observed_monthly_credits = projected
    return TavilyCreditDecision(
        available=config["usage_path"] is not None,
        allowed=allowed,
        status=status,
        enforcement=str(config["enforcement"]),
        month=_current_month(),
        monthly_credit_limit=monthly_limit,
        monthly_soft_limit=soft_limit,
        observed_monthly_credits=observed_monthly_credits,
        request_credits=request_credits,
        projected_monthly_credits=projected,
        projected_monthly_remaining=remaining,
        usage_path=str(config["usage_path"]) if config["usage_path"] is not None else None,
        note=note,
    )


def _budget_config() -> dict[str, Any]:
    monthly_limit = _env_int(
        "KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT",
        DEFAULT_TAVILY_MONTHLY_CREDIT_LIMIT,
    )
    monthly_limit = max(0, monthly_limit)
    soft_default = int(monthly_limit * DEFAULT_TAVILY_MONTHLY_SOFT_LIMIT_RATIO)
    soft_limit = _env_int("KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT", soft_default)
    soft_limit = max(0, min(monthly_limit, soft_limit))
    enforcement = os.getenv("KEYSTONE_TAVILY_CREDIT_ENFORCEMENT", "warn").strip().lower()
    if enforcement not in {"warn", "block", "off"}:
        enforcement = "warn"
    return {
        "monthly_credit_limit": monthly_limit,
        "monthly_soft_limit": soft_limit,
        "enforcement": enforcement,
        "usage_path": _usage_path() if enforcement != "off" else None,
    }


def _usage_path() -> Path | None:
    explicit = os.getenv("KEYSTONE_TAVILY_USAGE_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if _env_bool("KEYSTONE_TEST_MODE", default=False):
        return None
    state_dir = runtime_state_dir()
    if state_dir is not None:
        return state_dir / "tavily_credit_usage.json"
    return Path.cwd() / ".keystone" / "state" / "tavily_credit_usage.json"


def _read_monthly_credits(path: Path | None, *, month: str) -> int:
    if path is None:
        return 0
    with _LOCK:
        data = _read_usage_file(path)
        months = data.get("months") if isinstance(data, dict) else {}
        record = months.get(month) if isinstance(months, dict) else {}
        if not isinstance(record, dict):
            return 0
        return _int_value(record.get("credits"))


def _increment_monthly_credits(
    path: Path | None,
    *,
    month: str,
    credits: int,
    usage_source: str,
) -> int:
    if path is None:
        return credits
    with _LOCK:
        data = _read_usage_file(path)
        months = data.setdefault("months", {})
        if not isinstance(months, dict):
            months = {}
            data["months"] = months
        record = months.setdefault(month, {})
        if not isinstance(record, dict):
            record = {}
            months[month] = record
        observed = _int_value(record.get("credits")) + credits
        record["credits"] = observed
        record["request_count"] = _int_value(record.get("request_count")) + 1
        record["last_usage_source"] = usage_source
        record["updated_at"] = datetime.now(UTC).isoformat()
        data["updated_at"] = record["updated_at"]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True), "utf-8")
        except OSError:
            return observed
        return observed


def _read_usage_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"months": {}}
    except OSError:
        return {"months": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"months": {}}
    return data if isinstance(data, dict) else {"months": {}}


def _current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _int_value(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "DEFAULT_TAVILY_MONTHLY_CREDIT_LIMIT",
    "TavilyCreditDecision",
    "record_tavily_credit_usage",
    "tavily_credit_budget_snapshot",
    "tavily_credit_preflight",
    "tavily_search_credit_estimate",
]
