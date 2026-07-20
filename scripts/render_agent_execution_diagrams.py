#!/usr/bin/env python3
"""Render integrated, sequence, and executable WorkItem graph diagrams.

The integrated view connects system ownership, ordered request flow, read-only
context agents, direct execution, and a source-derived graph summary. The
sequence view isolates operator-facing order. The topology view is generated
from the compiled LangGraph node and edge metadata so the documentation cannot
silently invent a second workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from keystone_agents.agent_registry import (  # noqa: E402
    CHIEF_OF_STAFF_AGENT_SPEC,
    ORCHESTRATOR_AGENT_SPEC,
    SPECIALIST_AGENT_SPECS,
)
from keystone_agents.langgraph_workflow import (  # noqa: E402
    LangGraphUnavailableError,
    build_work_item_langgraph,
    langgraph_available,
)

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "docs" / "assets"
INTEGRATED_STEM = "kba-integrated-agent-architecture"
SEQUENCE_STEM = "kba-request-execution-sequence"
TOPOLOGY_STEM = "kba-workitem-langgraph-topology"
WORKFLOW_ROUTES = {
    "gmail_triage",
    "business_research_analyst",
    "opportunity_scout",
    "outreach_composer",
}
CONTEXT_STAGE_LABELS = {
    "stage_feed_context": "RSS + Preprints context agents",
    "stage_zotero_context": "Zotero context agent",
    "stage_airtable_context": "Airtable context agent",
    "stage_google_workspace_context": "Google Workspace context agent",
}
NODE_LABELS = {
    "__start__": "START",
    "__end__": "END",
    "normalize_request": "normalize_request\nPreserve raw ask + bounded context",
    "orchestrator_preflight": (
        "orchestrator_preflight\nLLM interpretation, safe defaults, route advice"
    ),
    "state_followup": "state_followup\nAnswer state-only follow-up or continue",
    "prepare_work_item": "prepare_work_item\nCanonical WorkItem + typed context pack",
    **{
        node_name: f"{node_name}\n{description}"
        for node_name, description in CONTEXT_STAGE_LABELS.items()
    },
    "run_business_research": "run_business_research\nBusiness Research Analyst",
    "run_opportunity_scout": "run_opportunity_scout\nOpportunity Scout",
    "run_gmail_triage": "run_gmail_triage\nGmail Triage",
    "run_outreach_composer": "run_outreach_composer\nOutreach Composer",
    "run_chief_of_staff": "run_chief_of_staff\nChief of Staff manager",
    "run_unsupported_route": "run_unsupported_route\nUseful blocker + next safe action",
    "finalize_step": "finalize_step\nValidate result + update canonical state",
    "manager_loop_continue": "manager_loop_continue\nAdvance another planned stage",
    "manager_loop_finalize": "manager_loop_finalize\nCompose terminal graph result",
    "approval_checkpoint": "approval_checkpoint\nPause before gated next action",
}


@dataclass(frozen=True, order=True)
class RuntimeEdge:
    """Serializable edge metadata from the compiled LangGraph."""

    source: str
    target: str
    label: str = ""
    conditional: bool = False


@dataclass(frozen=True)
class RuntimeTopology:
    """Compiled graph nodes and edges used by the topology renderer."""

    nodes: tuple[str, ...]
    edges: tuple[RuntimeEdge, ...]


def _dot_quote(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{text}"'


def _dot_node(node_id: str, *, attrs: dict[str, str]) -> str:
    rendered = ", ".join(f"{key}={_dot_quote(value)}" for key, value in attrs.items())
    return f"    {_dot_quote(node_id)} [{rendered}];"


def _runtime_topology() -> RuntimeTopology:
    if not langgraph_available():
        raise LangGraphUnavailableError(
            "The topology diagram requires the optional LangGraph dependency. "
            "Install the repo orchestration extra before regenerating it."
        )
    runtime_graph = build_work_item_langgraph().get_graph()
    nodes = tuple(sorted(str(node_id) for node_id in runtime_graph.nodes))
    edges = tuple(
        sorted(
            RuntimeEdge(
                source=str(edge.source),
                target=str(edge.target),
                label=str(edge.data or ""),
                conditional=bool(edge.conditional),
            )
            for edge in runtime_graph.edges
        )
    )
    return RuntimeTopology(nodes=nodes, edges=edges)


def _registered_context_agent_names() -> tuple[str, ...]:
    return tuple(
        spec.agent_name
        for spec in SPECIALIST_AGENT_SPECS
        if spec.route_name not in WORKFLOW_ROUTES
    )


def _registered_workflow_agent_names() -> tuple[str, ...]:
    return tuple(
        spec.agent_name
        for spec in SPECIALIST_AGENT_SPECS
        if spec.route_name in WORKFLOW_ROUTES
    )


def _runtime_group_label(topology: RuntimeTopology, node_ids: tuple[str, ...]) -> str:
    present = [node_id for node_id in node_ids if node_id in topology.nodes]
    return "\n".join(present)


def build_integrated_architecture_dot(
    topology: RuntimeTopology,
    *,
    generated_date: str,
) -> str:
    """Return one readable overall map with an ordered execution backbone."""

    context_nodes = tuple(node for node in topology.nodes if node.startswith("stage_"))
    specialist_nodes = tuple(node for node in topology.nodes if node.startswith("run_"))
    expected_nodes = {
        "__start__",
        "__end__",
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
        "finalize_step",
        "manager_loop_continue",
        "manager_loop_finalize",
        "approval_checkpoint",
        *context_nodes,
        *specialist_nodes,
    }
    missing_nodes = expected_nodes - set(topology.nodes)
    if missing_nodes:
        raise ValueError(
            "Integrated architecture cannot summarize missing runtime nodes: "
            + ", ".join(sorted(missing_nodes))
        )

    registered_workflow = "\n".join(_registered_workflow_agent_names())
    graph_control = (
        "Preserve the raw ask\n"
        "Orchestrator preflight\n"
        "State-aware follow-up\n"
        "(__start__ • normalize_request • orchestrator_preflight • state_followup)"
    )
    graph_context = (
        "Stage only the evidence the request needs\n"
        "feed/preprint • Airtable • Workspace • Zotero\n"
        f"({_runtime_group_label(topology, context_nodes).replace(chr(10), ' • ')})"
    )
    graph_specialists = (
        "Run the next owning agent\n"
        "Research • Chief • Gmail • Opportunity • Outreach\n"
        f"({_runtime_group_label(topology, specialist_nodes).replace(chr(10), ' • ')})"
    )
    graph_manager = (
        "Continue the next planned stage\n"
        "or finalize one reviewed result\n"
        "(manager_loop_continue • manager_loop_finalize)"
    )
    context_purposes = {
        "airtable_context_agent": "Base, table, field, and record evidence",
        "google_workspace_context_agent": "Drive, Docs, and Sheets evidence",
        "zotero_context_agent": "Library, collection, and item evidence",
        "rss_context_agent": "Feed and article evidence",
        "preprints_context_agent": "Preprint and article evidence",
    }
    context_catalog_rows: list[str] = []
    for spec in SPECIALIST_AGENT_SPECS:
        if spec.route_name in WORKFLOW_ROUTES:
            continue
        context_catalog_rows.append(
            f"{spec.agent_name} — "
            f"{context_purposes.get(spec.route_name, 'Read-only bounded evidence')}"
        )
    context_catalog = "\n".join(context_catalog_rows)

    lines = [
        "digraph kba_integrated_agent_architecture {",
        '  graph [rankdir=TB, bgcolor="#f8fafc", pad="0.28", nodesep="0.34", '
        'ranksep="0.58", splines=polyline, compound=true, newrank=true, '
        'ratio="compress", '
        'fontname="Arial", fontsize=21, labelloc=t, '
        'label="Keystone Business Agents — one natural-language request, two execution shapes\\n'
        f"Generated {generated_date} • numbered arrows show request order • "
        "context staging can feed either path • "
        f"graph branch derived from {len(topology.nodes)} compiled nodes / "
        f'{len(topology.edges)} compiled edges"];',
        '  node [shape=box, style="rounded,filled", color="#94a3b8", '
        'fillcolor="#ffffff", fontname="Arial", fontsize=10, margin="0.15,0.10"];',
        '  edge [color="#475569", fontcolor="#334155", fontname="Arial", '
        'fontsize=9, penwidth=1.35, arrowsize=0.66];',
        "",
        "  subgraph cluster_intake {",
        '    label="A — NATURAL-LANGUAGE CONTROL PLANE"; fontsize=15; '
        'color="#fdba74"; fontcolor="#9a3412"; style="rounded,dashed";',
        '    entry [label="1  Operator entry\\n@KNI CoS • direct agent mention\\n'
        'Slack action • continuation • automation", fillcolor="#f1f5f9", '
        'color="#64748b", width=2.7];',
        '    payload [label="2  Preserve the request\\nRaw ask + bounded thread + saved '
        'work identity\\npayload manifest • truncation • attachment proof", '
        'fillcolor="#eff6ff", color="#2563eb", width=3.0];',
        (
            f'    orchestrator [label="3  {ORCHESTRATOR_AGENT_SPEC.agent_name}\\n'
            "Interpret the goal, likely owner, assumptions, and next safe action\\n"
            'An agent mention is routing advice—not authority", fillcolor="#fff7ed", '
            'color="#ea580c", penwidth=2.0, width=3.8];'
        ),
        '    context_resolution [label="4  Resolve uncertainty intelligently\\nInfer safe '
        'defaults • retrieve bounded context\\nask only about material ambiguity", '
        'fillcolor="#fff7ed", color="#ea580c", width=3.3];',
        '    gates [label="5  Set the safe execution envelope\\napproval • exact identity '
        '• provider/live gates\\nsource sufficiency • exact write scope", '
        'fillcolor="#fffbeb", color="#d97706", width=3.2];',
        '    shape [shape=diamond, label="6  Execution shape?\\nBounded single owner\\n'
        'or stateful / multi-owner", fillcolor="#eef2ff", color="#4f46e5", '
        'width=2.6, height=1.05];',
        "    { rank=same; entry; payload; orchestrator; }",
        "    { rank=same; context_resolution; gates; shape; }",
        "  }",
        "",
        "  subgraph cluster_context_agents {",
        '    label="OPTIONAL READ / CONTEXT STAGING — invoked only when the request '
        'needs evidence"; fontsize=15; color="#7dd3fc"; fontcolor="#075985"; '
        'style="rounded,dashed";',
        (
            '    context_catalog [label="Registered read/context specialists\\n'
            f'{_dot_quote(context_catalog)[1:-1]}\\n'
            'Selected by meaning; stage evidence only", fillcolor="#f0f9ff", '
            'color="#0284c7", width=5.4];'
        ),
        '    context_contract [label="Typed evidence packet\\nsource refs • schema • exact '
        'object identity\\nconstraints • uncertainty • blockers", '
        'fillcolor="#e0f2fe", color="#0284c7", penwidth=2.0, width=3.4];',
        "    { rank=same; context_catalog; context_contract; }",
        "  }",
        "",
        "  subgraph cluster_direct {",
        '    label="B1 — DIRECT / BOUNDED SINGLE-OWNER"; fontsize=15; '
        'color="#86efac"; fontcolor="#166534"; style="rounded,dashed";',
        (
            f'    direct_owner [label="7A  One owner\\nDirect specialist, or '
            f'{CHIEF_OF_STAFF_AGENT_SPEC.agent_name}\\ndelegates one bounded task", '
            'fillcolor="#ecfdf5", color="#16a34a", width=3.0];'
        ),
        (
            '    direct_specialist [label="8A  Owning SDK specialist\\n'
            f'{_dot_quote(registered_workflow)[1:-1]}\\n'
            'or one registered context specialist", '
            'fillcolor="#ecfdf5", color="#16a34a", width=3.0];'
        ),
        '    direct_tool [label="9A  Typed helper executes exact scope\\nread • query • '
        'create • update\\nmarked cleanup • read-after-write", fillcolor="#ecfdf5", '
        'color="#16a34a", width=3.0];',
        "  }",
        "",
        "  subgraph cluster_graph {",
        '    label="B2 — WORKITEM / LANGGRAPH RUNTIME — stateful or multi-owner"; '
        'fontsize=15; color="#a5b4fc"; fontcolor="#3730a3"; style="rounded,dashed";',
        (
            '    graph_control [label="7B  Request control\\n'
            f'{_dot_quote(graph_control)[1:-1]}", '
            'fillcolor="#fff7ed", color="#ea580c", width=2.6];'
        ),
        '    graph_workitem [label="8B  Canonical WorkItem\\nprepare_work_item • SQLite '
        'state\\ntyped context pack + selected backend", fillcolor="#f5f3ff", '
        'color="#7c3aed", penwidth=2.0, width=2.8];',
        (
            '    graph_context [label="9B  Graph context stages\\n'
            f'{_dot_quote(graph_context)[1:-1]}", '
            'fillcolor="#f0f9ff", color="#0284c7", width=2.8];'
        ),
        (
            '    graph_agents [label="10B  Specialist branches\\n'
            f'{_dot_quote(graph_specialists)[1:-1]}", '
            'fillcolor="#ecfdf5", color="#16a34a", width=2.8];'
        ),
        '    graph_finalize [label="11B  finalize_step\\nValidate result + update canonical '
        'state", fillcolor="#f5f3ff", color="#7c3aed", width=2.7];',
        (
            '    graph_manager [label="12B  Continue or finish\\n'
            f'{_dot_quote(graph_manager)[1:-1]}", '
            'fillcolor="#eef2ff", color="#4f46e5", width=2.7];'
        ),
        '    graph_approval [label="Approval branch\\napproval_checkpoint\\nPause at the '
        'exact gated action", fillcolor="#fffbeb", color="#d97706", width=2.6];',
        '    graph_end [label="Terminal branch\\n__end__ • reviewed WorkItem result", '
        'shape=oval, fillcolor="#f1f5f9", color="#64748b", width=2.8];',
        "    { rank=same; graph_control; graph_workitem; graph_context; }",
        "    { rank=same; graph_agents; graph_finalize; graph_manager; }",
        "    { rank=same; graph_approval; graph_end; }",
        "  }",
        "",
        "  subgraph cluster_integrations {",
        '    label="C — TOOLS, PROVIDERS, AND VERIFIED STATE"; fontsize=15; '
        'color="#93c5fd"; fontcolor="#1e40af"; style="rounded,dashed";',
        '    providers [label="Bounded provider integrations\\nCalendar • Gmail • Airtable '
        '• Workspace • Zotero\\nSlack • search/extraction • local stores", '
        'fillcolor="#eff6ff", color="#2563eb", width=3.4];',
        '    receipt [label="13  Verified receipt\\nprovider ID • completed stages • '
        'read-back\\npartial-success state • safe retry point", '
        'fillcolor="#eff6ff", color="#2563eb", penwidth=2.0, width=3.4];',
        "    { rank=same; providers; receipt; }",
        "  }",
        "",
        "  subgraph cluster_output {",
        '    label="D — REVIEW, RENDER, AND FEEDBACK"; fontsize=15; '
        'color="#f9a8d4"; fontcolor="#9d174d"; style="rounded,dashed";',
        '    review [label="14  Orchestrator / deterministic review\\nrequest coverage • '
        'receipt truth • safety\\nuseful blocker or repair", '
        'fillcolor="#fff7ed", color="#ea580c", width=3.3];',
        '    render [label="15  One operator-facing answer\\nSlack thread • CLI • '
        'artifact\\nresult first; metadata stays secondary", fillcolor="#eff6ff", '
        'color="#2563eb", width=3.3];',
        '    telemetry [label="Traces • logs • evals • audit\\nroute + tool + cost + '
        'receipt linkage", fillcolor="#fdf2f8", color="#db2777", width=3.0];',
        "    { rank=same; review; render; telemetry; }",
        "  }",
        "",
        '  entry -> payload [label="1"];',
        '  payload -> orchestrator [label="2"];',
        '  orchestrator -> context_resolution [label="3"];',
        '  context_resolution -> gates [label="4"];',
        '  gates -> shape [label="5"];',
        '  context_resolution -> context_catalog [label="optional bounded retrieval", '
        'color="#0284c7", style=dashed, constraint=false];',
        '  context_catalog -> context_contract [label="typed evidence", '
        'color="#0284c7", arrowsize=0.60];',
        "",
        '  shape -> direct_owner [label="6A  direct", color="#16a34a"];',
        '  direct_owner -> direct_specialist [label="7A", color="#16a34a"];',
        '  context_contract -> direct_specialist [label="optional context", '
        'color="#0284c7", style=dashed];',
        '  direct_specialist -> direct_tool [label="8A", color="#16a34a"];',
        '  direct_tool -> providers [label="9A  exact call", color="#16a34a"];',
        "",
        '  shape -> graph_control [label="6B  graph", color="#4f46e5"];',
        '  graph_control -> graph_workitem [label="7B", color="#4f46e5"];',
        '  graph_workitem -> graph_context [label="8B", color="#0284c7"];',
        '  context_contract -> graph_context [label="typed context", '
        'color="#0284c7", style=dashed];',
        '  graph_context -> graph_agents [label="9B", color="#0284c7"];',
        '  graph_agents -> graph_finalize [label="10B", color="#16a34a"];',
        '  graph_finalize -> graph_manager [label="11B", color="#7c3aed"];',
        '  graph_manager -> graph_workitem [label="next stage", color="#4f46e5", '
        'constraint=false];',
        '  graph_manager -> graph_approval [label="gated", color="#d97706"];',
        '  graph_approval -> graph_workitem [label="resume", color="#d97706", '
        'constraint=false];',
        '  graph_manager -> graph_end [label="complete", color="#4f46e5"];',
        '  graph_agents -> providers [label="typed calls", color="#16a34a"];',
        "",
        '  providers -> receipt [label="10A / provider state", color="#2563eb"];',
        '  graph_end -> receipt [label="12B", color="#4f46e5"];',
        '  receipt -> review [label="13"];',
        '  review -> render [label="14"];',
        '  render -> telemetry [label="15  same caller/thread", color="#db2777"];',
        "  { rank=same; direct_owner; graph_control; }",
        "  { rank=same; direct_tool; graph_end; graph_approval; }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def build_request_sequence_dot(*, generated_date: str) -> str:
    """Return a numbered direct-versus-stateful request flow in DOT."""

    context_names = " • ".join(_registered_context_agent_names())
    workflow_names = " • ".join(_registered_workflow_agent_names())
    lines = [
        "digraph kba_request_execution_sequence {",
        '  graph [rankdir=TB, bgcolor="#f8fafc", pad="0.35", nodesep="0.38", '
        'ranksep="0.62", splines=polyline, fontname="Arial", fontsize=22, '
        'labelloc=t, label="Keystone @KNI request execution sequence\\n'
        f"Generated {generated_date} • semantic interpretation first; "
        'deterministic gates remain authoritative"];',
        '  node [shape=box, style="rounded,filled", color="#94a3b8", '
        'fillcolor="#ffffff", fontname="Arial", fontsize=12, margin="0.16,0.10"];',
        '  edge [color="#475569", fontcolor="#334155", fontname="Arial", '
        'fontsize=10, penwidth=1.5, arrowsize=0.75];',
        "",
        '  entry [label="Operator entry\\n@KNI CoS • direct agent ask • '
        'Slack action • automation", '
        'fillcolor="#f1f5f9", color="#64748b"];',
        (
            f'  interpret [label="{ORCHESTRATOR_AGENT_SPEC.agent_name} preflight\\n'
            "Interpret the goal, preserve "
            'wording, and treat an agent mention as routing advice", '
            'fillcolor="#fff7ed", color="#ea580c", penwidth=2.0];'
        ),
        (
            '  uncertainty [label="Resolve uncertainty intelligently\\nInfer safe defaults '
            '→ retrieve bounded context → clarify only material ambiguity", '
            'fillcolor="#fff7ed", color="#ea580c"];'
        ),
        (
            '  gates [label="Deterministic policy gates\\nApproval • record identity • live '
            'flags • exact write scope • source sufficiency", '
            'fillcolor="#fffbeb", color="#d97706"];'
        ),
        (
            '  backend [shape=diamond, label="Execution shape?\\nBounded single owner or '
            'stateful / multi-owner", fillcolor="#eef2ff", color="#4f46e5", '
            'width=2.9, height=1.0];'
        ),
        "",
        "  subgraph cluster_direct {",
        '    label="DIRECT / SINGLE-OWNER PATH"; color="#86efac"; '
        'fontcolor="#166534"; style="rounded,dashed";',
        (
            '    direct_owner [label="Owning SDK specialist\\nor Chief of Staff delegates '
            f'one bounded task ({CHIEF_OF_STAFF_AGENT_SPEC.agent_name})", '
            'fillcolor="#ecfdf5", color="#16a34a"];'
        ),
        (
            '    direct_tool [label="Typed provider helper\\nRead / query / scoped write / '
            'modify / marked test cleanup", fillcolor="#ecfdf5", color="#16a34a"];'
        ),
        "  }",
        "",
        "  subgraph cluster_stateful {",
        '    label="WORKITEM / LANGGRAPH PATH"; color="#a5b4fc"; '
        'fontcolor="#3730a3"; style="rounded,dashed";',
        (
            '    workitem [label="Canonical WorkItem\\nSQLite state + selected backend + '
            'typed context packs", fillcolor="#f5f3ff", color="#7c3aed"];'
        ),
        (
            '    context [label="Read-only context staging\\n'
            f'{_dot_quote(context_names)[1:-1]}", fillcolor="#f0f9ff", color="#0284c7"];'
        ),
        (
            '    specialists [label="Specialist / Chief manager loop\\n'
            f'{_dot_quote(workflow_names)[1:-1]}", fillcolor="#ecfdf5", color="#16a34a"];'
        ),
        (
            '    checkpoint [label="Approval checkpoint when required\\nPause, block, or '
            'resume the exact next action", fillcolor="#fffbeb", color="#d97706"];'
        ),
        "  }",
        "",
        (
            '  receipt [label="Verified provider or workflow receipt\\nRead-back and exact '
            'outcome override speculative completion prose", '
            'fillcolor="#eff6ff", color="#2563eb"];'
        ),
        (
            '  review [label="Orchestrator / deterministic output review\\nCheck request '
            'coverage, blockers, safety, and receipt truth", '
            'fillcolor="#fff7ed", color="#ea580c"];'
        ),
        (
            '  render [label="Operator-facing result\\nSlack / CLI / artifact renderer shows '
            'what happened and what remains", fillcolor="#eff6ff", color="#2563eb"];'
        ),
        "",
        '  entry -> interpret [label="1  raw request + bounded context"];',
        '  interpret -> uncertainty [label="2  semantic plan + assumptions"];',
        '  uncertainty -> gates [label="3  resolved context or targeted blocker"];',
        '  gates -> backend [label="4  safe execution envelope"];',
        '  backend -> direct_owner [label="5A  bounded single owner", color="#16a34a"];',
        '  direct_owner -> direct_tool [label="6A  exact operation", color="#16a34a"];',
        '  direct_tool -> receipt [label="7A  read-back", color="#16a34a"];',
        '  backend -> workitem [label="5B  stateful / multi-owner", color="#4f46e5"];',
        '  workitem -> context [label="6B  stage only relevant context", color="#0284c7"];',
        '  context -> specialists [label="7B  evidence handoff", color="#0284c7"];',
        '  specialists -> checkpoint [label="8B  if next action is gated", color="#d97706"];',
        '  specialists -> receipt [label="8B  completed safe step", color="#16a34a"];',
        '  checkpoint -> receipt [label="9B  approved, blocked, or paused", color="#d97706"];',
        '  receipt -> review [label="8A / 10B  structured result"];',
        '  review -> render [label="9A / 11B  one reviewed answer"];',
        "}",
    ]
    return "\n".join(lines) + "\n"


def _node_style(node_id: str) -> dict[str, str]:
    attrs = {"label": NODE_LABELS.get(node_id, node_id)}
    if node_id in {"__start__", "__end__"}:
        return {
            **attrs,
            "shape": "oval",
            "fillcolor": "#f1f5f9",
            "color": "#64748b",
        }
    if node_id == "orchestrator_preflight":
        return {**attrs, "fillcolor": "#fff7ed", "color": "#ea580c", "penwidth": "2.0"}
    if node_id.startswith("stage_"):
        return {**attrs, "fillcolor": "#f0f9ff", "color": "#0284c7"}
    if node_id.startswith("run_"):
        return {**attrs, "fillcolor": "#ecfdf5", "color": "#16a34a"}
    if node_id == "approval_checkpoint":
        return {**attrs, "fillcolor": "#fffbeb", "color": "#d97706", "penwidth": "2.0"}
    if node_id.startswith("manager_loop"):
        return {**attrs, "fillcolor": "#eef2ff", "color": "#4f46e5"}
    return {**attrs, "fillcolor": "#f5f3ff", "color": "#7c3aed"}


def _topology_edge_line(edge: RuntimeEdge) -> str:
    attrs: dict[str, str] = {}
    if edge.conditional:
        attrs.update(style="dashed", color="#94a3b8", penwidth="0.8")
    else:
        attrs.update(color="#475569", penwidth="1.25")
    if edge.source.startswith("stage_"):
        attrs["color"] = "#38bdf8"
    if edge.source.startswith("run_"):
        attrs["color"] = "#22c55e"
    if edge.source in {"finalize_step", "approval_checkpoint"}:
        attrs["color"] = "#d97706"
    if edge.label:
        attrs["label"] = edge.label
    if edge.source == edge.target or (
        edge.source == "manager_loop_continue" and edge.target == "prepare_work_item"
    ):
        attrs["constraint"] = "false"
    rendered = ", ".join(f"{key}={_dot_quote(value)}" for key, value in attrs.items())
    return f"  {_dot_quote(edge.source)} -> {_dot_quote(edge.target)} [{rendered}];"


def build_workitem_topology_dot(
    topology: RuntimeTopology,
    *,
    generated_date: str,
) -> str:
    """Return DOT for every node and edge in the compiled WorkItem graph."""

    node_set = set(topology.nodes)
    missing_edge_nodes = {
        endpoint
        for edge in topology.edges
        for endpoint in (edge.source, edge.target)
        if endpoint not in node_set
    }
    if missing_edge_nodes:
        raise ValueError(
            "Runtime edges reference nodes missing from the topology: "
            + ", ".join(sorted(missing_edge_nodes))
        )

    control_nodes = (
        "__start__",
        "normalize_request",
        "orchestrator_preflight",
        "state_followup",
        "prepare_work_item",
    )
    context_nodes = tuple(node for node in topology.nodes if node.startswith("stage_"))
    specialist_nodes = tuple(node for node in topology.nodes if node.startswith("run_"))
    lifecycle_nodes = (
        "finalize_step",
        "manager_loop_continue",
        "manager_loop_finalize",
        "approval_checkpoint",
        "__end__",
    )
    grouped = set((*control_nodes, *context_nodes, *specialist_nodes, *lifecycle_nodes))
    ungrouped = tuple(node for node in topology.nodes if node not in grouped)

    lines = [
        "digraph kba_workitem_langgraph_topology {",
        '  graph [rankdir=TB, bgcolor="#f8fafc", pad="0.35", nodesep="0.28", '
        'ranksep="0.58", splines=polyline, newrank=true, concentrate=true, '
        'fontname="Arial", fontsize=22, labelloc=t, '
        'label="Executable KBA WorkItem / LangGraph topology\\n'
        f'Generated {generated_date} from build_work_item_langgraph().get_graph() • '
        f'{len(topology.nodes)} nodes • {len(topology.edges)} edges"];',
        '  node [shape=box, style="rounded,filled", color="#94a3b8", '
        'fillcolor="#ffffff", fontname="Arial", fontsize=10, margin="0.12,0.08"];',
        '  edge [fontname="Arial", fontsize=8, arrowsize=0.55];',
        "",
        "  subgraph cluster_control {",
        '    label="1 — REQUEST CONTROL + CANONICAL STATE"; color="#fdba74"; '
        'fontcolor="#9a3412"; style="rounded,dashed";',
        *(_dot_node(node, attrs=_node_style(node)) for node in control_nodes if node in node_set),
        "  }",
        "",
        "  subgraph cluster_context {",
        '    label="2 — READ-ONLY CONTEXT STAGING"; color="#7dd3fc"; '
        'fontcolor="#075985"; style="rounded,dashed";',
        *(_dot_node(node, attrs=_node_style(node)) for node in context_nodes),
        "  }",
        "",
        "  subgraph cluster_specialists {",
        '    label="3 — SDK SPECIALISTS / CHIEF MANAGER"; color="#86efac"; '
        'fontcolor="#166534"; style="rounded,dashed";',
        *(_dot_node(node, attrs=_node_style(node)) for node in specialist_nodes),
        "  }",
        "",
        "  subgraph cluster_lifecycle {",
        '    label="4 — FINALIZE, LOOP, OR CHECKPOINT"; color="#c4b5fd"; '
        'fontcolor="#5b21b6"; style="rounded,dashed";',
        *(
            _dot_node(node, attrs=_node_style(node))
            for node in lifecycle_nodes
            if node in node_set
        ),
        "  }",
    ]
    if ungrouped:
        lines.extend(
            [
                "",
                "  subgraph cluster_unclassified {",
                '    label="UNCLASSIFIED RUNTIME NODES"; color="#fca5a5"; '
                'fontcolor="#991b1b"; style="rounded,dashed";',
                *(_dot_node(node, attrs=_node_style(node)) for node in ungrouped),
                "  }",
            ]
        )
    lines.extend(
        [
            "",
            '  { rank=same; "stage_feed_context"; "stage_zotero_context"; '
            '"stage_airtable_context"; "stage_google_workspace_context"; }',
            '  { rank=same; "run_business_research"; "run_opportunity_scout"; '
            '"run_gmail_triage"; "run_outreach_composer"; "run_chief_of_staff"; '
            '"run_unsupported_route"; }',
            "",
            *(_topology_edge_line(edge) for edge in topology.edges),
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def _dot_sha256(dot_source: str) -> str:
    return hashlib.sha256(dot_source.encode("utf-8")).hexdigest()


def _render_svg(dot_source: str, *, dot_binary: str) -> str:
    result = subprocess.run(
        [dot_binary, "-Tsvg"],
        input=dot_source,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Graphviz failed with exit code {result.returncode}: {result.stderr.strip()}"
        )
    source_hash = _dot_sha256(dot_source)
    svg = result.stdout
    marker = f"<!-- source-dot-sha256: {source_hash} -->"
    if "<svg " not in svg:
        raise RuntimeError("Graphviz returned output without an SVG root.")
    return svg.replace("<svg ", f"{marker}\n<svg ", 1)


def _write_diagram(
    output_dir: Path,
    *,
    stem: str,
    dot_source: str,
    dot_binary: str,
) -> tuple[Path, Path]:
    dot_path = output_dir / f"{stem}.dot"
    svg_path = output_dir / f"{stem}.svg"
    dot_path.write_text(dot_source, encoding="utf-8")
    svg_path.write_text(
        _render_svg(dot_source, dot_binary=dot_binary),
        encoding="utf-8",
    )
    return dot_path, svg_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory. Defaults to {DEFAULT_OUTPUT_DIR.relative_to(PROJECT_ROOT)}.",
    )
    parser.add_argument(
        "--generated-date",
        default=datetime.now(UTC).date().isoformat(),
        help="Date label to write into both diagrams.",
    )
    args = parser.parse_args(argv)

    dot_binary = shutil.which("dot")
    if not dot_binary:
        parser.error("Graphviz `dot` is required to render the architecture SVGs.")

    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    topology = _runtime_topology()
    outputs = (
        *_write_diagram(
            output_dir,
            stem=INTEGRATED_STEM,
            dot_source=build_integrated_architecture_dot(
                topology,
                generated_date=args.generated_date,
            ),
            dot_binary=dot_binary,
        ),
        *_write_diagram(
            output_dir,
            stem=SEQUENCE_STEM,
            dot_source=build_request_sequence_dot(generated_date=args.generated_date),
            dot_binary=dot_binary,
        ),
        *_write_diagram(
            output_dir,
            stem=TOPOLOGY_STEM,
            dot_source=build_workitem_topology_dot(
                topology,
                generated_date=args.generated_date,
            ),
            dot_binary=dot_binary,
        ),
    )
    for output_path in outputs:
        try:
            display_path = output_path.relative_to(PROJECT_ROOT)
        except ValueError:
            display_path = output_path
        print(f"wrote {display_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
