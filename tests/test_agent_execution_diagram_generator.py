from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from keystone_agents.agent_registry import (
    CHIEF_OF_STAFF_AGENT_SPEC,
    ORCHESTRATOR_AGENT_SPEC,
    SPECIALIST_AGENT_SPECS,
)
from keystone_agents.langgraph_workflow import langgraph_available

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "render_agent_execution_diagrams.py"
ASSET_DIR = PROJECT_ROOT / "docs" / "assets"


def _load_generator_module():
    spec = importlib.util.spec_from_file_location(
        "render_agent_execution_diagrams",
        SCRIPT_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_request_sequence_separates_semantic_interpretation_from_execution_shape() -> None:
    generator = _load_generator_module()
    dot = generator.build_request_sequence_dot(generated_date="2026-07-18")

    for expected in (
        ORCHESTRATOR_AGENT_SPEC.agent_name,
        CHIEF_OF_STAFF_AGENT_SPEC.agent_name,
        "rankdir=LR",
        "splines=ortho",
        "1  Preserve the operator request",
        "2  Orchestrator Agent",
        "3  Python validates the envelope",
        "4  Choose execution shape",
        "5  Context specialists stage typed evidence only when needed in A or B",
        "6A  Owning specialist reasons",
        "6B  Chief / manager + owning specialist",
        "7  Provider / tool result",
        "8  Reconcile decision + receipt",
        "9  Render one answer",
        "SQLite business state ≠ graph checkpoint proof",
    ):
        assert expected in dot
    for spec in SPECIALIST_AGENT_SPECS:
        assert spec.agent_name in dot

    numbered_labels = re.findall(r'label="(\d+(?:[AB])?[^"]*)"', dot)
    assert len(numbered_labels) >= 9
    assert dot.index("entry -> interpret") < dot.index("interpret -> gates")
    assert dot.index("interpret -> gates") < dot.index("gates -> backend")
    assert 'label="' not in dot[dot.index("  entry -> interpret;") :]


@pytest.mark.skipif(not langgraph_available(), reason="optional LangGraph is not installed")
def test_integrated_architecture_connects_sequence_context_direct_and_graph_views() -> None:
    generator = _load_generator_module()
    topology = generator._runtime_topology()
    dot = generator.build_integrated_architecture_dot(
        topology,
        generated_date="2026-07-18",
    )

    for expected in (
        "clear conceptual overview",
        "request → interpretation → selected path → verified evidence → answer",
        "UNDERSTAND AND AUTHORIZE",
        "EXECUTION PATHS — CONTEXT OPTIONAL",
        "REASON → VERIFY → ANSWER",
        "DIRECT / SINGLE OWNER",
        "WORKITEM / OPTIONAL LANGGRAPH",
        "OPTIONAL CONTEXT — ONLY WHEN NEEDED",
        "No graph implied",
        "manager loop • approval pause • resume",
        "graph checkpoint proof is separate",
        "SELECTED SPECIALIST REASONS",
        "Attached tools ≠ actual tool calls",
        "SDK tool call + output • workflow helper/read",
        "pre-acquired verified context",
        "permission • exact scope • read-back • receipt",
    ):
        assert expected in dot
    for spec in SPECIALIST_AGENT_SPECS:
        assert spec.agent_name in dot

    for literal_runtime_detail in (
        "normalize_request",
        "stage_airtable_context",
        "manager_loop_finalize",
        "run_unsupported_route",
    ):
        assert literal_runtime_detail not in dot

    for earlier, later in (
        ("request -> orchestrator", "orchestrator -> gates"),
        ("orchestrator -> gates", "gates -> choose"),
        ("choose -> direct", "direct -> specialist"),
        ("choose -> workitem", "workitem -> specialist"),
        ("specialist -> evidence", "evidence -> review"),
        ("evidence -> review", "review -> answer"),
    ):
        assert dot.index(earlier) < dot.index(later)
    assert "workitem -> workitem" in dot


@pytest.mark.skipif(not langgraph_available(), reason="optional LangGraph is not installed")
def test_topology_contains_every_compiled_runtime_node_and_edge() -> None:
    generator = _load_generator_module()
    topology = generator._runtime_topology()
    dot = generator.build_workitem_topology_dot(
        topology,
        generated_date="2026-07-18",
    )

    assert len(topology.nodes) == 21
    assert "run_rag_retrieval" in topology.nodes
    assert len(topology.edges) == 78
    for node in topology.nodes:
        assert f'"{node}" [' in dot
    for edge in topology.edges:
        assert generator._topology_edge_line(edge) in dot

    for expected in (
        "RSS + Preprints context agents",
        "Zotero context agent",
        "Airtable context agent",
        "Google Workspace context agent",
        "Business Research Analyst",
        "RAG Retrieval Specialist",
        "Opportunity Scout",
        "Gmail Triage",
        "Outreach Composer",
        "Chief of Staff manager",
        "approval_checkpoint",
        "manager_loop_continue",
        "dashed = compiled conditional choice",
        "context stages are conditional",
        "next stage loop",
        "approval required",
        "pause / resume boundary",
        "2 — CONTEXT STAGING",
        "4 — FINALIZE / LOOP / APPROVAL",
    ):
        assert expected in dot


@pytest.mark.skipif(
    not langgraph_available() or shutil.which("dot") is None,
    reason="diagram generation requires optional LangGraph and Graphviz",
)
def test_committed_execution_diagrams_match_sources_and_have_fresh_svg_hashes() -> None:
    generator = _load_generator_module()
    integrated_dot_path = ASSET_DIR / f"{generator.INTEGRATED_STEM}.dot"
    sequence_dot_path = ASSET_DIR / f"{generator.SEQUENCE_STEM}.dot"
    topology_dot_path = ASSET_DIR / f"{generator.TOPOLOGY_STEM}.dot"
    integrated_svg_path = ASSET_DIR / f"{generator.INTEGRATED_STEM}.svg"
    sequence_svg_path = ASSET_DIR / f"{generator.SEQUENCE_STEM}.svg"
    topology_svg_path = ASSET_DIR / f"{generator.TOPOLOGY_STEM}.svg"

    integrated_dot = integrated_dot_path.read_text(encoding="utf-8")
    sequence_dot = sequence_dot_path.read_text(encoding="utf-8")
    topology_dot = topology_dot_path.read_text(encoding="utf-8")
    generated_date_match = re.search(r"Generated (\d{4}-\d{2}-\d{2})", integrated_dot)
    assert generated_date_match is not None
    generated_date = generated_date_match.group(1)
    topology = generator._runtime_topology()
    assert integrated_dot == generator.build_integrated_architecture_dot(
        topology,
        generated_date=generated_date,
    )
    assert sequence_dot == generator.build_request_sequence_dot(
        generated_date=generated_date
    )
    assert topology_dot == generator.build_workitem_topology_dot(
        topology,
        generated_date=generated_date,
    )

    for dot_source, svg_path in (
        (integrated_dot, integrated_svg_path),
        (sequence_dot, sequence_svg_path),
        (topology_dot, topology_svg_path),
    ):
        svg = svg_path.read_text(encoding="utf-8")
        ET.fromstring(svg)
        assert (
            f"source-dot-sha256: {generator._dot_sha256(dot_source)}"
            in svg
        )

    visual_context = (PROJECT_ROOT / "docs" / "VISUAL_CONTEXT.md").read_text(
        encoding="utf-8"
    )
    for stem in (
        generator.INTEGRATED_STEM,
        generator.SEQUENCE_STEM,
        generator.TOPOLOGY_STEM,
    ):
        assert f"assets/{stem}.svg" in visual_context
        assert f"assets/{stem}.dot" in visual_context
    assert "scripts/render_agent_execution_diagrams.py" in visual_context
