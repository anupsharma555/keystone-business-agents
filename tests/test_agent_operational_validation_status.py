from __future__ import annotations

from pathlib import Path

from keystone_agents.agent_registry import REGISTERED_AGENT_SPECS

STATUS_DOC = Path("docs/AGENT_OPERATIONAL_VALIDATION_STATUS.md")
LINEAR_BACKLOG = Path("LINEAR_BACKLOG.MD")


def test_operational_status_covers_every_registered_agent_family() -> None:
    text = STATUS_DOC.read_text()
    display_names = {
        "orchestrator": "Orchestrator",
        "chief_of_staff": "Chief of Staff",
        "gmail_triage": "Gmail Triage",
        "business_research_analyst": "Business Research Analyst",
        "opportunity_scout": "Opportunity Scout",
        "outreach_composer": "Outreach Composer",
        "airtable_context_agent": "Airtable Context",
        "google_workspace_context_agent": "Google Workspace Context",
        "zotero_context_agent": "Zotero Context",
        "rss_context_agent": "RSS Context",
        "preprints_context_agent": "Preprints Context",
    }

    route_names = {spec.route_name for spec in REGISTERED_AGENT_SPECS}
    assert route_names == set(display_names)
    for route_name in sorted(route_names):
        assert f"| {display_names[route_name]} |" in text


def test_operational_status_preserves_evidence_dimensions_and_eval_boundary() -> None:
    text = STATUS_DOC.read_text()

    for phrase in (
        "Interpretation / route",
        "Typed tool / provider",
        "Useful reasoning",
        "Lifecycle or read proof",
        "Safety / continuation",
        "Promptfoo suite",
        "zero model requests",
        "fixture-only",
    ):
        assert phrase in text


def test_airtable_backlog_matches_current_attachment_evidence_boundary(
    require_local_evidence,
) -> None:
    backlog = require_local_evidence(LINEAR_BACKLOG).read_text()

    assert "private-PNG lifecycle also passed" in backlog
    assert "Fake-model\n   SDK execution now distinguishes HTTPS URLs" in backlog
    assert "do not\n   claim a joined natural attachment pass" in backlog
    assert "Structural base/table/field/view creation is a\n   separate ANU-211 boundary" in backlog
    assert "local-file\n   local-file receipt upload" not in backlog


def test_workspace_backlog_matches_current_live_and_structural_evidence(
    require_local_evidence,
) -> None:
    backlog = require_local_evidence(LINEAR_BACKLOG).read_text()

    assert "natural selected-file read/synthesis, a natural marked-Sheet lifecycle" in backlog
    assert "separate folder/Doc lifecycles, exact identity/read-back/cleanup" in backlog
    assert "selected-file pass consumed all 1,096" in backlog
    assert "characters from the exact `README.doc`" in backlog
    assert "two provider reads succeeded" in backlog
    assert "no Workspace mutation" in backlog
    assert "join the layers in one approved live-model run" not in backlog
