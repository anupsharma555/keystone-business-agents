from __future__ import annotations

import re
from pathlib import Path

from keystone_agents.manual_request import infer_manual_request_plan

SMOKE_DOC = Path("docs/BASIC_AGENT_EXECUTION_SMOKE_TASKS.md")
EXPECTED_IDS = [f"SMK-{index:02d}" for index in range(1, 21)]


def _smoke_rows() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in SMOKE_DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| SMK-") or "`@KNI" not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        prompt_match = re.search(r"`([^`]+)`", cells[1])
        assert prompt_match is not None
        rows[cells[0]] = {
            "prompt": prompt_match.group(1),
            "primary_route": cells[2],
            "first_validation": cells[3],
            "later_live_validation": cells[4],
        }
    return rows


def _coverage_rows() -> set[str]:
    ids: set[str] = set()
    in_coverage_map = False
    for line in SMOKE_DOC.read_text(encoding="utf-8").splitlines():
        if line == "## Coverage Map":
            in_coverage_map = True
            continue
        if in_coverage_map and line.startswith("## "):
            break
        if in_coverage_map and line.startswith("| SMK-"):
            ids.add(line.split("|")[1].strip())
    return ids


def test_basic_smoke_doc_has_twenty_natural_prompts_and_coverage_rows() -> None:
    rows = _smoke_rows()

    assert list(rows) == EXPECTED_IDS
    assert _coverage_rows() == set(EXPECTED_IDS)

    for smoke_id, row in rows.items():
        prompt = row["prompt"]
        assert prompt.startswith("@KNI ")
        assert smoke_id not in prompt
        assert "offline route/safety" not in prompt
        assert "offline fixture/output" not in prompt
        assert "later live" not in prompt
        assert "--live" not in prompt
        assert "--no-live-sdk" not in prompt
        assert "validation lane" not in prompt.lower()
        assert row["primary_route"]
        assert row["first_validation"]
        assert row["later_live_validation"]

    assert "Measurement-based care AI evaluation" in rows["SMK-16"]["prompt"]
    assert "KNI Collections - Behavioral Health AI Validation" in rows["SMK-16"]["prompt"]


def test_basic_smoke_doc_preserves_no_write_boundaries() -> None:
    text = SMOKE_DOC.read_text(encoding="utf-8")

    required_boundaries = [
        "Do not send email, create Gmail drafts, create calendar events, post to Slack,",
        "write Airtable/CRM rows, or create/update Drive files",
        "Live validation is read-only unless a separate scoped approval explicitly",
        "actual mutation requires a separate approval outside this checklist",
    ]
    for boundary in required_boundaries:
        assert boundary in text

    rows = _smoke_rows()
    for smoke_id in {"SMK-01", "SMK-07", "SMK-10", "SMK-11", "SMK-12", "SMK-14", "SMK-19"}:
        prompt = rows[smoke_id]["prompt"].lower()
        assert any(term in prompt for term in ["do not", "without", "please do not"])


def test_basic_smoke_prompts_route_with_existing_planner_without_new_shortcuts() -> None:
    expected_targets = {
        "SMK-01": "outreach_composer",
        "SMK-02": "chief_of_staff",
        "SMK-03": "business_research_analyst",
        "SMK-04": "business_research_analyst",
        "SMK-05": "opportunity_scout",
        "SMK-06": "clarification",
        "SMK-07": "outreach_composer",
        "SMK-08": "clarification",
        "SMK-09": "gmail_triage",
        "SMK-10": "gmail_triage",
        "SMK-11": "gmail_triage",
        "SMK-12": "airtable_context_agent",
        "SMK-13": "airtable_context_agent",
        "SMK-14": "google_workspace_context_agent",
        "SMK-15": "google_workspace_context_agent",
        "SMK-16": "zotero_context_agent",
        "SMK-17": "zotero_context_agent",
        "SMK-18": "preprints_context_agent",
        "SMK-19": "rss_context_agent",
        "SMK-20": "chief_of_staff",
    }

    rows = _smoke_rows()
    for smoke_id, expected_target in expected_targets.items():
        plan = infer_manual_request_plan(
            rows[smoke_id]["prompt"],
            requested_agent="orchestrator",
        )
        assert plan.target_agent == expected_target
        assert plan.intent != "blocked_send"
