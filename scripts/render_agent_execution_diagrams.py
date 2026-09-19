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
        "orchestrator_preflight\nAgent-owned route/workflow decision + safe defaults"
    ),
    "state_followup": "state_followup\nAnswer state-only follow-up or continue",
    "prepare_work_item": "prepare_work_item\nCanonical WorkItem + typed context pack",
    **{
        node_name: f"{node_name}\n{description}"
        for node_name, description in CONTEXT_STAGE_LABELS.items()
    },
    "run_rag_retrieval": "run_rag_retrieval\nRAG Retrieval Specialist",
    "run_business_research": "run_business_research\nBusiness Research Analyst",
    "run_opportunity_scout": "run_opportunity_scout\nOpportunity Scout",
    "run_gmail_triage": "run_gmail_triage\nGmail Triage",
    "run_outreach_composer": "run_outreach_composer\nOutreach Composer",
    "run_chief_of_staff": "run_chief_of_staff\nChief of Staff manager",
    "run_unsupported_route": "run_unsupported_route\nUseful blocker + next safe action",
    "finalize_step": "finalize_step\nValidate decision/tool evidence + update canonical state",
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


def build_integrated_architecture_dot(
    topology: RuntimeTopology,
    *,
    generated_date: str,
) -> str:
    """Return a compact conceptual request-to-answer overview."""

    runtime_nodes = set(topology.nodes)
    required_runtime_nodes = {
        "orchestrator_preflight",
        "prepare_work_item",
        "finalize_step",
        "manager_loop_continue",
        "manager_loop_finalize",
        "approval_checkpoint",
        "__end__",
    }
    missing_nodes = required_runtime_nodes - runtime_nodes
    if missing_nodes or not any(node.startswith("stage_") for node in runtime_nodes):
        missing = sorted(missing_nodes) or ["stage_* context nodes"]
        raise ValueError(
            "Integrated overview cannot summarize the current runtime: "
            + ", ".join(missing)
        )

    workflow_catalog = "\n".join(_registered_workflow_agent_names())
    context_catalog = "\n".join(_registered_context_agent_names())
    lines = [
        "digraph kba_integrated_agent_architecture {",
        '  graph [rankdir=TB, bgcolor="#f8fafc", pad="0.30", nodesep="0.42", '
        'ranksep="0.62", splines=ortho, compound=true, newrank=true, '
        'fontname="Arial", fontsize=22, labelloc=t, '
        'label="Keystone Business Agents — clear conceptual overview\\n'
        f'Generated {generated_date} • request → interpretation → selected path → '
        'verified evidence → answer"];',
        '  node [shape=box, style="rounded,filled", color="#94a3b8", '
        'fillcolor="#ffffff", fontname="Arial", fontsize=13, margin="0.20,0.14", '
        'penwidth=1.6];',
        '  edge [color="#475569", fontname="Arial", penwidth=1.8, arrowsize=0.78];',
        "",
        "  subgraph cluster_control {",
        '    label="UNDERSTAND AND AUTHORIZE"; fontsize=15; color="#fdba74"; '
        'fontcolor="#9a3412"; style="rounded,dashed"; margin=22;',
        '    request [label="1  REQUEST\\nSlack • CLI • schedule\\nPreserve raw ask + '
        'bounded context", fillcolor="#f1f5f9", color="#64748b", width=2.6];',
        (
            f'    orchestrator [label="2  {ORCHESTRATOR_AGENT_SPEC.agent_name.upper()}\\n'
            'Interpret meaning • uncertainty\\nowner • constraints • safe defaults\\n'
            'Agent mention is advice—not authority", fillcolor="#fff7ed", '
            'color="#ea580c", penwidth=2.4, width=3.2];'
        ),
        '    gates [label="3  SAFE EXECUTION ENVELOPE\\napproval • identity • source '
        'sufficiency\\nprovider/live scope • exact writes\\nmodel budget", '
        'fillcolor="#fffbeb", color="#d97706", width=3.0];',
        '    choose [shape=diamond, label="4  CHOOSE ONE PATH\\nBounded single owner\\n'
        'or resumable / approval-dependent\\nambiguous / multi-owner", '
        'fillcolor="#eef2ff", color="#4f46e5", width=3.0, height=1.3];',
        "    { rank=same; request; orchestrator; gates; choose; }",
        "  }",
        "",
        "  subgraph cluster_paths {",
        '    label="EXECUTION PATHS — CONTEXT OPTIONAL"; fontsize=15; '
        'labeljust=r; color="#a5b4fc"; fontcolor="#3730a3"; '
        'style="rounded,dashed"; margin=22;',
        (
            f'    direct [label="5A  DIRECT / SINGLE OWNER\\nOwning specialist, or '
            f'{CHIEF_OF_STAFF_AGENT_SPEC.agent_name}\\ndelegates one bounded task\\n'
            'No graph implied", fillcolor="#ecfdf5", color="#16a34a", width=3.5];'
        ),
        (
            '    workitem [label="5B  WORKITEM / OPTIONAL LANGGRAPH\\nSQLite business '
            'state + typed context pack\\nChief / manager selects the next owner\\n'
            'manager loop • approval pause • resume\\ngraph checkpoint proof is separate", '
            'fillcolor="#f5f3ff", color="#7c3aed", penwidth=2.2, width=3.7];'
        ),
        (
            '    context [label="OPTIONAL CONTEXT — ONLY WHEN NEEDED\\n'
            f'{_dot_quote(context_catalog)[1:-1]}\\n'
            'Bounded read evidence • model-visible\\nNo inherited write authority", '
            'fillcolor="#f0f9ff", color="#0284c7", width=3.7];'
        ),
        "    { rank=same; direct; workitem; context; }",
        "  }",
        "",
        "  subgraph cluster_result {",
        '    label="REASON → VERIFY → ANSWER"; fontsize=15; labeljust=l; '
        'color="#86efac"; fontcolor="#166534"; style="rounded,dashed"; '
        'margin=22;',
        (
            '    specialist [label="6  SELECTED SPECIALIST REASONS\\n'
            f'{_dot_quote(workflow_catalog)[1:-1]}\\n'
            'Raw ask + Orchestrator memo + context", fillcolor="#ecfdf5", '
            'color="#16a34a", width=3.6];'
        ),
        '    evidence [label="7  EVIDENCE + TOOL BOUNDARY\\nAttached tools ≠ actual tool '
        'calls\\nSDK tool call + output • workflow helper/read\\npre-acquired verified '
        'context\\npermission • exact scope • read-back • receipt", '
        'fillcolor="#eff6ff", color="#2563eb", width=3.8];',
        '    review [label="8  RECONCILE\\nOrchestrator + deterministic review\\n'
        'request coverage • receipt truth\\nsafe repair or useful blocker", '
        'fillcolor="#fff7ed", color="#ea580c", width=3.2];',
        '    answer [label="9  ONE ANSWER\\nSlack • CLI • artifact\\nResult first; '
        'proof boundaries visible", fillcolor="#eff6ff", color="#2563eb", '
        'penwidth=2.2, width=2.8];',
        "  }",
        "",
        '  request -> orchestrator;',
        '  orchestrator -> gates;',
        '  gates -> choose;',
        '  choose -> direct [color="#16a34a"];',
        '  choose -> workitem [color="#4f46e5"];',
        '  orchestrator -> context [color="#0284c7", style=dashed, constraint=false];',
        '  direct -> specialist [color="#16a34a"];',
        '  workitem -> specialist [color="#4f46e5"];',
        '  workitem -> workitem [color="#7c3aed", constraint=false];',
        '  context -> specialist [color="#0284c7", style=dashed];',
        '  specialist -> evidence [color="#16a34a"];',
        '  evidence -> review [color="#2563eb"];',
        '  review -> answer;',
        "}",
    ]
    return "\n".join(lines) + "\n"


