"""Run the KNI Chief of Staff agent in deterministic or explicit live-SDK mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from keystone_agents.agents.calendar_action_interpreter import (
    calendar_lookup_response_scope,
    calendar_lookup_target_from_plan,
    resolve_calendar_action_plan,
    resolve_calendar_lookup_answer,
)
from keystone_agents.agents.chief_of_staff import (
    chief_of_staff_should_use_specialist_tools,
    plan_chief_of_staff_request,
    render_chief_of_staff_result,
    run_chief_of_staff_sdk,
)
from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.agents.orchestrator import review_specialist_output, run_orchestrator_preflight
from keystone_agents.agents.web_query_planner import resolve_web_query_plan
from keystone_agents.calendar_actions import infer_calendar_action_plan
from keystone_agents.cli import execute_direct_calendar_action
from keystone_agents.cli_sdk import add_sdk_session_arguments, sdk_session_from_args
from keystone_agents.config import load_settings
from keystone_agents.cost_tracking import parse_cost_tracking_directive
from keystone_agents.execution_request import attach_execution_public_result
from keystone_agents.finance_expense_receipts import (
    resolve_finance_expense_receipt_target,
)
from keystone_agents.gmail_triage.contact_lookup import (
    run_gmail_contact_lookup_workflow,
)
from keystone_agents.gmail_triage.execution_plan import resolve_gmail_execution_plan
from keystone_agents.gmail_triage.priority_grouping import (
    run_gmail_priority_grouping_workflow,
)
from keystone_agents.local_kni_evidence import (
    build_local_kni_evidence_packet,
    build_local_kni_evidence_packet_for_query,
    local_kni_evidence_paths,
    local_kni_live_instruction,
    looks_like_local_kni_evidence_lookup,
)
from keystone_agents.manual_request import (
    positive_capability_text,
    request_forbids_live_research,
)
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.models import RunMode
from keystone_agents.operator_failures import known_exception_to_operator_failure
from keystone_agents.orchestrator.preflight_context import (
    compact_orchestrator_preflight_payload,
    load_manual_request_plan_from_env,
    load_orchestrator_preflight_from_env,
    load_specialist_execution_context_from_env,
)
from keystone_agents.provider_side_effect_policy import (
    semantic_provider_side_effect_policy,
)
from keystone_agents.quality_budget import AgentQualityBudget, chief_of_staff_quality_budget
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
    ChiefOfStaffSourceRef,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.semantic_execution import ExecutionIntentAuthority
from keystone_agents.source_layer_context import runtime_source_layer_policy_context
from keystone_agents.storage.sqlite_store import redact_secrets
from keystone_agents.tools.google_calendar_tool import (
    DEFAULT_CALENDAR_TIMEZONE,
    GOOGLE_CALENDAR_TIMEZONE_ENV,
    read_google_calendar_window_impl,
    resolve_google_calendar_event_impl,
)
from keystone_agents.visible_sources import append_visible_source_urls_to_output


def _chief_of_staff_session_from_args(args: argparse.Namespace) -> object | None:
    if (
        getattr(args, "sdk_session", None) is None
        and not getattr(args, "sdk_session_id", "")
        and not getattr(args, "sdk_session_db", "")
    ):
        return None
    return sdk_session_from_args(
        args,
        scope="chief_of_staff",
        components=("direct-script", os.environ.get("USER", "local"), str(Path.cwd())),
        default_enabled=False,
    )


def _approval_reference_for_request(input_text: str) -> str:
    digest = hashlib.sha256(str(input_text or "").encode("utf-8")).hexdigest()[:12]
    return f"chief-of-staff-command:{digest}"


def _requests_google_workspace_artifact(input_text: str) -> bool:
    lowered = str(input_text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "google doc",
            "google docs",
            "gdrive",
            "google drive",
            "drive folder",
            "doc link",
        )
    ) and any(
        marker in lowered
        for marker in (
            "create",
            "write",
            "provide a link",
            "provide link",
            "save",
            "analysis",
            "analyze",
            "summary",
            "report",
        )
    )


def _requests_finance_tracker_airtable_write(input_text: str) -> bool:
    lowered = " ".join(str(input_text or "").lower().split())
    has_airtable_tracker = "airtable" in lowered and any(
        marker in lowered
        for marker in (
            "finance_tax_tracker",
            "finance tax tracker",
            "tax tracker",
            "personal expenses",
            "personal expense",
            "business expenses",
            "business expense",
        )
    )
    has_write_intent = any(
        marker in lowered
        for marker in (
            "add ",
            "create ",
            "insert ",
            "record ",
            "update ",
            "change ",
            "set ",
            "fill ",
        )
    )
    return has_airtable_tracker and has_write_intent


def _requests_calendar_write(
    input_text: str,
    manual_request_plan: object | None = None,
) -> bool:
    """Bind a semantically interpreted Calendar request to Calendar tools only."""

    return bool(
        str(getattr(manual_request_plan, "provider_system", "") or "")
        == "google_calendar"
        and str(getattr(manual_request_plan, "intent", "") or "")
        == "business_system_write"
    )


def _live_side_effect_policy(
    input_text: str,
    manual_request_plan: object | None = None,
) -> str:
    semantic_policy = semantic_provider_side_effect_policy(manual_request_plan)
    if semantic_policy is not None:
        return semantic_policy
    if _requests_calendar_write(input_text, manual_request_plan):
        return (
            "The authenticated operator requested one exact Google Calendar action. "
            "Use only the dedicated Calendar create/update/delete tool needed for that "
            "action, with the supplied approval_reference and configured primary "
            "calendar/timezone. Infer the next-occurrence year and all-day status when "
            "the request supports one conventional interpretation. Verify provider "
            "state before reporting completion. Do not mutate Slack, Gmail, Airtable, "
            "Google Drive/Docs/Sheets, the repo, or any other system."
        )
    if _requests_google_workspace_artifact(input_text):
        return (
            "Live internal Airtable reads are allowed for this command. Live Google "
            "Workspace folder/doc writes are allowed only for the explicitly requested "
            "internal KNIOps artifact, using the supplied approval_reference and typed "
            "Google Workspace tools. Do not post to Slack beyond the normal result, send "
            "Gmail, create calendar events, write the repo, file tax returns, make tax "
            "payments, or mutate Airtable unless separately requested."
        )
    if _requests_finance_tracker_airtable_write(input_text):
        return (
            "Live internal Airtable schema reads, capped record reads, and the explicitly "
            "requested finance_tax_tracker Airtable create/update are allowed only through "
            "typed Airtable tools, using the supplied approval_reference and exact allowed "
            "table/field mapping. If the operator supplied a receipt/invoice PDF or image "
            "and Airtable schema exposes an attachment field on the target expense record, "
            "one receipt attachment upload is allowed through typed Airtable tools after "
            "record identity is known. The Airtable tool's dry-run/live-write/upload gates "
            "remain authoritative. Do not delete records, change schema, file tax returns, "
            "make tax payments, post to Slack beyond the normal result, send Gmail, create "
            "calendar events, write the repo, or mutate any other system."
        )
    return "read-only; no Slack post, Gmail send, calendar write, repo write, or external action"


def _run_interpreted_calendar_action(
    *,
    input_text: str,
    plan: object,
    json_output: bool,
    openai_requests: int,
    model_override: str | None = None,
    manual_plan: ManualRequestPlan | None = None,
) -> int:
    """Execute a bounded Calendar action and keep the Chief Slack wire contract."""

    direct = execute_direct_calendar_action(
        input_text,
        plan,
        live=True,
        openai_requests=openai_requests,
    )
    receipt = direct.get("tool_receipt")
    if not isinstance(receipt, dict):
        if json_output:
            print(json.dumps(direct, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            print(str(direct.get("message") or "Calendar action could not be completed."))
        return 1

    operation = str(getattr(plan, "operation", "") or receipt.get("operation") or "action")
    direct_summary = str(direct.get("human_summary") or "").strip()
    title = str(
        receipt.get("title")
        or (direct.get("calendar_lookup") or {}).get("title")
        or getattr(plan, "event_reference", "")
        or getattr(plan, "title", "")
    ).strip()
    passed = bool((receipt.get("verification") or {}).get("passed"))
    verb = {"create": "Created", "update": "Updated", "delete": "Deleted"}.get(
        operation,
        operation.capitalize(),
    )
    start_date = str(receipt.get("start_date") or getattr(plan, "start_date", "")).strip()
    start_time = str(receipt.get("start_time") or getattr(plan, "start_time", "")).strip()
    end_time = str(receipt.get("end_time") or getattr(plan, "end_time", "")).strip()
    timing = " ".join(
        part
        for part in (
            f"on {start_date}" if start_date else "",
            f"at {start_time}-{end_time}" if start_time and end_time else "",
        )
        if part
    )
    synthesis_requests = 0
    synthesis_warnings: tuple[str, ...] = ()
    if operation == "read":
        summary, synthesis_requests, synthesis_warnings = _calendar_lookup_answer(
            input_text=input_text,
            plan=plan,
            manual_plan=manual_plan,
            receipt=receipt,
            fallback=direct_summary,
            model_override=model_override,
        )
    elif operation == "delete":
        summary = (
            f"{verb} {title}; Google Calendar read-back confirmed the exact event "
            "is no longer active."
        )
    else:
        summary = (
            f"{verb} {title}{(' ' + timing) if timing else ''}; "
            "Google Calendar provider read-back passed."
        )
    actions: list[str] = []
    description = str(getattr(plan, "description", "") or "").strip()
    if description and operation != "read":
        actions.append(f'Calendar note verified from the requested update: "{description}".')
    if operation == "update":
        actions.append("The existing event was modified; no duplicate event was created.")

    payload = {
        "status": "done" if passed else str(direct.get("status") or "failed"),
        "mode": "live_calendar",
        "live_sdk": True,
        "model": "orchestrator-preflight-plus-typed-calendar-tools",
        "output": {
            "summary": summary,
            "synthesis": "",
            "recommended_actions": actions,
            "recommended_route": {
                "workflow_type": (
                    "calendar-read-complete"
                    if operation == "read"
                    else "calendar-action-complete"
                ),
                "command_text": "",
                "target_channel": "",
                "rationale": "Chief of Staff executed the exact provider-owned Calendar action.",
            },
            "approval_required": operation != "read",
            "slack_post_allowed": False,
            "send_enabled": False,
            "audit_notes": [
                "Provider receipt, not model speculation, is authoritative for completion."
            ],
        },
        "calendar_action": direct.get("calendar_action"),
        "calendar_lookup": direct.get("calendar_lookup"),
        "calendar_lookup_synthesis_warnings": list(synthesis_warnings),
        "tool_receipt": receipt,
        "usage": {
            "available": True,
            "requests": openai_requests + synthesis_requests,
        },
        "side_effects": direct.get("side_effects"),
        "human_summary": summary,
        "display_text": summary,
        "slack_display_text": summary,
    }
    if isinstance(direct.get("public_result"), dict):
        payload["public_result"] = {
            **direct["public_result"],
            "text": summary,
        }
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(summary)
        for action in actions:
            print(f"- {action}")
    return 0 if passed else 1


def _calendar_lookup_answer(
    *,
    input_text: str,
    plan: object,
    manual_plan: ManualRequestPlan | None,
    receipt: dict[str, object],
    fallback: str,
    model_override: str | None,
) -> tuple[str, int, tuple[str, ...]]:
    """Resolve one reader-ready Calendar answer through the shared direct contract."""

    events = [
        event
        for event in receipt.get("events", [])
        if isinstance(event, dict)
    ]
    response_scope = calendar_lookup_response_scope(
        manual_plan,
        plan,
    )
    resolution = resolve_calendar_lookup_answer(
        input_text,
        events,
        lookup_target=calendar_lookup_target_from_plan(manual_plan),
        response_scope=response_scope,
        fallback=fallback,
        live=True,
        model=model_override,
    )
    return (
        resolution.text,
        resolution.openai_requests,
        resolution.warnings,
    )


def _run_interpreted_gmail_contact_lookup(
    *,
    input_text: str,
    manual_plan: ManualRequestPlan,
    gmail_plan: object,
    orchestrator_preflight: object | None,
    quality_budget: AgentQualityBudget,
    model_override: str | None,
    json_output: bool,
) -> int:
    """Delegate one bounded Gmail read to Gmail Triage and return its verified answer."""

    execution = run_gmail_contact_lookup_workflow(
        operator_request=input_text,
        gmail_query=str(getattr(gmail_plan, "gmail_query", "") or "").strip(),
        max_messages=int(getattr(gmail_plan, "max_messages", 10) or 10),
        label=str(getattr(gmail_plan, "source_label", "") or "").strip() or None,
        live_sdk=True,
        model=model_override,
    )
    result = execution.result
    contact_result = result.model_dump(mode="json")
    chief_result = ChiefOfStaffResult(
        mode="llm",
        intent=input_text,
        summary=execution.human_summary,
        operating_capabilities=["gmail_contact_lookup"],
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="gmail-triage",
            target_channel="current Slack thread",
            rationale=(
                "Chief of Staff delegated a bounded read-only Gmail contact lookup "
                "to the owning specialist."
            ),
        ),
        context_sources_considered=["operator_request", "bounded_gmail_evidence"],
        retrieval_diagnostics={
            "provider": "gmail",
            "query": str(getattr(gmail_plan, "gmail_query", "") or "").strip(),
            "candidate_count": len(execution.outcome.typed_input.candidates),
            "selected_message_ids": list(result.supporting_message_ids),
            "provider_read": True,
            "provider_write": False,
            "specialist_output": contact_result,
        },
        audit_notes=[
            "The raw current operator request reached both planning and Gmail Triage.",
            "The returned address was bound to an exact provider message header.",
            "No Gmail write, draft, send, label, or external action occurred.",
        ],
    )
    outcome = execution.outcome
    model = {
        "provider": outcome.model_provider,
        "name": outcome.model_name,
        "run_mode": outcome.model_run_mode,
    }
    payload = _payload(
        mode="live_sdk_gmail_contact_lookup",
        live_sdk=True,
        model=model,
        output=chief_result,
        input_text=input_text,
        quality_budget=quality_budget,
        manual_request_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        usage=outcome.usage,
        cost=outcome.cost,
        request_cache=outcome.request_cache,
        tool_receipts=[execution.provider_receipt],
    )
    # The Slack bridge persists the bounded ``provider`` telemetry field but
    # intentionally does not retain arbitrary tool payloads. Preserve this
    # already-redacted receipt so backend acceptance can prove which provider
    # read supported the public answer without exposing message contents.
    payload["provider"] = dict(execution.provider_receipt)
    payload["gmail_contact_lookup_result"] = contact_result
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(execution.human_summary)
    return 0


def _run_interpreted_gmail_priority_grouping(
    *,
    input_text: str,
    manual_plan: ManualRequestPlan,
    gmail_plan: object,
    orchestrator_preflight: object | None,
    quality_budget: AgentQualityBudget,
    model_override: str | None,
    json_output: bool,
) -> int:
    """Run provider-first read-only Gmail collection triage for Chief."""

    execution = run_gmail_priority_grouping_workflow(
        operator_request=input_text,
        gmail_plan=gmail_plan,
        live_sdk=True,
        model=model_override,
    )
    result_payload = execution.result.model_dump(mode="json")
    chief_result = ChiefOfStaffResult(
        mode="llm",
        intent=input_text,
        summary=execution.human_summary,
        operating_capabilities=["gmail_priority_grouping"],
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="gmail-triage",
            target_channel="current Slack thread",
            rationale=(
                "Chief of Staff delegated one bounded read-only Gmail collection "
                "to the owning specialist."
            ),
        ),
        context_sources_considered=["operator_request", "bounded_gmail_collection"],
        retrieval_diagnostics={
            **execution.provider_receipt,
            "specialist_output": result_payload,
        },
        audit_notes=[
            "The raw current operator request reached both planning and Gmail Triage.",
            "Every displayed message identity was rebound to the provider result.",
            "No Gmail draft, send, label, archive, or other write occurred.",
        ],
    )
    outcome = execution.outcome
    payload = _payload(
        mode="live_sdk_gmail_priority_grouping",
        live_sdk=True,
        model={
            "provider": outcome.model_provider,
            "name": outcome.model_name,
            "run_mode": outcome.model_run_mode,
        },
        output=chief_result,
        input_text=input_text,
        quality_budget=quality_budget,
        manual_request_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        usage=outcome.usage,
        cost=outcome.cost,
        request_cache=outcome.request_cache,
        tool_receipts=[execution.provider_receipt],
    )
    payload["provider"] = dict(execution.provider_receipt)
    payload["gmail_priority_grouping_result"] = result_payload
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(execution.human_summary)
    return 0


def _request_forbids_live_web_research(input_text: str) -> bool:
    """Compatibility shim for the shared natural-language research boundary."""

    return request_forbids_live_research(input_text)


def _chief_of_staff_should_attach_tools(
    input_text: str,
    manual_plan: object | None = None,
) -> bool:
    """Attach the narrow tool set selected by the semantic plan.

    Provider words, supplied examples, and historical thread text cannot attach
    tools by themselves. A planner-unavailable Chief run remains provider-free
    rather than guessing from phrases.
    """

    del input_text
    if manual_plan is None:
        return False
    tool_backed_intents = {
        "company_research",
        "research_brief",
        "opportunity_search",
        "opportunity_to_outreach_loop",
        "gmail_triage",
        "slack_operations",
        "browser_diagnostics",
        "reference_capture",
        "business_system_write",
        "context_lookup",
    }
    return bool(
        getattr(manual_plan, "requires_live_search", False)
        or str(getattr(manual_plan, "provider_system", "") or "")
        != "unspecified"
        or list(getattr(manual_plan, "provider_operations", []) or [])
        or str(getattr(manual_plan, "intent", "") or "") in tool_backed_intents
        or str(getattr(manual_plan, "task_objective", "") or "") in tool_backed_intents
        or list(getattr(manual_plan, "workflow", []) or [])
    )


def _chief_web_query_plan_subject(input_text: str, manual_plan: object | None) -> str:
    primary_target = str(getattr(manual_plan, "primary_target", "") or "").strip()
    if primary_target:
        return primary_target[:160]
    objective = str(getattr(manual_plan, "objective", "") or "").strip()
    if objective:
        return objective[:160]
    cleaned = " ".join(str(input_text or "").split())
    return cleaned[:160] or "KNI Chief of Staff web research request"


def _chief_should_build_web_query_plan(input_text: str, manual_plan: object | None) -> bool:
    if str(getattr(manual_plan, "provider_system", "") or "") != "unspecified":
        return False
    if (
        resolve_finance_expense_receipt_target(
            input_text,
            manual_plan=manual_plan,
        )
        is not None
    ):
        return False
    blocked_plan_values = {
        "business_system_write",
        "business_system_write_plan",
        "business_system_context",
    }
    for attr in ("intent", "task_objective", "expected_artifact_type", "target_type"):
        value = str(getattr(manual_plan, attr, "") or "").strip()
        if value in blocked_plan_values:
            return False
    return True


def _chief_fallback_web_queries(input_text: str, subject: str) -> list[str]:
    topic = subject.strip() or "KNI business research"
    request = " ".join(str(input_text or "").split())
    focus = request[:140] if request and request.lower() != topic.lower() else topic
    queries = [
        f"{topic} official source",
        f"{topic} recent news 2026",
        f"{topic} independent coverage 2026",
        f"{topic} partnership funding evidence",
        f"{topic} customer case study validation",
        f"{topic} leadership hiring product launch",
    ]
    if focus and focus.lower() not in topic.lower():
        queries.insert(1, f"{topic} {focus}")
        queries.insert(2, f"{focus} source backed brief")
    return list(dict.fromkeys(query for query in queries if query.strip()))[:12]


def _configure_live_web_research_env(*, enabled: bool) -> None:
    if enabled:
        os.environ["KEYSTONE_LIVE_MODE"] = "true"
        os.environ["KEYSTONE_DRY_RUN"] = "false"
        os.environ["KEYSTONE_ENABLE_LIVE_RESEARCH"] = "true"
        return
    os.environ["KEYSTONE_ENABLE_LIVE_RESEARCH"] = "false"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan KNI Slack operations routing.")
    parser.add_argument(
        "--mode",
        choices=[RunMode.DRY_RUN.value, RunMode.LIVE.value],
        default=RunMode.DRY_RUN.value,
    )
    parser.add_argument("--input", default="", help="Request text or a local fixture path.")
    parser.add_argument(
        "--slack-repo-path",
        default=None,
        help="Optional Keystone Slack repo path for context tools.",
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for local state.")
    parser.add_argument(
        "--live-sdk",
        action="store_true",
        help=(
            "Run through live SDK model execution. Slack posts still require channel policy; "
            "external writes remain gated."
        ),
    )
    parser.add_argument("--model", default=None, help="Optional model override.")
    parser.add_argument(
        "--quality",
        choices=["fast", "balanced", "deep"],
        default=None,
        help="Chief of Staff quality budget for SDK planning.",
    )
    parser.add_argument(
        "--live-search-plan",
        action="store_true",
        help=(
            "Use the live SDK web-query planner to expand broad public web research "
            "requests before Chief of Staff chooses search/read tools."
        ),
    )
    parser.add_argument(
        "--live-search",
        action="store_true",
        help=(
            "Allow Chief of Staff to call the shared live `search_web` provider ladder "
            "during live SDK execution. Requires --live-sdk and remains read-only."
        ),
    )
    add_sdk_session_arguments(parser)
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def _read_input(value: str) -> str:
    if not value:
        return ""
    if len(value) < 240 and "\n" not in value:
        try:
            path = Path(value)
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            pass
    return value


def _cost_tracking_note(*, usage: object | None, cost: object | None) -> str:
    usage_data = usage if isinstance(usage, dict) else {}
    cost_data = cost if isinstance(cost, dict) else {}
    if cost_data.get("estimated_usd") is not None:
        tokens = usage_data if usage_data.get("available") else cost_data.get("billable_tokens", {})
        input_tokens = tokens.get("input_tokens")
        cached_tokens = tokens.get("cached_input_tokens")
        output_tokens = tokens.get("output_tokens")
        token_parts = []
        if input_tokens is not None:
            token_parts.append(f"input={input_tokens}")
        if cached_tokens is not None:
            token_parts.append(f"cached_input={cached_tokens}")
        if output_tokens is not None:
            token_parts.append(f"output={output_tokens}")
        token_text = f"; tokens {', '.join(token_parts)}" if token_parts else ""
        return (
            f"Run cost tracked: estimated ${float(cost_data['estimated_usd']):.6f} USD"
            f"{token_text}. This is a local pricing-table estimate, not an invoice."
        )
    note = str(cost_data.get("note") or "").strip()
    if note:
        return f"Run cost tracking requested, but no dollar estimate is available: {note}"
    return "Run cost tracking requested, but provider usage/cost metadata was not available."


def _with_cost_tracking_note(
    output: object,
    *,
    requested: bool,
    usage: object | None,
    cost: object | None,
) -> object:
    if not requested or not hasattr(output, "model_copy"):
        return output
    note = _cost_tracking_note(usage=usage, cost=cost)
    summary = str(getattr(output, "summary", "") or "").strip()
    audit_notes = list(getattr(output, "audit_notes", []) or [])
    if note not in audit_notes:
        audit_notes.append(note)
    if note not in summary:
        summary = f"{summary}\n\n{note}" if summary else note
    return output.model_copy(update={"summary": summary, "audit_notes": audit_notes})


def _chief_of_staff_human_summary(
    output: object,
    *,
    manual_request_plan: object | None = None,
) -> str:
    """Return the complete public Chief answer without operational metadata."""

    summary = str(getattr(output, "summary", "") or "").strip()
    synthesis = str(getattr(output, "synthesis", "") or "").strip()
    ask_shape = getattr(manual_request_plan, "ask_shape", None)
    constraints = getattr(ask_shape, "output_constraints", None)
    requested_item_count = getattr(constraints, "maximum_items", None)
    exact_item_count = (
        getattr(constraints, "item_count_mode", "") == "exact"
        and isinstance(requested_item_count, int)
        and requested_item_count > 0
    )
    summary_bullets = [
        line
        for line in summary.splitlines()
        if re.match(r"^\s*(?:[-*]|\d+[.)])\s+\S", line)
    ]
    if (
        getattr(ask_shape, "output_form", "") == "bullets"
        and exact_item_count
        and len(summary_bullets) == requested_item_count
    ):
        return summary
    if not summary:
        return synthesis
    if not synthesis:
        return summary

    normalized_summary = " ".join(summary.lower().split())
    normalized_synthesis = " ".join(synthesis.lower().split())
    if normalized_summary in normalized_synthesis:
        return synthesis
    if normalized_synthesis in normalized_summary:
        return summary
    return f"{summary}\n\n{synthesis}"


def _normalized_calendar_title(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _calendar_title_tokens(value: object) -> set[str]:
    return {
        token
        for token in _normalized_calendar_title(value).split()
        if len(token) > 1 and token not in {"the", "and", "for", "with"}
    }


def _calendar_event_when(event: dict[str, object]) -> str:
    display_date = str(
        event.get("display_start_date") or event.get("start_date") or ""
    ).strip()
    display_time = str(event.get("display_start_time") or "").strip()
    if display_date:
        if not display_time:
            return f" on {display_date}"
        try:
            time_text = datetime.strptime(display_time[:5], "%H:%M").strftime(
                "%-I:%M %p"
            )
        except ValueError:
            time_text = display_time
        timezone = " ".join(str(event.get("display_timezone") or "").split())
        timezone_suffix = f" ({timezone})" if timezone else ""
        return f" on {display_date} at {time_text}{timezone_suffix}"
    start = str(event.get("start") or "").strip()
    if not start:
        return ""
    date_text = start[:10]
    if "T" not in start:
        return f" on {date_text}"
    try:
        parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
        time_text = parsed.strftime("%-I:%M %p")
    except ValueError:
        time_text = start[11:16]
    return f" on {date_text} at {time_text}"


def _calendar_event_location(event: dict[str, object]) -> str:
    location = " ".join(str(event.get("location") or "").split())
    return f" Location: {location}." if location else ""


def _calendar_provider_human_summary(
    *,
    manual_request_plan: object,
    receipts: list[dict[str, object]],
    fallback: str,
) -> str:
    """Render provider truth for Calendar without exposing IDs or workflow prose."""

    intent = str(getattr(manual_request_plan, "intent", "") or "")
    successful = [
        receipt
        for receipt in receipts
        if receipt.get("status") in {"success", "not_found", "ambiguous"}
        and str(receipt.get("operation") or "").startswith(
            (
                "read_calendar",
                "resolve_calendar",
                "create_calendar",
                "update_calendar",
                "delete_calendar",
            )
        )
    ]
    if intent == "business_system_write":
        verified_writes = [
            receipt
            for receipt in successful
            if str(receipt.get("operation") or "").startswith(
                ("create_calendar", "update_calendar", "delete_calendar")
            )
            and isinstance(receipt.get("verification"), dict)
            and receipt["verification"].get("passed") is True
        ]
        if not verified_writes:
            return fallback
        receipt = verified_writes[-1]
        operation = str(receipt.get("operation") or "")
        verb = (
            "created"
            if operation.startswith("create")
            else "updated"
            if operation.startswith("update")
            else "deleted"
        )
        title = str(receipt.get("title") or "").strip() or str(
            getattr(manual_request_plan, "primary_target", "") or ""
        ).strip()
        when = _calendar_event_when(receipt)
        return f'Google Calendar event {verb} and verified: "{title}"{when}.'

    resolved_reads = [
        receipt
        for receipt in successful
        if str(receipt.get("operation") or "").startswith("resolve_calendar")
    ]
    if resolved_reads:
        receipt = resolved_reads[-1]
        target = str(receipt.get("event_reference") or "").strip() or str(
            getattr(manual_request_plan, "primary_target", "") or ""
        ).strip()
        status = str(receipt.get("status") or "")
        if status == "not_found":
            return f'No - I did not find an active event matching "{target}" in Google Calendar.'
        if status == "ambiguous":
            return (
                f'I found multiple active events matching "{target}" in Google Calendar. '
                "Give me a date or one more title detail and I can identify the right one."
            )
        title = str(receipt.get("title") or target).strip()
        return (
            f'Yes - "{title}" is on your Google Calendar'
            f"{_calendar_event_when(receipt)}."
            f"{_calendar_event_location(receipt)}"
        )

    reads = [
        receipt
        for receipt in successful
        if str(receipt.get("operation") or "").startswith("read_calendar")
    ]
    if not reads:
        return fallback
    receipt = reads[-1]
    events = [
        event
        for event in list(receipt.get("events") or [])
        if isinstance(event, dict)
    ]
    required_entities = [
        str(item or "").strip()
        for item in list(getattr(manual_request_plan, "required_entities", []) or [])
        if str(item or "").strip()
    ]
    target = (
        required_entities[0]
        if required_entities
        else str(getattr(manual_request_plan, "primary_target", "") or "").strip()
    )
    normalized_target = _normalized_calendar_title(target)
    if not normalized_target:
        return fallback or f"I checked Google Calendar and found {len(events)} active events."
    exact_matches = [
        event
        for event in events
        if _normalized_calendar_title(event.get("title")) == normalized_target
    ]
    matches = exact_matches or [
        event
        for event in events
        if normalized_target in _normalized_calendar_title(event.get("title"))
        or _normalized_calendar_title(event.get("title")) in normalized_target
    ]
    target_tokens = _calendar_title_tokens(target)
    matches = matches or [
        event
        for event in events
        if len(target_tokens) >= 2
        and target_tokens.issubset(_calendar_title_tokens(event.get("title")))
    ]
    if not matches:
        if not events:
            return f'No - I did not find an active event named "{target}" in Google Calendar.'
        # A semantically equivalent renamed event can require model judgment.
        # Provider execution is verified here; preserve the model's interpretation
        # rather than converting a non-exact title comparison into a false "No".
        return fallback
    event = matches[0]
    title = str(event.get("title") or target).strip()
    return (
        f'Yes - "{title}" is on your Google Calendar'
        f"{_calendar_event_when(event)}."
        f"{_calendar_event_location(event)}"
    )


def _calendar_context_lookup_target(manual_request_plan: object) -> str:
    """Use model-resolved provider identity without interpreting request phrases."""

    return calendar_lookup_target_from_plan(manual_request_plan)


def _verified_provider_synthesis_budget(
    budget: AgentQualityBudget,
) -> AgentQualityBudget:
    """Use one tool-free Chief turn after a verified provider read is acquired."""

    return budget.model_copy(
        update={
            "max_turns": 1,
            "max_tool_calls": 0,
            "enable_context_deepening": False,
            "hosted_web_search_max_calls": 0,
            "notes": [
                *budget.notes,
                (
                    "A typed provider read completed before Chief synthesis; the "
                    "model receives verified context and no provider tools."
                ),
            ],
        }
    )


def _explicit_calendar_date(value: object) -> str:
    """Extract only an explicit calendar date; semantic intent stays model-owned."""

    text = str(value or "")
    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if iso_match:
        try:
            return datetime.strptime(iso_match.group(1), "%Y-%m-%d").date().isoformat()
        except ValueError:
            return ""
    named_match = re.search(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Sept(?:ember)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+20\d{2}\b",
        text,
        flags=re.I,
    )
    if not named_match:
        return ""
    raw = named_match.group(0)
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _execute_required_calendar_context_lookup(
    manual_request_plan: object,
    *,
    input_text: str = "",
) -> dict[str, object] | None:
    """Guarantee one typed provider read when semantic planning requires it.

    The LLM owns intent and target interpretation. Python owns execution of the
    selected read and receipt capture so a model cannot claim it re-checked
    Calendar after merely reusing prior thread prose.
    """

    if (
        str(getattr(manual_request_plan, "provider_system", "") or "")
        != "google_calendar"
        or str(getattr(manual_request_plan, "intent", "") or "") != "context_lookup"
    ):
        return None
    if (
        str(getattr(manual_request_plan, "provider_read_scope", "") or "")
        == "bounded_collection"
    ):
        # A collection request such as "list all events tomorrow" has no exact
        # event identity. Let the Calendar window tool interpret the raw current
        # ask instead of collapsing the whole request into one title lookup.
        return None
    target = _calendar_context_lookup_target(manual_request_plan)
    if not target:
        return None
    resolved = resolve_google_calendar_event_impl(
        target,
        live=True,
    )
    if resolved.get("status") != "not_found":
        return resolved
    explicit_date = _explicit_calendar_date(input_text)
    if not explicit_date:
        return resolved
    timezone_name = (
        os.getenv(GOOGLE_CALENDAR_TIMEZONE_ENV) or DEFAULT_CALENDAR_TIMEZONE
    )
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo(DEFAULT_CALENDAR_TIMEZONE)
    start = datetime.fromisoformat(explicit_date).replace(tzinfo=timezone)
    return read_google_calendar_window_impl(
        start.isoformat(),
        (start + timedelta(days=1)).isoformat(),
        live=True,
    )


def _workflow_sdk_usage_events(
    *,
    orchestrator_preflight: object | None,
    mode: str,
    usage: object | None,
    cost: object | None,
    request_cache: object | None,
) -> list[dict[str, object]]:
    """Return every model stage that belongs to one Chief workflow.

    Orchestrator preflight owns planning usage while the selected specialist
    owns synthesis usage. Keep both as individual audit events before deriving
    the top-level workflow totals.
    """

    preflight = compact_orchestrator_preflight_payload(orchestrator_preflight)
    raw_events = preflight.get("sdk_usage_events")
    events = (
        [dict(event) for event in raw_events if isinstance(event, dict)]
        if isinstance(raw_events, list)
        else []
    )
    current_usage = dict(usage) if isinstance(usage, dict) else {}
    current_cost = dict(cost) if isinstance(cost, dict) else {}
    current_cache = dict(request_cache) if isinstance(request_cache, dict) else {}
    if current_usage or current_cost or current_cache:
        agent_name = (
            "gmail_triage"
            if mode
            in {
                "live_sdk_gmail_contact_lookup",
                "live_sdk_gmail_priority_grouping",
            }
            else "chief_of_staff"
        )
        event: dict[str, object] = {
            "agent_name": agent_name,
            "run_stage": mode,
        }
        if current_usage:
            event["usage"] = current_usage
        if current_cost:
            event["cost"] = current_cost
        if current_cache:
            event["request_cache"] = current_cache
        events.append(event)
    return events


def _aggregate_sdk_usage(events: list[dict[str, object]]) -> dict[str, object]:
    """Sum model usage without losing the individual workflow-stage events."""

    usage_records = [
        event.get("usage")
        for event in events
        if isinstance(event.get("usage"), dict)
    ]
    if not usage_records:
        return {}
    numeric_keys = (
        "requests",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_output_tokens",
    )
    aggregate: dict[str, object] = {
        "available": any(
            bool(record.get("available"))
            or any(record.get(key) not in (None, 0, "") for key in numeric_keys)
            for record in usage_records
        ),
        "stage_count": len(usage_records),
    }
    for key in numeric_keys:
        aggregate[key] = sum(
            _nonnegative_int(record.get(key)) for record in usage_records
        )
    if not aggregate["total_tokens"]:
        aggregate["total_tokens"] = (
            int(aggregate["input_tokens"]) + int(aggregate["output_tokens"])
        )
    input_tokens = int(aggregate["input_tokens"])
    cached_input_tokens = min(
        input_tokens,
        int(aggregate["cached_input_tokens"]),
    )
    aggregate["cache_hit_rate"] = (
        round(cached_input_tokens / input_tokens, 4) if input_tokens else 0.0
    )
    return aggregate


def _aggregate_sdk_cost(events: list[dict[str, object]]) -> dict[str, object]:
    """Sum compatible estimated-cost fields across workflow model stages."""

    cost_records = [
        event.get("cost")
        for event in events
        if isinstance(event.get("cost"), dict)
    ]
    if not cost_records:
        return {}

    def amount(record: dict[str, object]) -> float:
        for key in ("amount_usd", "estimated_usd", "estimated_cost_usd"):
            if record.get(key) in (None, ""):
                continue
            try:
                return max(0.0, float(record.get(key) or 0.0))
            except (TypeError, ValueError):
                continue
        return 0.0

    total = round(sum(amount(record) for record in cost_records), 8)
    aggregate: dict[str, object] = {
        "amount_usd": total,
        "estimated_usd": total,
        "estimated_cost_usd": total,
        "currency": "USD",
        "source": "aggregated_sdk_stages",
        "confidence": "estimate",
        "stage_count": len(cost_records),
        "note": (
            "Summed from the audit-safe per-stage SDK estimates for this workflow; "
            "this is not an invoice record."
        ),
    }
    for nested_key in ("billable_tokens", "components_usd"):
        nested_records = [
            record.get(nested_key)
            for record in cost_records
            if isinstance(record.get(nested_key), dict)
        ]
        if not nested_records:
            continue
        nested_names = {
            key for record in nested_records for key in record
        }
        aggregate[nested_key] = {
            key: round(
                sum(
                    max(0.0, float(record.get(key) or 0.0))
                    for record in nested_records
                ),
                8,
            )
            for key in sorted(nested_names)
        }
    return aggregate


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _payload(
    *,
    mode: str,
    live_sdk: bool,
    model: object,
    output: object,
    input_text: str,
    quality_budget: AgentQualityBudget | None = None,
    manual_request_plan: object | None = None,
    orchestrator_preflight: object | None = None,
    orchestrator_review: object | None = None,
    original_orchestrator_review: object | None = None,
    usage: object | None = None,
    cost: object | None = None,
    request_cache: object | None = None,
    web_query_plan: object | None = None,
    delegated_work_item_result: object | None = None,
    tool_receipts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    dumped = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
    human_summary = _chief_of_staff_human_summary(
        output,
        manual_request_plan=manual_request_plan,
    )
    payload: dict[str, object] = {
        "agent_name": "chief_of_staff",
        "mode": mode,
        "live_sdk": live_sdk,
        "model": model,
        "output_type": type(output).__name__,
        "input_summary": input_text[:240],
        "send_enabled": False,
        "output": dumped,
    }
    workflow_usage_events = _workflow_sdk_usage_events(
        orchestrator_preflight=orchestrator_preflight,
        mode=mode,
        usage=usage,
        cost=cost,
        request_cache=request_cache,
    )
    receipts = list(tool_receipts or [])
    if receipts:
        payload["tool_receipts"] = receipts
    if human_summary:
        payload["human_summary"] = human_summary
        payload["slack_display_text"] = human_summary
        payload["display_text"] = human_summary
    if quality_budget is not None:
        payload["quality_budget"] = quality_budget.model_dump(mode="json")
    if workflow_usage_events:
        payload["workflow_sdk_usage_events"] = workflow_usage_events
    aggregate_usage = _aggregate_sdk_usage(workflow_usage_events)
    if aggregate_usage:
        payload["usage"] = aggregate_usage
        payload["openai_requests"] = int(aggregate_usage.get("requests") or 0)
    aggregate_cost = _aggregate_sdk_cost(workflow_usage_events)
    if aggregate_cost:
        payload["cost"] = aggregate_cost
    if request_cache is not None:
        payload["request_cache"] = request_cache
    if web_query_plan is not None:
        payload["web_query_plan"] = (
            web_query_plan.model_dump(mode="json")
            if hasattr(web_query_plan, "model_dump")
            else web_query_plan
        )
    if delegated_work_item_result is not None:
        payload["delegated_work_item_result"] = (
            delegated_work_item_result.model_dump(mode="json")
            if hasattr(delegated_work_item_result, "model_dump")
            else delegated_work_item_result
        )
    provider_system = str(
        getattr(manual_request_plan, "provider_system", "") or ""
    )
    if provider_system == "google_calendar":
        intent = str(getattr(manual_request_plan, "intent", "") or "")
        matching_receipts = [
            receipt
            for receipt in receipts
            if receipt.get("status") in {"success", "not_found", "ambiguous"}
            if str(receipt.get("operation") or "").startswith(
                (
                    "read_calendar",
                    "resolve_calendar",
                    "create_calendar",
                    "update_calendar",
                    "delete_calendar",
                )
            )
        ]
        verified = bool(
            matching_receipts
            and (
                intent != "business_system_write"
                or any(
                    isinstance(receipt.get("verification"), dict)
                    and receipt["verification"].get("passed") is True
                    for receipt in matching_receipts
                )
            )
        )
        payload["status"] = "done" if verified else "blocked"
        payload["side_effects"] = {
            "calendar_write_performed": bool(
                intent == "business_system_write" and verified
            ),
            "email_sent": False,
            "slack_message_posted": False,
        }
        if not verified:
            failure_text = (
                "I could not verify the requested Google Calendar "
                + (
                    "write from a provider read-back receipt."
                    if intent == "business_system_write"
                    else "read from a provider result."
                )
            )
            payload["human_summary"] = failure_text
            payload["slack_display_text"] = failure_text
            payload["display_text"] = failure_text
            payload["block_kind"] = "calendar_provider_verification_required"
        else:
            provider_summary = _calendar_provider_human_summary(
                manual_request_plan=manual_request_plan,
                receipts=matching_receipts,
                fallback=human_summary,
            )
            payload["human_summary"] = provider_summary
            payload["slack_display_text"] = provider_summary
            payload["display_text"] = provider_summary
    if (
        provider_system == "gmail"
        and str(getattr(manual_request_plan, "task_objective", "") or "")
        == "contact_discovery"
    ):
        matching_receipts = [
            receipt
            for receipt in receipts
            if receipt.get("provider") == "gmail"
            and receipt.get("operation") == "search_and_read_contact_evidence"
        ]
        verified = bool(
            matching_receipts
            and all(receipt.get("verified") is True for receipt in matching_receipts)
        )
        payload["status"] = "done" if verified else "blocked"
        payload["completion_confirmed"] = verified
        payload["side_effects"] = {
            "gmail_read": bool(matching_receipts),
            "gmail_write": False,
            "email_sent": False,
            "slack_message_posted": False,
        }
        if verified:
            payload["public_result"] = {
                "status": "completed",
                "title": "Business Agents Result Ready",
                "text": human_summary,
                "completion_confirmed": True,
                "provider_write_attempted": False,
                "provider_receipt_verified": True,
            }
        else:
            failure_text = (
                "I could not verify the requested Gmail contact lookup from a "
                "bounded provider result."
            )
            payload["human_summary"] = failure_text
            payload["slack_display_text"] = failure_text
            payload["display_text"] = failure_text
            payload["block_kind"] = "gmail_contact_provider_verification_required"
    if (
        provider_system == "gmail"
        and str(getattr(manual_request_plan, "task_objective", "") or "")
        == "gmail_triage"
    ):
        matching_receipts = [
            receipt
            for receipt in receipts
            if receipt.get("provider") == "gmail"
            and receipt.get("operation") == "bounded_priority_grouping_read"
        ]
        verified = bool(
            matching_receipts
            and all(
                receipt.get("verified") is True
                and receipt.get("complete") is True
                and receipt.get("provider_read") is True
                and receipt.get("provider_write") is False
                for receipt in matching_receipts
            )
        )
        payload["status"] = "done" if verified else "blocked"
        payload["completion_confirmed"] = verified
        payload["side_effects"] = {
            "gmail_read": bool(matching_receipts),
            "gmail_write": False,
            "email_sent": False,
            "slack_message_posted": False,
        }
        if verified:
            payload["public_result"] = {
                "status": "completed",
                "title": "Business Agents Result Ready",
                "text": human_summary,
                "completion_confirmed": True,
                "provider_write_attempted": False,
                "provider_receipt_verified": True,
            }
        else:
            failure_text = (
                "I could not verify the requested Gmail collection triage from a "
                "complete bounded provider result."
            )
            payload["human_summary"] = failure_text
            payload["slack_display_text"] = failure_text
            payload["display_text"] = failure_text
            payload["block_kind"] = "gmail_collection_provider_verification_required"
    if manual_request_plan is not None:
        payload["manual_request_plan"] = (
            manual_request_plan.model_dump(mode="json")
            if hasattr(manual_request_plan, "model_dump")
            else manual_request_plan
        )
    if orchestrator_preflight is not None:
        payload["orchestrator_preflight"] = orchestrator_preflight
    if orchestrator_review is not None:
        payload["orchestrator_review"] = (
            orchestrator_review.model_dump(mode="json")
            if hasattr(orchestrator_review, "model_dump")
            else orchestrator_review
        )
    if original_orchestrator_review is not None:
        payload["original_orchestrator_review"] = (
            original_orchestrator_review.model_dump(mode="json")
            if hasattr(original_orchestrator_review, "model_dump")
            else original_orchestrator_review
        )
    attach_execution_public_result(payload)
    return payload


def _sdk_tool_receipts(raw_result: object) -> list[dict[str, object]]:
    """Extract bounded provider receipts from one SDK run for verification."""

    receipts: list[dict[str, object]] = []
    for item in list(getattr(raw_result, "new_items", []) or []):
        if str(getattr(item, "type", "")) != "tool_call_output_item":
            continue
        raw_output = getattr(item, "output", "")
        if isinstance(raw_output, str):
            try:
                parsed = json.loads(raw_output)
            except json.JSONDecodeError:
                continue
        elif isinstance(raw_output, dict):
            parsed = raw_output
        else:
            continue
        if not isinstance(parsed, dict):
            continue
        receipt = {
            key: parsed.get(key)
            for key in (
                "status",
                "operation",
                "calendar_id",
                "event_id",
                "event_reference",
                "match_count",
                "title",
                "start_date",
                "start_time",
                "end_time",
                "html_link",
                "provider_link",
                "events",
                "time_min",
                "time_max",
                "non_recurring_count",
                "recurring_count",
                "all_day",
                "timezone",
                "description_present",
                "verification",
                "send_enabled",
            )
            if key in parsed
        }
        if receipt:
            receipts.append(receipt)
    return receipts


def _merge_tool_receipts(
    *receipt_groups: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Merge SDK-result and execution-journal receipts without duplicating proof."""

    merged: list[dict[str, object]] = []
    seen: set[str] = set()
    for group in receipt_groups:
        for receipt in group:
            fingerprint = json.dumps(receipt, ensure_ascii=True, sort_keys=True, default=str)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            merged.append(receipt)
    return merged


