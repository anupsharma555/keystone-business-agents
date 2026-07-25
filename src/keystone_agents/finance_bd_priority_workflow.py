"""Provider-backed finance context to bounded Chief BD-priority decision."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agents.exceptions import OutputGuardrailTripwireTriggered

from keystone_agents.agents.chief_of_staff import run_chief_of_staff_sdk
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.privacy_minimized_synthesis import (
    PrivacyMinimizedFact,
    build_concept_signal_packet,
    packet_for_model,
    validate_privacy_minimized_packet,
)
from keystone_agents.quality_budget import AgentQualityBudget, QualityMode
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.sdk import private_context_sdk_profile

MAX_OPENAI_REQUESTS = 1
MAX_COST_USD = Decimal("0.05")

_FINANCE_TAXONOMY = {
    "current_quarter": ("current quarter", "current-quarter", "q1", "q2", "q3", "q4"),
    "income_data_present": ("income", "revenue"),
    "expense_data_present": ("expense", "cost", "spend"),
    "uncategorized_gap_present": ("uncategorized", "missing category"),
    "bounded_experiment_supported": ("bounded experiment", "bounded pilot"),
    "cost_constraint_present": ("cost constraint", "budget constraint", "limited budget"),
}
_TOTAL_PATTERN = re.compile(
    r"(?:total|combined)\s+(income|expenses?)\s*:\s*\$?([\d,]+(?:\.\d+)?)",
    re.I,
)
_DECISION_BEARING_FINANCE_CONCEPTS = {
    "operating_margin_positive",
    "operating_margin_negative",
    "operating_margin_neutral",
    "expense_load_low",
    "expense_load_moderate",
    "expense_load_high",
}


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
            "source": "canonical:l174_19_finance_packet",
            "target_agent": "chief_of_staff",
            "intent": "context_lookup",
            "task_objective": "context_lookup",
            "primary_target": "finance_tax_tracker current quarter",
            "target_type": "business_system_context",
            "provider_system": "airtable",
            "provider_operations": ["read"],
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
        "transmission_mode": "privacy_minimized_concept_signals",
        "supported_context_modes": ["privacy_minimized_concept_signals"],
        "privacy_minimized_preview_available": True,
        "proof_scope": "sanitized_context_proof",
    }


def finance_bd_priority_privacy_preview(
    packet: FinanceBDPriorityPacket,
) -> dict[str, Any]:
    """Materialize the exact no-tool decision context without a model call."""

    _validate_options(packet.options)
    minimized_packet = _finance_privacy_packet(packet)
    concepts = {fact.concept for fact in minimized_packet.facts}
    decision_signal_sufficient = bool(
        concepts
        & (
            _DECISION_BEARING_FINANCE_CONCEPTS
            | {"bounded_experiment_supported", "cost_constraint_present"}
        )
    )
    return {
        "status": (
            "privacy_minimized_preview"
            if decision_signal_sufficient
            else "privacy_minimized_preview_insufficient"
        ),
        "proof_scope": "sanitized_context_proof",
        "plan": finance_bd_priority_plan(packet),
        "bundle": {
            "finance_context": packet_for_model(minimized_packet),
            "business_development_options": [
                {
                    "option_id": option.option_id,
                    "title": option.title,
                    "description": option.description,
                }
                for option in packet.options
            ],
        },
        "exact_financial_values_transmitted": False,
        "decision_signal_sufficient": decision_signal_sufficient,
        "provider_read_verified": bool(
            packet.finance_receipt.get("provider_read_verified")
        ),
        "openai_requests_made": 0,
        "provider_writes": 0,
    }


def execute_finance_bd_priority_decision(
    packet: FinanceBDPriorityPacket,
    *,
    live_sdk: bool,
    approved_privacy_minimized_context: bool = False,
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
    if approved_privacy_minimized_context and approved_private_context:
        raise ValueError("Choose exactly one finance context mode.")
    if approved_private_context:
        raise ValueError(
            "Trusted-private finance synthesis is disabled; use the typed "
            "privacy-minimized posture packet."
        )
    if not approved_privacy_minimized_context and not approved_private_context:
        raise ValueError(
            "Finance-aware Chief decision requires approval for the privacy-minimized "
            "posture packet."
        )
    minimized_packet = _finance_privacy_packet(packet)
    concepts = {fact.concept for fact in minimized_packet.facts}
    if not concepts & (
        _DECISION_BEARING_FINANCE_CONCEPTS
        | {"bounded_experiment_supported", "cost_constraint_present"}
    ):
        raise ValueError(
            "Privacy-minimized finance context lacks a decision-bearing cost or "
            "bounded-experiment signal; add a deterministic posture assertion."
        )
    finance_context: Any = packet_for_model(minimized_packet)
    context_mode = "privacy_minimized_concept_signals"
    proof_scope = "sanitized_context_proof"
    exact_financial_values_transmitted = False

    request = (
        "Use the supplied privacy-minimized current-quarter finance posture before "
        "choosing exactly one "
        "business-development priority. Explain how the finance constraints affect the "
        "choice, compare both supplied options, and hand the selected option to Opportunity "
        "Scout for read-only validation. End the summary with exactly one line in the form "
        "'Priority: Option Alpha' or 'Priority: Option Beta'. "
        "Do not search, call tools, write, send, post, or "
        "publish. Keep the result strictly to operating posture and option tradeoffs; omit "
        "tax, legal, medical, and regulatory topics."
    )
    sdk_input = {
        "request": request,
        "finance_context": finance_context,
        "business_development_options": [
            {
                "option_id": option.option_id,
                "title": option.title,
                "description": option.description,
            }
            for option in packet.options
        ],
        "manual_request_plan": {
            "source": "canonical:l174_19_finance_bd_priority",
            "target_agent": "chief_of_staff",
            "workflow": ["chief_of_staff", "opportunity_scout"],
            "intent": "route_request",
            "task_objective": "route_or_continue",
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
        try:
            result = decision_runner(
                sdk_input,
                live=True,
                force_sdk_interpretation=True,
                include_specialist_tools=False,
                attach_tools=False,
                quality_budget=budget,
            )
        except OutputGuardrailTripwireTriggered as exc:
            output_info = getattr(exc.guardrail_result.output, "output_info", {}) or {}
            risk_flags = tuple(output_info.get("risk_flags") or ())
            reasons = tuple(output_info.get("reasons") or ())
            raise RuntimeError(
                "Finance-priority synthesis tripped the output guardrail; "
                f"risk_flags={risk_flags!r}; reasons={reasons!r}."
            ) from exc
    selected = _validate_decision(result, packet)
    decision_text = _decision_text(result.output)
    return {
        "status": "passed",
        "proof_scope": proof_scope,
        "context_mode": context_mode,
        "exact_financial_values_transmitted": exact_financial_values_transmitted,
        "plan": plan,
        "selected_option_id": selected.option_id,
        "decision": decision_text,
        "decision_storage": "local_artifact_only",
        "decision_sha256": hashlib.sha256(decision_text.encode()).hexdigest(),
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


def _finance_privacy_packet(packet: FinanceBDPriorityPacket) -> Any:
    minimized = build_concept_signal_packet(
        workflow="finance_priority",
        sources={"current_quarter_aggregate": packet.finance_summary},
        taxonomy=_FINANCE_TAXONOMY,
        constraints=(
            "no_exact_financial_values",
            "compare_both_options",
            "select_exactly_one",
            "no_external_action",
        ),
    )
    posture_concepts = _derive_finance_posture_concepts(packet.finance_summary)
    if not posture_concepts:
        return minimized
    source_hashes = minimized.source_hashes
    enriched = minimized.model_copy(
        update={
            "facts": minimized.facts
            + tuple(
                PrivacyMinimizedFact(
                    concept=concept,
                    evidence_count=1,
                    source_hashes=source_hashes,
                )
                for concept in posture_concepts
                if concept not in {fact.concept for fact in minimized.facts}
            )
        }
    )
    return validate_privacy_minimized_packet(
        enriched,
        forbidden_raw_values=(packet.finance_summary,),
    )


def _derive_finance_posture_concepts(summary: str) -> tuple[str, ...]:
    """Convert exact local totals into non-identifying decision posture bands."""

    totals: dict[str, Decimal] = {}
    for match in _TOTAL_PATTERN.finditer(summary):
        label = match.group(1).casefold()
        key = "expense" if label.startswith("expense") else "income"
        totals.setdefault(key, Decimal(match.group(2).replace(",", "")))
    income = totals.get("income")
    expense = totals.get("expense")
    if income is None or expense is None:
        return ()
    concepts: list[str] = []
    if income > expense:
        concepts.append("operating_margin_positive")
    elif expense > income:
        concepts.append("operating_margin_negative")
    else:
        concepts.append("operating_margin_neutral")
    if income <= 0:
        concepts.append("expense_load_high" if expense > 0 else "expense_load_low")
    else:
        ratio = expense / income
        if ratio < Decimal("0.50"):
            concepts.append("expense_load_low")
        elif ratio <= Decimal("1.00"):
            concepts.append("expense_load_moderate")
        else:
            concepts.append("expense_load_high")
    return tuple(concepts)


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
