"""Package-native Keystone command line entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.agent_registry import AGENT_REGISTRY, agent_cards
from keystone_agents.agents.chief_of_staff import (
    plan_chief_of_staff_request,
)
from keystone_agents.agents.orchestrator import (
    OrchestratorPreflight,
    build_orchestrator_agent,
    review_specialist_output,
    route_request,
    run_orchestrator_preflight,
    run_orchestrator_sdk,
)
from keystone_agents.automation_inventory import (
    build_automation_inventory_report,
    ensure_default_automation_inventory,
    render_automation_inventory_markdown,
)
from keystone_agents.child_process import run_isolated_child_process
from keystone_agents.cli_sdk import add_sdk_session_arguments
from keystone_agents.config import cli_default_live_research, cli_default_live_sdk, load_settings
from keystone_agents.cost_tracking import parse_cost_tracking_directive
from keystone_agents.evals import generate_eval_report, run_static_evals
from keystone_agents.gmail_triage.execution_plan import infer_gmail_execution_plan
from keystone_agents.health import format_health_report, report_to_json, run_health_check
from keystone_agents.models import RunMode
from keystone_agents.orchestrator.preflight_context import (
    compact_orchestrator_preflight_payload,
    orchestrator_preflight_env,
)
from keystone_agents.outreach_composer.execution_plan import infer_outreach_execution_plan
from keystone_agents.reporting import (
    render_markdown_table,
    render_work_item_result_text,
    sensitive_text_summary,
)
from keystone_agents.schemas.approval import (
    ApprovalQueueItem,
    ApprovalQueueStatus,
    ApprovalScope,
    ApprovalState,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.sdk_sessions import (
    SDKSessionSpec,
    build_sdk_session,
    context_file_session_components,
    default_cli_ask_session_components,
    resolve_sdk_session_spec,
    sdk_session_env,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.storage.sqlite_store import redact_secrets
from keystone_agents.tools.slack_tool import SlackTool, slack_review_message_from_approval_item
from keystone_agents.tools.storage_tool import StorageTool
from keystone_agents.work_items import (
    approve_artifact_context,
    attach_artifact,
    derive_case_status,
    drafting_ready,
    parse_artifact_spec,
    record_event,
    select_artifact,
    set_next_action,
)
from keystone_agents.workflow_runner import advance_work_item, advance_work_item_manager_loop
from keystone_agents.workflows import (
    pipeline_markdown_report,
    run_keystone_pipeline,
    run_opportunity_to_outreach_loop,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level Keystone CLI parser."""

    parser = argparse.ArgumentParser(description="Keystone business-agent operations.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Initialize local SQLite storage.")
    init_db.add_argument("--database-url", default=None, help="SQLite URL.")
    init_db.set_defaults(func=_run_init_db)

    health = subparsers.add_parser("health", help="Run an offline health check.")
    health.add_argument("--database-url", default=None, help="SQLite URL or :memory:.")
    health.add_argument("--json", action="store_true", help="Print JSON.")
    health.add_argument("--verbose", action="store_true", help="Print detailed text output.")
    health.set_defaults(func=_run_health)

    route = subparsers.add_parser("route", help="Route a request in deterministic dry-run mode.")
    route.add_argument(
        "--mode",
        choices=[RunMode.DRY_RUN.value, RunMode.LIVE.value],
        default="dry-run",
    )
    route.add_argument("--input", default="", help="Request text or a local fixture path.")
    route.add_argument("--save", action="store_true", help="Save route decision to SQLite.")
    route.add_argument("--ask-feedback", action="store_true", help="Attach a feedback request.")
    route.add_argument("--database-url", default=None, help="SQLite URL for --save.")
    route.set_defaults(func=_run_route)

    ask = subparsers.add_parser(
        "ask",
        help="Send a natural-language request to @KNI or a named Keystone agent.",
    )
    ask.add_argument(
        "prompt", nargs="*", help="Natural-language request, optionally @KNI-prefixed."
    )
    ask.add_argument("--input", default="", help="Request text or a local fixture path.")
    ask.add_argument(
        "--agent",
        choices=sorted(AGENT_REGISTRY),
        default=None,
        help="Target agent route. Overrides an @KNI mention.",
    )
    ask.add_argument(
        "--live-sdk",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Run the selected agent through live SDK model execution. Defaults to on in "
            "live-test/full-live mode; use --no-live-sdk to force dry-run selection."
        ),
    )
    add_sdk_session_arguments(ask)
    ask.add_argument(
        "--live-manual-plan",
        action="store_true",
        help="Use the SDK manual-request planner before direct agent execution.",
    )
    ask.add_argument("--database-url", default=None, help="SQLite URL for WorkItem mode.")
    ask.add_argument(
        "--context-file",
        default="",
        help="Optional selected-context JSON file to attach to a WorkItem run.",
    )
    ask.add_argument("--live-search", action="store_true", help="Use live search in WorkItem mode.")
    ask.add_argument("--max-results", type=int, default=3, help="Max WorkItem search results.")
    ask.add_argument(
        "--max-manager-steps",
        type=int,
        default=3,
        help="Max bounded manager-loop WorkItem steps for @KNI ask mode.",
    )
    ask.add_argument("--save", action="store_true", help="Save workflow artifacts when supported.")
    ask.add_argument(
        "--approval-channel",
        default="#ai-agents-workflow",
        help="Approval channel for workflow review packets.",
    )
    ask.add_argument(
        "--request-approval",
        action="store_true",
        help="Queue and preview approval packets for supported workflows.",
    )
    ask.add_argument(
        "--live-slack",
        action="store_true",
        help="Post workflow approval packets to Slack when approval is requested.",
    )
    ask.add_argument("--json", action="store_true", help="Print JSON.")
    ask.set_defaults(func=_run_ask)

    pipeline = subparsers.add_parser("pipeline", help="Run the dry-run fixture pipeline.")
    pipeline.add_argument("--email-fixture", required=True, help="Inbound email fixture path.")
    pipeline.add_argument("--company-fixture", default=None, help="Optional company fixture JSON.")
    pipeline.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    pipeline.add_argument("--sdk", action="store_true", help="Construct SDK agents.")
    pipeline.add_argument("--save", action="store_true", help="Save artifacts to SQLite.")
    pipeline.add_argument("--database-url", default=None, help="SQLite URL for --save.")
    pipeline.add_argument("--ask-feedback", action="store_true", help="Attach feedback requests.")
    pipeline.add_argument("--markdown", action="store_true", help="Print markdown report.")
    pipeline.add_argument(
        "--approval-state",
        choices=[
            state.value
            for state in ApprovalState
            if state
            not in {
                ApprovalState.APPROVED_FOR_EXTERNAL_USE,
                ApprovalState.APPROVED_FOR_SEND,
            }
        ],
        default=ApprovalState.PENDING.value,
    )
    pipeline.set_defaults(func=_run_pipeline)

    evals = subparsers.add_parser("evals", help="Run deterministic static evals.")
    evals.add_argument(
        "--agent",
        choices=["all", "gmail", "company", "scout", "outreach"],
        default="all",
    )
    evals.add_argument("--json", action="store_true", help="Print JSON.")
    evals.add_argument("--markdown", action="store_true", help="Print markdown.")
    evals.set_defaults(func=_run_evals)

    approvals = subparsers.add_parser("approvals", help="List approval queue items.")
    approvals.add_argument(
        "--status",
        choices=[status.value for status in ApprovalQueueStatus] + ["all"],
        default=ApprovalQueueStatus.PENDING.value,
    )
    approvals.add_argument("--object-type", default=None)
    approvals.add_argument("--source-agent", default=None)
    approvals.add_argument("--database-url", default=None)
    approvals.add_argument("--json", action="store_true", help="Print sanitized JSON.")
    approvals.set_defaults(func=_run_approvals)

    work_items = subparsers.add_parser("work-items", help="Inspect and advance WorkItems.")
    work_item_subparsers = work_items.add_subparsers(dest="work_items_command", required=True)
    work_items_list = work_item_subparsers.add_parser("list", help="List WorkItems.")
    work_items_list.add_argument("--status", default="all")
    work_items_list.add_argument("--kind", default=None)
    work_items_list.add_argument("--limit", type=int, default=20)
    work_items_list.add_argument("--database-url", default=None)
    work_items_list.add_argument("--json", action="store_true", help="Print JSON.")
    work_items_list.set_defaults(func=_run_work_items_list)

    work_items_show = work_item_subparsers.add_parser("show", help="Show one WorkItem.")
    work_items_show.add_argument("work_item_id")
    work_items_show.add_argument("--database-url", default=None)
    work_items_show.add_argument("--json", action="store_true", help="Print JSON.")
    work_items_show.set_defaults(func=_run_work_items_show)

    work_items_artifacts = work_item_subparsers.add_parser(
        "artifacts",
        help="List WorkItem artifact references.",
    )
    work_items_artifacts.add_argument("work_item_id")
    work_items_artifacts.add_argument("--database-url", default=None)
    work_items_artifacts.add_argument("--json", action="store_true", help="Print JSON.")
    work_items_artifacts.set_defaults(func=_run_work_items_artifacts)

    work_items_timeline = work_item_subparsers.add_parser(
        "timeline",
        help="List WorkItem audit events.",
    )
    work_items_timeline.add_argument("work_item_id")
    work_items_timeline.add_argument("--database-url", default=None)
    work_items_timeline.add_argument("--json", action="store_true", help="Print JSON.")
    work_items_timeline.set_defaults(func=_run_work_items_timeline)

    work_items_advance = work_item_subparsers.add_parser(
        "advance",
        help="Advance a WorkItem or create one from request text.",
    )
    work_items_advance.add_argument("work_item_id", nargs="?")
    work_items_advance.add_argument(
        "--input", default="", help="Request text or local fixture path."
    )
    work_items_advance.add_argument(
        "--context-file",
        default="",
        help="Optional selected-context JSON file to attach to this WorkItem advance.",
    )
    work_items_advance.add_argument("--database-url", default=None)
    work_items_advance.add_argument("--live-search", action="store_true")
    work_items_advance.add_argument("--live-sdk", action="store_true")
    add_sdk_session_arguments(work_items_advance)
    work_items_advance.add_argument(
        "--langgraph",
        action="store_true",
        help="Use the optional LangGraph WorkItem orchestration wrapper.",
    )
    work_items_advance.add_argument("--max-results", type=int, default=3)
    work_items_advance.add_argument(
        "--max-manager-steps",
        type=int,
        default=3,
        help="Max bounded manager-loop WorkItem steps.",
    )
    work_items_advance.add_argument("--json", action="store_true", help="Print JSON.")
    work_items_advance.set_defaults(func=_run_work_items_advance)

    work_items_attach = work_item_subparsers.add_parser(
        "attach",
        help="Attach an existing saved artifact to a WorkItem.",
    )
    work_items_attach.add_argument("work_item_id")
    work_items_attach.add_argument(
        "--artifact", required=True, help="Artifact spec, e.g. company_profile:12."
    )
    work_items_attach.add_argument(
        "--select", action="store_true", help="Select this artifact after attaching."
    )
    work_items_attach.add_argument("--database-url", default=None)
    work_items_attach.add_argument("--json", action="store_true", help="Print JSON.")
    work_items_attach.set_defaults(func=_run_work_items_attach)

    work_items_select = work_item_subparsers.add_parser(
        "select",
        help="Select one attached artifact for the next WorkItem step.",
    )
    work_items_select.add_argument("work_item_id")
    work_items_select.add_argument(
        "--artifact", required=True, help="Artifact spec, e.g. opportunity:12."
    )
    work_items_select.add_argument("--database-url", default=None)
    work_items_select.add_argument("--json", action="store_true", help="Print JSON.")
    work_items_select.set_defaults(func=_run_work_items_select)

    work_items_approve = work_item_subparsers.add_parser(
        "approve-context",
        help="Approve one attached artifact for WorkItem drafting context.",
    )
    work_items_approve.add_argument("work_item_id")
    work_items_approve.add_argument(
        "--artifact", required=True, help="Artifact spec, e.g. company_profile:12."
    )
    work_items_approve.add_argument(
        "--state",
        choices=[
            ApprovalState.APPROVED_FOR_DRAFTING.value,
            ApprovalState.APPROVED_FOR_EXTERNAL_USE.value,
        ],
        default=ApprovalState.APPROVED_FOR_DRAFTING.value,
    )
    work_items_approve.add_argument("--reviewer", default="cli")
    work_items_approve.add_argument("--notes", default="")
    work_items_approve.add_argument("--database-url", default=None)
    work_items_approve.add_argument("--json", action="store_true", help="Print JSON.")
    work_items_approve.set_defaults(func=_run_work_items_approve_context)

    automations = subparsers.add_parser("automations", help="Inspect Keystone automations.")
    automation_subparsers = automations.add_subparsers(
        dest="automations_command",
        required=True,
    )
    automation_list = automation_subparsers.add_parser("list", help="List automation specs.")
    automation_list.add_argument("--status", default="all")
    automation_list.add_argument("--database-url", default=None)
    automation_list.add_argument("--json", action="store_true", help="Print JSON.")
    automation_list.set_defaults(func=_run_automations_list)

    automation_runs = automation_subparsers.add_parser("runs", help="List automation runs.")
    automation_runs.add_argument("--automation-id", default=None)
    automation_runs.add_argument("--status", default="all")
    automation_runs.add_argument("--limit", type=int, default=20)
    automation_runs.add_argument("--database-url", default=None)
    automation_runs.add_argument("--json", action="store_true", help="Print JSON.")
    automation_runs.set_defaults(func=_run_automations_runs)

    automation_audit = automation_subparsers.add_parser(
        "audit",
        help="Build a Chief of Staff automation inventory report.",
    )
    automation_audit.add_argument("--channel", action="append", default=[])
    automation_audit.add_argument("--limit", type=int, default=20)
    automation_audit.add_argument("--database-url", default=None)
    automation_audit.add_argument("--json", action="store_true", help="Print JSON.")
    automation_audit.set_defaults(func=_run_automations_audit)

    agents = subparsers.add_parser("agents", help="Inspect registered agents.")
    agent_subparsers = agents.add_subparsers(dest="agents_command", required=True)
    agents_list = agent_subparsers.add_parser("list", help="List registered agent cards.")
    agents_list.add_argument("--json", action="store_true", help="Print JSON.")
    agents_list.set_defaults(func=_run_agents_list)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Keystone CLI."""

    args = build_parser().parse_args(argv)
    return int(args.func(args))


def _run_init_db(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    print(f"Initialized SQLite database: {store.path}")
    print(f"Schema version: {store.current_schema_version()}")
    return 0


def _run_health(args: argparse.Namespace) -> int:
    report = run_health_check(database_url=args.database_url)
    if args.json:
        print(report_to_json(report))
    else:
        print(format_health_report(report, verbose=args.verbose))
    return 1 if report.overall_status == "error" else 0


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


def _run_route(args: argparse.Namespace) -> int:
    if args.mode != RunMode.DRY_RUN.value:
        raise SystemExit("Live orchestrator mode is not implemented. Use deterministic dry-run.")

    agent = build_orchestrator_agent()
    input_text = _read_input(args.input)
    result = route_request(input_text, include_operator_feedback_request=args.ask_feedback)
    if args.save:
        StorageTool(args.database_url).save_agent_run(
            agent_name="orchestrator",
            input_payload={"input": input_text},
            input_summary=input_text[:180],
            output=result.model_dump(),
            model="fixture",
            dry_run=True,
            status="success",
        )
    print(f"Agent: {agent.name}")
    print(f"Route: {result.route}")
    print(f"Target: {result.target_agent or 'clarification'}")
    print(f"Send enabled: {result.send_enabled}")
    return 0


def _ask_input(args: argparse.Namespace) -> str:
    if args.input:
        return _read_input(args.input)
    return " ".join(args.prompt).strip()


def _agent_display_name(route: str) -> str:
    spec = AGENT_REGISTRY.get(route)
    return spec.agent_name if spec is not None else route


def _run_ask(args: argparse.Namespace) -> int:
    raw_input = _ask_input(args)
    mention = parse_agent_mention(raw_input)
    input_text = raw_input if args.agent else mention.input_text
    cost_directive = parse_cost_tracking_directive(input_text)
    input_text = (cost_directive.cleaned_text or input_text).strip()
    live_sdk = _ask_live_sdk_enabled(args)
    live_manual_plan = args.live_manual_plan or live_sdk
    live_search = args.live_search or (live_sdk and cli_default_live_research())
    if live_manual_plan:
        load_settings(force_dotenv=True)
    requested_route = args.agent or (mention.route if mention.explicit else None)
    orchestrator_preflight = run_orchestrator_preflight(
        input_text,
        requested_agent=requested_route,
        live_manual_plan=live_manual_plan,
        database_url=args.database_url,
    )
    manual_plan = orchestrator_preflight.manual_request_plan
    if _preflight_blocks_execution(orchestrator_preflight):
        return _print_ask_preflight_blocked(
            input_text,
            json_output=args.json,
            orchestrator_preflight=orchestrator_preflight,
        )
    if args.agent is None:
        if _should_run_opportunity_to_outreach_loop(
            manual_plan,
            explicit_route=mention.route if mention.explicit else None,
        ):
            return _run_ask_opportunity_to_outreach_loop(
                input_text,
                live_search=live_search,
                live_sdk=live_sdk,
                json_output=args.json,
                manual_plan=manual_plan,
                database_url=args.database_url,
                save=args.save,
                request_approval=args.request_approval,
                approval_channel=args.approval_channel,
                live_slack=args.live_slack,
                orchestrator_preflight=orchestrator_preflight,
                sdk_session_spec=_sdk_session_spec_for_ask(
                    args,
                    route="orchestrator",
                    default_enabled=False,
                ),
                cost_tracking_requested=cost_directive.requested,
            )
        if live_sdk and mention.explicit and mention.route is not None:
            route = str(mention.route)
            if manual_plan.intent == "browser_diagnostics" and manual_plan.target_agent in {
                "chief_of_staff",
                "orchestrator",
            }:
                route = manual_plan.target_agent
            if route == "orchestrator":
                return _run_ask_orchestrator(
                    input_text,
                    live_sdk=True,
                    json_output=args.json,
                    manual_plan=manual_plan,
                    orchestrator_preflight=orchestrator_preflight,
                    sdk_session_spec=_sdk_session_spec_for_ask(
                        args,
                        route="orchestrator",
                        default_enabled=False,
                    ),
                )
            return _run_ask_specialist_live(
                route,
                input_text,
                json_output=args.json,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                sdk_session_spec=_sdk_session_spec_for_ask(
                    args,
                    route=route,
                    default_enabled=_ask_route_session_default(route),
                ),
                cost_tracking_requested=cost_directive.requested,
            )
        return _run_ask_work_item(
            input_text,
            database_url=args.database_url,
            live_search=live_search,
            live_sdk=live_sdk,
            max_results=args.max_results,
            max_manager_steps=args.max_manager_steps,
            json_output=args.json,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            context_file_path=args.context_file,
            sdk_session_enabled=args.sdk_session,
            sdk_session_id=args.sdk_session_id,
            sdk_session_db_path=args.sdk_session_db,
            cost_tracking_requested=cost_directive.requested,
        )
    route = args.agent
    if route == "orchestrator":
        return _run_ask_orchestrator(
            input_text,
            live_sdk=live_sdk,
            json_output=args.json,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=_sdk_session_spec_for_ask(
                args,
                route="orchestrator",
                default_enabled=False,
            ),
            cost_tracking_requested=cost_directive.requested,
        )
    if live_sdk:
        if manual_plan.intent == "browser_diagnostics" and manual_plan.target_agent in {
            "chief_of_staff",
            "orchestrator",
        }:
            route = manual_plan.target_agent
            if route == "orchestrator":
                return _run_ask_orchestrator(
                    input_text,
                    live_sdk=True,
                    json_output=args.json,
                    manual_plan=manual_plan,
                    orchestrator_preflight=orchestrator_preflight,
                    sdk_session_spec=_sdk_session_spec_for_ask(
                        args,
                        route="orchestrator",
                        default_enabled=False,
                    ),
                    cost_tracking_requested=cost_directive.requested,
                )
        return _run_ask_specialist_live(
            route,
            input_text,
            json_output=args.json,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=_sdk_session_spec_for_ask(
                args,
                route=route,
                default_enabled=_ask_route_session_default(route),
            ),
            cost_tracking_requested=cost_directive.requested,
        )
    return _print_ask_dry_run(
        route,
        input_text,
        json_output=args.json,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        database_url=args.database_url,
    )


def _ask_live_sdk_enabled(args: argparse.Namespace) -> bool:
    explicit = getattr(args, "live_sdk", None)
    if explicit is not None:
        return bool(explicit)
    return cli_default_live_sdk()


def _sdk_session_spec_for_ask(
    args: argparse.Namespace,
    *,
    route: str,
    default_enabled: bool,
    context_file_path: str = "",
) -> SDKSessionSpec:
    context_scope = context_file_session_components(context_file_path)
    if context_scope is not None:
        scope, components = context_scope
    else:
        scope = "ask"
        components = default_cli_ask_session_components(route)
    return resolve_sdk_session_spec(
        scope=scope,
        components=components,
        enabled=getattr(args, "sdk_session", None),
        explicit_session_id=str(getattr(args, "sdk_session_id", "") or ""),
        database_path=str(getattr(args, "sdk_session_db", "") or ""),
        default_enabled=default_enabled,
    )


def _ask_route_session_default(route: str) -> bool:
    return route == "chief_of_staff"


def _preflight_blocks_execution(preflight: OrchestratorPreflight) -> bool:
    return not bool(preflight.execution_allowed)


def _orchestrator_preflight_payload(
    preflight: OrchestratorPreflight | None,
) -> dict[str, object] | None:
    payload = compact_orchestrator_preflight_payload(preflight)
    return payload or None


def _print_ask_preflight_blocked(
    input_text: str,
    *,
    json_output: bool,
    orchestrator_preflight: OrchestratorPreflight,
) -> int:
    result = orchestrator_preflight.route_result
    compact_preflight = _orchestrator_preflight_payload(orchestrator_preflight)
    combined_reason = " ".join(
        str(part or "")
        for part in (
            orchestrator_preflight.block_kind,
            orchestrator_preflight.block_reason,
            result.rationale,
            result.stop_reason,
            result.clarification_request,
        )
    ).lower()
    requires_approved_context = (
        "approved context" in combined_reason
        or "approved company" in combined_reason
        or "approved opportunity" in combined_reason
        or "outreach drafting requires" in combined_reason
    )
    payload: dict[str, object] = {
        "mode": "blocked",
        "selected_agent": orchestrator_preflight.selected_agent,
        "agent_name": _agent_display_name("orchestrator"),
        "input": input_text,
        "status": "blocked",
        "send_enabled": False,
        "block_kind": orchestrator_preflight.block_kind,
        "block_reason": orchestrator_preflight.block_reason,
        "manual_request_plan": orchestrator_preflight.manual_request_plan.model_dump(mode="json"),
        "orchestrator_preflight": compact_preflight,
        "message": result.clarification_request
        or result.stop_reason
        or "Orchestrator preflight blocked specialist execution.",
        "output": (compact_preflight or {}).get("route_result", {}),
    }
    if requires_approved_context:
        payload["requires_approved_context"] = True
        payload["recommended_next_action"] = (
            "Attach or approve source-backed context in a WorkItem before live drafting."
        )
    return _print_ask_live_payload(payload, json_output=json_output)


def _should_run_opportunity_to_outreach_loop(
    manual_plan: ManualRequestPlan | None,
    *,
    explicit_route: str | None,
) -> bool:
    if manual_plan is None or manual_plan.intent != "opportunity_to_outreach_loop":
        return False
    return explicit_route in {None, "orchestrator"}


def _run_ask_opportunity_to_outreach_loop(
    input_text: str,
    *,
    live_search: bool,
    live_sdk: bool,
    json_output: bool,
    manual_plan: ManualRequestPlan,
    database_url: str | None,
    save: bool,
    request_approval: bool,
    approval_channel: str,
    live_slack: bool,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    sdk_session_spec: SDKSessionSpec | None = None,
    cost_tracking_requested: bool = False,
) -> int:
    approval_requested = request_approval or _approval_requested_from_text(input_text)
    if live_slack and not approval_requested:
        raise SystemExit("--live-slack requires --request-approval or an approval request in text.")
    topic = (manual_plan.primary_target or input_text).strip()
    top_n = max(1, min(10, manual_plan.desired_count or 1))
    dry_run = not live_search
    session_env = sdk_session_env(sdk_session_spec) if sdk_session_spec is not None else {}
    previous_env = {key: os.environ.get(key) for key in session_env}
    try:
        os.environ.update(session_env)
        result = run_opportunity_to_outreach_loop(
            topic=topic,
            top_n=top_n,
            dry_run=dry_run,
            live_search=live_search,
            save=save or approval_requested,
            database_url=database_url,
            approval_channel=approval_channel,
            outreach_channel="email",
            live_sdk_synthesis=bool(live_sdk and live_search),
        )
    finally:
        for key, previous in previous_env.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    if approval_requested:
        slack = SlackTool(live=live_slack, approvals_channel=approval_channel)
        posted = []
        for saved_item in result.storage.get("items", []):
            approval_item = saved_item.get("approval_queue_item")
            if not isinstance(approval_item, dict):
                continue
            message = slack_review_message_from_approval_item(approval_item)
            posted.append(slack.post_review_message(message, channel=approval_channel))
        result.storage["slack_approval_posts"] = posted
    payload = {
        "mode": "live_workflow" if live_search else "dry_run_workflow",
        "selected_agent": "orchestrator",
        "agent_name": "Keystone Orchestrator Agent",
        "workflow": "opportunity_to_outreach_loop",
        "input": input_text,
        "topic": topic,
        "top_n": top_n,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json"),
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "sdk_session": sdk_session_spec.log_metadata() if sdk_session_spec else None,
        "cost_tracking_requested": cost_tracking_requested,
        "output_type": type(result).__name__,
        "output": result.model_dump(mode="json"),
    }
    return _print_ask_live_payload(payload, json_output=json_output)


def _approval_requested_from_text(text: str) -> bool:
    lower = str(text or "").lower()
    return any(
        marker in lower
        for marker in (
            "post approval",
            "request approval",
            "approval to this channel",
            "approval in this channel",
            "post to this channel",
        )
    )


def _workflow_cost_options_for_request_context(
    *,
    request_text: str,
    context_file_path: str = "",
    work_item: WorkItem | None = None,
) -> dict[str, Any]:
    """Let the WorkItem runner choose route-aware Slack cost controls."""

    if not _is_slack_workflow_context(context_file_path=context_file_path, work_item=work_item):
        return {}
    return {}


def _is_slack_workflow_context(
    *,
    context_file_path: str = "",
    work_item: WorkItem | None = None,
) -> bool:
    if _context_file_is_slack_context(context_file_path):
        return True
    if work_item is None:
        return False
    slack_context = getattr(work_item.target, "metadata", {}).get("slack_context")
    return isinstance(slack_context, dict) and bool(slack_context)


def _context_file_is_slack_context(context_file_path: str) -> bool:
    if not context_file_path:
        return False
    try:
        data = json.loads(Path(context_file_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    schema = str(data.get("schema") or data.get("schema_") or "").strip()
    return schema.startswith("keystone.slack.")


def _run_ask_work_item(
    input_text: str,
    *,
    database_url: str | None,
    live_search: bool,
    live_sdk: bool,
    max_results: int,
    json_output: bool,
    max_manager_steps: int = 3,
    manual_plan: ManualRequestPlan | None = None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    context_file_path: str = "",
    sdk_session_enabled: bool | None = None,
    sdk_session_id: str = "",
    sdk_session_db_path: str = "",
    cost_tracking_requested: bool = False,
) -> int:
    store = SQLiteStore(database_url or database_url_from_env())
    work_item_id = _resolve_continue_work_item_id(
        store,
        input_text=input_text,
        explicit_work_item_id=None,
        json_output=json_output,
    )
    existing_work_item = store.get_work_item(work_item_id) if work_item_id else None
    result = advance_work_item_manager_loop(
        WorkflowRunRequest(
            request_text=input_text,
            work_item_id=work_item_id,
            save=True,
            database_url=database_url,
            live_search=live_search,
            live_sdk=live_sdk,
            max_results=max_results,
            manual_request_plan=manual_plan.model_dump(mode="json") if manual_plan else None,
            orchestrator_preflight=_orchestrator_preflight_payload(orchestrator_preflight),
            context_file_path=context_file_path,
            sdk_session_enabled=sdk_session_enabled,
            sdk_session_id=sdk_session_id,
            sdk_session_db_path=sdk_session_db_path,
            cost_tracking_requested=cost_tracking_requested,
            **_workflow_cost_options_for_request_context(
                request_text=input_text,
                context_file_path=context_file_path,
                work_item=existing_work_item,
            ),
        ),
        max_steps=max_manager_steps,
        feedback_callback=None if json_output else _print_manager_loop_feedback,
    )
    return _print_work_item_result(result, json_output=json_output)


def _print_manager_loop_feedback(event_type: str, payload: dict[str, Any]) -> None:
    """Print compact real-time manager feedback without changing result schemas."""

    if event_type == "manager_loop_review":
        decision = str(payload.get("review_decision") or "").strip()
        label = (
            "Manager review block"
            if decision == "block" or payload.get("blocking") is True
            else "Manager review warning"
            if decision == "warn" or payload.get("advisory") is True
            else "Manager review"
        )
        print(
            f"{label}: "
            f"step {payload.get('step')} {payload.get('route')} "
            f"{payload.get('review_status')} ({payload.get('overall_score')}/100)"
        )
        next_step = str(payload.get("recommended_next_step") or "").strip()
        if next_step:
            print(f"Manager feedback: {next_step}")
    elif event_type == "manager_loop_completed":
        print(f"Manager loop: {payload.get('stop_reason')}")


def _run_ask_orchestrator(
    input_text: str,
    *,
    live_sdk: bool,
    json_output: bool,
    manual_plan: ManualRequestPlan | None = None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    sdk_session_spec: SDKSessionSpec | None = None,
    cost_tracking_requested: bool = False,
) -> int:
    sdk_result = None
    if live_sdk:
        load_settings(force_dotenv=True)
        sdk_result = run_orchestrator_sdk(
            input_text,
            live=True,
            session=build_sdk_session(sdk_session_spec) if sdk_session_spec else None,
        )
        result = sdk_result.output
    else:
        result = route_request(input_text, manual_plan=manual_plan)
    payload = {
        "mode": "live_sdk" if live_sdk else "dry_run",
        "selected_agent": "orchestrator",
        "agent_name": _agent_display_name("orchestrator"),
        "input": input_text,
        "route": result.route,
        "target_agent": result.target_agent,
        "send_enabled": result.send_enabled,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "sdk_session": sdk_session_spec.log_metadata() if sdk_session_spec else None,
        "cost_tracking_requested": cost_tracking_requested,
        "output": result.model_dump(mode="json"),
    }
    if sdk_result is not None:
        payload["usage"] = sdk_result.usage
        payload["cost"] = sdk_result.cost
        payload["request_cache"] = sdk_result.request_cache
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(f"Agent: {payload['agent_name']}")
        print(f"Route: {payload['route']}")
        print(f"Target: {payload['target_agent'] or 'clarification'}")
        print(f"Send enabled: {payload['send_enabled']}")
        if manual_plan:
            print(f"Manual plan: {manual_plan.target_agent} / {manual_plan.intent}")
    return 0


def _print_ask_dry_run(
    route: str,
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None = None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    database_url: str | None = None,
) -> int:
    agent = AGENT_REGISTRY[route].build_agent()
    chief_of_staff_output = (
        plan_chief_of_staff_request(
            input_text,
            database_url=database_url,
            manual_request_plan=manual_plan,
        )
        if route == "chief_of_staff"
        else None
    )
    payload = {
        "mode": "dry_run",
        "selected_agent": route,
        "agent_name": _agent_display_name(route),
        "sdk_agent_name": agent.name,
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "output": (
            chief_of_staff_output.model_dump(mode="json") if chief_of_staff_output else None
        ),
        "note": (
            "Dry-run selected the specialist but did not call a model. "
            "Re-run with --live-sdk to execute."
        ),
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(f"Agent: {payload['agent_name']}")
        print(f"Route: {route}")
        if manual_plan:
            print(f"Manual plan: {manual_plan.target_agent} / {manual_plan.intent}")
            if manual_plan.primary_target:
                print(f"Target: {manual_plan.primary_target}")
        if chief_of_staff_output is not None:
            route_output = chief_of_staff_output.recommended_route
            print(f"Workflow: {route_output.workflow_type}")
            print(f"Command: {route_output.command_text}")
            print(
                f"Target channel: #{route_output.target_channel}"
                if route_output.target_channel
                else "Target channel: clarify"
            )
            print(f"Slack post allowed: {chief_of_staff_output.slack_post_allowed}")
        print("Mode: dry_run")
        print("Send enabled: False")
        print(payload["note"])
    return 0


def _run_ask_specialist_live(
    route: str,
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None = None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    sdk_session_spec: SDKSessionSpec | None = None,
    cost_tracking_requested: bool = False,
) -> int:
    load_settings(force_dotenv=True)
    if route == "business_research_analyst":
        return _run_ask_company_research_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            cost_tracking_requested=cost_tracking_requested,
        )
    elif route == "chief_of_staff":
        return _run_ask_chief_of_staff_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            cost_tracking_requested=cost_tracking_requested,
        )
    elif route == "opportunity_scout":
        return _run_ask_opportunity_scout_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            cost_tracking_requested=cost_tracking_requested,
        )
    elif route == "outreach_composer":
        return _run_ask_outreach_composer_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            cost_tracking_requested=cost_tracking_requested,
        )
    elif route == "gmail_triage":
        return _run_ask_gmail_triage_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            cost_tracking_requested=cost_tracking_requested,
        )
    else:
        raise SystemExit(f"Unsupported agent route: {route}")


def _run_ask_company_research_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    cost_tracking_requested: bool,
) -> int:
    target = (manual_plan.primary_target if manual_plan else "") or input_text[:120]
    if not target.strip():
        return _print_ask_clarification(
            "business_research_analyst",
            input_text,
            "Business Research Analyst needs a company, person, institute, URL, or topic target.",
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
        )
    company_url = _manual_plan_url_target(manual_plan)
    company_name = _company_name_for_url_target(target.strip()) if company_url else target.strip()
    command = [
        sys.executable,
        "scripts/run_company_research.py",
        "--company",
        company_name,
        "--request-text",
        input_text,
        "--max-results",
        "5",
        "--live-search",
        "--no-dry-run",
        "--live-sdk",
        "--focused-brief",
        "--json",
    ]
    if company_url:
        command.extend(["--company-url", company_url])
    return _run_ask_script_live(
        "business_research_analyst",
        input_text,
        command,
        json_output=json_output,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        sdk_session_spec=sdk_session_spec,
        cost_tracking_requested=cost_tracking_requested,
    )


def _manual_plan_url_target(manual_plan: ManualRequestPlan | None) -> str:
    if manual_plan is None or manual_plan.target_type != "url":
        return ""
    target = manual_plan.primary_target.strip()
    return target if target.startswith(("http://", "https://")) else ""


def _company_name_for_url_target(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or parsed.path).strip().lower()
    return host.removeprefix("www.") or url


def _run_ask_chief_of_staff_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    cost_tracking_requested: bool,
) -> int:
    command = [
        sys.executable,
        "scripts/run_chief_of_staff.py",
        "--input",
        input_text,
        "--live-sdk",
        "--json",
    ]
    return _run_ask_script_live(
        "chief_of_staff",
        input_text,
        command,
        json_output=json_output,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        sdk_session_spec=sdk_session_spec,
        cost_tracking_requested=cost_tracking_requested,
    )


def _run_ask_opportunity_scout_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    cost_tracking_requested: bool,
) -> int:
    max_results = manual_plan.desired_count if manual_plan else 3
    target = (manual_plan.primary_target if manual_plan else "") or input_text
    command = [
        sys.executable,
        "scripts/run_opportunity_scout.py",
        "--topic",
        target,
        "--max-results",
        str(max(1, min(10, max_results))),
        "--live-search",
        "--live-search-plan",
        "--no-dry-run",
        "--live-sdk",
        "--json",
    ]
    return _run_ask_script_live(
        "opportunity_scout",
        input_text,
        command,
        json_output=json_output,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        sdk_session_spec=sdk_session_spec,
        cost_tracking_requested=cost_tracking_requested,
    )


def _run_ask_gmail_triage_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    cost_tracking_requested: bool,
) -> int:
    gmail_plan = infer_gmail_execution_plan(input_text)
    explicit_fixture_path = _gmail_direct_fixture_path(input_text)
    if gmail_plan.operation == "priority_grouping" and gmail_plan.live_read_required:
        command = [
            sys.executable,
            "scripts/run_gmail_triage.py",
            "--live-gmail",
            "--allow-inbox",
            "--priority-grouping",
            "--live-sdk",
            "--json",
            "--request",
            input_text,
            "--lookback-days",
            str(gmail_plan.lookback_days),
            "--max-messages",
            str(gmail_plan.max_messages),
        ]
        if gmail_plan.gmail_query:
            command.extend(["--gmail-query", gmail_plan.gmail_query])
        return _run_ask_script_live(
            "gmail_triage",
            input_text,
            command,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
        )
    if gmail_plan.operation == "draft_reply" and explicit_fixture_path is None:
        return _print_ask_clarification(
            "gmail_triage",
            input_text,
            (
                "Gmail Triage needs a selected Gmail thread, message, or explicit email "
                "fixture before drafting a reply. No synthetic email was created from the "
                "operator request."
            ),
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            extra={
                "status": "blocked",
                "block_kind": "missing_gmail_context",
                "requires_gmail_context": True,
                "recommended_next_action": (
                    "Select the Gmail thread/message or provide an explicit fixture path, "
                    "then rerun Gmail Triage."
                ),
                "agent_execution_plan": gmail_plan.model_dump(mode="json"),
            },
        )

    temp_path: Path | None = None
    if explicit_fixture_path is not None:
        selected_fixture = str(explicit_fixture_path)
    else:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".txt",
            prefix="keystone-gmail-manual-",
            delete=False,
        ) as tmp:
            tmp.write("Subject: Manual Gmail triage request\n\n")
            tmp.write(input_text.strip() or "Manual Gmail triage request.")
            temp_path = Path(tmp.name)
        selected_fixture = str(temp_path)
    command = [
        sys.executable,
        "scripts/run_gmail_triage.py",
        "--fixture",
        selected_fixture,
        "--request",
        input_text,
        "--live-sdk",
        "--json",
    ]
    if manual_plan and manual_plan.lookback_days is not None:
        command.extend(["--lookback-days", str(manual_plan.lookback_days)])
    try:
        return _run_ask_script_live(
            "gmail_triage",
            input_text,
            command,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _gmail_direct_fixture_path(input_text: str) -> Path | None:
    if not input_text or "\n" in input_text:
        return None
    try:
        path = Path(input_text).expanduser()
    except (OSError, RuntimeError):
        return None
    return path if path.is_file() else None


def _run_ask_outreach_composer_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    cost_tracking_requested: bool,
) -> int:
    outreach_plan = infer_outreach_execution_plan(input_text)
    if not outreach_plan.use_default_approved_fixture_for_backend_test:
        return _print_ask_outreach_context_blocked(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            extra={
                "agent_execution_plan": outreach_plan.model_dump(mode="json"),
            },
        )
    command = [
        sys.executable,
        "scripts/run_outreach_draft.py",
        "--live-sdk",
        "--json",
        "--goal",
        input_text,
        "--drafting-approval-decision",
        "approved_for_drafting",
        "--approval-decision",
        "pending",
        "--include-follow-up-schedule",
        "--use-example-rag",
    ]
    return _run_ask_script_live(
        "outreach_composer",
        input_text,
        command,
        json_output=json_output,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        sdk_session_spec=sdk_session_spec,
        agent_execution_plan=outreach_plan.model_dump(mode="json"),
        cost_tracking_requested=cost_tracking_requested,
    )


def _print_ask_outreach_context_blocked(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    extra: dict[str, object] | None = None,
) -> int:
    return _print_ask_clarification(
        "outreach_composer",
        input_text,
        (
            "Outreach Composer requires approved company, opportunity, contact, or "
            "research-brief context before live drafting. No default fixture was used."
        ),
        json_output=json_output,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        extra={
            "status": "blocked",
            "requires_approved_context": True,
            "send_enabled": False,
            "recommended_next_action": (
                "Attach or approve source-backed context in a WorkItem, or use "
                "scripts/run_outreach_draft.py with explicit approved fixtures."
            ),
            **(extra or {}),
        },
    )


def _run_ask_script_live(
    route: str,
    input_text: str,
    command: list[str],
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    sdk_session_spec: SDKSessionSpec | None = None,
    agent_execution_plan: dict[str, object] | None = None,
    cost_tracking_requested: bool = False,
) -> int:
    env = None
    child_env = orchestrator_preflight_env(orchestrator_preflight)
    if sdk_session_spec is not None:
        child_env = {**child_env, **sdk_session_env(sdk_session_spec)}
    if child_env:
        env = {**os.environ, **child_env}
    timeout_seconds = _child_agent_timeout_seconds()
    try:
        completed = run_isolated_child_process(
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        payload = {
            "mode": "live_sdk",
            "selected_agent": route,
            "agent_name": _agent_display_name(route),
            "input": input_text,
            "status": "timeout",
            "timeout_seconds": timeout_seconds,
            "send_enabled": False,
            "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
            "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
            "agent_execution_plan": agent_execution_plan,
            "sdk_session": sdk_session_spec.log_metadata() if sdk_session_spec else None,
            "cost_tracking_requested": cost_tracking_requested,
            "output": {
                "summary": (
                    f"{_agent_display_name(route)} timed out after "
                    f"{timeout_seconds:.0f} seconds."
                ),
                "send_enabled": False,
                "error_type": "timeout",
            },
        }
        _print_ask_live_payload(payload, json_output=json_output)
        return 1
    if completed.returncode != 0:
        payload = {
            "mode": "live_sdk",
            "selected_agent": route,
            "agent_name": _agent_display_name(route),
            "input": input_text,
            "status": "failed",
            "child_returncode": int(completed.returncode),
            "send_enabled": False,
            "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
            "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
            "agent_execution_plan": agent_execution_plan,
            "sdk_session": sdk_session_spec.log_metadata() if sdk_session_spec else None,
            "cost_tracking_requested": cost_tracking_requested,
            "output": {
                "summary": f"{_agent_display_name(route)} child process failed.",
                "send_enabled": False,
                "error_type": "child_process_failed",
                "returncode": int(completed.returncode),
                "stderr_excerpt": _redacted_child_output(completed.stderr),
                "stdout_excerpt": _redacted_child_output(completed.stdout),
            },
        }
        _print_ask_live_payload(payload, json_output=json_output)
        return int(completed.returncode) or 1
    try:
        script_payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        payload = {
            "mode": "live_sdk",
            "selected_agent": route,
            "agent_name": _agent_display_name(route),
            "input": input_text,
            "status": "failed",
            "child_returncode": int(completed.returncode),
            "send_enabled": False,
            "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
            "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
            "agent_execution_plan": agent_execution_plan,
            "sdk_session": sdk_session_spec.log_metadata() if sdk_session_spec else None,
            "cost_tracking_requested": cost_tracking_requested,
            "output": {
                "summary": f"{_agent_display_name(route)} returned malformed JSON.",
                "send_enabled": False,
                "error_type": "child_process_malformed_json",
                "parse_error": str(exc),
                "stderr_excerpt": _redacted_child_output(completed.stderr),
                "stdout_excerpt": _redacted_child_output(completed.stdout),
            },
        }
        _print_ask_live_payload(payload, json_output=json_output)
        return 1
    output = script_payload.get("output") if isinstance(script_payload, dict) else None
    review = review_specialist_output(
        agent_name=route,
        output=output if output is not None else script_payload,
        request_summary=input_text,
        run_type="live_sdk" if isinstance(script_payload, dict) else "live_sdk_unknown",
    )
    payload = {
        "mode": "live_sdk",
        "selected_agent": route,
        "agent_name": _agent_display_name(route),
        "input": input_text,
        "send_enabled": _payload_send_enabled(script_payload),
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "agent_execution_plan": agent_execution_plan,
        "sdk_session": sdk_session_spec.log_metadata() if sdk_session_spec else None,
        "cost_tracking_requested": cost_tracking_requested,
        "output_type": (
            str(script_payload.get("output_type") or type(output).__name__)
            if isinstance(script_payload, dict)
            else type(script_payload).__name__
        ),
        "missing_information": _payload_missing_information(script_payload),
        "orchestrator_review": review.model_dump(mode="json"),
        "output": output if output is not None else script_payload,
        "script_payload": script_payload,
    }
    retrieval_diagnostics = _payload_retrieval_diagnostics(script_payload)
    if retrieval_diagnostics:
        payload["retrieval_diagnostics"] = retrieval_diagnostics
    return _print_ask_live_payload(payload, json_output=json_output)


def _child_agent_timeout_seconds() -> float:
    raw = os.getenv("KEYSTONE_CHILD_AGENT_TIMEOUT_SECONDS") or os.getenv(
        "KEYSTONE_LIVE_MODEL_TIMEOUT_SECONDS"
    )
    if not raw:
        return 180.0
    try:
        value = float(raw)
    except ValueError:
        return 180.0
    return min(max(value, 1.0), 1800.0)


def _payload_send_enabled(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if bool(payload.get("send_enabled")):
        return True
    output = payload.get("output")
    return bool(output.get("send_enabled")) if isinstance(output, dict) else False


def _payload_missing_information(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    output = payload.get("output")
    values: list[str] = []
    for source in (payload, output if isinstance(output, dict) else {}):
        for key in (
            "missing_information",
            "missing_evidence",
            "missing_inputs",
            "missing_requirements",
            "unknowns",
            "limitations",
            "triage_limitations",
        ):
            raw = source.get(key)
            if isinstance(raw, list):
                values.extend(str(item).strip() for item in raw if str(item).strip())
    return list(dict.fromkeys(values))


def _payload_retrieval_diagnostics(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    direct = payload.get("retrieval_diagnostics")
    if isinstance(direct, dict):
        return direct
    retrieval = payload.get("retrieval")
    if isinstance(retrieval, dict) and isinstance(retrieval.get("retrieval_diagnostics"), dict):
        return retrieval["retrieval_diagnostics"]
    live_search_metadata = payload.get("live_search_metadata")
    if isinstance(live_search_metadata, dict) and isinstance(
        live_search_metadata.get("retrieval_diagnostics"),
        dict,
    ):
        return live_search_metadata["retrieval_diagnostics"]
    return {}


def _redacted_child_output(value: str | None, *, max_chars: int = 1200) -> str:
    redacted = redact_secrets(str(value or ""))
    text = str(redacted if redacted is not None else "")
    text = " ".join(text.replace("\x00", "").split())
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "..."
    return text


def _print_ask_clarification(
    route: str,
    input_text: str,
    message: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    extra: dict[str, object] | None = None,
) -> int:
    payload = {
        "mode": "blocked",
        "selected_agent": route,
        "agent_name": _agent_display_name(route),
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "message": message,
    }
    if extra:
        payload.update(extra)
    return _print_ask_live_payload(payload, json_output=json_output)


def _print_ask_live_payload(payload: dict[str, object], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(f"Agent: {payload.get('agent_name', '')}")
        if payload.get("output_type"):
            print(f"Output type: {payload['output_type']}")
        print(f"Send enabled: {payload.get('send_enabled', False)}")
        if payload.get("message"):
            print(payload["message"])
        else:
            print(json.dumps(payload.get("output"), ensure_ascii=True, indent=2, sort_keys=True))
        review = payload.get("orchestrator_review")
        if isinstance(review, dict):
            print(
                "Orchestrator review: "
                f"{review.get('status')} ({review.get('overall_score')}/100)"
            )
            next_step = str(review.get("recommended_next_step") or "").strip()
            if next_step:
                print(f"Orchestrator feedback: {next_step}")
        missing = payload.get("missing_information")
        if isinstance(missing, list) and missing:
            print("Missing information: " + "; ".join(str(item) for item in missing[:8]))
    return 0


def _run_pipeline(args: argparse.Namespace) -> int:
    if not args.dry_run:
        raise SystemExit("Keystone pipeline CLI is dry-run only. Re-run with --dry-run.")
    result = run_keystone_pipeline(
        email_fixture=Path(args.email_fixture),
        company_fixture=Path(args.company_fixture) if args.company_fixture else None,
        approval_state=args.approval_state,
        dry_run=args.dry_run,
        sdk=args.sdk,
        save=args.save,
        database_url=args.database_url,
        include_operator_feedback_request=args.ask_feedback,
    )
    if args.markdown:
        print(pipeline_markdown_report(result))
    else:
        print(json.dumps(result.model_dump(), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def _run_evals(args: argparse.Namespace) -> int:
    summary = run_static_evals(agent=args.agent)
    if args.json and not args.markdown:
        print(json.dumps(summary.to_dict(), ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(generate_eval_report(summary))
    return 0 if summary.failed == 0 else 1


def _run_approvals(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    status = None if args.status == "all" else args.status
    items = store.list_approval_items(
        status=status,
        object_type=args.object_type,
        source_agent=args.source_agent,
    )
    if args.json:
        print(
            json.dumps(
                [_approval_item_export_dict(item) for item in items],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(_render_approvals_table(items, status_label=args.status))
    return 0


def _run_work_items_list(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    status = None if args.status == "all" else args.status
    items = store.list_work_items(status=status, kind=args.kind, limit=args.limit)
    if args.json:
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in items],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    elif not items:
        print("No WorkItems found.")
    else:
        print(
            render_markdown_table(
                ["ID", "Status", "Kind", "Route", "Title", "Next action"],
                [
                    [
                        item.id,
                        item.status.value,
                        item.kind.value,
                        item.current_route.value,
                        item.title,
                        item.next_action.action if item.next_action else "",
                    ]
                    for item in items
                ],
            )
        )
    return 0


def _run_work_items_show(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    item = store.get_work_item(args.work_item_id)
    if item is None:
        raise SystemExit(f"WorkItem not found: {args.work_item_id}")
    payload = item.model_dump(mode="json")
    payload["events"] = [
        event.model_dump(mode="json") for event in store.list_work_item_events(item.id)
    ]
    payload["persisted_artifacts"] = [
        artifact.model_dump(mode="json") for artifact in store.list_work_item_artifacts(item.id)
    ]
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(f"WorkItem: {item.id}")
        print(f"Status: {item.status.value}")
        print(f"Kind: {item.kind.value}")
        print(f"Route: {item.current_route.value}")
        print(f"Title: {item.title}")
        print(f"Next action: {item.next_action.action if item.next_action else 'none'}")
        if item.artifact_refs:
            print(f"Artifacts: {len(item.artifact_refs)}")
            selected = [artifact for artifact in item.artifact_refs if artifact.selected]
            if selected:
                print(
                    "Selected: "
                    + ", ".join(
                        f"{artifact.artifact_type}:{artifact.artifact_id}" for artifact in selected
                    )
                )
    return 0


def _run_work_items_artifacts(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    item = _load_work_item_or_exit(store, args.work_item_id)
    artifacts = item.artifact_refs
    if args.json:
        print(
            json.dumps(
                [artifact.model_dump(mode="json") for artifact in artifacts],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    elif not artifacts:
        print("No WorkItem artifacts found.")
    else:
        print(
            render_markdown_table(
                ["Artifact", "Selected", "Approval", "Source agent", "Title"],
                [
                    [
                        f"{artifact.artifact_type}:{artifact.artifact_id}",
                        "yes" if artifact.selected else "",
                        artifact.approval_state,
                        artifact.source_agent,
                        artifact.title,
                    ]
                    for artifact in artifacts
                ],
            )
        )
    return 0


def _run_work_items_timeline(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    _load_work_item_or_exit(store, args.work_item_id)
    events = store.list_work_item_events(args.work_item_id)
    if args.json:
        print(
            json.dumps(
                [event.model_dump(mode="json") for event in events],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    elif not events:
        print("No WorkItem events found.")
    else:
        print(
            render_markdown_table(
                ["Created", "Event", "Actor", "Summary"],
                [
                    [event.created_at, event.event_type, event.actor, event.summary]
                    for event in events
                ],
            )
        )
    return 0


def _automation_store(args: argparse.Namespace) -> SQLiteStore:
    store = SQLiteStore(args.database_url or database_url_from_env())
    ensure_default_automation_inventory(store)
    return store


def _run_automations_list(args: argparse.Namespace) -> int:
    store = _automation_store(args)
    specs = store.list_automation_specs(status=args.status, limit=100)
    if args.json:
        print(
            json.dumps(
                [spec.model_dump(mode="json") for spec in specs],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    elif not specs:
        print("No automations found.")
    else:
        print(
            render_markdown_table(
                ["ID", "Status", "Trigger", "Workflow", "Default channel"],
                [
                    [
                        spec.id,
                        spec.status.value,
                        spec.trigger_type.value,
                        spec.workflow,
                        spec.default_channel,
                    ]
                    for spec in specs
                ],
            )
        )
    return 0


def _run_automations_runs(args: argparse.Namespace) -> int:
    store = _automation_store(args)
    runs = store.list_automation_runs(
        automation_id=args.automation_id,
        status=args.status,
        limit=args.limit,
    )
    if args.json:
        print(
            json.dumps(
                [run.model_dump(mode="json") for run in runs],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    elif not runs:
        print("No automation runs found.")
    else:
        print(
            render_markdown_table(
                ["ID", "Automation", "Stage", "Status", "WorkItem", "Approvals"],
                [
                    [
                        run.id,
                        run.automation_name or run.automation_id,
                        run.stage,
                        run.status.value,
                        run.work_item_id,
                        str(run.approval_count),
                    ]
                    for run in runs
                ],
            )
        )
    return 0


def _run_automations_audit(args: argparse.Namespace) -> int:
    report = build_automation_inventory_report(
        database_url=args.database_url,
        channels=args.channel,
        limit=args.limit,
    )
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(render_automation_inventory_markdown(report))
    return 0


def _run_work_items_advance(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    input_text = _read_input(args.input)
    work_item_id = _resolve_continue_work_item_id(
        store,
        input_text=input_text,
        explicit_work_item_id=args.work_item_id,
        json_output=args.json,
    )
    existing_work_item = store.get_work_item(work_item_id) if work_item_id else None
    preflight_requested_agent = _work_item_preflight_requested_agent(existing_work_item)
    substantive_request = str(input_text or "").strip().lower() not in {"", "continue", "resume"}
    orchestrator_preflight = None
    manual_plan = None
    if substantive_request:
        orchestrator_preflight = run_orchestrator_preflight(
            input_text,
            requested_agent=preflight_requested_agent,
            live_manual_plan=bool(args.live_sdk),
            database_url=args.database_url,
        )
        manual_plan = orchestrator_preflight.manual_request_plan
        if _preflight_blocks_execution(orchestrator_preflight):
            return _print_ask_preflight_blocked(
                input_text,
                json_output=args.json,
                orchestrator_preflight=orchestrator_preflight,
            )
    request = WorkflowRunRequest(
        request_text=input_text,
        work_item_id=work_item_id,
        save=True,
        database_url=args.database_url,
        live_search=args.live_search,
        live_sdk=args.live_sdk,
        max_results=args.max_results,
        context_file_path=args.context_file,
        manual_request_plan=manual_plan.model_dump(mode="json") if manual_plan else None,
        orchestrator_preflight=_orchestrator_preflight_payload(orchestrator_preflight),
        sdk_session_enabled=args.sdk_session,
        sdk_session_id=args.sdk_session_id,
        sdk_session_db_path=args.sdk_session_db,
        **_workflow_cost_options_for_request_context(
            request_text=input_text,
            context_file_path=args.context_file,
            work_item=existing_work_item,
        ),
    )
    graph_metadata = None
    if args.langgraph:
        outcome = _run_work_item_langgraph_for_request(request)
        result = outcome.result
        graph_metadata = _work_item_langgraph_metadata(outcome)
    else:
        from keystone_agents.langgraph_workflow import work_item_langgraph_enabled

        if work_item_langgraph_enabled():
            outcome = _run_work_item_langgraph_for_request(request)
            result = outcome.result
            graph_metadata = _work_item_langgraph_metadata(outcome)
        else:
            result = advance_work_item_manager_loop(
                request,
                max_steps=args.max_manager_steps,
                feedback_callback=None if args.json else _print_manager_loop_feedback,
            )
    return _print_work_item_result(result, json_output=args.json, graph_metadata=graph_metadata)


def _work_item_preflight_requested_agent(work_item: WorkItem | None) -> str | None:
    if work_item is None:
        return None
    route = work_item.current_route
    if route in {WorkItemRoute.ORCHESTRATOR, WorkItemRoute.CLARIFICATION}:
        return None
    return route.value


def _run_work_item_langgraph_for_request(request: WorkflowRunRequest):
    from keystone_agents.langgraph_workflow import (
        run_work_item_langgraph,
        work_item_graph_thread_id,
    )

    thread_id = work_item_graph_thread_id(request.work_item_id or "")
    return run_work_item_langgraph(request, thread_id=thread_id or None)


def _work_item_langgraph_metadata(outcome) -> dict:
    return {
        "runtime": outcome.graph_runtime,
        "graph_available": outcome.graph_available,
        "checkpoint_required": outcome.checkpoint_required,
        "checkpoint_reason": outcome.checkpoint_reason,
        "checkpoint_key": outcome.checkpoint_key,
        "node_path": outcome.node_path,
        "improvements": outcome.improvements,
    }


def _run_work_items_attach(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    item = _load_work_item_or_exit(store, args.work_item_id)
    artifact_type, artifact_id = parse_artifact_spec(args.artifact)
    artifact = _artifact_ref_from_store(store, artifact_type, artifact_id)
    item = attach_artifact(item, artifact)
    if args.select:
        item = select_artifact(item, artifact_type, artifact_id)
    store.save_work_item(item)
    store.save_work_item_artifact(item.id, artifact.model_copy(update={"selected": args.select}))
    record_event(
        item,
        event_type="artifact_attached",
        summary=f"Attached {artifact_type}:{artifact_id}.",
        metadata={"artifact": artifact.model_dump(mode="json"), "selected": args.select},
        store=store,
    )
    return _print_work_item_mutation(item, args.json, f"Attached {artifact_type}:{artifact_id}.")


def _run_work_items_select(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    item = _load_work_item_or_exit(store, args.work_item_id)
    artifact_type, artifact_id = parse_artifact_spec(args.artifact)
    item = select_artifact(item, artifact_type, artifact_id)
    record_event(
        item,
        event_type="artifact_selected",
        summary=f"Selected {artifact_type}:{artifact_id}.",
        metadata={"artifact_type": artifact_type, "artifact_id": artifact_id},
        store=store,
    )
    store.save_work_item(item)
    return _print_work_item_mutation(item, args.json, f"Selected {artifact_type}:{artifact_id}.")


def _run_work_items_approve_context(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    item = _load_work_item_or_exit(store, args.work_item_id)
    artifact_type, artifact_id = parse_artifact_spec(args.artifact)
    approval_scope = (
        ApprovalScope.EXTERNAL_USE
        if args.state == ApprovalState.APPROVED_FOR_EXTERNAL_USE.value
        else ApprovalScope.DRAFTING
    )
    approval_id = store.save_approval(
        object_type=artifact_type,
        object_id=artifact_id,
        decision=args.state,
        scope=approval_scope,
        reviewer=args.reviewer,
        notes=args.notes,
        source_agent="work_item",
    )
    item = approve_artifact_context(
        item,
        artifact_type,
        artifact_id,
        approval_state=args.state,
        approval_id=str(approval_id),
    )
    if drafting_ready(item).ready:
        item = set_next_action(
            item,
            WorkItemNextAction(
                action="draft_outreach",
                agent=WorkItemRoute.OUTREACH_COMPOSER,
                description="Selected context is approved for draft-only outreach.",
                command_hint=f"keystone work-items advance {item.id}",
            ),
        )
    item = item.model_copy(update={"status": derive_case_status(item)})
    record_event(
        item,
        event_type="context_approved",
        summary=f"Approved {artifact_type}:{artifact_id} as {args.state}.",
        metadata={
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "approval_id": approval_id,
            "approval_state": args.state,
        },
        store=store,
    )
    store.save_work_item(item)
    return _print_work_item_mutation(
        item,
        args.json,
        f"Approved {artifact_type}:{artifact_id} for WorkItem context.",
    )


def _load_work_item_or_exit(store: SQLiteStore, work_item_id: str):
    item = store.get_work_item(work_item_id)
    if item is None:
        raise SystemExit(f"WorkItem not found: {work_item_id}")
    return item


def _artifact_ref_from_store(
    store: SQLiteStore,
    artifact_type: str,
    artifact_id: str,
) -> WorkItemArtifactRef:
    if artifact_type == "company_profile":
        profile = store.load_company_profile(int(artifact_id))
        return WorkItemArtifactRef(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            source_agent="business_research_analyst",
            approval_state=ApprovalState.APPROVED_FOR_RESEARCH.value,
            title=profile.name,
            summary=(profile.fit_summary or profile.description)[:240],
        )
    if artifact_type == "opportunity":
        opportunity = store.load_opportunity_record(int(artifact_id))
        return WorkItemArtifactRef(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            source_agent="opportunity_scout",
            approval_state=ApprovalState.PENDING.value,
            title=opportunity.company_name,
            summary=opportunity.why_now_signal[:240],
        )
    return WorkItemArtifactRef(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        source_agent="external",
        approval_state=ApprovalState.PENDING.value,
        title=f"{artifact_type}:{artifact_id}",
    )


def _print_work_item_mutation(item, json_output: bool, message: str) -> int:
    if json_output:
        print(json.dumps(item.model_dump(mode="json"), ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(f"WorkItem: {item.id}")
        print(message)
        print(f"Status: {item.status.value}")
        print(f"Next action: {item.next_action.action if item.next_action else 'none'}")
    return 0


ACTIVE_WORK_ITEM_STATUSES = {
    WorkItemStatus.NEW,
    WorkItemStatus.IN_PROGRESS,
    WorkItemStatus.NEEDS_CONTEXT,
    WorkItemStatus.NEEDS_APPROVAL,
    WorkItemStatus.BLOCKED,
}


def _resolve_continue_work_item_id(
    store: SQLiteStore,
    *,
    input_text: str,
    explicit_work_item_id: str | None,
    json_output: bool,
) -> str | None:
    if explicit_work_item_id:
        return explicit_work_item_id
    if str(input_text or "").strip().lower() not in {"", "continue", "resume"}:
        return None
    candidates = _active_work_items(store)
    if len(candidates) == 1:
        return candidates[0].id
    if not candidates:
        raise SystemExit("No active WorkItem found. Start one with `research <target>`.")
    if json_output:
        print(
            json.dumps(
                {
                    "error": "ambiguous_active_work_item",
                    "candidates": [item.model_dump(mode="json") for item in candidates[:10]],
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("Multiple active WorkItems found. Specify one:")
        print(_render_work_item_candidates(candidates))
    raise SystemExit(2)


def _active_work_items(store: SQLiteStore, *, limit: int = 20) -> list[WorkItem]:
    return [
        item
        for item in store.list_work_items(status=None, limit=limit)
        if item.status in ACTIVE_WORK_ITEM_STATUSES
    ]


def _render_work_item_candidates(items: list[WorkItem]) -> str:
    return render_markdown_table(
        ["ID", "Status", "Route", "Title", "Next action"],
        [
            [
                item.id,
                item.status.value,
                item.current_route.value,
                item.title,
                item.next_action.action if item.next_action else "",
            ]
            for item in items[:10]
        ],
    )


def _print_work_item_result(
    result,
    *,
    json_output: bool,
    graph_metadata: dict | None = None,
) -> int:
    if json_output:
        payload = result.model_dump(mode="json")
        if graph_metadata is not None:
            payload["_langgraph"] = graph_metadata
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(render_work_item_result_text(result))
        if graph_metadata is not None:
            print(f"Graph runtime: {graph_metadata['runtime']}")
            print(f"Graph nodes: {' -> '.join(graph_metadata['node_path'])}")
            if graph_metadata["checkpoint_required"]:
                print(f"Graph checkpoint: {graph_metadata['checkpoint_reason']}")
    return 0


def _work_item_missing_information(result: WorkflowRunResult) -> list[str]:
    values = [blocker.message for blocker in result.blockers if blocker.message]
    pack = result.context_pack or {}
    if isinstance(pack, dict):
        raw = pack.get("missing_requirements")
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _work_item_limitation_notes(result: WorkflowRunResult) -> list[str]:
    pack = result.context_pack or {}
    if not isinstance(pack, dict):
        return []
    raw = pack.get("limitation_notes")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def _run_agents_list(args: argparse.Namespace) -> int:
    cards = agent_cards()
    if args.json:
        print(json.dumps(cards, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(
            render_markdown_table(
                ["Route", "Agent", "Schema", "Tools", "Live flags", "Safety"],
                [
                    [
                        card["route_name"],
                        card["agent_name"],
                        str(card["output_schema"]).rsplit(".", maxsplit=1)[-1],
                        str(len(card["tools"])),
                        ", ".join(card["live_flags_required"]) or "-",
                        "; ".join(card["safety_notes"][:2]),
                    ]
                    for card in cards
                ],
            )
        )
    return 0


def _approval_item_export_dict(item: ApprovalQueueItem) -> dict[str, object]:
    payload = item.model_dump(mode="json", exclude={"draft_text"})
    payload["draft_text_summary"] = sensitive_text_summary(item.draft_text)
    return payload


def _render_approvals_table(items: list[ApprovalQueueItem], *, status_label: str) -> str:
    if not items:
        return f"No {status_label} approvals found."
    rows = [
        [
            item.id,
            item.approval_status.value,
            item.object_type.value,
            item.object_id or "",
            item.source_agent,
            item.title,
            sensitive_text_summary(item.draft_text),
        ]
        for item in items
    ]
    return render_markdown_table(
        ["ID", "Status", "Object", "Object ID", "Agent", "Title", "Draft Text"],
        rows,
    )


if __name__ == "__main__":
    raise SystemExit(main())
