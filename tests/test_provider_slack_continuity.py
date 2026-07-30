from __future__ import annotations

from pathlib import Path

import pytest

from keystone_agents import cli, slack_actions
from keystone_agents.execution_request import (
    attach_execution_public_result,
    build_execution_request,
    execution_request_planning_text,
)
from keystone_agents.storage import SQLiteStore


@pytest.mark.parametrize(
    (
        "alias",
        "expected_agent",
        "receipt",
        "expected_provider",
        "expected_object_id",
    ),
    [
        (
            "GWC",
            "google_workspace_context_agent",
            {
                "operation": "write_doc",
                "document_id": "doc_123",
                "title": "Operating Model",
                "folder_path": "KNIOps",
                "verification": {"passed": True},
            },
            "google_drive",
            "doc_123",
        ),
        (
            "GT",
            "gmail_triage",
            {
                "operation": "update_draft",
                "draft_id": "draft_123",
                "message_id": "message_123",
                "gmail_account": "operator@example.com",
                "subject": "KBA_TEST_DRAFT continuity",
                "verification": {"passed": True},
            },
            "gmail",
            "draft_123",
        ),
        (
            "ATC",
            "airtable_context_agent",
            {
                "operation": "update",
                "record_id": "rec_123",
                "base_alias": "finance_tax_tracker",
                "table": "Business Expenses",
                "verification": {"passed": True},
            },
            "airtable",
            "rec_123",
        ),
        (
            "ZC",
            "zotero_context_agent",
            {
                "status": "success",
                "operation": "read_items",
                "provider_read": True,
                "selected_item_key": "article_123",
                "selected_item_title": "Selected journal article",
                "library_id": "12345",
                "library_type": "user",
            },
            "zotero",
            "article_123",
        ),
    ],
)
def test_verified_provider_object_survives_slack_thread_and_agent_switch(
    tmp_path: Path,
    alias: str,
    expected_agent: str,
    receipt: dict[str, object],
    expected_provider: str,
    expected_object_id: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'provider-continuity.db'}"
    payload: dict[str, object] = {
        "status": "done",
        "human_summary": "The exact provider object was verified.",
        "tool_receipt": receipt,
    }
    attach_execution_public_result(payload)
    payload["slack_run_provenance"] = {
        "schema": "keystone.slack.run_provenance.v1",
        "context_validated": True,
        "team_id": "T123",
        "channel_id": "C123",
        "thread_ts": "1715366400.000100",
        "request_ts": "1715366401.000100",
    }
    SQLiteStore(database_url).save_agent_run(
        agent_name="chief_of_staff",
        input_summary="Create or update the exact provider object.",
        dry_run=False,
        status="success",
        output=payload,
    )

    references = slack_actions.latest_verified_provider_objects_for_slack_thread(
        team_id="T123",
        channel_id="C123",
        thread_ts="1715366400.000100",
        before_request_ts="1715366402.000100",
        database_url=database_url,
    )
    assert len(references) == 1
    assert references[0].provider_system == expected_provider
    assert references[0].object_id == expected_object_id
    assert (
        slack_actions.latest_verified_provider_objects_for_slack_thread(
            team_id="T123",
            channel_id="C123",
            thread_ts="different-thread",
            before_request_ts="1715366402.000100",
            database_url=database_url,
        )
        == ()
    )

    current_request = "inspect the exact object now."
    request = build_execution_request(
        "\n".join(
            [
                "business agents continue this prior Slack thread.",
                "Prior task owner (advisory): chief_of_staff",
                "Previous request: Create the exact provider object.",
                "Previous result title: Business Agents Result Ready",
                "Previous result: The prior agent said it completed.",
                f"User follow-up: {alias} {current_request}",
                "Continue the same agent task.",
            ]
        )
    )
    resolved = cli._execution_request_with_workflow_verified_objects(
        request,
        {
            "verified_provider_objects": [
                reference.model_dump(mode="json")
                for reference in references
            ]
        },
    )

    assert resolved.requested_agent == expected_agent
    assert resolved.current_request == current_request
    assert resolved.continuation.prior_agent == "chief_of_staff"
    assert resolved.continuation.verified_objects[0].object_id == expected_object_id
    planning_text = execution_request_planning_text(resolved)
    assert f"id={expected_object_id}" in planning_text
    assert planning_text.endswith(f"Authoritative follow-up: {current_request}")
