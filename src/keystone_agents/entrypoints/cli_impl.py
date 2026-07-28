"""Full package-native Keystone command implementation."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from keystone_agents.agent_mentions import AgentMention, parse_agent_mention
from keystone_agents.agent_registry import AGENT_REGISTRY, agent_cards
from keystone_agents.agents.calendar_action_interpreter import resolve_calendar_action_plan
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
from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.automation_inventory import (
    build_automation_inventory_report,
    ensure_default_automation_inventory,
    render_automation_inventory_markdown,
)
from keystone_agents.calendar_actions import (
    CalendarActionPlan,
    infer_calendar_action_plan,
    is_calendar_action_candidate,
)
from keystone_agents.capabilities.profile import (
    compile_child_result_promotion_receipt,
)
from keystone_agents.child_process import run_isolated_child_process
from keystone_agents.cli_sdk import add_sdk_session_arguments
from keystone_agents.config import (
    cli_default_live_research,
    cli_default_live_sdk,
    load_settings,
    with_cli_environment,
)
from keystone_agents.cost_tracking import parse_cost_tracking_directive
from keystone_agents.costing import estimate_usage_cost
from keystone_agents.direct_response import build_direct_supplied_response_agent
from keystone_agents.eval_runtime_diagnostics import (
    slack_eval_blocker_diagnostics,
    slack_eval_child_step_summary,
)
from keystone_agents.evals import generate_eval_report, run_static_evals
from keystone_agents.execution_admission import (
    ExecutionAdmission,
    admit_provider_action,
)
from keystone_agents.execution_request import (
    build_execution_request,
    continuation_owner_advice,
    execution_request_planning_text,
    latest_slack_operator_request,
    slack_work_item_control_requested,
)
from keystone_agents.execution_telemetry import (
    ExecutionTelemetryRecorder,
    compact_execution_telemetry,
)
from keystone_agents.file_search import local_file_search_config_summary
from keystone_agents.finance_expense_receipts import (
    finance_expense_receipt_field_hints,
    finance_expense_receipt_provider_context,
    infer_finance_expense_receipt_target,
    resolve_finance_expense_receipt_target,
)
from keystone_agents.gmail_triage.draft_actions import (
    GMAIL_TEST_DRAFT_MARKER,
    execute_gmail_test_draft_lifecycle,
)
from keystone_agents.gmail_triage.execution_plan import (
    gmail_message_count_scope,
    gmail_provider_read_scope,
    resolve_gmail_execution_plan,
)
from keystone_agents.health import format_health_report, report_to_json, run_health_check
from keystone_agents.instruction_following import (
    instruction_following_blocker_text,
    interpreted_output_constraints_text,
    output_constraints_from_plan,
    resolve_instruction_following_response,
)
from keystone_agents.live_retrieval import (
    company_source_matches_official_url,
    infer_official_company_url,
)
from keystone_agents.local_file_inputs import local_file_input_bundle_from_text
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.models import RunMode
from keystone_agents.operator_failures import (
    known_exception_to_operator_failure,
    operator_failure_from_mapping,
    redact_operator_text,
)
from keystone_agents.orchestration.stages import (
    advance_work_item_manager_loop,
    chief_workflow_requests_marked_airtable_test_lifecycle,
    inline_gmail_fixture_from_request,
)
from keystone_agents.orchestrator.preflight_context import (
    compact_orchestrator_preflight_payload,
    orchestrator_preflight_env,
    specialist_execution_context_text,
)
from keystone_agents.outreach_composer.execution_plan import infer_outreach_execution_plan
from keystone_agents.planning.compatibility import (
    has_explicit_local_attachment_context,
    has_materialized_slack_attachment_context,
    infer_manual_request_plan,
    is_internal_slack_composition_plan,
    live_search_allowed_for_execution,
    looks_like_stateful_work_request,
    looks_like_supplied_context_synthesis_request,
    positive_capability_text,
    request_forbids_live_research,
    resolve_manual_request_owner,
)
from keystone_agents.planning.composition_admission import (
    is_provider_free_selected_context_draft_plan,
)
from keystone_agents.presentation.public_result import attach_execution_public_result
from keystone_agents.presentation.renderers import (
    render_markdown_table,
    render_work_item_result_text,
    sensitive_text_summary,
)
from keystone_agents.quality_budget import is_bounded_chief_response_only_request
from keystone_agents.receipts.mutations import receipt_reports_possible_write
from keystone_agents.run import (
    extract_sdk_usage,
    run_typed_sdk_agent,
    sdk_input_from_typed_input,
)
from keystone_agents.schemas.approval import (
    ApprovalQueueItem,
    ApprovalQueueStatus,
    ApprovalScope,
    ApprovalState,
)
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.execution_request import (
    DirectAgentResponse,
    DirectAgentResponseInput,
)
from keystone_agents.schemas.manual_request_plan import (
    ManualProviderResultSetScope,
    ManualRequestPlan,
)
from keystone_agents.schemas.operational_context import (
    AirtableContextResult,
    GoogleWorkspaceContextResult,
    HumanWorkContext,
    OperationalContextEntry,
    OperationalContextSource,
    OperationalWritePlan,
    PreprintsContextResult,
    RssContextResult,
    ZoteroContextResult,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.sdk import run_typed_sdk_sync
from keystone_agents.sdk_run_policy import SDKTurnPolicy, resolve_sdk_turn_policy
from keystone_agents.sdk_sessions import (
    SDKSessionSpec,
    build_sdk_session,
    context_file_session_components,
    default_cli_ask_session_components,
    resolve_sdk_session_spec,
    sdk_session_env,
)
from keystone_agents.slack_action_contract import (
    KBA_EVAL_ORCHESTRATOR_JUDGE,
    KBA_EVAL_REVIEW,
    KBA_INTENT_EVAL_ORCHESTRATOR_JUDGE,
    KBA_INTENT_EVAL_REVIEW,
    business_agent_action_value,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env, redact_secrets
from keystone_agents.tools.gmail_tool import GmailTool
from keystone_agents.tools.google_calendar_tool import (
    GoogleCalendarError,
    create_google_calendar_event_impl,
    delete_google_calendar_event_impl,
    read_google_calendar_window_impl,
    resolve_google_calendar_event_impl,
    update_google_calendar_event_impl,
)
from keystone_agents.tools.internal_data_tools import (
    AIRTABLE_ALLOWED_OPERATION_ENV,
    AIRTABLE_LIVE_READS_ENV,
    GOOGLE_WORKSPACE_LIVE_READS_ENV,
    airtable_aggregate_records_impl,
    airtable_test_record_lifecycle_impl,
    google_doc_test_lifecycle_impl,
)
from keystone_agents.tools.slack_tool import SlackTool, slack_review_message_from_approval_item
from keystone_agents.tools.storage_tool import StorageTool
from keystone_agents.tools.zotero_context_tools import (
    enrich_zotero_bibliographic_metadata,
    project_zotero_item_metadata,
    read_latest_zotero_journal_abstract_metadata,
    read_latest_zotero_journal_metadata,
    read_zotero_api_key_capabilities,
    zotero_creator_names,
    zotero_read_item_children,
    zotero_read_pdf_attachment_text,
)
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
from keystone_agents.workflows import (
    pipeline_markdown_report,
    run_keystone_pipeline,
    run_opportunity_to_outreach_loop,
)


class _CLIEntryTelemetryScope:
    """Request-local CLI timing state with content-free persistence targets."""

    def __init__(self, *, database_url: str | None) -> None:
        self.recorder = ExecutionTelemetryRecorder()
        self.database_url = database_url
        self.store: SQLiteStore | None = None
        self.agent_run_id: int | None = None
        self.work_item_id = ""


class _ObservedCLIStream:
    """Delegate output byte-for-byte while observing its first write."""

    def __init__(self, stream: Any, recorder: ExecutionTelemetryRecorder) -> None:
        self._stream = stream
        self._recorder = recorder

    def write(self, value: str) -> int:
        written = self._stream.write(value)
        if value:
            self._recorder.mark_first_feedback()
        return written

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


_ASK_ENTRY_TELEMETRY: ContextVar[_CLIEntryTelemetryScope | None] = ContextVar(
    "keystone_cli_ask_entry_telemetry",
    default=None,
)


CONTEXT_AGENT_ROUTES = {
    "airtable_context_agent",
    "google_workspace_context_agent",
    "preprints_context_agent",
    "rss_context_agent",
    "zotero_context_agent",
}


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
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Use the SDK manual-request planner before agent execution. Live SDK mode "
            "enables it by default; use --no-live-manual-plan for a bounded run that "
            "must avoid the extra planner request."
        ),
    )
    ask.add_argument(
        "--max-openai-requests",
        type=int,
        default=None,
        help=(
            "Fail before any model call when the estimated planner, specialist, graph, "
            "and final-synthesis requests exceed this per-command ceiling."
        ),
    )
    ask.add_argument("--database-url", default=None, help="SQLite URL for WorkItem mode.")
    ask.add_argument(
        "--context-file",
        default="",
        help="Optional selected-context JSON file to attach to a WorkItem run.",
    )
    ask.add_argument(
        "--linked-work-item-id",
        default="",
        help=argparse.SUPPRESS,
    )
    ask.add_argument("--live-search", action="store_true", help="Use live search in WorkItem mode.")
    ask.add_argument(
        "--live-rss-slack-read",
        action="store_true",
        help=(
            "Allow a bounded read of RSS announcement history through Slack when the "
            "separate process-level read gate is enabled. Never posts."
        ),
    )
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
    work_items_advance.add_argument("--live-rss-slack-read", action="store_true")
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
    agents_tools = agent_subparsers.add_parser(
        "tools",
        help="Show sanitized runtime tool availability for registered agents.",
    )
    agents_tools.add_argument(
        "--agent",
        choices=["all", *sorted(AGENT_REGISTRY)],
        default="all",
        help="Agent route to inspect. Defaults to all agents.",
    )
    agents_tools.add_argument("--json", action="store_true", help="Print JSON.")
    agents_tools.set_defaults(func=_run_agents_tools)
    agents_file_search_config = agent_subparsers.add_parser(
        "file-search-config",
        help="Validate the ignored local FileSearch vector-store config.",
    )
    agents_file_search_config.add_argument("--json", action="store_true", help="Print JSON.")
    agents_file_search_config.set_defaults(func=_run_agents_file_search_config)

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


def execute_direct_calendar_action(
    input_text: str,
    plan: CalendarActionPlan,
    *,
    live: bool,
    openai_requests: int = 0,
    interpretation_warnings: tuple[str, ...] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute one validated Calendar request without entering a workflow graph."""

    if not plan.complete:
        missing = ", ".join(plan.blockers)
        payload = {
            "status": "blocked",
            "block_kind": "calendar_action_missing_required_field",
            "message": f"Calendar action needs only: {missing}.",
            "calendar_action": plan.__dict__,
            "selected_agent": "chief_of_staff",
            "openai_requests": openai_requests,
            "send_enabled": False,
        }
        if interpretation_warnings:
            payload["interpretation_warnings"] = list(interpretation_warnings)
        return payload

    approval_reference = (
        "calendar-direct:" + hashlib.sha256(input_text.encode("utf-8")).hexdigest()[:16]
    )
    resolved_event_id = plan.event_id
    calendar_lookup: dict[str, Any] | None = None
    window_read = plan.operation == "read" and plan.read_scope in {
        "time_window",
        "filtered_window",
    }
    try:
        if window_read:
            time_min, time_max = _calendar_window_bounds(plan, now=now)
            calendar_lookup = read_google_calendar_window_impl(
                time_min,
                time_max,
                calendar_id=plan.calendar_id,
                calendar_scope=plan.calendar_scope,
                query=plan.query,
                max_results=100,
                live=live,
            )
            if not live:
                return {
                    "status": "dry-run",
                    "mode": "dry_run",
                    "selected_agent": "chief_of_staff",
                    "route": "chief_of_staff",
                    "input": input_text,
                    "calendar_action": plan.__dict__,
                    "calendar_lookup": calendar_lookup,
                    "openai_requests": openai_requests,
                    "send_enabled": False,
                    "side_effects": {
                        "calendar_write_performed": False,
                        "email_sent": False,
                        "slack_message_posted": False,
                    },
                }
            events = sorted(
                (event for event in calendar_lookup.get("events", []) if isinstance(event, dict)),
                key=lambda event: str(event.get("start") or ""),
            )
            selected_event = (
                events[0]
                if events and (plan.read_selection == "next" or len(events) == 1)
                else None
            )
            found = bool(selected_event) if plan.read_selection == "next" else bool(events)
            result = {
                **calendar_lookup,
                "status": "success",
                "operation": "read_calendar_window",
                "found": found,
                "selected_event": selected_event,
                "verification": {
                    "status": "verified_present" if found else "verified_absent",
                    "passed": True,
                },
                "send_enabled": False,
            }
        elif plan.operation != "create" and not resolved_event_id:
            calendar_lookup = resolve_google_calendar_event_impl(
                plan.event_reference,
                start_date=plan.event_reference_date or plan.start_date,
                calendar_id=plan.calendar_id,
                live=live,
            )
            if not live:
                payload = {
                    "status": "dry-run",
                    "mode": "dry_run",
                    "selected_agent": "chief_of_staff",
                    "route": "chief_of_staff",
                    "input": input_text,
                    "calendar_action": plan.__dict__,
                    "calendar_lookup": calendar_lookup,
                    "openai_requests": openai_requests,
                    "send_enabled": False,
                    "side_effects": {
                        "calendar_write_performed": False,
                        "email_sent": False,
                        "slack_message_posted": False,
                    },
                }
                return payload
            lookup_status = str(calendar_lookup.get("status") or "")
            if lookup_status == "not_found" and plan.operation != "read":
                raise GoogleCalendarError(
                    f'No active Calendar event matched "{plan.event_reference}". '
                    "Name the event more specifically or include its date."
                )
            if lookup_status == "ambiguous":
                raise GoogleCalendarError(
                    f'More than one active Calendar event matched "{plan.event_reference}". '
                    "Include the event date or a more specific title."
                )
            resolved_event_id = str(calendar_lookup.get("event_id") or "")
            if plan.operation != "read" and not resolved_event_id:
                raise GoogleCalendarError("Calendar event lookup returned no exact identity.")
        if window_read:
            pass
        elif plan.operation == "read":
            found = bool(calendar_lookup and calendar_lookup.get("status") == "success")
            result = {
                **(calendar_lookup or {}),
                "status": "success",
                "operation": "read_calendar_event",
                "found": found,
                "verification": {
                    "status": "verified_present" if found else "verified_absent",
                    "passed": True,
                },
                "send_enabled": False,
            }
        elif plan.operation == "create":
            result = create_google_calendar_event_impl(
                plan.title,
                plan.start_date,
                description=plan.description,
                end_date=plan.end_date,
                repeat_each_day=plan.repeat_each_day,
                calendar_id=plan.calendar_id,
                timezone=plan.timezone,
                start_time=plan.start_time,
                end_time=plan.end_time,
                approval_reference=approval_reference,
                live=live,
            )
        elif plan.operation == "update":
            update_start_date = plan.start_date
            if plan.start_time and not update_start_date:
                update_start_date = plan.event_reference_date or str(
                    (calendar_lookup or {}).get("start_date") or ""
                )
            result = update_google_calendar_event_impl(
                resolved_event_id,
                title=plan.title,
                start_date=update_start_date,
                description=plan.description,
                calendar_id=plan.calendar_id,
                timezone=plan.timezone,
                start_time=plan.start_time,
                end_time=plan.end_time,
                append_description=plan.append_description,
                approval_reference=approval_reference,
                live=live,
            )
        else:
            result = delete_google_calendar_event_impl(
                resolved_event_id,
                calendar_id=plan.calendar_id,
                approval_reference=approval_reference,
                live=live,
            )
    except (GoogleCalendarError, RuntimeError, ValueError) as exc:
        payload = {
            "status": "blocked",
            "block_kind": (
                "calendar_read_blocked" if plan.operation == "read" else "calendar_write_blocked"
            ),
            "message": str(exc),
            "calendar_action": plan.__dict__,
            "selected_agent": "chief_of_staff",
            "openai_requests": openai_requests,
            "send_enabled": False,
        }
        if interpretation_warnings:
            payload["interpretation_warnings"] = list(interpretation_warnings)
        return payload

    passed = bool((result.get("verification") or {}).get("passed"))
    human_summary = _direct_calendar_human_summary(plan, result)
    payload = {
        "status": "done" if passed else str(result.get("status") or "failed"),
        "mode": (
            "live_calendar_read"
            if live and plan.operation == "read"
            else "live_calendar"
            if live
            else "dry_run"
        ),
        "selected_agent": "chief_of_staff",
        "route": "chief_of_staff",
        "input": input_text,
        "calendar_action": plan.__dict__,
        "calendar_lookup": calendar_lookup,
        "tool_receipt": result,
        "human_summary": human_summary,
        "slack_display_text": human_summary,
        "openai_requests": openai_requests,
        "send_enabled": False,
        "side_effects": {
            "calendar_write_performed": bool(live and passed and plan.operation != "read"),
            "email_sent": False,
            "slack_message_posted": False,
        },
    }
    if live:
        attach_execution_public_result(payload)
    return payload


def _direct_calendar_human_summary(
    plan: CalendarActionPlan,
    result: dict[str, Any],
) -> str:
    """Render a verified Calendar result without exposing workflow metadata."""

    if plan.operation == "read" and plan.read_scope in {
        "time_window",
        "filtered_window",
    }:
        events = [event for event in result.get("events", []) if isinstance(event, dict)]
        selected_event = result.get("selected_event")
        if isinstance(selected_event, dict):
            title = str(selected_event.get("title") or "Untitled event").strip()
            start_date = str(selected_event.get("start_date") or plan.start_date).strip()
            start_time = str(selected_event.get("start_time") or "").strip()
            end_time = str(selected_event.get("end_time") or "").strip()
            time_suffix = _calendar_human_time_suffix(start_time, end_time)
            if plan.read_selection == "next" and plan.date_scope == "today":
                return f'Your next Google Calendar event today is "{title}"{time_suffix}.'
            date_suffix = f" on {start_date}" if start_date else ""
            if plan.read_scope == "filtered_window":
                return f'Matching Google Calendar event: "{title}"{date_suffix}{time_suffix}.'
            return f'Next Google Calendar event: "{title}"{date_suffix}{time_suffix}.'
        if events:
            heading = (
                f'Google Calendar events matching "{plan.query}"'
                if plan.read_scope == "filtered_window"
                else "Google Calendar events"
            )
            lines = [f"{heading}:"]
            for event in events[:10]:
                title = str(event.get("title") or "Untitled event").strip()
                start_date = str(event.get("start_date") or plan.start_date).strip()
                start_time = str(event.get("start_time") or "").strip()
                end_time = str(event.get("end_time") or "").strip()
                date_suffix = f" on {start_date}" if start_date else ""
                lines.append(
                    f'- "{title}"{date_suffix}{_calendar_human_time_suffix(start_time, end_time)}'
                )
            return "\n".join(lines)
        if plan.read_selection == "next" and plan.date_scope == "today":
            return "You have no upcoming Google Calendar events today."
        if plan.read_scope == "filtered_window":
            date_suffix = f" on {plan.start_date}" if plan.start_date else ""
            return (
                f'No Google Calendar events matching "{plan.query}" were found'
                f"{date_suffix} in the connected account's selected calendars."
            )
        return "No Google Calendar events were found in the requested time window."

    title = str(result.get("title") or plan.title or plan.event_reference).strip()
    start_date = str(
        result.get("start_date") or plan.start_date or plan.event_reference_date
    ).strip()
    start_time = str(result.get("start_time") or plan.start_time).strip()
    end_time = str(result.get("end_time") or plan.end_time).strip()
    time_suffix = _calendar_human_time_suffix(start_time, end_time)
    if plan.operation == "read":
        if result.get("found") is True:
            date_suffix = f" on {start_date}" if start_date else ""
            return f'Yes - "{title}" is on your Google Calendar{date_suffix}{time_suffix}.'
        date_suffix = f" on {start_date}" if start_date else ""
        return (
            f'No - I did not find an active event named "{title}"'
            f"{date_suffix} on your Google Calendar."
        )
    operation = {
        "create": "created",
        "update": "updated",
        "delete": "deleted",
    }.get(plan.operation, "completed")
    date_suffix = f" on {start_date}" if start_date else ""
    return f'Google Calendar event {operation} and verified: "{title}"{date_suffix}.'


def _calendar_window_bounds(
    plan: CalendarActionPlan,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return exact RFC3339 bounds for a validated Calendar day window."""

    try:
        timezone = ZoneInfo(plan.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown Calendar timezone: {plan.timezone}") from exc
    try:
        window_date = datetime.strptime(plan.start_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Calendar window needs an exact YYYY-MM-DD date.") from exc
    local_now = now or datetime.now(timezone)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=timezone)
    else:
        local_now = local_now.astimezone(timezone)
    day_start = datetime.combine(window_date, datetime.min.time(), tzinfo=timezone)
    day_end = day_start + timedelta(days=1)
    window_start = (
        max(local_now, day_start)
        if plan.read_selection == "next" and window_date == local_now.date()
        else day_start
    )
    if window_start >= day_end:
        raise ValueError("The requested Calendar window has already ended.")
    return window_start.isoformat(), day_end.isoformat()


def _calendar_human_time_suffix(start_time: str, end_time: str) -> str:
    if not start_time:
        return ""
    human_start = _human_calendar_clock(start_time)
    human_end = _human_calendar_clock(end_time) if end_time else ""
    return f" from {human_start} to {human_end}" if human_end else f" at {human_start}"


def _human_calendar_clock(value: str) -> str:
    """Render one verified provider clock value without a leading zero."""

    try:
        return datetime.strptime(value[:5], "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return value


def run_direct_calendar_action(
    input_text: str,
    plan: CalendarActionPlan,
    *,
    live: bool,
    json_output: bool,
    openai_requests: int = 0,
    interpretation_warnings: tuple[str, ...] = (),
    execution_admission: ExecutionAdmission | None = None,
    manual_plan: ManualRequestPlan | None = None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    database_url: str | None = None,
) -> int:
    payload = execute_direct_calendar_action(
        input_text,
        plan,
        live=live,
        openai_requests=openai_requests,
        interpretation_warnings=interpretation_warnings,
    )
    if execution_admission is not None:
        payload["execution_admission"] = execution_admission.__dict__
    if live and manual_plan is not None:
        payload["manual_request_plan"] = manual_plan.model_dump(mode="json")
    if live and orchestrator_preflight is not None:
        payload["orchestrator_preflight"] = _orchestrator_preflight_payload(orchestrator_preflight)
    if live:
        _persist_direct_calendar_run(
            input_text=input_text,
            payload=payload,
            database_url=database_url,
        )
    return _print_direct_calendar_payload(payload, json_output=json_output)


def _persist_direct_calendar_run(
    *,
    input_text: str,
    payload: dict[str, Any],
    database_url: str | None,
) -> None:
    """Persist the same typed Calendar result rendered to Slack and CLI."""

    try:
        status = str(payload.get("status") or "failed")
        run_id = SQLiteStore(database_url or database_url_from_env()).save_agent_run(
            agent_name="chief_of_staff",
            input_payload={"request_text": input_text, "route": "chief_of_staff"},
            input_summary=input_text[:500],
            output=payload,
            model="calendar-action-after-orchestrator-preflight",
            dry_run=False,
            status="success" if status == "done" else "blocked",
            error=str(payload.get("block_kind") or "") or None,
        )
        payload["agent_run_id"] = run_id
        public_result = payload.get("public_result")
        if isinstance(public_result, dict):
            public_result["run_id"] = str(run_id)
    except Exception as exc:  # pragma: no cover - diagnostics must not mask execution
        payload["agent_run_persistence_error"] = f"{type(exc).__name__}: {exc}"


def _print_direct_calendar_payload(payload: dict[str, Any], *, json_output: bool) -> int:
    _register_entry_agent_run(payload.get("agent_run_id"))
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        receipt = payload.get("tool_receipt")
        if isinstance(receipt, dict):
            print(
                f"Calendar event {receipt.get('operation', 'action')}: "
                f"{receipt.get('title', '')} {receipt.get('start_date', '')}".strip()
            )
            if receipt.get("html_link"):
                print(f"Event: {receipt['html_link']}")
        else:
            print(str(payload.get("message") or "Calendar action could not be completed."))
    return 0 if payload.get("status") in {"done", "dry-run"} else 1


def _route_with_manual_plan_advice(route: str, manual_plan: ManualRequestPlan) -> str:
    requested_route = str(manual_plan.requested_agent or "").strip()
    if requested_route in {"", "orchestrator", "clarification"}:
        requested_route = route
    resolved = resolve_manual_request_owner(
        requested_route,
        manual_plan,
        request_text=manual_plan.objective,
    )
    if resolved not in AGENT_REGISTRY:
        return route
    return resolved


def _run_with_entry_telemetry(
    args: argparse.Namespace,
    handler: Callable[[], int],
) -> int:
    """Measure one CLI entry without changing its stdout or stderr contract."""

    if _ASK_ENTRY_TELEMETRY.get() is not None:
        return int(handler())
    scope = _CLIEntryTelemetryScope(
        database_url=str(getattr(args, "database_url", "") or "") or None,
    )
    token = _ASK_ENTRY_TELEMETRY.set(scope)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    observed_stdout = _ObservedCLIStream(original_stdout, scope.recorder)
    observed_stderr = _ObservedCLIStream(original_stderr, scope.recorder)
    telemetry_status = "completed"
    result: int | None = None
    failure: BaseException | None = None
    try:
        sys.stdout = observed_stdout
        sys.stderr = observed_stderr
        try:
            with scope.recorder.span(
                "entry.dispatch",
                attributes={"source": "cli"},
            ):
                result = int(handler())
        except BaseException as exc:
            telemetry_status = "failed"
            failure = exc
        observed_stdout.flush()
        observed_stderr.flush()
        if failure is None:
            scope.recorder.mark_final_response()
        else:
            failed_snapshot = scope.recorder.snapshot(status="failed")
            if failed_snapshot.first_feedback_ms is not None:
                scope.recorder.mark_final_response()
        telemetry = compact_execution_telemetry(
            scope.recorder.snapshot(status=telemetry_status)
        )
        _persist_entry_execution_telemetry(scope, telemetry)
        if failure is not None:
            raise failure
        return int(result or 0)
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        _ASK_ENTRY_TELEMETRY.reset(token)


def _register_entry_agent_run(run_id: object) -> None:
    scope = _ASK_ENTRY_TELEMETRY.get()
    if scope is None:
        return
    try:
        scope.agent_run_id = int(run_id)
    except (TypeError, ValueError):
        return


def _register_entry_work_item(work_item_id: object) -> None:
    scope = _ASK_ENTRY_TELEMETRY.get()
    if scope is not None:
        scope.work_item_id = str(work_item_id or "").strip()


def _register_entry_store(store: SQLiteStore) -> None:
    scope = _ASK_ENTRY_TELEMETRY.get()
    if scope is not None:
        scope.store = store


def _persist_entry_execution_telemetry(
    scope: _CLIEntryTelemetryScope,
    telemetry: dict[str, Any],
) -> None:
    """Persist only the compact timing projection, never rendered content."""

    if not telemetry:
        return
    try:
        store = scope.store or SQLiteStore(
            scope.database_url or database_url_from_env()
        )
        if scope.agent_run_id is not None:
            store.annotate_agent_run_execution_telemetry(
                scope.agent_run_id,
                telemetry=telemetry,
            )
        if scope.work_item_id:
            item = store.get_work_item(scope.work_item_id)
            if item is not None:
                record_event(
                    item,
                    event_type="entrypoint_execution_telemetry",
                    summary="Recorded content-free CLI entry timing.",
                    metadata={"execution_telemetry": telemetry},
                    store=store,
                )
    except Exception:
        # Performance telemetry is advisory and must not change task execution.
        return


def _run_ask(args: argparse.Namespace) -> int:
    return _run_with_entry_telemetry(
        args,
        lambda: (
            _run_live_ask_with_environment(args)
            if args.live_sdk is True
            else _run_ask_with_current_environment(args)
        ),
    )


@with_cli_environment(force_dotenv=True)
def _run_live_ask_with_environment(args: argparse.Namespace) -> int:
    return _run_ask_with_current_environment(args)


def _run_ask_with_current_environment(args: argparse.Namespace) -> int:
    raw_input = _ask_input(args)
    slack_context_input = _context_file_is_slack_context(args.context_file)
    linked_work_item = _validated_slack_linked_work_item(
        work_item_id=str(getattr(args, "linked_work_item_id", "") or "").strip(),
        database_url=args.database_url,
        context_file_path=args.context_file,
    )
    execution_request = build_execution_request(
        raw_input,
        requested_agent=args.agent,
        slack_context_input=slack_context_input,
    )
    slack_continuation = execution_request.entrypoint == "slack_followup"
    operator_input = execution_request.current_request
    planning_input = execution_request_planning_text(execution_request)
    mention = _parse_ask_agent_mention(
        operator_input,
        slack_context_input=slack_context_input,
        slack_continuation=slack_continuation,
    )
    if (
        args.agent is None
        and execution_request.requested_agent_explicit
        and execution_request.requested_agent in AGENT_REGISTRY
    ):
        mention = AgentMention(
            route=execution_request.requested_agent,
            agent_name=_agent_display_name(execution_request.requested_agent),
            input_text=execution_request.current_request,
            explicit=True,
        )
    current_input_text = (
        operator_input
        if args.agent
        else mention.input_text
        if mention.explicit
        else execution_request.current_request
    )
    requested_route = (
        args.agent
        or execution_request.requested_agent
        or (mention.route if mention.explicit else None)
    )
    execution_input_text = (
        planning_input
        if slack_continuation and planning_input
        else current_input_text
    )
    semantic_input_text = current_input_text
    cost_directive = parse_cost_tracking_directive(current_input_text)
    input_text = (cost_directive.cleaned_text or current_input_text).strip()
    if semantic_input_text != current_input_text:
        semantic_cost_directive = parse_cost_tracking_directive(semantic_input_text)
        semantic_input_text = (semantic_cost_directive.cleaned_text or semantic_input_text).strip()
    else:
        semantic_input_text = input_text
    if has_materialized_slack_attachment_context(input_text):
        attachment_bundle = local_file_input_bundle_from_text(input_text)
        if not attachment_bundle.has_inputs:
            attachment_plan = infer_manual_request_plan(
                semantic_input_text,
                requested_agent=requested_route,
            )
            return _print_local_attachment_unavailable(
                str(attachment_plan.target_agent or requested_route or "chief_of_staff"),
                input_text,
                attachment_bundle.diagnostics,
                json_output=args.json,
                manual_plan=attachment_plan,
                orchestrator_preflight=None,
            )
    eval_command_allowed = args.agent is None or _looks_like_explicit_eval_command(input_text)
    if not _promptfoo_agent_eval_mode() and eval_command_allowed:
        eval_score_save = _eval_score_save_payload(input_text, context_file_path=args.context_file)
        if eval_score_save is not None:
            return _print_eval_score_saved(eval_score_save, json_output=args.json)
        eval_score_template = _eval_score_template_payload(
            input_text,
            context_file_path=args.context_file,
        )
        if eval_score_template is not None:
            return _print_eval_score_template(eval_score_template, json_output=args.json)
        eval_status = _eval_status_payload(input_text, context_file_path=args.context_file)
        if eval_status is not None:
            return _print_eval_status(eval_status, json_output=args.json)
    live_sdk = _ask_live_sdk_enabled(args, input_text=input_text)
    calendar_input_text = raw_input if slack_continuation else input_text
    calendar_plan = infer_calendar_action_plan(calendar_input_text)
    calendar_candidate = bool(
        calendar_plan is not None or is_calendar_action_candidate(calendar_input_text)
    )
    live_search_capability_enabled = args.live_search or (
        live_sdk and cli_default_live_research()
    )
    live_search = live_search_capability_enabled
    if live_search and (
        _request_forbids_live_research(semantic_input_text)
        or _context_file_is_work_item_source_bundle(args.context_file)
    ):
        live_search = False
    live_manual_plan_requested = (
        live_sdk if args.live_manual_plan is None else bool(args.live_manual_plan)
    )
    # Natural-language wording never bypasses semantic planning. Harnesses that
    # deliberately omit the planner must use the typed --no-live-manual-plan
    # control instead of embedding magic phrases such as "bounded smoke" in
    # the operator request.
    live_manual_plan = live_manual_plan_requested
    request_estimate = _estimate_ask_openai_requests(
        args,
        input_text=semantic_input_text,
        live_sdk=live_sdk,
        live_manual_plan=live_manual_plan,
        requested_route=requested_route,
        effective_live_search=live_search,
    )
    # Semantic interpretation is a single bounded request. Do not block that
    # planner call using the unresolved worst-case manager/graph estimate.
    # Recompute and enforce the actual route ceiling immediately after preflight.
    live_unowned_calendar_followup = bool(
        live_sdk
        and slack_continuation
        and args.agent is None
        and not execution_request.requested_agent_explicit
    )
    calendar_route_eligible = bool(
        (not live_sdk or live_unowned_calendar_followup)
        and calendar_candidate
        and requested_route
        in {
            None,
            "chief_of_staff",
            "orchestrator",
        }
    )
    preflight_budget_estimate = (
        1 if live_manual_plan else 0 if calendar_route_eligible else request_estimate["max"]
    )
    if args.max_openai_requests is not None and (
        args.max_openai_requests < 0 or preflight_budget_estimate > args.max_openai_requests
    ):
        return _print_ask_request_budget_blocked(
            json_output=args.json,
            requested_limit=args.max_openai_requests,
            estimate=request_estimate,
        )
    direct_workflow_state = _orchestrator_workflow_state_from_cli_context(
        context_file_path=args.context_file,
        request_text=input_text,
        database_url=args.database_url,
        work_item=linked_work_item,
    )
    if slack_continuation:
        direct_workflow_state = _merge_direct_workflow_state(
            direct_workflow_state,
            _slack_continuation_workflow_state(
                raw_input,
                prior_agent=execution_request.continuation.prior_agent,
            ),
        )
        direct_workflow_state = _merge_direct_workflow_state(
            direct_workflow_state,
            {"execution_continuation": (execution_request.continuation.model_dump(mode="json"))},
        )
        calendar_input_text = _calendar_continuation_input_text(
            raw_input,
            direct_workflow_state,
            prior_request=execution_request.continuation.prior_request,
        )
        calendar_plan = infer_calendar_action_plan(calendar_input_text)
    orchestrator_preflight = run_orchestrator_preflight(
        semantic_input_text,
        requested_agent=requested_route,
        live_manual_plan=live_manual_plan,
        database_url=args.database_url,
        workflow_state=direct_workflow_state,
    )
    orchestrator_preflight = _apply_continuation_provider_affinity(
        orchestrator_preflight,
        execution_request.continuation.provider_affinity,
    )
    continuity_route = continuation_owner_advice(
        execution_request,
        orchestrator_preflight.manual_request_plan,
    )
    if continuity_route in AGENT_REGISTRY:
        continuity_warning = (
            "Retained the prior task owner for a provider-free continuation of "
            "the same artifact; the current operator request remains authoritative."
        )
        continuity_plan = orchestrator_preflight.manual_request_plan.model_copy(
            update={
                "target_agent": continuity_route,
                "requires_approved_context": False,
                "planner_warnings": list(
                    dict.fromkeys(
                        [
                            *orchestrator_preflight.manual_request_plan.planner_warnings,
                            continuity_warning,
                        ]
                    )
                ),
            }
        )
        orchestrator_preflight = orchestrator_preflight.model_copy(
            update={
                "selected_agent": continuity_route,
                "manual_request_plan": continuity_plan,
                "route_result": orchestrator_preflight.route_result.model_copy(
                    update={"route": continuity_route}
                ),
            }
        )
    manual_plan = orchestrator_preflight.manual_request_plan
    live_search = live_search_allowed_for_execution(
        live_search_capability_enabled,
        manual_plan=manual_plan,
        request_text=semantic_input_text,
    )
    if _context_file_is_work_item_source_bundle(args.context_file):
        live_search = False
    if manual_plan.provider_system != "unspecified" and not manual_plan.requires_live_search:
        live_search = False
    calendar_semantic_candidate = bool(
        ExecutionIntentAuthority.from_value(manual_plan).canonical
        and manual_plan.provider_system == "google_calendar"
        and manual_plan.target_agent == "chief_of_staff"
        and manual_plan.intent in {"business_system_write", "context_lookup"}
        and (
            manual_plan.intent == "context_lookup" or not live_sdk or live_unowned_calendar_followup
        )
    )
    calendar_interpretation_eligible = bool(
        calendar_semantic_candidate
        or (
            calendar_route_eligible
            and manual_plan.target_agent == "chief_of_staff"
            and manual_plan.intent == "business_system_write"
        )
    )
    if calendar_interpretation_eligible:
        preflight_requests = _orchestrator_preflight_request_count(orchestrator_preflight)
        calendar_request_estimate = {
            "min": preflight_requests + (1 if live_sdk else 0),
            "max": preflight_requests + (1 if live_sdk else 0),
            "stages": [
                *(["manual_request_planner"] if preflight_requests else []),
                *(["calendar_action_interpreter"] if live_sdk else []),
            ],
        }
        if args.max_openai_requests is not None and (
            args.max_openai_requests < 0
            or calendar_request_estimate["max"] > args.max_openai_requests
        ):
            return _print_ask_request_budget_blocked(
                json_output=args.json,
                requested_limit=args.max_openai_requests,
                estimate=calendar_request_estimate,
                openai_requests_made=preflight_requests,
            )
        if _preflight_blocks_execution(orchestrator_preflight):
            return _print_ask_preflight_blocked(
                input_text,
                json_output=args.json,
                orchestrator_preflight=orchestrator_preflight,
            )
        calendar_resolution = resolve_calendar_action_plan(
            calendar_input_text,
            calendar_plan,
            manual_plan=manual_plan,
            semantic_candidate=calendar_semantic_candidate,
            live=live_sdk,
        )
        if calendar_resolution.plan is not None:
            calendar_admission = admit_provider_action(
                provider="google_calendar",
                provider_action_bound=True,
                semantic_plan=manual_plan,
                allowed_agents={"chief_of_staff"},
                allowed_intents={"business_system_write", "context_lookup"},
            )
            if calendar_admission.can_execute_provider_action:
                return run_direct_calendar_action(
                    calendar_input_text,
                    calendar_resolution.plan,
                    live=live_sdk,
                    json_output=args.json,
                    openai_requests=(preflight_requests + calendar_resolution.openai_requests),
                    interpretation_warnings=calendar_resolution.warnings,
                    execution_admission=calendar_admission,
                    manual_plan=manual_plan,
                    orchestrator_preflight=orchestrator_preflight,
                    database_url=args.database_url,
                )
    interpreted_lifecycle_route = str(manual_plan.target_agent or "").strip()
    interpreted_lifecycle_scope = _interpreted_lifecycle_scope_text(
        input_text,
        manual_plan,
    )
    interpreted_bounded_lifecycle = bool(
        live_sdk
        and interpreted_lifecycle_route
        in {
            "airtable_context_agent",
            "google_workspace_context_agent",
            "gmail_triage",
        }
        and _is_bounded_composite_lifecycle_request(
            interpreted_lifecycle_route,
            input_text=interpreted_lifecycle_scope,
            manual_plan=manual_plan,
        )
    )
    semantic_direct_route = (
        _bounded_direct_route_from_preflight(orchestrator_preflight)
        if args.agent is None and not mention.explicit
        else None
    )
    if interpreted_bounded_lifecycle:
        preflight_requests = _orchestrator_preflight_request_count(orchestrator_preflight)
        resolved_request_estimate = {
            "min": preflight_requests,
            "max": preflight_requests,
            "stages": [
                "manual_request_planner",
                f"{interpreted_lifecycle_route}_typed_lifecycle_provider",
            ],
        }
    else:
        resolved_request_estimate = _estimate_ask_openai_requests(
            args,
            input_text=semantic_input_text,
            live_sdk=live_sdk,
            live_manual_plan=live_manual_plan,
            requested_route=requested_route or semantic_direct_route,
            manual_plan=manual_plan,
            effective_live_search=live_search,
            provider_free_composition_allowed=bool(
                orchestrator_preflight.composition_admission.composition_allowed
            ),
        )
    if args.max_openai_requests is not None and (
        args.max_openai_requests < 0 or resolved_request_estimate["max"] > args.max_openai_requests
    ):
        return _print_ask_request_budget_blocked(
            json_output=args.json,
            requested_limit=args.max_openai_requests,
            estimate=resolved_request_estimate,
            openai_requests_made=_orchestrator_preflight_request_count(orchestrator_preflight),
        )
    direct_execution_context = _direct_specialist_execution_context(
        input_text,
        workflow_state=direct_workflow_state,
        force_thread_context=slack_continuation,
    )
    if _preflight_blocks_execution(orchestrator_preflight):
        return _print_ask_preflight_blocked(
            input_text,
            json_output=args.json,
            orchestrator_preflight=orchestrator_preflight,
        )
    if (
        manual_plan.target_agent == "clarification"
        or orchestrator_preflight.route_result.route == "clarification"
    ):
        clarification = (
            orchestrator_preflight.route_result.clarification_request
            or orchestrator_preflight.route_result.stop_reason
            or "Please provide the missing item, target, or requested change."
        )
        return _print_ask_clarification(
            "orchestrator",
            input_text,
            clarification,
            json_output=args.json,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            extra={
                "status": "needs_input",
                "route": "clarification",
                "block_kind": "clarification_required",
            },
        )
    if _manual_plan_reuses_linked_work_item(
        manual_plan,
        work_item=linked_work_item,
    ):
        return _run_ask_work_item(
            semantic_input_text,
            database_url=args.database_url,
            live_search=live_search,
            live_sdk=live_sdk,
            live_rss_slack_read=args.live_rss_slack_read,
            max_results=args.max_results,
            max_manager_steps=args.max_manager_steps,
            json_output=args.json,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            context_file_path=args.context_file,
            sdk_session_enabled=args.sdk_session,
            sdk_session_id=args.sdk_session_id,
            sdk_session_db_path=args.sdk_session_db,
            sdk_session_history_limit=args.sdk_session_history_limit,
            cost_tracking_requested=cost_directive.requested,
            requested_route=str(manual_plan.target_agent or "").strip() or None,
            explicit_work_item_id=linked_work_item.id,
        )
    if live_sdk:
        lifecycle_route = interpreted_lifecycle_route
        if lifecycle_route not in {
            "airtable_context_agent",
            "google_workspace_context_agent",
            "gmail_triage",
        }:
            lifecycle_route = str(requested_route or semantic_direct_route or "").strip()
        lifecycle_exit_code = _run_bounded_provider_lifecycle_after_preflight(
            lifecycle_route,
            input_text,
            lifecycle_scope_text=interpreted_lifecycle_scope,
            context_text=raw_input,
            json_output=args.json,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            database_url=args.database_url,
        )
        if lifecycle_exit_code is not None:
            return lifecycle_exit_code
    if args.agent is None:
        if _should_run_opportunity_to_outreach_loop(
            manual_plan,
            explicit_route=mention.route if mention.explicit else None,
        ):
            return _run_ask_opportunity_to_outreach_loop(
                semantic_input_text,
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
                    context_file_path=args.context_file,
                ),
                cost_tracking_requested=cost_directive.requested,
            )
        if _preflight_requires_work_item(
            orchestrator_preflight,
            request_text=semantic_input_text,
        ):
            requested_work_item_route = _preflight_work_item_entry_route(
                orchestrator_preflight
            )
            return _run_ask_work_item(
                execution_input_text,
                database_url=args.database_url,
                live_search=live_search,
                live_sdk=live_sdk,
                live_rss_slack_read=args.live_rss_slack_read,
                max_results=args.max_results,
                max_manager_steps=args.max_manager_steps,
                json_output=args.json,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                context_file_path=args.context_file,
                sdk_session_enabled=args.sdk_session,
                sdk_session_id=args.sdk_session_id,
                sdk_session_db_path=args.sdk_session_db,
                sdk_session_history_limit=args.sdk_session_history_limit,
                cost_tracking_requested=cost_directive.requested,
                requested_route=requested_work_item_route,
            )
        if live_sdk and (
            (mention.explicit and mention.route is not None) or semantic_direct_route is not None
        ):
            route = (
                _route_with_manual_plan_advice(str(mention.route), manual_plan)
                if mention.explicit and mention.route is not None
                else str(semantic_direct_route)
            )
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
                        context_file_path=args.context_file,
                    ),
                )
            return _run_ask_specialist_live(
                route,
                execution_input_text if route == "chief_of_staff" else input_text,
                json_output=args.json,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                sdk_session_spec=_sdk_session_spec_for_ask(
                    args,
                    route=route,
                    default_enabled=_ask_route_session_default(route),
                    context_file_path=args.context_file,
                ),
                execution_context=direct_execution_context,
                context_file_path=args.context_file,
                cost_tracking_requested=cost_directive.requested,
                database_url=args.database_url,
            )
        typed_context_continuation = bool(
            slack_continuation
            and semantic_direct_route in CONTEXT_AGENT_ROUTES
            and manual_plan.target_agent == semantic_direct_route
            and manual_plan.provider_system != "unspecified"
        )
        if (
            mention.explicit
            and (
                mention.route in CONTEXT_AGENT_ROUTES
                or (
                    mention.route in {"chief_of_staff", "orchestrator"}
                    and manual_plan.target_agent in CONTEXT_AGENT_ROUTES
                )
            )
        ) or typed_context_continuation:
            route = str(manual_plan.target_agent)
            return _print_ask_dry_run(
                route,
                input_text,
                json_output=args.json,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                database_url=args.database_url,
                execution_context=direct_execution_context,
            )
        if (
            mention.explicit
            and mention.route in _DIRECT_SUPPLIED_RESPONSE_ROUTES
            and _should_run_direct_supplied_response(
                input_text,
                requested_route=str(mention.route),
                manual_plan=manual_plan,
            )
        ):
            route = _route_with_manual_plan_advice(str(mention.route), manual_plan)
            return _print_ask_dry_run(
                route,
                input_text,
                json_output=args.json,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                database_url=args.database_url,
                execution_context=direct_execution_context,
            )
        return _run_ask_work_item(
            execution_input_text,
            database_url=args.database_url,
            live_search=live_search,
            live_sdk=live_sdk,
            live_rss_slack_read=args.live_rss_slack_read,
            max_results=args.max_results,
            max_manager_steps=args.max_manager_steps,
            json_output=args.json,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            context_file_path=args.context_file,
            sdk_session_enabled=args.sdk_session,
            sdk_session_id=args.sdk_session_id,
            sdk_session_db_path=args.sdk_session_db,
            sdk_session_history_limit=args.sdk_session_history_limit,
            cost_tracking_requested=cost_directive.requested,
        )
    route = args.agent
    if _preflight_requires_work_item(
        orchestrator_preflight,
        request_text=semantic_input_text,
    ):
        requested_work_item_route = _preflight_work_item_entry_route(
            orchestrator_preflight
        )
        return _run_ask_work_item(
            execution_input_text,
            database_url=args.database_url,
            live_search=live_search,
            live_sdk=live_sdk,
            live_rss_slack_read=args.live_rss_slack_read,
            max_results=args.max_results,
            max_manager_steps=args.max_manager_steps,
            json_output=args.json,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            context_file_path=args.context_file,
            sdk_session_enabled=args.sdk_session,
            sdk_session_id=args.sdk_session_id,
            sdk_session_db_path=args.sdk_session_db,
            sdk_session_history_limit=args.sdk_session_history_limit,
            cost_tracking_requested=cost_directive.requested,
            requested_route=requested_work_item_route,
        )
    if route == "orchestrator":
        if _context_file_is_work_item_source_bundle(args.context_file):
            return _run_ask_work_item(
                input_text,
                database_url=args.database_url,
                live_search=live_search,
                live_sdk=live_sdk,
                live_rss_slack_read=args.live_rss_slack_read,
                max_results=args.max_results,
                max_manager_steps=args.max_manager_steps,
                json_output=args.json,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                context_file_path=args.context_file,
                sdk_session_enabled=args.sdk_session,
                sdk_session_id=args.sdk_session_id,
                sdk_session_db_path=args.sdk_session_db,
                sdk_session_history_limit=args.sdk_session_history_limit,
                cost_tracking_requested=cost_directive.requested,
            )
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
                context_file_path=args.context_file,
            ),
            cost_tracking_requested=cost_directive.requested,
        )
    if live_sdk:
        route = _route_with_manual_plan_advice(route, manual_plan)
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
                    context_file_path=args.context_file,
                ),
                cost_tracking_requested=cost_directive.requested,
            )
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
                        context_file_path=args.context_file,
                    ),
                    cost_tracking_requested=cost_directive.requested,
                )
        return _run_ask_specialist_live(
            route,
            execution_input_text if route == "chief_of_staff" else input_text,
            json_output=args.json,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=_sdk_session_spec_for_ask(
                args,
                route=route,
                default_enabled=_ask_route_session_default(route),
                context_file_path=args.context_file,
            ),
            execution_context=direct_execution_context,
            context_file_path=args.context_file,
            cost_tracking_requested=cost_directive.requested,
            database_url=args.database_url,
        )
    return _print_ask_dry_run(
        route,
        execution_input_text if route == "chief_of_staff" else input_text,
        json_output=args.json,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        database_url=args.database_url,
        execution_context=direct_execution_context,
    )


def _parse_ask_agent_mention(
    operator_input: str,
    *,
    slack_context_input: bool,
    slack_continuation: bool,
) -> AgentMention:
    """Parse a recovered Slack ask without dropping a newest bare agent switch."""

    return parse_agent_mention(
        operator_input,
        allow_bare_context_agents=True,
        allow_bare_agent_aliases=slack_context_input or slack_continuation,
    )


def _latest_slack_operator_request(text: str) -> str:
    """Compatibility wrapper around the canonical entrypoint normalizer."""

    return latest_slack_operator_request(text)


def _slack_continuation_workflow_state(
    text: str,
    *,
    prior_agent: str = "",
) -> dict[str, Any]:
    """Recover bounded prior-result identity when Slack omits a context file."""

    raw = html.unescape(str(text or "").strip())
    if "continue this prior slack thread" not in raw.lower():
        return {}
    advisory_route = str(prior_agent or "").strip().lower()
    if advisory_route not in AGENT_REGISTRY:
        advisory_route = ""
    prior_runs: list[dict[str, str]] = []
    allow_failed_context = slack_work_item_control_requested(_latest_slack_operator_request(raw))
    pattern = re.compile(
        r"Previous result title:\s*(.*?)\s+Previous result:\s*(.*?)"
        r"(?=\s+(?:User follow-up:|Previous result title:)|$)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    matches = list(pattern.finditer(raw))
    for index, match in enumerate(matches, start=1):
        result_label = _bounded_redacted_text(match.group(1), max_chars=180)
        summary = _bounded_redacted_text(match.group(2), max_chars=900)
        result_status = _slack_result_status(result_label)
        if (
            (not result_label and not summary)
            or summary.lower() == "not available"
            or (result_status != "completed" and not allow_failed_context)
        ):
            continue
        route = _route_from_slack_result_label(result_label)
        if not route and result_status == "completed" and index == len(matches):
            # The Slack renderer intentionally uses generic success headings.
            # Recover the latest completed result's owner from the adapter's
            # separate advisory field instead of guessing from result prose.
            route = advisory_route
        object_title = _object_title_from_slack_result(summary)
        prior_runs.append(
            {
                key: value
                for key, value in {
                    "id": f"slack-envelope-{index}",
                    "route": route,
                    "status": result_status,
                    "thread_correlation": "same_thread",
                    "title": object_title or result_label,
                    "summary": summary,
                }.items()
                if value
            }
        )
    if not prior_runs:
        return {}
    return {"prior_agent_runs": prior_runs[-4:]}


def _slack_result_status(result_label: str) -> str:
    normalized = " ".join(str(result_label or "").lower().split())
    if "running" in normalized:
        return "running"
    if "need input" in normalized or "needs context" in normalized:
        return "needs_input"
    if any(
        marker in normalized
        for marker in (
            "awaiting approval",
            "approval required",
            "need review",
            "needs review",
            "pending approval",
            "run update",
        )
    ):
        return "needs_input"
    if any(marker in normalized for marker in ("failed", "blocked", "completion not confirmed")):
        return "blocked"
    return "completed"


def _calendar_continuation_input_text(
    raw_input: str,
    workflow_state: Mapping[str, Any],
    *,
    prior_request: str = "",
) -> str:
    """Give Calendar interpretation the latest human task anchor, never bot prose."""

    root_request = _bounded_redacted_text(
        workflow_state.get("slack_thread_root"),
        max_chars=2400,
    )
    current_request = _latest_slack_operator_request(raw_input)
    previous_request = _latest_distinct_slack_operator_turn(
        workflow_state,
        current_request=current_request,
    ) or _bounded_redacted_text(prior_request, max_chars=2400)
    if (
        previous_request
        and root_request
        and " ".join(previous_request.lower().split()) != " ".join(root_request.lower().split())
    ):
        task_anchor = f"{root_request} Most recent operator turn: {previous_request}"
    else:
        task_anchor = previous_request or root_request
    if not task_anchor or not current_request:
        return raw_input
    return "\n".join(
        (
            "business agents continue this prior Slack thread.",
            f"Previous request: {task_anchor}",
            f"User follow-up: {current_request}",
            "Continue the same agent task.",
        )
    )


def _latest_distinct_slack_operator_turn(
    workflow_state: Mapping[str, Any],
    *,
    current_request: str,
) -> str:
    """Recover one prior human turn from typed Slack history, ignoring bot output."""

    messages = workflow_state.get("recent_slack_thread")
    if not isinstance(messages, list):
        return ""
    current = " ".join(current_request.lower().split())
    for message in reversed(messages):
        if not isinstance(message, Mapping) or message.get("role") != "operator":
            continue
        candidate = _bounded_redacted_text(message.get("text"), max_chars=2400)
        if candidate and " ".join(candidate.lower().split()) != current:
            return candidate
    return ""


def _route_from_slack_result_label(value: str) -> str:
    normalized = " ".join(str(value or "").lower().split())
    route_markers = (
        ("zotero", "zotero_context_agent"),
        ("gmail", "gmail_triage"),
        ("opportunity", "opportunity_scout"),
        ("outreach", "outreach_composer"),
        ("company research", "business_research_analyst"),
        ("business research", "business_research_analyst"),
        ("google workspace", "google_workspace_context_agent"),
        ("calendar", "chief_of_staff"),
        ("chief of staff", "chief_of_staff"),
    )
    return next((route for marker, route in route_markers if marker in normalized), "")


def _object_title_from_slack_result(value: str) -> str:
    match = re.search(
        r"(?:^|\s)Title:\s*(.*?)(?=\s+(?:Summary:|Abstract summary:)|$)",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    return _bounded_redacted_text(match.group(1), max_chars=240) if match else ""


def _merge_direct_workflow_state(
    primary: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Merge continuation evidence without replacing richer Slack context."""

    if not fallback:
        return primary
    merged = dict(primary or {})
    for key, value in fallback.items():
        if key == "prior_agent_runs" and isinstance(value, list):
            existing = merged.get(key)
            combined = [*(existing if isinstance(existing, list) else []), *value]
            deduped: list[Any] = []
            seen: set[tuple[str, str, str]] = set()
            for item in combined:
                if not isinstance(item, dict):
                    continue
                identity = (
                    str(item.get("route") or ""),
                    str(item.get("title") or ""),
                    str(item.get("summary") or ""),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                deduped.append(item)
            merged[key] = deduped[-5:]
        elif key not in merged:
            merged[key] = value
    return merged


def _eval_score_save_payload(
    input_text: str,
    *,
    context_file_path: str = "",
) -> dict[str, object] | None:
    compact = " ".join(str(input_text or "").split())
    lowered = compact.lower()
    inferred = _eval_context_fields(context_file_path)
    if not (re.search(r"\b(?:eval|evaluation|promptfoo)\b", lowered) or inferred.get("case_id")):
        return None
    if not _looks_like_eval_human_score_reply(input_text):
        return None

    try:
        from promptfoo.eval_database import record_eval_trace_event, resolve_human_review_target
        from promptfoo.human_review import (
            DEFAULT_REVIEW_DB,
            parse_human_review,
            save_human_review,
        )
    except ImportError:
        return {
            "mode": "eval_score_saved",
            "status": "blocked",
            "route": "orchestrator",
            "human_summary": (
                "Promptfoo human-review helpers are not available in this environment."
            ),
            "send_enabled": False,
        }

    slack_context = _slack_context_metadata(context_file_path)
    try:
        review = parse_human_review(
            input_text,
            case_id=inferred.get("case_id", ""),
            run_id=inferred.get("run_id", ""),
            agent=inferred.get("agent", ""),
            slack_channel_id=slack_context.get("channel_id", "C0BA17Y9C01"),
            slack_channel_name=slack_context.get("channel_name", "evals"),
            slack_thread_ts=slack_context.get("thread_ts", ""),
        )
    except ValueError as exc:
        return {
            "mode": "eval_score_saved",
            "status": "blocked",
            "route": "orchestrator",
            "human_summary": f"Eval score was not saved: {exc}",
            "error": str(exc),
            "send_enabled": False,
        }

    database_path = Path(os.environ.get("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB") or DEFAULT_REVIEW_DB)
    try:
        review = resolve_human_review_target(
            review,
            database_path=database_path,
            require_recorded_response=True,
        )
    except ValueError as exc:
        return {
            "mode": "eval_score_saved",
            "status": "blocked",
            "route": "orchestrator",
            "human_summary": f"Eval score was not saved: {exc}",
            "error": str(exc),
            "send_enabled": False,
        }
    row_id = save_human_review(review, database_path=database_path)
    record_eval_trace_event(
        event_type="human_review_saved",
        trace_id=f"human_review:{row_id}",
        span_id=f"human_review_row:{row_id}",
        name="human_eval_review_saved",
        group_id=review.case_id,
        metadata={
            "schema": "keystone.eval_manual_trace.v1",
            "case_id": review.case_id,
            "run_id": review.run_id,
            "agent": review.agent,
            "source": "human_review",
            "average_score": review.average_score,
            "safety": review.safety,
            "score_dimension_count": len(review.scores),
            "slack_channel_name": review.slack_channel_name,
            "has_slack_thread_ts": bool(review.slack_thread_ts),
            "row_id": row_id,
        },
        database_path=database_path,
    )
    dashboard = _render_promptfoo_dashboard(database_path)
    case_dashboard = _dashboard_case_link(dashboard, review.case_id)
    payload = review.to_dict()
    summary = (
        f"Eval score saved for `{review.case_id}`"
        + (f" / `{review.run_id}`" if review.run_id else "")
        + f" with average {review.average_score}/5."
    )
    if case_dashboard.get("dashboard_case_url"):
        summary += (
            f" Dashboard: {_slack_link(case_dashboard['dashboard_case_url'], 'case dashboard')}."
        )
    elif dashboard.get("dashboard_path"):
        summary += f" Dashboard: {dashboard['dashboard_path']}."
    payload.update(
        {
            "id": row_id,
            "mode": "eval_score_saved",
            "status": "done",
            "route": "orchestrator",
            "human_summary": summary,
            "database_path": str(database_path),
            "refresh_targets": ["overview", "database", "runs_scoring", "analysis"],
            "trace_event_type": "human_review_saved",
            "eval_thread_reply": _eval_thread_reply_guidance(
                case_id=review.case_id,
                run_id=review.run_id,
                agent=review.agent,
                slack_thread_ts=review.slack_thread_ts,
                dashboard_case_url=case_dashboard.get("dashboard_case_url", ""),
            ),
            "send_enabled": False,
            **dashboard,
            **case_dashboard,
        }
    )
    return payload


def _eval_score_template_payload(
    input_text: str,
    *,
    context_file_path: str = "",
) -> dict[str, object] | None:
    compact = " ".join(str(input_text or "").split())
    lowered = compact.lower()
    inferred = _eval_context_fields(context_file_path)
    natural_scorecard_request = bool(
        re.search(
            r"\b(?:scorecard|rubric|score\s+this|score\s+the|grade\s+this|"
            r"review\s+this|human\s+score|scoring\s+template)\b",
            lowered,
        )
    )
    explicit_eval_context = bool(
        re.search(r"\b(?:eval|evaluation|promptfoo)\b", lowered) or inferred.get("case_id")
    )
    if not explicit_eval_context:
        return None
    if (
        not any(term in lowered for term in ("template", "rubric", "scorecard", "scoring"))
        and not natural_scorecard_request
    ):
        return None
    if re.search(r"\bstore|save|record|submit\b", lowered):
        return None

    try:
        from promptfoo.human_review import build_slack_review_template
    except ImportError:
        return {
            "mode": "eval_score_template",
            "status": "blocked",
            "route": "orchestrator",
            "human_summary": (
                "Promptfoo human-review helpers are not available in this environment."
            ),
            "send_enabled": False,
        }

    case_id = _extract_eval_template_field(compact, "case") or inferred.get("case_id") or ""
    run_id = _extract_eval_template_field(compact, "run") or inferred.get("run_id", "")
    agent = _extract_eval_template_field(compact, "agent") or inferred.get("agent", "")
    if not case_id:
        return {
            "mode": "eval_score_template",
            "status": "blocked",
            "route": "orchestrator",
            "case_id": "",
            "run_id": run_id,
            "agent": agent,
            "human_summary": (
                "I could not resolve the eval case from this thread. Use "
                "`@KNI can you give me a scorecard for this eval?` in the eval "
                "thread, or open the dashboard/review links from the eval footer "
                "to confirm the linked case and run IDs."
            ),
            "send_enabled": False,
        }
    template = build_slack_review_template(case_id=case_id, run_id=run_id, agent=agent)
    review_db_path = os.environ.get(
        "KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB",
        ".keystone/promptfoo/human-reviews.sqlite",
    )
    dashboard = _render_promptfoo_dashboard(Path(review_db_path))
    case_dashboard = _dashboard_case_link(dashboard, case_id)
    return {
        "mode": "eval_score_template",
        "status": "done",
        "route": "orchestrator",
        "case_id": case_id,
        "run_id": run_id,
        "agent": agent,
        "human_summary": template,
        "output": template,
        "eval_thread_reply": _eval_thread_reply_guidance(
            case_id=case_id,
            run_id=run_id,
            agent=agent,
            dashboard_case_url=case_dashboard.get("dashboard_case_url", ""),
        ),
        "slack_channel_id": "C0BA17Y9C01",
        "slack_channel_name": "evals",
        "send_enabled": False,
        **dashboard,
        **case_dashboard,
    }


def _looks_like_explicit_eval_command(input_text: str) -> bool:
    lowered = " ".join(str(input_text or "").lower().split())
    if not lowered:
        return False
    return bool(
        re.search(
            r"\b(?:eval|evaluation|promptfoo)\b.{0,40}\b(?:status|summary|show|list|doing|progress|"
            r"score|scorecard|scoring|rubric|grade|template|submit|save|record)\b"
            r"|\b(?:status|summary|show|list|doing|progress|score|scorecard|scoring|rubric|"
            r"grade|template|submit|save|record)\b.{0,40}\b(?:eval|evaluation|promptfoo)\b"
            r"|\b(?:score\s+this|score\s+the|grade\s+this|review\s+this|human\s+score|"
            r"scoring\s+template)\b",
            lowered,
        )
    )


def _looks_like_eval_human_score_reply(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:accuracy|relevance|explainability|readability|source_quality|"
            r"search_quality|synthesis|output|format|instruction_following|usefulness)"
            r"\s*(?:[:=\-]\s*)?[0-5](?:\.\d+)?\b",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )


def _eval_status_payload(
    input_text: str,
    *,
    context_file_path: str = "",
) -> dict[str, object] | None:
    compact = " ".join(str(input_text or "").split())
    lowered = compact.lower()
    inferred = _eval_context_fields(context_file_path)
    explicit_eval_status = bool(
        re.search(
            r"\b(?:eval|evaluation|promptfoo)\b.{0,40}\b(?:status|summary|show|list|doing|progress)\b"
            r"|\b(?:status|summary|show|list|doing|progress)\b.{0,40}\b(?:eval|evaluation|promptfoo)\b",
            lowered,
        )
    )
    if not explicit_eval_status:
        return None
    if not re.search(r"\b(?:status|summary|show|list|doing|progress)\b", lowered):
        return None
    case_id = _extract_eval_template_field(compact, "case") or inferred.get("case_id", "")
    if not case_id:
        return None

    try:
        from promptfoo.eval_database import DEFAULT_EVAL_DB, eval_case_status
    except ImportError:
        return {
            "mode": "eval_status",
            "status": "blocked",
            "route": "orchestrator",
            "human_summary": "Promptfoo eval database helpers are not available.",
            "send_enabled": False,
        }

    database_path = Path(os.environ.get("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB") or DEFAULT_EVAL_DB)
    status = eval_case_status(case_id, database_path=database_path)
    dashboard = _render_promptfoo_dashboard(database_path)
    case_dashboard = _dashboard_case_link(dashboard, case_id)
    promptfoo = (
        status.get("latest_promptfoo") if isinstance(status.get("latest_promptfoo"), dict) else None
    )
    human = (
        status.get("latest_target_human_review")
        if isinstance(status.get("latest_target_human_review"), dict)
        else None
    )
    promptfoo_text = "no imported Promptfoo result"
    if promptfoo:
        promptfoo_state = "pass" if promptfoo.get("success") else "fail"
        promptfoo_text = (
            f"{promptfoo_state}, score {promptfoo.get('score')}, eval {promptfoo.get('eval_id')}"
        )
    human_text = "no saved human review"
    if human:
        human_text = f"average {human.get('average_score')}/5, safety {human.get('safety')}"
    summary = (
        f"Eval `{case_id}` status: Promptfoo {promptfoo_text}; "
        f"human review {human_text}; Slack runs {status.get('slack_run_count')}."
    )
    if case_dashboard.get("dashboard_case_url"):
        summary += (
            f" Dashboard: {_slack_link(case_dashboard['dashboard_case_url'], 'case dashboard')}."
        )
    elif dashboard.get("dashboard_path"):
        summary += f" Dashboard: {dashboard['dashboard_path']}."
    review_case = _review_case_link(case_id)
    if review_case.get("review_case_url"):
        summary += (
            f" Review form: {_slack_link(review_case['review_case_url'], 'score this case')}."
        )
    latest_run = _latest_slack_eval_run(status)
    scorecard_request = "@KNI can you give me a scorecard for this eval?" if case_id else ""
    return {
        "mode": "eval_status",
        "status": "done",
        "route": "orchestrator",
        "case_id": case_id,
        "run_id": str(latest_run.get("run_id") or ""),
        "agent": str(latest_run.get("agent") or ""),
        "human_summary": summary,
        "scorecard_request": scorecard_request,
        "eval_thread_reply": _eval_thread_reply_guidance(
            case_id=case_id,
            run_id=str(latest_run.get("run_id") or ""),
            agent=str(latest_run.get("agent") or ""),
            slack_thread_ts=str(latest_run.get("slack_thread_ts") or ""),
            dashboard_case_url=case_dashboard.get("dashboard_case_url", ""),
            review_case_url=review_case.get("review_case_url", ""),
        ),
        "database_path": str(database_path),
        "eval_status": status,
        "send_enabled": False,
        **dashboard,
        **case_dashboard,
        **review_case,
    }


def _extract_eval_template_field(text: str, field: str) -> str:
    pattern = rf"\b{re.escape(field)}(?:_id)?\s*(?:[:=]\s*|\s+)([A-Za-z0-9_.:-]+)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return str(match.group(1)).strip() if match else ""


def _print_eval_score_template(payload: dict[str, object], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(str(payload.get("human_summary") or ""))
        if payload.get("dashboard_case_url"):
            print(f"Dashboard: {payload.get('dashboard_case_url')}")
    return 0


def _print_eval_score_saved(payload: dict[str, object], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(str(payload.get("human_summary") or "Eval score saved."))
        print(f"Database: {payload.get('database_path')}")
        if payload.get("dashboard_path"):
            print(f"Dashboard: {payload.get('dashboard_path')}")
        if payload.get("dashboard_case_url"):
            print(f"Case dashboard: {payload.get('dashboard_case_url')}")
        if payload.get("slack_thread_ts"):
            print(
                f"Slack thread: {payload.get('slack_channel_name')} "
                f"{payload.get('slack_thread_ts')}"
            )
    return 0


def _print_eval_status(payload: dict[str, object], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(str(payload.get("human_summary") or ""))
        print(f"Database: {payload.get('database_path')}")
        if payload.get("dashboard_path"):
            print(f"Dashboard: {payload.get('dashboard_path')}")
        if payload.get("dashboard_case_url"):
            print(f"Case dashboard: {payload.get('dashboard_case_url')}")
        if payload.get("review_case_url"):
            print(f"Human review form: {payload.get('review_case_url')}")
    return 0


def _render_promptfoo_dashboard(database_path: Path) -> dict[str, str]:
    try:
        from promptfoo.eval_dashboard import render_dashboard
    except ImportError:
        return {}
    try:
        dashboard_output = os.environ.get("KEYSTONE_PROMPTFOO_DASHBOARD_PATH", "")
        output_path = dashboard_output or str(database_path.parent / "dashboard.html")
        dashboard_path = render_dashboard(
            database_path=database_path,
            output_path=output_path,
        )
    except (OSError, ValueError) as exc:
        return {"dashboard_error": str(exc)}
    return {
        "dashboard_path": str(dashboard_path.resolve()),
        "dashboard_relative_path": str(dashboard_path),
        "dashboard_url": _promptfoo_dashboard_url(),
    }


def _promptfoo_dashboard_url() -> str:
    try:
        from promptfoo.eval_urls import eval_dashboard_url
    except ImportError:
        return os.environ.get(
            "KEYSTONE_PROMPTFOO_DASHBOARD_URL",
            "http://127.0.0.1:8769/dashboard",
        )
    return eval_dashboard_url()


def _dashboard_case_link(dashboard: dict[str, str], case_id: str) -> dict[str, str]:
    base_url = str(dashboard.get("dashboard_url") or "").strip()
    if not base_url or not case_id:
        return {}
    try:
        from promptfoo.eval_urls import eval_dashboard_case_url
    except ImportError:
        separator = "&" if "?" in base_url else "?"
        return {"dashboard_case_url": f"{base_url}{separator}case={quote(case_id)}"}
    return {"dashboard_case_url": eval_dashboard_case_url(case_id)}


def _review_case_link(case_id: str) -> dict[str, str]:
    if not case_id:
        return {}
    try:
        from promptfoo.eval_urls import eval_review_case_url
    except ImportError:
        base_url = _promptfoo_dashboard_url().replace("/dashboard", "/review")
        separator = "&" if "?" in base_url else "?"
        return {"review_case_url": f"{base_url}{separator}case={quote(case_id)}"}
    return {"review_case_url": eval_review_case_url(case_id)}


def _slack_link(url: object, label: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    return f"<{value}|{label}>"


def _latest_slack_eval_run(status: dict[str, object]) -> dict[str, object]:
    runs = status.get("slack_runs")
    if isinstance(runs, list) and runs and isinstance(runs[0], dict):
        return runs[0]
    return {}


def _eval_thread_reply_guidance(
    *,
    case_id: str,
    run_id: str = "",
    agent: str = "",
    slack_thread_ts: str = "",
    dashboard_case_url: str = "",
    review_case_url: str = "",
) -> dict[str, object]:
    guidance = {
        "scorecard_request": "@KNI can you give me a scorecard for this eval?",
        "orchestrator_judge_action": "Score with Orchestrator Judge",
        "orchestrator_judge_effect": (
            "When enabled, fills the same backend review form for the saved #evals Slack output "
            "and refreshes dashboard scoring/database/analysis."
        ),
        "submit_evaluation_action": "Submit Evaluation",
        "submit_evaluation_effect": "Writes scores and human notes to the local eval database, then refreshes dashboard views.",
        "refresh_targets": ["overview", "database", "runs_scoring", "analysis"],
        "case_id": case_id,
        "run_id": run_id,
        "slack_actions": _eval_slack_actions_for_thread(
            case_id=case_id,
            run_id=run_id,
            agent=agent,
            slack_thread_ts=slack_thread_ts,
            dashboard_case_url=dashboard_case_url,
            review_case_url=review_case_url,
        ),
    }
    if dashboard_case_url:
        guidance["dashboard_case_url"] = dashboard_case_url
    if review_case_url:
        guidance["review_case_url"] = review_case_url
    return {key: value for key, value in guidance.items() if value}


def _eval_slack_actions_for_thread(
    *,
    case_id: str,
    run_id: str = "",
    agent: str = "",
    slack_thread_ts: str = "",
    dashboard_case_url: str = "",
    review_case_url: str = "",
) -> list[dict[str, Any]]:
    case_id = " ".join(str(case_id or "").strip().split())
    run_id = " ".join(str(run_id or "").strip().split())
    agent = " ".join(str(agent or "").strip().split())
    slack_thread_ts = " ".join(str(slack_thread_ts or "").strip().split())
    dashboard_case_url = str(dashboard_case_url or "").strip()
    review_case_url = str(review_case_url or "").strip()
    if not case_id:
        return []
    eval_record = {
        "case_id": case_id,
        "run_id": run_id,
        "agent": agent,
        "slack_thread_ts": slack_thread_ts,
        "dashboard_case_url": dashboard_case_url,
        "review_case_url": review_case_url,
    }
    metadata = {"eval_record": eval_record}
    actions = [
        {
            "label": "Submit Evaluation",
            "action_id": KBA_EVAL_REVIEW,
            "intent": KBA_INTENT_EVAL_REVIEW,
            "style": "primary",
            "value": business_agent_action_value(
                intent=KBA_INTENT_EVAL_REVIEW,
                metadata=metadata,
            ),
            "metadata": metadata,
        }
    ]
    if run_id and slack_thread_ts:
        actions.append(
            {
                "label": "Score with Orchestrator Judge",
                "action_id": KBA_EVAL_ORCHESTRATOR_JUDGE,
                "intent": KBA_INTENT_EVAL_ORCHESTRATOR_JUDGE,
                "value": business_agent_action_value(
                    intent=KBA_INTENT_EVAL_ORCHESTRATOR_JUDGE,
                    metadata=metadata,
                ),
                "metadata": metadata,
            }
        )
    return actions


def _ask_live_sdk_enabled(args: argparse.Namespace, *, input_text: str = "") -> bool:
    if _request_forbids_live_sdk(input_text):
        return False
    explicit = getattr(args, "live_sdk", None)
    if explicit is not None:
        return bool(explicit)
    return cli_default_live_sdk()


def _request_forbids_live_sdk(text: str) -> bool:
    compact = " ".join(str(text or "").lower().split())
    return bool(
        re.search(
            r"\b(?:no|without|disable|do\s+not\s+(?:use|run|call))\s+"
            r"(?:live\s+sdk|model\s+calls?|openai\s+(?:api\s+)?calls?)\b"
            r"|\b(?:zero|0)\s+openai\s+(?:api\s+)?(?:calls?|requests?)\b"
            r"|\brun\s+deterministically\b",
            compact,
        )
    )


def _estimate_ask_openai_requests(
    args: argparse.Namespace,
    *,
    input_text: str,
    live_sdk: bool,
    live_manual_plan: bool,
    requested_route: str | None = None,
    manual_plan: ManualRequestPlan | None = None,
    effective_live_search: bool | None = None,
    provider_free_composition_allowed: bool = False,
) -> dict[str, Any]:
    if not live_sdk and not live_manual_plan:
        return {"min": 0, "max": 0, "stages": []}
    stages: list[str] = []
    maximum = 0
    if live_manual_plan:
        stages.append("manual_request_planner")
        maximum += 1
    if not live_sdk:
        return {"min": maximum, "max": maximum, "stages": stages}
    search_enabled = (
        bool(args.live_search) if effective_live_search is None else bool(effective_live_search)
    )
    direct_supplied_route = str(requested_route or args.agent or "").strip()
    if (
        ExecutionIntentAuthority.from_value(manual_plan).canonical
        and manual_plan is not None
        and manual_plan.target_agent in _DIRECT_SUPPLIED_RESPONSE_ROUTES
    ):
        # The same semantic owner must drive admission, cost estimation, and
        # execution. An explicit agent mention is routing advice; estimating a
        # different path can create a false budget blocker before execution.
        direct_supplied_route = manual_plan.target_agent
    bounded_provider_free_response = bool(
        _manual_plan_is_bounded_provider_free_response(
            manual_plan,
            route=direct_supplied_route,
            provider_free_composition_allowed=provider_free_composition_allowed,
        )
        or (
            manual_plan is None
            and _is_direct_supplied_response_request(
                input_text,
                requested_route=direct_supplied_route,
            )
        )
    )
    if bounded_provider_free_response:
        stages.append(f"{direct_supplied_route}_direct_supplied_response_sdk")
        maximum += 1
        return {
            "min": maximum,
            "max": maximum,
            "stages": stages,
            "note": (
                "Complete supplied-context transformations use one schema-constrained "
                "specialist request. An explicit live-planner override adds one prior "
                "planning request."
            ),
            "request_text_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest()[:12],
        }
    if _context_file_is_work_item_source_bundle(args.context_file):
        stages.append("outreach_composer_synthesis")
        maximum += 1
    elif requested_route or args.agent is not None or manual_plan is not None:
        original_route = str(
            requested_route
            or args.agent
            or (manual_plan.requested_agent if manual_plan is not None else "")
            or (manual_plan.target_agent if manual_plan is not None else "")
        )
        estimated_route = original_route
        deterministic_plan = manual_plan
        if estimated_route != "orchestrator":
            if deterministic_plan is None:
                deterministic_plan = infer_manual_request_plan(
                    input_text,
                    requested_agent=estimated_route,
                )
            estimated_route = _route_with_manual_plan_advice(
                estimated_route,
                deterministic_plan,
            )
        supplied_context_bound = bool(
            deterministic_plan is not None
            and deterministic_plan.ask_shape.prior_context_dependency == "selected_context"
            or (
                ExecutionIntentAuthority.from_value(deterministic_plan).fallback_allowed
                and looks_like_supplied_context_synthesis_request(input_text)
            )
        )
        bounded_supplied_workflow = bool(
            original_route == "chief_of_staff"
            and deterministic_plan is not None
            and len(deterministic_plan.workflow) > 1
            and supplied_context_bound
            and not deterministic_plan.requires_live_search
            and not search_enabled
            and deterministic_plan.side_effect_policy == "draft_or_read_only"
        )
        bounded_supplied_chief_response = bool(
            original_route == "chief_of_staff"
            and estimated_route == "chief_of_staff"
            and supplied_context_bound
            and deterministic_plan is not None
            and not deterministic_plan.requires_live_search
            and not search_enabled
            and deterministic_plan.side_effect_policy == "draft_or_read_only"
        )
        direct_specialist_route = estimated_route in _DIRECT_SPECIALIST_ROUTES
        delegated_chief_request = (
            original_route == "chief_of_staff"
            and direct_specialist_route
            and (
                not _manual_plan_has_multiple_provider_mutations(
                    deterministic_plan,
                    input_text=input_text,
                )
                or _is_bounded_composite_lifecycle_request(
                    estimated_route,
                    input_text=input_text,
                    manual_plan=deterministic_plan,
                )
            )
        )
        bounded_provider_request = bool(
            deterministic_plan is not None
            and deterministic_plan.provider_system != "unspecified"
            and deterministic_plan.intent
            in {"context_lookup", "business_system_write", "gmail_triage"}
            and not deterministic_plan.requires_live_search
            and not search_enabled
            and len(deterministic_plan.workflow) <= 1
            and (
                not _manual_plan_has_multiple_provider_mutations(
                    deterministic_plan,
                    input_text=input_text,
                )
                or _is_bounded_composite_lifecycle_request(
                    estimated_route,
                    input_text=input_text,
                    manual_plan=deterministic_plan,
                )
            )
            and original_route == "chief_of_staff"
            and estimated_route == "chief_of_staff"
        )
        if bounded_provider_request and deterministic_plan is not None:
            stages.append(f"{deterministic_plan.provider_system}_bounded_provider_sdk")
            # One model turn selects/calls the plan-scoped provider tool; the
            # next synthesizes the verified receipt. Provider calls themselves
            # are not OpenAI requests.
            maximum += 2
        elif bounded_supplied_workflow and deterministic_plan is not None:
            for route in deterministic_plan.workflow:
                if route in {
                    "business_research_analyst",
                    "opportunity_scout",
                }:
                    stages.append(f"{route}_source_provided_deterministic")
                    continue
                stages.append(f"{route}_sdk")
                maximum += 1
            stages.append("final_response_synthesis")
            maximum += 1
        elif (
            original_route == "chief_of_staff"
            and estimated_route == "chief_of_staff"
            and deterministic_plan is not None
            and deterministic_plan.provider_system != "unspecified"
            and not deterministic_plan.requires_live_search
            and not search_enabled
        ):
            stages.append("chief_of_staff_bounded_provider_sdk")
            maximum += 4
        elif (
            original_route == "chief_of_staff"
            and estimated_route == "chief_of_staff"
            and (
                bounded_supplied_chief_response
                or (
                    ExecutionIntentAuthority.from_value(deterministic_plan).fallback_allowed
                    and is_bounded_chief_response_only_request(input_text)
                )
            )
        ):
            stages.append("chief_of_staff_response_only_sdk")
            maximum += 1
        elif (
            original_route != "orchestrator"
            and direct_specialist_route
            and (original_route != "chief_of_staff" or delegated_chief_request)
        ):
            if estimated_route == "gmail_triage" and _is_bounded_composite_lifecycle_request(
                estimated_route,
                input_text=input_text,
                manual_plan=deterministic_plan,
            ):
                stages.append("gmail_test_draft_lifecycle_provider")
            else:
                stages.append(f"{estimated_route}_direct_sdk")
                maximum += _direct_specialist_request_estimate(
                    estimated_route,
                    input_text=input_text,
                    live_search=search_enabled,
                    manual_plan=deterministic_plan,
                )
        else:
            stages.append(f"{estimated_route}_sdk")
            maximum += resolve_sdk_turn_policy(
                estimated_route,
                request_text=input_text,
                live_search=search_enabled,
            ).max_turns
    else:
        manager_steps = max(1, int(args.max_manager_steps or 1))
        if _is_bounded_gmail_recommendation_graph(
            input_text,
            manager_steps=manager_steps,
        ):
            stages.append("outreach_composer_sdk")
            maximum += 1
        elif _is_bounded_gmail_research_reply_graph(
            input_text,
            manager_steps=manager_steps,
        ):
            stages.extend(
                (
                    "gmail_provider_read",
                    "business_research_sdk",
                    "outreach_composer_sdk",
                    "final_response_synthesis",
                )
            )
            maximum += 7
        elif _is_bounded_gmail_research_summary_graph(
            input_text,
            manager_steps=manager_steps,
        ):
            stages.extend(
                (
                    "gmail_provider_read",
                    "business_research_sdk",
                    "final_response_synthesis",
                )
            )
            maximum += 7
        else:
            stages.extend(
                f"manager_specialist_step_{index}" for index in range(1, manager_steps + 1)
            )
            stages.append("final_response_synthesis")
            maximum += manager_steps * 6 + 1
    constraint_plan = manual_plan or infer_manual_request_plan(
        input_text,
        requested_agent=str(requested_route or args.agent or "orchestrator"),
    )
    constraints = output_constraints_from_plan(constraint_plan)
    if constraints.has_deterministic_requirements():
        stages.append("conditional_instruction_following_repair")
        maximum += 1
    direct_specialist = any(stage.endswith("_direct_sdk") for stage in stages)
    mandatory_stage_count = sum(
        stage != "conditional_instruction_following_repair"
        and not stage.endswith("_provider")
        and not stage.endswith("_deterministic")
        for stage in stages
    )
    return {
        "min": mandatory_stage_count,
        "max": maximum,
        "stages": stages,
        "note": (
            "Direct specialist estimate counts model turns only; bounded provider tool "
            "calls are not OpenAI requests, and provider retries are a stop event."
            if direct_specialist
            else "Maximum uses configured SDK turn limits; provider retries are a stop event."
        ),
        "request_text_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest()[:12],
    }


_CONTINUATION_PROVIDER_SYSTEMS = {
    "calendar": "google_calendar",
    "google_calendar": "google_calendar",
    "gmail": "gmail",
    "airtable": "airtable",
    "google_workspace": "google_workspace",
    "zotero": "zotero",
    "slack": "slack",
}


def _apply_continuation_provider_affinity(
    preflight: OrchestratorPreflight,
    provider_affinity: str,
) -> OrchestratorPreflight:
    """Preserve typed provider continuity without interpreting request phrases.

    Slack supplies this affinity from the prior provider-owned thread. It may
    fill an unspecified provider on a semantically compatible plan, but it
    cannot override a provider chosen by the planner, change the task intent,
    grant write approval, or force a specialist owner.
    """

    plan = preflight.manual_request_plan
    provider = _CONTINUATION_PROVIDER_SYSTEMS.get(str(provider_affinity or "").strip().lower())
    if (
        not provider
        or plan.provider_system != "unspecified"
        or plan.intent not in {"context_lookup", "business_system_write", "gmail_triage"}
    ):
        return preflight
    return preflight.model_copy(
        update={"manual_request_plan": plan.model_copy(update={"provider_system": provider})}
    )


def _is_bounded_gmail_research_reply_graph(text: str, *, manager_steps: int) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    bounded_thread_context = bool(
        re.search(
            r"\b(?:using|use)\s+only\b.{0,140}\b(?:thread|email|gmail)\b"
            r"|\b(?:thread|email|gmail)\b.{0,140}\b(?:using|use)\s+only\b",
            normalized,
        )
    )
    return bool(
        manager_steps == 3
        and re.search(r"\b(?:gmail|email|thread)\b", normalized)
        and re.search(r"\b(?:read|find|review|latest|recent)\b", normalized)
        and re.search(
            r"\bresearch\b|\b(?:recommend|identify)\b.{0,100}\b(?:collaborat|next step)",
            normalized,
        )
        and re.search(r"\b(?:reply|response|draft)\b", normalized)
        and bounded_thread_context
    )


def _is_bounded_gmail_recommendation_graph(text: str, *, manager_steps: int) -> bool:
    """Recognize the read-only ANU-61 thread-to-next-step workflow."""

    normalized = " ".join(str(text or "").lower().split())
    return bool(
        manager_steps == 3
        and re.search(r"\b(?:gmail|email)\s+thread\b", normalized)
        and re.search(r"\b(?:read|review|latest|recent)\b", normalized)
        and re.search(r"\b(?:original inquiry|all messages|complete thread)\b", normalized)
        and re.search(r"\b(?:current conversation state|current state)\b", normalized)
        and re.search(r"\b(?:recommend|identify)\b.{0,120}\bnext step\b", normalized)
        and re.search(r"\breply\b.{0,80}\bonly if\b", normalized)
        and re.search(r"\breturn\b.{0,100}\bhere\b.{0,40}\breview\b", normalized)
        and re.search(r"\b(?:using|use)\s+only\b", normalized)
    )


def _is_bounded_gmail_research_summary_graph(text: str, *, manager_steps: int) -> bool:
    """Recognize read->research->summary asks with an explicit no-write boundary."""

    normalized = " ".join(str(text or "").lower().split())
    research_output = bool(
        re.search(r"\bresearch\b", normalized)
        and re.search(
            r"\b(?:summarize|summary|determine|understand|explain|identify)\b",
            normalized,
        )
        and re.search(
            r"\b(?:public sources?|source links?|current sources?|visible sources?)\b",
            normalized,
        )
    )
    no_outreach = bool(
        re.search(
            r"\b(?:do not|don't|dont|without|no)\b[^.;\n]{0,180}"
            r"\b(?:draft|reply|response|send|gmail draft|post|schedule|share|write)\b",
            normalized,
        )
    )
    return bool(
        manager_steps == 3
        and re.search(r"\b(?:gmail|email|thread)\b", normalized)
        and re.search(r"\b(?:read|find|review|latest|recent)\b", normalized)
        and research_output
        and no_outreach
    )


def _print_ask_request_budget_blocked(
    *,
    json_output: bool,
    requested_limit: int,
    estimate: dict[str, Any],
    openai_requests_made: int = 0,
) -> int:
    requests_made = max(0, int(openai_requests_made))
    if requests_made:
        message = (
            f"The live command used {requests_made} bounded planning request"
            f"{'' if requests_made == 1 else 's'}, then stopped because the resolved "
            "execution path would exceed the declared per-command ceiling. No "
            "specialist or provider action ran."
        )
    else:
        message = (
            "The live command stopped before any model or provider action because "
            "its estimated request count exceeds the declared per-command ceiling."
        )
    payload = {
        "status": "blocked",
        "block_kind": "openai_request_budget_exceeded",
        "message": message,
        "requested_limit": requested_limit,
        "estimated_requests": estimate,
        "openai_requests_made": requests_made,
        "send_enabled": False,
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(payload["message"])
        print(
            f"Estimated requests: {estimate.get('min')}–{estimate.get('max')}; "
            f"declared ceiling: {requested_limit}."
        )
    return 2


_DIRECT_SUPPLIED_RESPONSE_ROUTES = frozenset(
    {
        "chief_of_staff",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "gmail_triage",
    }
)


def _is_direct_supplied_response_request(
    input_text: str,
    *,
    requested_route: str | None,
) -> bool:
    """Identify a complete, provider-free transformation owned by one named agent."""

    route = str(requested_route or "").strip()
    if route not in _DIRECT_SUPPLIED_RESPONSE_ROUTES:
        return False
    if not looks_like_supplied_context_synthesis_request(input_text):
        return False
    if looks_like_stateful_work_request(input_text):
        return False
    plan = infer_manual_request_plan(input_text, requested_agent=route)
    return bool(
        plan.target_agent == route
        and not plan.workflow
        and not plan.requires_durable_state
        and not plan.requires_live_search
        and not plan.requires_approved_context
        and plan.provider_system == "unspecified"
        and not plan.provider_operations
        and plan.side_effect_policy == "draft_or_read_only"
        and plan.intent
        not in {
            "blocked_send",
            "business_system_write",
            "clarification",
            "continue_work_item",
        }
    )


def _manual_plan_is_bounded_provider_free_response(
    manual_plan: ManualRequestPlan | None,
    *,
    route: str,
    provider_free_composition_allowed: bool = False,
) -> bool:
    """Classify post-plan direct answers without inspecting request wording."""

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if not authority.canonical:
        return False
    assert authority.plan is not None
    manual_plan = authority.plan
    provider_context_is_already_supplied = bool(
        manual_plan.provider_operations
        and set(manual_plan.provider_operations) <= {"read"}
        and manual_plan.ask_shape.source_type_preference
        and all(
            re.search(
                r"\b(?:attached|local|file|image|pdf|document)\b",
                str(source_type or ""),
                re.IGNORECASE,
            )
            for source_type in manual_plan.ask_shape.source_type_preference
        )
    )
    return bool(
        manual_plan.target_agent == route
        and not manual_plan.workflow
        and not manual_plan.requires_durable_state
        and not manual_plan.requires_live_search
        and (
            not manual_plan.requires_approved_context
            or is_internal_slack_composition_plan(manual_plan)
            or (
                provider_free_composition_allowed
                and is_provider_free_selected_context_draft_plan(manual_plan)
            )
        )
        and (
            (manual_plan.provider_system == "unspecified" and not manual_plan.provider_operations)
            or provider_context_is_already_supplied
        )
        and manual_plan.side_effect_policy == "draft_or_read_only"
        and manual_plan.intent
        not in {
            "blocked_send",
            "business_system_write",
            "clarification",
            "continue_work_item",
        }
    )


def _should_run_direct_supplied_response(
    input_text: str,
    *,
    requested_route: str,
    manual_plan: ManualRequestPlan | None,
    provider_free_composition_allowed: bool = False,
) -> bool:
    """Let a live semantic plan decide whether tools/providers are unnecessary."""

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical and manual_plan is not None:
        return _manual_plan_is_bounded_provider_free_response(
            manual_plan,
            route=requested_route,
            provider_free_composition_allowed=provider_free_composition_allowed,
        )
    if authority.invalid:
        return False
    return _is_direct_supplied_response_request(
        input_text,
        requested_route=requested_route,
    )


_DIRECT_SPECIALIST_ROUTES = frozenset(
    {
        "business_research_analyst",
        "opportunity_scout",
        "gmail_triage",
        "outreach_composer",
        *CONTEXT_AGENT_ROUTES,
    }
)


def _direct_specialist_request_estimate(
    route: str,
    *,
    input_text: str,
    live_search: bool,
    manual_plan: ManualRequestPlan | None = None,
) -> int:
    """Estimate one direct specialist without charging for a manager graph."""

    profile = _direct_specialist_runtime_profile(
        route,
        input_text=input_text,
        manual_plan=manual_plan,
    )
    plan = profile["manual_plan"]
    normalized = str(profile["normalized_request"])
    if profile["compact_instructions"]:
        if route == "opportunity_scout" and not live_search_allowed_for_execution(
            True,
            manual_plan=manual_plan,
            request_text=input_text,
        ):
            # The compact supplied-evidence adapter performs one tool-free synthesis.
            return 1
        authority = ExecutionIntentAuthority.from_value(manual_plan)
        if (
            route == "zotero_context_agent"
            and (
                (
                    authority.canonical
                    and authority.plan is not None
                    and authority.plan.provider_system == "zotero"
                    and authority.plan.provider_selection_order == "latest"
                    and "abstract" in authority.plan.zotero_requested_fields
                )
                or (
                    authority.fallback_allowed
                    and "abstract" in normalized
                    and re.search(
                        r"\b(?:latest|most\s+recent(?:ly)?\s+added)\b",
                        normalized,
                    )
                )
            )
        ):
            # The ordered provider read is acquired before the specialist call.
            return 1
        if _is_bounded_composite_lifecycle_request(
            route,
            input_text=input_text,
            manual_plan=plan,
        ):
            # One guarded provider helper owns the complete marked lifecycle;
            # reserve one model turn for the call and one for synthesis.
            return 2
        if plan.intent == "business_system_write":
            if (
                route == "airtable_context_agent"
                and resolve_finance_expense_receipt_target(
                    input_text,
                    manual_plan=plan,
                )
                is not None
            ):
                # The composite receipt tool performs schema acquisition,
                # field mapping, create, attachment, and read-back in one tool
                # call. Reserve one model turn for the call and one for synthesis.
                return 2
            # Allow a separate target/schema read, mutation, and final synthesis.
            return 3
        if route == "google_workspace_context_agent":
            # A bounded provider-backed read may need one model turn to resolve
            # an exact target, one to read it, and one to synthesize the answer.
            # This is a ceiling rather than a required number of calls, so exact
            # reads that finish earlier keep their fast path.
            return 3
        # One model request may select a bounded read tool; the second synthesizes
        # its result. Provider calls do not count as OpenAI requests.
        return 2
    policy_max = resolve_sdk_turn_policy(
        route,
        request_text=input_text,
        live_search=live_search,
    ).max_turns
    return min(policy_max, 4)


def _direct_specialist_runtime_profile(
    route: str,
    *,
    input_text: str,
    manual_plan: ManualRequestPlan | None = None,
) -> dict[str, Any]:
    """Resolve prompt/tool depth from ask shape after the owning route is known."""

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    semantic_authority = authority.canonical
    plan = manual_plan or infer_manual_request_plan(input_text, requested_agent=route)
    normalized = " ".join(str(input_text or "").lower().split())
    deep_request = bool(
        plan.ask_shape.evidence_depth == "deep"
        or (
            not semantic_authority
            and re.search(
                r"\b(?:deep|comprehensive|exhaustive|multi-stage|full landscape|"
                r"all available sources|systematic review)\b",
                normalized,
            )
        )
    )
    bounded_composite_lifecycle = _is_bounded_composite_lifecycle_request(
        route,
        input_text=input_text,
        manual_plan=plan,
    )
    multi_operation = bool(
        (
            len(
                {
                    operation
                    for operation in authority.effective_provider_operations(
                        plan.provider_system
                    )
                    if operation in {"create", "update", "delete", "attach"}
                }
            )
            > 1
            if semantic_authority
            else _is_multi_operation_business_system_request(input_text)
        )
        and not bounded_composite_lifecycle
    )
    thread_followup = bool(
        plan.ask_shape.prior_context_dependency not in {"", "unspecified"}
        or (
            not semantic_authority
            and re.search(
                r"\b(?:same|this|that|previous|prior|current)\b.{0,80}"
                r"\b(?:article|record|event|thread|email|document|file|item)\b",
                normalized,
            )
        )
    )
    compact_item_limit = (
        5
        if route == "business_research_analyst"
        and plan.ask_shape.output_form == "bullets"
        else 3
    )
    compact = bool(
        plan.desired_count <= compact_item_limit
        and plan.ask_shape.ask_breadth != "broad"
        and not deep_request
        and not multi_operation
    )
    if not compact:
        request_class = "deep_or_multistage"
    elif bounded_composite_lifecycle or plan.intent == "business_system_write":
        request_class = "bounded_write"
    elif thread_followup:
        request_class = "thread_followup"
    else:
        request_class = "bounded_read"
    return {
        "route": route,
        "request_class": request_class,
        "compact_instructions": compact,
        "manual_plan": plan,
        "normalized_request": normalized,
        "deep_request": deep_request,
        "multi_operation": multi_operation,
        "bounded_composite_lifecycle": bounded_composite_lifecycle,
        "thread_followup": thread_followup,
    }


def _append_compact_direct_flag(
    command: list[str],
    *,
    route: str,
    input_text: str,
    manual_plan: ManualRequestPlan | None = None,
) -> None:
    """Select the compact child profile only for a bounded direct ask."""

    profile = _direct_specialist_runtime_profile(
        route,
        input_text=input_text,
        manual_plan=manual_plan,
    )
    if profile["compact_instructions"]:
        command.append("--compact-instructions")


def _is_multi_operation_business_system_request(text: str) -> bool:
    """Keep create/update/delete lifecycles out of the low-latency single-action lane."""

    normalized = " ".join(positive_capability_text(text).lower().split())
    operation_families = (
        bool(re.search(r"\b(?:add|create|insert)\b", normalized)),
        bool(re.search(r"\b(?:update|change|modify|revise|edit|set)\b", normalized)),
        bool(re.search(r"\b(?:delete|remove)\b", normalized)),
    )
    return sum(operation_families) > 1


def _manual_plan_has_multiple_provider_mutations(
    manual_plan: ManualRequestPlan | None,
    *,
    input_text: str,
) -> bool:
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical:
        assert authority.plan is not None
        return (
            len(
                {
                    operation
                    for operation in authority.effective_provider_operations(
                        authority.plan.provider_system
                    )
                    if operation in {"create", "update", "delete", "attach"}
                }
            )
            > 1
        )
    if authority.invalid:
        return False
    return _is_multi_operation_business_system_request(input_text)


def _is_marked_provider_lifecycle_planning_candidate(input_text: str) -> bool:
    """Allow one semantic-planning turn before choosing a guarded test helper.

    This is an admission hint only. The interpreted plan must still select the
    provider owner and satisfy the exact typed lifecycle contract before any
    provider action can run.
    """

    normalized = " ".join(str(input_text or "").lower().split())
    return bool(
        re.search(
            r"\bkba_test_(?:record|doc|draft)(?:_[a-z0-9]+)*\b",
            normalized,
        )
    )


def _is_bounded_composite_lifecycle_request(
    route: str,
    *,
    input_text: str,
    manual_plan: ManualRequestPlan | None = None,
) -> bool:
    """Recognize one guarded lifecycle without re-parsing an LLM plan.

    Exact KBA_TEST markers remain deterministic object-identity gates. When a
    live semantic plan is available, its structured provider operations decide
    whether the lifecycle helper applies. Phrase recognition is retained only
    for dry-run and planner-unavailable compatibility.
    """

    normalized = " ".join(str(input_text or "").lower().split())
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical:
        assert authority.plan is not None
        plan = authority.plan
        provider_by_route = {
            "airtable_context_agent": "airtable",
            "google_workspace_context_agent": "google_workspace",
            "gmail_triage": "gmail",
            "zotero_context_agent": "zotero",
        }
        required_operations = {
            "airtable_context_agent": {"create", "update", "delete"},
            "google_workspace_context_agent": {"create", "delete"},
            "gmail_triage": {"create", "update", "delete"},
            "zotero_context_agent": {"create", "update", "delete"},
        }
        marker_by_route = {
            "airtable_context_agent": r"\bkba_test_record(?:_[a-z0-9]+)*\b",
            "google_workspace_context_agent": r"\bkba_test_doc(?:_[a-z0-9]+)*\b",
            "gmail_triage": r"\bkba_test_draft(?:_[a-z0-9]+)*\b",
            "zotero_context_agent": r"\bkba_test_note(?:_[a-z0-9]+)*\b",
        }
        required = required_operations.get(route)
        marker = marker_by_route.get(route)
        return bool(
            required
            and marker
            and plan.target_agent == route
            and plan.intent == "business_system_write"
            and plan.provider_system == provider_by_route.get(route)
            and required.issubset(
                set(authority.effective_provider_operations(plan.provider_system))
            )
            and re.search(marker, normalized)
        )
    if authority.invalid:
        return False
    # Body copy is data, not an operation request. Excluding it prevents words
    # such as "set" or "update" inside a quoted test body from changing the
    # selected lifecycle.
    operation_text = re.sub(
        r"\bbody\s*[\"“][^\"”]*[\"”]",
        "body",
        normalized,
    )
    creates = bool(re.search(r"\b(?:add|create|make|write|insert)\b", operation_text))
    updates = bool(re.search(r"\b(?:update|change|modify|revise|edit)\b", operation_text))
    removes = bool(re.search(r"\b(?:delete|remove|trash|clean\s*up)\b", operation_text))
    if route == "airtable_context_agent":
        return bool(
            re.search(r"\bkba_test_record(?:_[a-z0-9]+)*\b", normalized)
            and creates
            and updates
            and removes
        )
    if route == "zotero_context_agent":
        return bool(
            re.search(r"\bkba_test_note(?:_[a-z0-9]+)*\b", normalized)
            and re.search(r"\bnotes?\b", normalized)
            and creates
            and updates
            and removes
        )
    if route == "google_workspace_context_agent":
        return bool(
            re.search(r"\bkba_test_doc(?:_[a-z0-9]+)*\b", normalized)
            and re.search(r"\b(?:google\s+docs?|documents?)\b", normalized)
            and creates
            and removes
            and not updates
        )
    if route == "gmail_triage":
        return bool(
            re.search(r"\bkba_test_draft(?:_[a-z0-9]+)*\b", normalized)
            and re.search(r"\b(?:gmail\s+)?drafts?\b", normalized)
            and creates
            and updates
            and removes
        )
    return False


def _run_bounded_provider_lifecycle_after_preflight(
    route: str,
    input_text: str,
    *,
    lifecycle_scope_text: str = "",
    context_text: str,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    database_url: str | None,
) -> int | None:
    """Bind an interpreted marked lifecycle to its owning typed provider helper."""

    if not _is_bounded_composite_lifecycle_request(
        route,
        input_text=lifecycle_scope_text or input_text,
        manual_plan=manual_plan,
    ):
        return None
    if route == "airtable_context_agent":
        return _run_direct_airtable_test_record_lifecycle(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            database_url=database_url,
        )
    if route == "google_workspace_context_agent":
        return _run_direct_google_doc_test_lifecycle(
            input_text,
            context_text=context_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            database_url=database_url,
        )
    if route == "gmail_triage":
        return _run_direct_gmail_test_draft_lifecycle(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            database_url=database_url,
        )
    return None


def _interpreted_lifecycle_scope_text(
    input_text: str,
    manual_plan: ManualRequestPlan | None,
) -> str:
    """Return object-identity text; structured LLM fields carry operations."""

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical or authority.invalid:
        return str(input_text or "")
    parts = [
        str(input_text or ""),
        str(manual_plan.objective or ""),
        *[str(item or "") for item in manual_plan.constraints],
    ]
    return "\n".join(part.strip() for part in parts if part.strip())


def _sdk_session_spec_for_ask(
    args: argparse.Namespace,
    *,
    route: str,
    default_enabled: bool,
    context_file_path: str = "",
) -> SDKSessionSpec:
    context_scope = context_file_session_components(context_file_path)
    history_limit = getattr(args, "sdk_session_history_limit", None)
    if route in {*CONTEXT_AGENT_ROUTES, "chief_of_staff", "gmail_triage"} and context_scope is None:
        default_enabled = False
    if context_scope is not None:
        scope, components = context_scope
        if history_limit is None:
            # The typed Slack context already carries the root request and recent
            # thread turns. Keep only a small continuity tail so stale bot output
            # cannot dominate the authoritative operator context or token budget.
            history_limit = 6
    else:
        scope = "ask"
        components = default_cli_ask_session_components(route)
    return resolve_sdk_session_spec(
        scope=scope,
        components=components,
        enabled=getattr(args, "sdk_session", None),
        explicit_session_id=str(getattr(args, "sdk_session_id", "") or ""),
        database_path=str(getattr(args, "sdk_session_db", "") or ""),
        history_limit=history_limit,
        default_enabled=default_enabled,
    )


def _ask_route_session_default(route: str) -> bool:
    return route in {*CONTEXT_AGENT_ROUTES, "chief_of_staff", "gmail_triage"}


def _preflight_blocks_execution(preflight: OrchestratorPreflight) -> bool:
    return not bool(preflight.execution_allowed)


def _bounded_direct_route_from_preflight(
    preflight: OrchestratorPreflight,
) -> str | None:
    """Select direct execution only for one-owner work after semantic preflight."""

    plan = preflight.manual_request_plan
    if plan.intent in {"continue_work_item", "opportunity_to_outreach_loop"}:
        return None
    if len(_preflight_workflow_routes(preflight)) > 1:
        return None
    route = str(plan.target_agent or preflight.route_result.route or "").strip()
    if route in {*_DIRECT_SPECIALIST_ROUTES, "chief_of_staff"}:
        return route
    return None


def _preflight_workflow_routes(preflight: OrchestratorPreflight) -> list[str]:
    """Return the validated ordered owners produced by semantic preflight."""

    plan_routes = list(getattr(preflight.manual_request_plan, "workflow", []) or [])
    route_result_routes = list(preflight.route_result.workflow or [])
    return list(
        dict.fromkeys(
            str(route)
            for route in [*plan_routes, *route_result_routes]
            if str(route) in AGENT_REGISTRY and str(route) not in {"orchestrator", "clarification"}
        )
    )


def _preflight_requires_work_item(
    preflight: OrchestratorPreflight,
    *,
    request_text: str = "",
) -> bool:
    """Use durable state only when the semantic plan or workflow requires it."""

    del request_text
    return bool(
        len(_preflight_workflow_routes(preflight)) > 1
        or preflight.manual_request_plan.requires_durable_state
    )


def _preflight_work_item_entry_route(
    preflight: OrchestratorPreflight,
) -> str | None:
    """Choose the WorkItem entry owner without turning advisors into managers."""

    plan = preflight.manual_request_plan
    workflow_routes = _preflight_workflow_routes(preflight)
    if (
        plan.target_agent == "chief_of_staff"
        and workflow_routes
        and (
            all(route in CONTEXT_AGENT_ROUTES for route in workflow_routes)
            or (
                plan.intent == "context_lookup"
                and plan.task_objective == "context_lookup"
                and plan.expected_artifact_type == "context_summary"
                and all(
                    route in {*CONTEXT_AGENT_ROUTES, "gmail_triage"}
                    for route in workflow_routes
                )
            )
        )
    ):
        return "chief_of_staff"
    if workflow_routes:
        return workflow_routes[0]
    return (
        str(plan.target_agent or preflight.route_result.route or "").strip()
        or None
    )


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
    return bool(
        explicit_route in {None, "orchestrator"}
        and not manual_plan.requires_durable_state
        and len(manual_plan.workflow) <= 1
    )


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


def _orchestrator_workflow_state_from_cli_context(
    *,
    context_file_path: str = "",
    request_text: str = "",
    database_url: str | None = None,
    work_item: WorkItem | None = None,
) -> dict[str, Any]:
    """Build bounded preflight context before the WorkItem runner mutates state."""

    state: dict[str, Any] = {}
    state_control_requested = slack_work_item_control_requested(request_text)
    if work_item is not None:
        if (
            work_item.status
            in {
                WorkItemStatus.NEEDS_CONTEXT,
                WorkItemStatus.BLOCKED,
                WorkItemStatus.ARCHIVED,
            }
            and not state_control_requested
        ):
            state["historical_work_item_facts"] = _planner_historical_work_item_facts(work_item)
        else:
            state["current_work_item"] = _planner_current_work_item_context(work_item)
        metadata = work_item.target.metadata if isinstance(work_item.target.metadata, dict) else {}
        slack_context = metadata.get("slack_context")
        if isinstance(slack_context, dict) and slack_context:
            state["slack_context"] = slack_context
            metadata_messages = slack_context.get("thread_messages")
            if isinstance(metadata_messages, list):
                sanitized_messages = _slack_history_messages_for_planner(
                    metadata_messages,
                    allow_failed_agent_context=state_control_requested,
                )
                if sanitized_messages:
                    state["recent_slack_thread"] = sanitized_messages[-8:]
                    state["slack_thread_transcript"] = _render_slack_history_messages(
                        sanitized_messages[-8:]
                    )
            else:
                metadata_transcript = str(slack_context.get("thread_transcript") or "").strip()
                if metadata_transcript and (
                    state_control_requested
                    or not _slack_history_text_is_non_authoritative(metadata_transcript)
                ):
                    state["slack_thread_transcript"] = metadata_transcript[:6000]
            metadata_prior_runs = slack_context.get("prior_agent_runs")
            if isinstance(metadata_prior_runs, list):
                admitted_prior_runs = _slack_prior_runs_for_planner(
                    metadata_prior_runs,
                    allow_failed_context=state_control_requested,
                )
                if admitted_prior_runs:
                    state["prior_agent_runs"] = admitted_prior_runs[-5:]

    if not _context_file_is_slack_context(context_file_path):
        return _attach_verified_provider_scope_from_slack_thread(
            state,
            database_url=database_url,
        )
    try:
        payload = json.loads(Path(context_file_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return state
    if not isinstance(payload, dict):
        return state

    schema = str(payload.get("schema") or payload.get("schema_") or "").strip()
    if schema == "keystone.slack.selected_message_context.v1":
        from keystone_agents.slack_actions import (
            SlackSelectedMessageContext,
            orchestrator_workflow_state_from_slack_context,
        )

        selected_context = SlackSelectedMessageContext.model_validate(payload)
        slack_state = orchestrator_workflow_state_from_slack_context(
            selected_context,
            request_text=request_text,
            database_url=database_url,
        )
        return _attach_verified_provider_scope_from_slack_thread(
            _merge_direct_workflow_state(slack_state, state),
            database_url=database_url,
        )

    slack_context = {
        key: payload.get(key)
        for key in (
            "channel_id",
            "channel_name",
            "selected_message_ts",
            "thread_ts",
            "request_ts",
            "thread_fetch_status",
            "permalink",
        )
        if payload.get(key)
    }
    if slack_context:
        state["slack_context"] = slack_context
    thread_root = str(payload.get("thread_root_request") or "").strip()
    if thread_root:
        state["slack_thread_root"] = thread_root[:2400]
    transcript = str(
        payload.get("slack_thread_transcript")
        or payload.get("thread_transcript")
        or payload.get("read_context")
        or ""
    ).strip()
    thread_messages = payload.get("thread_messages")
    if isinstance(thread_messages, list):
        sanitized_messages = _slack_history_messages_for_planner(
            thread_messages,
            allow_failed_agent_context=state_control_requested,
        )
        if sanitized_messages:
            state["recent_slack_thread"] = sanitized_messages[-8:]
            state["slack_thread_transcript"] = _render_slack_history_messages(
                sanitized_messages[-8:]
            )
    elif transcript and (
        state_control_requested or not _slack_history_text_is_non_authoritative(transcript)
    ):
        state["slack_thread_transcript"] = transcript[:6000]
    prior_runs = payload.get("prior_agent_runs")
    if isinstance(prior_runs, list):
        admitted_prior_runs = _slack_prior_runs_for_planner(
            prior_runs,
            allow_failed_context=state_control_requested,
        )
        if admitted_prior_runs:
            state["prior_agent_runs"] = admitted_prior_runs[-5:]
    return _attach_verified_provider_scope_from_slack_thread(
        state,
        database_url=database_url,
    )


def _attach_verified_provider_scope_from_slack_thread(
    state: dict[str, Any],
    *,
    database_url: str | None,
) -> dict[str, Any]:
    """Attach verified provider identity for an ordinary Slack thread follow-up."""

    if isinstance(state.get("prior_provider_result_scope"), Mapping):
        return state
    slack_scope = state.get("slack_context")
    if not isinstance(slack_scope, Mapping):
        return state
    channel_id = str(slack_scope.get("channel_id") or "").strip()
    thread_ts = str(slack_scope.get("thread_ts") or "").strip()
    if not channel_id or not thread_ts:
        return state
    from keystone_agents.slack_actions import (
        latest_verified_provider_result_scope_for_slack_thread,
    )

    scope = latest_verified_provider_result_scope_for_slack_thread(
        channel_id=channel_id,
        thread_ts=thread_ts,
        database_url=database_url,
    )
    if scope is None:
        return state
    return {
        **state,
        "prior_provider_result_scope": scope.model_dump(mode="json"),
    }


def _slack_history_messages_for_planner(
    messages: list[Any],
    *,
    allow_failed_agent_context: bool,
) -> list[dict[str, Any]]:
    """Keep human thread facts while excluding failed agent prose as authority."""

    admitted: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if (
            role == "agent"
            and not allow_failed_agent_context
            and _slack_history_message_is_non_authoritative(message)
        ):
            continue
        admitted.append(dict(message))
    return admitted


def _slack_history_message_is_non_authoritative(message: Mapping[str, Any]) -> bool:
    status = " ".join(str(message.get("status") or "").lower().split())
    if status in {"blocked", "failed", "needs_context", "needs_input", "running"}:
        return True
    return _slack_history_text_is_non_authoritative(str(message.get("text") or ""))


def _slack_history_text_is_non_authoritative(text: str) -> bool:
    heading = " ".join(str(text or "").lower().split())[:220]
    return any(
        marker in heading
        for marker in (
            "business agents completion not confirmed",
            "business agents need input",
            "business agents blocked",
            "business agents workitem failed",
            "business agents named agent failed",
            "business agents run running",
        )
    )


def _render_slack_history_messages(messages: list[dict[str, Any]]) -> str:
    lines = ["Slack thread history (failed agent attempts omitted):"]
    for message in messages:
        role = str(message.get("role") or "context").strip().lower()
        text = _bounded_redacted_text(message.get("text"), max_chars=1200)
        if text:
            lines.append(f"- {role}: {text}")
    return "\n".join(lines)[:6000]


def _slack_prior_runs_for_planner(
    prior_runs: list[Any],
    *,
    allow_failed_context: bool,
) -> list[dict[str, Any]]:
    admitted: list[dict[str, Any]] = []
    for run in prior_runs:
        if not isinstance(run, dict):
            continue
        status = str(run.get("status") or "completed").strip().lower()
        completed = status in {"completed", "done", "recovered", "success"}
        if not completed and not allow_failed_context:
            continue
        compact = dict(run)
        if completed:
            compact["status"] = "completed"
        compact["thread_correlation"] = "same_thread"
        admitted.append(compact)
    return admitted


def _planner_current_work_item_context(work_item: WorkItem) -> dict[str, Any]:
    """Expose exact bounded WorkItem identity for same-object planner follow-ups."""

    target = {
        key: value
        for key, value in {
            "name": _bounded_redacted_text(work_item.target.name, max_chars=240),
            "object_type": _bounded_redacted_text(
                work_item.target.object_type,
                max_chars=100,
            ),
            "external_id": _bounded_redacted_text(
                work_item.target.external_id,
                max_chars=180,
            ),
        }.items()
        if value
    }
    selected_artifacts = [
        {
            key: value
            for key, value in {
                "artifact_type": _bounded_redacted_text(
                    artifact.artifact_type,
                    max_chars=100,
                ),
                "artifact_id": _bounded_redacted_text(
                    artifact.artifact_id,
                    max_chars=180,
                ),
                "source_agent": _bounded_redacted_text(
                    artifact.source_agent,
                    max_chars=100,
                ),
                "title": _bounded_redacted_text(artifact.title, max_chars=240),
                "summary": _bounded_redacted_text(artifact.summary, max_chars=480),
            }.items()
            if value
        }
        for artifact in work_item.artifact_refs
        if artifact.selected
    ][-3:]
    next_action = (
        {
            key: value
            for key, value in {
                "action": _bounded_redacted_text(
                    work_item.next_action.action,
                    max_chars=100,
                ),
                "agent": (
                    work_item.next_action.agent.value
                    if work_item.next_action.agent is not None
                    else ""
                ),
                "description": _bounded_redacted_text(
                    work_item.next_action.description,
                    max_chars=480,
                ),
            }.items()
            if value
        }
        if work_item.next_action is not None
        else {}
    )
    return {
        key: value
        for key, value in {
            "id": _bounded_redacted_text(work_item.id, max_chars=100),
            "kind": work_item.kind.value,
            "status": work_item.status.value,
            "route": work_item.current_route.value,
            "title": _bounded_redacted_text(work_item.title, max_chars=240),
            "prior_request": _bounded_redacted_text(
                work_item.request_text,
                max_chars=1200,
                keep_tail=True,
            ),
            "target": target,
            "selected_artifacts": selected_artifacts,
            "next_action": next_action,
        }.items()
        if value not in ("", {}, [])
    }


def _planner_historical_work_item_facts(work_item: WorkItem) -> dict[str, Any]:
    """Carry bounded facts without granting a blocked WorkItem execution authority."""

    context = _planner_current_work_item_context(work_item)
    for key in ("id", "status", "route", "next_action"):
        context.pop(key, None)
    context["historical_only"] = True
    return context


_DIRECT_CONTEXT_REFERENCE_RE = re.compile(
    r"\b(?:this|that|these|those|it|same|above|previous|prior|earlier|current)\b",
    re.IGNORECASE,
)
_DIRECT_CONTEXT_OBJECT_OPERATION_RE = re.compile(
    r"\b(?:add|append|attach|change|delete|edit|modify|move|remove|rename|reply|"
    r"reschedule|revise|update)\b[^.\n]{0,120}\b(?:abstract|article|comment|"
    r"description|document|draft|email|event|field|file|item|metadata|note|"
    r"record|row|thread)\b",
    re.IGNORECASE,
)


def _direct_specialist_execution_context(
    input_text: str,
    *,
    workflow_state: dict[str, Any] | None,
    force_thread_context: bool = False,
) -> dict[str, Any]:
    """Project only reference-resolving thread state into a fast direct call."""

    if not workflow_state or not (
        force_thread_context
        or (
            _DIRECT_CONTEXT_REFERENCE_RE.search(input_text)
            or _DIRECT_CONTEXT_OBJECT_OPERATION_RE.search(input_text)
        )
    ):
        return {}

    context: dict[str, Any] = {"schema": "keystone.direct_specialist_context.v1"}
    slack_context = workflow_state.get("slack_context")
    if isinstance(slack_context, dict):
        scope = {
            key: _bounded_redacted_text(slack_context.get(key), max_chars=180)
            for key in (
                "channel_id",
                "channel_name",
                "selected_message_ts",
                "thread_ts",
                "request_ts",
            )
            if _bounded_redacted_text(slack_context.get(key), max_chars=180)
        }
        if scope:
            context["slack_scope"] = scope

    thread_root = _bounded_redacted_text(
        workflow_state.get("slack_thread_root"),
        max_chars=2200,
    )
    if thread_root:
        context["thread_root_request"] = thread_root

    current_normalized = " ".join(str(input_text or "").split())
    recent = workflow_state.get("recent_slack_thread")
    if isinstance(recent, list):
        messages: list[dict[str, str]] = []
        for item in recent[-6:]:
            if not isinstance(item, dict):
                continue
            summary = _bounded_redacted_text(
                item.get("summary") or item.get("text"),
                max_chars=480,
            )
            if not summary or " ".join(summary.split()) == current_normalized:
                continue
            compact = {
                key: value
                for key, value in {
                    "id": _bounded_redacted_text(item.get("id") or item.get("ts"), max_chars=100),
                    "source_agent": _bounded_redacted_text(
                        item.get("source_agent") or item.get("user_id"),
                        max_chars=120,
                    ),
                    "role": _bounded_redacted_text(
                        item.get("role"),
                        max_chars=20,
                    ),
                    "summary": summary,
                }.items()
                if value
            }
            if compact:
                messages.append(compact)
        if messages:
            context["recent_thread_messages"] = messages

    prior_runs = workflow_state.get("prior_agent_runs")
    if isinstance(prior_runs, list):
        runs: list[dict[str, str]] = []
        for item in prior_runs[-4:]:
            if not isinstance(item, dict):
                continue
            compact = {
                key: value
                for key, value in {
                    key: _bounded_redacted_text(item.get(key), max_chars=480)
                    for key in (
                        "id",
                        "route",
                        "status",
                        "thread_correlation",
                        "object_id",
                        "title",
                        "summary",
                    )
                }.items()
                if value
            }
            if compact:
                runs.append(compact)
        if runs:
            context["prior_agent_runs"] = runs

    transcript = _bounded_redacted_text(
        workflow_state.get("slack_thread_transcript"),
        max_chars=2400,
        keep_tail=True,
    )
    if transcript:
        context["thread_transcript_tail"] = transcript

    return context if len(context) > 1 else {}


def _attach_slack_run_provenance(
    payload: dict[str, Any],
    execution_context: Mapping[str, Any] | None,
) -> None:
    """Persist stable Slack correlation without exposing thread prose or provider data."""

    if not isinstance(execution_context, Mapping):
        return
    slack_scope = execution_context.get("slack_scope")
    if not isinstance(slack_scope, Mapping):
        return
    provenance = {
        key: _bounded_redacted_text(slack_scope.get(key), max_chars=180)
        for key in ("channel_id", "thread_ts", "request_ts", "selected_message_ts")
        if _bounded_redacted_text(slack_scope.get(key), max_chars=180)
    }
    if provenance.get("channel_id") and provenance.get("thread_ts"):
        payload["slack_run_provenance"] = {
            "schema": "keystone.slack.run_provenance.v1",
            **provenance,
        }


def _finance_receipt_context_input(
    input_text: str,
    execution_context: dict[str, Any] | None,
) -> str:
    """Join only bounded selected-thread context needed to resolve a receipt file."""

    bounded_context = specialist_execution_context_text(execution_context)
    if not bounded_context:
        return input_text
    return f"{input_text}\n\n{bounded_context}"


def _bounded_redacted_text(
    value: object,
    *,
    max_chars: int,
    keep_tail: bool = False,
) -> str:
    redacted = redact_secrets(str(value or ""))
    text = " ".join(str(redacted or "").replace("\x00", "").split())
    if len(text) <= max_chars:
        return text
    if keep_tail:
        return text[-max_chars:].lstrip()
    return text[:max_chars].rstrip()


def _context_file_is_work_item_source_bundle(context_file_path: str) -> bool:
    if not context_file_path:
        return False
    try:
        data = json.loads(Path(context_file_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and str(data.get("schema") or "").strip() == (
        "keystone.work_item.source_bundle.v1"
    )


def _slack_context_metadata(context_file_path: str) -> dict[str, str]:
    metadata = {
        "channel_id": "C0BA17Y9C01",
        "channel_name": "evals",
        "thread_ts": "",
    }
    if not context_file_path:
        return metadata
    try:
        data = json.loads(Path(context_file_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return metadata
    if not isinstance(data, dict):
        return metadata
    metadata["channel_id"] = str(data.get("channel_id") or metadata["channel_id"]).strip()
    metadata["channel_name"] = str(data.get("channel_name") or metadata["channel_name"]).strip()
    thread_ts = data.get("thread_ts") or data.get("selected_message_ts") or ""
    metadata["thread_ts"] = str(thread_ts or "").strip()
    return metadata


def _slack_context_payload(context_file_path: str) -> dict[str, Any]:
    if not context_file_path:
        return {}
    try:
        data = json.loads(Path(context_file_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _validated_slack_linked_work_item(
    *,
    work_item_id: str,
    database_url: str | None,
    context_file_path: str,
) -> WorkItem | None:
    """Load an adapter-linked WorkItem only for its authenticated Slack thread."""

    if not work_item_id:
        return None
    if not _context_file_is_slack_context(context_file_path):
        raise SystemExit("A linked WorkItem requires authenticated Slack context.")
    payload = _slack_context_payload(context_file_path)
    channel_id = str(payload.get("channel_id") or "").strip()
    thread_ts = str(payload.get("thread_ts") or "").strip()
    if not channel_id or not thread_ts:
        raise SystemExit("Linked WorkItem Slack context is missing channel or thread identity.")

    store = SQLiteStore(database_url or database_url_from_env())
    work_item = store.get_work_item(work_item_id)
    if work_item is None:
        raise SystemExit(f"Linked WorkItem not found: {work_item_id}")
    metadata = work_item.target.metadata if isinstance(work_item.target.metadata, dict) else {}
    stored_context = metadata.get("slack_context")
    if not isinstance(stored_context, dict):
        raise SystemExit("Linked WorkItem has no stored Slack thread binding.")
    stored_channel_id = str(stored_context.get("channel_id") or "").strip()
    stored_thread_ts = str(stored_context.get("thread_ts") or "").strip()
    if channel_id != stored_channel_id or thread_ts != stored_thread_ts:
        raise SystemExit("Linked WorkItem does not match the authenticated Slack thread.")
    return work_item


def _manual_plan_reuses_linked_work_item(
    manual_plan: ManualRequestPlan,
    *,
    work_item: WorkItem | None,
) -> bool:
    """Reuse linked state only for a safe revision of one selected artifact."""

    if work_item is None:
        return False
    if work_item.status in {
        WorkItemStatus.BLOCKED,
        WorkItemStatus.ARCHIVED,
        WorkItemStatus.NEEDS_CONTEXT,
    }:
        return False
    if manual_plan.ask_shape.prior_context_dependency != "selected_context":
        return False
    if (
        manual_plan.intent == "business_system_write"
        or manual_plan.provider_operations
        or manual_plan.side_effect_policy not in {"read_only", "draft_or_read_only"}
    ):
        return False
    expected_type = str(manual_plan.expected_artifact_type or "").strip()
    target_agent = str(manual_plan.target_agent or "").strip()
    if not expected_type or not target_agent:
        return False
    return any(
        artifact.selected
        and artifact.artifact_type == expected_type
        and artifact.source_agent == target_agent
        for artifact in work_item.artifact_refs
    )


def _eval_context_fields(context_file_path: str) -> dict[str, str]:
    """Infer eval identifiers from a Slack context file for natural follow-ups."""

    metadata = _eval_context_metadata(context_file_path)
    thread_metadata = _eval_context_fields_from_thread(context_file_path)
    text = _eval_context_text(context_file_path)
    compact = " ".join(text.split())
    case_id = (
        metadata.get("case_id", "")
        or thread_metadata.get("case_id", "")
        or _extract_eval_template_field(compact, "case")
        or _last_regex_group(r"\beval\s+case\s+([A-Za-z0-9_.:-]+)", compact)
        or _last_regex_group(r"\bcase[_\s-]*id\s*[:=]\s*([A-Za-z0-9_.:-]+)", compact)
    )
    run_id = (
        metadata.get("run_id", "")
        or thread_metadata.get("run_id", "")
        or _last_regex_group(r"\bWorkItem:\s*(wi_[A-Za-z0-9_.:-]+)", text)
        or _extract_eval_template_field(compact, "run")
        or _last_regex_group(r"\bRun:\s*([A-Za-z0-9_.:-]+)", text)
    )
    agent = (
        metadata.get("agent", "")
        or thread_metadata.get("agent", "")
        or _extract_eval_template_field(compact, "agent")
        or _last_regex_group(r"\bRoute:\s*([A-Za-z0-9_.:-]+)", text)
        or _agent_from_eval_context_text(compact)
    )
    return {
        key: value
        for key, value in {
            "case_id": case_id,
            "run_id": run_id,
            "agent": agent,
        }.items()
        if value
    }


def _eval_context_fields_from_thread(context_file_path: str) -> dict[str, str]:
    slack_context = _slack_context_metadata(context_file_path)
    if not slack_context.get("thread_ts"):
        return {}
    try:
        from promptfoo.eval_database import DEFAULT_EVAL_DB, eval_context_from_slack_thread
    except ImportError:
        return {}
    database_path = Path(os.environ.get("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB") or DEFAULT_EVAL_DB)
    try:
        return eval_context_from_slack_thread(
            slack_thread_ts=slack_context.get("thread_ts", ""),
            slack_channel_id=slack_context.get("channel_id", "C0BA17Y9C01"),
            slack_channel_name=slack_context.get("channel_name", "evals"),
            database_path=database_path,
        )
    except (OSError, ValueError):
        return {}


def _eval_context_metadata(context_file_path: str) -> dict[str, str]:
    if not context_file_path:
        return {}
    try:
        data = json.loads(Path(context_file_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    eval_data = data.get("eval") if isinstance(data.get("eval"), dict) else {}
    case_id = str(eval_data.get("case_id") or data.get("eval_case_id") or "").strip()
    run_id = str(eval_data.get("run_id") or data.get("eval_run_id") or "").strip()
    agent = str(eval_data.get("agent") or data.get("eval_agent") or "").strip()
    return {
        key: value
        for key, value in {
            "case_id": case_id,
            "run_id": run_id,
            "agent": agent,
        }.items()
        if value
    }


def _eval_context_text(context_file_path: str) -> str:
    if not context_file_path:
        return ""
    try:
        data = json.loads(Path(context_file_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    for key in (
        "read_context",
        "selected_message_text",
        "message_text",
        "request_text",
        "text",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


def _last_regex_group(pattern: str, text: str) -> str:
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    if not matches:
        return ""
    value = matches[-1]
    if isinstance(value, tuple):
        value = next((item for item in value if item), "")
    return str(value or "").strip().rstrip(".,;)")


def _agent_from_eval_context_text(text: str) -> str:
    lowered = text.lower()
    phrase_to_route = {
        "business research analyst": "business_research_analyst",
        "opportunity scout": "opportunity_scout",
        "chief of staff": "chief_of_staff",
        "gmail triage": "gmail_triage",
        "outreach composer": "outreach_composer",
    }
    for phrase, route in phrase_to_route.items():
        if phrase in lowered or route in lowered:
            return route
    return ""


def _run_ask_work_item(
    input_text: str,
    *,
    database_url: str | None,
    live_search: bool,
    live_sdk: bool,
    max_results: int,
    json_output: bool,
    live_rss_slack_read: bool = False,
    max_manager_steps: int = 3,
    manual_plan: ManualRequestPlan | None = None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    context_file_path: str = "",
    sdk_session_enabled: bool | None = None,
    sdk_session_id: str = "",
    sdk_session_db_path: str = "",
    sdk_session_history_limit: int | None = None,
    cost_tracking_requested: bool = False,
    requested_route: str | None = None,
    explicit_work_item_id: str | None = None,
) -> int:
    try:
        live_search = live_search_allowed_for_execution(
            live_search,
            manual_plan=manual_plan,
            request_text=input_text,
        )
        store = SQLiteStore(database_url or database_url_from_env())
        work_item_id = _resolve_continue_work_item_id(
            store,
            input_text=input_text,
            explicit_work_item_id=explicit_work_item_id,
            json_output=json_output,
        )
        existing_work_item = store.get_work_item(work_item_id) if work_item_id else None
        request = WorkflowRunRequest(
            request_text=input_text,
            work_item_id=work_item_id,
            save=True,
            database_url=database_url,
            live_search=live_search,
            live_sdk=live_sdk,
            live_rss_slack_read=live_rss_slack_read,
            max_results=max_results,
            requested_route=requested_route,
            manual_request_plan=manual_plan.model_dump(mode="json") if manual_plan else None,
            orchestrator_preflight=_orchestrator_preflight_payload(orchestrator_preflight),
            context_file_path=context_file_path,
            sdk_session_enabled=sdk_session_enabled,
            sdk_session_id=sdk_session_id,
            sdk_session_db_path=sdk_session_db_path,
            sdk_session_history_limit=sdk_session_history_limit,
            cost_tracking_requested=cost_tracking_requested,
            **_workflow_cost_options_for_request_context(
                request_text=input_text,
                context_file_path=context_file_path,
                work_item=existing_work_item,
            ),
        )
        from keystone_agents.langgraph_workflow import (
            advance_work_item_manager_loop_with_optional_langgraph,
        )

        result = advance_work_item_manager_loop_with_optional_langgraph(
            request,
            max_steps=max_manager_steps,
            feedback_callback=None if json_output else _print_manager_loop_feedback,
        )
    except Exception as exc:
        if not json_output:
            raise
        return _print_ask_work_item_failure(
            input_text=input_text,
            exc=exc,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
        )
    eval_record = None
    if not _promptfoo_agent_eval_mode():
        eval_record = _record_eval_slack_run_if_requested(
            input_text,
            context_file_path=context_file_path,
            result=result,
        )
        if eval_record is not None and not _slack_eval_feedback_is_operator_visible(
            input_text,
            context_file_path=context_file_path,
        ):
            # Keep ordinary operational Slack runs available to the local eval
            # database without turning eval links, buttons, or case metadata
            # into the user-facing business answer.
            eval_record = None
    graph_metadata = _stored_work_item_langgraph_metadata(store, result.work_item.id)
    return _print_work_item_result(
        result,
        json_output=json_output,
        graph_metadata=graph_metadata,
        eval_record=eval_record,
        execution_metadata={
            "live_sdk": live_sdk,
            "live_search": live_search,
            "langgraph": graph_metadata is not None,
            "openai_requests": _stored_work_item_openai_requests(
                store,
                result.work_item.id,
            ),
        },
    )


def _stored_work_item_langgraph_metadata(
    store: SQLiteStore,
    work_item_id: str,
) -> dict[str, object] | None:
    for event in reversed(store.list_work_item_events(work_item_id)):
        if event.event_type != "langgraph_orchestration":
            continue
        return {
            "runtime": str(event.metadata.get("runtime") or "langgraph"),
            "node_path": list(event.metadata.get("node_path") or []),
            "checkpoint_required": bool(event.metadata.get("checkpoint_required")),
            "checkpoint_reason": str(event.metadata.get("checkpoint_reason") or ""),
            "graph_completion_review": dict(event.metadata.get("graph_completion_review") or {}),
        }
    return None


def _stored_work_item_openai_requests(store: SQLiteStore, work_item_id: str) -> int:
    requests = 0
    for event in store.list_work_item_events(work_item_id):
        if event.event_type != "workflow_sdk_usage":
            continue
        usage = event.metadata.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            requests += int(usage.get("requests") or 0)
        except (TypeError, ValueError):
            continue
    return requests


def _promptfoo_agent_eval_mode() -> bool:
    return str(os.environ.get("KEYSTONE_PROMPTFOO_EVAL") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _slack_eval_feedback_is_operator_visible(
    input_text: str,
    *,
    context_file_path: str,
) -> bool:
    """Expose eval UI only in an explicit eval context, never from fuzzy matching."""

    payload = _slack_context_payload(context_file_path)
    channel_name = str(payload.get("channel_name") or "").strip().lower()
    if channel_name == "evals":
        return True
    if isinstance(payload.get("eval"), dict):
        return True
    normalized = " ".join(str(input_text or "").lower().split())
    return bool(re.search(r"\b(?:eval|promptfoo)\b", normalized))


def _record_eval_slack_run_if_requested(
    input_text: str,
    *,
    context_file_path: str,
    result: WorkflowRunResult,
) -> dict[str, object] | None:
    if not _context_file_is_slack_context(context_file_path):
        return None
    try:
        from promptfoo.eval_database import (
            DEFAULT_EVAL_DB,
            record_slack_eval_run,
            resolve_slack_eval_case_id,
        )
    except ImportError:
        return None

    slack_context = _slack_context_metadata(context_file_path)
    work_item = result.work_item
    run_id = getattr(work_item, "id", "") if work_item is not None else ""
    agent = str(getattr(work_item, "current_route", "") or getattr(result, "route", "") or "")
    case_id = _extract_eval_run_case_id(input_text, context_file_path=context_file_path)
    database_path = Path(os.environ.get("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB") or DEFAULT_EVAL_DB)
    if not case_id:
        try:
            case_id = resolve_slack_eval_case_id(
                request_text=input_text,
                agent=agent,
                slack_channel_id=slack_context.get("channel_id", "C0BA17Y9C01"),
                slack_channel_name=slack_context.get("channel_name", "evals"),
                database_path=database_path,
            )
        except (OSError, ValueError):
            case_id = ""
    if not case_id:
        return None
    summary = _eval_run_result_summary(result)
    evidence = _cli_slack_eval_evidence(
        slack_context_payload=_slack_context_payload(context_file_path),
        result=result,
        run_id=run_id,
        summary=summary,
    )
    try:
        row_id = record_slack_eval_run(
            case_id=case_id,
            run_id=run_id,
            agent=agent,
            work_item_id=run_id,
            slack_channel_id=slack_context.get("channel_id", "C0BA17Y9C01"),
            slack_channel_name=slack_context.get("channel_name", "evals"),
            slack_thread_ts=slack_context.get("thread_ts", ""),
            request_text=input_text,
            result_summary=summary,
            route=str(evidence.get("route") or agent),
            status=str(evidence.get("status") or ""),
            context_policy=str(evidence.get("context_policy") or ""),
            thread_fetch_status=str(evidence.get("thread_fetch_status") or ""),
            thread_message_count=int(evidence.get("thread_message_count") or 0),
            warning_count=int(evidence.get("warning_count") or 0),
            warnings=[str(item) for item in evidence.get("warnings") or []],
            cost_profile=str(evidence.get("cost_profile") or ""),
            source_count=int(evidence.get("source_count") or 0),
            visible_source_count=int(evidence.get("visible_source_count") or 0),
            sdk_estimated_cost_usd=evidence.get("sdk_estimated_cost_usd"),
            sdk_cache_hit_rate=evidence.get("sdk_cache_hit_rate"),
            duration_ms=evidence.get("duration_ms"),
            response_hash=str(evidence.get("response_hash") or ""),
            evidence=evidence,
            prompt_versions=[
                item for item in evidence.get("prompt_versions") or [] if isinstance(item, dict)
            ],
            prompt_metadata=(
                evidence.get("prompt_metadata")
                if isinstance(evidence.get("prompt_metadata"), dict)
                else {}
            ),
            model_provider=str(evidence.get("model_provider") or ""),
            model_name=str(evidence.get("model_name") or ""),
            run_mode=str(evidence.get("run_mode") or ""),
            search_provider=str(evidence.get("search_provider") or ""),
            search_provider_sequence=[
                str(item) for item in evidence.get("search_provider_sequence") or []
            ],
            database_path=database_path,
        )
    except (OSError, ValueError):
        return None
    try:
        from promptfoo.eval_dashboard import slack_run_post_save_state

        post_save_state = slack_run_post_save_state(
            case_id=case_id,
            row_id=row_id,
            database_path=database_path,
        )
    except (ImportError, OSError, ValueError, sqlite3.Error):
        post_save_state = {}
    dashboard = _render_promptfoo_dashboard(database_path)
    case_dashboard = _dashboard_case_link(dashboard, case_id)
    review_case = _review_case_link(case_id)
    return {
        "id": row_id,
        "case_id": case_id,
        "run_id": run_id,
        "agent": agent,
        "database_path": str(database_path),
        "slack_thread_ts": slack_context.get("thread_ts", ""),
        "evidence": evidence,
        "post_save_state": post_save_state,
        "dashboard_visibility": post_save_state.get("dashboard_visibility", {}),
        "refresh_endpoints": post_save_state.get("refresh_endpoints", []),
        "scorecard_request": "@KNI can you give me a scorecard for this eval?",
        "submit_evaluation_action": "Submit Evaluation",
        "eval_thread_reply": _eval_thread_reply_guidance(
            case_id=case_id,
            run_id=run_id,
            agent=agent,
            slack_thread_ts=slack_context.get("thread_ts", ""),
            dashboard_case_url=case_dashboard.get("dashboard_case_url", ""),
            review_case_url=review_case.get("review_case_url", ""),
        ),
        **dashboard,
        **case_dashboard,
        **review_case,
    }


def _extract_eval_run_case_id(input_text: str, *, context_file_path: str = "") -> str:
    context_case_id = _eval_context_fields(context_file_path).get("case_id", "")
    if context_case_id:
        return context_case_id
    compact = " ".join(str(input_text or "").split())
    lowered = compact.lower()
    if "eval" not in lowered and "promptfoo" not in lowered:
        return ""
    return _extract_eval_template_field(compact, "case")


def _cli_slack_eval_evidence(
    *,
    slack_context_payload: dict[str, Any],
    result: WorkflowRunResult,
    run_id: str,
    summary: str,
) -> dict[str, Any]:
    result_payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else {}
    work_item = (
        result_payload.get("work_item") if isinstance(result_payload.get("work_item"), dict) else {}
    )
    target = work_item.get("target") if isinstance(work_item.get("target"), dict) else {}
    metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
    slack_context = (
        metadata.get("slack_context") if isinstance(metadata.get("slack_context"), dict) else {}
    )
    query_prompt = (
        slack_context.get("query_prompt")
        if isinstance(slack_context.get("query_prompt"), dict)
        else {}
    )
    context_query_prompt = (
        slack_context_payload.get("query_prompt")
        if isinstance(slack_context_payload.get("query_prompt"), dict)
        else {}
    )
    thread_messages = (
        slack_context.get("thread_messages") or slack_context_payload.get("thread_messages") or []
    )
    message_count = len(thread_messages) if isinstance(thread_messages, list) else 0
    if not message_count and str(slack_context_payload.get("read_context") or "").strip():
        message_count = 1
    thread_fetch_status = str(
        slack_context.get("thread_fetch_status")
        or slack_context_payload.get("thread_fetch_status")
        or ("ok" if message_count else "not_requested")
    )
    warnings = [
        str(item)
        for item in (slack_context.get("warnings") or slack_context_payload.get("warnings") or [])
    ]
    sources = work_item.get("sources") if isinstance(work_item.get("sources"), list) else []
    sdk_usage = _latest_event_metadata(result_payload, "usage")
    sdk_cost = _latest_event_metadata(result_payload, "cost")
    model = result_payload.get("model") if isinstance(result_payload.get("model"), dict) else {}
    execution_provenance = (
        result_payload.get("execution_provenance")
        if isinstance(result_payload.get("execution_provenance"), dict)
        else {}
    )
    retrieval = (
        result_payload.get("retrieval") if isinstance(result_payload.get("retrieval"), dict) else {}
    )
    duration_ms = _number_or_none(
        result_payload.get("duration_ms")
        or result_payload.get("elapsed_ms")
        or result_payload.get("runtime_ms")
    )
    slack_channel_id = str(slack_context_payload.get("channel_id") or "")
    slack_channel_name = str(slack_context_payload.get("channel_name") or "")
    slack_thread_ts = str(slack_context_payload.get("thread_ts") or "")
    evidence = {
        "schema": "keystone.slack.eval_evidence.v1",
        "source": "cli_slack_context",
        "work_item_id": str(run_id or work_item.get("id") or ""),
        "route": str(result_payload.get("route") or ""),
        "status": str(result_payload.get("status") or ""),
        "slack_channel_id": slack_channel_id,
        "slack_channel_name": slack_channel_name,
        "slack_thread_ts": slack_thread_ts,
        "slack_context": {
            "channel_id": slack_channel_id,
            "channel_name": slack_channel_name,
            "thread_ts": slack_thread_ts,
            "permalink_present": bool(slack_context_payload.get("permalink")),
        },
        "context_policy": str(
            slack_context.get("prompt_context_layout")
            or slack_context.get("channel_history_policy")
            or slack_context_payload.get("context_scope")
            or slack_context_payload.get("schema")
            or ""
        ),
        "thread_fetch_status": thread_fetch_status,
        "thread_message_count": message_count,
        "warning_count": len(warnings),
        "warnings": warnings[:8],
        "cost_profile": str(
            query_prompt.get("cost_profile")
            or context_query_prompt.get("cost_profile")
            or slack_context_payload.get("cost_profile")
            or ""
        ),
        "source_count": len(sources),
        "visible_source_count": sum(
            1 for item in sources if isinstance(item, dict) and item.get("url")
        ),
        "sdk_estimated_cost_usd": _number_or_none(
            sdk_cost.get("estimated_usd") or sdk_cost.get("amount_usd")
        ),
        "sdk_cache_hit_rate": _number_or_none(sdk_usage.get("cache_hit_rate")),
        "duration_ms": duration_ms,
        "time_to_response_ms": duration_ms,
        "execution": {
            "duration_ms": duration_ms,
            "time_to_response_ms": duration_ms,
        },
        "response_hash": _hash_text(summary) if summary else "",
        "response_summary_chars": len(summary),
        "model_provider": str(
            model.get("provider") or execution_provenance.get("model_provider") or ""
        ),
        "model_name": str(
            model.get("name") or model.get("model") or execution_provenance.get("model_name") or ""
        ),
        "run_mode": str(model.get("run_mode") or execution_provenance.get("run_mode") or ""),
        "search_provider": str(
            retrieval.get("search_provider")
            or retrieval.get("provider")
            or execution_provenance.get("search_provider")
            or ""
        ),
        "search_provider_sequence": [
            str(item)
            for item in (
                retrieval.get("search_provider_sequence")
                or execution_provenance.get("search_provider_sequence")
                or []
            )
        ],
        "execution_provenance": execution_provenance,
        "prompt_versions": _trace_prompt_versions(result_payload),
        "prompt_metadata": {
            "source": "cli_slack_context_eval_save",
            "route": str(result_payload.get("route") or ""),
            "status": str(result_payload.get("status") or ""),
            "work_item_id": str(run_id or work_item.get("id") or ""),
        },
    }
    tool_summary = _trace_tool_summary_from_payload(result_payload)
    if tool_summary:
        evidence["tool_summary"] = tool_summary
    child_steps = slack_eval_child_step_summary(result_payload, tool_summary)
    if child_steps:
        evidence["child_step_summary"] = child_steps
    orchestrator_summary = _trace_orchestrator_summary_from_payload(result_payload)
    if orchestrator_summary.get("orchestrator"):
        evidence["orchestrator"] = orchestrator_summary["orchestrator"]
    if orchestrator_summary.get("orchestrator_preflight"):
        evidence["orchestrator_preflight"] = orchestrator_summary["orchestrator_preflight"]
    if orchestrator_summary.get("orchestrator_review"):
        evidence["orchestrator_review"] = orchestrator_summary["orchestrator_review"]
    blocker_diagnostics = slack_eval_blocker_diagnostics(result_payload)
    if blocker_diagnostics:
        evidence["blocker_diagnostics"] = blocker_diagnostics
    return evidence


def _latest_event_metadata(payload: dict[str, Any], key: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        value = metadata.get(key) if isinstance(metadata.get(key), dict) else {}
        if value:
            candidates.append(value)
    return candidates[-1] if candidates else {}


def _trace_prompt_versions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("prompt_versions", "prompt_config_versions"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value[:20] if isinstance(item, dict)]
    return []


def _trace_tool_summary_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    existing = payload.get("tool_summary") if isinstance(payload.get("tool_summary"), dict) else {}
    if existing:
        return existing
    tooling = payload.get("tooling") if isinstance(payload.get("tooling"), dict) else {}
    if tooling:
        return tooling
    counts: dict[str, int] = {}
    failures: dict[str, int] = {}
    statuses: dict[str, set[str]] = {}
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        event_type = str(event.get("event_type") or event.get("type") or "").lower()
        name = _clean_eval_scalar(
            metadata.get("tool_name")
            or metadata.get("tool")
            or metadata.get("function_name")
            or event.get("tool_name")
            or event.get("name")
            or ("unknown_tool" if "tool" in event_type or "function" in event_type else "")
        )
        if not name:
            continue
        status = _clean_eval_scalar(metadata.get("status") or event.get("status") or "")
        failed = bool(
            metadata.get("error")
            or metadata.get("error_type")
            or status.lower() in {"error", "failed", "failure", "timeout"}
        )
        counts[name] = counts.get(name, 0) + 1
        if status:
            statuses.setdefault(name, set()).add(status.lower())
        if failed:
            failures[name] = failures.get(name, 0) + 1
    if not counts:
        return {}
    return {
        "tool_call_count": sum(counts.values()),
        "failed_tool_call_count": sum(failures.values()),
        "tool_names": sorted(counts)[:20],
        "tool_call_summary": [
            {
                "name": name,
                "count": counts[name],
                "failed_count": failures.get(name, 0),
                "status": "failed"
                if failures.get(name, 0)
                else (sorted(statuses.get(name, set()))[-1] if statuses.get(name) else "observed"),
            }
            for name in sorted(counts)[:20]
        ],
    }


def _trace_orchestrator_summary_from_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    preflight = (
        payload.get("orchestrator_preflight")
        if isinstance(payload.get("orchestrator_preflight"), dict)
        else {}
    )
    review = (
        payload.get("orchestrator_review")
        if isinstance(payload.get("orchestrator_review"), dict)
        else {}
    )
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    feedback = payload.get("operator_feedback_requests")
    if not isinstance(feedback, list):
        feedback = []
    preflight_blocker_count = _safe_count(
        preflight.get("blocker_count") or preflight.get("preflight_blocker_count")
    )
    review_feedback_count = _safe_count(
        review.get("feedback_count") or review.get("review_feedback_count")
    )
    result: dict[str, dict[str, Any]] = {}
    orchestrator = {
        "preflight": bool(preflight),
        "review": bool(review),
        "blocker_count": preflight_blocker_count or len(blockers),
        "feedback_count": review_feedback_count or len(feedback),
        "selected_route": _clean_eval_scalar(
            preflight.get("selected_route") or preflight.get("route") or payload.get("route") or ""
        ),
        "review_status": _clean_eval_scalar(
            review.get("status") or review.get("review_status") or ""
        ),
    }
    if any(orchestrator.values()):
        result["orchestrator"] = orchestrator
    if preflight:
        result["orchestrator_preflight"] = {
            "blocker_count": orchestrator["blocker_count"],
            "selected_route": orchestrator["selected_route"],
            "has_preflight": True,
        }
    if review:
        result["orchestrator_review"] = {
            "feedback_count": orchestrator["feedback_count"],
            "review_status": orchestrator["review_status"],
            "has_review": True,
        }
    return result


def _clean_eval_scalar(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text if len(text) <= 96 else f"{text[:93].rstrip()}..."


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _eval_run_result_summary(result: WorkflowRunResult) -> str:
    work_item = result.work_item
    title = str(getattr(work_item, "title", "") or "").strip() if work_item is not None else ""
    status = str(getattr(result, "status", "") or "").strip()
    route = str(getattr(result, "route", "") or "").strip()
    if title:
        return f"{status} {route}: {title}".strip()
    return f"{status} {route}".strip()


def _print_ask_work_item_failure(
    *,
    input_text: str,
    exc: Exception,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
) -> int:
    error_type = type(exc).__name__
    failure = known_exception_to_operator_failure(exc, context="WorkItem run")
    payload = {
        "mode": "work_item",
        "status": "failed",
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "output": {
            "summary": failure.summary,
            "send_enabled": False,
            "failure": failure.to_dict(),
            "error_type": error_type,
            "error_message": failure.reason,
            "next_step": failure.next_step,
        },
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    print(f"Business Agents run failed: {failure.summary}", file=sys.stderr)
    if failure.reason and failure.reason != failure.summary:
        print(f"Reason: {failure.reason}", file=sys.stderr)
    return 1


def _operator_failure_from_child_output(
    stdout: str,
    *,
    fallback: Exception,
    context: str,
):
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        candidates = [
            payload.get("failure"),
            payload.get("output", {}).get("failure")
            if isinstance(payload.get("output"), dict)
            else None,
        ]
        for candidate in candidates:
            failure = operator_failure_from_mapping(candidate)
            if failure is not None:
                return failure
    return known_exception_to_operator_failure(fallback, context=context)


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
    database_url: str | None = None,
) -> int:
    sdk_result = None
    if live_sdk:
        load_settings(force_dotenv=True)
        sdk_result = run_orchestrator_sdk(
            input_text,
            live=True,
            session=build_sdk_session(sdk_session_spec) if sdk_session_spec else None,
            manual_request_plan=manual_plan,
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
    execution_context: dict[str, Any] | None = None,
) -> int:
    agent = AGENT_REGISTRY[route].build_agent()
    context_agent_output = _context_agent_dry_run_output(
        route,
        input_text,
        manual_plan,
        execution_context=execution_context,
    )
    chief_of_staff_output = (
        plan_chief_of_staff_request(
            input_text,
            database_url=database_url,
            manual_request_plan=manual_plan,
        )
        if route == "chief_of_staff" and context_agent_output is None
        else None
    )
    output_payload = (
        context_agent_output.model_dump(mode="json")
        if context_agent_output is not None
        else chief_of_staff_output.model_dump(mode="json")
        if chief_of_staff_output
        else None
    )
    output_type = (
        type(context_agent_output).__name__
        if context_agent_output is not None
        else type(chief_of_staff_output).__name__
        if chief_of_staff_output is not None
        else ""
    )
    status = (
        "blocked"
        if context_agent_output is not None and _context_agent_output_has_blockers(output_payload)
        else "done"
    )
    payload = {
        "_execution": {
            "langgraph": False,
            "live_sdk": False,
            "live_search": False,
            "openai_requests": 0,
        },
        "mode": "dry_run",
        "status": status,
        "route": route,
        "selected_agent": route,
        "agent_name": _agent_display_name(route),
        "sdk_agent_name": agent.name,
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "output_type": output_type,
        "output": output_payload,
        "human_summary": _context_agent_human_summary(output_payload, manual_plan)
        if context_agent_output is not None
        else "",
        "blockers": _context_agent_blocker_messages(output_payload),
        "side_effects": {
            "schema": "keystone.promptfoo.side_effects.v1",
            "email_sent": False,
            "gmail_draft_created": False,
            "gmail_label_changed": False,
            "slack_message_posted": False,
            "crm_write_performed": False,
            "calendar_write_performed": False,
            "external_file_write_performed": False,
            "external_write_performed": False,
            "blocked_write_attempts": _context_agent_blocked_write_attempts(output_payload),
            "approval_ref": "",
            "evidence_complete": True,
        },
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
        if context_agent_output is not None and payload["human_summary"]:
            print()
            print(payload["human_summary"])
        print("Mode: dry_run")
        print("Send enabled: False")
        print(payload["note"])
    return 0


def _context_agent_dry_run_output(
    route: str,
    input_text: str,
    manual_plan: ManualRequestPlan | None,
    *,
    execution_context: dict[str, Any] | None = None,
) -> (
    AirtableContextResult
    | GoogleWorkspaceContextResult
    | PreprintsContextResult
    | RssContextResult
    | ZoteroContextResult
    | None
):
    """Return deterministic no-API context-agent output for direct dry-run evals."""

    blocked = _context_agent_request_is_blocked(input_text)
    objective = (
        manual_plan.objective
        if manual_plan and manual_plan.objective
        else input_text.strip().strip('"')
    )
    if route == "airtable_context_agent":
        topic_terms = _airtable_context_topic_terms(objective)
        expense_receipt_target = resolve_finance_expense_receipt_target(
            _finance_receipt_context_input(objective, execution_context),
            manual_plan=manual_plan,
        )
        finance_tax_focus = _airtable_context_is_finance_tax_request(topic_terms, objective)
        eval_tracker_focus = _airtable_context_is_eval_tracker_request(topic_terms, objective)
        blockers = (
            [
                "Missing Airtable record id, field mapping, and approval reference for the requested base/table."
            ]
            if blocked
            else []
        )
        return AirtableContextResult(
            mode="deterministic",
            summary=(_airtable_context_summary(topic_terms, objective))
            if not blocked
            else (
                "Airtable blocker: record id, field mapping, and approval reference are "
                "required before Chief of Staff can approve any Airtable write."
            ),
            base_alias=(
                expense_receipt_target.base_alias
                if expense_receipt_target is not None
                else "finance_tax_tracker"
                if finance_tax_focus
                else "eval_tracker"
                if eval_tracker_focus
                else "requested_airtable_base"
            ),
            relevant_tables=[expense_receipt_target.table]
            if expense_receipt_target is not None
            else ["Tax Payments"]
            if finance_tax_focus
            else ["Eval tracker"]
            if eval_tracker_focus
            else ["requested table"],
            relevant_fields=(
                finance_expense_receipt_field_hints(expense_receipt_target)
                if expense_receipt_target is not None
                else _airtable_context_field_hints(topic_terms, objective)
            ),
            recommended_record_identity=_airtable_context_record_identity(topic_terms, objective),
            recommended_actions=[
                "Read Airtable schema before records.",
                "Use exact table and field names before interpreting records.",
                *(
                    [
                        (
                            "Infer `finance_tax_tracker` and the requested expense table "
                            "from the Airtable expense receipt ask; do not ask the operator "
                            "to restate the base/table."
                        ),
                        (
                            "Use receipt evidence and schema field matching to prepare the "
                            "create plan; ask only for fields or attachment support that schema "
                            "does not expose."
                        ),
                    ]
                    if expense_receipt_target is not None
                    else []
                ),
                "Keep Airtable writes owned by Airtable Context or the approved action handler.",
            ],
            write_plan=OperationalWritePlan(
                target_system="airtable",
                operation=(
                    "airtable_specialist_create_from_receipt_after_schema_and_approval"
                    if expense_receipt_target is not None
                    else "airtable_specialist_update_after_approval"
                ),
                target=(
                    f"{expense_receipt_target.base_alias} / {expense_receipt_target.table}"
                    if expense_receipt_target is not None
                    else "Airtable record selected after schema and record identity are confirmed"
                ),
                scope=(
                    "one receipt-backed expense create plus optional receipt attachment after schema mapping"
                    if expense_receipt_target is not None
                    else "read-only context handoff unless a separate approved write is provided"
                ),
                field_mapping=[
                    OperationalContextEntry(
                        key="base_alias",
                        value=(
                            expense_receipt_target.base_alias
                            if expense_receipt_target is not None
                            else "finance_tax_tracker"
                            if finance_tax_focus
                            else "eval_tracker"
                            if eval_tracker_focus
                            else "requested base"
                        ),
                    ),
                    OperationalContextEntry(
                        key="table",
                        value=(
                            expense_receipt_target.table
                            if expense_receipt_target is not None
                            else "Tax Payments"
                            if finance_tax_focus
                            else "Eval tracker"
                            if eval_tracker_focus
                            else "requested table"
                        ),
                    ),
                    *(
                        [
                            OperationalContextEntry(
                                key="receipt_local_path",
                                value=expense_receipt_target.receipt_local_path,
                            ),
                            OperationalContextEntry(
                                key="estimated_tax_period",
                                value="derive from receipt date using tracker period rules",
                            ),
                        ]
                        if expense_receipt_target is not None
                        else []
                    ),
                    OperationalContextEntry(
                        key="filter",
                        value=_airtable_context_record_identity(topic_terms, objective),
                    ),
                ],
                rationale="Specialist is read-only; Chief of Staff owns any approved write.",
            ),
            blockers=blockers,
            approval_needs=(
                [
                    "Scoped Airtable approval reference",
                    "Confirmed expense-table schema field mapping",
                    "Created record id before receipt attachment upload",
                    "Confirmed attachment field support before upload",
                ]
                if expense_receipt_target is not None
                else [
                    "Scoped Airtable approval reference",
                    "Confirmed base/table/record identity",
                ]
            ),
            human_work_context=HumanWorkContext(
                work_functions=_airtable_context_work_functions(topic_terms, objective),
                human_owner_hint="Chief of Staff",
                decision_needed=(
                    "Confirm schema field mapping and attachment-field support before Chief creates the expense record."
                    if expense_receipt_target is not None
                    else "Confirm exact Airtable base, table, schema, and record identity before any write."
                ),
                handoff_ready_context=(
                    [
                        "inferred finance_tax_tracker expense target",
                        "candidate receipt field mapping",
                        "read-only result limits",
                    ]
                    if expense_receipt_target is not None
                    else ["schema mapping", "record identity questions", "read-only result limits"]
                ),
                missing_context=[*blockers, "live Airtable schema and record reads"],
                integration_surfaces=["Airtable"],
                follow_up_actions=(
                    [
                        "run live schema read for the expense table",
                        "match receipt fields to exact Airtable field names",
                        "confirm attachment field before upload",
                    ]
                    if expense_receipt_target is not None
                    else ["run live read if record values are needed", "confirm target record"]
                ),
            ),
            sources=[
                OperationalContextSource(
                    source_id="dry_run_airtable_context",
                    title="Dry-run Airtable context boundary",
                    source_type="dry_run_context",
                    location="local KBA CLI",
                    note=(
                        "Useful references require live Airtable schema/record reads; this "
                        "dry-run proves route, topic focus, no-write boundaries, and output shape."
                    ),
                )
            ],
            diagnostics=[
                OperationalContextEntry(key="dry_run", value="true"),
                OperationalContextEntry(key="objective", value=objective),
            ],
        )
    if route == "google_workspace_context_agent":
        topic_terms = _workspace_context_topic_terms(objective)
        eval_artifact_focus = _workspace_context_is_eval_artifact_request(topic_terms, objective)
        topic_focus = (
            ", ".join(topic_terms[:4]) if topic_terms else "the requested Workspace context"
        )
        blockers = (
            [
                "Missing Drive folder id, document id, Sheet tab, approval reference, and sharing scope."
            ]
            if blocked
            else []
        )
        return GoogleWorkspaceContextResult(
            mode="deterministic",
            summary=(
                (
                    "Google Workspace read-only context dry-run recognized KNI Ops / Evals "
                    "artifact placement for the Slack eval review narrative Doc and Eval "
                    "tracker Sheet. It returns candidate folder, Doc purpose, Sheet tab "
                    "purpose, naming convention, approval needs, and no-write blockers. "
                )
                if eval_artifact_focus
                else (
                    "Google Workspace read-only context dry-run recognized the requested focus on "
                    f"{topic_focus}. "
                )
            )
            + (
                "Drive folders, Docs, Sheets, permissions, and file contents were not read "
                "in this dry-run; use the live Workspace context path before naming specific "
                "files or relying on document contents."
            )
            if not blocked
            else (
                (
                    "Google Workspace blocker for KNI Ops / Evals, Slack eval review "
                    "narrative, and Eval tracker Sheet: folder id, document id, Sheet tab, "
                    "sharing scope, and approval reference are required before any Drive, "
                    "Doc, or Sheet write."
                )
                if eval_artifact_focus
                else (
                    "Google Workspace blocker: folder id, document id, Sheet tab, sharing scope, "
                    "and approval reference are required before any Drive, Doc, or Sheet write."
                )
            ),
            relevant_folders=_workspace_context_folder_hints(topic_terms),
            relevant_docs=_workspace_context_doc_hints(topic_terms),
            relevant_sheets=_workspace_context_sheet_hints(topic_terms),
            recommended_target=_workspace_context_recommended_target(topic_terms),
            recommended_actions=[
                "List scoped Drive folder before selecting a handoff source.",
                "Read existing Doc or Sheet context before naming specific files.",
                "Keep Workspace writes owned by Google Workspace Context or the approved action handler.",
            ],
            write_plan=OperationalWritePlan(
                target_system="google_workspace",
                operation="workspace_specialist_artifact_update_after_approval",
                target="Drive folder, Doc, or Sheet selected after live read",
                scope="internal handoff context, source notes, and optional tracker metadata",
                field_mapping=[
                    OperationalContextEntry(key="folder", value="candidate Drive folder"),
                    OperationalContextEntry(key="doc", value="candidate handoff or SOP Doc"),
                    OperationalContextEntry(
                        key="sheet_tab", value="candidate tracker tab if relevant"
                    ),
                    OperationalContextEntry(key="name", value="target-specific naming convention"),
                ],
                rationale="Specialist is read-only; Chief of Staff owns any approved Workspace write.",
            ),
            blockers=blockers,
            approval_needs=[
                "Scoped Google Workspace approval reference",
                "Confirmed folder/file/tab identity and sharing scope",
            ],
            human_work_context=HumanWorkContext(
                work_functions=_workspace_context_work_functions(topic_terms),
                human_owner_hint="Chief of Staff",
                decision_needed="Confirm source folder/file and sharing scope before using Workspace context.",
                handoff_ready_context=["topic focus", "candidate folder/doc hints"],
                missing_context=[*blockers, "live Workspace file listing and content reads"],
                integration_surfaces=["Google Drive", "Google Docs", "Google Sheets"],
                follow_up_actions=[
                    "run live read if file evidence is needed",
                    "confirm artifact target",
                ],
            ),
            sources=[
                OperationalContextSource(
                    source_id="dry_run_google_workspace_context",
                    title="Dry-run Google Workspace context boundary",
                    source_type="dry_run_context",
                    location="local KBA CLI",
                    note=(
                        "Useful references require live Workspace reads; this dry-run proves "
                        "route, topic focus, no-write boundaries, and output shape."
                    ),
                )
            ],
            diagnostics=[
                OperationalContextEntry(key="dry_run", value="true"),
                OperationalContextEntry(key="objective", value=objective),
            ],
        )
    if route == "zotero_context_agent":
        topic_terms = _zotero_context_topic_terms(objective)
        validation_focus = _zotero_context_is_validation_collection_request(topic_terms, objective)
        topic_focus = ", ".join(topic_terms[:4]) if topic_terms else "the requested Zotero topic"
        normalized_objective = " ".join(objective.lower().split())
        pdf_attachment_requested = bool(
            "pdf" in normalized_objective
            and re.search(r"\b(?:add|attach|upload|link)\b", normalized_objective)
        )
        ordinary_note_requested = bool(
            re.search(r"\bnotes?\b", normalized_objective)
            and re.search(
                r"\b(?:add|append|change|create|delete|edit|modify|remove|update|write)\b",
                normalized_objective,
            )
            and "kba_test_note" not in normalized_objective
        )
        mutation_blockers: list[str] = []
        if pdf_attachment_requested:
            mutation_blockers.append(
                "Ordinary Zotero PDF attachment is not supported by the current tool "
                "contract. Add a bounded exact-item PDF attachment tool with approval, "
                "file validation, provider read-back, and exact child-item verification."
            )
        if ordinary_note_requested:
            mutation_blockers.append(
                "Ordinary Zotero child-note creation or modification is not supported by "
                "the current tool contract. Add a bounded exact-item note tool with "
                "approval, version preconditions, and provider read-back verification."
            )
        mutation_summary = (
            "The LLM/manual plan correctly selected Zotero Context as the source owner and "
            "classified this as a business-system write. The requested mutation is blocked "
            "at the tool boundary, not at natural-language interpretation. "
            if mutation_blockers
            else ""
        )
        return ZoteroContextResult(
            mode="deterministic",
            summary=mutation_summary
            + (
                (
                    "Zotero read-only context dry-run recognized the behavioral-health AI "
                    "validation collection criteria, including title, authors, year, DOI, "
                    "URL, validation evidence, measurement-based care, implementation "
                    "science, source-quality caveats, citation gaps, and next verification "
                    "steps. "
                )
                if validation_focus
                else (
                    "Zotero read-only context dry-run recognized the requested focus on "
                    f"{topic_focus}. "
                )
            )
            + (
                "Local item-level titles, authors, years, DOIs, URLs, full text, and "
                "collection membership were not read in this dry-run; use the live/local "
                "Zotero context path before citing specific sources."
            ),
            library_context="KNI Zotero collections in the Keystone research library",
            collection_hints=[
                *(["behavioral-health AI validation"] if validation_focus else []),
                *(topic_terms[:4] or ["requested Zotero topic"]),
                "foundational reviews",
                "psychiatry background literature",
            ],
            relevant_evidence=[
                "Use collection metadata before citing individual items.",
                "Treat item-level bibliographic facts as unverified until local Zotero records are read.",
                "Flag missing full text, citation gaps, and source-quality caveats before external use.",
            ],
            recommended_artifact_plan=OperationalWritePlan(
                target_system="google_workspace",
                operation="chief_owned_research_summary_after_approval",
                target="internal evidence packet",
                scope="Zotero collection criteria and citation-gap summary",
                rationale="Zotero specialist is read-only and never mutates library items.",
            ),
            recommended_actions=[
                "Search collection metadata before citing items.",
                "Return collection and item identifiers only after local Zotero records are read.",
                "Keep this pass read-only and preserve citation-gap caveats.",
                *(
                    [
                        "Implement and validate the missing exact-item Zotero mutation tool "
                        "before retrying this provider write."
                    ]
                    if mutation_blockers
                    else []
                ),
            ],
            blockers=mutation_blockers,
            approval_needs=[
                "Scoped approval before creating any Workspace artifact from Zotero context"
            ],
            human_work_context=HumanWorkContext(
                work_functions=["research evidence review", "eval source quality"],
                human_owner_hint="Chief of Staff",
                decision_needed="Confirm which evidence packet should use the Zotero criteria.",
                handoff_ready_context=["collection criteria", "citation gaps"],
                missing_context=["live/local item-level Zotero evidence"],
                integration_surfaces=["Zotero", "Google Workspace"],
                follow_up_actions=["verify citations", "request artifact approval if needed"],
            ),
            sources=[
                OperationalContextSource(
                    source_id="dry_run_zotero_context",
                    title="Dry-run Zotero context boundary",
                    source_type="dry_run_context",
                    location="local KBA CLI",
                    note=(
                        "Useful references require local item reads; this dry-run proves "
                        "route, topic focus, no-write boundaries, and output shape."
                    ),
                )
            ],
            diagnostics=[
                OperationalContextEntry(key="dry_run", value="true"),
                OperationalContextEntry(key="objective", value=objective),
            ],
        )
    if route == "rss_context_agent":
        topic_terms = _feed_context_topic_terms(objective)
        topic_focus = (
            ", ".join(topic_terms[:4]) if topic_terms else "the requested announcement topic"
        )
        return RssContextResult(
            mode="deterministic",
            summary=(
                "RSS/#announcements read-only context: use recent announcement history to "
                f"inspect {topic_focus}; identify recurring operational themes, partnership signals, clinical "
                "validation hooks, and follow-up monitoring queries. No feed item is "
                "published, posted, edited, or written externally in dry-run mode."
            ),
            query=objective,
            frontier_summary=(
                "Dry-run context is suitable for routing and handoff checks for "
                f"{topic_focus}; use live SDK plus the announcement-history tool for "
                "source-specific item summaries."
            ),
            recurring_themes=[
                *topic_terms[:4],
                "AI policy and operational readiness",
                "payer/provider workflow signals",
                "clinical validation and implementation evidence",
            ],
            evidence_gaps=[
                "Evidence gaps remain because dry-run mode does not read item-level announcement titles, dates, URLs, or extracted page text."
            ],
            opportunity_signals=[
                "Potential internal monitoring topics should be confirmed against retrieved feed items."
            ],
            future_directions=[
                "Run the live RSS context agent when item-level titles, dates, and source URLs are needed."
            ],
            recommended_actions=[
                "Keep the pass read-only.",
                "Return Answer and Detailed Summary separately.",
                "Escalate to Chief of Staff only after item-level evidence is available.",
            ],
            approval_needs=[
                "Separate approval is required before creating Workspace artifacts or posting summaries."
            ],
            sources=[
                OperationalContextSource(
                    source_id="dry_run_rss_context",
                    title="Dry-run RSS context boundary",
                    source_type="dry_run_context",
                    location="local KBA CLI",
                    note=(
                        "Useful references require live item reads; this dry-run proves "
                        "route, topic focus, no-write boundaries, and output shape."
                    ),
                )
            ],
            human_work_context=HumanWorkContext(
                work_functions=["announcement monitoring", "operations context"],
                human_owner_hint="Chief of Staff",
                decision_needed="Confirm whether item-level RSS evidence is needed.",
                handoff_ready_context=["theme scan", "monitoring-query suggestions"],
                missing_context=[],
                integration_surfaces=["RSS", "Slack announcements"],
                follow_up_actions=["run live context read if evidence is needed"],
            ),
            diagnostics=[
                OperationalContextEntry(key="dry_run", value="true"),
                OperationalContextEntry(key="objective", value=objective),
            ],
        )
    if route == "preprints_context_agent":
        topic_terms = _feed_context_topic_terms(objective)
        topic_focus = ", ".join(topic_terms[:4]) if topic_terms else "the requested preprint topic"
        return PreprintsContextResult(
            mode="deterministic",
            summary=(
                "Preprints/#knowledge-hub read-only context: use recent preprint history "
                f"to inspect {topic_focus}; identify research themes, psychiatry or clinical AI frontiers, evidence "
                "gaps, and monitoring directions. Dry-run mode does not fetch, publish, "
                "post, save, or modify any external artifact."
            ),
            query=objective,
            frontier_summary=(
                "Dry-run context is suitable for route validation for "
                f"{topic_focus}; use live SDK plus the preprint-history tool for "
                "item-level paper context."
            ),
            recurring_themes=[
                *topic_terms[:4],
                "clinical AI validation methods",
                "psychiatry and behavioral-health evidence",
                "measurement and implementation gaps",
            ],
            research_frontiers=[
                "Source-backed preprint synthesis requires retrieved item titles, dates, and URLs."
            ],
            evidence_gaps=[
                "Preliminary evidence remains unverified until item-level preprint titles, dates, URLs, and source text are read.",
                "No item-level preprint evidence was read in dry-run mode.",
            ],
            future_directions=[
                "Run the live preprints context agent for bounded paper/item summaries."
            ],
            recommended_actions=[
                "Keep the pass read-only.",
                "Separate Answer, Detailed Summary, and Useful references in Slack output.",
                "Ask for live item reads before using this externally.",
            ],
            approval_needs=[
                "Separate approval is required before creating evidence packets or Workspace artifacts."
            ],
            sources=[
                OperationalContextSource(
                    source_id="dry_run_preprints_context",
                    title="Dry-run preprints context boundary",
                    source_type="dry_run_context",
                    location="local KBA CLI",
                    note=(
                        "Useful references require live item reads; this dry-run proves "
                        "route, topic focus, preliminary-evidence caveats, and output shape."
                    ),
                )
            ],
            human_work_context=HumanWorkContext(
                work_functions=["research monitoring", "evidence triage"],
                human_owner_hint="Chief of Staff",
                decision_needed="Confirm whether item-level preprint evidence is needed.",
                handoff_ready_context=["theme scan", "evidence-gap summary"],
                missing_context=["live item-level preprint evidence"],
                integration_surfaces=["preprint feeds", "knowledge hub"],
                follow_up_actions=["run live context read if evidence is needed"],
            ),
            diagnostics=[
                OperationalContextEntry(key="dry_run", value="true"),
                OperationalContextEntry(key="objective", value=objective),
            ],
        )
    return None


_FEED_CONTEXT_TOPIC_PHRASES = (
    "adolescent depression",
    "digital phenotyping",
    "wearable-sensor monitoring",
    "wearable sensor monitoring",
    "clinical AI validation",
    "remote monitoring implementation",
    "operations dashboard governance",
    "remote monitoring",
    "remote-monitoring adherence",
    "dashboard review",
    "digital psychiatry",
    "biomarkers",
    "depression measurement",
    "preliminary evidence",
    "physical-therapy",
    "exercise-adherence",
)
_ZOTERO_CONTEXT_TOPIC_PHRASES = (
    "psychiatric diagnosis",
    "foundational depression",
    "depression review",
    "depression reviews",
    "digital psychiatry",
    "psychiatry background",
    "biomarkers",
    "rdoc",
    "measurement-based care",
    "implementation science",
    "clinical AI validation",
)
_WORKSPACE_CONTEXT_TOPIC_PHRASES = (
    "internal onboarding",
    "operations sop",
    "operations sops",
    "new collaborator",
    "chief of staff handoff",
    "lightweight handoff",
    "artifact governance",
    "eval tracking",
    "review notes",
    "drive context",
)
_AIRTABLE_CONTEXT_TOPIC_PHRASES = (
    "2026 finance & tax tracker",
    "finance & tax tracker",
    "finance and tax tracker",
    "tax payments",
    "estimated tax period 2",
    "estimated tax periods",
    "period 2 payment records",
    "period 2 payments",
    "q2 rolling taxes summary",
    "rolling taxes summary",
    "payment evidence",
    "payment date",
)
_FEED_CONTEXT_STOPWORDS = frozenset(
    {
        "agent",
        "announcement",
        "announcements",
        "answer",
        "available",
        "bounded",
        "brief",
        "case",
        "concise",
        "context",
        "create",
        "detailed",
        "elsewhere",
        "diagnostic",
        "dry-run",
        "draft",
        "externally",
        "eval",
        "evidence",
        "external",
        "export",
        "feed",
        "feeds",
        "file",
        "files",
        "handoff",
        "historical",
        "history",
        "human-useful",
        "inspect",
        "local",
        "live",
        "monitoring",
        "mutate",
        "publish",
        "post",
        "preprint",
        "preprints",
        "recent",
        "read",
        "read-only",
        "refresh",
        "relevant",
        "research",
        "records",
        "return",
        "rss",
        "schedule",
        "send",
        "slack",
        "summary",
        "use",
        "web",
        "write",
    }
)


def _workspace_context_topic_terms(request_text: str) -> list[str]:
    lower = str(request_text or "").lower()
    terms: list[str] = []
    seen: set[str] = set()
    for phrase in _WORKSPACE_CONTEXT_TOPIC_PHRASES:
        phrase_lower = phrase.lower()
        if phrase_lower in lower and phrase not in seen:
            terms.append(phrase)
            seen.add(phrase)
    if len(terms) >= 2:
        return terms[:8]
    phrase_words = {
        word for phrase in terms for word in re.findall(r"[a-z][a-z0-9-]{2,}", phrase.lower())
    }
    workspace_stopwords = {
        "google",
        "workspace",
        "drive",
        "docs",
        "sheets",
        "browser",
        "automation",
        "records",
        "available",
        "lightweight",
    }
    for token in re.findall(r"[a-z][a-z0-9-]{5,}", lower):
        if token in phrase_words:
            continue
        if token in _FEED_CONTEXT_STOPWORDS or token in workspace_stopwords or token in seen:
            continue
        if token.startswith("diag_") or token.startswith("2026"):
            continue
        terms.append(token)
        seen.add(token)
        if len(terms) >= 8:
            break
    return terms[:8]


def _airtable_context_topic_terms(request_text: str) -> list[str]:
    lower = str(request_text or "").lower()
    terms: list[str] = []
    seen: set[str] = set()
    for phrase in _AIRTABLE_CONTEXT_TOPIC_PHRASES:
        phrase_lower = phrase.lower()
        if phrase_lower not in lower or phrase in seen:
            continue
        if any(phrase_lower in existing.lower() for existing in terms):
            continue
        terms = [existing for existing in terms if existing.lower() not in phrase_lower]
        terms.append(phrase)
        seen.add(phrase)
    if len(terms) >= 3:
        return terms[:8]
    phrase_words = {
        word for phrase in terms for word in re.findall(r"[a-z][a-z0-9-]{2,}", phrase.lower())
    }
    airtable_stopwords = {
        "airtable",
        "browser",
        "automation",
        "context",
        "diagnostic",
        "schema",
        "table",
        "tools",
        "available",
        "records",
        "record",
        "read",
        "read-only",
        "return",
        "concise",
        "summary",
        "references",
        "blockers",
        "caveats",
        "create",
        "update",
        "delete",
        "attach",
        "export",
        "write",
        "send",
        "schedule",
        "publish",
        "elsewhere",
    }
    for token in re.findall(r"[a-z][a-z0-9-]{4,}", lower):
        if token in phrase_words:
            continue
        if token in _FEED_CONTEXT_STOPWORDS or token in airtable_stopwords or token in seen:
            continue
        if token.startswith("diag_") or token.startswith("2026"):
            continue
        terms.append(token)
        seen.add(token)
        if len(terms) >= 8:
            break
    return terms[:8]


def _airtable_context_is_finance_tax_request(topic_terms: list[str], request_text: str) -> bool:
    haystack = " ".join([str(request_text or "").lower(), *[term.lower() for term in topic_terms]])
    finance_markers = ("finance & tax", "finance and tax", "tax payments", "estimated tax")
    return any(marker in haystack for marker in finance_markers)


def _airtable_context_is_eval_tracker_request(topic_terms: list[str], request_text: str) -> bool:
    haystack = " ".join([str(request_text or "").lower(), *[term.lower() for term in topic_terms]])
    return "eval_tracker" in haystack or "eval tracker" in haystack


def _airtable_context_summary(topic_terms: list[str], request_text: str) -> str:
    topic_focus = ", ".join(topic_terms[:4]) if topic_terms else "the requested Airtable context"
    expense_receipt_target = infer_finance_expense_receipt_target(request_text)
    if expense_receipt_target is not None:
        return (
            "Airtable read-only context dry-run inferred base alias "
            f"`{expense_receipt_target.base_alias}` and table "
            f"`{expense_receipt_target.table}` from the explicit Airtable expense receipt "
            "ask. It should not ask the operator to restate the base or table. Airtable "
            "schema, records, attachments, and receipt contents were not read in this "
            "dry-run; use the live Airtable/Chief path to match exact fields, derive "
            "`Estimated Tax Periods` from the receipt date, create the expense record, "
            "and attach the receipt after record identity and attachment field are known."
        )
    if _airtable_context_is_finance_tax_request(topic_terms, request_text):
        return (
            "Airtable read-only context dry-run recognized the requested focus on "
            f"{topic_focus}. Airtable schema, records, attachments, and payment evidence "
            "were not read in this dry-run; use the live Airtable context path before "
            "confirming Period 2 payments, amounts, dates, or the Q2 Rolling Taxes Summary."
        )
    if _airtable_context_is_eval_tracker_request(topic_terms, request_text):
        return (
            "Airtable read-only context dry-run recognized base alias eval_tracker and "
            "table Eval tracker. It returns schema mapping, record identity questions, "
            "approval needs, and no-write blockers for Promptfoo case id, Slack run id, "
            "human reviewer, missing evidence, and next follow-up. Schema, records, "
            "attachments, and field values were not read in this dry-run; use the live "
            "Airtable context path before relying on record data."
        )
    return (
        "Airtable read-only context dry-run recognized the requested Airtable lookup focus "
        f"on {topic_focus}. It returns schema mapping, record identity questions, approval "
        "needs, and no-write blockers. Schema, records, attachments, and field values were "
        "not read in this dry-run; use the live Airtable context path before relying on "
        "record data."
    )


def _airtable_context_field_hints(topic_terms: list[str], request_text: str) -> list[str]:
    expense_receipt_target = infer_finance_expense_receipt_target(request_text)
    if expense_receipt_target is not None:
        return finance_expense_receipt_field_hints(expense_receipt_target)
    if _airtable_context_is_finance_tax_request(topic_terms, request_text):
        return [
            "Payment Name",
            "Estimated Tax Periods",
            "Tax Type",
            "Amount",
            "Payment Date",
            "Notes",
            "Attachments",
        ]
    if _airtable_context_is_eval_tracker_request(topic_terms, request_text):
        return [
            "Promptfoo case id",
            "Slack run id",
            "human reviewer",
            "missing evidence",
            "next follow-up",
            "analysis inclusion",
            "record identifier",
        ]
    return ["requested schema fields", "record identifier", "status or note fields"]


def _airtable_context_record_identity(topic_terms: list[str], request_text: str) -> str:
    expense_receipt_target = infer_finance_expense_receipt_target(request_text)
    if expense_receipt_target is not None:
        return (
            "This is a create operation, so there is no existing target record id. "
            "Resolve record identity from the record created by `airtable_write_record`; "
            "then use that record id for the receipt attachment upload."
        )
    if _airtable_context_is_finance_tax_request(topic_terms, request_text):
        return (
            "Filter Tax Payments records where Estimated Tax Periods equals 2; include "
            "IRS/PA Period 2 payment records and the Q2 Rolling Taxes Summary if present."
        )
    if _airtable_context_is_eval_tracker_request(topic_terms, request_text):
        return (
            "Confirm the Eval tracker record identity by Promptfoo case id plus Slack run id "
            "before reading or proposing any Airtable update."
        )
    return "Confirm base, table, primary field, and record filter before reading records."


def _airtable_context_work_functions(topic_terms: list[str], request_text: str) -> list[str]:
    if infer_finance_expense_receipt_target(request_text) is not None:
        return ["finance operations context", "expense receipt processing"]
    if _airtable_context_is_finance_tax_request(topic_terms, request_text):
        return ["finance operations context", "tax payment record review"]
    return ["structured record context", "Airtable schema review"]


def _workspace_context_folder_hints(topic_terms: list[str]) -> list[str]:
    if any("onboarding" in term or "sop" in term or "collaborator" in term for term in topic_terms):
        return ["KNIOps", "KNIOps/Operations", "KNIOps/Communication"]
    if any("eval" in term or "review" in term for term in topic_terms):
        return ["KNI Ops / Evals", "KNIOps", "KNIOps/Artifacts"]
    return ["KNIOps", "candidate scoped Drive folder"]


def _workspace_context_doc_hints(topic_terms: list[str]) -> list[str]:
    if any("onboarding" in term or "sop" in term or "collaborator" in term for term in topic_terms):
        return ["onboarding or operations SOP Doc", "collaborator handoff Doc"]
    if any("eval" in term or "review" in term for term in topic_terms):
        return ["Slack eval review narrative", "artifact notes Doc"]
    return ["candidate context Doc"]


def _workspace_context_sheet_hints(topic_terms: list[str]) -> list[str]:
    if any("eval" in term or "tracking" in term for term in topic_terms):
        return ["Eval tracker Sheet", "Sheet tab"]
    if any("onboarding" in term or "sop" in term or "collaborator" in term for term in topic_terms):
        return ["collaborator tracker Sheet if available"]
    return ["candidate tracker Sheet if relevant"]


def _workspace_context_recommended_target(topic_terms: list[str]) -> str:
    if any("onboarding" in term or "sop" in term or "collaborator" in term for term in topic_terms):
        return "KNIOps Operations onboarding/SOP context"
    if any("eval" in term or "review" in term for term in topic_terms):
        return "KNI Ops / Evals artifact context"
    return "scoped Google Workspace context"


def _workspace_context_work_functions(topic_terms: list[str]) -> list[str]:
    if any("onboarding" in term or "sop" in term or "collaborator" in term for term in topic_terms):
        return ["collaborator onboarding", "operations handoff"]
    if any("eval" in term or "review" in term for term in topic_terms):
        return ["artifact governance", "eval reporting"]
    return ["workspace context review", "internal handoff"]


def _workspace_context_is_eval_artifact_request(topic_terms: list[str], request_text: str) -> bool:
    haystack = " ".join([str(request_text or "").lower(), *[term.lower() for term in topic_terms]])
    return (
        "kni ops / evals" in haystack
        or "slack eval review narrative" in haystack
        or "eval tracker sheet" in haystack
        or ("eval" in haystack and "artifact" in haystack)
    )


def _zotero_context_topic_terms(request_text: str) -> list[str]:
    lower = str(request_text or "").lower()
    terms: list[str] = []
    seen: set[str] = set()
    for phrase in _ZOTERO_CONTEXT_TOPIC_PHRASES:
        phrase_lower = phrase.lower()
        if phrase_lower in lower and phrase not in seen:
            terms.append(phrase)
            seen.add(phrase)
    if len(terms) >= 2:
        return terms[:8]
    phrase_words = {
        word for phrase in terms for word in re.findall(r"[a-z][a-z0-9-]{2,}", phrase.lower())
    }
    for token in re.findall(r"[a-z][a-z0-9-]{5,}", lower):
        if token in phrase_words:
            continue
        if token in _FEED_CONTEXT_STOPWORDS or token in seen:
            continue
        if token in {
            "zotero",
            "library",
            "material",
            "support",
            "background",
            "import",
            "collections",
            "collection",
            "download",
            "downloads",
            "pdfs",
            "workspace",
            "artifacts",
        }:
            continue
        if token.startswith("diag_") or token.startswith("2026"):
            continue
        terms.append(token)
        seen.add(token)
        if len(terms) >= 8:
            break
    return terms[:8]


def _zotero_context_is_validation_collection_request(
    topic_terms: list[str], request_text: str
) -> bool:
    haystack = " ".join([str(request_text or "").lower(), *[term.lower() for term in topic_terms]])
    return (
        "behavioral-health ai validation" in haystack
        or ("validation" in haystack and "collection" in haystack)
        or ("title" in haystack and "doi" in haystack and "url" in haystack)
    )


def _feed_context_topic_terms(request_text: str) -> list[str]:
    lower = str(request_text or "").lower()
    terms: list[str] = []
    seen: set[str] = set()
    for phrase in _FEED_CONTEXT_TOPIC_PHRASES:
        phrase_lower = phrase.lower()
        if phrase_lower not in lower or phrase in seen:
            continue
        if any(phrase_lower in existing.lower() for existing in terms):
            continue
        if phrase.lower() in lower and phrase not in seen:
            terms.append(phrase)
            seen.add(phrase)
    if len(terms) >= 3:
        return terms[:8]
    phrase_words = {
        word for phrase in terms for word in re.findall(r"[a-z][a-z0-9-]{2,}", phrase.lower())
    }
    for token in re.findall(r"[a-z][a-z0-9-]{5,}", lower):
        if token in phrase_words:
            continue
        if token in _FEED_CONTEXT_STOPWORDS or token in seen:
            continue
        if token.startswith("diag_") or token.startswith("2026"):
            continue
        terms.append(token)
        seen.add(token)
        if len(terms) >= 8:
            break
    return terms[:8]


def _context_agent_request_is_blocked(input_text: str) -> bool:
    lowered = input_text.lower()
    return any(
        marker in lowered
        for marker in (
            "user says update",
            "user asks to save",
            "no base",
            "no folder id",
            "no document id",
            "no sheet tab",
            "no approval reference",
            "no sharing scope",
        )
    )


def _context_agent_output_has_blockers(output: object) -> bool:
    return bool(output and isinstance(output, dict) and output.get("blockers"))


def _context_agent_blocker_messages(output: object) -> list[dict[str, str]]:
    if not isinstance(output, dict):
        return []
    messages: list[dict[str, str]] = []
    for item in output.get("blockers") or []:
        message = (
            str(item.get("message") or "").strip()
            if isinstance(item, dict)
            else str(item or "").strip()
        )
        if message:
            messages.append({"message": message})
    return messages


def _context_agent_public_blockers(
    output: object,
    *,
    execution_blocker: str = "",
) -> list[dict[str, str]]:
    """Return stable public blocker objects without hashing nested mappings."""

    candidates = _context_agent_blocker_messages(output)
    if execution_blocker.strip():
        candidates.append({"message": execution_blocker.strip()})
    messages = list(
        dict.fromkeys(
            str(item.get("message") or "").strip()
            for item in candidates
            if str(item.get("message") or "").strip()
        )
    )
    return [{"message": message} for message in messages]


def _context_agent_blocked_write_attempts(
    output: object,
    *,
    tool_receipts: list[dict[str, object]] | None = None,
) -> list[str]:
    if not isinstance(output, dict):
        return []
    agent_name = str(output.get("agent_name") or "")
    blockers = [str(item) for item in output.get("blockers") or [] if str(item)]
    if blockers:
        return [f"{agent_name}: blocked write request pending exact target and approval"]
    failed_receipts = [
        receipt
        for receipt in list(tool_receipts or [])
        if str(receipt.get("status") or "").lower()
        in {"blocked", "error", "failed", "verification_failed"}
        and _context_agent_receipt_is_write(receipt)
    ]
    if failed_receipts:
        return [f"{agent_name}: provider write or cleanup did not verify"]
    return []


def _strict_requested_display_text(
    output: object,
    manual_plan: ManualRequestPlan | dict[str, object] | None,
) -> str:
    """Render exact narrow asks without adding generic workflow sections."""

    if not isinstance(output, dict) or not manual_plan:
        return ""
    plan = (
        manual_plan.model_dump(mode="json")
        if isinstance(manual_plan, ManualRequestPlan)
        else manual_plan
    )
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    canonical_plan = authority.plan if authority.canonical else None
    ask_shape = plan.get("ask_shape")
    if not isinstance(ask_shape, dict):
        return ""
    strict_mode = str(ask_shape.get("strict_filter_mode") or "") in {"exact", "strict"}
    stop_condition = str(ask_shape.get("stop_condition") or "").lower()
    output_form = str(ask_shape.get("output_form") or "")
    objective_text = " ".join(
        (
            str(plan.get("objective") or ""),
            stop_condition,
            *[str(item or "") for item in plan.get("required_terms") or []],
        )
    ).lower()
    draft_text = str(output.get("draft_reply") or output.get("draft_reply_summary") or "").strip()
    requests_copyable_draft = bool(
        draft_text
        and (output_form == "draft" or re.search(r"\b(?:draft|write|prepare)\b", objective_text))
        and re.search(r"\b(?:reply|response)\b", objective_text)
    )
    precise_output_boundary = bool(stop_condition and output_form in {"brief", "bullets"})
    if not strict_mode and not precise_output_boundary and not requests_copyable_draft:
        return ""

    if output.get("blockers") or output.get("approval_needs") or output.get("evidence_gaps"):
        requested_zotero_identity = (
            {"title", "authors", "publication_title"}.issubset(
                set(canonical_plan.zotero_requested_fields)
            )
            if canonical_plan is not None
            else all(
                re.search(pattern, str(plan.get("objective") or "").lower())
                for pattern in (
                    r"\btitle\b",
                    r"\bauthors?\b",
                    r"\bpublication\s+title\b",
                )
            )
        )
        diagnostic_values = {
            str(item.get("key") or "").strip(): str(item.get("value") or "").strip()
            for item in output.get("diagnostics") or []
            if isinstance(item, dict) and str(item.get("key") or "").strip()
        }
        verified_identity_fields = bool(
            output.get("article_titles")
            and diagnostic_values.get("authors")
            and diagnostic_values.get("publication_title")
        )
        if not (requested_zotero_identity and verified_identity_fields):
            return ""

    preview_lines = [
        str(item).strip()
        for item in output.get("artifact_preview_lines") or []
        if str(item).strip()
    ]
    if output_form == "bullets" and preview_lines:
        return "\n".join(
            line if re.match(r"^[-*]\s+", line) else f"- {line}" for line in preview_lines
        )

    titles = [str(item).strip() for item in output.get("article_titles") or [] if str(item).strip()]
    item_keys = [
        str(item).strip() for item in output.get("zotero_item_keys") or [] if str(item).strip()
    ]
    output_constraints = ask_shape.get("output_constraints")
    constraint_interpretation = (
        str(output_constraints.get("interpretation") or "")
        if isinstance(output_constraints, dict)
        else ""
    )
    if canonical_plan is not None:
        requested_zotero_fields = set(canonical_plan.zotero_requested_fields)
    else:
        requested_field_text = " ".join(
            [
                str(plan.get("objective") or ""),
                constraint_interpretation,
                *[str(item or "") for item in plan.get("required_terms") or []],
            ]
        ).lower()
        requested_zotero_fields = {
            field
            for field, pattern in (
                ("title", r"\btitle\b"),
                ("authors", r"\bauthors?\b"),
                ("publication_title", r"\bpublication\s+title\b"),
            )
            if re.search(pattern, requested_field_text)
        }
    if titles and {"title", "authors", "publication_title"}.issubset(
        requested_zotero_fields
    ):
        diagnostic_values = {
            str(item.get("key") or "").strip(): str(item.get("value") or "").strip()
            for item in output.get("diagnostics") or []
            if isinstance(item, dict) and str(item.get("key") or "").strip()
        }
        return "\n".join(
            (
                f"Title: {titles[0]}",
                "Authors: "
                + (diagnostic_values.get("authors") or "Unavailable in Zotero metadata"),
                "Publication title: "
                + (diagnostic_values.get("publication_title") or "Unavailable in Zotero metadata"),
            )
        )
    if "one" in stop_condition and "zotero" in stop_condition and titles and item_keys:
        return f"Title: {titles[0]}\nItem key: {item_keys[0]}"
    if (
        titles
        and requested_zotero_fields == {"title"}
        and (canonical_plan is not None or "only" in stop_condition)
    ):
        return titles[0]

    summary = str(output.get("answer") or output.get("summary") or "").strip()
    if not summary:
        facts = output.get("facts")
        if isinstance(facts, list):
            for fact in facts:
                if isinstance(fact, dict) and str(fact.get("text") or "").strip():
                    summary = str(fact["text"]).strip()
                    break
    if not summary:
        summary = str(output.get("why_it_matters") or output.get("product") or "").strip()
    if output_form in {"brief", "draft"} and (summary or draft_text):
        if requests_copyable_draft:
            assessment_requested = bool(
                re.search(
                    r"\b(?:assess|decide|explain|tell\s+me)\b[^.]{0,80}"
                    r"\b(?:whether|what|why|merit|warrant|reply|response)\b"
                    r"|\b(?:merits?|warrants?|needs?|requires?)\b[^.]{0,40}"
                    r"\b(?:reply|response)\b",
                    objective_text,
                )
            )
            assessment = (
                str(output.get("reasoning") or "").strip() if assessment_requested else summary
            )
            if not assessment:
                assessment = summary
            return "\n\n".join(
                item
                for item in (
                    assessment,
                    f"*Draft response:*\n{draft_text}",
                )
                if item
            )
        if "reply" in stop_condition and isinstance(output.get("needs_reply"), bool):
            reply = "Yes" if output["needs_reply"] else "No"
            return f"{summary}\nReply needed: {reply}."
        if titles and "title" in requested_zotero_fields:
            return f"Title: {titles[0]}\nSummary: {summary}"
        return summary
    return ""


def _context_agent_human_summary(
    output: object,
    manual_plan: ManualRequestPlan | dict[str, object] | None = None,
) -> str:
    if not isinstance(output, dict):
        return ""
    strict_display = _strict_requested_display_text(output, manual_plan)
    if strict_display:
        return strict_display
    summary = str(output.get("answer") or output.get("summary") or "").strip()
    approval_needs = [
        str(item).strip() for item in output.get("approval_needs") or [] if str(item).strip()
    ]
    blockers = [str(item).strip() for item in output.get("blockers") or [] if str(item).strip()]
    blockers.extend(
        str(item).strip() for item in output.get("evidence_gaps") or [] if str(item).strip()
    )
    parts: list[str] = []
    if summary:
        parts.append(f"*Answer:*\n{summary}")
    detail_lines: list[str] = []
    record_lines = _context_agent_record_summary_lines(output)
    if record_lines:
        detail_lines.append("- Records visible:")
        detail_lines.extend(record_lines)
    feed_item_lines = _context_agent_feed_item_summary_lines(output)
    if feed_item_lines:
        detail_lines.append("- Items reviewed:")
        detail_lines.extend(feed_item_lines)
    airtable_context_lines = _context_agent_airtable_context_lines(output)
    if airtable_context_lines:
        detail_lines.extend(airtable_context_lines)
    workspace_context_lines = _context_agent_workspace_context_lines(output)
    if workspace_context_lines:
        detail_lines.extend(workspace_context_lines)
    zotero_context_lines = _context_agent_zotero_context_lines(output)
    if zotero_context_lines:
        detail_lines.extend(zotero_context_lines)
    theme_lines = _context_agent_theme_summary_lines(output)
    if theme_lines:
        detail_lines.append("- Themes:")
        detail_lines.extend(theme_lines)
    reference_lines = _context_agent_reference_summary_lines(output)
    if approval_needs:
        if len(approval_needs) == 1:
            detail_lines.append(
                f"- Approval/write boundary: {_ensure_terminal_punctuation(approval_needs[0])}"
            )
        else:
            detail_lines.append("- Approval/write boundary:")
            detail_lines.extend(f"  - {item}" for item in approval_needs)
    if blockers:
        if len(blockers) == 1:
            detail_lines.append(f"- Needs attention: {_ensure_terminal_punctuation(blockers[0])}")
        else:
            detail_lines.append("- Needs attention:")
            detail_lines.extend(f"  - {item}" for item in blockers)
    if detail_lines:
        parts.append("*Detailed Summary:*\n" + "\n".join(detail_lines))
    if reference_lines:
        parts.append("*Useful references:*\n" + "\n".join(reference_lines))
    return "\n\n".join(part for part in parts if part)


def _ensure_terminal_punctuation(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return "."
    if text[-1] in ".!?":
        return text
    return f"{text}."


def _context_agent_reference_summary_lines(output: dict[str, object]) -> list[str]:
    lines: list[str] = []
    sources = output.get("sources")
    if isinstance(sources, list):
        for source in sources[:5]:
            if not isinstance(source, dict):
                continue
            title = str(source.get("title") or "").strip()
            note = str(source.get("note") or "").strip()
            location = str(source.get("location") or "").strip()
            if not title:
                continue
            label = title
            if note:
                label = f"{label} - {note}"
            if location and not _contains_internal_provider_identity(location):
                label = f"{label} ({location})"
            lines.append(f"  - {label}")
    article_refs = _context_agent_article_reference_lines(output)
    existing = {line.lower() for line in lines}
    for line in article_refs:
        if line.lower() not in existing:
            lines.append(line)
            existing.add(line.lower())
        if len(lines) >= 5:
            break
    return lines[:5]


def _context_agent_record_summary_lines(output: dict[str, object]) -> list[str]:
    entries = output.get("record_summaries")
    if not isinstance(entries, list):
        return []
    lines: list[str] = []
    for entry in entries[:5]:
        if isinstance(entry, dict):
            key = str(entry.get("key") or "").strip()
            value = str(entry.get("value") or "").strip()
            note = str(entry.get("note") or "").strip()
        else:
            key = ""
            value = str(entry or "").strip()
            note = ""
        if not value and not key:
            continue
        visible_key = "" if _contains_internal_provider_identity(key) else key
        label = f"{visible_key}: {value}" if visible_key and value else visible_key or value
        if note:
            label = f"{label} ({note})"
        lines.append(f"  - {label}")
    return lines


def _context_agent_feed_item_summary_lines(output: dict[str, object]) -> list[str]:
    articles = output.get("articles")
    if not isinstance(articles, list):
        return []
    lines: list[str] = []
    for article in articles[:5]:
        if not isinstance(article, dict):
            continue
        title = str(article.get("title") or "").strip()
        summary = str(
            article.get("summary")
            or article.get("detailed_summary")
            or article.get("selection_reason")
            or ""
        ).strip()
        source = str(article.get("published_at") or article.get("source") or "").strip()
        if not title and not summary:
            continue
        label = f"{title}: {summary}" if title and summary else title or summary
        if source:
            label = f"{label} ({source})"
        lines.append(f"  - {label}")
    return lines


def _context_agent_airtable_context_lines(output: dict[str, object]) -> list[str]:
    base_alias = str(output.get("base_alias") or "").strip()
    tables = [
        str(item).strip() for item in output.get("relevant_tables") or [] if str(item).strip()
    ][:5]
    fields = [
        str(item).strip() for item in output.get("relevant_fields") or [] if str(item).strip()
    ][:8]
    record_identity = str(output.get("recommended_record_identity") or "").strip()
    lines: list[str] = []
    if base_alias or tables:
        target_parts: list[str] = []
        if base_alias:
            target_parts.append(base_alias)
        if tables:
            target_parts.append(", ".join(tables))
        lines.append(f"- Airtable target hints: {' / '.join(target_parts)}")
    if fields:
        lines.append("- Field hints:")
        lines.extend(f"  - {item}" for item in fields)
    if record_identity and not _contains_internal_provider_identity(record_identity):
        lines.append(f"- Record filter guidance: {record_identity}")
    return lines


def _contains_internal_provider_identity(value: object) -> bool:
    text = str(value or "")
    return bool(re.search(r"(?:^|[\s/(])(?:rec|app)[A-Za-z0-9]{8,}(?:$|[\s/)])", text))


def _context_agent_workspace_context_lines(output: dict[str, object]) -> list[str]:
    folders = [
        str(item).strip() for item in output.get("relevant_folders") or [] if str(item).strip()
    ][:5]
    docs = [str(item).strip() for item in output.get("relevant_docs") or [] if str(item).strip()][
        :5
    ]
    sheets = [
        str(item).strip() for item in output.get("relevant_sheets") or [] if str(item).strip()
    ][:5]
    target = str(output.get("recommended_target") or "").strip()
    preview_lines = [
        str(item).strip()
        for item in output.get("artifact_preview_lines") or []
        if str(item).strip()
    ][:12]
    lines: list[str] = []
    if preview_lines:
        lines.append("- Draft artifact preview:")
        lines.extend(f"  - {item}" for item in preview_lines)
    if target:
        lines.append(f"- Candidate target: {target}")
    if folders:
        lines.append("- Folder hints:")
        lines.extend(f"  - {item}" for item in folders)
    if docs:
        lines.append("- Doc hints:")
        lines.extend(f"  - {item}" for item in docs)
    if sheets:
        lines.append("- Sheet hints:")
        lines.extend(f"  - {item}" for item in sheets)
    return lines


def _context_agent_zotero_context_lines(output: dict[str, object]) -> list[str]:
    hints = [
        str(item).strip() for item in output.get("collection_hints") or [] if str(item).strip()
    ][:5]
    evidence = [
        str(item).strip() for item in output.get("relevant_evidence") or [] if str(item).strip()
    ][:5]
    lines: list[str] = []
    if hints:
        lines.append("- Collection/context hints:")
        lines.extend(f"  - {item}" for item in hints)
    if evidence:
        lines.append("- Evidence guidance:")
        lines.extend(f"  - {item}" for item in evidence)
    return lines


def _context_agent_theme_summary_lines(output: dict[str, object]) -> list[str]:
    themes = output.get("recurring_themes")
    if not isinstance(themes, list):
        return []
    return [f"  - {str(theme).strip()}" for theme in themes[:5] if str(theme).strip()]


def _context_agent_article_reference_lines(output: dict[str, object]) -> list[str]:
    articles = output.get("articles")
    if not isinstance(articles, list):
        return []
    lines: list[str] = []
    for article in articles[:5]:
        if not isinstance(article, dict):
            continue
        title = str(article.get("title") or "").strip()
        url = str(article.get("url") or "").strip()
        if title and url:
            lines.append(f"  - {title} ({url})")
    return lines


def _run_ask_specialist_live(
    route: str,
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None = None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    sdk_session_spec: SDKSessionSpec | None = None,
    execution_context: dict[str, Any] | None = None,
    context_file_path: str = "",
    cost_tracking_requested: bool = False,
    database_url: str | None = None,
) -> int:
    load_settings(force_dotenv=True)
    if _should_run_direct_supplied_response(
        input_text,
        requested_route=route,
        manual_plan=manual_plan,
        provider_free_composition_allowed=bool(
            orchestrator_preflight
            and orchestrator_preflight.composition_admission.composition_allowed
        ),
    ):
        return _run_direct_supplied_context_response_live(
            route,
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            database_url=database_url,
        )
    if route == "business_research_analyst":
        return _run_ask_company_research_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            context_file_path=context_file_path,
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    elif route == "chief_of_staff":
        return _run_ask_chief_of_staff_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            context_file_path=context_file_path,
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    elif route == "opportunity_scout":
        return _run_ask_opportunity_scout_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            context_file_path=context_file_path,
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    elif route == "outreach_composer":
        return _run_ask_outreach_composer_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            context_file_path=context_file_path,
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    elif route == "gmail_triage":
        return _run_ask_gmail_triage_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            context_file_path=context_file_path,
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    elif route in CONTEXT_AGENT_ROUTES:
        return _run_ask_context_agent_live(
            route,
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    else:
        raise SystemExit(f"Unsupported agent route: {route}")


def _run_direct_supplied_context_response_live(
    route: str,
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    database_url: str | None,
    execution_context: dict[str, Any] | None = None,
) -> int:
    """Run the explicitly named specialist once without tools or domain blockers."""

    if has_explicit_local_attachment_context(input_text):
        attachment_bundle = local_file_input_bundle_from_text(input_text)
        if not attachment_bundle.has_inputs:
            return _print_local_attachment_unavailable(
                route,
                input_text,
                attachment_bundle.diagnostics,
                json_output=json_output,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
            )

    agent = build_direct_supplied_response_agent(
        route,
        request_text=input_text,
        manual_request_plan=manual_plan,
    )
    bounded_context = specialist_execution_context_text(execution_context)
    typed_input = DirectAgentResponseInput(
        requested_agent=route,
        original_request=input_text,
        selected_context=bounded_context,
        output_constraints=manual_plan.ask_shape.output_constraints.model_dump(mode="json"),
    )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=DirectAgentResponse,
        live=True,
        session=build_sdk_session(sdk_session_spec) if sdk_session_spec else None,
        workflow_name=f"keystone.ask.{route}.direct_response",
        trace_metadata={
            "agent": route,
            "entrypoint": "cli.ask",
            "mode": "live_sdk_direct_response",
            "tool_admission": "none",
        },
        max_turns=1,
    )
    bounded_evidence = "\n\n".join(part for part in (input_text, bounded_context) if part)
    instruction_resolution = resolve_instruction_following_response(
        result.output.answer,
        original_request=input_text,
        manual_plan=manual_plan,
        bounded_evidence=bounded_evidence,
        live=True,
    )
    passed = instruction_resolution.validation.passed
    answer = (
        instruction_resolution.response_text
        if passed
        else instruction_following_blocker_text(instruction_resolution.validation)
    )
    payload: dict[str, Any] = {
        "mode": "live_sdk",
        "selected_agent": route,
        "agent_name": _agent_display_name(route),
        "input": input_text,
        "status": "completed" if passed else "blocked",
        "block_kind": "" if passed else "instruction_following_constraint_failed",
        "send_enabled": False,
        "completion_confirmed": passed,
        "manual_request_plan": manual_plan.model_dump(mode="json"),
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "output_type": "DirectAgentResponse",
        "output": {"answer": answer, "summary": answer, "send_enabled": False},
        "human_summary": answer,
        "slack_display_text": answer,
        "display_text": answer,
        "summary": answer,
        "instruction_following": instruction_resolution.metadata(),
        "usage": result.usage,
        "cost": result.cost,
        "budget_guard": result.budget_guard,
        "request_cache": result.request_cache,
        "model": {
            "provider": get_runtime_agent_model_config(
                getattr(agent, "name", route),
                model_override=getattr(agent, "model", None),
            ).provider,
            "name": str(getattr(agent, "model", "") or ""),
            "run_mode": "live_sdk",
        },
        "model_execution": {
            "provider": get_runtime_agent_model_config(
                getattr(agent, "name", route),
                model_override=getattr(agent, "model", None),
            ).provider,
            "model": str(getattr(agent, "model", "") or ""),
            "run_mode": "live_sdk",
            "usage_available": True,
            "cost_available": True,
        },
        "tool_admission": {
            "admitted": False,
            "tool_count": len(list(getattr(agent, "tools", []) or [])),
            "reason": "complete_provider_free_supplied_context",
        },
        "side_effects": {
            "external_write_performed": False,
            "email_sent": False,
            "slack_message_posted": False,
        },
    }
    _attach_slack_run_provenance(payload, execution_context)
    attach_execution_public_result(payload)
    try:
        run_id = SQLiteStore(database_url or database_url_from_env()).save_agent_run(
            agent_name=route,
            input_payload={"request_text": input_text, "route": route},
            input_summary=input_text[:500],
            output=payload,
            model=f"sdk-live:{getattr(agent, 'model', '') or route}",
            dry_run=False,
            status="success" if passed else "blocked",
        )
        payload["agent_run_id"] = run_id
        attach_execution_public_result(payload)
    except Exception as exc:  # pragma: no cover - diagnostic metadata only
        payload["agent_run_persistence_error"] = f"{type(exc).__name__}: {exc}"
    return _print_ask_live_payload(payload, json_output=json_output)


def _run_ask_context_agent_live(
    route: str,
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None = None,
    orchestrator_preflight: OrchestratorPreflight | None = None,
    sdk_session_spec: SDKSessionSpec | None = None,
    execution_context: dict[str, Any] | None = None,
    cost_tracking_requested: bool = False,
    database_url: str | None = None,
) -> int:
    provider_context, preacquired_receipts, provider_blocker = _direct_zotero_provider_preflight(
        route,
        input_text,
        manual_plan=manual_plan,
    )
    (
        airtable_projection_context,
        airtable_projection_receipts,
        airtable_projection_blocker,
    ) = _direct_airtable_result_scope_provider_preflight(
        route,
        manual_plan=manual_plan,
    )
    preacquired_receipts.extend(airtable_projection_receipts)
    if airtable_projection_context:
        provider_context = "\n\n".join(
            item for item in (provider_context, airtable_projection_context) if item
        )
    if airtable_projection_blocker:
        provider_blocker = airtable_projection_blocker
    airtable_receipt_context = _direct_airtable_receipt_provider_context(
        route,
        input_text,
        manual_plan=manual_plan,
        execution_context=execution_context,
    )
    if airtable_receipt_context:
        provider_context = "\n\n".join(
            item for item in (provider_context, airtable_receipt_context) if item
        )
    if provider_blocker:
        return _print_ask_clarification(
            route,
            input_text,
            provider_blocker,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            extra={
                "status": "blocked",
                "block_kind": (
                    "airtable_provider_projection_failed"
                    if airtable_projection_blocker
                    else "zotero_provider_read_failed"
                ),
                "tool_receipts": preacquired_receipts,
                "send_enabled": False,
            },
        )
    provider_write_admitted = _direct_context_agent_write_admitted(
        manual_plan,
        input_text=input_text,
        route=route,
    )
    spec = AGENT_REGISTRY[route]
    build_kwargs: dict[str, Any] = {
        "request_text": input_text,
        "tool_tier": _direct_context_agent_tool_tier(
            manual_plan,
            input_text=input_text,
            route=route,
        ),
        "compact_instructions": bool(
            _direct_specialist_runtime_profile(
                route,
                input_text=input_text,
                manual_plan=manual_plan,
            )["compact_instructions"]
        ),
    }
    if route in {
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    }:
        build_kwargs["manual_plan"] = manual_plan
    if route == "airtable_context_agent":
        if airtable_projection_context:
            # The exact provider set has already been re-read and verified.
            # Keep the specialist turn focused on synthesis instead of allowing
            # a second tool call to broaden or replace that set.
            build_kwargs["attach_tools"] = False
    if route == "zotero_context_agent" and provider_context:
        # Provider acquisition is already complete. Keep this synthesis turn
        # tool-free; later thread follow-ups rebuild the agent with its Zotero
        # read tools when they need new metadata.
        build_kwargs["attach_tools"] = False
    agent = spec.build_agent(**build_kwargs)
    model_config = get_runtime_agent_model_config(
        getattr(agent, "name", None),
        model_override=getattr(agent, "model", None),
    )
    turn_policy = resolve_sdk_turn_policy(
        route,
        request_text=input_text,
        live_search=False,
    )
    direct_turn_limit = _direct_specialist_request_estimate(
        route,
        input_text=input_text,
        live_search=False,
        manual_plan=manual_plan,
    )
    if direct_turn_limit < turn_policy.max_turns:
        turn_policy = SDKTurnPolicy(
            agent_name=turn_policy.agent_name,
            max_turns=direct_turn_limit,
            source=f"direct_single_action:{turn_policy.source}",
        )
    output_type = spec.resolve_output_schema()
    live_read_env_names = {
        "airtable_context_agent": AIRTABLE_LIVE_READS_ENV,
        "google_workspace_context_agent": GOOGLE_WORKSPACE_LIVE_READS_ENV,
    }
    live_read_env_name = live_read_env_names.get(route)
    previous_live_reads = os.environ.get(live_read_env_name) if live_read_env_name else None
    operator_approval_env = "KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE"
    previous_operator_approval = os.environ.get(operator_approval_env)
    previous_airtable_operation = os.environ.get(AIRTABLE_ALLOWED_OPERATION_ENV)
    airtable_allowed_operation = (
        _direct_airtable_allowed_operation(manual_plan, input_text=input_text)
        if route == "airtable_context_agent"
        else ""
    )
    if route == "airtable_context_agent" and provider_write_admitted:
        request_hash = hashlib.sha256(input_text.encode("utf-8")).hexdigest()[:16]
        os.environ[operator_approval_env] = f"airtable-direct:{request_hash}"
    if route == "airtable_context_agent":
        if airtable_allowed_operation:
            os.environ[AIRTABLE_ALLOWED_OPERATION_ENV] = airtable_allowed_operation
        else:
            os.environ.pop(AIRTABLE_ALLOWED_OPERATION_ENV, None)
    zotero_approval_env = "KEYSTONE_ZOTERO_OPERATOR_APPROVAL_REFERENCE"
    previous_zotero_approval = os.environ.get(zotero_approval_env)
    if route == "zotero_context_agent" and provider_write_admitted:
        request_hash = hashlib.sha256(input_text.encode("utf-8")).hexdigest()[:16]
        os.environ[zotero_approval_env] = f"zotero-direct:{request_hash}"
    if live_read_env_name:
        os.environ[live_read_env_name] = "true"
    try:
        sdk_input_parts = [input_text]
        interpreted_constraints = interpreted_output_constraints_text(manual_plan)
        if interpreted_constraints:
            sdk_input_parts.append(interpreted_constraints)
        bounded_context = specialist_execution_context_text(execution_context)
        if bounded_context:
            sdk_input_parts.append(bounded_context)
        if provider_context:
            sdk_input_parts.append(provider_context)
        if provider_write_admitted:
            sdk_input_parts.append(
                "Authenticated direct execution context: this exact scoped provider "
                "write is approved for the selected specialist. Call the matching "
                "typed tool with live=true. Reuse the process-local operator approval "
                "reference; do not request duplicate approval or downgrade to preview."
            )
        if airtable_allowed_operation:
            sdk_input_parts.append(
                "Airtable generic write scope: `airtable_write_record` may use only "
                f"operation={airtable_allowed_operation!r}. Any other operation is "
                "blocked by the tool boundary."
            )
        sdk_input_text = "\n\n".join(sdk_input_parts)
        sdk_input = sdk_input_from_typed_input(
            sdk_input_text,
            live=True,
            provider=model_config.provider,
        )
        try:
            raw_result, output = run_typed_sdk_sync(
                agent,
                sdk_input,
                output_type,
                live=True,
                session=build_sdk_session(sdk_session_spec) if sdk_session_spec else None,
                workflow_name=f"keystone.ask.{route}",
                trace_metadata={
                    "agent": route,
                    "entrypoint": "cli.ask",
                    "mode": "live_sdk",
                },
                max_turns=turn_policy.max_turns,
            )
        except Exception as exc:
            return _print_ask_context_agent_failure(
                route=route,
                input_text=input_text,
                exc=exc,
                json_output=json_output,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                sdk_session_spec=sdk_session_spec,
                cost_tracking_requested=cost_tracking_requested,
                database_url=database_url,
            )
    finally:
        if live_read_env_name:
            if previous_live_reads is None:
                os.environ.pop(live_read_env_name, None)
            else:
                os.environ[live_read_env_name] = previous_live_reads
        if previous_operator_approval is None:
            os.environ.pop(operator_approval_env, None)
        else:
            os.environ[operator_approval_env] = previous_operator_approval
        if previous_airtable_operation is None:
            os.environ.pop(AIRTABLE_ALLOWED_OPERATION_ENV, None)
        else:
            os.environ[AIRTABLE_ALLOWED_OPERATION_ENV] = previous_airtable_operation
        if previous_zotero_approval is None:
            os.environ.pop(zotero_approval_env, None)
        else:
            os.environ[zotero_approval_env] = previous_zotero_approval
    output_payload = output.model_dump(mode="json")
    _reconcile_context_agent_answer_fields(
        route,
        input_text,
        output_payload,
        manual_plan=manual_plan,
    )
    tool_receipts = [*preacquired_receipts, *_context_agent_tool_receipts(raw_result)]
    _reconcile_airtable_aggregate_result(route, output_payload, tool_receipts)
    _reconcile_context_agent_metadata_projection(output_payload, tool_receipts)
    provider_receipt_blocker = _zotero_ordered_abstract_receipt_blocker(
        route,
        input_text,
        tool_receipts,
        manual_plan=manual_plan,
    )
    airtable_write_blocker = _airtable_write_execution_blocker(
        route,
        input_text,
        manual_plan=manual_plan,
        tool_receipts=tool_receipts,
    )
    if provider_receipt_blocker:
        output_payload["summary"] = provider_receipt_blocker
        output_payload["blockers"] = list(
            dict.fromkeys(
                [
                    *[str(item) for item in output_payload.get("blockers") or []],
                    provider_receipt_blocker,
                ]
            )
        )
    external_write_performed = _context_agent_external_write_performed(tool_receipts)
    _reconcile_context_agent_executed_write_plan(output_payload, tool_receipts)
    review = review_specialist_output(
        agent_name=route,
        output=output_payload,
        request_summary=input_text,
        run_type="live_sdk",
    )
    review_payload = review.model_dump(mode="json")
    review_payload["audit_notes"] = [
        note
        for note in list(review_payload.get("audit_notes") or [])
        if not str(note).startswith("No model, network, email")
    ]
    review_payload["audit_notes"].append(
        "The deterministic review made no additional model or provider calls; the "
        "specialist execution is reported separately in model_execution and tool_receipts."
    )
    approval_references = list(
        dict.fromkeys(
            str(receipt.get("approval_reference") or "")
            for receipt in tool_receipts
            if str(receipt.get("approval_reference") or "").strip()
        )
    )
    public_output_payload, public_tool_receipts = _context_agent_public_payload(
        route,
        output_payload,
        tool_receipts,
    )
    strict_display = _strict_requested_display_text(output_payload, manual_plan)
    candidate_summary = _verified_context_agent_write_summary(
        tool_receipts
    ) or strict_display or _context_agent_human_summary(output_payload, manual_plan)
    instruction_resolution = resolve_instruction_following_response(
        candidate_summary,
        original_request=input_text,
        manual_plan=manual_plan,
        bounded_evidence=json.dumps(public_output_payload, ensure_ascii=True, sort_keys=True),
        live=True,
    )
    resolved_summary = (
        instruction_resolution.response_text
        if instruction_resolution.validation.passed
        else instruction_following_blocker_text(instruction_resolution.validation)
    )
    execution_blocker = provider_receipt_blocker or airtable_write_blocker
    provider_links: list[str] = []
    if execution_blocker:
        resolved_summary = execution_blocker
    else:
        provider_links = _verified_provider_links(tool_receipts)
        if provider_links and not strict_display:
            resolved_summary = "\n\n".join(
                [
                    resolved_summary,
                    "Open in provider:\n" + "\n".join(f"- {link}" for link in provider_links),
                ]
            ).strip()
    payload = {
        "mode": "live_sdk",
        "status": "blocked" if execution_blocker else "done",
        "block_kind": (
            "zotero_provider_read_required"
            if provider_receipt_blocker
            else "airtable_write_unverified"
            if airtable_write_blocker
            else ""
        ),
        "selected_agent": route,
        "route": route,
        "agent_name": _agent_display_name(route),
        "sdk_agent_name": agent.name,
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "sdk_session": sdk_session_spec.log_metadata() if sdk_session_spec else None,
        "cost_tracking_requested": cost_tracking_requested,
        "output_type": type(output).__name__,
        "orchestrator_review": review_payload,
        "output": public_output_payload,
        "tool_receipts": public_tool_receipts,
        "verified_provider_links": provider_links,
        "human_summary": resolved_summary,
        "instruction_following": instruction_resolution.metadata(),
        "blockers": _context_agent_public_blockers(
            output_payload,
            execution_blocker=execution_blocker,
        ),
        "side_effects": {
            "schema": "keystone.promptfoo.side_effects.v1",
            "email_sent": False,
            "gmail_draft_created": False,
            "gmail_label_changed": False,
            "slack_message_posted": False,
            "crm_write_performed": False,
            "calendar_write_performed": False,
            "external_file_write_performed": bool(
                route == "google_workspace_context_agent" and external_write_performed
            ),
            "external_write_performed": external_write_performed,
            "blocked_write_attempts": _context_agent_blocked_write_attempts(
                output_payload,
                tool_receipts=tool_receipts,
            ),
            "approval_ref": ",".join(approval_references),
            "evidence_complete": not bool(execution_blocker),
        },
    }
    _attach_slack_run_provenance(payload, execution_context)
    usage = extract_sdk_usage(raw_result)
    if usage.get("available"):
        payload["usage"] = usage
        payload["cost"] = estimate_usage_cost(
            provider=model_config.provider,
            model=model_config.model,
            usage=usage,
        )
    payload["model_execution"] = {
        "provider": model_config.provider,
        "model": model_config.model,
        "run_mode": "live_sdk",
        "usage_available": bool(usage.get("available")),
        "cost_available": isinstance(payload.get("cost"), dict),
        "base_url_configured": bool(model_config.base_url),
        "gateway_mode": bool(model_config.use_responses is False and model_config.base_url),
        "sdk_turn_policy": turn_policy.metadata(),
    }
    # Persist the same completed public contract that the Slack adapter receives.
    # Verified provider scope remains internal and contains no raw record fields.
    attach_execution_public_result(payload)
    persistence_payload = json.loads(json.dumps(payload, default=str))
    airtable_result_scope = _verified_airtable_provider_result_scope(tool_receipts)
    if airtable_result_scope is not None:
        persistence_payload["verified_provider_result_scope"] = airtable_result_scope.model_dump(
            mode="json"
        )
    try:
        run_id = SQLiteStore(database_url or database_url_from_env()).save_agent_run(
            agent_name=route,
            input_payload={"request_text": input_text, "route": route},
            input_summary=input_text[:500],
            output=persistence_payload,
            model=f"sdk-live:{model_config.model}",
            dry_run=False,
            status="blocked" if execution_blocker else "success",
        )
        payload["agent_run_id"] = run_id
        public_result = payload.get("public_result")
        if isinstance(public_result, dict):
            public_result["run_id"] = str(run_id)
    except Exception as exc:  # pragma: no cover - diagnostic metadata only
        payload["agent_run_persistence_error"] = f"{type(exc).__name__}: {exc}"
    return _print_ask_live_payload(payload, json_output=json_output)


def _print_ask_context_agent_failure(
    *,
    route: str,
    input_text: str,
    exc: Exception,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    cost_tracking_requested: bool,
    database_url: str | None,
) -> int:
    """Render a concise context-agent blocker and persist traceback internally."""

    failure = known_exception_to_operator_failure(
        exc,
        context=f"{_agent_display_name(route)} run",
    )
    public_failure = failure.to_dict()
    public_failure["reason"] = failure.summary
    public_payload: dict[str, Any] = {
        "mode": "live_sdk",
        "status": "failed",
        "block_kind": failure.kind,
        "selected_agent": route,
        "route": route,
        "agent_name": _agent_display_name(route),
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "sdk_session": sdk_session_spec.log_metadata() if sdk_session_spec else None,
        "cost_tracking_requested": cost_tracking_requested,
        "human_summary": f"{failure.summary} {failure.next_step}".strip(),
        "blockers": [failure.summary],
        "output": {
            "summary": failure.summary,
            "next_step": failure.next_step,
            "failure": public_failure,
            "send_enabled": False,
        },
        "side_effects": {
            "schema": "keystone.promptfoo.side_effects.v1",
            "email_sent": False,
            "gmail_draft_created": False,
            "gmail_label_changed": False,
            "slack_message_posted": False,
            "crm_write_performed": False,
            "calendar_write_performed": False,
            "external_file_write_performed": False,
            "external_write_performed": False,
            "blocked_write_attempts": [],
            "approval_ref": "",
            "evidence_complete": False,
        },
    }
    persisted_payload = {
        **public_payload,
        "internal_diagnostics": {
            "error_type": type(exc).__name__,
            "traceback": redact_operator_text(traceback.format_exc(), max_chars=12000),
        },
    }
    try:
        run_id = SQLiteStore(database_url or database_url_from_env()).save_agent_run(
            agent_name=route,
            input_payload={"request_text": input_text, "route": route},
            input_summary=input_text[:500],
            output=persisted_payload,
            model="sdk-live",
            dry_run=False,
            status="error",
            error=failure.kind,
        )
        public_payload["agent_run_id"] = run_id
    except Exception:  # pragma: no cover - diagnostic metadata only
        public_payload["agent_run_persistence_error"] = (
            "Internal diagnostic persistence failed; no provider action was confirmed."
        )
    _print_ask_live_payload(public_payload, json_output=json_output)
    return 1


def _direct_context_agent_tool_tier(
    manual_plan: ManualRequestPlan | None,
    *,
    input_text: str = "",
    route: str = "",
) -> str:
    """Attach only the source tools needed by a bounded direct context call."""

    if _direct_context_agent_write_admitted(
        manual_plan,
        input_text=input_text,
        route=route,
    ):
        return "internal_write"
    return "core_read"


def _direct_context_agent_write_admitted(
    manual_plan: ManualRequestPlan | None,
    *,
    input_text: str,
    route: str = "",
) -> bool:
    """Use semantic intent for live plans and phrases only as offline fallback."""

    if manual_plan is not None:
        authority = ExecutionIntentAuthority.from_value(manual_plan)
        if authority.canonical:
            provider_owner = {
                "airtable": "airtable_context_agent",
                "google_workspace": "google_workspace_context_agent",
                "zotero": "zotero_context_agent",
            }.get(manual_plan.provider_system)
            effective_mutations = {
                operation
                for operation in authority.effective_provider_operations(
                    manual_plan.provider_system
                )
                if operation in {"create", "update", "delete", "attach"}
            }
            return bool(
                provider_owner
                and manual_plan.target_agent == provider_owner
                and (not route or route == provider_owner)
                and manual_plan.intent == "business_system_write"
                and effective_mutations
            )
        if authority.invalid:
            return False
        if manual_plan.intent == "business_system_write":
            return True
    return chief_workflow_requests_marked_airtable_test_lifecycle(input_text)


def _direct_airtable_allowed_operation(
    manual_plan: ManualRequestPlan | None,
    *,
    input_text: str,
) -> str:
    """Return the exact generic Airtable mutation admitted by the operator ask."""

    if manual_plan is None or manual_plan.intent != "business_system_write":
        return ""
    if chief_workflow_requests_marked_airtable_test_lifecycle(input_text):
        return ""
    receipt_target = resolve_finance_expense_receipt_target(
        input_text,
        manual_plan=manual_plan,
    )
    if receipt_target is not None:
        if receipt_target.operation == "create":
            return "create"
        if receipt_target.operation == "update":
            return "update"
        return ""
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical:
        if (
            manual_plan.target_agent != "airtable_context_agent"
            or manual_plan.provider_system != "airtable"
        ):
            return ""
        mutations = {
            operation
            for operation in authority.effective_provider_operations("airtable")
            if operation in {"create", "update"}
        }
        return next(iter(mutations)) if len(mutations) == 1 else ""
    if authority.invalid:
        return ""
    actionable = re.sub(
        r"\b(?:do\s+not|don't|dont|never|without)\b[^.;\n]*",
        " ",
        str(input_text or ""),
        flags=re.I,
    )
    if re.search(r"\b(?:reconcile|deduplicate|merge\s+duplicates?)\b", actionable, re.I):
        return "update"
    creates = bool(re.search(r"\b(?:add|create|insert|make)\b", actionable, re.I))
    updates = bool(
        re.search(r"\b(?:update|change|modify|revise|edit|set|correct)\b", actionable, re.I)
    )
    if creates and not updates:
        return "create"
    if updates and not creates:
        return "update"
    return ""


def _reconcile_context_agent_answer_fields(
    route: str,
    input_text: str,
    output: dict[str, Any],
    *,
    manual_plan: ManualRequestPlan | None = None,
) -> None:
    """Promote substantive model evidence when a strict answer lands in the wrong field."""

    normalized_request = " ".join(str(input_text or "").lower().split())
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical:
        abstract_requested = bool(
            authority.plan is not None
            and authority.plan.provider_system == "zotero"
            and "abstract" in authority.plan.zotero_requested_fields
        )
    elif authority.invalid:
        abstract_requested = False
    else:
        abstract_requested = "abstract" in normalized_request
    if route != "zotero_context_agent" or not abstract_requested:
        return
    if not output.get("article_titles"):
        return
    summary = " ".join(str(output.get("summary") or "").split())
    procedural_summary = bool(
        re.search(
            r"\b(?:selected|identified|found)\b.*\b(?:summari[sz]ed|summary)\b",
            summary,
            re.IGNORECASE,
        )
        or re.search(
            r"\b(?:summary|response)\b.*\b(?:word limit|requested format|constrained)\b",
            summary,
            re.IGNORECASE,
        )
    )
    if summary and not procedural_summary:
        return
    evidence = output.get("relevant_evidence")
    if not isinstance(evidence, list):
        return
    for item in evidence:
        candidate = " ".join(str(item or "").split())
        candidate_lower = candidate.lower()
        if not candidate or "abstract" not in candidate_lower:
            continue
        if not re.search(
            r"\babstract\b.*\b(?:reports?|finds?|describes?|shows?|concludes?|"
            r"examines?|reviews?|evaluates?|assesses?|investigates?)\b",
            candidate_lower,
        ):
            continue
        output["summary"] = candidate
        diagnostics = output.get("diagnostics")
        if isinstance(diagnostics, list):
            diagnostics.append(
                {
                    "key": "canonical_summary_field",
                    "value": "relevant_evidence",
                    "note": (
                        "Promoted the specialist's substantive abstract synthesis into "
                        "the canonical summary field."
                    ),
                }
            )
        return


def _reconcile_context_agent_metadata_projection(
    output: dict[str, Any],
    tool_receipts: list[dict[str, object]],
) -> None:
    """Preserve schema-aware provider fields through model synthesis and rendering."""

    projection = next(
        (
            receipt.get("metadata_projection")
            for receipt in tool_receipts
            if isinstance(receipt.get("metadata_projection"), dict)
        ),
        None,
    )
    if not isinstance(projection, dict):
        return
    fields = projection.get("fields")
    if not isinstance(fields, dict):
        return
    title = str(fields.get("title") or "").strip()
    if title and not output.get("article_titles"):
        output["article_titles"] = [title]
    diagnostics = output.get("diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = []
        output["diagnostics"] = diagnostics
    by_key = {
        str(item.get("key") or "").strip(): item
        for item in diagnostics
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    }
    for field in projection.get("requested_fields") or []:
        clean_field = str(field or "").strip()
        if clean_field in {"", "title"}:
            continue
        value = fields.get(clean_field)
        rendered_value = (
            "; ".join(str(item).strip() for item in value if str(item).strip())
            if isinstance(value, list)
            else str(value or "").strip()
        )
        entry = by_key.get(clean_field)
        if entry is None:
            diagnostics.append(
                {
                    "key": clean_field,
                    "value": rendered_value,
                    "note": (
                        "Projected from the schema-aware provider context."
                        if rendered_value
                        else "The requested field was unavailable in provider metadata."
                    ),
                }
            )
        elif rendered_value:
            entry["value"] = rendered_value
            entry["note"] = "Projected from the schema-aware provider context."


def _reconcile_airtable_aggregate_result(
    route: str,
    output: dict[str, Any],
    tool_receipts: list[dict[str, object]],
) -> None:
    """Make verified provider arithmetic authoritative over model prose."""

    if route != "airtable_context_agent":
        return
    receipt = next(
        (
            item
            for item in reversed(tool_receipts)
            if item.get("operation") == "aggregate_records" and item.get("status") == "success"
        ),
        None,
    )
    if receipt is None:
        return
    table = str(receipt.get("table") or "Airtable records").strip()
    total = str(receipt.get("total") or "0.00").strip()
    currency = str(receipt.get("currency") or "USD").strip()
    period = receipt.get("estimated_period")
    year = receipt.get("year")
    matching = int(receipt.get("matching_records") or 0)
    period_text = f"estimated period {period}" if period else "the requested scope"
    if year:
        period_text = f"{period_text} in {year}"
    amount_text = f"${total}" if currency == "USD" else f"{total} {currency}"
    output["summary"] = (
        f"Total {table.lower()} for {period_text}: {amount_text} across "
        f"{matching} matching record{'s' if matching != 1 else ''}."
    )
    output["base_alias"] = str(receipt.get("base_alias") or "")
    output["relevant_tables"] = [table]
    output["relevant_fields"] = list(
        dict.fromkeys(
            str(receipt.get(key) or "").strip()
            for key in ("amount_field", "period_field", "date_field")
            if str(receipt.get(key) or "").strip()
        )
    )
    matching_summaries = receipt.get("matching_record_summaries")
    if isinstance(matching_summaries, list) and matching_summaries:
        output["record_summaries"] = [
            item for item in matching_summaries[:10] if isinstance(item, dict)
        ]
    else:
        output["record_summaries"] = [
            {
                "key": "verified_total",
                "value": amount_text,
                "note": f"{matching} matching provider records; Decimal sum",
            }
        ]


def _verified_airtable_provider_result_scope(
    tool_receipts: list[dict[str, object]],
) -> ManualProviderResultSetScope | None:
    """Build reusable Airtable collection identity only from a verified receipt."""

    receipt = next(
        (
            item
            for item in reversed(tool_receipts)
            if item.get("operation") == "aggregate_records"
            and item.get("status") == "success"
            and item.get("provider_read") is True
            and item.get("provider_write") is not True
            and isinstance(item.get("verification"), dict)
            and item["verification"].get("passed") is True  # type: ignore[index]
            and item.get("complete") is True
            and item.get("verified") is True
        ),
        None,
    )
    if receipt is None:
        return None
    raw_scope = receipt.get("result_scope")
    scope = raw_scope if isinstance(raw_scope, dict) else {}
    item_refs = scope.get("item_refs")
    refs = item_refs if isinstance(item_refs, list) else []
    return ManualProviderResultSetScope(
        provider_system="airtable",
        provider_read_scope="bounded_collection",
        target_type="business_system_context",
        item_count=int(receipt.get("matching_records") or len(refs)),
        airtable_base_alias=str(scope.get("base_alias") or receipt.get("base_alias") or ""),
        airtable_table=str(scope.get("table") or receipt.get("table") or ""),
        airtable_amount_field=str(scope.get("amount_field") or receipt.get("amount_field") or ""),
        airtable_period_field=str(scope.get("period_field") or receipt.get("period_field") or ""),
        airtable_date_field=str(scope.get("date_field") or receipt.get("date_field") or ""),
        airtable_estimated_period=(
            int(scope.get("estimated_period") or receipt.get("estimated_period"))
            if scope.get("estimated_period") or receipt.get("estimated_period")
            else None
        ),
        airtable_year=(
            int(scope.get("year") or receipt.get("year"))
            if scope.get("year") or receipt.get("year")
            else None
        ),
        aggregate_total=str(scope.get("total") or receipt.get("total") or ""),
        aggregate_currency=str(scope.get("currency") or receipt.get("currency") or ""),
        item_refs=refs,
        complete=True,
        verified=True,
    )


def _verified_provider_links(
    tool_receipts: list[dict[str, object]],
) -> list[str]:
    """Return clickable links only from provider-verified successful receipts."""

    links: list[str] = []
    for receipt in tool_receipts:
        verification = receipt.get("verification")
        verified = bool(isinstance(verification, dict) and verification.get("passed") is True)
        if not verified or str(receipt.get("status") or "").lower() != "success":
            continue
        link = str(
            receipt.get("provider_link") or receipt.get("html_link") or receipt.get("url") or ""
        ).strip()
        if link.startswith("https://") and link not in links:
            links.append(link)
    return links[:5]


def _context_agent_tool_receipts(raw_result: object) -> list[dict[str, object]]:
    """Return bounded, content-safe provider receipts from SDK tool outputs."""

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
        verification = parsed.get("verification")
        safe_verification = (
            {
                key: verification.get(key)
                for key in (
                    "status",
                    "passed",
                    "record_id_match",
                    "matched_fields",
                    "mismatched_fields",
                    "provider_deleted",
                    "record_absent_after",
                    "create_read_back",
                    "same_record_update_read_back",
                    "record_absent_after_cleanup",
                    "attachment_read_back",
                    "updated_period",
                    "attachment_filenames",
                    "duplicate_provider_deleted",
                    "duplicate_record_absent_after",
                    "same_note_update_read_back",
                    "note_absent_after_cleanup",
                    "item_key_match",
                    "item_type_note",
                    "marker_present",
                    "note_match",
                    "item_absent_after",
                    "attachment_count_before",
                    "attachment_count_after",
                    "spreadsheet_id_match",
                    "title_match",
                    "mime_type_match",
                    "trashed",
                    "trashed_match",
                    "row_count_match",
                    "values_match",
                    "verified_row_count",
                    "row_found",
                    "row_number",
                    "row_count_delta",
                    "row_absent",
                    "artifact_exists",
                    "file_signature_valid",
                    "parent_hash_match",
                    "parent_mtime_match",
                    "artifact_absent_after",
                    "schema_read",
                    "records_read",
                    "period_filter_applied",
                    "deterministic_arithmetic",
                    "scope_membership_match",
                    "prior_total_match",
                    "record_projection_count",
                )
                if key in verification
            }
            if isinstance(verification, dict)
            else {}
        )
        receipt = {
            key: parsed.get(key)
            for key in (
                "status",
                "operation",
                "provider",
                "base_alias",
                "table",
                "record_id",
                "duplicate_record_id",
                "target_estimated_tax_period",
                "item_key",
                "parent_item_key",
                "document_id",
                "file_id",
                "spreadsheet_id",
                "title",
                "folder_path",
                "sheet_name",
                "row_count",
                "row_number",
                "key_column",
                "key_value",
                "deleted_row_index",
                "updated_range",
                "trashed",
                "query",
                "mime_type",
                "item_count",
                "provider_read",
                "provider_order",
                "selection_rule",
                "require_abstract",
                "selected_item_title",
                "selected_item_has_abstract",
                "selected_item_date_added",
                "char_count",
                "truncated",
                "slide_number",
                "output_format",
                "artifact_path",
                "artifact_size",
                "artifact_sha256",
                "parent_content_sha256",
                "parent_modified",
                "derived_copy_created",
                "approval_reference",
                "required_marker",
                "reason",
                "amount_field",
                "estimated_period",
                "period_field",
                "year",
                "date_field",
                "total",
                "currency",
                "matching_records",
                "contributing_records",
                "records_checked",
                "record_limit",
                "complete",
                "verified",
                "result_scope",
                "matching_record_summaries",
                "send_enabled",
            )
            if key in parsed
        }
        if safe_verification:
            receipt["verification"] = safe_verification
        if receipt:
            receipts.append(receipt)
    return receipts[:12]


def _direct_airtable_receipt_provider_context(
    route: str,
    input_text: str,
    *,
    manual_plan: ManualRequestPlan | None = None,
    execution_context: dict[str, Any] | None,
) -> str:
    """Expose one selected Slack receipt path to the bounded Airtable write tool."""

    if route != "airtable_context_agent":
        return ""
    target = resolve_finance_expense_receipt_target(
        _finance_receipt_context_input(input_text, execution_context),
        manual_plan=manual_plan,
    )
    if target is None or not target.receipt_local_path:
        return ""
    receipt_path = Path(target.receipt_local_path)
    if not receipt_path.is_file():
        return ""
    context = finance_expense_receipt_provider_context(target)
    return "Selected Slack receipt context for the approved Airtable operation:\n" + json.dumps(
        context, ensure_ascii=True, sort_keys=True
    )


def _direct_airtable_result_scope_provider_preflight(
    route: str,
    *,
    manual_plan: ManualRequestPlan | None,
) -> tuple[str, list[dict[str, object]], str]:
    """Re-read one verified Airtable aggregate scope for an item follow-up."""

    if (
        route != "airtable_context_agent"
        or manual_plan is None
        or manual_plan.provider_system != "airtable"
        or manual_plan.provider_result_mode != "items"
        or manual_plan.provider_result_scope is None
    ):
        return "", [], ""
    scope = manual_plan.provider_result_scope
    if (
        not scope.verified
        or not scope.complete
        or scope.provider_system != "airtable"
        or scope.provider_read_scope != "bounded_collection"
        or not scope.airtable_table
    ):
        return "", [], ""
    try:
        receipt = airtable_aggregate_records_impl(
            table=scope.airtable_table,
            base_alias=scope.airtable_base_alias or "finance_tax_tracker",
            amount_field=scope.airtable_amount_field,
            estimated_period=str(scope.airtable_estimated_period or ""),
            period_field=scope.airtable_period_field,
            year=scope.airtable_year or 0,
            date_field=scope.airtable_date_field,
            include_matching_records=True,
            expected_record_ids=scope.item_refs,
            expected_total=scope.aggregate_total,
            live=True,
        )
    except Exception as exc:  # pragma: no cover - provider diagnostics
        return (
            "",
            [],
            "The bounded Airtable follow-up could not re-read the verified prior "
            f"record set: {redact_operator_text(str(exc))}",
        )
    if receipt.get("status") != "success":
        reason = str(receipt.get("reason") or "provider_read_failed").strip()
        return (
            "",
            [receipt],
            "The bounded Airtable follow-up could not re-read the verified prior "
            f"record set ({reason}).",
        )
    summaries = receipt.get("matching_record_summaries")
    safe_context = {
        "provider": "airtable",
        "table": receipt.get("table"),
        "estimated_period": receipt.get("estimated_period"),
        "year": receipt.get("year"),
        "total": receipt.get("total"),
        "currency": receipt.get("currency"),
        "matching_records": receipt.get("matching_records"),
        "record_summaries": summaries if isinstance(summaries, list) else [],
        "verification": receipt.get("verification"),
    }
    return (
        "Verified Airtable provider projection for this follow-up. Synthesize only "
        "these records and state if the provider set or total changed since the prior "
        "answer:\n" + json.dumps(safe_context, ensure_ascii=True, sort_keys=True, default=str),
        [receipt],
        "",
    )


def _direct_zotero_provider_preflight(
    route: str,
    input_text: str,
    *,
    manual_plan: ManualRequestPlan | None = None,
) -> tuple[str, list[dict[str, object]], str]:
    """Acquire required ordered Zotero evidence before the synthesis model call."""

    normalized = " ".join(str(input_text or "").lower().split())
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    semantic_authority = authority.canonical
    if semantic_authority:
        write_requested = bool(
            route == "zotero_context_agent"
            and manual_plan is not None
            and manual_plan.target_agent == "zotero_context_agent"
            and manual_plan.provider_system == "zotero"
            and manual_plan.intent == "business_system_write"
            and any(
                operation in {"create", "update", "delete", "attach"}
                for operation in authority.effective_provider_operations("zotero")
            )
        )
    else:
        write_requested = bool(
            route == "zotero_context_agent"
            and re.search(
                r"\b(?:create|add|make|write|edit|update|change|revise|delete|remove)\b",
                normalized,
            )
            and re.search(r"\b(?:note|item|article|collection)\b", normalized)
        )
    if write_requested:
        try:
            capabilities = read_zotero_api_key_capabilities()
        except Exception as exc:
            return (
                "",
                [
                    {
                        "status": "error",
                        "provider_read": False,
                        "operation": "verify_api_key_capabilities",
                        "error_type": type(exc).__name__,
                    }
                ],
                (
                    "The configured Zotero API key was rejected by Zotero's "
                    "/keys/current capability check. Replace or re-authorize the key "
                    "before retrying; no Zotero data was modified."
                ),
            )
        receipt = {
            key: capabilities.get(key)
            for key in (
                "status",
                "provider_read",
                "operation",
                "user_id_present",
                "user_library",
                "user_files",
                "user_notes",
                "user_write",
                "send_enabled",
            )
        }
        if (
            capabilities.get("status") != "success"
            or capabilities.get("provider_read") is not True
            or capabilities.get("user_id_present") is not True
            or capabilities.get("user_library") is not True
            or capabilities.get("user_write") is not True
        ):
            return (
                "",
                [receipt],
                (
                    "The configured Zotero API key does not grant user-library write "
                    "access. Enable library write permission or replace the key before "
                    "retrying; no Zotero data was modified."
                ),
            )
        return (
            "Configured Zotero API key capability check passed for the approved write.",
            [receipt],
            "",
        )

    provider_read_authorized = bool(
        manual_plan is not None
        and manual_plan.target_agent == "zotero_context_agent"
        and manual_plan.provider_system == "zotero"
        and any(
            operation in {"read", "search", "verify"}
            for operation in authority.effective_provider_operations("zotero")
        )
    )
    if semantic_authority:
        assert manual_plan is not None
        provider_steps = ExecutionIntentAuthority.from_value(
            manual_plan
        ).provider_action_steps("zotero")
        ordered_collection_item_read = bool(
            manual_plan.target_type == "zotero_collection"
            and manual_plan.provider_selection_order == "latest"
            and manual_plan.zotero_requested_fields
            and any(
                step.resource_type == "zotero_collection"
                and step.operation in {"read", "search", "verify"}
                for step in provider_steps
            )
        )
        zotero_item_read = bool(
            manual_plan.target_type == "zotero_article"
            or any(
                step.resource_type == "zotero_item"
                and step.operation in {"read", "search", "verify"}
                for step in provider_steps
            )
            # A collection-scoped query can still select and project one item.
            # Keep this compatibility typed: ordering, requested item fields,
            # provider operation, and resource scope must all be explicit.
            or ordered_collection_item_read
        )
        latest_journal_requested = bool(
            route == "zotero_context_agent"
            and provider_read_authorized
            and zotero_item_read
            and manual_plan.provider_selection_order == "latest"
        )
        requested_zotero_fields = set(manual_plan.zotero_requested_fields)
        selection_rank = manual_plan.provider_selection_rank or 1
    else:
        latest_journal_requested = bool(
            route == "zotero_context_agent"
            and "zotero" in normalized
            and re.search(r"\b(?:journal\s+article|article)\b", normalized)
            and re.search(
                r"\b(?:latest|most\s+recent(?:ly)?\s+added)\b",
                normalized,
            )
        )
        requested_zotero_fields = set()
        selection_rank = 1
    if not latest_journal_requested:
        return "", [], ""
    require_abstract = bool(
        "abstract" in requested_zotero_fields
        if semantic_authority
        else "abstract" in normalized
    )
    try:
        if require_abstract:
            payload = (
                read_latest_zotero_journal_abstract_metadata()
                if selection_rank == 1
                else read_latest_zotero_journal_abstract_metadata(
                    selection_rank=selection_rank
                )
            )
        else:
            payload = (
                read_latest_zotero_journal_metadata(require_abstract=False)
                if selection_rank == 1
                else read_latest_zotero_journal_metadata(
                    require_abstract=False,
                    selection_rank=selection_rank,
                )
            )
    except Exception as exc:
        return (
            "",
            [{"status": "error", "provider_read": False, "error_type": type(exc).__name__}],
            (
                "The live Zotero metadata read failed before synthesis. No cached result "
                "was substituted and no Zotero data was modified."
            ),
        )

    receipt = {
        key: payload.get(key)
        for key in (
            "status",
            "provider_read",
            "provider_order",
            "selection_rule",
            "selection_rank",
            "available_item_count",
            "require_abstract",
            "item_count",
            "selected_item_title",
            "selected_item_has_abstract",
            "selected_item_date_added",
        )
        if key in payload
    }
    items = payload.get("items")
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    title = str(data.get("title") or payload.get("selected_item_title") or "").strip()
    abstract = str(data.get("abstractNote") or "").strip()
    item_key = str(item.get("key") or data.get("key") or "").strip()
    if (
        payload.get("status") != "success"
        or payload.get("provider_read") is not True
        or not title
        or (require_abstract and not abstract)
        or not item_key
    ):
        available_count = payload.get("available_item_count")
        if (
            isinstance(available_count, int)
            and available_count < selection_rank
        ):
            return (
                "",
                [receipt],
                (
                    "The Zotero provider returned "
                    f"{available_count} qualifying journal article"
                    f"{'' if available_count == 1 else 's'}, so ordered rank "
                    f"{selection_rank} has no matching item. No cached result or web "
                    "search was substituted."
                ),
            )
        return (
            "",
            [receipt],
            (
                "The live Zotero provider read did not return one identifiable journal "
                + (
                    "article with a stored abstract in the required ordering."
                    if require_abstract
                    else "article in the required ordering."
                )
            ),
        )

    links = item.get("links")
    provider_link = ""
    if isinstance(links, dict):
        for link_name in ("alternate", "self"):
            link = links.get(link_name)
            if not isinstance(link, dict):
                continue
            candidate_link = str(link.get("href") or "").strip()
            if candidate_link.startswith("https://"):
                provider_link = candidate_link
                break
    receipt.update(
        {
            "operation": (
                "read_latest_journal_metadata"
                if selection_rank == 1
                else "read_ranked_journal_metadata"
            ),
            "item_key": item_key,
            "provider_link": provider_link,
            "verification": {
                "status": "verified",
                "passed": (
                    int(payload.get("selection_rank") or 1) == selection_rank
                ),
                "item_key_match": True,
                "provider_order_match": True,
            },
        }
    )
    if selection_rank != 1:
        verification = receipt.get("verification")
        if isinstance(verification, dict):
            verification["selection_rank_match"] = (
                int(payload.get("selection_rank") or 1) == selection_rank
            )
    projection = project_zotero_item_metadata(
        item,
        request_text=input_text if not semantic_authority else "",
        requested_fields=(
            [
                field
                for field in manual_plan.zotero_requested_fields
                if field
                in {
                    "title",
                    "authors",
                    "publication_title",
                    "abstract",
                    "metadata",
                }
            ]
            if semantic_authority and manual_plan is not None
            else None
        ),
    )
    projected_fields = projection.get("fields")
    fields = projected_fields if isinstance(projected_fields, dict) else {}
    requested_fields = set(projection.get("requested_fields") or [])
    enrichment: dict[str, Any] = {}
    doi = str(fields.get("doi") or data.get("DOI") or "").strip()
    missing_bibliographic_fields = {
        field
        for field in ("authors", "publication_title")
        if field in requested_fields and fields.get(field) in (None, "", [], {})
    }
    if missing_bibliographic_fields and doi:
        try:
            enrichment = enrich_zotero_bibliographic_metadata(
                doi=doi,
                title=title,
                item_key=item_key,
            )
        except Exception as exc:  # pragma: no cover - live provider diagnostic
            enrichment = {"status": "error", "error_type": type(exc).__name__}
        if enrichment.get("status") == "success":
            for field in missing_bibliographic_fields:
                value = enrichment.get(field)
                if value not in (None, "", [], {}):
                    fields[field] = value
            projection["missing_requested_fields"] = [
                field
                for field in projection.get("requested_fields") or []
                if fields.get(field) in (None, "", [], {})
            ]
            projection["enrichment_source"] = "crossref_doi_metadata"

    receipt["metadata_projection"] = projection
    receipt["bibliographic_enrichment"] = {
        key: enrichment.get(key) for key in ("status", "source", "doi") if key in enrichment
    }
    normalized_request = " ".join(str(input_text or "").lower().split())
    if semantic_authority:
        notes_requested = "children" in requested_zotero_fields
        attachments_requested = bool(
            requested_zotero_fields.intersection({"children", "full_text"})
        )
    else:
        notes_requested = bool(
            re.search(
                r"\b(?:read|show|list|include|summari[sz]e|what)\b[^.]{0,80}"
                r"\bnotes?\b|\bnotes?\b[^.]{0,80}"
                r"\b(?:attached|associated|stored|say|contain)\b",
                normalized_request,
            )
        )
        attachments_requested = bool(
            re.search(
                r"\b(?:read|show|list|include|inspect|summari[sz]e)\b[^.]{0,80}"
                r"\b(?:attachments?|pdfs?)\b|\b(?:attachments?|pdfs?)\b[^.]{0,80}"
                r"\b(?:attached|associated|stored|available|contain)\b",
                normalized_request,
            )
        )
    child_context: dict[str, Any] = {}
    child_receipts: list[dict[str, object]] = []
    if notes_requested or attachments_requested:
        try:
            child_payload = json.loads(
                zotero_read_item_children(
                    parent_item_key=item_key,
                    limit=50,
                    live=True,
                )
            )
        except Exception as exc:  # pragma: no cover - live provider diagnostic
            child_payload = {"status": "error", "error_type": type(exc).__name__}
        if isinstance(child_payload, dict):
            children = child_payload.get("children")
            bounded_children = children if isinstance(children, list) else []
            child_context = {
                "schema": "keystone.zotero_item_children_context.v1",
                "children": bounded_children,
                "note_count": child_payload.get("note_count", 0),
                "attachment_count": child_payload.get("attachment_count", 0),
            }
            child_receipts.append(
                {
                    "status": child_payload.get("status"),
                    "provider_read": child_payload.get("provider_read", False),
                    "operation": "read_item_children",
                    "note_count": child_payload.get("note_count", 0),
                    "attachment_count": child_payload.get("attachment_count", 0),
                }
            )
            pdf_text_requested = bool(
                "full_text" in requested_zotero_fields
                if semantic_authority
                else (
                    attachments_requested
                    and re.search(
                        r"\b(?:read|extract|summari[sz]e|review)\b",
                        normalized_request,
                    )
                    and re.search(
                        r"\bpdf\b|\bfull\s+text\b|\bpaper\b",
                        normalized_request,
                    )
                    and not re.search(
                        r"\b(?:do\s+not|don't|without)\b[^.]{0,40}"
                        r"\b(?:pdf|full\s+text)\b",
                        normalized_request,
                    )
                )
            )
            pdf_children = [
                child
                for child in bounded_children
                if isinstance(child, dict)
                and child.get("item_type") == "attachment"
                and (
                    str(child.get("content_type") or "").lower() == "application/pdf"
                    or str(child.get("filename") or "").lower().endswith(".pdf")
                )
            ]
            pdf_child = pdf_children[0] if len(pdf_children) == 1 else None
            if pdf_text_requested and len(pdf_children) != 1:
                child_context["pdf_selection"] = {
                    "status": "not_found" if not pdf_children else "ambiguous",
                    "candidate_count": len(pdf_children),
                    "message": (
                        "No PDF attachment was available for the selected article."
                        if not pdf_children
                        else "Multiple PDF attachments were available; name one attachment."
                    ),
                }
            if pdf_text_requested and isinstance(pdf_child, dict):
                try:
                    pdf_payload = json.loads(
                        zotero_read_pdf_attachment_text(
                            parent_item_key=item_key,
                            attachment_item_key=str(pdf_child.get("item_key") or ""),
                            max_pages=25,
                            max_chars=50000,
                            live=True,
                        )
                    )
                except Exception as exc:  # pragma: no cover - live provider diagnostic
                    pdf_payload = {"status": "error", "error_type": type(exc).__name__}
                if isinstance(pdf_payload, dict):
                    child_context["selected_pdf"] = pdf_payload
                    child_receipts.append(
                        {
                            "status": pdf_payload.get("status"),
                            "provider_read": pdf_payload.get("provider_read", False),
                            "operation": "read_pdf_attachment_text",
                            "page_count": pdf_payload.get("page_count", 0),
                            "pages_read": pdf_payload.get("pages_read", 0),
                            "char_count": pdf_payload.get("char_count", 0),
                            "truncated": pdf_payload.get("truncated", False),
                            "file_persisted": False,
                        }
                    )
    context = {
        "schema": (
            "keystone.zotero_ordered_abstract_context.v1"
            if require_abstract
            else "keystone.zotero_ordered_journal_context.v1"
        ),
        "selection_rule": payload.get("selection_rule"),
        "provider_order": payload.get("provider_order"),
        "item_key": item_key,
        "title": title,
        "date_added": str(data.get("dateAdded") or ""),
        "doi": doi,
        "url": str(data.get("url") or ""),
        "authors": fields.get("authors") or zotero_creator_names(data.get("creators")),
        "publication_title": str(
            fields.get("publication_title") or data.get("publicationTitle") or ""
        ),
        "journal_abbreviation": str(data.get("journalAbbreviation") or ""),
        "publication_date": str(data.get("date") or ""),
        "volume": str(data.get("volume") or ""),
        "issue": str(data.get("issue") or ""),
        "pages": str(data.get("pages") or ""),
        "stored_abstract": _bounded_redacted_text(abstract, max_chars=12000),
        "metadata_projection": projection,
        "child_context": child_context,
    }
    return (
        "Authenticated read-only Zotero provider evidence for synthesis:\n"
        + json.dumps(context, ensure_ascii=True, sort_keys=True),
        [receipt, *child_receipts],
        "",
    )


def _zotero_ordered_abstract_receipt_blocker(
    route: str,
    input_text: str,
    tool_receipts: list[dict[str, object]],
    *,
    manual_plan: ManualRequestPlan | None = None,
) -> str:
    """Require live provider evidence for ordered Zotero abstract selection asks."""

    normalized = " ".join(str(input_text or "").lower().split())
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical:
        assert authority.plan is not None
        plan = authority.plan
        requires_ordered_abstract = bool(
            route == "zotero_context_agent"
            and plan.target_agent == "zotero_context_agent"
            and plan.provider_system == "zotero"
            and any(
                operation in {"read", "search", "verify"}
                for operation in authority.effective_provider_operations("zotero")
            )
            and plan.provider_selection_order == "latest"
            and "abstract" in plan.zotero_requested_fields
        )
        selection_rank = plan.provider_selection_rank or 1
    elif authority.invalid:
        requires_ordered_abstract = False
    else:
        requires_ordered_abstract = bool(
            route == "zotero_context_agent"
            and "zotero" in normalized
            and "abstract" in normalized
            and re.search(
                r"\b(?:latest|most\s+recent(?:ly)?\s+added)\b",
                normalized,
            )
        )
        selection_rank = 1
    if not requires_ordered_abstract:
        return ""
    provider_receipt = next(
        (
            receipt
            for receipt in tool_receipts
            if receipt.get("provider_read") is True
            and receipt.get("selection_rule")
            == (
                "first_nonempty_abstract_in_provider_order"
                if selection_rank == 1
                else "ranked_nonempty_abstract_in_provider_order"
            )
            and int(receipt.get("selection_rank") or 1) == selection_rank
        ),
        None,
    )
    if provider_receipt is None:
        return (
            "The requested Zotero result was not verified by a live, ordered provider "
            "metadata read; cached context was not accepted as a substitute."
        )
    if provider_receipt.get("require_abstract") is not True:
        return (
            "The Zotero provider read did not verify that the ordered selection "
            "required a stored abstract."
        )
    provider_order = provider_receipt.get("provider_order")
    expected_order = {
        "sort": "dateAdded",
        "direction": "desc",
        "top_level_only": True,
        "item_type": "journalArticle",
    }
    if not isinstance(provider_order, dict) or any(
        provider_order.get(key) != value for key, value in expected_order.items()
    ):
        return (
            "The Zotero provider read did not verify the required top-level journal "
            "article ordering by dateAdded descending."
        )
    if (
        str(provider_receipt.get("status") or "").lower() == "success"
        and provider_receipt.get("selected_item_has_abstract") is not True
    ):
        return "The selected Zotero provider item did not contain a stored abstract."
    return ""


def _context_agent_public_payload(
    route: str,
    output_payload: dict[str, object],
    tool_receipts: list[dict[str, object]],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Remove provider identities from public context-agent output and receipts."""

    public_output = json.loads(json.dumps(output_payload, default=str))
    public_receipts = json.loads(json.dumps(tool_receipts, default=str))
    identity_field = ""
    lifecycle_operation = ""
    if route == "airtable_context_agent":
        identity_field = "record_id"
        lifecycle_operation = "test_record_lifecycle"
        for receipt in public_receipts:
            if isinstance(receipt, dict):
                receipt.pop("result_scope", None)
                receipt.pop("matching_record_summaries", None)
    elif route == "zotero_context_agent":
        identity_field = "item_key"
        lifecycle_operation = "test_note_lifecycle"
    else:
        return public_output, public_receipts
    lifecycle_receipts = [
        receipt for receipt in tool_receipts if receipt.get("operation") == lifecycle_operation
    ]
    provider_ids = {
        str(receipt.get(identity_field) or "").strip()
        for receipt in lifecycle_receipts
        if str(receipt.get(identity_field) or "").strip()
    }
    if not provider_ids:
        return public_output, public_receipts
    identity_label = (
        "the marked test record" if route == "airtable_context_agent" else "the marked test note"
    )
    public_output = _replace_context_provider_ids(
        public_output,
        provider_ids,
        replacement=identity_label,
    )
    if route == "airtable_context_agent":
        public_output["base_id"] = ""
        public_output["candidate_record_ids"] = []
        public_output["recommended_record_identity"] = (
            "The same marked test record was retained internally through verified cleanup."
        )
        write_plan = public_output.get("write_plan")
        if isinstance(write_plan, dict):
            field_mapping = write_plan.get("field_mapping")
            if isinstance(field_mapping, list):
                write_plan["field_mapping"] = [
                    item
                    for item in field_mapping
                    if not (isinstance(item, dict) and item.get("key") == "record_id")
                ]
    else:
        public_output["zotero_item_keys"] = []
    for receipt in public_receipts:
        if isinstance(receipt, dict) and receipt.get("operation") == lifecycle_operation:
            receipt.pop(identity_field, None)
    return public_output, public_receipts


def _replace_context_provider_ids(
    value: object,
    provider_ids: set[str],
    *,
    replacement: str,
) -> object:
    if isinstance(value, dict):
        return {
            key: _replace_context_provider_ids(
                item,
                provider_ids,
                replacement=replacement,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_context_provider_ids(
                item,
                provider_ids,
                replacement=replacement,
            )
            for item in value
        ]
    if isinstance(value, str):
        cleaned = value
        for provider_id in provider_ids:
            cleaned = cleaned.replace(provider_id, replacement)
        return cleaned
    return value


def _context_agent_external_write_performed(
    tool_receipts: list[dict[str, object]],
) -> bool:
    """Classify successful typed context-agent provider mutations."""

    return any(
        _context_agent_receipt_is_write(receipt)
        and (
            receipt.get("status") == "success"
            or (
                receipt.get("status") in {"partial", "verification_failed"}
                and bool(str(receipt.get("record_id") or "").strip())
            )
        )
        for receipt in tool_receipts
    )


def _airtable_receipt_execution_blocker(
    route: str,
    input_text: str,
    *,
    manual_plan: ManualRequestPlan | None,
    tool_receipts: list[dict[str, object]],
) -> str:
    """Require authoritative create-and-attachment verification for direct receipt asks."""

    if route != "airtable_context_agent":
        return ""
    if manual_plan is None or manual_plan.intent != "business_system_write":
        return ""
    target = resolve_finance_expense_receipt_target(
        input_text,
        manual_plan=manual_plan,
    )
    if target is None:
        return ""
    operation = target.operation if target is not None else "create"
    if operation == "read":
        return ""
    expected_operations = (
        {"update", "reconcile_duplicate_expense"}
        if operation == "update"
        else {"create_expense_from_receipt"}
    )
    receipts = [
        receipt for receipt in tool_receipts if receipt.get("operation") in expected_operations
    ]
    if not receipts:
        return (
            "The approved Airtable receipt operation was not executed. No verified "
            "record-and-attachment receipt was returned."
        )
    if operation == "update" and any(
        receipt.get("operation") == "update"
        and receipt.get("status") == "success"
        and bool(str(receipt.get("record_id") or "").strip())
        and isinstance(receipt.get("verification"), dict)
        and receipt["verification"].get("passed") is True
        and receipt["verification"].get("record_id_match") is True
        and not receipt["verification"].get("mismatched_fields")
        for receipt in receipts
    ):
        return ""
    if operation == "update" and any(
        receipt.get("operation") == "reconcile_duplicate_expense"
        and receipt.get("status") == "success"
        and bool(str(receipt.get("record_id") or "").strip())
        and bool(str(receipt.get("duplicate_record_id") or "").strip())
        and isinstance(receipt.get("verification"), dict)
        and receipt["verification"].get("passed") is True
        and receipt["verification"].get("record_id_match") is True
        and receipt["verification"].get("updated_period") is True
        and receipt["verification"].get("attachment_read_back") is True
        and receipt["verification"].get("duplicate_provider_deleted") is True
        and receipt["verification"].get("duplicate_record_absent_after") is True
        for receipt in receipts
    ):
        return ""
    if operation == "create" and any(
        receipt.get("status") == "success"
        and isinstance(receipt.get("verification"), dict)
        and receipt["verification"].get("passed") is True
        and receipt["verification"].get("create_read_back") is True
        and receipt["verification"].get("attachment_read_back") is True
        for receipt in receipts
    ):
        return ""
    if any(receipt.get("status") == "partial" for receipt in receipts):
        return (
            "The Airtable receipt operation partially completed but did not verify both "
            "the created record and attachment. Do not retry blindly; inspect the exact "
            "provider record first to avoid a duplicate expense."
        )
    reason = next(
        (
            str(receipt.get("reason") or "").strip()
            for receipt in receipts
            if str(receipt.get("reason") or "").strip()
        ),
        "",
    )
    return reason or (
        "The Airtable receipt operation did not return verified record-and-attachment "
        "read-back evidence."
    )


def _airtable_write_execution_blocker(
    route: str,
    input_text: str,
    *,
    manual_plan: ManualRequestPlan | None,
    tool_receipts: list[dict[str, object]],
) -> str:
    """Require a successful typed mutation and provider read-back for direct writes."""

    if route != "airtable_context_agent":
        return ""
    if manual_plan is None or manual_plan.intent != "business_system_write":
        return ""
    if (
        resolve_finance_expense_receipt_target(
            input_text,
            manual_plan=manual_plan,
        )
        is not None
    ):
        return _airtable_receipt_execution_blocker(
            route,
            input_text,
            manual_plan=manual_plan,
            tool_receipts=tool_receipts,
        )
    lifecycle_receipts = [
        receipt for receipt in tool_receipts if receipt.get("operation") == "test_record_lifecycle"
    ]
    if lifecycle_receipts:
        if any(
            receipt.get("status") == "success"
            and isinstance(receipt.get("verification"), dict)
            and receipt["verification"].get("passed") is True
            and receipt["verification"].get("create_read_back") is True
            and receipt["verification"].get("same_record_update_read_back") is True
            and receipt["verification"].get("record_absent_after_cleanup") is True
            for receipt in lifecycle_receipts
        ):
            return ""
        reason = next(
            (
                str(receipt.get("reason") or receipt.get("failure") or "").strip()
                for receipt in lifecycle_receipts
                if str(receipt.get("reason") or receipt.get("failure") or "").strip()
            ),
            "",
        )
        return reason or (
            "The Airtable test-record lifecycle did not verify create, same-record "
            "update, and cleanup absence. Do not retry blindly; inspect the provider "
            "record identity first."
        )
    write_receipts = [
        receipt for receipt in tool_receipts if receipt.get("operation") in {"create", "update"}
    ]
    if not write_receipts:
        return (
            "The approved Airtable write was not executed. No typed provider mutation "
            "receipt was returned."
        )
    if any(
        receipt.get("status") == "success"
        and isinstance(receipt.get("verification"), dict)
        and receipt["verification"].get("passed") is True
        and receipt["verification"].get("record_id_match") is True
        and bool(str(receipt.get("record_id") or "").strip())
        for receipt in write_receipts
    ):
        return ""
    if any(bool(str(receipt.get("record_id") or "").strip()) for receipt in write_receipts):
        return (
            "The Airtable provider mutation returned a record ID but read-after-write "
            "verification did not pass. Do not retry blindly; inspect that exact record "
            "first to avoid a duplicate or conflicting update."
        )
    reason = next(
        (
            str(receipt.get("reason") or "").strip()
            for receipt in write_receipts
            if str(receipt.get("reason") or "").strip()
        ),
        "",
    )
    return reason or (
        "The Airtable write did not return verified provider mutation and read-back evidence."
    )


def _context_agent_receipt_is_write(receipt: dict[str, object]) -> bool:
    """Return whether a bounded tool receipt represents a provider mutation."""

    return receipt_reports_possible_write(receipt)


def _reconcile_context_agent_executed_write_plan(
    output_payload: dict[str, object],
    tool_receipts: list[dict[str, object]],
) -> None:
    """Align a direct-agent write plan with authoritative verified tool execution."""

    verified_write = False
    for receipt in tool_receipts:
        verification = receipt.get("verification")
        if (
            receipt.get("status") == "success"
            and isinstance(verification, dict)
            and verification.get("passed") is True
        ):
            verified_write = True
            break
    write_plan = output_payload.get("write_plan")
    if not verified_write or not isinstance(write_plan, dict):
        if not verified_write:
            return
    if isinstance(write_plan, dict):
        write_plan["live_write_allowed_for_specialist"] = True
        write_plan["approval_reference_needed"] = False
    contradiction = re.compile(
        r"\b(?:not|never|cannot|can't|did\s+not|was\s+not|were\s+not)\b"
        r"[^.;\n]{0,120}\b(?:execute|executed|write|written|create|created|"
        r"update|updated|delete|deleted|reconcile|reconciled|verify|verified)\b"
        r"|\b(?:approval|write)\b[^.;\n]{0,80}\b(?:still|required|blocked)\b",
        re.I,
    )
    for field in ("blockers", "approval_needs", "evidence_gaps"):
        values = output_payload.get(field)
        if not isinstance(values, list):
            continue
        output_payload[field] = [
            item for item in values if not contradiction.search(str(item or ""))
        ]


def _verified_context_agent_write_summary(
    tool_receipts: list[dict[str, object]],
) -> str:
    """Render verified provider execution from receipts, not model speculation."""

    for receipt in reversed(tool_receipts):
        verification = receipt.get("verification")
        if not (
            receipt.get("status") == "success"
            and isinstance(verification, dict)
            and verification.get("passed") is True
            and _context_agent_receipt_is_write(receipt)
        ):
            continue
        operation = str(receipt.get("operation") or "write").replace("_", " ")
        table = str(receipt.get("table") or "").strip()
        record_id = str(receipt.get("record_id") or "").strip()
        duplicate_id = str(receipt.get("duplicate_record_id") or "").strip()
        target = " ".join(part for part in (table, record_id) if part).strip()
        if operation == "reconcile duplicate expense":
            summary = (
                f"Reconciled and provider-verified the exact Airtable expense record "
                f"{record_id or 'requested record'} in place"
            )
            if duplicate_id:
                summary += f"; duplicate {duplicate_id} was removed and verified absent"
            return summary + "."
        if operation in {"update", "update record", "update row"}:
            return (
                f"Updated and provider-verified {target or 'the exact requested record'} in place."
            )
        if operation in {
            "create",
            "create expense from receipt",
            "create sheet",
            "create note",
        }:
            return f"Created and provider-verified {target or 'the exact requested item'}."
        return (
            f"Completed and provider-verified the requested {operation} operation"
            + (f" for {target}" if target else "")
            + "."
        )
    return ""


def _run_ask_company_research_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    execution_context: dict[str, Any] | None = None,
    context_file_path: str = "",
    cost_tracking_requested: bool = False,
    database_url: str | None = None,
) -> int:
    if not live_search_allowed_for_execution(
        True,
        manual_plan=manual_plan,
        request_text=input_text,
    ):
        profile = _direct_specialist_runtime_profile(
            "business_research_analyst",
            input_text=input_text,
            manual_plan=manual_plan,
        )
        if profile["compact_instructions"]:
            target = (manual_plan.primary_target if manual_plan else "") or input_text[:120]
            command = [
                sys.executable,
                "scripts/run_company_research.py",
                "--company",
                target.strip(),
                "--request-text",
                input_text,
                "--inline-source-context",
                input_text,
                "--no-live-search",
                "--no-dry-run",
                "--live-sdk",
                "--focused-brief",
                "--compact-instructions",
                "--json",
            ]
            return _run_ask_script_live(
                "business_research_analyst",
                input_text,
                command,
                json_output=json_output,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                sdk_session_spec=sdk_session_spec,
                execution_context=execution_context,
                cost_tracking_requested=cost_tracking_requested,
                database_url=database_url,
            )
        return _run_ask_work_item(
            input_text,
            database_url=database_url,
            live_search=False,
            live_sdk=True,
            max_results=max(1, min(10, manual_plan.desired_count if manual_plan else 5)),
            json_output=json_output,
            max_manager_steps=3,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            context_file_path=context_file_path,
            sdk_session_enabled=sdk_session_spec.enabled if sdk_session_spec else None,
            sdk_session_id=sdk_session_spec.session_id if sdk_session_spec else "",
            sdk_session_db_path=sdk_session_spec.database_path if sdk_session_spec else "",
            sdk_session_history_limit=sdk_session_spec.history_limit if sdk_session_spec else None,
            cost_tracking_requested=cost_tracking_requested,
        )
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
    quick_retrieval = _direct_company_research_quick_retrieval(manual_plan)
    command = [
        sys.executable,
        "scripts/run_company_research.py",
        "--company",
        company_name,
        "--request-text",
        input_text,
        "--max-results",
        "2" if quick_retrieval else "5",
        "--live-search",
        "--no-dry-run",
        "--live-sdk",
        "--focused-brief",
        "--json",
    ]
    if quick_retrieval:
        command.append("--quick-retrieval")
    else:
        command.append("--live-search-plan")
    _append_compact_direct_flag(
        command,
        route="business_research_analyst",
        input_text=input_text,
        manual_plan=manual_plan,
    )
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
        execution_context=execution_context,
        cost_tracking_requested=cost_tracking_requested,
        database_url=database_url,
    )


def _direct_company_research_quick_retrieval(
    manual_plan: ManualRequestPlan | None,
) -> bool:
    """Use a narrow evidence profile only when the interpreted ask requests it."""

    if manual_plan is None:
        return False
    ask_shape = manual_plan.ask_shape
    if ask_shape.evidence_depth == "deep":
        return False
    if ask_shape.evidence_depth == "quick" or ask_shape.cost_mode == "minimize":
        return True
    constraints = output_constraints_from_plan(manual_plan)
    if constraints.word_count is not None:
        return constraints.word_count <= 100
    compact_source_contract = (
        constraints.source_url_count is not None
        and constraints.source_url_count <= 3
        and (
            str(ask_shape.output_form or "") in {"brief", "bullets"}
            or constraints.maximum_items is not None
            and constraints.maximum_items <= 4
        )
    )
    if compact_source_contract:
        return True
    stop_condition = str(ask_shape.stop_condition or "")
    match = re.search(r"\bstop_after_([1-9]\d{0,3})_word_summary\b", stop_condition)
    return bool(match and int(match.group(1)) <= 100)


def _official_source_response_violations(
    script_payload: dict[str, Any],
    manual_plan: ManualRequestPlan | None,
    response_text: str,
) -> list[str]:
    """Verify repaired visible URLs against the child's official-domain evidence."""

    if manual_plan is None or "official" not in manual_plan.ask_shape.source_type_preference:
        return []
    visible_urls = re.findall(r"https?://[^\s)>]+", str(response_text or ""), flags=re.I)
    if not visible_urls:
        return []
    retrieval = script_payload.get("retrieval")
    official_url = (
        str(retrieval.get("resolved_company_url") or "").strip()
        if isinstance(retrieval, dict)
        else ""
    )
    output = script_payload.get("output")
    if not official_url and isinstance(output, dict):
        official_url = infer_official_company_url(
            company=str(output.get("company_name") or "").strip(),
            search_results=(
                output.get("sources") if isinstance(output.get("sources"), list) else []
            ),
        )
    if not official_url:
        return ["official company source domain could not be verified"]
    if any(
        not company_source_matches_official_url(url, official_url)
        for url in visible_urls
    ):
        return ["visible source URL is outside the verified official company domain"]
    return []


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
    execution_context: dict[str, Any] | None = None,
    context_file_path: str = "",
    cost_tracking_requested: bool = False,
    database_url: str | None = None,
) -> int:
    command = [
        sys.executable,
        "scripts/run_chief_of_staff.py",
        "--input",
        input_text,
        "--live-sdk",
        "--json",
    ]
    if manual_plan is not None and manual_plan.requires_live_search:
        command.extend(["--live-search", "--live-search-plan"])
    if (
        manual_plan is not None
        and manual_plan.provider_system != "unspecified"
        and not manual_plan.requires_live_search
    ):
        command.extend(["--quality", "fast"])
    return _run_ask_script_live(
        "chief_of_staff",
        input_text,
        command,
        json_output=json_output,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        sdk_session_spec=sdk_session_spec,
        execution_context=execution_context,
        cost_tracking_requested=cost_tracking_requested,
        database_url=database_url,
    )


def _request_forbids_live_research(text: str) -> bool:
    return request_forbids_live_research(text)


def _run_ask_opportunity_scout_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    execution_context: dict[str, Any] | None = None,
    context_file_path: str = "",
    cost_tracking_requested: bool = False,
    database_url: str | None = None,
) -> int:
    max_results = manual_plan.desired_count if manual_plan else 3
    if not live_search_allowed_for_execution(
        True,
        manual_plan=manual_plan,
        request_text=input_text,
    ):
        profile = _direct_specialist_runtime_profile(
            "opportunity_scout",
            input_text=input_text,
            manual_plan=manual_plan,
        )
        if profile["compact_instructions"]:
            command = [
                sys.executable,
                "scripts/run_compact_opportunity_assessment.py",
                "--request-text",
                input_text,
                "--inline-source-context",
                input_text,
                "--model",
                get_runtime_agent_model_config("opportunity_scout").model,
                "--max-openai-requests",
                "1",
                "--budget-usd",
                "0.10",
                "--no-save-output",
            ]
            return _run_ask_script_live(
                "opportunity_scout",
                input_text,
                command,
                json_output=json_output,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                sdk_session_spec=sdk_session_spec,
                execution_context=execution_context,
                cost_tracking_requested=cost_tracking_requested,
                database_url=database_url,
            )
        return _run_ask_work_item(
            input_text,
            database_url=database_url,
            live_search=False,
            live_sdk=True,
            max_results=max(1, min(10, max_results)),
            json_output=json_output,
            max_manager_steps=3,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            context_file_path=context_file_path,
            sdk_session_enabled=sdk_session_spec.enabled if sdk_session_spec else None,
            sdk_session_id=sdk_session_spec.session_id if sdk_session_spec else "",
            sdk_session_db_path=sdk_session_spec.database_path if sdk_session_spec else "",
            sdk_session_history_limit=sdk_session_spec.history_limit if sdk_session_spec else None,
            cost_tracking_requested=cost_tracking_requested,
        )
    command = [
        sys.executable,
        "scripts/run_opportunity_scout.py",
        "--topic",
        input_text,
        "--max-results",
        str(max(1, min(10, max_results))),
        "--live-search",
        "--no-dry-run",
        "--live-sdk",
        "--json",
    ]
    _append_compact_direct_flag(
        command,
        route="opportunity_scout",
        input_text=input_text,
        manual_plan=manual_plan,
    )
    return _run_ask_script_live(
        "opportunity_scout",
        input_text,
        command,
        json_output=json_output,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        sdk_session_spec=sdk_session_spec,
        execution_context=execution_context,
        cost_tracking_requested=cost_tracking_requested,
        database_url=database_url,
    )


def _run_ask_gmail_triage_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    execution_context: dict[str, Any] | None = None,
    context_file_path: str = "",
    cost_tracking_requested: bool = False,
    database_url: str | None = None,
) -> int:
    if _is_bounded_composite_lifecycle_request(
        "gmail_triage",
        input_text=input_text,
        manual_plan=manual_plan,
    ):
        return _run_direct_gmail_test_draft_lifecycle(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            database_url=database_url,
        )
    gmail_plan = resolve_gmail_execution_plan(
        input_text,
        manual_plan=manual_plan,
    )
    thread_execution_text = specialist_execution_context_text(execution_context)
    if gmail_plan.operation == "update_draft" and not (
        gmail_plan.draft_subject_hint or gmail_plan.draft_recipient_hint
    ):
        resolved_subject = _direct_context_object_title(
            execution_context,
            preferred_route="gmail_triage",
        )
        if resolved_subject:
            gmail_plan = gmail_plan.model_copy(
                update={
                    "source": "orchestrator_thread_context",
                    "draft_subject_hint": resolved_subject,
                    "planner_warnings": [],
                    "rationale": (
                        f"{gmail_plan.rationale} The bounded direct-call context resolved "
                        "the draft subject before exact provider matching."
                    ),
                }
            )
    explicit_fixture_path = _gmail_direct_fixture_path(input_text)
    inline_fixture = inline_gmail_fixture_from_request(input_text)
    if gmail_plan.operation == "clarification":
        return _print_ask_clarification(
            "gmail_triage",
            input_text,
            gmail_plan.rationale,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            extra={
                "status": "clarification_required",
                "block_kind": "gmail_execution_not_authorized",
                "send_enabled": False,
                "agent_execution_plan": gmail_plan.model_dump(mode="json"),
            },
        )
    if inline_fixture is not None and _gmail_inline_request_needs_work_item_graph(
        input_text,
        manual_plan=manual_plan,
    ):
        return _run_ask_work_item(
            input_text,
            database_url=database_url,
            live_search=False,
            live_sdk=True,
            max_results=3,
            json_output=json_output,
            max_manager_steps=3,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            context_file_path=context_file_path,
            sdk_session_enabled=sdk_session_spec.enabled if sdk_session_spec else None,
            sdk_session_id=sdk_session_spec.session_id if sdk_session_spec else "",
            sdk_session_db_path=sdk_session_spec.database_path if sdk_session_spec else "",
            sdk_session_history_limit=sdk_session_spec.history_limit if sdk_session_spec else None,
            cost_tracking_requested=cost_tracking_requested,
        )
    if gmail_plan.operation == "update_draft":
        if not (gmail_plan.draft_subject_hint or gmail_plan.draft_recipient_hint):
            return _print_ask_clarification(
                "gmail_triage",
                input_text,
                (
                    "Name the Gmail draft by subject and/or recipient so exactly one "
                    "provider draft can be resolved before modification."
                ),
                json_output=json_output,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                extra={
                    "status": "clarification_required",
                    "block_kind": "missing_gmail_draft_reference",
                    "send_enabled": False,
                    "agent_execution_plan": gmail_plan.model_dump(mode="json"),
                },
            )
        expected_account = _configured_gmail_draft_account()
        if not expected_account:
            return _print_ask_clarification(
                "gmail_triage",
                input_text,
                "Gmail draft modification requires a configured target Gmail account.",
                json_output=json_output,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                extra={
                    "status": "blocked",
                    "block_kind": "missing_gmail_draft_account",
                    "send_enabled": False,
                    "agent_execution_plan": gmail_plan.model_dump(mode="json"),
                },
            )
        command = [
            sys.executable,
            "scripts/run_gmail_triage.py",
            "--live-gmail",
            "--allow-inbox",
            "--no-dry-run",
            "--live-sdk",
            "--json",
            "--request",
            input_text,
            "--update-draft",
            "--approval-reference",
            _gmail_operator_approval_reference(input_text),
            "--expected-account",
            expected_account,
        ]
        _append_compact_direct_flag(
            command,
            route="gmail_triage",
            input_text=input_text,
            manual_plan=manual_plan,
        )
        if gmail_plan.draft_subject_hint:
            command.extend(["--draft-subject-hint", gmail_plan.draft_subject_hint])
        if gmail_plan.draft_recipient_hint:
            command.extend(["--draft-recipient-hint", gmail_plan.draft_recipient_hint])
        return _run_ask_script_live(
            "gmail_triage",
            input_text,
            command,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    if gmail_plan.operation == "contact_lookup" and gmail_plan.live_read_required:
        command = [
            sys.executable,
            "scripts/run_gmail_triage.py",
            "--live-gmail",
            "--allow-inbox",
            "--no-dry-run",
            "--live-sdk",
            "--json",
            "--request",
            input_text,
            "--contact-lookup",
            "--max-messages",
            str(gmail_plan.max_messages),
        ]
        _append_compact_direct_flag(
            command,
            route="gmail_triage",
            input_text=input_text,
            manual_plan=manual_plan,
        )
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
            execution_context=execution_context,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    if gmail_plan.operation == "message_projection" and gmail_plan.live_read_required:
        projection_scope = gmail_provider_read_scope(gmail_plan)
        command = [
            sys.executable,
            "scripts/run_gmail_triage.py",
            "--live-gmail",
            "--allow-inbox",
            "--no-dry-run",
            "--json",
            "--request",
            input_text,
            "--message-projection",
            "--mailbox-direction",
            gmail_plan.mailbox_direction,
            "--date-scope",
            gmail_plan.date_scope,
            "--provider-timezone",
            projection_scope["timezone"],
            "--max-messages",
            str(gmail_plan.max_messages),
        ]
        for requested_field in gmail_plan.requested_fields:
            command.extend(["--requested-field", requested_field])
        if projection_scope["query"]:
            command.extend(["--gmail-query", projection_scope["query"]])
        if projection_scope["label"]:
            command.extend(["--label-filter", projection_scope["label"]])
        if projection_scope["window_start"]:
            command.extend(["--window-start", projection_scope["window_start"]])
        if projection_scope["window_end"]:
            command.extend(["--window-end", projection_scope["window_end"]])
        if gmail_plan.expected_result_count is not None:
            command.extend(["--expected-result-count", str(gmail_plan.expected_result_count)])
        return _run_ask_script_live(
            "gmail_triage",
            input_text,
            command,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    if gmail_plan.operation == "message_count" and gmail_plan.live_read_required:
        count_scope = gmail_message_count_scope(gmail_plan)
        command = [
            sys.executable,
            "scripts/run_gmail_triage.py",
            "--live-gmail",
            "--allow-inbox",
            "--no-dry-run",
            "--json",
            "--request",
            input_text,
            "--message-count",
            "--mailbox-direction",
            gmail_plan.mailbox_direction,
            "--date-scope",
            gmail_plan.date_scope,
            "--provider-timezone",
            count_scope["timezone"],
        ]
        if count_scope["query"]:
            command.extend(["--gmail-query", count_scope["query"]])
        if count_scope["label"]:
            command.extend(["--label-filter", count_scope["label"]])
        if count_scope["window_start"]:
            command.extend(["--window-start", count_scope["window_start"]])
        if count_scope["window_end"]:
            command.extend(["--window-end", count_scope["window_end"]])
        return _run_ask_script_live(
            "gmail_triage",
            input_text,
            command,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
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
        _append_compact_direct_flag(
            command,
            route="gmail_triage",
            input_text=input_text,
            manual_plan=manual_plan,
        )
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
            execution_context=execution_context,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    if (
        gmail_plan.operation in {"single_message_triage", "thread_summary"}
        and gmail_plan.live_read_required
        and explicit_fixture_path is None
        and inline_fixture is None
        and not thread_execution_text
    ):
        command = [
            sys.executable,
            "scripts/run_gmail_triage.py",
            "--live-gmail",
            "--allow-inbox",
            "--no-dry-run",
            "--live-sdk",
            "--json",
            "--request",
            input_text,
            "--max-messages",
            str(gmail_plan.max_messages),
        ]
        if gmail_plan.operation == "thread_summary":
            command.append("--thread-summary")
        if gmail_plan.gmail_query:
            command.extend(["--gmail-query", gmail_plan.gmail_query])
        _append_compact_direct_flag(
            command,
            route="gmail_triage",
            input_text=input_text,
            manual_plan=manual_plan,
        )
        return _run_ask_script_live(
            "gmail_triage",
            input_text,
            command,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    if (
        gmail_plan.operation == "draft_reply"
        and explicit_fixture_path is None
        and inline_fixture is None
        and _gmail_query_has_specific_target(gmail_plan.gmail_query)
    ):
        command = [
            sys.executable,
            "scripts/run_gmail_triage.py",
            "--live-gmail",
            "--allow-inbox",
            "--no-dry-run",
            "--live-sdk",
            "--json",
            "--request",
            input_text,
            "--gmail-query",
            gmail_plan.gmail_query,
            "--max-messages",
            "1",
        ]
        _append_compact_direct_flag(
            command,
            route="gmail_triage",
            input_text=input_text,
            manual_plan=manual_plan,
        )
        if gmail_plan.create_gmail_drafts:
            expected_account = _configured_gmail_draft_account()
            if not expected_account:
                return _print_ask_clarification(
                    "gmail_triage",
                    input_text,
                    "Gmail draft creation requires a configured target Gmail account.",
                    json_output=json_output,
                    manual_plan=manual_plan,
                    orchestrator_preflight=orchestrator_preflight,
                    extra={
                        "status": "blocked",
                        "block_kind": "missing_gmail_draft_account",
                        "send_enabled": False,
                        "agent_execution_plan": gmail_plan.model_dump(mode="json"),
                    },
                )
            command.extend(
                [
                    "--create-draft",
                    "--approval-reference",
                    _gmail_operator_approval_reference(input_text),
                    "--expected-account",
                    expected_account,
                ]
            )
        return _run_ask_script_live(
            "gmail_triage",
            input_text,
            command,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            sdk_session_spec=sdk_session_spec,
            execution_context=execution_context,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    if (
        gmail_plan.operation == "draft_reply"
        and explicit_fixture_path is None
        and inline_fixture is None
        and not thread_execution_text
    ):
        return _print_ask_clarification(
            "gmail_triage",
            input_text,
            (
                "Gmail Triage needs usable email context before drafting a reply: a pasted "
                "sanitized email, selected message/thread, explicit fixture, or approved "
                "bounded read-only Gmail retrieval scope."
            ),
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            extra={
                "status": "blocked",
                "block_kind": "missing_gmail_context",
                "requires_gmail_context": True,
                "recommended_next_action": (
                    "Paste the sanitized email, select the Gmail message/thread, provide an "
                    "explicit fixture path, or approve bounded read-only retrieval; then rerun "
                    "Gmail Triage."
                ),
                "agent_execution_plan": gmail_plan.model_dump(mode="json"),
            },
        )

    temp_path: Path | None = None
    if explicit_fixture_path is not None:
        selected_fixture = str(explicit_fixture_path)
    elif inline_fixture is not None:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".txt",
            prefix="keystone-gmail-inline-",
            delete=False,
        ) as tmp:
            tmp.write(f"Subject: {inline_fixture.subject}\n\n")
            tmp.write(inline_fixture.body)
            temp_path = Path(tmp.name)
        selected_fixture = str(temp_path)
    elif thread_execution_text:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".txt",
            prefix="keystone-gmail-thread-context-",
            delete=False,
        ) as tmp:
            tmp.write("Subject: Selected Slack thread Gmail context\n\n")
            tmp.write(thread_execution_text)
            temp_path = Path(tmp.name)
        selected_fixture = str(temp_path)
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
        "--no-live-gmail",
        "--no-allow-inbox",
        "--request",
        input_text,
        "--live-sdk",
        "--json",
    ]
    _append_compact_direct_flag(
        command,
        route="gmail_triage",
        input_text=input_text,
        manual_plan=manual_plan,
    )
    if inline_fixture is not None and explicit_fixture_path is None:
        if inline_fixture.subject:
            command.extend(["--subject", inline_fixture.subject])
        if inline_fixture.sender_name:
            command.extend(["--sender-name", inline_fixture.sender_name])
        if inline_fixture.sender_email:
            command.extend(["--sender-email", inline_fixture.sender_email])
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
            execution_context=execution_context,
            agent_execution_plan=gmail_plan.model_dump(mode="json"),
            cost_tracking_requested=cost_tracking_requested,
            database_url=database_url,
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


_PUBLIC_LIFECYCLE_RECEIPT_PRIVATE_KEYS = frozenset(
    {
        "approval_reference",
        "document_id",
        "draft_id",
        "message_id",
        "record_id",
        "thread_id",
        "url",
        "provider_link",
    }
)


def _public_lifecycle_receipt(value: object) -> object:
    """Keep provider verification visible without publishing object identifiers."""

    if isinstance(value, dict):
        return {
            str(key): _public_lifecycle_receipt(item)
            for key, item in value.items()
            if str(key) not in _PUBLIC_LIFECYCLE_RECEIPT_PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [_public_lifecycle_receipt(item) for item in value]
    return value


def _provider_lifecycle_approval_reference(route: str, input_text: str) -> str:
    digest = hashlib.sha256(str(input_text).strip().encode("utf-8")).hexdigest()[:16]
    scope = str(route or "provider").replace("_context_agent", "").replace("_", "-")
    return f"operator-command:{scope}-test-lifecycle:{digest}"


def _persist_direct_provider_lifecycle_run(
    *,
    route: str,
    input_text: str,
    payload: dict[str, object],
    internal_receipt: dict[str, Any],
    passed: bool,
    database_url: str | None,
) -> None:
    """Persist the full local audit receipt while keeping the public payload redacted."""

    stored_payload = dict(payload)
    stored_payload["tool_receipt"] = internal_receipt
    try:
        run_id = SQLiteStore(database_url or database_url_from_env()).save_agent_run(
            agent_name=route,
            input_payload={"request_text": input_text, "route": route},
            input_summary=input_text[:500],
            output=stored_payload,
            model="direct-provider-after-orchestrator-preflight",
            dry_run=False,
            status="success" if passed else "blocked",
        )
        payload["agent_run_id"] = run_id
    except Exception as exc:  # pragma: no cover - diagnostic metadata only
        payload["agent_run_persistence_error"] = f"{type(exc).__name__}: {exc}"


def _run_direct_airtable_test_record_lifecycle(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    database_url: str | None,
) -> int:
    """Execute one guarded Airtable test-record lifecycle after interpretation."""

    approval_reference = _provider_lifecycle_approval_reference(
        "airtable_context_agent",
        input_text,
    )
    try:
        result = airtable_test_record_lifecycle_impl(
            table="Business Expenses",
            base_alias="finance_tax_tracker",
            approval_reference=approval_reference,
            live=True,
        )
    except (RuntimeError, ValueError) as exc:
        result = {
            "status": "blocked",
            "operation": "test_record_lifecycle",
            "failure": str(exc),
            "verification": {"passed": False},
            "send_enabled": False,
        }
    verification = result.get("verification")
    passed = bool(
        result.get("status") == "success"
        and isinstance(verification, dict)
        and verification.get("passed") is True
        and verification.get("create_read_back") is True
        and verification.get("same_record_update_read_back") is True
        and verification.get("record_absent_after_cleanup") is True
    )
    if passed:
        summary = (
            "Airtable test-record lifecycle completed: one marked record was created "
            "and read back, updated in place and read back again, then deleted and "
            "verified absent. No schema or other record was changed."
        )
    else:
        summary = str(result.get("failure") or "").strip() or (
            "The Airtable test-record lifecycle did not verify create, same-record "
            "update, and cleanup absence. Inspect the local provider receipt before "
            "any retry."
        )
    operator_output = AirtableContextResult(
        mode="deterministic",
        summary=summary,
        base_alias="finance_tax_tracker",
        relevant_tables=["Business Expenses"],
        recommended_actions=(
            [] if passed else ["Review the exact blocker and provider receipt before any retry."]
        ),
        blockers=[] if passed else [summary],
    ).model_dump(mode="json")
    payload: dict[str, object] = {
        "mode": "live_sdk",
        "status": "done" if passed else "blocked",
        "block_kind": "" if passed else "airtable_test_record_lifecycle_unverified",
        "selected_agent": "airtable_context_agent",
        "route": "airtable_context_agent",
        "agent_name": _agent_display_name("airtable_context_agent"),
        "output_type": "AirtableContextResult",
        "output": operator_output,
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "human_summary": summary,
        "slack_display_title": (
            "Business Agents Airtable Test Lifecycle Complete"
            if passed
            else "Business Agents Airtable Test Lifecycle Blocked"
        ),
        "slack_display_text": summary,
        "tool_receipt": _public_lifecycle_receipt(result),
        "provider_identity_redacted": True,
        "openai_requests": _orchestrator_preflight_request_count(orchestrator_preflight),
        "side_effects": {
            "airtable_test_record_created_and_verified": bool(
                isinstance(verification, dict) and verification.get("create_read_back") is True
            ),
            "airtable_test_record_absent_after_cleanup": bool(
                isinstance(verification, dict)
                and verification.get("record_absent_after_cleanup") is True
            ),
            "slack_message_posted": False,
        },
    }
    _persist_direct_provider_lifecycle_run(
        route="airtable_context_agent",
        input_text=input_text,
        payload=payload,
        internal_receipt=result,
        passed=passed,
        database_url=database_url,
    )
    return _print_ask_live_payload(payload, json_output=json_output)


def _google_doc_lifecycle_scope(
    input_text: str,
    *,
    context_text: str,
) -> tuple[str, str, str]:
    """Resolve exact marked Doc scope, including a prior body named as "same body"."""

    combined = f"{input_text}\n{context_text}"
    marker_match = re.search(
        r"\bKBA_TEST_DOC(?:_[A-Za-z0-9]+)*\b",
        input_text,
        re.IGNORECASE,
    ) or re.search(
        r"\bKBA_TEST_DOC(?:_[A-Za-z0-9]+)*\b",
        context_text,
        re.IGNORECASE,
    )
    body_matches = re.findall(
        r"\b(?:body|sentence|text|content)"
        r"(?:\s+(?:is|says?|reads?|that\s+says))?\s*:?\s*"
        r"(?:to|with)?\s*"
        r"[\"“'‘]([^\"”'’]+)[\"”'’]",
        combined,
        flags=re.IGNORECASE,
    )
    title = marker_match.group(0).upper() if marker_match else ""
    body = " ".join(body_matches[-1].split()) if body_matches else ""
    folder = "KNIOps" if re.search(r"\bKNIOps\b", combined, re.IGNORECASE) else ""
    return title, body, folder


def _google_doc_lifecycle_bodies(
    input_text: str,
    *,
    context_text: str,
) -> tuple[str, str]:
    """Return the original and optional revised bodies from bounded operator text."""

    combined = f"{input_text}\n{context_text}"
    matches = [
        " ".join(value.split())
        for value in re.findall(
            r"\b(?:body|sentence|text|content)"
            r"(?:\s+(?:is|says?|reads?|that\s+says))?\s*:?\s*"
            r"(?:to|with)?\s*"
            r"[\"“'‘]([^\"”'’]+)[\"”'’]",
            combined,
            flags=re.IGNORECASE,
        )
        if value.strip()
    ]
    if not matches:
        return "", ""
    original = matches[0]
    revised = matches[-1] if len(matches) > 1 and matches[-1] != original else ""
    return original, revised


def _run_direct_google_doc_test_lifecycle(
    input_text: str,
    *,
    context_text: str,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    database_url: str | None,
) -> int:
    """Execute one guarded Google Doc lifecycle after interpretation."""

    title, fallback_body_text, folder_path = _google_doc_lifecycle_scope(
        input_text,
        context_text=context_text,
    )
    body_text, updated_body_text = _google_doc_lifecycle_bodies(
        input_text,
        context_text=context_text,
    )
    body_text = body_text or fallback_body_text
    approval_reference = _provider_lifecycle_approval_reference(
        "google_workspace_context_agent",
        input_text,
    )
    if not title or not body_text or not folder_path:
        result: dict[str, Any] = {
            "status": "blocked",
            "operation": "test_doc_lifecycle",
            "failure": (
                "The Google Doc test lifecycle needs an exact KBA_TEST_DOC title, "
                "bounded body text, and the KNIOps folder. A thread follow-up may "
                "refer to the same body when the earlier request contains it."
            ),
            "verification": {"passed": False},
            "send_enabled": False,
        }
    else:
        try:
            result = google_doc_test_lifecycle_impl(
                title,
                body_text,
                updated_body_text=updated_body_text,
                folder_path=folder_path,
                approval_reference=approval_reference,
                live=True,
            )
        except (RuntimeError, ValueError) as exc:
            result = {
                "status": "blocked",
                "operation": "test_doc_lifecycle",
                "failure": str(exc),
                "verification": {"passed": False},
                "send_enabled": False,
            }
    verification = result.get("verification")
    passed = bool(
        result.get("status") == "success"
        and isinstance(verification, dict)
        and verification.get("passed") is True
        and verification.get("create_read_back") is True
        and verification.get("document_trashed_after_cleanup") is True
    )
    if passed:
        if updated_body_text:
            summary = (
                "Google Doc test lifecycle completed: the exact marked document was "
                "created in KNIOps and read back, the same document was updated and "
                "verified, then it was moved to Drive trash and verified trashed."
            )
        else:
            summary = (
                "Google Doc test lifecycle completed: the exact marked document was "
                "created in KNIOps and read back, then that same document was moved to "
                "Drive trash and verified trashed."
            )
    else:
        summary = str(result.get("failure") or "").strip() or (
            "The Google Doc test lifecycle did not verify both creation and cleanup. "
            "Inspect the local provider receipt before any retry."
        )
    operator_output = GoogleWorkspaceContextResult(
        mode="deterministic",
        summary=summary,
        relevant_folders=[folder_path] if folder_path else [],
        relevant_docs=[title] if title else [],
        recommended_target=title,
        recommended_actions=(
            [] if passed else ["Review the exact blocker and provider receipt before any retry."]
        ),
        blockers=[] if passed else [summary],
    ).model_dump(mode="json")
    payload: dict[str, object] = {
        "mode": "live_sdk",
        "status": "done" if passed else "blocked",
        "block_kind": "" if passed else "google_doc_test_lifecycle_unverified",
        "selected_agent": "google_workspace_context_agent",
        "route": "google_workspace_context_agent",
        "agent_name": _agent_display_name("google_workspace_context_agent"),
        "output_type": "GoogleWorkspaceContextResult",
        "output": operator_output,
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "human_summary": summary,
        "slack_display_title": (
            "Business Agents Google Doc Test Lifecycle Complete"
            if passed
            else "Business Agents Google Doc Test Lifecycle Blocked"
        ),
        "slack_display_text": summary,
        "tool_receipt": _public_lifecycle_receipt(result),
        "provider_identity_redacted": True,
        "openai_requests": _orchestrator_preflight_request_count(orchestrator_preflight),
        "side_effects": {
            "google_doc_created_and_verified": bool(
                isinstance(verification, dict) and verification.get("create_read_back") is True
            ),
            "google_doc_modified_and_verified": bool(
                isinstance(verification, dict)
                and verification.get("same_document_update_read_back") is True
            ),
            "google_doc_trashed_after_cleanup": bool(
                isinstance(verification, dict)
                and verification.get("document_trashed_after_cleanup") is True
            ),
            "slack_message_posted": False,
        },
    }
    _persist_direct_provider_lifecycle_run(
        route="google_workspace_context_agent",
        input_text=input_text,
        payload=payload,
        internal_receipt=result,
        passed=passed,
        database_url=database_url,
    )
    return _print_ask_live_payload(payload, json_output=json_output)


def _run_direct_gmail_test_draft_lifecycle(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    database_url: str | None,
) -> int:
    """Execute one exact marked Gmail draft lifecycle after LLM preflight."""

    account = _configured_gmail_draft_account()
    marker_match = re.search(
        r"\bKBA_TEST_DRAFT(?:_[A-Za-z0-9]+)*\b",
        input_text,
        re.IGNORECASE,
    )
    marker = marker_match.group(0).upper() if marker_match else ""
    if not account or not marker:
        return _print_ask_clarification(
            "gmail_triage",
            input_text,
            (
                "The Gmail test-draft lifecycle requires a configured draft account "
                f"and an exact {GMAIL_TEST_DRAFT_MARKER} marker."
            ),
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            extra={
                "status": "blocked",
                "block_kind": "gmail_test_draft_scope_missing",
                "send_enabled": False,
            },
        )
    approval_reference = _gmail_operator_approval_reference(input_text)
    try:
        result = execute_gmail_test_draft_lifecycle(
            GmailTool(live=True),
            marker=marker,
            expected_account=account,
            recipient=account,
            approval_reference=approval_reference,
        )
    except (RuntimeError, ValueError) as exc:
        result = {
            "status": "blocked",
            "operation": "test_draft_lifecycle",
            "failure": str(exc),
            "verification": {"passed": False},
            "sent": False,
            "send_enabled": False,
        }
    verification = result.get("verification")
    passed = bool(
        result.get("status") == "success"
        and isinstance(verification, dict)
        and verification.get("passed") is True
        and verification.get("create_read_back") is True
        and verification.get("same_draft_update_read_back") is True
        and verification.get("draft_absent_after_cleanup") is True
    )
    if passed:
        summary = (
            "Gmail test draft lifecycle completed: the marked draft was created, "
            "read back, updated in place, read back again, deleted, and verified absent. "
            "No email was sent."
        )
    else:
        summary = str(result.get("failure") or "").strip() or (
            "The Gmail test-draft lifecycle did not verify create, same-draft update, "
            "and cleanup absence. Do not retry blindly; inspect the exact provider "
            "draft identity first."
        )
    operator_output = EmailTriageResult(
        subject=marker or GMAIL_TEST_DRAFT_MARKER,
        category="unrelated",
        confidence=1.0,
        priority="low",
        summary=summary,
        reasoning=(
            "This result reports a bounded marked Gmail test-draft provider lifecycle; "
            "it does not classify or send an inbound email."
        ),
        needs_reply=False,
        recommended_action=(
            "No further provider action is required."
            if passed
            else "Review the exact blocker and provider receipt before any retry."
        ),
        triage_limitations=[
            "This is a provider lifecycle receipt, not an inbound-email triage result."
        ],
        draft_reply=None,
        draft_created=False,
        approval_required=False,
        requires_human_review=not passed,
    ).model_dump(mode="json")
    payload: dict[str, object] = {
        "mode": "live_sdk",
        "status": "done" if passed else "blocked",
        "block_kind": "" if passed else "gmail_test_draft_lifecycle_unverified",
        "selected_agent": "gmail_triage",
        "route": "gmail_triage",
        "agent_name": _agent_display_name("gmail_triage"),
        "output_type": "EmailTriageResult",
        "output": operator_output,
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "human_summary": summary,
        "slack_display_title": (
            "Business Agents Gmail Draft Test Lifecycle Complete"
            if passed
            else "Business Agents Gmail Draft Test Lifecycle Blocked"
        ),
        "slack_display_text": summary,
        "tool_receipt": _public_lifecycle_receipt(result),
        "provider_identity_redacted": True,
        "openai_requests": _orchestrator_preflight_request_count(orchestrator_preflight),
        "side_effects": {
            "gmail_draft_created": passed,
            "gmail_draft_absent_after_cleanup": bool(
                isinstance(verification, dict)
                and verification.get("draft_absent_after_cleanup") is True
            ),
            "email_sent": False,
            "slack_message_posted": False,
        },
    }
    _persist_direct_provider_lifecycle_run(
        route="gmail_triage",
        input_text=input_text,
        payload=payload,
        internal_receipt=result,
        passed=passed,
        database_url=database_url,
    )
    return _print_ask_live_payload(payload, json_output=json_output)


def _orchestrator_preflight_request_count(
    preflight: OrchestratorPreflight | None,
) -> int:
    payload = compact_orchestrator_preflight_payload(preflight)
    events = payload.get("sdk_usage_events") if isinstance(payload, dict) else None
    total = 0
    for event in events if isinstance(events, list) else []:
        usage = event.get("usage") if isinstance(event, dict) else None
        if isinstance(usage, dict):
            total += int(usage.get("requests") or 0)
    return total


def _direct_context_object_title(
    execution_context: dict[str, Any] | None,
    *,
    preferred_route: str,
) -> str:
    """Resolve a prior typed object title without exposing or guessing provider IDs."""

    if not execution_context:
        return ""
    prior_runs = execution_context.get("prior_agent_runs")
    if not isinstance(prior_runs, list):
        return ""
    for item in reversed(prior_runs):
        if not isinstance(item, dict):
            continue
        title = _bounded_redacted_text(item.get("title"), max_chars=240)
        if not title:
            continue
        route = str(item.get("route") or "")
        if route in {"", preferred_route}:
            return title
    return ""


def _gmail_query_has_specific_target(query: str) -> bool:
    remainder = re.sub(
        r"^(?:newer_than:\d+d|after:\d{4}/\d{2}/\d{2})\s*",
        "",
        str(query or "").strip(),
    )
    return bool(remainder.strip())


def _configured_gmail_draft_account() -> str:
    for key in (
        "KEYSTONE_GMAIL_DRAFT_ACCOUNT",
        "KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT",
    ):
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _gmail_operator_approval_reference(input_text: str) -> str:
    digest = hashlib.sha256(str(input_text).strip().encode("utf-8")).hexdigest()[:16]
    return f"operator-command:gmail-draft:{digest}"


def _gmail_direct_fixture_path(input_text: str) -> Path | None:
    if not input_text or "\n" in input_text:
        return None
    try:
        path = Path(input_text).expanduser()
        return path if path.is_file() else None
    except (OSError, RuntimeError):
        return None


def _gmail_inline_request_needs_work_item_graph(
    input_text: str,
    *,
    manual_plan: ManualRequestPlan | None = None,
) -> bool:
    """Return whether inline Gmail context asks for durable downstream work."""

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.canonical:
        assert authority.plan is not None
        plan = authority.plan
        downstream_routes = {route for route in plan.workflow if route != "gmail_triage"}
        return bool(plan.requires_durable_state or downstream_routes)
    if authority.invalid:
        return False
    normalized = " ".join(str(input_text or "").lower().split())
    if not normalized:
        return False
    has_downstream_research = bool(
        re.search(r"\b(?:research|source-backed|source backed|company profile)\b", normalized)
    )
    has_downstream_outreach = bool(
        re.search(r"\b(?:outreach|slack-thread sample|sample outreach)\b", normalized)
    )
    return has_downstream_research or has_downstream_outreach


def _run_ask_outreach_composer_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    orchestrator_preflight: OrchestratorPreflight | None,
    sdk_session_spec: SDKSessionSpec | None,
    execution_context: dict[str, Any] | None = None,
    context_file_path: str = "",
    cost_tracking_requested: bool = False,
    database_url: str | None = None,
) -> int:
    if manual_plan is not None:
        # A canonical semantic plan has already separated internal response
        # composition from external outreach. Do not let the legacy phrase
        # heuristic become a second intent classifier at execution time.
        if is_internal_slack_composition_plan(manual_plan):
            return _run_direct_supplied_context_response_live(
                "outreach_composer",
                input_text,
                json_output=json_output,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                sdk_session_spec=sdk_session_spec,
                execution_context=execution_context,
                database_url=database_url,
            )
        approved_context_present = bool(
            orchestrator_preflight and orchestrator_preflight.route_result.approved_context_present
        )
        if approved_context_present:
            return _run_ask_work_item(
                input_text,
                database_url=database_url,
                live_search=False,
                live_sdk=True,
                max_results=3,
                json_output=json_output,
                max_manager_steps=3,
                manual_plan=manual_plan,
                orchestrator_preflight=orchestrator_preflight,
                context_file_path=context_file_path,
                sdk_session_enabled=sdk_session_spec.enabled if sdk_session_spec else None,
                sdk_session_id=sdk_session_spec.session_id if sdk_session_spec else "",
                sdk_session_db_path=sdk_session_spec.database_path if sdk_session_spec else "",
                sdk_session_history_limit=(
                    sdk_session_spec.history_limit if sdk_session_spec else None
                ),
                cost_tracking_requested=cost_tracking_requested,
            )
        return _print_ask_outreach_context_blocked(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            extra={
                "execution_contract_source": "manual_request_plan",
                "approved_context_present": False,
            },
        )

    # Compatibility-only fallback for callers that have not yet adopted the
    # Orchestrator-first typed planning contract.
    outreach_plan = infer_outreach_execution_plan(input_text)
    if outreach_plan.approved_inline_context_available:
        return _run_ask_work_item(
            input_text,
            database_url=database_url,
            live_search=False,
            live_sdk=True,
            max_results=3,
            json_output=json_output,
            max_manager_steps=3,
            manual_plan=manual_plan,
            orchestrator_preflight=orchestrator_preflight,
            context_file_path=context_file_path,
            sdk_session_enabled=sdk_session_spec.enabled if sdk_session_spec else None,
            sdk_session_id=sdk_session_spec.session_id if sdk_session_spec else "",
            sdk_session_db_path=sdk_session_spec.database_path if sdk_session_spec else "",
            sdk_session_history_limit=sdk_session_spec.history_limit if sdk_session_spec else None,
            cost_tracking_requested=cost_tracking_requested,
        )
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
    _append_compact_direct_flag(
        command,
        route="outreach_composer",
        input_text=input_text,
        manual_plan=manual_plan,
    )
    return _run_ask_script_live(
        "outreach_composer",
        input_text,
        command,
        json_output=json_output,
        manual_plan=manual_plan,
        orchestrator_preflight=orchestrator_preflight,
        sdk_session_spec=sdk_session_spec,
        execution_context=execution_context,
        agent_execution_plan=outreach_plan.model_dump(mode="json"),
        cost_tracking_requested=cost_tracking_requested,
        database_url=database_url,
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
    execution_context: dict[str, Any] | None = None,
    agent_execution_plan: dict[str, object] | None = None,
    cost_tracking_requested: bool = False,
    database_url: str | None = None,
) -> int:
    env = None
    child_env = orchestrator_preflight_env(
        orchestrator_preflight,
        execution_context=execution_context,
    )
    if sdk_session_spec is not None:
        child_env = {**child_env, **sdk_session_env(sdk_session_spec)}
    if child_env:
        env = {**os.environ, **child_env}
    timeout_seconds = _child_agent_timeout_seconds(route=route)
    try:
        completed = run_isolated_child_process(
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        failure = known_exception_to_operator_failure(
            subprocess.TimeoutExpired(command, timeout_seconds),
            context=f"{_agent_display_name(route)} child process",
        )
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
                "summary": failure.summary,
                "send_enabled": False,
                "failure": failure.to_dict(),
                "error_type": "timeout",
                "next_step": failure.next_step,
            },
        }
        _persist_ask_script_failure(
            payload,
            route=route,
            input_text=input_text,
            database_url=database_url,
            error_kind=failure.kind,
        )
        _print_ask_live_payload(payload, json_output=json_output)
        return 1
    if completed.returncode != 0:
        failure = _operator_failure_from_child_output(
            completed.stdout,
            fallback=RuntimeError(
                _redacted_child_output(completed.stderr)
                or _redacted_child_output(completed.stdout)
                or f"child process exited {completed.returncode}"
            ),
            context=f"{_agent_display_name(route)} child process",
        )
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
                "summary": failure.summary,
                "send_enabled": False,
                "failure": failure.to_dict(),
                "error_type": "child_process_failed",
                "returncode": int(completed.returncode),
                "stderr_excerpt": _redacted_child_output(completed.stderr, max_chars=4000),
                "stdout_excerpt": _redacted_child_output(completed.stdout, max_chars=4000),
                "error_tail": _redacted_child_tail(completed.stderr or completed.stdout),
                "next_step": failure.next_step,
            },
        }
        _persist_ask_script_failure(
            payload,
            route=route,
            input_text=input_text,
            database_url=database_url,
            error_kind=failure.kind,
        )
        _print_ask_live_payload(payload, json_output=json_output)
        return int(completed.returncode) or 1
    try:
        script_payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        failure = known_exception_to_operator_failure(
            exc,
            context=f"{_agent_display_name(route)} child process",
        )
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
                "summary": failure.summary,
                "send_enabled": False,
                "failure": failure.to_dict(),
                "error_type": "child_process_malformed_json",
                "parse_error": str(exc),
                "stderr_excerpt": _redacted_child_output(completed.stderr, max_chars=4000),
                "stdout_excerpt": _redacted_child_output(completed.stdout, max_chars=4000),
                "error_tail": _redacted_child_tail(completed.stderr or completed.stdout),
                "next_step": failure.next_step,
            },
        }
        _persist_ask_script_failure(
            payload,
            route=route,
            input_text=input_text,
            database_url=database_url,
            error_kind=failure.kind,
        )
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
        "specialist_output_review": review.model_dump(mode="json"),
        "output": output if output is not None else script_payload,
        "script_payload": script_payload,
    }
    script_status = str(script_payload.get("status") or "").strip().lower()
    typed_display = ""
    if script_status in {"blocked", "clarification_required", "needs_input"}:
        payload["status"] = "blocked"
        payload["block_kind"] = str(
            script_payload.get("block_kind") or script_payload.get("reason_code") or "blocked"
        )
    if script_status in {"blocked", "clarification_required", "needs_input"}:
        # Provider no-match and ambiguity are already safe terminal outcomes.
        # Do not spend a repair-model request trying to force normal answer-shape
        # constraints onto a blocker.
        human_summary = _payload_human_summary(script_payload)
    else:
        interpreted_constraints = output_constraints_from_plan(manual_plan)
        verified_provider_summary = _verified_child_provider_summary(script_payload)
        typed_display = _strict_requested_display_text(output, manual_plan)
        candidate_summary = (
            verified_provider_summary or _payload_human_summary(script_payload)
            if interpreted_constraints.has_deterministic_requirements()
            else typed_display or _payload_human_summary(script_payload)
        )
        if verified_provider_summary:
            candidate_summary = verified_provider_summary
        instruction_resolution = resolve_instruction_following_response(
            candidate_summary,
            original_request=input_text,
            manual_plan=manual_plan,
            bounded_evidence=json.dumps(
                (
                    script_payload
                    if verified_provider_summary
                    else output
                    if output is not None
                    else script_payload
                ),
                ensure_ascii=True,
                sort_keys=True,
            ),
            live=True,
        )
        final_validation = instruction_resolution.validation
        provenance_violations = _official_source_response_violations(
            script_payload,
            manual_plan,
            instruction_resolution.response_text,
        )
        if provenance_violations:
            final_validation = final_validation.model_copy(
                update={
                    "passed": False,
                    "violations": list(
                        dict.fromkeys(
                            [
                                *final_validation.violations,
                                *provenance_violations,
                            ]
                        )
                    ),
                }
            )
        human_summary = (
            instruction_resolution.response_text
            if final_validation.passed
            else instruction_following_blocker_text(final_validation)
        )
        instruction_metadata = instruction_resolution.metadata()
        instruction_metadata["validation"] = final_validation.model_dump(
            mode="json",
            exclude={"checked_text", "source_url_count"}
            if final_validation.source_url_count is None
            else {"checked_text"},
        )
        payload["instruction_following"] = instruction_metadata
        if (
            final_validation.applicable
            and not final_validation.passed
        ):
            payload["status"] = "blocked"
            payload["block_kind"] = "instruction_following_constraint_failed"
    promotion_receipt = compile_child_result_promotion_receipt(
        script_payload,
        summary=human_summary,
        instruction_repair_verified=bool(
            payload.get("instruction_following", {}).get("repair_succeeded")
            if isinstance(payload.get("instruction_following"), dict)
            else False
        ),
        typed_display_verified=bool(
            script_status not in {"blocked", "clarification_required", "needs_input"}
            and typed_display
        ),
    )
    payload["child_result_promotion_receipt"] = promotion_receipt.receipt()
    if review.status != "fail" or not promotion_receipt.reader_ready:
        payload["orchestrator_review"] = review.model_dump(mode="json")
    if human_summary:
        payload["human_summary"] = human_summary
        payload["slack_display_text"] = human_summary
        payload["display_text"] = human_summary
        payload["summary"] = human_summary
    for key in ("usage", "cost", "request_cache", "model", "budget_guard"):
        value = script_payload.get(key) if isinstance(script_payload, dict) else None
        if value:
            payload[key] = value
    model_payload = script_payload.get("model") if isinstance(script_payload, dict) else {}
    if isinstance(model_payload, dict):
        payload["model_execution"] = {
            "provider": str(model_payload.get("provider") or ""),
            "model": str(model_payload.get("name") or model_payload.get("model") or ""),
            "run_mode": str(model_payload.get("run_mode") or "live_sdk"),
            "usage_available": isinstance(payload.get("usage"), dict),
            "cost_available": isinstance(payload.get("cost"), dict),
        }
    retrieval_diagnostics = _payload_retrieval_diagnostics(script_payload)
    if retrieval_diagnostics:
        payload["retrieval_diagnostics"] = retrieval_diagnostics
    _attach_slack_run_provenance(payload, execution_context)
    attach_execution_public_result(payload)
    try:
        model_name = ""
        model_payload = payload.get("model") if isinstance(payload.get("model"), dict) else {}
        if isinstance(model_payload, dict):
            model_name = str(model_payload.get("name") or model_payload.get("model") or "")
        run_id = SQLiteStore(database_url or database_url_from_env()).save_agent_run(
            agent_name=route,
            input_payload={"request_text": input_text, "route": route},
            input_summary=input_text[:500],
            output=payload,
            model=f"sdk-live:{model_name or route}",
            dry_run=False,
            status="blocked" if payload.get("status") == "blocked" else "success",
        )
        payload["agent_run_id"] = run_id
    except Exception as exc:  # pragma: no cover - diagnostic metadata only
        payload["agent_run_persistence_error"] = f"{type(exc).__name__}: {exc}"
    return _print_ask_live_payload(payload, json_output=json_output)


def _persist_ask_script_failure(
    payload: dict[str, Any],
    *,
    route: str,
    input_text: str,
    database_url: str | None,
    error_kind: str,
) -> None:
    """Persist redacted child diagnostics when the caller supplied local storage."""

    if not database_url:
        return
    try:
        run_id = SQLiteStore(database_url).save_agent_run(
            agent_name=route,
            input_payload={"request_text": input_text, "route": route},
            input_summary=input_text[:500],
            output=payload,
            model=f"child-error:{route}",
            dry_run=False,
            status="error",
            error=str(error_kind or "child_process_failed"),
        )
        payload["agent_run_id"] = run_id
    except Exception as exc:  # pragma: no cover - diagnostics must not mask failure
        payload["agent_run_persistence_error"] = f"{type(exc).__name__}: {exc}"


def _child_agent_timeout_seconds(*, route: str = "") -> float:
    explicit_child_timeout = os.getenv("KEYSTONE_CHILD_AGENT_TIMEOUT_SECONDS")
    raw = explicit_child_timeout or os.getenv("KEYSTONE_LIVE_MODEL_TIMEOUT_SECONDS")
    if not raw:
        value = 180.0
    else:
        try:
            value = float(raw)
        except ValueError:
            value = 180.0
    value = min(max(value, 1.0), 1800.0)
    if explicit_child_timeout or route != "opportunity_scout":
        return value

    retrieval_raw = os.getenv("KEYSTONE_OPPORTUNITY_RETRIEVAL_DEADLINE_SECONDS", "90")
    try:
        retrieval_deadline = float(retrieval_raw)
    except ValueError:
        retrieval_deadline = 90.0
    retrieval_deadline = min(max(retrieval_deadline, 1.0), 600.0)
    return min(max(value, retrieval_deadline + 60.0), 1800.0)


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


def _payload_human_summary(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("human_summary")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    output = payload.get("output")
    if isinstance(output, dict):
        nested = output.get("human_summary") or output.get("summary")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _verified_child_provider_summary(payload: object) -> str:
    """Return receipt-backed child copy before unverified nested model prose."""

    if not isinstance(payload, dict):
        return ""
    if payload.get("user_facing_result_verified") is not True:
        return ""
    receipts = payload.get("tool_receipts")
    if not isinstance(receipts, list) or not any(isinstance(receipt, dict) for receipt in receipts):
        return ""
    public_result = payload.get("public_result")
    if not isinstance(public_result, dict):
        return ""
    if (
        public_result.get("completion_confirmed") is not True
        or str(public_result.get("status") or "") != "completed"
    ):
        return ""
    if (
        public_result.get("provider_write_attempted") is True
        and public_result.get("provider_receipt_verified") is not True
    ):
        return ""
    return _payload_human_summary(payload)


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
        marker = " ...<truncated>... "
        if max_chars > len(marker) + 40:
            head_len = (max_chars - len(marker)) // 2
            tail_len = max_chars - len(marker) - head_len
            return f"{text[:head_len].rstrip()}{marker}{text[-tail_len:].lstrip()}"
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _redacted_child_tail(value: str | None, *, max_chars: int = 1600) -> str:
    redacted = redact_secrets(str(value or ""))
    text = str(redacted if redacted is not None else "")
    text = " ".join(text.replace("\x00", "").split())
    if max_chars <= 0:
        return ""
    if len(text) > max_chars:
        return text[-max_chars:].lstrip()
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
    _register_entry_agent_run(payload.get("agent_run_id"))
    attach_execution_public_result(payload)
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(f"Agent: {payload.get('agent_name', '')}")
        if payload.get("output_type"):
            print(f"Output type: {payload['output_type']}")
        print(f"Send enabled: {payload.get('send_enabled', False)}")
        if payload.get("human_summary"):
            print(payload["human_summary"])
        elif payload.get("message"):
            print(payload["message"])
        else:
            print(json.dumps(payload.get("output"), ensure_ascii=True, indent=2, sort_keys=True))
        review = payload.get("orchestrator_review")
        if isinstance(review, dict):
            print(
                f"Orchestrator review: {review.get('status')} ({review.get('overall_score')}/100)"
            )
            next_step = str(review.get("recommended_next_step") or "").strip()
            if next_step:
                print(f"Orchestrator feedback: {next_step}")
        else:
            specialist_review = payload.get("specialist_output_review")
            if isinstance(specialist_review, dict):
                print(
                    "Specialist output review: "
                    f"{specialist_review.get('status')} "
                    f"({specialist_review.get('overall_score')}/100)"
                )
                next_step = str(
                    specialist_review.get("recommended_next_step") or ""
                ).strip()
                if next_step:
                    print(f"Specialist feedback: {next_step}")
        missing = payload.get("missing_information")
        if isinstance(missing, list) and missing:
            print("Missing information: " + "; ".join(str(item) for item in missing[:8]))
    return 0


def _print_local_attachment_unavailable(
    route: str,
    input_text: str,
    diagnostics: tuple[str, ...],
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan,
    orchestrator_preflight: OrchestratorPreflight | None,
) -> int:
    """Stop before model execution when Slack did not yield readable bytes."""

    message = (
        "I received the attachment request, but the file bytes were not available "
        "to read. Please reattach the file in this Slack thread and try again."
    )
    payload = {
        "mode": "live_sdk",
        "status": "blocked",
        "block_kind": "local_attachment_bytes_unavailable",
        "selected_agent": route,
        "input": input_text,
        "send_enabled": False,
        "completion_confirmed": False,
        "message": message,
        "human_summary": message,
        "slack_display_text": message,
        "display_text": message,
        "summary": message,
        "attachment_diagnostics": list(diagnostics),
        "manual_request_plan": manual_plan.model_dump(mode="json"),
        "orchestrator_preflight": _orchestrator_preflight_payload(orchestrator_preflight),
        "tool_admission": {"tool_count": 0, "tool_names": []},
        "openai_requests": 0,
        "side_effects": {
            "email_sent": False,
            "slack_message_posted": False,
            "external_write_performed": False,
        },
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(message)
    return 2


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
    return _run_with_entry_telemetry(
        args,
        lambda: _run_work_items_advance_with_current_environment(args),
    )


def _run_work_items_advance_with_current_environment(
    args: argparse.Namespace,
) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    _register_entry_store(store)
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
            workflow_state=_orchestrator_workflow_state_from_cli_context(
                context_file_path=args.context_file,
                request_text=input_text,
                database_url=args.database_url,
                work_item=existing_work_item,
            ),
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
        live_rss_slack_read=args.live_rss_slack_read,
        max_results=args.max_results,
        context_file_path=args.context_file,
        manual_request_plan=manual_plan.model_dump(mode="json") if manual_plan else None,
        orchestrator_preflight=_orchestrator_preflight_payload(orchestrator_preflight),
        sdk_session_enabled=args.sdk_session,
        sdk_session_id=args.sdk_session_id,
        sdk_session_db_path=args.sdk_session_db,
        sdk_session_history_limit=args.sdk_session_history_limit,
        **_workflow_cost_options_for_request_context(
            request_text=input_text,
            context_file_path=args.context_file,
            work_item=existing_work_item,
        ),
    )
    graph_metadata = None
    from keystone_agents.langgraph_workflow import (
        should_use_langgraph_for_work_item,
        work_item_langgraph_env_override,
    )

    override = work_item_langgraph_env_override()
    use_langgraph = (
        True
        if args.langgraph
        else override
        if override is not None
        else should_use_langgraph_for_work_item(request, manager_loop=True)
    )
    if use_langgraph:
        outcome = _run_work_item_langgraph_for_request(
            request,
            max_manager_steps=args.max_manager_steps,
        )
        result = outcome.result
        graph_metadata = _work_item_langgraph_metadata(outcome)
    else:
        result = advance_work_item_manager_loop(
            request,
            max_steps=args.max_manager_steps,
            feedback_callback=None if args.json else _print_manager_loop_feedback,
        )
    return _print_work_item_result(
        result,
        json_output=args.json,
        graph_metadata=graph_metadata,
        execution_metadata={
            "live_sdk": bool(args.live_sdk),
            "live_search": bool(args.live_search),
            "langgraph": graph_metadata is not None,
            "openai_requests": _stored_work_item_openai_requests(
                store,
                result.work_item.id,
            ),
        },
    )


def _work_item_preflight_requested_agent(work_item: WorkItem | None) -> str | None:
    if work_item is None:
        return None
    route = work_item.current_route
    if route in {WorkItemRoute.ORCHESTRATOR, WorkItemRoute.CLARIFICATION}:
        return None
    return route.value


def _run_work_item_langgraph_for_request(
    request: WorkflowRunRequest,
    *,
    max_manager_steps: int = 3,
):
    from keystone_agents.langgraph_workflow import (
        run_work_item_langgraph,
        work_item_graph_thread_id,
    )

    thread_id = work_item_graph_thread_id(request.work_item_id or "")
    return run_work_item_langgraph(
        request,
        thread_id=thread_id or None,
        manager_loop=True,
        max_manager_steps=max_manager_steps,
    )


def _work_item_langgraph_metadata(outcome) -> dict:
    return {
        "runtime": outcome.graph_runtime,
        "graph_available": outcome.graph_available,
        "checkpoint_required": outcome.checkpoint_required,
        "checkpoint_reason": outcome.checkpoint_reason,
        "checkpoint_key": outcome.checkpoint_key,
        "node_path": [node for node in outcome.node_path if node != "manager_loop_finalize"],
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
    eval_record: dict[str, object] | None = None,
    execution_metadata: dict[str, object] | None = None,
) -> int:
    result = _shape_work_item_result_for_requested_output(result)
    result, user_facing_result_verified = _ensure_work_item_user_facing_summary(result)
    _register_entry_work_item(getattr(result.work_item, "id", ""))
    if json_output:
        payload = result.model_dump(mode="json")
        result_status = str(getattr(result.status, "value", result.status))
        completion_confirmed = bool(
            user_facing_result_verified
            and result_status == "done"
            and not result.blockers
        )
        payload["user_facing_result_verified"] = user_facing_result_verified
        payload["completion_confirmed"] = completion_confirmed
        payload["slack_display_title"] = (
            "Business Agents Result Ready"
            if completion_confirmed
            else (
                "Business Agents Awaiting Approval"
                if result_status == "needs_approval"
                else "Business Agents Completion Not Confirmed"
            )
        )
        payload["slack_display_text"] = result.human_summary
        payload["display_text"] = result.human_summary
        payload["summary"] = result.human_summary
        if graph_metadata is not None:
            payload["_langgraph"] = graph_metadata
        if execution_metadata is not None:
            payload["_execution"] = execution_metadata
        if eval_record is not None:
            payload["_eval_record"] = eval_record
            payload["human_summary"] = _append_eval_thread_guidance_to_summary(
                str(payload.get("human_summary") or ""),
                eval_record=eval_record,
            )
        attach_execution_public_result(payload)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(render_work_item_result_text(result))
        if eval_record is not None:
            print(f"Eval record: {eval_record.get('case_id')} / {eval_record.get('run_id')}")
            if eval_record.get("dashboard_case_url"):
                print(f"Eval dashboard: {eval_record.get('dashboard_case_url')}")
            if eval_record.get("review_case_url"):
                print(f"Human review form: {eval_record.get('review_case_url')}")
        if graph_metadata is not None:
            print(f"Graph runtime: {graph_metadata['runtime']}")
            print(f"Graph nodes: {' -> '.join(graph_metadata['node_path'])}")
            if graph_metadata["checkpoint_required"]:
                print(f"Graph checkpoint: {graph_metadata['checkpoint_reason']}")
    return 0


def _ensure_work_item_user_facing_summary(result: Any) -> tuple[Any, bool]:
    """Reject workflow metadata as a substitute for a user-facing result."""

    summary = " ".join(str(getattr(result, "human_summary", "") or "").split())
    metadata_only = not summary or bool(
        re.fullmatch(
            r"(?:business agents\s+)?(?:work\s*item\s+)?"
            r"(?:command\s+)?(?:completed|complete|ready|done)[.!]?",
            summary,
            flags=re.I,
        )
    )
    if not metadata_only:
        return result, True
    blocker_messages = [
        " ".join(str(getattr(blocker, "message", "") or "").split())
        for blocker in list(getattr(result, "blockers", []) or [])
        if str(getattr(blocker, "message", "") or "").strip()
    ]
    if blocker_messages:
        replacement = (
            "I could not complete the requested work. "
            f"{blocker_messages[0]} Completion is not confirmed."
        )
    else:
        replacement = (
            "I could not verify a user-facing result for this run. Completion is "
            "not confirmed; review the agent trace before relying on it."
        )
    return result.model_copy(update={"human_summary": replacement}), False


def _shape_work_item_result_for_requested_output(result: Any) -> Any:
    """Apply an exact narrow WorkItem display contract after synthesis."""

    plan = getattr(result, "manual_request_plan", None)
    if not isinstance(plan, dict):
        return result
    ask_shape = plan.get("ask_shape")
    if not isinstance(ask_shape, dict):
        return result
    if str(ask_shape.get("strict_filter_mode") or "") not in {"exact", "strict"}:
        return result
    stop_condition = str(ask_shape.get("stop_condition") or "")
    if stop_condition != "stop_after_exact_requested_sentence_count":
        return result
    request_text = str(getattr(getattr(result, "work_item", None), "request_text", "") or "")
    count_match = re.search(r"\bexactly\s+(\d+)\s+sentences?\b", request_text, re.I)
    if count_match is None or int(count_match.group(1)) != 2:
        return result
    work_item = getattr(result, "work_item", None)
    sources = list(getattr(work_item, "sources", []) or []) if work_item is not None else []
    claims: list[str] = []
    for source in sources:
        supported_claim = str(getattr(source, "supported_claim", "") or "").strip()
        key_facts = list(getattr(source, "key_facts", []) or [])
        for claim in [supported_claim, *key_facts]:
            cleaned = _ensure_terminal_punctuation(str(claim or "").strip())
            if cleaned != "." and cleaned not in claims:
                claims.append(cleaned)
    if not claims:
        return result
    first_sentence = claims[0]
    if len(claims) > 1:
        second_sentence = claims[1]
    else:
        second_sentence = (
            "No additional company details are supported by the bounded evidence supplied "
            "for this run."
        )
    shaped = result.model_copy(
        update={
            "human_summary": f"{first_sentence} {second_sentence}",
            "next_action": None,
        }
    )
    if work_item is not None:
        shaped = shaped.model_copy(
            update={"work_item": work_item.model_copy(update={"next_action": None})}
        )
    return shaped


def _append_eval_thread_guidance_to_summary(
    summary: str,
    *,
    eval_record: dict[str, object],
) -> str:
    if "Review form:" in summary and "case dashboard" in summary:
        return summary
    case_id = str(eval_record.get("case_id") or "").strip()
    if not case_id:
        return summary
    run_id = str(eval_record.get("run_id") or "").strip()
    dashboard_case_url = str(eval_record.get("dashboard_case_url") or "").strip()
    review_case_url = str(eval_record.get("review_case_url") or "").strip()
    parts = [summary.strip()] if summary.strip() else []
    detail = f"Eval: case `{case_id}`"
    if run_id:
        detail += f", run `{run_id}`"
    detail += "."
    if dashboard_case_url:
        detail += f" Dashboard: {_slack_link(dashboard_case_url, 'case dashboard')}."
    if review_case_url:
        detail += f" Review form: {_slack_link(review_case_url, 'score this case')}."
    detail += " Score from the linked form, or use `Score with Orchestrator Judge` when enabled, then press `Submit Evaluation` in Slack to save scores and refresh the dashboard."
    parts.append(detail)
    return "\n\n".join(parts).strip()


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
    load_settings(force_dotenv=True)
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


def _run_agents_tools(args: argparse.Namespace) -> int:
    load_settings(force_dotenv=True)
    cards = [
        card for card in agent_cards() if args.agent == "all" or card["route_name"] == args.agent
    ]
    payload = [
        {
            "route_name": card["route_name"],
            "agent_name": card["agent_name"],
            "runtime_tool_availability": card["runtime_tool_availability"],
        }
        for card in cards
    ]
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(_render_agent_tool_availability_table(payload))
    return 0


def _run_agents_file_search_config(args: argparse.Namespace) -> int:
    load_settings(force_dotenv=True)
    summary = local_file_search_config_summary()
    if args.json:
        print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(_render_file_search_config_summary(summary))
    return 1 if summary["status"] == "invalid_config" else 0


def _render_file_search_config_summary(summary: dict[str, Any]) -> str:
    status = str(summary.get("status") or "unknown")
    path = str(summary.get("path") or "")
    if status == "missing":
        return f"Local FileSearch config: missing ({path})"
    if status == "invalid_config":
        return f"Local FileSearch config: invalid ({path})\n{summary.get('error')}"
    rows: list[list[str]] = []
    global_summary = summary.get("global")
    if isinstance(global_summary, dict):
        rows.append(
            [
                "global",
                "-",
                str(global_summary.get("vector_store_id_count", 0)),
                str(global_summary.get("max_num_results") or "-"),
                str(bool(global_summary.get("include_search_results"))),
            ]
        )
    for agent in summary.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        rows.append(
            [
                "agent",
                str(agent.get("agent_name") or ""),
                str(agent.get("vector_store_id_count", 0)),
                str(agent.get("max_num_results") or "-"),
                str(bool(agent.get("include_search_results"))),
            ]
        )
    header = f"Local FileSearch config: {status} ({path})"
    if not rows:
        return header
    table = render_markdown_table(
        ["Scope", "Agent", "Stores", "Max results", "Include results"],
        rows,
    )
    unknown_agents = summary.get("unknown_agents") or []
    if unknown_agents:
        return f"{header}\n\n{table}\n\nUnknown agents: " + ", ".join(
            str(agent) for agent in unknown_agents
        )
    return f"{header}\n\n{table}"


def _render_agent_tool_availability_table(cards: list[dict[str, Any]]) -> str:
    rows: list[list[str]] = []
    for card in cards:
        route_name = str(card.get("route_name") or "")
        availability = card.get("runtime_tool_availability") or {}
        if not isinstance(availability, dict):
            continue
        for tool_name in sorted(availability):
            status = availability.get(tool_name)
            if not isinstance(status, dict):
                continue
            rows.append(
                [
                    route_name,
                    tool_name,
                    str(status.get("status") or "-"),
                    "yes" if bool(status.get("available")) else "no",
                    _tool_availability_details(tool_name, status),
                ]
            )
    if not rows:
        return "No runtime tool availability records found."
    return render_markdown_table(
        ["Agent", "Tool", "Status", "Available", "Details"],
        rows,
    )


def _tool_availability_details(tool_name: str, status: dict[str, Any]) -> str:
    if tool_name == "file_search":
        details = [
            f"stores={status.get('vector_store_id_count', 0)}",
            f"scope={status.get('vector_store_source') or 'none'}",
        ]
        env_name = status.get("vector_store_env_name")
        if env_name:
            details.append(f"env={env_name}")
        config_path = status.get("vector_store_config_path")
        if config_path:
            details.append(f"config={config_path}")
        return "; ".join(details)
    if tool_name == "search_web":
        providers = status.get("provider_sequence") or []
        provider_text = ",".join(str(provider) for provider in providers) or "none"
        return (
            f"live_enabled={bool(status.get('live_enabled'))}; "
            f"live_available={bool(status.get('live_available'))}; "
            f"providers={provider_text}"
        )
    if tool_name == "local_kni_documents":
        return (
            f"indexed={status.get('indexed_count', 0)}; "
            f"local_only={bool(status.get('local_only'))}; "
            f"model_context_allowed={bool(status.get('model_context_allowed'))}; "
            f"send_enabled={bool(status.get('send_enabled'))}"
        )
    if tool_name == "source_layer_policy":
        layers = status.get("layers") or []
        if not isinstance(layers, list):
            return "-"
        names = []
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            layer_name = str(layer.get("layer") or "").strip()
            if not layer_name:
                continue
            status_text = str(layer.get("runtime_status") or "unknown").strip()
            names.append(f"{layer_name}:{status_text}")
        return "layers=" + ",".join(names) if names else "-"
    reason = str(status.get("reason") or status.get("error") or "").strip()
    return reason or "-"


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