def _maybe_execute_recommended_work_item_handoff(
    output: object,
    *,
    input_text: str,
    database_url: str | None,
    live_sdk: bool,
    manual_request_plan: object | None,
    orchestrator_preflight: object | None,
) -> tuple[object, object | None]:
    if not isinstance(output, ChiefOfStaffResult):
        return output, None
    output_payload = output.model_dump(mode="json")
    from keystone_agents.langgraph_workflow import (
        advance_work_item_manager_loop_with_optional_langgraph,
    )
    from keystone_agents.schemas.work_item import WorkflowRunRequest
    from keystone_agents.workflow_runner import (
        _chief_output_recommended_work_item_handoff_route,
    )

    delegated_route = _chief_output_recommended_work_item_handoff_route(
        output_payload,
        input_text,
    )
    if delegated_route is None or not _chief_handoff_execution_authorized(
        input_text=input_text,
        manual_request_plan=manual_request_plan,
        delegated_route=delegated_route,
    ):
        return output, None
    inline_only = _direct_handoff_should_use_inline_only(input_text)
    delegated = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=input_text,
            save=True,
            database_url=database_url,
            live_search=False,
            live_sdk=bool(live_sdk and not inline_only),
            max_results=3,
            requested_route=delegated_route,
            manual_request_plan=(
                manual_request_plan.model_dump(mode="json")
                if hasattr(manual_request_plan, "model_dump")
                else manual_request_plan
            ),
            orchestrator_preflight=(
                orchestrator_preflight
                if isinstance(orchestrator_preflight, dict)
                else (
                    orchestrator_preflight.model_dump(mode="json")
                    if hasattr(orchestrator_preflight, "model_dump")
                    else None
                )
            ),
            allow_manager_loop_repair=True,
        ),
        max_steps=2,
    )
    return _chief_result_from_delegated_work_item(output, delegated), delegated


