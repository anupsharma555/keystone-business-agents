#!/usr/bin/env python3
# ruff: noqa: E501,F841
"""Render the current Keystone Business Agents architecture diagram.

The diagram intentionally uses only repo-local metadata and stdlib SVG output.
It is meant to be rerun after agent registry, workflow, observability, or eval
structure changes.
"""

from __future__ import annotations

import argparse
import html
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from textwrap import wrap

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from keystone_agents.agent_registry import (  # noqa: E402
    CHIEF_OF_STAFF_AGENT_SPEC,
    ORCHESTRATOR_AGENT_SPEC,
    SPECIALIST_AGENT_SPECS,
)

try:  # noqa: E402
    from promptfoo.eval_database import DATABASE_TABLE_SUMMARIES
except Exception:  # pragma: no cover - optional when promptfoo deps are absent
    DATABASE_TABLE_SUMMARIES = ()


DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "assets" / "kba-current-agent-architecture.svg"
UTC = UTC
WORKFLOW_ROUTES = {
    "gmail_triage",
    "business_research_analyst",
    "opportunity_scout",
    "outreach_composer",
}
AGENT_DISPLAY_LABELS = {
    "rag_retrieval_specialist": ("RAG Retrieval", "saved corpus evidence"),
    "gmail_triage": ("Gmail Triage", "classify / draft"),
    "business_research_analyst": ("Business Research", "source briefs"),
    "opportunity_scout": ("Opportunity Scout", "score / dedupe"),
    "outreach_composer": ("Outreach Composer", "no-send drafts"),
    "airtable_context_agent": ("Airtable Context", "schema / records"),
    "google_workspace_context_agent": ("Google Workspace", "Drive / Docs / Sheets"),
    "zotero_context_agent": ("Zotero Context", "library evidence"),
    "rss_context_agent": ("RSS Context", "feed evidence"),
    "preprints_context_agent": ("Preprints Context", "preprint evidence"),
}


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _text(
    lines: list[str],
    text: str,
    *,
    x: int,
    y: int,
    cls: str,
    max_chars: int = 70,
    line_height: int = 22,
) -> int:
    wrapped: list[str] = []
    for paragraph in text.splitlines() or [""]:
        wrapped.extend(wrap(paragraph, width=max_chars) or [""])
    for offset, line in enumerate(wrapped):
        lines.append(f'<text class="{cls}" x="{x}" y="{y + offset * line_height}">{_esc(line)}</text>')
    return y + max(0, len(wrapped) - 1) * line_height


def _bullet_list(
    lines: list[str],
    items: Iterable[str],
    *,
    x: int,
    y: int,
    cls: str = "body",
    max_chars: int = 52,
    line_height: int = 24,
    limit: int | None = None,
) -> int:
    current_y = y
    selected = list(items)
    if limit is not None:
        selected = selected[:limit]
    for item in selected:
        current_y = _text(
            lines,
            f"- {item}",
            x=x,
            y=current_y,
            cls=cls,
            max_chars=max_chars,
            line_height=line_height,
        )
        current_y += line_height
    return current_y


def _rect(lines: list[str], cls: str, x: int, y: int, width: int, height: int, *, rx: int = 14) -> None:
    lines.append(f'<rect class="{cls}" x="{x}" y="{y}" width="{width}" height="{height}" rx="{rx}"/>')


def _agent_label(route_name: str) -> str:
    return AGENT_DISPLAY_LABELS.get(route_name, (route_name.replace("_", " ").title(), ""))[0]


def _agent_role(route_name: str) -> str:
    return AGENT_DISPLAY_LABELS.get(route_name, ("", ""))[1]


def _workflow_specialists() -> list[object]:
    return [spec for spec in SPECIALIST_AGENT_SPECS if spec.route_name in WORKFLOW_ROUTES]


def _context_specialists() -> list[object]:
    return [spec for spec in SPECIALIST_AGENT_SPECS if spec.route_name not in WORKFLOW_ROUTES]


def _eval_table_names() -> list[str]:
    return [name for name, _timestamp_column in DATABASE_TABLE_SUMMARIES] or [
        "promptfoo_eval_runs",
        "promptfoo_case_results",
        "slack_eval_runs",
        "human_eval_reviews",
        "eval_trace_events",
    ]


