from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.handle_slack_agent_action as slack_agent_action_cli
from keystone_agents.cli import main
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.approval import ApprovalQueueItem
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.slack_actions import (
    RUN_AGENT_MESSAGE_CALLBACK_ID,
    RUN_AGENT_TASK_ACTION_ID,
    RUN_AGENT_TASK_BLOCK_ID,
    RUN_AGENT_VIEW_CALLBACK_ID,
    SlackAgentActionResult,
    build_run_agent_modal,
    build_selected_message_context,
    handle_run_agent_interaction,
    slack_message_action_manifest_patch,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.structured_logging import structured_log_event


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


def test_selected_message_context_sorts_and_dedupes_thread_messages() -> None:
    context = build_selected_message_context(
        _message_action_payload(),
        thread_messages=[
            {"ts": "1715366460.000200", "user": "U789", "text": "Thread reply"},
            {"ts": "1715366400.000100", "user": "U456", "text": "Selected root"},
            {"ts": "1715366460.000200", "user": "U789", "text": "Duplicate reply"},
        ],
    )

    assert [message.text for message in context.thread_messages] == [
        "Selected root",
        "Thread reply",
    ]


def test_selected_message_context_limits_thread_messages_to_recent_week() -> None:
    context = build_selected_message_context(
        _message_action_payload(),
        thread_messages=[
            {
                "ts": "1715366400.000100",
                "user": "U456",
                "text": "Older selected root from more than one week before latest reply",
            },
            {
                "ts": "1715366460.000200",
                "user": "U789",
                "text": "Older reply outside recent window",
            },
            {
                "ts": "1716400000.000300",
                "user": "U999",
                "text": "Recent reply inside one-week context window",
            },
        ],
    )

    assert [message.text for message in context.thread_messages] == [
        "Recent reply inside one-week context window",
    ]
    assert context.metadata["context_window_days"] == 7
    assert context.metadata["channel_history_included"] is False
    assert any("most recent 7 days" in warning for warning in context.warnings)


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
    assert context_path.is_absolute()
    metadata = json.loads(result.modal_view["private_metadata"])
    assert metadata["selected_context"]["selected_message"]["text"].startswith("Can someone")


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
    query_prompt = work_item["target"]["metadata"]["slack_context"]["query_prompt"]
    assert query_prompt["kind"] == "research_summary"
    assert query_prompt["target_route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert "task_brief" not in query_prompt
    assert any(
        event["event_type"] == "slack_query_prompt_selected"
        for event in run_result.feedback_events
    )


def test_modal_submission_uses_embedded_context_when_context_file_is_missing(
    tmp_path: Path,
) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )
    missing_path = Path(modal_result.context_file_path)
    missing_path.unlink()

    run_result = handle_run_agent_interaction(
        _modal_submission(modal_result.modal_view["private_metadata"]),
        database_url=_database_url(tmp_path),
        context_dir=tmp_path / "contexts",
    )

    assert run_result.stage == "work_item"
    assert Path(run_result.context_file_path).is_file()
    assert any("embedded modal context fallback" in warning for warning in run_result.warnings)
    work_item = run_result.work_item or {}
    assert work_item["target"]["metadata"]["slack_context"]["channel_id"] == "C123"


def test_run_agent_modal_private_metadata_uses_small_embedded_fallback(
    tmp_path: Path,
) -> None:
    context = build_selected_message_context(
        _message_action_payload(),
        thread_messages=[
            {"ts": f"17153664{i}.000200", "user": "U789", "text": "Thread reply " + ("x" * 1200)}
            for i in range(20)
        ],
    )
    context.prior_agent_runs = [
        {
            "run_id": f"run_{i}",
            "request_text": "prior request " + ("y" * 1200),
            "result_text": "prior result " + ("z" * 1200),
        }
        for i in range(10)
    ]

    modal = build_run_agent_modal(
        context,
        context_file_path=tmp_path / "slack-context-large.json",
    )
    metadata = json.loads(modal["private_metadata"])

    assert len(modal["private_metadata"]) < 2800
    assert metadata["context_file_path"].endswith("slack-context-large.json")
    assert metadata["selected_context"]["channel_id"] == "C123"
    assert metadata["selected_context"].get("thread_messages") in (None, [])
    assert metadata["selected_context"].get("prior_agent_runs") in (None, [])
    assert metadata["selected_context"]["metadata"]["fallback_scope"] == (
        "selected_message_minimal"
    )


def test_run_agent_modal_private_metadata_has_final_pointer_size_guard() -> None:
    context = build_selected_message_context(_message_action_payload())
    context.team_id = "T" * 500
    context.team_domain = "kni-" + ("domain" * 90)
    context.channel_id = "C" * 500
    context.channel_name = "channel-" + ("name" * 120)
    context.selected_message_ts = "1715366400." + ("1" * 500)
    context.thread_ts = "1715366400." + ("2" * 500)
    context.permalink = "https://kni.slack.com/archives/" + ("x" * 500)
    context.selected_message.ts = "1715366400." + ("3" * 500)
    context.selected_message.user_id = "U" * 500
    context.selected_message.username = "user-" + ("name" * 120)
    context.selected_message.permalink = "https://kni.slack.com/archives/" + ("y" * 500)
    context.warnings = ["warning-" + ("z" * 500) for _ in range(5)]

    modal = build_run_agent_modal(context, context_file_path="Z" * 500)
    metadata = json.loads(modal["private_metadata"])

    assert len(modal["private_metadata"]) < 2800
    assert metadata["context_file_path"] == "Z" * 500
    assert metadata["selected_context"]["metadata"]["fallback_scope"] == (
        "selected_message_pointer"
    )
    assert metadata["selected_context"].get("thread_messages") in (None, [])
    assert metadata["selected_context"].get("prior_agent_runs") in (None, [])


def test_modal_submission_orchestrator_block_stops_before_work_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("WorkItem manager loop should not run after preflight block")

    monkeypatch.setattr(
        "keystone_agents.slack_actions.advance_work_item_manager_loop",
        fail_if_called,
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            modal_result.modal_view["private_metadata"],
            task="send this outreach email now",
        ),
        database_url=_database_url(tmp_path),
        context_dir=tmp_path / "contexts",
        live_sdk=True,
    )

    assert run_result.stage == "work_item"
    assert run_result.work_item is None
    assert run_result.status == "blocked"
    assert run_result.result is not None
    assert run_result.result["block_kind"] == "send"
    assert run_result.result["send_enabled"] is False
    assert run_result.feedback_events[0]["payload"]["execution_allowed"] is False


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
    manual_plan = work_item["target"]["metadata"]["manual_request_plan"]
    result_payload = run_result.result or {}
    assert slack_context["permalink"] == "https://kni.slack.com/archives/C123/p1715366400000100"
    assert slack_context["selected_message"]["text"].startswith("Can someone research")
    assert manual_plan["target_agent"] == "business_research_analyst"
    assert manual_plan["intent"] == "company_research"
    preflight_payload = result_payload["orchestrator_preflight"]
    assert preflight_payload["selected_agent"] == "business_research_analyst"
    assert preflight_payload["route_result"]["route"] == "business_research_analyst"
    assert "workflow_state_summary" not in preflight_payload["route_result"]
    assert "Can someone research" not in json.dumps(preflight_payload)
    provenance = run_result.run_provenance
    assert provenance["schema"] == "keystone.slack.agent_run_provenance.v1"
    assert provenance["context_validated"] is True
    assert provenance["validation_errors"] == []
    assert provenance["work_item_id"] == work_item["id"]
    assert provenance["source_channel_id"] == "C123"
    assert provenance["source_message_ts"] == "1715366400.000100"
    assert provenance["source_thread_ts"] == "1715366400.000100"
    assert result_payload["slack_run_provenance"] == provenance
    assert any(source["source_type"] == "slack_message" for source in work_item["sources"])
    assert work_item["approval_gates"] == []