def _chief_handoff_execution_authorized(
    *,
    input_text: str,
    manual_request_plan: object | None,
    delegated_route: object,
) -> bool:
    """Require request/plan authority before executing a model-suggested handoff."""

    route_value = str(getattr(delegated_route, "value", delegated_route) or "").strip()
    planned_target = str(
        getattr(manual_request_plan, "target_agent", "") or ""
    ).strip()
    if route_value and planned_target == route_value:
        return True

    workflow = {
        str(getattr(item, "value", item) or "").strip()
        for item in (getattr(manual_request_plan, "workflow", None) or [])
    }
    if route_value and route_value in workflow:
        return True

    positive_request = positive_capability_text(input_text).lower()
    return bool(
        re.search(
            r"\b(?:coordinate|delegate|execute|hand\s+off|handoff|run)\b",
            positive_request,
        )
        and re.search(
            r"\b(?:agent|specialist|work\s*item|workflow|next\s+owner|"
            r"business\s+research|research\s+analyst|opportunity\s+scout|"
            r"outreach\s+composer|gmail\s+triage)\b",
            positive_request,
        )
    )


def _direct_handoff_should_use_inline_only(input_text: str) -> bool:
    lowered = " ".join(str(input_text or "").lower().split())
    return bool(
        re.search(r"\buse\s+only\b.{0,80}\b(?:inline|sanitized|approved|provided)\b", lowered)
        or re.search(r"\bdo\s+not\b.{0,80}\b(?:research|search|access|use)\b", lowered)
        and re.search(r"\b(?:externally|external|web|browser|gmail|airtable|drive|zotero)\b", lowered)
        or "no external action" in lowered
        or "no external actions" in lowered
    )


