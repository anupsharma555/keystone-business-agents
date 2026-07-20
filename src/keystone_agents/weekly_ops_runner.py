"""One-purpose, cost-bounded Chief synthesis runner for weekly operations packets."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

from keystone_agents.agents.chief_of_staff import run_chief_of_staff_sdk
from keystone_agents.model_provider import OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.privacy_minimized_synthesis import PROOF_SCOPE, packet_for_model
from keystone_agents.quality_budget import AgentQualityBudget, QualityMode
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput
from keystone_agents.sdk import private_context_sdk_profile
from keystone_agents.weekly_ops_packet import (
    build_weekly_ops_privacy_minimized_packet,
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
        "transmission_mode": "privacy_minimized_assertions",
        "supported_context_modes": ["privacy_minimized_assertions"],
        "proof_scope": PROOF_SCOPE,
        "data_handling": {
            "response_store": False,
            "prompt_cache_retention": "in_memory",
            "tracing_disabled": True,
            "trace_include_sensitive_data": False,
        },
    }


def weekly_ops_privacy_preview(payload: WeeklyOpsAssemblyInput) -> dict[str, Any]:
    """Materialize the exact no-tool model packet and readiness receipt."""

    bundle = build_weekly_ops_source_bundle(payload)
    minimized = build_weekly_ops_privacy_minimized_packet(payload)
    return {
        "status": (
            "privacy_minimized_preview"
            if bundle["synthesis_ready"]
            else "privacy_minimized_preview_insufficient"
        ),
        "plan": weekly_ops_live_run_plan(payload),
        "bundle": packet_for_model(minimized),
        "source_count": len(minimized.source_hashes),
        "assertion_count": len(minimized.assertions),
        "synthesis_ready": bool(bundle["synthesis_ready"]),
        "openai_requests_made": 0,
        "provider_writes": 0,
        "send_enabled": False,
    }


def run_weekly_ops_packet_synthesis(
    payload: WeeklyOpsAssemblyInput,
    *,
    live_sdk: bool = False,
    max_openai_requests: int = WEEKLY_PACKET_MAX_REQUESTS,
    max_cost_usd: Decimal = WEEKLY_PACKET_MAX_COST_USD,
    approved_privacy_minimized_context: bool = False,
    approved_private_context: bool = False,
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
    if approved_privacy_minimized_context and approved_private_context:
        raise ValueError("Choose exactly one weekly-packet context mode.")
    if approved_private_context:
        raise ValueError(
            "Trusted-private weekly synthesis is disabled; use the typed "
            "privacy-minimized assertion packet."
        )
    if not approved_privacy_minimized_context and not approved_private_context:
        raise ValueError(
            "Live weekly synthesis requires approval for the typed privacy-minimized "
            "assertion packet."
        )
    model_context = packet_for_model(build_weekly_ops_privacy_minimized_packet(payload))
    context_key = "privacy_minimized_context"
    context_mode = "privacy_minimized_assertions"
    proof_scope = PROOF_SCOPE
    specificity_bundle: dict[str, Any] | None = model_context

    request = weekly_ops_operator_request(payload)
    sdk_input = {
        "request": request,
        context_key: model_context,
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
    result = replace(
        result,
        output=_materialize_weekly_assertion_basis(
            result.output,
            specificity_bundle=specificity_bundle,
        ),
    )
    _validate_live_result(
        result,
        max_openai_requests=max_openai_requests,
        max_cost_usd=max_cost_usd,
        specificity_bundle=specificity_bundle,
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
        "proof_scope": proof_scope,
        "context_mode": context_mode,
        "raw_private_context_transmitted": approved_private_context,
    }


def weekly_ops_operator_request(payload: WeeklyOpsAssemblyInput) -> str:
    """Return the canonical natural request shared by KBA and matched baselines."""

    date_min = payload.window.time_min[:10]
    date_max = payload.window.time_max[:10]
    return (
        "Prepare an internal weekly operations packet from only the supplied bounded "
        f"{date_min} through {date_max} context. Use these headings in order: "
        "Executive focus areas; Workstreams and decisions; Completed runs and outcomes; "
        "Carry forward; One-time Calendar focus; Recurring Calendar cadence; Next actions; "
        "Source basis; Operational health; Packet metadata. For privacy-minimized "
        "assertions, name each source family and preserve the supplied workstream, status, "
        "action-state, owner-role, and count labels in readable form; do not invent private "
        "topics, names, owners, or outcomes beyond those typed categories. Use "
        "clear human-readable headings corresponding to every section. Prioritize "
        "one-time Calendar "
        "events, mention recurring cadence briefly, include only explicitly relevant completed "
        "agent runs, and keep operational health and metadata succinct at the end. Return a "
        "review-only packet. Do not search, call tools, create a Google Doc, post to Slack, "
        "send, schedule, publish, or write any provider."
    )


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
    specificity_bundle: dict[str, Any] | None,
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
    _validate_packet_specificity(packet_text, specificity_bundle=specificity_bundle)
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


def _validate_packet_specificity(
    packet_text: str,
    *,
    specificity_bundle: dict[str, Any] | None,
) -> None:
    if specificity_bundle is None:
        raise RuntimeError(
            "Privacy-minimized weekly context cannot prove operationally specific packet "
            "content; add typed non-identifying source assertions."
        )
    if specificity_bundle.get("schema") == "keystone.privacy_minimized_synthesis.v1":
        assertions = specificity_bundle.get("assertions") or []
        required_dimensions = {
            ("slack_workstream", "status"): "slack",
            ("gmail_follow_up", "action_state"): "gmail",
            ("completed_run", "workstream"): "completed",
            ("one_time_calendar", "workstream"): "calendar",
        }
        normalized = " ".join(packet_text.casefold().split())
        missing = []
        for (subject, predicate), source_label in required_dimensions.items():
            candidates = [
                assertion
                for assertion in assertions
                if assertion.get("subject") == subject
                and assertion.get("predicate") == predicate
            ]
            if not candidates or not any(
                source_label in normalized
                and str(assertion.get("object") or "").replace("_", " ") in normalized
                and str(assertion.get("count")) in normalized
                for assertion in candidates
            ):
                missing.append(f"{subject}:{predicate}")
        if missing:
            raise RuntimeError(
                "Privacy-minimized weekly packet omitted typed operational assertions: "
                + ", ".join(missing)
            )
        return
    text_tokens = _meaningful_tokens(packet_text)
    source_groups = []
    for source in (specificity_bundle.get("sources") or [])[:4]:
        facts = [
            str(fact.get("summary") or "")
            for fact in source.get("key_facts") or []
            if str(fact.get("summary") or "").strip()
        ]
        source_groups.append(facts)
    missing_groups = [
        index
        for index, facts in enumerate(source_groups)
        if facts and not any(_fact_supported(text_tokens, fact) for fact in facts)
    ]
    if len(source_groups) < 4 or missing_groups:
        raise RuntimeError(
            "Weekly packet synthesis omitted concrete Slack, Gmail, completed-run, or "
            "one-time Calendar evidence."
        )


def _materialize_weekly_assertion_basis(
    output: ChiefOfStaffResult,
    *,
    specificity_bundle: dict[str, Any],
) -> ChiefOfStaffResult:
    """Append the authoritative typed operating basis without model paraphrase drift."""

    if specificity_bundle.get("schema") != "keystone.privacy_minimized_synthesis.v1":
        return output
    dimensions = (
        ("slack_workstream", "status", "Slack workstream status"),
        ("gmail_follow_up", "action_state", "Gmail follow up action state"),
        ("completed_run", "workstream", "Completed run workstream"),
        ("one_time_calendar", "workstream", "One time Calendar workstream"),
    )
    assertions = specificity_bundle.get("assertions") or []
    lines = ["Typed operational basis"]
    for subject, predicate, label in dimensions:
        values = [
            f"{str(item.get('object') or '').replace('_', ' ')} (count {item.get('count')})"
            for item in assertions
            if item.get("subject") == subject and item.get("predicate") == predicate
        ]
        if values:
            lines.append(f"{label}: " + "; ".join(values) + ".")
    annex = "\n".join(lines)
    if output.synthesis:
        return output.model_copy(update={"synthesis": f"{output.synthesis.rstrip()}\n\n{annex}"})
    return output.model_copy(update={"summary": f"{output.summary.rstrip()}\n\n{annex}"})


def _fact_supported(text_tokens: set[str], fact: str) -> bool:
    tokens = _meaningful_tokens(fact)
    return bool(tokens) and len(tokens & text_tokens) / len(tokens) >= 0.6


def _meaningful_tokens(value: str) -> set[str]:
    stop = {
        "a",
        "an",
        "and",
        "for",
        "in",
        "is",
        "of",
        "one",
        "the",
        "to",
        "was",
        "with",
    }
    return {
        token
        for token in "".join(
            character if character.isalnum() else " " for character in value.casefold()
        ).split()
        if len(token) >= 3 and token not in stop
    }
