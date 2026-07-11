from __future__ import annotations

from pathlib import Path

DOC = Path("docs/AI_AGENTS_WORKFLOW_NO_LIVE_VALIDATION.md")


def test_current_validation_doc_records_missing_anu174_smokes() -> None:
    text = DOC.read_text(encoding="utf-8")

    smoke_ids = (
        "SMK-01",
        "SMK-02",
        "SMK-04",
        "SMK-08",
        "SMK-11",
        "SMK-15",
        "SMK-17",
        "SMK-20",
    )
    for smoke_id in smoke_ids:
        assert f"| {smoke_id} |" in text
    assert "No API budget is needed for this phase." in text


def test_current_acceptance_scorecard_covers_every_operating_route() -> None:
    text = DOC.read_text(encoding="utf-8")
    routes = {
        "Orchestrator",
        "Chief of Staff",
        "Gmail Triage",
        "Business Research",
        "Opportunity Scout",
        "Outreach Composer",
        "Airtable Context",
        "Google Workspace Context",
        "Zotero Context",
        "RSS Context",
        "Preprints Context",
    }

    for route in routes:
        assert f"| {route} |" in text
    assert "Failure category proved" in text
    assert "Tool tier" in text
    assert "Side-effect boundary" in text
    assert "Next live proof" in text


def test_legacy_promptfoo_is_explicitly_deferred() -> None:
    text = DOC.read_text(encoding="utf-8")

    assert "legacy 102-case Promptfoo suite" in text
    assert "intentionally excluded from this gate" in text
    assert "future migration and reactivation" in text