def _chief_result_from_delegated_work_item(
    output: ChiefOfStaffResult,
    delegated: object,
) -> ChiefOfStaffResult:
    route_value = str(getattr(delegated, "route", "") or "").strip()
    agent_label = _delegated_agent_label(route_value)
    summary = str(getattr(delegated, "human_summary", "") or "").strip()
    if not summary:
        summary = f"{agent_label} completed the delegated WorkItem step."
    work_item = getattr(delegated, "work_item", None)
    work_item_id = str(getattr(work_item, "id", "") or "").strip()
    status = str(getattr(delegated, "status", "") or "").strip()
    audit_notes = list(getattr(output, "audit_notes", []) or [])
    note = (
        f"Chief of Staff executed a WorkItem handoff to {route_value or agent_label}"
        + (f" ({work_item_id})." if work_item_id else ".")
    )
    if note not in audit_notes:
        audit_notes.append(note)
    return output.model_copy(
        update={
            "summary": f"Chief of Staff handed this to {agent_label}.\n\n{summary}",
            "synthesis": "",
            "recommended_route": ChiefOfStaffRouteRecommendation(
                workflow_type="clarification",
                rationale=(
                    "Delegated through the WorkItem manager loop"
                    + (f"; downstream status: {status}." if status else ".")
                ),
            ),
            "recommended_actions": [
                "Review the downstream WorkItem result before any external action."
            ],
            "nested_specialist_results": [],
            "retrieval_diagnostics": {},
            "audit_notes": audit_notes,
        }
    )


