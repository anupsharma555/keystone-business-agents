from __future__ import annotations

from datetime import UTC, datetime

import pytest

from keystone_agents.execution_identity import create_validation_execution_identity


def test_validation_execution_identity_is_traceable_and_content_safe() -> None:
    identity = create_validation_execution_identity(
        scenario="workspace_selected_doc_search_read_summarize",
        route="google_workspace_context_agent",
        now=datetime(2026, 7, 11, 12, 30, tzinfo=UTC),
        nonce="a1b2c3d4",
    )

    assert identity.run_id == (
        "kba_workspace_selected_doc_searc_20260711T123000Z_a1b2c3d4"
    )
    assert identity.case_id == "validation_workspace_selected_doc_search_read_summarize"
    assert identity.created_at_utc == "2026-07-11T12:30:00Z"
    assert identity.trace_metadata() == {
        "run_id": identity.run_id,
        "case_id": identity.case_id,
        "route": "google_workspace_context_agent",
        "workflow_kind": "workspace_selected_doc_search_read_summarize",
    }
    assert identity.receipt()["run_id"] == identity.run_id


def test_validation_execution_identity_rejects_unbounded_labels_and_nonce() -> None:
    with pytest.raises(ValueError, match="scenario"):
        create_validation_execution_identity(scenario="!!!", route="gmail_triage")
    with pytest.raises(ValueError, match="nonce"):
        create_validation_execution_identity(
            scenario="gmail_revision",
            route="gmail_triage",
            nonce="not-safe",
        )
