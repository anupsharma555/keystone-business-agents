"""Run the KNI Chief of Staff agent in deterministic or explicit live-SDK mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from keystone_agents.agents.chief_of_staff import (
    chief_of_staff_should_use_specialist_tools,
    plan_chief_of_staff_request,
    render_chief_of_staff_result,
    run_chief_of_staff_sdk,
)
from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.agents.orchestrator import review_specialist_output, run_orchestrator_preflight
from keystone_agents.agents.web_query_planner import resolve_web_query_plan
from keystone_agents.cli_sdk import add_sdk_session_arguments, sdk_session_from_args
from keystone_agents.config import load_settings
from keystone_agents.cost_tracking import parse_cost_tracking_directive
from keystone_agents.finance_expense_receipts import infer_finance_expense_receipt_target
from keystone_agents.local_kni_evidence import (
    build_local_kni_evidence_packet,
    build_local_kni_evidence_packet_for_query,
    local_kni_evidence_paths,
    local_kni_live_instruction,
    looks_like_local_kni_evidence_lookup,
)
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.models import RunMode
from keystone_agents.operator_failures import known_exception_to_operator_failure
from keystone_agents.orchestrator.preflight_context import (
    compact_orchestrator_preflight_payload,
    load_manual_request_plan_from_env,
    load_orchestrator_preflight_from_env,
)
from keystone_agents.quality_budget import AgentQualityBudget, chief_of_staff_quality_budget
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
    ChiefOfStaffSourceRef,
)
from keystone_agents.source_layer_context import runtime_source_layer_policy_context
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


def _live_side_effect_policy(input_text: str) -> str:
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


def _request_forbids_live_web_research(input_text: str) -> bool:
    normalized = " ".join(str(input_text or "").lower().split())
    if not normalized:
        return False
    return bool(
        re.search(
            r"\b(?:do\s+not|don't|dont|never|no|without|avoid|skip)\b"
            r"[^.;\n]{0,180}\b"
            r"(?:web\s+search|live\s+web|external\s+(?:search|research|tools?)|"
            r"browser\s+automation|research\s+externally)\b",
            normalized,
        )
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
    if infer_finance_expense_receipt_target(input_text) is not None:
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
) -> dict[str, object]:
    dumped = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
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
    if quality_budget is not None:
        payload["quality_budget"] = quality_budget.model_dump(mode="json")
    if usage is not None:
        payload["usage"] = usage
    if cost is not None:
        payload["cost"] = cost
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
    return payload


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
    if delegated_route is None:
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
    audit_notes = [
        *getattr(result, "audit_notes", []),
        (
            "Live SDK structured output could not be parsed or validated; "
            f"deterministic Chief of Staff fallback was rendered instead ({type(exc).__name__})."
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
    local_kni_lookup = _looks_like_local_kni_evidence_lookup(input_text.lower())
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
        elif local_kni_lookup:
            manual_plan = resolve_manual_request_plan(
                input_text,
                requested_agent="chief_of_staff",
                live=False,
            )
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
        budget = chief_of_staff_quality_budget(
            args.quality,
            request_text=input_text,
            live_sdk=True,
        )
        typed_result = None
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
            typed_input: dict[str, object] = {
                "request": input_text,
                "slack_repo_path": args.slack_repo_path,
                "approval_reference": _approval_reference_for_request(input_text),
                "side_effect_policy": _live_side_effect_policy(input_text),
                "live_web_research_enabled": live_web_research_enabled,
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
            )
            result = typed_result.output
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
            if _review_detected_unrelated_output(review) or (
                _reference_capture_mismatch(input_text, result) and not local_kni_missing_path
            ):
                original_review = review
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
        )
    else:
        if args.mode != RunMode.DRY_RUN.value:
            raise SystemExit("Only dry-run planning and --live-sdk model planning are supported.")
        budget = chief_of_staff_quality_budget(
            args.quality,
            request_text=input_text,
            live_sdk=False,
        )
        manual_plan = parent_manual_plan or resolve_manual_request_plan(
            input_text,
            requested_agent="chief_of_staff",
            live=False,
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
