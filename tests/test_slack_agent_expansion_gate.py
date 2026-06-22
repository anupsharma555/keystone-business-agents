from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def _load_gate_module() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_slack_agent_expansion_gate.py"
    spec = importlib.util.spec_from_file_location("run_slack_agent_expansion_gate", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_route_counts_are_sorted_from_provider_rows() -> None:
    gate = _load_gate_module()

    counts = gate._route_counts(
        [
            {"route": "chief_of_staff"},
            {"route": "business_research_analyst"},
            {"route": "chief_of_staff"},
            {"route": ""},
        ]
    )

    assert counts == {
        "business_research_analyst": 1,
        "chief_of_staff": 2,
        "unknown": 1,
    }
    assert (
        gate._format_route_counts(counts)
        == "business_research_analyst=1, chief_of_staff=2, unknown=1"
    )


def test_route_coverage_failures_report_missing_default_lanes() -> None:
    gate = _load_gate_module()

    failures = gate._route_coverage_failures(
        {"business_research_analyst": 5, "chief_of_staff": 10},
        {"business_research_analyst": 5, "chief_of_staff": 11, "rss_context_agent": 1},
    )

    assert failures == [
        "chief_of_staff=10, expected >= 11",
        "rss_context_agent=0, expected >= 1",
    ]
