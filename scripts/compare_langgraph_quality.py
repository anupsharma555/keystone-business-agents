"""Compare graph-off and backend-selected LangGraph WorkItem quality offline."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from keystone_agents.langgraph_quality import (
    LIVE_OUTPUT_GRAPH_EVENT_SCHEMA,
    LIVE_OUTPUT_GRAPH_EVIDENCE_MODES,
    LIVE_OUTPUT_GRAPH_OFF_EVIDENCE_MODES,
    compare_langgraph_quality,
    finalize_langgraph_live_output_review,
    langgraph_edge_program_inventory,
    langgraph_live_output_review_packet,
    langgraph_live_output_review_rubric,
    langgraph_live_smoke_plan,
    langgraph_live_smoke_plan_from_packet,
    langgraph_open_smoke_checkpoint,
    langgraph_quality_markers,
    render_langgraph_edge_program_inventory,
    render_langgraph_live_output_review_decision,
    render_langgraph_live_output_review_packet,
    render_langgraph_live_output_review_rubric,
    render_langgraph_live_smoke_plan,
    render_langgraph_open_smoke_checkpoint,
    render_langgraph_quality_comparison,
    validate_langgraph_edge_program_inventory,
)
from keystone_agents.langgraph_workflow import (
    LANGGRAPH_WORKITEM_ENV_KEYS,
    advance_work_item_manager_loop_with_optional_langgraph,
)
from keystone_agents.schemas.announcement_feed import AnnouncementFeedEvidence, AnnouncementFeedItem
from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.slack_action_contract import business_agent_result_display_text
from keystone_agents.storage.sqlite_store import SQLiteStore

RESEARCH_OPPORTUNITY_REQUEST = (
    "@KNI business research analyst Research NeuroFlow as a behavioral-health AI "
    "opportunity with payer partnership and outcomes-evidence signals, then have "
    "Opportunity Scout assess whether this is a real KNI advisory/research "
    "opportunity and what source-backed evidence is missing. Stop before outreach."
)
RSS_OPPORTUNITY_REQUEST = (
    "use RSS context agent announcement history as source-provided signal, then "
    "have Opportunity Scout assess behavioral-health AI opportunities for "
    "NeuroFlow. Stop before outreach."
)
PREPRINTS_ZOTERO_RESEARCH_REQUEST = (
    "use preprints context agent history and Zotero context agent handoff to "
    "build an internal evidence packet for NeuroFlow, then research NeuroFlow."
)
CHIEF_CONTEXT_OPPORTUNITY_REQUEST = (
    "chief of staff coordinate the best next owner for NeuroFlow opportunity "
    "assessment; use Chief of Staff -> Opportunity Scout Agent if the next "
    "durable specialist should assess source-backed opportunity fit. Use RSS "
    "context agent announcement history and Zotero context agent handoff first. "
    "Do not draft, send, post, schedule, publish, or write externally."
)
OUTREACH_CHECKPOINT_REQUEST = (
    "research NeuroFlow, find matching opportunities, and prepare draft-only "
    "outreach."
)
GMAIL_RESEARCH_OUTREACH_REQUEST = (
    "gmail triage this sanitized inbound email from Mindful Care, research "
    "Mindful Care, and prepare draft-only outreach. Email: From: Jordan Lee, "
    "Operations at Mindful Care. Subject: Follow-up on measurement support. "
    "Body: Hi Jordan, our team is reviewing measurement-based care workflows and "
    "may need advisory help on evaluation design. Could you let me know if this "
    "is relevant for Keystone?"
)
GMAIL_THREAD_DRAFT_REQUEST = (
    "gmail triage this sanitized inbound email from Mindful Care, research "
    "Mindful Care, and prepare a draft-only Slack-thread sample outreach for "
    "review. Email: From: Jordan Lee, Operations at Mindful Care. Subject: "
    "Follow-up on measurement support. Body: Hi Jordan, our team is reviewing "
    "measurement-based care workflows and may need advisory help on evaluation "
    "design. Could you let me know if this is relevant for Keystone?"
)
SCENARIOS: dict[str, dict[str, Any]] = {
    "research-opportunity": {
        "request_text": RESEARCH_OPPORTUNITY_REQUEST,
        "manual_request_plan": {
            "source": "langgraph_quality_comparison",
            "requested_agent": "business_research_analyst",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "NeuroFlow",
        },
        "allow_route_change": False,
        "expected_graph_route": "",
        "seed_rss": False,
        "seed_preprints": False,
    },
    "rss-opportunity": {
        "request_text": RSS_OPPORTUNITY_REQUEST,
        "manual_request_plan": {
            "source": "langgraph_quality_comparison",
            "requested_agent": "rss_context_agent",
            "target_agent": "rss_context_agent",
            "intent": "internal_review_handoff",
            "primary_target": "NeuroFlow",
        },
        "allow_route_change": True,
        "expected_graph_route": "opportunity_scout",
        "seed_rss": True,
        "seed_preprints": False,
    },
    "preprints-zotero-research": {
        "request_text": PREPRINTS_ZOTERO_RESEARCH_REQUEST,
        "manual_request_plan": {
            "source": "langgraph_quality_comparison",
            "requested_agent": "preprints_context_agent",
            "target_agent": "preprints_context_agent",
            "intent": "context_lookup",
            "primary_target": "NeuroFlow",
        },
        "allow_route_change": True,
        "expected_graph_route": "business_research_analyst",
        "seed_rss": False,
        "seed_preprints": True,
    },
    "chief-context-opportunity": {
        "request_text": CHIEF_CONTEXT_OPPORTUNITY_REQUEST,
        "manual_request_plan": {
            "source": "langgraph_quality_comparison",
            "requested_agent": "chief_of_staff",
            "target_agent": "chief_of_staff",
            "intent": "internal_review_handoff",
            "primary_target": "NeuroFlow",
        },
        "allow_route_change": True,
        "expected_graph_route": "opportunity_scout",
        "seed_rss": True,
        "seed_preprints": False,
    },
    "outreach-checkpoint": {
        "request_text": OUTREACH_CHECKPOINT_REQUEST,
        "manual_request_plan": {
            "source": "langgraph_quality_comparison",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "NeuroFlow",
        },
        "allow_route_change": False,
        "expected_graph_route": "",
        "seed_rss": False,
        "seed_preprints": False,
    },
    "gmail-research-outreach": {
        "request_text": GMAIL_RESEARCH_OUTREACH_REQUEST,
        "manual_request_plan": {
            "source": "langgraph_quality_comparison",
            "requested_agent": "orchestrator",
            "target_agent": "gmail_triage",
            "intent": "gmail_triage",
            "primary_target": "Mindful Care",
            "target_type": "gmail_thread",
            "task_objective": "gmail_triage",
        },
        "allow_route_change": False,
        "expected_graph_route": "",
        "seed_rss": False,
        "seed_preprints": False,
    },
    "gmail-research-thread-draft": {
        "request_text": GMAIL_THREAD_DRAFT_REQUEST,
        "manual_request_plan": {
            "source": "langgraph_quality_comparison",
            "requested_agent": "orchestrator",
            "target_agent": "gmail_triage",
            "intent": "gmail_triage",
            "primary_target": "Mindful Care",
            "target_type": "gmail_thread",
            "task_objective": "gmail_triage",
        },
        "allow_route_change": False,
        "expected_graph_route": "",
        "seed_rss": False,
        "seed_preprints": False,
    },
}
REVIEW_WINNERS = {"graph", "control", "tie", "unreviewed"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a dry-run graph-off vs backend-selected LangGraph WorkItem "
            "comparison and print deterministic quality markers."
        )
    )
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="research-opportunity",
        help="Preconfigured dry-run comparison scenario.",
    )
    parser.add_argument(
        "--request",
        default="",
        help="Override the scenario request text.",
    )
    parser.add_argument(
        "--database-dir",
        default="",
        help="Optional directory for the two comparison SQLite databases.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=4,
        help="Maximum manager-loop steps per variant.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON.")
    parser.add_argument(
        "--review-packet",
        default="",
        help=(
            "Finalize an edited live-output review packet JSON instead of running "
            "a new offline comparison."
        ),
    )
    parser.add_argument(
        "--write-review-packet",
        default="",
        help=(
            "Write the generated or updated live-output review packet JSON to this "
            "path for Slack/API evidence capture."
        ),
    )
    parser.add_argument(
        "--merge-mode-evidence",
        choices=[
            "open_default_backend_selected",
            "forced_langgraph_false_control",
            "forced_langgraph_true",
        ],
        default="",
        help=(
            "With --review-packet, merge captured Slack/API evidence into this "
            "mode before finalizing the checkpoint and decision."
        ),
    )
    parser.add_argument(
        "--evidence-json",
        default="",
        help=(
            "JSON file containing captured Slack/KBA result evidence for "
            "--merge-mode-evidence. Use --evidence-from-work-item instead to "
            "merge DB-backed WorkItem evidence directly."
        ),
    )
    parser.add_argument(
        "--write-evidence-template",
        default="",
        help=(
            "Write a compact Slack/KBA evidence JSON template to this path, "
            "then exit without running a comparison."
        ),
    )
    parser.add_argument(
        "--evidence-from-work-item",
        default="",
        help=(
            "Build partial Slack/KBA evidence from a local WorkItem id. This "
            "extracts route/status, sources, graph events, and SDK usage from "
            "SQLite but still requires manual Slack visible-output and "
            "side-effect review."
        ),
    )
    parser.add_argument(
        "--evidence-database-url",
        default="",
        help="SQLite database URL or path for --evidence-from-work-item.",
    )
    parser.add_argument(
        "--evidence-mode",
        choices=[
            "open_default_backend_selected",
            "forced_langgraph_false_control",
            "forced_langgraph_true",
        ],
        default="open_default_backend_selected",
        help="Mode label to include in --write-evidence-template output.",
    )
    parser.add_argument(
        "--actual-environment",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Attach captured runner environment evidence to Slack/API evidence. "
            "Repeat for multiple values, for example "
            "KEYSTONE_WORKITEM_LANGGRAPH=false."
        ),
    )
    parser.add_argument(
        "--require-graph-better",
        action="store_true",
        help="With --review-packet, exit nonzero unless the decision is graph_better.",
    )
    parser.add_argument(
        "--score-review",
        action="append",
        default=[],
        metavar="CRITERION=WINNER",
        help=(
            "With --review-packet, set one live-output review winner without "
            "hand-editing JSON. Repeat for multiple criteria. Winner must be "
            "graph, control, tie, or unreviewed."
        ),
    )
    parser.add_argument(
        "--review-observation",
        action="append",
        default=[],
        metavar="CRITERION.FIELD=TEXT",
        help=(
            "With --review-packet, set a review observation. FIELD must be "
            "control or graph, mapping to control_observation or graph_observation."
        ),
    )
    parser.add_argument(
        "--include-output-rubric",
        action="store_true",
        help="Include the live Slack/API output usefulness review rubric.",
    )
    parser.add_argument(
        "--include-live-smoke-plan",
        action="store_true",
        help="Include the bounded live-smoke plan and stop conditions.",
    )
    parser.add_argument(
        "--edge-inventory",
        action="store_true",
        help=(
            "Print the selected LangGraph edge-program inventory and live-smoke "
            "boundary instead of running a comparison."
        ),
    )
    parser.add_argument(
        "--all-scenarios",
        action="store_true",
        help=(
            "Run every offline comparison scenario referenced by the selected "
            "edge-program inventory."
        ),
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Exit nonzero unless the comparison is ready for a bounded live smoke.",
    )
    return parser


def run_comparison(
    *,
    scenario: str = "research-opportunity",
    request_text: str = "",
    database_dir: str | Path | None = None,
    max_steps: int = 4,
    include_all_scenarios_gate: bool = False,
) -> dict[str, Any]:
    """Run a dry-run comparison and return structured quality markers."""

    scenario_config = _scenario_config(scenario)
    selected_request = request_text or str(scenario_config["request_text"])
    if database_dir:
        base_dir = Path(database_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        return _run_comparison_in_dir(
            scenario=scenario,
            scenario_config=scenario_config,
            request_text=selected_request,
            base_dir=base_dir,
            max_steps=max_steps,
            include_all_scenarios_gate=include_all_scenarios_gate,
        )
    with tempfile.TemporaryDirectory(prefix="kba-langgraph-quality-") as tmp:
        return _run_comparison_in_dir(
            scenario=scenario,
            scenario_config=scenario_config,
            request_text=selected_request,
            base_dir=Path(tmp),
            max_steps=max_steps,
            include_all_scenarios_gate=include_all_scenarios_gate,
        )


def run_all_scenarios(
    *,
    database_dir: str | Path | None = None,
    max_steps: int = 4,
) -> dict[str, Any]:
    """Run all inventory-referenced offline comparison scenarios."""

    inventory = langgraph_edge_program_inventory()
    validation = validate_langgraph_edge_program_inventory(
        inventory,
        known_quality_scenarios=SCENARIOS,
    )
    selected_scenarios = _selected_inventory_scenarios(inventory)
    missing_scenarios = [
        scenario for scenario in selected_scenarios if scenario not in SCENARIOS
    ]
    scenario_results: list[dict[str, Any]] = []

    def run_with_base(base_dir: Path) -> None:
        for scenario in selected_scenarios:
            if scenario in missing_scenarios:
                continue
            scenario_dir = base_dir / _safe_scenario_dir_name(scenario)
            scenario_results.append(
                run_comparison(
                    scenario=scenario,
                    database_dir=scenario_dir,
                    max_steps=max_steps,
                    include_all_scenarios_gate=False,
                )
            )

    if database_dir:
        base_dir = Path(database_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        run_with_base(base_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="kba-langgraph-all-scenarios-") as tmp:
            run_with_base(Path(tmp))

    scenario_summaries = [
        _scenario_result_summary(result) for result in scenario_results
    ]
    not_ready = [
        summary["scenario"]
        for summary in scenario_summaries
        if not bool(summary["ready_for_live_smoke"])
    ]
    regressions = [
        summary["scenario"]
        for summary in scenario_summaries
        if summary["regression_markers"]
    ]
    blockers = []
    if not validation.get("valid"):
        blockers.append("edge_inventory_not_valid")
    blockers.extend(f"missing_scenario:{scenario}" for scenario in missing_scenarios)
    blockers.extend(f"scenario_not_ready:{scenario}" for scenario in not_ready)
    blockers.extend(f"scenario_regressed:{scenario}" for scenario in regressions)
    return {
        "schema": "keystone.langgraph.all_scenarios_readiness.v1",
        "ready": not blockers,
        "blockers": blockers,
        "inventory_validation": validation,
        "scenario_count": len(scenario_summaries),
        "selected_scenarios": selected_scenarios,
        "missing_scenarios": missing_scenarios,
        "not_ready_scenarios": not_ready,
        "regressed_scenarios": regressions,
        "scenarios": scenario_summaries,
    }


def _run_comparison_in_dir(
    *,
    scenario: str,
    scenario_config: dict[str, Any],
    request_text: str,
    base_dir: Path,
    max_steps: int,
    include_all_scenarios_gate: bool,
) -> dict[str, Any]:
    control = _run_variant(
        request_text=request_text,
        database_url=f"sqlite:///{base_dir / 'langgraph_control.sqlite3'}",
        use_langgraph=False,
        max_steps=max_steps,
        manual_request_plan=dict(scenario_config["manual_request_plan"]),
        seed_rss=bool(scenario_config["seed_rss"]),
        seed_preprints=bool(scenario_config["seed_preprints"]),
    )
    with _cleared_langgraph_env():
        graph = _run_variant(
            request_text=request_text,
            database_url=f"sqlite:///{base_dir / 'langgraph_backend_selected.sqlite3'}",
            use_langgraph=None,
            max_steps=max_steps,
            manual_request_plan=dict(scenario_config["manual_request_plan"]),
            seed_rss=bool(scenario_config["seed_rss"]),
            seed_preprints=bool(scenario_config["seed_preprints"]),
        )
    comparison = compare_langgraph_quality(
        control["quality_markers"],
        graph["quality_markers"],
        allow_route_change=bool(scenario_config["allow_route_change"]),
        expected_graph_route=str(scenario_config["expected_graph_route"]),
    )
    all_scenarios_report = (
        run_all_scenarios(
            database_dir=base_dir / "all_scenarios_gate",
            max_steps=max_steps,
        )
        if include_all_scenarios_gate
        else None
    )
    all_scenarios_summary = (
        _all_scenarios_summary(all_scenarios_report) if all_scenarios_report else None
    )
    output_review_packet = langgraph_live_output_review_packet(
        request_text=request_text,
        control_output=control["output_surface"],
        graph_output=graph["output_surface"],
        quality_target_terms=(
            str(scenario_config["manual_request_plan"].get("primary_target") or ""),
        ),
        all_scenarios_ready=(
            bool(all_scenarios_report.get("ready")) if all_scenarios_report else None
        ),
        all_scenarios_summary=all_scenarios_summary,
    )
    open_smoke_checkpoint = langgraph_open_smoke_checkpoint(output_review_packet)
    output_review_decision = finalize_langgraph_live_output_review(output_review_packet)
    live_smoke_plan = langgraph_live_smoke_plan(
        scenario=scenario,
        request_text=request_text,
        comparison=comparison,
        all_scenarios_ready=(
            bool(all_scenarios_report.get("ready")) if all_scenarios_report else None
        ),
        all_scenarios_summary=all_scenarios_summary,
    )
    return {
        "schema": "keystone.langgraph.quality_comparison_run.v1",
        "scenario": scenario,
        "request_text": request_text,
        "control": control,
        "graph": graph,
        "comparison": comparison,
        "live_output_review_rubric": langgraph_live_output_review_rubric(),
        "live_output_review_packet": output_review_packet,
        "live_open_smoke_checkpoint": open_smoke_checkpoint,
        "live_output_review_decision": output_review_decision,
        "live_smoke_plan": live_smoke_plan,
        "all_scenarios_readiness": all_scenarios_report,
        "report": render_langgraph_quality_comparison(comparison),
    }


def _run_variant(
    *,
    request_text: str,
    database_url: str,
    use_langgraph: bool | None,
    max_steps: int,
    manual_request_plan: dict[str, Any],
    seed_rss: bool,
    seed_preprints: bool,
) -> dict[str, Any]:
    if seed_rss:
        _seed_rss_context(database_url)
    if seed_preprints:
        _seed_preprints_context(database_url)
    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            database_url=database_url,
            save=True,
            live_sdk=False,
            live_search=False,
            manual_request_plan=manual_request_plan,
        ),
        max_steps=max_steps,
        use_langgraph=use_langgraph,
    )
    store = SQLiteStore(database_url)
    events = store.list_work_item_events(result.work_item.id)
    markers = langgraph_quality_markers(result, events)
    return {
        "database_url": database_url,
        "work_item_id": result.work_item.id,
        "route": str(result.route.value),
        "status": str(result.status.value),
        "artifact_types": [artifact.artifact_type for artifact in result.work_item.artifact_refs],
        "event_types": [event.event_type for event in events],
        "quality_markers": markers,
        "output_surface": _output_surface(result),
    }


def _output_surface(result: Any) -> dict[str, Any]:
    work_item = result.work_item
    artifact_summaries = [
        {
            "artifact_type": artifact.artifact_type,
            "title": artifact.title,
            "summary": artifact.summary,
            "approval_state": artifact.approval_state,
        }
        for artifact in work_item.artifact_refs
        if artifact.summary or artifact.title
    ]
    source_urls = [
        source.url
        for source in work_item.sources
        if str(getattr(source, "url", "") or "").strip()
    ]
    blockers = [blocker.code for blocker in result.blockers]
    output_text = "\n".join(
        item
        for item in [
            str(result.human_summary or "").strip(),
            *[
                f"{artifact['artifact_type']}: {artifact['title']} {artifact['summary']}".strip()
                for artifact in artifact_summaries
            ],
            *[f"blocker: {code}" for code in blockers],
        ]
        if item
    )
    return {
        "route": str(result.route.value),
        "status": str(result.status.value),
        "human_summary": result.human_summary,
        "artifact_summaries": artifact_summaries,
        "artifact_summary_count": len(artifact_summaries),
        "source_urls": source_urls[:8],
        "source_url_count": len(source_urls),
        "blockers": blockers,
        "next_action": (
            result.next_action.model_dump(mode="json") if result.next_action is not None else None
        ),
        "audit_notes": list(result.audit_notes),
        "output_char_count": len(output_text),
    }


def _selected_inventory_scenarios(inventory: dict[str, Any]) -> list[str]:
    scenarios: list[str] = []
    for edge in inventory.get("selected_edges") or []:
        if not isinstance(edge, dict):
            continue
        for scenario in edge.get("quality_scenarios") or []:
            name = str(scenario or "").strip()
            if name and name not in scenarios:
                scenarios.append(name)
    return scenarios


def _safe_scenario_dir_name(scenario: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in scenario)


def _scenario_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    comparison = result.get("comparison") if isinstance(result, dict) else {}
    if not isinstance(comparison, dict):
        comparison = {}
    control = result.get("control") if isinstance(result, dict) else {}
    graph = result.get("graph") if isinstance(result, dict) else {}
    control_markers = (
        control.get("quality_markers") if isinstance(control, dict) else {}
    )
    graph_markers = graph.get("quality_markers") if isinstance(graph, dict) else {}
    if not isinstance(control_markers, dict):
        control_markers = {}
    if not isinstance(graph_markers, dict):
        graph_markers = {}
    return {
        "scenario": str(result.get("scenario") or ""),
        "ready_for_live_smoke": bool(comparison.get("ready_for_live_smoke")),
        "regression_markers": list(comparison.get("regression_markers") or []),
        "improvement_markers": list(comparison.get("improvement_markers") or []),
        "control_route": str(control_markers.get("route") or ""),
        "control_status": str(control_markers.get("status") or ""),
        "graph_route": str(graph_markers.get("route") or ""),
        "graph_status": str(graph_markers.get("status") or ""),
        "graph_checkpoint_required": bool(graph_markers.get("checkpoint_required")),
    }


def _all_scenarios_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "ready": bool(report.get("ready")),
        "scenario_count": int(report.get("scenario_count") or 0),
        "blockers": list(report.get("blockers") or []),
        "not_ready_scenarios": list(report.get("not_ready_scenarios") or []),
        "regressed_scenarios": list(report.get("regressed_scenarios") or []),
    }


def render_all_scenarios_readiness(report: dict[str, Any]) -> str:
    """Render the all-scenario readiness result for operator review."""

    lines = [
        "LangGraph all-scenario offline readiness",
        f"- Ready: {_yes_no(report.get('ready'))}",
        f"- Scenario count: {int(report.get('scenario_count') or 0)}",
    ]
    blockers = [str(item) for item in report.get("blockers") or [] if str(item)]
    lines.append(f"- Blockers: {', '.join(blockers) if blockers else 'none'}")
    for item in report.get("scenarios") or []:
        if not isinstance(item, dict):
            continue
        lines.append(
            "- "
            f"{item.get('scenario')}: "
            f"{item.get('control_route')}/{item.get('control_status')} -> "
            f"{item.get('graph_route')}/{item.get('graph_status')}; "
            f"ready={_yes_no(item.get('ready_for_live_smoke'))}; "
            f"regressions={len(item.get('regression_markers') or [])}"
        )
    return "\n".join(lines)


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _scenario_config(scenario: str) -> dict[str, Any]:
    try:
        return SCENARIOS[scenario]
    except KeyError as exc:
        raise ValueError(f"Unknown LangGraph comparison scenario: {scenario}") from exc


def _seed_rss_context(database_url: str) -> None:
    SQLiteStore(database_url).save_announcement_feed_item(
        AnnouncementFeedItem(
            title="NeuroFlow payer partnership outcomes update",
            url="https://example.org/neuroflow-payer-outcomes",
            source="#announcements",
            feed="rss",
            tags=["behavioral health", "payer partnership", "outcomes evidence"],
            selected=True,
            selection_reason=(
                "Relevant announcement signal for behavioral-health opportunity triage."
            ),
            summary=(
                "A behavioral-health AI vendor announced payer partnership and "
                "outcomes-evidence signals."
            ),
            evidence=[
                AnnouncementFeedEvidence(
                    kind="article",
                    title="NeuroFlow outcomes source",
                    url="https://example.org/neuroflow-payer-outcomes",
                    snippet=(
                        "The announcement describes payer partnership and "
                        "outcomes-evidence signals."
                    ),
                    source="trafilatura",
                    status="success",
                    char_count=850,
                )
            ],
        )
    )


def _seed_preprints_context(database_url: str) -> None:
    SQLiteStore(database_url).save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Preprint on depression evidence workflows",
            url="https://doi.org/10.1101/2026.02.04.234567",
            source="medRxiv",
            feed="preprints",
            doi="10.1101/2026.02.04.234567",
            tags=["preprint", "depression", "evidence"],
            selected=True,
            selection_reason="Relevant preliminary evidence for an internal packet.",
            summary=(
                "A preprint reviews evidence workflow considerations for depression "
                "measurement."
            ),
            evidence=[
                AnnouncementFeedEvidence(
                    kind="preprint",
                    title="Preprint evidence workflow source",
                    url="https://doi.org/10.1101/2026.02.04.234567",
                    snippet=(
                        "The preprint reviews preliminary evidence workflow "
                        "considerations for depression measurement."
                    ),
                    source="local_history",
                    status="historical_summary_only",
                    char_count=640,
                )
            ],
        )
    )


@contextmanager
def _cleared_langgraph_env():
    prior = {key: os.environ.get(key) for key in LANGGRAPH_WORKITEM_ENV_KEYS}
    try:
        for key in LANGGRAPH_WORKITEM_ENV_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.edge_inventory:
        inventory = langgraph_edge_program_inventory()
        validation = validate_langgraph_edge_program_inventory(
            inventory,
            known_quality_scenarios=SCENARIOS,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "inventory": inventory,
                        "validation": validation,
                    },
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(render_langgraph_edge_program_inventory(inventory))
        if args.require_ready and not validation.get("valid"):
            return 2
        return 0
    if args.all_scenarios:
        report = run_all_scenarios(
            database_dir=args.database_dir or None,
            max_steps=args.max_steps,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            print(render_all_scenarios_readiness(report))
        if args.require_ready and not report.get("ready"):
            return 2
        return 0
    if (args.write_evidence_template or args.evidence_from_work_item) and not args.review_packet:
        if args.evidence_from_work_item:
            template = _live_slack_evidence_from_work_item(
                mode=args.evidence_mode,
                work_item_id=args.evidence_from_work_item,
                database_url=args.evidence_database_url or None,
            )
        else:
            template = _live_slack_evidence_template(mode=args.evidence_mode)
        actual_environment = _parse_key_value_items(
            args.actual_environment,
            option="--actual-environment",
        )
        if actual_environment:
            template["actual_environment"] = actual_environment
        _validate_mode_evidence_consistency(mode=args.evidence_mode, evidence=template)
        if args.write_evidence_template:
            _write_json_file(args.write_evidence_template, template)
        if args.json:
            print(json.dumps(template, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            if args.write_evidence_template:
                print(f"Wrote live Slack evidence template: {args.write_evidence_template}")
            else:
                print(json.dumps(template, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    if args.review_packet:
        packet = _load_json_file(args.review_packet)
        if args.merge_mode_evidence:
            if bool(args.evidence_json) == bool(args.evidence_from_work_item):
                raise ValueError(
                    "--merge-mode-evidence requires exactly one of "
                    "--evidence-json or --evidence-from-work-item"
                )
            if args.evidence_from_work_item:
                evidence = _live_slack_evidence_from_work_item(
                    mode=args.merge_mode_evidence,
                    work_item_id=args.evidence_from_work_item,
                    database_url=args.evidence_database_url or None,
                )
            else:
                evidence = _load_json_file(args.evidence_json)
            actual_environment = _parse_key_value_items(
                args.actual_environment,
                option="--actual-environment",
            )
            if actual_environment:
                evidence["actual_environment"] = actual_environment
            packet = _merge_mode_evidence(
                packet,
                mode=args.merge_mode_evidence,
                evidence=evidence,
            )
            if args.write_review_packet:
                _write_json_file(args.write_review_packet, packet)
        if args.score_review:
            packet = _apply_review_scores(packet, args.score_review)
            if args.write_review_packet:
                _write_json_file(args.write_review_packet, packet)
        if args.review_observation:
            packet = _apply_review_observations(packet, args.review_observation)
            if args.write_review_packet:
                _write_json_file(args.write_review_packet, packet)
        open_smoke_checkpoint = langgraph_open_smoke_checkpoint(packet)
        decision = finalize_langgraph_live_output_review(packet)
        if args.json:
            print(
                json.dumps(
                    {
                        "live_output_review_packet": packet,
                        "live_open_smoke_checkpoint": open_smoke_checkpoint,
                        "live_output_review_decision": decision,
                    },
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            if (
                args.merge_mode_evidence or args.score_review or args.review_observation
            ) and args.write_review_packet:
                print(f"Wrote live-output review packet: {args.write_review_packet}")
            if args.include_live_smoke_plan:
                print(
                    render_langgraph_live_smoke_plan(
                        langgraph_live_smoke_plan_from_packet(packet)
                    )
                )
                print()
            print(render_langgraph_open_smoke_checkpoint(open_smoke_checkpoint))
            print()
            print(render_langgraph_live_output_review_decision(decision))
        if args.require_graph_better and decision.get("decision") != "graph_better":
            return 3
        return 0
    output = run_comparison(
        scenario=args.scenario,
        request_text=args.request,
        database_dir=args.database_dir or None,
        max_steps=args.max_steps,
        include_all_scenarios_gate=True,
    )
    if args.write_review_packet:
        _write_json_file(args.write_review_packet, output["live_output_review_packet"])
    if args.json:
        print(json.dumps(output, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(output["report"])
        if args.include_output_rubric:
            print()
            print(render_langgraph_live_output_review_rubric())
            print()
            print(render_langgraph_live_output_review_packet(output["live_output_review_packet"]))
            print()
            print(render_langgraph_live_output_review_decision(output["live_output_review_decision"]))
        if args.include_live_smoke_plan:
            print()
            print(render_langgraph_live_smoke_plan(output["live_smoke_plan"]))
            print()
            print(render_langgraph_open_smoke_checkpoint(output["live_open_smoke_checkpoint"]))
        if args.write_review_packet:
            print(f"Wrote live-output review packet: {args.write_review_packet}")
        print(f"Control route/status: {output['control']['route']} / {output['control']['status']}")
        print(f"Graph route/status: {output['graph']['route']} / {output['graph']['status']}")
    if args.require_ready and (
        not output["comparison"]["ready_for_live_smoke"]
        or not output["live_smoke_plan"]["ready_for_live_smoke"]
    ):
        return 2
    return 0


def _load_json_file(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def _write_json_file(path: str, data: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _merge_mode_evidence(
    packet: dict[str, Any],
    *,
    mode: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    mode_items = packet.get("mode_evidence")
    if not isinstance(mode_items, list):
        raise ValueError("review packet is missing mode_evidence list")
    evidence_mode = str(evidence.get("mode") or "").strip()
    if evidence_mode and evidence_mode != mode:
        raise ValueError(
            f"Evidence mode {evidence_mode!r} does not match merge mode {mode!r}"
        )
    partial_db_extraction = bool(evidence.get("partial_db_extraction"))
    normalized = _normalize_slack_mode_evidence(evidence)
    if partial_db_extraction:
        normalized["partial_db_extraction"] = True
    _validate_mode_evidence_consistency(mode=mode, evidence=normalized)
    for item in mode_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("mode") or "").strip() != mode:
            continue
        item.update(
            {
                key: value
                for key, value in normalized.items()
                if _should_merge_evidence_value(
                    key,
                    value,
                    target=item,
                    partial_db_extraction=partial_db_extraction,
                )
            }
        )
        return packet
    raise ValueError(f"review packet has no mode_evidence entry for {mode}")


def _parse_key_value_items(items: Sequence[str], *, option: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_item in items:
        key, separator, value = raw_item.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value:
            raise ValueError(f"{option} values must use KEY=VALUE with nonempty parts")
        parsed[key] = value
    return parsed


def _validate_mode_evidence_consistency(*, mode: str, evidence: dict[str, Any]) -> None:
    graph_event = evidence.get("langgraph_orchestration_event")
    if (
        mode in LIVE_OUTPUT_GRAPH_OFF_EVIDENCE_MODES
        and isinstance(graph_event, dict)
        and graph_event
    ):
        raise ValueError(
            f"Evidence for {mode!r} must not include langgraph_orchestration_event"
        )
    if (
        mode in LIVE_OUTPUT_GRAPH_EVIDENCE_MODES
        and evidence.get("partial_db_extraction") is True
        and (not isinstance(graph_event, dict) or not graph_event)
    ):
        raise ValueError(
            f"DB evidence for {mode!r} must include langgraph_orchestration_event"
        )
    if isinstance(graph_event, dict) and graph_event:
        if not _valid_langgraph_orchestration_event(graph_event):
            schema = str(graph_event.get("schema") or "").strip() or "missing"
            raise ValueError(
                "Evidence langgraph_orchestration_event schema "
                f"{schema!r} does not match {LIVE_OUTPUT_GRAPH_EVENT_SCHEMA!r}"
            )


def _valid_langgraph_orchestration_event(event: dict[str, Any]) -> bool:
    schema = str(event.get("schema") or "").strip()
    if schema:
        return schema == LIVE_OUTPUT_GRAPH_EVENT_SCHEMA
    runtime = str(event.get("runtime") or "").strip().lower()
    node_path = event.get("node_path")
    return runtime == "langgraph" and isinstance(node_path, list) and bool(node_path)


def _apply_review_scores(packet: dict[str, Any], scores: Sequence[str]) -> dict[str, Any]:
    questions = packet.get("review_questions")
    if not isinstance(questions, list):
        raise ValueError("review packet is missing review_questions list")
    by_criterion = _review_questions_by_criterion(packet)
    for score in scores:
        criterion, winner = _parse_review_score(score)
        if criterion not in by_criterion:
            raise ValueError(f"Unknown review criterion: {criterion}")
        by_criterion[criterion]["winner"] = winner
    return packet


def _apply_review_observations(
    packet: dict[str, Any],
    observations: Sequence[str],
) -> dict[str, Any]:
    by_criterion = _review_questions_by_criterion(packet)
    for observation in observations:
        criterion, field, text = _parse_review_observation(observation)
        if criterion not in by_criterion:
            raise ValueError(f"Unknown review criterion: {criterion}")
        by_criterion[criterion][field] = text
    return packet


def _review_questions_by_criterion(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    questions = packet.get("review_questions")
    if not isinstance(questions, list):
        raise ValueError("review packet is missing review_questions list")
    return {
        str(item.get("criterion") or "").strip(): item
        for item in questions
        if isinstance(item, dict) and str(item.get("criterion") or "").strip()
    }


def _parse_review_score(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if "=" not in raw:
        raise ValueError("--score-review values must use CRITERION=WINNER")
    criterion, winner = (part.strip() for part in raw.split("=", 1))
    if not criterion:
        raise ValueError("--score-review criterion cannot be empty")
    winner = winner.lower()
    if winner not in REVIEW_WINNERS:
        raise ValueError(f"Invalid review winner for {criterion}: {winner}")
    return criterion, winner


def _parse_review_observation(value: str) -> tuple[str, str, str]:
    raw = str(value or "").strip()
    if "=" not in raw:
        raise ValueError("--review-observation values must use CRITERION.FIELD=TEXT")
    left, text = (part.strip() for part in raw.split("=", 1))
    if "." not in left:
        raise ValueError("--review-observation left side must use CRITERION.FIELD")
    criterion, field_name = (part.strip() for part in left.rsplit(".", 1))
    field_map = {
        "control": "control_observation",
        "graph": "graph_observation",
        "control_observation": "control_observation",
        "graph_observation": "graph_observation",
    }
    field = field_map.get(field_name)
    if not criterion:
        raise ValueError("--review-observation criterion cannot be empty")
    if field is None:
        raise ValueError(f"Invalid review observation field for {criterion}: {field_name}")
    if not text:
        raise ValueError(f"Review observation text cannot be empty for {criterion}.{field_name}")
    return criterion, field, text


def _should_merge_evidence_value(
    key: str,
    value: Any,
    *,
    target: dict[str, Any],
    partial_db_extraction: bool,
) -> bool:
    if value is None or value in ("", {}, []):
        return False
    if (
        partial_db_extraction
        and target.get("side_effects_reviewed") is True
        and key in {"side_effects", "side_effects_reviewed"}
    ):
        return False
    return True


def _live_slack_evidence_template(*, mode: str) -> dict[str, Any]:
    return {
        "schema": "keystone.langgraph.live_slack_evidence.v1",
        "mode": mode,
        "partial_db_extraction": False,
        "actual_environment": {},
        "slack_permalink": "",
        "slack_message_ts": "",
        "slack_thread_ts": "",
        "run_id": "",
        "work_item_id": "",
        "route": "",
        "status": "",
        "operator_status": "",
        "slack_display_title": "",
        "visible_output": "",
        "source_urls": [],
        "side_effects": {
            "send_attempted": False,
            "gmail_draft_created": False,
            "external_post_created": False,
            "schedule_created": False,
            "external_write_performed": False,
        },
        "side_effects_reviewed": False,
        "workflow_sdk_usage_event": {
            "schema": "keystone.workflow_sdk_usage.v1",
            "usage": {},
            "cost": {},
            "request_cache": {},
        },
        "workflow_sdk_usage_events": [],
        "langgraph_orchestration_event": {},
        "notes": "",
        "capture_instructions": [
            "Copy route/status, operator_status, slack_display_title, visible_output, source_urls, and side_effects from the KBA Slack result payload when possible.",
            "For forced comparison modes, set actual_environment.KEYSTONE_WORKITEM_LANGGRAPH to the captured runner value.",
            "Keep status as canonical WorkItem state; use operator_status/slack_display_title for Slack-facing status.",
            "Keep side_effects explicit; every send/draft/post/schedule/write flag should be false for this comparison.",
            "Set side_effects_reviewed=true only after inspecting the run output/events for side effects.",
            "Use workflow_sdk_usage_events for every SDK usage event from the same WorkItem run; keep workflow_sdk_usage_event as the latest event for backward compatibility.",
            "Do not invent usage or cost values.",
            "For forced_langgraph_true, include the langgraph_orchestration event from the same WorkItem run.",
            "For open/default, merge this file before running forced comparison modes.",
        ],
    }


def _live_slack_evidence_from_work_item(
    *,
    mode: str,
    work_item_id: str,
    database_url: str | None,
) -> dict[str, Any]:
    store = SQLiteStore(database_url)
    work_item = store.get_work_item(work_item_id)
    if work_item is None:
        raise ValueError(f"WorkItem not found: {work_item_id}")
    events = store.list_work_item_events(work_item.id)
    event_payloads = [event.model_dump(mode="json") for event in events]
    raw_evidence = {
        "schema": "keystone.langgraph.live_slack_evidence.v1",
        "mode": mode,
        "partial_db_extraction": True,
        "work_item": work_item.model_dump(mode="json"),
        "work_item_id": work_item.id,
        "route": str(work_item.current_route.value),
        "status": str(work_item.status.value),
        "source_urls": [
            source.url
            for source in work_item.sources
            if str(getattr(source, "url", "") or "").strip()
        ],
        "side_effects": _side_effects_from_work_item(work_item),
        "side_effects_reviewed": False,
        "events": event_payloads,
        "notes": (
            "Partial DB extraction. Add Slack permalink, operator_status, "
            "slack_display_title, visible_output, and set side_effects_reviewed "
            "only after inspecting Slack output and WorkItem events."
        ),
    }
    template = _live_slack_evidence_template(mode=mode)
    normalized = _normalize_slack_mode_evidence(raw_evidence)
    template.update(
        {
            key: value
            for key, value in normalized.items()
            if value is not None and value not in ("", {}, [])
        }
    )
    template["side_effects_reviewed"] = False
    template["partial_db_extraction"] = True
    return template


def _side_effects_from_work_item(work_item: Any) -> dict[str, bool]:
    side_effects = {
        "send_attempted": False,
        "gmail_draft_created": False,
        "external_post_created": False,
        "schedule_created": False,
        "external_write_performed": False,
    }
    for artifact in getattr(work_item, "artifact_refs", []) or []:
        metadata = getattr(artifact, "metadata", {}) or {}
        if not isinstance(metadata, dict):
            continue
        nested = metadata.get("side_effects")
        candidates = [metadata]
        if isinstance(nested, dict):
            candidates.append(nested)
        for candidate in candidates:
            for key in side_effects:
                if key in candidate:
                    side_effects[key] = bool(side_effects[key] or candidate[key])
    return side_effects


def _normalize_slack_mode_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    result_payload = _dict_value(evidence.get("result"))
    work_item = _dict_value(evidence.get("work_item") or result_payload.get("work_item"))
    provenance = _dict_value(
        evidence.get("slack_run_provenance")
        or result_payload.get("slack_run_provenance")
        or evidence.get("run_provenance")
        or result_payload.get("run_provenance")
    )
    eval_record = _dict_value(evidence.get("eval_record") or result_payload.get("eval_record"))
    workflow_usage_events = (
        _workflow_sdk_usage_events_from_payload(evidence)
        or _workflow_sdk_usage_events_from_payload(result_payload)
        or _workflow_sdk_usage_events_from_events(evidence)
        or _workflow_sdk_usage_events_from_events(result_payload)
    )
    workflow_usage = (
        _dict_value(evidence.get("workflow_sdk_usage_event"))
        or _dict_value(result_payload.get("workflow_sdk_usage_event"))
        or _latest_workflow_sdk_usage_from_events(evidence)
        or _latest_workflow_sdk_usage_from_events(result_payload)
        or (workflow_usage_events[-1] if workflow_usage_events else {})
    )
    graph_orchestration = (
        _dict_value(evidence.get("langgraph_orchestration_event"))
        or _dict_value(result_payload.get("langgraph_orchestration_event"))
        or _latest_langgraph_orchestration_from_events(evidence)
        or _latest_langgraph_orchestration_from_events(result_payload)
    )
    source_urls = (
        _source_urls_from_payload(evidence)
        or _source_urls_from_payload(result_payload)
        or _source_urls_from_payload(work_item)
    )
    side_effects = _side_effects_from_payload(evidence) or _side_effects_from_payload(
        result_payload
    )
    side_effects_reviewed = _optional_bool_value(
        evidence.get("side_effects_reviewed")
        if "side_effects_reviewed" in evidence
        else result_payload.get("side_effects_reviewed")
    )
    visible_payload = result_payload if result_payload else evidence
    visible_output = (
        _string_value(evidence.get("visible_output"))
        or _string_value(result_payload.get("visible_output"))
        or business_agent_result_display_text(visible_payload)
        or business_agent_result_display_text(evidence)
    )
    return {
        "actual_environment": (
            _dict_value(evidence.get("actual_environment"))
            or _dict_value(result_payload.get("actual_environment"))
        ),
        "slack_message_ts": _first_string(
            evidence,
            result_payload,
            provenance,
            keys=("slack_message_ts", "source_message_ts", "selected_message_ts"),
        ),
        "slack_thread_ts": _first_string(
            evidence,
            result_payload,
            eval_record,
            provenance,
            keys=("slack_thread_ts", "source_thread_ts", "thread_ts"),
        ),
        "slack_permalink": _first_string(
            evidence,
            result_payload,
            eval_record,
            keys=("slack_permalink", "permalink", "url"),
        ),
        "run_id": _first_string(evidence, result_payload, eval_record, keys=("run_id", "id")),
        "work_item_id": _first_string(
            evidence,
            result_payload,
            work_item,
            provenance,
            keys=("work_item_id", "id"),
        ),
        "route": _first_string(evidence, result_payload, provenance, keys=("route",)),
        "status": _first_string(evidence, result_payload, provenance, keys=("status",)),
        "operator_status": _first_string(
            evidence,
            result_payload,
            keys=("operator_status",),
        ),
        "slack_display_title": _first_string(
            evidence,
            result_payload,
            keys=("slack_display_title",),
        ),
        "workflow_sdk_usage_event": workflow_usage,
        "workflow_sdk_usage_events": workflow_usage_events,
        "langgraph_orchestration_event": graph_orchestration,
        "visible_output": visible_output,
        "source_urls": source_urls,
        "side_effects": side_effects,
        "side_effects_reviewed": side_effects_reviewed,
        "notes": _first_string(evidence, result_payload, keys=("notes", "note")),
    }


def _latest_workflow_sdk_usage_from_events(payload: dict[str, Any]) -> dict[str, Any]:
    events = _workflow_sdk_usage_events_from_events(payload)
    return events[-1] if events else {}


def _workflow_sdk_usage_events_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    events = payload.get("workflow_sdk_usage_events")
    if isinstance(events, list):
        normalized = [event for event in events if isinstance(event, dict) and event]
        if normalized:
            return normalized
    event = payload.get("workflow_sdk_usage_event")
    if isinstance(event, dict) and event:
        return [event]
    return []


def _workflow_sdk_usage_events_from_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get("events") or payload.get("feedback_events") or []
    if not isinstance(events, list):
        return []
    usage_events: list[dict[str, Any]] = []
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or event.get("type") or "").strip()
        metadata = _dict_value(event.get("metadata") or event.get("payload"))
        if event_type == "workflow_sdk_usage" or metadata.get("schema") == (
            "keystone.workflow_sdk_usage.v1"
        ):
            usage_events.append(metadata)
    return list(reversed(usage_events))


def _latest_langgraph_orchestration_from_events(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload.get("events") or payload.get("feedback_events") or []
    if not isinstance(events, list):
        return {}
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or event.get("type") or "").strip()
        metadata = _dict_value(event.get("metadata") or event.get("payload"))
        if event_type == "langgraph_orchestration" or metadata.get("schema") == (
            "keystone.langgraph.orchestration.v1"
        ):
            return metadata
    return {}


def _source_urls_from_payload(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return []
    direct_urls = _string_list_value(payload.get("source_urls"))
    if direct_urls:
        return direct_urls
    sources = payload.get("sources") or payload.get("source_refs") or []
    if not isinstance(sources, list):
        return []
    urls: list[str] = []
    for source in sources:
        if isinstance(source, dict):
            value = _first_string(source, keys=("url", "source_url", "href"))
        else:
            value = _string_value(source)
        if value:
            urls.append(value)
    return urls


def _side_effects_from_payload(payload: dict[str, Any]) -> dict[str, bool]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get("side_effects") or payload.get("side_effect_checks")
    if not isinstance(value, dict):
        return {}
    return {str(key): bool(raw_value) for key, raw_value in value.items() if str(key).strip()}


def _string_list_value(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (_string_value(raw) for raw in value) if item]


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_value(value: Any) -> str:
    return str(value or "").strip()


def _optional_bool_value(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized in {"1", "true", "yes", "y", "reviewed"}
    return bool(value)


def _first_string(*payloads: dict[str, Any], keys: Sequence[str]) -> str:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in keys:
            value = _string_value(payload.get(key))
            if value:
                return value
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