def _delegated_agent_label(route_value: str) -> str:
    labels = {
        "business_research_analyst": "Business Research Agent",
        "opportunity_scout": "Opportunity Scout Agent",
        "gmail_triage": "Gmail Triage Agent",
        "outreach_composer": "Outreach Composer Agent",
    }
    return labels.get(route_value, route_value.replace("_", " ").title() or "the next agent")


def _chief_of_staff_output_review(
    *,
    input_text: str,
    output: object,
    run_type: str,
) -> object:
    return review_specialist_output(
        agent_name="chief_of_staff",
        output=output,
        request_summary=input_text,
        run_type=run_type,
    )


def _review_detected_unrelated_output(review: object) -> bool:
    relevance = getattr(review, "relevance", None)
    if str(getattr(relevance, "status", "") or "") != "fail":
        return False
    gaps = getattr(review, "observed_gaps", []) or []
    return any("Request/output term overlap is low" in str(gap) for gap in gaps)


def _reference_capture_mismatch(input_text: str, output: object) -> bool:
    route = getattr(output, "recommended_route", None)
    workflow_type = str(getattr(route, "workflow_type", "") or "")
    if workflow_type != "reference-capture":
        return False
    lowered = str(input_text or "").lower()
    if _looks_like_local_kni_evidence_lookup(lowered):
        return not _output_mentions_local_kni_evidence_path(output)
    explicit_capture_markers = (
        "keep this for future reference",
        "for future reference",
        "remember this",
        "save this",
        "save for later",
        "bookmark this",
        "note this",
        "store this",
        "add this to memory",
        "keep this",
    )
    if any(marker in lowered for marker in explicit_capture_markers):
        return False
    question_or_search_markers = (
        "what is",
        "what are",
        "search",
        "brief",
        "synthesize",
        "find",
        "research",
        "source",
    )
    return any(marker in lowered for marker in question_or_search_markers)