def render_svg(*, generated_date: str) -> str:
    workflow_specs = _workflow_specialists()
    context_specs = _context_specialists()
    eval_dataset_count = sum(len(spec.eval_datasets) for spec in (*SPECIALIST_AGENT_SPECS, ORCHESTRATOR_AGENT_SPEC, CHIEF_OF_STAFF_AGENT_SPEC))
    validation_count = sum(len(spec.validation_paths) for spec in (*SPECIALIST_AGENT_SPECS, ORCHESTRATOR_AGENT_SPEC, CHIEF_OF_STAFF_AGENT_SPEC))
    lines: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="1280" viewBox="0 0 1800 1280" role="img" aria-labelledby="title desc">',
        '  <title id="title">Keystone Business Agents current architecture</title>',
        '  <desc id="desc">Generated architecture diagram showing entrypoints, agent-owned semantic decisions, Orchestrator-first control, request-scoped tools, bounded validation and recovery, WorkItem canonical state, optional LangGraph execution, cross-provider context, receipts, telemetry, and renderers. Representative handoffs include gmail_triage -> business_research_analyst -> outreach_composer -> approval_checkpoint; chief_of_staff -> airtable/google_workspace context -> approval_checkpoint; and rss/preprints/zotero context -> business research/opportunity specialist.</desc>',
        "  <defs>",
        '    <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#334155"/></marker>',
        '    <marker id="arrow-det" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#d97706"/></marker>',
        '    <marker id="arrow-tool" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#0891b2"/></marker>',
        '    <marker id="arrow-obs" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#db2777"/></marker>',
        "    <style>",
        "      .bg { fill: #f8fafc; }",
        "      .frame { fill: #ffffff; stroke: #cbd5e1; stroke-width: 2; }",
        "      .lane { fill: #f1f5f9; stroke: #cbd5e1; stroke-width: 2; }",
        "      .orchestrator { fill: #fff7ed; stroke: #ea580c; stroke-width: 3; }",
        "      .chief { fill: #ecfeff; stroke: #0891b2; stroke-width: 3; }",
        "      .graph { fill: #eef2ff; stroke: #4f46e5; stroke-width: 3; }",
        "      .specialist { fill: #ecfdf5; stroke: #16a34a; stroke-width: 2; }",
        "      .context { fill: #f0f9ff; stroke: #0284c7; stroke-width: 2; }",
        "      .gate { fill: #fffbeb; stroke: #d97706; stroke-width: 2; }",
        "      .state { fill: #f5f3ff; stroke: #7c3aed; stroke-width: 2; }",
        "      .observability { fill: #fdf2f8; stroke: #db2777; stroke-width: 2; }",
        "      .render { fill: #eff6ff; stroke: #2563eb; stroke-width: 2; }",
        "      .title { font: 700 34px Arial, sans-serif; fill: #0f172a; }",
        "      .subtitle { font: 400 16px Arial, sans-serif; fill: #475569; }",
        "      .section { font: 700 18px Arial, sans-serif; fill: #0f172a; }",
        "      .cardTitle { font: 700 15px Arial, sans-serif; fill: #0f172a; }",
        "      .body { font: 400 14px Arial, sans-serif; fill: #334155; }",
        "      .small { font: 400 12px Arial, sans-serif; fill: #475569; }",
        "      .tiny { font: 400 11px Arial, sans-serif; fill: #64748b; }",
        "      .label { font: 700 12px Arial, sans-serif; fill: #334155; }",
        "      .link { font: 700 12px Arial, sans-serif; fill: #2563eb; text-decoration: underline; }",
        "      .routeLine { stroke: #334155; stroke-width: 2.5; fill: none; marker-end: url(#arrow); }",
        "      .detLine { stroke: #d97706; stroke-width: 2.5; fill: none; marker-end: url(#arrow-det); }",
        "      .toolAgentLine { stroke: #0891b2; stroke-width: 2.5; fill: none; marker-end: url(#arrow-tool); }",
        "      .obsLine { stroke: #db2777; stroke-width: 2.5; fill: none; marker-end: url(#arrow-obs); }",
        "      .dash { stroke-dasharray: 7 5; }",
        "    </style>",
        "  </defs>",
        '  <rect class="bg" width="1800" height="1280"/>',
        '  <text class="title" x="52" y="56">Keystone Business Agents Architecture</text>',
        '  <text class="subtitle" x="54" y="84">Generated from AgentSpec, decision/tool contracts, WorkItem graph runtime, receipts, telemetry, and eval database shape.</text>',
        f'  <text class="tiny" x="1510" y="84">Generated {generated_date}</text>',
        '  <rect class="frame" x="40" y="115" width="1720" height="1115" rx="18"/>',
    ]

    _rect(lines, "lane", 70, 155, 310, 205)
    lines.append('<text class="section" x="95" y="190">Entrypoints</text>')
    _bullet_list(
        lines,
        (
            "Slack @KNI and message actions",
            "CLI ask and explicit agent mentions",
            "WorkItem continue or run again",
            "Scheduled automations",
            "Promptfoo and Slack eval harnesses",
        ),
        x=95,
        y=222,
        max_chars=38,
    )
    lines.append('<text class="tiny" x="95" y="350">Raw request wording is preserved.</text>')

    _rect(lines, "orchestrator", 450, 145, 470, 225)
    lines.append(f'<text class="section" x="482" y="181">{_esc(ORCHESTRATOR_AGENT_SPEC.agent_name)}</text>')
    _bullet_list(
        lines,
        (
            "First LLM control plane for natural-language asks",
            "Owns semantic route + ordered workflow choice",
            "Validated internal decision reaches specialist",
            "Decision + output review before rendering",
        ),
        x=482,
        y=215,
        max_chars=52,
    )
    lines.append('<text class="small" x="482" y="330">Explicit agent mention is routing advice, not authority.</text>')
    lines.append('<text class="small" x="482" y="350">Python gates remain authoritative.</text>')

    _rect(lines, "state", 985, 145, 330, 225)
    lines.append('<text class="section" x="1010" y="181">Planning and State Contract</text>')
    _bullet_list(
        lines,
        (
            "ManualRequestPlan compatibility envelope",
            "OrchestratorResult",
            "AgentDecisionRecord + validator outcome",
            "WorkItems / SQLite canonical state",
            "Typed context packs",
            "Backend graph selection",
        ),
        x=1010,
        y=213,
        cls="small",
        max_chars=44,
        line_height=22,
    )
    lines.append('<text class="tiny" x="1010" y="357">Schema-light planning; graph-aware typed execution.</text>')

    _rect(lines, "lane", 1365, 145, 310, 225)
    lines.append('<text class="section" x="1390" y="181">Connection Legend</text>')
    lines.append('<path class="routeLine" d="M1390 215 L1465 215"/>')
    lines.append('<text class="body" x="1480" y="220">Routing / handoff</text>')
    lines.append('<path class="detLine" d="M1390 252 L1465 252"/>')
    lines.append('<text class="body" x="1480" y="257">Deterministic gate or state</text>')
    lines.append('<path class="toolAgentLine dash" d="M1390 289 L1465 289"/>')
    lines.append('<text class="body" x="1480" y="294">Agents as tools</text>')
    lines.append('<path class="obsLine dash" d="M1390 326 L1465 326"/>')
    lines.append('<text class="body" x="1480" y="331">Traces / logs / evals</text>')
    lines.append('<text class="tiny" x="1390" y="356">Lines show ownership, not execution order.</text>')

    _rect(lines, "graph", 985, 382, 690, 42, rx=10)
    lines.append('<text class="cardTitle" x="1010" y="408">Backend graph selector: single specialist step or LangGraph WorkItem graph</text>')

    _rect(lines, "chief", 450, 430, 470, 245)
    lines.append(f'<text class="section" x="482" y="466">{_esc(CHIEF_OF_STAFF_AGENT_SPEC.agent_name)}</text>')
    _bullet_list(
        lines,
        (
            "Broad operating synthesis and coordination layer",
            "Plans Slack, workflow, and automation work",
            "Reads KNI docs, local context, Slack repo context",
            "May call specialists as advisory tools",
            "Selects typed cross-provider context + handoff",
            "Stages review plans, approvals, and blockers",
        ),
        x=482,
        y=500,
        max_chars=52,
    )
    lines.append('<text class="small" x="482" y="642">Positioning: inside the centralized Orchestrator + WorkItem path,</text>')
    lines.append('<text class="small" x="482" y="662">not a parallel router or alternate communication channel.</text>')

    _rect(lines, "specialist", 985, 430, 690, 265)
    lines.append('<text class="section" x="1010" y="466">Workflow Specialists</text>')
    card_x = 1010
    for spec in workflow_specs:
        _rect(lines, "specialist", card_x, 490, 150, 78, rx=10)
        label = _agent_label(spec.route_name)
        label_parts = label.split(" ", maxsplit=1)
        if len(label_parts) == 2 and spec.route_name != "gmail_triage":
            lines.append(
                f'<text class="cardTitle" x="{card_x + 16}" y="512">{_esc(label_parts[0])}</text>'
            )
            lines.append(
                f'<text class="cardTitle" x="{card_x + 16}" y="530">{_esc(label_parts[1])}</text>'
            )
            role_y = 548
            tools_y = 564
        else:
            lines.append(f'<text class="cardTitle" x="{card_x + 16}" y="518">{_esc(label)}</text>')
            role_y = 542
            tools_y = 560
        lines.append(f'<text class="small" x="{card_x + 16}" y="{role_y}">{_esc(_agent_role(spec.route_name))}</text>')
        lines.append(f'<text class="small" x="{card_x + 16}" y="{tools_y}">{len(spec.tools)} tools</text>')
        card_x += 166
    lines.append('<text class="body" x="1010" y="610">Each run receives raw request + validated decision + model-visible evidence.</text>')
    lines.append('<text class="small" x="1010" y="630">Model requests are separate from actual SDK tool calls and returned outputs.</text>')
    lines.append('<text class="tiny" x="1010" y="646">Research/Opportunity WorkItems: agent-owned SDK selection + no-reread repair.</text>')
    lines.append('<text class="tiny" x="1010" y="660">RSS/Preprints replay tool-free; A/W/Z have missing-tool + semantic repair.</text>')
    lines.append('<text class="tiny" x="1010" y="674">Chief nested decisions are trace-retained; supplied response is synthesis-only.</text>')
    lines.append('<text class="tiny" x="1010" y="688">Direct actions (continue, more research, find contact, revise) reuse the dispatched path.</text>')

    _rect(lines, "context", 70, 430, 310, 300)
    lines.append('<text class="section" x="95" y="466">Read / Context Specialists</text>')
    context_y = _bullet_list(
        lines,
        (_agent_label(spec.route_name) for spec in context_specs),
        x=95,
        y=500,
        max_chars=36,
        line_height=22,
    )
    lines.append(f'<text class="body" x="95" y="{context_y + 2}">Local KNI document evidence</text>')
    lines.append(f'<text class="body" x="95" y="{context_y + 26}">Slack and automation context tools</text>')
    note_y = _text(
        lines,
        "Context agents stage read-only evidence for graph handoffs.",
        x=95,
        y=context_y + 45,
        cls="small",
        max_chars=34,
        line_height=15,
    )
    _text(
        lines,
        "Provider writes stay with owning specialists or approved handlers.",
        x=95,
        y=note_y + 17,
        cls="small",
        max_chars=34,
        line_height=15,
    )

    _rect(lines, "gate", 70, 735, 760, 195)
    lines.append('<text class="section" x="95" y="771">SDK Guardrails + Deterministic Gates</text>')
    _bullet_list(
        lines,
        (
            "SDK guardrails: input/output/tool scans for PHI, secrets, send-like actions, unsafe claims",
            "Python gates: request tool scope, approvals, identity, live flags, provider budgets; hard N+1 model ledger",
            "Typed checks: model-visible candidates, decision coverage, schema, arithmetic, dedupe",
            "Postconditions: required/optional/forbidden tool evidence; bounded correction/repair",
            "Negated constraints do not steer routes: 'do not scout opportunities' blocks that route",
        ),
        x=95,
        y=805,
        max_chars=112,
        line_height=25,
    )
    lines.append('<text class="small" x="95" y="924">Approval checkpoints and no-send/no-write gates remain Python-authoritative after graph steps.</text>')

    _rect(lines, "context", 870, 735, 805, 195)
    lines.append('<text class="section" x="895" y="771">Evidence, Retrieval, and Integrations</text>')
    _bullet_list(
        lines,
        (
            "Search: dry-run, SearXNG, hosted web search, Exa, Tavily, Firecrawl, explicit Serper",
            "Extraction: Trafilatura / Firecrawl, HTML review, rendered browser diagnostics",
            "Receipts: attempts, successes, durable proof, read-back, safe retry point",
            "State/context: SQLite WorkItems, artifacts, KNI docs, Slack, Gmail, Airtable, Workspace, Zotero",
        ),
        x=895,
        y=805,
        max_chars=110,
        line_height=25,
    )
    lines.append('<text class="small" x="895" y="914">Graph-produced sources can satisfy downstream offline evidence gates.</text>')

    _rect(lines, "render", 70, 980, 520, 210)
    lines.append('<text class="section" x="95" y="1016">Reviewed Output and Renderers</text>')
    _bullet_list(
        lines,
        (
            "Response synthesis produces operator-facing prose from structured outputs",
            "No-SDK draft paths ask for model reasoning or exact copy, not placeholders",
            "Slack / CLI / artifact renderers own final layout",
            "Source-backed external facts stay visible in first answer",
        ),
        x=95,
        y=1050,
        cls="small",
        max_chars=75,
        line_height=23,
    )
    lines.append('<text class="small" x="95" y="1168">Raw trace noise and internal JSON stay hidden unless debugging was requested.</text>')

    _rect(lines, "observability", 635, 980, 1040, 210)
    lines.append('<text class="section" x="660" y="1016">Traces, Logs, Evals, and Feedback Loop</text>')
    _bullet_list(
        lines,
        (
            "keystone.sdk_run_summary.v1 separates model requests, actual SDK tool calls, workflow/pre-acquired/helper origins",
            "Request budget: correlation, consumed, remaining, exhaustion; rejected before N+1 model call",
            "Semantic stage rows: initial / tool correction / decision repair; transport retries stay ledger-bound",
            "Structured logs: keystone.structured_log.v1 stores redacted correlation keys only",
            f"Correlated traces + eval stores: {', '.join(_eval_table_names())}, benchmark SQLite",
        ),
        x=660,
        y=1050,
        cls="small",
        max_chars=135,
        line_height=23,
    )
    lines.append('<text class="small" x="660" y="1178">Promptfoo and Slack #evals exercise the same Orchestrator-first workflow path, then feed dashboards and regression tests.</text>')

    lines.extend(
        [
            '  <path class="routeLine" d="M380 250 L450 250"/>',
            '  <path class="detLine" d="M920 250 L985 250"/>',
            '  <path class="detLine" d="M1150 370 L1150 382"/>',
            '  <path class="routeLine" d="M1320 424 L1320 430"/>',
            '  <path class="routeLine" d="M685 370 L685 430"/>',
            '  <path class="toolAgentLine dash" d="M920 552 L985 552"/>',
            '  <path class="toolAgentLine dash" d="M450 552 L380 552"/>',
            '  <path class="detLine" d="M380 700 L405 700 L405 735"/>',
            '  <path class="detLine" d="M1320 695 L1320 735"/>',
            '  <path class="detLine" d="M650 675 L560 735"/>',
            '  <path class="detLine" d="M450 930 L350 980"/>',
            '  <path class="obsLine dash" d="M1280 930 L1160 980"/>',
            '  <a href="kba-request-execution-sequence.svg"><text class="link" x="70" y="1218">Numbered request sequence</text></a>',
            '  <a href="kba-workitem-langgraph-topology.svg"><text class="link" x="255" y="1218">Compiled WorkItem topology</text></a>',
            "</svg>",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"SVG output path. Defaults to {DEFAULT_OUTPUT.relative_to(PROJECT_ROOT)}.",
    )
    parser.add_argument(
        "--generated-date",
        default=datetime.now(UTC).date().isoformat(),
        help="Date label to write into the diagram.",
    )
    args = parser.parse_args()

    output_path = args.output
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_svg(generated_date=args.generated_date), encoding="utf-8")
    print(f"wrote {output_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