def test_slack_eval_message_action_records_run_and_reply_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from promptfoo.eval_database import eval_case_status

    review_db = tmp_path / "human-reviews.sqlite"
    monkeypatch.setenv("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB", str(review_db))
    payload = _message_action_payload()
    payload["channel"] = {"id": "C0BA17Y9C01", "name": "evals"}
    payload["eval"] = {
        "case_id": "slack_bridge_hidden_eval_001",
        "source": "slack_eval_channel_backend",
        "visible_in_prompt": False,
    }

    modal_result = handle_run_agent_interaction(
        payload,
        context_dir=tmp_path / "contexts",
    )
    metadata = json.loads(modal_result.modal_view["private_metadata"])
    assert metadata["selected_context"]["eval"]["case_id"] == "slack_bridge_hidden_eval_001"

    run_result = handle_run_agent_interaction(
        _modal_submission(modal_result.modal_view["private_metadata"]),
        database_url=_database_url(tmp_path),
        context_dir=tmp_path / "contexts",
    )

    eval_record = run_result.eval_record or {}
    result_payload = run_result.result or {}
    assert eval_record["case_id"] == "slack_bridge_hidden_eval_001"
    assert eval_record["run_id"] == run_result.work_item["id"]
    assert eval_record["slack_thread_ts"] == "1715366400.000100"
    assert eval_record["dashboard_case_url"].endswith(
        "?case=slack_bridge_hidden_eval_001"
    )
    assert eval_record["scorecard_request"] == (
        "@KNI can you give me a scorecard for this eval?"
    )
    assert eval_record["dashboard_visibility"]["case_visible"] is True
    assert eval_record["dashboard_visibility"]["latest_run_visible"] is True
    assert eval_record["dashboard_visibility"]["trace_event_visible"] is True
    assert eval_record["post_save_state"]["merged_status"]["slack_run_count"] == 1
    assert eval_record["post_save_state"]["trace"]["manual_run_summary_present"] is True
    assert "/api/status?refresh=1" in eval_record["refresh_endpoints"]
    assert "/api/trace-diagnostics" in eval_record["refresh_endpoints"]
    assert result_payload["eval_record"] == eval_record
    assert result_payload["slack_actions"][0]["label"] == "Submit Evaluation"
    assert result_payload["slack_actions"][0]["action_id"] == "kba_eval_review"
    assert result_payload["slack_actions"][0]["intent"] == "eval_review"
    assert result_payload["slack_actions"][0]["style"] == "primary"
    assert (
        result_payload["slack_actions"][0]["metadata"]["eval_record"]["case_id"]
        == "slack_bridge_hidden_eval_001"
    )
    assert (
        result_payload["slack_actions"][0]["metadata"]["eval_record"]["run_id"]
        == run_result.work_item["id"]
    )
    assert result_payload["slack_overflow_actions"] == []
    assert eval_record["slack_actions"][0] == result_payload["slack_actions"][0]
    assert eval_record["eval_thread_reply"]["slack_actions"][0] == result_payload["slack_actions"][0]
    assert "Eval: case `slack_bridge_hidden_eval_001`" in result_payload["human_summary"]
    assert (
        "<http://127.0.0.1:8769/dashboard?case=slack_bridge_hidden_eval_001|case dashboard>"
        in result_payload["human_summary"]
    )
    assert "press `Submit Evaluation` in Slack" in result_payload["human_summary"]
    assert (
        "<http://127.0.0.1:8769/review?case=slack_bridge_hidden_eval_001|score this case>"
        in result_payload["human_summary"]
    )

    status = eval_case_status("slack_bridge_hidden_eval_001", database_path=review_db)
    assert status["slack_run_count"] == 1
    assert status["slack_runs"][0]["run_id"] == run_result.work_item["id"]
    assert status["slack_runs"][0]["work_item_id"] == run_result.work_item["id"]
    assert status["slack_runs"][0]["thread_fetch_status"] in {"not_requested", "ok"}
    assert status["slack_runs"][0]["thread_message_count"] >= 1
    assert status["slack_runs"][0]["response_hash"]
    assert status["slack_runs"][0]["evidence"]["schema"] == "keystone.slack.eval_evidence.v1"
    assert eval_record["evidence"]["work_item_id"] == run_result.work_item["id"]


