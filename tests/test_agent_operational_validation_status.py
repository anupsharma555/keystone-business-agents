from __future__ import annotations

from pathlib import Path

from keystone_agents.agent_registry import REGISTERED_AGENT_SPECS

STATUS_DOC = Path("docs/AGENT_OPERATIONAL_VALIDATION_STATUS.md")
LINEAR_BACKLOG = Path("LINEAR_BACKLOG.MD")


def _current_matrix_cells(path: Path, agent_family: str) -> list[str]:
    """Return one agent row from the canonical current operational matrix."""

    prefix = f"| {agent_family} |"
    matches = [line for line in path.read_text().splitlines() if line.startswith(prefix)]

    assert len(matches) == 1
    cells = [cell.strip() for cell in matches[0].strip("|").split("|")]
    assert len(cells) == 7
    assert cells[0] == agent_family
    return cells


def test_operational_status_covers_every_registered_agent_family() -> None:
    text = STATUS_DOC.read_text()
    display_names = {
        "orchestrator": "Orchestrator",
        "chief_of_staff": "Chief of Staff",
        "gmail_triage": "Gmail Triage",
        "business_research_analyst": "Business Research Analyst",
        "rag_retrieval_specialist": "RAG Retrieval Specialist",
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


def test_canonical_airtable_status_preserves_live_and_blocked_boundaries(
    require_local_evidence,
) -> None:
    cells = _current_matrix_cells(
        require_local_evidence(STATUS_DOC), "Airtable Context"
    )
    interpretation, tool, _, lifecycle, safety, boundary = cells[1:]

    assert "Proven live" in interpretation
    assert "both attachment modes" in interpretation
    assert "provider-backed synthesis" in interpretation
    assert "separate no-tool synthesis mode" in tool
    assert "HTTPS link attachment" in lifecycle
    assert "private local upload" in lifecycle
    assert "no raw record, send, or post persisted" in safety
    assert "ANU-211" in boundary
    assert "resource-scope blocked" in boundary


def test_canonical_workspace_status_preserves_live_and_fixture_boundaries(
    require_local_evidence,
) -> None:
    cells = _current_matrix_cells(
        require_local_evidence(STATUS_DOC), "Google Workspace Context"
    )
    interpretation, tool, reasoning, lifecycle, safety, boundary = cells[1:]

    assert "live selected-document synthesis" in interpretation
    assert "joined research-to-Doc execution" in interpretation
    assert "Proven live" in tool
    assert "exact `README.doc`" in tool
    assert "fake-model execution" in tool
    assert "Proven live for selected-file reasoning" in reasoning
    assert "research-to-Doc lifecycles pass" in lifecycle
    assert "no search/send/share occurred" in safety
    assert "inline-image insertion still requires a publicly accessible URI" in safety
    assert "True Google Doc image embed remains blocked" in boundary


def test_linear_backlog_links_to_canonical_operational_status(
    require_local_evidence,
) -> None:
    backlog = require_local_evidence(LINEAR_BACKLOG).read_text()
    require_local_evidence(STATUS_DOC)

    assert (
        "[Agent Operational Validation Status]"
        "(docs/AGENT_OPERATIONAL_VALIDATION_STATUS.md)" in backlog
    )
