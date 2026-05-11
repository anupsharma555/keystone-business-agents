"""Package-native Keystone command line entrypoint."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.agent_registry import agent_cards
from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_research_brief_agent,
)
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.agents.manual_request_planner import resolve_manual_request_plan
from keystone_agents.agents.opportunity_scout import (
    build_opportunity_scout_agent,
)
from keystone_agents.agents.orchestrator import (
    build_orchestrator_agent,
    route_request,
    run_orchestrator_sdk,
)
from keystone_agents.agents.outreach_composer import (
    build_outreach_composer_agent,
)
from keystone_agents.config import cli_default_live_research, cli_default_live_sdk, load_settings
from keystone_agents.evals import generate_eval_report, run_static_evals
from keystone_agents.health import format_health_report, report_to_json, run_health_check
from keystone_agents.models import RunMode
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
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
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
from keystone_agents.workflow_runner import advance_work_item
from keystone_agents.workflows import pipeline_markdown_report, run_keystone_pipeline


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
        choices=[
            "orchestrator",
            "business_research_analyst",
            "opportunity_scout",
            "outreach_composer",
            "gmail_triage",
        ],
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
    ask.add_argument(
        "--live-manual-plan",
        action="store_true",
        help="Use the SDK manual-request planner before direct agent execution.",
    )
    ask.add_argument("--database-url", default=None, help="SQLite URL for WorkItem mode.")
    ask.add_argument("--live-search", action="store_true", help="Use live search in WorkItem mode.")
    ask.add_argument("--max-results", type=int, default=3, help="Max WorkItem search results.")
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
    work_items_advance.add_argument("--database-url", default=None)
    work_items_advance.add_argument("--live-search", action="store_true")
    work_items_advance.add_argument("--live-sdk", action="store_true")
    work_items_advance.add_argument("--max-results", type=int, default=3)
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


AGENT_DISPLAY_NAMES = {
    "orchestrator": "Keystone Orchestrator Agent",
    "business_research_analyst": "Business Research Analyst",
    "opportunity_scout": "Opportunity Scout Agent",
    "outreach_composer": "Outreach Composer Agent",
    "gmail_triage": "Gmail Triage Agent",
}


def _ask_input(args: argparse.Namespace) -> str:
    if args.input:
        return _read_input(args.input)
    return " ".join(args.prompt).strip()


def _run_ask(args: argparse.Namespace) -> int:
    raw_input = _ask_input(args)
    mention = parse_agent_mention(raw_input)
    input_text = raw_input if args.agent else mention.input_text
    live_sdk = _ask_live_sdk_enabled(args)
    live_manual_plan = args.live_manual_plan or live_sdk
    live_search = args.live_search or (live_sdk and cli_default_live_research())
    if live_manual_plan:
        load_settings(force_dotenv=True)
    requested_route = args.agent or (mention.route if mention.explicit else None)
    if args.agent is None:
        manual_plan = resolve_manual_request_plan(
            input_text,
            requested_agent=requested_route,
            live=live_manual_plan,
        )
        if live_sdk and mention.explicit and mention.route is not None:
            route = str(mention.route)
            if route == "orchestrator":
                return _run_ask_orchestrator(
                    input_text,
                    live_sdk=True,
                    json_output=args.json,
                    manual_plan=manual_plan,
                )
            return _run_ask_specialist_live(
                route,
                input_text,
                json_output=args.json,
                manual_plan=manual_plan,
            )
        return _run_ask_work_item(
            input_text,
            database_url=args.database_url,
            live_search=live_search,
            live_sdk=live_sdk,
            max_results=args.max_results,
            json_output=args.json,
            manual_plan=manual_plan,
        )
    route = args.agent
    manual_plan = resolve_manual_request_plan(
        input_text,
        requested_agent=route,
        live=live_manual_plan,
    )
    if route == "orchestrator":
        return _run_ask_orchestrator(
            input_text,
            live_sdk=live_sdk,
            json_output=args.json,
            manual_plan=manual_plan,
        )
    if live_sdk:
        return _run_ask_specialist_live(
            route,
            input_text,
            json_output=args.json,
            manual_plan=manual_plan,
        )
    return _print_ask_dry_run(route, input_text, json_output=args.json, manual_plan=manual_plan)


def _ask_live_sdk_enabled(args: argparse.Namespace) -> bool:
    explicit = getattr(args, "live_sdk", None)
    if explicit is not None:
        return bool(explicit)
    return cli_default_live_sdk()


def _run_ask_work_item(
    input_text: str,
    *,
    database_url: str | None,
    live_search: bool,
    live_sdk: bool,
    max_results: int,
    json_output: bool,
    manual_plan: ManualRequestPlan | None = None,
) -> int:
    store = SQLiteStore(database_url or database_url_from_env())
    work_item_id = _resolve_continue_work_item_id(
        store,
        input_text=input_text,
        explicit_work_item_id=None,
        json_output=json_output,
    )
    result = advance_work_item(
        WorkflowRunRequest(
            request_text=input_text,
            work_item_id=work_item_id,
            save=True,
            database_url=database_url,
            live_search=live_search,
            live_sdk=live_sdk,
            max_results=max_results,
            manual_request_plan=manual_plan.model_dump(mode="json") if manual_plan else None,
        )
    )
    return _print_work_item_result(result, json_output=json_output)


def _run_ask_orchestrator(
    input_text: str,
    *,
    live_sdk: bool,
    json_output: bool,
    manual_plan: ManualRequestPlan | None = None,
) -> int:
    if live_sdk:
        load_settings(force_dotenv=True)
        result = run_orchestrator_sdk(input_text, live=True).output
    else:
        result = route_request(input_text, manual_plan=manual_plan)
    payload = {
        "mode": "live_sdk" if live_sdk else "dry_run",
        "selected_agent": "orchestrator",
        "agent_name": AGENT_DISPLAY_NAMES["orchestrator"],
        "input": input_text,
        "route": result.route,
        "target_agent": result.target_agent,
        "send_enabled": result.send_enabled,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "output": result.model_dump(mode="json"),
    }
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
) -> int:
    builder = {
        "business_research_analyst": build_business_research_analyst_research_brief_agent,
        "opportunity_scout": build_opportunity_scout_agent,
        "outreach_composer": build_outreach_composer_agent,
        "gmail_triage": build_gmail_triage_agent,
    }[route]
    agent = builder()
    payload = {
        "mode": "dry_run",
        "selected_agent": route,
        "agent_name": AGENT_DISPLAY_NAMES[route],
        "sdk_agent_name": agent.name,
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
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
) -> int:
    load_settings(force_dotenv=True)
    if route == "business_research_analyst":
        return _run_ask_company_research_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
        )
    elif route == "opportunity_scout":
        return _run_ask_opportunity_scout_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
        )
    elif route == "outreach_composer":
        return _print_ask_outreach_context_blocked(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
        )
    elif route == "gmail_triage":
        return _run_ask_gmail_triage_live(
            input_text,
            json_output=json_output,
            manual_plan=manual_plan,
        )
    else:
        raise SystemExit(f"Unsupported agent route: {route}")


def _run_ask_company_research_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
) -> int:
    target = (manual_plan.primary_target if manual_plan else "") or input_text[:120]
    if not target.strip():
        return _print_ask_clarification(
            "business_research_analyst",
            input_text,
            "Business Research Analyst needs a company, person, institute, URL, or topic target.",
            json_output=json_output,
            manual_plan=manual_plan,
        )
    command = [
        sys.executable,
        "scripts/run_company_research.py",
        "--company",
        target.strip(),
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
    return _run_ask_script_live(
        "business_research_analyst",
        input_text,
        command,
        json_output=json_output,
        manual_plan=manual_plan,
    )


def _run_ask_opportunity_scout_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
) -> int:
    max_results = manual_plan.desired_count if manual_plan else 3
    command = [
        sys.executable,
        "scripts/run_opportunity_scout.py",
        "--topic",
        input_text,
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
    )


def _run_ask_gmail_triage_live(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
) -> int:
    temp_path: Path | None = None
    fixture_path = Path(input_text).expanduser() if input_text and "\n" not in input_text else None
    if fixture_path is not None and fixture_path.is_file():
        selected_fixture = str(fixture_path)
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
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _print_ask_outreach_context_blocked(
    input_text: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
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
        extra={
            "status": "blocked",
            "requires_approved_context": True,
            "send_enabled": False,
            "recommended_next_action": (
                "Attach or approve source-backed context in a WorkItem, or use "
                "scripts/run_outreach_draft.py with explicit approved fixtures."
            ),
        },
    )


def _run_ask_script_live(
    route: str,
    input_text: str,
    command: list[str],
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
) -> int:
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit((completed.stderr or completed.stdout or "Agent script failed.").strip())
    try:
        script_payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Agent script returned non-JSON output: {completed.stdout[:500]}"
        ) from exc
    output = script_payload.get("output") if isinstance(script_payload, dict) else None
    payload = {
        "mode": "live_sdk",
        "selected_agent": route,
        "agent_name": AGENT_DISPLAY_NAMES[route],
        "input": input_text,
        "send_enabled": _payload_send_enabled(script_payload),
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
        "output_type": (
            str(script_payload.get("output_type") or type(output).__name__)
            if isinstance(script_payload, dict)
            else type(script_payload).__name__
        ),
        "missing_information": _payload_missing_information(script_payload),
        "output": output if output is not None else script_payload,
        "script_payload": script_payload,
    }
    return _print_ask_live_payload(payload, json_output=json_output)


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


def _print_ask_clarification(
    route: str,
    input_text: str,
    message: str,
    *,
    json_output: bool,
    manual_plan: ManualRequestPlan | None,
    extra: dict[str, object] | None = None,
) -> int:
    payload = {
        "mode": "blocked",
        "selected_agent": route,
        "agent_name": AGENT_DISPLAY_NAMES[route],
        "input": input_text,
        "send_enabled": False,
        "manual_request_plan": manual_plan.model_dump(mode="json") if manual_plan else None,
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


def _run_work_items_advance(args: argparse.Namespace) -> int:
    store = SQLiteStore(args.database_url or database_url_from_env())
    input_text = _read_input(args.input)
    work_item_id = _resolve_continue_work_item_id(
        store,
        input_text=input_text,
        explicit_work_item_id=args.work_item_id,
        json_output=args.json,
    )
    result = advance_work_item(
        WorkflowRunRequest(
            request_text=input_text,
            work_item_id=work_item_id,
            save=True,
            database_url=args.database_url,
            live_search=args.live_search,
            live_sdk=args.live_sdk,
            max_results=args.max_results,
        )
    )
    return _print_work_item_result(result, json_output=args.json)


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


def _print_work_item_result(result, *, json_output: bool) -> int:
    if json_output:
        print(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=True, indent=2, sort_keys=True)
        )
    else:
        print(render_work_item_result_text(result))
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
                ["Route", "Agent", "Builder", "Schema", "Evals"],
                [
                    [
                        card["route_name"],
                        card["agent_name"],
                        str(card["builder"]).rsplit(":", maxsplit=1)[-1],
                        str(card["output_schema"]).rsplit(".", maxsplit=1)[-1],
                        ", ".join(card["eval_datasets"]),
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