def _semantic_reference_capture_mismatch(
    manual_request_plan: ManualRequestPlan,
    output: object,
    *,
    input_text: str,
) -> bool:
    """Use the LLM plan to detect a false reference-capture result."""

    if manual_request_plan.source != "llm":
        return _reference_capture_mismatch(input_text, output)
    route = getattr(output, "recommended_route", None)
    workflow_type = str(getattr(route, "workflow_type", "") or "")
    return bool(
        workflow_type == "reference-capture"
        and manual_request_plan.intent != "reference_capture"
    )


def _with_live_review_diagnostic(output: object, review: object) -> object:
    """Preserve live output while recording a non-authoritative review failure."""

    model_copy = getattr(output, "model_copy", None)
    if not callable(model_copy):
        return output
    gaps = [
        str(item).strip()
        for item in (getattr(review, "observed_gaps", []) or [])
        if str(item).strip()
    ][:3]
    audit_notes = list(getattr(output, "audit_notes", []) or [])
    note = (
        "Post-run review flagged possible request/output misalignment, but the "
        "live model output and provider receipts were preserved instead of being "
        "replaced by a deterministic fallback."
    )
    if gaps:
        note += " Review gaps: " + " | ".join(gaps)
    if note not in audit_notes:
        audit_notes.append(note)
    return model_copy(update={"audit_notes": audit_notes})


def _output_mentions_local_kni_evidence_path(output: object) -> bool:
    values: list[str] = [
        str(getattr(output, "summary", "") or ""),
        str(getattr(output, "synthesis", "") or ""),
    ]
    sources = getattr(output, "sources", []) or []
    if isinstance(sources, list):
        for source in sources:
            values.extend(
                [
                    str(getattr(source, "title", "") or ""),
                    str(getattr(source, "url", "") or ""),
                    str(getattr(source, "note", "") or ""),
                ]
            )
            if isinstance(source, dict):
                values.extend(
                    [
                        str(source.get("title") or ""),
                        str(source.get("url") or ""),
                        str(source.get("note") or ""),
                    ]
                )
    diagnostics = getattr(output, "retrieval_diagnostics", {}) or {}
    if isinstance(diagnostics, dict):
        values.append(str(diagnostics.get("evidence_path") or ""))
    combined = " ".join(values).lower()
    return (
        "evidence path" in combined
        or "00_admin/formation/" in combined
        or "formationdocument" in combined
        or ".pdf" in combined
    )


def _local_kni_evidence_paths(packet: object | None) -> list[str]:
    return local_kni_evidence_paths(packet)


