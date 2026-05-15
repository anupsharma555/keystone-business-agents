from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.cli import main
from keystone_agents.slack_actions import (
    RUN_AGENT_MESSAGE_CALLBACK_ID,
    RUN_AGENT_TASK_ACTION_ID,
    RUN_AGENT_TASK_BLOCK_ID,
    RUN_AGENT_VIEW_CALLBACK_ID,
    build_selected_message_context,
    handle_run_agent_interaction,
    slack_message_action_manifest_patch,
)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'slack_action.db'}"


def _message_action_payload() -> dict:
    return {
        "type": "message_action",
        "callback_id": RUN_AGENT_MESSAGE_CALLBACK_ID,
        "trigger_id": "trigger-123",
        "team": {"id": "T123", "domain": "kni"},
        "channel": {"id": "C123", "name": "bizdev"},
        "user": {"id": "U123", "username": "anup"},
        "message": {
            "type": "message",
            "user": "U456",
            "ts": "1715366400.000100",
            "thread_ts": "1715366400.000100",
            "text": "Can someone research Acme Health before the partner call?",
            "permalink": "https://kni.slack.com/archives/C123/p1715366400000100",
        },
    }


def _modal_submission(private_metadata: str, task: str = "research Acme Health") -> dict:
    return {
        "type": "view_submission",
        "user": {"id": "U123", "username": "anup"},
        "view": {
            "callback_id": RUN_AGENT_VIEW_CALLBACK_ID,
            "private_metadata": private_metadata,
            "state": {
                "values": {
                    RUN_AGENT_TASK_BLOCK_ID: {
                        RUN_AGENT_TASK_ACTION_ID: {
                            "type": "plain_text_input",
                            "value": task,
                        }
                    }
                }
            },
        },
    }


def test_slack_message_shortcut_manifest_uses_run_agent_callback() -> None:
    patch = slack_message_action_manifest_patch()

    shortcut = patch["features"]["shortcuts"][0]
    assert shortcut["type"] == "message"
    assert shortcut["callback_id"] == RUN_AGENT_MESSAGE_CALLBACK_ID
    assert patch["settings"]["interactivity"]["is_enabled"] is True


def test_selected_message_payload_parses_bounded_thread_context() -> None:
    context = build_selected_message_context(
        _message_action_payload(),
        thread_messages=[
            {"ts": "1715366400.000100", "user": "U456", "text": "Selected root"},
            {"ts": "1715366460.000200", "user": "U789", "text": "Thread reply"},
        ],
    )

    assert context.channel_id == "C123"
    assert context.permalink.endswith("p1715366400000100")
    assert context.thread_fetch_status == "ok"
    assert [message.text for message in context.thread_messages] == [
        "Selected root",
        "Thread reply",
    ]


def test_message_action_creates_context_file_and_modal(tmp_path: Path) -> None:
    result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    assert result.stage == "modal"
    assert result.callback_id == RUN_AGENT_MESSAGE_CALLBACK_ID
    assert result.modal_view is not None
    assert result.modal_view["callback_id"] == RUN_AGENT_VIEW_CALLBACK_ID
    context_path = Path(result.context_file_path)
    assert context_path.is_file()
    context_payload = json.loads(context_path.read_text(encoding="utf-8"))
    assert context_payload["permalink"] == "https://kni.slack.com/archives/C123/p1715366400000100"
    assert context_payload["selected_message"]["text"].startswith("Can someone research")



def test_modal_submission_can_create_context_file_from_private_metadata(tmp_path: Path) -> None:
    context = build_selected_message_context(_message_action_payload())
    private_metadata = json.dumps(
        {
            "schema": context.schema_,
            "selected_context": context.model_dump(mode="json", by_alias=True),
        }
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(private_metadata),
        database_url=_database_url(tmp_path),
        context_dir=tmp_path / "contexts",
    )

    assert run_result.stage == "work_item"
    assert Path(run_result.context_file_path).is_file()
    work_item = run_result.work_item or {}
    assert work_item["target"]["metadata"]["slack_context"]["channel_id"] == "C123"


def test_modal_submission_starts_work_item_with_slack_metadata(tmp_path: Path) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )
    submission = _modal_submission(modal_result.modal_view["private_metadata"])

    run_result = handle_run_agent_interaction(
        submission,
        database_url=_database_url(tmp_path),
    )

    assert run_result.stage == "work_item"
    assert run_result.route == "business_research_analyst"
    work_item = run_result.work_item or {}
    slack_context = work_item["target"]["metadata"]["slack_context"]
    assert slack_context["permalink"] == "https://kni.slack.com/archives/C123/p1715366400000100"
    assert slack_context["selected_message"]["text"].startswith("Can someone research")
    assert any(source["source_type"] == "slack_message" for source in work_item["sources"])
    assert work_item["approval_gates"] == []


def test_thread_fetch_failure_warns_but_run_proceeds(tmp_path: Path) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
        thread_fetch_error="missing_scope",
    )
    assert modal_result.warnings
    submission = _modal_submission(modal_result.modal_view["private_metadata"])

    run_result = handle_run_agent_interaction(
        submission,
        database_url=_database_url(tmp_path),
    )

    assert run_result.stage == "work_item"
    assert run_result.status in {"in_progress", "done"}
    assert "missing_scope" in run_result.warnings[0]
    work_item = run_result.work_item or {}
    assert work_item["target"]["metadata"]["slack_context"]["thread_fetch_status"] == "failed"
    assert any("Slack context warning" in note for note in work_item["audit_notes"])


def test_cli_ask_context_file_attaches_slack_context(
    tmp_path: Path,
    capsys,
) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    exit_code = main(
        [
            "ask",
            "--database-url",
            _database_url(tmp_path),
            "--context-file",
            modal_result.context_file_path,
            "--json",
            "research Acme Health",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    slack_context = payload["work_item"]["target"]["metadata"]["slack_context"]
    assert slack_context["channel_id"] == "C123"
    assert payload["context_pack"]["source_refs"][0]["source_type"] == "slack_message"
