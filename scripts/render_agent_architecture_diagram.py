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
    "gmail_triage": ("Gmail Triage", "classify / draft"),
    "business_research_analyst": ("Business Research", "source briefs"),
    "opportunity_scout": ("Opportunity Scout", "score / dedupe"),
    "outreach_composer": ("Outreach Composer", "no-send drafts"),
    "airtable_context_agent": ("Airtable Context", "schema / records"),
    "google_workspace_context_agent": ("Google Workspace", "Drive / Docs / Sheets"),
    "zotero_context_agent": ("Zotero Context", "library evidence"),
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
        '  <desc id="desc">Generated architecture diagram showing entrypoints, Orchestrator-first preflight and review, WorkItem canonical state, backend graph selection, optional LangGraph execution, Chief of Staff operating synthesis, registered SDK agents, typed context packs, deterministic gates, traces, logs, evals, and renderers.</desc>',
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
        "      .routeLine { stroke: #334155; stroke-width: 2.5; fill: none; marker-end: url(#arrow); }",
        "      .reviewLine { stroke: #334155; stroke-width: 2.2; fill: none; marker-end: url(#arrow); stroke-dasharray: 4 4; }",
        "      .detLine { stroke: #d97706; stroke-width: 2.5; fill: none; marker-end: url(#arrow-det); }",
        "      .toolAgentLine { stroke: #0891b2; stroke-width: 2.5; fill: none; marker-end: url(#arrow-tool); }",
        "      .obsLine { stroke: #db2777; stroke-width: 2.5; fill: none; marker-end: url(#arrow-obs); }",
        "      .dash { stroke-dasharray: 7 5; }",
        "    </style>",
        "  </defs>",
        '  <rect class="bg" width="1800" height="1280"/>',
        '  <text class="title" x="52" y="56">Keystone Business Agents Architecture</text>',
        '  <text class="subtitle" x="54" y="84">Generated from the current AgentSpec registry, WorkItem graph runtime contracts, trace processor, and eval database shape.</text>',
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
            "Route advice, blockers, retrieval hints",
            "Compact preflight context across boundaries",
            "Deterministic output review before final rendering",
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
            "ManualRequestPlan",
            "OrchestratorResult",
            "WorkItems / SQLite canonical state",
            "Typed context packs",
            "Backend graph selection",
            "Approval gates and artifact refs",
        ),
        x=1010,
        y=215,
        max_chars=36,
    )
    lines.append('<text class="tiny" x="1010" y="350">Schema-light planning; graph-aware typed execution.</text>')

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
    lines.append('<text class="tiny" x="1390" y="356">Dashed dark arrows show deterministic Orchestrator review feedback.</text>')

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
            "Stages review plans, approvals, and blockers",
        ),
        x=482,
        y=500,
        max_chars=52,
    )
    lines.append('<text class="small" x="482" y="642">Positioning: inside the centralized Orchestrator + WorkItem path,</text>')
    lines.append('<text class="small" x="482" y="662">not a parallel router or alternate communication channel.</text>')

    _rect(lines, "specialist", 985, 430, 690, 245)
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
    lines.append('<text class="body" x="1010" y="610">Each run receives raw request + Orchestrator memo + typed context pack.</text>')
    lines.append('<text class="small" x="1010" y="638">Graph-worthy workflows use backend-selected LangGraph nodes; simple runs stay single-step.</text>')
    lines.append('<text class="small" x="1010" y="660">Edges include Chief/Gmail/context -> Research -> Opportunity/Outreach checkpoints.</text>')

    _rect(lines, "context", 70, 430, 310, 285)
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
    lines.append(f'<text class="small" x="95" y="{context_y + 60}">Context agents stage read-only evidence for graph handoffs.</text>')
    lines.append(f'<text class="small" x="95" y="{context_y + 80}">Provider writes use owning specialists or approved handlers.</text>')

    _rect(lines, "gate", 70, 735, 760, 195)
    lines.append('<text class="section" x="95" y="771">SDK Guardrails + Deterministic Gates</text>')
    _bullet_list(
        lines,
        (
            "SDK guardrails: input/output/tool scans for PHI, secrets, send-like actions, unsafe claims",
            "Python gates: approvals, source sufficiency, record identity, live flags, provider budgets",
            "Typed checks: schema reads, arithmetic, deduplication, context-pack readiness",
            "Tool policy: core reads, web search, deep retrieval, diagnostics, owned writes",
        ),
        x=95,
        y=805,
        max_chars=112,
        line_height=25,
    )
    lines.append('<text class="small" x="95" y="914">No send, Gmail draft, post, provider write, file write, or library mutation is automatic.</text>')

    _rect(lines, "context", 870, 735, 805, 195)
    lines.append('<text class="section" x="895" y="771">Evidence, Retrieval, and Integrations</text>')
    _bullet_list(
        lines,
        (
            "Search: dry-run, SearXNG, hosted web search, Exa, Tavily, Firecrawl, explicit Serper",
            "Extraction: Trafilatura / Firecrawl, HTML review, rendered browser diagnostics",
            "State: SQLite WorkItems, approvals, memory, artifacts, tool events, automations",
            "Private context: KNI docs, Slack threads, Gmail, Airtable, Google files, Zotero",
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
        max_chars=60,
    )
    lines.append('<text class="small" x="95" y="1168">Raw trace noise and internal JSON stay hidden unless debugging was requested.</text>')

    _rect(lines, "observability", 635, 980, 1040, 210)
    lines.append('<text class="section" x="660" y="1016">Traces, Logs, Evals, and Feedback Loop</text>')
    _bullet_list(
        lines,
        (
            "SDK summaries: keystone.sdk_run_summary.v1 tracks model, tools, retrieval, cost, session, route, review",
            "Structured logs: keystone.structured_log.v1 stores redacted correlation keys only",
            f"Eval stores: {', '.join(_eval_table_names())}, benchmark SQLite",
            "Optional LLM-as-judge belongs here for quality evals, not as an approval authority",
        ),
        x=660,
        y=1050,
        max_chars=118,
        line_height=25,
    )
    lines.append('<text class="small" x="660" y="1178">Promptfoo and Slack #evals exercise the same Orchestrator-first workflow path, then feed dashboards and regression tests.</text>')

    lines.extend(
        [
            '  <path class="routeLine" d="M380 250 C410 250 420 250 450 250"/>',
            '  <text class="label" x="398" y="236">raw request</text>',
            '  <path class="detLine" d="M920 250 C950 250 955 250 985 250"/>',
            '  <text class="label" x="938" y="236">state + memo</text>',
            '  <path class="detLine" d="M1150 370 C1150 376 1150 378 1150 382"/>',
            '  <text class="label" x="1164" y="379">canonical state</text>',
            '  <path class="routeLine" d="M1320 424 C1320 426 1320 428 1320 430"/>',
            '  <text class="label" x="1338" y="424">backend selected</text>',
            '  <path class="routeLine" d="M685 370 C685 395 685 405 685 430"/>',
            '  <text class="label" x="700" y="404">broad ops route</text>',
            '  <path class="toolAgentLine dash" d="M920 552 C950 552 955 552 985 552"/>',
            '  <text class="label" x="936" y="538">agents as tools</text>',
            '  <path class="toolAgentLine dash" d="M450 552 C410 552 405 552 380 552"/>',
            '  <text class="label" x="394" y="538">advisory read-plan</text>',
            '  <path class="detLine" d="M225 675 C225 705 310 725 390 735"/>',
            '  <text class="label" x="250" y="715">evidence</text>',
            '  <path class="detLine" d="M1320 675 C1320 705 1320 710 1320 735"/>',
            '  <text class="label" x="1335" y="714">verified tool use</text>',
            '  <path class="detLine" d="M650 675 C630 705 590 715 560 735"/>',
            '  <text class="label" x="604" y="716">guarded action</text>',
            '  <path class="detLine" d="M450 930 C425 955 390 965 350 980"/>',
            '  <text class="label" x="385" y="958">approved structure</text>',
            '  <path class="obsLine dash" d="M1280 930 C1240 955 1205 965 1160 980"/>',
            '  <text class="label" x="1200" y="958">diagnostics</text>',
            '  <path class="detLine" d="M1315 370 C1605 450 1650 650 1535 735"/>',
            '  <text class="label" x="1570" y="700">state joins</text>',
            '  <path class="routeLine" d="M790 370 C855 388 930 398 985 403"/>',
            '  <text class="label" x="870" y="388">routing / graph handoff</text>',
            '  <path class="reviewLine" d="M985 610 C880 590 860 430 820 370"/>',
            '  <text class="label" x="830" y="560">deterministic review</text>',
            '  <path class="reviewLine" d="M610 430 C610 400 620 390 640 370"/>',
            '  <text class="label" x="522" y="405">review CoS output</text>',
            '  <path class="obsLine dash" d="M1675 552 C1735 600 1725 925 1675 1065"/>',
            '  <text class="label" x="1558" y="820">trace / eval linkage</text>',
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
