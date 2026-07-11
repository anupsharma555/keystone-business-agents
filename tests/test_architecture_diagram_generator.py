from __future__ import annotations

import importlib.util
from pathlib import Path
from xml.etree import ElementTree as ET

from keystone_agents.agent_registry import (
    CHIEF_OF_STAFF_AGENT_SPEC,
    ORCHESTRATOR_AGENT_SPEC,
    SPECIALIST_AGENT_SPECS,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "render_agent_architecture_diagram.py"


def _load_generator_module():
    spec = importlib.util.spec_from_file_location("render_agent_architecture_diagram", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_architecture_diagram_generator_renders_current_registry_and_edge_legend() -> None:
    generator = _load_generator_module()
    svg = generator.render_svg(generated_date="2026-06-20")

    ET.fromstring(svg)

    assert ORCHESTRATOR_AGENT_SPEC.agent_name in svg
    assert CHIEF_OF_STAFF_AGENT_SPEC.agent_name in svg
    for spec in SPECIALIST_AGENT_SPECS:
        for label_part in generator._agent_label(spec.route_name).split():
            assert label_part in svg

    for expected in (
        "Routing / handoff",
        "Deterministic gate or state",
        "Agents as tools",
        "Traces / logs / evals",
        "Backend graph selector",
        "LangGraph WorkItem graph",
        "not a parallel router",
        "Stages review plans, approvals, and blockers",
        "Provider writes use owning specialists or approved handlers",
        "Direct actions (continue, more research, find contact, revise)",
        "gmail_triage -> business_research_analyst -> outreach_composer -> approval_checkpoint",
        "chief_of_staff -> airtable/google_workspace context -> approval_checkpoint",
        "rss/preprints/zotero context -> business research/opportunity specialist",
        "Negated constraints do not steer routes",
        "Approval checkpoints and no-send/no-write gates remain Python-authoritative",
        "WorkItems / SQLite canonical state",
        "keystone.sdk_run_summary.v1",
        "keystone.structured_log.v1",
    ):
        assert expected in svg

    assert "Owns Chief-level write plans" not in svg
    assert "Outreach Composer</text>" not in svg
    assert "Business Research</text>" not in svg