def test_modal_submission_selects_reusable_prompt_for_multi_target_source_read(
    tmp_path: Path,
) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )
    task = (
        "business research analyst: reusable source-read test. Compare how three public "
        "AI companion or chatbot products describe teen safety, escalation, or "
        "trusted-contact features. Do not draft, send, publish, schedule, write files, "
        "or post elsewhere."
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(modal_result.modal_view["private_metadata"], task=task),
        database_url=_database_url(tmp_path),
        context_dir=tmp_path / "contexts",
    )

    work_item = run_result.work_item or {}
    slack_context = work_item["target"]["metadata"]["slack_context"]
    query_prompt = slack_context["query_prompt"]
    prompt_events = [
        event
        for event in run_result.feedback_events
        if event["event_type"] == "slack_query_prompt_selected"
    ]

    assert prompt_events
    assert query_prompt["kind"] == "research_summary"
    assert query_prompt["target_route"] == WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value
    assert query_prompt["context_flags"]["needs_source_triage"] is True
    assert "task_brief" not in query_prompt
    assert any(
        artifact["artifact_type"] == "multi_target_research"
        for artifact in work_item["artifact_refs"]
    )
    assert work_item["status"] == WorkItemStatus.BLOCKED.value


def test_modal_submission_gmail_request_uses_gmail_context_gate_not_unsupported_route(
    tmp_path: Path,
) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            modal_result.modal_view["private_metadata"],
            task=(
                "A Gmail consulting inquiry came in. Triage it, research the company, "
                "create an opportunity record, and draft a response only after approval."
            ),
        ),
        database_url=_database_url(tmp_path),
        context_dir=tmp_path / "contexts",
    )
    result_payload = run_result.result or {}
    blockers = result_payload["blockers"]
    blocker_codes = {blocker["code"] for blocker in blockers}

    assert run_result.stage == "work_item"
    assert run_result.route == "gmail_triage"
    assert run_result.status == "blocked"
    assert "gmail_context_required" in blocker_codes
    assert "manager_loop_research_not_completed" in blocker_codes
    assert "manager_loop_opportunity_not_created" in blocker_codes
    assert "manager_loop_outreach_not_drafted" in blocker_codes
    assert "route_not_supported_in_workitem_phase" not in blocker_codes
    assert result_payload.get("send_enabled", False) is False


def test_modal_submission_passes_prior_thread_runs_to_orchestrator_preflight(
    tmp_path: Path,
) -> None:
    context = build_selected_message_context(_message_action_payload())
    context.prior_agent_runs = [
        {
            "run_id": "sbar_prior",
            "request_text": "prior architecture review",
            "work_item_id": "wi_prior",
            "route": "chief_of_staff",
            "status": "completed",
            "result_text": "Prior run summarized architecture blockers.",
            "operator_feedback": [
                {
                    "type": "correction",
                    "text": "That response was unrelated to the architecture request.",
                }
            ],
            "created_at": "2026-05-25T10:00:00Z",
        }
    ]
    private_metadata = json.dumps(
        {
            "schema": context.schema_,
            "selected_context": context.model_dump(mode="json", by_alias=True),
        }
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            private_metadata, task="chief of staff continue this architecture review"
        ),
        database_url=_database_url(tmp_path),
    )

    preflight_payload = (run_result.result or {})["orchestrator_preflight"]
    assert preflight_payload["selected_agent"] == "chief_of_staff"
    assert "workflow_state_summary" not in preflight_payload["route_result"]
    serialized_preflight = json.dumps(preflight_payload)
    assert "architecture blockers" not in serialized_preflight
    assert "unrelated to the architecture request" not in serialized_preflight


