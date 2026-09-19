"""Central SDK run-loop policy for direct specialist wrappers."""

from __future__ import annotations

import os
from dataclasses import dataclass

from keystone_agents.quality_budget import (
    business_research_quality_budget,
    opportunity_scout_quality_budget,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


@dataclass(frozen=True)
class SDKTurnPolicy:
    """Resolved max-turn policy for one SDK run."""

    agent_name: str
    max_turns: int
    source: str

    def metadata(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "max_turns": self.max_turns,
            "source": self.source,
        }


_FIXED_DEFAULT_MAX_TURNS: dict[str, int] = {
    "gmail_triage": 4,
    "gmail_priority_grouping": 4,
    "outreach_composer": 4,
}


def resolve_sdk_turn_policy(
    agent_name: str,
    *,
    request_text: str = "",
    live_search: bool = False,
    cost_profile: str = "standard",
    manual_request_plan: ManualRequestPlan | dict[str, object] | None = None,
    explicit_max_turns: int | None = None,
    formal_opportunity: bool = False,
    source_context_required: bool = False,
) -> SDKTurnPolicy:
    """Resolve the direct-wrapper max-turn budget for a specialist SDK run."""

    normalized = _agent_key(agent_name)
    if explicit_max_turns is not None:
        return SDKTurnPolicy(
            agent_name=normalized,
            max_turns=_validate_max_turns(explicit_max_turns),
            source="explicit",
        )
    env_override = _env_max_turns(normalized)
    if env_override is not None:
        return SDKTurnPolicy(
            agent_name=normalized,
            max_turns=env_override,
            source="env",
        )
    if normalized == "business_research_analyst":
        budget = business_research_quality_budget(
            request_text=request_text,
            live_search=live_search,
            cost_profile=cost_profile,
            manual_request_plan=manual_request_plan,
        )
        return SDKTurnPolicy(
            agent_name=normalized,
            max_turns=_validate_max_turns(budget.max_turns),
            source=f"quality_budget:{budget.mode.value}",
        )
    if normalized == "opportunity_scout":
        budget = opportunity_scout_quality_budget(
            request_text=request_text,
            live_search=live_search,
            cost_profile=cost_profile,
            formal_opportunity=formal_opportunity,
            source_context_required=source_context_required,
            manual_request_plan=manual_request_plan,
        )
        return SDKTurnPolicy(
            agent_name=normalized,
            max_turns=_validate_max_turns(budget.max_turns),
            source=f"quality_budget:{budget.mode.value}",
        )
    fixed = _FIXED_DEFAULT_MAX_TURNS.get(normalized, 6)
    return SDKTurnPolicy(agent_name=normalized, max_turns=fixed, source="fixed_default")


def resolve_sdk_tool_call_limit(
    agent_name: str,
    *,
    request_text: str = "",
    live_search: bool = False,
    cost_profile: str = "standard",
    manual_request_plan: ManualRequestPlan | dict[str, object] | None = None,
    formal_opportunity: bool = False,
    source_context_required: bool = False,
) -> int | None:
    """Resolve the quality budget's real total function-tool ceiling."""

    normalized = _agent_key(agent_name)
    if normalized == "business_research_analyst":
        return business_research_quality_budget(
            request_text=request_text,
            live_search=live_search,
            cost_profile=cost_profile,
            manual_request_plan=manual_request_plan,
        ).max_tool_calls
    if normalized == "opportunity_scout":
        return opportunity_scout_quality_budget(
            request_text=request_text,
            live_search=live_search,
            cost_profile=cost_profile,
            formal_opportunity=formal_opportunity,
            source_context_required=source_context_required,
            manual_request_plan=manual_request_plan,
        ).max_tool_calls
    return None


def _agent_key(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _env_max_turns(agent_name: str) -> int | None:
    keys = (
        f"KEYSTONE_{agent_name.upper()}_SDK_MAX_TURNS",
        "KEYSTONE_SDK_MAX_TURNS",
    )
    for key in keys:
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        return _validate_max_turns(raw)
    return None


def _validate_max_turns(value: int | str | None) -> int:
    try:
        parsed = int(value if value is not None else 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("SDK max_turns must be a positive integer.") from exc
    if parsed <= 0:
        raise ValueError("SDK max_turns must be a positive integer.")
    return parsed