def build_request_sequence_dot(*, generated_date: str) -> str:
    """Return a compact left-to-right direct-versus-stateful request flow."""

    context_names = " • ".join(_registered_context_agent_names())
    workflow_names = " • ".join(_registered_workflow_agent_names())
    lines = [
        "digraph kba_request_execution_sequence {",
        '  graph [rankdir=LR, bgcolor="#f8fafc", pad="0.30", nodesep="0.30", '
        'ranksep="0.52", splines=ortho, newrank=true, fontname="Arial", fontsize=20, '
        'labelloc=t, label="Keystone @KNI request execution sequence\\n'
        f"Generated {generated_date} • read left to right • semantic ownership and "
        'execution shape are separate"];',
        '  node [shape=box, style="rounded,filled", color="#94a3b8", '
        'fillcolor="#ffffff", fontname="Arial", fontsize=10, margin="0.15,0.10", '
        'width=2.20];',
        '  edge [color="#475569", fontname="Arial", penwidth=1.45, arrowsize=0.70];',
        "",
        '  entry [label="1  Preserve the operator request\\nSlack • CLI • schedule\\n'
        'raw ask + bounded context", fillcolor="#f1f5f9", color="#64748b"];',
        (
            f'  interpret [label="2  {ORCHESTRATOR_AGENT_SPEC.agent_name}\\n'
            'interpret meaning • uncertainty\\nowner • constraints • safe defaults\\n'
            'agent mention = routing advice", '
            'fillcolor="#fff7ed", color="#ea580c", penwidth=2.0];'
        ),
        (
            '  gates [label="3  Python validates the envelope\\nsafety • exact identity '
            '• approval\\nprovider scope • source sufficiency\\nN+1 model call blocked before '
            'dispatch", '
            'fillcolor="#fffbeb", color="#d97706"];'
        ),
        (
            '  backend [shape=diamond, label="4  Choose execution shape\\nbounded '
            'single owner\\nor resumable / approval-dependent\\nambiguous / multi-owner", '
            'fillcolor="#eef2ff", color="#4f46e5", width=2.55, height=1.18];'
        ),
        "",
        "  subgraph cluster_direct {",
        '    label="A — DIRECT / SINGLE OWNER"; color="#86efac"; '
        'fontcolor="#166534"; style="rounded,dashed";',
        (
            '    direct_owner [label="5A  Optional bounded context\\n6A  Owning specialist '
            'reasons\\n'
            f'{CHIEF_OF_STAFF_AGENT_SPEC.agent_name} may delegate\\none selected owner", '
            'fillcolor="#ecfdf5", color="#16a34a", width=2.55];'
        ),
        "  }",
        "",
        "  subgraph cluster_stateful {",
        '    label="B — WORKITEM / OPTIONAL LANGGRAPH"; color="#a5b4fc"; '
        'fontcolor="#3730a3"; style="rounded,dashed";',
        (
            '    graph_owner [label="5B  Canonical WorkItem + context pack\\n'
            'SQLite business state ≠ graph checkpoint proof\\n'
            '6B  Chief / manager + owning specialist\\n'
            'loop or approval checkpoint when required", '
            'fillcolor="#f5f3ff", color="#7c3aed", width=3.05];'
        ),
        "  }",
        "",
        (
            '  context_catalog [shape=note, label="5  Context specialists stage typed '
            'evidence only when needed in A or B\\n'
            f'{_dot_quote(context_names)[1:-1]}\\n'
            'model-visible evidence • no inherited write authority", '
            'fillcolor="#f0f9ff", color="#0284c7", width=3.2];'
        ),
        (
            '  specialist_catalog [shape=note, label="Registered workflow owners used by A or B\\n'
            f'{_dot_quote(workflow_names)[1:-1]}", fillcolor="#ecfdf5", '
            'color="#16a34a", width=3.0];'
        ),
        (
            '  provider [label="7  Provider / tool result\\nexact admitted permission + '
            'read-back\\n'
            'model call • SDK tool call • helper\\npre-acquired context stay distinct", '
            'fillcolor="#eff6ff", color="#2563eb", width=2.75];'
        ),
        (
            '  review [label="8  Reconcile decision + receipt\\nOrchestrator / deterministic '
            'review\\nperformed • not performed • unknown", '
            'fillcolor="#fff7ed", color="#ea580c", width=2.50];'
        ),
        (
            '  render [label="9  Render one answer\\nSlack • CLI • artifact\\n'
            'result first; metadata secondary", fillcolor="#eff6ff", '
            'color="#2563eb", width=2.25];'
        ),
        "",
        '  entry -> interpret;',
        '  interpret -> gates;',
        '  gates -> backend;',
        '  backend -> direct_owner [color="#16a34a"];',
        '  backend -> graph_owner [color="#4f46e5"];',
        '  direct_owner -> provider [color="#16a34a"];',
        '  graph_owner -> provider [color="#4f46e5"];',
        '  provider -> review;',
        '  review -> render;',
        '  { rank=same; direct_owner; graph_owner; context_catalog; specialist_catalog; }',
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
    display_label = edge.label or {
        ("finalize_step", "manager_loop_continue"): "continue",
        ("manager_loop_continue", "prepare_work_item"): "next stage loop",
        ("finalize_step", "manager_loop_finalize"): "finish",
        ("manager_loop_finalize", "__end__"): "terminal",
        ("finalize_step", "approval_checkpoint"): "approval required",
        ("approval_checkpoint", "__end__"): "pause / resume boundary",
    }.get((edge.source, edge.target), "")
    if display_label:
        attrs["label"] = display_label
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
        'label="Compiled executable KBA WorkItem / LangGraph topology\\n'
        f'Generated {generated_date} from build_work_item_langgraph().get_graph() • '
        f'{len(topology.nodes)} nodes • {len(topology.edges)} edges • '
        'dashed = compiled conditional choice • context stages are conditional\\n'
        'node presence does not imply named-route dispatch; display labels annotate '
        'unchanged runtime edges"];',
        '  node [shape=box, style="rounded,filled", color="#94a3b8", '
        'fillcolor="#ffffff", fontname="Arial", fontsize=10, margin="0.12,0.08"];',
        '  edge [fontname="Arial", fontsize=8, arrowsize=0.55];',
        "",
        "  subgraph cluster_control {",
        '    label="1 — CONTROL + WORKITEM STATE"; color="#fdba74"; '
        'fontcolor="#9a3412"; fontsize=13; labeljust=l; margin=24; '
        'style="rounded,dashed";',
        *(_dot_node(node, attrs=_node_style(node)) for node in control_nodes if node in node_set),
        "  }",
        "",
        "  subgraph cluster_context {",
        '    label="2 — CONTEXT STAGING"; color="#7dd3fc"; '
        'fontcolor="#075985"; fontsize=13; labeljust=r; margin=24; '
        'style="rounded,dashed";',
        *(_dot_node(node, attrs=_node_style(node)) for node in context_nodes),
        "  }",
        "",
        "  subgraph cluster_specialists {",
        '    label="3 — SPECIALISTS / CHIEF"; color="#86efac"; '
        'fontcolor="#166534"; fontsize=13; labeljust=l; margin=24; '
        'style="rounded,dashed";',
        *(_dot_node(node, attrs=_node_style(node)) for node in specialist_nodes),
        "  }",
        "",
        "  subgraph cluster_lifecycle {",
        '    label="4 — FINALIZE / LOOP / APPROVAL"; color="#c4b5fd"; '
        'fontcolor="#5b21b6"; fontsize=13; labeljust=l; margin=24; '
        'style="rounded,dashed";',
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
                'fontcolor="#991b1b"; fontsize=14; labeljust=l; '
                'style="rounded,dashed";',
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


def _render_png(dot_source: str, *, dot_binary: str) -> bytes:
    result = subprocess.run(
        [dot_binary, "-Tpng"],
        input=dot_source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Graphviz PNG rendering failed with exit code "
            f"{result.returncode}: {result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Graphviz returned output without a PNG signature.")
    return result.stdout


def _write_diagram(
    output_dir: Path,
    *,
    stem: str,
    dot_source: str,
    dot_binary: str,
    include_png: bool = False,
) -> tuple[Path, ...]:
    dot_path = output_dir / f"{stem}.dot"
    svg_path = output_dir / f"{stem}.svg"
    dot_path.write_text(dot_source, encoding="utf-8")
    svg_path.write_text(
        _render_svg(dot_source, dot_binary=dot_binary),
        encoding="utf-8",
    )
    outputs: list[Path] = [dot_path, svg_path]
    if include_png:
        png_path = output_dir / f"{stem}.png"
        png_path.write_bytes(_render_png(dot_source, dot_binary=dot_binary))
        outputs.append(png_path)
    return tuple(outputs)


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
            include_png=True,
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