def test_modal_submission_passes_channel_automation_context_when_relevant(
    tmp_path: Path,
) -> None:
    payload = _message_action_payload()
    payload["channel"] = {"id": "CWORKFLOW", "name": "ai-agents-workflow"}
    modal_result = handle_run_agent_interaction(
        payload,
        context_dir=tmp_path / "contexts",
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            modal_result.modal_view["private_metadata"],
            task="chief of staff why did this post here and what is running here",
        ),
        database_url=_database_url(tmp_path),
    )

    preflight_payload = (run_result.result or {})["orchestrator_preflight"]
    assert preflight_payload["selected_agent"] == "chief_of_staff"
    assert "workflow_state_summary" not in preflight_payload["route_result"]
    assert "auto_weekly_opportunity" not in json.dumps(preflight_payload)


def test_modal_submission_omits_channel_automation_context_for_unrelated_research(
    tmp_path: Path,
) -> None:
    payload = _message_action_payload()
    payload["channel"] = {"id": "CWORKFLOW", "name": "ai-agents-workflow"}
    modal_result = handle_run_agent_interaction(
        payload,
        context_dir=tmp_path / "contexts",
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            modal_result.modal_view["private_metadata"],
            task="research Acme Health before the partner call",
        ),
        database_url=_database_url(tmp_path),
    )

    preflight_payload = (run_result.result or {})["orchestrator_preflight"]
    assert preflight_payload["route_result"]["route"] == "business_research_analyst"
    assert "workflow_state_summary" not in preflight_payload["route_result"]


def test_modal_submission_rejects_mismatched_final_result_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    def fake_advance_work_item_manager_loop(request, *, feedback_callback=None):
        return WorkflowRunResult(
            work_item=WorkItem(
                kind=WorkItemKind.RESEARCH_BRIEF,
                title="Mismatched Slack run",
                request_text=request.request_text,
                current_route=WorkItemRoute.CHIEF_OF_STAFF,
                target=WorkItemTarget(
                    metadata={
                        "slack_context": {
                            "channel_id": "C999",
                            "selected_message_ts": "9999999999.999999",
                            "thread_ts": "9999999999.999999",
                        }
                    }
                ),
            ),
            route=WorkItemRoute.CHIEF_OF_STAFF,
            status=WorkItemStatus.DONE,
            advanced=True,
        )

    monkeypatch.setattr(
        "keystone_agents.slack_actions.advance_work_item_manager_loop",
        fake_advance_work_item_manager_loop,
    )

    with pytest.raises(ValueError, match="Slack run provenance mismatch"):
        handle_run_agent_interaction(
            _modal_submission(modal_result.modal_view["private_metadata"]),
            database_url=_database_url(tmp_path),
        )


def test_modal_submission_rejects_missing_final_result_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    def fake_advance_work_item_manager_loop(request, *, feedback_callback=None):
        return WorkflowRunResult(
            work_item=WorkItem(
                kind=WorkItemKind.RESEARCH_BRIEF,
                title="Missing Slack run",
                request_text=request.request_text,
                current_route=WorkItemRoute.CHIEF_OF_STAFF,
                target=WorkItemTarget(metadata={"slack_context": {}}),
            ),
            route=WorkItemRoute.CHIEF_OF_STAFF,
            status=WorkItemStatus.DONE,
            advanced=True,
        )

    monkeypatch.setattr(
        "keystone_agents.slack_actions.advance_work_item_manager_loop",
        fake_advance_work_item_manager_loop,
    )

    with pytest.raises(ValueError, match="Slack run provenance mismatch"):
        handle_run_agent_interaction(
            _modal_submission(modal_result.modal_view["private_metadata"]),
            database_url=_database_url(tmp_path),
        )


def test_modal_submission_exposes_realtime_orchestrator_feedback(tmp_path: Path) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )
    forwarded_events: list[tuple[str, dict]] = []

    run_result = handle_run_agent_interaction(
        _modal_submission(modal_result.modal_view["private_metadata"]),
        database_url=_database_url(tmp_path),
        feedback_callback=lambda event_type, payload: forwarded_events.append(
            (event_type, payload)
        ),
    )

    event_types = [event["event_type"] for event in run_result.feedback_events]
    assert event_types[0] == "orchestrator_preflight"
    assert "manager_loop_review" in event_types
    assert "manager_loop_completed" in event_types
    assert all(
        event["schema"] == "keystone.slack.agent_feedback_event.v1"
        for event in run_result.feedback_events
    )
    preflight = run_result.feedback_events[0]["payload"]
    assert preflight["selected_agent"] == "business_research_analyst"
    assert preflight["blocked_by_orchestrator"] is False
    review = next(
        event["payload"]
        for event in run_result.feedback_events
        if event["event_type"] == "manager_loop_review"
    )
    assert review["route"] == "business_research_analyst"
    assert review["overall_score"] >= 0
    assert review["review_decision"] in {"pass", "warn", "block"}
    if review["review_status"] == "fail":
        assert review["blocking"] is False
        assert review["advisory"] is True
        assert review["review_decision"] == "warn"
    assert [event_type for event_type, _ in forwarded_events] == event_types


