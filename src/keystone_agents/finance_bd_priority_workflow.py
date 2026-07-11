"""Provider-backed finance context to bounded Chief BD-priority decision."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from keystone_agents.agents.chief_of_staff import run_chief_of_staff_sdk
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.quality_budget import AgentQualityBudget, QualityMode
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.sdk import private_context_sdk_profile

MAX_OPENAI_REQUESTS = 1
MAX_COST_USD = Decimal("0.05")


@dataclass(frozen=True)
class BusinessDevelopmentOption:
    option_id: str
    title: str
    description: str


@dataclass(frozen=True)
class FinanceBDPriorityPacket:
    finance_summary: str
    finance_receipt: dict[str, Any]
    options: tuple[BusinessDevelopmentOption, ...]


def collect_current_quarter_finance_packet(
    *,
    options: tuple[BusinessDevelopmentOption, ...],
    live: bool,
    finance_runner: Callable[..., TypedAgentRunResult[ChiefOfStaffResult]] = (
        run_chief_of_staff_sdk
    ),
) -> FinanceBDPriorityPacket:
    """Read and aggregate the current quarter without invoking a model."""

    if not live:
        raise ValueError("Current-quarter finance packet requires live Airtable reads.")
    _validate_options(options)
    request = (
        "Chief of Staff: read finance_tax_tracker and calculate total income and total "
        "expenses for the current quarter, including expense categories and any "
        "uncategorized gap. Read-only; "
        "do not search, write, send, post, publish, or make tax decisions."
    )
    result = finance_runner(
        request,
        live=True,
        force_sdk_interpretation=False,
        manual_request_plan={
            "source": "l174_19_finance_packet",
            "target_agent": "chief_of_staff",
            "intent": "business_system_context",
            "primary_target": "finance_tax_tracker current quarter",
        },
    )
    output = result.output
    summary = str(output.summary or "").strip()
    checks = {
        "live": bool(result.live),
        "deterministic_mode": output.mode == "deterministic",
        "finance_handler": result.raw_result == {"deterministic": "finance_tax_tracker"},
        "summary_present": bool(summary),
        "quarter_visible": "quarter" in summary.casefold()
        or any(f"q{quarter}" in summary.casefold() for quarter in range(1, 5)),
        "income_visible": "income" in summary.casefold(),
        "expense_visible": "expense" in summary.casefold(),
        "no_send": not output.send_enabled,
        "no_write_request": not output.write_requests,
        "controlled_failure": "could not complete" in summary.casefold(),
    }
    if not all(value for key, value in checks.items() if key != "controlled_failure") or checks[
        "controlled_failure"
    ]:
        if checks["controlled_failure"]:
            raise RuntimeError(
                f"Current-quarter finance provider aggregate failed: {summary[:300]}"
            )
        failed = sorted(
            key for key, value in checks.items() if key != "controlled_failure" and not value
        )
        raise RuntimeError(
            "Current-quarter finance provider aggregate was not verified; failed checks: "
            + ", ".join(sorted(set(failed)))
            + f"; provider summary: {summary[:300]}"
        )
    return FinanceBDPriorityPacket(
        finance_summary=summary,
        finance_receipt={
            "schema": "keystone.finance_context.receipt.v1",
            "provider": "airtable_context_agent",
            "base_alias": "finance_tax_tracker",
            "period": "current_quarter",
            "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
            "summary_character_count": len(summary),
            "provider_read_verified": True,
            "deterministic_arithmetic": True,
            "raw_records_persisted": False,
            "provider_writes": 0,
            "send_enabled": False,
        },
        options=options,
    )


def finance_bd_priority_plan(packet: FinanceBDPriorityPacket) -> dict[str, Any]:
    return {
        "schema": "keystone.finance_bd_priority.plan.v1",
        "finance_receipt": packet.finance_receipt,
        "option_ids": [option.option_id for option in packet.options],
        "option_content_hashes": [
            hashlib.sha256(
                f"{option.option_id}\n{option.title}\n{option.description}".encode()
            ).hexdigest()
            for option in packet.options
        ],
        "manager": "chief_of_staff",
        "downstream_owner": "opportunity_scout",
        "max_openai_requests": MAX_OPENAI_REQUESTS,
        "max_cost_usd": str(MAX_COST_USD),
        "model_tools_attached": False,
        "live_search": False,
        "provider_writes": False,
        "send_enabled": False,
    }


def execute_finance_bd_priority_decision(
    packet: FinanceBDPriorityPacket,
    *,
    live_sdk: bool,
    approved_private_context: bool = False,
    decision_runner: Callable[..., TypedAgentRunResult[ChiefOfStaffResult]] = (
        run_chief_of_staff_sdk
    ),
) -> dict[str, Any]:
    """Run one no-tool Chief decision and emit a sanitized specialist handoff."""

    _validate_options(packet.options)
    plan = finance_bd_priority_plan(packet)
    if not live_sdk:
        return {"status": "validated_offline", "plan": plan}
    if not approved_private_context:
        raise ValueError("Finance-aware Chief decision requires private-context approval.")

    request = (
        "Use the supplied current-quarter finance aggregate before choosing exactly one "
        "business-development priority. Explain how the finance constraints affect the "
        "choice, compare both supplied options, and hand the selected option to Opportunity "
        "Scout for read-only validation. Do not search, call tools, write, send, post, or "
        "publish. Do not provide tax advice."
    )
    sdk_input = {
        "request": request,
        "finance_context": packet.finance_summary,
        "business_development_options": [
            {
                "option_id": option.option_id,
                "title": option.title,
                "description": option.description,
            }
            for option in packet.options
        ],
        "manual_request_plan": {
            "source": "l174_19_finance_bd_priority",
            "target_agent": "chief_of_staff",
            "intent": "internal_review_handoff",
            "primary_target": "next business-development priority",
        },
        "include_specialist_tools": False,
        "specialist_tool_mode": "read_plan",
    }
    budget = AgentQualityBudget(
        agent_name="chief_of_staff",
        mode=QualityMode.FAST,
        max_turns=1,
        reasoning_effort="low",
        verbosity="low",
        max_tokens=1600,
        max_review_passes=0,
        max_tool_calls=0,
        max_seconds=60,
        tool_tier="core_read",
        enable_synthesis_review=False,
        enable_context_deepening=False,
        allow_manager_loop_repair=False,
        notes=["One finance-aware BD priority decision; supplied context only."],
    )
    with private_context_sdk_profile() as data_profile:
        result = decision_runner(
            sdk_input,
            live=True,
            force_sdk_interpretation=True,
            include_specialist_tools=False,
            attach_tools=False,
            quality_budget=budget,
        )
    selected = _validate_decision(result, packet)
    return {
        "status": "passed",
        "plan": plan,
        "selected_option_id": selected.option_id,
        "decision_sha256": hashlib.sha256(_decision_text(result.output).encode()).hexdigest(),
        "finance_basis_visible": True,
        "both_options_considered": True,
        "specialist_receipts": [
            packet.finance_receipt,
            {
                "schema": "keystone.manager_decision.receipt.v1",
                "manager": "chief_of_staff",
                "selected_option_id": selected.option_id,
                "downstream_owner": "opportunity_scout",
                "option_ids_preserved": [option.option_id for option in packet.options],
                "search_enabled": False,
                "provider_writes": 0,
                "send_enabled": False,
            },
        ],
        "data_handling": data_profile.audit_metadata(),
        "usage": dict(result.usage or {}),
        "cost": dict(result.cost or {}),
        "send_enabled": False,
    }


def _validate_options(options: tuple[BusinessDevelopmentOption, ...]) -> None:
    if len(options) != 2:
        raise ValueError("Finance-aware priority validation requires exactly two options.")
    ids = [option.option_id.strip() for option in options]
    titles = [option.title.strip() for option in options]
    descriptions = [option.description.strip() for option in options]
    if not all(ids) or len(set(ids)) != 2 or not all(titles) or not all(descriptions):
        raise ValueError("Business-development options require unique IDs, titles, and details.")


def _validate_decision(
    result: TypedAgentRunResult[ChiefOfStaffResult],
    packet: FinanceBDPriorityPacket,
) -> BusinessDevelopmentOption:
    output = result.output
    text = _decision_text(output)
    lowered = text.casefold()
    mentioned = [option for option in packet.options if option.title.casefold() in lowered]
    if len(mentioned) != 2:
        raise RuntimeError("Chief decision did not visibly compare both supplied options.")
    selected = [
        option
        for option in packet.options
        if any(
            phrase in lowered
            for phrase in (
                f"recommend {option.title.casefold()}",
                f"select {option.title.casefold()}",
                f"priority: {option.title.casefold()}",
                f"choose {option.title.casefold()}",
            )
        )
    ]
    if len(selected) != 1:
        raise RuntimeError("Chief decision must explicitly select exactly one option.")
    if not any(term in lowered for term in ("finance", "quarter", "cost", "budget")):
        raise RuntimeError("Chief decision omitted its finance-aware rationale.")
    if output.send_enabled or output.slack_post_allowed or output.write_requests:
        raise RuntimeError("Chief decision returned an unexpected side effect.")
    usage = dict(result.usage or {})
    if usage.get("requests", usage.get("request_count")) != MAX_OPENAI_REQUESTS:
        raise RuntimeError("Chief decision must use exactly one model request.")
    cost = dict(result.cost or {})
    observed = cost.get("estimated_usd", cost.get("actual_usd"))
    if observed is None or Decimal(str(observed)) > MAX_COST_USD:
        raise RuntimeError("Chief decision is missing cost evidence or exceeded budget.")
    return selected[0]


def _decision_text(output: ChiefOfStaffResult) -> str:
    return "\n".join(
        value
        for value in (
            str(output.synthesis or "").strip(),
            str(output.summary or "").strip(),
            *[str(item).strip() for item in output.recommended_actions],
        )
        if value
    )