def _with_local_kni_evidence_path_note(output: object, packet: object | None) -> object:
    paths = _local_kni_evidence_paths(packet)
    if not paths or not hasattr(output, "model_copy"):
        return output
    primary_path = paths[0]
    actions = list(getattr(output, "recommended_actions", []) or [])
    evidence_action = f"Review candidate evidence path: {primary_path}."
    if evidence_action not in actions:
        actions.append(evidence_action)
    diagnostics = dict(getattr(output, "retrieval_diagnostics", {}) or {})
    diagnostics.setdefault("local_only", True)
    diagnostics.setdefault("send_enabled", False)
    diagnostics.setdefault("evidence_path", primary_path)
    diagnostics.setdefault("evidence_paths", paths[:5])
    sources = list(getattr(output, "sources", []) or [])
    if not any(primary_path in str(getattr(source, "title", "") or "") for source in sources):
        sources.append(
            ChiefOfStaffSourceRef(
                title=primary_path,
                source_type="local_kni_document",
                note="Candidate evidence path used for local KNI document synthesis.",
            )
        )
    audit_notes = list(getattr(output, "audit_notes", []) or [])
    audit_note = (
        "Local KNI evidence path was appended after live synthesis omitted an explicit "
        "path; the live model answer was preserved."
    )
    if audit_note not in audit_notes:
        audit_notes.append(audit_note)
    return output.model_copy(
        update={
            "recommended_actions": actions,
            "retrieval_diagnostics": diagnostics,
            "sources": sources,
            "audit_notes": audit_notes,
        }
    )


def _looks_like_local_kni_evidence_lookup(lowered: str) -> bool:
    return looks_like_local_kni_evidence_lookup(lowered)


def _manual_plan_requests_local_kni_evidence(
    manual_plan: ManualRequestPlan | None,
    *,
    input_text: str,
) -> bool:
    """Let a live semantic plan select local KNI retrieval."""

    if manual_request_plan := manual_plan:
        if manual_request_plan.source == "llm":
            return bool(
                manual_request_plan.target_agent == "chief_of_staff"
                and manual_request_plan.intent == "context_lookup"
                and manual_request_plan.target_type == "local_document_collection"
                and manual_request_plan.provider_system == "unspecified"
            )
    return _looks_like_local_kni_evidence_lookup(input_text.lower())


def _fallback_after_unrelated_live_output(
    *,
    input_text: str,
    slack_repo_path: str | None,
    database_url: str | None,
    manual_request_plan: object | None = None,
) -> object:
    result = plan_chief_of_staff_request(
        input_text,
        slack_repo_path=slack_repo_path,
        database_url=database_url,
        manual_request_plan=manual_request_plan,
    )
    audit_notes = [
        *getattr(result, "audit_notes", []),
        (
            "Live SDK output failed orchestrator request-alignment review; "
            "deterministic Chief of Staff fallback was rendered instead."
        ),
    ]
    if hasattr(result, "model_copy"):
        return result.model_copy(update={"audit_notes": audit_notes})
    return result


def _should_fallback_after_live_sdk_exception(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "invalid json when parsing",
            "could not be validated",
            "validation error",
            "model_validate_json",
            "structured output",
        )
    )


def _fallback_after_live_sdk_exception(
    *,
    input_text: str,
    slack_repo_path: str | None,
    database_url: str | None,
    manual_request_plan: object | None = None,
    exc: Exception,
) -> object:
    result = plan_chief_of_staff_request(
        input_text,
        slack_repo_path=slack_repo_path,
        database_url=database_url,
        manual_request_plan=manual_request_plan,
    )
    error_detail = " ".join(str(redact_secrets(str(exc or "")) or "").split())[:500]
    audit_notes = [
        *getattr(result, "audit_notes", []),
        (
            "Live SDK structured output could not be parsed or validated; "
            f"deterministic Chief of Staff fallback was rendered instead ({type(exc).__name__})."
        ),
        *(
            [f"Live SDK validation diagnostic: {error_detail}"]
            if error_detail
            else []
        ),
    ]
    if hasattr(result, "model_copy"):
        return result.model_copy(update={"audit_notes": audit_notes})
    return result


def _local_kni_evidence_packet(
    output: object,
    *,
    query_text: str = "",
    max_candidate_documents: int = 5,
) -> dict[str, object]:
    return build_local_kni_evidence_packet(
        output,
        query_text=query_text,
        max_candidate_documents=max_candidate_documents,
    )