def test_realtime_feedback_callback_failure_is_nonfatal(tmp_path: Path) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )
    forwarded_events: list[str] = []

    def failing_callback(event_type: str, _payload: dict) -> None:
        forwarded_events.append(event_type)
        raise RuntimeError("modal update failed")

    run_result = handle_run_agent_interaction(
        _modal_submission(modal_result.modal_view["private_metadata"]),
        database_url=_database_url(tmp_path),
        feedback_callback=failing_callback,
    )

    event_types = [event["event_type"] for event in run_result.feedback_events]
    assert run_result.stage == "work_item"
    assert run_result.status in {"in_progress", "done"}
    assert forwarded_events == ["orchestrator_preflight"]
    assert "feedback_delivery_failed" in event_types
    assert "manager_loop_review" in event_types
    assert "manager_loop_completed" in event_types
    failure = next(
        event["payload"]
        for event in run_result.feedback_events
        if event["event_type"] == "feedback_delivery_failed"
    )
    assert failure["failed_event_type"] == "orchestrator_preflight"
    assert failure["error_type"] == "RuntimeError"
    assert failure["send_enabled"] is False
    assert (run_result.result or {}).get("send_enabled") is not True


def test_realtime_feedback_events_do_not_authorize_side_effects(tmp_path: Path) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(modal_result.modal_view["private_metadata"]),
        database_url=_database_url(tmp_path),
        feedback_callback=lambda _event_type, _payload: None,
    )

    forbidden_keys = {
        "approval_id",
        "approval_state",
        "approval_granted",
        "send_enabled",
        "can_send_email",
        "posted",
        "published",
        "scheduled",
        "crm_updated",
        "live_side_effects_enabled",
    }
    for event in run_result.feedback_events:
        payload = event["payload"]
        assert not any(payload.get(key) is True for key in forbidden_keys)
        assert "approval_id" not in payload
        if event["event_type"] == "manager_loop_review":
            assert payload["review_decision"] in {"pass", "warn", "block"}
            assert isinstance(payload["blocking"], bool)
            assert isinstance(payload["advisory"], bool)
    assert (run_result.result or {}).get("send_enabled") is not True


def test_slack_agent_action_cli_streams_feedback_jsonl(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    context = build_selected_message_context(_message_action_payload())
    private_metadata = json.dumps(
        {
            "schema": context.schema_,
            "selected_context": context.model_dump(mode="json", by_alias=True),
        }
    )
    payload_path = tmp_path / "view-submission.json"
    payload_path.write_text(
        json.dumps(_modal_submission(private_metadata), ensure_ascii=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "handle_slack_agent_action.py",
            "--payload-file",
            str(payload_path),
            "--database-url",
            _database_url(tmp_path),
            "--context-dir",
            str(tmp_path / "contexts"),
            "--feedback-jsonl",
            "--json",
        ],
    )

    assert slack_agent_action_cli.main() == 0

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    events = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    assert output["stage"] == "work_item"
    assert events[0]["schema"] == "keystone.slack.agent_feedback_event.v1"
    assert events[0]["event_type"] == "orchestrator_preflight"
    assert events[0]["structured_log"]["schema"] == "keystone.structured_log.v1"
    assert events[0]["structured_log"]["component"] == "slack_agent_action"
    assert events[0]["structured_log"]["event"] == "orchestrator_preflight"
    assert events[0]["structured_log"]["redaction"]["raw_payload_included"] is False
    assert any(event["event_type"] == "manager_loop_review" for event in events)


def test_structured_log_event_redacts_secrets_and_body_text() -> None:
    event = structured_log_event(
        component="promptfoo_provider",
        event="child_failed",
        level="error",
        payload={
            "run_id": "run_123",
            "case_id": "case_abc",
            "status": "error",
            "stdout": "raw answer sk-testSECRET123456 should not be stored",
            "api_key": "sk-testSECRET123456",
        },
    )

    serialized = json.dumps(event, sort_keys=True)
    assert event["schema"] == "keystone.structured_log.v1"
    assert event["correlation"]["run_id"] == "run_123"
    assert event["correlation"]["case_id"] == "case_abc"
    assert event["redaction"]["raw_payload_included"] is False
    assert "stdout_summary" in event["payload"]
    assert "stdout" not in event["payload"]
    assert "sk-testSECRET123456" not in serialized


def test_slack_agent_action_cli_prints_thread_reply_for_local_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_path = tmp_path / "view-submission.json"
    payload_path.write_text(json.dumps({"type": "view_submission"}), encoding="utf-8")

    def fake_handle_run_agent_interaction(*_args, **_kwargs):
        return SlackAgentActionResult(
            stage="work_item",
            callback_id=RUN_AGENT_VIEW_CALLBACK_ID,
            work_item={"id": "wi_eval_123"},
            route="opportunity_scout",
            status="done",
            run_provenance={"source_thread_ts": "1781202023.470699"},
            result={
                "human_summary": (
                    "Eval `slack_agents_sdk_course_001` status: Promptfoo pass. "
                    "Dashboard: <http://127.0.0.1:8769/dashboard?case=slack_agents_sdk_course_001|case dashboard>."
                )
            },
        )

    monkeypatch.setattr(
        slack_agent_action_cli,
        "handle_run_agent_interaction",
        fake_handle_run_agent_interaction,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "handle_slack_agent_action.py",
            "--payload-file",
            str(payload_path),
            "--database-url",
            _database_url(tmp_path),
        ],
    )

    assert slack_agent_action_cli.main() == 0

    output = capsys.readouterr().out
    assert "Keystone WorkItem started." in output
    assert "WorkItem: wi_eval_123" in output
    assert "Thread reply:" in output
    assert "case dashboard" in output
    assert "Reply thread: 1781202023.470699" in output


