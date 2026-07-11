"""One-purpose, cost-bounded Chief synthesis over guarded local KNI evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from keystone_agents.agents.chief_of_staff import run_chief_of_staff_sdk
from keystone_agents.local_kni_evidence import build_local_kni_evidence_packet_for_query
from keystone_agents.model_provider import OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.quality_budget import AgentQualityBudget, QualityMode
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.sdk import private_context_sdk_profile

LOCAL_KNI_SUMMARY_MODEL = OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL
LOCAL_KNI_SUMMARY_MAX_REQUESTS = 1
LOCAL_KNI_SUMMARY_MAX_COST_USD = Decimal("0.05")
LOCAL_KNI_SUMMARY_WORKFLOW = "KNI capability statement summary"


def local_kni_summary_live_plan(query: str) -> dict[str, Any]:
    packet = build_local_kni_evidence_packet_for_query(query)
    candidates = _candidate_documents(packet)
    return {
        "schema": "keystone.local_kni_summary.live_run_plan.v1",
        "workflow_name": LOCAL_KNI_SUMMARY_WORKFLOW,
        "agent": "chief_of_staff",
        "model": LOCAL_KNI_SUMMARY_MODEL,
        "max_openai_requests": LOCAL_KNI_SUMMARY_MAX_REQUESTS,
        "max_cost_usd": str(LOCAL_KNI_SUMMARY_MAX_COST_USD),
        "live_search": False,
        "tools_attached": False,
        "provider_writes": False,
        "send_enabled": False,
        "private_context_approval_required": True,
        "data_handling": {
            "response_store": False,
            "prompt_cache_retention": "in_memory",
            "tracing_disabled": True,
            "trace_include_sensitive_data": False,
            "local_retrieval_only": True,
            "hosted_file_storage": False,
            "web_search": False,
            "remote_mcp": False,
            "background_mode": False,
        },
        "candidate_count": len(candidates),
        "candidate_path_hashes": [_path_hash(doc["relative_path"]) for doc in candidates],
        "review_required_count": sum(bool(doc.get("review_required")) for doc in candidates),
        "synthesis_ready": _packet_is_ready(packet),
    }


def run_local_kni_capability_summary(
    query: str,
    *,
    live_sdk: bool = False,
    max_openai_requests: int = LOCAL_KNI_SUMMARY_MAX_REQUESTS,
    max_cost_usd: Decimal = LOCAL_KNI_SUMMARY_MAX_COST_USD,
    approved_private_context: bool = False,
    runner: Callable[..., TypedAgentRunResult[ChiefOfStaffResult]] = run_chief_of_staff_sdk,
) -> dict[str, Any]:
    """Validate locally or run one no-tool Chief turn over bounded local excerpts."""

    _validate_limits(max_openai_requests=max_openai_requests, max_cost_usd=max_cost_usd)
    packet = build_local_kni_evidence_packet_for_query(query)
    if not _packet_is_ready(packet):
        raise ValueError("Local KNI capability summary is not synthesis-ready.")
    plan = local_kni_summary_live_plan(query)
    if not live_sdk:
        return {"status": "validated_offline", "plan": plan}
    if not approved_private_context:
        raise ValueError(
            "Live local KNI synthesis requires explicit approval to transmit bounded "
            "private business-document excerpts."
        )

    request = (
        "Search the supplied bounded local KNI evidence for the latest appropriate client "
        "proposal or capability statement and summarize the key service areas. Re-rank the "
        "candidate documents against this request; do not assume the first match is best. "
        "Distinguish current KNI material from templates or unrelated proposals. Include the "
        "evidence path for every source used, state uncertainty, and retain human-review, "
        "local_only=true, and send_enabled=false. Do not search, call tools, send, post, "
        "publish, create an artifact, or write any provider."
    )
    sdk_input = {
        "request": request,
        "local_kni_evidence_packet": packet,
        "include_specialist_tools": False,
        "specialist_tool_mode": "read_plan",
        "manual_request_plan": {
            "source": "local_kni_summary_runner",
            "target_agent": "chief_of_staff",
            "intent": "context_lookup",
            "primary_target": "latest KNI proposal or capability statement",
            "task_objective": "context_lookup",
        },
    }
    budget = AgentQualityBudget(
        agent_name="chief_of_staff",
        mode=QualityMode.FAST,
        max_turns=1,
        reasoning_effort="low",
        verbosity="low",
        max_tokens=1800,
        max_review_passes=0,
        max_tool_calls=0,
        max_seconds=60,
        tool_tier="core_read",
        enable_synthesis_review=False,
        enable_context_deepening=False,
        allow_manager_loop_repair=False,
        notes=["One-request local KNI summary; supplied evidence only; no tools or writes."],
    )
    with private_context_sdk_profile() as data_profile:
        result = runner(
            sdk_input,
            live=True,
            model=LOCAL_KNI_SUMMARY_MODEL,
            quality_budget=budget,
            force_sdk_interpretation=True,
            include_specialist_tools=False,
            attach_tools=False,
        )
    return _validated_receipt(
        result,
        packet=packet,
        plan=plan,
        max_openai_requests=max_openai_requests,
        max_cost_usd=max_cost_usd,
        data_handling=data_profile.audit_metadata(),
    )


def _validated_receipt(
    result: TypedAgentRunResult[ChiefOfStaffResult],
    *,
    packet: dict[str, Any],
    plan: dict[str, Any],
    max_openai_requests: int,
    max_cost_usd: Decimal,
    data_handling: dict[str, Any],
) -> dict[str, Any]:
    output = result.output
    if output.send_enabled or output.slack_post_allowed or output.write_requests:
        raise RuntimeError("Local KNI synthesis returned an unexpected side-effect request.")
    diagnostics = dict(output.retrieval_diagnostics or {})
    if diagnostics.get("local_only") is not True or diagnostics.get("send_enabled") is not False:
        raise RuntimeError("Local KNI synthesis omitted local-only/no-send diagnostics.")
    summary = str(output.synthesis or output.summary or "").strip()
    if len(summary) < 40:
        raise RuntimeError("Local KNI synthesis did not return a substantive summary.")
    candidate_paths = {doc["relative_path"] for doc in _candidate_documents(packet)}
    used_paths = {
        str(source.title or "").strip()
        for source in output.sources
        if str(source.title or "").strip() in candidate_paths
    }
    if not used_paths:
        raise RuntimeError("Local KNI synthesis omitted a candidate evidence path.")

    usage = dict(result.usage or {})
    observed_requests = _numeric_value(usage, "requests", "request_count", "total_requests")
    if observed_requests != max_openai_requests:
        raise RuntimeError("Local KNI synthesis did not use exactly one OpenAI request.")
    cost = dict(result.cost or {})
    observed_cost = _numeric_value(cost, "estimated_usd", "total_usd", "actual_usd")
    if observed_cost is None or Decimal(str(observed_cost)) > max_cost_usd:
        raise RuntimeError("Local KNI synthesis is missing cost evidence or exceeded its ceiling.")
    request_cache = dict(result.request_cache or {})
    retries = int(request_cache.get("rate_limit_retries") or 0)
    if retries:
        raise RuntimeError("Local KNI synthesis recorded an unexpected retry.")

    return {
        "status": "success",
        "plan": plan,
        "summary_present": True,
        "summary_character_count": len(summary),
        "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
        "used_source_count": len(used_paths),
        "used_path_hashes": sorted(_path_hash(path) for path in used_paths),
        "human_review_required": output.human_review_required,
        "local_only": True,
        "send_enabled": False,
        "provider_writes": False,
        "data_handling": data_handling,
        "usage": usage,
        "cost": cost,
        "request_cache": request_cache,
    }


def _validate_limits(*, max_openai_requests: int, max_cost_usd: Decimal) -> None:
    if max_openai_requests != LOCAL_KNI_SUMMARY_MAX_REQUESTS:
        raise ValueError("Local KNI summary requires max_openai_requests=1.")
    if max_cost_usd <= 0 or max_cost_usd > LOCAL_KNI_SUMMARY_MAX_COST_USD:
        raise ValueError("Local KNI summary requires a cost ceiling from $0.01 to $0.05.")


def _candidate_documents(packet: dict[str, Any]) -> list[dict[str, Any]]:
    return [doc for doc in packet.get("candidate_documents") or [] if isinstance(doc, dict)]


def _packet_is_ready(packet: dict[str, Any]) -> bool:
    candidates = _candidate_documents(packet)
    diagnostics = packet.get("retrieval_diagnostics") or {}
    return bool(
        packet.get("local_only") is True
        and packet.get("send_enabled") is False
        and isinstance(diagnostics, dict)
        and diagnostics.get("candidate_selection_valid") is True
        and candidates
        and all(
            doc.get("content_excerpt") and doc.get("model_context_allowed")
            for doc in candidates
        )
    )


def _path_hash(path: str) -> str:
    return hashlib.sha256(str(path or "").encode()).hexdigest()[:12]


def _numeric_value(payload: dict[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    return None