def _local_kni_evidence_packet_for_query(
    query_text: str,
    *,
    max_candidate_documents: int = 5,
) -> dict[str, object]:
    return build_local_kni_evidence_packet_for_query(
        query_text,
        max_candidate_documents=max_candidate_documents,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == RunMode.LIVE.value and not args.live_sdk:
        raise SystemExit(
            "Use --live-sdk for explicit Chief of Staff model execution. "
            "Live side-effect mode is not supported."
        )

    raw_input_text = _read_input(args.input)
    cost_directive = parse_cost_tracking_directive(raw_input_text)
    input_text = (cost_directive.cleaned_text or raw_input_text).strip()
    orchestrator_preflight = load_orchestrator_preflight_from_env()
    parent_manual_plan = load_manual_request_plan_from_env()
    specialist_execution_context = load_specialist_execution_context_from_env()
    local_kni_lookup = False
    if args.live_sdk:
        load_settings(force_dotenv=True)
        live_web_research_enabled = bool(
            args.live_search and not _request_forbids_live_web_research(input_text)
        )
        _configure_live_web_research_env(enabled=live_web_research_enabled)
        model_config = get_runtime_agent_model_config("chief_of_staff", model_override=args.model)
        sdk_session = _chief_of_staff_session_from_args(args)
        if parent_manual_plan is not None:
            manual_plan = parent_manual_plan
        else:
            preflight = run_orchestrator_preflight(
                input_text,
                requested_agent="chief_of_staff",
                live_manual_plan=True,
                model=args.model,
                session=sdk_session,
                database_url=args.database_url,
            )
            manual_plan = preflight.manual_request_plan
            orchestrator_preflight = compact_orchestrator_preflight_payload(preflight)
        local_kni_lookup = _manual_plan_requests_local_kni_evidence(
            manual_plan,
            input_text=input_text,
        )
        budget = chief_of_staff_quality_budget(
            args.quality,
            request_text=input_text,
            live_sdk=True,
            manual_request_plan=manual_plan,
        )
        if (
            ExecutionIntentAuthority.from_value(manual_plan).canonical
            and manual_plan.provider_system == "google_calendar"
            and manual_plan.intent == "context_lookup"
        ):
            calendar_resolution = resolve_calendar_action_plan(
                input_text,
                infer_calendar_action_plan(input_text),
                manual_plan=manual_plan,
                semantic_candidate=True,
                live=True,
                model=args.model,
            )
            if calendar_resolution.plan is not None:
                return _run_interpreted_calendar_action(
                    input_text=input_text,
                    plan=calendar_resolution.plan,
                    json_output=args.json,
                    openai_requests=(
                        (0 if parent_manual_plan is not None else 1)
                        + calendar_resolution.openai_requests
                    ),
                    model_override=args.model,
                    manual_plan=manual_plan,
                )
        gmail_plan = resolve_gmail_execution_plan(
            input_text,
            manual_plan=manual_plan,
        )
        if gmail_plan.operation == "contact_lookup":
            return _run_interpreted_gmail_contact_lookup(
                input_text=input_text,
                manual_plan=manual_plan,
                gmail_plan=gmail_plan,
                orchestrator_preflight=orchestrator_preflight,
                quality_budget=budget,
                model_override=args.model,
                json_output=args.json,
            )
        if gmail_plan.operation == "priority_grouping":
            return _run_interpreted_gmail_priority_grouping(
                input_text=input_text,
                manual_plan=manual_plan,
                gmail_plan=gmail_plan,
                orchestrator_preflight=orchestrator_preflight,
                quality_budget=budget,
                model_override=args.model,
                json_output=args.json,
            )
        typed_result = None
        tool_receipts: list[dict[str, object]] = []
        original_review = None
        local_kni_evidence = None
        web_query_plan = None
        if local_kni_lookup:
            local_kni_evidence = _local_kni_evidence_packet_for_query(input_text)
            orchestrator_preflight = orchestrator_preflight or {
                "local_kni_evidence_prefetch": True,
                "note": (
                    "Local KNI retrieval packaged bounded evidence directly from "
                    "the raw user query before live Chief of Staff synthesis."
                ),
            }
        elif (
            args.live_search_plan
            and live_web_research_enabled
            and _chief_should_build_web_query_plan(input_text, manual_plan)
        ):
            web_query_plan_subject = _chief_web_query_plan_subject(input_text, manual_plan)
            web_query_plan = resolve_web_query_plan(
                subject=web_query_plan_subject,
                request_text=input_text,
                fallback_queries=_chief_fallback_web_queries(input_text, web_query_plan_subject),
                max_queries=12 if budget.mode.value == "deep" else 8,
                live=True,
                model=args.model,
                planner_context=json.dumps(
                    orchestrator_preflight or {},
                    ensure_ascii=True,
                    sort_keys=True,
                )[:4000],
            )
        try:
            preacquired_calendar_read = None
            if (
                ExecutionIntentAuthority.from_value(manual_plan).canonical
                and manual_plan.provider_system == "google_calendar"
                and manual_plan.intent == "context_lookup"
            ):
                preacquired_calendar_read = _execute_required_calendar_context_lookup(
                    manual_plan,
                    input_text=input_text,
                )
                if preacquired_calendar_read is not None:
                    tool_receipts.append(preacquired_calendar_read)
                    budget = _verified_provider_synthesis_budget(budget)
            typed_input: dict[str, object] = {
                "request": input_text,
                "slack_repo_path": args.slack_repo_path,
                "approval_reference": _approval_reference_for_request(input_text),
                "side_effect_policy": _live_side_effect_policy(input_text, manual_plan),
                "live_web_research_enabled": live_web_research_enabled,
                "attach_tools": _chief_of_staff_should_attach_tools(
                    input_text,
                    manual_plan,
                ),
                "include_specialist_tools": chief_of_staff_should_use_specialist_tools(
                    input_text,
                    manual_plan,
                ),
                "manual_request_plan": manual_plan.model_dump(mode="json"),
                "orchestrator_preflight": orchestrator_preflight,
                "runtime_source_layer_policy": runtime_source_layer_policy_context(
                    "chief_of_staff"
                ),
            }
            if manual_plan.provider_system == "google_calendar":
                typed_input["provider_execution_contract"] = {
                    "provider_system": "google_calendar",
                    "intent": manual_plan.intent,
                    "primary_target": manual_plan.primary_target,
                    "execution_required": preacquired_calendar_read is None,
                    "execution_complete": preacquired_calendar_read is not None,
                    "live": True,
                    "approval_reference": typed_input["approval_reference"],
                    "instruction": (
                        (
                            "The typed Calendar read already completed. Answer from "
                            "verified_provider_context and do not call any provider tool."
                        )
                        if preacquired_calendar_read is not None
                        else (
                            "Interpret the raw operator request and bounded execution "
                            "context. For business_system_write, call the one required "
                            "Calendar mutation tool with live=true and the supplied "
                            "approval_reference. Use provider output for the answer; do "
                            "not return a plan when the scoped operation can execute."
                        )
                    ),
                }
            if preacquired_calendar_read is not None:
                typed_input["verified_provider_context"] = preacquired_calendar_read
                typed_input["verified_provider_context_instruction"] = (
                    "This is the current typed Google Calendar provider result. Synthesize "
                    "the requested answer from it. Provider truth outranks prior Slack text."
                )
                typed_input["attach_tools"] = False
            if specialist_execution_context:
                typed_input["execution_context"] = specialist_execution_context
                typed_input["execution_context_instruction"] = (
                    "Use the bounded execution context to resolve references such as "
                    "'original', 'above', 'same', and 'previous'. The current operator "
                    "request remains authoritative. Human thread-root facts outrank prior "
                    "agent replies, route metadata, and stale WorkItem state."
                )
            if local_kni_evidence is not None:
                typed_input["local_kni_evidence_packet"] = local_kni_evidence
                typed_input["local_kni_instruction"] = local_kni_live_instruction()
            if web_query_plan is not None:
                typed_input["web_query_plan"] = web_query_plan.model_dump(mode="json")
                typed_input["web_query_plan_instruction"] = (
                    "Use this bounded query plan for public web discovery when the "
                    "request requires current source-backed research. Run the most "
                    "relevant planned queries with search_web, then read/extract "
                    "selected URLs before synthesis when tools and budget allow."
                )
            typed_result = run_chief_of_staff_sdk(
                typed_input,
                live=True,
                model=args.model,
                quality_budget=budget,
                session=sdk_session,
                force_sdk_interpretation=True,
                manual_request_plan=manual_plan,
                include_specialist_tools=bool(typed_input["include_specialist_tools"]),
                attach_tools=bool(typed_input["attach_tools"]),
            )
            result = typed_result.output
            tool_receipts = _merge_tool_receipts(
                tool_receipts,
                _sdk_tool_receipts(typed_result.raw_result),
                list(typed_result.tool_receipts or []),
            )
            if (
                manual_plan.provider_system == "google_calendar"
                and manual_plan.intent == "context_lookup"
                and not any(
                    str(receipt.get("operation") or "").startswith(
                        ("read_calendar", "resolve_calendar")
                    )
                    for receipt in tool_receipts
                )
            ):
                required_read = _execute_required_calendar_context_lookup(
                    manual_plan,
                    input_text=input_text,
                )
                if required_read is not None:
                    tool_receipts.append(required_read)
            result = append_visible_source_urls_to_output(result)
            result = _with_cost_tracking_note(
                result,
                requested=cost_directive.requested,
                usage=typed_result.usage,
                cost=typed_result.cost,
            )
            review = _chief_of_staff_output_review(
                input_text=input_text,
                output=result,
                run_type="live_sdk",
            )
            local_kni_missing_path = local_kni_lookup and not _output_mentions_local_kni_evidence_path(result)
            review_mismatch = bool(
                _review_detected_unrelated_output(review)
                or (
                    _semantic_reference_capture_mismatch(
                        manual_plan,
                        result,
                        input_text=input_text,
                    )
                    and not local_kni_missing_path
                )
            )
            if review_mismatch:
                original_review = review
                if manual_plan.source == "llm":
                    result = _with_live_review_diagnostic(result, review)
                else:
                    result = _fallback_after_unrelated_live_output(
                        input_text=input_text,
                        slack_repo_path=args.slack_repo_path,
                        database_url=args.database_url,
                        manual_request_plan=manual_plan,
                    )
                    review = _chief_of_staff_output_review(
                        input_text=input_text,
                        output=result,
                        run_type="deterministic_fallback_after_live_review",
                    )
            elif local_kni_missing_path:
                result = _with_local_kni_evidence_path_note(result, local_kni_evidence)
                review = _chief_of_staff_output_review(
                    input_text=input_text,
                    output=result,
                    run_type="live_sdk_with_local_kni_evidence_path_repair",
                )
        except Exception as exc:
            if not _should_fallback_after_live_sdk_exception(exc):
                raise
            result = _fallback_after_live_sdk_exception(
                input_text=input_text,
                slack_repo_path=args.slack_repo_path,
                database_url=args.database_url,
                manual_request_plan=manual_plan,
                exc=exc,
            )
            result = append_visible_source_urls_to_output(result)
            review = _chief_of_staff_output_review(
                input_text=input_text,
                output=result,
                run_type="deterministic_fallback_after_live_exception",
            )
        result, delegated_result = _maybe_execute_recommended_work_item_handoff(
            result,
            input_text=input_text,
            database_url=args.database_url,
            live_sdk=args.live_sdk,
            manual_request_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
        )
        if delegated_result is not None:
            review = _chief_of_staff_output_review(
                input_text=input_text,
                output=result,
                run_type="live_sdk_delegated_work_item_handoff",
            )
        payload = _payload(
            mode="live_sdk",
            live_sdk=True,
            model=model_config.as_log_dict(),
            output=result,
            input_text=input_text,
            quality_budget=budget,
            manual_request_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            orchestrator_review=review,
            original_orchestrator_review=original_review,
            usage=typed_result.usage if typed_result is not None else None,
            cost=typed_result.cost if typed_result is not None else None,
            request_cache=typed_result.request_cache if typed_result is not None else None,
            web_query_plan=web_query_plan,
            delegated_work_item_result=delegated_result,
            tool_receipts=tool_receipts,
        )
    else:
        if args.mode != RunMode.DRY_RUN.value:
            raise SystemExit("Only dry-run planning and --live-sdk model planning are supported.")
        manual_plan = parent_manual_plan or resolve_manual_request_plan(
            input_text,
            requested_agent="chief_of_staff",
            live=False,
        )
        budget = chief_of_staff_quality_budget(
            args.quality,
            request_text=input_text,
            live_sdk=False,
            manual_request_plan=manual_plan,
        )
        result = plan_chief_of_staff_request(
            input_text,
            slack_repo_path=args.slack_repo_path,
            database_url=args.database_url,
            manual_request_plan=manual_plan,
        )
        result = append_visible_source_urls_to_output(result)
        result = _with_cost_tracking_note(
            result,
            requested=cost_directive.requested,
            usage=None,
            cost=None,
        )
        review = _chief_of_staff_output_review(
            input_text=input_text,
            output=result,
            run_type="dry_run",
        )
        result, delegated_result = _maybe_execute_recommended_work_item_handoff(
            result,
            input_text=input_text,
            database_url=args.database_url,
            live_sdk=False,
            manual_request_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
        )
        if delegated_result is not None:
            review = _chief_of_staff_output_review(
                input_text=input_text,
                output=result,
                run_type="dry_run_delegated_work_item_handoff",
            )
        payload = _payload(
            mode="dry_run",
            live_sdk=False,
            model="fixture",
            output=result,
            input_text=input_text,
            quality_budget=budget,
            manual_request_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            orchestrator_review=review,
            delegated_work_item_result=delegated_result,
            tool_receipts=[],
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(render_chief_of_staff_result(result))
    return 0


def _handle_uncaught_exception(exc: Exception, argv: list[str] | None = None) -> int:
    failure = known_exception_to_operator_failure(exc, context="Chief of Staff run")
    if "--json" in set(argv or []):
        print(
            json.dumps(
                {
                    "status": "failed",
                    "output": {
                        "failure": failure.to_dict(),
                        "summary": failure.summary,
                        "next_step": failure.next_step,
                    },
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    print(failure.summary, file=sys.stderr)
    print(f"Reason: {failure.reason}", file=sys.stderr)
    print(f"Next step: {failure.next_step}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(_handle_uncaught_exception(exc, sys.argv[1:])) from exc