def test_slack_agent_action_cli_emits_structured_error_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_path = tmp_path / "invalid.json"
    payload_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "handle_slack_agent_action.py",
            "--payload-file",
            str(payload_path),
            "--feedback-jsonl",
            "--json",
        ],
    )

    assert slack_agent_action_cli.main() == 1

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    events = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    assert output["status"] == "error"
    assert output["send_enabled"] is False
    assert output["failure"]["schema"] == "keystone.operator_failure.v1"
    assert output["failure"]["kind"] == "schema_or_parse_error"
    assert output["next_step"]
    assert events[-1]["event_type"] == "agent_error"
    assert events[-1]["payload"]["send_enabled"] is False
    assert events[-1]["payload"]["failure"]["schema"] == "keystone.operator_failure.v1"
    assert events[-1]["payload"]["failure"]["kind"] == "schema_or_parse_error"
    assert events[-1]["payload"]["next_step"]
    assert events[-1]["structured_log"]["schema"] == "keystone.structured_log.v1"
    assert events[-1]["structured_log"]["level"] == "error"
    assert events[-1]["structured_log"]["correlation"]["failure_kind"] == "schema_or_parse_error"
    assert events[-1]["structured_log"]["redaction"]["raw_payload_included"] is False


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
    database_url = _database_url(tmp_path)
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    exit_code = main(
        [
            "ask",
            "--database-url",
            database_url,
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
    events = SQLiteStore(database_url).list_work_item_events(payload["work_item"]["id"])
    advance_started = next(event for event in events if event.event_type == "advance_started")
    assert advance_started.metadata["cost_profile"] == "slack_research_balanced"
    assert advance_started.metadata["allow_manager_loop_repair"] is False
    assert advance_started.metadata["hosted_web_search_max_calls"] == 1
    assert advance_started.metadata["reuse_existing_research"] is True


def test_cli_ask_history_context_resolves_prior_post_company(
    tmp_path: Path,
    capsys,
) -> None:
    context_file = tmp_path / "slack-history.json"
    context_file.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "source": "slack_app_mention_history",
                "channel_id": "C123",
                "thread_ts": "1715366400.000100",
                "request_ts": "1715366460.000200",
                "context_window_days": 14,
                "read_context": (
                    "Slack channel history digest:\n"
                    "Recent candidate messages, newest first:\n"
                    "- ts=1715366390.000050 author=U123 title=company being discussed: "
                    "OpenEvidence: company being discussed: OpenEvidence"
                ),
                "thread_fetch_status": "ok",
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "ask",
            "--database-url",
            _database_url(tmp_path),
            "--context-file",
            str(context_file),
            "--json",
            "@KNI business research analyst Using the selected Slack thread as context, "
            "produce a concise source-backed profile of the company being discussed "
            "in prior post.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["work_item"]["target"]["name"] == "OpenEvidence"
    slack_context = payload["work_item"]["target"]["metadata"]["slack_context"]
    assert slack_context["read_context"].startswith("Slack channel history digest")
    assert slack_context["context_window_days"] == 7
    assert "7-day window" in " ".join(slack_context["warnings"])
    assert "broad channel history is compacted" in slack_context["channel_history_policy"]
    assert payload["context_pack"]["source_refs"][0]["source_type"] == "slack_message"


def test_context_pack_includes_bounded_slack_thread_messages(tmp_path: Path) -> None:
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
        thread_messages=[
            {"ts": "1715366400.000100", "user": "U456", "text": "Selected root"},
            {"ts": "1715366460.000200", "user": "U789", "text": "Thread reply with target detail"},
        ],
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(modal_result.modal_view["private_metadata"]),
        database_url=_database_url(tmp_path),
    )

    assert run_result.result is not None
    slack_context = run_result.result["context_pack"]["target"]["metadata"]["slack_context"]
    assert slack_context["thread_fetch_status"] == "ok"
    assert [item["text"] for item in slack_context["thread_messages"]] == [
        "Selected root",
        "Thread reply with target detail",
    ]
    assert slack_context["latest_user_follow_up"] == "research Acme Health"
    assert slack_context["thread_transcript"].index("Selected root") < slack_context[
        "thread_transcript"
    ].index("Latest operator follow-up:")
    assert slack_context["thread_transcript"].endswith("research Acme Health")


def test_live_slack_run_reuses_thread_sdk_session_for_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keystone_agents.agents.orchestrator import run_orchestrator_preflight
    from keystone_agents.slack_actions import load_selected_message_context_file

    captured: dict[str, object] = {}

    def capture_preflight(request_text, **kwargs):
        captured["session"] = kwargs.get("session")
        return run_orchestrator_preflight(
            request_text,
            live_manual_plan=False,
            database_url=kwargs.get("database_url"),
            workflow_state=kwargs.get("workflow_state"),
        )

    def fake_manager_loop(request, **_kwargs):
        context = load_selected_message_context_file(request.context_file_path)
        work_item = WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            status=WorkItemStatus.DONE,
            title=request.request_text,
            request_text=request.request_text,
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            target=WorkItemTarget(
                metadata={
                    "slack_context": {
                        "channel_id": context.channel_id,
                        "selected_message_ts": context.selected_message_ts,
                        "thread_ts": context.thread_ts,
                    }
                }
            ),
            last_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        )
        return WorkflowRunResult(
            work_item=work_item,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.DONE,
            advanced=True,
            human_summary="done",
        )

    monkeypatch.setattr(
        "keystone_agents.slack_actions.run_orchestrator_preflight",
        capture_preflight,
    )
    monkeypatch.setattr(
        "keystone_agents.slack_actions.advance_work_item_manager_loop",
        fake_manager_loop,
    )
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    handle_run_agent_interaction(
        _modal_submission(modal_result.modal_view["private_metadata"]),
        database_url=_database_url(tmp_path),
        context_dir=tmp_path / "contexts",
        live_sdk=True,
    )

    assert captured["session"] is not None


