"""One-purpose, cost-bounded Chief synthesis runner for weekly operations packets."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from keystone_agents.agents.chief_of_staff import run_chief_of_staff_sdk
from keystone_agents.model_provider import OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.quality_budget import AgentQualityBudget, QualityMode
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput
from keystone_agents.sdk import private_context_sdk_profile
from keystone_agents.weekly_ops_packet import (
    build_weekly_ops_external_synthesis_bundle,
    build_weekly_ops_source_bundle,
)

WEEKLY_PACKET_MODEL = OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL
WEEKLY_PACKET_MAX_REQUESTS = 1
WEEKLY_PACKET_MAX_COST_USD = Decimal("0.05")
WEEKLY_PACKET_WORKFLOW_NAME = "Chief Prior Week Packet"


def weekly_ops_live_run_plan(payload: WeeklyOpsAssemblyInput) -> dict[str, Any]:
    bundle = build_weekly_ops_source_bundle(payload)
    return {
        "schema": "keystone.weekly_ops.live_run_plan.v1",
        "workflow_name": WEEKLY_PACKET_WORKFLOW_NAME,
        "agent": "chief_of_staff",
        "model": WEEKLY_PACKET_MODEL,
        "max_openai_requests": WEEKLY_PACKET_MAX_REQUESTS,
        "max_cost_usd": str(WEEKLY_PACKET_MAX_COST_USD),
        "live_search": False,
        "specialist_tools": False,
        "provider_writes": False,
        "google_doc_create": False,
        "slack_post": False,
        "packet_title": bundle["delivery_plan"]["document_title"],
        "synthesis_ready": bundle["synthesis_ready"],
        "data_handling": {
            "response_store": False,
            "prompt_cache_retention": "in_memory",
            "tracing_disabled": True,
            "trace_include_sensitive_data": False,
        },
    }


def run_weekly_ops_packet_synthesis(
    payload: WeeklyOpsAssemblyInput,
    *,
    live_sdk: bool = False,
    max_openai_requests: int = WEEKLY_PACKET_MAX_REQUESTS,
    max_cost_usd: Decimal = WEEKLY_PACKET_MAX_COST_USD,
    approved_external_business_synthesis: bool = False,
    personal_redaction_terms: tuple[str, ...] = (),
    runner: Callable[..., TypedAgentRunResult[ChiefOfStaffResult]] = run_chief_of_staff_sdk,
) -> dict[str, Any]:
    """Validate offline by default or run exactly one no-tool Chief SDK turn."""

    _validate_live_limits(
        live_sdk=live_sdk,
        max_openai_requests=max_openai_requests,
        max_cost_usd=max_cost_usd,
    )
    bundle = build_weekly_ops_source_bundle(payload)
    if not bundle["synthesis_ready"]:
        raise ValueError("Weekly operations packet is not synthesis-ready.")
    plan = weekly_ops_live_run_plan(payload)
    if not live_sdk:
        return {"status": "validated_offline", "plan": plan, "bundle": bundle}
    if not approved_external_business_synthesis:
        raise ValueError(
            "Live weekly synthesis requires explicit approval for external non-personal "
            "business-data synthesis."
        )
    external_bundle = build_weekly_ops_external_synthesis_bundle(
        payload,
        personal_redaction_terms=personal_redaction_terms,
    )
    if not external_bundle["synthesis_ready"]:
        raise ValueError("External weekly operations bundle is not synthesis-ready.")

    date_min = external_bundle["window"]["date_min"]
    date_max = external_bundle["window"]["date_max"]
    request = (
        "Prepare the internal KNI weekly operations packet from only the supplied bounded "
        f"{date_min} through {date_max} source bundle. Follow section_order exactly, using "
        "clear human-readable headings corresponding to every section. Prioritize "
        "one-time Calendar "
        "events, mention recurring cadence briefly, include only explicitly relevant completed "
        "agent runs, and keep operational health and metadata succinct at the end. Return a "
        "review-only packet. Do not search, call tools, create a Google Doc, post to Slack, "
        "send, schedule, publish, or write any provider."
    )
    sdk_input = {
        "request": request,
        "weekly_ops_source_bundle": external_bundle,
        "include_specialist_tools": False,
        "specialist_tool_mode": "read_plan",
        "manual_request_plan": {
            "source": "weekly_ops_runner",
            "target_agent": "chief_of_staff",
            "intent": "portfolio_review",
            "primary_target": bundle["target"]["name"],
            "task_objective": "portfolio_summary",
        },
    }
    budget = AgentQualityBudget(
        agent_name="chief_of_staff",
        mode=QualityMode.FAST,
        max_turns=1,
        reasoning_effort="low",
        verbosity="low",
        max_tokens=2500,
        max_review_passes=0,
        max_tool_calls=0,
        max_seconds=60,
        tool_tier="core_read",
        enable_synthesis_review=False,
        enable_context_deepening=False,
        allow_manager_loop_repair=False,
        notes=["One-request weekly packet synthesis; no tools or writes."],
    )
    with private_context_sdk_profile() as data_profile:
        result = runner(
            sdk_input,
            live=True,
            model=WEEKLY_PACKET_MODEL,
            quality_budget=budget,
            force_sdk_interpretation=True,
            include_specialist_tools=False,
            attach_tools=False,
        )
    _validate_live_result(
        result,
        max_openai_requests=max_openai_requests,
        max_cost_usd=max_cost_usd,
    )
    return {
        "status": "success",
        "plan": plan,
        "packet": result.output.model_dump(mode="json"),
        "usage": dict(result.usage or {}),
        "cost": dict(result.cost or {}),
        "request_cache": dict(result.request_cache or {}),
        "data_handling": data_profile.audit_metadata(),
        "request_count_bound": max_openai_requests,
        "provider_writes": False,
        "send_enabled": False,
    }


def _validate_live_limits(
    *,
    live_sdk: bool,
    max_openai_requests: int,
    max_cost_usd: Decimal,
) -> None:
    if max_openai_requests != WEEKLY_PACKET_MAX_REQUESTS:
        raise ValueError("Weekly packet synthesis requires max_openai_requests=1.")
    if max_cost_usd > WEEKLY_PACKET_MAX_COST_USD or max_cost_usd <= 0:
        raise ValueError("Weekly packet synthesis requires a cost ceiling from $0.01 to $0.05.")
    if not live_sdk:
        return


def _validate_live_result(
    result: TypedAgentRunResult[ChiefOfStaffResult],
    *,
    max_openai_requests: int,
    max_cost_usd: Decimal,
) -> None:
    output = result.output
    if output.send_enabled or output.slack_post_allowed or output.write_requests:
        raise RuntimeError("Weekly packet synthesis returned an unexpected side-effect request.")
    packet_text = "\n".join(
        value
        for value in (str(output.synthesis or "").strip(), str(output.summary or "").strip())
        if value
    )
    _validate_packet_sections(packet_text)
    usage = dict(result.usage or {})
    if not usage:
        raise RuntimeError("Weekly packet synthesis is missing SDK usage evidence.")
    observed_requests = _numeric_value(usage, "requests", "request_count", "total_requests")
    if observed_requests != max_openai_requests:
        raise RuntimeError("Weekly packet synthesis must use exactly one OpenAI request.")
    cost = dict(result.cost or {})
    observed_cost = _numeric_value(cost, "estimated_usd", "total_usd", "actual_usd")
    if observed_cost is None:
        raise RuntimeError("Weekly packet synthesis is missing cost evidence.")
    if Decimal(str(observed_cost)) > max_cost_usd:
        raise RuntimeError("Weekly packet synthesis exceeded the approved cost ceiling.")


def _numeric_value(payload: dict[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    return None


def _validate_packet_sections(packet_text: str) -> None:
    normalized = " ".join(packet_text.casefold().replace("_", " ").split())
    section_aliases = (
        ("executive focus areas",),
        ("workstreams and decisions",),
        ("completed runs and outcomes", "completed agent runs"),
        ("carry forward", "carry-forward"),
        ("non recurring calendar", "non-recurring calendar", "one-time calendar"),
        ("recurring calendar cadence", "recurring cadence"),
        ("next actions",),
        ("source basis", "sources"),
        ("operational health",),
        ("packet metadata",),
    )
    missing = [
        aliases[0]
        for aliases in section_aliases
        if not any(alias in normalized for alias in aliases)
    ]
    if missing:
        raise RuntimeError(
            "Weekly packet synthesis omitted required sections: " + ", ".join(missing)
        )
    health_index = normalized.rfind("operational health")
    metadata_index = normalized.rfind("packet metadata")
    if health_index < 0 or metadata_index <= health_index:
        raise RuntimeError("Weekly packet terminal sections are out of order.")
