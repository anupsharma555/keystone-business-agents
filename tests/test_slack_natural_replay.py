from __future__ import annotations

import json
from pathlib import Path

import keystone_agents.cli as cli
from keystone_agents.agents.orchestrator import run_orchestrator_preflight
from keystone_agents.slack_actions import (
    build_selected_message_context,
    orchestrator_workflow_state_from_slack_context,
)
from keystone_agents.storage.sqlite_store import SQLiteStore

FIXTURE_PATH = Path("tests/fixtures/slack_followup_subject_change_replay.json")
ORDINAL_FIXTURE_PATH = Path(
    "tests/fixtures/slack_zotero_ordinal_correction_replay.json"
)


def test_subject_change_replay_keeps_current_human_ask_authoritative(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    database_url = f"sqlite:///{tmp_path / 'replay.db'}"
    prior_run = fixture["prior_provider_run"]
    run_id = SQLiteStore(database_url).save_agent_run(
        agent_name=prior_run["agent_name"],
        input_summary=prior_run["input_summary"],
        dry_run=False,
        output=prior_run["output"],
    )
    context = build_selected_message_context(
        fixture["message_action_payload"],
        thread_messages=fixture["thread_messages"],
    )
    context.prior_agent_runs = [
        {
            "run_id": str(run_id),
            "route": "gmail_triage",
            "status": "completed",
        }
    ]

    current_request = fixture["current_request"]
    workflow_state = orchestrator_workflow_state_from_slack_context(
        context,
        request_text=current_request,
        database_url=database_url,
    )
    preflight = run_orchestrator_preflight(
        current_request,
        requested_agent=fixture["requested_agent"],
        live_manual_plan=False,
        workflow_state=workflow_state,
    )

    transcript = workflow_state["slack_thread_transcript"]
    expected = fixture["expected"]
    assert transcript.endswith(current_request)
    assert "Business Agents Completion Not Confirmed" in transcript
    for fragment in expected["forbidden_transcript_fragments"]:
        assert fragment not in transcript
    assert workflow_state["prior_provider_result_scope"]["provider_system"] == "gmail"
    assert workflow_state["prior_provider_result_scope"]["verified"] is True
    assert preflight.selected_agent == expected["selected_agent"]
    assert preflight.manual_request_plan.intent == expected["intent"]
    assert preflight.manual_request_plan.provider_system == expected["provider_system"]
    assert preflight.manual_request_plan.workflow == expected["workflow"]
    assert preflight.manual_request_plan.objective == current_request
    assert preflight.manual_request_plan.provider_system != (
        workflow_state["prior_provider_result_scope"]["provider_system"]
    )


def test_zotero_ordinal_correction_replay_changes_provider_object_without_stale_bot_authority(
    tmp_path: Path,
    capsys,
) -> None:
    fixture = json.loads(ORDINAL_FIXTURE_PATH.read_text(encoding="utf-8"))
    database_url = f"sqlite:///{tmp_path / 'ordinal-replay.db'}"
    prior_run = fixture["prior_provider_run"]
    run_id = SQLiteStore(database_url).save_agent_run(
        agent_name=prior_run["agent_name"],
        input_summary=prior_run["input_summary"],
        dry_run=False,
        output=prior_run["output"],
    )
    context = build_selected_message_context(
        fixture["message_action_payload"],
        thread_messages=fixture["thread_messages"],
    )
    context.prior_agent_runs = [
        {
            "run_id": str(run_id),
            "route": "zotero_context_agent",
            "status": "completed",
        }
    ]
    context_path = tmp_path / "ordinal-slack-context.json"
    context_path.write_text(
        context.model_dump_json(by_alias=True),
        encoding="utf-8",
    )
    current_request = fixture["current_request"]
    continuation = " ".join(
        (
            "zotero context agent continue this prior Slack thread.",
            "Previous request: Zotero Context Agent, read the most recently added "
            "journal article and give me its title.",
            "Previous result title: Business Agents Zotero Context Ready",
            "Previous result: Title: Latest provider article.",
            f"User follow-up: {current_request}",
            "Continue the same agent task, treating the current user request as authoritative.",
        )
    )

    exit_code = cli.main(
        [
            "ask",
            "--database-url",
            database_url,
            "--context-file",
            str(context_path),
            "--no-live-sdk",
            "--json",
            continuation,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    plan = payload["manual_request_plan"]
    expected = fixture["expected"]
    normalized_current_request = current_request.replace("—", "-")
    assert payload["input"] == normalized_current_request
    assert plan["objective"] == normalized_current_request
    assert plan["target_agent"] == expected["target_agent"]
    assert plan["provider_system"] == expected["provider_system"]
    assert plan["provider_operations"] == expected["provider_operations"]
    assert plan["provider_selection_order"] == expected["provider_selection_order"]
    assert plan["provider_selection_rank"] == expected["provider_selection_rank"]
    assert plan["zotero_requested_fields"] == expected["zotero_requested_fields"]
    assert plan["requires_live_search"] is False
    assert plan["ask_shape"]["permission_state"] == "read_only"
    assert payload["_execution"]["openai_requests"] == 0
    serialized = json.dumps(payload, sort_keys=True)
    for fragment in expected["forbidden_transcript_fragments"]:
        assert fragment not in serialized