def test_slack_thread_follow_up_can_reach_chief_of_staff_live_sdk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_chief_of_staff_sdk(typed_input, **kwargs):
        captured["typed_input"] = typed_input
        captured["kwargs"] = kwargs
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary="Summarized Slack follow-ups from the selected thread.",
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="slack-follow-up-review",
                    target_channel="bizdev",
                ),
                send_enabled=False,
                slack_post_allowed=False,
            ),
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
        thread_messages=[
            {"ts": "1715366400.000100", "user": "U456", "text": "Selected root"},
            {"ts": "1715366460.000200", "user": "U789", "text": "Please follow up Monday"},
        ],
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            modal_result.modal_view["private_metadata"],
            task="chief of staff summarize the follow-ups in this Slack thread",
        ),
        database_url=_database_url(tmp_path),
        live_sdk=True,
    )

    assert run_result.route == "chief_of_staff"
    assert run_result.work_item["last_agent"] == "chief_of_staff"
    assert captured["kwargs"]["force_sdk_interpretation"] is True
    assert captured["kwargs"]["include_specialist_tools"] is False
    assert captured["typed_input"]["include_specialist_tools"] is False
    slack_context = captured["typed_input"]["slack_context"]
    orchestrator_context = captured["typed_input"]["orchestrator_context"]
    assert slack_context["channel_id"] == "C123"
    assert [item["text"] for item in slack_context["thread_messages"]] == [
        "Selected root",
        "Please follow up Monday",
    ]
    assert orchestrator_context["raw_request"] == (
        "chief of staff summarize the follow-ups in this Slack thread"
    )
    assert orchestrator_context["orchestrator_preflight"]["selected_agent"] == "chief_of_staff"
    assert orchestrator_context["slack_context"]["channel_id"] == "C123"


def test_slack_chief_of_staff_cross_agent_ask_enables_specialist_tools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_chief_of_staff_sdk(typed_input, **kwargs):
        captured["typed_input"] = typed_input
        captured["kwargs"] = kwargs
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary="Integrated research, opportunity, and outreach context.",
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="portfolio-prioritization",
                    target_channel="bizdev",
                ),
                send_enabled=False,
                slack_post_allowed=False,
            ),
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    payload = _message_action_payload()
    payload["channel"] = {"id": "CWORKFLOW", "name": "ai-agents-workflow"}
    modal_result = handle_run_agent_interaction(
        payload,
        context_dir=tmp_path / "contexts",
        thread_messages=[
            {"ts": "1715366400.000100", "user": "U456", "text": "New company lead"},
            {"ts": "1715366460.000200", "user": "U789", "text": "Could be an outreach target"},
        ],
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            modal_result.modal_view["private_metadata"],
            task=(
                "chief of staff what should we do next across research, "
                "opportunities, and outreach for this Slack thread?"
            ),
        ),
        database_url=_database_url(tmp_path),
        live_sdk=True,
    )

    assert run_result.route == "chief_of_staff"
    assert captured["kwargs"]["include_specialist_tools"] is True
    assert captured["typed_input"]["include_specialist_tools"] is True
    assert captured["typed_input"]["slack_context"]["channel_name"] == "ai-agents-workflow"
    assert captured["typed_input"]["orchestrator_context"]["raw_request"] == (
        "chief of staff what should we do next across research, "
        "opportunities, and outreach for this Slack thread?"
    )


def test_slack_chief_of_staff_negated_context_mentions_do_not_enable_specialist_tools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_chief_of_staff_sdk(typed_input, **kwargs):
        captured["typed_input"] = typed_input
        captured["kwargs"] = kwargs
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary="Read-only handoff ready.",
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="slack-follow-up-review",
                    target_channel="ai-agents-workflow",
                ),
                send_enabled=False,
                slack_post_allowed=False,
            ),
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    payload = _message_action_payload()
    payload["channel"] = {"id": "CWORKFLOW", "name": "ai-agents-workflow"}
    modal_result = handle_run_agent_interaction(
        payload,
        context_dir=tmp_path / "contexts",
        thread_messages=[
            {"ts": "1715366400.000100", "user": "U456", "text": "Inline diagnostic context"},
        ],
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            modal_result.modal_view["private_metadata"],
            task=(
                "chief of staff use only this sanitized inline context from a Gmail triage "
                "diagnostic. Do not access Gmail, Airtable, Google Drive, Zotero, or live "
                "web. Return a concise handoff. Do not draft outreach, send email, create "
                "a Gmail draft, label messages, schedule, write files, create CRM records, "
                "publish, or post elsewhere."
            ),
        ),
        database_url=_database_url(tmp_path),
        live_sdk=True,
    )

    assert run_result.route == "chief_of_staff"
    assert captured["kwargs"]["include_specialist_tools"] is False
    assert captured["typed_input"]["include_specialist_tools"] is False


def test_slack_chief_of_staff_local_kni_followup_gets_evidence_packet(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_chief_of_staff_sdk(typed_input, **kwargs):
        captured["typed_input"] = typed_input
        captured["kwargs"] = kwargs
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "The local KNI evidence identifies ProAssurance as broker/producer. "
                    "Evidence path: 00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="project-context-review",
                    target_channel="current-thread",
                ),
                send_enabled=False,
                slack_post_allowed=False,
                retrieval_diagnostics={
                    "local_only": True,
                    "send_enabled": False,
                    "evidence_path": "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf",
                },
            ),
            raw_result={"sdk": "called"},
            live=True,
        )

    def fake_plan_chief_of_staff_request(*_args, **_kwargs):
        raise AssertionError("local KNI live evidence must be built from the query")

    def fake_build_local_kni_evidence_packet_for_query(query_text, *, max_candidate_documents=5):
        captured["packet_query_text"] = query_text
        return {
            "packet_type": "bounded_local_kni_document_evidence",
            "local_only": True,
            "send_enabled": False,
            "candidate_documents": [
                {
                    "relative_path": "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf",
                    "content_excerpt": "PRODUCER IAO, Inc. DBA ProAssurance Agency",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                    "local_only": True,
                    "send_enabled": False,
                }
            ],
            "retrieval_diagnostics": {
                "local_only": True,
                "send_enabled": False,
                "effective_lookup_kind": "insurance",
                "effective_answer_focus": "broker",
            },
        }

    monkeypatch.setattr(
        "keystone_agents.workflow_runner.run_chief_of_staff_sdk",
        fake_run_chief_of_staff_sdk,
    )
    monkeypatch.setattr(
        "keystone_agents.workflow_runner.plan_chief_of_staff_request",
        fake_plan_chief_of_staff_request,
    )
    monkeypatch.setattr(
        "keystone_agents.workflow_runner.build_local_kni_evidence_packet_for_query",
        fake_build_local_kni_evidence_packet_for_query,
    )

    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
        thread_messages=[
            {"ts": "1715366400.000100", "user": "U456", "text": "CFC insurance provider was discussed"},
            {"ts": "1715366460.000200", "user": "U789", "text": "Actually it was ProAssurance"},
        ],
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            modal_result.modal_view["private_metadata"],
            task="chief of staff who was the broker for the CFC insurance?",
        ),
        database_url=_database_url(tmp_path),
        live_sdk=True,
    )

    typed_input = captured["typed_input"]

    assert run_result.route == "chief_of_staff"
    assert typed_input["local_kni_evidence_packet"]["local_only"] is True
    assert typed_input["local_kni_evidence_packet"]["candidate_documents"][0][
        "relative_path"
    ].endswith("COI_AnupSharma_2026.pdf")
    assert "not as a prewritten final answer" in typed_input["local_kni_instruction"]
    assert typed_input["orchestrator_context"]["local_kni_evidence_prefetch"] is True
    assert captured["packet_query_text"] == "chief of staff who was the broker for the CFC insurance?"


def test_slack_natural_followup_with_pending_approval_reaches_workflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)
    SQLiteStore(database_url).save_approval_item(
        ApprovalQueueItem(
            id="approval-pending",
            object_type="outreach_draft",
            object_id="draft-1",
            title="Draft approval",
            summary="Review draft before external use.",
            source_agent="outreach_composer",
        )
    )
    captured: dict[str, object] = {}

    def fake_manager_loop(request, *, feedback_callback=None):
        captured["request"] = request
        slack_context = {
            "channel_id": "C123",
            "selected_message_ts": "1715366400.000100",
            "thread_ts": "1715366400.000100",
        }
        work_item = WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Source follow-up",
            request_text=request.request_text,
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            target=WorkItemTarget(metadata={"slack_context": slack_context}),
            status=WorkItemStatus.DONE,
            last_agent=WorkItemRoute.CHIEF_OF_STAFF.value,
        )
        return WorkflowRunResult(
            work_item=work_item,
            route=WorkItemRoute.CHIEF_OF_STAFF,
            status=WorkItemStatus.DONE,
            advanced=True,
            human_summary="Allowed natural source follow-up to workflow.",
        )

    monkeypatch.setattr(
        "keystone_agents.slack_actions.advance_work_item_manager_loop",
        fake_manager_loop,
    )
    modal_result = handle_run_agent_interaction(
        _message_action_payload(),
        context_dir=tmp_path / "contexts",
    )

    run_result = handle_run_agent_interaction(
        _modal_submission(
            modal_result.modal_view["private_metadata"],
            task="chief of staff continue this prior Slack thread. can u summarize link 1",
        ),
        database_url=database_url,
        context_dir=tmp_path / "contexts",
    )

    assert run_result.status == WorkItemStatus.DONE.value
    assert run_result.route == WorkItemRoute.CHIEF_OF_STAFF.value
    assert "request" in captured
    forwarded_request = captured["request"]
    assert forwarded_request.request_text == (
        "chief of staff continue this prior Slack thread. can u summarize link 1"
    )
    preflight = forwarded_request.orchestrator_preflight
    assert preflight["selected_agent"] == "chief_of_staff"
    assert preflight["blocked_by_orchestrator"] is False
    assert preflight["route_result"]["route"] == "chief_of_staff"
